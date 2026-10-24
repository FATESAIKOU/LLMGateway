# LLMGateway v4（定案）：實體、控制時序、介面（條列，供 PMO 畫圖）

> 依本人 2026-10-11 夜的方向：LLMGateway 只有 list、get、登錄冷卻三個介面；相依只有 sidecar→agora、sidecar→LLMGateway；LLMGateway 只依賴「一台連得到的 Linux 機器＋SSH」（會合點），不依賴 MyLinuxPool。sidecar 那一側見 MyAiStorage 的 `docs/proposals/aicompany-design-v3.md`，名詞一致。機器名不寫，「放服務的那台」是本人選定的常駐 provider。

> v5（2026-10-12，本人）：下面凡標「（v5：…）」的行已被取代，取代內容見文末「v5 變更」一節；沒標的行不變。

## 1. 實體圖

**實體**
- GitHub（LLMGateway repo）（v5 第二輪：兩個 workflow 整組拿掉；secret 只剩解密密碼備份）：
  - secret：`GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`（解密密碼備份）；
  - private repo `FATESAIKOU/LLMGateway-keys`：`keys.env.enc` 正本（只有本人用）。
- 會合點（用設定指定的一台 SSH 主機；v0 是池跳板機上現有的 sshproxy 帳號，本人指定）：
  - 127.0.0.1:4710（API）、127.0.0.1:4711（服務那台的 sshd）：服務那台以 reverse tunnel 公開過來的；
  - `GatewayPorts no`，只綁 loopback。
- 放服務的那台：跑兩個服務，外加 key-sync（v5，見文末）。
  - 帳號 `llmgw`（LLMGateway）：
    - `lgwd`（list／get／登錄冷卻，只綁 127.0.0.1）；
    - 隧道守護（自己的 `ssh -R` 到會合點＋健康探針；v5：程式搬到 `ops-scripts/`，行為不變）；
    - key-sync（v5：約每 10 分鐘用 `gh` 拉加密檔，見文末）；
    - `keys.env`（0600，五把 key；v5：改由 key-sync 從 private repo 加密檔拉下來換）；
    - SQLite（冷卻、稽核；v5：單一 `until`）。
  - 池的管理帳號（MyLinuxPool 的服務）：和 LLMGateway 無關。
- worker 容器（MyLinuxPool 建立）：
  - sidecar daemon；
  - agent（Claude Code／opencode）；
  - pool-tunnel 既有的 sshproxy 金鑰（id_tunnel），sidecar 用它連會合點。
- Mac：sidecar daemon、agent、自己能登入 sshproxy 的金鑰。
- 手機：不跑 sidecar；自行用 ssh -J 等方式把會合點 API 轉到本機再打 list／get（MyAiEntry#147 已關掉，不做 App 內轉發；LLMGateway 不提供程式）。
- 上游：Anthropic、OpenRouter、Ollama Cloud、OpenCode Zen。
- agora：Session 的保存與接續。

**連線（誰 → 誰：做什麼）**
- 隧道守護 → 會合點：`ssh -R`，把 lgwd 的 API 埠與那台的 sshd 公開到會合點的 127.0.0.1；斷了就重連。
- 隧道守護 → 會合點 → lgwd：每 60 秒端對端探一次。
- sidecar（worker 與 Mac）→ 本機轉發 →（跳板）→ 會合點（sshproxy）→ `lgwd`：list、get、登錄冷卻（不做應用層認證；v5：不再經 `lgw connect`，sidecar 直接 curl 本機 port）。
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

### 2.0a 客戶端連線（v5：整節由文末 v5 取代；不再有 `lgw connect`）
1. sidecar → `lgw connect`（函式庫）：依設定 `ssh -N -L 127.0.0.1:<臨時埠>:127.0.0.1:4710 [-J 跳板…] sshproxy@<跳板機>`。
2. 成功 → 回 `http://127.0.0.1:<臨時埠>`；失敗 → 回「無法確認」，sidecar 等一下再試。
3. 之後的 list／get／登錄冷卻都打這個位址（不帶 token）。

### 2.1 開一個 agent
1. 控制者 → sidecar：`open {subscription 的 id、agent 種類、Session}`。控制者只給 id，key 不經過控制者。
2. sidecar → `lgwd`（經 2.0a 的連線）：get(id, agent)。`lgwd` 回共通 JSON（含 key），記一筆稽核。
3. sidecar → AgentDriver：由 JSON 推出 env，launch 子行程。
4. sidecar → agora：繼承指定的 Session。

### 2.2 額度用完、換一把（全部在 sidecar 內部，LLMGateway 只被呼叫）
1. AgentDriver → sidecar：狀態＝額度用完（何時能再用，有時刻就帶；判讀不出時 sidecar 以現在＋1 小時報）。
2. sidecar → `lgwd`：登錄冷卻 `{until}`。`lgwd` 記到該時刻，記一筆稽核。
3. sidecar → `lgwd`：list，挑下一把（同 2.1 第 3 步）。
4. 有能用的：
   - sidecar → `lgwd`：get；
   - sidecar → AgentDriver：stop（溫和）；
   - sidecar → agora：寫回；
   - sidecar → AgentDriver：用新 env launch，continue 同一個 Session。
5. 沒有能用的：sidecar 停掉 agent、寫回，狀態記成「等額度」（附最早的冷卻結束時刻，從 list 看得到）。
6. 冷卻時刻一過，list 自動回到 `available`。不需要任何人通知。

### 2.3 拉 key（v5：整節取代部署 key；不再有 `deploy-keys` workflow）
1. 本人 → private repo：更新加密檔正本。
2. key-sync（約每 10 分鐘）→ private repo：拉→比對→解密→驗證→原子替換 `keys.env`。
3. `lgwd`：依 mtime 偵測到換檔就重讀（失敗沿用舊的）。

### 2.4 升級服務（v5 第二輪選乙：手動，不再有 deploy-service）
1. 本人打 tag `lgw-vX.Y.Z` 並 push。
2. 本人以管理帳號 ssh 上服務那台，跑 `install.sh --tag <tag>`（手冊步驟，不寫機器名），前後各跑一次 `--check`。
3. 舊版本只留前一個；`keys.env` 與 `tunnel.env` 不隨版本。

### 2.5 建 worker（MyLinuxPool；v5：通道由 MyLinuxPool 的 capability 建）
1. create-worker：照常建容器（id_tunnel 本來就有）。
2. worker 裡的 sidecar 經自備的本機通道連到會合點後就能 list／get。LLMGateway 不參與，也不需要知道有新 worker。

## 3. 介面圖

**LLMGateway 對外（只有這三個）**
- `list`：`GET /v1/subscriptions` → 所有 subscription 的共通 JSON，不含 key。
- `get`：`GET /v1/subscriptions/{id}?agent=<種類>` → 一筆的共通 JSON，含 key。`agent` 不在 `agents` 裡就回 403，這是 Claude 條款的守門。
- `登錄冷卻`：`POST /v1/subscriptions/{id}/cooldown {until}`（v5：只收何時能再用）。
- 認證：不做應用層認證，只靠 SSH（本人選 (a)）。

**連線（v5：LLMGateway 不再提供連線程式）**
- 伺服器端：隧道守護 `llmgateway-tunnel`（v5：程式搬到 `ops-scripts/`）。設定檔 `tunnel.env` 有會合點 `user@host:port`、金鑰、known_hosts、會合點上的 API 埠與 SSH 埠、探針間隔。
- 客戶端：自備通道。sidecar 把會合點 API 轉到本機 loopback 某 port 後直接 curl（本機 port 預設 4710，可改；worker 由 MyLinuxPool capability 建通道，Mac 用 mlp 或 ssh -J 自己轉發；手機自行用 ssh -J 等方式轉發，LLMGateway 不提供程式）。
- 會合點（sshproxy）是既有設施：隧道金鑰（服務那台產生）照舊登記；部署金鑰整組不用了（v5：`keys` 動詞與 deploy-keys 拿掉）。

**共通 JSON schema（LLMGateway 與 sidecar 一致）**
- `schema`、`id`、`provider`、`kind`（`claude-subscription`／`api-key`）；
- `agents`（`claude-code`／`opencode`）、`priority`、`models{allow, default}`、`endpoint`；
- `key`（只在 get）；
- `cooldown{until}`（v5：單一 `{until}` 或 null，不再分窗口）、`available`。
- **不含**環境變數名：由 sidecar 的 AgentDriver 推出。

**LLMGateway 內部（介面與實作分開）**
- `CredentialProvider`：一種 provider 一個實作（anthropic、openrouter、ollama-cloud、opencode-zen），負責 schema 中與 provider 有關的欄位。
- 政策檔：priority、models、agents。是資料，不是介面。
- 冷卻表、稽核紀錄：SQLite。

**sidecar 那一側用到 LLMGateway 的地方（MyAiStorage v4；v5：內部層次是 sidecar 的事）**
- sidecar 內部自理 list／get／登錄冷卻的呼叫與挑把邏輯（v5：不再稱 SubscriptionSource，那是 sidecar 內部名）。
- AgentDriver：把共通 JSON 變成 agent 的環境變數；判讀額度用完（判讀不出恢復時間時以現在＋1 小時報）。可以用 `lgw.signals` 的規則表。

**和前一版相比拿掉的**
- 租約（request／start／renew／exhausted／release）、同時租用上限、10 分鐘確認；
- 狀態板、用量查詢；
- 管理 API（發撤 worker 憑證）、whoami；
- 依賴 MyLinuxPool 的部分：池的跳板機轉發條目（#68）、pool-tunnel 上的路（#69）、docker bridge 綁定；
- `launch`（agent 專用的環境變數對應）；
- v5 拿掉的：`lgw connect` 與 `connect.json`、`deploy-keys` workflow、`remote-deploy.sh keys` 動詞、SIGHUP 重讀、部署公鑰／forced command、冷卻 `window`（5h／1w）。

## v5 變更（2026-10-12，本人；以 v4 為底，只列改的地方）

1. **冷卻只收 `until`**：`POST …/cooldown` 的 body 改 `{until}`；`cooldown` 欄位改單一 `{until}` 或 null；`available`＝已部署且無未來 `until`；sidecar 判讀不出恢復時間時報現在＋1 小時。
2. **拿掉 `lgw connect`**：上文 §2.0a 整節作廢；sidecar 直接 curl 本機 port（本機 port 預設 4710，可改；worker 由 MyLinuxPool capability 建通道，Mac 用 mlp 或 ssh -J 自己轉發；手機自行轉發，LLMGateway 不提供程式，MyAiEntry#147 已關掉）。
3. **反向隧道搬到 `ops-scripts/`**：`llmgateway-tunnel` 與其 unit 內容不變、只搬目錄；`install.sh`、`bootstrap.sh` 改從新路徑裝。
4. **金鑰自己拉（v5 第二輪選乙）**：正本是 `FATESAIKOU/LLMGateway-keys` 的 `keys.env.enc`；key-sync 約每 10 分鐘用 `gh` 拉（`gh api …/contents/keys.env.enc`，blob `sha` 即版本）→解碼→解密→驗證→原子替換；`lgwd` 依 mtime 重讀；`gh` 登入是使用前提（`llmgw` 身分、`gh auth login` 用瀏覽器登入，不用 token 檔）。拿掉：兩個 workflow、`remote-deploy.sh` 整檔、SIGHUP 重讀、部署公鑰／forced command、部署連線 4 secret、MyLinuxPool 部署公鑰登記（隧道公鑰保留）。升級改手動（上文 §2.4）。
5. **說法**：sshproxy 是既有設施；sidecar 內部層次不寫名。
