"""lgwd/usage.py（#60 tasks 3.1、3.2）：usage 形狀、stale、available_again_at、觸發、single-flight、退避、TTL。

取用量一律用假 fetcher（可控制成功／失敗／卡住）與 FakeClock；不連網、不起子行程。
"""

import datetime as _dt
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

SHAPE = {"status", "source", "fetched_at", "stale", "reason", "session", "weekly"}


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

    def test_七個鍵恆在_ok(self):
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
                             "reason": "not_fetched_yet", "session": None, "weekly": None})

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
                          "stale": None, "reason": None, "session": None, "weekly": None})

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


if __name__ == "__main__":
    unittest.main()
