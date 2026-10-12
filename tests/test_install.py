import os
import pwd
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
TUNNEL_DAEMON = os.path.join(REPO_ROOT, "ops-scripts", "llmgateway-tunnel")
TUNNEL_UNIT = os.path.join(REPO_ROOT, "ops-scripts", "llmgateway-tunnel.service")
KEY_SYNC = os.path.join(REPO_ROOT, "ops-scripts", "key-sync")
KEY_SYNC_SERVICE = os.path.join(REPO_ROOT, "ops-scripts", "key-sync.service")
KEY_SYNC_TIMER = os.path.join(REPO_ROOT, "ops-scripts", "key-sync.timer")

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
mkdir -p "$dest/policy" "$dest/deploy" "$dest/ops-scripts"
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
    printf '#!/bin/sh\\n# %s\\nexit 0\\n' "$TUNNEL_MARKER" > "$dest/ops-scripts/llmgateway-tunnel"
else
    cat "${REAL_TUNNEL:?}" > "$dest/ops-scripts/llmgateway-tunnel"
fi
chmod +x "$dest/ops-scripts/llmgateway-tunnel"
if [[ -n "${TUNNEL_UNIT_MARKER:-}" ]]; then
    printf '[Unit]\\n# %s\\n' "$TUNNEL_UNIT_MARKER" > "$dest/ops-scripts/llmgateway-tunnel.service"
else
    cat "${REAL_TUNNEL_UNIT:?}" > "$dest/ops-scripts/llmgateway-tunnel.service"
fi
if [[ -n "${KEY_SYNC_MARKER:-}" ]]; then
    printf '#!/bin/sh\\n# %s\\nexit 0\\n' "$KEY_SYNC_MARKER" > "$dest/ops-scripts/key-sync"
else
    cat "${REAL_KEY_SYNC:?}" > "$dest/ops-scripts/key-sync"
fi
chmod +x "$dest/ops-scripts/key-sync"
if [[ -n "${KEY_SYNC_SERVICE_MARKER:-}" ]]; then
    printf '[Unit]\\n# %s\\n' "$KEY_SYNC_SERVICE_MARKER" > "$dest/ops-scripts/key-sync.service"
else
    cat "${REAL_KEY_SYNC_SERVICE:?}" > "$dest/ops-scripts/key-sync.service"
fi
if [[ -n "${KEY_SYNC_TIMER_MARKER:-}" ]]; then
    printf '[Unit]\\n# %s\\n' "$KEY_SYNC_TIMER_MARKER" > "$dest/ops-scripts/key-sync.timer"
else
    cat "${REAL_KEY_SYNC_TIMER:?}" > "$dest/ops-scripts/key-sync.timer"
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
if [[ "$*" == *"is-failed"* ]]; then
    # 0＝failed；其他＝沒失敗或查不到 unit
    if [[ "$*" == *"key-sync.service"* && "${SYS_KEYSYNC_FAILED:-0}" == "1" ]]; then
        exit 0
    fi
    exit 1
fi
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

        # 假 gh：支援 `gh auth status` 與 `gh api repos/<repo> --jq .full_name`
        # （install.sh --check 用）。FAKE_GH_MODE：ok／logged_out／repo_denied／
        # missing（rc=127，模擬沒裝）／slow（rc=124，模擬逾時）
        gh_script = """#!/usr/bin/env bash
echo "gh $*" >> "${SANDBOX:?}/gh-argv.log"
mode="${FAKE_GH_MODE:-ok}"
if [[ "$mode" == "missing" ]]; then
    echo "bash: gh: command not found" >&2
    exit 127
fi
if [[ "$mode" == "slow" ]]; then
    # 逾時路徑由假 timeout 擋（exit 124），這裡直接回同樣的碼避免真的睡
    exit 124
fi
if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
    if [[ "$mode" == "logged_out" ]]; then
        echo "You are not logged into any GitHub hosts. To log in, run: gh auth login" >&2
        exit 1
    fi
    exit 0
fi
if [[ "${1:-}" == "api" ]]; then
    if [[ "$mode" == "repo_denied" ]]; then
        echo "gh: Could not resolve to a Repository with the name 'x/y'." >&2
        exit 1
    fi
    out="${2#repos/}"
    # 觀察-2：某版 gh 的 --jq 輸出可能包引號或多空白；這裡模擬，install.sh 要先修剪再比對
    case "${FAKE_GH_DECORATE:-plain}" in
        quoted) out="\\"${out}\\"" ;;
        squoted) out="'${out}'" ;;
        spaced) out="  ${out}  " ;;
        quoted_spaced) out="  \\"${out}\\"  " ;;
    esac
    printf '%s\\n' "$out"
    exit 0
fi
echo "fake gh: unsupported: $*" >&2
exit 1
"""
        gh_target = os.path.join(self.fakebin, "gh")
        with open(gh_target, "w", encoding="utf-8") as f:
            f.write(gh_script)
        os.chmod(gh_target, 0o755)

        # 假 timeout：讓 --check 的逾時路徑可在測試裡觸發（真 sleep 由呼叫端決定）
        timeout_script = """#!/usr/bin/env bash
echo "timeout $*" >> "${SANDBOX:?}/gh-argv.log"
limit="$1"; shift
if [[ "${FAKE_GH_MODE:-ok}" == "slow" ]]; then
    exit 124
fi
exec "$@"
"""
        timeout_target = os.path.join(self.fakebin, "timeout")
        with open(timeout_target, "w", encoding="utf-8") as f:
            f.write(timeout_script)
        os.chmod(timeout_target, 0o755)

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

        # nopy: symlink git, systemctl, systemd-run, curl, gh
        for name in ["git", "systemctl", "systemd-run", "curl", "gh"]:
            os.symlink(os.path.join(self.fakebin, name), os.path.join(self.fakebin_nopy, name))

        # nosys: symlink git, python3, curl, gh（沒有 systemctl）
        for name in ["git", "python3", "curl", "gh"]:
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
            "REAL_KEY_SYNC": KEY_SYNC,
            "REAL_KEY_SYNC_SERVICE": KEY_SYNC_SERVICE,
            "REAL_KEY_SYNC_TIMER": KEY_SYNC_TIMER,
            "REAL_PYTHON": sys.executable,
            "PROBE_SSH_LOG": self.probe_ssh_log,
            "FAKE_PROBE_CODE": "200",
            # --check 問「服務帳號」的 gh 狀態；測試沒有真的 llmgw，
            # 讓它以執行測試的這個使用者身分直接跑假 gh。
            "LGW_SERVICE_USER": pwd.getpwuid(os.geteuid()).pw_name,
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

    def write_keysync_env(self, home=None, value="FakeKeysyncPassword_ForTests_01"):
        """解密密碼檔（假值）：key-sync 用，install.sh --check 會看有沒有放。"""
        if home is None:
            home = self.home
        conf_dir = os.path.join(home, ".config", "llmgateway")
        os.makedirs(conf_dir, exist_ok=True)
        path = os.path.join(conf_dir, "keysync.env")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"LLMGW_KEYS_PASSWORD={value}\n")
        os.chmod(path, 0o600)
        return path

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
        if not os.path.exists(os.path.join(conf_dir, "keysync.env")):
            self.write_keysync_env(home)

    def test_prerequisites_and_file_modes(self):
        self.assertTrue(os.path.isfile(INSTALL_SH))
        self.assertTrue(os.access(INSTALL_SH, os.X_OK))

        self.assertTrue(os.path.isfile(RUN_WRAPPER))
        self.assertTrue(os.access(RUN_WRAPPER, os.X_OK))

        self.assertTrue(os.path.isfile(SERVICE_UNIT))
        with open(SERVICE_UNIT, "r", encoding="utf-8") as f:
            svc_content = f.read()
        self.assertIn("ExecStart=%h/.local/bin/llmgateway-run", svc_content)
        self.assertNotIn("ExecReload", svc_content)  # v5：沒有 SIGHUP 重讀了
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

        # key-sync（v5.4）：script 與它的 unit／timer 都在 ops-scripts/
        self.assertTrue(os.path.isfile(KEY_SYNC))
        self.assertTrue(os.access(KEY_SYNC, os.X_OK))
        res = subprocess.run([sys.executable, "-m", "py_compile", KEY_SYNC],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        self.assertTrue(os.path.isfile(KEY_SYNC_SERVICE))
        with open(KEY_SYNC_SERVICE, "r", encoding="utf-8") as f:
            keysync_service = f.read()
        self.assertIn("ExecStart=%h/.local/bin/key-sync", keysync_service)
        self.assertIn("EnvironmentFile=-%h/.config/llmgateway/keysync.env", keysync_service)

        self.assertTrue(os.path.isfile(KEY_SYNC_TIMER))
        with open(KEY_SYNC_TIMER, "r", encoding="utf-8") as f:
            keysync_timer = f.read()
        self.assertIn("Unit=key-sync.service", keysync_timer)
        self.assertIn("OnUnitActiveSec=10min", keysync_timer)

        # 搬家後的引用：install.sh 只從 ops-scripts/ 裝（別再指到 deploy/）
        with open(INSTALL_SH, "r", encoding="utf-8") as f:
            install_content = f.read()
        self.assertIn('OPS_DIR="ops-scripts"', install_content)
        self.assertNotIn("deploy/${TUNNEL_BIN}", install_content)
        self.assertNotIn("deploy/${KEY_SYNC_BIN}", install_content)

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

        # key-sync 的執行檔與 unit／timer（從 ops-scripts/ 裝）
        installed_keysync = os.path.join(self.home, ".local", "bin", "key-sync")
        self.assertTrue(os.path.isfile(installed_keysync))
        self.assertTrue(os.access(installed_keysync, os.X_OK))
        for unit in ("key-sync.service", "key-sync.timer"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.home, ".config", "systemd", "user", unit)),
                unit)

        git_log = self.read_log("git")
        self.assertIn("clone --depth 1 --branch lgw-v0.1.0 https://github.com/FATESAIKOU/LLMGateway", git_log)

        sys_log = self.read_log("systemctl")
        self.assertIn("daemon-reload", sys_log)
        self.assertIn("enable --now llmgateway.service", sys_log)
        self.assertIn("enable llmgateway-tunnel.service", sys_log)
        self.assertIn("start llmgateway-tunnel.service", sys_log)
        self.assertNotIn("stop llmgateway-tunnel.service", sys_log)
        self.assertNotIn("restart llmgateway-tunnel.service", sys_log)
        self.assertIn("enable key-sync.timer", sys_log)
        self.assertIn("start key-sync.timer", sys_log)

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

    def test_check_keysync_states(self):
        """--check 看得出「gh 不能用」「解密密碼沒放」「key-sync 最近失敗」（v5.4）"""
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.write_probe_config()
        base_env = {"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0",
                    "FAKE_PROBE_CODE": "200"}

        def check(**overrides):
            env = dict(base_env)
            env.update(overrides)
            return self.run_install(["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
                                    env_overrides=env)

        # 0：gh 已登入且讀得到金鑰 repo、解密密碼放了、上次沒失敗
        ok = check()
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("gh 已登入，且讀得到 FATESAIKOU/LLMGateway-keys", ok.stdout)
        self.assertIn("key-sync: 解密密碼已放", ok.stdout)
        gh_log = self.read_log("gh")
        self.assertIn("auth status", gh_log)
        # 只問 repo 本身，不讀任何檔案內容
        self.assertIn("api repos/FATESAIKOU/LLMGateway-keys --jq .full_name", gh_log)

        # 1：gh 沒登入／token 失效
        gh_fail = check(FAKE_GH_MODE="logged_out")
        self.assertEqual(gh_fail.returncode, 1, gh_fail.stdout)
        self.assertIn("key-sync: 不能用", gh_fail.stdout)
        self.assertIn("gh 沒登入或 token 失效", gh_fail.stdout)
        self.assertNotIn("解密密碼已放", gh_fail.stdout)

        # 1：登入了但讀不到金鑰 repo（權限被改／repo 改名）
        repo_denied = check(FAKE_GH_MODE="repo_denied")
        self.assertEqual(repo_denied.returncode, 1, repo_denied.stdout)
        self.assertIn("讀不到 FATESAIKOU/LLMGateway-keys", repo_denied.stdout)

        # 2：gh 不存在 → 無法確認（不是「確定壞掉」）
        no_gh = check(FAKE_GH_MODE="missing")
        self.assertEqual(no_gh.returncode, 2, no_gh.stdout)
        self.assertIn("無法確認", no_gh.stdout)
        self.assertIn("找不到 gh", no_gh.stdout)

        # 2：查詢逾時 → 無法確認
        gh_slow = check(FAKE_GH_MODE="slow")
        self.assertEqual(gh_slow.returncode, 2, gh_slow.stdout)
        self.assertIn("無法確認", gh_slow.stdout)
        self.assertIn("逾時", gh_slow.stdout)

        # 1：解密密碼沒放（keysync.env 不存在）
        keysync_env = os.path.join(self.home, ".config", "llmgateway", "keysync.env")
        os.remove(keysync_env)
        no_pass = check()
        self.assertEqual(no_pass.returncode, 1, no_pass.stdout)
        self.assertIn("還沒放解密密碼", no_pass.stdout)

        # 1：密碼行是空的
        with open(keysync_env, "w", encoding="utf-8") as f:
            f.write("LLMGW_KEYS_PASSWORD=\n")
        empty_pass = check()
        self.assertEqual(empty_pass.returncode, 1, empty_pass.stdout)
        self.assertIn("沒有非空的", empty_pass.stdout)

        # 1：讀不到解密密碼檔（權限被改壞）要講自己的話，不是「沒有非空的 …」。
        # 審查 #13 低-6：兩個問題分開講，別把人往錯的方向指。
        os.chmod(keysync_env, 0o000)
        try:
            if os.access(keysync_env, os.R_OK):
                self.skipTest("這個使用者讀得到 000 的檔（root），測不了讀不到的情況")
            unreadable = check()
            self.assertEqual(unreadable.returncode, 1, unreadable.stdout)
            self.assertIn("讀不到解密密碼檔", unreadable.stdout)
            self.assertIn("檢查權限", unreadable.stdout)
            self.assertNotIn("沒有非空的", unreadable.stdout)
        finally:
            os.chmod(keysync_env, 0o600)

        # 1：key-sync 最近一次執行失敗
        self.write_keysync_env()
        failed_run = check(SYS_KEYSYNC_FAILED="1")
        self.assertEqual(failed_run.returncode, 1, failed_run.stdout)
        self.assertIn("最近一次執行失敗", failed_run.stdout)
        self.assertIn("is-failed --quiet key-sync.service", self.read_log("systemctl"))

        # 回到 0：密碼放著、上次失敗的狀態清掉
        ok_again = check()
        self.assertEqual(ok_again.returncode, 0, ok_again.stdout + ok_again.stderr)

    def test_check_keysync_sudo_failure_is_unknown(self):
        """管理帳號直跑 --check 且 sudo 要密碼／缺席時回 2，不是「gh 沒登入」（#13 新-低-1）"""
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.write_probe_config()
        base_env = {"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0",
                    "FAKE_PROBE_CODE": "200",
                    "LGW_SERVICE_USER": "llmgw-test-user"}

        # install.sh 用 `id -un` 判斷目前身分；測試 PATH 裡本來沒有 id
        # （缺 id 時當成服務帳號自己、直連），這裡補假 id 讓它走 sudo 分支。
        fake_id = os.path.join(self.fakebin, "id")
        with open(fake_id, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\n'
                    'echo "manager"\n'
                    'exit 0\n')
        os.chmod(fake_id, 0o755)

        def check(**overrides):
            env = dict(base_env)
            env.update(overrides)
            return self.run_install(["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
                                    env_overrides=env)

        # 假 sudo：一律要密碼（auth 那步就失敗）
        sudo_fail = os.path.join(self.sandbox, "fake-sudo-needs-password")
        with open(sudo_fail, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\n'
                    'echo "sudo $*" >> "${SANDBOX:?}/sudo-argv.log"\n'
                    'echo "sudo: a password is required" >&2\n'
                    'exit 1\n')
        os.chmod(sudo_fail, 0o755)

        r1 = check(LGW_SUDO=sudo_fail)
        self.assertEqual(r1.returncode, 2, r1.stdout)
        self.assertIn("無法確認", r1.stdout)
        self.assertIn("sudo", r1.stdout)
        self.assertNotIn("gh 沒登入", r1.stdout)

        # sudo 本體缺席：同樣回 2（不是「找不到 gh」）
        r2 = check(LGW_SUDO="/nonexistent/no-such-sudo")
        self.assertEqual(r2.returncode, 2, r2.stdout)
        self.assertIn("無法確認", r2.stdout)
        self.assertIn("sudo", r2.stdout)

        # 第二步（讀 repo）才 sudo 失效：auth 過，api 那步回 2
        sudo_fail_api = os.path.join(self.sandbox, "fake-sudo-fail-api")
        with open(sudo_fail_api, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\n'
                    'echo "sudo $*" >> "${SANDBOX:?}/sudo-argv.log"\n'
                    'args=()\n'
                    'skip_next=0\n'
                    'for a in "$@"; do\n'
                    '    if [[ "$skip_next" == "1" ]]; then skip_next=0; continue; fi\n'
                    '    case "$a" in -n) continue;; -u) skip_next=1; continue;; *) args+=("$a");; esac\n'
                    'done\n'
                    'if [[ "${args[*]}" == *" api "* ]]; then\n'
                    '    echo "sudo: a password is required" >&2\n'
                    '    exit 1\n'
                    'fi\n'
                    'exec "${args[@]}"\n')
        os.chmod(sudo_fail_api, 0o755)

        r3 = check(LGW_SUDO=sudo_fail_api)
        self.assertEqual(r3.returncode, 2, r3.stdout)
        self.assertIn("無法確認", r3.stdout)
        self.assertIn("sudo", r3.stdout)
        self.assertNotIn("讀不到 FATESAIKOU/LLMGateway-keys", r3.stdout)

        # sudo 正常轉交時仍回 0（沒把成功路徑修壞）
        sudo_ok = os.path.join(self.sandbox, "fake-sudo-passthrough")
        with open(sudo_ok, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\n'
                    'echo "sudo $*" >> "${SANDBOX:?}/sudo-argv.log"\n'
                    'args=()\n'
                    'skip_next=0\n'
                    'for a in "$@"; do\n'
                    '    if [[ "$skip_next" == "1" ]]; then skip_next=0; continue; fi\n'
                    '    case "$a" in -n) continue;; -u) skip_next=1; continue;; *) args+=("$a");; esac\n'
                    'done\n'
                    'exec "${args[@]}"\n')
        os.chmod(sudo_ok, 0o755)

        r0 = check(LGW_SUDO=sudo_ok)
        self.assertEqual(r0.returncode, 0, r0.stdout + r0.stderr)

    def test_check_keysync_repo_name_trimmed(self):
        """gh --jq .full_name 包引號或多空白時仍認得是同一個 repo（觀察-2）"""
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.write_probe_config()
        base_env = {"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0",
                    "FAKE_PROBE_CODE": "200"}

        def check(**overrides):
            env = dict(base_env)
            env.update(overrides)
            return self.run_install(["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
                                    env_overrides=env)

        for decorate in ("plain", "quoted", "squoted", "spaced", "quoted_spaced"):
            with self.subTest(decorate=decorate):
                r = check(FAKE_GH_DECORATE=decorate)
                self.assertEqual(r.returncode, 0, f"{decorate}: {r.stdout + r.stderr}")
                self.assertIn("gh 已登入，且讀得到", r.stdout)

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

    def test_files_installed_from_new_version_ops_scripts(self):
        # Wrapper 與 unit 從取到的 tag 的 deploy/，隧道與 key-sync 從 ops-scripts/；
        # 都不是從執行中的這份腳本旁邊裝
        wrapper_marker = "TEST_WRAPPER_V2_MARKER_99"
        service_marker = "TEST_SERVICE_V2_MARKER_88"
        tunnel_marker = "TEST_TUNNEL_V2_MARKER_77"
        tunnel_unit_marker = "TEST_TUNNEL_UNIT_V2_MARKER_66"
        keysync_marker = "TEST_KEYSYNC_V2_MARKER_55"
        keysync_service_marker = "TEST_KEYSYNC_SERVICE_V2_MARKER_44"
        keysync_timer_marker = "TEST_KEYSYNC_TIMER_V2_MARKER_33"

        res = self.run_install(
            ["--tag", "lgw-v0.2.0", "--home", self.home],
            env_overrides={
                "WRAPPER_MARKER": wrapper_marker,
                "SERVICE_MARKER": service_marker,
                "TUNNEL_MARKER": tunnel_marker,
                "TUNNEL_UNIT_MARKER": tunnel_unit_marker,
                "KEY_SYNC_MARKER": keysync_marker,
                "KEY_SYNC_SERVICE_MARKER": keysync_service_marker,
                "KEY_SYNC_TIMER_MARKER": keysync_timer_marker,
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

        # key-sync 也從取到的 tag 的 ops-scripts/ 裝
        installed_keysync = os.path.join(self.home, ".local", "bin", "key-sync")
        with open(installed_keysync, "r", encoding="utf-8") as f:
            self.assertIn(keysync_marker, f.read())
        unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        with open(os.path.join(unit_dir, "key-sync.service"), "r", encoding="utf-8") as f:
            self.assertIn(keysync_service_marker, f.read())
        with open(os.path.join(unit_dir, "key-sync.timer"), "r", encoding="utf-8") as f:
            self.assertIn(keysync_timer_marker, f.read())

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
