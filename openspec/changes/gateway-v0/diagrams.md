# LLMGateway v4（定案）：實體、控制時序、介面（條列，供 PMO 畫圖）

> 依本人 2026-10-11 夜的方向：LLMGateway 只有 list、get、登錄冷卻三個介面；相依只有 sidecar→agora、sidecar→LLMGateway；LLMGateway 只依賴「一台連得到的 Linux 機器＋SSH」（會合點），不依賴 MyLinuxPool。sidecar 那一側見 MyAiStorage 的 `docs/proposals/aicompany-design-v3.md`，名詞一致。機器名不寫，「放服務的那台」是本人選定的常駐 provider。

## 1. 實體圖

**實體**
- GitHub（LLMGateway repo）：
  - secret：五把 key、部署金鑰與部署連線資訊；
  - workflow：`deploy-keys`、`deploy-service`（手動觸發、本人核准）。
- 會合點（用設定指定的一台 SSH 主機；v0 是池跳板機上現有的 sshproxy 帳號，本人指定）：
  - 127.0.0.1:4710（API）、127.0.0.1:4711（服務那台的 sshd）：服務那台以 reverse tunnel 公開過來的；
  - `GatewayPorts no`，只綁 loopback。
- 放服務的那台：跑兩個服務。
  - 帳號 `llmgw`（LLMGateway）：
    - `lgwd`（list／get／登錄冷卻，只綁 127.0.0.1）；
    - 隧道守護（自己的 `ssh -R` 到會合點＋健康探針）；
    - `remote-deploy.sh`（動詞 `keys`、`upgrade`）；
    - `keys.env`（0600，五把 key）；
    - SQLite（冷卻、稽核）。
  - 池的管理帳號（MyLinuxPool 的服務）：和 LLMGateway 無關。
- worker 容器（MyLinuxPool 建立）：
  - sidecar daemon；
  - agent（Claude Code／opencode）；
  - pool-tunnel 既有的 sshproxy 金鑰（id_tunnel），sidecar 用它連會合點。
- Mac：sidecar daemon、agent、自己能登入 sshproxy 的金鑰。
- 上游：Anthropic、OpenRouter、Ollama Cloud、OpenCode Zen。
- agora：Session 的保存與接續。

**連線（誰 → 誰：做什麼）**
- 隧道守護 → 會合點：`ssh -R`，把 lgwd 的 API 埠與那台的 sshd 公開到會合點的 127.0.0.1；斷了就重連。
- 隧道守護 → 會合點 → lgwd：每 60 秒端對端探一次。
- workflow → 會合點（sshproxy，用部署金鑰）→ 服務那台的 `llmgw`：SSH，只能跑部署腳本；寫 key、升級。
- 部署腳本 → `lgwd`：重讀或重啟。
- sidecar（worker 與 Mac）→ `lgw connect` →（跳板）→ 會合點（sshproxy）→ `lgwd`：list、get、登錄冷卻（不做應用層認證）。
- sidecar → agent：開子行程，env 由 AgentDriver 從 get 的結果推出，不上命令列。
- sidecar → agora：開啟時繼承 Session，停掉時寫回。
- agent → 上游：直接連。
- **沒有的連線**：
  - `lgwd` 不連 sidecar、不連 agent、不連 agora、不連上游；
  - MyLinuxPool 不呼叫 `lgwd` 的任何 API；不用 pool-tunnel、NODE_*、池的帳號（只負責把客戶端憑證掛進 worker）；
  - Angareion 獨立。

## 2. 控制時序圖

### 2.0 服務端的隧道與健康（常駐）
1. 隧道守護 → 會合點：`ssh -N -R 127.0.0.1:4710:127.0.0.1:<本機 API 埠> -R 127.0.0.1:4711:127.0.0.1:22`（`ExitOnForwardFailure`、`ServerAliveInterval=30`、`ServerAliveCountMax=3`）。
2. 公開失敗或斷線 → 守護程式結束 → systemd 立即重開 → 回到第 1 步。
3. 每 60 秒：守護程式經會合點 `ssh -W 127.0.0.1:4710` 打 `GET /v1/subscriptions`，收到 HTTP 200 就算健康。
4. 連續 3 次沒有 HTTP 回應 → 殺掉隧道連線 → systemd 重開。

### 2.0a 客戶端連線
1. sidecar → `lgw connect`（函式庫）：依設定 `ssh -N -L 127.0.0.1:<臨時埠>:127.0.0.1:4710 [-J 跳板…] sshproxy@<跳板機>`。
2. 成功 → 回 `http://127.0.0.1:<臨時埠>`；失敗 → 回「無法確認」，sidecar 等一下再試。
3. 之後的 list／get／登錄冷卻都打這個位址（不帶 token）。

### 2.1 開一個 agent
1. 控制者 → sidecar：`open {subscription 的 id、agent 種類、Session}`。控制者只給 id，key 不經過控制者。
2. sidecar → `lgwd`（經 2.0a 的連線）：get(id, agent)。`lgwd` 回共通 JSON（含 key），記一筆稽核。
3. sidecar → AgentDriver：由 JSON 推出 env，launch 子行程。
4. sidecar → agora：繼承指定的 Session。

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

### 2.3 部署 key
1. 本人 → GitHub：手動觸發 `deploy-keys`，在 environment 核准。
2. workflow → 會合點 → `llmgw`：`ssh -J sshproxy@<跳板機> -p 4711 llmgw@127.0.0.1`，動詞 `keys`；stdin 帶五把 key。
3. 部署腳本：驗證 → 暫存檔 0600 → rename `keys.env` → 通知 `lgwd` 重讀。
4. `lgwd`：重讀 key。

### 2.4 升級服務
1. 本人 → GitHub：手動觸發 `deploy-service`（輸入 tag），在 environment 核准。
2. workflow → 會合點 → `llmgw`：動詞 `upgrade <tag>`。
3. 安裝程式：固定 tag 取碼（已有就重用）、current 原子切換、裝 unit、重啟 `lgwd`；舊版本只留前一個。

### 2.5 建 worker（MyLinuxPool）
1. create-worker：照常建容器（id_tunnel 本來就有）。
2. worker 裡的 sidecar 用 `lgw connect` 連到會合點後就能 list／get。LLMGateway 不參與，也不需要知道有新 worker。

## 3. 介面圖

**LLMGateway 對外（只有這三個）**
- `list`：`GET /v1/subscriptions` → 所有 subscription 的共通 JSON，不含 key。
- `get`：`GET /v1/subscriptions/{id}?agent=<種類>` → 一筆的共通 JSON，含 key。`agent` 不在 `agents` 裡就回 403，這是 Claude 條款的守門。
- `登錄冷卻`：`POST /v1/subscriptions/{id}/cooldown {window: 5h|1w, resets_at?}`。
- 認證：不做應用層認證，只靠 SSH（本人選 (a)）。

**連線（LLMGateway 提供，不是 API）**
- 伺服器端：隧道守護 `llmgateway-tunnel.service`。設定檔 `tunnel.env` 有會合點 `user@host:port`、金鑰、known_hosts、會合點上的 API 埠與 SSH 埠、探針間隔。
- 客戶端：`lgw connect`。
  - 函式庫：`with lgw.connect(cfg) as base_url`；
  - 指令：`lgw connect`。
  - 設定檔 `connect.env` 有 `jumps`、`rendezvous`、`identity`、`known_hosts`、`remote_api`。
- 會合點（sshproxy）上登記的 LLMGateway 公鑰：隧道金鑰（服務那台產生）、部署金鑰（secret）。能帶選項就收窄；本人已接受不收窄。

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

**sidecar 那一側用到 LLMGateway 的地方（MyAiStorage v4）**
- sidecar 內部有一層包住 list／get／登錄冷卻（aistorage-pm 稱 `SubscriptionSource`）。挑把邏輯在這一層。
- AgentDriver：把共通 JSON 變成 agent 的環境變數；判讀額度用完。可以用 `lgw.signals` 的規則表。

**和前一版相比拿掉的**
- 租約（request／start／renew／exhausted／release）、同時租用上限、10 分鐘確認；
- 狀態板、用量查詢；
- 管理 API（發撤 worker 憑證）、whoami；
- 依賴 MyLinuxPool 的部分：池的跳板機轉發條目（#68）、pool-tunnel 上的路（#69）、docker bridge 綁定；
- `launch`（agent 專用的環境變數對應）。
