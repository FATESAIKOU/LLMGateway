對應工單：#147（層級 d）「usage-reporting spec：『stale 與 6 小時』補上 pipes（交叉引用）」；相關 #142、#139。

## Why

#142 歸檔審查發現：主 spec `openspec/specs/usage-reporting/spec.md` 的 Requirement「管子（pipes）」寫「`resets_at` 為 null 的管子依『stale 與 6 小時』一併消失」，但「stale 與 6 小時」那條 Requirement 只寫「`session`／`weekly` 為 null」，沒有提到 `pipes`，交叉引用落空。實作（`lgwd/usage.py` 的 6 小時過期分支走 `_unknown`，會帶 `pipes: []`）符合，已由 test_C7 覆蓋，只差 spec 沒寫。


## What Changes

- `openspec/specs/usage-reporting/spec.md` 的 Requirement「stale 與 6 小時」：MODIFIED，把「超過 6 小時 MUST 改回 `status: unknown`、`reason: stale_expired`、`session`／`weekly` 為 null」補成「…為 null、`pipes` 為 `[]`」；保留既有所有 Scenario，並新增一個 Scenario：超過 6 小時之後 `pipes` 為 `[]`（含 `resets_at` 為 null 的 credits 管子也一起消失）。
- 不改 `lgwd/`；只補 spec 文字與測試。

## What Does Not Change

- 不改對外介面、不改模組界線、不改行為；`pipes` 在 6 小時過期時本就為 `[]`。
- 不歸檔此 change。

## Impact

- 交叉引用「管子（pipes）」與「stale 與 6 小時」一致。
- 測試：確認 `tests/test_usage.py` 已覆蓋「超過 6 小時 → `pipes == []`」；已有則不動。
