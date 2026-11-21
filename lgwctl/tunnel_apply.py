"""隧道設定的切換保險：``llmgwctl tunnel-apply``（#221 設計 §5.2(2)、tasks 3.2／C-10）。

G3＝B 下唯一進場是 ``4711``：重啟隧道若設定寫錯就失聯，所以套用新的 ``tunnel.env``
與 drop-in 要有無人值守的反向保險。這**不是**遷移用的 ``lgw-cutover``／``lgw-revert``
（``migrate.py`` 那組子命令不呼叫、不改），只沿用它的寫法：``flock`` 互斥、
``systemd-run --on-active`` 的 transient timer、``--inner`` 只收 ``INVOCATION_ID``。

用法::

    llmgwctl tunnel-apply --new-env <暫存 tunnel.env.new> [--new-dropin <暫存 credential.conf.new>]
                          [--revert-after 10m]
    llmgwctl tunnel-apply --confirm

次序（人手下的那一次「外層」做 1～3，其餘在 ``systemd-run`` 起的獨立 transient unit
``lgw-tunnel-apply``，前景行程被 SIGHUP／SIGTERM 殺掉也照做完）：

1. ``flock``（``<ctl>/.tunnel-apply.lock``）互斥；
2. **先備份**（``<ctl>/tunnel-apply.bak/``，目錄 0700、檔 0600）目前可用的
   ``tunnel.env``、drop-in ``credential.conf``、切換前已存在的 ``FOLLOW_FILE``
   （含預設路徑；新舊路徑不同兩個都記）與私用 ``.tunnel_known_hosts``，逐項記路徑與存在性；
3. 起 transient unit（內層，``--apply-inner``）。內層：
4. **排** ``systemd-run --on-active=<revert-after> --unit=lgw-tunnel-revert``
   （``--revert-inner``）；失敗就中止，**不套用任何新檔**；
5. **以複製套用**暫存檔（保留 ``.new``）→ ``daemon-reload`` ＋重啟隧道；
6. 探針（預設等 180 秒，``rc=2``＝無法探＝失敗；每次探針子行程的逾時＝
   min(單次逾時, 剩餘窗口)，過了期限即使回 0 也算失敗）；失敗或逾時立即還原；
7. 通過後由人重連登入驗收，再 ``--confirm`` 取消 timer。timer 的到期時間記在狀態檔
   （``deadline_epoch``）：``--confirm`` 在鎖內發現已到期（或還原 service 已觸發）
   就**不確認、改走還原**。

還原（``--revert-inner``，timer 到點或內層失敗時）：還原備份的 ``tunnel.env``；drop-in
原不存在只刪本次放的 ``credential.conf``，目錄為本次新建且已空才 ``rmdir``；
``FOLLOW_FILE``／``.tunnel_known_hosts`` 切換前已存在者還原為切換前內容，本次新建者
才改名移開（``*.reverted-<時間戳>``，不刪）；再 ``daemon-reload`` 並重啟隧道。timer 已
還原後 ``--confirm`` 拒絕。

**還原失敗不收保險、不毀備份**：``revert-failed`` 屬於「進行中」狀態（擋住新一輪
``tunnel-apply``，備份原樣保留），並另排 ``lgw-tunnel-revert-retry<N>`` timer 每 60 秒
重試（最多 5 次）；用盡或排不起來就印固定的人工恢復指引。人可在修好原因後跑
``llmgwctl tunnel-apply --retry-revert``（root；只在 ``revert-failed`` 可用），成功才進
終態 ``reverted``。timer 到點時等不到鎖也同樣重排，不放棄。

**檔案操作一律 no-follow**（``lgwctl/safefs.py``）：``FOLLOW_FILE``、``.tunnel_known_hosts``
在 llmgw 可寫的目錄，隨時可能被換成 symlink／FIFO／目錄；備份、還原、改名、刪除都用
父目錄 fd（``O_DIRECTORY|O_NOFOLLOW``）＋``O_NOFOLLOW``＋``fstat`` 一般檔＋大小上限＋
``dir_fd=``。來源檔（``--new-env``／``--new-dropin``）同樣以有上限的 no-follow 讀取。

**``lgw-tunnel-revert.timer`` 是 transient unit，重開機後失效；窗口內切勿重開機。**

輸出與狀態檔不含 ``tunnel.env``／token 的內容，只有階段、時間與固定名稱。測試用的
可調值：``LLMGW_SYSTEMD_RUN``（假指令）、``LLMGW_TUNNEL_APPLY_WAIT``（探針等待秒數，
預設 180、正式下限 90）、``LLMGW_TUNNEL_APPLY_INTERVAL``（探針間隔，預設 10）、
``LLMGW_TUNNEL_APPLY_LOCK_WAIT``（等鎖秒數）、``LLMGW_TUNNEL_APPLY_FOLLOW``
（外層追蹤內層的秒數）。**這四個加速變數只在 ``LLMGW_TUNNEL_APPLY_TEST=1`` 時生效**，
否則一律忽略、用預設值（正式環境不會因環境變數把窗口縮到 90 秒以下）。

``--apply-inner``／``--revert-inner`` 要求 ``INVOCATION_ID`` 非空：那是**防誤用**
（人手照抄整串指令會失去分離執行），**不是認證**——root 本來就能手動設它。
"""

from __future__ import annotations

import json
import os
import re
import stat
import time

from . import atomic, probe, safefs, units
from . import run as runmod
from .layout import TUNNEL_SERVICE_NAME, parse_env_text
from .migrate import (
    ENV_INVOCATION,
    ENV_SYSTEMD_RUN,
    MIN_REVERT_AFTER_SECONDS,
    _REVERT_AFTER_RE,
    _Lock,
    _revert_after_seconds,
    _tool,
    ctl_dir,
    transient_setenv_args,
)
from .report import Report

APPLY_UNIT = "lgw-tunnel-apply"
REVERT_UNIT = "lgw-tunnel-revert"
REVERT_TIMER_UNIT = REVERT_UNIT + ".timer"
DEFAULT_REVERT_AFTER = "10m"
LOCK_NAME = ".tunnel-apply.lock"
STATE_NAME = ".tunnel-apply.state"
BAK_DIR_NAME = "tunnel-apply.bak"
MANIFEST_NAME = "manifest.json"
DROPIN_REL = (TUNNEL_SERVICE_NAME + ".d", "credential.conf")
KNOWN_HOSTS_NAME = ".tunnel_known_hosts"
DEFAULT_FOLLOW_NAME = "gateway.json"
MAX_FILE_BYTES = safefs.MAX_BYTES
RETRY_FLAG = "--retry-revert"

# 探針：設計 §5.2(2)：等待上限預設 150–180 秒（下限 ≥ 90 秒；死埠釋放要 60–120 秒）。
DEFAULT_PROBE_WAIT = 180.0
MIN_PROBE_WAIT = 90.0
DEFAULT_PROBE_INTERVAL = 10.0
DEFAULT_INNER_LOCK_WAIT = 60.0
DEFAULT_REVERT_LOCK_WAIT = 300.0
DEFAULT_CONFIRM_LOCK_WAIT = 10.0
FOLLOW_MARGIN = 120.0
STALE_SCHEDULED_GRACE = 120.0
RETRY_DELAY = "60s"
MAX_REVERT_RETRIES = 5
RETRY_UNIT_PREFIX = REVERT_UNIT + "-retry"
POLL_STEP = 0.5

ENV_TEST = "LLMGW_TUNNEL_APPLY_TEST"
ENV_PROBE_WAIT = "LLMGW_TUNNEL_APPLY_WAIT"
ENV_PROBE_INTERVAL = "LLMGW_TUNNEL_APPLY_INTERVAL"
ENV_LOCK_WAIT = "LLMGW_TUNNEL_APPLY_LOCK_WAIT"
ENV_FOLLOW = "LLMGW_TUNNEL_APPLY_FOLLOW"

PH_SCHEDULED = "apply-scheduled"
PH_APPLYING = "applying"
PH_PENDING = "pending-confirm"
PH_CONFIRMED = "confirmed"
PH_REVERTED = "reverted"
PH_REVERT_FAILED = "revert-failed"
PH_ABORTED = "aborted"
# revert-failed 也是「進行中」：備份是唯一的回頭路，不能被下一輪清掉。
OPEN_PHASES = (PH_SCHEDULED, PH_APPLYING, PH_PENDING, PH_REVERT_FAILED)
SETTLED_PHASES = (PH_PENDING, PH_CONFIRMED, PH_REVERTED, PH_REVERT_FAILED, PH_ABORTED)

REBOOT_WARNING = ("lgw-tunnel-revert.timer 是 transient unit，重開機後即失效；"
                  "切換窗口內切勿重開機")


class _BackupError(Exception):
    pass


# -- 路徑與狀態 ----------------------------------------------------------


def lock_path(layout) -> str:
    return os.path.join(ctl_dir(layout), LOCK_NAME)


def state_path(layout) -> str:
    return os.path.join(ctl_dir(layout), STATE_NAME)


def bak_dir(layout) -> str:
    return os.path.join(ctl_dir(layout), BAK_DIR_NAME)


def dropin_path(layout) -> str:
    return os.path.join(layout.unit_dir, *DROPIN_REL)


def read_state(layout) -> dict:
    try:
        with open(state_path(layout), encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(layout, phase: str, *, fresh: bool = False, **extra) -> None:
    data = {} if fresh else read_state(layout)
    data.update(extra)
    data["phase"] = phase
    data["updated"] = _now()
    atomic.write_text(state_path(layout), json.dumps(data, sort_keys=True) + "\n", 0o600)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def _test_mode(layout) -> bool:
    return layout.env.get(ENV_TEST) == "1"


def _float_env(layout, name: str, default: float) -> float:
    """測試加速變數：只有 ``LLMGW_TUNNEL_APPLY_TEST=1`` 才生效，否則一律用預設值。"""
    if not _test_mode(layout):
        return default
    raw = layout.env.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def probe_wait(layout) -> float:
    wait = _float_env(layout, ENV_PROBE_WAIT, DEFAULT_PROBE_WAIT)
    return wait if _test_mode(layout) else max(wait, MIN_PROBE_WAIT)


def probe_interval(layout) -> float:
    return _float_env(layout, ENV_PROBE_INTERVAL, DEFAULT_PROBE_INTERVAL)


# -- 備份與還原 ----------------------------------------------------------


def _read_env_values(path: str, label: str) -> dict:
    """有上限、no-follow 地讀一個 KEY=VALUE 檔；不存在回空字典，其他問題 → ``_BackupError``。"""
    try:
        data, _ = safefs.read_path(path)
    except FileNotFoundError:
        return {}
    except OSError:
        raise _BackupError(f"{label} 讀不到、不是一般檔、太大或父目錄不安全") from None
    return parse_env_text(data.decode("utf-8", "replace"))


def _follow_paths(layout, new_env: str) -> list:
    """切換前後可能被用到的 FOLLOW_FILE 路徑（含預設）；新舊相同只記一個。"""
    default = os.path.join(layout.state_dir, DEFAULT_FOLLOW_NAME)
    paths = []
    for values in (_read_env_values(layout.tunnel_env_file, "tunnel.env"),
                   _read_env_values(new_env, "--new-env")):
        path = values.get("FOLLOW_FILE") or default
        if path not in paths:
            paths.append(path)
    return paths


def _snapshot(path: str, bak_fd: int, fname: str, label: str) -> dict:
    """把一個檔備份到備份目錄（0600）；回 manifest 項目（記路徑與存在性）。

    父目錄 nofollow 開 fd、最終分量 ``O_NOFOLLOW`` 並 ``fstat``：被換成 symlink／FIFO／
    目錄、超過上限、父目錄是 symlink 都拒絕（不備份也不切換）。
    """
    entry = {"label": label, "path": path, "exists": False, "parent_existed": True}
    directory, name = safefs.split(path)
    try:
        dir_fd = safefs.open_dir(directory)
    except FileNotFoundError:
        entry["parent_existed"] = False
        return entry
    except OSError:
        raise _BackupError(f"{label} 的父目錄不安全（symlink？）或讀不到，不備份也不切換") from None
    try:
        try:
            data, info = safefs.read_regular(dir_fd, name)
        except FileNotFoundError:
            return entry
        except OSError:
            raise _BackupError(f"{label} 不是一般檔、太大或被換掉，不備份也不切換") from None
    finally:
        os.close(dir_fd)
    try:
        safefs.write_atomic(bak_fd, fname, data, 0o600)
    except OSError:
        raise _BackupError(f"{label} 備份失敗") from None
    entry.update(exists=True, file=fname, mode=stat.S_IMODE(info.st_mode),
                 uid=info.st_uid, gid=info.st_gid)
    return entry


def _open_bak(layout, *, create: bool) -> int:
    """開備份目錄 fd（ctl 目錄與備份目錄都 nofollow）。"""
    ctl = ctl_dir(layout)
    if create:
        os.makedirs(ctl, mode=0o700, exist_ok=True)
        os.chmod(ctl, 0o700)
    ctl_fd = safefs.open_dir(ctl)
    try:
        try:
            return os.open(BAK_DIR_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=ctl_fd)
        except FileNotFoundError:
            if not create:
                raise
        os.mkdir(BAK_DIR_NAME, 0o700, dir_fd=ctl_fd)
        return os.open(BAK_DIR_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=ctl_fd)
    finally:
        os.close(ctl_fd)


def make_backup(layout, new_env: str) -> dict:
    """先備份（任何覆寫之前）；回 manifest。失敗擲 ``_BackupError``。"""
    try:
        bak_fd = _open_bak(layout, create=True)
    except OSError:
        raise _BackupError("備份目錄開不起來（或是 symlink）") from None
    try:
        os.fchmod(bak_fd, 0o700)
        # 上一輪的備份已無用（狀態不是進行中才會走到這裡）；整個重建。
        for name in os.listdir(bak_fd):
            try:
                safefs.unlink(bak_fd, name)
            except OSError:
                pass
        dropin = dropin_path(layout)
        manifest = {
            "created": _now(),
            "tunnel_env": _snapshot(layout.tunnel_env_file, bak_fd, "tunnel.env", "tunnel.env"),
            "dropin": _snapshot(dropin, bak_fd, "credential.conf", "drop-in"),
            "files": [],
        }
        manifest["dropin"]["dir_existed"] = manifest["dropin"]["parent_existed"]
        for index, path in enumerate(_follow_paths(layout, new_env)):
            manifest["files"].append(_snapshot(path, bak_fd, f"follow-{index}", "FOLLOW_FILE"))
        manifest["files"].append(_snapshot(os.path.join(layout.state_dir, KNOWN_HOSTS_NAME), bak_fd,
                                           "known-hosts", "known_hosts"))
        try:
            safefs.write_atomic(bak_fd, MANIFEST_NAME,
                                (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"), 0o600)
        except OSError:
            raise _BackupError("備份清單寫不進去") from None
    finally:
        os.close(bak_fd)
    return manifest


def load_manifest(layout) -> dict | None:
    try:
        bak_fd = _open_bak(layout, create=False)
    except OSError:
        return None
    try:
        data = json.loads(safefs.read_regular(bak_fd, MANIFEST_NAME)[0].decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(bak_fd)
    return data if isinstance(data, dict) else None


def _restore_content(bak_fd: int, entry: dict, stamp: str) -> None:
    """切換前已存在 → 還原內容；切換前不存在 → 現在有就改名移開（不刪）。

    全程 fd-relative：最終分量是 symlink 時 rename 取代／移開的是 symlink 本身，
    不會寫到、也不會動到它指的目標；父目錄是 symlink 就拒絕（還原失敗）。
    """
    directory, name = safefs.split(entry["path"])
    if entry.get("exists"):
        data, _ = safefs.read_regular(bak_fd, entry["file"])
        dir_fd = safefs.open_dir(directory, create=True)
        try:
            safefs.write_atomic(dir_fd, name, data, entry.get("mode", 0o600),
                                owner=(entry["uid"], entry["gid"]) if "uid" in entry else None)
        finally:
            os.close(dir_fd)
        return
    try:
        dir_fd = safefs.open_dir(directory)
    except FileNotFoundError:
        return
    try:
        try:
            safefs.lstat(dir_fd, name)
        except FileNotFoundError:
            return
        safefs.rename(dir_fd, name, f"{name}.reverted-{stamp}")
    finally:
        os.close(dir_fd)


def _restore_dropin(bak_fd: int, entry: dict, stamp: str) -> None:
    if entry.get("exists"):
        _restore_content(bak_fd, entry, stamp)
        return
    # 原不存在：只刪本次放的 credential.conf；目錄是本次新建且已空才 rmdir。
    directory, name = safefs.split(entry["path"])
    try:
        dir_fd = safefs.open_dir(directory)
    except FileNotFoundError:
        return
    try:
        try:
            safefs.lstat(dir_fd, name)
        except FileNotFoundError:
            pass
        else:
            safefs.unlink(dir_fd, name)
    finally:
        os.close(dir_fd)
    if not entry.get("dir_existed", True):
        parent, dirname = safefs.split(directory)
        try:
            parent_fd = safefs.open_dir(parent)
        except OSError:
            return
        try:
            os.rmdir(dirname, dir_fd=parent_fd)
        except OSError:
            pass
        finally:
            os.close(parent_fd)


def restore_backup(layout, manifest: dict, report) -> bool:
    """依 manifest 還原；各項獨立進行，回「是否全部成功」。"""
    stamp = _stamp()
    try:
        bak_fd = _open_bak(layout, create=False)
    except OSError:
        report.fail("revert-restore", "備份目錄開不起來")
        return False
    ok = True
    try:
        steps = [("tunnel.env", lambda: _restore_content(bak_fd, manifest["tunnel_env"], stamp)),
                 ("drop-in", lambda: _restore_dropin(bak_fd, manifest["dropin"], stamp))]
        for entry in manifest.get("files", []):
            steps.append((entry["label"], lambda entry=entry: _restore_content(bak_fd, entry, stamp)))
        for label, step in steps:
            try:
                step()
            except (OSError, KeyError):
                report.fail("revert-restore", f"{label} 還原失敗")
                ok = False
    finally:
        os.close(bak_fd)
    if ok:
        report.ok("revert-restore", "已還原切換前的設定")
    return ok


def _stop_timer(layout, unit: str = REVERT_TIMER_UNIT) -> bool:
    try:
        return units.raw(layout, "stop", unit).returncode == 0
    except runmod.RunError:
        return False


def _stop_timers(layout) -> None:
    """還原成功後收掉保險 timer 與還在排著的重試 timer。"""
    _stop_timer(layout)
    pending = read_state(layout).get("retry_timer")
    if isinstance(pending, str) and pending.startswith(RETRY_UNIT_PREFIX):
        _stop_timer(layout, pending)


def revert(layout, report) -> bool:
    """還原＋daemon-reload＋重啟隧道（已持有鎖）。回是否全部成功。"""
    manifest = load_manifest(layout)
    if manifest is None:
        report.fail("revert", "找不到備份清單，無法還原（人工處理）")
        return False
    ok = restore_backup(layout, manifest, report)
    if units.daemon_reload(layout):
        report.ok("revert-daemon-reload")
    else:
        report.fail("revert-daemon-reload")
        ok = False
    if units.restart(layout, TUNNEL_SERVICE_NAME):
        report.ok("revert-restart", TUNNEL_SERVICE_NAME)
    else:
        report.fail("revert-restart", TUNNEL_SERVICE_NAME)
        ok = False
    return ok


def _recovery_guide(layout, report) -> None:
    """固定的人工恢復指引（不含任何檔案內容）。"""
    report.fail("tunnel-apply", f"還原未完全成功，需人工處理；備份保留在 {bak_dir(layout)}"
                                "（下一輪 tunnel-apply 會被擋，備份不會被清掉）")
    report.todo("recovery",
                f"看 journal -u {APPLY_UNIT} / -u {REVERT_UNIT} 找原因並修好，再執行 "
                "llmgwctl tunnel-apply --retry-revert；仍不行就人工把備份目錄內的檔"
                "（路徑見 manifest.json）放回原處、daemon-reload 並重啟 "
                f"{TUNNEL_SERVICE_NAME}；在那之前不要再跑 tunnel-apply --new-env")


def _schedule_retry(layout, report, *, attempt: int, round_id: str | None) -> str | None:
    """排下一次還原重試的 transient timer；回 timer unit 名，排不起來回 None。"""
    unit = f"{RETRY_UNIT_PREFIX}{attempt}"
    argv = revert_timer_argv(layout, revert_after=RETRY_DELAY, unit=unit, attempt=attempt,
                             round_id=round_id)
    try:
        proc = runmod.run(argv, env=layout.child_env())
    except runmod.RunError as exc:
        proc, why = None, exc.kind
    else:
        why = f"rc={proc.returncode}"
    if proc is None or proc.returncode != 0:
        report.fail("revert-retry", f"{unit}.timer 排不起來（{why}）")
        return None
    report.ok("revert-retry", f"{unit}.timer 已排（{RETRY_DELAY} 後重試還原，第 {attempt}/"
                              f"{MAX_REVERT_RETRIES} 次）")
    return unit + ".timer"


def _do_revert(layout, report, *, reason: str, attempt: int, round_id: str | None) -> bool:
    """還原並落狀態（已持有鎖）。失敗：保留備份、狀態＝revert-failed、另排重試、印指引。"""
    if revert(layout, report):
        _stop_timers(layout)
        write_state(layout, PH_REVERTED, reason=reason, retry_timer="")
        return True
    scheduled = ""
    if attempt + 1 <= MAX_REVERT_RETRIES:
        scheduled = _schedule_retry(layout, report, attempt=attempt + 1, round_id=round_id) or ""
    else:
        report.fail("revert-retry", f"自動重試已用盡（{MAX_REVERT_RETRIES} 次）")
    write_state(layout, PH_REVERT_FAILED, reason=reason, retry_timer=scheduled)
    _recovery_guide(layout, report)
    return False


# -- 套用 ----------------------------------------------------------------


def _copy_apply(src: str, dst: str, default_mode: int) -> None:
    """複製（非 rename）原子寫入；沿用目標原有的權限與擁有者，保留來源。

    來源以有上限的 no-follow 讀；目標在 nofollow 開的父目錄 fd 底下以 rename 取代。
    """
    data, _ = safefs.read_path(src)
    directory, name = safefs.split(dst)
    dir_fd = safefs.open_dir(directory, create=True)
    try:
        mode, owner = default_mode, None
        try:
            info = safefs.lstat(dir_fd, name)
        except OSError:
            pass
        else:
            if stat.S_ISREG(info.st_mode):
                mode, owner = stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid)
        safefs.write_atomic(dir_fd, name, data, mode, owner=owner)
    finally:
        os.close(dir_fd)


def _systemd_run_base(layout, unit: str) -> list:
    return [_tool(layout, ENV_SYSTEMD_RUN, "systemd-run"), f"--unit={unit}", "--collect"]


def _inner_tail(layout) -> list:
    return [*transient_setenv_args(layout), f"--working-directory={layout.current_link}",
            layout.python3, "-m", "lgwctl", "tunnel-apply"]


def apply_unit_argv(layout, *, new_env: str, new_dropin: str | None, revert_after: str,
                    round_id: str | None = None) -> list:
    argv = [*_systemd_run_base(layout, APPLY_UNIT), *_inner_tail(layout),
            "--apply-inner", "--new-env", new_env, "--revert-after", revert_after]
    if new_dropin:
        argv += ["--new-dropin", new_dropin]
    if round_id:
        argv += ["--round", round_id]
    return argv


def revert_timer_argv(layout, *, revert_after: str, unit: str = REVERT_UNIT, attempt: int = 0,
                      round_id: str | None = None) -> list:
    base = _systemd_run_base(layout, unit)
    argv = [*base[:2], f"--on-active={revert_after}", *base[2:], *_inner_tail(layout),
            "--revert-inner"]
    if attempt:
        argv += ["--revert-attempt", str(attempt)]
    if round_id:
        argv += ["--round", round_id]
    return argv


def _probe_until_ok(layout, report) -> bool:
    """探針直到 200 或窗口用完。窗口是實際上限：每次探針子行程的逾時＝
    min(單次逾時, 剩餘窗口)；過了期限即使回 0 也當失敗（保守）。"""
    deadline = time.monotonic() + probe_wait(layout)
    interval = probe_interval(layout)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.fail("probe", "等待逾時，經會合點仍不是 200")
            return False
        rc = probe.remote_rc(layout, timeout=min(runmod.PROBE_TIMEOUT, remaining))
        if time.monotonic() > deadline:
            report.fail("probe", "探針回來時已超過等待窗口，保守視為失敗")
            return False
        if rc == 0:
            report.ok("probe", "經會合點 200")
            return True
        if rc is None or rc == 2:
            report.fail("probe", "無法探（rc=2 或探針跑不起來），視為失敗")
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.fail("probe", "等待逾時，經會合點仍不是 200")
            return False
        time.sleep(min(interval, remaining))


def _fail_and_revert(layout, report, name: str, detail: str, round_id: str | None) -> int:
    """已動過檔案後的失敗：立即還原（失敗則保留備份＋排重試＋指引）。"""
    report.fail(name, detail)
    if _do_revert(layout, report, reason=name, attempt=0, round_id=round_id):
        report.fail("tunnel-apply", "已還原為切換前的設定")
    return 1


def _apply_inner(layout, report, *, new_env: str, new_dropin: str | None,
                 revert_after: str, round_id: str | None) -> int:
    lock = _Lock(lock_path(layout))
    if not lock.acquire(_float_env(layout, ENV_LOCK_WAIT, DEFAULT_INNER_LOCK_WAIT)):
        report.fail("lock", "拿不到 .tunnel-apply.lock（另一個 tunnel-apply 在跑）")
        return 1
    try:
        state = read_state(layout)
        if (state.get("phase") != PH_SCHEDULED or load_manifest(layout) is None
                or (round_id and state.get("round") not in (None, round_id))):
            report.fail("apply", "沒有待套用的排程或備份（不動任何檔）")
            return 1
        # ② 先排保險；排不起來就中止，不套用任何新檔。
        scheduled_at = time.time()
        argv = revert_timer_argv(layout, revert_after=revert_after, round_id=round_id)
        try:
            proc = runmod.run(argv, env=layout.child_env())
        except runmod.RunError as exc:
            proc, why = None, exc.kind
        else:
            why = f"rc={proc.returncode}"
        if proc is None or proc.returncode != 0:
            write_state(layout, PH_ABORTED, reason="insurance")
            report.fail("insurance", f"{REVERT_TIMER_UNIT} 排不起來（{why}）；不套用，設定原樣")
            return 1
        report.ok("insurance", f"{REVERT_TIMER_UNIT} 已排（{revert_after} 後還原）")
        # timer 最早在 scheduled_at + revert_after 到點：confirm 以此判斷「保險已到期」。
        write_state(layout, PH_APPLYING,
                    deadline_epoch=scheduled_at + (_revert_after_seconds(revert_after) or 0.0))
        # ③ 以複製套用（保留 .new）。
        try:
            if new_dropin:
                _copy_apply(new_dropin, dropin_path(layout), 0o644)
            _copy_apply(new_env, layout.tunnel_env_file, 0o600)
        except OSError:
            return _fail_and_revert(layout, report, "apply", "複製套用失敗", round_id)
        report.ok("apply", "已複製套用" + ("（含 drop-in）" if new_dropin else ""))
        # ④ daemon-reload ＋重啟隧道。
        if not units.daemon_reload(layout):
            return _fail_and_revert(layout, report, "daemon-reload", "失敗", round_id)
        if not units.restart(layout, TUNNEL_SERVICE_NAME):
            return _fail_and_revert(layout, report, "restart", TUNNEL_SERVICE_NAME, round_id)
        report.ok("restart", TUNNEL_SERVICE_NAME)
        # ⑤ 探針。
        if not _probe_until_ok(layout, report):
            return _fail_and_revert(layout, report, "tunnel-apply", "探針未過", round_id)
        write_state(layout, PH_PENDING)
        report.ok("tunnel-apply", "探針通過；重連 <會合點>:4711 登入驗收後執行 "
                                  "llmgwctl tunnel-apply --confirm")
        report.todo("reboot", REBOOT_WARNING)
        return 0
    finally:
        lock.release()


def _revert_inner(layout, report, *, attempt: int = 0, round_id: str | None = None) -> int:
    lock = _Lock(lock_path(layout))
    if not lock.acquire(_float_env(layout, ENV_LOCK_WAIT, DEFAULT_REVERT_LOCK_WAIT)):
        # 等不到鎖（apply 還在跑）：不放棄，重排一個重試（有上限）；沒鎖不寫狀態。
        report.fail("lock", "拿不到 .tunnel-apply.lock；還原這次沒做")
        if attempt + 1 <= MAX_REVERT_RETRIES:
            round_id = round_id or read_state(layout).get("round")
            _schedule_retry(layout, report, attempt=attempt + 1, round_id=round_id)
        else:
            report.todo("recovery", "重試已用盡，需人工處理：看 journal，確認沒有別的 "
                                    "tunnel-apply 卡住後執行 llmgwctl tunnel-apply --retry-revert")
            report.fail("tunnel-apply", "還原沒做，需人工處理")
        return 1
    try:
        state = read_state(layout)
        if round_id and state.get("round") not in (None, round_id):
            report.ok("revert", "這個保險屬於較早的一輪；不動現在的設定")
            return 0
        phase = state.get("phase")
        if phase in (PH_CONFIRMED, PH_REVERTED, PH_ABORTED):
            report.ok("revert", f"狀態是 {phase}；不需要還原")
            return 0
        if _do_revert(layout, report, reason="timer", attempt=attempt,
                      round_id=round_id or state.get("round")):
            report.ok("tunnel-apply", "已還原為切換前的設定")
            return 0
        return 1
    finally:
        lock.release()


def _retry_revert(layout, report) -> int:
    """人工重試還原（只在 revert-failed）；成功才進終態 reverted。"""
    lock = _Lock(lock_path(layout))
    if not lock.acquire(_float_env(layout, ENV_LOCK_WAIT, DEFAULT_CONFIRM_LOCK_WAIT)):
        report.fail("lock", "tunnel-apply 還在進行中（等它結束再 --retry-revert）")
        return 1
    try:
        phase = read_state(layout).get("phase")
        if phase != PH_REVERT_FAILED:
            report.fail("retry-revert", f"只在 revert-failed 可用（狀態：{phase or '無'}）")
            return 1
        if revert(layout, report):
            _stop_timers(layout)
            write_state(layout, PH_REVERTED, reason="retry-revert", retry_timer="")
            report.ok("tunnel-apply", "已還原為切換前的設定")
            return 0
        write_state(layout, PH_REVERT_FAILED, reason="retry-revert")
        _recovery_guide(layout, report)
        return 1
    finally:
        lock.release()


def _confirm(layout, report) -> int:
    lock = _Lock(lock_path(layout))
    if not lock.acquire(_float_env(layout, ENV_LOCK_WAIT, DEFAULT_CONFIRM_LOCK_WAIT)):
        report.fail("lock", "tunnel-apply 還在進行中（等它結束再 --confirm）")
        return 1
    try:
        state = read_state(layout)
        phase = state.get("phase")
        if phase in (PH_REVERTED, PH_REVERT_FAILED):
            hint = (f"；還原未完全成功，見 llmgwctl tunnel-apply {RETRY_FLAG}"
                    if phase == PH_REVERT_FAILED else "")
            report.fail("confirm", "已還原（保險 timer 已觸發或探針失敗），不能確認；"
                                   f"要重來請再跑 tunnel-apply --new-env …{hint}")
            return 1
        if phase != PH_PENDING:
            report.fail("confirm", f"沒有待確認的切換（狀態：{phase or '無'}）")
            return 1
        # 保險已到期（或還原 service 已被 timer 觸發、正在等這把鎖）：confirm 不得勝出。
        deadline = state.get("deadline_epoch")
        expired = isinstance(deadline, (int, float)) and time.time() >= deadline
        if not expired and units.unit_state(layout, REVERT_UNIT + ".service") == "active":
            expired = True
        if expired:
            report.fail("confirm", "保險已到期，正在還原；不確認")
            if _do_revert(layout, report, reason="confirm-expired", attempt=0,
                          round_id=state.get("round")):
                report.fail("tunnel-apply", "已還原為切換前的設定")
            return 1
        stopped = _stop_timer(layout)
        if not stopped and units.unit_state(layout, REVERT_TIMER_UNIT) not in ("inactive", "missing"):
            report.fail("confirm", f"取消不了 {REVERT_TIMER_UNIT}；不標記確認（保險仍在）")
            return 1
        write_state(layout, PH_CONFIRMED)
        report.ok("confirm", f"{REVERT_TIMER_UNIT} 已取消；不會自動還原")
        return 0
    finally:
        lock.release()


def _stale_scheduled(layout, state: dict) -> bool:
    """``apply-scheduled`` 卡住：外層在寫狀態後、起 unit 前被殺（無 unit、無 timer）。

    超過寬限且 ``lgw-tunnel-apply`` 確定不在（inactive／missing；``unknown`` 保守當還在）
    才算殘留。內層若真的起來了，狀態早已離開 apply-scheduled。
    """
    epoch = state.get("scheduled_epoch")
    if not isinstance(epoch, (int, float)) or time.time() - epoch < STALE_SCHEDULED_GRACE:
        return False
    return units.unit_state(layout, APPLY_UNIT + ".service") in ("inactive", "missing")


def _outer(layout, report, *, new_env: str, new_dropin: str | None, revert_after: str) -> int:
    lock = _Lock(lock_path(layout))
    if not lock.acquire(0):
        report.fail("lock", "另一個 tunnel-apply 正在跑（.tunnel-apply.lock）")
        return 1
    try:
        state = read_state(layout)
        phase = state.get("phase")
        if phase == PH_REVERT_FAILED:
            report.fail("tunnel-apply", "上一輪還原失敗（狀態：revert-failed），備份保留中；"
                                        f"先 llmgwctl tunnel-apply {RETRY_FLAG}（或照指引人工處理）")
            return 1
        if phase == PH_SCHEDULED and _stale_scheduled(layout, state):
            report.todo("stale", "上一輪停在 apply-scheduled 且 lgw-tunnel-apply 不在"
                                 "（外層被中斷的殘留，設定沒動過）；重新開始")
        elif phase in OPEN_PHASES:
            report.fail("tunnel-apply", f"上一輪還沒結束（狀態：{phase}）；先 --confirm 或等還原")
            return 1
        try:
            make_backup(layout, new_env)
        except _BackupError as exc:
            report.fail("backup", f"{exc}；不切換")
            return 1
        report.ok("backup", f"{bak_dir(layout)}（0700/0600）")
        round_id = os.urandom(4).hex()
        write_state(layout, PH_SCHEDULED, fresh=True, revert_after=revert_after, round=round_id,
                    scheduled_epoch=time.time())
        # 內層要自己拿鎖：先放掉；狀態已是 apply-scheduled，別的外層會被擋（OPEN_PHASES）。
        lock.release()
        argv = apply_unit_argv(layout, new_env=new_env, new_dropin=new_dropin,
                               revert_after=revert_after, round_id=round_id)
        try:
            proc = runmod.run(argv, env=layout.child_env())
        except runmod.RunError as exc:
            proc, why = None, exc.kind
        else:
            why = f"rc={proc.returncode}"
        if proc is None or proc.returncode != 0:
            write_state(layout, PH_ABORTED, reason="launch")
            report.fail("tunnel-apply", f"起不了獨立 unit {APPLY_UNIT}（{why}）；沒有套用任何檔")
            return 1
    finally:
        lock.release()
    report.ok("detached", f"{APPLY_UNIT} 已在背景跑（連線被重啟斷掉也會做完；看 journal -u {APPLY_UNIT}）")
    report.todo("reboot", REBOOT_WARNING)
    return _follow(layout, report)


def _follow(layout, report) -> int:
    """追蹤內層到結果（前景被殺不影響內層）。"""
    limit = _float_env(layout, ENV_FOLLOW, probe_wait(layout) + FOLLOW_MARGIN)
    deadline = time.monotonic() + limit
    seen = PH_SCHEDULED
    while True:
        phase = read_state(layout).get("phase")
        if phase and phase != seen:
            seen = phase
            report.ok("phase", phase)
        if phase == PH_PENDING:
            report.ok("tunnel-apply", "探針通過；重連 <會合點>:4711 登入驗收後執行 "
                                      "llmgwctl tunnel-apply --confirm（逾時自動還原）")
            return 0
        if phase == PH_REVERT_FAILED:
            report.fail("tunnel-apply", f"還原未完全成功，需人工處理（journal -u {APPLY_UNIT}；"
                                        f"備份保留，修好後 llmgwctl tunnel-apply {RETRY_FLAG}）")
            return 1
        if phase in (PH_REVERTED, PH_ABORTED):
            report.fail("tunnel-apply", f"沒有套用成功（狀態：{phase}；詳情 journal -u {APPLY_UNIT}）")
            return 1
        if time.monotonic() >= deadline:
            report.todo("tunnel-apply", f"還在進行（狀態：{phase}）；看 journal -u {APPLY_UNIT}")
            return 1
        time.sleep(POLL_STEP)


# -- 入口 ----------------------------------------------------------------


def run(layout, *, new_env: str | None = None, new_dropin: str | None = None,
        revert_after: str = DEFAULT_REVERT_AFTER, confirm: bool = False,
        revert_inner: bool = False, apply_inner: bool = False, retry_revert: bool = False,
        revert_attempt: int = 0, round_id: str | None = None,
        report: Report | None = None) -> int:
    report = report or Report()
    if not runmod.require_root(layout, report):
        return 1
    if revert_inner or apply_inner:
        # 內部旗標只給 systemd-run 起的 unit（沿用 migrate.py：INVOCATION_ID）。
        # 這是防誤用（人手照抄整串指令會失去分離執行），不是認證。
        if not layout.env.get(ENV_INVOCATION):
            report.fail("inner", "--apply-inner／--revert-inner 只給 transient unit 用"
                                 "（沒有 INVOCATION_ID）；人手請跑 llmgwctl tunnel-apply --new-env …")
            return 1
    if confirm:
        return _confirm(layout, report)
    if retry_revert:
        return _retry_revert(layout, report)
    if revert_inner:
        return _revert_inner(layout, report, attempt=max(0, int(revert_attempt or 0)),
                             round_id=round_id)
    if not new_env:
        report.fail("new-env", "要 --new-env <暫存 tunnel.env.new>")
        return 1
    # 錯誤訊息一律固定字串，不回顯使用者輸入（可能夾帶控制字元或敏感字串）。
    if not _REVERT_AFTER_RE.fullmatch(revert_after or ""):
        report.fail("revert-after", "時間格式不認得（例 10m、600s）")
        return 1
    seconds = _revert_after_seconds(revert_after)
    if seconds is None or seconds < MIN_REVERT_AFTER_SECONDS:
        report.fail("revert-after",
                    "太短：探針最多等數分鐘，保險會在切換進行中就開跑；"
                    f"至少 {int(MIN_REVERT_AFTER_SECONDS / 60)}m")
        return 1
    new_env = os.path.abspath(new_env)
    new_dropin = os.path.abspath(new_dropin) if new_dropin else None
    for label, path in (("new-env", new_env), ("new-dropin", new_dropin)):
        if path is not None and (os.path.islink(path) or not os.path.isfile(path)):
            report.fail(label, "不是一般檔（或不存在）")
            return 1
    if apply_inner:
        return _apply_inner(layout, report, new_env=new_env, new_dropin=new_dropin,
                            revert_after=revert_after, round_id=round_id)
    return _outer(layout, report, new_env=new_env, new_dropin=new_dropin,
                  revert_after=revert_after)
