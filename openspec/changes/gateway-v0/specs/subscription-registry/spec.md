## Purpose

LLMGateway 對外只提供三個介面：列出 Provider-Subscription、取得一個 subscription 的詳細（共通 JSON schema，sidecar 直接拿來開 agent）、登錄 5h／1w 的冷卻。換哪一把由 sidecar 自己挑；LLMGateway 只保管、列出、記冷卻。

## ADDED Requirements

### Requirement: 共通 JSON schema
每個 subscription SHALL 以同一個 JSON 形狀描述，欄位如下：
- `schema`（版本，v0 為 1）、`id`、`provider`、`kind`；
- `agents`：可以用這把的 agent 種類，`claude-code` 或 `opencode`；
- `priority`：預設挑選順序，數字小的先；
- `models`：`allow` 與 `default`；
- `endpoint`（可為 null）；
- `key`：只在 get 回；
- `cooldown`：`5h` 與 `1w` 各為 `{until}` 或 null；
- `available`：key 已部署，而且兩個窗口都不在冷卻。

這個形狀 MUST NOT 帶任何 agent 專用的環境變數名；把 key 變成哪些環境變數由 sidecar 的 agent driver 決定。Claude 訂閱的 `agents` MUST 只有 `claude-code`。

#### Scenario: Claude 訂閱的描述
- **WHEN** 讀取 `claude-personal` 的描述
- **THEN** `provider` 為 anthropic、`kind` 為 claude-subscription、`agents` 只有 `claude-code`，形狀裡沒有任何環境變數名

#### Scenario: 能不能用
- **WHEN** 某把的 key 已部署，而且 5h、1w 都沒在冷卻
- **THEN** `available` 為 true；其中一個窗口在冷卻或 key 沒部署時為 false

### Requirement: list
`GET /v1/subscriptions` SHALL 回所有 subscription 的共通 JSON，依 `priority` 排序，**不含 `key`**。

#### Scenario: 不含秘密
- **WHEN** 客戶端呼叫 list
- **THEN** 每一筆都沒有 `key` 欄位，回覆裡找不到任何 key 的值

#### Scenario: 冷卻反映在 list
- **WHEN** `claude-personal` 的 5h 窗口冷卻到 15:00
- **THEN** 15:00 之前 list 裡它的 `available` 為 false、`cooldown.5h.until` 為 15:00；15:00 之後 `available` 回到 true

### Requirement: get
`GET /v1/subscriptions/{id}?agent=<種類>` SHALL 回該 subscription 的共通 JSON，含 `key`。`agent` 是必填參數，不在該筆的 `agents` 裡 MUST 回 403 `agent_not_allowed`，不回 key。不存在的 id 回 404；key 沒部署時回 409 `not_deployed`。

#### Scenario: Claude Code 拿 Claude 訂閱
- **WHEN** 以 `agent=claude-code` get `claude-personal`
- **THEN** 回覆含 `key`

#### Scenario: opencode 不能拿 Claude 訂閱
- **WHEN** 以 `agent=opencode` get `claude-company`
- **THEN** 回 403 `agent_not_allowed`，不含 key

#### Scenario: 冷卻中仍可 get
- **WHEN** 某把在冷卻中，有人 get 它
- **THEN** 照樣回覆（`available` 為 false），由呼叫方決定要不要用

### Requirement: 登錄冷卻
`POST /v1/subscriptions/{id}/cooldown {window, resets_at?}` SHALL 登錄某個窗口的冷卻：
- `window` 為 `5h` 或 `1w`；
- 有 `resets_at` 時冷卻到 `resets_at` 加 2 分鐘；沒有時用該窗口的預設長度（5 小時、7 天）；
- `resets_at` 在過去或超過 8 天以後，MUST 回 400，不登錄；
- 同一窗口重複登錄時取較晚的結束時刻；
- 過了結束時刻自動解除，不需要另一個動作。

#### Scenario: 帶重置時刻
- **WHEN** 以 `window=5h`、`resets_at=15:00` 登錄 `claude-personal`
- **THEN** 它的 5h 窗口冷卻到 15:02，list 顯示 `available` 為 false

#### Scenario: 不合理的時刻
- **WHEN** `resets_at` 是過去的時刻
- **THEN** 回 400，冷卻不變

#### Scenario: 沒有重置時刻
- **WHEN** 以 `window=1w` 登錄而沒有 `resets_at`
- **THEN** 1w 窗口冷卻 7 天

### Requirement: 稽核紀錄
每次 get 與登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、客戶端類別、subscription id、agent 種類（get）、窗口與結束時刻（登錄冷卻）。紀錄 MUST NOT 含 key 的值。

#### Scenario: 看誰拿過哪一把
- **WHEN** `pool-worker` 以 `agent=claude-code` get `claude-personal`
- **THEN** 稽核紀錄有一筆，內容沒有 key 的值
