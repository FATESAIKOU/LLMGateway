## MODIFIED Requirements

### Requirement: usage 物件的形狀
list 的每一筆與 resolve 的 `meta` SHALL 帶一個 `usage` 物件，欄位固定為：`status`、`source`、`fetched_at`、`stale`、`reason`、`session`、`weekly`、`pipes`，每個都 MUST 存在（缺值用 null，`pipes` 無管子時用 `[]`，不省略）。

- `status`：`ok`（`session`、`weekly`、`pipes` 至少一項讀到）、`unknown`（有取用量實作，但這次讀不到，含窗口與管子全部已過期）、`not_applicable`（這個 provider 沒有取用量實作）。
- `source`：取得來源的固定字串（Ollama Cloud 為 `ollama-api-balance`，Claude 為 `claude-statusline`，OpenRouter 為 `openrouter-api-key`）；`not_applicable` 時為 null。
- `fetched_at`：最近一次**成功**取得的時刻（RFC 3339 UTC），從沒成功過為 null。
- `stale`：見「stale 與 6 小時」；從沒成功過與 `not_applicable` 為 null。
- `reason`：固定碼，只能是 `not_fetched_yet`、`disabled`、`no_key`、`upstream_error`、`rate_limited`、`timeout`、`parse_error`、`no_windows`、`stale_expired`、`internal` 或 null；MUST NOT 回顯上游內容。
- `session`、`weekly`：`{"remaining_percent": 數字, "resets_at": RFC 3339 UTC}` 或 null。`remaining_percent` 保留原始小數，範圍 0～100。讀不到的窗口 MUST 是 null，**絕不用 0 代替**。這兩個鍵是相容層，語意與值不隨 `pipes` 的加入而改變。
- `pipes`：陣列，見「管子（pipes）」。

#### Scenario: 讀到兩個窗口
- **WHEN** Ollama 的 balance 回 `included.session`（剩 75%、`resets_at` 2026-10-25T07:00:00Z）與 `included.weekly`（剩 40%、`resets_at` 2026-10-28T00:00:00Z），取完後再 list
- **THEN** 該筆 `usage` 為 `status: ok`、`source: ollama-api-balance`、`stale: false`、`reason: null`，`fetched_at` 是取得時刻，`session.remaining_percent` 為 75、`weekly.remaining_percent` 為 40，`resets_at` 都是 RFC 3339 UTC

#### Scenario: 只讀到一個窗口
- **WHEN** 上游只給 `weekly`
- **THEN** `status: ok`，`session` 是 null，`weekly` 有值；`session` 的 `remaining_percent` 不會是 0

#### Scenario: 形狀恆定
- **WHEN** 對任一種 `status`／`reason` 的 `usage` 檢查鍵集
- **THEN** 八個鍵都在，沒有多餘鍵；`session`／`weekly` 即使是 null 也存在，`pipes` 即使沒有管子也是 `[]`

#### Scenario: 沒有用量來源的 provider
- **WHEN** list 含 `opencode-zen`、`opencode-go` 與 `antigravity`
- **THEN** 三筆的 `usage` 都是 `status: not_applicable`、`source: null`、`fetched_at: null`、`stale: null`、`reason: null`、`session: null`、`weekly: null`、`pipes: []`；不觸發任何取用量

#### Scenario: 還沒取過
- **WHEN** lgwd 剛啟動，第一次 list 含已啟用的 `ollama-cloud`
- **THEN** 它的 `usage` 是 `status: unknown`、`reason: not_fetched_yet`、`fetched_at: null`、`stale: null`、`session: null`、`weekly: null`、`pipes: []`

#### Scenario: 方案沒有 session／weekly
- **WHEN** Ollama 的 balance 回應沒有 `included.session` 也沒有 `included.weekly`（新方案，只有 `balance_usd`／`allowance_usd`），取完後再 list
- **THEN** `usage` 是 `status: unknown`、`reason: no_windows`，`session`／`weekly` 為 null，`pipes` 為 `[]`，回應不帶金額（沒有 `credits` 絕對值）

#### Scenario: 失敗原因碼
- **WHEN** 取用量分別遇到：上游回非 200／連線失敗、逾時、回應不是預期結構、回 429、vault 裡沒有該 key、內部例外
- **THEN** `usage.reason` 依序是 `upstream_error`、`timeout`、`parse_error`、`rate_limited`、`no_key`、`internal`，`status` 皆為 `unknown`（或見「stale 與 6 小時」的 `ok`＋`stale`），`session`／`weekly` 為 null、`pipes` 為 `[]`，且 `reason` 之外的欄位不含上游回應內容

## ADDED Requirements

### Requirement: 管子（pipes）
`usage.pipes` SHALL 是陣列，每個元素代表同一把 key 底下一個獨立的額度桶，欄位固定為：`id`、`models`、`models_except`、`remaining_percent`、`resets_at`，每個都 MUST 存在。

- `id`：字串，同一 subscription 內唯一，由 Gateway 程式內的固定表決定，MUST NOT 取自上游回應原文；出貨後 MUST NOT 改名。
- `models`、`models_except`：fnmatch 樣式的字串陣列。模型名符合 `models` 任一樣式且不符合 `models_except` 任一樣式，才屬於該管子；`["*"]` 表示整把 key。
- `remaining_percent`：數字，0～100，保留原始小數。
- `resets_at`：RFC 3339 UTC 或 null。OpenRouter 的 `credits` 管子 MUST 恆為 null（重置週期的上游語意未驗，之後另開單）。

只有讀到的管子 MUST 出現在陣列裡，MUST NOT 以 `remaining_percent: null` 或 0 作佔位。`resets_at` 已過的管子 MUST 從陣列拿掉；`resets_at` 為 null 的管子依「stale 與 6 小時」一併消失。OpenRouter `credits` 的值若異常（負數、大於 100%、型別不對、`limit` 非正數），MUST 只丟棄 `credits` 這一根，MUST NOT 使 `free_daily`、`status` 或退避受影響。管子 MUST NOT 帶任何絕對數量、金額或 key 值。有 `session`／`weekly` 的 source，其對應管子與這兩個鍵 MUST 逐值相等。`available_again_at` 的算法 MUST NOT 因 `pipes` 改變（仍只看 `session`／`weekly`）。

#### Scenario: Claude 的兩根管子
- **WHEN** statusline 的 `five_hour.used_percentage` 是 23.5、`seven_day.used_percentage` 是 61
- **THEN** `pipes` 有 `five_hour`（`remaining_percent` 76.5）與 `seven_day`（39），`models` 皆為 `["*"]`，與 `session`／`weekly` 逐值相等

#### Scenario: OpenRouter 有上限的 credits
- **WHEN** `/api/v1/key` 回 `free_model_daily_requests`（remaining 25、limit 50）、`limit` 12345.678、`limit_remaining` 4938.2712
- **THEN** `pipes` 有 `free_daily`（50）與 `credits`（40，`resets_at` 為 null，`models_except` 為 `["*:free","openrouter/free"]`）；`session` 只等於 `free_daily`；回應不含 `12345.678`、`4938.2712`

#### Scenario: OpenRouter 無上限的 credits
- **WHEN** `limit` 與 `limit_remaining` 為 null
- **THEN** `pipes` 沒有 `credits`，`status` 不受影響

#### Scenario: 只有 credits 讀到
- **WHEN** 沒有 `free_model_daily_requests`，但 credits 有上限
- **THEN** `status: ok`，`session` 為 null，`pipes` 只有 `credits`

#### Scenario: 管子過了重置時刻
- **WHEN** 某管子的 `resets_at` 早於現在，其他管子在未來
- **THEN** 該管子不在 `pipes`，其他照舊，`status` 仍是 `ok`

#### Scenario: credits 異常值只丟棄 credits
- **WHEN** `free_model_daily_requests` 正常，但 `limit_remaining` 為負數（或大於 `limit`、型別不對）
- **THEN** `pipes` 沒有 `credits`，`free_daily` 與 `session` 仍在，`status: ok`，不觸發退避

#### Scenario: credits 的 resets_at 恆為 null
- **WHEN** `/api/v1/key` 回應帶任何重置週期欄位，且 `credits` 讀到
- **THEN** `credits.resets_at` 為 null

#### Scenario: 範圍聲明（available_again_at）
- **WHEN** 檢視本 change 的 Gateway 行為
- **THEN** `available_again_at` 只由 `cooldown.until` 與 `session`／`weekly` 計算，不依 `pipes`（讀者如何消費 `pipes` 不在本 change）

#### Scenario: 不外洩
- **WHEN** 以憑證樣式與絕對值 canary 掃描含 `pipes` 的 list 與 resolve 回覆
- **THEN** 沒有命中；`id` 為 fetcher 固定值，不含上游標籤原文
