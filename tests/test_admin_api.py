import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from lgwd.clock import FakeClock
from lgwd.errors import ApiError
from lgwd.http import make_api_server
from lgwd.keys import KeyStore
from lgwd.leasing import LeaseService
from lgwd.policy import load_policy

POLICY = os.path.join(os.path.dirname(__file__), "..", "policy", "default.json")
KEYS = (
    "CLAUDE_PERSONAL_OAUTH_TOKEN=CANARY-admin-personal\n"
    "CLAUDE_COMPANY_OAUTH_TOKEN=CANARY-admin-company\n"
    "OPENROUTER_API_KEY=x\nOPENCODE_ZEN_API_KEY=y\nOLLAMA_API_KEY=z\n"
)


class AdminApiServiceTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        kp = os.path.join(d, "keys.env")
        with open(kp, "w") as f:
            f.write(KEYS)
        self.svc = LeaseService(os.path.join(d, "lgw.db"), load_policy(POLICY), KeyStore(kp), FakeClock())
        self.issuer_token = self.svc.add_client("mlp-issuer", role="issuer")
        self.mac_token = self.svc.add_client("mac-test")

    def lease(self, client, instance="i1"):
        return self.svc.request_lease(client, {"worker": client, "role": "r", "agent": "claude-code",
                                               "instance": instance, "request_id": f"rq-{client}-{instance}"})

    def test_issue_worker_client_can_lease(self):
        status, body = self.svc.admin_issue("mlp-issuer", {"name": "worker-a1"})
        self.assertEqual(status, 201)
        self.assertEqual(self.svc.authenticate(body["token"]), "worker-a1")
        self.assertEqual(self.svc.role_of("worker-a1"), "client")
        self.assertEqual(self.lease("worker-a1")[0], 201)

    def test_reissue_revokes_old_token_and_leases(self):
        _, first = self.svc.admin_issue("mlp-issuer", {"name": "worker-a1"})
        _, lease = self.lease("worker-a1")
        self.svc.start("worker-a1", lease["lease"]["lease_id"], {"request_id": "s1"})
        _, second = self.svc.admin_issue("mlp-issuer", {"name": "worker-a1"})
        self.assertIsNone(self.svc.authenticate(first["token"]))
        self.assertEqual(self.svc.authenticate(second["token"]), "worker-a1")
        board = self.svc.board()
        personal = next(s for s in board["subscriptions"] if s["name"] == "claude-personal")
        self.assertEqual(personal["count"], 0)

    def test_name_outside_worker_range_rejected(self):
        for name in ("mac-test", "mlp-issuer", "worker-", "worker-a b", "Worker-a1", "worker-" + "x" * 58, "../worker-a"):
            with self.assertRaises(ApiError) as ctx:
                self.svc.admin_issue("mlp-issuer", {"name": name})
            self.assertEqual(ctx.exception.code, "name_not_allowed", name)
        self.assertIsNotNone(self.svc.authenticate(self.mac_token))

    def test_revoke(self):
        _, body = self.svc.admin_issue("mlp-issuer", {"name": "worker-a1"})
        self.assertEqual(self.svc.admin_revoke("mlp-issuer", "worker-a1"), (204, {}))
        self.assertIsNone(self.svc.authenticate(body["token"]))
        with self.assertRaises(ApiError) as ctx:
            self.svc.admin_revoke("mlp-issuer", "worker-a1")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ApiError) as ctx:
            self.svc.admin_revoke("mlp-issuer", "worker-never")
        self.assertEqual(ctx.exception.status, 404)

    def test_roles_are_separated(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.admin_issue("mac-test", {"name": "worker-a1"})
        self.assertEqual(ctx.exception.code, "forbidden_role")
        with self.assertRaises(ApiError) as ctx:
            self.svc.request_lease("mlp-issuer", {"worker": "mlp-issuer", "role": "r", "agent": "claude-code",
                                                  "instance": "i", "request_id": "x"})
        self.assertEqual(ctx.exception.code, "forbidden_role")

    def test_issuer_survives_restart(self):
        db = self.svc._store  # rebuild from events keeps role
        db.rebuild()
        self.assertEqual(self.svc.role_of("mlp-issuer"), "issuer")


class AdminApiHttpTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        kp = os.path.join(d, "keys.env")
        with open(kp, "w") as f:
            f.write(KEYS)
        self.svc = LeaseService(os.path.join(d, "lgw.db"), load_policy(POLICY), KeyStore(kp), FakeClock())
        self.issuer = self.svc.add_client("mlp-issuer", role="issuer")
        self.mac = self.svc.add_client("mac-test")
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()

    def serve(self, host):
        srv = make_api_server(self.svc, host, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        return srv.server_address[1]

    def call(self, port, method, path, token, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})

    def test_issue_and_revoke_over_loopback(self):
        port = self.serve("127.0.0.1")
        status, body = self.call(port, "POST", "/v1/admin/clients", self.issuer, {"name": "worker-a1"})
        self.assertEqual(status, 201)
        token = body["token"]
        self.assertEqual(self.call(port, "GET", "/v1/whoami", token)[1]["client"], "worker-a1")
        self.assertEqual(self.call(port, "DELETE", "/v1/admin/clients/worker-a1", self.issuer)[0], 204)
        self.assertEqual(self.call(port, "GET", "/v1/whoami", token)[0], 401)

    def test_admin_refused_when_not_bound_to_loopback(self):
        port = self.serve("0.0.0.0")
        status, body = self.call(port, "POST", "/v1/admin/clients", self.issuer, {"name": "worker-a1"})
        self.assertEqual((status, body.get("error")), (403, "forbidden_role"))

    def test_roles_over_http(self):
        port = self.serve("127.0.0.1")
        self.assertEqual(self.call(port, "POST", "/v1/admin/clients", self.mac, {"name": "worker-a1"})[1]["error"], "forbidden_role")
        self.assertEqual(self.call(port, "GET", "/v1/board", self.issuer)[1]["error"], "forbidden_role")
        self.assertEqual(self.call(port, "GET", "/v1/whoami", self.issuer)[1]["role"], "issuer")


if __name__ == "__main__":
    unittest.main()
