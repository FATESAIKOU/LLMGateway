"""lgwctl sync／cli 分派的測試：手動 key 拉取的身分與密碼傳遞、失敗碼、
遷移子指令的預留分派、``python3 -m lgwctl`` 入口。
"""

from __future__ import annotations

import importlib.machinery
import json
import os
import stat
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path

try:
    from .lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        Sandbox,
        current_user,
        has_line,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        Sandbox,
        current_user,
        has_line,
    )

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        # 真機 install 一定會建 state 目錄；sync 跑完要補寫 .keysync.status
        # （低-12：寫不進去＝失敗），沙箱也要有。
        os.makedirs(self.box.state(""), exist_ok=True)

    def write_password(self, value=FAKE_PASSWORD):
        path = self.box.etc("keys.pass")
        self.box.write(path, f"# 假的密碼檔\nLLMGW_KEYS_PASSWORD={value}\n", 0o640)
        return path

    def test_sync_runs_key_sync_with_new_paths(self):
        box = Sandbox(service_user=current_user())   # 已經是服務帳號，不用 sudo
        self.addCleanup(box.close)
        box.install_state(FAKE_TAG)
        os.makedirs(box.state(""), exist_ok=True)
        box.write(box.etc("keys.pass"), f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        rc, out, err = box.run_cli(["sync"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "sync", "ok"), out)
        log = box.log("keysync.log")
        self.assertIn(f"--keys {box.state('keys.env')}", log)
        self.assertIn(f"--code-dir {box.current_link}", log)
        self.assertIn(f"--gh {box.fakebin}/gh", log)
        self.assertIn("password-env yes", log)
        self.assertIn(f"home {box.service_home}", log)
        self.assertEqual(box.log("sudo.log"), "")
        # 密碼不進 argv、不進輸出、不進 log
        self.assertNotIn(FAKE_PASSWORD, log)
        self.assertNotIn(FAKE_PASSWORD, out + err)

    def test_sync_uses_sudo_for_the_service_user(self):
        self.write_password()
        rc, out, err = self.box.run_cli(["sync"])
        self.assertEqual(rc, 0, out + err)
        sudo_log = self.box.log("sudo.log")
        self.assertIn("-n -u lgwfake", sudo_log)
        self.assertIn("key-sync", sudo_log)
        self.assertIn("password-env yes", self.box.log("keysync.log"))
        self.assertNotIn(FAKE_PASSWORD, sudo_log)
        self.assertNotIn(FAKE_PASSWORD, out + err)

    def test_sync_without_password_file(self):
        rc, out, err = self.box.run_cli(["sync"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "sync", "fail"), out)
        self.assertIn("keys.pass", out)
        self.assertEqual(self.box.log("keysync.log"), "")

    def test_sync_without_script(self):
        self.write_password()
        rc, out, err = self.box.run_cli(
            ["sync"], env={**self.box.env,
                          "LLMGW_KEYSYNC_SCRIPT": os.path.join(self.box.root, "no-key-sync")})
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "sync", "fail"), out)
        self.assertIn("no-key-sync", out)

    def test_sync_returns_child_exit_code(self):
        self.write_password()
        rc, out, err = self.box.run_cli(["sync"], env={**self.box.env, "FAKE_KEYSYNC_RC": "9"})
        self.assertEqual(rc, 9)
        self.assertTrue(has_line(out, "sync", "fail"), out)
        self.assertIn("key-sync rc=9", out)
        # 子行程的輸出照原樣轉印
        self.assertIn("key-sync: fake run", out)

    def test_sync_without_filled_password_does_not_invent_one(self):
        self.box.write(self.box.etc("keys.pass"), "# 只有註解\n", 0o640)
        rc, out, err = self.box.run_cli(["sync"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("password-env no", self.box.log("keysync.log"))

    # -- 狀態檔與 restart 提示（中-5）---------------------------------------

    def test_sync_success_writes_status_and_hints_restart(self):
        """成功後補寫 T2 格式的 .keysync.status，並提醒要 restart（中-5）。"""
        self.write_password()
        self.box.write(self.box.state("keys.env"), f"KEY={FAKE_KEY_VALUE}\n", 0o600)
        rc, out, err = self.box.run_cli(["sync"])
        self.assertEqual(rc, 0, out + err)
        text = self.box.read(self.box.state(".keysync.status"))
        status = json.loads(text)
        self.assertTrue(status["ok"])
        self.assertEqual(status["reason"], "ok")
        self.assertLess(abs(status["ts"] - time.time()), 300)
        # 檔裡只有 ts／ok／reason：不含任何值
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertNotIn(FAKE_KEY_VALUE, text)
        # 提示：LoadCredential 是啟動時的快照，要 restart 才換
        self.assertTrue(has_line(out, "restart", "todo"), out)
        self.assertIn("LoadCredential", out)
        # check 看得到手動拉取的結果
        rc2, out2, err2 = self.box.run_cli(["check", "--no-remote"])
        self.assertTrue(has_line(out2, "keysync", "ok"), out2)

    def test_sync_failure_writes_failed_status(self):
        """失敗要寫 ok:false（不能假成功、也不能讓 check 說好）（中-5）。"""
        self.write_password()
        self.box.write(self.box.state("keys.env"), "", 0o600)
        rc, out, err = self.box.run_cli(["sync"],
                                        env={**self.box.env, "FAKE_KEYSYNC_RC": "9"})
        self.assertEqual(rc, 9, out + err)
        status = json.loads(self.box.read(self.box.state(".keysync.status")))
        self.assertFalse(status["ok"])
        self.assertIn("rc=9", status["reason"])
        rc2, out2, err2 = self.box.run_cli(["check", "--no-remote"])
        self.assertTrue(has_line(out2, "keysync", "fail"), out2)

    def test_sync_early_failure_keeps_old_status(self):
        """沒跑到 key-sync（keys.pass 不在）就不動狀態檔（不要假成功，中-5）。"""
        old = json.dumps({"ts": 1, "ok": False, "reason": "old"})
        self.box.write(self.box.state(".keysync.status"), old, 0o644)
        rc, out, err = self.box.run_cli(["sync"])
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.box.read(self.box.state(".keysync.status")), old)

    def test_sync_status_write_failure_is_rc_1(self):
        """key 拉到了但狀態檔寫不進去也是失敗：automation 只看結束碼會以為
        sync 完整成功，check 之後仍看到舊狀態（低-12）。"""
        self.write_password()
        # 讓 .keysync.status 變成目錄：atomic write 的 rename 會 OSError
        os.makedirs(self.box.state(".keysync.status"))
        rc, out, err = self.box.run_cli(["sync"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "sync", "ok"), out)
        self.assertTrue(has_line(out, "status", "fail"), out)

    def test_sync_is_the_only_child_allowed_to_see_the_password(self):
        """child_env 預設移除密碼；sync 明確放行（低-4）。"""
        self.box.write(self.box.etc("keys.pass"), "# 只有註解\n", 0o640)
        env = {**self.box.env, "LLMGW_KEYS_PASSWORD": FAKE_PASSWORD}
        rc, out, err = self.box.run_cli(["sync"], env=env)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("password-env yes", self.box.log("keysync.log"))
        self.assertNotIn(FAKE_PASSWORD, out + err)

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_sync_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        env["LLMGW_SERVICE_HOME"] = self.box.service_home
        rc, out, err = self.box.run_cli(["sync"], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)


class MigrateDispatchTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def test_missing_migrate_module_is_a_clear_error(self):
        # migrate.py 現在在了（成員 C 的檔案）；這條測的是「找不到那個模組」的
        # 分派路徑，所以把 sys.modules 的那一格設成 None（find_spec 就會回 None）。
        sys.modules["lgwctl.migrate"] = None
        self.addCleanup(sys.modules.pop, "lgwctl.migrate", None)
        rc, out, err = self.box.run_cli(["cutover"])
        self.assertEqual(rc, 3)
        self.assertIn("lgwctl/migrate.py", err)
        self.assertEqual(out, "")

    def test_dispatch_to_fake_migrate_module(self):
        calls = []
        fake = types.ModuleType("lgwctl.migrate")
        fake.__spec__ = importlib.machinery.ModuleSpec("lgwctl.migrate", None)
        fake.main = lambda argv, env=None: (calls.append(list(argv)) or 7)
        sys.modules["lgwctl.migrate"] = fake
        self.addCleanup(sys.modules.pop, "lgwctl.migrate", None)
        rc, out, err = self.box.run_cli(["confirm", "--tag", FAKE_TAG])
        self.assertEqual(rc, 7)
        self.assertEqual(calls, [["confirm", "--tag", FAKE_TAG]])

    def test_module_without_main_is_a_clear_error(self):
        fake = types.ModuleType("lgwctl.migrate")
        fake.__spec__ = importlib.machinery.ModuleSpec("lgwctl.migrate", None)
        sys.modules["lgwctl.migrate"] = fake
        self.addCleanup(sys.modules.pop, "lgwctl.migrate", None)
        rc, out, err = self.box.run_cli(["rollback-to-user"])
        self.assertEqual(rc, 3)
        self.assertIn("沒有 main()", err)


class EntryPointTests(unittest.TestCase):
    """python3 -m lgwctl 的入口（cwd 在版本目錄、wrapper 就是這樣跑）。"""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def run_module(self, argv):
        return subprocess.run([sys.executable, "-m", "lgwctl", *argv], cwd=REPO_ROOT,
                              env=self.box.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def test_help(self):
        proc = self.run_module(["--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("install", proc.stdout)

    def test_unknown_subcommand(self):
        proc = self.run_module(["nonsense"])
        self.assertEqual(proc.returncode, 2)

    def test_status_runs(self):
        self.box.install_state(FAKE_TAG)
        proc = self.run_module(["status"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(FAKE_TAG, proc.stdout)

    def test_migrate_entry_point(self):
        # migrate.py 現在在了；這條測的是「模組不在」時入口給的訊息，所以在
        # 子行程裡把 sys.modules 的那一格設成 None（find_spec 就會回 None）。
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['lgwctl.migrate'] = None; "
             "from lgwctl.cli import main; sys.exit(main(['prepare-migration']))"],
            cwd=REPO_ROOT, env=self.box.env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("lgwctl/migrate.py", proc.stderr)

    def test_install_entry_point(self):
        proc = self.run_module(["install", "--tag", FAKE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.lexists(self.box.current_link))
        self.assertTrue(stat.S_IMODE(os.stat(self.box.etc("keys.pass")).st_mode) == 0o640)


if __name__ == "__main__":
    unittest.main()
