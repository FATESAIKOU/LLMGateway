# v0 規格可測性分析（test 視角）

- 範圍：`openspec/changes/gateway-v0/specs/` 下三份規格（agent-lease、key-vault、status-board），逐個 Scenario 判定能否只用假資料寫成自動測試，並附測試大綱（前置／動作／驗證）與缺少的錯誤路徑。
- **針對 commit 47e9cbf 之後的最新版寫**（決定 9 改寫：換把程序放在 sidecar 的 agent 宿主，本 repo 的 `lgw` 只提供租用 API 客戶端、訊號規則表與管理命令；Mac 入口改為 `sidecar agent run`；specs 與 tasks 同步修改）。
- 對照的設計與任務：`openspec/changes/gateway-v0/design.md`（決定 6～11）、`proposal.md`、`tasks.md`（任務 1.10 要求「spec 的每個 Scenario 都有對應測試」）。
- 記號：⭕ 純假資料可測／⚠️ 部分可測（標出需實機或跨 repo 的段）／❓ 規格模糊、寫不出斷言（需澄清）。「跨 repo」指實作在 MyAiStorage 的 sidecar，本 repo 只能測到介面與合約。
- 規矩：只讀 repo 與公開官方文件；未讀憑證、未跑 `gh`、未 commit。

## 0. 測試替身與 harness（本 repo 三種假＋一種假服務）

47e9cbf 後，換把執行面（herdr、agora）搬出本 repo。Migration plan 第 1 步同步改為「含測試（假 key、假上游、假時鐘）」——**假 herdr 已不在本 repo 的測試清單**，移到 sidecar 側。本 repo 的替身如下：

| 替身 | 做法 | 用途 |
|---|---|---|
| 假 key | 固定 canary 值（如 `test-key-personal-001`），只放測試程序記憶體與 tmp key 檔 | 測選定、`env` 組裝、外洩掃描（掃到 canary 即失敗） |
| 假上游 | 測試內用 `http.server` 起 stub（OpenRouter `GET /api/v1/key`、Ollama `GET /api/balance`），可控回傳／失敗／hang | 測狀態板用量、錯誤不記 key |
| 假時鐘 | 可注入的 `now()`（服務用；測試撥快時間，不真等） | 測冷卻到期、租約逾期、上限釋出、用量查詢頻率 |
| 假服務 | 錄請求、回放核准／各種拒絕／斷線的 stub（給 `lgw` API 客戶端用） | 測 tasks 2.5（客戶端對每種回覆、連不上回「無法確認」不丟例外） |

- 服務測試：tmp 目錄的 SQLite＋假 key 檔＋假時鐘，直打 HTTP 介面；`unittest`（任務 1.1 指定只用標準函式庫）。
- 訊號規則測試：`quota-signals.md` 的樣本當測試資料，規則表獨立測（tasks 2.2、2.5）。
- 跨 repo 驗收（本 repo 寫不出，見各 Scenario 標註）：sidecar 側用假 herdr／假 agora／假 `lgw` 服務跑「額度用完 → 換把 → 接回」（tasks 4.2、4.5 在 MyAiStorage 側）。
- 對實作的要求見 §5（時鐘可注入、`lgw` 不綁 herdr／agora、固定 ref 打包可裝）。

## 1. agent-lease

### Requirement：租約欄位（全服務側，皆 ⭕）

- **申請內容齊全** ⭕。前置：假 key 五把齊、政策預設、假時鐘。動作：`POST /v1/leases`（worker、角色、`claude-code`）。驗證：201，有租約 id／訂閱／`env`／`unset`／有效期限；事件紀錄全文掃 canary，零命中。
- **欄位缺漏** ⭕。前置：同上。動作：輪流缺 worker／角色／coding agent 三種申請。驗證：拒絕並指出缺少的欄位集合；事件表無新增（❓見缺口 M-10：拒絕本身要不要留「拒絕紀錄」）。
- **帶 preferences** ⭕。前置：同上。動作：帶 `preferred_models`＋`floor_model` 申請兩次（一次帶、一次不帶）。驗證：兩次選到同一訂閱；granted 事件含 preferences。
- **客戶端冒用 worker 名稱** ⭕。前置：客戶端憑證登記 `worker-a`。動作：申請 worker=`worker-b`。驗證：拒絕，理由 `client_mismatch`。

### Requirement：依政策選定訂閱（全服務側，皆 ⭕）

- **不指定訂閱** ⭕。前置：無冷卻、無租約。動作：`claude-code` 申請。驗證：選 `claude-personal`；granted 事件有選定理由。
- **一個訂閱在冷卻中** ⭕。前置：先 `exhausted` 把 `claude-personal` 送進冷卻。動作：再申請。驗證：選 `claude-company`。
- **全部都在冷卻中** ⭕。前置：兩把都冷卻（不同 `resets_at`）。動作：申請。驗證：denied，理由 `all_cooling`，附**最早**的冷卻結束時刻（斷言取 min）。
- **冷卻結束後回到第一順位** ⭕（假時鐘）。前置：`claude-personal` 冷卻中。動作：假時鐘撥過 `resets_at＋2 分鐘`後申請。驗證：選 `claude-personal`。

### Requirement：憑證由 launcher 注入（47e9cbf 後：launcher＝sidecar；本 repo 只剩服務半邊）

- **只注入一把** ⚠️（跨 repo）。本 repo 段 ⭕：服務回 `claude-company` 租約時，`env` 只有公司 canary、`unset` 含 `ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`ANTHROPIC_BASE_URL`。注入執行面（`herdr tab create --env`、`env -u`、環境斷言）在 sidecar 側測，本 repo 寫不出。
- **值不上命令列** ⚠️（跨 repo）。本 repo 段 ⭕：服務的任何回覆、事件、log 掃 canary（值只在 `env` 回覆內）。herdr argv／pane 資訊／`ps` 段在 sidecar 側；真機補（tasks 4.5，worker herdr 0.9.3）。
- **憑證來源缺失** ⭕（服務側）。前置：key 檔缺某訂閱。動作：申請（不指定＋指定該訂閱兩種）。驗證：該訂閱被跳過（狀態板「未部署」）；指定時拒絕；**絕無空或部分 `env`**（斷言 `env` 非空完整或根本無 `env` 鍵）。
- **強制階段沒有租約** ⚠️（跨 repo）。本 repo 段 ⭕：拒絕（附 `earliest_reset`）與斷線（「無法確認」不丟例外，`lgw` 客戶端測）。「不啟動 agent」在 sidecar 側。
- **只記錄階段沒有租約** ⭕（服務側）。前置：一張 launcher 租約＋一個假裝「舊 key 直開」的 agent。動作：讀 board。驗證：舊 key 的 agent 不在板上；launcher 開的必有先行租約。
- **opencode 不能拿 Claude 訂閱** ⭕（服務側）。動作：`opencode` 指定 `claude-personal`／`claude-company`。驗證：拒絕，理由 `not_allowed`。

### Requirement：訂閱切換不得安靜發生（服務半邊 ⭕，程序半邊跨 repo）

- **一個訂閱額度用完** ⚠️（拆半）。服務段 ⭕：`exhausted`（訊號＋`resets_at`）→ 原租約 superseded、`claude-personal` 冷卻到 3:47pm（＋2 分鐘）、同一次回覆給 `claude-company` 下一張。launcher 段（先結束再回報的順序、重開、`agora continue` 接回）在 sidecar 側，本 repo 只測得到「判讀規則把該訊號判為額度用完」。
- **短暫限流不換把** ⚠️（拆半）。`lgw` 規則段 ⭕：暫時性 429 判為「暫時錯誤」，用 `quota-signals.md` 樣本測。「不呼叫 `exhausted`」在 sidecar 側。
- **可用的訂閱都用完** ⚠️（拆半）。服務段 ⭕：`exhausted` 時其餘皆冷卻 → 原租約結束、拒絕 `all_cooling`＋最早結束時刻。停下 agent＋通知在 sidecar 側。
- **沒有重置時刻** ⭕（服務側）。前置：假時鐘固定。動作：`exhausted` 不帶 `resets_at`。驗證：冷卻到「現在＋該訂閱預設冷卻」（Claude 5h／Ollama 5h／Zen 無 retry-after 1h／OpenRouter 1h，見 design 決定 8）。
- **通知管道失效** ⚠️（拆半）。服務段 ⭕：換把相關事件完整（冷卻、superseded、下一張／拒絕）。「通知不回滾＋恢復後補送恰一則」在 sidecar 側（Angareion／畫面都在那邊）。
- **切換後新開 session** ⭕（純服務選定）。前置：切到 `claude-company`、`claude-personal` 仍冷卻。動作：新開申請（冷卻中一次、撥過冷卻後一次）。驗證：先拿 `claude-company`，後拿 `claude-personal`。
- **冷卻結束不打斷** ⭕（純服務選定＋sidecar 不主動換回）。服務段：選定不受冷卻結束影響（申请才重选）。sidecar 段：不因冷卻結束主動重開——跨 repo，列入合約（見缺口 N-6）。

### Requirement：Mac 與 worker 走同一套（47e9cbf 後：同一份 sidecar agent 宿主程序）

- **Mac 上開 agent** ⚠️（跨 repo）。本 repo 段 ⭕：`worker=mac:<主機名>` 參數化重跑服務測試（租約事件形狀與 worker 一致）；靜態測試鎖死本 repo 無 Mac 分支（`lgw`＋`server` 出現 `sys.platform`／`darwin` 即失敗）。「同一份程序」本體在 MyAiStorage 側驗（tasks 2.4）。新增依賴風險：v0 Mac 驗收等 sidecar 提供 `sidecar agent run`（design 決定 9 明寫）。
- **Mac 上額度用完** ⚠️（跨 repo）。服務段（回報→冷卻→下一張）⭕；重開接回在 sidecar 側。

### Requirement（ADDED）：同時租用上限（全服務側，皆 ⭕）

- **達到上限** ⭕。前置：`claude-personal` 2 張 active。動作：第三個申請。驗證：選 `claude-company`；兩把都滿時拒絕，理由 `cap_reached`。
- **結束後釋出** ⭕。前置：滿 2 張。動作：`release`（或撥過效期）一張後再申請。驗證：拿到 `claude-personal`。

## 2. key-vault（未受 47e9cbf 影響）

### Requirement：key 只保管在一處

- **部署** ⚠️。可測段 ⭕：部署腳本純邏輯——假 secret 值經 stdin 傳入、寫檔內容正確、log 掃值零命中（只許出現 secret 名稱）。需實機段：真 provider 上的 0600 擁有者、SIGHUP 後服務行為（見缺口 M-4）。
- **權限** ⚠️。可測段 ⭕：寫檔函式斷言 mode 0600（tmp 檔）。真機擁有者檢查留實機。

### Requirement：值不外洩

- **掃描** ⭕。前置：全流程跑一次（申請、exhausted、拒絕、board JSON／HTML、服務 log）。動作：以 canary 樣式掃 repo 工作樹、事件表、服務 log、板輸出。驗證：零命中（任務 1.10／4.7）。
- **錯誤時** ⭕。前置：假上游回 500。動作：觸發狀態查詢失敗。驗證：只記錯誤類型＋HTTP 碼；請求標頭與含 key 內容零出現（掃 canary）。

### Requirement：客戶端憑證

- **撤銷** ⭕。前置：兩客戶端。動作：revoke 其一後，該客戶端申請／續期／回報。驗證：全拒；另一客戶端正常。另斷言服務只存雜湊（dump `clients` 表掃原值零命中）。
- **沒有憑證** ⭕。動作：無 Bearer／錯 Bearer 打各端點。驗證：401；無租約事件（❓同 M-10）。

### Requirement：服務不對外

- **從外部連** ⚠️。動作：本機測只綁 loopback＋內部介面（連外部 IP 被拒）。「池外」段留實機。

## 3. status-board（未受 47e9cbf 影響）

### Requirement：每把 key 的必備欄位

- **有人在用** ⭕。前置：`claude-personal` 2 張 active。動作：`GET /v1/board`。驗證：兩張的 worker／角色／coding agent／開始時刻齊全，數量 `2／2`。
- **冷卻中** ⭕。前置：冷卻到 15:47。動作：冷卻中讀一次、假時鐘撥過 15:47 再讀。驗證：先顯示冷卻中＋15:47，後顯示不在冷卻。
- **逾期的租約不算** ⭕。前置：一張效期過的租約。動作：讀板。驗證：不在清單、不計數（❓見缺口 M-6：board 讀取是否觸發延後結算）。

### Requirement：用量

- **拿得到** ⭕。前置：假上游 `GET /api/v1/key` 成功。動作：等一次輪詢後讀板。驗證：OpenRouter 列有用量＋查詢時刻（假時鐘斷言輪詢間隔 5 分鐘）。
- **拿不到** ⭕。前置：假上游失敗（及無查詢介面的 Claude／Zen）。動作：讀板＋再申請租約。驗證：用量「未知」（失敗時附最後成功時刻）；租用照常。

### Requirement：唯讀且不含值

- **讀取** ⭕。前置：有租約＋冷卻。動作：持客戶端憑證讀 JSON 與 HTML。驗證：五把齊全、兩種形式一致、掃 canary 零命中；打壞方法（如 POST）無狀態變化（❓見缺口 M-11：HTML 認證方式未定義，瀏覽器測試寫不出）。

## 4. 缺少的錯誤路徑（規格沒寫，測試無斷言可對）

任務點名的四個放最前（M-1～M-4），N 系列為 47e9cbf 新增的跨 repo 缺口。

- **M-1．服務重啟中途**。`exhausted` 處理到一半（已記冷卻、未發下一張）當機，重建後原租約是 superseded 還是 active？`request_id` 去重跨不跨重啟？通知重試佇列在記憶體還是落盤？缺：當機注入測試與重建一致性斷言。
- **M-2．同時兩個 exhausted 回報**。同一租約併發回報兩次（重試／sidecar 與 Mac 同時監看）：第二個是冪等成功、拒絕、還是發出第二張租約（違「同一 agent 不同時兩張」）？同訂閱不同租約同時回報，`earliest_reset` 與 next 是否一致？缺：併發測試與「exhausted 冪等」定義。
- **M-3．冷卻剛好到期的邊界**。`now == resets_at＋2min` 那一刻算冷卻中還是可用（≤ vs ＜）？比較用牆鐘還是單調鐘（NTP 回撥會不會讓冷卻中的訂閱復活）？缺：邊界值測試與時鐘種類定義。
- **M-4．key 檔被 SIGHUP 換掉時正在出租**。寫一半的檔被讀到怎麼辦（原子替換 `rename` 有無）？重讀失敗（損壞／缺行）時沿用舊 key 還是全停？已發出的租約用新舊哪把？缺：半寫檔、損壞檔、空值（`""` 算缺失嗎？規格只說「絕不回傳空的或部分的 env」，測試要斷言空字串視同缺失）。
- **M-5．拒絕理由的優先順序**。混合狀態（A 冷卻中、B 達上限）拒絕理由是 `all_cooling` 還是 `cap_reached`？指定訂閱時它在冷卻／達上限，理由為何？`cap_reached` 附不附 `earliest_reset`（規格只定義 `all_cooling` 附）？缺：理由決策表。
- **M-6．逾期結算的「任何往來」**。design 說「下一次任何往來時補記 expired」：讀 board 算不算往來？`report_usage` 算不算？缺：定義，否則「逾期的租約不算」測不出。
- **M-7．`request_id` 去重**。範圍（全域還是按客戶端＋路徑）？TTL（會不會無限增長）？跨重啟？併發同 id？缺：定義＋測試。
- **M-8．revoke 後既有租約**。規格只說「之後的申請、續期、回報都被拒」：該 client 已持有的租約是否立即失效、用量是否繼續？缺：定義。
- **M-9．switch 中途失敗的孤兒租約**。`exhausted` 成功但重開失敗（herdr 掛、agora 未寫回、服務斷線）：新租約已發出但 agent 沒跑，誰回收？缺：補償／逾時回收定義（效期 12h 太久，建議 short-lease 或啟動確認）。（47e9cbf 後此項屬 sidecar 側＋服務合約兩邊。）
- **M-10．拒絕留不留紀錄**。proposal 驗收說「每一次出租、冷卻、切換、拒絕都留下紀錄」，spec 卻說欄位缺漏／無憑證「不留下（租約）事件」。拒絕要不要留「拒絕紀錄」（稽核）？缺：澄清，否則 PMO 追溯斷鏈。
- **M-11．`/board` HTML 認證方式**。「經同一套客戶端憑證存取」：瀏覽器如何帶 Bearer？測試寫不出。缺：定義。
- **M-12．`below_floor` 與模型政策**。`below_floor` 在 v0 不可達，但拒絕碼表應有完整性測試；design 說 openrouter 只許 `:free`，spec 無對應 Scenario（申請付費模型是拒絕還是放行？）。缺：補 Scenario。
- **M-13．輸入的形狀檢查**。`role` 不透明字串：長度／字元上限為何（HTML 轉義測試要先有上限）？`preferences` 格式錯誤？`usage` snapshot 非法？`resets_at` 非法時間（過去／毫秒秒混用）？`model` 缺席與政策模型清單的關係？缺：驗證規則表。
- **M-14．租約生命週期的非法轉換**。renew 已 expired／superseded／不存在／別人的租約；release 不存在／別人的／重複；exhausted 不存在／別人的租約 id。缺：每種的回碼定義。
- **M-15．用量查詢的失敗形狀**。上游 hang（不斷線也不回）會不會卡住租用（需逾時＋隔離測試）？OpenRouter 401（key 失效）是否該把狀態板轉「未部署」？Ollama `balance` 回新舊哪種欄位（tasks 4.4 已列實打）？缺：逾時定義＋key 失效語義。
- **M-16．並發與持久化**。並發申請搶最後一個上限名額（原子性）；events 單調序號併發；磁碟滿／event 損壞時重建；policy 檔損壞時服務行為。缺：定義＋故障注入測試。
- **M-17．退回路徑**。`herdr tab create --env` 在 0.9.3 驗不過時的「0600 暫存 env 檔、讀完即刪」在 spec 無 Scenario（design 風險段有）。47e9cbf 後此項屬 sidecar 側，但 tasks 4.5 仍列驗收；缺：補 Scenario（含 crash 後殘留檔掃 canary），並寫明歸屬 sidecar。
- **M-18．Open Questions 的占位**。`agora continue` 的 id 前綴寫法、opencode 變數名、Ollama 相容位址在實作前是 ❓：env 對照表測試先寫形狀斷言（鍵集合），值等實打後填（tasks 2.5、4.4 已列）。
- **N-1．三段終止的超時與範圍**（sidecar 側，新）。`send-keys ctrl+c` → 5 秒 → process group SIGTERM → 5 秒 → SIGKILL → `pane close`：等待用哪個時鐘（假時鐘可測性在 sidecar 的要求）；process group 誤殺範圍（同 pane 無辜程序）；agent 忽略 ctrl+c 直接走到 SIGKILL 的事件紀錄。缺：sidecar 側測試＋與本 repo 無關，但換把 Scenario 的一半斷言落在這裡。
- **N-2．Esc 次數差異**（sidecar 側，新）。「打斷一輪不用結束 agent 時用 Esc（Claude Code 1 次、opencode 2 次）」寫在 design 而不在 spec：編碼位置、測法、agent 改版失效時的迴歸資料，與訊號規則表同等對待。缺：補 spec 或註明歸屬。
- **N-3．`agora continue` 確切形狀**（合約，新）。design 已寫 `agora continue session <Session> --agent <agent> --sync-period <分鐘>`、不用 `--fork`：應有合約測試（斷言呼叫形狀、禁用 `--fork`）；id 前綴的 Open Question 仍在，解掉前接回測試是 ❓。
- **N-4．`lgw` 固定 ref 的版本漂移**（合約，新）。sidecar 從固定 ref 裝 `lgw`（tasks 2.3、design 決定 9）：服務加了新拒絕碼而 sidecar 的 `lgw` 規則表還在舊版，判讀／交接會靜默過時。缺：版本相容測試（拒絕碼表快照、規則表版本欄）或發版紀律。
- **N-5．「前景或常駐只差」宣稱的驗證法**（sidecar 側，新）。Mac `sidecar agent run` 與 worker 常駐「換把程序完全同一份」：sidecar 側以同一測試參數化兩種模式；本 repo 只能鎖死自己這邊無 Mac 分支，不能證明那邊。
- **N-6．「冷卻結束不打斷」的一半在 sidecar**。服務選定天然不打斷；但「不因冷卻結束主動重開」是 sidecar 迴圈的行為，需合約寫明（冷卻結束事件是否通知 sidecar？若通知了，sidecar 必須忽略）。缺：合約一行。

## 5. 給實作的約束（否則上面的 ⭕ 會退化）

1. 服務的「現在時刻」必須經可注入的 clock 介面；直接呼叫 `time.time()` 之處，冷卻／逾期／輪詢測試只能真等。（sidecar 的 5 秒終止等待同理，是 sidecar 側的要求。）
2. `lgw` 不綁 herdr／agora（47e9cbf 後的硬性要求）：以測試鎖死——`lgw` 內出現 `subprocess`／`herdr`／`agora` 引用即失敗；API 客戶端只用標準函式庫 HTTP。
3. 上游查詢（`GET /api/v1/key`、`GET /api/balance`）走可注入的 HTTP 層，預設逾時，失敗路徑只記類型＋狀態碼（key-vault「錯誤時」Scenario 即測之）。
4. `env` 的值只活在記憶體；測試用 canary＋全輸出掃描當迴歸網。
5. 本 repo 鎖死自己這邊無 Mac 分支；Mac／worker 同程序的證明在 sidecar 側（tasks 2.4），本 repo 的 `mac:` 參數化服務測試只覆蓋服務半邊。
6. `lgw` 打包成可從固定 ref 安裝（tasks 2.3）：測試至少覆蓋「從 ref 裝得起來、無標準函式庫外依賴」；拒絕碼與規則表加版本快照，防 N-4 漂移。
