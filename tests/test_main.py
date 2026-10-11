"""lgwd/main.py 的測試：假 key 檔、真 policy/default.json、隨機埠。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lgwd.clock import FakeClock
from lgwd.keys import KEY_NAMES
from lgwd.main import Gateway, _BoardWithUsage, load_config
from lgwd.usage import UsagePoller

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


def http_get(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(url, headers=headers)
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
        from lgwd.main import Config
        self.config = Config(
            db=str(self.root / "llm.db"),
            policy_path=str(self.root / "policy.json"),
            keys_path=str(self.root / "keys.env"),
            api_hosts=("127.0.0.1",),
            api_port=0,
            board_port=0,
            usage_enabled=False,
        )
        self.clock = FakeClock()
        self.gw = Gateway(self.config, clock=self.clock)
        self.gw.start()
        host, port = self.gw.api_addrs[0][:2]
        self.base = f"http://{host}:{port}"
        bhost, bport = self.gw.board_addr[:2]
        self.board_base = f"http://{bhost}:{bport}"

    def tearDown(self):
        self.gw.stop()
        self.tmp.cleanup()

    def _token(self, name="w1"):
        return self.gw.service.add_client(name)

    # --- 設定 ---
    def test_設定解析_argv優先(self):
        cfg = load_config(["--db", "/tmp/a.db", "--api-host", "10.0.0.1",
                           "--api-host", "10.0.0.2", "--api-port", "1",
                           "--board-port", "2", "--no-usage"],
                          env={})
        self.assertEqual((cfg.db, cfg.api_hosts, cfg.api_port,
                          cfg.board_port, cfg.usage_enabled),
                         ("/tmp/a.db", ("10.0.0.1", "10.0.0.2"), 1, 2, False))

    def test_設定解析_env與預設(self):
        cfg = load_config([], env={"LGWD_API_HOSTS": "127.0.0.1, 10.0.0.9",
                                   "LGWD_USAGE": "0"})
        self.assertEqual(cfg.api_hosts, ("127.0.0.1", "10.0.0.9"))
        self.assertFalse(cfg.usage_enabled)
        self.assertEqual(cfg.api_port, 8480)
        cfg2 = load_config([], env={})
        self.assertEqual(cfg2.api_hosts, ("127.0.0.1",))
        self.assertTrue(cfg2.usage_enabled)

    def test_設定解析_錯誤值(self):
        with self.assertRaises(ValueError):
            load_config(["--api-port", "99999"], env={})
        with self.assertRaises(ValueError):
            load_config([], env={"LGWD_USAGE": "maybe"})

    # --- 對外行為 ---
    def test_whoami(self):
        token = self._token()
        st, raw = http_get(self.base + "/v1/whoami", token)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw)["client"], "w1")
        st, _ = http_get(self.base + "/v1/whoami", "bad")
        self.assertEqual(st, 401)
        st, _ = http_get(self.base + "/v1/whoami")
        self.assertEqual(st, 401)

    def test_board_json要憑證(self):
        token = self._token()
        st, raw = http_get(self.base + "/v1/board", token)
        self.assertEqual(st, 200)
        data = json.loads(raw)
        self.assertEqual(len(data["subscriptions"]), 5)
        # poller 停用：用量固定 unknown（LeaseService 層留 None，main 層填上）。
        self.assertTrue(all(s["usage"]["state"] == "unknown"
                            for s in data["subscriptions"]))
        st, _ = http_get(self.base + "/v1/board")
        self.assertEqual(st, 401)

    def test_板_html免憑證(self):
        st, raw = http_get(self.board_base + "/board")
        self.assertEqual(st, 200)
        text = raw.decode("utf-8")
        self.assertIn("LLMGateway 狀態板", text)
        for v in FAKE_KEYS.values():
            self.assertNotIn(v, text)

    def test_租用全流程經服務(self):
        token = self._token()
        body = json.dumps({"worker": "w1", "role": "r", "agent": "claude-code",
                           "instance": "i", "request_id": "x"}).encode()
        req = urllib.request.Request(self.base + "/v1/leases", data=body,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 201)
            data = json.loads(resp.read())
        self.assertEqual(data["lease"]["subscription"], "claude-personal")
        self.assertEqual(data["lease"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                         FAKE_KEYS["claude-personal"])

    # --- usage 合併 ---
    def test_usage合併(self):
        def fake_fetch(url, key, timeout):
            if "openrouter" in url:
                return 200, {"data": {"usage": 1.5, "limit_remaining": 8.5,
                                      "secret_field": "must-drop"}}
            raise OSError("down")

        poller = UsagePoller(self.gw.keys, self.clock, fetch=fake_fetch, enabled=True)
        poller.poll_once()
        data = _BoardWithUsage(self.gw.service, poller).board()
        by_name = {s["name"]: s for s in data["subscriptions"]}
        self.assertEqual(by_name["openrouter"]["usage"]["state"], "ok")
        self.assertEqual(by_name["openrouter"]["usage"]["summary"],
                         {"usage": 1.5, "limit_remaining": 8.5})
        self.assertEqual(by_name["ollama-cloud"]["usage"]["state"], "error")
        self.assertEqual(by_name["claude-personal"]["usage"]["state"], "unknown")
        blob = json.dumps(data)
        for v in FAKE_KEYS.values():
            self.assertNotIn(v, blob)

    # --- SIGHUP ---
    def test_SIGHUP重讀key檔(self):
        prev = signal.getsignal(signal.SIGHUP)
        fired = threading.Event()

        def on_hup(signum, frame):
            fired.set()
            self.gw.reload()

        signal.signal(signal.SIGHUP, on_hup)
        try:
            self._token()
            _, r = self._req("i-old")
            self.assertEqual(r["lease"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                             FAKE_KEYS["claude-personal"])
            rotated = dict(FAKE_KEYS, **{"claude-personal": "test-main-rotated-111"})
            (self.root / "keys.env").write_text(key_text(rotated), encoding="utf-8")
            os.kill(os.getpid(), signal.SIGHUP)
            self.assertTrue(fired.wait(5))
            fired.clear()
            st, r = self._req("i-new")
            self.assertEqual(st, 201)
            self.assertEqual(r["lease"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                             "test-main-rotated-111")
            # 空出 personal 名額（兩個 granted 已佔滿 cap=2），再測壞檔沿用。
            self.svc_release("i-old")
            self.svc_release("i-new")
            # 壞檔：沿用舊的，並記下錯誤（不含值）。
            (self.root / "keys.env").write_text("NOT A LINE\n", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGHUP)
            self.assertTrue(fired.wait(5))
            _, r = self._req("i-newer")
            self.assertEqual(r["lease"]["subscription"], "claude-personal")
            self.assertEqual(r["lease"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                             "test-main-rotated-111")
            self.assertIsNotNone(self.gw.keys.last_reload_error)
            for v in list(rotated.values()) + ["NOT A LINE"]:
                self.assertNotIn(v, self.gw.keys.last_reload_error or "")
        finally:
            signal.signal(signal.SIGHUP, prev)

    def _req(self, instance):
        return self.gw.service.request_lease(
            "w1", {"worker": "w1", "role": "r", "agent": "claude-code",
                   "instance": instance, "request_id": "rid-" + instance})

    def svc_release(self, instance):
        with self.gw.service._store.txn() as cur:
            row = cur.execute("SELECT lease_id FROM leases WHERE instance = ? "
                              "ORDER BY granted_at DESC LIMIT 1",
                              (instance,)).fetchone()
        self.gw.service.release("w1", row["lease_id"], {"request_id": "rel-" + instance})

    def test_SIGHUP壞policy沿用舊的(self):
        self._token()
        svc_before = self.gw.service
        (self.root / "policy.json").write_text("{bad json", encoding="utf-8")
        self.gw.reload()
        self.assertIs(self.gw.service, svc_before)
        _, r = self._req("i-p")
        self.assertEqual(r["lease"]["subscription"], "claude-personal")

    def test_SIGHUP好policy換上新的(self):
        self._token()
        data = json.loads((self.root / "policy.json").read_text(encoding="utf-8"))
        data["order"]["claude-code"] = ["claude-company", "claude-personal"]
        (self.root / "policy.json").write_text(json.dumps(data), encoding="utf-8")
        self.gw.reload()
        _, r = self._req("i-q")
        self.assertEqual(r["lease"]["subscription"], "claude-company")

    def test_關閉可重複呼叫(self):
        self.gw.stop()
        self.gw.stop()

    def test_python_m_lgwd_help(self):
        proc = subprocess.run([sys.executable, "-m", "lgwd", "--help"],
                              capture_output=True, timeout=30, cwd=str(REPO))
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"usage: lgwd", proc.stdout)


if __name__ == "__main__":
    unittest.main()
