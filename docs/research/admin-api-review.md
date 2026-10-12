# 管理 API 與部署腳本審查（commit 5d48ca2 & f7285f7 vs design 決定 2、3、4、6、key-vault spec）

> **審查身分**：LLMGateway 團隊 `agy-review`（PM：`llmgateway-f8`）  
> **審查範圍**：
> 1. commit `5d48ca2`：「deploy: remote-deploy.sh with keys/upgrade verbs; deploy-service workflow」
>    - `.github/workflows/deploy-service.yml`
>    - `.github/workflows/deploy-keys.yml`
>    - `deploy/remote-deploy.sh`
>    - `tests/test_deploy_script.py`
> 2. commit `f7285f7`：「lgwd: issuer role and admin API for worker credentials」
>    - `lgwd/http.py`
>    - `lgwd/leasing.py`
>    - `lgwd/store.py`
>    - `lgwd/errors.py`
>    - `lgw/admin.py`
>    - `tests/test_admin_api.py`
> 
> **對照規格**：
> - `openspec/changes/gateway-v0/design.md`（決定 2：key 保管與部署；決定 3：服務形狀；決定 4：客戶端身分；決定 6：租用 API）
> - `openspec/changes/gateway-v0/specs/key-vault/spec.md`（key-vault 規格）
> 
> **審查結論**：**必修 0 條，建議 7 條**。  
> 核心安全邊界、白名單解析、角色分離、同名重發之交易原子性、資料庫欄位遷移與事件還原、零秘密外洩等核心機制均嚴格符合設計與規格要求。建議項主要聚焦於 IPv6 伺服器啟動相容性、位址遮蔽 edge case、維運腳本前置防呆與重送語意說明。

---

## 0. 規格合規與安全檢驗清查總結

| 檢驗項目 | 對照規格 | 檢驗結果 | 說明 |
|---|---|---|---|
| **1. remote-deploy.sh 白名單解析** | design 決定 2 | **通過** | 單行強制檢查、`keys` 嚴格全字比對、`upgrade <tag>` 嚴格正規式、無 shell eval、引數皆雙引號傳遞 |
| **2. deploy-service.yml 驗證與遮蔽** | design 決定 2 | **通過** | `TAG` 經環境變數傳入避免 GHA 注入、雙層 regex 檢驗、失敗不回顯輸入、stderr 通過 sed 遮罩跳板與目標位址 |
| **3. 角色分離（client / issuer）** | design 決定 4, 6 | **通過** | client 無法打 admin API；issuer 無法打租約（leases）與狀態板（board）；`whoami` 兩者皆可用且免額度 |
| **4. 來源介面限制（Loopback-only）** | design 決定 3, 4 | **通過** | 非 loopback 介面請求 admin API 一律 403；外部介面連線 fail-closed |
| **5. worker-* 正規式** | design 決定 4 | **通過** | `worker-[A-Za-z0-9._-]{1,57}` 全字匹配，長度 8~64 字元，防路徑穿越與特殊字元 |
| **6. 同名重發之交易原子性** | design 決定 4, 12 | **通過** | 單一 `BEGIN IMMEDIATE` 交易內撤銷舊憑證、撤銷進行中租約、寫入新雜湊、記錄事件，全成或全敗 |
| **7. Issuer 權限隔離** | design 決定 4 | **通過** | issuer 無法撤銷 Mac 憑證（403）或 Issuer 憑證（404），亦無法重發覆蓋 issuer |
| **8. 舊 DB 遷移與 Events 還原** | design 決定 3, 12 | **通過** | `PRAGMA table_info` 動態補 `role` 欄位預設 `client`；`rebuild` 從 `client_added` event 還原 `role` |
| **9. 重送與去重語意** | design 決定 4, 6, 12 | **通過** | admin API 不帶 `request_id`，重發即輪替；避免在資料庫保存明文 token，符合安全不變量 |
| **10. 零機密外洩（Zero Leakage）** | key-vault spec | **通過** | 上游 key、客戶端明文憑證與雜湊值均不進 log、events payload、錯誤訊息或狀態板 |

---

## 1. remote-deploy.sh 與 SSH_ORIGINAL_COMMAND 白名單解析審查

### 1.1 解析機制剖析（`deploy/remote-deploy.sh:10-31`）
```sh
verb="${SSH_ORIGINAL_COMMAND:-}"
nl='
'
case "$verb" in
  *"$nl"*) echo "deploy: command must be a single line" >&2; exit 2 ;;
esac
case "$verb" in
  keys) ;;
  "upgrade "*)
    tag="${verb#upgrade }"
    if ! printf '%s' "$tag" | grep -Eq '^lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'; then
      echo "deploy: invalid tag" >&2
      exit 2
    fi
    installer="${LGW_INSTALLER:-$(dirname "$0")/install.sh}"
    exec "$installer" --tag "$tag" </dev/null
    ;;
  *)
    echo "deploy: unknown or missing command (expected: keys | upgrade lgw-vX.Y.Z)" >&2
    exit 2
    ;;
esac
```

### 1.2 各面向安全檢驗

1. **多行命令（newlines）**：
   - 腳本在開頭使用 `nl` 變數定義實體換行字元，以 `case "$verb" in *"$nl"*) exit 2 ;; esac`（行 13-15）進行攔截。任何夾帶換行符號的輸入皆會在第一時間被拒絕，無法逃逸進入多行腳本執行。
2. **空白字元（whitespace）**：
   - `keys`：使用精確比對 `keys)`。任何前後帶有空白（如 `' keys'`、`'keys '`、`'keys\t'`）之輸入皆不匹配，落入預設分支 `*)` 退出（exit 2）。
   - `upgrade <tag>`：使用 `"upgrade "*` 作為前綴匹配。若為多個空白（如 `upgrade  lgw-v1.0.0`），`${verb#upgrade }` 剝離前綴後會殘留前導空白 `' lgw-v1.0.0'`，後續 regex `^lgw-v...` 即刻判定不合法而拒絕。
   - tag 後帶有尾隨空白（如 `'lgw-v1.0.0 '`）亦會因 regex 結尾 `[0-9]$` 不匹配而被拒絕。
3. **引號（quotes）**：
   - 若攻擊者嘗試送入 `upgrade "lgw-v1.0.0"` 或 `upgrade 'lgw-v1.0.0'`，引號字元無法通過 `^lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$` 驗證，直接 exit 2。
4. **命令替換 `$()` 與反引號（backticks）**：
   - `verb="${SSH_ORIGINAL_COMMAND:-}"` 僅為標準變數賦值，在 POSIX shell 中絕不會對變數內容進行求值（eval）。
   - `case "$verb" in` 僅針對字串做 glob 樣式比對，不執行命令替換。
   - 任何包含 `$()` 或反引號的 tag 皆無法匹配正規式，無法觸發執行。
   - `exec "$installer" --tag "$tag" </dev/null`（行 25）中變數皆以雙引號封裝，杜絕 word splitting 與 glob expansion。
5. **tag 格式規範**：
   - 正規式 `^lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$` 限制版號每一段為 1 到 4 位純數字，最小長度 10 字元（`lgw-v0.0.0`），最大長度 19 字元（`lgw-v9999.9999.9999`）。完全符合 design 決定 2 規定之 `lgw-vX.Y.Z` 命名，且杜絕任何旗標注入（如 `-v`、`--help`）或路徑穿越（如 `../`）。
6. **`exec install.sh` 參數與環境**：
   - 參數固定為 `--tag "$tag"`，重導導向 `</dev/null` 防止子程序非預期讀取 stdin。
   - 執行替換前未讀取任何密鑰或環境，行程直接交棒給 installer。

---

## 2. deploy-service.yml 驗證與機密防護審查

### 2.1 工作流程結構（`.github/workflows/deploy-service.yml:50-68`）
```yaml
      - name: Upgrade service
        env:
          JUMP: ${{ secrets.GH_LLMGATEWAY__POOL_SSH__DEPLOY_JUMP }}
          TARGET: ${{ secrets.GH_LLMGATEWAY__POOL_SSH__DEPLOY_TARGET }}
          TAG: ${{ inputs.tag }}
        run: |
          set -eu -o pipefail
          printf '%s' "$TAG" | grep -Eqx 'lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}' || { echo "invalid tag" >&2; exit 2; }
          target_user_host="${TARGET%:*}"
          target_port="${TARGET##*:}"
          jump_host="${JUMP#*@}"; jump_host="${jump_host%:*}"
          target_host="${target_user_host#*@}"
          ssh -T -n \
              -i ~/.ssh/lgw_deploy -o IdentitiesOnly=yes \
              -o UserKnownHostsFile=~/.ssh/lgw_known_hosts -o StrictHostKeyChecking=yes \
              -o BatchMode=yes -o ConnectTimeout=20 \
              -J "$JUMP" -p "$target_port" "$target_user_host" "upgrade $TAG" \
              2> >(sed -e "s|${jump_host}|[jump]|g; s|${target_host}|[target]|g" >&2)
```

### 2.2 各面向安全檢驗

1. **Tag 輸入注入防禦**：
   - `TAG: ${{ inputs.tag }}` 置於 `env:` 區塊傳入，避免在 `run:` 腳本中直接展開 `${{ inputs.tag }}` 造成的模板注入漏洞。
   - 行 57 使用 `grep -Eqx 'lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}'` 嚴格全行比對。失敗時僅輸出 `echo "invalid tag" >&2`，**不將非法輸入內容回顯至 stderr**，防止惡意輸入污染公開 Actions log。
2. **私鑰與 Known Hosts 安全**：
   - 部署私鑰與 host fingerprint 以 `umask 077` 寫入 `~/.ssh/`（權限 0600，行 40-48）。
   - 在 `always()` 步驟強制 `rm -f ~/.ssh/lgw_deploy ~/.ssh/lgw_known_hosts`（行 69-71），確保短暫 runner 清理。
3. **位址與錯誤遮蔽**：
   - `JUMP` 與 `TARGET` 儲存於 GitHub Secrets，GitHub 預設對 secret 全字串具備自動遮罩。
   - 行 67 透過 `sed` 將 `jump_host` 與 `target_host` 替換為 `[jump]` 與 `[target]`，針對連線錯誤（如 `connect to host <jump_host> port <port>: Connection refused`）提供第二層遮罩，符合 public repo 不外洩池內拓撲的要求。
   - `ssh` 指令加上 `-T`（關閉 pty）與 `-n`（避免吃 stdin），防止非預期的遠端終端回顯。

---

## 3. 管理 API（Admin API）與角色安全架構審查

### 3.1 角色分離（Role Separation）
- **路由與層次檢查（`lgwd/http.py:148-178`）**：
  1. 所有 API 先經 `authenticate(token)` 驗證身分（行 148）。未授權或已撤銷一律 401。
  2. 若為管理 API（`admin_issue` 或 `admin_revoke`）：
     - 檢查是否綁定於 loopback（行 158，非 loopback 直接 403 `forbidden_role`）。
     - 由 `service.admin_issue` 與 `service.admin_revoke` 執行 `_require_live_client(cur, issuer, role="issuer")`。若為一般 `client` 呼叫，丟出 403 `forbidden_role`。
  3. 若為租約相關 API（`leases`、`board`、各項 action）：
     - 行 166 顯式檢查 `elif role != "client": raise forbidden_role()`。`issuer` 角色完全無法調用任何租用動作或檢視狀態板。
  4. 若為 `GET /v1/whoami`：
     - 行 164-165 回傳 `{"client": client, "role": role}`，不分角色皆可查詢，核對身分且不碰租約，與 design 決定 4 完全對齊。

### 3.2 來源介面限制（Loopback Verification）
- **實作方式**：
  `lgwd/http.py:158`：
  ```python
  if self.server.server_address[0] not in _LOOPBACK:
      raise forbidden_role()
  ```
- **分析**：
  1. `self.server.server_address[0]` 代表該 HTTP 伺服器實體監聽（bind）的網路介面位址。
  2. 依據 design 決定 3 與 `lgwd/main.py:184-185`，服務在啟動時會為每個宣告的 host 各自建立一個 `ThreadingHTTPServer` 實體（例如一個綁 `127.0.0.1`，另一個綁 docker bridge `172.17.0.1`）。
  3. 從 docker bridge 進入的連線，其對應的 server 實體之 `server_address[0]` 為 `172.17.0.1`，不在 `_LOOPBACK`（`127.0.0.1`, `::1`）內，直接觸發 403 `forbidden_role`。
  4. 從本機 loopback 進入的連線對應 `127.0.0.1` 伺服器實體，允許通過。

### 3.3 worker-* 名字規範（`lgwd/leasing.py:50, 184, 201`）
- 正規式：`WORKER_NAME = re.compile(r"worker-[A-Za-z0-9._-]{1,57}")`
- 使用 `WORKER_NAME.fullmatch(name)` 驗證。
- 前綴固定為 `worker-`（7 字元），後續字元介於 1 到 57 字元，總長度嚴格落在 8 ~ 64 字元之間。
- 字元集僅允許大小寫字母、數字、點號、底線與連字號，徹底封殺空白、引號、換行及斜線（`/`），杜絕路徑穿越（如 `../`）。

### 3.4 同名重複發證之交易原子性（`lgwd/leasing.py:182-194`）
```python
        with self._store.txn() as cur:
            self._require_live_client(cur, issuer, role="issuer")
            if not WORKER_NAME.fullmatch(name):
                raise name_not_allowed()
            row = self._store.client_get(cur, name)
            if row is not None and row["role"] != "client":
                raise name_not_allowed()
            if row is not None:
                self._revoke_cascade(cur, row, name, now)
            self._put_client(cur, row, name, digest, now, "client")
            self._store.log(cur, "client_issued", now, client=issuer,
                            payload={"name": name, "issuer": issuer, "reissued": row is not None})
        return 201, {"name": name, "token": token}
```
- **原子性驗證**：
  - 整個流程完全包覆在 `with self._store.txn() as cur:`（內部執行 `BEGIN IMMEDIATE`）之中。
  - 若已存在同名舊憑證，在同一筆交易內調用 `_revoke_cascade`：
    - 將舊客戶端標記為 `revoked_at`，記錄 `client_revoked` 事件。
    - 掃描所有該客戶端未結束的租約（`leases_unfinished`），標記為 `status = 'revoked'`，並逐一寫入 `lease_revoked` 事件。
  - 緊接著調用 `_put_client` 更新 `cred_hash`、清除 `revoked_at = NULL`，並記錄 `client_added` 事件。
  - 最後寫入 `client_issued` 事件後統一 commit。
  - 撤舊、撤未完租約、發新憑證與留存審計事件**全成或全敗**，無中途殘留或狀態分裂之可能。

### 3.5 Issuer 權限隔離（撤銷與覆蓋保護）
1. **能否撤銷 Mac 憑證？**
   - 在 `admin_revoke`（`lgwd/leasing.py:201`）中，先檢查 `WORKER_NAME.fullmatch(name)`。Mac 客戶端命名（如 `mac-main`）無法匹配 `worker-*`，直接回傳 403 `name_not_allowed`，不予撤銷。
2. **能否撤銷 Issuer 憑證？**
   - 即使某個 issuer 命名意外匹配 `worker-*`，行 204 亦有檢查：
     `if row is None or row["revoked_at"] is not None or row["role"] != "client": raise not_found()`
     若目標角色的 `role != "client"`，一律回傳 404 `not_found`。
3. **能否同名重發覆蓋 Issuer 憑證？**
   - 在 `admin_issue`（`lgwd/leasing.py:187-188`）中：
     `if row is not None and row["role"] != "client": raise name_not_allowed()`
     若目標名稱已有憑證且為 `issuer`，直接回傳 403 `name_not_allowed`。
- **結論**：issuer 角色被嚴密限制僅能發撤 `role == 'client'` 且名字符合 `worker-*` 的客戶端，無法撼動管理員或其他 issuer。

### 3.6 舊資料庫 schema 遷移（`lgwd/store.py:76-79`）
```python
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(clients)")}
        if "role" not in cols:  # 舊資料庫補欄位
            self._conn.execute("ALTER TABLE clients ADD COLUMN role TEXT NOT NULL DEFAULT 'client'")
            self._conn.commit()
```
- 資料庫連線初始化時，使用 SQLite `PRAGMA table_info` 檢查 `clients` 表是否已具備 `role` 欄位。
- 若無，透過 `ALTER TABLE clients ADD COLUMN role TEXT NOT NULL DEFAULT 'client'` 平滑補上，舊有資料自動帶入 `'client'` 角色，與無 issuer 的舊版本無縫相容。

### 3.7 重啟事件還原（Rebuild from Events）
- **`lgwd/store.py:256-265`**：
  ```python
  if t == "client_added":
      # 雜湊不可逆，記進事件不洩漏憑證；重建時還原身分。
      cur.execute(
          "INSERT INTO clients (name, cred_hash, created_at, revoked_at, role)"
          " VALUES (?, ?, ?, NULL, ?)"
          " ON CONFLICT(name) DO UPDATE SET"
          " cred_hash = excluded.cred_hash, created_at = excluded.created_at,"
          " revoked_at = NULL, role = excluded.role",
          (p["name"], p.get("cred_hash", ""), p["created_at"], p.get("role", "client")),
      )
  ```
- 當服務重啟並自 `events` 重建現況時，`_apply()` 自事件 payload 讀取 `p.get("role", "client")`：
  - 舊事件無 `role` 欄位：預設填入 `'client'`。
  - 新事件具備 `role`（如 `'issuer'`）：正確還原為 `'issuer'`。
  - 單元測試 `test_issuer_survives_restart` 亦證明重構後 issuer 角色不失真。

### 3.8 去重與重送語意（Deduplication & Retry Semantics）
- **現象**：管理 API（`POST /v1/admin/clients`）未帶 `request_id`，未進 `dedup` 表。呼叫端若遇逾時重送，每次皆會生成新 token 並撤銷舊憑證。
- **安全性與設計合理性審核**：
  1. **零明文留存鐵律**：`specs/key-vault/spec.md` 與 design 決定 3/4 規定服務資料庫（含 events 與 dedup 表）**絕不存放客戶端明文憑證**，僅存 SHA-256 雜湊。
  2. 若要在管理 API 實作基於 `request_id` 的冪等去重並回傳既有回覆，資料庫勢必得將明文 token 存入 `dedup` 表 7 天，此舉將嚴重破壞「資料庫無憑證」的安全不變量。
  3. design 決定 4 明確規定：「同名重複發證時，先撤掉舊的（連同它未結束的租約）再回新的，不會同時有兩把有效的。」且決定 6 的 API 規格表（行 129-130）中，管理 API 的請求欄位僅有 `name`，未列 `request_id`。
  4. MyLinuxPool 的 `create-worker` 由系統排程調用，重試時覆蓋掛載目錄之 token 檔即可正常運作。
- **結論**：管理 API 不設 `request_id` 去重、重送即撤舊發新，係基於核心安全架構之深思熟慮設計，**完全符合架構規範且可以接受**。

---

## 4. 零機密外洩（Zero Leakage Audit）審核

對照 `specs/key-vault/spec.md`「Requirement: 值不外洩」進行全路徑查核：

1. **上游 Key**：
   - 僅存在於記憶體 `KeyStore` 與 `keys.env`（0600）。
   - 去重表寫入前一律透過 `_redact(body)`（`lgwd/leasing.py:82-89`）清空 `env` 內容（`{k: ""}`）。
   - 事件 payload、例外回傳、HTTP log 皆無任何 key 值。
2. **客戶端明文憑證（Client Token）**：
   - 僅於 `admin_issue` 成功回覆之 body 及 `lgw.admin client add` 的 stdout 單次輸出。
   - 資料庫 `clients` 表僅存 `cred_hash`（SHA-256 hex）。
   - `events` 記錄中：`client_issued` 事件 payload 僅含 `{"name", "issuer", "reissued"}`，無 token；`client_added` 僅含 `cred_hash`。
   - HTTP 日誌僅記錄 method、path、status，不記 headers（`Authorization`）與 body。
3. **客戶端憑證雜湊（`cred_hash`）**：
   - 不出現在 HTTP 任何 API 的回覆與錯誤訊息中。
   - `lgw.admin client list` 僅輸出 `name\trole\tcreated_at\trevoked_at`，顯式排除雜湊值。
   - HTTP 存取日誌與狀態板 HTML / JSON 均不包含雜湊值。
4. **錯誤訊息與例外處理**：
   - 未預期例外一律於 `lgwd/http.py:183-187` 泛化為 500 `{"error": "internal"}`，log 僅記錄例外類別名稱（`type(exc).__name__`），不記例外詳情與堆疊。

---

## 5. 審查發現與改善建議（必修／建議清單）

本審查共提出 **0 條必修，7 條建議**：

### 【建議 S1】`make_api_server` 預設不支援 IPv6 `::1` 綁定
- **位置**：[`lgwd/http.py:23, 199`](file:///Users/fatesaikou/testAI/LLMGateway/lgwd/http.py#L23-L199)
- **問題**：`_LOOPBACK` 常數包含 `"::1"`（行 23），且行 158 亦檢查 `server_address[0] not in _LOOPBACK`。然而 `ThreadingHTTPServer` 在 Python 標準庫中預設使用 `socket.AF_INET`（IPv4）。若維運端在 `LGWD_API_HOSTS` 配置 `::1`，服務在 `make_api_server` 啟動時會因底層 socket 無法解析/綁定 IPv6 位址而直接噴錯 `socket.gaierror` 或 `OSError: [Errno 97] Address family not supported by protocol`。
- **改善建議**：若 v0 規劃支援 IPv6 loopback，應在 `make_api_server` 中依據傳入的 host 格式（如包含冒號）動態將 server 類別的 `address_family` 設為 `socket.AF_INET6`；若 v0 僅限定 IPv4，建議在 `_LOOPBACK` 與文件加註說明。

### 【建議 S2】監聽 `0.0.0.0` 時管理 API 全面被拒之 fail-closed 行為說明
- **位置**：[`lgwd/http.py:158`](file:///Users/fatesaikou/testAI/LLMGateway/lgwd/http.py#L158), [`tests/test_admin_api.py:132-136`](file:///Users/fatesaikou/testAI/LLMGateway/tests/test_admin_api.py#L132-L136)
- **問題**：`self.server.server_address[0]` 檢查的是伺服器綁定之介面，而非客戶端來源位址 `self.client_address[0]`。當開發或測試者啟動 `lgwd --api-host 0.0.0.0` 時，`server_address[0]` 為 `"0.0.0.0"`，導致即使本機經由 `127.0.0.1` 呼叫管理 API 也會被判為 403 `forbidden_role`（此行為目前在 `test_admin_refused_when_not_bound_to_loopback` 測試鎖住）。
- **改善建議**：雖然 fail-closed 是安全的，但若未來需要以單一伺服器同時服務容器 bridge 與本機 loopback，應同時或改為檢驗連線來源 `self.client_address[0] in _LOOPBACK`。現階段建議在 `deploy/` 或架構說明中載明：正式環境務必分別綁定 `127.0.0.1` 與 docker bridge 介面，勿直接綁 `0.0.0.0`。

### 【建議 S3】`remote-deploy.sh` 執行 installer 前缺少檔案與執行權限預檢
- **位置**：[`deploy/remote-deploy.sh:24-25`](file:///Users/fatesaikou/testAI/LLMGateway/deploy/remote-deploy.sh#L24-L25)
- **問題**：`exec "$installer" --tag "$tag" </dev/null` 在執行前未檢查 `$installer` 是否存在且具備執行權限（`test -x`）。若目前 repo 尚未建立 `deploy/install.sh`，觸發 `upgrade` 時會由 shell 印出底層 `exec: ... not found` 並返回 exit 127。
- **改善建議**：在 `exec` 之前加上檢查：
  ```sh
  if [ ! -x "$installer" ]; then
    echo "deploy: installer not found or not executable" >&2
    exit 2
  fi
  ```
  確保錯誤訊息風格與前段保持一致（exit 2 且訊息格式統一）。

### 【建議 S4】`deploy-service.yml` 若 JUMP 或 TARGET 為空可能引發 sed 語法錯誤
- **位置**：[`.github/workflows/deploy-service.yml:60-67`](file:///Users/fatesaikou/testAI/LLMGateway/.github/workflows/deploy-service.yml#L60-L67)
- **問題**：行 67 使用 `sed -e "s|${jump_host}|[jump]|g; s|${target_host}|[target]|g"` 過濾 stderr。若 secret 未設置導致 `jump_host` 或 `target_host` 為空字串，sed 表達式會退化為 `s||[jump]|g`，引發 sed 報錯 `sed: -e expression #1, char 0: no previous regular expression`。
- **改善建議**：在步驟開頭（行 57 附近）補上前置檢查，如 `test -n "$JUMP" && test -n "$TARGET"`，或在字串替換前驗證 host 非空。

### 【建議 S5】`admin_revoke` 重試時回傳 404 之呼叫端處理指引
- **位置**：[`lgwd/leasing.py:204-205`](file:///Users/fatesaikou/testAI/LLMGateway/lgwd/leasing.py#L204-L205)
- **問題**：`DELETE /v1/admin/clients/{name}` 在客戶端已撤銷時拋出 `not_found()`（404）。若 MyLinuxPool `delete-worker` 因網路逾時進行重試，第一次成功撤銷後，第二次呼叫將獲得 404。
- **改善建議**：此行為完全符合 design 決定 6（「DELETE: 204；不存在回 404」）；建議在與 MyLinuxPool PM（MyLinuxPool#70）交接時明確註明：呼叫端遇到 404 應視為已撤銷完成（幂等成功），避免誤判為異常而引發告警。

### 【建議 S6】Mac 客戶端命名防呆防護
- **位置**：[`lgwd/leasing.py:50, 184, 201`](file:///Users/fatesaikou/testAI/LLMGateway/lgwd/leasing.py#L50-L201), [`lgw/admin.py:68`](file:///Users/fatesaikou/testAI/LLMGateway/lgw/admin.py#L68)
- **問題**：`admin_issue` 與 `admin_revoke` 依賴 `worker-[A-Za-z0-9._-]{1,57}` 限制 issuer 的操作範圍。若管理員在執行 `lgw admin client add` 為 Mac 或手機發證時，不慎命名為 `worker-mac`，該憑證將落入 issuer 可發撤之範圍。
- **改善建議**：在 `lgw admin client add` 或相關操作文件中標明：Mac / 手機憑證嚴禁使用 `worker-` 前綴；或於 `lgw admin client add` 加入防呆提醒。

### 【建議 S7】`deploy-service.yml` 之 stdout 輸出防護建議
- **位置**：[`.github/workflows/deploy-service.yml:62-67`](file:///Users/fatesaikou/testAI/LLMGateway/.github/workflows/deploy-service.yml#L62-L67)
- **問題**：行 67 僅對 stderr（`2>`）透過 sed 進行主機名稱遮罩，stdout 則直接串流至 GHA log。雖然目前 `remote-deploy.sh` 正常情況下不印出主機位址，但若後續 `install.sh` 輸出包含本機或遠端之內部 IP/位址，可能繞過 stderr 遮蔽而進入公開 log。
- **改善建議**：在撰寫後續之 `deploy/install.sh` 時，應遵守相同的 log 規範，stdout 僅輸出進度提示，不輸出主機名稱與 IP 位址。
