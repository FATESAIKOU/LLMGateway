"""#347 N13a §4.2、§4.3／C-1042：``llmgwctl audit-summary``。

- 時間參數嚴格格式（反例集）、Z／+00:00 正規化邊界、since>=until → 2。
- coverage 五案（complete_new／partial_new_and_legacy／none×3）；cooldown_transitions 只計 prev_until IS NULL。
- free_low_ratio 等比率的分母只含 role_resolves，零分母為 null；public_free_low_ratio 由交叉表算。
- usage_exhausted_observed 去重、首次觀測、截斷列不計；空日與舊 DB 缺欄輸出 unavailable 而非 0。
- 唯讀；舊 DB 相容；真實 CLI 入口（``python3 -m lgwctl``）。
- 假資料：暫存目錄裡的 llm.db（lgwd.store.Store 建真實 schema）。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lgwctl import audit_summary  # noqa: E402
from lgwctl.layout import Layout  # noqa: E402
from lgwd.store import Store  # noqa: E402

SINCE = "2026-12-04T00:00:00Z"
UNTIL = "2026-12-05T00:00:00Z"

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
        self.db = os.path.join(self._tmp.name, "llm.db")

    def tearDown(self):
        self._tmp.cleanup()

    def new_db(self):
        Store(self.db).close()  # 真實 schema（含 additive 遷移）

    def add(self, ts, action="resolve", sub="opencode-zen", **cols):
        names = ["ts", "subscription", "action"] + list(cols)
        values = [ts, sub, action] + list(cols.values())
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO audit ({0}) VALUES ({1})".format(
                ", ".join(names), ", ".join("?" for _ in names)), values)
            conn.commit()
        finally:
            conn.close()

    def summarize(self, since=SINCE, until=UNTIL):
        conn = audit_summary._open_ro(self.db)
        try:
            return audit_summary.summarize(conn, since, until)
        finally:
            conn.close()

    def run_cli(self, since=SINCE, until=UNTIL, db=None):
        out, err = io.StringIO(), io.StringIO()
        code = audit_summary.run(
            Layout.from_env({"LLMGW_PREFIX": self._tmp.name}),
            since=since, until=until, db=self.db if db is None else db, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class TimeFormatTests(_Base):
    """C-1042：嚴格格式反例集；全部結束 2 且 stdout 空。"""

    BAD = (
        "2026-12-04 00:00:00Z",      # 空白分隔
        "20261204T000000Z",          # 無分隔符
        "2026-12-04t00:00:00z",      # 小寫 t／z
        "2026-12-04T00:00:00-00:00", # -00:00
        "2026-12-04T00:00:00+0000",  # 無冒號時差
        "2026-12-04T00:00:00.000Z",  # 小數秒
        "2026-12-04T00:00:00+08:00", # 其他時差
        "2026-02-30T00:00:00Z",      # 無效日曆
        "2026-12-04T00:00:00Z\n",    # 結尾換行（$ 會吞，fullmatch 不吞）
        "2026-12-04T00:00:00",       # 無時區
        "２０２６-12-04T00:00:00Z",   # 全形數字
        "",
    )

    def test_c1042_strict_format_rejects(self):
        self.new_db()
        for bad in self.BAD:
            with self.subTest(bad=bad):
                code, out, err = self.run_cli(since=bad)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                code, out, _ = self.run_cli(until=bad)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")

    def test_c1042_since_not_before_until_is_2(self):
        self.new_db()
        self.assertEqual(self.run_cli(since=UNTIL, until=SINCE)[0], 2)
        self.assertEqual(self.run_cli(since=SINCE, until=SINCE)[0], 2)

    def test_c1042_z_and_offset_boundaries_normalize(self):
        self.new_db()
        self.add("2026-12-04T00:00:00Z", role="impl", tier="free", data="public")
        self.add("2026-12-04T01:00:00Z", role="impl", tier="free", data="public")
        code, out, _ = self.run_cli(since="2026-12-04T00:00:00+00:00",
                                    until="2026-12-04T01:00:00+00:00")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["since"], "2026-12-04T00:00:00Z")
        self.assertEqual(result["until"], "2026-12-04T01:00:00Z")
        self.assertEqual(result["total_resolves"], 1)  # 起點含、終點不含

    def test_c1042_mixed_boundary_equal_after_normalize_is_2(self):
        self.new_db()
        self.assertEqual(
            self.run_cli(since="2026-12-04T01:00:00Z", until="2026-12-04T01:00:00+00:00")[0], 2)


class CoverageTests(_Base):
    """C-1042：coverage 五案。"""

    def _cd(self, ts, mode, prev=None, sub="opencode-zen", action="cooldown"):
        self.add(ts, action=action, sub=sub, cooldown_mode=mode, prev_until=prev)

    def test_c1042_coverage_complete_new(self):
        self.new_db()
        self._cd("2026-12-04T02:00:00Z", "extend")
        self._cd("2026-12-04T03:00:00Z", "extend", prev="2026-12-04T09:00:00Z")
        ct = self.summarize()["cooldown_transitions"]
        self.assertTrue(ct["available"])
        self.assertEqual(ct["coverage"], "complete_new")
        self.assertEqual(ct["total"], 1)  # 只數 prev_until IS NULL
        self.assertEqual(ct["legacy_cooldown_rows"], 0)

    def test_c1042_coverage_partial_new_and_legacy(self):
        self.new_db()
        self._cd("2026-12-04T02:00:00Z", "extend")
        self._cd("2026-12-04T03:00:00Z", None)  # 舊列
        ct = self.summarize()["cooldown_transitions"]
        self.assertTrue(ct["available"])
        self.assertEqual(ct["coverage"], "partial_new_and_legacy")
        self.assertEqual(ct["legacy_cooldown_rows"], 1)
        self.assertEqual(ct["total"], 1)  # 舊列不計轉換

    def test_c1042_coverage_none_legacy_only(self):
        self.new_db()
        self._cd("2026-12-04T02:00:00Z", None)
        ct = self.summarize()["cooldown_transitions"]
        self.assertEqual(ct, {"available": False, "reason": "no_new_rows",
                              "coverage": "none", "legacy_cooldown_rows": 1})

    def test_c1042_coverage_none_only_clear_and_replace(self):
        self.new_db()
        self.add("2026-12-04T02:00:00Z", action="cooldown_clear", cooldown_mode="clear")
        self.add("2026-12-04T03:00:00Z", action="cooldown_replace", cooldown_mode="replace")
        ct = self.summarize()["cooldown_transitions"]
        self.assertEqual(ct["available"], False)
        self.assertEqual(ct["reason"], "no_new_rows")
        self.assertEqual(ct["coverage"], "none")
        self.assertEqual(ct["legacy_cooldown_rows"], 0)

    def test_c1042_coverage_none_empty_day(self):
        self.new_db()
        self.assertEqual(
            self.summarize()["cooldown_transitions"],
            {"available": False, "reason": "no_new_rows", "coverage": "none",
             "legacy_cooldown_rows": 0})

    def test_c1042_transitions_grouped_by_subscription_and_model(self):
        self.new_db()
        self.add("2026-12-04T02:00:00Z", action="cooldown", sub="a", model="m1",
                 cooldown_mode="extend", prev_until=None, until="2026-12-04T05:00:00Z")
        self.add("2026-12-04T04:00:00Z", action="cooldown", sub="a", model="m1",
                 cooldown_mode="extend", prev_until=None, until="2026-12-04T06:00:00Z")
        ct = self.summarize()["cooldown_transitions"]
        [group] = ct["by_subscription"]
        self.assertEqual((group["subscription"], group["model"], group["count"]), ("a", "m1", 2))
        self.assertEqual(len(group["events"]), 2)


class RatioTests(_Base):
    def test_c1042_free_low_ratio_role_only_and_public_from_cross_table(self):
        self.new_db()
        # public：free、normal、normal → public_free_low = 1/3（由交叉表算）
        self.add("2026-12-04T01:00:00Z", role="impl", tier="free", data="public")
        self.add("2026-12-04T01:01:00Z", role="impl", tier="normal", data="public")
        self.add("2026-12-04T01:02:00Z", role="impl", tier="normal", data="public")
        # general：free → 全體 free_low 分子 2、分母 4（role）
        self.add("2026-12-04T01:03:00Z", role="qa", tier="free", data="general")
        # 舊路徑（role NULL，無 tier）：不入任何比率、另列 legacy_resolves
        self.add("2026-12-04T01:04:00Z", role=None, tier=None, data=None)
        result = self.summarize()
        self.assertEqual(result["role_resolves"], 4)
        self.assertEqual(result["legacy_resolves"], 1)
        self.assertEqual(result["total_resolves"], 5)
        self.assertEqual(result["by_tier"], {"free": 2, "normal": 2})
        self.assertEqual(result["by_data"], {"general": 1, "public": 3})
        self.assertAlmostEqual(result["free_low_ratio"], 0.5)
        self.assertAlmostEqual(result["free_low_ratio_all"], 0.5)
        self.assertAlmostEqual(result["public_free_low_ratio"], 1 / 3)
        self.assertEqual(result["by_data_tier"]["public"], {"free": 1, "normal": 2})

    def test_c1042_zero_denominator_is_null(self):
        self.new_db()
        self.add("2026-12-04T01:00:00Z", role=None, tier=None, data=None)  # 只有舊路徑
        result = self.summarize()
        self.assertIsNone(result["free_low_ratio"])
        self.assertIsNone(result["free_low_ratio_all"])
        self.assertIsNone(result["public_free_low_ratio"])
        self.assertEqual(result["legacy_resolves"], 1)
        self.assertEqual(result["role_resolves"], 0)

    def test_c1042_table_versions_and_breakdown(self):
        self.new_db()
        self.add("2026-12-04T01:00:00Z", role="impl", tier="free", data="public",
                 table_sha="aaa", sub="s1", model="m1")
        self.add("2026-12-04T02:00:00Z", role="impl", tier="free", data="public",
                 table_sha="aaa", sub="s1", model="m1")
        self.add("2026-12-04T03:00:00Z", role="qa", tier="normal", data="real",
                 table_sha="bbb", sub="s2", model="m2")
        result = self.summarize()
        self.assertEqual(result["table_versions"], [
            {"table_sha": "aaa", "first_ts": "2026-12-04T01:00:00Z",
             "last_ts": "2026-12-04T02:00:00Z", "count": 2},
            {"table_sha": "bbb", "first_ts": "2026-12-04T03:00:00Z",
             "last_ts": "2026-12-04T03:00:00Z", "count": 1},
        ])
        self.assertEqual(result["breakdown"][0],
                         {"role": "impl", "subscription": "s1", "model": "m1",
                          "tier": "free", "count": 2})

    def test_c1042_ref_life_and_explicit_model(self):
        self.new_db()
        self.add("2026-12-04T01:00:00Z", ref="life1.a", explicit_model="1")
        self.add("2026-12-04T01:01:00Z", ref="life1.b")
        self.add("2026-12-04T01:02:00Z", ref="life2")
        result = self.summarize()
        self.assertEqual(result["distinct_ref"], 3)
        self.assertEqual(result["distinct_life"], 2)  # 無 . 的 ref 整串當一個 life
        self.assertEqual(result["resolves_per_life"], {"life1": 2})
        self.assertEqual(result["explicit_model_resolves"], 1)
        self.assertTrue(result["c2"] is None)
        self.assertEqual(result["schema"], 1)


class UsageExhaustedTests(_Base):
    def test_c1042_usage_exhausted_dedup_first_seen_and_truncated_excluded(self):
        self.new_db()
        exhausted = json.dumps([{"subscription": "s1", "reason": "usage_exhausted",
                                 "until": "2026-12-04T08:00:00Z"}])
        self.add("2026-12-04T01:00:00Z", role="impl", skipped=exhausted,
                 skipped_truncated="0")
        self.add("2026-12-04T02:00:00Z", role="impl", skipped=exhausted,
                 skipped_truncated="0")
        # 截斷列：不計觀測，但回報在 completeness
        self.add("2026-12-04T03:00:00Z", role="impl", skipped=exhausted,
                 skipped_truncated="1")
        # 同一格另一個 until（較晚的 reset）→ 另一筆
        later = json.dumps([{"subscription": "s1", "reason": "usage_exhausted",
                             "until": "2026-12-04T20:00:00Z"}])
        self.add("2026-12-04T04:00:00Z", role="impl", skipped=later, skipped_truncated="0")
        obs = self.summarize()["usage_exhausted_observed"]
        self.assertTrue(obs["available"])
        items = {item["until"]: item for item in obs["items"]}
        self.assertEqual(items["2026-12-04T08:00:00Z"]["first_seen_ts"], "2026-12-04T01:00:00Z")
        self.assertEqual(items["2026-12-04T08:00:00Z"]["seen_count"], 2)
        self.assertEqual(items["2026-12-04T20:00:00Z"]["seen_count"], 1)
        self.assertEqual(obs["completeness"]["skipped_truncated_resolves"], 1)
        self.assertEqual(obs["completeness"]["role_resolves_scanned"], 4)
        self.assertTrue(obs["completeness"]["observed_only"])

    def test_c1042_usage_exhausted_no_role_rows_is_no_new_rows(self):
        self.new_db()
        self.add("2026-12-04T01:00:00Z", role=None, skipped=None, skipped_truncated=None)
        self.assertEqual(self.summarize()["usage_exhausted_observed"],
                         {"available": False, "reason": "no_new_rows"})

    def test_c1042_usage_exhausted_empty_day(self):
        self.new_db()
        self.assertEqual(self.summarize()["usage_exhausted_observed"],
                         {"available": False, "reason": "no_new_rows"})


class OldDbTests(_Base):
    def _v5_db(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(V5_AUDIT)
        conn.execute("INSERT INTO audit (ts, subscription, action, agent) VALUES"
                     " ('2026-12-04T01:00:00Z', 'opencode-zen', 'resolve', 'opencode')")
        conn.execute("INSERT INTO audit (ts, subscription, action, until) VALUES"
                     " ('2026-12-04T02:00:00Z', 'opencode-zen', 'cooldown',"
                     " '2026-12-04T05:00:00Z')")
        conn.commit()
        conn.close()

    def test_c1042_old_db_unavailable_not_zero(self):
        self._v5_db()
        result = self.summarize()
        self.assertEqual(result["total_resolves"], 1)  # 基底欄位仍可算
        self.assertEqual(result["role_resolves"]["available"], False)
        self.assertEqual(result["role_resolves"]["reason"], "missing_columns")
        self.assertIn("role", result["role_resolves"]["missing_columns"])
        self.assertEqual(result["free_low_ratio"]["reason"], "missing_columns")
        self.assertEqual(result["cooldown_transitions"]["reason"], "missing_columns")
        self.assertEqual(result["cooldown_transitions"]["missing_columns"],
                         ["cooldown_mode", "prev_until"])
        self.assertEqual(result["usage_exhausted_observed"]["reason"], "missing_columns")
        self.assertEqual(result["table_versions"]["reason"], "missing_columns")

    def test_c1042_old_db_cli_exits_0(self):
        self._v5_db()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["total_resolves"], 1)


class ReadOnlyAndEntryTests(_Base):
    def test_c1042_connection_is_read_only(self):
        self.new_db()
        conn = audit_summary._open_ro(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM audit")
        finally:
            conn.close()

    def test_c1042_missing_db_is_1_and_not_created(self):
        code, out, err = self.run_cli(db=os.path.join(self._tmp.name, "nope.db"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "nope.db")))

    def test_c1042_cli_entry_via_python_m_lgwctl(self):
        self.new_db()
        cmd = [sys.executable, "-m", "lgwctl", "audit-summary",
               "--since", SINCE, "--until", UNTIL, "--db", self.db]
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["schema"], 1)
        bad = [sys.executable, "-m", "lgwctl", "audit-summary",
               "--since", "2026-12-04 00:00:00Z", "--until", UNTIL, "--db", self.db]
        proc = subprocess.run(bad, cwd=str(REPO_ROOT), capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
