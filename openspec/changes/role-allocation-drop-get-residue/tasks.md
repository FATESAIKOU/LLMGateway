# tasks：#162 role-allocation 主 spec 清掉 get 退場後的殘留

## 1. spec delta

- [x] 1.1 新增 `openspec/changes/role-allocation-drop-get-residue/specs/role-allocation/spec.md`：
  - REMOVED「過渡期的 get 不擋」：附 Reason（#48：單筆 get 移除）與 Migration（改用 resolve；單筆路徑已回 404）。
  - MODIFIED「roles 為 advisory」：原文整條照抄主 spec，只將 Scenario「帶 data 的 resolve 仍擋釘住」THEN 結尾「（`get` 是另一回事，見 `subscription-registry`）」刪除。
- [x] 1.2 `openspec validate role-allocation-drop-get-residue --strict` 與 `openspec validate --specs --strict` 皆綠。

## 2. 測試與程式

- [x] 2.1 確認現行程式（`lgwd/`）與測試（`tests/`）不需變動；本 change 不新增測試、不改 `lgwd/`。

## 3. 驗證

- [x] 3.1 比對 MODIFIED 的 Requirement 與主 spec 同名區塊，確認僅刪除 Scenario THEN 結尾的殘留括號文字。
- [x] 3.2 比對 REMOVED 的 Requirement，確認對應主 spec 移除「過渡期的 get 不擋」。
- [x] 3.3 `git status --short` 乾淨（只含本 change 的新檔）。
