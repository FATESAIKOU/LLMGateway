"""共用測試 helper：判斷行程還活不活（#136）。

Linux worker 容器的 PID 1 不是 init、不回收殭屍：killpg 殺掉的孫行程停在
/proc/<pid>/stat 的 Z 狀態，而 `os.kill(pid, 0)` 對殭屍仍算成功 → 誤判「還活著」，
害 3 個測試在 worker 上誤紅（lgwd 程式本身沒錯）。

所以先照 `os.kill(pid, 0)` 判，再看 /proc 的狀態欄：狀態 Z（殭屍）算死。
讀不到 /proc（macOS）就維持 `os.kill` 的結果。
"""

from __future__ import annotations

import os
import sys


def alive(pid: int) -> bool:
    """pid 還在跑才算活；Linux 上的殭屍（狀態欄 Z）算死。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # 行程存在，只是不是自己的
    if not sys.platform.startswith("linux"):
        return True                       # 沒有 /proc（macOS）：維持 os.kill 的結果
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat_text = fh.read()
    except FileNotFoundError:
        return False                      # /proc/<pid> 已被移除：行程死了且被收掉
    except OSError:
        return True                       # 讀不到狀態：維持 os.kill 的結果
    # 欄 2 是 comm，可能含空白與括號：用最後一個 ')' 之後的第一欄（狀態欄）
    fields = stat_text[stat_text.rfind(")") + 1:].split()
    return not fields or fields[0] != "Z"
