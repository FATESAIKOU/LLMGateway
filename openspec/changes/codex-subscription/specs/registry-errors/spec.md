## MODIFIED Requirements

### Requirement: 400 bad_request 的輸入形狀
`400 bad_request` SHALL 用於 JSON 解析失敗、body 不是物件、缺必填（帶 `missing: [...]`）；resolve 的輸入形狀錯（缺 `agent`／`provider`、`agent` 不是 `claude-code`／`opencode`／`agy`／`codex`、`data` 有給但不在 `public`／`general`／`real` 之內、`subscription` 與 `provider` 不一致、`subscription` 與 `exclude` 同時給、同一 query 參數重複）。

#### Scenario: 缺必填
- **WHEN** 登錄冷卻的 body 沒有 `until`
- **THEN** 回 400 `bad_request`，`missing` 含 `until`

#### Scenario: 未知 agent 是輸入錯誤
- **WHEN** resolve 的 `agent` 不是 `claude-code`、`opencode`、`agy` 或 `codex`
- **THEN** 回 400 `bad_request`

#### Scenario: codex 不是未知 agent
- **WHEN** resolve 的 `agent` 是 `codex`、`provider` 是 `openai`
- **THEN** 不因 `agent` 回 400 `bad_request`

### Requirement: 400 bad_request 的登錄冷卻 model 形狀
登錄冷卻的 `model` 不是非空字串（`null` 除外——那是「沒給」）、超過 200 字元、以 `<agent>/` 開頭（`opencode/`、`claude-code/`、`agy/`、`codex/`）、含萬用字元，或不被該把 `models.allow` 涵蓋時，MUST 回 400 `bad_request`。

#### Scenario: 登錄冷卻的 model 被政策擋下
- **WHEN** 以不被該把 `models.allow` 涵蓋的 `model` 登錄冷卻
- **THEN** 回 400 `bad_request`（不是 403 `model_not_allowed`），冷卻不變

#### Scenario: 登錄冷卻的 model 帶 codex 前綴
- **WHEN** 以 `model=codex/gpt-6.1-sol` 登錄 `chatgpt-personal`
- **THEN** 回 400 `bad_request`，冷卻不變
