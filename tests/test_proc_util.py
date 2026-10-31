"""tests/proc_util.py（#136）：alive() 要把殭屍當死，活著的行程仍算活。

殭屍狀態只在 Linux 的 /proc/<pid>/stat 看得到；macOS 沒有 /proc，殭屍案例跳過。
"""

import os
import subprocess
import sys
import time
import unittest

from tests.proc_util import alive


def _state(pid):
    """直接讀 /proc/<pid>/stat 的狀態欄（確認前提用，不經過要測的 alive）。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    fields = text[text.rfind(")") + 1:].split()
    return fields[0] if fields else None


class AliveTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "殭屍狀態在 /proc 才讀得到")
    def test_殭屍算死_收屍之後仍是死(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)                    # 子行程立刻結束；父行程先不 wait → 殭屍
        try:
            for _ in range(500):           # 等它真的變成殭屍（看 /proc，不依賴 helper）
                if _state(pid) == "Z":
                    break
                time.sleep(0.01)
            self.assertEqual(_state(pid), "Z", "子行程沒變成殭屍，前提不成立")
            self.assertFalse(alive(pid), "殭屍（Z）要被當成死")
        finally:
            os.waitpid(pid, 0)
        self.assertFalse(alive(pid), "收屍之後（pid 已不存在）仍是死")

    def test_活著的行程為True_結束後False(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            self.assertTrue(alive(proc.pid), "在跑的行程要算活")
        finally:
            proc.kill()                    # 只殺自己起的子行程
            proc.wait(timeout=10)
        self.assertFalse(alive(proc.pid), "行程結束且被收掉後要算死")


if __name__ == "__main__":
    unittest.main()
