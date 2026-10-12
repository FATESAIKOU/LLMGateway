"""可替換的時鐘（design 決定 12）。服務裡取「現在」一律經過 Clock。"""

from __future__ import annotations

import datetime as _dt
import threading


UTC = _dt.timezone.utc


class Clock:
    def now(self) -> _dt.datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> _dt.datetime:
        return _dt.datetime.now(tz=UTC)


class FakeClock(Clock):
    """測試用。advance() 推進時間；不會自己走。"""

    def __init__(self, start: _dt.datetime | None = None):
        self._now = start or _dt.datetime(2026, 10, 11, 0, 0, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def now(self) -> _dt.datetime:
        with self._lock:
            return self._now

    def advance(self, **kwargs) -> _dt.datetime:
        with self._lock:
            self._now = self._now + _dt.timedelta(**kwargs)
            return self._now

    def set(self, when: _dt.datetime) -> None:
        with self._lock:
            self._now = when


def to_rfc3339(when: _dt.datetime) -> str:
    """UTC、秒精度、結尾 Z。"""
    return when.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_rfc3339(text: str) -> _dt.datetime:
    """接受 ...Z 或帶時差的 RFC 3339；回傳 UTC。格式錯誤丟 ValueError。"""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = _dt.datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)
