"""lgwd 子程序的內建 key 拉取整合測試（#35 T2）：成功／失敗／逾時都不影響 API。

用 `python3 -m lgwd` 起真的服務（隨機埠、假 key、repo 的 policy、假 systemd 憑證
目錄），key-sync 換成假的 bash script（不連網、不用真 key）：只驗 API、狀態檔、
log 與 SIGTERM 的乾淨結束。子行程本身的 stderr 用檔案接，避免 pipe 塞住。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from lgwd.keys import KEY_NAMES

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PASSWORD = "FakeKeysyncApiPassword_ForTests_0011"  # 假值，只在測試裡用
PASSWORD_ENV = "LLMGW_KEYS_PASSWORD"
# PYTHON_CMD 由測試換成 sys.executable（只記密碼的 sha256，不記值）
FAKE_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
set -uo pipefail
{
  printf 'run\\n'
  printf 'argv:%s\\n' "$*"
  printf 'pid:%s\\n' "$$"
  PYTHON_CMD -c 'import hashlib, os; pw = os.environ.get("LLMGW_KEYS_PASSWORD") or ""; print("password_sha: " + (hashlib.sha256(pw.encode()).hexdigest() if pw else "none"))'
} >> "${FAKE_KS_LOG:?}"
case "${FAKE_KS_MODE:-ok}" in
  ok) exit 0 ;;
  fail) echo "failed=gh_auth gh 沒登入（假）" >&2; exit 5 ;;
  hang) exec sleep 60 ;;
esac
exit 1
"""


def key_text(prefix: str = "test-keysync-api") -> str:
    lines = ["# 測試用假 key（不是真值）"]
    for sub, name in KEY_NAMES.items():
        lines.append(f"{name}={prefix}-{sub}")
    return "\n".join(lines) + "\n"


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class KeysyncApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.keys_path = os.path.join(self.state, "keys.env")
        with open(self.keys_path, "w", encoding="utf-8") as fh:
            fh.write(key_text())
        self.policy = os.path.join(self.root, "policy.json")
        with open(os.path.join(REPO, "policy", "default.json"), encoding="utf-8") as fh:
            policy_text = fh.read()
        # 起真 lgwd 子行程：關掉 Ollama 取用量，免得測試對外連線（#60）
        policy_obj = json.loads(policy_text)
        policy_obj["usage"] = {"ollama-cloud": {"enabled": False}}
        with open(self.policy, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(policy_obj))
        self.cred = os.path.join(self.root, "cred")
        os.makedirs(self.cred)
        with open(os.path.join(self.cred, "keys.pass"), "w", encoding="utf-8") as fh:
            fh.write(f"{PASSWORD_ENV}={PASSWORD}\n")
        self.log = os.path.join(self.root, "keysync.log")
        self.stderr_path = os.path.join(self.root, "lgwd.stderr")
        self.stderr_fh = open(self.stderr_path, "wb")
        self.addCleanup(self.stderr_fh.close)
        self.script = os.path.join(self.root, "fake-key-sync")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(FAKE_SCRIPT_TEMPLATE.replace("PYTHON_CMD", shlex.quote(sys.executable)))
        os.chmod(self.script, 0o755)
        self.port = self._free_port()
        self.proc = None
        self.addCleanup(self.stop)

    @staticmethod
    def _free_port():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def env(self, **overrides):
        base = dict(
            os.environ,
            LGWD_DB=os.path.join(self.state, "llm.db"),
            LGWD_POLICY=self.policy,
            LGWD_KEYS=self.keys_path,
            LGWD_API_HOST="127.0.0.1",
            LGWD_API_PORT=str(self.port),
            LGW_KEYSYNC_SCRIPT=self.script,
            LGW_KEYSYNC_INTERVAL="0.2",
            LGW_KEYSYNC_TIMEOUT="30",
            CREDENTIALS_DIRECTORY=self.cred,
            FAKE_KS_LOG=self.log,
        )
        base.update(overrides)
        return base

    def start(self, **overrides):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lgwd"],
            stdout=subprocess.DEVNULL,
            stderr=self.stderr_fh,
            cwd=REPO,
            env=self.env(**overrides),
        )
        self.assertTrue(wait_until(lambda: self.list_status() == 200, timeout=15),
                        "服務沒起來")
        return self.proc

    def stop(self, timeout=15):
        """SIGTERM 收掉服務；回 (returncode, stderr 文字)。"""
        if self.proc is None:
            return None, ""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)
        text = self.read_stderr()
        return self.proc.returncode, text

    def read_stderr(self):
        try:
            with open(self.stderr_path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def read_log(self):
        try:
            with open(self.log, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def status_raw(self):
        try:
            with open(os.path.join(self.state, ".keysync.status"), encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def read_status(self):
        raw = self.status_raw()
        return json.loads(raw) if raw else None

    def wait_status(self, predicate=lambda data: True, timeout=15):
        """等狀態檔出現（且符合 predicate）；回狀態 dict；逾時 fail。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.read_status()
            if data is not None and predicate(data):
                return data
            time.sleep(0.02)
        self.fail(f"狀態檔沒出現或不合：{self.status_raw()!r}")

    def wait_log(self, needle, timeout=15):
        self.assertTrue(wait_until(lambda: needle in self.read_log(), timeout=timeout),
                        f"假 key-sync 的 log 沒有 {needle!r}")

    def list_status(self):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/v1/subscriptions", timeout=5) as resp:
                if resp.status == 200:
                    json.loads(resp.read())
                return resp.status
        except OSError:
            return None
        except ValueError:
            return -1

    def assert_no_leak(self, text):
        self.assertNotIn(PASSWORD, text)
        for value in key_text().splitlines():
            if "=" in value:
                self.assertNotIn(value.split("=", 1)[1], text)

    # --- 測試 -------------------------------------------------------------
    def test_成功_狀態ok_API照樣200_SIGTERM乾淨(self):
        # interval 拉長（5s）讓「啟動後先跑一次、還沒跑第二次」這段沒有競態（低-6）
        self.start(FAKE_KS_MODE="ok", LGW_KEYSYNC_INTERVAL="5")
        data = self.wait_status(lambda d: d["reason"] == "ok")
        self.assertTrue(data["ok"])
        self.assertEqual(set(data), {"ts", "ok", "reason"})
        status_path = os.path.join(self.state, ".keysync.status")
        self.assertEqual(stat.S_IMODE(os.stat(status_path).st_mode), 0o644)
        self.assertEqual(self.status_raw().count("\n"), 1)
        self.assertEqual(self.list_status(), 200)
        self.assertIsNone(self.proc.poll())  # 服務還活著
        self.assertEqual(self.read_log().count("run"), 1)  # 啟動後先跑一次（不等 interval）
        # 之後照 interval：第二次在 ~5 秒後
        self.assertTrue(wait_until(lambda: self.read_log().count("run") >= 2, timeout=15))
        self.assertIn("password_sha: " + hashlib.sha256(PASSWORD.encode()).hexdigest(),
                      self.read_log())
        self.assertNotIn(PASSWORD, self.read_log())  # 值不落地
        rc, stderr = self.stop()
        self.assertEqual(rc, 0)
        self.assertIn("SIGTERM", stderr)
        self.assertIn("keysync ok", stderr)
        self.assert_no_leak(stderr)
        self.assert_no_leak(self.status_raw())

    def test_失敗_狀態記failed代碼_API照樣200(self):
        self.start(FAKE_KS_MODE="fail")
        data = self.wait_status(lambda d: d["reason"] == "gh_auth")
        self.assertFalse(data["ok"])
        for _ in range(3):
            self.assertEqual(self.list_status(), 200)
        self.assertIsNone(self.proc.poll())
        rc, stderr = self.stop()
        self.assertEqual(rc, 0)
        self.assertIn("keysync failed reason=gh_auth", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assert_no_leak(stderr)
        self.assert_no_leak(self.status_raw())

    def test_逾時_狀態記timeout_卡住時API照樣200(self):
        self.start(FAKE_KS_MODE="hang", LGW_KEYSYNC_TIMEOUT="0.5",
                   LGW_KEYSYNC_INTERVAL="5")
        self.wait_log("argv:")  # key-sync 已經跑起來（還在卡）
        self.assertEqual(self.list_status(), 200)
        data = self.wait_status(lambda d: d["reason"] == "timeout")
        self.assertFalse(data["ok"])
        self.assertEqual(self.list_status(), 200)
        self.assertIsNone(self.proc.poll())
        rc, stderr = self.stop()
        self.assertEqual(rc, 0)
        self.assertIn("keysync failed reason=timeout", stderr)
        # 逾時的子行程（sleep）要被殺掉，不留孤兒
        pids = [int(line.split(":", 1)[1]) for line in self.read_log().splitlines()
                if line.startswith("pid:")]
        for pid in pids:
            self.assertTrue(wait_until(lambda: not is_pid_alive(pid), timeout=5),
                            f"子行程 {pid} 沒被殺掉")

    def test_SIGTERM時卡住的key_sync被叫停_服務立刻結束(self):
        self.start(FAKE_KS_MODE="hang", LGW_KEYSYNC_TIMEOUT="60",
                   LGW_KEYSYNC_INTERVAL="5")
        self.wait_log("pid:")
        pid = int([line.split(":", 1)[1] for line in self.read_log().splitlines()
                   if line.startswith("pid:")][-1])
        started = time.monotonic()
        rc, _ = self.stop(timeout=15)
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertTrue(wait_until(lambda: not is_pid_alive(pid), timeout=5))
        self.assertIsNone(self.status_raw())  # 打斷的那次不寫狀態（不記成失敗）


if __name__ == "__main__":
    unittest.main()
