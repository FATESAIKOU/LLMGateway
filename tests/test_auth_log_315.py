"""C-607 聚合的 count 語意與收尾（#315；契約 C-1012～C-1025，見 REPORT-315-arch.md §6）。

每個 C 項至少一條測試，編號寫在測試名稱裡。`AuthLog` 用注入的假時鐘（單調）；接線
（C-1016～C-1024）用真的 `open_runtime`＋真的 watcher 執行緒＋真的 `Gateway`，與
`tests/test_auth_runtime.py` 的 `RuntimeCase` 同一套暫存目錄與真的暫存金鑰；不碰
`/var/lib`、`/etc`、不讀憑證、不連真機。
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from lgwd import auth
from lgwd.clock import FakeClock
from lgwd.main import Config, Gateway
from lgwd.providers import UsageDeps
from tests.test_auth_runtime import RuntimeCase
from tests.test_main import FAKE_KEYS, key_text, write_policy
from tests.usage_fakes import refuse_http

LINE_RE = re.compile(
    r"^auth mode=(\S+) outcome=(\S+) reason=(\S+) route=(\S+) principal=(\S+) count=(\d+)$")
ROUTE = "/v1/roles/{role}"


class Clock:
    """單調的假時鐘：`t` 只增不減（契約：clock 必須單調）。"""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_log(lines: list, clock: Clock, window: float = 60.0) -> auth.AuthLog:
    return auth.AuthLog(sink=lines.append, clock=clock, window=window)


def rec(log: auth.AuthLog, *, outcome=auth.OUTCOME_DENY, reason=auth.REASON_MISSING,
        route=ROUTE, principal=None, mode="warn") -> None:
    log.record(mode=mode, outcome=outcome, reason=reason, route=route, principal=principal)


def key_of(outcome, reason, route, principal, mode="warn") -> tuple:
    """`record` 之後行裡的鍵（pass 的 reason 是空字串；空的 principal 寫成 `-`，這裡還原）。"""
    return (mode, outcome, "" if outcome == auth.OUTCOME_PASS else reason, route, principal or "")


def parse(line: str) -> tuple[tuple, int]:
    match = LINE_RE.match(line)
    assert match, f"行格式不對：{line!r}"
    mode, outcome, reason, route, principal, count = match.groups()
    return (mode, outcome, "" if reason == "-" else reason, route,
            "" if principal == "-" else principal), int(count)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class C607CountSemanticsTests(unittest.TestCase):
    """C-1012～C-1015、C-1019、C-1022、C-1023：純 AuthLog（假時鐘、列表 sink）。"""

    def setUp(self):
        self.lines: list[str] = []
        self.clock = Clock(0.0)
        self.log = make_log(self.lines, self.clock)

    def test_C1012_random_sequences_sum_to_record_counts(self):
        # 固定 seed 的隨機序列：同鍵與多鍵交錯、時間步含 0、窗內、恰好 window、window−ε、
        # 超過 window；穿插 flush_expired。最後 flush_all 後每鍵行的 count 加總＝record 次數。
        steps = (0.0, 0.001, 0.5, 59.999, 60.0, 60.001, 61.0, 120.0)
        for seed in range(40):
            rng = random.Random(seed)
            lines: list[str] = []
            clock = Clock(0.0)
            log = make_log(lines, clock)
            expected: dict[tuple, int] = {}
            for _ in range(200):
                clock.t += rng.choice(steps)
                if rng.random() < 0.1:
                    log.flush_expired()
                    continue
                outcome = rng.choice([auth.OUTCOME_PASS, auth.OUTCOME_DENY])
                reason = rng.choice([auth.REASON_MISSING, auth.REASON_REPLAY])
                principal = rng.choice([None, "alice", "bob"])
                rec(log, outcome=outcome, reason=reason, principal=principal)
                key = key_of(outcome, reason, ROUTE, principal)
                expected[key] = expected.get(key, 0) + 1
            log.flush_all()
            sums: dict[tuple, int] = {}
            for line in lines:
                key, count = parse(line)
                sums[key] = sums.get(key, 0) + count
            self.assertEqual(sums, expected, f"seed={seed}")
            self.assertEqual(log._windows, {}, f"seed={seed}")

    def test_C1012_boundary_exactly_window_and_window_minus_epsilon(self):
        # 恰好 window 秒（60.0）就算到期；window−ε（59.999）還沒到期。
        rec(self.log)                       # t=0：立刻寫 1
        rec(self.log)                       # t=0：pending
        self.clock.t = 59.999
        rec(self.log)                       # 窗內：pending
        self.assertEqual(len(self.lines), 1)
        self.clock.t = 60.0
        rec(self.log)                       # 恰好到期：先寫 pending=2，再寫 1
        self.assertEqual([parse(x)[1] for x in self.lines], [1, 2, 1])
        self.log.flush_all()
        self.assertEqual(sum(parse(x)[1] for x in self.lines), 4)

    def test_C1012_boundary_flush_expired_exactly_at_window(self):
        rec(self.log)
        rec(self.log)
        self.clock.t = 59.999
        self.assertEqual(self.log.flush_expired(), 0)   # 還沒到期：不寫、不刪
        self.assertEqual(len(self.log._windows), 1)
        self.clock.t = 60.0
        self.assertEqual(self.log.flush_expired(), 1)   # 恰好到期：寫 pending=1
        self.assertEqual([parse(x)[1] for x in self.lines], [1, 1])

    def test_C1013_first_event_is_visible_immediately(self):
        rec(self.log, principal="alice")
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].endswith("count=1"))
        # 另一個鍵的第一筆也立刻可見（不等視窗）。
        rec(self.log, route="/v1/other")
        self.assertEqual(len(self.lines), 2)
        self.assertIn("route=/v1/other", self.lines[1])
        self.assertTrue(self.lines[1].endswith("count=1"))

    def test_C1014_in_window_no_lines_then_expiry_writes_pending_then_one(self):
        for i in range(5):                  # N=5，全在窗內（t=0..4）
            self.clock.t = float(i)
            rec(self.log)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].endswith("count=1"))
        self.clock.t = 65.0
        rec(self.log)                       # 到期：先 count=4，再 count=1
        self.assertEqual(len(self.lines), 3)
        self.assertTrue(self.lines[1].endswith("count=4"))
        self.assertTrue(self.lines[2].endswith("count=1"))

    def test_C1014_single_event_expiry_writes_no_pending_line(self):
        # N=1：到期時沒有 pending，不寫 count=0 的行。
        rec(self.log)
        self.clock.t = 60.0
        rec(self.log)
        self.assertEqual(len(self.lines), 2)
        self.assertTrue(all(parse(x)[1] == 1 for x in self.lines))

    def test_C1015_flush_expired_writes_due_pending_and_drops_keys(self):
        key_a = dict(principal="alice")
        key_b = dict(principal="bob")
        key_c = dict(principal="carol")
        rec(self.log, **key_a)              # A：t=0，寫 1，pending 2
        rec(self.log, **key_a)
        rec(self.log, **key_a)
        rec(self.log, **key_b)              # B：t=0，寫 1，pending 0
        self.clock.t = 50.0
        rec(self.log, **key_c)              # C：t=50，寫 1，pending 1（到 t=60 還沒到期）
        rec(self.log, **key_c)
        self.assertEqual(len(self.lines), 3)
        self.clock.t = 60.0                 # A、B 到期；C 還沒（60−50 < 60）
        self.assertEqual(self.log.flush_expired(), 1)   # 只有 A 寫出；B 只刪不寫
        self.assertTrue(self.lines[-1].endswith("count=2"))
        self.assertIn("principal=alice", self.lines[-1])
        self.assertEqual(self.log.flush_expired(), 0)   # 再呼叫一次不寫任何行
        self.assertEqual(len(self.lines), 4)
        self.assertEqual(set(self.log._windows), {key_of(auth.OUTCOME_DENY, auth.REASON_MISSING,
                                                         ROUTE, "carol")})

    def test_C1019_line_format_and_malformed_reason_unchanged(self):
        # 格式逐 byte 不變；收尾行同格式；表外 reason 仍記 malformed。
        rec(self.log, principal=None)
        rec(self.log, principal=None)
        self.clock.t = 60.0
        self.log.flush_expired()
        self.assertEqual(
            self.lines[-1],
            "auth mode=warn outcome=deny reason=missing route=/v1/roles/{role} principal=- count=1")
        rec(self.log, outcome=auth.OUTCOME_PASS, reason="", principal="alice")
        self.assertEqual(
            self.lines[-1],
            "auth mode=warn outcome=pass reason=- route=/v1/roles/{role} principal=alice count=1")
        rec(self.log, reason="not_in_whitelist", route="/v1/x")
        self.assertIn("reason=malformed", self.lines[-1])
        self.assertNotIn("not_in_whitelist", self.lines[-1])
        for line in self.lines:
            self.assertRegex(line, LINE_RE)

    def test_C1022_memory_bounded_after_expiry(self):
        for i in range(100):                # 100 個不同身分，同一個視窗
            rec(self.log, principal=f"p{i}")
        self.assertEqual(len(self.log._windows), 100)
        self.clock.t = 60.0
        self.assertEqual(self.log.flush_expired(), 0)   # 每鍵 pending=0：只刪不寫
        self.assertEqual(len(self.log._windows), 0)
        key = key_of(auth.OUTCOME_DENY, auth.REASON_MISSING, ROUTE, "p1")
        rec(self.log, principal="p1")
        self.assertIn(key, self.log._windows)
        self.clock.t = 120.0
        rec(self.log, principal="p1")       # 同鍵到期（pending 0 不寫），開新視窗：只剩它一個鍵
        self.assertEqual(set(self.log._windows), {key})
        self.clock.t = 180.0
        self.log.flush_expired()
        self.assertEqual(len(self.log._windows), 0)

    def test_C1023_reopen_after_flush_all_writes_one_immediately(self):
        rec(self.log)
        rec(self.log)
        self.log.flush_all()                # 收尾 count=1；清空
        n = len(self.lines)
        rec(self.log)                       # 清空後同鍵：立刻寫 count=1
        self.assertEqual(len(self.lines), n + 1)
        self.assertTrue(self.lines[-1].endswith("count=1"))
        rec(self.log)                       # 再下一筆：進 pending，不寫
        self.assertEqual(len(self.lines), n + 1)

    def test_C1023_reopen_after_flush_expired_writes_one_immediately(self):
        rec(self.log)
        rec(self.log)
        self.clock.t = 60.0
        self.assertEqual(self.log.flush_expired(), 1)   # 寫 count=1 並刪鍵
        n = len(self.lines)
        rec(self.log)                       # 鍵已刪：立刻寫 count=1
        self.assertEqual(len(self.lines), n + 1)
        self.assertTrue(self.lines[-1].endswith("count=1"))
        rec(self.log)
        self.assertEqual(len(self.lines), n + 1)


class C607SinkFailureTests(unittest.TestCase):
    """C-1020：sink 例外每一行各自吞掉；回傳值＝嘗試寫出的行數。"""

    def setUp(self):
        self.lines: list[str] = []
        self.failing = True
        self.clock = Clock(0.0)
        self.log = auth.AuthLog(sink=self._sink, clock=self.clock)

    def _sink(self, line: str) -> None:
        if self.failing:
            raise OSError("sink down")
        self.lines.append(line)

    def test_C1020_flush_return_value_matches_lines_delta_when_sink_fails(self):
        rec(self.log)
        rec(self.log)
        self.clock.t = 60.0
        before = self.log.lines
        ret = self.log.flush_expired()
        self.assertEqual(ret, self.log.lines - before)
        self.assertEqual(ret, 1)

    def test_C1020_sink_always_failing_never_raises_and_counts_attempts(self):
        rec(self.log)                       # 第一筆（寫出時 sink 丟例外）
        rec(self.log)                       # pending
        self.clock.t = 60.0
        before = self.log.lines
        self.assertEqual(self.log.flush_expired(), 1)     # 嘗試寫出 1 行（sink 丟例外）
        self.assertEqual(self.log.lines - before, 1)
        self.assertEqual(self.lines, [])
        self.assertEqual(self.log.flush_all(), 0)         # 沒有 pending 就不寫
        rec(self.log)                       # 新視窗第一筆：嘗試寫 1，sink 丟例外
        self.assertEqual(self.log.flush_all(), 0)         # 收尾 pending=0：不寫；清空
        self.assertEqual(self.lines, [])

    def test_C1020_sink_recovers_lost_lines_are_not_backfilled(self):
        rec(self.log)                       # 第一筆 count=1：丟了，不補寫
        rec(self.log)                       # pending 1
        self.failing = False
        self.clock.t = 60.0
        self.assertEqual(self.log.flush_expired(), 1)     # 寫 pending=1，sink 正常
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(sum(parse(x)[1] for x in self.lines), 1)   # 2 筆事件只剩 1（少掉的是丟的那行）

class AuthRuntimeWiringTests(RuntimeCase):
    """C-1016～C-1018、C-1021、C-1024：真的 runtime、watcher 與 Gateway。"""

    def make_gateway(self, runtime):
        keys = Path(self.dir) / "keys.env"
        keys.write_text(key_text(FAKE_KEYS), encoding="utf-8")
        write_policy(Path(self.dir))
        config = Config(
            db=os.path.join(self.dir, "llm.db"),
            policy_path=os.path.join(self.dir, "policy.json"),
            keys_path=str(keys),
            api_host="127.0.0.1",
            api_port=0,
        )
        return Gateway(config, clock=FakeClock(),
                       usage_deps=UsageDeps(http_get=refuse_http), auth_runtime=runtime)

    def test_C1016_background_tick_writes_tail_line_after_expiry(self):
        # 真 watcher（poll_interval 0.05）＋Gateway；假時鐘覆寫 AuthLog 的 _clock／window。
        runtime = self.open("warn", poll_interval=0.05)
        runtime.start()
        gateway = self.make_gateway(runtime)
        log = gateway.api_server.auth_log
        self.assertIs(runtime.auth_log, log)
        clock = Clock(0.0)
        lines: list[str] = []
        log._clock = clock
        log._sink = lines.append
        log.window = 1.0
        gateway.start()
        self.addCleanup(gateway.stop)
        rec(log)                            # 第一筆：立刻寫 count=1
        rec(log)                            # pending
        self.assertEqual(len(lines), 1)
        clock.t = 2.0                       # 推過視窗；不送任何新請求
        self.assertTrue(wait_until(lambda: len(lines) == 2, timeout=5.0),
                        "背景 tick 沒有在 5 秒內寫出收尾行")
        self.assertTrue(lines[1].endswith("count=1"))

    def test_C1017_shutdown_flushes_pending_after_watcher_stop_in_call_order(self):
        runtime = self.open("warn", poll_interval=0.05)
        runtime.start()
        gateway = self.make_gateway(runtime)
        log = gateway.api_server.auth_log
        order: list[str] = []
        lines: list[str] = []
        log._sink = lines.append
        watcher = runtime.watcher
        real_stop = watcher.stop
        real_flush = log.flush_all
        watcher.stop = lambda *a, **k: (order.append("watcher.stop"), real_stop(*a, **k))[1]
        log.flush_all = lambda: (order.append("flush_all"), real_flush())[1]
        gateway.start()
        rec(log)
        rec(log)                            # 視窗內 pending（不到期）
        self.assertEqual(len(lines), 1)
        gateway.stop()                      # 停 API、停 watcher、flush、其餘收尾
        self.assertEqual(order, ["watcher.stop", "flush_all"])
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("count=1"))

    def test_C1017_stop_twice_does_not_write_twice(self):
        runtime = self.open("warn")
        lines: list[str] = []
        log = auth.AuthLog(sink=lines.append, clock=Clock(0.0))
        runtime.auth_log = log
        rec(log)
        rec(log)
        runtime.stop()
        self.assertEqual(len(lines), 2)
        runtime.stop()
        self.assertEqual(len(lines), 2)

    def test_C1017_auth_log_none_stop_is_fine(self):
        runtime = self.open("warn")
        self.assertIsNone(runtime.auth_log)
        runtime.stop()                      # 不得丟例外

    def test_C1020_runtime_stop_with_failing_sink_does_not_raise(self):
        # 真 runtime 的 auth_log 接一個每次都丟例外的 sink：關機收尾不往上丟，重複 stop 也不丟。
        runtime = self.open("warn")
        attempts: list[str] = []

        def failing_sink(line: str) -> None:
            attempts.append(line)
            raise OSError("sink down")

        log = auth.AuthLog(sink=failing_sink, clock=Clock(0.0))
        runtime.auth_log = log
        rec(log)
        rec(log)
        runtime.stop()                      # 不得丟例外
        runtime.stop()
        self.assertEqual(len(attempts), 2)  # 第一筆 + 關機收尾，各嘗試一次（不重試）

    def test_C1018_off_writes_nothing_through_apply(self):
        # off 不建 runtime、apply 不記事件（既有行為不變）。
        lines: list[str] = []
        log = make_log(lines, Clock(0.0))
        verdict = SimpleNamespace(outcome=auth.OUTCOME_DENY, reason=auth.REASON_MISSING,
                                  principal=None, retry_after=None)
        action = auth.apply("off", verdict, route=ROUTE, log=log)
        self.assertIs(action, auth.CONTINUE)
        self.assertEqual(lines, [])
        self.assertEqual(log.flush_all(), 0)

    def test_C1021a_lock_holder_does_not_block_tick_or_shutdown_flush(self):
        # 另一執行緒持有 gate._lock 與 store 的 _db._lock（不是被測 AuthLog 的 _lock）：
        # `_log_tick` 與 `flush_all` 都要在 1 秒內完成。
        runtime = self.open("warn")
        log = auth.AuthLog(sink=lambda line: None, clock=Clock(0.0))
        runtime.auth_log = log
        held = threading.Event()
        release = threading.Event()

        def holder():
            with runtime.gate._lock, runtime.replay.db._lock:
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            rec(log)
            rec(log)
            log._clock = Clock(61.0)
            worker = threading.Thread(target=runtime._log_tick, daemon=True)
            worker.start()
            worker.join(1.0)
            self.assertFalse(worker.is_alive(), "tick 被鎖卡住")
            rec(log)                        # 新視窗：有 pending 的鍵才會被 flush_all 寫
            rec(log)
            worker = threading.Thread(target=log.flush_all, daemon=True)
            worker.start()
            worker.join(1.0)
            self.assertFalse(worker.is_alive(), "flush_all 被鎖卡住")
        finally:
            release.set()
            thread.join(5)

    def test_C1021b_flush_never_runs_while_gate_lock_is_held(self):
        # 無人持鎖：sink 內探測 gate._lock 必須拿得到（證明 flush 沒在持 gate 鎖時呼叫）。
        runtime = self.open("warn")
        probes: list[bool] = []
        lines: list[str] = []

        def sink(line: str) -> None:
            ok = runtime.gate._lock.acquire(blocking=False)
            if ok:
                runtime.gate._lock.release()
            probes.append(ok)
            lines.append(line)

        log = auth.AuthLog(sink=sink, clock=Clock(0.0))
        runtime.auth_log = log
        rec(log)
        rec(log)
        log._clock = Clock(61.0)
        runtime._log_tick()                 # 到期 pending 經 tick 寫出
        self.assertEqual(len(lines), 2)
        rec(log)                            # 新視窗
        rec(log)                            # pending
        runtime.stop()                      # 關機收尾
        self.assertEqual(len(lines), 4)
        self.assertTrue(probes and all(probes), probes)

    def test_C1024_watcher_none_still_flushes_on_stop(self):
        # warn 降級（沒有 watcher）：stop 仍寫出 pending 的收尾行。
        runtime = self.open("warn")
        lines: list[str] = []
        log = auth.AuthLog(sink=lines.append, clock=Clock(0.0))
        runtime.auth_log = log
        rec(log)
        rec(log)
        runtime.watcher = None
        runtime.stop()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("count=1"))


if __name__ == "__main__":
    unittest.main()
