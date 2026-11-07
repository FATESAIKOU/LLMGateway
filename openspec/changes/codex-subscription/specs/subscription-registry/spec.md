## MODIFIED Requirements

### Requirement: 訂閱 id 是固定集合
`id` MUST 是 `lgwd/policy.py` 的 `SUBSCRIPTIONS` 常數裡的值（`claude-personal`、`claude-company`、`opencode-zen`、`opencode-go`、`groq`、`openrouter`、`ollama-cloud`、`antigravity`，本 change 加 `chatgpt-personal`）。

#### Scenario: 未知的訂閱鍵
- **WHEN** 政策檔 `subscriptions` 有不在 `SUBSCRIPTIONS` 的鍵
- **THEN** 政策載入失敗（`PolicyError("policy.subscriptions has unknown key")`），lgwd 不啟動

#### Scenario: chatgpt-personal 在固定集合內
- **WHEN** 政策檔 `subscriptions` 含 `chatgpt-personal`，且 `SUBSCRIPTIONS`、`AGENTS`、`PROVIDERS` 都已加對應的一筆
- **THEN** 政策載入成功

### Requirement: 共通 JSON 的 agents 條款
Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（條款；免費模型只經過 opencode）；`kind` 為 `local-login` 的訂閱的 `agents` MUST 只有它綁定的那個 agent（對照表見「local-login 的 agents 條款」）；政策載入 MUST 拒絕這三類訂閱的 `agents` 不是剛好那一個值的政策檔，也 MUST 拒絕其他訂閱的 `agents` 含 local-login 綁定的 agent。

#### Scenario: 政策檔把 openrouter 的 agents 改寬
- **WHEN** 政策檔的 `openrouter.agents` 不是剛好 `["opencode"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動；resolve 不會把 Claude Code 指到 OpenRouter

#### Scenario: openrouter 只給 opencode
- **WHEN** 讀取 `openrouter` 的描述
- **THEN** `agents` 只有 `opencode`

#### Scenario: codex 不得出現在其他訂閱
- **WHEN** 政策檔的 `opencode-zen.agents` 含 `codex`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

### Requirement: 沒有憑證的訂閱（kind `local-login`）
`kind` 為 `local-login` 的 subscription SHALL 表示憑證由呼叫方那一台機器自己保管，Gateway 不搬、不匯出、不代管。

固定集合裡的 local-login 訂閱是 `antigravity`（agy 用 Mac 本機的 Google 登入）與 `chatgpt-personal`（Codex CLI 用 ChatGPT 訂閱登入）；`auth.json` 的保管、分發與多處使用時的序列化由呼叫方負責，不在 Gateway。

#### Scenario: 讀取 antigravity 的 kind
- **WHEN** 讀取 `antigravity` 的共通 JSON
- **THEN** `kind` 為 `local-login`，表示憑證在呼叫方那一台機器

#### Scenario: 讀取 chatgpt-personal 的共通 JSON
- **WHEN** 政策檔含 `chatgpt-personal`，呼叫 list
- **THEN** 該筆的 `provider` 為 `openai`、`kind` 為 `local-login`、`endpoint` 為 null、`agents` 為 `["codex"]`，且沒有 `key`

### Requirement: local-login 的 endpoint 為 null
`kind` 為 `local-login`（`antigravity`、`chatgpt-personal`）時 `endpoint` MUST 為 null。呼叫方用自己那一台機器的本機登入直連上游，Gateway MUST NOT 代理、MUST NOT 換端點。

#### Scenario: agy 沒有 endpoint
- **WHEN** 讀取 `antigravity` 的共通 JSON
- **THEN** `endpoint` 為 null、`kind` 為 `local-login`；Gateway 不回任何要交給 agy 的位址

#### Scenario: codex 沒有 endpoint
- **WHEN** 讀取 `chatgpt-personal` 的共通 JSON
- **THEN** `endpoint` 為 null、`kind` 為 `local-login`；Gateway 不回任何要交給 codex 的位址

### Requirement: local-login 的 agents 條款
local-login 對照表（每把 local-login 訂閱綁一個 agent）為 `antigravity` 對 `agy`、`chatgpt-personal` 對 `codex`；政策載入 MUST 拒絕 local-login 訂閱的 `agents` 不是剛好它綁的那個 agent 的政策檔（條款；見「共通 JSON 的 agents 條款」），也 MUST 拒絕其他訂閱的 `agents` 含任何 local-login 綁定的 agent（否則 api-key 的 row 會用空 env 被選中）。

#### Scenario: 沒有憑證的訂閱 agents 改寬
- **WHEN** 政策檔的 `antigravity.agents` 不是剛好 `["agy"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

#### Scenario: chatgpt-personal 的 agents 不是剛好 codex
- **WHEN** 政策檔的 `chatgpt-personal.agents` 為 `["codex", "claude-code"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

#### Scenario: codex 出現在 agy 的訂閱
- **WHEN** 政策檔的 `antigravity.agents` 為 `["agy", "codex"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動
