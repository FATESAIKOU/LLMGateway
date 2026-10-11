"""額度訊號規則（design 決定 9；tasks 2.2）。由 test 實作。

classify(agent, text, now=None, tz=None) → Classification(kind, signal, resets_at)：
- kind：exhausted（額度用完）｜transient（暫時錯誤，自己重試）｜normal。
- 規則放 RULES（list of dict：agent、pattern、signal、kind、reset 解析法，可另帶
  require／exclude）；第一個命中的生效；順序即優先順序（額度標記一律在暫時錯誤之前）。
- resets_at 一律回 UTC 的 aware datetime；判讀不出就回 None（不斷言）。
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
