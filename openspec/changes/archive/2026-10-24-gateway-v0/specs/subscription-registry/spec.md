## Purpose

LLMGateway 對外只提供三個介面：列出 Provider-Subscription、取得一個 subscription 的詳細（共通 JSON schema，sidecar 直接拿來開 agent）、登錄冷卻（只收何時能再用 `until`）。換哪一把由 sidecar 自己挑；LLMGateway 只保管、列出、記冷卻。

> v5（2026-10-12）：冷卻 until-only。`共通 JSON schema`、`登錄冷卻`、`稽核紀錄`、`錯誤回覆`四條以 v5 全文寫在下面的 ADDED（主規格原本沒有 subscription-registry，歸檔時只能 ADDED，故從 MODIFIED 改過來）。

## ADDED Requirements

### Requirement: list
`GET /v1/subscriptions` SHALL 回所有 subscription 的共通 JSON，依 `priority` 排序，**不含 `key`**。

#### Scenario: 不含秘密
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都沒有 `key` 欄位，回覆裡找不到任何 key 的值

#### Scenario: 冷卻反映在 list
- **WHEN** `claude-personal` 冷卻到 15:00（`cooldown.until` 為 15:00）
- **THEN** 15:00 之前 list 裡它的 `available` 為 false；15:00 之後 `available` 回到 true

### Requirement: get
`GET /v1/subscriptions/{id}?agent=<種類>` SHALL 回該 subscription 的共通 JSON，含 `key`。`agent` 是必填參數，缺少時回 400 `bad_request`（帶 `missing: ["agent"]`）；不在該筆的 `agents` 裡 MUST 回 403 `agent_not_allowed`，不回 key。不存在的 id 回 404 `not_found`；key 沒部署時回 409 `not_deployed`。

#### Scenario: Claude Code 拿 Claude 訂閱
- **WHEN** 以 `agent=claude-code` get `claude-personal`
- **THEN** 回覆含 `key`

#### Scenario: opencode 不能拿 Claude 訂閱
- **WHEN** 以 `agent=opencode` get `claude-company`
- **THEN** 回 403 `agent_not_allowed`，不含 key

#### Scenario: 冷卻中仍可 get
- **WHEN** 某把在冷卻中，有人 get 它
- **THEN** 照樣回覆（`available` 為 false），由呼叫方決定要不要用

### Requirement: models.default 的語意
`models.default` SHALL 是**供應商自己的模型 id**，也就是 provider 端 API 認得的名字（例如 `openrouter/free`、`mimo-v2.6-flash-free`、`deepseek-v4.1-flash`），也可以是 null。這個欄位 MUST NOT 帶 agent 專用的 provider 前綴（例如 opencode 的 `opencode/`）；要組成 agent 專用的字串（例如 opencode 的 `provider/model`）時，由呼叫方的 agent driver 負責。null 的語意依 `kind` 而定：`kind` 為 `claude-subscription` 時表示用 agent 自己的預設模型（呼叫方不指定模型）；其他 `kind` 為 null 時表示這筆沒有預設，服務不代選，呼叫方 SHALL 自己依 `models.allow` 選一個模型，或不用這一筆。

#### Scenario: 供應商自己的模型 id
- **WHEN** 讀取 `opencode-zen` 的共通 JSON
- **THEN** `models.default` 為 `mimo-v2.6-flash-free`（不帶 `opencode/` 前綴；opencode 用的 `opencode/<模型>` 由呼叫方的 agent driver 自己組成）

#### Scenario: Claude 訂閱沒有預設
- **WHEN** 讀取 `claude-personal` 的共通 JSON
- **THEN** `models.default` 為 null，呼叫方不帶模型參數，用 Claude Code 自己的預設模型

#### Scenario: 其他 kind 沒有預設
- **WHEN** 某筆非 `claude-subscription` 的 `models.default` 為 null
- **THEN** 服務不代選模型；呼叫方自己從 `allow` 選一個模型來用，或不用這一筆

### Requirement: models.default 必須被 allow 涵蓋
政策檔載入時 SHALL 檢查：`models.default` 不是 null 時，必須被同一筆 `models.allow` 的樣式涵蓋（fnmatch）；不符合 MUST 載入失敗（`PolicyError`），服務不啟動。`default` 為 null 時不做這項檢查。

#### Scenario: default 蓋不進 allow
- **WHEN** 政策檔某筆的 `models.default` 是 `muse-spark-1.3`，同一筆的 `models.allow` 只有 `*-free` 與 `big-pickle`
- **THEN** 載入失敗（`PolicyError`），訊息指出是哪一筆

### Requirement: endpoint 的語意
`endpoint` SHALL 是 provider 的**根位址**（值以 `lgwd/providers.py` 為準），`kind` 為 `claude-subscription` 時 MUST 為 null（由 agent 直連）。出廠值與形狀：
- `ollama-cloud` 的 `https://ollama.com` 可以直接當 Claude Code 的 `ANTHROPIC_BASE_URL`；`openrouter` 的 `https://openrouter.ai/api` 同屬 Anthropic 相容的 base（本系統只給 opencode，接法由 opencode driver 決定）；兩者的 OpenAI 相容路徑由呼叫方自己組（例如再接 `/v1`）；
- `opencode-zen` 的 `https://opencode.ai/zen/v1`：OpenAI 相容的 base。

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

### Requirement: 共通 JSON schema
每個 subscription SHALL 以同一個 JSON 形狀描述，欄位如下：
- `schema`（版本，v0 為 1）、`id`、`provider`、`kind`；
- `agents`：可以用這把的 agent 種類，`claude-code` 或 `opencode`；
- `priority`：預設挑選順序，數字小的先；
- `models`：`allow` 與 `default`；
- `endpoint`（可為 null）；
- `key`：只在 get 回；
- `cooldown`：`{"until": RFC 3339 UTC}` 或 null（v5：單一，不再分窗口）；
- `available`：key 已部署，而且沒有未來的 `until`。

這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 key 變成哪些環境變數由 sidecar 的 agent driver 決定。Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（Claude Code 只用 Claude 訂閱與 Ollama，免費模型只經過 opencode 使用；Claude Code 碰到 `openrouter/free` 會 unrecognized_model）。

#### Scenario: Claude 訂閱的描述
- **WHEN** 讀取 `claude-personal` 的描述
- **THEN** `provider` 為 anthropic、`kind` 為 claude-subscription、`agents` 只有 `claude-code`，形狀裡沒有任何環境變數名

#### Scenario: openrouter 只給 opencode
- **WHEN** 以 `agent=claude-code` get `openrouter`
- **THEN** 回 403 `agent_not_allowed`，不含 key

#### Scenario: 能不能用
- **WHEN** 某把的 key 已部署，而且沒有未來的 `until`
- **THEN** `available` 為 true；有未來的 `until` 或 key 沒部署時為 false

### Requirement: 登錄冷卻
`POST /v1/subscriptions/{id}/cooldown {until}` SHALL 登錄冷卻：
- `until` 為必填的 RFC 3339 UTC 字串（何時能再用）；缺少回 400 `bad_request`（`missing` 含 `until`）；
- `until` 不是 RFC 3339 字串（或型別不對）時回 400 `invalid_until`，不登錄；
- `until` 在過去或超過 8 天以後 MUST 回 400 `until_out_of_range`，不登錄；
- 未知欄位 MUST 忽略；
- 重複登錄時取較晚的 `until`；
- 過了 `until` 自動解除，不需要另一個動作。
- 呼叫方拿不到恢復時間時 SHALL 以呼叫當下＋1 小時為 `until`（約定；`lgw.signals` 只負責把判讀結果轉成這個形狀）。

#### Scenario: 帶恢復時刻
- **WHEN** 以 `until=15:00` 登錄 `claude-personal`
- **THEN** 它的冷卻到 15:00，list 顯示 `available` 為 false

#### Scenario: 不合理的時刻
- **WHEN** `until` 是過去的時刻
- **THEN** 回 400 `until_out_of_range`，冷卻不變

#### Scenario: 判讀不出恢復時間
- **WHEN** 呼叫方判讀不出恢復時間
- **THEN** 以呼叫當下＋1 小時為 `until` 登錄

### Requirement: 稽核紀錄
每次 get 與登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類（get）、結束時刻 `until`（登錄冷卻）。不做認證，所以紀錄不分是哪個客戶端。紀錄 MUST NOT 含 key 的值。

#### Scenario: get 留下紀錄
- **WHEN** 有客戶端以 `agent=claude-code` get `claude-personal`
- **THEN** 稽核紀錄有一筆，內容沒有 key 的值

### Requirement: 錯誤回覆
所有錯誤 SHALL 以 `{"error": "<碼>"}` 的 JSON 回覆，碼固定如下，訊息不得回顯請求裡的值：
- 400：`bad_request`（JSON 解析失敗、body 不是物件、缺必填；缺的欄位名放在 `missing`）、`invalid_until`、`until_out_of_range`；
- 403：`agent_not_allowed`；
- 404：`not_found`（不存在的 id 或未知路徑）；
- 409：`not_deployed`；
- 413：`body_too_large`（body 超過 1 MB）；
- 500：`internal`。

客戶端 SHALL 另外以 `unreachable`（連不上或逾時）與 `bad_response`（回覆不是 JSON 物件）表示自己判讀的失敗，不丟例外。

#### Scenario: 缺必填
- **WHEN** 登錄冷卻的 body 沒有 `until`
- **THEN** 回 400 `bad_request`，`missing` 含 `until`

#### Scenario: 時刻格式不對
- **WHEN** `until` 不是 RFC 3339 字串
- **THEN** 回 400 `invalid_until`，冷卻不變

#### Scenario: 連不上
- **WHEN** 客戶端連不上服務
- **THEN** 結果的 `error` 為 `unreachable`，呼叫方沒有拿到例外
