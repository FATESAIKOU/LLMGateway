"""SQLite 細節（LeaseService 的下層；由 impl 實作）。只用標準函式庫。

- 單一連線＋RLock；會改變狀態的動作一律經 txn()（BEGIN IMMEDIATE）。
- events 只追加、seq 單調；leases／cooldowns／clients 由事件重建（rebuild）。
- leases 列、events payload 絕不存 env（key 的值只活在回覆裡）。
- dedup 存第一次的完整回覆（保留 7 天，跨重啟有效）。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator

from .errors import ApiError

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    client TEXT,
    lease_id TEXT,
    subscription TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    worker TEXT NOT NULL,
    role TEXT NOT NULL,
    agent TEXT NOT NULL,
    instance TEXT NOT NULL,
    subscription TEXT NOT NULL,
    model TEXT,
    purpose TEXT,
    preferences TEXT,
    status TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    confirm_by TEXT NOT NULL,
    started_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS cooldowns (
    subscription TEXT PRIMARY KEY,
    until TEXT NOT NULL,
    signal TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    name TEXT PRIMARY KEY,
    cred_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS dedup (
    client TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client, request_id)
);
"""


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self.rebuild()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def txn(self) -> Iterator[sqlite3.Cursor]:
        """一筆 BEGIN IMMEDIATE 交易；成功或預期的拒絕（ApiError）都 commit，
        非預期錯誤才 rollback（denied 事件與延後結算要留下來）。"""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self._conn.commit()
            except ApiError:
                self._conn.commit()
                raise
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # --- 事件 ---
    def log(
        self,
        cur: sqlite3.Cursor,
        type: str,
        ts: str,
        client: str | None = None,
        lease_id: str | None = None,
        subscription: str | None = None,
        payload: dict | None = None,
    ) -> int:
        cur.execute(
            "INSERT INTO events (ts, type, client, lease_id, subscription, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts, type, client, lease_id, subscription, json.dumps(payload or {})),
        )
        return cur.lastrowid

    def all_events(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, type, client, lease_id, subscription, payload"
                " FROM events ORDER BY seq"
            ).fetchall()
            return [dict(r) for r in rows]

    # --- 客戶端 ---
    def client_add(self, cur: sqlite3.Cursor, name: str, cred_hash: str, created_at: str) -> None:
        cur.execute(
            "INSERT INTO clients (name, cred_hash, created_at) VALUES (?, ?, ?)",
            (name, cred_hash, created_at),
        )

    def client_get(self, cur: sqlite3.Cursor, name: str) -> sqlite3.Row | None:
        return cur.execute("SELECT * FROM clients WHERE name = ?", (name,)).fetchone()

    def client_all(self, cur: sqlite3.Cursor) -> list[sqlite3.Row]:
        return cur.execute("SELECT * FROM clients ORDER BY name").fetchall()

    def client_revoke(self, cur: sqlite3.Cursor, name: str, revoked_at: str) -> None:
        cur.execute("UPDATE clients SET revoked_at = ? WHERE name = ?", (revoked_at, name))

    # --- 租約 ---
    def lease_add(self, cur: sqlite3.Cursor, **fields) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur.execute(f"INSERT INTO leases ({cols}) VALUES ({marks})", tuple(fields.values()))

    def lease_get(self, cur: sqlite3.Cursor, lease_id: str) -> sqlite3.Row | None:
        return cur.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()

    def lease_update(self, cur: sqlite3.Cursor, lease_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur.execute(f"UPDATE leases SET {sets} WHERE lease_id = ?", (*fields.values(), lease_id))

    def leases_unfinished(self, cur: sqlite3.Cursor) -> list[sqlite3.Row]:
        return cur.execute(
            "SELECT * FROM leases WHERE status IN ('granted', 'active') ORDER BY granted_at"
        ).fetchall()

    def count_unfinished_for_sub(self, cur: sqlite3.Cursor, subscription: str) -> int:
        row = cur.execute(
            "SELECT COUNT(*) AS n FROM leases"
            " WHERE subscription = ? AND status IN ('granted', 'active')",
            (subscription,),
        ).fetchone()
        return int(row["n"])

    def has_unfinished_instance(self, cur: sqlite3.Cursor, client: str, instance: str) -> bool:
        row = cur.execute(
            "SELECT COUNT(*) AS n FROM leases"
            " WHERE client = ? AND instance = ? AND status IN ('granted', 'active')",
            (client, instance),
        ).fetchone()
        return int(row["n"]) > 0

    def actives_for_sub(self, cur: sqlite3.Cursor, subscription: str) -> list[sqlite3.Row]:
        return cur.execute(
            "SELECT * FROM leases WHERE subscription = ? AND status = 'active'"
            " ORDER BY started_at",
            (subscription,),
        ).fetchall()

    # --- 冷卻 ---
    def cool_get(self, cur: sqlite3.Cursor, subscription: str) -> sqlite3.Row | None:
        return cur.execute(
            "SELECT * FROM cooldowns WHERE subscription = ?", (subscription,)
        ).fetchone()

    def cool_set(
        self, cur: sqlite3.Cursor, subscription: str, until: str, signal: str | None,
        updated_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO cooldowns (subscription, until, signal, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(subscription) DO UPDATE SET"
            " until = excluded.until, signal = excluded.signal, updated_at = excluded.updated_at",
            (subscription, until, signal, updated_at),
        )

    def cool_all(self, cur: sqlite3.Cursor) -> list[sqlite3.Row]:
        return cur.execute("SELECT * FROM cooldowns").fetchall()

    # --- 去重 ---
    def dedup_get(self, cur: sqlite3.Cursor, client: str, request_id: str) -> sqlite3.Row | None:
        return cur.execute(
            "SELECT * FROM dedup WHERE client = ? AND request_id = ?", (client, request_id)
        ).fetchone()

    def dedup_put(
        self, cur: sqlite3.Cursor, client: str, request_id: str,
        status: int, body: dict, created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO dedup (client, request_id, status, body, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(client, request_id) DO NOTHING",
            (client, request_id, status, json.dumps(body), created_at),
        )

    def dedup_prune(self, cur: sqlite3.Cursor, cutoff: str) -> None:
        cur.execute("DELETE FROM dedup WHERE created_at < ?", (cutoff,))

    # --- 重建 ---
    def rebuild(self) -> None:
        """從 events 重建現況（leases／cooldowns／clients；dedup 沿用原表）。"""
        with self.txn() as cur:
            rows = cur.execute(
                "SELECT ts, type, client, lease_id, subscription, payload"
                " FROM events ORDER BY seq"
            ).fetchall()
            cur.execute("DELETE FROM leases")
            cur.execute("DELETE FROM cooldowns")
            cur.execute("DELETE FROM clients")
            for row in rows:
                self._apply(cur, row)

    def _apply(self, cur: sqlite3.Cursor, row: sqlite3.Row) -> None:
        p = json.loads(row["payload"] or "{}")
        t = row["type"]
        if t == "client_added":
            # 雜湊不可逆，記進事件不洩漏憑證；重建時還原身分。
            cur.execute(
                "INSERT INTO clients (name, cred_hash, created_at, revoked_at)"
                " VALUES (?, ?, ?, NULL)"
                " ON CONFLICT(name) DO UPDATE SET"
                " cred_hash = excluded.cred_hash, created_at = excluded.created_at,"
                " revoked_at = NULL",
                (p["name"], p.get("cred_hash", ""), p["created_at"]),
            )
        elif t == "client_revoked":
            cur.execute(
                "UPDATE clients SET revoked_at = ? WHERE name = ?", (p["revoked_at"], p["name"])
            )
        elif t == "granted":
            cur.execute(
                "INSERT OR IGNORE INTO leases (lease_id, client, worker, role, agent,"
                " instance, subscription, model, purpose, preferences,"
                " status, granted_at, confirm_by, started_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'granted', ?, ?, NULL, NULL)",
                (
                    p["lease_id"], p["client"], p["worker"], p["role"], p["agent"],
                    p["instance"], p["subscription"], p.get("model"), p.get("purpose"),
                    json.dumps(p["preferences"]) if p.get("preferences") is not None else None,
                    p["granted_at"], p["confirm_by"],
                ),
            )
        elif t == "started":
            cur.execute(
                "UPDATE leases SET status = 'active', started_at = ?, expires_at = ?"
                " WHERE lease_id = ?",
                (p["started_at"], p["expires_at"], p["lease_id"]),
            )
        elif t == "renewed":
            cur.execute(
                "UPDATE leases SET expires_at = ? WHERE lease_id = ?", (p["expires_at"], p["lease_id"])
            )
        elif t == "released":
            cur.execute(
                "UPDATE leases SET status = 'released' WHERE lease_id = ?", (p["lease_id"],)
            )
        elif t == "expired":
            cur.execute(
                "UPDATE leases SET status = 'expired' WHERE lease_id = ?", (p["lease_id"],)
            )
        elif t == "superseded":
            cur.execute(
                "UPDATE leases SET status = 'superseded' WHERE lease_id = ?", (p["lease_id"],)
            )
        elif t == "lease_revoked":
            cur.execute(
                "UPDATE leases SET status = 'revoked' WHERE lease_id = ?", (p["lease_id"],)
            )
        elif t == "cooling":
            cur.execute(
                "INSERT INTO cooldowns (subscription, until, signal, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(subscription) DO UPDATE SET"
                " until = excluded.until, signal = excluded.signal,"
                " updated_at = excluded.updated_at",
                (p["subscription"], p["until"], p.get("signal"), p.get("updated_at", "")),
            )
        # requested／denied／usage 不影響現況表，只留事件。
