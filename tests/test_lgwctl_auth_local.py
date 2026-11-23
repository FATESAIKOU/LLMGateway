"""`llmgwctl` 的本機管理 principal（#222 D4b；契約 C-710～C-714）。

- C-710：私鑰的路徑與權限檢查（root:root 0600、一般檔、不跟隨 symlink），不符就是
  「沒有 signer」——不簽、不改送無簽重試；讀不到時也不簽（C-714）。
- C-711：`check` 的 `auth-local`——非 root 一律 todo 且不打業務端點；root 時 enforce 的
  401／403／429／503 為 fail、warn 為 todo、off 為 ok 並註明。
- C-712：`usage` 在 enforce 下非 root 收到 401 時結束碼 1 並提示需 sudo。
- C-713：`install`／`upgrade`／`rollback`／`api-local` 的 `/healthz` 探針不簽。
- C-714：讀不到私鑰時不簽，也不做「簽了失敗再改送無簽」。

端到端用的是**真的** `lgwd.http.make_api_server` ＋ `lgwd.auth` 的 enforce gate、真的兩庫、
真的 `ssh-keygen` 與臨時金鑰；另有一個只回固定狀態碼的假服務，用來驗 403／503／warn／
off 的分級。不碰 `/etc`、`/var/lib`、`~/.ssh`，不讀真鑰，不連網。
"""

from __future__ import annotations

import builtins
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from lgwctl import check, local_auth, probe, usage
from lgwctl.layout import Layout
from lgwctl.report import Report
from lgwd import auth
from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths
from lgwd.clock import SystemClock
from lgwd.http import make_api_server
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer as KeyFactory
from tests.auth_signer import key_entry, registry_bytes
from tests.lgwctl_fakes import FAKE_TAG, NEW_TAG, Sandbox, has_line
from tests.test_http import FakeService

HOST = "127.0.0.1"
KEY_ID = local_auth.DEFAULT_KEY_ID
HEADER_NAMES = ("X-LGW-Auth-Version", "X-LGW-Key-Id", "X-LGW-Timestamp",
                "X-LGW-Nonce", "X-LGW-Signature")


def header(request: dict, name: str):
    for key, value in request["headers"].items():
        if key.lower() == name.lower():
            return value
    return None


def permission_denied_only(target: str):
    """mock `open`：只有 `target` 這個路徑拋 PermissionError（模擬 0640 root:llmgw）。

    測試機是非 root、不能把檔案 chown 成別人擁有，所以用 mock；其他路徑照常開。
    """
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise PermissionError(13, "permission denied")
        return real_open(path, *args, **kwargs)

    return mock.patch("builtins.open", side_effect=fake_open)


class CountingGate:
    """把 `authenticate` 計數後交給真的 gate（證明請求有沒有送到業務端）。"""

    def __init__(self, real):
        self.real = real
        self.mode = real.mode
        self.calls = 0

    def authenticate(self, **kwargs):
        self.calls += 1
        return self.real.authenticate(**kwargs)


class ScriptedApi:
    """只回固定狀態碼的假服務；記下每個請求的 request-target 與標頭。"""

    def __init__(self, status: int = 200, payload: bytes = b'{"subscriptions": []}'):
        self.status = status
        self.payload = payload
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append({
                    "target": self.path,
                    "headers": dict(self.headers.items()),
                })
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.payload)))
                self.end_headers()
                self.wfile.write(outer.payload)

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.url = "http://{0}:{1}".format(host, port)
        self.port = port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BuildSignerTest(unittest.TestCase):
    """C-710／C-714：路徑與權限檢查（不合格就是沒有 signer）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = cls._tmp.name
        cls.key = KeyFactory(cls.dir, "local-admin-signing-key")
        os.chmod(cls.key.private_path, 0o600)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def layout(self, *, key_path=None):
        layout = Layout.from_env({"LLMGW_PREFIX": os.path.join(self.dir, "machine")})
        # 測試機不能 chown 到 root：用 build_signer 的測試接縫（正式流程不設它）。
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        layout.euid = 0
        return layout

    def classify(self, path, **kwargs):
        signer, code = local_auth.build_signer(
            self.layout(), key_path=path, expected_uid=os.getuid(),
            expected_gid=os.getegid(), **kwargs)
        self.assertIsNone(signer)
        return code

    def test_C710_合格私鑰建出D4a的Signer(self):
        signer, code = local_auth.build_signer(
            self.layout(), key_path=self.key.private_path,
            expected_uid=os.getuid(), expected_gid=os.getegid())
        self.assertIsNone(code)
        self.assertIsNotNone(signer)
        headers = signer.headers(method="GET", target="/v1/subscriptions")
        self.assertEqual(sorted(headers), sorted(HEADER_NAMES))
        self.assertEqual(len(headers["X-LGW-Signature"]), 240)
        # 不洩密：repr 只有路徑，沒有簽章或 nonce。
        self.assertIn(self.key.private_path, repr(signer))
        self.assertNotIn(headers["X-LGW-Signature"], repr(signer))
        self.assertNotIn(headers["X-LGW-Nonce"], repr(signer))

    def test_C710_預設路徑寫死(self):
        self.assertEqual(local_auth.DEFAULT_KEY_PATH,
                         "/etc/llmgateway/local-admin-signing-key")
        layout = Layout.from_env({"LLMGW_PREFIX": "/tmp/x"})
        self.assertEqual(layout.local_admin_key_path,
                         "/tmp/x/etc/llmgateway/local-admin-signing-key")
        self.assertEqual(local_auth.DEFAULT_KEY_ID, "llmgwctl-local-admin")

    def test_C710_私鑰不在(self):
        self.assertEqual(self.classify(os.path.join(self.dir, "nope")), "missing")

    def test_C714_私鑰是symlink時不跟隨(self):
        link = os.path.join(self.dir, "link-key")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(self.key.private_path, link)
        self.assertEqual(self.classify(link), "symlink")

    def test_C710_私鑰不是一般檔(self):
        self.assertEqual(self.classify(self.dir), "not_regular")

    def test_C710_私鑰權限不是0600(self):
        path = os.path.join(self.dir, "mode-key")
        with open(path, "w", encoding="ascii") as fh:
            fh.write("not a key")
        for mode in (0o644, 0o640, 0o400):
            with self.subTest(mode=oct(mode)):
                os.chmod(path, mode)
                self.assertEqual(self.classify(path), "mode")

    def test_C710_擁有者不是rootroot(self):
        self.assertEqual(
            local_auth.build_signer(
                self.layout(), key_path=self.key.private_path,
                expected_uid=os.getuid() + 1, expected_gid=os.getegid())[1],
            "owner")

    def test_C710_非root不建signer(self):
        layout = self.layout()
        layout.euid = 1000
        signer, code = local_auth.build_signer(layout, key_path=self.key.private_path)
        self.assertIsNone(signer)
        self.assertEqual(code, "not_root")

    def test_C714_讀不到私鑰時不簽(self):
        with mock.patch("lgwctl.local_auth.os.open", side_effect=PermissionError):
            self.assertEqual(self.classify(self.key.private_path), "unreadable")

    def test_C710_lstat之後開檔之前被換成0644時不建signer(self):
        """L-2：lstat 與開檔之間的換檔由同一個 fd 的 fstat 重驗擋下。"""
        victim = os.path.join(self.dir, "swap-key")
        shutil.copy2(self.key.private_path, victim)
        os.chmod(victim, 0o600)
        real_lstat = os.lstat
        swapped = []

        def swapping_lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if os.fspath(path) == victim and not swapped:
                # lstat 說 0600，回來後（開檔之前）把它換成 0644 的內容。
                with open(self.key.private_path, "rb") as src, open(victim, "wb") as dst:
                    dst.write(src.read())
                os.chmod(victim, 0o644)
                swapped.append(True)
            return info

        with mock.patch("lgwctl.local_auth.os.lstat", side_effect=swapping_lstat):
            signer, code = local_auth.build_signer(
                self.layout(), key_path=victim, expected_uid=os.getuid(),
                expected_gid=os.getegid())
        self.assertTrue(swapped, "測試沒有換到檔")
        self.assertIsNone(signer)
        self.assertEqual(code, "mode")

    def test_C710_固定說明只含路徑(self):
        for code in ("missing", "symlink", "not_regular", "owner", "mode",
                     "unreadable", "not_root"):
            with self.subTest(code=code):
                text = local_auth.detail(code, "/x/key")
                self.assertIn("/x/key", text)
                self.assertIn(code, local_auth.DETAILS)
                self.assertNotIn("/x/key", local_auth.short_detail(code))


class LayoutAuthModeTest(unittest.TestCase):
    """L-4／agy 低-2：`Layout.auth_mode()` 的讀取失敗與空字串。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.env_file = os.path.join(self.root, "etc", "llmgateway", "llmgateway.env")
        os.makedirs(os.path.dirname(self.env_file))

    def layout(self, text=None):
        if text is not None:
            with open(self.env_file, "w", encoding="utf-8") as fh:
                fh.write(text)
        return Layout.from_env({"LLMGW_PREFIX": self.root})

    def test_C711_模式檔讀不到回unreadable(self):
        layout = self.layout('LGWD_AUTH_MODE="enforce"\n')
        with permission_denied_only(self.env_file):
            self.assertEqual(layout.auth_mode(), "unreadable")
        self.assertEqual(layout.auth_mode(), "enforce")   # 沒 mock 時讀得到

    def test_C711_模式檔不存在是off(self):
        self.assertEqual(self.layout().auth_mode(), "off")

    def test_C711_空字串模式是off與lgwd一致(self):
        """agy 低-2：lgwd 的 `env_of` 把空字串當成沒設（預設 off）。"""
        self.assertEqual(self.layout('LGWD_AUTH_MODE=""\n').auth_mode(), "off")

    def test_C711_非法模式是unknown(self):
        self.assertEqual(self.layout('LGWD_AUTH_MODE="bogus"\n').auth_mode(), "unknown")


class EnforceCase(unittest.TestCase):
    """共用：真的 lgwd enforce 服務（真的兩庫、真的驗章）、暫存的管理私鑰。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = cls._tmp.name
        cls.key = KeyFactory(cls.dir, "local-admin-signing-key")
        os.chmod(cls.key.private_path, 0o600)
        cls.state_dir = os.path.join(cls.dir, "state")
        os.makedirs(cls.state_dir)
        cls.registry = os.path.join(cls.dir, "auth-registry.json")
        cls.paths = auth_paths(cls.state_dir, registry=cls.registry)
        create_stores(cls.paths.trust_db, cls.paths.replay_db)
        write_registry_file(
            cls.registry, registry_bytes(1, [key_entry(KEY_ID, cls.key)]))
        cls.runtime = auth.open_runtime(
            auth.AuthConfig(mode="enforce", registry_path=cls.registry,
                            state_dir=cls.state_dir, per_key_max=600,
                            poll_interval=0.0),
            SystemClock(), owner_uid=0, registry_owner_uid=os.getuid())
        cls.gate = CountingGate(cls.runtime.gate)
        cls.service = FakeService()
        cls.server = make_api_server(cls.service, HOST, 0, gate=cls.gate)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.runtime.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.gate_before = self.gate.calls
        self.service_before = len(self.service.calls)

    def new_machine(self, mode="enforce", *, env_file=True):
        """一個新的假機器目錄（只放 llmgateway.env）；回 (layout, box)。

        用 `Sandbox` 的假 curl／systemctl，讓 `check` 的其他項目不碰真機；伺服器
        埠指向這一個 class 的 enforce 服務。
        """
        box = Sandbox()
        self.addCleanup(box.close)
        if env_file:
            box.write(box.etc("llmgateway.env"),
                      'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="{1}"\n'.format(
                          self.port, mode))
        env = dict(box.env)
        layout = Layout.from_env(env)
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        return layout, box

    def with_key(self, mode="enforce", *, euid=0, key_path=None, key_id=None):
        layout, box = self.new_machine(mode)
        env = dict(box.env)
        env["LLMGW_LOCAL_ADMIN_KEY"] = key_path or self.key.private_path
        if key_id is not None:
            env["LLMGW_LOCAL_ADMIN_KEY_ID"] = key_id
        layout = Layout.from_env(env)
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        layout.euid = euid
        return layout, box

    # -- check 的輔助（其他項目換成 no-op，只看 auth-local 與結束碼） ------

    def run_check(self, layout):
        report = Report(out=lambda line: None)
        with mock.patch.object(check, "_check_units", lambda *a, **k: (False, "missing")), \
                mock.patch.object(check, "_check_keysync", lambda *a, **k: None), \
                mock.patch.object(check, "_check_policy", lambda *a, **k: None), \
                mock.patch.object(check, "_check_gh_repo", lambda *a, **k: None), \
                mock.patch.object(check, "_check_auth_stores", lambda *a, **k: None):
            rc = check.run(layout, report=report, no_remote=True)
        return rc, report

    def auth_local_line(self, report) -> str:
        lines = [line for line in report.lines if line.split("（", 1)[0].endswith("auth-local")]
        self.assertEqual(len(lines), 1, report.lines)
        return lines[0]


class CheckAuthLocalTest(EnforceCase):
    """C-711：`check` 的 `auth-local`（結束碼語意沿用 0／1／2）。"""

    def test_C711_非root一律todo且不打業務端點(self):
        layout, box = self.with_key(euid=1000)
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("todo auth-local"), line)
        self.assertIn("需 root", line)
        self.assertEqual(rc, 2, report.lines)
        self.assertEqual(self.gate.calls, self.gate_before)
        self.assertEqual(self.service.calls[self.service_before:], [])

    def test_C711_root_enforce_正確principal為ok(self):
        layout, box = self.with_key()
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("ok auth-local"), line)
        self.assertIn("已簽章 list 200", line)
        self.assertEqual(rc, 0, report.lines)
        self.assertEqual(self.gate.calls, self.gate_before + 1)
        self.assertEqual(len(self.service.calls), self.service_before + 1)
        # 輸出不含簽章、nonce 或私鑰內容（只有路徑可以出現在 root 的訊息裡）。
        text = "\n".join(report.lines)
        self.assertNotIn("X-LGW", text)
        self.assertNotIn(self.key.public_key_line, text)

    def test_C711_root_enforce_未登記principal為fail(self):
        layout, box = self.with_key(key_id="not-registered-key")
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("fail auth-local"), line)
        self.assertIn("401 unauthorized", line)
        self.assertEqual(rc, 1, report.lines)
        self.assertEqual(self.gate.calls, self.gate_before + 1)

    def test_C711_root_enforce_403與503與429為fail(self):
        for status, payload in ((403, b'{"error": "forbidden"}'),
                                (429, b'{"error": "rate_limited"}'),
                                (503, b'{"error": "auth_unavailable"}')):
            with self.subTest(status=status):
                api = ScriptedApi(status, payload)
                self.addCleanup(api.close)
                layout, box = self.new_machine("enforce")
                layout.euid = 0
                env = dict(box.env)
                env["LLMGW_LOCAL_ADMIN_KEY"] = self.key.private_path
                box.write(box.etc("llmgateway.env"),
                          'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="enforce"\n'.format(api.port))
                layout = Layout.from_env(env)
                layout.local_admin_key_owner = (os.getuid(), os.getegid())
                layout.euid = 0
                rc, report = self.run_check(layout)
                line = self.auth_local_line(report)
                self.assertTrue(line.startswith("fail auth-local"), line)
                self.assertIn(str(status), line)
                self.assertEqual(rc, 1, report.lines)
                # 請求有簽（五個 X-LGW 標頭都在）。
                self.assertEqual(len(api.requests), 1, api.requests)
                for name in HEADER_NAMES:
                    self.assertIsNotNone(header(api.requests[0], name), name)

    def test_C711_root_warn一律todo(self):
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("warn")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="warn"\n'.format(api.port))
        env = dict(box.env)
        env["LLMGW_LOCAL_ADMIN_KEY"] = self.key.private_path
        layout = Layout.from_env(env)
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        layout.euid = 0
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("todo auth-local"), line)
        self.assertIn("warn", line)
        self.assertEqual(rc, 2, report.lines)
        self.assertEqual(len(api.requests), 1, api.requests)

    def test_C711_off為ok並註明未啟用認證(self):
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("off")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="off"\n'.format(api.port))
        layout.euid = 0   # off 就算 root 也是 ok，且不簽（不建 signer）
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("ok auth-local"), line)
        self.assertIn("未啟用認證", line)
        self.assertEqual(rc, 0, report.lines)
        self.assertEqual(api.requests, [])

    def test_C711_模式讀不懂為todo(self):
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("enforce")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="bogus"\n'.format(api.port))
        layout.euid = 0
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("todo auth-local"), line)
        self.assertEqual(rc, 2, report.lines)
        self.assertEqual(api.requests, [])

    def test_C711_模式讀不到為todo而非ok(self):
        """L-4／中-1：0640 的 llmgateway.env 非 root 讀不到，不能說成 off 的 ok。"""
        for euid in (0, 1000):
            with self.subTest(euid=euid):
                layout, box = self.with_key(euid=euid)
                before = self.gate.calls
                with permission_denied_only(layout.env_file):
                    rc, report = self.run_check(layout)
                line = self.auth_local_line(report)
                self.assertTrue(line.startswith("todo auth-local"), line)
                self.assertIn("認證模式讀不到", line)
                self.assertNotIn("未啟用認證", line)
                self.assertEqual(rc, 2, report.lines)
                self.assertEqual(self.gate.calls, before)
                self.assertEqual(self.service.calls[self.service_before:], [])

    def test_C711_空字串模式在非root為ok未啟用認證(self):
        """agy 低-2：`LGWD_AUTH_MODE=""` 與 lgwd 一樣視為 off（不簽、不打端點）。"""
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("off")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE=""\n'.format(api.port))
        layout.euid = 1000
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("ok auth-local"), line)
        self.assertIn("未啟用認證", line)
        self.assertEqual(rc, 0, report.lines)
        self.assertEqual(api.requests, [])

    def test_C714_check簽章失敗為fail且不送請求(self):
        """agy 低-1：私鑰合格但 ssh-keygen 失敗 → 固定分類、不送出、不重送。"""
        layout, box = self.with_key()
        with mock.patch("lgw.signer.subprocess.run", side_effect=FileNotFoundError):
            rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("fail auth-local"), line)
        self.assertIn("簽章失敗（未送出請求）：sshkeygen_missing", line)
        self.assertEqual(rc, 1, report.lines)
        self.assertEqual(self.gate.calls, self.gate_before)
        self.assertEqual(self.service.calls[self.service_before:], [])
        text = "\n".join(report.lines)
        self.assertNotIn("X-LGW", text)
        self.assertNotIn(self.key.public_key_line, text)

    def test_C710_root_enforce_私鑰權限不符為fail且不打端點(self):
        path = os.path.join(self.dir, "loose-key")
        with open(self.key.private_path, "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
        os.chmod(path, 0o644)
        layout, box = self.with_key(key_path=path)
        rc, report = self.run_check(layout)
        line = self.auth_local_line(report)
        self.assertTrue(line.startswith("fail auth-local"), line)
        self.assertIn("權限不是 0600", line)
        self.assertIn(path, line)
        self.assertEqual(rc, 1, report.lines)
        self.assertEqual(self.gate.calls, self.gate_before)
        self.assertEqual(self.service.calls[self.service_before:], [])

    def test_C710_root_enforce_私鑰缺失與symlink都不簽(self):
        missing = os.path.join(self.dir, "no-such-key")
        link = os.path.join(self.dir, "sneaky-key")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(self.key.private_path, link)
        for path, needle in ((missing, "不在"), (link, "symlink")):
            with self.subTest(path=path):
                layout, box = self.with_key(key_path=path)
                rc, report = self.run_check(layout)
                line = self.auth_local_line(report)
                self.assertTrue(line.startswith("fail auth-local"), line)
                self.assertIn(needle, line)
                self.assertEqual(rc, 1, report.lines)
        self.assertEqual(self.gate.calls, self.gate_before)

    def test_C713_api_local探針不簽(self):
        """`list-local` 走 curl（不帶 X-LGW），只有 `auth-local` 會建 signer。"""
        layout, box = self.with_key()
        with mock.patch.object(local_auth, "build_signer",
                               wraps=local_auth.build_signer) as builder:
            rc, report = self.run_check(layout)
        self.assertEqual(rc, 0, report.lines)
        self.assertEqual(builder.call_count, 1)     # 只有 auth-local 建簽章
        curl_log = box.log("curl.log")
        self.assertIn("/healthz", curl_log)
        self.assertNotIn("X-LGW", curl_log)         # 探針的 curl 沒有簽章標頭


class UsageAuthTest(EnforceCase):
    """C-712（＋C-710）：`usage` 的已簽章呼叫與非 root 的 401。"""

    def run_usage(self, layout):
        out, err = [], []
        rc = usage.run(layout, out=out.append, err=err.append)
        return rc, out, err

    def test_C712_非root在enforce收到401結束碼1並提示sudo(self):
        layout, box = self.with_key(euid=1000)
        rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(any("enforce 下需 sudo" in line for line in err), err)
        self.assertEqual(out, [])
        # 一個無簽請求、不重送；輸出不含金鑰路徑或原因細節。
        self.assertEqual(self.gate.calls, self.gate_before + 1)
        self.assertEqual(self.service.calls[self.service_before:], [])
        text = "\n".join(out + err)
        self.assertNotIn(self.key.private_path, text)
        self.assertNotIn("not_root", text)
        self.assertNotIn("unauthorized", text)

    def test_C712_root在enforce以正確principal可用(self):
        layout, box = self.with_key()
        rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 0, err)
        self.assertIn("claude-personal", "\n".join(out))
        self.assertEqual(self.gate.calls, self.gate_before + 1)
        self.assertEqual(len(self.service.calls), self.service_before + 1)

    def test_C710_root有私鑰時請求帶五個X_LGW標頭(self):
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("enforce")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="enforce"\n'.format(api.port))
        env = dict(box.env)
        env["LLMGW_LOCAL_ADMIN_KEY"] = self.key.private_path
        layout = Layout.from_env(env)
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        layout.euid = 0
        rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(api.requests), 1)
        for name in HEADER_NAMES:
            self.assertIsNotNone(header(api.requests[0], name), name)

    def test_C710_私鑰權限不符時是無簽的一次請求(self):
        path = os.path.join(self.dir, "loose-usage-key")
        with open(self.key.private_path, "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
        os.chmod(path, 0o604)
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("enforce")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="enforce"\n'.format(api.port))
        env = dict(box.env)
        env["LLMGW_LOCAL_ADMIN_KEY"] = path
        layout = Layout.from_env(env)
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        layout.euid = 0
        rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 0, err)                     # 假服務回 200
        self.assertEqual(len(api.requests), 1)           # 一次、不重送
        for name in HEADER_NAMES:                        # 而且沒有簽章
            self.assertIsNone(header(api.requests[0], name), name)
        text = "\n".join(err)
        self.assertIn("權限不是 0600", text)
        self.assertIn("送出無簽請求", text)

    def test_C712_off模式不簽也不碰私鑰(self):
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("off")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="off"\n'.format(api.port))
        layout.euid = 0
        with mock.patch.object(local_auth, "build_signer",
                               side_effect=AssertionError("off 不得建 signer")):
            rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(api.requests), 1)
        for name in HEADER_NAMES:
            self.assertIsNone(header(api.requests[0], name), name)

    def test_C712_非root在off與warn照舊可用(self):
        for mode in ("off", "warn"):
            with self.subTest(mode=mode):
                api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
                self.addCleanup(api.close)
                layout, box = self.new_machine(mode)
                box.write(box.etc("llmgateway.env"),
                          'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="{1}"\n'.format(
                              api.port, mode))
                layout.euid = 1000
                rc, out, err = self.run_usage(layout)
                self.assertEqual(rc, 0, err)
                self.assertEqual(err, [])      # 非 root 不印原因（design :262）

    def test_C712_模式讀不到時不簽(self):
        """L-4：llmgateway.env 讀不到時不當成 off 去簽，就是沒有 signer。"""
        api = ScriptedApi(200, b'{"schema": 1, "subscriptions": []}')
        self.addCleanup(api.close)
        layout, box = self.new_machine("enforce")
        box.write(box.etc("llmgateway.env"),
                  'LGW_API_PORT="{0}"\nLGWD_AUTH_MODE="enforce"\n'.format(api.port))
        env = dict(box.env)
        env["LLMGW_LOCAL_ADMIN_KEY"] = self.key.private_path
        layout = Layout.from_env(env)
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        layout.euid = 0
        with permission_denied_only(layout.env_file), \
                mock.patch.object(Layout, "api_port", lambda self: api.port):
            rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(api.requests), 1)
        for name in HEADER_NAMES:
            self.assertIsNone(header(api.requests[0], name), name)
        self.assertEqual(err, [])              # 沒有「原因」可印（不是私鑰不合格）

    def test_C712_root無簽章得401時說明未簽章(self):
        """L-5：root 沒有 signer 就送無簽，401 的原因要寫「未簽章」。"""
        layout, box = self.with_key(key_path=os.path.join(self.dir, "gone-key"))
        rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 1, out + err)
        text = "\n".join(err)
        self.assertIn("管理私鑰不在", text)
        self.assertIn("送出無簽請求", text)
        self.assertIn("未簽章（管理私鑰不可用，原因見上一行）", text)
        self.assertNotIn("未登記", text)
        self.assertNotIn("已撤銷", text)
        self.assertEqual(out, [])
        self.assertEqual(self.gate.calls, self.gate_before + 1)

    def test_C714_usage簽章失敗時結束碼1且不送請求(self):
        """agy 低-1：私鑰合格但簽章失敗 → 只印固定分類、不送、也不改送無簽。"""
        layout, box = self.with_key()
        with mock.patch("lgw.signer.subprocess.run", side_effect=FileNotFoundError):
            rc, out, err = self.run_usage(layout)
        self.assertEqual(rc, 1, out + err)
        text = "\n".join(err)
        self.assertIn("簽章失敗（sshkeygen_missing）；未送出請求", text)
        self.assertNotIn("X-LGW", text)
        self.assertNotIn(self.key.public_key_line, text)
        self.assertEqual(out, [])
        self.assertEqual(self.gate.calls, self.gate_before)
        self.assertEqual(self.service.calls[self.service_before:], [])


class ProbeNeverSignsTest(unittest.TestCase):
    """C-713：install／upgrade／rollback 的 `/healthz` 探針不簽。"""

    def test_C713_探針不需要signer(self):
        box = Sandbox()
        self.addCleanup(box.close)
        box.install_state(FAKE_TAG)
        box.write(box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        layout = Layout.from_env(box.env)
        layout.euid = 0
        with mock.patch.object(local_auth, "build_signer",
                               side_effect=AssertionError("探針不得建 signer")) as builder:
            self.assertEqual(probe.local_code(layout, mode="new"), ("ok", "healthz 200"))
            self.assertTrue(probe.local_ok(layout, mode="new"))
            self.assertTrue(probe.wait_local_ok(layout, 0.5, mode="new"))
        self.assertEqual(builder.call_count, 0)
        self.assertIn("/healthz", box.log("curl.log"))
        self.assertNotIn("X-LGW", box.log("curl.log"))

    def test_C713_install_upgrade_rollback不簽(self):
        box = Sandbox()
        self.addCleanup(box.close)
        with mock.patch.object(local_auth, "build_signer",
                               side_effect=AssertionError("探針不得建 signer")) as builder:
            rc, out, err = box.run_cli(["install", "--tag", FAKE_TAG,
                                        "--repo", "/fake/repo"])
            self.assertEqual(rc, 0, out + err)
            rc, out, err = box.run_cli(["upgrade", "--tag", NEW_TAG,
                                        "--repo", "/fake/repo"])
            self.assertEqual(rc, 0, out + err)
            rc, out, err = box.run_cli(["rollback"])
            self.assertNotEqual(rc, 1, out + err)
            self.assertTrue(has_line(out, "auth-local", "ok"), out)
        self.assertEqual(builder.call_count, 0)
        rc, out, err = box.run_cli(["check", "--no-remote"])
        self.assertNotEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "list-local", "ok"), out)
        self.assertTrue(has_line(out, "auth-local", "ok"), out)


if __name__ == "__main__":
    unittest.main()
