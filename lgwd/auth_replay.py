"""防重放庫（D2 PR-3，契約 C-305～C-308；design「防重放交易」「容量、配額與多實例」）。

單寫者：`accept` 與清除共用同一個 `AuthDb`（一條連線、一把行程內鎖），交易一律
`BEGIN IMMEDIATE`，所以「驗重放」與「清除」互相序列化：

- 重放若排在清除之前，原 nonce 還在，唯一約束擋下；
- 重放若排在清除之後，`H[key]`（每 key 清除水位）已 ≥ 該簽章的 ts，水位擋下。

交易內只讀一次 `now`（經注入的 Clock），時窗與水位都用這個值，所以請求在鎖上等多久
都不影響結論。非 ACCEPTED 一律 rollback、不寫入。
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import threading
import time

from lgwd.auth_db import DEFAULT_BUSY_TIMEOUT, AuthDb, open_rw
from lgwd.auth_paths import now_seconds
from lgwd.clock import Clock

WINDOW_SECONDS = 60        # |now − ts| ≤ 60
PURGE_SECONDS = 65         # 清除門檻：ts < now − 65；配額也只計 ts ≥ now − 65
PURGE_BATCH = 5000         # 每批最多刪幾列，保持交易短
PURGE_INTERVAL = 30.0      # 背景清除每 30 秒一輪
DEFAULT_PER_KEY_MAX = 600  # 每 key 配額（每秒約 9 筆的持續量）
RETRY_AFTER_OFFSET = PURGE_SECONDS + 1  # 計入配額的最小 ts 何時離開範圍：+66
MAX_RETRY_AFTER = 126      # ts 可落在未來至多 60 秒，所以 Retry-After 上限 126

# 三個結果類別（C-305；與 C-603 的 200／401／429 一一對應，PR-6 只依 outcome 分類）。
ACCEPTED = "accepted"
DENIED = "denied"
RATE_LIMITED = "rate_limited"

REASON_EXPIRED = "expired"
REASON_WATER = "water"
REASON_REPLAY = "replay"

_LOG = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Accept:
    """`accept` 的結果；欄位只在對應的 `outcome` 有值（不變式見下）。

    - `ACCEPTED`：`reason` 空、`retry_after` 0。
    - `DENIED`：`reason` ∈ {`expired`, `water`, `replay`}、`retry_after` 0。
    - `RATE_LIMITED`：`reason` 空、`retry_after` > 0（秒）。

    `reason` 是 C-607 日誌白名單的一部分；PR-6 的 `apply` 依 `outcome` 分 401／429，
    不要靠比對 `reason` 字串（否則 429 會被誤判成 401、`Retry-After` 掉）。
    """

    outcome: str
    reason: str = ""
    retry_after: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == ACCEPTED


ACCEPT_RESULT = Accept(ACCEPTED)
EXPIRED_RESULT = Accept(DENIED, REASON_EXPIRED)
WATER_RESULT = Accept(DENIED, REASON_WATER)
REPLAY_RESULT = Accept(DENIED, REASON_REPLAY)


def rate_limited(retry_after: int) -> Accept:
    """`retry_after`＝`min(126, max(1, 最小計入 ts + 66 − now))`（C-306）。"""
    value = min(MAX_RETRY_AFTER, max(1, int(retry_after)))
    return Accept(RATE_LIMITED, "", value)


class _Reject(Exception):
    """交易內部的拒絕訊號：讓 `AuthDb.txn` 走 rollback 再回到 `accept`。"""

    def __init__(self, result: Accept):
        super().__init__(result.outcome)
        self.result = result


def _integer(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{0} must be an integer".format(name))
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError("{0} must be a string".format(name))
    return value


class ReplayStore:
    """防重放庫：接受請求（唯一寫入點）與背景清除。"""

    def __init__(self, path: str, clock: Clock, *, per_key_max: int = DEFAULT_PER_KEY_MAX,
                 busy_timeout: float = DEFAULT_BUSY_TIMEOUT, db: AuthDb | None = None,
                 collect_lock_waits: bool = False):
        self.path = path
        self.clock = clock
        self.per_key_max = _integer(per_key_max, "per_key_max")
        self.busy_timeout = float(busy_timeout)
        self._db = db if db is not None else open_rw(
            path, busy_timeout=busy_timeout, collect_lock_waits=collect_lock_waits
        )

    @property
    def db(self) -> AuthDb:
        return self._db

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ReplayStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- 接受請求（唯一寫入點） ---

    def accept(self, key_id: str, nonce: str, ts: int) -> Accept:
        """交易內依序：時窗 → 水位 → 重放 → 配額 → INSERT（C-305）。"""
        _text(key_id, "key_id")
        _text(nonce, "nonce")
        _integer(ts, "ts")
        deadline = time.monotonic() + self.busy_timeout
        try:
            with self._db.txn(deadline) as cur:
                now = now_seconds(self.clock)  # 拿到鎖並 BEGIN 之後才讀，整個交易只用這一個值
                if abs(now - ts) > WINDOW_SECONDS:
                    raise _Reject(EXPIRED_RESULT)
                water = self._water(cur, key_id)
                if water is not None and ts <= water:
                    raise _Reject(WATER_RESULT)
                if self._nonce_seen(cur, key_id, nonce):
                    raise _Reject(REPLAY_RESULT)
                count, oldest = self._quota(cur, key_id, now)
                if count >= self.per_key_max:
                    raise _Reject(rate_limited(oldest + RETRY_AFTER_OFFSET - now))
                try:
                    cur.execute(
                        "INSERT INTO nonces (key_id, nonce, ts) VALUES (?, ?, ?)",
                        (key_id, nonce, ts),
                    )
                except sqlite3.IntegrityError:
                    # 唯一約束衝突也是重放（C-305；查詢與 INSERT 之間被別人插隊）。
                    raise _Reject(REPLAY_RESULT) from None
                return ACCEPT_RESULT
        except _Reject as reject:
            return reject.result

    # --- 清除（自己的交易內讀 now；批間放鎖） ---

    def purge_once(self) -> int:
        """刪掉所有 `ts < now − 65` 的列，回傳刪了幾列（C-307）。

        C-308 的不變式「庫中每列 `ts > H[key]`」在**每一輪清除結束後**成立：同一 `ts`
        若剛好被 `PURGE_BATCH` 截斷，批與批之間會短暫出現同 `ts` 的列尚未刪，但那些列的
        `ts` 已 `< now − 65`，任何新請求都先被 ±60 時窗擋下，安全性不受影響。
        """
        total = 0
        while True:
            deleted, full = self._purge_batch()
            total += deleted
            if not full:
                return total

    def _purge_batch(self) -> tuple[int, bool]:
        deadline = time.monotonic() + self.busy_timeout
        with self._db.txn(deadline) as cur:
            now = now_seconds(self.clock)
            rows = cur.execute(
                "SELECT rowid, key_id, ts FROM nonces WHERE ts < ? ORDER BY ts LIMIT ?",
                (now - PURGE_SECONDS, PURGE_BATCH),
            ).fetchall()
            if not rows:
                return 0, False
            highest: dict[str, int] = {}
            for row in rows:
                key_id, ts = str(row["key_id"]), int(row["ts"])
                if key_id not in highest or ts > highest[key_id]:
                    highest[key_id] = ts
            cur.executemany(
                "DELETE FROM nonces WHERE rowid = ?", [(row["rowid"],) for row in rows]
            )
            for key_id, ts in highest.items():
                # H[key] 只升不降（除了 D5 的人工 reset-key-water）；與刪除同一交易。
                cur.execute(
                    "INSERT INTO water (key_id, h) VALUES (?, ?)"
                    " ON CONFLICT(key_id) DO UPDATE SET h = MAX(water.h, excluded.h)",
                    (key_id, ts),
                )
            return len(rows), len(rows) >= PURGE_BATCH

    # --- 觀測 ---

    def keys_with_future_water(self, now_plus: int = WINDOW_SECONDS) -> list[str]:
        """`H[key]` 超過「現在＋60 秒」的 key（啟動時只記事件；也是 llmgwctl check 的 fail 項）。"""
        now = now_seconds(self.clock)
        rows = self._db.read(
            "SELECT key_id FROM water WHERE h > ? ORDER BY key_id", (now + now_plus,)
        )
        return [str(row[0]) for row in rows]

    def water_of(self, key_id: str) -> int | None:
        """測試與 D5 觀測用；不印 nonce。"""
        rows = self._db.read("SELECT h FROM water WHERE key_id = ?", (_text(key_id, "key_id"),))
        return None if not rows else int(rows[0][0])

    def row_count(self, key_id: str | None = None) -> int:
        if key_id is None:
            rows = self._db.read("SELECT COUNT(*) FROM nonces")
        else:
            rows = self._db.read(
                "SELECT COUNT(*) FROM nonces WHERE key_id = ?", (_text(key_id, "key_id"),)
            )
        return int(rows[0][0])

    # --- 內部 ---

    def _water(self, cur, key_id: str) -> int | None:
        row = cur.execute("SELECT h FROM water WHERE key_id = ?", (key_id,)).fetchone()
        return None if row is None else int(row[0])

    def _nonce_seen(self, cur, key_id: str, nonce: str) -> bool:
        row = cur.execute(
            "SELECT 1 FROM nonces WHERE key_id = ? AND nonce = ?", (key_id, nonce)
        ).fetchone()
        return row is not None

    def _quota(self, cur, key_id: str, now: int) -> tuple[int, int]:
        """該 key 在庫中 `ts ≥ now − 65` 的列數與最小 ts（過期待清的列不計；C-305）。"""
        row = cur.execute(
            "SELECT COUNT(*), MIN(ts) FROM nonces WHERE key_id = ? AND ts >= ?",
            (key_id, now - PURGE_SECONDS),
        ).fetchone()
        count = int(row[0] or 0)
        oldest = int(row[1]) if row[1] is not None else now
        return count, oldest


class PurgeTimer:
    """背景清除執行緒（比照 `lgwd/keysync.py` 的 `KeySyncTimer` 啟停）。

    每 `interval` 秒跑一次 `purge_once()`；例外只記型別、不記值。`stop()` 會叫停並
    等執行緒收乾淨（daemon，SIGTERM 後不拖住主程式）。
    """

    def __init__(self, store: ReplayStore, *, interval: float = PURGE_INTERVAL):
        self.store = store
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.purges = 0  # 測試用：跑過幾次

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or self.interval <= 0:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auth-purge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=timeout)

    def _loop(self) -> None:
        while True:
            self.run_once()
            if self._stop.wait(self.interval):
                return

    def run_once(self) -> int:
        """跑一次清除；失敗不丟例外，回 0。"""
        try:
            deleted = self.store.purge_once()
        except Exception as exc:  # 背景執行緒不能因一次失敗就死掉
            _LOG.warning("auth purge failed: %s", type(exc).__name__)
            return 0
        self.purges += 1
        return deleted


__all__ = [
    "ACCEPTED",
    "ACCEPT_RESULT",
    "Accept",
    "DEFAULT_PER_KEY_MAX",
    "DENIED",
    "EXPIRED_RESULT",
    "MAX_RETRY_AFTER",
    "PURGE_BATCH",
    "PURGE_INTERVAL",
    "PURGE_SECONDS",
    "PurgeTimer",
    "RATE_LIMITED",
    "REASON_EXPIRED",
    "REASON_REPLAY",
    "REASON_WATER",
    "REPLAY_RESULT",
    "ReplayStore",
    "WATER_RESULT",
    "WINDOW_SECONDS",
    "rate_limited",
]
