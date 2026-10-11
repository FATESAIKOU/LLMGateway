"""API 錯誤（design 決定 6 的錯誤表）。HTTP 層把 ApiError 轉成狀態碼與 JSON。

訊息只用固定字串；不回顯請求裡的值（值永不進例外或 log）。
"""

from __future__ import annotations


class ApiError(Exception):
    """status：HTTP 狀態碼；code：錯誤碼字串；extra：附在回覆 JSON 的欄位。"""

    def __init__(self, status: int, code: str, **extra):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, **self.extra}


# 400
def bad_request(missing: list[str] | None = None, detail: str | None = None) -> ApiError:
    extra = {}
    if missing:
        extra["missing"] = sorted(missing)
    if detail:
        extra["detail"] = detail
    return ApiError(400, "bad_request", **extra)


# 403：agent 不在該筆的 agents 裡
def agent_not_allowed() -> ApiError:
    return ApiError(403, "agent_not_allowed")


# 404
def not_found() -> ApiError:
    return ApiError(404, "not_found")


# 409：key 沒部署
def not_deployed() -> ApiError:
    return ApiError(409, "not_deployed")
