對應工單：#76（c）；上層 MyPMO#163（2026-10-26 本人裁定升為最優先）。查證日期 2026-10-26。動機見 proposal.md 的 Why。

引用規則：`檔案:行號` 指本 repo（`feat/76-model-cooldown`，自 main `c45f0b5`）；標「側車」的引用指 private repo `MyLinuxPool` 的 `modules/sidecar/src/sidecar/`（本機唯讀副本，只讀、不在本 change 改），引用時只寫檔名與行號。標「推論」的是判斷，不是文件寫死的。

**本版（改版 1）的基準**：本人 2026-10-26 裁定「冷卻只是讓叫人方省麻煩、知道下次何時再叫；他真要再叫，Gateway 也擋不了，也沒必要擋」。因此初版的「提前解除（A1）」「為冷卻做呼叫方身分（A2）」整段移除，resolve 的 409 `cooling` 整條移除；設計目標從「只排除那個模型」改成「Gateway 記錄並告知，不排除、不代選」。初版與本版的差異清單見 `.work/REPORT-DESIGN-2.md`。

## Context

現況（全部已查證）：

- 冷卻只有一層、以 subscription 為單位：`cooldowns` 表 `subscription TEXT PRIMARY KEY, until TEXT NOT NULL`（`lgwd/store.py:21-24`）；寫入是 UPSERT、只在新值較晚時更新（`lgwd/store.py:88-95`）；`register_cooldown` 另外先讀舊值再取較晚者（`lgwd/registry.py:226-230`），所以「取較晚」有兩道，行為一致。
- 登錄只有 `until` 必填、未知欄位忽略（`lgwd/registry.py:213-233`、`openspec/specs/subscription-registry/spec.md:140-149`：「未知欄位 MUST 忽略」「重複登錄時取較晚的 `until`」）。
- `resolve` 挑把的第 7 步是「排除冷卻中的（`cooldown` 有未來的 `until`）；全部冷卻 → 409 `cooling`（body 帶 `until`＝最早的恢復時刻）」（`openspec/specs/subscription-registry/spec.md:317`），實作在 `lgwd/registry.py:148-161`：逐候選讀整把冷卻，全部冷卻時回 `to_rfc3339(earliest)`。
- `list`／`get` 共用的 row 由 `_item` 組（`lgwd/registry.py:350-375`）：`item["cooldown"] = cooldown`、`item["available"] = deployed and cooldown is None`；`_cooldown` 把「過了 `until`（含相等）」視為 null（`lgwd/registry.py:377-382`）。
- `resolve` 的 `meta` 目前明寫「MUST NOT 含 `cooldown`／`available`／`available_again_at`」（`openspec/specs/subscription-registry/spec.md:237`）；理由是「200 已代表可用，這三個恆為 null／true／null」。冷卻變成提示後，這個前提不再成立（見決定 3）。
- 錯誤碼表在 `lgwd/errors.py:5-18`；`model_not_allowed` 目前只給 resolve 用（`lgwd/errors.py:60-62`），`cooling` 帶 `until`（`lgwd/errors.py:80-82`）。
- HTTP 只有四個路由；`do_DELETE` 已經接了但沒有 DELETE 路由，會落到 404（`lgwd/http.py:3-8`、`lgwd/http.py:86-102`、`lgwd/http.py:138-139`）。
- **沒有應用層認證**：`lgwd/http.py:9` 寫「沒有應用層認證（本人選 (a)）；存取控制只靠 SSH」；`lgwd/store.py:8` 寫「絕不存 key 的值，也不記客戶端（沒有應用層認證）」；`docs/deploy.md:17` 寫「**不做應用層認證**，存取控制只靠 SSH 與本機綁定」，客戶端（Mac、worker 的 sidecar、手機）都走同一條隧道到同一個 API 埠（`docs/deploy.md:19-23`）。本 change 不再為冷卻處理這件事（決定 4），但仍要記得：任何能打到 API 的人都能寫一筆提示。
- 呼叫方送的東西：`lgw/signals.py:48-59` 的 `to_cooldown_body()` 只產生 `{"until": ...}`，判讀不出重置時間時由呼叫方用「現在＋1 小時」（`lgw/signals.py:8-12`）；`lgw/client.py:153-159` 的 `cooldown(id, until)` 同理。
- 側車（只讀）：`_cooldown_until` 送單一 `until`，driver 的 `resets_at` 不可用時用 `QUOTA_FALLBACK_SECONDS = 3600.0`（`core.py:139`、`core.py:1789-1808`）；`SubscriptionSource.cooldown(subscription_id, until)` 只有兩個參數（`protocols.py:1163-1173`），HTTP 版送 `{"until": until}`（`sources.py:420-440`、`sources.py:444-456`）；`DriverState` 只有 `window`／`resets_at`／`code`／`reason`，**沒有 model**（`protocols.py:465-471`），opencode 的 `_apply_failure` 建 quota 狀態時也不帶模型（`drivers/opencode.py:783-797`），但 handle 上有實際啟動的模型（`handle.expected_model`，`drivers/opencode.py:501-505`）；換把時先登錄冷卻、再用 `exclude=<剛用完的那把>` 重新 resolve（`swapping.py:214-215`、`swapping.py:255-262`）。
- 側車怎麼讀冷卻：`Subscription.from_json` 只認 `cooldown`，忽略不認得的鍵、且 `schema > 1` 會整列拒收（`protocols.py:352-381`、`protocols.py:358-362`）；`is_deployed()`＝`available or bool(cooldown.until)`（`protocols.py:384-391`）；選把用 `s.available`（`selectors.py:23-28`）；重新開啟的時刻用 `cooldown.until`（`swapping.py:691-708`）；resolve 的 409 `cooling` 對應 `CoolingError`，用來排自動重開（`swapping.py:256-262`、`picking.py:476-481`）。
- 現在被測試釘住的 409 行為：`tests/test_registry.py:589`（釘住那把冷卻中）、`:689`（唯一一把冷卻中）、`:706`（全部候選冷卻中）、`:1328`（非 200 不取用量）；`tests/test_http.py:218`（錯誤碼對照）；`tests/test_client.py:234`（client 判讀 cooling）。實作時這些要一起改（決定 8）。
- 出廠政策：`opencode-zen` 的 `models.allow` 是 `["*-free", "big-pickle"]`、`default` 是 `mimo-v2.6-flash-free`（`policy/default.json`）。所以「同一把底下多個免費模型」是常態，不是特例。

## 目標與非目標

目標：

1. 冷卻以**模型**為單位記錄：一個模型的 429 只留下「那個模型到什麼時候再叫比較省事」這一筆，不動整把。
2. `resolve`／`get` **不再因冷卻拒絕**：冷卻不影響候選、不影響排序；照常選把、給 key、給 `env`，並在 `meta` 附上冷卻資訊。
3. `list` 看得到整把與每個模型的冷卻，過期的不列；`available` 的定義不變。
4. 不帶 `model` 的既有呼叫方（舊 sidecar、PM 的 curl、`lgw.client.cooldown`）行為完全不變。
5. 舊 DB 直接升級、可退版。

非目標（見 proposal「不做」）：不做提前解除、不做呼叫方身分、不改政策形狀、不代選模型、不改 `until` 上限、不做窗口、不加定時清理、不動 `usage`／`available_again_at`。

## 決定 1 登錄：`POST /v1/subscriptions/{id}/cooldown` 的 body 多選填 `model`

**決定**

- body：`{"until": <RFC 3339 UTC>, "model": <選填，供應商自己的模型 id>}`。
- 有 `model`：只登錄（把、模型）那一筆；沒有：整把（現行語意）。兩種都回 204、無 body。
- 同一（把、模型）重複登錄 → 取較晚的 `until`；較早的忽略（與整把同一條規則，`lgwd/store.py:88-95` 的 UPSERT 直接沿用）。
- 整把與模型兩層**各自獨立**：登錄整把不會刪掉模型列，登錄模型也不會動整把列（各自到期、各自失效）。
- `model` 的形狀：有給就必須是**非空字串**，否則是 400 `bad_request`（欄位在、值不對是形狀錯；不當成「沒給」——那只會悄悄變成整把冷卻，正是這次的 bug）。
- `model` 的值：供應商自己的模型 id，**不帶 agent 前綴**（與 resolve 的 `model` 同一條規則，見 `openspec/specs/subscription-registry/spec.md:46`）。Gateway 不正規化。
- `model` **含 fnmatch 萬用字元（`*`、`?`、`[`）→ 400 `bad_request`**：它不可能是供應商 id，而且 `_covered` 會把它當樣式放行，存進去是一筆永遠命不中的列（靜默失效）。
- `model` 要**被該把的 `models.allow` 涵蓋**（與 resolve 第 4 步同一個 fnmatch，`lgwd/registry.py:63-65` 的 `_covered`）。不涵蓋 → **400 `bad_request`**（不是 403 `model_not_allowed`），不登錄。
- `until` 的驗證完全不變（必填、RFC 3339、不是過去、不超過 `until_max_ahead_seconds`＝8 天）：模型冷卻也照同一個上限，不另開旋鈕。
- 驗證順序照現行實作、不重排：未知 id → 404；缺 `until` → 400 `bad_request`；`until` 格式／範圍 → 400；最後才是 `model` 的形狀與 allow → 400。
- 未知欄位照舊忽略（含 `window`、`resets_at`）。

**理由**

- 選填欄位、沒有就退回整把：這是唯一讓「舊呼叫方一行都不用改」又「新呼叫方能表達模型」的形狀。舊 lgwd 會把 `model` 當未知欄位忽略 → 退回整把，方向是保守的（比今天嚴、不比今天鬆）。
- 用 body 不用 query：`until` 已經在 body，`model` 是同一筆登錄的另一個欄位；而且 cooldown 路由現在完全不看 query（`lgwd/http.py:145-147` 只用 `split.path` 路由），改用 query 等於同時多一套「空白值＝沒給」的解讀規則（那是 resolve 專屬的，見 `lgwd/http.py:105-119`）。
- **要驗 allow，但回 400**：
  - 不驗的話，sidecar 只要把 `opencode/glm-5.3-flash-free`（帶前綴）送上來，Gateway 就會存一筆永遠命不中的列——冷卻看起來登錄成功、實際上完全沒效果，然後同一個模型繼續被狂打。這種「靜默失效」比一個 400 難查得多。
  - 不用 403：`model_not_allowed` 在這個系統的語意是「resolve 的政策拒絕」（`lgwd/errors.py:60-62`）。登錄冷卻不是呼叫方在要求政策放行，而是在自報「我剛剛用了這個模型、它到 X 時刻再用比較好」；模型不在 allow 是呼叫方送錯值（打錯字、帶前綴），是**輸入錯誤**，不是政策拒絕。另外側車的 `_mapped` 把**所有 403 都記成 `agent_not_allowed`**（`sources.py:549-550`），403 會讓 log 與控制面回報指錯方向；400 會走 `SourceError(code)` 原樣保留碼（`sources.py:557-560`），比較誠實。
- 拒絕萬用字元：`_covered("*-free", "*-free")` 會成立，存進去卻永遠對不上一個真實模型；A5 的目的就是「避免打錯字」，這種形狀要一起擋。
- 兩層各自獨立、互不刪除：整把登錄不知道是哪個模型用完的（現在的 body 就是這樣），去清模型列等於消滅真實資訊；反過來，模型登錄去覆蓋整把會讓「整把」這個較強的宣告被一個較弱的宣告蓋掉。獨立、各自到期，語意最單純，也沒有「順序不同結果不同」的坑。

**被否決的選項**

1. **`model` 放 query（`?model=`）**：與 resolve 的 query 規則混在一起、要多寫一套空白值規則；body 已經有 `until`，多一個欄位最省。
2. **另開 `POST /v1/subscriptions/{id}/model-cooldowns`（複數、陣列）**：多一個路由與一種形狀，sidecar 要判斷打哪一個；而「哪一個模型」本來就是同一把的同一種狀態。
3. **`model` 照收不驗**：靜默失效（見上），而且 `list` 會被 typo 的模型 id 污染。
4. **沿用 403 `model_not_allowed`**：與 resolve 的政策拒絕混在一起；側車會把它記成 `agent_not_allowed`（`sources.py:549-550`），排查時會往錯的方向走。
5. **把 agent 前綴正規化掉**（`opencode/x` → `x`）：與 resolve 明確「不正規化、帶前綴就 403」的既有決定衝突（`openspec/specs/subscription-registry/spec.md:46`、`:334-336`）；兩邊不一致的話，呼叫方永遠搞不清哪個介面吃前綴。
6. **登錄整把時清掉該把的模型列**：副作用太大、會縮短真實冷卻，且讓呼叫順序決定結果。
7. **`model` 給空字串時當成「沒給」**：太危險——「我送了一個 model 但打錯成空字串」與「我要整把冷卻」是兩件事。

## 決定 2 resolve 的選把：冷卻完全不參與（不排除、也不排序）

**決定**

- 刪掉現行第 7 步「排除冷卻中的候選」（`lgwd/registry.py:148-161`、`openspec/specs/subscription-registry/spec.md:317`）。
- 選把只剩：釘住 → provider → agent → model 守門／推導 → `exclude` → key 已部署 → `priority` 最小、同分 id（原第 8 步變成第 7 步）。
- **冷卻不影響候選，也不影響排序**：整把的 `until`、任何模型的 `until` 都不參與 `min()` 的鍵；`priority` 與 id 是唯二的排序鍵。
- 因此：指定 A 而 A 冷卻 → 200，`meta.model=A`、附資訊；指定 B 而 A 冷卻 → 200，`meta.model=B`；整把冷卻 → 200，附整把 `until`；全部候選冷卻 → 200，選 `priority` 最小者，附資訊。
- 既有的其他拒絕完全不動：未知 provider／subscription → 404；agent 不允許 → 403；model 不在 allow → 403；`exclude` 後沒有候選 → 409 `no_candidate`；key 未部署 → 409 `not_deployed`；推不出模型 → 400 `no_model`。
- 呼叫方要「避開冷卻」只能自己看 `list` 或 resolve 的 `meta`，或直接指定 `model`／`subscription`／`exclude`。Gateway 不代選。

**理由**

- 本人講的是「Gateway 擋不了，也沒必要擋」。**排序**也是一種「替呼叫方決定」：如果同 priority 時沒冷卻的優先，Gateway 就開始用一個會隨時間變動的狀態改變 `meta.subscription`——同一個請求在不同分鐘會拿到不同把，稽核對不上，呼叫方也看不出是為什麼（`meta` 又不會說「我幫你跳過了一把」）。最簡單、最可預測的規則就是：選把只看政策與請求，冷卻只是回覆裡的資料。
- 冷卻已經是「提示」，提示放在回覆裡就夠了；讓它同時改變選把，等於同一個訊號有兩種作用，之後很難回答「為什麼這次選的是這把」。
- 選把步驟愈少愈好：刪掉第 7 步後，`priority` 之前不再需要讀 `cooldowns`／`model_cooldowns`，resolve 的資料庫讀取只剩「回覆時附資訊」與稽核，語意與成本都更單純。

**被否決的選項**

1. **同 `priority` 時沒冷卻的優先**：引入一個「只在有冷卻時才生效」的隱性排序；`priority` 是政策順序，冷卻是瞬時狀態，兩者混在一起會讓「同一個請求為何選不同把」沒有單一答案。同分本來就有 id 決勝，夠了。
2. **冷卻候選一律排到最後（跨 priority）**：這就是 Gateway 代選——會把 `priority` 較高但正在冷卻的把跳過，選一個呼叫方沒要求的較低 priority 把；`meta.subscription` 隨狀態漂，稽核無法回答「為什麼不是 policy 的第一名」。
3. **只在「全部候選都冷卻」時回 409**：留一個例外就等於留一個閘門；本人明說不擋，且側車會繼續寫 409 分支。
4. **沒指定 model 時用「同把還有哪個模型沒冷卻」代選**：那是代選模型，既有決定已否決（`openspec/specs/subscription-registry/spec.md:314`、`:375-377`）。

**已知的介面語意變動（要讓本人知道）**

- 現行 scenario「同 provider 兩把，一把冷卻中 → 選另一把」（`openspec/specs/subscription-registry/spec.md:322-324`）會反過來：**選原本 priority 較高的那一把（即使它冷卻中），並附上冷卻資訊**。這是「冷卻不影響選把」的直接後果，不是實作 bug。
- 現行 scenario「釘住的那把冷卻中 → 409 `cooling`」（`:251-253`）改成 200。
- 對只會看 `env`、不看 `meta` 的舊呼叫方，resolve 現在可能交回一把「已知剛剛 429」的把；它會在第一次呼叫時再吃一次 429。這是提示語意的成本，緩解在呼叫方（看 `meta`／`list`）與側車升級（決定 7）。

## 決定 3 resolve 的回應：`meta` 附 `cooldown` 與 `model_cooldowns`

**決定**

- `meta` 新增兩個欄位，形狀**與 list row 的同名欄位完全相同**：
  - `cooldown`：該把整把的冷卻，`{"until": <RFC 3339 UTC>}` 或 `null`；過了 `until`（含相等）視為 null。
  - `model_cooldowns`：物件，模型 id → `{"until": ...}`；只列還沒過的；沒有時 `{}`（欄位恆在）。
- **只附「這次選中的那一把」**，不附其他把；內容是該把**所有未過期的模型冷卻**（不是只列 `meta.model` 那一個）。
- 呼叫方要問「這次這個呼叫什麼時候再試比較好」時：看 `cooldown.until` 與（`meta.model` 非 null 時）`model_cooldowns[meta.model].until`，兩者取較晚者。這個公式寫進 spec，避免每個消費端自己發明。
- `meta` **仍不含** `available`／`available_again_at`：resolve 的 200 已代表 key 已部署，`available` 恆為 true、沒有資訊；`available_again_at` 是「整把＋用量窗口」的合併時刻，屬 list／`usage-reporting`，不搬進 resolve。
- 改寫主 spec 的「`meta` MUST NOT 含 `cooldown`／`available`／`available_again_at`」：新的句子是「MUST 含 `cooldown`／`model_cooldowns`，MUST NOT 含 `available`／`available_again_at`」，並說明 200 不再代表「沒有冷卻」。
- `schema` 不 bump：只新增 meta 欄位（與 `usage` 同一個規則，`openspec/specs/subscription-registry/spec.md:241`）。
- `get` 不在 `meta` 這條：它回的是 list 同一種 row，`cooldown` 本來就有，`model_cooldowns` 跟著 row 出現（決定 5）。

**理由**

- 放在 `meta` 而不是新的頂層鍵：`meta` 已經是「這次選了什麼、它的政策與用量」的容器；`usage` 就在那裡，冷卻與用量同類（都是「關於這把的狀態」）。放頂層會多一個與 `meta` 平行的容器，`schema` 的語意也更難講。
- 用**與 list 相同的兩個欄位**，不發明第三種形狀：側車與人已經要讀 list 的 `cooldown`／`model_cooldowns`；resolve 用同一組名字與形狀，程式與文件都只有一套。`meta.cooldown` 也不會與 list 的 `cooldown` 語意分叉。
- 附整把的所有模型（而非只附 `meta.model`）：呼叫方最常見的下一步就是「這個模型在冷卻，同一把還有哪個可以」；附整份地圖讓它不必再打一次 list，也讓「換模型」在同一個回應裡就能決定。模型數量有限（一個 provider 十幾二十個），而且只列未過期的。
- 只附選中的那一把：resolve 只承諾這一把；附別把的冷卻會讓回應變大、還得解釋「為什麼有我沒選的資料」。
- `available` 不進來：它在 resolve 恆為 true，附了只是噪音；`available_again_at` 不進來：它需要用量合併，而 resolve 的 `usage` 已經給了原料，消費端要看可以自己算（也避免 resolve 多一層「用量壞了怎麼辦」的規則）。

**被否決的選項**

1. **只附「這次的有效時刻」一個欄位**（例如 `meta.cooldown_until`＝max(整把, 該模型)）：呼叫方拿到一個數字，卻不知道是整把還是模型的冷卻、也不知道同把還有哪些模型可換；要換模型還得再打 list。資訊被壓縮過頭。
2. **`meta.cooldown = {until, models}` 巢狀**：與 list 的 `cooldown` 形狀不同，`available` 的既有公式也要重講；兩個端點同一件事兩種形狀，長期一定漂。
3. **附 `available`／`available_again_at`**：見上，一個恆為 true、一個是別人的合併規則。
4. **附整份 list 的所有把**：resolve 只選一把；其他把的冷卻與這次呼叫無關，還可能讓呼叫方誤以為那些把「被考慮過」。
5. **放在頂層 `cooldown`（與 `meta` 平行）**：`meta` 已經是同類資訊的容器；多一個頂層鍵要改 `{schema, env_schema, meta, env}` 的形狀說明，且未來每個「關於這把的狀態」都會想放頂層。

## 決定 4 409 `cooling` 的處置與相容性

**決定**

- **新 Gateway 不再產生 409 `cooling`**：刪掉 `lgwd/registry.py:148-161` 的排除與 `raise cooling(...)`；`lgwd/errors.py:80-82` 的 `cooling()` 一併刪除（沒有生產者，留著是死碼）。
- `until` 不再出現在任何錯誤回覆；錯誤回覆只剩既有的 `{"error": code, ...}`（`400 bad_request` 可能帶 `missing`）。
- **不保留給任何其他情況**：其他 409 已有 `no_candidate`（`exclude` 造成）與 `not_deployed`（key 沒部署），語意各自清楚；把 `cooling` 留給其中一個會讓同一個碼有兩種意思。
- **但文件與消費端要保留對舊 Gateway 的判讀**：
  - `lgw/client.py`：對非 2xx 的處理是通用的（`error` 放進 `Result.error`、其餘欄位放進 `Result.data`，`lgw/client.py:98-117`），新 Gateway 不再回 `cooling`，client 不需改；`cooldown(id, until)` 的簽名與行為也不變（新增的是選填 `model`）。docstring 的錯誤碼清單把 `cooling` 標成「只有舊 Gateway 會回」。
  - `lgw/signals.py`：`to_cooldown_body()` 仍是 `{"until": ...}`（`lgw/signals.py:48-59`），不變；要不要帶 `model` 是呼叫方（sidecar）的事，signals 不知道模型。
  - **新 sidecar 對舊 Gateway**：升版空窗內仍會收到 409 `cooling`；sidecar 保留現行 `CoolingError` 路徑（`swapping.py:256-262`、`picking.py:476-481`），不要因為新 Gateway 不回就刪掉。`list` 沒有 `model_cooldowns` 時就退回現行（整把 `cooldown`）行為。
  - **舊 sidecar 對新 Gateway**（過渡期的主要代價）：
    - `list` 的形狀沒變（只多一個它會忽略的 `model_cooldowns`）、`available` 的定義沒變（決定 5），所以它的部署判斷（`protocols.py:384-391`）與選把（`selectors.py:23-28`）都不會壞。
    - 它不送 `model` → 登錄仍是整把（記錄層的 bug 還在，但這是舊呼叫方的選擇）。
    - **它依賴的 409 `cooling` 不再出現**：resolve-mode 的 agent 首次開會直接拿到 200、開起來、第一次呼叫吃 429；接著它登錄整把冷卻，再用 `exclude=<剛用完的那把>` 重 resolve（`swapping.py:214-215`）。對只有一把的 provider（zen 只有 `opencode-zen`），`exclude` 後沒有候選 → 409 `no_candidate` → 它會停掉 agent（`swapping.py:265-268`），而不是像今天一樣排隊等。釘住的 agent 則依 `list` 的整把 `until` 等（`swapping.py:691-708`），行為與今天相近；走舊 list→get 路徑的舊型 agent 仍受 `available` 約束，行為也與今天相同。
    - 結論：**舊 sidecar + 新 Gateway 比今天差**（今天至少會排隊），所以升級順序要反過來（見「部署」）：sidecar 先、Gateway 後，或同一個維護窗一起上。
- 版本探測：不新增 capability 欄位。新 sidecar 用「list row 有沒有 `model_cooldowns`」判斷對方是不是新 Gateway（`Subscription.raw` 保留整列、未知鍵照收，`protocols.py:352-381`）；沒有就退回現行。

**理由**

- 本人說「擋不了，也沒必要擋」。留一個「全部冷卻時還是 409」的例外，就還是擋；而且呼叫方得為了一個很少發生的分支繼續寫 409 處理。
- 錯誤碼是契約：留一個永遠不產生的碼，會讓下一個讀 spec 的人以為它還有意義；刪掉生產者、在 client／sidecar 標 legacy，是唯一能讓兩邊文件都誠實的作法。
- 舊 sidecar 的代價必須寫清楚而不是藏起來：它不是「行為不變」，而是「首次開會多吃一次 429、單一把 provider 會停」。這直接決定部署順序，也決定要不要先升 sidecar。

**被否決的選項**

1. **保留 `cooling` 給「釘住的那把」**：本人明確說不擋；釘住與否不改變「冷卻只是提示」。留這個例外會讓側車繼續依賴 409，延後它改用提示的時間。
2. **回 200 但在 body 加 `warning`／`hint` 布林**：多一組沒有既有詞彙的欄位，client 與 sidecar 都要新增判讀；`meta.cooldown` 已經把資訊給足了。
3. **改用 409 `cooling_hint` 或 200 + 429**：前者只是換名字、仍是「錯誤」；後者把 Gateway 變成上游的轉發器，且 Gateway 不知道上游現在到底能不能用（提示可能過期）。
4. **保留 `cooling()` 函式但不呼叫**：死碼；下一個改動的人會以為它還在用。刪掉，讓 import 失敗成為「還有地方在回 cooling」的警報。

## 決定 5 list／get：`model_cooldowns`，`available` 定義不變

**決定**

- 每筆 row 多一個欄位 `model_cooldowns`：物件，鍵是模型 id、值是 `{"until": <RFC 3339 UTC>}`；**只列還沒過的**（`until <= now` 視為不存在）；沒有就是 `{}`（欄位恆在，形狀穩定）。
- `cooldown` 仍然是整把的 `{"until"}` 或 null；整把冷卻中時照實列 `model_cooldowns`（那些模型列在整把解除之後還是會咬人）；**兩個都列**。
- **`available` 的定義完全不變**：`已部署（key 在）而且沒有未來的整把 until`；模型冷卻（`model_cooldowns`）**不影響** `available`。
- `list` 與 `get` 都帶 `model_cooldowns`（共用 `_item`）；`usage`／`available_again_at` 仍只在 `list`。
- `get` 本來就不因冷卻拒絕（`openspec/specs/subscription-registry/spec.md:40-42` 的「冷卻中仍可 get」）；本 change 只讓它也帶 `model_cooldowns`，沒有新的拒絕條件。
- `schema` 不 bump（仍為 1）。

**理由**

- `available` 為什麼不改：它是側車判斷「key 有沒有部署」與「整把能不能無腦開」的粗訊號。側車的 `is_deployed()` 是 `available or cooldown.until`（`protocols.py:384-391`），選把用 `s.available`（`selectors.py:23-28`）。如果改成「只剩 key 已部署」：
  - 整把冷卻中的把在舊 sidecar 眼中會變成可用（`available=true`），它會立刻開、立刻 429——比今天更糟；
  - `is_deployed()` 也失去「不可用但正在冷卻」與「key 沒部署」的區分；
  - 而新 sidecar 要判斷「這把整把在冷卻」就只剩 `cooldown.until`，與 `available` 兩套語意並存，遲早不一致。
  保留現有定義，等於保留一個「粗但穩定」的提示；模型層的細節由 `model_cooldowns` 補上。
- 模型冷卻不進 `available`：一個模型用完不該讓整把看起來沒部署（這正是本次要修的 bug；初版決定 2 的否決選項 3 已論證）。
- 放在**新的頂層欄位**而不是塞進 `cooldown`：`cooldown` 的語意是「整把冷卻」，`available` 的定義直接引用它（`openspec/specs/subscription-registry/spec.md:101-102`）。把模型塞進去，`available` 的公式就要重寫，側車的 `is_deployed()` 也會跟著誤判。
- 物件（模型 id → until）而不是陣列：同一（把、模型）只有一筆，物件不會出現重複鍵；消費端要查「某模型有沒有冷卻」是一次 lookup。模型 id 可含 `/`、`:`（`openspec/specs/subscription-registry/spec.md:46`），當 JSON 鍵沒問題。
- 過期不列（而不是列出來讓消費端自己判斷）：與 `cooldown` 的既有規則一致（「過了 `until` 即視為 null」，`lgwd/registry.py:377-382`），消費端不必再帶時鐘。
- `available_again_at` 不動：它的語意是「這把**整把**什麼時候完全恢復」（用量用完的窗口與整把冷卻取最晚，`lgwd/usage.py:139-155`；規則見 spec `usage-reporting`），模型冷卻不在裡面；要看模型冷卻的人讀 `model_cooldowns`。不改 `usage-reporting` 也是這個 change 刻意不擴大戰線的結果。
- `get` 也帶：`_item` 共用、資料來源是同一次 `cooldowns` 讀取，沒有額外成本；`get` 是過渡期介面（#48 移除），為它多一個分支不值得。`usage`／`available_again_at` 之前被排除是因為它們是「額外取用量」的產物、而且 get 不該觸發取用量；`model_cooldowns` 沒有這個問題。
- 不 bump `schema`：側車看到 `schema > 1` 會整列拒收（`protocols.py:358-362`），新增欄位而 bump 等於逼側車先升級才看得到 list。

**被否決的選項**

1. **`available` 改成只代表「key 已部署」**：見上，會讓舊 sidecar 對整把冷卻的把立刻開、立刻 429，也讓 `is_deployed()` 分不出「冷卻」與「沒部署」。這是**本人要判斷的介面語意**（判斷點 B3），但建議維持。
2. `cooldown: {until, models: {...}}`（巢狀）：語意混在一起、`available` 要重定義、側車相容性差。
3. 陣列 `model_cooldowns: [{model, until}]`：可能重複、要自己排序／去重，消費端多寫程式。
4. 放進 `models`（`models.cooldowns`）：`models` 是政策（`allow`／`default`，來自政策檔），冷卻是狀態（DB）；混在一起會讓人以為政策檔可以寫冷卻。
5. 讓模型冷卻也設 `available=false`：見上，回到這次的 bug。
6. 只在 `list` 帶、`get` 不帶：多一個分支、形狀不一致，而 get 反正要退場。
7. bump `schema` 到 2：側車會拒收整列，等於一次不相容變更；這次不需要。

## 決定 6 儲存：新表 `model_cooldowns`，additive migration

**決定**

```sql
CREATE TABLE IF NOT EXISTS model_cooldowns (
    subscription TEXT NOT NULL,
    model TEXT NOT NULL,
    until TEXT NOT NULL,
    PRIMARY KEY (subscription, model)
);
```

- 加進 `lgwd/store.py` 的 `SCHEMA`（啟動時 `executescript`，`CREATE TABLE IF NOT EXISTS` 天然就是 additive migration，`lgwd/store.py:20-36`、`:47`）；**不需要** `ALTER TABLE`、不需要資料搬移。
- 查詢：`cooldown_get_model(cur, sub, model)`、`cooldown_set_model(cur, sub, model, until)`（UPSERT、只在新值較晚時更新，與整把同一條 SQL 形狀，`lgwd/store.py:88-95`）、`model_cooldowns_of(cur, sub)`（回該把全部模型列，`list`／`get`／resolve 附資訊用）。
- **不需要刪除函式**：本版沒有提前解除（本人裁定）；過期列讀時視為無效。
- 整把那張表、那兩個函式、以及「取較晚」的兩道判斷完全不動。
- `audit` 表的 `model` 欄位已經存在（`lgwd/store.py:25-35`），登錄冷卻用它；**不加欄位**。

**理由**

- 舊程式（退版）讀不到新表也不會壞：它只 `SELECT * FROM cooldowns`（`lgwd/store.py:83-86`），新表對它不存在。這是最重要的一點——這個 repo 的退版劇本是「`llmgwctl rollback` 回前一版」（舊路徑 `install.sh --tag`，`docs/deploy.md:302-305`、`docs/deploy.md:413`），不能讓 DB 變成單向門。
- 若改成在 `cooldowns` 加 `model` 欄、改成 `PRIMARY KEY (subscription, model)`：SQLite 換主鍵要重建表；而且舊程式的 `cooldown_get` 用 `WHERE subscription = ?` + `fetchone()` 會撈到「某一列模型冷卻」當成整把冷卻（保守但錯誤），`cooldown_set` 的 `ON CONFLICT(subscription)` 在新主鍵下也不再成立——舊程式會插入重複的整把列。退版直接壞掉。
- 兩張表的職責剛好對上兩層語意（整把／模型），查詢各自單純，不必在 SQL 裡分 `model IS NULL`。
- 不需要刪除函式：留著只會被誤用（例如未來有人以為可以「解除」）；真的要做解除時再一起設計稽核與權限。

**被否決的選項**

1. 在 `cooldowns` 加 `model` 欄（見上，退版會壞）。
2. 把模型冷卻序列化成 JSON 塞進既有的 `cooldowns.until`：型別混用、無法查、`list` 要整包解析、以後想加欄位更痛。
3. 每次寫入時順手刪掉過期的列：讀路徑不該寫（`list` 只讀，`lgwd/registry.py:106-120`）；過期列本來就被 `_cooldown` 當不存在（`lgwd/registry.py:377-382`），多一個清理行程只多一個故障點。
4. 加一個「冷卻歷史」表：稽核表已經是只追加的歷史，不需要第二份。
5. 先把「解除」的函式一起做起來放著：本版沒有這個介面，先做等於開了沒人管的路；要做得連同權限、稽核、濫用分析一起（初版已經分析過，本人裁定不做）。

## 決定 7 交 MyLinuxPool 的請求（不在這個 repo 改；PM 經 PMO 轉）

這一節是本 change 對 private repo `MyLinuxPool`（`modules/sidecar`）的**請求**，不是本 repo 的實作。每一條都附上 sidecar 現況的 `檔案:行號`，方便對方對照。核心是一句話：**把冷卻當提示，不要當閘門；要不要等、換哪個模型由 sidecar 自己決定。**

1. **開隊員前先看 `list` 的提示**。現在 sidecar 的選把只看 `available`（`selectors.py:23-28`），看不到模型層。請求：選把時，除了 `available`，也讀每一列的 `model_cooldowns`，**避開 `models.allow` 內正在冷卻的模型**；同一把還有 allowed 且沒冷卻的模型時，直接挑那個模型（送 `model` 給 resolve），不要因為預設模型在冷卻就放棄整把。
2. **收到 429／額度事件才登記冷卻，而且要送「該模型」**。`SubscriptionSource.cooldown` 現在是 `(subscription_id, until)`（`protocols.py:1163-1173`、`sources.py:420-440`），HTTP body 是 `{"until": until}`（`sources.py:444-456`）。請求加一個選填參數，body 變成 `{"until": ..., "model": ...}`。值用 **driver 實際啟動的那個模型 id**：opencode 的 `handle.expected_model`（`drivers/opencode.py:501-505`）、Claude 的 `ANTHROPIC_MODEL`。**Claude 訂閱不要送 `model`**：它的額度是帳號級的 `5h`／`1w` 窗口（`DriverState.window`，`protocols.py:465-471`），不是某個模型用完；送模型會讓 Gateway 只冷卻一個模型，實際上整個訂閱都不可用——那是把「太嚴」換成「太鬆」。
3. **`DriverState` 加選填 `model`（建議）或在 core 從 handle 取**。現在 `DriverState` 沒有模型（`protocols.py:465-471`），opencode 的 `_apply_failure` 建 quota 狀態時也沒有模型可帶（`drivers/opencode.py:783-797`），所以 core 的 `_cooldown_until`（`core.py:1789-1808`）就算想送也拿不到。建議加在 `DriverState`：driver 在失敗的當下有 handle、有模型，core 不必回頭猜。
4. **免費模型的 fallback 改短**：`QUOTA_FALLBACK_SECONDS = 3600.0`（`core.py:139`）現在對所有「driver 說不出 `resets_at`」的情況都用 1 小時。請求：
   - 有 `window` 的（Claude `5h`／`1w`、Zen Go）維持 3600 秒；
   - **opencode 免費模型**（`window` 為 None、錯誤是 `FreeUsageLimitError`／`free_tier_limit`）用 **600 秒**，同一把同一模型在短時間內重複 429 就加倍（600 → 1200 → 2400，上限 3600）；
   - 理由：這個 fallback 是猜的（opencode 對免費模型沒有對外承諾的重置時間，`lgw/signals.py:132-137` 只認 `retrying in Xh Ym`，抓不到就是 None）；**猜太長的代價是全隊被擋（今天的成本），猜太短的代價只是多一次 429**（sidecar 會再登錄一次），所以寧可短。
5. **`model` 被拒時不要退回「整把＋1 小時」**。現在 `invalid_until`／`until_out_of_range` 會重送一次 now+1h（`sources.py:431-440`）；請求把 **400 `bad_request`（model 不在 allow／形狀不對）排除在重送之外**：記 log、跳過登錄，換把照舊進行——登錄失敗不該擋換把（`swapping.py:216-220` 已經是這個語意）。若在這裡退回整把，今天這個 bug 會原地復活。
6. **保留對舊 Gateway 409 `cooling` 的判讀**。新 Gateway 不會再回，但升版空窗內會遇到舊 Gateway；現行的 `CoolingError` 路徑（`swapping.py:256-262`、`picking.py:476-481`）先不要刪。同時，新 sidecar 要能讀 `meta.cooldown`／`meta.model_cooldowns`（不在就當沒有提示），不要假設 resolve 會替你避開冷卻。
7. **不要用 `model_cooldowns` 判斷「key 沒部署」**：`Subscription.is_deployed()` 是 `available or bool(cooldown.until)`（`protocols.py:384-391`），本 change 刻意讓 `available` 不因模型冷卻改變，就是為了不踩這個判讀。部署狀態請照舊只看 `available`／`cooldown`。
8. **換把前先試同一把的其他模型**。現在換把是「登錄冷卻 → 用 `exclude=<剛用完的那把>` 重新 resolve」（`swapping.py:214-215`、`swapping.py:255-262`），所以側車自己一定會離開剛用完的那把——即使只有一個模型用完。對**只有一把**的 provider（zen 只有 `opencode-zen`）就會走到 `no_candidate` 而停掉 agent。請求：若 list（或 resolve 的 `meta`）顯示同一把還有 allowed 且沒有模型冷卻的模型，先用 `resolve(model=<該模型>)` 重試同一把，再考慮換把／停手。

## 決定 8 測試計畫

全部在本 repo 的 `tests/`（假時鐘 `lgwd/clock.py` 的 `FakeClock`、臨時 DB、臨時 `keys.env`；不連外、不碰真 key）。命名沿用既有測試的中文方法名風格。**既有 409 測試是這次要改的對象，不是不能動的護欄**；改的時候連同 scenario 名稱一起對齊 spec。

**核心（工單的完成判準，已按本人裁定改寫）**

1. `test_A模型冷卻時指定A仍給key並附冷卻資訊`（`tests/test_registry.py`）：`opencode-zen` 登錄 `model=mimo-v2.6-flash-free` 冷卻 → `resolve(agent=opencode, provider=opencode-zen, model="mimo-v2.6-flash-free")` 回 200，`meta.subscription == "opencode-zen"`、`meta.model == "mimo-v2.6-flash-free"`、`env` 有 `OPENCODE_MODEL`，且 `meta.cooldown` 為 null、`meta.model_cooldowns[mimo…]["until"]` 是登錄值。
2. `test_A模型冷卻時指定B模型仍選得到同一把`：同上但 `model="big-pickle"` → 200、`meta.model == "big-pickle"`；`meta.model_cooldowns` 仍列出 A 的冷卻（資訊不因換模型消失）。
3. `test_整把冷卻時resolve仍給key並附資訊`：登錄整把到 T → `resolve` 任何模型 → 200、`meta.cooldown == {"until": T}`、`meta.model_cooldowns` 照實；`env` 照常。
4. `test_不帶model的登錄仍記整把`：同一把不帶 `model` 登錄 → `list` 的 `cooldown == {"until": …}`、`available` 為 false、`model_cooldowns == {}`；resolve 仍 200 並附 `meta.cooldown`；稽核的 `model` 欄是 null；另加「舊 client 的 `cooldown(id, until)` 簽名仍可用」。
5. `test_model不在allow回400`：`opencode-zen` + `model="muse-spark-1.3"` → 400 `bad_request`，`model_cooldowns` 不變（**不是** 403 `model_not_allowed`）。
6. `test_list列出各模型與整把冷卻`：兩個模型各登錄、其中一個把時鐘推過期 → `model_cooldowns` 只剩未過的那個；再登錄整把 → `cooldown` 與 `model_cooldowns` 同時出現；`available` 只受整把影響。
7. `test_舊409路徑已不存在`：把「唯一一把冷卻」「兩把都冷卻」「釘住那把冷卻」「default 模型冷卻」四種形狀逐一 resolve，全部 200；HTTP 層的錯誤對照表不再有 `cooling`；`lgwd/errors.py` 沒有 `cooling` 這個名字（實作刪除後自然成立）。

**resolve 的選把（冷卻不參與）**

8. `test_冷卻不影響選把的priority`：同 provider 兩把，priority 較小者冷卻 → 仍選它（200），不是選沒冷卻的較低 priority 那把。
9. `test_同priority時冷卻不是排序鍵`：兩把同 priority、id 較小者冷卻 → 仍選 id 較小者（排序只看 priority／id）。
10. `test_不指定model時default冷卻仍200`：default 冷卻、別模型沒冷卻 → 200、`meta.model` 是 default、附資訊（Gateway 不代選）。
11. `test_沒有模型可對時仍200`：`claude-subscription`＋default null、只有某個模型冷卻、整把沒冷卻 → 200、`meta.model` 為 null、附 `model_cooldowns`（模型冷卻只是資訊）。
12. 迴歸：既有的選把測試（釘住、`exclude`、`no_candidate`、`not_deployed`、priority、`no_model`、`model_not_allowed`）一行都不改、全綠——除了第 7 條列出的冷卻案例要改寫。

**登錄與儲存**

13. `test_同一模型重複登錄取較晚的`：較早的忽略、較晚的覆蓋；兩個模型互不影響；整把與模型並存；稽核的 `until` 記有效值、`model` 有值。
14. `test_model形狀不對回400`：`""`、`123`、`null`、`["x"]`、`opencode/big-pickle`（前綴）、`*-free`／`big?pickle`（萬用字元）→ 400 `bad_request`，冷卻不變；`until` 的既有驗證（`invalid_until`、`until_out_of_range`、8 天邊界、時差正規化）測試不改、照樣綠。
15. `test_未知欄位仍忽略`：body 多帶 `window`／`resets_at`／`extra` 不影響登錄（既有的 `test_未知欄位忽略` 擴充一個帶 `model` 的案例）。
16. `test_稽核記模型冷卻`：`action=cooldown`、`model` 是該模型、`until` 是有效值、`agent` 為 null；沒有 `cooldown_release` 這種 action。
17. `test_舊DB沒有新表也能啟動`：用 v5 的 schema（只有 `cooldowns`＋`audit`）建 DB、塞一筆整把冷卻與一筆舊稽核 → 新 lgwd 起得來、`model_cooldowns` 表被建立、舊冷卻照舊生效、舊稽核的 `model` 為 null。
18. `test_退版讀新DB不受影響`：新 DB 有模型列 → 用舊程式的 SQL 讀 `SELECT subscription, until FROM cooldowns` 仍是一把一列（模擬退版）；`audit` 的舊欄位 SELECT 照舊成功（沿用既有的 `AuditMigrationTest` 手法）。
19. `test_模型冷卻跨重啟`：關掉 service 再開，`model_cooldowns` 還在。
20. 值紀律：模型冷卻不讓 key 出現在 `list`、`get`、稽核、SQLite 檔（擴充既有的 canary 掃描）。

**回應形狀與端點**

21. `test_meta含冷卻且不含available`：resolve 200 的 `meta` 有 `cooldown`、`model_cooldowns`，沒有 `available`、沒有 `available_again_at`；`schema` 仍為 1。
22. `test_list與get的模型冷卻形狀`：`cooldown` 仍是 `{"until"}` 或 null（沒有被塞進模型）；`model_cooldowns` 只列未過期、沒有時 `{}`、`/` 與 `:` 當鍵；`get` 也帶但沒有 `usage`。
23. `test_http_cooldown帶model`：POST body 的 `model` 原樣傳到 service；未知欄位忽略；body 不是物件 400；1 MB 上限不變；路由仍是四個（`DELETE /v1/subscriptions/{id}/cooldown` 仍 404）。
24. `test_client_cooldown帶model`：`cooldown(id, until, model=None)` 沒給時 body 只有 `until`、有給時多 `model`；`Result` 的 repr 仍不印 data；client 對 409 的通用判讀仍可用（用假服務模擬舊 Gateway）。

**改壞驗證（在副本做；沒轉紅的列進 PR 報告）**

- 把第 7 步「排除冷卻中的候選」加回去 → 第 1、3、8 條必須轉紅。
- 把 `meta.cooldown`／`meta.model_cooldowns` 拿掉 → 第 1、2、3、21 條必須轉紅。
- 把 `available` 改成只看 key 部署 → 第 6、22 條必須轉紅。
- 把登錄的不在 allow 改回 403 → 第 5 條必須轉紅。
- 把冷卻加進排序（同 priority 時沒冷卻優先） → 第 9 條必須轉紅。

## 風險與取捨

| 風險 | 說明 | 緩解 |
|---|---|---|
| **呼叫方不看提示** | resolve 會照樣交回一把正在冷卻的把；不看 `meta` 的人會再吃一次 429 | 這是本人裁定的直接後果；提示在 `meta` 與 `list` 都給了；側車升級（決定 7）與人用 `curl` 時自己看 |
| **舊 sidecar + 新 Gateway** | resolve-mode 的 agent 首次開不再排隊，會先開再 429；單一把 provider 之後可能 `no_candidate` 停掉 | 升級順序改成 **sidecar 先、Gateway 後**（見「部署」）；文件與 release note 寫明 |
| **新 sidecar + 舊 Gateway** | 仍會收到 409 `cooling`；`list` 沒有 `model_cooldowns` | sidecar 保留 `CoolingError` 路徑與「沒有提示就退回現行」；不新增版本探測 |
| **`resolve` 選到冷卻中的高 priority 把** | 選把與冷卻無關的直接後果 | 呼叫方用 `subscription`／`exclude`／`model` 指定；或先讀 `list`／`meta` |
| **`available` 仍是「部署＋整把冷卻」** | 語意不是「保證可用」；只看 `available` 的消費端看不到模型冷卻 | 文件明寫；要看模型冷卻的人讀 `model_cooldowns`；這是判斷點 B3 |
| **提示可能過期或不準** | 冷卻是呼叫方自報的，沒有認證、沒有驗證上游 | 它只是提示，Gateway 不用它擋人；錯了最壞就是多一次 429，不會鎖死 |
| **`until` 上限 8 天** | 模型冷卻也只能 8 天 | 沿用；真的超過 8 天就是「不要用那個模型」，由政策處理 |
| **`model_cooldowns` 讓 list 變大** | 每把多一個物件 | 模型數有限（一個 provider 十幾二十個），且只列未過期的 |
| **字串比對的脆弱** | 模型 id 大小寫、前綴、`*`／`?` 的樣式都靠字串 | 沿用 resolve 的 `_covered`（同一個 fnmatch，`lgwd/registry.py:63-65`）；登錄時額外拒絕萬用字元；不正規化、不猜 |

## 部署、遷移與回滾

- **升級順序（本版改了）**：**MyLinuxPool 的 sidecar 先上，Gateway 後上**（或同一個維護窗一起）。
  - sidecar 先上、Gateway 還是舊的：sidecar 送的 `model` 被當未知欄位忽略 → 登錄退回整把（不比今天差）；`list` 沒有 `model_cooldowns`／`meta` 沒有 `cooldown` → sidecar 退回現行行為。安全。
  - Gateway 先上、sidecar 還是舊的：resolve 不再 409，舊 sidecar 會「先開再 429」，單一把 provider 可能停掉 agent（決定 4）。比今天差，所以不建議這個順序。
- **遷移**：啟動時 `CREATE TABLE IF NOT EXISTS`（`lgwd/store.py:20-36`、`:47`）自動建立，無資料搬移、無停機步驟、無設定變更。備份照既有的升級／回滾流程（回滾時舊 DB 會先備份成 `.bak-*`，`docs/deploy.md:194`）。
- **回滾**：`llmgwctl rollback`（舊路徑 `install.sh --tag <前一版>`；`docs/deploy.md:302-305`）。舊程式不讀 `model_cooldowns`，`cooldowns` 表與 `audit` 表都沒改 → 直接可用。回滾後的語意變化：舊程式看不到模型冷卻，於是「某模型冷卻中」在那把上等於沒冷卻——回到本次之前的行為（不會壞，只是今天的 bug 回來）。
- **`openspec` 歸檔**：本 change 的 delta 是完整重貼，`openspec archive model-cooldown` 直接套進 `openspec/specs/subscription-registry/spec.md`。
- **文件**：`docs/deploy.md` 第 0 節的服務描述可補一句「冷卻只是提示，resolve 不因冷卻拒絕」（`docs/deploy.md:17`）、第 4 節的 client 範例加一行 `client.cooldown(..., model=...)`（`docs/deploy.md:256`）。`docs/deploy.md` 屬實作單，不在本設計 PR。`lgwd/http.py` 的「只有四個路由」不變（沒有新增路由）。

## 本人的判斷點

以下四點是介面語意或跨 repo 的取捨，建議照「建議」走；PMO 會拿去向本人說明，特別是 B1 與 B2 會改變呼叫方看到的行為。

**B1. resolve 的選把要不要完全不看冷卻（連排序都不看）？**（決定 2）

- **建議：完全不看。** 候選不因冷卻被排除、排序也不因冷卻改變；冷卻只在 `meta` 裡。
- 代價：同 provider 有兩把、priority 較高的那把正在冷卻時，resolve 會回那一把（附提示），而不是自動給沒冷卻的另一把；舊 scenario「一把冷卻中 → 選另一把」因此反過來。呼叫方要別把就自己指定 `subscription`／`exclude`。
- 替代方案（不建議）：同 priority 時沒冷卻的優先。它會讓 `meta.subscription` 隨時間漂、稽核對不上，而且只解決「同分」這一小塊。

**B2. resolve 的冷卻資訊放在 `meta.cooldown` ＋ `meta.model_cooldowns`（與 list 同形狀），可以嗎？**（決定 3）

- **建議：可以。** 兩個欄位恆在（沒有時 null／`{}`），只附這次選中的那一把；整把與各模型都給。主 spec 那句「`meta` MUST NOT 含 `cooldown`」會改寫成「MUST 含這兩個、MUST NOT 含 `available`／`available_again_at`」。
- 這是**介面語意的變動**：resolve 的 200 不再代表「沒有冷卻」；舊消費端如果只讀 `env`，行為不變（照樣拿到 key），但它不會知道冷卻。
- 替代方案（不建議）：只給一個「這次的有效時刻」欄位。資訊太少，換模型還得再打 list。

**B3. `list` 的 `available` 要不要改成只代表「key 已部署」？**（決定 5）

- **建議：不要改。** 維持「key 已部署而且整把沒有未來的 `until`」；模型冷卻只放 `model_cooldowns`，不影響 `available`。
- 理由：側車用 `available` 判斷部署與選把（`selectors.py:23-28`、`protocols.py:384-391`）；改成只看部署，會讓整把冷卻中的把在舊 sidecar 眼中變成可用，立刻開、立刻 429，比今天更糟。
- 若本人要「`available` 完全等於可用」，那要連 sidecar 的 `is_deployed()` 與選把一起改，屬下一個 change，不建議夾在這裡。

**B4. 交 MyLinuxPool 的免費模型 fallback 600 秒起、重複加倍，照這個數字去談嗎？**（決定 7 第 4 條；跨 repo，PM 經 PMO 轉）

- **建議：照提。**（600 → 1200 → 2400，上限 3600）。這是猜的（沒有公開的重置時間），寧可短：猜短的代價是多一次 429，猜長的代價是全隊被擋。
- 若 MyLinuxPool 認為免費模型的重置其實是 5 小時窗口，那就該用 driver 的 `resets_at`／`window`，而不是 fallback 數字——這條要在那邊的設計裡定。

## 作者提議、本人未指示

1. 登錄的 `model` 不在 allow／含萬用字元／形狀不對，一律用既有的 400 `bad_request`，**不新增錯誤碼**（「model 不在 allow」不回 403）。
2. `meta` 的冷卻資訊沿用 list 的兩個欄位名與形狀（`cooldown`、`model_cooldowns`），不新增巢狀或第三種形狀。
3. resolve 的排序完全不看冷卻（B1 的建議）；刪掉第 7 步，原第 8 步變成第 7 步。
4. 刪掉 `lgwd/errors.py` 的 `cooling()`（死碼），`lgw/client.py` 的文件把它標成 legacy；側車的 `CoolingError` 路徑保留給舊 Gateway。
5. 模型冷卻沿用 `until_max_ahead_seconds`（8 天），不另開上限。
6. `model_cooldowns` 用物件（模型 id → `{until}`）而不是陣列；沒有時 `{}`（欄位恆在）。
7. `get` 也帶 `model_cooldowns`；`usage`／`available_again_at` 仍只在 list。
8. 不清過期的模型冷卻列（與整把一致，讀時視為無效）；不為解除預留刪除函式。
9. 升級順序改成 sidecar 先、Gateway 後（或同一窗），並寫進 release note。
10. 測試方法名沿用既有的中文命名；既有的 409 冷卻測試改寫成「仍 200＋附資訊」，不是刪掉了事。

## 沒查到／無法查證

1. **MyPMO#163、工單規則檔、`MyLinuxPool` 的 issue 與 PR** 是 private，依規矩沒讀；只依 issue #76 的內文、PM 在 Angareion 的轉述，以及本機唯讀的 sidecar 程式。
2. **opencode zen 免費模型的真實重置時間**：沒有公開文件可查。`lgw/signals.py:132-137` 只認 `FreeUsageLimitError`／`Free usage exceeded` 後面的 `retrying in Xh Ym`；抓不到就沒有 `resets_at`，側車就落到 1 小時的 fallback（`core.py:1789-1808`）。決定 7 的 600 秒是**推論值**，不是量測值。
3. **哪台 worker 跑哪一版 sidecar、哪一版 Gateway**：不連真機、不 ssh，未查。所以「舊 sidecar 對新 Gateway」的實際時序（誰先升級、空窗多久）只是設計上的相容性論證，不是部署事實。
4. **上游 429 之後的真實行為**：一直重叫會不會換來更長的封鎖，沒有實測（也不能為了設計去實測——會燒本人的額度、也可能違反上游條款）。這是「提示可能不準」風險的來源。
5. **側車 `wait_quota`／`earliest_lift` 對「模型層冷卻」的完整語意**：只讀到 `swapping.py:691-708` 與 `swapping.py:578-616` 的候選過濾，沒有逐行追完所有重開路徑；決定 7 是請求，不是對側車現況的保證。
6. **`meta` 新增欄位對既有第三方消費端的實際影響**：本 repo 只看得到 `lgw/client.py` 與側車；其他手寫 `curl`／腳本沒有清冊，未查。
