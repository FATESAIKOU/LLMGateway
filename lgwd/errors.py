"""API 錯誤（design 決定 6 的錯誤表）。HTTP 層把 ApiError 轉成狀態碼與 JSON。"""

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


# 401
def unauthorized() -> ApiError:
    return ApiError(401, "unauthorized")


# 403
def client_mismatch() -> ApiError:
    return ApiError(403, "client_mismatch")


def not_owner() -> ApiError:
    return ApiError(403, "not_owner")


def forbidden_role() -> ApiError:
    return ApiError(403, "forbidden_role")


def name_not_allowed() -> ApiError:
    return ApiError(403, "name_not_allowed")


# 404
def not_found() -> ApiError:
    return ApiError(404, "not_found")


# 409（生命週期）
def lease_not_active() -> ApiError:
    return ApiError(409, "lease_not_active")


def lease_not_confirmed() -> ApiError:
    return ApiError(409, "lease_not_confirmed")


# 409（拒絕）：reason ∈ not_allowed | instance_busy | cap_reached | all_cooling | below_floor
REFUSAL_REASONS = ("not_allowed", "instance_busy", "cap_reached", "all_cooling", "below_floor")


def refusal(reason: str, earliest_reset: str | None = None) -> ApiError:
    assert reason in REFUSAL_REASONS, reason
    extra = {"reason": reason}
    if reason == "all_cooling" and earliest_reset:
        extra["earliest_reset"] = earliest_reset
    return ApiError(409, "refused", **extra)
