v4（2026-10-11 深夜）。本人確認 v4 之前不動程式。

## 1. 服務改成三個介面（本 repo；假 key、假時鐘）

- [ ] 1.1 `GET /v1/subscriptions`（list，不含 key，依 priority 排序）、`GET /v1/subscriptions/{id}?agent=`（get，含 key；agent 不符 403、未部署 409）、`POST /v1/subscriptions/{id}/cooldown`（5h／1w、resets_at＋2 分鐘、預設長度、不合理時刻 400、取較晚、自動解除）
- [ ] 1.2 共通 JSON schema（design 決定 7）；內部 `CredentialProvider`（四種 provider）與政策檔（priority、models、agents）
- [ ] 1.3 認證（依 3.2 的選擇）
- [ ] 1.4 SQLite 只剩 `cooldowns` 與 `audit`；稽核紀錄不含 key
- [ ] 1.5 移除 impl branch 上的租約核心、管理 API、whoami、狀態板、用量查詢與相關測試
- [ ] 1.6 測試：spec 每個服務端 Scenario；canary key 不出現在 list、資料庫、log

## 2. `lgw` 客戶端

- [ ] 2.1 list／get／登錄冷卻的客戶端（標準函式庫；連不上回「無法確認」，不丟例外）
- [x] 2.2 訊號規則（只有帶額度標記的 402／429 才算用完），給 sidecar 的 AgentDriver 當函式庫用
- [ ] 2.3 以 git tag `lgw-vX.Y.Z` 發版

## 3. 連線與部署（只依賴會合機＋SSH）

- [x] 3.1 本人選定放服務的那台；LLMGateway 是獨立服務
- [x] 3.1a `deploy/`：安裝程式與 `remote-deploy.sh`（`keys`、`upgrade <tag>`）；已用假資料測試
- [ ] 3.1b 隧道守護 `llmgateway-tunnel.service`：自己的 `ssh -N -R`（API 埠、SSH 埠），`ExitOnForwardFailure`／`ServerAlive*`；端對端探針，連續 3 次失敗就重建；設定檔 `tunnel.env`；`--check` 加上隧道
- [ ] 3.1c `lgw connect`（函式庫＋指令）：`ssh -N -L … [-J …] llmgw-relay@<會合機>`，回 base URL；連不上回「無法確認」；設定檔 `connect.env`
- [ ] 3.1d workflow 改走會合機（`ssh -J llmgw-relay@<會合機> -p <SSH 埠> llmgw@127.0.0.1`）
- [ ] 3.1e lgwd 只綁 127.0.0.1（拿掉 docker bridge 綁定）
- [ ] 3.2 認證：依本人選的 (a)／(b) 實作；選 (b) 時 `deploy-keys` 多帶 token、服務存雜湊
- [ ] 3.3 一次性開機設定（root，本人做或本人授權）：
  - 會合機：建 `llmgw-relay`，放三種金鑰（隧道、客戶端、部署；各自的 permitlisten／permitopen），確認 sshd 不擋；
  - 服務那台：建 `llmgw`（不加 docker 群組）、linger、放部署公鑰、第一次安裝。
- [ ] 3.4 本人設定 environment「llmgateway-deploy」（required reviewer＝本人）與 secret；secret 名稱等認證方式定了再定

## 4. 驗收

- [ ] 4.1 Mac：sidecar 以 list／get 開 Claude Code，回得了一則最短的訊息
- [ ] 4.2 換把：用假的額度用完訊號 → sidecar 登錄冷卻 → list 顯示 `claude-personal` 不能用 → sidecar 挑到 `claude-company` 重開並接回；冷卻到期後 list 恢復
- [ ] 4.3 opencode 拿不到 Claude 訂閱（403）；opencode 對 Zen、OpenRouter（免費模型）、Ollama 各跑通一次
- [ ] 4.4 要用 key 的驗證（PM 或本人）：OpenRouter、Ollama Cloud 讓 Claude Code 跑一則最短的訊息；opencode 讀 key 的方式
- [ ] 4.5 worker：在一台 worker 經 `lgw connect` 連線並驗收換把
- [ ] 4.5a 隧道：殺掉隧道連線後自動恢復；佔住遠端埠時守護程式重試而不假裝健康
- [ ] 4.6 秘密掃描：repo、資料庫、log、list 的回覆沒有任何 key 的值
- [ ] 4.7 退回演練

## 5. 銜接

- [ ] 5.1 把共通 JSON schema 與三個介面的最後版本交給 aistorage-pm（已在 v3 對齊草案）
- [x] 5.2 手機的轉送：MyAiEntry#147
- [ ] 5.3 另開單：把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key
- [ ] 5.4 通知 MyAiEntry PM：手機改用讀取 token＋list／get（不再有租約）
