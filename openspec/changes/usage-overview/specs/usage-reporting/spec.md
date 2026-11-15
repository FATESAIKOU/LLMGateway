## MODIFIED Requirements

### Requirement: usage 物件的形狀
list 的每一筆與 resolve 的 `meta` SHALL 帶一個 `usage` 物件，欄位固定為：`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`、`pipes`，每個都 MUST 存在（缺值用 null，`pipes` 無管子時用 `[]`，不省略）。`note` 是本 change 新增的第九個鍵（見「note 的語意」）；九個鍵以外 MUST NOT 有其他鍵。

#### Scenario: 形狀恆定
- **WHEN** 對任一種 `status`／`reason` 的 `usage` 檢查鍵集
- **THEN** 九個鍵都在，沒有多餘鍵；`session`／`weekly` 即使是 null 也存在，`pipes` 即使沒有管子也是 `[]`，`note` 即使是 null 也存在

#### Scenario: 新增的 note 鍵不改變舊鍵的名稱與型別
- **WHEN** 既有客戶端讀取 `usage` 的 `status`、`source`、`fetched_at`、`stale`、`reason`、`session`、`weekly`、`pipes`
- **THEN** 這八個鍵的名稱與型別與本 change 前相同，`status`、`source`、`fetched_at`、`stale`、`session`、`weekly`、`pipes` 的語意不變；`reason` 有一處語意變更：`status` 為 `not_applicable` 時由 null 變為 `no_quota_api` 或 `unavailable`（見「usage 的 status 與 source」）。這是行為變更，不只是新增欄位；`schema` 不 bump 的理由見 design §3

### Requirement: usage 的 status 與 source
`usage.status` MUST 是下列之一：`ok`（`session`、`weekly`、`pipes` 至少一項讀到）、`unknown`（有取用量實作，但這次讀不到，含窗口與管子全部已過期）、`not_applicable`（這個 subscription 沒有取用量實作）。`usage.status` 的值域不因本 change 增加。`usage.source` MUST 是取得來源的固定字串（Ollama Cloud 為 `ollama-api-balance`，Claude 為 `claude-statusline`，OpenRouter 為 `openrouter-api-key`）；`not_applicable` 時為 null。

`status` 為 `not_applicable` 時，`usage.reason` 與 `usage.note` 依「not_applicable 的 reason 與固定表」決定（`reason` MUST NOT 為 null）。

#### Scenario: 沒有用量來源的 provider
- **WHEN** list 含 `opencode-zen`、`opencode-go` 與 `antigravity`
- **THEN** 三筆的 `usage` 都是 `status: not_applicable`、`source: null`、`fetched_at: null`、`stale: null`、`reason: unavailable`、`note` 為非空的一句話、`session: null`、`weekly: null`、`pipes: []`；不觸發任何取用量

#### Scenario: 還沒取過
- **WHEN** lgwd 剛啟動，第一次 list 含已啟用的 `ollama-cloud`
- **THEN** 它的 `usage` 是 `status: unknown`、`reason: not_fetched_yet`、`fetched_at: null`、`stale: null`、`note: null`、`session: null`、`weekly: null`、`pipes: []`

### Requirement: usage 的時刻與原因碼
`usage.fetched_at` 是最近一次**成功**取得的時刻（RFC 3339 UTC），從沒成功過為 null。`usage.stale` 見「stale 與 6 小時」；從沒成功過與 `not_applicable` 為 null。`usage.reason` MUST 是固定碼之一：`not_fetched_yet`、`disabled`、`no_key`、`upstream_error`、`rate_limited`、`timeout`、`parse_error`、`no_windows`、`stale_expired`、`internal`、`no_quota_api`、`unavailable` 或 null；MUST NOT 回顯上游內容。`no_quota_api` 與 `unavailable` MUST 只出現在 `status` 為 `not_applicable` 時；其他 `status` 不得使用這兩個碼。

#### Scenario: 失敗原因碼
- **WHEN** 取用量分別遇到：上游回非 200／連線失敗、逾時、回應不是預期結構、回 429、vault 裡沒有該 key、內部例外
- **THEN** `usage.reason` 依序是 `upstream_error`、`timeout`、`parse_error`、`rate_limited`、`no_key`、`internal`，`status` 皆為 `unknown`（或見「stale 與 6 小時」的 `ok`＋`stale`），`session`／`weekly` 為 null、`pipes` 為 `[]`，且 `reason` 之外的欄位不含上游回應內容

#### Scenario: 拿不到的碼只配 not_applicable
- **WHEN** 任一筆 `status` 為 `unknown` 或 `ok`
- **THEN** `reason` MUST NOT 是 `no_quota_api` 或 `unavailable`

### Requirement: 被查到且過期才取，不等結果
取用量 SHALL 只在 list、resolve 或 `GET /v1/usage` 組裝某個 subscription 的 `usage` 時觸發，條件是：該 provider 有取用量實作、已啟用（見「政策檔的 usage 設定」）、key 已部署，而且快取不存在或 `now - fetched_at` 超過 TTL，而且不在退避期間內。

#### Scenario: 快取過期的第一次查詢
- **WHEN** 快取不存在，呼叫 list，假取用量卡住不返回
- **THEN** list 立刻回 200，該筆 `usage` 是 `unknown`／`not_fetched_yet`，假取用量被呼叫一次

#### Scenario: 快取還新鮮
- **WHEN** `now - fetched_at` 小於 TTL 時呼叫 list、resolve 或 `GET /v1/usage`
- **THEN** 不觸發取用量

#### Scenario: 用量一覽也會觸發
- **WHEN** 快取過期，呼叫 `GET /v1/usage`
- **THEN** 觸發的條件與 list 相同（同一套 single-flight 與退避）；回覆不等取用量，該筆是 `unknown`／`not_fetched_yet`

## ADDED Requirements

### Requirement: not_applicable 的 reason 與固定表
`status` 為 `not_applicable` 時，`usage.reason` MUST 是下列之一：`no_quota_api`（研究文件或官方文件**明載**上游沒有額度資訊來源）、`unavailable`（其餘情形：上游有額度資訊，但 Gateway 取不到，例如只在帳戶 session 或呼叫方本機登入的介面上）。出廠分類：`opencode-zen`、`opencode-go`、`antigravity`、`chatgpt-personal` 皆為 `unavailable`；`no_quota_api` 目前無出廠訂閱符合，保留給日後有實證者。`reason` 與 `note` 依 subscription id 查固定表：先查表內（用專屬 note），查不到才用通用說明。表外者 MUST NOT 讓程式啟動失敗，MUST 降級為 `reason: unavailable` 與通用說明，每個 subscription id 只記一條 warning。MUST NOT 給 `reason: null`。

#### Scenario: 不存在 no_quota_api 的出廠訂閱
- **WHEN** 出廠政策檔載入後呼叫 list
- **THEN** 沒有任何一筆的 `usage.reason` 是 `no_quota_api`（該碼保留給日後有實證的 subscription）

#### Scenario: 固定表未收錄的 not_applicable 不啟動失敗
- **WHEN** 政策檔含一個 `not_applicable` 的 subscription（例如 `groq`），它的 id 不在固定表裡，lgwd 啟動並呼叫 list
- **THEN** lgwd 照常啟動；該筆 `usage` 是 `status: not_applicable`、`source: null`、`reason: unavailable`、`note` 為固定的通用說明一句（非 null），並記一條 warning；之後同一 id 的 list 不再重記；沒有任何一筆的 `not_applicable` 是 `reason: null`

#### Scenario: 空的 UsageService 下，表內 id 用專屬 note、表外 id 用通用 note
- **WHEN** `RegistryService` 沒有注入取用量實作（測試或未注入），呼叫 list
- **THEN** 每一筆的 `usage` 都是 `status: not_applicable`、`reason: unavailable`、`note` 非 null，不觸發任何取用量；表內 id（例如 `opencode-zen`）的 `note` 是固定表中的專屬一句，表外 id（例如 `groq`）的 `note` 是通用說明一句

### Requirement: note 的語意
`usage.note` SHALL 是字串或 null。`reason` 為 `no_quota_api` 或 `unavailable` 時，`note` MUST 是程式內固定表的一句話（依 subscription id 查出，說明為何拿不到；固定表未收錄者用固定的通用說明一句）；其他情況 `note` MUST 為 null。`note` MUST NOT 取自上游回應、MUST NOT 含 key、token、帳號識別或金額。`resolve` 的 `meta.usage` 與 list 的 `usage` 同形狀，`note` 規則相同。

#### Scenario: note 只在拿不到時出現
- **WHEN** `usage.status` 為 `ok`，或為 `unknown` 且 `reason` 是 `upstream_error`
- **THEN** `note` 為 null

#### Scenario: note 不回顯上游內容
- **WHEN** 假上游的錯誤 body 含 `canary-secret` 字樣，且該 provider 的 `reason` 為 `upstream_error`
- **THEN** list、resolve 與 `GET /v1/usage` 的回覆都找不到那段字樣，`note` 為 null

#### Scenario: 不拿金額或帳號
- **WHEN** 以憑證樣式與金額 canary 掃描含 `note` 的 list、resolve 與 `GET /v1/usage` 回覆
- **THEN** 沒有命中

#### Scenario: resolve 的 meta.usage 帶 note
- **WHEN** resolve 選中 `chatgpt-personal`（`not_applicable`）
- **THEN** `meta.usage.reason` 是 `unavailable`，`meta.usage.note` 是固定表中的一句話，`meta.usage` 的鍵集與 list 的 `usage` 相同

### Requirement: 用量一覽（GET /v1/usage）
`GET /v1/usage` SHALL 回 `{"schema": 1, "subscriptions": [...]}`，`subscriptions` 的順序與 list 相同（依 `priority`）。每一筆 MUST 恰好有下列鍵：`id`、`kind`、`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`；`session`、`weekly` 的規則同「usage 的 session 與 weekly 窗口」。每一筆的 `status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly` MUST 與 list 同一筆的 `usage` 逐值相等。

#### Scenario: 每筆的鍵集恰為十個
- **WHEN** 呼叫 `GET /v1/usage`
- **THEN** 每一筆的鍵恰為 `id`、`kind`、`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`，沒有 `pipes`、沒有其他鍵；回覆 `schema` 為 1

#### Scenario: 與 list 逐值相等
- **WHEN** 在同一個快取狀態下先呼叫 list、再呼叫 `GET /v1/usage`
- **THEN** 每一筆 `id` 對應的 `usage` 欄位（不含 `pipes`）與 `GET /v1/usage` 的同一筆逐值相等

#### Scenario: 不含 key、token 與帳號
- **WHEN** 在 key 檔放可辨識的 canary 值，跑成功、各種失敗與 `not_applicable` 後，以憑證樣式掃描 `GET /v1/usage` 回覆
- **THEN** 沒有命中；回覆中的 `id` 是政策檔的 subscription id，不是帳號或 key 名

#### Scenario: 不含 pipes 與 credits 管子
- **WHEN** OpenRouter 的快取帶有 `credits` 管子，呼叫 `GET /v1/usage`
- **THEN** 回覆沒有 `pipes` 鍵，回覆字串中找不到 `credits`，`session` 與 list 的同名窗口相同，且不含任何絕對數量

#### Scenario: 卡住的取用量不拖慢一覽
- **WHEN** 假取用量實作永遠不返回，呼叫 `GET /v1/usage`
- **THEN** 回 200，延遲與沒有用量功能時同一量級，該筆是 `unknown`／`not_fetched_yet`

#### Scenario: 單筆組裝失敗只影響該筆
- **WHEN** 某一筆在組裝 `usage` 時拋例外
- **THEN** 該筆的 `status` 為 `unknown`、`reason` 為 `internal`、`note` 為 null，其他筆照常，回 200

#### Scenario: 只接受 GET
- **WHEN** 對 `/v1/usage` 送出 `POST`、`PUT`、`DELETE` 或 `PATCH`
- **THEN** 回 404，不觸發任何取用量，不改快取

### Requirement: 用量一覽的內容邊界
`GET /v1/usage` MUST NOT 含 `pipes`、`key`、`token`、`endpoint`、`agents`、`models`、`cooldown`、`model_cooldowns`、`available`、`available_again_at`，也 MUST NOT 含帳號或憑證名稱。本 change 的 `GET /v1/usage` 不含金額；金額是否放寬見 design「未決 1」，放寬時另以 MODIFIED 處理。`id` 是政策檔的 subscription 名稱，不是帳號或 key 名。

#### Scenario: 一覽不含的欄位
- **WHEN** 呼叫 `GET /v1/usage`，快取含 `credits` 管子與冷卻
- **THEN** 回覆沒有上述任何鍵，回覆字串中找不到 `credits`，且不含任何絕對數量

### Requirement: 用量一覽的讀取語意
`GET /v1/usage` MUST 只讀快取加純計算，觸發取用量的條件 MUST 與 list 相同（見「被查到且過期才取，不等結果」）。下列既有 Requirement 對 `GET /v1/usage` 同樣適用：「觸發不等結果」、「不排程也不預取」、「用量不拖累 list 與 resolve」、「single-flight」、「用量不外洩秘密」、「管子不帶絕對數量」。

#### Scenario: 一覽與 list 同樣的觸發條件
- **WHEN** 快取過期且 key 已部署，呼叫 `GET /v1/usage`，連續十次（含多執行緒併發）
- **THEN** 取用量實作只被呼叫一次（single-flight）
