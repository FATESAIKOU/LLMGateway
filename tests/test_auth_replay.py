"""lgwd/auth_replay.py 的單元測試（D2 PR-3，契約 C-305～C-308）。

全部用暫存 SQLite（`tempfile`）、FakeClock 與假 nonce；不碰 /var/lib/llmgateway、
不讀憑證、不連真機。邊界向量、並行（32 執行緒）、清除與重放交錯、故障注入都用
真的 `BEGIN IMMEDIATE` 與真的 SQLite（不靠假物件）。
"""

from __future__ import annotations

import ast
import contextlib
import datetime as _dt
import os
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

from lgwd.auth_db import DEFAULT_BUSY_TIMEOUT, AuthDb, create_stores
from lgwd.auth_paths import auth_paths, now_seconds
from lgwd.auth_replay import (
    ACCEPTED,
    DEFAULT_PER_KEY_MAX,
    DENIED,
    MAX_RETRY_AFTER,
    PURGE_BATCH,
    PURGE_SECONDS,
    RATE_LIMITED,
    REASON_EXPIRED,
    REASON_REPLAY,
    REASON_WATER,
    WINDOW_SECONDS,
    PurgeTimer,
    ReplayStore,
)
from lgwd.clock import FakeClock

UTC = _dt.timezone.utc

# 並行測試的期限：正式預設是 5 秒（`DEFAULT_BUSY_TIMEOUT`）。測試機是多 session 共用的
# 高負載機器（實測 load average 30–55，16 核），fsync 變慢時尾端等鎖會超過 5 秒而變成
# StoreBusy；功能那一支放長期限，把「並行下有沒有一個邏輯層面的不可寫」與「機器有多慢」
# 分開。時間那一支仍用正式預設 5 秒（見下面 TIME_* 的固定到達率量法）。
CONCURRENT_BUSY_TIMEOUT = 30.0
CONCURRENT_THREADS = 32
CONCURRENT_PER_THREAD = 500

# 時間支（#311）的量法與判準。新判準是「在設計操作點下鎖不會成為瓶頸」：以固定到達率
# 持續 15 秒，只量等鎖 p99 與 StoreBusy 並印出（不 gating；#311 fix1 裁定 (c)）。到達率 30/s
# 是 (d) 的操作點（設計持續率 50/s 的 60%）；(d) 在受控共載下假失敗超過一次，所以降為觀察。
# 100 筆/秒與背靠背 32×500 同為不 gating 的觀察（LGW_SLOW_TESTS=1）。
TIME_RATE_PER_SECOND = 30
TIME_DURATION_SECONDS = 15.0
LOCK_WAIT_P99_LIMIT = 1.0           # 判準：等鎖 p99 < 1 秒，且零失敗（StoreBusy）
LOAD_SKIP_FACTOR = 2                # 開始前 load1 > 2×核數：最外層的舊規則，仍 skip
IO_SOME_AVG10_LIMIT = 40.0          # 跑期間 /proc/pressure/io some avg10（%）超過就 skip
IO_FULL_AVG10_LIMIT = 30.0          # full avg10（%）超過就 skip
HOLD_P99_FALLBACK_LIMIT = 0.200     # 無 /proc/pressure 時的退路：持鎖 p99（秒）超過就 skip


def _oversubscribed() -> tuple[float, int, bool]:
    """機器是否被別的 session 共載到量不出等鎖 p99 的程度（load > 2 × 核數）。

    等鎖 p99 的判準是設計值，要在不被別的 heavy job 搶磁碟／CPU 的機器上量；共載時單次
    fsync 可從 ~2 ms 升到數十毫秒，尾端等鎖由環境決定而不是由設計決定。回
    (load1, 核數, 是否過載)；讀不到 load 就當沒過載（照常量）。
    """
    cores = os.cpu_count() or 1
    try:
        load = float(os.getloadavg()[0])
    except OSError:  # pragma: no cover - 非 POSIX
        return 0.0, cores, False
    return load, cores, load > LOAD_SKIP_FACTOR * cores


def _parse_io_pressure(text: str | None) -> tuple[float, float] | None:
    """解析 /proc/pressure/io 的文字，回 (some avg10, full avg10)；讀不懂回 None。

    格式：`some avg10=0.54 avg60=... avg300=... total=...` 與同形的 `full` 行。
    """
    try:
        avg10 = {}
        for line in text.splitlines():
            kind, *fields = line.split()
            avg10[kind] = float(dict(field.split("=", 1) for field in fields)["avg10"])
        return avg10["some"], avg10["full"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _read_io_pressure() -> tuple[float, float] | None:
    """目前的 IO 壓力 (some avg10, full avg10)；沒有 /proc/pressure 回 None。"""
    try:
        with open("/proc/pressure/io", encoding="ascii") as handle:
            return _parse_io_pressure(handle.read())
    except OSError:
        return None


def _read_cpu_some() -> str:
    """目前 CPU 壓力的 some avg10（只用來記錄進訊息）；讀不到回 n/a。"""
    try:
        with open("/proc/pressure/cpu", encoding="ascii") as handle:
            first = handle.readline().split()
        return first[1].split("=", 1)[1]
    except (OSError, IndexError):
        return "n/a"


def _time_branch_skip(load_before: float | None, cores: int | None,
                      io_before: tuple[float, float] | None,
                      io_after: tuple[float, float] | None,
                      hold_p99: float | None) -> tuple[bool, str]:
    """時間支該不該 skip（純函式，不碰機器）：回 (skip, 說明)。

    依序判斷，第一個成立的決定結果：
    1. 開始前 load1 > 2×核數：外層的舊規則，仍 skip（說明帶 load1 與核數）。
    2. 有 /proc/pressure/io：取開始前與跑完後較大的 some／full avg10；任一超過門檻就 skip。
       門檻 some > 40、full > 30 的依據（REPORT-310-impl §2、§7）：乾淨樣本的 some 最大
       約 38（中位 26）；失敗樣本 some 落在 30～63，失敗時 full 最高 31.2。
       這兩個數只表示「環境已經壞到量不出設計值」，不是判定失敗的依據。
    3. 沒有 /proc/pressure：退路是實測持鎖 p99（本次 accept 交易的 body＋commit）超過
       0.2 秒，代表磁碟慢到連單筆都量不準，skip。
    4. 兩者皆無：照常斷言。

    skip 的說明一律帶量到的數值，供報告引用。
    """
    if load_before is not None and cores and load_before > LOAD_SKIP_FACTOR * cores:
        return True, "開始前 load1={0:.2f} > {1}×{2} 核（外層舊規則）".format(
            load_before, LOAD_SKIP_FACTOR, cores)
    pressures = [item for item in (io_before, io_after) if item is not None]
    if pressures:
        some = max(item[0] for item in pressures)
        full = max(item[1] for item in pressures)
        detail = "io 壓力（開始前與跑完後較大者）some avg10={0:.2f} full avg10={1:.2f}".format(
            some, full)
        if some > IO_SOME_AVG10_LIMIT or full > IO_FULL_AVG10_LIMIT:
            return True, "{0}，超過門檻 some>{1:g} 或 full>{2:g}".format(
                detail, IO_SOME_AVG10_LIMIT, IO_FULL_AVG10_LIMIT)
        return False, "{0}，未超門檻".format(detail)
    if hold_p99 is not None:
        if hold_p99 > HOLD_P99_FALLBACK_LIMIT:
            return True, "無 /proc/pressure；實測持鎖 p99={0:.3f}s > {1:g}s（磁碟太慢）".format(
                hold_p99, HOLD_P99_FALLBACK_LIMIT)
        return False, "無 /proc/pressure；實測持鎖 p99={0:.3f}s，未超 {1:g}s".format(
            hold_p99, HOLD_P99_FALLBACK_LIMIT)
    return False, "無 /proc/pressure 且讀不到持鎖；照常斷言"


def _p99(values: list[float]) -> float:
    """與舊測試同一個取法：sorted 後取 int(n×0.99)−1 的位置。"""
    ordered = sorted(values)
    return ordered[int(len(ordered) * 0.99) - 1]


def _record_hold_times(db, holds: list) -> None:
    """在這個 AuthDb 物件上包一層 `txn`，把每筆「交易本體＋commit＋釋鎖」的秒數記進 holds。

    不含等鎖與 BEGIN（那是等鎖的一部分）；只改測試用的這個實例，不動 lgwd。
    """
    real_txn = db.txn

    @contextlib.contextmanager
    def timed_txn(deadline=None):
        with real_txn(deadline) as cur:
            started = time.monotonic()
            yield cur
        holds.append(time.monotonic() - started)

    db.txn = timed_txn


def _at(seconds: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(seconds, UTC)


class ReplayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, mode=0o700)
        self.paths = auth_paths(self.state_dir)
        create_stores(self.paths.trust_db, self.paths.replay_db)
        self.clock = FakeClock()
        self.now = now_seconds(self.clock)
        self._stores: list[ReplayStore] = []

    def tearDown(self) -> None:
        for store in self._stores:
            try:
                store.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def store(self, *, clock: FakeClock | None = None, per_key_max: int = DEFAULT_PER_KEY_MAX,
              busy_timeout: float = 5.0, collect_lock_waits: bool = False) -> ReplayStore:
        store = ReplayStore(
            self.paths.replay_db, clock or self.clock, per_key_max=per_key_max,
            busy_timeout=busy_timeout, collect_lock_waits=collect_lock_waits,
        )
        self._stores.append(store)
        return store


class WindowAndWaterTests(ReplayTestBase):
    """C-305 的時窗與水位邊界向量（design「邊界寫死」）。"""

    def test_c305_window_boundaries(self) -> None:
        store = self.store()
        now = self.now
        cases = [
            (self.now + 60, True, "now = ts − 60 接受"),
            (self.now + 61, False, "now = ts − 61 拒"),
            (self.now - 60, True, "now = ts + 60 接受"),
            (self.now - 61, False, "now = ts + 61 拒"),
        ]
        for index, (ts, expected, label) in enumerate(cases):
            result = store.accept("window", "n{0}".format(index), ts)
            self.assertEqual(result.ok, expected, msg=label)
            if not expected:
                self.assertEqual(result.reason, REASON_EXPIRED, msg=label)
        self.assertEqual(store.row_count("window"), 2)

    def test_c305_water_boundaries(self) -> None:
        """`ts = H` 拒、`ts = H + 1` 收（H 由清除取得）。"""
        store = self.store()
        self.assertTrue(store.accept("w", "first", self.now).ok)
        self.clock.advance(seconds=PURGE_SECONDS + 1)
        self.assertEqual(store.purge_once(), 1)
        self.assertEqual(store.water_of("w"), self.now)
        # 時鐘回到時窗內，才驗得到水位而不是時窗。
        self.clock.set(_at(self.now + 30))
        equal = store.accept("w", "equal", self.now)
        self.assertEqual((equal.ok, equal.reason), (False, REASON_WATER))
        above = store.accept("w", "above", self.now + 1)
        self.assertTrue(above.ok)
        self.assertEqual(above.outcome, ACCEPTED)

    def test_c305_same_second_different_nonce_both_accepted(self) -> None:
        store = self.store()
        self.assertTrue(store.accept("k", "n1", self.now).ok)
        self.assertTrue(store.accept("k", "n2", self.now).ok)
        self.assertEqual(store.row_count("k"), 2)

    def test_c305_nonce_uniqueness_is_per_key(self) -> None:
        """重放鍵是 `(key_id, nonce)`；同一 nonce 用在不同 key 不算重放。"""
        store = self.store()
        self.assertTrue(store.accept("key-a", "shared", self.now).ok)
        self.assertTrue(store.accept("key-b", "shared", self.now).ok)

    def test_c305_type_errors(self) -> None:
        store = self.store()
        for ts in ("1", True, 1.5, None):
            with self.assertRaises(TypeError):
                store.accept("k", "n", ts)
        for bad in (b"n", None, 5):
            with self.assertRaises(TypeError):
                store.accept("k", bad, self.now)
        for bad in (b"k", None, 5):
            with self.assertRaises(TypeError):
                store.accept(bad, "n", self.now)


class ReplayTests(ReplayTestBase):
    """C-305：重放判定（唯一約束為最終依據）與不寫入紀律。"""

    def test_c305_replay_is_rejected_and_keeps_single_row(self) -> None:
        store = self.store()
        self.assertTrue(store.accept("k", "n", self.now).ok)
        again = store.accept("k", "n", self.now)
        self.assertEqual((again.ok, again.reason), (False, REASON_REPLAY))
        self.assertEqual(store.row_count("k"), 1)

    def test_c305_replay_only_compares_key_and_nonce(self) -> None:
        """防重放只認 `(key_id, nonce)`，不比簽章 bytes（S＋L 延展擋在預解析）。"""
        store = self.store()
        self.assertTrue(store.accept("k", "n", self.now).ok)
        # 同 key 同 nonce、ts 不同（看起來像另一份簽章）仍是重放。
        other_ts = store.accept("k", "n", self.now + 1)
        self.assertEqual(other_ts.reason, REASON_REPLAY)
        self.assertEqual(store.row_count("k"), 1)

    def test_c305_replay_after_process_restart(self) -> None:
        """重啟後重放：關庫再開同一個檔，nonce 仍在。"""
        store = self.store()
        self.assertTrue(store.accept("k", "n", self.now).ok)
        store.close()
        self._stores.remove(store)
        reopened = self.store()
        result = reopened.accept("k", "n", self.now)
        self.assertEqual(result.reason, REASON_REPLAY)

    def test_c305_rejected_requests_write_nothing(self) -> None:
        store = self.store(per_key_max=1)
        self.assertTrue(store.accept("k", "quota", self.now).ok)
        rejected = [
            store.accept("k", "too-new", self.now + 61),
            store.accept("k", "too-old", self.now - 61),
            store.accept("k", "quota", self.now),
            store.accept("k", "over-quota", self.now),
        ]
        self.assertEqual(
            [(item.outcome, item.reason) for item in rejected],
            [(DENIED, REASON_EXPIRED), (DENIED, REASON_EXPIRED),
             (DENIED, REASON_REPLAY), (RATE_LIMITED, "")],
        )
        self.assertGreater(rejected[3].retry_after, 0)
        self.assertEqual([item.retry_after for item in rejected[:3]], [0, 0, 0])
        self.assertEqual(store.row_count(), 1)
        self.assertIsNone(store.water_of("k"))


class QuotaTests(ReplayTestBase):
    """C-305 配額段、C-306 Retry-After。"""

    def test_c305_quota_full_is_rate_limited_without_writing(self) -> None:
        store = self.store(per_key_max=2)
        self.assertTrue(store.accept("k", "n1", self.now).ok)
        self.assertTrue(store.accept("k", "n2", self.now).ok)
        result = store.accept("k", "n3", self.now)
        self.assertEqual((result.ok, result.outcome), (False, RATE_LIMITED))
        self.assertEqual(result.reason, "")
        self.assertGreater(result.retry_after, 0)
        self.assertEqual(store.row_count("k"), 2)

    def test_c305_replay_wins_over_quota(self) -> None:
        """配額已滿時，重放仍回 DENIED/REPLAY（重放判斷排在配額之前）。"""
        store = self.store(per_key_max=1)
        self.assertTrue(store.accept("k", "n1", self.now).ok)
        self.assertEqual(store.accept("k", "n2", self.now).outcome, RATE_LIMITED)
        replay = store.accept("k", "n1", self.now)
        self.assertEqual((replay.outcome, replay.reason), (DENIED, REASON_REPLAY))

    def test_c305_expired_rows_not_counted_against_quota(self) -> None:
        """清除落後時，過期未清的列不計入配額（不誤拒合法請求）。"""
        store = self.store(per_key_max=2)
        self.assertTrue(store.accept("k", "a", self.now).ok)
        self.assertTrue(store.accept("k", "b", self.now).ok)
        later = self.now + PURGE_SECONDS + 5
        self.clock.set(_at(later))
        # 兩列都還沒被清除，但 ts < now − 65，計數時不算。
        self.assertTrue(store.accept("k", "c", later).ok)
        self.assertTrue(store.accept("k", "d", later).ok)
        self.assertEqual(store.accept("k", "e", later).outcome, RATE_LIMITED)
        self.assertEqual(store.row_count("k"), 4)

    def test_c305_outcome_is_three_valued_and_fields_are_disjoint(self) -> None:
        """C-305／C-603：outcome 三值；reason 只在 denied、retry_after 只在 rate_limited。"""
        store = self.store(per_key_max=1)
        accepted = store.accept("k", "n1", self.now)
        replay = store.accept("k", "n1", self.now)
        limited = store.accept("k", "n2", self.now)
        expired = store.accept("k", "n3", self.now + WINDOW_SECONDS + 1)
        self.assertEqual(
            [(item.outcome, item.reason, item.retry_after)
             for item in (accepted, replay, limited, expired)],
            [(ACCEPTED, "", 0), (DENIED, REASON_REPLAY, 0),
             (RATE_LIMITED, "", 66), (DENIED, REASON_EXPIRED, 0)],
        )
        self.assertEqual({item.outcome for item in (accepted, replay, limited, expired)},
                         {ACCEPTED, DENIED, RATE_LIMITED})
        self.assertTrue(accepted.ok)
        self.assertFalse(replay.ok or limited.ok)

    def test_c306_retry_after_formula_and_bounds(self) -> None:
        """retry_after＝min(126, max(1, 計入配額的最小 ts + 66 − now))。"""
        store = self.store(per_key_max=1)
        self.assertTrue(store.accept("now", "n", self.now).ok)
        self.assertEqual(store.accept("now", "x", self.now).retry_after, 66)

        future = self.now + WINDOW_SECONDS  # ts 在未來 60 秒
        self.assertTrue(store.accept("future", "n", future).ok)
        self.assertEqual(store.accept("future", "x", future).retry_after, MAX_RETRY_AFTER)

        past = self.now - WINDOW_SECONDS  # 最小計入 ts 是 now − 60
        self.assertTrue(store.accept("past", "n", past).ok)
        self.assertEqual(store.accept("past", "x", past).retry_after, 6)

    def test_c306_waiting_retry_after_then_new_nonce_succeeds(self) -> None:
        store = self.store(per_key_max=1)
        self.assertTrue(store.accept("k", "n1", self.now).ok)
        limited = store.accept("k", "n2", self.now)
        self.assertEqual(limited.outcome, RATE_LIMITED)
        self.assertGreater(limited.retry_after, 0)
        self.clock.advance(seconds=limited.retry_after)
        self.assertTrue(store.accept("k", "n3", now_seconds(self.clock)).ok)


class PurgeTests(ReplayTestBase):
    """C-307 清除、C-308 不變式。"""

    def _bulk_insert(self, store: ReplayStore, key: str, stamps: list[int]) -> None:
        with store.db.txn() as cur:
            cur.executemany(
                "INSERT INTO nonces (key_id, nonce, ts) VALUES (?, ?, ?)",
                [(key, "bulk-{0}".format(index), ts) for index, ts in enumerate(stamps)],
            )

    def test_c307_threshold_65_keeps_66_purges(self) -> None:
        """C-307 邊界寫死：`now = ts + 65` 不清、`ts + 66` 起才清（不引用 `PURGE_SECONDS`）。"""
        store = self.store()
        self._bulk_insert(store, "k", [self.now])
        self.clock.set(_at(self.now + 65))               # now = ts + 65
        self.assertEqual(store.purge_once(), 0)
        self.assertEqual(store.row_count("k"), 1)
        self.assertIsNone(store.water_of("k"))
        self.clock.advance(seconds=1)                   # now = ts + 66
        self.assertEqual(store.purge_once(), 1)
        self.assertEqual(store.row_count("k"), 0)
        self.assertEqual(store.water_of("k"), self.now)

    def test_c307_water_is_never_lowered_by_a_later_batch(self) -> None:
        """C-307／低-3-6：`H[key]` 只升不降。

        `H[key]` 已被前幾輪清除（或 D5 的 reset-key-water）推得較高時，庫中仍可能有
        比它更舊的列（直接寫入、故障後殘留）；下一批刪到較低的最大 ts，不得把 H 拉低。
        """
        store = self.store()
        self._bulk_insert(store, "k", [self.now - 400, self.now - 350, self.now - 200])
        high = self.now - 100
        with store.db.txn() as cur:
            cur.execute("INSERT INTO water (key_id, h) VALUES ('k', ?)", (high,))
        self.clock.advance(seconds=100)                 # 三列都 < now − 65
        self.assertEqual(store.purge_once(), 3)
        self.assertEqual(store.water_of("k"), high)

    def test_c307_only_old_rows_are_deleted(self) -> None:
        store = self.store()
        self._bulk_insert(store, "k", [self.now - 200, self.now - 100, self.now + 50])
        self.clock.advance(seconds=100)
        self.assertEqual(store.purge_once(), 2)
        self.assertEqual(store.row_count("k"), 1)
        self.assertEqual(store.water_of("k"), self.now - 100)

    def test_c307_water_is_max_deleted_ts_same_transaction(self) -> None:
        store = self.store()
        self._bulk_insert(store, "k", [self.now, self.now + 30])
        self._bulk_insert(store, "other", [self.now + 10])
        self.clock.set(_at(self.now + PURGE_SECONDS + 31))
        self.assertEqual(store.purge_once(), 3)
        self.assertEqual(store.water_of("k"), self.now + 30)
        self.assertEqual(store.water_of("other"), self.now + 10)

    def test_c307_batches_are_separate_transactions(self) -> None:
        """每批至多 5000 列、批與批之間放鎖：每一批都是一個獨立交易。"""
        store = self.store()
        stamps = [self.now - 100 for _ in range(PURGE_BATCH + 3)]
        self._bulk_insert(store, "k", stamps)
        self.clock.advance(seconds=100)
        calls = []
        original = AuthDb.txn

        def counting(instance, deadline=None):
            calls.append(1)
            return original(instance, deadline)

        with mock.patch.object(AuthDb, "txn", counting):
            deleted = store.purge_once()
        self.assertEqual(deleted, PURGE_BATCH + 3)
        self.assertEqual(len(calls), 2)  # 5000 + 3，兩批

    def test_c307_small_batch_size_still_drains(self) -> None:
        store = self.store()
        self._bulk_insert(store, "k", [self.now - 100 for _ in range(12)])
        self.clock.advance(seconds=100)
        with mock.patch("lgwd.auth_replay.PURGE_BATCH", 5):
            self.assertEqual(store.purge_once(), 12)

    def test_c307_empty_store_purges_zero(self) -> None:
        self.assertEqual(self.store().purge_once(), 0)

    def test_c307_purge_timer_runs_and_stops(self) -> None:
        store = self.store()
        self._bulk_insert(store, "k", [self.now - 100])
        self.clock.advance(seconds=100)
        timer = PurgeTimer(store, interval=0.05)
        timer.start()
        try:
            deadline = time.monotonic() + 5
            while store.row_count("k") and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(store.row_count("k"), 0)
            self.assertGreaterEqual(timer.purges, 1)
        finally:
            timer.stop()
        self.assertFalse(timer.running)

    def test_c307_purge_timer_swallows_errors(self) -> None:
        store = self.store()
        store.close()
        self._stores.remove(store)
        timer = PurgeTimer(store, interval=0.05)
        self.assertEqual(timer.run_once(), 0)  # 不丟例外
        self.assertEqual(timer.purges, 0)

    def test_c308_every_row_above_water_invariant(self) -> None:
        """C-308：任何時刻庫中每列 `ts > H[key]`；清除後 `H[key] < 該次 now − 65`。"""
        store = self.store(per_key_max=50)
        for index in range(10):
            # ts 在未來 50 秒：清除後仍在庫中（ts > now − 65）。
            self.assertTrue(store.accept("k", "n{0}".format(index), self.now + 50).ok)
        self._bulk_insert(store, "old", [self.now - 300, self.now - 200])
        self.clock.advance(seconds=100)
        last_now = now_seconds(self.clock)
        self.assertEqual(store.purge_once(), 2)
        rows = store.db.read("SELECT key_id, ts FROM nonces")
        water = {str(row[0]): int(row[1]) for row in store.db.read("SELECT key_id, h FROM water")}
        for row in rows:
            key_id, ts = str(row["key_id"]), int(row["ts"])
            self.assertTrue(
                key_id not in water or ts > water[key_id],
                msg="{0}: ts={1} water={2}".format(key_id, ts, water.get(key_id)),
            )
        for key_id, h in water.items():
            if key_id == "old":
                self.assertLess(h, last_now - PURGE_SECONDS)

    def test_c305_keys_with_future_water(self) -> None:
        """`H[key]` 落在未來（時鐘事故 iii）要列得出來，給啟動事件與 llmgwctl check。"""
        store = self.store()
        future = self.now + WINDOW_SECONDS
        self.assertTrue(store.accept("ahead", "n", future).ok)
        self.assertEqual(store.keys_with_future_water(), [])
        self.clock.set(_at(future + PURGE_SECONDS + 1))
        self.assertEqual(store.purge_once(), 1)
        self.assertEqual(store.water_of("ahead"), future)
        self.clock.set(_at(self.now - 1))  # 時鐘回撥：H 現在超過 now＋60
        self.assertEqual(store.keys_with_future_water(), ["ahead"])


class TimeSourceTests(unittest.TestCase):
    """D2 共通：時間一律經注入的 Clock；新程式不直接呼叫 `time.time()`／`datetime.now`。"""

    def test_common_auth_modules_never_call_wall_clock(self) -> None:
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        offenders = []
        for name in ("auth_db.py", "auth_replay.py"):
            path = os.path.join(repo, "lgwd", name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                owner = node.func.value
                if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in (
                    ("time", "time"), ("datetime", "now"), ("datetime", "utcnow")
                ):
                    offenders.append("{0}:{1}".format(name, node.lineno))
        self.assertEqual(offenders, [])


class NowInsideTransactionTests(ReplayTestBase):
    """C-305／C-304：`now` 必須在拿到鎖並 `BEGIN IMMEDIATE` 之後才讀（同一交易只用一次）。"""

    def test_c305_now_is_read_after_the_lock(self) -> None:
        store = self.store()
        holding = threading.Event()
        advance = threading.Event()
        advanced = threading.Event()

        def holder() -> None:
            with store.db.txn():
                holding.set()
                advance.wait(5)
                self.clock.advance(seconds=WINDOW_SECONDS + 1)
                advanced.set()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        try:
            self.assertTrue(holding.wait(5))
            results: list = []

            def worker() -> None:
                results.append(store.accept("k", "n", self.now))

            worker_thread = threading.Thread(target=worker)
            worker_thread.start()
            time.sleep(0.05)          # 讓 worker 卡在行程內鎖上
            advance.set()
            self.assertTrue(advanced.wait(5))
            holder_thread.join(5)
            worker_thread.join(5)
        finally:
            advance.set()
            holder_thread.join(5)

        self.assertEqual(len(results), 1)
        # 交易內的 now 是推進後的時間，所以 ts 已超出時窗（若在鎖外先讀就會被接受）。
        self.assertEqual(results[0].reason, REASON_EXPIRED)
        self.assertEqual(store.row_count("k"), 0)


class ConcurrencyTests(ReplayTestBase):
    """C-305：並行（32 執行緒）與清除／重放交錯。"""

    def test_c305_concurrent_same_nonce_exactly_one_accepted(self) -> None:
        store = self.store(per_key_max=10_000, busy_timeout=CONCURRENT_BUSY_TIMEOUT)
        threads = 32
        barrier = threading.Barrier(threads)
        results: list = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            result = store.accept("k", "shared-nonce", self.now)
            with lock:
                results.append(result)

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(30)
        self.assertEqual(len(results), threads)
        self.assertEqual(len([item for item in results if item.ok]), 1)
        self.assertEqual({item.reason for item in results if not item.ok}, {REASON_REPLAY})
        self.assertEqual(store.row_count("k"), 1)

    def _hammer_distinct_nonces(self, store: ReplayStore) -> list:
        """32 執行緒各 500 個不同 nonce；回傳失敗清單（例外型別或 `outcome:reason`）。"""
        barrier = threading.Barrier(CONCURRENT_THREADS)
        failures: list = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            key = "k{0:02d}".format(index)
            barrier.wait()
            for number in range(CONCURRENT_PER_THREAD):
                try:
                    result = store.accept(key, "n{0:04d}".format(number), self.now)
                except Exception as exc:  # 任何例外都算失敗
                    with lock:
                        failures.append(type(exc).__name__)
                    return
                if not result.ok:
                    with lock:
                        failures.append("{0}:{1}".format(result.outcome, result.reason))

        workers = [threading.Thread(target=worker, args=(index,))
                   for index in range(CONCURRENT_THREADS)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(300)
        return failures

    def test_c305_concurrent_distinct_nonces_zero_unavailable(self) -> None:
        """C-305 (a) 功能：**永遠執行**（期限放寬到 30 秒），斷言零例外／零 unavailable、
        全數 ACCEPTED、列數＝32×500。機器多慢都不該讓這一支消失。"""
        store = self.store(per_key_max=600, busy_timeout=CONCURRENT_BUSY_TIMEOUT)
        failures = self._hammer_distinct_nonces(store)
        self.assertEqual(failures[:5], [])
        self.assertEqual(store.row_count(), CONCURRENT_THREADS * CONCURRENT_PER_THREAD)

    def _paced_accepts(self, store: ReplayStore, rate: float, duration: float) -> tuple[list, int]:
        """32 條執行緒各自按固定間隔打 accept，整體約 `rate` 筆/秒，持續 `duration` 秒。

        每條執行緒的排程以開始時刻為基準（不累加誤差）；各執行緒的起點錯開一小段，讓到達
        是平均的而不是 32 條同時撞上。回 (失敗清單, 送出筆數)；失敗＝例外型別或 `outcome:reason`。
        """
        interval = CONCURRENT_THREADS / rate
        per_thread = int(duration / interval)
        start = time.monotonic() + 0.5
        lock = threading.Lock()
        failures: list = []

        def worker(index: int) -> None:
            key = "k{0:02d}".format(index)
            offset = index * interval / CONCURRENT_THREADS
            for number in range(per_thread):
                pause = start + offset + number * interval - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
                try:
                    result = store.accept(key, "p{0:04d}".format(number), self.now)
                except Exception as exc:  # 任何例外都算失敗
                    with lock:
                        failures.append(type(exc).__name__)
                    continue
                if not result.ok:
                    with lock:
                        failures.append("{0}:{1}".format(result.outcome, result.reason))

        workers = [threading.Thread(target=worker, args=(index,))
                   for index in range(CONCURRENT_THREADS)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(duration + 120)
        return failures, per_thread * CONCURRENT_THREADS

    def _observe_paced(self, rate: float, label: str) -> None:
        """固定到達率的等鎖觀察：只印統計與警示，**不斷言**（#311 fix1，(c)）。

        `_time_branch_skip` 的結果只寫進說明（環境是否已壓到量不出設計值），不決定通過與否。
        """
        load, cores, _ = _oversubscribed()
        io_before = _read_io_pressure()
        store = self.store(per_key_max=600, busy_timeout=DEFAULT_BUSY_TIMEOUT,
                           collect_lock_waits=True)
        holds: list = []
        _record_hold_times(store.db, holds)
        started = time.monotonic()
        failures, offered = self._paced_accepts(store, rate, TIME_DURATION_SECONDS)
        elapsed = time.monotonic() - started
        io_after = _read_io_pressure()
        waits = sorted(store.db.lock_waits or [])
        hold_mean = sum(holds) / len(holds) if holds else float("nan")
        p99 = _p99(waits) if waits else float("nan")
        skip, why = _time_branch_skip(load, cores, io_before, io_after,
                                      _p99(holds) if holds else None)
        line = ("{0}（觀察，不 gating）：rate={1:.1f}/s n={2} failures={3} wait_p99={4:.3f}s "
                "wait_max={5:.3f}s hold_mean={6:.4f}s hold_p99={7:.3f}s load1={8:.2f} cores={9} "
                "io_some={10} cpu_some={11}；環境判定={12}（{13}）").format(
            label, offered / elapsed, offered, len(failures), p99,
            waits[-1] if waits else float("nan"), hold_mean,
            _p99(holds) if holds else float("nan"), load, cores,
            "{0:.2f}/{1:.2f}".format(io_before[0], io_after[0]) if io_before and io_after else "n/a",
            _read_cpu_some(), "環境壓力" if skip else "未達環境壓力", why)
        sys.stderr.write(line + "\n")
        if failures or p99 >= LOCK_WAIT_P99_LIMIT:
            warnings.warn(line, stacklevel=1)

    def test_c305_paced_30_observation_not_gating(self) -> None:
        """C-305 時間支（#311 fix1 裁定 (c)）：固定到達率 30 筆/秒、15 秒，**不 gating**。

        沿革：#311 原判準是等鎖 p99 < 1 秒（50/s，再改 25/s、30/s）。受控共載下 (d) 的假失敗超過一次
        （REPORT-311-fix1 §2），依 BRIEF-311-fix1 降為觀察：只印統計與警示，不斷言 p99，也不斷言零
        StoreBusy。要設 LGW_SLOW_TESTS=1 才跑。**代價**：CI 沒有鎖退化的自動 gating；鎖退化的自動訊號
        只剩功能支 `test_c305_concurrent_distinct_nonces_zero_unavailable`（30 秒期限，零 unavailable）。
        """
        if os.environ.get("LGW_SLOW_TESTS") != "1":
            self.skipTest("等鎖觀察（不 gating）：設 LGW_SLOW_TESTS=1 才跑")
        self._observe_paced(TIME_RATE_PER_SECOND, "paced30")

    def test_c305_paced_100_observation_not_gating(self) -> None:
        """C-305 固定到達率 100 筆/秒觀察（#311 fix1；**不 gating**）：設計操作點的 4 倍。

        同一套 `_paced_accepts`、15 秒、正式預設 5 秒期限；只印統計與警示，**不斷言**。
        要設 LGW_SLOW_TESTS=1 才跑。用來看單寫者在這台機器上的容量邊界。
        """
        if os.environ.get("LGW_SLOW_TESTS") != "1":
            self.skipTest("100 筆/秒觀察（不 gating）：設 LGW_SLOW_TESTS=1 才跑")
        self._observe_paced(100, "paced100")

    def test_c305_stress_backtoback_observation_not_gating(self) -> None:
        """C-305 背靠背壓力觀察（#311；**不 gating**）：32 條執行緒背靠背各 500 次 accept。

        這是悲觀上界（遠超設計持續率，見 `test_c305_paced_30_observation_not_gating`），
        只印統計與警示，**不斷言 p99**。要設 LGW_SLOW_TESTS=1 才跑。
        """
        if os.environ.get("LGW_SLOW_TESTS") != "1":
            self.skipTest("壓力觀察（不 gating）：設 LGW_SLOW_TESTS=1 才跑")
        load, cores, _ = _oversubscribed()
        store = self.store(per_key_max=600, busy_timeout=DEFAULT_BUSY_TIMEOUT,
                           collect_lock_waits=True)
        failures = self._hammer_distinct_nonces(store)
        waits = sorted(store.db.lock_waits or [])
        line = ("stress（悲觀上界，不 gating）：n={0} failures={1} wait_p99={2:.3f}s "
                "wait_max={3:.3f}s load1={4:.2f} cores={5}").format(
            len(waits), len(failures), _p99(waits), waits[-1], load, cores)
        sys.stderr.write(line + "\n")
        if failures or _p99(waits) >= LOCK_WAIT_P99_LIMIT:
            warnings.warn(line, stacklevel=1)

    def test_c305_purge_and_replay_interleave_at_most_once(self) -> None:
        """到期前一刻重放與清除交錯 1000 輪，每輪至多接受一次。"""
        main = self.store(per_key_max=1000, busy_timeout=CONCURRENT_BUSY_TIMEOUT)
        purger_clock = FakeClock()
        purger_clock.set(_at(self.now))
        purger = self.store(clock=purger_clock, per_key_max=1000,
                            busy_timeout=CONCURRENT_BUSY_TIMEOUT)
        accepted = 0
        for round_number in range(1000):
            key = "k{0:04d}".format(round_number)
            nonce = "n{0:04d}".format(round_number)
            self.assertTrue(main.accept(key, nonce, self.now).ok)
            accepted += 1
            purger_clock.set(_at(self.now + PURGE_SECONDS + 1))
            barrier = threading.Barrier(3)
            replay_results: list = []

            def purge() -> None:
                barrier.wait()
                purger.purge_once()

            def replay() -> None:
                barrier.wait()
                replay_results.append(main.accept(key, nonce, self.now))

            purging = threading.Thread(target=purge)
            replaying = threading.Thread(target=replay)
            purging.start()
            replaying.start()
            barrier.wait()
            purging.join(30)
            replaying.join(30)
            self.assertEqual(len(replay_results), 1)
            self.assertFalse(replay_results[0].ok, msg="round {0}".format(round_number))
            self.assertIn(
                replay_results[0].reason, (REASON_REPLAY, REASON_WATER),
                msg="round {0}".format(round_number),
            )
        self.assertEqual(accepted, 1000)


class TimeBranchSkipRuleTests(unittest.TestCase):
    """#311：時間支 skip 決定（純函式表格測試，不碰機器）。"""

    def decide(self, load, cores, io_before, io_after, hold):
        return _time_branch_skip(load, cores, io_before, io_after, hold)

    def test_parse_io_pressure_reads_some_and_full_avg10(self):
        text = ("some avg10=1.37 avg60=1.17 avg300=1.83 total=65210540783\n"
                "full avg10=1.14 avg60=1.01 avg300=1.48 total=26248009840\n")
        self.assertEqual(_parse_io_pressure(text), (1.37, 1.14))

    def test_parse_io_pressure_rejects_garbage(self):
        for text in (None, "", "some avg60=1.0\nfull avg60=1.0\n", "nonsense", "some avg10=x\nfull avg10=1\n"):
            self.assertIsNone(_parse_io_pressure(text), repr(text))

    def test_outer_load_rule_still_skips_first(self):
        skip, detail = self.decide(33.0, 16, (0.0, 0.0), (0.0, 0.0), 0.01)
        self.assertTrue(skip)
        self.assertIn("load1=33.00", detail)
        self.assertIn("16", detail)

    def test_load_exactly_twice_cores_does_not_skip(self):
        skip, _ = self.decide(32.0, 16, (0.0, 0.0), (0.0, 0.0), 0.01)
        self.assertFalse(skip)

    def test_clean_pressure_does_not_skip(self):
        skip, detail = self.decide(3.0, 16, (1.37, 1.14), (2.0, 1.5), 0.02)
        self.assertFalse(skip, detail)
        self.assertIn("some avg10=2.00", detail)

    def test_clean_baseline_max_38_does_not_skip(self):
        # REPORT-310-impl 乾淨基線 some 最大約 38（中位 26）：不得因此 skip。
        skip, detail = self.decide(9.5, 16, (26.0, 10.0), (37.8, 12.0), 0.03)
        self.assertFalse(skip, detail)

    def test_pressure_before_run_alone_skips(self):
        skip, detail = self.decide(9.0, 16, (45.3, 10.0), None, 0.03)
        self.assertTrue(skip)
        self.assertIn("some avg10=45.30", detail)

    def test_pressure_after_run_alone_skips(self):
        skip, detail = self.decide(9.0, 16, (1.0, 1.0), (10.0, 31.2), 0.03)
        self.assertTrue(skip)
        self.assertIn("full avg10=31.20", detail)

    def test_some_over_limit_skips(self):
        skip, detail = self.decide(9.0, 16, (41.0, 0.0), (0.0, 0.0), 0.03)
        self.assertTrue(skip, detail)

    def test_no_pressure_uses_hold_fallback_slow_disk_skips(self):
        skip, detail = self.decide(9.0, 16, None, None, 0.35)
        self.assertTrue(skip)
        self.assertIn("磁碟太慢", detail)
        self.assertIn("0.350s", detail)

    def test_no_pressure_fast_disk_asserts(self):
        skip, detail = self.decide(9.0, 16, None, None, 0.05)
        self.assertFalse(skip, detail)

    def test_no_pressure_and_no_hold_asserts(self):
        skip, detail = self.decide(9.0, 16, None, None, None)
        self.assertFalse(skip, detail)
        self.assertIn("照常斷言", detail)

    def test_pressure_available_takes_precedence_over_hold(self):
        # 有 /proc/pressure 時不看持鎖退路：壓力正常、持鎖慢也不因退路 skip。
        skip, detail = self.decide(9.0, 16, (1.0, 1.0), (1.0, 1.0), 0.9)
        self.assertFalse(skip, detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
