"""額度訊號規則（design 決定 9；tasks 2.2）。由 test 實作。

classify(agent, text, now=None, tz=None) → Classification(kind, signal, resets_at)：
- kind：exhausted（額度用完）｜transient（暫時錯誤，自己重試）｜normal。
- 規則放 RULES（list of dict：agent、pattern、signal、kind、reset 解析法，可另帶
  require／exclude）；第一個命中的生效；順序即優先順序（額度標記一律在暫時錯誤之前）。
- resets_at 一律回 UTC 的 aware datetime；判讀不出就回 None（不斷言）。
- 登錄冷卻的 body 是 {"until": RFC 3339 UTC}（v5）；Signal.to_cooldown_body() 只負責轉換，
  resets_at 判讀不出（None）時由呼叫方以「現在＋1 小時」登錄。
- 注意：Claude Code 的 resets 時刻是 agent 所在機器的本地時間，呼叫方（sidecar）
  要傳那台機器的 tz；沒傳時用本機時區（在本機跑 sidecar 時兩者相同）。
- 只用 re、datetime；不引用 subprocess、herdr、agora，不做平台判斷。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Classification:
    kind: str  # exhausted | transient | normal
    signal: str | None = None  # design 決定 6 的 signal 列舉
    resets_at: datetime | None = None  # UTC aware；判讀不出為 None


@dataclass(frozen=True)
class Signal:
    """結構化額度事件的判讀結果。

    欄位設計直接對應 LLMGateway 的 POST /v1/subscriptions/{id}/cooldown {until}：
    - exhausted: 是否額度用完。
    - resets_at: UTC aware 的 datetime，或 None（判讀不出或無重置時間）。
      判讀不出時由呼叫方（例如 sidecar）以呼叫當下＋1 小時為 until 登錄。
    - source: 事件來源識別字串（如 "opencode"、"claude-code"）。
    """

    exhausted: bool
    resets_at: datetime | None  # UTC aware
    source: str

    def to_cooldown_body(self) -> dict[str, str]:
        """產生 POST /v1/subscriptions/{id}/cooldown 的 body 字典 {"until": RFC 3339 UTC}。

        resets_at 為 None（判讀不出恢復時間）時丟 ValueError；約定由呼叫方
        改用「現在＋1 小時」登錄（spec subscription-registry）。
        """
        if self.resets_at is None:
            raise ValueError("resets_at is None; caller must use now + 1 hour")
        body: dict[str, str] = {
            "until": self.resets_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        return body


def _to_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def _parse_claude(match: re.Match, now: datetime) -> datetime | None:
    """resets 3:45pm（今天，過了算隔天）／resets Mon 12:00am（下一次該時刻）。"""
    day, hour, minute, ampm = match.group(1, 2, 3, 4)
    hour, minute = int(hour), int(minute)
    if minute > 59 or hour < 1 or hour > 12:
        return None
    if ampm.lower() == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    if day is None:
        cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now:  # 過了當天就算隔天
            cand += timedelta(days=1)
        return _to_utc(cand)
    target = _WEEKDAYS[day.lower()]
    ahead = (target - now.weekday()) % 7
    cand = (now + timedelta(days=ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if cand <= now:
        cand += timedelta(days=7)
    return _to_utc(cand)


def _parse_retrying_in(match: re.Match, now: datetime) -> datetime | None:
    """retrying in 15h 30m → now＋該長度；抓不到數字回 None。"""
    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    if not hours and not minutes:
        return None
    return _to_utc(now + timedelta(hours=hours, minutes=minutes))


_DURATION = re.compile(r"(\d+)\s*(days?|hours?|minutes?|seconds?)", re.IGNORECASE)


def _parse_reset_in(match: re.Match, now: datetime) -> datetime | None:
    """It will reset in 5 hours 23 minutes → now＋該長度；less than a minute 算 60 秒。"""
    total = 0
    mults = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}
    for amount, unit in _DURATION.findall(match.group(1)):
        total += int(amount) * mults[unit.lower().rstrip("s")]
    if total:
        return _to_utc(now + timedelta(seconds=total))
    if re.search(r"less than a minute", match.group(1), re.IGNORECASE):
        return _to_utc(now + timedelta(seconds=60))
    return None


_RESET_PARSERS = {"claude": _parse_claude, "retrying_in": _parse_retrying_in,
                  "reset_in": _parse_reset_in}

# 順序即優先順序：額度標記先，暫時錯誤後；訊息標記優先於狀態碼（design 決定 9）。
RULES = [
    # Claude Code：額度用完（只認 claude-code 的畫面）
    {"agent": "claude-code",
     "pattern": re.compile(
         r"You've hit your (?:session|weekly|Opus|Sonnet) limit\s*(?:·|-|:)?\s*"
         r"resets\s+(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?(\d{1,2}):(\d{2})\s*(am|pm)"),
     "signal": "claude_limit", "kind": "exhausted", "reset": "claude"},
    # Claude spend limit 類：換把但不帶重置時刻（用政策預設冷卻）
    {"agent": "claude-code",
     "pattern": re.compile(
         r"spend limit|individual usage limit|shared budget", re.IGNORECASE),
     "signal": "claude_limit", "kind": "exhausted", "reset": None},
    # opencode＋Zen 免費額度
    {"agent": "opencode",
     "pattern": re.compile(r"FreeUsageLimitError|Free usage exceeded", re.IGNORECASE),
     "signal": "zen_free_limit", "kind": "exhausted", "reset": "retrying_in",
     "reset_pattern": re.compile(
         r"retrying in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.IGNORECASE)},
    # opencode＋Zen Go 訂閱額度
    {"agent": "opencode",
     "pattern": re.compile(r"GoUsageLimitError|usage limit reached", re.IGNORECASE),
     "signal": "zen_go_limit", "kind": "exhausted", "reset": "reset_in",
     "reset_pattern": re.compile(
         r"[Ii]t will reset in\s+(.+?)(?:\.|\s+-|\s*$)", re.IGNORECASE)},
    # opencode＋Zen 餘額不足
    {"agent": "opencode",
     "pattern": re.compile(r"CreditLimitExceeded"),
     "signal": "credit_limit", "kind": "exhausted", "reset": None},
    # OpenRouter：in-flight 預算的 402 是暫時錯誤（先判）
    {"agent": None,
     "pattern": re.compile(r"in_flight_budget"),
     "signal": None, "kind": "transient", "reset": None},
    # OpenRouter：其他 402（要帶 openrouter 或 limit_source 上下文才算）
    {"agent": None,
     "pattern": re.compile(r"\b402\b"),
     "require": re.compile(r"openrouter|limit_source", re.IGNORECASE),
     "exclude": re.compile(r"in_flight_budget"),
     "signal": "openrouter_limit", "kind": "exhausted", "reset": None},
    # Ollama Cloud：額度類 429（訊息標記優先，不要求數字）
    {"agent": None,
     "pattern": re.compile(r"(session|weekly)\s+usage\s+limit", re.IGNORECASE),
     "signal": "ollama_limit", "kind": "exhausted", "reset": None},
    # 其他 402／429、rate limit、overloaded：一律暫時錯誤
    {"agent": None,
     "pattern": re.compile(r"\b402\b|\b429\b|rate[-_ ]?limit|overloaded",
                           re.IGNORECASE),
     "signal": None, "kind": "transient", "reset": None},
]


def _local_tz():
    return datetime.now().astimezone().tzinfo


def classify(agent: str, text: str | None, now: datetime | None = None,
             tz=None) -> Classification:
    """判讀畫面文字。now 預設現在；tz 預設本機時區；resets_at 回 UTC。"""
    text = text or ""
    tzinfo = tz if tz is not None else _local_tz()
    if now is None:
        now = datetime.now(tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tzinfo)
    for rule in RULES:
        if rule["agent"] is not None and rule["agent"] != agent:
            continue
        match = rule["pattern"].search(text)
        if not match:
            continue
        require = rule.get("require")
        if require is not None and not require.search(text):
            continue
        exclude = rule.get("exclude")
        if exclude is not None and exclude.search(text):
            continue
        resets_at = None
        parser = _RESET_PARSERS.get(rule.get("reset"))
        if parser is not None:
            sub = rule.get("reset_pattern")
            target = sub.search(text) if sub is not None else match
            if target is not None:
                resets_at = parser(target, now)
        return Classification(kind=rule["kind"], signal=rule["signal"],
                              resets_at=resets_at)
    return Classification(kind="normal")


OPENCODE_QUOTA_REASONS: frozenset[str] = frozenset({"free_tier_limit"})


def from_opencode_status(obj: dict) -> Signal | None:
    """判讀 opencode server 的 session.status 事件或 status 物件。

    支援傳入整包 session.status 事件：
      {"type": "session.status", "properties": {"status": {...}}}
    或直接傳入 status 層：
      {"type": "retry", ...}

    規則：
    - status.type == "retry" 且 status.action.reason 為額度類（OPENCODE_QUOTA_REASONS）：
      額度用完，回傳 Signal(exhausted=True, resets_at=..., source="opencode")。
      status.next 是 epoch 毫秒，轉成 UTC datetime。
    - status.type 為 "busy"、"idle" 或其他 retry 原因：回傳 None。
    - 欄位缺漏、型別不對、非 dict：回傳 None，不丟例外。
    """
    if not isinstance(obj, dict):
        return None

    if obj.get("type") == "session.status":
        props = obj.get("properties")
        if not isinstance(props, dict):
            return None
        status = props.get("status")
    else:
        status = obj

    if not isinstance(status, dict):
        return None

    stype = status.get("type")
    if stype != "retry":
        return None

    action = status.get("action")
    if not isinstance(action, dict):
        return None

    reason = action.get("reason")
    if not isinstance(reason, str) or reason not in OPENCODE_QUOTA_REASONS:
        return None

    next_val = status.get("next")
    resets_at: datetime | None = None
    if next_val is not None:
        if not isinstance(next_val, (int, float)) or isinstance(next_val, bool):
            return None
        try:
            resets_at = datetime.fromtimestamp(next_val / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    return Signal(
        exhausted=True,
        resets_at=resets_at,
        source="opencode",
    )


def from_claude_rate_limit_event(obj: dict) -> Signal | None:
    """判讀 Claude Code stream-json 中的 rate_limit_event 事件或 rate_limit_info 物件。

    支援傳入整包 rate_limit_event 事件：
      {"type": "rate_limit_event", "rate_limit_info": {...}}
    或直接傳入 rate_limit_info 層：
      {"status": ..., "rateLimitType": ..., ...}

    規則：
    - rate_limit_info.status != "allowed"：額度用完，回傳 Signal(exhausted=True, resets_at=..., source="claude-code")。
    - rate_limit_info.status == "allowed"：回傳 None。
    - resetsAt 是 epoch 秒，轉成 UTC datetime。
    - 欄位缺漏、型別不對、非 dict：回傳 None，不丟例外。
    """
    if not isinstance(obj, dict):
        return None

    if obj.get("type") == "rate_limit_event":
        info = obj.get("rate_limit_info")
    elif "type" not in obj and "status" in obj:
        info = obj
    else:
        return None

    if not isinstance(info, dict):
        return None

    status = info.get("status")
    if not isinstance(status, str):
        return None

    if status == "allowed":
        return None

    resets_at_raw = info.get("resetsAt")
    resets_at: datetime | None = None
    if resets_at_raw is not None:
        if not isinstance(resets_at_raw, (int, float)) or isinstance(resets_at_raw, bool):
            return None
        try:
            resets_at = datetime.fromtimestamp(resets_at_raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    return Signal(
        exhausted=True,
        resets_at=resets_at,
        source="claude-code",
    )
