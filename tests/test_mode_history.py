"""#288 降級滿一小時告警的測試（契約 C-960～C-968）。

全在沙箱裡跑（假 systemctl／假 curl）；寫入路徑用 `layout.euid = 0` 模擬 root，
並把 `os.fchown` 換成 no-op（測試行程本身不是 root，不能真的 chown 0:0）。
非 root 的情境一律明確把 `layout.euid` 設成 1000，不依賴測試行程的身分。
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import unittest
from unittest import mock

from lgwctl import mode_history
from lgwctl.layout import Layout
from lgwctl.report import Report

try:
    from .lgwctl_fakes import FAKE_TAG, NEW_TAG, OLD_TAG, Sandbox
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import FAKE_TAG, NEW_TAG, OLD_TAG, Sandbox

_UNSET = object()


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        self.set_mode("enforce")
        self.box.write(self.box.state("keys.env"), "FAKE_KEY=1\n", 0o600)
        self.box.set_active("llmgateway.service")
        self.history_path = self.box.state(mode_history.HISTORY_NAME)
        self.audit_path = self.box.state(mode_history.AUDIT_NAME)

    def set_mode(self, mode):
        self.box.write(self.box.etc("llmgateway.env"),
                       "LGW_API_PORT=8480\nLGWD_AUTH_MODE={0}\n".format(mode))

    def layout(self, *, euid=0):
        layout = Layout.from_env(self.box.env)
        layout.euid = euid
        return layout

    def root(self):
        """模擬 root 寫入：`atomic.write_bytes` 的 owner=(0,0) 在測試裡是 no-op。"""
        return mock.patch("os.fchown")

    def record(self, *, source="restart", now, unit_since=_UNSET,
               active=None, euid=0):
        kwargs = {}
        if unit_since is not _UNSET:
            kwargs["unit_since"] = unit_since
        if active is not None:
            kwargs["active"] = active
        with self.root():
            return mode_history.record(self.layout(euid=euid), source=source,
                                       now=now, **kwargs)

    def history(self):
        return json.loads(self.box.read(self.history_path))

    def audit_records(self):
        try:
            text = self.box.read(self.audit_path)
        except OSError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def report_for(self, *, euid=1000):
        report = Report(out=lambda line: None)
        mode_history.check(self.layout(euid=euid), report)
        return report


class HistoryFileTests(HistoryBase):
    """C-960：位置、權限、格式、原子寫入；非 root 絕不寫。"""

    def test_c960_location_mode_and_exact_format(self):
        with mock.patch("os.fchown") as fchown:
            self.assertTrue(mode_history.record(
                self.layout(euid=0), source="restart", now=1000))
        self.assertEqual(os.path.dirname(self.history_path),
                         self.box.state())
        self.assertEqual(os.path.basename(self.history_path),
                         "auth-mode-history.json")
        self.assertEqual(stat.S_IMODE(os.stat(self.history_path).st_mode), 0o644)
        fchown.assert_called_with(mock.ANY, 0, 0)
        text = self.box.read(self.history_path)
        self.assertEqual(text.count("\n"), 1, "狀態檔是一行 JSON")
        data = json.loads(text)
        self.assertEqual(set(data), set(mode_history._HISTORY_KEYS))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["last_mode"], "enforce")
        self.assertEqual(data["last_observed"], 1000)
        self.assertEqual(data["enforce_first_seen"], 1000)
        self.assertEqual(data["enforce_last_seen"], 1000)
        self.assertIsNone(data["degraded_since"])
        self.assertEqual(data["source"], "restart")

    def test_c960_non_root_never_writes(self):
        layout = self.layout(euid=1000)
        with mock.patch("os.fchown") as fchown:
            self.assertFalse(mode_history.record(layout, source="restart", now=1000))
        self.assertFalse(os.path.exists(self.history_path))
        self.assertFalse(os.path.exists(self.box.state(mode_history.LOCK_NAME)))
        self.assertFalse(fchown.called)


class ObserveRuleTests(HistoryBase):
    """C-961：§1.3 的更新規則（純函式表格）。"""

    def test_enforce_first_and_later(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        self.assertEqual(history["enforce_first_seen"], 1000)
        self.assertEqual(history["enforce_last_seen"], 1000)
        later = mode_history.observe(history, "enforce", 2000)
        self.assertEqual(later["enforce_first_seen"], 1000)
        self.assertEqual(later["enforce_last_seen"], 2000)
        self.assertIsNone(later["degraded_since"])

    def test_warn_after_enforce_uses_unit_start(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        degraded = mode_history.observe(history, "warn", 1500, 1400)
        self.assertEqual(degraded["last_mode"], "warn")
        self.assertEqual(degraded["degraded_since"], 1400)

    def test_repeated_warn_keeps_degraded_since(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        first = mode_history.observe(history, "warn", 1500, 1400)
        again = mode_history.observe(first, "warn", 1600, 1500)
        self.assertEqual(again["degraded_since"], 1400)
        self.assertEqual(again["last_observed"], 1600)

    def test_warn_without_unit_since_uses_now(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        degraded = mode_history.observe(history, "warn", 1500, None)
        self.assertEqual(degraded["degraded_since"], 1500)

    def test_warn_before_restart_does_not_degrade(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        pending = mode_history.observe(history, "warn", 1500, 900)
        self.assertIsNone(pending["degraded_since"])
        self.assertEqual(pending["last_mode"], "warn")
        self.assertTrue(mode_history.pending_restart(pending, "warn", 900))

    def test_warn_without_enforce_only_updates_mode(self):
        warn = mode_history.observe(mode_history.empty_history(), "warn", 1000)
        self.assertEqual(warn["last_mode"], "warn")
        self.assertIsNone(warn["enforce_last_seen"])
        self.assertIsNone(warn["degraded_since"])

    def test_enforce_clears_degraded_since(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        degraded = mode_history.observe(history, "warn", 1500, 1400)
        back = mode_history.observe(degraded, "enforce", 2000)
        self.assertIsNone(back["degraded_since"])
        self.assertEqual(back["enforce_last_seen"], 2000)
        again = mode_history.observe(back, "warn", 2500, 2400)
        self.assertEqual(again["degraded_since"], 2400)

    def test_clock_regression_returns_unchanged(self):
        history = mode_history.observe(mode_history.empty_history(), "enforce", 1000)
        self.assertEqual(mode_history.observe(history, "warn", 999, 999), history)
        degraded = mode_history.observe(history, "warn", 1500, 1400)
        self.assertEqual(mode_history.observe(degraded, "warn", 1399, 1399), degraded)


class UnitSinceParseTests(unittest.TestCase):
    """C-968：只接受 `@<整數>`。"""

    def test_only_at_integer_is_accepted(self):
        cases = {
            "@1795000000\n": 1795000000,
            "@0": 0,
            "  @42  ": 42,
            "": None,
            "1795000000": None,
            "@": None,
            "@abc": None,
            "@12.5": None,
            "@-1": None,
            "@ 12": None,
            "Mon 2026-01-01 00:00:00 UTC": None,
            None: None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(mode_history.parse_unit_since(text), expected)


class WindowTests(HistoryBase):
    """W-7a～W-7l：每一個窗口。"""

    def test_w7a_degrade_restart_todo_then_fail_after_hour(self):
        self.assertTrue(self.record(now=1000))
        self.set_mode("warn")
        self.assertTrue(self.record(now=2000))
        data = self.history()
        self.assertEqual(data["last_mode"], "warn")
        self.assertEqual(data["degraded_since"], 2000)
        layout = self.layout(euid=1000)
        level, detail = mode_history.evaluate(layout, now=2000, unit_since=2000)
        self.assertEqual(level, "todo", detail)
        self.assertIn("回退等於恢復免認證，不是安全措施", detail)
        level, _ = mode_history.evaluate(layout, now=2000 + 3599, unit_since=2000)
        self.assertEqual(level, "todo")
        level, detail = mode_history.evaluate(layout, now=2000 + 3600, unit_since=2000)
        self.assertEqual(level, "fail", detail)
        self.assertIn("超過 1 小時", detail)

    def test_w7b_repeated_restarts_keep_degraded_since(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        for stamp in (2001, 2002, 2003):
            self.record(now=stamp)
        self.assertEqual(self.history()["degraded_since"], 2000)

    def test_w7c_mode_changed_but_not_restarted(self):
        self.record(now=1000)
        self.set_mode("warn")
        layout = self.layout(euid=1000)
        level, detail = mode_history.evaluate(layout, now=1500, unit_since=900)
        self.assertEqual(level, "todo")
        self.assertIn("尚未重啟", detail)
        # root 補記也不可以把 degraded_since 設起來
        self.record(now=1500, unit_since=900)
        self.assertIsNone(self.history()["degraded_since"])

    def test_w7d_bypass_llmgwctl_uses_systemd_start_time(self):
        self.record(now=1000)
        self.set_mode("off")
        self.box.set_active_enter_timestamp(1100)
        with self.root():
            self.assertTrue(mode_history.record(self.layout(euid=0),
                                                source="check", now=1200))
        self.assertEqual(self.history()["degraded_since"], 1100)
        level, _ = mode_history.evaluate(self.layout(euid=1000),
                                         now=1200, unit_since=1100)
        self.assertEqual(level, "todo")

    def test_w7e_back_to_enforce_resets_timer(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        self.set_mode("enforce")
        self.record(now=3000)
        self.assertIsNone(self.history()["degraded_since"])
        self.set_mode("warn")
        self.record(now=4000)
        self.assertEqual(self.history()["degraded_since"], 4000)

    def test_w7f_clock_backwards_and_forward(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        before = self.box.read(self.history_path)
        self.assertFalse(self.record(now=1999))
        self.assertEqual(self.box.read(self.history_path), before)
        level, detail = mode_history.evaluate(self.layout(euid=1000),
                                              now=1999, unit_since=2000)
        self.assertEqual(level, "todo")
        self.assertIn("時鐘倒退", detail)
        # 往前跳：照算（可能提早 fail）
        level, _ = mode_history.evaluate(self.layout(euid=1000),
                                         now=2000 + 7200, unit_since=2000)
        self.assertEqual(level, "fail")

    def test_w7g_delete_history_mid_degrade_keeps_audit(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        records = self.audit_records()
        self.assertTrue(records, "模式轉換要附加審計")
        self.assertEqual(records[-1]["command"], "auth-mode")
        self.assertEqual(records[-1]["old"]["mode"], "enforce")
        self.assertEqual(records[-1]["new"]["mode"], "warn")
        os.unlink(self.history_path)
        level, detail = mode_history.evaluate(self.layout(euid=1000),
                                              now=2000, unit_since=2000)
        self.assertEqual(level, "ok", detail)
        self.assertIn("未曾 enforce（目前 warn）", detail)
        self.assertTrue(self.audit_records(), "已知限制：轉換紀錄仍在審計檔")

    def test_w7h_corrupt_history_during_degrade_fails(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        self.box.write(self.history_path, "{not json", 0o644)
        level, detail = mode_history.evaluate(self.layout(euid=1000),
                                              now=2000, unit_since=2000)
        self.assertEqual(level, "fail")
        self.assertEqual(detail, mode_history.CORRUPT_DETAIL)
        # 非 root 的 check 也一樣 fail，不能 fail-open
        report = self.report_for(euid=1000)
        self.assertEqual(report.level_of("auth-degraded"), "fail")

    def test_w7i_non_root_check_reads_but_does_not_write(self):
        self.record(now=1000)
        mtime = os.stat(self.history_path).st_mtime_ns
        self.assertFalse(self.record(now=2000, euid=1000))
        self.assertEqual(os.stat(self.history_path).st_mtime_ns, mtime)
        report = self.report_for(euid=1000)
        self.assertEqual(report.level_of("auth-degraded"), "ok")
        self.assertEqual(os.stat(self.history_path).st_mtime_ns, mtime)

    def test_w7j_concurrent_writes_are_locked(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        state_dir, _, lock_path = mode_history.history_paths(self.layout())
        self.assertTrue(os.path.isfile(lock_path))
        holding = threading.Event()
        release = threading.Event()

        def holder():
            with mode_history.lock_history(state_dir):
                holding.set()
                release.wait(5)

        def writer():
            self.record(now=2001)
            done.set()

        done = threading.Event()
        first = threading.Thread(target=holder)
        second = threading.Thread(target=writer)
        first.start()
        self.assertTrue(holding.wait(5))
        second.start()
        self.assertFalse(done.wait(0.3), "有鎖時第二個寫入要等")
        release.set()
        first.join(5)
        second.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(mode_history.load(self.layout()).state, "ok")
        self.assertEqual(self.history()["degraded_since"], 2000)

    def test_w7k_warn_runtime_downgrade_without_enforce_is_ok(self):
        self.set_mode("warn")
        level, detail = mode_history.evaluate(self.layout(euid=1000),
                                              now=1000, unit_since=None)
        self.assertEqual(level, "ok")
        self.assertIn("未曾 enforce（目前 warn）", detail)

    def test_w7l_write_failure_only_warns_and_keeps_exit_code(self):
        os.chmod(self.box.state(), 0o555)
        self.addCleanup(os.chmod, self.box.state(), 0o755)
        with mock.patch("os.geteuid", return_value=0):
            rc, out, err = self.box.run_cli(["restart"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("警告", err)
        self.assertFalse(os.path.exists(self.history_path))


class RecordCallSiteTests(HistoryBase):
    """C-962：start／restart／upgrade／rollback 只在成功且探針 ok 後記錄。"""

    def run_cli_as_root(self, argv, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown"):
            return self.box.run_cli(argv, env=env)

    def prepare_upgrade(self):
        self.box.write(self.box.unit("llmgateway.service"),
                       f"[Unit]\n# llmgateway {FAKE_TAG}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {FAKE_TAG}\n")
        self.box.set_active("llmgateway-tunnel.service")

    def test_c962_restart_ok_records(self):
        rc, out, err = self.run_cli_as_root(["restart"])
        self.assertEqual(rc, 0, out + err)
        data = self.history()
        self.assertEqual(data["last_mode"], "enforce")
        self.assertEqual(data["source"], "restart")

    def test_c962_start_ok_records(self):
        self.box.set_active("llmgateway.service", False)
        rc, out, err = self.run_cli_as_root(["start"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isfile(self.history_path))

    def test_c962_failed_restart_does_not_record(self):
        self.box.set_start_fail("llmgateway.service")
        rc, out, err = self.run_cli_as_root(["restart"])
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(os.path.exists(self.history_path))

    def test_c962_record_runtime_error_does_not_break_restart(self):
        """低-1：record 丟出非預期例外時，restart 仍回 0、只印警告、不 traceback。"""
        with mock.patch("lgwctl.mode_history.record",
                        side_effect=RuntimeError("boom")):
            rc, out, err = self.box.run_cli(["restart"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("警告", err)
        self.assertNotIn("Traceback", err)
        self.assertIn("RuntimeError", err)

    def test_c962_upgrade_ok_records(self):
        self.prepare_upgrade()
        rc, out, err = self.run_cli_as_root(["upgrade", "--tag", NEW_TAG])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isfile(self.history_path))
        self.assertEqual(self.history()["source"], "restart")

    def test_c962_upgrade_probe_failure_does_not_record(self):
        self.prepare_upgrade()
        rc, out, err = self.run_cli_as_root(["upgrade", "--tag", NEW_TAG],
                                            FAKE_CURL_CODE="500")
        self.assertEqual(rc, 1, out + err)
        self.assertFalse(os.path.exists(self.history_path))

    def test_c962_rollback_ok_records(self):
        self.box.install_state(FAKE_TAG, previous=OLD_TAG)
        self.box.write(self.box.unit("llmgateway.service"),
                       f"[Unit]\n# llmgateway {FAKE_TAG}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {FAKE_TAG}\n")
        self.box.set_active_enter_timestamp(1000)
        rc, out, err = self.run_cli_as_root(["rollback"])
        self.assertTrue(os.path.isfile(self.history_path), out + err)


class CorruptionAndAuditTests(HistoryBase):
    """C-964／C-965：損毀與審計。"""

    def test_c964_corrupt_enforce_rebuilds_with_audit(self):
        self.box.write(self.history_path, '{"schema": 2}', 0o644)
        with self.root():
            self.assertTrue(mode_history.record(self.layout(euid=0),
                                                source="check", now=1000))
        data = self.history()
        self.assertEqual(data["last_mode"], "enforce")
        self.assertEqual(data["source"], "check")
        events = [record["args"]["event"] for record in self.audit_records()]
        self.assertIn("rebuild", events)
        level, detail = mode_history.evaluate(self.layout(euid=1000), now=1000)
        self.assertEqual((level, detail), ("ok", "enforce"))

    def test_c965_transition_adds_one_audit_record_only(self):
        self.record(now=1000)
        self.set_mode("warn")
        self.record(now=2000)
        self.record(now=2001)
        transitions = [record for record in self.audit_records()
                       if record["args"]["event"] == "transition"]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["command"], "auth-mode")
        self.assertEqual(transitions[0]["old"], {"mode": "enforce"})
        self.assertEqual(transitions[0]["new"], {"mode": "warn"})

    def test_c963_check_emits_the_item(self):
        base = int(time.time())
        self.record(now=base - 100)
        self.set_mode("warn")
        self.record(now=base - 50)
        report = self.report_for(euid=1000)
        self.assertEqual(report.level_of("auth-degraded"), "todo")
        line = [line for line in report.lines if "auth-degraded" in line][0]
        self.assertIn("回退等於恢復免認證，不是安全措施", line)

    def test_c963_check_enforce_is_ok(self):
        self.record(now=int(time.time()))
        report = self.report_for(euid=1000)
        self.assertEqual(report.level_of("auth-degraded"), "ok")

    def test_c963_unreadable_history_is_todo_need_root(self):
        self.record(now=1000)
        os.chmod(self.history_path, 0o000)
        self.addCleanup(os.chmod, self.history_path, 0o644)
        level, detail = mode_history.evaluate(self.layout(euid=1000), now=1000)
        self.assertEqual((level, detail), ("todo", "需要 root"))

    def test_c963_unknown_mode_is_todo(self):
        self.set_mode("bogus")
        level, detail = mode_history.check_for_report(self.layout(euid=1000))
        self.assertEqual(level, "todo")
        self.assertIn("讀不懂", detail)


if __name__ == "__main__":
    unittest.main()
