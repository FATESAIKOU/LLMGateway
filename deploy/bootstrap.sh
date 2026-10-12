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

# --rendezvous 驗證 user@host:port 格式（中 M4）
if [[ -n "$RENDEZVOUS" ]]; then
    if ! [[ "$RENDEZVOUS" =~ ^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+:[1-9][0-9]*$ ]]; then
        echo "錯誤: --rendezvous 格式無效，需為 user@host:port（埠 1-65535）: ${RENDEZVOUS}" >&2
        exit 2
    fi
    rendezvous_port="${RENDEZVOUS##*:}"
    if [[ -z "$rendezvous_port" ]] || [[ "$rendezvous_port" -lt 1 ]] || [[ "$rendezvous_port" -gt 65535 ]] 2>/dev/null; then
        echo "錯誤: --rendezvous 埠號無效（需 1-65535）: ${RENDEZVOUS}" >&2
        exit 2
    fi
fi

# --rendezvous-known-hosts 用 ssh-keygen -l -f 驗證（中 M4）
if [[ -n "$RENDEZVOUS_KNOWN_HOSTS" ]]; then
    if ! ssh-keygen -l -f "$RENDEZVOUS_KNOWN_HOSTS" >/dev/null 2>&1; then
        echo "錯誤: --rendezvous-known-hosts 檔案無效（無法被 ssh-keygen -l -f 讀取）: ${RENDEZVOUS_KNOWN_HOSTS}" >&2
        exit 2
    fi
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
    if [[ -n "$SUDO_PASS_FILE" ]]; then
        sudo -S -p '' -v < "$SUDO_PASS_FILE" || {
            log_step prereq fail "sudo 密碼驗證失敗"
            step_failed=1
            return 1
        }
    else
        sudo -v || {
            log_step prereq fail "sudo -v 失敗"
            step_failed=1
            return 1
        }
    fi
}

run_root() {
    sudo -n "$@" </dev/null
}

run_as_llmgw() {
    local uid
    uid="$(id -u "$USER_NAME" 2>/dev/null || echo "1000")"
    # 先切到 llmgw 可讀的目錄，避免繼承管理帳號 cwd 導致 git clone 失敗 (H6)
    sudo -n -u "$USER_NAME" env HOME="$BOOT_HOME" XDG_RUNTIME_DIR="${RUNDIR}/${uid}" bash -c 'cd "${BOOT_HOME:-/home/llmgw}" || cd /; exec "$@"' bash "$@" </dev/null
}

# llmgw_exists：以 llmgw 身分判斷路徑狀態，並把「不存在」與「讀不到」分開
#   回 0：存在
#   回 1：不存在
#   回 2：無法判斷（llmgw 進不了父目錄，例如權限或家目錄不在預期位置）
# 管理帳號不得直接 [[ -f ]] 讀 ~llmgw 底下的檔案：家目錄 0700/0750 時會讀不到
# (F2)；一律經這裡判斷，讀不到時呼叫者要記 fail，不要當成不存在而覆寫。
llmgw_exists() {
    local path="$1" dir
    dir="$(dirname "$path")"
    # 進得去父目錄 → test -e 的結果可信
    if run_as_llmgw test -x "$dir"; then
        if run_as_llmgw test -e "$path"; then
            return 0
        fi
        return 1
    fi
    # 進不去：分「父目錄不存在」與「父目錄存在但讀不到」
    if ! run_as_llmgw test -d "$dir"; then
        return 1
    fi
    return 2
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
        if ! run_root useradd -m -s /bin/bash "$USER_NAME"; then
            log_step user fail "useradd -m -s /bin/bash ${USER_NAME} 失敗"
            step_failed=1
            return 1
        fi
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
        if ! run_root gpasswd -d "$USER_NAME" docker; then
            log_step user fail "gpasswd -d ${USER_NAME} docker 失敗"
            step_failed=1
            return 1
        fi
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
    if ! run_root loginctl enable-linger "$USER_NAME"; then
        log_step linger fail "loginctl enable-linger ${USER_NAME} 失敗"
        step_failed=1
        return 1
    fi

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
    sshd_out="$(run_root sshd -T -C "user=${USER_NAME},host=localhost,addr=127.0.0.1" 2>/dev/null || true)"

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

    # AllowGroups / DenyGroups 檢查（中等 M3）
    local allow_groups deny_groups
    allow_groups="$(grep -Ei '^allowgroups[[:space:]]+' <<< "$sshd_out" | cut -d' ' -f2- || true)"
    deny_groups="$(grep -Ei '^denygroups[[:space:]]+' <<< "$sshd_out" | cut -d' ' -f2- || true)"
    if [[ -n "$deny_groups" ]]; then
        for g in $deny_groups; do
            # 簡單明確比對；若含萬用字元或複雜格式，印 todo 請人工確認
            case "$g" in
                *\** | *\?* | *@*)
                    reasons+=("sshd DenyGroups 設定複雜（${g}），請人工確認是否封鎖 ${USER_NAME}")
                    ;;
                *)
                    local user_groups
                    user_groups="$(id -Gn "$USER_NAME" 2>/dev/null || true)"
                    if printf '%s\n' "$user_groups" | grep -qwF "$g"; then
                        reasons+=("sshd DenyGroups 包含 ${g}，可能封鎖 ${USER_NAME}（需人工確認）")
                    fi
                    ;;
            esac
        done
    fi
    if [[ -n "$allow_groups" ]]; then
        local allowed_group=0
        for g in $allow_groups; do
            # 簡單明確比對；若含萬用字元或複雜格式，印 todo
            case "$g" in
                *\** | *\?* | *@*)
                    reasons+=("sshd AllowGroups 設定複雜（${g}），請人工確認是否允許 ${USER_NAME}")
                    allowed_group=1
                    ;;
                *)
                    local user_groups
                    user_groups="$(id -Gn "$USER_NAME" 2>/dev/null || true)"
                    if printf '%s\n' "$user_groups" | grep -qwF "$g"; then
                        allowed_group=1
                    fi
                    ;;
            esac
        done
        # 只要 AllowGroups 有設但沒明確允許 llmgw，就印 todo（不自己解析萬用字元與 user@host）
        if [[ $allowed_group -eq 0 ]]; then
            reasons+=("sshd AllowGroups 未明確允許 ${USER_NAME}（請人工確認 AllowGroups 設定）")
        fi
    fi

    local allow_users=""
    allow_users="$(grep -Ei '^allowusers[[:space:]]+' <<< "$sshd_out" | cut -d' ' -f2- || true)"
    if [[ -n "$allow_users" ]]; then
        local allowed=0
        for u in $allow_users; do
            # 處理 user@host 形式（取 @ 前面的 user 部分）
            local u_simple="${u%%@*}"
            case "$u_simple" in
                *\** | *\?*)
                    # 萬用字元：不自己解析，印 todo 請人工確認
                    reasons+=("sshd AllowUsers 包含萬用字元或 user@host 格式（${u}），請人工確認是否允許 ${USER_NAME}")
                    allowed=1
                    break
                    ;;
                *)
                    if [[ "$u_simple" == "$USER_NAME" ]]; then
                        allowed=1
                    fi
                    ;;
            esac
        done
        if [[ $allowed -eq 0 ]]; then
            reasons+=("sshd AllowUsers 未包含 ${USER_NAME}（需將 ${USER_NAME} 加入 AllowUsers，或人工確認萬用字元設定）")
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
    local tag_state=0
    llmgw_exists "$tag_file" || tag_state=$?
    if [[ $tag_state -eq 0 ]]; then
        if ! installed_tag="$(run_as_llmgw cat "$tag_file")"; then
            log_step install fail "無法讀取已安裝版本檔 (${tag_file})"
            step_failed=1
            return 1
        fi
    elif [[ $tag_state -ne 1 ]]; then
        log_step install fail "無法判斷已安裝版本檔是否存在 (${tag_file})"
        step_failed=1
        return 1
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

    local grp=""
    grp="$(id -gn "$USER_NAME" 2>/dev/null || true)"
    sudo_refresh
    if [[ -n "$grp" ]]; then
        if ! run_root install -d -m 755 -o "$USER_NAME" -g "$grp" "$share_dir"; then
            log_step install fail "install -d ... ${share_dir} 失敗"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -d -m 755 -o "$USER_NAME" "$share_dir"; then
            log_step install fail "install -d ... ${share_dir} 失敗"
            step_failed=1
            return 1
        fi
    fi

    local bootstrap_clone="${share_dir}/.bootstrap-${TAG}"
    if ! run_as_llmgw rm -rf "$bootstrap_clone"; then
        log_step install fail "rm -rf ${bootstrap_clone} 失敗"
        step_failed=1
        return 1
    fi
    if ! run_as_llmgw git clone --depth 1 --branch "$TAG" "$REPO_URL" "$bootstrap_clone"; then
        log_step install fail "git clone ... 失敗"
        step_failed=1
        return 1
    fi
    if ! run_as_llmgw bash "${bootstrap_clone}/deploy/install.sh" --tag "$TAG" --home "$BOOT_HOME"; then
        log_step install fail "bash install.sh ... 失敗"
        step_failed=1
        return 1
    fi
    if ! run_as_llmgw rm -rf "$bootstrap_clone"; then
        log_step install fail "rm -rf ${bootstrap_clone} (清理) 失敗"
        step_failed=1
        return 1
    fi

    log_step install changed "已安裝版本 ${TAG}"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 6: 隧道金鑰
# -----------------------------------------------------------------------------
step_tunnel_key() {
    local key_file="${BOOT_HOME}/.ssh/id_llmgw_tunnel"
    local key_state=0
    llmgw_exists "$key_file" || key_state=$?
    if [[ $key_state -eq 0 ]]; then
        log_step tunnel-key ok "隧道金鑰已存在 (${key_file})"
        return 0
    elif [[ $key_state -ne 1 ]]; then
        # 無法判斷是否存在就不去產生：ssh-keygen 對既有檔會問覆寫，
        # stdin 是 /dev/null 直接讀到 EOF 放棄，第二次執行必紅 (F2)
        log_step tunnel-key fail "無法判斷隧道金鑰是否存在 (${key_file})，不重新產生"
        step_failed=1
        return 1
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        log_step tunnel-key todo "隧道金鑰尚未產生"
        record_todo "需為 ${USER_NAME} 產生隧道金鑰 (${key_file})"
        return 0
    fi

    local grp=""
    grp="$(id -gn "$USER_NAME" 2>/dev/null || true)"
    sudo_refresh
    if [[ -n "$grp" ]]; then
        if ! run_root install -d -m 700 -o "$USER_NAME" -g "$grp" "${BOOT_HOME}/.ssh"; then
            log_step tunnel-key fail "install -d ... .ssh 失敗"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -d -m 700 -o "$USER_NAME" "${BOOT_HOME}/.ssh"; then
            log_step tunnel-key fail "install -d ... .ssh 失敗"
            step_failed=1
            return 1
        fi
    fi
    if ! run_as_llmgw ssh-keygen -t ed25519 -N '' -C llmgateway-tunnel -f "$key_file"; then
        log_step tunnel-key fail "ssh-keygen ... 失敗"
        step_failed=1
        return 1
    fi
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
    local auth_content=""
    local auth_state=0
    llmgw_exists "$auth_keys" || auth_state=$?
    case "$auth_state" in
        0)
            if ! auth_content="$(run_as_llmgw cat "$auth_keys")"; then
                log_step deploy-key fail "無法讀取 authorized_keys (${auth_keys})"
                step_failed=1
                return 1
            fi
            local match_count
            match_count="$(printf '%s\n' "$auth_content" | grep -c -F "$pubkey_b64" || true)"
            if [[ "$match_count" -eq 1 ]]; then
                local matching_line
                matching_line="$(printf '%s\n' "$auth_content" | grep -F "$pubkey_b64" || true)"
                if [[ "$matching_line" == "$target_line" ]]; then
                    needs_update=0
                fi
            fi
            ;;
        1)
            :  # 不存在 → 直接寫入
            ;;
        *)
            log_step deploy-key fail "無法判斷 authorized_keys 是否存在 (${auth_keys})，不覆寫"
            step_failed=1
            return 1
            ;;
    esac

    if [[ $needs_update -eq 0 ]]; then
        log_step deploy-key ok "部署公鑰已就位"
        return 0
    fi

    if [[ $CHECK_ONLY -eq 1 ]]; then
        log_step deploy-key todo "部署公鑰待更新至 authorized_keys"
        record_todo "部署公鑰待更新至 ${auth_keys}"
        return 0
    fi

    local grp=""
    grp="$(id -gn "$USER_NAME" 2>/dev/null || true)"
    sudo_refresh
    if [[ -n "$grp" ]]; then
        if ! run_root install -d -m 700 -o "$USER_NAME" -g "$grp" "${BOOT_HOME}/.ssh"; then
            log_step deploy-key fail "install -d ... .ssh 失敗"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -d -m 700 -o "$USER_NAME" "${BOOT_HOME}/.ssh"; then
            log_step deploy-key fail "install -d ... .ssh 失敗"
            step_failed=1
            return 1
        fi
    fi

    local tmp_auth
    tmp_auth="$(mktemp "${BOOT_HOME}/.ssh/.auth.XXXXXX" 2>/dev/null || mktemp)"
    # 只在能讀到原檔時才保留其他行；讀不到原檔但檔案存在則 fail（絕不覆蓋空內容）
    if [[ -n "${auth_content:-}" ]]; then
        printf '%s\n' "$auth_content" | grep -v -F "$pubkey_b64" > "$tmp_auth" || true
    elif [[ $auth_state -eq 0 ]]; then
        # 檔案存在但未取得內容（理論上已在上面 fail），保險再 fail
        log_step deploy-key fail "無法讀取 authorized_keys 原內容，拒絕覆寫空內容"
        rm -f "$tmp_auth"
        step_failed=1
        return 1
    fi
    printf '%s\n' "$target_line" >> "$tmp_auth"

    if ! run_root install -m 600 -o "$USER_NAME" -g "$(id -gn "$USER_NAME" 2>/dev/null || echo "$USER_NAME")" "$tmp_auth" "$auth_keys"; then
        log_step deploy-key fail "install -m 600 ... authorized_keys 失敗"
        rm -f "$tmp_auth"
        step_failed=1
        return 1
    fi
    rm -f "$tmp_auth"

    log_step deploy-key changed "已更新部署公鑰至 authorized_keys"
    changed_count=$((changed_count + 1))
}

# -----------------------------------------------------------------------------
# 步驟 8: 會合點設定
# -----------------------------------------------------------------------------
# 產生 tunnel.env：既有內容由 stdin 讀入（呼叫端以 llmgw 身分讀好），
# 保留註解與未列出的鍵，只換 RENDEZVOUS／IDENTITY／KNOWN_HOSTS。
gen_env() {
python3 - "$RENDEZVOUS" "$target_id" "$target_kh" << 'PYEOF'
import sys

rendezvous, identity, known_hosts = sys.argv[1:4]
lines = []
keys_seen = set()

try:
    raw = sys.stdin.buffer.read().decode("utf-8")
except (OSError, UnicodeDecodeError) as exc:
    # 讀既有內容出錯就退出，不要 traceback 讓整支腳本當場死掉
    print("cannot read existing tunnel.env: %s" % type(exc).__name__, file=sys.stderr)
    sys.exit(3)

for line in raw.splitlines(keepends=True):
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
PYEOF
}

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

    # 兩份既有檔案都以 llmgw 身分讀，並把「不存在」與「讀不到」分開 (F2)：
    # 讀不到就 fail，不拿空內容覆寫。
    local kh_state=0 kh_existing=""
    llmgw_exists "$target_kh" || kh_state=$?
    if [[ $kh_state -eq 0 ]]; then
        if ! kh_existing="$(run_as_llmgw cat "$target_kh")"; then
            log_step rendezvous fail "無法讀取 known_hosts (${target_kh})"
            step_failed=1
            return 1
        fi
    elif [[ $kh_state -ne 1 ]]; then
        log_step rendezvous fail "無法判斷 known_hosts 是否存在 (${target_kh})"
        step_failed=1
        return 1
    fi

    local env_state=0 env_existing=""
    llmgw_exists "$target_env" || env_state=$?
    if [[ $env_state -eq 0 ]]; then
        if ! env_existing="$(run_as_llmgw cat "$target_env")"; then
            log_step rendezvous fail "無法讀取 tunnel.env (${target_env})"
            step_failed=1
            return 1
        fi
    elif [[ $env_state -ne 1 ]]; then
        log_step rendezvous fail "無法判斷 tunnel.env 是否存在 (${target_env})"
        step_failed=1
        return 1
    fi

    local kh_differs=1
    if [[ $kh_state -eq 0 && "$(cat "$RENDEZVOUS_KNOWN_HOSTS")" == "$kh_existing" ]]; then
        kh_differs=0
    fi

    local tmp_env
    tmp_env="$(mktemp "${conf_dir}/.env.XXXXXX" 2>/dev/null || mktemp)"

    # 既有內容從 stdin 餵進去，python 不直接開檔（管理帳號可能讀不到）
    if ! printf '%s' "$env_existing" | gen_env > "$tmp_env"; then
        log_step rendezvous fail "產生 tunnel.env 失敗"
        rm -f "$tmp_env"
        step_failed=1
        return 1
    fi

    local env_differs=1
    if [[ $env_state -eq 0 && "$(cat "$tmp_env")" == "$env_existing" ]]; then
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
    local grp=""
    grp="$(id -gn "$USER_NAME" 2>/dev/null || true)"
    if [[ -n "$grp" ]]; then
        if ! run_root install -d -m 700 -o "$USER_NAME" -g "$grp" "${BOOT_HOME}/.ssh"; then
            log_step rendezvous fail "install -d ... .ssh 失敗"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -d -m 700 -o "$USER_NAME" "${BOOT_HOME}/.ssh"; then
            log_step rendezvous fail "install -d ... .ssh 失敗"
            step_failed=1
            return 1
        fi
    fi
    if [[ -n "$grp" ]]; then
        if ! run_root install -m 644 -o "$USER_NAME" -g "$grp" "$RENDEZVOUS_KNOWN_HOSTS" "$target_kh"; then
            log_step rendezvous fail "install -m 644 ... known_hosts 失敗"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -m 644 -o "$USER_NAME" "$RENDEZVOUS_KNOWN_HOSTS" "$target_kh"; then
            log_step rendezvous fail "install -m 644 ... known_hosts 失敗"
            step_failed=1
            return 1
        fi
    fi
    local grp_dir=""
    grp_dir="$(id -gn "$USER_NAME" 2>/dev/null || true)"
    if [[ -n "$grp_dir" ]]; then
        if ! run_root install -d -m 755 -o "$USER_NAME" -g "$grp_dir" "$conf_dir"; then
            log_step rendezvous fail "install -d ... conf_dir 失敗"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -d -m 755 -o "$USER_NAME" "$conf_dir"; then
            log_step rendezvous fail "install -d ... conf_dir 失敗"
            step_failed=1
            return 1
        fi
    fi
    if [[ -n "${grp_dir:-}" ]]; then
        if ! run_root install -m 600 -o "$USER_NAME" -g "$grp_dir" "$tmp_env" "$target_env"; then
            log_step rendezvous fail "install -m 600 ... tunnel.env 失敗"
            rm -f "$tmp_env"
            step_failed=1
            return 1
        fi
    else
        if ! run_root install -m 600 -o "$USER_NAME" "$tmp_env" "$target_env"; then
            log_step rendezvous fail "install -m 600 ... tunnel.env 失敗"
            rm -f "$tmp_env"
            step_failed=1
            return 1
        fi
    fi
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
    local script_state=0
    llmgw_exists "$install_script" || script_state=$?
    if [[ $script_state -ne 0 ]]; then
        if [[ $script_state -eq 1 && $CHECK_ONLY -eq 1 ]]; then
            log_step check todo "current 尚未安裝，無法執行 install.sh --check"
            record_todo "current 尚未安裝，無法執行 install.sh --check"
            return 0
        fi
        log_step check fail "找不到或無法讀取 ${install_script}"
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
    elif [[ $rc -eq 1 ]]; then
        # 新三態 rc=1：確定不成立。
        # 檢查是否為「隧道設定未填 (結束碼 78) / 尚未提供會合點」所致：
        # 若主要服務 llmgateway.service 是 active，且 tunnel.env 尚未填妥 RENDEZVOUS，
        # 則此狀態為等待會合點設定的預期 todo，而非腳本安裝失敗。
        local conf_dir="${BOOT_HOME}/.config/llmgateway"
        local tunnel_env="${conf_dir}/tunnel.env"
        local tunnel_configured=0
        local tunnel_state=0
        llmgw_exists "$tunnel_env" || tunnel_state=$?
        if [[ $tunnel_state -eq 0 ]]; then
            local tunnel_content=""
            if tunnel_content="$(run_as_llmgw cat "$tunnel_env")" \
                && grep -Eq '^RENDEZVOUS=[^[:space:]]+' <<< "$tunnel_content"; then
                tunnel_configured=1
            fi
        fi

        local gw_active=0
        if run_as_llmgw systemctl --user is-active --quiet llmgateway.service 2>/dev/null; then
            gw_active=1
        fi

        if [[ $tunnel_configured -eq 0 && $gw_active -eq 1 ]]; then
            log_step check todo "install.sh --check 未通過 (rc=1，llmgateway 正常但隧道設定未填/結束碼 78)"
            record_todo "待完成會合點設定後使 install.sh --check 通過"
        else
            log_step check fail "install.sh --check 未通過 (rc=1)"
            step_failed=1
        fi
    elif [[ $rc -eq 2 ]]; then
        log_step check fail "install.sh --check 無法確認 (rc=2)"
        step_failed=1
    else
        log_step check fail "install.sh --check 異常退出 (rc=${rc})"
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
    local pub_state=0 pub_content=""
    llmgw_exists "$tunnel_pub" || pub_state=$?
    if [[ $pub_state -eq 0 ]]; then
        if pub_content="$(run_as_llmgw cat "$tunnel_pub")"; then
            printf '%s\n' "$pub_content"
        else
            echo "   (無法讀取 ${tunnel_pub})"
        fi
    elif [[ $pub_state -eq 1 ]]; then
        echo "   (尚未產生)"
    else
        echo "   (無法判斷 ${tunnel_pub} 是否存在)"
    fi
    echo ""
    echo "2. 這台的 ed25519 host 公鑰 (給 GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS):"
    if [[ -f "$HOSTKEY_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ -z "$line" || "$line" =~ ^# ]] && continue
            # 只印前兩欄（類型 + 金鑰），去掉註解（可能帶機器名）
            awk '{print $1, $2}' <<< "$line" | while read -r type key rest; do
                printf '[127.0.0.1]:4711 %s %s\n' "$type" "$key"
            done
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
step_prereq
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_user
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_linger
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_sshd
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_install
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_tunnel_key
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_deploy_key
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_rendezvous
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

step_check
if [[ $step_failed -ne 0 ]]; then print_summary; exit 1; fi

print_summary

if [[ $step_failed -ne 0 ]]; then
    exit 1
elif [[ ${#TODOS[@]} -gt 0 ]]; then
    exit 3
else
    exit 0
fi
