"""用量測試共用的假物件（#60）：假 fetcher、窗口建構、固定基準時間。"""

import datetime as _dt
import threading

from lgwd.usage import FetchResult

UTC = _dt.timezone.utc
BASE = _dt.datetime(2026, 10, 25, 3, 0, 0, tzinfo=UTC)


def at(base=BASE, **kw):
    return base + _dt.timedelta(**kw)


def windows(session=(75.0, None), weekly=(40.0, None), base=BASE):
    out = {}
    for name, spec in (("session", session), ("weekly", weekly)):
        out[name] = None if spec is None else (spec[0], spec[1] or at(base, days=3))
    return out


class FakeFetcher:
    """可控制成功／失敗／卡住／例外的假取用量實作；不連網、不起子行程。"""

    source = "fake-source"

    def __init__(self, ttl=120, min_ttl=60, enabled=True, base=BASE):
        self.default_ttl, self.min_ttl, self.default_enabled = ttl, min_ttl, enabled
        self.calls = 0
        self.outcome = FetchResult(windows=windows(base=base))
        self.gate = None          # threading.Event：設了就卡到 gate.set()
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def fetch(self, key, guard):
        with self._lock:
            self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def refuse_http(url, headers, timeout):
    """拒絕連線的 HTTP 層：沒有要驗取用量、只想確保不連外的測試傳給 UsageDeps(http_get=…)。"""
    raise ConnectionRefusedError("refused")
