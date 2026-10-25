"""lgwd/usage_claude.py（#60 tasks 5.1）：用假 claude（tests/fakes/fake_claude.py）驗證。

不呼叫真的 Claude Code、不連網、不碰真 token；測試只收掉自己記下 pid 的行程。
#68：假 claude 在 pty 下跑（互動式叫用，不加 -p），預設回完不退出（像真機）；「自己退出」是特例。
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lgwd.usage import ProcessGuard
from lgwd.usage_claude import (
    WORK_PREFIX,
    ClaudeUsageFetcher,
    parse_rate_limits,
    sweep_work_dir,
    statusline_command,
)

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake_claude.py")
TOKEN = "canary-claude-token-0001"
NOW = 1_800_000_000
PAYLOAD = json.dumps({"rate_limits": {
    "five_hour": {"used_percentage": 25, "resets_at": NOW + 3600},
    "seven_day": {"used_percentage": 50.5, "resets_at": NOW + 86400}}})


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ClaudeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.cwd = self.root / "trusted"
        self.cwd.mkdir()
        self.work = self.root / "work"
        self.dump = self.root / "dump.jsonl"
        self.pidfile = self.root / "child.pid"
        self.guard = ProcessGuard(threading.Lock())
        self.addCleanup(self.tmp.cleanup)

    def command(self, mode="ok", payload=PAYLOAD, noise="", exit_style="cmd", delay="1.5"):
        """python 寫的包裝（不經 /bin/sh：sh 當 session leader 會自己拿到控制終端，遮住 TIOCSCTTY）。"""
        path = self.root / "claude"
        path.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            f"os.environ.update(FAKE_CLAUDE_MODE={mode!r}, FAKE_CLAUDE_DUMP={str(self.dump)!r},\n"
            f"                  FAKE_CLAUDE_PIDFILE={str(self.pidfile)!r}, FAKE_CLAUDE_PAYLOAD={payload!r},\n"
            f"                  FAKE_CLAUDE_NOISE={noise!r}, FAKE_CLAUDE_EXIT={exit_style!r},\n"
            f"                  FAKE_CLAUDE_DELAY={delay!r})\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {FAKE!r}] + sys.argv[1:])\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def fetcher(self, command=None, cwd="default", timeout=20.0, **kw):
        return ClaudeUsageFetcher(
            cwd=str(self.cwd) if cwd == "default" else cwd,
            command=command or self.command(**kw), work_dir=str(self.work), timeout=timeout,
            ready_quiet=0.3, ready_max=5.0, norate_grace=0.8, exit_wait=1.5)

    def dumps(self):
        return [json.loads(line) for line in self.dump.read_text().splitlines()]


class FetchTest(ClaudeCase):
    def test_換算_remaining是100減used(self):
        got = self.fetcher().fetch(TOKEN, self.guard)
        self.assertTrue(got.ok)
        self.assertEqual(got.windows["session"][0], 75)
        self.assertEqual(got.windows["weekly"][0], 49.5)
        self.assertEqual(int(got.windows["session"][1].timestamp()), NOW + 3600)
        self.assertEqual(got.windows["session"][1].utcoffset().total_seconds(), 0)

    def test_欄位缺席_no_windows(self):
        for payload in ('{"rate_limits": {}}', '{"rate_limits": null}'):
            with self.subTest(payload):
                got = self.fetcher(payload=payload).fetch(TOKEN, self.guard)
                self.assertEqual((got.ok, got.reason), (False, "no_windows"))

    def test_只有一個窗口_另一個是None不是0(self):
        payload = json.dumps({"rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": NOW}}})
        got = self.fetcher(payload=payload).fetch(TOKEN, self.guard)
        self.assertTrue(got.ok)
        self.assertIsNone(got.windows["session"])
        self.assertEqual(got.windows["weekly"][0], 90)

    def test_互動式叫用_不加p_有pty與控制終端(self):
        self.assertTrue(self.fetcher().fetch(TOKEN, self.guard).ok)
        (seen,) = self.dumps()
        self.assertNotIn("-p", seen["argv"])
        self.assertEqual(seen["argv"][:2], ["--model", "haiku"])
        self.assertIn("--settings", seen["argv"])
        self.assertEqual(seen["isatty"], [True, True, True])
        self.assertTrue(seen["ctty"])
        self.assertEqual(Path(str(self.dump) + ".input").read_text().split()[0], "ok")

    def test_第1次無rate_limits_第2次有_保留有的那次(self):
        got = self.fetcher().fetch(TOKEN, self.guard)    # 啟動時那次沒有、送出後那次有；claude 不會自己退出
        self.assertTrue(got.ok)
        self.assertEqual(got.windows["session"][0], 75)

    def test_拿到rate_limits就收行程(self):
        started = time.monotonic()
        got = self.fetcher(timeout=30.0).fetch(TOKEN, self.guard)
        self.assertTrue(got.ok)
        self.assertLess(time.monotonic() - started, 10)       # 不必等逾時
        pid = self.dumps()[0]["pid"]
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(alive(pid))
        self.assertEqual(self.guard.procs, set())

    def test_claude不理會exit_也會被終止自己的群組(self):
        started = time.monotonic()
        got = self.fetcher(exit_style="ignore", timeout=30.0).fetch(TOKEN, self.guard)
        self.assertTrue(got.ok)
        self.assertLess(time.monotonic() - started, 15)
        pid = self.dumps()[0]["pid"]
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(alive(pid))

    def test_claude自己退出是特例_仍可取到用量(self):
        got = self.fetcher(exit_style="self").fetch(TOKEN, self.guard)
        self.assertTrue(got.ok)

    def test_statusline命令_沒有rate_limits的後續呼叫不覆蓋已寫的(self):
        path = str(self.root / "rate.json")
        run = lambda payload: subprocess.run(statusline_command(path), shell=True, check=True,
                                             input=payload.encode())
        run("{}")
        self.assertFalse(os.path.exists(path))               # 沒有 rate_limits：不寫
        run(PAYLOAD)
        run('{"model": "x"}')
        run("{}")
        self.assertEqual(Path(path).read_text(), PAYLOAD)
        self.assertEqual([p.name for p in self.root.iterdir() if ".tmp" in p.name], [])
        self.assertFalse(os.path.exists(path + ".calls"))    # 都還沒有 API 回應：沒有記號
        run('{"cost": {"total_api_duration_ms": 0}}')
        self.assertFalse(os.path.exists(path + ".calls"))
        run('{"cost": {"total_api_duration_ms": 1500}}')
        run('{"cost":{"total_api_duration_ms":7},"rate_limits":{}}')
        self.assertEqual(Path(path + ".calls").read_text(), "." * 2)    # 只有已有 API 回應的呼叫留記號

    def test_送出時先有無API回應的呼叫_隔過寬限才有rate_limits_判ok不是no_windows(self):
        # 寬限（0.8 秒）只從「有 API 回應、卻沒有 rate_limits」的呼叫起算；這裡送出後的第一次呼叫
        # 還沒有 API 回應，隔 4 秒（> 寬限＋exit_wait，收尾期間也等不到）才來帶 rate_limits 的那次
        got = self.fetcher(mode="slowapi", delay="4", timeout=30.0).fetch(TOKEN, self.guard)
        self.assertTrue(got.ok, got.reason)
        self.assertEqual(got.windows["session"][0], 75)

    def test_成功收尾時假claude收到exit_自己結束(self):
        self.assertTrue(self.fetcher().fetch(TOKEN, self.guard).ok)
        deadline = time.monotonic() + 5
        inputs = ""
        while time.monotonic() < deadline and "/exit" not in inputs:
            inputs = Path(str(self.dump) + ".input").read_text()
            time.sleep(0.05)
        self.assertEqual(inputs.split(), ["ok", "/exit"])     # 先送 /exit，不是直接 SIGKILL

    def test_全部沒有rate_limits_no_windows_claude不退出也不等逾時(self):
        started = time.monotonic()
        got = self.fetcher(mode="norate", timeout=30.0).fetch(TOKEN, self.guard)
        self.assertEqual((got.ok, got.reason), (False, "no_windows"))
        self.assertLess(time.monotonic() - started, 10)
        pid = self.dumps()[0]["pid"]
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(alive(pid))

    def test_送出後出現登入錯誤畫面_upstream_error_不等逾時(self):
        started = time.monotonic()
        got = self.fetcher(mode="loginerr", timeout=30.0).fetch(TOKEN, self.guard)
        self.assertEqual((got.ok, got.reason), (False, "upstream_error"))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.guard.procs, set())

    def test_卡在trust或主題畫面_upstream_error_且不送輸入(self):
        for mode in ("trust", "theme"):
            with self.subTest(mode):
                started = time.monotonic()
                got = self.fetcher(mode=mode, timeout=30.0).fetch(TOKEN, self.guard)
                self.assertEqual((got.ok, got.reason), (False, "upstream_error"))
                self.assertLess(time.monotonic() - started, 10)   # 不等到逾時
                self.assertFalse(Path(str(self.dump) + ".input").exists())   # 沒有替人按 Enter
                self.assertEqual(self.guard.procs, set())
                pid = self.dumps()[-1]["pid"]
                for _ in range(100):
                    if not alive(pid):
                        break
                    time.sleep(0.05)
                self.assertFalse(alive(pid))

    def test_statusline沒跑_結束碼0是no_windows_非0是upstream_error(self):
        # 自己退出的特例（statusline 沒跑就退出）
        self.assertEqual(self.fetcher(mode="silent", exit_style="self").fetch(TOKEN, self.guard).reason,
                         "no_windows")
        self.assertEqual(self.fetcher(mode="fail").fetch(TOKEN, self.guard).reason, "upstream_error")

    def test_可執行檔跳板exec失敗_upstream_error(self):
        bad = self.root / "bad-claude"
        bad.write_text("#!/nonexistent/interpreter\n")
        bad.chmod(0o755)
        got = self.fetcher(command=str(bad)).fetch(TOKEN, self.guard)
        self.assertEqual((got.ok, got.reason), (False, "upstream_error"))

    def _fd_count(self):
        return len(os.listdir("/dev/fd"))

    def test_fd不漏_成功與spawn失敗各一次(self):
        fetcher = self.fetcher()
        fetcher.fetch(TOKEN, self.guard)          # 暖機：先讓一次性的 fd 都開過
        before = self._fd_count()
        self.assertTrue(fetcher.fetch(TOKEN, self.guard).ok)
        self.assertEqual(self._fd_count(), before)
        for exc in (OSError("boom"), subprocess.SubprocessError("boom")):
            with mock.patch.object(ProcessGuard, "spawn", side_effect=exc):
                self.assertEqual(fetcher.fetch(TOKEN, self.guard).reason, "upstream_error")
            self.assertEqual(self._fd_count(), before)
        self.guard.stopped = True
        self.assertEqual(fetcher.fetch(TOKEN, self.guard).reason, "internal")
        self.assertEqual(self._fd_count(), before)

    def test_parse_error_不補0(self):
        bad = ['{"rate_limits": {"five_hour": {"used_percentage": "25", "resets_at": 1}}}',
               '{"rate_limits": {"five_hour": {"used_percentage": 25}}}',
               '{"rate_limits": {"five_hour": {"used_percentage": 250, "resets_at": 1}}}',
               '{"rate_limits": {"five_hour": 3}}', '{"rate_limits": 3}', "not json", "[]"]
        for payload in bad:
            with self.subTest(payload):
                got = parse_rate_limits(payload)
                self.assertEqual((got.ok, got.reason), (False, "parse_error"))
        broken = '{"rate_limits": {"five_hour": {"used_percentage": "25", "resets_at": 1}}}'
        self.assertEqual(self.fetcher(payload=broken).fetch(TOKEN, self.guard).reason, "parse_error")

    def test_token只在環境_環境不繼承服務的環境(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "canary-gh", "SSH_AUTH_SOCK": "/x",
                                          "ANTHROPIC_API_KEY": "canary-anthropic"}):
            self.fetcher().fetch(TOKEN, self.guard)
        (seen,) = self.dumps()
        self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        for name in ("GH_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, seen["env"])
        self.assertNotIn(TOKEN, json.dumps(seen["argv"]))
        # 工作檔：run 目錄用完即刪，work 目錄下不留任何含 token 的東西
        for path in self.work.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN, path.read_text())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_兩次取用量cwd是同一個固定路徑(self):
        fetcher = self.fetcher()
        fetcher.fetch(TOKEN, self.guard); fetcher.fetch(TOKEN, self.guard)
        cwds = [d["cwd"] for d in self.dumps()]
        self.assertEqual(cwds, [str(self.cwd)] * 2)

    def test_cwd不存在或沒設定_upstream_error且不起子行程(self):
        for cwd in (str(self.root / "missing"), None, ""):
            with self.subTest(cwd=cwd):
                got = self.fetcher(cwd=cwd).fetch(TOKEN, self.guard)
                self.assertEqual(got.reason, "upstream_error")
        self.assertFalse(self.dump.exists())
        self.assertFalse((self.root / "missing").exists())    # 不自動建立

    def test_找不到可執行檔(self):
        got = self.fetcher(command="definitely-not-installed-claude").fetch(TOKEN, self.guard)
        self.assertEqual((got.ok, got.reason), (False, "upstream_error"))

    def test_已停止不起子行程(self):
        self.guard.stopped = True
        got = self.fetcher().fetch(TOKEN, self.guard)
        self.assertFalse(got.ok)
        self.assertFalse(self.dump.exists())

    def test_子行程輸出不進log(self):
        noise = "canary-noise-from-child"
        import logging
        handler = logging.Handler()
        records = []
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            got = self.fetcher(mode="noisy", noise=noise).fetch(TOKEN, self.guard)
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertTrue(got.ok)
        self.assertNotIn(noise, "\n".join(r.getMessage() for r in records))
        self.assertNotIn(noise, repr(got))

    def test_逾時只收自己的群組_不動無關行程(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                     start_new_session=True)
        self.addCleanup(lambda: (bystander.kill(), bystander.wait(10)))
        started = time.monotonic()
        got = self.fetcher(mode="hang", timeout=10.0).fetch(TOKEN, self.guard)    # 寬鬆：Mac 負載高時孫行程也來得及起
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual((got.ok, got.reason), (False, "timeout"))
        grandchild = int(self.pidfile.read_text())
        for _ in range(100):
            if not alive(grandchild):
                break
            time.sleep(0.05)
        self.assertFalse(alive(grandchild), "整個行程群組都要被收掉")
        self.assertIsNone(bystander.poll())               # 無關的行程不受影響
        self.assertEqual(self.guard.procs, set())          # 登記已釋放

    def test_stop會終止跑到一半的子行程(self):
        from lgwd.usage import UsageService
        service = UsageService.__new__(UsageService)       # 只借 stop 的邏輯：用 guard 的鎖與集合
        service._lock = self.guard._lock
        service._guard = self.guard
        service._threads = []
        result = {}
        thread = threading.Thread(target=lambda: result.update(
            got=self.fetcher(mode="hang", timeout=30.0).fetch(TOKEN, self.guard)))
        thread.start()
        service._threads = [thread]
        for _ in range(200):
            if self.guard.procs and self.pidfile.exists():
                break
            time.sleep(0.05)
        started = time.monotonic()
        UsageService.stop(service, grace=2.0)
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.guard.procs, set())


    def test_stop_子行程收到SIGTERM就結束時_耗時遠小於grace(self):
        from lgwd.usage import UsageService
        service = UsageService.__new__(UsageService)
        service._lock = self.guard._lock
        service._guard = self.guard
        service._threads = []
        result = {}
        thread = threading.Thread(target=lambda: result.update(
            got=self.fetcher(mode="hang", timeout=30.0).fetch(TOKEN, self.guard)))
        thread.start()
        service._threads = [thread]
        for _ in range(200):
            if self.guard.procs and self.pidfile.exists():
                break
            time.sleep(0.05)
        started = time.monotonic()
        UsageService.stop(service, grace=30.0)         # 若不送 SIGTERM，要等滿 30 秒才會 SIGKILL
        elapsed = time.monotonic() - started
        thread.join(15)
        self.assertLess(elapsed, 10)


class SweepTest(ClaudeCase):
    def test_啟動掃殘留_只刪前綴相符的(self):
        self.work.mkdir()
        stale = self.work / f"{WORK_PREFIX}abc"
        stale.mkdir()
        (stale / "rate.json").write_text("{}")
        keep_dir = self.work / "other-dir"
        keep_dir.mkdir()
        keep_file = self.work / f"{WORK_PREFIX}file"
        keep_file.write_text("x")                           # 不是目錄：不動
        self.assertEqual(sweep_work_dir(str(self.work)), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(keep_dir.exists() and keep_file.exists())
        self.assertEqual(sweep_work_dir(str(self.root / "nope")), 0)
        self.assertEqual(self.fetcher().sweep(), 0)


if __name__ == "__main__":
    unittest.main()
