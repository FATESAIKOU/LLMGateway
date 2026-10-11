對應工單：#8「LLMGateway v0：切 scope、接上訂閱，讓 Mac 的 opencode／Claude Code 經由 Gateway 使用」；上層：MyPMO#4。

> 這份先只有 scope 提案，給本人確認。確認後再補 design、specs、tasks，然後實作。

## Why

本人要 LLMGateway 盡快能用：現有訂閱接到 Gateway 上，Mac 上的 opencode 與 Claude Code 改走 Gateway。期 1 的設計（#1，`agent-lease-and-usage-record`）只做了控制面：租約加紀錄，模型請求由 agent 直接打上游。v0 要的是**資料面**，也就是請求真的經過 Gateway。所以這一版會改掉期 1 設計的兩個前提，見下方「和期 1 設計的差別」。

## v0 做什麼

1. **一個資料面 Gateway**，跑在池內一台常駐的 provider 上，只綁 loopback。
   - **推薦 LiteLLM Proxy**（MIT）。理由：
     - 它的 virtual key 可以帶 metadata（worker、角色、agent），用量紀錄也能匯出；
     - 它認得 Claude 訂閱 token；
     - 同一個模型可以掛兩把憑證，自動分攤；遇到 429 就讓那一把進冷卻、改用另一把（router 的 cooldown 機制，spike 時確認），正好對上「Claude 兩把地位相同、平均分攤、冷卻中不選」的裁定。
   - 備選是本人試用中的 sub2api。它的訂閱帳號池更強（會追蹤 5h／7d 額度窗口），但 key 不能帶 metadata，README 自己寫了條款風險，而且要加開 PostgreSQL 與 Redis。
   - 實作第一步先用半天做 spike：拿 Claude 訂閱 token 經 LiteLLM 跑通 Claude Code。如果不穩，就改用 sub2api。
2. **接上的訂閱**：Claude 個人、Claude 公司、Ollama Cloud、OpenRouter、OpenCode Zen。
   - 憑證值由本人放進只有 Gateway 讀得到的檔案，或由 PMO 用瀏覽器帶本人設，**不經過 AI**。
3. **Mac 改走 Gateway**：
   - 連線：沿用池的 SSH 跳板做本機埠轉送（`ssh -L`），Mac 已經有 client 身分，不需要開公開埠。
   - Claude Code：加一個包裝指令（暫名 `claude-gw`），設 `ANTHROPIC_BASE_URL` 指向 Gateway，`ANTHROPIC_AUTH_TOKEN` 用 Mac 專用的 Gateway key。原本的 `claude` 不動，要退回就改回用 `claude`。
   - opencode：在使用者設定加一個自訂 provider（`baseURL` 指向 Gateway，key 從環境變數讀），列出各上游的模型。
4. **每個客戶端一把 Gateway key**：v0 只發 Mac 這一把，key 上帶 metadata（客戶端、用途）。之後 worker、手機各自另發，不共用。
5. **用量**：以 Gateway 的用量紀錄為準，可以依客戶端、訂閱、模型、期間查詢。
6. **切換留下紀錄**：Claude 兩把之間的分攤與 429 冷卻都在 Gateway 內發生，每一次都寫進紀錄，查得到是哪一把服務了哪個請求。

## v0 不做

| 項目 | 進不進 v0 | 理由 |
|---|---|---|
| #2 多訂閱無縫切換 | **部分進** | Claude 兩把的分攤與 429 冷卻在 v0 做（LiteLLM 本來就有）。跨供應商的自動切換（例如 Claude 用完改用 Ollama）不做，因為那會換掉模型，要等 #4 |
| #3 免費模型依效能自動切換 | 不進 | blocked by #4 |
| #4 隱私界線 | 不進 | v0 只送到客戶端指定的模型，不自動改送公開／免費模型，所以不會因此多出外洩面 |
| #7 手機 app 的入口 | **部分進** | ① OpenAI 相容的 `/v1/chat/completions`、`/v1/models` 是 LiteLLM 本來就有的，v0 就有。② 外部入口與 ③ app 專用 key 不進：外部入口要決定開公開入口還是走 localproxy（MyAiEntry#31），屬於安全面的決定 |
| #6 和 sidecar 對齊 | **介面進、實作不進** | v0 定出 sidecar `ModelBroker` 要的租用 API：`request_lease`（worker、角色、coding agent、模型）回傳 Gateway 網址、一把帶效期的 Gateway key 與效期；`release` 撤銷那把 key。sidecar 的假 broker 照這個形狀做。真的實作等 Mac 驗收過，而且 worker 容器連得到 Gateway 之後（要改 MyLinuxPool 的網路）再做 |
| Antigravity（agy） | 不進 | agy 只有在 Gemini API key 模式下才能換端點，而且要 Gemini 相容的形狀 |
| 串流以外的特殊功能 | 不保證 | Claude Code 經閘道時 Remote Control 不能用，MCP tool search 預設關閉，訂閱額度用完的等待畫面也沒有。這些在 v0 視為已知限制 |

## 和期 1 設計的差別（要本人確認）

- **決定 1「只做控制面」→ v0 加上資料面。** 租約改成「發一把帶效期、帶 metadata 的 Gateway key」，用量改以 Gateway 的紀錄為準（比各 agent 自己回報準）。
- **決定 2「上游憑證值不經過 LLMGateway」→ v0 由 Gateway 持有上游憑證。** 這是「接上訂閱」的前提。客戶端（Mac、之後的 worker、手機）只拿 Gateway key，拿不到上游憑證。風險集中在 Gateway 那台機器，所以 Gateway 只綁 loopback、只經 SSH 跳板連入，憑證檔 0600。
- **Claude 條款**：Claude Code 法務頁不允許第三方開發者用 Free／Pro／Max 憑證「代使用者」路由請求。v0 是本人自己的 Gateway，只服務本人自己的 agent，和「代使用者」不同；但官方沒有對這種用法正面表態，而且公司訂閱走的是商業條款。請本人知悉後決定照做。調查見 `docs/research/claude-failover.md` §4。
- 期 1 的 OpenSpec change（已 merge）在 v0 的 design 裡以 MODIFIED 的方式改，不另外推翻重寫。

## 驗收

1. Mac 上 `claude-gw -p "只回 OK"` 回 OK；Gateway 紀錄有這筆，標著客戶端＝Mac、訂閱＝Claude 兩把之一。
2. `opencode run -m <gateway provider>/<模型> "只回 OK"` 對每個上游各成功一次：Claude、Ollama Cloud、OpenRouter、OpenCode Zen。
3. 讓 Claude 的其中一把回 429（用假上游或測試設定，不真的燒額度），下一個請求改由另一把服務，紀錄看得到冷卻與切換。
4. 依客戶端、訂閱查得到用量合計。
5. repo、紀錄、log 掃描不到任何憑證值。
6. 退回：Mac 改回用原本的 `claude`、opencode 改回原本的 provider，就跟以前一樣能用。

## Impact

- 新的 OpenSpec change（本檔）：補 design、specs（MODIFIED `agent-lease`、`usage-record`，ADDED gateway 的資料面），以及 tasks。
- MyLinuxPool：Gateway 要跑在哪台 provider、怎麼裝。可能要做成一個 shared-config 單元，另開 MyLinuxPool 的工單。
- MyAiStorage#31：sidecar 的 `ModelBroker` 照 v0 的租用 API 形狀做假 broker。
- Mac：多一個包裝指令與一段 opencode 設定。

## 要本人決定

1. Gateway 用 LiteLLM（推薦）還是 sub2api。
2. 知悉「v0 由 Gateway 持有上游憑證，包含 Claude 兩把訂閱 token」與上面的條款說明，照做。
3. 跑在哪：池內常駐的那台 provider（推薦），不放在對外的跳板機上。
