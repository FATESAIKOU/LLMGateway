## MODIFIED Requirements

### Requirement: 用量一覽（GET /v1/usage）
`GET /v1/usage` SHALL 回 `{"schema": 1, "subscriptions": [...]}`，`subscriptions` 的順序與 list 相同（依 `priority`）。每一筆 MUST 恰好有下列鍵：`id`、`kind`、`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`、`weekly_used_usd`、`weekly_used_usd_fetched_at`；`session`、`weekly` 的規則同「usage 的 session 與 weekly 窗口」。每一筆的 `status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly` MUST 與 list 同一筆的 `usage` 逐值相等。`weekly_used_usd` 與 `weekly_used_usd_fetched_at` 的規則同「OpenRouter 本週已用 USD」。

#### Scenario: 每筆的鍵集恰為十個
- **WHEN** 同快取狀態查 list 與一覽
- **THEN** 一覽每筆的鍵恰為 `id`、`kind`、`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`、`weekly_used_usd`、`weekly_used_usd_fetched_at` 十二鍵，沒有 `pipes`、沒有其他鍵；回覆 `schema` 為 1，原八個 usage 欄位與 list 逐值相等

#### Scenario: 不含 key、token 與帳號
- **WHEN** 在 key 檔放可辨識的 canary 值，跑成功、各種失敗與 `not_applicable` 後，以憑證樣式掃描 `GET /v1/usage` 回覆
- **THEN** 沒有命中；回覆中的 `id` 是政策檔的 subscription id，不是帳號或 key 名

#### Scenario: 卡住的取用量不拖慢一覽
- **WHEN** 假取用量實作永遠不返回，呼叫 `GET /v1/usage`
- **THEN** 回 200，延遲與沒有用量功能時同一量級，該筆是 `unknown`／`not_fetched_yet`

#### Scenario: 只接受 GET
- **WHEN** 對 `/v1/usage` 送出 `POST`、`PUT`、`DELETE` 或 `PATCH`
- **THEN** 回 404，不觸發任何取用量，不改快取

#### Scenario: 與 list 逐值相等
- **WHEN** 在同一個快取狀態下先呼叫 list、再呼叫 `GET /v1/usage`
- **THEN** 每一筆 `id` 對應的 `usage` 欄位（不含 `pipes`）與 `GET /v1/usage` 的同一筆逐值相等

#### Scenario: 不含 pipes 與 credits 管子
- **WHEN** OpenRouter 的快取帶有 `credits` 管子，呼叫 `GET /v1/usage`
- **THEN** 回覆沒有 `pipes` 鍵，回覆字串中找不到 `credits`，`session` 與 list 的同名窗口相同，且不含任何絕對數量（唯一例外是 OpenRouter 的 `weekly_used_usd`）

#### Scenario: 單筆組裝失敗只影響該筆
- **WHEN** 某一筆在組裝 `usage` 時拋例外
- **THEN** 該筆的 `status` 為 `unknown`、`reason` 為 `internal`、`note` 為 null，該筆的 `weekly_used_usd` 與 `weekly_used_usd_fetched_at` 為 null，其他筆照常，回 200

### Requirement: 用量一覽的內容邊界
`GET /v1/usage` MUST NOT 含 `pipes`、`key`、`token`、`endpoint`、`agents`、`models`、`cooldown`、`model_cooldowns`、`available`、`available_again_at`，也 MUST NOT 含帳號或憑證名稱。除 OpenRouter 的 `weekly_used_usd` 外，任何訂閱及欄位 MUST NOT 含金額；其他絕對數量仍不回。`id` 是政策檔的 subscription 名稱，不是帳號或 key 名。

#### Scenario: 一覽不含的欄位
- **WHEN** 呼叫 `GET /v1/usage`，快取含 `credits` 管子與冷卻
- **THEN** 回覆沒有上述任何鍵，回覆字串中找不到 `credits`，且不含任何絕對數量（唯一例外是 OpenRouter 的 `weekly_used_usd`）

#### Scenario: 金額 canary 只在指定欄位出現
- **WHEN** 假上游在 OpenRouter 與其他訂閱放不同金額 canary，另在 credits、帳號與憑證欄位放 canary
- **THEN** 只有 `weekly_used_usd` 可出現該金額；其他金額、credits、帳號及憑證 canary 均不出現

### Requirement: usage 的 session 與 weekly 窗口
`usage.session` 與 `usage.weekly` MUST 是 `{"remaining_percent": 數字, "resets_at": RFC 3339 UTC}` 或 null。`remaining_percent` 保留原始小數，範圍 0～100。讀不到的窗口 MUST 是 null，**絕不用 0 代替**。這兩個鍵是相容層，語意與值不隨 `pipes` 的加入而改變。

#### Scenario: 讀到兩個窗口
- **WHEN** Ollama 的 balance 回 `included.session`（剩 75%、`resets_at` 2026-10-25T07:00:00Z）與 `included.weekly`（剩 40%、`resets_at` 2026-10-28T00:00:00Z），取完後再 list
- **THEN** 該筆 `usage` 為 `status: ok`、`source: ollama-api-balance`、`stale: false`、`reason: null`，`fetched_at` 是取得時刻，`session.remaining_percent` 為 75、`weekly.remaining_percent` 為 40，`resets_at` 都是 RFC 3339 UTC

#### Scenario: 只讀到一個窗口
- **WHEN** 上游只給 `weekly`
- **THEN** `status: ok`，`session` 是 null，`weekly` 有值；`session` 的 `remaining_percent` 不會是 0

#### Scenario: 方案沒有 session／weekly
- **WHEN** Ollama 的 balance 回應沒有 `included.session` 也沒有 `included.weekly`（新方案，只有 `balance_usd`／`allowance_usd`），取完後再 list
- **THEN** `usage` 是 `status: unknown`、`reason: no_windows`，`session`／`weekly` 為 null，`pipes` 為 `[]`，list 回應中的 `usage` 不帶金額（沒有 `credits` 絕對值）

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
- **THEN** 沒有命中（唯一例外是 `GET /v1/usage` 的 `weekly_used_usd`）

#### Scenario: resolve 的 meta.usage 帶 note
- **WHEN** resolve 選中 `chatgpt-personal`（`not_applicable`）
- **THEN** `meta.usage.reason` 是 `unavailable`，`meta.usage.note` 是固定表中的一句話，`meta.usage` 的鍵集與 list 的 `usage` 相同

## ADDED Requirements

### Requirement: OpenRouter 本週已用 USD
一覽每筆 MUST 有 `weekly_used_usd` 與 `weekly_used_usd_fetched_at`。僅 OpenRouter fetcher 可回上游 `data.usage_weekly` 的有限非負 JSON 數字與該值取得時刻（RFC 3339 UTC）；其他訂閱兩者均為 null。兩值只存記憶體、成對更新；缺值、失敗、停用、key 未部署、內部例外或超過六小時時均為 null。金額所屬 UTC 週與讀取時的 UTC 週不同時，兩值亦均為 null。金額獨立於窗口成功與 status，不改窗口結果及觸發規則。

#### Scenario: 有值與零
- **WHEN** OpenRouter 的有效 `usage_weekly` 分別為 1.25 與 0
- **THEN** 一覽分別回 1.25 與 0 及各自取得時刻；零不是 null

#### Scenario: 無效欄位
- **WHEN** 欄位缺、為 null、字串、布林、負數、NaN、Infinity 或非有限數
- **THEN** 金額與時間皆為 null，其他有效窗口不受影響，不回顯上游值

#### Scenario: 窗口與金額獨立
- **WHEN** 金額合法而窗口全部缺失或解析失敗
- **THEN** 金額與其取得時刻仍回出，窗口 status 與 reason 照既有規則；反之金額無效不影響有效窗口

#### Scenario: 失敗與舊資料
- **WHEN** 先前有金額，後續上游失敗、整體結構無效、欄位缺失、停用或 key 未部署，或金額超過六小時
- **THEN** 金額與時間均為 null，不以 0 或舊金額代替

#### Scenario: 跨週失效
- **WHEN** 金額取得於上一個 UTC 週，現在已是新的 UTC 週（週一 00:00 UTC 之後）
- **THEN** 一覽的 `weekly_used_usd` 與 `weekly_used_usd_fetched_at` 均為 null，不以上週的值代替

#### Scenario: 邊界與秘密
- **WHEN** 假資料在非 OpenRouter 訂閱、pipes、note 與上游其他欄位注入不同金額、key、token 和帳號 canary
- **THEN** list 與 resolve 的 usage、pipes、note 均無金額及秘密；一覽僅指定欄位可回合法金額，log 與稽核不含 canary；resolve 原有 env 除外
