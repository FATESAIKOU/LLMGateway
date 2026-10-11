## 1. 租用服務（本 repo，假 key、假上游）

- [ ] 1.1 專案骨架：`server/`（服務）、`lgw/`（客戶端模組）、`policy/default.json`、`client/opencode.json`；只用 Python 標準函式庫；測試用 `unittest`
- [ ] 1.2 SQLite：`events`（只追加、單調序號）、`leases`、`cooldowns`、`clients`；重啟時從 `events` 重建
- [ ] 1.3 客戶端憑證：`lgw admin client add|revoke|list`，只存雜湊；請求認身分；worker 名稱與客戶端相符的檢查
- [ ] 1.4 `POST /v1/leases`：欄位檢查、依政策選定（順序、冷卻、上限）、回 `env`／`unset`、`preferences` 只記錄；拒絕的理由與 `earliest_reset`
- [ ] 1.5 `POST /v1/leases/{id}/exhausted`：設冷卻（重置時刻＋2 分鐘，或政策的預設）、原租約 superseded、同一次回覆給下一張或拒絕
- [ ] 1.6 `renew`、`release`、`usage`；延後結算的逾期；`request_id` 去重
- [ ] 1.7 key 檔讀取與 SIGHUP 重讀；值不進事件、log、錯誤訊息
- [ ] 1.8 env 對照表（設計決定 7）：Claude 兩把只給 `claude-code`；Zen 只給 `opencode`；`unset` 清單
- [ ] 1.9 狀態板：`GET /v1/board`（JSON）與 `/board`（唯讀 HTML）；OpenRouter `/api/v1/key` 與 Ollama `/api/balance` 的定期查詢（可關、失敗不影響租用）；Claude 與 Zen 顯示「未知」
- [ ] 1.10 測試：spec 的每個 Scenario 都有對應測試；憑證樣式掃描事件、log、狀態板輸出

## 2. 客戶端模組 `lgw`（API 客戶端＋訊號規則；換把程序在 sidecar）

- [ ] 2.1 API 客戶端：base URL 與客戶端憑證路徑可設定；連不上時回「無法確認」，不丟例外
- [ ] 2.2 訊號判讀：規則用資料表（Claude Code 的 `You've hit your … limit · resets …`、opencode 的 `Free usage exceeded`、`usage limit reached`、402／429）；解析重置時刻；用 `quota-signals.md` 的樣本寫測試
- [ ] 2.3 把 `lgw` 打包成可從固定 ref 安裝（給 sidecar 的 venv 與 Mac），標準函式庫以外沒有依賴
- [ ] 2.4 和 aistorage-pm 交接：sidecar 的換把程序（開 agent 帶 env、`--settings` 關掉 autoContinue、判讀、安全結束、`exhausted`、接回）改用 `lgw`；`sidecar agent run claude|opencode` 作為 Mac 的入口，沒有 Mac 專用分支（MyAiStorage 那邊實作）
- [ ] 2.5 測試：`lgw` 的 API 客戶端對假服務的每種回覆（核准、各種拒絕、連不上）；訊號規則用 `quota-signals.md` 的樣本

## 3. 部署（需要 MyLinuxPool 的工單）

- [ ] 3.1 開 MyLinuxPool 的單：服務的 shared-config 單元、本 repo Actions 的 client 身分、worker 容器連到服務的路、客戶端憑證放進 `/run/mlp-secrets/`
- [ ] 3.2 `deploy-keys` workflow：手動觸發、經跳板寫 key 檔（stdin 傳值）、送 SIGHUP；log 只有 secret 名稱
- [ ] 3.3 本人設五把 secret（PMO 帶本人設，AI 不經手值）→ 跑 `deploy-keys`
- [ ] 3.4 在 provider 上發客戶端憑證：Mac 一把、每台 worker 一把

## 4. 驗收

- [ ] 4.1 Mac：`sidecar agent run claude` 開出的 Claude Code 回得了一則最短的訊息；事件與 release 齊全；agent 環境只有那一把
- [ ] 4.2 Mac：用假的額度用完訊號換把（`claude-personal` → `claude-company`），自動重開並接回；冷卻到期後新開的回到 `claude-personal`
- [ ] 4.3 Mac：`sidecar agent run opencode` 對 Zen、OpenRouter（免費模型）、Ollama Cloud 各跑通一次
- [ ] 4.4 要用 key 的驗證（PM 或本人）：OpenRouter、Ollama Cloud 讓 Claude Code 跑一則最短的訊息；Ollama `GET /api/balance` 回哪一種欄位；opencode 讀 key 的變數名
- [ ] 4.5 sidecar：aistorage-pm 把假 broker 換成 `lgw` 的 API 客戶端；在一台 worker 驗收換把；在 worker 的 herdr 0.9.3 驗 `tab create --env` 不留值
- [ ] 4.6 狀態板：五把的使用者、數量／上限、冷卻、用量（拿不到的顯示「未知」）
- [ ] 4.7 秘密掃描：repo、事件、log、狀態板沒有任何 key 的值
- [ ] 4.8 退回演練：停服務、Mac 改回原本的指令

## 5. 銜接

- [ ] 5.1 把最後的欄位與錯誤碼交給 aistorage-pm（設計決定 6）
- [x] 5.2 請 MyAiEntry 開「手機 App 經 SSH 跳板埠轉送到租用服務」的單（本人選 (2)）→ MyAiEntry#147
- [ ] 5.4 租約欄位定案後通知 MyAiEntry PM；決定給 App 的 agent 種類值
- [ ] 5.3 另開單：把 MyLinuxPool#50 的 key 來源換成租用，收掉 MyLinuxPool 裡的 key
