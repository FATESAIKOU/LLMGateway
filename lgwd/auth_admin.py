"""管理用程式庫（D5a；契約 C-801～C-809；REPORT-D45-arch §②、§④）。

這是 `llmgwctl auth-*` 背後**唯一**會寫認證庫與名冊的程式庫；CLI 本身（D5b／D5c）
只負責 root／`--confirm`／結束碼與輸出。所有路徑都由 `lgwd.auth_paths.AuthPaths`
推導，時間一律經過注入的 `lgwd.clock.Clock`。

安全邊界（本模組的核心）：

- **寫入一律先取 store flock**（`auth-store.lock` 的 `LOCK_EX|LOCK_NB`）。lgwd 在
  warn／enforce 執行時持有同一把鎖，所以這裡的每個寫入函式在 lgwd 運行時都會以
  `lock_busy`（結束碼 2）失敗，庫完全不變（W-D5）；反過來，本模組持鎖時 lgwd 取不
  到鎖（W-D6，lgwd 端對應結束碼 78 由 D2 的 `main` 實作）。**權限收尾在鎖內**：
  `store_write_lock` 在 `_drop_empty_sidecars` 之後、放鎖之前，不論成功或失敗都跑
  `finalize_permissions`（中-2），所以 lgwd 不會在「庫／鎖檔還沒 chown、chmod」的
  窗口啟動，失敗路徑也不會留下 root 擁有的檔案（W-D9）。
- **回退防護**：`reset_generation` 在**任何寫入之前**照 reload 的規則驗墓碑
  （`auth_registry.check_tombstones`），不通過就什麼都不改（W-D8）；通過之後才寫
  名冊檔，再在信任庫的**同一個 transaction** 內換掉世代水位**與**已驗證快照並補
  墓碑（`TrustStore.reset_to`；`commit` 的世代守門不動）。`recover_replay` 只重建
  replay 庫，墓碑與水位不動，並把每把 key 的 `H` 設為 `now＋60`（W-D11）。
  `rebuild_trust` 是唯一允許「回退防護已失效」的例外路徑：它一定在輸出、審計與
  回傳值裡明示，而且**只給信任庫真的損毀／缺失時用**——健康的信任庫、或「部分讀
  得出但舊水位高於名冊世代」一律拒絕（中-3；降世代走 `reset_generation`、換名冊
  走 `install`）。讀得出的舊墓碑與名冊衝突時也拒絕（不讓已撤銷 key 復活）。
- **唯讀一律 `mode=ro`、不建檔、不取 store 鎖**：`inspect_*` 不以 `rw` 開庫，缺檔
  就是 `store_missing`（W-D10）；也**不參與鎖**（中-1），所以一個狀態查詢不會讓
  同時啟動的 lgwd 拿到 `LockBusy` 而以 78 結束。SQLite 讀 WAL 庫時自己建的
  `-wal`／`-shm`（`-wal` 是 0 byte，不含未落地的交易）不刪，只把擁有者對齊庫檔
  （root 才做得到）；之後任何寫入指令在持鎖時由 `_drop_empty_sidecars` 順手清掉。
- **審計**：`AuditLog` 一行一筆 JSON、`root:root 0600`、`O_APPEND|O_CREAT|
  O_NOFOLLOW`；只記誰、何時、哪個指令、去敏的參數與新舊**摘要**（世代、雜湊、key
  ID、`H`），不記公鑰全文與簽章。每個寫入函式都先寫審計才動庫。
- **不洩密**：輸出與例外訊息不含公鑰全文、簽章或 nonce；fingerprint 的計算仍只在
  `auth_registry.fingerprint` 一份。

只用標準函式庫；不 import `http`／`main`／`registry`，也不碰 `allowed_signers.*`
（那是 lgwd 的產物，見 REPORT-D45-arch §④）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import quote

from lgwd.auth_db import (
    IDENTITY_BYTES,
    REPLAY_SCHEMA,
    TRUST_SCHEMA,
    AuthDb,
    LockBusy,
    LockUnavailable,
    StoreBusy,
    StoreExists,
    StoreMissing,
    StoreUnavailable,
    acquire_store_lock,
    classify_message,
    create_store,
    error_from,
    open_rw,
)
from lgwd.auth_paths import AuthPaths, now_seconds, sqlite_sidecars
from lgwd.auth_registry import (
    KIND_TOMBSTONE_VIOLATION as _KIND_TOMBSTONE_VIOLATION,
    Registry,
    RegistryError,
    check_tombstones,
    content_hash,
    parse_registry,
    read_registry_file,
)
from lgwd.auth_replay import WINDOW_SECONDS
from lgwd.auth_trust import TrustState, TrustStore
from lgwd.clock import Clock

# --- 固定字串（CLI 只依這些分類決定輸出與結束碼） ---

KIND_INVALID_ARGUMENT = "invalid_argument"
KIND_STORE_EXISTS = "store_exists"
KIND_STORE_MISSING = "store_missing"
KIND_STORE_UNAVAILABLE = "store_unavailable"
KIND_TRUST_IDENTITY_MISSING = "trust_identity_missing"
KIND_LOCK_BUSY = "lock_busy"
KIND_LOCK_UNAVAILABLE = "lock_unavailable"
KIND_TOMBSTONE_VIOLATION = _KIND_TOMBSTONE_VIOLATION
KIND_REPLAY_HEALTHY = "replay_store_healthy"
KIND_TRUST_HEALTHY = "trust_store_healthy"
KIND_TRUST_GENERATION_DOWNGRADE = "trust_generation_downgrade"
KIND_KEY_NOT_FOUND = "key_not_found"
KIND_OWNER_FAILED = "owner_failed"
KIND_AUDIT_FAILED = "audit_failed"
KIND_IO_ERROR = "io_error"

#: rebuild-trust／init 的必印警示（design「庫的建立與遺失」）。
ROLLBACK_PROTECTION_LOST = "回退防護已失效，名冊須人工核驗"
#: 同一件事的審計用固定鍵（審計只收去敏的 ASCII 值，不寫中文句子）。
ROLLBACK_PROTECTION_LOST_KEY = "rollback_protection_lost"

#: reset-key-water 的必印代價（design 時鐘事故 (iii)-3）。
RESET_WATER_COST = "故障期間被截存、紀錄已清除的簽章各可再被代送一次"

#: `recover-store` 把每把 key 的 `H` 設成「現在＋60 秒」（= 時窗）。
WATER_AHEAD_SECONDS = WINDOW_SECONDS
#: `reset-key-water` 把 `H` 設成「現在−65 秒」（= 清除門檻）。
WATER_RESET_SECONDS = 65

#: 寫入後庫與鎖檔的權限；擁有者由呼叫端給（root 執行時是 `llmgw:llmgw`）。
STORE_MODE = 0o600
REGISTRY_MODE = 0o640
AUDIT_MODE = 0o600

Owner = tuple[int, int]

#: 審計允許的字串形狀：指令名、key ID、hex 庫識別、`sha256:` 內容雜湊、路徑。
#: 公鑰行（含空白）、任何二進位內容與 240 字元的簽章都過不了（長度上限 200），
#: 避免誤記。
_AUDIT_SCALAR = re.compile(r"[/A-Za-z0-9][/A-Za-z0-9._:@%+=,-]{0,199}\Z")

#: 指令名（含空白，例如 `auth-store init`）另用這個形狀；呼叫端傳的是本模組的常數，
#: 不接受任意字串（公鑰行不以 `auth-` 開頭）。
_AUDIT_COMMAND = re.compile(r"auth-[a-z0-9]+(?: [a-z0-9-]+){0,3}\Z")


class AdminError(Exception):
    """管理操作的固定分類錯誤；`exit_code` 是契約的 1（結果不合格）或 2（無法執行）。

    訊息固定、不含輸入值、公鑰或簽章；`hint` 是給操作者的下一步（也固定）。
    """

    def __init__(self, kind: str, message: str, *, exit_code: int = 1, hint: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.exit_code = int(exit_code)
        self.hint = hint


class AuditValueError(AdminError):
    """審計記錄的值不合白名單（可能是誤把公鑰或簽章放進來）；整個寫入中止。

    它同時是 `AdminError`（`kind=audit_failed`、結束碼 2；低-1）：D5b／D5c 的 CLI 只要
    接 `AdminError` 就接得到，不會丟 traceback；既有的 `except AuditValueError` 也照樣
    成立。審計一律在動庫之前，所以轉成這個型別之後仍然什麼都沒改。
    """

    def __init__(self, detail: str):
        super().__init__(
            KIND_AUDIT_FAILED,
            "審計值不合白名單（可能是公鑰、簽章或二進位內容）",
            exit_code=2,
            hint="審計只記世代、雜湊、key ID 與 H 等去敏摘要",
        )
        self.detail = detail


def _store_admin_error(exc: StoreMissing | StoreBusy | StoreUnavailable, *, exit_code: int,
                       hint: str | None = None) -> AdminError:
    if isinstance(exc, StoreMissing):
        kind = KIND_STORE_MISSING
    elif isinstance(exc, StoreBusy):
        kind = KIND_STORE_UNAVAILABLE
    else:
        kind = KIND_STORE_UNAVAILABLE
    return AdminError(kind, "認證庫不可用", exit_code=exit_code, hint=hint)


# --- 唯讀開庫（mode=ro；不建庫、不建檔） ---

def _uri(path: str, mode: str) -> str:
    """`file:<path>?mode=rw|ro`；編碼規則與 `auth_db._uri` 相同（`?`、`#`、`%`）。"""
    return "file:{0}?mode={1}".format(quote(os.fspath(path), safe="/"), mode)


def _readonly_sidecars(path: str, existed: Mapping[str, bool]) -> None:
    """唯讀開庫後，把**這次才出現的** `-wal`／`-shm` 擁有者對齊庫檔（W-D9；中-1）。

    `mode=ro` 開 WAL 庫時 SQLite 會建立 0 byte `-wal` 與 `-shm`。這裡**不取 store
    flock、也不刪**：

    - 不取鎖：一個唯讀的狀態查詢不得讓同時啟動的 lgwd 拿到 `LockBusy`，以 78 結束
      （那是 `RestartPreventExitStatus=78` 之後會一直停著的事故；REPORT-D45 §4
      「唯讀指令在 lgwd 運行或停止時都只讀，不參與鎖」）。
    - 不刪：0 byte 的 `-wal` 不含任何未落地的交易，對 SQLite 與 lgwd 都無害；之後
      任何拿到 flock 的寫入指令會由 `_drop_empty_sidecars` 順手清掉。

    唯一做的是把新 sidecar 的擁有者對齊庫檔（root 執行時才做得到），避免留下 root
    擁有的 `-wal`／`-shm`。非 root 的 `chown` 失敗靜默略過（與其他權限收尾一致）。
    """
    try:
        info = os.stat(path)
    except OSError:
        return
    for name in sqlite_sidecars(path):
        if existed.get(name, True):
            # 本來就有：那是別的行程（例如運行中的 lgwd）的檔，不動。
            continue
        try:
            side = os.stat(name)
        except OSError:
            continue
        if side.st_uid != info.st_uid or side.st_gid != info.st_gid:
            try:
                os.chown(name, info.st_uid, info.st_gid)
            except OSError:
                pass


@contextlib.contextmanager
def open_readonly(path: str):
    """以 `mode=ro` 開既有庫；缺檔 `StoreMissing`、其他 `StoreUnavailable`（不建檔）。

    **不取 store flock**（中-1）：唯讀指令在 lgwd 運行或停止時都只讀、不參與鎖。
    """
    target = os.fspath(path)
    existed = {name: os.path.exists(name) for name in sqlite_sidecars(target)}
    try:
        conn = sqlite3.connect(
            _uri(target, "ro"), uri=True, isolation_level=None, check_same_thread=False, timeout=0
        )
    except sqlite3.Error as exc:
        if classify_message(str(exc)) == "missing":
            if not os.path.exists(target):
                raise StoreMissing("store file is missing") from None
            raise StoreUnavailable("missing") from None
        raise error_from(exc) from None
    db = AuthDb(conn, path=os.path.abspath(target))
    try:
        yield db
    finally:
        db.close()
        _readonly_sidecars(target, existed)


def _read_trust(paths: AuthPaths) -> tuple[str | None, TrustState]:
    """唯讀讀出信任庫的識別與一致快照（沒有快照＝`TrustState(None, None, None, 墓碑)`）。"""
    with open_readonly(paths.trust_db) as db:
        identity = db.read_identity()
        rows = db.read("SELECT generation, content_hash, raw FROM snapshot WHERE id = 1")
        tombstones = tuple(
            (str(row[0]), bytes(row[1]))
            for row in db.read("SELECT key_id, key FROM tombstones ORDER BY key_id")
        )
    if not rows:
        return identity, TrustState(None, None, None, tombstones)
    generation, stored_hash, raw = rows[0]
    return identity, TrustState(
        generation=int(generation),
        snapshot_raw=bytes(raw),
        snapshot_hash=str(stored_hash),
        tombstones=tombstones,
    )


def _read_replay_identity(paths: AuthPaths) -> str | None:
    with open_readonly(paths.replay_db) as db:
        return db.read_identity()


# --- 權限／檔案工具 ---

def _chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _apply_owner(path: str, owner: Owner, mode: int) -> None:
    """chmod 後 chown；chown 失敗是 `owner_failed`（結束碼 1）。"""
    _chmod(path, mode)
    try:
        os.chown(path, int(owner[0]), int(owner[1]))
    except OSError:
        raise AdminError(
            KIND_OWNER_FAILED,
            "檔案的擁有者無法設成指定帳號",
            exit_code=1,
            hint="檢查是否以 root 執行、以及該帳號是否存在",
        ) from None


def finalize_permissions(paths: AuthPaths, owner: Owner | None, *,
                         extra: Iterable[str] = ()) -> None:
    """把兩庫與鎖檔設成 0600，並在給了 `owner` 時 chown（root 執行時是 `llmgw:llmgw`）。

    `owner=None` 只 chmod（非 root 的測試與程式庫呼叫端）；chown 失敗是
    `owner_failed`（結束碼 1）——庫已經寫好但擁有者不對，不能靜默回 0。
    """
    for path in (paths.trust_db, paths.replay_db, paths.store_lock, *extra):
        if not os.path.exists(path):
            continue
        _chmod(path, STORE_MODE)
        if owner is None:
            continue
        try:
            os.chown(path, int(owner[0]), int(owner[1]))
        except OSError:
            raise AdminError(
                KIND_OWNER_FAILED,
                "庫的擁有者無法設成指定帳號",
                exit_code=1,
                hint="檢查是否以 root 執行、以及該帳號是否存在",
            ) from None


def _atomic_write(path: str, data: bytes, mode: int, owner: Owner | None = None) -> None:
    """同目錄暫存 → fsync → chmod → rename；失敗不留暫存、不動原檔。"""
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if owner is not None:
            os.chown(tmp, int(owner[0]), int(owner[1]))
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _rename_with_sidecars(path: str, target: str) -> None:
    """連同 `-wal`／`-shm` 一起改名（事故檔留作分析，不得拿來還原）。"""
    for src, dst in zip((path, *sqlite_sidecars(path)), (target, *sqlite_sidecars(target))):
        if not os.path.exists(src):
            continue
        try:
            os.replace(src, dst)
        except OSError:
            raise AdminError(
                KIND_STORE_UNAVAILABLE, "事故檔無法改名", exit_code=1, hint="檢查目錄權限"
            ) from None


def _remove_store_files(path: str) -> None:
    """刪掉本次建出的庫檔（主檔與 `-wal`／`-shm`）；只給「失敗時清掉本次建出的檔」用。"""
    for name in (path, *sqlite_sidecars(path)):
        try:
            os.unlink(name)
        except OSError:
            pass


def _same_store_path(left: str, right: str) -> bool:
    """字串相同（都還不存在）或 `samefile`（硬連結／同一路徑已存在）都算同一個檔。"""
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _create_store_pair(trust_path: str, replay_path: str) -> str:
    """配對建信任庫與防重放庫：同一個庫識別、失敗只清掉本次建出的檔。

    **單庫的建庫邏輯只有一份**（中-4）：`O_EXCL`、schema、`meta.identity`／
    `meta.schema`、0600 與部分建檔的清理都在 `auth_db.create_store`；這裡只負責配對
    （同一識別、兩條路徑不得相同、本次建出的檔在失敗時清掉）。`auth_db` 另有兩庫版本
    （給測試與其他地方用），但 PR-3 的 C-301 靜態掃描（既有測試、本輪不改）以文字禁止
    其他 `lgwd/*.py` 提到那個名字，所以這裡不呼叫它；兩份的差異只剩這 20 行編排。
    """
    trust = os.path.abspath(os.fspath(trust_path))
    replay = os.path.abspath(os.fspath(replay_path))
    if _same_store_path(trust, replay):
        raise AdminError(KIND_STORE_EXISTS, "兩條路徑指到同一個檔", exit_code=1)
    identity = os.urandom(IDENTITY_BYTES).hex()
    created: list[str] = []
    try:
        for path, schema in ((trust, TRUST_SCHEMA), (replay, REPLAY_SCHEMA)):
            create_store(path, schema, identity)
            created.append(path)
    except BaseException:
        for path in created:
            _remove_store_files(path)
        raise
    return identity


def _drop_empty_sidecars(paths: AuthPaths) -> None:
    """收拾空 `-wal` 與其 `-shm`（呼叫端必須持有 store flock，且沒有自己的連線還開著）。

    `mode=ro` 讀 WAL 庫時 SQLite 會留下 0 byte 的 `-wal` 與 `-shm`（唯讀路徑不刪，
    見 `_readonly_sidecars`）；它們不含任何未落地的交易（`-wal` 是空的），所以在
    獨佔鎖下刪掉是安全的，也滿足 W-D9「不留 root 擁有的 `-wal`／`-shm`」。`-wal` 非
    空時一律不動（可能有真的資料）。
    """
    for path in (paths.trust_db, paths.replay_db):
        wal, shm = sqlite_sidecars(path)
        try:
            if os.path.exists(wal):
                if os.path.getsize(wal) != 0:
                    continue
                os.unlink(wal)
            if os.path.exists(shm):
                os.unlink(shm)
        except OSError:
            continue


@contextlib.contextmanager
def store_write_lock(paths: AuthPaths, owner: Owner | None = None, *,
                     extra: Iterable[str] = ()):
    """取 `auth-store.lock` 的 `LOCK_EX|LOCK_NB`、做權限收尾，再放鎖（W-D5／W-D6）。

    拿不到（lgwd 正在跑或另一個寫入工具持有）＝`lock_busy`、結束碼 2，庫完全不動；
    鎖檔開不了＝`lock_unavailable`、結束碼 2。鎖檔不存在時會建立（init／recover／
    rebuild 會順便把它修成 0600）。

    **權限收尾在鎖內**（中-2）：`_drop_empty_sidecars` 之後、`release()` 之前，
    **不論成功或失敗**都跑 `finalize_permissions`。這樣 lgwd 不可能在「庫與鎖檔已經
    寫好、卻還是 root 擁有或權限過寬」的窗口啟動（否則會開不了鎖檔／庫而以 78 結束）；
    失敗路徑也照樣收尾，不會永久留下 root 的檔案（W-D9）。失敗路徑若連 chown 都失敗，
    保留原本的例外，並把 `owner_failed` 附在它的 `owner_error` 屬性上（Python 3.10
    沒有 `Exception.add_note`）。
    """
    try:
        handle = acquire_store_lock(paths.store_lock)
    except LockBusy:
        raise AdminError(
            KIND_LOCK_BUSY,
            "lgwd 正在執行或另一個寫入工具持有鎖",
            exit_code=2,
            hint="先 systemctl stop llmgateway，或等另一個寫入工具結束",
        ) from None
    except LockUnavailable:
        raise AdminError(
            KIND_LOCK_UNAVAILABLE,
            "store 鎖檔開不了",
            exit_code=2,
            hint="檢查 auth_state_dir 與鎖檔權限",
        ) from None
    failure: BaseException | None = None
    try:
        yield handle
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            _drop_empty_sidecars(paths)
            try:
                finalize_permissions(paths, owner, extra=extra)
            except AdminError as owner_exc:
                if failure is None:
                    raise
                # 原本的例外優先：把擁有者失敗附在屬性上，不改型別與訊息。
                try:
                    failure.owner_error = owner_exc  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
        finally:
            handle.release()


# --- 審計（C-807） ---

_MISSING = object()


def _redact(value):
    """只允許固定形狀的摘要值；公鑰行、簽章與二進位內容一律拒絕（`AuditValueError`）。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        if not _AUDIT_SCALAR.match(value):
            raise AuditValueError("audit value is not a redacted scalar")
        return value
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _AUDIT_SCALAR.match(key):
                raise AuditValueError("audit mapping key is not a redacted scalar")
            out[key] = _redact(item)
        return out
    raise AuditValueError("audit value must be a redacted scalar")


class AuditLog:
    """附加式審計（`auth-audit.log`；root:root 0600，一行一筆 JSON）。

    欄位固定：`time`（Unix 秒）、`uid`、`sudo_user`、`command`、`args`、`old`、
    `new`。舊／新值只放世代、雜湊、key ID、`H` 與庫識別；`_redact` 會擋下公鑰行
    （含空白）與任何 bytes。寫入是 `O_APPEND` 的一次 `write()`，所以多個行程同時
    附加也不會交錯（單筆遠小於 PIPE_BUF）。
    """

    def __init__(self, path: str, *, clock: Clock, uid: int | None = None,
                 sudo_user: object = _MISSING):
        self.path = os.fspath(path)
        self.clock = clock
        self.uid = os.geteuid() if uid is None else int(uid)
        self._sudo_user = sudo_user

    def _sudo(self) -> str | None:
        if self._sudo_user is _MISSING:
            return os.environ.get("SUDO_USER") or None
        value = self._sudo_user
        return None if value is None else str(value)

    def append(self, command: str, *, args=None, old=None, new=None) -> dict:
        if not isinstance(command, str) or not _AUDIT_COMMAND.match(command):
            raise AuditValueError("audit command is not a known command name")
        record = {
            "time": now_seconds(self.clock),
            "uid": self.uid,
            "sudo_user": _redact(self._sudo()),
            "command": command,
            "args": _redact(args),
            "old": _redact(old),
            "new": _redact(new),
        }
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                AUDIT_MODE,
            )
        except OSError:
            raise AdminError(
                KIND_AUDIT_FAILED,
                "審計檔無法附加",
                exit_code=2,
                hint="檢查 auth_state_dir 與審計檔權限（不得是 symlink）",
            ) from None
        try:
            os.fchmod(fd, AUDIT_MODE)
            os.write(fd, payload)
            os.fsync(fd)
        except OSError:
            raise AdminError(KIND_AUDIT_FAILED, "審計檔寫入失敗", exit_code=2) from None
        finally:
            os.close(fd)
        return record


# --- 結果型別 ---

@dataclass(frozen=True)
class InitResult:
    """`init_stores` 的結果。`identity` 是兩庫共用的 128 位元庫識別（hex）。"""

    identity: str
    warnings: tuple[str, ...] = ()

    @property
    def short_identity(self) -> str:
        return self.identity[:8]


@dataclass(frozen=True)
class StoreInspection:
    """`inspect_stores` 的唯讀結果（不含公鑰；`tombstone_count` 只給計數）。"""

    trust_identity: str | None
    replay_identity: str | None
    replay_present: bool
    identity_match: bool
    trust_generation: int | None
    trust_hash: str | None
    tombstone_count: int


@dataclass(frozen=True)
class ReplayInspection:
    """`inspect_replay` 的唯讀結果：伺服器時間、每 key 的 `H`、非空 `water` 列數。"""

    now: int
    water: Mapping[str, int]
    future_keys: tuple[str, ...]
    nonce_count: int


@dataclass(frozen=True)
class RecoverResult:
    """`recover_replay` 的結果。`identity`＝信任庫的庫識別（新 replay 庫用它）。"""

    identity: str
    action: str            # "created" | "replaced"
    broken_path: str | None
    water: Mapping[str, int]


@dataclass(frozen=True)
class RebuildResult:
    """`rebuild_trust` 的結果；`warning` 一定含 `ROLLBACK_PROTECTION_LOST`。"""

    identity: str
    generation: int
    tombstones: int
    warning: str
    broken_path: str | None


@dataclass(frozen=True)
class ResetGenerationResult:
    """`reset_generation` 的結果（舊世代／舊快照雜湊 → 新世代／新雜湊）。"""

    old_generation: int | None
    old_hash: str | None
    new_generation: int
    new_hash: str
    registry_written: bool


@dataclass(frozen=True)
class WaterChange:
    key_id: str
    old: int | None
    new: int


@dataclass(frozen=True)
class WaterResetResult:
    """`reset_key_water` 的結果；`cost` 一定要印給操作者。"""

    now: int
    changes: tuple[WaterChange, ...]
    cost: str = RESET_WATER_COST


# --- 載入名冊（與 lgwd 同一個 parser／檔案檢查） ---

def _load_registry(path: str, *, owner_uid: int) -> tuple[bytes, Registry, str]:
    try:
        raw = read_registry_file(path, owner_uid=owner_uid)
    except RegistryError as exc:
        raise AdminError(exc.kind, "名冊檔讀取或權限檢查失敗", exit_code=1) from None
    try:
        registry = parse_registry(raw)
    except RegistryError as exc:
        raise AdminError(exc.kind, "名冊解析失敗", exit_code=1) from None
    return raw, registry, content_hash(raw)


# --- C-801：首次建庫 ---

def init_stores(paths: AuthPaths, *, clock: Clock, audit: AuditLog,
                owner: Owner | None = None, owner_uid: int = 0) -> InitResult:
    """`auth-store init` 的程式庫面（C-801）：兩庫都不存在才建，並建鎖檔。

    - 兩庫以 `O_EXCL` 原子建立、同一個庫識別、`meta.schema`；任一已存在就
      `store_exists`（結束碼 1，指向 recover-store／rebuild-trust），本次建出的檔
      會被清掉、既有的檔不動。
    - 鎖檔建立並設 0600（有 `owner` 時一起 chown）。
    - **回退防護警示**：`auth-audit.log` 已有紀錄，或名冊檔的世代大於 1，就回傳
      `ROLLBACK_PROTECTION_LOST` 並寫進審計（只警示、不擋，design「庫的建立與遺失」）。
    - 先寫審計才動庫；成功後再補一筆帶新庫識別的紀錄。
    """
    warnings: list[str] = []
    audit_has_records = False
    try:
        audit_has_records = os.path.getsize(audit.path) > 0
    except OSError:
        pass
    registry_generation: int | None = None
    if os.path.exists(paths.registry):
        try:
            _, registry, _ = _load_registry(paths.registry, owner_uid=owner_uid)
        except AdminError:
            registry_generation = None
        else:
            registry_generation = registry.generation
    if audit_has_records or (registry_generation is not None and registry_generation > 1):
        warnings.append(ROLLBACK_PROTECTION_LOST)

    with store_write_lock(paths, owner):
        audit.append(
            "auth-store init",
            args={"registry_generation": registry_generation},
            old={"audit_log_has_records": audit_has_records},
            new={"warning": ROLLBACK_PROTECTION_LOST_KEY} if warnings else None,
        )
        try:
            identity = _create_store_pair(paths.trust_db, paths.replay_db)
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(
                exc, exit_code=1, hint="檢查 auth_state_dir 與磁碟"
            ) from None
        except (StoreExists, AdminError):
            raise AdminError(
                KIND_STORE_EXISTS,
                "信任庫或防重放庫已存在，不覆寫",
                exit_code=1,
                hint="兩庫都已存在時走 auth-replay recover-store／auth-registry rebuild-trust；"
                     "只缺一個時不要用 init",
            ) from None
        audit.append("auth-store init", new={"identity": identity})
    return InitResult(identity=identity, warnings=tuple(warnings))


# --- C-802：唯讀檢視（mode=ro、不建檔；W-D10） ---

def inspect_stores(paths: AuthPaths) -> StoreInspection:
    """唯讀比對兩庫的庫識別、信任庫的世代／快照雜湊與墓碑數（C-802）。

    信任庫一定是 `mode=ro` 開；缺檔／打不開是 `store_missing`／`store_unavailable`
    （結束碼 2），**不**改用 rw 開、也不建庫。replay 庫缺席不算錯（回
    `replay_present=False`），因為它正是 recover-store 要修的東西。
    """
    try:
        trust_identity, state = _read_trust(paths)
    except (StoreMissing, StoreUnavailable) as exc:
        raise _store_admin_error(
            exc, exit_code=2, hint="檢查該庫是否存在與權限；缺 replay 走 recover-store，缺信任庫走 rebuild-trust"
        ) from None
    replay_present = True
    try:
        replay_identity = _read_replay_identity(paths)
    except StoreMissing:
        replay_present, replay_identity = False, None
    except StoreUnavailable as exc:
        raise _store_admin_error(exc, exit_code=2, hint="檢查 replay 庫是否存在與權限") from None
    return StoreInspection(
        trust_identity=trust_identity,
        replay_identity=replay_identity,
        replay_present=replay_present,
        identity_match=(
            replay_present
            and trust_identity is not None
            and trust_identity == replay_identity
        ),
        trust_generation=state.generation,
        trust_hash=state.snapshot_hash,
        tombstone_count=len(state.tombstones),
    )


def inspect_replay(paths: AuthPaths, *, clock: Clock) -> ReplayInspection:
    """`auth-replay status` 的程式庫面（C-802）：`H`、未來 `H` 的 key、nonce 列數。

    一律 `mode=ro`；讀不到就是 `store_missing`／`store_unavailable`（結束碼 2），
    不回退成 rw（W-D10）。不印 nonce，也不印公鑰。
    """
    now = now_seconds(clock)
    try:
        with open_readonly(paths.replay_db) as db:
            water = {
                str(row[0]): int(row[1])
                for row in db.read("SELECT key_id, h FROM water ORDER BY key_id")
            }
            rows = db.read("SELECT COUNT(*) FROM nonces")
            nonce_count = int(rows[0][0])
    except (StoreMissing, StoreUnavailable) as exc:
        raise _store_admin_error(
            exc, exit_code=2, hint="replay 庫缺失或打不開：確認不是權限問題後走 recover-store"
        ) from None
    future = tuple(sorted(key for key, h in water.items() if h > now + WATER_AHEAD_SECONDS))
    return ReplayInspection(now=now, water=water, future_keys=future, nonce_count=nonce_count)


# --- C-803：只重建 replay 庫 ---

def recover_replay(paths: AuthPaths, *, clock: Clock, audit: AuditLog,
                   owner: Owner | None = None) -> RecoverResult:
    """`auth-replay recover-store`（C-803；design 時鐘事故 (v)）。

    只重建 `auth-replay.sqlite`：

    - 信任庫必須開得了、庫識別讀得出、快照解析得出來；否則 `store_missing`／
      `store_unavailable`（結束碼 1，指向 rebuild-trust），什麼都不改。
    - 既有 replay 庫打不開、損毀或識別不符時，連同 `-wal`／`-shm` 改名為
      `<檔名>.broken-<整數秒>`（`AuthPaths.broken_path`，留作分析、不得還原），
      再建新庫；既有的庫**健康且識別相同**時拒絕（`replay_store_healthy`），不白白
      清掉落地的 nonce。
    - 新庫寫入信任庫的庫識別，並把信任庫快照內**每一把 key（含 revoked）**的 `H`
      設為 `now ＋ 60`（= 時窗）；墓碑與世代水位完全不動（W-D11）。
    """
    with store_write_lock(paths, owner):
        try:
            trust_identity, trust_state = _read_trust(paths)
        except (StoreMissing, StoreUnavailable) as exc:
            raise _store_admin_error(
                exc, exit_code=1, hint="先跑 auth-registry rebuild-trust 重建信任庫"
            ) from None
        if trust_identity is None:
            raise AdminError(
                KIND_TRUST_IDENTITY_MISSING,
                "信任庫沒有庫識別",
                exit_code=1,
                hint="先跑 auth-registry rebuild-trust",
            )
        if trust_state.has_snapshot:
            try:
                registry = parse_registry(trust_state.snapshot_raw)
            except RegistryError:
                raise AdminError(
                    KIND_STORE_UNAVAILABLE,
                    "信任庫快照無法解析",
                    exit_code=1,
                    hint="先跑 auth-registry rebuild-trust",
                ) from None
            keys = tuple(key.key_id for key in registry.keys)
        else:
            keys = ()
        now = now_seconds(clock)
        water = {key_id: now + WATER_AHEAD_SECONDS for key_id in keys}

        action = "created"
        broken: str | None = None
        existing_identity = None
        replay_present = os.path.exists(paths.replay_db) or any(
            os.path.exists(name) for name in sqlite_sidecars(paths.replay_db)
        )
        if replay_present:
            try:
                existing_identity = _read_replay_identity(paths)
            except (StoreMissing, StoreUnavailable):
                existing_identity = None
            if existing_identity == trust_identity:
                raise AdminError(
                    KIND_REPLAY_HEALTHY,
                    "replay 庫可讀且識別與信任庫一致，不需 recover-store",
                    exit_code=1,
                    hint="真的遺失過才重建；健康的庫重建等於丟掉 nonce 與 H",
                )
            audit.append(
                "auth-replay recover-store",
                old={"replay_identity": existing_identity},
                new={"replay_identity": trust_identity, "water": water},
            )
            broken = paths.broken_path(paths.replay_db, clock)
            _rename_with_sidecars(paths.replay_db, broken)
            action = "replaced"
        else:
            audit.append(
                "auth-replay recover-store",
                old={"replay_identity": None},
                new={"replay_identity": trust_identity, "water": water},
            )
        try:
            create_store(paths.replay_db, REPLAY_SCHEMA, trust_identity)
        except StoreExists:
            raise AdminError(
                KIND_STORE_EXISTS,
                "replay 庫在重建過程中已被建立",
                exit_code=1,
                hint="先確認沒有另一個寫入工具；重跑前先看 auth-replay status",
            ) from None
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(
                exc, exit_code=1, hint="新 replay 庫無法建立：檢查 auth_state_dir 與磁碟"
            ) from None
        try:
            db = open_rw(paths.replay_db)
            try:
                with db.txn() as cur:
                    for key_id, h in water.items():
                        cur.execute(
                            "INSERT INTO water (key_id, h) VALUES (?, ?)"
                            " ON CONFLICT(key_id) DO UPDATE SET h = excluded.h",
                            (key_id, h),
                        )
            finally:
                db.close()
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(exc, exit_code=1, hint="新 replay 庫寫不進去：檢查磁碟") from None
    return RecoverResult(identity=trust_identity, action=action, broken_path=broken, water=water)


# --- C-804：重建信任庫（回退防護失效的例外路徑） ---

def _read_trust_for_rebuild(
    paths: AuthPaths,
) -> tuple[bool, str | None, int | None, str | None, tuple[tuple[str, bytes], ...]]:
    """唯讀讀舊信任庫給 rebuild-trust 判斷：`(healthy, identity, generation, hash, 墓碑)`。

    `healthy` 的定義（中-3）：庫識別讀得出、快照列讀得出而且 `parse_registry` 過得了、
    快照的內容雜湊與存下的一致、墓碑讀得出。庫完全讀不出（缺檔／損毀）＝
    `(False, None, None, None, ())`；部分壞掉（例如快照壞了）仍盡量回水位與墓碑，
    給「降世代」判斷與墓碑搬移用。

    全程 `mode=ro`、不取 store flock（呼叫端已持鎖，這裡只是讀同一把鎖底下的檔）。
    """
    try:
        with open_readonly(paths.trust_db) as db:
            identity = db.read_identity()
            rows = db.read("SELECT generation, content_hash, raw FROM snapshot WHERE id = 1")
            tombstones = tuple(
                (str(row[0]), bytes(row[1]))
                for row in db.read("SELECT key_id, key FROM tombstones ORDER BY key_id")
            )
    except (StoreMissing, StoreUnavailable):
        return False, None, None, None, ()
    if not rows:
        return False, identity, None, None, tombstones
    generation: int | None = None
    try:
        generation = int(rows[0][0])
    except (TypeError, ValueError):
        generation = None
    stored_hash = None if rows[0][1] is None else str(rows[0][1])
    healthy = False
    if identity is not None and generation is not None:
        try:
            raw = bytes(rows[0][2])
        except (TypeError, ValueError):
            raw = None
        if raw is not None:
            try:
                parse_registry(raw)
            except (RegistryError, ValueError):
                healthy = False
            else:
                healthy = content_hash(raw) == stored_hash
    return healthy, identity, generation, stored_hash, tombstones


def rebuild_trust(paths: AuthPaths, registry_path: str, *, clock: Clock, audit: AuditLog,
                  owner: Owner | None = None, owner_uid: int = 0) -> RebuildResult:
    """`auth-registry rebuild-trust`（C-804；design「信任庫與防重放庫分開」）。

    - 名冊必須通過 `auth_registry.parse_registry`（與 lgwd 同一個 parser／檔案檢查）；
      不合法就什麼都不改。
    - **只給信任庫真的損毀／缺失時用**（中-3）：舊信任庫健康（識別、快照、墓碑都讀
      得出且快照完整）就拒絕（`trust_store_healthy`，結束碼 1），要降世代走
      `reset-generation`、要換名冊走 `install`；只能部分讀出、但舊世代水位高於名冊
      世代時也拒絕（`trust_generation_downgrade`），避免同一步同時「重建」又「降
      世代」而繞過防回退。
    - 舊信任庫讀得出的部分先搬出墓碑與世代水位（**只搬得出多少算多少**）；搬得出的
      墓碑必須與名冊一致（`check_tombstones`），否則拒絕（`tombstone_violation`）——
      rebuild 不能讓已撤銷 key 復活。讀不出就以名冊的 `revoked` 列重建墓碑，世代
      用名冊的世代。
    - 舊信任庫改名為 `.broken-<整數秒>`（含 `-wal`／`-shm`），建新庫，寫入快照、
      世代水位與墓碑。庫識別沿用 replay 庫可讀時的識別，否則產生新的（回傳值與輸出
      要提示接著跑 `recover-store`）。
    - 一定回傳並審計 `ROLLBACK_PROTECTION_LOST`（回退防護已失效，名冊須人工核驗）。
    """
    raw, registry, digest = _load_registry(registry_path, owner_uid=owner_uid)
    with store_write_lock(paths, owner):
        healthy, _, old_generation, old_hash, old_tombstones = _read_trust_for_rebuild(paths)
        if healthy:
            raise AdminError(
                KIND_TRUST_HEALTHY,
                "舊信任庫仍可讀、快照也完整，不需要 rebuild-trust",
                exit_code=1,
                hint="要降世代用 auth-registry reset-generation；要換名冊用 auth-registry install",
            )
        if old_generation is not None and old_generation > registry.generation:
            raise AdminError(
                KIND_TRUST_GENERATION_DOWNGRADE,
                "舊信任庫的世代水位高於這份名冊：rebuild 不得同時降世代",
                exit_code=1,
                hint="先用世代不低於 {0} 的名冊重建，再跑 reset-generation".format(old_generation),
            )
        tombstones: dict[str, bytes] = dict(old_tombstones)
        try:
            check_tombstones(registry, list(tombstones.items()))
        except RegistryError:
            raise AdminError(
                KIND_TOMBSTONE_VIOLATION,
                "舊信任庫的墓碑與這份名冊衝突（不得讓已撤銷 key 復活）",
                exit_code=1,
                hint="改用包含這些 revoked 列的名冊，或另開工單人工裁定",
            ) from None
        for key in registry.keys:
            if key.status == "revoked":
                tombstones.setdefault(key.key_id, key.public_key)

        try:
            replay_identity = _read_replay_identity(paths)
        except (StoreMissing, StoreUnavailable):
            replay_identity = None
        identity = replay_identity or os.urandom(IDENTITY_BYTES).hex()

        audit.append(
            "auth-registry rebuild-trust",
            args={"registry": os.path.abspath(os.fspath(registry_path))},
            old={
                "generation": old_generation,
                "snapshot_hash": old_hash,
                "tombstones": sorted(tombstones),
            },
            new={
                "generation": registry.generation,
                "snapshot_hash": digest,
                "identity": identity,
                "warning": ROLLBACK_PROTECTION_LOST_KEY,
            },
        )
        broken: str | None = None
        trust_present = os.path.exists(paths.trust_db) or any(
            os.path.exists(name) for name in sqlite_sidecars(paths.trust_db)
        )
        if trust_present:
            # 主檔不在、只剩 `-wal`／`-shm` 時也一起改名留證（審查 §6 低-3）。
            broken = paths.broken_path(paths.trust_db, clock)
            _rename_with_sidecars(paths.trust_db, broken)
        try:
            create_store(paths.trust_db, TRUST_SCHEMA, identity)
        except StoreExists:
            raise AdminError(
                KIND_STORE_EXISTS,
                "信任庫在重建過程中已被建立",
                exit_code=1,
                hint="先確認沒有另一個寫入工具；重跑前先看 auth-store inspect",
            ) from None
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(
                exc, exit_code=1, hint="新信任庫無法建立：檢查 auth_state_dir 與磁碟"
            ) from None
        try:
            db = open_rw(paths.trust_db)
            try:
                with db.txn() as cur:
                    for key_id, public_key in sorted(tombstones.items()):
                        cur.execute(
                            "INSERT OR IGNORE INTO tombstones (key_id, key) VALUES (?, ?)",
                            (key_id, public_key),
                        )
                    cur.execute(
                        "INSERT INTO snapshot (id, generation, content_hash, raw)"
                        " VALUES (1, ?, ?, ?)",
                        (registry.generation, digest, raw),
                    )
                    cur.execute(
                        "INSERT INTO generation (id, generation) VALUES (1, ?)",
                        (registry.generation,),
                    )
            finally:
                db.close()
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(
                exc, exit_code=1, hint="新信任庫寫不進去：檢查磁碟"
            ) from None
    return RebuildResult(
        identity=identity,
        generation=registry.generation,
        tombstones=len(tombstones),
        warning=ROLLBACK_PROTECTION_LOST,
        broken_path=broken,
    )


# --- C-805：reset-generation（同一個 transaction 換水位＋快照） ---

def reset_generation(paths: AuthPaths, registry_path: str, *, clock: Clock, audit: AuditLog,
                     owner: Owner | None = None, owner_uid: int = 0,
                     registry_owner: Owner | None = None) -> ResetGenerationResult:
    """`auth-registry reset-generation`（C-805；design「transaction 與恢復」）。

    順序固定：解析名冊 → 照 reload 規則驗墓碑（**不通過就什麼都不改**，W-D8）→
    寫審計（舊水位、舊快照雜湊）→ 原子寫名冊檔（內容已相同就跳過）→ 在信任庫的
    **同一個 transaction** 內把世代水位、已驗證快照（內容、雜湊）一起換掉並補墓碑
    （`TrustStore.reset_to`；墓碑只增不減，不能讓已撤銷 key 復活）。

    寫完名冊檔、transaction 之前當機（W-D7）：名冊已是新世代、信任庫仍是舊的，
    lgwd 重啟會拒絕較低世代並沿用舊快照；重跑同一指令即成功（冪等）。
    """
    raw, registry, digest = _load_registry(registry_path, owner_uid=owner_uid)
    target = os.path.abspath(os.fspath(paths.registry))
    source = os.path.abspath(os.fspath(registry_path))
    with store_write_lock(paths, owner):
        try:
            trust = TrustStore(paths.trust_db)
        except StoreMissing:
            raise AdminError(
                KIND_STORE_MISSING,
                "信任庫不存在",
                exit_code=1,
                hint="先跑 auth-store init 或 auth-registry rebuild-trust",
            ) from None
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(exc, exit_code=1) from None
        try:
            state = trust.state()
            try:
                check_tombstones(registry, state.tombstones)
            except RegistryError:
                raise AdminError(
                    KIND_TOMBSTONE_VIOLATION,
                    "名冊違反撤銷墓碑，什麼都沒有改",
                    exit_code=1,
                    hint="名冊必須保留每一筆已撤銷的 key（同 ID、同公鑰、status=revoked）",
                ) from None
            old_generation, old_hash = state.generation, state.snapshot_hash
            audit.append(
                "auth-registry reset-generation",
                args={"registry": source},
                old={"generation": old_generation, "snapshot_hash": old_hash},
                new={"generation": registry.generation, "snapshot_hash": digest},
            )
            registry_written = False
            try:
                if source != target:
                    _atomic_write(target, raw, REGISTRY_MODE, registry_owner)
                    registry_written = True
                elif registry_owner is not None:
                    _apply_owner(target, registry_owner, REGISTRY_MODE)
            except OSError:
                raise AdminError(
                    KIND_IO_ERROR,
                    "名冊檔無法原子寫入",
                    exit_code=1,
                    hint="檢查名冊路徑的目錄權限與磁碟",
                ) from None
            try:
                trust.reset_to(registry, raw, digest)
            except (StoreBusy, StoreUnavailable) as exc:
                raise _store_admin_error(
                    exc, exit_code=1, hint="信任庫交易失敗：檢查磁碟與權限"
                ) from None
        finally:
            trust.close()
    return ResetGenerationResult(
        old_generation=old_generation,
        old_hash=old_hash,
        new_generation=registry.generation,
        new_hash=digest,
        registry_written=registry_written,
    )


# --- C-806：reset-key-water ---

def reset_key_water(paths: AuthPaths, *, clock: Clock, audit: AuditLog,
                    keys: Iterable[str] | None = None, all_future: bool = False,
                    owner: Owner | None = None) -> WaterResetResult:
    """`auth-replay reset-key-water`（C-806；design 時鐘事故 (iii)-3／(iv)）。

    `keys`（一批 key ID）或 `all_future=True`（所有 `H > now＋60` 的 key）擇一；
    兩個都給或都不給是 `invalid_argument`（結束碼 2）。先把伺服器時間與各 key 的
    舊 `H` 放進回傳值與審計（呼叫端據此在動手前印給操作者確認時鐘），再把 `H`
    設為 `now − 65`。找不到的 key 是 `key_not_found`（結束碼 1），什麼都不改。

    代價（`RESET_WATER_COST`）一定要印：故障期間被截存、紀錄已清除的簽章各可再
    被代送一次。
    """
    if (keys is None) == (not all_future):
        raise AdminError(
            KIND_INVALID_ARGUMENT,
            "必須剛好指定 --key 或 --all-future 其中一個",
            exit_code=2,
        )
    targets = None if keys is None else [str(key) for key in keys]
    now = now_seconds(clock)
    new_h = now - WATER_RESET_SECONDS
    with store_write_lock(paths, owner):
        try:
            db = open_rw(paths.replay_db)
        except StoreMissing:
            raise AdminError(
                KIND_STORE_MISSING,
                "replay 庫不存在",
                exit_code=1,
                hint="先跑 auth-replay recover-store",
            ) from None
        except (StoreBusy, StoreUnavailable) as exc:
            raise _store_admin_error(exc, exit_code=1) from None
        try:
            with db.txn() as cur:
                water = {
                    str(row[0]): int(row[1])
                    for row in cur.execute("SELECT key_id, h FROM water ORDER BY key_id").fetchall()
                }
                if all_future:
                    chosen = [key for key, h in water.items() if h > now + WATER_AHEAD_SECONDS]
                else:
                    chosen = list(targets or ())
                    missing = [key for key in chosen if key not in water]
                    if missing:
                        raise AdminError(
                            KIND_KEY_NOT_FOUND,
                            "指定的 key 沒有 H 列",
                            exit_code=1,
                            hint="先用 auth-replay status 確認 key ID；沒有 H 的 key 不需要重設",
                        )
                changes = tuple(WaterChange(key, water[key], new_h) for key in chosen)
                audit.append(
                    "auth-replay reset-key-water",
                    args={"keys": chosen, "all_future": all_future},
                    old={"now": now, "water": {key: water[key] for key in chosen}},
                    new={"water": {key: new_h for key in chosen}},
                )
                for change in changes:
                    cur.execute(
                        "UPDATE water SET h = ? WHERE key_id = ?", (change.new, change.key_id)
                    )
        finally:
            db.close()
    return WaterResetResult(now=now, changes=changes)


__all__ = [
    "AUDIT_MODE",
    "AdminError",
    "AuditLog",
    "AuditValueError",
    "InitResult",
    "KIND_AUDIT_FAILED",
    "KIND_INVALID_ARGUMENT",
    "KIND_IO_ERROR",
    "KIND_KEY_NOT_FOUND",
    "KIND_LOCK_BUSY",
    "KIND_LOCK_UNAVAILABLE",
    "KIND_OWNER_FAILED",
    "KIND_REPLAY_HEALTHY",
    "KIND_STORE_EXISTS",
    "KIND_STORE_MISSING",
    "KIND_STORE_UNAVAILABLE",
    "KIND_TOMBSTONE_VIOLATION",
    "KIND_TRUST_GENERATION_DOWNGRADE",
    "KIND_TRUST_HEALTHY",
    "KIND_TRUST_IDENTITY_MISSING",
    "REGISTRY_MODE",
    "RESET_WATER_COST",
    "ROLLBACK_PROTECTION_LOST",
    "ROLLBACK_PROTECTION_LOST_KEY",
    "RebuildResult",
    "RecoverResult",
    "ReplayInspection",
    "ResetGenerationResult",
    "STORE_MODE",
    "StoreInspection",
    "WATER_AHEAD_SECONDS",
    "WATER_RESET_SECONDS",
    "WaterChange",
    "WaterResetResult",
    "finalize_permissions",
    "init_stores",
    "inspect_replay",
    "inspect_stores",
    "open_readonly",
    "rebuild_trust",
    "recover_replay",
    "reset_generation",
    "reset_key_water",
    "store_write_lock",
]
