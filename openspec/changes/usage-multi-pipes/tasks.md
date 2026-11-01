## 前置
- [x] `openrouter-usage-fetcher` 先歸檔

## 契約（C-n；實作報告逐條對到測試名稱）
- C-1：任何 `usage`（含 `not_applicable`、`unknown`）鍵集恰為 `status, source, fetched_at, stale, reason, session, weekly, pipes`；`pipes` 為陣列（無管子＝`[]`）。
- C-2：Claude 兩窗口讀到時，`pipes` 有 `five_hour`、`seven_day`，其 `remaining_percent`／`resets_at` 與 `session`／`weekly` 逐值相等；`models` 為 `["*"]`。注意：不可把 `credits` 加進 `WINDOWS`（`usage.py:33`；它會被 `available_again_at` 與 `session`／`weekly` 導出迴圈拿去用）。
- C-3：Ollama 讀到 session／weekly 時，`pipes` 有 `session`、`weekly` 且逐值相等；新方案（只有金額）仍 `unknown／no_windows`，`pipes: []`。
- C-4：OpenRouter `free_model_daily_requests` 讀到 → `pipes` 有 `free_daily`（models `["*:free","openrouter/free"]`），`session` 與其逐值相等（現狀不變）。
- C-5：OpenRouter `limit` 為正數、`limit_remaining` 在 0～`limit` → 多一根 `credits`（percent＝`limit_remaining/limit*100`、`resets_at` null、models_except 為 free 樣式）；`session` 仍只由 `free_daily` 導出；`limit` 或 `limit_remaining` 為 null → 沒有 `credits`，不影響 `status`。
- C-6：只有 `credits` 讀到、`free_model_daily_requests` 缺 → `status: ok`，`session` 為 null，`pipes` 只有 `credits`。
- C-7（注意：`resets_at` 為 None 時不能做 `window[1] > now`，要先判 None）：管子 `resets_at` 已過 → 不在 `pipes`；`session`／`weekly` 行為同現行；全部都沒有 → `unknown／stale_expired`。`resets_at` 為 null 的管子，超過 6 小時一併消失。
- C-8：`available_again_at` 算法不變：只有 `credits` 為 0 時仍為 null；`session`／`weekly` 為 0 時與現行相同。
- C-9：`not_applicable`（zen、go、antigravity）的 `pipes` 是 `[]`，不觸發取用量。
- C-10：回應（含 `pipes`）找不到 key／token、絕對值 canary（`limit` 12345.678、`limit_remaining` 4938.2712）、上游原文標籤。
- C-11：credits 異常值（`limit_remaining` 負數、大於 `limit`、型別不對、`limit` ≤ 0）→ 沒有 `credits`，`free_daily`／`session` 仍在，`status: ok`，無退避（`failure()` 未被呼叫）。注意：OpenRouter `_parse` 的 NO_WINDOWS 判斷與 `FetchResult` docstring 要納入 `credits`（只有 credits 讀到時不是 no_windows）。
- C-12：`credits.resets_at` 恆為 null（即使回應帶任何重置週期欄位）。

## 實作
- [x] `lgwd/usage.py`：固定表 `PIPE_TABLE`；`FetchResult.windows` 容許選填 `credits`；`build_usage` 產生 `pipes`；`_not_applicable`／`_unknown`／`internal_usage` 補 `pipes: []`；null `resets_at` 的過期處理。
- [x] `lgwd/usage_openrouter.py`：解析 credits（異常只丟棄）。
- [x] 主 spec delta、測試。
- [x] 改 SHAPE 為八鍵：`tests/test_usage.py:23`（使用處 `:45`、`:263`）、`tests/test_registry.py:2514`（使用處 `:2552`、`:2710`）。
- [x] `openspec validate usage-multi-pipes` 綠。

## 測試策略
單元層：fetcher 以假 HTTP／假 rate 檔餵固定回應，驗 `FetchResult` 與 credits 異常值；`build_usage` 純函式驗過期與導出；list／resolve 層一個整合測試驗鍵集與 `available_again_at` 不變。替身：沿用現有 `http_get` 注入與假時鐘。驗收門檻由 PM 定。
