# key-vault Specification

## Purpose
把五把上游 key（Claude 個人與公司訂閱、OpenRouter、OpenCode Zen、Ollama Cloud）收在 LLMGateway 一個地方保管；正本是 private repo 的加密檔，服務定期自己拉；客戶端經會合點的 SSH 轉發，由 resolve 回覆的 `env` 拿到（過渡期也可用 get，由 LLMGateway#48 移除），其他地方不再放 key。

> v5（2026-10-12）：`key 只保管在一處`以 v5 全文為準；新增`拉取與輪替`。`值不外洩`、`不做應用層認證`、`服務不對外`不變。第二輪（同日，本人）：拉取改 `gh api`，repo 寫死，`gh` 登入是使用前提。
## Requirements
### Requirement: 拉取與輪替
key-sync SHALL 約每 10 分鐘用 `gh` 從 `FATESAIKOU/LLMGateway-keys` 拉一次 `keys.env.enc`，流程固定為：`gh api repos/FATESAIKOU/LLMGateway-keys/contents/keys.env.enc` 取回 base64 內容與 blob `sha`→比對 `sha`（和上次套用的一樣就不動）→base64 解碼→`openssl enc -d -aes-256-cbc -pbkdf2 -iter 10000 -a -pass env:…` 解密→驗證格式（同 `keys.env` 五個 `KEY=VALUE`）→原子替換 `keys.env`→`lgwd` 重讀。使用前提是服務那台以 `llmgw` 身分登入過 `gh`（LLMGateway 本身不做登入引導）；認證方式由本人選定：`llmgw` 用瀏覽器 `gh auth login` 登入，不用 token 檔（憑證由 `gh` 自己管在該帳號家目錄，LLMGateway 不碰）。`gh` 沒登入、token 失效或讀不到該 repo 時 MUST 記一行 log（不含值）、保留舊版，下次再試。值在過程中 MUST NOT 上命令列、不寫進 log。

#### Scenario: 拉到新版
- **WHEN** 本人更新加密檔並 push（blob `sha` 變了）
- **THEN** 下一次拉取後那台的 key 檔更新，`lgwd` 重讀後 get 回新值

#### Scenario: gh 不能用
- **WHEN** `gh` 沒登入、token 失效或讀不到 `FATESAIKOU/LLMGateway-keys`
- **THEN** key-sync 記一行 log、`keys.env` 不動，服務照用舊 key；`install.sh --check` 看得出「gh 不能用」（以服務帳號身分問，並實際驗一次讀得到那個 repo）

### Requirement: 值不外洩
key 的值 MUST NOT 出現在 repo、服務的資料庫或稽核紀錄、服務的 log、list 的回覆、錯誤訊息。用量取得器（spec `usage-reporting`）讀 key 只在行程內：Ollama 的 key 只放進該次請求的 `Authorization` header，Claude 的 token 只以子行程環境變數 `CLAUDE_CODE_OAUTH_TOKEN` 傳入；兩者都 MUST NOT 進 argv、檔案、log、稽核、例外訊息，也 MUST NOT 出現在 list 與 resolve 新增的 `usage`／`available_again_at` 欄位。取用量新增的外連只到官方 API（直連只有 `ollama.com` 的 `/api/balance`；Claude 的外連由原版 Claude Code 自己發出，Gateway 不另開連線）。值只出現在 resolve 回覆的 `env`（以及過渡期 get 的回覆），以及由 sidecar 開的 agent 的環境。get 是過渡期介面，由 LLMGateway#48 移除。

#### Scenario: 掃描
- **WHEN** 以憑證樣式掃描 repo、服務資料庫、服務 log 與 list 的回覆
- **THEN** 沒有任何命中

#### Scenario: 取用量不外洩 key
- **WHEN** 在 key 檔放可辨識的 canary 值，讓 Ollama 與（假的）Claude 取用量各成功與失敗一次，再以憑證樣式掃描 list 與 resolve 的新增欄位、服務 log、稽核紀錄、SQLite 檔與例外訊息
- **THEN** 沒有任何命中；canary 值只出現在假 HTTP 層收到的 `Authorization` header 與假 Claude 子行程的環境變數 `CLAUDE_CODE_OAUTH_TOKEN`

#### Scenario: 取用量的外連範圍
- **WHEN** 以假 HTTP 層捕捉 lgwd 因取用量發出的所有請求
- **THEN** 只有對 `ollama.com` 的 `/api/balance`

### Requirement: 不做應用層認證
服務 SHALL NOT 要求應用層的憑證；能連到服務的就能 list、resolve、get（過渡期）、登錄冷卻。存取控制只靠 SSH：服務只綁 127.0.0.1，只能經會合點的 SSH 轉發連入（本人選定，`archive/2026-10-24-gateway-v0/design.md` 決定 5a）。發現外洩時，以更新加密檔並等一次拉取因應；客戶端不持有 key，不用改。客戶端裝置（例如手機）遺失時，先由 MyLinuxPool 撤掉那把 sshproxy 金鑰，再更新加密檔等一次拉取。

#### Scenario: 經會合點連入
- **WHEN** 持有會合點 SSH 金鑰的客戶端經轉發呼叫 list
- **THEN** 服務回 200，不要求任何 token

#### Scenario: 換 key
- **WHEN** 本人更新 private repo 的加密檔並等一次拉取
- **THEN** 之後的 get 回新值，客戶端的設定不用改

### Requirement: 服務不對外
服務 SHALL 只綁在服務那台的 127.0.0.1；MUST NOT 綁任何對外或容器網路的介面。客戶端只能經會合機（spec `connectivity`）連入。

#### Scenario: 從其他介面連
- **WHEN** 從服務那台以外、或那台的非 loopback 介面連服務的埠
- **THEN** 連不上

### Requirement: key 只保管在一處
五把 key 的正本 SHALL 是 private repo `FATESAIKOU/LLMGateway-keys` 根目錄的 `keys.env.enc`（格式與加密方式見 `archive/2026-10-24-gateway-v0/design.md` 的 v5 變更；值由本人更新，AI 不經手值）。使用前提是服務那台以 `llmgw` 身分登入過 `gh`（`gh auth login`，憑證在該帳號家目錄；LLMGateway 不做登入引導、不碰憑證值）。LLMGateway SHALL 經由定期拉取（本檔「拉取與輪替」），把解密驗證過的內容寫到放服務那台、只有服務帳號讀得到的檔案（權限 0600）。拉取與解密過程中 MUST NOT 把值放在命令列或寫進 log。

#### Scenario: 部署
- **WHEN** 本人更新加密檔並等一次拉取
- **THEN** 那台的 key 檔更新，服務重讀後 get 回新值，log 裡不出現任何值

#### Scenario: 權限
- **WHEN** 檢查那台的 key 檔
- **THEN** 檔案權限是 0600，擁有者是服務帳號

