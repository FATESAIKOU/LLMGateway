# tasks：model-rotation-survey 的規格與任務（#58、#63）

## 1. Skill 實作與輪換表格式（PR #62）

- [x] 1.1 `.claude/skills/model-rotation-survey/SKILL.md`：建立 Claude Code skill（`disable-model-invocation: true`），實作 `survey` 與 `apply` 兩模式之流程、四家機械化來源、標籤與各欄填法、git 分支與安全邊界護欄。驗證：人工：檢視 PR #62 變更內容、frontmatter 設定及來源端點公開可存取性。
- [x] 1.2 `docs/model-rotation.md`：建立人讀模型輪換表架構、與訂閱分配分工說明、讀表規則、五種標籤定義與表頭。驗證：人工：檢視 PR #62 檔案結構與表頭欄位格式。

## 2. 規格補齊與首次實跑（PR #63、PR #135）

- [x] 2.1 `openspec/changes/model-rotation-survey/specs/model-rotation-survey/spec.md`：建立 delta spec（20 條 Requirement、45 個 Scenario），涵蓋觸發限制、survey 流程、四家來源、標籤與欄位、comment 篩選與 apply、安全邊界與外部注入防護。驗證：`openspec validate model-rotation-survey --strict`。
- [x] 2.2 `openspec/changes/model-rotation-survey/tasks.md`：建立 tasks.md 追蹤實作、規格與邊界任務。驗證：`openspec validate model-rotation-survey --strict`。
- [x] 2.3 `docs/model-rotation.md` 首次實跑：PR #135 於 2026-10-31 完成首次實跑調查，自四家來源取得 54 筆模型資料並開啟 PR，說明差異與各格來源。驗證：人工：檢視 PR #135 之 diff 與 PR body。

## 不在本 change

- [ ] 3.1 MyPMO 手冊改引用：MyPMO 模型輪換手冊的「候選模型與數字」改為引用 LLMGateway 的 `docs/model-rotation.md`，由 PMO 於 MyPMO 執行。
- [ ] 3.2 PR #135 本人審核與 merge：等本人在 PR #135 留下 comment 後執行 `/model-rotation-survey apply` 套用修改並由本人 merge。
- [ ] 3.3 主規格歸檔（第二個 PR（歸檔））：待實跑驗收完畢後，執行 `openspec archive model-rotation-survey` 將 change 歸檔為主規格。
