## REMOVED Requirements

### Requirement: get
**Reason**: #48：單筆 get（含 key）移除。對外終態介面只有 list、resolve、登錄冷卻（roles 另見 role-allocation）；sidecar 已不呼叫 get（MyLinuxPool#541、#348 已關），稽核 get＝0。
**Migration**: 改用 resolve：要用哪把、哪個模型，交給 resolve 依政策挑；它回的 `env` 就是 agent 要的憑證與模型變數。沒有單筆取回 key 的替代端點；歷史 `action='get'` 稽核列保留不刪。

## MODIFIED Requirements

### Requirement: 共通 JSON schema

每個 subscription SHALL 以同一個 JSON 形狀描述，欄位如下：
- `schema`：該端點回覆形狀的版本（整數）；
- `id`：subscription 的名稱；
- `provider`、`kind`：供應商，與型態（`claude-subscription`、api-key 類，或沒有憑證的 `local-login`）；
- `agents`：允許使用這一把的 agent 種類清單；
- `priority`：整數，**數字小的先**；
- `models`：`{allow, default}`，`allow` 是模型 id 樣式（fnmatch）清單，`default` 是供應商自己的模型 id 或 null（語意見「models.default 的語意」）；
- `endpoint`：provider 的根位址，或 null（語意見「endpoint 的語意」）；
- `cooldown`：`{"until": RFC 3339 UTC}` 或 null，不分窗口；過了 `until` 即視為 null（整把的冷卻；各模型的冷卻另見 `model_cooldowns`）；
- `model_cooldowns`：物件（模型 id → `{"until": RFC 3339 UTC}`），**只列 `until` 還沒過的**（`until` 在過去或以現在為界即不出現）；沒有時是 `{}`；模型 id 可含 `/`、`:` 等字元；
- `available`：已部署（key 在；`kind` 為 `local-login` 時恆已部署）而且沒有未來的整把 `until` 時為 true，否則 false；用量為 0% MUST NOT 讓它變 false；模型冷卻（`model_cooldowns`）MUST NOT 讓它變 false；它是「部署＋整把冷卻」的粗提示，不是「保證不會被上游 429」；
- `available_again_at`：RFC 3339 UTC 或 null，**只在 list 出現**（合併規則見 spec `usage-reporting`）；
- `usage`：用量物件，**只在 list 出現**（resolve 的 `meta` 另帶同形狀的 `usage`，見「resolve」；形狀與規則見 spec `usage-reporting`）。

**訂閱 id 是固定集合**：`id` MUST 是 `lgwd/policy.py` 的 `SUBSCRIPTIONS` 常數裡的值（`claude-personal`、`claude-company`、`opencode-zen`、`opencode-go`、`groq`、`openrouter`、`ollama-cloud`，本 change 加 `antigravity`）。政策檔 `subscriptions` 的鍵不在這個集合裡時 MUST 政策載入失敗（`PolicyError("policy.subscriptions has unknown key")`，`lgwd/policy.py:253-255`）、lgwd 不啟動；所以新增一個訂閱＝改 `SUBSCRIPTIONS`（與 `AGENTS`、`PROVIDERS` 對應的一筆）＋政策檔兩處，兩邊不一致時是「載不起來」而不是「靜默忽略」。

這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 provider＋model 用起來的環境變數由 resolve 的回覆 `env` 給（「env 的組成」），list 只是菜單。`usage`／`available_again_at` 在 `list_subscriptions` 內逐筆合入，只出現在 list 的回覆。Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（條款；免費模型只經過 opencode）；`kind` 為 `local-login` 的訂閱（本 change 的 `antigravity`）的 `agents` MUST 只有 `agy`；政策載入 MUST 拒絕這三類訂閱的 `agents` 不是剛好那一個值的政策檔，也 MUST 拒絕其他訂閱的 `agents` 含 `agy`。`endpoint` MUST NOT 內嵌憑證：`endpoint` 不在政策檔，是 `lgwd/providers.py` 的 `CredentialProvider.endpoint` 常數；providers 載入／lgwd 啟動時 MUST 逐筆檢查 `PROVIDERS` 的 endpoint 值，含 `key=`／`token=`／`api_key=`／`access_token=` query 樣式或 userinfo `@` 即拒絕啟動（`PolicyError`；endpoint 是中介資料、會進稽核與 meta——前提是不含憑證）。`schema` 是該端點回覆形狀的版本；list row 的 `schema` 與 resolve 的 `schema` 各自獨立。list 新增 `usage`／`available_again_at` 只是新增欄位，`schema` MUST NOT bump（目前仍為 1；消費端忽略不認得的欄位）。`model_cooldowns` 也是新增欄位，同樣 MUST NOT bump `schema`；它出現在 list 的回覆，resolve 的 `meta` 也帶同一形狀的 `cooldown` 與 `model_cooldowns`（見「resolve」）。`cooldown` 與 `available` 仍是**整把**的語意：`available` MUST NOT 因 `model_cooldowns` 改變（消費端用 `available` 判斷 key 有沒有部署，把模型冷卻算進去會讓一個模型用完看起來像整把沒部署）；`kind` 為 `local-login` 時「部署」由 kind 判定，不是 key。

#### Scenario: list 的形狀
- **WHEN** 呼叫 list
- **THEN** row 只有上列欄位（含 `available_again_at`、`usage`、`model_cooldowns`）：不含 key、不含任何環境變數名

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
- **WHEN** 讀取 `openrouter` 的描述
- **THEN** `agents` 只有 `opencode`

#### Scenario: 能不能用
- **WHEN** 某把的 key 已部署，而且沒有未來的 `until`
- **THEN** `available` 為 true；有未來的 `until` 或 key 沒部署時為 false

#### Scenario: 新增欄位不 bump schema
- **WHEN** 呼叫 list
- **THEN** 回覆的 `schema` 仍為 1，既有欄位的名稱與語意不變

#### Scenario: get 不帶 usage
- **WHEN** 呼叫已移除的單筆路徑（`GET /v1/subscriptions/{id}?agent=`）
- **THEN** 回 404 `not_found`；`usage` 與 `available_again_at` 只在 list 出現

#### Scenario: 模型冷卻的形狀
- **WHEN** `opencode-zen` 有兩個模型的冷卻，一個還沒過、一個已經過了
- **THEN** `model_cooldowns` 只有還沒過的那一個，值是 `{"until": "<RFC 3339 UTC>"}`；兩個都過期時是 `{}`

#### Scenario: 模型冷卻不改 available
- **WHEN** 某把只有模型冷卻、整把沒有未來的 `until`、key 已部署
- **THEN** `available` 為 true、`cooldown` 為 null，`model_cooldowns` 有那一筆

#### Scenario: 模型冷卻不 bump schema
- **WHEN** 呼叫 list，某把有模型冷卻
- **THEN** 回覆的 `schema` 仍為 1，`cooldown` 仍是 `{"until"}` 或 null（沒有被塞進模型）

#### Scenario: 模型 id 可含斜線與冒號
- **WHEN** 某把的 `model_cooldowns` 有 `openrouter/free` 這種含 `/` 的模型 id
- **THEN** 它是正常的物件鍵，值是 `{"until": "…"}`，不影響其他鍵與 `models` 的內容

#### Scenario: 沒有憑證的訂閱也在 list
- **WHEN** 呼叫 list，政策檔含 `antigravity`
- **THEN** 該筆的 `kind` 為 `local-login`、`endpoint` 為 null、`agents` 為 `["agy"]`、`available` 為 true（沒有未來冷卻時）、`usage.status` 為 `not_applicable`、`available_again_at` 為 null，且沒有 `key`

#### Scenario: 沒有憑證的訂閱 agents 改寬
- **WHEN** 政策檔的 `antigravity.agents` 不是剛好 `["agy"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

### Requirement: 稽核紀錄

每次**成功**（200）的 resolve 與每次登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類（resolve）、provider 與 model（resolve；登錄冷卻記 `model`，沒給時為 null）、結束時刻 `until`（登錄冷卻記有效的 `until`）。紀錄 MUST NOT 含憑證的值，也 MUST NOT 含 `env` 的憑證值；provider／model 是中介資料，SHALL 記（否則「誰用了哪個模型」就沒有紀錄）。失敗的回覆（4xx／409）不記稽核（沒有選中的 subscription 可記），診斷靠只含方法／路徑／狀態碼的 HTTP log。不做認證，紀錄不分是哪個客戶端。`action` 為 `get` 的歷史列（#48 移除前寫入）保留不刪，移除後不再寫。

#### Scenario: resolve 留下紀錄
- **WHEN** 有客戶端以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 稽核紀錄有一筆（subscription、agent、provider、model），內容沒有憑證值或 env 的憑證值

#### Scenario: get 留下紀錄
- **WHEN** 呼叫已移除的單筆路徑（`GET /v1/subscriptions/{id}?agent=`，LLMGateway#48）
- **THEN** 回 404，稽核紀錄不新增任何一筆

#### Scenario: 舊 DB 沒有新欄位
- **WHEN** 以 v5 schema 的既有 DB 檔啟動新 lgwd，並寫入一筆 resolve
- **THEN** 啟動時補上 `provider`／`model` 欄位，寫入成功；舊資料的這兩個欄位為 null

#### Scenario: 模型冷卻留下紀錄
- **WHEN** 以 `model=mimo-v2.6-flash-free` 登錄 `opencode-zen` 的冷卻
- **THEN** 稽核紀錄的 `action` 是 `cooldown`、`model` 是該模型 id、`until` 是有效值、`agent` 為 null

### Requirement: 錯誤回覆

所有錯誤 SHALL 以 `{"error": "<碼>"}` 的 JSON 回覆，訊息不得回顯請求裡的值；resolve 的錯誤回覆 MUST NOT 含 `env`。完整碼表：
- 400 `bad_request`：JSON 解析失敗、body 不是物件、缺必填（帶 `missing: [...]`）；登錄冷卻的 `model` 不是非空字串（`null` 除外——那是「沒給」）、超過 200 字元、以 `<agent>/` 開頭（`opencode/`、`claude-code/`、`agy/`）、含萬用字元，或不被該把 `models.allow` 涵蓋；resolve 的輸入形狀錯（缺 `agent`／`provider`、`agent` 不是 `claude-code`／`opencode`／`agy`、`data` 有給但不在 `public`／`general`／`real` 之內、`subscription` 與 `provider` 不一致、`subscription` 與 `exclude` 同時給、同一 query 參數重複）；
- 400 `invalid_until`：`until` 不是 RFC 3339 字串（或型別不對）；
- 400 `until_out_of_range`：`until` 在過去或超過 8 天以後；
- 400 `no_model`：resolve 的 `model` 省略且沒有候選能推導模型（不代選）；
- 403 `agent_not_allowed`：`agent` 不在該筆的 `agents` 裡（resolve 的選把步驟 3）；
- 403 `model_not_allowed`：resolve 的 model 不被任何候選的 `models.allow` 涵蓋；
- 403 `privacy_not_allowed`：resolve 宣告的 `data` 高於所有候選的 `privacy`（步驟 3.5；`data` 不在三個等級是 400，不是這個碼）；
- 404 `not_found`：未知路徑，或不存在的 subscription id（resolve 的未知 provider／未知 `subscription` 同用）；
- 409 `not_deployed`：key 尚未部署（`kind` 為 `local-login` 的訂閱沒有憑證，但 resolve 不會因它回 409）；
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
- **WHEN** resolve 的 `agent` 不是 `claude-code`、`opencode` 或 `agy`
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

#### Scenario: 沒有憑證的訂閱不是 not_deployed 的錯誤
- **WHEN** 以 `agent=agy&provider=google` resolve `antigravity`
- **THEN** 回 200（不是 409 `not_deployed`）

### Requirement: resolve

`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]][&data=<public|general|real>]` SHALL 回 `{schema, env_schema, meta, env}`。

`agent` 與 `provider` 必填；缺少時回 400 `bad_request`（`missing` 列出缺的欄位）。`agent` MUST 是 `claude-code`、`opencode` 或 `agy`，否則 400 `bad_request`（不是 403 `agent_not_allowed`）。`model` 選填，是**供應商自己的模型 id**（可含 `/`、`+`、`:` 等字元）。`subscription` 選填，是釘住某一把 subscription：有給時 MUST 與該把的 `provider` 相同，不一致回 400 `bad_request`；未知 id 回 404 `not_found`。`exclude` 選填，是逗號分隔的 subscription id 清單；不在政策檔的 id SHALL 忽略。`subscription` 與 `exclude` 同時給 MUST 回 400 `bad_request`。`data` 選填，是呼叫方**宣告的資料等級**：有給時 MUST 是 `public`、`general`、`real` 之一，否則 400 `bad_request`（`data` 不是資源，不是 404；等級夠不夠是政策拒絕的 403，見「resolve 的選把規則」）。**沒給 `data` 時 Gateway MUST NOT 依 `privacy` 過濾**（缺省不濾：不帶＝不擋，與這個參數加入前相同；宣告了才一定不降級，翻成缺省擋留給後續的 d）。未知 query 參數 SHALL 忽略；空值視為沒給（`subscription=x&exclude=` 因此是合法釘住），同一參數重複出現回 400 `bad_request`。所有 query 值由呼叫端 percent-encode（`+` 編成 `%2B`），server 以 form 解碼。

`meta` SHALL 含 `subscription`（選中的 subscription id）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`）、`endpoint`、`priority`、`data`（請求的資料等級**原樣**，沒給時 null）、`cooldown`（該把整把的冷卻：`{"until": RFC 3339 UTC}` 或 null，過了 `until` 即視為 null）、`model_cooldowns`（該把各模型的冷卻：物件，模型 id → `{"until": RFC 3339 UTC}`，只列還沒過的，沒有時 `{}`；形狀與 list 的同一欄位相同）。`meta.data` 是「這次宣告了什麼」，**不是生效等級**：resolve 沒有 role／floor，所以它不會被抬高，與 `roles` 回覆的 `data`（請求值與 floor 取較嚴）語意不同。`meta` 另含 `usage`（與 list 同形狀；只讀快取，快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 resolve MUST 仍照常回 200，`env` 與選把結果不受影響）。冷卻資訊是**提示**：resolve 的 200 不代表「沒有冷卻」；呼叫方要判斷「這次的呼叫會不會撞到冷卻」時，看 `meta.cooldown.until` 與（`meta.model` 非 null 時）`meta.model_cooldowns[meta.model].until`，兩者取較晚者。`meta` MUST NOT 含 `available`／`available_again_at`（200 已代表部署完成：key 在，或 `kind` 為 `local-login` 而恆已部署；`available` 恆為 true、不帶資訊；用量窗口的時刻在 `usage`）。resolve 回非 200 時不組裝 `usage`，也不觸發取用量。

`env` SHALL 是把選定的 provider＋model 用起來的必要環境變數（名稱→值），依 `agent` 組成（見「env 的組成」）。`agent` 為 `agy` 時 `env` MUST 是空物件 `{}`：它用自己的本機登入，Gateway 不給憑證也不給模型變數，模型只走 `meta.model`。`env` 的**憑證值** MUST NOT 出現在 list 的回覆、稽核紀錄、log 或 `GET /agents` status；model、provider、endpoint 是中介資料，稽核可記——前提是 **`endpoint` MUST NOT 內嵌憑證**（query 的 `key=`／`token=`／`api_key=`／`access_token=`、userinfo 的 `@`；providers 載入／啟動時拒絕，見「共通 JSON schema」）。錯誤回覆 MUST NOT 含 `env`。resolve 的 200 回覆 SHALL 帶 `Cache-Control: no-store` 與 `Pragma: no-cache`。

`schema` SHALL 是**該端點回覆形狀的版本**（resolve 與 list 各自獨立；目前皆為 1），resolve 形狀有**不相容**變更（移除／改名／改語意）時 MUST bump；只**新增** meta 欄位（例如 `usage`、`cooldown`、`model_cooldowns`、`data`）不 bump（消費端忽略不認得的欄位）。`env_schema` SHALL 是 **env 組成（鍵集與鍵的語意）的版本**（目前為 1）；新增／移除 env 鍵或改變鍵的語意時 MUST bump；Gateway MUST NOT 在同一個 `env_schema` 內改變鍵集（個別回應因 model 為 null 或 agent 為 `agy` 而少給鍵，不算鍵集變更）。消費端對不認得的更高 `schema`／`env_schema` 的處理（拒收等）見 `archive/2026-10-24-agent-provider-model-interface/design.md` 決定 6，由 sidecar 側規格承接。

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

#### Scenario: agy 的 resolve
- **WHEN** 以 `agent=agy&provider=google&model=<該把的裸 id>` resolve
- **THEN** 回 200，`meta.subscription` 為 `antigravity`、`meta.kind` 為 `local-login`、`meta.agent` 為 `agy`、`meta.model` 為該 id、`env` 為 `{}`；`env_schema` 仍是 1

#### Scenario: agy 的用量是 not_applicable
- **WHEN** resolve 成功選中 `antigravity`
- **THEN** `meta.usage.status` 是 `not_applicable`、`source` 與窗口都是 null，不觸發任何取用量

### Requirement: 沒有憑證的訂閱（kind `local-login`）

`kind` 為 `local-login` 的 subscription SHALL 表示憑證由呼叫方那一台機器自己保管（本 change 只有 `antigravity`：agy 用 Mac 本機的 Google 登入，Gateway 不搬、不匯出、不代管）。這一類訂閱：

- MUST NOT 出現在 key 檔，MUST NOT 佔 `KEY_NAMES` 的鍵名；Gateway MUST NOT 為它讀、寫、保管或轉發任何憑證；
- 「已部署」MUST 以 kind 判定（恆已部署），MUST NOT 讀 key 檔：list 的 `available` 因此只受整把冷卻影響，resolve 的第 6 步 MUST NOT 排除它，`available_again_at` MUST 只由冷卻計算；
- resolve 的 `env` MUST 是空物件 `{}`（見「env 的組成」）；模型只走 `meta.model`；
- `endpoint` MUST 為 null（見「endpoint 的語意」）；
- `usage` 為 `not_applicable`（沒有取用量實作）；政策檔的 `usage` 段對它不生效；
- 稽核紀錄照既有規則記 provider／model；沒有憑證值可記，也不因此新增事件或欄位。

Gateway MUST NOT 把這類訂閱當成「key 未部署」而回 409 `not_deployed`（resolve）或 `available: false`（list）。政策載入 MUST 拒絕這類訂閱的 `agents` 不是剛好 `["agy"]` 的政策檔（條款；見「共通 JSON schema」），也 MUST 拒絕其他訂閱的 `agents` 含 `agy`（否則 api-key 的 row 會用空 env 被選中）。

#### Scenario: list 不因沒有 key 顯示未部署
- **WHEN** key 檔裡沒有任何 `antigravity` 的鍵，呼叫 list
- **THEN** 該筆 `available` 為 true（沒有未來冷卻時）、`usage.status` 為 `not_applicable`、`available_again_at` 為 null，且沒有 `key`

#### Scenario: resolve 不讀 key
- **WHEN** 以 `agent=agy&provider=google` resolve `antigravity`，key 檔裡沒有它的鍵
- **THEN** 回 200，`env` 為 `{}`；Gateway 沒有對 key 檔做它的查詢（不因 `unknown subscription` 丟錯）

#### Scenario: get 沒有值可給
- **WHEN** 呼叫已移除的單筆路徑（`GET /v1/subscriptions/antigravity?agent=agy`）
- **THEN** 回 404 `not_found`，body 沒有 `key`

#### Scenario: 稽核沒有憑證
- **WHEN** 一次成功的 agy resolve 之後檢查稽核紀錄
- **THEN** 有 subscription、agent、provider、model，沒有任何憑證值；事件種類仍是 `resolve`，沒有新增欄位

#### Scenario: 冷卻仍可用
- **WHEN** 對 `antigravity` 登錄整把冷卻到未來
- **THEN** 回 204；list 的 `available` 為 false、`cooldown.until` 照實，`available_again_at` 為該時刻；resolve 仍回 200 並附冷卻資訊（冷卻不擋選把）
