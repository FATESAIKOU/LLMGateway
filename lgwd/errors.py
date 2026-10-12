"""API 錯誤（design 決定 6；v5 的錯誤碼表見 design 決定 8 的取代）。HTTP 層把 ApiError 轉成狀態碼與 JSON。

訊息只用固定字串；不回顯請求裡的值（值永不進例外或 log）。

完整錯誤碼表（回覆 JSON 一律為 {"error": "<碼>", ...}）：
- 400 bad_request：JSON 解析失敗、body 不是物件、缺必填（保留 "missing": [...] 欄位）
- 400 invalid_until：until 不是 RFC 3339 字串（或型別不對）
- 400 until_out_of_range：until 在過去或超過 8 天以後
- 403 agent_not_allowed：agent 不在該筆的 agents 裡
- 404 not_found：不存在的 id 或未知路徑
- 409 not_deployed：key 尚未部署
- 413 body_too_large：body 超過 1 MB
- 500 internal：伺服器內部非預期錯誤
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


# 400：JSON 解析失敗、body 不是物件、缺必填（保留 missing 欄位，不含 detail 自由文字）
def bad_request(missing: list[str] | None = None) -> ApiError:
    extra = {}
    if missing:
        extra["missing"] = sorted(missing)
    return ApiError(400, "bad_request", **extra)


# 400：until 不是 RFC 3339 字串（或型別不對）
def invalid_until() -> ApiError:
    return ApiError(400, "invalid_until")


# 400：until 在過去或超過 8 天以後
def until_out_of_range() -> ApiError:
    return ApiError(400, "until_out_of_range")


# 403：agent 不在該筆的 agents 裡
def agent_not_allowed() -> ApiError:
    return ApiError(403, "agent_not_allowed")


# 404：不存在的 id 或未知路徑
def not_found() -> ApiError:
    return ApiError(404, "not_found")


# 409：key 沒部署
def not_deployed() -> ApiError:
    return ApiError(409, "not_deployed")


# 413：body 超過 1 MB
def body_too_large() -> ApiError:
    return ApiError(413, "body_too_large")
