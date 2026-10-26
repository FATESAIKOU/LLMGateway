對應工單：#85（層級 d）「openrouter 的 usage fetcher：讀 /api/v1/key 的剩餘」。

## Why

OpenRouter 提供 `GET /api/v1/key` 查詢剩餘（含 `free_model_daily_requests` 與 `limit_remaining`），查詢本身不吃免費請求數。Gateway 目前對 openrouter 的 `usage` 是 `not_applicable`，無法看到剩餘。本 change 把 OpenRouter 列為有取用量實作的 provider，source 為 `openrouter-api-key`。

## What Changes

- `lgwd/usage_openrouter.py` 新增 `OpenRouterUsageFetcher`，讀 `https://openrouter.ai/api/v1/key`。
- `lgwd/providers.py` 的 `OpenRouterProvider.make_usage_fetcher` 回傳它。
- spec `usage-reporting`：source 清單加 OpenRouter；「沒有用量來源的 provider」情境改為 `opencode-zen`／`opencode-go`；新增「OpenRouter 取用量」Requirement。

## What Does Not Change

- 不改 `usage` 物件的七鍵形狀、不改 TTL／退避／single-flight 機制。
- 不把 `limit_remaining` 等其他欄位塞進 usage（沒有對應窗口形狀）。

## Impact

- openrouter 的 `usage` 不再是 `not_applicable`；預設啟用（`default_enabled = True`），TTL 預設 120 秒、下限 60 秒。