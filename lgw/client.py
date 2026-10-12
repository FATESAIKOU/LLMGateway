"""三個介面的客戶端（design 決定 6；tasks 2.1）：list、get、登錄冷卻。

用法：

    client = lgw.client.Client("http://127.0.0.1:4710")  # base URL 由呼叫方給
    client.list(); client.get("claude-personal", "claude-code")
    client.cooldown("claude-personal", "2026-10-12T03:00:00Z")

- list() → GET /v1/subscriptions
- get(id, agent) → GET /v1/subscriptions/{id}?agent={agent}
- cooldown(id, until) → POST /v1/subscriptions/{id}/cooldown，body {"until": RFC 3339 UTC}；成功 204。
- 沒有應用層認證（design 決定 5a），所以不帶任何認證標頭。
- 回 Result：ok、status、data、error、unreachable。連不上或逾時回 unreachable=True、
  不丟例外；403／404／409／400 照實回狀態碼與 error。error 可能是：
  - 服務回的碼：bad_request｜invalid_until｜until_out_of_range｜
    agent_not_allowed｜not_found｜not_deployed｜body_too_large；
  - 客戶端判讀的：unreachable（連不上或逾時）｜bad_response（回覆不是 JSON 物件）。
- Result 的 repr 不印 data（get 的 data 裡有 key）；不要 log／print 整個 Result。
- 只用標準函式庫（urllib）；不引用 subprocess、herdr、agora。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.client import HTTPException


@dataclass
class Result:
    """一次呼叫的結果；repr 不印 data 的內容（S6：key 不得出現在 log）。"""

    ok: bool
    status: int | None = None
    data: dict = field(default_factory=dict)
    error: str | None = None
    unreachable: bool = False

    def __repr__(self) -> str:  # 不印 data 的內容
        return "Result(ok={0!r}, status={1!r}, error={2!r})".format(
            self.ok, self.status, self.error
        )


class Client:
    """只用 urllib 打三個介面；網路問題一律回 unreachable，不丟例外。

    base_url 不得含憑證；v0 只傳 loopback URL（例如 http://127.0.0.1:4710）。
    """

    def __init__(self, base_url: str, timeout: float = 10):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:
        return "Client(base_url={0!r}, timeout={1!r})".format(
            self._base_url, self._timeout
        )

    # --- 內部 ---

    def _send(self, method: str, path: str, body: dict | None) -> Result:
        url = self._base_url + path
        headers: dict[str, str] = {}  # 無應用層認證，不帶任何認證標頭
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return self._finish(resp.status, resp.read())
        except urllib.error.HTTPError as exc:  # 非 2xx：讀 body 再判（先於 URLError 接）
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return self._finish(exc.code, payload)
        except (urllib.error.URLError, OSError, HTTPException):
            return Result(ok=False, unreachable=True, error="unreachable")

    @staticmethod
    def _finish(status: int, payload: bytes) -> Result:
        try:
            text = payload.decode("utf-8") if payload.strip() else ""
            body = json.loads(text) if text else {}
        except (UnicodeDecodeError, ValueError):
            return Result(ok=False, status=status, error="bad_response")
        if not isinstance(body, dict):
            return Result(ok=False, status=status, error="bad_response")
        if 200 <= status < 300 and "error" not in body:
            return Result(ok=True, status=status, data=body)
        # 400／403／404／409：狀態碼與錯誤碼照實回（不重寫成拒絕理由）
        return Result(ok=False, status=status, error=str(body.get("error", status)))

    # --- 公開 API（design 決定 6 的路徑） ---

    def list(self) -> Result:
        """列出 subscriptions（不含 key；依 priority 排序由服務負責）。"""
        return self._send("GET", "/v1/subscriptions", None)

    def get(self, id: str, agent: str) -> Result:
        """取一把 key；agent 不符 403、未部署 409、不存在 404。"""
        path = "/v1/subscriptions/{0}?agent={1}".format(
            urllib.parse.quote(str(id), safe=""),
            urllib.parse.quote(str(agent), safe=""),
        )
        return self._send("GET", path, None)

    def cooldown(self, id: str, until: str) -> Result:
        """登錄冷卻；until 是何時能再用（RFC 3339 UTC 字串）；成功 204。"""
        body: dict = {"until": until}
        path = "/v1/subscriptions/{0}/cooldown".format(
            urllib.parse.quote(str(id), safe="")
        )
        return self._send("POST", path, body)
