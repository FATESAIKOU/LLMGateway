"""子行程護欄（#60 tasks 1.1）：起停、killpg、stderr 尾端；keysync 與 Claude 取用量共用。

只管「怎麼安全地起一個子行程、怎麼叫停它」；計時、環境內容、逾時策略由呼叫方決定。
- 一律 `start_new_session`（獨立行程群，pgid＝pid）；
- 叫停只對 spawn 當下記下的 pid 當 pgid（`signal_child`），不依名稱、不重查 getpgid；
- stderr 只在背景執行緒留最後 `STDERR_TAIL_LIMIT` 位元組。
只用標準函式庫。
"""

from __future__ import annotations

import os
import subprocess
import sys

STDERR_TAIL_LIMIT = 4096    # 子行程 stderr 只留最後這些位元組（記憶體有上限）


def read_tail(stream, holder: list, limit: int = STDERR_TAIL_LIMIT) -> None:
    """把子行程的 stderr 管線讀乾，只留最後 limit 位元組（記憶體有上限）。

    在背景執行緒跑：子行程輸出比管線緩衝大時才不會卡住；讀完把尾巴放進 holder。
    """
    buf = bytearray()
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > limit:
                del buf[:-limit]
    except (OSError, ValueError):
        pass  # 關閉或管線斷掉：留著已經讀到的部分
    holder.append(bytes(buf))


# 全新單執行緒直譯器裡把 stdin（pty 從端）設成新 session 的控制終端，再 exec 原版程式
# （不用 preexec_fn：多執行緒服務裡 fork 後跑 Python 不安全）。
_CTTY_TRAMPOLINE = ("import fcntl,os,signal,sys,termios;"
                    "signal.signal(signal.SIGPIPE,signal.SIG_DFL);"
                    "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                    "os.execv(sys.argv[1],sys.argv[1:])")


def spawn(argv: list, env: dict, cwd: str | None = None,
          stderr=subprocess.PIPE, tty: int | None = None) -> subprocess.Popen:
    """起子行程：stdin／stdout 丟掉、獨立行程群；stderr 預設走管線（呼叫方用 read_tail 讀），
    不需要時傳 subprocess.DEVNULL。

    傳 `tty`（pty 從端的 fd）時，stdin／stdout／stderr 全接到它並成為控制終端
    （給要求互動式終端的程式用；此時 `stderr` 參數無效；`argv[0]` 要是可執行檔的完整路徑，
    經 Python 跳板取得控制終端後 exec）。從端 fd 由呼叫方在 spawn 後關掉。
    """
    if tty is not None:
        return subprocess.Popen(
            [sys.executable, "-I", "-c", _CTTY_TRAMPOLINE, *argv],
            stdin=tty, stdout=tty, stderr=tty, env=env, cwd=cwd, start_new_session=True,
        )
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )


def signal_child(proc: subprocess.Popen, sig: int) -> None:
    """把子行程（start_new_session 起的，pgid＝pid）連它的行程群叫停。

    直接用 spawn 當下記下的 pid 當 pgid，不在要殺的時候重查 getpgid（重查的瞬間
    PID 可能已被回收再配置，killpg 就會打到無關的行程群）。
    """
    pid = proc.pid
    if hasattr(os, "killpg") and pid != os.getpgid(0):  # 護欄：不要打到自己的群
        try:
            os.killpg(pid, sig)
            return
        except OSError:
            pass
    try:
        proc.send_signal(sig)
    except OSError:
        pass
