"""防重放庫忙碌（StoreBusy）的計數、取樣與狀態檔（#312；契約 C-1004～C-1006、C-1009）。

- `BusyCounter`：請求路徑上的計數器。`hit()` 只取自己的葉鎖，不做 I/O、不讀時鐘、不記日誌；
  計數不放進 `AuthLog`，因為 AuthLog 持鎖時會呼叫 sink。
- `BusyWindow`：背景執行緒呼叫 `sample()`，算出 30／60 分鐘內的次數。窗口用「樣本」拼出來，
  不記每一次事件的時間。
- 狀態檔 `.auth-busy.status`：一行 JSON，寫端 `write_status`（同目錄暫存、0644、`os.replace`，
  不 fsync），讀端 `read_status`／`parse_status` 嚴格解析。檔案不含任何請求資料。

不 import `lgwd.auth`、`lgwd.http`、`lgwd.main`，不碰 SQLite；門檻判斷在 lgwctl。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

BUSY_STATUS_SCHEMA = 1
BUSY_STATUS_MAX_BYTES = 4096
BUSY_STATUS_MODE = 0o644
BUSY_STATUS_MODES = ("warn", "enforce")
BUSY_STATUS_KEYS = frozenset({"schema", "mode", "since", "updated",
                              "store_busy_total", "store_busy_30m", "store_busy_60m"})

#: 窗口（秒）：30 分鐘（fail 用）與 60 分鐘（todo 用）。
WINDOW_30M = 1800
WINDOW_60M = 3600


class BusyCounter:
    """StoreBusy 的累計次數（葉鎖：持有時不取任何其他鎖、不做 I/O）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0

    def hit(self) -> None:
        with self._lock:
            self._total += 1

    def total(self) -> int:
        with self._lock:
            return self._total


class BusyWindow:
    """背景取樣：每次 `sample()` 回 `(total, last_30m, last_60m)`。

    規則（C-1006）：建構時放入 `(m0, 0)`；每次取樣先修剪、再對每個窗口找「時間 ≤ now−W」
    的最新樣本當基準（沒有就用第一筆），窗口次數＝目前 total − 基準 total，最後才加入
    `(now, total)`。修剪時永遠留一筆基準樣本，所以時鐘跳躍不會退回減 0。只在背景執行緒呼叫。
    """

    def __init__(self, counter: BusyCounter, *, monotonic=time.monotonic) -> None:
        self._counter = counter
        self._monotonic = monotonic
        self._samples: list[tuple[float, int]] = [(float(monotonic()), 0)]

    def sample(self) -> tuple[int, int, int]:
        now = float(self._monotonic())
        total = self._counter.total()
        samples = self._samples
        # 修剪：只要第二筆已經在「now−3600」之前，第一筆就沒用了；結束時第一筆是
        # 最新的一筆基準樣本（或啟動樣本）。
        while len(samples) >= 2 and samples[1][0] <= now - WINDOW_60M:
            samples.pop(0)
        last_30m = total - self._base(now - WINDOW_30M)
        last_60m = total - self._base(now - WINDOW_60M)
        samples.append((now, total))
        return total, last_30m, last_60m

    def _base(self, cut: float) -> int:
        base = self._samples[0][1]
        for when, value in self._samples:
            if when <= cut:
                base = value
        return base


class BusyStatusMissing(Exception):
    """狀態檔不存在。"""


class BusyStatusBad(Exception):
    """狀態檔讀不到或讀不懂；`reason` 是固定分類字串（`unreadable`／`too_large`／`format`）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def status_bytes(document: dict) -> bytes:
    """一行 JSON＋LF（UTF-8）。"""
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_status(data: bytes) -> dict:
    """嚴格解析；任何不合的情況丟 `BusyStatusBad("format")`。"""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BusyStatusBad("format") from None
    if not isinstance(document, dict) or set(document) != BUSY_STATUS_KEYS:
        raise BusyStatusBad("format")
    schema = document["schema"]
    if isinstance(schema, bool) or schema != BUSY_STATUS_SCHEMA:
        raise BusyStatusBad("format")
    if document["mode"] not in BUSY_STATUS_MODES:
        raise BusyStatusBad("format")
    for key in ("since", "updated", "store_busy_total", "store_busy_30m", "store_busy_60m"):
        if not _is_count(document[key]):
            raise BusyStatusBad("format")
    return document


def read_status(path: str) -> dict:
    """讀狀態檔；只丟 `BusyStatusMissing` 與 `BusyStatusBad`。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise BusyStatusMissing() from None
    except OSError:
        raise BusyStatusBad("unreadable") from None
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > BUSY_STATUS_MAX_BYTES:
                raise BusyStatusBad("too_large")
            chunks.append(chunk)
    except OSError:
        raise BusyStatusBad("unreadable") from None
    finally:
        os.close(fd)
    return parse_status(b"".join(chunks))


def write_status(path: str, document: dict) -> None:
    """同目錄暫存 → chmod 0644 → `os.replace`；不 fsync。失敗丟 `OSError`，不留暫存檔。"""
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
        with os.fdopen(fd, "wb") as fh:
            fh.write(status_bytes(document))
        os.chmod(tmp, BUSY_STATUS_MODE)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


__all__ = [
    "BUSY_STATUS_KEYS",
    "BUSY_STATUS_MAX_BYTES",
    "BUSY_STATUS_MODE",
    "BUSY_STATUS_MODES",
    "BUSY_STATUS_SCHEMA",
    "BusyCounter",
    "BusyStatusBad",
    "BusyStatusMissing",
    "BusyWindow",
    "WINDOW_30M",
    "WINDOW_60M",
    "parse_status",
    "read_status",
    "status_bytes",
    "write_status",
]
