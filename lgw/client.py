"""五個呼叫的客戶端（design 決定 6；tasks 2.1、2.3）：list、resolve、get（過渡期）、登錄冷卻、roles。

用法：

    client = lgw.client.Client("http://127.0.0.1:4710")  # base URL 由呼叫方給
    client.list()
    client.resolve("opencode", "opencode-zen", model="big-pickle")
    client.resolve("claude-code", "anthropic", subscription="claude-personal")
    client.roles("impl", "opencode", data="public")
    client.get("claude-personal", "claude-code")
    client.cooldown("claude-personal", "2026-10-12T03:00:00Z")
    client.cooldown("opencode-zen", "2026-10-12T03:00:00Z", model="big-pickle")

- list() → GET /v1/subscriptions
- resolve(agent, provider, model, subscription, exclude) → GET /v1/resolve
  （model／subscription 是供應商 id 與釘住的 id；exclude 由 client 用 ",".join
  合併；每個 query 值都用 urllib.parse.quote(value, safe="") 編碼——`+` 因此
  編成 %2B，server 的 parse_qs 才不會解成空白）
- roles(role, agent, data=None) → GET /v1/roles/{role}?agent={agent}[&data={data}]
  （#100；spec role-allocation。data 省略時不帶參數，由服務取最嚴）
- get(id, agent) → GET /v1/subscriptions/{id}?agent={agent}（過渡期；resolve 的
  並存夥伴，移除在 d 單）
- cooldown(id, until, model=None) → POST /v1/subscriptions/{id}/cooldown，body
  {"until": RFC 3339 UTC}（有給 model 時多一個 "model"）；成功 204。model 是供應商
  自己的模型 id（不帶 agent 前綴）；沒有 model 即整把冷卻。
- 沒有應用層認證（design 決定 5a），所以不帶任何認證標頭。
- 回 Result：ok、status、data、error、unreachable。連不上或逾時回 unreachable=True、
  不丟例外；400／403／404／409 照實回狀態碼與 error。error 可能是：
  - 服務回的碼：bad_request｜invalid_until｜until_out_of_range｜
    agent_not_allowed｜model_not_allowed｜no_model｜not_found｜not_deployed｜
    no_candidate｜body_too_large；
  - cooling：**只有舊版 Gateway 會回**（legacy；新 Gateway 不再因冷卻拒絕，200 的
    meta 帶 cooldown／model_cooldowns 當提示）。本 client 對它照實回狀態碼與 until，
    呼叫方要保留判讀；
  - resolve 的非 2xx 回覆把 body 裡 error 以外的欄位（until、missing）放進
    Result.data（錯誤回覆沒有 env；過渡期 get 的錯誤不進 data，與舊行為同款）；
  - 客戶端判讀的：unreachable（連不上或逾時）｜bad_response（回覆不是 JSON 物件）。
- Result 的 repr 不印 data（get 的 key、resolve 的 env 都有憑證值）；不要 log／print 整個 Result。
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
    """只用 urllib 打四個呼叫（list／resolve／get／登錄冷卻）；網路問題一律回 unreachable，不丟例外。

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

    def _send(self, method: str, path: str, body: dict | None,
              error_data: bool = False) -> Result:
        url = self._base_url + path
        headers: dict[str, str] = {}  # 無應用層認證，不帶任何認證標頭
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return self._finish(resp.status, resp.read(), error_data)
        except urllib.error.HTTPError as exc:  # 非 2xx：讀 body 再判（先於 URLError 接）
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return self._finish(exc.code, payload, error_data)
        except (urllib.error.URLError, OSError, HTTPException):
            return Result(ok=False, unreachable=True, error="unreachable")

    @staticmethod
    def _finish(status: int, payload: bytes, error_data: bool = False) -> Result:
        try:
            text = payload.decode("utf-8") if payload.strip() else ""
            body = json.loads(text) if text else {}
        except (UnicodeDecodeError, ValueError):
            return Result(ok=False, status=status, error="bad_response")
        if not isinstance(body, dict):
            return Result(ok=False, status=status, error="bad_response")
        if 200 <= status < 300 and "error" not in body:
            return Result(ok=True, status=status, data=body)
        # 400／403／404／409：狀態碼與錯誤碼照實回（不重寫成拒絕理由）。
        # resolve 另留呼叫方要用的欄位（until、missing）；env 不放（錯誤回覆沒有）。
        data = {}
        if error_data:
            data = {
                name: value for name, value in body.items()
                if name not in ("error", "env")
            }
        return Result(ok=False, status=status, data=data,
                      error=str(body.get("error", status)))

    # --- 公開 API（design 決定 6 的路徑） ---

    def list(self) -> Result:
        """列出 subscriptions（不含 key；依 priority 排序由服務負責）。"""
        return self._send("GET", "/v1/subscriptions", None)

    def resolve(self, agent: str, provider: str, model: str | None = None,
                subscription: str | None = None,
                exclude: list[str] | None = None) -> Result:
        """resolve：依 provider＋model（可釘住／排除）取選中的 subscription 與 env。

        model／subscription 省略時不帶參數；exclude 由 client 用逗號合併。
        """
        params = [("agent", str(agent)), ("provider", str(provider))]
        if model is not None:
            params.append(("model", str(model)))
        if subscription is not None:
            params.append(("subscription", str(subscription)))
        if exclude:
            params.append(("exclude", ",".join(str(value) for value in exclude)))
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

    def get(self, id: str, agent: str) -> Result:
        """取一把 key；agent 不符 403、未部署 409、不存在 404。"""
        path = "/v1/subscriptions/{0}?agent={1}".format(
            urllib.parse.quote(str(id), safe=""),
            urllib.parse.quote(str(agent), safe=""),
        )
        return self._send("GET", path, None)

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
