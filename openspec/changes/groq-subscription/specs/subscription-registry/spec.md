## MODIFIED Requirements

### Requirement: endpoint 的語意
`endpoint` SHALL 是 provider 的**根位址**（值以 `lgwd/providers.py` 為準），`kind` 為 `claude-subscription` 時 MUST 為 null（由 agent 直連）。出廠值與形狀：
- `ollama-cloud` 的 `https://ollama.com` 可以直接當 Claude Code 的 `ANTHROPIC_BASE_URL`；`openrouter` 的 `https://openrouter.ai/api` 同屬 Anthropic 相容的 base（本系統只給 opencode，接法由 opencode driver 決定）；兩者的 OpenAI 相容路徑由呼叫方自己組（例如再接 `/v1`）；
- `opencode-zen` 的 `https://opencode.ai/zen/v1`：OpenAI 相容的 base；
- `groq` 的 `https://api.groq.com/openai/v1`：OpenAI 相容的 base（官方文件以 `base_url` 的形式給這個值）。本系統只給 opencode；opencode 端官方支援 Groq 的內建 provider。

呼叫方（agent driver）MUST NOT 假設 `endpoint` 是某種協定的完整路徑；要依 `provider` 組出自己要的接法，或使用 agent 內建的 provider 設定。

#### Scenario: opencode driver 拿到 ollama-cloud 的 endpoint
- **WHEN** opencode 的 driver 拿到 `ollama-cloud` 的 `endpoint`（`https://ollama.com`）
- **THEN** 它自己組 OpenAI 相容的路徑（例如再接 `/v1`），或不覆寫內建 provider 的 baseURL；服務回的 `endpoint` 不變

#### Scenario: Claude Code 拿到 ollama-cloud 的 endpoint
- **WHEN** Claude Code 的 driver 拿到 `ollama-cloud` 的 `endpoint`
- **THEN** 直接當 `ANTHROPIC_BASE_URL` 用（這個值是 Anthropic 相容的 base）

#### Scenario: Claude 訂閱沒有 endpoint
- **WHEN** 讀取 `claude-personal` 的共通 JSON
- **THEN** `endpoint` 為 null，agent 直連、不使用 base

#### Scenario: groq 的 endpoint
- **WHEN** 讀取 `groq` 的共通 JSON
- **THEN** `endpoint` 為 `https://api.groq.com/openai/v1`，`provider` 為 `groq`，`agents` 只有 `opencode`

### Requirement: env 的組成

`env` SHALL 依（provider、kind、agent）組成，只含憑證、端點、模型命名（含 opencode 的 provider id）三類變數；MUST NOT 含呼叫方的維運變數（auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數、`permission`、`instructions`——那些由 sidecar 疊）。鍵集＝下列清單（`env` MUST 只含這些鍵；每鍵的型別／空值／出現條件；sidecar 對不在清單上的鍵見「未知鍵」）：

- `CLAUDE_CODE_OAUTH_TOKEN`（claude-code＋claude-subscription）：字串＝該把的 key（OAuth token）；不可空；該組合必然出現。
- `ANTHROPIC_API_KEY`（claude-code＋api-key）：provider 為 anthropic 時字串＝該把的 key、不可空（**現行不可達**：anthropic 恆為 claude-subscription，預留形狀）；其他 provider 時＝**空字串**（刻意佔位，蓋掉子行程環境可能殘留的繼承值）；claude-subscription 時不出現。
- `ANTHROPIC_AUTH_TOKEN`（claude-code＋api-key、provider 非 anthropic）：字串＝該把的 key；不可空。
- `ANTHROPIC_BASE_URL`（claude-code＋api-key、provider 非 anthropic）：字串＝該把的 `endpoint`（URL）；僅 `endpoint` 非 null 時出現。
- `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`（claude-code 全部組合）：字串＝選定的 model（供應商 model id；兩鍵同值）；不可空；僅 model 非 null 時出現。
- `OPENCODE_API_KEY`（opencode、各 provider）：字串＝該把的 key；不可空；必然出現。
- `OPENCODE_PROVIDER_ID`（opencode、各 provider）：字串＝opencode 的 provider id（`opencode-zen`→`opencode`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud`、`groq`→`groq`，由 LLMGateway 維護；opencode config 內的模型名＝`<OPENCODE_PROVIDER_ID>/<OPENCODE_MODEL>`，由 sidecar 組）；不可空；必然出現。
- `OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`（opencode、各 provider）：字串＝選定的 model（供應商 model id；兩鍵同值）；不可空；僅 model 非 null 時出現。

opencode 只回**片段**（上述 `OPENCODE_*` 四鍵）：Gateway MUST NOT 回 `OPENCODE_CONFIG_CONTENT`，也不回 `baseURL`（`endpoint` 是 provider 根位址，依 #28 不抄進 opencode 的 `options.baseURL`）。model 為 null（agent 自己的預設）時 SHALL 不給模型變數。

**未知鍵與消費端規則**：`env` MUST 只含上述鍵（Gateway 在同一個 `env_schema` 內不得改鍵集）；sidecar 對未知鍵、overlay、`OPENCODE_CONFIG_CONTENT` 的組成、`permission`／`instructions` 的擁有、繼承環境不外漏等**消費端義務**見 `archive/2026-10-24-agent-provider-model-interface/design.md` 決定 2 與決定 6，由 MyLinuxPool 的 sidecar 規格承接（本 spec 只定 Gateway 能實作與驗證的部分）。

#### Scenario: claude 訂閱不帶模型變數
- **WHEN** resolve 選中 `claude-personal`（claude-subscription）而 `meta.model` 為 null
- **THEN** `env` 只有 `CLAUDE_CODE_OAUTH_TOKEN`，沒有 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`

#### Scenario: opencode 的兩個模型（片段）
- **WHEN** 同一把 `opencode-zen` 先後 resolve `mimo-v2.6-flash-free` 與 `big-pickle`
- **THEN** 兩次 `env` 的 `OPENCODE_PROVIDER_ID` 都是 `opencode`、`OPENCODE_API_KEY` 相同，`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 各自是該模型；`env` 不含 `OPENCODE_CONFIG_CONTENT`

#### Scenario: groq 的片段
- **WHEN** 以 `agent=opencode&provider=groq&model=openai/gpt-oss-20b` resolve
- **THEN** `env` 的 `OPENCODE_PROVIDER_ID` 為 `groq`、`OPENCODE_API_KEY` 為該把的 key（`GROQ_API_KEY` 的值，不與其他把共用），`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 為 `openai/gpt-oss-20b`（供應商 id 含 `/` 照原樣，不前綴正規化）
