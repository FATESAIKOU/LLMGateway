對應工單：#176（d）「model-rotation-survey 的 SKILL.md 與 spec 小收斂（PR 1／2）」；上層：#63（model-rotation-survey 的 spec 與 tasks 補齊）、#58（模型輪換表搬到 LLMGateway）。

## Why

本人裁定（2026-11-03）：同一模型在 AA（Artificial Analysis）榜單有多個 effort 檔位時，取**最高檔**分數（例：某模型 max 檔 32、non-reasoning 檔 19，取 32）。

現況沒有寫這條口徑，調查時可能取到哪個檔位就填哪個：

- `.claude/skills/model-rotation-survey/SKILL.md` 的「各欄填法」只寫 terminal／coding／抽象架構設計／綜合各採哪個 AA 指標，沒說同一模型多檔位怎麼取。
- 主規格 `openspec/specs/model-rotation-survey/spec.md` 的 Requirement「比例與能力評分欄填法」同樣沒有這條，`openspec validate` 不會發現。

## What Changes

- `.claude/skills/model-rotation-survey/SKILL.md`：「各欄填法」在「綜合」與「排序」之間加一句「同一模型在 AA 榜單有多個 effort 檔位時，取最高檔分數。」，其餘文字不動。
- 主規格 delta（`openspec/changes/model-rotation-survey-effort/specs/model-rotation-survey/spec.md`，`## MODIFIED Requirements`）：只對「比例與能力評分欄填法」發 MODIFIED，逐字帶上主規格該條的完整內文與三個既有 Scenario，內文加上同一句規則，並新增 Scenario「多 effort 檔位取最高檔」（WHEN 某模型在 AA 榜單有 max 與 non-reasoning 兩個檔位（假分數 32／19）／THEN 該格採 32）。

## 不做

- 不改其他 Requirement、其他 Scenario：delta 只有「比例與能力評分欄填法」一條。
- 不動 Zen 規則（含「待核對」列）；Zen 的差異另案處理。
- 不動 `docs/model-rotation.md`、不動 `policy/`、不改 Gateway 程式。
- 不改「每格標籤」與「來源與讀取日期」的既有寫法。

## Impact

- 只動一個指令檔的一段文字與一個 delta spec；不影響 Gateway 程式、政策檔與 PR 流程。
- 主規格歸檔（`openspec archive model-rotation-survey-effort`）由 PR 2／2 做。
- public repo：不寫入私人資訊；範例分數 32／19 是假分數。
