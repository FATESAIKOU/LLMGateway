"""lgwctl install 的測試：冪等、todo 項目、unit、權限、wrapper、清理舊版。

全部在沙箱裡跑（假 systemctl／假 git／假檔案系統），不碰真機；keys.pass 與
keys.env 都是假的。
"""

from __future__ import annotations

import os
import stat
import unittest

try:
    from .lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        NEW_TAG,
        OLD_TAG,
        Sandbox,
        current_user,
        has_line,
        lines_with_level,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        NEW_TAG,
        OLD_TAG,
        Sandbox,
        current_user,
        has_line,
        lines_with_level,
    )


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.repo = "/fake/repo"

    # -- 共用的小工具 -------------------------------------------------------

    def install(self, tag=FAKE_TAG, repo=None, **extra_env):
        env = dict(self.box.env)
        env.update(extra_env)
        argv = ["install", "--tag", tag]
        if repo is None:
            argv += ["--repo", self.repo]
        elif repo.startswith("-"):
            # argparse 認 --opt=值，值本身可以 - 開頭（中-6 的注入面就從這來）
            argv += [f"--repo={repo}"]
        else:
            argv += ["--repo", repo]
        return self.box.run_cli(argv, env=env)

    def fill_tunnel_config(self):
        """把隧道設定填好（假的金鑰檔），讓 install 會去 start 隧道單位。"""
        identity = os.path.join(self.box.service_home, ".ssh", "id_llmgw_tunnel")
        known_hosts = os.path.join(self.box.service_home, ".ssh", "known_hosts_rendezvous")
        for path in (identity, known_hosts):
            self.box.write(path, "fake\n", 0o600)
        self.box.write(self.box.etc("tunnel.env"), (
            "RENDEZVOUS=user@example:22\n"
            f"IDENTITY={identity}\n"
            f"KNOWN_HOSTS={known_hosts}\n"
        ))

    # -- 測試 ---------------------------------------------------------------

    def test_fresh_install(self):
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [])
        # 版本目錄、current、.lgw-tag
        tag_dir = self.box.opt(FAKE_TAG)
        self.assertTrue(os.path.isdir(tag_dir))
        self.assertEqual(os.readlink(self.box.current_link), tag_dir)
        self.assertEqual(self.box.read(os.path.join(tag_dir, ".lgw-tag")), FAKE_TAG + "\n")
        # 取碼參數是固定 tag
        git_log = self.box.log("git.log")
        self.assertIn("clone", git_log)
        self.assertIn("--depth 1", git_log)
        self.assertIn(f"--branch {FAKE_TAG}", git_log)
        # unit 檔（內容是那一版自己的）
        service_unit = self.box.unit("llmgateway.service")
        tunnel_unit = self.box.unit("llmgateway-tunnel.service")
        self.assertIn(FAKE_TAG, self.box.read(service_unit))
        self.assertIn(FAKE_TAG, self.box.read(tunnel_unit))
        # 設定範本
        self.assertIn('LGW_API_PORT="8480"', self.box.read(self.box.etc("llmgateway.env")))
        tunnel_env = self.box.read(self.box.etc("tunnel.env"))
        self.assertIn("IDENTITY=" + os.path.join(self.box.service_home, ".ssh",
                                                 "id_llmgw_tunnel"), tunnel_env)
        self.assertIn("RENDEZVOUS=", tunnel_env)
        # LOCAL_API 要跟上面的 LGW_API_PORT 一致（低-1；這裡是預設 8480）
        self.assertIn("LOCAL_API=8480", tunnel_env)
        # keys.pass 是空殼（只有註解）、keys.env 是空的 0600
        keys_pass = self.box.etc("keys.pass")
        self.assertNotIn(FAKE_PASSWORD, self.box.read(keys_pass))
        self.assertEqual(stat.S_IMODE(os.stat(keys_pass).st_mode), 0o640)
        keys_env = self.box.state("keys.env")
        self.assertEqual(self.box.read(keys_env), "")
        self.assertEqual(stat.S_IMODE(os.stat(keys_env).st_mode), 0o600)
        # wrapper
        wrapper = self.box.path("usr", "local", "bin", "llmgwctl")
        self.assertIn(self.box.current_link, self.box.read(wrapper))
        self.assertTrue(os.stat(wrapper).st_mode & stat.S_IXUSR)
        # systemctl：daemon-reload、enable 兩個、start 服務；隧道還沒填所以不 start
        actions = self.box.actions()
        self.assertIn("daemon-reload daemon-reload", actions)
        self.assertIn("enable llmgateway.service", actions)
        self.assertIn("enable llmgateway-tunnel.service", actions)
        self.assertIn("start llmgateway.service", actions)
        self.assertNotIn("start llmgateway-tunnel.service", actions)
        # 本人動作的 todo（假 gh 預設沒登入）
        self.assertTrue(has_line(out, "keys-pass", "todo"), out)
        self.assertTrue(has_line(out, "tunnel-key", "todo"), out)
        self.assertTrue(has_line(out, "gh-login", "todo"), out)
        # 帳號是假的 useradd 建的
        self.assertIn("lgwfake", self.box.log("useradd.log"))
        self.assertTrue(has_line(out, "account", "ok"), out)

    def test_install_is_idempotent_and_keeps_config(self):
        self.install()
        # 模擬本人動過的設定與已放的密碼
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="9999"\n')
        self.fill_tunnel_config()
        self.box.write(self.box.etc("keys.pass"),
                       f"# comment\nLLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        before = {path: self.box.read(path) for path in
                  (self.box.etc("llmgateway.env"), self.box.etc("tunnel.env"),
                   self.box.etc("keys.pass"), self.box.state("keys.env"))}

        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [])
        for path, text in before.items():
            self.assertEqual(self.box.read(path), text, path)
        # 已完整就沿用，不再 clone
        self.assertEqual(self.box.log_lines("git.log").__len__(), 1)
        self.assertTrue(has_line(out, "fetch", "ok"), out)
        # 服務已在跑就不重啟、隧道已在跑也不動（升級才 restart 服務）
        self.assertNotIn("restart llmgateway.service", self.box.actions())
        self.assertNotIn("restart llmgateway-tunnel.service", self.box.actions())
        self.assertEqual(self.box.read(self.box.etc("tunnel.env")), before[self.box.etc("tunnel.env")])
        # 密碼已放、隧道已填，就不再列那兩個 todo
        self.assertTrue(has_line(out, "keys-pass", "ok"), out)
        self.assertFalse(has_line(out, "keys-pass", "todo"), out)
        self.assertFalse(has_line(out, "tunnel-key", "todo"), out)

    def test_existing_account_is_kept(self):
        box = Sandbox(service_user=current_user())
        self.addCleanup(box.close)
        rc, out, err = box.run_cli(["install", "--tag", FAKE_TAG])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "account", "ok"), out)
        self.assertIn("已存在", out)
        self.assertEqual(box.log("useradd.log"), "")

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_install_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        env["LLMGW_SERVICE_HOME"] = self.box.service_home
        rc, out, err = self.box.run_cli(["install", "--tag", FAKE_TAG], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")

    def test_bad_tag_stops_before_doing_anything(self):
        rc, out, err = self.install(tag="not-a-tag")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "tag", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")
        self.assertFalse(os.path.lexists(self.box.current_link))

    def test_git_failure_is_a_fail_and_no_current(self):
        rc, out, err = self.install(FAKE_GIT_FAIL="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertFalse(os.path.lexists(self.box.current_link))
        self.assertFalse(os.path.lexists(self.box.unit("llmgateway.service")))

    def test_tag_without_tunnel_unit_is_rejected(self):
        rc, out, err = self.install(
            FAKE_GIT_SKIP="deploy/units/llmgateway-tunnel.service")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertIn("deploy/units/llmgateway-tunnel.service", out)
        self.assertFalse(os.path.lexists(self.box.current_link))

    # -- 取碼的 repo 與必備檔（中-6／低-8）-----------------------------------

    def test_repo_starting_with_dash_is_rejected(self):
        """git 會把 - 開頭的 repo 當選項解（--upload-pack=…）；一律擋（中-6）。"""
        rc, out, err = self.install(repo="--upload-pack=/bin/evil")
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertIn("repo 位址不可用", out)
        self.assertEqual(self.box.log("git.log"), "")   # 根本沒跑 git

    def test_env_repo_starting_with_dash_is_rejected(self):
        """LLMGW_REPO 走同一關（--repo 有檢查不等於環境變數沒事，中-6）。"""
        rc, out, err = self.box.run_cli(
            ["install", "--tag", FAKE_TAG],
            env={**self.box.env, "LLMGW_REPO": "--upload-pack=/bin/evil"})
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")

    def test_git_clone_puts_double_dash_before_repo(self):
        """clone 的 repo 前面要有 --（選項與路徑分開，中-6）。"""
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"-- {self.repo} ", self.box.log("git.log"))

    def test_release_files_must_all_be_present(self):
        """必備檔缺一個就不算完整（低-8）：lgwd/keys.py 也要在。"""
        rc, out, err = self.install(FAKE_GIT_SKIP="lgwd/keys.py")
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "fetch", "fail"), out)
        self.assertIn("lgwd/keys.py", out)
        self.assertFalse(os.path.lexists(self.box.current_link))

    def test_bad_repo_helper(self):
        from lgwctl import release

        self.assertTrue(release.bad_repo("--upload-pack=/bin/sh"))
        self.assertTrue(release.bad_repo("-e.pwn"))
        self.assertTrue(release.bad_repo("a\0b"))
        self.assertFalse(release.bad_repo("https://example.com/repo.git"))
        self.assertFalse(release.bad_repo("/fake/repo"))

    def test_tunnel_ready_starts_tunnel_unit(self):
        self.box.write(self.box.etc("tunnel.env"), "")
        self.fill_tunnel_config()
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("start llmgateway-tunnel.service", self.box.actions())
        self.assertTrue(has_line(out, "tunnel-config", "ok"), out)
        self.assertFalse(has_line(out, "tunnel-key", "todo"), out)

    def test_service_start_failure_is_a_fail(self):
        self.box.set_start_fail("llmgateway.service")
        rc, out, err = self.install()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "start", "fail"), out)

    def test_start_ok_but_list_not_200_is_fail(self):
        """systemctl start 回 0 不代表 app 起得來；list 不是 200 就是 fail（中-7）。"""
        self.box.set_start_noop("llmgateway.service")   # start 回 0 但 app 沒起來
        rc, out, err = self.install(FAKE_CURL_CODE="500")
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "start", "fail"), out)
        self.assertIn("list 不是 200", out)

    def test_tunnel_start_is_confirmed_with_is_active(self):
        """隧道 start 之後再 is-active 確認一次；沒跑起來是 todo 不是假 ok（中-7）。"""
        self.box.write(self.box.etc("tunnel.env"), "")
        self.fill_tunnel_config()
        self.box.set_start_noop("llmgateway-tunnel.service")
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)   # 隧道是本人動作，todo 不算失敗
        self.assertTrue(has_line(out, "tunnel-run", "todo"), out)
        self.assertIn("沒確認成功", out)

    def test_tunnel_todo_tells_the_next_step(self):
        """設定未填的 todo 要寫明下一步（低-2）。"""
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "tunnel-key", "todo"), out)
        self.assertIn("systemctl start llmgateway-tunnel.service", out)

    def test_install_refuses_when_old_user_units_are_running(self):
        """中-10：舊 user 版還在跑時這是遷移，不是安裝；不能先起新服務搶 8480。"""
        from unittest import mock

        with mock.patch("lgwctl.units.user_unit_state", return_value="active"):
            rc, out, err = self.install()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "old-user-units", "fail"), out)
        self.assertIn("prepare-migration", out)
        self.assertEqual(self.box.log("git.log"), "")
        self.assertFalse(os.path.lexists(self.box.current_link))
        self.assertEqual(self.box.actions(), [])

    def test_start_ok_but_unit_not_active_is_fail(self):
        """中-10：本機 list 200 可能是舊服務在回答；新 unit 沒 active 就是失敗。"""
        self.box.set_start_noop("llmgateway.service")
        rc, out, err = self.install()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "start", "fail"), out)
        self.assertIn("is-active", out)

    def test_install_fixes_a_private_etc_dir(self):
        """低-1：root umask 077 留下的 0700 /etc/llmgateway，llmgw 進不去。"""
        os.makedirs(self.box.etc(), exist_ok=True)
        os.chmod(self.box.etc(), 0o700)
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(stat.S_IMODE(os.stat(self.box.etc()).st_mode), 0o755)

    def test_install_rerun_hints_restart_after_keys_pass_is_filled(self):
        """低-11：LoadCredential 是啟動快照；填完 keys.pass 重跑 install 要提醒 restart。"""
        self.assertEqual(self.install()[0], 0)
        self.box.write(self.box.etc("keys.pass"),
                       f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "restart", "todo"), out)
        self.assertIn("快照", out)
        self.assertEqual(self.box.actions().count("start llmgateway.service"), 1)

    def test_install_with_a_different_tag_is_rejected(self):
        """install 換 tag 會造成「跑舊碼、status 說新版」；一律擋下（中-3）。"""
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.install(tag=NEW_TAG)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "tag", "fail"), out)
        self.assertIn("upgrade", out)
        self.assertEqual(os.readlink(self.box.current_link), self.box.opt(FAKE_TAG))
        self.assertEqual(len(self.box.log_lines("git.log")), 1)  # 沒有偷抓新版

    def test_install_with_the_same_tag_stays_idempotent(self):
        """同 tag 重跑仍然要照跑（冪等，中-3 的另一半）。"""
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.install(tag=FAKE_TAG)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.readlink(self.box.current_link), self.box.opt(FAKE_TAG))

    def test_tunnel_env_port_follows_llmgateway_env(self):
        """tunnel.env 的 LOCAL_API 要跟 llmgateway.env 的 LGW_API_PORT 一致（低-1）。"""
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="9999"\n')
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("LOCAL_API=9999", self.box.read(self.box.etc("tunnel.env")))

    # -- 帳號與權限（中-4／低-9）--------------------------------------------

    def test_useradd_creates_the_group(self):
        """unit 的 Group= 要有同名群組，useradd 要帶 -U（低-9）。"""
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("-U", self.box.log("useradd.log"))

    def test_missing_group_is_a_fail(self):
        """群組不在（-U 沒建出來）是 fail，不能裝完才發現服務起不來（低-9）。"""
        from unittest import mock
        from lgwctl import install as installmod

        with mock.patch.object(installmod, "_group_exists", return_value=False):
            rc, out, err = self.install()
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "account", "fail"), out)
        self.assertIn("群組", out)

    def test_keys_pass_chown_targets(self):
        """keys.pass → root:<llmgw gid>、keys.env 與目錄 → llmgw:llmgw（中-4）。"""
        from types import SimpleNamespace
        from unittest import mock

        from lgwctl import install as installmod
        from lgwctl.layout import Layout

        layout = Layout.from_env(self.box.env)
        layout.prefix = ""      # 裝成正式模式（prefix 模式一律不 chown）
        layout.euid = 0
        calls = []
        with mock.patch.object(installmod.pwd, "getpwnam",
                               return_value=SimpleNamespace(pw_uid=111, pw_gid=222)), \
                mock.patch.object(installmod.os, "chown",
                                  side_effect=lambda path, uid, gid:
                                      calls.append((path, uid, gid))):
            installmod._chown_keys_pass(layout)
            installmod._chown_service(layout, layout.keys_env)
            installmod._chown_service(layout, layout.state_dir)
        self.assertEqual(calls, [
            (layout.keys_pass_file, 0, 222),
            (layout.keys_env, 111, 222),
            (layout.state_dir, 111, 222),
        ])

    @unittest.skipIf(os.geteuid() != 0, "只有 root 會真的 chown")
    def test_keys_pass_is_root_owned_with_service_group(self):
        """真的 root 跑一次：keys.pass root:<gid> 0640、keys.env llmgw:llmgw；重跑不變。"""
        import pwd as pwdmod

        box = Sandbox(service_user=current_user())
        self.addCleanup(box.close)
        rc, out, err = box.run_cli(["install", "--tag", FAKE_TAG])
        self.assertEqual(rc, 0, out + err)
        info = pwdmod.getpwnam(box.service_user)
        st = os.stat(box.etc("keys.pass"))
        self.assertEqual((st.st_uid, st.st_gid), (0, info.pw_gid))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
        env_st = os.stat(box.state("keys.env"))
        self.assertEqual((env_st.st_uid, env_st.st_gid), (info.pw_uid, info.pw_gid))
        # 重跑不得把 keys.pass 改成服務帳號擁有
        rc, out, err = box.run_cli(["install", "--tag", FAKE_TAG])
        self.assertEqual(rc, 0, out + err)
        st = os.stat(box.etc("keys.pass"))
        self.assertEqual((st.st_uid, st.st_gid), (0, info.pw_gid))

    def test_gh_logged_in_is_not_a_todo(self):
        rc, out, err = self.install(FAKE_GH_AUTH_RC="0")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "gh-login", "ok"), out)
        self.assertFalse(has_line(out, "gh-login", "todo"), out)
        sudo_log = self.box.log("sudo.log")
        self.assertIn("-n -u lgwfake", sudo_log)
        self.assertIn("/fakebin/gh auth status", sudo_log)

    def test_gh_missing_is_a_todo_not_a_fail(self):
        rc, out, err = self.install(LLMGW_GH=os.path.join(self.box.root, "no-gh"))
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "gh-login", "todo"), out)
        self.assertEqual(lines_with_level(out, "fail"), [])

    def test_install_removes_other_versions(self):
        stray = self.box.make_release(OLD_TAG)
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(stray))
        self.assertTrue(has_line(out, "cleanup", "ok"), out)

    def test_install_does_not_write_secret_values_into_output(self):
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.box.write(self.box.etc("keys.pass"),
                       f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        self.box.write(self.box.state("keys.env"),
                       f"OPENROUTER_API_KEY={FAKE_KEY_VALUE}\n", 0o600)
        rc2, out2, err2 = self.install()
        self.assertEqual(rc2, 0, out2 + err2)
        rc3, out3, _ = self.box.run_cli(["status"])
        text = out + out2 + out3 + err + err2
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertNotIn(FAKE_KEY_VALUE, text)

    def test_children_do_not_inherit_the_exported_password(self):
        """操作者 shell export 的解密密碼不傳給 git／gh 等子行程（低-4）。"""
        rc, out, err = self.install(LLMGW_KEYS_PASSWORD=FAKE_PASSWORD)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("env-password no", self.box.log("gh.log"))
        self.assertNotIn(FAKE_PASSWORD, out + err)

    # -- 版本目錄的權限（Lima 演練抓到的 CHDIR bug）-------------------------

    def test_version_dir_is_readable_by_the_service_user(self):
        """mkdtemp 的 0700 會讓 User=llmgw 進不去 current（status=200/CHDIR）。"""
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        tag_dir = self.box.opt(FAKE_TAG)
        self.assertEqual(stat.S_IMODE(os.stat(tag_dir).st_mode), 0o755)
        for rel in ("lgwd", "ops-scripts", "policy", "deploy", "deploy/units"):
            path = os.path.join(tag_dir, rel)
            self.assertTrue(os.path.isdir(path), rel)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755, rel)
        for rel in ("policy/default.json", "lgwd/__main__.py",
                    "deploy/units/llmgateway.service", ".lgw-tag"):
            path = os.path.join(tag_dir, rel)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o644, rel)
            self.assertEqual(mode & 0o022, 0, rel)  # 沒有 group／other 寫位
        for rel in ("ops-scripts/key-sync", "ops-scripts/llmgateway-tunnel"):
            path = os.path.join(tag_dir, rel)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755, rel)
        # 發佈目錄不需要 .git
        self.assertFalse(os.path.exists(os.path.join(tag_dir, ".git")))

    def test_reused_version_dir_is_fixed_too(self):
        """已存在的版本目錄（舊版留下來的 0700）重跑 install 也要修好。"""
        self.install()
        tag_dir = self.box.opt(FAKE_TAG)
        os.chmod(tag_dir, 0o700)
        os.chmod(os.path.join(tag_dir, "lgwd", "__main__.py"), 0o600)
        rc, out, err = self.install()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("沿用", out)
        self.assertEqual(stat.S_IMODE(os.stat(tag_dir).st_mode), 0o755)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(tag_dir, "lgwd", "__main__.py")).st_mode),
            0o644)


class UnitFileTests(unittest.TestCase):
    """deploy/units/llmgateway.service 的靜態檢查（#35 spec 的固定值）。"""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "deploy", "units", "llmgateway.service")
        with open(path, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_required_settings(self):
        for needle in (
            "User=llmgw",
            # 中-7：exec 成功才算啟動中（預設 simple 是 fork 完就算）。
            "Type=exec",
            "Restart=on-failure",
            "WantedBy=multi-user.target",
            "LoadCredential=keys.pass:/etc/llmgateway/keys.pass",
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ReadWritePaths=/var/lib/llmgateway",
            "PrivateTmp=true",
            "EnvironmentFile=/etc/llmgateway/llmgateway.env",
            "WorkingDirectory=/opt/llmgateway/current",
            # T2/T3 審查的補充：開機後立刻拉 key，網路沒好會寫下假的失敗。
            "After=network-online.target",
            "Wants=network-online.target",
            # ExecStart 的路徑用新版面（#35）。
            "--policy /opt/llmgateway/current/policy/default.json",
            "--keys /var/lib/llmgateway/keys.env",
            "--db /var/lib/llmgateway/llm.db",
            "--api-port=${LGW_API_PORT}",
        ):
            self.assertIn(needle, self.text, needle)

    def test_not_user_layer(self):
        for needle in ("WantedBy=default.target", "--user", "%h"):
            self.assertNotIn(needle, self.text, needle)

    def test_old_wrong_type_comment_is_gone(self):
        # 之前寫「有 LoadCredential= 時 systemd 用 exec 語意」是錯的（中-7）。
        self.assertNotIn("systemd 用 exec 語意", self.text)


if __name__ == "__main__":
    unittest.main()
