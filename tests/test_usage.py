"""lgwd/usage.py（#60 tasks 3.1、3.2）：usage 形狀、stale、available_again_at、觸發、single-flight、退避、TTL。

取用量一律用假 fetcher（可控制成功／失敗／卡住）與 FakeClock；不連網、不起子行程。
"""

import datetime as _dt
import json
import threading
import unittest
from types import SimpleNamespace

from lgwd import usage as usage_mod
from lgwd.clock import FakeClock, from_rfc3339, to_rfc3339
from lgwd.policy import UsageSetting
from tests.usage_fakes import BASE, UTC, FakeFetcher, at, windows
from lgwd.usage import (
    FetchResult,
    UsageService,
    available_again_at,
    build_usage,
    failure,
)

SHAPE = {"status", "source", "fetched_at", "stale", "reason", "session", "weekly", "pipes"}


def make(fetcher=None, *, settings=None, deployed=True, sub="sub"):
    clock = FakeClock(BASE)
    fetcher = fetcher or FakeFetcher()
    state = SimpleNamespace(deployed=deployed, key="canary-usage-key")
    service = UsageService(
        clock, fetchers={sub: fetcher}, settings=settings or {},
        key_of=lambda s: state.key if state.deployed else None,
        deployed=lambda s: state.deployed)
    return service, fetcher, clock, state


class BuildUsageTest(unittest.TestCase):
    def entry(self, fetched=BASE, wins=None, reason=None):
        e = usage_mod._Entry()
        e.fetched_at, e.windows, e.last_reason = fetched, wins or windows(), reason
        return e

    def test_八個鍵恆在_ok(self):
        u = build_usage("s", self.entry(), at(seconds=10), 120)
        self.assertEqual(set(u), SHAPE)
        self.assertEqual(u["status"], "ok")
        self.assertIs(u["stale"], False)
        self.assertIsNone(u["reason"])
        self.assertEqual(u["session"]["remaining_percent"], 75.0)
        self.assertEqual(u["weekly"]["resets_at"], to_rfc3339(at(days=3)))
        self.assertEqual(u["fetched_at"], to_rfc3339(BASE))

    def test_沒取過_not_fetched_yet(self):
        u = build_usage("s", None, BASE, 120)
        self.assertEqual(u, {"status": "unknown", "source": "s", "fetched_at": None, "stale": None,
                             "reason": "not_fetched_yet", "session": None, "weekly": None, "pipes": []})

    def test_從沒成功過_reason帶最近失敗碼(self):
        e = usage_mod._Entry()
        e.last_reason = "upstream_error"
        u = build_usage("s", e, BASE, 120)
        self.assertEqual((u["status"], u["reason"], u["fetched_at"], u["stale"]),
                         ("unknown", "upstream_error", None, None))

    def test_過TTL_stale_true_reason為null(self):
        u = build_usage("s", self.entry(), at(minutes=3), 120)
        self.assertEqual((u["status"], u["stale"], u["reason"]), ("ok", True, None))

    def test_過TTL且最近失敗_reason帶失敗碼(self):
        u = build_usage("s", self.entry(reason="upstream_error"), at(minutes=3), 120)
        self.assertEqual((u["status"], u["stale"], u["reason"]), ("ok", True, "upstream_error"))

    def test_剛好TTL不算stale(self):
        u = build_usage("s", self.entry(), at(seconds=120), 120)
        self.assertIs(u["stale"], False)

    def test_六小時邊界(self):
        ok = build_usage("s", self.entry(), at(hours=6), 120)
        self.assertEqual(ok["status"], "ok")
        gone = build_usage("s", self.entry(), at(hours=6, minutes=1), 120)
        self.assertEqual((gone["status"], gone["reason"], gone["stale"], gone["session"], gone["weekly"]),
                         ("unknown", "stale_expired", True, None, None))
        self.assertEqual(gone["fetched_at"], to_rfc3339(BASE))

    def test_單一窗口過了重置時刻_該窗口null(self):
        e = self.entry(wins=windows(session=(10.0, at(minutes=5)), weekly=(40.0, at(days=3))))
        u = build_usage("s", e, at(minutes=10), 3600)
        self.assertEqual(u["status"], "ok")
        self.assertIsNone(u["session"])
        self.assertEqual(u["weekly"]["remaining_percent"], 40.0)

    def test_兩個窗口都過了_stale_expired(self):
        e = self.entry(wins=windows(session=(10.0, at(minutes=5)), weekly=(40.0, at(minutes=6))))
        u = build_usage("s", e, at(minutes=10), 3600)
        self.assertEqual((u["status"], u["reason"], u["session"], u["weekly"]),
                         ("unknown", "stale_expired", None, None))

    def test_只有一個窗口讀到_另一個是null不是0(self):
        e = self.entry(wins=windows(session=None))
        u = build_usage("s", e, at(seconds=1), 120)
        self.assertIsNone(u["session"])
        self.assertEqual(u["status"], "ok")


class AvailableAgainAtTest(unittest.TestCase):
    def u(self, session=None, weekly=None):
        def w(spec):
            return None if spec is None else {"remaining_percent": spec[0], "resets_at": to_rfc3339(spec[1])}
        return {"session": w(session), "weekly": w(weekly)}

    def test_只有冷卻(self):
        self.assertEqual(available_again_at(self.u((50, at(hours=1))), at(hours=12), BASE, True),
                         to_rfc3339(at(hours=12)))

    def test_只有額度用完(self):
        got = available_again_at(self.u((50, at(hours=1)), (0, at(days=1))), None, BASE, True)
        self.assertEqual(got, to_rfc3339(at(days=1)))

    def test_取最晚者(self):
        got = available_again_at(self.u((0, at(hours=1, minutes=30)), (0, at(days=2))),
                                 at(hours=1), BASE, True)
        self.assertEqual(got, to_rfc3339(at(days=2)))

    def test_冷卻比額度重置晚(self):
        got = available_again_at(self.u((0, at(hours=1))), at(hours=3), BASE, True)
        self.assertEqual(got, to_rfc3339(at(hours=3)))

    def test_小數的0不算用完(self):
        self.assertIsNone(available_again_at(self.u((0.2, at(hours=1))), None, BASE, True))

    def test_負值或剛好0都算用完(self):
        self.assertEqual(available_again_at(self.u((-1, at(hours=1))), None, BASE, True),
                         to_rfc3339(at(hours=1)))

    def test_unknown不推測(self):
        self.assertIsNone(available_again_at(self.u(), None, BASE, True))

    def test_過去的冷卻不算(self):
        self.assertIsNone(available_again_at(self.u(), BASE - _dt.timedelta(minutes=1), BASE, True))
        self.assertIsNone(available_again_at(self.u(), BASE, BASE, True))

    def test_key沒部署一律null(self):
        self.assertIsNone(available_again_at(self.u((0, at(hours=1))), at(hours=2), BASE, False))


class TriggerTest(unittest.TestCase):
    def test_沒有fetcher是not_applicable(self):
        service = UsageService(FakeClock(BASE))
        self.assertEqual(service.usage_for("openrouter"),
                         {"status": "not_applicable", "source": None, "fetched_at": None,
                          "stale": None, "reason": None, "session": None, "weekly": None, "pipes": []})

    def test_卡住時不等_回not_fetched_yet_並只觸發一次(self):
        service, fetcher, _, _ = make()
        fetcher.gate = threading.Event()
        try:
            first = service.usage_for("sub")
            self.assertEqual((first["status"], first["reason"]), ("unknown", "not_fetched_yet"))
            self.assertTrue(fetcher.entered.wait(5))
            for _ in range(10):
                service.usage_for("sub")
            self.assertEqual(fetcher.calls, 1)
        finally:
            fetcher.gate.set()
            service.wait_idle()

    def test_下一次查詢才看到新值_且不再觸發(self):
        service, fetcher, _, _ = make()
        service.usage_for("sub")
        self.assertTrue(service.wait_idle())
        got = service.usage_for("sub")
        self.assertEqual(got["status"], "ok")
        self.assertEqual(fetcher.calls, 1)

    def test_過期後連續多次查詢只觸發一次_多執行緒(self):
        service, fetcher, _, _ = make()
        fetcher.gate = threading.Event()
        threads = [threading.Thread(target=service.usage_for, args=("sub",)) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        fetcher.gate.set()
        service.wait_idle()
        self.assertEqual(fetcher.calls, 1)

    def test_沒人查就不取(self):
        service, fetcher, clock, _ = make()
        clock.advance(hours=5)
        self.assertEqual(fetcher.calls, 0)

    def test_快取還新鮮不觸發_過期再觸發(self):
        service, fetcher, clock, _ = make()
        service.usage_for("sub"); service.wait_idle()
        clock.advance(seconds=100)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 1)
        clock.advance(seconds=30)           # 130 > 120
        stale = service.usage_for("sub")
        self.assertEqual((stale["status"], stale["stale"]), ("ok", True))   # 回舊值
        service.wait_idle()
        self.assertEqual(fetcher.calls, 2)

    def test_不同subscription互不影響(self):
        clock = FakeClock(BASE)
        a, b = FakeFetcher(), FakeFetcher()
        a.gate = threading.Event()
        service = UsageService(clock, fetchers={"a": a, "b": b}, key_of=lambda s: "k",
                               deployed=lambda s: True)
        try:
            service.usage_for("a")
            service.usage_for("b")
            self.assertTrue(a.entered.wait(5))
            for _ in range(100):
                if b.calls:
                    break
                threading.Event().wait(0.02)
            self.assertEqual((a.calls, b.calls), (1, 1))
        finally:
            a.gate.set()
            service.wait_idle()

    def test_key沒部署_no_key且不觸發_即使有舊值(self):
        service, fetcher, _, state = make()
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(service.usage_for("sub")["status"], "ok")
        state.deployed = False
        got = service.usage_for("sub")
        self.assertEqual((got["status"], got["reason"], got["session"], got["weekly"]),
                         ("unknown", "no_key", None, None))
        self.assertEqual(fetcher.calls, 1)

    def test_disabled優先於no_key(self):
        service, fetcher, _, _ = make(settings={"sub": UsageSetting(enabled=False)}, deployed=False)
        self.assertEqual(service.usage_for("sub")["reason"], "disabled")
        self.assertEqual(fetcher.calls, 0)

    def test_預設關閉的fetcher_disabled且不觸發(self):
        service, fetcher, _, _ = make(FakeFetcher(enabled=False))
        got = service.usage_for("sub")
        self.assertEqual((got["status"], got["reason"], got["source"]), ("unknown", "disabled", "fake-source"))
        self.assertEqual(fetcher.calls, 0)

    def test_政策開啟預設關閉的fetcher(self):
        service, fetcher, _, _ = make(FakeFetcher(enabled=False), settings={"sub": UsageSetting(enabled=True)})
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 1)

    def test_取用量拋例外_轉internal_並進退避(self):
        service, fetcher, _, _ = make()
        fetcher.outcome = RuntimeError("canary-secret-in-exception")
        with self.assertLogs("lgwd.usage", level="WARNING") as logs:
            service.usage_for("sub"); service.wait_idle()
        got = service.usage_for("sub")
        self.assertEqual((got["status"], got["reason"]), ("unknown", "internal"))
        self.assertNotIn("canary-secret", "\n".join(logs.output))   # 只記型別名

    def test_組裝本身出錯_該筆internal(self):
        service, _, _, _ = make()
        service._deployed = lambda s: 1 / 0
        got = service.usage_for("sub")
        self.assertEqual((got["status"], got["reason"]), ("unknown", "internal"))
        self.assertEqual(set(got), SHAPE)


class BackoffTest(unittest.TestCase):
    def test_失敗後退避期間內不觸發_之後再觸發(self):
        service, fetcher, clock, _ = make()
        fetcher.outcome = failure("upstream_error")
        service.usage_for("sub"); service.wait_idle()
        clock.advance(seconds=30)
        got = service.usage_for("sub"); service.wait_idle()
        self.assertEqual((fetcher.calls, got["reason"]), (1, "upstream_error"))
        clock.advance(seconds=91)       # 超過 120
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 2)

    def test_連續失敗加倍_上限1800(self):
        service, fetcher, clock, _ = make()
        fetcher.outcome = failure("upstream_error")
        for wait in (120, 240, 480, 960, 1800, 1800, 1800):
            service.usage_for("sub"); service.wait_idle()
            calls = fetcher.calls
            clock.advance(seconds=wait - 1)
            service.usage_for("sub"); service.wait_idle()
            self.assertEqual(fetcher.calls, calls, f"wait={wait} 內不該觸發")
            clock.advance(seconds=1)

    def test_上限隨TTL調高(self):
        service, fetcher, clock, _ = make(settings={"sub": UsageSetting(ttl_seconds=3 * 3600)})
        fetcher.min_ttl = 60
        fetcher.outcome = failure("upstream_error")
        service.usage_for("sub"); service.wait_idle()       # 失敗 1：等 3 小時
        clock.advance(hours=3)
        service.usage_for("sub"); service.wait_idle()       # 失敗 2：加倍 6h，上限 3h
        calls = fetcher.calls
        clock.advance(hours=3, seconds=-1)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, calls)
        clock.advance(seconds=1)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, calls + 1)

    def test_429照RetryAfter_取較大者(self):
        service, fetcher, clock, _ = make()
        fetcher.outcome = failure("rate_limited", retry_after=600)
        service.usage_for("sub"); service.wait_idle()
        clock.advance(seconds=599)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 1)
        clock.advance(seconds=1)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 2)

    def test_RetryAfter小於退避_取退避(self):
        service, fetcher, clock, _ = make()
        fetcher.outcome = failure("rate_limited", retry_after=10)
        service.usage_for("sub"); service.wait_idle()
        clock.advance(seconds=119)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 1)
        clock.advance(seconds=1)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, 2)

    def test_成功重置退避(self):
        service, fetcher, clock, _ = make()
        fetcher.outcome = failure("upstream_error")
        service.usage_for("sub"); service.wait_idle()
        clock.advance(seconds=120)
        service.usage_for("sub"); service.wait_idle()       # 第 2 次失敗（下次等 240）
        clock.advance(seconds=240)
        fetcher.outcome = FetchResult(windows=windows())
        service.usage_for("sub"); service.wait_idle()       # 成功
        clock.advance(seconds=121)                          # 過 TTL
        fetcher.outcome = failure("upstream_error")
        service.usage_for("sub"); service.wait_idle()       # 重新失敗 1：等 120（不是 480）
        calls = fetcher.calls
        clock.advance(seconds=120)
        service.usage_for("sub"); service.wait_idle()
        self.assertEqual(fetcher.calls, calls + 1)


class TtlTest(unittest.TestCase):
    def test_夾到下限並記log(self):
        with self.assertLogs("lgwd.usage", level="WARNING") as logs:
            service, _, _, _ = make(FakeFetcher(ttl=1800, min_ttl=1800), settings={"sub": UsageSetting(ttl_seconds=300)})
        self.assertEqual(service._ttl["sub"], 1800)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("canary", logs.output[0])

    def test_調高有效_且低於下限的ollama夾到60(self):
        service, _, _, _ = make(FakeFetcher(ttl=1800, min_ttl=1800), settings={"sub": UsageSetting(ttl_seconds=7200)})
        self.assertEqual(service._ttl["sub"], 7200)
        with self.assertLogs("lgwd.usage", level="WARNING"):
            service, _, _, _ = make(settings={"sub": UsageSetting(ttl_seconds=1)})
        self.assertEqual(service._ttl["sub"], 60)
        service, _, _, _ = make(settings={"sub": UsageSetting(ttl_seconds=300)})
        self.assertEqual(service._ttl["sub"], 300)

    def test_預設值(self):
        service, _, _, _ = make(FakeFetcher(ttl=120, min_ttl=60))
        self.assertEqual(service._ttl["sub"], 120)


class StopTest(unittest.TestCase):
    def test_stop後再查不觸發(self):
        service, fetcher, _, _ = make()
        service.stop()
        service.usage_for("sub")
        self.assertEqual(fetcher.calls, 0)

    def test_stop可重複呼叫(self):
        service, _, _, _ = make()
        service.stop(); service.stop()


class PipesContractTest(unittest.TestCase):
    def entry(self, fetched=BASE, wins=None, reason=None):
        e = usage_mod._Entry()
        e.fetched_at, e.windows, e.last_reason = fetched, wins or windows(), reason
        return e

    def test_C1_形狀恆為八鍵且pipes為陣列(self):
        # 任何 usage（含 not_applicable、unknown）鍵集恰為八鍵，pipes 為陣列（無管子＝[]）
        na = usage_mod._not_applicable()
        self.assertEqual(set(na), SHAPE)
        self.assertEqual(na["pipes"], [])

        unk = usage_mod._unknown("s", "upstream_error")
        self.assertEqual(set(unk), SHAPE)
        self.assertEqual(unk["pipes"], [])

        internal = usage_mod.internal_usage("s")
        self.assertEqual(set(internal), SHAPE)
        self.assertEqual(internal["pipes"], [])

        ok = build_usage("fake-source", self.entry(), at(seconds=10), 120)
        self.assertEqual(set(ok), SHAPE)
        self.assertIsInstance(ok["pipes"], list)

    def test_C2_Claude兩窗口與pipes逐值相等(self):
        # Claude 兩窗口讀到時，pipes 有 five_hour、seven_day，逐值相等，models 為 ["*"]
        e = self.entry(wins={"session": (76.5, at(hours=3)), "weekly": (39.0, at(days=5))})
        u = build_usage("claude-statusline", e, at(seconds=10), 120)
        self.assertEqual(u["status"], "ok")
        self.assertEqual(u["session"]["remaining_percent"], 76.5)
        self.assertEqual(u["session"]["resets_at"], to_rfc3339(at(hours=3)))
        self.assertEqual(u["weekly"]["remaining_percent"], 39.0)
        self.assertEqual(u["weekly"]["resets_at"], to_rfc3339(at(days=5)))

        self.assertEqual(len(u["pipes"]), 2)
        p1, p2 = u["pipes"]
        self.assertEqual(p1, {
            "id": "five_hour",
            "models": ["*"],
            "models_except": [],
            "remaining_percent": 76.5,
            "resets_at": to_rfc3339(at(hours=3)),
        })
        self.assertEqual(p2, {
            "id": "seven_day",
            "models": ["*"],
            "models_except": [],
            "remaining_percent": 39.0,
            "resets_at": to_rfc3339(at(days=5)),
        })
        self.assertEqual(p1["remaining_percent"], u["session"]["remaining_percent"])
        self.assertEqual(p1["resets_at"], u["session"]["resets_at"])
        self.assertEqual(p2["remaining_percent"], u["weekly"]["remaining_percent"])
        self.assertEqual(p2["resets_at"], u["weekly"]["resets_at"])

    def test_C3_Ollama讀到窗口與pipes逐值相等_新方案pipes為空(self):
        # Ollama 讀到 session/weekly 時 pipes 有 session, weekly 且逐值相等；新方案 unknown/no_windows, pipes: []
        e = self.entry(wins={"session": (75.0, at(hours=2)), "weekly": (40.0, at(days=4))})
        u = build_usage("ollama-api-balance", e, at(seconds=10), 120)
        self.assertEqual(u["status"], "ok")
        self.assertEqual(len(u["pipes"]), 2)
        p_sess, p_week = u["pipes"]
        self.assertEqual(p_sess, {
            "id": "session",
            "models": ["*"],
            "models_except": [],
            "remaining_percent": 75.0,
            "resets_at": to_rfc3339(at(hours=2)),
        })
        self.assertEqual(p_week, {
            "id": "weekly",
            "models": ["*"],
            "models_except": [],
            "remaining_percent": 40.0,
            "resets_at": to_rfc3339(at(days=4)),
        })
        self.assertEqual(p_sess["remaining_percent"], u["session"]["remaining_percent"])
        self.assertEqual(p_week["remaining_percent"], u["weekly"]["remaining_percent"])

        # 新方案：fetch 失敗 no_windows
        e_empty = usage_mod._Entry()
        e_empty.last_reason = "no_windows"
        u_empty = build_usage("ollama-api-balance", e_empty, BASE, 120)
        self.assertEqual(u_empty["status"], "unknown")
        self.assertEqual(u_empty["reason"], "no_windows")
        self.assertEqual(u_empty["pipes"], [])

    def test_C4_OpenRouter_free_daily與session逐值相等(self):
        # OpenRouter free_model_daily_requests 讀到 -> pipes 有 free_daily (models ["*:free","openrouter/free"])，session 與其逐值相等
        midnight = at(hours=5)
        e = self.entry(wins={"session": (80.0, midnight), "weekly": None})
        u = build_usage("openrouter-api-key", e, at(seconds=10), 120)
        self.assertEqual(u["status"], "ok")
        self.assertEqual(len(u["pipes"]), 1)
        p = u["pipes"][0]
        self.assertEqual(p, {
            "id": "free_daily",
            "models": ["*:free", "openrouter/free"],
            "models_except": [],
            "remaining_percent": 80.0,
            "resets_at": to_rfc3339(midnight),
        })
        self.assertEqual(u["session"]["remaining_percent"], p["remaining_percent"])
        self.assertEqual(u["session"]["resets_at"], p["resets_at"])
        self.assertIsNone(u["weekly"])

    def test_C5_OpenRouter_credits導出(self):
        # OpenRouter 有 credits -> 多一根 credits（percent, resets_at null, models_except free 樣式），session 仍只由 free_daily 導出
        midnight = at(hours=5)
        e = self.entry(wins={
            "session": (80.0, midnight),
            "weekly": None,
            "credits": (40.0, None),
        })
        u = build_usage("openrouter-api-key", e, at(seconds=10), 120)
        self.assertEqual(u["status"], "ok")
        self.assertEqual(u["session"]["remaining_percent"], 80.0)
        self.assertEqual(len(u["pipes"]), 2)
        p_free, p_cred = u["pipes"]
        self.assertEqual(p_free["id"], "free_daily")
        self.assertEqual(p_cred, {
            "id": "credits",
            "models": ["*"],
            "models_except": ["*:free", "openrouter/free"],
            "remaining_percent": 40.0,
            "resets_at": None,
        })

    def test_C6_只有credits讀到時status為ok且session為null(self):
        # 只有 credits 讀到、free_model_daily_requests 缺 -> status: ok，session 為 null，pipes 只有 credits
        e = self.entry(wins={
            "session": None,
            "weekly": None,
            "credits": (45.0, None),
        })
        u = build_usage("openrouter-api-key", e, at(seconds=10), 120)
        self.assertEqual(u["status"], "ok")
        self.assertIsNone(u["session"])
        self.assertIsNone(u["weekly"])
        self.assertEqual(len(u["pipes"]), 1)
        self.assertEqual(u["pipes"][0]["id"], "credits")
        self.assertEqual(u["pipes"][0]["remaining_percent"], 45.0)
        self.assertIsNone(u["pipes"][0]["resets_at"])

    def test_C7_管子resets_at過期與null過期處理(self):
        # 管子 resets_at 已過 -> 不在 pipes；全部都沒有 -> unknown/stale_expired。resets_at 為 null 超過 6 小時一併消失
        # 1. 兩根管子，其中一根已過
        wins = {
            "session": (70.0, at(hours=-1)), # 已過期
            "weekly": (50.0, at(hours=2)),  # 未過期
        }
        e = self.entry(wins=wins)
        u = build_usage("claude-statusline", e, BASE, 120)
        self.assertEqual(u["status"], "ok")
        self.assertIsNone(u["session"])
        self.assertIsNotNone(u["weekly"])
        self.assertEqual(len(u["pipes"]), 1)
        self.assertEqual(u["pipes"][0]["id"], "seven_day")

        # 2. 全部管子已過期 -> unknown/stale_expired
        wins_expired = {
            "session": (70.0, at(hours=-2)),
            "weekly": (50.0, at(hours=-1)),
        }
        e_exp = self.entry(wins=wins_expired)
        u_exp = build_usage("claude-statusline", e_exp, BASE, 120)
        self.assertEqual(u_exp["status"], "unknown")
        self.assertEqual(u_exp["reason"], "stale_expired")
        self.assertEqual(u_exp["pipes"], [])

        # 3. resets_at 為 null 的 credits 在 6 小時內仍在，超過 6 小時消失
        wins_cred = {
            "session": None,
            "weekly": None,
            "credits": (50.0, None),
        }
        e_cred = self.entry(fetched=BASE, wins=wins_cred)
        u_within = build_usage("openrouter-api-key", e_cred, at(hours=5, minutes=59), 120)
        self.assertEqual(u_within["status"], "ok")
        self.assertEqual(len(u_within["pipes"]), 1)

        u_after = build_usage("openrouter-api-key", e_cred, at(hours=6, seconds=1), 120)
        self.assertEqual(u_after["status"], "unknown")
        self.assertEqual(u_after["reason"], "stale_expired")
        self.assertEqual(u_after["pipes"], [])

    def test_C8_available_again_at算法不變_只由session_weekly計算(self):
        # available_again_at 算法不變：只有 credits 為 0 時仍為 null；session/weekly 為 0 時與現行相同
        # 只有 credits 為 0（由 build_usage 產生，驗證 credits 不在 WINDOWS 裡、不影響 session/weekly 與 available_again_at）
        e_cred_zero = self.entry(wins={
            "session": None,
            "weekly": None,
            "credits": (0.0, None),
        })
        u_cred_zero = build_usage("openrouter-api-key", e_cred_zero, BASE, 120)
        self.assertEqual(set(u_cred_zero), SHAPE)
        self.assertNotIn("credits", u_cred_zero)  # credits 絕不作為 usage 的一級鍵
        self.assertIsNone(available_again_at(u_cred_zero, None, BASE, True))

        # session 為 0
        u_sess_zero = {
            "status": "ok",
            "source": "openrouter-api-key",
            "session": {"remaining_percent": 0.0, "resets_at": to_rfc3339(at(hours=2))},
            "weekly": None,
            "pipes": [{
                "id": "free_daily", "models": ["*"], "models_except": [],
                "remaining_percent": 0.0, "resets_at": to_rfc3339(at(hours=2)),
            }],
        }
        self.assertEqual(available_again_at(u_sess_zero, None, BASE, True), to_rfc3339(at(hours=2)))

        # weekly 為 0
        u_week_zero = {
            "status": "ok",
            "source": "claude-statusline",
            "session": None,
            "weekly": {"remaining_percent": 0.0, "resets_at": to_rfc3339(at(days=1))},
            "pipes": [{
                "id": "seven_day", "models": ["*"], "models_except": [],
                "remaining_percent": 0.0, "resets_at": to_rfc3339(at(days=1)),
            }],
        }
        self.assertEqual(available_again_at(u_week_zero, None, BASE, True), to_rfc3339(at(days=1)))

        # pipes 有一根 resets_at 在未來且 percent 0，但 session/weekly 為 null -> 仍為 None
        u_pipe_future_zero = {
            "status": "ok",
            "source": "fake-source",
            "session": None,
            "weekly": None,
            "pipes": [{
                "id": "custom_future", "models": ["*"], "models_except": [],
                "remaining_percent": 0.0, "resets_at": to_rfc3339(at(hours=2)),
            }],
        }
        self.assertIsNone(available_again_at(u_pipe_future_zero, None, BASE, True))

    def test_C9_not_applicable的pipes是空陣列且不觸發取用量(self):
        service = UsageService(FakeClock(BASE))
        for p in ("opencode-zen", "opencode-go", "antigravity"):
            with self.subTest(provider=p):
                u = service.usage_for(p)
                self.assertEqual(u["status"], "not_applicable")
                self.assertIsNone(u["source"])
                self.assertEqual(u["pipes"], [])

    def test_C10_canary不出現在usage與pipes中(self):
        # 回應（含 pipes）找不到 key/token、絕對值 canary (limit 12345.678, limit_remaining 4938.2712)、上游原文標籤
        canary_limit = 12345.678
        canary_rem = 4938.2712
        pct = canary_rem / canary_limit * 100
        e = self.entry(wins={
            "session": (80.0, at(hours=3)),
            "weekly": None,
            "credits": (pct, None),
        })
        u = build_usage("openrouter-api-key", e, BASE, 120)
        dump = json.dumps(u)
        self.assertNotIn("12345.678", dump)
        self.assertNotIn("4938.2712", dump)
        self.assertNotIn("free_model_daily_requests", dump)
        self.assertNotIn("limit_remaining", dump)


if __name__ == "__main__":
    unittest.main()
