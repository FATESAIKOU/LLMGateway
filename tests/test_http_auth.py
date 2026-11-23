"""`lgwd/http.py` 與 `lgwd/auth.py` 的整合測試（#222 D2 PR-6a；C-601、C-603、C-604、
C-607、C-609；W-12、W-14、W-15、W-19 的 HTTP 面）。

用真的 loopback、真的暫存金鑰、真的 `ssh-keygen`、真的兩庫；service 是
`tests/test_http.py` 的 `FakeService`（會記下呼叫參數，用來證明 service 沒收到
principal、也證明拒絕時 service 呼叫數 0）。

不碰 `/var/lib`、`/etc`、不讀憑證、不連真機。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from lgwd import auth
from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths, now_seconds
from lgwd.clock import FakeClock
from lgwd.http import make_api_server
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer, key_entry, registry_bytes
from tests.test_http import FakeService
from tests.test_http_framing import exchange, normalize, parse_response, req

HOST = "127.0.0.1"
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
COOLDOWN = "/v1/subscriptions/claude-personal/cooldown"
JSON_BODY = b'{"until": "2026-10-11T15:00:00Z"}'
UNAUTHORIZED = b'{"error": "unauthorized"}'
HEALTHZ = b'{"status": "ok"}'


class CountingGate:
    """把 `authenticate` 計數後交給真的 gate（C-601／C-609：healthz 呼叫數 0）。"""

    def __init__(self, real):
        self.real = real
        self.mode = real.mode
        self.calls = 0

    def authenticate(self, **kwargs):
        self.calls += 1
        return self.real.authenticate(**kwargs)


class NullGate:
    """永遠不放過的 gate：如果它被呼叫，回應就不會是 200。"""

    def __init__(self, mode="enforce"):
        self.mode = mode
        self.calls = 0

    def authenticate(self, **kwargs):
        self.calls += 1
        return auth.unavailable(auth.REASON_BUSY)


class HttpAuthCase(unittest.TestCase):
    signer: Signer
    signer_b: Signer

    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = Signer(cls._keys.name, "k-a")
        cls.signer_b = Signer(cls._keys.name, "k-b")

    @classmethod
    def tearDownClass(cls):
        cls._keys.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir)
        self.paths = auth_paths(
            self.state_dir, registry=os.path.join(self.dir, "auth-registry.json"))
        create_stores(self.paths.trust_db, self.paths.replay_db)
        self.clock = FakeClock(NOW)
        self.write_registry(self.registry_data(1))
        self.servers = []

    # --- 工具 ---

    def registry_data(self, generation: int, entries=None) -> bytes:
        return registry_bytes(
            generation, entries if entries is not None
            else [key_entry("k-a", self.signer), key_entry("k-b", self.signer_b)])

    def write_registry(self, raw: bytes) -> None:
        write_registry_file(self.paths.registry, raw)

    def config(self, mode: str = "enforce", **over) -> auth.AuthConfig:
        kwargs = dict(
            mode=mode, registry_path=self.paths.registry, state_dir=self.state_dir,
            per_key_max=600, poll_interval=0.0)
        kwargs.update(over)
        return auth.AuthConfig(**kwargs)

    def open_runtime(self, mode: str = "enforce", **over):
        runtime = auth.open_runtime(
            self.config(mode, **over), self.clock,
            owner_uid=0, registry_owner_uid=os.getuid())
        self.addCleanup(runtime.stop)
        return runtime

    def serve(self, gate, service=None):
        service = service if service is not None else FakeService()
        server = make_api_server(service, HOST, 0, gate=gate)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.addCleanup(self._stop, server, thread)
        return server, service, server.server_address[1]

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def now(self) -> int:
        return now_seconds(self.clock)

    def sign(self, *, key_id="k-a", signer=None, method="GET", target="/v1/subscriptions",
             body=b"", timestamp=None):
        signer = signer or self.signer
        return signer.headers(
            key_id=key_id, method=method, target=target, body=body,
            timestamp=self.now() if timestamp is None else timestamp)

    def call(self, port, method, target, headers=(), body=b""):
        extra = list(headers)
        if body and not any(name.lower() == "content-length" for name, _ in extra):
            extra.append(("Content-Length", str(len(body))))
        raw = req(method, target, extra, body)
        return parse_response(exchange(port, raw))

    def enforce_server(self, **over):
        runtime = self.open_runtime("enforce", **over)
        server, service, port = self.serve(runtime.gate)
        return runtime, service, port


class C601RoutingTests(HttpAuthCase):
    """C-601：原文 → 路由 → `/healthz` → framing → gate → service。"""

    def test_C601_healthz_never_reaches_the_gate(self):
        real = self.open_runtime()
        gate = CountingGate(real.gate)
        server, service, port = self.serve(gate)
        requests = {
            "plain": req("GET", "/healthz"),
            "te": req("GET", "/healthz", [("Transfer-Encoding", "chunked")]),
            "dup": req("GET", "/healthz",
                       [("X-LGW-Key-Id", "a"), ("X-LGW-Key-Id", "b")]),
            "signed": req("GET", "/healthz", self.sign(target="/healthz")),
            "unknown": req("GET", "/healthz", self.sign(key_id="k-unknown")),
        }
        for name, raw in requests.items():
            with self.subTest(case=name):
                status, body, headers = parse_response(exchange(port, raw))
                self.assertEqual((status, body), (200, HEALTHZ))
                self.assertNotIn("www-authenticate", headers)
        self.assertEqual(gate.calls, 0)
        self.assertEqual(service.calls, [])

    def test_C601_unknown_route_is_404_before_auth(self):
        runtime, service, port = self.enforce_server()
        status, body, _ = self.call(port, "GET", "/v1/nope", self.sign(target="/v1/nope"))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body.decode())["error"], "not_found")
        self.assertEqual(service.calls, [])
        self.assertEqual(runtime.replay.row_count(), 0, msg="404 不得寫 nonce")

    def test_C601_healthz_is_exempt_even_when_not_authenticated(self):
        runtime, service, port = self.enforce_server()
        status, body, _ = self.call(port, "GET", "/healthz")
        self.assertEqual((status, body), (200, HEALTHZ))
        self.assertEqual(runtime.gate.authenticate_calls, 0)


class C603EnforceEndToEndTests(HttpAuthCase):
    """C-603：五條業務路由的合法簽章與各種拒絕（service 呼叫數 0）。"""

    def test_C603_five_routes_pass_with_a_signed_request(self):
        runtime, service, port = self.enforce_server()
        cases = (
            ("GET", "/v1/subscriptions", b"", 200),
            ("GET", "/v1/usage", b"", 200),
            ("GET", "/v1/resolve?agent=opencode&provider=opencode-zen", b"", 200),
            ("GET", "/v1/roles/impl?agent=opencode", b"", 200),
            ("POST", COOLDOWN, JSON_BODY, 204),
        )
        for method, target, body, expected in cases:
            with self.subTest(target=target):
                headers = self.sign(method=method, target=target, body=body)
                status, _, _ = self.call(port, method, target, headers, body)
                self.assertEqual(status, expected)
        self.assertEqual(len(service.calls), 5)
        # C-608／W-12：service 的呼叫參數裡沒有 principal、也沒有任何新欄位。
        for call in service.calls:
            self.assertNotIn("w1-worker", repr(call))
        self.assertEqual(service.calls[0], ("list_subscriptions",))
        self.assertEqual(service.calls[4],
                         ("register_cooldown", "claude-personal",
                          {"until": "2026-10-11T15:00:00Z"}))

    def test_C603_unsigned_401_body_is_identical_and_no_www_authenticate(self):
        runtime, service, port = self.enforce_server()
        bodies = set()
        for method, target, body in (
                ("GET", "/v1/subscriptions", b""),
                ("GET", "/v1/usage", b""),
                ("GET", "/v1/resolve?agent=opencode&provider=opencode-zen", b""),
                ("GET", "/v1/roles/impl?agent=opencode", b""),
                ("POST", COOLDOWN, JSON_BODY)):
            with self.subTest(target=target):
                status, payload, headers = self.call(port, method, target, (), body)
                self.assertEqual(status, 401)
                self.assertNotIn("www-authenticate", headers)
                bodies.add(payload)
        self.assertEqual(bodies, {UNAUTHORIZED})
        self.assertEqual(service.calls, [])

    def test_C603_wrong_signature_and_unknown_key(self):
        runtime, service, port = self.enforce_server()
        wrong = self.sign(signer=self.signer_b)       # key B 的簽章冒充 k-a
        status, body, _ = self.call(port, "GET", "/v1/subscriptions", wrong)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        unknown = self.sign(key_id="k-unknown")
        status, body, _ = self.call(port, "GET", "/v1/subscriptions", unknown)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(service.calls, [])

    def test_C603_changed_query_after_signing(self):
        runtime, service, port = self.enforce_server()
        headers = self.sign(target="/v1/resolve?agent=opencode&provider=opencode-zen")
        status, body, _ = self.call(
            port, "GET", "/v1/resolve?agent=agy&provider=opencode-zen", headers)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(service.calls, [])

    def test_C603_changed_body_after_signing(self):
        runtime, service, port = self.enforce_server()
        headers = self.sign(method="POST", target=COOLDOWN, body=JSON_BODY)
        changed = b'{"until": "2026-10-12T15:00:00Z"}'
        status, body, _ = self.call(port, "POST", COOLDOWN, headers, changed)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(service.calls, [])

    def test_C603_signature_is_bound_to_the_method(self):
        """「POST 簽章送 GET」：八行第 6 行不同就驗不過（gate 層，路由不參與）。"""
        runtime = self.open_runtime()
        headers = self.sign(method="POST", target="/v1/subscriptions")
        from tests.test_auth_gate import Headers, make_framing
        verdict = runtime.gate.authenticate(
            method="GET", framing=make_framing(), headers=Headers(headers),
            body=b"")
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_DENY, auth.REASON_BAD_SIGNATURE))

    def test_C603_expired_and_revoked(self):
        runtime, service, port = self.enforce_server()
        expired = self.sign(timestamp=self.now() - 61)
        status, _, _ = self.call(port, "GET", "/v1/subscriptions", expired)
        self.assertEqual(status, 401)
        # 撤銷：新世代把 k-a 標 revoked。
        self.write_registry(self.registry_data(
            2, [key_entry("k-a", self.signer, status="revoked"),
                key_entry("k-b", self.signer_b)]))
        runtime.watcher.tick()
        status, body, _ = self.call(port, "GET", "/v1/subscriptions", self.sign())
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        status, _, _ = self.call(port, "GET", "/v1/subscriptions",
                                 self.sign(key_id="k-b", signer=self.signer_b))
        self.assertEqual(status, 200)
        self.assertEqual(len(service.calls), 1)

    def test_C603_replay_rejected_including_after_restart(self):
        runtime, service, port = self.enforce_server()
        headers = self.sign(target="/v1/usage")
        status, _, _ = self.call(port, "GET", "/v1/usage", headers)
        self.assertEqual(status, 200)
        status, body, _ = self.call(port, "GET", "/v1/usage", headers)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(len(service.calls), 1)
        runtime.stop()
        restarted = self.open_runtime()
        server, service2, port2 = self.serve(restarted.gate)
        status, body, _ = self.call(port2, "GET", "/v1/usage", headers)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(service2.calls, [])

    def test_C603_429_retry_after_then_new_signature_passes(self):
        runtime, service, port = self.enforce_server(per_key_max=1)
        first = self.sign(target="/v1/usage")
        status, _, _ = self.call(port, "GET", "/v1/usage", first)
        self.assertEqual(status, 200)
        rejected = self.sign(target="/v1/subscriptions")
        status, body, headers = self.call(port, "GET", "/v1/subscriptions", rejected)
        self.assertEqual((status, json.loads(body.decode())["error"]),
                         (429, "rate_limited"))
        self.assertEqual(headers.get("retry-after"), "66")
        # 等 Retry-After 秒：清除後以新 nonce、新 timestamp 重簽可通過。
        self.clock.advance(seconds=66)
        runtime.replay.purge_once()
        fresh = self.sign(target="/v1/subscriptions")
        status, _, _ = self.call(port, "GET", "/v1/subscriptions", fresh)
        self.assertEqual(status, 200)
        # 被 429 拒絕的那一份日後重送：已離開時窗（401），不會第二次執行。
        status, _, _ = self.call(port, "GET", "/v1/subscriptions", rejected)
        self.assertEqual(status, 401)
        self.assertEqual(len(service.calls), 2)

    def test_C603_quota_of_one_key_does_not_affect_another(self):
        runtime, service, port = self.enforce_server(per_key_max=1)
        self.assertEqual(
            self.call(port, "GET", "/v1/usage", self.sign(target="/v1/usage"))[0], 200)
        self.assertEqual(
            self.call(port, "GET", "/v1/usage",
                      self.sign(target="/v1/usage", key_id="k-a"))[0], 429)
        status, _, _ = self.call(port, "GET", "/v1/usage",
                                 self.sign(target="/v1/usage", key_id="k-b",
                                           signer=self.signer_b))
        self.assertEqual(status, 200)
        self.assertEqual(len(service.calls), 2)

    def test_C603_permit_exhaustion_is_503_and_healthz_still_ok(self):
        """W-14：32 個名額用盡，第 33 個 enforce 503 `busy`；`/healthz` 照常。"""
        runtime, service, port = self.enforce_server()
        server, _ = next((item for item in self.servers if item[0].server_address[1] == port),
                         (None, None))
        held = 0
        while server.framing_semaphore.acquire(blocking=False):
            held += 1
        self.assertEqual(held, 32)
        try:
            status, body, _ = self.call(port, "GET", "/v1/subscriptions", self.sign())
            status_h, body_h, _ = self.call(port, "GET", "/healthz")
        finally:
            for _ in range(held):
                server.framing_semaphore.release()
        self.assertEqual((status, body), (503, b'{"error": "auth_unavailable"}'))
        self.assertEqual((status_h, body_h), (200, HEALTHZ))
        self.assertEqual(service.calls, [])


class C603WarnTests(HttpAuthCase):
    """C-603：warn 記 would-deny 但照舊處理業務；驗章不可用也不改業務結果。"""

    def test_C603_warn_unsigned_serves_and_logs_would_deny(self):
        runtime = self.open_runtime("warn")
        server, service, port = self.serve(runtime.gate)
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, _, _ = self.call(port, "GET", "/v1/subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(service.calls, [("list_subscriptions",)])
        text = "\n".join(logs.output)
        self.assertIn("mode=warn", text)
        self.assertIn("outcome=deny", text)
        self.assertIn("reason=missing", text)
        self.assertIn("mode=warn", text)

    def test_C603_warn_verifier_unavailable_still_serves(self):
        runtime = self.open_runtime("warn")
        runtime.gate.fail_publish(runtime.gate.snapshot()[0])
        server, service, port = self.serve(runtime.gate)
        status, _, _ = self.call(port, "GET", "/v1/usage", self.sign(target="/v1/usage"))
        self.assertEqual(status, 200)
        self.assertEqual(service.calls, [("list_usage_summary",)])


class PermitScopeTests(HttpAuthCase):
    """C-106／REPORT-D2c-arch §2.2：讀 body＋驗章（含 accept）都在同一個 framing 號誌內。"""

    def test_gate_runs_while_a_framing_permit_is_held(self):
        runtime = self.open_runtime()
        observed = {}

        class ProbeGate:
            mode = "enforce"

            def __init__(self, real):
                self.real = real
                self.sem = None

            def authenticate(self, **kwargs):
                got = 0
                while self.sem.acquire(blocking=False):
                    got += 1
                for _ in range(got):
                    self.sem.release()
                observed["free"] = got
                return self.real.authenticate(**kwargs)

        probe = ProbeGate(runtime.gate)
        server, service, port = self.serve(probe)
        probe.sem = server.framing_semaphore
        status, _, _ = self.call(port, "GET", "/v1/usage",
                                 self.sign(target="/v1/usage"))
        self.assertEqual(status, 200)
        self.assertEqual(observed["free"], 31,
                         msg="gate 執行時必須已經佔住一個 framing 名額（共 32）")


class C601NoAuthOutsidePermitOrFramingTests(HttpAuthCase):
    """中-1（C-601／C-106）：號誌取不到或 framing 已判不合時，不在號誌外驗章、不記第二筆事件。"""

    @staticmethod
    def auth_lines(logs):
        return [line for line in logs.output if "auth mode=" in line]

    def test_warn_without_permit_does_not_authenticate_and_logs_busy_once(self):
        runtime = self.open_runtime("warn")
        gate = CountingGate(runtime.gate)
        server, service, port = self.serve(gate)
        held = 0
        while server.framing_semaphore.acquire(blocking=False):
            held += 1
        self.assertEqual(held, 32)
        before = gate.calls
        try:
            with self.assertLogs("lgwd.http", level="INFO") as logs:
                status, _, _ = self.call(port, "GET", "/v1/subscriptions")
        finally:
            for _ in range(held):
                server.framing_semaphore.release()
        self.assertEqual(status, 200)
        self.assertEqual(service.calls, [("list_subscriptions",)])
        self.assertEqual(gate.calls - before, 0, msg="號誌取不到時不得在號誌外驗章")
        lines = self.auth_lines(logs)
        self.assertEqual(len(lines), 1, msg="號誌取不到只記一筆不可判定")
        self.assertIn("outcome=unavailable", lines[0])
        self.assertIn("reason=busy", lines[0])
        self.assertEqual(runtime.replay.row_count(), 0, msg="不得 INSERT nonce")

    def test_warn_framing_deny_does_not_authenticate_and_logs_once(self):
        runtime = self.open_runtime("warn")
        gate = CountingGate(runtime.gate)
        server, service, port = self.serve(gate)
        dup = [("X-LGW-Key-Id", "a"), ("X-LGW-Key-Id", "b")]
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, _, _ = self.call(port, "GET", "/v1/subscriptions", dup)
        self.assertEqual(status, 200)
        self.assertEqual(service.calls, [("list_subscriptions",)])
        self.assertEqual(gate.calls, 0, msg="framing 已判不合時不得再驗章")
        lines = self.auth_lines(logs)
        self.assertEqual(len(lines), 1, msg="framing 只記一筆 deny")
        self.assertIn("mode=warn", lines[0])
        self.assertIn("outcome=deny", lines[0])
        self.assertIn("reason=framing", lines[0])


class S4PermitReturnTests(HttpAuthCase):
    """S4（agy 審查）：每一個退出路徑都要歸還 framing 號誌（單一 server 連打 > 32 筆）。"""

    def test_S4_all_exit_paths_return_their_permit(self):
        runtime = self.open_runtime("warn")

        class FlakyGate:
            mode = "warn"

            def __init__(self, real):
                self.real = real
                self.boom = False

            def authenticate(self, **kwargs):
                if self.boom:
                    raise RuntimeError("inject")   # 驗章例外：逸出 handler
                return self.real.authenticate(**kwargs)

        gate = FlakyGate(runtime.gate)
        server, service, port = self.serve(gate)
        dup = [("X-LGW-Key-Id", "a"), ("X-LGW-Key-Id", "b")]
        # 1) framing 不合（warn 記 would-deny 後照舊服務）×20
        for _ in range(20):
            status, _, _ = self.call(port, "GET", "/v1/subscriptions", dup)
            self.assertEqual(status, 200)
        # 2) 讀 body 逾時（warn 記不可判定並關閉連線）×1
        with mock.patch("lgwd.http._READ_DEADLINE_SECONDS", 0.2):
            raw = req("POST", COOLDOWN, [("Content-Length", "8")]) + b"{"
            exchange(port, raw)
        # 3) 驗章例外（逸出 handler；finally 必須歸還號誌）×1
        gate.boom = True
        exchange(port, req("GET", "/v1/usage"))
        gate.boom = False
        # 4) 正常（warn 缺簽章 → 記 deny 後照舊服務）×15
        for _ in range(15):
            status, _, _ = self.call(port, "GET", "/v1/usage")
            self.assertEqual(status, 200)
        # 全部退出路徑走過後，32 個名額必須全數歸還（拿不滿就是洩漏）。
        held = 0
        try:
            while server.framing_semaphore.acquire(blocking=False):
                held += 1
        finally:
            for _ in range(held):
                server.framing_semaphore.release()
        self.assertEqual(held, 32, msg="有退出路徑沒有歸還 framing 號誌")
        # 再打一筆也不會 busy（名額真的可用，不只是計數對）。
        status, _, _ = self.call(port, "GET", "/v1/usage", dup)
        self.assertEqual(status, 200, msg="後續請求不得 busy")


class C604AndC607HttpTests(HttpAuthCase):
    """C-604（HTTP 面）與 C-607（日誌、off 一行、不洩密）。"""

    def test_C604_401_has_no_www_authenticate_and_identical_bytes(self):
        runtime, service, port = self.enforce_server()
        responses = set()
        for headers in (
                (),
                self.sign(signer=self.signer_b),
                self.sign(key_id="k-unknown"),
                self.sign(timestamp=self.now() - 61),
                self.sign(target="/v1/resolve?agent=opencode"),
        ):
            raw = req("GET", "/v1/resolve?agent=opencode&provider=opencode-zen", list(headers))
            response = normalize(exchange(port, raw))
            head, _, body = response.partition(b"\r\n\r\n")
            responses.add(body)
            self.assertNotIn(b"www-authenticate", head.lower())
        self.assertEqual(responses, {UNAUTHORIZED})
        self.assertEqual(service.calls, [])

    def test_C607_off_logs_only_the_startup_line(self):
        service = FakeService()
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            server = make_api_server(service, HOST, 0)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            self.addCleanup(self._stop, server, thread)
            port = server.server_address[1]
            for _ in range(3):
                exchange(port, req("GET", "/v1/subscriptions"))
                exchange(port, req("GET", "/v1/nope"))
        text = "\n".join(logs.output)
        self.assertEqual(text.count("auth mode=off"), 1)
        self.assertNotIn("outcome=", text)
        self.assertNotIn("reason=", text)

    def test_C607_logs_never_contain_secrets_or_classifications(self):
        runtime, service, port = self.enforce_server(per_key_max=1)
        public_b64 = self.signer.public_key_line.split()[1]
        good = self.sign(target="/v1/usage")
        nonce_value = dict(good)["X-LGW-Nonce"]
        signature = dict(good)["X-LGW-Signature"]
        secret_query = "TOPSECRETQUERY"
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            self.call(port, "GET", "/v1/usage", good)                  # pass
            self.call(port, "GET", "/v1/usage", good)                  # replay
            self.call(port, "GET", "/v1/resolve?agent=%s" % secret_query)  # missing
            self.call(port, "GET", "/v1/subscriptions", self.sign())   # 429
            self.call(port, "POST", COOLDOWN + "?q=" + secret_query,
                      [("Content-Length", "5")], b"{nope")             # framing／400
            self.call(port, "GET", "//v1/resolve?q=" + secret_query)   # target 原文
        text = "\n".join(logs.output)
        for secret in (public_b64, signature, nonce_value, secret_query,
                       "k-a", "Reject", "Outcome.", "incorrect signature",
                       "Could not verify", "signature_not_canonical", "unavailable\"",
                       "bad_signature\n"):
            self.assertNotIn(secret, text, msg="日誌不得含 {0!r}".format(secret))
        self.assertIn("outcome=", text)


class C609HealthzBytesTests(HttpAuthCase):
    """C-609／W-15：三種模式的 `/healthz` bytes 與 D1 相同（扣除 Date／Server）。

    每個模式各自一份狀態目錄：一份 stores 只能有一個 lgwd（`flock` 禁止多實例），
    所以不能在同一個狀態目錄上開兩個 runtime。
    """

    def make_paths(self):
        root = tempfile.mkdtemp(dir=self.dir)
        state = os.path.join(root, "state")
        os.makedirs(state)
        paths = auth_paths(state, registry=os.path.join(root, "registry.json"))
        create_stores(paths.trust_db, paths.replay_db)
        write_registry_file(paths.registry, self.registry_data(1))
        return paths

    def open_on(self, paths, mode="enforce"):
        runtime = auth.open_runtime(
            auth.AuthConfig(mode=mode, registry_path=paths.registry,
                            state_dir=paths.state_dir, poll_interval=0.0),
            self.clock, owner_uid=0, registry_owner_uid=os.getuid())
        self.addCleanup(runtime.stop)
        return runtime

    def d1_bytes(self, requests):
        server = make_api_server(FakeService(), HOST, 0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        port = server.server_address[1]
        return {name: normalize(exchange(port, raw)) for name, raw in requests.items()}

    def test_C609_three_modes_are_identical(self):
        requests = {
            "plain": req("GET", "/healthz"),
            "signed_unknown": req("GET", "/healthz", self.sign(key_id="k-unknown")),
            "signed_garbage": req("GET", "/healthz", [
                ("X-LGW-Auth-Version", "1"), ("X-LGW-Key-Id", "k-a"),
                ("X-LGW-Timestamp", "1"), ("X-LGW-Nonce", "x"),
                ("X-LGW-Signature", "y" * 240)]),
            "dup_header": req("GET", "/healthz",
                              [("X-LGW-Signature", "a"), ("X-LGW-Signature", "b")]),
            "te": req("GET", "/healthz", [("Transfer-Encoding", "chunked")]),
            "post": req("POST", "/healthz", [("Content-Length", "2")], b"{}"),
            # 中-3：非 origin-form 的 /healthz 也是健康請求（spec「以 // 開頭的路徑」
            # 三模式 bytes 相同；絕對 URI 的 path 同樣是 /healthz）。
            "double_slash": req("GET", "//healthz"),
            "double_slash_query": req("GET", "//healthz?x=1"),
            "absolute_uri": req("GET", "http://h/healthz"),
        }
        baseline = self.d1_bytes(requests)

        warn_runtime = self.open_on(self.make_paths(), "warn")
        enforce_runtime = self.open_on(self.make_paths(), "enforce")
        broken_paths = self.make_paths()
        broken = self.open_on(broken_paths, "enforce")
        broken.gate.fail_publish(broken.gate.snapshot()[0])
        degraded = auth.AuthGate(mode="enforce", clock=self.clock, verifier=None,
                                 replay=None, stores_ok=False)
        broken_verifier = self.open_on(self.make_paths(), "enforce")
        broken_verifier.gate.fail_publish(broken_verifier.gate.snapshot()[0])
        gates = {
            "off": None,
            "warn": warn_runtime.gate,
            "enforce": enforce_runtime.gate,
            "enforce-signers-broken": broken.gate,
            "enforce-store-degraded": degraded,
        }
        for label, gate in gates.items():
            server, _, port = self.serve(gate)
            for name, raw in requests.items():
                with self.subTest(mode=label, case=name):
                    self.assertEqual(normalize(exchange(port, raw)), baseline[name])
            # framing 名額耗盡時 healthz 也不受影響（W-15）。
            held = 0
            while server.framing_semaphore.acquire(blocking=False):
                held += 1
            try:
                self.assertEqual(normalize(exchange(port, requests["plain"])),
                                 baseline["plain"])
            finally:
                for _ in range(held):
                    server.framing_semaphore.release()
        self.assertEqual(warn_runtime.gate.authenticate_calls, 0)
        self.assertEqual(enforce_runtime.gate.authenticate_calls, 0)
        # 中-3：非 origin-form 的 /healthz（含上面新增的三案）不得呼叫 gate。
        self.assertEqual(broken.gate.authenticate_calls, 0)
        self.assertEqual(broken_verifier.gate.authenticate_calls, 0)
        self.assertEqual(degraded.authenticate_calls, 0)


if __name__ == "__main__":
    unittest.main()
