# Sub2API 替代方案調查

- 調查日期：2026-10-10（Asia/Taipei）
- 範圍：LiteLLM Proxy、new-api／one-api、claude-code-router（CCR）、「不用閘道：啟動時注入環境變數＋各 agent 自帶用量紀錄」。
- 方法：只讀公開 repo 與官方文件。程式碼快照：
  - LiteLLM `main` @ `5282aed0`（partial clone，僅取相關目錄）
  - new-api `main` @ `c6741c36`（原 `Calcium-Ion/new-api`，現已改名／轉向 `QuantumNous/new-api`）
  - one-api `main` @ `8df4a267`（最後主線 commit 2025-02-21）
  - claude-code-router `main` @ `057abaa1`
  - 外部數據（stars、issue 數、最後推送）為 2026-10-10 GitHub 公開 API 取得。
- 本文未讀取任何憑證檔、未記錄任何金鑰或 token 實際值。相關前作：[sub2api 調查](sub2api.md)。

## TL;DR

- **要 key 帶 metadata（worker／角色／agent）＋完整用量紀錄**：LiteLLM Proxy 是唯一現成且完整的選擇；它甚至官方辨識 Claude Code 的 `sk-ant-oat*` OAuth token，可直接接 Claude 訂閱。
- **new-api／one-api**：截至 2026-10-10，Claude 訂閱不支援（Claude channel 只吃 Console API key；new-api 另支援 ChatGPT 訂閱 OAuth 的 Codex channel），token 沒有任何 metadata 欄位；new-api 授權是 AGPL-3.0，one-api 已停滯。
- **claude-code-router**：定位是「單機 agent 控制台」，可重用本機 Claude Code 登入與憑證池、有本機用量 SQLite；不是多租戶發 key 的中央閘道。
- **不用閘道**：條款風險最低、部署最輕，Claude Code 有官方 OpenTelemetry（可帶自訂資源屬性標 worker/角色）、opencode 有本機 `stats`／SQLite／匯出，OpenRouter 與 Ollama Cloud 都有官方用量介面；代價是沒有中央發 key 與配額閘門，要自建彙總。
- 對 LLMGateway 期 1：建議先走「無閘道＋中央觀測」，把閘道需求（多租戶 key、配額、跨供應商調度）延後到真的出現再上 LiteLLM。

---

## 0. 同一張表比較

| 方案 | **LiteLLM Proxy** | **new-api** | **one-api** | **claude-code-router** | **無閘道（env 注入）** |
|---|---|---|---|---|---|
| Claude 訂閱 token（setup-token／OAuth） | ✅ 官方辨識 `sk-ant-oat*`，可當 api_key 用；支援 `/v1/messages` | ❌ Claude channel 僅 `x-api-key`；另有 Codex channel 支援 ChatGPT OAuth | ❌ 僅 API key | ✅ 可匯入本機 Claude Code OAuth（Keychain）並查 5h/7d 配額 | ✅ 每台 worker 各自 login 或 `CLAUDE_CODE_OAUTH_TOKEN`（setup-token） |
| Ollama Cloud | ⚠️ 用 `ollama/`＋自訂 api_base＋api_key（程式碼會送 Bearer）；官方文件無專頁 | ⚠️ Ollama channel 可自訂 base＋Bearer | ⚠️ 同類做法（舊版） | ⚠️ 需自訂 OpenAI 相容 provider | ✅ 官方 API key（`OLLAMA_API_KEY`） |
| OpenRouter | ✅ 內建 provider | ✅ 內建 channel | ✅ 內建 adaptor | ✅ 內建 provider catalog | ✅ 官方 key |
| OpenCode Zen | ⚠️ 用 OpenAI 相容 custom provider 指到 `opencode.ai/zen/v1` | ⚠️ 自訂渠道 | ⚠️ 自訂渠道 | ⚠️ 支援 OpenCode agent；provider 需自訂 | ✅ 原生支援（`opencode auth login`） |
| key 的 metadata（worker／角色／agent） | ✅ 最完整：key metadata、`metadata.tags`、`spend_logs_metadata`、`x-litellm-tags`、user/team/org 階層 | ❌ token 只有 name／group／model_limits／IP；無 free-form metadata | ❌ 更少（name／models／subnet；無 group） | ❌ 非多租戶；本機 gateway 有 key 授權但無標籤 | ➖ 無閘道層；靠 OS/環境隔離＋`OTEL_RESOURCE_ATTRIBUTES` |
| 用量紀錄欄位與匯出 | ✅ SpendLogs（key hash／user／team／tags／model／spend／tokens／metadata）＋`/spend/logs/v2` 分頁匯出 API、`/user/daily/activity`、Prometheus、S3 callbacks | ⚠️ logs 表（token_name／model／quota／prompt+completion tokens／channel／group／IP／request_id）；可選 ClickHouse；無 CSV 匯出 | ⚠️ logs 表更簡；無匯出 | ⚠️ 本機 SQLite `usage_events`（tokens／cache／cost／provider／credential_id）；本機 UI；無 CSV | ⚠️ 分散各家：Claude Code OTel 指標/事件、`opencode stats`＋本機 DB、OpenRouter `/generation`、Zen 後台；要自建彙總 |
| 部署負擔 | 中：Python＋Postgres 必備；Docker；config.yaml；部分治理功能屬 Enterprise | 中低：Go 單 binary＋SQLite/MySQL/PG＋Redis(選)；Docker | 同 new-api（但版本舊） | 低（單機）：桌面 App／本機 gateway | 最低：只裝 agent＋env；但需自架 OTel collector 與報表 |
| 授權 | MIT（core）；`enterprise/` 目錄商業授權 | **AGPL-3.0** | MIT | MIT | 不適用（Claude Code 為 Anthropic 專有、opencode MIT） |
| 上游條款風險 | 中：工具中立，但用訂閱 token 走第三方閘道仍是 Anthropic 條款灰區（無官方警示） | 中高：典型中轉站性質（README 無警示） | 中高：同上 | 中：重用本機登入自用較輕，集中代管他人帳號較重 | 低：各自帳號／金鑰；但「自動化多裝置共用一人訂閱」仍待 Anthropic 界定 |

> 表中與本文所有「無／未見／沒有／查無」等否定判斷，均為截至 2026-10-10 的查證結果；上游功能與官方文件隨時可能變動。

---

## 1. LiteLLM Proxy

- 規模與維護：60,820 stars、12,204 forks、5,440 open issues；2026-10-10 仍在推送（`pushed_at` 當日）。出處：https://api.github.com/repos/BerriAI/litellm 、https://github.com/BerriAI/litellm
- 授權：核心 MIT；`enterprise/` 目錄另有商業授權（LICENSE 檔明示分界）。出處：https://github.com/BerriAI/litellm/blob/main/LICENSE

### 1.1 Claude 訂閱 token

- LiteLLM 的 Anthropic provider 有 **OAuth token 專門處理**：常數 `ANTHROPIC_OAUTH_TOKEN_PREFIX = "sk-ant-oat"`，遇到此類 token 會改送 `Authorization: Bearer <token>` 並附上必要的 `anthropic-beta` OAuth 標頭。`sk-ant-oat*` 正是 Claude Code OAuth／`claude setup-token` 產生的格式，因此可把訂閱 token 直接當 `api_key` 掛在 `anthropic/` 模型上。
  - 出處：https://github.com/BerriAI/litellm/blob/main/litellm/types/llms/anthropic.py
  - 出處：https://github.com/BerriAI/litellm/blob/main/litellm/llms/anthropic/common_utils.py
- 下游協定：支援 `/v1/messages` 端點（Claude Code 可直接以 `ANTHROPIC_BASE_URL` 指向 LiteLLM），另有 Anthropic pass-through。
  - 出處：https://docs.litellm.ai/docs/supported_endpoints
  - 出處：https://docs.litellm.ai/docs/pass_through/intro

### 1.2 其他上游

- OpenRouter：內建 provider，預設 `https://openrouter.ai/api/v1`。出處：https://github.com/BerriAI/litellm/tree/main/litellm/llms/openrouter
- Ollama：provider 預設 `http://localhost:11434`，`api_base` 可自訂；帶 api_key 時會送 `Authorization: Bearer`（`litellm/llms/ollama/chat/transformation.py`），因此 `api_base: https://ollama.com`＋api_key 即可打 Ollama Cloud 的 `/api/chat`（Ollama Cloud 官方 API 文件：https://docs.ollama.com/cloud ）；但截至 2026-10-10，LiteLLM 官方沒有 Ollama Cloud 專頁，屬設定組合而非一級支援。
  - 出處：https://github.com/BerriAI/litellm/blob/main/litellm/llms/ollama/chat/transformation.py
- OpenCode Zen：截至 2026-10-10 沒有專屬 provider；Zen 端點本身是 OpenAI／Anthropic 相容（https://opencode.ai/docs/zen/ ），可用 LiteLLM 的 OpenAI-compatible custom provider 指向 `https://opencode.ai/zen/v1`。

### 1.3 key 與 metadata（本節是它與其他方案差距最大的地方）

- Virtual key 建立時可帶任意 `metadata`；`metadata.tags` 可當 key 級標籤。
  - 出處：https://docs.litellm.ai/docs/proxy/virtual_keys
- 請求級標籤：`x-litellm-tags` 標頭、request body `tags`、模型部署 `tags`；預設還會把 **User-Agent 記成 tag**（官方舉例就能用來看 Claude Code／Gemini CLI 的用量）。
  - 出處：https://docs.litellm.ai/docs/proxy/request_tags
  - 出處：https://docs.litellm.ai/docs/proxy/cost_tracking
- 任意 key/value 進日誌：`x-litellm-spend-logs-metadata`（或 key metadata 的 `spend_logs_metadata`），可塞 `worker`／`role`／`agent` 等自訂欄位。
  - 出處：https://docs.litellm.ai/docs/proxy/cost_tracking

### 1.4 用量紀錄與匯出

- `LiteLLM_SpendLogs` 欄位（官方範例）：api_key hash、user、team_id、`request_tags`、end_user、model_group、api_base、spend、total/completion/prompt tokens、metadata（fallbacks 等）。
  - 出處：https://docs.litellm.ai/docs/proxy/cost_tracking
- 匯出／查詢：`/spend/logs`（legacy，最多一萬列）、**`/spend/logs/v2`（分頁，官方明說適合 exports/ETL）**、`/user/daily/activity`（每日×model×provider×key 明細）、`/global/spend/report`（Enterprise）、Prometheus 指標、logging callbacks（可導 S3 等）。
  - 出處：https://docs.litellm.ai/docs/proxy/cost_tracking
  - 出處：https://docs.litellm.ai/docs/proxy/logging
- Enterprise 邊界：SSO/SAML、audit logs、spend reports、key rotation（含 scheduled rotation）等標示為 Enterprise。
  - 出處：https://docs.litellm.ai/docs/enterprise

### 1.5 部署

- 需要 Postgres 才有 virtual keys／spend logs（`DATABASE_URL`＋master key）；Docker 映像與 config.yaml；大量治理功能設定在 `general_settings`／`litellm_settings`。
  - 出處：https://docs.litellm.ai/docs/proxy/virtual_keys
  - 出處：https://docs.litellm.ai/docs/proxy/deploy

### 1.6 條款風險

- 工具本身中立；截至 2026-10-10，官方文件沒有 sub2api 那種「可能違反上游條款」的警示。風險取決於你怎麼用：把 Claude 訂閱 token 放進共享閘道給多個下游，與 sub2api 同一類灰區（見 §6）。

---

## 2. new-api

- Repo：`Calcium-Ion/new-api` 已 301 轉到 **`QuantumNous/new-api`**；49,597 stars、11,922 forks、1,369 open issues；2026-10-10 當日仍有推送。
  - 出處：https://api.github.com/repositories/717197250 、https://github.com/QuantumNous/new-api
- 授權：**AGPL-3.0**（與 one-api 的 MIT 不同，網路服務化修改有傳染性開源義務）。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/LICENSE

### 2.1 上游

- Channel 目錄齊全（`relay/channel/`）：claude、codex、gemini、vertex、aws、xai、ollama、openrouter、deepseek、moonshot、zhipu、minimax…，甚至有一個 `sub2api/` channel（可直接把 sub2api 當上游）。另有 `advancedcustom/` 自訂渠道可指任何 OpenAI 相容端點。
  - 出處：https://github.com/QuantumNous/new-api/tree/main/relay/channel
- **Claude 訂閱**：Claude channel 只設 `x-api-key`（Console API key），截至 2026-10-10 未見 OAuth／setup-token 支援。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/relay/channel/claude/adaptor.go
- **ChatGPT 訂閱 OAuth**：Codex channel 會 parse 含 `access_token`／`refresh_token`／`account_id` 的 OAuth key JSON，並有 codex usage 查詢。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/relay/channel/codex/oauth_key.go
  - 出處：https://github.com/QuantumNous/new-api/blob/main/controller/codex_usage.go
- **Ollama Cloud**：Ollama channel 對上游送 `Authorization: Bearer <ApiKey>`，base URL 可自訂 → 指到 `https://ollama.com` 即可。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/relay/channel/ollama/adaptor.go
- **OpenRouter**：內建 channel，預設 `https://openrouter.ai/api`。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/constant/channel.go
- **OpenCode Zen**：截至 2026-10-10 無專屬 channel；用 `advancedcustom` 指 `https://opencode.ai/zen/v1`。

### 2.2 key 與 metadata

- Token 模型欄位：`key`、`name`、`status`、`expired_time`、`remain_quota`／`unlimited_quota`、`model_limits`、`allow_ips`、`group`、`cross_group_retry`。**沒有 free-form metadata／標籤欄位**（截至 2026-10-10 對照 `model/token.go`）——worker／角色／agent 只能用 `name` 與 `group` 表達。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/model/token.go

### 2.3 用量紀錄與匯出

- Log 欄位：user_id、created_at、type、content、username、**token_name**、**model_name**、quota、prompt_tokens、completion_tokens、use_time、is_stream、channel_id/name、token_id、**group**、**IP**、request_id、upstream_request_id、`other`（JSON）。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/model/log.go
- 支援把 log 庫切到 **ClickHouse**（大流量），並有 `docs/openapi/api.json`／`relay.json` 可當 API 介面參考。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/model/log.go
  - 出處：https://github.com/QuantumNous/new-api/tree/main/docs/openapi
- 匯出：截至 2026-10-10，controller 未見 CSV／JSON 匯出（與 sub2api 一樣要直接查 DB 或自行翻頁 API）。

### 2.4 部署與條款

- Go 單一 binary；SQLite／MySQL／PostgreSQL＋Redis（選用），Docker 部署、內建完整管理 UI。
  - 出處：https://github.com/QuantumNous/new-api/blob/main/README.md
- 條款風險與 sub2api 同類（多帳號池＋中轉給下游），但截至 2026-10-10，README 沒有像 sub2api 那樣的條款警示。

---

## 3. one-api

- Repo：37,112 stars、6,876 forks、1,039 open issues；`main` 最後 commit **2025-02-21**（GitHub API 顯示 2026-01 有其他推送，但主線實質停滯）。出處：https://api.github.com/repos/songquanpeng/one-api 、https://github.com/songquanpeng/one-api
- 授權：MIT。出處：https://github.com/songquanpeng/one-api/blob/main/LICENSE
- 能力是 new-api 的子集：
  - Adaptor 有 anthropic、gemini、ollama、openrouter、vertexai、aws、xai 等（Claude 同樣只吃 API key）。
    - 出處：https://github.com/songquanpeng/one-api/tree/main/relay/adaptor
  - Token 欄位更精簡：`name`、`expired_time`、`remain_quota`、`models`、`subnet`——連 `group` 都沒有，更無 metadata（截至 2026-10-10 對照 `model/token.go`）。
    - 出處：https://github.com/songquanpeng/one-api/blob/main/model/token.go
  - 截至 2026-10-10 無 Codex（ChatGPT 訂閱）channel、無 Claude 訂閱；log 欄位亦較基本。
- 結論：除非要 MIT 授權的極簡 relay，否則沒有理由選它；但它與 new-api 同源，可用來理解其資料模型。

---

## 4. claude-code-router（CCR）

- Repo：37,605 stars、3,158 forks、1,135 open issues；2026-10-10 仍在推送。授權 MIT。
  - 出處：https://api.github.com/repos/musistudio/claude-code-router 、https://github.com/musistudio/claude-code-router/blob/main/LICENSE
- 定位：**本機**模型閘道／控制台（桌面 App＋local gateway），管理 Claude Code、Codex、Grok CLI、OpenCode 等 agent 的 provider／模型／路由；主打 retry、credential pool、key rotation、fallback。
  - 出處：https://github.com/musistudio/claude-code-router/blob/main/README.md

### 4.1 Claude 訂閱與各上游

- **Claude 訂閱 OAuth**：CCR 會掃描本機 Claude Code 憑證（macOS Keychain `Claude Code-credentials` 或檔案），匯入 OAuth token，並用官方 usage API 映射 5h／7d／Opus／Sonnet 等配額。
  - 出處：https://github.com/musistudio/claude-code-router/blob/main/packages/core/src/agents/local-providers/claude-code.ts
- 另有 local providers：codex（ChatGPT 登入）、grok、kimi、opencode、opencode-freetier、zcode。
  - 出處：https://github.com/musistudio/claude-code-router/tree/main/packages/core/src/agents/local-providers
- OpenRouter：內建 provider catalog。出處：https://github.com/musistudio/claude-code-router/blob/main/packages/core/src/providers/openrouter-provider-catalog.ts
- Ollama Cloud／OpenCode Zen：截至 2026-10-10 無專屬頁面；README 支援「custom compatible providers」，Zen 亦可直接當 agent 用。
  - 出處：https://github.com/musistudio/claude-code-router/blob/main/README.md
- 有趣的是它自己也整合 new-api：`packages/core/src/providers/new-api.ts`。
  - 出處：https://github.com/musistudio/claude-code-router/blob/main/packages/core/src/providers/new-api.ts

### 4.2 key metadata 與用量

- 它是「一人工作站」：本機 gateway 有 API key 授權（`gateway/api-key-authorizer`），但截至 2026-10-10 沒有 key metadata／tags 的概念；「credential pool」指的是上游多把金鑰輪替，不是下游多租戶標籤。
  - 出處：https://github.com/musistudio/claude-code-router/blob/main/packages/core/src/providers/credential-pool.ts
- 用量：本機 SQLite `usage_events`（created_at、request_id、client、model、logical_model、provider、credential_id、status_code、duration_ms、input/output/cache_read/cache_write/total tokens、cost_usd、cost_source）＋請求日誌，UI 顯示統計；截至 2026-10-10 未見 CSV 匯出。
  - 出處：https://github.com/musistudio/claude-code-router/blob/main/packages/core/src/usage/store.ts
- 部署：Node／桌面 App，單機即用；要跨多台 worker 得靠各自安裝＋外部彙總。
- 條款風險：重用自己機器的 Claude Code 登入，自用性質較輕；但若拿來代管他人帳號給多人用，就與中轉站同性質。

---

## 5. 不用閘道：啟動時注入環境變數＋各 agent 自帶紀錄

這個方案不發 key、不做中轉：每台 worker 啟動時注入自己那一份憑證（env／憑證檔），用量由各 agent 與各供應商的官方機制記錄，再由中央定期收集。

### 5.1 憑證注入（啟動時）

- **Claude Code**：`CLAUDE_CODE_OAUTH_TOKEN`（由 `claude setup-token` 產生，官方註明「alternative to `/login` for SDK and automated environments，取代 keychain 憑證」）可直接以環境變數注入，或用 `ANTHROPIC_API_KEY`。
  - 出處：https://code.claude.com/docs/en/env-vars
- **opencode**：`opencode auth login` 把 provider 憑證存到 `~/.local/share/opencode/auth.json`；也可用環境變數與專案 `.env`。
  - 出處：https://opencode.ai/docs/cli/
- **Ollama Cloud**：官方 API key，`Authorization: Bearer $OLLAMA_API_KEY` 打 `https://ollama.com/api/chat`（也有 OpenAI／Anthropic 相容端點）；`ollama launch claude/codex/opencode` 可讓 CLI 代登入。
  - 出處：https://docs.ollama.com/cloud
- **OpenRouter**：官方 key，OpenAI 相容端點；回應自帶 usage（見下）。
- **OpenCode Zen**：`/connect` 貼 Zen API key；Zen 後台有 workspace／成員月限額、auto-reload。
  - 出處：https://opencode.ai/docs/zen/

### 5.2 Claude Code OpenTelemetry（官方）

- 開關與輸出：`CLAUDE_CODE_ENABLE_TELEMETRY=1`＋`OTEL_METRICS_EXPORTER`／`OTEL_LOGS_EXPORTER`（otlp／prometheus／console），OTLP 端點與 headers 可設；管理員可用 managed settings 強制全組織。
  - 出處：https://code.claude.com/docs/en/monitoring-usage
- 指標（可直接當帳）：`claude_code.token.usage`、`claude_code.cost.usage`、`claude_code.session.count`、`claude_code.lines_of_code.count`、`claude_code.commit.count`、`claude_code.pull_request.count` 等。
  - 出處：https://code.claude.com/docs/en/monitoring-usage
- 事件：`user_prompt`、`tool_decision`、`tool_result`、`api_error` 等；`llm_request` span 帶 `model`、`input_tokens`／`output_tokens`／`cache_read_tokens`／`cache_creation_tokens`、`request_id`、`cost` 對應欄位。
  - 出處：https://code.claude.com/docs/en/monitoring-usage
- 標準屬性含 `user.email`、`organization.id`、`session.id`、`app.version`…；**自訂標籤**用 `OTEL_RESOURCE_ATTRIBUTES="worker=...,role=...,agent=..."`（官方明講可做 team／cost center 維度）。
  - 出處：https://code.claude.com/docs/en/monitoring-usage

### 5.3 opencode 本機紀錄

- `opencode stats`：token 用量與成本統計（`--days`／`--models`／`--project`）。
- `opencode db path`／`opencode db [query] --format json|tsv`：直接查本機資料庫。
- `opencode export [sessionID]`：session JSON（含 transcript、token 資訊）。
  - 出處：https://opencode.ai/docs/cli/

### 5.4 各供應商官方用量

- OpenRouter：每個回應內建 usage（prompt/completion/total、cached、`cost`、`cost_details.upstream_inference_cost`）；可用 `id` 事後查 `/generation`。出處：https://openrouter.ai/docs/use-cases/usage-accounting
- OpenCode Zen：後台 usage history、workspace／成員月限額與 auto-reload。出處：https://opencode.ai/docs/zen/
- Ollama Cloud：官方用量頁 `https://ollama.com/settings/usage`（截至 2026-10-10 **沒有公開 API**，要自動化只能像 sub2api 一樣爬設定頁）。出處：https://docs.ollama.com/cloud
- Claude 訂閱：Claude Code OTel（上節）；另外 Anthropic Console 的 Admin API 有組織層 usage/cost report（本文件未展開）。

### 5.5 綜合評語

- 優點：沒有新的憑證集中點（每台 worker 自己持有）、條款最乾淨、零閘道維運；Claude Code 與 Zen 的官方紀錄已足以做帳。
- 缺點：沒有「發 key—配額—停用」的中央政策點；opencode 紀錄在本機（要自己想辦法拉）、Ollama Cloud 無公開 API（截至 2026-10-10）；跨供應商彙總要自建（OTel collector＋一份儲存＋報表）。
- 授權：無新增授權問題（Claude Code 依 Anthropic 條款；opencode 為 MIT：https://github.com/anomalyco/opencode ）。

---

## 6. 對 LLMGateway 期 1 設計的建議

1. **期 1 預設走「無閘道＋中央觀測」**：LLMGateway 只做「配發與登記」（哪台 worker／角色／agent 用哪個訂閱，以 env 注入 `CLAUDE_CODE_OAUTH_TOKEN` 或各家 API key），用量靠 Claude Code OTel（`OTEL_RESOURCE_ATTRIBUTES` 標 worker/role/agent）＋opencode `stats`／DB 定時回報＋各供應商後台。這一步不需要新服務、不集中憑證，直接對上期 1 的「開關申請與紀錄」。
2. **把「要不要閘道」設成明確的觸發條件**：當出現「一把 key 給多台 worker 共用」「需要統一配額/停用」「需要跨供應商自動 fallback」任兩項時，才升級到 LiteLLM。它對 Claude 訂閱 token（`sk-ant-oat*`）、`/v1/messages`、metadata/tags、spend logs 匯出都有官方支援，是唯一不用自己造輪子的選項；先做一個 spike 驗證 Postgres＋Claude 訂閱 token 的合規與穩定度。
3. **new-api／one-api 排除在核心之外**：new-api 是 AGPL-3.0、token 無 metadata，做不到「worker／角色／agent」歸因；one-api 已停滯。若只是要參考中轉站 UI，可看 new-api，但別讓它變成資料主權所在。
4. **CCR 定位為「worker 端的小工具」而非中央**：適合在單機重用 Claude Code 登入、做憑證輪替與本機成本顯示；若之後要它跟 LLMGateway 回報，需自行把它的 SQLite 用量拉出來（無匯出 API）。
5. **先釘死兩個風險前提再談閘道**：(a) Anthropic 對「訂閱 token 自動化／多裝置」的界線（sub2api README 已自承可能違反條款）；(b) 憑證是否必須集中保管。只要 (b) 可避免，就維持方案 1；真的需要集中，才進方案 2，並限定單一管理員、內網、不轉給第三方。

---

## 附錄：主要來源

- LiteLLM：https://github.com/BerriAI/litellm ／https://docs.litellm.ai/docs/proxy/virtual_keys ／https://docs.litellm.ai/docs/proxy/cost_tracking ／https://docs.litellm.ai/docs/proxy/request_tags
- new-api：https://github.com/QuantumNous/new-api
- one-api：https://github.com/songquanpeng/one-api
- claude-code-router：https://github.com/musistudio/claude-code-router
- Claude Code 監控（OTel）：https://code.claude.com/docs/en/monitoring-usage
- Claude Code 環境變數（`CLAUDE_CODE_OAUTH_TOKEN`）：https://code.claude.com/docs/en/env-vars
- opencode CLI（stats／db／export）：https://opencode.ai/docs/cli/
- OpenCode Zen：https://opencode.ai/docs/zen/
- OpenRouter 用量：https://openrouter.ai/docs/use-cases/usage-accounting
- Ollama Cloud API：https://docs.ollama.com/cloud
- Anthropic 條款（未逐條比對）：https://www.anthropic.com/legal/consumer-terms 、https://www.anthropic.com/legal/aup
