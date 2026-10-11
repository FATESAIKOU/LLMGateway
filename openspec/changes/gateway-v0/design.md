## Context

動機與範圍見 proposal.md（第 3 版，本人已確認）。這裡只列影響做法的現況與限制：

- **Claude 條款**：訂閱的 OAuth 是給原版 Claude Code 這類 Anthropic 原生 app 的一般使用；第三方不得代為路由、收集、存放、中轉 Claude.ai 的 token（`docs/research/claude-failover.md` §7）。所以 LLMGateway 不能代轉 Claude 的請求，只能把 `CLAUDE_CODE_OAUTH_TOKEN` 交給本人自己的原版 Claude Code。
- **期 1 設計**（已封存為 `openspec/specs/agent-lease`、`openspec/specs/usage-record`）：控制面、租約、只追加事件、launcher 注入、切換留紀錄。v0 延伸它；差別見決定 1、2、3。
- **池**：池內有常駐的 provider，Mac 已經有經 SSH 跳板連入池內的身分。worker 是跑在 provider 上的容器。對外只有跳板機的 SSH 埠，其餘服務都不對外。
- **sidecar**（MyAiStorage#31；本人 2026-10-11 晚的要求，設計見 MyAiStorage 的 `docs/proposals/aicompany-design-v2.md`）：一台機器一個 daemon（Mac 也能跑），對外只有開啟、暫停、關閉、搜集狀態、戳五個操作加上 HTTP 控制；不基於 herdr，agent 是它開的子行程；內部用 `AgentDriver`、`SessionStore`、`CredentialSource` 三個介面，換把在內部完成。
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

### 決定 2：key 的保管與部署、服務的部署

- **來源**：本 repo 的 GitHub secret，五把各一個，名稱照本人的命名規則（tasks 3.3），值由本人設。
- **部署的路**：本 repo 的 workflow 經池的 SSH 跳板，以服務帳號 `llmgw` 登入放服務的那台。
  - 跳板機上：部署金鑰只能轉發到那台的隧道埠（`restrict,port-forwarding,permitopen="127.0.0.1:<埠>"`），不能開 shell。這一行由 MyLinuxPool 管（MyLinuxPool#68）。
  - 那台上：`~llmgw/.ssh/authorized_keys` 用 `command="<…>/current/deploy/remote-deploy.sh",restrict` 強制執行部署腳本。這一行由 LLMGateway 管，池的 pool-sync 不會碰其他帳號。
  - 跳板與那台用同一把部署金鑰；私鑰放本 repo 的 secret，由本人設。
- **部署腳本的兩個動詞**（從 `SSH_ORIGINAL_COMMAND` 以白名單解析，其他一律拒絕）：
  - `keys`：從 stdin 讀 key 檔內容 → 驗證 → 寫暫存檔、0600、rename 成 `keys.env` → 通知服務重讀。由 workflow `deploy-keys` 使用。
  - `upgrade <tag>`：tag 必須符合 `lgw-vX.Y.Z` → 以固定 tag 取碼、切換 current、重啟服務。由 workflow `deploy-service` 使用。
- workflow 的 log 只出現 secret 名稱；值用 stdin 傳，不上命令列；ssh 的錯誤輸出會把位址遮掉。
- **為什麼**：本人已同意把來源放在這裡；GitHub secret 加上 Actions 是本人既有的做法。服務帳號自己管部署的那一行，就不必依賴 MyLinuxPool 的 authkeys 組裝。
- **放棄的選項**：
  - 繼續放 MyLinuxPool 的 `POOL_WORKER__*`：本人裁定那只是過渡。
  - 本人手動把檔案放到那台：不能重現，輪替也容易漏。
  - 用池的管理帳號跑服務：本人裁定 LLMGateway 跟 MyLinuxPool 無關；而且管理帳號的 authorized_keys 由池整份取代。

### 決定 3：服務的形狀

- **程式**：Python 標準函式庫，HTTP 用 `http.server.ThreadingHTTPServer`、資料用 `sqlite3`，沒有第三方套件。
- **位置**：本人選定的常駐 provider（機器名稱不寫在 public repo）。本人的裁定是：「這東西跟 mlp 無關，說到底就是那台跑了兩個服務。」所以 LLMGateway 是**獨立的服務**，不是 MyLinuxPool 的單元：
  - 在那台用**自己的系統帳號 `llmgw`** 跑，是 systemd user unit（帳號開了 linger）。`llmgw` **不加進 docker 群組**，因為 docker 群組等同 root，加了就打破隔離。
  - 安裝程式、unit、run wrapper、版本、`--check` 都在本 repo 的 `deploy/`。版本 tag 只在部署時指定一次（`upgrade <tag>`）。
  - 只綁 loopback，以及同一台容器看得到的 docker bridge 位址；管理 API 與 HTML 狀態板只綁 loopback。
  - `--check` 三態：0＝服務在跑、版本對、`GET /v1/board` 不帶憑證時回 401；1＝確定不成立；2＝無法確認。
- **一次性的開機設定**（要 root，由本人做或本人授權）：
  - 建 `llmgw` 帳號、`loginctl enable-linger llmgw`；
  - 確認 sshd 沒有擋 `llmgw`；
  - 放部署公鑰；
  - 第一次安裝；
  - 發「發證用」的管理憑證（決定 4），由本人直接設進 MyLinuxPool 的 secret。
- **資料**：同一個 SQLite 檔，表有 `events`（只追加，單調序號）、`leases`、`cooldowns`、`clients`（憑證雜湊與角色）、去重表。
- **重啟**：從 `events` 重建現況。
- **為什麼**：
  - 流量很小（一個 agent 一小時幾次），不需要框架；
  - 少一個供應鏈風險；
  - 獨立帳號讓 key 只有服務讀得到，和池的管理帳號隔開；那台的 auditd 也會稽核這個帳號。
- **放棄的選項**：
  - Go 單一執行檔：sidecar 與 MyAiStorage 都是 Python，共用模型與測試慣例比較省事。
  - FastAPI 之類的框架：多一串依賴。
  - 做成 MyLinuxPool 的 shared-config 單元（MyLinuxPool#67，已作廢）：本人裁定不是池的一部分。

### 決定 4：客戶端身分

- 每個客戶端一把隨機的客戶端憑證（32 bytes），服務只存 SHA-256，可以單獨撤銷。客戶端有兩種角色：
  - `client`：worker 的 sidecar、Mac、之後的手機。只能用租用 API。
  - `issuer`：發證用的管理憑證。只能用管理 API，不能租用。
- **管理 API**（只綁 loopback）：
  - `POST /v1/admin/clients {name}`：回 `201 {name, token}`；
  - `DELETE /v1/admin/clients/{name}`：回 204；不存在的名字回 404。
  - issuer 只能發撤名字符合 `worker-[A-Za-z0-9._-]{1,57}` 的客戶端。
  - 同名重複發證時，先撤掉舊的（連同它未結束的租約）再回新的，不會同時有兩把有效的。
  - 從非 loopback 介面來的管理請求一律 403。
- **worker 的客戶端憑證由 MyLinuxPool 自動發撤**（MyLinuxPool#70）：
  - create-worker 經 pool-ssh 的 stdin 通道，在那台以 curl 打 127.0.0.1 的管理 API。發證憑證從 stdin 讀、不上 argv；回來的 token 直接寫進 worker 的掛載目錄，不經 log 與 output。
  - delete-worker 撤證，失敗時明確報錯。
  - 發證憑證存成 MyLinuxPool 的 secret `GH_MYLINUXPOOL__LLMGATEWAY_ADMIN__ISSUE`。
- **其他客戶端**（Mac、手機）由 PM 在那台以 `llmgw` 帳號用 `lgw admin client add <名稱>` 發，值只印一次；`lgw admin client rm <名稱>` 撤銷。
- **客戶端憑證放的地方**：
  - worker：`/run/mlp-secrets/llmgateway-client-token`（0400），由 MyLinuxPool 放進去；
  - Mac：`~/.config/lgw/client-token`（0600）。
- 租約的 `worker` 欄位必須和客戶端憑證登記的名稱一致，服務端檢查，所以一個客戶端不能冒用別台機器的名字。
  - 池的 worker 名是 `<provider>-<image>-<run_id>` 這種形式；MyLinuxPool 發證時用 `worker-<池的 worker 名>` 當客戶端名（上限 57 字元，超過的 worker 在建容器前就被拒絕）。
  - sidecar 啟動時先呼叫 `GET /v1/whoami`，用回來的 `client` 當租約的 `worker` 欄位，不自己拼名字。
- `GET /v1/whoami`：回 `200 {client, role}`，不花任何額度，給 MyLinuxPool 的驗證程式核對憑證用。
- **為什麼**：池內只能經 SSH 進來，SSH 已經擋掉外人；客戶端憑證是為了區分身分、撤銷、記錄，不靠它擋外人。MyLinuxPool 透過 API 發證，就不用碰 LLMGateway 的資料庫。

### 決定 5：怎麼連到服務

- **Mac**：沿用 `mlp` 的 SSH 跳板做本機埠轉送（`ssh -L`）。`lgw` 的 base URL 指向本機那個埠。
- **worker**（MyLinuxPool#69）：sidecar 一律連 `http://127.0.0.1:<固定埠>`，不必管服務在哪一台。
  - 同一台 provider 上的容器：經 docker bridge 的位址連。
  - 其他 provider 上的容器：放服務的那台在既有隧道上多掛一條 `-R`，把服務放到跳板機本機的固定埠；worker 在既有隧道上多掛一條 `-L`，接到自己本機的固定埠。
  - 全程只綁 loopback，不改 sshd；埠宣告在 MyLinuxPool 的設定裡。
- **手機**：之後由 MyAiEntry 的 App 經同一條 SSH 跳板做埠轉送（本人選 (2)）。
- 服務本身從不對外。

### 決定 6：租用 API

- JSON over HTTP，路徑前綴 `/v1`，用 `Authorization: Bearer <客戶端憑證>` 認身分。
- 時刻一律用 RFC 3339 的 UTC 字串。
- 每個會改變狀態的請求都帶客戶端產生的 `request_id`（去重見決定 12）。
- 每個回覆都帶 `api_version`（v0 是 `1`）。

**租約物件**（`lease`）：`{lease_id, subscription, agent, model, env, unset, expires_at, confirm_by}`。`env` 與 `unset` 只出現在核准的那一次回覆，之後查詢都不帶。回覆一律把租約物件包在鍵裡：申請回 `{"lease": …}`，`exhausted` 回 `{"next": …}`。

| 方法 | 路徑 | 請求 | 成功回傳 |
|---|---|---|---|
| POST | `/v1/leases` | `worker, role, agent, instance, model?, subscription?, purpose?, preferences?, request_id` | `201 {"lease": <租約物件>}` |
| POST | `/v1/leases/{id}/start` | `request_id` | `200 {expires_at}`：確認 agent 已經開起來，granted → active；對已 active 的租約重送也回 200 |
| POST | `/v1/leases/{id}/exhausted` | `signal, resets_at?, request_id` | `200 {next: lease}`，或 `200 {next: null, refusal}` |
| POST | `/v1/leases/{id}/renew` | `request_id` | `200 {expires_at}` |
| POST | `/v1/leases/{id}/release` | `reason?, request_id` | `204`（重複 release 也回 204） |
| GET | `/v1/board` | — | `200 {subscriptions: [...]}` |
| GET | `/v1/whoami` | — | `200 {client, role}`（核對憑證用，不花額度） |
| POST | `/v1/admin/clients` | `name` | `201 {name, token}`（只有 issuer、只綁 loopback、只限 `worker-*`；同名就撤舊發新） |
| DELETE | `/v1/admin/clients/{name}` | — | `204`；不存在回 404 |

**欄位說明**
- `agent`：`claude-code` 或 `opencode`。sidecar 內部的 `claude`、`opencode` 由它自己對應。
- `instance`（必填）：客戶端給每個 agent 的 id，例如 sidecar registry 裡的 agent id。同一個客戶端的同一個 `instance` 只能有一張未結束的租約。
- `model`：可空；空的就是該訂閱在政策檔裡的預設模型。
- `signal`（列舉）：`claude_limit`、`zen_free_limit`、`zen_go_limit`、`credit_limit`、`openrouter_limit`、`ollama_limit`、`other`。

**錯誤**
- `400 bad_request`：欄位缺漏或格式不合，附缺的欄位。
- `401`：沒有或錯誤的客戶端憑證。
- `413 body_too_large`：請求 body 超過 1 MB，不讀內容。
- `403 client_mismatch`：worker 與客戶端憑證登記的名稱不符。
- `403 not_owner`：動到別的客戶端的租約。
- `403 forbidden_role`：client 打管理 API、issuer 打租用 API，或管理 API 不是從 loopback 來的。
- `403 name_not_allowed`：issuer 發撤不符合 `worker-*` 的名字。
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

### 決定 9：換把在 sidecar 內部，經 sidecar 定義的 `CredentialSource` 介面；`lgw` 提供實作 `LgwCredentialSource`

（和 aistorage-pm 2026-10-11 晚談定，對應他提案的 Q5；圖見 `diagrams.md`。）

- **sidecar**（MyAiStorage）內部定義 `CredentialSource`：
  - `acquire(agent 種類, 模型偏好) → env／unset`
  - `started()`
  - `renew()`
  - `exhausted(訊號, 重置時刻) → 下一組 env 或拒絕`
  - `release()`

  sidecar 本體只認這個介面，**不知道 LLMGateway**。
- **`lgw` 提供 `LgwCredentialSource`**，把這五個動作對到租用 API：
  - `acquire` → `POST /v1/leases`
  - `started` → `start`
  - `renew` → `renew`
  - `exhausted` → `exhausted`
  - `release` → `release`

  `lgw` 只用標準函式庫，不綁 herdr、agora、任何 agent 工具。
- **額度用完由 agent driver 判讀**（sidecar 的 `AgentDriver`：Claude Code、opencode 各一個實作），在狀態裡回正規化的「額度用完＋重置時刻」。driver 建議用 agent 的程式化模式（Claude Code stream-json、opencode server），錯誤是結構化的。`lgw.signals` 的規則表（只有帶額度標記的 402／429 才算用完）可以給 driver 當函式庫用，判讀的責任在 driver。
- **換把的步驟**（sidecar 內部，不是新的對外介面）：driver 回報額度用完 → `CredentialSource.exhausted` 拿下一組 → `stop`（溫和，寫回 agora）→ `SessionStore.prepare`（continue 同一個 Session）→ `launch`（新的 env）→ `started`。拿到拒絕時停下，狀態記成「等額度」，`status` 看得到。
- **env 的交付**：sidecar 直接開子行程，env 用子行程的環境給，不上命令列；`unset` 列出的變數從子行程環境去掉。由 sidecar 開的 Claude Code 關掉 `autoContinueAtUsageLimit`（以設定或旗標）。
- **Mac 與 worker 走同一套**：Mac 也跑同一個 sidecar daemon，用它的 `open` 開 agent。沒有 Mac 專用的分支，也沒有另一個換把程式。
- **為什麼**：
  - sidecar 對外仍是五個操作，LLMGateway 仍是租用介面；「怎麼拿 key、怎麼換」只在 `LgwCredentialSource` 這個實作裡。
  - 不多一個常駐行程。
  - 定時開啟的 job 執行時也拿得到憑證。
- **放棄的選項**：
  - 換把器做成 sidecar 外面的第三個行程（我先前的提案）：多一個常駐行程，而且 sidecar 的定時 job 拿不到憑證。
  - 換把程序放在 `lgw` 並綁 herdr（PR #9 更早的寫法）：本人要求不基於 herdr。

### 決定 10：通知

- 每次冷卻、換把、兩把都冷卻而停下，都寫進事件與狀態板。
- Angareion 可用之後，由 sidecar 以 team 的身分通知 PMO（sidecar 本來就有 Angareion）。
- Mac 上同樣由 sidecar 記錄並在 `status` 顯示。
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
- 期 1 `usage-record` 的 token 回報（OTel、opencode export）不在 v0；回報用量的端點也拿掉（沒有人用，本人要求「必要而且只有這些」），之後真的需要再加。

### 決定 12：一致性、邊界與錯誤處理

- **交易**：每個會改變狀態的 API 動作是一筆 SQLite 交易（`BEGIN IMMEDIATE`）。例如 `exhausted` 的「設冷卻＋原租約 superseded＋發下一張」要嘛全成、要嘛全不成，服務中途當掉也不會留下一半。搶最後一個上限名額也在同一筆交易裡判定。
- **去重**：以（客戶端，`request_id`）為鍵，把第一次的回覆存進資料庫保留 7 天；重送時回同一份回覆，跨重啟也有效。
  - **存進去的回覆不含 `env` 的值**，只留變數名。重送時若回覆含租約，依那張租約的訂閱與目前的 key 檔重新組出 `env`；那張租約已結束就回 `409 lease_not_active`。
  - 這樣資料庫的任何一張表都沒有 key 的值，key 只存在 key 檔（2026-10-11 實作冒煙測試時發現：原本的寫法會把值存進去重表）。
  - 重送不保證位元組一致：重組的 `env` 依當下的 key 檔（輪替後拿到新值）；租約已結束 → `409 lease_not_active`，客戶端要重新申請；key 已撤下 → `409 not_allowed`；原本是拒絕的就照原樣回。重送不是新申請，不記 `denied` 事件。
  - **順序**：認證 → 延後結算 → 去重 → 業務邏輯。撤銷後拿舊 `request_id` 重送一律回 401。
  - 請求 body 超過 1 MB 回 413，不讀內容。同一張租約用不同的 `request_id` 再回報 `exhausted`，回 `409 lease_not_active`，不會發出第二張。
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
- [sidecar 改成直接開子行程，agent 的程式化模式還沒 spike] → 由 aistorage-pm 先 spike（他提案的 Q3）；env 一律走子行程環境、不上命令列。
- [換把會中斷正在跑的那一輪] → 本人已知；靠 `agora continue` 接回。
- [服務掛掉] → systemd 拉起並從 events 重建。客戶端拿不到租約時不開新 agent；已經在跑的 agent 不受影響，因為 env 已經給了。
- [GitHub Actions 能登入放服務的那台] → 部署金鑰在跳板機上只能轉發、在那台只能跑部署腳本的兩個動詞；workflow 只能手動觸發，而且要本人在 environment 核准；跳板那一行可在 MyLinuxPool 單獨撤銷，那台那一行由 llmgw 帳號撤銷。
- [發證用的管理憑證外洩] → 只能發撤 `worker-*`、只從 loopback 收；可以用 `lgw admin client rm` 撤銷後重發。

## Migration Plan

1. 服務、政策檔、`lgw`（API 客戶端、訊號規則、管理命令）在本 repo 寫好，含測試（假 key、假上游、假時鐘）。
2. 本人做一次性的開機設定（決定 3）：建 `llmgw`、linger、放部署公鑰、第一次安裝、發發證用的管理憑證。
3. MyLinuxPool：
   - MyLinuxPool#68：跳板機上的轉發專用條目與 host key 指紋；
   - MyLinuxPool#69：worker 怎麼連到服務；
   - MyLinuxPool#70：create-／delete-worker 透過管理 API 發撤 worker 的客戶端憑證。
4. 本人設 environment 與 secret，跑 `deploy-service`（`upgrade <tag>`）與 `deploy-keys`。
5. Mac：用 sidecar 的 `open` 開 Claude Code 與 opencode 驗收，包含用假訊號換把（等 sidecar v2 與 `LgwCredentialSource`）。
6. worker：sidecar 用 `LgwCredentialSource`，在一台 worker 驗收。
7. 另開單把 MyLinuxPool#50 的 key 來源換成租用，收掉 MyLinuxPool 裡的 key。
- **退回**：停服務、Mac 改回原本的指令；worker 在第 7 步之前仍有 MyLinuxPool#50 的 key，可以照舊。

## Open Questions

- opencode 讀各上游 key 的確切變數名，以及 Ollama 的 Anthropic 相容位址：照官方文件，實作時實打確認。不影響形狀。
