MiMo-V2.6-Pro

# #33 設計修訂審查（第 2 輪，spot review）：provider＋model 介面

審查對象：worktree `/Users/fatesaikou/testAI/LLMGateway-worktrees/review-33`（branch `review/design-33`）的
`openspec/changes/agent-provider-model-interface/`（proposal／design／tasks／specs/subscription-registry/spec.md）
與 `docs/research/design-33-overview.md`，即 `1f9993d → 663162d` 的修訂。只讀文件與本 repo 程式；
未改被審對象、未 push、未讀憑證檔、未連遠端機器。

驗證方式：`openspec validate --strict agent-provider-model-interface`（通過，exit 0）；
`git -C <wt> diff 1f9993d 663162d -- openspec docs/research/design-33-overview.md` 逐條對照
fix1 表格的「改了什麼」，不以表格自述為準；`lgwd/store.py`、`lgwd/policy.py`、`lgwd/errors.py`、
`policy/default.json`、`openspec/changes/gateway-v0/specs/subscription-registry/spec.md` 交叉核對事實。

結論先講：**需修**；新問題 高 0、中 2、低 6（第 1 輪高-1／中-1～中-10 全數已修，抽查的低 4 條全數已修）。

## 指定情境的回碼核對（prompt 第 2 點前半）

| 情境 | 回碼 | 文件一致性 |
|---|---|---|
| 釘住 `subscription`＋`provider` 不一致 | 400 `bad_request` | 一致（design 決定 1、錯誤表 L122；spec L11＋Scenario「釘住的 subscription 與 provider 不一致」L31–33；overview 介面圖 L98） |
| `subscription`＋`exclude` 同時給 | **文件寫的不同**：400 `bad_request` vs 「跳過 `exclude`」 | design 決定 1（L51）與 spec L11 說 400；design 決定 3 步驟 4（L107）與 spec 選把規則步驟 4（L74）卻說「跳過 `exclude`」＝當它可共存而忽略。見新-中-1 |
| 釘住的那把冷卻中 | 409 `cooling`＋`until`，不代換 | 一致（design 決定 3、錯誤表 L127；spec L80＋Scenario L27–29；overview） |
| 釘住的那把 key 未部署 | 409 `not_deployed` | 大體一致（design 錯誤表 L126「釘住時只看該把」；spec 選把步驟 4＋6 隱含相同）；但 spec 的「錯誤回覆」碼清單（L166–172）沒列 `not_deployed`（v5 表已有，可接受），也沒有對應 Scenario |

## 第 1 輪項目驗證

| 項目 | 已修／部分／沒修 | 依據的檔案位置 |
|---|---|---|
| 高-1 指定 claude-personal／claude-company 的能力消失 | 已修 | diff 確認 `663162d` 新增：design.md 決定 1 的 `subscription` 段（L50；不一致 400、未知 404、冷卻 409 不代換）、決定 3 步驟 4（L107）、錯誤表（L122–127）；spec.md resolve Requirement（L11）＋Scenario「釘住公司那一把」（L23–25）、「釘住的那把冷卻中」（L27–29）、「釘住的 subscription 與 provider 不一致」（L31–33）；tasks.md 3.4（L35）；overview 介面圖（L86）與未決 1。符合第 1 輪建議（選填 query 參數、非新端點、不開 `force`） |
| 中-1 env 值與稽核 MUST 矛盾 | 已修 | spec.md L15 限定「**憑證值** MUST NOT…model／provider／endpoint 是中介資料，稽核可記」；稽核 Requirement 同步（L154）；design.md 決定 2 值的紀律列明憑證鍵含過渡期 `OPENCODE_CONFIG_CONTENT` 內 apiKey（L89）；tasks.md 1.5 canary 只掃憑證值（L14）；overview L110–113 |
| 中-2 稽核表沒有 provider／model、沒有 DB 遷移 | 已修 | tasks.md 1.4（L13：`SCHEMA` 加欄、`PRAGMA table_info`＋`ALTER TABLE` additive、`audit_add`／`audit_all`）、1.5（L14：v5 DB 啟動寫入／舊 DB／舊 lgwd 讀新 DB）；spec.md Scenario「舊 DB 沒有新欄位」（L160–162）；design.md Risks（L195）。與程式現況對得上（`lgwd/store.py` L18–30 六欄、L78–98 固定欄位），遷移必要性屬實 |
| 中-3 `REMOVED get` 與並存期矛盾、歸檔無護欄 | 已修 | diff 確認 spec.md 刪掉整個 `REMOVED Requirements`；get 相關改「過渡期」條款（spec L3、L154、L192）；proposal.md 頭註（L3–5）；design.md 決定 5（L148–149：本 change 不 REMOVED＋歸檔 gate）；tasks.md 4.1（L39）、5.4（L46） |
| 中-4 sidecar 完整子行程環境寫成「resolve env＋維運變數」 | 已修 | design.md 決定 2 的 overlay 三層（L82–88：完整 base env→維運變數→resolve env 不篩選 overlay；自己行程的 `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 不漏進＋canary 測試）；spec.md env 組成（L114）；tasks.md 2.3／2.6；overview 實體圖（L23–24） |
| 中-5 實機驗收 3b／3c 缺映像／Zen 限制／假額度做法 | 已修 | tasks.md 3.0（L31：worker 映像＋`sidecar --version`）、3.2（L33：**新開 session**＋upstream 拒絕不算 resolve 失敗）、3.3（L34：手動 `POST /v1/subscriptions/{id}/cooldown`＋cooldown 登錄失敗驗 `exclude`）；design.md Migration 3.0/3b/3c（L203–206）；另補 3d 釘住演練。惟 3.0／3.1 的順序自相矛盾（見新-中-2） |
| 中-6 policy loader 沒擋 `openrouter.agents` | 已修 | tasks.md 1.0（L9：`agents == ("opencode",)`＋負向測試）；spec.md 共通 JSON schema「政策載入 MUST 拒絕」（L192）＋Scenario（L198–200）；design.md Context（L7）、決定 3（L117）。與程式現況對得上（`lgwd/policy.py` L103–107 確無此檢查） |
| 中-7 冷卻以 subscription 為單位、未決沒問 | 已修（保守分支） | design.md Risks 明示限制（L193）、未決 2（L213）；tasks.md 3.2 末句「只證明初始選擇」；overview 未決 2。未把 model 進 cooldown 紀錄，以「明示限制＋未決＋稽核記 model」滿足第 1 輪建議的「至少」分支，取捨理由有寫（fix1 沒改原因欄），可接受 |
| 中-8 沒有排除剛用完那把的參數 | 已修 | design.md 決定 1 `exclude`（L51）、決定 3（L108、L115：409 `no_candidate` 不回 `cooling`）、決定 6.6（L171：cooldown 失敗也帶 exclude）；spec.md（L11、L80、Scenario L39–45）；tasks.md 2.4；overview 時序圖（L67–68） |
| 中-9 `POST /agents` 遷移不足 | 已修 | design.md 決定 6.1 組合矩陣（L159–165：舊形狀不硬切、過渡期、呼叫端清冊、B-1 收斂標注 PM 對齊）、決定 4 `model` 是供應商 id＋前綴正規化（L141）；tasks.md 2.2（L22）、2.6；spec.md models.default（L204）＋Scenario（L210–212）；proposal Impact（L43） |
| 中-10 `schema` 沒有版本化 env 組成 | 已修 | design.md 決定 1 `env_schema`（L55：鍵集／語意版本、bump 規則、拒收）＋`schema` 語意定義（L54）；spec.md（L17）＋Scenario（L63–65）；tasks.md 1.3／1.5／2.6；overview 介面圖（L88）。是否該有兩個版本欄位另見新-低-4 |
| 低-3 `no_model` 選中後才判斷 | 已修 | spec.md 選把規則第 3 步把「能推導模型」列入候選條件（L73）＋Scenario（L102–104）；design.md 決定 3 步驟 3（L106）、決定 4（L138）。釘住路徑的步驟順序另見新-低-1 |
| 低-4 未知 agent 回 403 | 已修 | design.md 決定 1（L47）、錯誤表（L122）；spec.md（L11）＋Scenario（L51–53）、錯誤回覆 Scenario（L186–188）；tasks.md 1.1 |
| 低-8 query `+`／`&` 編碼沒講 | 已修 | design.md 決定 1（L56：client `quote(safe="")`、`+`→`%2B`、`&`／`#`／`:` 同理、server `parse_qs` form 解碼、`exclude` 逗號語意）；spec.md（L11 末句）；tasks.md 1.3／1.6＋`model=a+b` 測試 |
| 低-12 `meta.cooldown`／`meta.available` 恆定值 | 已修 | design.md 決定 1 meta 段（L52：移除兩欄＋理由）；spec.md（L13）＋Scenario（L55–57）；overview 介面圖（L89–90）；list 保留有寫理由 |

## 新問題

### 中

#### 新-中-1　`subscription`＋`exclude` 同時給：一處說 400，一處說「跳過 `exclude`」

- **位置**：design.md 決定 1（L51「`subscription` 與 `exclude` 同時給 → 400 `bad_request`（釘住與排除是兩個互斥的意圖）」）vs design.md 決定 3 步驟 4（L107「有 `subscription`（釘住）：候選只剩這一把；**跳過 `exclude`**」）；spec.md L11（MUST 回 400）vs spec.md 選把規則步驟 4（L74「候選只剩這一把，**跳過 `exclude`**」）。spec 的 Scenario「釘住與排除同時給」（L35–37）則預期 400。
- **問題**：同一份 design、同一份 spec 對同一個請求組合給出兩種行為：參數驗證層說這是 400 錯誤，選把規則層說這是合法請求、只是把 `exclude` 當沒給。實作者照 spec 選把規則做就會接受這個組合，照 resolve Requirement 做就會拒；兩者都能通過「照文件做」。
- **為什麼是問題**：這是本輪新增參數的核心交叉點（prompt 指定查核情境），且是規格內部矛盾（不是措辭不精準——「跳過」明示可共存）；spec 是驗收依據，Scenario 與選把規則不一致會讓測試與實作各自挑一邊。
- **建議**：定案一邊：既然同時給＝400（Scenario 已如此），把兩處「跳過 `exclude`」改成「此步不套 `exclude`（同時給已在參數驗證拒絕）」；或反過來決定「同時給＝忽略 `exclude`」，那就刪掉 400 條文與 Scenario。建議前者（互斥意圖的診斷比較有幫助）。

#### 新-中-2　實機部署順序自相矛盾：3.0／3.1 的編號順序與文字順序相反

- **位置**：tasks.md 3.0（L31「建／部署含新 sidecar 的 worker 映像」）排在 3.1（L32「部署新版 lgwd……順序在 worker 映像之前」）之前；design.md Migration 同樣把「3.0 建／部署 worker 映像」（L203）列在「3a 部署新版 lgwd」（L204）之前，卻在同一段寫「部署順序：lgwd 先、worker 映像後」。
- **問題**：照編號執行＝先部署 worker 映像、後部署 lgwd；照文字＝相反。「建／部署」明寫含部署，不是只建檔。兩種讀法互相矛盾。
- **為什麼是問題**：實機驗收（prompt 指定查核的排序一致性）是本單動到服務那台與 worker 的部分；順序錯了，新 sidecar 會先對舊 lgwd 打 resolve。雖有回退（design 6.2：resolve 404 回 get，不致停擺），但會讓 3.2／3.3 在錯誤的組合下跑、驗的不是設計要驗的東西；且兩份文件各自內部矛盾，照哪個做都算「照文件」。
- **建議**：把 3.0 拆成「3.0 建 worker 映像（可先做）」與部署兩步，或直接改編號順序為 lgwd 部署→worker 映像部署；design Migration 同步。順帶在 3.2 的通過條件寫明「lgwd 版本與 worker 映像版本都要對上（`--check`／`sidecar --version`）」。

### 低

#### 新-低-1　釘住＋該把推不出模型、同 provider 其他把可推導時，選把步驟交集為空，回碼未定

- **位置**：spec.md 選把規則步驟 3（L73，先全域過濾「能推導模型」）與步驟 4（L74，釘住只剩這一把）；design.md 決定 3 步驟 3／4（L106–107）同構。
- **問題**：`model` 省略、釘住的那把 `default` 為 null 且 `allow` 全萬用字元、但同 provider 另一把有 `default` 時：步驟 3 的「無一能推導 → 400 `no_model`」不觸發（另一把可推導），步驟 4 把候選限回釘住那把——那把已被步驟 3 濾掉，交集為空，回什麼沒有定義。Scenario「推不出模型」（L146–148）只涵蓋「沒有別人可推導」的形狀。
- **為什麼是問題**：spec 聲稱「不會出現選中後才發現推不出」（L136），但這句只覆蓋未釘住路徑；釘住路徑的步驟順序留了個未定義分支。今天五把不會觸發（anthropic 兩把都是 `claude-subscription`），但 spec 是政策層規則。
- **建議**：把步驟 4 的釘住限縮提到步驟 3 之前（先釘住限縮候選，再跑 2～3 與 6～7——步驟 4 的括號文字其實已是這個意思），或在步驟 3 加「釘住時只看該把，推不出 → 400 `no_model`」；補一個 Scenario。

#### 新-低-2　輸入錯誤的 precedence 未定：404 與 400 同時成立時回哪個沒有寫

- **位置**：design.md 決定 3 步驟 1（L104：provider 不存在／沒有 subscription → 404）vs 錯誤表 400 列（L122：`subscription` 與 `provider` 不一致）vs 404 列（L123：`subscription` 指定的 id 不存在）；spec.md L11 同構。
- **問題**：`provider=NoSuchProvider&subscription=claude-personal` 同時滿足「provider 不存在 → 404」與「subscription 與 provider 不一致 → 400」；`subscription=<未知 id>&exclude=x` 同時滿足「未知 id → 404」與「同時給 → 400」。回哪個沒有定義。
- **為什麼是問題**：小，但錯誤碼表是本單的驗收依據；控制者的除錯路徑會看到不同訊息。
- **建議**：在錯誤表加一行「同時成立時：400（輸入形狀錯誤）先於 404（查無）」或反之，一句話即可。

#### 新-低-3　proposal 的 meta 欄位清單漏 `agent`

- **位置**：proposal.md L22「meta（選中的 subscription、provider、kind、model、models（限制）、endpoint、priority）」vs design.md L52、spec.md L13、overview L89–90 都含 `agent`。
- **問題**：四份文件三份有 `agent`、proposal 漏；照 proposal 做的 client 會少讀一個欄位。
- **為什麼是問題**：純粹的內部不一致（prompt 第 3 點查核項）。
- **建議**：proposal L22 補 `agent`。

#### 新-低-4　`env_schema` 與 `schema` 兩個版本欄位，「必要且只有這些」可再議

- **位置**：design.md 決定 1（L54–55）；spec.md L17。
- **問題**：`subscription` 對應「公司／個人帳號選擇＋除錯驗收」（真實場景，非留不可）；`exclude` 對應「cooldown 登錄失敗仍要換把」（真實場景；替代＝失敗就停下重試 cooldown，會退步成 v5 換把的缺角，不建議拿掉）；`env_schema` 對應「Gateway 獨立升級改 env 鍵集」——但這也可以用「`schema` 語意＝回覆形狀＋env 鍵集，任一變更 bump」一個欄位做到（第 1 輪低-5 的替代方案）。現在是兩個版本號並存。
- **為什麼是問題**：本人原則是「介面必要且只有這些」；兩個版本號不是錯，但多一個可觀測欄位、多一條測試線。meta 形狀與 env 鍵集分開演進的實際好處（sidecar 對前者寬、對後者嚴）在文件裡沒有舉例。
- **建議**：二選一：(a) 維持兩欄，但在決定 1 補一句「meta 形狀與 env 鍵集獨立演進的場景」（例：加 meta 欄位不逼舊 sidecar 拒收）；(b) 合併成 `schema` 一個版本號，env 鍵集變更即 bump。我傾向 (a) 成本已付、(b) 更貼原則——請本人定。

#### 新-低-5　endpoint 被定為「中介資料可記」，若 endpoint 內嵌憑證會漏進稽核

- **位置**：design.md 決定 2 值的紀律（L89「model、provider、endpoint 是中介資料，稽核可記」）；spec.md L15、L154 同；`meta` 也回 `endpoint`。
- **問題**：「endpoint 是中介資料」的前提是 URL 不含憑證。部分供應商的接法會把 token 放 query 或 userinfo；若未來政策／providers 的 endpoint 這麼寫，值會進稽核、log（HTTP log 記路徑不含 query，但稽核會記 endpoint）、meta，而 canary 只掃四個憑證鍵的名字對應值，掃不到 URL 裡的 token。
- **為什麼是問題**：值的紀律修訂（中-1）是本輪的核心條文，這是它剩下的唯一明顯縫。
- **建議**：加一句「`endpoint` MUST NOT 內嵌憑證（政策 loader 或 providers 層保證）；canary 對 endpoint 值做 `key=`／`token=`／`@` 的樣式掃描」或等價條文；成本一行。

#### 新-低-6　`POST /agents` 組合矩陣是對 PM B-1 的重新解讀，建議列為 2.2 的開工條件

- **位置**：design.md 決定 6.1 的 B-1 收斂段（L164）；tasks.md 2.2（L22）。
- **問題**：矩陣把「`subscription`＋`provider`」解讀為釘住（合法），但 MyLinuxPool PM 的 B-1 字面是「`subscription` 與 `provider/model` 同時給→400」。文件有標注「PM 對齊時確認；若要 B-1 字面，需另定」，但 tasks 2.2 没有把 PM 確認列為開工條件（2.0 只 gate #216 排序）。
- **為什麼是問題**：兩個 repo 的介面交接點；若 PM 堅持字面，釘住入口在 sidecar 層就沒了，2.2 做完才發現會返工。
- **建議**：tasks 2.2 加「開工前 PM 對 B-1／B-3 的合併解讀確認」（或掛在 5.1 的交付物）；不影響本 repo 的 resolve 介面。

## 其他核對（prompt 第 2～4 點）

- **`no_candidate` 與既有錯誤碼表**：`no_candidate`（409）、`cooling`（409＋`until`）、`no_model`（400）、`model_not_allowed`（403）是新增；`not_deployed`、`agent_not_allowed`、`not_found`、`bad_request` 在 v5 表（gateway-v0 spec 錯誤回覆）與 `lgwd/errors.py` 都已存在，tasks 1.1 只加四個新碼——與程式現況一致，沒有漏。`no_candidate` 只在未釘住時可能（錯誤表 L128），與「同時給 400」自洽。
- **「介面必要且只有這些」場景對照**：`subscription`＝公司／個人帳號選擇＋除錯驗收（proposal L14、3.4）；`exclude`＝換把不原地打轉（中-8 的實際失敗模式）；`env_schema`＝Gateway 獨立升級護欄（中-10）。三者都有真實場景，可議的只有 `env_schema` 與 `schema` 的分工（新-低-4）。
- **值的紀律的縫**：錯誤回覆 MUST NOT 含 env＋固定字串不回顯（spec L15、L166；`lgwd/errors.py` 文件字串一致）；opencode 片段的 `OPENCODE_API_KEY` 明列為憑證值（design L89、tasks 1.5）；canary 範圍 Gateway／sidecar 分工清楚（design L89、tasks 5.5）；client `Result` repr 不印 data（tasks 1.6）。剩餘縫只有 endpoint 內嵌憑證（新-低-5）。
- **tasks 編號／依存**：1.0–1.7、2.0–2.7、3.0–3.4、4.1、5.1–5.5 無跳號；4 掛在 2／3 之後（4.1）；5.4 歸檔 gate 與 design 決定 5 一致；「#216 階段 2.5a 之後」與「排序以 PMO 為準」在 tasks 開頭、2.0、5.1、proposal Impact、design 6.8 五處一致。唯一問題是 3.0／3.1 順序（新-中-2）。
- **驗收標準**：仍是「worker 上 sidecar 開實作隊員＋審查隊員，兩個都拿到正確的模型」（tasks 3.2：兩個不同模型、status／第一個 step／model-mismatch、稽核兩筆、log／status 無憑證值）；3b 的「只證明初始選擇」註明與未決 2 一致。
- **內部一致**：端點（`GET /v1/resolve`、`GET /v1/subscriptions`、`GET /v1/subscriptions/{id}`、`POST /v1/subscriptions/{id}/cooldown`）五份文件一致；參數（`agent`／`provider`／`model`／`subscription`／`exclude`）一致；回覆形狀 `{schema, env_schema, meta, env}` 一致（meta 清單見新-低-3）；未決 design 與 overview 都是 6 題且同序（釘住額度用完／冷卻粒度／anthropic 預設選法／get 退場／meta 到期／不指定 provider），fix1 自述相符。
- **public repo 規矩**：新文字掃過 diff（IP／金鑰／絕對路徑／帳號樣式），沒有金鑰值、沒有位址；`MyLinuxPool#216`、`#229`、`MyPMO`、`MyAiStorage#85` 是工單／repo 代號引用（`MyAiStorage#85` 舊版已有、非新增），不含量值內容。`loopback 4710` 是本專案既有文件的連接埠。可以放行；若要更嚴，「來源是 private repo 的輪換表筆記」（overview L128）可收斂成「本人觀察（2026-10-17）」。
- **驗證紀錄**：

```
$ openspec validate --strict agent-provider-model-interface
Change 'agent-provider-model-interface' is valid   # exit 0

$ git diff 1f9993d 663162d --stat -- openspec docs/research/design-33-overview.md
 design-33-overview.md | 114 +++---   design.md | 207 +++++----
 proposal.md | 39 ++-   spec.md | 155 +++++++---   tasks.md | 54 +++--（5 files）
（spec 的 -## REMOVED Requirements 出現在 diff 刪除側，證實中-3 的 REMOVED 已移除）
```

## 沒查到的地方

- **sidecar `build_env`／`_swap` 的實作細節**：overlay 的實際鍵序、`POST /agents` 欄位白名單（`ALLOWED_KEYS`／`_JOB_ARGS["open"]`）現況、2.5a 的 `_swap` 改動——仍是 issue／文件層級，未見實作（同第 1 輪）。
- **MyLinuxPool PM 的 B-1／B-3 真意**：組合矩陣的「合併解讀」是否成立只有設計作者的推論；PM 未確認（新-低-6 的背景）。
- **opencode pinned 版本（2.0.26／2.0.32）對 legacy `provider`／`small_model` 的接受度**：仍只以官網 schema 核對 deprecated；無實機。
- **OpenSpec `archive`／`apply` 實際行為**：只跑 `validate --strict`；歸檔 gate 仍依 delta／REMOVED 語意推理。
- **`#33` issue 全文與「meta 到期」原意**：未決 5 無法核對（同第 1 輪）。
- **服務那台／worker 現況、Zen 限流單位**：未連線、未跑 `install.sh --check`；3.0–3.4 皆未執行（同第 1 輪）。

結論：需修
