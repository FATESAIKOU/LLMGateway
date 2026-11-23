"""能力旗標接上各呼叫點（#246 P-3 呼叫點表）：T-23～T-31、T-34。

每個測試都在沙箱裡跑（假 systemctl／假 git／假 curl／假隧道探針）。沙箱的版本
目錄預設**沒有** ``deploy/capabilities.json``，所以是 ``old``（先打 ``/healthz``、
404 才退回 list）；要 ``new`` 的測試自己把旗標寫進那一版的目錄或樣板。
"""

import json
import os
import time
import unittest

from lgwctl import tunnel_apply
from lgwctl.layout import Layout
from lgwctl.report import Report

try:
    from .lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_TAG,
        NEW_TAG,
        OLD_TAG,
        Sandbox,
        has_line,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_TAG,
        NEW_TAG,
        OLD_TAG,
        Sandbox,
        has_line,
    )

VALID = '{"schema": 1, "probe": "healthz"}\n'
HEALTH = "/healthz"
LIST = "/v1/subscriptions"


class CapabilityCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.box.write(self.box.state(".keysync.status"),
                       json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}), 0o644)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        self.box.write(self.box.unit("llmgateway.service"),
                       f"[Unit]\n# llmgateway {FAKE_TAG}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {FAKE_TAG}\n")

    # -- 工具 ---------------------------------------------------------------

    def layout(self, **env_extra) -> Layout:
        env = dict(self.box.env)
        env.update(env_extra)
        return Layout.from_env(env)

    def current(self) -> str:
        return self.box.read(os.path.join(self.box.current_link, ".lgw-tag")).strip()

    def requests(self) -> list:
        paths = []
        for line in self.box.log_lines("curl.log"):
            url = line.split()[-1]
            paths.append("/" + url.split("://", 1)[-1].split("/", 1)[1])
        return paths

    def template_capabilities(self, text) -> None:
        """放進版本樣板：假 git clone（upgrade）與 make_release 都會帶過去。"""
        self.box.set_capabilities(self.box.template, text)

    def new_release(self, tag: str, capabilities=None) -> str:
        path = self.box.make_release(tag)
        self.box.set_capabilities(path, capabilities)
        return path

    def upgrade(self, tag=NEW_TAG, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        return self.box.run_cli(["upgrade", "--tag", tag], env=env)

    def install(self, tag, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        return self.box.run_cli(["install", "--tag", tag], env=env)


class UpgradeCapabilityTests(CapabilityCase):
    """T-23～T-25：upgrade 的切換前驗證與探針模式。"""

    def test_T23_invalid_capability_blocks_the_switch(self):
        """新 tag 的旗標損毀 → fail capabilities；current／previous／unit 都沒變。"""
        self.template_capabilities("{not json")
        before_unit = self.box.read(self.box.unit("llmgateway.service"))
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "capabilities", "fail"), out)
        self.assertIn("不切版", out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertFalse(os.path.lexists(self.box.previous_link))
        self.assertEqual(self.box.read(self.box.unit("llmgateway.service")), before_unit)
        self.assertNotIn("restart llmgateway.service", self.box.actions())
        self.assertEqual(self.box.log("curl.log"), "")   # 連探都沒探

    def test_T23_unreadable_capability_blocks_the_switch(self):
        """讀不到（EACCES）也一樣擋。

        這裡用注入的方式造 unreadable：假 git 是 `cp -a`，讀不到檔時連複製都會
        失敗，做不出「已取碼但旗標讀不到」的狀態。解析本身（含真的 EACCES）由
        T-8 驗；這一格驗 upgrade 有沒有把它當成「不切版」。
        """
        from unittest import mock

        from lgwctl import capability

        self.template_capabilities(VALID)
        with mock.patch.object(capability, "read", return_value=capability.UNREADABLE):
            rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "capabilities", "fail"), out)
        self.assertIn("讀不到", out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertNotIn("restart llmgateway.service", self.box.actions())

    def test_T24_new_capability_probes_healthz_only(self):
        self.template_capabilities(VALID)
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "capabilities", "ok"), out)
        self.assertIn("new", out)
        self.assertEqual(self.current(), NEW_TAG)
        self.assertEqual(self.requests(), [HEALTH])
        self.assertNotIn(LIST, self.box.log("curl.log"))

    def test_T25_new_capability_against_old_service_never_probes_list(self):
        """new＋舊服務（/healthz 404）→ list-wait 逾時並自動回滾，**沒有** list 請求。"""
        self.template_capabilities(VALID)
        self.box.curl_code(HEALTH, 404)
        self.box.curl_code(LIST, 200)
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "list", "fail"), out)
        self.assertEqual(self.current(), FAKE_TAG)      # 已自動切回
        self.assertEqual(self.requests(), [HEALTH])
        self.assertNotIn(LIST, self.box.log("curl.log"))


class RollbackCapabilityTests(CapabilityCase):
    """T-26～T-27：rollback 讀 previous 的旗標；損毀不擋回滾。"""

    def to_new_version(self) -> None:
        new_dir = self.new_release(NEW_TAG, VALID)
        self.box.symlink(new_dir, self.box.current_link)

    def test_T26_rollback_to_a_version_without_flags_probes_old(self):
        """從 new 版回滾到沒有旗標的舊版：/healthz 404 → list 200，遠端用 old。"""
        self.to_new_version()
        self.box.symlink(self.box.opt(FAKE_TAG), self.box.previous_link)
        self.box.curl_code(HEALTH, 404)
        self.box.curl_code(LIST, 200)
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertTrue(has_line(out, "capabilities", "ok"), out)
        self.assertIn("無（old）", out)
        self.assertTrue(has_line(out, "list-local", "ok"), out)
        self.assertIn("old 退回", out)
        # 請求序列：每一次本機探針都是 /healthz 404 之後才 list（rollback 自己
        # 等一次、check 再探一次）
        self.assertEqual(self.requests()[-2:], [HEALTH, LIST])
        # 遠端探針用 old（不是 new）
        probe_log = self.box.log_lines("probe.log")
        self.assertIn("api-mode old", probe_log)
        self.assertNotIn("api-mode new", probe_log)

    def test_T27_invalid_previous_capability_still_rolls_back(self):
        """previous 的旗標損毀：照樣回滾，check 的 list-local 是 fail。"""
        self.to_new_version()
        broken = self.new_release(OLD_TAG, "{not json")
        self.box.symlink(broken, self.box.previous_link)
        self.box.curl_code(HEALTH, 404)
        self.box.curl_code(LIST, 200)
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.current(), OLD_TAG)
        self.assertTrue(has_line(out, "capabilities", "todo"), out)
        self.assertIn("損毀", out)
        self.assertTrue(has_line(out, "list-local", "fail"), out)
        self.assertIn("能力旗標損毀", out)
        # 以 old 探（有 /healthz，也有 404 之後的 list）
        self.assertEqual(self.requests()[-2:], [HEALTH, LIST])


class InstallCapabilityTests(unittest.TestCase):
    """T-28：install 的切換前驗證與探針模式。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def test_T28_first_install_probes_healthz(self):
        self.box.set_capabilities(self.box.template, VALID)
        rc, out, err = self.box.run_cli(["install", "--tag", NEW_TAG])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "capabilities", "ok"), out)
        self.assertIn("new", out)
        self.assertEqual(os.readlink(self.box.current_link), self.box.opt(NEW_TAG))
        self.assertEqual(self.box.log("curl.log").count("/healthz"), 1)
        self.assertNotIn(LIST, self.box.log("curl.log"))

    def test_T28_invalid_capability_fails_before_switching(self):
        self.box.set_capabilities(self.box.template, "[]")
        rc, out, err = self.box.run_cli(["install", "--tag", NEW_TAG])
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "capabilities", "fail"), out)
        self.assertFalse(os.path.lexists(self.box.current_link))
        self.assertEqual(self.box.actions(), [])
        self.assertEqual(self.box.log("curl.log"), "")


class StartStopCapabilityTests(CapabilityCase):
    """T-29～T-31：start／stop 的探針。"""

    def new_current(self) -> None:
        new_dir = self.new_release(NEW_TAG, VALID)
        self.box.symlink(new_dir, self.box.current_link)

    def test_T29_start_when_active_and_healthy_does_nothing(self):
        self.new_current()
        self.box.curl_code(HEALTH, 200)
        rc, out, err = self.box.run_cli(["start"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.actions(), [])
        self.assertEqual(self.requests(), [HEALTH])

    def test_T30_stop_when_inactive_does_not_probe(self):
        self.new_current()
        self.box.set_active("llmgateway.service", False)
        rc, out, err = self.box.run_cli(["stop"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.log("curl.log"), "")
        self.assertEqual(self.box.actions(), [])

    def test_T31_new_capability_with_503_never_gets_healthy(self):
        """旗標 new、active、/healthz 503：start 後等到逾時回 1。"""
        self.new_current()
        self.box.set_active("llmgateway.service", False)
        self.box.curl_code(HEALTH, 503)
        self.box.curl_code(LIST, 200)
        rc, out, err = self.box.run_cli(["start"])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("start llmgateway.service", self.box.actions())
        # new 不退回 list：503 就是失敗
        self.assertEqual(set(self.requests()), {HEALTH})


class TunnelApplyCapabilityTests(CapabilityCase):
    """T-34：tunnel-apply 的探針帶 current 的模式，rc 分類照舊。"""

    def test_T34_probe_until_ok_uses_the_current_mode(self):
        new_dir = self.new_release(NEW_TAG, VALID)
        self.box.symlink(new_dir, self.box.current_link)
        report = Report()
        self.assertTrue(tunnel_apply._probe_until_ok(self.layout(), report))
        self.assertIn("api-mode new", self.box.log_lines("probe.log"))

    def test_T34_rc2_is_still_an_unprobeable_failure(self):
        report = Report()
        layout = self.layout(FAKE_PROBE_RC="2", LLMGW_TUNNEL_APPLY_TEST="1",
                             LLMGW_TUNNEL_APPLY_WAIT="30")
        self.assertFalse(tunnel_apply._probe_until_ok(layout, report))
        self.assertTrue(has_line("\n".join(report.lines), "probe", "fail"),
                        report.lines)
        self.assertIn("無法探", "\n".join(report.lines))
        # old（這個版本沒有旗標）
        self.assertIn("api-mode old", self.box.log_lines("probe.log"))


if __name__ == "__main__":
    unittest.main()
