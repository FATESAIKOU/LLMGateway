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
- **叫用方式（`-p` 或 pty）尚未鎖定**：statusline 在 `-p` 是否執行沒有文件，待部署探測
  （tasks 10.3）。鎖定前 Claude 取用量維持 disabled（政策預設）；管道 2（`/usage`）只留
  介面，未實測、不啟用。
只用標準函式庫。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile

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
# 最短的輸入與最便宜的模型別名（未實測；待 tasks 10.3 探測後鎖定）
PROMPT = "ok"
MODEL = "haiku"
RATE_FILE = "rate.json"
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


class ClaudeUsageFetcher:
    source = SOURCE
    default_ttl = 1800
    min_ttl = 1800          # 每次取耗訂閱額度：政策不能調低
    default_enabled = False  # 部署授權前關閉

    def __init__(self, cwd: str | None, command: str = "claude", work_dir: str | None = None,
                 timeout: float = TIMEOUT_SECONDS):
        self._cwd = cwd
        self._command = command
        self._work_dir = work_dir or default_work_dir()
        self._timeout = timeout

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
        settings = json.dumps({"statusLine": {
            "type": "command", "command": f"cat > {shlex.quote(rate_path)}"}})
        argv = [exe, "-p", PROMPT, "--model", MODEL, "--settings", settings]
        try:
            # stdout／stderr 都丟掉：子行程輸出可能含任何內容，一律不看、不記
            proc = guard.spawn(argv, env, self._cwd, stderr=subprocess.DEVNULL)
        except OSError:
            return failure(UPSTREAM_ERROR)
        if proc is None:
            return failure(INTERNAL)             # 已停止；結果會被丟掉
        timed_out = False
        try:
            proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            subproc.signal_child(proc, signal.SIGKILL)   # 只動自己記下 pid 的群組
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            guard.release(proc)
        if timed_out:
            return failure(TIMEOUT)
        try:
            with open(rate_path, encoding="utf-8") as fh:
                text = fh.read(_MAX_FILE)
        except FileNotFoundError:
            # statusline 沒跑（trust 關卡、叫用方式不對…）：結束碼非 0 算上游錯，否則沒有窗口
            return failure(UPSTREAM_ERROR if proc.returncode else NO_WINDOWS)
        except (OSError, ValueError):
            return failure(PARSE_ERROR)
        return parse_rate_limits(text)

    def fetch_via_usage_command(self, key: str, guard) -> FetchResult:
        """管道 2（`-p` 下的 `/usage`）：未實測、不啟用、不鎖定格式（tasks 10.3）。"""
        return failure(UPSTREAM_ERROR)
