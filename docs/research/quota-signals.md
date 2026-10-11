# 各 coding agent 額度用完／限流訊號研究（quota-signals）

- 用途：給 worker 上的監督程式偵測「額度用完」使用。
- 調查日期：2026-10-11。方法限制（本人指定）：只讀公開的官方文件與公開網頁；未讀任何憑證檔（auth.json、*.token、.env、SECRETS 等），未跑 `gh`、未 `git commit`、未打需要金鑰的 API。
- 「官方」指各產品自己的文件站（code.claude.com、opencode.ai、docs.ollama.com、openrouter.ai/docs、antigravity.google/docs）；GitHub issue／PR／gist／社群教學為「公開網頁（非官方）」，已在出處旁標註。
- 查不到的項目寫「截至 2026-10-11 查無」，不臆測。

## 1. Claude Code（訂閱，含 `CLAUDE_CODE_OAUTH_TOKEN`）

### 1.1 畫面訊息（互動 session）

- 訂閱方案用完會顯示以下四種之一，**訊息內直接帶重置時間**，擋住後續請求直到該時間（出處：https://code.claude.com/docs/en/errors ）：
  - `You've hit your session limit · resets 3:45pm`
  - `You've hit your weekly limit · resets Mon 12:00am`
  - `You've hit your Opus limit · resets 3:45pm`
  - `You've hit your Sonnet limit · resets 3:45pm`
- session／weekly 限額跨所有模型共用，切換模型無效；Opus／Sonnet 限額只針對該模型家族，切到家族外可繼續（出處：https://code.claude.com/docs/en/errors ）。
- 用完之前會有預警，例如 `You've used 85% of your session limit · resets 3:45pm`（出處：https://code.claude.com/docs/en/errors ）。
- 互動 session 可原地等待重置後自動繼續（v2.1.234 起；連續再撞限額最多自動重掛兩次，之後顯示 `Automatic continue stopped after repeated usage-limit hits · /rate-limit-options to try again`；可用 `/rate-limit-options` 或設定 `autoContinueAtUsageLimit` 控制）（出處：https://code.claude.com/docs/en/errors 、https://code.claude.com/docs/en/interactive-mode ）。
- API key／Bedrock／GCP 專案的限流是另一種訊息：`API Error: Request rejected (429) · this may be a temporary capacity issue. If it persists, check https://status.claude.com.`（出處：https://code.claude.com/docs/en/errors ）。
- 與方案配額無關的伺服器端節流：`API Error: Server is temporarily limiting requests (not your usage limit)`（出處：https://code.claude.com/docs/en/errors ）。
- usage credits 花到 spend limit：`You've hit your monthly spend limit · raise it at …`／`individual spend limit`／`org's monthly spend limit`／`team's shared budget`／`channel's monthly spend limit` 等變體；若同時是方案視窗用完會加註如 `your session limit resets 3:45pm`（出處：https://code.claude.com/docs/en/errors ）。
- gateway 後方：`spend limit reached (daily; resets 2026-08-09 00:00 UTC)`（小寫；舊版只有 `spend limit reached`）（出處：https://code.claude.com/docs/en/errors ）。
- 誤用 API key 導致被當 API 帳單：`Credit balance is too low`（出處：https://code.claude.com/docs/en/errors ）。

### 1.2 `claude -p` 的 exit code 與 `--output-format stream-json` 事件

- 官方只寫：成功 exit code 0，失敗為非零；run 內失敗（如缺認證）會把失敗內容印在 stdout 當 result（出處：https://code.claude.com/docs/en/headless ）。
- **各額度錯誤各自對應哪個 exit code，截至 2026-10-11 查無**（官方文件只區分 0／非零）。
- stream-json 固定有三類事件：開頭 `system/init`、中間 `stream_event` 等、`result` 收尾（最後一行是 `result`，內含最終文字、cost、session metadata）（出處：https://code.claude.com/docs/en/headless ）。
- API 請求失敗重試前會發 `system/api_retry` 事件，可程式讀取，欄位含 `attempt`、`max_retries`、`retry_delay_ms`、`error_status`（HTTP 碼）、`error`（分類字串）。`error` 的枚舉含 `rate_limit`、`overloaded`、`billing_error` 等（完整：`authentication_failed`、`oauth_org_not_allowed`、`account_on_hold`、`billing_error`、`rate_limit`、`overloaded`、`invalid_request`、`model_not_found`、`server_error`、`max_output_tokens`、`cloud_credential_error`、`unknown`）（出處：https://code.claude.com/docs/en/headless ）。
- **額度用完時 `result` 事件長什麼樣子（`result` 內有無專屬 quota 欄位、錯誤文字是否等於畫面訊息），截至 2026-10-11 查無**（官方 headless 文件只寫 `permission_denied` 與 `permission_denials` 的呈現，未列 quota 案例）。

### 1.3 hooks 的 `StopFailure`

- turn 因 API 錯誤結束時跑 `StopFailure`（取代 `Stop`）；**hook 的輸出與 exit code 會被忽略**（`terminalSequence` 除外），只能做 logging／alerting／recovery side effect（出處：https://code.claude.com/docs/en/hooks ）。
- matcher 即錯誤分類，完整清單：`rate_limit`、`overloaded`、`authentication_failed`、`oauth_org_not_allowed`、`account_on_hold`、`billing_error`、`invalid_request`、`model_not_found`、`server_error`、`max_output_tokens`、`cloud_credential_error`、`unknown`（出處：https://code.claude.com/docs/en/hooks ）。
- 輸入 JSON 欄位：`error`（上列分類）、`error_details`（如 `"429 Too Many Requests"`）、`last_assistant_message`（即對話中顯示的錯誤原文，如 `"API Error: Rate limit reached"`）（出處：https://code.claude.com/docs/en/hooks ）。
- **「You've hit your session/weekly/Opus/Sonnet limit」四種訊息分別對應哪個 matcher 值（是否都是 `rate_limit`），截至 2026-10-11 查無**（官方 hooks 文件未給 UI 訊息→matcher 對照表）。監督程式若要用，建議同時比對 `last_assistant_message` 的文字前綴 `You've hit your`。
- 另有 `Notification` 事件的 matcher 含 `quota_auto_resume_fired`／`quota_auto_resume_stale`／`quota_auto_resume_disabled`（自動等待重置相關通知），可作為輔助訊號（出處：https://code.claude.com/docs/en/hooks ）。

### 1.4 status line JSON 的 `rate_limits`

- 欄位名（出處：https://code.claude.com/docs/en/statusline ）：
  - `rate_limits.five_hour.used_percentage`（0–100）、`rate_limits.five_hour.resets_at`（Unix epoch 秒）
  - `rate_limits.seven_day.used_percentage`、`rate_limits.seven_day.resets_at`
  - gateway 後方另有 `rate_limits.spend_limit.*`（`used_percentage`、`resets_at`、`used_usd`、`limit_usd`、`period`）
- 出現條件：**只有 claude.ai Pro 和 Max 訂閱者**（或 gateway 有 spend limit 時），且**該 session 第一次 API 回應之後**才有；`five_hour`／`seven_day` 各自可獨立缺席，過了 `resets_at` 的視窗會被拿掉。讀取範例：`jq -r '.rate_limits.five_hour.used_percentage // empty'`（出處：https://code.claude.com/docs/en/statusline ）。
- **Team／Enterprise 方案下 status line 有無 `rate_limits`**：官方 statusline 文件只寫 Pro 和 Max，截至 2026-10-11 查無（註：costs 文件說 Team／Enterprise 成員用量走「seat allowance＋5 小時／每週視窗」，但未說明 status line 是否吐出同欄位。出處：https://code.claude.com/docs/en/costs ）。
- **setup-token（`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`）下有無 `rate_limits`**：截至 2026-10-11 查無（官方 authentication 文件只說該 token 需 Pro／Max／Team／Enterprise、可發 model request、不能開 Remote Control／不能取 claude.ai connectors、`--bare` 不讀它；未區分 statusline 行為。出處：https://code.claude.com/docs/en/authentication ）。非官方補充（僅供參考）：社群 gist 稱 setup-token 只有 `user:inference` scope，而非官方 usage 端點需 `user:profile` scope（公開網頁，非官方：https://gist.github.com/thomaslty/72a86a5d539e8bca101ecc1528dc0948 ）；另一社群專案稱 setup-token 無 refresh token 且 `CLAUDE_CODE_OAUTH_TOKEN` 優先於 credentials 檔（公開網頁，非官方：https://github.com/IvanMurzak/Claude-Subscription-Switcher/blob/main/README.md ）。
- 認證順位上 `CLAUDE_CODE_OAUTH_TOKEN` 排第 5（cloud provider → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → profile → `/login` 訂閱）（出處：https://code.claude.com/docs/en/authentication ）。注意 `ANTHROPIC_API_KEY` 殘留會蓋掉訂閱（`/status` 可查；`Credit balance is too low` 常見肇因）（出處：https://code.claude.com/docs/en/errors ）。

### 1.5 `/usage`、`/status`

- `/usage`：Session 區塊（token／估算金額）；Pro／Max／Team／Enterprise 另有方案視窗用量條與重置時間；usage-credits 列；取不到時顯示 60 分鐘內 last-known 快照＋`Showing last-known usage`（出處：https://code.claude.com/docs/en/costs ）。
- `/status` 可確認當前 credential（Login method／API key 列），登入過期會顯示 `Expired — log in again`（出處：https://code.claude.com/docs/en/authentication ）。

## 2. opencode

### 2.1 OpenCode Zen 免費額度用完（任務指定的例：`Free usage exceeded, subscribe to Go [retrying in 15h 30m attempt #1]`）

- 免費額度錯誤型別為 `FreeUsageLimitError`（HTTP 429，server 回 `retry-after`，最長可達約 24h）；TUI 顯示字串為常數 `Free usage exceeded, subscribe to Go`（`GO_UPSELL_MESSAGE`），後綴 `[retrying in <Xh Ym> attempt #N]` 為 session retry 排程的 footer（公開網頁，非官方：opencode 原始碼 https://github.com/anomalyco/opencode/blob/v1.18.21/packages/opencode/src/session/retry.ts ；同訊息回報：https://github.com/anomalyco/opencode/issues/42128 、https://github.com/anomalyco/opencode/issues/42029 ）。
- 重試行為：指數退避（初次 2s、×2、無 header 上限 30s；有 `retry-after`／`retry-after-ms` header 則照 header 等，不受 30s 上限限制）；最多 5 次（`RETRY_MAX_RETRIES = 5`）（公開網頁，非官方：同上 retry.ts；社群整理：https://opencode.runman.ai/en/appendix/troubleshoot.html ）。
- Go 訂閱額度用完是另一種型別 `GoUsageLimitError`（429），TUI 顯示如 `<limitName> usage limit reached. It will reset in <Xd Yh Zm>. To continue using this model now, enable usage from your available balance - https://opencode.ai/workspace/<id>/go`（limitName 如 `5 hour`；無 metadata 時為 `Usage limit reached.`）（公開網押，非官方：同上 retry.ts；實例回報：https://github.com/anomalyco/opencode/issues/47613 ）。
- 餘額不足是 `CreditLimitExceeded`（402，不重試）；另有 `InsufficientFunds`／`LimitExceeded`（"Account budget exceeded"）等會被包成 `server_error` 的變體（公開網頁，非官方：https://github.com/anomalyco/opencode/pull/49195 ）。
- 注意：免費模型 quota 與 Zen 預付餘額是分開的；有餘額仍可能顯示免費額度用完（應切到付費版模型）（公開網頁，非官方：https://github.com/anomalyco/opencode/issues/45324 ）。
- 免費 `-free` 模型有 anti-abuse 閘門：非官方 client 的 `User-Agent` 會直接拿 429 `FreeUsageLimitError`（只影響免費模型）（公開網頁，非官方：https://github.com/anomalyco/opencode/issues/42074 ）。

### 2.2 opencode 對 OpenRouter（402／429）

- OpenRouter 官方：餘額／key 額度／in-flight 預算不夠 → **402**，body 為 `{"error":{"code":402,"message":"…","metadata":{…}}}`；`metadata.limit_source` 區分 `openrouter_in_flight_budget`（看 `Retry-After` 重試）、`openrouter_key_limit`、`openrouter_credits`；串流中以 200＋chunk 內 `error`／`finish_reason: "error"` 送達（出處：https://openrouter.ai/docs/api_reference/limits 、https://openrouter.ai/docs/api_reference/errors-and-debugging ）。
- OpenRouter 官方：平台限流／上游限流 → **429**，`{"error":{"code":429,"message":"Rate limit exceeded","metadata":{"error_type":"rate_limit_exceeded"}}}`，上游原始碼放 `metadata.provider_code`；`429`（及部分 402）可能帶 `Retry-After`；免費模型另有每分／每日上限（出處：同上；免費額度數字：https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know ）。
- 預先查詢：`GET /api/v1/key` 回 `limit_remaining`（及 `limit_reset`）可程式監控（出處：https://openrouter.ai/docs/api_reference/limits ）。
- opencode 側顯示：走通用 session-retry 流程，顯示 provider 回傳的 `message`＋同 2.1 的 retry footer；402 歸類為 `QuotaExceeded`、不再重試（新版行為；舊版曾把部分 402 當可重試）（公開網頁，非官方：https://github.com/anomalyco/opencode/pull/49195 、https://github.com/anomalyco/opencode/issues/47686 ）。**OpenRouter 402／429 在 opencode TUI 的逐字顯示字串，官方文件截至 2026-10-11 查無**（opencode 官方 providers／troubleshooting 文件未列各 provider 錯誤字串。出處：https://opencode.ai/v2/docs/providers 、https://opencode.ai/docs/troubleshooting/ ）。
- 實例（公開回報，非官方）：`This request requires more credits, or fewer max_tokens. You requested up to 32000 tokens, but can only afford 8421.`（https://github.com/anomalyco/opencode/issues/22158 ）。

### 2.3 opencode 對 Ollama Cloud（429）

- Ollama 官方錯誤格式：`{"error": "<訊息>"}`，429 即 Too Many Requests（出處：https://docs.ollama.com/api/errors ）。
- 額度類 429 的實際字串（公開回報，非官方）：`you (<帳號>) have reached your session usage limit, upgrade for higher limits: https://ollama.com/upgrade or add extra usage: https://ollama.com/settings`（https://github.com/NousResearch/hermes-agent/issues/65563 ）。
- opencode 側同走通用 retry（顯示 provider 訊息＋retry footer）。**Ollama Cloud 429 在 opencode 的逐字顯示與重置時間是否可得，官方文件截至 2026-10-11 查無**。

### 2.4 opencode 可程式讀取的事件／log

- 有：`session.error` 事件（官方 plugins 文件列為 Session Events 之一）（出處：https://opencode.ai/docs/plugins/ ）。payload 內含 `APIError { message, statusCode?, isRetryable, responseHeaders?, responseBody? }`（公開網頁，非官方 schema 整理：https://github.com/joshuadavidthomas/opencode-plugins-manual/blob/main/docs/appendix/event-schemas.md ）——`responseHeaders` 的 `retry-after`／`retry-after-ms` 即重置等待秒數，可程式解析。
- 有：本地 log `~/.local/share/opencode/log/`（檔名帶時間戳，留最近 10 個；429 可見如 `[sticky-retry] POST https://opencode.ai/zen/go/v1/messages status=429 retryAfterMs=…`）（公開網頁，非官方：https://opencode.runman.ai/en/appendix/troubleshoot.html ；實例：https://github.com/anomalyco/opencode/issues/47613 。官方 troubleshooting 文件只提及清除 `~/.local/share/opencode`，未記載 log 格式。出處：https://opencode.ai/docs/troubleshooting/ ）。
- 有：`opencode run hello --print-logs` 可印除錯 log（公開回報中 maintainer 指示，非官方：https://github.com/sst/opencode/issues/4327 ）。
- **opencode 官方記載的 exit code 對照表，截至 2026-10-11 查無**（成功例 `EXIT=0` 見公開回報，非官方：https://github.com/anomalyco/opencode/issues/47613 ）。
- 注意：有回報稱免費額度 retry loop 期間 backend log 無對應紀錄（公開網頁，非官方：https://github.com/anomalyco/opencode/issues/45989 ）——監督程式應以 `session.error` 事件為主、log 為輔。

## 3. agy（Antigravity CLI）

### 3.1 額度用完訊息

- 官方文件未列出額度錯誤字串；以下為公開回報（非官方），皆含 `Resets in` 重置時間，可用 regex 抓：
  - `⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 143h57m55s.`（https://github.com/google-antigravity/antigravity-cli/issues/789 ）
  - `⚠ Individual quota reached. Contact your administrator to enable overages. Resets in 167h54m10s`／`…126h51m11s`（https://github.com/google-antigravity/antigravity-cli/issues/46 ）
  - 底層另有 `RESOURCE_EXHAUSTED`（https://github.com/google-antigravity/antigravity-cli/issues/46 、https://github.com/google-antigravity/antigravity-cli/issues/594 ）
- **agy 額度訊息的官方字串定義，截至 2026-10-11 查無**（官方 docs 只有 `/usage` 與 headless 文件，未列 quota 錯誤訊息）。

### 3.2 `/usage` 的內容

- 官方：`/usage`（別名 `/quota`）開啟前會從 backend 刷新 quota，再開互動式 TUI 面板；面板為各模型用量上限與剩餘 request／token 明細（出處：https://www.antigravity.google/docs/cli/commands/usage/ ）。鍵盤：↑↓／PgUp PgDn／G／Esc（Q）關閉（同出處）。
- 各列格式（公開回報，非官方）：模型名＋百分比條，如 `40% remaining · Refreshes in 126h 49m`；不同模型不同剩餘量（`100%` 顯示 `Quota available`）（https://github.com/google-antigravity/antigravity-cli/issues/46 ）。
- **`/usage` 有無非互動／JSON 輸出，截至 2026-10-11 查無**（官方只記載 TUI 面板；且 `/usage` 不能餵進 `--input-format stream-json` session，會以 ERROR 結果結束。出處：https://www.antigravity.google/docs/cli/headless/ ）。

### 3.3 headless（`agy -p`）供監督程式用的部分

- 成功 exit `0`；失敗非零並把原因寫 stderr；`json`／`stream-json` 另有 `status`（`SUCCESS`／`ERROR`／`CANCELED`／`INTERRUPTED`／`INVALID`／`WAITING`／`RUNNING`）與 `error` 欄位；response 正常走 stdout、診斷走 stderr；預設最多等 5 分鐘（`--print-timeout`，如 `--print-timeout 15m`）；stream 形狀為 `init` → `step_update*` → `result`（`result` 同 `json` envelope，含 `usage` token 計數，無 quota 欄位）（出處：https://www.antigravity.google/docs/cli/headless/ ）。
- **額度用完時 headless 的 `error` 字串與 exit code 是否與 3.1 相同、有無專屬 quota 事件，截至 2026-10-11 查無**（官方 headless 文件只示範未知 `--model` 的 `ERROR`＋exit 1）。

## 4. 用量／額度查詢 API（程式可用、免 key 以外皆需 key——本次未呼叫，僅列文件）

### 4.1 Ollama Cloud：有（官方）

- 用量統計：`GET https://ollama.com/api/usage?range=24h|7d|30d[&scope=self|team]`（Bearer API key），回 `totals`（`request_count`、`usage_usd`、`input_tokens`、`cached_input_tokens`、`output_tokens`）＋`buckets`；限 10 req/min（429 附 `Retry-After`）（出處：https://docs.ollama.com/api/cloud-usage ）。注意：這是**歷史用量**，不含剩餘配額。
- 餘額／配額：`GET https://ollama.com/api/balance`（Bearer），現行方案回 `included { balance_usd, allowance_usd, period { from, until } }`＋`purchased { balance_usd }`；**legacy plan（session／weekly 限額制）回 `included.session`／`included.weekly` 各含 `remaining_percent`（0–100）與 `resets_at`（UTC）**——正是監督程式要的「剩餘＋重置時間」（出處：https://docs.ollama.com/api/balance ）。
- 限流回應的通用格式見 https://docs.ollama.com/api/errors 。
- session／weekly 專用查詢端點目前只有 feature request，**正式端點截至 2026-10-11 查無**（公開網頁，非官方：https://github.com/ollama/ollama/issues/15132 、https://github.com/ollama/ollama/issues/15663 、https://github.com/ollama/ollama/issues/16448 ）。

### 4.2 OpenCode Zen：無公開文件

- **Zen 餘額／用量查詢的公開 API 文件，截至 2026-10-11 查無**。公開的 feature request：`GET /zen/v1/balance`（https://github.com/anomalyco/opencode/issues/10448 、https://github.com/anomalyco/opencode/issues/10447 ）、把餘額併入 `GET /zen/go/v1/usage`（https://github.com/anomalyco/opencode/issues/44189 ，內稱該端點回 rolling／weekly／monthly 百分比）。
- 相關但非 quota：Console Usage CSV 匯出 `GET /api/v1/usage/export`（service account key；歷史用量，非剩餘配額）（出處：https://dev.opencode.ai/console/guides/usage ）。
- Zen 官方文件只有模型端點（`https://opencode.ai/zen/v1/...`）、`/connect` 接入、auto-reload（低於 $5 自動加 $20）與 monthly limit 說明（出處：https://opencode.ai/docs/zen/ ）。

### 4.3 OpenRouter：有（官方）

- `GET /api/v1/key` 查 `limit_remaining`／`limit_reset`（出處：https://openrouter.ai/docs/api_reference/limits ）。

## 5. 給監督程式的速查表

| 來源 | 偵測字串／事件 | 重置時間可得？ |
|---|---|---|
| Claude Code 畫面／`-p` stdout | `You've hit your (session\|weekly\|Opus\|Sonnet) limit · resets …` | ✅ 訊息內 |
| Claude Code stream-json | `system/api_retry`（`error: rate_limit` 等） | ⚠️ 無絕對時間，只有 `retry_delay_ms` |
| Claude Code hooks | `StopFailure`（matcher `rate_limit` 等）＋`last_assistant_message` | ⚠️ 同上，建議配文字 `You've hit your` |
| Claude Code statusline | `rate_limits.five_hour／seven_day.{used_percentage,resets_at}` | ✅（epoch 秒；僅 Pro／Max；setup-token 查無） |
| opencode Zen 免費 | `FreeUsageLimitError`／`Free usage exceeded, subscribe to Go`＋`[retrying in … attempt #N]` | ✅（`retry-after` header／footer） |
| opencode Zen Go | `GoUsageLimitError`／`<limit> usage limit reached. It will reset in …` | ✅（`retry-after`＋訊息內） |
| opencode 通用 | `session.error`（`statusCode` 402／429、`responseHeaders`、`responseBody` 內 `FreeUsageLimitError`／`GoUsageLimitError`／`CreditLimitExceeded`） | ✅（header 為主） |
| OpenRouter | 402 `limit_source`（`openrouter_in_flight_budget` 看 `Retry-After`）／429 `X-RateLimit-*`；`GET /api/v1/key` | ✅ |
| Ollama Cloud | 429 `{"error":"…session usage limit…"}`；`GET /api/balance`（legacy 有 `remaining_percent`＋`resets_at`） | ✅（API） |
| agy | `Individual quota reached. … Resets in 143h57m55s`／`RESOURCE_EXHAUSTED`；`/usage` TUI（`40% remaining · Refreshes in …`） | ✅（訊息內；headless 查無） |
| OpenCode Zen 查詢 API | —— | ❌ 截至 2026-10-11 查無公開文件 |

## 6. 本次刻意未確認（需金鑰／需實機，超出本次授權）

- 各 agent 在額度用完當下的實際 stdout／exit code／事件 payload（需真實跑到限額）。
- `CLAUDE_CODE_OAUTH_TOKEN`（setup-token）下 statusline `rate_limits` 有無。
- `/zen/go/v1/usage` 是否為穩定公開端點（僅見於 issue 討論與程式碼引用，無公開文件）。
