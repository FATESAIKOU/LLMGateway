## MODIFIED Requirements

### Requirement: 角色候選端點
`GET /v1/roles/{role}?agent=<種類>[&data=<等級>]` SHALL 回 `{schema, role, data, agent, candidates}`。`agent` 必填（缺少回 400 `bad_request` 帶 `missing`），且 MUST 是 `claude-code`、`opencode` 或 `agy`，否則回 400 `bad_request`（對齊 `resolve`）；`role` 不在五個之內回 404 `not_found`；`data` 不在三個等級之內回 400 `bad_request`。`data=` 空值、重複參數與未知參數 MUST 與 `resolve` 的 query 處理規則相同，已知參數集合也與 `resolve` 相同（`data` 自 LLMGateway#102 起進 `_RESOLVE_PARAMS`，roles 端不再另立含 `data` 的集合，只是同一份的別名——同一參數不會被檢查兩次）。生效的資料等級＝`data`（缺省 `real`）與該角色 `floor` 取較嚴者，回覆的 `data` 是這個生效值。`candidates` 依該角色的 `order`，只含 `agents` 包含請求 `agent` 且 `privacy` 不低於生效等級的 subscription；每筆 SHALL 是 `{id, provider, rank, privacy, available, cooldown, model_cooldowns}`（`provider` 是 `resolve` 要的中介資料、不是 key，`provider ≠ id`，例：`claude-personal` 的 provider 是 `anthropic`），`rank` 為篩選後連續的整數 `1..n`（依 `order`）。每筆 MUST NOT 含 `usage`、`available_again_at`（依 `subscription-registry`，`usage` 只在 list 與 resolve `meta`，`available_again_at` 只在 list），回覆 MUST NOT 含 `key`；`roles` MUST NOT 觸發取用量。冷卻、用量與 `available` MUST NOT 影響 `candidates` 的成員與順序（Gateway 不代選）。沒有候選時 `candidates` 是 `[]`、狀態 200。`schema` 為 1。`agent` 為 `agy` 時，只有 `agents` 含 `agy` 的 subscription 會出現；它們是否在 `candidates` 仍只看該角色的 `order` 與 `privacy`／`floor`，與其他 agent 同一條規則（本 change 不把 `antigravity` 寫進任何 `order`；要不要寫是政策資料的決定）。

#### Scenario: impl 取真資料候選
- **WHEN** `GET /v1/roles/impl?agent=opencode&data=real`
- **THEN** `candidates` 只含 `privacy` 為 `real` 且 `agents` 含 `opencode` 的訂閱，順序照 `roles.impl.order`，`rank` 從 1 連續

#### Scenario: PMO 想用較低等級
- **WHEN** `GET /v1/roles/PMO?agent=claude-code&data=public`，而 `roles.PMO.floor` 是 `real`
- **THEN** 回覆的 `data` 是 `real`，候選照 `real` 篩

#### Scenario: 缺省 data 取最嚴
- **WHEN** `GET /v1/roles/qa?agent=opencode` 沒有 `data`
- **THEN** 回覆的 `data` 是 `real`

#### Scenario: 冷卻不改候選
- **WHEN** `opencode-zen` 整把冷卻中，呼叫 `GET /v1/roles/impl?agent=opencode&data=public`
- **THEN** `opencode-zen` 仍在 `candidates` 中、`rank` 不變，其 `cooldown` 與 `available` 照實呈現

#### Scenario: 隱私不足時不降級
- **WHEN** `agent=opencode`、`data=real`，而 `roles.qa.order` 內 `agents` 含 `opencode` 的訂閱 `privacy` 都低於 `real`
- **THEN** 回 200、`candidates` 為 `[]`，回應中沒有那些訂閱的 id

#### Scenario: 低等級時高隱私訂閱仍可出現
- **WHEN** `roles.qa.order` 含 `privacy` 為 `public`、`general`、`real` 且 `agents` 含 `opencode` 的訂閱，呼叫 `GET /v1/roles/qa?agent=opencode&data=public`
- **THEN** `candidates` 含 `privacy` 為 `public`、`general`、`real` 且 `agents` 含 `opencode` 的訂閱，證明過濾方向是「不低於」

#### Scenario: row 帶 provider 且不等於 id
- **WHEN** `GET /v1/roles/PMO?agent=claude-code`
- **THEN** 含 `claude-personal` 的那筆 `provider` 是 `anthropic`，`id` 是 `claude-personal`

#### Scenario: resolve 回 200 但冷卻在未來
- **WHEN** 某訂閱整把冷卻到未來，對它 `resolve`
- **THEN** 回 200，`meta.cooldown.until` 照實帶出未來時間（Gateway 不擋、不代選）

#### Scenario: agent 種類不合法
- **WHEN** `GET /v1/roles/impl?agent=clade-code`
- **THEN** 回 400 `bad_request`，不回 200 `[]`

#### Scenario: 不含 key 與額度
- **WHEN** 任何 `roles` 回覆
- **THEN** 每筆 row 沒有 `key`、環境變數名、`usage`、`available_again_at`（`provider` 是中介資料，允許）

#### Scenario: data 參數規則與 resolve 相同
- **WHEN** `GET /v1/roles/impl?agent=opencode&data=public&data=real`
- **THEN** 回 400 `bad_request`（與 `resolve` 的重複參數規則相同），不回 200

#### Scenario: agy 不在 order 時是空候選
- **WHEN** 政策檔有 `antigravity`（`agents` 為 `["agy"]`、`privacy` 為 `public`），但沒有任何 `roles.<role>.order` 含它；呼叫 `GET /v1/roles/impl?agent=agy&data=public`
- **THEN** 回 200、`candidates` 為 `[]`（不是 400、不是 404）

#### Scenario: agy 在 order 且等級足夠
- **WHEN** 單元測試的假政策把 `antigravity` 寫進 `roles.impl.order`，呼叫 `GET /v1/roles/impl?agent=agy&data=public`
- **THEN** `candidates` 只有 `antigravity` 一筆，`provider` 為 `google`、`available` 依 kind `local-login` 為 true，不含 `key`／`usage`／`available_again_at`

#### Scenario: agy 不在 order 不影響其他 agent
- **WHEN** `roles.impl.order` 不含 `antigravity`，呼叫 `GET /v1/roles/impl?agent=opencode&data=public`
- **THEN** 候選與加這筆之前相同（`antigravity` 的 `agents` 不含 `opencode`，也不會被選進來）
