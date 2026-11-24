"""認證模式歷史與「降級滿一小時」判定（#288；契約 C-960～C-968）。

模式只會在**重啟**時改變，而重啟照文件走 ``llmgwctl``，所以由 ``start``／
``restart``／``upgrade``／``rollback`` 在服務起來、探針 ok 之後記錄；``check``
另外依 systemd 的啟動時刻補記，並判定 ``auth-degraded``。``lgwd`` 一行不改。

- 狀態檔：``<auth_state_dir>/auth-mode-history.json``（root:root 0644，一行 JSON；
  鍵恰好是 :data:`_HISTORY_KEYS`，其他一律視為損毀）。非 root 只讀、**絕不寫**。
- 寫入：同目錄的 ``auth-mode-history.lock``（0600，``flock`` 獨佔）串起來，
  內容經 :func:`lgwctl.atomic.write_bytes` 原子替換。
- 審計：``last_mode`` 改變時用 :class:`lgwd.auth_admin.AuditLog` 附加一筆
  ``auth-mode``（不另寫格式）；損毀後重建也記一筆。
- 寫入失敗只印一行警告，不改呼叫端的結束碼——記錄不能讓重啟失敗。

判定的純函式是 :func:`observe`（§1.3）與 :func:`evaluate`（§2 的表格）。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import atomic, units
from .layout import SERVICE_NAME
from .run import RunError

#: 狀態檔與鎖檔名（放在 `auth_state_dir` 底下，不改 `lgwd/auth_paths.py`）。
HISTORY_NAME = "auth-mode-history.json"
LOCK_NAME = "auth-mode-history.lock"
AUDIT_NAME = "auth-audit.log"

SCHEMA = 1
#: 降級滿這麼多秒就判 fail（proposal 第 6 項；固定值，不開放設定）。
DEGRADED_FAIL_SECONDS = 3600
SOURCES = ("restart", "check")
MODES = ("off", "warn", "enforce")

#: 狀態檔的鍵（恰好這些；多了或少了都算損毀）。
_HISTORY_KEYS = frozenset({
    "schema",
    "last_mode",
    "last_observed",
    "enforce_first_seen",
    "enforce_last_seen",
    "degraded_since",
    "source",
})
_MAX_BYTES = 64 * 1024

#: 損毀而且可能正在降級時的固定訊息（§1.4；不能 fail-open）。
CORRUPT_DETAIL = ("降級紀錄損毀，無法判定已降級多久；確認後刪除該檔並記錄風險")
_UNSET = object()


@dataclass(frozen=True)
class ReadResult:
    """`load` 的結果：``state`` 是 ok｜missing｜unreadable｜corrupt。"""

    state: str
    history: dict | None = None


def empty_history() -> dict:
    """還沒有任何紀錄時的形狀（`last_mode`／`source` 是 None，寫入前一定會被填）。"""
    return {
        "schema": SCHEMA,
        "last_mode": None,
        "last_observed": None,
        "enforce_first_seen": None,
        "enforce_last_seen": None,
        "degraded_since": None,
        "source": None,
    }


def is_root(layout) -> bool:
    """只看 `layout.euid`（與 auth 指令一致，測試才驗得到非 root）。"""
    return int(layout.euid) == 0


def history_paths(layout) -> tuple[str, str, str]:
    """回 (state_dir, history_path, lock_path)；狀態目錄與 lgwd 同一份來源。"""
    from .auth_cli import paths_for

    state_dir = paths_for(layout).state_dir
    return (state_dir,
            os.path.join(state_dir, HISTORY_NAME),
            os.path.join(state_dir, LOCK_NAME))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_history(raw: bytes) -> dict | None:
    """一行 JSON → dict；不是合法狀態檔就回 None（呼叫端當成損毀）。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != _HISTORY_KEYS:
        return None
    if not _is_int(data.get("schema")) or data["schema"] != SCHEMA:
        return None
    if data.get("last_mode") not in MODES:
        return None
    if not _is_int(data.get("last_observed")):
        return None
    for name in ("enforce_first_seen", "enforce_last_seen", "degraded_since"):
        value = data.get(name)
        if value is not None and not _is_int(value):
            return None
    if data.get("source") not in SOURCES:
        return None
    return data


def load(layout) -> ReadResult:
    """讀狀態檔（唯讀；不建檔、不取鎖）。"""
    _, path, _ = history_paths(layout)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return ReadResult("missing")
    except PermissionError:
        return ReadResult("unreadable")
    except OSError:
        # symlink（O_NOFOLLOW 的 ELOOP）或其他打不開的情況：當成損毀，不猜。
        return ReadResult("corrupt")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return ReadResult("corrupt")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        return ReadResult("corrupt")
    data = parse_history(raw)
    if data is None:
        return ReadResult("corrupt")
    return ReadResult("ok", data)


def clock_regressed(history: dict | None, now: int) -> bool:
    """時鐘倒退（`now < last_observed`，或 `now < degraded_since`）。"""
    if not history:
        return False
    last = history.get("last_observed")
    since = history.get("degraded_since")
    return ((last is not None and now < last)
            or (since is not None and now < since))


def pending_restart(history: dict | None, mode: str,
                    unit_since: int | None) -> bool:
    """設定檔已改成 warn／off，但服務還沒重啟（目前跑的仍是 enforce）。"""
    if not history or mode not in ("warn", "off"):
        return False
    last_seen = history.get("enforce_last_seen")
    if last_seen is None or history.get("degraded_since") is not None:
        return False
    return unit_since is not None and int(unit_since) < last_seen


def observe(history: dict | None, mode: str, now: int,
            unit_since: int | None = None, *, active: bool = True) -> dict:
    """§1.3 的更新規則（純函式）：回新的歷史 dict，不改傳進來的物件。

    ``active`` 是「unit 現在是 active」；``unit_since`` 是 unit 這次啟動的時刻
    （取不到是 None）。``mode`` 是 enforce 但 ``active`` 是 False 時不更新——
    服務沒在跑 enforce，不該留下假的 enforce 紀錄（那是 `llmgateway.service`
    那一項的 fail）。時鐘倒退時原樣回傳（不寫）。
    """
    if mode not in MODES:
        raise ValueError("mode must be one of off/warn/enforce: {0!r}".format(mode))
    now = int(now)
    base = empty_history() if history is None else dict(history)
    if clock_regressed(base, now):
        return base
    out = dict(base)
    if mode == "enforce":
        if not active:
            return out
        out["last_mode"] = "enforce"
        out["last_observed"] = now
        out["enforce_last_seen"] = now
        if out["enforce_first_seen"] is None:
            out["enforce_first_seen"] = now
        out["degraded_since"] = None
        return out

    out["last_mode"] = mode
    out["last_observed"] = now
    last_seen = base["enforce_last_seen"]
    if last_seen is None:
        return out                       # 從未 enforce：只更新 last_mode
    if base["degraded_since"] is not None:
        return out                       # 已經在降級：不重新計時
    if unit_since is None:
        out["degraded_since"] = now      # 取不到啟動時刻：保守偏晚，可能晚報
        return out
    unit_since = int(unit_since)
    if unit_since >= last_seen:
        out["degraded_since"] = unit_since
    # unit_since < last_seen：設定改了、服務還沒重啟，仍跑 enforce；不設。
    return out


def parse_unit_since(text: str | None) -> int | None:
    """systemctl ``--timestamp=unix --value`` 的輸出：只接受 ``@<整數>``。"""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"@([0-9]+)", text.strip())
    return int(match.group(1)) if match else None


def read_unit_since(layout) -> int | None:
    """`llmgateway.service` 這次啟動的時刻；取不到（或格式不符）回 None。"""
    try:
        proc = units.raw(layout, "show", "-p", "ActiveEnterTimestamp",
                         "--timestamp=unix", "--value", SERVICE_NAME)
    except RunError:
        return None
    if proc.returncode != 0:
        return None
    return parse_unit_since(proc.stdout or "")


@contextmanager
def lock_history(state_dir: str):
    """`auth-mode-history.lock` 的獨佔鎖（0600；只有 root 會走到這裡）。"""
    path = os.path.join(state_dir, LOCK_NAME)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _warn(text: str) -> None:
    print("llmgwctl: 警告：" + text, file=sys.stderr)


def warn_record_failure(exc: BaseException) -> None:
    """呼叫端的縱深防禦：任何記錄例外都只印一行警告，不拋 traceback。

    C-962：記錄不能讓一次成功的重啟失敗；呼叫端（restart／upgrade／rollback）
    在自己的 try/except 裡叫這一個，不依賴 :func:`record` 是否窮舉了例外。
    """
    _warn("認證模式記錄失敗（{0}），未記錄".format(type(exc).__name__))


def _audit(layout, *, source: str, event: str,
           old: str | None, new: str | None) -> bool:
    """用 `lgwd.auth_admin.AuditLog` 附加一筆；失敗只警告、不擋。"""
    try:
        from lgwd.auth_admin import AdminError, AuditLog
        from lgwd.clock import SystemClock

        state_dir, _, _ = history_paths(layout)
        AuditLog(os.path.join(state_dir, AUDIT_NAME), clock=SystemClock()).append(
            "auth-mode",
            args={"source": source, "event": event},
            old=None if old is None else {"mode": old},
            new=None if new is None else {"mode": new},
        )
        return True
    except (AdminError, OSError, ValueError):
        _warn("審計檔附加模式轉換失敗，轉換紀錄未留下")
        return False


def record(layout, *, source: str, now: int | None = None,
           unit_since=_UNSET, active: bool | None = None) -> bool:
    """把一次觀察寫進狀態檔；回「有沒有寫」。

    非 root 一律回 False（不寫）；寫入／鎖／審計失敗只警告，不丟例外——呼叫端
    （restart／upgrade／rollback）的結束碼不受影響。``source`` 是 restart｜check。
    """
    if source not in SOURCES:
        raise ValueError("source must be restart or check: {0!r}".format(source))
    if not is_root(layout):
        return False
    mode = layout.auth_mode()
    if mode not in MODES:
        return False
    now = int(time.time()) if now is None else int(now)
    try:
        state_dir, path, _ = history_paths(layout)
    except ValueError:
        _warn("認證狀態目錄設定錯誤，未記錄認證模式")
        return False
    if active is None:
        active = (True if source == "restart"
                  else units.unit_state(layout, SERVICE_NAME) == "active")
    if unit_since is _UNSET:
        # 重啟剛完成時就以 now 當啟動時刻；check 才向 systemd 要。
        unit_since = now if source == "restart" else read_unit_since(layout)
    if not active:
        unit_since = None
    rebuilt = False
    before = None
    updated = None
    try:
        with lock_history(state_dir):
            read = load(layout)
            if read.state == "unreadable":
                _warn("認證模式歷史檔讀不到（權限），未記錄")
                return False
            if read.state == "corrupt":
                if mode != "enforce":
                    _warn("認證模式歷史檔損毀，未記錄（check 會判 fail）")
                    return False
                history = empty_history()
                rebuilt = True
            elif read.state == "missing":
                history = empty_history()
            else:
                history = read.history
            before = history.get("last_mode")
            updated = observe(history, mode, now, unit_since, active=active)
            if updated == history:
                return False
            updated["source"] = source
            payload = (json.dumps(updated, ensure_ascii=False,
                                  separators=(",", ":"), sort_keys=True) + "\n")
            try:
                atomic.write_bytes(path, payload.encode("utf-8"), 0o644,
                                   owner=(0, 0))
            except OSError as exc:
                _warn("認證模式歷史檔寫入失敗（{0}），未記錄".format(type(exc).__name__))
                return False
    except OSError as exc:
        _warn("認證模式歷史檔的鎖開不了（{0}），未記錄".format(type(exc).__name__))
        return False
    if rebuilt:
        _audit(layout, source=source, event="rebuild", old=None, new=mode)
    elif before is not None and before != updated["last_mode"]:
        _audit(layout, source=source, event="transition",
               old=before, new=updated["last_mode"])
    return True


def evaluate(layout, *, now: int | None = None,
             unit_since=_UNSET) -> tuple[str, str]:
    """§2 的判定表：回 (``ok``｜``todo``｜``fail``, detail)。唯讀。"""
    mode = layout.auth_mode()
    if mode == "unreadable":
        return "todo", "認證模式讀不到（llmgateway.env 需 root 或 llmgw 才讀得）"
    if mode == "unknown":
        return "todo", "認證模式讀不懂（llmgateway.env 的 LGWD_AUTH_MODE）"
    now = int(time.time()) if now is None else int(now)
    try:
        read = load(layout)
    except ValueError:
        return "fail", "認證路徑設定錯誤"
    if read.state == "unreadable":
        return "todo", "需要 root"
    if read.state == "corrupt":
        # 可能降級時不能 fail-open；enforce 是 root 重建後才走到（重建失敗也照判 ok）。
        return ("ok", "enforce") if mode == "enforce" else ("fail", CORRUPT_DETAIL)
    history = read.history or empty_history()
    if clock_regressed(history, now):
        return "todo", "時鐘倒退，降級時間無法判定"
    if mode == "enforce":
        return "ok", "enforce"
    last_seen = history["enforce_last_seen"]
    if last_seen is None:
        return "ok", "未曾 enforce（目前 {0}）".format(mode)
    if unit_since is _UNSET:
        unit_since = read_unit_since(layout)
    if pending_restart(history, mode, unit_since):
        return "todo", "設定已改成 {0} 但服務尚未重啟，目前仍是 enforce".format(mode)
    since = history["degraded_since"]
    if since is None:
        if unit_since is not None and int(unit_since) >= last_seen:
            since = int(unit_since)
        else:
            since = now
    elapsed = now - since
    if elapsed < DEGRADED_FAIL_SECONDS:
        return "todo", (
            "已從 enforce 降到 {0} {1} 分鐘；回退等於恢復免認證，不是安全措施"
            .format(mode, elapsed // 60))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    return "fail", (
        "已從 enforce 降到 {0} 超過 1 小時（自 {1}）；回退等於恢復免認證，不是"
        "安全措施；確認原因後切回 enforce，或照 docs/deploy.md 8.8 記錄風險"
        .format(mode, stamp))


def check_for_report(layout, *, now: int | None = None) -> tuple[str, str]:
    """`check` 的 `auth-degraded`：root 依 §1.3 補記後判定（非 root 只讀）。"""
    mode = layout.auth_mode()
    if mode == "unreadable":
        return "todo", "認證模式讀不到（llmgateway.env 需 root 或 llmgw 才讀得）"
    if mode == "unknown":
        return "todo", "認證模式讀不懂（llmgateway.env 的 LGWD_AUTH_MODE）"
    now = int(time.time()) if now is None else int(now)
    if is_root(layout):
        record(layout, source="check", now=now)
    return evaluate(layout, now=now)


def check(layout, report) -> None:
    """`check` 的 `auth-degraded` 一項（給測試與呼叫端的小包裝）。"""
    level, detail = check_for_report(layout)
    report.add(level, "auth-degraded", detail)
