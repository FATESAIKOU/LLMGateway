"""lgwd/http.py 的測試：全用假 service（固定值或丟 ApiError），不用真 key。"""

import http.client
import json
import threading
import unittest
import urllib.error
import urllib.request

from lgwd import errors
from lgwd.http import make_api_server, make_board_server

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
BOARD = {
    "api_version": 1,
    "generated_at": "2026-10-11T00:00:00Z",
    "subscriptions": [
        {
            "name": "claude-personal",
            "deployed": True,
            "active": [
                {
                    "worker": "worker-a",
                    "role": "coder",
                    "agent": "claude-code",
                    "since": "2026-10-10T20:00:00Z",
                }
            ],
            "count": 1,
            "cap": 2,
            "cooling": False,
            "cooling_until": None,
            "usage": None,
        }
    ],
}


class FakeService:
    """假 service：固定回覆；raise_for[方法名] 設例外就丟。會記下收到的呼叫。"""

    def __init__(self):
        self.calls = []
        self.raise_for = {}

    def authenticate(self, token):
        return "worker-a" if token == GOOD_TOKEN else None

    def request_lease(self, client, body):
        self.calls.append(("request_lease", client, body))
        if "request_lease" in self.raise_for:
            raise self.raise_for["request_lease"]
        return 201, dict(LEASE)

    def _lease_op(self, name, client, lease_id, body, code, payload):
        self.calls.append((name, client, lease_id, body))
        if name in self.raise_for:
            raise self.raise_for[name]
        return code, payload

    def start(self, client, lease_id, body):
        return self._lease_op(
            "start", client, lease_id, body, 200, {"expires_at": LEASE["expires_at"]}
        )

    def exhausted(self, client, lease_id, body):
        return self._lease_op(
            "exhausted", client, lease_id, body, 200, {"next": dict(LEASE)}
        )

    def renew(self, client, lease_id, body):
        return self._lease_op(
            "renew", client, lease_id, body, 200, {"expires_at": LEASE["expires_at"]}
        )

    def release(self, client, lease_id, body):
        return self._lease_op("release", client, lease_id, body, 204, {})

    def report_usage(self, client, lease_id, body):
        return self._lease_op("report_usage", client, lease_id, body, 204, {})

    def board(self):
        self.calls.append(("board",))
        if "board" in self.raise_for:
            raise self.raise_for["board"]
        return json.loads(json.dumps(BOARD))


class ServerCase(unittest.TestCase):
    def serve(self, server):
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        host, port = server.server_address[:2]
        return "http://{0}:{1}".format(host, port)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def call(self, base, method, path, body=None, raw=None, token=GOOD_TOKEN):
        """打 API；HTTPError 也解析成 (狀態碼, body)。"""
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None
        )
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class ApiTest(ServerCase):
    def setUp(self):
        self.fake = FakeService()
        self.base = self.serve(make_api_server(self.fake, HOST, 0))

    def body_of(self, payload):
        data = json.loads(payload.decode())
        self.assertEqual(data.pop("api_version"), 1)  # 每個回覆都帶
        return data

    def test_leases_ok(self):
        status, payload = self.call(
            self.base,
            "POST",
            "/v1/leases",
            {"worker": "worker-a", "role": "r", "agent": "claude-code",
             "instance": "i1", "request_id": "q1"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(self.body_of(payload)["lease_id"], "L1")
        name, client, body = self.fake.calls[-1]
        self.assertEqual((name, client), ("request_lease", "worker-a"))
        self.assertEqual(body["request_id"], "q1")

    def test_bad_json(self):
        status, payload = self.call(self.base, "POST", "/v1/leases", raw=b"{nope")
        self.assertEqual(status, 400)
        self.assertEqual(self.body_of(payload)["error"], "bad_request")

    def test_json_list_body(self):
        status, payload = self.call(self.base, "POST", "/v1/leases", raw=b"[1]")
        self.assertEqual(status, 400)

    def test_missing_bearer(self):
        status, payload = self.call(
            self.base, "POST", "/v1/leases", {"request_id": "q"}, token=None
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.body_of(payload)["error"], "unauthorized")

    def test_wrong_bearer(self):
        for header_value in ("Bearer bad-token", "Token abc", "Bearer ", "Bearer"):
            with self.subTest(header_value=header_value):
                status, _ = self._raw_auth(header_value)
                self.assertEqual(status, 401)

    def _raw_auth(self, header_value):
        req = urllib.request.Request(
            self.base + "/v1/leases",
            data=json.dumps({"request_id": "q"}).encode(),
            headers={"Authorization": header_value, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_unknown_route(self):
        for method, path in (("GET", "/nope"), ("POST", "/v1/nope"),
                             ("GET", "/v1/leases"), ("POST", "/v1/board")):
            with self.subTest(method=method, path=path):
                status, payload = self.call(self.base, method, path, {"request_id": "q"})
                self.assertEqual(status, 404)
                self.assertEqual(self.body_of(payload)["error"], "not_found")

    def test_query_ignored(self):
        status, _ = self.call(
            self.base, "POST", "/v1/leases?x=1",
            {"worker": "w", "role": "r", "agent": "a", "instance": "i", "request_id": "q"},
        )
        self.assertEqual(status, 201)

    def test_lease_routes_reach_service(self):
        for action, code in (("start", 200), ("exhausted", 200), ("renew", 200),
                             ("release", 204), ("usage", 204)):
            with self.subTest(action=action):
                status, _ = self.call(
                    self.base, "POST", "/v1/leases/L9/" + action, {"request_id": "q"}
                )
                self.assertEqual(status, code)
                self.assertEqual(self.fake.calls[-1][1:3], ("worker-a", "L9"))

    def test_204_empty(self):
        status, payload = self.call(
            self.base, "POST", "/v1/leases/L1/release", {"request_id": "q"}
        )
        self.assertEqual(status, 204)
        self.assertEqual(payload, b"")

    def test_api_error_mapping(self):
        cases = [
            (errors.client_mismatch(), 403, "client_mismatch"),
            (errors.not_owner(), 403, "not_owner"),
            (errors.not_found(), 404, "not_found"),
            (errors.lease_not_active(), 409, "lease_not_active"),
            (errors.lease_not_confirmed(), 409, "lease_not_confirmed"),
            (errors.bad_request(missing=["worker"]), 400, "bad_request"),
        ]
        for exc, code, err in cases:
            with self.subTest(code=err):
                self.fake.raise_for["renew"] = exc
                status, payload = self.call(
                    self.base, "POST", "/v1/leases/L1/renew", {"request_id": "q"}
                )
                self.assertEqual(status, code)
                data = self.body_of(payload)
                self.assertEqual(data["error"], err)
                if err == "bad_request":
                    self.assertEqual(data["missing"], ["worker"])
        del self.fake.raise_for["renew"]

    def test_refused(self):
        self.fake.raise_for["request_lease"] = errors.refusal(
            "all_cooling", "2026-10-11T03:47:00Z"
        )
        status, payload = self.call(self.base, "POST", "/v1/leases", {"request_id": "q"})
        self.assertEqual(status, 409)
        data = self.body_of(payload)
        self.assertEqual(data["error"], "refused")
        self.assertEqual(data["reason"], "all_cooling")
        self.assertEqual(data["earliest_reset"], "2026-10-11T03:47:00Z")

    def test_refused_unknown_reason_passthrough(self):
        self.fake.raise_for["request_lease"] = errors.ApiError(
            409, "refused", reason="future_reason"
        )
        status, payload = self.call(self.base, "POST", "/v1/leases", {"request_id": "q"})
        self.assertEqual(status, 409)
        self.assertEqual(self.body_of(payload)["reason"], "future_reason")

    def test_board_ok(self):
        status, payload = self.call(self.base, "GET", "/v1/board")
        self.assertEqual(status, 200)
        data = self.body_of(payload)
        self.assertEqual(data["subscriptions"][0]["name"], "claude-personal")

    def test_board_needs_auth(self):
        status, _ = self.call(self.base, "GET", "/v1/board", token=None)
        self.assertEqual(status, 401)

    def test_whoami_ok(self):
        status, payload = self.call(self.base, "GET", "/v1/whoami")
        self.assertEqual(status, 200)
        self.assertEqual(self.body_of(payload), {"client": "worker-a", "role": "client"})
        self.assertFalse(self.fake.calls)  # 不碰 service、不碰租約

    def test_whoami_401(self):
        status, _ = self.call(self.base, "GET", "/v1/whoami", token=None)
        self.assertEqual(status, 401)
        status, _ = self._raw_auth("Bearer nope")
        self.assertEqual(status, 401)

    def test_unexpected_500(self):
        self.fake.raise_for["board"] = RuntimeError("boom")
        status, payload = self.call(self.base, "GET", "/v1/board")
        self.assertEqual(status, 500)
        self.assertEqual(self.body_of(payload), {"error": "internal"})

    def test_log_clean(self):
        secret = "s3cr3t-value-zz9"
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            self.call(
                self.base, "POST", "/v1/leases",
                {"purpose": secret, "request_id": "q"},
                token="Bearer-" + secret,
            )
        text = "\n".join(logs.output)
        self.assertNotIn(secret, text)  # 標頭與 body 都不進 log
        self.assertIn("POST /v1/leases", text)  # 方法、路徑、狀態碼有記
        self.assertIn("401", text)

    def test_body_too_large(self):
        # 宣告 2 MB，只送 2 bytes：服務不讀 body，直接 413。
        host, port = self.base.rsplit(":", 1)
        conn = http.client.HTTPConnection(host.rsplit("/", 1)[-1], int(port), timeout=5)
        try:
            conn.putrequest("POST", "/v1/leases")
            conn.putheader("Authorization", "Bearer " + GOOD_TOKEN)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(2 * 1024 * 1024))
            conn.endheaders()
            conn.send(b"{}")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["error"], "body_too_large")
            self.assertEqual(data["api_version"], 1)
        finally:
            conn.close()
        self.assertFalse(self.fake.calls)  # 沒進 service

    def test_body_exactly_1mb_ok(self):
        status, _ = self.call(self.base, "POST", "/v1/leases", raw=b" " * (1024 * 1024))
        self.assertEqual(status, 201)  # 邊界：超過才擋，假 service 照常回

    def test_500_logs_kind_not_value(self):
        secret = "CANARY-500-zz9"
        self.fake.raise_for["board"] = ValueError(secret)
        try:
            with self.assertLogs("lgwd.http", level="INFO") as logs:
                status, payload = self.call(self.base, "GET", "/v1/board")
        finally:
            del self.fake.raise_for["board"]
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(payload.decode())["error"], "internal")
        text = "\n".join(logs.output)
        self.assertIn("ValueError", text)  # 只記種類
        self.assertNotIn(secret, text)


class BoardServerTest(ServerCase):
    def setUp(self):
        self.fake = FakeService()
        self.base = self.serve(make_board_server(self.fake, HOST, 0))

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), ""

    def test_host_rejected(self):
        for host in ("0.0.0.0", "", "192.168.1.5", "::1", "localhost"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    make_board_server(self.fake, host, 0)

    def test_board_ok_no_auth(self):
        status, payload, ctype = self.get("/board")  # 不帶憑證也看得到
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        text = payload.decode()
        self.assertIn("claude-personal", text)
        self.assertIn("worker-a", text)

    def test_board_escapes(self):
        evil = '<script>alert("x")</script>'
        sub = dict(
            BOARD["subscriptions"][0],
            active=[{"worker": evil, "role": evil, "agent": evil, "since": evil}],
        )
        self.fake.board = lambda: dict(BOARD, subscriptions=[sub])  # 只換這台假資料
        status, payload, _ = self.get("/board")
        self.assertEqual(status, 200)
        text = payload.decode()
        self.assertNotIn(evil, text)
        self.assertIn("&lt;script&gt;", text)

    def test_board_404(self):
        for method, path in (("GET", "/nope"), ("POST", "/board"), ("GET", "/v1/board")):
            with self.subTest(method=method, path=path):
                status, _, _ = self.get(path, method)
                self.assertEqual(status, 404)

    def test_board_pending(self):
        sub = dict(BOARD["subscriptions"][0], pending=2)
        self.fake.board = lambda: dict(BOARD, subscriptions=[sub])
        status, payload, _ = self.get("/board")
        self.assertEqual(status, 200)
        text = payload.decode()
        self.assertIn("未確認", text)
        self.assertIn("2", text)


if __name__ == "__main__":
    unittest.main()
