## Purpose

讓本人與 PMO 一眼看到五把 key 現在的狀態：誰在用、用了幾個、有沒有在冷卻、冷卻到何時，以及拿得到時的用量。資料來自 LLMGateway 自己的紀錄與上游的公開查詢介面。

## ADDED Requirements

### Requirement: 每把 key 的必備欄位
狀態板 SHALL 對五把 key 各列出：
- 使用中的租約，每張含 worker、角色、coding agent、開始時刻；
- 使用中的數量與同時租用上限；
- 是否在冷卻；
- 冷卻結束時刻。

這些欄位 MUST 只由 LLMGateway 自己的租約與冷卻紀錄算出。

#### Scenario: 有人在用
- **WHEN** `claude-personal` 有兩張 active 租約
- **THEN** 狀態板列出這兩張的 worker、角色、coding agent、開始時刻，數量顯示 2／2

#### Scenario: 冷卻中
- **WHEN** `claude-personal` 被回報額度用完、冷卻到 15:47
- **THEN** 狀態板顯示冷卻中、結束時刻 15:47；15:47 之後顯示不在冷卻

#### Scenario: 未部署
- **WHEN** 服務的 key 檔裡沒有某一把的 key
- **THEN** 狀態板那一列顯示「未部署」

#### Scenario: 逾期的租約不算
- **WHEN** 某張租約已過有效期限而沒有續期
- **THEN** 它不出現在使用中的清單，也不算進數量

### Requirement: 用量
狀態板 SHALL 在拿得到時顯示各 key 的用量，拿不到時顯示「未知」：
- OpenRouter：服務定期查 `GET /api/v1/key`；
- Ollama Cloud：服務定期查 `GET /api/balance`，頻率不超過官方上限；
- Claude 兩把與 OpenCode Zen：v0 顯示「未知」。

用量查詢失敗 MUST NOT 影響租用功能。

#### Scenario: 拿得到
- **WHEN** 最近一次 `GET /api/v1/key` 成功
- **THEN** OpenRouter 一列顯示查到的用量與查詢時刻

#### Scenario: 拿不到
- **WHEN** 某把 key 沒有可用的查詢介面，或最近一次查詢失敗
- **THEN** 該列的用量顯示「未知」（查詢失敗時附最後一次成功的時刻），租用照常

### Requirement: 唯讀且不含值
狀態板 SHALL 提供兩種形式：
- JSON：要客戶端憑證；
- 唯讀的 HTML：只綁在 provider 的 loopback，不要客戶端憑證，從 Mac 經 SSH 埠轉送開。

兩者都 MUST NOT 提供任何改變狀態的操作，MUST NOT 顯示 key 的值；客戶端憑證 MUST NOT 出現在 URL。

#### Scenario: 讀取 JSON
- **WHEN** 持有客戶端憑證的客戶端讀取 JSON 狀態板
- **THEN** 回傳五把 key 的狀態，內容不含任何 key 的值

#### Scenario: HTML 只在 loopback
- **WHEN** 從 worker 容器看得到的介面請求 HTML 狀態板
- **THEN** 連不上；從 provider 的 loopback（或經 SSH 埠轉送）請求才拿得到
