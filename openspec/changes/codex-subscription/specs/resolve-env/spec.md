## MODIFIED Requirements

### Requirement: local-login 的 env 為空物件
`kind` 為 `local-login` 的訂閱（`antigravity`、`chatgpt-personal`）**沒有 env**：`env` MUST 是**空物件 `{}`**——沒有任何鍵，也就 MUST NOT 出現 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`／`OPENCODE_*`。agy 與 codex 各用自己在那台機器的本機登入，Gateway 不給憑證、不給端點、不給模型變數；模型只走 `meta.model`（sidecar 的 agy、codex driver 只從那裡拿）。

#### Scenario: agy 的 env 是空物件
- **WHEN** 以 `agent=agy&provider=google&model=<該把的裸 id>` resolve，選中 `antigravity`（kind `local-login`）
- **THEN** `env` 是 `{}`：沒有 `ANTHROPIC_*`（含 `ANTHROPIC_API_KEY` 的空字串佔位）、沒有 `OPENCODE_*`、沒有 `CLAUDE_CODE_OAUTH_TOKEN`；`env_schema` 仍是 1

#### Scenario: codex 的 env 是空物件
- **WHEN** 以 `agent=codex&provider=openai&model=<該把的裸 id>` resolve，選中 `chatgpt-personal`
- **THEN** `env` 是 `{}`：沒有任何 `OPENAI_*`、`CODEX_*`、`ANTHROPIC_*`、`OPENCODE_*` 鍵（`CODEX_HOME` 由 sidecar 決定，不進 env）；`env_schema` 仍是 1

### Requirement: agent 或 kind 任一成立即回空物件
實作 MUST 同時寫兩條判斷——`agent` 屬於 local-login agent 集合（`agy`、`codex`），**或**該把的 `kind` 為 `local-login`，任一成立就回 `{}`，防線不依賴單一條件。只判 `agent` 擋的是「agy 或 codex 進了某一筆 api-key row」（政策條款被改弱，該把的 `kind` 不是 `local-login`）；只判 `kind` 擋的是「某把 local-login 訂閱的 `agents` 被改弱成含 `claude-code`」（該把仍是 `local-login` 但 agent 不在集合內）；兩條合起來，任一邊被改弱都還是回 `{}`。

#### Scenario: 條款被改弱也不會回 claude 的 env
- **WHEN** 單元測試直接以一筆 api-key provider（例如 `opencode-zen`）呼叫 `build_env("agy", <key>, <model>)`——模擬「`_AGY_ONLY` 條款被改弱、agy 進了 api-key row」的組合（經政策載入不可達，所以這裡測的是單元層的第二道防線）
- **THEN** `env` 仍是 `{}`、不回任何 `ANTHROPIC_*`／`OPENCODE_*`：`kind` 不是 `local-login`，但 `agent` 為 `agy` 那條判斷成立

#### Scenario: codex 進了 api-key row 也不會回 claude 的 env
- **WHEN** 單元測試直接以一筆 api-key provider（例如 `opencode-zen`）呼叫 `build_env("codex", <key>, <model>)`
- **THEN** `env` 仍是 `{}`、不回任何 `ANTHROPIC_*`／`OPENCODE_*`

#### Scenario: 條款被改弱成含 claude-code 也不會回 claude 的 env
- **WHEN** 單元測試直接以一個 `kind` 為 `local-login` 的 provider（`GoogleProvider`）呼叫 `build_env("claude-code", null, <model>)`——模擬「`_AGY_ONLY` 條款被改弱、`antigravity` 的 `agents` 含 `claude-code`」的組合（經政策載入不可達，所以這裡測的是單元層的第二道防線）
- **THEN** `env` 仍是 `{}`、不回任何 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`／`OPENCODE_*`：`agent` 不是 `agy`，但 `kind` 為 `local-login` 那條判斷成立

#### Scenario: chatgpt-personal 的 agents 被改弱成含 claude-code 也不會回 claude 的 env
- **WHEN** 單元測試直接以 `OpenAIProvider`（`kind` 為 `local-login`）呼叫 `build_env("claude-code", null, <model>)`——模擬 `chatgpt-personal` 的 `agents` 被改弱
- **THEN** `env` 仍是 `{}`：`agent` 不在 local-login agent 集合內，但 `kind` 為 `local-login` 那條判斷成立
