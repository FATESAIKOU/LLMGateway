"""#296：`check` 失敗時通知 PMO（OnFailure ＋ aicomm、1 小時去重）。

契約 C 編號對測試名稱（也列在 REPORT-296-impl.md）：

- C-990（unit 的靜態內容）：`llmgateway-check.service` 的 `[Unit]` 恰好一行
  `OnFailure=llmgateway-check-notify.service`；notify unit 的每個鍵照契約 §1.2，
  而且沒有 `[Install]`、`OnFailure`、`Restart`（通知失敗不能再觸發通知）：
  `test_c990_check_service_has_exactly_one_onfailure`、
  `test_c990_check_service_still_treats_todo_as_success`、
  `test_c990_notify_unit_keys`、
  `test_c990_notify_unit_has_no_install_onfailure_or_restart`、
  `test_c990_notify_unit_runs_the_ops_script`。
- C-991（摘要的來源與過濾）：只取 fail 行的項目名、丟掉說明、忽略不合格的行、
  去重排序、最多 10 項 +`+N`、失敗類別白名單、訊息長度與字元過濾：
  `test_c991_extract_items_keeps_only_names`、
  `test_c991_extract_items_ignores_other_lines`、
  `test_c991_extract_items_dedupes_and_sorts`、
  `test_c991_classify_result_whitelist`、
  `test_c991_format_message_shape`、
  `test_c991_format_message_is_bounded_and_safe`、
  `test_c991_format_message_without_items`。
- C-992（去重）：`decide` 的一小時邊界、時鐘倒退、key 不同；`load_state` 不存在／
  壞 JSON／錯 schema／壞條目都當成空的；`prune` 的 7 天：
  `test_c992_decide_one_hour_boundary`、`test_c992_decide_clock_backwards_sends`、
  `test_c992_failure_key_covers_unit_result_and_items`、
  `test_c992_load_state_is_empty_on_any_problem`、`test_c992_load_state_valid`、
  `test_c992_prune_drops_older_than_seven_days`。
- C-993（腳本的行為：假的 journalctl／systemctl／aicomm）：`aicomm` 收到的 argv
  恰好是 `send --channel <ch> --to <to> --body <一行>`、body 不含說明／家目錄／
  位址／公鑰／身分檔路徑；不可用的三種情況 exit 0、journal 恰好一行；送失敗不寫
  狀態；去重與鎖：
  `test_c993_sends_one_whitelisted_line`、`test_c993_falls_back_to_systemctl`、
  `test_c993_bad_monitor_id_falls_back`、`test_c993_not_configured_exits_zero`、
  `test_c993_aicomm_missing_exits_zero`、`test_c993_send_failure_exits_zero_and_records_nothing`、
  `test_c993_second_failure_within_an_hour_is_suppressed`、
  `test_c993_sends_again_after_an_hour`、`test_c993_clock_backwards_sends`、
  `test_c993_corrupt_state_still_sends`、`test_c993_lock_busy_exits_zero`。
- C-994（狀態檔與鎖）：位置與名字、0600、原子寫（暫存檔名唯一，審查低-1）、
  7 天清理：`test_c994_save_state_is_0600_and_reloadable`、
  `test_c994_save_state_uses_a_unique_temp_file`、`test_c993_*`（狀態檔在去重路徑上）。
- C-995（install／upgrade／rollback／migrate）：notify unit 跟著裝、跟著移除、
  回滾到 #300 的版本時被移除、`rollback-to-user` 只 disable 不刪檔：
  `test_c995_install_installs_the_notify_unit`、
  `test_c995_upgrade_installs_the_notify_unit`、
  `test_c995_rollback_to_a_version_without_it_removes_it`、
  `test_c995_rollback_to_a_version_with_it_keeps_it`、
  `test_c995_migrate_only_disables_it`。
- C-996（`check` 的 `check-notify`）：只會 ok／todo；設定齊全＋aicomm 才 ok：
  `test_c996_ok_when_configured`、`test_c996_todo_without_the_env_file`、
  `test_c996_todo_when_a_key_is_missing`、`test_c996_todo_without_aicomm`、
  `test_c996_not_listed_for_older_versions`、`test_c996_never_fails`；
  遷移的停損門檻（通知沒設定不算新服務壞了）：
  `test_c996_benign_todo_only_is_accepted`、`test_c996_other_todos_still_stop_loss`、
  `test_c996_fail_is_never_accepted`、`test_c996_zero_stays_zero`、
  `test_c996_cutover_and_finalize_use_it`。
- C-997（文件與設定範本）：8.21 節、`check-notify` 項目名、設定範本的鍵與程式
  一致：
  `test_c997_section_documents_the_facts`、
  `test_c997_example_keys_match_program_and_doc`。
- C-998（修正第 1 輪，架構師低-1）：三個外部指令的逾時合計與整趟總預算都要留在
  unit 的 `TimeoutStartSec` 之內，而且 `main` 只建一個 `Budget` 傳給每一個指令：
  `test_c998_timeouts_fit_inside_the_unit_timeout`、
  `test_c998_budget_caps_each_call_by_the_remaining_time`、
  `test_c998_exhausted_budget_does_not_run_the_command`、
  `test_c998_main_passes_one_budget_to_every_command`。
- C-999（修正第 1 輪，架構師低-2）：`LGW_NOTIFY_LABEL` 只收
  `[A-Za-z0-9._-]{1,32}`，不合就當成沒設（一行 journal、不送）：
  `LabelFormatTests.test_c999_label_pattern_accepts_only_the_whitelist`、
  `test_c999_invalid_label_is_treated_as_unset`、
  `test_c999_length_boundaries`、`test_c999_check_item_reports_a_bad_label`、
  `test_c999_pattern_is_shared_with_the_docs_and_the_example`。
- #304（範本的 `place-holder-*` 算未設定）：check 的 `check-notify` 回 todo 並只寫
  鍵名、腳本走 `notify skipped: not configured` 不送；兩處規則由測試鎖住一致：
  `CheckNotifyItemTests.test_304_*`、`PlaceholderValueTests.test_304_*`、
  `PlaceholderRuleTests.test_304_*`、
  `DeployDocNotifyTests.test_304_docs_and_example_say_placeholder_is_unset`。

全部離線：腳本用假的 `journalctl`／`systemctl`／`aicomm`（暫存目錄，暫存 PATH），
lgwctl 用 `tests/lgwctl_fakes.Sandbox` 的假 systemctl；不讀憑證、不送真訊息、
不碰真的 systemd。
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    from .lgwctl_fakes import CHECK_NOTIFY, FAKE_TAG, NEW_TAG, Sandbox, has_line
    from .test_lgwctl_install import _UnitFields
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import CHECK_NOTIFY, FAKE_TAG, NEW_TAG, Sandbox, has_line
    from test_lgwctl_install import _UnitFields

from lgwctl import layout as layoutmod
from lgwctl import migrate
from lgwctl.check import _check_notify
from lgwctl.layout import CHECK_NOTIFY_LABEL_PATTERN, Layout
from lgwctl.report import Report

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = REPO_ROOT / "deploy" / "units"
SCRIPT = REPO_ROOT / "ops-scripts" / "llmgateway-check-notify"
EXAMPLE_ENV = REPO_ROOT / "deploy" / "check-notify.env.example"
CHECK_SERVICE = "llmgateway-check.service"

_SCRIPT_MODULE = None


def script_module():
    """把沒有 `.py` 副檔名的腳本載進來，直接測它的純函式。

    直接 `compile` 原始碼（不經 `SourceFileLoader`）：這樣不會寫也不會讀
    `__pycache__`，改壞驗證在副本裡改完再改回來時，不會拿到長度相同、mtime 同一秒
    的舊 bytecode。
    """
    global _SCRIPT_MODULE
    if _SCRIPT_MODULE is None:
        module = types.ModuleType("lgw_check_notify_296_under_test")
        module.__file__ = str(SCRIPT)
        code = compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")
        exec(code, module.__dict__)
        _SCRIPT_MODULE = module
    return _SCRIPT_MODULE


def read_unit(name: str) -> str:
    return (UNIT_DIR / name).read_text(encoding="utf-8")


def parsed_unit(name: str) -> _UnitFields:
    return _UnitFields(read_unit(name))


#: fail 行可能夾帶的東西（說明文字裡的）：家目錄、位址、公鑰、身分檔、帳號。
LEAKY_DETAIL = ("（/home/llmgw/.ssh/id_llmgw_tunnel ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 "
                "root@build 10.1.2.3 http://127.0.0.1:8480 "
                "/etc/llmgateway/keys.pass）")


class CheckNotifyUnitFileTests(unittest.TestCase):
    """C-990：兩個 unit 的靜態內容。"""

    def setUp(self):
        self.check = parsed_unit(CHECK_SERVICE)
        self.notify = parsed_unit(CHECK_NOTIFY)
        self.notify_text = read_unit(CHECK_NOTIFY)

    def test_c990_check_service_has_exactly_one_onfailure(self):
        self.assertEqual(self.check.every("OnFailure"),
                         [("Unit", "llmgateway-check-notify.service")])

    def test_c990_check_service_still_treats_todo_as_success(self):
        # 只有 todo（結束碼 2）不能算失敗，也就不會通知（變異：拿掉這一行）。
        self.assertEqual(self.check.values("Service", "SuccessExitStatus"), ["2"])

    def test_c990_notify_unit_keys(self):
        expected = {
            "Type": "oneshot",
            "User": "llmgw",
            "SupplementaryGroups": "systemd-journal",
            "EnvironmentFile": "-/etc/llmgateway/check-notify.env",
            "ExecStart": "/opt/llmgateway/current/ops-scripts/llmgateway-check-notify",
            "SyslogIdentifier": "llmgwctl-check-notify",
            "TimeoutStartSec": "60",
            "NoNewPrivileges": "true",
            "ProtectSystem": "strict",
            "ReadWritePaths": "/var/lib/llmgateway",
            "PrivateTmp": "true",
        }
        for key, value in expected.items():
            self.assertEqual(self.notify.values("Service", key), [value], key)
            self.assertEqual(len(self.notify.every(key)), 1, key)

    def test_c990_notify_unit_has_no_install_onfailure_or_restart(self):
        sections = {section for (section, _key, _value, _line) in self.notify.entries}
        self.assertEqual(sections, {"Unit", "Service"})
        for key in ("OnFailure", "Restart", "RestartSec", "WantedBy",
                    "SuccessExitStatus"):
            self.assertEqual(self.notify.every(key), [], key)
        # 也不要靠 KillMode／ExecStop 之類的東西重試。
        self.assertNotIn("WantedBy", self.notify_text)

    def test_c990_notify_unit_runs_the_ops_script(self):
        value = self.notify.values("Service", "ExecStart")[0]
        self.assertTrue(value.startswith("/opt/llmgateway/current/ops-scripts/"))
        script = REPO_ROOT / "ops-scripts" / os.path.basename(value)
        self.assertTrue(script.is_file(), script)
        self.assertTrue(os.access(script, os.X_OK), "腳本必須是可執行的")


class ExtractItemsTests(unittest.TestCase):
    """C-991：只取項目名，說明文字與不合格的行都不留。"""

    def setUp(self):
        self.module = script_module()

    def test_c991_extract_items_keeps_only_names(self):
        lines = ["fail auth-degraded" + LEAKY_DETAIL,
                 "fail list-local" + LEAKY_DETAIL]
        self.assertEqual(self.module.extract_items(lines),
                         ["auth-degraded", "list-local"])

    def test_c991_extract_items_ignores_other_lines(self):
        lines = ["ok auth-local", "todo keysync（x）", "fail Bad Name（x）",
                 "fail  （x）", " fail list-local", "fail-list-local",
                 "fail " + "a" * 40, "auth-degraded"]
        self.assertEqual(self.module.extract_items(lines), [])

    def test_c991_extract_items_dedupes_and_sorts(self):
        lines = ["fail zeta", "fail alpha", "fail zeta（dup）", "fail beta"]
        self.assertEqual(self.module.extract_items(lines),
                         ["alpha", "beta", "zeta"])

    def test_c991_classify_result_whitelist(self):
        for kind in ("exit-code", "timeout", "signal", "core-dump", "watchdog",
                     "oom-kill", "start-limit-hit", "resources"):
            self.assertEqual(self.module.classify_result(kind), kind)
        for junk in ("", None, "exit-code；rm -rf", "EXIT-CODE", "exit_code",
                     "other", 3):
            self.assertEqual(self.module.classify_result(junk), "other", repr(junk))

    def test_c991_format_message_shape(self):
        body = self.module.format_message(
            "gw-prod", "exit-code", ["auth-degraded", "list-local"],
            "2026-11-24T03:04:05Z")
        self.assertEqual(
            body,
            "llmgateway-check failed label=gw-prod result=exit-code "
            "items=auth-degraded,list-local at=2026-11-24T03:04:05Z")

    def test_c991_format_message_without_items(self):
        body = self.module.format_message("gw-prod", "timeout", [],
                                          "2026-11-24T03:04:05Z")
        self.assertIn(" result=timeout items=- at=", body)
        self.assertTrue(body.endswith("Z"))

    def test_c991_format_message_is_bounded_and_safe(self):
        items = ["item-" + str(index) + "-" + "a" * 20 for index in range(12)]
        label = "gw prod\n\t10.1.2.3 ünïcode " + "x" * 40
        body = self.module.format_message(label, "signal", items,
                                          "2026-11-24T03:04:05Z")
        self.assertLessEqual(len(body), 300)
        self.assertTrue(body.startswith("llmgateway-check failed label="))
        self.assertTrue(body.endswith(" at=2026-11-24T03:04:05Z"))
        # 白名單字元以外的一律換成 ?：沒有空白以外的控制字元、沒有公鑰、沒有路徑。
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                      "0123456789 ._:,=+-?")
        self.assertEqual({char for char in body if char not in allowed}, set())
        self.assertNotIn("\n", body)
        self.assertNotIn("\t", body)
        # 12 項放不下 → 用 +N 交代剩下的。
        self.assertRegex(body, r"items=[^ ]*\+[0-9]+")
        # label 限 32 字元。
        label_field = body.split("label=", 1)[1].split(" result=", 1)[0]
        self.assertLessEqual(len(label_field), 32)


class DedupeTests(unittest.TestCase):
    """C-992：去重的純函式。"""

    def setUp(self):
        self.module = script_module()
        self.key = "0123456789abcdef"

    def test_c992_decide_one_hour_boundary(self):
        decide = self.module.decide
        self.assertEqual(decide({}, self.key, 1000), "send")
        self.assertEqual(decide({self.key: 1000}, self.key, 1000), "suppress")
        self.assertEqual(decide({self.key: 1000}, self.key, 4599), "suppress")
        self.assertEqual(decide({self.key: 1000}, self.key, 4600), "send")
        self.assertEqual(decide({self.key: 1000}, self.key, 9999), "send")
        self.assertEqual(decide({"anotherkey00000": 1000}, self.key, 1001), "send")

    def test_c992_decide_clock_backwards_sends(self):
        # 時鐘倒退：不能因為「sent 在未來」被當成一小時內。
        self.assertEqual(self.module.decide({self.key: 2000}, self.key, 1000), "send")

    def test_c992_failure_key_covers_unit_result_and_items(self):
        key = self.module.failure_key
        base = key("llmgateway-check.service", "exit-code", ["a", "b"])
        self.assertRegex(base, r"^[0-9a-f]{16}$")
        self.assertEqual(base, key("llmgateway-check.service", "exit-code", ["b", "a"]))
        self.assertNotEqual(base, key("llmgateway-check.service", "timeout", ["a", "b"]))
        self.assertNotEqual(base, key("llmgateway-check.service", "exit-code", ["a"]))
        self.assertNotEqual(base, key("llmgateway-check.service", "exit-code", ["a", "b", "c"]))
        self.assertNotEqual(base, key("another.service", "exit-code", ["a", "b"]))
        # 不認得的 result 會先正規化成 other：與 other 同一把 key。
        self.assertEqual(key("u", "weird", ["a"]), key("u", "other", ["a"]))

    def _state(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "check-notify.state")
            if text is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            return self.module.load_state(path)

    def test_c992_load_state_is_empty_on_any_problem(self):
        self.assertEqual(self._state(None), {})
        self.assertEqual(self._state(""), {})
        self.assertEqual(self._state("not json"), {})
        self.assertEqual(self._state("[]"), {})
        self.assertEqual(self._state('{"schema": 2, "sent": {}}'), {})
        self.assertEqual(self._state('{"schema": 1, "sent": []}'), {})
        self.assertEqual(self._state('{"schema": 1, "sent": {"zz": 1}}'), {})
        self.assertEqual(
            self._state('{"schema": 1, "sent": {"' + self.key + '": true}}'), {})
        self.assertEqual(
            self._state('{"schema": 1, "sent": {"' + self.key + '": "1"}}'), {})

    def test_c992_load_state_valid(self):
        text = '{"schema": 1, "sent": {"' + self.key + '": 1000}}'
        self.assertEqual(self._state(text), {self.key: 1000})

    def test_c992_prune_drops_older_than_seven_days(self):
        week = 7 * 86400
        now = 10 * week
        state = {"old": now - week - 1, "fresh": now - week + 1,
                 "future": now + 5}
        self.assertEqual(self.module.prune(state, now),
                         {"fresh": now - week + 1, "future": now + 5})

    def test_c994_save_state_is_0600_and_reloadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "check-notify.state")
            self.module.save_state(path, {"b" * 16: 2, "a" * 16: 1})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(self.module.load_state(path),
                             {"a" * 16: 1, "b" * 16: 2})
            self.assertEqual(sorted(os.listdir(tmp)), ["check-notify.state"])

    def test_c994_save_state_uses_a_unique_temp_file(self):
        """審查低-1：暫存檔名不是固定的 `.tmp`，不會動到別人留下的同名檔。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "check-notify.state")
            stale = path + ".tmp"
            with open(stale, "w", encoding="utf-8") as handle:
                handle.write("keep me")
            self.module.save_state(path, {"a" * 16: 1})
            with open(stale, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep me")
            self.assertEqual(sorted(os.listdir(tmp)),
                             ["check-notify.state", "check-notify.state.tmp"])
            self.assertEqual(self.module.load_state(path), {"a" * 16: 1})


#: 假的外部指令（用 sys.executable 當 shebang，PATH 再怎麼設都跑得起來）。
FAKE_TOOL = """#!{python}
import json, os, sys
with open(os.environ["LGW_TEST_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(["{name}"] + sys.argv[1:], ensure_ascii=False) + "\\n")
{body}
"""

FAKE_JOURNALCTL_BODY = """
try:
    with open(os.environ["LGW_TEST_JOURNAL"], encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
except OSError:
    pass
"""

FAKE_SYSTEMCTL_BODY = """
sys.stdout.write(os.environ.get("LGW_TEST_INVOCATION", "") + "\\n")
"""

FAKE_AICOMM_BODY = """
sys.stdout.write("fake aicomm: sent (this must not reach the journal)\\n")
sys.stderr.write("fake aicomm: done\\n")
sys.exit(int(os.environ.get("LGW_TEST_AICOMM_RC", "0")))
"""


class ScriptFixture(unittest.TestCase):
    """腳本的離線沙箱：暫存 PATH（假的 journalctl／systemctl／aicomm）＋暫存狀態。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.fakebin = os.path.join(self.root, "fakebin")
        self.state_dir = os.path.join(self.root, "state")
        self.log = os.path.join(self.root, "tools.log")
        self.journal = os.path.join(self.root, "journal.txt")
        os.makedirs(self.fakebin)
        os.makedirs(self.state_dir)
        self.write_journal(["fail auth-degraded" + LEAKY_DETAIL,
                            "fail list-local（http://127.0.0.1:8480 500）",
                            "ok auth-local"])
        self.write_tool("journalctl", FAKE_JOURNALCTL_BODY)
        self.write_tool("systemctl", FAKE_SYSTEMCTL_BODY)
        self.write_tool("aicomm", FAKE_AICOMM_BODY)

    def write_tool(self, name: str, body: str) -> str:
        path = os.path.join(self.fakebin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_TOOL.format(python=sys.executable, name=name, body=body))
        os.chmod(path, 0o755)
        return path

    def write_journal(self, lines) -> None:
        with open(self.journal, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def env(self, **extra) -> dict:
        env = {
            "PATH": self.fakebin,
            "LGW_NOTIFY_CHANNEL": "team-test-w1",
            "LGW_NOTIFY_TO": "pmo-test",
            "LGW_NOTIFY_LABEL": "gw-test",
            "LGW_NOTIFY_STATE_DIR": self.state_dir,
            "MONITOR_SERVICE_RESULT": "exit-code",
            "MONITOR_INVOCATION_ID": "a" * 32,
            "LGW_TEST_LOG": self.log,
            "LGW_TEST_JOURNAL": self.journal,
            "LGW_TEST_INVOCATION": "b" * 32,
        }
        for name, value in extra.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run_script(self, **extra):
        proc = subprocess.run([sys.executable, str(SCRIPT)], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def calls(self, name: str) -> list:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        return [line for line in lines if line[0] == name]

    def aicomm_calls(self) -> list:
        """每次 ``aicomm`` 的參數（不含程式本身）。"""
        return [entry[1:] for entry in self.calls("aicomm")]

    def state_path(self) -> str:
        return os.path.join(self.state_dir, layoutmod.CHECK_NOTIFY_STATE_NAME)


class ScriptTests(ScriptFixture):
    """C-993：腳本實際跑起來的行為（假的 aicomm，不送任何真訊息）。"""

    def test_c993_sends_one_whitelisted_line(self):
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(out.splitlines(), ["notify sent"])
        calls = self.aicomm_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:6], ["send", "--channel", "team-test-w1",
                                    "--to", "pmo-test", "--body"])
        self.assertEqual(len(argv), 7, argv)
        body = argv[6]
        self.assertNotIn("\n", body)
        self.assertLessEqual(len(body), 300)
        self.assertTrue(body.startswith(
            "llmgateway-check failed label=gw-test result=exit-code items="))
        self.assertIn("items=auth-degraded,list-local", body)
        self.assertTrue(body.endswith("Z"))
        # 說明文字裡的東西一個都不能出現。
        for needle in ("/home", "llmgw", "ssh-ed25519", "AAAA", "root@",
                       "10.1.2.3", "127.0.0.1", "8480", "keys.pass", "500"):
            self.assertNotIn(needle, body, needle)
        # MONITOR_INVOCATION_ID 有值時不叫 systemctl。
        self.assertEqual(self.calls("systemctl"), [])

    def test_c993_falls_back_to_systemctl(self):
        rc, out, err = self.run_script(MONITOR_INVOCATION_ID=None)
        self.assertEqual(rc, 0, out + err)
        systemctl = self.calls("systemctl")
        self.assertEqual(len(systemctl), 1)
        self.assertEqual(systemctl[0][1:],
                         ["show", "-p", "InvocationID", "--value", CHECK_SERVICE])
        journalctl = self.calls("journalctl")
        self.assertEqual(len(journalctl), 1)
        self.assertEqual(journalctl[0][1:],
                         ["_SYSTEMD_INVOCATION_ID=" + "b" * 32,
                          "-t", "llmgwctl-check", "-p", "err", "-o", "cat",
                          "--no-pager"])

    def test_c993_bad_monitor_id_falls_back(self):
        rc, out, err = self.run_script(MONITOR_INVOCATION_ID="not-a-hex-id")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.calls("systemctl")), 1)
        self.assertEqual(self.calls("journalctl")[0][1],
                         "_SYSTEMD_INVOCATION_ID=" + "b" * 32)
        self.assertEqual(len(self.aicomm_calls()), 1)

    def _assert_single_line(self, out, err, expected):
        self.assertEqual(err, "")
        self.assertEqual(out.splitlines(), [expected])

    def test_c993_not_configured_exits_zero(self):
        rc, out, err = self.run_script(LGW_NOTIFY_CHANNEL=None)
        self.assertEqual(rc, 0, out + err)
        self._assert_single_line(out, err, "notify skipped: not configured")
        self.assertEqual(self.aicomm_calls(), [])

    def test_c993_aicomm_missing_exits_zero(self):
        os.unlink(os.path.join(self.fakebin, "aicomm"))
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0, out + err)
        self._assert_single_line(out, err, "notify skipped: aicomm not available")
        self.assertFalse(os.path.exists(self.state_path()))

    def test_c993_send_failure_exits_zero_and_records_nothing(self):
        rc, out, err = self.run_script(LGW_TEST_AICOMM_RC="3")
        self.assertEqual(rc, 0, out + err)
        self._assert_single_line(out, err, "notify failed: aicomm exit 3")
        # 送失敗不記狀態：下一次照樣會試。
        self.assertFalse(os.path.exists(self.state_path()))
        self.run_script(LGW_TEST_AICOMM_RC="3")
        self.assertEqual(len(self.aicomm_calls()), 2)

    def test_c993_second_failure_within_an_hour_is_suppressed(self):
        self.assertEqual(self.run_script()[0], 0)
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0, out + err)
        self._assert_single_line(out, err, "notify suppressed: same failure within 1h")
        self.assertEqual(len(self.aicomm_calls()), 1)
        with open(self.state_path(), encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(os.stat(self.state_path()).st_mode & 0o777, 0o600)
        self.assertEqual(document["schema"], 1)
        self.assertEqual(len(document["sent"]), 1)

    def _age_state(self, seconds: int) -> None:
        with open(self.state_path(), encoding="utf-8") as handle:
            document = json.load(handle)
        (key,) = document["sent"]
        document["sent"][key] = int(time.time()) - seconds
        with open(self.state_path(), "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def test_c993_sends_again_after_an_hour(self):
        self.assertEqual(self.run_script()[0], 0)
        self._age_state(3601)
        self.assertEqual(self.run_script()[0], 0)
        self.assertEqual(len(self.aicomm_calls()), 2)

    def test_c993_clock_backwards_sends(self):
        self.assertEqual(self.run_script()[0], 0)
        self._age_state(-86400)          # 狀態檔的時刻在未來（時鐘倒退）
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.splitlines(), ["notify sent"])
        self.assertEqual(len(self.aicomm_calls()), 2)

    def test_c993_corrupt_state_still_sends(self):
        with open(self.state_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.assertEqual(self.run_script()[0], 0)
        self.assertEqual(len(self.aicomm_calls()), 1)
        # 之後以正確格式重寫。
        with open(self.state_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["schema"], 1)

    def test_c993_different_items_is_a_different_failure(self):
        self.assertEqual(self.run_script()[0], 0)
        self.write_journal(["fail keysync" + LEAKY_DETAIL])
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.splitlines(), ["notify sent"])
        self.assertIn("items=keysync ", self.aicomm_calls()[-1][6])
        self.assertEqual(len(self.aicomm_calls()), 2)

    def test_c993_lock_busy_exits_zero(self):
        lock_path = os.path.join(self.state_dir, layoutmod.CHECK_NOTIFY_LOCK_NAME)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc, out, err = self.run_script()
        finally:
            os.close(fd)
        self.assertEqual(rc, 0, out + err)
        self._assert_single_line(out, err, "notify skipped: lock busy")
        self.assertEqual(self.aicomm_calls(), [])

    def test_c993_state_dir_unwritable_exits_zero(self):
        missing = os.path.join(self.root, "no-such-dir", "state")
        rc, out, err = self.run_script(LGW_NOTIFY_STATE_DIR=missing)
        self.assertEqual(rc, 0, out + err)
        self._assert_single_line(out, err, "notify skipped: state unavailable")
        self.assertEqual(self.aicomm_calls(), [])


class InstallNotifyUnitTests(unittest.TestCase):
    """C-995：notify unit 跟著版本進出（假的 systemctl）。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")

    def publish_notify_unit(self, tag: str = NEW_TAG) -> None:
        """把 notify unit 放進假 git 的版本樣板（之後的 fetch／upgrade 都帶著它）。"""
        self.box.write(os.path.join(self.box.template, "deploy", "units", CHECK_NOTIFY),
                       f"[Unit]\n# notify {tag}\n")

    def test_c995_install_installs_the_notify_unit(self):
        self.publish_notify_unit(FAKE_TAG)
        rc, out, err = self.box.run_cli(
            ["install", "--tag", FAKE_TAG, "--repo", "/fake/repo"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.exists(self.box.unit(CHECK_NOTIFY)), out + err)
        self.assertIn("notify", self.box.read(self.box.unit(CHECK_NOTIFY)))
        # 它沒有 [Install]：install 不該對它 enable。
        self.assertNotIn("enable " + CHECK_NOTIFY, self.box.actions())

    def test_c995_upgrade_installs_the_notify_unit(self):
        # 這一版（current）沒有 notify unit；升級的目標版本有。
        os.makedirs(os.path.dirname(self.box.unit("placeholder")), exist_ok=True)
        self.box.install_state(FAKE_TAG)
        current_unit = os.path.join(self.box.opt(FAKE_TAG), "deploy", "units",
                                    CHECK_NOTIFY)
        if os.path.exists(current_unit):
            os.unlink(current_unit)
        self.assertFalse(os.path.exists(self.box.unit(CHECK_NOTIFY)))
        self.publish_notify_unit()
        rc, out, err = self.box.run_cli(["upgrade", "--tag", NEW_TAG])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.exists(self.box.unit(CHECK_NOTIFY)), out + err)

    def write_release_unit(self, tag_dir: str, tag: str) -> None:
        self.box.write(os.path.join(tag_dir, "deploy", "units", CHECK_NOTIFY),
                       f"[Unit]\n# notify {tag}\n")

    def _rollback_setup(self, *, target_has_notify: bool) -> None:
        new_dir = self.box.make_release(NEW_TAG)
        old_dir = self.box.make_release(FAKE_TAG)
        self.write_release_unit(new_dir, NEW_TAG)        # current 有 notify
        if target_has_notify:
            self.write_release_unit(old_dir, FAKE_TAG)
        self.box.symlink(new_dir, self.box.current_link)
        self.box.symlink(old_dir, self.box.previous_link)
        self.box.write(self.box.unit("llmgateway.service"), f"[Unit]\n# {NEW_TAG}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"), f"[Unit]\n# {NEW_TAG}\n")
        self.box.write(self.box.unit(CHECK_SERVICE), f"[Unit]\n# {NEW_TAG}\n")
        self.box.write(self.box.unit(CHECK_NOTIFY), f"[Unit]\n# notify {NEW_TAG}\n")

    def test_c995_rollback_to_a_version_without_it_removes_it(self):
        """#300 的版本沒有 notify unit（有 check service 與 timer）。"""
        self._rollback_setup(target_has_notify=False)
        self.box.set_unit_enabled("llmgateway-check.timer")
        self.box.set_active("llmgateway-check.timer")
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertNotIn("Traceback", out + err)
        self.assertIn("disable --now " + CHECK_NOTIFY, self.box.actions(), out + err)
        self.assertFalse(os.path.exists(self.box.unit(CHECK_NOTIFY)))

    def test_c995_rollback_to_a_version_with_it_keeps_it(self):
        self._rollback_setup(target_has_notify=True)
        self.box.set_unit_enabled("llmgateway-check.timer")
        self.box.set_active("llmgateway-check.timer")
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn("disable --now " + CHECK_NOTIFY, self.box.actions())
        self.assertIn(FAKE_TAG, self.box.read(self.box.unit(CHECK_NOTIFY)))

    def test_c995_migrate_only_disables_it(self):
        """`rollback-to-user` 對 check unit 只 disable、不刪檔（#289 的原則）。"""
        layout = Layout.from_env(self.box.env)
        report = Report(out=lambda _line: None)
        for name in (CHECK_SERVICE, "llmgateway-check.timer", CHECK_NOTIFY):
            self.box.write(self.box.unit(name), f"[Unit]\n# {name}\n")
        self.box.set_unit_enabled("llmgateway-check.timer")
        self.box.set_active("llmgateway-check.timer")
        migrate._remove_new_timer(layout, report)
        actions = self.box.actions()
        for name in (CHECK_SERVICE, "llmgateway-check.timer", CHECK_NOTIFY):
            self.assertIn("disable --now " + name, actions)
            self.assertTrue(os.path.exists(self.box.unit(name)), name)
        self.assertFalse(report.has_fail, report.lines)


class CheckNotifyItemTests(unittest.TestCase):
    """C-996：`check` 的 `check-notify`（只會 ok／todo）。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        self.box.write(self.box.state("keys.env"),
                       "CLAUDE_PERSONAL_OAUTH_TOKEN=FakeKeyValue_DoNotUse\n", 0o600)
        self.box.write(self.box.state(".keysync.status"),
                       json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}),
                       0o644)
        # 這一版的 deploy/units 有 notify unit（check 才會列這一項）。
        self.version_unit = os.path.join(self.box.opt(FAKE_TAG), "deploy", "units",
                                         CHECK_NOTIFY)
        self.box.write(self.version_unit, f"[Unit]\n# {CHECK_NOTIFY}\n")
        self.box.write(self.box.unit(CHECK_NOTIFY), f"[Unit]\n# {CHECK_NOTIFY}\n")
        self.box.set_unit_enabled("llmgateway-check.timer")
        self.box.set_active("llmgateway-check.timer")
        self.toolbin = os.path.join(self.box.root, "toolbin")
        os.makedirs(self.toolbin, exist_ok=True)
        aicomm = os.path.join(self.toolbin, "aicomm")
        self.box.write(aicomm, "#!/bin/sh\nexit 0\n", 0o755)

    def write_env(self, text: str) -> str:
        return self.box.write(self.box.etc("check-notify.env"), text)

    def check(self, **env_extra):
        # 真機的 /usr/local/bin 有真的 aicomm；這裡只用暫存目錄的假 aicomm，
        # 其餘工具（沙箱的假 curl／sudo／systemctl 用 /usr/bin/env bash）照常。
        env = dict(self.box.env)
        env["PATH"] = self.toolbin + ":/usr/bin:/bin"
        env.update(env_extra)
        return self.box.run_cli(["check", "--no-remote"], env=env)

    def test_c996_ok_when_configured(self):
        self.write_env("LGW_NOTIFY_CHANNEL=c\nLGW_NOTIFY_TO=t\nLGW_NOTIFY_LABEL=l\n")
        rc, out, err = self.check()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "check-notify", "ok"), out)

    def test_c996_todo_without_the_env_file(self):
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "check-notify", "todo"), out)
        self.assertIn("失敗不會通知 PMO", out)
        self.assertIn("check-notify.env", out)

    def test_c996_todo_when_a_key_is_missing(self):
        self.write_env("LGW_NOTIFY_CHANNEL=c\n")
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        line = next(line for line in out.splitlines() if " check-notify" in line)
        self.assertIn("LGW_NOTIFY_TO", line)
        self.assertIn("LGW_NOTIFY_LABEL", line)
        self.assertNotIn("LGW_NOTIFY_CHANNEL", line)

    def test_c996_todo_without_aicomm(self):
        self.write_env("LGW_NOTIFY_CHANNEL=c\nLGW_NOTIFY_TO=t\nLGW_NOTIFY_LABEL=l\n")
        os.unlink(os.path.join(self.toolbin, "aicomm"))
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        line = next(line for line in out.splitlines() if " check-notify" in line)
        self.assertIn("找不到 aicomm", line)

    def test_c996_not_listed_for_older_versions(self):
        os.unlink(self.version_unit)
        rc, out, err = self.check()
        self.assertFalse(has_line(out, "check-notify"), out)
        self.assertEqual(rc, 0, out + err)

    def test_c996_never_fails(self):
        # 設定缺、aicomm 也缺：仍然是 todo（2），不是 fail（1）。
        os.unlink(os.path.join(self.toolbin, "aicomm"))
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        self.assertFalse(any(line.startswith("fail check-notify") for line in
                             out.splitlines()), out)

    def test_c996_does_not_send_anything(self):
        self.write_env("LGW_NOTIFY_CHANNEL=c\nLGW_NOTIFY_TO=t\nLGW_NOTIFY_LABEL=l\n")
        log = os.path.join(self.box.root, "aicomm.called")
        aicomm = os.path.join(self.toolbin, "aicomm")
        self.box.write(aicomm, "#!/bin/sh\necho called > " + log + "\nexit 0\n", 0o755)
        self.assertEqual(self.check()[0], 0)
        self.assertFalse(os.path.exists(log), "check 不能送訊息")

    # -- #304：範本留下的 place-holder-* 佔位值算未設定 ----------------------

    REAL = {"LGW_NOTIFY_CHANNEL": "team-test-w1",
            "LGW_NOTIFY_TO": "pmo-test",
            "LGW_NOTIFY_LABEL": "gw-test"}
    TEMPLATE = {"LGW_NOTIFY_CHANNEL": "place-holder-channel",
                "LGW_NOTIFY_TO": "place-holder-recipient",
                "LGW_NOTIFY_LABEL": "place-holder-label"}

    def _write_values(self, values) -> None:
        self.write_env("".join(key + "=" + value + "\n"
                               for key, value in values.items()))

    def _notify_line(self, out) -> str:
        return next(line for line in out.splitlines() if " check-notify" in line)

    def _assert_placeholder_todo(self, keys, values):
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        line = self._notify_line(out)
        self.assertIn("todo", line)
        self.assertIn("佔位值", line)
        for key in keys:
            self.assertIn(key, line)
        for key in set(values) - set(keys):
            self.assertNotIn(key, line)
        # 只寫鍵名，不印值（範本的值一個字都不能出現在輸出裡）。
        for value in values.values():
            self.assertNotIn(value, line)

    def test_304_check_flags_each_placeholder_key(self):
        for key in self.TEMPLATE:
            with self.subTest(key=key):
                values = dict(self.REAL)
                values[key] = self.TEMPLATE[key]
                self._write_values(values)
                self._assert_placeholder_todo([key], values)

    def test_304_check_flags_all_placeholders(self):
        values = dict(self.TEMPLATE)
        self._write_values(values)
        self._assert_placeholder_todo(list(self.TEMPLATE), values)

    def test_304_check_flags_when_only_one_key_is_real(self):
        values = dict(self.TEMPLATE)
        values["LGW_NOTIFY_CHANNEL"] = "team-test-w1"
        self._write_values(values)
        self._assert_placeholder_todo(["LGW_NOTIFY_TO", "LGW_NOTIFY_LABEL"], values)

    def test_304_check_ignores_case_whitespace_and_quotes(self):
        self.write_env('LGW_NOTIFY_CHANNEL="  Place-Holder-Channel  "\n'
                       "LGW_NOTIFY_TO=PLACE-HOLDER-RECIPIENT\n"
                       "LGW_NOTIFY_LABEL=Place-Holder-Label\n")
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        line = self._notify_line(out)
        self.assertIn("佔位值", line)
        for key in self.TEMPLATE:
            self.assertIn(key, line)
        for value in self.TEMPLATE.values():
            self.assertNotIn(value, line)

    def test_304_check_missing_beats_placeholder(self):
        """空鍵走既有的「缺鍵」訊息；不要把它跟佔位值混在一起。"""
        self.write_env("LGW_NOTIFY_CHANNEL=\n"
                       "LGW_NOTIFY_TO=place-holder-recipient\n"
                       "LGW_NOTIFY_LABEL=place-holder-label\n")
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        line = self._notify_line(out)
        self.assertIn("缺 LGW_NOTIFY_CHANNEL", line)
        self.assertNotIn("LGW_NOTIFY_TO", line)
        self.assertNotIn("LGW_NOTIFY_LABEL", line)

    def test_304_check_ok_when_all_values_are_real(self):
        self._write_values(self.REAL)
        rc, out, err = self.check()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "check-notify", "ok"), out)

    def test_304_check_only_the_prefix_counts(self):
        """只有「開頭就是」佔位值才算未設定；名字裡剛好有不算。"""
        values = dict(self.REAL)
        values["LGW_NOTIFY_CHANNEL"] = "x-place-holder-channel"
        self._write_values(values)
        rc, out, err = self.check()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "check-notify", "ok"), out)


class CutoverCheckCodeTests(unittest.TestCase):
    """C-996：遷移的停損門檻不能被「通知還沒設定」的 todo 觸發。"""

    def _report(self, *lines):
        report = Report(out=lambda _line: None)
        for level, name, detail in lines:
            report.add(level, name, detail)
        return report

    def test_c996_benign_todo_only_is_accepted(self):
        report = self._report(("ok", "llmgateway.service", ""),
                              ("todo", "check-notify", "失敗不會通知 PMO"))
        self.assertEqual(migrate._acceptable_check_code(report, 2), 0)

    def test_c996_other_todos_still_stop_loss(self):
        report = self._report(("todo", "check-notify", "x"),
                              ("todo", "keysync", "y"))
        self.assertEqual(migrate._acceptable_check_code(report, 2), 2)

    def test_c996_fail_is_never_accepted(self):
        report = self._report(("todo", "check-notify", "x"),
                              ("fail", "list-local", "z"))
        self.assertEqual(migrate._acceptable_check_code(report, 1), 1)

    def test_c996_zero_stays_zero(self):
        self.assertEqual(migrate._acceptable_check_code(
            Report(out=lambda _line: None), 0), 0)

    def test_c996_cutover_and_finalize_use_it(self):
        source = (REPO_ROOT / "lgwctl" / "migrate.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("_acceptable_check_code(check_report, code)"), 2,
            "cutover 與 finalize 兩處的 check 門檻都要用它")


class TimeoutBudgetTests(unittest.TestCase):
    """C-998：外部指令的逾時合計與整趟總預算都在 unit 的逾時之內（架構師低-1）。"""

    def setUp(self):
        self.module = script_module()

    def unit_timeout_seconds(self) -> int:
        unit = parsed_unit(CHECK_NOTIFY)
        (value,) = unit.values("Service", "TimeoutStartSec")
        match = re.fullmatch(r"([0-9]+)(s|sec|min)?", value.strip())
        self.assertIsNotNone(match, value)
        number = int(match.group(1))
        return number * 60 if match.group(2) == "min" else number

    def timeouts(self):
        return (self.module.SYSTEMCTL_TIMEOUT, self.module.JOURNALCTL_TIMEOUT,
                self.module.AICOMM_TIMEOUT)

    def test_c998_timeouts_fit_inside_the_unit_timeout(self):
        unit_timeout = self.unit_timeout_seconds()
        total = sum(self.timeouts())
        # 三個都卡住時，加起來也不能超過 unit 的上限（不然 systemd 會先殺掉 notify）。
        self.assertLess(total, unit_timeout, self.timeouts())
        # 至少留 10 秒給 systemd 的啟動與收尾。
        self.assertLessEqual(self.module.TOTAL_BUDGET, unit_timeout - 10)
        # 整趟總預算不能小於單一指令的上限（不然某一個指令永遠拿不到自己的上限）。
        for value in self.timeouts():
            self.assertLess(0, value)
            self.assertLessEqual(value, self.module.TOTAL_BUDGET)
        self.assertEqual(self.module.TOTAL_BUDGET, sum(self.timeouts()))

    def test_c998_budget_caps_each_call_by_the_remaining_time(self):
        now = [100.0]

        def clock():
            return now[0]

        budget = self.module.Budget(total=50, clock=clock)
        self.assertEqual(budget.slice(20), 20.0)
        now[0] += 30.0
        self.assertEqual(budget.remaining(), 20.0)
        # 剩 20 秒：一個上限 30 秒的指令只能等 20 秒。
        self.assertEqual(budget.slice(30), 20.0)
        now[0] += 25.0
        self.assertEqual(budget.remaining(), 0.0)
        self.assertEqual(budget.slice(20), 0.0)

    def test_c998_exhausted_budget_does_not_run_the_command(self):
        marker = os.path.join(tempfile.mkdtemp(), "ran")
        self.addCleanup(shutil.rmtree, os.path.dirname(marker), True)
        budget = self.module.Budget(total=0)
        proc = self.module._run(
            [sys.executable, "-c",
             "open({0!r}, 'w').write('ran')".format(marker)], 20, budget)
        self.assertIsNone(proc)
        self.assertFalse(os.path.exists(marker), "預算用完就不該跑那個指令")

class MainBudgetTests(ScriptFixture):
    """C-998：`main` 只建一個 `Budget`，而且傳給每一個外部指令。"""

    def setUp(self):
        super().setUp()
        self.module = script_module()

    def test_c998_main_passes_one_budget_to_every_command(self):
        seen = []
        real_run = self.module._run

        def spy(argv, timeout, budget=None):
            seen.append((os.path.basename(argv[0]), budget, timeout))
            return real_run(argv, timeout, budget)

        out = io.StringIO()
        # 不給 MONITOR_INVOCATION_ID：第一支外部指令會是 systemctl（假的那一支）。
        env = self.env(MONITOR_INVOCATION_ID=None)
        with mock.patch.object(self.module, "_run", spy), \
                mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.module.main(), 0)
        self.assertEqual([name for (name, _budget, _timeout) in seen],
                         ["systemctl", "journalctl", "aicomm"], seen)
        budgets = [budget for (_name, budget, _timeout) in seen]
        self.assertEqual(len({id(budget) for budget in budgets}), 1,
                         "每個外部指令都要拿到同一個 Budget")
        self.assertIsInstance(budgets[0], self.module.Budget)
        # 每個指令拿到的是它自己的上限，而且都在總預算之內。
        for (_name, budget, timeout) in seen:
            self.assertLessEqual(timeout, budget.total)
        self.assertEqual(out.getvalue().splitlines(), ["notify sent"])


class LabelFormatTests(ScriptFixture):
    """C-999：`LGW_NOTIFY_LABEL` 只收 `[A-Za-z0-9._-]{1,32}`（架構師低-2）。"""

    def setUp(self):
        super().setUp()
        self.module = script_module()

    def test_c999_label_pattern_accepts_only_the_whitelist(self):
        pattern = self.module.LABEL_RE
        for good in ("gw-prod", "gw_prod.1", "a", "A" * 32, "node-4.b"):
            self.assertIsNotNone(pattern.fullmatch(good), good)
        for bad in ("gw prod", "gw:prod", "gw=prod", "gw,prod", "gw+prod",
                    "rm -rf /home/worker", "", " ", "a" * 33, "節點"):
            self.assertIsNone(pattern.fullmatch(bad), bad)

    def test_c999_invalid_label_is_treated_as_unset(self):
        for bad in ("gw prod", "rm -rf /home/worker", "gw:prod", "gw=prod"):
            with self.subTest(label=bad):
                rc, out, err = self.run_script(LGW_NOTIFY_LABEL=bad)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(err, "")
                self.assertEqual(out.splitlines(),
                                 ["notify skipped: invalid label"])
                self.assertEqual(self.aicomm_calls(), [])
                self.assertFalse(os.path.exists(self.state_path()))

    def test_c999_length_boundaries(self):
        rc, out, err = self.run_script(LGW_NOTIFY_LABEL="a" * 32)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.splitlines(), ["notify sent"])
        label_field = self.aicomm_calls()[0][6].split("label=", 1)[1]
        self.assertTrue(label_field.startswith("a" * 32))
        rc, out, err = self.run_script(LGW_NOTIFY_LABEL="a" * 33)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.splitlines(), ["notify skipped: invalid label"])

    def test_c999_check_item_reports_a_bad_label(self):
        box = Sandbox()
        self.addCleanup(box.close)
        box.install_state(FAKE_TAG)
        box.write(box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        box.set_active("llmgateway.service")
        box.set_active("llmgateway-tunnel.service")
        box.write(box.state("keys.env"), "X=FakeKeyValue_DoNotUse\n", 0o600)
        box.write(box.state(".keysync.status"),
                  json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}), 0o644)
        box.write(os.path.join(box.opt(FAKE_TAG), "deploy", "units", CHECK_NOTIFY),
                  f"[Unit]\n# {CHECK_NOTIFY}\n")
        box.write(box.etc("check-notify.env"),
                  "LGW_NOTIFY_CHANNEL=c\nLGW_NOTIFY_TO=t\n"
                  "LGW_NOTIFY_LABEL=gw prod\n")
        env = dict(box.env)
        env["PATH"] = "/usr/bin:/bin"
        rc, out, err = box.run_cli(["check", "--no-remote"], env=env)
        self.assertEqual(rc, 2, out + err)
        line = next(line for line in out.splitlines() if " check-notify" in line)
        self.assertIn("格式不對", line)
        self.assertIn("[A-Za-z0-9._-]{1,32}", line)
        self.assertNotIn("gw prod", line)

    def test_c999_pattern_is_shared_with_the_docs_and_the_example(self):
        self.assertEqual(CHECK_NOTIFY_LABEL_PATTERN, self.module.LABEL_PATTERN)
        self.assertIn(CHECK_NOTIFY_LABEL_PATTERN,
                      (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8"))
        self.assertIn(CHECK_NOTIFY_LABEL_PATTERN,
                      EXAMPLE_ENV.read_text(encoding="utf-8"))
        self.assertIn("notify skipped: invalid label",
                      (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8"))


class PlaceholderValueTests(ScriptFixture):
    """#304：腳本把範本的 `place-holder-*` 當成未設定（與「設定不存在」同一條路）。"""

    def setUp(self):
        super().setUp()
        self.module = script_module()

    REAL = {"LGW_NOTIFY_CHANNEL": "team-test-w1",
            "LGW_NOTIFY_TO": "pmo-test",
            "LGW_NOTIFY_LABEL": "gw-test"}
    TEMPLATE = {"LGW_NOTIFY_CHANNEL": "place-holder-channel",
                "LGW_NOTIFY_TO": "place-holder-recipient",
                "LGW_NOTIFY_LABEL": "place-holder-label"}

    def _assert_skipped(self, **overrides):
        rc, out, err = self.run_script(**overrides)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(out.splitlines(), ["notify skipped: not configured"])
        self.assertEqual(self.aicomm_calls(), [], "不該嘗試送訊息")
        self.assertFalse(os.path.exists(self.state_path()))
        # 連 journalctl 都不會跑（設定不完整就停）。
        self.assertEqual(self.calls("journalctl"), [])

    def test_304_script_treats_each_placeholder_key_as_unset(self):
        for key in self.TEMPLATE:
            with self.subTest(key=key):
                self._assert_skipped(**{key: self.TEMPLATE[key]})

    def test_304_script_treats_all_placeholders_as_unset(self):
        self._assert_skipped(**self.TEMPLATE)

    def test_304_script_treats_one_real_value_as_unset(self):
        overrides = dict(self.TEMPLATE)
        overrides["LGW_NOTIFY_CHANNEL"] = "team-test-w1"
        self._assert_skipped(**overrides)

    def test_304_script_ignores_case_and_whitespace(self):
        for value in ("  Place-Holder-Channel  ", "PLACE-HOLDER-CHANNEL",
                      "\tplace-Holder-Channel"):
            with self.subTest(value=value):
                self._assert_skipped(LGW_NOTIFY_CHANNEL=value)

    def test_304_script_sends_when_all_values_are_real(self):
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.splitlines(), ["notify sent"])
        self.assertEqual(len(self.aicomm_calls()), 1)

    def test_304_script_only_the_prefix_counts(self):
        """名字裡剛好有 place-holder 不算（只有開頭就是才算）。"""
        for value in ("x-place-holder-channel", "place-holder",
                      "my-place-holder-team"):
            with self.subTest(value=value):
                if os.path.exists(self.state_path()):
                    os.unlink(self.state_path())      # 去重會擋掉同一組失敗
                rc, out, err = self.run_script(LGW_NOTIFY_CHANNEL=value)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(out.splitlines(), ["notify sent"])
                self.assertEqual(self.aicomm_calls()[-1][2], value)


class PlaceholderRuleTests(unittest.TestCase):
    """#304：check 與腳本的判斷規則一致（同一個字串、同一組輸入同一種結果）。"""

    def setUp(self):
        self.module = script_module()

    def test_304_prefix_is_shared(self):
        self.assertEqual(layoutmod.CHECK_NOTIFY_PLACEHOLDER_PREFIX,
                         self.module.PLACEHOLDER_PREFIX)
        self.assertEqual(layoutmod.CHECK_NOTIFY_PLACEHOLDER_PREFIX,
                         "place-holder-")

    def test_304_both_sides_agree_on_every_shape(self):
        samples = ("place-holder-channel", "place-holder-recipient",
                   "place-holder-label", "PLACE-HOLDER-X", "Place-Holder-Y",
                   "  place-holder-z  ", "\tplace-holder-q\n",
                   "place-holder", "xplace-holder-y", "my-place-holder-team",
                   "team-test-w1", "pmo-test", "gw-test", "a", "", "   ")
        for value in samples:
            with self.subTest(value=value):
                expected = ("" if layoutmod.check_notify_placeholder(value)
                            else value.strip())
                self.assertEqual(self.module.unset_value(value), expected)
        # 佔位值的各種大小寫／空白變體兩邊都算未設定。
        for value in ("place-holder-x", "PLACE-HOLDER-X", " Place-Holder-X\t"):
            with self.subTest(value=value):
                self.assertTrue(layoutmod.check_notify_placeholder(value))
                self.assertEqual(self.module.unset_value(value), "")


class DeployDocNotifyTests(unittest.TestCase):
    """C-997：8.21 節與設定範本跟程式一致。"""

    def setUp(self):
        self.doc = (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
        self.example = EXAMPLE_ENV.read_text(encoding="utf-8")
        self.script = SCRIPT.read_text(encoding="utf-8")

    def example_keys(self) -> list:
        keys = []
        for line in self.example.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            keys.append(line.partition("=")[0].strip())
        return keys

    def test_c997_section_documents_the_facts(self):
        start = self.doc.find("### 8.21 ")
        self.assertGreaterEqual(start, 0, "docs/deploy.md 缺 8.21 節")
        section = self.doc[start:]
        for needle in (CHECK_NOTIFY, CHECK_SERVICE, "OnFailure",
                       layoutmod.CHECK_NOTIFY_ENV_NAME, "aicomm",
                       "check-notify", "一小時", "251", "install --tag"):
            self.assertIn(needle, section, needle)
        for key in layoutmod.CHECK_NOTIFY_ENV_KEYS:
            self.assertIn(key, section, key)
        self.assertIn("check-notify.env.example", section)
        # 第一版不送「恢復」通知（PM 的裁定）。
        self.assertIn("不送「恢復」通知", section)

    def test_c997_example_keys_match_program_and_doc(self):
        keys = self.example_keys()
        self.assertEqual(sorted(keys), sorted(
            list(layoutmod.CHECK_NOTIFY_ENV_KEYS) + ["ANGAREION_IDENTITY"]))
        for key in keys:
            self.assertIn("=", self.example)
            self.assertIn(key, self.script, key)
            self.assertIn(key, self.doc, key)
        # 範本的值都是佔位（不能誤放真的頻道／收件人／身分）。
        for line in self.example.splitlines():
            if line.startswith("LGW_NOTIFY_") or line.startswith("ANGAREION_"):
                self.assertIn("place-holder", line, line)

    def test_c997_names_match_layout(self):
        for name in (layoutmod.CHECK_NOTIFY_NAME, layoutmod.CHECK_NOTIFY_STATE_NAME,
                     layoutmod.CHECK_NOTIFY_LOCK_NAME, layoutmod.CHECK_NOTIFY_AICOMM):
            self.assertIn(name, self.script, name)
        self.assertEqual(layoutmod.CHECK_NOTIFY_NAME, CHECK_NOTIFY)
        source = (REPO_ROOT / "lgwctl" / "check.py").read_text(encoding="utf-8")
        self.assertRegex(source, r'report\.(?:ok|todo)\(\s*"check-notify"')
        self.assertNotRegex(source, r'report\.fail\(\s*"check-notify"')

    def test_c997_check_item_uses_layout_paths(self):
        layout = Layout.from_env({"LLMGW_PREFIX": "/tmp/lgw-296-doc"})
        self.assertTrue(layout.check_notify_env.endswith(
            "/etc/llmgateway/" + layoutmod.CHECK_NOTIFY_ENV_NAME))
        self.assertTrue(layout.check_notify_state_file.endswith(
            "/var/lib/llmgateway/" + layoutmod.CHECK_NOTIFY_STATE_NAME))
        self.assertTrue(layout.check_notify_lock_file.endswith(
            "/var/lib/llmgateway/" + layoutmod.CHECK_NOTIFY_LOCK_NAME))

    def test_c997_check_function_is_wired(self):
        report = Report(out=lambda _line: None)
        _check_notify(Layout.from_env({"LLMGW_PREFIX": "/tmp/lgw-296-nowhere"}),
                      report)
        self.assertEqual(report.lines, [], "這一版目錄沒有 notify unit → 不列這一項")

    def test_304_docs_and_example_say_placeholder_is_unset(self):
        """#304：範本與 8.21 節都要寫明「值還是範本的佔位值就算未設定」。"""
        prefix = layoutmod.CHECK_NOTIFY_PLACEHOLDER_PREFIX
        self.assertIn(prefix, self.example)
        self.assertIn("視為未設定", self.example)
        start = self.doc.find("### 8.21 ")
        self.assertGreaterEqual(start, 0, "docs/deploy.md 缺 8.21 節")
        section = self.doc[start:]
        self.assertIn(prefix, section)
        self.assertIn("視為未設定", section)
        self.assertIn("佔位值", section)
        # 腳本端走的路徑也寫在文件裡（與「設定不存在」同一行 journal）。
        self.assertIn("notify skipped: not configured", section)


if __name__ == "__main__":
    unittest.main()
