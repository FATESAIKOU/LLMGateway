"""lgwctl sync／cli 分派的測試：手動 key 拉取的身分與密碼傳遞、失敗碼、
遷移子指令的預留分派、``python3 -m lgwctl`` 入口。
"""

from __future__ import annotations

import importlib.machinery
import os
import stat
import subprocess
import sys
import types
import unittest
from pathlib import Path

try:
    from .lgwctl_fakes import (
        FAKE_PASSWORD,
        FAKE_TAG,
        Sandbox,
        current_user,
        has_line,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
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

    def write_password(self, value=FAKE_PASSWORD):
        path = self.box.etc("keys.pass")
        self.box.write(path, f"# 假的密碼檔\nLLMGW_KEYS_PASSWORD={value}\n", 0o640)
        return path

    def test_sync_runs_key_sync_with_new_paths(self):
        box = Sandbox(service_user=current_user())   # 已經是服務帳號，不用 sudo
        self.addCleanup(box.close)
        box.install_state(FAKE_TAG)
        box.write(box.etc("keys.pass"), f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        rc, out, err = box.run_cli(["sync"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "sync", "ok"), out)
        log = box.log("keysync.log")
        self.assertIn(f"--keys {box.state('keys.env')}", log)
        self.assertIn(f"--code-dir {box.current_link}", log)
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
        rc, out, err = self.box.run_cli(["cutover"])
        self.assertEqual(rc, 3)
        self.assertIn("lgwctl/migrate.py", err)
        self.assertEqual(out, "")

    def test_dispatch_to_fake_migrate_module(self):
        calls = []
        fake = types.ModuleType("lgwctl.migrate")
        fake.__spec__ = importlib.machinery.ModuleSpec("lgwctl.migrate", None)
        fake.main = lambda argv: (calls.append(list(argv)) or 7)
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
        proc = self.run_module(["prepare-migration"])
        self.assertEqual(proc.returncode, 3)
        self.assertIn("lgwctl/migrate.py", proc.stderr)

    def test_install_entry_point(self):
        proc = self.run_module(["install", "--tag", FAKE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.lexists(self.box.current_link))
        self.assertTrue(stat.S_IMODE(os.stat(self.box.etc("keys.pass")).st_mode) == 0o640)


if __name__ == "__main__":
    unittest.main()
