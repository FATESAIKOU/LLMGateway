"""OpenRouter 取用量（#85；spec usage-reporting「OpenRouter 取用量」）。

`GET https://openrouter.ai/api/v1/key`，URL 寫死（不來自政策檔、不跟隨轉址），key 只放進
該次請求的 `Authorization: Bearer`；逾時 5 秒、不重試。取 `data.free_model_daily_requests`
的 `remaining`／`limit`（每日免費請求數）換算成 `session` 窗口（與 pipes 的 `free_daily` 管子）的
`remaining_percent`，`resets_at` 為下一個 UTC 00:00（官方：每日免費請求數以 UTC 日為單位重置）。
取 `data.limit` 與 `data.limit_remaining` 換算成 `credits` 管子的 `remaining_percent`（`resets_at` 恆為 null；
金額與配額絕對值不進 usage）。key 不進例外訊息與 log。

前提（#85 低-4）：這個 `session` 窗口只代表 `:free` 模型的每日請求數
（`free_model_daily_requests`），不是這把 key 的全部可用量；官方明寫
「Accounts and endpoints exempt from free-model limits, and BYOK requests, are not
gated by it」。出廠政策的 openrouter 只允許 `*:free`／`openrouter/free`
（policy/default.json 的 subscriptions.openrouter.models.allow），所以對得上；
若日後放寬 allow 到付費模型，`session` 百分比與 `available_again_at` 會變成誤導。
HTTP 層可注入（`http_get`），測試用假的。只用標準函式庫。

官方出處：
- https://openrouter.ai/docs/api-reference/limits
  - "To check the rate limit or credits left on an API key, make a GET request to
    https://openrouter.ai/api/v1/key."
  - "free_model_daily_requests: { used, limit, remaining }" — "Free-model requests recorded
    so far in the current UTC day"（reset 00:00 UTC）。
  - "limit_remaining: number | null — Remaining credits for the key, or null if unlimited"
- https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key
"""

from __future__ import annotations

import http.client
import json
import math
import socket
from datetime import datetime, time, timedelta, timezone

from .clock import Clock, SystemClock
from .usage import (
    FetchResult,
    NO_WINDOWS,
    PARSE_ERROR,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_ERROR,
    WINDOWS,
    failure,
)

HOST = "openrouter.ai"
PATH = "/api/v1/key"
URL = f"https://{HOST}{PATH}"
SOURCE = "openrouter-api-key"
TIMEOUT_SECONDS = 5.0
MAX_BODY = 65536


def default_http_get(url: str, headers: dict, timeout: float):
    """標準函式庫的 GET：不跟隨轉址（3xx 當一般回應回給呼叫方）。回 (status, headers, body)。"""
    if url != URL:  # 寫死的位址；不連別的主機
        raise ValueError("unexpected usage url")
    conn = http.client.HTTPSConnection(HOST, timeout=timeout)
    try:
        conn.request("GET", PATH, headers=headers)
        resp = conn.getresponse()
        body = resp.read(MAX_BODY)
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


def _next_utc_midnight(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    nxt = (now.astimezone(timezone.utc).date() + timedelta(days=1))
    return datetime.combine(nxt, time.min, tzinfo=timezone.utc)


def _parse(body: bytes, now: datetime | None = None) -> FetchResult:
    try:
        data = json.loads(body)
        payload = data["data"]
    except (ValueError, KeyError, TypeError):
        return failure(PARSE_ERROR)
    if not isinstance(payload, dict):
        return failure(PARSE_ERROR)

    windows = {name: None for name in WINDOWS}
    fmdr = payload.get("free_model_daily_requests")
    if fmdr is None:
        # 沒給這個欄位：該窗口讀不到，不算 parse_error
        pass
    elif not isinstance(fmdr, dict):
        return failure(PARSE_ERROR)
    else:
        used = fmdr.get("used")
        limit = fmdr.get("limit")
        remaining = fmdr.get("remaining")
        if isinstance(used, bool) or isinstance(limit, bool) or isinstance(remaining, bool):
            return failure(PARSE_ERROR)
        if not isinstance(limit, (int, float)) or not isinstance(remaining, (int, float)):
            return failure(PARSE_ERROR)
        if limit <= 0:
            return failure(PARSE_ERROR)
        percent = remaining / limit * 100
        if not (0 <= percent <= 100):
            return failure(PARSE_ERROR)
        resets = _next_utc_midnight(now)
        windows["session"] = (percent, resets)

    # credits 解析（C-5, C-11, C-12；design §5）：
    # 僅當 limit 為正數且 limit_remaining 在 0～limit 範圍時才產生 credits 管子；
    # 任何異常值（負數、大於 limit、型別不對、bool、limit <= 0）只丟棄 credits，不引發 parse_error 或 failure。
    # resets_at 恆為 None。
    c_limit = payload.get("limit")
    c_rem = payload.get("limit_remaining")
    if (
        not isinstance(c_limit, bool)
        and not isinstance(c_rem, bool)
        and isinstance(c_limit, (int, float))
        and isinstance(c_rem, (int, float))
        and math.isfinite(c_limit)
        and math.isfinite(c_rem)
        and c_limit > 0
        and 0 <= c_rem <= c_limit
    ):
        percent = c_rem / c_limit * 100
        windows["credits"] = (percent, None)

    if all(value is None for value in windows.values()):
        return failure(NO_WINDOWS)
    return FetchResult(windows=windows)


class OpenRouterUsageFetcher:
    source = SOURCE
    default_ttl = 120
    min_ttl = 60
    default_enabled = True

    def __init__(self, http_get=None, clock=None):
        self._http_get = http_get or default_http_get
        self._clock = clock if clock is not None else SystemClock()

    def fetch(self, key: str, guard=None) -> FetchResult:
        now = self._clock.now()
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        try:
            status, resp_headers, body = self._http_get(URL, headers, TIMEOUT_SECONDS)
        except (socket.timeout, TimeoutError):
            return failure(TIMEOUT)
        except (OSError, http.client.HTTPException):
            return failure(UPSTREAM_ERROR)       # 不帶例外內容（可能含位址／標頭）
        if status == 429:
            return failure(RATE_LIMITED, retry_after=_retry_after(resp_headers))
        if status != 200:                        # 含 3xx：不跟隨轉址
            return failure(UPSTREAM_ERROR)
        return _parse(body, now)


def _retry_after(headers: dict) -> float | None:
    """Retry-After 只認秒數（非負整數）；其他格式當沒有，只用退避。"""
    value = (headers or {}).get("retry-after")
    if isinstance(value, str) and value.strip().isdigit():
        return float(value.strip())
    return None
