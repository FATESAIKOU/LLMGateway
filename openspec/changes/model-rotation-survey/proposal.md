對應工單：#58（c）「模型輪換表搬到 LLMGateway，每週手動調查免費模型並開 PR，可依本人 comment 修改」；源頭：MyPMO 的模型輪換手冊（private repo，不引用內容）。

> 本 change 目前**只有短設計**（proposal＋design）。specs／tasks 等本人同意定稿後再寫；design.md 第 7 節尚有 2 項待本人決定（Ollama Cloud 與 Claude Fable 的節流欄）。

## Why

本人（2026-10-24）要：

1. 模型輪換規則表從 MyPMO 搬到 LLMGateway（現在由 PMO 每月第一次週例手動重排）。
2. 用 Claude Code＋固定 prompt 調查 openrouter、opencode 的免費模型，並（本人 2026-10-25 追加）含付費的 Ollama Cloud 與 Claude 訂閱模型（欄位：模型名、比例、terminal／coding／抽象架構設計／綜合能力、提供商，另加「可用的 agent」「計入週用量節流」），有變化就開 PR。
3. 本人在 PR 留 comment，可依 comment 修改表。

本人已決定（design.md 第 6 節，10-24、10-25）：**手動**，不做自動排程、不用 Actions；由 PMO 每週例在 Mac 上用本人的 Claude Code 登入執行，因此不需要 GitHub secret，也不經 resolve。

## What Changes

- 新增 `.claude/skills/model-rotation-survey/SKILL.md`：固定調查指令（本人 prompt、輸出格式、來源標記規則；`survey`／`apply` 兩模式）。
- 新增 `docs/model-rotation.md`（人讀表），每格標來源與讀取日期；只有 md，不做機器可讀檔。
- 範圍四家：OpenRouter／Zen 免費、Ollama Cloud（付費）、Claude（訂閱）。Claude 只能給原版 Claude Code 用，表內逐列標「可用的 agent」；付費／訂閱列標是否計入週用量節流（免費列「不適用」）。綜合欄只看 Intelligence Index。
- 表只當參考；MyPMO 手冊的「候選模型與數字」改引用本表，規矩留在手冊，手冊由 PMO 另行修改。

## What Does Not Change

- **不做**：自動排程、GitHub Actions、自動改 `policy/default.json`、自動 merge、在 GitHub 放任何 LLM secret、把 Claude 訂閱 token 交給非原版 Claude Code 的工具。
- **不抄** MyPMO 手冊的內容進本 repo（private）。
- 不改 resolve／cooldown 等既有介面與規格；不動 `lgwd/` 程式。

## Impact

- 只新增一個 skill 檔與文件；每週由 PMO 手動耗一次 Claude 用量，是否執行由 PMO 依週用量規則判斷。
- public repo：表與 PR 內容不得含私人資訊、額度數字、key。
