import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOOTSTRAP_SH = os.path.join(REPO_ROOT, "deploy", "bootstrap.sh")
INSTALL_SH = os.path.join(REPO_ROOT, "deploy", "install.sh")
RUN_WRAPPER = os.path.join(REPO_ROOT, "deploy", "llmgateway-run")
SERVICE_UNIT = os.path.join(REPO_ROOT, "deploy", "llmgateway.service")
REPO_DEPLOY = os.path.join(REPO_ROOT, "deploy")
REPO_OPS = os.path.join(REPO_ROOT, "ops-scripts")
REPO_POLICY = os.path.join(REPO_ROOT, "policy")

TOOLS_LIST = [
    "mkdir", "mktemp", "chmod", "mv", "ln", "rm", "cat", "date",
    "whoami", "cp", "bash", "head", "grep", "ls", "readlink",
    "cmp", "stat", "dirname", "sort", "tail", "awk", "sed", "tr", "cut", "sleep",
    "test", "python3", "ssh", "env"
]


class BootstrapScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sandbox = self.tmpdir.name

        self.home = os.path.join(self.sandbox, "home")
        self.rundir = os.path.join(self.sandbox, "rundir")
        self.logdir = os.path.join(self.sandbox, "log")
        self.state = os.path.join(self.sandbox, "state")
        self.fakebin = os.path.join(self.sandbox, "fakebin")
        self.tools = os.path.join(self.sandbox, "tools")

        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.rundir, exist_ok=True)
        os.makedirs(self.logdir, exist_ok=True)
        os.makedirs(self.state, exist_ok=True)
        os.makedirs(self.fakebin, exist_ok=True)
        os.makedirs(self.tools, exist_ok=True)

        for t in TOOLS_LIST:
            p = shutil.which(t)
            if p:
                os.symlink(p, os.path.join(self.tools, t))

        # Host key file
        self.hostkey_file = os.path.join(self.sandbox, "ssh_host_ed25519_key.pub")
        with open(self.hostkey_file, "w", encoding="utf-8") as f:
            f.write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForBootstrapTest999 root@mockhost\n")

        # 解密密碼檔（假值；單行、不含空白）
        self.decrypt_pass_file = os.path.join(self.sandbox, "decrypt_pass.txt")
        self.decrypt_password = "FakeBootstrapDecryptPassword_01"
        with open(self.decrypt_pass_file, "w", encoding="utf-8") as f:
            f.write(self.decrypt_password + "\n")

        # Known hosts file for rendezvous
        self.known_hosts_file = os.path.join(self.sandbox, "rendezvous_known_hosts")
        with open(self.known_hosts_file, "w", encoding="utf-8") as f:
            f.write("[jump.example.com]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJumpHostKey123\n")

        # Initial state flags
        self._set_state("user_exists", "0")
        self._set_state("user_in_docker", "0")
        self._set_state("user_linger", "0")
        self._set_state("sshd_pubkey", "yes")
        self._set_state("sshd_deny", "")
        self._set_state("sshd_allow", "")
        self._set_state("sshd_listen", "1")
        self._set_state("curl_code", "200")
        self._set_state("curl_rc", "0")
        self._set_state("ssh_probe_code", "200")

        self._create_fake_tools()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _set_state(self, key, val):
        with open(os.path.join(self.state, key), "w", encoding="utf-8") as f:
            f.write(str(val))

    def _get_state(self, key, default=""):
        p = os.path.join(self.state, key)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        return default

    def _create_fake_tools(self):
        # 1. sudo
        # 假 sudo: 去掉選項後以目前使用者執行，-S 只允許和 -v 一起出現，否則測試失敗
        # 兩種失敗注入（狀態檔在 LGW_TEST_STATE_DIR）：
        #   sudo_fail_substr=<子字串>：argv 含此字串的指令回非零
        #   sudo_no_cache=1：-n 的指令必須先有 sudo -v 刷新過，否則回非零
        sudo_script = """#!/usr/bin/env bash
set -uo pipefail
echo "sudo $*" >> "${LOG_DIR}/sudo-argv.log"

has_S=0
has_v=0
has_n=0

args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -S) has_S=1; shift ;;
        -v) has_v=1; shift ;;
        -n) has_n=1; shift ;;
        -p) shift; if [[ $# -gt 0 ]]; then shift; fi ;;
        -u) shift; if [[ $# -gt 0 ]]; then shift; fi ;;
        --) shift; args+=("$@"); break ;;
        -*) shift ;;
        *) args+=("$@"); break ;;
    esac
done

if [[ $has_S -eq 1 && $has_v -eq 0 ]]; then
    echo "fake sudo error: -S is only allowed with -v" >&2
    exit 99
fi

if [[ $has_v -eq 1 ]]; then
    if [[ $has_S -eq 1 ]]; then
        cat >/dev/null || true
    fi
    echo "1" > "${LGW_TEST_STATE_DIR}/sudo-v-called"
    exit 0
fi

if [[ ${#args[@]} -eq 0 ]]; then
    exit 0
fi

fail_substr="$(cat "${LGW_TEST_STATE_DIR}/sudo_fail_substr" 2>/dev/null || true)"
if [[ -n "$fail_substr" && "$*" == *"${fail_substr}"* ]]; then
    echo "fake sudo: injected failure for: ${fail_substr}" >&2
    exit 1
fi

if [[ $has_n -eq 1 && "$(cat "${LGW_TEST_STATE_DIR}/sudo_no_cache" 2>/dev/null || echo 0)" == "1" ]]; then
    if [[ ! -f "${LGW_TEST_STATE_DIR}/sudo-v-called" ]]; then
        echo "fake sudo: no cached credentials (sudo -n without sudo -v)" >&2
        exit 1
    fi
fi

exec "${args[@]}"
"""
        # 2. useradd
        useradd_script = """#!/usr/bin/env bash
set -euo pipefail
echo "useradd $*" >> "${LOG_DIR}/useradd-argv.log"
echo "1" > "${STATE_DIR}/user_exists"
user="${@: -1}"
home="${LGW_BOOT_HOME:-/home/$user}"
mkdir -p "$home"
exit 0
"""
        # 3. gpasswd
        gpasswd_script = """#!/usr/bin/env bash
set -euo pipefail
echo "gpasswd $*" >> "${LOG_DIR}/gpasswd-argv.log"
if [[ "${1:-}" == "-d" ]]; then
    echo "0" > "${STATE_DIR}/user_in_docker"
fi
exit 0
"""
        # 4. id
        id_script = """#!/usr/bin/env bash
set -euo pipefail
echo "id $*" >> "${LOG_DIR}/id-argv.log"
exists="$(cat "${STATE_DIR}/user_exists" 2>/dev/null || echo "0")"
if [[ "$exists" != "1" ]]; then
    exit 1
fi

if [[ "${1:-}" == "-u" ]]; then
    echo "1001"
    exit 0
elif [[ "${1:-}" == "-Gn" ]]; then
    in_docker="$(cat "${STATE_DIR}/user_in_docker" 2>/dev/null || echo "0")"
    if [[ "$in_docker" == "1" ]]; then
        echo "llmgw docker"
    else
        echo "llmgw"
    fi
    exit 0
elif [[ "${1:-}" == "-gn" ]]; then
    echo "llmgw"
    exit 0
else
    echo "uid=1001(llmgw) gid=1001(llmgw) groups=1001(llmgw)"
    exit 0
fi
"""
        # 5. getent
        getent_script = """#!/usr/bin/env bash
set -euo pipefail
echo "getent $*" >> "${LOG_DIR}/getent-argv.log"
exists="$(cat "${STATE_DIR}/user_exists" 2>/dev/null || echo "0")"
if [[ "${1:-}" == "passwd" ]]; then
    if [[ "$exists" == "1" ]]; then
        echo "llmgw:x:1001:1001::${LGW_BOOT_HOME:-/home/llmgw}:/bin/bash"
        exit 0
    else
        exit 2
    fi
elif [[ "${1:-}" == "group" ]]; then
    echo "docker:x:999:llmgw"
    exit 0
fi
exit 2
"""
        # 6. loginctl
        loginctl_script = """#!/usr/bin/env bash
set -euo pipefail
echo "loginctl $*" >> "${LOG_DIR}/loginctl-argv.log"
if [[ "${1:-}" == "show-user" ]]; then
    linger="$(cat "${STATE_DIR}/user_linger" 2>/dev/null || echo "0")"
    if [[ "$linger" == "1" ]]; then
        echo "Linger=yes"
    else
        echo "Linger=no"
    fi
    exit 0
elif [[ "${1:-}" == "enable-linger" ]]; then
    echo "1" > "${STATE_DIR}/user_linger"
    uid="1001"
    rundir="${LGW_BOOT_RUNDIR:-/run/user}/${uid}"
    mkdir -p "$rundir"
    exit 0
fi
exit 0
"""
        # 7. sshd
        sshd_script = """#!/usr/bin/env bash
set -euo pipefail
echo "sshd $*" >> "${LOG_DIR}/sshd-argv.log"
pubkey="$(cat "${STATE_DIR}/sshd_pubkey" 2>/dev/null || echo "yes")"
deny="$(cat "${STATE_DIR}/sshd_deny" 2>/dev/null || true)"
allow="$(cat "${STATE_DIR}/sshd_allow" 2>/dev/null || true)"

echo "pubkeyauthentication ${pubkey}"
if [[ -n "$deny" ]]; then
    echo "denyusers ${deny}"
fi
if [[ -n "$allow" ]]; then
    echo "allowusers ${allow}"
fi
exit 0
"""
        # 8. ss
        ss_script = """#!/usr/bin/env bash
set -euo pipefail
echo "ss $*" >> "${LOG_DIR}/ss-argv.log"
listen="$(cat "${STATE_DIR}/sshd_listen" 2>/dev/null || echo "1")"
if [[ "$listen" == "1" ]]; then
    echo "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*"
else
    echo "LISTEN 0 128 192.168.1.1:22 0.0.0.0:*"
fi
exit 0
"""
        # 9. ssh-keygen
        ssh_keygen_script = """#!/usr/bin/env bash
set -euo pipefail
echo "ssh-keygen $*" >> "${LOG_DIR}/ssh-keygen-argv.log"
if [[ "${1:-}" == "-l" ]]; then
    file=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-f" ]]; then
            file="$2"; shift 2
        else
            shift
        fi
    done
    if [[ -f "$file" ]]; then
        # 真 ssh-keygen 對空檔（或只有空白／註解）回非零
        if grep -q '[^[:space:]]' "$file" 2>/dev/null; then
            echo "256 SHA256:MockFingerprintKeyString comment (ED25519)"
            exit 0
        fi
        echo "fake ssh-keygen: $file has no key" >&2
        exit 1
    else
        exit 1
    fi
elif [[ "${1:-}" == "-t" ]]; then
    file=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-f" ]]; then
            file="$2"; shift 2
        else
            shift
        fi
    done
    dir="$(dirname "$file")"
    mkdir -p "$dir"
    echo "-----BEGIN OPENSSH PRIVATE KEY-----" > "$file"
    echo "FakePrivateKey" >> "$file"
    echo "-----END OPENSSH PRIVATE KEY-----" >> "$file"
    echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeTunnelPubKeyGenerated123 llmgateway-tunnel" > "${file}.pub"
    exit 0
fi
exit 0
"""
        # 10. systemctl
        systemctl_script = """#!/usr/bin/env bash
set -euo pipefail
echo "systemctl $*" >> "${LOG_DIR}/systemctl-argv.log"
if [[ "$*" == *"is-active"* ]]; then
    if [[ "$*" == *"llmgateway-tunnel.service"* ]]; then
        tunnel_env="${LGW_BOOT_HOME:-}/.config/llmgateway/tunnel.env"
        if [[ -f "$tunnel_env" ]] && grep -Eq '^RENDEZVOUS=[^[:space:]]+' "$tunnel_env" 2>/dev/null; then
            exit 0
        else
            exit 3
        fi
    fi
    exit 0
fi
exit 0
"""
        # 11. git
        git_script = """#!/usr/bin/env bash
set -euo pipefail
echo "git $*" >> "${LOG_DIR}/git-argv.log"
if [[ "${1:-}" == "clone" ]]; then
    dest="${@: -1}"
    mkdir -p "$dest/policy" "$dest/deploy" "$dest/ops-scripts"
    if [[ -d "${REPO_POLICY:-}" ]]; then
        cp -R "${REPO_POLICY}/." "$dest/policy/"
    else
        echo "{}" > "$dest/policy/default.json"
    fi
    if [[ -d "${REPO_DEPLOY:-}" ]]; then
        cp -R "${REPO_DEPLOY}/." "$dest/deploy/"
    fi
    if [[ -d "${REPO_OPS:-}" ]]; then
        cp -R "${REPO_OPS}/." "$dest/ops-scripts/"
    fi
fi
exit 0
"""
        # 12. install
        # 假 install: 記錄 argv，並將 -o 剝除以避免 macOS / 非 root 環境 chown 失敗
        install_script = """#!/usr/bin/env bash
set -euo pipefail
echo "install $*" >> "${LOG_DIR}/install-argv.log"

        args=()
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -o)
                    shift; if [[ $# -gt 0 ]]; then shift; fi ;;
                -g)
                    shift; if [[ $# -gt 0 ]]; then shift; fi ;;
                *)
                    args+=("$1"); shift ;;
            esac
        done

exec "${REAL_INSTALL_BIN}" "${args[@]}"
"""
        # 13. curl (for install.sh --check)
        curl_script = """#!/usr/bin/env bash
set -euo pipefail
echo "curl $*" >> "${LOG_DIR}/curl-argv.log"
code="$(cat "${STATE_DIR}/curl_code" 2>/dev/null || echo "200")"
rc="$(cat "${STATE_DIR}/curl_rc" 2>/dev/null || echo "0")"
printf '%s' "$code"
exit "$rc"
"""
        real_install_bin = shutil.which("install") or "/usr/bin/install"

        # 14. ssh
        # 假 ssh: -W (探針) 讀完 stdin 後回一個可設定的 HTTP 狀態列 (預設 200)；
        # 其餘參數 (守護程式的 -N) 直接成功。避免測試真的去連會合點位址。
        ssh_script = """#!/usr/bin/env bash
set -uo pipefail
echo "ssh $*" >> "${LOG_DIR}/ssh-argv.log"

mode="plain"
for a in "$@"; do
    if [[ "$a" == "-W" ]]; then
        mode="probe"
    fi
done

if [[ "$mode" == "probe" ]]; then
    # 探針會把 HTTP 請求寫進 stdin，讀完再回狀態列
    cat >/dev/null 2>&1 || true
    code="$(cat "${LGW_TEST_STATE_DIR}/ssh_probe_code" 2>/dev/null || echo "200")"
    printf 'HTTP/1.0 %s\\r\\n\\r\\n' "$code"
    exit 0
fi

exit 0
"""

        scripts = [
            ("sudo", sudo_script),
            ("useradd", useradd_script),
            ("gpasswd", gpasswd_script),
            ("id", id_script),
            ("getent", getent_script),
            ("loginctl", loginctl_script),
            ("sshd", sshd_script),
            ("ss", ss_script),
            ("ssh-keygen", ssh_keygen_script),
            ("systemctl", systemctl_script),
            ("git", git_script),
            ("install", install_script),
            ("curl", curl_script),
            ("ssh", ssh_script),
        ]

        for name, content in scripts:
            target = os.path.join(self.fakebin, name)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(target, 0o755)

        self.real_install_bin = real_install_bin

    def bootstrap_env(self, env_overrides=None):
        env = {
            "PATH": f"{self.fakebin}:{self.tools}",
            "LOG_DIR": self.logdir,
            "STATE_DIR": self.state,
            "LGW_BOOT_USER": "llmgw",
            "LGW_BOOT_HOME": self.home,
            "LGW_BOOT_RUNDIR": self.rundir,
            "LGW_BOOT_HOSTKEY": self.hostkey_file,
            "LGW_BOOT_OS": "Linux",
            "LGW_TEST_STATE_DIR": self.state,
            "LGW_BOOT_DECRYPT_PROMPT": "0",
            "REAL_INSTALL_SH": INSTALL_SH,
            "REAL_RUN_WRAPPER": RUN_WRAPPER,
            "REAL_SERVICE_UNIT": SERVICE_UNIT,
            "REPO_DEPLOY": REPO_DEPLOY,
            "REPO_OPS": REPO_OPS,
            "REPO_POLICY": REPO_POLICY,
            "REAL_INSTALL_BIN": self.real_install_bin,
        }
        if env_overrides:
            env.update(env_overrides)
        return env

    def run_bootstrap(self, args, env_overrides=None):
        cmd = ["bash", BOOTSTRAP_SH] + list(args)
        return subprocess.run(cmd, env=self.bootstrap_env(env_overrides),
                              capture_output=True, text=True)

    def read_log(self, name):
        p = os.path.join(self.logdir, f"{name}-argv.log")
        if not os.path.exists(p):
            return ""
        with open(p, "r", encoding="utf-8") as f:
            return f.read()

    def assert_summary_printed(self, stdout):
        """摘要三段都在：隧道公鑰、host 公鑰、待辦清單"""
        self.assertIn("開機設定摘要 (Bootstrap Summary)", stdout)
        self.assertIn("1. 隧道公鑰", stdout)
        self.assertIn("2. 這台的 ed25519 host 公鑰", stdout)
        self.assertIn("3. 待辦事項 (Pending Todos):", stdout)

    # -------------------------------------------------------------------------
    # 測試項目
    # -------------------------------------------------------------------------
    def test_syntax_and_executable(self):
        self.assertTrue(os.path.isfile(BOOTSTRAP_SH))
        self.assertTrue(os.access(BOOTSTRAP_SH, os.X_OK))
        res = subprocess.run(["bash", "-n", BOOTSTRAP_SH], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_fresh_machine_run_and_idempotence(self):
        """全新機器跑一次全部 changed/todo 正確，第二次跑 changed 為 0"""
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        # 第一次執行 (全新機器)
        res1 = self.run_bootstrap(args)
        self.assertEqual(res1.returncode, 0, f"stdout:\n{res1.stdout}\nstderr:\n{res1.stderr}")

        # 檢查輸出包含各步驟狀態
        self.assertIn("[prereq] ok", res1.stdout)
        self.assertIn("[user] changed", res1.stdout)
        self.assertIn("[linger] changed", res1.stdout)
        self.assertIn("[sshd] ok", res1.stdout)
        self.assertIn("[install] changed", res1.stdout)
        self.assertIn("[tunnel-key] changed", res1.stdout)
        self.assertIn("[keysync] changed", res1.stdout)
        self.assertIn("[rendezvous] changed", res1.stdout)
        self.assertIn("[check] ok", res1.stdout)
        self.assertIn("無 (全部完成)", res1.stdout)

        # 清除日誌準備第二次執行
        for f in os.listdir(self.logdir):
            os.remove(os.path.join(self.logdir, f))

        # 第二次執行 (冪等性驗證)
        res2 = self.run_bootstrap(args)
        self.assertEqual(res2.returncode, 0, f"stdout:\n{res2.stdout}\nstderr:\n{res2.stderr}")

        # 驗證第二次執行無任何 changed
        self.assertNotIn("changed", res2.stdout)
        self.assertIn("[user] ok", res2.stdout)
        self.assertIn("[linger] ok", res2.stdout)
        self.assertIn("[install] ok", res2.stdout)
        self.assertIn("[tunnel-key] ok", res2.stdout)
        self.assertIn("[keysync] ok", res2.stdout)
        self.assertIn("[rendezvous] ok", res2.stdout)
        self.assertIn("[check] ok", res2.stdout)

    def test_password_never_leaks(self):
        """密碼檔的內容（用假字串）不出現在 stdout、stderr、argv 紀錄、其他生成檔案裡。

        解密密碼是例外：它必須寫進 ~/.config/llmgateway/keysync.env（0600），
        這是它的去處；除此之外任何地方都不該出現。
        """
        sudo_canary = "MOCK_CANARY_SUDO_PASSWORD_SECRET_987654"
        decrypt_canary = "MOCK_CANARY_DECRYPT_PASSWORD_SECRET_123456"
        passfile = os.path.join(self.sandbox, "sudo_pass.txt")
        with open(passfile, "w", encoding="utf-8") as f:
            f.write(sudo_canary + "\n")
        decrypt_passfile = os.path.join(self.sandbox, "decrypt_pass.txt")
        with open(decrypt_passfile, "w", encoding="utf-8") as f:
            f.write(decrypt_canary + "\n")

        args = [
            "--tag", "lgw-v0.1.0",
            "--sudo-pass-file", passfile,
            "--decrypt-pass-file", decrypt_passfile,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 0, res.stderr)

        keysync_env = os.path.join(self.home, ".config", "llmgateway", "keysync.env")
        # 1. 兩個值都不在 stdout / stderr
        for canary in (sudo_canary, decrypt_canary):
            self.assertNotIn(canary, res.stdout)
            self.assertNotIn(canary, res.stderr)

        # 2. 兩個值都不在任何 argv 紀錄中
        for log_name in os.listdir(self.logdir):
            content = self.read_log(log_name.replace("-argv.log", ""))
            for canary in (sudo_canary, decrypt_canary):
                self.assertNotIn(canary, content, f"Leaked in {log_name}")

        # 3. 解密密碼只出現在 keysync.env（0600）裡，其他生成檔案都沒有
        self.assertTrue(os.path.isfile(keysync_env))
        self.assertEqual(stat.S_IMODE(os.stat(keysync_env).st_mode), 0o600)
        with open(keysync_env, "r", encoding="utf-8") as f:
            keysync_content = f.read()
        self.assertIn(f"LLMGW_KEYS_PASSWORD={decrypt_canary}\n", keysync_content)
        for root, _, files in os.walk(self.home):
            for file in files:
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        fc = f.read()
                except Exception:
                    continue
                self.assertNotIn(sudo_canary, fc, f"Leaked in {filepath}")
                if filepath != keysync_env:
                    self.assertNotIn(decrypt_canary, fc, f"Leaked in {filepath}")

    def test_docker_group_removal(self):
        """帳號已在 docker 群組時會被移出"""
        self._set_state("user_exists", "1")
        self._set_state("user_in_docker", "1")

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 0, res.stderr)

        gpasswd_log = self.read_log("gpasswd")
        self.assertIn("gpasswd -d llmgw docker", gpasswd_log)
        self.assertIn("[user] changed 將帳號 llmgw 自 docker 群組移出", res.stdout)
        self.assertEqual(self._get_state("user_in_docker"), "0")

    def test_tunnel_key_not_regenerated_if_exists(self):
        """隧道金鑰已存在時不重產"""
        ssh_dir = os.path.join(self.home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        key_path = os.path.join(ssh_dir, "id_llmgw_tunnel")
        key_pub_path = os.path.join(ssh_dir, "id_llmgw_tunnel.pub")
        original_priv = "CANARY_EXISTING_PRIVATE_KEY_CONTENT\n"
        original_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExistingTunnelPub CanaryComment\n"

        with open(key_path, "w", encoding="utf-8") as f:
            f.write(original_priv)
        with open(key_pub_path, "w", encoding="utf-8") as f:
            f.write(original_pub)

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 0, res.stderr)

        keygen_log = self.read_log("ssh-keygen")
        self.assertNotIn("id_llmgw_tunnel", keygen_log)
        self.assertIn("[tunnel-key] ok 隧道金鑰已存在", res.stdout)

        with open(key_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original_priv)
        with open(key_pub_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original_pub)

    def test_keysync_env_management_and_permissions(self):
        """keysync.env：第一次寫入（0600）、重跑不動、既有內容絕不覆蓋"""
        conf_dir = os.path.join(self.home, ".config", "llmgateway")
        keysync_env = os.path.join(conf_dir, "keysync.env")

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        # 1. 第一次寫入解密密碼
        res1 = self.run_bootstrap(args)
        self.assertEqual(res1.returncode, 0, res1.stdout + res1.stderr)
        self.assertIn("[keysync] changed", res1.stdout)

        with open(keysync_env, "r", encoding="utf-8") as f:
            written = f.read()
        self.assertIn(f"LLMGW_KEYS_PASSWORD={self.decrypt_password}\n", written)
        self.assertEqual(stat.S_IMODE(os.stat(keysync_env).st_mode), 0o600)

        # 2. 既有檔案存在時：就算給不同的密碼檔也不覆蓋
        other_pass = os.path.join(self.sandbox, "other_pass.txt")
        with open(other_pass, "w", encoding="utf-8") as f:
            f.write("FakeOtherPassword_ShouldNotWin_02\n")
        args_other = list(args)
        args_other[args_other.index(self.decrypt_pass_file)] = other_pass

        res2 = self.run_bootstrap(args_other)
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
        self.assertIn("[keysync] ok 解密密碼已就位", res2.stdout)

        with open(keysync_env, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), written)
        self.assertNotIn("FakeOtherPassword_ShouldNotWin_02", res2.stdout + res2.stderr)

        # 3. 密碼檔格式錯（多行）→ 這一步 fail，不動既有的檔
        two_lines = os.path.join(self.sandbox, "two_lines_pass.txt")
        with open(two_lines, "w", encoding="utf-8") as f:
            f.write("first\nsecond\n")
        os.remove(keysync_env)
        args_bad = list(args)
        args_bad[args_bad.index(self.decrypt_pass_file)] = two_lines
        res3 = self.run_bootstrap(args_bad)
        self.assertEqual(res3.returncode, 1, res3.stdout + res3.stderr)
        self.assertIn("[keysync] fail", res3.stdout)
        self.assertFalse(os.path.exists(keysync_env))

    def test_互動輸入解密密碼不回顯也不外洩(self):
        """接在終端機時（stdin 是 tty）可以互動輸入；密碼寫進 keysync.env，不進 argv／輸出"""
        import pty

        master, slave = pty.openpty()
        args = [
            "--tag", "lgw-v0.1.0",
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]
        env = self.bootstrap_env({"LGW_BOOT_DECRYPT_PROMPT": "1"})
        proc = subprocess.Popen(
            ["bash", BOOTSTRAP_SH] + args, stdin=slave,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        os.close(slave)
        try:
            # 等提示出現（等同本人看到提示才輸入）：read -s 之前先送進去的會被丟掉，
            # 所以要等提示出來、read 開始之後才寫入。
            seen = ""
            prompt_seen = False
            deadline = time.time() + 60
            while time.time() < deadline:
                chunk = proc.stderr.read(1)
                if not chunk:
                    break
                seen += chunk
                if "解密密碼" in seen:
                    prompt_seen = True
                    break
            self.assertTrue(prompt_seen, f"沒看到互動提示；stderr={seen!r}")
            time.sleep(0.3)
            os.write(master, (self.decrypt_password + "\n").encode())
            out, err = proc.communicate(timeout=60)
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        err = seen + err

        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("[keysync] changed", out)
        keysync_env = os.path.join(self.home, ".config", "llmgateway", "keysync.env")
        with open(keysync_env, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn(f"LLMGW_KEYS_PASSWORD={self.decrypt_password}\n", content)
        self.assertNotIn(self.decrypt_password, out)
        self.assertNotIn(self.decrypt_password, err)
        for log_name in os.listdir(self.logdir):
            self.assertNotIn(self.decrypt_password,
                             self.read_log(log_name.replace("-argv.log", "")))

    def test_沒有終端機又不給密碼檔時記todo(self):
        """非互動（stdin 不是終端機）且沒給 --decrypt-pass-file 時要 todo、不要等人輸入"""
        proc = subprocess.run(
            ["bash", BOOTSTRAP_SH, "--tag", "lgw-v0.1.0"],
            env=self.bootstrap_env({"LGW_BOOT_DECRYPT_PROMPT": "1"}),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("[keysync] todo 未提供解密密碼", proc.stdout)
        self.assertFalse(
            os.path.exists(os.path.join(self.home, ".config", "llmgateway", "keysync.env")))

    def test_check_mode_changes_nothing(self):
        """--check 不改任何東西 (argv 紀錄裡沒有 useradd、loginctl enable-linger、install 等)"""
        args = [
            "--check",
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        # 由於 user 不存在且服務尚未安裝，--check 會回傳 3 (有待辦/todo)
        self.assertEqual(res.returncode, 3)

        # 驗證 argv 紀錄中絕無修改動作
        self.assertEqual(self.read_log("useradd"), "")
        self.assertEqual(self.read_log("gpasswd"), "")
        self.assertNotIn("enable-linger", self.read_log("loginctl"))
        self.assertEqual(self.read_log("git"), "")

        # install 紀錄中不應有寫入/複製
        install_log = self.read_log("install")
        self.assertEqual(install_log, "")

    def test_check_probe_uses_fake_ssh(self):
        """install.sh --check 的會合點探針走 fake ssh（不連真位址），回 200 時 check 通過"""
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("[check] ok", res.stdout)

        ssh_log = self.read_log("ssh")
        self.assertIn("-W 127.0.0.1:4710", ssh_log)
        self.assertIn("sshproxy@jump.example.com", ssh_log)

    def test_check_probe_non_200_is_fail(self):
        """探針回非 200 時 check 記 fail、結束碼 1（不是 todo）"""
        self._set_state("ssh_probe_code", "503")

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 1, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("[check] fail install.sh --check 未通過 (rc=1)", res.stdout)

    def test_sudo_command_failure_fails_the_step(self):
        """某個 sudo -n 指令失敗 → 該步記 fail、不印 changed、結束碼 1（H1 失敗傳播）"""
        self._set_state("sudo_fail_substr", "git clone")

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 1, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("[install] fail git clone", res.stdout)
        self.assertNotIn("[install] changed", res.stdout)
        # 失敗之後不再往下走
        self.assertNotIn("[check]", res.stdout)
        # 失敗也要跑摘要
        self.assert_summary_printed(res.stdout)

    def test_failed_step_still_prints_summary(self):
        """前面某步就 fail 時也要有摘要：公鑰與待辦清單兩段都印出來"""
        self._set_state("sudo_fail_substr", "useradd")

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 1, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("[user] fail useradd", res.stdout)
        self.assertNotIn("[linger]", res.stdout)
        self.assert_summary_printed(res.stdout)

    def test_sudo_no_cache_check_refreshes_before_sshd(self):
        """沒有 sudo 快取時，--check 也要先 sudo -v；sshd 檢查不因 sudo -n 失敗而記假 todo"""
        self._set_state("user_exists", "1")
        self._set_state("user_linger", "1")
        self._set_state("sudo_no_cache", "1")

        args = [
            "--check",
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 3, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("[sshd] ok", res.stdout)

        sudo_lines = [line for line in self.read_log("sudo").splitlines() if line.strip()]
        first_n = next((i for i, line in enumerate(sudo_lines) if " -n " in line), None)
        self.assertIsNotNone(first_n, f"no sudo -n call: {sudo_lines}")
        self.assertIn("sudo -v", sudo_lines[:first_n],
                      f"sudo -v must come before the first sudo -n: {sudo_lines[:first_n + 1]}")

    def test_home_reads_go_through_sudo(self):
        """讀 ~llmgw 的動作都經 sudo：家目錄 0700/0750 時管理帳號自己讀不到"""
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res1 = self.run_bootstrap(args)
        self.assertEqual(res1.returncode, 0, f"stdout:\n{res1.stdout}\nstderr:\n{res1.stderr}")

        # 第二次執行要讀：已安裝版本、隧道金鑰是否存在、keysync.env、
        # 會合點 known_hosts、tunnel.env、install.sh 是否存在
        for f in os.listdir(self.logdir):
            os.remove(os.path.join(self.logdir, f))

        res2 = self.run_bootstrap(args)
        self.assertEqual(res2.returncode, 0, f"stdout:\n{res2.stdout}\nstderr:\n{res2.stderr}")

        sudo_log = self.read_log("sudo")
        expected = [
            f"test -e {os.path.join(self.home, '.ssh', 'id_llmgw_tunnel')}",
            f"test -e {os.path.join(self.home, '.ssh', 'known_hosts_rendezvous')}",
            f"test -e {os.path.join(self.home, '.config', 'llmgateway', 'keysync.env')}",
            f"test -e {os.path.join(self.home, '.config', 'llmgateway', 'tunnel.env')}",
            f"cat {os.path.join(self.home, '.ssh', 'known_hosts_rendezvous')}",
            f"cat {os.path.join(self.home, '.config', 'llmgateway', 'tunnel.env')}",
            f"cat {os.path.join(self.home, '.config', 'llmgateway', 'keysync.env')}",
            f"cat {os.path.join(self.home, '.local', 'share', 'llmgateway', 'current', '.lgw-tag')}",
            f"test -e {os.path.join(self.home, '.local', 'share', 'llmgateway', 'current', 'deploy', 'install.sh')}",
        ]
        for needle in expected:
            self.assertIn(needle, sudo_log, f"not read through sudo: {needle}")

    def test_rendezvous_argument_validation_exit_2(self):
        """--rendezvous 格式錯與無效的 known_hosts 都回 2"""
        bad_rendezvous = [
            "sshproxy@jump.example.com",        # 缺埠
            "sshproxy@jump.example.com:0",      # 埠 0
            "sshproxy@jump.example.com:70000",  # 埠超出範圍
            "sshproxy@jump.example.com:abc",    # 埠不是數字
            "jump.example.com:2222",            # 沒有 user
            "sshproxy@:2222",                   # 沒有 host
        ]
        for rv in bad_rendezvous:
            res = self.run_bootstrap([
                "--tag", "lgw-v0.1.0",
                "--rendezvous", rv,
                "--rendezvous-known-hosts", self.known_hosts_file,
            ])
            self.assertEqual(res.returncode, 2, f"Failed for rendezvous: {rv}")

        empty_kh = os.path.join(self.sandbox, "empty_known_hosts")
        with open(empty_kh, "w", encoding="utf-8"):
            pass
        res = self.run_bootstrap([
            "--tag", "lgw-v0.1.0",
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", empty_kh,
        ])
        self.assertEqual(res.returncode, 2, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")

    def test_argument_errors_exit_2(self):
        """參數錯回 2"""
        cases = [
            ["--tag", "invalid_tag"],
            ["--tag", "lgw-v1.0"],
            [],  # 沒有 --tag 且不是 --check
            ["--rendezvous", "sshproxy@jump:2222"],  # 缺少 --rendezvous-known-hosts
            ["--rendezvous-known-hosts", self.known_hosts_file],  # 缺少 --rendezvous
            ["--sudo-pass-file", "/nonexistent/pass.txt"],
            ["--decrypt-pass-file", "/nonexistent/pass.txt"],
            ["--rendezvous-known-hosts", "/nonexistent/kh.txt"],
            ["--tag"],  # 缺少值
            ["--unknown-arg"],
        ]
        for c in cases:
            res = self.run_bootstrap(c)
            self.assertEqual(res.returncode, 2, f"Failed for args: {c}")

    def test_todos_exit_3(self):
        """有 todo 時回 3"""
        # 未提供 --decrypt-pass-file 與 --rendezvous
        args = [
            "--tag", "lgw-v0.1.0",
        ]
        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 3, f"stdout:\n{res.stdout}")
        self.assertIn("[keysync] todo 未提供解密密碼", res.stdout)
        self.assertIn("[rendezvous] todo 未提供會合點設定", res.stdout)
        self.assertIn("3. 待辦事項 (Pending Todos):", res.stdout)
        self.assertIn("未提供解密密碼", res.stdout)
        self.assertIn("未提供會合點設定", res.stdout)

    def test_sshd_todo_reporting(self):
        """sshd 不符時回 todo 且說明要改什麼"""
        self._set_state("sshd_pubkey", "no")
        self._set_state("sshd_deny", "llmgw")
        self._set_state("sshd_listen", "0")

        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 3)
        self.assertIn("[sshd] todo", res.stdout)
        self.assertIn("PubkeyAuthentication", res.stdout)
        self.assertIn("DenyUsers", res.stdout)
        self.assertIn("ListenAddress 127.0.0.1:22", res.stdout)


if __name__ == "__main__":
    unittest.main()
