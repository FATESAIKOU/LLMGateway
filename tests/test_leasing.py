"""LeaseService 測試（只用標準函式庫 unittest）。

- 覆蓋 specs/agent-lease 全部「服務端」Scenario，方法名照 Scenario 名。
- launcher 端的 Scenario（值不上命令列、短暫限流不換把、通知管道失效、
  只記錄階段、強制階段的 launcher 行為）由 MyAiStorage 交接（tasks 2.4）與驗收（4.5），這裡不測。
- policy／keys／build_env 用測試檔內的假替身（reviewer 的實作完成後再換真品）。
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import tempfile
import threading
import unittest

from lgwd import leasing
from lgwd.clock import FakeClock, from_rfc3339, to_rfc3339
from lgwd.errors import ApiError
from lgwd.keys import KeyStore
from lgwd.policy import Policy, SubscriptionPolicy

CANARY = {
    "claude-personal": "CANARY-CLAUDE-PERSONAL-9f8e7d6c5b4a",
    "claude-company": "CANARY-CLAUDE-COMPANY-1a2b3c4d5e6f",
    "openrouter": "CANARY-OPENROUTER-a1b2c3d4e5f6",
    "opencode-zen": "CANARY-ZEN-ffeeddccbbaa",
    "ollama-cloud": "CANARY-OLLAMA-112233445566",
}


class FakePolicy(Policy):
    def allows(self, subscription, agent, model):
        sub = self.subscriptions.get(subscription)
        if sub is None or agent not in sub.agents:
            return False
        if model is None:
            return True
        return any(fnmatch.fnmatchcase(model, pat) for pat in sub.model_allow)

    def candidates(self, agent):
        return self.order.get(agent, ())


class FakeKeyStore(KeyStore):
    def __init__(self, keys):
        self._keys = dict(keys)

    def get(self, subscription):
        return self._keys.get(subscription)

    def deployed(self, subscription):
        v = self._keys.get(subscription)
        return v is not None and v != ""

    @property
    def last_reload_error(self):
        return None


def fake_build_env(subscription, agent, key, model):
    """照 lgwd/envmap.py 檔頭的對照表；不允許的組合丟 ValueError。"""
    outsider = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    if subscription in ("claude-personal", "claude-company"):
        if agent != "claude-code":
            raise ValueError("not allowed")
        env = {"CLAUDE_CODE_OAUTH_TOKEN": key}
        unset = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    elif subscription == "openrouter":
        if agent == "claude-code":
            env = {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                   "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": ""}
            unset = ["CLAUDE_CODE_OAUTH_TOKEN"]
        elif agent == "opencode":
            env = {"OPENROUTER_API_KEY": key}
            unset = list(outsider)
        else:
            raise ValueError("not allowed")
    elif subscription == "ollama-cloud":
        if agent == "claude-code":
            env = {"ANTHROPIC_BASE_URL": "https://ollama.com",
                   "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": ""}
            unset = ["CLAUDE_CODE_OAUTH_TOKEN"]
        elif agent == "opencode":
            env = {"OLLAMA_API_KEY": key}
            unset = list(outsider)
        else:
            raise ValueError("not allowed")
    elif subscription == "opencode-zen":
        if agent != "opencode":
            raise ValueError("not allowed")
        env = {"OPENCODE_API_KEY": key}
        unset = list(outsider)
    else:
        raise ValueError("not allowed")
    if model is not None:
        env["ANTHROPIC_MODEL" if agent == "claude-code" else "LGW_MODEL"] = model
    return env, unset


def make_policy():
    subs = {
        "claude-personal": SubscriptionPolicy("claude-personal", ("claude-code",),
                                              ("*",), "claude-sonnet", 2, 5 * 3600, 24 * 3600),
        "claude-company": SubscriptionPolicy("claude-company", ("claude-code",),
                                             ("*",), "claude-sonnet", 2, 5 * 3600, 24 * 3600),
        "openrouter": SubscriptionPolicy("openrouter", ("opencode",),
                                         ("*:free",), "some-model:free", None, 3600, 24 * 3600),
        "opencode-zen": SubscriptionPolicy("opencode-zen", ("opencode",),
                                           ("*",), "zen-default", None, 3600, 24 * 3600),
        "ollama-cloud": SubscriptionPolicy("ollama-cloud", ("opencode",),
                                           ("*",), "ollama-default", None, 5 * 3600, 24 * 3600),
    }
    order = {"claude-code": ("claude-personal", "claude-company"),
             "opencode": ("opencode-zen", "openrouter", "ollama-cloud")}
    return FakePolicy(subscriptions=subs, order=order, cooldown_buffer_seconds=120,
                      lease_ttl_seconds=12 * 3600, confirm_within_seconds=600,
                      resets_at_max_ahead_seconds=8 * 24 * 3600)


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.policy = make_policy()
        self.keys = FakeKeyStore(dict(CANARY))
        self._orig_build_env = leasing.build_env
        leasing.build_env = fake_build_env
        self.svc = leasing.LeaseService(":memory:", self.policy, self.keys, self.clock)
        self.svc.add_client("w1")
        self._n = 0

    def tearDown(self):
        leasing.build_env = self._orig_build_env
        self.svc.close()

    # --- 小工具 ---
    def _rid(self, prefix="req"):
        self._n += 1
        return f"{prefix}-{self._n}"

    def req(self, client="w1", **kw):
        body = {"worker": client, "role": "role-1", "agent": "claude-code",
                "instance": "inst-1", "request_id": self._rid()}
        body.update(kw)
        return self.svc.request_lease(client, body)

    def active_lease(self, client="w1", instance="i1", **kw):
        st, resp = self.req(client=client, instance=instance, **kw)
        self.assertEqual(st, 201)
        lid = resp["lease"]["lease_id"]
        st, _ = self.svc.start(client, lid, {"request_id": self._rid("start")})
        self.assertEqual(st, 200)
        return lid

    def events(self):
        return self.svc._store.all_events()

    def assertRefused(self, ctx, reason):  # noqa: N802
        with self.assertRaises(ApiError) as cm:
            ctx() if callable(ctx) else ctx
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.code, "refused")
        self.assertEqual(cm.exception.extra.get("reason"), reason)
        return cm.exception

    # --- 租約欄位 ---
    def test_申請內容齊全(self):
        st, resp = self.req()
        self.assertEqual(st, 201)
        lease = resp["lease"]
        for k in ("lease_id", "subscription", "agent", "model", "env",
                  "unset", "expires_at", "confirm_by"):
            self.assertIn(k, lease)
        self.assertEqual(lease["subscription"], "claude-personal")
        self.assertEqual(lease["model"], "claude-sonnet")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", lease["env"])
        from_rfc3339(lease["confirm_by"])
        blob = json.dumps(self.events())
        for v in CANARY.values():
            self.assertNotIn(v, blob)

    def test_欄位缺漏(self):
        with self.assertRaises(ApiError) as cm:
            self.svc.request_lease("w1", {"worker": "w1", "request_id": "x"})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.extra["missing"], ["agent", "instance", "role"])
        lease_types = {"requested", "granted", "denied", "started", "renewed",
                       "released", "usage", "expired", "superseded", "cooling",
                       "lease_revoked"}
        self.assertEqual([e for e in self.events() if e["type"] in lease_types], [])

    def test_欄位長度與可列印(self):
        with self.assertRaises(ApiError) as cm:
            self.req(worker="w" * 65)
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ApiError) as cm:
            self.req(role="a\nb")
        self.assertEqual(cm.exception.status, 400)

    def test_帶_preferences(self):
        prefs = {"preferred_models": ["m1"], "floor_model": "m0"}
        _, with_p = self.req(preferences=prefs, instance="a")
        _, without = self.req(instance="b")
        self.assertEqual(with_p["lease"]["subscription"], without["lease"]["subscription"])
        granted = [e for e in self.events() if e["type"] == "granted"]
        prefs_seen = [json.loads(e["payload"])["preferences"] for e in granted]
        self.assertIn(prefs, prefs_seen)

    def test_preferences格式錯誤(self):
        with self.assertRaises(ApiError) as cm:
            self.req(preferences="x")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ApiError) as cm:
            self.req(preferences={"preferred_models": "x"})
        self.assertEqual(cm.exception.status, 400)

    def test_同一個_agent_實例申請兩次(self):
        self.req(instance="same")
        err = self.assertRefused(lambda: self.req(instance="same"), "instance_busy")
        denied = [e for e in self.events() if e["type"] == "denied"]
        self.assertTrue(any(json.loads(e["payload"])["reason"] == "instance_busy"
                            for e in denied))
        self.assertNotIn("earliest_reset", err.body())

    def test_客戶端冒用worker名稱(self):
        with self.assertRaises(ApiError) as cm:
            self.req(client="w1", worker="other")
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(cm.exception.code, "client_mismatch")

    # --- 依政策選定訂閱 ---
    def test_不指定訂閱(self):
        _, resp = self.req()
        self.assertEqual(resp["lease"]["subscription"], "claude-personal")
        granted = [json.loads(e["payload"]) for e in self.events() if e["type"] == "granted"]
        self.assertTrue(granted and granted[0]["reason"])

    def test_指定訂閱只看那一把(self):
        _, resp = self.req(subscription="claude-company")
        self.assertEqual(resp["lease"]["subscription"], "claude-company")

    def test_一個訂閱在冷卻中(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1))
        self.svc.exhausted("w1", lid, {"signal": "claude_limit", "resets_at": resets,
                                       "request_id": self._rid("ex")})
        _, resp = self.req(instance="b")
        self.assertEqual(resp["lease"]["subscription"], "claude-company")

    def test_全部都在冷卻中(self):
        la = self.active_lease(instance="a")
        ra = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1))
        self.svc.exhausted("w1", la, {"signal": "claude_limit", "resets_at": ra,
                                      "request_id": self._rid("ex")})
        lb = self.active_lease(instance="b", subscription="claude-company")
        rb = to_rfc3339(self.clock.now() + _dt.timedelta(hours=2))
        self.svc.exhausted("w1", lb, {"signal": "claude_limit", "resets_at": rb,
                                      "request_id": self._rid("ex")})
        err = self.assertRefused(lambda: self.req(instance="c"), "all_cooling")
        self.assertEqual(err.extra["earliest_reset"], to_rfc3339(
            from_rfc3339(ra) + _dt.timedelta(seconds=120)))

    def test_指定冷卻中的訂閱(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1))
        self.svc.exhausted("w1", lid, {"signal": "claude_limit", "resets_at": resets,
                                       "request_id": self._rid("ex")})
        err = self.assertRefused(
            lambda: self.req(instance="b", subscription="claude-personal"), "all_cooling")
        self.assertIn("earliest_reset", err.extra)

    def test_模型不在允許清單(self):
        self.assertRefused(
            lambda: self.req(agent="opencode", subscription="openrouter",
                             model="gpt-4", instance="m"), "not_allowed")

    def test_冷卻結束後回到第一順位(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1))
        self.svc.exhausted("w1", lid, {"signal": "claude_limit", "resets_at": resets,
                                       "request_id": self._rid("ex")})
        self.clock.advance(hours=1, minutes=3)
        _, resp = self.req(instance="b")
        self.assertEqual(resp["lease"]["subscription"], "claude-personal")

    def test_未知agent(self):
        self.assertRefused(lambda: self.req(agent="nope"), "not_allowed")

    def test_全部未部署(self):
        svc = leasing.LeaseService(":memory:", self.policy, FakeKeyStore({}), self.clock)
        svc.add_client("w1")
        try:
            with self.assertRaises(ApiError) as cm:
                svc.request_lease("w1", {"worker": "w1", "role": "r", "agent": "claude-code",
                                         "instance": "i", "request_id": "x"})
            self.assertEqual(cm.exception.extra.get("reason"), "not_allowed")
        finally:
            svc.close()

    # --- 憑證由 launcher 注入（服務端部分） ---
    def test_只注入一把(self):
        _, resp = self.req(subscription="claude-company")
        env = resp["lease"]["env"]
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), CANARY["claude-company"])
        for v in CANARY.values():
            if v != CANARY["claude-company"]:
                self.assertNotIn(v, json.dumps(env))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_憑證來源缺失(self):
        keys = FakeKeyStore({k: v for k, v in CANARY.items() if k != "opencode-zen"})
        svc = leasing.LeaseService(":memory:", self.policy, keys, self.clock)
        svc.add_client("w1")
        try:
            st, resp = svc.request_lease("w1", {"worker": "w1", "role": "r", "agent": "opencode",
                                                "instance": "i", "request_id": "x"})
            self.assertEqual(st, 201)
            self.assertEqual(resp["lease"]["subscription"], "openrouter")
            board = svc.board()
            zen = [s for s in board["subscriptions"] if s["name"] == "opencode-zen"][0]
            self.assertFalse(zen["deployed"])
        finally:
            svc.close()

    def test_強制階段沒有租約(self):
        lid = self.active_lease(instance="a")
        lb = self.active_lease(instance="b", subscription="claude-company")
        for x in (lid, lb):
            self.svc.exhausted("w1", x, {"signal": "claude_limit",
                                         "request_id": self._rid("ex")})
        err = self.assertRefused(lambda: self.req(instance="c"), "all_cooling")
        self.assertIn("earliest_reset", err.extra)

    def test_opencode不能拿Claude訂閱(self):
        self.assertRefused(
            lambda: self.req(agent="opencode", subscription="claude-personal",
                             instance="z"), "not_allowed")

    # --- 訂閱切換 ---
    def test_一個訂閱額度用完(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1, minutes=45))
        st, resp = self.svc.exhausted("w1", lid, {"signal": "claude_limit",
                                                 "resets_at": resets,
                                                 "request_id": self._rid("ex")})
        self.assertEqual(st, 200)
        self.assertEqual(resp["next"]["subscription"], "claude-company")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", resp["next"]["env"])
        expect_until = to_rfc3339(from_rfc3339(resets) + _dt.timedelta(seconds=120))
        sup = [json.loads(e["payload"]) for e in self.events() if e["type"] == "superseded"]
        self.assertEqual(sup[0]["cooling_until"], expect_until)
        with self._store() as cur:
            row = cur.execute("SELECT status FROM leases WHERE lease_id = ?",
                              (lid,)).fetchone()
            self.assertEqual(row["status"], "superseded")

    def test_沒有重置時刻(self):
        lid = self.active_lease(instance="a")
        before = self.clock.now()
        self.svc.exhausted("w1", lid, {"signal": "other", "request_id": self._rid("ex")})
        board = self.svc.board()
        personal = [s for s in board["subscriptions"] if s["name"] == "claude-personal"][0]
        self.assertTrue(personal["cooling"])
        self.assertEqual(personal["cooling_until"],
                         to_rfc3339(before + _dt.timedelta(hours=5)))

    def test_過去與太遠的重置時刻用預設(self):
        lid = self.active_lease(instance="a")
        past = to_rfc3339(self.clock.now() - _dt.timedelta(hours=1))
        self.svc.exhausted("w1", lid, {"signal": "other", "resets_at": past,
                                       "request_id": self._rid("ex")})
        lid2 = self.active_lease(instance="b", subscription="claude-company")
        far = to_rfc3339(self.clock.now() + _dt.timedelta(days=9))
        self.svc.exhausted("w1", lid2, {"signal": "other", "resets_at": far,
                                        "request_id": self._rid("ex")})
        board = self.svc.board()
        by_name = {s["name"]: s for s in board["subscriptions"]}
        self.assertEqual(by_name["claude-personal"]["cooling_until"],
                         to_rfc3339(self.clock.now() + _dt.timedelta(hours=5)))
        self.assertEqual(by_name["claude-company"]["cooling_until"],
                         to_rfc3339(self.clock.now() + _dt.timedelta(hours=5)))

    def test_可用的訂閱都用完(self):
        la = self.active_lease(instance="a")
        self.svc.exhausted("w1", la, {"signal": "claude_limit", "request_id": self._rid("ex")})
        lb = self.active_lease(instance="b", subscription="claude-company")
        st, resp = self.svc.exhausted("w1", lb, {"signal": "claude_limit",
                                                "request_id": self._rid("ex")})
        self.assertEqual(st, 200)
        self.assertIsNone(resp["next"])
        self.assertEqual(resp["refusal"]["reason"], "all_cooling")
        self.assertIn("earliest_reset", resp["refusal"])

    def test_切換後新開session(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1))
        self.svc.exhausted("w1", lid, {"signal": "claude_limit", "resets_at": resets,
                                       "request_id": self._rid("ex")})
        _, r1 = self.req(instance="new-1")
        self.assertEqual(r1["lease"]["subscription"], "claude-company")
        self.clock.advance(hours=1, minutes=3)
        _, r2 = self.req(instance="new-2")
        self.assertEqual(r2["lease"]["subscription"], "claude-personal")

    def test_冷卻結束不打斷(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(minutes=30))
        st, resp = self.svc.exhausted("w1", lid, {"signal": "claude_limit",
                                                 "resets_at": resets,
                                                 "request_id": self._rid("ex")})
        other = resp["next"]["lease_id"]
        self.svc.start("w1", other, {"request_id": self._rid("start")})
        self.clock.advance(hours=1)
        st, body = self.svc.renew("w1", other, {"request_id": self._rid("rn")})
        self.assertEqual(st, 200)
        _, r = self.req(instance="fresh")
        self.assertEqual(r["lease"]["subscription"], "claude-personal")

    def test_非法signal(self):
        lid = self.active_lease(instance="a")
        with self.assertRaises(ApiError) as cm:
            self.svc.exhausted("w1", lid, {"signal": "nope", "request_id": self._rid("ex")})
        self.assertEqual(cm.exception.status, 400)

    # --- 同時租用上限 ---
    def test_達到上限(self):
        self.active_lease(instance="a")
        self.active_lease(instance="b")
        _, r = self.req(instance="c")
        self.assertEqual(r["lease"]["subscription"], "claude-company")
        self.active_lease(instance="d", subscription="claude-company")
        self.assertRefused(lambda: self.req(instance="e"), "cap_reached")

    def test_指定達上限的訂閱(self):
        self.active_lease(instance="a")
        self.active_lease(instance="b")
        self.assertRefused(
            lambda: self.req(instance="c", subscription="claude-personal"), "cap_reached")

    def test_結束後釋出(self):
        la = self.active_lease(instance="a")
        self.active_lease(instance="b")
        self.svc.release("w1", la, {"request_id": self._rid("rel")})
        _, r = self.req(instance="c")
        self.assertEqual(r["lease"]["subscription"], "claude-personal")

    # --- 啟動後確認 ---
    def test_正常確認(self):
        st, resp = self.req(instance="a")
        lid = resp["lease"]["lease_id"]
        before = self.clock.now()
        st, body = self.svc.start("w1", lid, {"request_id": self._rid("start")})
        self.assertEqual(st, 200)
        self.assertEqual(body["expires_at"],
                         to_rfc3339(before + _dt.timedelta(hours=12)))
        board = self.svc.board()
        personal = [s for s in board["subscriptions"] if s["name"] == "claude-personal"][0]
        self.assertEqual(personal["active"][0]["since"], to_rfc3339(before))

    def test_確認前不能續期或回報額度(self):
        _, resp = self.req(instance="a")
        lid = resp["lease"]["lease_id"]
        with self.assertRaises(ApiError) as cm:
            self.svc.renew("w1", lid, {"request_id": self._rid("rn")})
        self.assertEqual(cm.exception.code, "lease_not_confirmed")
        with self.assertRaises(ApiError) as cm:
            self.svc.exhausted("w1", lid, {"signal": "other", "request_id": self._rid("ex")})
        self.assertEqual(cm.exception.code, "lease_not_confirmed")
        st, _ = self.svc.start("w1", lid, {"request_id": self._rid("start")})
        self.assertEqual(st, 200)

    def test_換把後沒開起來(self):
        lid = self.active_lease(instance="a")
        st, resp = self.svc.exhausted("w1", lid, {"signal": "other",
                                                 "request_id": self._rid("ex")})
        nxt = resp["next"]["lease_id"]
        self.clock.advance(minutes=10)
        with self.assertRaises(ApiError) as cm:
            self.svc.start("w1", nxt, {"request_id": self._rid("start")})
        self.assertEqual(cm.exception.code, "lease_not_active")
        _, r = self.req(instance="b", subscription="claude-company")
        self.assertEqual(r["lease"]["subscription"], "claude-company")

    def test_確認邊界_等於十分鐘即作廢(self):
        _, resp = self.req(instance="a")
        lid = resp["lease"]["lease_id"]
        self.clock.advance(seconds=600)
        with self.assertRaises(ApiError) as cm:
            self.svc.start("w1", lid, {"request_id": self._rid("start")})
        self.assertEqual(cm.exception.code, "lease_not_active")

    # --- 生命週期其他 ---
    def test_start重送(self):
        lid = self.active_lease(instance="a")
        with self._store() as cur:
            exp = cur.execute("SELECT expires_at FROM leases WHERE lease_id = ?",
                              (lid,)).fetchone()["expires_at"]
        st, body = self.svc.start("w1", lid, {"request_id": self._rid("start")})
        self.assertEqual((st, body["expires_at"]), (200, exp))

    def test_release重送(self):
        lid = self.active_lease(instance="a")
        for _ in range(2):
            st, body = self.svc.release("w1", lid, {"request_id": self._rid("rel")})
            self.assertEqual((st, body), (204, {}))
        rel = [e for e in self.events() if e["type"] == "released"]
        self.assertEqual(len(rel), 1)

    def test_renew冷卻中可續(self):
        la = self.active_lease(instance="a")
        lb = self.active_lease(instance="b")
        self.svc.exhausted("w1", la, {"signal": "other", "request_id": self._rid("ex")})
        st, _ = self.svc.renew("w1", lb, {"request_id": self._rid("rn")})
        self.assertEqual(st, 200)

    def test_租約到期自動expired(self):
        lid = self.active_lease(instance="a")
        self.clock.advance(hours=12)
        with self.assertRaises(ApiError) as cm:
            self.svc.renew("w1", lid, {"request_id": self._rid("rn")})
        self.assertEqual(cm.exception.code, "lease_not_active")
        board = self.svc.board()
        personal = [s for s in board["subscriptions"] if s["name"] == "claude-personal"][0]
        self.assertEqual(personal["active"], [])

    def test_他人租約與不存在(self):
        self.svc.add_client("w2")
        lid = self.active_lease(client="w1", instance="a")
        with self.assertRaises(ApiError) as cm:
            self.svc.start("w2", lid, {"request_id": "x"})
        self.assertEqual(cm.exception.code, "not_owner")
        with self.assertRaises(ApiError) as cm:
            self.svc.start("w1", "nope", {"request_id": "y"})
        self.assertEqual(cm.exception.code, "not_found")

    def test_usage快照(self):
        lid = self.active_lease(instance="a")
        st, body = self.svc.report_usage("w1", lid, {"snapshot": {"a": 1},
                                                    "request_id": self._rid("u")})
        self.assertEqual((st, body), (204, {}))
        with self.assertRaises(ApiError) as cm:
            self.svc.report_usage("w1", lid, {"snapshot": [1]})
        self.assertEqual(cm.exception.status, 400)

    # --- key-vault 服務端 ---
    def test_撤銷(self):
        token2 = self.svc.add_client("w2")
        lid = self.active_lease(client="w2", instance="a")
        self.svc.revoke_client("w2")
        self.assertIsNone(self.svc.authenticate(token2))
        # 已撤銷客戶端的一切操作都被拒（401；HTTP 層同理）。
        with self.assertRaises(ApiError) as cm:
            self.svc.start("w2", lid, {"request_id": "x"})
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(ApiError) as cm:
            self.req(client="w2", instance="b")
        self.assertEqual(cm.exception.status, 401)
        names = [c["name"] for c in self.svc.list_clients()]
        self.assertIn("w1", names)
        _, r = self.req(client="w1", instance="other")
        self.assertEqual(r["lease"]["subscription"], "claude-personal")

    def test_沒有憑證(self):
        self.assertIsNone(self.svc.authenticate("bad"))
        self.assertIsNone(self.svc.authenticate(""))
        names_hashes = json.dumps(self.svc.list_clients())
        for v in CANARY.values():
            self.assertNotIn(v, names_hashes)

    # --- 撤銷後重送一律 401 ---
    def test_撤銷後重送每一種動作都回401(self):
        bodies = {}
        bodies["request"] = {"worker": "w1", "role": "r", "agent": "claude-code",
                             "instance": "i", "request_id": "rr"}
        _, r = self.svc.request_lease("w1", dict(bodies["request"]))
        lid = r["lease"]["lease_id"]
        bodies["start"] = {"request_id": "rs"}
        self.svc.start("w1", lid, dict(bodies["start"]))
        bodies["exhausted"] = {"signal": "other", "request_id": "re"}
        _, ex = self.svc.exhausted("w1", lid, dict(bodies["exhausted"]))
        nxt = ex["next"]["lease_id"]
        self.svc.start("w1", nxt, {"request_id": "rs2"})
        bodies["renew"] = {"request_id": "rn"}
        self.svc.renew("w1", nxt, dict(bodies["renew"]))
        bodies["usage"] = {"snapshot": {}, "request_id": "ru"}
        self.svc.report_usage("w1", nxt, dict(bodies["usage"]))
        bodies["release"] = {"request_id": "rl"}
        self.svc.revoke_client("w1")
        calls = [
            lambda: self.svc.request_lease("w1", dict(bodies["request"])),
            lambda: self.svc.start("w1", lid, dict(bodies["start"])),
            lambda: self.svc.exhausted("w1", lid, dict(bodies["exhausted"])),
            lambda: self.svc.renew("w1", nxt, dict(bodies["renew"])),
            lambda: self.svc.release("w1", nxt, dict(bodies["release"])),
            lambda: self.svc.report_usage("w1", nxt, dict(bodies["usage"])),
        ]
        for call in calls:
            with self.assertRaises(ApiError) as cm:
                call()
            self.assertEqual(cm.exception.status, 401)

    # --- S4：未確認數量 ---
    def test_未確認數量(self):
        board = self.svc.board()
        personal = [s for s in board["subscriptions"] if s["name"] == "claude-personal"][0]
        self.assertEqual((personal["count"], personal["pending"]), (0, 0))
        _, r = self.req(instance="a")
        personal = [s for s in self.svc.board()["subscriptions"]
                    if s["name"] == "claude-personal"][0]
        self.assertEqual((personal["count"], personal["pending"]), (0, 1))
        self.svc.start("w1", r["lease"]["lease_id"], {"request_id": "s1"})
        personal = [s for s in self.svc.board()["subscriptions"]
                    if s["name"] == "claude-personal"][0]
        self.assertEqual((personal["count"], personal["pending"]), (1, 0))

    # --- S5：exhausted 沿用 preferences ---
    def test_exhausted沿用preferences(self):
        prefs = {"preferred_models": ["m1"], "floor_model": "m0"}
        lid = self.active_lease(instance="a", preferences=prefs)
        self.svc.exhausted("w1", lid, {"signal": "other", "request_id": "ex1"})
        granted = [json.loads(e["payload"]) for e in self.events()
                   if e["type"] == "granted"]
        self.assertEqual(len(granted), 2)
        self.assertEqual(granted[1]["preferences"], prefs)

    # --- 去重 ---
    def test_request去重(self):
        body = {"worker": "w1", "role": "r", "agent": "claude-code",
                "instance": "i", "request_id": "same"}
        st1, r1 = self.svc.request_lease("w1", dict(body))
        st2, r2 = self.svc.request_lease("w1", dict(body))
        self.assertEqual((st1, st2), (201, 201))
        self.assertEqual(r1, r2)
        with self._store() as cur:
            n = cur.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_不同client同request_id互不干擾(self):
        self.svc.add_client("w2")
        mk = lambda c: {"worker": c, "role": "r", "agent": "claude-code",
                        "instance": "i", "request_id": "same"}
        _, r1 = self.svc.request_lease("w1", mk("w1"))
        _, r2 = self.svc.request_lease("w2", mk("w2"))
        self.assertNotEqual(r1["lease"]["lease_id"], r2["lease"]["lease_id"])

    def test_exhausted重送相同request_id(self):
        lid = self.active_lease(instance="a")
        body = {"signal": "other", "request_id": "ex-1"}
        st1, r1 = self.svc.exhausted("w1", lid, dict(body))
        st2, r2 = self.svc.exhausted("w1", lid, dict(body))
        self.assertEqual(r1, r2)
        self.assertEqual(st1, 200)
        with self._store() as cur:
            n = cur.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_exhausted不同request_id再報(self):
        lid = self.active_lease(instance="a")
        self.svc.exhausted("w1", lid, {"signal": "other", "request_id": "ex-1"})
        with self.assertRaises(ApiError) as cm:
            self.svc.exhausted("w1", lid, {"signal": "other", "request_id": "ex-2"})
        self.assertEqual(cm.exception.code, "lease_not_active")

    def test_去重跨重啟(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/t.db"
            svc = leasing.LeaseService(path, self.policy, self.keys, self.clock)
            svc.add_client("w1")
            body = {"worker": "w1", "role": "r", "agent": "claude-code",
                    "instance": "i", "request_id": "same"}
            _, r1 = svc.request_lease("w1", dict(body))
            svc.close()
            svc2 = leasing.LeaseService(path, self.policy, self.keys, self.clock)
            try:
                _, r2 = svc2.request_lease("w1", dict(body))
                self.assertEqual(r1, r2)
            finally:
                svc2.close()

    def test_重啟從events重建(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/t.db"
            svc = leasing.LeaseService(path, self.policy, self.keys, self.clock)
            token = svc.add_client("w1")
            _, r = svc.request_lease("w1", {"worker": "w1", "role": "r", "agent": "claude-code",
                                            "instance": "g", "request_id": "g1"})
            granted = r["lease"]["lease_id"]
            lid = self._active_on(svc, "w1", "a")
            svc.exhausted("w1", lid, {"signal": "other", "request_id": "ex1"})
            before = svc.board()
            svc.close()
            svc2 = leasing.LeaseService(path, self.policy, self.keys, self.clock)
            try:
                self.assertEqual(svc2.board(), before)
                self.assertEqual(svc2.authenticate(token), "w1")
                st, _ = svc2.start("w1", granted, {"request_id": "s1"})
                self.assertEqual(st, 200)
            finally:
                svc2.close()

    def _active_on(self, svc, client, instance):
        _, r = svc.request_lease(client, {"worker": client, "role": "r",
                                          "agent": "claude-code", "instance": instance,
                                          "request_id": self._rid()})
        lid = r["lease"]["lease_id"]
        svc.start(client, lid, {"request_id": self._rid("start")})
        return lid

    def _store(self):
        return self.svc._store.txn()

    def _db_text(self):
        """整庫每一張表、每一列的文字（canary 掃描用；表名來自自家 schema）。"""
        with self.svc._store._lock:
            conn = self.svc._store._conn
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'")]
            parts = []
            for t in tables:
                parts.append("TABLE " + t)
                for row in conn.execute("SELECT * FROM " + t):
                    parts.append(repr(tuple(row)))
        return "\n".join(parts)

    def _no_canary(self, *blobs):
        for v in CANARY.values():
            for blob in blobs:
                self.assertNotIn(v, blob)

    # --- 狀態板 ---
    def test_有人在用(self):
        self.active_lease(instance="a")
        self.active_lease(instance="b")
        board = self.svc.board()
        self.assertEqual(board["api_version"], 1)
        from_rfc3339(board["generated_at"])
        personal = [s for s in board["subscriptions"] if s["name"] == "claude-personal"][0]
        self.assertEqual(personal["count"], 2)
        self.assertEqual(personal["cap"], 2)
        self.assertTrue(personal["deployed"])
        self.assertFalse(personal["cooling"])
        self.assertIsNone(personal["cooling_until"])
        self.assertIsNone(personal["usage"])
        self.assertEqual(len(personal["active"]), 2)
        for a in personal["active"]:
            self.assertEqual(set(a), {"worker", "role", "agent", "since"})
            from_rfc3339(a["since"])

    def test_冷卻中(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1, minutes=47))
        self.svc.exhausted("w1", lid, {"signal": "claude_limit", "resets_at": resets,
                                       "request_id": self._rid("ex")})
        personal = [s for s in self.svc.board()["subscriptions"]
                    if s["name"] == "claude-personal"][0]
        expect = to_rfc3339(from_rfc3339(resets) + _dt.timedelta(seconds=120))
        self.assertTrue(personal["cooling"])
        self.assertEqual(personal["cooling_until"], expect)
        self.clock.advance(hours=1, minutes=50)
        personal = [s for s in self.svc.board()["subscriptions"]
                    if s["name"] == "claude-personal"][0]
        self.assertFalse(personal["cooling"])

    def test_冷卻邊界_等於結束時刻即結束(self):
        lid = self.active_lease(instance="a")
        resets = to_rfc3339(self.clock.now() + _dt.timedelta(hours=1))
        self.svc.exhausted("w1", lid, {"signal": "other", "resets_at": resets,
                                       "request_id": self._rid("ex")})
        self.clock.advance(hours=1, minutes=2)  # 正好等於 resets＋buffer
        _, resp = self.req(instance="b")
        self.assertEqual(resp["lease"]["subscription"], "claude-personal")

    def test_未部署(self):
        keys = FakeKeyStore({k: v for k, v in CANARY.items() if k != "opencode-zen"})
        svc = leasing.LeaseService(":memory:", self.policy, keys, self.clock)
        svc.add_client("w1")
        try:
            zen = [s for s in svc.board()["subscriptions"] if s["name"] == "opencode-zen"][0]
            self.assertFalse(zen["deployed"])
        finally:
            svc.close()

    def test_逾期的租約不算(self):
        self.active_lease(instance="a")
        self.clock.advance(hours=12)
        self.svc.board()  # 觸發延後結算
        personal = [s for s in self.svc.board()["subscriptions"]
                    if s["name"] == "claude-personal"][0]
        self.assertEqual(personal["active"], [])
        self.assertEqual(personal["count"], 0)

    def test_讀取JSON不含值(self):
        self.active_lease(instance="a")
        blob = json.dumps(self.svc.board())
        for v in CANARY.values():
            self.assertNotIn(v, blob)

    # --- 併發 ---
    def test_併發request搶上限(self):
        results, lock = [], threading.Lock()

        def one(i):
            try:
                st, _ = self.svc.request_lease(
                    "w1", {"worker": "w1", "role": "r", "agent": "claude-code",
                           "instance": f"t-{i}", "request_id": f"c-{i}",
                           "subscription": "claude-personal"})
                ok = st == 201
            except ApiError:
                ok = False
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 2)

    def test_併發exhausted單一冷卻與單一下一張(self):
        lid = self.active_lease(instance="a")
        outcomes, lock = [], threading.Lock()

        def one(i):
            try:
                st, resp = self.svc.exhausted("w1", lid, {"signal": "other",
                                                         "request_id": f"ex-{i}"})
                with lock:
                    outcomes.append(resp["next"]["lease_id"])
            except ApiError as e:
                with lock:
                    outcomes.append(e.code)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [o for o in outcomes if not o.startswith("lease_")]
        self.assertEqual(len(ids), 1)
        self.assertEqual(outcomes.count("lease_not_active"), 3)
        with self._store() as cur:
            n = cur.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]
        self.assertEqual(n, 2)

    # --- canary：值不進資料庫 ---
    def test_canary全表掃描(self):
        mk = lambda rid: {"worker": "w1", "role": "role-1", "agent": "claude-code",
                          "instance": "a", "request_id": rid}
        _, r = self.svc.request_lease("w1", mk("rid-a"))
        lid = r["lease"]["lease_id"]
        first_env = dict(r["lease"]["env"])
        _, r2 = self.svc.request_lease("w1", mk("rid-a"))  # 真重送：env 一樣
        self.assertEqual(r2["lease"]["env"], first_env)
        self.svc.start("w1", lid, {"request_id": "s1"})
        self.svc.report_usage("w1", lid, {"snapshot": {"x": 1}, "request_id": "u1"})
        self.svc.renew("w1", lid, {"request_id": "rn1"})
        st, ex = self.svc.exhausted("w1", lid, {"signal": "other", "request_id": "ex1"})
        nxt = ex["next"]["lease_id"]
        next_env = dict(ex["next"]["env"])
        # exhausted 重送：next 還在，env 一樣。
        _, ex2 = self.svc.exhausted("w1", lid, {"signal": "other", "request_id": "ex1"})
        self.assertEqual(ex2["next"]["env"], next_env)
        self.svc.start("w1", lid, {"request_id": "s1"})  # 無秘密的重送
        self.svc.release("w1", nxt, {"request_id": "rel1"})
        self.svc.release("w1", nxt, {"request_id": "rel1"})
        try:
            self.req(agent="opencode", subscription="claude-personal", instance="z")
        except ApiError:
            pass
        self._no_canary(self._db_text())
        self._no_canary(json.dumps(self.events()), json.dumps(self.svc.board()),
                        json.dumps(self.svc.list_clients()), repr(self.keys))

    def test_去重表只留變數名(self):
        _, r = self.req(instance="a")
        with self._store() as cur:
            rows = cur.execute("SELECT body FROM dedup").fetchall()
        self.assertTrue(rows)
        for row in rows:
            body = json.loads(row["body"])
            for slot in ("lease", "next"):
                lease = body.get(slot)
                if isinstance(lease, dict) and isinstance(lease.get("env"), dict):
                    self.assertEqual(set(lease["env"]), set(r["lease"]["env"]))
                    self.assertTrue(all(v == "" for v in lease["env"].values()))
        self._no_canary(self._db_text())

    def test_重送時租約已結束(self):
        body = {"worker": "w1", "role": "r", "agent": "claude-code",
                "instance": "i", "request_id": "same"}
        _, r = self.svc.request_lease("w1", dict(body))
        lid = r["lease"]["lease_id"]
        self.svc.release("w1", lid, {"request_id": "rel"})
        with self.assertRaises(ApiError) as cm:
            self.svc.request_lease("w1", dict(body))
        self.assertEqual(cm.exception.code, "lease_not_active")
        # exhausted 的重送看的是下一張：下一張結束了同樣 409。
        lid2 = self.active_lease(instance="b")
        _, ex = self.svc.exhausted("w1", lid2, {"signal": "other", "request_id": "ex9"})
        self.svc.release("w1", ex["next"]["lease_id"], {"request_id": "rel9"})
        with self.assertRaises(ApiError) as cm:
            self.svc.exhausted("w1", lid2, {"signal": "other", "request_id": "ex9"})
        self.assertEqual(cm.exception.code, "lease_not_active")

    def test_輪替後重送拿新值(self):
        body = {"worker": "w1", "role": "r", "agent": "claude-code",
                "instance": "i", "request_id": "same"}
        _, r1 = self.svc.request_lease("w1", dict(body))
        self.keys._keys["claude-personal"] = "CANARY-ROTATED-zzzz"
        _, r2 = self.svc.request_lease("w1", dict(body))
        self.assertEqual(r2["lease"]["lease_id"], r1["lease"]["lease_id"])
        self.assertEqual(r2["lease"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "CANARY-ROTATED-zzzz")
        self._no_canary(self._db_text())

    def test_缺key時重送回not_allowed(self):
        body = {"worker": "w1", "role": "r", "agent": "claude-code",
                "instance": "i", "request_id": "same"}
        self.svc.request_lease("w1", dict(body))
        del self.keys._keys["claude-personal"]
        with self.assertRaises(ApiError) as cm:
            self.svc.request_lease("w1", dict(body))
        self.assertEqual(cm.exception.extra.get("reason"), "not_allowed")


if __name__ == "__main__":
    unittest.main()
