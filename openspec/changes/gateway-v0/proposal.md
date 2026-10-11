對應工單：#8「LLMGateway v0：切 scope、接上訂閱，讓 Mac 的 opencode／Claude Code 經由 Gateway 使用」；上層：MyPMO#4。

> **v4（2026-10-11 深夜）**：v3（三個介面）加上本人對 v3 的第 5～7 點：不依賴 MyLinuxPool、服務自己用 reverse tunnel 公開並維持健康、提供客戶端連線程式（含跳板）、認證二選一。v3 的內容照舊。之前的版本（資料面代轉、租約）見 git 歷史與 #8 的留言。sidecar 那一側見 MyAiStorage 的 `docs/proposals/aicompany-design-v3.md`（aistorage-pm），兩邊的共通 JSON schema 已一致。

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
  - 服務只綁 127.0.0.1，由自己的隧道守護程式以 ssh reverse tunnel 公開到會合機，並用端對端探針維持健康；
  - 客戶端用 LLMGateway 提供的 `lgw connect`（可經跳板）連過去；
  - 部署也走會合機；
  - 不用 pool-tunnel、NODE_*、池的帳號。
- **認證**（二選一，請本人選，design 決定 5a）：(a) 去掉應用層認證，只靠 SSH；(b) 一把共用 token（**建議**）。**沒有發證、撤證的 API**。
- **部署**（spec `key-vault`）：獨立帳號 `llmgw`、`deploy-keys`／`deploy-service`、`remote-deploy.sh` 的 `keys`／`upgrade` 動詞、forced command；路徑改走會合機。
- **拿掉**：租約（request／start／renew／exhausted／release）、同時租用上限、10 分鐘確認、狀態板、管理 API（issuer）、用量查詢。期 1 的 `agent-lease`、`usage-record` 規格一併移除。

## Capabilities

### New Capabilities

- `subscription-registry`：list、get、登錄冷卻、共通 JSON schema、稽核紀錄。
- `key-vault`：key 只放一處、部署、值不外洩、認證（共用 token）、服務只綁 127.0.0.1。
- `connectivity`：會合機、服務端的隧道守護與健康探針、客戶端連線程式、會合機上的存取收窄。

### Modified Capabilities

- `agent-lease`：全部移除（REMOVED），改由 `subscription-registry` 與 sidecar 取代。
- `usage-record`：全部移除（REMOVED）。

## 要本人決定的

**認證：(a) 或 (b)**（詳見 design 決定 5a）。
- (a) 去掉應用層認證，安全只靠 SSH。會合機上**所有**能開 TCP 轉發的帳號都連得到 API 埠。如果會合機是池的跳板機，sshproxy（所有 provider、worker 的隧道金鑰，以及家人維修機共用的那把）與管理帳號都沒有 permitopen 限制，都讀得到全部的 key；要安全就得改 MyLinuxPool，或另找一台只給 LLMGateway 用的會合機。
- (b) 一把共用 token：會合機上有誰都不影響。代價是客戶端要多帶一把 token。
- **建議 (b)**。

## 不做（v0）與理由

- **同時租用上限**：沒有租約就算不出一把正被幾個 agent 用；本人要最少的介面。
- **「誰正在用哪一把」**：看各 sidecar 的 status（裡面有 subscription 名）；LLMGateway 的稽核紀錄看得到「誰在何時 get 過哪一把」。
- **分辨是哪一台 worker**：認證不分機器，所以分不出來；要分就得多一套發撤憑證的介面。

## 銜接

- **MyAiStorage（sidecar v3）**：用 list、get、登錄冷卻；挑選與換把在 sidecar 內部；env 對應在 AgentDriver。
- **MyLinuxPool**（由 PMO 協調，v4 定案後再通知）：
  - #68、#69 不需要（部署與連線都走會合機）；
  - #70 縮成把客戶端憑證掛進 worker：會合機的客戶端金鑰，選 (b) 時再加 token。
- **MyAiEntry#147**：手機一樣經會合機連線（App 內做 SSH 轉發），用 list／get。

## 不做

- 資料面代轉、格式轉換（LiteLLM）。
- 依內容或效能改送（#3、#4）。
- token／成本計量、用量 %。
- Antigravity（agy）。
