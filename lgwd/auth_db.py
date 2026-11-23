"""SQLite 基礎（D2 PR-3，契約 C-301～C-304、C-309；design「防重放交易」「錯誤分類」
「庫的建立與遺失」「信任庫與防重放庫分開」）。

原則：

- **lgwd 永不建庫**：`open_rw` 只用 `file:<path>?mode=rw` URI，缺檔丟 `StoreMissing`
  而且呼叫後檔案仍然不存在（不能用會靜默建空庫的 `sqlite3.connect(path)`）。
  唯一會建庫的是 `create_store`（單庫）與 `create_stores`（兩庫；給測試與 D5 的
  `auth-store init`），lgwd 的守護行程不呼叫它們。
- **每次開啟**都設 `journal_mode=WAL`、`synchronous=FULL` 並讀回驗證，
  `isolation_level=None`；交易一律明確 `BEGIN IMMEDIATE`。
- **單寫者**：一個 `AuthDb` 一條連線一把行程內鎖；忙碌（行程內鎖或
  `SQLITE_BUSY`／`SQLITE_LOCKED`）重試到同一個期限，期限到才算 `busy_timeout`。
- **錯誤分類以訊息字串**（tasks C-304；不用 `sqlite_errorcode`，兼容 Python 3.10）：
  忙碌以外立即 `unavailable(kind)`；表外訊息＝`unknown`。

只用標準函式庫。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from urllib.parse import quote

DEFAULT_BUSY_TIMEOUT = 5.0

IDENTITY_KEY = "identity"
IDENTITY_BYTES = 16  # 128 位元庫識別
SCHEMA_KEY = "schema"
META_SCHEMA_VERSION = 1  # `meta.schema`：之後改表才有依據（低-3-3）


class StoreError(Exception):
    """認證相關 SQLite 錯誤的基底；訊息固定、不含輸入值。"""


class StoreMissing(StoreError):
    """`mode=rw` 開啟時檔案不存在（lgwd 不建庫）。"""


class StoreBusy(StoreError):
    """行程內鎖或 SQLite 忙碌，等滿期限仍拿不到（不是故障，enforce 回 503）。"""


class StoreUnavailable(StoreError):
    """忙碌以外的不可寫（`kind`：full／io／corrupt／readonly／missing／unknown）。"""

    def __init__(self, kind: str):
        super().__init__("store unavailable: {0}".format(kind))
        self.kind = kind


class StoreExists(StoreError):
    """`create_stores` 的目標已存在，或兩條路徑指到同一個檔。"""


class LockBusy(StoreError):
    """`flock` 被別的行程持有（禁止多實例）。"""


class LockUnavailable(StoreError):
    """鎖檔開不了或不能鎖（權限、唯讀檔案系統等）；與 `LockBusy` 型別不同。"""


# --- 錯誤分類（design「錯誤分類」表；以訊息字串，不用 sqlite_errorcode） ---

_BUSY_MESSAGES = ("database is locked", "database table is locked")
_FULL_MESSAGES = ("database or disk is full",)
_IO_MESSAGES = ("disk i/o error",)
_CORRUPT_MESSAGES = ("database disk image is malformed", "file is not a database")
_READONLY_MESSAGES = ("attempt to write a readonly database",)
_MISSING_MESSAGES = ("unable to open database file",)

UNAVAILABLE_KINDS = frozenset({"full", "io", "corrupt", "readonly", "missing", "unknown"})


def classify_message(message: str) -> str:
    """把 SQLite 的錯誤訊息分成 `busy`／`full`／`io`／`corrupt`／`readonly`／`missing`／`unknown`。"""
    text = str(message).lower()
    for kind, needles in (
        ("busy", _BUSY_MESSAGES),
        ("full", _FULL_MESSAGES),
        ("io", _IO_MESSAGES),
        ("corrupt", _CORRUPT_MESSAGES),
        ("readonly", _READONLY_MESSAGES),
        ("missing", _MISSING_MESSAGES),
    ):
        for needle in needles:
            if needle in text:
                return kind
    return "unknown"


def error_from(exc: sqlite3.Error) -> StoreError:
    """把 SQLite 例外轉成我們的型別：忙碌＝`StoreBusy`，其餘＝`StoreUnavailable(kind)`。"""
    kind = classify_message(str(exc))
    if kind == "busy":
        return StoreBusy("store is busy")
    return StoreUnavailable(kind)


def _uri(path: str, mode: str) -> str:
    """`file:<path>?mode=rw|ro`；只做最小百分號編碼（`?`、`#`、`%` 不能原樣進 URI）。"""
    return "file:{0}?mode={1}".format(quote(os.fspath(path), safe="/"), mode)


def _path_is_absent(path: str) -> bool:
    """`unable to open database file` 之後再確認路徑真的不存在（中-3-2）。

    只有 `ENOENT` 算不存在；父目錄不可進入（`EACCES`）等「看不出來」的情況回 False，
    寧可走 `StoreUnavailable("missing")` 讓維運檢查權限／路徑，也不要對可能存在的庫
    叫人跑 `recover-store`／`rebuild-trust`（會讓回退防護失效）。
    """
    try:
        os.lstat(os.fspath(path))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


class AuthDb:
    """一條 SQLite 連線＋一把行程內鎖；交易一律 `BEGIN IMMEDIATE`。

    通常經 `open_rw` 取得；測試可以直接把一條（例如唯讀）連線交給建構子驗錯誤分類。
    """

    def __init__(self, conn: sqlite3.Connection, *, path: str | None = None,
                 busy_timeout: float = DEFAULT_BUSY_TIMEOUT, collect_lock_waits: bool = False):
        self._conn = conn
        self.path = path
        self.busy_timeout = float(busy_timeout)
        self._lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None
        # 觀測用（預設關，不影響正式路徑）：記錄每次交易「等行程內鎖」的秒數，
        # 給並行驗收量「等鎖 p99 < 1 秒」用。
        self.lock_waits: list[float] | None = [] if collect_lock_waits else None

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def configure(self) -> None:
        """設 WAL＋`synchronous=FULL` 並讀回驗證（C-302）。"""
        try:
            row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if row is None or str(row[0]).lower() != "wal":
                raise StoreUnavailable("io")
            self._conn.execute("PRAGMA synchronous=FULL")
            row = self._conn.execute("PRAGMA synchronous").fetchone()
            if row is None or int(row[0]) != 2:
                raise StoreUnavailable("io")
        except sqlite3.Error as exc:
            raise error_from(exc) from None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """唯讀查詢（WAL 下不擋其他行程）；與寫入共用同一把行程內鎖。"""
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise error_from(exc) from None

    def read_identity(self) -> str | None:
        """兩庫共用的 128 位元庫識別（hex）；沒有就 None。"""
        rows = self.read("SELECT value FROM meta WHERE key = ?", (IDENTITY_KEY,))
        if not rows:
            return None
        return str(rows[0][0])

    @contextlib.contextmanager
    def txn(self, deadline: float | None = None) -> Iterator[sqlite3.Cursor]:
        """`BEGIN IMMEDIATE` 交易；成功才 COMMIT，任何例外都 rollback。

        `deadline` 是 `time.monotonic()` 的秒數，預設「現在＋`busy_timeout`」。行程內
        鎖的等待與 `SQLITE_BUSY`／`SQLITE_LOCKED` 的重試共用這同一個期限。
        """
        if deadline is None:
            deadline = time.monotonic() + self.busy_timeout
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise StoreBusy("in-process lock timed out")
        if self.lock_waits is not None:
            waited = time.monotonic() - started
            self.lock_waits.append(waited)
        cur = None
        try:
            cur = self._conn.cursor()
            self._begin(cur, deadline)
            try:
                yield cur
            except sqlite3.Error as exc:
                self._rollback()
                raise error_from(exc) from None
            except BaseException:
                self._rollback()
                raise
            try:
                self._conn.commit()
            except sqlite3.Error as exc:
                self._rollback()
                raise error_from(exc) from None
        finally:
            if cur is not None:
                cur.close()
            self._lock.release()

    def _begin(self, cur: sqlite3.Cursor, deadline: float) -> None:
        """明確 `BEGIN IMMEDIATE`；忙碌就重試到期限，其他錯誤立即分類。"""
        delay = 0.01
        while True:
            try:
                cur.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if classify_message(str(exc)) != "busy":
                    raise error_from(exc) from None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreBusy("store busy until deadline") from None
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.05)

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass


def open_rw(path: str, *, busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
            collect_lock_waits: bool = False) -> AuthDb:
    """以 `mode=rw` 開既有庫；缺檔丟 `StoreMissing`，**不會**把檔案建出來（C-301）。

    SQLite 對「檔案不存在」與「存在但權限不足／目錄不可進入」都回同一句
    `unable to open database file`；判為 missing 時再用 `_path_is_absent` 確認。
    真的不存在才丟 `StoreMissing`；存在（或看不出來）卻打不開丟
    `StoreUnavailable("missing")`，兩者型別不同——PR-6 只對前者叫人跑
    `recover-store`／`rebuild-trust`，後者印「檢查權限／路徑」（誤把權限問題當遺失
    會讓回退防護失效）。
    """
    target = os.fspath(path)
    try:
        conn = sqlite3.connect(_uri(target, "rw"), uri=True, isolation_level=None,
                               check_same_thread=False, timeout=0)
    except sqlite3.Error as exc:
        if classify_message(str(exc)) == "missing":
            if _path_is_absent(target):
                raise StoreMissing("store file is missing") from None
            raise StoreUnavailable("missing") from None
        raise error_from(exc) from None
    db = AuthDb(conn, path=os.path.abspath(target), busy_timeout=busy_timeout,
                collect_lock_waits=collect_lock_waits)
    try:
        db.configure()
    except BaseException:
        conn.close()
        raise
    return db


# --- 兩庫的庫識別（REPORT-D2a 低-3-5；C-303／C-605） ---

def check_store_identity(trust_db: AuthDb, replay_db: AuthDb) -> bool:
    """兩庫的 `meta.identity` 都存在且相同（`True`）；任一缺值或不同（`False`）。

    這是「兩庫是不是同一套」的唯讀判斷，啟動時呼叫一次（`auth.open_runtime` 的第 2
    步）。識別不符無法自動判斷是哪一庫被換過，呼叫端只回「依審計判斷」。

    讀取本身的失敗（`StoreBusy`／`StoreUnavailable`）**照原樣往上丟**，不吞成 `False`：
    「讀不到」與「不同」是兩種處置（前者可能重試，後者要人工判斷），呼叫端要分得出來。
    """
    trust_identity = trust_db.read_identity()
    replay_identity = replay_db.read_identity()
    if trust_identity is None or replay_identity is None:
        return False
    return trust_identity == replay_identity


# --- 建庫（只有測試與 D5 的 auth-store init 用；lgwd 永不呼叫） ---

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

REPLAY_SCHEMA = META_SCHEMA + """
CREATE TABLE IF NOT EXISTS water (
    key_id TEXT PRIMARY KEY,
    h INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nonces (
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (key_id, nonce)
);
CREATE INDEX IF NOT EXISTS nonces_key_ts ON nonces (key_id, ts);
"""

# 信任庫的表格由 PR-4（lgwd/auth_trust.py）使用：世代水位、撤銷墓碑、已驗證快照。
# PR-3 只負責把它們一起建出來，兩庫寫同一個庫識別（C-303）。改動須協調 PR-4。
#
# PR-4 起（C-402、REPORT-D2a 低-3-4／低-3）：
# - 墓碑只增不減，公鑰存驗證後的 32 bytes BLOB（`typeof(key) = 'blob'` ＋
#   `length(key) = 32`），直接餵 `auth_registry.check_tombstones`；32 個字元的 TEXT
#   也過不了。`create_stores` 用 `IF NOT EXISTS`，改動只影響新建的庫（目前沒有部署中的庫）。
# - 世代只有一個來源：`snapshot.generation`；`generation` 表是同一交易內同步的鏡像，
#   只給外部讀者看，lgwd 的判斷不用它。
TRUST_SCHEMA = META_SCHEMA + """
CREATE TABLE IF NOT EXISTS generation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones (
    key_id TEXT PRIMARY KEY,
    key BLOB NOT NULL CHECK (typeof(key) = 'blob' AND length(key) = 32)
);
CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    raw BLOB NOT NULL
);
"""


def _remove_store_files(path: str) -> None:
    """刪掉這個路徑的所有檔案（主檔與 `-wal`／`-shm`）；只給「本次建出的檔」用。"""
    for name in (path, path + "-wal", path + "-shm"):
        try:
            os.unlink(name)
        except OSError:
            pass


def _same_file_paths(trust: str, replay: str) -> bool:
    """字串相同（都還不存在）或 `samefile`（硬連結／同一路徑已存在）都算同一個檔。"""
    if trust == replay:
        return True
    try:
        return os.path.samefile(trust, replay)
    except OSError:
        return False


def _open_error_kind(exc: OSError) -> str:
    """`os.open` 的 OSError 分類（沿用 `UNAVAILABLE_KINDS` 的字串）。"""
    if exc.errno in (errno.ENOENT, errno.EACCES, errno.ENOTDIR, errno.ELOOP):
        return "missing"
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return "full"
    if exc.errno == errno.EIO:
        return "io"
    return "unknown"


def create_store(path: str, schema: str, identity: str) -> None:
    """以 `O_EXCL` 原子建立**一個**庫，寫 schema、庫識別與 `meta.schema`（低-3-3）。

    這是全 repo 唯一一份「建庫」的實作（中-4）：`create_stores` 與 D5 的管理程式庫
    都呼叫它，不再各寫一份 `O_EXCL`／schema／`meta` 的程式碼（分歧就會建出不一致的
    庫）。`lgwd` 的守護行程（`main`／`http`／`auth*`）永不呼叫它。

    `O_EXCL` 讓「已存在」的檢查與建檔是同一個動作，沒有「先檢查、再依 umask 建檔」
    的窗口；已存在（含 symlink）一律 `StoreExists`。建好後才以 `mode=rw` 連上去寫
    schema，所以失敗時呼叫端只知道「本次建出的檔」是哪幾個。
    """
    try:
        fd = os.open(
            os.fspath(path),
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError:
        raise StoreExists("store already exists") from None
    except OSError as exc:
        raise StoreUnavailable(_open_error_kind(exc)) from None
    os.close(fd)
    try:
        try:
            conn = sqlite3.connect(
                _uri(path, "rw"), uri=True, isolation_level=None, check_same_thread=False, timeout=0
            )
        except sqlite3.Error as exc:
            raise error_from(exc) from None
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(schema)
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (IDENTITY_KEY, identity))
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (SCHEMA_KEY, str(META_SCHEMA_VERSION)),
            )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            raise error_from(exc) from None
        finally:
            conn.close()
    except BaseException:
        # 這個檔是本次 `O_EXCL` 建出來的：失敗就只清掉它（含 sidecar），不留半套。
        _remove_store_files(path)
        raise
    os.chmod(path, 0o600)


def create_stores(trust_path: str, replay_path: str) -> str:
    """建信任庫與防重放庫、寫同一個 128 位元隨機庫識別；回傳該識別（hex）。

    任一庫已存在、或兩條路徑指到同一個檔就拒絕。給測試與 D5 的 `auth-store init`
    用；lgwd 本身永不呼叫。兩庫都以 `O_EXCL` 原子建檔（低-3-3）；建立失敗**只清掉
    本次建出的檔**，既有的檔（含另一個行程搶先建好的那一庫）不動。單庫的實作只有
    `create_store` 一份（中-4）。
    """
    trust = os.path.abspath(os.fspath(trust_path))
    replay = os.path.abspath(os.fspath(replay_path))
    if _same_file_paths(trust, replay):
        raise StoreExists("trust and replay stores must be different files")
    identity = os.urandom(IDENTITY_BYTES).hex()
    created: list[str] = []
    try:
        for path, schema in ((trust, TRUST_SCHEMA), (replay, REPLAY_SCHEMA)):
            # 契約允許的例外：只有建庫路徑能建庫（lgwd 守護行程永不呼叫）。
            create_store(path, schema, identity)
            created.append(path)
    except BaseException:
        for path in created:
            _remove_store_files(path)
        raise
    return identity


# --- 鎖檔（C-309：禁止多實例） ---

class LockHandle:
    """`acquire_store_lock` 的回傳值；`release()` 可重複呼叫。"""

    def __init__(self, fd: int, path: str):
        self._fd = fd
        self.path = path

    @property
    def fileno(self) -> int:
        return self._fd

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def release(self) -> None:
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def acquire_store_lock(path: str) -> LockHandle:
    """開鎖檔並取 `flock(LOCK_EX|LOCK_NB)`。

    以 `O_NOFOLLOW|O_CLOEXEC` 開：不跟 symlink（root 執行的 D5 CLI 也一樣）、鎖 fd
    不被子行程繼承。被別人持有（`EWOULDBLOCK`）＝`LockBusy`；開不了檔（權限、唯讀
    檔案系統）＝`LockUnavailable`。兩者型別不同，呼叫端的處理也不同（design「啟動、
    結束碼」）。
    """
    try:
        fd = os.open(os.fspath(path),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise LockUnavailable("store lock cannot be opened") from None
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise LockBusy("store lock is held") from None
        raise LockUnavailable("store lock cannot be locked") from None
    return LockHandle(fd, os.fspath(path))


__all__ = [
    "AuthDb",
    "DEFAULT_BUSY_TIMEOUT",
    "IDENTITY_BYTES",
    "IDENTITY_KEY",
    "LockBusy",
    "LockHandle",
    "LockUnavailable",
    "META_SCHEMA",
    "META_SCHEMA_VERSION",
    "REPLAY_SCHEMA",
    "SCHEMA_KEY",
    "StoreBusy",
    "StoreError",
    "StoreExists",
    "StoreMissing",
    "StoreUnavailable",
    "TRUST_SCHEMA",
    "UNAVAILABLE_KINDS",
    "acquire_store_lock",
    "check_store_identity",
    "classify_message",
    "create_store",
    "create_stores",
    "error_from",
    "open_rw",
]
