# tasks：model-rotation-survey 的 AA effort 檔位口徑（#176，PR 1／2）

## 1. SKILL.md 加一句

- [x] 1.1 `.claude/skills/model-rotation-survey/SKILL.md`：「各欄填法」在「綜合」與「排序」之間加「同一模型在 AA 榜單有多個 effort 檔位時，取最高檔分數。」，其餘文字不動。驗證：`grep -n "effort 檔位" .claude/skills/model-rotation-survey/SKILL.md`（應在第 100 行附近，且前後兩行分別是「綜合」與「排序」）。

## 2. 主規格 delta

- [x] 2.1 對 Requirement「比例與能力評分欄填法」發 MODIFIED，逐字帶上主規格該條的完整內文與三個既有 Scenario（「比例欄填法」「抽象架構設計欄填法」「AA 頁面抓不到時能力標查不到」），內文末尾加上同一句規則。驗證：人工：切出兩邊的 Requirement 塊（標題起、到下一個 `### ` 止），把 delta 的「；同一模型…」與「#### Scenario: 多 effort 檔位取最高檔」三行拿掉後 `diff` 應無輸出（本次以 python 腳本執行，見報告：`diff exit=0`）；直接比對原檔則只看到這兩處新增。
- [x] 2.2 新增 Scenario「多 effort 檔位取最高檔」：WHEN 某模型在 AA 榜單有 max 與 non-reasoning 兩個 effort 檔位（假分數 32／19）／THEN 該格採 32。驗證：`grep -n "多 effort 檔位取最高檔" openspec/changes/model-rotation-survey-effort/specs/model-rotation-survey/spec.md`。
- [x] 2.3 人工：Requirement 內文 <500 字（本機 openspec 1.8.0 不檢查，自行數）；本條 246 字。

## 3. 驗證

- [x] 3.1 `openspec validate model-rotation-survey-effort --strict`。
- [x] 3.2 `openspec validate --specs --strict`。
- [x] 3.3 `git status --short` 只列本 change 與 SKILL.md。
- [x] 3.4 腳本切 Requirement 塊比對：主規格與 delta 的「比例與能力評分欄填法」只差新增那句與新 Scenario（貼 diff）。

## 不在本 change

- [ ] 4.1 Zen 規則（含「待核對」列）：另案處理。
- [ ] 4.2 主規格歸檔：本 change 的 delta 待 merge 後由 PR 2／2 歸檔（`openspec archive model-rotation-survey-effort`）。
