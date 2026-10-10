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
