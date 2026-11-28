"""#347 N13a §3／C-1040：``llmgwctl cooldown-clear`` 的本機縮短與清除。

- clear 冪等、replace 範圍、原子性（交易中途失敗整筆 rollback）、兩層互不刪除、``--model`` 驗證。
- ``--model`` 的規則與 lgwd/registry.py 的 ``_cooldown_model`` 逐案對照。
- 只用假資料：暫存目錄裡的 llm.db（以 lgwd.store.Store 建出真實 schema），政策檔以符號連結指向本 repo。
"""

from __future__ import annotations

import datetime as _dt
import io
import os
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lgwctl import cooldown_clear  # noqa: E402
from lgwctl.cli import main  # noqa: E402
from lgwctl.layout import Layout  # noqa: E402
from lgwd.clock import to_rfc3339  # noqa: E402
from lgwd.policy import load_policy  # noqa: E402
from lgwd.registry import RegistryService  # noqa: E402
from lgwd.store import Store  # noqa: E402

NOW = _dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
SUB = "opencode-zen"
MODEL = "big-pickle"
POLICY = load_policy(str(REPO_ROOT / "policy" / "default.json"))
MAX_AHEAD = POLICY.until_max_ahead_seconds

# 與 lgwd 舊版 audit（v5 基底）對齊的最小欄位集合，用來造「沒有新欄」的舊 DB。
V5_AUDIT = """
CREATE TABLE cooldowns (subscription TEXT PRIMARY KEY, until TEXT NOT NULL);
CREATE TABLE model_cooldowns (subscription TEXT NOT NULL, model TEXT NOT NULL,
    until TEXT NOT NULL, PRIMARY KEY (subscription, model));
CREATE TABLE audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    subscription TEXT NOT NULL, action TEXT NOT NULL, agent TEXT, until TEXT,
    provider TEXT, model TEXT, principal TEXT);
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.prefix = self._tmp.name
        self.db = os.path.join(self.prefix, "var", "lib", "llmgateway", "llm.db")
        os.makedirs(os.path.dirname(self.db))
        current = os.path.join(self.prefix, "opt", "llmgateway", "current")
        os.makedirs(os.path.dirname(current))
        os.symlink(str(REPO_ROOT), current)
        self.layout = Layout.from_env({"LLMGW_PREFIX": self.prefix, "SUDO_USER": "tester"})

    def tearDown(self):
        self._tmp.cleanup()

    # --- 假資料 ---
    def new_db(self):
        Store(self.db).close()  # 真實 schema（含 additive 遷移）

    def exec_sql(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def query(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def set_whole(self, until, sub=SUB):
        self.exec_sql("INSERT OR REPLACE INTO cooldowns (subscription, until) VALUES (?, ?)",
                      (sub, to_rfc3339(until)))

    def set_model(self, until, sub=SUB, model=MODEL):
        self.exec_sql("INSERT OR REPLACE INTO model_cooldowns (subscription, model, until)"
                      " VALUES (?, ?, ?)", (sub, model, to_rfc3339(until)))

    def whole(self, sub=SUB):
        rows = self.query("SELECT until FROM cooldowns WHERE subscription = ?", (sub,))
        return rows[0]["until"] if rows else None

    def model_row(self, sub=SUB, model=MODEL):
        rows = self.query("SELECT until FROM model_cooldowns WHERE subscription = ?"
                          " AND model = ?", (sub, model))
        return rows[0]["until"] if rows else None

    def audit_rows(self):
        return self.query("SELECT * FROM audit WHERE action LIKE 'cooldown_%' ORDER BY seq")

    def call(self, subscription=SUB, model=None, until=None, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        code = cooldown_clear.run(self.layout, subscription=subscription, model=model,
                                  until=until, now=now, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class ClearTests(_Base):
    def test_c1040_clear_whole_deletes_row_and_writes_audit(self):
        self.new_db()
        self.set_whole(NOW + _dt.timedelta(hours=1))
        code, _, _ = self.call()
        self.assertEqual(code, 0)
        self.assertIsNone(self.whole())
        [row] = self.audit_rows()
        self.assertEqual(row["action"], "cooldown_clear")
        self.assertEqual(row["cooldown_mode"], "clear")
        self.assertEqual(row["prev_until"], to_rfc3339(NOW + _dt.timedelta(hours=1)))
        self.assertIsNone(row["until"])
        self.assertIsNone(row["principal"])
        self.assertEqual(row["os_user"], "tester")
        self.assertEqual(row["subscription"], SUB)

    def test_c1040_clear_is_idempotent_when_no_row(self):
        self.new_db()
        code, out, _ = self.call()
        self.assertEqual(code, 0)
        self.assertIn("無冷卻列", out)
        self.assertEqual(self.audit_rows(), [])
        code, _, _ = self.call()  # 再跑一次仍是 0
        self.assertEqual(code, 0)

    def test_c1040_clear_expired_row_records_null_prev_until(self):
        self.new_db()
        self.set_whole(NOW - _dt.timedelta(minutes=5))  # 已過期：不算「有效冷卻」
        code, _, _ = self.call()
        self.assertEqual(code, 0)
        [row] = self.audit_rows()
        self.assertIsNone(row["prev_until"])
        self.assertIsNone(self.whole())

    def test_c1040_clear_model_leaves_whole_sub_layer(self):
        self.new_db()
        self.set_whole(NOW + _dt.timedelta(hours=2))
        self.set_model(NOW + _dt.timedelta(hours=2))
        code, _, _ = self.call(model=MODEL)
        self.assertEqual(code, 0)
        self.assertIsNone(self.model_row())
        self.assertIsNotNone(self.whole())  # 兩層互不刪除

    def test_c1040_clear_whole_leaves_model_layer(self):
        self.new_db()
        self.set_whole(NOW + _dt.timedelta(hours=2))
        self.set_model(NOW + _dt.timedelta(hours=2))
        code, _, _ = self.call()
        self.assertEqual(code, 0)
        self.assertIsNone(self.whole())
        self.assertIsNotNone(self.model_row())

    def test_c1040_clear_model_unknown_subscription_is_refused(self):
        self.new_db()
        code, _, err = self.call(subscription="no-such-sub", model=MODEL)
        self.assertEqual(code, 2)
        self.assertIn("未知的訂閱", err)


class ReplaceTests(_Base):
    def test_c1040_replace_overwrites_even_when_shorter(self):
        self.new_db()
        old = NOW + _dt.timedelta(hours=5)
        self.set_whole(old)
        new = NOW + _dt.timedelta(hours=1)
        code, _, _ = self.call(until=to_rfc3339(new))
        self.assertEqual(code, 0)
        self.assertEqual(self.whole(), to_rfc3339(new))  # 縮短，不取較晚的
        [row] = self.audit_rows()
        self.assertEqual(row["action"], "cooldown_replace")
        self.assertEqual(row["cooldown_mode"], "replace")
        self.assertEqual(row["until"], to_rfc3339(new))
        self.assertEqual(row["prev_until"], to_rfc3339(old))
        self.assertEqual(row["os_user"], "tester")

    def test_c1040_replace_creates_row_when_absent(self):
        self.new_db()
        new = NOW + _dt.timedelta(hours=1)
        code, _, _ = self.call(model=MODEL, until=to_rfc3339(new))
        self.assertEqual(code, 0)
        self.assertEqual(self.model_row(), to_rfc3339(new))
        self.assertIsNone(self.whole())
        [row] = self.audit_rows()
        self.assertIsNone(row["prev_until"])
        self.assertEqual(row["model"], MODEL)

    def test_c1040_replace_range_lower_bound(self):
        self.new_db()
        code, _, err = self.call(until=to_rfc3339(NOW - _dt.timedelta(seconds=1)))
        self.assertEqual(code, 2)
        self.assertIn("早於現在", err)
        self.assertIsNone(self.whole())
        self.assertEqual(self.audit_rows(), [])

    def test_c1040_replace_range_upper_bound_inclusive(self):
        self.new_db()
        edge = NOW + _dt.timedelta(seconds=MAX_AHEAD)
        code, _, _ = self.call(until=to_rfc3339(edge))
        self.assertEqual(code, 0)  # 等於上限可以
        code, _, err = self.call(until=to_rfc3339(edge + _dt.timedelta(seconds=1)))
        self.assertEqual(code, 2)
        self.assertIn("超過", err)

    def test_c1040_replace_rejects_bad_timestamp(self):
        self.new_db()
        for bad in ("2026-12-01 01:00:00Z", "not-a-time", "2026-12-01T01:00:00",
                    "2026-02-30T01:00:00Z", "2026-12-01T01:00:00.5Z"):
            with self.subTest(bad=bad):
                code, _, _ = self.call(until=bad)
                self.assertEqual(code, 2)
        self.assertEqual(self.audit_rows(), [])


class AtomicityTests(_Base):
    """審查指定的「交易中途失敗」：audit 寫入被擋時，冷卻列必須跟著回滾。"""

    def _block_audit(self, action):
        self.exec_sql("CREATE TRIGGER block_audit BEFORE INSERT ON audit"
                      " WHEN NEW.action = '{0}' BEGIN SELECT RAISE(ABORT, 'blocked'); END"
                      .format(action))

    def test_c1040_rollback_replace_keeps_old_cooldown(self):
        self.new_db()
        old = NOW + _dt.timedelta(hours=5)
        self.set_whole(old)
        self._block_audit("cooldown_replace")
        code, _, err = self.call(until=to_rfc3339(NOW + _dt.timedelta(hours=1)))
        self.assertEqual(code, 1)
        self.assertIn("已回滾", err)
        self.assertEqual(self.whole(), to_rfc3339(old))  # 冷卻列未改
        self.assertEqual(self.audit_rows(), [])

    def test_c1040_rollback_clear_keeps_row(self):
        self.new_db()
        old = NOW + _dt.timedelta(hours=5)
        self.set_model(old)
        self._block_audit("cooldown_clear")
        code, _, _ = self.call(model=MODEL)
        self.assertEqual(code, 1)
        self.assertEqual(self.model_row(), to_rfc3339(old))
        self.assertEqual(self.audit_rows(), [])

    def test_c1040_audit_missing_new_columns_refused_without_change(self):
        # 舊 DB（沒有 cooldown_mode／prev_until／os_user）：拒絕寫，列不動。
        conn = sqlite3.connect(self.db)
        conn.executescript(V5_AUDIT)
        conn.execute("INSERT INTO cooldowns (subscription, until) VALUES (?, ?)",
                     (SUB, to_rfc3339(NOW + _dt.timedelta(hours=1))))
        conn.commit()
        conn.close()
        code, _, err = self.call()
        self.assertEqual(code, 1)
        self.assertIn("缺欄", err)
        self.assertIsNotNone(self.whole())

    def test_c1040_db_missing_is_error_and_not_created(self):
        code, _, err = self.call()
        self.assertEqual(code, 1)
        self.assertIn("找不到", err)
        self.assertFalse(os.path.exists(self.db))


class ModelValidationTests(_Base):
    """C-1040 的 `--model` 驗證：與 lgwd/registry.py `_cooldown_model` 逐案對照。"""

    CASES = (
        ("", False),
        ("big-pickle", True),
        ("big-pickle-x", False),  # 不在 models.allow 內
        ("opencode/big-pickle", False),  # 帶 <agent>/ 前綴
        ("claude-code/big-pickle", False),
        ("*-free", False),  # 萬用字元
        ("big-pickle?", False),
        ("big[pickle", False),
        ("x" * 201, False),  # 超過 200 字元
        ("nothing-here", False),
    )

    def _registry_accepts(self, model):
        try:
            return RegistryService._cooldown_model(None, POLICY.subscriptions[SUB], model) == model
        except Exception:  # bad_request 等 ApiError：拒絕
            return False

    def _clear_accepts(self, model):
        try:
            cooldown_clear.validate_model(POLICY.subscriptions[SUB], model)
            return True
        except cooldown_clear.CooldownClearError:
            return False

    def test_c1040_model_rule_matches_registry(self):
        for model, expected in self.CASES:
            with self.subTest(model=model[:20]):
                self.assertEqual(self._clear_accepts(model), expected)
                self.assertEqual(self._registry_accepts(model), expected)

    def test_c1040_model_none_is_whole_sub_not_validated(self):
        self.assertIsNone(RegistryService._cooldown_model(None, POLICY.subscriptions[SUB], None))

    def test_c1040_model_invalid_refused_by_cli_without_change(self):
        self.new_db()
        self.set_model(NOW + _dt.timedelta(hours=1))
        code, _, err = self.call(model="*-free")
        self.assertEqual(code, 2)
        self.assertIn("萬用字元", err)
        self.assertIsNotNone(self.model_row())
        self.assertEqual(self.audit_rows(), [])


class EntryPointTests(_Base):
    def test_c1040_cli_main_dispatches_clear(self):
        self.new_db()
        self.set_whole(NOW + _dt.timedelta(days=1))
        env = {"LLMGW_PREFIX": self.prefix, "SUDO_USER": "tester"}
        self.assertEqual(main(["cooldown-clear", SUB], env=env), 0)
        self.assertIsNone(self.whole())

    def test_c1040_cli_help_lists_subcommand(self):
        proc = subprocess.run([sys.executable, "-m", "lgwctl", "cooldown-clear", "--help"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--until", proc.stdout)

    def test_c1040_os_user_falls_back_to_euid_name(self):
        layout = Layout.from_env({"LLMGW_PREFIX": self.prefix})
        try:
            expected = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            expected = str(os.geteuid())
        self.assertEqual(cooldown_clear._os_user(layout), expected)


if __name__ == "__main__":
    unittest.main()
