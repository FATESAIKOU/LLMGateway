# Design review：`agent-lease-and-usage-record`（對應工單 #1）

> 審查者：LLMGateway 團隊 reviewer（工作 session）。日期：2026-10-10。
> 審查對象：`openspec/changes/agent-lease-and-usage-record/`（proposal.md、design.md、specs/agent-lease/spec.md、specs/usage-record/spec.md、tasks.md；README.md、`.openspec.yaml`、`openspec/config.yaml`）。
> 方法：逐條對照 `docs/research/`（sub2api、gateway-alternatives、agent-endpoints、claude-failover、sidecar-angareion；前四份已經二輪查證，見 `review-notes.md`）與官方文件；交叉檢查本 repo 工單 #1–#4（已確認存在，標題相符）；密鑰／私人資訊掃描。
> 規則：只讀、未改任何 change 檔、未 commit。
> 嚴重度：**必修**＝不修會讓規格矛盾或不可實作；**建議**＝可留待對齊／後續工單，但建議現在標記。

## 0. 結論

- 7 個決定**都有 `docs/research/` 根據**，與調查結果大致一致；沒有發現條款、優先序、切換訊號的**事實錯誤**。
- **必修 2 條**：(1) 「憑證值永遠不經過 LLMGateway」與第 3 階段「由租約發 key」自相矛盾，且 OpenRouter 發放需要一把高權限管理 key，持有人未定；(2) 逾期（expired）／續期（renew）在「無常駐後端」下沒有機制，spec 的驗證情境無從成立。
- **建議 11 條**：集中在切換理由的措辭、sidecar 依賴、名詞／狀態對映、通知對齊、v0 用量落地方式、public repo 引句規矩。
- public repo 規矩：**通過**（無金鑰值、無 email／PII；僅有變數名與佔位符）；惟私有工單的短句引用建議再確認（見建議 13）。
- OpenSpec 格式：符合 `openspec/config.yaml` 的 rules（proposal 開頭有工單、有「不做」；design 每決定有「為什麼」與「放棄的選項」）；兩份 spec 用 `## ADDED Requirements`＋Requirement／Scenario，`SHALL／MUST` 使用正確。

## 1. 必修

### M1. 「憑證值永遠不經過 LLMGateway」與第 3 階段矛盾；OpenRouter 管理 key 未定持有人
- **位置**：proposal.md「What Changes」第 1–2 點與「不做」對照；design.md Goals（「憑證值永遠不經過 LLMGateway」）、決定 1、決定 2、決定 5 第 3 階段。
- **問題**：決定 5 第 3 階段「照租約發放」＝由租約當下發一把短效 OpenRouter key、release 時撤銷——key 的值必然經過發放者（否則怎麼交給 launcher），這與「憑證值永遠不經過」「不持有、不轉送憑證值」的絕對句直接衝突。另外，發放與撤銷需要一把 OpenRouter **Management API key**（官方名詞；文件寫的 “provisioning key” 非官方頁面用語），而官方明說它**account 層、unscoped**、可建立／編輯／刪除帳號內所有 key、且外洩風險高——design 完全沒說這把放哪、誰持有。
- **建議修正**：把絕對句改成「**既有的**訂閱／供應商憑證值永不經過 LLMGateway；第 3 階段由控制面簽發的短效 key 除外」，並在決定 5 或 Open Questions 指定 Management key 的持有者（控制面保管並接受這是唯一的憑證例外，或由 launcher 持有、LLMGateway 只下指令）。名詞改用官方「Management API Keys」，並補上出處到 `docs/research/`（目前沒有任何調查檔涵蓋 key 發放 API）。
- **出處**：<https://openrouter.ai/docs/features/provisioning-api-keys>（“Management API keys … unscoped: one key covers administrative actions across all of the account's workspaces”；“A leaked management key could create, edit and delete the API keys in your account”；“Management keys cannot be used to make API calls”）。

### M2. 逾期與續期在「無常駐後端」下沒有機制，spec 情境無從驗證
- **位置**：specs/agent-lease/spec.md「租約狀態與事件」（Scenario: launcher 沒回報結束）；design.md 決定 3（v0 後端是不必自架服務的只追加紀錄、ETag 輪詢）、決定 4（每小時續期）、tasks.md 2.3。
- **問題**：「active 租約超過有效期限且 launcher 沒有續期 → LLMGateway 將租約記為 expired」需要有人在「到期當下」判定；但 v0 沒有常駐服務（決定 3 說明了為什麼不做）。誰、何時寫下 expired？同樣地，`renew`（每小時續期）如何改寫有效期限、要不要留事件，兩份 spec 都沒有寫（design 只在 Risks 提「每小時續期時寫入」）。以現在的架構，實作者只能猜，測試者無法驗證 Scenario。
- **建議修正**（二選一，建議第一種）：(a) 明訂 **lazy 結算**——任何 CLI 讀寫（request／ls／usage／renew…）時，先把已過期的 active 租約補記為 expired，並在 spec 寫「在下一次與 LLMGateway 往來時記為 expired」；(b) 用排程（如 GitHub Action）定期結算，並把「誰跑排程」寫成依賴。另把 `renew` 的事件與「有效期限延長」寫進規格。
- **出處**：設計內部矛盾（決定 3 vs spec Scenario）；同類問題在 MyAiStorage 已有先例與解法（`pending` 的補存機制），見 <https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L146>。

## 2. 建議

### D1. 「憑證來源名稱」的範例會誤導 launcher 實作
- **位置**：design.md 決定 2（「環境變數名稱 `CLAUDE_CODE_OAUTH_TOKEN_PERSONAL`」）。
- **問題**：Claude Code 只讀固定變數名 `CLAUDE_CODE_OAUTH_TOKEN`；`…_PERSONAL` 不會被讀。寫成「環境變數名稱」容易被照抄成真的注入變數。
- **建議**：註明來源名稱是 lgw 內部的識別鍵，launcher 取用後必須映射成 agent 真正讀的變數名（worker：`agora continue` 的啟動環境）。
- **出處**：agent-endpoints.md §1.2（`CLAUDE_CODE_OAUTH_TOKEN` 為載體）、claude-failover.md §3.1。

### D2. 決定 4 對「閘道換帳」的否決理由與 claude-failover.md 不一致
- **位置**：design.md 決定 4「放棄的選項」第一條（「對 agent 完全看不出來，正好違反『不能安靜』的要求」）。
- **問題**：claude-failover.md §3.2 明說閘道端可記 access log／換帳事件、也能把 `anthropic-ratelimit-unified-*` 原值轉發讓 Claude Code 端看到「當下這把」的狀態；所以「不能安靜」可以滿足，只是紀錄落在閘道端。真正的否決理由應是「憑證集中＋條款風險＋多維運一個服務」（這幾點 design 其他地方都寫對了）。
- **建議**：改寫理由；並確認 MyLinuxPool#50「不能安靜」的準確語意（系統要留紀錄，還是 agent 端必須看得見）——若是前者，決策不變但理由要換。
- **出處**：claude-failover.md §3.2「『不安靜』怎麼做」、§5.3；<https://code.claude.com/docs/en/llm-gateway-protocol#response-headers>。

### D3. 切換流程依賴 sidecar 的「終止」語意，但沒有列成依賴
- **位置**：design.md 決定 4 流程第 3 步；Risks（只提了 headless 啟動）。
- **問題**：換 token 必須結束現有 agent process、等 agora 收尾（session 寫回／`pending`），再用新憑證 `agora continue` 接續。sidecar 的「終止／暫停」語意未定案（sidecar-angareion.md §4 A2），而官方建議停止前先送 SIGINT 讓 turn 收尾（claude-failover.md §3.1）。
- **建議**：Open Questions 增列「切換依賴 sidecar 的安全終止與 agora 收尾完成」；tasks 1.1 對齊項目加上「終止後才以新租約重啟」。
- **出處**：sidecar-angareion.md §1.2、§4（A2）；claude-failover.md §3.1；<https://code.claude.com/docs/en/headless#stop-a-run-with-sigterm>。

### D4. opencode 用量抓取方式：`stats` 無法指定單一 session
- **位置**：design.md 決定 7 第一點、specs/usage-record/spec.md「agent 端回報帶租約 id」。
- **問題**：`opencode stats` 是聚合（`--days/--models/--project/--tools`），不能指名某一個 session；單一 session 的 token 與成本要用 `opencode export <sessionID>`（或直接查本機 DB）。
- **建議**：把「跑 `opencode stats` 或 `opencode export`」改成以 `export <sessionID>`（或 db 查詢）為 release 時的擷取來源。
- **出處**：agent-endpoints.md §2.3、§5.3（<https://opencode.ai/docs/cli/>）。

### D5. Claude Code OTel 在 v0 沒有接收端，「每筆都帶租約 id」無法成立
- **位置**：design.md 決定 7（「v0 可以只在 release 時從本機彙總，collector 留到需要時再做」）、specs/usage-record/spec.md Requirement「agent 端回報帶租約 id」。
- **問題**：OTel 一定要有 exporter 目的地（`otlp`／`prometheus`／`console`）；沒有 collector 或本機端點，就沒有可彙總的資料——第 1 階段的用量情境將無從驗證。
- **建議**：明定 v0 做法，例如用 `OTEL_METRICS_EXPORTER=prometheus`（本機 9464，由 launcher 在 release 時抓）或 `console` 輸出由 launcher 截取；或明說「第 1 階段只保證租約事件，Claude 端用量等 collector 上線」並據此調整 spec 用語。
- **出處**：agent-endpoints.md §1.5（<https://code.claude.com/docs/en/monitoring-usage>：exporter 選項與專案 settings 限制）。

### D6. lgw 後端（private GitHub repo）的 token 範圍與發放未提
- **位置**：design.md 決定 3；tasks 1.3。
- **問題**：Angareion #30 已把「token 的範圍（只能讀寫信道 repo 的 issue）」列為待設計；lgw 的 v0 後端是另一個（或共用）private repo，同樣需要一把 token，設計完全沒提。
- **建議**：Open Questions 增列「lgw 後端 repo 與 token 的發放／範圍」，或明說與 Angareion 共用同一套 token 策略。
- **出處**：sidecar-angareion.md §2.2、§4（B10）。

### D7. 與 Angareion 的「互相引用」未定
- **位置**：design.md 決定 4 流程第 4 步（通知走 Angareion）；specs/agent-lease/spec.md 切換 Requirement。
- **問題**：sidecar-angareion.md §4（C17）要求決定「稽核落點與互相引用」；本設計把權威紀錄放 lgw 事件、通知放 Angareion，但沒定兩邊怎麼互指。
- **建議**：切換通知內容帶租約 id；lgw 事件可記 Angareion 訊息 id／工單 refs（OKF `refs` 的既有慣例）。
- **出處**：sidecar-angareion.md §4（C17）、§2.3；<https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L58>。

### D8. 「角色與 Angareion 一致」依賴 #30 尚未定案的身分表示
- **位置**：specs/agent-lease/spec.md 租約欄位（「角色（repo × 職務，與 Angareion 的角色表示一致）」）。
- **問題**：#30 明說「身分怎麼表示」是要設計的題目之一（sidecar-angareion.md §2.2）；現在把「一致」寫進規格，等於把未定案當已定案。
- **建議**：規格保留 `role` 為不透明字串、註明格式待 #30 定案後對齊；或在 Open Questions 連到 #30。
- **出處**：sidecar-angareion.md §2.2。

### D9. 動詞／狀態／事件對映不完整（含 revoke 路徑與 renewal 事件）
- **位置**：specs/agent-lease/spec.md「租約狀態與事件」＋Scenario（回報 `stopped`）；design.md 決定 3 的 CLI（`request|start|stop|switch|renew`）；tasks 2.3。
- **問題**：狀態清單是 `released／expired／revoked／superseded`，Scenario 卻說「回報 stopped」；`renew` 與 `revoke` 沒有對應的事件規格；「本人或 PMO 撤銷」也沒有操作路徑（CLI 沒有 revoke）。
- **建議**：在 spec 或 design 放一張「CLI 動詞 → 事件 → 狀態」對映表（start→active、stop→released、switch→superseded＋新租約、renew→有效期限延長事件、revoke→revoked、逾期→expired）。
- **出處**：設計內部一致性（規格審查）。

### D10. 「政策外拒絕」的通知：proposal 與 spec 不一致
- **位置**：proposal.md「What Changes」第 4 點（「切換與**拒絕**的通知走 Angareion」）vs specs/agent-lease/spec.md（只有切換與備援用完要通知）。
- **建議**：二者取一——要嘛在 spec 加「denied 需通知」情境，要嘛把 proposal 的「拒絕」拿掉。
- **出處**：設計內部一致性。

### D11. 政策預設「等同預設 profile 全給」不可驗證
- **位置**：specs/agent-lease/spec.md「依政策核准」。
- **問題**：「全給」是什麼集合，取決於 MyLinuxPool 的能力現況（private，本調查未讀）；規格本身無法驗證。
- **建議**：改以「政策檔內容即為允許集合」為權威（初始政策檔＝當時既有的組合），或列出初始允許清單。
- **出處**：設計內部可驗證性。

### D12. 私有工單短句引用（public repo 規矩）
- **位置**：proposal.md（MyLinuxPool#50）；design.md Context（MyPMO#4「池內，細節在啟動時定」）、決定 4。
- **問題**：本 repo 的規矩是「不寫 MyBrain 的內容與私人資訊；需要時**連到** MyPMO 的工單」；引用私有工單的原文短句雖非憑證或個資，仍建議確認是否符合規矩的範圍。
- **建議**：原則改成轉述＋連號（例如「依 MyLinuxPool#50 的要求，切換必須留下紀錄與通知」），必要時把原句留在私有工單。
- **出處**：本 repo `AGENTS.md`（工單規則摘要）。

## 3. 查核屬實（正面清單）

- **每決定都有根據**：決定 1（條款＋sub2api／gateway-alternatives 比較）、決定 2（sidecar #31＋agent-endpoints 的注入旋鈕）、決定 3（Angareion #30 的 ETag／只追加後端）、決定 4（`StopFailure`／`api_retry`／exit code 與優先序；claude-failover.md §1、§3.1）、決定 5（階段與 MyLinuxPool 能力）、決定 6（gateway-alternatives.md 的升級條件與 CUSTOM_HEADERS 介面）、決定 7（OTel／opencode／OpenRouter 的來源）。
- **引用的調查結論與原文相符**：Claude 條款段（claude-failover.md §4.2）逐字核對無誤；`StopFailure` 只有通知能力、不能否決（同 §1.4a）；`CLAUDE_CODE_OAUTH_TOKEN` 每 session 重讀、可重啟換 token（§3.1）；「閘道憑證才換計費、只設 BASE_URL 不換帳」（agent-endpoints.md §1.1）；`ANTHROPIC_CUSTOM_HEADERS`／opencode `options.headers` 可帶租約 id（§1.4、§2.1）。
- **工單交叉引用**：本 repo #1–#4 存在且標題與引用相符（#1 設計、#2 多訂閱無縫切換、#3 免費模型自動切換、#4 隱私界線）。
- **格式與規矩**：符合 `openspec/config.yaml` 的 rules；spec 使用 `## ADDED Requirements`／Requirement／Scenario，`SHALL`／`MUST` 用語正確；tasks 明示「期 1 只做設計、開工另開工單」。
- **密鑰／私人資訊掃描**：`openspec/` 全部檔案 0 命中（無 token／key 值、無 email／電話；只有變數名與狀態名）。

## 4. 範圍與限制

- MyLinuxPool#46／#50、MyPMO#4 是 private repo，本次**未讀**；相關陳述（能力全給現況、裁定的原文）只能照 proposal／design 的轉述，無法獨立查證。
- `docs/research/` 的五份調查已經二輪查證（`review-notes.md` 六～八節）；本審查引用的是查證後的版本。

## 複查（PR #5）

> 複查者：LLMGateway 團隊 reviewer。日期：2026-10-10。
> 對象：PR #5 `design/agent-lease-and-usage`，commit `95c9748`（2026-10-10 17:56:52 +0900）；工作樹乾淨、與 origin 同步。
> 方法：對照原審查 M1、M2、D1–D12 逐條核對修法；檢查是否引入新矛盾；對照 test 的 `spec-testability.md`（MS-1–MS-12）確認哪些進 spec、哪些留在實作；`openspec validate agent-lease-and-usage-record --strict` 實測通過。
> 結論：**M2、D1–D12 全部處理**；M1 主體已處理，僅剩「Management API key 出處補進 `docs/research/`」未做。修法引入 **1 條狀態機矛盾（N1）** 與 5 條小殘留（N2–N6，皆建議）。

### 一、原審查項目的處理狀態

| 項目 | 狀態 | 說明 |
|---|---|---|
| M1 憑證例外＋Management key | ✅ 主體處理 | Goals 改成「既有的…（第 3 階段的例外見決定 5）」；決定 5 新增例外段落並把「誰保管 Management API key」交給本人裁定（tasks 5.1）；proposal 同步改「不持有、不轉送**既有的**上游憑證值」。**殘留**：Management API key 的關鍵事實（帳號層級、unscoped、可建刪所有 key）仍沒有 `docs/research/` 出處（見 N5）。 |
| M2 逾期／續期機制 | ✅ 處理 | 決定 3 新增「延後結算」（任何 CLI 往來先補記 expired）；spec 改寫「launcher 沒回報結束」為下次往來時結算、新增「續期」情境、CLI 加 `revoke` 並補「撤銷」「對已結束的租約續期→拒絕」「重複回報→恰一筆」情境、新增「回報暫時失敗時保存後補送」Requirement；去重鍵（租約 id＋動作＋請求 id）與單調序號寫進決定 3。 |
| D1 來源名稱≠變數名 | ✅ 處理 | 決定 2 改成「來源識別鍵」並明說 launcher 要換成 agent 實際讀的變數名（Claude Code 一律是 `CLAUDE_CODE_OAUTH_TOKEN`）；spec「只注入一把」情境同步。 |
| D2 閘道否決理由 | ✅ 處理 | 決定 4 放棄的選項改寫：「閘道端也能留紀錄，『不安靜』做得到；不採用是因為要把兩把訂閱 token 集中在閘道，而且要多維運一個服務」；#50 的要求改為轉述（留紀錄＋通知）。 |
| D3 終止依賴 | ✅ 處理 | 決定 4 流程第 2 步加入「先送 SIGINT、等 agora 寫回；依賴 sidecar 的終止語意」；Risks 增列同項並指向 tasks 1.1。 |
| D4 opencode 用量來源 | ✅ 處理 | 決定 7 改「`stats` 是彙總，不能指定單一 session；啟動時記 session id，release 時 `opencode export <sessionID>`」；spec 同步；tasks 3.3 同步。 |
| D5 OTel 在 v0 的落地 | ✅ 處理（引入 N3） | 決定 7 改「v0 不架 collector：launcher 讓 Claude Code 用 `OTEL_METRICS_EXPORTER=prometheus`，結束前抓一次」；並註明 OTel 設定不能放專案 `.claude/settings.json`。見 N3 的併發限制。 |
| D6 lgw 後端 token | ✅ 處理 | Open Questions 增列「寫入它的 token 的範圍與發放方式（比照 Angareion 的 token 設計）」。 |
| D7 與 Angareion 互指 | ✅ 處理 | 決定 4 流程第 5 步：通知帶新舊租約 id、lgw 事件記 Angareion 訊息 id、通知失敗不回滾並留本機重試；spec 新增「通知管道失效」情境。也一併回應了 test 的 MS-9。 |
| D8 角色格式依賴 #30 | ✅ 處理 | spec 欄位改「角色（不透明字串；格式待 Angareion 的身分表示定案後對齊）」；Risks 增列同項。 |
| D9 動詞／狀態／事件對映 | ✅ 處理 | spec 明列 start→active、stop→released、switch→superseded＋新租約、renew→續期事件（狀態不變）、revoke→revoked；revoke 進入 CLI 與 tasks 2.1／2.3。 |
| D10 拒絕通知不一致 | ✅ 處理 | proposal Impact 改成「切換與額度用完的通知走 Angareion」，與 spec 的通知範圍一致。 |
| D11 政策預設可驗證性 | ✅ 處理 | spec 改「政策檔的內容就是允許集合的權威；期 1 的初始政策檔 SHALL 列入當時能力已提供的全部組合」。 |
| D12 私有工單引句 | ✅ 處理 | 決定 1 的 MyPMO#4 引句已移除（只留連號）；決定 4 對 #50 改為轉述。proposal Why 仍用「不能安靜地切換」一詞，但其行為已在 spec 定義，可接受。 |

### 二、修法引入／殘留的新問題

| # | 嚴重度 | 位置 | 問題與建議 |
|---|---|---|---|
| N1 | **必修**（低成本） | specs/agent-lease/spec.md「租約狀態與事件」＋「憑證來源缺失」＋「只記錄階段沒有租約」 | 規格說「SHALL **依序** 經過 requested → granted／denied → active → released…」，但新情境出現兩條不合法路徑：(a)「憑證來源缺失」是 granted 後直接以 stop 回報（released），沒有 active；(b)「只記錄階段」要補送「未經核准的啟動紀錄」（start→active 卻沒有 granted）。建議補一張允許的轉移表（例如 granted→released 允許、理由＝未啟動；未核准啟動另立事件類型 `unleased-start` 不進狀態機），或改寫這兩個情境。 |
| N2 | 建議 | specs/agent-lease/spec.md「只記錄階段沒有租約」 | 只涵蓋「LLMGateway 無法連線」；design 階段 1 說「申請**失敗**也照樣啟動」，但「政策拒絕（denied）」在階段 1 的行為沒有情境。建議補一個 denied 的階段 1 情境，或把情境文字擴成「無法連線或被拒」。 |
| N3 | 建議 | design.md 決定 7（prometheus 方案） | Prometheus exporter 固定在本機 `http://localhost:9464/metrics`，官方文件未載可改埠；同一台 worker／Mac 同時兩個 Claude Code session（sidecar 的 team 常態）會撞同一個埠。建議在 Risks 或 Open Questions 註明「v0 假設同時只有一個 Claude session 導出」，或改用 OTLP／確認埠可設定。出處：<https://code.claude.com/docs/en/monitoring-usage>（“For Prometheus, scraped from http://localhost:9464/metrics”；self-hosted 段落也以 capacity one 描述）。 |
| N4 | 建議 | specs/agent-lease/spec.md「回報暫時失敗時保存後補送」 | 列舉了 start／stop／switch／用量，**漏了 renew**；test 的 MS-3（a–c：暫時失敗、重試耗盡、過期後才續期）也因此仍沒有語意。建議把 renew 納入補送範圍，或明說失敗語意留給實作（fail closed 或撐到有效期限）。 |
| N5 | 建議 | design.md 決定 5 第 3 段的敘述（「出處都在 docs/research/」） | M1 要求的出處未補：`docs/research/gateway-alternatives.md` 沒有 OpenRouter Management API Keys 一節（現有 `management key` 字樣只出現在 agent-endpoints 的 `/credits` 說明）。建議在 gateway-alternatives.md §5.4 或新小節補：Management API key 帳號層級、unscoped、可建立／停用／刪除帳號內 key、不能發 completion 呼叫（<https://openrouter.ai/docs/features/provisioning-api-keys>）。 |
| N6 | 建議（小） | proposal.md「What Changes」第 2 點 | 仍寫「opencode **stats**」帶租約 id 回報；design／spec 已改成 `opencode export <sessionID>`（stats 為彙總）。建議同步 proposal 措辭。 |

### 三、test 的 MS-1～MS-12 對照（哪些進 spec、哪些刻意留實作）

| MS | 去處 | 說明 |
|---|---|---|
| MS-1 申請時連不上 | **部分進 spec** | 進：「強制階段沒有租約」「只記錄階段沒有租約」兩情境的結果（不啟動／照樣啟動＋警告＋補送）。留實作：逾時上界、重試次數、不得留半張租約的具體判定；階段 1 的 denied 未涵蓋（N2）。 |
| MS-2 結束回報時連不上 | **進 spec** | 新 Requirement「回報暫時失敗時保存後補送」＋情境「結束時連不上」（最終恰一筆 released、用量不漏）。 |
| MS-3 續期失敗 | **部分進 spec** | 進：正常續期情境；「對已結束的租約續期→拒絕＋launcher 終止」（＝子情境 d）。留實作：a（暫時失敗重試）、b（耗盡）、c（過期後續期）、e（併發）的語意；renew 未納入補送（N4）。 |
| MS-4 同一 session 連續切換 | **部分進 spec** | 進：d（切換後新開 session 回第一優先）、e 的通知部分（備援用完→停止＋通知）、「通知管道失效→補送恰一則」。留實作：a（連續撞限上限）、b（三把以上的鏈）、c（切換中併發訊號）。 |
| MS-5 時鐘誤差 | **進 spec（要點）** | 序號為準、時刻只作註記；查詢期間「含起點、不含終點」。留實作：亂序時鐘注入的測試方法；恢復時刻的權威來源在 design（上游 reset）。 |
| MS-6 憑證來源缺失 | **進 spec** | 新情境「憑證來源缺失」（不啟動、不部分注入、回報失敗理由）。⚠ 與狀態機的矛盾見 N1。 |
| MS-7 重複與亂序回報冪等 | **進 spec（部分）** | 進：「重複回報」情境（恰一筆）；去重鍵寫在 design 決定 3。留實作：亂序重排（spec 已說以序號為準，具體機制留實作）。 |
| MS-8 同 worker×角色兩張 active | **留實作／政策** | spec 未定允許或拒絕；測試大綱本身也說「依先定的政策」——刻意留給政策檔與實作。 |
| MS-9 通知管道失效 | **進 spec** | 新情境「通知管道失效」＋ Requirement「通知失敗 MUST NOT 讓切換回滾、留本機重試直到送出」。 |
| MS-10 內容外洩掃描 | **進 spec（要點）** | 理由欄位改為固定代碼＋≤200 字、說明不得直接貼 agent 錯誤輸出；掃描情境加入「內容標記」。留實作：具體的截斷／過濾實作。 |
| MS-11 政策變更撤銷 | **進 spec** | 新情境「政策收緊」（下次往來 revoked、觸發者：政策、launcher 終止 agent）。 |
| MS-12 用量遲到與改歸屬 | **進 spec** | 新情境「用量晚到」（併入原租約、彙總更新）＋「未歸因只有在之後出現相同租約 id 時才能改歸屬」。 |

**刻意留在實作**（test `spec-testability.md` §5 的其餘項目）：政策檔 schema（tasks 1.3）、worker 名稱來源與測試覆寫、錯誤碼／逾時詞彙表、通知 sink 的介面細節（依 MyAiStorage#30）、`lgw` 的退出碼。

### 四、複查驗證

- `openspec validate agent-lease-and-usage-record --strict`：**通過**（2026-10-10 實測）。
- 憑證／PII 掃描（沿用前次 regex 集）：`openspec/` 全部檔案 **0 命中**。
- 無新增私有資料引用（MyPMO#4 引句已移除；僅存工單號）。
- 原審查的 M1 殘留（N5）與新問題（N1）之外，未發現其他新矛盾。

