## Purpose

LLMGateway 對外提供三個介面（終態）：list（列菜單，不含 key）、resolve（輸入 agent＋provider＋選填 model／subscription／exclude，輸出選中的 subscription 的 meta 與必要的環境變數）、登錄冷卻（只收何時能再用 `until`）。呼叫方（sidecar）以 provider＋model 點菜，也可以用 `subscription` 釘住某一把、用 `exclude` 排除剛耗盡的那一把；換哪一把 subscription 由 resolve 依政策挑；`models.allow` 是模型守門。過渡期另有 get（舊 sidecar 使用），由 d 單移除；本 change 不 `REMOVED` get。

## ADDED Requirements

### Requirement: resolve

`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]]` SHALL 回 `{schema, env_schema, meta, env}`。

`agent` 與 `provider` 必填；缺少時回 400 `bad_request`（`missing` 列出缺的欄位）。`agent` MUST 是 `claude-code` 或 `opencode`，否則 400 `bad_request`（不是 403 `agent_not_allowed`）。`model` 選填，是**供應商自己的模型 id**（可含 `/`、`+`、`:` 等字元）。`subscription` 選填，是釘住某一把 subscription：有給時 MUST 與該把的 `provider` 相同，不一致回 400 `bad_request`；未知 id 回 404 `not_found`。`exclude` 選填，是逗號分隔的 subscription id 清單；不在政策檔的 id SHALL 忽略。`subscription` 與 `exclude` 同時給 MUST 回 400 `bad_request`。未知 query 參數 SHALL 忽略；空值視為沒給（`subscription=x&exclude=` 因此是合法釘住），同一參數重複出現回 400 `bad_request`。所有 query 值由呼叫端 percent-encode（`+` 編成 `%2B`），server 以 form 解碼。

`meta` SHALL 含 `subscription`（選中的 subscription id）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`）、`endpoint`、`priority`。`meta` MUST NOT 含 `cooldown`／`available`（200 已代表可用；這兩個在 resolve 恆為 null／true，不帶資訊）。

`env` SHALL 是把選定的 provider＋model 用起來的必要環境變數（名稱→值），依 `agent` 組成（見「env 的組成」）。`env` 的**憑證值** MUST NOT 出現在 list 的回覆、稽核紀錄、log 或 `GET /agents` status；model、provider、endpoint 是中介資料，稽核可記——前提是 **`endpoint` MUST NOT 內嵌憑證**（query 的 `key=`／`token=`／`api_key=`／`access_token=`、userinfo 的 `@`；providers 載入／啟動時拒絕，見「共通 JSON schema」）。錯誤回覆 MUST NOT 含 `env`。resolve 的 200 回覆 SHALL 帶 `Cache-Control: no-store` 與 `Pragma: no-cache`（過渡期的 get 亦同）。

`schema` SHALL 是**該端點回覆形狀的版本**（resolve 與 list 各自獨立；目前皆為 1），resolve 形狀改變時 MUST bump。`env_schema` SHALL 是 **env 組成（鍵集與鍵的語意）的版本**（目前為 1）；新增／移除 env 鍵或改變鍵的語意時 MUST bump；Gateway MUST NOT 在同一個 `env_schema` 內改變鍵集（個別回應因 model 為 null 而少給模型鍵，不算鍵集變更）。sidecar MUST 對不認得的更高 `schema`／`env_schema` 拒收（較低版本若仍支援則接受）。

#### Scenario: 同一把的兩個免費模型
- **WHEN** 分別以 `agent=opencode&provider=opencode-zen&model=mimo-v2.6-flash-free` 與 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 兩次都回 200，`meta.subscription` 都是 `opencode-zen`，`meta.model` 各自是請求的模型，兩次的 `env` 不同（模型設定不同）

#### Scenario: 釘住公司那一把
- **WHEN** 以 `agent=claude-code&provider=anthropic&subscription=claude-company` resolve，而 `claude-personal`（priority 10）未冷卻、已部署
- **THEN** 回 200，`meta.subscription` 是 `claude-company`（不是 priority 較高的 `claude-personal`）

#### Scenario: 釘住的那把冷卻中
- **WHEN** 以 `subscription=claude-personal` resolve，而 `claude-personal` 在冷卻中
- **THEN** 回 409 `cooling`，body 帶 `until`，不含 `env`，且不代換 `claude-company`

#### Scenario: 釘住的 subscription 與 provider 不一致
- **WHEN** 以 `provider=anthropic&subscription=opencode-zen` resolve
- **THEN** 回 400 `bad_request`，不含 `env`

#### Scenario: 釘住與排除同時給
- **WHEN** 以 `provider=anthropic&subscription=claude-company&exclude=claude-personal` resolve
- **THEN** 回 400 `bad_request`，不含 `env`

#### Scenario: 換把時排除剛耗盡的那一把
- **WHEN** `claude-personal` 剛耗盡、cooldown 登錄失敗（未進入冷卻），以 `agent=claude-code&provider=anthropic&exclude=claude-personal` resolve
- **THEN** 回 200，`meta.subscription` 是 `claude-company`（不會選回 `claude-personal`）

#### Scenario: 排除後沒有候選
- **WHEN** `agent=claude-code&provider=anthropic&exclude=claude-personal` resolve，而 `claude-company` 不在政策檔
- **THEN** 回 409 `no_candidate`，不含 `env`，不回 `cooling`（沒有恢復時刻）

#### Scenario: 缺必填
- **WHEN** resolve 缺 `agent` 或 `provider`
- **THEN** 回 400 `bad_request`，`missing` 列出缺的欄位，回覆不含 `env`

#### Scenario: 未知的 agent
- **WHEN** 以 `agent=foo&provider=opencode-zen` resolve
- **THEN** 回 400 `bad_request`（不是 403 `agent_not_allowed`）

#### Scenario: meta 只帶有資訊的欄位
- **WHEN** resolve 成功
- **THEN** `meta` 含 `subscription`、`provider`、`kind`、`agent`、`model`、`models`、`endpoint`、`priority`，不含 `cooldown`／`available`

#### Scenario: 值不外洩與不快取
- **WHEN** 以憑證樣式掃描稽核紀錄、log 與 list 的回覆（resolve 的 200 本來就含憑證值，只在 SSH 通道內），並檢查 resolve 的 200 標頭
- **THEN** 沒有 env 任何**憑證值**的命中；回覆帶 `Cache-Control: no-store` 與 `Pragma: no-cache`

#### Scenario: env 組成版本升高
- **WHEN** Gateway 回覆的 `env_schema` 高於 sidecar 認得的版本
- **THEN** sidecar 拒收該回覆（不把不認得的鍵集交給子行程）

### Requirement: resolve 的選把規則

resolve SHALL 依序挑 subscription，並依斷在哪一步回錯誤：

1. 有 `subscription`（釘住）時，候選**先限縮為這一把**，後續每一步只在這一把上判斷，不代換另一把；此步不套 `exclude`（同時給已在參數驗證回 400 `bad_request`）。
2. `provider` 不存在，或該 provider 沒有任何 subscription → 404 `not_found`。
3. 候選＝`provider` 相同、`agents` 含請求的 agent；無一允許 → 403 `agent_not_allowed`。
4. `model` 有給時，候選限於被 `models.allow` 的樣式（fnmatch）涵蓋的；無一涵蓋 → 403 `model_not_allowed`。`model` 省略時，候選限於**能推導模型**的（kind 為 `claude-subscription`，或 `models.default` 非 null，或 `allow` 至少一個不含萬用字元的樣式）；無一能推導 → 400 `no_model`（釘住時就是該把——同 provider 另一把可推導也不代換）。`claude-subscription`＋default null 算能推導（用 agent 預設、合法 200），所以 `no_model` 只落在非 `claude-subscription` 且 default null、`allow` 全萬用字元的形狀（現行政策沒有這種 provider；單元測試用假政策）。
5. 沒有釘住時，套 `exclude`（不在政策檔的 id 忽略）；沒有候選 → 409 `no_candidate`。
6. 排除 key 未部署的候選；全部未部署 → 409 `not_deployed`。
7. 排除冷卻中的（`cooldown` 有未來的 `until`）；全部冷卻 → 409 `cooling`（body 帶 `until`＝最早的恢復時刻）。
8. 挑 `priority` 最小，同分以 id 排序。

錯誤訊息 MUST NOT 回顯請求裡的值。`subscription` 與 `provider` 不一致的檢查只在該 id 存在時成立（id 未知回 404）。多個錯誤同時成立時 SHALL 回優先序最前者：輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403 `agent_not_allowed`／`model_not_allowed`，與 400 `no_model`）先於候選狀態（409 `no_candidate`／`not_deployed`／`cooling`）——與上面的步驟順序同一軸。釘住的那把冷卻中 MUST 回 409 `cooling`＋`until`，不代換另一把；`exclude` 後沒有候選 MUST 回 409 `no_candidate`，不選回被排除的那把、也不回 `cooling`。

#### Scenario: 同 provider 兩把，一把冷卻中
- **WHEN** `claude-personal`（priority 10）在冷卻中，以 `agent=claude-code&provider=anthropic` resolve（未釘住）
- **THEN** 選中 `claude-company`（priority 20），200

#### Scenario: 全部候選冷卻中
- **WHEN** provider＋agent 的所有候選都在冷卻中
- **THEN** 回 409 `cooling`，body 的 `until` 是最早的恢復時刻，不含 `env`

#### Scenario: model 不在 allow
- **WHEN** 以 `agent=opencode&provider=opencode-zen` resolve 一個不被 `*-free`／`big-pickle` 涵蓋的 model
- **THEN** 回 403 `model_not_allowed`，不含 `env`，不代選其他模型

#### Scenario: 帶 provider 前綴的 model
- **WHEN** 以 `agent=opencode&provider=opencode-zen&model=opencode/big-pickle` resolve（`model` 不是供應商 id）
- **THEN** 回 403 `model_not_allowed`（Gateway 不正規化前綴；sidecar 要在呼叫前正規化或回 400）

#### Scenario: 未知 provider
- **WHEN** 以 `provider=NoSuchProvider` resolve
- **THEN** 回 404 `not_found`

#### Scenario: 最高 priority 的候選推不出模型
- **WHEN** `model` 省略，最高 priority 的候選 `default` 為 null 且 `allow` 全是萬用字元，下一個候選有 `default`
- **THEN** 選中下一個能推導模型的候選（不是直接 400 `no_model`）

#### Scenario: 釘住那把推不出模型，同 provider 另一把可推導
- **WHEN** 用單元測試的假政策（現行政策下不可達；`claude-subscription`＋default null 是合法 200、不算推不出）：一個非 `claude-subscription` 的 provider 有兩把 api-key subscription，`model` 省略、以 `provider=<該 provider>&subscription=<第一把>` resolve（釘住），而該把的 `default` 為 null 且 `allow` 全是萬用字元樣式（推不出模型），但同 provider 的另一把能推導模型
- **THEN** 回 400 `no_model`，不代換另一把（釘住在第 1 步先限縮候選，第 4 步的「另一把可推導」不觸發）

### Requirement: env 的組成

`env` SHALL 依（provider、kind、agent）組成，只含憑證、端點、模型命名（含 opencode 的 provider id）三類變數；MUST NOT 含呼叫方的維運變數（auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數、`permission`、`instructions`——那些由 sidecar 疊）。鍵集＝下列清單（`env` MUST 只含這些鍵；每鍵的型別／空值／出現條件；sidecar 對不在清單上的鍵見「未知鍵」）：

- `CLAUDE_CODE_OAUTH_TOKEN`（claude-code＋claude-subscription）：字串＝該把的 key（OAuth token）；不可空；該組合必然出現。
- `ANTHROPIC_API_KEY`（claude-code＋api-key）：provider 為 anthropic 時字串＝該把的 key、不可空（**現行不可達**：anthropic 恆為 claude-subscription，預留形狀）；其他 provider 時＝**空字串**（刻意佔位，蓋掉子行程環境可能殘留的繼承值）；claude-subscription 時不出現。
- `ANTHROPIC_AUTH_TOKEN`（claude-code＋api-key、provider 非 anthropic）：字串＝該把的 key；不可空。
- `ANTHROPIC_BASE_URL`（claude-code＋api-key、provider 非 anthropic）：字串＝該把的 `endpoint`（URL）；僅 `endpoint` 非 null 時出現。
- `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`（claude-code 全部組合）：字串＝選定的 model（供應商 model id；兩鍵同值）；不可空；僅 model 非 null 時出現。
- `OPENCODE_API_KEY`（opencode、各 provider）：字串＝該把的 key；不可空；必然出現。
- `OPENCODE_PROVIDER_ID`（opencode、各 provider）：字串＝opencode 的 provider id（`opencode-zen`→`opencode`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud`，由 LLMGateway 維護；opencode config 內的模型名＝`<OPENCODE_PROVIDER_ID>/<OPENCODE_MODEL>`，由 sidecar 組）；不可空；必然出現。
- `OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`（opencode、各 provider）：字串＝選定的 model（供應商 model id；兩鍵同值）；不可空；僅 model 非 null 時出現。

opencode 只回**片段**（上述 `OPENCODE_*` 四鍵）：Gateway MUST NOT 回 `OPENCODE_CONFIG_CONTENT`，也不回 `baseURL`（`endpoint` 是 provider 根位址，依 #28 不抄進 opencode 的 `options.baseURL`）。model 為 null（agent 自己的預設）時 SHALL 不給模型變數。

**未知鍵（消費端規則；PM 第二輪對齊）**：`env` 出現不在上述清單的鍵＝Gateway 在同一個 `env_schema` 內違規改鍵集（sidecar 對更高 `env_schema` 拒收是第一道）。sidecar 對未知鍵 MUST 忽略並 WARN（log 只記**鍵名**、不記值），MUST NOT 原樣塞進 `OPENCODE_CONFIG_CONTENT` 或子行程環境；清單上的鍵照常併入。`permission`／`instructions` 由 sidecar 擁有（`env` 不含這兩個鍵），同名鍵 sidecar 贏。

opencode 的 `OPENCODE_CONFIG_CONTENT` SHALL 由 sidecar 用上述片段＋自己的 `permission`／`instructions`＋worker 釘的 opencode 版本組成。sidecar 的子行程環境以現行**完整 base env**（PATH／HOME／Locale 等）＋維運變數為底，resolve 的 `env` 是**不篩選的 overlay**（清單上的鍵全數併入、同名覆蓋 base；未知鍵依「未知鍵」規則丟棄）；sidecar 自己行程**繼承來的** `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` MUST NOT 原樣漏進子行程（sidecar 要給子行程的維運變數，例 `OPENCODE_SERVER_PASSWORD`，是另外疊的，不在此限）。

#### Scenario: claude 訂閱不帶模型變數
- **WHEN** resolve 選中 `claude-personal`（claude-subscription）而 `meta.model` 為 null
- **THEN** `env` 只有 `CLAUDE_CODE_OAUTH_TOKEN`，沒有 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`

#### Scenario: opencode 的兩個模型（片段）
- **WHEN** 同一把 `opencode-zen` 先後 resolve `mimo-v2.6-flash-free` 與 `big-pickle`
- **THEN** 兩次 `env` 的 `OPENCODE_PROVIDER_ID` 都是 `opencode`、`OPENCODE_API_KEY` 相同，`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 各自是該模型；`env` 不含 `OPENCODE_CONFIG_CONTENT`

#### Scenario: sidecar 組 opencode config
- **WHEN** sidecar 收到 `OPENCODE_PROVIDER_ID=opencode`、`OPENCODE_MODEL=big-pickle`、`OPENCODE_API_KEY=…`
- **THEN** sidecar 組出的 `OPENCODE_CONFIG_CONTENT` 有 `provider.opencode.options.apiKey`、`provider.opencode.models.big-pickle.name`、`model`／`small_model`＝`opencode/big-pickle`，並保留自己的 `permission`／`instructions`

#### Scenario: 只注入這一把的憑證
- **WHEN** sidecar 行程自己的環境有 `ANTHROPIC_API_KEY=canary`，子行程以 resolve 的 env 啟動
- **THEN** 子行程只看到 resolve 給的憑證值（或沒有），看不到 sidecar 自己的 `canary`

#### Scenario: env 出現未知鍵
- **WHEN** resolve 的 `env` 含一個不在鍵集清單上的鍵（例：Gateway 在同一個 `env_schema` 內違規加了新鍵）
- **THEN** sidecar 忽略該鍵並 WARN（log 記鍵名、不記值），不把它塞進 `OPENCODE_CONFIG_CONTENT` 或子行程環境；清單上的鍵照常併入

### Requirement: model 省略時的語意

`model` 省略時 SHALL 用選中那把的 `models.default`；default 為 null 時：kind 為 `claude-subscription` → 用 agent 自己的預設（`meta.model` 為 null、env 不帶模型變數）；其他 kind → `models.allow` 第一個不含萬用字元的項；兩者都沒有 → 400 `no_model`（不代選）。候選在選把時已限於能推導模型的（見「resolve 的選把規則」第 4 步；釘住時第 1 步先限縮成那把、推不出直接 400 `no_model`），所以不會出現「選中後才發現推不出、但另一個候選可以」。

#### Scenario: claude-subscription 的預設
- **WHEN** 以 `agent=claude-code&provider=anthropic&subscription=claude-personal` resolve（model 省略）而該把的 default 為 null
- **THEN** 200，`meta.model` 為 null，env 不帶模型變數

#### Scenario: api-key 的預設
- **WHEN** 以 `agent=opencode&provider=opencode-zen` resolve（model 省略）
- **THEN** `meta.model` 為 `mimo-v2.6-flash-free`（該把的 default）

#### Scenario: 推不出模型
- **WHEN** model 省略、釘住那把的 default 為 null、`models.allow` 全是萬用字元樣式，且該把的 kind 不是 `claude-subscription`（api-key 形狀；現行政策沒有這種 provider，用假政策測）
- **THEN** 回 400 `no_model`，不代選

## MODIFIED Requirements

### Requirement: 稽核紀錄

每次**成功**（200）的 resolve、每次成功的 get（過渡期）與每次登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類（resolve／get）、provider 與 model（resolve）、結束時刻 `until`（登錄冷卻）。紀錄 MUST NOT 含憑證的值，也 MUST NOT 含 `env` 的憑證值；provider／model 是中介資料，SHALL 記（否則「誰用了哪個模型」就沒有紀錄）。失敗的回覆（4xx／409）不記稽核（沒有選中的 subscription 可記，與現行 get 同），診斷靠只含方法／路徑／狀態碼的 HTTP log。不做認證，紀錄不分是哪個客戶端。

#### Scenario: resolve 留下紀錄
- **WHEN** 有客戶端以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 稽核紀錄有一筆（subscription、agent、provider、model），內容沒有憑證值或 env 的憑證值

#### Scenario: 舊 DB 沒有新欄位
- **WHEN** 以 v5 schema 的既有 DB 檔啟動新 lgwd，並寫入一筆 resolve
- **THEN** 啟動時補上 `provider`／`model` 欄位，寫入成功；舊資料的這兩個欄位為 null

### Requirement: 錯誤回覆

所有錯誤 SHALL 以 `{"error": "<碼>"}` 的 JSON 回覆，訊息不得回顯請求裡的值；resolve 的錯誤回覆 MUST NOT 含 `env`。碼在 v5 的表上新增／明確化：
- 400 `bad_request`：缺 `agent`／`provider`；`agent` 不是 `claude-code`／`opencode`；`subscription` 與 `provider` 不一致；`subscription` 與 `exclude` 同時給；
- 400 `no_model`：`model` 省略且沒有候選能推導模型（不代選）；
- 403 `model_not_allowed`：model 不被任何候選的 `models.allow` 涵蓋；
- 404 `not_found`：`subscription` 指定的 id 不存在；
- 409 `cooling`：候選全部冷卻中（body 帶 `until`＝最早恢復時刻；釘住時就是該把）；
- 409 `no_candidate`：`exclude` 排除後沒有候選（沒有恢復時刻）。

多個錯誤同時成立時 SHALL 回優先序最前者：輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403，與 400 `no_model`）先於候選狀態（409）；與「resolve 的選把規則」的步驟同一軸。

#### Scenario: 候選全部冷卻中
- **WHEN** resolve 的所有候選都在冷卻中
- **THEN** 回 409 `cooling`，body 的 `until` 是最早的恢復時刻

#### Scenario: 模型被政策擋下
- **WHEN** resolve 的 model 不在候選的 `models.allow`
- **THEN** 回 403 `model_not_allowed`，不含 `env`，不代選其他模型

#### Scenario: 排除後沒有候選
- **WHEN** resolve 的 `exclude` 排除了最後一個可用候選
- **THEN** 回 409 `no_candidate`，不含 `env`

#### Scenario: 未知 agent 是輸入錯誤
- **WHEN** resolve 的 `agent` 不是 `claude-code`／`opencode`
- **THEN** 回 400 `bad_request`

#### Scenario: 輸入形狀錯誤先於查無
- **WHEN** 以 `provider=NoSuchProvider&subscription=claude-personal` resolve（`claude-personal` 存在、其 provider 是 `anthropic`，與請求的 `provider` 不一致；`NoSuchProvider` 也不是政策裡的 provider）
- **THEN** 回 400 `bad_request`（`subscription` 與 `provider` 不一致先於 provider 不存在的 404）

### Requirement: 共通 JSON schema

list 的 row 形狀不變（v5 的欄位：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models`、`endpoint`、`cooldown`、`available`；`key` 只在 get，過渡期）。這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 provider＋model 用起來的環境變數由 resolve 的回覆 `env` 給（「env 的組成」），list 只是菜單。Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（沿 v5 的理由：條款；免費模型只經過 opencode）；政策載入 MUST 拒絕 openrouter 的 `agents` 不是剛好 `["opencode"]` 的政策檔（目前 loader 只驗 `models.allow`，本 change 補上）。`endpoint` MUST NOT 內嵌憑證：`endpoint` 不在政策檔，是 `lgwd/providers.py` 的 `CredentialProvider.endpoint` 常數；providers 載入／lgwd 啟動時 MUST 逐筆檢查 `PROVIDERS` 的 endpoint 值，含 `key=`／`token=`／`api_key=`／`access_token=` query 樣式或 userinfo `@` 即拒絕啟動（`PolicyError`；endpoint 是中介資料、會進稽核與 meta——前提是不含憑證）。`schema` 是該端點回覆形狀的版本；list row 的 `schema` 與 resolve 的 `schema` 各自獨立。

#### Scenario: list 不變
- **WHEN** 呼叫 list
- **THEN** row 形狀與 v5 相同：不含 key、不含任何環境變數名

#### Scenario: 政策檔把 openrouter 的 agents 改寬
- **WHEN** 政策檔的 `openrouter.agents` 不是剛好 `["opencode"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動；resolve 不會把 Claude Code 指到 OpenRouter

#### Scenario: provider 表的 endpoint 內嵌憑證
- **WHEN** 假 provider 表（monkeypatch `PROVIDERS`）某筆 `CredentialProvider.endpoint` 值含 `key=`／`token=`／`api_key=`／`access_token=` 的 query 或 userinfo（`user:pass@`）樣式
- **THEN** providers 載入／lgwd 啟動失敗（`PolicyError`），lgwd 不啟動（endpoint 會進稽核與 meta——前提是不含憑證）

### Requirement: models.default 的語意

`models.default`（與 resolve 的 `model` 參數）SHALL 是**供應商自己的模型 id**（例 `mimo-v2.6-flash-free`、`openrouter/free`——可含 `/`），不帶 agent 專用的 provider 前綴；組成 agent 專用的字串（例 opencode 的 `<id>/<model>`）由 sidecar 在 `OPENCODE_CONFIG_CONTENT` 內組，`meta.model` 一律不帶前綴。resolve 的 `model` 參數不做前綴正規化：帶 `<provider>/<model>` 的值會照 `models.allow` 比對（通常 403 `model_not_allowed`）；sidecar 的 `POST /agents` 若收到前綴，要自己正規化或回 400。null 的語意依 `kind`（見「model 省略時的語意」）：`claude-subscription` 表示用 agent 自己的預設；其他 kind 表示沒有預設、服務不代選。

#### Scenario: 供應商自己的模型 id
- **WHEN** 讀取 resolve 成功的 `meta`
- **THEN** `meta.model` 是供應商自己的模型 id，不帶 `opencode/` 等前綴；前綴只出現在 sidecar 組的 config 內容裡

#### Scenario: 帶前綴的 model 不自動正規化
- **WHEN** 以 `provider=opencode-zen&model=opencode/big-pickle` resolve
- **THEN** 回 403 `model_not_allowed`（不是自動去掉 `opencode/` 後成功）
