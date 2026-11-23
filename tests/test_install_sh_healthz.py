"""``deploy/install.sh`` 的 ``check_unit`` 本機探針（#246 T-35／K-10）。

user 層是遷移前的舊佈局，一律 ``old``：先 ``curl /healthz``，**只有** 404 才退回
``curl /v1/subscriptions``；401／503 之類一律失敗，不再打 list。遠端那段（隧道
腳本 ``--probe-once``）不改：user 層沒設 ``LGW_PROBE_API_MODE``，舊腳本會忽略它。

整個 install.sh 在沙箱裡跑：假的 systemctl／curl／gh，服務帳號就是現在的使用者
（不 sudo），解密密碼是自己的假字串。
"""

import os
import pwd
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = os.path.join(REPO_ROOT, "deploy", "install.sh")

FAKE_CURL = """#!/usr/bin/env bash
# 假 curl：依 URL 路徑回碼（$SANDBOX/curl_code.<key>）；$SANDBOX/curl_rc 可以
# 讓它直接失敗（模擬連不上／逾時）。
echo "curl $*" >> "${SANDBOX}/curl-argv.log"
if [[ -f "${SANDBOX}/curl_rc" ]]; then
    exit "$(cat "${SANDBOX}/curl_rc")"
fi
url=""
for a in "$@"; do url="$a"; done
path="${url#*://}"
path="${path#*/}"
key="${path//\\//_}"
printf '%s' "$(cat "${SANDBOX}/curl_code.${key}" 2>/dev/null || echo 200)"
exit 0
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "${SANDBOX}/systemctl.log"
verb=""
for a in "$@"; do
    case "$a" in
        is-active) verb=is-active ;;
        is-failed) verb=is-failed ;;
    esac
done
case "$verb" in
    is-active) exit 0 ;;
    is-failed) exit 1 ;;
    *) exit 0 ;;
esac
"""

FAKE_GH = """#!/usr/bin/env bash
case "${1:-}" in
    auth) exit 0 ;;
    api) printf '%s\\n' "FATESAIKOU/LLMGateway-keys"; exit 0 ;;
    *) exit 0 ;;
esac
"""

FAKE_PROBE_BIN = """#!/usr/bin/env bash
echo "probe $*" >> "${SANDBOX}/probe-argv.log"
exit 0
"""


class CheckUnitProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.sandbox = os.path.join(self.home, "sandbox")
        self.fakebin = os.path.join(self.home, "fakebin")
        os.makedirs(self.fakebin, exist_ok=True)
        for name, text in (("curl", FAKE_CURL), ("systemctl", FAKE_SYSTEMCTL),
                           ("gh", FAKE_GH)):
            path = os.path.join(self.fakebin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(path, 0o755)
        # 隧道探針（BIN_DIR/llmgateway-tunnel）：check_unit 遠端那段會叫它。
        probe = os.path.join(self.home, ".local", "bin", "llmgateway-tunnel")
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(FAKE_PROBE_BIN)
        os.chmod(probe, 0o755)
        # check_keysync 的前置：解密密碼放了（假值）、gh 已登入（假 gh）。
        self.write(os.path.join(self.home, ".config", "llmgateway", "keysync.env"),
                   "LLMGW_KEYS_PASSWORD=FakePassword_DoNotUse_246\n", 0o600)
        self.write(os.path.join(self.home, ".config", "llmgateway", "llmgateway.env"),
                   'LGW_API_PORT="8480"\n', 0o600)

    # -- 工具 ---------------------------------------------------------------

    def write(self, path: str, text: str, mode: int = 0o644) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, mode)

    def code(self, path: str, code) -> None:
        stem = path.lstrip("/").replace("/", "_")
        self.write(os.path.join(self.sandbox, f"curl_code.{stem}"), str(code))

    def run_check(self) -> subprocess.CompletedProcess:
        env = {
            "HOME": self.home,
            "PATH": self.fakebin + ":/usr/bin:/bin",
            "SANDBOX": self.sandbox,
            "LGW_SYSTEMCTL": os.path.join(self.fakebin, "systemctl"),
            "LGW_GH": os.path.join(self.fakebin, "gh"),
            "LGW_SERVICE_USER": pwd.getpwuid(os.geteuid()).pw_name,
        }
        return subprocess.run(
            ["bash", INSTALL_SH, "--check", "--home", self.home],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def curl_paths(self) -> list:
        try:
            with open(os.path.join(self.sandbox, "curl-argv.log"), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return []
        return ["/" + line.split()[-1].split("://", 1)[-1].split("/", 1)[1]
                for line in lines]

    # -- T-35 的四格 --------------------------------------------------------

    def test_healthz_200_is_ok_and_does_not_probe_list(self):
        self.code("/healthz", 200)
        self.code("/v1/subscriptions", 200)
        res = self.run_check()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("本機 healthz 探針回 200", res.stdout)
        self.assertEqual(self.curl_paths(), ["/healthz"])

    def test_healthz_404_falls_back_to_list_200(self):
        self.code("/healthz", 404)
        self.code("/v1/subscriptions", 200)
        res = self.run_check()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("本機 list 探針回 200（old 退回）", res.stdout)
        self.assertEqual(self.curl_paths(), ["/healthz", "/v1/subscriptions"])

    def test_healthz_401_is_fail_without_list(self):
        self.code("/healthz", 401)
        self.code("/v1/subscriptions", 200)
        res = self.run_check()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("本機 healthz 探針回 401", res.stdout)
        self.assertEqual(self.curl_paths(), ["/healthz"])

    def test_curl_failure_is_unconfirmed(self):
        self.write(os.path.join(self.sandbox, "curl_rc"), "7\n")
        res = self.run_check()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("curl 連不上或逾時", res.stdout)
        self.assertEqual(self.curl_paths(), ["/healthz"])

    def test_404_then_list_not_200_is_fail(self):
        """退回 list 之後 list 不是 200：照舊的 1（不是 2）。"""
        self.code("/healthz", 404)
        self.code("/v1/subscriptions", 503)
        res = self.run_check()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(self.curl_paths(), ["/healthz", "/v1/subscriptions"])


if __name__ == "__main__":
    unittest.main()
