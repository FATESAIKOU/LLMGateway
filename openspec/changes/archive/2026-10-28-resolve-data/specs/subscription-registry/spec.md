## MODIFIED Requirements

### Requirement: resolve

`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]][&data=<public|general|real>]` SHALL 回 `{schema, env_schema, meta, env}`。

`agent` 與 `provider` 必填；缺少時回 400 `bad_request`（`missing` 列出缺的欄位）。`agent` MUST 是 `claude-code` 或 `opencode`，否則 400 `bad_request`（不是 403 `agent_not_allowed`）。`model` 選填，是**供應商自己的模型 id**（可含 `/`、`+`、`:` 等字元）。`subscription` 選填，是釘住某一把 subscription：有給時 MUST 與該把的 `provider` 相同，不一致回 400 `bad_request`；未知 id 回 404 `not_found`。`exclude` 選填，是逗號分隔的 subscription id 清單；不在政策檔的 id SHALL 忽略。`subscription` 與 `exclude` 同時給 MUST 回 400 `bad_request`。`data` 選填，是呼叫方**宣告的資料等級**：有給時 MUST 是 `public`、`general`、`real` 之一，否則 400 `bad_request`（`data` 不是資源，不是 404；等級夠不夠是政策拒絕的 403，見「resolve 的選把規則」）。**沒給 `data` 時 Gateway MUST NOT 依 `privacy` 過濾**（缺省不濾：不帶＝不擋，與這個參數加入前相同；宣告了才一定不降級，翻成缺省擋留給後續的 d）。未知 query 參數 SHALL 忽略；空值視為沒給（`subscription=x&exclude=` 因此是合法釘住），同一參數重複出現回 400 `bad_request`。所有 query 值由呼叫端 percent-encode（`+` 編成 `%2B`），server 以 form 解碼。

`meta` SHALL 含 `subscription`（選中的 subscription id）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`）、`endpoint`、`priority`、`data`（請求的資料等級**原樣**，沒給時 null）、`cooldown`（該把整把的冷卻：`{"until": RFC 3339 UTC}` 或 null，過了 `until` 即視為 null）、`model_cooldowns`（該把各模型的冷卻：物件，模型 id → `{"until": RFC 3339 UTC}`，只列還沒過的，沒有時 `{}`；形狀與 list 的同一欄位相同）。`meta.data` 是「這次宣告了什麼」，**不是生效等級**：resolve 沒有 role／floor，所以它不會被抬高，與 `roles` 回覆的 `data`（請求值與 floor 取較嚴）語意不同。`meta` 另含 `usage`（與 list 同形狀；只讀快取，快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 resolve MUST 仍照常回 200，`env` 與選把結果不受影響）。冷卻資訊是**提示**：resolve 的 200 不代表「沒有冷卻」；呼叫方要判斷「這次的呼叫會不會撞到冷卻」時，看 `meta.cooldown.until` 與（`meta.model` 非 null 時）`meta.model_cooldowns[meta.model].until`，兩者取較晚者。`meta` MUST NOT 含 `available`／`available_again_at`（200 已代表 key 已部署，`available` 恆為 true、不帶資訊；用量窗口的時刻在 `usage`）。resolve 回非 200 時不組裝 `usage`，也不觸發取用量。

`env` SHALL 是把選定的 provider＋model 用起來的必要環境變數（名稱→值），依 `agent` 組成（見「env 的組成」）。`env` 的**憑證值** MUST NOT 出現在 list 的回覆、稽核紀錄、log 或 `GET /agents` status；model、provider、endpoint 是中介資料，稽核可記——前提是 **`endpoint` MUST NOT 內嵌憑證**（query 的 `key=`／`token=`／`api_key=`／`access_token=`、userinfo 的 `@`；providers 載入／啟動時拒絕，見「共通 JSON schema」）。錯誤回覆 MUST NOT 含 `env`。resolve 的 200 回覆 SHALL 帶 `Cache-Control: no-store` 與 `Pragma: no-cache`（過渡期的 get 亦同）。

`schema` SHALL 是**該端點回覆形狀的版本**（resolve 與 list 各自獨立；目前皆為 1），resolve 形狀有**不相容**變更（移除／改名／改語意）時 MUST bump；只**新增** meta 欄位（例如 `usage`、`cooldown`、`model_cooldowns`、`data`）不 bump（消費端忽略不認得的欄位）。`env_schema` SHALL 是 **env 組成（鍵集與鍵的語意）的版本**（目前為 1）；新增／移除 env 鍵或改變鍵的語意時 MUST bump；Gateway MUST NOT 在同一個 `env_schema` 內改變鍵集（個別回應因 model 為 null 而少給模型鍵，不算鍵集變更）。消費端對不認得的更高 `schema`／`env_schema` 的處理（拒收等）見 `archive/2026-10-24-agent-provider-model-interface/design.md` 決定 6，由 sidecar 側規格承接。

#### Scenario: 同一把的兩個免費模型
- **WHEN** 分別以 `agent=opencode&provider=opencode-zen&model=mimo-v2.6-flash-free` 與 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 兩次都回 200，`meta.subscription` 都是 `opencode-zen`，`meta.model` 各自是請求的模型，兩次的 `env` 不同（模型設定不同）

#### Scenario: 釘住公司那一把
- **WHEN** 以 `agent=claude-code&provider=anthropic&subscription=claude-company` resolve，而 `claude-personal`（priority 10）未冷卻、已部署
- **THEN** 回 200，`meta.subscription` 是 `claude-company`（不是 priority 較高的 `claude-personal`）

#### Scenario: 釘住的那把冷卻中
- **WHEN** 以 `subscription=claude-personal` resolve，而 `claude-personal` 在冷卻中
- **THEN** 回 200，`meta.subscription` 是 `claude-personal`，`meta.cooldown.until` 是該冷卻時刻，`env` 照常

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
- **THEN** 回 409 `no_candidate`，不含 `env`

#### Scenario: 缺必填
- **WHEN** resolve 缺 `agent` 或 `provider`
- **THEN** 回 400 `bad_request`，`missing` 列出缺的欄位，回覆不含 `env`

#### Scenario: 未知的 agent
- **WHEN** 以 `agent=foo&provider=opencode-zen` resolve
- **THEN** 回 400 `bad_request`（不是 403 `agent_not_allowed`）

#### Scenario: meta 只帶有資訊的欄位
- **WHEN** resolve 成功
- **THEN** `meta` 含 `subscription`、`provider`、`kind`、`agent`、`model`、`models`、`endpoint`、`priority`、`data`、`usage`、`cooldown`、`model_cooldowns`，不含 `available`／`available_again_at`

#### Scenario: 值不外洩與不快取
- **WHEN** 以憑證樣式掃描稽核紀錄、log 與 list 的回覆（resolve 的 200 本來就含憑證值，只在 SSH 通道內），並檢查 resolve 的 200 標頭
- **THEN** 沒有 env 任何**憑證值**的命中（含 `usage` 與取用量的 log）；回覆帶 `Cache-Control: no-store` 與 `Pragma: no-cache`

#### Scenario: meta 帶 usage
- **WHEN** resolve 成功選中 `ollama-cloud`，快取裡有未過期的用量
- **THEN** `meta.usage` 與 list 同一筆的 `usage` 相同；`env`、`meta.subscription` 與沒有用量功能時一致

#### Scenario: 選中沒有用量來源的 provider
- **WHEN** resolve 成功選中 `opencode-zen`
- **THEN** `meta.usage` 是 `status: not_applicable`

#### Scenario: 用量壞了 resolve 照回
- **WHEN** 取用量實作拋例外或卡住，resolve 一個合法請求
- **THEN** 仍回 200，`env` 與沒有用量功能時相同，`meta.usage` 為 `unknown`

#### Scenario: 用量為 0% 不改選把
- **WHEN** 被選中的候選 `remaining_percent` 為 0、沒有冷卻
- **THEN** 選把規則照舊（仍回 200 選它），`meta.usage` 如實顯示 0

#### Scenario: 失敗的 resolve 不帶 usage
- **WHEN** resolve 回 409 `no_candidate`
- **THEN** body 只有 `error`，沒有 `usage`、沒有 `env`

#### Scenario: 冷卻時仍回 key 並附冷卻資訊
- **WHEN** `opencode-zen` 整把冷卻到 15:00，以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 回 200；`env` 有 `OPENCODE_MODEL`（值為 `big-pickle`）；`meta.cooldown` 是 `{"until": "…15:00…"}`；`meta.model_cooldowns` 如實列出該把的模型冷卻

#### Scenario: data 不在三個等級
- **WHEN** 以 `agent=opencode&provider=opencode-zen&data=private` resolve
- **THEN** 回 400 `bad_request`（不是 404、不是 403 `privacy_not_allowed`），不含 `env`

#### Scenario: 缺省不帶 data 不濾
- **WHEN** 以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve（不帶 `data`），而 `opencode-zen` 的 `privacy` 是 `public`
- **THEN** 回 200、`meta.subscription` 是 `opencode-zen`、`meta.data` 是 null——不帶 `data` 不會依 `privacy` 擋（與這個參數加入前的行為相同）

#### Scenario: meta 帶宣告的等級
- **WHEN** 分別以 `data=public`、`data=general`、`data=real` resolve 同一把足以勝任的訂閱
- **THEN** 三次的 `meta.data` 各自是請求值原樣；沒帶 `data` 時是 null（不是「生效等級」，resolve 沒有 role／floor）

#### Scenario: data 空值視為沒給
- **WHEN** 以 `agent=opencode&provider=opencode-zen&data=` resolve（空白值）
- **THEN** 回 200，`meta.data` 是 null；空白不算宣告、不觸發隱私過濾

#### Scenario: data 重複參數 400
- **WHEN** 同一個 query 出現兩次 `data`（含其中一次是空白，例如 `data=&data=public`）
- **THEN** 回 400 `bad_request`（與其他已知參數的重複規則相同），不含 `env`

#### Scenario: roles 回覆的候選用回覆的 data resolve 不被隱私擋
- **WHEN** 對任一個角色與 agent 取 `GET /v1/roles/{role}?agent=<agent>[&data=…]`，逐個 candidate 以**回覆的 `data`**＋`subscription=<該筆 id>`＋`provider=<該筆 provider>` resolve（`roles` 已把 `data` 與 `floor` 取較嚴）
- **THEN** 每一筆都不得回 403 `privacy_not_allowed`（`resolve` 不會比 `roles` 更寬）；key 已部署且 model 可推導時回 200

### Requirement: resolve 的選把規則

resolve SHALL 依序挑 subscription，並依斷在哪一步回錯誤：

1. 有 `subscription`（釘住）時，候選**先限縮為這一把**，後續每一步只在這一把上判斷，不代換另一把；此步不套 `exclude`（同時給已在參數驗證回 400 `bad_request`）。
2. `provider` 不存在，或該 provider 沒有任何 subscription → 404 `not_found`。
3. 候選＝`provider` 相同、`agents` 含請求的 agent；無一允許 → 403 `agent_not_allowed`。
3.5 有給 `data` 時，候選限於 `privacy` **不低於** `data` 的（`public` < `general` < `real`）；一個都不剩 → 403 `privacy_not_allowed`。沒給 `data` 時此步不作用（完全不過濾）。此步排在 3 與 4 之間（編號記 3.5，4～7 不變，與程式註解同一套編號）。
4. `model` 有給時，候選限於被 `models.allow` 的樣式（fnmatch）涵蓋的；無一涵蓋 → 403 `model_not_allowed`。`model` 省略時，候選限於**能推導模型**的（kind 為 `claude-subscription`，或 `models.default` 非 null，或 `allow` 至少一個不含萬用字元的樣式）；無一能推導 → 400 `no_model`（釘住時就是該把——同 provider 另一把可推導也不代換）。`claude-subscription`＋default null 算能推導（用 agent 預設、合法 200），所以 `no_model` 只落在非 `claude-subscription` 且 default null、`allow` 全萬用字元的形狀（現行政策沒有這種 provider；單元測試用假政策）。
5. 沒有釘住時，套 `exclude`（不在政策檔的 id 忽略）；沒有候選 → 409 `no_candidate`。
6. 排除 key 未部署的候選；全部未部署 → 409 `not_deployed`。
7. 挑 `priority` 最小，同分以 id 排序。

步驟 3.5 排在第 5 步的 409 `no_candidate` 與第 6 步的 409 `not_deployed` **之前**：隱私不足時回 403 `privacy_not_allowed`，不會被 `exclude` 救成 409，也不會因為釘住的那把剛好沒部署 key 變成 409。

冷卻 MUST NOT 參與選把：任何候選 MUST NOT 因為整把或模型的冷卻被排除，排序也 MUST NOT 因冷卻改變（`priority` 與 id 是唯二的排序鍵）。冷卻只影響回覆附帶的資訊（見「resolve」的 `meta`）。`privacy` MUST NOT 參與排序：篩完之後仍只按 `priority` 與 id 選（宣告得起的低等級訂閱不會因為「等級較低」被排到後面）。

錯誤訊息 MUST NOT 回顯請求裡的值。`subscription` 與 `provider` 不一致的檢查只在該 id 存在時成立（id 未知回 404）。多個錯誤同時成立時 SHALL 回優先序最前者：輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403 `agent_not_allowed`／`privacy_not_allowed`／`model_not_allowed`，與 400 `no_model`）先於候選狀態（409 `no_candidate`／`not_deployed`）——與上面的步驟順序同一軸；三個 403 之間的先後是 agent → privacy → model。釘住的那把冷卻中 MUST 仍回 200（不代換另一把），並在 `meta` 附上該把的冷卻；`exclude` 後沒有候選 MUST 回 409 `no_candidate`，不選回被排除的那把。

#### Scenario: 同 provider 兩把，一把冷卻中
- **WHEN** `claude-personal`（priority 10）在冷卻中，以 `agent=claude-code&provider=anthropic` resolve（未釘住）
- **THEN** 回 200，選中 `claude-personal`（priority 10；冷卻不影響選把），`meta.cooldown` 帶它的冷卻時刻

#### Scenario: 全部候選冷卻中
- **WHEN** provider＋agent 的所有候選都在冷卻中
- **THEN** 回 200，選 `priority` 最小者，`meta` 附冷卻資訊；不回 409、不含 `error`

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

#### Scenario: A 模型冷卻時指定 B 模型仍選得到同一把
- **WHEN** `opencode-zen` 的 `mimo-v2.6-flash-free` 在冷卻中，以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 回 200，`meta.subscription` 是 `opencode-zen`、`meta.model` 是 `big-pickle`；`meta.model_cooldowns` 仍列出 `mimo-v2.6-flash-free` 的冷卻

#### Scenario: 指定冷卻中的那個模型仍給 key
- **WHEN** `opencode-zen` 的 `mimo-v2.6-flash-free` 在冷卻中，以同一個模型 resolve
- **THEN** 回 200，`meta.model` 是 `mimo-v2.6-flash-free`、`env` 照常、`meta.model_cooldowns` 有那一筆；不回 409、不回 `until` 錯誤欄位

#### Scenario: 整把冷卻時仍選得到
- **WHEN** `opencode-zen` 整把冷卻中，以 `agent=opencode&provider=opencode-zen` resolve
- **THEN** 回 200，`meta.cooldown` 是 `{"until": …}`，`env` 照常（Gateway 不擋也不代換）

#### Scenario: 不指定 model 時 default 冷卻仍回 200
- **WHEN** `opencode-zen` 的 `models.default`（`mimo-v2.6-flash-free`）在冷卻中、其他模型沒冷卻，以 `agent=opencode&provider=opencode-zen` resolve（不帶 `model`）
- **THEN** 回 200，`meta.model` 是 `mimo-v2.6-flash-free`（推導出的模型）、`meta.model_cooldowns` 有那一筆；Gateway 不代選另一個模型

#### Scenario: 冷卻不是排序鍵
- **WHEN** 同 provider 的兩把 priority 相同，id 較小的那把在冷卻中
- **THEN** 回 200，選 id 較小的那把（排序只看 priority 與 id），`meta` 附它的冷卻

#### Scenario: 模型未知時冷卻只是資訊
- **WHEN** 某把 `claude-subscription` 的 `models.default` 為 null、只有某個模型的冷卻、整把沒有冷卻，以該把 resolve（不帶 `model`）
- **THEN** 回 200（`meta.model` 為 null），`meta.model_cooldowns` 照實列出那一筆；不會因為模型未知而改看整把或拒絕

#### Scenario: 釘住的低等級訂閱被擋
- **WHEN** 以 `agent=opencode&provider=opencode-zen&subscription=opencode-zen&data=real` resolve（`opencode-zen` 的 `privacy` 是 `public`）
- **THEN** 回 403 `privacy_not_allowed`，不含 `env`——釘住不會繞過隱私（第 1 步先限縮成那把，第 3.5 步只判斷它）

#### Scenario: 未釘住時低等級候選被篩掉
- **WHEN** 同一個 provider 有兩把（`privacy` 分別是 `public` 與 `real`），以 `data=real` resolve（未釘住）
- **THEN** 只選得 `privacy: real` 的那把；不帶 `data` 時兩把都在候選、照 `priority` 選

#### Scenario: 全部候選隱私不足回 403
- **WHEN** provider＋agent 的候選 `privacy` 都低於宣告的 `data`，而且同時給了 `exclude`
- **THEN** 回 403 `privacy_not_allowed`（第 3.5 步先於第 5 步的 409 `no_candidate`），不含 `env`

#### Scenario: 隱私 403 先於 model 403
- **WHEN** 以 `agent=opencode&provider=opencode-zen&model=muse-spark-1.3&data=real` resolve（model 不在 allow，而 zen 的 `privacy` 也不夠）
- **THEN** 回 403 `privacy_not_allowed`（第 3.5 步先於第 4 步）

#### Scenario: 釘住且 agent 不允許＋隱私不足
- **WHEN** 以 `agent=claude-code&provider=opencode-zen&subscription=opencode-zen&data=real` resolve（zen 不給 `claude-code`，且 `privacy` 是 `public`）
- **THEN** 回 403 `agent_not_allowed`（第 3 步先於第 3.5 步；不是 `privacy_not_allowed`）

#### Scenario: 釘住且隱私不足但 key 未部署
- **WHEN** 以 `agent=opencode&provider=opencode-zen&subscription=opencode-zen&data=real` resolve，而該把的 key 未部署
- **THEN** 回 403 `privacy_not_allowed`（第 3.5 步先於第 6 步的 409 `not_deployed`），不是 409；不帶 `data` 的同一請求才回 409 `not_deployed`

#### Scenario: 隱私不改 priority 排序
- **WHEN** 同 provider 的兩把 priority 分別是 10（`privacy: public`）與 20（`privacy: real`），以 `data=public` resolve
- **THEN** 兩把都合格，仍選 priority 10 的那把（`privacy` 不參與排序）

### Requirement: 錯誤回覆

所有錯誤 SHALL 以 `{"error": "<碼>"}` 的 JSON 回覆，訊息不得回顯請求裡的值；resolve 的錯誤回覆 MUST NOT 含 `env`。完整碼表：
- 400 `bad_request`：JSON 解析失敗、body 不是物件、缺必填（帶 `missing: [...]`）；登錄冷卻的 `model` 不是非空字串（`null` 除外——那是「沒給」）、超過 200 字元、以 `<agent>/` 開頭（`opencode/`、`claude-code/`）、含萬用字元，或不被該把 `models.allow` 涵蓋；resolve 的輸入形狀錯（缺 `agent`／`provider`、`agent` 不是 `claude-code`／`opencode`、`data` 有給但不在 `public`／`general`／`real` 之內、`subscription` 與 `provider` 不一致、`subscription` 與 `exclude` 同時給、同一 query 參數重複）；
- 400 `invalid_until`：`until` 不是 RFC 3339 字串（或型別不對）；
- 400 `until_out_of_range`：`until` 在過去或超過 8 天以後；
- 400 `no_model`：resolve 的 `model` 省略且沒有候選能推導模型（不代選）；
- 403 `agent_not_allowed`：`agent` 不在該筆的 `agents` 裡（get 與 resolve 的選把步驟 3 共用）；
- 403 `model_not_allowed`：resolve 的 model 不被任何候選的 `models.allow` 涵蓋；
- 403 `privacy_not_allowed`：resolve 宣告的 `data` 高於所有候選的 `privacy`（步驟 3.5；`data` 不在三個等級是 400，不是這個碼）；
- 404 `not_found`：未知路徑，或不存在的 subscription id（resolve 的未知 provider／未知 `subscription` 同用）；
- 409 `not_deployed`：key 尚未部署；
- 409 `no_candidate`：resolve 的 `exclude` 排除後沒有候選（沒有恢復時刻）；
- 413 `body_too_large`：body 超過 1 MB；
- 500 `internal`：伺服器內部非預期錯誤（不帶細節）。

新 Gateway MUST NOT 回 409 `cooling`：冷卻不是錯誤（見「resolve 的選把規則」），錯誤回覆也不再有 `until`。舊版 Gateway 仍可能回 409 `cooling`（body 帶 `until`）；`lgw.client` 與側車 MUST 保留對這個碼的判讀，直到所有 Gateway 都升到不回它的版本。

客戶端（`lgw.client`）另外判讀兩種結果，不是服務回的碼：`unreachable`（連不上或逾時，呼叫方不會拿到例外）、`bad_response`（回覆不是 JSON 物件）。

多個錯誤同時成立時 SHALL 回優先序最前者：輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403，與 400 `no_model`）先於候選狀態（409 `no_candidate`／`not_deployed`）——與「resolve 的選把規則」的步驟同一軸。政策拒絕的三個 403 之間 SHALL 是 `agent_not_allowed` 先於 `privacy_not_allowed` 先於 `model_not_allowed`；`privacy_not_allowed` 也先於兩種 409。**登錄冷卻不在這條通則內**：它的順序沿用現行程式（查無 404 先於 `until`／`model` 的 400，見「登錄冷卻」），因為那是既有行為、不是本 change 要改的語意。`exclude` 後沒有候選 MUST 回 409 `no_candidate`，不選回被排除的那把。

#### Scenario: 候選全部冷卻中
- **WHEN** resolve 的所有候選都在冷卻中
- **THEN** 回 200（冷卻不是錯誤），`meta` 附 `cooldown`／`model_cooldowns`，不會出現 `error` 或 `until`

#### Scenario: 模型被政策擋下
- **WHEN** resolve 的 model 不在候選的 `models.allow`
- **THEN** 回 403 `model_not_allowed`，不含 `env`，不代選其他模型

#### Scenario: 隱私被政策擋下
- **WHEN** resolve 宣告的 `data` 高於所有候選的 `privacy`
- **THEN** 回 403 `privacy_not_allowed`，body 只有 `error`（沒有 `env`、沒有 `usage`、沒有 `until`），不代選其他訂閱

#### Scenario: 排除後沒有候選
- **WHEN** resolve 的 `exclude` 排除了最後一個可用候選
- **THEN** 回 409 `no_candidate`，不含 `env`

#### Scenario: 未知 agent 是輸入錯誤
- **WHEN** resolve 的 `agent` 不是 `claude-code`／`opencode`
- **THEN** 回 400 `bad_request`

#### Scenario: 輸入形狀錯誤先於查無
- **WHEN** 以 `provider=NoSuchProvider&subscription=claude-personal` resolve（`claude-personal` 存在、其 provider 是 `anthropic`，與請求的 `provider` 不一致；`NoSuchProvider` 也不是政策裡的 provider）
- **THEN** 回 400 `bad_request`（`subscription` 與 `provider` 不一致先於 provider 不存在的 404）

#### Scenario: 缺必填
- **WHEN** 登錄冷卻的 body 沒有 `until`
- **THEN** 回 400 `bad_request`，`missing` 含 `until`

#### Scenario: 時刻格式不對
- **WHEN** `until` 不是 RFC 3339 字串
- **THEN** 回 400 `invalid_until`，冷卻不變

#### Scenario: 連不上
- **WHEN** 客戶端連不上服務
- **THEN** 結果的 `error` 為 `unreachable`，呼叫方沒有拿到例外

#### Scenario: 登錄冷卻的 model 被政策擋下
- **WHEN** 以不被該把 `models.allow` 涵蓋的 `model` 登錄冷卻
- **THEN** 回 400 `bad_request`（不是 403 `model_not_allowed`），冷卻不變
