# LLMGateway v3：實體、控制時序、介面（條列，供 PMO 畫圖）

> 依本人 2026-10-11 夜的方向：LLMGateway 只有 list、get、登錄冷卻三個介面；相依只有 sidecar→agora、sidecar→LLMGateway。sidecar 那一側見 MyAiStorage 的 `docs/proposals/aicompany-design-v3.md`，名詞一致。機器名不寫，「放服務的那台」是本人選定的常駐 provider。

## 1. 實體圖

**實體**
- GitHub（LLMGateway repo）：
  - secret：五把 key、各類機器的讀取 token、部署連線資訊；
  - workflow：`deploy-keys`、`deploy-service`（手動觸發、本人核准）。
- 池的跳板機（MyLinuxPool）：一行部署金鑰，只能轉發到那台的隧道埠。
- 放服務的那台：跑兩個服務。
  - 帳號 `llmgw`（LLMGateway）：
    - `remote-deploy.sh`（動詞 `keys`、`upgrade`）；
    - `lgwd`（list／get／登錄冷卻）；
    - `keys.env`（0600，key 與 token）；
    - SQLite（冷卻、稽核）。
  - 池的管理帳號（MyLinuxPool 的服務）：和 LLMGateway 無關。
- worker 容器（MyLinuxPool 建立）：
  - sidecar daemon；
  - agent（Claude Code／opencode）；
  - `pool-worker` 的讀取 token（MyLinuxPool 掛進來）。
- Mac：sidecar daemon、agent、Mac 的讀取 token。
- 上游：Anthropic、OpenRouter、Ollama Cloud、OpenCode Zen。
- agora：Session 的保存與接續。

**連線（誰 → 誰：做什麼）**
- workflow → 跳板 → `llmgw`：SSH，只能跑部署腳本；寫 key 與 token、升級。
- 部署腳本 → `lgwd`：重讀或重啟。
- sidecar（worker 與 Mac） → `lgwd`：list、get、登錄冷卻（帶讀取 token）。
  - worker 經 MyLinuxPool#69 的路，一律連本機固定埠；
  - Mac 經 SSH 埠轉送。
- sidecar → agent：開子行程，env 由 AgentDriver 從 get 的結果推出，不上命令列。
- sidecar → agora：開啟時繼承 Session，停掉時寫回。
- agent → 上游：直接連。
- **沒有的連線**：
  - `lgwd` 不連 sidecar、不連 agent、不連 agora、不連上游；
  - MyLinuxPool 不呼叫 `lgwd` 的任何 API（只掛 token）；
  - Angareion 獨立。

## 2. 控制時序圖

### 2.1 開一個 agent
1. 控制者 → sidecar：`open {id 或 agent 種類}`。
2. sidecar → `lgwd`：list（看 `available`、`agents`、`priority`）。
3. sidecar 挑出一把（`available` 而且 `agents` 含要開的種類、`priority` 最前）。
4. sidecar → `lgwd`：get(id, agent)。`lgwd` 回共通 JSON（含 key），記一筆稽核。
5. sidecar → AgentDriver：由 JSON 推出 env，launch 子行程。
6. sidecar → agora：繼承指定的 Session。

### 2.2 額度用完、換一把（全部在 sidecar 內部，LLMGateway 只被呼叫）
1. AgentDriver → sidecar：狀態＝額度用完（窗口 5h 或 1w，有重置時刻就帶）。
2. sidecar → `lgwd`：登錄冷卻 `{window, resets_at}`。`lgwd` 記到重置＋2 分鐘，記一筆稽核。
3. sidecar → `lgwd`：list，挑下一把（同 2.1 第 3 步）。
4. 有能用的：
   - sidecar → `lgwd`：get；
   - sidecar → AgentDriver：stop（溫和）；
   - sidecar → agora：寫回；
   - sidecar → AgentDriver：用新 env launch，continue 同一個 Session。
5. 沒有能用的：sidecar 停掉 agent、寫回，狀態記成「等額度」（附最早的冷卻結束時刻，從 list 看得到）。
6. 冷卻時刻一過，list 自動回到 `available`。不需要任何人通知。

### 2.3 部署 key 與 token
1. 本人 → GitHub：手動觸發 `deploy-keys`，在 environment 核准。
2. workflow → 跳板 → `llmgw`：SSH，動詞 `keys`；stdin 帶五把 key 與各類 token。
3. 部署腳本：驗證 → 暫存檔 0600 → rename `keys.env` → 通知 `lgwd` 重讀。
4. `lgwd`：重讀 key，token 只存雜湊；舊 token 立即失效。

### 2.4 升級服務
1. 本人 → GitHub：手動觸發 `deploy-service`（輸入 tag），在 environment 核准。
2. workflow → 跳板 → `llmgw`：動詞 `upgrade <tag>`。
3. 安裝程式：固定 tag 取碼（已有就重用）、current 原子切換、裝 unit、重啟 `lgwd`；舊版本只留前一個。

### 2.5 建 worker（MyLinuxPool）
1. create-worker：照常建容器，並把 `pool-worker` 的讀取 token 像其他 `POOL_WORKER__*` 一樣掛進 worker。
2. worker 裡的 sidecar 啟動後就能 list／get。LLMGateway 不參與，也不需要知道有新 worker。

## 3. 介面圖

**LLMGateway 對外（只有這三個）**
- `list`：`GET /v1/subscriptions` → 所有 subscription 的共通 JSON，不含 key。
- `get`：`GET /v1/subscriptions/{id}?agent=<種類>` → 一筆的共通 JSON，含 key。`agent` 不在 `agents` 裡就回 403，這是 Claude 條款的守門。
- `登錄冷卻`：`POST /v1/subscriptions/{id}/cooldown {window: 5h|1w, resets_at?}`。
- 認證：`Authorization: Bearer <一類機器一把的讀取 token>`。

**共通 JSON schema（LLMGateway 與 sidecar 一致）**
- `schema`、`id`、`provider`、`kind`（`claude-subscription`／`api-key`）；
- `agents`（`claude-code`／`opencode`）、`priority`、`models{allow, default}`、`endpoint`；
- `key`（只在 get）；
- `cooldown{5h, 1w}`（各為 `{until}` 或 null）、`available`。
- **不含**環境變數名：由 sidecar 的 AgentDriver 推出。

**LLMGateway 內部（介面與實作分開）**
- `CredentialProvider`：一種 provider 一個實作（anthropic、openrouter、ollama-cloud、opencode-zen），負責 schema 中與 provider 有關的欄位。
- 政策檔：priority、models、agents。是資料，不是介面。
- 冷卻表、稽核紀錄：SQLite。

**sidecar 那一側用到 LLMGateway 的地方（MyAiStorage v3）**
- sidecar 內部有一層包住 list／get／登錄冷卻（aistorage-pm 稱 `SubscriptionSource`）。挑把邏輯在這一層。
- AgentDriver：把共通 JSON 變成 agent 的環境變數；判讀額度用完。可以用 `lgw.signals` 的規則表。

**和前一版相比拿掉的**
- 租約（request／start／renew／exhausted／release）、同時租用上限、10 分鐘確認；
- 狀態板、用量查詢；
- 管理 API（發撤 worker 憑證）、whoami；
- `launch`（agent 專用的環境變數對應）。
