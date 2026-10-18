"""遷移：舊的 user 版（v0.1.4）→ 新的 system 版（#35 安裝簡單化設計 §5）。

五個子指令。人手下的只有 ``prepare-migration`` 與 ``cutover``（後者只排一個
分離執行的 transient unit 就結束），其餘照輸出做：

- ``prepare-migration --tag X [--old-home D] [--force]``：真機「準備」。用 install
  的邏輯把新佈局（``/opt``、``/etc``、``/var/lib``、system unit 檔）放好，但
  **不 enable、不 start**；再把舊佈局的 key（``keys.env``、``llm.db``）、隧道與
  API 設定、解密密碼複製過來（``cp -a`` 語意；值不印出、不進 log）。舊的 user
  unit 與舊目錄全程不動，舊檔一律不搬不刪。已存在的目標不覆蓋，除非 ``--force``
  （``keys.pass`` 例外：還沒填值時會從舊的 ``keysync.env`` 補上——不然新服務拿
  不到解密密碼）。
- ``cutover --tag X --revert-after 10m [--old-home D] [--no-remote]``：切換窗口。
  人手下的那一次只做一件事：用 ``systemd-run --unit=lgw-cutover`` 起一個獨立
  的 transient unit（ssh 連線斷了也不中止），自己立刻結束。真正的工作跑在
  ``--inner``（transient unit 帶的參數，人手不用加；它要求 systemd 設的
  ``INVOCATION_ID``）：
  (a) 先排**反向保險** ``systemd-run --unit=lgw-revert --on-active=<revert-after>
  … rollback-to-user``（單元名記到 ``.cutover.state``）；排不起來就不切。
  ``--revert-after`` 有 5 分鐘下限（切換最壞要幾分鐘，太短會在切換中就回滾）；
  (b) 以 ``runuser -u llmgw --``（自帶 XDG_RUNTIME_DIR／DBUS_SESSION_BUS_ADDRESS）
  停舊 user 的服務與隧道，**接著 disable** 它們（觀察期內重開機不得新舊雙跑），
  再用 ``is-active`` 逐一確認真的不跑了；
  (c) enable＋start 兩個新 unit；
  (d) 等最多 60 秒本機 list 200、等隧道 unit active，再經會合點探測（預設
  3 次、間隔 10 秒；``--no-remote`` 略過）；
  (e) 任一不過就立刻回舊版；全過先確認 ``lgw-revert.timer`` 還在（保險是
  一次性 transient，切換拖過 revert-after 會在切換中到點死掉），再寫
  ``pending-confirm`` 並印 ``執行 llmgwctl confirm 取消自動回舊版``。
  preflight（outer 排程前、inner 排保險前）除了版本目錄／keys／tunnel 還
  檢查 gh 已登入與新 tag 的 policy dry-run（壞的在停舊服務前就擋下）。
  注意：``lgw-revert.timer`` 是 transient unit，**重開機後不在**。pending-confirm
  期間重開機時新版會自己起、舊版已 disable（不會雙跑），但保險不會自己回舊版；
  那時要立刻 ``confirm`` 或 ``rollback-to-user``（cutover 的輸出有這行提醒）。
- ``confirm``：先確認反向保險沒有已經開跑、此刻在跑的是新版（舊 user unit 不是
  active），再停掉保險的 timer、狀態改 ``confirmed``（觀察期從這裡起算）。
  狀態不是 ``pending-confirm`` 就拒絕並非零結束。
- ``rollback-to-user [--old-home D]``：回舊版（高-1：**人手一律分離執行**——
  回滾會停掉你正在走的新隧道，直接跑會被 SIGHUP 殺在半路）。人手下的那一次
  只做一件事：``systemd-run --unit=lgw-rollback`` 起獨立 transient unit 自己
  立刻結束（結果看 journal）。真正的工作跑在 ``--inner``（要求 ``INVOCATION_ID``，
  與 cutover 同一套判斷；反向保險到點跑的也是它），順序：先確認四個舊 unit
  檔都在（finalize 後不動新 unit）、停新 service（讓出 8480 埠）、把新的
  ``llm.db`` 複製回舊 state（只在新的真的寫過時；舊的先備份成帶時間戳的
  ``llm.db.bak-*``）、``keys.env`` 只在新的比較新且格式驗得過時才覆蓋、
  enable＋start 舊 user unit（含 key-sync.timer）、探本機 200，**之後才停新
  隧道**（管理連線最後才斷）、停反向保險、狀態寫 ``reverted``。
  用 flock 互斥（unit 裡等最多 5 分鐘，保險到點時切換通常還握著鎖），
  所以反向保險與人工同時觸發也安全。結束碼反映「舊服務有沒有真的回來」
  且中間任何 fail（db 備份失敗等）也會非零。
  狀態是 ``finalized``、或舊 unit 檔不在時，**在停新 unit 之前**就拒絕。
  ``reverted``／``aborted`` 是終態但可以開新一輪：prepare／cutover 會先把
  舊狀態檔歸檔成 ``.cutover.state.<時間戳>`` 再說。
- ``finalize-migration [--old-home D] --yes``：觀察期後收尾（狀態要 ``confirmed``
  且已過 3 天，否則拒絕）。停舊 unit、把舊目錄與舊 unit 檔**搬移**到
  ``<state>/old-user-layout/``（不刪除）、``loginctl disable-linger``。

公開 repo 的規則：輸出、log、狀態檔都不含 key 的值或解密密碼；狀態檔是一行
JSON，只放階段、時間、單元名與代碼。

測試用的外部指令都能換成假指令（``LLMGW_SYSTEMD_RUN``、``LLMGW_RUNUSER``、
``LLMGW_LOGINCTL``；其餘沿用 layout.py 的），等待時間也可調
（``LLMGW_CUTOVER_WAIT``、``LLMGW_KEYSYNC_GRACE``、``LLMGW_OBSERVE_DAYS``）。
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

from . import atomic, check, install, probe, release, units, upgrade
from . import run as runmod
from .layout import (
    DEFAULT_API_PORT,
    KEYSYNC_SHA_NAME,
    OLD_USER_UNIT_REL,
    SERVICE_NAME,
    TAG_PATTERN,
    TAG_RE,
    TUNNEL_SERVICE_NAME,
    Layout,
    parse_env_text,
)
from .report import Report

# -- 固定值 ---------------------------------------------------------------

CUTOVER_UNIT = "lgw-cutover"
REVERT_UNIT = "lgw-revert"
# 人手下的回舊版一律分離執行到這個 unit（高-1：它會停掉操作者正在走的隧道）。
ROLLBACK_UNIT = "lgw-rollback"
REVERT_TIMER_UNIT = REVERT_UNIT + ".timer"
DEFAULT_REVERT_AFTER = "10m"
# 遷移控制檔（狀態、鎖）放 root 專屬目錄，服務帳號（llmgw）寫不到（高-3）：
# 這些欄位會被 root 的指令當輸入用，不能留在 llmgw 擁有的 state 目錄裡。
CTL_DIR = "/var/lib/llmgateway-ctl"
CUTOVER_STATE_NAME = ".cutover.state"
CUTOVER_LOCK_NAME = ".cutover.lock"
ARCHIVE_DIR_NAME = "old-user-layout"

# 舊（v0.1.4；user 層）佈局：相對服務帳號的 HOME。
OLD_SHARE_REL = (".local", "share", "llmgateway")
OLD_CONF_REL = (".config", "llmgateway")
OLD_STATE_REL = (".local", "state", "llmgateway")
OLD_UNIT_REL = OLD_USER_UNIT_REL
OLD_KEYSYNC_ENV_NAME = "keysync.env"
OLD_KEYSYNC_SERVICE = "key-sync.service"
OLD_KEYSYNC_TIMER = "key-sync.timer"

# 停舊 unit 的順序：隧道最後停（它承載管理連線，這樣中斷最短）。
OLD_STOP_ORDER = (SERVICE_NAME, OLD_KEYSYNC_TIMER, OLD_KEYSYNC_SERVICE, TUNNEL_SERVICE_NAME)
# 啟動舊 unit 的順序：先服務與 timer；隧道等新隧道停止讓出埠後才啟動（中-2）。
OLD_START_SERVICES = (SERVICE_NAME, OLD_KEYSYNC_TIMER)
OLD_START_ORDER = (SERVICE_NAME, TUNNEL_SERVICE_NAME, OLD_KEYSYNC_TIMER)
OLD_UNIT_NAMES = (SERVICE_NAME, TUNNEL_SERVICE_NAME, OLD_KEYSYNC_SERVICE, OLD_KEYSYNC_TIMER)

# 狀態檔的階段（字串會寫進 .cutover.state，測試也會看）。
PHASE_CUTOVER = "cutover-started"
PHASE_PENDING = "pending-confirm"
PHASE_CONFIRMED = "confirmed"
PHASE_REVERTED = "reverted"
PHASE_FINALIZED = "finalized"
PHASE_ABORTED = "aborted"

# 進行中／不可重來的階段：不能直接再切（中-4）。終態 reverted／aborted 可以開
# 新一輪（先把舊狀態檔歸檔）。
OPEN_PHASES = (PHASE_PENDING, PHASE_CONFIRMED, PHASE_FINALIZED, PHASE_CUTOVER)

# 狀態檔裡的 unit 名是不可信輸入（服務帳號以前寫得到那個檔）：只接受保險
# 自己的兩個固定值，其他一律不當 systemctl 的參數（高-3）。
STATE_UNIT_NAMES = (REVERT_TIMER_UNIT, REVERT_UNIT + ".service")

# 環境變數（測試用的假指令與可調的等待）。
ENV_SYSTEMD_RUN = "LLMGW_SYSTEMD_RUN"
ENV_LOGINCTL = "LLMGW_LOGINCTL"
ENV_CUTOVER_WAIT = "LLMGW_CUTOVER_WAIT"
ENV_KEYSYNC_GRACE = "LLMGW_KEYSYNC_GRACE"
ENV_OBSERVE_DAYS = "LLMGW_OBSERVE_DAYS"
ENV_TUNNEL_WAIT = "LLMGW_TUNNEL_WAIT"
ENV_REMOTE_TRIES = "LLMGW_REMOTE_TRIES"
ENV_REMOTE_WAIT = "LLMGW_REMOTE_WAIT"
ENV_LOCK_WAIT = "LLMGW_LOCK_WAIT"
ENV_TUNNEL_STABLE = "LLMGW_TUNNEL_STABLE"
# systemd 給每個 service／transient unit 的環境變數：只有它才證明「這是
# 分離執行的那一次」，不是人手在 ssh 上直接跑的（低-6）。
ENV_INVOCATION = "INVOCATION_ID"

# transient unit 真的需要的呼叫端變數（其餘不轉送；中-1）。
TRANSIENT_ENV_NAMES = ("LLMGW_PREFIX", "LLMGW_SERVICE_USER", "LLMGW_SERVICE_HOME")

DEFAULT_CUTOVER_WAIT = 60.0
DEFAULT_KEYSYNC_GRACE = 90.0
DEFAULT_OBSERVE_DAYS = 3.0
DEFAULT_TUNNEL_WAIT = 45.0
DEFAULT_TUNNEL_STABLE = 15.0
DEFAULT_REMOTE_TRIES = 3
DEFAULT_REMOTE_WAIT = 10.0
DEFAULT_LOCK_WAIT = 30.0
# 分離執行（systemd unit 裡）的回舊版可以等久一點：保險到點時切換通常還握著
# 同一把鎖，等 30 秒就放棄＝保險被吃掉（中-2）。切換最壞約 5 分鐘，就等 5 分鐘。
DEFAULT_DETACHED_LOCK_WAIT = 300.0
# 切換最壞要 60 秒本機＋90 秒 key 拉取＋遠端探針；保險比這短會在切換中就
# 開跑（中-3、低-6）。給 5 分鐘當下限。
MIN_REVERT_AFTER_SECONDS = 300.0
SECONDS_PER_DAY = 86400.0
MAX_KEYS_ENV_BYTES = 1 << 20
# llm.db 一致快照：SQLite backup API 重試次數與保留的備份份數（中-6）。
DB_COPY_ATTEMPTS = 3
DB_BACKUP_KEEP = 3
SQLITE_HEADER = b"SQLite format 3\x00"

# systemctl 的輸出分類（中-2）：先認「根本連不上／指令不在」這種確定失敗，
# 再認「unit 本來就沒在跑」。'No such file' 這種太寬的字串不能算 missing——
# `systemctl --user` 連不到 bus 時就是印它。
_FATAL_MARKERS = ("Failed to connect to bus", "command not found",
                  "No such file or directory")
_UNIT_MISSING_MARKERS = ("not loaded", "Loaded not-found", "does not exist",
                         "could not be found")

# 反向保險的 --on-active 只收 systemd 的時間格式（不猜、不讓它帶著壞值上場）。
_REVERT_AFTER_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(us|ms|s|sec|m|min|h|hr|d|w)?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationReport(Report):
    """三態之外再加 ``skip``：「已存在、不覆蓋」這種有看沒有動的項目。

    skip 不是失敗也不是待辦，所以不影響 ``exit_code()``（只看 ok／todo／fail）。
    輸出格式照 report.py：一行一項。
    """

    def skip(self, name: str, detail: str = "") -> None:
        line = f"skip {name}" + (f"（{detail}）" if detail else "")
        self.lines.append(line)
        self._out(line)


class OldLayout:
    """舊（v0.1.4；user 層）佈局的位置；``home`` 是 ``--old-home``（預設服務帳號的 HOME）。"""

    def __init__(self, home: str):
        self.home = home
        self.share_dir = os.path.join(home, *OLD_SHARE_REL)
        self.conf_dir = os.path.join(home, *OLD_CONF_REL)
        self.state_dir = os.path.join(home, *OLD_STATE_REL)
        self.unit_dir = os.path.join(home, *OLD_UNIT_REL)

    @property
    def env_file(self) -> str:
        return os.path.join(self.conf_dir, "llmgateway.env")

    @property
    def tunnel_env_file(self) -> str:
        return os.path.join(self.conf_dir, "tunnel.env")

    @property
    def keysync_env_file(self) -> str:
        return os.path.join(self.conf_dir, OLD_KEYSYNC_ENV_NAME)

    @property
    def keys_env(self) -> str:
        return os.path.join(self.state_dir, "keys.env")

    @property
    def db_file(self) -> str:
        return os.path.join(self.state_dir, "llm.db")

    @property
    def keysync_sha_file(self) -> str:
        return os.path.join(self.state_dir, KEYSYNC_SHA_NAME)

    def unit_file(self, name: str) -> str:
        return os.path.join(self.unit_dir, name)


# -- 小工具 ---------------------------------------------------------------


def _tool(layout, env_name: str, default: str) -> str:
    value = layout.env.get(env_name)
    return default if value in (None, "") else value


def _wait_seconds(layout, env_name: str, default: float) -> float:
    """等多久（秒）；``LLMGW_*`` 可調（測試調 0＝只試一次）。"""
    raw = layout.env.get(env_name)
    if raw in (None, ""):
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _observe_seconds(layout) -> float:
    """觀察期（秒）：預設 3 天；``LLMGW_OBSERVE_DAYS`` 可以調（測試用）。"""
    raw = layout.env.get(ENV_OBSERVE_DAYS)
    if raw in (None, ""):
        return DEFAULT_OBSERVE_DAYS * SECONDS_PER_DAY
    try:
        days = float(raw)
    except ValueError:
        return DEFAULT_OBSERVE_DAYS * SECONDS_PER_DAY
    return max(0.0, days) * SECONDS_PER_DAY


def _duration_text(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 120:
        return f"{int(seconds)} 秒"
    if seconds < 7200:
        return f"{int(seconds / 60)} 分鐘"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小時"
    return f"{seconds / 86400:.1f} 天"


def _exists(path: str) -> bool:
    return os.path.lexists(path)


def _target_exists(path: str) -> bool:
    """目標是不是「可以用的一般檔」：一般檔、不是 symlink、大小大於 0（低-10）。

    目標可能在 llmgw 可寫的目錄（舊版留下的 state、或人工放錯的 symlink）：
    ``lexists`` 會把斷鏈當成「已存在」而 skip，切換時 systemd 的
    ``EnvironmentFile`` 或新服務開 db 才失敗。斷鏈／空檔都當不存在，照常放新的。
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _has_symlink_component(path: str, base: str | None = None) -> bool:
    """低-2：檢查路徑或其相對於 base 的父目錄是否有任何一層是 symlink。"""
    try:
        cur = os.path.normpath(path)
        base_norm = os.path.normpath(base) if base else None
        while cur and cur != "/":
            if base_norm:
                if cur == base_norm or not cur.startswith(base_norm):
                    break
            else:
                try:
                    return stat.S_ISLNK(os.lstat(cur).st_mode)
                except OSError:
                    return False
            try:
                st = os.lstat(cur)
                if stat.S_ISLNK(st.st_mode):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    except Exception:
        pass
    return False


def _open_regular(path: str, base: str | None = None) -> int:
    """``O_NOFOLLOW`` 開檔，並用**同一個 fd** 驗證是一般檔；不成立回 -1（中-9、低-2）。

    舊檔在 ``~llmgw``（服務帳號可控制）：「先 lstat 檢查、再用路徑重開
    （``copy2``／``sqlite3.connect``／``open``）」的中間，服務帳號可以把路徑換成
    指向 root 可讀檔的 symlink，root 就會把那個檔的內容交給服務帳號。所以這裡
    規定：**開了 fd 之後只信 fd**，之後路徑被換掉也不影響讀到的內容。
    低-2：若上層路徑元件包含 symlink 亦直接拒絕。
    """
    if _has_symlink_component(path, base):
        return -1
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return -1
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return -1
    except OSError:
        os.close(fd)
        return -1
    return fd


def _read_regular(path: str, limit: int | None = None, base: str | None = None) -> bytes | None:
    """從驗過的 fd 讀整個檔（``limit`` 給時最多讀那麼多 bytes；上限用
    ``limit`` 的呼叫端自己再看 size，中-9）。不是一般檔、讀不到回 ``None``。"""
    if base is not None:
        fd = _open_regular(path, base=base)
    else:
        fd = _open_regular(path)
    if fd < 0:
        return None
    try:
        chunks: list[bytes] = []
        total = 0
        while limit is None or total < limit:
            chunk = os.read(fd, min(1 << 16, limit - total) if limit is not None
                            else 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _lseek0(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        pass


def _copy_into_place(src: str, dst: str, mode: int | None = None, *,
                     owner: tuple[int, int] | None = None,
                     base: str | None = None) -> None:
    """同目錄暫存 → rename（原子）；不給 mode 就沿用來源權限（cp -a 語意）。

    內容從 ``O_NOFOLLOW`` 驗過的 fd 讀（中-9）：不再用 ``shutil.copy2(src, …)``
    （它會跟著路徑走，檢查與複製之間可被換成 symlink）。mtime 也照抄（cp -a）：
    回滾要用「新 keys.env 是不是比較新」判斷，時間戳不能被複製時間蓋掉。
    高-1／低-3：fchmod／fchown 在 rename 之前對 fd 操作，且 mkstemp 失敗時妥善關閉 src_fd。
    """
    directory = os.path.dirname(dst) or "."
    os.makedirs(directory, exist_ok=True)
    if base is not None:
        src_fd = _open_regular(src, base=base)
    else:
        src_fd = _open_regular(src)
    if src_fd < 0:
        raise OSError(f"{src} 不是一般檔")
    out_fd = -1
    tmp = None
    try:
        out_fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=directory)
        st = os.fstat(src_fd)
        file_mode = mode if mode is not None else stat.S_IMODE(st.st_mode)
        os.fchmod(out_fd, file_mode)
        if owner is not None:
            os.fchown(out_fd, owner[0], owner[1])
        with os.fdopen(out_fd, "wb") as out, os.fdopen(src_fd, "rb") as src_fh:
            out_fd = -1
            src_fd = -1
            shutil.copyfileobj(src_fh, out, 1 << 16)
        try:
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
        os.replace(tmp, dst)
        tmp = None
    finally:
        if src_fd >= 0:
            os.close(src_fd)
        if out_fd >= 0:
            os.close(out_fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _db_source_ok(path: str, base: str | None = None) -> bool:
    """是不是一個 SQLite 資料庫（backup API 的來源一定要是）；從驗過的 fd 讀。"""
    raw = _read_regular(path, len(SQLITE_HEADER) + 1, base=base)
    return bool(raw) and raw[:len(SQLITE_HEADER)] == SQLITE_HEADER


def _sqlite_connect_fd(fd: int):
    """用 fd 對應的路徑開一個 SQLite 連線（中-9）；平台不支援回 ``None``。

    ``/proc/self/fd/N``（Linux）與 ``/dev/fd/N``（macOS）指的是**已經驗過**的
    那個 inode：路徑之後被換成 symlink 也不影響。唯讀連線不寫任何東西。
    """
    for base in ("/proc/self/fd", "/dev/fd"):
        uri = f"file:{base}/{fd}?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("PRAGMA trusted_schema = OFF;")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            continue
        return conn
    return None


def _copy_fd_to_temp(src_fd: int, directory: str) -> str:
    """把驗過的 fd 複製成同目錄的 0600 暫存檔（root 專屬），回路徑。"""
    _lseek0(src_fd)
    fd, tmp = tempfile.mkstemp(prefix=".lgw-db-src.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out, os.fdopen(os.dup(src_fd), "rb") as src_fh:
            shutil.copyfileobj(src_fh, out, 1 << 16)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        _lseek0(src_fd)
    return tmp


def _backup_into(src_conn, dst_path: str) -> None:
    dst_conn = sqlite3.connect(dst_path)
    try:
        dst_conn.execute("PRAGMA trusted_schema = OFF;")
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()


def _sqlite_backup_fd(src_fd: int, dst_path: str, directory: str) -> None:
    """從驗過的 fd 線上備份到 ``dst_path``（中-9；舊服務還在寫也安全，高-4）。

    先試 fd 對應的路徑；開不起來或中途失敗（例如平台只有唯讀路徑、hot journal
    的唯讀限制）就退回「先從 fd 複製到 root 專屬暫存，再對暫存檔 backup」。
    退回路徑拿到的是位元組複製：寫入交易進行中可能是撕裂影像，靠後面的
    ``integrity_check`` 擋（擋下就重試；三次都不一致就整體失敗、不蓋目標）。
    """
    tmp_copy: str | None = None
    src_conn = None
    try:
        src_conn = _sqlite_connect_fd(src_fd)
        if src_conn is None:
            tmp_copy = _copy_fd_to_temp(src_fd, directory)
            src_conn = sqlite3.connect(tmp_copy)
            src_conn.execute("PRAGMA trusted_schema = OFF;")
        _backup_into(src_conn, dst_path)
        return
    except sqlite3.Error:
        if src_conn is not None:
            src_conn.close()
            src_conn = None
        if tmp_copy is None:
            tmp_copy = _copy_fd_to_temp(src_fd, directory)
        src_conn = sqlite3.connect(tmp_copy)
        src_conn.execute("PRAGMA trusted_schema = OFF;")
        _backup_into(src_conn, dst_path)
    finally:
        if src_conn is not None:
            src_conn.close()
        if tmp_copy is not None:
            try:
                os.unlink(tmp_copy)
            except OSError:
                pass


def _db_integrity_ok(path: str) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA trusted_schema = OFF;")
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == "ok"


def _verify_not_swapped(path: str, fd: int) -> None:
    """``path`` 還是不是 ``fd`` 那個檔（同一個 inode、不是 symlink；中-9）。"""
    by_fd = os.fstat(fd)
    by_path = os.lstat(path)
    if stat.S_ISLNK(by_path.st_mode) or (by_path.st_dev, by_path.st_ino) \
            != (by_fd.st_dev, by_fd.st_ino):
        raise OSError("暫存檔在過程中被換掉了")


def _copy_db_consistent(src: str, dst: str, mode: int = 0o600, *,
                        owner: tuple[int, int] | None = None,
                        base: str | None = None) -> None:
    """一致快照到 dst（同目錄暫存 → rename）；驗 integrity_check，失敗重試再放棄。

    不沿用 ``shutil.copy2``：rollback journal 模式的主檔在寫入交易期間是就地
    修改的，直接複製會拿到新舊頁混合的檔案（高-4）。``Connection.backup`` 對
    活著的寫入者安全（讀交易看到的是已提交的狀態）；來源一律從驗過的 fd 讀
    （中-9），暫存檔在 rename 前再驗一次 inode 沒被換掉。
    高-1／低-3：fchmod／fchown 在 rename 前對 fd 操作，且 mkstemp 失敗時妥善關閉 src_fd。
    """
    if not _db_source_ok(src, base=base):
        raise OSError(f"{src} 不是 SQLite 資料庫")
    directory = os.path.dirname(dst) or "."
    os.makedirs(directory, exist_ok=True)
    last: Exception | None = None
    for _ in range(DB_COPY_ATTEMPTS):
        src_fd = _open_regular(src, base=base)
        if src_fd < 0:
            raise OSError(f"{src} 不是一般檔")
        dst_fd = -1
        tmp = None
        try:
            dst_fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=directory)
            _sqlite_backup_fd(src_fd, tmp, directory)
            if not _db_integrity_ok(tmp):
                raise sqlite3.DatabaseError("integrity_check 不是 ok")
            _verify_not_swapped(tmp, dst_fd)
            os.fchmod(dst_fd, mode)
            if owner is not None:
                os.fchown(dst_fd, owner[0], owner[1])
            os.replace(tmp, dst)
            tmp = None
            return
        except (OSError, sqlite3.Error) as exc:
            last = exc
        finally:
            if dst_fd >= 0:
                os.close(dst_fd)
            if src_fd >= 0:
                os.close(src_fd)
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    raise OSError(f"一致快照失敗（{type(last).__name__ if last else 'unknown'}）")


def _db_backup_paths(dst: str) -> list:
    """舊 db 的時間戳備份（新到舊排序）。"""
    return sorted(glob.glob(dst + ".bak-*"), reverse=True)


def _prune_db_backups(dst: str) -> None:
    """只留最近 ``DB_BACKUP_KEEP`` 份時間戳備份（中-6）。"""
    for path in _db_backup_paths(dst)[DB_BACKUP_KEEP:]:
        try:
            os.unlink(path)
        except OSError:
            pass


def _has_password(path: str) -> bool:
    """檔案裡有沒有非空的 ``LLMGW_KEYS_PASSWORD=``（只看有沒有，值不出來）。"""
    raw = _read_regular(path)
    if raw is None:
        return False
    for line in raw.decode("utf-8", "replace").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == install.KEYS_PASS_VAR and value.strip():
            return True
    return False


def _keys_env_valid(path: str) -> bool:
    """新的 ``keys.env`` 「格式驗得過」：KEY=VALUE、鍵名在白名單、不重複、至少一個有值。

    低-7／低-6：語法合法但鍵名錯的檔（人工貼錯）、或同一個鍵出現兩次，舊服務的
    ``KeyStore._parse`` 都會拒收（重複鍵也是它的格式錯誤），正好打在「回舊版」
    這一步。鍵名白名單用 ``lgwd.keys.KEY_NAMES``（服務真正會載入的那一組）；
    載不到就當驗不過（不覆蓋舊的）。內容從驗過的 fd 讀（中-9）。
    """
    try:
        from lgwd.keys import KEY_NAMES
    except ImportError:
        return False
    allowed = set(KEY_NAMES.values())
    raw = _read_regular(path, MAX_KEYS_ENV_BYTES + 1)
    if not raw or len(raw) > MAX_KEYS_ENV_BYTES or b"\x00" in raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    saw_value = False
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_NAME_RE.match(name) or name not in allowed:
            return False
        if name in seen:                # 低-6：重複鍵舊 KeyStore 會拒收
            return False
        seen.add(name)
        if value.strip():
            saw_value = True
    return saw_value


def _service_owner_ids(layout) -> tuple[int, int] | None:
    if layout.prefix or layout.euid != 0:
        return None
    try:
        import pwd
        info = pwd.getpwnam(layout.service_user)
        return (info.pw_uid, info.pw_gid)
    except KeyError:
        return None


def _root_group_owner_ids(layout) -> tuple[int, int] | None:
    if layout.prefix or layout.euid != 0:
        return None
    try:
        import pwd
        info = pwd.getpwnam(layout.service_user)
        return (0, info.pw_gid)
    except KeyError:
        return None


def _regular_file(path: str, base: str | None = None) -> bool:
    """存在、是一般檔、**自身與其上層目錄都不是 symlink**（中-5、低-2）。"""
    try:
        if _has_symlink_component(path, base):
            return False
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _refuse_symlink(report, name: str, path: str, what: str = "舊的") -> None:
    report.fail(name, f"{what} {path} 或其上層目錄不是一般檔（symlink？）——不跟，請本人確認後再跑")


def _chown_keys_pass(layout, report, name: str = "keys-pass") -> bool:
    """keys.pass → root:<服務群組> 0640；拿不到群組要 fail，不能靜默 root:root。"""
    try:
        install.chown_root_group(layout, layout.keys_pass_file)
    except KeyError:
        report.fail(name, f"找不到服務帳號 {layout.service_user} 的群組；"
                          "keys.pass 不能留在 root:root（llmgwctl sync 會讀不到）")
        return False
    except OSError as exc:
        report.fail(name, f"chown 失敗（{type(exc).__name__}）")
        return False
    return True


# -- 狀態檔 ---------------------------------------------------------------


def ctl_dir(layout) -> str:
    """遷移控制檔的目錄（<prefix>/var/lib/llmgateway-ctl）：root 專屬（高-3）。"""
    return os.path.join(layout.prefix, CTL_DIR.lstrip("/"))


def cutover_state_path(layout) -> str:
    return os.path.join(ctl_dir(layout), CUTOVER_STATE_NAME)


def cutover_lock_path(layout) -> str:
    return os.path.join(ctl_dir(layout), CUTOVER_LOCK_NAME)


def _ensure_ctl_dir(layout) -> None:
    """建控制目錄並鎖成 0700 root:root（prefix 模式不 chown，照樣 chmod）。"""
    directory = ctl_dir(layout)
    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o700)
    if not layout.prefix and layout.euid == 0:
        os.chown(directory, 0, 0)


def read_state(layout) -> dict:
    """讀 ``.cutover.state``；不在或讀不懂回空字典（不 raise）。"""
    try:
        with open(cutover_state_path(layout), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(layout, **fields) -> dict:
    """合併欄位後原子寫一行 JSON（值只有階段、時間、單元名與代碼）。

    互斥由呼叫方的 ``_Lock`` 負責（confirm／inner／rollback／finalize／prepare
    都在鎖內寫）；這裡只做 read-merge-write。
    """
    data = read_state(layout)
    data.update(fields)
    data["ts"] = time.time()
    _ensure_ctl_dir(layout)
    atomic.write_text(cutover_state_path(layout), json.dumps(data, sort_keys=True) + "\n",
                      0o600)
    return data


def archive_state(layout) -> str | None:
    """把上一輪的狀態檔歸檔成 ``.cutover.state.<時間戳>``；不在回 ``None``（中-4）。

    ``reverted``／``aborted`` 是終態，但要能開新一輪（prepare／cutover）：新一輪
    開始前先把舊狀態檔移開，不然 ``write_state`` 的 read-merge-write 會沿用舊的
    phase、``new_service_ts`` 與保險單元名。原檔內容一字不改地留著（事後可查）。
    """
    path = cutover_state_path(layout)
    if not _exists(path):
        return None
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    target = f"{path}.{stamp}"
    index = 1
    while _exists(target):
        target = f"{path}.{stamp}-{index}"
        index += 1
    os.replace(path, target)
    return target


def _start_new_round(layout, report) -> None:
    """新一輪開始前，把終態（reverted／aborted）的舊狀態檔歸檔（中-4）。"""
    state = read_state(layout)
    if state.get("phase") not in (PHASE_REVERTED, PHASE_ABORTED):
        return
    try:
        archived = archive_state(layout)
    except OSError as exc:
        report.fail("state-archive", f"歸檔舊狀態檔失敗（{type(exc).__name__}）")
        return
    if archived:
        report.ok("state-archive", f"{CUTOVER_STATE_NAME} → {os.path.basename(archived)}")


def _checked_state_unit(value) -> str | None:
    """狀態檔裡的 unit 名只接受保險的固定值（高-3：不可信輸入）。"""
    name = str(value or "")
    return name if name in STATE_UNIT_NAMES else None


class _Lock:
    """``flock`` 互斥鎖（反向保險的行程與人工同時回舊版時只有一個真的做事）。"""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def acquire(self, timeout: float = 0.0) -> bool:
        """拿鎖；``timeout`` 秒內拿不到回 False（中-3：短暫重試，不要靜默跳過）。"""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(directory, flags)
            try:
                os.fchmod(dir_fd, 0o700)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.2, remaining))
                continue
            self.fd = fd
            return True

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


# -- systemctl（system 層／user 層）---------------------------------------


def _judge(proc) -> tuple:
    """把一個 systemctl 的結果分成 ok｜missing（unit 不在）｜failed（中-2）。"""
    if proc.returncode == 0:
        return "ok", ""
    text = (proc.stderr or "") + (proc.stdout or "")
    if any(marker in text for marker in _FATAL_MARKERS):
        return "failed", f"rc={proc.returncode}"
    if any(marker in text for marker in _UNIT_MISSING_MARKERS):
        return "missing", ""
    return "failed", f"rc={proc.returncode}"


def _systemctl_outcome(layout, *args) -> tuple:
    """system 層的 systemctl（走 units.raw，唯一真的 spawn systemctl 的地方）。"""
    try:
        proc = units.raw(layout, *args)
    except runmod.RunError as exc:
        return "failed", exc.kind
    return _judge(proc)


def user_systemctl_argv(layout, *args) -> list:
    """以服務帳號身分對它的 user manager 下指令（停／啟動舊 unit 用）。

    這步在 root 下代做，所以用 ``runuser -u llmgw --`` 自帶
    ``XDG_RUNTIME_DIR``／``DBUS_SESSION_BUS_ADDRESS``（#35 設計 §1 的痛點：
    不要再要求操作者自己 export）。systemctl 路徑沿用 layout.systemctl，
    測試換成假指令。實作在 units.user_systemctl_argv（這裡轉一層維持 migrate
    內部既有名稱）。
    """
    return units.user_systemctl_argv(layout, *args)


def _user_systemctl_outcome(layout, *args) -> tuple:
    try:
        proc = units.user_run(layout, *args)
    except runmod.RunError as exc:
        return "failed", exc.kind
    return _judge(proc)


def _user_unit_state(layout, unit: str) -> str:
    """user manager 眼中的 unit 狀態：active｜inactive｜missing｜unknown（中-2）。"""
    return units.user_unit_state(layout, unit)


def _wait_unit_active(layout, unit: str, seconds: float) -> bool:
    """等一個 system unit 真的 active（中-4：start 回 0 不等於連上了）。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if units.unit_state(layout, unit) == "active":
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def _system_unit_busy(layout, unit: str) -> bool:
    """unit 正在跑或正在起來（active／activating／reloading）；中-9 用。

    ``is-active`` 對 activating 回 3 但 stdout 是 ``activating``：只看結束碼
    會把「保險正在回舊版」誤判成沒在跑。
    """
    try:
        proc = units.raw(layout, "is-active", unit)
    except runmod.RunError:
        return False
    if proc.returncode == 0:
        return True
    text = (proc.stdout or "") + (proc.stderr or "")
    return any(word in text for word in ("activating", "reloading"))


def _system_unit_failed(layout, unit: str) -> bool:
    """unit 是不是 ``failed``（低-13：保險跑到一半失敗殘留，狀態可能半回滾）。"""
    try:
        proc = units.raw(layout, "is-failed", unit)
    except runmod.RunError:
        return False
    return proc.returncode == 0


def _outcome(layout, layer: str, *args) -> tuple:
    """``layer="user"`` 走 runuser 包起來的 user manager，其餘走 system 層。"""
    if layer == "user":
        return _user_systemctl_outcome(layout, *args)
    return _systemctl_outcome(layout, *args)


def _report_outcome(report, name: str, outcome: str, detail: str, *,
                    ok: str, missing: str, failed: str) -> None:
    if outcome == "ok":
        report.ok(name, ok)
    elif outcome == "missing":
        report.skip(name, missing)
    else:
        report.fail(name, failed or f"systemctl 失敗（{detail}）")


def _stop_unit(layout, report, name: str, unit: str, *, layer: str = "system",
               ok: str, missing: str) -> str:
    """停一個 unit；回 ``ok``（真的停了）｜``missing``（本來就沒在跑）｜``failed``。"""
    outcome, detail = _outcome(layout, layer, "stop", unit)
    _report_outcome(report, name, outcome, detail, ok=ok, missing=missing,
                    failed=f"停 {unit} 失敗（{detail}）")
    return outcome


def _disable_unit(layout, report, name: str, unit: str, *, layer: str = "system") -> str:
    """disable 一個 unit（開機不再自動起）；回三態。"""
    outcome, detail = _outcome(layout, layer, "disable", unit)
    _report_outcome(report, name, outcome, detail,
                    ok=f"{unit} 不再開機自動起",
                    missing=f"{unit}（本來就沒 enable）",
                    failed=f"disable {unit} 失敗（{detail}）")
    return outcome


def _count_value(layout, env_name: str, default: int) -> int:
    """整數型的旋鈕（次數）；壞值用預設。"""
    raw = layout.env.get(env_name)
    if raw in (None, ""):
        return default
    try:
        return max(1, int(float(raw)))
    except ValueError:
        return default


def _revert_after_seconds(value: str) -> float | None:
    """把 systemd 的時間值換成秒（給下限檢查用）；格式不認得回 None。"""
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)(us|ms|s|sec|m|min|h|hr|d|w)?$", value or "")
    if not match:
        return None
    factor = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "sec": 1.0, "m": 60.0, "min": 60.0,
              "h": 3600.0, "hr": 3600.0, "d": 86400.0, "w": 604800.0}[match.group(2) or "s"]
    return float(match.group(1)) * factor


# -- prepare-migration ----------------------------------------------------


def prepare(layout, *, tag: str, old_home: str | None = None, force: bool = False,
            report: MigrationReport | None = None) -> int:
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return report.exit_code(todo_ok=True)
    if not TAG_RE.match(tag or ""):
        report.fail("tag", f"格式不符（要 {TAG_PATTERN}）：{tag}")
        return report.exit_code(todo_ok=True)
    old = OldLayout(old_home or layout.service_home)
    # 低-4：prepare 也會複製 db／keys 並寫狀態檔，要與 cutover／rollback／
    # finalize 共用同一把 flock（不然回滾與 prepare 會互相覆寫狀態與檔案）。
    lock = _Lock(cutover_lock_path(layout))
    try:
        if not lock.acquire(_wait_seconds(layout, ENV_LOCK_WAIT, DEFAULT_LOCK_WAIT)):
            report.fail("prepare", "另一個切換／回滾／收尾正在跑（等不到鎖）；稍後再 prepare")
            return report.exit_code(todo_ok=True)
    except OSError as exc:
        report.fail("prepare", f"拿不到鎖（{type(exc).__name__}）：{lock.path}")
        return report.exit_code(todo_ok=True)
    try:
        return _prepare_locked(layout, old, tag=tag, force=force, report=report)
    finally:
        lock.release()


def _prepare_locked(layout, old: OldLayout, *, tag: str, force: bool,
                    report) -> int:
    """prepare 的本體（已持有 flock）。"""
    state = read_state(layout)
    phase = state.get("phase")
    # 中-1：切換已進行或已完成時拒絕重新準備（避免 --force 或換 tag 覆蓋跑中的新資料）。
    if phase in OPEN_PHASES:
        report.fail("prepare", f"狀態是 {phase}；切換已進行或已完成，拒絕重新準備"
                               "（若需開新一輪請先以 rollback-to-user 回滾）")
        return report.exit_code(todo_ok=True)
    if phase in (PHASE_REVERTED, PHASE_ABORTED):
        _start_new_round(layout, report)
    if not install._step_account(layout, report):
        return report.exit_code(todo_ok=True)
    if not install._step_dirs(layout, report):
        return report.exit_code(todo_ok=True)
    tag_dir = release.fetch(layout, tag, None, report)
    if tag_dir is None:
        return report.exit_code(todo_ok=True)
    # 中-7：壞的 policy 在這裡（還沒動任何服務）就擋下來。
    if not upgrade._dry_run_policy(layout, tag_dir, report):
        return report.exit_code(todo_ok=True)
    try:
        old_tag = release.switch_current(layout, tag)
    except OSError as exc:
        report.fail("current", f"切 current 失敗（{type(exc).__name__}）")
        return report.exit_code(todo_ok=True)
    if old_tag and old_tag != tag:
        report.ok("current", f"{old_tag} → {tag}")
    else:
        report.ok("current", tag)
    if not units.install_unit_files(layout, tag_dir, report):
        return report.exit_code(todo_ok=True)
    if not units.daemon_reload(layout):
        report.fail("daemon-reload", f"{layout.systemctl} daemon-reload 失敗")
        return report.exit_code(todo_ok=True)
    report.ok("daemon-reload", "unit 已放好（不 enable、不 start）")
    if not install._step_wrapper(layout, report):
        return report.exit_code(todo_ok=True)

    _copy_old_files(layout, old, report, force=force)
    _step_gh_login(layout, report)
    _step_tunnel_config(layout, report)
    # 把正規化後的 old_home 綁進狀態檔（低-3）：finalize 不收命令列打錯的目錄。
    # 上一輪若是終態（reverted／aborted），先把舊狀態檔歸檔再開新一輪（中-4）。
    _start_new_round(layout, report)
    try:
        if not read_state(layout).get("old_home"):
            write_state(layout, old_home=old.home)
    except OSError as exc:
        report.fail("state", f"寫 {cutover_state_path(layout)} 失敗（{type(exc).__name__}）")
    report.ok("old-untouched", f"舊 user unit 與舊目錄都沒動（{old.home}）")
    return report.exit_code(todo_ok=True)


def _copy_old_files(layout, old: OldLayout, report, *, force: bool) -> None:
    """把舊佈局的 key、資料庫、設定與解密密碼複製到新佈局（舊的一律唯讀）。"""
    _step_keys_env(layout, old, report, force=force)
    _step_db(layout, old, report, force=force)
    _step_env_file(layout, old, report, force=force, name="tunnel-env",
                   src=old.tunnel_env_file, dst=layout.tunnel_env_file,
                   template=install.TUNNEL_ENV_TEMPLATE.format(
                       home=layout.service_home,
                       identity=os.path.join(layout.service_home, ".ssh",
                                             install.TUNNEL_KEY_NAME),
                       known_hosts=os.path.join(layout.service_home, ".ssh",
                                                install.TUNNEL_KNOWN_HOSTS_NAME),
                       port=DEFAULT_API_PORT))
    _step_env_file(layout, old, report, force=force, name="llmgateway-env",
                   src=old.env_file, dst=layout.env_file,
                   template=install.LLMGATEWAY_ENV_TEMPLATE.format(port=DEFAULT_API_PORT))
    _step_keys_pass(layout, old, report, force=force)
    _step_keysync_sha(layout, old, report, force=force)


def _step_keys_env(layout, old: OldLayout, report, *, force: bool) -> None:
    dst = layout.keys_env
    if _target_exists(dst) and not force:
        report.skip("state-keys-env", f"{dst} 已存在（不覆蓋）")
        return
    if _exists(old.keys_env) and not _regular_file(old.keys_env, old.home):
        # 中-5：舊檔在服務帳號控制下；symlink 會讓 root 把任意檔交給 llmgw。
        _refuse_symlink(report, "state-keys-env", old.keys_env)
        return
    if not os.path.isfile(old.keys_env):
        if not _exists(dst):
            try:
                atomic.write_text(dst, "", 0o600, owner=_service_owner_ids(layout))
                install._chown_service(layout, dst)
            except OSError as exc:
                report.fail("state-keys-env", f"建空檔失敗（{type(exc).__name__}）")
                return
        report.todo("state-keys-env",
                    f"舊的 {old.keys_env} 不在；新版啟動後由 key 拉取補（或本人確認）")
        return
    try:
        _copy_into_place(old.keys_env, dst, 0o600, owner=_service_owner_ids(layout))
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("state-keys-env", f"複製失敗（{type(exc).__name__}）")
        return
    report.ok("state-keys-env", "從舊 state 複製（0600、llmgw）")


def _step_db(layout, old: OldLayout, report, *, force: bool) -> None:
    dst = layout.db_file
    if _target_exists(dst) and not force:
        report.skip("state-db", f"{dst} 已存在（不覆蓋）")
        return
    if _exists(old.db_file) and not _regular_file(old.db_file, old.home):
        _refuse_symlink(report, "state-db", old.db_file)
        return
    if not os.path.isfile(old.db_file):
        report.todo("state-db", f"舊的 {old.db_file} 不在；新服務會自己建")
        return
    try:
        _copy_db_consistent(old.db_file, dst, owner=_service_owner_ids(layout))
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("state-db", f"一致快照失敗（{type(exc).__name__}）")
        return
    report.ok("state-db", "從舊 state 一致快照 llm.db（舊服務還在寫也安全）")


def _step_env_file(layout, old: OldLayout, report, *, force: bool, name: str,
                   src: str, dst: str, template: str) -> None:
    if _target_exists(dst) and not force:
        report.skip(name, f"{dst} 已存在（不覆蓋）")
        return
    if _exists(src) and not _regular_file(src, old.home):
        _refuse_symlink(report, name, src)
        return
    if not os.path.isfile(src):
        try:
            atomic.write_text(dst, template, 0o640,
                              owner=_root_group_owner_ids(layout))
            install.chown_root_group(layout, dst)
        except KeyError:
            report.fail(name, f"找不到服務帳號 {layout.service_user} 的群組；"
                              "設定檔 owner 沒設好")
            return
        except OSError as exc:
            report.fail(name, f"寫範本失敗（{type(exc).__name__}）")
            return
        report.todo(name, f"舊的 {src} 不在；放了範本，要本人確認")
        return
    try:
        # 內容原樣照抄（IDENTITY／KNOWN_HOSTS 是絕對路徑、不改寫），但 owner／
        # mode 是規格值：/etc 的設定檔 root:<llmgw 群組> 0640（中-7）。
        _copy_into_place(src, dst, 0o640,
                         owner=_root_group_owner_ids(layout),
                         base=old.home)
        install.chown_root_group(layout, dst)
    except KeyError:
        report.fail(name, f"找不到服務帳號 {layout.service_user} 的群組；"
                          "設定檔 owner 沒設好")
        return
    except OSError as exc:
        report.fail(name, f"複製失敗（{type(exc).__name__}）")
        return
    report.ok(name, f"從舊 {src} 複製（root:{layout.service_user} 0640）")


def _step_keys_pass(layout, old: OldLayout, report, *, force: bool) -> None:
    dst = layout.keys_pass_file
    if not force and _has_password(dst):
        # 低-9：有值就 skip，但權限還是要校正（人工建立／舊版留下的可能是
        # root:root 0600 或 llmgw:llmgw，llmgwctl sync 會讀不到）；與 install 對齊。
        if os.path.islink(dst):
            report.fail("keys-pass", f"{dst} 是 symlink；拒絕以 root 變更權限")
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(dst, flags)
            try:
                os.fchmod(fd, 0o640)
            finally:
                os.close(fd)
        except OSError as exc:
            report.fail("keys-pass", f"chmod 失敗（{type(exc).__name__}）")
            return
        if not _chown_keys_pass(layout, report):
            return
        report.skip("keys-pass", f"{dst} 已有值（不覆蓋；owner／mode 已校正）")
        return
    if _exists(old.keysync_env_file) and not _regular_file(old.keysync_env_file):
        # 中-5：舊 keysync.env 在服務帳號控制下（symlink 到 /proc/… 或外部檔）。
        _refuse_symlink(report, "keys-pass", old.keysync_env_file)
        return
    raw = _read_regular(old.keysync_env_file, MAX_KEYS_ENV_BYTES + 1)
    if raw is None:
        if _exists(old.keysync_env_file) and not _regular_file(old.keysync_env_file):
            _refuse_symlink(report, "keys-pass", old.keysync_env_file)
            return
        value = ""
    elif len(raw) > MAX_KEYS_ENV_BYTES:
        value = ""
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        value = parse_env_text(text).get(install.KEYS_PASS_VAR, "")
    if not value:
        if not _exists(dst):
            try:
                atomic.write_text(dst, install.KEYS_PASS_TEMPLATE, 0o640,
                                  owner=_root_group_owner_ids(layout))
            except OSError as exc:
                report.fail("keys-pass", f"建空殼失敗（{type(exc).__name__}）")
                return
        if not _chown_keys_pass(layout, report):
            return
        report.todo("keys-pass",
                    f"舊的 {old.keysync_env_file} 沒有非空的 {install.KEYS_PASS_VAR}；"
                    f"要本人填進 {dst}")
        return
    try:
        atomic.write_text(dst, f"{install.KEYS_PASS_VAR}={value}\n", 0o640,
                          owner=_root_group_owner_ids(layout))
    except OSError as exc:
        report.fail("keys-pass", f"寫失敗（{type(exc).__name__}）")
        return
    if not _chown_keys_pass(layout, report):
        return
    report.ok("keys-pass",
              f"從舊 {OLD_KEYSYNC_ENV_NAME} 複製一行到 {dst}"
              f"（root:{layout.service_user} 0640；值不印出）")


def _step_keysync_sha(layout, old: OldLayout, report, *, force: bool) -> None:
    """``.keysync.sha`` 一起複製（低-8a）：省一次 key 的全量重拉。"""
    src, dst = old.keysync_sha_file, layout.keysync_sha_file
    if _target_exists(dst) and not force:
        report.skip("state-keysync-sha", f"{dst} 已存在（不覆蓋）")
        return
    if _exists(src) and not _regular_file(src, old.home):
        _refuse_symlink(report, "state-keysync-sha", src)
        return
    if not os.path.isfile(src):
        report.todo("state-keysync-sha",
                    f"舊的 {src} 不在；第一次 key 拉取會全量重拉（功能不受影響）")
        return
    try:
        _copy_into_place(src, dst, 0o600, owner=_service_owner_ids(layout),
                         base=old.home)
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("state-keysync-sha", f"複製失敗（{type(exc).__name__}）")
        return
    report.ok("state-keysync-sha", "從舊 state 複製（省一次全量拉取）")


def _step_gh_login(layout, report) -> None:
    """順便確認 gh 登入（#35 §6 風險：搬到 system 層後 HOME 還是 llmgw 的）。"""
    logged_in = install._gh_logged_in(layout)
    if logged_in is True:
        report.ok("gh-login", f"{layout.service_user} 的 gh 已登入")
    elif logged_in is False:
        report.todo("gh-login", f"以 {layout.service_user} 身分 gh auth login")
    else:
        report.todo("gh-login", f"無法確認（找不到 {layout.gh} 或 sudo 失敗）")


def _step_tunnel_config(layout, report) -> None:
    ready, missing = layout.tunnel_config_state()
    if ready:
        report.ok("tunnel-config", "tunnel.env 已就緒")
    else:
        report.todo("tunnel-config",
                    f"tunnel.env 還沒填（{missing}）；切換時經會合點會探不過")


# -- cutover --------------------------------------------------------------


def transient_setenv_args(layout) -> list:
    """把呼叫端的 ``LLMGW_*`` 變數轉送給 transient unit（中-1）。

    ``systemd-run`` 只給服務管理器的預設環境，呼叫端的 ``LLMGW_PREFIX`` 等
    不會自動過去；不轉送的話，演練（Lima VM＋prefix 隔離）的 detached 單元
    會打到真實路徑。只轉送真的需要的幾個，不是整個環境。
    """
    args = []
    for name in TRANSIENT_ENV_NAMES:
        value = layout.env.get(name)
        if value not in (None, ""):
            args.append(f"--setenv={name}={value}")
    return args


def cutover_unit_argv(layout, *, tag: str, revert_after: str, old_home: str,
                      no_remote: bool) -> list:
    """分離執行的 transient unit：人手下的 cutover 只跑這一件事。

    ssh 連線斷了也繼續（system 層的 transient service，不隨登入 session 結束）。
    """
    inner = [layout.python3, "-m", "lgwctl", "cutover",
             "--tag", tag, "--revert-after", revert_after, "--old-home", old_home,
             "--inner"]
    if no_remote:
        inner.append("--no-remote")
    return [_tool(layout, ENV_SYSTEMD_RUN, "systemd-run"),
            f"--unit={CUTOVER_UNIT}", "--collect", *transient_setenv_args(layout),
            f"--working-directory={layout.current_link}", *inner]


def insurance_unit_argv(layout, *, revert_after: str, old_home: str) -> list:
    """反向保險：時間到就跑 ``rollback-to-user``（system 層的 transient timer）。

    這是停損的最後一道：沒人 confirm 就自己回舊版。注意 transient 的 timer
    **重開機就沒了**：pending-confirm 期間重開機不會自動回舊版（見 cutover
    輸出的提醒與模組說明）。
    """
    return [_tool(layout, ENV_SYSTEMD_RUN, "systemd-run"),
            f"--unit={REVERT_UNIT}", f"--on-active={revert_after}", "--collect",
            *transient_setenv_args(layout),
            f"--working-directory={layout.current_link}",
            layout.python3, "-m", "lgwctl", "rollback-to-user",
            "--old-home", old_home, "--inner"]


def preflight(layout, tag: str, old: OldLayout | None = None) -> list:
    """切換前的不影響舊服務檢查（中-8）；回問題清單（空＝可以切）。

    outer 在排 transient unit **之前**、inner 在排保險之前都跑同一組：缺
    keys.pass 的值、tunnel.env 沒填、版本目錄服務帳號進不去…這些 prepare 只
    列 todo，切下去才發現就等於白白停一次舊服務（還要回滾）。

    中-6（gh 沒登入）與中-7（policy dry-run）也在這裡：`check` 把 gh 沒登入
    當 todo（rc 2），切完才踩到就會白停一次舊服務；壞 policy 同理（新服務載
    不起 policy → list 非 200 → 停損回舊版）。
    中-3：檢查 OLD_UNIT_NAMES 四個舊 unit 檔都在且為一般檔（缺則停損回滾會拒絕，切換前拒絕）。
    """
    problems: list[str] = []
    old_layout = old or OldLayout(layout.service_home)
    missing_old = [unit for unit in OLD_UNIT_NAMES
                   if not _regular_file(old_layout.unit_file(unit), old_layout.home)]
    if missing_old:
        problems.append(f"舊 unit 檔缺漏或不是一般檔（{old_layout.unit_dir} 的 "
                        f"{'、'.join(missing_old)}；缺則停損回滾會拒絕，切換前拒絕）")
    if layout.installed_tag() != tag:
        problems.append(f"current 不是 {tag}（先跑 prepare-migration --tag {tag}）")
    tag_dir = layout.tag_dir(tag)
    try:
        mode = os.stat(tag_dir).st_mode
    except OSError:
        problems.append(f"版本目錄不在：{tag_dir}")
        mode = None
    if mode is not None and not (mode & 0o005):
        problems.append(f"版本目錄服務帳號進不去（mode {oct(mode & 0o777)}；高-1）")
    if not os.path.isfile(layout.keys_env):
        problems.append("keys.env 不在（先跑 prepare-migration）")
    if not install._keys_pass_filled(layout):
        problems.append(f"keys.pass 沒有非空的 {install.KEYS_PASS_VAR}（要本人填）")
    ready, missing = layout.tunnel_config_state()
    if not ready:
        problems.append(f"tunnel.env 還沒就緒（{missing}）")
    # 中-6：gh 沒登入時 `check` 只會 todo（rc 2）→ cutover 當停損白切一次。
    if install._gh_logged_in(layout) is False:
        problems.append("gh 沒登入（先 gh auth login；切完 check 會當停損）")
    # 中-7：policy dry-run（只讀檔、用新 tag 的 loader；壞 policy 不排程）。
    if mode is not None and tag_dir:
        if not upgrade._dry_run_policy(layout, tag_dir, MigrationReport()):
            problems.append("policy dry-run 不過（先修新 tag 的 policy）")
    return problems


def cutover(layout, *, tag: str, revert_after: str = DEFAULT_REVERT_AFTER,
            old_home: str | None = None, no_remote: bool = False, inner: bool = False,
            report: MigrationReport | None = None) -> int:
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return 1
    if not TAG_RE.match(tag or ""):
        report.fail("tag", f"格式不符（要 {TAG_PATTERN}）：{tag}")
        return 1
    if not _REVERT_AFTER_RE.match(revert_after or ""):
        report.fail("revert-after", f"時間格式不認得（例 10m、600s）：{revert_after}")
        return 1
    seconds = _revert_after_seconds(revert_after)
    if seconds is None or seconds < MIN_REVERT_AFTER_SECONDS:
        report.fail("revert-after",
                    f"太短（{revert_after}）：切換最壞要幾分鐘，保險會在切換進行中"
                    f"就開跑；至少 {int(MIN_REVERT_AFTER_SECONDS / 60)}m")
        return 1
    old = OldLayout(old_home or layout.service_home)
    if inner:
        # --inner 只有 systemd-run 起的 transient unit 會帶（低-6）：人手在 ssh
        # 上照抄整串指令會讓切換失去分離執行，連線一斷就停在中間。
        if not layout.env.get(ENV_INVOCATION):
            report.fail("inner", "--inner 只給 lgw-cutover 的 transient unit 用"
                                 "（沒有 INVOCATION_ID；人手請直接跑 llmgwctl cutover）")
            return 1
        return _cutover_inner(layout, old, tag=tag, revert_after=revert_after,
                              no_remote=no_remote, report=report)
    # 中-4：進行中的階段在 outer 就拒絕（不要先排 transient，inner 才失敗）。
    phase = read_state(layout).get("phase")
    if phase in OPEN_PHASES:
        report.fail("cutover", f"狀態是 {phase}；不重複切換"
                                "（要開新一輪先跑 prepare-migration）")
        return 1
    problems = preflight(layout, tag, old=old)
    if problems:
        for problem in problems:
            report.fail("preflight", problem)
        report.fail("cutover", "前置條件沒過；不排程（舊服務完全沒動）")
        return 1
    argv = cutover_unit_argv(layout, tag=tag, revert_after=revert_after,
                             old_home=old.home, no_remote=no_remote)
    try:
        proc = runmod.run(argv, env=layout.child_env())
    except runmod.RunError as exc:
        report.fail("cutover", f"跑不起來 {argv[0]}（{exc.kind}）")
        return 1
    if proc.returncode != 0:
        report.fail("cutover", f"{argv[0]} rc={proc.returncode}（{CUTOVER_UNIT} 已存在？）")
        return 1
    report.ok("cutover", f"已排入 {CUTOVER_UNIT}（分離執行；"
                         f"{layout.journalctl} -u {CUTOVER_UNIT} 看結果）")
    return 0


def _cutover_inner(layout, old: OldLayout, *, tag: str, revert_after: str,
                   no_remote: bool, report: MigrationReport) -> int:
    """切換窗口真正的工作（跑在 systemd-run 起的 transient unit 裡）。

    與 rollback-to-user 共用同一把 flock（中-3）：保險提早到、或切換中有人
    手動回滾時，兩邊不會同時動 unit 與互相覆寫狀態。
    """
    lock = _Lock(cutover_lock_path(layout))
    try:
        if not lock.acquire(_wait_seconds(layout, ENV_LOCK_WAIT, DEFAULT_LOCK_WAIT)):
            report.fail("cutover", "另一個切換／回滾正在跑（等不到鎖）；不切")
            return 1
    except OSError as exc:
        report.fail("cutover", f"拿不到鎖（{type(exc).__name__}）：{lock.path}")
        return 1
    try:
        return _cutover_locked(layout, old, tag=tag, revert_after=revert_after,
                               no_remote=no_remote, report=report)
    finally:
        lock.release()


def _sync_final_cutover_data(layout, old: OldLayout, report) -> bool:
    """中-2：停完舊服務後、啟動新服務前，重新快照舊 llm.db 並同步更新的 keys.env / .keysync.sha。
    此時舊服務已停，沒有併發寫入者，可將 prepare 至 cutover 期間的冷卻與稽核資料完整帶過來。
    """
    # 1. 舊 llm.db 重新快照
    if os.path.isfile(old.db_file):
        if not _regular_file(old.db_file, old.home):
            _refuse_symlink(report, "sync-db", old.db_file)
            return False
        try:
            _copy_db_consistent(old.db_file, layout.db_file,
                                owner=_service_owner_ids(layout),
                                base=old.home)
            install._chown_service(layout, layout.db_file)
            report.ok("sync-db", "舊服務停止後重新快照 llm.db（包含最新冷卻與稽核）")
        except OSError as exc:
            report.fail("sync-db", f"重新快照 llm.db 失敗（{type(exc).__name__}）")
            return False

    # 2. 舊 keys.env 若較新則複製
    if os.path.isfile(old.keys_env):
        if not _regular_file(old.keys_env, old.home):
            _refuse_symlink(report, "sync-keys-env", old.keys_env)
            return False
        newer = True
        if os.path.isfile(layout.keys_env):
            try:
                newer = os.path.getmtime(old.keys_env) > os.path.getmtime(layout.keys_env)
            except OSError:
                newer = False
        if newer:
            try:
                _copy_into_place(old.keys_env, layout.keys_env, 0o600,
                                 owner=_service_owner_ids(layout),
                                 base=old.home)
                install._chown_service(layout, layout.keys_env)
                report.ok("sync-keys-env", "同步最新 keys.env")
            except OSError as exc:
                report.fail("sync-keys-env", f"同步 keys.env 失敗（{type(exc).__name__}）")
                return False

    # 3. 舊 .keysync.sha 若較新則複製
    if os.path.isfile(old.keysync_sha_file):
        if not _regular_file(old.keysync_sha_file, old.home):
            _refuse_symlink(report, "sync-keysync-sha", old.keysync_sha_file)
            return False
        newer = True
        if os.path.isfile(layout.keysync_sha_file):
            try:
                newer = os.path.getmtime(old.keysync_sha_file) > os.path.getmtime(layout.keysync_sha_file)
            except OSError:
                newer = False
        if newer:
            try:
                _copy_into_place(old.keysync_sha_file, layout.keysync_sha_file, 0o600,
                                 owner=_service_owner_ids(layout),
                                 base=old.home)
                install._chown_service(layout, layout.keysync_sha_file)
                report.ok("sync-keysync-sha", "同步最新 .keysync.sha")
            except OSError as exc:
                report.fail("sync-keysync-sha", f"同步 .keysync.sha 失敗（{type(exc).__name__}）")
                return False
    return True


def _cutover_locked(layout, old: OldLayout, *, tag: str, revert_after: str,
                    no_remote: bool, report: MigrationReport) -> int:
    state = read_state(layout)
    phase = state.get("phase")
    # 中-4：進行中／不可重來的階段一律拒絕；終態（reverted／aborted）歸檔後
    # 可以開新一輪（prepare 也會歸檔，這裡是沒重跑 prepare 的直切）。
    if phase in OPEN_PHASES:
        report.fail("cutover", f"狀態是 {phase}；不重複切換"
                                "（要開新一輪先跑 prepare-migration）")
        return 1
    if phase in (PHASE_REVERTED, PHASE_ABORTED):
        _start_new_round(layout, report)
    problems = preflight(layout, tag, old=old)
    if problems:
        for problem in problems:
            report.fail("preflight", problem)
        report.fail("cutover", "前置條件沒過；沒動舊服務")
        return 1

    # (a) 先排反向保險；排不起來就不切（保險是停損的最後一道）
    try:
        proc = runmod.run(insurance_unit_argv(layout, revert_after=revert_after,
                                              old_home=old.home),
                          env=layout.child_env())
    except runmod.RunError as exc:
        report.fail("insurance", f"排不起來（{exc.kind}）；不切")
        return 1
    if proc.returncode != 0:
        report.fail("insurance", f"systemd-run rc={proc.returncode}；不切")
        return 1
    try:
        write_state(layout, phase=PHASE_CUTOVER, tag=tag, revert_unit=REVERT_TIMER_UNIT,
                    old_home=old.home, new_service_ts=time.time())
    except OSError as exc:
        _cancel_revert(layout, REVERT_TIMER_UNIT, report)
        report.fail("insurance", f"寫 {cutover_state_path(layout)} 失敗"
                                 f"（{type(exc).__name__}）；不切")
        return 1
    report.ok("insurance", f"{REVERT_TIMER_UNIT}（{revert_after} 後沒 confirm 就自動回舊版）")

    # (b) 停舊 user 的服務與隧道，並且 disable：觀察期內重開機不得新舊雙跑（高-2）
    # 中-5：窗口內（(b) 舊已 disable、(c) 新還沒 enable）重開機什麼都不會起，
    # 保險也是 transient 的；這個警告要印在停舊**之前**。
    report.todo("reboot-window",
                "(b)～(e) 期間不要重開機：那時舊的已停＋disabled、新的還沒 "
                "enable，重開機後 API 與隧道都不會自己起（保險也沒了）")
    stopped: list[str] = []
    disabled: list[str] = []
    for unit in OLD_STOP_ORDER:
        outcome = _stop_unit(layout, report, "stop-old", unit, layer="user",
                             ok=f"{unit}（user 層）已停",
                             missing=f"{unit}（本來就沒在跑）")
        if outcome == "ok":
            stopped.append(unit)
        elif outcome == "failed":
            _restore_old_units(layout, disabled, stopped, report)
            _cancel_revert(layout, REVERT_TIMER_UNIT, report)
            try:
                write_state(layout, phase=PHASE_ABORTED, tag=tag, reason="stop-old")
            except OSError:
                pass
            report.fail("cutover", "停舊服務失敗，沒切換（已把停掉的叫回來）")
            return 1
        # 停掉的（含本來就沒在跑但檔案在的）一律 disable：重開機不再自動起。
        if outcome != "failed":
            disabled_outcome = _disable_unit(layout, report, "disable-old", unit,
                                             layer="user")
            if disabled_outcome == "failed":
                _restore_old_units(layout, disabled, stopped, report)
                _cancel_revert(layout, REVERT_TIMER_UNIT, report)
                try:
                    write_state(layout, phase=PHASE_ABORTED, tag=tag, reason="disable-old")
                except OSError:
                    pass
                report.fail("cutover", "舊 unit disable 不掉（重開機會雙跑）；沒切換")
                return 1
            disabled.append(unit)
    if not _confirm_old_units_stopped(layout, report):
        _restore_old_units(layout, disabled, stopped, report)
        _cancel_revert(layout, REVERT_TIMER_UNIT, report)
        try:
            write_state(layout, phase=PHASE_ABORTED, tag=tag, reason="old-still-active")
        except OSError:
            pass
        report.fail("cutover", "舊 unit 沒有真的停（is-active 不是 inactive）；沒切換")
        return 1
    report.ok("stopped-check", "舊 unit 都停了（is-active 逐一確認）")

    # 不可逆步驟前再確認一次狀態沒被回滾蓋掉（中-3）。
    if read_state(layout).get("phase") != PHASE_CUTOVER:
        _restore_old_units(layout, disabled, stopped, report)
        _cancel_revert(layout, REVERT_TIMER_UNIT, report)
        report.fail("cutover", "狀態在切換中被改掉（可能已被回滾）；中止")
        return 1

    # 中-2：停完舊服務後、啟動新服務前，重新快照舊 llm.db 並同步更新的 keys.env / .keysync.sha
    if not _sync_final_cutover_data(layout, old, report):
        _restore_old_units(layout, disabled, stopped, report)
        _cancel_revert(layout, REVERT_TIMER_UNIT, report)
        try:
            write_state(layout, phase=PHASE_ABORTED, tag=tag, reason="sync-data-failed")
        except OSError:
            pass
        report.fail("cutover", "同步舊服務最新資料失敗；中止切換並復原舊服務")
        return 1

    # (c) 啟新 unit（enable：觀察期內重開機也要起得來）
    # 中-1：切換開始時間要在啟動新服務**之前**記（之後 (e) 等 keysync 的門檻）。
    cutover_started = time.time()
    for unit in (SERVICE_NAME, TUNNEL_SERVICE_NAME):
        if not units.enable(layout, unit):
            return _stop_loss(layout, old, [f"{unit} enable 失敗"], report)
    if not units.start(layout, SERVICE_NAME):
        return _stop_loss(layout, old, [f"{SERVICE_NAME} 起不來"], report)
    report.ok("start", SERVICE_NAME)
    if not units.start(layout, TUNNEL_SERVICE_NAME):
        return _stop_loss(layout, old, [f"{TUNNEL_SERVICE_NAME} 起不來"], report)
    report.ok("start", TUNNEL_SERVICE_NAME)

    # (d) 本機 list 200（最多 60 秒）＋隧道真的 active＋經會合點探測（重試）
    problems: list[str] = []
    if probe.wait_local_ok(layout, _wait_seconds(layout, ENV_CUTOVER_WAIT,
                                                 DEFAULT_CUTOVER_WAIT)):
        report.ok("list-local", "200")
    else:
        report.fail("list-local", "時間內等不到 200")
        problems.append("本機 list 非 200")
    if not _wait_unit_active(layout, TUNNEL_SERVICE_NAME,
                             _wait_seconds(layout, ENV_TUNNEL_WAIT, DEFAULT_TUNNEL_WAIT)):
        report.fail("tunnel-active", "隧道 unit 時間內沒有 active")
        problems.append("隧道 unit 沒起來")
    else:
        report.ok("tunnel-active", TUNNEL_SERVICE_NAME)
    if no_remote:
        report.skip("list-remote", "--no-remote 略過")
    else:
        level, detail = _probe_remote_with_retry(layout)
        report.add("ok" if level == "ok" else level, "list-remote", detail)
        if level not in ("ok",):
            problems.append("經會合點非 200")
    if problems:
        return _stop_loss(layout, old, problems, report)

    # (e) check；它的 keysync 那項要等第一次拉取有結果才有意義（不然一定 todo）。
    # 中-1：門檻用 (c) 之前的切換開始時間，不是等待開始時間（不然正常情況一定
    # 等滿 grace 秒並印假 todo）。中-8：遠端已在 (d) 重試確認過，check 不再單探。
    if not _wait_file(layout.keysync_status_file,
                      _wait_seconds(layout, ENV_KEYSYNC_GRACE, DEFAULT_KEYSYNC_GRACE),
                      since=cutover_started):
        report.todo("keysync", "等不到這次切換後第一次拉取的結果（check 會照實報）")
    # 用 check 自己的 Report：遷移自己的 todo 不能讓 check 的結束碼變成 2
    # （兩者共用一份報告時，任何 todo 都會被當成「check 非 0」→ 假停損）。
    code = check.run(layout, no_remote=True)
    if code != 0:
        return _stop_loss(layout, old, [f"check 非 0（rc={code}）"], report)

    # 中-2：保險是一次性 transient timer：若切換拖過 revert-after，它已在切換中
    # 到點並死在等鎖上；這裡不確認就寫 pending，等於宣稱一個不存在的保險。
    if not units.is_active(layout, REVERT_TIMER_UNIT):
        try:
            write_state(layout, phase=PHASE_PENDING, tag=tag)
        except OSError:
            pass
        report.fail("insurance", f"{REVERT_TIMER_UNIT} 已經不在（保險在切換中"
                                 "到點了）；沒有自動回舊版")
        report.todo("no-insurance", "請立刻 llmgwctl confirm 或"
                                    " llmgwctl rollback-to-user")
        return 1

    try:
        write_state(layout, phase=PHASE_PENDING, tag=tag)
    except OSError as exc:
        return _stop_loss(layout, old, [f"寫狀態失敗（{type(exc).__name__}）"], report)
    report.ok("confirm-needed", "執行 llmgwctl confirm 取消自動回舊版")
    report.todo("insurance-reboot",
                "觀察期內不要重開機：lgw-revert.timer 是 transient，重開機後不會"
                "回來（新版會自己起、舊版已 disable，不會雙跑）；那時請立刻 "
                "llmgwctl confirm 或 llmgwctl rollback-to-user")
    return 0


def _confirm_old_units_stopped(layout, report) -> bool:
    """停完舊 unit 後逐一用 user manager 的 is-active 確認（中-2）。"""
    ok = True
    for unit in OLD_STOP_ORDER:
        state = _user_unit_state(layout, unit)
        if state in ("inactive", "missing"):
            continue
        report.fail("stopped-check", f"{unit} 還是 {state}；不切")
        ok = False
    return ok


def _probe_remote_with_retry(layout) -> tuple:
    """經會合點的探針：有間隔地重試（中-4），全部失敗才算停損。

    新隧道的 ssh 連線／reverse forward 建立要時間，舊隧道剛停時會合點的
    4710／4711 也可能還沒釋放；單次探測會把「還沒連上」判成「切換失敗」。
    """
    tries = _count_value(layout, ENV_REMOTE_TRIES, DEFAULT_REMOTE_TRIES)
    interval = _wait_seconds(layout, ENV_REMOTE_WAIT, DEFAULT_REMOTE_WAIT)
    level, detail = "fail", ""
    for attempt in range(tries):
        level, detail = probe.remote(layout)
        if level == "ok":
            if tries > 1:
                detail = f"{detail}（第 {attempt + 1}/{tries} 次）"
            return level, detail
        if attempt + 1 < tries:
            time.sleep(interval)
    return level, f"探了 {tries} 次，最後一次：{detail}"


def _stop_loss(layout, old: OldLayout, problems: list, report: MigrationReport) -> int:
    """停損成立：立刻回舊版（反向保險那一套邏輯，同一個 _rollback_locked）。

    inner 已經持有 flock，直接呼叫不拿鎖的版本（不然會與自己死結）。
    """
    report.fail("cutover", "停損成立（" + "、".join(problems) + "）；立刻回舊版")
    _rollback_locked(layout, old, report)
    return 1


def _wait_file(path: str, seconds: float, *, since: float | None = None) -> bool:
    """等一個檔出現（或比 ``since`` 新），最多 seconds 秒（0＝只試一次）。

    低-5：切換前留下的舊 ``.keysync.status`` 不能讓 90 秒等待立刻通過。
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if os.path.isfile(path):
            if since is None:
                return True
            try:
                if os.path.getmtime(path) >= float(since):
                    return True
            except OSError:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def _restore_old_units(layout, enable_units: list, start_units: list, report) -> None:
    """中止切換時，把 disable／停掉的舊 unit 復原（enable 全部、start 停過的）。"""
    for unit in enable_units:
        outcome, _ = _user_systemctl_outcome(layout, "enable", unit)
        if outcome in ("failed", "missing"):
            report.fail("enable-old", f"{unit} enable 不回來（重開機不會自動起）")
    for unit in start_units:
        outcome, detail = _user_systemctl_outcome(layout, "start", unit)
        if outcome == "ok":
            report.ok("start-old", f"{unit}（叫回來）")
        else:
            report.fail("start-old", f"{unit} 叫不回來（{detail or outcome}）")


def _cancel_revert(layout, unit: str, report) -> bool:
    """停掉反向保險的 timer；「找不到」當作已取消（confirm／回滾都這樣）。"""
    outcome, detail = _systemctl_outcome(layout, "stop", unit or REVERT_TIMER_UNIT)
    _report_outcome(report, "revert-cancel", outcome, detail,
                    ok=f"{unit} 已取消（不會自動回舊版）",
                    missing=f"{unit} 不在（當作已取消）",
                    failed=f"停不掉 {unit}（{detail}）")
    return outcome != "failed"


# -- confirm --------------------------------------------------------------


def confirm(layout, *, report: MigrationReport | None = None) -> int:
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return 1
    lock = _Lock(cutover_lock_path(layout))
    try:
        if not lock.acquire(_wait_seconds(layout, ENV_LOCK_WAIT, DEFAULT_LOCK_WAIT)):
            report.fail("confirm", "另一個切換／回滾正在跑（等不到鎖）；稍後再 confirm")
            return 1
    except OSError as exc:
        report.fail("confirm", f"拿不到鎖（{type(exc).__name__}）：{lock.path}")
        return 1
    try:
        return _confirm_locked(layout, report)
    finally:
        lock.release()


def _confirm_locked(layout, report) -> int:
    """confirm 的本體（已持有 flock）。"""
    state = read_state(layout)
    phase = state.get("phase")
    if phase != PHASE_PENDING:
        report.fail("confirm", f"狀態不是 {PHASE_PENDING}（現在："
                               f"{phase or '（沒有狀態檔）'}）；拒絕")
        return 1
    unit = _checked_state_unit(state.get("revert_unit"))
    if unit is None:
        report.fail("revert-unit", "狀態檔的 revert_unit 不是認得的值；拒絕"
                                  "（不會拿它去呼叫 systemctl）")
        return 1
    # 保險是不是已經開跑（中-9）：停 timer 不會停掉已經觸發的 service；先確認。
    if _system_unit_busy(layout, REVERT_UNIT + ".service"):
        report.fail("insurance-running",
                    f"{REVERT_UNIT}.service 已經在跑（保險正在回舊版）；"
                    "先等它跑完，不要 confirm")
        return 1
    # 此刻在跑的必須是新版、舊的不能還在跑（中-9）。
    if not units.is_active(layout, SERVICE_NAME):
        report.fail("confirm", f"{SERVICE_NAME} 不是 active（現在跑的不是新版？）；拒絕")
        return 1
    if _user_unit_state(layout, SERVICE_NAME) == "active":
        report.fail("confirm", f"舊 user 的 {SERVICE_NAME} 還在跑（新舊同時？）；拒絕")
        return 1
    if not _cancel_revert(layout, unit, report):
        report.fail("confirm", f"{unit} 停不掉；保險還在，先人工處理再 confirm")
        return 1
    # 低-13：停 timer 與 busy 檢查之間 timer 可能剛觸發；停完再驗一次（並看
    # failed——保險跑到一半失敗殘留時，狀態可能在半回滾，要人工確認）。
    if _system_unit_busy(layout, REVERT_UNIT + ".service"):
        report.fail("insurance-running",
                    f"{REVERT_UNIT}.service 停 timer 後還在跑（保險正在回舊版）；"
                    "不要 confirm，等它跑完看 status")
        return 1
    if _system_unit_failed(layout, REVERT_UNIT + ".service"):
        report.fail("insurance-failed",
                    f"{REVERT_UNIT}.service 是 failed（保險曾跑到一半失敗）；"
                    "先 llmgwctl status／journal -u lgw-revert.service 確認再 confirm")
        return 1
    try:
        write_state(layout, phase=PHASE_CONFIRMED, confirmed_ts=time.time())
    except OSError as exc:
        report.fail("confirm", f"寫狀態失敗（{type(exc).__name__}）")
        return 1
    days = _observe_seconds(layout) / SECONDS_PER_DAY
    report.ok("confirm", f"觀察期從現在起算（{days:g} 天；期間有事 llmgwctl rollback-to-user）")
    return 0


# -- rollback-to-user -----------------------------------------------------


def rollback_unit_argv(layout, *, old_home: str) -> list:
    """人手回舊版的分離執行 transient unit（高-1）。

    回舊版會停掉新的 ``llmgateway-tunnel.service``——那正是操作者正在走的
    管理連線（設計前提：管理連線經隧道進來）。人手直接跑會在「新服務已停、
    舊服務還沒回來」之間被 SIGHUP 殺掉，所以一律 ``systemd-run`` 分離執行，
    結果看 journal。
    """
    inner = [layout.python3, "-m", "lgwctl", "rollback-to-user",
             "--old-home", old_home, "--inner"]
    return [_tool(layout, ENV_SYSTEMD_RUN, "systemd-run"),
            f"--unit={ROLLBACK_UNIT}", "--collect", *transient_setenv_args(layout),
            f"--working-directory={layout.current_link}", *inner]


def rollback_to_user(layout, *, old_home: str | None = None, inner: bool = False,
                     report: MigrationReport | None = None) -> int:
    """回舊版（高-1：人手一律分離執行到 ``lgw-rollback``）。

    人手跑的這一次只排一個 transient unit 就結束（ssh 連線斷了也不影響）；
    真正的工作在 ``--inner``（要求 systemd 設的 ``INVOCATION_ID``，與 cutover
    同一套判斷），跑在 ``lgw-rollback`` unit 裡、輸出進 journal。
    """
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return 1
    old = OldLayout(old_home or layout.service_home)
    if inner:
        # 人手在 ssh 上照抄整串指令會讓回滾失去分離執行（連線一斷就停在中間，
        # 新隧道已停、舊 unit 還沒叫回來）；只有 transient unit 會帶 INVOCATION_ID。
        if not layout.env.get(ENV_INVOCATION):
            report.fail("inner", "--inner 只給 lgw-rollback 的 transient unit 用"
                                 "（沒有 INVOCATION_ID；人手請直接跑 "
                                 "llmgwctl rollback-to-user，它會分離執行）")
            return 1
        return _rollback_inner(layout, old, report)
    # 分離執行：立刻返回，操作者看重連後的 journal／status。
    argv = rollback_unit_argv(layout, old_home=old.home)
    try:
        proc = runmod.run(argv, env=layout.child_env())
    except runmod.RunError as exc:
        report.fail("rollback", f"排不起來 {argv[0]}（{exc.kind}）")
        return 1
    if proc.returncode != 0:
        report.fail("rollback", f"{argv[0]} rc={proc.returncode}（{ROLLBACK_UNIT} 已存在？）")
        return 1
    report.ok("rollback", f"已排入 {ROLLBACK_UNIT}（分離執行；"
                          f"{layout.journalctl} -u {ROLLBACK_UNIT} 看結果）")
    report.todo("rollback-wait", "等它跑完再看 status；期間不要動 unit（有 flock 互斥）")
    return 0


def _rollback_inner(layout, old: OldLayout, report) -> int:
    """回舊版真正的工作（跑在 ``lgw-rollback`` transient unit 裡；持 flock）。"""
    lock = _Lock(cutover_lock_path(layout))
    try:
        # 保險到點時切換通常還握著同一把鎖（中-2）：unit 裡等久一點（預設 5
        # 分鐘）；人工路徑與保險路徑走同一套（高-1）。
        if not lock.acquire(_wait_seconds(layout, ENV_LOCK_WAIT,
                                          DEFAULT_DETACHED_LOCK_WAIT)):
            report.fail("rollback", "另一個切換／回滾正在做（等不到鎖）；這次沒有回滾")
            return 1
    except OSError as exc:
        report.fail("rollback", f"拿不到鎖（{type(exc).__name__}：{lock.path}）")
        return 1
    try:
        return _rollback_locked(layout, old, report)
    finally:
        lock.release()


def _rollback_locked(layout, old: OldLayout, report) -> int:
    """回舊版的本體（呼叫方已持有 flock；停損時由 inner 直接呼叫）。

    順序（高-1）：先確認**舊 unit 檔都在**（finalize 後檔被搬走，那時停新版
    只會讓機器沒服務）→ restore db／keys → enable＋start 舊 unit（含
    key-sync.timer）→ 探本機 200，**之後才停新隧道**。新舊同綁 8480，所以
    新 service 要先停（讓出埠）才換舊的起來；新隧道最後才停——它承載管理
    連線，關鍵工作（舊版活回來）都完成後斷線也無妨。
    """
    state = read_state(layout)
    phase = state.get("phase")
    if phase == PHASE_FINALIZED:
        report.fail("rollback", "狀態是 finalized（舊 unit 與舊目錄已收走）；"
                                "不能回舊版（要新的就修新版或重裝）")
        return 1
    missing = [unit for unit in OLD_UNIT_NAMES
               if not os.path.isfile(old.unit_file(unit))]
    if missing:
        # 中-4／低-2：舊 unit 檔不在就停手——停掉新版只會讓機器沒有任何服務。
        report.fail("rollback", f"舊 unit 檔不在（{old.unit_dir} 的 "
                                f"{'、'.join(missing)}）；不動新 unit")
        return 1
    if not state:
        # 提示要求：無狀態檔時照常冪等回舊版，但不能印 fail 卻回 0。
        report.ok("rollback-state", "沒有進行中的切換（狀態檔不在）；照常回舊版")
    if phase == PHASE_REVERTED:
        # 已經回過（人工先回、保險後來才觸發）：再確認一次就好，不動資料——
        # 新的 llm.db 已經複製回去過，再複製一次只會蓋掉舊服務寫的東西。
        # 上一次可能停在「舊服務回來了、但舊隧道起不來，所以新隧道先留著」；
        # 修好後再跑一次就能補完（中-2）。
        report.ok("rollback", "狀態已經是 reverted（人工與保險重複觸發也安全）")
        started = _start_old_units(layout, report)
        ok = _wait_old_list(layout, report)
        if units.is_active(layout, TUNNEL_SERVICE_NAME):
            tunnel_ok = _switch_to_old_tunnel(layout, report, ok)
        else:
            tunnel_ok = _wait_old_tunnel(layout, report)
            if not tunnel_ok:
                report.fail("tunnel-old", "舊隧道仍然起不來；新隧道先留著，"
                                          "管理連線不會斷（修好後再跑一次 rollback-to-user）")
        _cancel_revert(layout, _state_revert_unit(state, report), report)
        try:
            write_state(layout, phase=PHASE_REVERTED,
                        local_ok=bool(ok and started),
                        tunnel_ok=bool(tunnel_ok and started))
        except OSError as exc:
            report.fail("rollback", f"寫狀態失敗（{type(exc).__name__}）")
        return 0 if (ok and started and tunnel_ok
                     and not getattr(report, "has_fail", False)) else 1

    # 停新 service（讓出 8480 埠）＋disable；新隧道留到確認舊服務後才停（中-2）。
    _stop_new_service(layout, report)
    _restore_db(layout, old, state, report)
    _restore_keys_env(layout, old, report)
    started = _start_old_units(layout, report)
    ok = _wait_old_list(layout, report)
    # 中-2：舊服務回來後才停新隧道並起舊隧道，驗證連續穩定與 probe.remote
    tunnel_ok = _switch_to_old_tunnel(layout, report, ok)
    _cancel_revert(layout, _state_revert_unit(state, report), report)
    try:
        write_state(layout, phase=PHASE_REVERTED, local_ok=bool(ok and started),
                    tunnel_ok=bool(tunnel_ok and ok))
    except OSError as exc:
        report.fail("rollback", f"寫狀態失敗（{type(exc).__name__}）")
    # 低-3：資料回復有 fail（db 備份失敗等）也要反映在結束碼。
    failed = getattr(report, "has_fail", False)
    return 0 if (ok and started and tunnel_ok and not failed) else 1


def _state_revert_unit(state: dict, report=None) -> str:
    """保險的 unit 名（只接受固定值；其他一律用常數，不進 systemctl，高-3）。

    狀態檔沒寫 ``revert_unit``（例如沒跑過 cutover 就人工回滾）不算錯誤：
    用固定的 ``lgw-revert.timer`` 即可（停不掉也只是「保險不存在」）。
    """
    unit = _checked_state_unit(state.get("revert_unit"))
    if unit is None:
        if report is not None and state.get("revert_unit"):
            report.fail("revert-unit",
                        "狀態檔的 revert_unit 不是認得的值；用固定的 "
                        f"{REVERT_TIMER_UNIT}（不拿它去呼叫 systemctl）")
        return REVERT_TIMER_UNIT
    return unit


def _stop_new_service(layout, report) -> None:
    """停（並 disable）新的 ``llmgateway.service``：讓出 8480 埠給舊服務。

    失敗不擋後面的回舊版（舊版優先），成敗最後看本機 list 200。
    """
    unit = SERVICE_NAME
    _stop_unit(layout, report, "stop-new", unit,
               ok=f"{unit}（system 層）已停",
               missing=f"{unit}（本來就沒在跑）")
    outcome, detail = _systemctl_outcome(layout, "disable", unit)
    _report_outcome(report, "disable-new", outcome, detail,
                    ok=f"{unit} 不再開機自動起",
                    missing=f"{unit}（本來就沒 enable）",
                    failed=f"disable {unit} 失敗（{detail}）")


def _stop_new_tunnel(layout, report) -> None:
    """最後才停新的隧道（高-1：它承載管理連線，舊服務活回來才斷）。"""
    unit = TUNNEL_SERVICE_NAME
    _stop_unit(layout, report, "stop-new", unit,
               ok=f"{unit}（system 層；管理連線到此為止）已停",
               missing=f"{unit}（本來就沒在跑）")
    outcome, detail = _systemctl_outcome(layout, "disable", unit)
    _report_outcome(report, "disable-new", outcome, detail,
                    ok=f"{unit} 不再開機自動起",
                    missing=f"{unit}（本來就沒 enable）",
                    failed=f"disable {unit} 失敗（{detail}）")


def _restore_db(layout, old: OldLayout, state: dict, report) -> None:
    """把新 state 的 ``llm.db`` 複製回舊 state（只在新的真的寫過時，中-6）。

    舊的先備份成帶時間戳的 ``llm.db.bak-YYYYmmddHHMMSS``（保留最近幾份）；
    複製走 SQLite backup API（回滾時同理，高-4）。
    """
    src, dst = layout.db_file, old.db_file
    if not os.path.isfile(src):
        report.skip("db-copy", f"{src} 不在（沒得複製）")
        return
    if not _db_source_ok(src):
        report.skip("db-copy", "新的 llm.db 還不是可用的資料庫（新服務沒寫過）；不覆蓋舊的")
        return
    started = _new_service_ts(state)
    if started is None:
        # 低-7：沒有切換時間就無法判斷新 db 是不是新服務寫的（prepare 快照的
        # mtime 是建立時間，可能比舊 db 新）。安全的一邊是不覆蓋。
        report.skip("db-copy", "狀態檔沒有切換時間（沒跑過切換？）；不覆蓋舊的")
        return
    if not _db_has_new_writes(started, src):
        report.skip("db-copy", "新的 llm.db 沒有比較新（新服務沒寫過），不覆蓋舊的")
        return
    if os.path.isfile(dst) and not _regular_file(dst, old.home):
        report.fail("db-copy", f"舊的 {dst} 或其上層目錄是 symlink；不覆蓋")
        return
    if os.path.isfile(dst):
        try:
            bak = _backup_old_db(layout, dst, old_home=old.home)
        except OSError as exc:
            report.fail("db-bak", f"舊 llm.db 備份失敗（{type(exc).__name__}）；不覆蓋")
            return
    else:
        bak = None
        report.skip("db-bak", f"{dst} 不在（沒有舊的可以備份）")
    try:
        _copy_db_consistent(src, dst, owner=_service_owner_ids(layout))
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("db-copy", f"複製失敗（{type(exc).__name__}）")
        return
    where = f"（舊的備份：{os.path.basename(bak)}）" if bak else ""
    report.ok("db-copy", f"新 llm.db → {dst}{where}")


def _new_service_ts(state: dict) -> float | None:
    """切換開始時間（狀態檔的 ``new_service_ts``）；沒有／壞值回 ``None``。"""
    started = state.get("new_service_ts")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return float(started)
    return None


def _db_has_new_writes(started: float, src: str) -> bool:
    """新的 db 是不是在切換開始之後被寫過（中-6）。

    呼叫方要先確認狀態檔有 ``new_service_ts``（低-7：沒有就無從判斷，不覆蓋）。
    """
    try:
        return os.path.getmtime(src) > started
    except OSError:
        return False


def _backup_old_db(layout, dst: str, old_home: str | None = None) -> str:
    """把舊 db 一致快照成帶時間戳的備份；失敗丟 OSError（呼叫方不覆蓋）。"""
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    bak = f"{dst}.bak-{stamp}"
    index = 1
    while _exists(bak):
        bak = f"{dst}.bak-{stamp}-{index}"
        index += 1
    _copy_db_consistent(dst, bak, owner=_service_owner_ids(layout), base=old_home)
    install._chown_service(layout, bak)
    _prune_db_backups(dst)
    return bak


def _restore_keys_env(layout, old: OldLayout, report) -> None:
    """``keys.env`` 不覆蓋舊的，除非新的比較新而且格式驗得過（設計 §5）。"""
    src, dst = layout.keys_env, old.keys_env
    if not os.path.isfile(src):
        report.skip("keys-env", f"{src} 不在（不覆蓋舊的）")
        return
    if os.path.isfile(dst):
        if not _regular_file(dst, old.home):
            report.fail("keys-env", f"舊的 {dst} 或其上層目錄是 symlink；不覆蓋")
            return
        try:
            newer = os.path.getmtime(src) > os.path.getmtime(dst)
        except OSError:
            newer = False
        if not newer:
            report.skip("keys-env", "新的沒有比較新，不覆蓋舊的")
            return
        if not _keys_env_valid(src):
            report.todo("keys-env", "新的 keys.env 格式驗不過，不覆蓋舊的；請本人確認")
            return
    try:
        _copy_into_place(src, dst, 0o600, owner=_service_owner_ids(layout))
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("keys-env", f"複製失敗（{type(exc).__name__}）")
        return
    report.ok("keys-env", "新的 keys.env 比較新且格式過，複製回舊 state")


def _start_old_units(layout, report) -> bool:
    """以 llmgw 身分（重新 enable 並）啟動舊 user unit。

    cutover 停舊 unit 時把四個都 disable（高-2，觀察期重開機不得雙跑）；
    回舊版時要全部 enable 回來（含 key-sync.timer，不然舊版的 key 不會再拉），
    先啟動舊服務與 key-sync.timer。舊隧道留待新隧道停止讓出會合點埠後才啟動（中-2）。
    """
    ok = True
    for unit in OLD_UNIT_NAMES:
        outcome, detail = _user_systemctl_outcome(layout, "enable", unit)
        if outcome == "failed":
            ok = False
            report.fail("enable-old", f"{unit} enable 不回來（重開機不會自動起）")
        elif outcome == "missing":
            # 低-2：unit 檔不在（finalize 搬走或被刪）時 enable 會 missing——
            # 印「開機會自動起」是錯的，而且接下來 start 一定失敗。
            ok = False
            report.fail("enable-old", f"{unit} 的 unit 檔不在（enable 算失敗）")
        else:
            report.ok("enable-old", f"{unit}（開機會自動起）")
    for unit in OLD_START_SERVICES:
        outcome, detail = _user_systemctl_outcome(layout, "start", unit)
        if outcome == "ok":
            report.ok("start-old", f"{unit}（user 層）")
        else:
            ok = False
            report.fail("start-old", f"{unit} 起不來（{detail or outcome}）")
    return ok


def _wait_old_list(layout, report) -> bool:
    ok = probe.wait_local_ok(layout, _wait_seconds(layout, ENV_CUTOVER_WAIT,
                                                   DEFAULT_CUTOVER_WAIT))
    if ok:
        report.ok("list-local", "200（舊服務）")
        return True
    report.fail("list-local", "舊服務時間內沒回來（本機 list 不是 200）")
    return False


def _user_unit_restarts(layout, unit: str) -> int | None:
    try:
        proc = units.user_run(layout, "show", unit, "-p", "NRestarts", "--value")
        if proc.returncode == 0:
            val = proc.stdout.strip()
            if val.isdigit():
                return int(val)
    except Exception:
        pass
    return None


def _switch_to_old_tunnel(layout, report, old_service_ok: bool) -> bool:
    """中-2：回滾時舊隧道與新隧道不能同時持有會合點的 4710/4711 埠。

    順序：
    1. 舊服務若未回來（list 200 失敗）：不切換隧道，新隧道維持運行（留住管理通道）並報錯。
    2. 記錄舊隧道 NRestarts。
    3. 停新隧道（先不 disable，讓出會合點埠，短暫空窗）。
    4. 啟舊隧道。
    5. 等舊隧道連續穩定 ≥15～20 秒且 NRestarts 不增。
    6. probe.remote 經會合點驗證回 200。
    7. 成功才 disable 新隧道。
    8. 失敗則將新隧道 start 回來（留住管理通道）並報錯。
    """
    unit = TUNNEL_SERVICE_NAME
    if not old_service_ok:
        report.fail("tunnel-old", "舊服務未恢復（本機 list 非 200），新隧道維持運行（管理連線未中斷）；修好後請再執行 rollback-to-user")
        return False

    initial_restarts = _user_unit_restarts(layout, unit)

    # 停新隧道（先不 disable，讓出會合點 4710/4711 埠）
    outcome, detail = _systemctl_outcome(layout, "stop", unit)
    _report_outcome(report, "stop-new-tunnel", outcome, detail,
                    ok=f"{unit}（system 層；短暫切換空窗，讓出會合點埠）已停",
                    missing=f"{unit}（本來就沒在跑）",
                    failed=f"停 {unit} 失敗（{detail}）")
    if outcome == "failed":
        report.fail("tunnel-old", "新隧道停止失敗；無法切換至舊隧道")
        return False

    # 啟動舊隧道
    outcome, detail = _user_systemctl_outcome(layout, "start", unit)
    if outcome != "ok":
        report.fail("start-old-tunnel", f"{unit} 起不來（{detail or outcome}）")
        _recover_new_tunnel(layout, report)
        return False
    report.ok("start-old-tunnel", f"{unit}（user 層）")

    # 等舊隧道連續穩定
    if not _wait_old_tunnel(layout, report, initial_restarts=initial_restarts):
        _user_systemctl_outcome(layout, "stop", unit)
        _recover_new_tunnel(layout, report)
        return False

    # 經會合點探測回 200
    level, detail = _probe_remote_with_retry(layout)
    if level not in ("ok",):
        report.fail("probe-old-tunnel", f"舊隧道遠端探針失敗（{detail}）")
        _user_systemctl_outcome(layout, "stop", unit)
        _recover_new_tunnel(layout, report)
        return False

    report.ok("tunnel-old", f"{unit}（user 層）已連續穩定 active 且遠端探針 200")

    # 成功才 disable 新隧道
    outcome, detail = _systemctl_outcome(layout, "disable", unit)
    _report_outcome(report, "disable-new-tunnel", outcome, detail,
                    ok=f"{unit} 不再開機自動起",
                    missing=f"{unit}（本來就沒 enable）",
                    failed=f"disable {unit} 失敗（{detail}）")
    return True


def _recover_new_tunnel(layout, report) -> None:
    """舊隧道失敗時把新隧道 start 回來，保留管理連線通道（中-2）。"""
    unit = TUNNEL_SERVICE_NAME
    outcome, detail = _systemctl_outcome(layout, "start", unit)
    _report_outcome(report, "recover-new-tunnel", outcome, detail,
                    ok=f"{unit}（system 層）已重新啟動（保留管理通道）",
                    missing=f"{unit} 不在",
                    failed=f"重新啟動 {unit} 失敗（{detail}）；管理通道中斷，請以 console 登入處理")
    report.fail("tunnel-old", "舊隧道未達連續穩定或探針非 200；已重新啟動新隧道留住管理通道，修好後再跑一次 rollback-to-user")


def _wait_old_tunnel(layout, report, initial_restarts: int | None = None) -> bool:
    """等舊 user 隧道連續穩定 active（高-1、中-4：它活了才切斷新的管理連線）。

    中-4：不能只取樣一次 is-active（crash-loop 會過）。需連續穩定一段時間
    （active 且 NRestarts 沒增加）才判定為 true。
    低-1：讀不到 NRestarts 時印一行 todo，預設穩定窗口拉長到 15 秒。
    """
    total_wait = _wait_seconds(layout, ENV_TUNNEL_WAIT, DEFAULT_TUNNEL_WAIT)
    stable_cfg = _wait_seconds(layout, ENV_TUNNEL_STABLE, DEFAULT_TUNNEL_STABLE)
    stable_required = min(total_wait, stable_cfg) if total_wait > 0 else 0.0
    deadline = time.monotonic() + total_wait

    if initial_restarts is None:
        initial_restarts = _user_unit_restarts(layout, TUNNEL_SERVICE_NAME)
    if initial_restarts is None:
        report.todo("tunnel-restarts", "無法讀取舊隧道 NRestarts，僅以連續 is-active 判定穩定")

    active_since: float | None = None
    crash_loop_detected = False

    while True:
        state = _user_unit_state(layout, TUNNEL_SERVICE_NAME)
        current_restarts = _user_unit_restarts(layout, TUNNEL_SERVICE_NAME)
        restarts_increased = False
        if initial_restarts is not None and current_restarts is not None:
            if current_restarts > initial_restarts:
                restarts_increased = True
                crash_loop_detected = True
                initial_restarts = current_restarts

        if state == "active" and not restarts_increased:
            now = time.monotonic()
            if active_since is None:
                active_since = now
            if (now - active_since) >= stable_required:
                return True
        else:
            active_since = None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = f"（{state}）"
            if crash_loop_detected:
                detail += "（偵測到重啟崩潰迴圈 crash-loop，NRestarts 曾增加）"
            report.fail("tunnel-old", f"{TUNNEL_SERVICE_NAME}（user 層）未達連續穩定 active {detail}")
            return False
        time.sleep(min(0.5, remaining))


# -- finalize-migration ---------------------------------------------------


def finalize(layout, *, old_home: str | None = None, yes: bool = False,
             report: MigrationReport | None = None) -> int:
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return 1
    if not yes:
        report.fail("finalize", "要 --yes 才會動舊的 unit 與目錄（先看 llmgwctl status）")
        return 1
    # 低-4：finalize 全程拿同一把 flock（與 confirm／cutover／rollback 互斥），
    # 搬舊目錄與 rollback 同時跑會把狀態與檔案搬移交錯。
    lock = _Lock(cutover_lock_path(layout))
    try:
        if not lock.acquire(_wait_seconds(layout, ENV_LOCK_WAIT, DEFAULT_LOCK_WAIT)):
            report.fail("finalize", "另一個切換／回滾正在跑（等不到鎖）；稍後再 finalize")
            return 1
    except OSError as exc:
        report.fail("finalize", f"拿不到鎖（{type(exc).__name__}）：{lock.path}")
        return 1
    try:
        return _finalize_locked(layout, old_home=old_home, report=report)
    finally:
        lock.release()


def _finalize_locked(layout, *, old_home: str | None, report) -> int:
    """finalize 的本體（已持有 flock）。"""
    state = read_state(layout)
    phase = state.get("phase")
    if phase != PHASE_CONFIRMED:
        report.fail("finalize", f"狀態不是 {PHASE_CONFIRMED}（現在："
                                f"{phase or '（沒有狀態檔）'}）；拒絕")
        return 1
    started = state.get("confirmed_ts")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        report.fail("finalize", "狀態檔沒有觀察期起始時間；拒絕")
        return 1
    observe = _observe_seconds(layout)
    age = time.time() - float(started)
    if age < observe:
        report.fail("finalize", f"觀察期還沒過（還要 {_duration_text(observe - age)}）；拒絕")
        return 1

    # 收尾的目標目錄要和切換時綁定（低-3）：命令列打錯不能搬錯或什麼都沒搬。
    recorded_home = state.get("old_home")
    if old_home:
        if isinstance(recorded_home, str) and recorded_home \
                and os.path.realpath(old_home) != os.path.realpath(recorded_home):
            report.fail("old-home",
                        f"--old-home {old_home} 與切換時記錄的 {recorded_home} 不一致；拒絕")
            return 1
        home = old_home
    elif isinstance(recorded_home, str) and recorded_home:
        home = recorded_home
    else:
        home = layout.service_home
    old = OldLayout(home)
    # 新版此刻要健康才收舊版（低-3）：壞掉時該做的是 rollback-to-user，不是
    # 把舊目錄搬走。低-5：只探本機 list 不夠（隧道壞了、key 拉取掛了也照樣
    # 收），要跑完整 check（含遠端；人在 console 就加 --no-remote 重跑）。
    if not probe.wait_local_ok(layout, _wait_seconds(layout, ENV_CUTOVER_WAIT,
                                                     DEFAULT_CUTOVER_WAIT)):
        report.fail("list-local", "不是 200")
        report.fail("finalize", "新版本機 list 不是 200；先別收舊版"
                                "（要回舊版就跑 llmgwctl rollback-to-user）")
        return 1
    report.ok("list-local", "200（新版健康，才收舊版）")
    code = check.run(layout)
    if code != 0:
        report.fail("check", f"check 非 0（rc={code}）；先別收舊版"
                             "（隧道壞了就 rollback-to-user，人在 console 可 "
                             "check --no-remote 先看本機）")
        return 1
    # 舊的收掉：先停再 disable（拿不到 user manager 只記 todo，檔案照樣搬——
    # 狀態是 confirmed 代表新版才是活的那一套）
    for unit in OLD_STOP_ORDER:
        outcome, detail = _user_systemctl_outcome(layout, "stop", unit)
        _report_outcome(report, "stop-old", outcome, detail,
                        ok=f"{unit}（user 層）已停",
                        missing=f"{unit}（本來就沒在跑）",
                        failed=f"{unit} 停不掉（{detail}）；請人工確認（檔案還是會搬）")
        outcome, detail = _user_systemctl_outcome(layout, "disable", unit)
        _report_outcome(report, "disable-old", outcome, detail,
                        ok=f"{unit} 不再自動起",
                        missing=f"{unit}（本來就沒 enable）",
                        failed=f"{unit} disable 不掉（{detail}）")

    archive = os.path.join(layout.state_dir, ARCHIVE_DIR_NAME)
    moved_ok = True
    for name, src in (("move-share", old.share_dir), ("move-conf", old.conf_dir),
                      ("move-state", old.state_dir)):
        moved_ok = _move_step(src, os.path.join(archive, os.path.relpath(src, old.home)),
                              report, name) and moved_ok
    for name in OLD_UNIT_NAMES:
        src = old.unit_file(name)
        dst = os.path.join(archive, *OLD_UNIT_REL, name)
        moved_ok = _move_step(src, dst, report, "move-unit") and moved_ok
    if not moved_ok:
        report.fail("finalize", "有東西搬不動；狀態留在 confirmed，修好再跑一次")
        return 1

    outcome, detail = _user_systemctl_outcome(layout, "daemon-reload")
    if outcome == "failed":
        report.todo("daemon-reload-user", f"user manager 沒吃到（{detail}）")
    else:
        report.ok("daemon-reload-user", "舊 unit 已從 user manager 拿掉")
    _disable_linger(layout, report)
    try:
        write_state(layout, phase=PHASE_FINALIZED)
    except OSError as exc:
        report.fail("finalize", f"寫狀態失敗（{type(exc).__name__}）")
        return 1
    report.ok("finalize", f"舊 unit 與舊目錄搬到 {archive}（沒有刪除）")
    return report.exit_code(todo_ok=True)


def _move_step(src: str, dst: str, report, name: str) -> bool:
    """搬移（不刪除）：來源不在、或目標已經有了，都算 skip。"""
    if not _exists(src):
        report.skip(name, f"{src} 不在（沒得搬）")
        return True
    if _exists(dst):
        report.skip(name, f"{dst} 已經有了（搬過）")
        return True
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
    except OSError as exc:
        report.fail(name, f"搬移失敗（{type(exc).__name__}：{src}）")
        return False
    report.ok(name, f"{src} → {dst}")
    return True


def _disable_linger(layout, report) -> bool:
    tool = _tool(layout, ENV_LOGINCTL, "loginctl")
    try:
        proc = runmod.run([tool, "disable-linger", layout.service_user],
                          env=layout.child_env())
    except runmod.RunError as exc:
        report.todo("disable-linger",
                    f"跑不起來（{exc.kind}）；請人工 {tool} disable-linger "
                    f"{layout.service_user}")
        return False
    if proc.returncode == 0:
        report.ok("disable-linger", f"{layout.service_user} 不再常駐 user manager")
        return True
    report.todo("disable-linger", f"{tool} rc={proc.returncode}；請人工確認")
    return False


# -- 子指令分派 -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmgwctl",
        description="LLMGateway 遷移（user 版 → system 版；#35 設計 §5）。"
                    " 人手下的只有 prepare 與 cutover 的那一下，其餘照輸出做。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare-migration",
                       help="真機準備：放新佈局（不 enable、不 start）＋複製舊 key 與設定")
    p.add_argument("--tag", required=True, help="新版本的 tag（lgw-vX.Y.Z）")
    p.add_argument("--old-home", default=None, help="舊佈局的 HOME（預設服務帳號的 HOME）")
    p.add_argument("--force", action="store_true", help="目標已存在也覆蓋（預設不覆蓋）")

    p = sub.add_parser("cutover", help="切換窗口：排分離執行的 transient unit（先排反向保險）")
    p.add_argument("--tag", required=True, help="新版本的 tag（lgw-vX.Y.Z）")
    p.add_argument("--revert-after", default=DEFAULT_REVERT_AFTER,
                   help=f"反向保險的等待時間（systemd 時間格式，預設 {DEFAULT_REVERT_AFTER}）")
    p.add_argument("--old-home", default=None, help="舊佈局的 HOME（預設服務帳號的 HOME）")
    p.add_argument("--no-remote", action="store_true", help="不探經會合點（該項停損略過）")
    p.add_argument("--inner", action="store_true",
                   help=f"給 {CUTOVER_UNIT} 的 transient unit 用（人手不用加）")

    sub.add_parser("confirm", help="取消反向保險、進入觀察期（狀態要 pending-confirm）")

    p = sub.add_parser("rollback-to-user",
                       help=f"回舊版：人手一律分離執行到 {ROLLBACK_UNIT}（會斷你正在走的隧道）")
    p.add_argument("--old-home", default=None, help="舊佈局的 HOME（預設服務帳號的 HOME）")
    p.add_argument("--inner", action="store_true",
                   help=f"給 {ROLLBACK_UNIT} 的 transient unit 用（人手不用加）")

    p = sub.add_parser("finalize-migration",
                       help="觀察期後收尾：搬移舊 unit 與目錄（不刪除）、disable-linger")
    p.add_argument("--old-home", default=None, help="舊佈局的 HOME（預設服務帳號的 HOME）")
    p.add_argument("--yes", action="store_true", help="確定要搬移（沒給就拒絕）")
    return parser


def main(argv=None, env=None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    layout = Layout.from_env(env)
    report = MigrationReport()
    if args.command == "prepare-migration":
        return prepare(layout, tag=args.tag, old_home=args.old_home, force=args.force,
                       report=report)
    if args.command == "cutover":
        return cutover(layout, tag=args.tag, revert_after=args.revert_after,
                       old_home=args.old_home, no_remote=args.no_remote,
                       inner=args.inner, report=report)
    if args.command == "confirm":
        return confirm(layout, report=report)
    if args.command == "rollback-to-user":
        return rollback_to_user(layout, old_home=args.old_home, inner=args.inner,
                                report=report)
    if args.command == "finalize-migration":
        return finalize(layout, old_home=args.old_home, yes=args.yes, report=report)
    raise AssertionError(f"unhandled command: {args.command}")
