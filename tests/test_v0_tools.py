"""#320 V-0 工具的離線測試：v0_prepare 與 v0_load_client。

真起一個 lgwd 子行程（warn 模式、127.0.0.1 隨機埠），用外部埠客戶端打 2 秒，斷言幾乎全 200、
JSON 欄位齊全、連線被拒單獨計、SIGTERM 時乾淨結束；prepare 的輸出不含金鑰內容、policy
確實關掉用量抓取。只用臨時目錄與 127.0.0.1，不碰 /var/lib、/etc 與真金鑰。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from tests import v0_prepare
from tests.subprocess_policy import usage_all_disabled, _fetcher_providers
from tests.test_main import FAKE_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_RESULT_KEYS = {"elapsed_s", "requests", "statuses", "errors", "connection_refused", "rate"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _client(directory: str, port: int, duration: str = "2", concurrency: str = "8"):
    cmd = [sys.executable, "-m", "tests.v0_load_client", "--dir", directory,
           "--port", str(port), "--duration", duration, "--concurrency", concurrency]
    return subprocess.run(cmd, cwd=REPO_ROOT, env=_env(), capture_output=True,
                          text=True, timeout=60)


class PrepareTest(unittest.TestCase):
    def test_prepare_prints_usable_args_without_key_material(self):
        with tempfile.TemporaryDirectory() as d:
            port = _free_port()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = v0_prepare.main(["--dir", d, "--mode", "warn", "--port", str(port)])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            lines = text.splitlines()
            self.assertTrue(lines[0].startswith("lgwd-args: "), text)
            self.assertIn("client-env: LGW_V0_DIR LGW_V0_PORT", lines)
            argv = shlex.split(lines[0][len("lgwd-args: "):])
            for flag in ("--auth-mode", "--auth-state-dir", "--auth-registry", "--api-port",
                         "--db", "--keys", "--policy", "--keysync-interval"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--api-port") + 1], str(port))
            self.assertEqual(argv[argv.index("--auth-mode") + 1], "warn")
            # 金鑰內容一律不出現在輸出裡（假 key 值與私鑰全文）
            private = (Path(d) / v0_prepare.V0_KEY_NAME).read_text(encoding="ascii")
            for value in FAKE_KEYS.values():
                self.assertNotIn(value, text)
            self.assertNotIn(private.strip().splitlines()[1], text)

    def test_policy_turns_every_usage_fetcher_off(self):
        with tempfile.TemporaryDirectory() as d:
            v0_prepare.prepare(d, "enforce", _free_port())
            policy = json.loads((Path(d) / "policy.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["usage"], usage_all_disabled())
            self.assertTrue(_fetcher_providers(), "expected at least one usage fetcher")
            for name in _fetcher_providers():
                self.assertEqual(policy["usage"][name], {"enabled": False}, name)

    def test_secret_files_are_0600_and_directory_must_be_empty(self):
        with tempfile.TemporaryDirectory() as d:
            v0_prepare.prepare(d, "warn", _free_port())
            self.assertEqual((Path(d) / "keys.env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((Path(d) / v0_prepare.V0_KEY_NAME).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                v0_prepare.prepare(d, "warn", _free_port())

    def test_bad_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                v0_prepare.prepare(d, "off", _free_port())


class LoadClientTest(unittest.TestCase):
    def test_client_against_warn_process_is_almost_all_200(self):
        with tempfile.TemporaryDirectory() as d:
            port = _free_port()
            argv = v0_prepare.prepare(d, "warn", port)
            proc = subprocess.Popen([sys.executable, "-m", "lgwd", *argv], cwd=REPO_ROOT,
                                    env=_env(), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                            break
                    except OSError:
                        if proc.poll() is not None or time.monotonic() > deadline:
                            self.fail("lgwd did not start")
                        time.sleep(0.1)
                done = _client(d, port, duration="2", concurrency="8")
            finally:
                proc.terminate()
                proc.wait(timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout)
            self.assertTrue(CLIENT_RESULT_KEYS <= set(result), result)
            self.assertGreater(result["requests"], 0, result)
            ok = result["statuses"].get("200", 0)
            self.assertGreaterEqual(ok, 0.99 * result["requests"], result)
            self.assertEqual(result["connection_refused"], 0, result)
            self.assertEqual(result["errors"], {}, result)
            self.assertGreater(result["rate"], 0, result)

    def test_refused_connections_are_counted_apart_from_statuses(self):
        with tempfile.TemporaryDirectory() as d:
            v0_prepare.prepare(d, "warn", _free_port())
            done = _client(d, _free_port(), duration="1", concurrency="4")
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout)
            self.assertGreater(result["connection_refused"], 0, result)
            self.assertEqual(result["statuses"], {}, result)
            self.assertEqual(result["errors"], {}, result)

    def _with_fake_server(self, mode: str, duration: str = "1", concurrency: str = "4"):
        """假服務。模式：`503`＝回 503；`garbage`＝回非 HTTP 的字；`reset`＝讀完就 RST 切斷；
        `reset_then_garbage`＝前半段 RST、過了一半時間後改回 garbage；`silent`＝收了請求卻不回，
        客戶端會在 socket 逾時時記錯誤（用來鎖「第三種錯誤也有 window」）。

        回傳 (客戶端 JSON, 伺服器紀錄)；紀錄是 (種類, time.time())，種類 `reset`／`garbage`／`503`，
        時間是伺服器在送出該回應或 RST 之前記下的。
        """
        import struct
        import threading
        log: list = []
        with tempfile.TemporaryDirectory() as d:
            v0_prepare.prepare(d, "warn", _free_port())
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(64)
            server.settimeout(0.2)
            port = server.getsockname()[1]
            halt = threading.Event()
            held: list = []
            began = time.time()
            switch_at = began + float(duration) / 2

            def serve() -> None:
                while not halt.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    if mode == "silent":
                        held.append(conn)
                        continue
                    with conn:
                        conn.recv(65536)
                        now = time.time()
                        if mode == "503":
                            log.append(("503", now))
                            conn.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
                        elif mode == "garbage" or (mode == "reset_then_garbage" and now >= switch_at):
                            log.append(("garbage", now))
                            conn.sendall(b"HELLO\r\n\r\n")
                        elif mode in ("reset", "reset_then_garbage"):
                            log.append(("reset", now))
                            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                done = _client(d, port, duration=duration, concurrency=concurrency)
            finally:
                halt.set()
                thread.join(timeout=5)
                server.close()
                for conn in held:
                    conn.close()
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout), list(log)

    def test_503_is_reported_as_status_and_never_as_200(self):
        result, _ = self._with_fake_server("503")
        self.assertGreater(result["statuses"].get("503", 0), 0, result)
        self.assertEqual(set(result["statuses"]), {"503"}, result)
        self.assertEqual(result["rate"], 0.0, result)
        self.assertEqual(result["connection_refused"], 0, result)

    def test_rst_is_counted_as_connection_reset_with_time_window(self):
        result, log = self._with_fake_server("reset")
        self.assertGreater(result["connection_reset"], 0, result)
        self.assertEqual(result["connection_reset"], result["errors"].get("ConnectionResetError"), result)
        self.assertEqual(result["bad_status_line"], 0, result)
        self.assertEqual(result["statuses"], {}, result)
        window = result["error_window"]["ConnectionResetError"]
        self.assertLessEqual(window["first"], window["last"], result)

    def test_error_window_first_is_the_first_occurrence_not_the_latest(self):
        # 低-4／M10：連續製造多次、不同時間的 reset。first 必須是第一次那一下，last 是最後一次。
        # 伺服器在 RST 之前記下時間；客戶端在收到錯誤之後才記，所以 first 只會晚，不會早。
        result, log = self._with_fake_server("reset")
        resets = [t for kind, t in log if kind == "reset"]
        self.assertGreater(len(resets), 5, "需要連續多次 reset 才測得出 first 與 last 的差")
        self.assertGreater(resets[-1] - resets[0], 0.5, "測試前提：reset 要橫跨一段時間")
        window = result["error_window"]["ConnectionResetError"]
        self.assertGreaterEqual(window["first"], resets[0] - 0.01, result)
        self.assertLess(window["first"] - resets[0], 0.5, f"first 不是第一次出現：{window} {resets[0]}")
        self.assertLess(window["first"], window["last"], result)
        self.assertGreaterEqual(window["last"], resets[-1] - 0.5, result)
        self.assertGreater(window["last"] - window["first"], 0.5, result)

    def test_reset_window_ends_before_garbage_window_begins(self):
        # 中-2 的機制：前段 RST、後段 garbage；兩個 window 必須能分開。
        result, log = self._with_fake_server("reset_then_garbage")
        self.assertGreater(result["connection_reset"], 0, result)
        self.assertGreater(result["bad_status_line"], 0, result)
        reset_window = result["error_window"]["ConnectionResetError"]
        garbage_window = result["error_window"]["bad_status_line"]
        self.assertLess(reset_window["last"], garbage_window["first"], result)
        self.assertEqual(log[0][0], "reset", log)
        self.assertEqual(log[-1][0], "garbage", log)

    def test_garbage_reply_is_counted_as_bad_status_line(self):
        result, _ = self._with_fake_server("garbage")
        self.assertGreater(result["bad_status_line"], 0, result)
        self.assertEqual(result["bad_status_line"], result["errors"].get("bad_status_line"), result)
        self.assertEqual(result["connection_reset"], 0, result)
        self.assertIn("bad_status_line", result["error_window"], result)

    def test_silent_peer_timeout_is_a_third_kind_with_a_window(self):
        result, _ = self._with_fake_server("silent")
        self.assertGreater(sum(result["errors"].values()), 0, result)
        self.assertTrue(any("imeout" in kind for kind in result["errors"]), result)
        self.assertEqual(set(result["errors"]), set(result["error_window"]), result)
        self.assertEqual(result["connection_reset"], 0, result)
        self.assertEqual(result["bad_status_line"], 0, result)

    def test_every_error_kind_gets_a_window_and_no_kind_is_missing(self):
        # 低-5 的測試面：errors 出現過的每一種，error_window 都有同名的一格（first≤last）。
        # 不鎖「哪些型別算切斷」——那是文件判準，不在這裡。
        for mode in ("503", "garbage", "reset", "reset_then_garbage", "silent"):
            with self.subTest(mode=mode):
                result, _ = self._with_fake_server(mode)
                self.assertEqual(set(result["error_window"]), set(result["errors"]), result)
                for kind, window in result["error_window"].items():
                    self.assertLessEqual(window["first"], window["last"], (kind, result))

    def test_sigterm_ends_client_cleanly_and_still_prints_result(self):
        with tempfile.TemporaryDirectory() as d:
            v0_prepare.prepare(d, "warn", _free_port())
            cmd = [sys.executable, "-m", "tests.v0_load_client", "--dir", d,
                   "--port", str(_free_port()), "--duration", "60", "--concurrency", "4"]
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=_env(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.stderr.readline().strip(), 'v0_load_client: ready')
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("client did not exit within 20s after SIGTERM")
            self.assertEqual(proc.returncode, 0, err)
            result = json.loads(out)
            self.assertTrue(CLIENT_RESULT_KEYS <= set(result), result)
            self.assertLess(result["elapsed_s"], 20, result)


if __name__ == "__main__":
    unittest.main()
