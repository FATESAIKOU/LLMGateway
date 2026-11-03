# subscription-registry Specification

## Purpose
定義 LLMGateway 的 Provider-Subscription 菜單（`list` 端點）、共通 JSON schema、endpoint 規格、訂閱 id 集合、稽核紀錄，以及沒有憑證的訂閱（`local-login`）。
> 本檔 Scenario 中的 subscription 名稱、priority 與 models.default 是**示例政策**，用來說明規則，不是出廠政策；出廠政策以 `policy/default.json` 為準。

> v5（2026-10-12）：冷卻 until-only（`稽核紀錄`、`共通 JSON 的 endpoint 與 cooldown` 以 until-only 語意為準）。
## Requirements
### Requirement: list
`GET /v1/subscriptions` SHALL 回所有 subscription 的共通 JSON，依 `priority` 排序，**不含 `key`**；每一筆多帶 `usage` 與 `available_again_at`（形狀與規則見 spec `usage-reporting`）；每一筆另帶 `model_cooldowns`（該把各模型的冷卻，只列還沒過的；形狀見「共通 JSON 的 model_cooldowns」）。list 只讀用量快取，並在快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 list MUST 仍回 200。

#### Scenario: 不含秘密
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都沒有 `key` 欄位，回覆裡找不到任何 key 的值

#### Scenario: 冷卻反映在 list
- **WHEN** `claude-personal` 冷卻到 15:00（`cooldown.until` 為 15:00）
- **THEN** 15:00 之前 list 裡它的 `available` 為 false；15:00 之後 `available` 回到 true（這是給人看的粗提示；resolve 不會因此拒絕）

#### Scenario: 每一筆都有 usage 與 available_again_at
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都有 `usage`（七個鍵齊全）與 `available_again_at`（RFC 3339 UTC 或 null）；沒有用量來源的 provider 是 `usage.status: not_applicable`

#### Scenario: 用量壞了 list 照回
- **WHEN** 取用量實作拋例外或卡住
- **THEN** list 仍回 200，其他欄位（`cooldown`、`available`、`model_cooldowns` 等）不變，該筆 `usage` 為 `unknown`

#### Scenario: 模型冷卻反映在 list
- **WHEN** `opencode-zen` 的 `mimo-v2.6-flash-free` 冷卻到 15:00，同一把的其他模型沒有冷卻
- **THEN** 15:00 之前 `model_cooldowns` 有 `mimo-v2.6-flash-free`（`until` 為 15:00），而 `cooldown` 仍是 null、`available` 仍是 true（key 已部署時）；15:00（含相等）之後該鍵消失

#### Scenario: 整把與模型冷卻同時列出
- **WHEN** 同一把先有模型 A 冷卻到 16:00，再有整把冷卻到 15:00
- **THEN** `cooldown` 是 `{"until": "…15:00…"}`、`model_cooldowns` 有 A（`until` 為 16:00）；兩個欄位都保留，互不覆蓋

### Requirement: models.default 的語意

`models.default`（與 resolve 的 `model` 參數）SHALL 是**供應商自己的模型 id**（例 `mimo-v2.6-flash-free`、`openrouter/free`——可含 `/`），不帶 agent 專用的 provider 前綴；組成 agent 專用的字串（例 opencode 的 `<id>/<model>`）由 sidecar 在 `OPENCODE_CONFIG_CONTENT` 內組，`meta.model` 一律不帶前綴。resolve 的 `model` 參數不做前綴正規化：帶 `<provider>/<model>` 的值會照 `models.allow` 比對（通常 403 `model_not_allowed`）；sidecar 的 `POST /agents` 若收到前綴，要自己正規化或回 400。null 的語意依 `kind`（見 `resolve-env`「model 省略時的語意」）：`claude-subscription` 表示用 agent 自己的預設；其他 kind 表示沒有預設、服務不代選。

#### Scenario: 供應商自己的模型 id
- **WHEN** 讀取 resolve 成功的 `meta`
- **THEN** `meta.model` 是供應商自己的模型 id，不帶 `opencode/` 等前綴；前綴只出現在 sidecar 組的 config 內容裡

#### Scenario: 帶前綴的 model 不自動正規化
- **WHEN** 以 `provider=opencode-zen&model=opencode/big-pickle` resolve
- **THEN** 回 403 `model_not_allowed`（不是自動去掉 `opencode/` 後成功）

#### Scenario: claude-subscription 的 default 為 null 時
- **WHEN** 某把 claude-subscription 的 `models.default` 為 null
- **THEN** 呼叫方不帶模型參數，用 Claude Code 自己的預設模型

#### Scenario: 其他 kind 沒有預設
- **WHEN** 某筆非 `claude-subscription` 的 `models.default` 為 null
- **THEN** 服務不代選模型；呼叫方自己從 `allow` 選一個模型來用，或不用這一筆（經 resolve 時依 `resolve-env`「model 省略時的語意」推導）

### Requirement: models.default 必須被 allow 涵蓋
政策檔載入時 SHALL 檢查：`models.default` 不是 null 時，必須被同一筆 `models.allow` 的樣式涵蓋（fnmatch）；不符合 MUST 載入失敗（`PolicyError`），服務不啟動。`default` 為 null 時不做這項檢查。

#### Scenario: default 蓋不進 allow
- **WHEN** 政策檔某筆的 `models.default` 是 `muse-spark-1.3`，同一筆的 `models.allow` 只有 `*-free` 與 `big-pickle`
- **THEN** 載入失敗（`PolicyError`），訊息指出是哪一筆

### Requirement: endpoint 的語意
`endpoint` SHALL 是 provider 的**根位址**（值以 `lgwd/providers.py` 為準）。

#### Scenario: opencode driver 拿到 ollama-cloud 的 endpoint
- **WHEN** opencode 的 driver 拿到 `ollama-cloud` 的 `endpoint`（`https://ollama.com`）
- **THEN** 它自己組 OpenAI 相容的路徑（例如再接 `/v1`），或不覆寫內建 provider 的 baseURL；服務回的 `endpoint` 不變

### Requirement: claude-subscription 的 endpoint 為 null
`kind` 為 `claude-subscription` 時 `endpoint` MUST 為 null。

#### Scenario: Claude 訂閱沒有 endpoint
- **WHEN** 讀取 `claude-personal` 的共通 JSON
- **THEN** `endpoint` 為 null，agent 直連、不使用 base

### Requirement: 出廠 endpoint 的形狀
出廠 endpoint SHALL 為：`ollama-cloud` 的 `https://ollama.com` 可以直接當 Claude Code 的 `ANTHROPIC_BASE_URL`（Anthropic 相容的 base）；`openrouter` 的 `https://openrouter.ai/api` 同屬 Anthropic 相容的 base（本系統只給 opencode，接法由 opencode driver 決定）；兩者的 OpenAI 相容路徑由呼叫方自己組（例如再接 `/v1`）。

#### Scenario: Claude Code 拿到 ollama-cloud 的 endpoint
- **WHEN** Claude Code 的 driver 拿到 `ollama-cloud` 的 `endpoint`
- **THEN** 直接當 `ANTHROPIC_BASE_URL` 用（這個值是 Anthropic 相容的 base）

### Requirement: opencode-zen 與 groq 的出廠 endpoint
出廠 endpoint SHALL 為：`opencode-zen` 的 `https://opencode.ai/zen/v1`（OpenAI 相容的 base）；`groq` 的 `https://api.groq.com/openai/v1`（OpenAI 相容的 base）；`opencode-go` 的 `https://opencode.ai/inference/go/openai/v1`：chat 的 OpenAI 相容 base。Go 的 models 端點前綴不同（`https://opencode.ai/inference/go/v1/models`），呼叫方 MUST NOT 從 `endpoint` 直接接 `/models` 推導（官方文件沒有這條路徑）。

#### Scenario: opencode-zen 與 groq 的 endpoint
- **WHEN** 讀取 `opencode-zen` 與 `groq` 的 endpoint
- **THEN** 分別為 `https://opencode.ai/zen/v1` 與 `https://api.groq.com/openai/v1`

#### Scenario: opencode-go 的 endpoint
- **WHEN** 讀取 `opencode-go` 的共通 JSON
- **THEN** `endpoint` 為 `https://opencode.ai/inference/go/openai/v1`，`provider` 為 `opencode-go`

### Requirement: local-login 的 endpoint 為 null
`kind` 為 `local-login`（本 change 的 `antigravity`）時 `endpoint` MUST 為 null。agy 用呼叫方那一台機器的本機登入直連上游，Gateway MUST NOT 代理、MUST NOT 換端點。

#### Scenario: agy 沒有 endpoint
- **WHEN** 讀取 `antigravity` 的共通 JSON
- **THEN** `endpoint` 為 null、`kind` 為 `local-login`；Gateway 不回任何要交給 agy 的位址

### Requirement: 呼叫方不假設 endpoint 的協定
呼叫方（agent driver）MUST NOT 假設 `endpoint` 是某種協定的完整路徑；要依 `provider` 組出自己要的接法，或使用 agent 內建的 provider 設定。

#### Scenario: driver 自行組接法
- **WHEN** Claude Code 或 opencode 的 driver 拿到任一非 null 的 `endpoint`
- **THEN** 不把它當完整路徑使用，依 `provider` 組出接法或使用 agent 內建設定

### Requirement: 共通 JSON schema

每個 subscription SHALL 以同一個 JSON 形狀描述；`schema` 是該端點回覆形狀的版本（整數），`id` 是 subscription 的名稱，`provider`、`kind` 分別是供應商與型態（`claude-subscription`、api-key 類，或沒有憑證的 `local-login`）。

#### Scenario: list 的形狀
- **WHEN** 呼叫 list
- **THEN** row 只有本檔各「共通 JSON …」requirement 定義的欄位：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models`、`endpoint`、`cooldown`、`model_cooldowns`、`available`、`available_again_at`、`usage`；不含 key、不含任何環境變數名

### Requirement: 共通 JSON 的 agents、priority 與 models
每筆 SHALL 含 `agents`（允許使用這一把的 agent 種類清單）、`priority`（整數，數字小的先）與 `models`（`{allow, default}`：`allow` 是模型 id 樣式（fnmatch）清單，`default` 是供應商自己的模型 id 或 null，語意見「models.default 的語意」）。

#### Scenario: 共通 JSON 欄位的型別
- **WHEN** 讀取任一筆 list row
- **THEN** `agents` 是陣列，`priority` 是整數，`models` 有 `allow` 與 `default` 兩鍵

### Requirement: 共通 JSON 的 endpoint 與 cooldown
每筆 SHALL 含 `endpoint`（provider 的根位址，或 null，語意見「endpoint 的語意」）與 `cooldown`（`{"until": RFC 3339 UTC}` 或 null，不分窗口；過了 `until` 即視為 null，是整把的冷卻；各模型的冷卻另見 `model_cooldowns`）。

#### Scenario: 過了 until 即為 null
- **WHEN** 某把的整把冷卻 `until` 已過
- **THEN** `cooldown` 為 null

### Requirement: 共通 JSON 的 model_cooldowns
每筆 SHALL 含 `model_cooldowns`：物件（模型 id → `{"until": RFC 3339 UTC}`），**只列 `until` 還沒過的**（`until` 在過去或以現在為界即不出現）；沒有時是 `{}`；模型 id 可含 `/`、`:` 等字元。

#### Scenario: 模型冷卻的形狀
- **WHEN** `opencode-zen` 有兩個模型的冷卻，一個還沒過、一個已經過了
- **THEN** `model_cooldowns` 只有還沒過的那一個，值是 `{"until": "<RFC 3339 UTC>"}`；兩個都過期時是 `{}`

#### Scenario: 模型 id 可含斜線與冒號
- **WHEN** 某把的 `model_cooldowns` 有 `openrouter/free` 這種含 `/` 的模型 id
- **THEN** 它是正常的物件鍵，值是 `{"until": "…"}`，不影響其他鍵與 `models` 的內容

### Requirement: 共通 JSON 的 available
每筆 SHALL 含 `available`：已部署（key 在；`kind` 為 `local-login` 時恆已部署）而且沒有未來的整把 `until` 時為 true，否則 false；用量為 0% MUST NOT 讓它變 false；模型冷卻（`model_cooldowns`）MUST NOT 讓它變 false；它是「部署＋整把冷卻」的粗提示，不是「保證不會被上游 429」。

#### Scenario: 能不能用
- **WHEN** 某把的 key 已部署，而且沒有未來的 `until`
- **THEN** `available` 為 true；有未來的 `until` 或 key 沒部署時為 false

### Requirement: 共通 JSON 的 available_again_at 與 usage
每筆 SHALL 含 `available_again_at`（RFC 3339 UTC 或 null，只在 list 出現，合併規則見 spec `usage-reporting`）與 `usage`（用量物件，只在 list 出現；resolve 的 `meta` 另帶同形狀的 `usage`，見 `resolve`「resolve」；形狀與規則見 spec `usage-reporting`）。`usage`／`available_again_at` 在 `list_subscriptions` 內逐筆合入，只出現在 list 的回覆。

#### Scenario: get 不帶 usage
- **WHEN** 呼叫已移除的單筆路徑（`GET /v1/subscriptions/{id}?agent=`）
- **THEN** 回 404 `not_found`；`usage` 與 `available_again_at` 只在 list 出現

### Requirement: 共通 JSON 形狀不帶 agent 專用變數
這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 provider＋model 用起來的環境變數由 resolve 的回覆 `env` 給（`resolve-env`「env 的組成」），list 只是菜單。

#### Scenario: Claude 訂閱的描述
- **WHEN** 讀取 `claude-personal` 的描述
- **THEN** `provider` 為 anthropic、`kind` 為 claude-subscription、`agents` 只有 `claude-code`，形狀裡沒有任何環境變數名

### Requirement: 訂閱 id 是固定集合
`id` MUST 是 `lgwd/policy.py` 的 `SUBSCRIPTIONS` 常數裡的值（`claude-personal`、`claude-company`、`opencode-zen`、`opencode-go`、`groq`、`openrouter`、`ollama-cloud`，本 change 加 `antigravity`）。

#### Scenario: 未知的訂閱鍵
- **WHEN** 政策檔 `subscriptions` 有不在 `SUBSCRIPTIONS` 的鍵
- **THEN** 政策載入失敗（`PolicyError("policy.subscriptions has unknown key")`，`lgwd/policy.py:253-255`），lgwd 不啟動

### Requirement: 新增訂閱需兩處一致
政策檔 `subscriptions` 的鍵不在集合裡時 MUST 政策載入失敗、lgwd 不啟動，所以新增一個訂閱 MUST 改 `SUBSCRIPTIONS`（與 `AGENTS`、`PROVIDERS` 對應的一筆）加上政策檔兩處；兩邊不一致時是「載不起來」而不是「靜默忽略」。

#### Scenario: 只改政策檔不改 SUBSCRIPTIONS
- **WHEN** 政策檔新增一個 `SUBSCRIPTIONS` 之外的訂閱 id，而 `SUBSCRIPTIONS` 沒有改
- **THEN** 政策載入失敗，lgwd 不啟動，不會靜默忽略該訂閱

### Requirement: endpoint 不內嵌憑證
`endpoint` MUST NOT 內嵌憑證：`endpoint` 不在政策檔，是 `lgwd/providers.py` 的 `CredentialProvider.endpoint` 常數；providers 載入／lgwd 啟動時 MUST 逐筆檢查 `PROVIDERS` 的 endpoint 值，含 `key=`／`token=`／`api_key=`／`access_token=` query 樣式或 userinfo `@` 即拒絕啟動（`PolicyError`；endpoint 是中介資料、會進稽核與 meta——前提是不含憑證）。

#### Scenario: provider 表的 endpoint 內嵌憑證
- **WHEN** 假 provider 表（monkeypatch `PROVIDERS`）某筆 `CredentialProvider.endpoint` 值含 `key=`／`token=`／`api_key=`／`access_token=` 的 query 或 userinfo（`user:pass@`）樣式
- **THEN** providers 載入／lgwd 啟動失敗（`PolicyError`），lgwd 不啟動（endpoint 會進稽核與 meta——前提是不含憑證）

### Requirement: 共通 JSON 的 agents 條款
Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（條款；免費模型只經過 opencode）；`kind` 為 `local-login` 的訂閱（本 change 的 `antigravity`）的 `agents` MUST 只有 `agy`；政策載入 MUST 拒絕這三類訂閱的 `agents` 不是剛好那一個值的政策檔，也 MUST 拒絕其他訂閱的 `agents` 含 `agy`。

#### Scenario: 政策檔把 openrouter 的 agents 改寬
- **WHEN** 政策檔的 `openrouter.agents` 不是剛好 `["opencode"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動；resolve 不會把 Claude Code 指到 OpenRouter

#### Scenario: openrouter 只給 opencode
- **WHEN** 讀取 `openrouter` 的描述
- **THEN** `agents` 只有 `opencode`

### Requirement: schema 版本不因新增欄位而 bump
`schema` 是該端點回覆形狀的版本；list row 的 `schema` 與 resolve 的 `schema` 各自獨立。list 新增 `usage`／`available_again_at` 只是新增欄位，`schema` MUST NOT bump（目前仍為 1；消費端忽略不認得的欄位）。

#### Scenario: 新增欄位不 bump schema
- **WHEN** 呼叫 list
- **THEN** 回覆的 `schema` 仍為 1，既有欄位的名稱與語意不變

### Requirement: model_cooldowns 不 bump schema
`model_cooldowns` 也是新增欄位，同樣 MUST NOT bump `schema`；它出現在 list 的回覆，resolve 的 `meta` 也帶同一形狀的 `cooldown` 與 `model_cooldowns`（見 `resolve`「resolve」）。

#### Scenario: 模型冷卻不 bump schema
- **WHEN** 呼叫 list，某把有模型冷卻
- **THEN** 回覆的 `schema` 仍為 1，`cooldown` 仍是 `{"until"}` 或 null（沒有被塞進模型）

### Requirement: cooldown 與 available 仍是整把語意
`cooldown` 與 `available` 仍是**整把**的語意：`available` MUST NOT 因 `model_cooldowns` 改變（消費端用 `available` 判斷 key 有沒有部署，把模型冷卻算進去會讓一個模型用完看起來像整把沒部署）；`kind` 為 `local-login` 時「部署」由 kind 判定，不是 key。

#### Scenario: 模型冷卻不改 available
- **WHEN** 某把只有模型冷卻、整把沒有未來的 `until`、key 已部署
- **THEN** `available` 為 true、`cooldown` 為 null，`model_cooldowns` 有那一筆

#### Scenario: 沒有 key 的 local-login 不顯示未部署
- **WHEN** 呼叫 list，key 檔裡沒有 `antigravity` 的鍵
- **THEN** `antigravity` 的 `available` 為 true（沒有未來冷卻時），不因沒有 key 顯示未部署

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

### Requirement: 沒有憑證的訂閱（kind `local-login`）
`kind` 為 `local-login` 的 subscription SHALL 表示憑證由呼叫方那一台機器自己保管（本 change 只有 `antigravity`：agy 用 Mac 本機的 Google 登入，Gateway 不搬、不匯出、不代管）。

#### Scenario: 讀取 antigravity 的 kind
- **WHEN** 讀取 `antigravity` 的共通 JSON
- **THEN** `kind` 為 `local-login`，表示憑證在呼叫方那一台機器

### Requirement: local-login 不進 key 檔也不保管
這一類訂閱 MUST NOT 出現在 key 檔，MUST NOT 佔 `KEY_NAMES` 的鍵名；Gateway MUST NOT 為它讀、寫、保管或轉發任何憑證。

#### Scenario: get 沒有值可給
- **WHEN** 呼叫已移除的單筆路徑（`GET /v1/subscriptions/antigravity?agent=agy`）
- **THEN** 回 404 `not_found`，body 沒有 `key`

### Requirement: local-login 的已部署由 kind 判定
這一類訂閱的「已部署」MUST 以 kind 判定（恆已部署），MUST NOT 讀 key 檔：list 的 `available` 因此只受整把冷卻影響，resolve 的第 6 步 MUST NOT 排除它，`available_again_at` MUST 只由冷卻計算。

#### Scenario: list 不因沒有 key 顯示未部署
- **WHEN** key 檔裡沒有任何 `antigravity` 的鍵，呼叫 list
- **THEN** 該筆 `available` 為 true（沒有未來冷卻時）、`usage.status` 為 `not_applicable`、`available_again_at` 為 null，且沒有 `key`

### Requirement: local-login 的 env 與 endpoint
resolve 的 `env` MUST 是空物件 `{}`（見 `resolve-env`「env 的組成」）；模型只走 `meta.model`；`endpoint` MUST 為 null（見「endpoint 的語意」）。

#### Scenario: resolve 不讀 key
- **WHEN** 以 `agent=agy&provider=google` resolve `antigravity`，key 檔裡沒有它的鍵
- **THEN** 回 200，`env` 為 `{}`；Gateway 沒有對 key 檔做它的查詢（不因 `unknown subscription` 丟錯）

### Requirement: local-login 的 usage 與政策段
`usage` MUST 為 `not_applicable`（沒有取用量實作）；政策檔的 `usage` 段 MUST NOT 對它生效。

#### Scenario: 沒有憑證的訂閱也在 list
- **WHEN** 呼叫 list，政策檔含 `antigravity`
- **THEN** 該筆的 `kind` 為 `local-login`、`endpoint` 為 null、`agents` 為 `["agy"]`、`available` 為 true（沒有未來冷卻時）、`usage.status` 為 `not_applicable`、`available_again_at` 為 null，且沒有 `key`

### Requirement: local-login 的稽核
稽核紀錄 SHALL 照既有規則記 provider／model；沒有憑證值可記，也 MUST NOT 因此新增事件或欄位。

#### Scenario: 稽核沒有憑證
- **WHEN** 一次成功的 agy resolve 之後檢查稽核紀錄
- **THEN** 有 subscription、agent、provider、model，沒有任何憑證值；事件種類仍是 `resolve`，沒有新增欄位

### Requirement: local-login 不是 not_deployed 的錯誤
Gateway MUST NOT 把這類訂閱當成「key 未部署」而回 409 `not_deployed`（resolve）或 `available: false`（list）。

#### Scenario: local-login 不因 key 缺失而 available false
- **WHEN** key 檔沒有 `antigravity` 的鍵，且沒有未來冷卻，呼叫 list
- **THEN** `antigravity` 的 `available` 為 true，不因 key 缺失為 false

### Requirement: local-login 的 agents 條款
政策載入 MUST 拒絕這類訂閱的 `agents` 不是剛好 `["agy"]` 的政策檔（條款；見「共通 JSON 的 agents 條款」），也 MUST 拒絕其他訂閱的 `agents` 含 `agy`（否則 api-key 的 row 會用空 env 被選中）。

#### Scenario: 沒有憑證的訂閱 agents 改寬
- **WHEN** 政策檔的 `antigravity.agents` 不是剛好 `["agy"]`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

