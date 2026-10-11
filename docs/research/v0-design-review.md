# v0 設計批判性審查（design／specs／tasks vs 研究與 hygiene）

> 審查範圍：`openspec/changes/gateway-v0/`（proposal.md 第 3 版＝本人已確認 scope；design.md；specs/agent-lease、key-vault、status-board；tasks.md），對照 `docs/research/`（upstream-claude-code-compat.md、quota-signals.md、claude-failover.md §7、litellm-supply-chain.md）與期 1 specs（`openspec/specs/agent-lease`、`usage-record` 存在，參照有效）。
> 本版針對 commit 47e9cbf 之後的三份最新版寫成（design 決定 9 改為「換把程序歸 sidecar，`lgw` 只留 API 客戶端＋訊號規則＋管理命令」；spec／tasks 同步跟進）。PM 指示前已寫過一版者以本版為準。
> 規矩：只讀 repo 與公開官方文件；未讀憑證、未跑 `gh`、未 commit、未打要 key 的 API。
> 每條標【必修】（實作前必須改，否則行為錯誤或破 hygiene）或【建議】（應改，但不擋開工）。
> 結論先行：**必修 2 條，建議 26 條（S1–S26；其中 S19–S26 針對新版邊界變化）；無需退回重寫，修完可進實作。**

## 0. 先說與研究一致、不需改的部分

- design 決定 1（只保管出租、不代轉）與 claude-failover §7.4 本人裁定一致：不代轉、不給 opencode 用 Claude 訂閱、不用 sub2api 帳號池；灰色地帶（個人↔公司）本人接受且 design Risks 已載明約束。spec 以 `not_allowed` 落實 opencode↔Claude 互斥，一致。
- design 決定 7 的上游接法與 upstream 相容研究一致：OpenRouter／Ollama Cloud 有官方 Claude Code 接法（`ANTHROPIC_BASE_URL`＋bearer、`ANTHROPIC_API_KEY` 清空）；Zen 無官方接法故 `claude-code` 拒絕。一致。
- design 決定 9 關掉 `autoContinueAtUsageLimit`：研究確認該設定只讀 user／`--settings`／managed（failover §1.2），且 launcher 與它互斥等待的理由成立。一致。
- 換把流程（SIGINT 安全結束 → `exhausted` → 新 env 重開 → `agora continue` 接回）用的是研究 §3.1(a) 的官方零件（resume 接續、中斷語意），只是把 `--resume` 換成 sidecar 已對齊的 `agora continue`；Open Questions 已標 id 前綴待確認。一致。
- 額度訊號來源（Claude 畫面字串帶 resets、opencode `session.error` 402／429＋`retry-after`）與 quota-signals 一致；agy 不在五把內，排除一致。
- LiteLLM 條件式＋固定版本查核，與 litellm-supply-chain.md 一致。
- 冷卻預設值有研究根據：Claude 5h（官方 five_hour 視窗，見 statusline 文件）、Ollama 5h（官方 launch blog「extended 5-hour coding session window」，見 https://ollama.com/blog/launch）、Zen 取 `retry-after`（免費 429 最長約 24h，見已合併的 anomalyco/opencode#49195）。三者皆可引用，唯 design 未註出來（見 S18）。
- 新版決定 9（換把程序歸 sidecar agent 宿主，`lgw` 只留 API 客戶端＋訊號規則表＋管理命令）與研究無衝突：failover §3.1(a) 路線本就允許「程序由外部 wrapper 持有」，放 sidecar 仍是同一條路線。但 repo 邊界變化帶來新的介面問題，見 S19–S26。

---

## A. 必修（2 條）

### B1. tasks 2.2「402／429」寫法過寬，會導致短暫限流也被當額度用完【必修】

- 檔名＋段落：`tasks.md` §2.2；`design.md` 決定 9(2)。
- 問題：2.2 把判讀規則寫成「`Free usage exceeded`、`usage limit reached`、402／429」。照字面做，任何 402／429（例如 OpenRouter in-flight 預算 402 重試即好、短暫上游 429）都會觸發 `exhausted` 換把，直接違反 design 決定 8 與 spec「短暫限流不換把」Scenario，以及 proposal「避免來回跳」的風險約束。研究怎麼說：quota-signals §2.2 寫明 in-flight 402 是看 `Retry-After` 重試的暫態；§2.1 的 429 預設走指數退避重試；只有 `FreeUsageLimitError`／`GoUsageLimitError`／`CreditLimitExceeded`／`limit_source` 等 quota 型才該換把。
- 正確說法＋出處：規則表必須寫成「quota 型 402／429 才算額度用完；無 quota 標記的 429／402 判為暫時錯誤」。出處：`docs/research/quota-signals.md` §2.1–§2.2、https://openrouter.ai/docs/api/reference/limits（`limit_source` 語義）、https://github.com/anomalyco/opencode/pull/49195（已合併的分類表）。
- 建議改法：2.2 改為「規則用資料表：Claude `You've hit your … limit`；Zen `FreeUsageLimitError`／`GoUsageLimitError`／`CreditLimitExceeded`（body 標記優先於狀態碼）；OpenRouter `limit_source` 非 in-flight 才算；其餘 402／429 一律判暫時錯誤」，並以 quota-signals 樣本寫正反兩面測試。

### B2. `/board` HTML 的認身分機制沒定義，spec 卻要求同一套客戶端憑證【必修】

- 檔名＋段落：`specs/status-board/spec.md`「唯讀且不含值」；`design.md` 決定 11；`tasks.md` 1.9。
- 問題：spec 要求 JSON 與 HTML「經同一套客戶端憑證存取」。瀏覽器開 HTML 頁無法像 API 那樣送 `Authorization: Bearer`；實作若圖方便把 token 放 URL query，會進 access log 與瀏覽器歷史，直接踩 key-vault「值不外洩」的紅線（客戶端憑證雖非上游 key，但外洩即失守）。
- 正確說法＋出處：先定義機制再實作，例如「HTML 只經 SSH 埠轉送在本機開，照樣送 header（用小包裝頁／curl 產頁），token 永不進 URL」。出處：本 repo 決定 5（SSH 跳板埠轉送）即是現成載體。
- 建議改法：tasks 1.9 加一條「board HTML 認證方式（不用 URL 傳憑證）」，驗收 4.6 加測「URL／log 無憑證」。

---

## B. 建議（18 條）

### S1. 線上合約詞彙未統一：`purpose` vs 來由、`signal` 無枚舉、時間格式未定【建議】

- 檔名＋段落：`design.md` 決定 6（請求欄位 `purpose?`、`signal`、`resets_at`、`snapshot`）；`specs/agent-lease/spec.md`（「來由」）；`tasks.md` 5.1。
- 問題：同一欄位兩種名字（`purpose`／來由）；`exhausted` 的 `signal` 值域無定義（兩邊 repo 各自實作必分叉，sidecar 範圍 B 已開工）；`expires_at`／`resets_at`／`earliest_reset` 格式與時區未定；`report_usage` 的 `snapshot` 形狀留白。
- 正確說法：wire contract 應在 5.1 交接前鎖定，例如 `signal ∈ {session_limit, weekly_limit, opus_limit, sonnet_limit, free_usage, go_usage, credit_exhausted, unknown}`、時間一律 ISO-8601 UTC、`earliest_reset` 缺席時省略。
- 嚴重度：建議（5.1 本來就是交接點，屆時一併鎖定即可）。

### S2. `model` 是否可空，proposal 與 spec 打架【建議】

- 檔名＋段落：`proposal.md` §2 表（模型可空）vs `specs/agent-lease/spec.md` 租約欄位（「模型；」無可空註記，旁邊來由／preferences 都有）。
- 問題：實作照 spec 會把 model 當必填，照 proposal 則可空。
- 正確說法：統一為可空（與 proposal 一致），spec 補「（可空）」二字。
- 嚴重度：建議。

### S3. 決定 7 表格把尚未實打的變數名寫成定案【建議】

- 檔名＋段落：`design.md` 決定 7 表（opencode 側 `OPENROUTER_API_KEY`／`OLLAMA_API_KEY`／`OPENCODE_API_KEY`）、`tasks.md` 1.8。
- 問題：upstream 研究只確認 opencode 官方路線是 `/connect` 存 key（`upstream-claude-code-compat.md` §3.3：不需手設 shell 變數），沒確認這三個 env 名可被 opencode 讀到；Ollama 相容位址亦同。Open Questions 雖有標待實打，但表格語氣像已定案。
- 正確說法＋出處：表格加「（暫定，4.4 實打確認）」。出處：`design.md` Open Questions、https://opencode.ai/docs/providers。
- 嚴重度：建議。

### S4. `below_floor` 與「平均分攤可選」只在 design，spec／tasks 沒承接【建議】

- 檔名＋段落：`design.md` 決定 6（拒絕理由含保留的 `below_floor`）、決定 8（平均分攤保留可選）；spec 拒絕理由只有四種；tasks 無對應。
- 問題：之後啟用時客戶端（sidecar）會收到沒見過的 reason；policy schema 也沒留欄位。
- 正確說法：spec 拒絕理由加「`below_floor`：保留，v0 不發」；policy 加可選欄位註記；tasks 1.1／1.4 附帶。
- 嚴重度：建議。

### S5. 同時租用上限缺併發原子性【建議】

- 檔名＋段落：`design.md` 決定 3／8；`specs/agent-lease/spec.md` 同時租用上限；`tasks.md` 1.2／1.4。
- 問題：`ThreadingHTTPServer`＋SQLite 下，兩道併發 `POST /v1/leases` 可能同時通過上限檢查（check-then-insert race），cap 2 變 cap 3。
- 正確說法：選定與寫入同一事務（`BEGIN IMMEDIATE` 或等價），tasks 1.10 加併發測試。
- 嚴重度：建議（v0 流量小，但 cap 正是條款防線，不宜留 race）。

### S6. 冷卻中訂閱上的既有租約能否 `renew`，沒寫【建議】

- 檔名＋段落：`specs/agent-lease/spec.md`（冷卻中 MUST NOT 出租；「不打斷」規則）；`design.md` 決定 8。
- 問題：A 回報用完 → 訂閱冷卻；B 仍持有同訂閱的舊租約並每小時 renew。renew 算不算「出租」？擋 renew 會打斷正在跑的 B（違反不打斷）；放行則冷卻形同只擋新客。
- 正確說法：二選一寫死。建議：放行 renew（不打斷優先），冷卻只擋新租約；spec 補一句。
- 嚴重度：建議。

### S7. 「同一 agent 不雙持租約」只有 exhausted 路徑保證，直接申請兩次擋不住【建議】

- 檔名＋段落：`specs/agent-lease/spec.md` 切换規則；`design.md` 決定 8。
- 問題：服務認得 worker＋agent，但租約申請沒有 agent-session 維度；同一 worker＋agent 連發兩次 `POST /v1/leases` 可雙持，spec 的 MUST 無執行點。
- 正確說法：三選一：(a) 服務端加護欄（同 worker＋agent＋訂閱只留一張 active）；(b) 明寫「由客戶端保證，服務不檢查」；(c) 租約加可選 `agent_session` 欄。建議 (b)＋log 警告，v0 不做複雜的。
- 嚴重度：建議。

### S8. 狀態板「未部署」與「最後成功時刻」有 spec 無 tasks【建議】

- 檔名＋段落：`specs/agent-lease/spec.md`（憑證來源缺失→狀態板「未部署」）、`specs/status-board/spec.md`（查詢失敗附最後成功時刻）；`tasks.md` 1.9。
- 問題：1.9 只寫 JSON／HTML＋定期查詢＋未知，兩處顯示需求漏了。
- 正確說法：1.9 補「未部署列」與「失敗附最後成功時刻」。
- 嚴重度：建議。

### S9. 401 留不留稽核事件，兩處說法需對齊【建議】

- 檔名＋段落：`specs/key-vault/spec.md`（401 不留租約事件）vs `proposal.md`（每一次拒絕都留紀錄）。
- 問題：401 算不算「拒絕」？全不記則撤銷憑證後的除錯無跡；記了又要定義記什麼（只能記 클라이언트名＋時間，不能記 token）。
- 正確說法：二選一寫死。建議：401 記一條 auth 事件（客戶端名、時間、結果，不含憑證任何成分），與「不留租約事件」並存。
- 嚴重度：建議。

### S10. 客戶端憑證雜湊演算法與 token 熵未指定【建議】

- 檔名＋段落：`specs/key-vault/spec.md` 客戶端憑證；`design.md` 決定 4；`tasks.md` 1.3。
- 問題：只寫「雜湊」「隨機」，沒寫演算法（加鹽？）與長度，實作自由發揮。
- 正確說法：例如 token `secrets.token_hex(32)`、存 salted SHA-256（salt 同存），revoke 即刪。寫進 1.3。
- 嚴重度：建議。

### S11. `events` 只追加無上限，長期會膨脹【建議】

- 檔名＋段落：`design.md` 決定 3；`tasks.md` 1.2。
- 問題：事件表無 retention／快照，重建越久越慢，磁碟只增不減。
- 正確說法：v0 加註「滿 N 條或每月做一次 snapshot＋封存舊事件」（實作可留到 v0 後，但設計先寫）。
- 嚴重度：建議。

### S12. 綁定位址的驗收測試缺失【建議】

- 檔名＋段落：`specs/key-vault/spec.md`（從外部連不上）；`tasks.md` 1.10／3.1。
- 問題：有 scenario（外部連不上）但 tasks 只有單元本體的測試，沒有綁定驗證（bind loopback＋內部介面、無 0.0.0.0）。
- 正確說法：1.10 或 3.1 補「啟動後檢查監聽位址」測試。
- 嚴重度：建議。

### S13. 通知「恰一則補送」落在 sidecar，2.4 交接須點名【建議】

- 檔名＋段落：`specs/agent-lease/spec.md`（通知管道失效 Scenario）；`design.md` 決定 10（Mac 由 `sidecar agent run` 在終端機印出）；`tasks.md` 2.4。
- 問題：新版下通知實作全在 sidecar（常駐 Angareion＋Mac 終端機印出），spec 的「換把照常＋恢復後補送恰一則」若不出現在 2.4 交接清單就會被漏掉。
- 正確說法：2.4 補「通知語義：事件先行、發送失敗進本機重試佇列、去重恰一則；Mac 前景印出同規」。
- 嚴重度：建議。

### S14. policy 預設值散在 design，tasks 1.1 只有檔名【建議】

- 檔名＋段落：`design.md` 決定 8（順序、`:free` 限制、caps 2、冷卻預設）＋決定 6（租期 12h、renew 1h）；`tasks.md` 1.1。
- 問題：1.1 只寫「`policy/default.json`」，上述數值沒有明確落點，實作可能各寫各的。
- 正確說法：1.1（或 1.4）補「policy 預設值即 design 決定 6／8 之數，測試鎖定」。
- 嚴重度：建議。

### S15. v0 policy 禁止 claude-code 走 OpenRouter／Ollama 應明寫【建議】

- 檔名＋段落：`proposal.md`（預設不接，要接由政策檔打開）；`specs/agent-lease/spec.md` 選定規則。
- 問題：spec 的初始政策只列順序，沒寫「v0 的政策檔 MUST NOT 含 claude-code→openrouter／ollama」；將來有人手滑加一行就繞過 proposal 的政策。
- 正確說法：初始政策加一條否定約束（或 1.4 測試鎖定順序表恰為此二列）。
- 嚴重度：建議。

### S16. `exhausted` 回覆 `{next: <同上>}` 指代不明【建議】

- 檔名＋段落：`design.md` 決定 6。
- 問題：「同上」指租約建立回覆（含 `lease_id, subscription, env, unset, expires_at`）還是含 `request_id` 回聲？客戶端要解析就不能猜。
- 正確說法：寫清 `next` 即租約物件（含新 `lease_id`），或直接複用建立回覆的 schema 名。
- 嚴重度：建議（併 S1 在 5.1 鎖定）。

### S17. board 的 worker 名含 Mac 主機名，提醒內部可見即可【建議】

- 檔名＋段落：`specs/agent-lease/spec.md`（`mac:<主機名>`）；`specs/status-board/spec.md`。
- 問題：主機名可能具識別性；目前只出現在運行時紀錄與內網狀態板，不在 repo，合規。但 board 一旦可見範圍擴大即有外洩面。
- 正確說法：維持現狀，加一句「狀態板（含 worker 名）永遠只走客戶端憑證＋SSH 通道，不對外」。
- 嚴重度：建議。

### S18. design 決定 8 的預設冷卻請把研究出處寫進去【建議】

- 檔名＋段落：`design.md` 決定 8（Claude 5h、Ollama 5h、Zen retry-after）。
- 問題：數字正確但無引文，後人會以為是拍腦袋。
- 正確說法＋出處：Claude 5h 出自官方 five_hour 視窗（https://code.claude.com/docs/en/statusline）；Ollama 5h 出自官方 launch blog（https://ollama.com/blog/launch）；Zen 24h 上限出自 https://github.com/anomalyco/opencode/pull/49195。
- 嚴重度：建議。

### S19. 決定 9 把 sidecar 的程序細節寫進本 repo，兩份描述會漂移【建議】

- 檔名＋段落：`design.md` 決定 9（send-keys ctrl+c、三段終止秒數、Esc 次數、agora continue 旗標、D4／spike 引用）。
- 問題：新版分工是「程序歸 sidecar 實作」，但秒數、旗標、次數等細節寫在我們這邊；將來任一邊改了，另一邊即過時，且 reviewer 無法驗證（sidecar 程式不在本 repo）。
- 正確說法：決定 9 標明「程序規格以 MyAiStorage#31 為準，本節為引用摘要」；秒數與旗標要么移出、要么標「轉述，改動以那邊為準」。
- 嚴重度：建議。

### S20. `client/opencode.json` 建了，但新版下誰讀它沒寫【建議】

- 檔名＋段落：`design.md` 決定 7（「由模組帶 `OPENCODE_CONFIG` 指給 opencode」）；`tasks.md` 1.1。
- 問題：`lgw` 不再開 agent，「模組」指誰？sidecar 直接讀檔，還是經 `lgw` 的 getter？1.1 只管建檔，不管消費端。
- 正確說法：1.1 或 2.4 寫清消費者與讀取方式（sidecar 經何種介面取得該檔內容）。
- 嚴重度：建議。

### S21. agora id 前綴的 open question 成孤兒【建議】

- 檔名＋段落：`design.md` Open Questions（id 前綴實作時確認）；`tasks.md`（舊 2.5 已刪，新版無對應條目）。
- 問題：確認工作原屬舊 2.5，改版後無落點；2.4 交接也沒點名。若 sidecar 本來就有慣例，應直接關閉該 question。
- 正確說法：2.4 加「agora continue 旗標與 session-id 前綴慣例對齊」，或確認已有後關閉該 open question。
- 嚴重度：建議。

### S22. 2.3「固定 ref」無版本政策【建議】

- 檔名＋段落：`tasks.md` 2.3；`design.md` 決定 9（sidecar 從固定 ref 裝進 venv）。
- 問題：ref 是 tag 還是 commit？`lgw` API 不相容時 sidecar 如何跟？沒寫，屆時兩邊各說各話。
- 正確說法：2.3 加版本規則，例如「固定 tag、可重現；改 API 即換 tag 並經 5.1 通知」。
- 嚴重度：建議。

### S23. Mac 驗收等 sidecar 的命令，依賴無追蹤條目【建議】

- 檔名＋段落：`tasks.md` 4.1–4.3、Migration 4（等 sidecar 提供 `sidecar agent run`）；`design.md` 決定 9（依賴）。
- 問題：v0 關鍵路徑跨 repo，卡住時 tasks 看不出來等誰。
- 正確說法：§5 或 2.4 加「等 MyAiStorage 提供 `sidecar agent run`（blocked-by 註記）」。
- 嚴重度：建議。

### S24. 1.10「每個 Scenario 都有對應測試」在新分工下不成立【建議】

- 檔名＋段落：`tasks.md` 1.10；`specs/agent-lease/spec.md` launcher 側 scenarios（值不上命令列、只注入一把的環境面、強制階段不啟動、只記錄階段）。
- 問題：launcher 側行為在本 repo 測不了（程序在 sidecar）；照字面執行 1.10 會去測測不到的東西。
- 正確說法：1.10 限「服務端 scenarios」；launcher 側改由 4.5 驗收（`tab create --env` 不留值已有）＋2.4 交接測項。
- 嚴重度：建議。

### S25. proposal §5 與新決定 9 不一致（`lgw run` 入口、換把程序歸屬）【建議】

- 檔名＋段落：`proposal.md` §5（`lgw run claude|opencode` 命令列入口、換把程序寫在模組內）；`design.md` 決定 9 新版。
- 問題：proposal 是本人確認的定案紀錄，§5 仍寫舊歸屬（`lgw run`、程序在模組）；實作者兩份對著看會混淆。
- 正確說法：scope 不變，只改歸屬——proposal §5 加 errata 行指向 design 決定 9 新版（入口改 `sidecar agent run`，程序歸 sidecar，`lgw` 只留 API 客戶端＋規則表＋管理命令）。
- 嚴重度：建議。

### S26. tasks 5.4／5.3 順序顛倒【建議】

- 檔名＋段落：`tasks.md` §5（5.4 在 5.3 之前）。
- 問題：純排版，但按序執行會先讀到後面的號。
- 正確說法：調回 5.3→5.4。
- 嚴重度：建議。

---

## C. tasks 覆蓋總表（spec 要求 → tasks 條目）

| spec 要求 | tasks 落點 | 狀態 |
|---|---|---|
| 租約欄位／env／unset／preferences／拒絕＋earliest_reset | 1.4 | ✅（model 可空見 S2） |
| 依政策選定／冷卻跳過／初始順序 | 1.4 | ✅（否定約束見 S15） |
| client_mismatch | 1.3 | ✅ |
| 只注入一把／值不上命令列／缺 key 跳過 | 1.8（env 表）、sidecar（2.4 交接） | ✅（launcher 側測試權責見 S24） |
| 無租約不啟動 | sidecar（2.4 交接） | ✅ |
| 先結束再回報／superseded／同回覆給下一張 | 1.5（服務）、sidecar（2.4 交接） | ✅ |
| 冷卻＋2min／預設冷卻／冷卻禁租禁試探 | 1.5 | ✅（renew 語義見 S6） |
| 單租約／不打斷／關 autoContinue／事件＋板 | 1.5、sidecar（2.4 交接）、1.9 | ✅（通知語義見 S13） |
| 短暫限流不換把 | spec 有，tasks 2.2 需改寫 | ⚠️ 見 B1 |
| Mac 同一套／無 Mac 分支 | sidecar 同一程式（2.4 交接）、4.1–4.3 | ✅（外部依賴見 S23） |
| `lgw` 打包固定 ref | 2.3 | ✅（版本政策見 S22） |
| agora id 前綴慣例 | 無落點 | ⚠️ 見 S21 |
| 同時租用上限 | 1.4 | ✅（併發見 S5） |
| key 部署／SIGHUP／0600／stdin／log 無值 | 1.7、3.2 | ✅ |
| 值不外洩／樣式掃描 | 1.10、4.7 | ✅ |
| 客戶端憑證 add／revoke／hash／401 | 1.3 | ✅（演算法見 S10；401 稽核見 S9） |
| 服務不對外／外部連不上 | 3.1 | ⚠️ 測試見 S12 |
| 板必備欄位／JSON＋HTML／用量／未知／失敗不影響租用 | 1.9 | ⚠️ 未部署列、最後成功時刻見 S8；HTML 認證見 B2 |
| 板唯讀無值＋憑證存取 | 1.9（部分） | ⚠️ 見 B2 |
| 驗收 8 項（proposal） | 4.1–4.8（入口已改 `sidecar agent run`） | ✅（4.4 含 open question 實打） |
| 銜接（MyLinuxPool／sidecar／MyAiEntry／#50） | 3.1、5.1、5.3、5.4 | ✅（wire 詞彙見 S1／S16；5.4 新增 App agent 種類；5.2 已結→MyAiEntry#147） |

---

## D. public repo hygiene 掃描（本次讀過的全部 change 檔案）

- key／token 實際值：無。secret 只以「五把各一個」「值由本人設」描述，未出現任何值、未出現 secret 名稱。✅
- 私人資訊：無人名、帳號、信箱。`mac:<主機名>` 是運行時格式佔位，非具體值（見 S17）。✅
- 池內機器名／IP／埠：無。只見「常駐 provider」「跳板」「內部介面」等泛稱；服務埠號根本未定（未寫＝不外洩，但實作定埠後注意勿寫死敏感拓撲，可順手）。✅
- 憑證檔路徑（`/run/mlp-secrets/…`、`keys.env`、`~/.config/lgw/`）：屬路徑不屬值，可留。✅
- 對外 Issue／PR 連結只出現在本審查文件與 research，不在 change 內；change 內無外部私人連結。✅
- 新版新增字串（`herdr agent send-keys …`、`agora continue session <Session> --agent <agent> --sync-period <分鐘>`、`lgw admin client add`、MyAiEntry#147）皆為命令形狀與公開 issue 號，無值、無拓撲。✅
- 結論：hygiene 通過，無必修；S17 為唯一附帶提醒。
