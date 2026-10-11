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

JSON over HTTP，路徑前綴 `/v1`，用 `Authorization: Bearer <客戶端憑證>` 認身分。每個會改變狀態的請求都帶客戶端產生的 `request_id`，用來去重。

| 方法 | 路徑 | 請求 | 成功回傳 |
|---|---|---|---|
| POST | `/v1/leases` | `worker, role, agent, model?, subscription?, purpose?, preferences?, request_id` | `201 {lease_id, subscription, env, unset, expires_at}` |
| POST | `/v1/leases/{id}/exhausted` | `signal, resets_at?, request_id` | `200 {next: <同上> }`，或 `200 {next: null, refusal: {...}}` |
| POST | `/v1/leases/{id}/renew` | `request_id` | `200 {expires_at}` |
| POST | `/v1/leases/{id}/usage` | `snapshot` | `204` |
| POST | `/v1/leases/{id}/release` | `reason?, request_id` | `204` |
| GET | `/v1/board` | — | `200 {subscriptions: [...]}` |
| GET | `/board` | — | 唯讀 HTML |

- **拒絕**：`409 {reason, earliest_reset?}`。`reason` 是以下其中之一：
  - `all_cooling`：可選的都在冷卻；
  - `cap_reached`：可選的都達同時租用上限；
  - `not_allowed`：政策不允許；
  - `below_floor`：保留給「之後」的底線模型；
  - `client_mismatch`：worker 與客戶端憑證不符。
- **逾期與續期**：租約效期預設 12 小時，客戶端每小時 renew。過期的租約在下一次任何往來時補記為 expired（期 1 的延後結算，服務常駐後也照用，邏輯比較單純）。
- **`agent`**：值為 `claude-code` 或 `opencode`。sidecar 內部的 `claude`、`opencode` 由它自己對應。

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
  - 每種 agent 的訂閱順序：`claude-code: [claude-personal, claude-company]`、`opencode: [opencode-zen, openrouter, ollama-cloud]`；
  - 每個訂閱允許的模型：`openrouter` 只允許 `:free`；
  - 每個訂閱的同時租用上限：Claude 兩把各 2，其他不設；
  - 每個訂閱的預設冷卻時間。
- **選把**：申請沒有指定訂閱時，照順序取第一個「沒有冷卻、未達上限、政策允許」的；都不行就拒絕，`earliest_reset` 填最早的重置時間。指定了訂閱的，只看那一把。
- **冷卻**：
  - 冷卻到 `resets_at` 再加 2 分鐘緩衝。沒有 `resets_at` 時用政策檔的預設：Claude 5 小時、Ollama 5 小時、Zen 依 `retry-after`（沒有就 1 小時）、OpenRouter 1 小時。寧可等久一點，也不在冷卻中硬撞。
  - 冷卻中的訂閱一律不出租、不試探。
- **只有額度用完才觸發**：客戶端只在判讀為「額度用完」時呼叫 `exhausted`。短暫的 429 由 agent 自己重試。
- **同一個 agent 不同時拿兩把**：`exhausted` 會先結束原租約，再發新的。
- **擴充點**：`preferences: {preferred_models, floor_model}` 存進 granted 事件，v0 的選把邏輯不看它。之後選把改成「在底線之上照優先順序選」，介面不變。

### 決定 9：換把程序只有一份，放在 sidecar 的 agent 宿主；`lgw` 只給 API 客戶端與訊號規則

- **本 repo 的 `lgw`**：Python 套件，只用標準函式庫，不綁 herdr、agora。內容：
  1. **API 客戶端**：base URL 與客戶端憑證路徑可設定；連不上時回報「無法確認」，不丟例外讓呼叫方當掉。
  2. **訊號規則**：輸入 agent 的畫面文字，輸出「額度用完（含重置時刻或無）」「暫時錯誤」或「正常」。規則照 `quota-signals.md`：
     - Claude Code 的 `You've hit your … limit · resets …`；
     - opencode 的 `Free usage exceeded`、`usage limit reached` 與 402／429。

     規則放資料表，可以單獨更新與測試。
  3. **管理命令**：`lgw admin client add|revoke|list`，在 provider 上用。
- **換把程序在 MyAiStorage 的 sidecar 裡（agent 宿主）**：
  - 開 agent：`herdr tab create --env` 帶 env；unset 用 `env -u NAME`；Claude Code 加 `--settings` 關掉 `autoContinueAtUsageLimit`。
  - 判讀：用 `herdr agent read` 讀畫面，交給 `lgw` 的規則判讀。
  - 安全結束：`herdr agent send-keys <name> ctrl+c` 讓 agent 收尾，由 agora continue 做最後寫回；5 秒後還在就對 process group 送 SIGTERM，再 5 秒送 SIGKILL，最後才 `pane close`。
  - 打斷一輪而不結束 agent 時用 Esc（Claude Code 1 次、opencode 2 次）。
  - 接回：`agora continue session <Session> --agent <agent> --sync-period <分鐘>`，不用 `--fork`。
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

- `GET /v1/board` 對每把回傳：
  - 使用中的租約（worker、角色、agent、開始時刻）；
  - 數量／上限；
  - 是否冷卻與冷卻到何時；
  - 用量（有就給）：OpenRouter 由服務每 5 分鐘查一次 `GET /api/v1/key`；Ollama 每 5 分鐘查一次 `GET /api/balance`（上限每分鐘 10 次）；Claude 與 Zen 顯示「未知」。
- `/board` 是同一份資料的唯讀 HTML。
- 從不顯示 key 的值。
- 期 1 `usage-record` 的 token 回報（OTel、opencode export）不在 v0，`report_usage` 介面先留著。

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
- `agora continue` 接 agent 自己的 session id 的寫法（id 前綴）：實作時確認。
