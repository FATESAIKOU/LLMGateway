"""遷移各步的模式來源（#246 P-3 呼叫點表）：T-32、T-33。

- T-32：``rollback-to-user`` 探的是 **user 層的舊服務**，一律寫死 ``old``——就算
  ``current`` 是新版（旗標 new）也一樣；遠端探針給隧道腳本的模式也必須是 old。
- T-33：``cutover``／``finalize-migration`` 探的是新版，模式跟著 ``current``。

沙箱沿用 ``test_lgwctl_migrate`` 的假指令組（分 system／user 兩層的假
systemctl、假 systemd-run／runuser…），只把假 curl 換成「依 URL 路徑分開回碼」
的版本、假隧道探針換成會記 ``LGW_PROBE_API_MODE`` 的版本——既有測試那一份不動。
"""

import os
import time
import unittest

try:
    from .lgwctl_fakes import FAKE_CURL
    from .test_lgwctl_migrate import FAKE_TAG, MigrationTestCase, MigrateSandbox
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import FAKE_CURL
    from test_lgwctl_migrate import FAKE_TAG, MigrationTestCase, MigrateSandbox

VALID = '{"schema": 1, "probe": "healthz"}\n'
HEALTH = "/healthz"
LIST = "/v1/subscriptions"

FAKE_TUNNEL_PROBE_MODE = """#!/usr/bin/env bash
# 假隧道探針：記下 argv、LGW_TUNNEL_ENV 與 LGW_PROBE_API_MODE（#246）。
fake="${LGW_FAKE:?}"
printf 'probe %s\\n' "$*" >> "${fake}/order.log"
printf 'probe-env %s\\n' "${LGW_TUNNEL_ENV:-}" >> "${fake}/probe.log"
printf 'api-mode %s\\n' "${LGW_PROBE_API_MODE:-}" >> "${fake}/probe.log"
exit "${FAKE_PROBE_RC:-0}"
"""


class CapabilityMigrateSandbox(MigrateSandbox):
    """MigrateSandbox ＋ 依路徑回碼的假 curl ＋ 會記模式的假隧道探針。"""

    def _write_migrate_tools(self) -> None:
        super()._write_migrate_tools()
        self.write(os.path.join(self.fakebin, "curl"), FAKE_CURL, 0o755)
        self.write(os.path.join(self.template, "ops-scripts", "llmgateway-tunnel"),
                   FAKE_TUNNEL_PROBE_MODE, 0o755)

    def requests(self) -> list:
        paths = []
        for line in self.log_lines("curl.log"):
            url = line.split()[-1]
            paths.append("/" + url.split("://", 1)[-1].split("/", 1)[1])
        return paths

    def api_modes(self) -> list:
        return [line.split(" ", 1)[1] for line in self.log_lines("probe.log")
                if line.startswith("api-mode ")]


class MigrateCapabilityCase(MigrationTestCase):
    def setUp(self):
        # MigrationTestCase 的 setUp 用 MigrateSandbox；這裡換成擴充版。
        self.box = CapabilityMigrateSandbox()
        self.addCleanup(self.box.close)


class RollbackToUserCapabilityTests(MigrateCapabilityCase):
    """T-32：rollback-to-user 一律 old（探 user 層舊服務）。"""

    def setUp(self):
        super().setUp()
        self.box.make_old_layout()
        # current 是**新**版（有旗標 new）：回滾時不能拿 new 去探舊服務。
        self.box.install_new_state(FAKE_TAG)
        self.box.set_capabilities(self.box.opt(FAKE_TAG), VALID)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        for unit in ("llmgateway.service", "llmgateway-tunnel.service", "key-sync.timer"):
            self.box.set_active(unit, False, layer="user")
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG,
                                     revert_unit="lgw-revert.timer",
                                     new_service_ts=time.time() - 3600)
        self.box.make_db(self.box.state("llm.db"), "new-db")
        self.box.write(self.box.state("keys.env"), "UPDATED_KEY=val\n", 0o600)
        self.box.age(self.box.state("keys.env"), -3600)
        # 舊服務（user 層）沒有 /healthz：old 才會 404 之後退回 list。
        self.box.curl_code(HEALTH, 404)
        self.box.curl_code(LIST, 200)

    def test_T32_rollback_to_user_probes_old_even_when_current_is_new(self):
        rc, out, err = self.box.run_migrate(["rollback-to-user", "--inner"])
        self.assertEqual(rc, 0, out + err)
        # 舊服務是 old：先 /healthz（404）再 list（200）
        self.assertEqual(self.box.requests()[:2], [HEALTH, LIST])
        # 遠端探針（經舊隧道探舊服務）給腳本的模式也是 old
        modes = self.box.api_modes()
        self.assertTrue(modes, self.box.log("order.log"))
        self.assertEqual(set(modes), {"old"}, modes)


class CutoverCapabilityTests(MigrateCapabilityCase):
    """T-33：cutover 用 current 的模式（此時 current＝新 tag）。"""

    def setUp(self):
        super().setUp()
        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)
        self.box.set_capabilities(self.box.opt(FAKE_TAG), VALID)

    def test_T33_cutover_uses_the_current_mode(self):
        rc, out, err = self.box.run_migrate(
            ["cutover", "--tag", FAKE_TAG, "--revert-after", "10m", "--inner"])
        self.assertEqual(rc, 0, out + err)
        # 新服務：new 只打 /healthz（200），不退回 list
        self.assertIn(HEALTH, self.box.requests())
        self.assertNotIn(LIST, self.box.requests())
        modes = self.box.api_modes()
        self.assertTrue(modes, self.box.log("order.log"))
        self.assertEqual(set(modes), {"new"}, modes)


class FinalizeCapabilityTests(MigrateCapabilityCase):
    """T-33：finalize 也用 current 的模式。"""

    def setUp(self):
        super().setUp()
        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)
        self.box.set_capabilities(self.box.opt(FAKE_TAG), VALID)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        for unit in ("llmgateway.service", "llmgateway-tunnel.service", "key-sync.timer"):
            self.box.set_active(unit, False, layer="user")
        self.box.write_cutover_state(phase="confirmed", tag=FAKE_TAG,
                                     confirmed_ts=time.time() - 4 * 86400,
                                     revert_unit="lgw-revert.timer",
                                     old_home=self.box.service_home)

    def test_T33_finalize_uses_the_current_mode(self):
        rc, out, err = self.box.run_migrate(["finalize-migration", "--yes"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn(HEALTH, self.box.requests())
        self.assertNotIn(LIST, self.box.requests())
        modes = self.box.api_modes()
        self.assertTrue(modes, self.box.log("order.log"))
        self.assertEqual(set(modes), {"new"}, modes)


if __name__ == "__main__":
    unittest.main()
