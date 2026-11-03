# tasks：model-rotation-survey 的 SKILL.md 與 spec 小收斂（#176，PR 1／2）

## 1. A：節流來源日期不要寫死

- [x] 1.1 `.claude/skills/model-rotation-survey/SKILL.md`：「計入週用量節流」的來源字串由「本人帳號設定頁 2026-10-25」改為「本人帳號設定頁（讀取日期見該列）」，其餘文字不動。驗證：`grep -n "本人帳號設定頁" .claude/skills/model-rotation-survey/SKILL.md`。
- [x] 1.2 `docs/model-rotation.md`：先 grep `2026-10-25` 確認本檔沒有同一個來源註記，未改動。驗證：`grep -n "2026-10-25" docs/model-rotation.md`（應無輸出）。
- [x] 1.3 主規格 delta：對「可用的 agent 與節流標記」與「公開儲存庫安全性與外部注入防護」兩條 Requirement 發 MODIFIED，逐字帶上完整內文與既有 Scenario，只改來源字串。驗證：`grep -rn "2026-10-25" openspec/specs/model-rotation-survey/spec.md` 找出的每個位置，其 Requirement 都出現在 delta 的 `## MODIFIED Requirements`（人工：逐條對照；`openspec validate` 不檢查這點，改動刪掉一條 Requirement 仍會綠）。

## 2. B：補「篩完無新 comment」Scenario

- [x] 2.1 主規格 delta：對「PR comment 篩選條件」發 MODIFIED，逐字帶上完整內文與兩個既有 Scenario，新增 Scenario「篩完無新 comment 且由 survey 帶入」（WHEN 篩完無新 comment 且由 survey 帶入／THEN 跳過修改／commit／回覆，回到 survey 繼續調查）。驗證：`grep -n "篩完無新 comment" openspec/changes/model-rotation-survey-align/specs/model-rotation-survey/spec.md`。

## 3. 驗證

- [x] 3.1 `openspec validate model-rotation-survey-align --strict`。
- [x] 3.2 `openspec validate --specs --strict`。
- [x] 3.3 `git status --short` 只列本 change 與 SKILL.md。
- [x] 3.4 人工：Requirement 內文各段 <500 字（本機 openspec 1.8.0 不檢查，自行數）。

## 不在本 change

- [ ] 4.1 Zen「待核對」列的規則：交本人確認後另案處理。
- [ ] 4.2 AA effort 檔位的口徑：交本人確認後另案處理。
- [ ] 4.3 主規格歸檔：本 change 的 delta 待 merge 後由 PR 2／2 歸檔（`openspec archive model-rotation-survey-align`）。
