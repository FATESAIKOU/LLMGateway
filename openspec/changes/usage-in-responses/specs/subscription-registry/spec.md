## MODIFIED Requirements

### Requirement: list
`GET /v1/subscriptions` SHALL 回所有 subscription 的共通 JSON，依 `priority` 排序，**不含 `key`**；每一筆多帶 `usage` 與 `available_again_at`（形狀與規則見 spec `usage-reporting`）。list 只讀用量快取，並在快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 list MUST 仍回 200。

#### Scenario: 不含秘密
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都沒有 `key` 欄位，回覆裡找不到任何 key 的值

#### Scenario: 冷卻反映在 list
- **WHEN** `claude-personal` 冷卻到 15:00（`cooldown.until` 為 15:00）
- **THEN** 15:00 之前 list 裡它的 `available` 為 false；15:00 之後 `available` 回到 true

#### Scenario: 每一筆都有 usage 與 available_again_at
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都有 `usage`（七個鍵齊全）與 `available_again_at`（RFC 3339 UTC 或 null）；沒有用量來源的 provider 是 `usage.status: not_applicable`

#### Scenario: 用量壞了 list 照回
- **WHEN** 取用量實作拋例外或卡住
- **THEN** list 仍回 200，其他欄位（`cooldown`、`available` 等）不變，該筆 `usage` 為 `unknown`

### Requirement: 共通 JSON schema

每個 subscription SHALL 以同一個 JSON 形狀描述，欄位如下：
- `schema`：該端點回覆形狀的版本（整數）；
- `id`：subscription 的名稱；
- `provider`、`kind`：供應商，與型態（`claude-subscription` 或 api-key 類）；
- `agents`：允許使用這一把的 agent 種類清單；
- `priority`：整數，**數字小的先**；
- `models`：`{allow, default}`，`allow` 是模型 id 樣式（fnmatch）清單，`default` 是供應商自己的模型 id 或 null（語意見「models.default 的語意」）；
- `endpoint`：provider 的根位址，或 null（語意見「endpoint 的語意」）；
- `key`：憑證值，**只在 get 的回覆出現**（過渡期，由 LLMGateway#48 移除）；list 不含；
- `cooldown`：`{"until": RFC 3339 UTC}` 或 null，不分窗口；過了 `until` 即視為 null；
- `available`：已部署（key 在）而且沒有未來的 `until` 時為 true，否則 false；用量為 0% MUST NOT 讓它變 false；
- `available_again_at`：RFC 3339 UTC 或 null，**只在 list 出現**（合併規則見 spec `usage-reporting`）；
- `usage`：用量物件，**只在 list 出現**（resolve 的 `meta` 另帶同形狀的 `usage`，見「resolve」；形狀與規則見 spec `usage-reporting`）。

這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 provider＋model 用起來的環境變數由 resolve 的回覆 `env` 給（「env 的組成」），list 只是菜單。`usage`／`available_again_at` 在 `list_subscriptions` 內逐筆合入，get 的回覆 MUST NOT 帶（get 由 LLMGateway#48 退場，不續命）。Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（條款；免費模型只經過 opencode）；政策載入 MUST 拒絕 openrouter 的 `agents` 不是剛好 `["opencode"]` 的政策檔。`endpoint` MUST NOT 內嵌憑證：`endpoint` 不在政策檔，是 `lgwd/providers.py` 的 `CredentialProvider.endpoint` 常數；providers 載入／lgwd 啟動時 MUST 逐筆檢查 `PROVIDERS` 的 endpoint 值，含 `key=`／`token=`／`api_key=`／`access_token=` query 樣式或 userinfo `@` 即拒絕啟動（`PolicyError`；endpoint 是中介資料、會進稽核與 meta——前提是不含憑證）。`schema` 是該端點回覆形狀的版本；list row 的 `schema` 與 resolve 的 `schema` 各自獨立。list 新增 `usage`／`available_again_at` 只是新增欄位，`schema` MUST NOT bump（目前仍為 1；消費端忽略不認得的欄位）。

#### Scenario: list 的形狀
- **WHEN** 呼叫 list
- **THEN** row 只有上列欄位（含 `available_again_at`、`usage`）：不含 key、不含任何環境變數名

#### Scenario: 政策檔把 openrouter 的 agents 改寬
- **WHEN** 政策檔的 `openrouter.agents` 不是剛好 `["opencode"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動；resolve 不會把 Claude Code 指到 OpenRouter

#### Scenario: provider 表的 endpoint 內嵌憑證
- **WHEN** 假 provider 表（monkeypatch `PROVIDERS`）某筆 `CredentialProvider.endpoint` 值含 `key=`／`token=`／`api_key=`／`access_token=` 的 query 或 userinfo（`user:pass@`）樣式
- **THEN** providers 載入／lgwd 啟動失敗（`PolicyError`），lgwd 不啟動（endpoint 會進稽核與 meta——前提是不含憑證）

#### Scenario: Claude 訂閱的描述
- **WHEN** 讀取 `claude-personal` 的描述
- **THEN** `provider` 為 anthropic、`kind` 為 claude-subscription、`agents` 只有 `claude-code`，形狀裡沒有任何環境變數名

#### Scenario: openrouter 只給 opencode
- **WHEN** 以 `agent=claude-code` get `openrouter`
- **THEN** 回 403 `agent_not_allowed`，不含 key

#### Scenario: 能不能用
- **WHEN** 某把的 key 已部署，而且沒有未來的 `until`
- **THEN** `available` 為 true；有未來的 `until` 或 key 沒部署時為 false

#### Scenario: 新增欄位不 bump schema
- **WHEN** 呼叫 list
- **THEN** 回覆的 `schema` 仍為 1，既有欄位的名稱與語意不變

#### Scenario: get 不帶 usage
- **WHEN** 以合法的 `agent` get 任一 subscription
- **THEN** 回覆沒有 `usage`、沒有 `available_again_at`，形狀與本 change 之前相同

### Requirement: resolve

`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]]` SHALL 回 `{schema, env_schema, meta, env}`。

`agent` 與 `provider` 必填；缺少時回 400 `bad_request`（`missing` 列出缺的欄位）。`agent` MUST 是 `claude-code` 或 `opencode`，否則 400 `bad_request`（不是 403 `agent_not_allowed`）。`model` 選填，是**供應商自己的模型 id**（可含 `/`、`+`、`:` 等字元）。`subscription` 選填，是釘住某一把 subscription：有給時 MUST 與該把的 `provider` 相同，不一致回 400 `bad_request`；未知 id 回 404 `not_found`。`exclude` 選填，是逗號分隔的 subscription id 清單；不在政策檔的 id SHALL 忽略。`subscription` 與 `exclude` 同時給 MUST 回 400 `bad_request`。未知 query 參數 SHALL 忽略；空值視為沒給（`subscription=x&exclude=` 因此是合法釘住），同一參數重複出現回 400 `bad_request`。所有 query 值由呼叫端 percent-encode（`+` 編成 `%2B`），server 以 form 解碼。

`meta` SHALL 含 `subscription`（選中的 subscription id）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`）、`endpoint`、`priority`。`meta` 另含 `usage`（與 list 同形狀；只讀快取，快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 resolve MUST 仍照常回 200，`env` 與選把結果不受影響）。`meta` MUST NOT 含 `cooldown`／`available`／`available_again_at`（200 已代表可用；這三個在 resolve 恆為 null／true／null，不帶資訊）。resolve 回非 200 時不組裝 `usage`，也不觸發取用量。

`env` SHALL 是把選定的 provider＋model 用起來的必要環境變數（名稱→值），依 `agent` 組成（見「env 的組成」）。`env` 的**憑證值** MUST NOT 出現在 list 的回覆、稽核紀錄、log 或 `GET /agents` status；model、provider、endpoint 是中介資料，稽核可記——前提是 **`endpoint` MUST NOT 內嵌憑證**（query 的 `key=`／`token=`／`api_key=`／`access_token=`、userinfo 的 `@`；providers 載入／啟動時拒絕，見「共通 JSON schema」）。錯誤回覆 MUST NOT 含 `env`。resolve 的 200 回覆 SHALL 帶 `Cache-Control: no-store` 與 `Pragma: no-cache`（過渡期的 get 亦同）。

`schema` SHALL 是**該端點回覆形狀的版本**（resolve 與 list 各自獨立；目前皆為 1），resolve 形狀有**不相容**變更（移除／改名／改語意）時 MUST bump；只**新增** meta 欄位（例如 `usage`）不 bump（消費端忽略不認得的欄位）。`env_schema` SHALL 是 **env 組成（鍵集與鍵的語意）的版本**（目前為 1）；新增／移除 env 鍵或改變鍵的語意時 MUST bump；Gateway MUST NOT 在同一個 `env_schema` 內改變鍵集（個別回應因 model 為 null 而少給模型鍵，不算鍵集變更）。消費端對不認得的更高 `schema`／`env_schema` 的處理（拒收等）見 `archive/2026-10-24-agent-provider-model-interface/design.md` 決定 6，由 sidecar 側規格承接。

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
- **THEN** `meta` 含 `subscription`、`provider`、`kind`、`agent`、`model`、`models`、`endpoint`、`priority`、`usage`，不含 `cooldown`／`available`／`available_again_at`

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
- **WHEN** resolve 回 409 `cooling`
- **THEN** body 只有既有欄位（含 `until`），沒有 `usage`、沒有 `env`
