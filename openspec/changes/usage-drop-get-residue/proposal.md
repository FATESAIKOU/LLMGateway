對應工單：LLMGateway#156（層級 d）「usage-reporting spec：清掉 get 退場後的殘留敘述」；相關 #48、#147。

## Why

#48（get 退場）的 openspec delta 只涵蓋 subscription-registry、key-vault、connectivity，沒有涵蓋 usage-reporting。所以歸檔之後，`openspec/specs/usage-reporting/spec.md` 還有把 get 當成現存介面的敘述：
- 約第 129 行：「get 與冷卻登錄 MUST NOT 觸發取用量」；
- 約第 157～159 行：Scenario「get 與冷卻登錄不觸發」，WHEN 寫「呼叫 get 或登錄冷卻」，THEN 寫「get 的回覆沒有 usage」。

## What Changes

- `openspec/specs/usage-reporting/spec.md` 的 Requirement「被查到且過期才取，不等結果」：MODIFIED，拿掉 get 的部分（保留冷卻登錄不觸發取用量的規定）；Scenario 的名稱保持不變，WHEN 改成只講「登錄冷卻，快取過期」，THEN 只留「取用量實作沒有被呼叫」。
- 不改 `lgwd/`；只改 spec 文字。
- 不歸檔此 change。

## What Does Not Change

- 不改對外介面、不改模組界線、不改行為；get 端點本來就不存在（一律 404）。
- 不改其他 Requirement 與 Scenario；原有的 Scenario 全部保留。

## Impact

- 主 spec `usage-reporting` 不再把 get 當成現存介面。
- 測試：確認 `tests/test_registry.py` 已有「登錄冷卻不觸發取用量」（`test_登錄冷卻不觸發取用量`）；本 change 不新增測試、不改 `lgwd/`。
