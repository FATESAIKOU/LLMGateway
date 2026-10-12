#!/usr/bin/env bash
# deploy/install.sh
#
# 在常駐機器上安裝 LLMGateway：固定 tag 取碼、current symlink 原子切換、
# systemd user unit（subscription registry lgwd、隧道守護 llmgateway-tunnel、
# key-sync 的 timer）。放哪一台由安裝的人決定，這裡不寫機器名或 IP。
#
# 隧道守護與 key-sync 的檔案在 repo 的 ops-scripts/（維運工具定位）；從取到的
# tag 目錄裝，不從執行中的這份腳本旁邊裝。
#
# 隧道守護承載部署連線本身，所以這裡絕不當場 restart／stop 它：沒在跑就
# start，有更新時用 systemd-run 排一個約 60 秒後的重啟。
set -uo pipefail

LGW_REPO="https://github.com/FATESAIKOU/LLMGateway"
# TAG 格式與 deploy/bootstrap.sh 的 TAG_REGEX 同值；改一邊要改另一邊。
TAG_REGEX='^lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'

log() {
    local level="$1"; shift
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [[ "$level" == "INFO" ]]; then
        printf '[%s] %s %s\n' "$ts" "$level" "$*"
    else
        printf '[%s] %s %s\n' "$ts" "$level" "$*" >&2
    fi
}

usage() {
    echo "usage: install.sh --tag lgw-vX.Y.Z [--home <dir>] [--check]" >&2
}

HOME_DIR="${HOME}"
CHECK_ONLY=0
LGW_TAG=""
TAG_SPECIFIED=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --home) [[ $# -ge 2 ]] || { usage; exit 2; }; HOME_DIR="$2"; shift 2 ;;
        --tag) [[ $# -ge 2 ]] || { usage; exit 2; }; LGW_TAG="$2"; TAG_SPECIFIED=1; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log ERROR "unknown argument: $1"; usage; exit 2 ;;
    esac
done

if [[ "$TAG_SPECIFIED" -eq 1 ]]; then
    if [[ ! "$LGW_TAG" =~ $TAG_REGEX ]]; then
        log ERROR "tag 格式不符（須符合 ${TAG_REGEX}）：${LGW_TAG}"
        exit 2
    fi
fi

if [[ "$CHECK_ONLY" -eq 0 && "$TAG_SPECIFIED" -eq 0 ]]; then
    log ERROR "未指定 --tag"
    usage
    exit 2
fi

SHARE_DIR="${HOME_DIR}/.local/share/llmgateway"
TAG_DIR="${SHARE_DIR}/${LGW_TAG}"
CURRENT_LINK="${SHARE_DIR}/current"
TAG_FILE="${CURRENT_LINK}/.lgw-tag"
CONF_DIR="${HOME_DIR}/.config/llmgateway"
ENV_FILE="${CONF_DIR}/llmgateway.env"
TUNNEL_ENV_FILE="${CONF_DIR}/tunnel.env"
KEYSYNC_ENV_FILE="${CONF_DIR}/keysync.env"
STATE_DIR="${HOME_DIR}/.local/state/llmgateway"
KEYS_FILE="${STATE_DIR}/keys.env"
BIN_DIR="${HOME_DIR}/.local/bin"
UNIT_DIR="${HOME_DIR}/.config/systemd/user"
SERVICE_NAME="llmgateway.service"
TUNNEL_SERVICE_NAME="llmgateway-tunnel.service"
TUNNEL_BIN="llmgateway-tunnel"
KEY_SYNC_BIN="key-sync"
KEY_SYNC_SERVICE="key-sync.service"
KEY_SYNC_TIMER="key-sync.timer"
KEY_SYNC_PASSWORD_VAR="LLMGW_KEYS_PASSWORD"
# 金鑰正本所在的 private repo（key-sync 從這裡拉，--check 也問讀不讀得到）
KEYS_REPO="FATESAIKOU/LLMGateway-keys"
OPS_DIR="ops-scripts"

# 測試可換成假指令。
SYSTEMCTL="${LGW_SYSTEMCTL:-systemctl}"
SYSTEMD_RUN="${LGW_SYSTEMD_RUN:-systemd-run}"

DEFAULT_API_PORT="8480"

# install_file <source> <target> <mode>：同目錄暫存 → chmod → mv（原子，不留暫存）。
install_file() {
    local src="$1" dst="$2" mode="$3" dir tmp
    dir="${dst%/*}"
    tmp="$(mktemp "${dir}/.${dst##*/}.XXXXXX")" || return 1
    if ! cp "$src" "$tmp" || ! chmod "$mode" "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    mv -f "$tmp" "$dst" || { rm -f "$tmp"; return 1; }
}

# install_text <target> <mode>：stdin → 暫存 → chmod → mv（原子，不留暫存）。
install_text() {
    local dst="$1" mode="$2" dir tmp
    dir="${dst%/*}"
    tmp="$(mktemp "${dir}/.${dst##*/}.XXXXXX")" || return 1
    if ! cat > "$tmp" || ! chmod "$mode" "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    mv -f "$tmp" "$dst" || { rm -f "$tmp"; return 1; }
}

# switch_link <tmp-link> <link>：把 symlink 原子切過去。
switch_link() {
    if mv -T "$1" "$2" 2>/dev/null; then
        return 0
    fi
    # 非 GNU mv（例如 macOS 本機）沒有 -T，且 mv -f 會把暫存搬進 link 指到的
    # 目錄。先 unlink 再搬，有極小窗口；正式環境（Linux）走上面的分支。
    rm -f "$2" || return 1
    mv -f "$1" "$2"
}

need_python() {
    command -v python3 >/dev/null 2>&1 || { log ERROR "需要 python3（>= 3.10），找不到"; return 1; }
    local ver num major rest minor
    ver="$(python3 --version 2>&1)" || { log ERROR "python3 --version 失敗"; return 1; }
    num="${ver#Python }"
    major="${num%%.*}"; rest="${num#*.}"; minor="${rest%%.*}"
    case "$major" in ''|*[!0-9]*) log ERROR "看不懂 python3 版本：${ver}"; return 1;; esac
    case "$minor" in ''|*[!0-9]*) log ERROR "看不懂 python3 版本：${ver}"; return 1;; esac
    if [[ "$major" -lt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -lt 10 ]]; }; then
        log ERROR "需要 python3 >= 3.10，現在是 ${ver}"
        return 1
    fi
}

check_port() {
    local port="$DEFAULT_API_PORT"
    if [[ -f "$ENV_FILE" ]]; then
        . "$ENV_FILE" 2>/dev/null || true
        [[ -n "${LGW_API_PORT:-}" ]] && port="$LGW_API_PORT"
    fi
    printf '%s' "$port"
}

# fetch_code：TAG_DIR 已裝完整（.lgw-tag 相符）就直接用，不重抓——current
# 可能正指著它，服務跑在裡面。否則抓到同層暫存目錄、驗完再 mv 就位；
# 殘留的不完整 TAG_DIR 先移開；失敗清掉暫存目錄。
fetch_code() {
    local installed_tag=""
    [[ -f "${TAG_DIR}/.lgw-tag" ]] && installed_tag="$(cat "${TAG_DIR}/.lgw-tag" 2>/dev/null)"
    if [[ "$installed_tag" == "$LGW_TAG" ]]; then
        log INFO "already have ${LGW_TAG}, reuse"
        return 0
    fi
    local tag_tmp stale
    tag_tmp="$(mktemp -d "${SHARE_DIR}/.${LGW_TAG}.XXXXXX")" || {
        log ERROR "建暫存目錄失敗"; return 1; }
    if ! git clone --depth 1 --branch "$LGW_TAG" "$LGW_REPO" "$tag_tmp" </dev/null; then
        log ERROR "取碼失敗（${LGW_REPO} ${LGW_TAG}）"
        rm -rf "$tag_tmp"
        return 1
    fi
    if [[ ! -f "${tag_tmp}/policy/default.json" ]]; then
        log ERROR "取到的版本缺 policy/default.json"
        rm -rf "$tag_tmp"
        return 1
    fi
    if [[ ! -f "${tag_tmp}/deploy/llmgateway-run" ]] || [[ ! -f "${tag_tmp}/deploy/llmgateway.service" ]]; then
        log ERROR "取到的版本缺 deploy/llmgateway-run 或 deploy/llmgateway.service"
        rm -rf "$tag_tmp"
        return 1
    fi
    if [[ ! -f "${tag_tmp}/${OPS_DIR}/${TUNNEL_BIN}" ]] || [[ ! -f "${tag_tmp}/${OPS_DIR}/${TUNNEL_SERVICE_NAME}" ]]; then
        log ERROR "取到的版本缺 ${OPS_DIR}/${TUNNEL_BIN} 或 ${OPS_DIR}/${TUNNEL_SERVICE_NAME}"
        rm -rf "$tag_tmp"
        return 1
    fi
    if [[ ! -f "${tag_tmp}/${OPS_DIR}/${KEY_SYNC_BIN}" ]] \
        || [[ ! -f "${tag_tmp}/${OPS_DIR}/${KEY_SYNC_SERVICE}" ]] \
        || [[ ! -f "${tag_tmp}/${OPS_DIR}/${KEY_SYNC_TIMER}" ]]; then
        log ERROR "取到的版本缺 ${OPS_DIR}/${KEY_SYNC_BIN} 或它的 unit／timer"
        rm -rf "$tag_tmp"
        return 1
    fi
    printf '%s\n' "$LGW_TAG" | install_text "${tag_tmp}/.lgw-tag" 644 || {
        log ERROR "寫版本標記失敗"
        rm -rf "$tag_tmp"
        return 1
    }
    if [[ -e "$TAG_DIR" ]]; then
        stale="${TAG_DIR}.stale.$$"
        rm -rf "$stale"
        mv -f "$TAG_DIR" "$stale" || {
            rm -rf "$tag_tmp"; log ERROR "移開殘留目錄失敗"; return 1; }
        if mv -f "$tag_tmp" "$TAG_DIR"; then
            rm -rf "$stale"
        else
            mv -f "$stale" "$TAG_DIR" 2>/dev/null || true
            rm -rf "$tag_tmp"
            log ERROR "新版就位失敗"
            return 1
        fi
    else
        mv -f "$tag_tmp" "$TAG_DIR" || {
            rm -rf "$tag_tmp"; log ERROR "新版就位失敗"; return 1; }
    fi
    return 0
}

# check_unit：0 成立、1 確定不成立、2 無法確認。
check_unit() {
    command -v "$SYSTEMCTL" >/dev/null 2>&1 || { log INFO "llmgateway: systemctl 不能用"; return 2; }
    local unit rc
    for unit in "$SERVICE_NAME" "$TUNNEL_SERVICE_NAME"; do
        "$SYSTEMCTL" --user is-active --quiet "$unit" >/dev/null 2>&1
        rc=$?
        if [[ "$rc" -eq 3 ]]; then
            log INFO "llmgateway: ${unit} 沒在跑"
            return 1
        elif [[ "$rc" -ne 0 ]]; then
            log INFO "llmgateway: systemctl 不能用（rc=${rc}）"
            return 2
        fi
    done
    if [[ -n "$LGW_TAG" ]]; then
        local installed_tag=""
        [[ -f "$TAG_FILE" ]] && installed_tag="$(cat "$TAG_FILE" 2>/dev/null)"
        if [[ "$installed_tag" != "$LGW_TAG" ]]; then
            log INFO "llmgateway: 版本不同（裝的是 '${installed_tag}'，要 '${LGW_TAG}'）"
            return 1
        fi
    fi
    local port code
    port="$(check_port)"
    code=""
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${port}/v1/subscriptions" 2>/dev/null)"
    rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log INFO "llmgateway: curl 連不上或逾時（rc=${rc}）"
        return 2
    fi
    if [[ "$code" != "200" ]]; then
        log INFO "llmgateway: 本機 list 探針回 ${code}，不是 200"
        return 1
    fi
    log INFO "llmgateway: 本機 list 探針回 200"

    # 經會合點：用隧道守護同一份設定與 ssh 參數再探一次端對端。
    local probe_bin="${BIN_DIR}/${TUNNEL_BIN}" probe_rc
    if [[ ! -x "$probe_bin" ]]; then
        log INFO "llmgateway: 經會合點探針無法執行（找不到 ${probe_bin}）"
        return 2
    fi
    HOME="$HOME_DIR" "$probe_bin" --probe-once >/dev/null 2>&1
    probe_rc=$?
    case "$probe_rc" in
        0)
            log INFO "llmgateway: 經會合點 list 探針回 200"
            ;;
        1)
            log INFO "llmgateway: 經會合點 list 探針失敗（不是 200 或沒回應）"
            return 1
            ;;
        *)
            log INFO "llmgateway: 經會合點探針無法執行（rc=${probe_rc}）"
            return 2
            ;;
    esac

    # 隧道重啟已排程（約 60 秒後重連）：不改三態，只提醒 (L-R3)。
    if "$SYSTEMCTL" --user is-active --quiet llmgateway-tunnel-restart.timer >/dev/null 2>&1; then
        log INFO "llmgateway-tunnel: 重啟已排程（約 60 秒後重連），不是故障"
    fi

    # 服務活著，再看金鑰拉取那條路能不能用（v5.4）。
    check_keysync
}

# check_keysync：key-sync 能不能用（v5.4）。0 成立、1 確定不成立、2 無法確認。
#   1. 以服務帳號身分問 gh：有沒有登入、讀不讀得到金鑰 repo
#      （沒登入／token 失效／讀不到 repo → 1：key 會凍在舊版；
#        gh 不存在或查詢逾時 → 2：沒法確認；
#        經 sudo 問而 sudo 自己失敗（要密碼、本體缺席）→ 2：連問都沒問到）；
#   2. 解密密碼放了沒（keysync.env 有非空的一行；只看有沒有，不讀值出來）；
#   3. 上次拉取有沒有失敗（systemd 把最後一次非零結束的 oneshot 記成 failed）。
# 排到最後才跑：前面（unit、本機、經會合點）先確定服務是活的。
check_keysync() {
    # 2：服務帳號名（預設由 HOME_DIR 推；測試可覆寫）。
    local service_user="${LGW_SERVICE_USER:-$(basename "$HOME_DIR")}"
    local gh_bin="${LGW_GH:-gh}"
    local gh_timeout="${LGW_GH_TIMEOUT:-30}"

    # gh 的登入跟著 Unix 使用者走，所以一定要問服務帳號的狀態；目前就是服務帳號
    # 就直接跑，否則 sudo -u 過去（升級時用管理帳號跑 --check 的情況）。
    local current_user=""
    current_user="$(id -un 2>/dev/null || true)"
    local -a gh_cmd=()
    local use_sudo=0
    if [[ -z "$current_user" || "$current_user" == "$service_user" ]]; then
        gh_cmd=("$gh_bin")
    else
        gh_cmd=("${LGW_SUDO:-sudo}" -n -u "$service_user" "$gh_bin")
        use_sudo=1
    fi
    # 有 timeout 就加（沒有的系統就直接跑，不擋）
    if command -v timeout >/dev/null 2>&1; then
        gh_cmd=(timeout "$gh_timeout" "${gh_cmd[@]}")
    fi

    local auth_out="" rc=0
    auth_out="$("${gh_cmd[@]}" auth status 2>&1)" || rc=$?
    # 管理帳號直跑 --check 且 sudo 快取缺時，sudo -n 自己失敗（rc=1＋sudo:…），
    # 不能算成「gh 沒登入」；sudo 本體缺席的 127 同理（#13 新-低-1）。三態用 2。
    if [[ "$use_sudo" -eq 1 ]]; then
        if [[ "$rc" -eq 127 || "$auth_out" == *"sudo:"* ]]; then
            log INFO "key-sync: 無法確認（sudo 需要密碼或失敗，無法以 ${service_user} 身分問 gh）"
            return 2
        fi
    fi
    if [[ "$rc" -eq 127 ]]; then
        log INFO "key-sync: 無法確認（${service_user} 身分找不到 ${gh_bin}）"
        return 2
    fi
    if [[ "$rc" -eq 124 ]]; then
        log INFO "key-sync: 無法確認（${gh_bin} auth status 逾時 ${gh_timeout}s）"
        return 2
    fi
    if [[ "$rc" -ne 0 ]]; then
        log INFO "key-sync: 不能用（${service_user} 的 gh 沒登入或 token 失效）"
        return 1
    fi

    # 登入不代表讀得到那個 repo（權限被改、repo 改名、網路擋）；只問 repo 本身，
    # 不讀任何檔案內容。
    local repo_out="" repo_rc=0
    repo_out="$("${gh_cmd[@]}" api "repos/${KEYS_REPO}" --jq .full_name 2>&1)" || repo_rc=$?
    # 同上：經 sudo 問而 sudo 自己失敗時回 2，不是「讀不到 repo」。
    if [[ "$use_sudo" -eq 1 ]]; then
        if [[ "$repo_rc" -eq 127 || "$repo_out" == *"sudo:"* ]]; then
            log INFO "key-sync: 無法確認（sudo 需要密碼或失敗，無法以 ${service_user} 身分問 gh）"
            return 2
        fi
    fi
    if [[ "$repo_rc" -eq 124 ]]; then
        log INFO "key-sync: 無法確認（讀 ${KEYS_REPO} 逾時 ${gh_timeout}s）"
        return 2
    fi
    # 真 gh 的 --jq .full_name 預期回裸名；某版包引號或多空白時別誤判 1
    # （觀察-2）。純 bash 修剪（不叫 sed/tr，測試 PATH 未必有）。
    local repo_name="$repo_out"
    repo_name="${repo_name#"${repo_name%%[![:space:]]*}"}"
    repo_name="${repo_name%"${repo_name##*[![:space:]]}"}"
    if [[ "$repo_name" == '"'*'"' ]]; then
        repo_name="${repo_name#?}"
        repo_name="${repo_name%?}"
    elif [[ "$repo_name" == \'*\' ]]; then
        repo_name="${repo_name#?}"
        repo_name="${repo_name%?}"
    fi
    repo_name="${repo_name#"${repo_name%%[![:space:]]*}"}"
    repo_name="${repo_name%"${repo_name##*[![:space:]]}"}"
    if [[ "$repo_rc" -ne 0 ]] || [[ "$repo_name" != "${KEYS_REPO}" ]]; then
        log INFO "key-sync: 不能用（讀不到 ${KEYS_REPO}：登入的帳號可能沒有讀權限）"
        return 1
    fi
    log INFO "key-sync: gh 已登入，且讀得到 ${KEYS_REPO}"

    if [[ ! -f "$KEYSYNC_ENV_FILE" ]]; then
        log INFO "key-sync: 還沒放解密密碼（${KEYSYNC_ENV_FILE}）"
        return 1
    fi
    # 分不出「讀不到」（權限被改壞等）與「沒有那行」就不該講同一句話：前者請人修
    # 權限，後者請人放密碼（審查 #13 低-6；同 bootstrap step_check 的 M-F3 精神）。
    if [[ ! -r "$KEYSYNC_ENV_FILE" ]]; then
        log INFO "key-sync: 讀不到解密密碼檔（${KEYSYNC_ENV_FILE}，檢查權限）"
        return 1
    fi
    if ! grep -Eq "^[[:space:]]*${KEY_SYNC_PASSWORD_VAR}=[^[:space:]]+" "$KEYSYNC_ENV_FILE" 2>/dev/null; then
        log INFO "key-sync: 解密密碼檔裡沒有非空的 ${KEY_SYNC_PASSWORD_VAR}"
        return 1
    fi
    log INFO "key-sync: 解密密碼已放"

    "$SYSTEMCTL" --user is-failed --quiet "$KEY_SYNC_SERVICE" >/dev/null 2>&1
    rc=$?
    if [[ "$rc" -eq 0 ]]; then
        log INFO "key-sync: 最近一次執行失敗（看 journalctl --user -u ${KEY_SYNC_SERVICE%.service}）"
        return 1
    fi
    # rc ≠ 0：沒失敗，或查不到 unit（舊安裝）——後者不當成失敗
    return 0
}

# cleanup_old_versions：只留 current 指著的與前一個，其他刪掉；current 指著的絕對不刪。
cleanup_old_versions() {
    local current_target
    current_target="$(readlink "$CURRENT_LINK" 2>/dev/null || true)"
    [[ -n "$current_target" ]] || return 0
    local current_name="${current_target##*/}"

    local dirs=()
    local d name
    for d in "${SHARE_DIR}"/*; do
        [[ -d "$d" && ! -L "$d" ]] || continue
        name="${d##*/}"
        if [[ -f "$d/.lgw-tag" ]] || [[ "$name" =~ $TAG_REGEX ]]; then
            dirs+=("$name")
        fi
    done

    [[ ${#dirs[@]} -le 2 ]] && return 0

    local others=()
    for name in "${dirs[@]}"; do
        if [[ "$name" != "$current_name" ]]; then
            others+=("$name")
        fi
    done

    [[ ${#others[@]} -le 1 ]] && return 0

    local keep_other=""
    if [[ -n "${PREV_TAG:-}" && "$PREV_TAG" != "$current_name" ]]; then
        for name in "${others[@]}"; do
            if [[ "$name" == "$PREV_TAG" ]]; then
                keep_other="$name"
                break
            fi
        done
    fi

    if [[ -z "$keep_other" ]]; then
        keep_other="$(printf '%s\n' "${others[@]}" | sort -V | tail -n 1)"
    fi

    for name in "${others[@]}"; do
        if [[ "$name" != "$keep_other" ]]; then
            log INFO "removing old version ${name}"
            rm -rf "${SHARE_DIR:?}/${name}"
        fi
    done
}

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    check_unit
    exit $?
fi

need_python || exit 1

mkdir -p "$SHARE_DIR" "$CONF_DIR" "$STATE_DIR" "$BIN_DIR" "$UNIT_DIR" || {
    log ERROR "建目錄失敗"; exit 1; }
chmod 0700 "$STATE_DIR" || { log ERROR "chmod 狀態目錄失敗"; exit 1; }

if [[ ! -f "$ENV_FILE" ]]; then
    printf 'LGW_API_PORT="%s"\n' "$DEFAULT_API_PORT" | install_text "$ENV_FILE" 644 || {
        log ERROR "寫 env 檔失敗"; exit 1; }
fi

if [[ ! -f "$TUNNEL_ENV_FILE" ]]; then
    {
        printf '%s\n' "# LLMGateway 隧道守護設定。必填三項（值留空）；其餘留空用預設值。"
        printf '%s\n' "# 預設：REMOTE_API=4710 REMOTE_SSH=4711 LOCAL_API=8480 PROBE_INTERVAL=60 PROBE_FAILS=3"
        printf 'RENDEZVOUS=\n'
        printf 'IDENTITY=\n'
        printf 'KNOWN_HOSTS=\n'
        printf 'REMOTE_API=\n'
        printf 'REMOTE_SSH=\n'
        printf 'LOCAL_API=\n'
        printf 'PROBE_INTERVAL=\n'
        printf 'PROBE_FAILS=\n'
    } | install_text "$TUNNEL_ENV_FILE" 644 || { log ERROR "寫隧道設定範本失敗"; exit 1; }
fi

if [[ ! -f "$KEYS_FILE" ]]; then
    : | install_text "$KEYS_FILE" 600 || { log ERROR "建 keys.env 失敗"; exit 1; }
fi

fetch_code || exit 1

PREV_TAG=""
if [[ -L "$CURRENT_LINK" ]]; then
    _link="$(readlink "$CURRENT_LINK" 2>/dev/null || true)"
    [[ -n "$_link" ]] && PREV_TAG="${_link##*/}"
fi

tmp_link="${CURRENT_LINK}.tmp"
rm -f "$tmp_link"
ln -s "$TAG_DIR" "$tmp_link" || { log ERROR "建暫存連結失敗"; exit 1; }
switch_link "$tmp_link" "$CURRENT_LINK" || { rm -f "$tmp_link"; log ERROR "切換 current 失敗"; exit 1; }

cleanup_old_versions

install_file "${TAG_DIR}/deploy/llmgateway-run" "${BIN_DIR}/llmgateway-run" 755 || {
    log ERROR "裝 llmgateway-run 失敗"; exit 1; }
install_file "${TAG_DIR}/deploy/llmgateway.service" "${UNIT_DIR}/${SERVICE_NAME}" 644 || {
    log ERROR "裝 systemd unit 失敗"; exit 1; }

# 隧道執行檔或 unit 有變更嗎（不存在＝有變更）；要在覆蓋前比。
tunnel_changed=0
if ! cmp -s "${TAG_DIR}/${OPS_DIR}/${TUNNEL_BIN}" "${BIN_DIR}/${TUNNEL_BIN}" 2>/dev/null; then
    tunnel_changed=1
fi
if ! cmp -s "${TAG_DIR}/${OPS_DIR}/${TUNNEL_SERVICE_NAME}" "${UNIT_DIR}/${TUNNEL_SERVICE_NAME}" 2>/dev/null; then
    tunnel_changed=1
fi

install_file "${TAG_DIR}/${OPS_DIR}/${TUNNEL_BIN}" "${BIN_DIR}/${TUNNEL_BIN}" 755 || {
    log ERROR "裝 ${TUNNEL_BIN} 失敗"; exit 1; }
install_file "${TAG_DIR}/${OPS_DIR}/${TUNNEL_SERVICE_NAME}" "${UNIT_DIR}/${TUNNEL_SERVICE_NAME}" 644 || {
    log ERROR "裝隧道 systemd unit 失敗"; exit 1; }

# key-sync 與它的 unit／timer（v5.4：金鑰自己拉）；timer 只負責定期觸發。
install_file "${TAG_DIR}/${OPS_DIR}/${KEY_SYNC_BIN}" "${BIN_DIR}/${KEY_SYNC_BIN}" 755 || {
    log ERROR "裝 ${KEY_SYNC_BIN} 失敗"; exit 1; }
install_file "${TAG_DIR}/${OPS_DIR}/${KEY_SYNC_SERVICE}" "${UNIT_DIR}/${KEY_SYNC_SERVICE}" 644 || {
    log ERROR "裝 ${KEY_SYNC_SERVICE} 失敗"; exit 1; }
install_file "${TAG_DIR}/${OPS_DIR}/${KEY_SYNC_TIMER}" "${UNIT_DIR}/${KEY_SYNC_TIMER}" 644 || {
    log ERROR "裝 ${KEY_SYNC_TIMER} 失敗"; exit 1; }

"$SYSTEMCTL" --user daemon-reload || { log ERROR "daemon-reload 失敗"; exit 1; }
if "$SYSTEMCTL" --user is-active --quiet "$SERVICE_NAME" >/dev/null 2>&1; then
    "$SYSTEMCTL" --user restart "$SERVICE_NAME" || { log ERROR "restart 失敗"; exit 1; }
else
    "$SYSTEMCTL" --user enable --now "$SERVICE_NAME" || { log ERROR "enable 失敗"; exit 1; }
fi

# 隧道守護承載部署連線本身：只 enable；沒在跑就 start；有變更才排
# 約 60 秒後的重啟，絕不當場 restart／stop（會切斷正在跑的部署）。
"$SYSTEMCTL" --user enable "$TUNNEL_SERVICE_NAME" || {
    log ERROR "enable 隧道 unit 失敗"; exit 1; }
if ! "$SYSTEMCTL" --user is-active --quiet "$TUNNEL_SERVICE_NAME" >/dev/null 2>&1; then
    "$SYSTEMCTL" --user start "$TUNNEL_SERVICE_NAME" || \
        log INFO "llmgateway-tunnel: start 未成功（設定未填或未就緒時為預期）"
elif [[ "$tunnel_changed" -eq 1 ]]; then
    if "$SYSTEMCTL" --user is-active --quiet llmgateway-tunnel-restart.timer >/dev/null 2>&1; then
        log INFO "llmgateway-tunnel: 已排程"
    elif ! "$SYSTEMD_RUN" --user --on-active=60 --unit=llmgateway-tunnel-restart \
        "$SYSTEMCTL" --user restart "$TUNNEL_SERVICE_NAME" 2>/dev/null; then
        log WARN "llmgateway-tunnel: 排程重啟失敗（忽略）"
    fi
fi

# key-sync 的 timer：只 enable；沒在跑才 start（已在跑就不動它，免得把排程重來）。
"$SYSTEMCTL" --user enable "$KEY_SYNC_TIMER" || {
    log ERROR "enable ${KEY_SYNC_TIMER} 失敗"; exit 1; }
if "$SYSTEMCTL" --user is-active --quiet "$KEY_SYNC_TIMER" >/dev/null 2>&1; then
    log INFO "${KEY_SYNC_TIMER}: 已在跑"
else
    "$SYSTEMCTL" --user start "$KEY_SYNC_TIMER" || \
        log WARN "${KEY_SYNC_TIMER}: start 未成功（可稍後 systemctl --user start ${KEY_SYNC_TIMER}）"
fi

log INFO "installed llmgateway ${LGW_TAG}"
