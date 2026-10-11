"""租用 API 客戶端（design 決定 9；tasks 2.1）。由 test 實作。

LeaseClient(base_url, token_path, timeout=10)：
- token 從 token_path 讀（去掉結尾換行），不印出、不進 repr。
- request_lease(worker, role, agent, instance, model=None, subscription=None, purpose=None, preferences=None)
  start(lease_id)、exhausted(lease_id, signal, resets_at=None)、renew(lease_id)、release(lease_id, reason=None)、
  report_usage(lease_id, snapshot)、board()、whoami()
- 每個改變狀態的呼叫自動帶新的 request_id（uuid4）；呼叫方要重送時可傳 request_id 參數。
- 回傳 Result：ok(bool)、status(int|None)、data(dict)、refusal(reason, earliest_reset)|None、error(str|None)、
  unreachable(bool)。連不上或逾時 → unreachable=True、ok=False，不丟例外。不認得的拒絕理由照樣放在 refusal。
- 只用 urllib；不引用 subprocess、herdr、agora。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from uuid import uuid4


@dataclass
class Refusal:
    """拒絕：reason 照服務回的放，不認得的也不擋（design 決定 12）。"""

    reason: str
    earliest_reset: str | None = None


@dataclass
class Result:
    ok: bool
    status: int | None = None
    data: dict = field(default_factory=dict)
    refusal: Refusal | None = None
    error: str | None = None
    unreachable: bool = False


def _new_request_id() -> str:
    return uuid4().hex


class LeaseClient:
    """只用 urllib 打租用 API；網路問題一律回 unreachable，不丟例外。"""

    def __init__(self, base_url: str, token_path: str, timeout: float = 10):
        self._base_url = base_url.rstrip("/")
        self._token_path = str(token_path)
        self._timeout = timeout
        raw = Path(token_path).read_text(encoding="utf-8")
        token = raw.split("\n")[0].rstrip("\r")  # 只去結尾換行
        if not token:
            raise ValueError("empty token")
        self._token = token

    def __repr__(self) -> str:  # token 絕不進 repr
        return (
            "LeaseClient(base_url={0!r}, token_path={1!r}, timeout={2!r})".format(
                self._base_url, self._token_path, self._timeout
            )
        )

    # --- 內部 ---

    def _send(self, method: str, path: str, body: dict | None) -> Result:
        url = self._base_url + path
        headers = {"Authorization": "Bearer " + self._token}
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
            return Result(ok=False, status=status, error="bad response")
        if not isinstance(body, dict):
            return Result(ok=False, status=status, error="bad response")
        body.pop("api_version", None)  # 傳輸欄位，不進 data
        ref = body.get("refusal")  # exhausted 的形狀：{next: null, refusal: {...}}
        if isinstance(ref, dict) and isinstance(ref.get("reason"), str):
            return Result(
                ok=False,
                status=status,
                refusal=Refusal(
                    reason=ref["reason"], earliest_reset=ref.get("earliest_reset")
                ),
            )
        if body.get("error") == "refused" and isinstance(body.get("reason"), str):
            return Result(
                ok=False,
                status=status,
                refusal=Refusal(
                    reason=body["reason"], earliest_reset=body.get("earliest_reset")
                ),
            )
        if 200 <= status < 300 and "error" not in body:
            return Result(ok=True, status=status, data=body)
        return Result(ok=False, status=status, error=str(body.get("error", status)))

    # --- 公開 API（design 決定 6 的路徑；request_id 依表只放在會改變狀態的 POST） ---

    def request_lease(
        self,
        worker: str,
        role: str,
        agent: str,
        instance: str,
        model=None,
        subscription=None,
        purpose=None,
        preferences=None,
        request_id: str | None = None,
    ) -> Result:
        body: dict = {
            "worker": worker,
            "role": role,
            "agent": agent,
            "instance": instance,
            "request_id": request_id or _new_request_id(),
        }
        for key, value in (
            ("model", model),
            ("subscription", subscription),
            ("purpose", purpose),
            ("preferences", preferences),
        ):
            if value is not None:  # 沒給的欄位不送，避免服務把 null 當值
                body[key] = value
        return self._send("POST", "/v1/leases", body)

    def start(self, lease_id: str, request_id: str | None = None) -> Result:
        return self._send(
            "POST",
            "/v1/leases/" + urllib.parse.quote(lease_id, safe="") + "/start",
            {"request_id": request_id or _new_request_id()},
        )

    def exhausted(
        self,
        lease_id: str,
        signal: str,
        resets_at: str | None = None,
        request_id: str | None = None,
    ) -> Result:
        body: dict = {"signal": signal, "request_id": request_id or _new_request_id()}
        if resets_at is not None:
            body["resets_at"] = resets_at
        return self._send(
            "POST",
            "/v1/leases/" + urllib.parse.quote(lease_id, safe="") + "/exhausted",
            body,
        )

    def renew(self, lease_id: str, request_id: str | None = None) -> Result:
        return self._send(
            "POST",
            "/v1/leases/" + urllib.parse.quote(lease_id, safe="") + "/renew",
            {"request_id": request_id or _new_request_id()},
        )

    def release(
        self,
        lease_id: str,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> Result:
        body: dict = {"request_id": request_id or _new_request_id()}
        if reason is not None:
            body["reason"] = reason
        return self._send(
            "POST",
            "/v1/leases/" + urllib.parse.quote(lease_id, safe="") + "/release",
            body,
        )

    def report_usage(self, lease_id: str, snapshot: dict) -> Result:
        # design 決定 6 的表：usage 只帶 snapshot，不帶 request_id
        return self._send(
            "POST",
            "/v1/leases/" + urllib.parse.quote(lease_id, safe="") + "/usage",
            {"snapshot": snapshot},
        )

    def board(self) -> Result:
        return self._send("GET", "/v1/board", None)

    def whoami(self) -> Result:
        """只驗憑證，不碰租約；成功 data 為 {"client": 名稱}。"""
        return self._send("GET", "/v1/whoami", None)
