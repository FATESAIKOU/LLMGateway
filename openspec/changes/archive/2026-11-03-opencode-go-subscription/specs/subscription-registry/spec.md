## MODIFIED Requirements

### Requirement: opencode-zen 與 groq 的出廠 endpoint
出廠 endpoint SHALL 為：`opencode-zen` 的 `https://opencode.ai/zen/v1`（OpenAI 相容的 base）；`groq` 的 `https://api.groq.com/openai/v1`（OpenAI 相容的 base）；`opencode-go` 的 `https://opencode.ai/inference/go/openai/v1`：chat 的 OpenAI 相容 base。Go 的 models 端點前綴不同（`https://opencode.ai/inference/go/v1/models`），呼叫方 MUST NOT 從 `endpoint` 直接接 `/models` 推導（官方文件沒有這條路徑）。

#### Scenario: opencode-zen 與 groq 的 endpoint
- **WHEN** 讀取 `opencode-zen` 與 `groq` 的 endpoint
- **THEN** 分別為 `https://opencode.ai/zen/v1` 與 `https://api.groq.com/openai/v1`

#### Scenario: opencode-go 的 endpoint
- **WHEN** 讀取 `opencode-go` 的共通 JSON
- **THEN** `endpoint` 為 `https://opencode.ai/inference/go/openai/v1`，`provider` 為 `opencode-go`

### Requirement: OPENCODE_PROVIDER_ID
`agent` 為 `opencode` 時，`env` MUST 含 `OPENCODE_PROVIDER_ID`，字串 MUST 是 opencode 的 provider id（`opencode-zen`→`opencode`、`opencode-go`→`opencode-go`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud`，由 LLMGateway 維護；opencode config 內的模型名＝`<OPENCODE_PROVIDER_ID>/<OPENCODE_MODEL>`，由 sidecar 組），MUST NOT 為空，且必然出現。

#### Scenario: opencode 的兩個模型（片段）
- **WHEN** 同一把 `opencode-zen` 先後 resolve `mimo-v2.6-flash-free` 與 `big-pickle`
- **THEN** 兩次 `env` 的 `OPENCODE_PROVIDER_ID` 都是 `opencode`、`OPENCODE_API_KEY` 相同，`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 各自是該模型；`env` 不含 `OPENCODE_CONFIG_CONTENT`

#### Scenario: opencode-go 的片段
- **WHEN** 以 `agent=opencode&provider=opencode-go&model=beam-free` resolve
- **THEN** `env` 的 `OPENCODE_PROVIDER_ID` 為 `opencode-go`、`OPENCODE_API_KEY` 為 Zen 那把 key 的值（兩把共用同一個鍵名），`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 為 `beam-free`
