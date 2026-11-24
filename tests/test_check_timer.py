"""#289：`llmgwctl check` 的定時執行（timer）。

- C-980／C-981：兩個新 unit 的靜態解析（依段落；各鍵恰好一次、「沒有」的鍵真的沒有）；
- C-982：`LLMGW_CHECK_JOURNAL=1` 時每行加 syslog 優先級前綴，沒設時逐 byte 不變；
- C-983：install 在服務啟動**之後**對 timer 做 `enable --now`，重跑不重複；
- C-984：upgrade 成功時 enable；失敗回復時先 `disable --now` 再刪檔；
- C-985：rollback 到沒有 timer 的版本 → `disable --now`、刪檔、`daemon-reload`；
- C-986：`check-timer` 的 ok／todo（未安裝、systemctl 不能用）；
- C-987：既有的兩個 unit 逐 byte 不變。

全部在 `tests/lgwctl_fakes.Sandbox` 的假 systemctl 沙箱裡跑，不碰真的 systemd。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from .lgwctl_fakes import (
        CHECK_NOTIFY,
        CHECK_SERVICE,
        CHECK_TIMER,
        FAKE_KEY_VALUE,
        FAKE_TAG,
        NEW_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )
    from .test_lgwctl_install import _UnitFields
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        CHECK_NOTIFY,
        CHECK_SERVICE,
        CHECK_TIMER,
        FAKE_KEY_VALUE,
        FAKE_TAG,
        NEW_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )
    from test_lgwctl_install import _UnitFields

from lgwctl import units as unitsmod
from lgwctl.layout import Layout
from lgwctl.report import Report

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT_DIR = os.path.join(REPO_ROOT, "deploy", "units")

#: C-987：既有兩個 unit 的內容雜湊（#289 這一張單不得改動它們）。
#: #306（D5d 段二）依契約 C-835～C-837 在 llmgateway.service 加了資源上限，
#: 所以服務 unit 的雜湊跟著更新；tunnel unit 不變，逐 byte 的斷言照舊。
LLMGATEWAY_SERVICE_SHA256 = "5019d7331ede120771de5977ec188463869c3f14f095a7e47dd2e70813fb0d4f"
LLMGATEWAY_TUNNEL_SHA256 = "f84b664fb6521c91dfb634a26f32637cf7729cd6554708e74644c17667d068bc"


def read_unit(name: str) -> str:
    with open(os.path.join(UNIT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def parsed_unit(name: str) -> _UnitFields:
    return _UnitFields(read_unit(name))


class CheckServiceUnitTests(unittest.TestCase):
    """C-980：`llmgateway-check.service` 的固定值。"""

    def setUp(self):
        self.text = read_unit(CHECK_SERVICE)
        self.unit = parsed_unit(CHECK_SERVICE)

    def test_c980_service_keys(self):
        expected = {
            "Type": "oneshot",
            "User": "root",
            "ExecStart": "/usr/local/bin/llmgwctl check",
            "SuccessExitStatus": "2",
            "SyslogIdentifier": "llmgwctl-check",
            "Environment": "LLMGW_CHECK_JOURNAL=1",
            "TimeoutStartSec": "5min",
            "ReadWritePaths": "/var/lib/llmgateway",
            "NoNewPrivileges": "true",
            "ProtectSystem": "strict",
            "PrivateTmp": "true",
            "ProtectHome": "read-only",
        }
        for key, value in expected.items():
            self.assertEqual(self.unit.values("Service", key), [value], key)

    def test_c980_each_key_appears_exactly_once(self):
        for key in ("Type", "User", "ExecStart", "SuccessExitStatus",
                    "SyslogIdentifier", "Environment", "TimeoutStartSec",
                    "ReadWritePaths", "NoNewPrivileges", "ProtectSystem",
                    "PrivateTmp", "ProtectHome"):
            self.assertEqual(len(self.unit.every(key)), 1, key)

    def test_c980_absent_keys_really_absent(self):
        sections = {section for (section, _key, _value, _line) in self.unit.entries}
        self.assertNotIn("Install", sections, "service 沒有 [Install]（由 timer 啟動）")
        for key in ("KillMode", "Restart"):
            self.assertEqual(self.unit.every(key), [], key)

    def test_c980_execstart_has_no_new_flags(self):
        for needle in ("--no-remote", "--tag", "--journal", "--syslog", "--timer"):
            self.assertNotIn(needle, self.text, needle)


class CheckTimerUnitTests(unittest.TestCase):
    """C-981：`llmgateway-check.timer` 的固定值。"""

    def setUp(self):
        self.text = read_unit(CHECK_TIMER)
        self.unit = parsed_unit(CHECK_TIMER)

    def test_c981_timer_keys(self):
        self.assertEqual(self.unit.values("Timer", "Unit"), ["llmgateway-check.service"])
        self.assertEqual(self.unit.values("Timer", "OnBootSec"), ["5min"])
        self.assertEqual(self.unit.values("Timer", "OnUnitActiveSec"), ["15min"])
        self.assertEqual(self.unit.values("Timer", "RandomizedDelaySec"), ["60"])
        self.assertEqual(self.unit.values("Timer", "AccuracySec"), ["1min"])
        self.assertEqual(self.unit.values("Install", "WantedBy"), ["timers.target"])
        for key in ("Unit", "OnBootSec", "OnUnitActiveSec", "RandomizedDelaySec",
                    "AccuracySec"):
            self.assertEqual(len(self.unit.every(key)), 1, key)

    def test_c981_no_persistent(self):
        # Persistent= 只對 OnCalendar= 有效；開機後的第一次由 OnBootSec 負責。
        self.assertEqual(self.unit.every("Persistent"), [])
        self.assertEqual(self.unit.every("OnCalendar"), [])

    def test_c981_timer_has_no_service_section(self):
        sections = {section for (section, _key, _value, _line) in self.unit.entries}
        self.assertEqual(sections, {"Unit", "Timer", "Install"})


class ExistingUnitsUnchangedTests(unittest.TestCase):
    """C-987：`llmgateway.service` 與 `llmgateway-tunnel.service` 逐 byte 不變。

    #306（D5d 段二）是例外：服務 unit 依契約 C-835～C-837 加了三個資源上限，
    上面 pin 的雜湊跟著更新；tunnel unit 仍逐 byte 比對。
    """

    def test_existing_units_are_unchanged(self):
        self.assertEqual(
            hashlib.sha256(read_unit("llmgateway.service").encode()).hexdigest(),
            LLMGATEWAY_SERVICE_SHA256)
        self.assertEqual(
            hashlib.sha256(read_unit("llmgateway-tunnel.service").encode()).hexdigest(),
            LLMGATEWAY_TUNNEL_SHA256)


class EnabledStateTests(unittest.TestCase):
    """#289（架構師低-3）：is-enabled 的結束碼語意不可靠時看輸出文字。"""

    def setUp(self):
        self.layout = Layout.from_env({"LLMGW_PREFIX": "/tmp/lgw-289-enabled"})

    def state_for(self, returncode, stdout="", stderr=""):
        proc = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch.object(unitsmod, "run", return_value=proc):
            return unitsmod.enabled_state(self.layout, CHECK_TIMER)

    def test_rc0_is_enabled(self):
        self.assertEqual(self.state_for(0), "enabled")

    def test_rc1_plain_is_disabled(self):
        self.assertEqual(self.state_for(1), "disabled")

    def test_rc1_with_not_found_text_is_missing(self):
        """有些版本對不存在的 unit 回 1；不能一律當成 disabled。"""
        self.assertEqual(
            self.state_for(1, stderr="Failed to get unit file state for "
                                    "llmgateway-check.timer: No such file or directory"),
            "missing")

    def test_rc4_is_missing(self):
        self.assertEqual(self.state_for(4), "missing")

    def test_other_rc_is_unknown(self):
        self.assertEqual(self.state_for(3, stderr="Failed to connect to bus"), "unknown")


class JournalPrefixTests(unittest.TestCase):
    """C-982：前綴只跟著等級走；沒設時輸出逐 byte 不變。"""

    def test_c982_prefixes_by_level(self):
        lines = []
        report = Report(out=lines.append, journal=True)
        report.ok("a")
        report.todo("b", "x")
        report.fail("c")
        self.assertEqual(lines, ["<6>ok a", "<4>todo b（x）", "<3>fail c"])
        # 收集到的原始行不含前綴（三態判斷與測試都用它）
        self.assertEqual(report.lines, ["ok a", "todo b（x）", "fail c"])

    def test_c982_without_journal_is_byte_identical(self):
        lines = []
        report = Report(out=lines.append)
        report.ok("a")
        report.todo("b", "x")
        report.fail("c")
        self.assertEqual(lines, ["ok a", "todo b（x）", "fail c"])
        self.assertFalse(any(line.startswith("<") for line in lines))

    def test_c982_exit_code_does_not_change(self):
        for journal in (False, True):
            report = Report(out=lambda line: None, journal=journal)
            report.ok("a")
            self.assertEqual(report.exit_code(), 0)
            report.todo("b")
            self.assertEqual(report.exit_code(), 2)
            report.fail("c")
            self.assertEqual(report.exit_code(), 1)


class TimerFixture(unittest.TestCase):
    """共同沙箱：裝好 FAKE_TAG、服務與隧道在跑、keysync 狀態 ok。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.box.write(self.box.state(".keysync.status"),
                       json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}),
                       0o644)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")

    def check(self, *argv, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        return self.box.run_cli(["check", *argv], env=env)


class CheckJournalCliTests(TimerFixture):
    """C-982：真的從 CLI 跑 check 時的行為。"""

    def test_c982_env_var_prefixes_every_line_and_keeps_the_exit_code(self):
        rc_plain, out_plain, err_plain = self.check("--no-remote")
        self.assertEqual(rc_plain, 0, out_plain + err_plain)
        self.assertFalse(any(line.startswith("<") for line in out_plain.splitlines()),
                         out_plain)

        rc_journal, out_journal, err_journal = self.check(
            "--no-remote", LLMGW_CHECK_JOURNAL="1")
        self.assertEqual(rc_journal, rc_plain)
        self.assertTrue(out_journal.splitlines(), out_journal)
        for line in out_journal.splitlines():
            match = re.match(r"^<([346])>(ok|todo|fail) ", line)
            self.assertIsNotNone(match, line)
            self.assertEqual(match.group(1), {"fail": "3", "todo": "4", "ok": "6"}[
                match.group(2)], line)


class CheckTimerItemTests(TimerFixture):
    """C-986：`check-timer` 的三態。"""

    def setUp(self):
        super().setUp()
        self.box.set_unit_enabled(CHECK_TIMER)
        self.box.set_active(CHECK_TIMER)

    def test_c986_enabled_and_active_is_ok(self):
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "check-timer", "ok"), out)

    def test_c986_not_enabled_is_todo_with_the_install_hint(self):
        # unit 檔不在（例如從不認得 timer 的舊版升級上來的機器）：講「未安裝」。
        self.assertFalse(os.path.exists(self.box.unit(CHECK_TIMER)))
        self.box.set_unit_enabled(CHECK_TIMER, False)
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "check-timer", "todo"), out)
        self.assertIn("未安裝", out)
        self.assertIn("llmgwctl install", out)

    def test_c986_installed_but_disabled_says_installed(self):
        """agy 低-2：unit 檔在、只是沒 enable → 不能說「未安裝」。"""
        self.box.write(self.box.unit(CHECK_TIMER), f"[Unit]\n# {CHECK_TIMER}\n")
        self.box.set_unit_enabled(CHECK_TIMER, False)
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 2, out + err)
        line = next(line for line in out.splitlines() if "check-timer" in line)
        self.assertIn("已安裝但未啟用", line)
        self.assertNotIn("未安裝", line)
        self.assertIn("systemctl enable --now " + CHECK_TIMER, line)

    def test_c986_not_active_is_todo(self):
        # 已安裝、enabled，但被 stop 了（維護期間）：講「沒在跑」，給 start。
        self.box.write(self.box.unit(CHECK_TIMER), f"[Unit]\n# {CHECK_TIMER}\n")
        self.box.set_active(CHECK_TIMER, False)
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 2, out + err)
        line = next(line for line in out.splitlines() if "check-timer" in line)
        self.assertIn("已安裝但沒在跑", line)
        self.assertIn("systemctl start " + CHECK_TIMER, line)

    def test_c986_systemctl_unavailable_is_todo(self):
        missing = os.path.join(self.box.root, "no-systemctl")
        rc, out, err = self.check("--no-remote", LLMGW_SYSTEMCTL=missing)
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "check-timer", "todo"), out)
        self.assertIn("systemctl 不能用", out)

    def test_c986_older_version_without_the_unit_does_not_list_it(self):
        """這一版根本沒有 timer（舊 tag）：不列 check-timer，也不能因此變非 0。"""
        os.unlink(os.path.join(self.box.opt(FAKE_TAG), "deploy", "units", CHECK_TIMER))
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(has_line(out, "check-timer"), out)

    # -- #296（C-996）：這一版有 notify unit 檔、系統上卻沒有 -----------------

    def test_c296_timer_ok_but_notify_unit_missing_is_todo(self):
        """從 #300 的版本升級上來：跑升級的是舊版 lgwctl，不會裝 notify unit。

        timer 在跑也要提醒補跑 install（systemd 會在 check 失敗時記找不到 unit）。
        """
        self.box.write(os.path.join(self.box.opt(FAKE_TAG), "deploy", "units",
                                    CHECK_NOTIFY), f"[Unit]\n# {CHECK_NOTIFY}\n")
        self.assertFalse(os.path.exists(self.box.unit(CHECK_NOTIFY)))
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 2, out + err)
        line = next(line for line in out.splitlines() if " check-timer" in line)
        self.assertIn(CHECK_NOTIFY, line)
        self.assertIn("未安裝", line)
        self.assertIn("llmgwctl install", line)

    def test_c296_timer_ok_and_notify_unit_installed_is_ok(self):
        self.box.write(os.path.join(self.box.opt(FAKE_TAG), "deploy", "units",
                                    CHECK_NOTIFY), f"[Unit]\n# {CHECK_NOTIFY}\n")
        self.box.write(self.box.unit(CHECK_NOTIFY), f"[Unit]\n# {CHECK_NOTIFY}\n")
        rc, out, err = self.check("--no-remote")
        # check-notify 另有自己的 todo（沙箱沒放 check-notify.env），這一項是 ok。
        self.assertTrue(has_line(out, "check-timer", "ok"), out)
        self.assertIn(rc, (0, 2), out + err)

    def test_c296_version_without_notify_unit_does_not_check_it(self):
        """這一版沒有 notify unit（#300 及更舊）：只看 timer，不因它變 todo。"""
        rc, out, err = self.check("--no-remote")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "check-timer", "ok"), out)


class InstallTimerTests(unittest.TestCase):
    """C-983：install 在服務啟動之後 enable --now timer，而且冪等。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        # 模擬「還沒裝 timer 的機器」：版本裡有 unit，但系統上沒 enable／active。
        self.box.set_unit_enabled(CHECK_TIMER, False)
        self.box.set_active(CHECK_TIMER, False)

    def install(self, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        return self.box.run_cli(
            ["install", "--tag", FAKE_TAG, "--repo", "/fake/repo"], env=env)

    def test_c983_timer_is_enabled_after_the_service_starts(self):
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        actions = self.box.actions()
        enabled = "enable --now " + CHECK_TIMER
        self.assertIn(enabled, actions)
        self.assertEqual(actions.count(enabled), 1)
        self.assertIn("start llmgateway.service", actions)
        self.assertGreater(actions.index(enabled),
                           actions.index("start llmgateway.service"),
                           "timer 必須在服務啟動之後才 enable --now")
        self.assertTrue(has_line(out, "enable-timer", "ok"), out)
        self.assertTrue(os.path.exists(self.box.unit(CHECK_TIMER)))

    def test_c983_reinstall_does_not_touch_running_timer(self):
        self.assertEqual(self.install()[0], 0)
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.actions().count("enable --now " + CHECK_TIMER), 1)
        self.assertTrue(has_line(out, "enable-timer", "ok"), out)

    def test_c983_version_without_timer_does_not_enable_it(self):
        """版本裡沒有 timer（例如舊版）就不 enable；不能對不存在的 unit 叫 systemctl。"""
        rc, out, err = self.install(FAKE_GIT_SKIP="deploy/units/" + CHECK_TIMER)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("enable --now " + CHECK_TIMER, self.box.actions())
        self.assertFalse(has_line(out, "enable-timer", "ok"), out)


class UpgradeTimerTests(unittest.TestCase):
    """C-984：upgrade 成功 enable；失敗回復先 disable 再刪檔。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.box.write(self.box.state(".keysync.status"),
                       json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}),
                       0o644)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        self.box.write(self.box.unit("llmgateway.service"),
                       f"[Unit]\n# llmgateway {FAKE_TAG}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {FAKE_TAG}\n")
        # 升級前：timer 還沒 enable（模擬從不認得 timer 的版本升上來）。
        self.box.set_unit_enabled(CHECK_TIMER, False)
        self.box.set_active(CHECK_TIMER, False)

    def upgrade(self):
        return self.box.run_cli(["upgrade", "--tag", NEW_TAG])

    def test_c984_success_enables_the_new_timer(self):
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("enable --now " + CHECK_TIMER, self.box.actions())
        self.assertTrue(has_line(out, "enable-timer", "ok"), out)
        self.assertTrue(os.path.exists(self.box.unit(CHECK_TIMER)))

    def test_c984_timer_is_enabled_after_restart(self):
        """架構師低-2：timer 在 restart（與探針）之後才 enable；順序可斷言。"""
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 0, out + err)
        actions = self.box.actions()
        self.assertGreater(actions.index("enable --now " + CHECK_TIMER),
                           actions.index("restart llmgateway.service"))

    def test_c984_probe_failure_never_enables_the_timer(self):
        """探針失敗＝沒有「健康」可言；enable 必須在探針 ok 之後，所以不能發生。"""
        self.box.curl_codes(["500"])
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("enable --now " + CHECK_TIMER, self.box.actions())

    def test_c984_failed_upgrade_disables_then_removes_the_timer(self):
        self.box.set_start_fail("llmgateway.service")
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        actions = self.box.actions()
        disabled = "disable --now " + CHECK_TIMER
        self.assertIn(disabled, actions)
        # 刪檔之後才 daemon-reload（回滾的最後一個 daemon-reload 在 disable 之後）。
        last_reload = len(actions) - 1 - actions[::-1].index("daemon-reload daemon-reload")
        self.assertGreater(last_reload, actions.index(disabled), actions)
        self.assertFalse(os.path.exists(self.box.unit(CHECK_TIMER)),
                         "回復後不該留下新版裝的 timer 檔")


class RollbackTimerTests(unittest.TestCase):
    """C-985：rollback 到沒有 timer 的版本要移除；目標版本有的就保留。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.box.write(self.box.state(".keysync.status"),
                       json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}),
                       0o644)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")

    def _install_current_units(self, tag: str) -> None:
        self.box.write(self.box.unit("llmgateway.service"),
                       f"[Unit]\n# llmgateway {tag}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {tag}\n")

    def test_c985_rollback_to_a_version_without_timer_removes_it(self):
        new_dir = self.box.make_release(NEW_TAG)                      # 有 timer
        old_dir = self.box.make_release(FAKE_TAG, check_units=False)  # 沒有 timer
        self.box.symlink(new_dir, self.box.current_link)
        self.box.symlink(old_dir, self.box.previous_link)
        self._install_current_units(NEW_TAG)
        self.box.write(self.box.unit(CHECK_SERVICE), f"[Unit]\n# {NEW_TAG}\n")
        self.box.write(self.box.unit(CHECK_TIMER), f"[Unit]\n# {NEW_TAG}\n")
        self.box.set_unit_enabled(CHECK_TIMER)
        self.box.set_active(CHECK_TIMER)

        rc, out, err = self.box.run_cli(["rollback"])
        self.assertNotIn("Traceback", out + err)
        actions = self.box.actions()
        disabled = "disable --now " + CHECK_TIMER
        self.assertIn(disabled, actions, out + err)
        # agy 低-1：timer 先停，再停 service（避免 timer 在 service 刪掉後觸發）。
        self.assertLess(actions.index(disabled),
                        actions.index("disable --now " + CHECK_SERVICE), actions)
        last_reload = len(actions) - 1 - actions[::-1].index("daemon-reload daemon-reload")
        self.assertGreater(last_reload, actions.index(disabled), actions)
        self.assertFalse(os.path.exists(self.box.unit(CHECK_TIMER)))
        self.assertFalse(os.path.exists(self.box.unit(CHECK_SERVICE)))

    def test_c985_tunnel_unit_is_not_removed(self):
        """架構師低-1：移除範圍只限兩個 check unit；隧道 unit 不因為目標版本
        沒有它就被停掉、刪掉。"""
        new_dir = self.box.make_release(NEW_TAG)
        old_dir = self.box.make_release(FAKE_TAG, check_units=False)
        # 目標版本（previous）沒有隧道 unit；這是刻意造的邊界情況。
        os.unlink(os.path.join(old_dir, "deploy", "units", "llmgateway-tunnel.service"))
        self.box.symlink(new_dir, self.box.current_link)
        self.box.symlink(old_dir, self.box.previous_link)
        self._install_current_units(NEW_TAG)
        self.box.write(self.box.unit(CHECK_SERVICE), f"[Unit]\n# {NEW_TAG}\n")
        self.box.write(self.box.unit(CHECK_TIMER), f"[Unit]\n# {NEW_TAG}\n")
        self.box.set_unit_enabled(CHECK_TIMER)
        self.box.set_active(CHECK_TIMER)

        rc, out, err = self.box.run_cli(["rollback"])
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn("disable --now llmgateway-tunnel.service", self.box.actions())
        self.assertTrue(os.path.exists(self.box.unit("llmgateway-tunnel.service")))
        self.assertIn(NEW_TAG, self.box.read(self.box.unit("llmgateway-tunnel.service")))
        # 但 check unit 仍然照 C-985 移除。
        self.assertFalse(os.path.exists(self.box.unit(CHECK_TIMER)))

    def test_c985_rollback_enables_timer_after_restart_and_probe(self):
        """架構師低-2：rollback 的 enable --now 也要在 restart 與探針 ok 之後。"""
        old_dir = self.box.make_release(FAKE_TAG, check_units=False)
        new_dir = self.box.make_release(NEW_TAG)
        self.box.symlink(old_dir, self.box.current_link)
        self.box.symlink(new_dir, self.box.previous_link)
        self._install_current_units(FAKE_TAG)
        self.box.set_unit_enabled(CHECK_TIMER, False)
        self.box.set_active(CHECK_TIMER, False)

        rc, out, err = self.box.run_cli(["rollback"])
        self.assertNotIn("Traceback", out + err)
        actions = self.box.actions()
        enabled = "enable --now " + CHECK_TIMER
        self.assertIn(enabled, actions, out + err)
        self.assertGreater(actions.index(enabled),
                           actions.index("restart llmgateway.service"))

    def test_c985_rollback_to_a_version_with_timer_keeps_it(self):
        old_dir = self.box.make_release(FAKE_TAG, check_units=False)  # current 沒有
        new_dir = self.box.make_release(NEW_TAG)                      # previous 有
        self.box.symlink(old_dir, self.box.current_link)
        self.box.symlink(new_dir, self.box.previous_link)
        self._install_current_units(FAKE_TAG)
        self.box.set_unit_enabled(CHECK_TIMER)
        self.box.set_active(CHECK_TIMER)

        rc, out, err = self.box.run_cli(["rollback"])
        self.assertNotIn("Traceback", out + err)
        actions = self.box.actions()
        self.assertNotIn("disable --now " + CHECK_TIMER, actions)
        self.assertTrue(os.path.exists(self.box.unit(CHECK_TIMER)))
        # 裝的是目標版本自己的 unit 檔（不是舊的、也不是沒換）。
        self.assertEqual(self.box.read(self.box.unit(CHECK_TIMER)),
                         self.box.read(os.path.join(new_dir, "deploy", "units", CHECK_TIMER)))


if __name__ == "__main__":
    unittest.main()
