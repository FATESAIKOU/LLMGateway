# usage-reporting Specification

## Purpose
讓 list 與 resolve 的回覆帶每把 subscription 的用量（`session`／`weekly` 兩個窗口）與「再次能用的時間」。Gateway 自己取用量（Ollama Cloud 取官方 `GET /api/balance`；Claude 訂閱跑原版 Claude Code 讀 statusline 的 `rate_limits`），只在有人查、而且快取過期時才在背景取，結果放記憶體快取。**用量是附加資訊，壞了不能拖累 list／resolve。**

> 本檔的數字（TTL、逾時、退避、6 小時）與政策檔 `usage` 設定段的形狀，除「Claude TTL 下限 30 分鐘」與「被查到才取、不等」（本人 2026-10-25 裁定 C、E1、F）外，都是作者提議（design「作者提議、本人未指示」）。示例中的百分比與時間皆為假值。
## Requirements
### Requirement: usage 物件的形狀
list 的每一筆與 resolve 的 `meta` SHALL 帶一個 `usage` 物件，欄位固定為：`status`、`source`、`fetched_at`、`stale`、`reason`、`session`、`weekly`，每個都 MUST 存在（缺值用 null，不省略）。

- `status`：`ok`（至少一個窗口讀到）、`unknown`（有取用量實作，但這次讀不到，含窗口全部已過期）、`not_applicable`（這個 provider 沒有取用量實作）。
- `source`：取得來源的固定字串（Ollama Cloud 為 `ollama-api-balance`，Claude 為 `claude-statusline`）；`not_applicable` 時為 null。
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
- **WHEN** list 含 `openrouter` 與 `opencode-zen`
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

### Requirement: 窗口換算與過期
欄位白名單：Ollama 只取 `included.session`／`included.weekly` 的 `remaining_percent`、`resets_at`，其餘丟掉。Claude 的 `rate_limits.five_hour` 對應 `session`、`seven_day` 對應 `weekly`，上游給 `used_percentage`，`remaining_percent` MUST 等於 `100 - used_percentage`（不先取整），`resets_at`（epoch 秒）轉成 RFC 3339 UTC。某個窗口的 `resets_at` 已過而尚未重取時，該窗口 MUST 回 null（不假設已回到 100%）；兩個窗口都已過時 `status` 為 `unknown`、`reason` 為 `stale_expired`。

#### Scenario: Claude 用量換算
- **WHEN** statusline 的 `five_hour.used_percentage` 是 23.5、`seven_day.used_percentage` 是 61，`resets_at` 為 epoch 秒
- **THEN** `session.remaining_percent` 為 76.5、`weekly.remaining_percent` 為 39，`resets_at` 是對應的 RFC 3339 UTC 字串

#### Scenario: 單一窗口過了重置時刻
- **WHEN** 快取裡 `session.resets_at` 早於現在、`weekly.resets_at` 在未來
- **THEN** `session` 回 null，`weekly` 照舊，`status` 仍是 `ok`

#### Scenario: 兩個窗口都過了
- **WHEN** 快取裡兩個窗口的 `resets_at` 都早於現在
- **THEN** `status: unknown`、`reason: stale_expired`，`session`／`weekly` 為 null，不回舊百分比

#### Scenario: 欄位白名單
- **WHEN** Ollama 的 balance 回應除了 `included.session`／`weekly` 之外還有其他欄位（`balance_usd`、`period`、任意額外鍵）
- **THEN** list 的 `usage` 裡找不到那些欄位

### Requirement: stale 與 6 小時
`stale` SHALL 在 `now - fetched_at` 超過該 subscription 的 TTL 時為 true，否則 false；從沒成功取過為 null。`now - fetched_at` 超過 6 小時 MUST 改回 `status: unknown`、`reason: stale_expired`、`session`／`weekly` 為 null，不再顯示舊值（`fetched_at` 保留最近一次成功的時刻、`stale` 為 true）。`status: ok` 且 `stale: true` 時，`reason` MUST 是**最近一次取用量失敗的碼**；最近一次沒有失敗（只是過期、重取還沒完成）則為 null。

#### Scenario: 過了 TTL 還沒重取完
- **WHEN** Ollama 的快取 `fetched_at` 在 3 分鐘前（TTL 120 秒），重取還沒完成
- **THEN** 回舊值，`status: ok`、`stale: true`、`reason: null`，`fetched_at` 照實

#### Scenario: 過期後重取失敗
- **WHEN** 同上，但最近一次重取失敗，碼為 `upstream_error`
- **THEN** 回舊值，`status: ok`、`stale: true`、`reason: upstream_error`

#### Scenario: 超過 6 小時
- **WHEN** 最近一次成功是 6 小時又 1 分鐘前
- **THEN** `status: unknown`、`reason: stale_expired`、`stale: true`，`session`／`weekly` 為 null，`fetched_at` 仍是那次成功的時刻

#### Scenario: 剛好沒過期
- **WHEN** `now - fetched_at` 小於 TTL
- **THEN** `stale: false`，`reason: null`

### Requirement: available_again_at
list 的每一筆 SHALL 帶 `available_again_at`（RFC 3339 UTC 或 null）。其值 MUST 是下列候選中**最晚**的一個：`cooldown.until`（只算未來的）、所有 `remaining_percent` ≤ 0 的窗口之 `resets_at`（用原始數值判斷，不先取整；只看 `usage` 當下實際回出的窗口，已回 null 的窗口不算）。沒有候選為 null。key 沒部署時一律 null。`usage` 是 `unknown`／`not_applicable` 時 MUST NOT 推測（不用「現在」代替）。`available` 與冷卻的既有語意不變：額度為 0 MUST NOT 讓 `available` 變 false。resolve 的 `meta` MUST NOT 帶 `available_again_at`。

#### Scenario: 只有冷卻
- **WHEN** `cooldown.until` 為 15:00（未來），用量兩個窗口都還有餘額
- **THEN** `available_again_at` 為 15:00

#### Scenario: 只有額度用完
- **WHEN** 沒有冷卻，`weekly.remaining_percent` 為 0、`resets_at` 為明天 00:00，`session` 還有餘額
- **THEN** `available_again_at` 為明天 00:00，`available` 仍為 true

#### Scenario: 取最晚者
- **WHEN** `cooldown.until` 為 15:00，`session` 用完且 `resets_at` 為 16:30，`weekly` 用完且 `resets_at` 為 2 天後
- **THEN** `available_again_at` 為 2 天後那個 `weekly.resets_at`

#### Scenario: 冷卻比額度重置晚
- **WHEN** `session` 用完且 `resets_at` 為 16:30，`cooldown.until` 為 18:00
- **THEN** `available_again_at` 為 18:00

#### Scenario: 小數的 0
- **WHEN** `remaining_percent` 為 0.2（取整後顯示為 0）
- **THEN** 該窗口不算用完，不進入候選

#### Scenario: 未知時不推測
- **WHEN** `usage.status` 是 `unknown`，沒有冷卻
- **THEN** `available_again_at` 為 null

#### Scenario: 過去的冷卻不算
- **WHEN** `cooldown.until` 已過
- **THEN** 它不進入候選；沒有其他候選時為 null

#### Scenario: key 沒部署
- **WHEN** 該把的 key 沒部署（`available` 為 false 的原因之一）
- **THEN** `available_again_at` 為 null，即使快取裡有用量為 0 的窗口

#### Scenario: resolve 不帶
- **WHEN** resolve 成功
- **THEN** `meta` 沒有 `available_again_at`

### Requirement: 被查到且過期才取，不等結果
取用量 SHALL 只在 list 或 resolve 組裝某個 subscription 的 `usage` 時觸發，條件是：該 provider 有取用量實作、已啟用（見「政策檔的 usage 設定」）、key 已部署，而且快取不存在或 `now - fetched_at` 超過 TTL，而且不在退避期間內。觸發只是叫一條取用量執行緒去做，這次 list／resolve 的回覆 MUST NOT 等它：有舊值回舊值（`stale: true`），從沒取過回 `unknown`／`not_fetched_yet`；新值取完寫進快取，下一次查詢才看到。MUST NOT 有定時排程或啟動時預取。get 與冷卻登錄 MUST NOT 觸發取用量。resolve 回非 200（409 `cooling`、409 `no_candidate`、409 `not_deployed`、404、400 `bad_request`／`no_model`、403 `agent_not_allowed`／`model_not_allowed`）時 MUST NOT 組裝 `meta.usage`，也 MUST NOT 觸發取用量。

**原因碼的優先順序**：已被政策關閉（`enabled: false`）→ `disabled`（優先，不看 key）；其次 key 沒部署 → `no_key`。key 沒部署時，即使快取裡有舊值，`usage` 也 MUST 回 `unknown`／`no_key`、窗口為 null，不回舊值（key 已不在，舊值不再可信），且不觸發；key 重新部署後，快取沿用既有規則（未過期則直接顯示）。

#### Scenario: 快取過期的第一次查詢
- **WHEN** 快取不存在，呼叫 list，假取用量卡住不返回
- **THEN** list 立刻回 200，該筆 `usage` 是 `unknown`／`not_fetched_yet`，假取用量被呼叫一次

#### Scenario: 下一次查詢才看到新值
- **WHEN** 上一個 Scenario 的取用量完成並寫入快取，再呼叫 list
- **THEN** 該筆 `usage` 是 `status: ok`；且這一次因為快取未過期，不再觸發

#### Scenario: 快取還新鮮
- **WHEN** `now - fetched_at` 小於 TTL 時 list 或 resolve
- **THEN** 不觸發取用量

#### Scenario: 沒人查就不取
- **WHEN** lgwd 啟動後沒有任何 list 或 resolve，經過數個 TTL
- **THEN** 取用量實作一次都沒被呼叫

#### Scenario: resolve 成功時帶 usage 並觸發
- **WHEN** resolve 回 200 選中 `ollama-cloud`，快取過期
- **THEN** `meta.usage` 與 list 同形狀（只讀快取），並觸發一次背景取用量，回覆不等它

#### Scenario: resolve 非 200 不觸發
- **WHEN** resolve 分別回 409 `cooling`、409 `no_candidate`、409 `not_deployed`、404 `not_found`、400 `bad_request`、400 `no_model`、403 `agent_not_allowed` 或 403 `model_not_allowed`，且相關 subscription 的快取過期
- **THEN** 回覆不含 `usage`，取用量實作沒有被呼叫

#### Scenario: get 與冷卻登錄不觸發
- **WHEN** 呼叫 get 或登錄冷卻，快取過期
- **THEN** 取用量實作沒有被呼叫，get 的回覆沒有 `usage`／`available_again_at`

#### Scenario: key 沒部署不觸發
- **WHEN** `ollama-cloud` 的 key 沒部署，呼叫 list
- **THEN** `usage` 是 `unknown`／`no_key`，取用量實作沒有被呼叫

#### Scenario: key 沒部署但快取有舊值
- **WHEN** `ollama-cloud` 的快取有 1 分鐘前的成功值，之後 key 檔被換成不含該 key 的版本，呼叫 list
- **THEN** `usage` 是 `unknown`／`no_key`，`session`／`weekly` 為 null，不回舊值，也不觸發取用量

#### Scenario: disabled 優先於 no_key
- **WHEN** 政策檔把 `ollama-cloud` 設為 `enabled: false`，而且它的 key 沒部署
- **THEN** `usage.reason` 是 `disabled`（不是 `no_key`）

### Requirement: single-flight
同一個 subscription 同時只能有一個取用量在跑。觸發判斷、in-flight 旗標與「已停止」旗標 MUST 由同一把鎖保護；取用量執行中再被查到 MUST NOT 重複觸發。

#### Scenario: 過期後連續多次查詢只觸發一次
- **WHEN** 快取過期，取用量卡住，連續呼叫 list 與 resolve 共 10 次（含多執行緒併發）
- **THEN** 取用量實作只被呼叫一次

#### Scenario: 不同 subscription 互不影響
- **WHEN** `ollama-cloud` 的取用量卡住，同時 Claude 的取用量過期被查到
- **THEN** 各自獨立觸發

### Requirement: 失敗退避
取用量失敗（含 `rate_limited`）後 MUST 記下「下次最早可再觸發的時間」＝ max(退避, `Retry-After`)；退避間隔從 TTL 起算，連續失敗每次加倍，上限 max(30 分鐘, TTL)（`Retry-After` 本身不受此上限限制）；成功即重置。在這段時間內被查到 MUST NOT 觸發，回的仍是舊值或 `unknown`，`reason` 帶最近一次失敗的碼。上游回 429 時依 `Retry-After`（秒數）延後；沒有可解析的 `Retry-After` 時只用退避。

#### Scenario: 失敗後退避期間內不觸發
- **WHEN** Ollama 取用量失敗（`upstream_error`），TTL 120 秒，30 秒後 list
- **THEN** 不觸發取用量；`usage` 的 `reason` 是 `upstream_error`

#### Scenario: 退避結束後再觸發
- **WHEN** 同上，退避期間（120 秒）過後 list
- **THEN** 觸發一次取用量

#### Scenario: 連續失敗加倍
- **WHEN** 連續失敗五次以上，TTL 120 秒
- **THEN** 連續五次失敗後的等待依序是 120、240、480、960、1800 秒（第五次 1920 被夾到上限 max(30 分鐘, TTL)）；之後每次失敗都維持 1800 秒，不再加倍

#### Scenario: 上限隨 TTL 調高
- **WHEN** Claude 的 TTL 設為 3 小時，連續多次失敗
- **THEN** 退避上限為 3 小時（max(30 分鐘, TTL)），不是 30 分鐘

#### Scenario: 429 照 Retry-After
- **WHEN** 上游回 429、`Retry-After: 600`，退避算出來是 120 秒
- **THEN** 下次最早觸發在 600 秒後；`reason` 為 `rate_limited`

#### Scenario: Retry-After 小於退避
- **WHEN** 上游回 429、`Retry-After: 10`，退避算出來是 120 秒
- **THEN** 下次最早觸發在 120 秒後（取較大者）

#### Scenario: 成功重置
- **WHEN** 連續失敗兩次之後一次成功，之後又失敗一次
- **THEN** 這次失敗的退避回到從 TTL 起算（不是接續加倍）

### Requirement: TTL 與下限
Ollama Cloud 的預設 TTL SHALL 是 120 秒；Claude 的預設 TTL SHALL 是 30 分鐘。政策檔可以調整 TTL，但有下限：Claude（`claude-subscription`）MUST NOT 低於 30 分鐘，Ollama Cloud MUST NOT 低於 60 秒（官方限額 10 次／分鐘、跨 key 與裝置共用，建議每分鐘最多查一次）。設定值低於下限時 MUST 夾到下限並記一筆 log（不含任何 key 值）；可以調高。

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
- **THEN** Ollama 的 TTL 為 120 秒、Claude 的 TTL 為 1800 秒

### Requirement: 政策檔的 usage 設定
政策檔 MAY 有頂層 `usage` 物件，鍵為 subscription id，值為 `{"enabled": <布林>, "ttl_seconds": <正整數>}`（兩欄都選填）。未設定時：有取用量實作且 `kind` 為 `claude-subscription` 的預設 `enabled: false`，其餘有實作的預設 `enabled: true`。下列情形政策載入 MUST 失敗（`PolicyError`），lgwd 不啟動（fail-closed，與既有政策檔對未知鍵的處理一致）：`usage` 本身不是 object；`usage` 的鍵不是政策檔裡的 subscription；某個值不是 object；值裡有 `enabled`、`ttl_seconds` 以外的未知鍵（例如打錯成 `enable`）；`enabled` 不是布林；`ttl_seconds` 不是正整數。沒有取用量實作的 provider 設了 `usage` 不報錯，仍是 `not_applicable`。

#### Scenario: Claude 預設關閉
- **WHEN** 政策檔沒有 `usage` 段，list 含 `claude-personal`，快取為空
- **THEN** 它的 `usage` 是 `status: unknown`、`source: claude-statusline`、`reason: disabled`，不觸發取用量（不起任何子行程）

#### Scenario: 開啟 Claude
- **WHEN** 政策檔 `usage.claude-personal.enabled` 為 true、key 已部署，list 在快取過期時被呼叫
- **THEN** 觸發一次背景取用量；這次回覆的 `reason` 是 `not_fetched_yet`

#### Scenario: 關閉 Ollama
- **WHEN** 政策檔 `usage.ollama-cloud.enabled` 為 false
- **THEN** `ollama-cloud` 的 `usage` 是 `unknown`／`disabled`，不觸發取用量

#### Scenario: usage 或其值不是 object
- **WHEN** 政策檔的 `usage` 是陣列或字串，或 `usage.ollama-cloud` 是 `true`
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

#### Scenario: 內層未知鍵
- **WHEN** `usage.ollama-cloud` 寫成 `{"enable": false}`（打錯鍵）
- **THEN** 政策載入失敗（`PolicyError`），不會靜默忽略而讓 Ollama 仍然啟用

#### Scenario: 不合法的設定
- **WHEN** `usage` 含不存在的 subscription id、`enabled` 為字串，或 `ttl_seconds` 為 0 或負數
- **THEN** 政策載入失敗（`PolicyError`），lgwd 不啟動

### Requirement: Ollama Cloud 取用量
Ollama 取用量 SHALL 對寫死在程式裡的 `https://ollama.com/api/balance` 發 `GET`（URL 不來自政策檔、不跟隨轉址到別的主機），以 vault 的 key 當 `Authorization: Bearer`，逾時 5 秒，不重試。key MUST 只放進該次請求的 header，不進 log、例外訊息、回覆。每次取之前 MUST 先 `reload_keys_if_changed()`，使換 key 後用新 key。

#### Scenario: 請求形狀
- **WHEN** 以假 HTTP 層捕捉一次 Ollama 取用量
- **THEN** 方法為 GET、主機與路徑為 `ollama.com` 的 `/api/balance`、帶 `Authorization: Bearer <key>`，沒有其他位址的請求

#### Scenario: 轉址不跟隨
- **WHEN** 上游回 302 指向其他主機
- **THEN** 不對那個主機發請求；`usage.reason` 為 `upstream_error`

#### Scenario: 逾時
- **WHEN** 上游超過 5 秒沒回應
- **THEN** 取用量結束、記失敗碼 `timeout`，不重試

#### Scenario: 換 key 後用新值
- **WHEN** key 檔在兩次取用量之間被換掉
- **THEN** 第二次請求帶的是新 key

### Requirement: Claude 取用量
Claude 取用量 SHALL 執行**未修改的原版 Claude Code** 並讀其 statusline 的 `rate_limits`（`five_hour`／`seven_day`）；Gateway MUST NOT 自己用訂閱 token 對 Anthropic 的 API 或沒有文件的 usage endpoint 發請求，MUST NOT 收集或轉手 session token。**程式**在政策沒有設定時預設關閉（`enabled: false`）；**出廠政策檔**（`policy/default.json`）自 #60 階段 C 起開啟 Claude（經本人授權）；部署前需完成 10.1／10.2 與 C1（階段 C 的前置，即原版 Claude Code、trust 固定目錄、服務的 PATH 與 `LGW_USAGE_CLAUDE_CWD` 或 `--usage-claude-cwd`），未完成時 `usage` 會是 `unknown`／`upstream_error`。只用管道 1（statusline）；取不到就是 `unknown`，MUST NOT 再找別條路。管道 2（`-p` 下的 `/usage`）於 2026-10-25 階段 B 探測 (b) 實測不可用（只回 session cost、沒有窗口），不採用。子行程規則：
- token MUST 只以子行程環境變數 `CLAUDE_CODE_OAUTH_TOKEN` 傳入，MUST NOT 出現在 argv、檔案；
- 子行程環境用白名單，MUST NOT 繼承服務的環境；
- 子行程 stdout／stderr MUST NOT 進 log，只留 `rate_limits` 的四個數字欄位；解析失敗（欄位缺、型別不對、非 Pro／Max）→ `unknown`（`parse_error`／`no_windows`），MUST NOT 補 0；
- 逾時 60 秒；子行程以自己的行程群組啟動，逾時只終止**自己記下 pid 的那個群組**，MUST NOT 依名稱殺行程；
- 找不到 Claude Code 可執行檔時為 `unknown`／`upstream_error`，不拋例外；
- 子行程的 cwd MUST 是 lgwd `Config` 指定的**固定**目錄（環境變數 `LGW_USAGE_CLAUDE_CWD`，CLI 參數 `--usage-claude-cwd`，欄位 `usage_claude_cwd`；**不放政策檔**；部署時事先 trust 的就是這個路徑，見 tasks 10.2），MUST NOT 每次新建空目錄（沒接受 workspace trust 時 statusline 一律是空的）；沒設定、目錄不存在或不可用時為 `unknown`／`upstream_error`，不起子行程、不自動建立；
- 暫存輸出放服務帳號自己目錄下的固定前綴子目錄；lgwd 啟動時掃掉前綴相符的舊殘留。

#### Scenario: 用假的 claude 取到用量
- **WHEN** 已啟用，假的 Claude Code 可執行檔把含 `five_hour`（used 25）、`seven_day`（used 50）的 `rate_limits` JSON 交給 statusline 命令
- **THEN** 取用量完成後 `usage` 為 `status: ok`、`source: claude-statusline`，`session.remaining_percent` 為 75、`weekly.remaining_percent` 為 50

#### Scenario: 欄位缺席
- **WHEN** 假的輸出沒有 `rate_limits`（非 Pro／Max，或第一個 API 回應前）
- **THEN** `usage` 為 `unknown`／`no_windows`，窗口為 null，不是 0

#### Scenario: 解析失敗
- **WHEN** 假的輸出 `used_percentage` 是字串或 JSON 損壞
- **THEN** `unknown`／`parse_error`，窗口為 null

#### Scenario: token 只在環境變數
- **WHEN** 檢查假 runner 收到的 argv、環境與工作檔
- **THEN** token 只出現在環境變數 `CLAUDE_CODE_OAUTH_TOKEN`；argv 與檔案裡都沒有；環境裡沒有服務本身環境的其他變數（例：設在測試行程的假 `GH_TOKEN`）

#### Scenario: 逾時只收自己的行程群組
- **WHEN** 假的子行程超過 60 秒不結束（測試以縮短的逾時模擬）
- **THEN** 該子行程所在的行程群組被終止，`usage` 的失敗碼為 `timeout`；測試行程以及另一個無關的同名行程不受影響

#### Scenario: 固定的工作目錄
- **WHEN** 連續兩次取用量，以假 runner 記錄子行程的 cwd
- **THEN** 兩次的 cwd 都是 `Config.usage_claude_cwd` 指定的同一個固定路徑，不是每次新建的暫存目錄

#### Scenario: 工作目錄不存在
- **WHEN** `usage_claude_cwd` 指定的目錄不存在，或根本沒設定
- **THEN** `usage` 是 `unknown`／`upstream_error`，不起子行程、不自動建立該目錄，list 仍回 200

#### Scenario: 找不到可執行檔
- **WHEN** 已啟用但服務帳號找不到 Claude Code
- **THEN** `usage` 是 `unknown`／`upstream_error`，list 仍回 200，lgwd 沒有例外

#### Scenario: 子行程輸出不進 log
- **WHEN** 假的子行程在 stdout／stderr 印出含 `canary` 字樣的內容後結束
- **THEN** lgwd 的 log 裡找不到那段內容

#### Scenario: 啟動時掃殘留
- **WHEN** 暫存前綴目錄下有舊的殘留檔，lgwd 啟動
- **THEN** 殘留檔被清除，前綴不符的檔案不動

### Requirement: stop 之後不觸發，並收掉子行程
`Gateway.stop` SHALL 先設「已停止」旗標，之後 MUST NOT 觸發任何取用量（旗標與觸發在同一把鎖內）；spawn 子行程與登記其 pid MUST 在同一把鎖內，spawn 前再檢查一次旗標。stop MUST **一律終止**仍在跑的取用量子行程，不等它跑完：SIGTERM→join→SIGKILL，只終止自己記下 pid 的群組；順序在 `api_server.shutdown()` 之前。

#### Scenario: stop 後再查不觸發
- **WHEN** 呼叫 `Gateway.stop()` 之後，仍在處理的 list 讓快取過期的 subscription 被查到
- **THEN** 取用量實作沒有被呼叫，沒有新的子行程

#### Scenario: 收掉在跑的子行程
- **WHEN** 有一個取用量子行程正在跑（假的長時間子行程），呼叫 `stop()`
- **THEN** 該子行程的行程群組被終止，`stop()` 在有限時間內返回，沒有殘留行程

#### Scenario: stop 與 spawn 交錯
- **WHEN** 觸發判斷通過之後、spawn 之前呼叫 `stop()`
- **THEN** 不會 spawn 子行程（或 spawn 了也被登記並收掉），不留殘留

#### Scenario: keysync 行為不變
- **WHEN** 抽出子行程護欄（起停、killpg、stderr 尾端）後跑 keysync 的既有測試
- **THEN** `KeySyncTimer` 的對外行為與狀態檔不變，既有測試維持通過

### Requirement: 用量不拖累 list 與 resolve
list 與 resolve 組裝 `usage` 時 SHALL 只做字典讀取、純計算（`available_again_at`、`stale`）與「是否觸發」判斷（旗標檢查＋建一條執行緒），MUST NOT 做網路或子行程 I/O，MUST NOT 等取用量執行緒。取用量的任何例外 MUST 在執行緒內捕捉並轉成 `unknown`；組裝 `usage` 欄位與觸發判斷本身 MUST 再包一層 try，失敗時該筆的 `usage` 回 `unknown`／`internal`，`available_again_at` 回 null，list 與 resolve 仍回 200，且 resolve 的 `env`、選把結果不受影響。

#### Scenario: 取用量卡住
- **WHEN** 假取用量實作永遠不返回，連續呼叫 list 與 resolve
- **THEN** 每次的延遲與沒有用量功能時相同量級（不等取用量），回應內容除 `usage`／`available_again_at` 外與沒有用量功能時一致

#### Scenario: 取用量拋例外
- **WHEN** 假取用量實作拋出例外
- **THEN** list 與 resolve 仍回 200，`env` 與 `meta.subscription` 不變；該筆 `usage` 為 `unknown`，lgwd 不當掉

#### Scenario: 組裝 usage 本身出錯
- **WHEN** 組裝路徑出錯（例如假快取物件在讀取時拋例外）
- **THEN** 該筆 `usage` 為 `unknown`／`internal`、`available_again_at` 為 null，其他欄位與其他筆不受影響，list 回 200

#### Scenario: 選把不受用量影響
- **WHEN** `claude-personal` 的用量為 0%（`remaining_percent` 0）而沒有冷卻，resolve 一個只有它符合的請求
- **THEN** 仍回 200 並選它（本人 B：不因用量自動標成不可用）；不自動登錄冷卻

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
- **THEN** 只有對 `ollama.com` 的 `/api/balance`；Claude 的外連由原版 Claude Code 自己發出，lgwd 沒有為 Claude 另開連線

