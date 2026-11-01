# tasks：#156 usage-reporting spec 清掉 get 的殘留敘述

## 1. spec delta

- [x] 1.1 新增 `openspec/changes/usage-drop-get-residue/specs/usage-reporting/spec.md`：MODIFIED「被查到且過期才取，不等結果」，原文整條照抄主 spec，只把「get 與冷卻登錄 MUST NOT 觸發取用量。」改成「冷卻登錄 MUST NOT 觸發取用量。」；Scenario「get 與冷卻登錄不觸發」名稱保持不變，WHEN 改成「登錄冷卻，快取過期」，THEN 只留「取用量實作沒有被呼叫」。
- [x] 1.2 `openspec validate usage-drop-get-residue` 與 `openspec validate --specs` 皆綠。

## 2. 測試

- [x] 2.1 確認 `tests/test_registry.py` 已有「登錄冷卻不觸發取用量」斷言（`test_登錄冷卻不觸發取用量`）；本 change 不新增測試、不改 `lgwd/`。

## 3. 驗證

- [x] 3.1 把 delta 那條 Requirement 與主 spec 同名的那條做 diff，確認只有上面兩處不同。
- [x] 3.2 `git status --short` 乾淨（只含本 change 的新檔）。
