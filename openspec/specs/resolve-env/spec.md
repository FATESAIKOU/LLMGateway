# resolve-env Specification

## Purpose
定義 LLMGateway resolve 成功時回傳的環境變數 `env` 的組成規則、各 agent 與 provider 的變數鍵值對應、未知鍵與省略語意。少了 `agent` 判斷會漏出 api-key row 的憑證，少了 `kind` 判斷會漏出 local-login 的憑證；雙重防線之規範見「agent 或 kind 任一成立即回空物件」。

## Requirements
### Requirement: env 的組成

`env` SHALL 依（provider、kind、agent）組成，只含憑證、端點、模型命名（含 opencode 的 provider id）三類變數。

#### Scenario: 依 provider、kind、agent 組成
- **WHEN** 同一把 `opencode-zen` 以 `agent=opencode` resolve
- **THEN** `env` 只含憑證、端點、模型命名三類變數，不含呼叫方的維運變數

### Requirement: env 不含呼叫方的維運變數
`env` MUST NOT 含呼叫方的維運變數（auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數、`permission`、`instructions`——那些由 sidecar 疊）。

#### Scenario: 維運變數不在 env
- **WHEN** 以任一 `agent`＋provider 組合 resolve
- **THEN** `env` 不含 auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數、`permission`、`instructions`

### Requirement: env 的鍵集範圍
`env` MUST 只含下列各 Requirement 定義的鍵（Gateway 在同一個 `env_schema` 內不得改鍵集）；每鍵的型別、空值與出現條件見各自的 Requirement。

#### Scenario: 未知鍵不出現
- **WHEN** resolve 任一組合
- **THEN** `env` 的每個鍵都是本檔各 env 鍵 requirement 定義的鍵

### Requirement: CLAUDE_CODE_OAUTH_TOKEN
`agent` 為 `claude-code` 且 `kind` 為 `claude-subscription` 時，`env` MUST 含 `CLAUDE_CODE_OAUTH_TOKEN`，字串 MUST 是該把的 key（OAuth token），MUST NOT 為空；該組合必然出現。

#### Scenario: CLAUDE_CODE_OAUTH_TOKEN 必然出現
- **WHEN** resolve `claude-personal`（`agent=claude-code`）
- **THEN** `env` 含非空的 `CLAUDE_CODE_OAUTH_TOKEN`

### Requirement: ANTHROPIC_API_KEY 的值
`agent` 為 `claude-code` 且 `kind` 為 api-key 時：provider 為 `anthropic` 的 `env` MUST 含 `ANTHROPIC_API_KEY`，字串 MUST 是該把的 key、MUST NOT 為空（**現行不可達**：`anthropic` 恆為 `claude-subscription`，預留形狀）；其他 provider 時 MUST 是**空字串**（刻意佔位，蓋掉子行程環境可能殘留的繼承值）；`claude-subscription` 時 MUST NOT 出現。

#### Scenario: api-key 的 ANTHROPIC_API_KEY 為空字串佔位
- **WHEN** 以 `agent=claude-code` resolve 一把非 `anthropic` 的 api-key 訂閱
- **THEN** `env` 的 `ANTHROPIC_API_KEY` 是空字串，不是該把的 key

### Requirement: ANTHROPIC_AUTH_TOKEN 與 ANTHROPIC_BASE_URL
`agent` 為 `claude-code`、`kind` 為 api-key 且 provider 非 `anthropic` 時：`ANTHROPIC_AUTH_TOKEN` MUST 是該把的 key，MUST NOT 為空；`ANTHROPIC_BASE_URL` MUST 是該把的 `endpoint`（URL），僅 `endpoint` 非 null 時出現。

#### Scenario: api-key 的 AUTH_TOKEN 與 BASE_URL
- **WHEN** 以 `agent=claude-code` resolve 一把 `endpoint` 非 null 的非 `anthropic` api-key 訂閱
- **THEN** `env` 含非空的 `ANTHROPIC_AUTH_TOKEN` 與等於該把 `endpoint` 的 `ANTHROPIC_BASE_URL`

### Requirement: ANTHROPIC_MODEL 與 ANTHROPIC_SMALL_FAST_MODEL
`agent` 為 `claude-code` 的全部組合：`ANTHROPIC_MODEL` 與 `ANTHROPIC_SMALL_FAST_MODEL` MUST 同值，為選定的 model（供應商 model id），MUST NOT 為空；僅 `model` 非 null 時出現。

#### Scenario: claude 訂閱不帶模型變數
- **WHEN** resolve 選中 `claude-personal`（claude-subscription）而 `meta.model` 為 null
- **THEN** `env` 只有 `CLAUDE_CODE_OAUTH_TOKEN`，沒有 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`

### Requirement: OPENCODE_API_KEY
`agent` 為 `opencode` 時，`env` MUST 含 `OPENCODE_API_KEY`，字串 MUST 是該把的 key，MUST NOT 為空，且必然出現。

#### Scenario: OPENCODE_API_KEY 必然出現
- **WHEN** resolve `opencode-zen`（`agent=opencode`）
- **THEN** `env` 含非空的 `OPENCODE_API_KEY`，兩次 resolve 的值相同

### Requirement: OPENCODE_PROVIDER_ID
`agent` 為 `opencode` 時，`env` MUST 含 `OPENCODE_PROVIDER_ID`，字串 MUST 是 opencode 的 provider id（`opencode-zen`→`opencode`、`opencode-go`→`opencode-go`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud`，由 LLMGateway 維護；opencode config 內的模型名＝`<OPENCODE_PROVIDER_ID>/<OPENCODE_MODEL>`，由 sidecar 組），MUST NOT 為空，且必然出現。

#### Scenario: opencode 的兩個模型（片段）
- **WHEN** 同一把 `opencode-zen` 先後 resolve `mimo-v2.6-flash-free` 與 `big-pickle`
- **THEN** 兩次 `env` 的 `OPENCODE_PROVIDER_ID` 都是 `opencode`、`OPENCODE_API_KEY` 相同，`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 各自是該模型；`env` 不含 `OPENCODE_CONFIG_CONTENT`

#### Scenario: opencode-go 的片段
- **WHEN** 以 `agent=opencode&provider=opencode-go&model=beam-free` resolve
- **THEN** `env` 的 `OPENCODE_PROVIDER_ID` 為 `opencode-go`、`OPENCODE_API_KEY` 為 Zen 那把 key 的值（兩把共用同一個鍵名），`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 為 `beam-free`

### Requirement: OPENCODE_MODEL 與 OPENCODE_SMALL_MODEL
`agent` 為 `opencode` 時，`OPENCODE_MODEL` 與 `OPENCODE_SMALL_MODEL` MUST 同值，為選定的 model（供應商 model id），MUST NOT 為空；僅 `model` 非 null 時出現。

#### Scenario: model 為 null 時不給 OPENCODE 模型變數
- **WHEN** resolve `opencode-zen`（`agent=opencode`）且 `meta.model` 為 null
- **THEN** `env` 不含 `OPENCODE_MODEL`、`OPENCODE_SMALL_MODEL`，其餘 `OPENCODE_*` 照常給

### Requirement: local-login 的 env 為空物件
`kind` 為 `local-login` 的訂閱（本 change 的 `antigravity`）**沒有 env**：`env` MUST 是**空物件 `{}`**——沒有任何鍵，也就 MUST NOT 出現 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`／`OPENCODE_*`。agy 用它自己在那台機器的本機登入，Gateway 不給憑證、不給端點、不給模型變數；模型只走 `meta.model`（sidecar 的 agy driver 只從那裡拿）。

#### Scenario: agy 的 env 是空物件
- **WHEN** 以 `agent=agy&provider=google&model=<該把的裸 id>` resolve，選中 `antigravity`（kind `local-login`）
- **THEN** `env` 是 `{}`：沒有 `ANTHROPIC_*`（含 `ANTHROPIC_API_KEY` 的空字串佔位）、沒有 `OPENCODE_*`、沒有 `CLAUDE_CODE_OAUTH_TOKEN`；`env_schema` 仍是 1

### Requirement: agent 或 kind 任一成立即回空物件
實作 MUST 同時寫兩條判斷——`agent` 為 `agy` **或** 該把的 `kind` 為 `local-login`，任一成立就回 `{}`，防線不依賴單一條件。只判 `agent` 擋的是「agy 進了某一筆 api-key row」（政策條款被改弱，該把的 `kind` 不是 `local-login`）；只判 `kind` 擋的是「`antigravity` 的 `agents` 被改弱成含 `claude-code`」（該把仍是 `local-login` 但 agent 不是 `agy`）；兩條合起來，任一邊被改弱都還是回 `{}`。

#### Scenario: 條款被改弱也不會回 claude 的 env
- **WHEN** 單元測試直接以一筆 api-key provider（例如 `opencode-zen`）呼叫 `build_env("agy", <key>, <model>)`——模擬「`_AGY_ONLY` 條款被改弱、agy 進了 api-key row」的組合（經政策載入不可達，所以這裡測的是單元層的第二道防線）
- **THEN** `env` 仍是 `{}`、不回任何 `ANTHROPIC_*`／`OPENCODE_*`：`kind` 不是 `local-login`，但 `agent` 為 `agy` 那條判斷成立

#### Scenario: 條款被改弱成含 claude-code 也不會回 claude 的 env
- **WHEN** 單元測試直接以一個 `kind` 為 `local-login` 的 provider（`GoogleProvider`）呼叫 `build_env("claude-code", null, <model>)`——模擬「`_AGY_ONLY` 條款被改弱、`antigravity` 的 `agents` 含 `claude-code`」的組合（經政策載入不可達，所以這裡測的是單元層的第二道防線）
- **THEN** `env` 仍是 `{}`、不回任何 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`／`OPENCODE_*`：`agent` 不是 `agy`，但 `kind` 為 `local-login` 那條判斷成立

### Requirement: 回空物件不改鍵集
`kind` 是 provider 常數、不是政策資料，所以 `kind` 那條判斷不隨政策檔改變；`agent` 那條則不隨 provider 表改變。回 `{}` 是「因 agent／kind 而少給鍵」，`env_schema` MUST 維持 1，不改鍵集（見 `resolve`「resolve」）。

#### Scenario: 回 {} 不改 env_schema
- **WHEN** resolve 因 agent 或 kind 回 `env` 為 `{}`
- **THEN** `env_schema` 仍是 1，不視為鍵集變更

### Requirement: opencode 只回片段
opencode 只回**片段**（上述 `OPENCODE_*` 四鍵）：Gateway MUST NOT 回 `OPENCODE_CONFIG_CONTENT`，也 MUST NOT 回 `baseURL`（`endpoint` 是 provider 根位址，依 #28 不抄進 opencode 的 `options.baseURL`）。

#### Scenario: env 不含 OPENCODE_CONFIG_CONTENT
- **WHEN** resolve `opencode-zen`（`agent=opencode`）
- **THEN** `env` 不含 `OPENCODE_CONFIG_CONTENT`，也不含 `baseURL`

### Requirement: model 為 null 時不給模型變數
`model` 為 null（agent 自己的預設）時 SHALL 不給模型變數（`ANTHROPIC_MODEL`、`ANTHROPIC_SMALL_FAST_MODEL`、`OPENCODE_MODEL`、`OPENCODE_SMALL_MODEL` 都不出現）。

#### Scenario: claude 訂閱 model 為 null
- **WHEN** resolve 選中 `claude-personal` 且 `meta.model` 為 null
- **THEN** `env` 沒有 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`，也沒有 `OPENCODE_*` 模型變數

### Requirement: 未知鍵與消費端規則
`env` MUST 只含上述鍵（Gateway 在同一個 `env_schema` 內不得改鍵集）；sidecar 對未知鍵、overlay、`OPENCODE_CONFIG_CONTENT` 的組成、`permission`／`instructions` 的擁有、繼承環境不外漏等消費端義務，見 `archive/2026-10-24-agent-provider-model-interface/design.md` 決定 2 與決定 6，由 MyLinuxPool 的 sidecar 規格承接（本 spec 只定 Gateway 能實作與驗證的部分）。

#### Scenario: 消費端的義務不在 Gateway 範圍
- **WHEN** 檢視本 spec 的 `env` 規則
- **THEN** 只定 Gateway 能實作與驗證的部分；sidecar 的消費端義務以 `archive/2026-10-24-agent-provider-model-interface/design.md` 為準

### Requirement: model 省略時的語意

`model` 省略時 SHALL 用選中那把的 `models.default`；default 為 null 時：kind 為 `claude-subscription` → 用 agent 自己的預設（`meta.model` 為 null、env 不帶模型變數）；其他 kind → `models.allow` 第一個不含萬用字元的項；兩者都沒有 → 400 `no_model`（不代選）。候選在選把時已限於能推導模型的（見 `resolve`「resolve 的選把規則」第 4 步；釘住時第 1 步先限縮成那把、推不出直接 400 `no_model`），所以不會出現「選中後才發現推不出、但另一個候選可以」。

#### Scenario: claude-subscription 的預設
- **WHEN** 以 `agent=claude-code&provider=anthropic&subscription=claude-personal` resolve（model 省略）而該把的 default 為 null
- **THEN** 200，`meta.model` 為 null，env 不帶模型變數

#### Scenario: api-key 的預設
- **WHEN** 以 `agent=opencode&provider=opencode-zen` resolve（model 省略）
- **THEN** `meta.model` 為 `mimo-v2.6-flash-free`（該把的 default）

#### Scenario: 推不出模型
- **WHEN** model 省略、釘住那把的 default 為 null、`models.allow` 全是萬用字元樣式，且該把的 kind 不是 `claude-subscription`（api-key 形狀；現行政策沒有這種 provider，用假政策測）
- **THEN** 回 400 `no_model`，不代選

