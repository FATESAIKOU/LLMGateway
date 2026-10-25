"""Ollama Cloud 取用量（#60；spec usage-reporting「Ollama Cloud 取用量」）。

`GET https://ollama.com/api/balance`，URL 寫死（不來自政策檔、不跟隨轉址），key 只放進
該次請求的 `Authorization: Bearer`；逾時 5 秒、不重試。只取 `included.session`／
`included.weekly` 的 `remaining_percent`、`resets_at`（欄位白名單）。key 不進例外訊息與 log。
HTTP 層可注入（`http_get`），測試用假的。只用標準函式庫。
"""

from __future__ import annotations

import http.client
import json
import socket

from .clock import from_rfc3339
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

HOST = "ollama.com"
PATH = "/api/balance"
URL = f"https://{HOST}{PATH}"
SOURCE = "ollama-api-balance"
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


def _parse(body: bytes) -> FetchResult:
    try:
        data = json.loads(body)
        included = data["included"]
    except (ValueError, KeyError, TypeError):
        return failure(PARSE_ERROR)
    if not isinstance(included, dict):
        return failure(PARSE_ERROR)
    windows = {}
    for name in WINDOWS:
        if name not in included:
            windows[name] = None
            continue
        obj = included[name]
        if not isinstance(obj, dict):
            return failure(PARSE_ERROR)
        percent, resets = obj.get("remaining_percent"), obj.get("resets_at")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) \
                or not 0 <= percent <= 100 or not isinstance(resets, str):
            return failure(PARSE_ERROR)
        try:
            windows[name] = (percent, from_rfc3339(resets))
        except ValueError:
            return failure(PARSE_ERROR)
    if all(value is None for value in windows.values()):
        return failure(NO_WINDOWS)          # 新方案：只有金額，一律不取
    return FetchResult(windows=windows)


class OllamaUsageFetcher:
    source = SOURCE
    default_ttl = 120
    min_ttl = 60
    default_enabled = True

    def __init__(self, http_get=None):
        self._http_get = http_get or default_http_get

    def fetch(self, key: str, guard=None) -> FetchResult:
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
        return _parse(body)


def _retry_after(headers: dict) -> float | None:
    """Retry-After 只認秒數（非負整數）；其他格式當沒有，只用退避。"""
    value = (headers or {}).get("retry-after")
    if isinstance(value, str) and value.strip().isdigit():
        return float(value.strip())
    return None
