# 上游 Claude Code 相容性研究：OpenRouter、Ollama Cloud、OpenCode Zen

- 研究日期：2026-10-11（以下「查無」均以此日期為準）。
- 方法與限制：只讀公開的官方文件與公開網頁；未讀任何憑證檔，未執行需要金鑰的 API，未使用 `gh`，未做 `git commit`。
- 背景知識（Anthropic 官方對 gateway 變數的定義）：`ANTHROPIC_BASE_URL` 指向 gateway；`ANTHROPIC_AUTH_TOKEN` 以 `Authorization: Bearer` 送出，適用於 bearer-token 類 gateway；`ANTHROPIC_API_KEY` 以 `x-api-key` 送出；兩者放錯會拿到 `401`（出處：https://code.claude.com/docs/en/llm-gateway-connect）。
- 同一頁也說明驗證方式是直接對 `$ANTHROPIC_BASE_URL/v1/messages` 發一個最小的 messages 請求（出處：https://code.claude.com/docs/en/llm-gateway-connect）。
- 同一文件說明 `ANTHROPIC_AUTH_TOKEN` 會立即覆寫已存的登入，`ANTHROPIC_API_KEY` 在互動模式會先問一次才接管；可用 `/logout` 清掉舊登入、`/status` 確認生效中的 base URL 與憑證來源（出處：https://code.claude.com/docs/en/llm-gateway-connect）。

## 1. OpenRouter：原版 Claude Code 可直接接上

結論：可以。官方文件明確寫「把 `ANTHROPIC_BASE_URL` 指到 OpenRouter，Claude Code 直接用原生 Anthropic 協定對話，不需要本機 proxy」（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。

### 1.1 官方 Anthropic 相容端點與網址

- Claude Code 用的 base URL 是 `https://openrouter.ai/api`（注意：給 Claude Code 的是這個，不含 `/v1`；Claude Code 會自己加上 `/v1/messages`）（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- OpenRouter 的 Anthropic Messages API  raw 端點是 `POST https://openrouter.ai/api/v1/messages`，官方寫明「用 Anthropic Messages API 格式建立訊息，支援文字、圖片、PDF、tools、extended thinking」（出處：https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message）。
- 同一頁的 OpenAPI `servers` 寫 `https://openrouter.ai/api/v1`（出處：https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message）。
- 官方把這層稱為「Anthropic Skin」，行為與 Anthropic API 一致，會處理模型對應，並透傳 Thinking 區塊與原生 tool use（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。

### 1.2 官方「Claude Code 整合」說明頁

- 有。官方說明頁：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 站內另有一份內容基本相同的指南頁 https://openrouter.ai/docs/guides/guides/coding-agents/claude-code-integration，本研究以 cookbook 版為主（出處：https://openrouter.ai/docs/guides/guides/coding-agents/claude-code-integration）。

### 1.3 要設的環境變數（只列變數名）

- 必需：`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY`（官方要求顯式設為空字串，避免與直接連 Anthropic 的憑證衝突）（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 官方範例另用 `OPENROUTER_API_KEY` 存放 OpenRouter 金鑰，再指派給 `ANTHROPIC_AUTH_TOKEN`（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 官方說明選 `ANTHROPIC_AUTH_TOKEN` 的理由：OpenRouter 是 bearer-token gateway，而 Claude Code 把 `ANTHROPIC_AUTH_TOKEN` 以 `Authorization: Bearer` 送出；`ANTHROPIC_API_KEY` 則走 `x-api-key` 並被視為直連 Anthropic 的憑證（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 模型覆寫（選用）：`ANTHROPIC_DEFAULT_FABLE_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`CLAUDE_CODE_SUBAGENT_MODEL`（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- Gateway 模型探索（選用）：`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- Fast mode 跳過組織檢查（Opus fast 專用，選用）：`CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK`（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 官方提醒：不要把這些變數放進專案 `.env`，原生安裝的 Claude Code 不讀標準 `.env`；要用 shell profile 或 `.claude/settings.local.json` 的 `env` 區塊（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 官方提醒：若之前用 Anthropic 帳號登入過，要先跑 `/logout` 清掉快取登入，再重開 `claude`；用 `/status` 確認 Auth token 顯示 `ANTHROPIC_AUTH_TOKEN`、base URL 顯示 `https://openrouter.ai/api`（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。

### 1.4 可用模型

- 官方保證的是 Anthropic 第一方 provider（Anthropic 1P）；文件建議把 Anthropic 1P 設為最高優先 provider（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 官方明說「Claude Code 為 Anthropic 模型最佳化，用其他 provider 的模型可能無法正常運作」（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 模型覆寫範例使用 `~anthropic/claude-fable-latest[1m]`、`~anthropic/claude-opus-latest[1m]`、`~anthropic/claude-sonnet-latest[1m]`、`~anthropic/claude-haiku-latest` 等別名；`[1m]` 標記是讓 Claude Code 假設 1M context，OpenRouter 會在路由前剝掉該標記（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- Fast mode 只支援 `Claude Opus 4.6`、`Claude Opus 4.7`、`Claude Opus 4.8`、`Claude Opus 5`，且只由 Anthropic 第一方提供；Claude Fable 沒有 fast tier（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- Gateway 模型選擇器顯示的是精選的高排名模型子集，不是完整目錄；用上述 `ANTHROPIC_DEFAULT_*` 釘住模型可繞過選擇器（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。

### 1.5 已知限制

- tools：Anthropic Messages 端點的 request schema 明確支援 `tools` 與 `tool_choice`（含 `auto`、`any`、`none`、指定 `tool`，以及 `disable_parallel_tool_use`），官方 Claude Code 頁也說會透傳原生 tool use（出處：https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message；https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- 串流：Anthropic Messages 端點支援 `stream`，回應有 `text/event-stream` 形式與 `content_block_delta` 等串流事件範例（出處：https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message）。
- prompt caching：OpenRouter 支援 Anthropic 式 `cache_control` 斷點（含 5 分鐘與 1 小時 TTL），並用 provider sticky routing 盡量維持 warm cache；Anthropic 的 cache 寫入約 1.25x（5 分鐘）/ 2x（1 小時），讀取約 0.1x（出處：https://openrouter.ai/docs/features/prompt-caching）。
- Anthropic 1P 以外的限制：截至 2026-10-11，OpenRouter 官方 Claude Code 頁除了「非 Anthropic 模型可能無法正常運作」「只保證 Anthropic 1P」之外，沒有再列 tools／串流／caching 的逐模型相容矩陣，實務上以 `/model` 與 Activity Dashboard 的實際路由為準（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- count_tokens：截至 2026-10-11 查無。OpenRouter 官方 Anthropic Messages 文件只列出 `POST /messages`（及既有的 `/api/v1/generation` 用量查詢），找不到等同 Anthropic `/v1/messages/count_tokens` 的端點說明（出處：https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message；https://openrouter.ai/docs/llms.txt）。
- 用量不需要 count_tokens 的替代做法：OpenRouter 會在每個回應（含串流的最後一個 SSE 訊息）自動附 `usage`（prompt／completion／total、reasoning、cached token、cost），不需要額外參數（出處：https://openrouter.ai/docs/guides/guides/usage-accounting）。

### 1.6 opencode 怎麼接（替代／並行方案）

- OpenRouter 是 opencode 內建 provider：啟動後跑 `/connect` 搜尋 OpenRouter 並貼上 API key，再用 `/models` 選模型（出處：https://openrouter.ai/docs/cookbook/coding-agents/opencode-integration）。
- 在 `opencode.json` 用 OpenRouter slug 加掛模型，`~` 開頭是 latest 別名，例如 `~anthropic/claude-sonnet-latest`；頂層 `model` 用 `openrouter/` 前綴，例如 `openrouter/~anthropic/claude-sonnet-latest`（出處：https://openrouter.ai/docs/cookbook/coding-agents/opencode-integration）。
- 可對單一模型加 `options.provider` 控制上游路由，例如 `order` 指定 `anthropic` 並允許 fallback（出處：https://openrouter.ai/docs/cookbook/coding-agents/opencode-integration）。

## 2. Ollama Cloud：原版 Claude Code 可直接接上

結論：可以。官方有專屬 Claude Code 整合頁，支援兩種走法：(a) `ollama launch claude` 一鍵啟動；(b) 不裝 Ollama，直接把 `ANTHROPIC_BASE_URL` 指到 `https://ollama.com` 並用 `OLLAMA_API_KEY` 做 bearer 認證（出處：https://docs.ollama.com/integrations/claude-code）。

### 2.1 官方 Anthropic 相容端點與網址

- Anthropic client 用的 base URL：雲端是 `https://ollama.com`，本機是 `http://localhost:11434`；Anthropic client 會自己加上 `/v1/messages`（出處：https://docs.ollama.com/api/introduction）。
- 雲端實際請求範例是 `POST https://ollama.com/v1/messages`，必須帶 `Authorization: Bearer`，不接受只用 `x-api-key`（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 本機範例是 `POST http://localhost:11434/v1/messages`（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 官方 Anthropic 相容說明頁：https://docs.ollama.com/api/anthropic-compatibility（出處：https://docs.ollama.com/api/anthropic-compatibility）。

### 2.2 官方「Claude Code 整合」說明頁

- 有。官方整合頁：https://docs.ollama.com/integrations/claude-code（出處：https://docs.ollama.com/integrations/claude-code）。
- 官方部落格公告：Ollama v0.14.0 起相容 Anthropic Messages API，可把 Claude Code 等工具接到開源模型；同文說明本機與 `ollama.com` 雲端兩種用法（出處：https://ollama.com/blog/claude）。

### 2.3 要設的環境變數（只列變數名）

- 直連 Ollama Cloud：`OLLAMA_API_KEY`（先存金鑰）、`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY`（官方範例把後者設為空）（出處：https://docs.ollama.com/integrations/claude-code）。
- 官方直連雲端的 inline 範例就是 `ANTHROPIC_BASE_URL`＋`ANTHROPIC_AUTH_TOKEN`＋空的 `ANTHROPIC_API_KEY`，再下 `claude --model` 指定雲端模型；並提醒用 `ANTHROPIC_AUTH_TOKEN` 做 bearer 認證（出處：https://docs.ollama.com/integrations/claude-code）。
- 本機手動設定：`ANTHROPIC_AUTH_TOKEN`（官方寫「required but ignored」，範例填 `ollama`）、`ANTHROPIC_API_KEY`（設為空）、`ANTHROPIC_BASE_URL`（出處：https://docs.ollama.com/api/anthropic-compatibility；https://docs.ollama.com/integrations/claude-code）。
- 一鍵走法不需要手設變數：`ollama launch claude`（可加 `--model`、`--yes`，`--` 之後透傳給 Claude Code）（出處：https://docs.ollama.com/integrations/claude-code）。
- 雲端金鑰申請位置在 Settings 的 Keys（官方寫「Set your API key in `OLLAMA_API_KEY`」並連結到 keys 設定頁）（出處：https://docs.ollama.com/api/anthropic-compatibility）。

### 2.4 可用模型

- 本機模型：官方點名的 coding 適用例子有 `qwen3-coder`、`gpt-oss:20b`、`qwen3.5`；用前先 `ollama pull`（出處：https://docs.ollama.com/api/anthropic-compatibility；https://docs.ollama.com/integrations/claude-code）。
- 雲端模型：官方點名的例子有 `gemma4:31b`（直連雲端 API 用的 hosted identifier）、`gemma4:cloud`（App／CLI／signed-in server 用的名稱）、`glm-5.3-flash`、`glm-4.7:cloud`、`minimax-m2.1:cloud`、`gpt-oss:120b-cloud`、`qwen3-coder:480b-cloud`、`kimi-k2.7-code:cloud`（出處：https://docs.ollama.com/api/anthropic-compatibility；https://docs.ollama.com/integrations/claude-code；https://ollama.com/blog/launch）。
- 官方提醒：選「支援 tools 的雲端模型」，基本聊天與檔案編輯在相容模型上可動；context 建議 64k 以上，雲端模型一律跑滿完整 context（出處：https://docs.ollama.com/integrations/claude-code；https://ollama.com/blog/claude）。
- 官方提醒：直連雲端要用 `https://ollama.com/api/tags` 回傳的名稱（例如 `gemma4:31b`）；經 signed-in 本機 server 則用 `gemma4:cloud` 這類名稱（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 官方提醒：依賴 Anthropic 預設模型名的工具，可用 `ollama cp` 把既有模型複製成該名稱（例如複製成 `claude-3-5-sonnet`）（出處：https://docs.ollama.com/api/anthropic-compatibility）。

### 2.5 已知限制

- 有支援：messages、串流、system prompt、多輪對話、vision（圖片）、tools（function calling）、tool results、thinking／extended thinking（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 有支援的請求欄位：`model`、`max_tokens`、`messages`（含 text／base64 image／content block 陣列／`tool_use`／`tool_result`／`thinking`）、`system`、`stream`、`temperature`、`top_p`、`top_k`、`stop_sequences`、`tools`、`thinking`、`output_config.effort`（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 有支援的回應欄位：`id`、`type`、`role`、`model`、`content`（text／tool_use／thinking）、`stop_reason`（`end_turn`／`max_tokens`／`tool_use`）、`usage`（`input_tokens`／`output_tokens`）（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 串流事件：`message_start`、`content_block_start`、`content_block_delta`（`text_delta`／`input_json_delta`／`thinking_delta`）、`content_block_stop`、`message_delta`、`message_stop`、`ping`、`error`（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 明確不支援：`/v1/messages/count_tokens`、`tool_choice`、`metadata`、prompt caching（`cache_control`）、Batches API（`/v1/messages/batches`）、citations、PDF（`document` 區塊）、串流中的 server-sent error 事件（錯誤改以 HTTP status 回傳）（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 部分支援：圖片只支援 base64、不支援 URL；extended thinking 基本支援，`budget_tokens` 接受但不強制執行（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- 行為差異：本機 server 不驗證 API key；雲端一定要有效的 bearer token；雲端會拒絕早於 `2023-06-01` 的 `anthropic-version`；token 計數是基於底層 tokenizer 的近似值（出處：https://docs.ollama.com/api/anthropic-compatibility）。
- Claude Code 情境的額外限制：官方寫「選支援 tools 的雲端模型」「Hosted WebSearch 與進階 tool 控制未完整支援」「tool-choice controls、deferred tools、hosted web search 未完整支援」（出處：https://docs.ollama.com/integrations/claude-code；https://docs.ollama.com/api/anthropic-compatibility）。

### 2.6 opencode 怎麼接（替代／並行方案）

- 一鍵走法：`ollama launch opencode`（只配 config 不啟動則加 `--config`）；該命令用 inline config 啟動，不會覆寫 `~/.config/opencode/opencode.json`（出處：https://docs.ollama.com/integrations/opencode）。
- 手動走法：在 `opencode.json` 加 Ollama provider，`npm` 用 `@ai-sdk/openai-compatible`，`options.baseURL` 用 `http://localhost:11434/v1`，再於 `models` 列模型（出處：https://docs.ollama.com/integrations/opencode）。
- Ollama Cloud 走法（opencode 官方文件）：到 `https://ollama.com/` 登入並在 Settings Keys 建 key，跑 `/connect` 搜尋 Ollama Cloud 並貼上 key；使用雲端模型前先 `ollama pull` 對應的 `*-cloud` 模型資訊，再用 `/models` 選模型（出處：https://opencode.ai/docs/providers）。
- opencode 要求 context 64k 以上（出處：https://docs.ollama.com/integrations/opencode）。

## 3. OpenCode Zen：原版 Claude Code「官方未說明」，只有社群接法；opencode 才是官方路

結論：Zen 有 Anthropic 相容的 Messages 端點，但官方 Zen 文件只說明「在 opencode 內使用 Zen」；截至 2026-10-11，官方找不到「用原版 Claude Code 接 Zen」的整合說明頁。社群有非官方的 `ANTHROPIC_BASE_URL=https://opencode.ai/zen` 接法，本節把官方與社群分開寫。

### 3.1 官方 Anthropic 相容端點與網址

- 有。Zen 官方端點表把 Claude 家族列在 `https://opencode.ai/zen/v1/messages`，AI SDK 配 `@ai-sdk/anthropic`（出處：https://opencode.ai/docs/zen）。
- 同表列入 `/v1/messages` 的非 Claude 模型還有 `qwen3.8-flash`、`qwen3.7-max`、`qwen3.7-plus`、`qwen3.6-plus`、`qwen3.5-plus`；其餘模型走 `https://opencode.ai/zen/v1/chat/completions`（`@ai-sdk/openai-compatible`）、`https://opencode.ai/zen/v1/responses`（`@ai-sdk/openai`）、`https://opencode.ai/zen/v1/models/gemini-*`（`@ai-sdk/google`）、`https://opencode.ai/zen/v1/systemone`（出處：https://opencode.ai/docs/zen）。
- 模型列表官方機讀端點：`https://opencode.ai/zen/v1/models`（出處：https://opencode.ai/docs/zen）。
- Zen 認證：到 `https://opencode.ai/auth` 登入、加帳單資訊後複製 API key；SystemOne 範例顯示以 `Authorization: Bearer` 送該 key（出處：https://opencode.ai/docs/zen）。

### 3.2 官方「Claude Code 整合」說明頁

- 截至 2026-10-11 查無。Zen 官方文件只寫三步：在 TUI 跑 `/connect` 選 OpenCode Zen、到 `opencode.ai/auth` 取 key、跑 `/models` 選模型；沒有 `ANTHROPIC_BASE_URL`／`ANTHROPIC_AUTH_TOKEN` 接原版 Claude Code 的段落（出處：https://opencode.ai/docs/zen）。
- 公開網頁上找得到的是社群（非官方）指南，例如有人整理「把 `ANTHROPIC_BASE_URL` 指到 `https://opencode.ai/zen` 即可讓 Claude Code 經 Zen 出去」，但那不是 opencode 官方文件（出處：https://github.com/KoLDXr00T/cc-with-oc-zen）。
- 同類社群做法還有把 Claude Code 指到自架轉接器再往 Zen 送（例如 Anthropic→OpenAI 相容轉接），同樣非官方（出處：https://github.com/kqrege/zen-claude-bridge）。

### 3.3 要設的環境變數（只列變數名）

- 官方（opencode 路線）：不需要手設 shell 變數；用 opencode TUI 的 `/connect` 存 key、用 `/models` 選模型（出處：https://opencode.ai/docs/zen）。
- 社群回報的原版 Claude Code 接法（非官方、自行承擔相容風險）：`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`，視轉接器吃哪個 header 而定）（出處：https://github.com/KoLDXr00T/cc-with-oc-zen；https://github.com/techwithburhan/-Claude-Code-Free-Setup-via-OpenCode）。
- 提醒：Anthropic 官方 gateway 語義下，放錯變數會因 header 不符而 `401`；Zen 官方沒說它吃哪個 header，所以社群接法若 `401`，應按 https://code.claude.com/docs/en/llm-gateway-connect 的方式換另一個變數重測（出處：https://code.claude.com/docs/en/llm-gateway-connect）。

### 3.4 可用模型（走 `/v1/messages` 的 Zen 模型）

- Claude：`claude-fable-5-1`、`claude-fable-5`、`claude-opus-5-5`、`claude-opus-5`、`claude-opus-4-8`、`claude-opus-4-7`、`claude-opus-4-6`、`claude-opus-4-5`、`claude-sonnet-5-5`、`claude-sonnet-5`、`claude-sonnet-4-6`、`claude-sonnet-4-5`、`claude-haiku-5-5`、`claude-haiku-4-5`（出處：https://opencode.ai/docs/zen）。
- Qwen（同走 `/v1/messages`）：`qwen3.8-flash`、`qwen3.7-max`、`qwen3.7-plus`、`qwen3.6-plus`、`qwen3.5-plus`（出處：https://opencode.ai/docs/zen）。
- 其餘 Zen 模型走 `/v1/chat/completions`（DeepSeek、MiniMax、GLM、Kimi、Mistral 等）、`/v1/responses`（GPT、Grok、Muse Spark 等）、`/v1/models/gemini-*`、`／v1/systemone`（Jev）；原版 Claude Code 走 Anthropic 協定时，原則上只考慮 `/v1/messages` 表內的模型（出處：https://opencode.ai/docs/zen）。
- 在 opencode config 內的模型寫法是 `opencode/<model-id>`，例如 `opencode/gpt-5.5`（出處：https://opencode.ai/docs/zen）。

### 3.5 已知限制

- tools：截至 2026-10-11 查無。Zen 官方端點表只列 URL 與 AI SDK 配件，沒有 tools／tool_choice 支援矩陣（出處：https://opencode.ai/docs/zen）。
- 串流：截至 2026-10-11 查無。Zen 官方文件沒有說明 `/v1/messages` 的 SSE／串流行為（出處：https://opencode.ai/docs/zen）。
- prompt caching：官方沒有特徵說明，但 Zen 定價表對 Claude 行有 `Cached Read`／`Cached Write` 欄位（例如 Opus 4.5 讀 0.5／寫 6.25 美元每 1M tokens），僅能證明計費面有 cache 概念，不能推導為完整的 Anthropic `cache_control` 語義；特徵語義截至 2026-10-11 查無（出處：https://opencode.ai/docs/zen）。
- count_tokens：截至 2026-10-11 查無。Zen 官方文件沒有等同 `/v1/messages/count_tokens` 的端點說明（出處：https://opencode.ai/docs/zen）。

### 3.6 opencode 怎麼接（官方替代方案）

- 這是 Zen 的官方路：到 `https://opencode.ai/auth` 登入、加帳單資訊、複製 API key；在 TUI 跑 `/connect` 選 OpenCode Zen 並貼上 key；跑 `/models` 看推薦模型表（出處：https://opencode.ai/docs/zen）。
- Zen 在 opencode 內與其他 provider 一樣用，是可選的；不用 Zen 也能用 opencode（出處：https://opencode.ai/docs/zen）。
- 計費：pay-as-you-go，每 1M tokens 計價；餘額低於 5 美元會自動加值 20 美元（可改可關），也可設 workspace／成員月限額（出處：https://opencode.ai/docs/zen）。

## 4. OpenRouter `GET /api/v1/key` 回傳的用量欄位

- 方法：`GET https://openrouter.ai/api/v1/key`，`Authorization: Bearer` 帶任意有效的 API key（含一般 key 或 management key），回傳 `data` 物件（出處：https://openrouter.ai/docs/api/reference/limits；https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key）。
- 用量欄位（單位 USD；`usage*` 是 OpenRouter  credit 用量，`byok_usage*` 是外部 BYOK 用量；日／週／月窗口皆 UTC，週從週一開始）（出處：https://openrouter.ai/docs/api/reference/limits；https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key）：
  - `usage`：累計用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `usage_daily`：當日用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `usage_weekly`：當週用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `usage_monthly`：當月用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `byok_usage`：累計 BYOK 用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `byok_usage_daily`：當日 BYOK 用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `byok_usage_weekly`：當週 BYOK 用量（出處：https://openrouter.ai/docs/api/reference/limits）。
  - `byok_usage_monthly`：當月 BYOK 用量（出處：https://openrouter.ai/docs/api/reference/limits）。
- 同一回應內的額度欄位（非用量，但實務上會一起看）：`limit`、`limit_remaining`、`limit_reset`、`include_byok_in_limit`（出處：https://openrouter.ai/docs/api/reference/limits）。
- 同一回應內的預算欄位：`budgets[]`（`scope`、`guardrail_id`、`budget_id`、`limit_usd`、`spend_usd`、`remaining_usd`、`reset_interval`、`resets_at`、`include_byok`）、`effective_budget`（最先擋下請求的那一層）（出處：https://openrouter.ai/docs/api/reference/limits；https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key）。
- 同一回應內的免費模型日配額：`free_model_daily_requests.{used,limit,remaining}`（出處：https://openrouter.ai/docs/api/reference/limits；https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key）。
- 同一回應內的身分／狀態欄位：`label`、`is_free_tier`、`is_provisioning_key`（舊名，等同 management）、`is_management_key`、`creator_user_id`、`expires_at`、`organization_id`、`workspace_id`、`allowed_data_regions`（出處：https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key）。
- 已棄用欄位：`rate_limit`（官方註明 legacy，固定回傳舊值，應忽略；即時限流改看 `429` 回應的 `X-RateLimit-*`／`Retry-After` header）（出處：https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key；https://openrouter.ai/docs/api/reference/limits）。

## 5. 給 LLMGateway 的取用建議（摘要）

- OpenRouter：走 `ANTHROPIC_BASE_URL=https://openrouter.ai/api`＋bearer token，官方支援最完整；要鎖 Anthropic 1P provider 並注意 `[1m]`、`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`、`CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK` 這些官方變數（出處：https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration）。
- Ollama Cloud：走 `ANTHROPIC_BASE_URL=https://ollama.com`＋bearer token，官方有文件；但只支援子集（無 `tool_choice`／`cache_control`／`count_tokens` 等），選模型要挑支援 tools 且 64k context 以上的（出處：https://docs.ollama.com/integrations/claude-code；https://docs.ollama.com/api/anthropic-compatibility）。
- OpenCode Zen：不要當成「官方支援原版 Claude Code 直連」的上游；官方路是 opencode `/connect`；若硬要用原版 Claude Code 指過去，那是社群做法，需自行驗證 header 與模型相容性（出處：https://opencode.ai/docs/zen；https://github.com/KoLDXr00T/cc-with-oc-zen）。
