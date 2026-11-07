## MODIFIED Requirements

### Requirement: 角色候選端點的參數檢查
`agent` 必填（缺少回 400 `bad_request` 帶 `missing`），且 MUST 是 `claude-code`、`opencode`、`agy` 或 `codex`，否則回 400 `bad_request`（對齊 `resolve`）；`role` 不在五個之內回 404 `not_found`；`data` 不在三個等級之內回 400 `bad_request`。

#### Scenario: agent 種類不合法
- **WHEN** `GET /v1/roles/impl?agent=clade-code`
- **THEN** 回 400 `bad_request`，不回 200 `[]`

#### Scenario: codex 是合法的 agent
- **WHEN** `GET /v1/roles/impl?agent=codex&data=public`
- **THEN** 不回 400 `bad_request`（回 200，`candidates` 依政策而定）

## ADDED Requirements

### Requirement: codex 的候選
`agent` 為 `codex` 時，只有 `agents` 含 `codex` 的 subscription SHALL 出現在 `candidates`，且是否出現 MUST 只看該角色的 `order` 與 `privacy`／`floor`，與其他 agent 同一條規則。

本 change 與 P2 都不把 `chatgpt-personal` 寫進任何 `order`；進 `impl`／`qa` 的 `order`（排在 `claude-personal` 之前）是 P4 的政策資料，前提是 MyLinuxPool#797 能開。

#### Scenario: codex 不在 order 時是空候選
- **WHEN** 政策檔有 `chatgpt-personal`（`agents` 為 `["codex"]`），但沒有任何 `roles.<role>.order` 含它，`roles.impl.floor` 為 `public`；呼叫 `GET /v1/roles/impl?agent=codex&data=public`
- **THEN** 回 200、`candidates` 為 `[]`（不是 400、不是 404）

#### Scenario: codex 在 order 且等級足夠
- **WHEN** 單元測試的假政策把 `chatgpt-personal`（`privacy` 為 `public`）寫進 `roles.impl.order`，且 `roles.impl.floor` 為 `public`，呼叫 `GET /v1/roles/impl?agent=codex&data=public`
- **THEN** `candidates` 只有 `chatgpt-personal` 一筆，`provider` 為 `openai`、`available` 依 kind `local-login` 為 true，不含 `key`／`usage`／`available_again_at`

#### Scenario: privacy 不足時被擋
- **WHEN** 假政策的 `chatgpt-personal.privacy` 為 `public`、`roles.impl.floor` 為 `public`，呼叫 `GET /v1/roles/impl?agent=codex&data=real`
- **THEN** `candidates` 為 `[]`

#### Scenario: codex 在 order 不影響其他 agent
- **WHEN** `roles.impl.order` 含 `chatgpt-personal`，呼叫 `GET /v1/roles/impl?agent=opencode&data=public`
- **THEN** 候選不含 `chatgpt-personal`（它的 `agents` 不含 `opencode`）
