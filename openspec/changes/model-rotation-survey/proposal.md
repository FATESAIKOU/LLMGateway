對應工單：#58（c）「模型輪換表搬到 LLMGateway，每天自動調查免費模型並開 PR，可依本人 comment 修改」；源頭：MyPMO 的模型輪換手冊（private repo，不引用內容）。

> 本 change 目前**只有短設計**（proposal＋design）。specs／tasks 等設計審過、本人同意後再寫。設計中標「本人的判斷點」的項目未決，不可先實作。

## Why

本人（2026-10-24）要三件事：

1. 模型輪換規則表從 MyPMO 搬到 LLMGateway（現在由 PMO 每月第一次週例手動重排）。
2. 每天自動更新：用 Claude Code＋固定 prompt 調查 openrouter、opencode 目前的免費模型（欄位：模型名、推估每天可用免費 token 量的相對比例、terminal／coding／抽象架構設計／綜合能力、提供商），產出後開 PR。
3. 本人在 PR 留 comment，要能依 comment 修改表。

卡點在「誰來跑 Claude Code、憑證從哪來」：現行規矩是 LLM key 全放 LLMGateway、GitHub 上不放 LLM secret（MyLinuxPool#99 已收掉），而 Claude 訂閱的 token 依條款只能給原版 Claude Code 使用。所以先設計、讓本人選路，再實作。

## What Changes

（設計通過後才會做；現在只是方向）

- 新增 `docs/model-rotation.md`：輪換表（人讀），每列標來源與調查時間，推估與主觀欄位明確標註。
- 新增每日調查流程與「依本人 comment 修改」流程；跑的地方由 design.md 的憑證選項決定。
- 表與政策檔 `policy/default.json` 的關係：表只當參考，不自動改政策；改政策仍走原本的政策變更 PR。
- MyPMO 手冊裡「候選模型與數字來源」改成指向 LLMGateway 這份；其餘（角色分表、隱私分級、門檻、排除清單等）留在手冊，切分待本人決定（design.md 第 4 節）。手冊的修改由 MyPMO 另開工單，本 repo 不改它。

## What Does Not Change

- **不做**：自動改 `policy/default.json`；自動 merge；在 GitHub 放任何 LLM secret；把 Claude 訂閱 token 交給非原版 Claude Code 的工具。
- **不抄** MyPMO 手冊的內容進本 repo（private）；只描述哪些搬、哪些留。
- 不改 resolve／cooldown 等既有介面與規格。

## Impact

- 新增文件與（可能的）workflow／排程腳本；不動 `lgwd/` 程式。
- 每天固定耗一次 Claude 用量（估計見 design.md 第 5 節），需有超量自動跳過的機制。
- public repo：表與 PR 內容不得含私人資訊、額度數字（本人的訂閱額度）、key。
