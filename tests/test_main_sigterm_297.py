"""#297：SIGTERM 落在主執行緒正在寫 stderr 的中間時，handler 不能把訊號吞掉。

`lgwd/main.py` 的 on_signal 原本**先**寫一行 log、**後** stop.set()。SIGTERM 剛好
落在主執行緒自己寫 stderr（啟動時的 `serving api=…`）的中間時，logging 會撞上
BufferedWriter 的重入保護丟 RuntimeError，例外從 handler 冒出去，stop.set() 沒被
執行；systemd 只送一次 SIGTERM，行程因此要等到 TimeoutSec 才被 SIGKILL（#294
架構複審 §1）。修正後 handler 只 set 事件，log 由主執行緒在 stop.wait() 回來之後
再寫。

重現的方式（不靠 sleep 賭時機）：stderr 接一條不讀取的 pipe，先塞到只剩 FREE_BYTES
的空間。行程啟動時主執行緒會依序寫 handler 安裝前那兩行（`auth mode=off`、
`keysync disabled: interval=0`，共 121 bytes），然後卡在 `serving api=…` 那次寫入
——那行比剩下的空間長，寫不完。等 SigCgt 已有 SIGTERM（handler 已安裝）、而且主
執行緒卡在 write(2) 裡之後才送訊號；這個阻塞不會自己解除（沒有人在讀 pipe），所以
是確定性的。送完訊號才開始讀 pipe：

- 修正後：handler 只 append，寫入恢復後主執行緒醒來寫 `received …` 並收尾，行程在
  期限內以 0 結束；
- 舊的順序（先 log 後 set）：handler 重入 logging 丟 RuntimeError、stop 沒設，
  行程不結束，這支就紅。

另外有兩支靜態斷言（#297 中-1）：`on_signal` 只能做 `signalled.append(signum)`
（不得呼叫會取鎖或做 I/O 的 `set()`、`_LOG`、`print`、`write`、`wait()`），主迴圈
是 `while not signalled: time.sleep(0.5)`、不再有 `threading.Event`。`stop.set()`
本身就會取不可重入的鎖，訊號落在主執行緒持鎖的那幾微秒內會死結，機率太低做不出
穩定的端到端重現，所以在原始碼層面守。
"""

from __future__ import annotations

import ast
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.test_main import FAKE_KEYS, key_text, write_policy

REPO = Path(__file__).resolve().parent.parent
PIPE_BYTES = 4096        # 最小的 pipe 容量（一個頁）
FREE_BYTES = 150         # 只夠 handler 安裝前的兩行啟動 log，不夠 serving api
START_SECONDS = 15.0
EXIT_SECONDS = 10.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _shrink_pipe(fd: int) -> int:
    """把 pipe 縮到最小再回報實際容量（不能縮就用預設容量）。"""
    try:
        return fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_BYTES)
    except OSError:
        return fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)


def _blocked_in_write(pid: int) -> bool:
    """主執行緒（TID 同 pid）是不是正卡在一個 write(2) 裡。

    `wchan` 是 `pipe_write` 就代表卡在 pipe 的寫入；讀不到 `wchan` 時退而看
    `syscall`（1＝write）。兩者都只有同一使用者、而且是它的父行程才讀得到（這裡
    就是）。
    """
    try:
        if Path(f"/proc/{pid}/wchan").read_text().strip() == "pipe_write":
            return True
    except OSError:
        pass
    try:
        return Path(f"/proc/{pid}/syscall").read_text().split(" ", 1)[0] == "1"
    except OSError:
        return False


def _caught(pid: int) -> int:
    """行程目前「有 handler」的訊號位元遮罩（/proc/<pid>/status 的 SigCgt）。"""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("SigCgt:"):
                    return int(line.split()[1], 16)
    except OSError:
        return 0
    return 0


def _drain(fd: int, chunks: list[bytes]) -> None:
    while True:
        data = os.read(fd, 65536)
        if not data:
            return
        chunks.append(data)


def _tail(chunks: list[bytes]) -> str:
    return b"".join(chunks)[-2000:].decode(errors="replace")


def _dotted(node: ast.AST) -> str | None:
    """把呼叫目標變成 `a.b.c` 字串；不是單純名字鏈就回 None。"""
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return None


def _find_func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"lgwd/main.py 找不到 {name}()")


class SigtermDuringStderrWriteTest(unittest.TestCase):
    """SIGTERM／SIGINT 落在主執行緒寫 stderr 的中間時，行程仍要正常收尾。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "keys.env").write_text(key_text(FAKE_KEYS), encoding="utf-8")
        write_policy(self.root)

    def _cmd(self, tag: str) -> list[str]:
        return [sys.executable, "-m", "lgwd", "--auth-mode", "off",
                "--db", str(self.root / f"app-{tag}.db"),
                "--keys", str(self.root / "keys.env"),
                "--policy", str(self.root / "policy.json"),
                "--api-port", str(_free_port()),
                "--keysync-interval", "0"]

    def _run(self, signum: int) -> None:
        read_fd, write_fd = os.pipe()
        proc = None
        chunks: list[bytes] = []
        reader = None
        try:
            capacity = _shrink_pipe(write_fd)
            # handler 安裝前的兩行啟動 log 是固定格式（`auth mode=off` 53 bytes、
            # `keysync disabled: interval=0` 68 bytes），FREE_BYTES 必須放得下它們，
            # 又短於再下一行（serving api=…，至少 68 bytes）。
            self.assertGreater(FREE_BYTES, 121, "FREE_BYTES 放不進 handler 安裝前的啟動 log")
            self.assertGreater(capacity, FREE_BYTES, "pipe 容量太小")
            os.write(write_fd, b"x" * (capacity - FREE_BYTES))
            proc = subprocess.Popen(self._cmd(signal.Signals(signum).name),
                                    cwd=str(REPO), stdout=subprocess.DEVNULL,
                                    stderr=write_fd)
            os.close(write_fd)
            write_fd = -1
            # 等「handler 已安裝」且「主執行緒卡在某次 stderr 寫入」。前者看 SigCgt
            # 的 SIGTERM 位元；後者看主執行緒是不是卡在 write(2)（wchan=pipe_write）。
            # 卡住之後不會自己醒（沒有人在讀 pipe），所以是確定性的，不是 sleep 猜
            # 時機。
            deadline = time.monotonic() + START_SECONDS
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    self.fail(f"lgwd 在阻塞前就結束了：rc={proc.returncode}")
                blocked = _caught(proc.pid) & (1 << (signal.SIGTERM - 1))
                if blocked and _blocked_in_write(proc.pid):
                    break
                time.sleep(0.002)
            else:
                self.fail("lgwd 沒有卡在 stderr 寫入（或 handler 沒安裝）")

            started = time.monotonic()
            os.kill(proc.pid, signum)
            reader = threading.Thread(target=_drain, args=(read_fd, chunks))
            reader.start()
            try:
                rc = proc.wait(timeout=EXIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail(f"{signal.Signals(signum).name} 沒有讓 lgwd 結束（訊號被吞掉）："
                          + _tail(chunks))
            reader.join(EXIT_SECONDS)
            elapsed = time.monotonic() - started
            output = b"".join(chunks)
            self.assertEqual(rc, 0, _tail(chunks))
            self.assertLess(elapsed, EXIT_SECONDS)
            self.assertIn(b"received ", output)
            self.assertIn(signal.Signals(signum).name.encode(), output)
            self.assertIn(b"shutting down", output)
        finally:
            if write_fd != -1:
                os.close(write_fd)
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            if reader is not None:
                reader.join(EXIT_SECONDS)
            os.close(read_fd)

    def test_297_SIGTERM落在主執行緒寫stderr時仍會收尾(self):
        self._run(signal.SIGTERM)

    def test_297_SIGINT落在主執行緒寫stderr時仍會收尾(self):
        self._run(signal.SIGINT)


class SignalHandlerIsLockFreeTest(unittest.TestCase):
    """#297 中-1：handler 的每一行都不得取鎖或做 I/O（靜態斷言）。

    `stop.set()`／`stop.wait()` 都會取 `threading.Event` 的（不可重入）`Lock`；訊號
    落在主執行緒持有那把鎖的幾微秒內，handler 在同一執行緒再取鎖就永久死結。窗口
    太小，端到端做不出穩定重現，所以在原始碼層面守：handler 只能 append，主迴圈只
    看 list 並 `time.sleep`。
    """

    def setUp(self):
        self.tree = ast.parse((REPO / "lgwd" / "main.py").read_text(encoding="utf-8"))

    def test_297_handler只做append不取鎖不做IO(self):
        handler = _find_func(self.tree, "on_signal")
        calls = [node for node in ast.walk(handler) if isinstance(node, ast.Call)]
        self.assertEqual([_dotted(call.func) for call in calls], ["signalled.append"],
                         "handler 只准做 signalled.append(signum)")
        self.assertEqual(len(handler.body), 1, "handler 只能有這一行")
        self.assertIsInstance(handler.body[0], ast.Expr)
        call = handler.body[0].value
        self.assertEqual([arg.id for arg in call.args], ["signum"])
        # 明確列出不得出現的呼叫（會取鎖或做 I/O）；上面的「只能有一個呼叫」已涵蓋，
        # 這裡讓意圖與失敗訊息更清楚。
        forbidden = {"stop.set", "stop.wait", "_LOG", "print", "write", "wait", "set", "flush"}
        for name in {_dotted(call.func) for call in calls}:
            self.assertNotIn(name, forbidden, f"handler 呼叫了會取鎖／做 I/O 的 {name}")

    def test_297_主迴圈用signalled清單與sleep(self):
        main = _find_func(self.tree, "main")
        names = {node.id for node in ast.walk(main) if isinstance(node, ast.Name)}
        self.assertNotIn("stop", names, "main() 還留著 threading.Event 的 stop")
        loops = [node for node in ast.walk(main)
                 if isinstance(node, ast.While)
                 and isinstance(node.test, ast.UnaryOp)
                 and isinstance(node.test.op, ast.Not)
                 and isinstance(node.test.operand, ast.Name)
                 and node.test.operand.id == "signalled"]
        self.assertEqual(len(loops), 1, "找不到 while not signalled 的主迴圈")
        self.assertEqual(len(loops[0].body), 1)
        call = loops[0].body[0].value
        self.assertIsInstance(call, ast.Call)
        self.assertEqual(_dotted(call.func), "time.sleep")
        self.assertEqual([arg.value for arg in call.args], [0.5])


if __name__ == "__main__":
    unittest.main()
