"""Claude 訂閱取用量（#60；spec usage-reporting「Claude 取用量」；design 決定 3）。

Gateway 不自己用訂閱 token 打 Anthropic：執行**未修改的原版 Claude Code**，用 `--settings`
指定一個只把 stdin JSON 寫進暫存檔的 statusLine 命令，讀其中的 `rate_limits`
（`five_hour`→session、`seven_day`→weekly；給的是 used，remaining＝100 - used）。

- token 只以子行程環境變數 `CLAUDE_CODE_OAUTH_TOKEN` 傳入（不進 argv、不寫檔）；
  子行程環境用白名單，不繼承服務的環境；
- cwd 是 lgwd Config 指定的固定目錄（部署時事先 trust；不每次新建）；沒設定或不存在
  → upstream_error，不自動建立；
- 子行程 stdout／stderr 都丟掉（不進 log）；解析失敗不猜、不補 0；
- 逾時 60 秒，只終止自己記下 pid 的行程群組（lgwd/subproc.py）；
- **叫用方式是 pty（#68）**：`-p` 下 statusline 一次都不跑（2026-10-25 探測；design.md「探測結果」），
  所以以 pty 啟動互動式 claude，等輸入框就緒後送 `ok`＋Enter。互動式 claude 回完**不會自己退出**，
  結束條件不依賴它：statusline 的 rate 檔出現就收（只在帶 `rate_limits` 時以 tmp＋mv 寫入，存在＝完整）；
  或 statusline 在「已有 API 回應」的呼叫留下記號、送出後有新記號而 rate 檔遲遲不出現 → no_windows。收法：先送
  `/exit`，短暫等待沒退出再終止自己記下的行程群組。pty 畫面只在記憶體判斷（首次畫面與送出後的
  登入／錯誤關卡），不進 log、不落檔。管道 2（`/usage`）已探測：`-p` 下沒有窗口，不啟用（design.md「探測結果」）。
只用標準函式庫。
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import logging
import os
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time

from . import subproc
from .usage import (
    FetchResult,
    INTERNAL,
    NO_WINDOWS,
    PARSE_ERROR,
    TIMEOUT,
    UPSTREAM_ERROR,
    failure,
)

_LOG = logging.getLogger("lgwd.usage")

SOURCE = "claude-statusline"
TIMEOUT_SECONDS = 60.0
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
WORK_PREFIX = "lgwd-usage-"             # 暫存子目錄的固定前綴；啟動時掃掉前綴相符的舊殘留
ENV_WHITELIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
# 最短的輸入與最便宜的模型別名（2026-10-25 探測：pty 下約 20 秒完成）
PROMPT = "ok"
MODEL = "haiku"
READY_QUIET = 1.5        # 畫面靜默這麼久（且已有輸出）視為輸入框就緒
READY_MAX = 12.0         # 等不到靜默時，最多等這麼久就送（探測用固定 12 秒成功過）
NORATE_GRACE = 10.0      # 送出後 statusline 有「已有 API 回應」的新呼叫、但 rate 檔沒出現：再等這麼久（每次新記號重算）才判 no_windows
EXIT_WAIT = 3.0          # 送 /exit 後等 claude 自己退出多久，沒退出才終止行程群組
_SCREEN_KEEP = 200000    # 記憶體裡留的畫面位元組上限
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
# 關卡（trust／主題／登入／錯誤）的關鍵字；比對小寫、去控制碼後的畫面。送輸入前先比對：
# 在 trust 對話框送 Enter 會替人接受預設選項，所以出現就不送、直接判 upstream_error。
# 送出後只比對送出後新增的畫面。不用單獨的 "/login"（輸入框提示或指令清單可能平常就出現）；
# "run /login" 是 token 失效時的錯誤訊息用語（推論，未對真畫面驗）。
_GATE_WORDS = ("trust this folder", "do you trust", "trust the files", "text style",
               "select login method", "paste code", "run /login")
RATE_FILE = "rate.json"
CALLS_SUFFIX = ".calls"
_FIELDS = {"five_hour": "session", "seven_day": "weekly"}
_MAX_FILE = 65536


def default_work_dir() -> str:
    """服務帳號自己目錄下的固定位置。"""
    return os.path.join(os.path.expanduser("~"), ".cache", "lgwd-usage")


def sweep_work_dir(work_dir: str) -> int:
    """啟動時掃掉前綴相符的舊殘留（前綴不符的不動）；回刪掉幾個。"""
    removed = 0
    try:
        names = os.listdir(work_dir)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(work_dir, name)
        if name.startswith(WORK_PREFIX) and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def parse_rate_limits(text: str) -> FetchResult:
    """statusline 寫下的 JSON → FetchResult。欄位缺→no_windows；型別不對→parse_error；不補 0。"""
    try:
        data = json.loads(text)
    except ValueError:
        return failure(PARSE_ERROR)
    if not isinstance(data, dict):
        return failure(PARSE_ERROR)
    limits = data.get("rate_limits")
    if limits is None:
        return failure(NO_WINDOWS)          # 非 Pro／Max，或第一個 API 回應之前
    if not isinstance(limits, dict):
        return failure(PARSE_ERROR)
    windows = {"session": None, "weekly": None}
    for field, name in _FIELDS.items():
        if field not in limits:
            continue
        obj = limits[field]
        if not isinstance(obj, dict):
            return failure(PARSE_ERROR)
        used, resets = obj.get("used_percentage"), obj.get("resets_at")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (used, resets)) \
                or not 0 <= used <= 100:
            return failure(PARSE_ERROR)
        try:
            when = _dt.datetime.fromtimestamp(resets, tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return failure(PARSE_ERROR)
        windows[name] = (100 - used, when)
    if all(value is None for value in windows.values()):
        return failure(NO_WINDOWS)
    return FetchResult(windows=windows)


def statusline_command(rate_path: str) -> str:
    """statusLine 命令：只在 stdin JSON 帶 rate_limits 時才寫 rate 檔（先寫暫存再 mv，讀的人不會看到
    寫一半的檔；暫存檔名帶 $$，並行呼叫不互踩）。第 1 次呼叫沒有 rate_limits，不寫就不會蓋掉之後帶
    rate_limits 的那一次。之後（rate 檔先出現，記號才增加）若 JSON 顯示已有 API 回應
    （`cost.total_api_duration_ms` 非 0；欄位出處是 Claude Code statusline 文件，憑記憶、未對真機驗，
    屬推論）才在 `<rate_path>.calls` 附加一個記號——「statusline 有跑、有 API 回應、卻沒有
    rate_limits」的判斷（no_windows）只從這種呼叫起算，啟動／送出時那幾次不算。"""
    tmp = shlex.quote(rate_path + ".tmp.") + "$$"
    final = shlex.quote(rate_path)
    calls = shlex.quote(rate_path + CALLS_SUFFIX)
    return (f'd=$(cat); '
            f'case "$d" in *\'"rate_limits"\'*) printf %s "$d" > {tmp} && mv {tmp} {final};; esac; '
            f'if printf %s "$d" | grep -Eq \'"total_api_duration_ms" *: *[1-9]\'; '
            f'then printf . >> {calls}; fi')


def _set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _calls(rate_path: str) -> int:
    try:
        return os.path.getsize(rate_path + CALLS_SUFFIX)
    except OSError:
        return 0


def _at_gate(screen: bytes) -> bool:
    text = _ANSI.sub(b"", screen).decode("utf-8", "replace").lower()
    return any(word in text for word in _GATE_WORDS)


class ClaudeUsageFetcher:
    source = SOURCE
    default_ttl = 1800
    min_ttl = 1800          # 每次取耗訂閱額度：政策不能調低
    default_enabled = False  # 部署授權前關閉

    def __init__(self, cwd: str | None, command: str = "claude", work_dir: str | None = None,
                 timeout: float = TIMEOUT_SECONDS, ready_quiet: float = READY_QUIET,
                 ready_max: float = READY_MAX, norate_grace: float = NORATE_GRACE,
                 exit_wait: float = EXIT_WAIT):
        self._cwd = cwd
        self._command = command
        self._work_dir = work_dir or default_work_dir()
        self._timeout = timeout
        self._ready_quiet = ready_quiet
        self._ready_max = ready_max
        self._norate_grace = norate_grace
        self._exit_wait = exit_wait

    def sweep(self) -> int:
        return sweep_work_dir(self._work_dir)

    def _env(self, token: str) -> dict:
        env = {name: os.environ[name] for name in ENV_WHITELIST if name in os.environ}
        env[TOKEN_ENV] = token
        return env

    def fetch(self, key: str, guard) -> FetchResult:
        if not self._cwd or not os.path.isdir(self._cwd):
            return failure(UPSTREAM_ERROR)       # 固定目錄沒設定或不存在：不自動建立
        env = self._env(key)
        exe = shutil.which(self._command, path=env.get("PATH"))
        if exe is None:
            return failure(UPSTREAM_ERROR)
        try:
            os.makedirs(self._work_dir, mode=0o700, exist_ok=True)
            run_dir = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=self._work_dir)
        except OSError:
            return failure(UPSTREAM_ERROR)
        try:
            return self._run(exe, env, run_dir, guard)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def _run(self, exe: str, env: dict, run_dir: str, guard) -> FetchResult:
        rate_path = os.path.join(run_dir, RATE_FILE)
        command = statusline_command(rate_path)
        settings = json.dumps({"statusLine": {"type": "command", "command": command}})
        argv = [exe, "--model", MODEL, "--settings", settings]    # 不加 -p：互動式才會跑 statusline
        master, slave = pty.openpty()
        try:
            try:
                _set_winsize(master)
                proc = guard.spawn(argv, env, self._cwd, tty=slave)
            except (OSError, subprocess.SubprocessError):
                return failure(UPSTREAM_ERROR)
            finally:
                os.close(slave)
            if proc is None:
                return failure(INTERNAL)             # 已停止；結果會被丟掉
            try:
                return self._drive(proc, master, rate_path)
            finally:
                if proc.poll() is None:
                    subproc.signal_child(proc, signal.SIGKILL)   # 只動自己記下 pid 的群組
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                guard.release(proc)
        finally:
            os.close(master)

    def _drive(self, proc, master: int, rate_path: str) -> FetchResult:
        """讀 pty 畫面（只在記憶體、不記錄）、就緒後送輸入，直到有結論／行程結束／逾時。

        結論：rate 檔出現（讀一次、解析一次）；送出後 statusline 有新呼叫而 rate 檔遲遲不出現
        → no_windows；畫面出現關卡字樣 → upstream_error；行程自己結束 → 看有沒有 rate 檔與結束碼。
        """
        start = time.monotonic()
        deadline = start + self._timeout
        screen = b""             # 送出前的畫面
        after = b""              # 送出後新增的畫面
        last_output = None
        sent = False
        last_calls = 0
        last_call_at = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                return failure(TIMEOUT)
            ready, _, _ = select.select([master], [], [], min(0.2, deadline - now))
            exited = False
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""                    # Linux：從端全關閉後讀到 EIO
                if data:                          # 讀進來就丟：只留尾端判斷關卡
                    if sent:
                        after = (after + data)[-_SCREEN_KEEP:]
                    else:
                        screen = (screen + data)[-_SCREEN_KEEP:]
                    last_output = time.monotonic()
                else:
                    exited = True
            if os.path.exists(rate_path):         # 存在＝完整（tmp＋mv）
                self._close_claude(proc, master)
                return self._read_rate(rate_path, 0)
            if exited or proc.poll() is not None:
                if _at_gate(screen + after):
                    return failure(UPSTREAM_ERROR)
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
                return self._read_rate(rate_path, code)
            now = time.monotonic()
            if not sent:
                quiet = last_output is not None and now - last_output >= self._ready_quiet
                if quiet or now - start >= self._ready_max:
                    if _at_gate(screen):
                        return failure(UPSTREAM_ERROR)
                    try:
                        os.write(master, PROMPT.encode() + b"\r")
                    except OSError:
                        return failure(UPSTREAM_ERROR)
                    sent = True
                    last_calls = _calls(rate_path)
                continue
            if _at_gate(after):
                return failure(UPSTREAM_ERROR)    # 送出後才出現的登入／錯誤畫面
            calls = _calls(rate_path)
            if calls > last_calls:
                last_calls, last_call_at = calls, now
            if last_call_at is not None and now - last_call_at >= self._norate_grace:
                self._close_claude(proc, master)
                if os.path.exists(rate_path):     # 收尾期間才出現：採用
                    return self._read_rate(rate_path, 0)
                return failure(NO_WINDOWS)        # 有 API 回應、但 statusline 沒有帶 rate_limits

    def _close_claude(self, proc, master: int) -> None:
        """送 /exit（照探測），最多等 exit_wait 秒；沒退出的由呼叫端的 finally 終止行程群組。"""
        try:
            os.write(master, b"/exit\r")
        except OSError:
            return
        end = time.monotonic() + self._exit_wait
        while proc.poll() is None and time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    if not os.read(master, 65536):
                        break
                except OSError:
                    break

    @staticmethod
    def _read_rate(rate_path: str, returncode: int | None) -> FetchResult:
        try:
            with open(rate_path, encoding="utf-8") as fh:
                text = fh.read(_MAX_FILE)
        except FileNotFoundError:
            # 沒有任何帶 rate_limits 的 statusline 呼叫：結束碼非 0 算上游錯，否則沒有窗口
            return failure(UPSTREAM_ERROR if returncode else NO_WINDOWS)
        except (OSError, ValueError):
            return failure(PARSE_ERROR)
        return parse_rate_limits(text)

    def fetch_via_usage_command(self, key: str, guard) -> FetchResult:
        """管道 2（`-p` 下的 `/usage`）：已探測，沒有窗口；不啟用、不鎖定格式（tasks 10.3）。"""
        return failure(UPSTREAM_ERROR)
