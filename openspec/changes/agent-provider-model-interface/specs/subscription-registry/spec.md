## Purpose

LLMGateway 對外提供三個介面：list（列菜單，不含 key）、resolve（輸入 provider＋model＋agent 種類，輸出選中的 subscription 的 meta 與必要的環境變數）、登錄冷卻（只收何時能再用 `until`）。呼叫方（sidecar）以 provider＋model 點菜；換哪一把 subscription 由 resolve 依政策挑；`models.allow` 是模型守門。

## ADDED Requirements

### Requirement: resolve
`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>]` SHALL 回 `{schema, meta, env}`。`agent` 與 `provider` 必填，缺少時回 400 `bad_request`（`missing` 列出缺的欄位）；`model` 選填；未知 query 參數 SHALL 忽略。`meta` SHALL 含 `subscription`（選中的 subscription id）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`）、`endpoint`、`priority`、`cooldown`、`available`。`env` SHALL 是把選定的 provider＋model 用起來的必要環境變數（名稱→值），依 `agent` 組成（見「env 的組成」）。`env` 的值 MUST NOT 出現在 list 的回覆、稽核紀錄或 log。錯誤回覆 MUST NOT 含 `env`。

#### Scenario: 同一把的兩個免費模型
- **WHEN** 分別以 `agent=opencode&provider=opencode-zen&model=mimo-v2.6-flash-free` 與 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 兩次都回 200，`meta.subscription` 都是 `opencode-zen`，`meta.model` 各自是請求的模型，兩次的 `env` 不同（模型設定不同）

#### Scenario: 缺必填
- **WHEN** resolve 缺 `agent` 或 `provider`
- **THEN** 回 400 `bad_request`，`missing` 列出缺的欄位，回覆不含 `env`

#### Scenario: meta 的冷卻欄位
- **WHEN** resolve 成功
- **THEN** `meta.cooldown` 為 null、`meta.available` 為 true（選把已排除冷卻與未部署；欄位保留是為了與 list 的 row 同形）

### Requirement: resolve 的選把規則
resolve SHALL 依序挑 subscription：(1) 候選＝`provider` 相同、`agents` 含請求的 agent、（`model` 有給時）model 被 `models.allow` 的樣式（fnmatch）涵蓋、key 已部署；(2) 排除冷卻中的；(3) 挑 `priority` 最小，同分以 id 排序。沒有可挑的 subscription 時 SHALL 依斷在哪一步回：無任何 subscription 是該 provider → 404 `not_found`；有該 provider 的 subscription 但無一允許該 agent → 403 `agent_not_allowed`；有 provider＋agent 的候選但 model 不被涵蓋 → 403 `model_not_allowed`；有涵蓋的候選但全部 key 未部署 → 409 `not_deployed`；有部署的候選但全部冷卻中 → 409 `cooling`（body 帶 `until`＝最早的恢復時刻）。錯誤訊息 MUST NOT 回顯請求裡的值。

#### Scenario: 同 provider 兩把，一把冷卻中
- **WHEN** `claude-personal`（priority 10）在冷卻中，以 `agent=claude-code&provider=anthropic` resolve
- **THEN** 選中 `claude-company`（priority 20），200

#### Scenario: 全部候選冷卻中
- **WHEN** provider＋agent 的所有候選都在冷卻中
- **THEN** 回 409 `cooling`，body 的 `until` 是最早的恢復時刻，不含 `env`

#### Scenario: model 不在 allow
- **WHEN** 以 `agent=opencode&provider=opencode-zen` resolve 一個不被 `*-free`／`big-pickle` 涵蓋的 model
- **THEN** 回 403 `model_not_allowed`，不含 `env`，不代選其他模型

#### Scenario: 未知 provider
- **WHEN** 以 `provider=NoSuchProvider` resolve
- **THEN** 回 404 `not_found`

### Requirement: env 的組成
`env` SHALL 依（provider、kind、agent）組成，只含憑證、端點、模型三類變數；MUST NOT 含呼叫方的維運變數（auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數、permission——那些由 sidecar 疊）：
- claude-code＋claude-subscription：`CLAUDE_CODE_OAUTH_TOKEN`＝key；model 非 null 時 `ANTHROPIC_MODEL`＝`ANTHROPIC_SMALL_FAST_MODEL`＝model；
- claude-code＋api-key：provider 為 anthropic 時 `ANTHROPIC_API_KEY`＝key，否則 `ANTHROPIC_AUTH_TOKEN`＝key 且 `ANTHROPIC_API_KEY`＝空字串；`endpoint` 非 null 時 `ANTHROPIC_BASE_URL`＝endpoint；model 變數同上；
- opencode：`OPENCODE_CONFIG_CONTENT`＝完整 JSON 設定字串，含 `provider.<opencode 的 provider id>.options.apiKey`、`provider.<id>.models.<model>.name`、`model` 與 `small_model`＝`<id>/<model>`；provider id 的對映（`opencode-zen`→`opencode` 等）由 LLMGateway 維護。

model 為 null（agent 自己的預設）時 SHALL 不給模型變數。

#### Scenario: claude 訂閱不帶模型變數
- **WHEN** resolve 選中 `claude-personal`（claude-subscription）而 `meta.model` 為 null
- **THEN** `env` 只有 `CLAUDE_CODE_OAUTH_TOKEN`，沒有 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`

#### Scenario: opencode 的兩個模型
- **WHEN** 同一把 `opencode-zen` 先後 resolve `mimo-v2.6-flash-free` 與 `big-pickle`
- **THEN** 兩次 `env` 的 `OPENCODE_CONFIG_CONTENT` 中 `model`／`small_model` 各自是 `opencode/<該模型>`，憑證欄位相同

#### Scenario: 值不外洩
- **WHEN** 以憑證樣式掃描稽核紀錄、log 與 list 的回覆
- **THEN** 沒有 `env` 任何值的命中

### Requirement: model 省略時的語意
`model` 省略時 SHALL 用選中那把的 `models.default`；default 為 null 時：kind 為 `claude-subscription` → 用 agent 自己的預設（`meta.model` 為 null、env 不帶模型變數）；其他 kind → `models.allow` 第一個不含萬用字元的項；兩者都沒有 → 400 `no_model`（不代選）。

#### Scenario: claude-subscription 的預設
- **WHEN** 以 `agent=claude-code&provider=anthropic` resolve（model 省略）而選中那把的 default 為 null
- **THEN** 200，`meta.model` 為 null，env 不帶模型變數

#### Scenario: api-key 的預設
- **WHEN** 以 `agent=opencode&provider=opencode-zen` resolve（model 省略）
- **THEN** `meta.model` 為 `mimo-v2.6-flash-free`（該把的 default）

#### Scenario: 推不出模型
- **WHEN** model 省略、選中那把的 default 為 null、`models.allow` 全是萬用字元樣式
- **THEN** 回 400 `no_model`，不代選

## MODIFIED Requirements

### Requirement: 稽核紀錄
每次 resolve、每次 get（並存期）與每次登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類（resolve／get）、provider 與 model（resolve）、結束時刻 `until`（登錄冷卻）。紀錄 MUST NOT 含 key 的值，也 MUST NOT 含 `env` 的任何值。不做認證，紀錄不分是哪個客戶端。

#### Scenario: resolve 留下紀錄
- **WHEN** 有客戶端以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 稽核紀錄有一筆（subscription、agent、provider、model），內容沒有 key 或 `env` 的值

### Requirement: 錯誤回覆
所有錯誤 SHALL 以 `{"error": "<碼>"}` 的 JSON 回覆，訊息不得回顯請求裡的值；resolve 的錯誤回覆 MUST NOT 含 `env`。碼在 v5 的表上新增：
- 400：`no_model`（model 省略且推不出模型，不代選）；
- 403：`model_not_allowed`（model 不被任何候選的 `models.allow` 涵蓋）；
- 409：`cooling`（候選全部冷卻中；body 帶 `until`＝最早恢復時刻）。

#### Scenario: 候選全部冷卻中
- **WHEN** resolve 的所有候選都在冷卻中
- **THEN** 回 409 `cooling`，body 的 `until` 是最早的恢復時刻

#### Scenario: 模型被政策擋下
- **WHEN** resolve 的 model 不在候選的 `models.allow`
- **THEN** 回 403 `model_not_allowed`，不含 `env`，不代選其他模型

### Requirement: 共通 JSON schema
list 的 row 形狀不變（v5 的欄位：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models`、`endpoint`、`cooldown`、`available`；`key` 只在 get，並存期）。這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 provider＋model 用起來的環境變數由 resolve 的回覆 `env` 給（「env 的組成」），list 只是菜單。Claude 訂閱的 `agents` MUST 只有 `claude-code`；openrouter 的 `agents` MUST 只有 `opencode`（沿 v5 的理由：條款；免費模型只經過 opencode）。

#### Scenario: list 不變
- **WHEN** 呼叫 list
- **THEN** row 形狀與 v5 相同：不含 key、不含任何環境變數名

### Requirement: models.default 的語意
`models.default`（與 resolve 的 `model` 參數）SHALL 是**供應商自己的模型 id**（例 `mimo-v2.6-flash-free`、`openrouter/free`——可含 `/`），不帶 agent 專用的 provider 前綴；組成 agent 專用的字串（例 opencode 的 `<id>/<model>`）只在 resolve 的 `env` 內部（`OPENCODE_CONFIG_CONTENT` 的 `model`／`small_model`），`meta.model` 一律不帶前綴。null 的語意依 `kind`（見「model 省略時的語意」）：`claude-subscription` 表示用 agent 自己的預設；其他 kind 表示沒有預設、服務不代選。

#### Scenario: 供應商自己的模型 id
- **WHEN** 讀取 resolve 成功的 `meta`
- **THEN** `meta.model` 是供應商自己的模型 id，不帶 `opencode/` 等前綴；前綴只出現在 `env` 的設定內容裡

## REMOVED Requirements

### Requirement: get
**Reason**: #33（本人 2026-10-17）：介面改成「輸入 provider＋model，輸出 JSON（meta 資訊、必要的環境變數）」；以 id 指定 subscription 的入口由 resolve 取代，終態對外三個介面（list、resolve、cooldown）。
**Migration**: 並存期 get 照常服務舊 sidecar（舊 worker 映像）；sidecar 遷移＋實機驗收後另開工單移除實作與 `lgw.client.get`（design 遷移計畫第 4 步）。
