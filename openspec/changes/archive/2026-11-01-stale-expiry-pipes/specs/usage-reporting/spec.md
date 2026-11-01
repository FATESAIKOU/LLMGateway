## MODIFIED Requirements

### Requirement: stale 與 6 小時
`stale` SHALL 在 `now - fetched_at` 超過該 subscription 的 TTL 時為 true，否則 false；從沒成功取過為 null。`now - fetched_at` 超過 6 小時 MUST 改回 `status: unknown`、`reason: stale_expired`、`session`／`weekly` 為 null、`pipes` 為 `[]`，不再顯示舊值（`fetched_at` 保留最近一次成功的時刻、`stale` 為 true）。`status: ok` 且 `stale: true` 時，`reason` MUST 是**最近一次取用量失敗的碼**；最近一次沒有失敗（只是過期、重取還沒完成）則為 null。

#### Scenario: 過了 TTL 還沒重取完
- **WHEN** Ollama 的快取 `fetched_at` 在 3 分鐘前（TTL 120 秒），重取還沒完成
- **THEN** 回舊值，`status: ok`、`stale: true`、`reason: null`，`fetched_at` 照實

#### Scenario: 過期後重取失敗
- **WHEN** 同上，但最近一次重取失敗，碼為 `upstream_error`
- **THEN** 回舊值，`status: ok`、`stale: true`、`reason: upstream_error`

#### Scenario: 超過 6 小時
- **WHEN** 最近一次成功是 6 小時又 1 分鐘前
- **THEN** `status: unknown`、`reason: stale_expired`、`stale: true`，`session`／`weekly` 為 null，`fetched_at` 仍是那次成功的時刻

#### Scenario: 超過 6 小時 pipes 一併消失
- **WHEN** 最近一次成功是 6 小時又 1 秒前，快取帶有 `credits` 管子（`resets_at` 為 null）
- **THEN** `status: unknown`、`reason: stale_expired`、`stale: true`，`session`／`weekly` 為 null，`pipes` 為 `[]`（含 `resets_at` 為 null 的 credits 管子也一起消失），`fetched_at` 仍是那次成功的時刻

#### Scenario: 剛好沒過期
- **WHEN** `now - fetched_at` 小於 TTL
- **THEN** `stale: false`，`reason: null`
