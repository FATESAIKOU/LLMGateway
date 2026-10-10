對應工單：#1「LLMGateway 設計：agent 的開關申請與用量紀錄（期 1 只做設計）」；上層：MyPMO#31（AiCompany 期 1）、MyPMO#4（啟動 LLMGateway）。

## Why

AiCompany 期 1 要在 MyLinuxPool 的 worker 上跑 AI team，Mac 上也天天在開 agent，但現在沒有任何地方知道「哪台機器、什麼角色、用哪個訂閱、哪個 coding agent、哪個模型」正在跑。憑證是用 MyLinuxPool 的能力寫死全發（MyLinuxPool#50），Claude 的個人／公司兩把要能切換、而且不能安靜地切換。LLMGateway 的設計同時牽動 sidecar（MyAiStorage#31）與 worker 的設計，所以要在它們定案前把分工講清楚。

## What Changes

- 新增「agent 租約」：任何 launcher（worker 上的 sidecar、Mac 上的啟動包裝）開 agent 前向 LLMGateway 申請一張租約，寫明 worker、角色、訂閱、coding agent、模型與來由；結束、切換、逾期都留下事件。
- 新增「用量紀錄」：以租約為主鍵，把租約事件（誰在何時用了什麼）當權威紀錄；token／成本由各 agent 既有的觀測管道（Claude Code OpenTelemetry、opencode 的 session 匯出、供應商 API）帶上租約 id 回報，LLMGateway 彙整。
- 定下 LLMGateway 與 sidecar 的分工：**LLMGateway 決定與記錄，不持有、不轉送既有的上游憑證值**；launcher 從本機的憑證來源（worker 的能力掛載、Mac 的登入）取出租約指定的那一把，注入後啟動 agent。
- 定下 Claude 個人為主、公司備援的切換：由 launcher 偵測額度訊號、換租約、接續 session，並發出通知；不在中間層安靜換帳號。
- 定下從「能力寫死」遷移到這套的階段（只記錄 → 照租約注入 → 憑證改由租約發放），每一階段都能單獨關掉。
- 期 1 **只做設計**，不寫程式。

## Capabilities

### New Capabilities

- `agent-lease`：agent 開關的申請、核准、啟動、切換、結束與逾期；租約的欄位與狀態；launcher 與 LLMGateway 的責任界線。
- `usage-record`：租約事件紀錄與 token／成本用量的歸因、回報與查詢；紀錄裡能放與不能放的內容。

### Modified Capabilities

（無；本 repo 目前沒有既有 spec）

## Impact

- MyAiStorage#31（sidecar）：sidecar 啟動 agent 前要申請租約、結束時回報；憑證注入由 sidecar 做。需要 sidecar 設計確認。
- MyAiStorage#30（Angareion）：切換與額度用完的通知走 Angareion；角色的格式等它的身分表示定案後對齊。
- MyLinuxPool#46／#50：能力仍負責把憑證掛進 worker；遷移後期才縮小掛載範圍。
- Mac 上開 agent 的方式（herdr）：多一層啟動包裝。
- 調查依據：`docs/research/`（sub2api、替代方案、agent 端點、Claude 切換、sidecar／Angareion）。

## 不做

- 不實作（期 1 只做設計）。
- 不做資料面的閘道（代理所有模型請求的服務）；何時需要另立條件，見 design.md。
- 不做多訂閱無縫切換（#2）、免費模型依效能自動切換（#3）、隱私界線（#4）；本設計只留接點。
- 不處理 Antigravity（agy）的端點改寫；agy 只登記租約。
