"""端到端：授權判斷與 403（#222 D3a；C-901～C-909、W-3a～W-3g）。

真的 loopback、真的暫存金鑰、真的 `ssh-keygen`、真的兩庫、**真的 `RegistryService`**：
所以「缺 agent 是 400」「未知 role 是 404」「業務稽核沒有新增列」都是真的業務回覆，
不是假 service 的固定值。每個案例各自一份狀態目錄（`flock` 禁止多實例），用完即刪。

不碰 `/var/lib`、`/etc`、不讀憑證、不連真機。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lgwd import auth
from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths, now_seconds
from lgwd.auth_registry import content_hash
from lgwd.clock import FakeClock
from lgwd.http import make_api_server
from lgwd.keys import KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService
from tests.auth_fakes import principal_all, write_registry_file
from tests.auth_signer import Signer, key_entry
from tests.auth_signer import registry_bytes as signer_registry_bytes
from tests.test_http_framing import exchange, normalize, parse_response, req

HOST = "127.0.0.1"
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = b'{"error": "forbidden"}'
UNAUTHORIZED = b'{"error": "unauthorized"}'
HEALTHZ = b'{"status": "ok"}'
COOLDOWN = "/v1/subscriptions/claude-personal/cooldown"
JSON_BODY = b'{"until": "2026-10-11T15:00:00Z"}'
RESOLVE = "/v1/resolve?agent=opencode&provider=opencode-zen"

#: 五條業務路由與各自的成功狀態（C-901 的對應表；agent／role 都填合法值，只差 op）。
ROUTES = (
    ("list", "GET", "/v1/subscriptions", b"", 200),
    ("usage", "GET", "/v1/usage", b"", 200),
    ("resolve", "GET", RESOLVE, b"", 200),
    ("roles", "GET", "/v1/roles/impl?agent=opencode", b"", 200),
    ("cooldown", "POST", COOLDOWN, JSON_BODY, 204),
)


class RecordingService:
    """真 service 的轉發者：記下呼叫（斷言被拒的請求沒有進業務），並轉出稽核列。"""

    def __init__(self, real):
        self.real = real
        self.calls = []
        # (方法, principal) 的呼叫紀錄（#222 D3b 的 C-913／W-3i 用；calls 的形狀不動）。
        self.principals = []

    def audit_rows(self):
        return self.real._store.audit_all()

    def list_subscriptions(self):
        self.calls.append(("list_subscriptions",))
        return self.real.list_subscriptions()

    def list_usage_summary(self):
        self.calls.append(("list_usage_summary",))
        return self.real.list_usage_summary()

    def resolve(self, agent, provider, model=None, subscription=None, exclude=None,
                data=None, *, principal=None):
        self.calls.append(("resolve", agent, provider))
        self.principals.append(("resolve", principal))
        return self.real.resolve(agent, provider, model=model, subscription=subscription,
                                 exclude=exclude, data=data, principal=principal)

    def role_candidates(self, role, agent, data=None):
        self.calls.append(("role_candidates", role, agent))
        return self.real.role_candidates(role, agent, data)

    def register_cooldown(self, sub_id, body, *, principal=None):
        self.calls.append(("register_cooldown", sub_id))
        self.principals.append(("register_cooldown", principal))
        return self.real.register_cooldown(sub_id, body, principal=principal)


class HttpAuthzCase(unittest.TestCase):
    """真的 service＋真的認證 runtime；每個伺服器一份自己的狀態目錄。"""

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
        self.clock = FakeClock(NOW)
        self.servers = []

    # --- 工具 ---

    def make_service(self) -> RegistryService:
        root = Path(tempfile.mkdtemp(dir=self.dir))
        keys_path = root / "keys.env"
        keys_path.write_text(
            "# 測試用假 key（不是真憑證）\n"
            "CLAUDE_PERSONAL_OAUTH_TOKEN=fake-personal\n"
            "CLAUDE_COMPANY_OAUTH_TOKEN=fake-company\n"
            "OPENROUTER_API_KEY=fake-openrouter\n"
            "OPENCODE_ZEN_API_KEY=fake-zen\n"
            "GROQ_API_KEY=fake-groq\n"
            "OLLAMA_API_KEY=fake-ollama\n",
            encoding="utf-8",
        )
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        service = RegistryService(str(root / "llm.db"), policy,
                                  KeyStore(str(keys_path)), FakeClock())
        self.addCleanup(service.close)
        return service

    def make_paths(self):
        root = tempfile.mkdtemp(dir=self.dir)
        state = os.path.join(root, "state")
        os.makedirs(state)
        paths = auth_paths(state, registry=os.path.join(root, "registry.json"))
        create_stores(paths.trust_db, paths.replay_db)
        return paths

    def registry_raw(self, generation=1, principal=None, entries=None) -> bytes:
        return signer_registry_bytes(
            generation,
            entries if entries is not None else
            [key_entry("k-a", self.signer), key_entry("k-b", self.signer_b)],
            principals={"w1-worker": principal_all() if principal is None else principal},
        )

    def write_registry(self, paths, generation=1, principal=None, entries=None) -> bytes:
        raw = self.registry_raw(generation, principal, entries)
        write_registry_file(paths.registry, raw)
        return raw

    def open_runtime(self, paths, mode="enforce", **over):
        kwargs = dict(mode=mode, registry_path=paths.registry, state_dir=paths.state_dir,
                      per_key_max=600, poll_interval=0.0)
        kwargs.update(over)
        runtime = auth.open_runtime(
            auth.AuthConfig(**kwargs), self.clock,
            owner_uid=0, registry_owner_uid=os.getuid())
        self.addCleanup(runtime.stop)
        return runtime

    def serve(self, gate, service):
        if not isinstance(service, RecordingService):
            service = RecordingService(service)
        server = make_api_server(service, HOST, 0, gate=gate)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.addCleanup(self._stop, server, thread)
        return server, server.server_address[1]

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def server_for(self, mode="enforce", *, principal=None, service=None, paths=None,
                   generation=1, entries=None, **over):
        """建一份（狀態目錄、名冊、runtime、service、server）；回 runtime／service／port。"""
        paths = paths if paths is not None else self.make_paths()
        self.write_registry(paths, generation, principal, entries)
        runtime = self.open_runtime(paths, mode, **over)
        service = service if service is not None else self.make_service()
        service = service if isinstance(service, RecordingService) else RecordingService(service)
        server, port = self.serve(runtime.gate, service)
        return runtime, service, port

    def now(self) -> int:
        return now_seconds(self.clock)

    def sign(self, *, key_id="k-a", signer=None, method="GET", target="/v1/subscriptions",
             body=b""):
        signer = signer or self.signer
        return signer.headers(key_id=key_id, method=method, target=target, body=body,
                              timestamp=self.now())

    def call(self, port, method, target, headers=(), body=b""):
        extra = list(headers)
        if body and not any(name.lower() == "content-length" for name, _ in extra):
            extra.append(("Content-Length", str(len(body))))
        return parse_response(exchange(port, req(method, target, extra, body)))


def no_resolve() -> dict:
    """拿掉 `resolve` 的 B 預設（`ops` 不含 resolve 時 `resolve_agents` 必須為空）。"""
    return principal_all(
        ops=["list", "usage", "roles", "cooldown"], resolve_agents=[],
        roles={"impl": ["opencode"]})


class C901RouteOpTests(HttpAuthzCase):
    """C-901：路由與操作的對應固定；`/healthz` 與 404 不經授權。"""

    def test_C901_each_route_requires_its_own_op(self):
        for missing, _, _, _, _ in ROUTES:
            ops = [op for op in ("list", "usage", "resolve", "roles", "cooldown")
                   if op != missing]
            principal = principal_all(
                ops=ops,
                resolve_agents=["opencode"] if "resolve" in ops else [],
                roles={"impl": ["opencode"]} if "roles" in ops else {},
            )
            runtime, service, port = self.server_for(principal=principal)
            for op, method, target, body, expected in ROUTES:
                with self.subTest(missing=missing, route=op):
                    status, payload, headers = self.call(
                        port, method, target, self.sign(method=method, target=target,
                                                        body=body), body)
                    if op == missing:
                        self.assertEqual((status, payload), (403, FORBIDDEN))
                        self.assertNotIn("www-authenticate", headers)
                    else:
                        self.assertEqual(status, expected, payload)
            # 成功的那四條各呼叫一次 service；被拒的那一條 0 次。
            self.assertEqual(len(service.calls), 4)

    def test_C901_empty_ops_forbids_every_business_route(self):
        empty = principal_all(ops=[], resolve_agents=[], roles={})
        runtime, service, port = self.server_for(principal=empty)
        for op, method, target, body, _ in ROUTES:
            with self.subTest(route=op):
                status, payload, _ = self.call(
                    port, method, target, self.sign(method=method, target=target,
                                                    body=body), body)
                self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(service.calls, [])
        # C-901：`/healthz` 在 `ops` 為空時仍回 200（16 bytes）。
        status, payload, _ = self.call(port, "GET", "/healthz")
        self.assertEqual((status, payload), (200, HEALTHZ))
        # C-901：未知路徑維持 404，不經授權、不進 service。
        status, payload, _ = self.call(
            port, "GET", "/v1/nope", self.sign(target="/v1/nope"))
        self.assertEqual((status, json.loads(payload)["error"]), (404, "not_found"))
        self.assertEqual(service.calls, [])


class C902AuthorizationTests(HttpAuthzCase):
    """C-902：agent／role 的負向；缺或非法值不判，交給業務 400／404。"""

    def test_C902_resolve_agent_must_be_listed(self):
        principal = principal_all(resolve_agents=["opencode"])
        runtime, service, port = self.server_for(principal=principal)
        target = "/v1/resolve?agent=opencode&provider=opencode-zen"
        status, _, _ = self.call(port, "GET", target, self.sign(target=target))
        self.assertEqual(status, 200)
        for agent, provider in (("agy", "google"), ("codex", "openai")):
            refused = "/v1/resolve?agent={0}&provider={1}".format(agent, provider)
            with self.subTest(agent=agent):
                status, payload, _ = self.call(
                    port, "GET", refused, self.sign(target=refused))
                self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(len(service.calls), 1)

    def test_C902_missing_or_unknown_agent_is_400_not_403(self):
        # `resolve_agents` 為空：若授權先判 agent，這裡會變 403；必須是業務的 400。
        principal = principal_all(resolve_agents=[])
        runtime, service, port = self.server_for(principal=principal)
        target = "/v1/resolve?provider=opencode-zen"
        status, payload, _ = self.call(port, "GET", target, self.sign(target=target))
        self.assertEqual(status, 400)
        missing = json.loads(payload)["missing"]
        self.assertEqual(missing, ["agent"])
        unknown = "/v1/resolve?agent=xyz&provider=opencode-zen"
        status, payload, _ = self.call(port, "GET", unknown, self.sign(target=unknown))
        self.assertEqual((status, json.loads(payload)["error"]), (400, "bad_request"))
        # 400 由業務判（service 有被呼叫、但沒有寫稽核），不是授權的 403。
        self.assertEqual(service.audit_rows(), [])

    def test_C902_roles_requires_role_and_agent_in_the_list(self):
        principal = principal_all(roles={"impl": ["opencode"]})
        runtime, service, port = self.server_for(principal=principal)
        allowed = "/v1/roles/impl?agent=opencode"
        status, _, _ = self.call(port, "GET", allowed, self.sign(target=allowed))
        self.assertEqual(status, 200)
        # role 在 ROLES、agent 合法，但 role 不在該 principal 的 roles → 403。
        missing_role = "/v1/roles/qa?agent=opencode"
        status, payload, _ = self.call(
            port, "GET", missing_role, self.sign(target=missing_role))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        # role 在列、agent 合法，但不在 roles[role] → 403。
        missing_agent = "/v1/roles/impl?agent=agy"
        status, payload, _ = self.call(
            port, "GET", missing_agent, self.sign(target=missing_agent))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(len(service.calls), 1)

    def test_C902_invalid_role_or_agent_is_not_judged_by_authorization(self):
        principal = principal_all(roles={})
        runtime, service, port = self.server_for(principal=principal)
        # role 不是五個之一 → 業務 404（不是 403）。
        unknown_role = "/v1/roles/Unknown?agent=opencode"
        status, payload, _ = self.call(
            port, "GET", unknown_role, self.sign(target=unknown_role))
        self.assertEqual((status, json.loads(payload)["error"]), (404, "not_found"))
        # 缺 agent → 400。
        no_agent = "/v1/roles/impl"
        status, payload, _ = self.call(port, "GET", no_agent, self.sign(target=no_agent))
        self.assertEqual((status, json.loads(payload)["missing"]), (400, ["agent"]))
        # agent 不是 AGENTS → 400。
        bad_agent = "/v1/roles/impl?agent=xyz"
        status, payload, _ = self.call(port, "GET", bad_agent, self.sign(target=bad_agent))
        self.assertEqual((status, json.loads(payload)["error"]), (400, "bad_request"))
        # 400／404 都是業務判的（授權不判），所以 service 有被呼叫、稽核沒有新增列。
        self.assertEqual(service.audit_rows(), [])


class C906ForbiddenBytesTests(HttpAuthzCase):
    """C-906：enforce 的 403 body 對所有授權 reason 逐 byte 相同、不加標頭。"""

    def test_C906_enforce_403_is_identical_for_all_reasons(self):
        cases = (
            ("forbidden_op", no_resolve(), RESOLVE),
            ("forbidden_agent", principal_all(resolve_agents=["opencode"]),
             "/v1/resolve?agent=agy&provider=google"),
            ("forbidden_role", principal_all(roles={"impl": ["opencode"]}),
             "/v1/roles/qa?agent=opencode"),
        )
        seen = set()
        for label, principal, target in cases:
            with self.subTest(reason=label):
                runtime, service, port = self.server_for(principal=principal)
                response = normalize(exchange(
                    port, req("GET", target, list(self.sign(target=target)))))
                head, _, body = response.partition(b"\r\n\r\n")
                self.assertEqual(body, FORBIDDEN)
                self.assertEqual(head.split(b"\r\n")[0], b"HTTP/1.0 403 Forbidden")
                self.assertNotIn(b"www-authenticate", head.lower())
                self.assertNotIn(b"forbidden_", body)
                self.assertEqual(service.calls, [])
                seen.add(body)
        self.assertEqual(seen, {FORBIDDEN})


class WindowTests(HttpAuthzCase):
    """W-3a～W-3g：快照、生效時機、稽核、解析、warn、nonce、`/healthz`。"""

    def test_W3a_authorization_uses_the_verification_time_snapshot(self):
        """請求停在 verify 時換世代並拿掉 resolve：這一筆照舊，下一筆 403（C-905）。"""
        paths = self.make_paths()
        self.write_registry(paths, 1, principal_all())
        runtime = self.open_runtime(paths)
        service = RecordingService(self.make_service())
        server, port = self.serve(runtime.gate, service)
        target = RESOLVE

        class PausingVerifier:
            def __init__(self, real):
                self.real = real
                self.entered = threading.Event()
                self.resume = threading.Event()

            def verify(self, message, blob, entry, signers):
                self.entered.set()
                if not self.resume.wait(30):
                    raise AssertionError("W-3a hook never released")
                return self.real.verify(message, blob, entry, signers)

        pauser = PausingVerifier(runtime.gate.verifier)
        runtime.gate.verifier = pauser
        headers = self.sign(target=target)
        result = {}

        def send():
            result["response"] = self.call(port, "GET", target, headers)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        self.assertTrue(pauser.entered.wait(15), msg="請求沒有進到 verify")
        # 名冊換世代：同一個 principal 拿掉 resolve。
        self.write_registry(paths, 2, no_resolve())
        runtime.watcher.tick()
        pauser.resume.set()
        thread.join(15)
        status, payload, _ = result["response"]
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["meta"]["subscription"], "opencode-zen")
        # 下一個請求用新快照：403。
        status, payload, _ = self.call(port, "GET", target, self.sign(target=target))
        self.assertEqual((status, payload), (403, FORBIDDEN))

    def test_W3b_permission_change_applies_once_status_shows_the_hash(self):
        """install 新名冊、status 顯示已載入新雜湊後，馬上送 resolve 必定 403。"""
        paths = self.make_paths()
        self.write_registry(paths, 1, principal_all())
        runtime = self.open_runtime(paths)
        service = RecordingService(self.make_service())
        server, port = self.serve(runtime.gate, service)
        raw = self.write_registry(paths, 2, no_resolve())
        runtime.watcher.tick()
        status_doc = json.loads(Path(paths.status).read_text(encoding="utf-8"))
        self.assertEqual(status_doc["loaded_generation"], 2)
        self.assertEqual(status_doc["loaded_hash"], content_hash(raw))
        status, payload, _ = self.call(port, "GET", RESOLVE, self.sign(target=RESOLVE))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(service.calls, [])

    def test_W3c_rejected_requests_leave_no_business_audit(self):
        """401／403／429／503 都沒有業務稽核列；只有成功的 resolve 寫一列。"""
        target = RESOLVE
        # 401：無簽章。
        paths = self.make_paths()
        self.write_registry(paths, 1, principal_all())
        runtime = self.open_runtime(paths)
        service = RecordingService(self.make_service())
        server, port = self.serve(runtime.gate, service)
        self.assertEqual(self.call(port, "GET", target)[0], 401)
        self.assertEqual(service.audit_rows(), [])
        # 403：同一個 runtime 換成缺 resolve 的名冊。
        self.write_registry(paths, 2, no_resolve())
        runtime.watcher.tick()
        status, payload, _ = self.call(port, "GET", target,
                                       self.sign(target=target))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(service.audit_rows(), [])
        # 429：per_key_max=1 的獨立 runtime（先成功一筆用量，第二筆撞配額）。
        runtime429, service429, port429 = self.server_for(per_key_max=1)
        usage = "/v1/usage"
        self.assertEqual(
            self.call(port429, "GET", usage, self.sign(target=usage))[0], 200)
        status, payload, _ = self.call(port429, "GET", target,
                                       self.sign(target=target))
        self.assertEqual((status, json.loads(payload)["error"]),
                         (429, "rate_limited"))
        self.assertEqual(service429.audit_rows(), [])
        # 503：驗章／庫不可用的 gate（不碰業務）。
        service503 = RecordingService(self.make_service())
        gate = auth.AuthGate(mode="enforce", clock=self.clock,
                             verifier=None, replay=None, stores_ok=False)
        server503, port503 = self.serve(gate, service503)
        status, payload, _ = self.call(port503, "GET", target,
                                       self.sign(target=target))
        self.assertEqual((status, json.loads(payload)["error"]),
                         (503, "auth_unavailable"))
        self.assertEqual(service503.audit_rows(), [])
        # 成功的 resolve：正好一列（在業務交易內；principal 欄是 D3b 的事）。
        runtime_ok, service_ok, port_ok = self.server_for()
        status, _, _ = self.call(port_ok, "GET", target, self.sign(target=target))
        self.assertEqual(status, 200)
        rows = service_ok.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["action"], rows[0]["subscription"]),
                         ("resolve", "opencode-zen"))

    def test_W3d_authorization_uses_the_service_query_decoding(self):
        """授權看到的值與 service 實際使用的同一個（C-909；不得自己再解析 query）。"""
        principal = principal_all(resolve_agents=["opencode"])
        runtime, service, port = self.server_for(principal=principal)
        # URL 編碼的 agent：service 解成 agy；授權若看原文就會漏判。
        encoded = "/v1/resolve?agent=%61gy&provider=google"
        status, payload, _ = self.call(port, "GET", encoded,
                                       self.sign(target=encoded))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        # 重複的已知參數：業務 400（授權不判），不是 403、也不是 200。
        duplicate = "/v1/resolve?agent=opencode&agent=agy&provider=google"
        status, payload, _ = self.call(port, "GET", duplicate,
                                       self.sign(target=duplicate))
        self.assertEqual((status, json.loads(payload)["error"]),
                         (400, "bad_request"))
        self.assertEqual(service.calls, [])

    def test_W3e_warn_forbidden_keeps_the_business_result(self):
        """warn：forbidden 只記 would-deny，業務照舊 200，稽核照常寫（W-3e）。"""
        principal = principal_all(resolve_agents=["opencode"])
        runtime, service, port = self.server_for(mode="warn", principal=principal)
        target = "/v1/resolve?agent=agy&provider=google"
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, payload, _ = self.call(port, "GET", target,
                                           self.sign(target=target))
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["meta"]["agent"], "agy")
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(len(service.audit_rows()), 1)
        text = "\n".join(logs.output)
        self.assertIn("mode=warn", text)
        self.assertIn("outcome=forbidden", text)
        self.assertIn("reason=forbidden_agent", text)
        self.assertIn("route=/v1/resolve", text)
        self.assertIn("principal=w1-worker", text)

    def test_W3f_a_denied_request_replayed_is_401_not_403(self):
        """nonce 在授權之前已被消耗（C-907）：原樣重送是 401 replay，不是 403。"""
        runtime, service, port = self.server_for(principal=no_resolve())
        headers = self.sign(target=RESOLVE)
        status, payload, _ = self.call(port, "GET", RESOLVE, headers)
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(runtime.replay.row_count(), 1,
                         msg="accept 必須在授權之前，nonce 已消耗")
        status, payload, _ = self.call(port, "GET", RESOLVE, headers)
        self.assertEqual((status, payload), (401, UNAUTHORIZED))
        self.assertEqual(service.calls, [])

    def test_W3g_healthz_is_exempt_from_authorization(self):
        """`/healthz` 在 ops 為空、名冊不可用時仍是 D1 的 16 bytes（W-3g）。"""
        empty = principal_all(ops=[], resolve_agents=[], roles={})
        runtime, service, port = self.server_for(principal=empty)
        # 簽章的 /healthz 也不呼叫 gate、不經授權。
        status, payload, _ = self.call(port, "GET", "/healthz",
                                       self.sign(target="/healthz"))
        self.assertEqual((status, payload), (200, HEALTHZ))
        self.assertEqual(runtime.gate.authenticate_calls, 0)
        # 同一份空權限下業務路由是 403。
        status, _, _ = self.call(port, "GET", "/v1/subscriptions", self.sign())
        self.assertEqual(status, 403)
        # 名冊不可用（allowed_signers 壞掉）：/healthz 照舊。
        runtime.gate.fail_publish(runtime.gate.snapshot()[0])
        status, payload, _ = self.call(port, "GET", "/healthz")
        self.assertEqual((status, payload), (200, HEALTHZ))


class OffModeTests(HttpAuthzCase):
    """C-908：off 完全不判授權，回應與沒有 gate 的 D1 路徑逐 byte 相同。"""

    def test_off_never_authorizes_and_matches_the_plain_path(self):
        requests = {
            "list": req("GET", "/v1/subscriptions"),
            "resolve": req("GET", "/v1/resolve?agent=agy&provider=google"),
            "roles": req("GET", "/v1/roles/qa?agent=opencode"),
            "cooldown": req("POST", COOLDOWN,
                            [("Content-Length", str(len(JSON_BODY)))], JSON_BODY),
            "unsigned_resolve": req("GET", RESOLVE),
        }
        expected = {"list": 200, "resolve": 200, "roles": 200, "cooldown": 204,
                    "unsigned_resolve": 200}
        service = self.make_service()
        server, port = self.serve(None, service)
        with mock.patch("lgwd.http.authorize",
                        side_effect=AssertionError("off must not authorize")):
            off = {}
            for name, raw in requests.items():
                response = normalize(exchange(port, raw))
                off[name] = response
                status = parse_response(response)[0]
                self.assertEqual(
                    status, expected[name],
                    msg="{0}: {1!r}".format(name, response))
        # 逐 byte 相同：off（gate=None）與「有 gate 但模式 off」走的是同一條 D1 路徑。
        other_service = self.make_service()
        other_server, other_port = self.serve(type("OffGate", (), {"mode": "off"})(),
                                              other_service)
        for name, raw in requests.items():
            with self.subTest(case=name):
                self.assertEqual(normalize(exchange(other_port, raw)), off[name])
        # 同一批請求在 enforce＋空權限下是 403：證明 off 真的沒有判。
        runtime, enforce_service, enforce_port = self.server_for(
            principal=principal_all(ops=[], resolve_agents=[], roles={}))
        status, payload, _ = self.call(
            enforce_port, "GET", "/v1/resolve?agent=agy&provider=google",
            self.sign(target="/v1/resolve?agent=agy&provider=google"))
        self.assertEqual((status, payload), (403, FORBIDDEN))


if __name__ == "__main__":
    unittest.main()
