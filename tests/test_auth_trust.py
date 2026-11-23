"""lgwd/auth_trust.py 的單元測試（D2 PR-4，契約 C-402；design「信任庫與防重放庫分開」）。

全部用暫存 SQLite（`tempfile`）與假名冊；不碰 /var/lib/llmgateway、不讀憑證、不連真機。
故障注入用真實觸發：在 `snapshot` 表上加一個會 `RAISE(ABORT)` 的 trigger，讓提交在
快照寫入時失敗，再看墓碑有沒有被留下（同一交易＝不會）。

測試名都帶 C 編號，報告的「C 編號對測試名稱」表直接由這裡對出來。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from lgwd.auth_db import StoreMissing, StoreUnavailable, create_stores
from lgwd.auth_paths import auth_paths
from lgwd.auth_registry import content_hash, parse_registry
from lgwd.auth_trust import (
    EMPTY_TRUST_STATE,
    TrustReject,
    TrustState,
    TrustStateError,
    TrustStore,
)
from tests.auth_fakes import VALID_A, VALID_B, VALID_C, key, registry_bytes


def _parse(data: bytes):
    return parse_registry(data)


class TrustStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir)
        self.paths = auth_paths(self.state_dir, registry=os.path.join(self.dir, "registry.json"))
        self.identity = create_stores(self.paths.trust_db, self.paths.replay_db)
        self.trust = TrustStore(self.paths.trust_db)
        self.addCleanup(self.trust.close)

    def commit(self, generation: int, keys: list) -> TrustState:
        raw = registry_bytes(generation, keys=keys)
        return self.trust.commit(_parse(raw), raw, content_hash(raw))


class TrustStateShapeTests(unittest.TestCase):
    def test_c402_snapshot_fields_must_be_present_together(self) -> None:
        with self.assertRaises(TrustStateError):
            TrustState(generation=1, snapshot_raw=None, snapshot_hash=None)
        with self.assertRaises(TrustStateError):
            TrustState(generation=None, snapshot_raw=b"{}", snapshot_hash="sha256:00")
        self.assertFalse(EMPTY_TRUST_STATE.has_snapshot)
        self.assertEqual(EMPTY_TRUST_STATE.tombstone_list(), [])

    def test_c402_tombstone_list_is_a_copy(self) -> None:
        state = TrustState(1, b"{}", "sha256:00", (("a", VALID_A),))
        state.tombstone_list().append(("b", VALID_B))
        self.assertEqual(state.tombstones, (("a", VALID_A),))


class TrustStoreReadTests(TrustStoreTestCase):
    def test_c402_empty_store_has_no_snapshot(self) -> None:
        state = self.trust.state()
        self.assertFalse(state.has_snapshot)
        self.assertIsNone(state.generation)
        self.assertIsNone(state.snapshot_raw)
        self.assertIsNone(state.snapshot_hash)
        self.assertEqual(state.tombstones, ())

    def test_c402_missing_store_is_not_created(self) -> None:
        missing = os.path.join(self.state_dir, "absent.sqlite")
        with self.assertRaises(StoreMissing):
            TrustStore(missing)
        self.assertFalse(os.path.exists(missing))

    def test_c402_state_survives_reopen(self) -> None:
        keys = [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")]
        self.commit(1, keys)
        before = self.trust.state()
        self.trust.close()
        reopened = TrustStore(self.paths.trust_db)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.state(), before)


class TrustStoreCommitTests(TrustStoreTestCase):
    def test_c402_commit_first_writes_generation_snapshot_and_tombstones(self) -> None:
        keys = [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")]
        raw = registry_bytes(1, keys=keys)
        state = self.trust.commit(_parse(raw), raw, content_hash(raw))
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.snapshot_raw, raw)
        self.assertEqual(state.snapshot_hash, content_hash(raw))
        # 已撤銷的 key 進墓碑，形狀是 (key ID, 驗證後 32 bytes)，直接餵 check_tombstones。
        self.assertEqual(state.tombstones, (("w1-tunnel-old", VALID_B),))
        self.assertEqual(self.trust.state(), state)

    def test_c402_tombstones_only_grow(self) -> None:
        """墓碑只增不減：較高世代刪掉撤銷列、或以同 ID 換公鑰，都不會動到墓碑。"""
        self.commit(1, [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")])
        # 世代 2 完全不列那把已撤銷的 key（decide 會擋；這裡直接呼叫 commit 驗只增）。
        self.commit(2, [key("w1-tunnel-a")])
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))
        # 世代 3 用同一個 key ID 換成另一組 bytes，也不能覆寫墓碑。
        self.commit(3, [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_C, status="revoked")])
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))

    def test_c402_commit_is_one_transaction(self) -> None:
        """快照寫入失敗時，同一交易裡先寫的墓碑也要一起 rollback。"""
        keys = [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")]
        db = self.trust.db
        db.conn.execute(
            "CREATE TRIGGER refuse_snapshot BEFORE INSERT ON snapshot"
            " BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        raw = registry_bytes(1, keys=keys)
        with self.assertRaises(StoreUnavailable):
            self.trust.commit(_parse(raw), raw, content_hash(raw))
        state = self.trust.state()
        self.assertFalse(state.has_snapshot)
        self.assertEqual(state.tombstones, ())

    def test_c402_commit_requires_increasing_generation(self) -> None:
        self.commit(2, [key("w1-tunnel-a")])
        before = self.trust.state()
        for generation in (2, 1):
            with self.subTest(generation=generation):
                raw = registry_bytes(generation, keys=[key("w1-tunnel-a")])
                with self.assertRaises(TrustReject) as caught:
                    self.trust.commit(_parse(raw), raw, content_hash(raw))
                self.assertEqual(caught.exception.kind, "generation_not_increased")
                self.assertEqual(self.trust.state(), before)

    def test_c402_commit_rejects_hash_mismatch(self) -> None:
        raw = registry_bytes(1, keys=[key("w1-tunnel-a")])
        with self.assertRaises(ValueError):
            self.trust.commit(_parse(raw), raw, content_hash(raw + b" "))
        self.assertFalse(self.trust.state().has_snapshot)

    def test_c402_generation_table_is_a_mirror(self) -> None:
        self.commit(7, [key("w1-tunnel-a")])
        rows = self.trust.db.read("SELECT generation FROM generation WHERE id = 1")
        self.assertEqual([int(row[0]) for row in rows], [7])
        self.assertEqual(self.trust.state().generation, 7)

    def test_c402_tombstone_column_is_32_byte_blob(self) -> None:
        self.commit(1, [key("w1-tunnel-old", raw=VALID_B, status="revoked")])
        rows = self.trust.db.read("SELECT typeof(key), length(key), key FROM tombstones")
        self.assertEqual(rows[0][0], "blob")
        self.assertEqual(int(rows[0][1]), 32)
        self.assertEqual(bytes(rows[0][2]), VALID_B)
        with self.assertRaises(sqlite3.IntegrityError):
            self.trust.db.conn.execute(
                "INSERT INTO tombstones (key_id, key) VALUES (?, ?)", ("short", b"\x00" * 31)
            )

    def test_c402_tombstone_column_rejects_text(self) -> None:
        """低-3：32 個字元的 TEXT 也過不了墓碑的 CHECK（`typeof` 必須是 blob）。"""
        self.commit(1, [key("w1-tunnel-a")])
        with self.assertRaises(sqlite3.IntegrityError):
            self.trust.db.conn.execute(
                "INSERT INTO tombstones (key_id, key) VALUES (?, ?)", ("text-id", "a" * 32)
            )
        self.assertEqual(self.trust.state().tombstones, ())

    def test_c402_commit_keeps_other_keys_out_of_tombstones(self) -> None:
        """只有 revoked 的 key 進墓碑；有效 key 的公鑰不會被誤記成撤銷。"""
        self.commit(1, [key("w1-tunnel-a"), key("w1-tunnel-old", raw=VALID_B, status="revoked")])
        ids = [row[0] for row in self.trust.db.read("SELECT key_id FROM tombstones")]
        self.assertEqual(ids, ["w1-tunnel-old"])
        self.assertEqual(self.trust.state().tombstones, (("w1-tunnel-old", VALID_B),))

    def test_c402_commit_accepts_registry_without_revoked_keys(self) -> None:
        self.commit(1, [key("w1-tunnel-a")])
        state = self.trust.state()
        self.assertEqual(state.tombstones, ())
        self.assertEqual(state.snapshot_hash, content_hash(registry_bytes(1, keys=[key("w1-tunnel-a")])))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
