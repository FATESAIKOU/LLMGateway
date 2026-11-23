"""lgwd/auth_reload.py 的單元測試（D2 PR-4，契約 C-401、C-403～C-406）。

全部用暫存目錄＋暫存 SQLite、`FakeClock`（不真的 sleep 等 2 秒輪詢）與假名冊；只有
「閒置也載入」那一支用真的背景執行緒與短輪詢間隔（契約明列的 4 秒內載入）。不碰
`/var/lib/llmgateway`、`/etc/llmgateway`、不讀憑證、不連真機。

測試名都帶 C 編號，報告的「C 編號對測試名稱」表直接由這裡對出來。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lgwd import auth_reload
from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths, now_seconds
from lgwd.auth_registry import (
    Registry,
    RegistryError,
    content_hash,
    parse_registry,
)
from lgwd.auth_reload import (
    COMMIT,
    COMMIT_FIRST,
    NO_REGISTRY,
    REJECT,
    RESULTS,
    UNCHANGED,
    FileResult,
    RegistryWatcher,
    Snapshot,
    decide,
)
from lgwd.auth_trust import EMPTY_TRUST_STATE, TrustState, TrustStore
from lgwd.clock import FakeClock
from tests.auth_fakes import (
    VALID_B,
    key,
    registry_bytes,
    write_registry_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- decide 的單元測試（不碰檔案） ---

def _valid_file(raw: bytes) -> FileResult:
    return FileResult(raw=raw, registry=parse_registry(raw), content_hash=content_hash(raw), error=None)


def _trust(generation=None, raw=None, digest=None, tombstones=()) -> TrustState:
    return TrustState(
        generation=generation,
        snapshot_raw=raw,
        snapshot_hash=digest,
        tombstones=tuple(tombstones),
    )


def _keys_with_revoked() -> list:
    return [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")]


class DecideTests(unittest.TestCase):
    """C-401：唯一決策函式的每一個分支（啟動與執行中都只走這一個）。"""

    def setUp(self) -> None:
        self.raw = registry_bytes(2, keys=_keys_with_revoked())
        self.digest = content_hash(self.raw)
        self.file = _valid_file(self.raw)

    def test_c401_commit_first_when_trust_has_no_snapshot(self) -> None:
        decision = decide(self.file, EMPTY_TRUST_STATE)
        self.assertEqual(decision.action, COMMIT_FIRST)
        self.assertEqual(decision.result, "ok")
        self.assertEqual(decision.content_hash, self.digest)
        self.assertEqual(decision.generation, 2)

    def test_c401_commit_first_still_checks_tombstones(self) -> None:
        """沒有快照但信任庫已有墓碑時仍驗墓碑（rebuild-trust 的半套狀態不能讓撤銷失效）。"""
        tombstone = (("w1-tunnel-old", VALID_B),)
        dropped = registry_bytes(2, keys=[key("w1-tunnel-a")])
        decision = decide(_valid_file(dropped), _trust(tombstones=tombstone))
        self.assertEqual(decision.action, REJECT)
        self.assertEqual(decision.result, "tombstone_violation")

    def test_c401_unchanged_for_same_generation_and_hash(self) -> None:
        decision = decide(self.file, _trust(2, self.raw, self.digest))
        self.assertEqual(decision.action, UNCHANGED)
        self.assertEqual(decision.result, "ok")

    def test_c401_same_generation_different_hash_is_rejected(self) -> None:
        other = registry_bytes(2, keys=_keys_with_revoked()[:-1] + [
            key("w1-tunnel-old", raw=VALID_B, status="revoked"),
        ]) + b"\n"   # 只差一個換行，內容雜湊就不同
        decision = decide(_valid_file(other), _trust(2, self.raw, self.digest))
        self.assertEqual(decision.action, REJECT)
        self.assertEqual(decision.result, "generation_not_increased")
        self.assertEqual(decision.content_hash, content_hash(other))

    def test_c401_lower_generation_is_rejected(self) -> None:
        lower = registry_bytes(1, keys=_keys_with_revoked())
        decision = decide(_valid_file(lower), _trust(2, self.raw, self.digest))
        self.assertEqual(decision.action, REJECT)
        self.assertEqual(decision.result, "generation_not_increased")

    def test_c401_higher_generation_commits(self) -> None:
        higher = registry_bytes(3, keys=_keys_with_revoked())
        decision = decide(_valid_file(higher), _trust(2, self.raw, self.digest))
        self.assertEqual(decision.action, COMMIT)
        self.assertEqual(decision.result, "ok")
        self.assertEqual(decision.generation, 3)

    def test_c401_higher_generation_dropping_tombstone_is_rejected(self) -> None:
        snapshot = _trust(2, self.raw, self.digest, (("w1-tunnel-old", VALID_B),))
        variants = {
            "deleted": [key("w1-tunnel-a")],
            "active_again": [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B)],
            "reused_by_new_id": [key("w1-tunnel-a"), key("w1-tunnel-new", raw=VALID_B)],
        }
        for name, keys in variants.items():
            with self.subTest(variant=name):
                higher = registry_bytes(3, keys=keys)
                decision = decide(_valid_file(higher), snapshot)
                self.assertEqual(decision.action, REJECT)
                self.assertEqual(decision.result, "tombstone_violation")

    def test_c401_invalid_file_is_rejected_and_snapshot_kept(self) -> None:
        for kind in ("parse_error", "invalid_key", "duplicate", "too_large",
                     "insecure_file", "io_error"):
            with self.subTest(kind=kind):
                file_result = FileResult(raw=b"{bad", registry=None,
                                         content_hash=content_hash(b"{bad"), error=kind)
                decision = decide(file_result, _trust(2, self.raw, self.digest))
                self.assertEqual(decision.action, REJECT)
                self.assertEqual(decision.result, kind)

    def test_c401_invalid_file_without_snapshot_is_no_registry(self) -> None:
        file_result = FileResult(raw=b"{bad", registry=None,
                                 content_hash=content_hash(b"{bad"), error="parse_error")
        decision = decide(file_result, EMPTY_TRUST_STATE)
        self.assertEqual(decision.action, NO_REGISTRY)
        self.assertEqual(decision.result, "parse_error")

    def test_c401_decide_rejects_wrong_arguments(self) -> None:
        with self.assertRaises(TypeError):
            decide(self.file, None)
        with self.assertRaises(TypeError):
            decide(b"raw", EMPTY_TRUST_STATE)

    def test_c405_result_whitelist_is_fixed(self) -> None:
        self.assertEqual(
            RESULTS,
            frozenset({
                "ok", "parse_error", "invalid_key", "duplicate", "insecure_file",
                "too_large", "tombstone_violation", "generation_not_increased",
                "io_error", "trust_unavailable",
            }),
        )

    def test_c405_file_result_rejects_unknown_classification(self) -> None:
        with self.assertRaises(ValueError):
            FileResult(raw=b"x", registry=None, content_hash="sha256:00", error="ok")
        with self.assertRaises(ValueError):
            FileResult(raw=b"x", registry=None, content_hash="sha256:00", error="made_up")
        with self.assertRaises(ValueError):
            FileResult(raw=b"x", registry=None, content_hash=None, error=None)


# --- Watcher 的整合測試（真的暫存檔、真的信任庫） ---

class WatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir)
        self.paths = auth_paths(self.state_dir, registry=os.path.join(self.dir, "auth-registry.json"))
        create_stores(self.paths.trust_db, self.paths.replay_db)
        self.clock = FakeClock()
        self.trust = TrustStore(self.paths.trust_db)
        self.addCleanup(self.trust.close)
        self.owner_uid = os.getuid()

    # --- 工具 ---

    def write(self, data: bytes) -> None:
        write_registry_file(self.paths.registry, data)

    def watcher(self, **kwargs) -> RegistryWatcher:
        kwargs.setdefault("owner_uid", self.owner_uid)
        kwargs.setdefault("poll_interval", 0)   # 單元測試不開背景執行緒
        watcher = RegistryWatcher(self.paths, self.trust, self.clock, **kwargs)
        self.addCleanup(watcher.stop)
        return watcher

    def registry(self, generation: int = 1, *, old_status: str = "revoked") -> bytes:
        keys = [key("w1-tunnel-a")]
        if old_status is not None:
            keys.append(key("w1-tunnel-old", raw=VALID_B, status=old_status))
        return registry_bytes(generation, keys=keys)

    def status(self) -> dict:
        with open(self.paths.status, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.endswith("\n"))
        return json.loads(text)


class InitialLoadTests(WatcherTestCase):
    def test_c401_initial_load_commits_first_snapshot(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        decision = watcher.load_initial()
        self.assertEqual(decision.action, COMMIT_FIRST)
        self.assertEqual(watcher.snapshot.generation, 1)
        self.assertEqual(self.trust.state().snapshot_hash, content_hash(self.registry(1)))
        document = self.status()
        self.assertEqual(document["loaded_generation"], 1)
        self.assertEqual(document["loaded_hash"], content_hash(self.registry(1)))
        self.assertEqual(document["attempted_hash"], document["loaded_hash"])
        self.assertEqual(document["result"], "ok")

    def test_c401_load_initial_and_tick_both_call_decide(self) -> None:
        """啟動與執行中走同一個函式：兩個入口都必須呼叫模組層的 `decide`。"""
        calls = []
        real = auth_reload.decide

        def wrapper(file_result, trust_state):
            calls.append(file_result.error)
            return real(file_result, trust_state)

        self.write(self.registry(1))
        with mock.patch.object(auth_reload, "decide", wrapper):
            watcher = self.watcher()
            watcher.load_initial()
            self.assertEqual(len(calls), 1)
            self.write(self.registry(2))
            watcher.tick()
        self.assertEqual(len(calls), 2, msg="tick 必須與 load_initial 共用 decide")

    def test_c403_missing_registry_file_is_io_error(self) -> None:
        watcher = self.watcher()
        decision = watcher.load_initial()
        self.assertEqual(decision.result, "io_error")
        self.assertIsNone(watcher.snapshot)
        self.assertEqual(self.status()["result"], "io_error")
        # 檔案之後出現（不同 stamp）就立刻載入：io_error 是暫時性錯誤，但新 stamp 不必等退避。
        self.write(self.registry(1))
        watcher.tick()
        self.assertEqual(watcher.snapshot.generation, 1)

    def test_c401_no_registry_when_file_and_snapshot_are_both_invalid(self) -> None:
        self.write(b"this is not a registry")
        watcher = self.watcher()
        decision = watcher.load_initial()
        self.assertEqual(decision.action, NO_REGISTRY)
        self.assertEqual(decision.result, "parse_error")
        self.assertIsNone(watcher.snapshot)
        document = self.status()
        self.assertEqual(document["result"], "parse_error")
        self.assertIsNone(document["loaded_generation"])
        self.assertEqual(document["attempted_hash"], content_hash(b"this is not a registry"))

    def test_c207_insecure_file_is_reported_without_loading(self) -> None:
        self.write(self.registry(1))
        os.chmod(self.paths.registry, 0o666)
        watcher = self.watcher()
        decision = watcher.load_initial()
        self.assertEqual(decision.result, "insecure_file")
        self.assertIsNone(watcher.snapshot)
        self.assertEqual(self.status()["result"], "insecure_file")


class PollingTests(WatcherTestCase):
    def test_c403_idle_service_loads_new_generation_within_four_seconds(self) -> None:
        """C-403：沒有任何請求時安裝新世代，4 秒內載入並寫 status。"""
        self.write(self.registry(1))
        watcher = self.watcher(poll_interval=0.05)
        watcher.load_initial()
        self.assertEqual(self.status()["loaded_generation"], 1)
        started = time.monotonic()
        watcher.start()
        self.addCleanup(watcher.stop)
        self.write(self.registry(2))
        deadline = started + 4.0
        while time.monotonic() < deadline:
            if self.status()["loaded_generation"] == 2:
                break
            time.sleep(0.02)
        watcher.stop()
        document = self.status()
        self.assertEqual(document["loaded_generation"], 2)
        self.assertEqual(document["result"], "ok")
        self.assertLess(time.monotonic() - started, 4.0)

    def test_c403_request_path_never_reads_or_writes(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        reads, writes = watcher.reads, watcher.status_writes
        for _ in range(200):
            self.assertIsNotNone(watcher.snapshot)
            self.assertEqual(watcher.snapshot.generation, 1)
        self.assertEqual(watcher.reads, reads, msg="請求路徑不得觸發讀檔")
        self.assertEqual(watcher.status_writes, writes, msg="請求路徑不得寫 status")

    def test_c403_same_bad_stamp_is_not_reread(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        self.write(b"{ not json")
        watcher.tick()
        self.assertEqual(watcher.reads, 2)
        self.assertEqual(self.status()["result"], "parse_error")
        for _ in range(3):
            self.assertIsNone(watcher.tick())
        self.clock.advance(seconds=600)
        self.assertIsNone(watcher.tick())
        self.assertEqual(watcher.reads, 2, msg="同一個壞 stamp 不得重讀（非暫時性錯誤）")
        # 檔案再變（例如回滾成好的名冊）才重讀。
        self.write(self.registry(1))
        watcher.tick()
        self.assertEqual(watcher.reads, 3)
        self.assertEqual(self.status()["result"], "ok")

    def test_c403_transient_errors_back_off_five_to_three_hundred(self) -> None:
        self.write(self.registry(1))
        calls = []

        def reader(path, owner_uid):
            calls.append(path)
            raise RegistryError("io_error")

        watcher = self.watcher(read_file=reader)
        watcher.load_initial()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.status()["result"], "io_error")
        expected = 1
        for step in (5, 10, 20, 40, 80, 160, 300, 300):
            self.clock.advance(seconds=step - 1)
            self.assertIsNone(watcher.tick(), msg="退避 {0} 秒內不得重試".format(step))
            self.assertEqual(len(calls), expected)
            self.clock.advance(seconds=1)
            watcher.tick()
            expected += 1
            self.assertEqual(len(calls), expected, msg="退避 {0} 秒後要重試同一個 stamp".format(step))

    def test_c403_extra_ticks_run_on_every_tick(self) -> None:
        calls = []
        self.write(self.registry(1))
        watcher = self.watcher(extra_ticks=[lambda: calls.append(1)])
        watcher.load_initial()
        watcher.tick()
        watcher.tick()
        self.assertEqual(len(calls), 2)

    def test_c403_extra_tick_failure_does_not_break_polling(self) -> None:
        def boom():
            raise RuntimeError("extra tick failed")

        self.write(self.registry(1))
        watcher = self.watcher(extra_ticks=[boom])
        self.assertEqual(watcher.load_initial().result, "ok")
        self.assertIsNone(watcher.tick())
        self.assertEqual(self.status()["result"], "ok")


class StatusFileTests(WatcherTestCase):
    def test_c404_status_format_permissions_and_no_secrets(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        with open(self.paths.status, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(len(text.strip().splitlines()), 1, msg="必須是一行 JSON")
        document = json.loads(text)
        self.assertEqual(
            set(document),
            {"schema", "loaded_generation", "loaded_hash", "attempted_hash",
             "result", "heartbeat"},
        )
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["heartbeat"], now_seconds(self.clock))
        mode = stat.S_IMODE(os.stat(self.paths.status).st_mode)
        self.assertEqual(mode, 0o644)
        # 不含公鑰與 key ID（C-404）。
        for secret in ("ssh-ed25519", "w1-tunnel-a", "w1-tunnel-old", "AAAAC3NzaC1lZDI1NTE5"):
            self.assertNotIn(secret, text)
        leftovers = [name for name in os.listdir(self.state_dir)
                     if name.startswith(".auth-registry.status.")]
        self.assertEqual(leftovers, [], msg="原子寫不得留暫存檔")

    def test_c404_unchanged_does_not_rewrite_status(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        writes = watcher.status_writes
        self.write(self.registry(1))          # 同世代同內容、新的 inode／mtime
        decision = watcher.tick()
        self.assertEqual(decision.action, UNCHANGED)
        self.assertEqual(watcher.status_writes, writes)

    def test_c404_restoring_the_loaded_registry_sets_ok_again(self) -> None:
        good = self.registry(1)
        self.write(good)
        watcher = self.watcher()
        watcher.load_initial()
        loaded = self.status()
        bad = b"{ not json"
        self.write(bad)
        watcher.tick()
        broken = self.status()
        self.assertEqual(broken["result"], "parse_error")
        self.assertEqual(broken["attempted_hash"], content_hash(bad))
        self.assertEqual(broken["loaded_hash"], loaded["loaded_hash"])
        self.assertEqual(broken["loaded_generation"], loaded["loaded_generation"])
        self.write(good)
        decision = watcher.tick()
        self.assertEqual(decision.action, UNCHANGED)
        restored = self.status()
        self.assertEqual(restored["result"], "ok", msg="UNCHANGED 而目前是錯誤時要改回 ok")
        self.assertEqual(restored["attempted_hash"], loaded["loaded_hash"])

    def test_c404_heartbeat_at_most_every_sixty_seconds(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        writes = watcher.status_writes
        self.clock.advance(seconds=30)
        watcher.tick()
        self.assertEqual(watcher.status_writes, writes)
        self.clock.advance(seconds=31)
        watcher.tick()
        self.assertEqual(watcher.status_writes, writes + 1)
        self.assertEqual(self.status()["heartbeat"], now_seconds(self.clock))
        self.clock.advance(seconds=59)
        watcher.tick()
        self.assertEqual(watcher.status_writes, writes + 1)

    def test_c404_heartbeat_stops_when_the_polling_thread_stops(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher(poll_interval=0.02)
        watcher.load_initial()
        watcher.start()
        deadline = time.monotonic() + 2.0
        while not watcher.running and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        watcher.stop()
        with open(self.paths.status, "rb") as fh:
            before = fh.read()
        writes = watcher.status_writes
        self.clock.advance(minutes=10)
        time.sleep(0.2)
        with open(self.paths.status, "rb") as fh:
            after = fh.read()
        self.assertEqual(after, before, msg="輪詢停止後心跳不得再前進")
        self.assertEqual(watcher.status_writes, writes)

    def test_c404_generation_rejection_records_attempted_hash(self) -> None:
        self.write(self.registry(2))
        watcher = self.watcher()
        watcher.load_initial()
        loaded = self.status()
        lower = self.registry(1)
        self.write(lower)
        decision = watcher.tick()
        self.assertEqual(decision.result, "generation_not_increased")
        document = self.status()
        self.assertEqual(document["result"], "generation_not_increased")
        self.assertEqual(document["attempted_hash"], content_hash(lower))
        self.assertEqual(document["loaded_generation"], loaded["loaded_generation"])
        self.assertEqual(document["loaded_hash"], loaded["loaded_hash"])

    def test_c404_heartbeat_advances_while_the_registry_stays_broken(self) -> None:
        """中-3：壞檔留著不動時 tick 照跑，心跳每 60 秒前進、`result` 維持 parse_error。

        心跳只代表背景執行緒還活著，與結果無關；否則壞名冊留著沒修時，D5 看到的是
        「執行緒死了」而不是 parse_error。
        """
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        broken = b"{ not json"
        self.write(broken)
        self.assertEqual(watcher.tick().result, "parse_error")
        first = self.status()
        self.assertEqual(first["result"], "parse_error")
        heartbeats = []
        for _ in range(40):                      # 200 秒，每 5 秒 tick 一次
            self.clock.advance(seconds=5)
            self.assertIsNone(watcher.tick(), msg="同一個壞 stamp 不得重讀")
            document = self.status()
            self.assertEqual(document["result"], "parse_error", msg="心跳不得改結果")
            self.assertEqual(
                document["attempted_hash"], content_hash(broken), msg="心跳不得改其他欄位"
            )
            if not heartbeats or document["heartbeat"] != heartbeats[-1]:
                heartbeats.append(document["heartbeat"])
        # 第一筆是 parse_error 那次寫入（t+5），之後每 60 秒一次：t+65、t+125、t+185。
        self.assertEqual(
            heartbeats, [first["heartbeat"] + 60 * step for step in range(4)]
        )

    def test_c404_heartbeat_advances_during_transient_backoff(self) -> None:
        """中-3：暫時性錯誤的退避等待中（不重讀、不改結果）心跳仍前進。"""
        def reader(path, owner_uid):
            raise RegistryError("io_error")

        self.write(self.registry(1))
        watcher = self.watcher(read_file=reader)
        self.assertEqual(watcher.load_initial().result, "io_error")
        start = self.status()["heartbeat"]
        for _ in range(12):                      # 60 秒，每 5 秒 tick 一次
            self.clock.advance(seconds=5)
            watcher.tick()
        document = self.status()
        self.assertEqual(document["result"], "io_error")
        self.assertEqual(document["attempted_hash"], None)
        self.assertGreaterEqual(document["heartbeat"] - start, 60)


class RestartTests(WatcherTestCase):
    def test_c403_restart_with_invalid_file_uses_the_trust_snapshot(self) -> None:
        good = self.registry(1)
        self.write(good)
        self.watcher().load_initial()
        self.write(b"\x00 not a registry")
        adopted = []
        watcher = self.watcher(on_snapshot=adopted.append)
        decision = watcher.load_initial()
        self.assertEqual(decision.result, "parse_error")
        self.assertIsNotNone(watcher.snapshot)
        self.assertEqual(watcher.snapshot.generation, 1)
        self.assertEqual(watcher.snapshot.raw, good)
        self.assertEqual([registry.generation for registry in adopted], [1])
        self.assertIsInstance(adopted[0], Registry)
        document = self.status()
        self.assertEqual(document["result"], "parse_error")
        self.assertEqual(document["loaded_hash"], content_hash(good))

    def test_c401_restart_rejects_same_generation_tampered_file(self) -> None:
        good = self.registry(1)
        self.write(good)
        running = self.watcher()
        running.load_initial()
        tampered = self.registry(1, old_status="active")
        self.write(tampered)
        decision = running.tick()
        self.assertEqual(decision.action, REJECT)
        self.assertEqual(decision.result, "generation_not_increased")
        self.assertEqual(running.snapshot.registry.key_by_id("w1-tunnel-old").status, "revoked")
        # 重啟：同一套判斷，仍拒絕並沿用信任庫快照。
        restart = self.watcher()
        restart_decision = restart.load_initial()
        self.assertEqual(restart_decision.result, "generation_not_increased")
        self.assertEqual(restart.snapshot.generation, 1)
        self.assertEqual(restart.snapshot.registry.key_by_id("w1-tunnel-old").status, "revoked")
        self.assertEqual(self.trust.state().snapshot_hash, content_hash(good))
        self.assertEqual(self.status()["result"], "generation_not_increased")


    def test_c401_unchanged_adopts_the_trust_snapshot_in_memory(self) -> None:
        """信任庫已有快照、但這個 watcher 還沒有記憶體快照時，UNCHANGED 要補上並回呼。

        這是「上一次提交成功、讀回狀態卻失敗」那個窗口的修復路徑（C-401／C-406）。
        """
        self.write(self.registry(1))
        self.watcher().load_initial()
        seen = []
        fresh = self.watcher(on_snapshot=seen.append)
        self.assertIsNone(fresh.snapshot)
        decision = fresh.tick()
        self.assertEqual(decision.action, UNCHANGED)
        self.assertEqual(fresh.snapshot.generation, 1)
        self.assertEqual(fresh.snapshot.content_hash, content_hash(self.registry(1)))
        self.assertEqual([registry.generation for registry in seen], [1])


class TombstoneTests(WatcherTestCase):
    def test_c402_higher_generation_cannot_revive_a_key(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))
        variants = {
            "deleted": registry_bytes(2, keys=[key("w1-tunnel-a")]),
            "active_again": self.registry(2, old_status="active"),
            "reused_by_new_id": registry_bytes(
                2, keys=[key("w1-tunnel-a"), key("w1-tunnel-new", raw=VALID_B)]
            ),
        }
        for name, data in variants.items():
            with self.subTest(variant=name):
                self.write(data)
                decision = watcher.tick()
                self.assertEqual(decision.result, "tombstone_violation")
                self.assertEqual(watcher.snapshot.generation, 1)
                self.assertEqual(self.trust.state().generation, 1)
                self.assertEqual(self.status()["result"], "tombstone_violation")
                self.assertEqual(self.status()["attempted_hash"], content_hash(data))
        self.assertEqual(watcher.snapshot.registry.key_by_id("w1-tunnel-old").status, "revoked")
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))
        # 合法的新世代照常提交，墓碑只增。
        self.write(self.registry(2))
        decision = watcher.tick()
        self.assertEqual(decision.action, COMMIT)
        self.assertEqual(watcher.snapshot.generation, 2)
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))

    def test_c402_tombstones_are_independent_of_the_replay_store(self) -> None:
        """信任庫與防重放庫分開：replay 庫整個不見，墓碑與防回退都不受影響。"""
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.paths.replay_db + suffix)
            except FileNotFoundError:
                pass
        self.write(registry_bytes(2, keys=[key("w1-tunnel-a")]))
        self.assertEqual(watcher.tick().result, "tombstone_violation")
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))
        self.assertEqual(watcher.snapshot.generation, 1)


class SnapshotTests(WatcherTestCase):
    def test_c406_inflight_snapshot_stays_stable(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        held = watcher.snapshot
        self.assertIsInstance(held, Snapshot)
        self.write(self.registry(2))
        watcher.tick()
        self.assertEqual(held.generation, 1)
        self.assertEqual(held.registry.generation, 1)
        self.assertEqual(watcher.snapshot.generation, 2)
        self.assertIsNot(held, watcher.snapshot)
        self.assertEqual(
            held.active_keys(), tuple(k for k in held.registry.keys if k.active)
        )
        self.assertNotIn("ssh-ed25519", repr(held), msg="快照的 repr 不含公鑰")

    def test_c402_on_snapshot_runs_after_the_transaction_committed(self) -> None:
        seen = []

        def on_snapshot(registry):
            seen.append((
                registry.generation,
                self.trust.state().snapshot_hash,
                watcher.snapshot.content_hash,
            ))

        self.write(self.registry(1))
        watcher = self.watcher(on_snapshot=on_snapshot)
        watcher.load_initial()
        self.write(self.registry(2))
        decision = watcher.tick()
        self.assertEqual(decision.action, COMMIT)
        generation, committed, in_memory = seen[-1]
        self.assertEqual(generation, 2)
        self.assertEqual(committed, in_memory, msg="交易提交後才換記憶體快照再回呼")
        self.assertEqual(in_memory, content_hash(self.registry(2)))

    def test_c402_status_is_written_after_on_snapshot_returns(self) -> None:
        """中-2：換世代時回呼內讀到的 status 仍是舊世代，回呼結束後才是新世代。

        D5 的 install 要在 `loaded_hash` 換成新世代、`result` 為 ok 時才回 0；status 若在
        回呼（PR-6 換 allowed_signers）之前就寫成新世代，撤銷會在真正生效前被宣告完成。
        """
        self.write(self.registry(1))
        during = []

        def on_snapshot(registry):
            if os.path.exists(self.paths.status):
                during.append((registry.generation, self.status()["loaded_generation"]))

        watcher = self.watcher(on_snapshot=on_snapshot)
        watcher.load_initial()          # 第一份：回呼時還沒有 status 檔，略過
        self.assertEqual(during, [])
        self.write(self.registry(2))
        self.assertEqual(watcher.tick().action, COMMIT)
        self.assertEqual(during, [(2, 1)], msg="回呼進行中 status 必須還是舊世代")
        self.assertEqual(self.status()["loaded_generation"], 2)

    def test_c402_unchanged_repair_writes_status_after_on_snapshot_returns(self) -> None:
        """中-2：UNCHANGED 的 `repaired` 路徑同樣先回呼、後寫 status。"""
        self.write(self.registry(1))
        self.watcher().load_initial()   # 信任庫與 status 都是第 1 代
        os.unlink(self.paths.status)    # 新行程還沒寫過 status（模擬 status 遺失）
        during = []

        def on_snapshot(registry):
            during.append(os.path.exists(self.paths.status))

        fresh = self.watcher(on_snapshot=on_snapshot)
        decision = fresh.tick()
        self.assertEqual(decision.action, UNCHANGED)
        self.assertEqual(during, [False], msg="回呼時 status 還沒寫（修補也要等回呼完成）")
        self.assertEqual(self.status()["loaded_generation"], 1)

    def test_c402_failed_callback_still_records_ok(self) -> None:
        """回呼失敗不改已提交的信任庫與記憶體快照；status 照樣 ok（名冊確實已載入）。"""
        def boom(registry):
            raise RuntimeError("boom")

        self.write(self.registry(1))
        watcher = self.watcher(on_snapshot=boom)
        decision = watcher.load_initial()
        self.assertEqual(decision.result, "ok")
        self.assertIsNotNone(watcher.snapshot)
        self.assertEqual(watcher.snapshot.generation, 1)
        self.assertEqual(self.trust.state().generation, 1)
        document = self.status()
        self.assertEqual(document["result"], "ok")
        self.assertEqual(document["loaded_generation"], 1)

    def test_c402_on_snapshot_is_not_called_when_the_load_is_rejected(self) -> None:
        seen = []
        self.write(self.registry(1))
        watcher = self.watcher(on_snapshot=seen.append)
        watcher.load_initial()
        before = len(seen)
        self.write(registry_bytes(2, keys=[key("w1-tunnel-a")]))
        self.assertEqual(watcher.tick().result, "tombstone_violation")
        self.assertEqual(len(seen), before)

    def test_c402_memory_snapshot_is_not_swapped_when_the_commit_fails(self) -> None:
        self.write(self.registry(1))
        watcher = self.watcher()
        watcher.load_initial()
        held = watcher.snapshot
        self.trust.db.conn.execute(
            "CREATE TRIGGER refuse_snapshot BEFORE INSERT ON snapshot"
            " BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        self.write(self.registry(2))
        decision = watcher.tick()
        self.assertEqual(decision.result, "trust_unavailable")
        self.assertIs(watcher.snapshot, held, msg="交易失敗不得先換記憶體快照")
        self.assertEqual(self.trust.state().generation, 1)
        self.assertEqual(self.status()["result"], "trust_unavailable")


class ModuleBoundaryTests(unittest.TestCase):
    def test_c403_contract_constants(self) -> None:
        """契約寫死的間隔與退避（2 秒輪詢、60 秒心跳、5→300 秒退避）。"""
        self.assertEqual(auth_reload.POLL_INTERVAL, 2.0)
        self.assertEqual(auth_reload.HEARTBEAT_SECONDS, 60)
        self.assertEqual(auth_reload.BACKOFF_INITIAL, 5.0)
        self.assertEqual(auth_reload.BACKOFF_MAX, 300.0)

    def test_auth_modules_do_not_import_forbidden_modules(self) -> None:
        for name in ("auth_trust.py", "auth_reload.py"):
            source = (REPO_ROOT / "lgwd" / name).read_text(encoding="utf-8")
            for forbidden in ("lgwd.http", "lgwd.main", "lgwd.registry", "lgwd.sshsig"):
                self.assertNotIn("import " + forbidden, source, msg=name)
        reload_source = (REPO_ROOT / "lgwd" / "auth_reload.py").read_text(encoding="utf-8")
        self.assertNotIn("import sqlite3", reload_source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
