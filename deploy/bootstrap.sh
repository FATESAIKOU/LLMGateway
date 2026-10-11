#!/usr/bin/env bash
# deploy/bootstrap.sh
#
# LLMGateway: 放服務那台的一次回機設定腳本 (tasks 3.3; docs/deploy.md §1)
# 由具備 sudo 權限之帳號執行，可重複執行 (冪等)。
set -euo pipefail

TAG_REGEX='^lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'

usage() {
    cat << 'EOF' >&2
用法: bootstrap.sh --tag lgw-vX.Y.Z [選項]
選項:
  --tag lgw-vX.Y.Z              要安裝的版本 (格式同 install.sh)
  --sudo-pass-file PATH         sudo 密碼檔路徑 (選填；未給則使用互動 sudo)
  --deploy-pubkey-file PATH     部署公鑰檔案路徑 (選填，單行 OpenSSH 公鑰)
  --rendezvous ADDR             會合點 sshproxy@<host>:<port> (選填，需與 --rendezvous-known-hosts 合併使用)
  --rendezvous-known-hosts PATH 會合點 known_hosts 檔路徑 (選填)
  --check                       僅檢查現狀，不修改系統狀態
  --reinstall                   即使已安裝同版本仍重跑 install.sh
  -h, --help                    顯示說明
EOF
}

# -----------------------------------------------------------------------------
# 參數解析與驗證
# -----------------------------------------------------------------------------
TAG=""
SUDO_PASS_FILE=""
DEPLOY_PUBKEY_FILE=""
RENDEZVOUS=""
RENDEZVOUS_KNOWN_HOSTS=""
CHECK_ONLY=0
REINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            [[ $# -ge 2 ]] || { echo "錯誤: --tag 缺少參數" >&2; exit 2; }
            TAG="$2"; shift 2 ;;
        --sudo-pass-file)
            [[ $# -ge 2 ]] || { echo "錯誤: --sudo-pass-file 缺少參數" >&2; exit 2; }
            SUDO_PASS_FILE="$2"; shift 2 ;;
        --deploy-pubkey-file)
            [[ $# -ge 2 ]] || { echo "錯誤: --deploy-pubkey-file 缺少參數" >&2; exit 2; }
            DEPLOY_PUBKEY_FILE="$2"; shift 2 ;;
        --rendezvous)
            [[ $# -ge 2 ]] || { echo "錯誤: --rendezvous 缺少參數" >&2; exit 2; }
            RENDEZVOUS="$2"; shift 2 ;;
        --rendezvous-known-hosts)
            [[ $# -ge 2 ]] || { echo "錯誤: --rendezvous-known-hosts 缺少參數" >&2; exit 2; }
            RENDEZVOUS_KNOWN_HOSTS="$2"; shift 2 ;;
        --check)
            CHECK_ONLY=1; shift ;;
        --reinstall)
            REINSTALL=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "錯誤: 未知參數 $1" >&2; usage; exit 2 ;;
    esac
done

if [[ -n "$TAG" ]]; then
    if [[ ! "$TAG" =~ $TAG_REGEX ]]; then
        echo "錯誤: tag 格式不符 (需符合 ${TAG_REGEX}): ${TAG}" >&2
        exit 2
    fi
fi

if [[ $CHECK_ONLY -eq 0 && -z "$TAG" ]]; then
    echo "錯誤: 未指定 --tag" >&2
    usage
    exit 2
fi

if [[ -n "$RENDEZVOUS" && -z "$RENDEZVOUS_KNOWN_HOSTS" ]] || [[ -z "$RENDEZVOUS" && -n "$RENDEZVOUS_KNOWN_HOSTS" ]]; then
    echo "錯誤: --rendezvous 與 --rendezvous-known-hosts 必須同時提供" >&2
    exit 2
fi

if [[ -n "$SUDO_PASS_FILE" && ! -f "$SUDO_PASS_FILE" ]]; then
    echo "錯誤: sudo 密碼檔不存在" >&2
    exit 2
fi

if [[ -n "$DEPLOY_PUBKEY_FILE" && ! -f "$DEPLOY_PUBKEY_FILE" ]]; then
    echo "錯誤: 部署公鑰檔案不存在" >&2
    exit 2
fi

if [[ -n "$RENDEZVOUS_KNOWN_HOSTS" && ! -f "$RENDEZVOUS_KNOWN_HOSTS" ]]; then
    echo "錯誤: rendezvous known_hosts 檔案不存在" >&2
    exit 2
fi

# -----------------------------------------------------------------------------
# 環境配置 (支援測試覆寫)
# -----------------------------------------------------------------------------
USER_NAME="${LGW_BOOT_USER:-llmgw}"
RUNDIR="${LGW_BOOT_RUNDIR:-/run/user}"
HOSTKEY_FILE="${LGW_BOOT_HOSTKEY:-/etc/ssh/ssh_host_ed25519_key.pub}"
REPO_URL="${LGW_BOOT_REPO:-https://github.com/FATESAIKOU/LLMGateway.git}"

resolve_home() {
    if [[ -n "${LGW_BOOT_HOME:-}" ]]; then
        printf '%s' "$LGW_BOOT_HOME"
    else
        local ent
        ent="$(getent passwd "$USER_NAME" 2>/dev/null || true)"
        if [[ -n "$ent" ]]; then
            cut -d: -f6 <<< "$ent"
        else
            printf '/home/%s' "$USER_NAME"
        fi
    fi
}

BOOT_HOME="$(resolve_home)"

# -----------------------------------------------------------------------------
# sudo 封裝
# -----------------------------------------------------------------------------
sudo_refresh() {
    if [[ $CHECK_ONLY -eq 1 ]]; then
        return 0
    fi
    if [[ -n "$SUDO_PASS_FILE" ]]; then
        sudo -S -p '' -v < "$SUDO_PASS_FILE"
    else
        sudo -v
    fi
}

run_root() {
    sudo -n "$@" </dev/null
}

run_as_llmgw() {
    local uid
    uid="$(id -u "$USER_NAME" 2>/dev/null || echo "1000")"
    sudo -n -u "$USER_NAME" env HOME="$BOOT_HOME" XDG_RUNTIME_DIR="${RUNDIR}/${uid}" "$@" </dev/null
}

# -----------------------------------------------------------------------------
# 步驟日誌與狀態追蹤
# -----------------------------------------------------------------------------
TODOS=()
step_failed=0
changed_count=0

log_step() {
    local step="$1" status="$2"
    shift 2
    printf '[%s] %s %s\n' "$step" "$status" "$*"
}

record_todo() {
    TODOS+=("$*")
}

# -----------------------------------------------------------------------------
# 步驟 1: 前提
# -----------------------------------------------------------------------------
step_prereq() {
    local os
    os="${LGW_BOOT_OS:-$(uname -s)}"
    if [[ "$os" != "Linux" ]]; then
        log_step prereq fail "作業系統需為 Linux (目前為 ${os})"
        step_failed=1
        return 1
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        log_step prereq fail "未找到 systemctl (需要 systemd)"
        step_failed=1
        return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        log_step prereq fail "未找到 python3"
        step_failed=1
        return 1
    fi

    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        log_step prereq fail "python3 版本需 >= 3.10"
        step_failed=1
        return 1
    fi

    local tool
    for tool in git curl ssh ssh-keygen; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            log_step prereq fail "未找到必要工具: ${tool}"
            step_failed=1
            return 1
        fi
    done

    log_step prereq ok "Linux, systemd, python3 >= 3.10, git, curl, ssh, ssh-keygen"
}

# -----------------------------------------------------------------------------
# 步驟 2: 帳號 llmgw
# -----------------------------------------------------------------------------
step_user() {
    if ! id -u "$USER_NAME" >/dev/null 2>&1; then
        if [[ $CHECK_ONLY -eq 1 ]]; then
            log_step user todo "帳號 ${USER_NAME} 不存在，需執行 useradd -m -s /bin/bash ${USER_NAME}"
            record_todo "帳號 ${USER_NAME} 不存在，需執行 useradd -m -s /bin/bash ${USER_NAME}"
            return 0
        fi
        sudo_refresh
        run_root useradd -m -s /bin/bash "$USER_NAME"
        BOOT_HOME="$(resolve_home)"
        log_step user changed "建立帳號 ${USER_NAME}"
        changed_count=$((changed_count + 1))
        return 0
    fi

    BOOT_HOME="$(resolve_home)"

    local in_docker=0
    for g in $(id -Gn "$USER_NAME" 2>/dev/null); do
        if [[ "$g" == "docker" ]]; then
            in_docker=1
            break
        fi
    done

    if [[ $in_docker -eq 1 ]]; then
        if [[ $CHECK_ONLY -eq 1 ]]; then
            log_step user todo "帳號 ${USER_NAME} 在 docker 群組中，需移出"
            record_todo "帳號 ${USER_NAME} 在 docker 群組中，需執行 gpasswd -d ${USER_NAME} docker"
            return 0
        fi
        sudo_refresh
        run_root gpasswd -d "$USER_NAME" docker
        log_step user changed "將帳號 ${USER_NAME} 自 docker 群組移出"
        changed_count=$((changed_count + 1))
    else
        log_step user ok "帳號 ${USER_NAME} 已存在且不在 docker 群組"
    fi
}

# -----------------------------------------------------------------------------
# 步驟 3: linger
# -----------------------------------------------------------------------------
step_linger() {
    local linger_val
    linger_val="$(loginctl show-user "$USER_NAME" -p Linger 2>/dev/null || true)"
    if [[ "$linger_val" =~ Linger=yes || "$linger_val" == "yes" ]]; then
        log_step linger ok "帳號 ${USER_NAME} 已開啟 linger"
        return 0
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        log_step linger todo "帳號 ${USER_NAME} 尚未開啟 linger"
        record_todo "帳號 ${USER_NAME} 需開啟 linger (loginctl enable-linger ${USER_NAME})"
        return 0
    fi

    sudo_refresh
    run_root loginctl enable-linger "$USER_NAME"

    local uid
    uid="$(id -u "$USER_NAME" 2>/dev/null || echo "1000")"
    local user_rundir="${RUNDIR}/${uid}"
    local waited=0
    while [[ $waited -lt 15 ]]; do
        if [[ -d "$user_rundir" ]]; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if [[ ! -d "$user_rundir" ]]; then
        log_step linger fail "等候 ${user_rundir} 出現逾時 (15 秒)"
        step_failed=1
        return 1
    fi

    log_step linger changed "已為帳號 ${USER_NAME} 開啟 linger"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 4: sshd
# -----------------------------------------------------------------------------
step_sshd() {
    local reasons=()
    local sshd_out=""
    sshd_out="$(sshd -T -C "user=${USER_NAME},host=localhost,addr=127.0.0.1" 2>/dev/null || true)"

    local pubkey_auth=""
    pubkey_auth="$(grep -Ei '^pubkeyauthentication[[:space:]]+' <<< "$sshd_out" | awk '{print tolower($2)}' || true)"
    if [[ "$pubkey_auth" != "yes" ]]; then
        reasons+=("sshd 未啟用 PubkeyAuthentication (需於 sshd_config 設定 PubkeyAuthentication yes)")
    fi

    local deny_users=""
    deny_users="$(grep -Ei '^denyusers[[:space:]]+' <<< "$sshd_out" | cut -d' ' -f2- || true)"
    if [[ -n "$deny_users" ]]; then
        for u in $deny_users; do
            if [[ "$u" == "$USER_NAME" ]]; then
                reasons+=("sshd DenyUsers 阻擋了 ${USER_NAME} (需自 DenyUsers 移除)")
                break
            fi
        done
    fi

    local allow_users=""
    allow_users="$(grep -Ei '^allowusers[[:space:]]+' <<< "$sshd_out" | cut -d' ' -f2- || true)"
    if [[ -n "$allow_users" ]]; then
        local allowed=0
        for u in $allow_users; do
            if [[ "$u" == "$USER_NAME" ]]; then
                allowed=1
                break
            fi
        done
        if [[ $allowed -eq 0 ]]; then
            reasons+=("sshd AllowUsers 未包含 ${USER_NAME} (需將 ${USER_NAME} 加入 AllowUsers)")
        fi
    fi

    local ss_out=""
    ss_out="$(ss -ltn 2>/dev/null || true)"
    if ! grep -E '(127\.0\.0\.1:22|0\.0\.0\.0:22|\*:22|\[::\]:22|:::22)([[:space:]]|$)' <<< "$ss_out" >/dev/null 2>&1; then
        reasons+=("sshd 未在 127.0.0.1:22 監聽 (需於 sshd_config 加入 ListenAddress 127.0.0.1:22 並重啟 sshd)")
    fi

    if [[ ${#reasons[@]} -eq 0 ]]; then
        log_step sshd ok "sshd 支援公鑰登入、未封鎖 ${USER_NAME} 且在 127.0.0.1:22 監聽"
    else
        local reason_str
        reason_str="$(IFS='; '; echo "${reasons[*]}")"
        log_step sshd todo "$reason_str"
        for r in "${reasons[@]}"; do
            record_todo "$r"
        done
    fi
}

# -----------------------------------------------------------------------------
# 步驟 5: 第一次安裝
# -----------------------------------------------------------------------------
step_install() {
    local share_dir="${BOOT_HOME}/.local/share/llmgateway"
    local current_link="${share_dir}/current"
    local tag_file="${current_link}/.lgw-tag"
    local installed_tag=""
    if [[ -f "$tag_file" ]]; then
        installed_tag="$(cat "$tag_file" 2>/dev/null || true)"
    fi

    if [[ -n "$TAG" && "$installed_tag" == "$TAG" && $REINSTALL -eq 0 ]]; then
        log_step install ok "程式版本已是 ${TAG}"
        return 0
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        log_step install todo "尚未安裝指定版本 ${TAG}"
        record_todo "需安裝版本 ${TAG}"
        return 0
    fi

    sudo_refresh
    run_root install -d -m 755 -o "$USER_NAME" "$share_dir"

    local bootstrap_clone="${share_dir}/.bootstrap-${TAG}"
    run_as_llmgw rm -rf "$bootstrap_clone"
    run_as_llmgw git clone --depth 1 --branch "$TAG" "$REPO_URL" "$bootstrap_clone"
    run_as_llmgw bash "${bootstrap_clone}/deploy/install.sh" --tag "$TAG" --home "$BOOT_HOME"
    run_as_llmgw rm -rf "$bootstrap_clone"

    log_step install changed "已安裝版本 ${TAG}"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 6: 隧道金鑰
# -----------------------------------------------------------------------------
step_tunnel_key() {
    local key_file="${BOOT_HOME}/.ssh/id_llmgw_tunnel"
    if [[ -f "$key_file" ]]; then
        log_step tunnel-key ok "隧道金鑰已存在 (${key_file})"
        return 0
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        log_step tunnel-key todo "隧道金鑰尚未產生"
        record_todo "需為 ${USER_NAME} 產生隧道金鑰 (${key_file})"
        return 0
    fi

    sudo_refresh
    run_root install -d -m 700 -o "$USER_NAME" "${BOOT_HOME}/.ssh"
    run_as_llmgw ssh-keygen -t ed25519 -N '' -C llmgateway-tunnel -f "$key_file"
    log_step tunnel-key changed "已產生隧道金鑰 (${key_file})"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 7: 部署公鑰
# -----------------------------------------------------------------------------
step_deploy_key() {
    if [[ -z "$DEPLOY_PUBKEY_FILE" ]]; then
        log_step deploy-key todo "未提供部署公鑰 (--deploy-pubkey-file)"
        record_todo "未提供部署公鑰 (--deploy-pubkey-file)"
        return 0
    fi

    local lines_in_file
    lines_in_file="$(grep -c '[^[:space:]]' "$DEPLOY_PUBKEY_FILE" || true)"
    if [[ "$lines_in_file" -ne 1 ]] || ! ssh-keygen -l -f "$DEPLOY_PUBKEY_FILE" >/dev/null 2>&1; then
        log_step deploy-key fail "部署公鑰檔案格式無效或非單行公鑰 (${DEPLOY_PUBKEY_FILE})"
        step_failed=1
        return 1
    fi

    local pubkey
    pubkey="$(grep '[^[:space:]]' "$DEPLOY_PUBKEY_FILE" | head -n 1)"
    local pubkey_b64
    pubkey_b64="$(awk '{print $2}' <<< "$pubkey")"
    if [[ -z "$pubkey_b64" ]]; then
        log_step deploy-key fail "無法解析部署公鑰 base64 內容"
        step_failed=1
        return 1
    fi

    local remote_deploy="${BOOT_HOME}/.local/share/llmgateway/current/deploy/remote-deploy.sh"
    local target_line="command=\"${remote_deploy}\",restrict ${pubkey}"
    local auth_keys="${BOOT_HOME}/.ssh/authorized_keys"

    local needs_update=1
    if [[ -f "$auth_keys" ]]; then
        local match_count
        match_count="$(grep -c -F "$pubkey_b64" "$auth_keys" || true)"
        if [[ $match_count -eq 1 ]]; then
            local matching_line
            matching_line="$(grep -F "$pubkey_b64" "$auth_keys" || true)"
            if [[ "$matching_line" == "$target_line" ]]; then
                needs_update=0
            fi
        fi
    fi

    if [[ $needs_update -eq 0 ]]; then
        log_step deploy-key ok "部署公鑰已就位"
        return 0
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        log_step deploy-key todo "部署公鑰待更新至 authorized_keys"
        record_todo "部署公鑰待更新至 ${auth_keys}"
        return 0
    fi

    sudo_refresh
    run_root install -d -m 700 -o "$USER_NAME" "${BOOT_HOME}/.ssh"

    local tmp_auth
    tmp_auth="$(mktemp "${BOOT_HOME}/.ssh/.auth.XXXXXX" 2>/dev/null || mktemp)"
    if [[ -f "$auth_keys" ]]; then
        grep -v -F "$pubkey_b64" "$auth_keys" > "$tmp_auth" || true
    else
        : > "$tmp_auth"
    fi
    printf '%s\n' "$target_line" >> "$tmp_auth"

    run_root install -m 600 -o "$USER_NAME" "$tmp_auth" "$auth_keys"
    rm -f "$tmp_auth"

    log_step deploy-key changed "已更新部署公鑰至 authorized_keys"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 8: 會合點設定
# -----------------------------------------------------------------------------
step_rendezvous() {
    if [[ -z "$RENDEZVOUS" ]]; then
        log_step rendezvous todo "未提供會合點設定 (--rendezvous 與 --rendezvous-known-hosts)"
        record_todo "未提供會合點設定 (--rendezvous 與 --rendezvous-known-hosts)"
        return 0
    fi

    local target_kh="${BOOT_HOME}/.ssh/known_hosts_rendezvous"
    local conf_dir="${BOOT_HOME}/.config/llmgateway"
    local target_env="${conf_dir}/tunnel.env"
    local target_id="${BOOT_HOME}/.ssh/id_llmgw_tunnel"

    local kh_differs=1
    if [[ -f "$target_kh" ]] && cmp -s "$RENDEZVOUS_KNOWN_HOSTS" "$target_kh"; then
        kh_differs=0
    fi

    local tmp_env
    tmp_env="$(mktemp "${conf_dir}/.env.XXXXXX" 2>/dev/null || mktemp)"

    python3 - "$target_env" "$RENDEZVOUS" "$target_id" "$target_kh" > "$tmp_env" << 'EOF'
import sys

path, rendezvous, identity, known_hosts = sys.argv[1:5]
lines = []
keys_seen = set()

try:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                k = stripped.split("=", 1)[0].strip()
                keys_seen.add(k)
                if k == "RENDEZVOUS":
                    lines.append(f"RENDEZVOUS={rendezvous}\n")
                elif k == "IDENTITY":
                    lines.append(f"IDENTITY={identity}\n")
                elif k == "KNOWN_HOSTS":
                    lines.append(f"KNOWN_HOSTS={known_hosts}\n")
                else:
                    lines.append(line)
            else:
                lines.append(line)
except FileNotFoundError:
    pass

if "RENDEZVOUS" not in keys_seen:
    lines.append(f"RENDEZVOUS={rendezvous}\n")
if "IDENTITY" not in keys_seen:
    lines.append(f"IDENTITY={identity}\n")
if "KNOWN_HOSTS" not in keys_seen:
    lines.append(f"KNOWN_HOSTS={known_hosts}\n")

defaults = [
    ("REMOTE_API", "4710"),
    ("REMOTE_SSH", "4711"),
    ("LOCAL_API", "8480"),
    ("PROBE_INTERVAL", "60"),
    ("PROBE_FAILS", "3"),
]
for k, v in defaults:
    if k not in keys_seen:
        lines.append(f"{k}={v}\n")

sys.stdout.write("".join(lines))
EOF

    local env_differs=1
    if [[ -f "$target_env" ]] && cmp -s "$tmp_env" "$target_env"; then
        env_differs=0
    fi

    if [[ $kh_differs -eq 0 && $env_differs -eq 0 ]]; then
        rm -f "$tmp_env"
        log_step rendezvous ok "會合點設定與 tunnel.env 已是最新"
        return 0
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        rm -f "$tmp_env"
        log_step rendezvous todo "會合點設定待更新"
        record_todo "會合點設定待更新至 tunnel.env"
        return 0
    fi

    sudo_refresh
    run_root install -d -m 700 -o "$USER_NAME" "${BOOT_HOME}/.ssh"
    run_root install -m 644 -o "$USER_NAME" "$RENDEZVOUS_KNOWN_HOSTS" "$target_kh"
    run_root install -d -m 755 -o "$USER_NAME" "$conf_dir"
    run_root install -m 600 -o "$USER_NAME" "$tmp_env" "$target_env"
    rm -f "$tmp_env"

    run_as_llmgw systemctl --user restart llmgateway-tunnel.service

    log_step rendezvous changed "已更新會合點設定並重啟 llmgateway-tunnel.service"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 9: 檢查 (install.sh --check)
# -----------------------------------------------------------------------------
step_check() {
    local install_script="${BOOT_HOME}/.local/share/llmgateway/current/deploy/install.sh"
    if [[ ! -f "$install_script" ]]; then
        if [[ $CHECK_ONLY -eq 1 ]]; then
            log_step check todo "current 尚未安裝，無法執行 install.sh --check"
            record_todo "current 尚未安裝，無法執行 install.sh --check"
            return 0
        fi
        log_step check fail "找不到 ${install_script}"
        step_failed=1
        return 1
    fi

    sudo_refresh
    local chk_cmd=(bash "$install_script" --check --home "$BOOT_HOME")
    if [[ -n "$TAG" ]]; then
        chk_cmd+=(--tag "$TAG")
    fi

    set +e
    run_as_llmgw "${chk_cmd[@]}"
    local rc=$?
    set -e

    if [[ $rc -eq 0 ]]; then
        log_step check ok "install.sh --check 通過 (rc=0)"
    else
        log_step check fail "install.sh --check 未通過 (rc=${rc})"
        step_failed=1
    fi
}

# -----------------------------------------------------------------------------
# 結尾摘要
# -----------------------------------------------------------------------------
print_summary() {
    local tunnel_pub="${BOOT_HOME}/.ssh/id_llmgw_tunnel.pub"

    echo ""
    echo "======================================================================"
    echo "開機設定摘要 (Bootstrap Summary)"
    echo "======================================================================"
    echo "1. 隧道公鑰 (給 MyLinuxPool TUNNEL_EXTRA_PUBKEYS):"
    if [[ -f "$tunnel_pub" ]]; then
        cat "$tunnel_pub"
    else
        echo "   (尚未產生)"
    fi
    echo ""
    echo "2. 這台的 ed25519 host 公鑰 (給 GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS):"
    if [[ -f "$HOSTKEY_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ -z "$line" || "$line" =~ ^# ]] && continue
            printf '[127.0.0.1]:4711 %s\n' "$line"
        done < "$HOSTKEY_FILE"
    else
        echo "   (未找到 host key: ${HOSTKEY_FILE})"
    fi
    echo ""
    echo "3. 待辦事項 (Pending Todos):"
    if [[ ${#TODOS[@]} -gt 0 ]]; then
        for t in "${TODOS[@]}"; do
            printf '   - %s\n' "$t"
        done
    else
        echo "   無 (全部完成)"
    fi
    echo "======================================================================"
}

# -----------------------------------------------------------------------------
# 主程序
# -----------------------------------------------------------------------------
step_prereq || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_user || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_linger || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_sshd || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_install || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_tunnel_key || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_deploy_key || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_rendezvous || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

step_check || true
if [[ $step_failed -ne 0 ]]; then
    exit 1
fi

print_summary

if [[ $step_failed -ne 0 ]]; then
    exit 1
elif [[ ${#TODOS[@]} -gt 0 ]]; then
    exit 3
else
    exit 0
fi
