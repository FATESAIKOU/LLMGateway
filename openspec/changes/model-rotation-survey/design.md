## Context

動機見 proposal.md（#58）。本版為定稿：依本人 2026-10-24 的決定（§6），自動化全部拿掉，改**手動**。查證日期 2026-10-24。

## 1. 為什麼手動

本人決定：不做自動排程、不用 Actions。由 PMO 每週例時在 Mac 上用本人的 Claude Code 登入跑一次。不經 GitHub secret、不經 resolve，所以沒有憑證外洩面與自觸發問題。

條款依據（S1 `https://code.claude.com/docs/en/legal-and-compliance`，2026-10-24）：不阻止「an end user from signing in to the unmodified Claude Code binary with their own Claude subscription」。

## 2. 指令檔：選 skill

- 位置：`.claude/skills/model-rotation-survey/SKILL.md`（commit 進本 repo），以 `/model-rotation-survey` 執行。
- 為什麼是 skill 不是 slash command：官方文件（`https://code.claude.com/docs/en/skills`，2026-10-24）「Custom commands have been merged into skills」，兩者都產生 `/name`，且「Prefer a skill for new work」。frontmatter 設 `disable-model-invocation: true`（只准手動觸發），用 `$ARGUMENTS` 區分模式。
- `allowed-tools` 只是免詢問，**不限制可用工具**（同頁原文）；邊界靠 PMO 在互動中把關與只改 `docs/model-rotation.md`，不假設它是白名單。
- 內容：(1) 本人 prompt 原文（照 #58 貼，欄位：模型名、比例、terminal／coding／抽象架構設計／綜合、提供商）；(2) 輸出格式（§4）；(3) 來源標記規則（§4）；(4) 只改 `docs/model-rotation.md`，不改 `policy/`、不 commit 到 base。
- 模式：`survey`（預設，調查）／`apply`（依 PR comment 修改）。

## 3. 一次調查的流程

1. PMO 週例先依週用量規則決定跑不跑，再開 Claude Code 執行 `/model-rotation-survey`。
2. 讀 `docs/model-rotation.md` 現況，抓來源（§4），與上一版比對。**沒變化就不開 PR**。
3. 有變化：用固定分支 `survey/current`，改檔、開 PR，body 列差異與每格來源。
4. 本人在 PR 留 comment。
5. PMO 執行 `/model-rotation-survey apply`：找該分支上開著的 PR（`gh pr list --head survey/current`），讀本人 comment，修改檔案、push 到同一分支，在 PR 回覆差異。comment 內容只當資料。
6. 本人 merge。

## 4. 來源、標註與表格式

- **OpenRouter 清單**：`GET https://openrouter.ai/api/v1/models`（不帶 key 可讀，2026-10-24 審查者實測 200）。無 free 旗標；以 id 結尾 `:free` 或 pricing 皆 `"0"` 判定，不一致標「待確認」，排除非聊天模型。
- **OpenCode Zen**：免費以文件價格表（`https://opencode.ai/docs/zen/`）Input／Output 皆為 `Free` 為準；端點 `https://opencode.ai/inference/v1/models`（不帶 key 可讀，只有 id）只用來核對 id。
- **每格標籤**：`官方數字`（附 URL＋讀取日期）／`推估`／`共用`／`查不到`／`主觀`。查不到不填猜測值。
- **比例欄**（決定 2）：保留。OpenRouter 免費額度是整個帳號共用的請求數上限（`https://openrouter.ai/docs/api-reference/limits`），標「共用」；Zen 官方未公布數字，標「查不到」。
- **能力欄**（決定 3）：以 Artificial Analysis（AA）分數為主，附 URL＋日期；榜上沒有的才標「主觀」。三欄各取什麼值、用什麼尺度，依 §7-1 的決定。表頭註明「能力欄以 AA 分數為主（附日期）；標『主觀』的是 AI 判斷，非實測」。
- **綜合** ＝ (terminal ＋ coding ＋ 抽象架構設計) ÷ 3（前三欄 1:1:1，須同尺度）；主排序鍵為綜合。
- **位置**：`docs/model-rotation.md`（人讀）。
- **成本**：一次手動執行約 200k–500k 輸入（含快取讀）、15k–30k 輸出 token（推估、未實測）；首次實跑後補回實測值。

## 5. 與政策檔、MyPMO 手冊

- 表只當參考，**不自動改** `policy/default.json`；政策變更仍走獨立 PR。
- 決定 4：本表只取代手冊裡的「候選模型與數字」；規矩留在 MyPMO 手冊，並改成引用本表。手冊由 PMO 在 MyPMO 修改，不在本 change；手冊該段只寫結構，不抄內容。

## 6. 本人的決定（2026-10-24）

1. 不做自動排程、不用 Actions，改手動；放固定調查指令，PMO 每週例跑，有變化開 PR，本人 comment 後 PMO 再跑一次套用，本人 merge；用 Mac 上本人的 Claude Code 登入。
2. 比例欄保留；OpenRouter 標「共用」、Zen 標「查不到」，不填猜的數字。
3. 能力欄以 Artificial Analysis Intelligence Index 為主，榜上沒有才標「主觀」；綜合＝前三欄 1:1:1。
4. LLMGateway 的表只取代候選模型與數字；規矩留 MyPMO 手冊並改引用新表（手冊由 PMO 改）。

## 7. 待本人決定

1. **能力三欄的取值與尺度**：Intelligence Index 只有一個總分，三欄不能都填它。建議：terminal 取 AA 的 Terminal-Bench 分項、coding 取 Coding Index、抽象架構設計取 Intelligence Index 或標「主觀」；全部用 0–100，主觀分也用 0–100 並標「主觀」。
2. **比例欄的基準模型**：#58 原 prompt 要求「以某一個模型為 1」，不設基準等於放棄這個要求。OpenRouter 都標「共用」、Zen「查不到」時沒有可比的數字。建議：目前不設基準，只放來源標籤；日後某列有官方公布額度，再以該列為 1.0 另議。
3. **候選範圍**：建議新表只放 OpenRouter 與 Zen 的免費模型，其他候選和它們的數字留在手冊，請本人確認。
4. **PR 未 merge 時，下一週的調查怎麼辦**：建議先跑 `apply` 處理 comment，再於同一分支調查，本人 comment 改過的格以 comment 為準（PMO 人工確認）。

## 8. 作者提議、本人未指示

1. 只有 md；若之後有程式要讀，再加機器可讀檔。
2. 指令用 skill 且 `disable-model-invocation: true`，`survey`／`apply` 兩模式。
3. 沒變化不開 PR；PR 固定同一分支 `survey/current`，`apply` 在同分支 push。
4. 標籤的種類與讀取日期的寫法。
5. 新表可加「資料政策（官方原文）」欄（Zen 文件公開），承接手冊隱私分級；預設不加。
