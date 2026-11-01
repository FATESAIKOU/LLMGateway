## MODIFIED Requirements

### Requirement: 被查到且過期才取，不等結果
取用量 SHALL 只在 list 或 resolve 組裝某個 subscription 的 `usage` 時觸發，條件是：該 provider 有取用量實作、已啟用（見「政策檔的 usage 設定」）、key 已部署，而且快取不存在或 `now - fetched_at` 超過 TTL，而且不在退避期間內。觸發只是叫一條取用量執行緒去做，這次 list／resolve 的回覆 MUST NOT 等它：有舊值回舊值（`stale: true`），從沒取過回 `unknown`／`not_fetched_yet`；新值取完寫進快取，下一次查詢才看到。MUST NOT 有定時排程或啟動時預取。冷卻登錄 MUST NOT 觸發取用量。resolve 回非 200（409 `cooling`、409 `no_candidate`、409 `not_deployed`、404、400 `bad_request`／`no_model`、403 `agent_not_allowed`／`model_not_allowed`）時 MUST NOT 組裝 `meta.usage`，也 MUST NOT 觸發取用量。

**原因碼的優先順序**：已被政策關閉（`enabled: false`）→ `disabled`（優先，不看 key）；其次 key 沒部署 → `no_key`。key 沒部署時，即使快取裡有舊值，`usage` 也 MUST 回 `unknown`／`no_key`、窗口為 null，不回舊值（key 已不在，舊值不再可信），且不觸發；key 重新部署後，快取沿用既有規則（未過期則直接顯示）。

#### Scenario: 快取過期的第一次查詢
- **WHEN** 快取不存在，呼叫 list，假取用量卡住不返回
- **THEN** list 立刻回 200，該筆 `usage` 是 `unknown`／`not_fetched_yet`，假取用量被呼叫一次

#### Scenario: 下一次查詢才看到新值
- **WHEN** 上一個 Scenario 的取用量完成並寫入快取，再呼叫 list
- **THEN** 該筆 `usage` 是 `status: ok`；且這一次因為快取未過期，不再觸發

#### Scenario: 快取還新鮮
- **WHEN** `now - fetched_at` 小於 TTL 時 list 或 resolve
- **THEN** 不觸發取用量

#### Scenario: 沒人查就不取
- **WHEN** lgwd 啟動後沒有任何 list 或 resolve，經過數個 TTL
- **THEN** 取用量實作一次都沒被呼叫

#### Scenario: resolve 成功時帶 usage 並觸發
- **WHEN** resolve 回 200 選中 `ollama-cloud`，快取過期
- **THEN** `meta.usage` 與 list 同形狀（只讀快取），並觸發一次背景取用量，回覆不等它

#### Scenario: resolve 非 200 不觸發
- **WHEN** resolve 分別回 409 `cooling`、409 `no_candidate`、409 `not_deployed`、404 `not_found`、400 `bad_request`、400 `no_model`、403 `agent_not_allowed` 或 403 `model_not_allowed`，且相關 subscription 的快取過期
- **THEN** 回覆不含 `usage`，取用量實作沒有被呼叫

#### Scenario: get 與冷卻登錄不觸發
- **WHEN** 登錄冷卻，快取過期
- **THEN** 取用量實作沒有被呼叫

#### Scenario: key 沒部署不觸發
- **WHEN** `ollama-cloud` 的 key 沒部署，呼叫 list
- **THEN** `usage` 是 `unknown`／`no_key`，取用量實作沒有被呼叫

#### Scenario: key 沒部署但快取有舊值
- **WHEN** `ollama-cloud` 的快取有 1 分鐘前的成功值，之後 key 檔被換成不含該 key 的版本，呼叫 list
- **THEN** `usage` 是 `unknown`／`no_key`，`session`／`weekly` 為 null，不回舊值，也不觸發取用量

#### Scenario: disabled 優先於 no_key
- **WHEN** 政策檔把 `ollama-cloud` 設為 `enabled: false`，而且它的 key 沒部署
- **THEN** `usage.reason` 是 `disabled`（不是 `no_key`）
