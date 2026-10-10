# Sub2API 調查

- 調查日期：2026-10-10（Asia/Taipei）
- 調查對象：`Wei-Shaw/sub2api`，https://github.com/Wei-Shaw/sub2api
- 調查方法：clone 公開 repo，程式碼快照為 `main` @ `3a6fd1c9`（對應 release `v0.2.15`，2026-10-09；兩者僅 `backend/cmd/server/VERSION` 一行不同）。本文程式碼出處一律連到 `v0.2.15` 標籤，外部數據（stars、issue 數）為 2026-10-10 由 GitHub 公開 API 取得。
- 本文只使用公開資料，未讀取任何憑證檔、未記錄任何金鑰或 token 實際值。

## TL;DR

Sub2API 是一套「把 AI 訂閱帳號（Claude／ChatGPT／Gemini／Grok 等）的額度轉發給下游 API Key」的自架閘道，Go + PostgreSQL + Redis + Vue，Docker 一鍵部署。它支援我們關心的 Claude OAuth／setup-token／Console API key、OpenAI OAuth、Gemini、Antigravity、Grok、OpenCode Zen/Go、Ollama Cloud 等上游，內建帳號池、粘性會話、負載感知調度、額度門檻暫停與失敗換帳號。下游以 `sk-` 開頭 API key 發放，key 綁使用者＋分組；**沒有** key 層級的 metadata／標籤欄位，「哪台機器／哪個角色／哪個 agent」要靠「一消費者一 key ＋ 分組 ＋ 自訂使用者屬性」自行約定，用量紀錄（`usage_logs`）則記到 key／group／account／user_agent／IP／session_id，可事後歸因。授權 LGPL-3.0（or later）＋ CLA（作者可再授權閉源），專案非常活躍但 issue 積壓也很大；最大風險是上游訂閱條款（README 自己明文警告）。

---

## 1. 架構與部署方式

### 1.1 語言與技術棧

- 後端：Go（`backend/go.mod` 宣告 `go 1.27.2`）＋ Gin（HTTP）＋ Ent（ORM，schema 在 `backend/ent/schema/`，41 個實體檔案）；README 標示 Go 1.27.0、Gin、Ent。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/go.mod
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
  - 出處：https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/backend/ent/schema
- 前端：Vue 3.4+、Vite 5+、TailwindCSS（`frontend/package.json`），以 `go build -tags embed` 內嵌進後端單一執行檔。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- 資料庫：PostgreSQL 15+；快取／佇列與粘性會話：Redis 7+。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- DB schema 由兩套並存維護：Ent schema＋292 支 `.sql` migration（`backend/migrations/` 目錄共 315 個項目；含分區、歷史資料修補、約束調整）。
  - 出處：https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/backend/migrations
- 主要資料表（由 migration `001_init.sql` 起）：`users`、`accounts`（上游帳號）、`groups`、`api_keys`、`account_groups`、`usage_logs`、`proxies`、`redeem_codes` 等。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/migrations/001_init.sql

### 1.2 部署方式

README 提供四種官方路徑：
1. **腳本安裝（systemd）**：`curl … install.sh | sudo bash`，裝到 `/opt/sub2api`、建 systemd service，首次開 `http://IP:8080` 走 Setup Wizard（DB／Redis／管理員帳號）。支援從 Dashboard 一鍵檢查更新／升級／回滾。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/install.sh
2. **Docker Compose（推薦）**：`docker-deploy.sh` 下載 `docker-compose.local.yml` 並自動生成 JWT_SECRET、TOTP_ENCRYPTION_KEY、POSTGRES_PASSWORD；一組 compose 檔同時起 sub2api＋PostgreSQL＋Redis。另有 named volumes 版與 standalone 版。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/docker-deploy.sh
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/docker-compose.local.yml
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/DOCKER.md
3. **Apple container（macOS 26 + Apple silicon）**：`./apple-container.sh init/up/status`，官方註明這是操作者本地流程，生產仍建議 Compose。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/APPLE_CONTAINER.md
4. **原始碼建置**：Node 建前端 → Go 以 `-tags embed` 編單一 binary → 首次啟動 Setup Wizard。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md

- 官方 Dockerfile 為多階段（node:24-alpine → golang:1.27.2-alpine → alpine:3.21，另附 postgres:18-alpine），最終映像含內嵌前端。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/Dockerfile
- 另有 **Simple Mode**（`RUN_MODE=simple`）：隱藏 SaaS 功能、跳過計費，適合個人／內部團隊快速使用。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- 安全周邊文件齊全：`deploy/EDGE_SECURITY.md`（CDN／轉發標頭信任邊界）、Caddyfile、config 範本（`.env.example` 23K、`config.example.yaml` 62K）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/EDGE_SECURITY.md
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/config.example.yaml
- CI：GitHub Actions 有 `backend-ci.yml`、`security-scan.yml`、`release.yml`、`cla.yml`。
  - 出處：https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/.github/workflows

---

## 2. 上游支援、帳號池、輪替與額度切換

### 2.1 上游平台清單（單一來源在程式碼）

`backend/internal/domain/platforms.go` 登記的平台（順序即 UI 顯示序）：Anthropic、OpenAI、Gemini、Antigravity、Grok、Kimi、Zhipu GLM、DeepSeek、MiniMax、OpenCode（`opencode_go`）、TypeSafe／Jev、Command Code、Cline；另有 `composite` 組合分組（管理員路由層，把請求模型解析到具體供應商）。
- 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/platforms.go
- 出處（composite 說明）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/docs/COMPOSITE_GROUPS.md

### 2.2 帳號型別（credentials 存 JSONB）

`backend/internal/domain/constants.go` 定義六種帳號型別：
- `oauth`：完整 OAuth（profile＋inference）
- `setup-token`：inference-only scope（Claude 訂閱的 setup token）
- `apikey`：官方 API key
- `upstream`：Base URL＋API Key 透傳
- `bedrock`：AWS Bedrock（SigV4 或 API key）
- `service_account`：Google Service Account（Vertex AI）
- 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/constants.go
- 帳號 schema（platform、type、credentials、extra、priority、concurrency、schedulable、rate_limited_at、overload_until、temp_unschedulable_until、session_window_* 等）：
  https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/account.go

### 2.3 各上游的實際支援

- **Claude 訂閱 OAuth**：內建 Claude OAuth（授權 URL `https://claude.com/cai/oauth/authorize`），並有 refresh token 更新機制；admin 路由另有 `apply-oauth-credentials`。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/pkg/oauth/oauth.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/server/routes/admin.go
  - 出處（token 刷新）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/token_refresher.go
- **Claude setup-token**：admin API 有 `POST /api/v1/admin/accounts/generate-setup-token-url`、`exchange-setup-token-code`、`setup-token-cookie-auth` 三個端點，走瀏覽器授權換 token 的流程。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/handler/admin/account_handler.go
- **Anthropic Console API key**：`apikey` 型別（含 native Anthropic base URL 校驗）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/anthropic_apikey_auth.go
- **OpenAI**：ChatGPT 訂閱 OAuth（`https://auth.openai.com/oauth/authorize`）＋ API key＋Responses／Codex 支援（含 Codex 模型目錄、WebSocket 入口）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/pkg/openai/oauth.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/openai_account_scheduler.go
- **Gemini**：Gemini OAuth 帳號、AI Studio API key、Vertex（`service_account`）；`/v1beta` 端點。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/vertex_service_account.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/platforms.go
- **Antigravity**：授權後提供 `/antigravity/v1/messages`（Claude 模型）與 `/antigravity/v1beta/`（Gemini 模型）；可選混合調度；README 警告 Anthropic Claude 與 Antigravity Claude 不可混在同一對話上下文，需用分組隔離。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- **Grok／xAI**：OAuth 訂閱帳號（PKCE，走 `cli-chat-proxy.grok.com`）與 xAI API key 兩種；支援 Responses、Claude 相容 `/v1/messages`、圖片／影片生成。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- **國產 OpenAI 相容**：Kimi（Moonshot）、Zhipu GLM、DeepSeek、MiniMax；各有「按量付費 payg」與「Coding Plan」模式，另有 `api_protocol`（chat_completions／anthropic／responses／adaptive）決定轉發端點。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/constants.go
- **OpenCode Zen／Go**：平台 `opencode_go`，模式 `zen`（按量，`https://opencode.ai/zen/v1`）與 `go`（訂閱窗口，`https://opencode.ai/zen/go/v1`），原生 Anthropic 端點可直接餵 Claude Code。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/constants.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/account/credentialsBuilder.ts
- **Ollama Cloud**（與我們直接相關）：帳號以瀏覽器 Cookie（`wos-session`）加密落庫，定時抓 `ollama.com` 官方設定頁解析用量（5h／7d 百分比、餘額、模型清單）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/ollama_cloud_usage_parser.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/i18n/locales/zh/admin/accounts.ts
- **TypeSafe／Jev**：原生 System One 協議（`/v1/systemone`），非串流、不與 Chat Completions 相容。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- **Command Code、Cline**：多模型聚合平台，含積分／訂閱窗口用量探測。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/command_code_usage.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/cline_usage.go
- **AWS Bedrock**：`bedrock` 型別（SigV4 或 API key），有專屬串流轉發實作。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/gateway_bedrock.go

### 2.4 帳號池與分組

- `accounts` ↔ `groups` 是多對多（中間表 `account_groups`，可設組內 priority）；一個分組可掛多個帳號，一台 worker 的 key 綁一個分組即等於綁一池帳號。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/migrations/001_init.sql
- 帳號層級可設：`priority`（越小越優先）、`concurrency`（並發上限）、`rate_multiplier`（帳號計費倍率）、`schedulable`、過期時間、proxy。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/account.go
- 管理面支援批量建立／批量更新憑證／批量刷新／批量清錯（`POST /admin/accounts/batch*`），以及從 CRS（另一套 Claude relay）匯入帳號（`crs_sync_service.go` 會讀 export 並比對 `crs_account_id`）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/server/routes/admin.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/crs_sync_service.go

### 2.5 调度（輪替）與黏性會話

- 入口 `SelectAccountWithLoadAwareness`：以 `group + session hash + requested model` 選帳號，分層決策：
  1. 模型路由（分組設定的 model→account 清單）優先；
  2. 粘性會話（Redis 記 `session_hash → account_id`，TTL 可設；Nginx 需開 `underscores_in_headers`，因為 session 標頭含底線）；
  3. 負載感知挑選（併發槽、等待計畫、分數排序）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/gateway_scheduling.go
  - 出處（TTL 設定）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/config/config.go
  - 出處（Nginx 註記）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- 調度分數包含：優先級、負載、排隊、錯誤率、首包延遲（first-token latency）、重置窗口、額度餘量、計費倍率；粘性加分另計。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/i18n/locales/zh/admin/accounts.ts
- 候選過濾條件（逐步排除）：排除清單、可調度、利潤閘門、平台相符、模型支援（含 model mapping）、渠道限制、模型限流、配額、窗口費用、RPM。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/gateway_scheduling.go
- 失敗重試：上游可重試錯誤（401/402/403/429/529/5xx/transport）會把該帳號加入 exclusion 再選下一個；`apikey` 帳號可開 `pool_mode`（同帳號自訂重試次數與狀態碼清單，預設 401/403/429）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/account.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/ratelimit_service.go

### 2.6 額度用完時的行為

- **429／過載**：記錄 `rate_limited_at`／`rate_limit_reset_at`；529 設 `overload_until`；期間調度器跳過該帳號；另有 `temp_unschedulable_until`＋原因字串（JSON，欄位如 `used_percent`、`threshold_percent`、`until_unix`、`source`、`error_message`）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/account.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/account_scheduling_threshold_reason.go
- **額度門檻暫停**：可為平台設 `account_scheduling_threshold`（例如用到 90% 就先暫停該帳號到窗口重置），支援 Anthropic／OpenAI／Grok／Kimi／Zhipu／MiniMax／OpenCode／Command Code；Anthropic 的窗口資料來自官方 `https://api.anthropic.com/api/oauth/usage`，其餘來自回應標頭被動採樣（`anthropic-ratelimit-unified-5h-utilization` 等）或各平台用量 API。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/account_scheduling_threshold_eval.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/repository/claude_usage_service.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/ratelimit_service.go
- **上游窗口語意**：5h／7d 是上游（OpenAI ChatGPT、Claude 等）官方對帳號的滾動限制，sub2api 無法解除，只做記錄與避讓。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/i18n/locales/zh/admin/accounts.ts
- **被動＋主動探測**：各平台另有 usage 探測器，例如 OpenCode Go `https://opencode.ai/zen/go/v1/usage`、Cline `/api/v1/users/me/plan/usage-limits`、Command Code `/alpha/usage/summary`、智譜 `/api/monitor/usage/quota/limit`。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/opencode_go_usage.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/cn_provider_quota_service.go

---

## 3. 下游發 key：「哪台機器／哪個角色／哪個 agent」能不能區分

### 3.1 Key 的資料模型

`api_keys`（Ent schema）欄位：
- `user_id`（必）、`key`（唯一，系統生成，預設前綴 `sk-`）、`name`、`group_id`（可空）、`status`
- 配額：`quota`／`quota_used`（USD，0=不限）
- 速率窗：`rate_limit_5h`／`rate_limit_1d`／`rate_limit_7d` 與 `usage_5h`／`usage_1d`／`usage_7d`＋window 起點
- `expires_at`、`ip_whitelist`／`ip_blacklist`（CIDR 陣列）、`last_used_at`
- 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/api_key.go
- 出處（前綴 `sk-`）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/api_key_service.go

**結論：key 上沒有任何 free-form metadata／labels／tags 欄位。** 可用的「標籤」只有 `name`（100 字）＋ `group_id`＋user 歸屬。

### 3.2 分組（group）作為區分維度

`groups` 欄位極多，可當「用途／角色」隔離層：`platform`（該組走哪個上游平台）、`rate_multiplier`、日／週／月 USD 上限、`model_allowlist`、`model_routing`（模型→帳號）、`claude_code_only`＋`fallback_group_id`（非 Claude Code 請求降級到別組）、`supported_model_scopes`、`rpm_limit`、`mcp_xml_inject`、`is_exclusive`、圖片／影片獨立計價等。
- 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/group.go

### 3.3 使用者層級的「自訂屬性」＝最接近 metadata 的機制

- `user_attribute_definitions`＋`user_attribute_values`：管理員可定義任意 key（型別：text／textarea／number／email／url／date／select／multi_select，含驗證規則），掛在 **user** 上，例如 `role`、`machine`、`agent_name`。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/user_attribute_definition.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/user_attribute_value.go
  - 型別列舉的前端定義（`UserAttributeType`）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/types/index.ts （另見 `frontend/src/components/user/UserAttributesConfigModal.vue`）
- 使用者另有 `allowed_groups` 白名單與每平台日／週／月 USD 配額（`user_platform_quotas`）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/user_platform_quota.go

### 3.4 實務上如何區分「哪台機器／哪個角色／哪個 agent」

Sub2API 沒有 first-class 的機器／角色概念，建議模式（由其資料模型可推得）：
- **一消費者一 key**：每台機器／每個 agent 各發一把 key，`name` 當標籤（如 `worker-a/planner`），用量歸因靠 `usage_logs.api_key_id`。
- **一角色一分組**：不同角色給不同 group（可各自限制平台、模型白名單、倍率、RPM），key 綁 group。
- **一角色一 user**：需要 metadata 時用 user＋自訂屬性；缺點是管理單位變成 user 而非 key。
- 請求層面的線索：`usage_logs` 記 `user_agent`、`ip_address`、`session_id`（來自 `X-Session-Id`／`X-Conversation-ID`／`X-Claude-Code-Session-Id` 等標頭，經清洗、上限 255 字）；Claude Code 客戶端可用 UA＋`metadata.user_id` 格式識別（用來做 `claude_code_only` 限制）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/session_id.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/gateway_upstream_response.go
- 粘性路由本身也是用 `metadata.user_id`／會話標頭導出 session hash，與歸因用的 session_id 是不同用途、不同欄位。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/gateway_service.go

---

## 4. 用量紀錄：資料模型、匯出、API

### 4.1 `usage_logs` 資料模型（append-only）

Ent schema `backend/ent/schema/usage_log.go`＋歷史 migration 的完整欄位大致如下：
- 關聯：`user_id`、`api_key_id`、`account_id`、`group_id`、`subscription_id`、`channel_id`
- 請求：`request_id`、`inbound_endpoint`、`upstream_endpoint`、`model`、`requested_model`、`upstream_model`、`upstream_response_model`、`upstream_model_mismatch`、`model_mapping_chain`、`billing_tier`、`billing_mode`、`reasoning_effort`／`requested_reasoning_effort`、`stream`、`openai_ws_mode`
- Token：`input_tokens`、`output_tokens`、`cache_creation_tokens`、`cache_read_tokens`、`cache_creation_5m_tokens`、`cache_creation_1h_tokens`；圖片 `image_count`／`image_input_tokens`／`image_output_tokens` 等；影片 `video_count`／`video_resolution`／`video_duration_seconds`
- 成本：`input_cost`、`output_cost`、`cache_creation_cost`、`cache_read_cost`、`image_*_cost`、`total_cost`、`actual_cost`、`account_stats_cost`、`rate_multiplier`、`account_rate_multiplier`、`long_context_billing_applied`、`cache_ttl_overridden`
- 上下文：`user_agent`（512）、`ip_address`（45，IPv6）、`session_id`（255）、`duration_ms`、`first_token_ms`
- 時間：只有 `created_at`（Immutable，append-only）
- 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/usage_log.go
- 個別欄位的 migration 出處：https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/backend/migrations（`028_add_usage_logs_user_agent.sql`、`031_add_ip_address.sql`、`187_add_usage_log_session_id.sql` 等）

- 保留策略：支援月分區（`035_usage_logs_partitioning.sql`，僅在已分區時自動建分區，轉換成分區需人工計畫）；有 `usage_cleanup_tasks` 表與 admin API 可建清理任務（含取消審計）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/migrations/035_usage_logs_partitioning.sql
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/migrations/042_add_usage_cleanup_tasks.sql

### 4.2 API

- 使用者端：`GET /api/v1/usage`（List）、`/usage/:id`、`/usage/stats`、`/usage/errors`、`/usage/dashboard/*`，及 `GET /user/api-keys/:id/usage/daily`。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/server/routes/user.go
- 管理端：`GET /api/v1/admin/usage`（List，支援 `start_date`／`end_date` 等篩選）、`/usage/stats`、`/usage/search-users`、`/usage/search-api-keys`、清理任務 CRUD；帳號維度另有 `GET /admin/accounts/:id/usage`、`POST /admin/accounts/usage/batch`。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/server/routes/admin.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/handler/admin/usage_handler.go
- 外部系統整合可申請 **Admin API Key**（`x-api-key: admin-<64hex>`）呼叫 admin 路由（目前文件主要示範支付／充值，但同一認證機制適用查詢類端點）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/docs/ADMIN_PAYMENT_INTEGRATION_API.md
- 另有 dashboard 用的聚合表（`026_ops_metrics_aggregation_tables.sql`、`034_usage_dashboard_aggregation_tables.sql`）。
  - 出處：https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/backend/migrations

### 4.3 匯出

- **沒有**usage_logs 的 CSV／JSON 匯出端點。全 repo CSV 匯出只用在兌換碼（`GET /admin/redeem-codes/export`），另有帳號／代理的 JSON 匯出是給搬家遷移用。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/handler/admin/redeem_handler.go
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/handler/admin/account_data.go
- 要匯出用量得直接查 PostgreSQL（資料表本身完整），或走 admin API 自行翻頁蒐集。

---

## 5. Claude Code 與 opencode 接入

### 5.1 Claude Code

- 環境變數（Unix／CMD／PowerShell 三種殼）：
  ```
  ANTHROPIC_BASE_URL="<Sub2API 網址>"
  ANTHROPIC_AUTH_TOKEN="sk-…"
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  ```
  或寫入 `~/.claude/settings.json` 的 `env` 區塊。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/keys/UseKeyModal.vue
- Antigravity 專用端點：`ANTHROPIC_BASE_URL="http://host:8080/antigravity"`。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- 非 Anthropic 平台（如 Grok、DeepSeek、MiniMax、composite、OpenAI 開 `allow_messages_dispatch`）也可用 Claude Code tab：把 `ANTHROPIC_MODEL`／`ANTHROPIC_DEFAULT_*_MODEL`／`CLAUDE_CODE_SUBAGENT_MODEL` 一併指到對應模型。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/keys/UseKeyModal.vue
- UI 的「Use Key」彈窗會依平台給對應教學 tab：Claude Code／Codex CLI／Codex WS／Gemini CLI／Grok CLI／OpenCode／System One。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/keys/UseKeyModal.vue

### 5.2 opencode

- 產出 `opencode.json`（含 `$schema: https://opencode.ai/config.json`），provider 區塊範例：
  ```json
  {
    "provider": {
      "<platform>": {
        "options": { "baseURL": "<Sub2API 網址>", "apiKey": "sk-…" },
        "npm": "@ai-sdk/anthropic|@ai-sdk/google|@ai-sdk/openai-compatible",
        "models": { "…": { "limit": { "context": …, "output": … } } }
      }
    }
  }
  ```
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/keys/UseKeyModal.vue
- 各平台對應：anthropic／openai／gemini／grok／antigravity-claude／antigravity-gemini 都有對應 provider 與模型清單；Gemini 用 `geminiBase`，Antigravity 分 Claude／Gemini 兩個 provider（避免上下文混用）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/keys/UseKeyModal.vue
- Codex CLI（config.toml）、Gemini CLI（env）、Grok CLI（`~/.grok/config.toml`）也有現成配置產生器。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md

---

## 6. 授權條款、維護狀況、風險

### 6.1 授權

- **LGPL-3.0 or later**，Copyright (c) 2026 Wesley Liddick；GitHub 授權偵測亦為 LGPL-3.0。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/LICENSE
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
  - 出處：https://api.github.com/repos/Wei-Shaw/sub2api
- **CLA（Individual Contributor License Agreement v1.0）**：貢獻者授權 Project Owner「irrevocable」著作權／專利授權，且明示 **可用任何授權（含 proprietary／commercial）再授權**、可轉讓給任何第三人，並放棄著作人格權。白話：作者保留把專案（含社群貢獻）拿去閉源／商業雙授權的權利。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/CLA.md
  - CLA 由 `cla.yml` 工作流在 PR 上強制簽署：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/.github/workflows/cla.yml
- LGPL 對我們自架、不下發 binary 的內部使用基本無額外義務；但若未來把修改版隨產品散布，需注意 LGPL 的原始碼提供與可替換性要求。

### 6.2 維護狀況（2026-10-10 觀測）

- Repo 建立 2025-12-18，最新 release `v0.2.15`（2026-10-09），`pushed_at` 2026-10-10；截至 2026-10-10 共 59 個 release（`releases?per_page=1` 的 Link header last 為第 59 頁）；近 30 天 ≥100 個 commit（API 取樣上限）。
  - 出處：https://api.github.com/repos/Wei-Shaw/sub2api
  - 出處：https://api.github.com/repos/Wei-Shaw/sub2api/releases
- 43,617 stars、9,359 forks、91 watchers、**3,568 open issues**；主貢獻者 Wei-Shaw（2,782 commits），前幾名貢獻者含 touwaeriol 等（≥100 位貢獻者，API 首頁）。
  - 出處：https://api.github.com/repos/Wei-Shaw/sub2api
  - 出處：https://api.github.com/repos/Wei-Shaw/sub2api/contributors
- 節奏：幾乎每 1–5 天一版、主線單一 `main` 分支；版本號 0.x，無 LTS 承諾；更新可在 Dashboard 一鍵或腳本重跑。
  - 出處：https://github.com/Wei-Shaw/sub2api/releases
- 周邊：內建支付系統（EasyPay／支付寶／微信／Stripe）、贊助商一排（多為轉售帳號／中轉站），生態裡有第三方行動 App（sub2api-mobile）。
  - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md

### 6.3 風險

1. **上游條款風險（最大）**：README 明文警告「使用本專案可能違反 Anthropic 及其他上游供應商的服務條款」，風險自負；repo 描述直接寫「拼車共享」——把訂閱額度轉發／共享給多個下游，正是 Anthropic 消費者條款與 Usage Policy 通常不允許的型態。帳號被封的損失自負。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
   - 出處：https://github.com/Wei-Shaw/sub2api
   - Anthropic 官方條款頁（未逐條比對）：https://www.anthropic.com/legal/consumer-terms 、https://www.anthropic.com/legal/aup
2. **商業使用定位模糊**：README 聲明「開發者從未授權任何人以本專案從事任何形式的商業營運」，以此專案名義的商業活動與開發者無關。拿來做公司內部分配工具雖非「以本專案名義營運」，仍需自行判斷。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
3. **機密集中風險**：系統本質是憑證保險庫——DB 內存 OAuth refresh token／API key（`credentials` JSONB；Ollama cookie 加密；JWT_SECRET、TOTP_ENCRYPTION_KEY 為必設密鑰）。外洩即等於所有上游帳號失守；官方有 `EDGE_SECURITY.md` 要求把原站鎖在 CDN／代理後面。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/account.go
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/deploy/EDGE_SECURITY.md
4. **CLA＋雙授權**：作者可把含社群貢獻的程式碼閉源再授權；社群版與商業版可能分岔。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/CLA.md
5. **運維負擔**：0.x 快速迭代、3,568 open issues、schema 頻繁變動（292 支 migrations）；升級需要跟版，且用量表可能需人工轉分區。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/migrations/035_usage_logs_partitioning.sql
6. **上游 API 依賴**：不少功能靠逆向／非公開端點（`/wham/usage`、`cli-chat-proxy.grok.com`、各站 usage scraping），上游一改就壞，屬社群快速跟修的灰色地帶。
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/ollama_cloud_usage_parser.go
   - 出處：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/openai_gateway_usage.go

---

## 7. 對 LLMGateway 的初步評估

- 需求對位：**上游**（Claude OAuth／setup-token、ChatGPT、Gemini、OpenCode Zen/Go、Ollama Cloud…）覆蓋面遠超本專案目前所需；**下游**發 key＋分組＋配額也現成。
- 主要缺口在「哪台 worker／什麼角色／哪個 coding agent」的 first-class 模型：Sub2API 只有 key（name）＋group＋user attributes 可用，沒有 key-level metadata／tag；若我們要嚴謹的「worker×角色×訂閱×agent×模型」申請與紀錄，需要在 LLMGateway 自己這層補（或約定命名規約＋自訂屬性）。
- 用量資料模型完整（token 級、成本、倍率、UA、IP、session），且有 REST API，但沒有內建匯出；整合時直接查 DB 或走 Admin API。
- 若作為「參考實作」吸收概念（粘性調度、quota threshold、多協議轉換）風險低；若直接部署自用，需先解 Anthropic 條款與憑證保管兩個風險，且要預期頻繁跟版。

---

## 附錄：主要來源

- Repo 首頁：https://github.com/Wei-Shaw/sub2api
- README（功能／部署／Antigravity／Grok／TypeSafe）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/README.md
- 平台清單：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/platforms.go
- 帳號型別常數：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/domain/constants.go
- 調度核心：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/gateway_scheduling.go
- 額度門檻：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/account_scheduling_threshold_eval.go
- 限流／視窗：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/ratelimit_service.go
- Account schema：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/account.go
- API key schema：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/api_key.go
- Group schema：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/group.go
- Usage log schema：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/usage_log.go
- 使用者自訂屬性：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/ent/schema/user_attribute_definition.go
- Claude OAuth：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/pkg/oauth/oauth.go
- Claude 用量：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/repository/claude_usage_service.go
- 路由（admin／user／gateway）：https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/backend/internal/server/routes
- Use Key 彈窗（Claude Code／opencode 配置產生器）：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/keys/UseKeyModal.vue
- LICENSE：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/LICENSE
- CLA：https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/CLA.md
