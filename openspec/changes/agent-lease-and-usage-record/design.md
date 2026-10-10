## Context

動機見 proposal.md。這裡只列影響做法的現況與限制（出處都在 `docs/research/`）：

- **憑證現況**：期 1 用 MyLinuxPool 的能力把 LLM 憑證唯讀掛進 worker（MyLinuxPool#46／#50），預設 profile 全給。Claude 用訂閱的長效 token（`CLAUDE_CODE_OAUTH_TOKEN`，個人為主、公司備援），同一台機器不放 API key。
- **sidecar 現況**：MyAiStorage#31 還在設計；已定的是「啟動用 `agora continue`、匯出用 `agora import`、AI team 的啟動經過 sidecar」。登錄方式、暫停語意、headless 啟動都還沒定（`sidecar-angareion.md` §1、§4）。
- **Angareion**：角色信箱，收件人是 repo × 職務；叫醒與投遞由 sidecar 負責（MyAiStorage#30）。
- **Claude 條款**：Claude Code 法務頁不允許第三方以 Free／Pro／Max 憑證代使用者路由請求，也不允許代為收集、儲存、中介這類憑證（`claude-failover.md` §4.2）。「本人自己的兩把、自己的機器」是否落在這條之外，官方沒有正面說法。
- **既有閘道**：sub2api 的 key 沒有 metadata，做不到 worker／角色／agent 歸因，且 README 自承可能違反上游條款（`sub2api.md` §3、§6.3）。LiteLLM Proxy 是唯一現成又有 key metadata 與用量匯出的閘道（`gateway-alternatives.md`）。
- **agent 的旋鈕**：Claude Code 可用 `ANTHROPIC_BASE_URL`／憑證變數改道，用 OpenTelemetry 匯出用量並可加資源屬性；opencode 每個 provider 可設 `baseURL`，用量存在本機（`opencode stats`）（`agent-endpoints.md`）。
- **Claude 額度訊號**：hooks 的 `StopFailure`（rate_limit）、`-p` 的非零 exit code 與 `api_retry` 事件、statusline 的 5h／7d 用量；session 可用 `--resume` 接續（`claude-failover.md` §1、§3.1）。

## Goals / Non-Goals

**Goals**
- 讓「開關申請與紀錄」在期 1 就能接上 sidecar 與 Mac，而且不需要先蓋一個代理所有請求的服務。
- 既有的上游訂閱與供應商憑證值永遠不經過 LLMGateway（第 3 階段的例外見決定 5）。
- 從「能力寫死」走到「照租約發放」的每一步都能單獨開關、可以退回。

**Non-Goals**
- 不設計資料面的閘道（請求轉送、跨供應商調度、統一配額）；只定義「何時需要它」的條件（決定 6）。
- 不設計 sidecar 本身的登錄與投遞；只定義 sidecar 對 LLMGateway 的介面。
- 不設計 #2、#3、#4 的內容。

## Decisions

### 決定 1：LLMGateway 期 1 是「控制面」，不是「資料面」

LLMGateway 只做三件事：依政策發租約、記錄租約事件、彙整用量。模型請求照舊由 agent 直接打上游。

- **為什麼**：(1) Claude 條款不允許中介訂閱憑證，集中代管兩把訂閱 token 的風險最大；(2) 「哪台 worker、什麼角色」這些資訊本來就在 launcher 手上，不需要從請求流量裡反推；(3) LLMGateway 要跑在哪台機器還沒定（見 MyPMO#4），控制面可以先用很輕的後端。
- **放棄的選項**：
  - sub2api：key 沒有 metadata、條款風險明寫在 README、0.x 版快速變動。
  - LiteLLM Proxy 當期 1 核心：key metadata 與用量匯出最完整，但等於把 Claude 訂閱 token 集中到一個服務；而且還要多維運 Postgres。留作決定 6 的升級選項。
  - new-api／one-api：token 沒有 metadata；new-api 是 AGPL-3.0；one-api 已停滯。

### 決定 2：launcher 申請、注入、回報；LLMGateway 不碰憑證值

| 誰 | 做什麼 |
|---|---|
| launcher（worker：sidecar；Mac：啟動包裝） | 申請租約 → 從本機憑證來源取出租約指定的那一把 → 只注入那一把 → 啟動 agent（worker 用 `agora continue`，Mac 用 herdr）→ 回報 started／stopped／切換 → 結束時附上用量 |
| LLMGateway | 驗欄位、比政策、發或拒租約、記事件、逾期處理、彙整用量、回答查詢 |
| 本機憑證來源 | worker：MyLinuxPool 能力的唯讀掛載；Mac：本人既有的登入或環境 |

- 租約只以「來源識別鍵」指出憑證（例如 `claude-personal` 對應某個能力掛載），不帶值。launcher 取出後要換成 agent 實際讀取的變數名再注入，例如 Claude Code 一律是 `CLAUDE_CODE_OAUTH_TOKEN`。
- **為什麼**：sidecar 本來就負責啟動 agent（MyAiStorage#31），把注入也放在同一處，憑證就不用多跑一趟網路；LLMGateway 被攻破時也拿不到任何上游憑證。
- **放棄的選項**：LLMGateway 發放憑證值（例如每次啟動時把 token 傳給 launcher）。這樣等於多一個集中保管點，留到決定 5 的第 3 階段，而且只用在能發短效、可撤銷 key 的供應商。

### 決定 3：租約的介面是一支 CLI，後端可以替換；v0 後端是只追加的私有紀錄

- 介面：`lgw lease request|start|stop|switch|renew|revoke`、`lgw ls`、`lgw usage`。launcher 只呼叫 CLI，不直接碰後端。
- v0 後端：private GitHub repo 裡只追加的事件紀錄，和 Angareion v0 同一種做法（ETag 輪詢、不吃額度、不必自架服務）。政策檔放在同一個 repo，由本人改。
- 有效期限：預設 12 小時，launcher 每小時續期一次。
- **逾期採延後結算**：沒有常駐服務，所以不在到期當下記錄。任何一次 CLI 往來（申請、查詢、續期、回報）都先把已過期的 active 租約補記為 expired，再處理這次往來。`lgw ls` 因此一定看不到已過期的租約。
- **事件順序與去重**：每筆事件帶單調序號，先後以序號為準，時刻只作註記，不受各機器時鐘誤差影響。去重鍵是「租約 id＋動作＋launcher 產生的請求 id」，重送不會多出事件。
- **本機待送佇列**：start、stop、switch 與用量回報失敗時，先存在 launcher 本機的佇列，恢復後依原順序補送（沿用 agora `pending` 的做法）。
- **為什麼**：期 1 沒有常駐主機；Angareion 已經證明這種後端夠用；介面固定以後，後端換成 HTTP 服務不影響 launcher。
- **放棄的選項**：
  - 現在就做 HTTP 服務：要先決定主機與可用性。
  - 直接把租約當 Angareion 訊息：Angareion 是對話管道，要查「現在有哪些 active 租約」得重播整個話題；而且叫醒邏輯綁在 sidecar 上，LLMGateway 不該依賴它。

### 決定 4：Claude 個人／公司的切換由 launcher 做，並且留下紀錄、發出通知

流程：

1. 偵測額度訊號：Claude Code 的 `StopFailure`（rate_limit）hook，或 `-p` 模式非零結束且輸出帶額度錯誤。
2. 安全結束原 agent：先送 SIGINT 讓這一輪收尾，再等 agora 寫回 session。這一步依賴 sidecar 的終止語意（MyAiStorage#31）。
3. `lgw lease switch`：原租約記為 superseded（理由代碼、恢復時刻），同時申請政策裡的下一個訂閱。
4. 只換注入的那一把，以 `agora continue`（Mac 上用 `claude --resume`）接續原 session。
5. 透過 Angareion 通知 PMO。通知帶新舊租約 id；送出後，lgw 的事件記下 Angareion 的訊息 id，兩邊可以互查。通知送不出去時不回滾切換，留在本機佇列重試。

下一次新開的租約仍然從個人開始，不會因為上次切到公司就一直用公司。

- **為什麼**：MyLinuxPool#50 要求切換時要留下紀錄並發出通知。在 launcher 切換時，每次換帳號都留得下事件，也發得出通知；官方的接續機制也完整。
- **放棄的選項**：
  - 閘道在中間換帳號：閘道端也能留紀錄，「不安靜」做得到；不採用是因為要把兩把訂閱 token 集中在閘道（條款風險最大），而且要多維運一個服務（決定 1）。
  - `apiKeyHelper` 動態回傳：官方的定位是產生 API 憑證，拿來回傳訂閱 token 沒有文件支持；而且它的優先順序會蓋過訂閱 token。

### 決定 5：從能力寫死遷移的四個階段

| 階段 | 憑證從哪來 | LLMGateway 做什麼 | 關掉後回到 |
|---|---|---|---|
| 0（現在） | 能力全掛、agent 自己讀 | 無 | — |
| 1 只記錄 | 同上 | launcher 申請租約並回報事件；申請失敗也照樣啟動，只發警告 | 0 |
| 2 照租約注入 | 能力全掛，但 launcher 只注入租約指定的那一把 | 沒有租約就不啟動；切換照決定 4 | 1 |
| 3 照租約發放 | 能用 API 發放與刪除 key 的供應商（OpenRouter）改由租約當下發一把有額度上限的 key；Claude 訂閱 token 仍走掛載 | 發 key；在 released、expired、revoked、superseded 時刪除（OpenRouter 發出的 key 沒有到期參數，截至 2026-10-10，所以「短效」靠租約結束時刪除） | 2 |

- 每個階段都用 launcher 的一個設定切換，不改 agent。
- 第 2 階段完成後，MyLinuxPool 的能力可以從「全給」縮成「照政策給」（期 2，依部門）。
- **第 3 階段是「憑證值不經過 LLMGateway」的唯一例外**：發出的短效 key 必然經過發放者。發放與刪除要用 OpenRouter 的 Management API key，這把 key 是帳號層級、不分範圍的，外洩後可以建立、停用或刪除帳號內所有 key（`gateway-alternatives.md` §5.5）。因此第 3 階段另開工單，開工前要本人裁定這把 key 由誰保管：放在控制面，或放在 launcher、由 LLMGateway 只下指令。

### 決定 6：什麼時候才上資料面閘道

符合以下任兩項，就另開工單評估「在控制面之外加 LiteLLM Proxy」：

- 同一把上游 key 要給多台 worker 共用，又必須分別限額或停用。
- 需要跨供應商自動 fallback（例如 #2 的無縫切換）。
- 需要以請求為單位擋下內容（例如 #4 的隱私界線要在送出前檢查）。
- agent 端回報的用量長期對不上供應商帳單。

加上去以後，租約 id 透過 `ANTHROPIC_CUSTOM_HEADERS`／opencode 的 `options.headers` 帶給閘道，對應到閘道的 metadata；Claude 訂閱流量能不能經過閘道，要先重新確認條款。

### 決定 7：用量回報的管道

- Claude Code：啟用 OpenTelemetry，用 `OTEL_RESOURCE_ATTRIBUTES` 帶租約 id、worker、角色。v0 不架 collector：launcher 讓 Claude Code 用 `OTEL_METRICS_EXPORTER=prometheus` 在本機開指標端點，租約結束前抓一次，把 token 與成本合計附在結束事件。需要集中觀測時再改成 OTLP 送到 collector。OTel 的設定要放在 launcher 給的環境或使用者 settings，不能放專案的 `.claude/settings.json`（Claude Code 會忽略）。
- opencode：`opencode stats` 是彙總，不能指定單一 session，所以啟動時記下 session id，release 時用 `opencode export <sessionID>` 取該 session 的 token 與成本，附在 released 事件。
- 供應商端（OpenRouter 的 `/api/v1/key`、`/generation`）：只用來對帳，不當主紀錄。
- Ollama Cloud、OpenCode Zen：截至 2026-10-10 查無公開的用量 API，只靠 agent 端回報。
- **為什麼**：控制面看不到流量，只能讓 agent 端回報；以租約 id 為鍵，彙總時才對得起來。

## Risks / Trade-offs

- [agent 端回報可以被關掉或漏掉] → 租約事件仍記下「誰在何時用了什麼」；對不上的用量列為未歸因，長期對不上就觸發決定 6。
- [launcher 自己沒申請就直接開 agent] → 第 2 階段起能力不再讓 agent 自己讀，只有 launcher 讀得到；Mac 上靠本人的使用習慣（啟動包裝），不能強制。
- [同時用個人和公司兩把 Claude 訂閱給自己的 agent，可能有條款與公司政策問題] → 要本人確認（公司訂閱的合約與內部政策）；確認前，政策檔可以先把 `claude-company` 設為只在切換時使用。
- [v0 後端是 GitHub repo，有寫入次數上限] → 事件只在狀態變化與每小時續期時寫入；即使有十個 agent，每小時也遠低於上限。
- [sidecar 的 headless 啟動還沒定] → 本設計只要求 sidecar 能在啟動前與結束後各呼叫一次 CLI，不依賴 headless 的細節。
- [v0 用 Prometheus 匯出時，Claude Code 的本機指標端點固定在 9464 埠，同一台機器同時有兩個 Claude Code session 會互撞] → v0 假設每台機器同時只有一個 Claude Code session 匯出指標，其餘 session 的 Claude 用量列為未歸因；sidecar 開多個 Claude session 成為常態時，改用 OTLP 送到 collector。
- [切換要先安全結束 agent，依賴 sidecar 的終止語意] → 列為和 MyAiStorage#31 對齊的項目（tasks 1.1）。
- [角色的格式依賴 Angareion 的身分表示（MyAiStorage#30 待設計）] → 規格先把角色當不透明字串，定案後再對齊。

## Migration Plan

1. 本設計經本人看過後，和 MyAiStorage#31 的設計對齊 launcher 介面（決定 2、3）。
2. 實作順序：CLI 與 v0 後端 → Mac 啟動包裝（第 1 階段，先在 Mac 上跑）→ sidecar 接上（第 1 階段）→ 第 2 階段 → 第 3 階段（只對 OpenRouter）。
3. 每一階段都能用 launcher 的設定退回上一階段（見決定 5 的表）。

## Open Questions

- v0 後端用哪個 private repo（另開一個，還是和 Angareion 的信道 repo 共用），以及寫入它的 token 的範圍與發放方式（比照 Angareion 的 token 設計）：不影響介面，實作時再定。
- OpenTelemetry collector 放哪：v0 不需要，等第 2 階段再定。
