"""lgwctl check／status 的測試：各項 ok／todo／fail、結束碼 0／1／2、
``--no-remote``、``--tag``、keysync 狀態檔（含 T2/T3 審查的規則）、輸出不含值。

全在沙箱裡跑（假 systemctl／假 curl／假 gh／假探針）。
"""

from __future__ import annotations

import json
import os
import time
import unittest

try:
    from .lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        NEW_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        NEW_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )

STATUS_NAME = ".keysync.status"


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.install_state(FAKE_TAG)
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.write_status(ok=True, ts=time.time(), reason="ok")
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")

    def write_status(self, *, ok: bool, ts: float | None, reason: str | None) -> None:
        data = {"ok": ok}
        if ts is not None:
            data["ts"] = ts
        if reason is not None:
            data["reason"] = reason
        self.box.write(self.box.state(STATUS_NAME), json.dumps(data), 0o644)

    def fill_tunnel_config(self) -> None:
        """把隧道設定填好（假的金鑰檔），讓「隧道沒在跑」算 fail 而不是 todo。"""
        identity = os.path.join(self.box.service_home, ".ssh", "id_llmgw_tunnel")
        known_hosts = os.path.join(self.box.service_home, ".ssh", "known_hosts_rendezvous")
        for path in (identity, known_hosts):
            self.box.write(path, "fake\n", 0o600)
        self.box.write(self.box.etc("tunnel.env"), (
            "RENDEZVOUS=user@example:22\n"
            f"IDENTITY={identity}\n"
            f"KNOWN_HOSTS={known_hosts}\n"
        ))

    def check(self, *argv, **env_extra):
        env = dict(self.box.env)
        env.update(env_extra)
        return self.box.run_cli(["check", *argv], env=env)

    # -- 全部都好 -----------------------------------------------------------

    def test_all_ok(self):
        rc, out, err = self.check()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [])
        self.assertEqual(lines_with_level(out, "todo"), [])
        for name in ("llmgateway.service", "llmgateway-tunnel.service", "list-local",
                     "list-remote", "keysync", "policy", "gh-keys-repo"):
            self.assertTrue(has_line(out, name, "ok"), f"{name}: {out}")
        # 本機探針打的是 llmgateway.env 的埠與 list 路徑
        self.assertIn("http://127.0.0.1:8480/v1/subscriptions", self.box.log("curl.log"))
        # 經會合點的探針呼叫當版的 --probe-once，並把設定檔指向新位置
        probe_log = self.box.log("probe.log")
        self.assertIn("probe --probe-once", probe_log)
        self.assertIn("tunnel-env " + self.box.etc("tunnel.env"), probe_log)
        # gh 是以服務帳號身分問 repo（不讀內容）
        self.assertIn(f"api repos/FATESAIKOU/LLMGateway-keys --jq .full_name",
                      self.box.log("gh.log"))
        self.assertIn("-n -u lgwfake", self.box.log("sudo.log"))

    def test_api_port_from_env_file_is_used(self):
        self.box.write(self.box.etc("llmgateway.env"), 'LGW_API_PORT="9999"\n')
        rc, out, err = self.check()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("http://127.0.0.1:9999/v1/subscriptions", self.box.log("curl.log"))

    # -- unit ---------------------------------------------------------------

    def test_units_not_running_is_fail(self):
        self.fill_tunnel_config()
        self.box.set_active("llmgateway.service", False)
        self.box.set_active("llmgateway-tunnel.service", False)
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "llmgateway.service", "fail"), out)
        self.assertTrue(has_line(out, "llmgateway-tunnel.service", "fail"), out)

    def test_tunnel_not_running_without_config_is_todo(self):
        """隧道設定還沒填時，隧道沒在跑算 todo（本人動作），不是 fail。"""
        self.box.set_active("llmgateway-tunnel.service", False)
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "llmgateway-tunnel.service", "todo"), out)
        self.assertEqual(lines_with_level(out, "fail"), [])

    def test_unknown_systemctl_is_todo(self):
        rc, out, err = self.check(LLMGW_SYSTEMCTL=os.path.join(self.box.root, "no-systemctl"))
        self.assertTrue(has_line(out, "llmgateway.service", "todo"), out)
        self.assertEqual(rc, 2, out + err)

    def test_missing_units_are_fail(self):
        """systemctl is-active rc 4＝unit 檔不存在：確定沒裝，是 fail 不是 todo。"""
        self.box.set_missing("llmgateway.service")
        self.box.set_missing("llmgateway-tunnel.service")
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "llmgateway.service", "fail"), out)
        self.assertTrue(has_line(out, "llmgateway-tunnel.service", "fail"), out)
        self.assertIn("沒裝", out)
        self.assertNotIn("不能用", out)   # 不是「systemctl 不能用」的那種 todo

    def test_missing_tunnel_unit_is_fail_even_without_config(self):
        """隧道 unit 不在＝沒裝（install 本來就該放兩個），「設定未填」特例不適用。"""
        self.box.set_missing("llmgateway-tunnel.service")
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "llmgateway-tunnel.service", "fail"), out)

    # -- list ---------------------------------------------------------------

    def test_list_local_not_200_is_fail(self):
        rc, out, err = self.check(FAKE_CURL_CODE="500")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list-local", "fail"), out)
        self.assertIn("500", out)

    def test_list_local_without_curl_is_todo(self):
        rc, out, err = self.check(LLMGW_CURL=os.path.join(self.box.root, "no-curl"))
        self.assertTrue(has_line(out, "list-local", "todo"), out)
        self.assertEqual(rc, 2, out + err)

    def test_list_remote_fail(self):
        rc, out, err = self.check(FAKE_PROBE_RC="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list-remote", "fail"), out)

    def test_list_remote_unavailable_is_todo(self):
        rc, out, err = self.check(FAKE_PROBE_RC="2")
        self.assertTrue(has_line(out, "list-remote", "todo"), out)
        self.assertEqual(rc, 2, out + err)

    def test_no_remote_skips_the_line(self):
        rc, out, err = self.check("--no-remote", FAKE_PROBE_RC="1")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(has_line(out, "list-remote"), out)
        self.assertEqual(self.box.log("probe.log"), "")

    # -- keysync（T2 的狀態檔）---------------------------------------------

    def test_keysync_status_missing_is_todo(self):
        os.unlink(self.box.state(STATUS_NAME))
        rc, out, err = self.check()
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "keysync", "todo"), out)

    def test_keysync_status_not_ok_is_fail(self):
        self.write_status(ok=False, ts=time.time(), reason="gh_auth")
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keysync", "fail"), out)
        self.assertIn("gh_auth", out)

    def test_keysync_status_stale_is_fail(self):
        self.write_status(ok=True, ts=time.time() - 3600, reason="ok")
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keysync", "fail"), out)
        self.assertIn("沒更新", out)

    def test_keysync_status_just_inside_the_window_is_ok(self):
        self.write_status(ok=True, ts=time.time() - 1700, reason="ok")
        rc, out, err = self.check()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "keysync", "ok"), out)

    def test_keysync_status_without_timestamp_is_fail(self):
        self.write_status(ok=True, ts=None, reason="ok")
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keysync", "fail"), out)

    def test_keysync_ok_but_keys_env_missing_is_fail(self):
        os.unlink(self.box.state("keys.env"))
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keysync", "fail"), out)
        self.assertIn("keys.env 不在", out)

    def test_keysync_status_broken_json_is_fail(self):
        self.box.write(self.box.state(STATUS_NAME), "{not json", 0o644)
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keysync", "fail"), out)

    # -- policy 與 tag ------------------------------------------------------

    def test_bad_policy_is_fail(self):
        self.box.write(os.path.join(self.box.opt(FAKE_TAG), "policy", "default.json"),
                       '{"subscriptions": {}}\n')
        rc, out, err = self.check()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "policy", "fail"), out)

    def test_tag_matches(self):
        rc, out, err = self.check("--tag", FAKE_TAG)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "tag", "ok"), out)

    def test_tag_mismatch_is_fail(self):
        rc, out, err = self.check("--tag", NEW_TAG)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "tag", "fail"), out)
        self.assertIn(FAKE_TAG, out)

    # -- gh -----------------------------------------------------------------

    def test_gh_not_logged_in_is_todo(self):
        rc, out, err = self.check(FAKE_GH_API_RC="4",
                                  FAKE_GH_API_ERR="gh: To get started with GitHub CLI, "
                                                  "please run: gh auth login")
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "gh-keys-repo", "todo"), out)

    def test_gh_no_read_permission_is_fail(self):
        rc, out, err = self.check(FAKE_GH_API_RC="1", FAKE_GH_API_ERR="gh: HTTP 404: Not Found")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "gh-keys-repo", "fail"), out)

    def test_gh_other_failure_is_todo(self):
        rc, out, err = self.check(FAKE_GH_API_RC="1", FAKE_GH_API_ERR="gh: network unreachable")
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "gh-keys-repo", "todo"), out)

    def test_gh_missing_is_todo(self):
        rc, out, err = self.check(LLMGW_GH=os.path.join(self.box.root, "no-gh"))
        self.assertEqual(rc, 2, out + err)
        self.assertTrue(has_line(out, "gh-keys-repo", "todo"), out)

    def test_gh_weird_output_is_fail(self):
        rc, out, err = self.check(FAKE_GH_API_OUT="someone/else")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "gh-keys-repo", "fail"), out)

    # -- 輸出不含值 ---------------------------------------------------------

    def test_output_has_no_key_values_or_password(self):
        self.box.write(self.box.etc("keys.pass"),
                       f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        self.box.write(self.box.state("keys.env"),
                       f"OPENROUTER_API_KEY={FAKE_KEY_VALUE}\n", 0o600)
        rc, out, err = self.check()
        rc2, out2, _ = self.box.run_cli(["status"])
        text = out + out2 + err
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertNotIn(FAKE_KEY_VALUE, text)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def test_status_without_install(self):
        rc, out, err = self.box.run_cli(["status"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("（沒有）", out)

    def test_status_shows_tags_and_units(self):
        self.box.install_state(FAKE_TAG, previous="lgw-v1.2.2")
        self.box.set_active("llmgateway.service")
        rc, out, err = self.box.run_cli(["status"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn(FAKE_TAG, out)
        self.assertIn("lgw-v1.2.2", out)
        self.assertIn("llmgateway.service", out)
        self.assertIn("active", out)
        self.assertIn("llmgateway-tunnel.service", out)
        # status 只看狀態，不動任何東西
        self.assertEqual(self.box.actions(), [])
        self.assertEqual(self.box.log("systemctl.log").count("is-active"), 2)

    def test_status_unknown_systemctl(self):
        self.box.install_state(FAKE_TAG)
        rc, out, err = self.box.run_cli(
            ["status"], env={**self.box.env,
                             "LLMGW_SYSTEMCTL": os.path.join(self.box.root, "no-systemctl")})
        self.assertEqual(rc, 0, out + err)
        self.assertIn("unknown", out)

    def test_status_shows_missing(self):
        """unit 檔不在（is-active rc 4）時 status 要顯示 missing，不是 unknown。"""
        self.box.install_state(FAKE_TAG)
        self.box.set_missing("llmgateway.service")
        rc, out, err = self.box.run_cli(["status"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("missing", out)
        self.assertNotIn("unknown", out)

    def test_check_on_an_empty_machine_does_not_crash(self):
        """什麼都還沒裝時 check 要照樣給三態行（policy 一定是 fail），不要 traceback。"""
        rc, out, err = self.box.run_cli(["check", "--no-remote"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "policy", "fail"), out)
        self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
