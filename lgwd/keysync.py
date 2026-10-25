"""lgwd 內建 key 拉取計時（#35 T2；設計 §4.3）。只用標準函式庫。

原本 key 拉取由 user 層的 `key-sync.timer` 每 10 分鐘叫一次 `key-sync.service`；
現在改成 `lgwd` 自己起一個背景執行緒：啟動後先跑一次（在 30 秒內），之後每
`interval` 秒（預設 600）用 `subprocess` 跑既有的 `ops-scripts/key-sync`。

- 關閉條件（不建執行緒；各記一行不含值的 log）：`interval` 為 0；script 沒設或
  不能用（路徑不存在／不是執行檔）。
- 逾時（預設 120 秒）就殺掉子行程（連它的行程群一起殺）；失敗、逾時與任何例外
  都只吞掉並記一行不含值的 log。獨立執行緒，**不得影響 API 服務**。
- 子行程的 stdout 直接丟掉、stderr 只在背景執行緒留最後 4KB（`_failed_reason`
  只需要最後一個 `failed=`）：它的輸出不會灌進 API 行程的記憶體。
- 子行程的環境另外放 `LGWD_KEYS`（跟 lgwd 讀同一個 keys.env；key-sync 的預設值
  還是舊版面）與 `LGWD_CODE_DIR`（從 `<current>/ops-scripts/key-sync` 推出
  `<current>`，讓 key-sync 用同一份 lgwd/keys.py 驗證）。
- 解密密碼只從 `$CREDENTIALS_DIRECTORY/keys.pass`（單行
  `LLMGW_KEYS_PASSWORD=…`）或自己的環境 `LLMGW_KEYS_PASSWORD` 取，只放進子行程
  的環境變數；MUST NOT 進 argv、log、狀態檔。
- 每次結果原子寫進 `keys.env` 同目錄的 `.keysync.status`（一行 JSON、0644）：

  `{"ts": <epoch 秒>, "ok": true/false, "reason": "<ok｜key-sync 的 failed= 代碼（白名單，不認得的記 unknown）｜timeout｜spawn_failed｜exit_<碼>／signal_<號>>"}`

  給 `llmgwctl check` 讀；不含任何 key 值或密碼。
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time

from . import subproc
from .subproc import read_tail as _read_tail
from .subproc import signal_child as _signal_child

_LOG = logging.getLogger("lgwd.keysync")

DEFAULT_INTERVAL = 600.0    # 秒；沒設 LGW_KEYSYNC_INTERVAL 時每 10 分鐘一次
DEFAULT_TIMEOUT = 120.0     # 子行程逾時秒數；逾時就殺
DEFAULT_PASSWORD_ENV = "LLMGW_KEYS_PASSWORD"
ENV_PREFIX = "LLMGW_PREFIX"
ENV_CREDENTIALS_DIRECTORY = "CREDENTIALS_DIRECTORY"
ENV_KEYS = "LGWD_KEYS"           # key-sync 讀的環境變數名（見 ops-scripts/key-sync）
ENV_CODE_DIR = "LGWD_CODE_DIR"  # key-sync 載 lgwd/keys.py 的目錄
CREDENTIAL_NAME = "keys.pass"
STATUS_FILENAME = ".keysync.status"
# 預設的 key-sync：<LLMGW_PREFIX>/opt/llmgateway/current/ops-scripts/key-sync
CURRENT_DIR = "/opt/llmgateway/current"
SYNC_SUBPATH = os.path.join("ops-scripts", "key-sync")

REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_SPAWN_FAILED = "spawn_failed"
REASON_UNKNOWN = "unknown"

# key-sync（ops-scripts/key-sync）既有的 failed= 代碼；其他的子行程失敗一律記
# unknown，不把子行程輸出裡的任意字串寫進狀態檔／log（低-4）。
KNOWN_FAILED_TAGS = frozenset({
    "bad_response", "code_missing", "decode_failed", "decrypt_failed", "format_failed",
    "gh_auth", "gh_failed", "gh_missing", "gh_not_found", "gh_timeout",
    "openssl_missing", "password_missing", "unexpected", "write_failed",
})

_FAILED_RE = re.compile(r"failed=([A-Za-z_][A-Za-z0-9_]*)")
_SHUTDOWN_GRACE = 5.0       # stop() 等子行程與執行緒收乾淨的秒數


def default_script(env=None) -> str:
    """預設的 key-sync 路徑（前綴 LLMGW_PREFIX，測試與非預設版面用）。"""
    src = os.environ if env is None else env
    prefix = (src.get(ENV_PREFIX) or "").rstrip("/")
    return os.path.join(prefix + CURRENT_DIR, SYNC_SUBPATH)


def status_path_for(keys_path: str) -> str:
    """狀態檔路徑＝keys.env 同目錄的 `.keysync.status`（新版面即 /var/lib/llmgateway）。"""
    return os.path.join(os.path.dirname(os.path.abspath(keys_path)), STATUS_FILENAME)


def code_dir_for(script: str) -> str | None:
    """從 `<current>/ops-scripts/key-sync` 推出 `<current>`（key-sync 的程式目錄）。

    新版面（/opt/llmgateway/current）下 key-sync 的預設值還是舊版面，所以要把
    `LGWD_KEYS`／`LGWD_CODE_DIR` 傳給它；不是 ops-scripts 底下的路徑就回 None
    （讓 key-sync 用自己的預設值）。
    """
    ops_dir = os.path.dirname(os.path.abspath(script))
    if os.path.basename(ops_dir) != "ops-scripts":
        return None
    return os.path.dirname(ops_dir)


def read_password(env=None) -> str | None:
    """解密密碼：`$CREDENTIALS_DIRECTORY/keys.pass` 優先，沒有再看自己的環境。

    只回值本身（呼叫方只能放進子行程環境）。讀不到、格式不對、值為空都回 None；
    失敗不記 log、不回顯內容。值前後空白忽略（bootstrap 也禁值內空白）。
    """
    src = os.environ if env is None else env
    directory = src.get(ENV_CREDENTIALS_DIRECTORY)
    if directory:
        try:
            with open(os.path.join(directory, CREDENTIAL_NAME), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        for line in text.splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == DEFAULT_PASSWORD_ENV and value.strip():
                return value.strip()
    value = src.get(DEFAULT_PASSWORD_ENV)
    return (value or "").strip() or None


def _failed_reason(stderr: bytes, returncode: int) -> str:
    """從 key-sync 的 log 抓最後一個 failed= 代碼；抓不到就用結束方式當代碼。

    只認 KNOWN_FAILED_TAGS 裡的值，其他一律 unknown（低-4：不把子行程輸出裡的
    任意字串原樣寫進狀態檔與 log）。
    """
    text = (stderr or b"").decode("utf-8", "replace")
    tags = _FAILED_RE.findall(text)
    if tags:
        return tags[-1] if tags[-1] in KNOWN_FAILED_TAGS else REASON_UNKNOWN
    return f"signal_{-returncode}" if returncode < 0 else f"exit_{returncode}"


def _spawn(script: str, env: dict) -> subprocess.Popen:
    """起 key-sync 子行程：stdout 丟掉、stderr 只讀尾巴、獨立行程群（lgwd/subproc.py）。

    包成一支是為了測試能模擬「spawn 還沒回來的窗口」（低-1）。
    """
    return subproc.spawn([script], env)


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    """同目錄暫存（暫存 0600）→ fsync → chmod → rename；失敗不留暫存、不動原檔。"""
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class KeySyncTimer:
    """背景計時器：`run_once()` 跑一次；`start()`／`stop()` 啟停執行緒。

    - 執行緒是 daemon（SIGTERM 後不拖住主程式），但 `stop()` 仍會明確叫停：
      先叫停跑中的子行程，再等執行緒收乾淨；join 逾時就對行程群升級 SIGKILL。
    - 任何例外都在這裡吞掉，只記一行不含值的 log；狀態檔永遠不含值或密碼。
    - 被 `stop()` 打斷的那次不寫狀態檔（上一次的結果留著，不記成失敗）。
    """

    def __init__(self, script: str, interval: float, *, timeout: float = DEFAULT_TIMEOUT,
                 keys_path: str | None = None, env=None):
        self.script = str(script)
        self.interval = float(interval)
        self.timeout = float(timeout)
        self.keys_path = os.path.abspath(keys_path) if keys_path else None
        self.status_path = status_path_for(self.keys_path) if self.keys_path else None
        self.code_dir = code_dir_for(self.script)
        self._env = None if env is None else dict(env)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self.runs = 0  # 測試用：跑過幾次

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if self.interval <= 0:
            _LOG.info("keysync disabled: interval=0")  # 保險：正常由 make_keysync 擋掉
            return
        self._stop.clear()
        _LOG.info("keysync enabled: script=%s interval=%gs timeout=%gs",
                  self.script, self.interval, self.timeout)
        self._thread = threading.Thread(target=self._loop, name="keysync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = _SHUTDOWN_GRACE) -> None:
        """停止旗標＋叫停子行程＋等執行緒；可以重複呼叫。

        `_proc`／`_thread` 都跟 spawn 用同一把鎖讀，所以不會漏掉「剛 spawn 完、
        還沒登記」的子行程（低-1）。`_thread` 不設回 None：執行緒真的還在跑時
        `running` 要照實回報。join 逾時就對該行程群升級 SIGKILL 並記一行 log。
        """
        self._stop.set()
        with self._lock:
            proc, thread = self._proc, self._thread
        if proc is not None and proc.poll() is None:
            _signal_child(proc, signal.SIGTERM)
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=timeout)
        if not thread.is_alive():
            return
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _signal_child(proc, signal.SIGKILL)
        _LOG.warning("keysync shutdown timeout (%gs): escalated to SIGKILL", timeout)

    def _loop(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception as exc:  # run_once 已自己吞；這裡再包一層保險
                _LOG.warning("keysync unexpected error: %s", type(exc).__name__)
            if self._stop.wait(self.interval):
                return

    def run_once(self) -> tuple[bool, str]:
        """跑一次 key-sync 並寫狀態檔；回 (ok, reason)。失敗不丟例外。"""
        ok = False
        reason = REASON_SPAWN_FAILED
        child_env = dict(os.environ if self._env is None else self._env)
        password = read_password(child_env)
        if password:
            child_env[DEFAULT_PASSWORD_ENV] = password  # 只經環境，不進 argv
        if self.keys_path:
            # key-sync 的預設路徑還是舊版面；跟 lgwd 讀同一個 keys.env、同一份程式
            child_env[ENV_KEYS] = self.keys_path
        if self.code_dir:
            child_env[ENV_CODE_DIR] = self.code_dir
        proc = None
        try:
            # 同一把鎖包住 spawn 與登記：stop() 不會落在中間（低-1）
            with self._lock:
                proc = _spawn(self.script, child_env)
                self._proc = proc
        except OSError as exc:
            _LOG.warning("keysync cannot run script: %s", type(exc).__name__)

        if proc is not None:
            tail: list[bytes] = []
            reader = threading.Thread(target=_read_tail, args=(proc.stderr, tail),
                                      name="keysync-stderr", daemon=True)
            reader.start()
            timed_out = False
            try:
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _signal_child(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
            reader.join(timeout=5)
            stderr_tail = tail[0] if tail else b""
            try:
                proc.stderr.close()
            except OSError:
                pass
            if timed_out:
                reason = REASON_TIMEOUT
            elif proc.returncode == 0:
                ok, reason = True, REASON_OK
            else:
                reason = _failed_reason(stderr_tail, proc.returncode)

        self.runs += 1
        if ok:
            _LOG.info("keysync ok")
        else:
            _LOG.warning("keysync failed reason=%s", reason)
        if self._stop.is_set() and not ok:
            # 被 stop() 打斷的那次不覆蓋上一次的狀態（不然服務重啟會留下假的失敗）
            _LOG.info("keysync result dropped (shutting down)")
            return ok, reason
        try:
            self._write_status(ok, reason)
        except OSError as exc:  # 寫不進去也只記一行
            _LOG.warning("keysync status write failed: %s", type(exc).__name__)
        return ok, reason

    def _write_status(self, ok: bool, reason: str) -> None:
        if not self.status_path:
            return
        line = json.dumps({"ts": int(time.time()), "ok": bool(ok), "reason": reason},
                          ensure_ascii=False)
        _atomic_write(self.status_path, (line + "\n").encode("utf-8"), 0o644)
