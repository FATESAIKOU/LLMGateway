#!/bin/bash
# mac-fallback 三支腳本（start／stop／switch）共用的函式。只定義函式，source 進來不做任何動作。
# repo 裡不放任何實際值：程式目錄、密碼檔、狀態目錄、名稱對應全部來自設定檔（repo 外）。
# 不印任何 key 值：取值只用 awk 直接寫檔（0600）；名稱檢查只印「有／沒有」。

fb_say() { printf '%s\n' "$*"; }

# 讀設定檔。路徑：LGW_MAC_FALLBACK_CONF，沒設就用 ~/.config/llmgateway/mac-fallback.conf。
# LGW_FB_CODE／LGW_FB_TAG 若設了，覆蓋設定檔的 CODE／TAG（switch 用它換版本）。
fb_load_conf() {
  FB_CONF=${LGW_MAC_FALLBACK_CONF:-$HOME/.config/llmgateway/mac-fallback.conf}
  if [ ! -r "$FB_CONF" ]; then
    fb_say "STOP-PRE: 讀不到設定檔（${FB_CONF}）。範例見 mac-fallback.conf.example；路徑可用 LGW_MAC_FALLBACK_CONF 指定"
    return 1
  fi
  # 設定檔會被 source（等於執行它），只載入「屬於目前使用者」且 group／world 不可寫的檔
  local owner perm
  read -r owner perm < <(stat -f '%Su %Lp' "$FB_CONF" 2>/dev/null) || true
  if [ "$owner" != "$(id -un)" ]; then
    fb_say "STOP-PRE: 設定檔不是目前使用者所有（${FB_CONF}），拒絕載入"; return 1
  fi
  if [ $(( 8#$perm & 022 )) -ne 0 ]; then
    fb_say "STOP-PRE: 設定檔 group 或 world 可寫（權限 ${perm}），拒絕載入；請 chmod 600 ${FB_CONF}"; return 1
  fi
  # 預設值；設定檔可覆蓋
  PORT=4710
  WAIT_SECONDS=60
  KEYSYNC_INTERVAL=600
  AUDIT_SINCE=1970-01-01T00:00:00Z
  PWFILE=; CODE=; TAG=; STATE=; PASS_SOURCE=; KEY_MAP=; OPTIONAL_KEY_MAP=
  SWITCH_OLD_CODE=; SWITCH_OLD_TAG=; SWITCH_NEW_CODE=; SWITCH_NEW_TAG=
  VERIFY_PIPES=0; VERIFY_OLD_GET_ID=; VERIFY_RESOLVE=; VERIFY_RESOLVE_MODEL=
  . "$FB_CONF" || { fb_say "STOP-PRE: 設定檔載入失敗"; return 1; }
  [ -n "${LGW_FB_CODE:-}" ] && CODE=$LGW_FB_CODE
  [ -n "${LGW_FB_TAG:-}" ] && TAG=$LGW_FB_TAG
  return 0
}

# fb_require <變數名>…：設定檔沒填的必要項目就停
fb_require() {
  local v
  for v in "$@"; do
    [ -n "${!v:-}" ] || { fb_say "STOP-PRE: 設定檔缺 ${v}"; return 1; }
  done
}

# fb_check_checkout <目錄> <tag>：必須是 lgwd 的 checkout，且 HEAD 正好是那個 tag
fb_check_checkout() {
  local head
  [ -f "$1/lgwd/__main__.py" ] || { fb_say "STOP-PRE: $1 不是 lgwd 的 checkout"; return 1; }
  head=$(git -C "$1" describe --tags --exact-match HEAD 2>/dev/null || true)
  [ "$head" = "$2" ] || { fb_say "STOP-PRE: $1 的 HEAD 不是 ${2}（是「${head:-無 tag}」）"; return 1; }
  return 0
}

# 4710 有人在聽？找不到 lsof 時無法確認，當作有人在聽（保守，不啟動），並明說是哪個原因
fb_port_busy() {
  if ! command -v lsof >/dev/null 2>&1; then
    fb_say "找不到 lsof，無法確認 127.0.0.1:${PORT} 是否空著（保守當作不能用）"; return 0
  fi
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

fb_listeners() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print "  listener:", $1, "pid", $2}'
}

# 密碼檔裡有沒有這個名稱（只看名稱，不讀值進任何輸出）
fb_has() {
  cut -d= -f1 "$PWFILE" | tr -d ' \r' | grep -qxF -- "$1"
}

# 密碼檔可用？路線 1（PASS_SOURCE 在檔裡）或路線 2（KEY_MAP 每一組的來源都在檔裡）。只比對名稱，不印值。
fb_pw_ok() {
  [ -r "$PWFILE" ] || return 1
  if [ -n "$PASS_SOURCE" ] && fb_has "$PASS_SOURCE"; then return 0; fi
  [ -n "$KEY_MAP" ] || return 1
  local m
  for m in $KEY_MAP; do fb_has "${m%%:*}" || return 1; done
  return 0
}

# 印出每個對應的「來源 → 目標：有／沒有」
fb_names_report() {
  local m
  fb_say "== 密碼檔名稱檢查（只印有／沒有）"
  if fb_has "$PASS_SOURCE"; then fb_say "  $PASS_SOURCE → LLMGW_KEYS_PASSWORD: 有"; else fb_say "  $PASS_SOURCE → LLMGW_KEYS_PASSWORD: 沒有"; fi
  for m in $KEY_MAP $OPTIONAL_KEY_MAP; do
    if fb_has "${m%%:*}"; then fb_say "  ${m%%:*} → ${m#*:}: 有"; else fb_say "  ${m%%:*} → ${m#*:}: 沒有"; fi
  done
}

# fb_write_mapped <目的檔> <來源:目標>…
# 挑出來源名稱的行，鍵改成目標名稱、值原樣保留（值裡的 = 也保留）；沒列在對應裡的行不寫；檔案 0600。
fb_write_mapped() {
  local dst=$1; shift
  : > "$dst"; chmod 600 "$dst"
  awk -v maps="$*" 'BEGIN{n=split(maps,a," "); for(i=1;i<=n;i++){split(a[i],p,":"); to[p[1]]=p[2]}}
       { i=index($0,"="); if (i==0) next; k=substr($0,1,i-1); gsub(/[ \r]/,"",k);
         if (k in to) { v=substr($0,i+1); sub(/\r$/,"",v); print to[k] "=" v } }' "$PWFILE" >> "$dst"
}

# fb_keysync_verdict <status 檔> <啟動時刻 epoch 秒>
# 印出：none（沒有檔或讀不懂）／old（ts 早於啟動時刻）／ok／fail:<reason>
fb_keysync_verdict() {
  python3 -I - "$1" "$2" <<'PY' 2>/dev/null || echo none
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("not an object")
    ts = int(d.get("ts", 0))
except Exception:
    print("none"); sys.exit(0)
if ts < int(sys.argv[2]): print("old")
elif d.get("ok") is True: print("ok")
else: print("fail:" + str(d.get("reason")))
PY
}

# fb_kill_lgwd <pid>：只停這一個 pid，先確認它是 python3 -m lgwd；不是就不動（回傳 1）
fb_kill_lgwd() {
  local pid=$1 i
  kill -0 "$pid" 2>/dev/null || return 0
  ps -p "$pid" -o command= | grep -q "python3 -m lgwd" || { fb_say "STOP: pid $pid 不是 lgwd，不動"; return 1; }
  kill "$pid" 2>/dev/null
  for i in $(seq 10); do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
  fb_say "pid $pid 沒在 10 秒內結束，送 KILL"
  kill -9 "$pid" 2>/dev/null
  return 0
}
