"""稽核加 principal 欄（#222 D3b；C-910～C-913、W-3h／W-3i／W-3c）。

兩層：

- 服務／儲存層（真的 `Store`、真的 `RegistryService`、暫存 SQLite）：additive migration
  的可重跑與退版、`audit_add`／`audit_all` 的 principal、service 介面只能以關鍵字傳
  principal（C-913）。
- 端到端（真的 loopback、真的 `ssh-keygen`、真的兩庫、真的 `RegistryService`）：只有
  驗章 PASS 且授權通過才寫已驗證的 principal；off／warn 無簽、壞簽、未知 key 一律
  null，絕不使用標頭自稱的 `X-LGW-Key-Id`（C-911／W-3i）；被拒的請求沒有業務稽核列
  （C-912／W-3c）。

不碰 `/var/lib`、`/etc`、不讀憑證、不連真機；金鑰只在暫存目錄產生、用完即刪。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lgwd import auth
from lgwd.clock import FakeClock
from lgwd.errors import ApiError
from lgwd.keys import KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService
from lgwd.store import Store
from tests.auth_fakes import principal_all
from tests.test_http_authz import (
    COOLDOWN,
    FORBIDDEN,
    JSON_BODY,
    RESOLVE,
    HttpAuthzCase,
    RecordingService,
    no_resolve,
)
from tests.test_registry import CANARY, key_text

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = str(REPO_ROOT / "policy" / "default.json")

#: 舊版（D3b 之前）`Store.audit_all` 的 SELECT：指定欄位、沒有 principal（退版情境）。
_LEGACY_AUDIT_SQL = (
    "SELECT seq, ts, subscription, action, agent, until, provider, model"
    " FROM audit ORDER BY seq"
)

#: v5（gateway-v0）的 audit 沒有 provider／model／principal 三欄。
_V5_SCHEMA = """
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
    until TEXT
);
"""


def _audit_columns(path: str) -> list[tuple[str, str]]:
    """`(欄名, 型別)` 清單（PRAGMA table_info 的順序）。"""
    conn = sqlite3.connect(path)
    try:
        return [(row[1], row[2]) for row in conn.execute("PRAGMA table_info(audit)")]
    finally:
        conn.close()


class C910StoreMigrationTests(unittest.TestCase):
    """C-910／W-3h：principal 放進既有的 additive migration，可重跑、只增不改。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "llm.db")

    def _v5_db_with_history(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_V5_SCHEMA)
            conn.execute(
                "INSERT INTO audit (ts, subscription, action, agent)"
                " VALUES ('2026-10-11T00:00:00Z', 'claude-personal', 'get',"
                " 'claude-code')")
            conn.commit()
        finally:
            conn.close()

    def test_C910_W3h_upgrade_keeps_history_and_adds_null_principal(self):
        """有歷史列的舊 DB 升級後：欄位存在、舊列 principal 為 null、audit_all 照常。"""
        self._v5_db_with_history()
        store = Store(self.db_path)
        self.addCleanup(store.close)

        columns = dict(_audit_columns(self.db_path))
        self.assertIn("principal", columns)
        self.assertEqual(columns["principal"], "TEXT")
        # additive：舊欄位都還在、型別沒被改。
        for name in ("seq", "ts", "subscription", "action", "agent", "until"):
            self.assertIn(name, columns)

        rows = store.audit_all()
        self.assertEqual([row["action"] for row in rows], ["get"])
        self.assertIsNone(rows[0]["principal"])
        self.assertEqual(rows[0]["agent"], "claude-code")

        # 新列可以寫 principal，舊列維持 null。
        with store.txn() as cur:
            store.audit_add(cur, "2026-10-11T01:00:00Z", "opencode-zen", "resolve",
                            agent="opencode", provider="opencode-zen",
                            model="big-pickle", principal="w1-worker")
        rows = store.audit_all()
        self.assertEqual([row["action"] for row in rows], ["get", "resolve"])
        self.assertIsNone(rows[0]["principal"])
        self.assertEqual(rows[1]["principal"], "w1-worker")

    def test_C910_W3h_second_start_does_not_add_the_column_again(self):
        """同一個 DB 啟動兩次：不重複加欄、不重建表、資料照舊。"""
        first = Store(self.db_path)
        with first.txn() as cur:
            first.audit_add(cur, "2026-10-11T01:00:00Z", "opencode-zen", "resolve",
                            principal="w1-worker")
        first.close()

        second = Store(self.db_path)
        self.addCleanup(second.close)
        names = [name for name, _ in _audit_columns(self.db_path)]
        self.assertEqual(names.count("principal"), 1)
        rows = second.audit_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["principal"], "w1-worker")

    def test_C910_low1_duplicate_column_race_is_tolerated(self):
        """低-1：兩個行程同時第一次開舊庫時，後到者的 duplicate column 不算錯。"""
        store = Store(self.db_path)
        self.addCleanup(store.close)
        # 欄位已經在了，再 ALTER 一次就是那個競態的結果：不丟例外、也不重複加欄。
        store._add_audit_column("principal")
        names = [name for name, _ in _audit_columns(self.db_path)]
        self.assertEqual(names.count("principal"), 1)
        # 其他 OperationalError 照樣往外丟，不吞掉真正的失敗。
        real_conn = store._conn

        class _FailingConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error")

        store._conn = _FailingConn()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store._add_audit_column("principal")
        finally:
            store._conn = real_conn

    def test_C910_W3h_legacy_store_reads_the_new_db(self):
        """新 DB 交給舊版 Store 的 SQL 讀：舊欄位照常、多出的 principal 欄不影響。"""
        store = Store(self.db_path)
        with store.txn() as cur:
            store.audit_add(cur, "2026-10-11T01:00:00Z", "opencode-zen", "resolve",
                            agent="opencode", provider="opencode-zen",
                            model="big-pickle", principal="w1-worker")
        store.close()

        conn = sqlite3.connect(self.db_path)
        try:
            legacy = conn.execute(_LEGACY_AUDIT_SQL).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0][2:5], ("opencode-zen", "resolve", "opencode"))
        self.assertEqual(legacy[0][6:8], ("opencode-zen", "big-pickle"))


class C910ServiceCase(unittest.TestCase):
    """真的 RegistryService（暫存 SQLite、假 key 檔、FakeClock）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(key_text(CANARY), encoding="utf-8")
        self.service = RegistryService(
            str(root / "llm.db"), load_policy(POLICY_PATH),
            KeyStore(str(keys_path)), FakeClock())
        self.addCleanup(self.service.close)

    def rows(self):
        return self.service._store.audit_all()


class C913ServiceInterfaceTests(C910ServiceCase):
    """C-913：`resolve`／`register_cooldown` 的 principal 只能以關鍵字傳入。"""

    def test_C913_principal_is_keyword_only(self):
        with self.assertRaises(TypeError):
            self.service.resolve("opencode", "opencode-zen", None, None, None, None,
                                 "w1-worker")
        with self.assertRaises(TypeError):
            self.service.register_cooldown(
                "claude-personal", {"until": "2026-10-11T15:00:00Z"}, "w1-worker")
        self.assertEqual(self.rows(), [])

    def test_C911_without_principal_audits_null(self):
        """既有呼叫方（不傳 principal）不受影響，稽核該欄是 null。"""
        self.service.resolve("opencode", "opencode-zen", model="big-pickle")
        self.service.register_cooldown("claude-personal",
                                       {"until": "2026-10-11T15:00:00Z"})
        rows = self.rows()
        self.assertEqual([row["action"] for row in rows], ["resolve", "cooldown"])
        self.assertEqual([row["principal"] for row in rows], [None, None])

    def test_C912_principal_is_written_in_the_same_transaction(self):
        """稽核與業務在同一個交易（沿用既有 `with self._store.txn()`）；失敗的業務不寫。"""
        self.service.resolve("opencode", "opencode-zen", model="big-pickle",
                             principal="w1-worker")
        self.service.register_cooldown("claude-personal",
                                       {"until": "2026-10-11T15:00:00Z"},
                                       principal="w1-worker")
        rows = self.rows()
        self.assertEqual([row["principal"] for row in rows],
                         ["w1-worker", "w1-worker"])
        # 業務失敗（未知 id 404）不寫稽核。
        before = len(rows)
        with self.assertRaises(ApiError):
            self.service.register_cooldown("no-such-subscription",
                                           {"until": "2026-10-11T15:00:00Z"},
                                           principal="w1-worker")
        self.assertEqual(len(self.rows()), before)

    def test_C912_audit_rolls_back_with_the_business_transaction(self):
        """稽核列真的在同一個交易內：交易回滾時它跟著消失（不是交易外補寫）。"""
        real_add = self.service._store.audit_add

        def insert_then_fail(cur, *args, **kwargs):
            real_add(cur, *args, **kwargs)
            raise RuntimeError("業務交易中的失敗")

        with mock.patch.object(self.service._store, "audit_add",
                               side_effect=insert_then_fail):
            with self.assertRaises(RuntimeError):
                self.service.resolve("opencode", "opencode-zen", model="big-pickle",
                                     principal="w1-worker")
        self.assertEqual(self.service._store.audit_all(), [])


def _corrupt_signature(headers) -> list:
    """把簽章值改掉一個字元：標頭自稱的 key ID 不變，簽章驗不過。"""
    out = []
    for name, value in headers:
        if name == "X-LGW-Signature":
            first = "A" if value[0] != "A" else "B"
            value = first + value[1:]
        out.append((name, value))
    return out


class C911EndToEndTests(HttpAuthzCase):
    """C-911／W-3i：端到端只記已驗證的 principal，絕不用標頭自稱的 key ID。"""

    def test_C911_enforce_verified_requests_audit_the_principal(self):
        runtime, service, port = self.server_for()
        status, payload, _ = self.call(port, "GET", RESOLVE, self.sign(target=RESOLVE))
        self.assertEqual(status, 200, payload)
        headers = self.sign(method="POST", target=COOLDOWN, body=JSON_BODY)
        status, _, _ = self.call(port, "POST", COOLDOWN, headers, JSON_BODY)
        self.assertEqual(status, 204)
        rows = service.audit_rows()
        self.assertEqual([row["action"] for row in rows], ["resolve", "cooldown"])
        self.assertEqual([row["principal"] for row in rows],
                         ["w1-worker", "w1-worker"])
        self.assertEqual(service.principals,
                         [("resolve", "w1-worker"),
                          ("register_cooldown", "w1-worker")])

    def test_C911_warn_unsigned_resolve_audits_null(self):
        runtime, service, port = self.server_for(mode="warn")
        status, payload, _ = self.call(port, "GET", RESOLVE)
        self.assertEqual(status, 200, payload)
        rows = service.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["principal"])
        self.assertEqual(service.principals, [("resolve", None)])

    def test_C911_warn_forbidden_records_the_verified_principal(self):
        """W-3e（PM 於 #279 fix1 裁定）：warn 下授權 forbidden、業務照舊成功 → 記已驗證的 principal。"""
        runtime, service, port = self.server_for(mode="warn", principal=no_resolve())
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, payload, _ = self.call(port, "GET", RESOLVE,
                                           self.sign(target=RESOLVE))
        self.assertEqual(status, 200, payload)
        rows = service.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "resolve")
        self.assertEqual(rows[0]["principal"], "w1-worker")
        self.assertEqual(service.principals, [("resolve", "w1-worker")])
        text = "\n".join(logs.output)
        self.assertIn("outcome=forbidden", text)
        self.assertIn("principal=w1-worker", text)

    def test_C911_enforce_forbidden_writes_no_business_audit(self):
        """enforce 的 forbidden 在 apply 就回 403：業務不執行、稽核沒有新增列。"""
        runtime, service, port = self.server_for(principal=no_resolve())
        status, payload, _ = self.call(port, "GET", RESOLVE, self.sign(target=RESOLVE))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(service.audit_rows(), [])
        self.assertEqual(service.principals, [])

    def test_W3i_bad_signature_never_uses_the_claimed_key_id(self):
        """壞簽帶 `X-LGW-Key-Id: k-a`（真實 key ID）：enforce 401 無稽核列、日誌 principal=-。"""
        runtime, service, port = self.server_for()
        headers = _corrupt_signature(self.sign(target=RESOLVE))
        self.assertEqual(dict(headers)["X-LGW-Key-Id"], "k-a")
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, payload, _ = self.call(port, "GET", RESOLVE, headers)
        self.assertEqual((status, payload), (401, b'{"error": "unauthorized"}'))
        self.assertEqual(service.audit_rows(), [])
        text = "\n".join(logs.output)
        self.assertIn("principal=-", text)
        self.assertNotIn("principal=k-a", text)
        self.assertNotIn("principal=w1-worker", text)

    def test_W3i_unknown_key_never_uses_the_claimed_key_id(self):
        """未知 key 帶自稱的 `X-LGW-Key-Id`：同樣不進業務、稽核沒有列、日誌 principal=-。"""
        runtime, service, port = self.server_for()
        headers = self.sign(key_id="k-unknown", target=RESOLVE)
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, _, _ = self.call(port, "GET", RESOLVE, headers)
        self.assertEqual(status, 401)
        self.assertEqual(service.audit_rows(), [])
        self.assertIn("principal=-", "\n".join(logs.output))

    def test_W3i_warn_bad_or_unknown_signature_still_audits_null(self):
        """warn 下壞簽／未知 key 照舊處理業務，但稽核 principal 仍是 null（不是自稱值）。"""
        runtime, service, port = self.server_for(mode="warn")
        tampered = _corrupt_signature(self.sign(target=RESOLVE))
        self.assertEqual(dict(tampered)["X-LGW-Key-Id"], "k-a")
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, payload, _ = self.call(port, "GET", RESOLVE, tampered)
        self.assertEqual(status, 200, payload)
        unknown = self.sign(key_id="k-unknown", target=RESOLVE)
        with self.assertLogs("lgwd.http", level="INFO") as more:
            status, payload, _ = self.call(port, "GET", RESOLVE, unknown)
        self.assertEqual(status, 200, payload)
        rows = service.audit_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["principal"] for row in rows], [None, None])
        self.assertEqual(service.principals,
                         [("resolve", None), ("resolve", None)])
        for text in ("\n".join(logs.output), "\n".join(more.output)):
            self.assertIn("principal=-", text)


class C912RejectedRequestsTests(HttpAuthzCase):
    """C-912／W-3c：401／403／429／503 都沒有業務稽核列；只有成功的那一次才寫。"""

    def test_W3c_rejected_requests_write_no_business_audit(self):
        target = RESOLVE
        # 401：無簽章。
        paths = self.make_paths()
        self.write_registry(paths, 1, principal=None)
        runtime = self.open_runtime(paths)
        service = RecordingService(self.make_service())
        server, port = self.serve(runtime.gate, service)
        self.assertEqual(self.call(port, "GET", target)[0], 401)
        self.assertEqual(service.audit_rows(), [])
        # 401：被拒的 POST cooldown 也走同一個閘門（agy 低-1）；一樣沒有業務稽核列。
        status, payload, _ = self.call(port, "POST", COOLDOWN, (), JSON_BODY)
        self.assertEqual((status, payload), (401, b'{"error": "unauthorized"}'))
        self.assertEqual(service.audit_rows(), [])
        # 403：有效簽章但 principal 的 ops 沒有 cooldown → 403，仍沒有業務稽核列。
        no_cooldown = principal_all(
            ops=["list", "usage", "roles", "resolve"],
            resolve_agents=["opencode"], roles={"impl": ["opencode"]})
        runtime_nc, service_nc, port_nc = self.server_for(principal=no_cooldown)
        headers = self.sign(method="POST", target=COOLDOWN, body=JSON_BODY)
        status, payload, _ = self.call(port_nc, "POST", COOLDOWN, headers, JSON_BODY)
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(service_nc.audit_rows(), [])
        # 403：同一 runtime 換成缺 resolve 的名冊。
        self.write_registry(paths, 2, no_resolve())
        runtime.watcher.tick()
        status, payload, _ = self.call(port, "GET", target, self.sign(target=target))
        self.assertEqual((status, payload), (403, FORBIDDEN))
        self.assertEqual(service.audit_rows(), [])
        # 429：per_key_max=1（先成功一筆用量，第二筆撞配額）。
        runtime429, service429, port429 = self.server_for(per_key_max=1)
        usage = "/v1/usage"
        self.assertEqual(
            self.call(port429, "GET", usage, self.sign(target=usage))[0], 200)
        status, payload, _ = self.call(port429, "GET", target,
                                       self.sign(target=target))
        self.assertEqual((status, json.loads(payload)["error"]),
                         (429, "rate_limited"))
        self.assertEqual(service429.audit_rows(), [])
        # 503：驗章／庫不可用的 gate（不碰業務）。
        service503 = RecordingService(self.make_service())
        gate = auth.AuthGate(mode="enforce", clock=self.clock,
                             verifier=None, replay=None, stores_ok=False)
        server503, port503 = self.serve(gate, service503)
        status, payload, _ = self.call(port503, "GET", target,
                                       self.sign(target=target))
        self.assertEqual((status, json.loads(payload)["error"]),
                         (503, "auth_unavailable"))
        self.assertEqual(service503.audit_rows(), [])
        # 成功的那一次才寫業務稽核（而且帶已驗證的 principal）。
        runtime_ok, service_ok, port_ok = self.server_for()
        status, _, _ = self.call(port_ok, "GET", target, self.sign(target=target))
        self.assertEqual(status, 200)
        rows = service_ok.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "resolve")
        self.assertEqual(rows[0]["principal"], "w1-worker")


if __name__ == "__main__":
    unittest.main()
