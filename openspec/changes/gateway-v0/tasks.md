## 1. 租用服務（本 repo；假 key、假上游、假時鐘）

- [ ] 1.1 專案骨架：`server/`（服務）、`lgw/`（API 客戶端、訊號規則、管理命令）、`policy/default.json`（內容照設計決定 8）、`client/opencode.json`；只用 Python 標準函式庫；測試用 `unittest`；「現在」經可替換的 clock 介面
- [ ] 1.2 SQLite：`events`（只追加、單調序號）、`leases`、`cooldowns`、`clients`、去重表；每個改變狀態的動作一筆 `BEGIN IMMEDIATE` 交易；重啟時從 `events` 重建
- [ ] 1.3 客戶端憑證：`lgw admin client add|rm|list`（rm 等同 revoke）、`GET /v1/whoami`；32 bytes 隨機值，只存 SHA-256；撤銷時一併 revoke 該客戶端未結束的租約；worker 名稱與客戶端相符的檢查
- [ ] 1.4 `POST /v1/leases`：
  - 欄位與長度檢查（`instance` 必填）；`instance` 唯一；`preferences`、`usage` 的格式檢查；
  - 依政策選定（順序、冷卻、上限、模型允許清單）；
  - 判定順序：`400` → `401` → `403` → `404` → `409`；`409` 之間 `not_allowed` → `instance_busy` → `cap_reached`／`all_cooling`；`earliest_reset` 只在 `all_cooling` 附上；被拒時記 `denied` 事件；
  - 回租約物件；`preferences` 只記錄。
- [ ] 1.5 `POST /v1/leases/{id}/start`：granted → active（效期從此起算；已 active 重送回 200）；10 分鐘未確認的記為 expired；未確認的租約只接受 start、release（其他回 `lease_not_confirmed`）
- [ ] 1.6 `POST /v1/leases/{id}/exhausted`：同一筆交易內設冷卻（重置時刻＋2 分鐘；不合理或沒有時用預設）、原租約 superseded、給下一張或拒絕
- [ ] 1.7 `renew`、`release`（重複也回 204）、`usage`；延後結算的逾期（任何經認證的請求都先結算）；（客戶端，`request_id`）去重存 7 天、跨重啟有效；錯誤碼照設計決定 6
- [ ] 1.8 key 檔：讀取、SIGHUP 重讀（失敗沿用舊的、空字串視同沒部署）；值不進事件、log、錯誤訊息
- [ ] 1.9 env 對照表（設計決定 7）：Claude 兩把只給 `claude-code`；Zen 只給 `opencode`；`unset` 清單；`claude-code` 的順序不含 `openrouter`、`ollama-cloud`（測試鎖住）
- [ ] 1.10 狀態板：
  - `GET /v1/board`（要憑證）；`/board` HTML 只綁 loopback、不要憑證、輸出跳脫；
  - 顯示未部署、key 無效、未知與最後成功時刻；
  - OpenRouter `/api/v1/key` 與 Ollama `/api/balance` 的定期查詢：10 秒逾時、獨立執行緒、可關、失敗不影響租用。
- [ ] 1.11 政策檔驗證（不合法就不套用）；`events` 超過 180 天每月匯出壓縮檔再刪除
- [ ] 1.12 測試：
  - spec 中**服務端**的每個 Scenario 都有測試（launcher 端的由 2.4 交接與 4.5 驗收）；
  - `exhausted` 併發與中途當掉的一致性；
  - 冷卻邊界（等於結束時刻即結束）；
  - canary 值掃描事件、log、狀態板輸出。

## 2. `lgw`（API 客戶端＋訊號規則；換把程序在 sidecar）

- [ ] 2.1 API 客戶端：base URL 與客戶端憑證路徑可設定；連不上時回「無法確認」，不丟例外；不認得的拒絕理由當一般拒絕
- [ ] 2.2 訊號規則（設計決定 9），用資料表、帶 `signal`：
  - Claude：`You've hit your … limit · resets …`；
  - Zen：`FreeUsageLimitError`／`GoUsageLimitError`／`CreditLimitExceeded`（訊息標記優先於狀態碼）；
  - OpenRouter：402 且 `limit_source` 不是 in-flight；
  - Ollama：429 且含 session／weekly usage limit；
  - Claude 的 spend limit 類訊息判為 `claude_limit`、不帶重置時刻；
  - **沒有額度標記的 402／429 判為暫時錯誤**；
  - 正反兩面都用 `quota-signals.md` 的樣本測試。
- [ ] 2.3 打包與版本：以 git tag `lgw-vX.Y.Z` 發版，可從 tag 安裝，標準函式庫以外沒有依賴；`client/opencode.json` 隨套件提供路徑；測試鎖住 `lgw` 不引用 `subprocess`、herdr、agora，也沒有依平台分支（`sys.platform`、`darwin`）
- [ ] 2.4 和 aistorage-pm 交接（MyAiStorage 那邊實作）：
  - sidecar 的換把程序改用 `lgw`：開 agent 帶 env、`--settings` 關掉 autoContinue、開起後呼叫 `start`、判讀、安全結束、`exhausted`、接回；
  - session id 寫法照 sidecar 慣例；
  - 通知失敗留在本機重試、補送恰一則；
  - 冷卻結束不主動重開；
  - `sidecar agent run claude|opencode` 作為 Mac 的入口，沒有 Mac 專用分支，同一套測試參數化兩種模式。
  - **Mac 的驗收（4.1～4.3）blocked by 這一項。**
- [ ] 2.5 測試：`lgw` 的 API 客戶端對假服務的每種回覆（核准、各種拒絕、各種錯誤、連不上）

## 3. 部署（需要 MyLinuxPool 的工單）

- [x] 3.1 開 MyLinuxPool 的單（mylinuxpool-pm 已開，都 blocked by #8）：
  - MyLinuxPool#67：服務的 shared-config 單元；
  - MyLinuxPool#68：部署專用的受限金鑰；
  - MyLinuxPool#69：worker 連到服務的路；
  - MyLinuxPool#70：worker 客戶端憑證的發、撤、核對。
- [ ] 3.1a 本人選服務放哪一台常駐 provider（MyLinuxPool#67）
- [ ] 3.2（草稿已寫，branch `impl/gateway-v0-core`）`deploy-keys` workflow：手動觸發、用部署專用金鑰經跳板（只能轉發）到 provider（只能跑部署腳本）、以 stdin 傳值；log 只有 secret 名稱
- [ ] 3.3 本人設定（PMO 帶本人做，AI 不經手值）：
  - environment「llmgateway-deploy」，required reviewer 設本人；
  - secret 的名字照本人的命名規則 `<存取方>__<被存取方>__<動作>`：
    - 部署 4 個：`GH_LLMGATEWAY__POOL_SSH__DEPLOY`（部署專用私鑰）、`GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS`（用 MyLinuxPool 提供的指紋，不用 ssh-keyscan）、`GH_LLMGATEWAY__POOL_SSH__DEPLOY_JUMP`、`GH_LLMGATEWAY__POOL_SSH__DEPLOY_TARGET`（`user@127.0.0.1:<埠>`，和 permitopen 字面一致）；
    - key 5 個：`POOL_LLMGATEWAY__CLAUDE_PERSONAL__LLM`、`POOL_LLMGATEWAY__CLAUDE_COMPANY__LLM`、`POOL_LLMGATEWAY__OPENROUTER_API__LLM`、`POOL_LLMGATEWAY__OPENCODE_ZEN__LLM`、`POOL_LLMGATEWAY__OLLAMA_CLOUD__LLM`；
  - 之後跑 `deploy-keys`。
- [ ] 3.4 發客戶端憑證：Mac 一把由 PM 在 provider 上發；worker 的由 create-worker 自動發（MyLinuxPool#70）
- [ ] 3.5 綁定驗收：從池外與 worker 介面連 HTML 狀態板連不上；從池外連 API 連不上

## 4. 驗收

- [ ] 4.1 Mac：`sidecar agent run claude` 開出的 Claude Code 回得了一則最短的訊息；事件（含 start、release）齊全；agent 環境只有那一把
- [ ] 4.2 Mac：用假的額度用完訊號換把（`claude-personal` → `claude-company`），自動重開並接回；冷卻到期後新開的回到 `claude-personal`；短暫 429 不換把
- [ ] 4.3 Mac：`sidecar agent run opencode` 對 Zen、OpenRouter（免費模型）、Ollama Cloud 各跑通一次
- [ ] 4.4 要用 key 的驗證（PM 或本人）：OpenRouter、Ollama Cloud 讓 Claude Code 跑一則最短的訊息；Ollama `GET /api/balance` 回哪一種欄位；opencode 讀 key 的變數名
- [ ] 4.5 sidecar：aistorage-pm 把假 broker 換成 `lgw`；在一台 worker 驗收換把；在 worker 的 herdr 0.9.3 驗 `tab create --env` 不留值（不過就改用 0600 暫存 env 檔、讀完即刪）
- [ ] 4.6 狀態板：五把的使用者、數量／上限、冷卻、部署狀態、用量（拿不到的顯示「未知」）；URL 與 log 裡沒有客戶端憑證
- [ ] 4.7 秘密掃描：repo、事件、log、狀態板、匯出檔沒有任何 key 的值
- [ ] 4.8 退回演練：停服務、Mac 改回原本的指令

## 5. 銜接

- [ ] 5.1 把最後的欄位與錯誤碼交給 aistorage-pm（設計決定 6）
- [x] 5.2 請 MyAiEntry 開「手機 App 經 SSH 跳板埠轉送到租用服務」的單（本人選 (2)）→ MyAiEntry#147
- [ ] 5.3 另開單：把 MyLinuxPool#50 的 key 來源換成租用，收掉 MyLinuxPool 裡的 key
- [ ] 5.4 租約欄位定案後通知 MyAiEntry PM；決定給 App 的 agent 種類值
