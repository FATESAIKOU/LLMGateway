"""StoreBusy 計數、取樣、狀態檔與 `auth-busy` 檢查（#312；契約 C-1000～C-1011）。

全部用暫存目錄、暫存 SQLite、假時鐘與假單調時鐘；不碰 /var/lib、/etc，不讀憑證，不連真機。
不做時間斷言（不重蹈 #311）：C-1004 只比數量；C-1005 只比「在 1 秒內返回」這一個寬鬆上限。
"""

from __future__ import annotations

import datetime as _dt
import inspect
import json
import logging
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from lgwd import auth, auth_busy, framing, sshsig
from lgwd.auth_busy import (
    BusyCounter,
    BusyStatusBad,
    BusyStatusMissing,
    BusyWindow,
    parse_status,
    read_status,
    status_bytes,
    write_status,
)
from lgwd.auth_db import (
    StoreBusy,
    StoreMissing,
    StoreUnavailable,
    StoreUnclean,
    create_stores,
)
from lgwd.auth_paths import auth_paths, now_seconds
from lgwd.auth_registry import parse_registry
from lgwd.auth_replay import ACCEPTED, DENIED, Accept, ReplayStore
from lgwd.clock import FakeClock
from lgwctl import check
from lgwctl.report import Report
from tests.auth_fakes import REPO_ROOT, write_registry_file
from tests.test_main import FAKE_KEYS, key_text, write_policy
from tests.auth_signer import Signer, key_entry, registry_bytes

UTC = _dt.timezone.utc
NOW = _dt.datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
TARGET = "/v1/subscriptions"


class Mono:
    """假單調時鐘：只在測試裡手動推進。"""

    def __init__(self, start: float = 0.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t


class Headers:
    """`email.message.Message` 的最小替身（同 test_auth_gate）。"""

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


class FakeVerifier:
    def __init__(self, outcome=sshsig.Outcome.PASS):
        self.outcome = outcome

    def verify(self, message, blob, entry, signers):
        return self.outcome

    def recheck_if_changed(self):
        return True


class FakeReplay:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else Accept(ACCEPTED)
        self.error = error

    def accept(self, key_id, nonce, ts):
        if self.error is not None:
            raise self.error
        return self.result


def _nonce(seed: int) -> str:
    import base64
    return base64.urlsafe_b64encode(bytes([seed]) * 32).rstrip(b"=").decode("ascii")


def _framing(target=TARGET):
    return framing.Framing(target_ok=True, target=target, path=target, query="",
                           length=0, too_large=False, ok=True)


# ---------------------------------------------------------------------------
# C-1001～C-1003：gate 的分類、計數與回應
# ---------------------------------------------------------------------------

class GateBusyBase(unittest.TestCase):
    """真的名冊與 allowed_signers（同 test_auth_gate），替身 verifier，replay 由每個測試決定。"""

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
        self.dir = self._tmp.name
        self.clock = FakeClock(NOW)
        self.now = now_seconds(self.clock)
        self.registry = parse_registry(registry_bytes(1, [key_entry("k-a", self.signer)]))
        self.signers = sshsig.write_allowed_signers(
            self.registry.active_keys(), self.dir, self.registry.generation)
        self.counter = BusyCounter()
        self.replay = FakeReplay()
        self.gate = auth.AuthGate(mode="enforce", clock=self.clock, verifier=FakeVerifier(),
                                  replay=self.replay, busy_counter=self.counter)
        self.gate.publish(self.registry, self.signers)
        self.headers = Headers(self.signer.headers(
            key_id="k-a", method="GET", target=TARGET, body=b"",
            timestamp=self.now, nonce=_nonce(1)))

    def authenticate(self):
        return self.gate.authenticate(method="GET", framing=_framing(), headers=self.headers,
                                      body=b"")


class GateClassificationTests(GateBusyBase):
    def test_C1000_storebusy_is_store_busy_and_others_stay_store_unavailable(self):
        cases = (
            (StoreBusy("busy"), auth.REASON_STORE_BUSY),
            (StoreUnavailable("io"), auth.REASON_STORE_UNAVAILABLE),
            (StoreUnavailable("full"), auth.REASON_STORE_UNAVAILABLE),
            (StoreMissing("missing"), auth.REASON_STORE_UNAVAILABLE),
            (StoreUnavailable("corrupt"), auth.REASON_STORE_UNAVAILABLE),
            (StoreUnavailable("readonly"), auth.REASON_STORE_UNAVAILABLE),
            (StoreUnavailable("unknown"), auth.REASON_STORE_UNAVAILABLE),
            (StoreUnclean(), auth.REASON_STORE_UNAVAILABLE),
            (OSError("closed"), auth.REASON_STORE_UNAVAILABLE),
            (RuntimeError("bug"), auth.REASON_STORE_UNAVAILABLE),
        )
        for error, reason in cases:
            with self.subTest(error=type(error).__name__, kind=str(error)):
                self.replay.error = error
                verdict = self.authenticate()
                self.assertEqual(verdict.outcome, auth.OUTCOME_UNAVAILABLE)
                self.assertEqual(verdict.reason, reason)

    def test_C1000_none_replay_is_store_unavailable(self):
        self.gate.replay = None
        verdict = self.authenticate()
        self.assertEqual(verdict.reason, auth.REASON_STORE_UNAVAILABLE)
        self.assertEqual(self.counter.total(), 0)

    def test_C1001_only_storebusy_counts(self):
        self.replay.error = StoreBusy("busy")
        self.authenticate()
        self.assertEqual(self.counter.total(), 1)
        for error in (StoreUnavailable("io"), OSError("x"), RuntimeError("y")):
            self.replay.error = error
            self.authenticate()
        self.assertEqual(self.counter.total(), 1)
        self.replay.error = None
        for result in (Accept(DENIED, "replay"), Accept(DENIED, "expired"),
                       Accept(ACCEPTED)):
            self.replay.result = result
            self.authenticate()
        self.assertEqual(self.counter.total(), 1)

    def test_C1001_no_counter_gives_the_same_verdict(self):
        self.replay.error = StoreBusy("busy")
        with_counter = self.authenticate()
        self.gate.busy_counter = None
        without_counter = self.authenticate()
        self.assertEqual(with_counter, without_counter)
        self.assertEqual(without_counter.reason, auth.REASON_STORE_BUSY)


class GateResponseAndLogTests(GateBusyBase):
    def test_C1002_enforce_503_is_byte_identical_to_store_unavailable(self):
        busy = auth.apply("enforce", auth.unavailable(auth.REASON_STORE_BUSY))
        plain = auth.apply("enforce", auth.unavailable(auth.REASON_STORE_UNAVAILABLE))
        self.assertEqual(busy.kind, auth.ACTION_RESPOND)
        self.assertEqual(busy.status, 503)
        self.assertEqual(busy.status, plain.status)
        self.assertEqual(busy.error.body(), plain.error.body())
        self.assertEqual(busy.headers, plain.headers)
        self.assertEqual(busy.headers, ())

    def test_C1002_warn_continues(self):
        action = auth.apply("warn", auth.unavailable(auth.REASON_STORE_BUSY))
        self.assertEqual(action.kind, auth.ACTION_CONTINUE)

    def test_C1003_log_line_format_and_no_request_data(self):
        lines: list[str] = []
        log = auth.AuthLog(sink=lines.append, clock=Mono(0.0))
        self.replay.error = StoreBusy("busy")
        verdict = self.authenticate()
        auth.apply("enforce", verdict, route="/v1/subscriptions", log=log)
        self.assertEqual(lines, [
            "auth mode=enforce outcome=unavailable reason=store_busy "
            "route=/v1/subscriptions principal=- count=1"])
        for needle in ("k-a", _nonce(1), self.headers.get("X-LGW-Signature")):
            self.assertNotIn(needle, lines[0])


# ---------------------------------------------------------------------------
# C-1000 (a)(b)(c)、C-1004、C-1005：真實 SQLite 與真實鎖
# ---------------------------------------------------------------------------

class RealStoreBase(GateBusyBase):
    def setUp(self):
        super().setUp()
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir, mode=0o700)
        self.paths = auth_paths(self.state_dir)
        create_stores(self.paths.trust_db, self.paths.replay_db)
        self.store = ReplayStore(self.paths.replay_db, self.clock, busy_timeout=0.2)
        self.addCleanup(self.store.close)
        self.gate.replay = self.store

    def hold_write_lock(self):
        holder = sqlite3.connect(self.paths.replay_db, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: holder.execute("ROLLBACK") if holder.in_transaction else None)


class RealStoreBusyTests(RealStoreBase):
    def test_C1000a_begin_immediate_timeout_is_store_busy(self):
        self.hold_write_lock()
        verdict = self.authenticate()
        self.assertEqual(verdict.reason, auth.REASON_STORE_BUSY)
        self.assertEqual(self.counter.total(), 1)

    def test_C1000b_in_process_lock_timeout_is_store_busy(self):
        # 另一條執行緒持有 AuthDb._lock 直到測試結束；accept 在 busy_timeout 內拿不到鎖。
        held = threading.Event()
        release = threading.Event()

        def holder():
            with self.store._db._lock:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            verdict = self.authenticate()
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(verdict.reason, auth.REASON_STORE_BUSY)
        self.assertEqual(self.counter.total(), 1)

    def test_C1000c_database_is_locked_on_insert_is_store_busy(self):
        real = self.store._db._conn

        class LockedInsertCursor:
            def __init__(self, cur):
                self._cur = cur

            def execute(self, sql, params=()):
                if sql.lstrip().upper().startswith("INSERT"):
                    raise sqlite3.OperationalError("database is locked")
                return self._cur.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._cur, name)

        class LockedInsertConn:
            def cursor(self):
                return LockedInsertCursor(real.cursor())

            def __getattr__(self, name):
                return getattr(real, name)

        self.store._db._conn = LockedInsertConn()
        verdict = self.authenticate()
        self.assertEqual(verdict.reason, auth.REASON_STORE_BUSY)
        self.assertEqual(self.counter.total(), 1)
        self.store._db._conn = real

    def test_C1004_three_numbers_agree_under_contention(self):
        self.store.busy_timeout = 0.05
        self.hold_write_lock()
        log_clock = Mono(0.0)
        lines: list[str] = []
        log = auth.AuthLog(sink=lines.append, clock=log_clock, window=60.0)
        gate_busy = [0]
        gate_lock = threading.Lock()

        def worker():
            for _ in range(8):
                verdict = self.authenticate()
                auth.apply("enforce", verdict, route="/v1/subscriptions", log=log)
                if verdict.reason == auth.REASON_STORE_BUSY:
                    with gate_lock:
                        gate_busy[0] += 1

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        n1 = gate_busy[0]          # 第一個視窗內 gate 回 store_busy 的筆數
        self.assertGreaterEqual(n1, 2)
        # 假時鐘推過 60 秒，再打一次，讓 AuthLog 滾動寫出第一個視窗的累計。
        log_clock.t += 61.0
        verdict = self.authenticate()
        auth.apply("enforce", verdict, route="/v1/subscriptions", log=log)
        self.assertEqual(verdict.reason, auth.REASON_STORE_BUSY)
        self.assertEqual(self.counter.total(), n1 + 1)
        # 日誌行序列（修訂 3）：第一視窗開頭即時通知 1、滾動寫出的第一視窗累計 N1、
        # 新視窗開頭即時通知 1。不把 count 加總（AuthLog 的 count 不可相加）。
        counts = [int(line.rsplit("count=", 1)[1]) for line in lines
                  if " reason=store_busy " in line]
        self.assertEqual(counts, [1, n1, 1])

    def test_C1004_counter_is_exact_across_threads(self):
        counter = BusyCounter()
        threads_n, per = 32, 1000

        def hammer():
            for _ in range(per):
                counter.hit()

        threads = [threading.Thread(target=hammer) for _ in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(counter.total(), threads_n * per)

    def test_C1005_hit_and_total_take_only_their_own_lock(self):
        class SpyLock:
            def __init__(self, inner):
                self._inner = inner
                self.acquired = 0

            def __enter__(self):
                self.acquired += 1
                return self._inner.__enter__()

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        spy = SpyLock(self.counter._lock)
        self.counter._lock = spy
        self.counter.hit()
        self.counter.total()
        self.assertEqual(spy.acquired, 2)

    def test_C1005_hit_returns_while_gate_and_log_and_store_locks_are_held(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with self.gate._lock, self.log_lock_holder_target._lock, self.store._db._lock:
                held.set()
                release.wait(5)

        self.log_lock_holder_target = auth.AuthLog(sink=lambda line: None)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            started = time.monotonic()
            with mock.patch.object(auth_busy, "time") as clock_mock, \
                    mock.patch("builtins.open") as open_mock, \
                    mock.patch("os.open") as os_open_mock, \
                    self.assertNoLogs(level=logging.DEBUG):
                self.counter.hit()
                self.counter.total()
            elapsed = time.monotonic() - started
            self.assertEqual(clock_mock.mock_calls, [])
            self.assertEqual(open_mock.mock_calls, [])
            self.assertEqual(os_open_mock.mock_calls, [])
            self.assertLess(elapsed, 1.0)
            self.assertEqual(self.counter.total(), 1)
        finally:
            release.set()
            thread.join(5)


# ---------------------------------------------------------------------------
# C-1006：取樣視窗
# ---------------------------------------------------------------------------

class BusyWindowTests(unittest.TestCase):
    def setUp(self):
        self.counter = BusyCounter()
        self.mono = Mono(0.0)
        self.window = BusyWindow(self.counter, monotonic=self.mono)

    def hit(self, n=1):
        for _ in range(n):
            self.counter.hit()

    def test_i_before_30_minutes_equals_since_start(self):
        self.hit(3)
        self.mono.t = 600.0
        self.assertEqual(self.window.sample(), (3, 3, 3))

    def test_ii_two_hours_without_hits_gives_zero_windows(self):
        self.hit(5)
        self.mono.t = 60.0
        self.assertEqual(self.window.sample(), (5, 5, 5))
        self.mono.t = 7260.0
        self.assertEqual(self.window.sample(), (5, 0, 0))

    def test_iii_clock_jump_gives_hits_since_last_sample_not_total(self):
        self.hit(47)
        self.mono.t = 60.0
        self.assertEqual(self.window.sample(), (47, 47, 47))
        self.hit(3)
        self.mono.t = 7260.0
        self.assertEqual(self.window.sample(), (50, 3, 3))

    def test_iv_sample_count_is_bounded(self):
        longest = 0
        for step in range(1, 3 * 60 + 1):
            self.mono.t = step * 60.0
            self.window.sample()
            longest = max(longest, len(self.window._samples))
        self.assertLessEqual(longest, 62)

    def test_v_window_does_not_use_wall_clock(self):
        self.assertNotIn("clock", inspect.signature(BusyWindow).parameters)
        self.hit(2)
        self.mono.t = 60.0
        self.assertEqual(self.window.sample(), (2, 2, 2))

    def test_vi_30_minute_boundary_follows_the_sampling_rule(self):
        # 每 60 秒取樣一次；第一次 hit 落在 61 秒，也就是 60 秒樣本與 120 秒樣本之間。
        self.mono.t = 60.0
        self.window.sample()
        for second in range(120, 3781, 60):
            if second == 120:
                self.mono.t = 61.0
                self.hit(1)
            self.mono.t = float(second)
            result = self.window.sample()
            if second == 1860:   # 離 hit 約 30 分鐘，cut=60：仍在窗口內
                self.assertEqual(result[1], 1)
            elif second == 1920:  # cut=120：hit 已經在基準之前，不在窗口內
                self.assertEqual(result[1], 0)
            elif second == 3660:  # 60 分鐘窗口：仍在內
                self.assertEqual(result[2], 1)
            elif second == 3720:
                self.assertEqual(result[2], 0)


# ---------------------------------------------------------------------------
# C-1007：狀態檔的格式、讀寫與寫入失敗
# ---------------------------------------------------------------------------

def _document(**over):
    document = {"schema": 1, "mode": "enforce", "since": 1764000000, "updated": 1764003600,
                "store_busy_total": 3, "store_busy_30m": 0, "store_busy_60m": 2}
    document.update(over)
    return document


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, ".auth-busy.status")

    def test_status_write_never_fsyncs(self):
        with mock.patch("lgwd.auth_busy.os.fsync") as fsync:
            write_status(self.path, _document())
        fsync.assert_not_called()

    def test_round_trip_and_mode_0644(self):
        write_status(self.path, _document())
        self.assertEqual(read_status(self.path), _document())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        self.assertEqual(os.listdir(self._tmp.name), [".auth-busy.status"])
        self.assertLess(len(status_bytes(_document())), 200)

    def test_strict_parse_rejects_each_bad_shape(self):
        bad = {
            "extra key": {**_document(), "x": 1},
            "missing key": {k: v for k, v in _document().items() if k != "since"},
            "schema 2": _document(schema=2),
            "schema true": _document(schema=True),
            "mode off": _document(mode="off"),
            "mode bool": _document(mode=True),
            "negative": _document(store_busy_30m=-1),
            "float": _document(store_busy_total=1.0),
            "bool count": _document(store_busy_60m=False),
            "string count": _document(since="1"),
        }
        for name, document in bad.items():
            with self.subTest(name):
                with self.assertRaises(BusyStatusBad):
                    parse_status(json.dumps(document).encode("utf-8"))
        for data in (b"\xff\xfe", b"[1,2]", b"", b"{not json"):
            with self.subTest(data=data):
                with self.assertRaises(BusyStatusBad):
                    parse_status(data)

    def test_missing_file_is_missing(self):
        with self.assertRaises(BusyStatusMissing):
            read_status(self.path)

    def test_oversize_and_bad_utf8_and_symlink_and_directory(self):
        with open(self.path, "wb") as fh:
            fh.write(b" " * 4097)
        with self.assertRaises(BusyStatusBad) as ctx:
            read_status(self.path)
        self.assertEqual(ctx.exception.reason, "too_large")
        with open(self.path, "wb") as fh:
            fh.write(b"\xff" * 10)
        with self.assertRaises(BusyStatusBad) as ctx:
            read_status(self.path)
        self.assertEqual(ctx.exception.reason, "format")
        os.unlink(self.path)
        os.symlink(os.path.join(self._tmp.name, "elsewhere"), self.path)
        with self.assertRaises(BusyStatusBad) as ctx:
            read_status(self.path)
        self.assertEqual(ctx.exception.reason, "unreadable")
        os.unlink(self.path)
        os.mkdir(self.path)
        with self.assertRaises(BusyStatusBad) as ctx:
            read_status(self.path)
        self.assertEqual(ctx.exception.reason, "unreadable")

    def test_write_failure_raises_oserror_and_leaves_no_temp_file(self):
        with mock.patch("lgwd.auth_busy.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                write_status(self.path, _document())
        self.assertEqual(os.listdir(self._tmp.name), [])


class RuntimeWriterTests(unittest.TestCase):
    """`AuthRuntime._busy_tick` 的寫檔節奏、重試與日誌（C-1007 (i)～(iv)）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, mode=0o700)
        self.paths = auth_paths(self.state_dir)
        self.clock = FakeClock(NOW)
        cfg = auth.AuthConfig(mode="enforce", state_dir=self.state_dir,
                              registry_path=os.path.join(self._tmp.name, "reg.json"))
        self.runtime = auth.AuthRuntime(cfg, self.clock, self.paths, owner_uid=os.getuid())
        self.runtime.busy_counter = BusyCounter()
        self.mono = Mono(1000.0)
        patcher = mock.patch.object(auth, "_monotonic", lambda: self.mono.t)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime.busy_window = BusyWindow(self.runtime.busy_counter,
                                              monotonic=lambda: auth._monotonic())
        self.runtime._busy_since = now_seconds(self.clock)
        self.runtime.watcher = object()   # 只要不是 None：寫檔只在 watcher 存在時做

    def test_first_tick_writes_then_every_60_seconds(self):
        self.runtime._busy_tick()
        self.assertTrue(os.path.isfile(self.paths.busy_status))
        self.mono.t += 30
        with mock.patch("lgwd.auth.write_status") as writer:
            self.runtime._busy_tick()
        writer.assert_not_called()
        self.mono.t += 30
        with mock.patch("lgwd.auth.write_status") as writer:
            self.runtime._busy_tick()
        writer.assert_called_once()

    def test_no_watcher_or_no_window_means_no_write(self):
        self.runtime.watcher = None
        self.runtime._busy_tick()
        self.assertFalse(os.path.exists(self.paths.busy_status))

    def test_i_five_failed_ticks_log_once_and_try_every_time(self):
        self.mono.t += 2
        with mock.patch("lgwd.auth.write_status", side_effect=OSError("x")) as writer, \
                self.assertLogs("lgwd.auth", level="WARNING") as logs:
            for _ in range(5):
                self.mono.t += 2
                self.runtime._busy_tick()
        self.assertEqual(writer.call_count, 5)
        self.assertEqual(len(logs.records), 1)

    def test_ii_fail_success_fail_logs_twice(self):
        with mock.patch("lgwd.auth.write_status", side_effect=OSError("x")), \
                self.assertLogs("lgwd.auth", level="WARNING") as logs:
            self.runtime._busy_tick()
        self.mono.t += 2
        self.runtime._busy_tick()   # 補寫成功
        self.mono.t += 60
        with mock.patch("lgwd.auth.write_status", side_effect=OSError("x")), \
                self.assertLogs("lgwd.auth", level="WARNING") as logs2:
            self.runtime._busy_tick()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(logs2.records), 1)

    def test_iii_after_recovery_next_write_waits_60_seconds(self):
        with mock.patch("lgwd.auth.write_status", side_effect=OSError("x")), \
                self.assertLogs("lgwd.auth", level="WARNING"):
            self.runtime._busy_tick()
        self.mono.t += 2
        self.runtime._busy_tick()                       # 補寫成功
        self.mono.t += 30
        with mock.patch("lgwd.auth.write_status") as writer:
            self.runtime._busy_tick()
        writer.assert_not_called()
        self.mono.t += 30
        with mock.patch("lgwd.auth.write_status") as writer:
            self.runtime._busy_tick()
        writer.assert_called_once()

    def test_iv_log_line_has_no_state_dir_path(self):
        error = PermissionError(13, "Permission denied", self.paths.busy_status)
        with mock.patch("lgwd.auth.write_status", side_effect=error), \
                self.assertLogs("lgwd.auth", level="WARNING") as logs:
            self.runtime._busy_tick()
        message = logs.output[0]
        self.assertIn("busy_status_write_failed", message)
        self.assertIn("kind=PermissionError", message)
        self.assertNotIn(self.state_dir, message)
        self.assertNotIn("Permission denied", message)

    def test_written_document_has_the_contract_fields(self):
        self.runtime.busy_counter.hit()
        self.runtime._busy_tick()
        document = read_status(self.paths.busy_status)
        self.assertEqual(document["mode"], "enforce")
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["store_busy_total"], 1)
        self.assertEqual(document["since"], now_seconds(self.clock))
        self.assertEqual(document["updated"], now_seconds(self.clock))


# ---------------------------------------------------------------------------
# 真實 open_runtime 的接線（修訂 mid-1）與端到端
# ---------------------------------------------------------------------------

class RuntimeWiringTests(unittest.TestCase):
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
        self.dir = self._tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir, mode=0o700)
        self.paths = auth_paths(self.state_dir,
                                registry=os.path.join(self.dir, "auth-registry.json"))
        create_stores(self.paths.trust_db, self.paths.replay_db)
        write_registry_file(self.paths.registry,
                            registry_bytes(1, [key_entry("k-a", self.signer)]))
        self.clock = FakeClock(NOW)
        self.mono = Mono(1000.0)
        patcher = mock.patch.object(auth, "_monotonic", lambda: self.mono.t)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, mode):
        cfg = auth.AuthConfig(mode=mode, registry_path=self.paths.registry,
                              state_dir=self.state_dir, per_key_max=600,
                              poll_interval=0.0, busy_timeout=0.1)
        runtime = auth.open_runtime(cfg, self.clock, owner_uid=0,
                                    registry_owner_uid=os.getuid())
        self.addCleanup(runtime.stop)
        return runtime

    def authenticate(self, runtime):
        headers = Headers(self.signer.headers(
            key_id="k-a", method="GET", target=TARGET, body=b"",
            timestamp=now_seconds(self.clock)))
        return runtime.gate.authenticate(method="GET", framing=_framing(),
                                         headers=headers, body=b"")

    def hold_write_lock(self):
        holder = sqlite3.connect(self.paths.replay_db, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: holder.execute("ROLLBACK") if holder.in_transaction else None)

    def scenario(self, mode):
        runtime = self.open(mode)
        # 接線：計數器注入 gate；watcher 的 extra tick 含寫檔；第一次 tick 就寫。
        self.assertIs(runtime.gate.busy_counter, runtime.busy_counter)
        self.assertIn(runtime._busy_tick, runtime.watcher.extra_ticks)
        runtime.watcher.tick()
        first = read_status(self.paths.busy_status)
        self.assertEqual((first["mode"], first["store_busy_total"]), (mode, 0))
        self.assertEqual(first["since"], now_seconds(self.clock))
        self.assertEqual(first["updated"], first["since"])
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.busy_status).st_mode), 0o644)

        self.hold_write_lock()
        for _ in range(2):
            verdict = self.authenticate(runtime)
            self.assertEqual(verdict.reason, auth.REASON_STORE_BUSY)
        self.mono.t = 3000.0
        runtime.watcher.tick()
        second = read_status(self.paths.busy_status)
        self.assertEqual((second["store_busy_total"], second["store_busy_30m"],
                          second["store_busy_60m"]), (2, 2, 2))

        self.assertEqual(self.authenticate(runtime).reason, auth.REASON_STORE_BUSY)
        # 時鐘前進 2000 秒（updated 與 since 不同）；單調時鐘在 3000 之後 1900 秒。
        self.clock.advance(seconds=2000)
        self.mono.t = 4900.0
        runtime.watcher.tick()
        third = read_status(self.paths.busy_status)
        self.assertEqual(third["mode"], mode)
        self.assertEqual(third["store_busy_total"], 3)
        self.assertEqual(third["store_busy_30m"], 1)   # 基準是 3000 秒那一筆（total 2）
        self.assertEqual(third["store_busy_60m"], 3)   # 基準是啟動樣本（total 0）
        self.assertEqual(third["since"], first["since"])
        self.assertEqual(third["updated"] - third["since"], 2000)

    def test_enforce_wiring_and_end_to_end(self):
        self.scenario("enforce")

    def test_warn_wiring_and_end_to_end(self):
        self.scenario("warn")


# ---------------------------------------------------------------------------
# C-1008：off 前後的 state dir 目錄清單（修訂 mid-2）
# ---------------------------------------------------------------------------

class OffStateDirTests(unittest.TestCase):
    def test_off_leaves_an_existing_state_dir_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_policy(root)
            (root / "keys.env").write_text(key_text(FAKE_KEYS))
            state = root / "state"
            state.mkdir(mode=0o700)
            (state / "sentinel").write_text("keep")
            before = sorted(os.listdir(state))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            cmd = [sys.executable, "-m", "lgwd", "--auth-mode", "off",
                   "--auth-state-dir", str(state),
                   "--auth-registry", str(root / "registry"),
                   "--auth-sshkeygen-path", str(root / "ssh-keygen"),
                   "--db", str(root / "app.sqlite"), "--keys", str(root / "keys.env"),
                   "--policy", str(root / "policy.json"), "--keysync-interval", "0",
                   "--api-port", str(port)]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                            self.assertEqual(response.status, 200)
                            break
                    except OSError:
                        self.assertIsNone(proc.poll())
                        if time.monotonic() > deadline:
                            self.fail("server did not start")
                        time.sleep(0.05)
                # 業務路由：off 下不驗章，任何 HTTP 回應都算走完一次請求。
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/subscriptions", timeout=2)
                except urllib.error.HTTPError:
                    pass
            finally:
                proc.terminate()
                proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(sorted(os.listdir(state)), before)
            self.assertFalse((state / ".auth-busy.status").exists())


# ---------------------------------------------------------------------------
# C-1009：check 的 auth-busy
# ---------------------------------------------------------------------------

class FakeLayout:
    euid = 0

    def __init__(self, mode):
        self._mode = mode

    def auth_mode(self):
        return self._mode


class CheckAuthBusyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, mode=0o700)
        self.paths = auth_paths(self.state_dir)
        self.now = int(time.time())
        patcher = mock.patch("lgwctl.auth_cli.paths_for", lambda layout: self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_busy(self, mode="enforce"):
        report = Report(out=lambda line: None)
        check._check_auth_busy(FakeLayout(mode), report)
        return report

    def write(self, **over):
        fields = {"updated": self.now, "since": self.now - 3600,
                  "store_busy_30m": 0, "store_busy_60m": 0}
        fields.update(over)
        document = _document(**fields)
        write_status(self.paths.busy_status, document)
        return document

    def level(self, **over):
        self.write(**over)
        return self.run_busy().level_of("auth-busy")

    def test_each_row_of_the_table(self):
        with self.subTest("off"):
            self.assertEqual(self.run_busy("off").level_of("auth-busy"), "ok")
        with self.subTest("unreadable mode"):
            self.assertEqual(self.run_busy("unreadable").level_of("auth-busy"), "todo")
        with self.subTest("unknown mode"):
            self.assertEqual(self.run_busy("bogus").level_of("auth-busy"), "todo")
        with self.subTest("paths error"):
            with mock.patch("lgwctl.auth_cli.paths_for", side_effect=ValueError("x")):
                self.assertEqual(self.run_busy().level_of("auth-busy"), "fail")
        with self.subTest("missing file"):
            self.assertEqual(self.run_busy().level_of("auth-busy"), "todo")
        with self.subTest("bad file"):
            with open(self.paths.busy_status, "w", encoding="utf-8") as fh:
                fh.write("garbage")
            self.assertEqual(self.run_busy().level_of("auth-busy"), "todo")
        with self.subTest("stale"):
            self.assertEqual(self.level(updated=self.now - 181), "todo")
        with self.subTest("updated in the future"):
            self.assertEqual(self.level(updated=self.now + 181), "todo")
        with self.subTest("enforce 30m=5"):
            self.assertEqual(self.level(store_busy_30m=5, store_busy_60m=5), "fail")
        with self.subTest("enforce 60m=1"):
            self.assertEqual(self.level(store_busy_30m=0, store_busy_60m=1), "todo")
        with self.subTest("warn 60m=1"):
            self.assertEqual(self.level(mode="warn", store_busy_60m=1), "todo")
        with self.subTest("warn never fails"):
            self.assertEqual(self.level(mode="warn", store_busy_30m=9, store_busy_60m=9), "todo")
        with self.subTest("all zero"):
            self.assertEqual(self.level(), "ok")

    def test_threshold_boundary_4_and_5(self):
        self.assertEqual(self.level(store_busy_30m=4, store_busy_60m=4), "todo")
        self.assertEqual(self.level(store_busy_30m=5, store_busy_60m=5), "fail")

    @unittest.skipIf(os.geteuid() == 0, "權限 000 的讀取測試需要非 root")
    def test_permission_000_is_todo(self):
        self.write()
        os.chmod(self.paths.busy_status, 0)
        self.addCleanup(os.chmod, self.paths.busy_status, 0o644)
        report = self.run_busy()
        self.assertEqual(report.level_of("auth-busy"), "todo")
        self.assertIn("unreadable", " ".join(report.lines))

    def test_directory_symlink_oversize_and_non_utf8_are_todo(self):
        with self.subTest("directory"):
            os.mkdir(self.paths.busy_status)
            report = self.run_busy()
            self.assertEqual(report.level_of("auth-busy"), "todo")
            self.assertIn("unreadable", " ".join(report.lines))
            os.rmdir(self.paths.busy_status)
        with self.subTest("symlink"):
            self.write()
            target = os.path.join(self._tmp.name, "real.status")
            os.rename(self.paths.busy_status, target)
            os.symlink(target, self.paths.busy_status)
            report = self.run_busy()
            self.assertEqual(report.level_of("auth-busy"), "todo")
            self.assertIn("unreadable", " ".join(report.lines))
            os.unlink(self.paths.busy_status)
            os.rename(target, self.paths.busy_status)
        with self.subTest("4097 bytes"):
            with open(self.paths.busy_status, "wb") as fh:
                fh.write(b" " * 4097)
            self.assertEqual(self.run_busy().level_of("auth-busy"), "todo")
        with self.subTest("non-utf8"):
            with open(self.paths.busy_status, "wb") as fh:
                fh.write(b"\xff\xfe\xfd")
            self.assertEqual(self.run_busy().level_of("auth-busy"), "todo")

    def test_unexpected_exception_is_todo_not_a_crash(self):
        with mock.patch("lgwd.auth_busy.read_status", side_effect=RuntimeError("bug")):
            report = self.run_busy()
        self.assertEqual(report.level_of("auth-busy"), "todo")
        self.assertIn("RuntimeError", " ".join(report.lines))

    def test_detail_has_only_counts_windows_and_age(self):
        self.write(store_busy_30m=2, store_busy_60m=3)
        report = self.run_busy()
        line = " ".join(report.lines)
        self.assertIn("近 30 分鐘 2 次", line)
        self.assertIn("近 60 分鐘 3 次", line)
        self.assertNotIn(self.state_dir, line)


# ---------------------------------------------------------------------------
# C-1008：off 零影響
# ---------------------------------------------------------------------------

class OffZeroEffectTests(unittest.TestCase):
    def test_off_check_neither_creates_nor_reads_the_busy_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            os.makedirs(state_dir, mode=0o700)
            paths = auth_paths(state_dir)
            before = sorted(os.listdir(state_dir))
            with mock.patch("lgwctl.auth_cli.paths_for", lambda layout: paths), \
                    mock.patch("lgwd.auth_busy.read_status") as reader, \
                    mock.patch("lgwd.auth_busy.write_status") as writer:
                report = Report(out=lambda line: None)
                check._check_auth_busy(FakeLayout("off"), report)
            self.assertEqual(report.level_of("auth-busy"), "ok")
            reader.assert_not_called()
            writer.assert_not_called()
            self.assertEqual(sorted(os.listdir(state_dir)), before)
            self.assertFalse(os.path.exists(paths.busy_status))

    def test_off_runtime_is_never_opened_by_main(self):
        # main 只在 mode != off 時開 runtime（見 lgwd/main.py），所以 off 不建計數器與取樣器。
        source = (REPO_ROOT / "lgwd" / "main.py").read_text(encoding="utf-8")
        self.assertIn('if config.auth.mode != "off":\n            runtime = open_runtime(', source)


# ---------------------------------------------------------------------------
# C-1010、C-1011：文件（白名單一句、deploy 章節）
# ---------------------------------------------------------------------------

class DocTests(unittest.TestCase):
    def test_design_whitelist_has_store_busy_sentence(self):
        text = (REPO_ROOT / "openspec" / "changes" / "lgw-resolve-auth" / "design.md").read_text(
            encoding="utf-8")
        self.assertIn("`store_busy`、`store_unavailable`", text)
        self.assertIn("`store_busy`：防重放庫忙碌到期限（行程內鎖或 SQLite 忙碌，C-304 的 StoreBusy）；"
                      "回應與 `store_unavailable` 逐 byte 相同（503 `auth_unavailable`），"
                      "只在日誌與 `.auth-busy.status` 分開計數。", text)

    def test_deploy_doc_mentions_busy_status_and_auth_busy(self):
        text = (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
        self.assertIn(".auth-busy.status", text)
        self.assertIn("auth-busy", text)
        self.assertIn("### 8.22", text)


if __name__ == "__main__":
    unittest.main()
