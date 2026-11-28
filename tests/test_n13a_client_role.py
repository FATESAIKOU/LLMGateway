"""#347 N13a C-1044：``lgw.client.Client.resolve_role``。

- 與舊 ``resolve`` 分離：不帶 agent／provider；``task_length`` 必填；exclude／exclude_family
  各自逗號合併成一個鍵；409 的 ``until``／``skipped`` 透傳在 data 裡；``cooldown`` 不變。
- 假 HTTP 服務只在本機 loopback 上跑，不連真機。
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lgw.client import Client  # noqa: E402

SKIPPED = [
    {"slot": 1, "subscription": "antigravity-gemini", "model": "gemini-3.8-flash-low",
     "reason": "usage_exhausted", "until": "2026-12-04T08:00:00Z"},
    {"slot": 2, "subscription": "opencode-zen", "model": "big-pickle",
     "reason": "excluded", "until": None},
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 不印到 stderr
        pass

    def _reply(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append({"method": method, "path": parsed.path,
                                 "query": urllib.parse.parse_qs(
                                     parsed.query, keep_blank_values=True),
                                 "raw_query": parsed.query,
                                 "body": json.loads(body) if body else None})
        status, payload = self.server.canned(parsed)
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply("GET")

    def do_POST(self):
        self._reply("POST")


def _canned(parsed):
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path == "/v1/resolve" and "role" in query:
        role = query["role"][0]
        if role == "PMO":
            return 400, {"error": "role_not_selectable"}
        if role == "cool":
            return 409, {"error": "cooling", "until": "2026-12-04T08:00:00Z",
                         "skipped": SKIPPED}
        return 200, {"schema": 1, "env_schema": 1,
                     "meta": {"role": role, "data_effective": "real"},
                     "env": {"OPENCODE_API_KEY": "fake-value"}}
    if parsed.path == "/v1/resolve":
        return 200, {"schema": 1, "env_schema": 1, "meta": {}, "env": {}}
    if parsed.path.endswith("/cooldown"):
        return 204, {}
    return 404, {"error": "not_found"}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.seen = []
        cls.server.canned = _canned
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:{0}".format(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.seen.clear()
        self.client = Client(self.base, timeout=5)


class ResolveRoleTests(_Base):
    def test_c1044_sends_role_and_task_length_only_no_agent_or_provider(self):
        res = self.client.resolve_role("impl", "short")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 200)
        [req] = self.server.seen
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/v1/resolve")
        self.assertEqual(req["query"]["role"], ["impl"])
        self.assertEqual(req["query"]["task_length"], ["short"])
        for name in ("agent", "provider", "subscription"):
            self.assertNotIn(name, req["query"])

    def test_c1044_task_length_is_required(self):
        with self.assertRaises(TypeError):
            self.client.resolve_role("impl")  # 位置參數漏掉
        with self.assertRaises(TypeError):
            self.client.resolve_role("impl", None)
        self.assertEqual(self.server.seen, [])  # 不送出任何請求

    def test_c1044_exclude_and_exclude_family_merged_into_one_key_each(self):
        self.client.resolve_role("qa", "long", exclude=["opencode-zen", "openrouter"],
                                 exclude_family=["google", "anthropic"])
        [req] = self.server.seen
        self.assertEqual(req["query"]["exclude"], ["opencode-zen,openrouter"])
        self.assertEqual(req["query"]["exclude_family"], ["google,anthropic"])

    def test_c1044_optional_params_omitted_when_absent(self):
        self.client.resolve_role("impl", "short")
        [req] = self.server.seen
        for name in ("exclude", "exclude_family", "model", "data", "ref"):
            self.assertNotIn(name, req["query"])

    def test_c1044_optional_params_are_percent_encoded(self):
        self.client.resolve_role("impl", "short", model="a+b c", data="real",
                                 ref="life.1")
        [req] = self.server.seen
        self.assertIn("model=a%2Bb%20c", req["raw_query"])
        self.assertEqual(req["query"]["model"], ["a+b c"])
        self.assertEqual(req["query"]["data"], ["real"])
        self.assertEqual(req["query"]["ref"], ["life.1"])

    def test_c1044_409_until_and_skipped_pass_through_in_data(self):
        res = self.client.resolve_role("cool", "short")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 409)
        self.assertEqual(res.error, "cooling")
        self.assertEqual(res.data["until"], "2026-12-04T08:00:00Z")
        self.assertEqual(res.data["skipped"], SKIPPED)

    def test_c1044_400_role_not_selectable_passes_through(self):
        res = self.client.resolve_role("PMO", "short")
        self.assertEqual((res.ok, res.status, res.error),
                         (False, 400, "role_not_selectable"))

    def test_c1044_success_env_not_in_repr(self):
        res = self.client.resolve_role("impl", "short")
        self.assertIn("fake-value", res.data["env"]["OPENCODE_API_KEY"])
        self.assertNotIn("fake-value", repr(res))

    def test_c1044_unreachable_does_not_raise(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        res = Client("http://127.0.0.1:{0}".format(port), timeout=2).resolve_role(
            "impl", "short")
        self.assertTrue(res.unreachable)
        self.assertEqual(res.error, "unreachable")


class SeparationTests(_Base):
    def test_c1044_legacy_resolve_unchanged_and_separate(self):
        res = self.client.resolve("claude-code", "anthropic", subscription="claude-personal")
        self.assertTrue(res.ok)
        [req] = self.server.seen
        self.assertEqual(req["query"]["agent"], ["claude-code"])
        self.assertEqual(req["query"]["provider"], ["anthropic"])
        self.assertNotIn("role", req["query"])
        self.assertNotIn("task_length", req["query"])

    def test_c1044_legacy_resolve_still_requires_agent_and_provider(self):
        self.client.resolve("claude-code", "anthropic")
        [req] = self.server.seen
        self.assertIn("agent", req["query"])
        self.assertIn("provider", req["query"])

    def test_c1044_cooldown_unchanged(self):
        res = self.client.cooldown("opencode-zen", "2026-12-04T08:00:00Z", model="big-pickle")
        self.assertTrue(res.ok)
        [req] = self.server.seen
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/v1/subscriptions/opencode-zen/cooldown")
        self.assertEqual(req["body"], {"until": "2026-12-04T08:00:00Z",
                                       "model": "big-pickle"})


if __name__ == "__main__":
    unittest.main()
