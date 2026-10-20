"""SQLite 細節（design 決定 5b、8、10 與 v5 的決定 8 取代；spec subscription-registry、key-vault）。只用標準函式庫。

只剩兩張表：
- cooldowns：subscription → until（何時能再用；重複登錄取較晚的）；
- audit：只追加。get（過渡期）記（時刻、subscription id、agent）；resolve 記（時刻、
  subscription id、agent、provider、model—— provider／model 是中介資料）；登錄冷卻記
  （時刻、subscription id、until）。
絕不存 key 的值，也不記客戶端（沒有應用層認證）。

單一連線＋RLock；會改變狀態的動作一律經 txn()（BEGIN IMMEDIATE）。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldowns (
    subscription TEXT PRIMARY KEY,
    until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    subscription TEXT NOT NULL,
    action TEXT NOT NULL,
    agent TEXT,
    until TEXT,
    provider TEXT,
    model TEXT
);
"""

# v5 schema（gateway-v0）的 audit 沒有的欄位；啟動時以 PRAGMA table_info 檢查、
# ALTER TABLE ADD COLUMN 補上（只增不改；舊資料的新欄位為 null）。
_AUDIT_MIGRATE_COLUMNS = ("provider", "model")


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(SCHEMA)
        self._migrate_audit()
        self._lock = threading.RLock()

    def _migrate_audit(self) -> None:
        """additive 遷移：v5 的 audit 少 provider／model 欄就補上。

        CREATE TABLE IF NOT EXISTS 不會補欄位，所以要一次 PRAGMA 檢查＋ALTER TABLE。
        舊 lgwd 的 SELECT 指定欄位，讀到多出的欄位不受影響（支援退版）。
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(audit)")}
        for column in _AUDIT_MIGRATE_COLUMNS:
            if column not in existing:
                self._conn.execute("ALTER TABLE audit ADD COLUMN {0} TEXT".format(column))
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def txn(self) -> Iterator[sqlite3.Cursor]:
        """一筆 BEGIN IMMEDIATE 交易；成功或預期的拒絕（ApiError）都 commit，
        非預期錯誤才 rollback。"""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # --- 冷卻 ---
    def cooldown_get(self, cur: sqlite3.Cursor, subscription: str) -> sqlite3.Row | None:
        return cur.execute(
            "SELECT * FROM cooldowns WHERE subscription = ?", (subscription,)
        ).fetchone()

    def cooldown_set(self, cur: sqlite3.Cursor, subscription: str, until: str) -> None:
        """寫入；同一 subscription 取較晚的 until（UPSERT 只在新的較晚時更新）。"""
        cur.execute(
            "INSERT INTO cooldowns (subscription, until) VALUES (?, ?)"
            " ON CONFLICT(subscription) DO UPDATE SET until = excluded.until"
            " WHERE excluded.until > cooldowns.until",
            (subscription, until),
        )

    # --- 稽核（只追加） ---
    def audit_add(
        self,
        cur: sqlite3.Cursor,
        ts: str,
        subscription: str,
        action: str,
        agent: str | None = None,
        until: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        cur.execute(
            "INSERT INTO audit (ts, subscription, action, agent, until, provider, model)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, subscription, action, agent, until, provider, model),
        )

    def audit_all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, subscription, action, agent, until, provider, model"
                " FROM audit ORDER BY seq"
            ).fetchall()
            return [dict(r) for r in rows]
