"""lgwd/auth_admin.py 的單元測試（D5a；契約 C-801～C-809、W-D5～W-D12）。

全部用暫存目錄與暫存 SQLite、`FakeClock` 與假名冊；不碰 `/var/lib/llmgateway`、
`/etc/llmgateway`，不讀憑證、不連真機。故障注入用真實觸發（改壞檔頭、另一把 flock、
`mock.patch.object` 模擬 transaction 前當機），不只餵假字串。

測試名都帶 C 編號；報告的「C 編號對測試名稱」表直接由這裡對出來。W-D1～W-D4
（install 的四種結局）與 HTTP 層的 401 屬 D5b／D5c，這裡不冒充已驗。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lgwd import auth_admin, auth_db
from lgwd.auth_admin import (
    KIND_AUDIT_FAILED,
    KIND_KEY_NOT_FOUND,
    KIND_LOCK_BUSY,
    KIND_OWNER_FAILED,
    KIND_REPLAY_HEALTHY,
    KIND_STORE_EXISTS,
    KIND_STORE_MISSING,
    KIND_STORE_UNAVAILABLE,
    KIND_TOMBSTONE_VIOLATION,
    KIND_TRUST_GENERATION_DOWNGRADE,
    KIND_TRUST_HEALTHY,
    REGISTRY_MODE,
    RESET_WATER_COST,
    ROLLBACK_PROTECTION_LOST,
    ROLLBACK_PROTECTION_LOST_KEY,
    AdminError,
    AuditLog,
    AuditValueError,
    init_stores,
    inspect_replay,
    inspect_stores,
    rebuild_trust,
    recover_replay,
    reset_generation,
    reset_key_water,
    store_write_lock,
)
from lgwd.auth_db import (
    IDENTITY_BYTES,
    IDENTITY_KEY,
    META_SCHEMA_VERSION,
    SCHEMA_KEY,
    LockBusy,
    StoreExists,
    StoreUnavailable,
    acquire_store_lock,
    check_store_identity,
    create_stores,
    open_rw,
)
from lgwd.auth_paths import auth_paths, sqlite_sidecars
from lgwd.auth_registry import content_hash, parse_registry
from lgwd.auth_reload import (
    REJECT,
    RESULT_GENERATION_NOT_INCREASED,
    RESULT_TOMBSTONE_VIOLATION,
    FileResult,
    decide,
)
from lgwd.auth_trust import TrustReject, TrustStore
from lgwd.clock import FakeClock
from tests.auth_fakes import (
    VALID_A,
    VALID_B,
    VALID_C,
    key,
    public_key_line,
    registry_bytes,
    write_registry_file,
)

REPO = Path(__file__).resolve().parent.parent


def _hash(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _hashes(*paths: str) -> dict[str, str | None]:
    return {path: (_hash(path) if os.path.exists(path) else None) for path in paths}


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _epoch(clock: FakeClock) -> int:
    return int(clock.now().timestamp())


class AdminTestCase(unittest.TestCase):
    """暫存 state dir ＋ etc 目錄；每個測試自己決定要不要 `init()`。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir)
        self.etc = os.path.join(self.dir, "etc")
        os.makedirs(self.etc)
        self.paths = auth_paths(
            self.state_dir, registry=os.path.join(self.etc, "auth-registry.json")
        )
        self.clock = FakeClock()
        self.audit = AuditLog(
            self.paths.audit_log, clock=self.clock, uid=os.geteuid(), sudo_user=None
        )
        self.owner = (os.geteuid(), os.getegid())
        self.owner_uid = os.getuid()

    # --- 工具 ---

    def init(self):
        return init_stores(
            self.paths,
            clock=self.clock,
            audit=self.audit,
            owner=self.owner,
            owner_uid=self.owner_uid,
        )

    def commit(self, generation: int, keys: list):
        store = TrustStore(self.paths.trust_db)
        try:
            raw = registry_bytes(generation, keys=keys)
            return store.commit(parse_registry(raw), raw, content_hash(raw))
        finally:
            store.close()

    def install_registry(self, generation: int, keys: list) -> bytes:
        raw = registry_bytes(generation, keys=keys)
        write_registry_file(self.paths.registry, raw)
        return raw

    def staged_registry(self, generation: int, keys: list, name: str = "staged.json"):
        raw = registry_bytes(generation, keys=keys)
        path = os.path.join(self.dir, name)
        write_registry_file(path, raw)
        return path, raw

    def set_water(self, mapping: dict[str, int]) -> None:
        db = open_rw(self.paths.replay_db)
        try:
            with db.txn() as cur:
                for key_id, h in mapping.items():
                    cur.execute(
                        "INSERT INTO water (key_id, h) VALUES (?, ?)"
                        " ON CONFLICT(key_id) DO UPDATE SET h = excluded.h",
                        (key_id, h),
                    )
        finally:
            db.close()

    def water(self) -> dict[str, int]:
        with auth_admin.open_readonly(self.paths.replay_db) as db:
            return {
                str(row[0]): int(row[1])
                for row in db.read("SELECT key_id, h FROM water ORDER BY key_id")
            }

    def break_trust_snapshot(self, raw: bytes = b"not a registry") -> None:
        """把信任庫的快照改壞（水位與墓碑仍讀得出）：rebuild-trust 的正當情境（中-3）。"""
        db = open_rw(self.paths.trust_db)
        try:
            with db.txn() as cur:
                cur.execute(
                    "UPDATE snapshot SET raw = ?, content_hash = ? WHERE id = 1",
                    (raw, content_hash(raw)),
                )
        finally:
            db.close()

    def break_trust_store(self) -> None:
        """把信任庫整份寫成垃圾（連讀都讀不出來）。"""
        with open(self.paths.trust_db, "wb") as fh:
            fh.write(b"broken trust store")

    def trust_snapshot(self):
        store = TrustStore(self.paths.trust_db)
        try:
            return store.state()
        finally:
            store.close()

    def assert_no_sidecars(self) -> None:
        for path in (self.paths.trust_db, self.paths.replay_db):
            for name in sqlite_sidecars(path):
                self.assertFalse(os.path.exists(name), msg=name)

    def assert_harmless_sidecars(self) -> None:
        """唯讀開庫後允許留下的只有 0 byte `-wal` 與 `-shm`，且擁有者等於庫檔（中-1）。"""
        for path in (self.paths.trust_db, self.paths.replay_db):
            if not os.path.exists(path):
                continue
            info = os.stat(path)
            for name in sqlite_sidecars(path):
                if not os.path.exists(name):
                    continue
                side = os.stat(name)
                self.assertEqual((side.st_uid, side.st_gid), (info.st_uid, info.st_gid), name)
                if name.endswith("-wal"):
                    self.assertEqual(side.st_size, 0, name)

    def assert_no_unexpected_state_files(self) -> None:
        """狀態目錄裡除了兩庫、鎖檔、審計與 SQLite sidecar 之外沒有別的檔。"""
        sidecars = {
            os.path.basename(name)
            for path in (self.paths.trust_db, self.paths.replay_db)
            for name in sqlite_sidecars(path)
        }
        expected = {
            os.path.basename(path)
            for path in (
                self.paths.trust_db,
                self.paths.replay_db,
                self.paths.store_lock,
                self.paths.audit_log,
            )
        }
        self.assertEqual(set(os.listdir(self.state_dir)) - sidecars, expected)

    def audit_lines(self) -> list[dict]:
        if not os.path.exists(self.paths.audit_log):
            return []
        with open(self.paths.audit_log, "rb") as fh:
            return [json.loads(line) for line in fh.read().splitlines()]


# --- C-801：init（create_stores 的原子建檔與 meta.schema） ---

class InitStoresTests(AdminTestCase):
    def test_c801_init_creates_two_stores_identity_schema_and_lock(self) -> None:
        result = self.init()
        self.assertEqual(result.warnings, ())
        self.assertEqual(len(result.identity), IDENTITY_BYTES * 2)
        int(result.identity, 16)
        self.assertEqual(len(result.short_identity), 8)
        for path in (self.paths.trust_db, self.paths.replay_db):
            self.assertTrue(os.path.exists(path))
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(os.stat(path).st_uid, os.geteuid())
            db = open_rw(path)
            try:
                self.assertEqual(db.read_identity(), result.identity)
                meta = {
                    str(row[0]): str(row[1])
                    for row in db.read("SELECT key, value FROM meta")
                }
            finally:
                db.close()
            self.assertEqual(meta[IDENTITY_KEY], result.identity)
            self.assertEqual(meta[SCHEMA_KEY], str(META_SCHEMA_VERSION))
        self.assertTrue(os.path.exists(self.paths.store_lock))
        self.assertEqual(_mode(self.paths.store_lock), 0o600)
        self.assert_no_sidecars()
        records = self.audit_lines()
        self.assertGreaterEqual(len(records), 2)
        self.assertEqual(records[0]["command"], "auth-store init")
        self.assertEqual(records[-1]["new"]["identity"], result.identity)
        self.assertEqual(_mode(self.paths.audit_log), 0o600)

    def test_c801_create_stores_cleanup_only_removes_files_created_this_run(self) -> None:
        """第二個庫已被別的行程搶先建好：只清掉本次建的信任庫，既有 replay 不動。"""
        with open(self.paths.replay_db, "wb") as fh:
            fh.write(b"pre-existing")
        with self.assertRaises(AdminError) as ctx:
            self.init()
        self.assertEqual(ctx.exception.kind, KIND_STORE_EXISTS)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertFalse(os.path.exists(self.paths.trust_db))
        with open(self.paths.replay_db, "rb") as fh:
            self.assertEqual(fh.read(), b"pre-existing")
        self.assert_no_sidecars()

    def test_c801_refuses_when_pair_already_exists_without_touching_them(self) -> None:
        first = self.init()
        before = _hashes(self.paths.trust_db, self.paths.replay_db, self.paths.store_lock)
        with self.assertRaises(AdminError) as ctx:
            self.init()
        self.assertEqual(ctx.exception.kind, KIND_STORE_EXISTS)
        self.assertEqual(
            _hashes(self.paths.trust_db, self.paths.replay_db, self.paths.store_lock), before
        )
        self.assertEqual(open_rw(self.paths.trust_db).read_identity(), first.identity)

    def test_c801_pair_builder_keeps_pre_existing_second_store(self) -> None:
        """`auth_db` 的兩庫建庫函式同樣只清本次建出的檔（init 之外的呼叫路徑）。"""
        with open(self.paths.replay_db, "wb") as fh:
            fh.write(b"pre-existing")
        with self.assertRaises(StoreExists):
            create_stores(self.paths.trust_db, self.paths.replay_db)
        self.assertFalse(os.path.exists(self.paths.trust_db))
        with open(self.paths.replay_db, "rb") as fh:
            self.assertEqual(fh.read(), b"pre-existing")
        self.assert_no_sidecars()

    def test_c801_pair_builder_writes_meta_schema_and_identity(self) -> None:
        identity = create_stores(self.paths.trust_db, self.paths.replay_db)
        for path in (self.paths.trust_db, self.paths.replay_db):
            db = open_rw(path)
            try:
                meta = {str(row[0]): str(row[1]) for row in db.read("SELECT key, value FROM meta")}
                self.assertEqual(db.read_identity(), identity)
            finally:
                db.close()
            self.assertEqual(meta[IDENTITY_KEY], identity)
            self.assertEqual(meta[SCHEMA_KEY], str(META_SCHEMA_VERSION))

    def test_c801_warns_when_not_a_first_run(self) -> None:
        """audit 已有紀錄或名冊世代 > 1：明示回退防護已失效，但仍建庫（只警示）。"""
        with open(self.paths.audit_log, "wb") as fh:
            fh.write(b'{"time":1}\n')
        self.install_registry(2, [key("w1-tunnel-a")])
        result = self.init()
        self.assertEqual(result.warnings, (ROLLBACK_PROTECTION_LOST,))
        self.assertTrue(os.path.exists(self.paths.trust_db))
        self.assertTrue(os.path.exists(self.paths.replay_db))
        records = self.audit_lines()
        self.assertEqual(records[1]["new"], {"warning": ROLLBACK_PROTECTION_LOST_KEY})
        self.assertEqual(records[1]["args"], {"registry_generation": 2})


# --- C-802：唯讀檢視、W-D10 ---

class InspectTests(AdminTestCase):
    def test_c802_inspect_stores_is_read_only_and_does_not_create_files(self) -> None:
        self.init()
        self.commit(4, [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")])
        self.install_registry(4, [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")])
        before = _hashes(self.paths.trust_db, self.paths.replay_db)
        listing = inspect_stores(self.paths)
        self.assertTrue(listing.identity_match)
        self.assertEqual(listing.trust_generation, 4)
        self.assertEqual(listing.tombstone_count, 1)
        self.assertEqual(listing.trust_identity, listing.replay_identity)
        self.assertEqual(inspect_stores(self.paths), listing)
        self.assertEqual(_hashes(self.paths.trust_db, self.paths.replay_db), before)
        # 唯讀不建庫、不建鎖；SQLite 自己建的 0 byte `-wal`／`-shm` 允許留下（中-1）。
        self.assert_harmless_sidecars()
        self.assert_no_unexpected_state_files()

    def test_c802_inspect_replay_reads_wal_store_while_writer_runs(self) -> None:
        """W-D10（lgwd 運行中）：另一個行程持有 flock 與寫入連線時仍讀得到 H。"""
        self.init()
        self.set_water({"w1-tunnel-a": 1000})
        holder = acquire_store_lock(self.paths.store_lock)
        db = open_rw(self.paths.replay_db)
        try:
            with db.txn() as cur:
                cur.execute(
                    "INSERT INTO nonces (key_id, nonce, ts) VALUES (?, ?, ?)",
                    ("w1-tunnel-a", "n1", 1000),
                )
            listing = inspect_replay(self.paths, clock=self.clock)
            self.assertEqual(listing.water, {"w1-tunnel-a": 1000})
            self.assertEqual(listing.nonce_count, 1)
            self.assertEqual(listing.now, _epoch(self.clock))
        finally:
            db.close()
            holder.release()
        self.assert_no_sidecars()

    def test_c802_inspect_replay_missing_store_is_2_and_creates_nothing(self) -> None:
        """W-D10：缺檔是 store_missing（結束碼 2），不得改用 rw 開出空庫。"""
        with self.assertRaises(AdminError) as ctx:
            inspect_replay(self.paths, clock=self.clock)
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_STORE_MISSING, 2))
        self.assertFalse(os.path.exists(self.paths.replay_db))
        with self.assertRaises(AdminError) as ctx:
            inspect_stores(self.paths)
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_STORE_MISSING, 2))
        self.assertFalse(os.path.exists(self.paths.trust_db))

    def test_c802_inspect_returns_2_when_wal_store_cannot_create_shm(self) -> None:
        """W-D10 反面：目錄不可寫、又沒有 `-shm` 時 `mode=ro` 讀不到 → 2，不改用 rw。"""
        if os.geteuid() == 0:
            self.skipTest("root 不受目錄權限限制，無法在本機重現唯讀目錄")
        self.init()
        ro_dir = os.path.join(self.dir, "ro-state")
        os.makedirs(ro_dir)
        target = os.path.join(ro_dir, "auth-replay.sqlite")
        shutil.copyfile(self.paths.replay_db, target)
        os.chmod(ro_dir, 0o555)
        self.addCleanup(os.chmod, ro_dir, 0o755)
        paths = auth_paths(ro_dir)
        before = _hash(target)
        with self.assertRaises(AdminError) as ctx:
            inspect_replay(paths, clock=self.clock)
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_STORE_UNAVAILABLE, 2))
        self.assertEqual(_hash(target), before)
        self.assertEqual(sorted(os.listdir(ro_dir)), ["auth-replay.sqlite"])

    def test_c802_inspect_corrupt_store_does_not_fall_back_to_rw(self) -> None:
        with open(self.paths.replay_db, "wb") as fh:
            fh.write(b"this is not a database")
        before = _hash(self.paths.replay_db)
        with self.assertRaises(AdminError) as ctx:
            inspect_replay(self.paths, clock=self.clock)
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertIn(ctx.exception.kind, (KIND_STORE_UNAVAILABLE, KIND_STORE_MISSING))
        self.assertEqual(_hash(self.paths.replay_db), before)
        self.assert_no_sidecars()


# --- C-803：recover-store 與 W-D11 ---

class RecoverReplayTests(AdminTestCase):
    def _trust_with_revoked(self, generation: int = 2):
        active = key("w1-tunnel-a")
        revoked = key("w1-tunnel-old", raw=VALID_B, status="revoked")
        self.commit(generation, [active, revoked])
        return active, revoked

    def test_c803_recover_replay_refuses_when_trust_missing(self) -> None:
        with self.assertRaises(AdminError) as ctx:
            recover_replay(self.paths, clock=self.clock, audit=self.audit, owner=self.owner)
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_STORE_MISSING, 1))
        self.assertIn("rebuild-trust", ctx.exception.hint or "")
        self.assertFalse(os.path.exists(self.paths.replay_db))
        self.assertEqual(self.audit_lines(), [])

    def test_c803_recover_replay_renames_broken_store_and_sets_water(self) -> None:
        self.init()
        self._trust_with_revoked()
        with open(self.paths.replay_db, "wb") as fh:
            fh.write(b"broken replay store")
        broken = self.paths.broken_path(self.paths.replay_db, self.clock)
        before_trust = _hash(self.paths.trust_db)
        result = recover_replay(self.paths, clock=self.clock, audit=self.audit, owner=self.owner)
        self.assertEqual(result.action, "replaced")
        self.assertEqual(result.broken_path, broken)
        with open(broken, "rb") as fh:
            self.assertEqual(fh.read(), b"broken replay store")
        self.assertEqual(_hash(self.paths.trust_db), before_trust)
        self.assertEqual(result.identity, inspect_stores(self.paths).trust_identity)
        now = _epoch(self.clock)
        self.assertEqual(
            self.water(),
            {"w1-tunnel-a": now + 60, "w1-tunnel-old": now + 60},
        )
        self.assertEqual(inspect_stores(self.paths).tombstone_count, 1)
        self.assert_harmless_sidecars()
        record = self.audit_lines()[-1]
        self.assertEqual(record["command"], "auth-replay recover-store")
        self.assertEqual(record["new"]["replay_identity"], result.identity)
        self.assertEqual(record["old"]["replay_identity"], None)

    def test_c803_recover_replay_refuses_a_healthy_store(self) -> None:
        self.init()
        self._trust_with_revoked()
        before = _hash(self.paths.replay_db)
        with self.assertRaises(AdminError) as ctx:
            recover_replay(self.paths, clock=self.clock, audit=self.audit, owner=self.owner)
        self.assertEqual(ctx.exception.kind, KIND_REPLAY_HEALTHY)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(_hash(self.paths.replay_db), before)

    def test_c803_recover_store_keeps_tombstones_and_blocks_revived_key(self) -> None:
        """W-D11：recover-store 之後墓碑仍在；較高世代刪掉撤銷 key 的名冊整份被拒。"""
        self.init()
        self._trust_with_revoked()
        with open(self.paths.replay_db, "wb") as fh:
            fh.write(b"broken")
        recover_replay(self.paths, clock=self.clock, audit=self.audit, owner=self.owner)
        raw = registry_bytes(3, keys=[key("w1-tunnel-a")])
        file_result = FileResult(
            raw=raw, registry=parse_registry(raw), content_hash=content_hash(raw), error=None
        )
        store = TrustStore(self.paths.trust_db)
        try:
            decision = decide(file_result, store.state())
        finally:
            store.close()
        self.assertEqual(decision.action, REJECT)
        self.assertEqual(decision.result, RESULT_TOMBSTONE_VIOLATION)


# --- C-804：rebuild-trust 與 W-D12 ---

class RebuildTrustTests(AdminTestCase):
    def test_c804_rebuild_salvages_tombstones_and_uses_replay_identity(self) -> None:
        """中-3 之後：只有信任庫壞掉（快照讀不出）才 rebuild；墓碑與識別照樣救得回。"""
        self.init()
        revoked = key("w1-tunnel-old", raw=VALID_B, status="revoked")
        self.commit(5, [key("w1-tunnel-a"), revoked])
        replay_identity = inspect_stores(self.paths).replay_identity
        self.break_trust_snapshot()
        staged, raw = self.staged_registry(6, [key("w1-tunnel-a"), revoked], name="trusted.json")
        result = rebuild_trust(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        self.assertEqual(result.identity, replay_identity)
        self.assertEqual(result.generation, 6)
        self.assertEqual(result.tombstones, 1)
        self.assertEqual(result.warning, ROLLBACK_PROTECTION_LOST)
        self.assertIsNotNone(result.broken_path)
        state = self.trust_snapshot()
        self.assertEqual(state.generation, 6)
        self.assertEqual(state.snapshot_hash, content_hash(raw))
        self.assertEqual(state.tombstones, (("w1-tunnel-old", VALID_B),))
        db = open_rw(self.paths.trust_db)
        replay = open_rw(self.paths.replay_db)
        try:
            self.assertTrue(check_store_identity(db, replay))
        finally:
            db.close()
            replay.close()
        record = self.audit_lines()[-1]
        self.assertEqual(record["command"], "auth-registry rebuild-trust")
        self.assertEqual(record["new"]["warning"], ROLLBACK_PROTECTION_LOST_KEY)
        self.assertEqual(record["old"]["generation"], 5)
        self.assertEqual(record["new"]["generation"], 6)
        self.assert_no_sidecars()

    def test_c804_rebuild_refuses_a_healthy_trust_store(self) -> None:
        """中-3：健康的信任庫不得用較低世代的名冊重建（那等於繞過 reset-generation）。"""
        self.init()
        self.commit(100, [key("w1-tunnel-a")])
        self.install_registry(100, [key("w1-tunnel-a")])
        staged, _ = self.staged_registry(12, [key("w1-tunnel-a")], name="lower.json")
        before = _hashes(
            self.paths.trust_db, self.paths.replay_db, self.paths.registry, self.paths.audit_log
        )
        with self.assertRaises(AdminError) as ctx:
            rebuild_trust(
                self.paths, staged, clock=self.clock, audit=self.audit,
                owner=self.owner, owner_uid=self.owner_uid,
            )
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_TRUST_HEALTHY, 1))
        self.assertIn("reset-generation", ctx.exception.hint or "")
        self.assertEqual(
            _hashes(
                self.paths.trust_db,
                self.paths.replay_db,
                self.paths.registry,
                self.paths.audit_log,
            ),
            before,
        )
        self.assertEqual(self.trust_snapshot().generation, 100)

    def test_c804_rebuild_refuses_downgrade_when_snapshot_unreadable(self) -> None:
        """中-3：只能部分讀出時也不得在同一步「重建＋降世代」。"""
        self.init()
        self.commit(100, [key("w1-tunnel-a")])
        self.install_registry(100, [key("w1-tunnel-a")])
        self.break_trust_snapshot()
        staged, _ = self.staged_registry(12, [key("w1-tunnel-a")], name="lower.json")
        before = _hashes(self.paths.trust_db, self.paths.replay_db, self.paths.audit_log)
        with self.assertRaises(AdminError) as ctx:
            rebuild_trust(
                self.paths, staged, clock=self.clock, audit=self.audit,
                owner=self.owner, owner_uid=self.owner_uid,
            )
        self.assertEqual(
            (ctx.exception.kind, ctx.exception.exit_code), (KIND_TRUST_GENERATION_DOWNGRADE, 1)
        )
        self.assertIn("100", ctx.exception.hint or "")
        self.assertEqual(
            _hashes(self.paths.trust_db, self.paths.replay_db, self.paths.audit_log), before
        )

    def test_c804_rebuild_refuses_tombstone_conflict_without_changing_anything(self) -> None:
        self.init()
        self.commit(5, [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")])
        self.break_trust_snapshot()
        staged, _ = self.staged_registry(6, [key("w1-tunnel-a")], name="revive.json")
        before = _hashes(self.paths.trust_db, self.paths.replay_db, self.paths.audit_log)
        with self.assertRaises(AdminError) as ctx:
            rebuild_trust(
                self.paths, staged, clock=self.clock, audit=self.audit,
                owner=self.owner, owner_uid=self.owner_uid,
            )
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_TOMBSTONE_VIOLATION, 1))
        self.assertEqual(
            _hashes(self.paths.trust_db, self.paths.replay_db, self.paths.audit_log), before
        )

    def test_c804_rebuild_renames_orphan_sidecars_when_main_file_is_gone(self) -> None:
        """審查 §6 低-3：主檔不在、只剩 `-wal`／`-shm` 時也要改名留證。"""
        self.init()
        self.commit(1, [key("w1-tunnel-a")])
        staged, _ = self.staged_registry(2, [key("w1-tunnel-a")], name="next.json")
        wal, shm = sqlite_sidecars(self.paths.trust_db)
        for name in (self.paths.trust_db, shm):
            if os.path.exists(name):
                os.unlink(name)
        with open(wal, "wb") as fh:
            fh.write(b"")
        result = rebuild_trust(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        self.assertIsNotNone(result.broken_path)
        self.assertTrue(os.path.exists(result.broken_path + "-wal"))
        self.assertEqual(self.trust_snapshot().generation, 2)

    def test_c804_rebuild_without_replay_generates_identity_then_recover_store(self) -> None:
        """W-D12：信任庫與 replay 都壞了 → 新識別＋提示 recover-store；跑完才一致。"""
        self.init()
        self.commit(1, [key("w1-tunnel-a")])
        old_identity = inspect_stores(self.paths).trust_identity
        self.break_trust_store()
        for path in (self.paths.replay_db, *sqlite_sidecars(self.paths.replay_db)):
            if os.path.exists(path):
                os.unlink(path)
        staged, _ = self.staged_registry(2, [key("w1-tunnel-a"), key("w1-key-b", raw=VALID_C)])
        result = rebuild_trust(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        self.assertNotEqual(result.identity, old_identity)
        self.assertFalse(inspect_stores(self.paths).identity_match)
        self.assertFalse(inspect_stores(self.paths).replay_present)
        recovered = recover_replay(
            self.paths, clock=self.clock, audit=self.audit, owner=self.owner
        )
        self.assertEqual(recovered.identity, result.identity)
        self.assertTrue(inspect_stores(self.paths).identity_match)


# --- C-805：reset-generation 與 W-D7／W-D8 ---

class ResetGenerationTests(AdminTestCase):
    def _setup_generation_100(self):
        active = key("w1-tunnel-a")
        revoked = key("w1-tunnel-old", raw=VALID_B, status="revoked")
        only_high = key("w1-only-in-100", raw=VALID_C)
        self.commit(100, [active, revoked, only_high])
        self.install_registry(100, [active, revoked, only_high])
        return active, revoked

    def test_c805_reset_lowers_generation_and_swaps_snapshot_in_one_transaction(self) -> None:
        self.init()
        active, revoked = self._setup_generation_100()
        staged, raw = self.staged_registry(12, [active, revoked], name="trusted-12.json")
        result = reset_generation(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid, registry_owner=self.owner,
        )
        self.assertEqual((result.old_generation, result.new_generation), (100, 12))
        self.assertEqual(result.new_hash, content_hash(raw))
        self.assertTrue(result.registry_written)
        with open(self.paths.registry, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        self.assertEqual(_mode(self.paths.registry), REGISTRY_MODE)
        state = self.trust_snapshot()
        self.assertEqual(state.generation, 12)
        self.assertEqual(state.snapshot_raw, raw)
        self.assertEqual(state.tombstones, (("w1-tunnel-old", VALID_B),))
        db = open_rw(self.paths.trust_db)
        try:
            rows = db.read("SELECT generation FROM generation WHERE id = 1")
        finally:
            db.close()
        self.assertEqual([int(row[0]) for row in rows], [12])
        # 只在世代 100 出現的 key 不在新快照裡；只在 12 有效的 key 在。
        self.assertIsNone(parse_registry(state.snapshot_raw).key_by_id("w1-only-in-100"))
        self.assertIsNotNone(parse_registry(state.snapshot_raw).key_by_id("w1-tunnel-a"))
        record = self.audit_lines()[-1]
        self.assertEqual(record["command"], "auth-registry reset-generation")
        self.assertEqual(record["old"]["generation"], 100)
        self.assertEqual(record["new"]["generation"], 12)
        self.assert_no_sidecars()

    def test_c805_reset_is_idempotent(self) -> None:
        self.init()
        active, revoked = self._setup_generation_100()
        staged, raw = self.staged_registry(12, [active, revoked], name="trusted-12.json")
        first = reset_generation(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        second = reset_generation(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        self.assertEqual((first.old_generation, first.new_generation), (100, 12))
        self.assertEqual((second.old_generation, second.new_generation), (12, 12))
        self.assertEqual(self.trust_snapshot().snapshot_raw, raw)

    def test_c805_reset_tombstone_violation_changes_nothing(self) -> None:
        """W-D8：名冊刪掉已撤銷 key → 名冊檔、信任庫雜湊都不變。"""
        self.init()
        self._setup_generation_100()
        staged, _ = self.staged_registry(12, [key("w1-tunnel-a")], name="revive.json")
        registry_before = _hash(self.paths.registry)
        trust_before = _hash(self.paths.trust_db)
        audit_before = _hash(self.paths.audit_log)
        with self.assertRaises(AdminError) as ctx:
            reset_generation(
                self.paths, staged, clock=self.clock, audit=self.audit,
                owner=self.owner, owner_uid=self.owner_uid,
            )
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_TOMBSTONE_VIOLATION, 1))
        self.assertEqual(_hash(self.paths.registry), registry_before)
        self.assertEqual(_hash(self.paths.trust_db), trust_before)
        self.assertEqual(_hash(self.paths.audit_log), audit_before)
        self.assertEqual(self.trust_snapshot().generation, 100)

    def test_c805_reset_crash_after_registry_write_before_transaction(self) -> None:
        """W-D7：名冊檔已換、transaction 前當機 → 信任庫不變；重跑同一指令成功。"""
        self.init()
        active, revoked = self._setup_generation_100()
        staged, raw = self.staged_registry(12, [active, revoked], name="trusted-12.json")
        with mock.patch.object(
            auth_admin.TrustStore, "reset_to", side_effect=RuntimeError("simulated crash")
        ):
            with self.assertRaises(RuntimeError):
                reset_generation(
                    self.paths, staged, clock=self.clock, audit=self.audit,
                    owner=self.owner, owner_uid=self.owner_uid,
                )
        with open(self.paths.registry, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        self.assertEqual(self.trust_snapshot().generation, 100)
        # 重啟時（lgwd 用同一套 decide）較低的世代被拒絕、沿用舊快照並告警。
        store = TrustStore(self.paths.trust_db)
        try:
            decision = decide(
                FileResult(
                    raw=raw,
                    registry=parse_registry(raw),
                    content_hash=content_hash(raw),
                    error=None,
                ),
                store.state(),
            )
        finally:
            store.close()
        self.assertEqual((decision.action, decision.result), (REJECT, RESULT_GENERATION_NOT_INCREASED))
        result = reset_generation(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        self.assertEqual((result.old_generation, result.new_generation), (100, 12))
        self.assertEqual(self.trust_snapshot().generation, 12)


# --- C-806：reset-key-water ---

class ResetKeyWaterTests(AdminTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.now = _epoch(self.clock)

    def test_c806_reset_key_water_sets_now_minus_65_and_reports_old(self) -> None:
        self.set_water({"w1-a": self.now + 500, "w1-b": self.now + 10})
        result = reset_key_water(
            self.paths, clock=self.clock, audit=self.audit, keys=["w1-a"], owner=self.owner
        )
        self.assertEqual(result.now, self.now)
        self.assertEqual(result.cost, RESET_WATER_COST)
        self.assertEqual(len(result.changes), 1)
        change = result.changes[0]
        self.assertEqual((change.key_id, change.old, change.new), ("w1-a", self.now + 500, self.now - 65))
        self.assertEqual(self.water(), {"w1-a": self.now - 65, "w1-b": self.now + 10})
        record = self.audit_lines()[-1]
        self.assertEqual(record["command"], "auth-replay reset-key-water")
        self.assertEqual(record["old"]["water"], {"w1-a": self.now + 500})
        self.assertEqual(record["new"]["water"], {"w1-a": self.now - 65})
        self.assert_harmless_sidecars()

    def test_c806_all_future_selects_only_h_above_now_plus_60(self) -> None:
        self.set_water(
            {
                "w1-future": self.now + 61,
                "w1-edge": self.now + 60,
                "w1-past": self.now + 30,
            }
        )
        result = reset_key_water(
            self.paths, clock=self.clock, audit=self.audit, all_future=True, owner=self.owner
        )
        self.assertEqual([change.key_id for change in result.changes], ["w1-future"])
        self.assertEqual(
            self.water(),
            {
                "w1-future": self.now - 65,
                "w1-edge": self.now + 60,
                "w1-past": self.now + 30,
            },
        )

    def test_c806_missing_key_is_error_and_changes_nothing(self) -> None:
        self.set_water({"w1-a": self.now + 500})
        before = self.water()
        with self.assertRaises(AdminError) as ctx:
            reset_key_water(
                self.paths, clock=self.clock, audit=self.audit, keys=["nope"], owner=self.owner
            )
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_KEY_NOT_FOUND, 1))
        self.assertEqual(self.water(), before)

    def test_c806_requires_exactly_one_selection_mode(self) -> None:
        for kwargs in ({}, {"keys": ["w1-a"], "all_future": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(AdminError) as ctx:
                    reset_key_water(self.paths, clock=self.clock, audit=self.audit, **kwargs)
                self.assertEqual(ctx.exception.exit_code, 2)


# --- C-807：審計日誌 ---

class AuditLogTests(AdminTestCase):
    def test_c807_one_json_line_per_record_append_and_0600(self) -> None:
        self.audit.append("auth-store init", old={"generation": None}, new={"generation": 1})
        self.audit.append("auth-replay reset-key-water", args={"keys": ["w1-a"]})
        self.assertEqual(_mode(self.paths.audit_log), 0o600)
        with open(self.paths.audit_log, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(line) for line in lines)
        self.assertEqual(
            set(first), {"time", "uid", "sudo_user", "command", "args", "old", "new"}
        )
        self.assertEqual(first["time"], _epoch(self.clock))
        self.assertEqual(first["uid"], os.geteuid())
        self.assertIsNone(first["sudo_user"])
        self.assertEqual(first["old"], {"generation": None})
        self.assertEqual(second["args"], {"keys": ["w1-a"]})

    def test_c807_rejects_public_keys_signatures_and_binary_values(self) -> None:
        with self.assertRaises(AuditValueError):
            self.audit.append("auth-registry install", old={"public_key": public_key_line(VALID_A)})
        with self.assertRaises(AuditValueError):
            self.audit.append("auth-registry install", new={"blob": VALID_A})
        with self.assertRaises(AuditValueError):
            self.audit.append("auth-registry install", new={"signature": "A" * 240})
        self.assertFalse(os.path.exists(self.paths.audit_log))

    def test_c807_symlink_audit_log_is_rejected(self) -> None:
        target = os.path.join(self.dir, "elsewhere.log")
        os.symlink(target, self.paths.audit_log)
        with self.assertRaises(AdminError) as ctx:
            self.audit.append("auth-store init")
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_AUDIT_FAILED, 2))
        self.assertFalse(os.path.exists(target))


# --- C-808：flock、擁有者、W-D5／W-D6／W-D9 ---

class StoreLockTests(AdminTestCase):
    def _prepared(self):
        self.init()
        keys = [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")]
        self.commit(4, keys)
        self.install_registry(4, keys)
        return self.staged_registry(10, keys, name="next.json")

    def test_c808_every_write_is_blocked_while_lgwd_holds_the_lock(self) -> None:
        """W-D5：lgwd 持有鎖時每一個寫入都回 2，庫、名冊、審計完全不變。"""
        staged, _ = self._prepared()
        paths = (self.paths.trust_db, self.paths.replay_db, self.paths.registry, self.paths.audit_log)
        before = _hashes(*paths)
        holder = acquire_store_lock(self.paths.store_lock)
        try:
            operations = (
                lambda: recover_replay(self.paths, clock=self.clock, audit=self.audit, owner=self.owner),
                lambda: rebuild_trust(
                    self.paths, staged, clock=self.clock, audit=self.audit,
                    owner=self.owner, owner_uid=self.owner_uid,
                ),
                lambda: reset_generation(
                    self.paths, staged, clock=self.clock, audit=self.audit,
                    owner=self.owner, owner_uid=self.owner_uid,
                ),
                lambda: reset_key_water(
                    self.paths, clock=self.clock, audit=self.audit,
                    keys=["w1-tunnel-a"], owner=self.owner,
                ),
            )
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(AdminError) as ctx:
                        operation()
                    self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_LOCK_BUSY, 2))
        finally:
            holder.release()
        self.assertEqual(_hashes(*paths), before)
        self.assert_no_sidecars()

    def test_c808_init_is_blocked_before_touching_anything(self) -> None:
        fresh_dir = os.path.join(self.dir, "fresh")
        os.makedirs(fresh_dir)
        paths = auth_paths(fresh_dir, registry=os.path.join(self.etc, "fresh-registry.json"))
        audit = AuditLog(paths.audit_log, clock=self.clock, uid=os.geteuid(), sudo_user=None)
        holder = acquire_store_lock(paths.store_lock)
        try:
            with self.assertRaises(AdminError) as ctx:
                init_stores(paths, clock=self.clock, audit=audit, owner=self.owner)
        finally:
            holder.release()
        self.assertEqual((ctx.exception.kind, ctx.exception.exit_code), (KIND_LOCK_BUSY, 2))
        self.assertFalse(os.path.exists(paths.trust_db))
        self.assertFalse(os.path.exists(paths.replay_db))
        self.assertFalse(os.path.exists(paths.audit_log))

    def test_c808_lgwd_cannot_take_the_lock_during_a_write(self) -> None:
        """W-D6：寫入指令持鎖期間，lgwd 的取鎖會失敗（它的結束碼 78 由 D2 的 main 實作）。"""
        staged, _ = self._prepared()
        real_reset = TrustStore.reset_to
        seen = {}

        def probe(store, registry, raw, digest):
            try:
                acquire_store_lock(self.paths.store_lock)
            except LockBusy:
                seen["busy"] = True
            else:
                self.fail("lgwd 在寫入期間取得了鎖")
            return real_reset(store, registry, raw, digest)

        with mock.patch.object(auth_admin.TrustStore, "reset_to", probe):
            reset_generation(
                self.paths, staged, clock=self.clock, audit=self.audit,
                owner=self.owner, owner_uid=self.owner_uid,
            )
        self.assertTrue(seen.get("busy"))

    def test_c808_writes_leave_no_sidecars_and_fix_owner_and_mode(self) -> None:
        """W-D9：root 寫過庫之後不留 root 的 -wal／-shm，庫與鎖檔 0600。"""
        self.init()
        active = key("w1-tunnel-a")
        revoked = key("w1-tunnel-old", raw=VALID_B, status="revoked")
        self.commit(1, [active, revoked])
        staged, _ = self.staged_registry(2, [active, revoked], name="trusted-2.json")
        reset_generation(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        with open(self.paths.replay_db, "wb") as fh:
            fh.write(b"broken")
        recover_replay(self.paths, clock=self.clock, audit=self.audit, owner=self.owner)
        # 中-3 之後健康的信任庫不得 rebuild：先把快照弄壞，這是 rebuild 的正當情境。
        self.break_trust_snapshot()
        rebuild_trust(
            self.paths, staged, clock=self.clock, audit=self.audit,
            owner=self.owner, owner_uid=self.owner_uid,
        )
        reset_key_water(
            self.paths, clock=self.clock, audit=self.audit, keys=[], owner=self.owner
        )
        self.assert_no_sidecars()
        for path in (self.paths.trust_db, self.paths.replay_db):
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(os.stat(path).st_uid, self.owner[0])
            self.assertEqual(os.stat(path).st_gid, self.owner[1])
        self.assertEqual(_mode(self.paths.store_lock), 0o600)


# --- C-809：TrustStore.reset_to 與 MAX_GENERATION 單一來源 ---

class ResetToTests(AdminTestCase):
    def test_c809_reset_to_bypasses_guard_but_commit_still_refuses(self) -> None:
        self.init()
        store = TrustStore(self.paths.trust_db)
        self.addCleanup(store.close)
        raw10 = registry_bytes(10, keys=[key("w1-tunnel-a")])
        store.commit(parse_registry(raw10), raw10, content_hash(raw10))
        raw3 = registry_bytes(3, keys=[key("w1-tunnel-a")])
        state = store.reset_to(parse_registry(raw3), raw3, content_hash(raw3))
        self.assertEqual(state.generation, 3)
        self.assertEqual(store.state().snapshot_raw, raw3)
        with self.assertRaises(TrustReject) as ctx:
            store.commit(parse_registry(raw3), raw3, content_hash(raw3))
        self.assertEqual(ctx.exception.kind, "generation_not_increased")
        raw4 = registry_bytes(4, keys=[key("w1-tunnel-a")])
        self.assertEqual(
            store.commit(parse_registry(raw4), raw4, content_hash(raw4)).generation, 4
        )

    def test_c809_reset_to_rejects_hash_mismatch_without_changing_state(self) -> None:
        self.init()
        store = TrustStore(self.paths.trust_db)
        self.addCleanup(store.close)
        raw = registry_bytes(7, keys=[key("w1-tunnel-a")])
        with self.assertRaises(ValueError):
            store.reset_to(parse_registry(raw), raw, content_hash(raw + b" "))
        self.assertFalse(store.state().has_snapshot)

    def test_c809_max_generation_has_exactly_one_definition(self) -> None:
        definitions = []
        for path in sorted((REPO / "lgwd").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if any(isinstance(target, ast.Name) and target.id == "MAX_GENERATION" for target in node.targets):
                    definitions.append(path.name)
        self.assertEqual(definitions, ["auth_paths.py"])
        from lgwd.auth_paths import MAX_GENERATION as paths_max
        from lgwd.auth_registry import MAX_GENERATION as registry_max

        self.assertIs(registry_max, paths_max)


    def test_c809_reset_to_rolls_back_everything_when_generation_write_fails(self) -> None:
        """RM3：`reset_to` 在寫完 `snapshot`、寫 `generation` 之前失敗 → 三者一起回滾。

        用真觸發（SQLite `BEFORE INSERT ... RAISE(ABORT)`）在交易中途丟例外，比對三個
        部分（世代、快照、墓碑）與修改前完全一致；若把 `reset_to` 拆成兩個交易（審查
        RM3 的變異），`snapshot` 已經落地，這支就會轉紅。
        """
        self.init()
        store = TrustStore(self.paths.trust_db)
        self.addCleanup(store.close)
        revoked = key("w1-tunnel-old", raw=VALID_B, status="revoked")
        raw100 = registry_bytes(100, keys=[key("w1-tunnel-a"), revoked])
        store.commit(parse_registry(raw100), raw100, content_hash(raw100))
        before = store.state()
        db = open_rw(self.paths.trust_db)
        try:
            with db.txn() as cur:
                cur.execute(
                    "CREATE TRIGGER fail_generation BEFORE INSERT ON generation"
                    " BEGIN SELECT RAISE(ABORT, 'boom'); END"
                )
        finally:
            db.close()
        raw12 = registry_bytes(
            12,
            keys=[
                key("w1-tunnel-a"),
                revoked,
                key("w1-tunnel-new", raw=VALID_C, status="revoked"),
            ],
        )
        with self.assertRaises(StoreUnavailable):
            store.reset_to(parse_registry(raw12), raw12, content_hash(raw12))
        after = store.state()
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(after.generation, 100)
        self.assertEqual(after.snapshot_raw, before.snapshot_raw)
        self.assertEqual(after.snapshot_hash, before.snapshot_hash)
        self.assertEqual(after.tombstones, before.tombstones)
        db = open_rw(self.paths.trust_db)
        try:
            rows = db.read("SELECT generation FROM generation WHERE id = 1")
        finally:
            db.close()
        self.assertEqual([int(row[0]) for row in rows], [100])


# --- 中-1：唯讀路徑不取 store 鎖、sidecar 只對齊擁有者不刪 ---

class ReadOnlyLockTests(AdminTestCase):
    def test_c802_read_only_never_takes_the_store_lock(self) -> None:
        """中-1：inspect 不呼叫 `acquire_store_lock`。

        舊行為是唯讀路徑拿 `LOCK_EX` 去清 sidecar；那一刻剛好啟動的 lgwd 會拿到
        `LockBusy` 並以 78 結束（D5d 加了 `RestartPreventExitStatus=78` 之後就一直停）。
        """
        self.init()
        self.commit(1, [key("w1-tunnel-a")])
        self.install_registry(1, [key("w1-tunnel-a")])
        calls = []
        real = auth_admin.acquire_store_lock

        def spy(path):
            calls.append(path)
            return real(path)

        with mock.patch.object(auth_admin, "acquire_store_lock", spy):
            inspect_stores(self.paths)
            inspect_replay(self.paths, clock=self.clock)
        self.assertEqual(calls, [])

    def test_c802_lgwd_can_take_the_lock_right_after_inspect(self) -> None:
        """中-1：inspect 收 sidecar 的那一刻，另一個 fd 取 `LOCK_EX|LOCK_NB` 一定成功。"""
        self.init()
        self.set_water({"w1-a": 1000})
        seen = []
        real = auth_admin._readonly_sidecars

        def probe(path, existed):
            handle = acquire_store_lock(self.paths.store_lock)
            try:
                seen.append("free")
            finally:
                handle.release()
            return real(path, existed)

        with mock.patch.object(auth_admin, "_readonly_sidecars", probe):
            inspect_stores(self.paths)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"free"})

    def test_c802_inspect_leaves_owned_empty_sidecars_and_a_write_cleans_them(self) -> None:
        """中-1：不刪 sidecar，但它們是 0 byte `-wal`、擁有者同庫檔，之後寫入清掉。"""
        self.init()
        self.set_water({"w1-a": 1000})
        inspect_replay(self.paths, clock=self.clock)
        self.assertTrue(os.path.exists(self.paths.replay_db + "-shm"))
        self.assert_harmless_sidecars()
        with store_write_lock(self.paths, self.owner):
            pass
        self.assert_no_sidecars()


# --- 中-2：權限收尾在 store 鎖釋放之前（成功與失敗都做） ---

class FinalizeInsideLockTests(AdminTestCase):
    def _prepared(self):
        self.init()
        active = key("w1-tunnel-a")
        revoked = key("w1-tunnel-old", raw=VALID_B, status="revoked")
        self.commit(1, [active, revoked])
        staged, _ = self.staged_registry(2, [active, revoked], name="t2.json")
        return staged

    def test_c808_permissions_are_finalized_before_the_lock_is_released(self) -> None:
        """中-2：finalize 時 lgwd 仍取不到鎖（不會在權限收尾前啟動）。"""
        staged = self._prepared()
        seen = []
        real = auth_admin.finalize_permissions

        def probe(paths, owner, *, extra=()):
            try:
                handle = acquire_store_lock(self.paths.store_lock)
            except LockBusy:
                seen.append("held")
            else:
                handle.release()
                seen.append("free")
            return real(paths, owner, extra=extra)

        with mock.patch.object(auth_admin, "finalize_permissions", probe):
            reset_generation(
                self.paths, staged, clock=self.clock, audit=self.audit,
                owner=self.owner, owner_uid=self.owner_uid,
            )
        self.assertEqual(seen, ["held"])

    def test_c808_failed_write_still_fixes_mode_before_releasing(self) -> None:
        """中-2：寫入中途例外，權限收尾照做（不會留下權限過寬的庫）。"""
        staged = self._prepared()
        os.chmod(self.paths.trust_db, 0o644)
        with mock.patch.object(
            auth_admin.TrustStore, "reset_to", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                reset_generation(
                    self.paths, staged, clock=self.clock, audit=self.audit,
                    owner=self.owner, owner_uid=self.owner_uid,
                )
        for path in (self.paths.trust_db, self.paths.replay_db, self.paths.store_lock):
            self.assertTrue(os.path.exists(path))
            self.assertEqual(_mode(path), 0o600, path)

    def test_c808_owner_failure_keeps_the_original_exception(self) -> None:
        """中-2：原本的例外優先，擁有者失敗附在 `owner_error`（3.10 沒有 `add_note`）。"""
        staged = self._prepared()
        with mock.patch.object(
            auth_admin.TrustStore, "reset_to", side_effect=RuntimeError("boom")
        ), mock.patch.object(auth_admin.os, "chown", side_effect=OSError("no")):
            with self.assertRaises(RuntimeError) as ctx:
                reset_generation(
                    self.paths, staged, clock=self.clock, audit=self.audit,
                    owner=self.owner, owner_uid=self.owner_uid,
                )
        owner_error = getattr(ctx.exception, "owner_error", None)
        self.assertIsNotNone(owner_error)
        self.assertEqual(owner_error.kind, KIND_OWNER_FAILED)


# --- 中-4：建庫只有 auth_db 一份實作；守護行程不得 import 管理程式庫 ---

DAEMON_PATH = (
    "main.py",
    "http.py",
    "auth.py",
    "auth_reload.py",
    "auth_trust.py",
    "auth_replay.py",
    "auth_db.py",
)


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            for alias in node.names:
                names.add(module + "." + alias.name)
    return names


def _called_names(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                found.append(func.id)
            elif isinstance(func, ast.Attribute):
                found.append(func.attr)
    return found


class CreateStoreDelegationTests(AdminTestCase):
    def test_c801_init_delegates_each_store_to_auth_db_create_store(self) -> None:
        """中-4：init 的兩個庫都由 `auth_db.create_store` 建（不另寫一份）。"""
        calls = []
        real = auth_db.create_store

        def spy(path, schema, identity):
            calls.append((os.path.basename(path), identity))
            return real(path, schema, identity)

        with mock.patch.object(auth_admin, "create_store", spy):
            result = self.init()
        self.assertEqual(
            [name for name, _ in calls], ["auth-trust.sqlite", "auth-replay.sqlite"]
        )
        self.assertTrue(all(identity == result.identity for _, identity in calls))

    def test_c301_admin_has_no_store_creation_code_of_its_own(self) -> None:
        """中-4：`O_EXCL`／schema 的建庫程式碼只有 `auth_db` 一份（auth_admin 只呼叫）。"""
        source = (REPO / "lgwd" / "auth_admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertIn("lgwd.auth_db.create_store", _imported_names(tree))
        self.assertIn("create_store", _called_names(tree))
        attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertNotIn("O_EXCL", attributes)
        self.assertNotIn("executescript", _called_names(tree))

    def test_c301_daemon_path_never_imports_admin_or_calls_create_store(self) -> None:
        """C-301（改寫成 AST／import graph，中-4）：守護行程不得 import 管理程式庫或建庫。

        舊版是掃 `lgwd/*.py` 的文字找 `create_stores`；那會連 docstring 一起擋，也
        擋不到 `import auth_admin`。這裡改看守護行程的執行路徑：不得 import
        `auth_admin`，也不得呼叫 `create_store`／`create_stores`（`auth_db` 自己是
        唯一的定義處，允許它組合）。
        """
        offenders = []
        create_calls = {"create_store", "create_stores"}
        for name in DAEMON_PATH:
            path = REPO / "lgwd" / name
            self.assertTrue(path.exists(), name)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = _imported_names(tree)
            if any(
                item == "auth_admin" or item.startswith("auth_admin.")
                or item == "lgwd.auth_admin" or item.startswith("lgwd.auth_admin.")
                for item in imported
            ):
                offenders.append(name + ": imports auth_admin")
            if name != "auth_db.py" and create_calls & set(_called_names(tree)):
                offenders.append(name + ": calls create_store(s)")
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
