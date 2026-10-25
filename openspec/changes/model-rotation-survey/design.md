## Context

動機見 proposal.md（#58）。本版為定稿：依本人 2026-10-24、10-25 的決定（§6），自動化全部拿掉，改**手動**；候選範圍為四家。查證日期 2026-10-24／10-25。

## 1. 為什麼手動

本人決定：不做自動排程、不用 Actions。由 PMO 每週例時在 Mac 上用本人的 Claude Code 登入跑一次。不經 GitHub secret、不經 resolve，所以沒有憑證外洩面與自觸發問題。

條款依據（S1 `https://code.claude.com/docs/en/legal-and-compliance`，2026-10-24）：不阻止「an end user from signing in to the unmodified Claude Code binary with their own Claude subscription」。

## 2. 指令檔：選 skill

- 位置：`.claude/skills/model-rotation-survey/SKILL.md`（commit 進本 repo），以 `/model-rotation-survey` 執行。
- 為什麼是 skill 不是 slash command：官方文件（`https://code.claude.com/docs/en/skills`，2026-10-24）「Custom commands have been merged into skills」，兩者都產生 `/name`，且「Prefer a skill for new work」。frontmatter 設 `disable-model-invocation: true`（只准手動觸發），用 `$ARGUMENTS` 區分模式。
- `allowed-tools` 只是免詢問，**不限制可用工具**（同頁原文）；邊界靠 PMO 在互動中把關與只改 `docs/model-rotation.md`，不假設它是白名單。
- 內容：(1) 本人 prompt 原文（照 #58 貼，欄位另加「可用的 agent」「計入週用量節流」，見 §4）；(2) 輸出格式（§4）；(3) 來源標記規則（§4）；(4) 只改 `docs/model-rotation.md`，不改 `policy/`、不 commit 到 base。
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
- **Ollama Cloud（付費；官方另有 Free 起步額度，同頁：「Free accounts include a starter amount of usage」）**：清單用官方 API `GET https://ollama.com/api/tags`（`https://docs.ollama.com/cloud`，2026-10-25：「For API requests to ollama.com, use the name returned by this list」「In the Ollama app or CLI, use gemma4:cloud」）。表的模型名以 API 回傳名稱為準，app／CLI 要加 `:cloud`。計量見 `https://ollama.com/pricing`（2026-10-25）：「Usage is measured in tokens at each model's rates」「included usage resets monthly on the same day of the month your subscription started」「the session and weekly limits of the old plans no longer apply」，即按月計量，現行方案沒有 session／週上限。
- **Claude（訂閱）**：模型清單以官方 models 文件為準（`https://platform.claude.com/docs/en/models/overview`，2026-10-25，ID：`claude-fable-5-1`／`claude-opus-5-5`／`claude-sonnet-5-5`／`claude-haiku-5-5`）。哪些模型在原版 Claude Code 的訂閱下可用、節流依據改用 `https://code.claude.com/docs/en/model-config`（2026-10-25）；其中「Depending on your plan and seat tier, Fable usage can bill to usage credits instead of drawing on your plan's included limits」，Fable 列加註「依方案可能改計 usage credits，不扣訂閱額度」（見 §7）。不寫本人方案等級。
- **每格標籤**：`官方數字`（附 URL＋讀取日期）／`推估`／`共用`／`查不到`／`主觀`。查不到不填猜測值。
- **比例欄**（決定 ②）：不設基準，只標來源。OpenRouter 免費額度是整個帳號共用的請求數上限（`https://openrouter.ai/docs/api-reference/limits`），標「共用」；Zen 官方未公布數字，標「查不到」；Ollama Cloud、Claude 列標「依訂閱」（見 §8-5）。
- **能力欄**（決定 ①）：terminal＝AA Terminal-Bench、coding＝Coding Index、抽象架構設計＝Intelligence Index 或標「主觀」，尺度 0–100，附 URL＋日期。表頭註明「標『主觀』的是 AI 判斷，非實測」。
- **綜合** ＝ Intelligence Index（只看這一個，避免三欄重疊重複計分）；榜上沒有的模型，綜合欄標「主觀」並給 0–100，排序時照該值。主排序鍵為綜合。
- **新增欄位**（決定 ③）：「可用的 agent」「計入週用量節流」。agent 欄依 `policy/default.json` 各訂閱的 `agents` 欄位：Claude 列只寫 `claude-code`（原版，S1 條款）；Ollama Cloud 列 `claude-code`、`opencode`；OpenRouter、Zen 列 `opencode`。節流欄：付費／訂閱列寫「是」或「否」，免費列「不適用」；Claude 列為「是」（週用量規矩本來就對 Claude 訂閱而訂，見 §8-6），Fable 列依方案（§7-2）；Ollama Cloud 列待決（§7-1）。
- **位置**：`docs/model-rotation.md`（人讀）。
- **成本**：一次手動執行約 200k–500k 輸入（含快取讀）、15k–30k 輸出 token（推估、未實測）；首次實跑後補回實測值。

## 5. 與政策檔、MyPMO 手冊

- 表只當參考，**不自動改** `policy/default.json`；政策變更仍走獨立 PR。
- 決定 ④：新表涵蓋 OpenRouter／Zen 免費、Ollama Cloud（付費）、Claude（訂閱）四家的候選模型與數字，取代手冊裡的「候選模型與數字」；規矩留在 MyPMO 手冊，並改成引用本表。手冊由 PMO 在 MyPMO 修改，不在本 change；手冊該段只寫結構，不抄內容。

## 6. 本人的決定

2026-10-24：
1. 不做自動排程、不用 Actions，改手動；放固定調查指令，PMO 每週例跑，有變化開 PR，本人 comment 後 PMO 再跑一次套用，本人 merge；用 Mac 上本人的 Claude Code 登入。
2. 比例欄保留；OpenRouter 標「共用」、Zen 標「查不到」，不填猜的數字。
3. ~~綜合＝前三欄 1:1:1~~（被 10-25 ① 取代）；能力欄以 Intelligence Index 為主，榜上沒有才標「主觀」。
4. LLMGateway 的表只取代候選模型與數字；規矩留 MyPMO 手冊並改引用新表（手冊由 PMO 改）。

2026-10-25（經 PMO 轉達，對原 §7 的裁定）：
- ① 能力三欄照建議取值（見 §4），尺度 0–100；排序用的綜合分只看 Intelligence Index。取代 10-24 的 1:1:1。
- ② 比例欄不設基準，只標來源；日後某列有官方公布額度，再以該列為 1.0 另議。
- ③ 候選範圍改為 OpenRouter／Zen 免費＋Ollama Cloud（付費）＋Claude（訂閱）。本人原話：「這張輪換表是 PMO/PM 抓模型時使用，應該還要包含付費的 OllamaCloud 跟 Claude 模型吧」。Claude 只能給原版 Claude Code 用，表內標清楚；付費者標是否計入週用量節流。
- ④ PR 未 merge 時，先跑 `apply`，再在同一分支調查；本人 comment 改過的格以 comment 為準（PMO 人工確認）。

## 7. 待本人決定

1. Ollama Cloud「計入週用量節流」。官方按月計量、現行方案沒有 session／週上限（§4）。建議：Ollama Cloud 按月計量，週節流（70%／90%）不直接適用，填「否（按月）」，月額度的節流門檻另由 PMO 規矩定。
2. Claude 的 Fable 列節流。官方載明依方案可能改計 usage credits（§4）。建議：Fable 列標「依方案」；本人的方案是否適用，由 PMO 實跑 `/model` 確認後填。

## 8. 作者提議、本人未指示

1. 只有 md；若之後有程式要讀，再加機器可讀檔。
2. 指令用 skill 且 `disable-model-invocation: true`，`survey`／`apply` 兩模式。
3. 沒變化不開 PR；PR 固定同一分支 `survey/current`，`apply` 在同分支 push。
4. 標籤的種類與讀取日期的寫法。
5. 比例欄：Ollama Cloud、Claude 列標「依訂閱」。取捨：Ollama 官方有各模型單價，算得出同額度下的相對比例，但額度隨方案而變、Claude 列則沒有，為了兩列寫法一致且不寫額度，維持「依訂閱」；Zen、OpenRouter 維持 ②。
6. Claude 列「計入週用量節流」標「是」：依據是 PMO 週用量規矩本來就對 Claude 訂閱而訂，模型可用性依 `model-config`（§4）；Fable 例外見 §7-2。
7. 新表可加「資料政策（官方原文）」欄（Zen 文件公開），承接手冊隱私分級；預設不加。
