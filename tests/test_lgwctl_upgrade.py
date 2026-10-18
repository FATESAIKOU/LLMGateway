"""lgwctl upgrade／rollback 的測試：policy dry-run、只重啟服務、等 list 200、
自動回滾、rollback 後跑 check、清理舊版。

沙箱裡跑；假 curl 可用 ``FAKE_CURL_CODE``／``curl_codes`` 控制每一次的回應。
"""

from __future__ import annotations

import json
import os
import stat
import time
import unittest

try:
    from .lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_TAG,
        NEW_TAG,
        OLD_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_TAG,
        NEW_TAG,
        OLD_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )


class UpgradeTests(unittest.TestCase):
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
        # 沙箱先有「install 裝好的 unit 檔」（升級會把它們換成新 tag 的，中-2）。
        self.box.write(self.box.unit("llmgateway.service"),
                       f"[Unit]\n# llmgateway {FAKE_TAG}\n")
        self.box.write(self.box.unit("llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {FAKE_TAG}\n")

    def upgrade(self, tag=NEW_TAG, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        return self.box.run_cli(["upgrade", "--tag", tag], env=env)

    def current(self):
        return self.box.read(os.path.join(self.box.current_link, ".lgw-tag")).strip()

    # -- 升級成功 -----------------------------------------------------------

    def test_upgrade_success(self):
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [])
        self.assertEqual(self.current(), NEW_TAG)
        self.assertEqual(os.readlink(self.box.previous_link), self.box.opt(FAKE_TAG))
        # 只 restart 服務，隧道完全不動
        actions = self.box.actions()
        self.assertIn("restart llmgateway.service", actions)
        self.assertNotIn("restart llmgateway-tunnel.service", actions)
        self.assertNotIn("stop llmgateway-tunnel.service", actions)
        self.assertTrue(has_line(out, "policy", "ok"), out)
        self.assertTrue(has_line(out, "list", "ok"), out)
        self.assertIn("dry-run 通過", out)
        # unit 檔換成新 tag 的，且 daemon-reload 過（中-2）
        self.assertTrue(has_line(out, "units", "ok"), out)
        self.assertIn(NEW_TAG, self.box.read(self.box.unit("llmgateway.service")))
        self.assertIn(NEW_TAG, self.box.read(self.box.unit("llmgateway-tunnel.service")))
        self.assertIn("daemon-reload daemon-reload", actions)
        # 新版目錄一樣是服務帳號讀得到的權限（Lima 演練的 CHDIR bug）
        self.assertEqual(stat.S_IMODE(os.stat(self.box.opt(NEW_TAG)).st_mode), 0o755)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.box.opt(NEW_TAG), "lgwd")).st_mode),
            0o755)

    def test_upgrade_keeps_two_versions_and_removes_others(self):
        self.box.install_state(FAKE_TAG, previous=OLD_TAG)
        self.box.set_active("llmgateway.service")
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isdir(self.box.opt(FAKE_TAG)))   # 變成 previous
        self.assertTrue(os.path.isdir(self.box.opt(NEW_TAG)))    # current
        self.assertFalse(os.path.exists(self.box.opt(OLD_TAG)))  # 被清掉
        self.assertTrue(has_line(out, "cleanup", "ok"), out)

    def test_upgrade_same_tag_is_safe(self):
        before_previous = os.readlink(self.box.previous_link) if os.path.lexists(
            self.box.previous_link) else None
        rc, out, err = self.upgrade(tag=FAKE_TAG)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.current(), FAKE_TAG)
        after_previous = os.readlink(self.box.previous_link) if os.path.lexists(
            self.box.previous_link) else None
        self.assertEqual(before_previous, after_previous)
        self.assertIn("已經是同版", out)
        self.assertIn("restart llmgateway.service", self.box.actions())

    # -- 預檢與失敗 ---------------------------------------------------------

    def test_policy_dry_run_failure_does_not_switch(self):
        rc, out, err = self.upgrade(FAKE_GIT_BAD_POLICY="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "policy", "fail"), out)
        self.assertIn("不切版", out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertNotIn("restart llmgateway.service", self.box.actions())

    def test_fetch_failure_keeps_running_version(self):
        rc, out, err = self.upgrade(FAKE_GIT_FAIL="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertEqual(self.box.actions(), [])

    def test_bad_tag_is_rejected(self):
        rc, out, err = self.upgrade(tag="nope")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "tag", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_upgrade_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        env["LLMGW_SERVICE_HOME"] = self.box.service_home
        rc, out, err = self.box.run_cli(["upgrade", "--tag", NEW_TAG], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")

    def test_service_restart_failure_restores_links(self):
        self.box.set_start_fail("llmgateway.service")
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "restart", "fail"), out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertFalse(os.path.lexists(self.box.previous_link))
        # unit 檔也要回到切換前的（中-2：失敗要連 unit 一起回復）
        self.assertIn(FAKE_TAG, self.box.read(self.box.unit("llmgateway.service")))

    def test_unit_install_failure_restores_links_and_units(self):
        """unit 寫不進去：link 與 unit 檔都回到切換前、服務不動（中-2）。"""
        unit_dir = os.path.dirname(self.box.unit("llmgateway.service"))
        os.chmod(unit_dir, 0o555)
        self.addCleanup(os.chmod, unit_dir, 0o755)
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "units", "fail"), out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertFalse(os.path.lexists(self.box.previous_link))
        self.assertIn(FAKE_TAG, self.box.read(self.box.unit("llmgateway.service")))
        # unit 還沒換成就停手：服務不該被動到
        self.assertNotIn("restart llmgateway.service", self.box.actions())

    def test_daemon_reload_failure_rolls_back(self):
        """daemon-reload 失敗也要回復（新 unit 沒被讀進去就停，中-2）。"""
        self.box.set_daemon_reload_fail()
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "daemon-reload", "fail"), out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertIn(FAKE_TAG, self.box.read(self.box.unit("llmgateway.service")))
        self.assertNotIn("restart llmgateway.service", self.box.actions())

    def test_policy_dry_run_uses_the_new_tag_loader(self):
        """dry-run 要跑新 tag 目錄裡的 lgwd.policy，不是跑 llmgwctl 的這份（低-5）。"""
        rc, out, err = self.upgrade(FAKE_GIT_BAD_LOADER="1")
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "policy", "fail"), out)
        self.assertIn("fake loader always refuses", out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertNotIn("restart llmgateway.service", self.box.actions())

    def test_restore_links_failure_is_a_line_not_a_traceback(self):
        """回復時連 restore_links 都壞（低-6）：只記一行 fail，不炸 traceback。"""
        from unittest import mock

        from lgwctl import release

        self.box.curl_codes(["500"])
        with mock.patch.object(release, "restore_links", side_effect=OSError("boom")):
            rc, out, err = self.upgrade()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertNotIn("Traceback", out + err)

    # -- 自動回滾 -----------------------------------------------------------

    def test_list_not_200_rolls_back_automatically(self):
        self.box.curl_codes(["500"])
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list", "fail"), out)
        self.assertIn("已切回", out)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertFalse(os.path.lexists(self.box.previous_link))
        # 兩次 restart：先去新版，回滾再回舊版
        self.assertEqual(self.box.actions().count("restart llmgateway.service"), 2)
        # 回滾後不清理，失敗的那版留著看
        self.assertTrue(os.path.isdir(self.box.opt(NEW_TAG)))

    def test_rollback_restores_previous_of_previous(self):
        self.box.install_state(FAKE_TAG, previous=OLD_TAG)
        self.box.set_active("llmgateway.service")
        self.box.curl_codes(["500"])
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertEqual(os.readlink(self.box.previous_link), self.box.opt(OLD_TAG))

    def test_upgrade_waits_and_succeeds_after_a_slow_start(self):
        # 2 秒是「容負載」用的：0.3 秒在測試機忙碌時會來不及第二次重試
        # （第一次 curl 就超過 deadline），不是要模擬 2 秒的啟動時間。
        self.box.curl_codes(["503", "200"])
        rc, out, err = self.upgrade(LLMGW_LIST_WAIT="2")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.current(), NEW_TAG)
        self.assertEqual(self.box.actions().count("restart llmgateway.service"), 1)

    # -- rollback ----------------------------------------------------------

    def test_rollback_after_upgrade_runs_check(self):
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 0, out + err)
        self.assertIn(NEW_TAG, self.box.read(self.box.unit("llmgateway.service")))
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.current(), FAKE_TAG)
        self.assertEqual(os.readlink(self.box.previous_link), self.box.opt(NEW_TAG))
        self.assertTrue(has_line(out, "policy", "ok"), out)      # 有跑 check
        self.assertTrue(has_line(out, "keysync", "ok"), out)
        self.assertTrue(has_line(out, "list-local", "ok"), out)
        self.assertEqual(self.box.actions().count("restart llmgateway.service"), 2)
        # 中-3：unit 檔也要換回舊 tag 的（不然舊程式跑在新 unit 下）。
        self.assertIn(FAKE_TAG, self.box.read(self.box.unit("llmgateway.service")))
        self.assertIn(FAKE_TAG, self.box.read(self.box.unit("llmgateway-tunnel.service")))
        self.assertNotIn(NEW_TAG, self.box.read(self.box.unit("llmgateway.service")))
        self.assertIn("daemon-reload daemon-reload", self.box.actions())

    def test_rollback_refuses_when_previous_has_no_unit_files(self):
        """中-3：previous 是舊格式（沒有 deploy/units）就在切 link 之前拒絕。"""
        import shutil

        self.assertEqual(self.upgrade()[0], 0)
        shutil.rmtree(os.path.join(self.box.opt(FAKE_TAG), "deploy", "units"))
        before = self.box.actions().count("restart llmgateway.service")
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertIn("deploy/units", out)
        self.assertEqual(self.current(), NEW_TAG)
        self.assertEqual(self.box.actions().count("restart llmgateway.service"), before)

    def test_rollback_returns_check_exit_code(self):
        self.upgrade()
        # 回滾後本機 list 一直不是 200（rollback 自己的等待也拿同一個值）→ check 說 fail
        self.box.curl_codes(["500", "500"])
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list-local", "fail"), out)

    def test_rollback_without_previous(self):
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertIn("沒有 previous", out)

    def test_rollback_with_missing_previous_dir(self):
        self.box.symlink(self.box.opt(OLD_TAG), self.box.previous_link)
        rc, out, err = self.box.run_cli(["rollback"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertIn("目錄不在", out)

    # -- 輸出不含值 ---------------------------------------------------------

    def test_output_has_no_values(self):
        rc, out, err = self.upgrade()
        text = out + err
        self.assertNotIn(FAKE_KEY_VALUE, text)

    # -- 壞環境（不該 traceback）-------------------------------------------

    def test_unwritable_opt_dir_is_a_fail_not_a_traceback(self):
        opt_dir = self.box.opt()
        os.chmod(opt_dir, 0o555)
        self.addCleanup(os.chmod, opt_dir, 0o755)
        rc, out, err = self.upgrade()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(self.current(), FAKE_TAG)


if __name__ == "__main__":
    unittest.main()
