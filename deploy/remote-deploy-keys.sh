#!/bin/sh
# LLMGateway：provider 上的部署腳本（design 決定 2；MyLinuxPool#68）。
# 由部署專用金鑰的 authorized_keys 以 command="…/remote-deploy-keys.sh",restrict 強制執行；
# 不看 SSH_ORIGINAL_COMMAND，只從 stdin 讀 key 檔內容。
# 流程：stdin（上限 64 KiB）→ 驗證格式 → 同目錄暫存檔（0600）→ rename 成 keys.env → 通知服務重讀。
# 任何輸出都不含值。
set -eu

STATE_DIR="${LGW_STATE_DIR:-$HOME/.local/state/llmgateway}"
KEYS_FILE="$STATE_DIR/keys.env"
MAX_BYTES=65536
# 重讀指令：預設對 systemd user unit 送 SIGHUP；測試可覆寫
RELOAD_CMD="${LGW_RELOAD_CMD:-systemctl --user kill -s HUP llmgateway.service}"
ALLOWED='CLAUDE_PERSONAL_OAUTH_TOKEN CLAUDE_COMPANY_OAUTH_TOKEN OPENROUTER_API_KEY OPENCODE_ZEN_API_KEY OLLAMA_API_KEY'

umask 077
mkdir -p "$STATE_DIR"
tmp="$(mktemp "$STATE_DIR/.keys.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

# 多讀 1 byte 才知道有沒有超過上限
head -c $((MAX_BYTES + 1)) > "$tmp"
size="$(wc -c < "$tmp" | tr -d ' ')"
if [ "$size" -gt "$MAX_BYTES" ]; then
  echo "deploy-keys: input too large" >&2
  exit 3
fi
if [ "$size" -eq 0 ]; then
  echo "deploy-keys: empty input" >&2
  exit 3
fi

# 驗證：每行是空行、# 註解，或 KEY=VALUE（KEY 在允許清單、同一個 KEY 只出現一次、VALUE 不含空白與引號）
seen=""
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  case "$line" in
    ''|'#'*) continue ;;
  esac
  name="${line%%=*}"
  if [ "$name" = "$line" ]; then
    echo "deploy-keys: line $lineno is not KEY=VALUE" >&2
    exit 4
  fi
  value="${line#*=}"
  ok=0
  for allowed in $ALLOWED; do
    [ "$name" = "$allowed" ] && ok=1
  done
  if [ "$ok" -ne 1 ]; then
    echo "deploy-keys: line $lineno has an unknown key name" >&2
    exit 4
  fi
  case " $seen " in
    *" $name "*) echo "deploy-keys: duplicate key name on line $lineno" >&2; exit 4 ;;
  esac
  seen="$seen $name"
  case "$value" in
    *[[:space:]]*|*\"*|*\'*) echo "deploy-keys: value on line $lineno contains whitespace or quotes" >&2; exit 4 ;;
  esac
done < "$tmp"

chmod 600 "$tmp"
mv -f "$tmp" "$KEYS_FILE"
trap - EXIT

if sh -c "$RELOAD_CMD" >/dev/null 2>&1; then
  echo "deploy-keys: written and reload requested"
else
  echo "deploy-keys: written, but reload failed (service will read it on next start)" >&2
  exit 5
fi
