## Context

動機見 proposal.md（#33）。影響做法的現況與限制（出處：本 repo 的 `docs/research/`、`openspec/changes/gateway-v0`、`policy/default.json`、`lgwd/`；sidecar 的事實只寫**介面層級**——它收什麼、呼叫哪個端點、環境怎麼組——來自 MyLinuxPool `modules/sidecar` 的現行文件與程式（private repo，不整段引用）：

- **現行 v5 介面**（`gateway-v0`，#8）：`GET /v1/subscriptions`（list，不含 key）、`GET /v1/subscriptions/{id}?agent=`（get，含 key）、`POST /v1/subscriptions/{id}/cooldown {until}`。共通 JSON row：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models{allow,default}`、`endpoint`、`key`（只在 get）、`cooldown{until|null}`、`available`。**row 不帶環境變數名**（v0 決定 7）：env 名跟 agent 版本走，而 agent 與 sidecar 一起釘在 worker 映像裡。稽核表目前是 `seq/ts/subscription/action/agent/until` 六欄（`lgwd/store.py`），**沒有 provider／model**。
- **政策檔**（`policy/default.json`）：五把 subscription（`claude-personal`／`claude-company`＝provider `anthropic`、`opencode-zen`、`openrouter`、`ollama-cloud`）；每把 `priority`、`agents`、`models.allow`（fnmatch 樣式）、`models.default`。openrouter 只允許免費模型；Claude 兩把 `agents` 只有 `claude-code`（條款）；zen 只有 `opencode`。**Claude 兩把的 `agents`、`models.allow`、`kind`、`endpoint` 完全相同，只有 priority（10／20）與 key 不同**——這是「必須能釘住某一把」的來源。
- **政策 loader 的缺口**：`lgwd/policy.py` 已擋 claude／zen 的 `agents` 與 openrouter 的 `models.allow`，但**沒有檢查 openrouter 的 `agents`**。v5 與本 change 都要求 openrouter 的 `agents` MUST 只有 `opencode`；政策檔若被改成 `["claude-code","opencode"]`，resolve 的 agent 守門會放行，把 Claude Code 指到 OpenRouter。本 change 補這個檢查（決定 3）。
- **sidecar 現況（介面層級）**：worker／Mac 上的 daemon；`POST /agents` 收 `id`（必填）、`agent`、`workdir`、`session`、`subscription: {"id": …}`（選填，省略時 sidecar 自己挑）、`input`。它對 LLMGateway 打三個端點（list／get／cooldown）。driver 的 `build_env(subscription)` 把（provider、kind、endpoint、key、models）組成子行程環境：claude 系＝`CLAUDE_CODE_OAUTH_TOKEN`（訂閱）或 `ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_BASE_URL`（api-key），模型進 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`；opencode＝`OPENCODE_CONFIG_CONTENT`（provider apiKey、model、permission）＋server password 等維運變數。模型＝`models.default`，否則 allow 第一個具體項；`claude-subscription`＋default null＝用 agent 自己的預設。
- **sidecar 的完整環境**：子行程環境是「完整環境」加上 PATH／HOME／Locale 等，再疊 sidecar 的維運／行為變數；sidecar **自己行程**的 `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 不會漏進子行程（現行 allowlist 紀律）。本設計把 env 的組成移進 Gateway 後，這個邊界要明寫（決定 2）。
- **換把現況**：額度訊號 → cooldown → list 挑下一個（priority）→ get → 同一個 agora session continue。sidecar 現行**會跳過剛用完的那一把，即使 cooldown 登錄失敗**；新設計若只靠 cooldown 成功來排除，登錄失敗時會選回同一把、原地打轉（決定 3）。
- **痛點**：指定只能到 subscription；同一把上別的免費模型點不到；`provider=anthropic` 時無法指定公司或個人那一把。
- **額度訊號**（`quota-signals.md`）：Zen 免費＝429 `FreeUsageLimitError`＋`retry-after`；sidecar 判讀不出恢復時間時報「現在＋1 小時」。**已知上游限制**（本人 2026-10-17 的觀察，MyAiStorage#85）：Zen 免費層只收 opencode 原生新開的 session；經匯入／接續的 session 用免費模型會被上游拒。換把在接續 session 上換到另一個 Zen 免費模型可能被上游拒——那是上游行為，resolve 擋不了，屬 sidecar 階梯設定的事（決定 7 的後續單）。
- **opencode config schema 的版本風險**：opencode 官網 config schema（2026-10-20 抓）把 top-level `provider` 與 `small_model` 標為 deprecated，新的 `providers` 用 `settings`。worker 釘的 opencode 版本（文件寫 2.0.26；Mac 2.0.32）是否仍接受 legacy 鍵，**沒有實機 binary 可驗**（列「沒查到」）。因此本設計**不讓 Gateway 組整包 `OPENCODE_CONFIG_CONTENT`**：Gateway 只回片段，config 的形狀由 sidecar 依 worker 釘的版本組（決定 2）。
- **#32（provider 追加可用模型）**：把政策檔各 provider 的 `models.allow` 補上具體模型；與本 change 互補（決定 7）。
- **歸檔狀態**：`gateway-v0` 尚未歸檔，主規格 `agent-lease` 仍是 v0 的「Claude 兩把平均分攤」舊語意；本 change 的 priority 選把要等 `gateway-v0` 歸檔後才成為主規格（proposal 的歸檔順序、tasks 5.4）。

## Goals / Non-Goals

**Goals**

- 呼叫方以 provider＋model（＋agent 種類）點菜；也可以選填 `subscription` 釘住某一把（公司／個人帳號、除錯）。審查可用同一把的不同免費模型。
- 介面「必要且只有這些」：終態對外仍是三個介面（list、resolve、cooldown）；`subscription`／`exclude` 都是 resolve 的 query 參數，不是新端點。
- 政策守門不變：`models.allow` 擋不該用的模型（例：會計費的），點了就當場拒，不代選；loader 補上 openrouter 的 `agents` 約束。
- 換把可排除剛耗盡的那一把（即使 cooldown 登錄失敗），不原地打轉。
- 值的紀律：resolve 回覆的**憑證值**只進 sidecar 建的子行程環境；不進 log、不進 status、不進稽核。model／provider／endpoint 是中介資料，稽核可記。
- env 組成的版本護欄：`env_schema` 讓 Gateway 獨立升級時，舊 sidecar 不會拿到不認得的鍵集。

**Non-Goals**

- 不給 command（指令、參數、終端、agora 接續＝sidecar 的事）。
- 不做資料面（請求代轉）、用量／額度查詢、限流。
- 不做租約式管控（同時上限、「誰正在用」）。
- 不處理 agy（政策無此 subscription；要支援另開單）。
- 不做冷卻細粒度（`(subscription, model)`）：本次維持 subscription 粒度，列未決 2。

## Decisions

### 決定 1：新介面是 `GET /v1/resolve`——輸入 agent＋provider（＋選填 model／subscription／exclude），輸出 meta＋env

| | |
|---|---|
| 誰呼叫 | sidecar（worker／Mac）；測試與 Mac 工具經 `lgw.client.resolve` |
| 操作 | `GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]]` |
| 範例 | `GET /v1/resolve?agent=opencode&provider=opencode-zen&model=big-pickle`；`GET /v1/resolve?agent=claude-code&provider=anthropic&subscription=claude-company` |
| 回傳 | 200 `{schema:1, env_schema:1, meta:{…}, env:{…}}`；錯誤 `{"error":"<碼>", …}`（決定 3） |

- `agent`：必填，`claude-code`｜`opencode`；**不是這兩個之一 → 400 `bad_request`**（不落到 `agent_not_allowed` 403：403 的語意是政策不允許，不是輸入打錯）。
- `provider`：必填，政策裡的 provider 名（`anthropic`、`opencode-zen`、`openrouter`、`ollama-cloud`）。**`anthropic` 蓋兩把 subscription**——不釘住時的預設選法見決定 3 與未決 3。
- `model`：選填，**供應商自己的模型 id**（與 `models.default` 同一命名空間；例 `mimo-v2.6-flash-free`、`openrouter/free`——可含 `/`）。省略時的語意見決定 4。
- `subscription`：選填，**釘住某一把 subscription**（高-1／PM 對齊 B-3）。有給時必須與該把的 `provider` 相同，不一致 → 400 `bad_request`；未知 id → 404 `not_found`。釘住時選把只在這一把上驗 agent／model／部署／冷卻，**不代換另一把**；冷卻中 → 409 `cooling`＋`until`，不換把。為什麼需要：`anthropic` 的兩把除了 priority 與 key 之外完全相同，沒有這個參數就無法要求「這次用公司帳號」或「這次用個人帳號」；公司／個人分離（計費、隱私、額度保留）與除錯／驗收都是真實場景。這不是新端點，終態仍是三個介面。
- `exclude`：選填，逗號分隔的 subscription id 清單；選把時先排除這些 id（不在政策檔的 id 忽略）。**為什麼需要**：sidecar 換把時要排除剛耗盡的那一把；現行 sidecar「cooldown 登錄失敗也繼續換把」，新設計若只靠冷卻登錄成功來排除，登錄失敗（400／409／網路）時 resolve 會選回同一把（`claude-personal` priority 10），sidecar 可能立刻重開同一把形成迴圈。`exclude` 是 query 參數，不是新端點，維持「三個介面」。`subscription` 與 `exclude` 同時給 → 400 `bad_request`（釘住與排除是兩個互斥的意圖）。
- `meta`（200 時）：`subscription`（選中的 id；冷卻登錄用它）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`——「限制」）、`endpoint`、`priority`。**不含 `cooldown`／`available`**：200 已代表可用，這兩個在 resolve 回覆恆為 null／true，不帶資訊，違反「必要且只有這些」；list 仍保留它們（菜單要顯示）。
- `env`（200 時）：名稱→值（決定 2）。錯誤回覆 MUST NOT 含 `env`。
- `schema`：**該端點回覆形狀的版本**（resolve 與 list 各自獨立；目前都是 1）。resolve 形狀改變時 bump。sidecar 對不認得的更高版本拒收。
- `env_schema`：**env 組成（鍵集與鍵的語意）的版本**（目前 1）。新增／移除 env 鍵、或改變某個鍵的語意時 MUST bump；Gateway MUST NOT 在同一個 `env_schema` 內改變鍵集（個別回應因 model 為 null 而少給模型鍵，不算鍵集變更）。sidecar 對不認得的高版本拒收（較低版本若仍支援則接受）。這是中-10 的護欄：Gateway 獨立升級、worker 映像不動時，舊 sidecar 不會拿到不認得的鍵集。
- **為什麼兩個版本欄（新-低-4）**：meta 形狀與 env 鍵集獨立演進——例：resolve 加一個 meta 欄位（`schema` bump）而 env 鍵集不變（`env_schema` 不 bump），sidecar 收到的信號是「只有 meta 形狀變了」、env 的處理不需重驗；反之改 env 鍵集只 bump `env_schema`、回覆形狀版本不抬。這也保留未來把 `schema` 一側放寬（meta 欄位 additive：sidecar 讀已知欄位、忽略新欄位）而不動 `env_schema` 嚴格護欄的空間。合併成一個版本號的替代（任何變更都 bump、sidecar 全量重驗）列未決 7，本人裁定。
- **為什麼 GET＋query**：v5 的 get 已是「GET＋query 回 key」的先例；model id 可含 `/`（例 `openrouter/free`），放 query 參數不必處理路徑段的 percent-encoding。**query 的編碼**：呼叫端 MUST 對每個 query 值做 percent-encode（`lgw.client` 用 `urllib.parse.quote(value, safe="")`）；`+` 要編成 `%2B`（server 的 `parse_qs` 會把未編碼的 `+` 解成空白），`&`、`#`、`:` 同理；server 用 `parse_qs` 的預設 form 解碼。`exclude` 的逗號是清單分隔符，值內的逗號不合法（id 不會有逗號）。
- **200 的標頭**：resolve（以及並存期的 get）回應 MUST 帶 `Cache-Control: no-store` 與 `Pragma: no-cache`——回覆含憑證值，不讓 HTTP 快取或本機工具留下。
- **為什麼第四個端點、終態仍是三個**：resolve 取代 get（決定 5）；get 的移除在 d 單。
- **放棄的選項**：
  - POST＋body：查詢性的呼叫多一條 body 解析與 413 路徑，沒有對應的好處。
  - `GET /v1/providers/{p}/models/{m}`：model 帶 `/` 時路徑段要 percent-encode，容易做錯；query 一律安全。
  - 端點叫 `/v1/env`：名字只講了輸出的一半（meta 也是重點）；resolve 講的是「把 provider＋model 解析成那一把＋環境」這個動作。
  - 拆 provider 名（`anthropic-company`／`anthropic-personal`）：會讓 list 的 `models`、cooldown 的 id 語意分叉，且 policy 的 provider 概念被訂閱 id 污染；用 `subscription` 參數更小。

### 決定 2：env 由 LLMGateway 組（名稱＋值）；opencode 只回片段，config 由 sidecar 組

LLMGateway 依（provider、kind、agent）組 `env`：

| agent＋kind | env |
|---|---|
| claude-code＋claude-subscription | `CLAUDE_CODE_OAUTH_TOKEN=<key>`；model 非 null 時 `ANTHROPIC_MODEL=<model>`、`ANTHROPIC_SMALL_FAST_MODEL=<model>` |
| claude-code＋api-key（anthropic） | `ANTHROPIC_API_KEY=<key>`；model 變數同上 |
| claude-code＋api-key（其他 provider） | `ANTHROPIC_AUTH_TOKEN=<key>`、`ANTHROPIC_API_KEY=""`；`endpoint` 非 null 時 `ANTHROPIC_BASE_URL=<endpoint>`；model 變數同上 |
| opencode（api-key） | 片段：`OPENCODE_API_KEY=<key>`、`OPENCODE_PROVIDER_ID=<opencode 的 provider id>`；model 非 null 時 `OPENCODE_MODEL=<供應商 model id>`、`OPENCODE_SMALL_MODEL=<供應商 model id>`。**不回 `OPENCODE_CONFIG_CONTENT`** |

- **opencode config 由 sidecar 組**（PM 對齊 B-2；中-4、低-7）：sidecar 用上述片段＋自己的 `permission`／`instructions`＋worker 釘的 opencode 版本，組成 `OPENCODE_CONFIG_CONTENT`（例：`provider.<OPENCODE_PROVIDER_ID>.options.apiKey=<OPENCODE_API_KEY>`、`provider.<id>.models.<OPENCODE_MODEL>.name`、`model`／`small_model`＝`<OPENCODE_PROVIDER_ID>/<OPENCODE_MODEL>`；不加 `baseURL`——`endpoint` 是 provider 根位址，依 #28 不抄進 opencode 的 `options.baseURL`，內建 provider 自帶正確端點）。**`permission`／`instructions` 由 sidecar 擁有**：Gateway 的 env MUST NOT 含這兩個鍵；同名鍵 sidecar 贏（目前兩邊的鍵集沒有重疊，這條是將來的合併規則）。**原未決「opencode 的 permission 歸屬」因此關閉**：B-2 已定 sidecar 擁有 permission／instructions 與 config 形狀，Gateway 只給片段。
- **為什麼回片段、不回整包 JSON**：
  1. opencode 的 config schema 在 v2 已把 top-level `provider`／`small_model` 標 deprecated（新的 `providers` 用 `settings`）。Gateway 若組整包 config，就把「config 形狀」綁進 Gateway 的版本；worker 釘的 opencode 版本不同時，Gateway 獨立升級會送出不認得的形狀。片段是 agent 中立的資料（provider id、api key、model id），config 形狀由**跟著 agent 版本走的 sidecar** 決定——這正好保留 v0 決定 7 原本的理由（env 名／形狀跟 agent 版本走），只把「值與 provider 對映」移到 Gateway。
  2. `permission`／`instructions` 是 sidecar 的行為政策（#233／#216 的擁有權），不該跨到 Gateway；整包 JSON 會讓 Gateway 碰到它不該知道的鍵。
  3. 片段讓 `env_schema` 的鍵集穩定、可測試；整包 JSON 的內部形狀改動不會被 `env_schema` 抓到（低-7／中-10）。
  - 若未來某個 opencode 版本需要 `baseURL` 或別的鍵，sidecar 在自己的版本內加；Gateway 的片段不變。
- **sidecar 的完整環境與 overlay（中-4）**：sidecar 子行程環境的組法是：
  1. **完整 base env**（PATH、HOME、Locale、TMPDIR 等）——照現行；
  2. sidecar 自己的維運／行為變數（auto-update／non-essential traffic 開關、`OPENCODE_SERVER_PASSWORD`、`AGORA_CLAUDE_CMD`、`MLP_AGENT_NAME`／`ANGAREION_IDENTITY`（#216）等）；
  3. **resolve 的 `env` 全部鍵的不篩選 overlay**（Gateway 是受信任來源；不另做 allowlist，否則 Gateway 新增的鍵會被舊 sidecar 丟掉）。
  - 同名鍵：resolve 的 env 覆蓋 base env（憑證／模型值是 resolve 的權威）；sidecar 的維運變數不得與 resolve 的鍵同名（目前沒有）。
  - sidecar **自己行程繼承來的** `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` MUST NOT 原樣漏進子行程（現行紀律）；sidecar 要給子行程的維運變數（例 `OPENCODE_SERVER_PASSWORD`）是另外疊的，不在此限。子行程只看到 resolve 給的那一把的憑證變數。測試：sidecar 行程自己有 `ANTHROPIC_API_KEY=canary`，子行程只看到 resolve 的值或沒有。
  - 過渡期：新 sidecar 若收到舊版 Gateway 的整包 `OPENCODE_CONFIG_CONTENT`（get 路徑），照舊解析；新版 Gateway 回片段時改組裝。以「有沒有 `OPENCODE_CONFIG_CONTENT`」判斷，不靠版本猜。
- **值的紀律（中-1）**：resolve 回覆的**憑證值**（`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`OPENCODE_API_KEY`，以及任何含 key 的值；過渡期 get 的 `OPENCODE_CONFIG_CONTENT` 內 apiKey 也算）MUST NOT 進 LLMGateway 的 log 與稽核，MUST NOT 進 sidecar 的 log 與 `GET /agents` status；值只存在 sidecar 建的子行程環境（不上命令列、不寫檔——sidecar 既有紀律＋resolve 的值同一套）。**model、provider、endpoint 是中介資料，稽核可記**（否則「誰用了哪個模型」就沒了）；前提：**`endpoint` MUST NOT 內嵌憑證**（新-低-5）——政策檔的 endpoint 值不得含 query 憑證（`key=`／`token=`）或 userinfo（`@`）樣式，政策 loader 載入時檢查並拒絕（`PolicyError`，固定字串，與決定 3 的 openrouter 檢查同一處）；否則 URL 內的 token 會隨「中介資料」進稽核／meta，而 canary 掃不到（它只掃憑證鍵的值）。canary 只掃憑證值（含 env 裡的 key），不掃 model／provider／endpoint 的值本身，另對 endpoint 值加 `key=`／`token=`／`@` 的樣式掃描（loader 已擋，canary 是第二道）。Gateway 的 canary 掃 Gateway 的 log／DB／稽核／list，sidecar 的 log／status 由 MyLinuxPool 的 c 單加 canary。HTTP log 照 v5 只記方法／路徑／狀態碼（query 只有 agent／provider／model／subscription／exclude，非機密）。
- **為什麼反轉 v0 決定 7（row 不帶環境變數名）**：
  1. 痛點本體是「模型選擇＋憑證接法綁在 sidecar driver」：新 provider／新接法都要改 sidecar、重建 worker 映像。移進 Gateway 後，加 provider＝改 Gateway（政策＋providers＋env 組成），sidecar 不動（opencode 的 config 形狀仍由 sidecar 依版本組，這是刻意的邊界）。
  2. 版本耦合是雙向的：v0 的理由是「env 名跟 agent 版本走、sidecar 與 agent 同映像」；但 Gateway 也是唯一升級點（tag），憑證／端點／模型對映集中後「上游／政策變了全體跟上」不用逐台 worker 重建。`env_schema` 是這個反轉的護欄。
  3. issue 明文：輸出就是「必要的環境變數」；「不給 command」已把行為邊界劃給 sidecar。
- **放棄的選項**：
  - 維持 v0（Gateway 只給 key＋meta、sidecar 組 env）：痛點仍在（審查換模型還是要動 sidecar），與 issue 相違。
  - Gateway 組「完整子行程環境」：`AGORA_CLAUDE_CMD`、`OPENCODE_SERVER_PASSWORD`、身分變數是 sidecar 的維運狀態，Gateway 不知道也不該知道。
  - Gateway 回整包 `OPENCODE_CONFIG_CONTENT`：把 opencode config 形狀綁進 Gateway（低-7），且 `permission`／`instructions` 跨到 Gateway（B-2 要 sidecar 擁有）。
  - env 給名稱清單、值另開一個端點取：多一個介面（違反「必要且只有這些」），值終究走同一條線。

### 決定 3：選把規則與錯誤碼（釘住、同 provider 多把、冷卻、allow 不含、排除）

**選把**（依序；釘住限縮最前——新-低-1：先限縮候選，再過濾 provider／agent／model／可推導模型／部署／冷卻）：

1. **有 `subscription`（釘住）**：候選**先限縮為這一把**，後續每一步只在這一把上判斷；不代換另一把（該把推不出模型 → 400 `no_model`，同 provider 另一把可推導也不代換）。此步不套 `exclude`——同時給已在參數驗證回 400 `bad_request`（新-中-1）；未知 id 也在參數驗證回 404。
2. `provider` 不存在 → 404 `not_found`；該 provider 沒有任何 subscription → 404 `not_found`。
3. 候選＝`provider` 相同、`agents` 含請求的 agent；無一允許 → 403 `agent_not_allowed`。
4. `model` 有給時，候選限於 `models.allow` 樣式（fnmatch）涵蓋的；無一涵蓋 → 403 `model_not_allowed`。`model` 省略時，候選限於**能推導模型**的（kind 為 `claude-subscription`，或 `models.default` 非 null，或 `allow` 至少一個不含萬用字元的樣式）；無一能推導 → 400 `no_model`（釘住時就是該把）。這一步在 priority 之前做，避免「最高 priority 的候選推不出模型、下一個可以」卻直接 400（低-3）。
5. **沒有釘住**：套 `exclude`（不在政策檔的 id 忽略）→ 沒有候選 → 409 `no_candidate`（沒有恢復時刻；這是呼叫方自己排除造成的，不是冷卻）。
6. 排除 key 未部署的候選；全部未部署 → 409 `not_deployed`。
7. 排除冷卻中的（`cooldown` 有未來的 `until`）；全部冷卻 → 409 `cooling`，`until`＝最早恢復時刻。
8. 挑 `priority` 最小；同分比 id（與 v5 sidecar 的選擇規則同一機械；政策檔是權威）。

- **釘住的那把冷卻中**：回 409 `cooling`＋`until`，**不是** get 的「照回 key、`available:false`」。冷卻＝額度已死；釘住是帳號選擇，不是繞過冷卻的入口。若未來要「指定冷卻也照辦」，另開明確的 `force`，本次不做（高-1）。
- **釘住的那把額度用完時要不要自動換另一把**：這是政策問題，列未決 1；**在本人裁定前，resolve 與 sidecar 的安全預設是不換**（回報失敗，讓控制者決定）。理由：釘住是呼叫方明確的帳號選擇（隱私／計費／額度保留），自動換到另一把會違反意圖；未釘住的路徑才照 priority 自動接力。
- **`exclude` 後沒有候選**：回 409 `no_candidate`，sidecar 停下、回報、等控制者或冷卻解除；不回 `cooling`（沒有恢復時刻），也不選回被排除的那把（中-8）。
- **為什麼排除冷卻、全部冷卻回 409 帶 `until`**：一次往返把「挑」做完（sidecar 不用自己迴圈試）；`until` 讓 sidecar 直接把自動重開 job 排到那個時刻（同今天「全部冷卻 → waiting-quota＋最早解除時刻」的行為）。
- **政策 loader 的 openrouter 檢查（中-6）**：`load_policy` 加 `openrouter.agents == ("opencode",)`，不合法丟 `PolicyError`（固定字串）；加負向測試。否則 spec 的 MUST 與 loader 各說各話，resolve 對 `provider=openrouter&agent=claude-code` 會回 200。
- **錯誤碼**（疊在 v5 的表上；錯誤回覆 MUST NOT 含 `env`，訊息固定字串、不回顯請求值）：

| 狀態 | 碼 | 什麼時候 | 附帶 |
|---|---|---|---|
| 400 | `bad_request` | 缺 `agent`／`provider`；`agent` 不是 `claude-code`／`opencode`；`subscription` 與 `provider` 不一致；`subscription` 與 `exclude` 同時給 | `missing`（缺欄位時） |
| 404 | `not_found` | 沒有任何 subscription 是這個 provider；`subscription` 指定的 id 不存在 | |
| 403 | `agent_not_allowed` | 有該 provider 的 subscription，但無一允許這個 agent（釘住時只看該把） | |
| 403 | `model_not_allowed` | 有 provider＋agent 的候選，但 model 不被任何候選的 `allow` 涵蓋（政策守門：擋不開放／會計費的模型；釘住時只看該把） | |
| 409 | `not_deployed` | 有涵蓋 model 的候選，但全部 key 未部署（釘住時只看該把） | |
| 409 | `cooling` | 有部署的候選，但全部冷卻中（釘住時就是該把） | `until`＝最早恢復時刻 |
| 409 | `no_candidate` | `exclude` 排除後沒有候選（只有在沒有釘住時可能） | |
| 400 | `no_model` | `model` 省略且沒有候選能推導模型（釘住時就是該把——同 provider 另一把可推導也不代換） | |

- **錯誤 precedence（新-低-2）**：多個錯誤同時成立時，回最前面的層級——**輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403 `agent_not_allowed`／`model_not_allowed`，與 400 `no_model`）先於候選狀態（409 `no_candidate`／`not_deployed`／`cooling`）**。例：`provider=NoSuchProvider&subscription=claude-personal` → 400 `bad_request`（`subscription` 與 `provider` 不一致先於 provider 查無）；`subscription=<未知 id>&exclude=x` → 400（同時給先於未知 id 的 404）。這個順序與選把的步驟同一軸：形狀驗證在選把之前，404 在步驟 2，政策在步驟 3～4，狀態在步驟 5～7。
- **放棄的選項**：
  - get 語意（冷卻中照回、`available:false`、呼叫方自己決定）：呼叫方要自己迴圈，而且會拿到不該用的 key。「指定一把就算冷卻也照辦」的舊能力在 provider＋model 的介面裡沒有對應物；釘住只保留「選哪一把」，不保留「繞過冷卻」。
  - 平均分攤（agent-lease 時代的選法）：v5 起沒有用量資料可攤；priority＋冷卻已達成「兩把都用得到」（一把額度死 → 冷卻 → 下一把自動接力）。未釘住時的預設選法列未決 3。
  - 用 `subscription` 參數取代 `provider`（只給 subscription）：會讓 list 的 provider 概念與 resolve 的輸入分叉；provider 仍是必填，subscription 只是釘住。

### 決定 4：model 省略時的語意（sidecar 現行的推導原封不動搬進來）

`model` 省略時：用選中那把的 `models.default`；default 為 null 時——kind 為 `claude-subscription` → 用 agent 自己的預設（`meta.model` null、env 不帶模型變數）；其他 kind → `models.allow` 第一個不含萬用字元的項；兩者都沒有 → 400 `no_model`（不代選）。釘住時用釘住那把的 default；沒有釘住時，候選已在決定 3 的第 4 步限於「能推導模型」的，釘住時則先限縮成那把、推不出就直接 400——兩條路徑都不會出現「選中後才發現推不出、但另一個候選可以」的情況。

- **為什麼**：「不指定模型」的既有開法（例：worker 的 claude-code 兩把、zen 的預設）行為不變；規則與 sidecar 現行 `build_env` 的推導一致，搬的是同一個機械。
- **`model` 的命名**：一律是供應商 id，不帶 opencode 的 provider 前綴（`meta.model` 也不帶）。sidecar 的 `POST /agents` 若收到 `<provider>/<model>`（例 `opencode/big-pickle`），由 sidecar 正規化（前綴等於該 provider 的 opencode id 時去掉），否則回 400 `invalid_request`；Gateway 不做這個正規化（`model=opencode/big-pickle` 會被 `*-free`／`big-pickle` 擋成 403 `model_not_allowed`）。
- **放棄的選項**：`model` 必填——`claude-subscription`「用 agent 自己的預設」就沒有入口，破壞現行用法；v5 spec（models.default 的語意）已定義 null 的意思，照用。

### 決定 5：跟 v5 的關係——並存遷移，終態 resolve 取代 get；本 change 不移除 get

- **並存期**：resolve 上線後 get 照常（舊 sidecar 釘在 worker 映像裡）；list、cooldown 不動；`lgw.client` 加 `resolve`、`get` 保留。
- **終態**：sidecar 遷移＋實機驗收後，另開 d 單移除 get 與 `lgw.client.get`；對外又是三個介面：**list、resolve、cooldown**。
- **本 change 的 delta 不 `REMOVED` get**（中-3）：get 是 `gateway-v0` 的 ADDED requirement，而 `gateway-v0` 尚未歸檔；本 change 若直接 REMOVED，`openspec archive` 後的主規格會說 get 不存在，但實作與舊 sidecar 還在用。把 REMOVED 移到 d 單，d 單的 delta 以那時的主規格為底，時序才一致。本 change 的「稽核紀錄」「共通 JSON schema」提到 get 的地方都是**過渡條款**（d 單完成前的行為），不是終態。
- **歸檔 gate（tasks 5.4）**：`gateway-v0` 已歸檔（本 delta 的基準存在於主規格）＋本 change 的 1 實作完成＋sidecar 遷移與 3 驗收完成，才 `openspec archive` 本 change。get 的移除不是本 change 的歸檔條件；d 單的開單條件是 sidecar 遷移＋驗收＋get 呼叫為零。
- **list 不變的理由**：它是菜單與階梯的資料來源（priority、`models.allow`／default、cooldown、available）——「不指定 provider＋model 時 sidecar 挑預設」與「換把挑下一個」「自動重開算最早解除時刻」都還要它。
- **cooldown 不變的理由**：冷卻的主體是 subscription 的額度；resolve 的 meta 給 `subscription`，剛好接上（sidecar 換把時用它登錄）。冷卻粒度另列未決 2。
- **為什麼並存**：worker 映像裡的舊 sidecar 不能即時換；Gateway 先加後減，任何時刻都有一側可用；退版＝`install.sh --tag` 回舊 tag。部署順序：Gateway 先、sidecar 映像後；退版 Gateway 時，新 sidecar 在 resolve 404 回 get（design 6.2）。
- **放棄的選項**：直接換掉（舊 sidecar 全 404，worker 停擺）；永遠並存（違反「必要且只有這些」）；本 change 直接 REMOVED get（規格與實作時序矛盾）。

### 決定 6：對 sidecar 的介面需求與建議（給 MyLinuxPool 的 PM 對齊；本文只寫介面層級）

**需求**（這些成立，本設計才完整）：

1. **`POST /agents` 的形狀與過渡期（中-9、B-1、B-3）**：保留必填 `id`、`agent`、`workdir`、選填 `session`／`input`；新形狀是平面 `provider`＋選填 `model`，並可帶選填 `subscription` 釘住。**`subscription` 沿用現行物件形狀 `{"id": …}`**（不是 resolve 的平面字串；sidecar 取 `.id` 傳給 resolve）。組合矩陣：
   - 只給 `subscription`（沒有 `provider`／`model`）→ **舊形狀**，走現行 get（過渡期）。這是合法功能，不硬切。
   - 三者都沒給（`provider`／`model`／`subscription` 都省略）→ sidecar 從 list 挑預設（priority＋可用性），再帶 provider＋model 去 resolve（不釘住）。
   - 給 `provider`（`model` 選填）→ **新形狀**，走 resolve；此時 `subscription` 若有給，解讀為**釘住**（不是舊形狀），sidecar 把它傳給 resolve。
   - `subscription`＋`model` 但沒有 `provider`；或 `model` 沒有 `provider`；或 `subscription` 的 provider 與 `provider` 不一致 → 400 `invalid_request`（帶明確訊息）。這些是殘缺／矛盾的組合，不是合法舊形狀。
   - **B-1 的「`subscription` 與 `provider/model` 同時給→400」收斂為**：舊形狀（只給 `subscription`）不得與新形狀欄位混用；新形狀內的 `subscription` 是釘住。這是 B-1 與 B-3 的合併解讀，PM 對齊時確認（tasks 2.2 的開工條件——確認前不開工，避免 PM 堅持字面時釘住入口返工）；若 MyLinuxPool PM 要 B-1 字面的全面 400，過渡期就沒有釘住入口，需另定（不是本人未決，屬 PM 對齊項）。
   - 呼叫端清冊（PM 腳本、dispatch、驗收腳本）與過渡期：舊形狀至少保留到 d 單；新形狀與舊形狀並存，不一次硬切合法功能。
   - `model` 是供應商 id；sidecar 若收到 `<provider>/<model>` 要正規化或回 400（決定 4）。
2. **來源（subscription source）**：對 LLMGateway 改呼 list／resolve／cooldown；resolve 404 時回 get（支援舊 Gateway／退版）；錯誤對照補 409 `cooling`（帶 `until` → 自動重開排到 until）、409 `no_candidate`（停下回報）、403 `model_not_allowed`、400 `no_model`（回給控制者）；其餘對照照舊。**以 resolve 的 `meta.subscription` 為準**：sidecar 先用 list 挑候選時，若 resolve 選出的 subscription 與 list 的選擇不同，記 log／回報（不要兩邊各用一個答案）。sidecar 對控制者的錯誤仍是 sidecar 的巢狀 `{"error": {"code": …, "message": …}}`；Gateway 的碼是內部映射，不要把 Gateway 的平面 `{"error": "<碼>"}` 直接對外（資訊-6）。
3. **driver**：`build_env` 的職責改為「sidecar 完整 base env＋sidecar 維運變數＋resolve env 的不篩選 overlay」（決定 2）；opencode 的 `OPENCODE_CONFIG_CONTENT` 由 sidecar 用 resolve 的片段＋`permission`／`instructions` 組（sidecar 擁有這兩個鍵，也擁有 config 形狀）；model-mismatch 檢查照舊（第一個 step 的模型＝`meta.model` 的期待值）。過渡期收到舊 Gateway 的整包 config 時照舊解析。
4. **`GET /agents` status**：**保留**既有的 `subscription`（低-1），加 `provider`／`model`（echo meta；**不含 env 值**）。
5. **agora 接續**：換把後同 session continue 時，resolve 來的模型／憑證變數（例 `ANTHROPIC_MODEL`）照今天一樣要讓 `agora continue` 起的行程看到；#229 的互動終端路徑同樣只吃「疊好的 env」。
6. **換把路徑（中-8）**：額度訊號 → cooldown（`meta.subscription`）→ resolve 帶 `exclude=<剛耗盡的 id>`（即使 cooldown 登錄失敗也帶）→ 新 env build 一次 → 同一個 agora session continue。`no_candidate` → 停下回報；`cooling`＋until → 自動重開排到 until。**釘住的那把**額度用完時，依未決 1 的安全預設：不自動換，回報失敗。
7. **值的紀律照舊並擴及 resolve 的值**：不上命令列、不進 log、不寫進任何檔案、不進 status；憑證值的 canary 在 Gateway（log／DB／稽核／list）與 MyLinuxPool 的 c 單（sidecar 的 log／status）各掃各的。
8. **排序**：sidecar 的實作排在 MyLinuxPool#216 階段 2.5a（換把／自動重開重 render harness，改 `_swap`）merge 之後；#216 的 2a／2.6／2.8（opencode pin 2.0.32）／階段 3、#229 也會碰到 `POST /agents` 或 `_swap`，合併時對一次。**這是排序建議，排序以 PMO 為準**。

**建議**（非必須）：錯誤對照表補 `no_candidate` 的映射；`GET /jobs` 的 open job 顯示 provider／model。

**不撞進行中的工單**：#216（id／harness／身分）與 #229（互動 PM、hook 狀態）與本設計正交；唯一交會是 core 的 `POST /agents` 欄位白名單（ALLOWED_KEYS）與 `_swap` 路徑，合併時對一次即可。

### 決定 7：跟 #32 的關係（介面與菜單；先後不互相依賴）

- **#33 先做也先解痛**：resolve 以精確模型名對 `models.allow` 樣式比對，現行 allow（`*-free`、`big-pickle`）已經點得到同一把的不同免費模型——審查換模型**不需要等 #32**。
- **#32 跟著補菜單**：把各 provider 的 allow 補上具體模型後，list 變成可列舉的菜單（人與 sidecar 都能枚舉「有哪些 model 可點」），也為 (provider, model) 粒度的換把階梯鋪路（冷卻粒度見未決 2；階梯本身是決定 7 的後續單）。
- **不互相依賴**：#32 只改政策檔內容（介面形狀不動）；#33 不改政策語意（只補 loader 對既有 MUST 的檢查）。要一起做的只有驗收時的範例模型選擇——#32 前後都能驗（用 allow 已蓋得到的模型）。
- **之後另開單**：(provider, model) 粒度的 fallback 階梯（例：Zen 免費 A 限流 → Zen 免費 B → 下一層），需要 #32 的具體清單＋sidecar 的階梯設定，還要考慮「接續 session 不能用 Zen 免費」的上游限制（Context）。

## Risks / Trade-offs

- **[env 組成與 agent 版本偏移]**（Gateway 獨立升級、agent 釘在 worker 映像）→ 用 `env_schema` 版本化 env 的鍵集與語意（中-10）：新增／移除鍵或改變語意時 bump，sidecar 對不認得的高版本拒收；Gateway 只在同一 `env_schema` 內改值。opencode 的 config 形狀由 sidecar 依 worker 釘的版本組，Gateway 只給片段（低-7）；worker 換釘 opencode 版本時，sidecar 要驗片段到 config 的映射（進該換釘單的驗收）。
- **[opencode 的 deprecated 鍵]**：官網 v2 schema 把 top-level `provider`／`small_model` 標 deprecated；本設計不讓 Gateway 碰這個形狀，但 sidecar 在 pinned 版本（2.0.26／2.0.32）是否仍接受 legacy 鍵**沒有實機證據**（列「沒查到」）。升級 opencode 時要驗；若要用 `providers`／`settings`，只改 sidecar。
- **[resolve 回覆帶值]** → 與 v5 get 同等的暴露面（SSH 通道內、無應用層認證——本人已接受的 v5 決定 5a）；resolve／get 加 `Cache-Control: no-store`／`Pragma: no-cache`；紀律把稽核／log 納入憑證值的 canary 掃描（中-1、低-6）。
- **[403 `model_not_allowed` 擋到人]** → 控制者點了不在 allow 的模型會被拒——這是政策守門的本意（例：免費額度外會計費的模型）；要開就改政策檔（本人），不是介面的事。
- **[選把移進 Gateway]** → 與 v5「Gateway 不挑把」的說法相反：輸入已是 provider＋model，「挑哪一把 subscription」無法留在呼叫方；規則是政策檔（priority＋冷卻）的機械套用，沒有新的裁量。釘住與 exclude 是呼叫方的明確輸入，不是 Gateway 的裁量。
- **[冷卻粒度是 subscription]**（中-7）→ 一把 subscription 上多個模型的額度是否獨立（例：Zen 免費層的限流單位）查無公開資料；冷卻以 subscription 為單位，可能「一個模型限流 → 整把冷卻 → 其他模型也被跳過」。本次維持保守的 subscription 粒度（未決 2），但 resolve 稽核記 model，保留未來分析／細粒度的資料；3b 只證明**初始選擇**（兩個模型、同一把），不證明「額度事件後還能換另一個免費模型」。
- **[釘住的那把額度用完]**（未決 1）→ 建議不自動換、回報失敗；在本人裁定前 sidecar 以此為安全預設。若裁定要自動換，sidecar 要另做「釘住失效 → 通知控制者 → 換把」的路徑，不能安靜換。
- **[稽核表 additive 遷移]**（中-2）→ 服務那台的既有 DB 是 v5 schema；`CREATE TABLE IF NOT EXISTS` 不會補欄位。啟動時以 `PRAGMA table_info` 檢查並 `ALTER TABLE audit ADD COLUMN provider TEXT`／`model TEXT`（nullable、additive）；舊 lgwd 的 SELECT 指定欄位，讀到多出的欄位不受影響，支援 `install.sh --tag` 退版。測試要從 v5 schema 的 DB 檔啟動並寫入。
- **[get 並存與歸檔時序]**（中-3）→ 本 change 不 REMOVED get；歸檔 gate 要求 `gateway-v0` 先歸檔；get 的移除在 d 單。`gateway-v0` 歸檔前，主規格 `agent-lease` 仍是平均分攤舊語意，優先序選把只在本 change 的 delta 裡。

## Migration Plan

1. **離線（不碰服務那台）**：本 repo 實作 resolve（決定 1–4）＋`lgw.client.resolve`＋稽核 additive 遷移＋policy loader 的 openrouter 檢查＋測試（loopback＋假 key＋假時鐘；spec 每個服務端 Scenario；canary：**憑證值**不出現在 log／資料庫／稽核／list 回覆；`env_schema` 拒收；`Cache-Control`；`model=a+b` 的編碼）。全在測試程序裡跑。
2. **sidecar（MyLinuxPool，c 單）**：決定 6 的需求；單元測試用假來源；Mac 上臨時跑（loopback＋假 key 的 lgwd＋sidecar）驗「兩個模型、同一把」的形狀，跑完即停。**排序排在 #216 階段 2.5a merge 之後（以 PMO 為準）**。
3. **實機（動服務那台與 worker；要本人授權，先問再動）**：
   - **3.0 部署新版 lgwd**（`install.sh --tag`，前後 `--check`；get 仍在，退版＝回舊 tag）。**部署順序：先 lgwd、後 worker 映像**（新-中-2；順序反了，新 sidecar 會先對舊 lgwd 打 resolve——有 6.2 的回退不致停擺，但 3b／3c 會在錯的組合下跑、驗的不是設計要驗的）。
   - **3a 建含新 sidecar 的 worker 映像並部署**（中-5）：映像可先建好（3.0 之前）；`sidecar --version` 對上 MyLinuxPool 的 c 單 commit；**部署在 3.0 之後**，通過條件＝lgwd 版本（`install.sh --check`）與 worker 映像的 sidecar 版本（`sidecar --version`）都對上。沒有這步，3b／3c 會拿舊 sidecar 驗新介面。
   - 3b worker 驗收（建議標準）：經 sidecar **新開 session**（省略 `session`，避開 Zen 免費層「只收原生新開 session」的上游限制；若 upstream 仍拒，改用別家模型重跑，這不是 resolve 的失敗）開一個**實作隊員**（`provider=opencode-zen`＋免費 A 模型，例：`mimo-v2.6-flash-free`）＋一個**審查隊員**（同 provider＋免費 B 模型，例：`big-pickle`；任何兩個 allow 涵蓋的不同模型皆可）——**兩個都拿到正確的模型**：status 的 model 各自正確、第一個 step 的模型＝所點（model-mismatch 通過）、稽核兩筆 resolve（同 subscription、不同 model）、log／status 無憑證值。
   - 3c 換把演練：**手動** `POST /v1/subscriptions/{id}/cooldown`（用 `meta.subscription`）模擬額度訊號（不假裝 agent 訊號）→ resolve 帶 `exclude=<剛耗盡的 id>` → 下一個候選 → 同一個 agora session continue；另一次讓 cooldown 登錄失敗（例：送過去時間或暫時停 lgwd），確認 `exclude` 仍換把、`no_candidate` 時 sidecar 停下回報。
   - 3d 釘住演練（建議）：以 `subscription=claude-company` 開一個隊員，確認 `meta.subscription` 是 company（不是 priority 較高的 personal）；額度用完的行為依未決 1（預設不自動換、回報）。
4. **退場**：另開 d 單移除 get（決定 5）；本 change 歸檔（tasks 5.4 的 gate）。

## Open Questions（未決，請本人裁定；每題附建議與理由）

1. **釘住的那把額度用完時，是失敗還是換另一把？** 建議：**失敗回報、不自動換**。理由：釘住是呼叫方明確的帳號選擇（公司／個人、隱私、計費、額度保留），自動換到另一把會違反意圖；回報讓控制者決定（改用別把、等冷卻、或改政策）。未釘住的路徑才照 priority 自動接力。替代：自動換，但 sidecar 必須通知控制者（不能安靜換），且要決定「換到哪一把」的規則。
2. **冷卻粒度要不要細到 `(subscription, model)`？** 建議：這次先維持 subscription 粒度的排除（保守；cooldown 的 id 是 subscription；Zen 限流單位查無公開資料），但 resolve 稽核已記 model，保留未來分析資料；下一單再決定是否只排除該 model。限制要明示：同一把的另一個免費模型也會被冷卻；3b 只證明初始選擇，不證明額度事件後還能換模型。替代：現在就做細粒度，需要 cooldown 帶 model、DB 改 `(subscription, model)`、sidecar 的訊號也要帶 model——介面與儲存都變大，且上游是否按模型限流未證實。
3. **未釘住時，`anthropic` 兩把的預設選法**：建議維持 priority（personal 10 先、company 20 後；company 只在 personal 冷卻或未部署時用），因為釘住已提供「這次用公司／個人」的明確入口；平均分攤需要用量資料，v5 已移除。替代：恢復平均分攤或加「角色／team → 哪一把」的政策規則；前者要用量，後者要 resolve 有身分（會擴介面）。
4. **get 的退場時點**：建議 sidecar 遷移＋實機驗收（3b／3c／3d）過後就開 d 單移除，不要拖（並存介面越多，「必要且只有這些」破得越久）。本 change 不 REMOVED get，d 單的 delta 以那時的主規格為底；d 單開單條件是 get 呼叫為零。
5. **meta「到期」的語意**：本設計解讀為**冷卻的 `until`**（409 `cooling` 帶、list 帶、resolve 的 meta 不再帶 `cooldown`／`available`）。若本人指的是別的到期（例：key 的到期），Gateway 目前沒有那資料（key-sync 只換檔、不記到期），要另做——請確認意圖。
6. **resolve 是否也收「不指定 provider」**（Gateway 連 provider 都代挑）：建議不要——issue 的輸入就是 provider＋model；不指定時由 sidecar 從 list 的 priority 挑好再 resolve，Gateway 的裁量不擴大；釘住與 exclude 已經把「呼叫方明確指定」的路徑留好了。
7. **`schema` 與 `env_schema` 要不要合併成一個版本號？**（新-低-4）建議**維持兩欄**。理由：兩者演進的原因不同（meta 形狀 vs env 鍵集）；分開的版本讓 sidecar 知道「哪一側變了」——env 鍵集變才需要重驗 env 處理，meta 的 additive 變更（例：加一個欄位）未來可放寬成「讀已知欄位、忽略新欄位」而不動 env 的嚴格護欄（決定 1 的場景）。合併的優點：少一個欄位、少一條測試線、任何變更都逼 sidecar 全量重驗（保守）；缺點：meta 的小變更會造成不必要的整體回覆拒收。成本已付，本輪維持兩欄；合併屬客戶端相容政策的重定，請本人裁定。
