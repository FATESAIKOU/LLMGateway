## MODIFIED Requirements

### Requirement: 登錄冷卻的 model 被擋時回 400
`model` 不是非空字串、長度超過 **200 字元**、以 `<agent>/` 開頭（agent ∈ `AGENTS`，即 `opencode/`、`claude-code/`、`agy/`、`codex/`；這個形狀 MUST 被擋，因為 allow 的 `*` 會匹配 `/`，只靠 allow 涵蓋檢查擋不到）、含 fnmatch 萬用字元（`*`、`?`、`[`），或不被該把 `models.allow` 的樣式（fnmatch）涵蓋，MUST 回 400 `bad_request`（**不是** 403 `model_not_allowed`——那是 resolve 的政策拒絕碼），不登錄。

#### Scenario: model 不在 allow
- **WHEN** 以 `model=muse-spark-1.3` 登錄 `opencode-zen`（只 allow `*-free` 與 `big-pickle`）
- **THEN** 回 400 `bad_request`（不是 403），冷卻不變

#### Scenario: model 帶 agent 前綴
- **WHEN** 以 `model=opencode/big-pickle` 登錄 `opencode-zen`
- **THEN** 回 400 `bad_request`（Gateway 不正規化前綴，與 resolve 同一條規則）

#### Scenario: model 帶 agy 前綴
- **WHEN** 以 `model=agy/gemini-3.8-flash-low` 登錄 `antigravity`（`allow` 只有該裸 id）
- **THEN** 回 400 `bad_request`，冷卻不變——`agy/` 由前綴規則擋下，不只看 allow 涵蓋

#### Scenario: model 帶 codex 前綴
- **WHEN** 以 `model=codex/gpt-6.1-sol` 登錄 `chatgpt-personal`（假政策的 `allow` 含 `gpt-6.1-sol`）
- **THEN** 回 400 `bad_request`，冷卻不變——`codex/` 由前綴規則擋下，不只看 allow 涵蓋

#### Scenario: model 帶前綴而且被 allow 樣式涵蓋
- **WHEN** 以 `model=opencode/glm-5.3-flash-free` 登錄 `opencode-zen`（allow 是 `["*-free", "big-pickle"]`，而 fnmatch 的 `*` 會匹配 `/`，所以它「被涵蓋」）
- **THEN** 回 400 `bad_request`，冷卻不變——前綴規則必須自己擋下這一種，allow 涵蓋檢查擋不到

#### Scenario: model 太長
- **WHEN** 以 201 個字元的 `model` 登錄 `claude-personal`（allow 是 `["*"]`），以及以 200 個字元的 `model` 登錄同一把
- **THEN** 前者回 400 `bad_request`、冷卻不變；後者回 204

#### Scenario: model 是萬用字元
- **WHEN** 以 `model=*-free` 登錄 `opencode-zen`
- **THEN** 回 400 `bad_request`，冷卻不變（它不是供應商 id，存進去永遠命不中）
