## MODIFIED Requirements

### Requirement: resolve 的 agent 種類
`agent` MUST 是 `claude-code`、`opencode`、`agy` 或 `codex`，否則 MUST 回 400 `bad_request`（不是 403 `agent_not_allowed`）。

#### Scenario: 未知的 agent
- **WHEN** 以 `agent=foo&provider=opencode-zen` resolve
- **THEN** 回 400 `bad_request`（不是 403 `agent_not_allowed`）

#### Scenario: codex 是已知的 agent
- **WHEN** 以 `agent=codex&provider=openai` resolve，政策檔有 `chatgpt-personal`
- **THEN** 不回 400 `bad_request`（agent 驗證通過）

### Requirement: env_schema 版本
`env_schema` SHALL 是 env 組成（鍵集與鍵的語意）的版本（目前為 1）；新增／移除 env 鍵或改變鍵的語意時 MUST bump；Gateway MUST NOT 在同一個 `env_schema` 內改變鍵集（個別回應因 model 為 null 或 agent 為 `agy`、`codex` 而少給鍵，不算鍵集變更）。

#### Scenario: 少給鍵不算鍵集變更
- **WHEN** resolve 的 `model` 為 null，`env` 少了模型變數
- **THEN** `env_schema` 仍是 1，不視為鍵集變更

#### Scenario: codex 的空 env 不算鍵集變更
- **WHEN** 以 `agent=codex&provider=openai` resolve，`env` 為 `{}`
- **THEN** `env_schema` 仍是 1，不視為鍵集變更

## ADDED Requirements

### Requirement: codex 的 env 為空物件
`agent` 為 `codex` 時 `env` MUST 是空物件 `{}`，模型只走 `meta.model`。

#### Scenario: codex 的 resolve
- **WHEN** 以 `agent=codex&provider=openai&model=<該把的裸 id>` resolve
- **THEN** 回 200，`meta.subscription` 為 `chatgpt-personal`、`meta.kind` 為 `local-login`、`meta.agent` 為 `codex`、`meta.model` 為該 id、`env` 為 `{}`；`env_schema` 仍是 1

#### Scenario: 不讀 key 檔
- **WHEN** 以 `agent=codex&provider=openai` resolve，key 檔裡沒有 `chatgpt-personal` 的鍵
- **THEN** 回 200，不因 `unknown subscription` 或 `not_deployed` 失敗

#### Scenario: 冷卻中的 chatgpt-personal 照規則回覆
- **WHEN** `chatgpt-personal` 在冷卻中，以 `agent=codex&provider=openai` resolve
- **THEN** 回 200（冷卻不參與選把），`env` 仍為 `{}`，`meta.cooldown` 帶冷卻資訊

### Requirement: codex 的用量為 not_applicable
`agent` 為 `codex` 且 resolve 成功選中 `chatgpt-personal` 時，`meta.usage.status` MUST 是 `not_applicable`，`source` 與窗口都 MUST 是 null。

#### Scenario: codex 的用量是 not_applicable
- **WHEN** resolve 成功選中 `chatgpt-personal`
- **THEN** `meta.usage.status` 是 `not_applicable`、`source` 與窗口都是 null，不觸發任何取用量
