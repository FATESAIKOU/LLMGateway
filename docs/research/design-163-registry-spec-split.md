# #163 設計：subscription-registry 主 spec 要不要拆

架構師：llmgateway-arch-m1-1　日期：2026-11-03　狀態：已核准（PMO 轉本人 J-1／J-2／J-3 無異議，2026-11-03）
基準：main c556be9（含 #164）。所有行號指 `openspec/specs/subscription-registry/spec.md`（下稱「本 spec」），除非另註。

## 結論

**拆，拆成 5 個 capability；純搬移、不改規則；不是大型設計，不需 Opus。** 另開實作單（一個 chore PR，加一個獨立的 key-vault 小 PR）。唯一前置：先處理未歸檔的 `openspec/changes/opencode-go-subscription`（見風險 R-1）。

## 1. 現況數字

- 行數 1084；requirement 124；scenario 174（`wc -l`；`### Requirement` 124 個；`#### Scenario` 174 個）。其他主 spec：connectivity 47、key-vault 101、role-allocation 190、usage-reporting 564；本 spec 佔五份總和（1986）的 55%。
- 工單 #163 內文寫「main 為 652 行、13 條」是 #137 前的舊數字；現在 main 就是 1084／124（已核對）。
- 分群（以行號區間切；requirement 數／scenario 數）：

| 群 | 內容 | 行號 | req | scn |
|---|---|---|---|---|
| A | list、models.default、endpoint、共通 JSON、訂閱 id 集合、schema 不 bump | 9–215 | 23 | 34 |
| B | 登錄冷卻（until／model 驗證、順序、兩層獨立、自動解除、冷卻只是提示） | 216–363 | 16 | 25 |
| 稽核 | 稽核紀錄 | 364–383 | 1 | 4 |
| D | 錯誤回覆（碼表、優先序、cooling 退場） | 384–509 | 15 | 20 |
| E | resolve 介面（query 規則、meta、usage、快取、版本號） | 510–737 | 29 | 35 |
| F | 選把規則（步驟 1～7、冷卻不參與、錯誤優先序） | 738–895 | 15 | 28 |
| G | env 組成（各變數、空物件、未知鍵、model 省略） | 896–1028 | 17 | 20 |
| H | local-login（沒有憑證的訂閱） | 1029–1084 | 8 | 8 |
| 合計 | | | 124 | 174 |

（統計以腳本跑出：逐行掃 `### Requirement`／`#### Scenario`，以標題行號落在區間分群；B 的 scenario 數含「登錄冷卻的 model 被擋時回 400」的 6 個。）最大單條：「冷卻不參與選把」8 scenario／約 1780 字元（`:829`）。

## 2. 拆不拆

**建議拆。** 理由與反面都列出：

為什麼拆
1. 一個 capability 一個 delta 檔，現在任何碰冷卻、錯誤碼、env 的 change 都要對 1084 行的檔寫 MODIFIED／ADDED，審查者要在同一檔裡找六個主題的交界；`:829`（8 scenario）與 `:259`（6 scenario）這類長條已證明主題之間是各自生長。
2. 群之間的耦合其實低：用腳本掃「引用另一條 requirement 標題」的句子（「…」形式），跨群引用只有 16 處（列於 §3.4），其餘都在群內。耦合低＝切口乾淨。
3. 邊界與程式模組對得上（B↔`register_cooldown`；E/F↔`resolve`／`_pick`；G↔`providers.build_env`），日後 change 的 delta 落點是可預期的。

為什麼不是更多
- 超過 5 個會讓 delta 要寫多檔的 change 變常態（現在典型 change 碰 2～3 群）。

反面（不拆的理由，我認為不足以勝出）
- openspec 工具沒有大小限制（本檔 strict 仍過，見 #137 提交說明）；拆分的收益是人用的，不是工具要求。
- 交叉引用成本：16 處跨群，加上 role-allocation 6 處、key-vault 1 處指到本 spec 的要改（見 §4）。成本小且一次性。
- 風險是 MODIFIED 的 header 必須在目標 capability 內精確比對：誤放目錄時 openspec 會**明確報錯**（找不到 header），不是靜默漏改。

## 3. 拆法

### 3.1 新 capability 名稱與邊界

| 名稱 | 範圍（舊群） | req | 約行 |
|---|---|---|---|
| `subscription-registry`（保留原名） | A＋稽核＋H（list、共通 JSON、訂閱集合、local-login） | 32 | 280 |
| `subscription-cooldown` | B（登錄冷卻 POST） | 16 | 150 |
| `registry-errors` | D（錯誤碼表與優先序） | 15 | 130 |
| `resolve` | E＋F（resolve 介面、meta、選把規則） | 44 | 390 |
| `resolve-env` | G（env 組成、各變數、空物件） | 17 | 135 |

保留 `subscription-registry` 原名的理由：role-allocation、key-vault 與 14 個已歸檔 change 都用這個名字；它仍是「菜單與共通 JSON」的家。
邊界判準：**誰擁有這條的規則**。「meta 的 cooldown 與 model_cooldowns」（`:617`）、「冷卻提示的判讀」（`:628`）是 resolve 回覆的欄位 → `resolve`；「冷卻只是提示」（`:350`）是登錄的語意 → `subscription-cooldown`。「稽核紀錄」（`:364`）涵蓋 resolve 與冷卻兩邊，留在 `subscription-registry`（它是資料庫層的共通規則）。local-login 八條（`:1029-1084`）橫跨 endpoint／env／usage／稽核，拆開會零碎，整組留在 `subscription-registry`，引用其他 capability 用名稱。

### 3.2 對照表

完整 124 條表由腳本產生（實作單的產出物，隨 PR 附），規則：依 §1 行號區間分配。要點與例外：
- 區間分配：9–215 與 364–383 與 1029–1084 → `subscription-registry`；216–363 → `subscription-cooldown`；384–509 → `registry-errors`；510–895 → `resolve`；896–1028 → `resolve-env`。
- 例外：`:1006`「未知鍵與消費端規則」與 `:1013`「model 省略時的語意」落在 G 區間，隨 G → `resolve-env`；`:731`「roles 候選用回覆的 data resolve 不被隱私擋」在 E 區間 → `resolve`；`:403`「400 bad_request 的登錄冷卻 model 形狀」與 `:503`「登錄冷卻不在通則內」留 `registry-errors`（它們是碼表的一部分），內文以名稱指向 `subscription-cooldown`。

### 3.3 不改語意的驗證法（規則集合前後比對）

實作單 MUST 附並跑一支腳本（不進 repo 或放 `scripts/`，由 PM 定）：
1. 切出 main 的每條 requirement：`(header, 全文含所有 scenario)`，正規化空白後取 SHA-256。
2. 對新的五份 spec 做同樣的切法，得到集合。
3. 斷言：(a) 標題集合相等（124 條，無遺漏無重複、跨檔標題不重名）；(b) 每條的 hash 相等，**除了**明列的白名單：被改寫跨 capability 引用的那些條（§3.4／§4，清單由 diff 腳本列出，PR 描述逐條說明只改了引用字樣）；(c) scenario 總數 174。
4. `openspec validate --specs --strict` 五份＋ role-allocation／key-vault 皆綠；`openspec list --specs` 看得到五個。
5. 靜態：`grep -rn "subscription-registry" openspec/specs` 的每一處引用指到的 requirement 都存在於它所指的 capability。

### 3.4 要改的跨 capability 引用

本 spec 內 16 處（行號為現行檔；格式「來源群→目標群 標題」）：`:38,:54`（A→env「model 省略時的語意」）、`:146,:199`（A→resolve「resolve」）、`:153,:1051`（→env「env 的組成」）、`:305`（B→errors「錯誤回覆」）、`:351`（B→resolve「resolve 的選把規則」）、`:469,:490`（errors→resolve「resolve 的選把規則」）、`:504`（errors→cooldown「登錄冷卻」）、`:669,:673`（resolve→env「env 的組成」）、`:676`（resolve→registry「endpoint 不內嵌憑證」）、`:986`、`:1015`（env→resolve）。改法：把「見「X」」寫成「見 `<capability>`「X」」，不動規則文字。

### 3.5 PR 切分

- PR-1（chore，純搬移）：建立四個新 `openspec/specs/<name>/spec.md`（各帶 `Purpose`）、裁掉原檔、改 §3.4／§4 引用、附驗證腳本輸出。單一 PR（拆成多 PR 會留下中間狀態：某些 requirement 同時存在兩處）。
- PR-2（獨立小 PR，可先可後）：§5 的 key-vault 三點。
- 不需 openspec change（#137 同為 chore 直接改主 spec）；不需 delta 歸檔。

## 4. 其他 spec 與歸檔 change 的引用

- `role-allocation/spec.md`：`:61,:68,:72,:79,:86,:89,:146` 共 7 處「subscription-registry」；其中 `:79` 指「roles 回覆的候選用回覆的 data resolve 不被隱私擋」→ 改 `resolve`；`:72`、`:86` 的「`get` 是另一回事／移除後單筆路徑回 404」→ 指 `subscription-registry`（get 退場在 A 群／`registry-errors` 的 404，實作時以腳本核對實際落點）；`:146` 的 `usage` 只在 list 與 resolve meta 兩處 → 各指 `subscription-registry`／`resolve`；`:61,:68,:89` 泛指整體，維持不動。
- `key-vault/spec.md:90`：指「沒有憑證的訂閱」→ 留在 `subscription-registry`，不動。
- 14 個已歸檔 change 與 `docs/research/*`（review-33、v5-review-12 等 ≥20 處）：**不改**。它們是歷史紀錄，指的是當時的檔；改了反而失真。
- 程式與測試：lgwd／lgw／lgwctl／tests 沒有引用 requirement 名稱（grep 無結果）。

## 5. #137 延後的三點

| 點 | 決定 | 理由 |
|---|---|---|
| (c) key-vault 純指標 requirement（`key-vault/spec.md:82-88`「key 正本的 gh 登入前提」，本條不另立規則） | **刪**：把它唯一的內容——Scenario「未登入時拉取失敗」——併入 `:14`「gh 登入前提」，刪掉指標條 | 標題「…前提」與內文「本條不另立規則」互相矛盾（#137 複審的保留意見）；刪後規則集合只少一條無規則的殼，scenario 不丟。這是**規則集合有變動**的唯一一項（requirement 數 12→11），所以獨立成 PR-2，不與搬移混在一起 |
| (d) PolicyError 字串（`:164`） | **字串放 scenario，正文只寫行為**（「政策載入失敗、lgwd 不啟動」）。`:166`「新增訂閱需兩處一致」已經是正文，不再重複字串 | 例外字串是可觀察的行為（測試會斷言），scenario 是它正確的位置；正文放實作內部字串會讓 spec 隨重命名漂移。現況 `:164` 已是這個形狀，不用改 |
| (e) env 說明段（少了哪條判斷會漏出什麼） | **放 `resolve-env` 的 Purpose**（非規範性說明），規範「MUST 同時寫兩條判斷」留在 requirement（現 `:975` 附近的「agent 或 kind 任一成立即回空物件」） | Purpose 本來就是放動機與後果的地方，不影響 requirement 字數限制；拆檔時順手補上最自然 |

## 6. 風險

- **R-1（需先處理）**：`openspec/changes/opencode-go-subscription/specs/subscription-registry/spec.md` 還在 `openspec/changes/`（未歸檔），內有 MODIFIED「endpoint 的語意」與「env 的組成」。拆後「env 的組成」住在 `resolve-env`，這個 delta 歸檔時會找不到 header 而失敗。對應 #77 已 CLOSED；是否已併進主 spec、該歸檔還是刪除，我沒查（未驗）。請 PM 在 PR-1 之前處理，否則要把這個 delta 的檔案搬到 `specs/resolve-env/`。
- R-2：搬移途中若有別的 change 同時改本 spec，會衝突；PR-1 應在沒有進行中的 spec change 時做（目前 `openspec/changes/` 只有上述兩個）。
- R-3：16＋7 處引用改寫是人工判斷；§3.3 第 5 點的靜態檢查是防線。
- R-4：驗證腳本的「正規化」若過寬會放過語意改動；hash 以逐字（僅正規化行尾與連續空白）為準，白名單必須逐條人工看。

## 7. 對外介面

不碰。只動文件（openspec 主 spec 的檔案配置）；HTTP、list／resolve／冷卻登錄的回應、程式碼與測試都不變。MyLinuxPool sidecar 無影響（它引用的是 archive 的 design 文件，且為歷史路徑）。

## 8. 判斷點（給 PM／本人）

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| J-1 | 拆不拆 | 拆（5 份）／不拆／只拆最肥的冷卻與 env 兩塊（3 份） | 拆 5 份；若想最保守，次選 3 份（`subscription-registry`＋`subscription-cooldown`＋`resolve-env`，其餘留原檔），收益約一半 |
| J-2 | key-vault 純指標條是否刪 | 刪／留 | 刪（PR-2） |
| J-3 | opencode-go-subscription 的舊 change 怎麼處理 | 先歸檔或刪／搬 delta | 先查是否已在主 spec，已在則刪；PM 決定 |

## 未驗

- 未執行 `openspec validate`（本輪只寫設計，沒動主 spec）；「strict 仍過」是 #137 提交說明所稱。
- `opencode-go-subscription` 的 delta 是否已被主 spec 吸收：未查。
- role-allocation `:72,:86,:146` 的新落點，需實作時以腳本確認。
- 行數／群數為腳本與 `wc` 輸出；區間邊界是我依標題行號畫的，未逐條人工複核 124 條的歸屬。
