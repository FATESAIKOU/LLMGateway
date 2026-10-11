# AGENTS.md

## 這個 repo 的 issue 是工單

這個 repo 是大專案「LLMGateway」。它的 issue 是一人公司工作樹上的工單，規則以 [FATESAIKOU/MyPMO 的工單規則](https://github.com/FATESAIKOU/MyPMO/blob/main/%E5%B7%A5%E5%96%AE%E8%A6%8F%E5%89%87.md) 為準（private repo；讀不到時照下面的摘要）。MyPMO 的 Actions 每天檢查一次，違規會通知本人。

- 這是 **public repo**：工單、留言、程式與文件**不寫 MyBrain 的內容與私人資訊**，也不寫任何金鑰、token 的實際值；需要時連到 MyPMO 的工單。

### 開 issue

- 層級：**b** 功能組新增或重構／**c** 功能組擴張或改修（好幾步）／**d** 小擴張或 bug（一個 PR 做得完）。**a（大專案新設或轉向）不開在這裡，開在 MyPMO。**
- 誰能開：b 只有本人能開（PM 只能以「提案中」提出）；隊員開的 c 要這個專案的 PM 或 reviewer 核准才成立；d 隊員可以自由開，但要另一位隊員同意。
- label：恰一個 `層級:`、恰一個 `狀態:`（構想／提案中／待開工／進行中／等本人／阻塞）；`狀態:等本人` 時再加至少一個 `等本人:核准|合併|驗收|裁定|動作`。沒有 `層級:` 的 issue 會被列為「樹外」。
- 內文段落：`## 為什麼`、`## 來源`、`## 相關`；等本人時加 `## 等什麼`。
- 從屬用 sub-issue（最多一個父，父的層級要比子高）；依存用 blocked by。

### 做事

- 開工改 `狀態:進行中`；這個大專案同時進行中的 b、c 合計最多 2 張（d 不限；2026-10-11 從 1 張放寬）。在等前置工單、還沒開工的維持待開工＋blocked by。
- 卡在本人：`狀態:等本人`＋種類。做到一半卡在別的工單或外部條件：`狀態:阻塞`＋blocked by 或寫明等什麼。
- 做完以 completed 關閉；不做以 not planned 關閉並加 `否決`（本人不同意）或 `作廢`（前提不存在）。
- 回報對象是 MyPMO 的 PMO；本人的決定由 PMO 轉達，或經 MyPMO 的決定檔 PR 套用。
- 隊員不讀憑證檔；工作 session 告一段落時匯入 Agora（title `LLMGateway-<issue 號碼>-<角色>`），規則見工單規則第 9 節。

## 這個專案是什麼

LLMGateway：一人公司所有 AI agent 共用的模型窗口。把本人手上的訂閱（Claude 公司／個人、Ollama Cloud、OpenRouter、OpenCode Zen、Antigravity）收成統一的入口，並管理「哪台 worker、什麼角色、哪個訂閱、哪個 coding agent、哪個模型」的開關申請與紀錄。上層計畫是 MyPMO 的 AiCompany 期 1（MyPMO#31）與「啟動 LLMGateway」（MyPMO#4）。
