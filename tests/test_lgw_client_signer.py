"""`lgw/client.py` 的已簽章呼叫（#222 D4a；契約 C-701～C-709）。

- 假服務是 in-process HTTP（隨機埠），記下 method／request-target／標頭／原始 body；
  用它驗「簽的 target 與 body 就是送出的」、redirect 不跟隨、401 不降級、不重試。
- 端到端一組用**真的** `lgwd.http.make_api_server` ＋ `lgwd.auth` 的 enforce gate、
  真的兩庫、真的 `ssh-keygen`、真的臨時金鑰與臨時 ssh-agent，跑五個方法。
- 不碰 `/var/lib`、`/etc`、`~/.ssh`；不讀真鑰、不連網。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from lgw.client import Client
from lgw.signer import Signer, SignerError, build_signing_message
from lgwd import auth
from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths
from lgwd.clock import SystemClock
from lgwd.http import make_api_server
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer as KeyFactory
from tests.auth_signer import key_entry, registry_bytes
from tests.test_http import FakeService

HOST = "127.0.0.1"
KEY_ID = "lgw-client-test"


def header(request: dict, name: str):
    for key, value in request["headers"].items():
        if key.lower() == name.lower():
            return value
    return None


class RawServer:
    """可腳本化的假服務：`responder(method, target, body, index)` 回
    `(status, headers, payload_bytes)`；每個請求都記下來。"""

    def __init__(self, responder=None):
        self.requests: list[dict] = []
        self.responder = responder or (lambda method, target, body, index: (200, {}, b"{}"))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                target = self.requestline.split(" ")[1]
                outer.requests.append({
                    "method": method, "target": target, "body": body,
                    "headers": dict(self.headers.items()),
                })
                status, response_headers, payload = outer.responder(
                    method, target, body, len(outer.requests))
                self.send_response(status)
                for key, value in response_headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.url = "http://{0}:{1}".format(host, port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RawServerCase(unittest.TestCase):
    def serve(self, responder=None):
        server = RawServer(responder)
        self.addCleanup(server.close)
        return server

    def real_signer(self):
        return Signer(key_id=KEY_ID, private_key_path=self.key.private_path)

    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory()
        cls.key = KeyFactory(cls._keys.name, "k")

    @classmethod
    def tearDownClass(cls):
        cls._keys.cleanup()


class RedirectTest(RawServerCase):
    """C-705：3xx 原樣回，不跟隨（有無 signer 都一樣）。"""

    def test_C705_302不跟隨_無signer(self):
        other = self.serve()
        target = self.serve(lambda *args: (302, {"Location": other.url + "/elsewhere"}, b""))
        client = Client(target.url)
        result = client.list()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 302)
        self.assertEqual(result.error, "redirect")
        self.assertEqual(len(target.requests), 1)
        self.assertEqual(other.requests, [])

    def test_C705_302不跟隨_有signer(self):
        other = self.serve()
        target = self.serve(lambda *args: (302, {"Location": other.url + "/elsewhere"}, b""))
        client = Client(target.url, signer=self.real_signer())
        result = client.list()
        self.assertEqual(result.status, 302)
        self.assertEqual(len(target.requests), 1)
        self.assertEqual(other.requests, [])
        self.assertIsNotNone(header(target.requests[0], "X-LGW-Signature"))


class NoDowngradeTest(RawServerCase):
    """C-705：401 後不改送無簽；簽章失敗時一個請求都不送。"""

    def test_C705_401後只有一個已簽請求(self):
        server = self.serve(lambda *args: (401, {}, b'{"error": "unauthorized"}'))
        client = Client(server.url, signer=self.real_signer())
        result = client.list()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 401)
        self.assertEqual(len(server.requests), 1)
        self.assertIsNotNone(header(server.requests[0], "X-LGW-Signature"))

    def test_C705_簽章失敗不送出也不改送無簽(self):
        server = self.serve()
        signer = Signer(key_id=KEY_ID, private_key_path=self.key.private_path,
                        sshkeygen="/nonexistent/ssh-keygen")
        result = Client(server.url, signer=signer).list()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "signer_unavailable")
        self.assertEqual(result.data["reason"], "sshkeygen_missing")
        self.assertEqual(server.requests, [])

    def test_C703_armor壞掉不送出(self):
        server = self.serve()
        bad = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=("-----BEGIN SSH SIGNATURE-----\n"
                    + base64.b64encode(b"\x00" * 179).decode("ascii")
                    + "\n-----END SSH SIGNATURE-----\n").encode("ascii"),
            stderr=b"")
        with mock.patch("lgw.signer.subprocess.run", return_value=bad):
            result = Client(
                server.url, signer=self.real_signer()).list()
        self.assertEqual(result.error, "signer_unavailable")
        self.assertEqual(result.data["reason"], "bad_signature")
        self.assertEqual(server.requests, [])


class ErrorMappingTest(RawServerCase):
    """C-706：401／403／429／503 的 Result 與 data。"""

    def test_C706_401帶伺服器Date與時差(self):
        server = self.serve(lambda *args: (401, {}, b'{"error": "unauthorized"}'))
        result = Client(server.url).list()
        self.assertEqual(result.status, 401)
        self.assertEqual(result.error, "unauthorized")
        self.assertIsInstance(result.data["server_date"], str)
        self.assertIsInstance(result.data["clock_skew_s"], int)
        self.assertLess(abs(result.data["clock_skew_s"]), 120)

    def test_C706_401非JSON也照樣分類(self):
        server = self.serve(lambda *args: (401, {}, b"not json"))
        result = Client(server.url).list()
        self.assertEqual(result.error, "unauthorized")
        self.assertIsInstance(result.data["clock_skew_s"], int)

    def test_C706_403_forbidden(self):
        for payload in (b'{"error": "forbidden"}', b""):
            with self.subTest(payload=payload):
                server = self.serve(lambda *args, p=payload: (403, {}, p))
                result = Client(server.url).list()
                self.assertEqual(result.status, 403)
                self.assertEqual(result.error, "forbidden")

    def test_C706_403業務碼照舊保留(self):
        server = self.serve(
            lambda *args: (403, {}, b'{"error": "model_not_allowed"}'))
        result = Client(server.url).resolve("opencode", "mna")
        self.assertEqual(result.status, 403)
        self.assertEqual(result.error, "model_not_allowed")

    def test_C706_429帶retry_after(self):
        server = self.serve(
            lambda *args: (429, {"Retry-After": "17"}, b'{"error": "rate_limited"}'))
        result = Client(server.url).list()
        self.assertEqual(result.status, 429)
        self.assertEqual(result.error, "rate_limited")
        self.assertEqual(result.data["retry_after"], 17)

    def test_C706_503_auth_unavailable(self):
        server = self.serve(
            lambda *args: (503, {}, b'{"error": "auth_unavailable"}'))
        result = Client(server.url).list()
        self.assertEqual(result.status, 503)
        self.assertEqual(result.error, "auth_unavailable")


class NoRetryTest(RawServerCase):
    """C-707：不自動重試；呼叫方重試時整個重簽（新的 nonce）。"""

    def test_C707_503只送一次(self):
        server = self.serve(
            lambda *args: (503, {}, b'{"error": "auth_unavailable"}'))
        Client(server.url, signer=self.real_signer()).list()
        self.assertEqual(len(server.requests), 1)

    def test_C707_429只送一次(self):
        server = self.serve(
            lambda *args: (429, {"Retry-After": "5"}, b'{"error": "rate_limited"}'))
        Client(server.url, signer=self.real_signer()).list()
        self.assertEqual(len(server.requests), 1)

    def test_C707_兩次呼叫各自新的nonce(self):
        server = self.serve()
        client = Client(server.url, signer=self.real_signer())
        client.list()
        client.list()
        first = header(server.requests[0], "X-LGW-Nonce")
        second = header(server.requests[1], "X-LGW-Nonce")
        self.assertEqual(len(first), 43)
        self.assertNotEqual(first, second)


class SigningShapeTest(RawServerCase):
    """C-702：簽的 request-target 就是送出的、body 只序列化一次且原樣送出。"""

    def test_C702_target包含_plus與斜線與非ASCII時簽的就是送出的(self):
        seen = {}

        class StubSigner:
            def headers(self, *, method, target, body=b"", now=None):
                seen.update({"method": method, "target": target, "body": body})
                return {"X-LGW-Auth-Version": "1", "X-LGW-Key-Id": "stub",
                        "X-LGW-Timestamp": "1", "X-LGW-Nonce": "n" * 43,
                        "X-LGW-Signature": "s" * 240}

        server = self.serve()
        result = Client(server.url, signer=StubSigner()).resolve(
            "opencode", "opencode-zen", model="a+b/c 中文")
        self.assertTrue(result.ok)
        self.assertEqual(seen["target"], server.requests[0]["target"])
        self.assertIn("%2B", seen["target"])
        self.assertIn("%2F", seen["target"])
        for character in seen["target"]:
            self.assertLess(ord(character), 128)
        self.assertEqual(seen["body"], b"")

    def test_C702_POST的body只序列化一次且與送出相同(self):
        server = self.serve(lambda *args: (204, {}, b""))
        real_dumps = json.dumps
        with mock.patch("lgw.client.json.dumps", side_effect=real_dumps) as dumps:
            result = Client(server.url).cooldown(
                "claude-personal", "2026-11-23T18:00:00Z", model="中文模型")
        self.assertTrue(result.ok)
        self.assertEqual(dumps.call_count, 1)
        sent = json.loads(server.requests[0]["body"].decode("utf-8"))
        self.assertEqual(sent, {"until": "2026-11-23T18:00:00Z", "model": "中文模型"})
        self.assertEqual(server.requests[0]["body"],
                         json.dumps(sent).encode("utf-8"))

    def test_C702_有signer時POST的body只序列化一次且簽的就是送出的(self):
        seen = {}

        class StubSigner:
            def headers(self, *, method, target, body=b"", now=None):
                seen.update({"method": method, "target": target, "body": body})
                return {"X-LGW-Auth-Version": "1", "X-LGW-Key-Id": "stub",
                        "X-LGW-Timestamp": "1", "X-LGW-Nonce": "n" * 43,
                        "X-LGW-Signature": "s" * 240}

        server = self.serve(lambda *args: (204, {}, b""))
        real_dumps = json.dumps
        with mock.patch("lgw.client.json.dumps", side_effect=real_dumps) as dumps:
            result = Client(server.url, signer=StubSigner()).cooldown(
                "claude-personal", "2026-11-23T18:00:00Z", model="中文模型")
        self.assertTrue(result.ok, result)
        self.assertEqual(dumps.call_count, 1)
        request = server.requests[0]
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["target"], request["target"])
        self.assertEqual(seen["target"],
                         "/v1/subscriptions/claude-personal/cooldown")
        # 簽的 body bytes 就是送出的 bytes（同一份序列化結果）。
        self.assertEqual(seen["body"], request["body"])
        self.assertEqual(json.loads(seen["body"].decode("utf-8")),
                         {"until": "2026-11-23T18:00:00Z", "model": "中文模型"})

    def test_C705_signer_none不帶任何X_LGW標頭(self):
        server = self.serve()
        Client(server.url).list()
        for name in ("X-LGW-Auth-Version", "X-LGW-Key-Id", "X-LGW-Timestamp",
                     "X-LGW-Nonce", "X-LGW-Signature"):
            self.assertIsNone(header(server.requests[0], name))


class BaseUrlTest(RawServerCase):
    """C-702：簽的第 7 行是**實際送出**的 request-target，含 base_url 的路徑前綴。

    用一個會組八行訊息的假 signer 記下第 7／8 行，再和記錄原始 request line 的
    server 比對（#266 架構審 中-1：以前簽的是不含前綴的 `path`，帶前綴就 401）。
    """

    def recording_signer(self, seen: dict):
        class StubSigner:
            def headers(self, *, method, target, body=b"", now=None):
                message = build_signing_message(
                    key_id="stub", timestamp="1", nonce="n" * 43,
                    method=method, target=target, body=body)
                lines = message.decode("ascii").split("\n")
                seen.update({"method": method, "target": target,
                             "line7": lines[6], "line8": lines[7]})
                return {"X-LGW-Auth-Version": "1", "X-LGW-Key-Id": "stub",
                        "X-LGW-Timestamp": "1", "X-LGW-Nonce": "n" * 43,
                        "X-LGW-Signature": "s" * 240}

        return StubSigner()

    def test_C702_base_url不帶路徑帶前綴帶結尾斜線的第7行都等於實際送出(self):
        for suffix in ("", "/", "/prefix", "/prefix/"):
            with self.subTest(base_url_suffix=suffix):
                server = self.serve()
                seen: dict = {}
                result = Client(
                    server.url + suffix,
                    signer=self.recording_signer(seen),
                ).resolve("opencode", "opencode-zen", model="a+b/c 中文")
                self.assertTrue(result.ok, result)
                sent = server.requests[0]["target"]
                self.assertEqual(seen["target"], sent)
                self.assertEqual(seen["line7"], sent)
                self.assertIn("%2B", seen["line7"])
                self.assertIn("%2F", seen["line7"])
                prefix = "/prefix" if "prefix" in suffix else ""
                self.assertTrue(sent.startswith(prefix + "/v1/resolve?"), sent)
                for character in seen["line7"]:
                    self.assertLess(ord(character), 128)

    def test_C702_base_url帶前綴時POST的target與body也一致(self):
        server = self.serve(lambda *args: (204, {}, b""))
        seen: dict = {}
        result = Client(
            server.url + "/prefix",
            signer=self.recording_signer(seen),
        ).cooldown("sub/1", "2026-11-23T18:00:00Z", model="中文+1")
        self.assertTrue(result.ok, result)
        request = server.requests[0]
        self.assertEqual(request["target"],
                         "/prefix/v1/subscriptions/sub%2F1/cooldown")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["line7"], request["target"])
        self.assertEqual(seen["line8"],
                         hashlib.sha256(request["body"]).hexdigest())
        self.assertEqual(seen["line8"], hashlib.sha256(json.dumps(
            {"until": "2026-11-23T18:00:00Z", "model": "中文+1"},
        ).encode("utf-8")).hexdigest())


class EndToEndTest(unittest.TestCase):
    """C-708：真的 lgwd enforce gate、真的兩庫、真的 ssh-keygen、真的五個方法。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = cls._tmp.name
        cls.key = KeyFactory(cls.dir, "e2e")
        cls.state_dir = os.path.join(cls.dir, "state")
        os.makedirs(cls.state_dir)
        cls.paths = auth_paths(
            cls.state_dir, registry=os.path.join(cls.dir, "auth-registry.json"))
        create_stores(cls.paths.trust_db, cls.paths.replay_db)
        write_registry_file(
            cls.paths.registry,
            registry_bytes(1, [key_entry(KEY_ID, cls.key)]))
        cls.runtime = auth.open_runtime(
            auth.AuthConfig(
                mode="enforce", registry_path=cls.paths.registry,
                state_dir=cls.state_dir, per_key_max=600, poll_interval=0.0),
            SystemClock(), owner_uid=0, registry_owner_uid=os.getuid())
        cls.counter = CountingGate(cls.runtime.gate)
        cls.service = FakeService()
        cls.server = make_api_server(cls.service, HOST, 0, gate=cls.counter)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.url = "http://{0}:{1}".format(host, port)
        cls.sock = os.path.join(cls.dir, "agent.sock")
        cls.agent = subprocess.Popen(
            ["/usr/bin/ssh-agent", "-D", "-a", cls.sock],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while not os.path.exists(cls.sock):
            if time.monotonic() > deadline:
                raise AssertionError("ssh-agent socket did not appear")
            time.sleep(0.02)
        subprocess.run(
            ["/usr/bin/ssh-add", cls.key.private_path], check=True,
            capture_output=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": cls.dir,
                 "SSH_AUTH_SOCK": cls.sock},
            timeout=30,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.runtime.stop()
        cls.agent.terminate()
        try:
            cls.agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.agent.kill()
            cls.agent.wait(timeout=5)
        cls._tmp.cleanup()

    def client(self, key_id=KEY_ID, **kwargs):
        return Client(self.url, signer=Signer(
            key_id=key_id, private_key_path=self.key.private_path, **kwargs))

    def test_C708_五個方法端到端(self):
        client = self.client()
        listing = client.list()
        self.assertTrue(listing.ok, listing)
        self.assertEqual(listing.status, 200)
        usage = client.usage()
        self.assertTrue(usage.ok, usage)
        resolve = client.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertTrue(resolve.ok, resolve)
        roles = client.roles("impl", "opencode", data="public")
        self.assertTrue(roles.ok, roles)
        cooldown = client.cooldown("claude-personal", "2026-11-23T18:00:00Z")
        self.assertTrue(cooldown.ok, cooldown)
        self.assertEqual(cooldown.status, 204)

    def test_C702_query含plus斜線與非ASCII_POST的body有中文(self):
        client = self.client()
        model = "a+b/c 中文"
        resolve = client.resolve("opencode", "opencode-zen", model=model)
        self.assertTrue(resolve.ok, resolve)
        name, agent, provider, seen_model, _, _, _ = self.service.calls[-1]
        self.assertEqual((name, agent, provider, seen_model),
                         ("resolve", "opencode", "opencode-zen", model))
        cooldown = client.cooldown(
            "claude-personal", "2026-11-23T18:00:00Z", model="模型+1")
        self.assertTrue(cooldown.ok, cooldown)
        name, sub_id, body = self.service.calls[-1]
        self.assertEqual((name, sub_id), ("register_cooldown", "claude-personal"))
        self.assertEqual(body, {"until": "2026-11-23T18:00:00Z", "model": "模型+1"})

    def test_C704_公鑰檔加ssh_agent端到端(self):
        client = Client(self.url, signer=Signer(
            key_id=KEY_ID, public_key_path=self.key.public_path,
            agent_sock=self.sock))
        result = client.list()
        self.assertTrue(result.ok, result)

    def test_C706_未知key為401並附時差(self):
        result = self.client(key_id="unknown-key").list()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 401)
        self.assertEqual(result.error, "unauthorized")
        self.assertIsInstance(result.data["clock_skew_s"], int)

    def test_C705_簽章失敗時gate完全沒被呼叫(self):
        before = self.counter.calls
        result = Client(self.url, signer=Signer(
            key_id=KEY_ID, private_key_path=self.key.private_path,
            sshkeygen="/nonexistent/ssh-keygen")).list()
        self.assertEqual(result.error, "signer_unavailable")
        self.assertEqual(self.counter.calls, before)

    def _tampered_status(self, url, method, body,
                         signed_target, signed_method, signed_body) -> int:
        """用真的 Signer 簽一組值，卻送出另一組（模擬中間人竄改）。"""
        headers = Signer(
            key_id=KEY_ID,
            private_key_path=self.key.private_path).headers(
                method=signed_method, target=signed_target, body=signed_body)
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_C702_篡改已簽章請求的query_body_method都401(self):
        """C-702 的負向端到端：gate 驗的是實際送出的 target／body／method。"""
        cooldown_url = self.url + "/v1/subscriptions/claude-personal/cooldown"
        signed_body = json.dumps(
            {"until": "2026-11-23T18:00:00Z"}).encode("utf-8")
        cases = {
            "query": (self.url + "/v1/subscriptions?agent=x", "GET", None,
                      "/v1/subscriptions", "GET", b""),
            "body": (cooldown_url, "POST", signed_body + b" ",
                     cooldown_url[len(self.url):], "POST", signed_body),
            "method": (cooldown_url, "POST", None,
                       cooldown_url[len(self.url):], "GET", b""),
        }
        for name, (url, method, body, target, signed_method, sent_signed_body) in (
                cases.items()):
            with self.subTest(tampered=name):
                status = self._tampered_status(
                    url, method, body, target, signed_method, sent_signed_body)
                self.assertEqual(status, 401)


class CountingGate:
    """把 `authenticate` 計數後交給真的 gate（證明請求有沒有送到服務）。"""

    def __init__(self, real):
        self.real = real
        self.mode = real.mode
        self.calls = 0

    def authenticate(self, **kwargs):
        self.calls += 1
        return self.real.authenticate(**kwargs)


if __name__ == "__main__":
    unittest.main()
