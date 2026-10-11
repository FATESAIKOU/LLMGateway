"""lgw/client.py 的測試：LeaseClient 打真 HTTP（假 service＋隨機埠），不用真 key。"""

import ast
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lgw.client as client_mod
from lgw.client import LeaseClient
from lgwd import errors
from lgwd.http import make_api_server

HOST = "127.0.0.1"
GOOD_TOKEN = "good-token"
LEASE = {
    "lease_id": "L1",
    "subscription": "claude-personal",
    "agent": "claude-code",
    "model": "default",
    "env": {"CLAUDE_CODE_OAUTH_TOKEN": "x"},
    "unset": ["ANTHROPIC_API_KEY"],
    "expires_at": "2026-10-11T12:00:00Z",
    "confirm_by": "2026-10-11T00:10:00Z",
}


class FakeService:
    """假 service：固定回覆；raise_for[方法名] 設例外就丟；記下收到的 body。」"""

    def __init__(self):
        self.bodies = []
        self.raise_for = {}
        self.seen_tokens = []

    def authenticate(self, token):
        self.seen_tokens.append(token)
        return "worker-a" if token == GOOD_TOKEN else None

    def request_lease(self, client, body):
        self.bodies.append(("request_lease", body))
        if "request_lease" in self.raise_for:
            raise self.raise_for["request_lease"]
        return 201, dict(LEASE)

    def _op(self, name, client, lease_id, body, code, payload):
        self.bodies.append((name, lease_id, body))
        if name in self.raise_for:
            raise self.raise_for[name]
        return code, payload

    def start(self, client, lease_id, body):
        return self._op("start", client, lease_id, body, 200,
                        {"expires_at": LEASE["expires_at"]})

    def exhausted(self, client, lease_id, body):
        return self._op("exhausted", client, lease_id, body, 200,
                        {"next": dict(LEASE)})

    def renew(self, client, lease_id, body):
        return self._op("renew", client, lease_id, body, 200,
                        {"expires_at": LEASE["expires_at"]})

    def release(self, client, lease_id, body):
        return self._op("release", client, lease_id, body, 204, {})

    def report_usage(self, client, lease_id, body):
        return self._op("report_usage", client, lease_id, body, 204, {})

    def board(self):
        return {"subscriptions": []}


class ClientCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeService()
        self.server = make_api_server(self.fake, HOST, 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        _, port = self.server.server_address[:2]
        self.tmp = tempfile.TemporaryDirectory()
        self.token_path = str(Path(self.tmp.name) / "client-token")
        Path(self.token_path).write_text(GOOD_TOKEN + "\n", encoding="utf-8")
        self.client = LeaseClient("http://{0}:{1}".format(HOST, port),
                                  self.token_path)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()


class LeaseTest(ClientCase):
    def test_request_lease_ok(self):
        res = self.client.request_lease("worker-a", "coder", "claude-code", "i1")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 201)
        self.assertEqual(res.data["lease_id"], "L1")
        self.assertNotIn("api_version", res.data)
        self.assertIsNone(res.refusal)
        name, body = self.fake.bodies[-1]
        self.assertEqual(name, "request_lease")
        self.assertEqual(len(body["request_id"]), 32)  # 自動帶 uuid4 hex

    def test_request_id_reuse(self):
        self.client.request_lease("w", "r", "claude-code", "i", request_id="r1")
        _, body = self.fake.bodies[-1]
        self.assertEqual(body["request_id"], "r1")

    def test_optionals_omitted(self):
        self.client.request_lease("w", "r", "claude-code", "i")
        _, body = self.fake.bodies[-1]
        for key in ("model", "subscription", "purpose", "preferences"):
            self.assertNotIn(key, body)
        res = self.client.request_lease("w", "r", "claude-code", "i",
                                        model="m", subscription="s",
                                        purpose="p", preferences={"floor_model": None})
        self.assertTrue(res.ok)
        _, body = self.fake.bodies[-1]
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["preferences"], {"floor_model": None})

    def test_refused(self):
        self.fake.raise_for["request_lease"] = errors.refusal(
            "all_cooling", "2026-10-11T03:47:00Z")
        res = self.client.request_lease("w", "r", "claude-code", "i")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 409)
        self.assertEqual(res.refusal.reason, "all_cooling")
        self.assertEqual(res.refusal.earliest_reset, "2026-10-11T03:47:00Z")

    def test_refused_unknown_reason(self):
        self.fake.raise_for["request_lease"] = errors.ApiError(
            409, "refused", reason="future_reason")
        res = self.client.request_lease("w", "r", "claude-code", "i")
        self.assertFalse(res.ok)
        self.assertEqual(res.refusal.reason, "future_reason")  # 不認得也照放
        self.assertIsNone(res.refusal.earliest_reset)

    def test_exhausted_next(self):
        res = self.client.exhausted("L1", "claude_limit", "2026-10-11T03:45:00Z")
        self.assertTrue(res.ok)
        self.assertEqual(res.data["next"]["lease_id"], "L1")
        _, _, body = self.fake.bodies[-1]
        self.assertEqual(body["signal"], "claude_limit")
        self.assertEqual(body["resets_at"], "2026-10-11T03:45:00Z")

    def test_exhausted_refusal(self):
        self.fake.exhausted = lambda c, i, b: (200, {"next": None, "refusal": {
            "reason": "all_cooling", "earliest_reset": "2026-10-11T03:47:00Z"}})
        res = self.client.exhausted("L1", "claude_limit")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.refusal.reason, "all_cooling")

    def test_start_renew(self):
        res = self.client.start("L1")
        self.assertTrue(res.ok)
        self.assertIn("expires_at", res.data)
        name, _, body = self.fake.bodies[-1]
        self.assertEqual(name, "start")
        self.assertEqual(len(body["request_id"]), 32)
        res = self.client.renew("L1", request_id="rn1")
        self.assertTrue(res.ok)
        name, _, body = self.fake.bodies[-1]
        self.assertEqual((name, body["request_id"]), ("renew", "rn1"))

    def test_release_and_usage(self):
        res = self.client.release("L1", reason="done")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 204)
        self.assertEqual(res.data, {})
        name, _, body = self.fake.bodies[-1]
        self.assertEqual((name, body["reason"]), ("release", "done"))
        res = self.client.report_usage("L1", {"note": "x"})
        self.assertTrue(res.ok)
        name, _, body = self.fake.bodies[-1]
        self.assertEqual(name, "report_usage")
        self.assertEqual(body, {"snapshot": {"note": "x"}})  # usage 不帶 request_id

    def test_board(self):
        res = self.client.board()
        self.assertTrue(res.ok)
        self.assertEqual(res.data, {"subscriptions": []})

    def test_whoami(self):
        res = self.client.whoami()
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.data, {"client": "worker-a"})

    def test_400_error(self):
        self.fake.raise_for["renew"] = errors.bad_request(missing=["request_id"])
        res = self.client.renew("L1")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 400)
        self.assertEqual(res.error, "bad_request")

    def test_401(self):
        Path(self.token_path).write_text("nope\n", encoding="utf-8")
        bad = LeaseClient(self.client._base_url, self.token_path)
        res = bad.board()
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 401)
        self.assertEqual(res.error, "unauthorized")

    def test_token_newline_stripped(self):
        self.client.board()
        self.assertEqual(self.fake.seen_tokens[-1], GOOD_TOKEN)  # 結尾換行已去掉


class LocalFileTest(unittest.TestCase):
    def test_missing_token_file(self):
        with self.assertRaises(FileNotFoundError):
            LeaseClient("http://127.0.0.1:1", "/nonexistent-dir/t")

    def test_empty_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t")
            Path(path).write_text("\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                LeaseClient("http://127.0.0.1:1", path)

    def test_repr_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t")
            Path(path).write_text("s3cr3t-token-value\n", encoding="utf-8")
            client = LeaseClient("http://127.0.0.1:1", path, timeout=3)
            text = repr(client)
            self.assertNotIn("s3cr3t-token-value", text)
            self.assertIn("http://127.0.0.1:1", text)

    def test_no_agent_tool_imports(self):
        """lgw 不引用 subprocess、herdr、agora（AST 檢查，docstring 文字不算）。"""
        tree = ast.parse(Path(client_mod.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported.isdisjoint({"subprocess", "herdr", "agora"}),
                        imported)


class UnreachableTest(unittest.TestCase):
    def _closed_port_client(self, timeout=2):
        sock = socket.socket()
        sock.bind((HOST, 0))
        port = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t")
            Path(path).write_text(GOOD_TOKEN, encoding="utf-8")
            return LeaseClient("http://{0}:{1}".format(HOST, port), path,
                               timeout=timeout)

    def test_closed_port(self):
        client = self._closed_port_client()
        res = client.board()
        self.assertFalse(res.ok)
        self.assertTrue(res.unreachable)
        self.assertIsNone(res.status)
        self.assertTrue(res.error)

    def test_timeout(self):
        class SleepHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(1.0)
                raw = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((HOST, 0), SleepHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            _, port = server.server_address[:2]
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "t")
                Path(path).write_text(GOOD_TOKEN, encoding="utf-8")
                client = LeaseClient("http://{0}:{1}".format(HOST, port), path,
                                     timeout=0.1)
                res = client.board()
            self.assertFalse(res.ok)
            self.assertTrue(res.unreachable)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
