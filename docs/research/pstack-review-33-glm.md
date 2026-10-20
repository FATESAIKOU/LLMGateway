GLM-5.3-Flash（opencode）

# #33 設計審查（pstack 獨立審查，glm）

審查對象：worktree `review-33p` 的 `openspec/changes/agent-provider-model-interface/`
（proposal／design／tasks／specs/subscription-registry/spec.md）與
`docs/research/design-33-overview.md`；現況對照：`lgwd/`（http.py、registry.py、policy.py、
providers.py、store.py、errors.py，另參 keys.py、main.py）、`lgw/client.py`、
`policy/default.json`、`openspec/changes/gateway-v0/specs/subscription-registry/spec.md`、
`openspec/specs/`（僅 agent-lease／usage-record 已歸檔）、`docs/research/agent-endpoints.md`。
只讀，未改任何被審檔案、未 commit、未讀憑證檔、未連遠端。

輸入脈絡：前三輪審查（review-33／review-33-r2／review-33-r3）與 fix1／fix2 的已修項目不重提；
「本人已裁定」七題（OQ 1–7 與 prompt 開頭的 1–7 裁定）視為已定，不挑戰。本輪**沒有**與裁定衝突的想法。

驗證方式：
- `openspec validate --strict agent-provider-model-interface` → `Change 'agent-provider-model-interface' is valid`（exit 0）。
- 設計的「現況」宣稱逐條對程式核實（見下方驗證紀錄的檔案行號）。
- **Code Quality Lens 行數檢查（`wc -l`）**：lgwd 目前 12 檔共 1,500 行
  （最大 keysync.py 351、main.py 310；被提到的 http.py 150、registry.py 151、providers.py 61、
  policy.py 141、store.py 99、errors.py 67）。按 tasks 1.0–1.4 的落點估算：registry＋resolve
  篩選／錯誤 ≈250–300 行、http＋路由與標頭 ≈180、providers（或新 envs.py）＋env 組成與
  validate_endpoints ≈160、policy＋openrouter.agents 檢查 ≈150。**沒有任何檔案會逼近 1,000 行紅線**；
  helper 落點與既有分層一致，未見「會長出壞味道」的同名邏輯散置或薄包裝。get 的並存是裁定 4 的過渡安排，
  依 prompt 指示不挑戰。

結論先講：**需小修**；critical 0、warning 1、nit 3。

## 指定情境的回碼核對

| 情境 | 回碼 | 文件一致性 |
|---|---|---|
| 釘住＋provider 不一致 | 400 `bad_request` | 一致（spec L11＋Scenario L31–33；design 決定 1 L50、錯誤表 L126；overview L105） |
| 釘住＋exclude 同時給 | 400 `bad_request` | 一致（spec L11、L35–37、L71；design L51、L107；tasks L10；proposal L21）——三輪已修，無殘留 |
| 釘住那把冷卻中 | 409 `cooling`＋until、不代換 | 一致（spec L27–29、L80；design L116；sidecar 端＝waiting-quota，L175／tasks L24） |
| 釘住那把 key 未部署 | 409 `not_deployed` | 一致（design 錯誤表 L130「釘住時只看該把」；spec 選把步驟 6 L76；not_deployed 在 v5 表，r2 已接受） |
| 帶前綴 `model=opencode/big-pickle` | 403 `model_not_allowed` | 一致且經 fnmatch 推演屬實（`big-pickle` 樣式不含 `*`，對 `opencode/big-pickle` 不匹配） |
| 釘住＋推不出模型（假政策 api-key 形狀） | 400 `no_model` | 一致——r3 新-中-1 的 kind 例錯已於第 4 輪修為假政策（spec L106–108、L161–163；design L110、L142；overview L57–59） |

## Findings

### 1. [warning] 「resolve 404 → 回 get」把三種語境塞進同一個信號；退版模式下的換把／釘住語意沒有定義

**Location**: design.md 決定 6.2（L172「resolve 404 時回 get（支援舊 Gateway／退版）」）、決定 5（L157）、
決定 6.6（L176）、Migration 3.0（L208）；tasks.md 2.1（L21）、2.4（L24）；proposal.md Impact（L48）

**Finding**: sidecar 把「resolve 回 404」當作「舊 Gateway／退版」的判別，回退 get。但 404
`not_found` 是**三個語境共用的信號**：

1. 舊 Gateway 對未知路由的回覆（`lgwd/http.py:114-117` 對 route miss 回 `{"error":"not_found"}`，
   新版沿用同 body）；
2. 新 Gateway 上「pinned 的 subscription id 不存在」（spec L11、錯誤表 L127）；
3. （理論上）未知 provider。

sidecar 無法從 body 區分。更實際的缺口在**信號本身之外**：退版模式（新 sidecar＋舊 Gateway）是
Migration 與 tasks 明確設計要撐住的組合（「不致停擺」），但：

- **換把（6.6 的未釘住路徑）退版時無定義**：resolve 404 後 6.6 的「resolve 帶 `exclude`→下一個候選」
  整段失效；退回 get 的話要用什麼 id、由誰挑（sidecar 的 list 挑選機械只寫在 6.1 的「三者都省略」路徑），
  以及 sidecar 要不要保留現行的「跳過剛用完那把（即使 cooldown 登錄失敗）」邏輯——設計只寫到「回 get」為止；
- **釘住的 waiting-quota（OQ 1）退版時退化**：重試 job 走 resolve→404→get；舊 get 語意是「冷卻中照回 key、
  `available:false`」——現行程式確實如引述（`lgwd/registry.py:63-79` 的 get 不檢查冷卻），agent 會在冷卻中被
  重複以同一把拉起、上游同一錯誤循環，直到 until 或人工介入。這個退化沒有被明示；
- 真實 404（政策退版拿掉某把後，AgentRecord 裡的舊 pinned id）也會進同一條 fallback：get 同 id → 404 →
  錯誤照回控制者，結果碰巧收斂，但過程會先打一次無意義的 get。

**Evidence**: 上面三條的行為差都直接從設計文本推得出：6.2 只定義了「404→get」，6.6 的 exclude／
`no_candidate`／`cooling`＋until 都以新 Gateway 的語意為前提；Migration 3.0 與 tasks 1.7 只保證「get 仍在、
可回退 tag」。而 get 的舊語意（冷卻照回）正是本 change 決定 3 明文否定的新行為（「冷卻＝額度已死；
釘住不是繞過冷卻的入口」L116）——同一份設計裡，退版模式就退回這個被否定的行為，卻沒有寫「這是退版模式的
已知退化、驗收不在此模式下進行」。

**Suggestion**: 不動機制本身（resolve 404 → get 已與 MyLinuxPool PM 對齊，且 fallback 觸發條件在
協定上本來就無法用 body 區分）。補文字即可，成本約三句：

- 決定 6.2 加：「新 Gateway 的真 404（未知 pinned id／未知 provider）在過渡期同樣觸發 get 回退；get 對未知
  id 也 404，錯誤照回控制者，sidecar 不得在 fallback 內吞錯。」
- 決定 6.6（或 5）加：「退版模式下，換把退回現行『list 挑下一把＋get』的機械；`exclude`／
  `no_candidate`／`cooling`＋until 的保證在此模式無效。sidecar 須保留現行『不重選剛耗盡那把（cooldown 登錄
  失敗也跳過）』的邏輯；釘住的 waiting-quota 在此模式退化為 get 的『冷卻照回』，屬已知退化，驗收不在此模式下做。」
- tasks 2.6 加一條退版模式測試注記（假來源覆蓋「resolve 404→get」與其後的換把行為）。

### 2. [nit] 「每次 resolve SHALL 記一筆稽核」在失敗回覆上沒有可記物件；稽核範圍未定義

**Location**: specs/subscription-registry/spec.md 稽核紀錄（L169「每次 resolve、每次 get（過渡期）與每次
登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類、provider 與 model、until」）；
Scenario（L171-173）；tasks.md 1.4（L13）

**Finding**: 條文說「每次 resolve 記一筆」，欄位卻只有成功時成套（選中的 subscription id、解析出的
model）：409 `cooling`／`no_candidate`、403 `model_not_allowed`、400 等失敗回覆沒有選中的 subscription id
可記。現行程式的先例是**只記成功**（`lgwd/registry.py:76-78`：get 的稽核在所有驗證與 key 取得之後才寫；
`register_cooldown` L90-101 的稽核在驗證之後），但 spec 沒有說 resolve（與 cooldown 的驗證失敗）照不照這個先例。
實作者兩種讀法都成立：全部呼叫記一筆（失敗的 subscription 欄任填 null？）或只記成功。1.5 要求「spec 每個
服務端 Scenario」都變測試，但失敗呼叫的稽核沒有對應 Scenario，無法收斂。

**Evidence**: 同一份 MODIFIED requirement 對 get／cooldown 各有「（過渡期）」與「結束時刻 until（登錄
冷卻）」的欄位限定，唯獨 resolve 的記錄條件未限定結果。這是 delta spec 的驗收基準文字，不是 design 的措辭味道。

**Suggestion**: 加一句寫死，例如「稽核只記**成功**的 resolve／get／cooldown；失敗的診斷靠 HTTP log
（方法／路徑／狀態碼）」，或補 Scenario「resolve 回 409 `cooling` → 稽核沒有新筆」。

### 3. [nit] env 鍵表含現行不可達的「anthropic＋api-key」分支，spec 把它寫成活性需求

**Location**: specs/subscription-registry/spec.md L115（`ANTHROPIC_API_KEY`：「provider 為 anthropic 時
字串＝該把的 key、不可空」）；design.md 決定 2 的 env 表（L73「claude-code＋api-key（anthropic）」）

**Finding**: `kind` 在程式裡由 provider 類別寫死（`lgwd/providers.py:25-28`：
`AnthropicProvider.kind = "claude-subscription"`），政策檔無法改 kind，PROVIDERS 裡
provider=anthropic 的兩把永遠是 claude-subscription——「claude-code＋**api-key** 且 provider=anthropic」
在現行程式與政策下**不可達**。spec 對這個不可達組合寫「不可空、字串＝該把的 key」，實作與測試若照做會
產生一個今天驗不到、明天也不該單獨改鍵集的分支（要動它得 bump `env_schema`）。與 prompt 的
subtract-before-you-add／「設計給觀察到的用法」相較，這是純預留；「必要且只有這些」的原則下，它也多佔一條
spec 鍵集契約。

**Evidence**: 與表內其他列對比：其他 provider 的 api-key＋claude-code（例：ollama-cloud＋claude-code）是
可達且快照到的（allow `*`、endpoint `https://ollama.com`），是活的規格；anthropic 那格是唯一不可達的。
design L77 的「鍵集契約」把每格都掛上 bump 規則，不可達格等於把未想像未來鎖進現版契約。

**Suggestion**: 在 spec 該列與 design 該列註明「（現行不可達：anthropic 恆為 claude-subscription；
預留 anthropic api-key 形狀，未來啟用需 bump `env_schema`）」——保留預留、把「可測活性」的誤讀拿掉。
若要更貼「必要且只有這些」，就把該格移到未來單、現版只寫「anthropic」的 claude-subscription 那條。

### 4. [nit] endpoint 驗證掛在 `policy.load_policy` 末尾：檢查的資料所有權跨層

**Location**: design.md 決定 3 的 providers endpoint 檢查（L121「providers.validate_endpoints() …
`policy.load_policy` 末尾呼叫並轉成 `PolicyError`」）；tasks.md 1.0（L9）；spec.md L213；對照
`lgwd/policy.py:3-4`（模組自述「政策檔只管 priority、agents、models」）、`lgwd/main.py:197-208`（啟動組裝）、
`lgwd/main.py:290`（啟動錯誤接頭）；providers.py L50-56

**Finding**: `validate_endpoints()` 驗的是 `lgwd/providers.py` 的模組常數 `PROVIDERS`（與政策檔無關，
政策檔的 JSON 沒有 endpoint 欄位），但設計把「呼叫點」選在 `load_policy(path)` 末尾。後果：

1. `load_policy` 的契約變形：讀的是給定路徑的政策檔、失敗原因卻可能來自一個它沒讀過的全域表——未來任何
   以驗政策檔為目的的呼叫例（CI 的政策檔 lint、測試 helper）都會被 endpoint 錯誤攔下，且錯誤訊息與被驗
   目標無關；
2. 測試耦合：tasks 1.0 自己就寫明 policy 測試要 monkeypatch `PROVIDERS` 才能跑負向案例——測政策的函式
   得操縱另一層的全域；
3. 而且 spec L213 本來就把責任寫對了（「providers 載入／**lgwd 啟動時**」），把落點拉到「載入政策檔」那一刻
   是 design/tasks 的實作選擇，不是需求推論。

「避免循環 import」的理由成立（providers 不 import policy），但**呼叫點在 main（組裝根）同樣沒有循環**：
Gateway.__init__ 已組 policy／keys／service，且 `main()` 的啟動失敗接頭（L290）已捕 `PolicyError`。

**Evidence**: boundary-discipline 的標準問句「這份資料在這一層是被信任的還是剛進場的？」——endpoint 是
providers 層的出廠常數，政策檔 loader 不是它的系統邊界；這是「邏輯放錯層、且有明確 canonical home」的小型案例。

**Suggestion**: 保留 `providers.validate_endpoints()` 與固定字串 `ValueError`；把「呼叫＋轉型為
`PolicyError`（或新增啟動錯誤型別）」移到組裝根（Gateway.__init__ 在 load_policy 之後呼叫，保持
不合法不啟動與既有 catch 元組），design L121／tasks 1.0 的「load_policy 末尾呼叫」改為「Gateway
啟動時呼叫」；測試維持 monkeypatch `PROVIDERS`，但改以組裝點為測樣即可。spec 不用改（其措辭本來
就是 providers 載入／啟動時）。

## 已核對、無發現的重點（不另列為條目，作為本輪盡責範圍紀錄）

- **delta 對主規格的時序**：本 change 不 REMOVED get、歸檔 gate（tasks 5.4）與 proposal 頭註一致；
  主規格目前只有 agent-lease／usage-record（`openspec/specs/` 實測），「以 gateway-v0 的 delta 為底」的
  說法與 gateway-v0 的 subscription-registry delta 對得上。
- **選把規則與錯誤 precedence 的同軸性**：spec 步驟 1–8 與 design 決定 3、錯誤表、tasks 1.1 四處同構；
  抽驗 precedence 的兩個既有 Scenario（L207-209）與 exclude→not_deployed→cooling 的層序都自洽。
- **env 鍵集契約**：spec L112-127 的 8 列 10 鍵與 design L72-75、tasks 1.2、overview 介面圖、proposal L26
  鍵名一致；`OPENCODE_PROVIDER_ID` 歸「模型命名」（r3 低-2 已修）、`ANTHROPIC_API_KEY=""` 佔位與
  canary 跳過空字串（r3 已註記）不再提。
- **政策 loader 缺口是真實的**：`lgwd/policy.py:127-134` 確無 openrouter `agents` 檢查；tasks 1.0 的修法
  落點正確。
- **稽核欄位缺口的描述屬實**：`lgwd/store.py:23-30` 正是 `seq/ts/subscription/action/agent/until` 六欄；
  tasks 1.4 的 additive 遷移＋退版相容說法與現程式的 SELECT 指定欄位寫法相容。
- **值紀律的現況宣稱屬實**：HTTP log 目前只記方法／路徑／狀態碼（http.py L115-137，query 不入 log）；
  稽核與 get 的 200 無 Cache-Control（http.py L49-58），tasks 1.3 要補，與設計一致。
- **sidecar 的行為層**（build_env 細節、`ALLOWED_KEYS`、`_swap`）依 prompt 只到介面層級，本輪未查、
  不發現。
- **行數紅線**：無（見上方驗證紀錄）。

## 沒查到的地方

- sidecar 的實作細節（同前三輪）：`build_env`／_swap／AgentRecord 的實際欄位；本輪只核介面層級文本。
- `install.sh --check` 的版本輸出形狀；服務那台與 worker 現況（未連線；3.0–3.4 未執行）。
- opencode pinned 版本（2.0.26／2.0.32）對 legacy `provider`／`small_model` 的接受度（設計已列「沒實機可驗」）。
- OpenSpec `archive`／`apply` 實際行為（本輪一樣只跑 `validate --strict`）。
- **MyLinuxPool PM 的 B-1／B-3 原始回覆**（設計／tasks 的「已確認」依 notes 摘要，不在本 repo 可核）。

## 驗證紀錄

```
$ openspec validate --strict agent-provider-model-interface
Change 'agent-provider-model-interface' is valid   # exit 0

$ git -C <worktree> log --oneline -3
a44806a docs(#33): 本人七題裁定（2026-10-20）寫進設計，Open Questions 改為已決定
3d86487 Merge branch 'review/design-33' into design/issue33-iface（審查報告 r1～r3 入庫）
a60fef6 docs(#33): 修訂第 4 輪（小修）

$ wc -l lgwd/*.py   # Code Quality Lens 行數檢查
http.py 150 / registry.py 151 / providers.py 61 / policy.py 141 / store.py 99 / errors.py 67
／keys.py 104 / clock.py 55 / main.py 310 / keysync.py 351（lgwd 合計 1,500；新增後無檔案近 1,000）
```

結論：需小修（critical 0、warning 1、nit 3）。
