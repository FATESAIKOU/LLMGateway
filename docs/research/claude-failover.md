# Claude 訂閱 token 兩把的切換：行為、可觀測訊號與三種切換做法（調查）

> 調查日期：2026-10-10。範圍：Claude Code 在 `CLAUDE_CODE_OAUTH_TOKEN` 下的限流行為與可程式訊號、`anthropic-ratelimit-unified-*` 官方文件、三種切換做法的可行性、訂閱條款原文。
> 規則：只依官方文件與公開條款；本檔不含任何金鑰／token 實際值；涉及合規的段落只整理原文，不代為下結論。
> 姊妹文件：`docs/research/agent-endpoints.md`（各 agent 指向閘道的方式）。

## 0. 摘要（TL;DR）

- **限流訊號**：Claude Code 有官方可程式訊號——hooks 的 `StopFailure`（matcher 含 `rate_limit`）、`Notification` 的 `quota_auto_resume_*`、`-p` 的 `system/api_retry` 事件與非零 exit code（SIGTERM 為 143）、statusline 的 `rate_limits.five_hour` / `seven_day`。互動模式另有內建「等 reset 後自動續跑」。
- **`anthropic-ratelimit-unified-*`**：官方文件**有**正式名稱（LLM gateway 相容指南的 response headers 表），要求閘道**原值轉發**；Claude Code 用它在成功回應上顯示方案用量（statusline 5h／7d），並在 429 時分辨「方案限額」與「暫時節流」。閘道只要不被剝掉就能讀到。
- **三種切換**：(a) wrapper 換 token ＋ 重啟接續 session：官方 resume 機制完整（`session_id` → `--resume`），可行且可留下紀錄；(b) 閘道換上游帳號：對 Claude Code 完全透明（「can't tell which upstream you forward to」），但要處理 OAuth 透傳與條款限制，且「切換」只能在閘道端留紀錄；(c) `apiKeyHelper`：官方定位是「產生 API 憑證」，拿它回傳 claude.ai 訂閱 OAuth token，截至 2026-10-10 查無文件支持，較適合輪替閘道金鑰、不是兩把訂閱 token 的切換器。
- **條款**：官方明文禁止分享帳號／憑證、禁止第三方代客路由訂閱憑證；但截至 2026-10-10，**沒有**「本人多台機器」的數量限制條文。兩把 token（個人＋公司）都給本人自己的 agent 用是否合規，取決於公司訂閱的合約與內部政策，本檔只列原文。

---

## 1. Q1：用量用完／被限流時，Claude Code 的行為與可程式訊號

### 1.1 使用者看到的訊息

- 訂閱方案是滾動式用量：用完時顯示 `You've hit your session limit · resets 3:45pm`、`You've hit your weekly limit · resets Mon 12:00am`、`You've hit your Opus limit · resets 3:45pm`、`You've hit your Sonnet limit · resets 3:45pm`。
  <https://code.claude.com/docs/en/errors#youve-hit-your-session-limit>
- session 與 weekly 限額**跨模型共用**，換模型不會恢復；Opus／Sonnet 限額只綁該模型家族，換到家族外的模型可繼續。
  <https://code.claude.com/docs/en/errors#youve-hit-your-session-limit>
- 接近上限時會先出現 `You've used 85% of your session limit · resets 3:45pm` 這類警告。
  <https://code.claude.com/docs/en/errors#youve-hit-your-session-limit>
- 其他相關訊息：`Server is temporarily limiting requests (not your usage limit)`（伺服器端短暫節流，非方案配額；Claude Code 用「是否有 unified quota headers」分辨兩者）、`API Error: Request rejected (429)`（API key／Bedrock／GCP 的 rate limit；自訂 `ANTHROPIC_BASE_URL` 時訊息會指向閘道主機）。
  <https://code.claude.com/docs/en/errors#server-is-temporarily-limiting-requests>、<https://code.claude.com/docs/en/errors#request-rejected-429>
- 官方也有 `/usage`（看方案限額與 reset 時間）與 `/usage-credits`（Pro/Max 加購用量；Team/Enterprise 向 admin 申請）。
  <https://code.claude.com/docs/en/errors#youve-hit-your-session-limit>

### 1.2 互動模式：blocked 之後的內建等待與自動續跑

- 達限後 Claude Code 擋掉後續請求，直到訊息顯示的 reset 時間。
  <https://code.claude.com/docs/en/errors#youve-hit-your-session-limit>
- 以 claude.ai 訂閱登入的互動 session（v2.1.234+）會**自動等 reset 並續跑**（預設開啟）：畫面顯示 `Usage limit reached · limit resets 3:45pm`／`Continuing automatically at 3:45pm · esc to cancel`；reset 後自動接續、**不會重送**上一條訊息。
  <https://code.claude.com/docs/en/interactive-mode#wait-for-a-usage-limit-to-reset>
- 期間若加購 usage credits、`/upgrade` 後重新登入、或 `/model` 換到可用模型，會提早續跑；連續再次撞限最多自動重試 **2 次**，之後顯示 `Automatic continue stopped after repeated usage-limit hits`。
  <https://code.claude.com/docs/en/interactive-mode#wait-for-a-usage-limit-to-reset>
- 取消：`Esc`／`Ctrl+C`，或 `/rate-limit-options` 選 **Don't continue automatically**；設定鍵 `autoContinueAtUsageLimit`（預設 true，只從 user／`--settings`／managed 讀）。
  <https://code.claude.com/docs/en/interactive-mode#cancel-the-wait>、<https://code.claude.com/docs/en/settings-reference#autocontinueatusagelimit>
- **重要限制**：`-p` 執行與背景 session **不提供**這個等待選項；API key／雲端 provider／按量計費也沒有 reset 可等；**走 LLM gateway 且沒有已存 claude.ai 登入**時，Claude Code 也不提供等待（只有已存的 claude.ai 登入是生效憑證時才提供）。
  <https://code.claude.com/docs/en/interactive-mode#start-a-wait-yourself>

### 1.3 `-p`（headless）模式：exit code

- 官方定義：`claude -p` **成功 exit 0、失敗非零**；若失敗發生在 run 內（例如缺認證），Claude Code 會把失敗**當作 result 印在 stdout**（不是只在 stderr）。
  <https://code.claude.com/docs/en/headless>
- 以 SIGTERM 停止時 **exit 143**：正在進行的 turn 不會完成、不留 result；會跑 `SessionEnd` hooks；resume 時被中斷的 turn 保持原狀，下一條 prompt 繼續；若要 resume 時**自動續跑被中斷的 turn**，設 `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`。
  <https://code.claude.com/docs/en/headless#stop-a-run-with-sigterm>
- Wrapper／SDK 端看到的是一般非零 exit code；官方明講「**exit code 本身不會說哪裡失敗**，答案在 process 自己的輸出裡」。
  <https://code.claude.com/docs/en/errors#claude-code-process-exited-with-code-n>
- Agent SDK 對「CLI 以錯誤 result 結束」另立錯誤型別（Python `ResultError`，`data` 帶完整 error result；TypeScript 為一般 Error，訊息形如 `Claude Code returned an error result: ...`）；單純非零退出是 `ProcessError`（Python）／訊息 `Claude Code process exited with code N. stderr: ...`。
  <https://code.claude.com/docs/en/agent-sdk/troubleshooting#cli-process-exit>、<https://code.claude.com/docs/en/agent-sdk/troubleshooting#claude-code-returned-an-error-result>

### 1.4 可程式判讀訊號（官方文件有載）

**(a) Hooks：`StopFailure`（最直接）**

- 觸發時機：**turn 因 API 錯誤結束時**（取代 `Stop`）。matcher 是錯誤類型，含 `rate_limit`、`overloaded`、`authentication_failed`、`oauth_org_not_allowed`、`account_on_hold`、`billing_error`、`invalid_request`、`model_not_found`、`server_error`、`max_output_tokens`、`cloud_credential_error`、`unknown`。
  <https://code.claude.com/docs/en/hooks#stopfailure>
- 輸入欄位：`error`（類型）、`error_details`（可選）、`last_assistant_message`（**API 錯誤字串本身**，例如 `"API Error: Rate limit reached"`）。
  <https://code.claude.com/docs/en/hooks#stopfailure-input>
- 限制：**沒有 decision control**——hook 的輸出與 exit code 被忽略；純粹用來記 log、發警報、做恢復動作。
  <https://code.claude.com/docs/en/hooks#stopfailure>

**(b) Hooks：`Notification` 的 `quota_auto_resume_*`**

- `quota_auto_resume_fired`：等待結束、自動續跑已觸發（reset 到、或加購／升級／換模型使額度提前可用）。
- `quota_auto_resume_stale`：電腦睡超過約 30 分鐘、reset 期間過去，改為等使用者按 Enter。
- `quota_auto_resume_disabled`：等待結束但**不續跑**（例如 `autoContinueAtUsageLimit` 關掉、reset 移到 24 小時外、連續撞限等）。需 v2.1.234+。
  <https://code.claude.com/docs/en/hooks#notification>、<https://code.claude.com/docs/en/hooks#notification-input>

**(c) `-p` 的 stream-json：`system/api_retry` 與 result**

- 可重試錯誤發生時先出 `system/api_retry` 事件，欄位含 `attempt`、`max_retries`、`retry_delay_ms`、`error_status`，以及 `error` 類別——**`rate_limit`** 是其中之一（另有 `authentication_failed`、`billing_error`、`overloaded`、`server_error` 等）。
  <https://code.claude.com/docs/en/headless#handle-api-retries>
- 串流最後一行是 `result` 訊息（最終回覆、成本、session metadata）；`--output-format json` 亦回 `session_id`、`total_cost_usd` 等。
  <https://code.claude.com/docs/en/headless#get-structured-output>、<https://code.claude.com/docs/en/headless>
- 註：`apiKeyHelper` 憑證被 401/403 拒絕時，前兩次重試**不發事件**（安靜重試），第三次起才發 `api_retry`。
  <https://code.claude.com/docs/en/headless#handle-api-retries>

**(d) statusline：`rate_limits`（主動監控）**

- status line 的 stdin JSON 含 `rate_limits.five_hour.used_percentage`／`resets_at`、`rate_limits.seven_day.used_percentage`／`resets_at`（0–100，Unix epoch 秒）。**只在 claude.ai Pro/Max 訂閱者、或有 spend limit 的 Claude apps gateway 後面出現**，且要等 session 第一個 API 回應之後；視窗過了 `resets_at` 就從資料裡消失。
  <https://code.claude.com/docs/en/statusline#rate-limit-usage>、<https://code.claude.com/docs/en/statusline#available-data>
- `spend_limit.used_percentage`、`used_usd`、`limit_usd`、`period` 是 Claude apps gateway 的 spend limit 欄位（v2.1.251+；金額欄位需 v2.1.284+）。
  <https://code.claude.com/docs/en/statusline#spend-limit-fields>

**(e) `claude auth status`（驗證生效憑證）**

- `claude auth status` 輸出 JSON：登入時 exit 0、未登入 exit 1；`authMethod` 為 `none`／`claude.ai`／`oauth_token`／`api_key`／`api_key_helper`／`third_party`，另有 `configDirectory` 欄位（v2.1.268+）。
  <https://code.claude.com/docs/en/cli-reference>

> 小結：**「額度用完」在 Claude Code 裡不是單一事件、而是好幾層訊號**：互動中是畫面訊息＋內建等待；程式面以 `StopFailure(rate_limit)` 與 `Notification(quota_auto_resume_*)` 最直接；`-p` 以 `api_retry`＋非零 exit code＋result 訊息呈現。截至 2026-10-10 的文件中，沒有任何一個訊號會「自動切換憑證」——切換是外部系統（wrapper／閘道）要自己做的事。

---

## 2. Q2：`anthropic-ratelimit-unified-*` 的官方文件與閘道可讀性

### 2.1 官方文件確實有

- LLM gateway 相容指南的 **Response headers** 表格原文：

  | Header | What to return and why |
  | :- | :- |
  | `anthropic-ratelimit-unified-*` | Forward the upstream's values unchanged on every response. Claude Code reads them on successful responses to show usage against plan limits to developers signed in with claude.ai, and on a `429` to tell a plan limit or spend cap from a temporary throttle; see usage limits |

  <https://code.claude.com/docs/en/llm-gateway-protocol#response-headers>
- Errors 頁也從側面證實：`Server is temporarily limiting requests` 的辨識方式是「**the absence of the unified quota headers** a real limit response carries」。
  <https://code.claude.com/docs/en/errors#server-is-temporarily-limiting-requests>
- 官方只用 wildcard `anthropic-ratelimit-unified-*` 指稱，截至 2026-10-10 **沒有逐一列出** 5h／7d utilization 等 header 全名；能對應的官方欄位是 statusline 的 `rate_limits.five_hour`／`seven_day`（見 §1.4d）。
  <https://code.claude.com/docs/en/llm-gateway-protocol#response-headers>、<https://code.claude.com/docs/en/statusline#rate-limit-usage>

### 2.2 閘道能不能讀到

- **可以**：它就在上游回應的 HTTP response headers 裡；官方對閘道的要求就是原值轉發、不要改寫。若閘道把 header 剝掉，Claude Code 會把方案限額誤判成「暫時節流」，statusline 的 5h／7d 用量也可能拿不到。
  <https://code.claude.com/docs/en/llm-gateway-protocol#response-headers>、<https://code.claude.com/docs/en/errors#server-is-temporarily-limiting-requests>
- **前提**：上游要是會回這些 header 的一方（claude.ai 訂閱流量直通 Anthropic）。若閘道把請求轉去 Bedrock／Vertex 等第三方，截至 2026-10-10 官方沒有說那裡也有 unified headers（response headers 表把「要不要回 unified」放在 Anthropic Messages 格式情境說明；成功回應顯示用量也只描述給 claude.ai 登入者）。
  <https://code.claude.com/docs/en/llm-gateway-protocol#response-headers>、<https://code.claude.com/docs/en/statusline#available-data>
- 閘道若同時當「換帳」決策點，這些 header 是它**最即時**的限流依據（比抓 OTel／輪詢 /usage 更直接）；但截至 2026-10-10，官方沒有提供 header 的完整名稱清單，實作時要照 wildcard 精神全部透傳，而不是 allowlist 幾個名字。
  <https://code.claude.com/docs/en/llm-gateway-protocol#forward-as-open-lists>

---

## 3. Q3：三種切換做法的比較

需求前提：兩把 `CLAUDE_CODE_OAUTH_TOKEN`（個人為主、公司備援）；切換**不能安靜**（要留紀錄／通知）。

### 3.1 (a) 包裝程式在 agent 外面偵測後換 token、重啟並接續 session

**官方可用的零件**

- `CLAUDE_CODE_OAUTH_TOKEN` 的讀取時機：**每個新 session 都會重新讀**；設定它會蓋過 keychain 內的憑證；在互動中跑 `/login` 只會把「當前 session」換成新登入，變數仍在，直到你從 shell profile／settings `env` 移除。→ wrapper 只要重啟一個新 process，就會拿到新的 token。
  <https://code.claude.com/docs/en/authentication#authentication-precedence>、<https://code.claude.com/docs/en/env-vars>
- session 接續：`--output-format json` 可拿 `session_id`；`--resume <session_id>` 可續接（可在不同目錄執行，Claude Code 會在整台機器找該 ID，v2.1.223+）；`--fork-session` 可另開新 ID。
  <https://code.claude.com/docs/en/headless#continue-conversations>、<https://code.claude.com/docs/en/cli-reference>
- 中斷語意：SIGTERM → exit 143、當前 turn 不完成（不留 result）、會跑 `SessionEnd` hooks；resume 後「被中斷的 turn 保持原狀，下一條 prompt 驅動」，要自動續跑該 turn 設 `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`。官方建議：若要讓該 turn 正常結束而非留下未完成狀態，先送 SIGINT（或 SDK `interrupt()`）再停程序。
  <https://code.claude.com/docs/en/headless#stop-a-run-with-sigterm>
- 偵測：wrapper 可從 `-p --output-format stream-json` 讀 `system/api_retry` 的 `error: rate_limit`，或直接從非零 exit code＋stdout 的失敗 result 判斷；長駐互動 session 則可在 settings 掛 `StopFailure` hook（matcher `rate_limit`）把事件寫去 wrapper 的 socket／檔案。
  <https://code.claude.com/docs/en/headless#handle-api-retries>、<https://code.claude.com/docs/en/hooks#stopfailure>
- 驗證現況：`claude auth status`（exit 0/1、`authMethod`）可讓 wrapper 判斷目前生效的是 `oauth_token` 還是別的。
  <https://code.claude.com/docs/en/cli-reference>

**可行性**：高。這是官方機制拼得出來的路線，不需要改 Claude Code binary。
**session 連續性**：重啟瞬間當前 turn 會斷（除非先以 SIGINT 收尾）；transcript 留在磁碟上，用 `--resume <session_id>` 接續、必要時 `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` 讓被中斷的 turn 自動續跑；對話歷史完整保留。
**「不安靜」怎麼做**：切換動作全在 wrapper，天然可以留完整紀錄——audit log 記時間、舊／新 token 的**來源標籤**（不是值）、原因（StopFailure 的 `error`／`error_details`）、`session_id`、新 process PID；通知用 wrapper 自己的通道（桌面通知、IM）。Claude Code 端可再加 `SessionStart` hook 在續接的 session 開頭把「已切換到備援」寫進對話／log。
  <https://code.claude.com/docs/en/hooks#sessionstart>
**風險／限制**：`-p` 與背景 session 沒有內建「等 reset」可用（§1.2），所以失敗偵測與切換節奏要 wrapper 自己做；重啟期間正在跑的 tool call 會依 SIGTERM 規則被處理（命令記為 killed）。
  <https://code.claude.com/docs/en/headless#stop-a-run-with-sigterm>

### 3.2 (b) 閘道在中間換上游帳號

**官方描述的透明性**

- Anthropic Messages 格式連線下：「The developer sets `ANTHROPIC_BASE_URL` to your gateway. **Claude Code treats the gateway as the Claude API and can't tell which upstream you forward to.**」
  <https://code.claude.com/docs/en/llm-gateway-protocol#how-the-connection-method-changes-client-behavior>
- 閘道的價值主張之一就是「**Provider switching**: change the provider in gateway configuration, without touching developer machines」，且「the provider key stays server-side」。
  <https://code.claude.com/docs/en/llm-gateway>

**技術注意**

- 訂閱流量透傳：只設 `ANTHROPIC_BASE_URL`、不設閘道憑證時，請求仍帶開發者的 claude.ai 登入；閘道若轉給 Anthropic，**必須轉發 `anthropic-beta` 裡的 OAuth capability**，否則 401。
  <https://code.claude.com/docs/en/llm-gateway>、<https://code.claude.com/docs/en/llm-gateway-protocol#request-headers>
- 要讓 Claude Code 端「看得見」切換：閘道可把上游的 `anthropic-ratelimit-unified-*`**原值轉發**（§2）；若是 Claude apps gateway 的 spend limit，還能讓 statusline 顯示 `rate_limits.spend_limit.used_usd` 等欄位（v2.1.284+ 雙邊版本）。
  <https://code.claude.com/docs/en/llm-gateway-protocol#response-headers>、<https://code.claude.com/docs/en/statusline#spend-limit-fields>
- 官方描述這種連線下換上游帳號對 client **不可見**（「Claude Code treats the gateway as the Claude API and can't tell which upstream you forward to」）；走閘道（沒有已存 claude.ai 登入）時，Claude Code 也不提供 usage-limit 等待。
  <https://code.claude.com/docs/en/llm-gateway-protocol#how-the-connection-method-changes-client-behavior>、<https://code.claude.com/docs/en/interactive-mode#start-a-wait-yourself>
- 用量回報：閘道憑證生效時，Claude Code 不向 Console analytics dashboard 回報用量——**閘道端要自己記**。
  <https://code.claude.com/docs/en/llm-gateway-connect>
- 條款面（見 §4）：官方條文要求 end user 用**自己的**憑證認證、且不得「pay for, resell, or intermediate Claude usage on their end users' behalf」；閘道若只是「同一個人自己的兩把訂閱、自己的機器」，與「代客路由」不同，但截至 2026-10-10，官方沒有對這種 failover 用法寫過任何正面許可，需自行承擔解讀風險。
  <https://code.claude.com/docs/en/legal-and-compliance>

**可行性**：技術上高（client 完全透明）。
**session 連續性**：**完全不受影響**——同一 session、同一 process，換帳發生在 server 端。
**「不安靜」怎麼做**：紀錄只能在閘道端（access log＋換帳事件 log，記 request-id／`x-claude-code-session-id`）。Claude Code 端截至 2026-10-10 沒有官方管道顯示「上游換人了」；能做的近似手段是把限流資訊照實轉發（unified headers／spend limit），讓 `/status`、statusline、`StopFailure` hook 反映的是「當下這把」的狀態。
  <https://code.claude.com/docs/en/llm-gateway-protocol#request-headers>

### 3.3 (c) `apiKeyHelper` 動態回傳憑證

**官方定義**

- 用途：「Run your own command to produce **the credential Claude Code sends** with model requests」；輸出會被送進 **`X-Api-Key` 與 `Authorization: Bearer` 兩個 header**；定位是「dynamic or rotating credentials」。
  <https://code.claude.com/docs/en/settings-reference#apikeyhelper>
- 重跑時機：快取 5 分鐘（可用 `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` 調）；遇到 API（直連或經 LLM gateway）回 **401／403** 時重跑；快取的輸出是**已過期的 JWT** 時在下一次請求前重跑（後兩者只在「helper 輸出就是生效憑證」且 `ANTHROPIC_AUTH_TOKEN` 未設時適用，v2.1.246+）。
  <https://code.claude.com/docs/en/settings-reference#apikeyhelper>
- 憑證優先序：`apiKeyHelper` 排第 4，**在 `CLAUDE_CODE_OAUTH_TOKEN`（第 5）之前**——只要 helper 生效，訂閱 token 就不會被用到。
  <https://code.claude.com/docs/en/authentication#authentication-precedence>
- 失敗行為：helper 失敗時請求帶 placeholder key，錯誤為 `Your apiKeyHelper script is failing`；401/403 的重跑前兩次是安靜的（見 §1.4c）。
  <https://code.claude.com/docs/en/errors#your-apikeyhelper-script-is-failing>、<https://code.claude.com/docs/en/headless#handle-api-retries>

**可行性**：

- 用於**輪替閘道金鑰／API key**：完全符合官方設計，可行。
  <https://code.claude.com/docs/en/llm-gateway-connect#rotate-credentials-with-apikeyhelper>
- 用於**兩把 claude.ai 訂閱 OAuth token 的切換**：截至 2026-10-10，官方文件**查無**支持——helper 文件把它描述成產生「API 憑證」的機制，`setup-token` 產的訂閱 OAuth token 官方指定的載體是 `CLAUDE_CODE_OAUTH_TOKEN`；把訂閱 token 從 helper 吐出來、經 `X-Api-Key`／`Authorization` 打 `api.anthropic.com` 是否被接受，沒有文件可引，**不宜列為可行方案**。
  <https://code.claude.com/docs/en/settings-reference#apikeyhelper>、<https://code.claude.com/docs/en/authentication#generate-a-long-lived-token>

**session 連續性**：若真的用於閘道金鑰，請求層更換、session 不受影響；但**切換時機不可控**（快取最長 5 分鐘，或等 401/403 才重跑），不適合「額度到了立即換」。
**「不安靜」怎麼做**：Claude Code 不會通知 helper 被重跑；唯一做法是 helper 腳本自己寫 log／發通知（但那是「你自己的紀錄」，不是 Claude Code 的官方訊號）。

### 3.4 三法比較表

| 面向 | (a) wrapper 換 token＋重啟 | (b) 閘道換上游帳號 | (c) apiKeyHelper |
| :- | :- | :- | :- |
| 官方機制支持 | 完整（resume／exit code／hooks／auth status） | 完整（gateway 設計本意就是換上游） | 只用於 API key／閘道金鑰；訂閱 OAuth 截至 2026-10-10 查無文件 |
| 偵測限流 | `StopFailure(rate_limit)`、`api_retry`、exit code、statusline | 讀上游 `anthropic-ratelimit-unified-*`（最即時） | 只能等 401/403 或 TTL，被動 |
| session 連續性 | 重啟＋`--resume`；被中斷 turn 需 `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` | 不受影響 | 不受影響（但換不到訂閱 token） |
| 換證延遲 | 立即（重啟） | 立即（server 端） | 最長 5 分鐘或 401/403 才換 |
| 「不安靜」紀錄 | wrapper audit log＋通知；可加 `SessionStart` hook 公告 | 僅閘道端 log；Claude Code 端隱形（可轉發 unified headers 讓狀態可見） | 僅 helper 自己的 log；Claude Code 不做任何通知 |
| 主要風險 | 中斷一個 turn；重啟成本 | 訂閱透傳需轉發 OAuth beta；條款解讀風險；用量要自己記 | 對訂閱 token 屬未證實用法；與訂閱 token 混用時 helper 優先序會蓋掉訂閱 |

---

## 4. Q4：訂閱條款的官方說法（只整理原文）

> 以下為原文摘錄與出處，**不是**對「個人為主、公司備援」是否合規的結論。判定取決於公司 token 的合約性質（Team/Enterprise seat？誰是 Customer？）與公司內部政策。

### 4.1 消費者條款（Free／Pro／Max）

- 不得分享帳號／憑證原文：
  > "You may not share your Account login information, Anthropic API key, or Account credentials with anyone else. You also may not make your Account available to anyone else."
  <https://www.anthropic.com/legal/consumer-terms>（Section 2, Account creation and access）
- 自動化存取限制（例外為 API key 或明示許可）：
  > "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise."
  <https://www.anthropic.com/legal/consumer-terms>（Section 3.7）
- 不得繞過保護措施：
  > "You also must not abuse, harm, interfere with, or disrupt our Services, including, for example, ... bypassing any of our systems or protective measures."
  <https://www.anthropic.com/legal/consumer-terms>（Section 3 末段）

### 4.2 Claude Code 官方法務頁（訂閱與憑證用途）

- 用量假設：
  > "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."
- OAuth 的定位：
  > "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications."
- 第三方開發者的禁制（與「閘道代客路由」直接相關）：
  > "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users. Moreover, developers may not collect, store, or intermediate Claude.ai credentials or session tokens — sign-in to a Claude account must complete through Anthropic's own flow."
- 但明示不限制本人的 API key 管理，也不阻止使用者以自己訂閱登入**未修改的** Claude Code：
  > "This does not restrict how customers provision and manage their own API keys or third-party inference provider credentials — ... provided the resulting usage is billed to the key owner under their agreement with Anthropic (or the applicable provider) and is not resold or intermediated as described above. Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription..."
- 託管 Claude Code 的條件（公司若把 Claude Code 裝進內部平台適用）：
  > "The Claude Code binary must not be modified."（含不得移除／停用內建認證方式）
  > "Customers may not pay for, resell, or intermediate Claude usage on their end users' behalf."（每個 end user 要用自己的憑證）
  以上皆出自 <https://code.claude.com/docs/en/legal-and-compliance>

### 4.3 商業條款（Team／Enterprise／API）

- 適用範圍：Team／Enterprise／API 由 Commercial Terms 管（Claude Code 法務頁指明）；「Services under these Terms are not for consumer use」。
  <https://code.claude.com/docs/en/legal-and-compliance>、<https://www.anthropic.com/legal/commercial-terms>
- 使用限制含：不得轉售 Services（未經 Anthropic 明示同意）：
  > "Customer may not ... (a) access the Services to build a competing product or service, including to train competing AI models or **resell the Services** except as expressly approved by Anthropic; ..."
  <https://www.anthropic.com/legal/commercial-terms>（Section D.4）
- 帳號責任在 Customer（公司）；activity under its account 由 Customer 負責。
  <https://www.anthropic.com/legal/commercial-terms>（Section D.5）

### 4.4 「跨機器」這件事，官方文件怎麼說

- 截至 2026-10-10，**沒有找到**任何「一個訂閱限幾台機器」的條文。相關的官方陳述是：
  - 不得把帳號／憑證給他人、不得讓他人使用你的帳號（§4.1 原文）。
  - usage limits 以「ordinary, individual usage」為前提（§4.2 原文）。
  - Claude Code 反而官方支援同一人多身分／多裝置的用法：多帳號可用 `CLAUDE_CONFIG_DIR` 分開登入；文件也描述跨機器 session（cross-session messaging 等）。
    <https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts>、<https://code.claude.com/docs/en/cross-session-messaging>
- 因此「同一人在自己的多台機器上用自己兩把訂閱」在官方文件上找不到禁止文字；但**自動 failover**（在一把限流時把流量改到另一把）是否屬於「bypassing protective measures」或超出「ordinary, individual usage」，截至 2026-10-10 官方沒有任何正面或負面說明，屬未定義地帶。本檔不代為定論；建議在 MyPMO 開一張等本人的裁定單處理。

---

## 5. 對 LLMGateway 設計的含意（整理，非結論）

1. **想「不換帳、只路由」**：維持只設 `ANTHROPIC_BASE_URL`、不設閘道憑證，訂閱仍是計費主體（見 agent-endpoints.md §1）；但這樣就沒有兩把 token 的 failover。
2. **想做無感 failover**：(b) 閘道換上游帳號對 Claude Code 最無感，但官方用量等待功能會因「沒有已存 claude.ai 登入」而不可用，且需自行處理 OAuth beta 透傳與條款解讀。
3. **想「絕不安靜」**：(a) 的紀錄能力最好（wrapper 掌握全程）；(b) 只能閘道端記錄，但可以把 unified headers 照實轉發，讓 Claude Code 端（statusline／錯誤訊息）呈現的是「當下這把」的真實狀態，這是最接近官方精神的做法。
4. **官方自己的替代路線**：互動 session 的「等 reset 自動續跑」（§1.2）就是 Anthropic 對限流的正規解；若需求只是「跑到一半被限流」，這是最無爭議的選項，`-p` 場景則需 wrapper 自行實作等效機制。

## 6. 出處清單

- Claude Code 錯誤參考：<https://code.claude.com/docs/en/errors>
- CLI reference：<https://code.claude.com/docs/en/cli-reference>
- Headless（`-p`、SIGTERM、json/stream-json、api_retry、resume）：<https://code.claude.com/docs/en/headless>
- Hooks（StopFailure、Notification、SessionStart）：<https://code.claude.com/docs/en/hooks>
- Interactive mode（等 reset 自動續跑）：<https://code.claude.com/docs/en/interactive-mode>
- Statusline（rate_limits 5h/7d、spend_limit）：<https://code.claude.com/docs/en/statusline>
- Settings reference（apiKeyHelper、autoContinueAtUsageLimit）：<https://code.claude.com/docs/en/settings-reference>
- Environment variables：<https://code.claude.com/docs/en/env-vars>
- Authentication（優先序、setup-token、多帳號）：<https://code.claude.com/docs/en/authentication>
- LLM gateway 相容指南（response headers、request headers）：<https://code.claude.com/docs/en/llm-gateway-protocol>
- LLM gateway 總覽／連接：<https://code.claude.com/docs/en/llm-gateway>、<https://code.claude.com/docs/en/llm-gateway-connect>
- Agent SDK troubleshooting（exit code／error result）：<https://code.claude.com/docs/en/agent-sdk/troubleshooting>
- Legal and compliance（OAuth 用途、託管條件）：<https://code.claude.com/docs/en/legal-and-compliance>
- Consumer Terms：<https://www.anthropic.com/legal/consumer-terms>
- Commercial Terms：<https://www.anthropic.com/legal/commercial-terms>
