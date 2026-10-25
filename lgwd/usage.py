"""用量（#60；spec usage-reporting）：list／resolve 回覆裡的 `usage` 與 `available_again_at`。

- 用量是附加資訊，**壞了不能拖累 list／resolve**：查詢路徑只做字典讀取、純計算與
  「是否觸發」的判斷（旗標檢查＋建一條執行緒），不做網路或子行程 I/O、不等取用量；
- 取用量只在被查到、快取過期、啟用、key 已部署、不在退避期間時才觸發，並在背景
  執行緒做；同一個 subscription 同時只有一個（single-flight）；
- 觸發判斷、in-flight 旗標、「已停止」旗標與子行程登記共用同一把鎖；
- 失敗退避＝max(退避, Retry-After)，退避從 TTL 起算、連續失敗加倍、上限 max(30 分鐘, TTL)；
- 快取只在記憶體（鍵＝subscription id），不進 SQLite、不進稽核；
- key／token 不進回應、log、例外訊息；失敗只記固定碼（reason）與 provider id。
只用標準函式庫。
"""

from __future__ import annotations

import datetime as _dt
import logging
import signal
import subprocess
import threading
from dataclasses import dataclass

from . import subproc
from .clock import Clock, from_rfc3339, to_rfc3339

_LOG = logging.getLogger("lgwd.usage")

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_APPLICABLE = "not_applicable"

# reason 固定碼（spec「usage 物件的形狀」）；不回顯上游內容。
NOT_FETCHED_YET = "not_fetched_yet"
DISABLED = "disabled"
NO_KEY = "no_key"
UPSTREAM_ERROR = "upstream_error"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
PARSE_ERROR = "parse_error"
NO_WINDOWS = "no_windows"
STALE_EXPIRED = "stale_expired"
INTERNAL = "internal"

WINDOWS = ("session", "weekly")
EXPIRE_AFTER = _dt.timedelta(hours=6)     # 超過這麼久的舊值不再顯示
BACKOFF_FLOOR_CAP = 1800.0                # 退避上限＝max(這個, TTL)
_SHUTDOWN_GRACE = 5.0                     # stop() 等取用量執行緒收乾淨的秒數


@dataclass(frozen=True)
class FetchResult:
    """一次取用量的結果。成功＝windows 非 None（{"session"/"weekly": (remaining_percent, resets_at)|None}，
    至少一個非 None）；失敗＝reason 為固定碼，retry_after 是 429 的 Retry-After 秒數（選填）。"""
    windows: dict | None = None
    reason: str | None = None
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.windows is not None


def failure(reason: str, retry_after: float | None = None) -> FetchResult:
    return FetchResult(windows=None, reason=reason, retry_after=retry_after)


class ProcessGuard:
    """取用量子行程的登記處：spawn 與登記 pid 在同一把鎖內，spawn 前再查「已停止」旗標。"""

    def __init__(self, lock: threading.Lock):
        self._lock = lock
        self.stopped = False
        self.procs: set = set()

    def spawn(self, argv: list, env: dict, cwd: str | None = None, stderr=subprocess.PIPE):
        """起子行程並登記；已停止回 None。OSError 照丟給呼叫方。"""
        with self._lock:
            if self.stopped:
                return None
            proc = subproc.spawn(argv, env, cwd=cwd, stderr=stderr)
            self.procs.add(proc)
            return proc

    def release(self, proc) -> None:
        with self._lock:
            self.procs.discard(proc)


class _Entry:
    __slots__ = ("fetched_at", "windows", "last_reason", "failures", "next_allowed", "in_flight")

    def __init__(self):
        self.fetched_at = None
        self.windows = None
        self.last_reason = None
        self.failures = 0
        self.next_allowed = None
        self.in_flight = False


def _not_applicable() -> dict:
    return {"status": STATUS_NOT_APPLICABLE, "source": None, "fetched_at": None,
            "stale": None, "reason": None, "session": None, "weekly": None}


def _unknown(source, reason, fetched_at=None, stale=None) -> dict:
    return {"status": STATUS_UNKNOWN, "source": source, "fetched_at": fetched_at,
            "stale": stale, "reason": reason, "session": None, "weekly": None}


def internal_usage(source=None) -> dict:
    """組裝路徑本身出錯時的保底：unknown／internal（呼叫方捕捉例外後用）。"""
    return _unknown(source, INTERNAL)


def build_usage(source, entry: _Entry | None, now: _dt.datetime, ttl: float) -> dict:
    """由快取內容（純計算）組 `usage` 物件：七個鍵恆在。"""
    if entry is None or entry.fetched_at is None:
        return _unknown(source, (entry.last_reason if entry else None) or NOT_FETCHED_YET)
    age = now - entry.fetched_at
    fetched_at = to_rfc3339(entry.fetched_at)
    stale = age.total_seconds() > ttl
    if age > EXPIRE_AFTER:
        return _unknown(source, STALE_EXPIRED, fetched_at, True)
    shown = {}
    for name in WINDOWS:
        window = (entry.windows or {}).get(name)
        if window is not None and window[1] > now:   # resets_at 已過＝不假設回到 100%
            shown[name] = {"remaining_percent": window[0], "resets_at": to_rfc3339(window[1])}
        else:
            shown[name] = None
    if all(value is None for value in shown.values()):
        return _unknown(source, STALE_EXPIRED, fetched_at, stale)
    return {"status": STATUS_OK, "source": source, "fetched_at": fetched_at,
            "stale": stale, "reason": entry.last_reason if stale else None, **shown}


def available_again_at(usage: dict, cooldown_until: _dt.datetime | None, now: _dt.datetime,
                       deployed: bool) -> str | None:
    """cooldown.until（未來才算）與「remaining_percent ≤ 0 的窗口之 resets_at」中最晚者。

    key 沒部署一律 None；usage 沒有窗口（unknown／not_applicable）不推測。
    remaining_percent 用原始數值判斷（0.2 不算用完）。
    """
    if not deployed:
        return None
    candidates = []
    if cooldown_until is not None and cooldown_until > now:
        candidates.append(cooldown_until)
    for name in WINDOWS:
        window = usage.get(name)
        if window is not None and window["remaining_percent"] <= 0:
            candidates.append(from_rfc3339(window["resets_at"]))
    return to_rfc3339(max(candidates)) if candidates else None


class UsageService:
    """快取＋觸發＋退避＋啟停。

    fetchers：{subscription id: fetcher}；沒有的 subscription 一律 not_applicable。
    fetcher 介面：`source`、`default_ttl`、`min_ttl`、`default_enabled`（屬性）與
    `fetch(key, guard) -> FetchResult`（任何例外由這裡捕捉成 internal）。
    settings：{subscription id: UsageSetting}（政策檔 `usage` 段）。
    key_of(subscription)：取用量執行緒用，先 reload_keys_if_changed 再讀 key；None＝沒部署。
    deployed(subscription)：查詢路徑用，key 是否部署。
    """

    def __init__(self, clock: Clock, *, fetchers: dict | None = None, settings: dict | None = None,
                 key_of=None, deployed=None):
        self._clock = clock
        self._fetchers = dict(fetchers or {})
        self._key_of = key_of or (lambda subscription: None)
        self._deployed = deployed or (lambda subscription: False)
        self._lock = threading.Lock()
        self._guard = ProcessGuard(self._lock)
        self._entries: dict[str, _Entry] = {}
        self._threads: list = []
        self._enabled: dict[str, bool] = {}
        self._ttl: dict[str, float] = {}
        for sub, fetcher in self._fetchers.items():
            setting = (settings or {}).get(sub)
            enabled = setting.enabled if setting and setting.enabled is not None else fetcher.default_enabled
            ttl = float(setting.ttl_seconds) if setting and setting.ttl_seconds is not None \
                else float(fetcher.default_ttl)
            if ttl < fetcher.min_ttl:
                _LOG.warning("usage ttl for %s below minimum, clamped to %gs", sub, fetcher.min_ttl)
                ttl = float(fetcher.min_ttl)
            self._enabled[sub] = enabled
            self._ttl[sub] = ttl

    def source_of(self, subscription: str) -> str | None:
        """該 subscription 的 usage.source（沒有取用量實作為 None）；組裝保底用。"""
        fetcher = self._fetchers.get(subscription)
        return fetcher.source if fetcher is not None else None

    # --- 查詢路徑（list／resolve）：不做 I/O、不等取用量 ---
    def usage_for(self, subscription: str) -> dict:
        """該 subscription 的 `usage` 物件；快取過期且符合條件時觸發一次背景取用量。

        任何內部錯誤都回 unknown／internal（不丟例外給 list／resolve）。
        """
        fetcher = self._fetchers.get(subscription)
        if fetcher is None:
            return _not_applicable()
        try:
            if not self._enabled[subscription]:
                return _unknown(fetcher.source, DISABLED)          # disabled 優先於 no_key
            if not self._deployed(subscription):
                return _unknown(fetcher.source, NO_KEY)            # 不回舊值、不觸發
            now = self._clock.now()
            ttl = self._ttl[subscription]
            with self._lock:
                entry = self._entries.get(subscription)
                result = build_usage(fetcher.source, entry, now, ttl)
                self._maybe_trigger(subscription, fetcher, entry, now, ttl)
            return result
        except Exception as exc:  # 用量壞了不拖累主要功能
            _LOG.warning("usage assemble failed for %s: %s", subscription, type(exc).__name__)
            return _unknown(fetcher.source, INTERNAL)

    def _maybe_trigger(self, subscription, fetcher, entry, now, ttl) -> None:
        """（持鎖）符合條件才建背景執行緒。"""
        if self._guard.stopped:
            return
        if entry is None:
            entry = self._entries[subscription] = _Entry()
        if entry.in_flight:
            return
        if entry.next_allowed is not None and now < entry.next_allowed:
            return
        if entry.fetched_at is not None and (now - entry.fetched_at).total_seconds() <= ttl:
            return
        entry.in_flight = True
        thread = threading.Thread(target=self._run, args=(subscription, fetcher),
                                  name=f"usage-{subscription}", daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        try:
            thread.start()
        except Exception:  # 起不了執行緒：放掉旗標，下次再試
            entry.in_flight = False
            raise

    # --- 背景執行緒 ---
    def _run(self, subscription: str, fetcher) -> None:
        try:
            key = self._key_of(subscription)
            result = failure(NO_KEY) if key is None else fetcher.fetch(key, self._guard)
            if not isinstance(result, FetchResult):
                result = failure(INTERNAL)
        except Exception as exc:  # 例外在執行緒內捕捉，轉成 unknown（只記型別名）
            _LOG.warning("usage fetch failed for %s: %s", subscription, type(exc).__name__)
            result = failure(INTERNAL)
        self._finish(subscription, result)

    def _finish(self, subscription: str, result: FetchResult) -> None:
        now = self._clock.now()
        ttl = self._ttl[subscription]
        with self._lock:
            entry = self._entries[subscription]
            entry.in_flight = False
            if self._guard.stopped:
                return
            if result.ok:
                entry.fetched_at, entry.windows = now, result.windows
                entry.last_reason, entry.failures, entry.next_allowed = None, 0, None
                return
            entry.last_reason = result.reason
            entry.failures += 1
            cap = max(BACKOFF_FLOOR_CAP, ttl)
            wait = min(ttl * (2 ** (entry.failures - 1)), cap)
            if result.retry_after is not None:
                wait = max(wait, float(result.retry_after))
            entry.next_allowed = now + _dt.timedelta(seconds=wait)
        _LOG.info("usage fetch failed for %s reason=%s", subscription, result.reason)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """測試用：等目前所有取用量執行緒結束；回 True 表示都結束了。"""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)
        return not any(thread.is_alive() for thread in threads)

    # --- 啟停 ---
    def stop(self, grace: float = _SHUTDOWN_GRACE) -> None:
        """設「已停止」旗標（之後不再觸發、不再 spawn）→ 一律終止在跑的子行程
        （SIGTERM→join→SIGKILL，只動自己登記的群組）。可以重複呼叫。"""
        with self._lock:
            self._guard.stopped = True
            procs = list(self._guard.procs)
            threads = list(self._threads)
        for proc in procs:
            if proc.poll() is None:
                subproc.signal_child(proc, signal.SIGTERM)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=grace)
        for proc in procs:
            if proc.poll() is None:
                subproc.signal_child(proc, signal.SIGKILL)
                _LOG.warning("usage subprocess escalated to SIGKILL")
