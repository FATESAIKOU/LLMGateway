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
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lgwd.clock import FakeClock
from lgwd.keys import KEY_NAMES, KeyFileError
from lgwd.main import Config, Gateway, load_config
from lgwd.policy import PolicyError
from lgwd.providers import UsageDeps
from tests.usage_fakes import refuse_http

REPO = Path(__file__).resolve().parent.parent
FAKE_KEYS = {sub: f"test-main-{sub}-000" for sub in
             ("claude-personal", "claude-company", "openrouter",
              "opencode-zen", "ollama-cloud")}


def write_policy(root):
    """出廠政策＋關掉 Ollama 與 Claude 取用量：起真 lgwd 子行程的測試不對外連線（#60）。"""
    policy = json.loads((REPO / "policy" / "default.json").read_text(encoding="utf-8"))
    policy["usage"] = {"ollama-cloud": {"enabled": False},
                        "claude-personal": {"enabled": False}}
    (root / "policy.json").write_text(json.dumps(policy), encoding="utf-8")


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
        write_policy(self.root)
        self.config = Config(
            db=str(self.root / "llm.db"),
            policy_path=str(self.root / "policy.json"),
            keys_path=str(self.root / "keys.env"),
            api_host="127.0.0.1",
            api_port=0,
        )
        self.clock = FakeClock()
        self.gw = Gateway(self.config, clock=self.clock, usage_deps=UsageDeps(http_get=refuse_http))
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
        self.assertEqual(len(data["subscriptions"]), 4)
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
            self.base + "/v1/subscriptions/claude-personal?agent=opencode")
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
        write_policy(self.root)
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
                    Gateway(self.config, clock=FakeClock(), usage_deps=UsageDeps(http_get=refuse_http))

    def test_出廠provider表組裝成功(self):
        # 沒有丟錯（所有既有測試都在這條路上，這裡只作正面的最小確認）
        from lgwd import providers as lgwd_providers
        lgwd_providers.validate_endpoints()


class UsageGatewayTest(unittest.TestCase):
    """#60 tasks 7.1：Gateway 組裝取用量、stop 的語意、Config 的 cwd 設定。

    HTTP 層與 claude 都是假的（假 HTTP 回呼、tests/fakes/fake_claude.py 的包裝腳本）。
    """

    FAKE = str(REPO / "tests" / "fakes" / "fake_claude.py")
    BALANCE = json.dumps({"included": {
        "session": {"remaining_percent": 75, "resets_at": "2026-10-12T07:00:00Z"},
        "weekly": {"remaining_percent": 40, "resets_at": "2026-10-14T00:00:00Z"}}}).encode()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / "keys.env").write_text(key_text(FAKE_KEYS), encoding="utf-8")
        self.requests = []
        self.gateways = []

    def make_policy(self, usage=None):
        obj = json.loads((REPO / "policy" / "default.json").read_text(encoding="utf-8"))
        if usage is None:
            # 出廠政策已開啟 Claude（#60 階段 C）；不談 Claude 的測試一律明確關閉，
            # 免得 list 一列就 spawn 真的 `claude`（測試要自足）。
            usage = {"claude-personal": {"enabled": False}}
        obj["usage"] = usage
        path = self.root / "policy.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return str(path)

    def http(self, url, headers, timeout):
        self.requests.append(headers["Authorization"])
        return 200, {}, self.BALANCE

    def claude_command(self, mode):
        path = self.root / f"claude-{mode}"
        path.write_text(
            "#!/bin/sh\n"
            f"export FAKE_CLAUDE_MODE={mode} FAKE_CLAUDE_PIDFILE={self.root}/child.pid\n"
            f"exec {sys.executable} {self.FAKE} \"$@\"\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def gateway(self, usage=None, claude_mode=None, cwd=True):
        from lgwd.providers import UsageDeps
        trusted = self.root / "trusted"
        trusted.mkdir(exist_ok=True)
        config = Config(db=str(self.root / "llm.db"), policy_path=self.make_policy(usage),
                        keys_path=str(self.root / "keys.env"), api_host="127.0.0.1", api_port=0,
                        usage_claude_cwd=str(trusted) if cwd else None)
        deps = UsageDeps(http_get=self.http, claude_cwd=config.usage_claude_cwd,
                         claude_command=self.claude_command(claude_mode) if claude_mode else "claude",
                         claude_work_dir=str(self.root / "work"))
        gw = Gateway(config, clock=FakeClock(), usage_deps=deps)
        gw.start()
        self.gateways.append(gw)
        self.addCleanup(gw.stop)
        host, port = gw.addr[:2]
        return gw, f"http://{host}:{port}"

    def items(self, base):
        return {i["id"]: i for i in json.loads(http_get(base + "/v1/subscriptions")[1])["subscriptions"]}

    def test_組裝後list與resolve帶usage_下一次才看到新值(self):
        gw, base = self.gateway()
        self.assertEqual(self.items(base)["ollama-cloud"]["usage"]["reason"], "not_fetched_yet")
        self.assertTrue(gw._usage.wait_idle())
        got = self.items(base)["ollama-cloud"]
        self.assertEqual(got["usage"]["status"], "ok")
        self.assertEqual(got["usage"]["session"]["remaining_percent"], 75)
        meta = json.loads(http_get(base + "/v1/resolve?agent=opencode&provider=ollama-cloud")[1])["meta"]
        self.assertEqual(meta["usage"]["status"], "ok")
        self.assertEqual(self.requests, [f"Bearer {FAKE_KEYS['ollama-cloud']}"])

    def test_換key後取用量用新key(self):
        gw, base = self.gateway()
        self.items(base); gw._usage.wait_idle()
        rotate(str(self.root / "keys.env"), key_text({**FAKE_KEYS, "ollama-cloud": "test-main-rotated-222"}))
        gw.service._clock.advance(seconds=121)       # 過 TTL
        self.items(base); gw._usage.wait_idle()
        self.assertEqual(self.requests[-1], "Bearer test-main-rotated-222")

    def test_Claude預設關閉_不起子行程(self):
        # 預設關閉測試：用明確 disabled（出廠已開，不能再假設預設關閉）
        gw, base = self.gateway(usage={"claude-personal": {"enabled": False}}, claude_mode="ok")
        got = self.items(base)["claude-personal"]["usage"]
        self.assertEqual((got["status"], got["reason"]), ("unknown", "disabled"))
        self.assertTrue(gw._usage.wait_idle())
        self.assertEqual(gw._usage._guard.procs, set())

    def test_政策開啟Claude_用Config的固定cwd(self):
        dump = self.root / "dump.jsonl"
        path = self.claude_command("ok")
        text = Path(path).read_text().replace("\n", f"\nexport FAKE_CLAUDE_DUMP={dump}\n", 1)
        Path(path).write_text(text)
        gw, base = self.gateway(usage={"claude-personal": {"enabled": True}}, claude_mode="ok")
        Path(self.claude_command("ok")).write_text(text)    # gateway() 重寫了包裝腳本：補回 dump
        self.items(base); gw._usage.wait_idle(30)
        self.assertTrue(dump.exists())
        self.assertEqual(json.loads(dump.read_text().splitlines()[0])["cwd"], str(self.root / "trusted"))

    def test_沒設cwd_Claude是upstream_error(self):
        gw, base = self.gateway(usage={"claude-personal": {"enabled": True}}, claude_mode="ok", cwd=False)
        self.items(base); gw._usage.wait_idle(30)
        got = self.items(base)["claude-personal"]["usage"]
        self.assertEqual((got["status"], got["reason"]), ("unknown", "upstream_error"))

    def test_stop後再查不觸發(self):
        gw, base = self.gateway()
        gw._usage.stop()
        self.items(base)
        self.assertTrue(gw._usage.wait_idle())
        self.assertEqual(self.requests, [])

    def test_stop收掉在跑的子行程且在有限時間內返回(self):
        gw, base = self.gateway(usage={"claude-personal": {"enabled": True}}, claude_mode="hang")
        self.items(base)
        pidfile = self.root / "child.pid"
        for _ in range(200):
            if gw._usage._guard.procs and pidfile.exists():
                break
            time.sleep(0.05)
        self.assertTrue(gw._usage._guard.procs, "假 claude 沒有起來")
        procs = list(gw._usage._guard.procs)
        grandchild = int(pidfile.read_text())
        started = time.monotonic()
        gw.stop()
        self.assertLess(time.monotonic() - started, 15)
        for proc in procs:
            self.assertIsNotNone(proc.poll())
        for _ in range(100):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("孫行程還活著")
        self.assertEqual(gw._usage._guard.procs, set())

    def test_stop與spawn交錯_不留殘留(self):
        from lgwd.usage import ProcessGuard
        guard = ProcessGuard(threading.Lock())
        guard.stopped = True        # stop() 先拿到鎖、設了旗標
        self.assertIsNone(guard.spawn([sys.executable, "-c", "pass"], dict(os.environ)))
        self.assertEqual(guard.procs, set())

    def test_load_config的cwd設定(self):
        self.assertEqual(load_config([], env={"LGW_USAGE_CLAUDE_CWD": "/x"}).usage_claude_cwd, "/x")
        self.assertEqual(load_config(["--usage-claude-cwd", "/y"], env={"LGW_USAGE_CLAUDE_CWD": "/x"})
                         .usage_claude_cwd, "/y")
        self.assertIsNone(load_config([], env={}).usage_claude_cwd)

    def test_壞的usage設定_服務不啟動(self):
        with self.assertRaises(PolicyError):
            self.gateway(usage={"claude-personal": {"enable": True}})


if __name__ == "__main__":
    unittest.main()
