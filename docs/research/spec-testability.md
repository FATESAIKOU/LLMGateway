# agent-lease-and-usage-record 兩份規格的可測試性分析

> 審查者：LLMGateway 團隊 test。日期：2026-10-10。只讀 `openspec/changes/agent-lease-and-usage-record/specs/` 下兩份 spec（另讀同目錄 design.md／proposal.md 作背景），未改動任何檔案、未 commit。
> 問題：每個 Scenario 能不能寫成自動測試（只用假資料、不需要真憑證）？附測試大綱（前置／動作／驗證）與建議補的 Scenario（錯誤路徑優先）。
> 本檔使用的 Scenario 編號（AL-x／UR-x）是本檔自編，非 spec 原文；原文無編號。引文行號對應 2026-10-10 的檔案內容。

## 0. 結論摘要

- **19 個 Scenario 全部可用「假資料、零真憑證」寫成自動測試**；其中 17 個在 CLI／事件層就能測，2 個（UR-15 的 Claude Code OTel、UR-16 的 opencode 統計）需要「假上游 stub ＋ 本機 fake agent」的整合層，但仍不需要真憑證。
- **最大的可測性風險不是難測，而是 spec 少定了幾個語意**：逾期由誰在何時記、續期失敗怎麼辦、事件如何去重（冪等）、opencode session 怎麼對到租約、時鐘以誰為準。這些沒定之前，對應測試寫不出**穩定**的斷言（會變成測實作細節）。清單見 §5。
- **錯誤路徑缺口明顯**：LLMGateway 在「結束回報」時連不上、續期失敗、同一 session 連續切換、時鐘誤差，四類都沒有 Scenario。建議補的 12 個見 §4。
- 所有假資料一律用**哨兵字串**（如 `FAKE-TOKEN-PERSONAL-<uuid>`）放進假憑證來源；測試尾端再掃描事件紀錄確認哨兵與假內容沒有外洩——這同時就是 UR-17 的測法。

## 1. 建議的測試基礎設施（先有這些，Scenario 才寫得順）

| 元件 | 用途 | 對應 spec 掛點 |
| :- | :- | :- |
| fake backend（記憶體或本機 git 的只追加事件檔），掛在 `lgw` CLI 的後端介面下 | 取代 v0 的 private GitHub repo；測試不得碰真 repo | design.md 決定 3「介面固定以後，後端換成 HTTP 服務不影響 launcher」 |
| 可注入時鐘（`clock` 介面）＋單調序號 | 逾期、續期、彙總期間、恢復時刻 | AL-S6、AL-S10、UR-S19 |
| 假憑證來源：一個目錄放哨兵檔／假環境變數（`CLAUDE_CODE_OAUTH_TOKEN_PERSONAL=FAKE-…`） | 測注入範圍；永不使用真憑證 | agent-lease spec.md:44-53 |
| 假 agent（印出自身 env 的 stub 程序） | 代替真的 claude/opencode 斷言環境 | AL 「只注入一把」「沒有租約不啟動」 |
| 假訊號注入器（偽造 `StopFailure` payload／`-p` 非零 exit＋錯誤字串） | 觸發切換流程 | design.md:63 |
| 假 `agora continue`／`claude --resume` stub（記錄被呼叫的參數） | 驗證「接續原 session」 | design.md:65／claude-failover.md §3.1 |
| 假 Angareion sink（append 檔案） | 驗證通知有發、內容不含憑證 | MyAiStorage#30；AL-S10/S11 |
| 掃描器：憑證樣式（`sk-`、`ghp_`、`AKIA`、`BEGIN PRIVATE KEY`、`password=`…）＋內容標記（假 prompt 字串） | UR-17；並在每個測試尾端當通用斷言 | usage-record spec.md:29-34 |
| Mac／worker 共用 conformance suite | 同一份測試跑兩種 launcher | AL「Mac 與 worker 走同一套」 |

## 2. `agent-lease` 各 Scenario 測試大綱

> 判定：✅ 純假資料可自動化；🟡 需整合層（假上游／stub 程序）；🔴 現階段被 spec 空白卡住（見 §5 編號）。

### AL-S1 申請內容齊全（spec.md:10-12）
- 判定：✅
- 前置：fake backend＋假政策檔（預設 profile 全給）；launcher stub。
- 動作：`lgw lease request --worker w1 --role repo-x/eng --subscription claude-personal --agent claude-code --model <alias>`。
- 驗證：(1) 回覆 JSON 含租約 id、worker、角色、訂閱、agent、模型、來由(null)、申請者、申請時刻、有效期限、狀態；(2) 全回覆以 schema 驗證（用 OpenSpec 文字逐欄檢查）；(3) 深度掃描：不得出現哨兵憑證值；憑證僅以**來源名稱**出現（如環境變數名稱）。

### AL-S2 欄位缺漏（spec.md:14-16）
- 判定：✅
- 前置：同 AL-S1。
- 動作：參數化缺一欄（worker／角色／訂閱／agent／模型），再加一個缺兩欄的 case。
- 驗證：每次拒絕（非 granted）；錯誤訊息**點名**缺少的欄位集合（斷言集合相等，不是只檢查非空）；未產生任何租約事件。

### AL-S3 政策內自動核准（spec.md:21-23）
- 判定：✅
- 前置：政策 fixture 明確允許某組合；無任何人工程序（測試中不模擬人）。
- 動作：申請該組合。
- 驗證：狀態 `granted`；事件序 `requested → granted`；期間沒有任何人為介入的 API 呼叫（以 spy 斷言）。

### AL-S4 政策外拒絕（spec.md:25-27）
- 判定：✅
- 前置：政策 fixture 不含該組合；另備一個「相近但沒申請」的訂閱組合。
- 動作：申請被禁組合。
- 驗證：狀態 `denied`；理由非空且指向政策；**事件帳上沒有替補租約**（斷言整份事件紀錄中沒有其他 granted 且訂閱 ≠ 申請值）；agent 未啟動。

### AL-S5 正常一輪（spec.md:32-34）
- 判定：✅
- 前置：granted 租約；假 agent；可用序號或可注入時鐘。
- 動作：`start`（回報 started）→ 假 agent 結束 → `stop`（回報 stopped）。
- 驗證：事件恰為 `requested, granted, active, released`（順序＋每個事件帶時刻與觸發者）；**不得多發**事件（數量斷言）。

### AL-S6 launcher 沒回報結束（spec.md:36-38）
- 判定：🟡→🔴 取決於「逾期由誰判定」（§5-1）
- 前置：active 租約，有效期限已過（用可注入時鐘推進），launcher 不續期。
- 動作：對 backend 做一次「會觸發逾期判定」的操作——若語意是 lazy（下次查詢時判定），則跑 `lgw ls`；若是排程，則跑假 cron。
- 驗證：租約記為 `expired`（事件含時刻）；沒有 `released`。**需要 spec 先定觸發點，否則此測試無法穩定**。

### AL-S7 撤銷（spec.md:40-42）
- 判定：✅（配假 launcher 輪詢）
- 前置：active 租約；撤銷指令來源＝本人／PMO（測試以角色權杖模擬）。
- 動作：撤銷 → 假 launcher 在下一次輪詢（可縮短週期）往來時收到 `revoked`。
- 驗證：事件 `revoked`（觸發者欄位是本人／PMO）；launcher 回呼「終止 agent」被呼叫一次（spy）；agent 程序被停止。

### AL-S8 只注入一把（spec.md:47-49）
- 判定：✅（核心測試）
- 前置：假憑證來源有 `…_PERSONAL=FAKE-A`、`…_COMPANY=FAKE-B`、`ANTHROPIC_API_KEY=FAKE-C`；假 agent 會 dump env。
- 動作：以 `claude-personal` 租約啟動假 agent。
- 驗證：env 中 `FAKE-A` 出現；`FAKE-B`、`FAKE-C` 不曾在 env（斷言 key 不存在、值不存在）；agent 的程序環境全文掃描無 `FAKE-B`。

### AL-S9 沒有租約不啟動（spec.md:51-53）
- 判定：✅（兩支都要）
- 前置：強制開啟（第 2 階段）；兩情境：申請被拒、backend 連不上（fake backend 注入連線失敗）。
- 動作：launcher 嘗試開 agent。
- 驗證：假 agent **從未被 spawn**（spy 斷言 0 次）；回報含失敗原因（拒絕理由／連線錯誤）；launcher 非零退出並可辨識錯誤碼（**需先定錯誤出口**，§5-5）。
- 補：設計的階段 1「申請失敗照樣啟動、只發警告」目前不在 spec，建議補 Scenario（見 §4 MS-1）。

### AL-S10 個人額度用完（spec.md:58-60）
- 判定：✅（全部可用假訊號）
- 前置：`claude-personal` active 租約；政策順序＝個人→公司；假 `StopFailure(rate_limit)` payload（含 reset 時刻）；假 resume stub；假 Angareion sink。
- 動作：注入額度訊號 → launcher 執行 `lgw lease switch`。
- 驗證：(1) 原租約事件 `superseded`，理由＝額度、恢復時刻＝訊號中的 reset；(2) 新租約訂閱＝`claude-company`、狀態 granted、`requested/granted/active` 齊；(3) resume stub 被呼叫且**帶原 session id**；(4) sink 恰有一則通知，內容含新舊訂閱與租約 id、**不含任何哨兵憑證值**；(5) 新租約的來由、角色、worker 與原租約一致。

### AL-S11 備援也用完（spec.md:62-64）
- 判定：✅
- 前置：已切到 `claude-company` 的 active 租約；政策中公司之後無其他可用訂閱。
- 動作：再注入額度訊號。
- 驗證：租約記 `released`（理由：額度）；agent 停止；PMO 收到通知；**沒有**任何新 granted 租約（特別是政策外的 `openrouter` 等不得出現）。

### AL-S12 Mac 上開 agent（spec.md:69-71）
- 判定：✅（需 worker 名稱可注入）
- 前置：Mac 啟動包裝 stub；主機名以測試環境變數固定（**需 spec 定名稱來源／測試覆寫方式**，§5-6）。
- 動作：走同一份 conformance suite 開 agent。
- 驗證：租約 worker＝`mac:<固定主機名>`；事件序列與 worker 版完全一致（同一測試套件兩種 driver 各跑一次，斷言輸出一致）。

## 3. `usage-record` 各 Scenario 測試大綱

### UR-S13 用量對到租約（spec.md:10-12）
- 判定：✅
- 前置：一張 known 租約（worker/角色/訂閱/agent/模型皆已知）；fake backend。
- 動作：送出一筆帶該租約 id 的假用量（token、成本）。
- 驗證：歸因到租約四元組；查詢結果與輸入一致；未歸因清單為空。

### UR-S14 用量沒有租約 id（spec.md:14-16）
- 判定：✅
- 前置：同上。
- 動作：兩筆假用量——(a) 完全沒有租約 id；(b) 租約 id 不存在。
- 驗證：兩筆都進未歸因清單；**沒有**被歸到任何現存租約；彙總時未歸因另列（與 UR-S19 呼應）。

### UR-S15 Claude Code 的 OpenTelemetry（spec.md:21-23）
- 判定：🟡 兩層：
  1. **環境層（必做、純假資料）**：假 agent 斷言 launcher 設了 `CLAUDE_CODE_ENABLE_TELEMETRY=1`（或等效）＋ `OTEL_RESOURCE_ATTRIBUTES` 含租約 id、worker、角色；`OTEL_*` 指向測試 collector。
  2. **整合層（選做）**：以假上游（本機 Anthropic Messages stub，接受假 token、回帶 usage 的固定回應）＋本機 OTLP collector，跑真 Claude Code 固定版本，斷言收到的 metrics 帶三個資源屬性。
- 驗證：整合層可能受 Claude Code 版本與 OTel 行為變動影響；建議標為 `@integration`、釘版本、可在 CI 跳過。

### UR-S16 opencode 結束時附上統計（spec.md:25-27）
- 判定：✅（配 stub opencode）；🔴 有一個對應缺口（§5-4）
- 前置：stub `opencode`（`stats/export` 回固定 JSON）；租約與其 session 的對應方式須先定（目前 spec 沒說 launcher 怎麼知道哪個 opencode session 屬於這張租約）。
- 動作：`lgw lease stop`（或 released 流程）。
- 驗證：released 事件附 input/output tokens 與成本，數字等於 stub 回傳；**只含該 session**（放兩個假 session，斷言另一 session 的數字不在）。

### UR-S17 掃描紀錄（spec.md:32-34）
- 判定：✅
- 前置：跑一條完整流程（AL-S8＋AL-S10 的假資料版本），憑證來源放哨兵、假 agent 產生假 prompt 與假檔名。
- 動作：以憑證樣式＋內容標記掃描整份事件紀錄與用量紀錄（含 GitHub 格式的 repo 內容時，測的是 local fake backend 的存檔）。
- 驗證：0 命中（憑證樣式與內容標記皆零）；另加正向對照：掃描器對已知含哨兵的夾具必須報中（避免掃描器壞掉卻誤過）。
- 註：spec 只要求掃「憑證樣式」，但 Requirement 文字同時禁止 prompt／回應／檔案內容；建議把內容標記也納入同一 Scenario（見 §4 MS-10）。

### UR-S18 看現在誰在跑（spec.md:39-41）
- 判定：✅
- 前置：事件紀錄內有 active、granted（尚未 active）、released、expired、superseded、denied 各一（造事件）。
- 動作：查詢 active 租約。
- 驗證：只回 active 那筆，欄位含 worker、角色、訂閱、agent、模型、開始時刻；已結束者全部不出現。

### UR-S19 依訂閱彙總（spec.md:43-45）
- 判定：✅（需時鐘注入）
- 前置：兩張租約（不同訂閱）＋數筆假用量＋一筆未歸因；可注入時鐘跨期間邊界。
- 動作：查某期間依訂閱彙總。
- 驗證：每訂閱的 token、成本、**租約數**加總正確；未歸因另列一行；期間邊界定義（含頭含尾？半開？）需先定（§5-9），並對邊界日各寫一筆。

## 4. 建議補的 Scenario（尤其錯誤路徑）

> 依 §5 的問題編號標注相依；每則附大綱草稿。建議直接進兩份 spec 的對應 Requirement 下。

### MS-1 LLMGateway 連不上——申請時（對應「憑證由 launcher 注入」）
- 前置：backend 注入逾時／連線錯誤；強制開啟與關閉兩支。
- 動作：launcher 申請租約。
- 驗證：逾時上界與重試次數（**需先定**，§5-5）；強制開→不啟動＋失敗回報；階段 1→照樣啟動＋警告事件；兩種都不得留下半張租約。

### MS-2 LLMGateway 連不上——結束回報時（對應「租約事件是權威紀錄」）
- 前置：agent 已結束；backend 此時不可用。
- 動作：`stop` 失敗 → backend 恢復 → 重放。
- 驗證：回報被本機 outbox 保存、恢復後補送；事件最終恰一筆（去重）；用量不漏；補送後查詢與彙總正確。**需先定 outbox 語意**（§5-7）。

### MS-3 續期失敗（對應「launcher 沒回報結束」）
- 子情境：(a) 暫時錯誤、到期前重試成功；(b) 重試耗盡仍在期限內→？(c) 已過期後才續期→拒絕？(d) 對 superseded／revoked 租約續期→拒絕且 launcher 必須終止；(e) 兩個 launcher 同時續同一租約（併發/重複）。
- 驗證：(a) 事件無 expired、續期事件恰一筆；(b-e) 依**先定的失敗語意**斷言（fail closed 停 agent 或撐到到期）；(e) 去重後恰一筆續期。**依 §5-2、§5-3**。

### MS-4 同一 session 連續切換（對應「訂閱切換不得安靜發生」）
- 子情境：(a) 個人→公司後，公司立刻也撞限→再切或停止（MS-4 與 AL-S11 的差別：同一 session、短時間連續）；(b) 政策有三把以上的鏈；(c) 切換途中（新租約申請中）又收到額度訊號（併發）；(d) 切換後新開的 session 必須回到個人（design.md:68，spec 未寫）；(e) 新租約被政策拒絕時的停止與通知。
- 驗證：每次切換恰一組 superseded＋一張新 granted；session id 不變；通知數＝切換數（不重不漏）；(d) 新 session 首租約＝個人。

### MS-5 時鐘誤差（對應「租約狀態與事件」＋「查詢與彙總」）
- 子情境：(a) launcher 時鐘落後／超前 backend 5 分鐘時，續期與逾期判定一致（以事件時序／server 側為準）；(b) 同秒寫入兩事件→單調序號保證排序穩定；(c) 恢復時刻（額度 reset）取上游值，本地時鐘漂移不改寫；(d) 彙總期間邊界在兩端時鐘不同時仍一致。
- 驗證：固定亂序時鐘注入下，事件排序與判定不變；建議 spec 明寫「時序以 append-only 紀錄的序號為準，時刻僅為註記」。

### MS-6 注入時憑證來源缺失（對應「憑證由 launcher 注入」）
- 前置：租約 granted，但本機來源沒有對應哨兵（檔案缺、空值）。
- 動作：注入→啟動。
- 驗證：不啟動 agent（fail closed）；**不部分注入**（env 中沒有其他訂閱的哨兵）；回報原因；事件留下一筆失敗（或 released 理由）。

### MS-7 重複與亂序回報的冪等（對應「租約事件是權威紀錄」）
- 動作：同一 `start`／`stop`／用量各重送兩次、`stop` 早於 `start` 抵達。
- 驗證：事件不重複（以冪等鍵去重）；亂序以序號重排後狀態機仍合法；**需先定冪等鍵**（§5-3）。

### MS-8 同 worker×角色 同時兩張 active（對應「租約欄位」）
- 動作：同一組合連開兩張。
- 驗證：依先定的政策——允許（各自獨立）或拒絕第二張（指出衝突租約）；無論哪種，兩張租約的用量歸因不得互混。

### MS-9 通知管道失效（對應「訂閱切換不得安靜發生」）
- 前置：假 Angareion sink 注入失敗。
- 動作：觸發切換。
- 驗證：切換仍完成且事件紀錄完整（不可因通知失敗而回滾或靜默）；通知入本機重試佇列；恢復後恰補一則。**需先定通知失敗語意**（§5-8）。

### MS-10 內容外洩掃描（擴充 UR-S17）
- 動作：除憑證樣式外，掃 prompt 標記、回應標記、檔名標記、以及理由欄位（錯誤訊息可能夾帶 prompt）。
- 驗證：0 命中；事件的理由欄位需有截斷／過濾規則（**需先定**）。

### MS-11 政策變更撤銷既有租約（與 AL-S7 互補）
- 動作：改政策檔（移除某組合）→ 同步後。
- 驗證：既有 active 租約在下次往來時被 `revoked`（觸發者＝政策）並終止 agent；與 S7 同一條「下次往來得知」語意。

### MS-12 使用量的遲到與不可回溯改歸屬（對應「租約事件是權威紀錄」）
- 動作：released 統計晚到；或先歸為未歸因、之後出現對應租約。
- 驗證：遲到資料併入原租約、彙總更新；未歸因是否可事後歸屬—依先定規則（spec 只說「不猜測」，未說可否修正）**需先定**。

## 5. spec／設計需要先定的問題（沒有這些，測試會測到實作細節）

1. **逾期的觸發與責任**：誰在何時把過期租約記 `expired`？（lazy 於下次讀取／launcher 回報／排程掃描）→ 卡 AL-S6、MS-3。
2. **續期失敗語意**：重試策略、耗盡後是 fail closed（停 agent）或撐到有效期限；過期後能否續期；對 superseded／revoked 的續期必須拒絕。→ 卡 MS-3。
3. **冪等鍵與事件去重**：append-only 紀錄用什麼鍵去重（租約 id＋事件型別＋序號？）、亂序如何重排。→ 卡 MS-2、MS-7、MS-12。
4. **opencode session 對租約的對應**：launcher 在啟動時要抓到什麼（session id／cwd／時間窗），release 時才能只取該 session 統計。→ 卡 UR-S16。
5. **強制開關與錯誤出口**：`lgw` 的逾時、重試、退出碼、錯誤碼詞彙表；launcher 設定切階段的介面。→ 卡 AL-S9、MS-1。
6. **worker 名稱來源**：`mac:<主機名>` 的主機名取得與測試覆寫（env？參數？）。→ 卡 AL-S12。
7. **離線 outbox**：stop／切換回報失敗時的本機保存、重放、去重、順序。→ 卡 MS-2。
8. **通知 sink 介面與失敗處理**：Angareion 的通知格式（最小欄位、必不含憑證）、失敗重試。→ 卡 MS-9。
9. **時序與期間邊界**：事件排序以序號為準；彙總期間含頭含尾；恢復時刻的權威來源（上游 reset）。→ 卡 AL-S10、UR-S19、MS-5。
10. **政策檔 schema**：允許清單與訂閱優先序的機器可讀格式（測試 fixtures 要用）；期 1 預設 profile 的等價內容。→ 卡 AL-S3／S4 的 fixture。
11. **理由欄位的內容規則**：長度上限、禁止夾帶 prompt／個資的過濾規則。→ 卡 MS-10。
12. **未歸因可否回溯歸屬**：只列不改，或允許在出現對應租約後修正。→ 卡 MS-12。

## 6. 附註

- 本分析不碰 v0 後端（private GitHub repo）：設計已把後端放在 CLI 介面之後（design.md 決定 3），自動測試應一律走 fake backend；真 repo 只在人工驗收時碰。
- 兩個需要整合層的 Scenario（Claude Code OTel、opencode stats）建議在 spec 標「test level」欄位區分 unit／integration，否則 CI 會被迫跑真 agent。
- 與 `docs/research/claude-failover.md` 的對接：AL-S10 的「額度訊號」用該檔 §1.4 的 `StopFailure(rate_limit)` 與 `api_retry`／exit code 假造，不需真額度；AL-S10 的「接續原 session」對應 `--resume <session_id>` 的 stub 斷言。
