"""客戶端（design 決定 6；tasks 2.1、2.3）：list、usage、resolve、登錄冷卻、roles。（get 已於 #48 移除。）

用法：

    client = lgw.client.Client("http://127.0.0.1:4710")  # base URL 由呼叫方給
    client.list()
    client.usage()
    client.resolve("opencode", "opencode-zen", model="big-pickle")
    client.resolve("opencode", "opencode-zen", model="big-pickle", data="public")
    client.resolve("claude-code", "anthropic", subscription="claude-personal")
    client.resolve("agy", "google", model="<agy models 的裸 id>")
    client.roles("impl", "opencode", data="public")
    client.cooldown("claude-personal", "2026-10-12T03:00:00Z")
    client.cooldown("opencode-zen", "2026-10-12T03:00:00Z", model="big-pickle")

- list() → GET /v1/subscriptions
- usage() → GET /v1/usage（用量一覽；回 200 時 data 是服務原樣回的 dict；連線失敗
  行為與 list() 相同，回 unreachable）
- resolve(agent, provider, model, subscription, exclude, data) → GET /v1/resolve
  （model／subscription 是供應商 id 與釘住的 id；exclude 由 client 用 ",".join
  合併；data 是選填的資料等級宣告（public／general／real），省略時不帶參數＝服務
  不依 privacy 過濾；每個 query 值都用 urllib.parse.quote(value, safe="") 編碼——
  `+` 因此編成 %2B，server 的 parse_qs 才不會解成空白）
- roles(role, agent, data=None) → GET /v1/roles/{role}?agent={agent}[&data={data}]
  （#100；spec role-allocation。data 省略時不帶參數，由服務取最嚴）
- agy（agent=agy、provider=google）的訂閱沒有憑證（kind local-login）：resolve 的
  env 是空物件 {}，模型只走 meta.model。
- cooldown(id, until, model=None) → POST /v1/subscriptions/{id}/cooldown，body
  {"until": RFC 3339 UTC}（有給 model 時多一個 "model"）；成功 204。model 是供應商
  自己的模型 id（不帶 agent 前綴）；沒有 model 即整把冷卻。
- 應用層認證依模式（#222 D4a）：`Client(..., signer=...)` 給一個
  `lgw.signer.Signer` 就對五個方法都簽（五個 `X-LGW-*` 標頭）；`signer=None`
  （預設）不帶任何認證標頭，行為與 0.1.0 相同（off 相容）。
  - 簽的是**實際送出**的 request-target（`request.selector`：`urllib.parse.quote`
    之後、含 query，**也含 base_url 的路徑前綴**，例如 base_url 是
    `http://host/prefix` 時簽 `/prefix/v1/subscriptions`）與 **實際送出**的 body
    bytes（只 `json.dumps` 一次；GET 為空 bytes）。
  - signer 出錯（找不到 ssh-keygen、逾時、私鑰有密語、armor 不是 180 bytes）回
    `Result(ok=False, error="signer_unavailable", data={"reason": ...})`，**不送出**
    任何請求，也不改送無簽版本。
  - `signer=None` 與有 signer 都**不跟隨 redirect**（3xx 原樣回 `error="redirect"`，
    這是 0.2.0 的行為變更）；401 也不改送無簽請求、任何錯誤都不自動重試。
- 回 Result：ok、status、data、error、unreachable。連不上或逾時回 unreachable=True、
  不丟例外；400／403／404／409 照實回狀態碼與 error。error 可能是：
  - 服務回的碼：bad_request｜invalid_until｜until_out_of_range｜
    agent_not_allowed｜model_not_allowed｜privacy_not_allowed｜no_model｜not_found｜
    not_deployed｜no_candidate｜body_too_large；
  - 認證（C-706）：401 `unauthorized`（data 另含 `server_date` 與
    `clock_skew_s`＝伺服器 Date 減本機時間，供呼叫方提示可能時鐘偏差，client 不自動
    調時）｜403 `forbidden`｜429 `rate_limited`（data 的 `retry_after` 是
    `Retry-After` 的整數秒）｜503 `auth_unavailable`；
  - cooling：**只有舊版 Gateway 會回**（legacy；新 Gateway 不再因冷卻拒絕，200 的
    meta 帶 cooldown／model_cooldowns 當提示）。本 client 對它照實回狀態碼與 until，
    呼叫方要保留判讀；
  - resolve 的非 2xx 回覆把 body 裡 error 以外的欄位（until、missing）放進
    Result.data（錯誤回覆沒有 env）；
  - 客戶端判讀的：unreachable（連不上或逾時）｜bad_response（回覆不是 JSON 物件）｜
    redirect（3xx，不跟隨）｜signer_unavailable（簽章失敗，未送出）。
- Result 的 repr 不印 data（resolve 的 env 有憑證值）；不要 log／print 整個 Result。
- 只用標準函式庫（urllib）；不引用 subprocess、herdr、agora（簽章子行程在
  `lgw.signer`，由呼叫方注入）。
"""

from __future__ import annotations

import email.utils
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import timezone
from http.client import HTTPException

from .signer import SignerError

#: 伺服器狀態碼對應的固定 error（body 沒有 error 時使用；C-706）。
_STATUS_ERRORS = {
    401: "unauthorized",
    403: "forbidden",
    429: "rate_limited",
    503: "auth_unavailable",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟隨 redirect：3xx 交給 `_finish` 原樣回（C-705，所有呼叫都適用）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _server_clock(headers) -> dict:
    """401 的 `server_date`／`clock_skew_s`（伺服器 Date 減本機時間）。

    讀不到或解析不了 Date 時兩個都是 None；client 不自動調時（C-706）。
    """
    raw = headers.get("Date") if headers is not None else None
    local_now = int(time.time())
    if not raw:
        return {"server_date": None, "clock_skew_s": None}
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return {"server_date": str(raw), "clock_skew_s": None}
    if parsed is None:
        return {"server_date": str(raw), "clock_skew_s": None}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return {"server_date": str(raw), "clock_skew_s": int(parsed.timestamp()) - local_now}


def _retry_after(headers, body: dict):
    """429 的 `Retry-After`（整數秒）；沒有或不是整數時退回 body 的值，再不行是 None。"""
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is not None:
        try:
            return int(str(raw).strip())
        except ValueError:
            pass
    value = body.get("retry_after")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _status_data(status: int, headers, body: dict) -> dict:
    """依狀態碼附上呼叫方需要的欄位（C-706）。"""
    if status == 401:
        return _server_clock(headers)
    if status == 429:
        return {"retry_after": _retry_after(headers, body)}
    return {}


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
    """只用 urllib 打五個呼叫（list／usage／resolve／登錄冷卻／roles）；網路問題一律回 unreachable，不丟例外。

    base_url 不得含憑證；v0 只傳 loopback URL（例如 http://127.0.0.1:4710）。
    base_url 可以帶路徑前綴（例如反向代理的 `http://127.0.0.1:4710/lgw`）：前綴
    也算在簽的 request-target 裡（簽的是 urllib 實際送出的 `request.selector`）。
    """

    def __init__(self, base_url: str, timeout: float = 10, *,
                 signer=None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._signer = signer
        # signer=None 也一樣不跟隨 redirect（C-705；0.2.0 的變更）。
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return "Client(base_url={0!r}, timeout={1!r})".format(
            self._base_url, self._timeout
        )

    # --- 內部 ---

    def _send(self, method: str, path: str, body: dict | None,
              error_data: bool = False) -> Result:
        url = self._base_url + path
        data = None
        if body is not None:
            # 只序列化一次：簽的 bytes 就是送出的 bytes（C-702）。
            data = json.dumps(body).encode("utf-8")
        # 先建 Request，再簽它的 selector：那是 urllib 實際送出的 request-target
        # （含 base_url 的路徑前綴與 query），簽的與送的同源（C-702）。
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._signer is not None:
            # 簽章失敗不送出任何請求，也不改送無簽（C-705）；分類固定、不含祕密。
            try:
                signature = self._signer.headers(
                    method=method, target=req.selector,
                    body=data if data is not None else b"")
            except SignerError as exc:
                return Result(ok=False, error="signer_unavailable",
                              data={"reason": exc.reason})
            except Exception:
                return Result(ok=False, error="signer_unavailable",
                              data={"reason": "signer_error"})
            for name, value in signature.items():
                req.add_header(name, value)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return self._finish(resp.status, resp.read(), error_data, resp.headers)
        except urllib.error.HTTPError as exc:  # 非 2xx：讀 body 再判（先於 URLError 接）
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return self._finish(exc.code, payload, error_data, exc.headers)
        except (urllib.error.URLError, OSError, HTTPException):
            return Result(ok=False, unreachable=True, error="unreachable")

    @staticmethod
    def _finish(status: int, payload: bytes, error_data: bool = False,
                headers=None) -> Result:
        if 300 <= status < 400:
            # 不跟隨 redirect：3xx 原樣回（C-705）；不讀 Location、不重送簽章。
            return Result(ok=False, status=status, error="redirect")
        try:
            text = payload.decode("utf-8") if payload.strip() else ""
            body = json.loads(text) if text else {}
        except (UnicodeDecodeError, ValueError):
            return Result(ok=False, status=status,
                          error=_STATUS_ERRORS.get(status, "bad_response"),
                          data=_status_data(status, headers, {}))
        if not isinstance(body, dict):
            return Result(ok=False, status=status,
                          error=_STATUS_ERRORS.get(status, "bad_response"),
                          data=_status_data(status, headers, {}))
        if 200 <= status < 300 and "error" not in body:
            return Result(ok=True, status=status, data=body)
        # 400／403／404／409：狀態碼與錯誤碼照實回（不重寫成拒絕理由）。
        # 認證類（401／403／429／503）沿用伺服器回的 error；body 沒有時用固定值。
        data = _status_data(status, headers, body)
        if error_data:
            # resolve 另留呼叫方要用的欄位（until、missing）；env 不放（錯誤回覆沒有）。
            data.update({
                name: value for name, value in body.items()
                if name not in ("error", "env")
            })
        error = body.get("error")
        if error is None:
            error = _STATUS_ERRORS.get(status, status)
        return Result(ok=False, status=status, data=data, error=str(error))

    # --- 公開 API（design 決定 6 的路徑） ---

    def list(self) -> Result:
        """列出 subscriptions（不含 key；依 priority 排序由服務負責）。"""
        return self._send("GET", "/v1/subscriptions", None)

    def usage(self) -> Result:
        """用量一覽：GET /v1/usage（每筆精簡欄位，不含 pipes；只讀快取，不觸發等結果）。"""
        return self._send("GET", "/v1/usage", None)

    def resolve(self, agent: str, provider: str, model: str | None = None,
                subscription: str | None = None,
                exclude: list[str] | None = None,
                data: str | None = None) -> Result:
        """resolve：依 provider＋model（可釘住／排除）取選中的 subscription 與 env。

        model／subscription／data 省略時不帶參數；exclude 由 client 用逗號合併。
        data 是資料等級宣告（public／general／real）：省略時不帶參數（服務不依
        privacy 過濾）；空字串也會被帶出，服務端視為沒給（HTTP 層的空白值規則），
        效果同省略。
        """
        params = [("agent", str(agent)), ("provider", str(provider))]
        if model is not None:
            params.append(("model", str(model)))
        if subscription is not None:
            params.append(("subscription", str(subscription)))
        if exclude:
            params.append(("exclude", ",".join(str(value) for value in exclude)))
        if data is not None:
            params.append(("data", str(data)))
        query = "&".join(
            "{0}={1}".format(name, urllib.parse.quote(value, safe=""))
            for name, value in params
        )
        return self._send("GET", "/v1/resolve?" + query, None, error_data=True)

    def roles(self, role: str, agent: str, data: str | None = None) -> Result:
        """取某角色在指定 agent 下的候選（依 rank；不含 key 與額度）。

        data 省略時不帶參數（服務取最嚴）；role 依服務的固定字串，不自行正規化。
        """
        params = [("agent", str(agent))]
        if data is not None:
            params.append(("data", str(data)))
        query = "&".join(
            "{0}={1}".format(name, urllib.parse.quote(value, safe=""))
            for name, value in params
        )
        path = "/v1/roles/{0}?{1}".format(
            urllib.parse.quote(str(role), safe=""), query
        )
        return self._send("GET", path, None, error_data=True)

    def cooldown(self, id: str, until: str, model: str | None = None) -> Result:
        """登錄冷卻；until 是何時能再用（RFC 3339 UTC 字串）；成功 204。

        model 選填（供應商自己的模型 id、不帶 agent 前綴）：有給時只冷卻那個模型，
        沒給即整把。
        """
        body: dict = {"until": until}
        if model is not None:
            body["model"] = model
        path = "/v1/subscriptions/{0}/cooldown".format(
            urllib.parse.quote(str(id), safe="")
        )
        return self._send("POST", path, body)
