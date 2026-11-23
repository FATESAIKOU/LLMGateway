"""lgwd/auth_db.py 的單元測試（D2 PR-3，契約 C-301～C-304、C-309）。

全部用暫存 SQLite（`tempfile`）、FakeClock 與假資料；不碰 /var/lib/llmgateway、
不讀憑證、不連真機。故障注入用真實觸發（`PRAGMA max_page_count`、改壞檔頭、
另一條連線持寫鎖、唯讀連線），不只餵假字串。
"""

from __future__ import annotations

import ast
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest

from lgwd.auth_db import (
    DEFAULT_BUSY_TIMEOUT,
    IDENTITY_BYTES,
    AuthDb,
    LockBusy,
    LockUnavailable,
    StoreBusy,
    StoreExists,
    StoreMissing,
    StoreUnavailable,
    acquire_store_lock,
    classify_message,
    create_stores,
    open_rw,
)
from lgwd.auth_paths import auth_paths

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTH_MODULES = (
    os.path.join(REPO, "lgwd", "auth_db.py"),
    os.path.join(REPO, "lgwd", "auth_replay.py"),
    os.path.join(REPO, "lgwd", "auth_paths.py"),
)


def _first_arg_is_restricted(node: ast.Call) -> bool:
    """`sqlite3.connect()` 的第一個參數是否被限制成 `mode=rw`／`mode=ro`。

    允許兩種寫法：字串常值直接含 `?mode=rw|ro`，或呼叫同模組的 `_uri(path, "rw"|"ro")`。
    """
    if not node.args:
        return False
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return "?mode=rw" in first.value or "?mode=ro" in first.value
    if isinstance(first, ast.Call) and isinstance(first.func, ast.Name) and first.func.id == "_uri":
        return (
            len(first.args) >= 2
            and isinstance(first.args[1], ast.Constant)
            and first.args[1].value in ("rw", "ro")
            and _has_uri_keyword(node)
        )
    return False


def _has_uri_keyword(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "uri" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
            return True
    return False


class _ConnectScan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.calls: list[tuple[tuple[str, ...], ast.Call]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node) -> None:  # pragma: no cover - 目前沒有 async
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_connect = (
            isinstance(func, ast.Attribute) and func.attr == "connect"
        ) or (isinstance(func, ast.Name) and func.id == "connect")
        if is_connect:
            self.calls.append((tuple(self.stack), node))
        self.generic_visit(node)


class _RaisingCursor:
    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database went sideways in a brand new way")

    def close(self) -> None:
        pass


class _RaisingConnection:
    """假連線：只為了驗「表外訊息＝未知故障」。"""

    def __init__(self) -> None:
        self.isolation_level = None
        self.row_factory = None

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database went sideways in a brand new way")

    def cursor(self):
        return _RaisingCursor()

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class AuthDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, mode=0o700)
        self.paths = auth_paths(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stores(self) -> str:
        return create_stores(self.paths.trust_db, self.paths.replay_db)

    def _open(self, **kwargs) -> AuthDb:
        self._stores()
        return open_rw(self.paths.replay_db, **kwargs)


class OpenRwTests(AuthDbTestCase):
    """C-301：不建庫。"""

    def test_c301_open_rw_missing_raises_and_leaves_no_file(self) -> None:
        """C-301：缺檔丟 StoreMissing，呼叫後檔案（含 -wal／-shm）仍然不存在。"""
        target = os.path.join(self.state_dir, "auth-replay.sqlite")
        with self.assertRaises(StoreMissing):
            open_rw(target)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + "-wal"))
        self.assertFalse(os.path.exists(target + "-shm"))

    def test_c301_open_rw_reads_existing_store(self) -> None:
        """C-301：既有庫照 mode=rw 開得起來，識別讀得回來。"""
        identity = self._stores()
        db = open_rw(self.paths.replay_db)
        try:
            self.assertEqual(db.read_identity(), identity)
        finally:
            db.close()

    def test_c301_existing_but_unreadable_store_is_not_missing(self) -> None:
        """C-301／中-3-2：檔案存在但權限不足（SQLite 同樣回 `unable to open database
        file`）不得報成 `StoreMissing`。"""
        if os.geteuid() == 0:
            self.skipTest("root 不受 0000 權限限制，造不出權限不足（非 root 才驗得到）")
        self._stores()
        os.chmod(self.paths.replay_db, 0o000)
        try:
            with self.assertRaises(StoreUnavailable) as ctx:
                open_rw(self.paths.replay_db)
        finally:
            os.chmod(self.paths.replay_db, 0o600)
        self.assertEqual(ctx.exception.kind, "missing")
        self.assertNotIsInstance(ctx.exception, StoreMissing)

    def test_c301_unenterable_directory_is_not_missing(self) -> None:
        """C-301／中-3-2：父目錄不可進入（存在性都看不出來）也不得報成 `StoreMissing`。"""
        if os.geteuid() == 0:
            self.skipTest("root 不受目錄權限限制，造不出不可進入的目錄（非 root 才驗得到）")
        locked = os.path.join(self.state_dir, "locked")
        os.makedirs(locked, mode=0o700)
        target = os.path.join(locked, "auth-replay.sqlite")
        with open(target, "wb"):
            pass
        os.chmod(locked, 0o000)
        try:
            with self.assertRaises(StoreUnavailable) as ctx:
                open_rw(target)
        finally:
            os.chmod(locked, 0o700)
        self.assertEqual(ctx.exception.kind, "missing")
        self.assertNotIsInstance(ctx.exception, StoreMissing)

    def test_c301_modules_never_connect_without_mode(self) -> None:
        """C-301（靜態掃描）：除 create_stores 外，`sqlite3.connect(` 一律帶 mode=rw／mode=ro。"""
        offenders = []
        for path in AUTH_MODULES:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            scan = _ConnectScan()
            scan.visit(tree)
            for stack, call in scan.calls:
                if "create_stores" in stack:
                    continue
                if not (_has_uri_keyword(call) and _first_arg_is_restricted(call)):
                    offenders.append("{0}:{1} ({2})".format(
                        os.path.basename(path), call.lineno, ".".join(stack) or "<module>"))
        self.assertEqual(offenders, [])

    def test_c301_scan_catches_aliased_and_bare_connect(self) -> None:
        """掃描本身的自檢：`import sqlite3 as x`、`from sqlite3 import connect` 都算。"""
        sample = (
            "import sqlite3 as _s3\n"
            "from sqlite3 import connect\n"
            "_s3.connect('/tmp/a')\n"
            "connect('/tmp/b')\n"
            "sqlite3.connect('file:/tmp/c?mode=rw', uri=True)\n"
        )
        scan = _ConnectScan()
        scan.visit(ast.parse(sample))
        self.assertEqual(len(scan.calls), 3)

    def test_c301_lgwd_never_calls_create_stores(self) -> None:
        """C-301：lgwd 永不呼叫 create_stores（只有 auth_db 自己定義、測試與 D5 會呼叫）。"""
        offenders = []
        for name in sorted(os.listdir(os.path.join(REPO, "lgwd"))):
            if not name.endswith(".py") or name == "auth_db.py":
                continue
            with open(os.path.join(REPO, "lgwd", name), encoding="utf-8") as handle:
                if "create_stores" in handle.read():
                    offenders.append(name)
        self.assertEqual(offenders, [])


class ConnectionSettingsTests(AuthDbTestCase):
    """C-302：WAL、synchronous=FULL、isolation_level=None、明確 BEGIN IMMEDIATE。"""

    def test_c302_opens_with_wal_full_and_no_implicit_transactions(self) -> None:
        db = self._open()
        try:
            self.assertIsNone(db.conn.isolation_level)
            self.assertEqual(str(db.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")
            self.assertEqual(int(db.conn.execute("PRAGMA synchronous").fetchone()[0]), 2)
        finally:
            db.close()

    def test_c302_txn_takes_write_lock_immediately(self) -> None:
        """C-302：一進入 `txn()` 就取寫鎖——**還沒寫任何一列**，另一條連線立刻
        `BEGIN IMMEDIATE` 就必須失敗（deferred `BEGIN` 到第一次寫才取鎖，會讓它成功）。"""
        db = self._open()
        other = sqlite3.connect(self.paths.replay_db, isolation_level=None, timeout=0,
                                check_same_thread=False)
        try:
            with db.txn() as cur:
                self.assertTrue(db.conn.in_transaction)
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    other.execute("BEGIN IMMEDIATE")
                self.assertIn("locked", str(ctx.exception).lower())
                cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', 'n', 1)")
            self.assertFalse(db.conn.in_transaction)
            self.assertEqual(db.read("SELECT COUNT(*) FROM nonces")[0][0], 1)
        finally:
            if other.in_transaction:
                other.rollback()
            other.close()
            db.close()

    def test_c302_txn_commits_on_success_and_rolls_back_on_error(self) -> None:
        db = self._open()
        try:
            with db.txn() as cur:
                cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', 'keep', 1)")
            with self.assertRaises(RuntimeError):
                with db.txn() as cur:
                    cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', 'drop', 1)")
                    raise RuntimeError("boom")
            rows = db.read("SELECT nonce FROM nonces ORDER BY nonce")
            self.assertEqual([row[0] for row in rows], ["keep"])
        finally:
            db.close()

    def test_c302_journal_or_sync_mismatch_is_unavailable(self) -> None:
        """C-302：讀回驗證不符（記憶體庫的 journal_mode=memory）就判不可寫，不靜默接受。"""
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        db = AuthDb(conn)
        try:
            with self.assertRaises(StoreUnavailable) as ctx:
                db.configure()
            self.assertIn(ctx.exception.kind, ("io",))
        finally:
            db.close()


class CreateStoresTests(AuthDbTestCase):
    """C-303：兩庫＋同一個 128 位元庫識別；已存在或 samefile 即拒。"""

    def test_c303_writes_same_identity_into_both_stores(self) -> None:
        identity = self._stores()
        self.assertEqual(len(identity), IDENTITY_BYTES * 2)
        int(identity, 16)
        for path in (self.paths.trust_db, self.paths.replay_db):
            db = open_rw(path)
            try:
                self.assertEqual(db.read_identity(), identity)
            finally:
                db.close()

    def test_c303_creates_both_schemas(self) -> None:
        """C-303：兩庫 schema 都建好（replay 的 nonces／water；trust 的 generation／墓碑／快照）。"""
        self._stores()
        expected = {
            self.paths.replay_db: {"meta", "water", "nonces"},
            self.paths.trust_db: {"meta", "generation", "tombstones", "snapshot"},
        }
        for path, tables in expected.items():
            db = open_rw(path)
            try:
                found = {row[0] for row in db.read("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables - found, set(), msg=os.path.basename(path))
            finally:
                db.close()

    def test_c303_refuses_when_either_store_exists(self) -> None:
        create_stores(self.paths.trust_db, self.paths.replay_db)
        with self.assertRaises(StoreExists):
            create_stores(self.paths.trust_db, os.path.join(self.state_dir, "other.sqlite"))
        with self.assertRaises(StoreExists):
            create_stores(os.path.join(self.state_dir, "other-trust.sqlite"), self.paths.replay_db)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "other.sqlite")))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "other-trust.sqlite")))

    def test_c303_refuses_same_path(self) -> None:
        same = os.path.join(self.state_dir, "same.sqlite")
        with self.assertRaises(StoreExists):
            create_stores(same, same)
        self.assertFalse(os.path.exists(same))

    def test_c303_failure_leaves_no_half_pair(self) -> None:
        """C-303：第二個庫建不起來時把第一個庫清掉，不留半套。"""
        missing_dir = os.path.join(self.state_dir, "nope", "auth-replay.sqlite")
        with self.assertRaises(StoreUnavailable):
            create_stores(self.paths.trust_db, missing_dir)
        self.assertFalse(os.path.exists(self.paths.trust_db))
        self.assertFalse(os.path.exists(self.paths.trust_db + "-wal"))

    def test_c303_store_files_are_owner_only(self) -> None:
        self._stores()
        for path in (self.paths.trust_db, self.paths.replay_db):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class ErrorClassificationTests(AuthDbTestCase):
    """C-304：忙碌重試到同一期限；其餘立即 unavailable(kind)；表外＝unknown。"""

    def test_c304_message_table(self) -> None:
        cases = {
            "database is locked": "busy",
            "database table is locked": "busy",
            "database or disk is full": "full",
            "disk I/O error": "io",
            "database disk image is malformed": "corrupt",
            "file is not a database": "corrupt",
            "attempt to write a readonly database": "readonly",
            "unable to open database file": "missing",
            "something sqlite has never said": "unknown",
        }
        for message, expected in cases.items():
            self.assertEqual(classify_message(message), expected, msg=message)
            self.assertEqual(classify_message(message.upper()), expected, msg=message)

    def test_c304_inprocess_lock_waits_until_deadline(self) -> None:
        db = self._open()
        holding = threading.Event()

        def holder() -> None:
            with db.txn():
                holding.set()
                time.sleep(0.4)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(holding.wait(2))
            started = time.monotonic()
            with self.assertRaises(StoreBusy):
                with db.txn(deadline=time.monotonic() + 0.05):
                    pass
            waited = time.monotonic() - started
            self.assertLess(waited, 0.3)
        finally:
            thread.join(2)
            db.close()

    def test_c304_external_write_lock_respects_short_deadline(self) -> None:
        """C-304：外來連線持寫鎖時，短 `deadline` 不得被 SQLite 預設 5 秒 busy timeout
        蓋掉（`open_rw` 以 `timeout=0` 開連線，忙碌只由自家迴圈看期限）。"""
        db = self._open()
        other = sqlite3.connect(self.paths.replay_db, isolation_level=None, timeout=0,
                                check_same_thread=False)
        try:
            other.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaises(StoreBusy):
                with db.txn(deadline=time.monotonic() + 0.2):
                    pass
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.15)
            self.assertLess(elapsed, 1.0)
        finally:
            if other.in_transaction:
                other.rollback()
            other.close()
            db.close()

    def test_c304_write_lock_held_four_seconds_still_succeeds(self) -> None:
        """C-304：另一條連線持寫鎖 4 秒後放掉，必須成功（不是 unavailable）。"""
        db = self._open()
        other = sqlite3.connect(self.paths.replay_db, isolation_level=None, timeout=0,
                                check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")
        release = threading.Timer(4.0, other.rollback)
        release.start()
        started = time.monotonic()
        try:
            with db.txn() as cur:
                cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', 'n', 1)")
            self.assertGreaterEqual(time.monotonic() - started, 3.5)
            self.assertEqual(db.read("SELECT COUNT(*) FROM nonces")[0][0], 1)
        finally:
            release.cancel()
            if other.in_transaction:
                other.rollback()
            other.close()
            db.close()

    def test_c304_write_lock_held_past_deadline_is_busy_timeout(self) -> None:
        """C-304：持寫鎖超過期限（6 秒 > 5 秒）才判 busy_timeout。"""
        db = self._open()
        other = sqlite3.connect(self.paths.replay_db, isolation_level=None, timeout=0,
                                check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")
        release = threading.Timer(6.0, other.rollback)
        release.start()
        started = time.monotonic()
        try:
            with self.assertRaises(StoreBusy):
                with db.txn() as cur:
                    cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', 'n', 1)")
            self.assertGreaterEqual(time.monotonic() - started, DEFAULT_BUSY_TIMEOUT - 0.2)
        finally:
            release.cancel()
            if other.in_transaction:
                other.rollback()
            other.close()
            db.close()

    def test_c304_disk_full_is_unavailable_full(self) -> None:
        """C-304：`PRAGMA max_page_count` 造出真正的 SQLITE_FULL → unavailable(full) 且 rollback。"""
        db = self._open()
        try:
            pages = int(db.conn.execute("PRAGMA page_count").fetchone()[0])
            db.conn.execute("PRAGMA max_page_count = {0}".format(pages))
            with self.assertRaises(StoreUnavailable) as ctx:
                with db.txn() as cur:
                    for index in range(20000):
                        cur.execute(
                            "INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', ?, 1)",
                            ("n{0:06d}".format(index),),
                        )
            self.assertEqual(ctx.exception.kind, "full")
            self.assertEqual(db.read("SELECT COUNT(*) FROM nonces")[0][0], 0)
        finally:
            db.close()

    def test_c304_corrupt_header_is_unavailable_corrupt(self) -> None:
        """C-304：改壞檔頭 → 真的 SQLITE_NOTADB → unavailable(corrupt)。"""
        self._stores()
        with open(self.paths.replay_db, "r+b") as handle:
            handle.write(b"this is not a database file" * 4)
        with self.assertRaises(StoreUnavailable) as ctx:
            open_rw(self.paths.replay_db)
        self.assertEqual(ctx.exception.kind, "corrupt")

    def test_c304_readonly_connection_is_unavailable_readonly(self) -> None:
        """C-304：唯讀連線上寫入 → attempted write to a readonly database → unavailable(readonly)。"""
        self._stores()
        conn = sqlite3.connect(
            "file:{0}?mode=ro".format(self.paths.replay_db), uri=True, isolation_level=None,
            check_same_thread=False,
        )
        db = AuthDb(conn)
        try:
            with self.assertRaises(StoreUnavailable) as ctx:
                with db.txn() as cur:
                    cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES ('k', 'n', 1)")
            self.assertEqual(ctx.exception.kind, "readonly")
        finally:
            db.close()

    def test_c304_unknown_message_is_unavailable_unknown(self) -> None:
        """C-304：表外訊息＝未知故障（不會被當成忙碌重試，也不會被當成驗證失敗）。"""
        db = AuthDb(_RaisingConnection())
        with self.assertRaises(StoreUnavailable) as ctx:
            with db.txn():
                pass
        self.assertEqual(ctx.exception.kind, "unknown")


class StoreLockTests(AuthDbTestCase):
    """C-309：獨佔 flock；被持有＝LockBusy、開不了＝LockUnavailable（型別不同）。"""

    def test_c309_second_acquire_is_lock_busy(self) -> None:
        handle = acquire_store_lock(self.paths.store_lock)
        try:
            with self.assertRaises(LockBusy):
                acquire_store_lock(self.paths.store_lock)
        finally:
            handle.release()
        again = acquire_store_lock(self.paths.store_lock)
        again.release()
        self.assertTrue(again.closed)

    def test_c309_unopenable_lock_is_lock_unavailable(self) -> None:
        missing = os.path.join(self.state_dir, "missing", "auth-store.lock")
        with self.assertRaises(LockUnavailable):
            acquire_store_lock(missing)
        self.assertFalse(issubclass(LockBusy, LockUnavailable))
        self.assertFalse(issubclass(LockUnavailable, LockBusy))

    def test_c309_lock_file_is_owner_only(self) -> None:
        handle = acquire_store_lock(self.paths.store_lock)
        handle.release()
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.store_lock).st_mode), 0o600)

    def test_c309_symlinked_lock_file_is_refused(self) -> None:
        """C-309：鎖檔以 `O_NOFOLLOW` 開，symlink 不跟（root 執行的 D5 CLI 也一樣）。"""
        target = os.path.join(self.state_dir, "real.lock")
        link = os.path.join(self.state_dir, "link.lock")
        with open(target, "w", encoding="utf-8"):
            pass
        os.symlink(target, link)
        with self.assertRaises(LockUnavailable):
            acquire_store_lock(link)

    def test_c309_release_is_idempotent(self) -> None:
        handle = acquire_store_lock(self.paths.store_lock)
        handle.release()
        handle.release()
        with acquire_store_lock(self.paths.store_lock) as locked:
            self.assertFalse(locked.closed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
