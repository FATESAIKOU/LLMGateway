# subscription-registry Specification

## Purpose
LLMGateway 對外提供三個介面：list（列出 Provider-Subscription 菜單，共通 JSON schema，不含 key）、resolve（呼叫方給 agent、provider、可選的 model，由 Gateway 依政策挑把，回選定的 subscription 與開 agent 要用的環境變數 `env`）、登錄冷卻（收 `until` 與選填 `model`）。換哪一把由 resolve 依政策決定；LLMGateway 保管 key、列出菜單、記冷卻（只是提示）、照常挑把並附冷卻資訊。`get`（取得單一 subscription 含 key）是過渡期介面，由 LLMGateway#48 移除。

> 本檔 Scenario 中的 subscription 名稱、priority 與 models.default 是**示例政策**，用來說明選把規則，不是出廠政策；出廠政策以 `policy/default.json` 為準（2026-10-23 LLMGateway#50 起：沒有 claude-company，claude-personal 為 priority 40＋default `claude-sonnet-5-5`）。

> v5（2026-10-12）：冷卻 until-only（`登錄冷卻`、`稽核紀錄`、`錯誤回覆`、`共通 JSON schema` 以 until-only 語意為準）。
## Requirements
### Requirement: list
`GET /v1/subscriptions` SHALL 回所有 subscription 的共通 JSON，依 `priority` 排序，**不含 `key`**；每一筆多帶 `usage` 與 `available_again_at`（形狀與規則見 spec `usage-reporting`）；每一筆另帶 `model_cooldowns`（該把各模型的冷卻，只列還沒過的；形狀見「共通 JSON schema」）。list 只讀用量快取，並在快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 list MUST 仍回 200。

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

### Requirement: get
`GET /v1/subscriptions/{id}?agent=<種類>` SHALL 回該 subscription 的共通 JSON，含 `key`。`agent` 是必填參數，缺少時回 400 `bad_request`（帶 `missing: ["agent"]`）；不在該筆的 `agents` 裡 MUST 回 403 `agent_not_allowed`，不回 key。不存在的 id 回 404 `not_found`；key 沒部署時回 409 `not_deployed`。`kind` 為 `local-login` 的訂閱沒有憑證（本 change 的 `antigravity`）MUST 回 409 `not_deployed`、不回 `key`：它沒有可交付的值，MUST NOT 因為「恆已部署」（見「沒有憑證的訂閱」）而回 200。

#### Scenario: Claude Code 拿 Claude 訂閱
- **WHEN** 以 `agent=claude-code` get `claude-personal`
- **THEN** 回覆含 `key`

#### Scenario: opencode 不能拿 Claude 訂閱
- **WHEN** 以 `agent=opencode` get `claude-company`
- **THEN** 回 403 `agent_not_allowed`，不含 key

#### Scenario: 冷卻中仍可 get
- **WHEN** 某把在冷卻中，有人 get 它
- **THEN** 照樣回覆（`available` 為 false），由呼叫方決定要不要用

#### Scenario: 沒有憑證的訂閱不發值
- **WHEN** 以 `agent=agy` get `antigravity`
- **THEN** 回 409 `not_deployed`，body 只有 `error`，沒有 `key`

### Requirement: models.default 的語意

`models.default`（與 resolve 的 `model` 參數）SHALL 是**供應商自己的模型 id**（例 `mimo-v2.6-flash-free`、`openrouter/free`——可含 `/`），不帶 agent 專用的 provider 前綴；組成 agent 專用的字串（例 opencode 的 `<id>/<model>`）由 sidecar 在 `OPENCODE_CONFIG_CONTENT` 內組，`meta.model` 一律不帶前綴。resolve 的 `model` 參數不做前綴正規化：帶 `<provider>/<model>` 的值會照 `models.allow` 比對（通常 403 `model_not_allowed`）；sidecar 的 `POST /agents` 若收到前綴，要自己正規化或回 400。null 的語意依 `kind`（見「model 省略時的語意」）：`claude-subscription` 表示用 agent 自己的預設；其他 kind 表示沒有預設、服務不代選。

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
- **THEN** 服務不代選模型；呼叫方自己從 `allow` 選一個模型來用，或不用這一筆（經 resolve 時依「model 省略時的語意」推導）

### Requirement: models.default 必須被 allow 涵蓋
政策檔載入時 SHALL 檢查：`models.default` 不是 null 時，必須被同一筆 `models.allow` 的樣式涵蓋（fnmatch）；不符合 MUST 載入失敗（`PolicyError`），服務不啟動。`default` 為 null 時不做這項檢查。

#### Scenario: default 蓋不進 allow
- **WHEN** 政策檔某筆的 `models.default` 是 `muse-spark-1.3`，同一筆的 `models.allow` 只有 `*-free` 與 `big-pickle`
- **THEN** 載入失敗（`PolicyError`），訊息指出是哪一筆

### Requirement: endpoint 的語意
`endpoint` SHALL 是 provider 的**根位址**（值以 `lgwd/providers.py` 為準），`kind` 為 `claude-subscription` 或 `local-login` 時 MUST 為 null。出廠值與形狀：
- `ollama-cloud` 的 `https://ollama.com` 可以直接當 Claude Code 的 `ANTHROPIC_BASE_URL`；`openrouter` 的 `https://openrouter.ai/api` 同屬 Anthropic 相容的 base（本系統只給 opencode，接法由 opencode driver 決定）；兩者的 OpenAI 相容路徑由呼叫方自己組（例如再接 `/v1`）；
- `opencode-zen` 的 `https://opencode.ai/zen/v1`：OpenAI 相容的 base；
- `groq` 的 `https://api.groq.com/openai/v1`：OpenAI 相容的 base；
- `kind` 為 `local-login`（本 change 的 `antigravity`）：MUST 為 null。agy 用呼叫方那一台機器的本機登入直連上游，Gateway 不代理、不換端點。

呼叫方（agent driver）MUST NOT 假設 `endpoint` 是某種協定的完整路徑；要依 `provider` 組出自己要的接法，或使用 agent 內建的 provider 設定。

#### Scenario: opencode driver 拿到 ollama-cloud 的 endpoint
- **WHEN** opencode 的 driver 拿到 `ollama-cloud` 的 `endpoint`（`https://ollama.com`）
- **THEN** 它自己組 OpenAI 相容的路徑（例如再接 `/v1`），或不覆寫內建 provider 的 baseURL；服務回的 `endpoint` 不變

#### Scenario: Claude Code 拿到 ollama-cloud 的 endpoint
- **WHEN** Claude Code 的 driver 拿到 `ollama-cloud` 的 `endpoint`
- **THEN** 直接當 `ANTHROPIC_BASE_URL` 用（這個值是 Anthropic 相容的 base）

#### Scenario: Claude 訂閱沒有 endpoint
- **WHEN** 讀取 `claude-personal` 的共通 JSON
- **THEN** `endpoint` 為 null，agent 直連、不使用 base

#### Scenario: agy 沒有 endpoint
- **WHEN** 讀取 `antigravity` 的共通 JSON
- **THEN** `endpoint` 為 null、`kind` 為 `local-login`；Gateway 不回任何要交給 agy 的位址

### Requirement: 共通 JSON schema

每個 subscription SHALL 以同一個 JSON 形狀描述，欄位如下：
- `schema`：該端點回覆形狀的版本（整數）；
- `id`：subscription 的名稱；
- `provider`、`kind`：供應商，與型態（`claude-subscription`、api-key 類，或沒有憑證的 `local-login`）；
- `agents`：允許使用這一把的 agent 種類清單；
- `priority`：整數，**數字小的先**；
- `models`：`{allow, default}`，`allow` 是模型 id 樣式（fnmatch）清單，`default` 是供應商自己的模型 id 或 null（語意見「models.default 的語意」）；
- `endpoint`：provider 的根位址，或 null（語意見「endpoint 的語意」）；
- `key`：憑證值，**只在 get 的回覆出現**（過渡期，由 LLMGateway#48 移除）；list 不含；
- `cooldown`：`{"until": RFC 3339 UTC}` 或 null，不分窗口；過了 `until` 即視為 null（整把的冷卻；各模型的冷卻另見 `model_cooldowns`）；
- `model_cooldowns`：物件（模型 id → `{"until": RFC 3339 UTC}`），**只列 `until` 還沒過的**（`until` 在過去或以現在為界即不出現）；沒有時是 `{}`；模型 id 可含 `/`、`:` 等字元；
- `available`：已部署（key 在；`kind` 為 `local-login` 時恆已部署）而且沒有未來的整把 `until` 時為 true，否則 false；用量為 0% MUST NOT 讓它變 false；模型冷卻（`model_cooldowns`）MUST NOT 讓它變 false；它是「部署＋整把冷卻」的粗提示，不是「保證不會被上游 429」；
- `available_again_at`：RFC 3339 UTC 或 null，**只在 list 出現**（合併規則見 spec `usage-reporting`）；
- `usage`：用量物件，**只在 list 出現**（resolve 的 `meta` 另帶同形狀的 `usage`，見「resolve」；形狀與規則見 spec `usage-reporting`）。

**訂閱 id 是固定集合**：`id` MUST 是 `lgwd/policy.py` 的 `SUBSCRIPTIONS` 常數裡的值（`claude-personal`、`claude-company`、`opencode-zen`、`opencode-go`、`groq`、`openrouter`、`ollama-cloud`，本 change 加 `antigravity`）。政策檔 `subscriptions` 的鍵不在這個集合裡時 MUST 政策載入失敗（`PolicyError("policy.subscriptions has unknown key")`，`lgwd/policy.py:253-255`）、lgwd 不啟動；所以新增一個訂閱＝改 `SUBSCRIPTIONS`（與 `AGENTS`、`PROVIDERS` 對應的一筆）＋政策檔兩處，兩邊不一致時是「載不起來」而不是「靜默忽略」。

這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 provider＋model 用起來的環境變數由 resolve 的回覆 `env` 給（「env 的組成」），list 只是菜單。`usage`／`available_again_at` 在 `list_subscriptions` 內逐筆合入，get 的回覆 MUST NOT 帶（get 由 LLMGateway#48 退場，不續命）。Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（條款；免費模型只經過 opencode）；`kind` 為 `local-login` 的訂閱（本 change 的 `antigravity`）的 `agents` MUST 只有 `agy`；政策載入 MUST 拒絕這三類訂閱的 `agents` 不是剛好那一個值的政策檔，也 MUST 拒絕其他訂閱的 `agents` 含 `agy`。`endpoint` MUST NOT 內嵌憑證：`endpoint` 不在政策檔，是 `lgwd/providers.py` 的 `CredentialProvider.endpoint` 常數；providers 載入／lgwd 啟動時 MUST 逐筆檢查 `PROVIDERS` 的 endpoint 值，含 `key=`／`token=`／`api_key=`／`access_token=` query 樣式或 userinfo `@` 即拒絕啟動（`PolicyError`；endpoint 是中介資料、會進稽核與 meta——前提是不含憑證）。`schema` 是該端點回覆形狀的版本；list row 的 `schema` 與 resolve 的 `schema` 各自獨立。list 新增 `usage`／`available_again_at` 只是新增欄位，`schema` MUST NOT bump（目前仍為 1；消費端忽略不認得的欄位）。`model_cooldowns` 也是新增欄位，同樣 MUST NOT bump `schema`；它同時出現在 list 與 get 的回覆（`usage`／`available_again_at` 仍只在 list），resolve 的 `meta` 帶同一形狀的 `cooldown` 與 `model_cooldowns`（見「resolve」）。`cooldown` 與 `available` 仍是**整把**的語意：`available` MUST NOT 因 `model_cooldowns` 改變（消費端用 `available` 判斷 key 有沒有部署，把模型冷卻算進去會讓一個模型用完看起來像整把沒部署）；`kind` 為 `local-login` 時「部署」由 kind 判定，不是 key。

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
- **THEN** 回覆沒有 `usage`、沒有 `available_again_at`；`model_cooldowns` 照樣出現（與 list 同一個形狀）

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

### Requirement: 登錄冷卻
`POST /v1/subscriptions/{id}/cooldown {until[, model]}` SHALL 登錄冷卻：
- 成功回 204（無 body）；
- `until` 為必填的 RFC 3339 UTC 字串（何時能再用）；缺少回 400 `bad_request`（`missing` 含 `until`）；
- `until` 不是 RFC 3339 字串（或型別不對）時回 400 `invalid_until`，不登錄；
- `until` 在過去或超過 8 天以後 MUST 回 400 `until_out_of_range`，不登錄（模型冷卻用同一個上限）；
- `model` 選填：有給時 MUST 只冷卻那個模型，沒有時冷卻整把（現行語意）；**`"model": null` MUST 視為沒給**（記整把），與空字串／數字／陣列不是同一類（後三者是「有給但形狀錯」）；
- `model` 有給（且不是 null）時 MUST 是非空字串，而且是供應商自己的模型 id（不帶 agent 專用前綴，語意同 resolve 的 `model`）；不是非空字串、長度超過 **200 字元**、以 `<agent>/` 開頭（agent ∈ `AGENTS`，即 `opencode/`、`claude-code/`、`agy/`；這個形狀 MUST 被擋，因為 allow 的 `*` 會匹配 `/`，只靠 allow 涵蓋檢查擋不到）、含 fnmatch 萬用字元（`*`、`?`、`[`），或不被該把 `models.allow` 的樣式（fnmatch）涵蓋，MUST 回 400 `bad_request`（**不是** 403 `model_not_allowed`——那是 resolve 的政策拒絕碼），不登錄；Gateway MUST NOT 正規化 `model`（不去前綴、不改大小寫）；
- 驗證順序 MUST 與現行程式一致、不重排：未知 subscription id → 404 `not_found`，**先於** `until`／`model` 的 400；缺 `until` → 400 `bad_request`；`until` 格式／範圍 → 400；最後才是 `model`（null 除外，見上）→ 400。**body 解析失敗、body 不是物件、超過 1 MB 的 413 `body_too_large`，仍由 HTTP 層先回**（它們不是這個端點的 service 層驗證，所以「404 先於 400」不涵蓋它們）。這個順序與「錯誤回覆」的通則（輸入形狀 400 先於查無 404）不同，是刻意沿用現況（404 是現行程式的第一道檢查）；
- 同一（把、模型）重複登錄時取較晚的 `until`；整把與模型兩層獨立，登錄其中一層 MUST NOT 刪除或縮短另一層既有的冷卻；
- 未知欄位 MUST 忽略；
- 過了 `until` 自動解除，不需要另一個動作（整把與每個模型各自解除）；
- 呼叫方拿不到恢復時間時 SHALL 以呼叫當下＋1 小時為 `until`（約定；`lgw.signals` 只負責把判讀結果轉成這個形狀）；
- 冷卻只是提示：登錄成功 MUST NOT 讓之後的 resolve 排除這把或這個模型（見「resolve 的選把規則」）；本端點不提供提前解除。`kind` 為 `local-login` 的訂閱也可以用（它沒有用量來源，冷卻常是唯一的訊號）。

#### Scenario: 帶恢復時刻
- **WHEN** 以 `until=15:00` 登錄 `claude-personal`
- **THEN** 它的冷卻到 15:00，list 顯示 `available` 為 false

#### Scenario: 不合理的時刻
- **WHEN** `until` 是過去的時刻
- **THEN** 回 400 `until_out_of_range`，冷卻不變

#### Scenario: 判讀不出恢復時間
- **WHEN** 呼叫方判讀不出恢復時間
- **THEN** 以呼叫當下＋1 小時為 `until` 登錄

#### Scenario: 只冷卻一個模型
- **WHEN** 以 `{"until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"}` 登錄 `opencode-zen`
- **THEN** 回 204；`list` 的 `cooldown` 為 null、`model_cooldowns` 有 `mimo-v2.6-flash-free`；以 `model=big-pickle` resolve 同一把回 200，以同一個模型 resolve 也回 200（附冷卻資訊）

#### Scenario: 同一模型重複登錄取較晚的
- **WHEN** 同一（把、模型）先登錄 15:00、再登錄 14:00、再登錄 16:00
- **THEN** 第二次（較早）不覆蓋、第三次（較晚）覆蓋；稽核記的有效 `until` 依序是 15:00、15:00、16:00

#### Scenario: 兩個模型的冷卻互不影響
- **WHEN** 同一把先登錄模型 A 到 15:00，再登錄模型 B 到 16:00
- **THEN** 兩筆都在 `model_cooldowns`，登錄 B 不改 A 的 `until`

#### Scenario: 整把與模型並存
- **WHEN** 某把已有模型 A 的冷卻，再登錄整把的冷卻
- **THEN** 兩層都在（`cooldown` 與 `model_cooldowns` 的 A）；登錄整把 MUST NOT 刪掉模型 A 的列

#### Scenario: model 不在 allow
- **WHEN** 以 `model=muse-spark-1.3` 登錄 `opencode-zen`（只 allow `*-free` 與 `big-pickle`）
- **THEN** 回 400 `bad_request`（不是 403），冷卻不變

#### Scenario: model 帶 agent 前綴
- **WHEN** 以 `model=opencode/big-pickle` 登錄 `opencode-zen`
- **THEN** 回 400 `bad_request`（Gateway 不正規化前綴，與 resolve 同一條規則）

#### Scenario: model 帶 agy 前綴
- **WHEN** 以 `model=agy/gemini-3.8-flash-low` 登錄 `antigravity`（`allow` 只有該裸 id）
- **THEN** 回 400 `bad_request`，冷卻不變——`agy/` 由前綴規則擋下，不只看 allow 涵蓋

#### Scenario: model 帶前綴而且被 allow 樣式涵蓋
- **WHEN** 以 `model=opencode/glm-5.3-flash-free` 登錄 `opencode-zen`（allow 是 `["*-free", "big-pickle"]`，而 fnmatch 的 `*` 會匹配 `/`，所以它「被涵蓋」）
- **THEN** 回 400 `bad_request`，冷卻不變——前綴規則必須自己擋下這一種，allow 涵蓋檢查擋不到

#### Scenario: model 太長
- **WHEN** 以 201 個字元的 `model` 登錄 `claude-personal`（allow 是 `["*"]`），以及以 200 個字元的 `model` 登錄同一把
- **THEN** 前者回 400 `bad_request`、冷卻不變；後者回 204

#### Scenario: model 大小寫不符
- **WHEN** 以 `model=Big-Pickle` 登錄 `opencode-zen`（allow 的樣式是 `big-pickle`，`_covered` 用大小寫敏感的 fnmatch）
- **THEN** 回 400 `bad_request`，冷卻不變（Gateway 不做大小寫正規化）

#### Scenario: model 是 null
- **WHEN** 以 `{"until": "2026-10-11T15:00:00Z", "model": null}` 登錄 `opencode-zen`
- **THEN** 回 204，且記的是整把冷卻（`cooldown` 有值、`model_cooldowns` 為 `{}`）——`null` 等於沒給

#### Scenario: 未知的 subscription 與壞的 model
- **WHEN** 以 `model=opencode/big-pickle`（形狀會 400）登錄一個不存在的 subscription id
- **THEN** 回 404 `not_found`（查無先於形狀錯誤，與現行程式一致）

#### Scenario: 壞的 until 與壞的 model
- **WHEN** 以不是 RFC 3339 的 `until` 搭配 `model=opencode/big-pickle` 登錄一個存在的 subscription
- **THEN** 回 400 `invalid_until`（`until` 的驗證先於 `model`），冷卻不變

#### Scenario: model 是萬用字元
- **WHEN** 以 `model=*-free` 登錄 `opencode-zen`
- **THEN** 回 400 `bad_request`，冷卻不變（它不是供應商 id，存進去永遠命不中）

#### Scenario: model 形狀不對
- **WHEN** body 的 `model` 是空字串、數字或陣列
- **THEN** 回 400 `bad_request`，冷卻不變

#### Scenario: 冷卻不擋下一次 resolve
- **WHEN** 先以 `model=big-pickle` 登錄 `opencode-zen` 的冷卻，再以同一個模型 resolve
- **THEN** 回 200，`env` 照常，`meta.model_cooldowns` 有那一筆（冷卻只記錄、不拒絕）

### Requirement: 稽核紀錄

每次**成功**（200）的 resolve、每次成功的 get（過渡期）與每次登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類（resolve／get）、provider 與 model（resolve；登錄冷卻記 `model`，沒給時為 null）、結束時刻 `until`（登錄冷卻記有效的 `until`）。紀錄 MUST NOT 含憑證的值，也 MUST NOT 含 `env` 的憑證值；provider／model 是中介資料，SHALL 記（否則「誰用了哪個模型」就沒有紀錄）。失敗的回覆（4xx／409）不記稽核（沒有選中的 subscription 可記，get 與 resolve 皆如此），診斷靠只含方法／路徑／狀態碼的 HTTP log。不做認證，紀錄不分是哪個客戶端。

#### Scenario: resolve 留下紀錄
- **WHEN** 有客戶端以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 稽核紀錄有一筆（subscription、agent、provider、model），內容沒有憑證值或 env 的憑證值

#### Scenario: get 留下紀錄
- **WHEN** 有客戶端以 `agent=claude-code` get `claude-personal`（過渡期，get 退場前，LLMGateway#48）
- **THEN** 稽核紀錄有一筆，內容沒有 key 的值

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
- 403 `agent_not_allowed`：`agent` 不在該筆的 `agents` 裡（get 與 resolve 的選把步驟 3 共用）；
- 403 `model_not_allowed`：resolve 的 model 不被任何候選的 `models.allow` 涵蓋；
- 403 `privacy_not_allowed`：resolve 宣告的 `data` 高於所有候選的 `privacy`（步驟 3.5；`data` 不在三個等級是 400，不是這個碼）；
- 404 `not_found`：未知路徑，或不存在的 subscription id（resolve 的未知 provider／未知 `subscription` 同用）；
- 409 `not_deployed`：key 尚未部署；`kind` 為 `local-login` 的訂閱沒有憑證，get 一律回這個碼（resolve 不會因它回 409）；
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
- **THEN** 回 200（不是 409 `not_deployed`）；只有 get 對它回 409

### Requirement: resolve

`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]][&data=<public|general|real>]` SHALL 回 `{schema, env_schema, meta, env}`。

`agent` 與 `provider` 必填；缺少時回 400 `bad_request`（`missing` 列出缺的欄位）。`agent` MUST 是 `claude-code`、`opencode` 或 `agy`，否則 400 `bad_request`（不是 403 `agent_not_allowed`）。`model` 選填，是**供應商自己的模型 id**（可含 `/`、`+`、`:` 等字元）。`subscription` 選填，是釘住某一把 subscription：有給時 MUST 與該把的 `provider` 相同，不一致回 400 `bad_request`；未知 id 回 404 `not_found`。`exclude` 選填，是逗號分隔的 subscription id 清單；不在政策檔的 id SHALL 忽略。`subscription` 與 `exclude` 同時給 MUST 回 400 `bad_request`。`data` 選填，是呼叫方**宣告的資料等級**：有給時 MUST 是 `public`、`general`、`real` 之一，否則 400 `bad_request`（`data` 不是資源，不是 404；等級夠不夠是政策拒絕的 403，見「resolve 的選把規則」）。**沒給 `data` 時 Gateway MUST NOT 依 `privacy` 過濾**（缺省不濾：不帶＝不擋，與這個參數加入前相同；宣告了才一定不降級，翻成缺省擋留給後續的 d）。未知 query 參數 SHALL 忽略；空值視為沒給（`subscription=x&exclude=` 因此是合法釘住），同一參數重複出現回 400 `bad_request`。所有 query 值由呼叫端 percent-encode（`+` 編成 `%2B`），server 以 form 解碼。

`meta` SHALL 含 `subscription`（選中的 subscription id）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`）、`endpoint`、`priority`、`data`（請求的資料等級**原樣**，沒給時 null）、`cooldown`（該把整把的冷卻：`{"until": RFC 3339 UTC}` 或 null，過了 `until` 即視為 null）、`model_cooldowns`（該把各模型的冷卻：物件，模型 id → `{"until": RFC 3339 UTC}`，只列還沒過的，沒有時 `{}`；形狀與 list 的同一欄位相同）。`meta.data` 是「這次宣告了什麼」，**不是生效等級**：resolve 沒有 role／floor，所以它不會被抬高，與 `roles` 回覆的 `data`（請求值與 floor 取較嚴）語意不同。`meta` 另含 `usage`（與 list 同形狀；只讀快取，快取過期時觸發一次背景取用量，回覆不等它；用量讀不到或取用量壞掉時 resolve MUST 仍照常回 200，`env` 與選把結果不受影響）。冷卻資訊是**提示**：resolve 的 200 不代表「沒有冷卻」；呼叫方要判斷「這次的呼叫會不會撞到冷卻」時，看 `meta.cooldown.until` 與（`meta.model` 非 null 時）`meta.model_cooldowns[meta.model].until`，兩者取較晚者。`meta` MUST NOT 含 `available`／`available_again_at`（200 已代表部署完成：key 在，或 `kind` 為 `local-login` 而恆已部署；`available` 恆為 true、不帶資訊；用量窗口的時刻在 `usage`）。resolve 回非 200 時不組裝 `usage`，也不觸發取用量。

`env` SHALL 是把選定的 provider＋model 用起來的必要環境變數（名稱→值），依 `agent` 組成（見「env 的組成」）。`agent` 為 `agy` 時 `env` MUST 是空物件 `{}`：它用自己的本機登入，Gateway 不給憑證也不給模型變數，模型只走 `meta.model`。`env` 的**憑證值** MUST NOT 出現在 list 的回覆、稽核紀錄、log 或 `GET /agents` status；model、provider、endpoint 是中介資料，稽核可記——前提是 **`endpoint` MUST NOT 內嵌憑證**（query 的 `key=`／`token=`／`api_key=`／`access_token=`、userinfo 的 `@`；providers 載入／啟動時拒絕，見「共通 JSON schema」）。錯誤回覆 MUST NOT 含 `env`。resolve 的 200 回覆 SHALL 帶 `Cache-Control: no-store` 與 `Pragma: no-cache`（過渡期的 get 亦同）。

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

### Requirement: resolve 的選把規則

resolve SHALL 依序挑 subscription，並依斷在哪一步回錯誤：

1. 有 `subscription`（釘住）時，候選**先限縮為這一把**，後續每一步只在這一把上判斷，不代換另一把；此步不套 `exclude`（同時給已在參數驗證回 400 `bad_request`）。
2. `provider` 不存在，或該 provider 沒有任何 subscription → 404 `not_found`。
3. 候選＝`provider` 相同、`agents` 含請求的 agent；無一允許 → 403 `agent_not_allowed`。
3.5 有給 `data` 時，候選限於 `privacy` **不低於** `data` 的（`public` < `general` < `real`）；一個都不剩 → 403 `privacy_not_allowed`。沒給 `data` 時此步不作用（完全不過濾）。此步排在 3 與 4 之間（編號記 3.5，4～7 不變，與程式註解同一套編號）。
4. `model` 有給時，候選限於被 `models.allow` 的樣式（fnmatch）涵蓋的；無一涵蓋 → 403 `model_not_allowed`。`model` 省略時，候選限於**能推導模型**的（kind 為 `claude-subscription`，或 `models.default` 非 null，或 `allow` 至少一個不含萬用字元的樣式）；無一能推導 → 400 `no_model`（釘住時就是該把——同 provider 另一把可推導也不代換）。`claude-subscription`＋default null 算能推導（用 agent 預設、合法 200），所以 `no_model` 只落在非 `claude-subscription` 且 default null、`allow` 全萬用字元的形狀（現行政策沒有這種 provider；單元測試用假政策）。
5. 沒有釘住時，套 `exclude`（不在政策檔的 id 忽略）；沒有候選 → 409 `no_candidate`。
6. 排除未部署的候選：`kind` 為 `local-login` 的訂閱恆算已部署（不讀 key 檔），其餘仍看 key 是否部署；全部未部署 → 409 `not_deployed`。
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

#### Scenario: 沒有 key 也能被選中
- **WHEN** `antigravity` 在 key 檔裡沒有任何鍵、沒有冷卻，以 `agent=agy&provider=google` resolve
- **THEN** 回 200 並選中它（kind `local-login` 恆算已部署），不是 409 `not_deployed`

#### Scenario: 只給 claude-code 的 provider 不影響 agy
- **WHEN** 以 `agent=agy&provider=anthropic` resolve
- **THEN** 回 403 `agent_not_allowed`（provider 存在，但沒有候選允許 `agy`）

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

`kind` 為 `local-login` 的訂閱（本 change 的 `antigravity`）**沒有 env**：`env` MUST 是**空物件 `{}`**——沒有任何鍵，也就 MUST NOT 出現 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`／`OPENCODE_*`。agy 用它自己在那台機器的本機登入，Gateway 不給憑證、不給端點、不給模型變數；模型只走 `meta.model`（sidecar 的 agy driver 只從那裡拿）。實作 MUST 同時寫兩條判斷——`agent` 為 `agy` **或** 該把的 `kind` 為 `local-login`，任一成立就回 `{}`，防線不依賴單一條件；兩條各擋一個方向：

- 只判 `agent`：擋的是「agy 進了某一筆 api-key row」的情形（政策條款被改弱，該把的 `kind` 不是 `local-login`）——沒有這一條就會落進 claude-code 分支、回出 `ANTHROPIC_AUTH_TOKEN`（該把的 key）與 `ANTHROPIC_API_KEY: ""` 佔位；
- 只判 `kind`：擋的是「`antigravity` 的 `agents` 被改弱成含 `claude-code`」的情形（政策條款被改弱，該把仍是 `local-login` 但 agent 不是 `agy`）——沒有這一條就會以 `agent=claude-code` 落進 claude-code 分支，回出 `ANTHROPIC_AUTH_TOKEN`（該把沒有 key，值是 null）與 `ANTHROPIC_API_KEY: ""` 佔位。

`kind` 是 provider 常數、不是政策資料，所以第二條不隨政策檔改變；`agent` 那條則不隨 provider 表改變——兩條合起來，任一邊被改弱都還是回 `{}`。回 `{}` 是「因 agent／kind 而少給鍵」，**不改鍵集、`env_schema` 維持 1**（見「resolve」）。

opencode 只回**片段**（上述 `OPENCODE_*` 四鍵）：Gateway MUST NOT 回 `OPENCODE_CONFIG_CONTENT`，也不回 `baseURL`（`endpoint` 是 provider 根位址，依 #28 不抄進 opencode 的 `options.baseURL`）。model 為 null（agent 自己的預設）時 SHALL 不給模型變數。

**未知鍵與消費端規則**：`env` MUST 只含上述鍵（Gateway 在同一個 `env_schema` 內不得改鍵集）；sidecar 對未知鍵、overlay、`OPENCODE_CONFIG_CONTENT` 的組成、`permission`／`instructions` 的擁有、繼承環境不外漏等**消費端義務**見 `archive/2026-10-24-agent-provider-model-interface/design.md` 決定 2 與決定 6，由 MyLinuxPool 的 sidecar 規格承接（本 spec 只定 Gateway 能實作與驗證的部分）。

#### Scenario: claude 訂閱不帶模型變數
- **WHEN** resolve 選中 `claude-personal`（claude-subscription）而 `meta.model` 為 null
- **THEN** `env` 只有 `CLAUDE_CODE_OAUTH_TOKEN`，沒有 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`

#### Scenario: opencode 的兩個模型（片段）
- **WHEN** 同一把 `opencode-zen` 先後 resolve `mimo-v2.6-flash-free` 與 `big-pickle`
- **THEN** 兩次 `env` 的 `OPENCODE_PROVIDER_ID` 都是 `opencode`、`OPENCODE_API_KEY` 相同，`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 各自是該模型；`env` 不含 `OPENCODE_CONFIG_CONTENT`

#### Scenario: agy 的 env 是空物件
- **WHEN** 以 `agent=agy&provider=google&model=<該把的裸 id>` resolve，選中 `antigravity`（kind `local-login`）
- **THEN** `env` 是 `{}`：沒有 `ANTHROPIC_*`（含 `ANTHROPIC_API_KEY` 的空字串佔位）、沒有 `OPENCODE_*`、沒有 `CLAUDE_CODE_OAUTH_TOKEN`；`env_schema` 仍是 1

#### Scenario: 條款被改弱也不會回 claude 的 env
- **WHEN** 單元測試直接以一筆 api-key provider（例如 `opencode-zen`）呼叫 `build_env("agy", <key>, <model>)`——模擬「`_AGY_ONLY` 條款被改弱、agy 進了 api-key row」的組合（經政策載入不可達，所以這裡測的是單元層的第二道防線）
- **THEN** `env` 仍是 `{}`、不回任何 `ANTHROPIC_*`／`OPENCODE_*`：`kind` 不是 `local-login`，但 `agent` 為 `agy` 那條判斷成立

#### Scenario: 條款被改弱成含 claude-code 也不會回 claude 的 env
- **WHEN** 單元測試直接以一個 `kind` 為 `local-login` 的 provider（`GoogleProvider`）呼叫 `build_env("claude-code", null, <model>)`——模擬「`_AGY_ONLY` 條款被改弱、`antigravity` 的 `agents` 含 `claude-code`」的組合（經政策載入不可達，所以這裡測的是單元層的第二道防線）
- **THEN** `env` 仍是 `{}`、不回任何 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`／`OPENCODE_*`：`agent` 不是 `agy`，但 `kind` 為 `local-login` 那條判斷成立

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

### Requirement: 沒有憑證的訂閱（kind `local-login`）

`kind` 為 `local-login` 的 subscription SHALL 表示憑證由呼叫方那一台機器自己保管（本 change 只有 `antigravity`：agy 用 Mac 本機的 Google 登入，Gateway 不搬、不匯出、不代管）。這一類訂閱：

- MUST NOT 出現在 key 檔，MUST NOT 佔 `KEY_NAMES` 的鍵名；Gateway MUST NOT 為它讀、寫、保管或轉發任何憑證；
- 「已部署」MUST 以 kind 判定（恆已部署），MUST NOT 讀 key 檔：list 的 `available` 因此只受整把冷卻影響，resolve 的第 6 步 MUST NOT 排除它，`available_again_at` MUST 只由冷卻計算；
- resolve 的 `env` MUST 是空物件 `{}`（見「env 的組成」）；模型只走 `meta.model`；
- `get` MUST 回 409 `not_deployed`，不回 `key`（見「get」）；
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
- **WHEN** 以 `agent=agy` get `antigravity`
- **THEN** 回 409 `not_deployed`，body 沒有 `key`

#### Scenario: 稽核沒有憑證
- **WHEN** 一次成功的 agy resolve 之後檢查稽核紀錄
- **THEN** 有 subscription、agent、provider、model，沒有任何憑證值；事件種類仍是 `resolve`，沒有新增欄位

#### Scenario: 冷卻仍可用
- **WHEN** 對 `antigravity` 登錄整把冷卻到未來
- **THEN** 回 204；list 的 `available` 為 false、`cooldown.until` 照實，`available_again_at` 為該時刻；resolve 仍回 200 並附冷卻資訊（冷卻不擋選把）

