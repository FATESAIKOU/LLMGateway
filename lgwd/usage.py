"""狀態板的用量查詢（design 決定 11；spec status-board「用量」）。

- OpenRouter：GET https://openrouter.ai/api/v1/key
- Ollama Cloud：GET https://ollama.com/api/balance（官方上限每分鐘 10 次；我們 5 分鐘一次）
- Claude 兩把、OpenCode Zen：沒有查詢介面，固定「未知」。

查詢在背景執行緒裡定期跑，每次 10 秒逾時；失敗只記錯誤類型與 HTTP 狀態碼，
不記標頭、不記回應內容（回應可能含帳號資訊），也絕不碰 key 的值以外的地方。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .clock import Clock, to_rfc3339
from .keys import KeyStore

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OLLAMA_BALANCE_URL = "https://ollama.com/api/balance"
POLL_SECONDS = 300
TIMEOUT_SECONDS = 10

# fetch(url, key, timeout) -> (status, parsed_json_or_None)；連不上丟 OSError
Fetch = Callable[[str, str, float], tuple[int, dict | None]]


def http_fetch(url: str, key: str, timeout: float) -> tuple[int, dict | None]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, None


@dataclass
class UsageEntry:
    state: str = "unknown"            # unknown | ok | invalid_key | error
    summary: dict = field(default_factory=dict)
    checked_at: str | None = None
    last_ok_at: str | None = None
    error: str | None = None          # 錯誤類型，例如 "http_500"、"timeout"；不含內容

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "summary": self.summary,
            "checked_at": self.checked_at,
            "last_ok_at": self.last_ok_at,
            "error": self.error,
        }


def summarize_openrouter(data: dict) -> dict:
    """只留用量相關的欄位（GET /api/v1/key 的 data）。"""
    d = data.get("data", data) if isinstance(data, dict) else {}
    keep = ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining", "limit_reset",
            "is_free_tier", "free_model_daily_requests")
    return {k: d[k] for k in keep if k in d}


def summarize_ollama(data: dict) -> dict:
    """legacy 方案有 session／weekly 的 remaining_percent 與 resets_at；現行方案是美元額度。兩種都只留數字欄位。"""
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    included = data.get("included")
    if isinstance(included, dict):
        for window in ("session", "weekly"):
            w = included.get(window)
            if isinstance(w, dict):
                out[window] = {k: w[k] for k in ("remaining_percent", "resets_at") if k in w}
        for k in ("balance_usd", "allowance_usd", "period"):
            if k in included:
                out[k] = included[k]
    purchased = data.get("purchased")
    if isinstance(purchased, dict) and "balance_usd" in purchased:
        out["purchased_balance_usd"] = purchased["balance_usd"]
    return out


TARGETS = {
    "openrouter": (OPENROUTER_KEY_URL, summarize_openrouter),
    "ollama-cloud": (OLLAMA_BALANCE_URL, summarize_ollama),
}


class UsagePoller:
    """poll_once() 查一輪；start() 開背景執行緒每 POLL_SECONDS 查一次。snapshot() 給狀態板用。"""

    def __init__(self, keys: KeyStore, clock: Clock, fetch: Fetch = http_fetch, enabled: bool = True):
        self._keys = keys
        self._clock = clock
        self._fetch = fetch
        self._enabled = enabled
        self._lock = threading.Lock()
        self._entries: dict[str, UsageEntry] = {name: UsageEntry() for name in TARGETS}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        if not self._enabled:
            return
        for name, (url, summarize) in TARGETS.items():
            key = self._keys.get(name)
            now = to_rfc3339(self._clock.now())
            with self._lock:
                entry = self._entries[name]
            if key is None:
                with self._lock:
                    entry.state, entry.summary, entry.error, entry.checked_at = "unknown", {}, "not_deployed", now
                continue
            try:
                status, data = self._fetch(url, key, TIMEOUT_SECONDS)
            except TimeoutError:
                status, data, err = None, None, "timeout"
            except OSError:
                status, data, err = None, None, "unreachable"
            else:
                err = None
            with self._lock:
                entry.checked_at = now
                if status == 200 and data is not None:
                    entry.state, entry.summary, entry.error, entry.last_ok_at = "ok", summarize(data), None, now
                elif status == 401:
                    entry.state, entry.summary, entry.error = "invalid_key", {}, "http_401"
                else:
                    entry.state, entry.summary = "error", {}
                    entry.error = err or f"http_{status}"

    def snapshot(self) -> dict[str, dict]:
        """訂閱 → 用量 dict；沒有查詢介面的訂閱固定 unknown。"""
        with self._lock:
            out = {name: entry.as_dict() for name, entry in self._entries.items()}
        for name in ("claude-personal", "claude-company", "opencode-zen"):
            out[name] = UsageEntry().as_dict()
        return out

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:  # 查詢失敗不影響服務；錯誤已記在 entry
                    pass
                self._stop.wait(POLL_SECONDS)

        self._thread = threading.Thread(target=loop, name="usage-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
