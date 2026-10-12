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

        # sudo 密碼檔（假值；單行）
        self.sudo_pass_file = os.path.join(self.sandbox, "sudo_pass.txt")
        self.sudo_password = "FakeBootstrapSudoPassword_03"
        with open(self.sudo_pass_file, "w", encoding="utf-8") as f:
            f.write(self.sudo_password + "\n")

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
        self._set_state("sshd_T_fail", "0")
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
        # 一種真的 sudo 行為模擬（環境變數）：
        #   LGW_SUDO_CACHE_PPID=1：快取跟著 parent pid 走（沒有 tty 時的真機
        #     行為）。-v 記下呼叫者的 PPID；-n 時 PPID 不同就沒有快取，回
        #     「sudo: interactive authentication is required」。
        #     腳本要在同一個 shell 層級先 -v 再 -n 才過得了：$(...) 子 shell
        #     的 PPID 跟主腳本不同，只在步驟開頭刷新的快取拿不到。
        sudo_script = """#!/usr/bin/env bash
set -uo pipefail
echo "sudo $*" >> "${LOG_DIR}/sudo-argv.log"
# 另記一條帶呼叫者 PPID 的紀錄，測試用來驗證「每個 sudo -n 之前，
# 同一個 parent pid 都有 sudo -v 過」
echo "$PPID|$*" >> "${LOG_DIR}/sudo-ppid.log"

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

if [[ "${LGW_SUDO_CACHE_PPID:-0}" == "1" ]]; then
    if [[ $has_v -eq 1 ]]; then
        echo "$PPID" > "${LGW_TEST_STATE_DIR}/sudo-v-ppid"
    elif [[ $has_n -eq 1 ]]; then
        cached_ppid="$(cat "${LGW_TEST_STATE_DIR}/sudo-v-ppid" 2>/dev/null || true)"
        if [[ -z "$cached_ppid" || "$cached_ppid" != "$PPID" ]]; then
            echo "sudo: interactive authentication is required" >&2
            exit 1
        fi
    fi
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
elif [[ "${1:-}" == "-un" || "${1:-}" == "--user-name" ]]; then
    echo "llmgw"
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
# sshd_T_fail=1：模擬 sudo sshd -T 失敗（沒權限、設定檔壞掉）。
# bootstrap 要記 fail「無法執行 sshd -T」，不能當成「未啟用 PubkeyAuthentication」
for a in "$@"; do
    if [[ "$a" == "-T" ]]; then
        if [[ "$(cat "${STATE_DIR}/sshd_T_fail" 2>/dev/null || echo 0)" == "1" ]]; then
            echo "fake sshd: /etc/ssh/sshd_config: Permission denied" >&2
            exit 1
        fi
        break
    fi
done
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
if [[ "$*" == *"is-failed"* ]]; then
    # 0＝failed；其他＝沒失敗
    if [[ "$(cat "${STATE_DIR}/keysync_failed" 2>/dev/null || echo 0)" == "1" ]]; then
        exit 0
    fi
    exit 1
fi
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

        # 15. gh（bootstrap 的 gh-auth 步驟與 install.sh --check 都用）
        # 狀態：${LGW_TEST_STATE_DIR}/gh_auth = ok | logged_out | repo_denied
        # 注意：狀態檔要用 LGW_TEST_STATE_DIR——install.sh 自己會覆寫 STATE_DIR，
        # 傳下去的 STATE_DIR 不會是測試的狀態目錄。
        gh_script = """#!/usr/bin/env bash
set -uo pipefail
echo "gh $*" >> "${LOG_DIR}/gh-argv.log"
state="$(cat "${LGW_TEST_STATE_DIR}/gh_auth" 2>/dev/null || echo ok)"
if [[ "$state" == "missing" ]]; then
    # 模擬「沒裝 gh」：bash 找不到指令時回的就是 127
    echo "bash: gh: command not found" >&2
    exit 127
fi
if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
    if [[ "$state" == "logged_out" ]]; then
        echo "You are not logged into any GitHub hosts. To log in, run: gh auth login" >&2
        exit 1
    fi
    exit 0
fi
if [[ "${1:-}" == "api" ]]; then
    if [[ "$state" == "repo_denied" ]]; then
        echo "gh: Could not resolve to a Repository with the name 'x/y'." >&2
        exit 1
    fi
    printf 'FATESAIKOU/LLMGateway-keys\\n'
    exit 0
fi
echo "fake gh: unsupported: $*" >&2
exit 1
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
            ("gh", gh_script),
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
            # 真機上 install.sh 是 bootstrap 以 llmgw 身分跑的，服務帳號與目前
            # 使用者相同、install.sh 不會自己 sudo。測試的家目錄 basename 不是
            # llmgw，所以明確指定，讓 install.sh 的行為跟真機一致（也才測得到
            # bootstrap 自己的 sudo 包裝）。
            "LGW_SERVICE_USER": "llmgw",
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

    def test_keysync_env沒有非空密碼行時會被補上(self):
        """既有檔但沒有非空密碼行（空檔／只有註解／VAR= 空值）視為沒放，會被寫掉補上。

        這是 bootstrap 的實際行為，腳本註解也照這樣寫（審查 #13 低-4：
        原本註解說「絕不覆蓋」過度絕對）。
        """
        conf_dir = os.path.join(self.home, ".config", "llmgateway")
        keysync_env = os.path.join(conf_dir, "keysync.env")
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        for name, content in (
            ("empty", ""),
            ("comment_only", "# 只有註解\n"),
            ("blank_value", "LLMGW_KEYS_PASSWORD=\n"),
        ):
            with self.subTest(state=name):
                os.makedirs(conf_dir, exist_ok=True)
                with open(keysync_env, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chmod(keysync_env, 0o600)
                res = self.run_bootstrap(args)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertIn("[keysync] changed", res.stdout)
                with open(keysync_env, "r", encoding="utf-8") as f:
                    self.assertIn(f"LLMGW_KEYS_PASSWORD={self.decrypt_password}", f.read())
                self.assertEqual(stat.S_IMODE(os.stat(keysync_env).st_mode), 0o600)

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

    def test_gh沒登入時check會說gh不能用(self):
        """v5：gh 登入是使用前提、bootstrap 不做登入；--check 看得出 gh 不能用"""
        self._set_state("gh_auth", "logged_out")
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("[check] fail", res.stdout)
        self.assertIn("gh 沒登入或 token 失效", res.stdout)

        # bootstrap 只問 gh 狀態，不代做登入
        gh_log = self.read_log("gh")
        self.assertIn("auth status", gh_log)
        self.assertNotIn("auth login", gh_log)
        self.assertNotIn("auth token", gh_log)

    def test_gh已登入時gh_auth步驟ok(self):
        """已登入 → [gh-auth] ok，而且兩步驟的順序在 user 之後"""
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]
        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("[gh-auth] ok llmgw 已登入 gh", res.stdout)

        lines = [ln for ln in res.stdout.splitlines() if ln.startswith("[")]
        steps = [ln.split("]")[0].lstrip("[") for ln in lines]
        self.assertIn("gh-auth", steps)
        self.assertLess(steps.index("user"), steps.index("gh-auth"))
        self.assertLess(steps.index("gh-auth"), steps.index("linger"))

    def test_gh沒登入時記todo結束碼3且照常印摘要(self):
        """沒登入 → todo（不是 fail）、結束碼 3、摘要照印，人照訊息去登入再重跑"""
        self._set_state("gh_auth", "logged_out")
        args = ["--tag", "lgw-v0.1.0", "--decrypt-pass-file", self.decrypt_pass_file]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("[gh-auth] todo llmgw 的 gh 尚未登入", res.stdout)
        self.assertIn("gh auth login", res.stdout)          # 訊息要寫明本人怎麼做
        self.assertIn("sudo -u llmgw -i", res.stdout)
        self.assertIn("重跑本腳本", res.stdout)
        # 後面的步驟照跑、摘要照印
        self.assertIn("[linger]", res.stdout)
        self.assert_summary_printed(res.stdout)
        self.assertNotIn("[gh-auth] fail", res.stdout)

    def test_gh沒裝時gh_auth步驟fail(self):
        """gh 沒裝 → fail（不是 todo）：拉金鑰的路根本不存在，結束碼 1"""
        self._set_state("gh_auth", "missing")
        res = self.run_bootstrap(["--tag", "lgw-v0.1.0"])
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("[gh-auth] fail 找不到 gh", res.stdout)
        self.assertNotIn("[linger]", res.stdout)  # 停在這裡
        self.assert_summary_printed(res.stdout)

    def test_gh_auth步驟在check模式也不寫任何東西(self):
        """--check 時 gh-auth 只報狀態，不碰系統"""
        self._set_state("gh_auth", "logged_out")
        res = self.run_bootstrap(["--check", "--tag", "lgw-v0.1.0"])
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("[gh-auth] todo", res.stdout)
        self.assertEqual(self.read_log("useradd"), "")
        self.assertEqual(self.read_log("install"), "")

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

    def test_summary_public_keys_strip_comments(self):
        """摘要的兩處公鑰只印前兩欄：註解（含機器名）不得出現在輸出（L-R8＋備查硬化）"""
        args = [
            "--tag", "lgw-v0.1.0",
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args)
        self.assertEqual(res.returncode, 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assert_summary_printed(res.stdout)
        # host key fixture 的註解 root@mockhost 不得出現，但金鑰本體要在
        self.assertNotIn("root@mockhost", res.stdout)
        self.assertIn("FakeHostKeyForBootstrapTest999", res.stdout)
        # 隧道 .pub 只印前兩欄：含註解的整行不得出現，去註解的行要在
        # （註：stdout 別處仍有 llmgateway-tunnel 字樣如 unit 名與排程提醒，
        # 這裡只斷言公鑰行本身去註解）。
        self.assertNotIn("FakeTunnelPubKeyGenerated123 llmgateway-tunnel", res.stdout)
        self.assertIn(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeTunnelPubKeyGenerated123\n",
            res.stdout)
        self.assertIn("FakeTunnelPubKeyGenerated123", res.stdout)

    # -------------------------------------------------------------------------
    # #18 sudo 驗證快取跟著 parent pid
    # -------------------------------------------------------------------------
    def _sudo_calls(self):
        """讀 sudo-ppid.log，回 [(ppid, argv)]。log 清空就回空 list。"""
        p = os.path.join(self.logdir, "sudo-ppid.log")
        if not os.path.exists(p):
            return []
        out = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                if not line.strip():
                    continue
                ppid, _, argv = line.partition("|")
                out.append((ppid, argv))
        return out

    def assert_every_sudo_n_was_validated(self):
        """每個 sudo -n 之前，同一個 parent pid 都已經 sudo -v 過

        這正是真機的行為：沒有 tty 時 sudo 的快取跟著 parent pid 走，
        $(...) 子 shell 的 PPID 跟主腳本不同，所以每個 -n 都必須在
        同一層級先 -v。
        """
        validated = set()
        bare = []
        for ppid, argv in self._sudo_calls():
            if " -v " in f" {argv} " or argv.rstrip().endswith(" -v"):
                validated.add(ppid)
            elif " -n " in f" {argv} ":
                if ppid not in validated:
                    bare.append((ppid, argv))
        self.assertEqual(bare, [], f"這些 sudo -n 沒有先在同一個 parent pid 上 -v: {bare}")

    def _ppid_env(self):
        """開啟假 sudo 的「快取跟著 parent pid」模式（真機沒有 tty 時的行為）"""
        return {"LGW_SUDO_CACHE_PPID": "1"}

    def _ppid_args(self, extra=None):
        """帶密碼檔的 args；--rendezvous 由 extra 決定"""
        args = [
            "--tag", "lgw-v0.1.0",
            "--sudo-pass-file", self.sudo_pass_file,
            "--decrypt-pass-file", self.decrypt_pass_file,
        ]
        return args + list(extra or [])

    def test_sudo_cache_ppid_first_run_no_rendezvous(self):
        """第一次跑（沒有 --rendezvous、gh 沒登入）在快取跟著 parent pid 的
        真機上也要結束碼是 3：$(...) 子 shell 裡的 sudo -n 必須自己驗證過"""
        self._set_state("gh_auth", "logged_out")

        res = self.run_bootstrap(self._ppid_args(), self._ppid_env())
        self.assertEqual(res.returncode, 3, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}")

        # 偽 todo 不見了：gh 是真的沒登入，不是 sudo 拿不到快取
        self.assertIn("[gh-auth] todo llmgw 的 gh 尚未登入", res.stdout)
        self.assertNotIn("[gh-auth] fail", res.stdout)
        # 摘要讀得到隧道公鑰（經 run_as_llmgw cat）
        self.assertIn("FakeTunnelPubKeyGenerated123", res.stdout)
        self.assertIn("1. 隧道公鑰", res.stdout)
        self.assertNotIn("(無法讀取", res.stdout)
        # sshd 結果正確：不是被 sudo 失敗拖成假 todo
        self.assertIn("[sshd] ok", res.stdout)
        # check 問 tunnel.env 也不會被 sudo 卡住：不存在是「還沒填」，不是讀不到
        self.assertIn("[check] todo", res.stdout)
        self.assertNotIn("[check] fail", res.stdout)
        # 沒有半個 sudo -n 是裸跑的：每一個 -n 之前，同一個 parent pid 都有 -v
        self.assert_every_sudo_n_was_validated()

    def test_sudo_cache_ppid_rerun_reads_version_file(self):
        """已安裝、第二次跑（帶 --rendezvous）讀得到已安裝版本檔，走到
        rendezvous 並寫好 tunnel.env（真機曾在此 fail）"""
        first = self.run_bootstrap(self._ppid_args(), self._ppid_env())
        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)

        second = self.run_bootstrap(
            self._ppid_args([
                "--rendezvous", "sshproxy@jump.example.com:2222",
                "--rendezvous-known-hosts", self.known_hosts_file,
            ]),
            self._ppid_env())
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        # 讀得到已安裝版本檔（真機曾經 fail 在這裡）
        self.assertNotIn("無法讀取已安裝版本檔", second.stdout)
        self.assertIn("[install] ok 程式版本已是 lgw-v0.1.0", second.stdout)
        # 走到 rendezvous 並寫好 tunnel.env
        self.assertIn("[rendezvous] changed", second.stdout)
        tunnel_env = os.path.join(self.home, ".config", "llmgateway", "tunnel.env")
        with open(tunnel_env, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("RENDEZVOUS=sshproxy@jump.example.com:2222", content)
        self.assertIn("IDENTITY=", content)
        self.assertIn("KNOWN_HOSTS=", content)

    def test_sshd_T_failure_is_fail_not_todo(self):
        """sshd -T 執行失敗 → fail，不當成「未啟用 PubkeyAuthentication」"""
        self._set_state("sshd_T_fail", "1")
        args = [
            "--tag", "lgw-v0.1.0",
            "--sudo-pass-file", self.sudo_pass_file,
            "--decrypt-pass-file", self.decrypt_pass_file,
            "--rendezvous", "sshproxy@jump.example.com:2222",
            "--rendezvous-known-hosts", self.known_hosts_file,
        ]

        res = self.run_bootstrap(args, self._ppid_env())
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("[sshd] fail 無法執行 sshd -T", res.stdout)
        self.assertNotIn("[sshd] todo", res.stdout)
        self.assertNotIn("PubkeyAuthentication", res.stdout)
        self.assert_summary_printed(res.stdout)


if __name__ == "__main__":
    unittest.main()
