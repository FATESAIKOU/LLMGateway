## MODIFIED Requirements

### Requirement: 角色分配表
政策檔頂層 MAY 有 `roles`：鍵 MUST 是 `PMO`、`PM`、`impl`、`rev`、`qa` 其中之一，值是 `{floor, order}`，有這兩者以外的鍵即拒收（`PolicyError`）。`floor` MUST 存在且是 `public`／`general`／`real` 之一，缺漏即拒收，MUST NOT 套用預設；`order` 是 subscription id 的非空陣列，不可重複，每個 id MUST 存在於 `subscriptions`。載入驗證 MUST 依序進行：`privacy` → 訂閱 id → 角色鍵 → `floor` → `order`，任一失敗即拒載入（`PolicyError`），MUST NOT 靜默放寬或略過該項。`roles` 只寫部分角色時合法；未寫的角色查詢回 404 `not_found`。沒有 `roles` 段時，`roles` 端點對任何角色回 404 `not_found`。`order` 只決定同一角色內的偏好順序，MUST NOT 改變 `subscription-registry` 的 `priority` 與 `resolve` 的選把。`roles` 為 advisory：Gateway MUST NOT 代選，`order` MUST NOT 限制 `resolve` 的釘住，也 MUST NOT 讓釘住繞過隱私——`resolve` 帶 `data` 時 MUST 依 `subscription-registry` 的隱私步驟擋下 `privacy` 不足的候選（含釘住的那把）；因此 `roles` 的候選篩選與 `resolve` 的隱私擋不矛盾：`roles` 可能比 `resolve` 更嚴（它把 `data` 與 `floor` 取較嚴），`resolve` MUST NOT 比 `roles` 更寬。過渡期的 `get` 仍不擋（洞仍在，隨 LLMGateway#48 移除）。`roles` 的 row 是候選投影，不是 `subscription-registry` 的共通 JSON，不受其欄位清單約束。

#### Scenario: 載入未知訂閱
- **WHEN** `roles.impl.order` 含不存在的 id
- **THEN** 政策載入失敗

#### Scenario: floor 缺漏
- **WHEN** `roles.PMO` 沒有 `floor`
- **THEN** 政策載入失敗，不套用 `public` 預設

#### Scenario: 沒有 roles 段
- **WHEN** 政策檔沒有 `roles`，呼叫 `GET /v1/roles/impl?agent=opencode`
- **THEN** 回 404 `not_found`

#### Scenario: 角色不在 roles 段
- **WHEN** `roles` 只有 `impl`，呼叫 `GET /v1/roles/PMO?agent=claude-code`
- **THEN** 回 404 `not_found`（不是 500）

#### Scenario: 帶 data 的 resolve 仍擋釘住
- **WHEN** `roles.impl.order` 含 `privacy: public` 的 `opencode-zen`，以 `agent=opencode&provider=opencode-zen&subscription=opencode-zen&data=real` resolve
- **THEN** 回 403 `privacy_not_allowed`——advisory 的 `order` 不讓釘住繞過隱私（`get` 是另一回事，見 `subscription-registry`）

### Requirement: 角色候選端點
`GET /v1/roles/{role}?agent=<種類>[&data=<等級>]` SHALL 回 `{schema, role, data, agent, candidates}`。`agent` 必填（缺少回 400 `bad_request` 帶 `missing`），且 MUST 是 `claude-code` 或 `opencode`，否則回 400 `bad_request`（對齊 `resolve`）；`role` 不在五個之內回 404 `not_found`；`data` 不在三個等級之內回 400 `bad_request`。`data=` 空值、重複參數與未知參數 MUST 與 `resolve` 的 query 處理規則相同，已知參數集合也與 `resolve` 相同（`data` 自 LLMGateway#102 起進 `_RESOLVE_PARAMS`，roles 端不再另立含 `data` 的集合，只是同一份的別名——同一參數不會被檢查兩次）。生效的資料等級＝`data`（缺省 `real`）與該角色 `floor` 取較嚴者，回覆的 `data` 是這個生效值。`candidates` 依該角色的 `order`，只含 `agents` 包含請求 `agent` 且 `privacy` 不低於生效等級的 subscription；每筆 SHALL 是 `{id, provider, rank, privacy, available, cooldown, model_cooldowns}`（`provider` 是 `resolve` 要的中介資料、不是 key，`provider ≠ id`，例：`claude-personal` 的 provider 是 `anthropic`），`rank` 為篩選後連續的整數 `1..n`（依 `order`）。每筆 MUST NOT 含 `usage`、`available_again_at`（依 `subscription-registry`，`usage` 只在 list 與 resolve `meta`，`available_again_at` 只在 list），回覆 MUST NOT 含 `key`；`roles` MUST NOT 觸發取用量。冷卻、用量與 `available` MUST NOT 影響 `candidates` 的成員與順序（Gateway 不代選）。沒有候選時 `candidates` 是 `[]`、狀態 200。`schema` 為 1。

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
