## Context

動機與範圍見 proposal.md（第 3 版，本人已確認）。這裡只列影響做法的現況與限制：

- **Claude 條款**：訂閱的 OAuth 是給原版 Claude Code 這類 Anthropic 原生 app 的一般使用；第三方不得代為路由、收集、存放、中轉 Claude.ai 的 token（`docs/research/claude-failover.md` §7）。所以 LLMGateway 不能代轉 Claude 的請求，只能把 `CLAUDE_CODE_OAUTH_TOKEN` 交給本人自己的原版 Claude Code。
- **期 1 設計**（已封存為 `openspec/specs/agent-lease`、`openspec/specs/usage-record`）：控制面、租約、只追加事件、launcher 注入、切換留紀錄。v0 延伸它；差別見決定 1、2、3。
- **池**：池內有常駐的 provider，Mac 已經有經 SSH 跳板連入池內的身分。worker 是跑在 provider 上的容器。對外只有跳板機的 SSH 埠，其餘服務都不對外。
- **sidecar**（MyAiStorage#31，範圍 B，已開工）：用 herdr 開關 agent，用 `agora continue` 接回 session，用假的 `ModelBroker` 頂著。介面形狀已和 aistorage-pm 對齊（proposal 第 2 節）。
- **各上游**：OpenRouter、Ollama Cloud 有官方的 Claude Code 接法；Zen 只有 opencode（`upstream-claude-code-compat.md`）。額度訊號見 `quota-signals.md`。
- **Claude Code**：`--settings <file-or-json>` 可以在啟動時給設定；互動模式有 `autoContinueAtUsageLimit`，會在額度重置後自己續跑。

## Goals / Non-Goals

**Goals**
- 五把 key 只放一處，客戶端只在租約期間拿到其中一把。
- 換把的判斷只有一份（服務端），換把的程序也只有一份（sidecar 的 agent 宿主），worker 與 Mac 共用。
- 第三方依賴越少越好：服務與 `lgw` 都只用 Python 標準函式庫。

**Non-Goals**
- 不轉送任何模型請求（沒有資料面）。
- 不做 token／成本的計量；期 1 `usage-record` 的 token 回報不在 v0（見決定 11）。
- 不做依內容或依效能的改送（#3、#4）。

## Decisions

### 決定 1：LLMGateway＝保管處＋租用服務，沒有資料面

- 服務只做：保管 key、依政策出租、記錄、冷卻、狀態板。模型請求由 agent 直接打上游。
- **為什麼**：Claude 條款不允許代轉訂閱 token；其他三把也不需要代轉，因為 agent 本來就能直接用。少一層代轉，也就少一個集中處理所有請求內容的地方。
- **放棄的選項**：
  - 資料面閘道（PR #9 第 1 版）：Gateway 持有 Claude 訂閱 token 代轉，違反條款。
  - sub2api：它的價值正是代轉與訂閱帳號池。
  - LiteLLM 當核心：只在某個上游非要格式轉換才加，而且要固定版本並查核供應鏈（`litellm-supply-chain.md`）。

### 決定 2：key 的保管與部署

- **來源**：本 repo 的 GitHub secret，五把各一個，名稱只寫在設定與文件，值由本人設。
- **部署**：
  - 手動觸發的 workflow `deploy-keys`：經池的 SSH 跳板登入那台 provider，把五把寫進 `~/.local/state/llmgateway/keys.env`（0600，擁有者是服務帳號）；寫完送 SIGHUP 讓服務重讀。
  - 輪替 key 的流程：改 secret → 跑 workflow。
- **workflow 的登入身分**：MyLinuxPool 為本 repo 的 Actions 加一個 client 身分（公鑰登記在 MyLinuxPool，私鑰放本 repo 的 secret）。這要 MyLinuxPool 開單。
- workflow 的 log 只出現 secret 名稱；寫檔用 stdin 傳值，不上命令列。
- **為什麼**：本人已同意把來源放在這裡；GitHub secret 加上 Actions 是本人既有的做法（MyPMO、MyLinuxPool 都這樣）。
- **放棄的選項**：
  - 繼續放 MyLinuxPool 的 `POOL_WORKER__*`：本人裁定那只是過渡。
  - 本人手動把檔案放到 provider：不能重現，輪替也容易漏。

### 決定 3：服務的形狀

- **程式**：Python 標準函式庫，HTTP 用 `http.server.ThreadingHTTPServer`、資料用 `sqlite3`，沒有第三方套件。
- **位置**：池內常駐的 provider，做成 MyLinuxPool 的一個 shared-config 單元（systemd user unit），只綁 loopback 與 worker 容器看得到的那個介面。
- **資料**：同一個 SQLite 檔，表有 `events`（只追加，單調序號）、`leases`（由 events 推出的現況）、`cooldowns`、`clients`（客戶端憑證的雜湊）。
- **重啟**：從 `events` 重建現況。
- **為什麼**：
  - 流量很小（一個 agent 一小時幾次），不需要框架；
  - 少一個供應鏈風險；
  - 期 1 設計說要有常駐主機才換掉 GitHub repo 後端，現在本人已同意放在常駐 provider。
- **放棄的選項**：
  - Go 單一執行檔：sidecar 與 MyAiStorage 都是 Python，共用模型與測試慣例比較省事。
  - FastAPI 之類的框架：多一串依賴。
  - 期 1 的「private GitHub repo 後端」：冷卻與同時租用上限都要即時判斷。

### 決定 4：客戶端身分

- 每個客戶端一把隨機的客戶端憑證，服務只存雜湊，可以單獨撤銷。客戶端是：每台 worker 的 sidecar、Mac、之後的手機。
- 由 PM 在 provider 上用 `lgw admin client add <名稱>` 發，值只印一次。
- 客戶端憑證放的地方：
  - worker：`/run/mlp-secrets/llmgateway-client-token`（0400），由 MyLinuxPool 的能力放進去，和 A2A token 同一種做法；
  - Mac：`~/.config/lgw/client-token`（0600）。
- 租約的 `worker` 欄位必須和客戶端憑證登記的名稱一致，服務端檢查，所以一個客戶端不能冒用別台機器的名字。
- **為什麼**：池內只能經 SSH 進來，SSH 已經擋掉外人；客戶端憑證是為了區分身分、撤銷、記錄，不靠它擋外人。

### 決定 5：怎麼連到服務

- **Mac**：沿用 `mlp` 的 SSH 跳板做本機埠轉送（`ssh -L`）。`lgw` 的 base URL 指向本機那個埠。
- **worker**：容器要連得到 provider 上的服務。做法由 MyLinuxPool 決定，例如 provider 上的容器經 docker 的 host gateway 連，其他 provider 上的容器經隧道連。這要開 MyLinuxPool 的單。
- **手機**：之後由 MyAiEntry 的 App 經同一條 SSH 跳板做埠轉送（本人選 (2)）。
- 服務本身從不對外。

### 決定 6：租用 API

- JSON over HTTP，路徑前綴 `/v1`，用 `Authorization: Bearer <客戶端憑證>` 認身分。
- 時刻一律用 RFC 3339 的 UTC 字串。
- 每個會改變狀態的請求都帶客戶端產生的 `request_id`（去重見決定 12）。
- 每個回覆都帶 `api_version`（v0 是 `1`）。

**租約物件**（`lease`）：`{lease_id, subscription, agent, model, env, unset, expires_at, confirm_by}`。`env` 與 `unset` 只出現在核准的那一次回覆，之後查詢都不帶。

| 方法 | 路徑 | 請求 | 成功回傳 |
|---|---|---|---|
| POST | `/v1/leases` | `worker, role, agent, instance, model?, subscription?, purpose?, preferences?, request_id` | `201 {lease}` |
| POST | `/v1/leases/{id}/start` | `request_id` | `200 {expires_at}`：確認 agent 已經開起來，granted → active；對已 active 的租約重送也回 200 |
| POST | `/v1/leases/{id}/exhausted` | `signal, resets_at?, request_id` | `200 {next: lease}`，或 `200 {next: null, refusal}` |
| POST | `/v1/leases/{id}/renew` | `request_id` | `200 {expires_at}` |
| POST | `/v1/leases/{id}/usage` | `snapshot` | `204` |
| POST | `/v1/leases/{id}/release` | `reason?, request_id` | `204`（重複 release 也回 204） |
| GET | `/v1/board` | — | `200 {subscriptions: [...]}` |

**欄位說明**
- `agent`：`claude-code` 或 `opencode`。sidecar 內部的 `claude`、`opencode` 由它自己對應。
- `instance`（必填）：客戶端給每個 agent 的 id，例如 sidecar registry 裡的 agent id。同一個客戶端的同一個 `instance` 只能有一張未結束的租約。
- `model`：可空；空的就是該訂閱在政策檔裡的預設模型。
- `signal`（列舉）：`claude_limit`、`zen_free_limit`、`zen_go_limit`、`credit_limit`、`openrouter_limit`、`ollama_limit`、`other`。

**錯誤**
- `400 bad_request`：欄位缺漏或格式不合，附缺的欄位。
- `401`：沒有或錯誤的客戶端憑證。
- `403 client_mismatch`：worker 與客戶端憑證登記的名稱不符。
- `403 not_owner`：動到別的客戶端的租約。
- `404 not_found`：租約不存在。
- `409 lease_not_active`：對已結束的租約做 start、renew、exhausted。
- `409 lease_not_confirmed`：對還沒 start 的租約做 renew、exhausted；還沒 start 的租約只接受 start 與 release。
- `409 {reason, earliest_reset?}`（**拒絕**）：
  - `not_allowed`：政策不允許這個組合，或模型不在允許清單；
  - `instance_busy`：同一個 `instance` 已有未結束的租約；
  - `cap_reached`：可選的訂閱裡至少有一個不在冷卻，但這些都已達上限；
  - `all_cooling`：可選的訂閱全在冷卻，附最早的冷卻結束時刻；
  - `below_floor`：保留給「之後」的底線模型，v0 不會出現。
  - `earliest_reset` 只在 `all_cooling` 時附上。
  - 判定順序：`400` → `401` → `403`（`client_mismatch`、`not_owner`）→ `404` → `409`。`409` 的拒絕之間：先看 `not_allowed`，再看 `instance_busy`，最後才看可用性（`cap_reached` 或 `all_cooling`）。

**有效期限**
- 租約效期預設 12 小時，從 `start` 確認時起算；客戶端每小時 renew。
- granted 之後 10 分鐘內沒有 `start`，租約記為 expired（理由：未確認）。這樣「換把時已發出新租約、agent 卻沒開起來」的租約不會占著名額 12 小時。

### 決定 7：env 的內容

| 訂閱 | `agent: claude-code` | `agent: opencode` |
|---|---|---|
| `claude-personal`／`claude-company` | `env: {CLAUDE_CODE_OAUTH_TOKEN}`；`unset: [ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL]` | 拒絕 `not_allowed`（條款） |
| `openrouter` | `env: {ANTHROPIC_BASE_URL=https://openrouter.ai/api, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY=""}`，加上模型覆寫變數；`unset: [CLAUDE_CODE_OAUTH_TOKEN]` | `env: {OPENROUTER_API_KEY}` |
| `ollama-cloud` | `env: {ANTHROPIC_BASE_URL=<Ollama 的 Anthropic 相容位址>, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY=""}`；`unset: [CLAUDE_CODE_OAUTH_TOKEN]` | `env: {OLLAMA_API_KEY}` |
| `opencode-zen` | 拒絕 `not_allowed`（沒有官方接法） | `env: {OPENCODE_API_KEY}` |

- opencode 用的 provider 設定（模型清單、base URL）放在 repo 的 `client/opencode.json`，由模組帶 `OPENCODE_CONFIG` 指給 opencode；key 用 `{env:...}` 讀，設定檔不含值。各變數名以實作時的實打結果定案。
- 回傳的值只在記憶體裡交給 agent，`lgw` 與 sidecar 都不寫檔、不寫 log。

### 決定 8：選把、冷卻、同時租用上限

- **政策檔** `policy/default.json`（repo 內，不含秘密）：
  - 每種 agent 的訂閱順序：`claude-code: [claude-personal, claude-company]`、`opencode: [opencode-zen, openrouter, ollama-cloud]`。v0 的 `claude-code` 順序**不含** `openrouter`、`ollama-cloud`，有測試鎖住。
  - 每個訂閱允許的模型與預設模型：`openrouter` 只允許名稱以 `:free` 結尾的模型。
  - 每個訂閱的同時租用上限：Claude 兩把各 2，其他不設。
  - 每個訂閱的預設冷卻時間（見下）。
- **選把**：申請沒有指定訂閱時，照順序取第一個「沒有冷卻、未達上限、政策允許」的；都不行就拒絕（理由與判定順序見決定 6）。指定了訂閱的，只看那一把。
- **冷卻**：
  - 冷卻到 `resets_at` 再加 2 分鐘緩衝。
  - 沒有 `resets_at` 時用政策檔的預設，寧可等久一點，也不在冷卻中硬撞：
    - Claude 5 小時：官方 status line 的 `five_hour` 窗口，<https://code.claude.com/docs/en/statusline>；
    - Ollama 5 小時：官方 session 用量窗口，<https://ollama.com/blog/launch>；
    - Zen 依 `retry-after`，上限 24 小時，沒有就 1 小時：<https://github.com/anomalyco/opencode/pull/49195>；
    - OpenRouter 1 小時。
  - `resets_at` 若在過去，或在 8 天以後（看起來不合理），就忽略它、改用預設。
  - 冷卻中的訂閱一律不出租、不試探。冷卻只影響新的出租：冷卻中訂閱上已經 active 的其他租約照常可以 renew（它們可能還在正常使用另一個 session）。
- **只有額度用完才觸發**：客戶端只在判讀為「額度用完」時呼叫 `exhausted`（規則見決定 9）。短暫的 402／429 由 agent 自己重試。
- **同一個 agent 不同時拿兩把**：靠 `instance`（決定 6）擋住直接申請兩次；`exhausted` 先結束原租約，再發新的。
- **擴充點**：`preferences: {preferred_models, floor_model}` 存進 granted 事件，v0 的選把邏輯不看它。之後選把改成「在底線之上照優先順序選，沒有就拒絕 `below_floor`」，介面不變。「平均分攤」留在政策檔的選項裡（期 1 的設計），v0 的初始政策不用它。

### 決定 9：換把程序只有一份，放在 sidecar 的 agent 宿主；`lgw` 只給 API 客戶端與訊號規則

- **本 repo 的 `lgw`**：Python 套件，只用標準函式庫，不綁 herdr、agora。內容：
  1. **API 客戶端**：base URL 與客戶端憑證路徑可設定；連不上時回報「無法確認」，不丟例外讓呼叫方當掉。
  2. **訊號規則**：輸入 agent 的畫面文字，輸出「額度用完（含 `signal` 與重置時刻或無）」「暫時錯誤」或「正常」。規則照 `quota-signals.md`：
     - Claude Code：`You've hit your (session|weekly|Opus|Sonnet) limit · resets …` → `claude_limit`；
     - opencode 對 Zen：`FreeUsageLimitError`／`Free usage exceeded` → `zen_free_limit`；`GoUsageLimitError`／`usage limit reached. It will reset in …` → `zen_go_limit`；`CreditLimitExceeded` → `credit_limit`。訊息內容的標記優先於狀態碼。
     - OpenRouter：402 且 `limit_source` 不是 in-flight 預算 → `openrouter_limit`。in-flight 預算的 402 是暫時錯誤。
     - Ollama Cloud：429 且訊息含 `session usage limit` 或 `weekly usage limit` → `ollama_limit`。
     - Claude Code 的 spend limit 類訊息（月額度、額外用量上限，不會隨 5 小時窗口重置）→ `claude_limit`、不帶重置時刻，於是用政策檔的預設冷卻；寧可冷卻久一點也不空轉重試。
     - **沒有額度標記的 402／429 一律判為「暫時錯誤」**，不換把。

     規則放資料表，正反兩面都用 `quota-signals.md` 的樣本測試。
  3. **opencode 設定**：`client/opencode.json`（模型清單、base URL，key 用 `{env:...}` 讀）隨 `lgw` 一起打包，sidecar 用 `lgw` 提供的路徑把它指給 opencode（`OPENCODE_CONFIG`）。
  4. **管理命令**：`lgw admin client add|revoke|list`，在 provider 上用。
- **換把程序在 MyAiStorage 的 sidecar 裡（agent 宿主）**。以下是轉述，規格以 MyAiStorage#31 為準，那邊改了以那邊為準：
  - 開 agent：`herdr tab create --env` 帶 env；unset 用 `env -u NAME`；Claude Code 加 `--settings` 關掉 `autoContinueAtUsageLimit`。
  - 判讀：用 `herdr agent read` 讀畫面，交給 `lgw` 的規則判讀。
  - 安全結束：`herdr agent send-keys <name> ctrl+c` 讓 agent 收尾，由 agora continue 做最後寫回；5 秒後還在就對 process group 送 SIGTERM，再 5 秒送 SIGKILL，最後才 `pane close`。
  - 打斷一輪而不結束 agent 時用 Esc（Claude Code 1 次、opencode 2 次）。
  - 開起來之後呼叫 `start` 確認租約。
  - 接回：`agora continue session <Session> --agent <agent> --sync-period <分鐘>`，不用 `--fork`；session id 的寫法照 sidecar 既有的慣例。
  - 這些都是 sidecar 已有、已審過的生命週期程式（MyAiStorage#31 的 D4 與 10-11 的 spike）。
- **誰呼叫**：
  - worker：sidecar 常駐，依賴 `lgw`（從本 repo 的固定 ref 裝進 sidecar 的 venv）。
  - Mac：`sidecar agent run claude|opencode`（sidecar 與 agora 是同一個 Python distribution，Mac 已經裝了 agora），在前景跑同一段程序。
  - 兩邊只差「常駐或前景」，換把程序完全同一份，沒有 Mac 專用的分支（本人要求）。
- **為什麼**（和 aistorage-pm 2026-10-11 談定）：
  - 開關 agent、三段終止、重啟後找回、agora continue 這段 sidecar 已經有了；LLMGateway 再寫一份就會變成兩份。
  - `lgw` 不綁 agent 工具，agent 工具換了，服務與 `lgw` 都不必改。
- **放棄的選項**：
  - 換把程序放在 `lgw`（PR #9 先前的寫法）：sidecar 要把已審過的那段改成呼叫 `lgw`，等於重寫一份。
  - 各自實作：會有兩份程序與兩份判讀規則。
- **依賴**：v0 在 Mac 上的驗收要等 sidecar 提供 `sidecar agent run`。

### 決定 10：通知

- 每次冷卻、換把、兩把都冷卻而停下，都寫進事件與狀態板。
- Angareion 可用之後，由 sidecar 以 team 的身分通知 PMO（sidecar 本來就有 Angareion）。
- Mac 上由 `sidecar agent run` 在終端機印出（Mac 沒有常駐的 sidecar，也沒有 A2A token）。
- 服務端不直接連 Angareion，所以服務不需要 A2A token。

### 決定 11：狀態板

- `GET /v1/board`（要客戶端憑證）對每把回傳：
  - 使用中的租約（worker、角色、agent、開始時刻）；
  - 數量／上限；
  - 是否冷卻與冷卻到何時；
  - 部署狀態：key 檔裡沒有這把就是「未部署」；
  - 用量：
    - OpenRouter：服務每 5 分鐘查一次 `GET /api/v1/key`；
    - Ollama：每 5 分鐘查一次 `GET /api/balance`（官方上限每分鐘 10 次）；
    - 兩個查詢都有 10 秒逾時、在獨立的執行緒，失敗不影響租用；上游回 401 時顯示「key 無效」；查詢失敗時顯示「未知」與最後一次成功的時刻；
    - Claude 與 Zen 顯示「未知」。
- **HTML 版**（`/board`）只綁在 provider 的 **loopback**（不開在 worker 看得到的介面），不要客戶端憑證、唯讀。Mac 經 SSH 埠轉送後用瀏覽器開。客戶端憑證永遠不放進 URL。
- 從不顯示 key 的值。狀態板含 worker 名稱（Mac 是主機名），只在池內、經 SSH 通道看得到。
- 期 1 `usage-record` 的 token 回報（OTel、opencode export）不在 v0，`report_usage` 介面先留著。

### 決定 12：一致性、邊界與錯誤處理

- **交易**：每個會改變狀態的 API 動作是一筆 SQLite 交易（`BEGIN IMMEDIATE`）。例如 `exhausted` 的「設冷卻＋原租約 superseded＋發下一張」要嘛全成、要嘛全不成，服務中途當掉也不會留下一半。搶最後一個上限名額也在同一筆交易裡判定。
- **去重**：以（客戶端，`request_id`）為鍵，把第一次的回覆存進資料庫保留 7 天；重送時回同一份回覆，跨重啟也有效。同一張租約用不同的 `request_id` 再回報 `exhausted`，回 `409 lease_not_active`，不會發出第二張。
- **時鐘**：服務的「現在」經可替換的 clock 介面取得（測試用假時鐘）。冷卻在 `now < 結束時刻` 時算冷卻中，等於就算結束。
- **逾期結算**：任何一次經過認證的請求（包含讀狀態板）都先補記已過期的租約，再處理該請求。
- **key 檔重讀**：`deploy-keys` 以「寫暫存檔再 rename」原子替換。SIGHUP 重讀失敗時（檔案損壞、格式錯）沿用舊的 key，並在狀態板與 log 標示重讀失敗。值是空字串視同沒部署。已經發出的租約不受重讀影響。
- **撤銷客戶端**：撤銷時，該客戶端所有未結束的租約一起記為 revoked。
- **拒絕的紀錄**：經過認證的申請被拒時，記一筆 `denied` 事件（不含任何值），供 PMO 追溯。`400`、`401` 不算租約，只記在服務 log，而且不記請求內容。
- **輸入限制**：`worker` ≤ 64 字、`role` ≤ 128 字、`purpose` ≤ 256 字，只收可列印字元。HTML 輸出一律跳脫。
- **事件的保存**：`events` 只追加；超過 180 天的事件每月匯出成壓縮檔、再從資料庫刪除。匯出檔同樣不含值。
- **政策檔**：啟動與 SIGHUP 時驗證；不合法就拒絕套用、沿用舊的（啟動時則不啟動）。
- **版本**：`lgw` 用 git tag（`lgw-vX.Y.Z`）發版，sidecar 固定在某個 tag。API 不相容的改變要升 `api_version`，並事先通知 aistorage-pm。`lgw` 遇到不認得的拒絕理由，一律當一般拒絕處理。

## Risks / Trade-offs

- [公司帳號用完換另一帳號是灰色地帶] → 本人已接受。設計上做到：冷卻中不出租不試探；Claude 兩把各最多 2 個同時；只有額度用完才換；事件全留。
- [key 集中在一台 provider] → 只綁內部介面、只經 SSH 進入；檔案 0600；事件、log、狀態板都不含值；輪替只要改 secret 並重跑 workflow。
- [訊號判讀靠畫面文字，agent 改版會失效] → 規則是資料表，有單元測試，用 `quota-signals.md` 的樣本與實際碰到的輸出當測試資料；判讀不出時就不換，只記錄。
- [`herdr tab create --env` 的行為在 worker 的 herdr 0.9.3 上還沒驗] → sidecar 驗收時再驗，不過就改用 0600 暫存 env 檔、讀完即刪。
- [換把會中斷正在跑的那一輪] → 本人已知；靠 `agora continue` 接回。
- [服務掛掉] → systemd 拉起並從 events 重建。客戶端拿不到租約時不開新 agent；已經在跑的 agent 不受影響，因為 env 已經給了。
- [GitHub Actions 能登入 provider] → 這把身分只用在 `deploy-keys`，workflow 只能手動觸發；身分可以在 MyLinuxPool 單獨撤銷。

## Migration Plan

1. 服務、政策檔、`lgw`（API 客戶端、訊號規則、管理命令）在本 repo 寫好，含測試（假 key、假上游、假時鐘）。
2. MyLinuxPool：
   - 服務的單元；
   - Actions 的 client 身分；
   - worker 怎麼連到服務；
   - 客戶端憑證放進 `/run/mlp-secrets/`。
   這四件另開 MyLinuxPool 的單。
3. 本人設五把 secret，跑 `deploy-keys`。
4. Mac：`sidecar agent run claude`、`sidecar agent run opencode` 驗收，包含用假訊號換把（等 sidecar 提供這個命令）。
5. sidecar：aistorage-pm 把假 broker 換成 `lgw` 的 API 客戶端，在一台 worker 驗收。
6. 另開單把 MyLinuxPool#50 的 key 來源換成租用，收掉 MyLinuxPool 裡的 key。
- **退回**：停服務、Mac 改回原本的指令；worker 在第 6 步之前仍有 MyLinuxPool#50 的 key，可以照舊。

## Open Questions

- opencode 讀各上游 key 的確切變數名，以及 Ollama 的 Anthropic 相容位址：照官方文件，實作時實打確認。不影響形狀。
