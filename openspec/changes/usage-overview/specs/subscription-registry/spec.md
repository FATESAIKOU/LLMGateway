## MODIFIED Requirements

### Requirement: list
`GET /v1/subscriptions` SHALL 回所有 subscription 的共通 JSON，依 `priority` 排序，**不含 `key`**；每一筆多帶 `usage` 與 `available_again_at`（形狀與規則見 spec `usage-reporting`）；每一筆另帶 `model_cooldowns`（該把各模型的冷卻，只列還沒過的；形狀見「共通 JSON 的 model_cooldowns」）。list 只讀用量快取，並在快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 list MUST 仍回 200。

#### Scenario: 不含秘密
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都沒有 `key` 欄位，回覆裡找不到任何 key 的值

#### Scenario: 冷卻反映在 list
- **WHEN** `claude-personal` 冷卻到 15:00（`cooldown.until` 為 15:00）
- **THEN** 15:00 之前 list 裡它的 `available` 為 false；15:00 之後 `available` 回到 true（這是給人看的粗提示；resolve 不會因此拒絕）

#### Scenario: 每一筆都有 usage 與 available_again_at
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都有 `usage`（九個鍵齊全）與 `available_again_at`（RFC 3339 UTC 或 null）；沒有用量來源的 provider 是 `usage.status: not_applicable`

#### Scenario: 用量壞了 list 照回
- **WHEN** 取用量實作拋例外或卡住
- **THEN** list 仍回 200，其他欄位（`cooldown`、`available`、`model_cooldowns` 等）不變，該筆 `usage` 為 `unknown`

#### Scenario: 模型冷卻反映在 list
- **WHEN** `opencode-zen` 的 `mimo-v2.6-flash-free` 冷卻到 15:00，同一把的其他模型沒有冷卻
- **THEN** 15:00 之前 `model_cooldowns` 有 `mimo-v2.6-flash-free`（`until` 為 15:00），而 `cooldown` 仍是 null、`available` 仍是 true（key 已部署時）；15:00（含相等）之後該鍵消失

#### Scenario: 整把與模型冷卻同時列出
- **WHEN** 同一把先有模型 A 冷卻到 16:00，再有整把冷卻到 15:00
- **THEN** `cooldown` 是 `{"until": "…15:00…"}`、`model_cooldowns` 有 A（`until` 為 16:00）；兩個欄位都保留，互不覆蓋
