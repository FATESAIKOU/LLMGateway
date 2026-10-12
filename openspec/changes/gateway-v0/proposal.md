對應工單：#8「LLMGateway v0：切 scope、接上訂閱，讓 Mac 的 opencode／Claude Code 經由 Gateway 使用」；上層：MyPMO#4。

> **v4 定案（本人，2026-10-11 深夜）**：三個介面；不依賴 MyLinuxPool；服務自己用 reverse tunnel 公開到會合點並維持健康；提供客戶端連線程式（含跳板）；會合點用池跳板機上現有的 sshproxy 帳號；不做應用層認證。之前的版本（資料面代轉、租約）見 git 歷史與 #8 的留言。sidecar 那一側見 MyAiStorage 的 `docs/proposals/aicompany-design-v3.md`（aistorage-pm），兩邊的共通 JSON schema 已一致。

## Why

本人的原始需求：AI 在 worker 上工作到一半 token 用完時，由外部的確定性程式自動換一把。可以換的有五把：

| subscription id | provider | 是什麼 |
|---|---|---|
| `claude-personal` | anthropic | Claude Code 個人訂閱（setup-token） |
| `claude-company` | anthropic | Claude Code 公司訂閱（setup-token） |
| `openrouter` | openrouter | OpenRouter key |
| `opencode-zen` | opencode-zen | OpenCode Zen key |
| `ollama-cloud` | ollama-cloud | Ollama Cloud key |

本人的新方向：

> 「LLMGateway 我想現在應該需要的介面是（不需要多餘介面喔）：
> - 列出 Provider-Subscription 一覽
> - 取得 Provider-Subscription 的詳細情報（共通 schema 的 json），sidecar 的 aiagent 生命週期控制介面吃這個共通 json schema 開 aiagent
> - 登錄這 subscription 的 1w／5h 冷卻時間，這樣 list 才能列出這資訊（現在能不能用）」

相依只有 sidecar→agora、sidecar→LLMGateway；LLMGateway 本身只依賴「一台連得到的 Linux 機器＋SSH」（會合機）。

## What Changes

- **LLMGateway 對外只有三個介面**（spec `subscription-registry`）：
  - `list`：所有 subscription 的共通 JSON，不含 key，含冷卻與 `available`；
  - `get(id, agent)`：一筆的共通 JSON，含 key。`agent` 必填，不在該筆的 `agents` 裡就拒絕；這是 Claude 條款的守門：Claude 訂閱只給 `claude-code`。
  - 登錄冷卻：`5h`／`1w` 的重置時刻，過了自動解除。
- **共通 JSON schema**（和 aistorage-pm 一致）：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models`、`endpoint`、`key`（只在 get）、`cooldown`（`5h`、`1w`）、`available`。**不帶 agent 專用的環境變數**：把 key 變成哪些環境變數，由 sidecar 的 agent driver 決定，跟著 agent 版本走。
- **換把由 sidecar 做**：偵測到用完 → 登錄冷卻 → 從 list 挑 `available` 而且 `agents` 含自己種類、`priority` 最前的 → get → 重開並 continue 同一個 Session。LLMGateway 只保管、列出、記冷卻。
- **連線**（spec `connectivity`）：
  - 服務只綁 127.0.0.1，由自己的隧道守護程式以 ssh reverse tunnel 公開到會合點（sshproxy；API 埠 4710、SSH 埠 4711），並用端對端探針維持健康；
  - 客戶端用 LLMGateway 提供的 `lgw connect`（可經跳板）連過去；worker 用 pool-tunnel 既有的 sshproxy 金鑰，Mac 用自己的；
  - 部署也走會合點；
  - 不用 pool-tunnel、NODE_*。
- **認證**：**不做應用層認證，只靠 SSH**（本人選 (a)：「反正洩漏我換 key 很簡單」）。沒有 token、沒有發證或撤證的 API。
- **部署**（spec `key-vault`）：獨立帳號 `llmgw`、`deploy-keys`／`deploy-service`、`remote-deploy.sh` 的 `keys`／`upgrade` 動詞、forced command；路徑改走會合機。
- **拿掉**：租約（request／start／renew／exhausted／release）、同時租用上限、10 分鐘確認、狀態板、管理 API（issuer）、用量查詢。期 1 的 `agent-lease`、`usage-record` 規格一併移除。

## Capabilities

### New Capabilities

- `subscription-registry`：list、get、登錄冷卻、共通 JSON schema、稽核紀錄。
- `key-vault`：key 只放一處、部署、值不外洩、不做應用層認證、服務只綁 127.0.0.1。
- `connectivity`：會合點與遠端埠、服務端的隧道守護與健康探針、客戶端連線程式。

### Modified Capabilities

- `agent-lease`：全部移除（REMOVED），改由 `subscription-registry` 與 sidecar 取代。
- `usage-record`：全部移除（REMOVED）。

## v5 變更（本人，2026-10-12；以 v4 為底，只列改的地方）

1. **冷卻只收 `until`**：登錄冷卻的 body 改為 `{until}`（RFC 3339 UTC，何時能再用），拿掉 `window`（5h／1w）；list／get 的 `cooldown` 欄位改成單一 `until`（或 null）；`available`＝已部署且沒有未來的 `until`。sidecar 拿不到恢復時間時報「現在＋1 小時」（那是 sidecar 的行為；`lgw.signals` 只負責把判讀結果轉成 `{until}`）。
2. **拿掉 `lgw connect`**：sidecar 直接 curl 本機的 port（本機 port 預設 4710，可改）。worker 端通道由 MyLinuxPool 的 LLMGateway capability 附建通道小 script 建立（LLMGateway 只寫清楚它需要什麼：本機 loopback 4710 即 HTTP API）；Mac 用 mlp 或 ssh -J 自己轉發；手機自行用 ssh -J 等方式轉發，LLMGateway 不提供程式。
3. **反向隧道搬到 `ops-scripts/`**：`llmgateway-tunnel` 與其 unit 從 `deploy/` 搬到 `ops-scripts/`（維運工具定位），內容不變。
4. **金鑰正本改放 `FATESAIKOU/LLMGateway-keys` 的 `keys.env.enc`**：兩個 workflow、`remote-deploy.sh` 整檔、SIGHUP 重讀、部署公鑰／forced command、部署連線 secret 整組拿掉（乙）；key-sync 約每 10 分鐘用 `gh` 拉（`gh` 登入是使用前提，`llmgw` 身分，token 二選一），拉到新版就換。升級改管理帳號手動 ssh 上去跑 `install.sh --tag`。細節見 `docs/research/v5-plan.md`。
5. **文件說法**：sidecar 內部的 SubscriptionSource 不是 LLMGateway 的事；sshproxy 是既有設施（MyLinuxPool 維運，LLMGateway 只當會合點用）。

### Capabilities（v5 增補）

- `subscription-registry`：MODIFIED（冷卻 until-only；`cooldown` 欄位、`available`、錯誤碼跟著改）。
- `connectivity`：MODIFIED（拿掉客戶端連線程式，加客戶端自備通道；服務端隧道守護保留，只搬目錄）。
- `key-vault`：MODIFIED（key 正本改 private repo 加密檔，加定期拉取與輪替）。

## 本人的決定（2026-10-11 深夜）

- **認證**：選 (a)，不做應用層認證。本人知道 sshproxy 上所有金鑰（provider、worker、家人維修機共用的那把）都讀得到全部 key，並接受這個風險。
- **會合點**：直接用池跳板機上的 sshproxy 帳號。
- **控制者只給 id**，sidecar 自己 get。
- **priority** 之後可能改成外部注入的函式計算；挑選在 sidecar，LLMGateway 照樣回這個欄位。

## 不做（v0）與理由

- **同時租用上限**：沒有租約就算不出一把正被幾個 agent 用；本人要最少的介面。
- **「誰正在用哪一把」**：看各 sidecar 的 status（裡面有 subscription 名）；LLMGateway 的稽核紀錄看得到「誰在何時 get 過哪一把」。
- **分辨是哪一台 worker**：認證不分機器，所以分不出來；要分就得多一套發撤憑證的介面。

## 銜接

- **MyAiStorage（sidecar v4）**：用 list、get、登錄冷卻；挑選與換把在 sidecar 內部；env 對應在 AgentDriver。
- **MyLinuxPool**（mylinuxpool-pm 提最小的做法，PMO 協調）：
  - 在 sshproxy 登記 LLMGateway 的隧道公鑰（v5 乙：部署公鑰不登記了）；
  - 確認 worker 的 sidecar 讀得到 id_tunnel；
  - #69、#70 不需要。
- **MyAiEntry#147 已關掉**（本人裁定）：不做 App 內轉發；手機自行用 ssh -J 等方式轉發，LLMGateway 不提供程式。

## 不做

- 資料面代轉、格式轉換（LiteLLM）。
- 依內容或效能改送（#3、#4）。
- token／成本計量、用量 %。
- Antigravity（agy）。
