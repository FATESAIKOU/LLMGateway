"""tests/proc_util.py（#136）：alive() 要把殭屍當死，活著的行程仍算活。

殭屍狀態只在 Linux 的 /proc/<pid>/stat 看得到；macOS 沒有 /proc，殭屍案例跳過。
"""

import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import proc_util
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


class AliveMockTest(unittest.TestCase):
    """不依賴真殭屍、macOS 也能跑的 mock 單元測試（#136 審查結論 1）。

    直接模擬 os.kill 與 /proc/<pid>/stat 的讀取結果，逐條涵蓋 alive() 的分支：
    comm 含空白與括號、狀態欄 Z／S、PermissionError、ProcessLookupError、
    open 的 FileNotFoundError 與其他 OSError，以及非 Linux（darwin）時不碰 /proc。
    """

    def _run(self, stat, kill_exc=None, *, platform="linux"):
        """把環境 mock 成指定狀況後回傳 alive(123)。

        stat 為 None 時讓 open 拋 FileNotFoundError；open_exc（如 PermissionError）
        有指定時讓 open 拋那個例外，蓋過 stat。
        """
        if stat is not None:
            open_mock = mock.mock_open(read_data=stat)
        else:
            open_mock = mock.Mock(side_effect=FileNotFoundError)
        with mock.patch.object(sys, "platform", platform), \
             mock.patch("tests.proc_util.os.kill", side_effect=kill_exc), \
             mock.patch("builtins.open", open_mock):
            return proc_util.alive(123)

    def test_comm含空白括號狀態Z_算死(self):
        # comm「a b」含空白與右括號：要靠最後一個 ')' 之後的欄位判斷，狀態是 Z
        self.assertFalse(self._run("123 (a b) c) Z 1 2"), "狀態欄 Z（殭屍）要被當成死")

    def test_comm含空白括號狀態S_算活(self):
        self.assertTrue(self._run("123 (a b) c) S 1 2"), "狀態欄 S 是活著的行程")

    def test_comm本身含Z與括號_仍照狀態欄判斷(self):
        # comm「Z Z」裡有 Z 與括號：rfind(')') 取到真正的狀態欄 S → 活
        self.assertTrue(self._run("123 (Z Z) S 1 2"), "狀態欄 S 要算活，不能被 comm 裡的 Z 騙")

    def test_kill拋PermissionError_算活(self):
        self.assertTrue(self._run("x", PermissionError()), "kill(0) 拋 PermissionError＝行程存在（不是自己的），要算活")

    def test_kill拋ProcessLookupError_算死(self):
        self.assertFalse(self._run("x", ProcessLookupError()), "kill(0) 拋 ProcessLookupError＝行程不存在，要算死")

    def test_open拋FileNotFoundError_算死(self):
        self.assertFalse(self._run(None), "/proc/<pid> 已被移除＝死了且被收掉，要算死")

    def test_open拋其他OSError_算活(self):
        # 讀不到狀態（非 FileNotFoundError）時維持 os.kill 的結果：kill 成功 → 活
        open_mock = mock.mock_open(read_data=None)
        open_mock.side_effect = PermissionError()
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch("tests.proc_util.os.kill"), \
             mock.patch("builtins.open", open_mock):
            self.assertTrue(proc_util.alive(123), "open 拋 OSError 要維持 os.kill 的結果（成功 → 活）")

    def test_darwin不讀proc_維持kill結果(self):
        open_mock = mock.mock_open(read_data="123 (a b) c) Z 1 2")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch("tests.proc_util.os.kill"), \
             mock.patch("builtins.open", open_mock):
            self.assertTrue(proc_util.alive(123), "macOS（無 /proc）要直接回 os.kill 成功的結果")
        self.assertFalse(open_mock.called, "darwin 不該去 open /proc/<pid>/stat")


if __name__ == "__main__":
    unittest.main()
