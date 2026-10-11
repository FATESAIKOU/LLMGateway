v3（2026-10-11 夜）。本人確認 v3 之前不動程式。

## 1. 服務改成三個介面（本 repo；假 key、假時鐘）

- [ ] 1.1 `GET /v1/subscriptions`（list，不含 key，依 priority 排序）、`GET /v1/subscriptions/{id}?agent=`（get，含 key；agent 不符 403、未部署 409）、`POST /v1/subscriptions/{id}/cooldown`（5h／1w、resets_at＋2 分鐘、預設長度、不合理時刻 400、取較晚、自動解除）
- [ ] 1.2 共通 JSON schema（design 決定 7）；內部 `CredentialProvider`（四種 provider）與政策檔（priority、models、agents）
- [ ] 1.3 認證：key 檔裡各類機器的讀取 token，讀進來只存雜湊；重讀即換
- [ ] 1.4 SQLite 只剩 `cooldowns` 與 `audit`；稽核紀錄不含 key
- [ ] 1.5 移除 impl branch 上的租約核心、管理 API、whoami、狀態板、用量查詢與相關測試
- [ ] 1.6 測試：spec 每個服務端 Scenario；canary key 不出現在 list、資料庫、log

## 2. `lgw` 客戶端

- [ ] 2.1 list／get／登錄冷卻的客戶端（標準函式庫；連不上回「無法確認」，不丟例外）
- [x] 2.2 訊號規則（只有帶額度標記的 402／429 才算用完），給 sidecar 的 AgentDriver 當函式庫用
- [ ] 2.3 以 git tag `lgw-vX.Y.Z` 發版

## 3. 部署（LLMGateway 自己的帳號；MyLinuxPool 只管連線與 worker 的掛載）

- [x] 3.1 本人選定放服務的那台；LLMGateway 是獨立服務（MyLinuxPool#67 作廢、PR #72 關閉）
- [x] 3.1a `deploy/`：安裝程式（固定 tag、current 原子切換、systemd user unit、`--check` 三態）與 `remote-deploy.sh`（`keys`、`upgrade <tag>`）；已用假資料測試
- [ ] 3.1b `deploy-keys`：stdin 多帶各類機器的讀取 token；`remote-deploy.sh` 的允許鍵名加上 token
- [ ] 3.1c MyLinuxPool：#68 跳板機上的轉發條目＋host key 指紋；#69 worker 連服務的路；#70 改成把 `pool-worker` 的 token 像 `POOL_WORKER__*` 一樣掛進 worker
- [ ] 3.1d 一次性開機設定（root，本人做或本人授權）：建 `llmgw`（不加 docker 群組）、linger、確認 sshd 不擋、放部署公鑰、第一次安裝
- [ ] 3.3 本人設定（AI 不經手值）：
  - environment「llmgateway-deploy」，required reviewer＝本人；
  - 部署 4 個 secret：`GH_LLMGATEWAY__POOL_SSH__DEPLOY`、`…_KNOWN_HOSTS`、`…_JUMP`、`…_TARGET`（`llmgw@127.0.0.1:<埠>`）；
  - key 5 個 secret：`POOL_LLMGATEWAY__CLAUDE_PERSONAL__LLM`、`POOL_LLMGATEWAY__CLAUDE_COMPANY__LLM`、`POOL_LLMGATEWAY__OPENROUTER_API__LLM`、`POOL_LLMGATEWAY__OPENCODE_ZEN__LLM`、`POOL_LLMGATEWAY__OLLAMA_CLOUD__LLM`；
  - 讀取 token 的 secret（名字暫定，請本人照命名規則定）：`POOL_WORKER__LLMGATEWAY__USE`（同一個值也設在 MyLinuxPool）、`MAC_LOCAL__LLMGATEWAY__USE`。

## 4. 驗收

- [ ] 4.1 Mac：sidecar 以 list／get 開 Claude Code，回得了一則最短的訊息
- [ ] 4.2 換把：用假的額度用完訊號 → sidecar 登錄冷卻 → list 顯示 `claude-personal` 不能用 → sidecar 挑到 `claude-company` 重開並接回；冷卻到期後 list 恢復
- [ ] 4.3 opencode 拿不到 Claude 訂閱（403）；opencode 對 Zen、OpenRouter（免費模型）、Ollama 各跑通一次
- [ ] 4.4 要用 key 的驗證（PM 或本人）：OpenRouter、Ollama Cloud 讓 Claude Code 跑一則最短的訊息；opencode 讀 key 的方式
- [ ] 4.5 worker：在一台 worker 驗收換把
- [ ] 4.6 秘密掃描：repo、資料庫、log、list 的回覆沒有任何 key 的值
- [ ] 4.7 退回演練

## 5. 銜接

- [ ] 5.1 把共通 JSON schema 與三個介面的最後版本交給 aistorage-pm（已在 v3 對齊草案）
- [x] 5.2 手機的轉送：MyAiEntry#147
- [ ] 5.3 另開單：把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key
- [ ] 5.4 通知 MyAiEntry PM：手機改用讀取 token＋list／get（不再有租約）
