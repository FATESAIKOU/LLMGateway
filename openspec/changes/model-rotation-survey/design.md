## Context

動機見 proposal.md（#58）。本版為定稿：依本人 2026-10-24、10-25 的決定（§6），自動化全部拿掉，改**手動**；候選範圍為四家，來源取得機械化（§4）。查證日期 2026-10-24／10-25。

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

**機械化規則**：每家的可用模型由該家自己的 API 或工具取得，抓得到就列、抓不到就不列；不人工推測計費細節。

| 提供商 | 取清單的方式 | key | 抓不到時 |
|---|---|---|---|
| OpenRouter | `GET https://openrouter.ai/api/v1/models`；id 結尾 `:free` 或 pricing 皆 `"0"` 為免費，不一致標「待確認」，排除非聊天模型 | 免 key（2026-10-24 審查者實測 200） | 不列 |
| Zen | 價格表 `https://opencode.ai/docs/zen/` Input／Output 皆 `Free` 為準；`https://opencode.ai/inference/v1/models`（只有 id）核對 id | 免 key | 不列 |
| Ollama Cloud | `GET https://ollama.com/api/tags`，模型名以回傳為準（app／CLI 用 `<名>:cloud`） | 免 key（2026-10-25 作者實測 200、23 筆） | 回非 2xx 則整家標「查不到」 |
| Claude | 官方公開清單 `https://platform.claude.com/docs/en/models/overview`（Claude API ID：`claude-fable-5-1`、`claude-opus-5-5`、`claude-sonnet-5-5`、`claude-haiku-5-5`，2026-10-25） | 免 key；不用訂閱 token 打 API | 不列 |

- Ollama Cloud 選免 key：`/api/tags` 實測不帶 key 即可讀，不必經 resolve，也不碰憑證；日後若改要 key，依上列「抓不到」處理，不把 key 帶進 Claude Code。依據 `https://docs.ollama.com/cloud`（2026-10-25）：「For API requests to ollama.com, use the name returned by this list」。
- **Claude 的合規取法**：訂閱 token 只給原版 Claude Code 用（S1 `https://code.claude.com/docs/en/legal-and-compliance`，2026-10-25：不阻止「an end user from signing in to the unmodified Claude Code binary with their own Claude subscription」），所以不拿訂閱 token 呼叫 `/v1/models`。比較：
  - 官方公開清單：不耗額度、可附 URL，缺點是只證明模型存在，不證明此訂閱能用。
  - `claude -p --model <id> --max-turns 1`（旗標見 `https://code.claude.com/docs/en/cli-reference`，2026-10-25）：在原版 Claude Code 內逐一試，能證明可用，但每試一個耗訂閱額度；一次約 2 萬–4 萬輸入（多為快取讀）、極少輸出（推估、未實測）；四個約 8 萬–16 萬輸入。
  - 選：**以官方公開清單為準**（零額度、每週都跑得起）；`claude -p` 只用來試清單上**新出現**的 id，試不過就不列。
- **每格標籤**：`官方數字`（附 URL＋讀取日期）／`推估`／`共用`／`查不到`／`主觀`。查不到不填猜測值。
- **比例欄**（決定 ②）：不設基準，只標來源。OpenRouter 免費額度是整個帳號共用的請求數上限（`https://openrouter.ai/docs/api-reference/limits`），標「共用」；Zen 官方未公布數字，標「查不到」；Ollama Cloud、Claude 列標「依訂閱」（§8-5）。
- **能力欄**（決定 ①）：terminal＝AA Terminal-Bench、coding＝Coding Index、抽象架構設計＝Intelligence Index 或標「主觀」，尺度 0–100，附 URL＋日期。表頭註明「標『主觀』的是 AI 判斷，非實測」。
- **綜合** ＝ Intelligence Index（只看這一個，避免三欄重疊重複計分）；榜上沒有的模型，綜合欄標「主觀」並給 0–100，排序時照該值。主排序鍵為綜合。
- **可用的 agent**：依 `policy/default.json` 各訂閱的 `agents` 欄位：Claude 列只寫 `claude-code`（原版，S1）；Ollama Cloud 列 `claude-code`、`opencode`；OpenRouter、Zen 列 `opencode`。
- **計入週用量節流**（決定 ③）：依提供商填：Claude 訂閱「是（週）」；Ollama Cloud「是（週，另有 5 小時 session 上限）」，來源「本人帳號設定頁 2026-10-25」（以帳號頁為準，帳號、方案細節、餘額不寫入本 repo）；免費「否」。
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

2026-10-25（第 2 次，經 PMO 轉達，原意照錄）：
- Ollama Cloud 計入週用量節流 → **是**。本人帳號設定頁顯示現行方案有 Session usage（每 5 小時重置）與 Weekly usage（每週重置）兩條上限，內含額度用完才改扣 credits；官方文件讀出的「按月、沒有 session／週上限」與實際帳號不符，**以本人帳號頁為準**（原 §7-1 已決）。
- 調查要機械化。本人原話：「Claude 的模型打 claude 的 api，OllamaCloud 的模型打 OllamaCloud 的 API，能抓到的東西就是有，沒抓到就沒有，很複雜？」每家由自己的 API 或工具取清單，抓不到就不列；不人工推測計費細節，Fable credits、seat tier 一律不留（原 §7-2 已決，不再有 Fable 特例）。「計入節流」依提供商：Claude 訂閱＝是（週）、Ollama Cloud＝是（週＋5 小時）、免費＝否。
- 界線：Claude 訂閱 token 只能給原版 Claude Code 用，不能直接呼叫 `/v1/models`；Claude 的合規取法由作者提案（§4）。

## 7. 待本人決定

無。

## 8. 作者提議、本人未指示

1. 只有 md；若之後有程式要讀，再加機器可讀檔。
2. 指令用 skill 且 `disable-model-invocation: true`，`survey`／`apply` 兩模式。
3. 沒變化不開 PR；PR 固定同一分支 `survey/current`，`apply` 在同分支 push。
4. 標籤的種類與讀取日期的寫法。
5. 比例欄：Ollama Cloud、Claude 列標「依訂閱」。理由：額度隨方案而變，Claude 公布的是 API 單價、訂閱內含額度的計算方式未公布；為兩列寫法一致且不寫額度，維持「依訂閱」；Zen、OpenRouter 維持 ②。
6. Claude 的模型清單用官方公開清單、`claude -p` 只試新 id（§4 的取捨），請本人覆核。
7. 新表可加「資料政策（官方原文）」欄（Zen 文件公開），承接手冊隱私分級；預設不加。
