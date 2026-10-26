## MODIFIED Requirements

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
