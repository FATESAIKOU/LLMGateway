"""lgwd/subproc.py（#60 tasks 1.1）：起停、killpg 只打自己記下 pid 的群、stderr 只留尾端。"""

import os
import signal
import sys
import threading
import unittest
from unittest import mock

from lgwd import subproc


def _reap(proc):
    """測試收尾：只停自己起的那個行程群。"""
    if proc.poll() is None:
        subproc.signal_child(proc, signal.SIGKILL)
    proc.wait(timeout=10)
    if proc.stderr:
        proc.stderr.close()


class SpawnTest(unittest.TestCase):
    def test_獨立行程群且stdout丟掉(self):
        proc = subproc.spawn([sys.executable, "-c", "import os;print(os.getpgid(0)==os.getpid())"],
                             dict(os.environ))
        self.addCleanup(_reap, proc)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(proc.stdout)  # DEVNULL
        self.assertEqual(os.getpgid(proc.pid) if proc.poll() is None else proc.pid, proc.pid)

    def test_tty直接exec的程式有控制終端(self):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        code = ("import os,sys\ntry:\n os.close(os.open('/dev/tty', os.O_RDWR)); sys.exit(0)\n"
                "except OSError: sys.exit(7)")
        try:
            proc = subproc.spawn([sys.executable, "-c", code], dict(os.environ), tty=slave)
        finally:
            os.close(slave)
        self.addCleanup(_reap, proc)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_環境與cwd由呼叫方給(self):
        code = "import os,sys;sys.stderr.write(os.getcwd()+'|'+os.environ.get('ONLY_ME','')+'|'+str('GH_TOKEN' in os.environ))"
        cwd = os.path.realpath(os.path.dirname(__file__))
        proc = subproc.spawn([sys.executable, "-c", code], {"ONLY_ME": "x", "PATH": os.environ["PATH"]}, cwd=cwd)
        self.addCleanup(_reap, proc)
        holder = []
        subproc.read_tail(proc.stderr, holder)
        proc.wait(timeout=10)
        self.assertEqual(holder[0].decode(), f"{cwd}|x|False")

    def test_stderr可丟掉(self):
        import subprocess
        proc = subproc.spawn([sys.executable, "-c", "import sys;sys.stderr.write('x')"],
                             dict(os.environ), stderr=subprocess.DEVNULL)
        self.addCleanup(_reap, proc)
        proc.wait(timeout=10)
        self.assertIsNone(proc.stderr)


class SignalChildTest(unittest.TestCase):
    def test_killpg_打整個群_含孫行程(self):
        code = ("import subprocess,sys,time;"
                "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                "sys.stderr.write(str(c.pid)+'\\n');sys.stderr.flush();time.sleep(60)")
        proc = subproc.spawn([sys.executable, "-c", code], dict(os.environ))
        self.addCleanup(_reap, proc)
        grandchild = int(proc.stderr.readline().strip())
        subproc.signal_child(proc, signal.SIGKILL)
        proc.wait(timeout=10)
        for _ in range(100):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            threading.Event().wait(0.05)
        else:
            self.fail("grandchild still alive")

    def test_只對記下的pid當pgid_不重查getpgid(self):
        proc = subproc.spawn([sys.executable, "-c", "import time;time.sleep(60)"], dict(os.environ))
        self.addCleanup(_reap, proc)
        with mock.patch("os.getpgid", wraps=os.getpgid) as spy, \
             mock.patch("os.killpg", wraps=os.killpg) as killpg:
            subproc.signal_child(proc, signal.SIGTERM)
        self.assertEqual(killpg.call_args.args[0], proc.pid)
        self.assertEqual([c.args for c in spy.call_args_list], [(0,)])  # 只查自己的群（護欄）

    def test_不打到自己的群(self):
        fake = mock.Mock(pid=os.getpgid(0))
        with mock.patch("os.killpg") as killpg:
            subproc.signal_child(fake, signal.SIGTERM)
        killpg.assert_not_called()
        fake.send_signal.assert_called_once_with(signal.SIGTERM)


class ReadTailTest(unittest.TestCase):
    def test_stderr只留尾端(self):
        code = "import sys;sys.stderr.write('A'*20000+'END')"
        proc = subproc.spawn([sys.executable, "-c", code], dict(os.environ))
        self.addCleanup(_reap, proc)
        holder = []
        subproc.read_tail(proc.stderr, holder, limit=100)
        proc.wait(timeout=10)
        self.assertEqual(len(holder[0]), 100)
        self.assertTrue(holder[0].endswith(b"END"))


if __name__ == "__main__":
    unittest.main()
