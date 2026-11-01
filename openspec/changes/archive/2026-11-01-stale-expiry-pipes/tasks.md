# tasks：#147 stale 與 6 小時補上 pipes

## 1. spec delta

- [x] 1.1 新增 `openspec/changes/stale-expiry-pipes/specs/usage-reporting/spec.md`：MODIFIED「stale 與 6 小時」，原文整條照抄主 spec，只在「超過 6 小時 MUST 改回 … `session`／`weekly` 為 null」補「、`pipes` 為 `[]`」；保留既有所有 Scenario，另外新增一個 Scenario：超過 6 小時之後 `pipes` 為 `[]`（含 `resets_at` 為 null 的 credits 管子也一起消失）。
- [x] 1.2 `openspec validate stale-expiry-pipes` 與 `openspec validate --specs` 皆綠。

## 2. 測試

- [x] 2.1 確認 `tests/test_usage.py` 已有「超過 6 小時 → `pipes == []`」斷言（`test_C7_管子resets_at過期與null過期處理`）；本 change 不新增測試、不改 `lgwd/`。

## 3. 文件

- [x] 3.1 `docs/deploy.md` 約第 368 行：改寫「兩份清單裡不能放同一個 id，否則照樣會互相放行」為「allow 是每把各自判斷，同一個 id 放在兩份清單裡，代表兩把都可以用它；要分開控制的是『萬用字元會把上游新增的模型一起放進來』這件事」，其餘不動。
