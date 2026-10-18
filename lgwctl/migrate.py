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
  ``--inner``（transient unit 帶的參數，人手不用加）：
  (a) 先排**反向保險** ``systemd-run --unit=lgw-revert --on-active=<revert-after>
  … rollback-to-user``（單元名記到 ``.cutover.state``）；排不起來就不切；
  (b) 以 ``runuser -u llmgw --``（自帶 XDG_RUNTIME_DIR／DBUS_SESSION_BUS_ADDRESS）
  停舊 user 的服務與隧道；
  (c) enable＋start 兩個新 unit；
  (d) 等最多 60 秒本機 list 200、再經會合點探一次（``--no-remote`` 略過）；
  (e) 任一不過就立刻 ``rollback-to-user``；全過寫 ``pending-confirm`` 並印
  ``執行 llmgwctl confirm 取消自動回舊版``。
- ``confirm``：停掉反向保險的 timer、狀態改 ``confirmed``（觀察期從這裡起算）。
  狀態不是 ``pending-confirm`` 就拒絕並非零結束。
- ``rollback-to-user [--old-home D]``：停（並 disable）新 system unit、把新的
  ``llm.db`` 複製回舊 state（舊的先備份 ``.bak``；``keys.env`` 只在新的比較新且
  格式驗得過時才覆蓋）、以 llmgw 身分啟動舊 user unit、探本機 list 200、停反向
  保險、狀態寫 ``reverted``。用 flock 互斥，所以反向保險與人工同時觸發也安全。
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
import pwd
import re
import shutil
import sqlite3
import sys
import tempfile
import time

from . import atomic, check, install, probe, release, units
from . import run as runmod
from .layout import (
    DEFAULT_API_PORT,
    SERVICE_NAME,
    TAG_PATTERN,
    TAG_RE,
    TUNNEL_SERVICE_NAME,
    Layout,
    parse_env_file,
)
from .report import Report

# -- 固定值 ---------------------------------------------------------------

CUTOVER_UNIT = "lgw-cutover"
REVERT_UNIT = "lgw-revert"
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
OLD_UNIT_REL = (".config", "systemd", "user")
OLD_KEYSYNC_ENV_NAME = "keysync.env"
OLD_KEYSYNC_SERVICE = "key-sync.service"
OLD_KEYSYNC_TIMER = "key-sync.timer"

# 停舊 unit 的順序：隧道最後停（它承載管理連線，這樣中斷最短）。
OLD_STOP_ORDER = (SERVICE_NAME, OLD_KEYSYNC_TIMER, OLD_KEYSYNC_SERVICE, TUNNEL_SERVICE_NAME)
# 啟動舊 unit 的順序：先服務再隧道（探針要 API 先起來）。
OLD_START_ORDER = (SERVICE_NAME, TUNNEL_SERVICE_NAME)
OLD_UNIT_NAMES = (SERVICE_NAME, TUNNEL_SERVICE_NAME, OLD_KEYSYNC_SERVICE, OLD_KEYSYNC_TIMER)

# 狀態檔的階段（字串會寫進 .cutover.state，測試也會看）。
PHASE_CUTOVER = "cutover-started"
PHASE_PENDING = "pending-confirm"
PHASE_CONFIRMED = "confirmed"
PHASE_REVERTED = "reverted"
PHASE_FINALIZED = "finalized"
PHASE_ABORTED = "aborted"

# 狀態檔裡的 unit 名是不可信輸入（服務帳號以前寫得到那個檔）：只接受保險
# 自己的兩個固定值，其他一律不當 systemctl 的參數（高-3）。
STATE_UNIT_NAMES = (REVERT_TIMER_UNIT, REVERT_UNIT + ".service")

# 環境變數（測試用的假指令與可調的等待）。
ENV_SYSTEMD_RUN = "LLMGW_SYSTEMD_RUN"
ENV_RUNUSER = "LLMGW_RUNUSER"
ENV_LOGINCTL = "LLMGW_LOGINCTL"
ENV_CUTOVER_WAIT = "LLMGW_CUTOVER_WAIT"
ENV_KEYSYNC_GRACE = "LLMGW_KEYSYNC_GRACE"
ENV_OBSERVE_DAYS = "LLMGW_OBSERVE_DAYS"

DEFAULT_CUTOVER_WAIT = 60.0
DEFAULT_KEYSYNC_GRACE = 90.0
DEFAULT_OBSERVE_DAYS = 3.0
SECONDS_PER_DAY = 86400.0
MAX_KEYS_ENV_BYTES = 1 << 20
# llm.db 一致快照：SQLite backup API 重試次數與保留的備份份數（中-6）。
DB_COPY_ATTEMPTS = 3
DB_BACKUP_KEEP = 3
SQLITE_HEADER = b"SQLite format 3\x00"

# systemctl「這個 unit 根本不在」的訊息（停舊 unit、取消保險時要分得出來）。
_UNIT_MISSING_MARKERS = ("not loaded", "not found", "No such file", "could not be found")

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


def _copy_into_place(src: str, dst: str, mode: int | None = None) -> None:
    """同目錄暫存 → rename（原子）；不給 mode 就沿用來源權限（cp -a 語意）。"""
    directory = os.path.dirname(dst) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=directory)
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dst)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _db_source_ok(path: str) -> bool:
    """是不是一個 SQLite 資料庫（backup API 的來源一定要是）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _sqlite_backup(src: str, dst: str) -> None:
    """用 SQLite 的線上備份 API 從 src 複製到 dst（舊服務還在寫也安全，高-4）。"""
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _db_integrity_ok(path: str) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == "ok"


def _copy_db_consistent(src: str, dst: str, mode: int = 0o600) -> None:
    """一致快照到 dst（同目錄暫存 → rename）；驗 integrity_check，失敗重試再放棄。

    不沿用 ``shutil.copy2``：rollback journal 模式的主檔在寫入交易期間是就地
    修改的，直接複製會拿到新舊頁混合的檔案（高-4）。``Connection.backup`` 對
    活著的寫入者安全（讀交易看到的是已提交的狀態）。
    """
    if not _db_source_ok(src):
        raise OSError(f"{src} 不是 SQLite 資料庫")
    directory = os.path.dirname(dst) or "."
    os.makedirs(directory, exist_ok=True)
    last: Exception | None = None
    for _ in range(DB_COPY_ATTEMPTS):
        fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=directory)
        os.close(fd)
        try:
            _sqlite_backup(src, tmp)
            if not _db_integrity_ok(tmp):
                raise sqlite3.DatabaseError("integrity_check 不是 ok")
            os.chmod(tmp, mode)
            os.replace(tmp, dst)
            tmp = None
            return
        except (OSError, sqlite3.Error) as exc:
            last = exc
        finally:
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
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == install.KEYS_PASS_VAR and value.strip():
            return True
    return False


def _keys_env_valid(path: str) -> bool:
    """新的 ``keys.env`` 「格式驗得過」：KEY=VALUE 的行、至少一個有值、不含 NUL。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_KEYS_ENV_BYTES)
    except OSError:
        return False
    if not raw or b"\x00" in raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    saw_value = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not _ENV_NAME_RE.match(name.strip()):
            return False
        if value.strip():
            saw_value = True
    return saw_value


def _chown_root_group(layout, path: str) -> None:
    """``keys.pass``（root:llmgw 0640）：owner 是 root、group 是服務帳號。"""
    if layout.prefix or layout.euid != 0:
        return
    try:
        gid = pwd.getpwnam(layout.service_user).pw_gid
    except KeyError:
        gid = 0
    os.chown(path, 0, gid)


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

    互斥由呼叫方的 ``_Lock`` 負責（confirm／inner／rollback／finalize 都在鎖內
    寫）；這裡只做 read-merge-write。
    """
    data = read_state(layout)
    data.update(fields)
    data["ts"] = time.time()
    _ensure_ctl_dir(layout)
    atomic.write_text(cutover_state_path(layout), json.dumps(data, sort_keys=True) + "\n",
                      0o600)
    return data


def _checked_state_unit(value) -> str | None:
    """狀態檔裡的 unit 名只接受保險的固定值（高-3：不可信輸入）。"""
    name = str(value or "")
    return name if name in STATE_UNIT_NAMES else None


class _Lock:
    """``flock`` 互斥鎖（反向保險的行程與人工同時回舊版時只有一個真的做事）。"""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
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
    """把一個 systemctl 的結果分成 ok｜missing（unit 不在）｜failed。"""
    if proc.returncode == 0:
        return "ok", ""
    text = (proc.stderr or "") + (proc.stdout or "")
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
    測試換成假指令。
    """
    uid = _user_uid(layout)
    return [_tool(layout, ENV_RUNUSER, "runuser"), "-u", layout.service_user, "--",
            "env",
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            layout.systemctl, "--user", *args]


def _user_uid(layout) -> int:
    try:
        return pwd.getpwnam(layout.service_user).pw_uid
    except KeyError:
        return os.geteuid()


def _user_systemctl_outcome(layout, *args) -> tuple:
    argv = user_systemctl_argv(layout, *args)
    try:
        proc = runmod.run(argv, env=layout.child_env())
    except runmod.RunError as exc:
        return "failed", exc.kind
    return _judge(proc)


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

    if not install._step_account(layout, report):
        return report.exit_code(todo_ok=True)
    if not install._step_dirs(layout, report):
        return report.exit_code(todo_ok=True)
    tag_dir = release.fetch(layout, tag, None, report)
    if tag_dir is None:
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


def _step_keys_env(layout, old: OldLayout, report, *, force: bool) -> None:
    dst = layout.keys_env
    if _exists(dst) and not force and os.path.getsize(dst) > 0:
        report.skip("state-keys-env", f"{dst} 已存在（不覆蓋）")
        return
    if not os.path.isfile(old.keys_env):
        if not _exists(dst):
            try:
                atomic.write_text(dst, "", 0o600)
                install._chown_service(layout, dst)
            except OSError as exc:
                report.fail("state-keys-env", f"建空檔失敗（{type(exc).__name__}）")
                return
        report.todo("state-keys-env",
                    f"舊的 {old.keys_env} 不在；新版啟動後由 key 拉取補（或本人確認）")
        return
    try:
        _copy_into_place(old.keys_env, dst, 0o600)
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("state-keys-env", f"複製失敗（{type(exc).__name__}）")
        return
    report.ok("state-keys-env", "從舊 state 複製（0600、llmgw）")


def _step_db(layout, old: OldLayout, report, *, force: bool) -> None:
    dst = layout.db_file
    if _exists(dst) and not force:
        report.skip("state-db", f"{dst} 已存在（不覆蓋）")
        return
    if not os.path.isfile(old.db_file):
        report.todo("state-db", f"舊的 {old.db_file} 不在；新服務會自己建")
        return
    try:
        _copy_db_consistent(old.db_file, dst)
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("state-db", f"一致快照失敗（{type(exc).__name__}）")
        return
    report.ok("state-db", "從舊 state 一致快照 llm.db（舊服務還在寫也安全）")


def _step_env_file(layout, old: OldLayout, report, *, force: bool, name: str,
                   src: str, dst: str, template: str) -> None:
    if _exists(dst) and not force:
        report.skip(name, f"{dst} 已存在（不覆蓋）")
        return
    if not os.path.isfile(src):
        try:
            atomic.write_text(dst, template, 0o644)
            install._chown_service(layout, dst)
        except OSError as exc:
            report.fail(name, f"寫範本失敗（{type(exc).__name__}）")
            return
        report.todo(name, f"舊的 {src} 不在；放了範本，要本人確認")
        return
    try:
        # cp -a 語意：權限沿用舊檔（舊 tunnel.env 是 0600），擁有者給服務帳號
        # （system unit 以 llmgw 跑，要讀得到）。IDENTITY／KNOWN_HOSTS 是絕對
        # 路徑，原樣照抄、不改寫。
        _copy_into_place(src, dst)
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail(name, f"複製失敗（{type(exc).__name__}）")
        return
    report.ok(name, f"從舊 {src} 複製（權限沿用）")


def _step_keys_pass(layout, old: OldLayout, report, *, force: bool) -> None:
    dst = layout.keys_pass_file
    if not force and _has_password(dst):
        report.skip("keys-pass", f"{dst} 已有值（不覆蓋）")
        return
    value = parse_env_file(old.keysync_env_file).get(install.KEYS_PASS_VAR, "")
    if not value:
        if not _exists(dst):
            try:
                atomic.write_text(dst, install.KEYS_PASS_TEMPLATE, 0o640)
                _chown_root_group(layout, dst)
            except OSError as exc:
                report.fail("keys-pass", f"建空殼失敗（{type(exc).__name__}）")
                return
        report.todo("keys-pass",
                    f"舊的 {old.keysync_env_file} 沒有非空的 {install.KEYS_PASS_VAR}；"
                    f"要本人填進 {dst}")
        return
    try:
        atomic.write_text(dst, f"{install.KEYS_PASS_VAR}={value}\n", 0o640)
        _chown_root_group(layout, dst)
    except OSError as exc:
        report.fail("keys-pass", f"寫失敗（{type(exc).__name__}）")
        return
    report.ok("keys-pass",
              f"從舊 {OLD_KEYSYNC_ENV_NAME} 複製一行到 {dst}（root:llmgw 0640；值不印出）")


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
            f"--unit={CUTOVER_UNIT}", "--collect",
            f"--working-directory={layout.current_link}", *inner]


def insurance_unit_argv(layout, *, revert_after: str, old_home: str) -> list:
    """反向保險：時間到就跑 ``rollback-to-user``（system 層的 transient timer）。

    這是停損的最後一道：沒人 confirm 就自己回舊版。
    """
    return [_tool(layout, ENV_SYSTEMD_RUN, "systemd-run"),
            f"--unit={REVERT_UNIT}", f"--on-active={revert_after}", "--collect",
            f"--working-directory={layout.current_link}",
            layout.python3, "-m", "lgwctl", "rollback-to-user",
            "--old-home", old_home]


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
    old = OldLayout(old_home or layout.service_home)
    if inner:
        return _cutover_inner(layout, old, tag=tag, revert_after=revert_after,
                              no_remote=no_remote, report=report)
    if layout.installed_tag() != tag:
        report.fail("cutover", f"current 不是 {tag}；先跑 prepare-migration --tag {tag}")
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
    """切換窗口真正的工作（跑在 systemd-run 起的 transient unit 裡）。"""
    if layout.installed_tag() != tag:
        report.fail("cutover", f"current 不是 {tag}；先跑 prepare-migration --tag {tag}")
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

    # (b) 停舊 user 的服務與隧道（root 代做，runuser 自帶 XDG／DBUS）
    stopped: list[str] = []
    for unit in OLD_STOP_ORDER:
        outcome = _stop_unit(layout, report, "stop-old", unit, layer="user",
                             ok=f"{unit}（user 層）已停",
                             missing=f"{unit}（本來就沒在跑）")
        if outcome == "ok":
            stopped.append(unit)
        elif outcome == "failed":
            _restore_old_units(layout, stopped, report)
            _cancel_revert(layout, REVERT_TIMER_UNIT, report)
            try:
                write_state(layout, phase=PHASE_ABORTED, tag=tag, reason="stop-old")
            except OSError:
                pass
            report.fail("cutover", "停舊服務失敗，沒切換（已把停掉的叫回來）")
            return 1

    # (c) 啟新 unit（enable：觀察期內重開機也要起得來）
    for unit in (SERVICE_NAME, TUNNEL_SERVICE_NAME):
        if not units.enable(layout, unit):
            return _stop_loss(layout, old, [f"{unit} enable 失敗"], report)
    if not units.start(layout, SERVICE_NAME):
        return _stop_loss(layout, old, [f"{SERVICE_NAME} 起不來"], report)
    report.ok("start", SERVICE_NAME)
    if not units.start(layout, TUNNEL_SERVICE_NAME):
        return _stop_loss(layout, old, [f"{TUNNEL_SERVICE_NAME} 起不來"], report)
    report.ok("start", TUNNEL_SERVICE_NAME)

    # (d) 本機 list 200（最多 60 秒）＋經會合點探一次
    problems: list[str] = []
    if probe.wait_local_ok(layout, _wait_seconds(layout, ENV_CUTOVER_WAIT,
                                                 DEFAULT_CUTOVER_WAIT)):
        report.ok("list-local", "200")
    else:
        report.fail("list-local", "時間內等不到 200")
        problems.append("本機 list 非 200")
    if no_remote:
        report.skip("list-remote", "--no-remote 略過")
    else:
        level, detail = probe.remote(layout)
        report.add("ok" if level == "ok" else level, "list-remote", detail)
        if level not in ("ok",):
            problems.append("經會合點非 200")
    if problems:
        return _stop_loss(layout, old, problems, report)

    # (e) check；它的 keysync 那項要等第一次拉取有結果才有意義（不然一定 todo）
    if not _wait_file(layout.keysync_status_file,
                      _wait_seconds(layout, ENV_KEYSYNC_GRACE, DEFAULT_KEYSYNC_GRACE)):
        report.todo("keysync", "等不到第一次拉取的結果（check 會照實報）")
    code = check.run(layout, report=report, no_remote=no_remote)
    if code != 0:
        return _stop_loss(layout, old, [f"check 非 0（rc={code}）"], report)

    try:
        write_state(layout, phase=PHASE_PENDING, tag=tag)
    except OSError as exc:
        return _stop_loss(layout, old, [f"寫狀態失敗（{type(exc).__name__}）"], report)
    report.ok("confirm-needed", "執行 llmgwctl confirm 取消自動回舊版")
    return 0


def _stop_loss(layout, old: OldLayout, problems: list, report: MigrationReport) -> int:
    """停損成立：立刻回舊版（反向保險那一套邏輯，同一個 rollback-to-user）。"""
    report.fail("cutover", "停損成立（" + "、".join(problems) + "）；立刻回舊版")
    rollback_to_user(layout, old_home=old.home, report=report)
    return 1


def _wait_file(path: str, seconds: float) -> bool:
    """等一個檔出現，最多 seconds 秒（0＝只試一次）。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if os.path.isfile(path):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def _restore_old_units(layout, units_list: list, report) -> None:
    """中止切換時，把剛剛停掉的舊 unit 叫回來（best effort）。"""
    for unit in units_list:
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
    if not _cancel_revert(layout, unit, report):
        report.fail("confirm", f"{unit} 停不掉；保險還在，先人工處理再 confirm")
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


def rollback_to_user(layout, *, old_home: str | None = None,
                     report: MigrationReport | None = None) -> int:
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return 1
    old = OldLayout(old_home or layout.service_home)
    lock = _Lock(cutover_lock_path(layout))
    try:
        if not lock.acquire():
            report.ok("rollback", "另一個回舊版正在跑（跳過，不會重複動作）")
            return 0
    except OSError as exc:
        report.fail("rollback", f"拿不到鎖（{type(exc).__name__}：{lock.path}）")
        return 1
    try:
        state = read_state(layout)
        if state.get("phase") == PHASE_REVERTED:
            # 已經回過（人工先回、保險後來才觸發）：再確認一次就好，不動資料——
            # 新的 llm.db 已經複製回去過，再複製一次只會蓋掉舊服務寫的東西。
            report.ok("rollback", "狀態已經是 reverted（人工與保險重複觸發也安全）")
            _start_old_units(layout, report)
            ok = _wait_old_list(layout, report)
            _cancel_revert(layout, _state_revert_unit(state, report), report)
            return 0 if ok else 1

        _stop_new_units(layout, report)
        _restore_db(layout, old, state, report)
        _restore_keys_env(layout, old, report)
        started = _start_old_units(layout, report)
        ok = _wait_old_list(layout, report)
        _cancel_revert(layout, _state_revert_unit(state, report), report)
        try:
            write_state(layout, phase=PHASE_REVERTED, local_ok=bool(ok and started))
        except OSError as exc:
            report.fail("rollback", f"寫狀態失敗（{type(exc).__name__}）")
        return 0 if (ok and started) else 1
    finally:
        lock.release()


def _state_revert_unit(state: dict, report=None) -> str:
    """保險的 unit 名（只接受固定值；其他一律用常數，不進 systemctl，高-3）。"""
    unit = _checked_state_unit(state.get("revert_unit"))
    if unit is None:
        if report is not None:
            report.fail("revert-unit",
                        "狀態檔的 revert_unit 不是認得的值；用固定的 "
                        f"{REVERT_TIMER_UNIT}（不拿它去呼叫 systemctl）")
        return REVERT_TIMER_UNIT
    return unit


def _stop_new_units(layout, report) -> None:
    """停（並 disable）新的兩個 system unit：回到 prepare 完、還沒切的狀態。

    失敗不擋後面的回舊版（舊版優先），成敗最後看本機 list 200。
    """
    for unit in (SERVICE_NAME, TUNNEL_SERVICE_NAME):
        _stop_unit(layout, report, "stop-new", unit,
                   ok=f"{unit}（system 層）已停",
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
    if not _db_has_new_writes(state, src, dst):
        report.skip("db-copy", "新的 llm.db 沒有比較新（新服務沒寫過），不覆蓋舊的")
        return
    if os.path.isfile(dst):
        try:
            bak = _backup_old_db(layout, dst)
        except OSError as exc:
            report.fail("db-bak", f"舊 llm.db 備份失敗（{type(exc).__name__}）；不覆蓋")
            return
    else:
        bak = None
        report.skip("db-bak", f"{dst} 不在（沒有舊的可以備份）")
    try:
        _copy_db_consistent(src, dst)
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("db-copy", f"複製失敗（{type(exc).__name__}）")
        return
    where = f"（舊的備份：{os.path.basename(bak)}）" if bak else ""
    report.ok("db-copy", f"新 llm.db → {dst}{where}")


def _db_has_new_writes(state: dict, src: str, dst: str) -> bool:
    """新的 db 是不是在切換開始之後被寫過（中-6）。

    狀態檔記了 ``new_service_ts``（切換開始）就用它；沒有（舊狀態檔或人工
    回滾）就退回 mtime 比較（新的大於舊的才算）。
    """
    started = state.get("new_service_ts")
    try:
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            return os.path.getmtime(src) > float(started)
        if os.path.isfile(dst):
            return os.path.getmtime(src) > os.path.getmtime(dst)
        return True
    except OSError:
        return False


def _backup_old_db(layout, dst: str) -> str:
    """把舊 db 一致快照成帶時間戳的備份；失敗丟 OSError（呼叫方不覆蓋）。"""
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    bak = f"{dst}.bak-{stamp}"
    index = 1
    while _exists(bak):
        bak = f"{dst}.bak-{stamp}-{index}"
        index += 1
    _copy_db_consistent(dst, bak)
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
        _copy_into_place(src, dst)
        install._chown_service(layout, dst)
    except OSError as exc:
        report.fail("keys-env", f"複製失敗（{type(exc).__name__}）")
        return
    report.ok("keys-env", "新的 keys.env 比較新且格式過，複製回舊 state")


def _start_old_units(layout, report) -> bool:
    """以 llmgw 身分啟動舊 user unit（同 cutover 停它們的做法）。"""
    ok = True
    for unit in OLD_START_ORDER:
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


# -- finalize-migration ---------------------------------------------------


def finalize(layout, *, old_home: str | None = None, yes: bool = False,
             report: MigrationReport | None = None) -> int:
    report = report or MigrationReport()
    if not runmod.require_root(layout, report):
        return 1
    if not yes:
        report.fail("finalize", "要 --yes 才會動舊的 unit 與目錄（先看 llmgwctl status）")
        return 1
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

    old = OldLayout(old_home or layout.service_home)
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
                       help="回舊版：停新 unit、把資料複製回舊 state、啟動舊 user unit")
    p.add_argument("--old-home", default=None, help="舊佈局的 HOME（預設服務帳號的 HOME）")

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
        return rollback_to_user(layout, old_home=args.old_home, report=report)
    if args.command == "finalize-migration":
        return finalize(layout, old_home=args.old_home, yes=args.yes, report=report)
    raise AssertionError(f"unhandled command: {args.command}")
