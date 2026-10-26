"""API 錯誤（design 決定 6；v5 的錯誤碼表見 design 決定 8 的取代）。HTTP 層把 ApiError 轉成狀態碼與 JSON。

訊息只用固定字串；不回顯請求裡的值（值永不進例外或 log）。

完整錯誤碼表（回覆 JSON 一律為 {"error": "<碼>", ...}）：
- 400 bad_request：JSON 解析失敗、body 不是物件、缺必填（保留 "missing": [...] 欄位）、
  登錄冷卻的 model 不是非空字串（null 除外——那是「沒給」）、超過 200 字元、
  以 agent 前綴開頭（opencode/、claude-code/）、含 fnmatch 萬用字元，或不被該把的
  models.allow 涵蓋、
  resolve 的輸入形狀錯（未知 agent、subscription＋exclude 同時給、subscription 與 provider
  不一致）、400 no_model：resolve 的 model 省略且沒有候選能推導模型
- 400 invalid_until：until 不是 RFC 3339 字串（或型別不對）
- 400 until_out_of_range：until 在過去或超過 8 天以後
- 403 agent_not_allowed：agent 不在該筆的 agents 裡（resolve 的選把步驟 3 同用）
- 403 model_not_allowed：resolve 的 model 不被任何候選的 models.allow 涵蓋
- 404 not_found：不存在的 id 或未知路徑（resolve 的未知 provider／未知 subscription 同用）
- 409 not_deployed：key 尚未部署
- 409 no_candidate：resolve 的 exclude 排除後沒有候選（沒有恢復時刻）
- 413 body_too_large：body 超過 1 MB
- 500 internal：伺服器內部非預期錯誤

沒有冷卻相關的錯誤碼：冷卻只是提示，不是閘門，resolve 不再因冷卻拒絕（design 決定 4）。
舊版 Gateway 仍可能在錯誤回覆裡帶它（body 帶 until）；消費端要保留對那個碼的判讀
（見 lgw/client.py 的 legacy 註記）。
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


# 403：resolve 的 model 不被任何候選的 models.allow 涵蓋（政策守門，不代選）
def model_not_allowed() -> ApiError:
    return ApiError(403, "model_not_allowed")


# 404：不存在的 id 或未知路徑
def not_found() -> ApiError:
    return ApiError(404, "not_found")


# 409：key 沒部署
def not_deployed() -> ApiError:
    return ApiError(409, "not_deployed")


# 400：resolve 的 model 省略且沒有候選能推導模型（不代選）
def no_model() -> ApiError:
    return ApiError(400, "no_model")


# 409：resolve 的 exclude 排除後沒有候選（沒有恢復時刻；不是冷卻）
def no_candidate() -> ApiError:
    return ApiError(409, "no_candidate")


# 413：body 超過 1 MB
def body_too_large() -> ApiError:
    return ApiError(413, "body_too_large")
