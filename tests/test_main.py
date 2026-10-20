"""lgwd/main.py 的測試：假 key 檔、真 policy/default.json、隨機埠。

涵蓋 spec subscription-registry 與 key-vault 的服務端 Scenario 在主程式裡的行為
（不帶應用層憑證、換 key、只綁 loopback、SIGTERM）。v5：key 檔由 mtime 偵測重讀，
沒有 SIGHUP／ExecReload；policy 只在啟動時讀。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lgwd.clock import FakeClock
from lgwd.keys import KEY_NAMES, KeyFileError
from lgwd.main import Config, Gateway, load_config
from lgwd.policy import PolicyError

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


def rotate(path, text):
    """像 key-sync 一樣原子換檔：寫暫存（0600）→ rename（inode 一定變）。"""
    tmp = f"{path}.tmp-{os.getpid()}-{time.time_ns()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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

    def test_ClaudeCode拿不到openrouter(self):
        # 出廠政策（#23）：openrouter 只給 opencode
        status, raw = http_get(
            self.base + "/v1/subscriptions/openrouter?agent=claude-code")
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
                              {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(status, 204)
        item = self.list_items()["claude-personal"]
        self.assertFalse(item["available"])
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.clock.advance(hours=16)
        self.assertTrue(self.list_items()["claude-personal"]["available"])

    def test_冷卻錯誤對應(self):
        status, _ = http_post(self.base + "/v1/subscriptions/ghost/cooldown",
                              {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(status, 404)
        status, _ = http_post(self.base + "/v1/subscriptions/claude-personal/cooldown",
                              {"until": "2026-10-10T00:00:00Z"})
        self.assertEqual(status, 400)

    def test_其他路徑404(self):
        for path in ("/v1/board", "/v1/leases", "/v1/whoami", "/board", "/nope"):
            with self.subTest(path=path):
                status, _ = http_get(self.base + path)
                self.assertEqual(status, 404)

    def test_關閉可重複呼叫(self):
        self.gw.stop()
        self.gw.stop()

    def test_SIGHUP整組已拿掉(self):
        # v5.4：沒有 SIGHUP 重讀了。程式裡不該再有 handler 或「SIGHUP 會做 X」的說明，
        # unit 也不該再有 ExecReload（審查 #13 低-1：policy.py 的註解曾殘留）。
        service = (REPO / "deploy" / "llmgateway.service").read_text(encoding="utf-8")
        self.assertNotIn("ExecReload", service)
        forbidden = ("signal.SIGHUP", "hasattr(signal,", "on_hup", "SIGHUP 時",
                     "SIGHUP 重讀", "SIGHUP：")
        narrative = ("沒有", "不再", "拿掉", "移除", "v5", "無熱重載")
        for module in ("main.py", "policy.py", "keys.py"):
            src = (REPO / "lgwd" / module).read_text(encoding="utf-8")
            for line in src.splitlines():
                if "SIGHUP" not in line:
                    continue
                for bad in forbidden:
                    self.assertNotIn(bad, line, f"{module} 還在講 SIGHUP 的舊行為：{line.strip()}")
                # 剩下的是敘述性提及（例如「不再用 SIGHUP」），必須寫明是拿掉了
                self.assertTrue(
                    any(word in line for word in narrative),
                    f"{module} 的 SIGHUP 提及沒說明已拿掉：{line.strip()}")


class ReloadTest(MainCase):
    def _get_key(self, sub="claude-personal", agent="claude-code"):
        status, raw = http_get(
            self.base + f"/v1/subscriptions/{sub}?agent={agent}")
        if status != 200:
            return status, None
        return status, json.loads(raw)["key"]

    def test_換key_mtime變了請求前重讀(self):
        # spec key-vault「換 key」：本人更新加密檔 → key-sync 換檔 → lgwd 自己重讀
        self.assertEqual(self._get_key()[1], FAKE_KEYS["claude-personal"])
        rotated = dict(FAKE_KEYS, **{"claude-personal": "test-main-rotated-111"})
        rotate(self.root / "keys.env", key_text(rotated))  # 不呼叫 reload()

        status, key = self._get_key()
        self.assertEqual(status, 200)
        self.assertEqual(key, "test-main-rotated-111")
        blob = http_get(self.base + "/v1/subscriptions")[1].decode()
        self.assertNotIn("test-main-rotated-111", blob)
        # 沒換回舊的：再問一次還是新的
        self.assertEqual(self._get_key()[1], "test-main-rotated-111")

    def test_沒變就不重讀(self):
        calls = []
        real = self.gw.keys.reload
        self.gw.keys.reload = lambda: (calls.append(1), real())[1]
        try:
            self.assertEqual(http_get(self.base + "/v1/subscriptions")[0], 200)
            self.assertEqual(self._get_key()[0], 200)
        finally:
            self.gw.keys.reload = real
        self.assertEqual(calls, [])  # 檔案沒動就不做白工

    def test_壞key檔沿用舊的(self):
        rotate(self.root / "keys.env", "NOT A LINE\n")
        status, key = self._get_key()
        self.assertEqual(status, 200)
        self.assertEqual(key, FAKE_KEYS["claude-personal"])
        self.assertIsNotNone(self.gw.keys.last_reload_error)
        for value in (*FAKE_KEYS.values(), "NOT A LINE"):
            self.assertNotIn(value, self.gw.keys.last_reload_error or "")

    def test_壞key檔之後再換好的會重讀(self):
        rotate(self.root / "keys.env", "NOT A LINE\n")
        self.assertEqual(self._get_key()[1], FAKE_KEYS["claude-personal"])
        rotated = dict(FAKE_KEYS, **{"claude-personal": "test-main-second-222"})
        rotate(self.root / "keys.env", key_text(rotated))
        self.assertEqual(self._get_key()[1], "test-main-second-222")
        self.assertIsNone(self.gw.keys.last_reload_error)

    def test_壞key檔會一直重試到修好(self):
        """審查 #13 低-5：失敗時不推進戳，下一個請求就再試（不是等檔案再變一次）。"""
        rotate(self.root / "keys.env", "NOT A LINE\n")
        calls = []
        real = self.gw.keys.reload
        self.gw.keys.reload = lambda: (calls.append(1), real())[1]
        try:
            for _ in range(3):
                self.assertEqual(self._get_key()[1], FAKE_KEYS["claude-personal"])
            self.assertEqual(len(calls), 3)  # 三個請求、三次重試
            # 壞檔期間一直是舊 key，服務不下線
            self.assertEqual(http_get(self.base + "/v1/subscriptions")[0], 200)
            # 不會無限重試到 hot loop：檔案沒再變、成功重讀過就不再試
            with self.assertRaises(KeyFileError):
                real()  # 直接重讀：還是壞的（證明戳沒被推進成「已處理」）
            # 修好（原地寫回同樣內容以外的新檔）→ 下個請求讀到新值，且不再重讀
            rotated = dict(FAKE_KEYS, **{"claude-personal": "test-main-fixed-444"})
            rotate(self.root / "keys.env", key_text(rotated))
            self.assertEqual(self._get_key()[1], "test-main-fixed-444")
            self.assertIsNone(self.gw.keys.last_reload_error)
            before = len(calls)
            self.assertEqual(self._get_key()[1], "test-main-fixed-444")
            self.assertEqual(len(calls), before)  # 戳推進了 → 不再重讀
        finally:
            self.gw.keys.reload = real

    def test_policy不再熱重載(self):
        # v5：拿掉 SIGHUP 之後 policy 只在啟動時讀，改政策要重啟服務
        data = json.loads((self.root / "policy.json").read_text(encoding="utf-8"))
        data["subscriptions"]["ollama-cloud"]["priority"] = 1
        (self.root / "policy.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(http_get(self.base + "/v1/subscriptions")[0], 200)
        first = self.list_items()["ollama-cloud"]
        self.assertNotEqual(first["priority"], 1)

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

    def test_子程序_換key與SIGTERM_log(self):
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
            # 子程序活著時換檔：下一次請求就該看到新值（mtime 偵測，沒有 SIGHUP）
            rotated = dict(FAKE_KEYS, **{"claude-personal": "test-main-child-333"})
            rotate(self.root / "keys.env", key_text(rotated))
            deadline = time.time() + 10
            seen = None
            while time.time() < deadline:
                status, raw = http_get(
                    f"http://127.0.0.1:{api_port}/v1/subscriptions/claude-personal"
                    "?agent=claude-code")
                if status == 200:
                    seen = json.loads(raw)["key"]
                    if seen == "test-main-child-333":
                        break
                time.sleep(0.1)
            self.assertEqual(seen, "test-main-child-333")
            proc.terminate()  # SIGTERM
            _, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0)
            self.assertIn(b"keys reload ok", stderr)
            self.assertIn(b"SIGTERM", stderr)
            for value in (*FAKE_KEYS.values(), "test-main-child-333"):
                self.assertNotIn(value.encode(), stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class ProviderEndpointGateTest(unittest.TestCase):
    """1.0：endpoint 檢查的呼叫點在組裝根（Gateway.__init__、load_policy 之後）。

    以 monkeypatch PROVIDERS 的假 provider 表為測樣；不合法（內嵌憑證）＝
    PolicyError（Gateway 在 validate_endpoints 的 ValueError 之後轉型），服務不啟動。
    """

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

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_providers(self, endpoint):
        from lgwd import providers as lgwd_providers

        class _Bad(lgwd_providers.CredentialProvider):
            provider = "fake"
            kind = "api-key"

        _Bad.endpoint = endpoint
        original = lgwd_providers.PROVIDERS
        lgwd_providers.PROVIDERS = {name: _Bad() for name in KEY_NAMES}
        self.addCleanup(setattr, lgwd_providers, "PROVIDERS", original)

    def test_假provider表endpoint內嵌憑證_組裝失敗(self):
        for endpoint in ("https://host/?key=canary-main-001",
                         "https://user:pass@host"):
            with self.subTest(endpoint=endpoint):
                self._patch_providers(endpoint)
                with self.assertRaises(PolicyError):
                    Gateway(self.config, clock=FakeClock())

    def test_出廠provider表組裝成功(self):
        # 沒有丟錯（所有既有測試都在這條路上，這裡只作正面的最小確認）
        from lgwd import providers as lgwd_providers
        lgwd_providers.validate_endpoints()


if __name__ == "__main__":
    unittest.main()
