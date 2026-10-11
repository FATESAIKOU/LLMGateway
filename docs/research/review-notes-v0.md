# 審查意見 v0：upstream-claude-code-compat.md／quota-signals.md 出處對照

> 方法（本人指定）：只讀公開的官方文件與公開網頁；未讀任何憑證檔，未跑 `gh`、未 `git commit`、未打需要金鑰的 API。
> 對照基準日：2026-10-11。查不到或未驗證的寫「截至 2026-10-11 查無／未驗證」，不臆測。
> 每條格式：檔名＋段落 → 問題 → 正確說法＋出處 → 嚴重度（高／中／低）。
> 嚴重度定義：高＝會讓監督程式誤判／誤動；中＝指引方向誤導；低＝引用精度、用詞、版本沿革等不影響行為的小問題。

## 0. 總結論與 PM 四問的直接回答

- 兩份文件整體正確，官方／非官方標註誠實；本次未發現高嚴重度問題（會弄壞監督程式的錯誤寫法），以下問題皆為低嚴重度。
- Q1「Ollama Cloud 的 `GET /api/balance` 是不是官方文件寫的、還在不在」：是官方文件，且還在。`https://docs.ollama.com/api/balance` 有完整說明（含現行 `included.balance_usd/allowance_usd/period`＋`purchased`，以及 Legacy plan 的 `included.session/weekly` 各含 `remaining_percent`＋`resets_at`；限 10 req/min，429 附 `Retry-After`）。quota-signals §4.1 對 balance 的描述正確。出處：https://docs.ollama.com/api/balance
- Q2「OpenRouter 與 Ollama Cloud 的 Claude Code 接法（網址、變數）」：兩份文件寫的與官方頁一致。OpenRouter：`ANTHROPIC_BASE_URL=https://openrouter.ai/api`（不含 `/v1`）＋`ANTHROPIC_AUTH_TOKEN` 放 OpenRouter key＋`ANTHROPIC_API_KEY` 顯式清空，另有 `ANTHROPIC_DEFAULT_*`、`CLAUDE_CODE_SUBAGENT_MODEL`、`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`、`CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK` 等變數。出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration。Ollama Cloud：`ollama launch claude` 一鍵，或 `ANTHROPIC_BASE_URL=https://ollama.com`＋`ANTHROPIC_AUTH_TOKEN=$OLLAMA_API_KEY`＋`ANTHROPIC_API_KEY=""`；本機 `ANTHROPIC_AUTH_TOKEN=ollama`（required but ignored）。出處：https://docs.ollama.com/integrations/claude-code、https://docs.ollama.com/api/anthropic-compatibility、https://docs.ollama.com/api/introduction
- Q3「opencode 的 `FreeUsageLimitError`／`session.error` 是官方文件還是只在原始碼或 issue」：`FreeUsageLimitError` 只在原始碼與 issue／PR，不在官方文件（quota-signals 標註正確）。原始碼 `packages/opencode/src/session/retry.ts` 在 tag `v1.18.21` 確實存在，`GO_UPSELL_MESSAGE = "Free usage exceeded, subscribe to Go"`、429＋`retry-after`／`retry-after-ms`、`RETRY_INITIAL_DELAY=2000`／×2／無 header 上限 30s／`RETRY_MAX_RETRIES=5` 皆與文件 §2.1 相符；「retry-after 最長約 24h」的直接出處是已合併的 PR #49195（2026-09-16 merged）。`session.error` 的事件名稱本身在官方 plugins 文件 Session Events 有列，但其 payload 結構（`APIError{message,statusCode,…}`）只在第三方整理文件，文件標註正確。出處：https://raw.githubusercontent.com/anomalyco/opencode/v1.18.21/packages/opencode/src/session/retry.ts、https://github.com/anomalyco/opencode/pull/49195、https://opencode.ai/docs/plugins/
- Q4「agy 的額度訊息出處」：`⚠ Individual quota reached. … Resets in 143h57m55s.` 逐字見於公開 issue #789；`Contact your administrator to enable overages` 變體與 `/usage` 面板格式（`40% remaining · Refreshes in …`／`Quota available`）逐字見於 issue #46；官方 `/usage`（https://www.antigravity.google/docs/cli/commands/usage/）與 headless（https://www.antigravity.google/docs/cli/headless/）兩頁確實未列 quota 錯誤字串。文件標註正確。出處：https://github.com/google-antigravity/antigravity-cli/issues/789、https://github.com/google-antigravity/antigravity-cli/issues/46

## 1. 已確認無誤的重點（抽查通過，不需改）

- OpenRouter 用量端點 `GET /api/v1/key` 與欄位（`usage*`／`byok_usage*`／`limit/limit_remaining/limit_reset`／`budgets[]`／`effective_budget`／`free_model_daily_requests`／`rate_limit` 已棄用）：兩種網址形式（`/docs/api/reference/limits` 與 `/docs/api_reference/limits`）皆可解析到同一官方頁，§4 與 §2.2 引用皆有效。出處：https://openrouter.ai/docs/api/reference/limits、https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key
- OpenRouter 402（`limit_source` 區分 in-flight／key／credits）／429（`error_type`、`provider_code`、`X-RateLimit-*`、`Retry-After`、串流中 `finish_reason:"error"`）：與官方 Limits 頁一致。出處：https://openrouter.ai/docs/api/reference/limits
- OpenRouter Anthropic 端點 `POST /messages`、OpenAPI servers `https://openrouter.ai/api/v1`、tools／tool_choice／stream SSE、官方只保證 Anthropic 1P、fast mode 限 Opus 4.6／4.7／4.8／5 且 Fable 無 fast tier：皆與官方頁一致。出處：https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message、https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration
- OpenRouter prompt caching（Anthropic `cache_control`、5 分／1 小時 TTL、寫入 1.25x／2x、讀取 0.1x、sticky routing）與 usage 自動附帶（串流最後一個 SSE）：與官方頁一致。出處：https://openrouter.ai/docs/features/prompt-caching、https://openrouter.ai/docs/guides/guides/usage-accounting
- Ollama Anthropic 相容矩陣（支援 tools／stream／thinking、不支援 `count_tokens`／`tool_choice`／`metadata`／caching／batches／citations／PDF、部分支援 base64 圖與 thinking budget）、base URL 表、雲端拒 `x-api-key`、拒 2023-06-01 以前版本：皆與官方頁一致。出處：https://docs.ollama.com/api/anthropic-compatibility、https://docs.ollama.com/api/introduction
- Ollama 雲端模型名（`glm-4.7:cloud`／`minimax-m2.1:cloud`／`gpt-oss:120b-cloud`／`qwen3-coder:480b-cloud`）出自 launch blog；「雲端跑滿完整 context」出自 blog／相容頁：引用正確。出處：https://ollama.com/blog/launch、https://ollama.com/blog/claude
- opencode 官方 providers 頁的 Ollama Cloud 段（Settings Keys 建 key、`/connect`、先 `ollama pull *-cloud`、再 `/models`）與文件 §2.6 一致。出處：https://opencode.ai/docs/providers
- Zen 端點表（Claude＋Qwen 走 `/v1/messages`）、`opencode/<id>` 寫法、`https://opencode.ai/zen/v1/models`、auto-reload（低於 $5 加 $20）、月限額、Claude 定價行 Cached Read／Write（如 Opus 4.5 讀 0.5／寫 6.25）：與現行官方 Zen 頁一致。出處：https://opencode.ai/docs/zen
- Claude Code 官方：四種 limit 訊息、85% 預警系（見 errors 索引）、auto-continue（v2.1.234＋、最多重掛兩次、`Automatic continue stopped…`、`autoContinueAtUsageLimit`，見 interactive-mode）、headless exit 0／非零＋stdout 結果、`system/init`→`stream_event`→`result`、`system/api_retry` 欄位與枚舉、`permission_denied`、hooks `StopFailure`（matcher 含 `rate_limit…cloud_credential_error…unknown`；輸入 `error`／`error_details`／`last_assistant_message`；輸出忽略）、Notification `quota_auto_resume_*`、statusline `rate_limits`（僅 Pro／Max、首個 API 回應後、`jq … // empty` 範例）、`/usage`（Session 區塊、方案用量條、60 分鐘 last-known 快照）、認證順位（…→`ANTHROPIC_AUTH_TOKEN`→`ANTHROPIC_API_KEY`→`apiKeyHelper`→`CLAUDE_CODE_OAUTH_TOKEN`→profile→`/login`）、setup-token（需 Pro／Max／Team／Enterprise、只能 model request、不能 Remote Control、 `--bare` 不讀）、`/status` 過期列（v2.1.210＋）：皆與官方頁一致。出處：https://code.claude.com/docs/en/errors、https://code.claude.com/docs/en/interactive-mode、https://code.claude.com/docs/en/headless、https://code.claude.com/docs/en/hooks、https://code.claude.com/docs/en/statusline、https://code.claude.com/docs/en/costs、https://code.claude.com/docs/en/authentication、https://code.claude.com/docs/en/llm-gateway-connect
- PR #49195（402 歸 `QuotaExceeded` 不再重試）狀態為 merged（2026-09-16），文件 §2.2「新版行為」表述成立。出處：https://github.com/anomalyco/opencode/pull/49195
- `anomalyco/opencode` 即上游（官方 troubleshooting 頁「Report issues」連到 `github.com/anomalyco/opencode/issues`，頁尾 © Anomaly），文件引用該 repo 的 issues／PR 屬於上游引用而非 fork。另：issue #42128（`Free usage exceeded, subscribe to Go [retrying attempt #2]`）與 hermes-agent #65563（Ollama 429 字串 `you (…) have reached your session usage limit, upgrade for higher limits: https://ollama.com/upgrade or add extra usage: https://ollama.com/settings`）皆存在且字串與文件相符。出處：https://opencode.ai/docs/troubleshooting/、https://github.com/anomalyco/opencode/issues/42128、https://github.com/NousResearch/hermes-agent/issues/65563

## 2. 問題條目

### R1. upstream-claude-code-compat.md §2.5：Ollama「串流中的 server-sent error 事件」列為明確不支援，但官方同頁的 streaming events 表把 `error` 標為支援

- 問題：文件寫「明確不支援：…串流中的 server-sent error 事件（錯誤改以 HTTP status 回傳）」。官方相容頁的確在「Not supported」表寫了 `Server-sent errors ｜ error events during streaming (errors return HTTP status)`，但同一頁的「Streaming events」清單卻把 `error` 標為 `[x]` 支援。官方文件自相矛盾，文件只採其中一種讀法且未註明。
- 正確說法＋出處：官方同頁兩處並存，應加註「官方文件此處不一致，監督程式若解析 Ollama 串流，以 HTTP status 為主、`error` 事件為輔」。出處：https://docs.ollama.com/api/anthropic-compatibility
- 嚴重度：低。

### R2. upstream-claude-code-compat.md §2.4：「context 建議 64k 以上」同時引用了寫 32K 的 blog

- 問題：文件寫「context 建議 64k 以上…（出處：…integrations/claude-code；…/blog/claude）」。integrations 頁確實寫 64k＋，但 blog 頁寫的是「at least 32K tokens」。
- 正確說法＋出處：64k 的出處只有 integrations 頁；blog 頁為 32K。建議把 blog 出處從該句移除或加註兩者差異。出處：https://docs.ollama.com/integrations/claude-code、https://ollama.com/blog/claude
- 嚴重度：低。

### R3. upstream-claude-code-compat.md §2.3：本機手設的 `ANTHROPIC_API_KEY=""` 引錯了頁

- 問題：文件把本機手設三變數（含 `ANTHROPIC_API_KEY` 設空）同時出處到 anthropic-compatibility 頁；該頁 env 區塊只列 `ANTHROPIC_AUTH_TOKEN=ollama`＋`ANTHROPIC_BASE_URL`，`ANTHROPIC_API_KEY=""` 出現在 integrations 頁的 inline 範例。
- 正確說法＋出處：行為本身無誤（照 integrations 頁 inline 範例設三個變數即可），只需把該子句的出處改為 integrations 頁。出處：https://docs.ollama.com/integrations/claude-code、https://docs.ollama.com/api/anthropic-compatibility
- 嚴重度：低。

### R4. quota-signals.md §4.1：「session／weekly 專用查詢端點…正式端點查無」易被誤讀，balance 已是正式端點

- 問題：文件一方面正確記載 `GET /api/balance` 含 legacy `session`／`weekly`（`remaining_percent`＋`resets_at`），另一方面又寫「session／weekly 專用查詢端點目前只有 feature request，正式端點…查無」。讀者可能誤以為沒有程式化查詢 legacy 配額的正式方法。
- 正確說法＋出處：官方 `/api/balance` 的 Legacy plans 段即為 legacy session／weekly 的正式查詢方式（回 `remaining_percent` 0–100 與 UTC `resets_at`）；「查無」僅指「獨立於 balance 之外的專用端點」。建議改寫為「無需另立專用端點，用 balance 即可」。出處：https://docs.ollama.com/api/balance。另：所引 #15132 實為要求 `used_percentage` 形式的新端點且已被標 duplicate（見 #12532），引用時建議加註其狀態。出處：https://github.com/ollama/ollama/issues/15132
- 嚴重度：低。

### R5. quota-signals.md §2.1：「retry-after 最長可達約 24h」在該段所列出處中找不到直接文字

- 問題：該段出處列 retry.ts＋issues，但 24h 上限的直接文字在 PR #49195 的對照表（`FreeUsageLimitError (daily cap, retry-after up to 24h)`），該 PR 未列於此段出處。
- 正確說法＋出處：補上 PR 出處即可，數字本身無誤。出處：https://github.com/anomalyco/opencode/pull/49195
- 嚴重度：低。

### R6. quota-signals.md §2.4：`opencode run hello --print-logs` 引的是舊 repo 位置的 issue

- 問題：出處為 `https://github.com/sst/opencode/issues/4327`。上游現為 `anomalyco/opencode`（官方 troubleshooting 頁的回報連結已指向 anomalyco），sst 連結至多隻剩重定向。
- 正確說法＋出處：建議改引 anomalyco 位置或先確認重定向有效。出處：https://opencode.ai/docs/troubleshooting/
- 嚴重度：低。

### R7. quota-signals.md §4.2：Console CSV 匯出（dev.opencode.ai）的出處未能靜態驗證

- 問題：出處 `https://dev.opencode.ai/console/guides/usage` 以純靜態抓取只回「OpenCode Console」字樣（疑為 JS 渲染頁），`GET /api/v1/usage/export`＋service account key 的說法未能驗證。
- 正確說法＋出處：截至 2026-10-11 查無可靜態驗證的公開文字；該條建議改標「未驗證」或補其他可驗出處。
- 嚴重度：低。

### R8. quota-signals.md §1.1：gateway spend-limit 字串的版本沿革僅在索引層級確認

- 問題：`spend limit reached (daily; resets 2026-08-09 00:00 UTC)`（小寫；舊版只有 `spend limit reached`）的 keys 見於官方 errors 索引（`spend limit reached`／`spend limit unavailable`），但其內文的精確格式與新舊版差異本次未逐字驗證（該頁過大）。
- 正確說法＋出處：出處成立限於索引層級；建議該條加註「格式細節未逐字重驗」。出處：https://code.claude.com/docs/en/errors
- 嚴重度：低。

### R9. quota-signals.md §3.1：「官方文件未列出額度錯誤字串」的驗證範圍只有兩頁

- 問題：文件寫「官方 docs 只有 `/usage` 與 headless 文件，未列 quota 錯誤訊息」。本次確認 `/usage`（https://www.antigravity.google/docs/cli/commands/usage/）與 headless（https://www.antigravity.google/docs/cli/headless/）兩頁確無 quota 字串，但未翻閱其餘官方頁（如 Plans／AI credits、Troubleshooting）。
- 正確說法＋出處：將該句收斂為「`/usage` 與 headless 兩頁未列」，其餘頁面截至 2026-10-11 查無（未全面翻閱）。出處：上列兩網址。
- 嚴重度：低。

### R10. upstream-claude-code-compat.md §3.2：兩個特定社群 repo 的 URL 未逐一開啟驗證

- 問題：`https://github.com/KoLDXr00T/cc-with-oc-zen`、`https://github.com/kqrege/zen-claude-bridge`（及 §3.3 的 techwithburhan 指南）本次未逐一開啟；僅確認「Claude Code 經社群 proxy 接 Zen」此類專案普遍存在（如 oc-go-cc、zenbridge 等皆為非官方）。
- 正確說法＋出處：「社群接法存在且皆非官方」的結論成立；三個 URL 建議使用前逐一點開確認。出處：https://opencode.ai/docs/zen（官方僅記載 opencode `/connect` 路線，無原版 Claude Code 接法，佐證「官方未說明」）。
- 嚴重度：低。

## 3. 本次未逐一驗證的引用（方法學揭露，非問題）

- opencode issues #42029／#47613／#45324／#42074／#45989／#47686、ollama issues #15663／#16448、opencode #10448／#10447／#44189：未逐一開啟；其中 #42128、PR #49195、hermes-agent #65563、ollama #15132、agy #789／#46 已開啟並逐字相符。
- gist（thomaslty）、IvanMurzak、joshuadavidthomas event-schemas、runman.ai troubleshoot、OpenRouter Zendesk 限流文：未開啟；相關數字（免費模型 20 RPM／50・1000 RPD）在官方 Limits 頁已有對應文字。出處：https://openrouter.ai/docs/api/reference/limits
- `/zen/go/v1/usage` 是否為穩定公開端點：文件 §6 已自行列為未確認，本次維持該結論。

## 4. 出處清單（本次實際讀過的公開網頁）

- https://docs.ollama.com/api/balance
- https://docs.ollama.com/api/cloud-usage
- https://docs.ollama.com/api/errors
- https://docs.ollama.com/api/introduction
- https://docs.ollama.com/api/anthropic-compatibility
- https://docs.ollama.com/integrations/claude-code
- https://docs.ollama.com/integrations/opencode
- https://ollama.com/blog/claude
- https://ollama.com/blog/launch
- https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration
- https://openrouter.ai/docs/guides/guides/coding-agents/claude-code-integration
- https://openrouter.ai/docs/cookbook/coding-agents/opencode-integration
- https://openrouter.ai/docs/api/reference/limits
- https://openrouter.ai/docs/api_reference/limits
- https://openrouter.ai/docs/api_reference/errors-and-debugging
- https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key
- https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message
- https://openrouter.ai/docs/features/prompt-caching
- https://openrouter.ai/docs/guides/guides/usage-accounting
- https://opencode.ai/docs/zen
- https://opencode.ai/docs/plugins/
- https://opencode.ai/docs/providers
- https://opencode.ai/docs/troubleshooting/
- https://code.claude.com/docs/en/errors
- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/statusline
- https://code.claude.com/docs/en/costs
- https://code.claude.com/docs/en/authentication
- https://code.claude.com/docs/en/interactive-mode
- https://code.claude.com/docs/en/llm-gateway-connect
- https://www.antigravity.google/docs/cli/commands/usage/
- https://www.antigravity.google/docs/cli/headless/
- https://raw.githubusercontent.com/anomalyco/opencode/v1.18.21/packages/opencode/src/session/retry.ts
- https://github.com/anomalyco/opencode/pull/49195
- https://github.com/anomalyco/opencode/issues/42128
- https://github.com/ollama/ollama/issues/15132
- https://github.com/NousResearch/hermes-agent/issues/65563
- https://github.com/google-antigravity/antigravity-cli/issues/789
- https://github.com/google-antigravity/antigravity-cli/issues/46
- https://dev.opencode.ai/console/guides/usage（僅確認頁面存在，內容未能靜態驗證）
