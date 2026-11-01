對應工單：#139（層級 c）的第 2 輪結論；實作另開 d。

## Why

本人 2026-11-01 說明：每把 key 的用量管子**不只一根，也不只分時間窗**——同一把 key 底下有多個獨立的額度桶，各有百分比與重置時刻（例：Antigravity 一個登入下有 Gemini、Claude／GPT、Argon 三組；OpenRouter 有每日免費請求數與 credits）。目前 `usage` 只有固定的 `session`／`weekly` 兩個窗口（主 spec `usage-reporting` 的「usage 物件的形狀」；`lgwd/usage.py:33` `WINDOWS = ("session", "weekly")`），表達不了「第三根」，也沒有「這根管子管哪些模型」。

## What Changes

- `usage` 物件**加一個鍵 `pipes`**（七鍵變八鍵）：陣列，每根管子 `{id, models, models_except, remaining_percent, resets_at}`；沒有管子時為空陣列 `[]`。
- `session`／`weekly` **保留**為相容層，語意與值不變（Claude／Ollama／OpenRouter 原樣；它們同時也是 `pipes` 的前兩根或第一根）。
- `pipes` 由 `build_usage` 從既有的 `windows`（`FetchResult` 多一個選填的 `credits` 項）加上程式內的固定表（source→id／models）產生；`session`／`weekly` 的導出路徑不動，不另設 compat 間接層。
- OpenRouter fetcher 多出 credits 管子（`limit` 為正數時；`resets_at` 恆為 null）。
- 不改：端點（不新增）、TTL／退避／single-flight、`available_again_at` 的算法（仍只看 `session`／`weekly`）、`stale`／`reason` 語意、antigravity 的 `not_applicable`。

## 誠實的範圍（實際受益者）

- **本 change 實際新增的管子只有 OpenRouter credits。** Claude、Ollama 的管子只是把既有 `session`／`weekly` 再列一次。
- **Antigravity 三組（Gemini／Claude／Argon）不在 Gateway**：Gateway 沒有它的 fetcher，`/usage` 只能在 Mac 上跑。本人要求的核心情境因此不由本 change 滿足，由 sidecar 取。
- sidecar 的 `UsageWindow`（name／used_percent／scope／applies）與這裡的 pipe 形狀不同；兩邊是否共用一個形狀尚待本人／MyLinuxPool 決定（設計文件 J-9）。
- 讀者（sidecar）目前只讀 `session`／`weekly` 與 `available_again_at`，不讀 `pipes`；要另開單才會消費。在那之前 credits 用完對挑 key 沒有效果（J-10：要不要讓 resolve／`available_again_at` 在本 change 就看 `pipes`）。

## What Does Not Change

- 不回絕對數量（credits／請求數；本人 J-2＝百分比就夠）；不回任何 key 值。
- `session`／`weekly` 不改名、不改值、不改 null 語意；既有讀它們的 sidecar 不受影響。
- Antigravity 不在本 change 取用量（見 design §6）。

## 前置

`openrouter-usage-fetcher` 要先歸檔：本 change 的 MODIFIED 以它的版本為底（含 `openrouter-api-key`、情境改為 zen／go）。

## Impact

- `pipes` 是新增鍵。審查者讀 sidecar 當前程式：它只用 `usage.get(...)`，不驗鍵集（其測試是否斷言鍵集未驗）。
- 測試：斷言 `usage` 鍵集的 SHAPE 要改為八鍵：`tests/test_usage.py:23,45,263`、`tests/test_registry.py:2514,2552,2710`。
