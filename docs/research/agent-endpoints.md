# 各 coding agent 與上游「指向一個閘道」的方式（調查）

> 調查日期：2026-10-10。範圍：Claude Code、opencode、Ollama Cloud、OpenRouter、OpenCode Zen、Antigravity（agy）。
> 規則：只依官方文件與公開 repo；本檔不含任何金鑰／token 實際值。
> 用途：LLMGateway 要把各 agent 收成統一入口前，先確認每個 agent 可以被指向哪裡、哪些設定會互相覆蓋、用量可以從哪裡取得。

## 0. 摘要（TL;DR）

- **Claude Code** 可以指向任意閘道：`ANTHROPIC_BASE_URL` 換端點，`ANTHROPIC_AUTH_TOKEN`／`ANTHROPIC_API_KEY`／`apiKeyHelper` 換憑證；憑證變數會蓋過 claude.ai 訂閱登入。自訂 header 可用 `ANTHROPIC_CUSTOM_HEADERS`。用量可用 OpenTelemetry 匯出（`claude_code.cost.usage`、`claude_code.token.usage`）。
- **opencode** 支援任意 provider（含自訂 `baseURL`），內建 OpenCode Zen；每個 provider 可設 `baseURL` 指向閘道；用量記錄在本地（session／message 的 tokens 與 cost）。
- **OpenRouter／Ollama Cloud** 皆為 OpenAI 相容（Ollama Cloud 另保留原生 `/api/*` 與 Anthropic 相容）；**OpenCode Zen** 依模型提供 Responses／Anthropic Messages／chat completions／Google 四種形狀。用量：OpenRouter 有 `/api/v1/key`、`/api/v1/credits`、`/api/v1/generation` 可查；Ollama Cloud 只有網頁 usage 頁、Zen 只有網頁 dashboard（截至 2026-10-10，這兩家查無公開用量 API）。
- **Antigravity（agy）**：預設 Google 帳號／Gemini Enterprise 登入模式**不可**換端點；但 `agy` CLI 在 Gemini API key 模式（`modelProvider: "gemini"` ＋ `GEMINI_API_KEY`）下可用 `GOOGLE_GEMINI_BASE_URL` 指向自訂的 **Gemini 相容**端點。SDK 另有 `LocalOpenAIAgentConfig` 可連 OpenAI 相容的本地 server。

以下逐節附出處。

## 1. Claude Code

### 1.1 指向閘道：`ANTHROPIC_BASE_URL`

- `ANTHROPIC_BASE_URL`：覆寫 API 端點，讓請求走 proxy 或閘道。設為非第一方主機時，MCP tool search 預設停用；自 v2.1.196 起，指向 `api.anthropic.com` 以外的主機時 Remote Control 也會停用。
  <https://docs.claude.com/en/docs/claude-code/env-vars>
- 閘道端要能接 Anthropic Messages API：驗證用 `POST <base>/v1/messages`，回應 JSON 以 `{"id":"msg_` 開頭即正常。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>
- 只設 `ANTHROPIC_BASE_URL` 而沒有閘道憑證時，**不會**取代訂閱登入：請求還是走閘道，但生效的仍是已存的 claude.ai 登入（額度與計費照訂閱算）。必須同時設閘道憑證（或 `apiKeyHelper`）才會改用閘道憑證計費。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway>、<https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>
- 管理端可把 `ANTHROPIC_BASE_URL` 釘在 managed settings 的 `env`，並用 `allowedProviders: ["customEndpoint"]` 讓受管機器只准走這個閘道（需 v2.1.285+）。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway>
- 其他 provider 專用端點變數（`ANTHROPIC_BEDROCK_BASE_URL`、`ANTHROPIC_VERTEX_BASE_URL`、`ANTHROPIC_FOUNDRY_BASE_URL`、`ANTHROPIC_AWS_BASE_URL`）也各自可指向閘道，但語意不同（Bedrock/Vertex 走原生格式，Foundry/AWS 走 Anthropic Messages 格式）。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>
- 每個 surface 讀設定的方式不同：CLI 讀環境變數與 settings 檔；VS Code 擴充用 `claudeCode.environmentVariables`；桌面版用自己的 third-party inference 設定（不讀 `ANTHROPIC_BASE_URL`）；GitHub Actions 讀 workflow `env`；Slack／cloud sessions 不吃閘道設定。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>

### 1.2 憑證變數與優先序

四個會被問到的變數：

| 變數 | 送的 header | 備註 |
| :- | :- | :- |
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer <value>` | 適合 bearer token 閘道 |
| `ANTHROPIC_API_KEY` | `X-Api-Key` | 互動模式會問一次是否核准；`-p` 非互動一律採用 |
| `apiKeyHelper`（settings 鍵） | 兩種都送 | 指令輸出當金鑰；預設快取 5 分鐘，TTL 用 `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` 調 |
| `CLAUDE_CODE_OAUTH_TOKEN` | claude.ai OAuth | 由 `claude setup-token` 產生；蓋過 keychain 內的憑證 |

出處：<https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>、<https://docs.claude.com/en/docs/claude-code/env-vars>、
<https://docs.claude.com/en/docs/claude-code/authentication>

**認證優先序**（多個憑證同時存在時，Claude Code 的選擇順序，高到低）：
1. cloud provider（`CLAUDE_CODE_USE_BEDROCK`／`VERTEX`／`FOUNDRY` 有設時）
2. `ANTHROPIC_AUTH_TOKEN`
3. `ANTHROPIC_API_KEY`
4. `apiKeyHelper`（script 輸出）
5. `CLAUDE_CODE_OAUTH_TOKEN`
6. Anthropic profile／federation 憑證
7. `/login` 的訂閱 OAuth（Pro/Max/Team/Enterprise 預設）

另註：Claude apps gateway 的登入 session 不在此排序內，它算「provider 選擇」，優先於以上全部。
出處：<https://docs.claude.com/en/docs/claude-code/authentication#authentication-precedence>

實務注意：
- 閘道憑證變數會蓋過已存的 claude.ai 登入；`ANTHROPIC_AUTH_TOKEN` 立即生效，`ANTHROPIC_API_KEY` 在互動模式要核准一次。`/status` 可看目前生效的來源。要清掉已存登入用 `/logout`。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>
- shell 與 settings 檔 `env` 同時設定同一變數時，**settings 檔的值生效**（大多數 session）。
  <https://docs.claude.com/en/docs/claude-code/env-vars>
- 帶閘道憑證時，Remote Control 與語音聽寫不可用；`ANTHROPIC_BASE_URL` 指向非 Anthropic 主機時 Remote Control 也停用。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>

### 1.3 `claude setup-token` 的用途與限制

- 用途：CI／腳本等無法開瀏覽器互動登入的環境，產生**一年期 OAuth token**。流程同 `/login` 的瀏覽器授權，token 印在終端機、**不存任何地方**，自己複製後設成 `CLAUDE_CODE_OAUTH_TOKEN`。
  <https://docs.claude.com/en/docs/claude-code/authentication#generate-a-long-lived-token>
- 需要 Pro／Max／Team／Enterprise 方案；以訂閱身分認證。
  <https://docs.claude.com/en/docs/claude-code/authentication#generate-a-long-lived-token>
- 限制：**只能發模型請求**，不能建立 Remote Control session、不能抓 claude.ai connectors；本地設定的 MCP servers 仍可用。Bare mode（`--bare`）不讀 `CLAUDE_CODE_OAUTH_TOKEN`（要用 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`）。
  <https://docs.claude.com/en/docs/claude-code/authentication#generate-a-long-lived-token>
- `CLAUDE_CODE_OAUTH_TOKEN` 蓋過 keychain 內憑證；設定後整個 session 都用它（除非跑 `/login`）；過期就重新產生並重啟。
  <https://docs.claude.com/en/docs/claude-code/env-vars>
- 管理端限制：`claude setup-token` 與 `/install-github-app` 只強制 `forceLoginMethod`，**不**檢查 `forceLoginOrgUUID`，所以可能 mint 到別組織的 token。
  <https://docs.claude.com/en/docs/claude-code/authentication#restrict-login-to-your-organization>

### 1.4 自訂 header 與 metadata

- `ANTHROPIC_CUSTOM_HEADERS`：新增自訂 header，格式 `Name: Value`，多個用換行分隔（JSON settings 檔內用 `\n`）。需 v2.1.227+。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect#send-additional-headers>
- 若值涉及憑證、組織／租戶、routing 或 API 行為類 header（如 `Authorization`、`Host`），由 server-managed settings 下發時算「需要核准的設定」；從 project／local settings 來時照 `env` 的套用規則。
  <https://docs.claude.com/en/docs/claude-code/env-vars>
- Requests 另有 `x-client-request-id`（client 產生）；system prompt 開頭有 attribution block（帶 client 版本與 prompt fingerprint），可用 `CLAUDE_CODE_ATTRIBUTION_HEADER=0` 關掉（對閘道快取 request body 的情境重要）。
  <https://docs.claude.com/en/docs/claude-code/monitoring-usage>、<https://docs.claude.com/en/docs/claude-code/env-vars>
- 閘道若拒絕新欄位，可用 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 拿掉 pre-release beta header 與欄位。
  <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>

### 1.5 OpenTelemetry 用量匯出

- 開關：`CLAUDE_CODE_ENABLE_TELEMETRY=1`；exporter：`OTEL_METRICS_EXPORTER`（`otlp`／`prometheus`／`console`／`none`）、`OTEL_LOGS_EXPORTER`；OTLP 端點與協定用標準 `OTEL_EXPORTER_OTLP_*`（含 `OTEL_EXPORTER_OTLP_HEADERS` 認證）。
  <https://docs.claude.com/en/docs/claude-code/monitoring-usage>
- 用量相關指標：`claude_code.cost.usage`（USD，可依 `model` 切分）、`claude_code.token.usage`（tokens，可依 token `type` 與 `model` 切分）；另有 session／lines of code／commit 等計數。
  <https://docs.claude.com/en/docs/claude-code/monitoring-usage>
- 事件：每次 API 請求有 `api_request` 事件（含 token／cost 等欄位），重試放棄後才有 `claude_code.api_error`。
  <https://docs.claude.com/en/docs/claude-code/monitoring-usage>
- 歸屬：`OTEL_RESOURCE_ATTRIBUTES` 可加自訂維度（如部門、team.id）；標準屬性含 `organization.id`、`user.account_uuid`、`session.id` 等。
  <https://docs.claude.com/en/docs/claude-code/monitoring-usage>
- **限制（與閘道相關）**：
  - OTel exporter 變數在專案的 `.claude/settings.json`／`.claude/settings.local.json` 會被忽略（可由 managed settings、使用者 settings 或 shell 設定）。
    <https://docs.claude.com/en/docs/claude-code/monitoring-usage>
  - 走 `ANTHROPIC_BASE_URL` 閘道時，Claude Code 對 Anthropic 的 telemetry 不帶閘道憑證；且閘道憑證生效時，Claude Code **不會**向 Console analytics dashboard 回報用量——閘道端要自己記錄用量。
    <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>
  - 要關掉非必要背景流量用 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`（會一併停用自動更新）。
    <https://docs.claude.com/en/docs/claude-code/llm-gateway-connect>

### 1.6 對 LLMGateway 的含意（小結）

- 要讓 Claude Code 走 LLMGateway：發一組閘道憑證，distribute `ANTHROPIC_BASE_URL` ＋ `ANTHROPIC_AUTH_TOKEN`（或 `apiKeyHelper`，可輪替）。
- 想沿用個人／公司訂閱而不換計費：只有 `ANTHROPIC_BASE_URL` 不夠，訂閱仍生效；這代表**閘道只做路由、不做換帳**是可行的（把 OAuth 能力透傳）。
- 用量紀錄兩個來源：(a) 閘道自己在 server 端記（唯一權威）；(b) 各 worker 的 OTel 匯出（自願性、可被關）。

## 2. opencode

### 2.1 Provider 設定與指向閘道

- 每個 provider 都可以用 `baseURL` 覆寫端點，適合 proxy 或自訂端點：
  ```json
  { "provider": { "anthropic": { "options": { "baseURL": "https://api.anthropic.com/v1" } } } }
  ```
  <https://opencode.ai/docs/providers/>
- 憑證由 `/connect` 寫入 `~/.local/share/opencode/auth.json`。（本調查不讀此檔。）
  <https://opencode.ai/docs/providers/>
- 自訂 provider（任何 OpenAI 相容服務）：`npm` 用 `@ai-sdk/openai-compatible`（走 `/v1/chat/completions`）；用 `/v1/responses` 的則用 `@ai-sdk/openai`；可設 `options.baseURL`、`options.apiKey`、`options.headers`（自訂 header）。
  <https://opencode.ai/docs/providers/#custom-provider>
- config 檔合併順序（後蓋前）：remote（`.well-known/opencode`）→ global（`~/.config/opencode/opencode.json`）→ `OPENCODE_CONFIG` → project `opencode.json` → `.opencode` 目錄 → `OPENCODE_CONFIG_CONTENT` → managed config（macOS `/Library/Application Support/opencode/`、Linux `/etc/opencode/`、Windows `%ProgramData%\opencode`）→ macOS MDM（`ai.opencode.managed`）。managed 最高、使用者不可覆寫。
  <https://opencode.ai/docs/config/>
- 可用 `enabled_providers`／`disabled_providers` 限制 provider 白名單；`experimental.policies` 也可 deny `provider.use`。
  <https://opencode.ai/docs/config/>、<https://opencode.ai/docs/policies/>
- 已內建 provider 目錄包含 OpenCode Zen、OpenRouter、Ollama、Ollama Cloud 等 75+ 家（以 Models.dev 為後端）。Ollama Cloud 需先 `ollama pull <model>-cloud` 把模型資訊拉到本地，才能用 `/models` 選。
  <https://opencode.ai/docs/providers/>

### 2.2 OpenCode Zen 作為 provider

- `/connect` 選 OpenCode Zen、貼 API key；model id 格式 `opencode/<model-id>`。
  <https://opencode.ai/docs/zen/>
- 它是 AI gateway，端點依模型而異：OpenAI Responses（`https://opencode.ai/zen/v1/responses`）、Anthropic Messages（`/zen/v1/messages`）、OpenAI 相容 chat completions（`/zen/v1/chat/completions`）、Google（`/zen/v1/models/<model>`）；模型清單 `https://opencode.ai/zen/v1/models`。
  <https://opencode.ai/docs/zen/#endpoints>
- 計費：pay-as-you-go、按 request 計價、可加值；balance 低於 $5 自動 reload $20；workspace 與每位成員可設每月上限。Teams 可指派 role（Admin／Member）、逐模型開關、BYOK（用自己的 OpenAI／Anthropic key，帳算在該 provider）。
  <https://opencode.ai/docs/zen/#pricing>、<https://opencode.ai/docs/zen/#for-teams>
- 用量可在 Zen 帳號的 usage history 看到；截至 2026-10-10，官方文件**查無**公開 usage／quota 查詢 API（目前只有 `/zen/v1/models` 這類模型端點）。
  <https://opencode.ai/docs/zen/>

### 2.3 用量記錄方式

- 本地記錄、可查：`opencode stats` 指令「Show token usage and cost statistics for your OpenCode sessions」，支援 `--days`、`--models`、`--project`、`--tools`。
  <https://opencode.ai/docs/cli/#stats>
- `opencode models --verbose` 會列出模型的 cost metadata（價格來自 Models.dev）。
  <https://opencode.ai/docs/cli/#models>
- 成本計算基礎是 Models.dev 的 model metadata（per-provider 價格）。
  <https://opencode.ai/docs/providers/>
- 資料存放於本地（`opencode db path` 可印出資料庫路徑；`opencode export` 可把 session 匯成 JSON）。
  <https://opencode.ai/docs/cli/#db>、<https://opencode.ai/docs/cli/#export>
- 截至 2026-10-10，官方文件未見像 Claude Code 那樣的內建 OTel 匯出；要集中用量得靠：外部讀本地 stats／DB、或 provider（閘道）端記錄。
- `small_model`（如 Haiku、Nano）會被用來產生 session 標題，會出現在 usage history。
  <https://opencode.ai/docs/zen/#pricing>、<https://opencode.ai/docs/config/#models>

### 2.4 對 LLMGateway 的含意（小結）

- 把 opencode 指向自架閘道最直接：在 `provider` 下用 OpenAI 相容自訂 provider ＋ `options.baseURL`，或覆寫現有 provider 的 `baseURL`。
- 想強制所有 worker 走閘道：用 managed config（不可被使用者覆寫）釘住 provider 與 baseURL（另可用 `disabled_providers` 或 policies 收窄）。
- 用量：閘道端記錄為主；worker 端可用 `opencode stats` 對帳。

## 3. 上游三家：Ollama Cloud、OpenRouter、OpenCode Zen

### 3.1 API 形狀

| 上游 | 端點 | 形狀 |
| :- | :- | :- |
| Ollama Cloud | `https://ollama.com/api/chat`（原生）；`https://ollama.com/v1`（OpenAI 相容，含 `/v1/chat/completions`、`/v1/responses`、`/v1/models`、`/v1/embeddings`）；另有 Anthropic 相容 | 原生＋OpenAI 相容＋Anthropic 相容（各支援子集）；認證 `Authorization: Bearer <OLLAMA_API_KEY>` |
| OpenRouter | `https://openrouter.ai/api/v1/chat/completions` | OpenAI Chat API 相容（schema 經正規化，跨模型一致） |
| OpenCode Zen | 依模型：`/zen/v1/responses`（OpenAI Responses）、`/zen/v1/messages`（Anthropic Messages）、`/zen/v1/chat/completions`（OpenAI 相容）、`/zen/v1/models/<model>`（Google） | 混合式 AI gateway；每個模型對應一種 API 形狀 |

出處：<https://docs.ollama.com/cloud>、<https://docs.ollama.com/api/openai-compatibility>、
<https://docs.ollama.com/api/anthropic-compatibility>（Ollama 的 Anthropic 相容專頁）、
<https://openrouter.ai/docs/api-reference/overview>、<https://opencode.ai/docs/zen/#endpoints>

補充：
- Ollama Cloud 的 OpenAI 相容層**不支援** stateful Responses、內建 web search、自由格式 tool-call replay。
  <https://docs.ollama.com/api/openai-compatibility>
- OpenRouter 回應**一定**帶 `usage`（token 數，用原生 tokenizer 算）與 `cost`（credits）；streaming 時在最後一個 chunk 回報一次。
  <https://openrouter.ai/docs/api-reference/overview>
- Zen 有 `GET https://opencode.ai/zen/v1/models` 可列模型；計價按 token 分輸入／輸出／cache（見 pricing 表）。
  <https://opencode.ai/docs/zen/>

### 3.2 查用量／額度

| 上游 | 方式 |
| :- | :- |
| Ollama Cloud | 網頁 <https://ollama.com/settings/usage>（另有 plans 頁）；**截至 2026-10-10 查無公開 usage/quota API** |
| OpenRouter | `GET https://openrouter.ai/api/v1/key`：回 `limit`／`limit_remaining`／`usage`（all-time、daily、weekly、monthly）／`byok_usage_*`／`free_model_daily_requests`；`GET /api/v1/credits`（需 management key）：`total_credits`／`total_usage`；`GET /api/v1/generation?id=`：單筆請求的 token 與 cost（事後對帳）；429 錯誤才帶 `X-RateLimit-*` |
| OpenCode Zen | 網頁 usage history／dashboard（workspace 與成員可設月上限）；**截至 2026-10-10 查無公開 usage API** |

出處：<https://docs.ollama.com/cloud#usage>、<https://openrouter.ai/docs/api-reference/limits>、
<https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits>、
<https://openrouter.ai/docs/api-reference/overview#querying-cost-and-stats>、<https://opencode.ai/docs/zen/#pricing>、<https://opencode.ai/docs/zen/#monthly-limits>

其它：
- OpenRouter 有 BYOK：用自己的 provider key 時，`usage.is_byok` 與 `cost_details.upstream_inference_*` 可分辨帳算在誰身上。
  <https://openrouter.ai/docs/api-reference/overview>、<https://openrouter.ai/docs/api-reference/limits>
- OpenRouter 402 錯誤的 `error.metadata.limit_source` 可分辨是帳戶餘額、per-key limit、或 in-flight budget。
  <https://openrouter.ai/docs/api-reference/limits>
- Ollama Cloud 可被 `ollama launch claude` / `launch codex` / `launch opencode` 直接拉起 coding agent（Ollama 代管登入）。
  <https://docs.ollama.com/cloud>

### 3.3 對 LLMGateway 的含意（小結）

- 三家都能當「閘道後面的上游」：OpenRouter／Zen（chat/completions 系列）與 Ollama Cloud（`/v1`）可走同一種 OpenAI 相容呼叫；Zen 的 Claude／Gemini 模型則要走 Messages／Google 端點。
- 用量對帳以閘道自身的紀錄為準；若需要直接查上游，OpenRouter 的 `/key` 與 `/generation` 最完整（可程式化），Ollama Cloud 與 Zen 截至 2026-10-10 只能看網頁。

## 4. Antigravity（agy）

### 4.1 CLI（`agy`）：可以換端點（有條件）

- 安裝後預設是 Google 帳號／Gemini Enterprise 登入（keyring 或 SSH OAuth），**此模式下截至 2026-10-10 的官方文件查無換端點設定**。
  <https://antigravity.google/docs/cli/install>
- 改用 API key 模式時可換端點，全部條件如下：
  1. `~/.gemini/antigravity-cli/settings.json` 設 `"modelProvider": "gemini"`（唯一接受值；`GEMINI_API_KEY` 單獨設無效、不讀 `.env`）。
  2. 環境變數 `GEMINI_API_KEY`（Google AI Studio 產生）。
  3. 環境變數 `GOOGLE_GEMINI_BASE_URL` 指向自訂端點——官方描述為「a different **Gemini-compatible** endpoint」，即閘道必須提供 Gemini 相容 API，不是 OpenAI 或 Anthropic 形狀。
  <https://antigravity.google/docs/cli/install#point-the-cli-to-a-custom-endpoint>
- 限制：這是 CLI 層；Antigravity IDE／2.0 的 settings 文件沒有對應的 endpoint 設定（只有 model 選擇、權限、telemetry 等）。
  <https://antigravity.google/docs/settings>
- 想回頭用帳號登入：移除 `modelProvider` 後重啟；`modelProvider` 仍在但 `GEMINI_API_KEY` 消失時 CLI 起不來。
  <https://antigravity.google/docs/cli/install#revert-to-default-authentication>

### 4.2 SDK（另一條路）

- Antigravity SDK（Python）的 `LocalOpenAIAgentConfig` 可連**任何 OpenAI 相容的本機 server**（Ollama、LM Studio、vLLM）：參數 `base_url`（如 `http://localhost:11434/v1`）＋ `model`。
  <https://antigravity.google/docs/sdk/local-models>
- 另有 `LiteRTAgentConfig` 跑本地 `.litertlm` 模型。這是 SDK 路線，不是 agy CLI。

### 4.3 用量查詢

- `/usage`（別名 `/quota`）：向 backend 抓最新配額，開 TUI 面板顯示各模型用量上限與剩餘。
  <https://antigravity.google/docs/cli/commands/usage>
- `/credits`：看剩餘 G1 credits 與加購連結；`useG1Credits` 設定可決定方案配額用完後是否動用個人 AI credits。
  <https://antigravity.google/docs/cli/reference>、<https://antigravity.google/docs/settings>
- 截至 2026-10-10，查無公開的用量 API 文件；CLI 內建面板與 `enableTelemetry`（匿名統計）都非查詢用介面。
  <https://antigravity.google/docs/settings>

### 4.4 對 LLMGateway 的含意（小結）

- 若要走 LLMGateway，只有 **`agy` CLI ＋ API key 模式 ＋ Gemini 相容端點**一途；因此閘道需對 Antigravity 暴露一個 Gemini 相容介面，或直接用 `GEMINI_API_KEY` 打真正的 Gemini API（此時閘道只能觀察、不能換上游）。
- IDE／預設登入模式不可換端點；要納入就只能維持原狀（不經閘道）。

## 5. 跨工具比較（結論）

| 工具 | 指向閘道的主要旋鈕 | 形狀 | 憑證 | 用量來源 |
| :- | :- | :- | :- | :- |
| Claude Code | `ANTHROPIC_BASE_URL` ＋ 憑證變數／`apiKeyHelper` | Anthropic Messages（另有 Bedrock/Vertex/Foundry 專用變數） | `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY`、`apiKeyHelper`、`CLAUDE_CODE_OAUTH_TOKEN`（優先序見 §1.2） | OTel 匯出（`claude_code.cost.usage` 等）＋閘道端紀錄 |
| opencode | `provider.<id>.options.baseURL`（含自訂 OpenAI 相容 provider） | OpenAI 相容（chat/completions 或 responses） | `auth.json`（`/connect`）或 `options.apiKey` | 本機 `opencode stats`／DB；閘道端紀錄 |
| Ollama Cloud | 直接用它或用 opencode/Claude Code 指向它 | 原生 `/api/*`＋OpenAI 相容 `/v1`＋Anthropic 相容 | `OLLAMA_API_KEY` | 網頁 usage 頁（截至 2026-10-10 無公開 API） |
| OpenRouter | `baseURL` 換成它的端點（任何相容 client） | OpenAI 相容 | API key（Bearer） | `GET /api/v1/key`、`/credits`、`/generation` |
| OpenCode Zen | 用 Zen 當 provider；或按模型挑端點 | 多形狀（Responses／Messages／chat/completions／Google） | Zen API key（Bearer） | 網頁 dashboard（截至 2026-10-10 無公開 API） |
| Antigravity（agy） | `modelProvider:"gemini"` ＋ `GOOGLE_GEMINI_BASE_URL`（API key 模式） | Gemini 相容 | `GEMINI_API_KEY` | `/usage`、`/credits`（截至 2026-10-10 無公開 API） |

共同觀察：
1. **端點與憑證是兩個獨立旋鈕**，但多半成對使用；只換端點不換憑證時，Claude Code 仍會帶原登入（訂閱計費照舊）。
2. **形狀決定閘道要做多少轉譯**：OpenAI 相容是最大公約數（opencode、OpenRouter、Ollama Cloud `/v1`、Zen 部分模型）；Claude Code 要 Anthropic Messages；agy 要 Gemini 相容。
3. **用量最可靠的來源是閘道自己**：各 agent 端（OTel、`stats`、`/usage`）都只是輔助，且可被使用者關閉。
