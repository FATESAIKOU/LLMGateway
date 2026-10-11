"""lgwd/main.py 的測試：假 key 檔、真 policy/default.json、隨機埠。

涵蓋 spec subscription-registry 與 key-vault 的服務端 Scenario 在主程式裡的行為
（不帶應用層憑證、換 key、只綁 loopback、SIGHUP／SIGTERM）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lgwd.clock import FakeClock
from lgwd.keys import KEY_NAMES
from lgwd.main import Config, Gateway, load_config

REPO = Path(__file__).resolve().parent.parent
FAKE_KEYS = {sub: f"test-main-{sub}-000" for sub in
             ("claude-personal", "claude-company", "openrouter",
              "opencode-zen", "ollama-cloud")}


def key_text(values):
    lines = ["# 測試用假 key"]
    for sub, name in KEY_NAMES.items():
        if sub in values:
            lines.append(f"{name}={values[sub]}")
    return "\n".join(lines) + "\n"


def http_get(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def http_post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class MainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "keys.env").write_text(key_text(FAKE_KEYS), encoding="utf-8")
        shutil.copy(REPO / "policy" / "default.json", self.root / "policy.json")
        self.config = Config(
            db=str(self.root / "llm.db"),
            policy_path=str(self.root / "policy.json"),
            keys_path=str(self.root / "keys.env"),
            api_host="127.0.0.1",
            api_port=0,
        )
        self.clock = FakeClock()
        self.gw = Gateway(self.config, clock=self.clock)
        self.gw.start()
        host, port = self.gw.addr[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.gw.stop()
        self.tmp.cleanup()

    def list_items(self):
        status, raw = http_get(self.base + "/v1/subscriptions")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["schema"], 1)
        return {item["id"]: item for item in data["subscriptions"]}

    def test_服務只綁loopback(self):
        # spec key-vault：服務不對外，只綁 127.0.0.1
        self.assertEqual(self.gw.api_server.server_address[0], "127.0.0.1")


class ConfigTest(unittest.TestCase):
    def test_設定解析_argv優先(self):
        cfg = load_config(["--db", "/tmp/a.db", "--api-host", "127.0.0.1",
                           "--api-port", "1"], env={})
        self.assertEqual((cfg.db, cfg.api_host, cfg.api_port), ("/tmp/a.db", "127.0.0.1", 1))

    def test_設定解析_env與預設(self):
        cfg = load_config([], env={"LGWD_API_HOST": "127.0.0.1", "LGWD_API_PORT": "9999"})
        self.assertEqual((cfg.api_host, cfg.api_port), ("127.0.0.1", 9999))
        cfg2 = load_config([], env={})
        self.assertEqual((cfg2.api_host, cfg2.api_port), ("127.0.0.1", 8480))

    def test_設定解析_錯誤值(self):
        for argv, env in (
            (["--api-port", "99999"], {}),
            (["--api-port", "abc"], {}),
            (["--api-host", "0.0.0.0"], {}),
            (["--api-host", "::1"], {}),
            (["--api-host", "localhost"], {}),
            ([], {"LGWD_API_HOST": "10.0.0.9"}),
            ([], {"LGWD_API_PORT": "70000"}),
        ):
            with self.subTest(argv=argv, env=env):
                with self.assertRaises(ValueError):
                    load_config(argv, env=env)

    def test_服務不對外的位址被拒(self):
        # spec key-vault：服務只綁 127.0.0.1，其他位址不啟動
        for host in ("0.0.0.0", "::1", "localhost", "10.0.0.9"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    load_config(["--api-host", host], env={})


class ApiTest(MainCase):
    def test_list不帶憑證且不含key(self):
        status, raw = http_get(self.base + "/v1/subscriptions")
        self.assertEqual(status, 200)  # 不做應用層認證
        data = json.loads(raw)
        self.assertEqual(len(data["subscriptions"]), 5)
        blob = raw.decode()
        for value in FAKE_KEYS.values():
            self.assertNotIn(value, blob)
        for item in data["subscriptions"]:
            self.assertNotIn("key", item)
        priorities = [item["priority"] for item in data["subscriptions"]]
        self.assertEqual(priorities, sorted(priorities))

    def test_get帶agent含key(self):
        status, raw = http_get(
            self.base + "/v1/subscriptions/claude-personal?agent=claude-code")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["key"], FAKE_KEYS["claude-personal"])

    def test_opencode拿不到Claude訂閱(self):
        status, raw = http_get(
            self.base + "/v1/subscriptions/claude-company?agent=opencode")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw), {"error": "agent_not_allowed"})

    def test_缺agent回400(self):
        status, _ = http_get(self.base + "/v1/subscriptions/claude-personal")
        self.assertEqual(status, 400)

    def test_不存在的id回404(self):
        status, _ = http_get(self.base + "/v1/subscriptions/ghost?agent=opencode")
        self.assertEqual(status, 404)

    def test_key沒部署回409(self):
        (self.root / "keys.env").write_text(
            key_text({k: v for k, v in FAKE_KEYS.items() if k != "ollama-cloud"}),
            encoding="utf-8")
        self.gw.keys.reload()
        status, raw = http_get(
            self.base + "/v1/subscriptions/ollama-cloud?agent=claude-code")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw), {"error": "not_deployed"})
        self.assertFalse(self.list_items()["ollama-cloud"]["available"])

    def test_冷卻流程(self):
        status, _ = http_post(self.base + "/v1/subscriptions/claude-personal/cooldown",
                              {"window": "5h", "resets_at": "2026-10-11T15:00:00Z"})
        self.assertEqual(status, 204)
        item = self.list_items()["claude-personal"]
        self.assertFalse(item["available"])
        self.assertEqual(item["cooldown"]["5h"]["until"], "2026-10-11T15:02:00Z")
        self.clock.advance(hours=16)
        self.assertTrue(self.list_items()["claude-personal"]["available"])

    def test_冷卻錯誤對應(self):
        status, _ = http_post(self.base + "/v1/subscriptions/ghost/cooldown",
                              {"window": "5h"})
        self.assertEqual(status, 404)
        status, _ = http_post(self.base + "/v1/subscriptions/claude-personal/cooldown",
                              {"window": "24h"})
        self.assertEqual(status, 400)

    def test_其他路徑404(self):
        for path in ("/v1/board", "/v1/leases", "/v1/whoami", "/board", "/nope"):
            with self.subTest(path=path):
                status, _ = http_get(self.base + path)
                self.assertEqual(status, 404)

    def test_關閉可重複呼叫(self):
        self.gw.stop()
        self.gw.stop()


class ReloadTest(MainCase):
    def test_換key(self):
        status, raw = http_get(
            self.base + "/v1/subscriptions/claude-personal?agent=claude-code")
        self.assertEqual(json.loads(raw)["key"], FAKE_KEYS["claude-personal"])
        rotated = dict(FAKE_KEYS, **{"claude-personal": "test-main-rotated-111"})
        (self.root / "keys.env").write_text(key_text(rotated), encoding="utf-8")
        self.gw.reload()
        status, raw = http_get(
            self.base + "/v1/subscriptions/claude-personal?agent=claude-code")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["key"], "test-main-rotated-111")
        blob = http_get(self.base + "/v1/subscriptions")[1].decode()
        self.assertNotIn("test-main-rotated-111", blob)

    def test_SIGHUP壞key檔沿用舊的(self):
        (self.root / "keys.env").write_text("NOT A LINE\n", encoding="utf-8")
        self.gw.reload()
        status, raw = http_get(
            self.base + "/v1/subscriptions/claude-personal?agent=claude-code")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["key"], FAKE_KEYS["claude-personal"])
        self.assertIsNotNone(self.gw.keys.last_reload_error)
        for value in (*FAKE_KEYS.values(), "NOT A LINE"):
            self.assertNotIn(value, self.gw.keys.last_reload_error or "")

    def test_SIGHUP壞policy沿用舊的(self):
        svc_before = self.gw.service
        (self.root / "policy.json").write_text("{bad json", encoding="utf-8")
        self.gw.reload()
        self.assertIs(self.gw.service, svc_before)
        self.assertEqual(len(self.list_items()), 5)

    def test_SIGHUP好policy換上新的(self):
        data = json.loads((self.root / "policy.json").read_text(encoding="utf-8"))
        data["subscriptions"]["ollama-cloud"]["priority"] = 1
        (self.root / "policy.json").write_text(json.dumps(data), encoding="utf-8")
        self.gw.reload()
        status, raw = http_get(self.base + "/v1/subscriptions")
        self.assertEqual(status, 200)
        first = json.loads(raw)["subscriptions"][0]["id"]
        self.assertEqual(first, "ollama-cloud")

    def test_python_m_lgwd_help(self):
        proc = subprocess.run([sys.executable, "-m", "lgwd", "--help"],
                              capture_output=True, timeout=30, cwd=str(REPO))
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"usage: lgwd", proc.stdout)

    @staticmethod
    def _free_port():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "no SIGHUP")
    def test_子程序_SIGHUP與SIGTERM_log(self):
        api_port = self._free_port()
        env = dict(os.environ,
                   LGWD_DB=str(self.root / "child.db"),
                   LGWD_POLICY=str(self.root / "policy.json"),
                   LGWD_KEYS=str(self.root / "keys.env"),
                   LGWD_API_HOST="127.0.0.1",
                   LGWD_API_PORT=str(api_port))
        proc = subprocess.Popen([sys.executable, "-m", "lgwd"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(REPO), env=env)
        try:
            up = False
            for _ in range(100):
                try:
                    status, _ = http_get(
                        f"http://127.0.0.1:{api_port}/v1/subscriptions")
                    up = status == 200
                    if up:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            self.assertTrue(up)
            proc.send_signal(signal.SIGHUP)
            time.sleep(1.0)
            proc.terminate()  # SIGTERM
            _, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0)
            self.assertIn(b"reload ok", stderr)
            self.assertIn(b"SIGTERM", stderr)
            for value in FAKE_KEYS.values():
                self.assertNotIn(value.encode(), stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
