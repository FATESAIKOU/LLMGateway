對應工單：#176（d）「model-rotation-survey 的 SKILL.md 與 spec 小收斂（PR 1／2）」；上層：#63（model-rotation-survey 的 spec 與 tasks 補齊）、#58（模型輪換表搬到 LLMGateway）。

## Why

主規格 `openspec/specs/model-rotation-survey/spec.md`（20 Requirement／45 Scenario）於 2026-11-03 歸檔後，#176 列出四項差異，本 change 收斂其中兩項：

- **A（來源日期寫死）**：`.claude/skills/model-rotation-survey/SKILL.md` 把節流來源寫成「本人帳號設定頁 2026-10-25」，日期寫死在指令檔裡，與「來源與讀取日期」每列各記讀取日期的既有規則不一致，調查時容易照抄舊日期。主規格「可用的 agent 與節流標記」與「公開儲存庫安全性與外部注入防護」的 Scenario 也帶同一個字串。
- **B（缺 Scenario）**：主規格「PR comment 篩選條件」的 Requirement 末句寫了「若篩選後無新 comment，MUST 回報並跳過修改／commit／回覆步驟；若由 survey 帶入，MUST 回到 survey 繼續調查」，但底下沒有對應的 Scenario，45 個 Scenario 未覆蓋這條行為。

## What Changes

- `.claude/skills/model-rotation-survey/SKILL.md`：「計入週用量節流」來源字串由「本人帳號設定頁 2026-10-25」改為「本人帳號設定頁（讀取日期見該列）」，其餘文字不動。
- `docs/model-rotation.md`：先 grep `2026-10-25`，本檔沒有這個來源註記，不需要改。
- 主規格 delta（`openspec/changes/model-rotation-survey-align/specs/model-rotation-survey/spec.md`，`## MODIFIED Requirements`）：
  - 「可用的 agent 與節流標記」：來源字串同步改為「本人帳號設定頁（讀取日期見該列）」，兩個既有 Scenario 原樣帶上。
  - 「公開儲存庫安全性與外部注入防護」：其 Scenario「排除私人額度數字與手冊內容」的 THEN 含同一字串，同步改掉；Requirement 內文與另一個 Scenario 原樣帶上。
  - 「PR comment 篩選條件」：Requirement 內文與兩個既有 Scenario 原樣帶上，新增 Scenario「篩完無新 comment 且由 survey 帶入」。

## 不做

- 不改 Zen「待核對」列的規則（#176 另一項差異；待本人確認後另案處理）。
- 不定 AA effort 檔位的口徑（#176 另一項差異；待本人確認後另案處理）。
- 不動其他 Requirement、其他 Scenario 與其他檔案；不改 `policy/`。

## Impact

- 只動一個指令檔文字與一個 delta spec；不影響 Gateway 程式、政策檔與 PR 流程。
- public repo：不寫入私人資訊；來源註記只留「本人帳號設定頁（讀取日期見該列）」。
