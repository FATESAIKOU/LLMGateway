"""`lgwd/auth_authz.py` 的純函式判斷，以及 `Verdict`／`apply` 的 403 對應（#222 D3a）。

這一支只碰不碰 I/O 的部分：`authorize` 的順序與負向、`Verdict` 的不變式、`apply` 的
模式對應與日誌。真 `ssh-keygen`＋HTTP 的端到端在 `tests/test_http_authz.py`；gate 的
C-905（PASS 帶驗章時那一份 `permissions`）在本檔最後用替身 verifier／replay 驗。

不碰 `/var/lib`、`/etc`、不讀憑證、不連真機。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import MappingProxyType

from lgwd import auth, framing, sshsig
from lgwd.auth_authz import (
    FORBIDDEN_AGENT,
    FORBIDDEN_OP,
    FORBIDDEN_REASONS,
    FORBIDDEN_ROLE,
    authorize,
)
from lgwd.auth_paths import now_seconds
from lgwd.auth_registry import OPS, PrincipalEntry, parse_registry
from lgwd.auth_replay import ACCEPTED, Accept
from lgwd.clock import FakeClock
from lgwd.errors import agent_not_allowed, forbidden as forbidden_error, model_not_allowed
from lgwd.policy import AGENTS, ROLES
from tests.auth_fakes import principal_all, principal_all_entry
from tests.auth_fakes import registry_bytes as fake_registry_bytes
from tests.auth_signer import Signer, key_entry, registry_bytes

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


def permissions(*, ops=OPS, resolve_agents=AGENTS, roles=None) -> PrincipalEntry:
    """組一份不可變權限；`roles=None` 時每個角色都給全部 agent（B 的預設形狀）。"""
    if roles is None:
        roles = {role: tuple(AGENTS) for role in ROLES}
    return PrincipalEntry(
        ops=frozenset(ops),
        resolve_agents=tuple(resolve_agents),
        roles=MappingProxyType({role: tuple(agents) for role, agents in roles.items()}),
    )


class PrincipalAllFixtureTests(unittest.TestCase):
    """夾具本身：`principal_all()` 解析後與 `principal_all_entry()` 逐值相同。"""

    def test_principal_all_matches_the_parsed_entry(self):
        raw = fake_registry_bytes(1, principals={"w1-worker": principal_all()})
        parsed = parse_registry(raw).principals["w1-worker"]
        entry = principal_all_entry()
        self.assertEqual(parsed.ops, entry.ops)
        self.assertEqual(parsed.resolve_agents, entry.resolve_agents)
        self.assertEqual(dict(parsed.roles), dict(entry.roles))

    def test_principal_all_gives_all_ops_agents_and_roles(self):
        data = principal_all()
        self.assertEqual(set(data["ops"]), set(OPS))
        self.assertEqual(set(data["resolve_agents"]), set(AGENTS))
        self.assertEqual(set(data["roles"]), set(ROLES))
        for role in ROLES:
            self.assertEqual(set(data["roles"][role]), set(AGENTS))



class AuthorizeOpTests(unittest.TestCase):
    """C-901／C-902 第 1 步：沒列的操作一律 forbidden_op。"""

    def test_C902_missing_op_is_forbidden_op(self):
        for op in OPS:
            missing = [name for name in OPS if name != op]
            with self.subTest(op=op):
                self.assertEqual(
                    authorize(permissions(ops=missing), op, agent="opencode", role="impl"),
                    FORBIDDEN_OP)

    def test_C902_empty_ops_forbids_every_op(self):
        empty = permissions(ops=(), resolve_agents=(), roles={})
        for op in OPS:
            with self.subTest(op=op):
                self.assertEqual(authorize(empty, op), FORBIDDEN_OP)

    def test_C901_each_allowed_op_passes(self):
        full = principal_all_entry()
        for op in OPS:
            with self.subTest(op=op):
                self.assertIsNone(
                    authorize(full, op, agent="opencode", role="impl"))

    def test_C902_op_check_comes_first(self):
        """op 不在列時即使 agent／role 也不在列，仍是 forbidden_op（順序固定）。"""
        narrow = permissions(ops=["list"], resolve_agents=(), roles={})
        self.assertEqual(authorize(narrow, "resolve", agent="agy"), FORBIDDEN_OP)
        self.assertEqual(authorize(narrow, "roles", agent="agy", role="qa"), FORBIDDEN_OP)


class AuthorizeResolveTests(unittest.TestCase):
    """C-902 第 2 步與 C-903：resolve 只限制 agent，不限制角色。"""

    def test_C902_agent_not_in_resolve_agents(self):
        only_opencode = permissions(resolve_agents=["opencode"])
        self.assertEqual(authorize(only_opencode, "resolve", agent="agy"),
                         FORBIDDEN_AGENT)
        self.assertEqual(authorize(only_opencode, "resolve", agent="codex"),
                         FORBIDDEN_AGENT)

    def test_C902_agent_in_resolve_agents_passes(self):
        only_opencode = permissions(resolve_agents=["opencode"])
        self.assertIsNone(authorize(only_opencode, "resolve", agent="opencode"))

    def test_C902_missing_or_invalid_agent_is_not_judged(self):
        """缺 agent、agent 不是 AGENTS：授權不判，交給業務 400（C-902）。"""
        empty = permissions(resolve_agents=())
        for agent in (None, "", "xyz", "opencode/", "OPENCODE"):
            with self.subTest(agent=agent):
                self.assertIsNone(authorize(empty, "resolve", agent=agent))

    def test_C903_resolve_is_not_limited_by_role(self):
        """resolve 沒有 role：名冊的 roles 不影響 resolve（C-903 的限制記錄）。"""
        no_roles = permissions(roles={})
        self.assertIsNone(authorize(no_roles, "resolve", agent="opencode", role="qa"))


class AuthorizeRolesTests(unittest.TestCase):
    """C-902 第 3 步：role 與 agent 都要在列。"""

    def test_C902_role_not_listed_is_forbidden_role(self):
        only_impl = permissions(roles={"impl": ["opencode"]})
        self.assertEqual(authorize(only_impl, "roles", agent="opencode", role="qa"),
                         FORBIDDEN_ROLE)
        for role in ROLES:
            if role == "impl":
                continue
            with self.subTest(role=role):
                self.assertEqual(
                    authorize(only_impl, "roles", agent="opencode", role=role),
                    FORBIDDEN_ROLE)

    def test_C902_agent_not_in_that_role_is_forbidden_role(self):
        only_impl = permissions(roles={"impl": ["opencode"]})
        self.assertEqual(authorize(only_impl, "roles", agent="agy", role="impl"),
                         FORBIDDEN_ROLE)

    def test_C902_role_and_agent_in_list_passes(self):
        only_impl = permissions(roles={"impl": ["opencode", "claude-code"]})
        self.assertIsNone(authorize(only_impl, "roles", agent="claude-code", role="impl"))

    def test_C902_invalid_role_or_agent_is_not_judged(self):
        """role 不是 ROLES、agent 缺或不合法：授權不判，交給業務 400／404。"""
        narrow = permissions(roles={})
        for role, agent in (("Unknown", "opencode"), ("", "opencode"),
                            ("impl", None), ("impl", "xyz")):
            with self.subTest(role=role, agent=agent):
                self.assertIsNone(authorize(narrow, "roles", agent=agent, role=role))

    def test_C902_role_missing_agent_is_not_judged(self):
        self.assertIsNone(authorize(permissions(roles={}), "roles", role="impl"))


class AuthorizeOtherOpsTests(unittest.TestCase):
    """C-904：list／usage／cooldown 只判操作，resolve_agents 與 roles 不參與。"""

    def test_C904_other_ops_ignore_agent_and_role(self):
        narrow = permissions(ops=OPS, resolve_agents=(), roles={})
        for op in ("list", "usage", "cooldown"):
            for agent, role in (("agy", "qa"), (None, None), ("xyz", "Unknown")):
                with self.subTest(op=op, agent=agent, role=role):
                    self.assertIsNone(authorize(narrow, op, agent=agent, role=role))

    def test_unknown_op_is_forbidden_op(self):
        self.assertEqual(authorize(principal_all_entry(), "delete"), FORBIDDEN_OP)


class AuthorizeContractTests(unittest.TestCase):
    """純函式契約：只讀傳進來的 `PrincipalEntry`，型別錯就大聲失敗。"""

    def test_permissions_must_be_a_principal_entry(self):
        for wrong in (None, {"ops": ["list"]}, frozenset()):
            with self.subTest(wrong=type(wrong).__name__):
                with self.assertRaises(TypeError):
                    authorize(wrong, "list")

    def test_authorize_returns_only_the_three_fixed_reasons_or_none(self):
        allowed = {None, FORBIDDEN_OP, FORBIDDEN_AGENT, FORBIDDEN_ROLE}
        self.assertEqual(set(FORBIDDEN_REASONS), allowed - {None})
        narrow = permissions(ops=(), resolve_agents=(), roles={})
        self.assertIn(authorize(narrow, "resolve", agent="agy"), allowed)
        self.assertIn(authorize(principal_all_entry(), "roles", agent="impl",
                                role="impl"), allowed)

    def test_authorize_does_not_mutate_or_read_anything_else(self):
        entry = permissions(resolve_agents=["opencode"])
        before = (entry.ops, entry.resolve_agents, dict(entry.roles))
        authorize(entry, "resolve", agent="agy")
        self.assertEqual((entry.ops, entry.resolve_agents, dict(entry.roles)), before)


class VerdictInvariantTests(unittest.TestCase):
    """C-906：`forbidden` 與 `permissions` 的不變式在型別層守住。"""

    def test_forbidden_needs_a_principal_and_one_of_three_reasons(self):
        for reason in FORBIDDEN_REASONS:
            with self.subTest(reason=reason):
                verdict = auth.forbidden("w1-worker", reason)
                self.assertEqual((verdict.outcome, verdict.reason, verdict.principal),
                                 (auth.OUTCOME_FORBIDDEN, reason, "w1-worker"))
                self.assertIsNone(verdict.permissions)
                self.assertEqual(verdict.retry_after, 0)
        for bad in ("", None, "missing", "bad_signature"):
            with self.subTest(reason=bad):
                with self.assertRaises(ValueError):
                    auth.Verdict(auth.OUTCOME_FORBIDDEN, bad, principal="p")
        with self.assertRaises(ValueError):
            auth.Verdict(auth.OUTCOME_FORBIDDEN, FORBIDDEN_OP)

    def test_pass_needs_the_verification_time_permissions(self):
        with self.assertRaises(ValueError):
            auth.Verdict(auth.OUTCOME_PASS, "", principal="p")
        with self.assertRaises(ValueError):
            auth.passed("p", None)
        entry = principal_all_entry()
        self.assertIs(auth.passed("p", entry).permissions, entry)

    def test_only_pass_carries_permissions(self):
        entry = principal_all_entry()
        for verdict in (auth.denied(auth.REASON_EXPIRED), auth.limited(9),
                        auth.unavailable(auth.REASON_BUSY),
                        auth.forbidden("p", FORBIDDEN_OP)):
            with self.subTest(outcome=verdict.outcome):
                with self.assertRaises(ValueError):
                    type(verdict)(verdict.outcome, verdict.reason, principal=verdict.principal,
                                  retry_after=verdict.retry_after, permissions=entry)

    def test_deny_cannot_use_an_authorization_reason(self):
        for reason in FORBIDDEN_REASONS:
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    auth.denied(reason)


class ApplyForbiddenTests(unittest.TestCase):
    """C-906：enforce 403（bytes 全同）、warn 照舊並記 would-deny、off 不呼叫。"""

    def test_C906_enforce_403_body_is_identical_for_all_reasons(self):
        bodies = set()
        for reason in FORBIDDEN_REASONS:
            action = auth.apply("enforce", auth.forbidden("w1-worker", reason))
            self.assertEqual((action.kind, action.status), (auth.ACTION_RESPOND, 403))
            self.assertEqual(action.error.code, "forbidden")
            self.assertEqual(action.headers, (), msg="403 不得帶任何額外標頭")
            bodies.add(json.dumps(action.error.body(), ensure_ascii=False,
                                  separators=(", ", ": ")).encode("utf-8"))
        self.assertEqual(len(bodies), 1)
        self.assertEqual(json.loads(bodies.pop()), {"error": "forbidden"})

    def test_C906_business_403_bodies_are_untouched(self):
        """業務 policy 的 403 保留原 body（只有通過授權之後才會走到）。"""
        for factory, code in ((agent_not_allowed, "agent_not_allowed"),
                              (model_not_allowed, "model_not_allowed"),
                              (forbidden_error, "forbidden")):
            self.assertEqual(factory().status, 403)
            self.assertEqual(factory().code, code)

    def test_C906_warn_continues_and_logs_forbidden(self):
        lines = []
        log = auth.AuthLog(sink=lines.append)
        action = auth.apply("warn", auth.forbidden("w1-worker", FORBIDDEN_AGENT),
                            route="/v1/resolve", log=log)
        self.assertEqual(action.kind, auth.ACTION_CONTINUE)
        self.assertEqual(len(lines), 1)
        self.assertIn("mode=warn", lines[0])
        self.assertIn("outcome=forbidden", lines[0])
        self.assertIn("reason=forbidden_agent", lines[0])
        self.assertIn("route=/v1/resolve", lines[0])
        self.assertIn("principal=w1-worker", lines[0])

    def test_C906_off_returns_continue_and_does_not_log(self):
        lines = []
        log = auth.AuthLog(sink=lines.append)
        action = auth.apply("off", auth.forbidden("w1-worker", FORBIDDEN_OP), log=log)
        self.assertEqual(action.kind, auth.ACTION_CONTINUE)
        self.assertEqual(lines, [])

    def test_C906_authorization_reasons_are_on_the_whitelist(self):
        self.assertEqual(set(auth.AUTHORIZATION_REASONS), set(FORBIDDEN_REASONS))
        self.assertTrue(set(FORBIDDEN_REASONS) <= set(auth.REASONS))


class FakeVerifier:
    def __init__(self, outcome=sshsig.Outcome.PASS):
        self.outcome = outcome
        self.calls = 0

    def verify(self, message, blob, entry, signers):
        self.calls += 1
        return self.outcome


class FakeReplay:
    def accept(self, key_id, nonce, ts):
        return Accept(ACCEPTED)


class Headers:
    defects = ()

    def __init__(self, pairs=()):
        self._pairs = [(str(name), str(value)) for name, value in pairs]

    def get_all(self, name):
        return [value for key, value in self._pairs if key.lower() == name.lower()]

    def get(self, name):
        values = self.get_all(name)
        return values[0] if values else None


class GatePermissionsTests(unittest.TestCase):
    """C-905：PASS 的 `Verdict` 帶的是驗章那一份名冊快照裡的 `PrincipalEntry`。"""

    signer: Signer

    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = Signer(cls._keys.name, "k-a")

    @classmethod
    def tearDownClass(cls):
        cls._keys.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock(NOW)

    def make_gate(self, principal, generation=1):
        raw = registry_bytes(
            generation, [key_entry("k-a", self.signer)],
            principals={"w1-worker": principal})
        registry = parse_registry(raw)
        gate = auth.AuthGate(mode="enforce", clock=self.clock,
                             verifier=FakeVerifier(), replay=FakeReplay())
        gate.publish(registry, sshsig.write_allowed_signers(
            registry.active_keys(), self._tmp.name, registry.generation))
        return gate, registry

    def signed(self):
        timestamp = now_seconds(self.clock)
        return Headers(self.signer.headers(
            key_id="k-a", method="GET", target="/v1/subscriptions",
            timestamp=timestamp))

    def authenticate(self, gate):
        return gate.authenticate(
            method="GET", framing=framing.Framing(
                target_ok=True, target="/v1/subscriptions", path="/v1/subscriptions",
                query="", length=0, too_large=False, ok=True),
            headers=self.signed(), body=b"")

    def test_C905_pass_carries_the_registry_entry(self):
        narrow = {"ops": ["list"], "resolve_agents": [], "roles": {}}
        gate, registry = self.make_gate(narrow)
        verdict = self.authenticate(gate)
        self.assertEqual(verdict.outcome, auth.OUTCOME_PASS)
        self.assertIs(verdict.permissions, registry.principals["w1-worker"])
        self.assertEqual(verdict.permissions.ops, frozenset({"list"}))
        # 授權只讀這個欄位：同一個 verdict 對 resolve 是 forbidden_op。
        self.assertEqual(authorize(verdict.permissions, "resolve", agent="opencode"),
                         FORBIDDEN_OP)

    def test_C905_permissions_follow_the_verification_time_snapshot(self):
        """換世代後下一個請求拿到新快照的權限；在途請求用的是自己那一份。"""
        gate, first = self.make_gate(principal_all(), generation=1)
        verdict_one = self.authenticate(gate)
        self.assertIs(verdict_one.permissions, first.principals["w1-worker"])
        narrow = {"ops": ["list"], "resolve_agents": [], "roles": {}}
        gate, second = self.make_gate(narrow, generation=2)
        verdict_two = gate.authenticate(
            method="GET", framing=framing.Framing(
                target_ok=True, target="/v1/subscriptions", path="/v1/subscriptions",
                query="", length=0, too_large=False, ok=True),
            headers=self.signed(), body=b"")
        self.assertIs(verdict_two.permissions, second.principals["w1-worker"])
        self.assertIsNot(verdict_two.permissions, verdict_one.permissions)


if __name__ == "__main__":
    unittest.main()
