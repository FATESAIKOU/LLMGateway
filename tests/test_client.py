"""lgw/client.py 的測試（v4 design 決定 6）：假 service（in-process HTTP）＋隨機埠。

- 三個介面（並存期四個呼叫）：list、resolve、get、cooldown；只驗 request 形狀、
  狀態碼與 Result 語意。
- 假 service 固定回覆；key／env 的憑證值一律是假 canary，不用真 key、不連真機器。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lgw.client import Client, Result

HOST = "127.0.0.1"
CANARY = "fake-canary-key-007"

SUBSCRIPTION = {
    "schema": 1,
    "id": "claude-personal",
    "provider": "claude",
    "kind": "claude-subscription",
    "agents": ["claude-code"],
    "priority": 10,
    "models": {"allow": ["*"], "default": None},
    "endpoint": None,
    "cooldown": None,
    "available": True,
}


class FakeService:
    """假 v4 service：list／get／登錄冷卻；記下每個請求的形狀。"""

    def __init__(self):
        self.requests = []  # (method, path, query, body, authorization)

    def handle(self, method, path, query, body, authorization):
        self.requests.append((method, path, query, body, authorization))
        if method == "GET" and path == "/v1/subscriptions":
            return 200, {"schema": 1, "subscriptions": [dict(SUBSCRIPTION)]}
        if method == "GET" and path == "/v1/resolve":
            agent = query.get("agent", [None])[0]
            provider = query.get("provider", [None])[0]
            model = query.get("model", [None])[0]
            subscription = query.get("subscription", [None])[0]
            exclude = query.get("exclude", [None])[0]
            if provider == "mna":
                return 403, {"error": "model_not_allowed"}
            if provider == "cooling":
                return 409, {"error": "cooling", "until": "2026-10-11T15:00:00Z"}
            if provider == "nom":
                return 400, {"error": "no_model"}
            if provider == "nc":
                return 409, {"error": "no_candidate"}
            return 200, {
                "schema": 1,
                "env_schema": 1,
                "meta": {"subscription": subscription or "opencode-zen",
                         "provider": provider, "agent": agent,
                         "model": model or "m2"},
                "env": {"OPENCODE_API_KEY": CANARY},
            }
        if method == "GET" and path.startswith("/v1/subscriptions/"):
            sub_id = urllib.parse.unquote(path.rsplit("/", 1)[1])
            if sub_id == "gone":
                return 404, {"error": "not_found"}
            if sub_id == "idle":
                return 409, {"error": "not_deployed"}
            if sub_id == "garbage":
                return 200, b"<html>not json</html>"
            if sub_id == "notobject":
                return 200, b"[1, 2]"
            agent = query.get("agent", [None])[0]
            if sub_id == "claude-personal" and agent != "claude-code":
                return 403, {"error": "agent_not_allowed"}
            payload = dict(SUBSCRIPTION)
            payload["key"] = CANARY
            return 200, payload
        if method == "POST" and path.endswith("/cooldown"):
            sub_id = urllib.parse.unquote(path.split("/")[3])
            if sub_id == "gone":
                return 404, {"error": "not_found"}
            if sub_id == "past":
                return 400, {"error": "until_out_of_range"}
            until = body.get("until")
            if not isinstance(until, str) or not until.endswith("Z"):
                return 400, {"error": "invalid_until"}
            return 204, {}
        return 404, {"error": "not_found"}


def make_server(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _dispatch(self, method):
            parsed = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            status, payload = fake.handle(
                method,
                parsed.path,
                urllib.parse.parse_qs(parsed.query),
                body,
                self.headers.get("Authorization"),
            )
            if status == 204:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if isinstance(payload, bytes):
                data = payload
            else:
                data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    server = ThreadingHTTPServer((HOST, 0), Handler)
    server.daemon_threads = True
    return server


class ClientCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeService()
        self.server = make_server(self.fake)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        _, port = self.server.server_address[:2]
        self.client = Client("http://{0}:{1}".format(HOST, port))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ListTest(ClientCase):
    def test_list_ok(self):
        res = self.client.list()
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.data["schema"], 1)
        subs = res.data["subscriptions"]
        self.assertEqual(subs[0]["id"], "claude-personal")
        self.assertNotIn("key", subs[0])  # list 不含 key
        method, path, query, body, auth = self.fake.requests[-1]
        self.assertEqual((method, path, query), ("GET", "/v1/subscriptions", {}))
        self.assertEqual(body, {})
        self.assertIsNone(auth)  # 不帶任何認證標頭


class ResolveTest(ClientCase):
    def test_resolve_ok(self):
        res = self.client.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.data["meta"]["provider"], "opencode-zen")
        self.assertEqual(res.data["env"]["OPENCODE_API_KEY"], CANARY)
        method, path, query, _, auth = self.fake.requests[-1]
        self.assertEqual((method, path), ("GET", "/v1/resolve"))
        self.assertEqual(query, {"agent": ["opencode"], "provider": ["opencode-zen"],
                                 "model": ["big-pickle"]})
        self.assertIsNone(auth)  # 不帶任何認證標頭

    def test_resolve_省略時不帶參數(self):
        self.client.resolve("claude-code", "anthropic")
        _, path, query, _, _ = self.fake.requests[-1]
        self.assertEqual(query, {"agent": ["claude-code"], "provider": ["anthropic"]})

    def test_resolve_model_plus編碼(self):
        # `+` 由 client 編成 %2B；server 的 parse_qs form 解碼會把未編碼的 + 解成空白
        self.client.resolve("opencode", "opencode-zen", model="a+b")
        _, _, query, _, _ = self.fake.requests[-1]
        self.assertEqual(query["model"], ["a+b"])
        self.assertNotIn("a b", ",".join(query["model"]))

    def test_resolve_model可含斜線(self):
        res = self.client.resolve("opencode", "openrouter", model="openrouter/free")
        self.assertTrue(res.ok)
        _, _, query, _, _ = self.fake.requests[-1]
        self.assertEqual(query["model"], ["openrouter/free"])

    def test_resolve_exclude合併(self):
        self.client.resolve("claude-code", "anthropic",
                            exclude=["claude-personal", "x y"])
        _, _, query, _, _ = self.fake.requests[-1]
        self.assertEqual(query["exclude"], ["claude-personal,x y"])

    def test_resolve_pin住(self):
        self.client.resolve("claude-code", "anthropic", subscription="claude-company")
        _, _, query, _, _ = self.fake.requests[-1]
        self.assertEqual(query, {"agent": ["claude-code"], "provider": ["anthropic"],
                                 "subscription": ["claude-company"]})

    def test_resolve_錯誤對照(self):
        cases = [("mna", 403, "model_not_allowed"),
                 ("nom", 400, "no_model"),
                 ("nc", 409, "no_candidate")]
        for provider, code, err in cases:
            with self.subTest(err=err):
                res = self.client.resolve("opencode", provider)
                self.assertFalse(res.ok)
                self.assertEqual(res.status, code)
                self.assertEqual(res.error, err)

    def test_resolve_cooling帶until(self):
        res = self.client.resolve("opencode", "cooling")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 409)
        self.assertEqual(res.error, "cooling")
        # until 與錯誤碼都在 Result 裡可讀（repr 不印）
        self.assertEqual(res.data, {})


class GetTest(ClientCase):
    def test_get_ok_returns_key(self):
        res = self.client.get("claude-personal", "claude-code")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.data["key"], CANARY)
        method, path, query, _, auth = self.fake.requests[-1]
        self.assertEqual((method, path), ("GET", "/v1/subscriptions/claude-personal"))
        self.assertEqual(query, {"agent": ["claude-code"]})
        self.assertIsNone(auth)

    def test_get_403_agent_not_allowed(self):
        res = self.client.get("claude-personal", "opencode")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 403)
        self.assertEqual(res.error, "agent_not_allowed")
        self.assertFalse(res.unreachable)

    def test_get_404(self):
        res = self.client.get("gone", "claude-code")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 404)
        self.assertEqual(res.error, "not_found")

    def test_get_409_not_deployed(self):
        res = self.client.get("idle", "claude-code")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 409)
        self.assertEqual(res.error, "not_deployed")

    def test_bad_response_not_json(self):
        res = self.client.get("garbage", "claude-code")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.error, "bad_response")  # 碼不含空白
        self.assertFalse(res.unreachable)

    def test_bad_response_not_object(self):
        res = self.client.get("notobject", "claude-code")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.error, "bad_response")

    def test_get_encodes_id_and_agent(self):
        self.client.get("a/b c", "agent x")
        _, path, query, _, _ = self.fake.requests[-1]
        self.assertEqual(path, "/v1/subscriptions/a%2Fb%20c")
        self.assertEqual(query, {"agent": ["agent x"]})


class CooldownTest(ClientCase):
    def test_cooldown_204(self):
        res = self.client.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 204)
        self.assertEqual(res.data, {})
        method, path, _, body, auth = self.fake.requests[-1]
        self.assertEqual(
            (method, path), ("POST", "/v1/subscriptions/claude-personal/cooldown")
        )
        self.assertEqual(body, {"until": "2026-10-11T15:00:00Z"})
        self.assertIsNone(auth)

    def test_cooldown_400_invalid_until(self):
        res = self.client.cooldown("claude-personal", "not-a-time")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 400)
        self.assertEqual(res.error, "invalid_until")
        _, _, _, body, _ = self.fake.requests[-1]
        self.assertEqual(body, {"until": "not-a-time"})  # 原樣送出，判讀在服務

    def test_cooldown_400_until_out_of_range(self):
        res = self.client.cooldown("past", "2020-01-01T00:00:00Z")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 400)
        self.assertEqual(res.error, "until_out_of_range")

    def test_cooldown_404(self):
        res = self.client.cooldown("gone", "2026-10-11T15:00:00Z")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 404)
        self.assertEqual(res.error, "not_found")


class UnreachableTest(unittest.TestCase):
    def _closed_port(self):
        sock = socket.socket()
        sock.bind((HOST, 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def test_closed_port(self):
        client = Client("http://{0}:{1}".format(HOST, self._closed_port()), timeout=2)
        res = client.list()
        self.assertFalse(res.ok)
        self.assertTrue(res.unreachable)
        self.assertIsNone(res.status)
        self.assertTrue(res.error)

    def test_timeout(self):
        class SleepHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(1.0)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((HOST, 0), SleepHandler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        try:
            _, port = server.server_address[:2]
            client = Client("http://{0}:{1}".format(HOST, port), timeout=0.1)
            res = client.list()
            self.assertFalse(res.ok)
            self.assertTrue(res.unreachable)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ResultTest(unittest.TestCase):
    def test_repr_no_data(self):
        """repr(Result) 不含 data 內容（get 的 key 不得進 log）。"""
        res = Result(ok=True, status=200, data={"key": CANARY, "id": "claude-personal"})
        text = repr(res)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("key", text)
        self.assertIn("ok=True", text)
        self.assertIn("200", text)

    def test_timeout_defaults_empty_on_error(self):
        res = Result(ok=False, status=403, error="agent_not_allowed")
        self.assertEqual(res.data, {})
        self.assertFalse(res.unreachable)


if __name__ == "__main__":
    unittest.main()
