# v0 程式碼審查：leasing／store／http／client（對 design 決定 4、6、8、11、12 與 specs）

> 範圍：`lgwd/leasing.py`、`lgwd/store.py`（impl）、`lgwd/http.py`、`lgw/client.py`（test）。
> 對照：design 決定 4（客戶端身分）、6（API 與錯誤表）、8（選把冷卻）、11（狀態板）、12（一致性邊界錯誤處理）；
> specs agent-lease、key-vault、status-board。另順帶確認 `lgwd/errors.py`、`lgwd/clock.py`（被引用的契約）。
> 規矩：只讀 repo；未讀憑證、未跑 `gh`、未 commit。只寫本檔，不改程式。
> 每條標【必修】／【建議】並附檔名:行號。結論：**必修 2 條，建議 7 條**。
> 行號以 3d34c02（scope/gateway-v0 最新提交）為準。E 節複驗工作樹未提交的同步修復（R1／R2 修復中，詳 E）。

## A. 必修（2 條）

### R1. 去重表存了含 env 的完整回覆，key 值落盤留 7 天【必修】

- 位置：`lgwd/leasing.py:404-407`（request_lease 成功回覆 `{"lease": lease}` 含 env）、`lgwd/leasing.py:502-505`
  （exhausted 下一張同）；`lgwd/store.py:211-220`（`dedup_put` 把 body 原樣 json 存庫，保留 7 天、跨重啟）。
- 問題：
  1. key-vault spec 寫「值只出現在核准租約時給客戶端的那一次回覆，以及 agent 的環境」，且「MUST NOT 出現在
     …事件紀錄」。dedup 表雖不是 events 表，但同樣是服務 SQLite 裡的持久紀錄，key 值在內最長留 7 天，
     與「不進紀錄」的約束相違。
  2. tasks 1.12／4.7 的 canary 掃描只列「事件、log、狀態板輸出」，掃不到 dedup 表——即使寫了掃描測試，
     這個外洩也測不出來。
  3. 附帶正確性問題：輪替 key 後，重播舊 request_id 拿回的是**輪替前**的舊 key（`_lease_obj` 當時組的 env），
     客戶端會拿舊值去打上游。
- 修法（不動介面）：dedup 存「去 env 版」回覆（或只存 `lease_id`＋狀態）；重播命中時用 `KeyStore` 現值經
  `build_env` 重組 env 再回。這樣值永不落盤，且重播永遠拿到當前有效的 key；canary 掃描擴到整個 DB 檔。
- 嚴重度：必修（spec 條文違反＋掃描盲區）。

### R2. 認證排在去重之後：撤銷後重送仍回舊成功回覆【必修】

- 位置：`lgwd/leasing.py:379-382`（request_lease）、`:420-423`（start）、`:463-466`（exhausted）、
  `:545-548`（renew）、`:577-580`（release）、`:605-608`（usage）——六處同一模式：先 `_dedup_hit`，後
  `_require_live_client`。
- 問題：
  1. 被撤銷的客戶端拿舊 request_id 重送，會拿到當初的成功回覆（request_lease／exhausted 的回覆**含 env key 值**），
     違反 key-vault「撤銷後申請、續期、回報都被拒」。
  2. 違反自家 docstring（`lgwd/leasing.py:63`）：「401 不進 dedup（客戶端集合可能變化，重送應重判）」——
     儲存面做到了（401 不寫 dedup），但讀取面沒做到（重送不重判身分）。
  3. 違反 design 決定 12（`design.md:222`）：「逾期結算：任何一次**經過認證的**請求…都先補記」——現行順序是
     `_settle` 先跑，**未認證**的請求（含 401）也會觸發結算寫入。
- 修法：六處一律改為 `_require_live_client` → `_settle` → `_dedup_hit` → 業務邏輯。一次同時滿足撤銷語義與結算範圍。
- 嚴重度：必修（撤銷是條款防線的一環；且與 design 明文不符）。

## B. 建議（7 條）

### S1. `/v1/whoami` 未在 design 決定 6 出現【建議】

- 位置：`lgwd/http.py:88-91`（路由）、`:140-141`（分發，不碰租約）；`lgw/client.py:208-210`（`whoami()`）。
- 問題：決定 6 的路徑表沒有 `/v1/whoami`；http 與 client 兩邊自洽（docstring 皆有載），但 design 沒載。風險低
  （要有效憑證才回，且只回客戶端名稱），純粹是文件缺口。
- 修法：design 決定 6 補一行，或刪掉該端點。二選一。
- 嚴重度：建議。

### S2. request body 無大小上限【建議】

- 位置：`lgwd/http.py:43-58`（`_read_json` 按 Content-Length 全讀進記憶體）。
- 說明：401 判定在讀 body 之前（`:132-136`），未認證大 body 打不進來，是對的；但**已認證**客戶端仍可用
  超大 body 灌爆單連線記憶體（每連線一執行緒）。
- 修法：Content-Length 超過上限（如 1MB）直接 400／413，不讀 body。
- 嚴重度：建議（信任邊界內，純粹縱深）。

### S3. 未預期錯誤無 traceback 可查【建議】

- 位置：`lgwd/http.py:155-158`（500 只記狀態）；`lgwd/leasing.py`、`lgwd/store.py` 全檔無 logging（只有事件 `store.log`）。
- 說明：400／401 的「記服務 log」由 http access 行（方法／路徑／狀態）滿足，無缺口；缺的是 500 那條路——
  線上除錯時只知道有 500，不知道哪裡炸。
- 修法：500 分支加一條記錯誤種類的 log（不含值；已有 `_LOG` 可用）。
- 嚴重度：建議。

### S4. 板 count 只計 active，cap 計 granted＋active，顯示會偏低【建議】

- 位置：`lgwd/leasing.py:627-637`（board 取 `actives_for_sub` 即 status='active'）；`lgwd/store.py:161-167`
  （`count_unfinished_for_sub` 計 granted＋active）。
- 說明：granted 未確認租約佔上限名額（spec start 章節明定，正確），但板上「使用中數量」不含它；10 分鐘窗口內
  可能顯示 1／2 而實際已滿。兩邊各自正確，只是口徑不一致。
- 修法：board 另列 granted 未確認數，或在該列註明。二選一。
- 嚴重度：建議。

### S5. exhausted 下一張不帶 preferences【建議】

- 位置：`lgwd/leasing.py:65`（docstring 自述）、`:489-491`（下一張申請傳 `None, None, None`）。
- 說明：v0 選把不看 preferences，無行為影響；但 `below_floor` 啟用後，換把鏈會丟失 floor 資訊。
- 修法：下一張沿用原租約的 preferences（granted 事件本來就有存），或在啟用 below_floor 時一併處理。
- 嚴重度：建議（v0 不擋，記下來即可）。

### S6. `Result` 預設 repr 含 data（含 env）【建議，本 repo 不改程式】

- 位置：`lgw/client.py:35-41`（`Result` dataclass 無自訂 `__repr__`）。
- 說明：值活在記憶體是設計如此；但 sidecar 若把 Result 整個拿去 log／印 pane，即外洩。`LeaseClient.__repr__`
  已 exemplar（`client.py:61-66` 只印路徑不印 token）。
- 修法：請 test 側在 client.py docstring 加「勿 log／print Result 全體」警語；sidecar 側同理只取用欄位。
- 嚴重度：建議（跨 repo 提醒）。

### S7. `_deny` 內 dedup 分支現行路徑永不觸發【建議】

- 位置：`lgwd/leasing.py:221-222`（`if ctx.get("request_id")` 才寫去重）；六處呼叫皆經 `_req_ctx`，
  無 request_id（例 `:244`），實際去重由各方法外層補做（例 `:399-403`）。
- 說明：無行為錯誤，但後人會誤以為 _deny 自己會去重。
- 修法：註明預留，或把 request_id 納入 `_req_ctx` 統一處理。二選一。
- 嚴重度：建議（可讀性）。

## C. 已驗無需改（結案備查）

- **交易與超賣**：`txn()` 單連線＋RLock＋`BEGIN IMMEDIATE`（`store.py:82-99`），檢查與核發同一筆交易
  （`leasing.py:376-408`、`460-506`）；`exhausted` 的設冷卻＋superseded＋發下一張原子（同上）；併發搶名額序列化，
  不超賣。ApiError 照常 commit（denied 事件留下，符合設計），非預期才 rollback。
- **拒絕順序**：400 → 401 → 403 → 404（先查存在再談 owner，`leasing.py:344-350`）→ 409，
  409 內 `not_allowed` → `instance_busy` → `cap_reached`／`all_cooling`（`leasing.py:240-280`），與 design 決定 6
  （`design.md:120`）一致；`earliest_reset` 只在 `all_cooling` 附帶，`errors.py:61-66` 強制＋各呼叫點傳 None 雙保險。
- **等於結束即結束**：`store.py` 無此邏輯，`leasing.py:225-232`（`now < until` 才算冷卻中）；RFC 3339 固定 `...Z`
  秒精度（`clock.py:43-55`），字串 `min()` 取最早重置（`leasing.py:278`）有效。
- **憑證**：`compare_digest` 逐一比對非撤銷雜湊（`leasing.py:133-144`）；撤銷連帶未結束租約（`:106-123`）；
  只存 SHA-256（tasks 1.3 指定，`leasing.py:86`）；重發覆蓋舊雜湊（`:88-99`）；`list_clients` 不含雜湊（`:125-131`）。
- **狀態板**：輸出全經 `_esc`（`http.py:175-229`，含 dict／list、None→未知）；HTML 只綁 `127.0.0.1`，
  非 loopback 直接丟錯（`:232-235`）；非 GET 一律 404（`:265-280`）；log 只有方法／路徑／狀態，
  query 不進路由與 log（`:126`）；board 500 不洩漏（`:250-253`）。
- **客戶端**：token 不進 repr（`client.py:61-66`）；request body 無秘密（`:135-203`）；unreachable 不丟例外（`:87-88`）；
  兩種拒絕形狀皆解析（`:100-116`）；usage 不帶 request_id（`:197-203`，與 design 表一致）。
- **前次複查殘留**：C1（granted 租約的 exhausted／renew）已由 `lease_not_confirmed` 明確覆蓋
  （`leasing.py:468-471`、`550-551`），無需處理；C5（earliest_reset 範圍）已由 `errors.refusal` 強制，無需處理。

## D. 範圍外註記（不斷言，只說明）

- 用量輪詢排程與 server 組裝（`lgwd/main.py`、`lgwd/usage.py`）不在本次四檔內；board 的 `usage` 欄位由該側填寫
  （`leasing.py:620` 留 None），`_render_board` 對 dict／None 皆有處理，不因該側未到而壞。
- `main.py` 以 grep 可見的 logging 點（reload 失敗警告、啟動錯誤印 stderr）只涉固定字串與例外類型，
  未見值通道；未全文審查，以上僅供參考。
- 公共 hygiene：四檔無 key 值、私人資訊、機器名、埠；新增的 `/v1/whoami` 只回客戶端名稱（S1 已列）。

## E. 複驗：工作樹對 R1／R2 的同步修復（未提交）

> 寫本審查時發現 `lgwd/leasing.py` 在工作樹有未提交修改（`git status` 唯一的 `M`；`git diff` 43+/3-），
> 內容正好是 R1／R2 的修復（`_redact`、`_replay`、五處 auth 前移）。以下驗證修復本身。
> diff 證據：hunk 止於 request_lease／start／renew／release／usage 五處，**沒有 `exhausted` 方法的 hunk**。

### E1. R1 修復方向正確，但 `exhausted` 重播漏接（功能迴歸＋殘留繞過）【必修】

- 已驗通過：`_redact` deepcopy 後清空 env 值、留變數名與 unset；`_replay` 用 DB 真值（subscription／agent／model）
  ＋現行 KeyStore 重組 env，租約已結束回 409、key 撤下回 not_allowed（不記新事件，正確——重播不是新申請）；
  拒絕形狀（無 lease slot）原樣回；build_env 失敗 fail-closed。輪替後重播拿舊 key 的問題順帶修好。
- **殘留 F1**：`exhausted` 仍是舊程式——先 dedup 後 auth，且直接 `return hit`。而存檔現已是 redacted 版，
  後果有二：(a) 客戶端重送 exhausted 會拿到**空字串 key**（迴歸：原先是完整重播）；(b) 撤銷後重送 exhausted
  仍回 200（R2 在此路徑沒修到）。
- 修法：`exhausted` 照其他五處改為 auth 先行，並把重播換成 `_replay(cur, stored, "next")`。
- 嚴重度：必修（(a) 是修復引入的迴歸，(b) 是原 bypass 殘留）。

### E2. R2 修了 5/6；`_settle` 仍在認證前【建議微調】

- 五處已改為 `_require_live_client` 在 `_dedup_hit` 之前，撤銷後重送回 401，符合 key-vault 撤銷語義。
- `_settle` 仍在最前：未認證請求也會觸發結算寫入，與 design「經過認證的請求先補記」字面仍差一行。
  無害（結算不含值、不涉授權），二選一：把 `_settle` 移到 auth 之後，或 design 加註現行順序。
- 嚴重度：建議。

### E3. 修復帶來的合約變化，5.1 交接應收【建議】

- 重播不再是位元組一致：grant 類重播 env 隨當下 KeyStore；租約已結束的重播從 200 變 409（需重申請）。
  sidecar 重送邏輯須按此寫，建議 5.1 加兩行（重播 409 → 重申請；重播 refusal 原樣）。
- `_replay` 的 not_allowed 不記 denied 事件是對的（重播非新申請），同請在 5.1 寫明，避免 sidecar 誤告警。
- 嚴重度：建議。
