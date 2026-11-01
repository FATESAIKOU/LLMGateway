## MODIFIED Requirements

### Requirement: usage 物件的形狀
list 的每一筆與 resolve 的 `meta` SHALL 帶一個 `usage` 物件，欄位固定為：`status`、`source`、`fetched_at`、`stale`、`reason`、`session`、`weekly`，每個都 MUST 存在（缺值用 null，不省略）。

- `status`：`ok`（至少一個窗口讀到）、`unknown`（有取用量實作，但這次讀不到，含窗口全部已過期）、`not_applicable`（這個 provider 沒有取用量實作）。
- `source`：取得來源的固定字串（Ollama Cloud 為 `ollama-api-balance`，Claude 為 `claude-statusline`，OpenRouter 為 `openrouter-api-key`）；`not_applicable` 時為 null。
- `fetched_at`：最近一次**成功**取得的時刻（RFC 3339 UTC），從沒成功過為 null。
- `stale`：見「stale 與 6 小時」；從沒成功過與 `not_applicable` 為 null。
- `reason`：固定碼，只能是 `not_fetched_yet`、`disabled`、`no_key`、`upstream_error`、`rate_limited`、`timeout`、`parse_error`、`no_windows`、`stale_expired`、`internal` 或 null；MUST NOT 回顯上游內容。
- `session`、`weekly`：`{"remaining_percent": 數字, "resets_at": RFC 3339 UTC}` 或 null。`remaining_percent` 保留原始小數，範圍 0～100。讀不到的窗口 MUST 是 null，**絕不用 0 代替**。

#### Scenario: 讀到兩個窗口
- **WHEN** Ollama 的 balance 回 `included.session`（剩 75%、`resets_at` 2026-10-25T07:00:00Z）與 `included.weekly`（剩 40%、`resets_at` 2026-10-28T00:00:00Z），取完後再 list
- **THEN** 該筆 `usage` 為 `status: ok`、`source: ollama-api-balance`、`stale: false`、`reason: null`，`fetched_at` 是取得時刻，`session.remaining_percent` 為 75、`weekly.remaining_percent` 為 40，`resets_at` 都是 RFC 3339 UTC

#### Scenario: 只讀到一個窗口
- **WHEN** 上游只給 `weekly`
- **THEN** `status: ok`，`session` 是 null，`weekly` 有值；`session` 的 `remaining_percent` 不會是 0

#### Scenario: 形狀恆定
- **WHEN** 對任一種 `status`／`reason` 的 `usage` 檢查鍵集
- **THEN** 七個鍵都在，沒有多餘鍵；`session`／`weekly` 即使是 null 也存在

#### Scenario: 沒有用量來源的 provider
- **WHEN** list 含 `opencode-zen` 與 `opencode-go`
- **THEN** 兩筆的 `usage` 都是 `status: not_applicable`、`source: null`、`fetched_at: null`、`stale: null`、`reason: null`、`session: null`、`weekly: null`；不觸發任何取用量

#### Scenario: 還沒取過
- **WHEN** lgwd 剛啟動，第一次 list 含已啟用的 `ollama-cloud`
- **THEN** 它的 `usage` 是 `status: unknown`、`reason: not_fetched_yet`、`fetched_at: null`、`stale: null`、`session: null`、`weekly: null`

#### Scenario: 方案沒有 session／weekly
- **WHEN** Ollama 的 balance 回應沒有 `included.session` 也沒有 `included.weekly`（新方案，只有 `balance_usd`／`allowance_usd`），取完後再 list
- **THEN** `usage` 是 `status: unknown`、`reason: no_windows`，`session`／`weekly` 為 null，回應不帶金額（沒有 `credits`）

#### Scenario: 失敗原因碼
- **WHEN** 取用量分別遇到：上游回非 200／連線失敗、逾時、回應不是預期結構、回 429、vault 裡沒有該 key、內部例外
- **THEN** `usage.reason` 依序是 `upstream_error`、`timeout`、`parse_error`、`rate_limited`、`no_key`、`internal`，`status` 皆為 `unknown`（或見「stale 與 6 小時」的 `ok`＋`stale`），`session`／`weekly` 為 null，且 `reason` 之外的欄位不含上游回應內容

### Requirement: 用量不外洩秘密
key／token MUST NOT 出現在 list 與 resolve 的新增欄位、log、稽核紀錄或例外訊息。錯誤只記固定碼（`reason`）與 provider id，MUST NOT 記上游回應內容。用量快取 MUST 只放在記憶體（鍵為 subscription id），不進 SQLite、不進稽核。稽核紀錄 MUST NOT 因取用量而新增事件。

#### Scenario: 掃描新增欄位與 log
- **WHEN** 在 key 檔放可辨識的 canary 值，跑各種 `status`／`reason`（成功、各種失敗、429）後，以憑證樣式掃描 list 與 resolve 回覆（resolve 的 `env` 除外）、lgwd log、稽核紀錄、SQLite 檔
- **THEN** 沒有命中

#### Scenario: 失敗訊息不含上游內容
- **WHEN** 假上游回應 body 含 `canary-secret` 字樣的錯誤文字
- **THEN** list／resolve 回覆與 log 都找不到那段字樣

#### Scenario: 快取不落地
- **WHEN** 取用量成功後檢查 SQLite 資料庫與稽核表
- **THEN** 沒有用量資料，稽核筆數不因取用量而增加

#### Scenario: 外連範圍
- **WHEN** 以假 HTTP 層捕捉 lgwd 發出的所有請求
- **THEN** 只有對 `ollama.com` 的 `/api/balance`、`openrouter.ai` 的 `/api/v1/key`；Claude 的外連由原版 Claude Code 自己發出，lgwd 沒有為 Claude 另開連線

### Requirement: TTL 與下限
Ollama Cloud 的預設 TTL SHALL 是 120 秒；Claude 的預設 TTL SHALL 是 30 分鐘；OpenRouter 的預設 TTL SHALL 是 120 秒。政策檔可以調整 TTL，但有下限：Claude（`claude-subscription`）MUST NOT 低於 30 分鐘，Ollama Cloud MUST NOT 低於 60 秒（官方限額 10 次／分鐘、跨 key 與裝置共用，建議每分鐘最多查一次），OpenRouter MUST NOT 低於 60 秒。設定值低於下限時 MUST 夾到下限並記一筆 log（不含任何 key 值）；可以調高。

#### Scenario: Claude 設定值低於下限
- **WHEN** 政策檔把 Claude 的 `ttl_seconds` 設為 300
- **THEN** 實際 TTL 為 1800 秒（`stale` 與重取以 1800 秒為準），並記一筆 log

#### Scenario: Ollama 設定值低於下限
- **WHEN** 政策檔把 `ollama-cloud` 的 `ttl_seconds` 設為 1
- **THEN** 實際 TTL 為 60 秒，並記一筆 log；設為 300 則實際為 300 秒

#### Scenario: Claude 設定值高於下限
- **WHEN** 政策檔把 Claude 的 `ttl_seconds` 設為 7200
- **THEN** 實際 TTL 為 7200 秒

#### Scenario: 預設值
- **WHEN** 政策檔沒有 `usage` 段
- **THEN** Ollama 的 TTL 為 120 秒、Claude 的 TTL 為 1800 秒、OpenRouter 的 TTL 為 120 秒

## ADDED Requirements

### Requirement: OpenRouter 取用量
OpenRouter 取用量 SHALL 對寫死在程式裡的 `https://openrouter.ai/api/v1/key` 發 `GET`（URL 不來自政策檔、不跟隨轉址到別的主機），以 vault 的 key 當 `Authorization: Bearer`，逾時 5 秒，不重試。key MUST 只放進該次請求的 header，不進 log、例外訊息、回覆。每次取之前 MUST 先 `reload_keys_if_changed()`，使換 key 後用新 key。只取 `data.free_model_daily_requests` 的 `remaining`／`limit` 換算成 `session` 窗口的 `remaining_percent`（`remaining / limit * 100`），`resets_at` 為下一個 UTC 00:00（官方每日免費請求數以 UTC 日為單位重置）；讀不到 `free_model_daily_requests` 時該窗口為 null，全部為 null 時為 `no_windows`。`limit_remaining` 等其他欄位不進 usage。key 沒部署時不觸發。TTL 預設 120 秒、下限 60 秒（與 Ollama Cloud 相同）。此 `session` 窗口只代表 `:free` 模型的每日請求數，不是這把 key 的全部可用量（官方：exempt／BYOK 帳號不受此上限約束）。

#### Scenario: 請求形狀
- **WHEN** 以假 HTTP 層捕捉一次 OpenRouter 取用量
- **THEN** 方法為 GET、主機與路徑為 `openrouter.ai` 的 `/api/v1/key`、帶 `Authorization: Bearer <key>`，逾時 5 秒，沒有其他位址的請求

#### Scenario: 逾時
- **WHEN** 上游超過 5 秒沒回應
- **THEN** 取用量結束、記失敗碼 `timeout`，不重試

#### Scenario: 轉址不跟隨
- **WHEN** 上游回 302 指向其他主機
- **THEN** 不對那個主機發請求；`usage.reason` 為 `upstream_error`
