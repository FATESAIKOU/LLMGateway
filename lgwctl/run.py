"""跑外部指令的小工具：統一把「跑不起來」與「非零結束」分開。

``run()`` 一律捕捉輸出（呼叫方要 parse 或轉印），逾時與找不到指令都丟
``RunError``；非零結束是正常的 ``CompletedProcess``，由呼叫方決定 ok／todo／fail。
錯誤訊息不含指令的參數值以外的東西（我們從不把 key 的值放進 argv）。
"""

from __future__ import annotations

import os
import pwd
import subprocess

DEFAULT_TIMEOUT = 30.0
CLONE_TIMEOUT = 300.0
SYNC_TIMEOUT = 180.0
PROBE_TIMEOUT = 60.0


class RunError(Exception):
    """不是非零結束，是根本跑不起來：kind 為 missing｜timeout｜oserror。"""

    def __init__(self, kind: str, cmd, detail: str = ""):
        name = cmd[0] if cmd else ""
        super().__init__(f"{kind}:{name}")
        self.kind = kind
        self.cmd = list(cmd)
        self.detail = detail


def run(cmd, *, env=None, timeout: float = DEFAULT_TIMEOUT, cwd=None):
    """跑一個指令；回 CompletedProcess（text 模式、stdout／stderr 已捕捉）。"""
    try:
        return subprocess.run(
            list(cmd),
            env=env,
            cwd=cwd,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except FileNotFoundError:
        raise RunError("missing", cmd) from None
    except subprocess.TimeoutExpired:
        raise RunError("timeout", cmd) from None
    except OSError as exc:
        raise RunError("oserror", cmd, type(exc).__name__) from None


def current_user() -> str:
    """現在這個行程的使用者名（判斷要不要 sudo 過去服務帳號）。"""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        return os.environ.get("USER") or "?"


def needs_sudo(layout) -> bool:
    return current_user() != layout.service_user


def service_user_argv(layout, argv) -> list:
    """要以服務帳號身分跑時，前面加 ``sudo -n -u <user>``；已經是它就不加。

    ``-n`` 讓需要密碼時直接失敗（不互動），呼叫方把失敗當「無法確認」。
    """
    if not needs_sudo(layout):
        return list(argv)
    return [layout.sudo, "-n", "-u", layout.service_user] + list(argv)


def require_root(layout, report) -> bool:
    """要動 /opt、/etc 與 systemctl 的子指令（install／upgrade／rollback）先擋。

    ``LLMGW_PREFIX`` 模式（測試沙箱）不需要 root。
    """
    if layout.prefix or layout.euid == 0:
        return True
    report.fail("root", "要 root（sudo llmgwctl …）；測用 LLMGW_PREFIX")
    return False


def sudo_failed(text: str) -> bool:
    """stderr 看起來是 sudo 自己的問題（不是被叫的那個指令的問題）。"""
    return any(needle in (text or "") for needle in
               ("sudo:", "not in the sudoers file", "command not found"))


def trimmed(text: str | None) -> str:
    """去掉前後空白與一層引號（gh 的 ``--jq .full_name`` 某些版本會包引號）。"""
    value = (text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()
