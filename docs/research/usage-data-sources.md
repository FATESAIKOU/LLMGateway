# Agent 與訂閱用量資料來源

目的：各 agent／訂閱實際給得出哪些用量資料（事實，不是設計）；供 MLP#1259 成本記錄設計使用。讀取日期：2026-11-15。「已查證」指親見文件／`--help` 原文，不代表實跑。

## Agent 總表

各格列出取得方式、欄位、粒度與狀態；不同粒度不能混加。來源連結適用於該列。

| agent／出處 | input | cached read | cached write | output＋reasoning | request 數 |
|---|---|---|---|---|---|
| Claude Code：[cost](https://code.claude.com/docs/en/agent-sdk/cost-tracking.md)、[statusline](https://code.claude.com/docs/en/statusline.md)、[OTel](https://code.claude.com/docs/en/monitoring-usage.md) | JSON result `usage.input_tokens`（頂層主 agent loop 累計，不含 subagent；streaming input mode 為該輪 turn；含 subagent 的整次呼叫累計看 `modelUsage` 與 `total_cost_usd`）；statusline `context_window.current_usage.input_tokens`（當前 request）；OTel `claude_code.token.usage` type `input`（request 累加）；已查證 | 同路徑 `cache_read_input_tokens`；OTel type `cacheRead`；粒度同左；已查證 | 同路徑 `cache_creation_input_tokens`；OTel type `cacheCreation`；粒度同左；已查證 | result `usage.output_tokens`（粒度與範圍同 input 的 result usage）；statusline `current_usage.output_tokens`（當前 request）；OTel type `output`（request 累加）；reasoning 無獨立欄位，納入 output；已查證 | stream assistant 訊息不重複 `id` 數；報告亦稱 OTel `claude_code.cost.usage`／`claude_code.token.usage` 資料點數可計數（request 累加）；已查證；資料點是否等於去重 request 數需核對 |
| OpenCode：[schema](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/schema/src/token-usage.ts)、[stats](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/schema/src/session-stats.ts)、[OpenAPI](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/protocol/openapi.json) | stream `step_finish` 的 `part.tokens.input`（step）；stats `tokens.input`（期間累計）；已查證 | `part.tokens.cache.read`（step）；stats `tokens.cache.read`（期間累計）；已查證 | `part.tokens.cache.write`（step）；stats `tokens.cache.write`（期間累計）；已查證 | `part.tokens.output`、`part.tokens.reasoning`（step）；stats `tokens.output`、`tokens.reasoning`（期間累計）；已查證 | `step_finish` 事件數（step）；stats `steps`（期間累計）；已查證 |
| Codex：[JSONL](https://learn.chatgpt.com/docs/non-interactive-mode)、[OTel](https://learn.chatgpt.com/docs/config-file/config-advanced)、[原始碼](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/mod.rs) | `turn.completed.usage.input_tokens`（turn）；已查證 | 同事件 `cached_input_tokens`（turn）；已查證 | JSONL 範例未列；原始碼 `last_token_usage.cache_write_input_tokens`（事件，可能為估算）；實際可用性未查證 | 同事件 `output_tokens`、`reasoning_output_tokens`（turn）；已查證 | OTel `codex.api_request`、`codex.websocket.request`（傳輸 request 計數）；已查證；去重後模型 request 數未查證 |
| agy：[headless](https://antigravity.google/docs/cli/headless/) | JSON `usage.input_tokens`；stream `step_update.usage.input_tokens`（已知時，step）、`result.usage.input_tokens`（彙總；連續輸入時 session 累計）；已查證 | 同路徑 `cache_read_tokens`；粒度同左；已查證 | 文件查不到；未查證 | 同路徑 `output_tokens`、`thinking_tokens`；粒度同左；已查證；兩者包含關係未查證 | `num_turns` 是 user turn 數（連續輸入時 session 累計），不是模型 request 數；模型 request 總數未查證 |

## 各 agent 的其他來源

### Claude Code

- CLI `--output-format json` result 有 `session_id`、`usage`、依模型分組的 `modelUsage`；stream-json 為 NDJSON，assistant 訊息有 `id` 和 usage，但單步 output 是佔位值，準確累計 output 讀 result。子 agent 文字可帶 `parent_tool_use_id`。已查證。[CLI](https://code.claude.com/docs/en/cli-reference.md)、[cost](https://code.claude.com/docs/en/agent-sdk/cost-tracking.md)
- 標準 `Stop`／`SessionEnd` hook 帶 `session_id`、`transcript_path`，未列當輪 token；resume／fork 時若 transcript 至少含一個 Claude 回應，`SessionStart` 收到 `context_tokens`、`estimated_cache_write_usd`（需 Claude Code v2.1.251 以上）；`PreModelSwitch` 有預估 `context_tokens`，`PostToolUse` 匹配 `Agent` 時有子 agent `tool_response.usage`。已查證。[hooks](https://code.claude.com/docs/en/hooks.md)
- Statusline 每次 API 回應後提供 `current_usage`、累計 cost 與 rate limits；OTel token counter 有 `model`、`query_source` 屬性。已查證。[statusline](https://code.claude.com/docs/en/statusline.md)、[OTel](https://code.claude.com/docs/en/monitoring-usage.md)
- 文件記載 session transcript 為 `~/.claude/projects/<project>/<session-id>.jsonl`；內部欄位未查證，未讀真實檔案。[sessions](https://code.claude.com/docs/en/sessions.md)

### OpenCode

- `opencode run --format json` 輸出 NDJSON，`step_finish` 帶 `sessionID`、`messageID` 與 step tokens；`opencode stats --json` 提供期間及按模型的累計。已查證。[run](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/cli/src/run/noninteractive.ts)、[stats](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/cli/src/commands/handlers/stats.ts)
- `opencode session export` 助理訊息記載 tokens、model、agent；server SSE `session.step.ended` 與 plugin event subscription 提供 step 資訊。已查證。[export](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/schema/src/session-transfer.ts)、[OpenAPI](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/protocol/openapi.json)、[plugins](https://opencode.ai/docs/build/plugins/server/)
- 原始碼記載本機 SQLite `opencode.db`；真實資料庫寫入狀況未查證，未讀真實檔案。[database](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/cli/src/database-path.ts)

### Codex

- `codex exec --json` 輸出 JSONL，`turn.completed` 是 turn 用量；失敗時 usage 完整性未查證。已查證文件。[JSONL](https://learn.chatgpt.com/docs/non-interactive-mode)
- OTel `codex.sse_event` 的 `response.completed` 含 token counts；確切欄位、完整率和 cache write 未查證。app-server 有 `thread/tokenUsage/updated`，所讀文件未給完整欄位。已查證文件。[OTel](https://learn.chatgpt.com/docs/config-file/config-advanced)、[app-server](https://learn.chatgpt.com/docs/app-server)
- 原始碼有 `TokenCountEvent`、`last_token_usage` 與本機估算路徑；main 版未核對本機版本。hook 用量及 rollout JSONL 持久化結構未查證；未讀本機紀錄。[原始碼](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/mod.rs)

### agy

- `--output-format json` 為 JSON envelope；`stream-json` 為 NDJSON，step usage 已知時才提供，result 為彙總。已查證。[headless](https://antigravity.google/docs/cli/headless/)
- `PreInvocation`／`PostInvocation` hooks 的逐 invocation usage 未查證；status line 文件稱 JSON 含 token usage，精確欄位及範圍未查證。[hooks](https://antigravity.google/docs/hooks)、[features](https://antigravity.google/docs/cli/features/)
- `--log-file` 可指定路徑；log 格式及 usage 未查證，未讀真實 log。已查證 `--help`。

## 訂閱用量

| 訂閱 | 端點／指令 | 認證類型 | 用量欄位、粒度與狀態 | 出處 |
|---|---|---|---|---|
| Claude | Claude Code statusline；獨立公開 REST 端點查不到 | Claude Code OAuth | `rate_limits.seven_day.used_percentage`、`resets_at`；`five_hour` 同名欄位；`spend_limit` 含 `used_usd`、`limit_usd`、`period`；窗口用量，已查證文件 | [statusline](https://code.claude.com/docs/en/statusline.md) |
| ChatGPT／Codex | `/status`；app-server RPC `account/rateLimits/read` | 帳戶 session | `rateLimits`、`rateLimitsByLimitId`；`primary`／`secondary` 的 `usedPercent`、`windowDurationMins`、`resetsAt` 可為 null；窗口用量，文件已查證，實帳號窗口未查證 | [pricing](https://learn.chatgpt.com/docs/pricing)、[app-server](https://learn.chatgpt.com/docs/app-server) |
| Antigravity | `/usage`（`/quota`）、Settings；公開 API 查不到 | 帳戶 session | 各模型額度與剩餘；週窗口具體顯示及機器可讀欄位未查證；文件已查證 | [usage](https://antigravity.google/docs/cli/commands/usage)、[plans](https://antigravity.google/docs/plans) |
| Ollama Cloud | `GET https://ollama.com/api/usage` | Bearer API key | `totals.request_count`、`usage_usd`、`input_tokens`、`cached_input_tokens`、`output_tokens`，以及 `buckets`、`groups`；指定期間累計，已查證 | [usage API](https://docs.ollama.com/api/cloud-usage.md) |
| Ollama Cloud | `GET https://ollama.com/api/balance` | Bearer API key | `included.balance_usd`、`allowance_usd`、`period.from`／`until`，部分方案 `included.weekly.remaining_percent`／`included.weekly.resets_at`，及 `purchased.balance_usd`；餘額／週窗口，已查證 | [balance](https://docs.ollama.com/api/balance.md) |
| OpenRouter | `GET https://openrouter.ai/api/v1/key` | Bearer API key | `data.usage_weekly`、`usage_daily`、`usage_monthly`、`limit`、`limit_remaining`、`limit_reset`、`budgets`、`effective_budget`、`free_model_daily_requests`；各窗口累計／預算，已查證 | [OpenAPI](https://openrouter.ai/docs/openapi/openapi.yaml) |
| OpenRouter | `GET https://openrouter.ai/api/v1/credits` | Bearer management key | `data.total_credits`、`total_usage`；總額累計，已查證 | [credits](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits.md) |
| OpenRouter | `GET https://openrouter.ai/api/v1/generation?id={id}` | Bearer token | `total_cost`、`tokens_prompt`、`tokens_completion`、`native_tokens_prompt`、`native_tokens_completion`、`native_tokens_cached`、`native_tokens_reasoning`；單筆 request，已查證 | [generation](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation.md) |
| OpenCode Zen／Console／Go | 本機 server `GET /api/experimental/usage/limits`、Console 網頁；外部獨立公開 REST 端點查不到 | server 認證／web session | `go.weekly.percent`、`limited`、`resetsAt`，`budgets` 的 `limit`、`spent`、`resetsAt`；週窗口／預算，已查證 | [OpenAPI](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/protocol/openapi.json)、[schema](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/schema/src/usage-limits.ts) |

## 未查證／拿不到

- Claude 訂閱與 OpenCode Zen 的外部獨立公開 REST 額度端點查不到；Claude Code reasoning 獨立欄位及標準 Stop／SessionEnd hook 的當輪 token 拿不到。[statusline](https://code.claude.com/docs/en/statusline.md)、[hooks](https://code.claude.com/docs/en/hooks.md)、[cost](https://code.claude.com/docs/en/agent-sdk/cost-tracking.md)、[OpenAPI](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/protocol/openapi.json)
- Claude transcript 內部欄位、OpenCode 真實 SQLite 寫入狀況未查證；未讀真實檔案。[sessions](https://code.claude.com/docs/en/sessions.md)、[database](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/cli/src/database-path.ts)
- Codex／agy 每個底層模型 request 的完整 token 與去重 request 數未查證；Codex 失敗用量、OTel 欄位、hook、rollout、cache write 實際可用性與版本差異未查證。[Codex](https://learn.chatgpt.com/docs/non-interactive-mode)、[OTel](https://learn.chatgpt.com/docs/config-file/config-advanced)、[agy](https://antigravity.google/docs/cli/headless/)
- agy hook／log 逐 request usage、cache write、input 與 cache read 及 output 與 thinking 的包含關係未查證；ChatGPT 實帳號窗口及 Antigravity 週窗口面板顯示未查證。[hooks](https://antigravity.google/docs/hooks)、[headless](https://antigravity.google/docs/cli/headless/)、[app-server](https://learn.chatgpt.com/docs/app-server)、[usage](https://antigravity.google/docs/cli/commands/usage)

## 歸戶（工單↔agent）相關的事實

- Claude result 帶 `session_id`、依模型的 `modelUsage`；stream 子 agent 訊息可帶 `parent_tool_use_id`；result `usage` 僅計頂層主 agent loop、不含 subagent；streaming input mode 下為該輪 turn 範圍；含 subagent 的整次呼叫累計看 `modelUsage` 與 `total_cost_usd`。已查證。[cost](https://code.claude.com/docs/en/agent-sdk/cost-tracking.md)、[CLI](https://code.claude.com/docs/en/cli-reference.md)
- OpenCode `step_finish` 帶 `sessionID`、`messageID`；session export 的助理訊息帶 model、agent；stats 有期間及按模型累計。已查證。[run](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/cli/src/run/noninteractive.ts)、[export](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/schema/src/session-transfer.ts)、[stats](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/schema/src/session-stats.ts)
- Codex `turn.completed` usage 為 turn 範圍；agy 連續輸入的 result usage 為 session 累計。已查證。[Codex](https://learn.chatgpt.com/docs/non-interactive-mode)、[agy](https://antigravity.google/docs/cli/headless/)
