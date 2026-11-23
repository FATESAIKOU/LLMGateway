"""`lgwd/auth.py` 的 gate／apply／日誌單元測試（#222 D2 PR-6a；C-602～C-604、C-607、C-608）。

這裡只碰 gate 本身：真的名冊／真的 `allowed_signers`／真的 SSHSIG 簽章（`tests/auth_signer.py`
的暫存金鑰），但 verifier 與 replay store 是替身，所以每一個 401／429／503 的分類都能
單獨構造。真 `ssh-keygen` 與 HTTP 的整合在 `tests/test_http_auth.py`，啟動組裝與窗口
（C-605／C-610）在 `tests/test_auth_runtime.py`。

不碰 `/var/lib`、`/etc`、不讀憑證、不連真機。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from lgwd import auth, framing, sshsig
from lgwd.auth_paths import now_seconds
from lgwd.auth_registry import parse_registry
from lgwd.auth_replay import ACCEPTED, DENIED, Accept, rate_limited
from lgwd.auth_db import StoreBusy
from lgwd.clock import FakeClock
from tests.auth_fakes import REPO_ROOT, principal_all_entry
from tests.auth_signer import Signer, b64url, key_entry, registry_bytes

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
TARGET = "/v1/subscriptions"


def nonce(seed: int) -> str:
    """可重現的**規範編碼** nonce（32 bytes 的 base64url；重複字元串不一定是合法的）。"""
    return b64url(bytes([seed]) * 32)


class Headers:
    """`email.message.Message` 的最小替身：`get_all` 不分大小寫、可放重複標頭。"""

    defects = ()

    def __init__(self, pairs=()):
        self._pairs = [(str(name), str(value)) for name, value in pairs]

    def get_all(self, name):
        return [value for key, value in self._pairs if key.lower() == name.lower()]

    def get(self, name):
        values = self.get_all(name)
        return values[0] if values else None

    def keys(self):
        return [key for key, _ in self._pairs]

    def items(self):
        return list(self._pairs)


def make_framing(target=TARGET, *, ok=True):
    return framing.Framing(
        target_ok=True, target=target, path=target.split("?")[0],
        query=target.partition("?")[2], length=0, too_large=False, ok=ok)


class FakeVerifier:
    """`auth.AuthGate` 只需要 `verify(message, blob, entry, signers)`。"""

    def __init__(self, outcome=sshsig.Outcome.PASS):
        self.outcome = outcome
        self.calls = 0
        self.messages: list[bytes] = []
        self.before_verify = None

    def verify(self, message, blob, entry, signers):
        self.calls += 1
        self.messages.append(message)
        if self.before_verify is not None:
            self.before_verify(self)
        return self.outcome

    def recheck_if_changed(self):
        return True


class FakeReplay:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else Accept(ACCEPTED)
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    def accept(self, key_id, nonce, ts):
        self.calls.append((key_id, nonce, ts))
        if self.error is not None:
            raise self.error
        return self.result


class GateCase(unittest.TestCase):
    """共用一把暫存簽章金鑰；每個測試各自一份名冊與 allowed_signers。"""

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
        self.now = now_seconds(self.clock)
        self.verifier = FakeVerifier()
        self.replay = FakeReplay()
        self.raw = registry_bytes(1, [key_entry("k-a", self.signer)])
        self.registry = parse_registry(self.raw)
        self.gate = auth.AuthGate(
            mode="enforce", clock=self.clock, verifier=self.verifier, replay=self.replay)
        self.signers = sshsig.write_allowed_signers(
            self.registry.active_keys(), self.dir, self.registry.generation)
        self.gate.publish(self.registry, self.signers)

    # --- 工具 ---

    def headers(self, *, key_id="k-a", signer=None, method="GET", target=TARGET,
                body=b"", timestamp=None, nonce=None):
        signer = signer or self.signer
        return Headers(signer.headers(
            key_id=key_id, method=method, target=target, body=body,
            timestamp=self.now if timestamp is None else timestamp, nonce=nonce))

    def authenticate(self, headers, *, method="GET", framing_=None, body=b""):
        return self.gate.authenticate(
            method=method,
            framing=framing_ if framing_ is not None else make_framing(),
            headers=headers,
            body=body,
        )

    def assertVerdict(self, verdict, outcome, reason=""):
        self.assertEqual((verdict.outcome, verdict.reason), (outcome, reason), verdict)


class C602GateFlowTests(GateCase):
    """C-602：標頭 → 版本 → key ID → 查表 → 形狀 → 粗篩 → 預解析 → 八行 → verify → accept。"""

    def test_C602_missing_header_is_missing(self):
        full = self.headers()
        for name in auth.HEADER_NAMES:
            with self.subTest(header=name):
                pairs = [(k, v) for k, v in full.items() if k != name]
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MISSING)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_duplicate_header_is_malformed(self):
        for name in auth.HEADER_NAMES:
            with self.subTest(header=name):
                pairs = self.headers().items() + [(name, "x")]
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_version_must_be_exactly_one(self):
        for value in ("2", "01", "", "1 "):
            with self.subTest(value=value):
                pairs = [(k, v) for k, v in self.headers().items()
                         if k != auth.HEADER_VERSION]
                pairs.append((auth.HEADER_VERSION, value))
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_key_id_format(self):
        for value in ("-bad", ".bad", "bad/id", "bad id", "a" * 65, "",
                      'bad"id', "bad*id"):
            with self.subTest(value=value):
                pairs = [(k, v) for k, v in self.headers().items()
                         if k != auth.HEADER_KEY_ID]
                pairs.append((auth.HEADER_KEY_ID, value))
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_unknown_key_and_revoked(self):
        self.assertVerdict(self.authenticate(self.headers(key_id="k-unknown")),
                           auth.OUTCOME_DENY, auth.REASON_UNKNOWN_KEY)
        revoked = registry_bytes(2, [key_entry("k-a", self.signer, status="revoked")])
        registry = parse_registry(revoked)
        signers = sshsig.write_allowed_signers(
            registry.active_keys(), self.dir, registry.generation)
        self.gate.publish(registry, signers)
        self.assertVerdict(self.authenticate(self.headers()),
                           auth.OUTCOME_DENY, auth.REASON_REVOKED)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_timestamp_shape(self):
        for value in ("0" + str(self.now), "-1", "abc", "+1", "1.0", "",
                      "9" * 4300):
            with self.subTest(value=value[:10]):
                pairs = [(k, v) for k, v in self.headers().items()
                         if k != auth.HEADER_TIMESTAMP]
                pairs.append((auth.HEADER_TIMESTAMP, value))
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_nonce_shape(self):
        good = nonce(7)
        noncanonical = good[:-1] + ("B" if good[-1] != "B" else "C")
        bad_values = (good[:-1], good + "A", "+" + good[1:], "/" + good[1:],
                      noncanonical, "")
        for value in bad_values:
            with self.subTest(value=value):
                pairs = [(k, v) for k, v in self.headers(nonce=good).items()
                         if k != auth.HEADER_NONCE]
                pairs.append((auth.HEADER_NONCE, value))
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_coarse_window_plus_minus_sixty(self):
        self.assertVerdict(
            self.authenticate(self.headers(timestamp=self.now + 61, nonce=nonce(1))),
            auth.OUTCOME_DENY, auth.REASON_EXPIRED)
        self.assertEqual(self.verifier.calls, 0)
        self.assertVerdict(
            self.authenticate(self.headers(timestamp=self.now - 61, nonce=nonce(2))),
            auth.OUTCOME_DENY, auth.REASON_EXPIRED)
        self.assertEqual(self.verifier.calls, 0)
        # 邊界：±60 仍進得去（最終由 accept 的交易內 now 判定）。
        self.assertVerdict(
            self.authenticate(self.headers(timestamp=self.now + 60, nonce=nonce(3))),
            auth.OUTCOME_PASS)

    def test_C602_framing_failure_is_deny_framing(self):
        self.assertVerdict(
            self.authenticate(self.headers(), framing_=make_framing(ok=False)),
            auth.OUTCOME_DENY, auth.REASON_FRAMING)
        self.assertEqual(self.verifier.calls, 0)

    def test_C602_preparse_rejects_do_not_fork(self):
        good = self.headers().get(auth.HEADER_SIGNATURE)
        # 239 字元（長度不符）、含 padding、字元表外、240 個字元但不是 SSHSIG。
        garbage = base64.urlsafe_b64encode(b"\x00" * 180).rstrip(b"=").decode("ascii")
        for value in (good[:-1], good + "=", "+" * 240, garbage, "A" * 240):
            with self.subTest(value=value[:12]):
                pairs = [(k, v) for k, v in self.headers().items()
                         if k != auth.HEADER_SIGNATURE]
                pairs.append((auth.HEADER_SIGNATURE, value))
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(self.verifier.calls, 0, msg="預解析拒絕不得開子行程")

    def test_C602_bad_signature_and_unavailable(self):
        self.verifier.outcome = sshsig.Outcome.BAD_SIG
        self.assertVerdict(self.authenticate(self.headers()),
                           auth.OUTCOME_DENY, auth.REASON_BAD_SIGNATURE)
        self.verifier.outcome = sshsig.Outcome.UNAVAILABLE
        self.assertVerdict(self.authenticate(self.headers()),
                           auth.OUTCOME_UNAVAILABLE, auth.REASON_VERIFIER_UNAVAILABLE)

    def test_C602_eight_line_message(self):
        target = "/v1/roles/a%2Fb?a=1&b=2"
        body = b'{"until": "2026-10-11T15:00:00Z"}'
        headers = self.headers(target=target, nonce=nonce(4))
        verdict = self.authenticate(headers, framing_=make_framing(target), body=body)
        self.assertVerdict(verdict, auth.OUTCOME_PASS)
        message = self.verifier.messages[-1]
        lines = message.decode("ascii").split("\n")
        self.assertEqual(lines[0], "LGW-HTTP-SIG-V1")
        self.assertEqual(lines[1], "llmgateway")
        self.assertEqual(lines[2], "k-a")
        self.assertEqual(lines[3], str(self.now))
        self.assertEqual(lines[4], nonce(4))
        self.assertEqual(lines[5], "GET")
        self.assertEqual(lines[6], target)  # 原文（帶 %2F、query）
        self.assertEqual(lines[7], hashlib.sha256(body).hexdigest())
        self.assertEqual(message[-1:], b"\n")
        self.assertEqual(len(lines), 9)  # 八行＋結尾 LF 的最後一個空字串

    def test_C602_accept_denied_and_rate_limited(self):
        self.replay.result = Accept(DENIED, "expired")
        self.assertVerdict(self.authenticate(self.headers()),
                           auth.OUTCOME_DENY, auth.REASON_EXPIRED)
        self.replay.result = Accept(DENIED, "water")
        self.assertVerdict(self.authenticate(self.headers(nonce=nonce(5))),
                           auth.OUTCOME_DENY, auth.REASON_WATER)
        self.replay.result = Accept(DENIED, "replay")
        self.assertVerdict(self.authenticate(self.headers(nonce=nonce(6))),
                           auth.OUTCOME_DENY, auth.REASON_REPLAY)
        self.replay.result = rate_limited(42)
        verdict = self.authenticate(self.headers(nonce=nonce(7)))
        self.assertVerdict(verdict, auth.OUTCOME_RATE_LIMITED, auth.REASON_RATE_LIMITED)
        self.assertEqual(verdict.retry_after, 42)

    def test_C602_accept_store_error_is_unavailable_never_pass(self):
        for error in (StoreBusy("busy"), OSError("closed"), RuntimeError("bug")):
            with self.subTest(error=type(error).__name__):
                self.replay.error = error
                self.assertVerdict(self.authenticate(self.headers()),
                                   auth.OUTCOME_UNAVAILABLE,
                                   auth.REASON_STORE_UNAVAILABLE)

    def test_C602_accept_receives_the_header_key_and_nonce(self):
        headers = self.headers(nonce=nonce(8))
        self.assertVerdict(self.authenticate(headers), auth.OUTCOME_PASS)
        self.assertEqual(self.replay.calls, [("k-a", nonce(8), self.now)])

    def test_C602_pass_carries_principal(self):
        verdict = self.authenticate(self.headers())
        self.assertVerdict(verdict, auth.OUTCOME_PASS)
        self.assertEqual(verdict.principal, "w1-worker")
        self.assertEqual(verdict.retry_after, 0)

    def test_W20_preparse_rejects_never_execute_the_verifier(self):
        """W-20：各類預解析 Reject 都不 fork、也不取 verifier 號誌。"""
        real = sshsig.Verifier(
            spool_dir=self.dir, sshkeygen_path="/usr/bin/ssh-keygen",
            owner_uid=os.getuid())
        self.gate.verifier = real
        executed = []
        with mock.patch.object(real, "_execute",
                               lambda *args, **kwargs: executed.append(args)):
            for value in ("A" * 240, "+" * 240, "B" * 239):
                pairs = [(k, v) for k, v in self.headers().items()
                         if k != auth.HEADER_SIGNATURE]
                pairs.append((auth.HEADER_SIGNATURE, value))
                self.assertVerdict(self.authenticate(Headers(pairs)),
                                   auth.OUTCOME_DENY, auth.REASON_MALFORMED)
        self.assertEqual(executed, [])


class C602UnavailableStateTests(GateCase):
    """C-602／C-603：庫、名冊、allowed_signers 不可用時的 503 分類（fail-closed）。"""

    def test_store_degraded_is_store_unavailable(self):
        gate = auth.AuthGate(mode="enforce", clock=self.clock, verifier=self.verifier,
                             replay=self.replay, stores_ok=False)
        gate.publish(self.registry, self.signers)
        verdict = gate.authenticate(
            method="GET", framing=make_framing(), headers=self.headers(), body=b"")
        self.assertVerdict(verdict, auth.OUTCOME_UNAVAILABLE, auth.REASON_STORE_UNAVAILABLE)

    def test_no_registry_is_registry_unavailable(self):
        gate = auth.AuthGate(mode="enforce", clock=self.clock, verifier=self.verifier,
                             replay=self.replay)
        verdict = gate.authenticate(
            method="GET", framing=make_framing(), headers=self.headers(), body=b"")
        self.assertVerdict(verdict, auth.OUTCOME_UNAVAILABLE,
                           auth.REASON_REGISTRY_UNAVAILABLE)

    def test_signers_not_ok_is_verifier_unavailable(self):
        self.gate.fail_publish(self.registry)
        self.assertVerdict(self.authenticate(self.headers()),
                           auth.OUTCOME_UNAVAILABLE, auth.REASON_VERIFIER_UNAVAILABLE)
        self.assertEqual(self.verifier.calls, 0)

    def test_retired_signers_cannot_be_acquired(self):
        old = self.signers
        # 低-1 後只有「換世代」會 retire（同世代重發布不 retire，否則會刪到新檔）；
        # 這裡用真的下一世代，維持原斷言「已 retire 的檔不得再被取用」。
        newer = registry_bytes(2, [key_entry("k-a", self.signer)])
        registry = parse_registry(newer)
        new = sshsig.write_allowed_signers(
            registry.active_keys(), self.dir, registry.generation)
        self.gate.publish(registry, new)
        self.assertFalse(old.acquire(), msg="已 retire 的檔不得再被取用")

    def test_same_generation_republish_keeps_the_new_file(self):
        """低-1（R3）：同一世代重新發布不得 retire 掉剛用 os.replace 換上的同路徑新檔。"""
        real = sshsig.Verifier(
            spool_dir=self.dir, sshkeygen_path="/usr/bin/ssh-keygen",
            owner_uid=os.getuid())
        self.gate.verifier = real
        again = sshsig.write_allowed_signers(
            self.registry.active_keys(), self.dir, self.registry.generation)
        self.assertEqual(again.path, self.signers.path, msg="同一世代＝同一路徑")
        self.gate.publish(self.registry, again)
        self.assertTrue(os.path.exists(again.path), msg="同世代重發布後檔案不得消失")
        self.assertVerdict(self.authenticate(self.headers()), auth.OUTCOME_PASS)

    def test_S1_exception_inside_decide_releases_the_signers_reference(self):
        """S1（agy 審查）：`_decide` 丟例外時 `SignersFile` 引用必須歸還，不洩漏。"""
        signers = self.gate.snapshot()[1]
        before = signers.refs
        self.assertEqual(before, 0)
        with mock.patch.object(sshsig, "preparse", side_effect=RuntimeError("inject")):
            with self.assertRaises(RuntimeError):
                self.authenticate(self.headers())
        self.assertEqual(signers.refs, before, msg="例外路徑也必須 release 引用")
        # 洩漏的話下一次請求就會卡在 refs，這裡確認恢復後照常可驗。
        self.assertVerdict(self.authenticate(self.headers(nonce=nonce(9))),
                           auth.OUTCOME_PASS)


class ModuleBoundaryTests(unittest.TestCase):
    """design :320 的依賴方向：`auth` 不 import http／main／registry；lgwd 不 import tests。"""

    def test_auth_imports_only_allowed_modules(self):
        source = (REPO_ROOT / "lgwd" / "auth.py").read_text(encoding="utf-8")
        for forbidden in ("lgwd.http", "lgwd.main", "lgwd.registry", "tests."):
            self.assertNotIn("import " + forbidden, source, msg=forbidden)

    def test_lgwd_never_imports_the_test_signer(self):
        for path in (REPO_ROOT / "lgwd").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("auth_signer", source, msg=path.name)


class C603ApplyTableTests(unittest.TestCase):
    """C-603：`Verdict` × `mode` 全表（純函式；一處決定 401／429／503／continue）。"""

    def test_C603_full_table(self):
        table = {
            auth.OUTCOME_PASS: (auth.ACTION_CONTINUE, auth.ACTION_CONTINUE),
            auth.OUTCOME_DENY: (auth.ACTION_RESPOND, auth.ACTION_CONTINUE),
            auth.OUTCOME_RATE_LIMITED: (auth.ACTION_RESPOND, auth.ACTION_CONTINUE),
            auth.OUTCOME_UNAVAILABLE: (auth.ACTION_RESPOND, auth.ACTION_CONTINUE),
        }
        verdicts = {
            auth.OUTCOME_PASS: auth.passed("p", principal_all_entry()),
            auth.OUTCOME_DENY: auth.denied(auth.REASON_MALFORMED),
            auth.OUTCOME_RATE_LIMITED: auth.limited(9),
            auth.OUTCOME_UNAVAILABLE: auth.unavailable(auth.REASON_BUSY),
        }
        for outcome, verdict in verdicts.items():
            with self.subTest(outcome=outcome, mode="enforce"):
                action = auth.apply("enforce", verdict)
                self.assertEqual(action.kind, table[outcome][0])
            with self.subTest(outcome=outcome, mode="warn"):
                action = auth.apply("warn", verdict)
                self.assertEqual(action.kind, table[outcome][1])
            with self.subTest(outcome=outcome, mode="off"):
                self.assertEqual(auth.apply("off", verdict).kind, auth.ACTION_CONTINUE)

    def test_C603_enforce_statuses_and_retry_after(self):
        denied = auth.apply("enforce", auth.denied(auth.REASON_EXPIRED))
        self.assertEqual((denied.status, denied.error.code), (401, "unauthorized"))
        self.assertEqual(denied.headers, ())
        limited = auth.apply("enforce", auth.limited(66))
        self.assertEqual((limited.status, limited.error.code), (429, "rate_limited"))
        self.assertEqual(limited.headers, (("Retry-After", "66"),))
        unavailable = auth.apply("enforce", auth.unavailable(auth.REASON_BUSY))
        self.assertEqual((unavailable.status, unavailable.error.code),
                         (503, "auth_unavailable"))

    def test_C603_warn_never_closes_except_unread_body(self):
        unavailable = auth.unavailable(auth.REASON_BUSY)
        self.assertEqual(auth.apply("warn", unavailable).kind, auth.ACTION_CONTINUE)
        self.assertEqual(
            auth.apply("warn", unavailable, close_on_warn=True).kind, auth.ACTION_CLOSE)
        # enforce 讀不完 body 時照樣回 503，不關閉（回應寫得出去）。
        self.assertEqual(
            auth.apply("enforce", unavailable, close_on_warn=True).status, 503)

    def test_C603_warn_pass_and_would_deny_are_logged(self):
        lines = []
        log = auth.AuthLog(sink=lines.append)
        auth.apply("warn", auth.denied(auth.REASON_BAD_SIGNATURE),
                   route="/v1/resolve", log=log)
        auth.apply("warn", auth.passed("p", principal_all_entry()),
                   route="/v1/resolve", log=log)
        self.assertEqual(len(lines), 2)
        self.assertIn("mode=warn", lines[0])
        self.assertIn("outcome=deny", lines[0])
        self.assertIn("reason=bad_signature", lines[0])
        self.assertIn("route=/v1/resolve", lines[0])
        self.assertIn("outcome=pass", lines[1])
        self.assertIn("principal=p", lines[1])

    def test_C603_off_does_not_log(self):
        lines = []
        log = auth.AuthLog(sink=lines.append)
        auth.apply("off", auth.passed("p", principal_all_entry()), log=log)
        auth.apply("off", auth.denied(auth.REASON_MISSING), log=log)
        self.assertEqual(lines, [])


class C604UnauthorizedBodyTests(unittest.TestCase):
    """C-604：enforce 的 401 body 對所有 reason 逐 byte 相同、沒有 WWW-Authenticate。"""

    def test_C604_all_reasons_are_identical(self):
        bodies = set()
        # 授權原因（forbidden_op／forbidden_agent／forbidden_role）屬於 `forbidden`
        # 結果（D3a／C-906），不在 401 的這張表裡；它們的 body 由下面 D3a 的測試鎖住。
        for reason in sorted(auth.REASONS - auth.AUTHORIZATION_REASONS):
            action = auth.apply("enforce", auth.denied(reason))
            self.assertEqual(action.status, 401)
            self.assertEqual(action.headers, (), msg="401 不得帶任何額外標頭")
            bodies.add(json.dumps(action.error.body(), ensure_ascii=False,
                                  separators=(", ", ": ")).encode("utf-8"))
        self.assertEqual(len(bodies), 1)
        self.assertEqual(json.loads(bodies.pop()), {"error": "unauthorized"})

    def test_C604_whitelist_is_fixed(self):
        self.assertEqual(
            set(auth.REASONS),
            {
                "framing", "missing", "malformed", "unknown_key", "revoked",
                "expired", "water", "bad_signature", "replay", "rate_limited",
                "busy", "store_unavailable", "verifier_unavailable",
                "registry_unavailable",
                # D3a／C-906：三個授權原因加進白名單（只屬於 `forbidden` 結果）。
                "forbidden_op", "forbidden_agent", "forbidden_role",
            },
        )

    def test_C604_off_whitelist_reason_is_rejected(self):
        for bad in ("Reject.kind", "bad_signature\n", "", "PASS", "unavailable"):
            with self.subTest(reason=bad):
                with self.assertRaises(ValueError):
                    auth.Verdict(auth.OUTCOME_DENY, bad)


class C607LogTests(unittest.TestCase):
    """C-607：60 秒聚合、白名單、不洩密、off 只記啟動一行（http 面見另一支）。"""

    def setUp(self):
        self.now = 1000.0
        self.lines: list[str] = []
        self.log = auth.AuthLog(sink=self.lines.append,
                                clock=lambda: self.now, window=60.0)

    def record(self, **over):
        kwargs = dict(mode="enforce", outcome="deny", reason="malformed",
                      route="/v1/resolve", principal=None)
        kwargs.update(over)
        self.log.record(**kwargs)

    def test_C607_same_key_aggregates_within_sixty_seconds(self):
        for _ in range(5):
            self.record()
            self.now += 1.0
        self.assertEqual(len(self.lines), 1)
        self.assertIn("count=1", self.lines[0])
        # 視窗過了：先補上一個視窗的累計，再開新視窗。
        self.now += 60.0
        self.record()
        self.assertEqual(len(self.lines), 3)
        self.assertIn("count=5", self.lines[1])
        self.assertIn("count=1", self.lines[2])

    def test_C607_different_keys_are_separate(self):
        self.record(reason="malformed")
        self.record(reason="replay")
        self.record(route="/v1/usage")
        self.record(principal="p", outcome="pass", reason="")
        self.assertEqual(len(self.lines), 4)

    def test_C607_reason_outside_whitelist_is_not_logged_verbatim(self):
        self.record(reason="signature_not_canonical")  # Reject.kind 之類的表外值
        self.assertIn("reason=malformed", self.lines[0])
        self.assertNotIn("signature_not_canonical", self.lines[0])

    def test_C607_line_has_no_secrets(self):
        self.record(mode="warn", outcome="deny", reason="framing",
                    route="/v1/roles/{role}", principal=None)
        line = self.lines[0]
        self.assertEqual(
            line,
            "auth mode=warn outcome=deny reason=framing route=/v1/roles/{role} "
            "principal=- count=1",
        )


if __name__ == "__main__":
    unittest.main()
