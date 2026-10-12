import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = os.path.join(REPO_ROOT, "deploy", "install.sh")
RUN_WRAPPER = os.path.join(REPO_ROOT, "deploy", "llmgateway-run")
SERVICE_UNIT = os.path.join(REPO_ROOT, "deploy", "llmgateway.service")
TUNNEL_DAEMON = os.path.join(REPO_ROOT, "deploy", "llmgateway-tunnel")
TUNNEL_UNIT = os.path.join(REPO_ROOT, "deploy", "llmgateway-tunnel.service")

TOOLS_LIST = [
    "mkdir", "mktemp", "chmod", "mv", "ln", "rm", "cat", "date",
    "whoami", "cp", "bash", "head", "grep", "ls", "readlink",
    "cmp", "stat", "dirname", "sort", "tail"
]


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sandbox = self.tmpdir.name
        self.home = os.path.join(self.sandbox, "home")
        os.makedirs(self.home, exist_ok=True)

        self.fakebin = os.path.join(self.sandbox, "fakebin")
        self.fakebin_nopy = os.path.join(self.sandbox, "fakebin_nopy")
        self.fakebin_nosys = os.path.join(self.sandbox, "fakebin_nosys")
        self.tools = os.path.join(self.sandbox, "tools")
        os.makedirs(self.fakebin, exist_ok=True)
        os.makedirs(self.fakebin_nopy, exist_ok=True)
        os.makedirs(self.fakebin_nosys, exist_ok=True)
        os.makedirs(self.tools, exist_ok=True)

        for t in TOOLS_LIST:
            p = shutil.which(t)
            if not p:
                raise RuntimeError(f"missing system tool {t}")
            os.symlink(p, os.path.join(self.tools, t))

        self.probe_ssh = os.path.join(self.sandbox, "fake-ssh-probe")
        self.probe_ssh_log = os.path.join(self.sandbox, "probe-ssh-argv.log")

        self._create_fake_tools()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_fake_tools(self):
        git_script = """#!/usr/bin/env bash
echo "git $*" >> "${SANDBOX:?}/git-argv.log"
if [[ "${GIT_FAIL:-0}" == "1" ]]; then
    echo "fake git: forced failure" >&2
    exit 1
fi
if [[ "${1:-}" != "clone" ]]; then
    echo "fake git: only clone" >&2
    exit 1
fi
dest="${@: -1}"
mkdir -p "$dest/policy" "$dest/deploy"
printf '{}\\n' > "$dest/policy/default.json"
if [[ -n "${WRAPPER_MARKER:-}" ]]; then
    printf '#!/bin/sh\\n# %s\\nexit 0\\n' "$WRAPPER_MARKER" > "$dest/deploy/llmgateway-run"
else
    cat "${REAL_WRAPPER:?}" > "$dest/deploy/llmgateway-run"
fi
chmod +x "$dest/deploy/llmgateway-run"
if [[ -n "${SERVICE_MARKER:-}" ]]; then
    printf '[Unit]\\n# %s\\n' "$SERVICE_MARKER" > "$dest/deploy/llmgateway.service"
else
    cat "${REAL_SERVICE:?}" > "$dest/deploy/llmgateway.service"
fi
if [[ -n "${TUNNEL_MARKER:-}" ]]; then
    printf '#!/bin/sh\\n# %s\\nexit 0\\n' "$TUNNEL_MARKER" > "$dest/deploy/llmgateway-tunnel"
else
    cat "${REAL_TUNNEL:?}" > "$dest/deploy/llmgateway-tunnel"
fi
chmod +x "$dest/deploy/llmgateway-tunnel"
if [[ -n "${TUNNEL_UNIT_MARKER:-}" ]]; then
    printf '[Unit]\\n# %s\\n' "$TUNNEL_UNIT_MARKER" > "$dest/deploy/llmgateway-tunnel.service"
else
    cat "${REAL_TUNNEL_UNIT:?}" > "$dest/deploy/llmgateway-tunnel.service"
fi
exit 0
"""
        py_script = """#!/usr/bin/env bash
echo "python3 $*" >> "${SANDBOX:?}/python-argv.log"
if [[ "${1:-}" == "--version" ]]; then
    echo "Python ${FAKE_PY_VERSION:-3.12.0}"
    exit 0
fi
# 其他呼叫照真 python3 執行（隧道執行檔的 shebang 會走到這裡）。
exec "${REAL_PYTHON:?}" "$@"
"""
        sys_script = """#!/usr/bin/env bash
echo "systemctl $*" >> "${SANDBOX:?}/systemctl-argv.log"
if [[ "$*" == *"is-active"* ]]; then
    if [[ "${SYS_NOBUS:-0}" == "1" ]]; then
        echo "fake systemctl: no bus" >&2
        exit 1
    fi
    state="${SYS_STATE:-inactive}"
    if [[ "$*" == *"llmgateway-tunnel.service"* ]]; then
        state="${SYS_TUNNEL_STATE:-$state}"
    elif [[ "$*" == *"llmgateway-tunnel-restart.timer"* ]]; then
        state="${SYS_TIMER_STATE:-inactive}"
    fi
    if [[ "$state" == "active" ]]; then
        exit 0
    else
        exit 3
    fi
fi
exit 0
"""
        systemd_run_script = """#!/usr/bin/env bash
echo "systemd-run $*" >> "${SANDBOX:?}/systemd-run-argv.log"
if [[ "${SYSTEMD_RUN_FAIL:-0}" == "1" ]]; then
    echo "fake systemd-run: failed" >&2
    exit 1
fi
exit 0
"""
        curl_script = """#!/usr/bin/env bash
echo "curl $*" >> "${SANDBOX:?}/curl-argv.log"
printf '%s' "${CURL_CODE:-200}"
exit "${CURL_RC:-0}"
"""
        probe_ssh_script = """#!/usr/bin/env bash
printf 'argv: %s\\n' "$*" >> "${PROBE_SSH_LOG:?}"
request="$(cat)"
printf 'request: %s\\n' "$request" >> "${PROBE_SSH_LOG}"
case "${FAKE_PROBE_CODE:-200}" in
    200) printf 'HTTP/1.0 200 OK\\r\\n\\r\\n' ;;
    503) printf 'HTTP/1.0 503 Service Unavailable\\r\\n\\r\\n' ;;
    none) ;;
esac
exit 0
"""
        with open(self.probe_ssh, "w", encoding="utf-8") as f:
            f.write(probe_ssh_script)
        os.chmod(self.probe_ssh, 0o755)

        for name, content in [
            ("git", git_script),
            ("python3", py_script),
            ("systemctl", sys_script),
            ("systemd-run", systemd_run_script),
            ("curl", curl_script),
        ]:
            target = os.path.join(self.fakebin, name)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(target, 0o755)

        # nopy: symlink git, systemctl, systemd-run, curl
        for name in ["git", "systemctl", "systemd-run", "curl"]:
            os.symlink(os.path.join(self.fakebin, name), os.path.join(self.fakebin_nopy, name))

        # nosys: symlink git, python3, curl（沒有 systemctl）
        for name in ["git", "python3", "curl"]:
            os.symlink(os.path.join(self.fakebin, name), os.path.join(self.fakebin_nosys, name))

    def run_install(self, args, home=None, env_overrides=None, path_mode="full"):
        if home is None:
            home = self.home
        env = {
            "SANDBOX": self.sandbox,
            "HOME": home,
            "REAL_WRAPPER": RUN_WRAPPER,
            "REAL_SERVICE": SERVICE_UNIT,
            "REAL_TUNNEL": TUNNEL_DAEMON,
            "REAL_TUNNEL_UNIT": TUNNEL_UNIT,
            "REAL_PYTHON": sys.executable,
            "PROBE_SSH_LOG": self.probe_ssh_log,
            "FAKE_PROBE_CODE": "200",
        }
        if path_mode == "full":
            env["PATH"] = f"{self.fakebin}:{self.tools}"
            env["LGW_SYSTEMCTL"] = os.path.join(self.fakebin, "systemctl")
            env["LGW_SYSTEMD_RUN"] = os.path.join(self.fakebin, "systemd-run")
        elif path_mode == "nopy":
            env["PATH"] = f"{self.fakebin_nopy}:{self.tools}"
            env["LGW_SYSTEMCTL"] = os.path.join(self.fakebin_nopy, "systemctl")
            env["LGW_SYSTEMD_RUN"] = os.path.join(self.fakebin_nopy, "systemd-run")
        elif path_mode == "nosys":
            env["PATH"] = f"{self.fakebin_nosys}:{self.tools}"
        if env_overrides:
            env.update(env_overrides)
        cmd = ["bash", INSTALL_SH] + list(args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True)

    def read_log(self, name):
        log_path = os.path.join(self.sandbox, f"{name}-argv.log")
        if not os.path.exists(log_path):
            return ""
        with open(log_path, "r", encoding="utf-8") as f:
            return f.read()

    def write_probe_config(self, home=None, **overrides):
        if home is None:
            home = self.home
        conf_dir = os.path.join(home, ".config", "llmgateway")
        os.makedirs(conf_dir, exist_ok=True)
        values = {
            "RENDEZVOUS": "fakeuser@rendezvous.invalid:2200",
            "IDENTITY": "/tmp/does-not-exist/id_fake_tunnel",
            "KNOWN_HOSTS": "/tmp/does-not-exist/known_hosts_fake",
            "SSH_PATH": self.probe_ssh,
        }
        values.update(overrides)
        with open(os.path.join(conf_dir, "tunnel.env"), "w", encoding="utf-8") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")

    def test_prerequisites_and_file_modes(self):
        self.assertTrue(os.path.isfile(INSTALL_SH))
        self.assertTrue(os.access(INSTALL_SH, os.X_OK))

        self.assertTrue(os.path.isfile(RUN_WRAPPER))
        self.assertTrue(os.access(RUN_WRAPPER, os.X_OK))

        self.assertTrue(os.path.isfile(SERVICE_UNIT))
        with open(SERVICE_UNIT, "r", encoding="utf-8") as f:
            svc_content = f.read()
        self.assertIn("ExecStart=%h/.local/bin/llmgateway-run", svc_content)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", svc_content)
        self.assertNotIn(".mylinuxpool", svc_content)

        self.assertTrue(os.path.isfile(TUNNEL_DAEMON))
        self.assertTrue(os.access(TUNNEL_DAEMON, os.X_OK))
        with open(TUNNEL_DAEMON, "r", encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), "#!/usr/bin/env python3")

        self.assertTrue(os.path.isfile(TUNNEL_UNIT))
        with open(TUNNEL_UNIT, "r", encoding="utf-8") as f:
            tunnel_content = f.read()
        self.assertIn("ExecStart=%h/.local/bin/llmgateway-tunnel", tunnel_content)
        self.assertIn("After=llmgateway.service", tunnel_content)
        self.assertIn("Restart=always", tunnel_content)
        self.assertIn("RestartSec=10", tunnel_content)
        self.assertIn("RestartPreventExitStatus=78", tunnel_content)

        # bash -n syntax checks
        res = subprocess.run(["bash", "-n", INSTALL_SH], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = subprocess.run(["bash", "-n", RUN_WRAPPER], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        res = subprocess.run(
            [sys.executable, "-m", "py_compile", TUNNEL_DAEMON],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_first_install(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        env_file = os.path.join(self.home, ".config", "llmgateway", "llmgateway.env")
        self.assertTrue(os.path.isfile(env_file))
        with open(env_file, "r", encoding="utf-8") as f:
            env_content = f.read()
        self.assertIn('LGW_API_PORT="8480"', env_content)
        self.assertNotIn("LGW_API_HOSTS", env_content)
        self.assertNotIn("LGW_BOARD_PORT", env_content)
        self.assertNotIn("LGW_USAGE", env_content)

        tunnel_env_file = os.path.join(self.home, ".config", "llmgateway", "tunnel.env")
        self.assertTrue(os.path.isfile(tunnel_env_file))
        with open(tunnel_env_file, "r", encoding="utf-8") as f:
            tunnel_env_content = f.read()
        for key in ["RENDEZVOUS", "IDENTITY", "KNOWN_HOSTS", "REMOTE_API", "REMOTE_SSH", "LOCAL_API", "PROBE_INTERVAL", "PROBE_FAILS"]:
            self.assertIn(f"{key}=\n", tunnel_env_content)
        self.assertNotIn("sshproxy@", tunnel_env_content)

        state_dir = os.path.join(self.home, ".local", "state", "llmgateway")
        self.assertTrue(os.path.isdir(state_dir))
        self.assertEqual(stat.S_IMODE(os.stat(state_dir).st_mode), 0o700)

        keys_file = os.path.join(state_dir, "keys.env")
        self.assertTrue(os.path.isfile(keys_file))
        self.assertEqual(os.path.getsize(keys_file), 0)
        self.assertEqual(stat.S_IMODE(os.stat(keys_file).st_mode), 0o600)

        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        current_link = os.path.join(share_dir, "current")
        self.assertTrue(os.path.islink(current_link))
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        tag_file = os.path.join(current_link, ".lgw-tag")
        self.assertTrue(os.path.isfile(tag_file))
        with open(tag_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "lgw-v0.1.0")

        installed_run = os.path.join(self.home, ".local", "bin", "llmgateway-run")
        self.assertTrue(os.path.isfile(installed_run))
        self.assertTrue(os.access(installed_run, os.X_OK))

        installed_svc = os.path.join(self.home, ".config", "systemd", "user", "llmgateway.service")
        self.assertTrue(os.path.isfile(installed_svc))

        installed_tunnel = os.path.join(self.home, ".local", "bin", "llmgateway-tunnel")
        self.assertTrue(os.path.isfile(installed_tunnel))
        self.assertTrue(os.access(installed_tunnel, os.X_OK))

        installed_tunnel_unit = os.path.join(
            self.home, ".config", "systemd", "user", "llmgateway-tunnel.service"
        )
        self.assertTrue(os.path.isfile(installed_tunnel_unit))

        git_log = self.read_log("git")
        self.assertIn("clone --depth 1 --branch lgw-v0.1.0 https://github.com/FATESAIKOU/LLMGateway", git_log)

        sys_log = self.read_log("systemctl")
        self.assertIn("daemon-reload", sys_log)
        self.assertIn("enable --now llmgateway.service", sys_log)
        self.assertIn("enable llmgateway-tunnel.service", sys_log)
        self.assertIn("start llmgateway-tunnel.service", sys_log)
        self.assertNotIn("stop llmgateway-tunnel.service", sys_log)
        self.assertNotIn("restart llmgateway-tunnel.service", sys_log)

        leftovers = [f for f in os.listdir(share_dir) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_second_install_preserves_env_and_keys(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        env_file = os.path.join(self.home, ".config", "llmgateway", "llmgateway.env")
        custom_env = 'LGW_API_PORT="9999"\n'
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(custom_env)

        tunnel_env_file = os.path.join(self.home, ".config", "llmgateway", "tunnel.env")
        custom_tunnel_env = "RENDEZVOUS=sshproxy@example.invalid:22\nIDENTITY=/tmp/id\nKNOWN_HOSTS=/tmp/kh\n"
        with open(tunnel_env_file, "w", encoding="utf-8") as f:
            f.write(custom_tunnel_env)

        keys_file = os.path.join(self.home, ".local", "state", "llmgateway", "keys.env")
        custom_keys = "SOME_KEY=fake-not-real\n"
        with open(keys_file, "w", encoding="utf-8") as f:
            f.write(custom_keys)

        res2 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home], env_overrides={"SYS_STATE": "active"})
        self.assertEqual(res2.returncode, 0, res2.stderr)

        with open(env_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_env)
        with open(tunnel_env_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_tunnel_env)
        with open(keys_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_keys)

        sys_log = self.read_log("systemctl")
        self.assertIn("restart llmgateway.service", sys_log)
        self.assertNotIn("restart llmgateway-tunnel.service", sys_log)
        self.assertNotIn("stop llmgateway-tunnel.service", sys_log)

    def test_reinstall_same_tag_reuses_without_git_clone(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        tag_dir = os.path.join(self.home, ".local", "share", "llmgateway", "lgw-v0.1.0")
        marker = os.path.join(tag_dir, "MARKER-reuse")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("marker")

        git_log_file = os.path.join(self.sandbox, "git-argv.log")
        if os.path.exists(git_log_file):
            os.remove(git_log_file)

        res2 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home], env_overrides={"SYS_STATE": "active"})
        self.assertEqual(res2.returncode, 0, res2.stderr)

        self.assertFalse(os.path.exists(git_log_file))
        self.assertTrue(os.path.exists(marker))

    def test_git_clone_failure_leaves_no_leftovers_and_keeps_current(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        res2 = self.run_install(["--tag", "lgw-v0.2.0", "--home", self.home], env_overrides={"GIT_FAIL": "1"})
        self.assertNotEqual(res2.returncode, 0)

        current_link = os.path.join(share_dir, "current")
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))
        self.assertFalse(os.path.exists(os.path.join(share_dir, "lgw-v0.2.0")))
        leftovers = [f for f in os.listdir(share_dir) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_symlink_atomic_switch(self):
        res1 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res1.returncode, 0, res1.stderr)

        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        current_link = os.path.join(share_dir, "current")
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        res2 = self.run_install(["--tag", "lgw-v0.2.0", "--home", self.home])
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.2.0"))

        res3 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res3.returncode, 0, res3.stderr)
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        leftovers = [f for f in os.listdir(share_dir) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_check_three_states_with_tag(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.write_probe_config()

        # 0: 兩個 unit 都 active、tag 相符、本機與經會合點 list 探針都回 200
        res_ok = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0", "FAKE_PROBE_CODE": "200"}
        )
        self.assertEqual(res_ok.returncode, 0, res_ok.stdout + res_ok.stderr)
        self.assertIn("本機", res_ok.stdout)
        self.assertIn("經會合點", res_ok.stdout)

        curl_log = self.read_log("curl")
        self.assertIn("/v1/subscriptions", curl_log)
        self.assertNotIn("/v1/board", curl_log)
        self.assertNotIn("Authorization", curl_log)

        probe_log = self.read_log("probe-ssh")
        self.assertIn("-W 127.0.0.1:4710", probe_log)
        self.assertIn("GET /v1/subscriptions HTTP/1.0", probe_log)

        # 1: 本機 200 但經會合點不是 200
        res_probe_503 = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0", "FAKE_PROBE_CODE": "503"}
        )
        self.assertEqual(res_probe_503.returncode, 1, res_probe_503.stdout + res_probe_503.stderr)
        self.assertIn("經會合點", res_probe_503.stdout)

        # 1: 經會合點沒回應（空輸出）
        res_probe_none = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0", "FAKE_PROBE_CODE": "none"}
        )
        self.assertEqual(res_probe_none.returncode, 1, res_probe_none.stdout + res_probe_none.stderr)

        # 2: 經會合點的設定不能用（tunnel.env 必填是空的）
        tunnel_env = os.path.join(self.home, ".config", "llmgateway", "tunnel.env")
        with open(tunnel_env, "w", encoding="utf-8") as f:
            f.write("RENDEZVOUS=\nIDENTITY=\nKNOWN_HOSTS=\n")
        res_probe_cfg = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0"}
        )
        self.assertEqual(res_probe_cfg.returncode, 2, res_probe_cfg.stdout + res_probe_cfg.stderr)

        # 2: 找不到 ssh
        self.write_probe_config(SSH_PATH="/nonexistent/no-such-ssh")
        res_probe_ssh = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0"}
        )
        self.assertEqual(res_probe_ssh.returncode, 2, res_probe_ssh.stdout + res_probe_ssh.stderr)

        # 2: 隧道執行檔不在（安裝不完整）
        self.write_probe_config()
        tunnel_bin = os.path.join(self.home, ".local", "bin", "llmgateway-tunnel")
        os.rename(tunnel_bin, tunnel_bin + ".bak")
        try:
            res_probe_nobin = self.run_install(
                ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
                env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0"}
            )
            self.assertEqual(res_probe_nobin.returncode, 2, res_probe_nobin.stdout + res_probe_nobin.stderr)
        finally:
            os.rename(tunnel_bin + ".bak", tunnel_bin)

        # 1: Service inactive
        res_inact = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "inactive"}
        )
        self.assertEqual(res_inact.returncode, 1)

        # 1: Tunnel inactive（例如 tunnel.env 還沒填、以 78 結束）
        res_tunnel_inact = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={
                "SYS_STATE": "active", "SYS_TUNNEL_STATE": "inactive",
                "CURL_CODE": "200", "CURL_RC": "0",
            }
        )
        self.assertEqual(res_tunnel_inact.returncode, 1)

        # 1: Tag mismatch
        tag_file = os.path.join(self.home, ".local", "share", "llmgateway", "lgw-v0.1.0", ".lgw-tag")
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("other-tag\n")
        res_mismatch = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0"}
        )
        self.assertEqual(res_mismatch.returncode, 1)
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("lgw-v0.1.0\n")

        # 1: list 回非 200（例如 401 或 500）；本機不是 200 時不跑經會合點那段
        if os.path.exists(self.probe_ssh_log):
            os.remove(self.probe_ssh_log)
        res_401 = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "401", "CURL_RC": "0"}
        )
        self.assertEqual(res_401.returncode, 1)
        self.assertEqual(self.read_log("probe-ssh"), "")

        res_500 = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "500", "CURL_RC": "0"}
        )
        self.assertEqual(res_500.returncode, 1)

        # 2: curl connection failure
        res_curl_fail = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_RC": "7"}
        )
        self.assertEqual(res_curl_fail.returncode, 2)

        # 2: curl timeout
        res_curl_to = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_RC": "28"}
        )
        self.assertEqual(res_curl_to.returncode, 2)

        # 2: systemctl fails / no bus
        res_sys_nobus = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "SYS_NOBUS": "1", "CURL_CODE": "200"}
        )
        self.assertEqual(res_sys_nobus.returncode, 2)

        # 2: systemctl unavailable in PATH
        res_no_sys = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            path_mode="nosys"
        )
        self.assertEqual(res_no_sys.returncode, 2)

    def test_check_without_tag(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.write_probe_config()

        # 兩個 unit active、本機與經會合點都 200 -> 0 even without --tag
        res_ok = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0", "FAKE_PROBE_CODE": "200"}
        )
        self.assertEqual(res_ok.returncode, 0, res_ok.stdout + res_ok.stderr)

        # 經會合點不是 200 -> 1
        res_probe_503 = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0", "FAKE_PROBE_CODE": "503"}
        )
        self.assertEqual(res_probe_503.returncode, 1, res_probe_503.stdout + res_probe_503.stderr)

        # Does not check version tag: even if .lgw-tag differs, it passes
        tag_file = os.path.join(self.home, ".local", "share", "llmgateway", "lgw-v0.1.0", ".lgw-tag")
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("different-version-not-checked\n")
        res_ok_diff = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0"}
        )
        self.assertEqual(res_ok_diff.returncode, 0)

        # Service inactive -> 1
        res_inact = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "inactive"}
        )
        self.assertEqual(res_inact.returncode, 1)

        # Tunnel inactive -> 1
        res_tunnel_inact = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "SYS_TUNNEL_STATE": "inactive", "CURL_CODE": "200"}
        )
        self.assertEqual(res_tunnel_inact.returncode, 1)

        # Probe returns 500 -> 1
        res_500 = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "500", "CURL_RC": "0"}
        )
        self.assertEqual(res_500.returncode, 1)

        # curl failure -> 2
        res_curl_fail = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_RC": "7"}
        )
        self.assertEqual(res_curl_fail.returncode, 2)

    def test_missing_or_old_python(self):
        # Missing python3 in PATH
        res_nopy = self.run_install(
            ["--tag", "lgw-v0.1.0", "--home", self.home],
            path_mode="nopy"
        )
        self.assertNotEqual(res_nopy.returncode, 0)
        self.assertIn("python3", res_nopy.stderr + res_nopy.stdout)

        # python3 < 3.10
        res_oldpy = self.run_install(
            ["--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"FAKE_PY_VERSION": "3.9.18"}
        )
        self.assertNotEqual(res_oldpy.returncode, 0)
        self.assertIn("3.10", res_oldpy.stderr + res_oldpy.stdout)

    def test_tag_format_validation(self):
        bad_tags = [
            "v1.0.0", "lgw-v1.0", "lgw-v1.0.0-test", "lgw-v1.0.0.0",
            "", "invalid", "lgw-v99999.1.1", "lgw-v1.2.3;echo", "lgw-v01.02.03-beta"
        ]
        for tag in bad_tags:
            res = self.run_install(["--tag", tag, "--home", self.home])
            self.assertEqual(res.returncode, 2, f"Expected 2 for tag={tag!r}, got {res.returncode}")

            res_chk = self.run_install(["--check", "--tag", tag, "--home", self.home])
            self.assertEqual(res_chk.returncode, 2, f"Expected 2 for check with tag={tag!r}")

        # Install mode without --tag exits 2
        res_notag = self.run_install(["--home", self.home])
        self.assertEqual(res_notag.returncode, 2)

    def test_files_installed_from_new_version_deploy(self):
        # Wrapper, units and tunnel daemon must come from the checked out tag's
        # deploy/, not from the running script's dir
        wrapper_marker = "TEST_WRAPPER_V2_MARKER_99"
        service_marker = "TEST_SERVICE_V2_MARKER_88"
        tunnel_marker = "TEST_TUNNEL_V2_MARKER_77"
        tunnel_unit_marker = "TEST_TUNNEL_UNIT_V2_MARKER_66"

        res = self.run_install(
            ["--tag", "lgw-v0.2.0", "--home", self.home],
            env_overrides={
                "WRAPPER_MARKER": wrapper_marker,
                "SERVICE_MARKER": service_marker,
                "TUNNEL_MARKER": tunnel_marker,
                "TUNNEL_UNIT_MARKER": tunnel_unit_marker,
            }
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        installed_run = os.path.join(self.home, ".local", "bin", "llmgateway-run")
        with open(installed_run, "r", encoding="utf-8") as f:
            run_content = f.read()
        self.assertIn(wrapper_marker, run_content)

        installed_svc = os.path.join(self.home, ".config", "systemd", "user", "llmgateway.service")
        with open(installed_svc, "r", encoding="utf-8") as f:
            svc_content = f.read()
        self.assertIn(service_marker, svc_content)

        installed_tunnel = os.path.join(self.home, ".local", "bin", "llmgateway-tunnel")
        with open(installed_tunnel, "r", encoding="utf-8") as f:
            tunnel_content = f.read()
        self.assertIn(tunnel_marker, tunnel_content)

        installed_tunnel_unit = os.path.join(
            self.home, ".config", "systemd", "user", "llmgateway-tunnel.service"
        )
        with open(installed_tunnel_unit, "r", encoding="utf-8") as f:
            tunnel_unit_content = f.read()
        self.assertIn(tunnel_unit_marker, tunnel_unit_content)

    def test_tunnel_files_changed_schedules_delayed_restart(self):
        first = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(first.returncode, 0, first.stderr)

        # v0.2.0 changes the tunnel daemon and unit; tunnel is active.
        second = self.run_install(
            ["--tag", "lgw-v0.2.0", "--home", self.home],
            env_overrides={
                "SYS_STATE": "active",
                "TUNNEL_MARKER": "TEST_TUNNEL_V2_CHANGED",
                "TUNNEL_UNIT_MARKER": "TEST_TUNNEL_UNIT_V2_CHANGED",
            },
        )
        self.assertEqual(second.returncode, 0, second.stderr)

        run_log = self.read_log("systemd-run")
        self.assertIn("--user --on-active=60 --unit=llmgateway-tunnel-restart", run_log)
        self.assertIn("--user restart llmgateway-tunnel.service", run_log)

        sys_log = self.read_log("systemctl")
        self.assertIn("restart llmgateway.service", sys_log)
        self.assertNotIn("restart llmgateway-tunnel.service", sys_log)
        self.assertNotIn("stop llmgateway-tunnel.service", sys_log)

    def test_unchanged_tunnel_files_no_delayed_restart(self):
        first = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_install(
            ["--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active"},
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.read_log("systemd-run"), "")

    def test_tunnel_restart_already_scheduled_skips_and_logs(self):
        first = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_install(
            ["--tag", "lgw-v0.2.0", "--home", self.home],
            env_overrides={
                "SYS_STATE": "active",
                "SYS_TIMER_STATE": "active",
                "TUNNEL_MARKER": "TEST_TUNNEL_V2_CHANGED",
                "TUNNEL_UNIT_MARKER": "TEST_TUNNEL_UNIT_V2_CHANGED",
            },
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("已排程", second.stdout)
        self.assertEqual(self.read_log("systemd-run"), "")

    def test_tunnel_restart_systemd_run_failure_warns_only_install_succeeds(self):
        first = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_install(
            ["--tag", "lgw-v0.2.0", "--home", self.home],
            env_overrides={
                "SYS_STATE": "active",
                "SYSTEMD_RUN_FAIL": "1",
                "TUNNEL_MARKER": "TEST_TUNNEL_V2_CHANGED",
                "TUNNEL_UNIT_MARKER": "TEST_TUNNEL_UNIT_V2_CHANGED",
            },
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("WARN", second.stderr)
        self.assertIn("--user --on-active=60 --unit=llmgateway-tunnel-restart", self.read_log("systemd-run"))

    def test_tunnel_inactive_is_started_but_install_still_succeeds(self):
        # 第一次安裝時 tunnel.env 還是空範本，守護程式以 78 結束是預期；
        # install.sh 只負責 start，不能因此失敗。
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)
        sys_log = self.read_log("systemctl")
        self.assertIn("enable llmgateway-tunnel.service", sys_log)
        self.assertIn("start llmgateway-tunnel.service", sys_log)
        self.assertEqual(self.read_log("systemd-run"), "")

    def test_version_retention_only_two_versions_and_current_never_deleted(self):
        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        current_link = os.path.join(share_dir, "current")

        def version_dirs():
            return sorted([d for d in os.listdir(share_dir) if d != "current" and not d.startswith(".")])

        # 1. Install v0.1.0 -> 1 version
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.1.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        # 2. Install v0.2.0 -> 2 versions
        res = self.run_install(["--tag", "lgw-v0.2.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.1.0", "lgw-v0.2.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.2.0"))

        # 3. Install v0.3.0 -> only 2 versions kept (v0.2.0 and v0.3.0); v0.1.0 removed
        res = self.run_install(["--tag", "lgw-v0.3.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.2.0", "lgw-v0.3.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.3.0"))

        # 4. Install v0.4.0 -> only 2 versions kept (v0.3.0 and v0.4.0); v0.2.0 removed
        res = self.run_install(["--tag", "lgw-v0.4.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.3.0", "lgw-v0.4.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.4.0"))

        # 5. Reinstall current v0.4.0 -> current is never deleted; both kept
        res = self.run_install(["--tag", "lgw-v0.4.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.3.0", "lgw-v0.4.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.4.0"))

        # 6. Rollback to v0.3.0 -> current switches to v0.3.0, both kept
        res = self.run_install(["--tag", "lgw-v0.3.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.3.0", "lgw-v0.4.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.3.0"))


if __name__ == "__main__":
    unittest.main()
