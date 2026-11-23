"""本機探針與隧道探針的模式（#246 P-1／K-7）：T-10、T-11、T-21。

- T-10：P-3 的 14 格（new／old × 7 種結果）逐格驗結果**與請求序列**。
- T-11：``mode`` 是必填的關鍵字參數（漏了 TypeError、亂給 ValueError）。
- T-21：``probe.remote`` 傳模式給隧道腳本用環境變數，argv 恰好
  ``[probe, "--probe-once"]``（不用 argv 傳，免得舊腳本起出第二個 daemon）。
"""

import os
import unittest

from lgwctl import probe
from lgwctl.layout import Layout

try:
    from .lgwctl_fakes import Sandbox
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import Sandbox

HEALTH = probe.HEALTH_PATH
LIST = probe.LEGACY_PATH


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def layout(self, **env_extra) -> Layout:
        env = dict(self.box.env)
        env.update(env_extra)
        return Layout.from_env(env)

    def requests(self) -> list:
        """假 curl 收到請求的順序（只留 URL 的路徑）。"""
        paths = []
        for line in self.box.log_lines("curl.log"):
            url = line.split()[-1]
            paths.append("/" + url.split("://", 1)[-1].split("/", 1)[1])
        return paths

    def reset(self) -> None:
        """清掉假指令的 log 與依路徑的回碼（每個子測資從乾淨的狀態開始）。"""
        for name in ("curl.log", "probe.log"):
            path = os.path.join(self.box.fake, name)
            if os.path.exists(path):
                os.unlink(path)
        for name in os.listdir(self.box.fake):
            if name.startswith("curl_code"):
                os.unlink(os.path.join(self.box.fake, name))


class LocalProbeMatrixTests(ProbeCase):
    """T-10：P-3 的 14 格。"""

    def cell(self, mode, *, healthz_code=None, list_code=None, rc=None):
        if healthz_code is not None:
            self.box.curl_code(HEALTH, healthz_code)
        if list_code is not None:
            self.box.curl_code(LIST, list_code)
        env_extra = {} if rc is None else {"FAKE_CURL_RC": str(rc)}
        return probe.local_code(self.layout(**env_extra), mode=mode)

    def test_healthz_200_is_healthy_in_both_modes(self):
        for mode in ("new", "old"):
            with self.subTest(mode=mode):
                self.reset()
                self.box.curl_code(HEALTH, 200)
                level, detail = probe.local_code(self.layout(), mode=mode)
                self.assertEqual(level, "ok", detail)
                self.assertIn("healthz 200", detail)
                # 200 不打 list
                self.assertEqual(self.requests(), [HEALTH])

    def test_healthz_404_new_does_not_fall_back(self):
        """new＋404 是失敗，而且**沒有** list 請求。"""
        level, detail = self.cell("new", healthz_code=404, list_code=200)
        self.assertEqual(level, "fail", detail)
        self.assertIn("404", detail)
        self.assertEqual(self.requests(), [HEALTH])

    def test_healthz_404_old_falls_back_to_list_200(self):
        """old＋404 打一次 list；list 200 就健康。"""
        level, detail = self.cell("old", healthz_code=404, list_code=200)
        self.assertEqual(level, "ok", detail)
        self.assertIn("old 退回", detail)
        self.assertEqual(self.requests(), [HEALTH, LIST])

    def test_healthz_404_old_with_list_failure_is_fail(self):
        """old＋404＋list 503 → 失敗（只有 list 200 才健康）。"""
        level, detail = self.cell("old", healthz_code=404, list_code=503)
        self.assertEqual(level, "fail", detail)
        self.assertEqual(self.requests(), [HEALTH, LIST])

    def test_401_503_500_fail_without_list(self):
        """401／503／500：兩種模式都失敗，而且都不打 list。"""
        for mode in ("new", "old"):
            for code in ("401", "503", "500"):
                with self.subTest(mode=mode, code=code):
                    self.reset()
                    level, detail = self.cell(mode, healthz_code=code, list_code=200)
                    self.assertEqual(level, "fail", detail)
                    self.assertIn(code, detail)
                    self.assertEqual(self.requests(), [HEALTH])

    def test_curl_timeout_is_fail_without_list(self):
        """逾時（curl rc 28）→ 失敗，不打 list。"""
        for mode in ("new", "old"):
            with self.subTest(mode=mode):
                self.reset()
                level, detail = self.cell(mode, healthz_code=200, rc=28)
                self.assertEqual(level, "fail", detail)
                self.assertIn("rc=28", detail)
                self.assertEqual(self.requests(), [HEALTH])

    def test_connection_refused_is_fail_without_list(self):
        """連不上（curl rc 7）→ 失敗，不打 list。"""
        for mode in ("new", "old"):
            with self.subTest(mode=mode):
                self.reset()
                level, detail = self.cell(mode, healthz_code=200, rc=7)
                self.assertEqual(level, "fail", detail)
                self.assertIn("rc=7", detail)
                self.assertEqual(self.requests(), [HEALTH])

    def test_missing_curl_is_todo(self):
        """找不到 curl → todo（無法確認），照舊。"""
        layout = self.layout(LLMGW_CURL=os.path.join(self.box.root, "no-curl"))
        level, detail = probe.local_code(layout, mode="old")
        self.assertEqual(level, "todo", detail)
        self.assertEqual(self.requests(), [])

    def test_local_ok_and_wait_use_the_mode(self):
        """local_ok／wait_local_ok 也吃同一個模式（new 404 不會變健康）。"""
        self.box.curl_code(HEALTH, 404)
        self.box.curl_code(LIST, 200)
        self.assertFalse(probe.local_ok(self.layout(), mode="new"))
        self.assertTrue(probe.local_ok(self.layout(), mode="old"))
        self.assertFalse(probe.wait_local_ok(self.layout(), 0, mode="new"))
        self.assertTrue(probe.wait_local_ok(self.layout(), 0, mode="old"))


class ModeIsRequiredTests(ProbeCase):
    """T-11：``mode`` 必填的關鍵字參數（沒有預設值）。"""

    def test_missing_mode_raises_type_error(self):
        layout = self.layout()
        calls = (
            lambda: probe.local_code(layout),
            lambda: probe.local_ok(layout),
            lambda: probe.wait_local_ok(layout, 0),
            lambda: probe.remote_rc(layout),
            lambda: probe.remote(layout),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()

    def test_unknown_mode_raises_value_error(self):
        layout = self.layout()
        calls = (
            lambda: probe.local_code(layout, mode="x"),
            lambda: probe.local_ok(layout, mode="x"),
            lambda: probe.wait_local_ok(layout, 0, mode="x"),
            lambda: probe.remote_rc(layout, mode="x"),
            lambda: probe.remote(layout, mode="x"),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_mode_must_be_keyword(self):
        """模式不能用位置參數傳（契約的 ``*``）：漏了就在呼叫端炸。"""
        with self.assertRaises(TypeError):
            probe.local_code(self.layout(), "old")


class RemoteProbeTests(ProbeCase):
    """T-21：argv 不變、模式走環境變數。"""

    def setUp(self):
        super().setUp()
        self.box.install_state("lgw-v1.2.3")

    def test_T21_argv_is_exactly_probe_once_and_mode_is_in_env(self):
        level, detail = probe.remote(self.layout(), mode="new")
        self.assertEqual(level, "ok", detail)
        lines = self.box.log_lines("probe.log")
        self.assertIn("probe --probe-once", lines)
        # argv 恰好是 --probe-once（沒有 --api-mode 之類的東西）
        self.assertEqual([line for line in lines if line.startswith("probe ")],
                         ["probe --probe-once"])
        self.assertIn("api-mode new", lines)

    def test_old_mode_is_passed_too(self):
        level, _ = probe.remote(self.layout(), mode="old")
        self.assertEqual(level, "ok")
        self.assertIn("api-mode old", self.box.log_lines("probe.log"))

    def test_remote_rc_passes_the_mode_and_the_timeout(self):
        rc = probe.remote_rc(self.layout(), mode="new", timeout=5)
        self.assertEqual(rc, 0)
        self.assertIn("api-mode new", self.box.log_lines("probe.log"))

    def test_remote_rc_reads_the_exit_code(self):
        env = dict(self.box.env)
        env["FAKE_PROBE_RC"] = "2"
        layout = Layout.from_env(env)
        self.assertEqual(probe.remote_rc(layout, mode="old"), 2)
        self.assertEqual(probe.remote(layout, mode="old")[0], "todo")

    def test_missing_probe_is_todo(self):
        env = dict(self.box.env)
        os.unlink(os.path.join(self.box.opt("lgw-v1.2.3"), "ops-scripts",
                               "llmgateway-tunnel"))
        self.assertEqual(probe.remote(Layout.from_env(env), mode="new")[0], "todo")
        self.assertIsNone(probe.remote_rc(Layout.from_env(env), mode="new"))


if __name__ == "__main__":
    unittest.main()
