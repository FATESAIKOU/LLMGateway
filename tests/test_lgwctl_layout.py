"""lgwctl/layout.py 與 report.py 的測試：路徑（LLMGW_PREFIX）、tag 格式、
設定檔解析、api_port、隧道設定狀態、三態行與結束碼。

只用暫存目錄與假值，不碰真機、不讀真憑證。
"""

from __future__ import annotations

import os
import tempfile
import unittest

try:
    from .lgwctl_fakes import FAKE_KEY_VALUE, FAKE_PASSWORD, Sandbox
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import FAKE_KEY_VALUE, FAKE_PASSWORD, Sandbox

from lgwctl.layout import (
    DEFAULT_API_PORT,
    DEFAULT_SERVICE_USER,
    KEYS_REPO,
    TAG_RE,
    Layout,
    parse_env_file,
)
from lgwctl.report import Report


class DefaultLayoutTests(unittest.TestCase):
    """沒有 LLMGW_PREFIX 時，所有路徑都是規格的固定值。"""

    def setUp(self):
        self.layout = Layout.from_env({})

    def test_fixed_paths(self):
        self.assertEqual(self.layout.opt_dir, "/opt/llmgateway")
        self.assertEqual(self.layout.current_link, "/opt/llmgateway/current")
        self.assertEqual(self.layout.previous_link, "/opt/llmgateway/previous")
        self.assertEqual(self.layout.etc_dir, "/etc/llmgateway")
        self.assertEqual(self.layout.env_file, "/etc/llmgateway/llmgateway.env")
        self.assertEqual(self.layout.tunnel_env_file, "/etc/llmgateway/tunnel.env")
        self.assertEqual(self.layout.keys_pass_file, "/etc/llmgateway/keys.pass")
        self.assertEqual(self.layout.state_dir, "/var/lib/llmgateway")
        self.assertEqual(self.layout.keys_env, "/var/lib/llmgateway/keys.env")
        self.assertEqual(self.layout.db_file, "/var/lib/llmgateway/llm.db")
        self.assertEqual(self.layout.keysync_status_file,
                         "/var/lib/llmgateway/.keysync.status")
        self.assertEqual(self.layout.unit_dir, "/etc/systemd/system")
        self.assertEqual(self.layout.unit_file("llmgateway.service"),
                         "/etc/systemd/system/llmgateway.service")
        self.assertEqual(self.layout.bin_dir, "/usr/local/bin")
        self.assertEqual(self.layout.tag_dir("lgw-v1.2.3"),
                         "/opt/llmgateway/lgw-v1.2.3")

    def test_defaults(self):
        self.assertEqual(self.layout.prefix, "")
        self.assertEqual(self.layout.service_user, DEFAULT_SERVICE_USER)
        self.assertEqual(self.layout.api_port(), DEFAULT_API_PORT)
        self.assertEqual(self.layout.list_wait, 30.0)
        self.assertEqual(self.layout.repo, "https://github.com/FATESAIKOU/LLMGateway")
        self.assertIsNone(self.layout.current_tag())
        self.assertIsNone(self.layout.previous_tag())

    def test_tag_pattern(self):
        for tag in ("lgw-v0.1.5", "lgw-v12.345.6789", "lgw-v1.0.0"):
            self.assertTrue(TAG_RE.match(tag), tag)
        for tag in ("", "v1.0.0", "lgw-v1.0", "lgw-v1.0.0.1", "lgw-v1.0.0-rc1",
                    "lgw-v12345.0.0", "lgw-v1.0.0 "):
            self.assertFalse(TAG_RE.match(tag), tag)


class PrefixLayoutTests(unittest.TestCase):
    def test_prefix_prefixes_every_path(self):
        layout = Layout.from_env({"LLMGW_PREFIX": "/tmp/sbx"})
        self.assertEqual(layout.opt_dir, "/tmp/sbx/opt/llmgateway")
        self.assertEqual(layout.etc_dir, "/tmp/sbx/etc/llmgateway")
        self.assertEqual(layout.state_dir, "/tmp/sbx/var/lib/llmgateway")
        self.assertEqual(layout.unit_dir, "/tmp/sbx/etc/systemd/system")
        self.assertEqual(layout.bin_dir, "/tmp/sbx/usr/local/bin")

    def test_prefix_trailing_slash_and_root(self):
        self.assertEqual(Layout.from_env({"LLMGW_PREFIX": "/tmp/sbx/"}).opt_dir,
                         "/tmp/sbx/opt/llmgateway")
        self.assertEqual(Layout.from_env({"LLMGW_PREFIX": "/"}).opt_dir,
                         "/opt/llmgateway")

    def test_tool_overrides(self):
        layout = Layout.from_env({
            "LLMGW_SYSTEMCTL": "/fake/systemctl",
            "LLMGW_GIT": "/fake/git",
            "LLMGW_CURL": "/fake/curl",
            "LLMGW_GH": "/fake/gh",
            "LLMGW_SUDO": "/fake/sudo",
            "LLMGW_SERVICE_USER": "someone",
            "LLMGW_LIST_WAIT": "2.5",
            "LLMGW_REPO": "/local/repo",
            "LLMGW_KEYSYNC_SCRIPT": "/x/key-sync",
        })
        self.assertEqual(layout.systemctl, "/fake/systemctl")
        self.assertEqual(layout.git, "/fake/git")
        self.assertEqual(layout.curl, "/fake/curl")
        self.assertEqual(layout.gh, "/fake/gh")
        self.assertEqual(layout.sudo, "/fake/sudo")
        self.assertEqual(layout.service_user, "someone")
        self.assertEqual(layout.list_wait, 2.5)
        self.assertEqual(layout.repo, "/local/repo")
        self.assertEqual(layout.keysync_script, "/x/key-sync")

    def test_bad_list_wait_falls_back(self):
        self.assertEqual(Layout.from_env({"LLMGW_LIST_WAIT": "abc"}).list_wait, 30.0)


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.layout = Layout.from_env({
            "LLMGW_PREFIX": self.tmp.name,
            "LLMGW_SERVICE_HOME": os.path.join(self.tmp.name, "home"),
        })

    def test_parse_env_file(self):
        path = os.path.join(self.tmp.name, "env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 註解\n\nLGW_API_PORT=9999\nexport OTHER='a b'\nBARE=\"x\"\n")
        values = parse_env_file(path)
        self.assertEqual(values["LGW_API_PORT"], "9999")
        self.assertEqual(values["OTHER"], "a b")
        self.assertEqual(values["BARE"], "x")
        self.assertEqual(parse_env_file(os.path.join(self.tmp.name, "missing")), {})

    def test_api_port_from_env_file(self):
        os.makedirs(self.layout.etc_dir, exist_ok=True)
        with open(self.layout.env_file, "w", encoding="utf-8") as fh:
            fh.write('LGW_API_PORT="9000"\n')
        self.assertEqual(self.layout.api_port(), 9000)
        with open(self.layout.env_file, "w", encoding="utf-8") as fh:
            fh.write("LGW_API_PORT=not-a-port\n")
        self.assertEqual(self.layout.api_port(), DEFAULT_API_PORT)
        with open(self.layout.env_file, "w", encoding="utf-8") as fh:
            fh.write("LGW_API_PORT=70000\n")
        self.assertEqual(self.layout.api_port(), DEFAULT_API_PORT)

    def test_tunnel_config_state(self):
        os.makedirs(self.layout.etc_dir, exist_ok=True)
        ready, detail = self.layout.tunnel_config_state()
        self.assertFalse(ready)
        self.assertIn("RENDEZVOUS", detail)
        identity = os.path.join(self.layout.service_home, ".ssh", "id_llmgw_tunnel")
        known_hosts = os.path.join(self.layout.service_home, ".ssh", "known_hosts_rendezvous")
        with open(self.layout.tunnel_env_file, "w", encoding="utf-8") as fh:
            fh.write("RENDEZVOUS=user@host:22\n")
            fh.write(f"IDENTITY={identity}\n")
            fh.write(f"KNOWN_HOSTS={known_hosts}\n")
        ready, detail = self.layout.tunnel_config_state()
        self.assertFalse(ready)
        self.assertIn("IDENTITY（檔案不在）", detail)
        os.makedirs(os.path.dirname(identity), exist_ok=True)
        for path in (identity, known_hosts):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x\n")
        ready, detail = self.layout.tunnel_config_state()
        self.assertTrue(ready, detail)

    def test_installed_tag_and_links(self):
        os.makedirs(self.layout.opt_dir, exist_ok=True)
        tag_dir = self.layout.tag_dir("lgw-v1.2.3")
        os.makedirs(tag_dir)
        with open(os.path.join(tag_dir, ".lgw-tag"), "w", encoding="utf-8") as fh:
            fh.write("lgw-v1.2.3\n")
        os.symlink(tag_dir, self.layout.current_link)
        os.symlink(self.layout.tag_dir("lgw-v1.2.2"), self.layout.previous_link)
        self.assertEqual(self.layout.current_tag(), "lgw-v1.2.3")
        self.assertEqual(self.layout.previous_tag(), "lgw-v1.2.2")
        self.assertEqual(self.layout.installed_tag(), "lgw-v1.2.3")
        with open(os.path.join(tag_dir, ".lgw-tag"), "w", encoding="utf-8") as fh:
            fh.write("lgw-v9.9.9\n")
        self.assertEqual(self.layout.installed_tag(), "lgw-v9.9.9")

    def test_child_env(self):
        layout = Layout.from_env({"LLMGW_PREFIX": self.tmp.name, "FOO": "1"})
        child = layout.child_env(HOME="/h", LGW_TUNNEL_ENV=None)
        self.assertEqual(child["FOO"], "1")
        self.assertEqual(child["HOME"], "/h")
        self.assertNotIn("LGW_TUNNEL_ENV", child)

    def test_child_env_drops_keys_password(self):
        """解密密碼一律不進子行程，除非明確指名（低-4）。"""
        layout = Layout.from_env({"LLMGW_PREFIX": self.tmp.name,
                                  "FOO": "1",
                                  "LLMGW_KEYS_PASSWORD": "ExportedFakePassword"})
        self.assertNotIn("LLMGW_KEYS_PASSWORD", layout.child_env())
        self.assertEqual(layout.child_env(LLMGW_KEYS_PASSWORD="x")["LLMGW_KEYS_PASSWORD"],
                         "x")


class ReportTests(unittest.TestCase):
    def test_lines_and_details(self):
        lines = []
        report = Report(out=lines.append)
        report.ok("policy")
        report.todo("keys-pass", "填值")
        report.fail("start", "rc=1")
        self.assertEqual(lines, ["ok policy", "todo keys-pass（填值）", "fail start（rc=1）"])
        self.assertTrue(report.has_fail)
        self.assertTrue(report.has_todo)
        self.assertEqual(report.exit_code(), 1)

    def test_exit_codes(self):
        self.assertEqual(Report(out=lambda line: None).exit_code(), 0)
        only_todo = Report(out=lambda line: None)
        only_todo.todo("x")
        self.assertEqual(only_todo.exit_code(), 2)
        self.assertEqual(only_todo.exit_code(todo_ok=True), 0)
        only_ok = Report(out=lambda line: None)
        only_ok.ok("x")
        self.assertEqual(only_ok.exit_code(), 0)

    def test_level_of(self):
        report = Report(out=lambda line: None)
        report.fail("keysync", "壞了")
        report.ok("keysync", "好了")
        self.assertEqual(report.level_of("keysync"), "ok")
        self.assertIsNone(report.level_of("nothing"))

    def test_secrets_are_not_reported(self):
        """三態行只寫有無與種類；這裡放的是假值，證明輸出不含值。"""
        lines = []
        report = Report(out=lines.append)
        report.todo("keys-pass", "把 LLMGW_KEYS_PASSWORD 填進去")
        report.ok("keysync", "keys.env 12s前")
        text = "\n".join(lines)
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertNotIn(FAKE_KEY_VALUE, text)
        self.assertNotIn(KEYS_REPO + "x", text)


class SandboxSmokeTests(unittest.TestCase):
    def test_fakes_are_wired(self):
        with Sandbox() as box:
            self.assertTrue(os.path.isdir(box.fakebin))
            self.assertTrue(os.path.isfile(os.path.join(box.fakebin, "systemctl")))


if __name__ == "__main__":
    unittest.main()
