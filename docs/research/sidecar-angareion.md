# Agent sidecar 與 Angareion 現況調查 — 兼論 LLMGateway 接點

> 調查日期：2026-10-10（Asia/Tokyo）。調查者：LLMGateway 團隊 reviewer（工作 session）。
> 資料範圍：只讀公開 GitHub repo（FATESAIKOU/MyAiStorage、FATESAIKOU/LLMGateway、Fission-AI/OpenSpec）與本機 `openspec` CLI 1.8.0 的說明輸出。**未讀**任何憑證檔（SECRETS.md、.env、~/.claude、auth.json 等），**未寫入**任何金鑰或 token 的實際值。MyPMO、MyLinuxPool 是 private repo，本文沒有讀它們的內容；提到它們的地方只引自公開 issue 的文字。
> 用語：本檔「sidecar」＝ Agent sidecar（MyAiStorage#31）；「Angareion」＝ A2A 通訊（MyAiStorage#30）。標「（推論）」的段落是根據公開資料的判斷，不是已定案的設計。
> 附帶說明：issue #30、#31 在調查當下沒有任何留言，本文的 issue 內容全部來自開單描述。
> 連結查核（2026-10-10）：文內 design.md 的行號連結（L37、L41、L58、L146、L219、L276、L283、L295、L297、L328、L418、L478）已逐條對 `main`（commit `2453f9df`）抽查，全部指到所述內容。main 之後若再前進，行號可能位移。

## 0. 摘要

1. **兩者都還沒進入實作**。MyAiStorage 的 `main` 分支上沒有 sidecar／a2a 的程式碼，也沒有對應的 openspec change；需求只寫在 issue [#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)（Angareion）與 [#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)（sidecar）。#31 明講「先設計（保留成獨立的設計任務，經本人看過再做）」——到調查日為止，公開資料看不到那份設計的產出。
2. **現在能動的底層是 agora CLI**。sidecar 已定調「啟動用 `agora continue`、匯出用 `agora import`」（#31）；agora 現況已有 pending＋flock、前景啟動 agent、agent 結束後寫回，以及 process group 的依序中斷機制（design.md）。
3. **Angareion v0 是一個「角色信箱」**：六欄位訊息、只追加、後端是 private GitHub repo（一話題一 issue、一訊息一留言）、指令 `a2a send/inbox/ack`、ETag 輪詢（#30）。身分表示與 token 範圍**明確列為待設計**（#30）。
4. **LLMGateway 的三個接點都還沒定案**：誰啟動 agent（公開資料只提到 sidecar）、誰持有／注入憑證（公開資料未見任何設計）、開關申請能否走 Angareion／sidecar（形式可行，但流程、權威與紀錄落點未定）。問題清單見第 4 節。
5. 本 repo（LLMGateway）**還沒有 OpenSpec root**（`openspec context` 實測回 `No OpenSpec root found`）；要在本 repo 建 OpenSpec change 的前置步驟見第 5 節。

## 1. Agent sidecar 的現況

### 1.1 設計到哪、範圍是什麼

- 定位：worker 上所有 AI agent（Claude Code、opencode，以及之後的其他 agent）都要受 sidecar 控制；sidecar 也負責收 Angareion 的訊息、送進 agent；之後（期 4）擴成監視與控制 worker 內所有 agent 的生命週期。出處：[MyAiStorage#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)。
- 製程：**先設計**，而且設計是「保留成獨立的設計任務，經本人看過再做」。出處：同上。
- **v0 範圍**：「收 Angareion → 依緊急度投遞」與「啟動、終止 team」。出處：同上。
- 期 4 的全生命週期（監視、暫停、重啟、異常處理）是另一張工單：[MyAiStorage#34](https://github.com/FATESAIKOU/MyAiStorage/issues/34)。
- 公開 repo 現況：`main` 分支的檔案樹裡沒有任何 sidecar／a2a 模組；`src/` 裡出現 "sidecar" 字樣的地方只有 Claude adapter 在講 Claude session 的附屬目錄（`_pack_aux`，見 [claude.py:235](https://github.com/FATESAIKOU/MyAiStorage/blob/main/src/agora/agents/claude.py#L235)），與本節的 Agent sidecar 無關。程式與部署設定放 MyAiStorage（#30）。

### 1.2 啟動與終止 agent：現在有的是什麼

sidecar 要「用」的動作，agora 這邊已經有實作與設計：

| 動作 | 現況 | 出處 |
|---|---|---|
| 啟動 agent | `agora continue session <id> --agent opencode\|claude`：取得原 session、轉接器 `start_native(raw, workdir)` 載入、寫 pending 並持有 flock（交給 agent 繼承）、**在前景啟動 agent**、結束後寫回 | [design.md 5.4](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L283)、[L295](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L295)、[L297](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L297)；指令面見 [README](https://github.com/FATESAIKOU/MyAiStorage/blob/main/README.md) |
| 終止（使用者中斷） | 前景：Ctrl-C 只給 agent（不殺 agora）；TUI 內跑的子程序：對整個 process group 依序 `SIGINT` → 5 秒 `SIGTERM` → 再 5 秒 `SIGKILL` | [design.md L297](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L297)、[L418](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L418) |
| 匯出 | `agora import session --external-session-id <id> --agent <agent>`：把 agent 自己的 session 匯入 Agora（Drive） | [design.md 5.2](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L219)；[README](https://github.com/FATESAIKOU/MyAiStorage/blob/main/README.md) |
| 送訊息 | sidecar 依緊急度決定「直接打斷」或「等 agent 下次輪詢再給」（尚未有協定細節） | [#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31) |

- **「暫停」目前沒有對應機制**。#31 把「暫停、終止」列在要設計的控制項裡，公開資料沒有更細的定義。#34（期 4）才把暫停放入全生命週期控制。
- **headless 化的落差（推論）**：`agora continue` 現在的契約是「前景啟動 agent」，agent（opencode／claude）本身是佔滿終端機的互動程式（[design.md L297](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L297)、[design.md 5.9](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L328)）。sidecar 若常駐在 worker 背景，要嘛提供 pty、要嘛 agora 需要一個非互動啟動模式——公開資料沒有說側的哪一邊吸收這個複雜度。
- **寫回時機的落差（推論）**：agora 的結果是「agent 結束後才寫回」；另有 #28 想加「執行期間每 5 分鐘同步」（[#28](https://github.com/FATESAIKOU/MyAiStorage/issues/28)）。sidecar 要監視長跑的 agent 時，可用的中間狀態取決於 #28 做不做。

### 1.3 登錄與發現

- #31 的設計第 1 點就是這個問題：「agent 怎麼向 sidecar 登錄，或 sidecar 怎麼找到 worker 上的 agent」。**尚未定案**，公開資料沒有進一步的設計或實作。出處：[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)。
- 相關的既有事實：agent 的 session 與工作目錄有對應關係（continue 一律在工作目錄開，[design.md 5.4](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L283)）；agora 用 `<state>/pending/<ULID>.json` 的 flock 判斷「接續中」（[design.md 4.2](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L146)）——這是一個現成的「某個 agent 正在跑哪個 session」線索（推論），但 sidecar 能不能直接用它並未被任何工單定案。

### 1.4 分發

- #31 的設計第 4 點：候選做法是做成 **MyLinuxPool 的 shared-config 單元（像 pool-runtime）**，讓任何 profile 都能帶，而不是另寫一個 profile，因為 profile＝部門；**由設計定案**。出處：[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)。
- MyLinuxPool 是 private repo（公開可見資訊：`FATESAIKOU/MyLinuxPool` 為 private），本文未讀其內容，因此無法查證 `pool-runtime` shared-config 的介面與權限模型。
- 期 4 相關：MyLinuxPool#27（定期監看）被 #34 引用為相關工單；同樣因 private 未讀。出處：[#34](https://github.com/FATESAIKOU/MyAiStorage/issues/34)。

## 2. Angareion 的現況

### 2.1 訊息格式（v0）

#30 定義 v0 的訊息有**六個欄位**（照引原文的欄位描述）：

- `from`、`to`：身分＋名字，可以是 group。
- `subject／channel`：這則訊息掛在哪個話題。
- `urgency`：數值越大越急。
- `content`：內文。
- `attachment`：參考資料陣列，每項是 `[名稱, {怎麼取得，或直接放內文}]`。

另外兩條性質（不是欄位）：「只追加、可以審計」「收件人是角色（repo × 職務），換 session 信也還在」。

出處：[MyAiStorage#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)。

其他 v0 定案：

| 項目 | 內容 | 出處 |
|---|---|---|
| 後端 | private GitHub repo；一個話題一個 issue、一則訊息一則留言，欄位寫在留言開頭 | [#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30) |
| 輪詢 | 用 ETag 輪詢，沒有新訊息就回 304，不吃額度 | 同上 |
| 指令 | `a2a send／inbox／ack`；後端包在指令後面，之後可以換 | 同上 |
| 部署 | 程式與部署設定放 MyAiStorage | 同上 |
| 叫醒與投遞 | 「叫醒與依緊急度投遞由 Agent sidecar 負責」——Angareion 本身不含喚醒邏輯 | 同上 |
| 非 v0 | 群組定址、同步溝通（期 2，另見 [#33](https://github.com/FATESAIKOU/MyAiStorage/issues/33)） | [#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30) |

**未見於公開資料的部分**：欄位的正式 schema（型別、必填／選填、message id、ack 的語意是「已讀」還是「已處理」、重送的冪等規則）、urgency 的值域與排序規則、attachment 的取得協定（用什麼身分去拿）。這些在 #30 只到欄位名稱與一行描述。

### 2.2 身分表示

- 收件端：**角色**，定義是 **repo × 職務**；「換 session 信也還在」——即信箱綁角色而不是綁 session。出處：[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)。
- `from`／`to` 的表示法：「身分＋名字」；群組定址延到期 2。出處：同上、[#33](https://github.com/FATESAIKOU/MyAiStorage/issues/33)。
- **#30 明列兩個必須設計的題目**：「身分怎麼表示；token 的範圍（只能讀寫信道 repo 的 issue）」。到調查日為止，公開資料沒有後續設計。出處：[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)。
- 周邊可以對照的既有身分概念（推論，僅供設計時參考）：
  - MyAiStorage 的 header 規範裡，actor 寫法沿用 OKF 慣例（例如 `opencode/<模型>`、`claude-code/<模型>`、`human:fatesaikou`），見 [design.md 3.1](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L41)。
  - agora 的 id 是 `<entity>:<不變 id>`（Agora 用 ULID），且四個實體（MyBrain／Agora／Foundry／Atelier）之間靠 header 互相指，不共用程式或儲存，見 [design.md 3](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L37)、[design.md 3.2](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L58)。Angareion 的「角色」是否要沿用這套 actor 語法，尚未定案。
  - 期 1 的 AI team 職務與 harness（Atelier）也在設計中：每個團隊一份 harness（know／do／judge／dont＋能力需求），由 sidecar 啟動 team 時載入，見 [MyAiStorage#32](https://github.com/FATESAIKOU/MyAiStorage/issues/32)、[MyAiStorage#17](https://github.com/FATESAIKOU/MyAiStorage/issues/17)。角色的粒度（repo × 職務 裡的「職務」怎麼對到 Atelier 的職務）公開資料未定。

### 2.3 與 sidecar 的分工

- Angareion 管「信道與訊息」；**叫醒與依緊急度投遞**是 sidecar 的責任。出處：[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)（末句）、[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)。
- sidecar v0 的功能就是「收 Angareion → 依緊急度投遞」＋「啟動、終止 team」。出處：[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)。
- 期 2 才設計部門／群組定址與同步溝通。出處：[#33](https://github.com/FATESAIKOU/MyAiStorage/issues/33)。

## 3. 對 LLMGateway 而言的接點

LLMGateway 現況（公開資料）：public repo，只有 [README.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/README.md)（「一人公司所有 AI agent 共用的模型窗口（設計中）」）與 [AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md)（唯一 commit `bd391c9`，2026-10-10）；還沒有 openspec root、沒有程式。AGENTS.md 對本專案的定義是：「把本人手上的訂閱（Claude 公司／個人、Ollama Cloud、OpenRouter、OpenCode Zen、Antigravity）收成統一的入口，並管理『哪台 worker、什麼角色、哪個訂閱、哪個 coding agent、哪個模型』的開關申請與紀錄。上層計畫是 MyPMO 的 AiCompany 期 1（MyPMO#31）與『啟動 LLMGateway』（MyPMO#4）。」——MyPMO 是 private repo，本文未讀。

以下三小節只回答「公開資料現在能說到哪」，其餘列進第 4 節的問題清單。

### 3.1 誰啟動 agent

- 公開資料的答案只有一個：**sidecar**。啟動用 `agora continue`；「AI team 的啟動也經過 sidecar」；v0 sidecar 負責「啟動、終止 team」。出處：[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)。
- LLMGateway 的公開資料（README／AGENTS.md）沒有任何「啟動 agent」的職責描述；它管的是模型存取與「開關申請與紀錄」。（推論）因此「核准一個開關之後，由誰、用什麼參數下啟動指令」是 LLMGateway 與 sidecar 之間尚未定義的動作。
- 可以對照的既有事實：`agora continue` 啟動時會決定「用哪個 agent」與工作目錄（`--agent`、`--dir`），而模型是 agent 自己設定／環境決定的（例如 summarize 用的模型可用 `AGORA_OPENCODE_MODEL` 指定，見 [design.md 5.3](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L276)）。（推論）「哪個模型」目前不是 agora 的啟動參數——LLMGateway 若要控制模型，注入點就在這一段，但公開資料沒有設計。

### 3.2 誰持有／注入憑證

- 公開資料能說的：LLMGateway 的**定位**是把訂閱收成統一入口（[README](https://github.com/FATESAIKOU/LLMGateway/blob/main/README.md)、[AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md)）→ 憑證（各訂閱的 API key／OAuth）預期由 LLMGateway 這一側掌管（推論；AGENTS.md 未寫持有方式）。
- **sidecar／worker 端 agent 怎麼拿到模型存取，公開資料完全沒有設計**：#31 的控制清單沒有「注入憑證」這一項；agora 的設計則明講 Drive 的 token 由 rclone 自己刷新、「agora 不碰 token」（[design.md 7 表 V4](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L478)），這是另一個憑證體系的既有慣例，不能直接推論到模型憑證。（推論）可能的形態（不互相排斥）：worker 上的 agent 各自登入自己的訂閱；或由 sidecar／worker 注入環境變數；或 agent 指向 LLMGateway 的端點、由 LLMGateway 代持上游憑證。三者都未被公開文件選定。
- 與權限模型相鄰的公開事實：#30 說 Angareion 的 token 範圍「只能讀寫信道 repo 的 issue」（[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)）——這是「最小權限」思路的既有範例。（推論）LLMGateway 的憑證設計是否沿用同一思路（每個角色／worker 只拿到自己的那一把），需要和 sidecar 一起定。
- 本調查沒有、也不會去讀任何憑證檔，因此以上只到「設計缺口」層級。

### 3.3 開關申請能不能走 Angareion 或 sidecar

- **形式可行性（推論）**：Angareion v0 的收件人是「角色（repo × 職務）」（[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)），而 LLMGateway 的業務正是「哪台 worker、什麼角色、哪個訂閱、哪個 coding agent、哪個模型」的開關申請與紀錄（[AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md)）——申請與核准的往返，形態上就是一對一的角色訊息，`urgency` 也適合表達急件。
- **sidecar 的角色（推論）**：如果「申請要叫醒受理方」，那是 sidecar 的投遞責任（#30 末句＋#31）；但 #31 v0 的範圍是「投遞給 agent」，沒有說「投遞給 LLMGateway 的受理角色」算不算。
- **未定案面**：申請的表單與狀態機（誰核准、駁回、失效）、核准之後的自動動作（改設定？重啟 agent？換模型？）、稽核的權威落點（Angareion 信箱 vs LLMGateway 自己的紀錄）、以及 LLMGateway 若直接讀信道 repo 需要的那把 token（#30 尚未定案）——公開資料都沒有。全部列入第 4 節。

## 4. 還沒定案、需要和 LLMGateway 一起決定的問題清單

### A. sidecar 與 worker（MyAiStorage 側）

1. **啟動介面**：sidecar 要用什麼方式啟動 agent？直接包 `agora continue` 的話，agora 需不需要新增 headless／daemon 模式（現在是前景＋TTY 的契約，[design.md L297](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L297)）？由 sidecar 自備 pty 可行嗎？
2. **終止與暫停的語意**：「暫停」是 SIGSTOP、存檔後結束、還是等下一則訊息？「終止 team」要沿用 TUI 的 process group 三段式中斷（[design.md L418](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L418)）嗎？
3. **登錄機制**：agent 主動向 sidecar 登錄（pull）還是 sidecar 掃描發現（discover）？agent 被 continue 重開（換 agent session id）時，身分要不要延續（[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31) 設計第 1 點）？
4. **sidecar 與 agora 的邊界**：sidecar 只呼叫 agora CLI，還是共用 pending／flock／outbox 等狀態（[design.md 4.2](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L146)）？誰負責崩潰補存（`recover_pending`）？
5. **緊急度投遞準則**：urgency 多高才「直接打斷」？打斷正在跑的工具呼叫是否安全（agora 是 agent 結束後才寫回，中途打斷的補存時機另見 [#28](https://github.com/FATESAIKOU/MyAiStorage/issues/28)）？
6. **分發與前提**：sidecar 做成 MyLinuxPool shared-config 單元（[#31](https://github.com/FATESAIKOU/MyAiStorage/issues/31)）的權限模型；v0 是否假設單一 worker、sidecar 與 agent 同機同使用者？
7. **與 #28／#29 的前後關係**：sidecar 要監視長跑 agent，依賴「執行期間同步」嗎？（[#28](https://github.com/FATESAIKOU/MyAiStorage/issues/28)、[#29](https://github.com/FATESAIKOU/MyAiStorage/issues/29)）

### B. Angareion（MyAiStorage 側）

8. **訊息 schema 定稿**：欄位型別、必填／選填、message id 的產生、`ack` 的語意（已讀／已處理）、重送的冪等規則、`urgency` 值域（[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30) 只到欄位名）。
9. **身分表示**：角色 id 的語法（repo × 職務）；a2a 的身分要不要沿用 OKF 的 actor 寫法（[design.md 3.1](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L41)）；「職務」怎麼對到 Atelier 的職務（[#32](https://github.com/FATESAIKOU/MyAiStorage/issues/32)）。
10. **Token 範圍與發放**：一人多角色時的最小權限切法；token 由誰發、放哪（部署設定在 MyAiStorage，[#30](https://github.com/FATESAIKOU/MyAiStorage/issues/30)）；輪替與撤銷。
11. **話題治理**：誰能開話題、話題的歸檔、以及**分級**：信道 repo 是 private，但內容仍要遵守「不寫 MyBrain 與私人資訊」的既有紀律（本 repo [AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md)、MyAiStorage [AGENTS.md](https://github.com/FATESAIKOU/MyAiStorage/blob/main/AGENTS.md) 都寫了這條）——信道裡要不要更明確的分級規則？
12. **投遞與叫醒的分工**：sidecar 沒跑的時候訊息怎麼等（純輪詢？）；`inbox` 的未讀狀態存哪（本機？信道？）。

### C. 與 LLMGateway 的交界

13. **開關申請的正式管道**：Angareion 角色訊息、sidecar 的請求佇列、還是 LLMGateway 自理的流程？三者誰是權威紀錄（單一真實來源）？
14. **憑證注入與撤銷**：worker 上 agent 的模型存取由 LLMGateway 發短期憑證、還是 worker 長期持有？輪替、撤銷、worker 停權時的即時切斷誰做？（公開資料未見設計）
15. **啟動決策的權威**：LLMGateway 核准一個開關後，由誰下啟動指令、用哪個模型／端點啟動？（agora 現在的啟動參數沒有「模型」這一項，見 3.1）
16. **角色的對映**：LLMGateway 的管理角色（受理開關的人／AI）在 Angareion 裡的身分是什麼（repo × 職務 的哪個組合）？申請人（worker team）的身分又是什麼？
17. **稽核與互相引用**：開關的申請與決定記錄放 LLMGateway 還是 Angareion？要不要用 OKF header 的 `refs` 互相指（[design.md 3.2](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L58)）？
18. **期 1 的依賴順序**：sidecar v0 依賴 Angareion v0；LLMGateway 是否也被期 1 驗收需要（上層是 MyPMO#31／#4，private 未讀）？三者的最小驗收交集是什麼？

## 5. 在本 repo（LLMGateway）建 OpenSpec change 需要先做什麼

### 5.1 現況（2026-10-10 實測）

- 本 repo **沒有** openspec 設定：在 repo 根目錄跑 `openspec context` 得到 `Error: No OpenSpec root found from the current directory. Fix: Run openspec init to create a root here.`
- 檔案只有 `README.md`、`AGENTS.md`、`docs/`（唯一 commit `bd391c9`）；`docs/research/` 是本檔所在的目錄。沒有 `openspec/`、沒有 `.opencode/`、沒有 `CLAUDE.md`。
- 本機 CLI 可用：`openspec --version` → **1.8.0**（實測）。MyAiStorage 已有完整的 openspec 使用範例可對照，但它的 change 建立時用的是哪個版本，公開資料無法查證。
- OpenSpec 本體：官方 repo [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)（官網 [openspec.dev](https://openspec.dev/)）；文件入口 [docs/README.md](https://github.com/Fission-AI/OpenSpec/blob/main/docs/README.md)。`openspec init` 的 `--tools` 在 1.8.0 支援 `opencode`、`agents`、`claude` 等三十多種（本機 `openspec init --help` 實測）。
- 可以照抄的鄰居：[MyAiStorage 的 openspec 目錄](https://github.com/FATESAIKOU/MyAiStorage/tree/main/openspec)——`config.yaml`（`schema: spec-driven`，[連結](https://github.com/FATESAIKOU/MyAiStorage/blob/main/openspec/config.yaml)）、主規格 `openspec/specs/<capability>/spec.md`（[連結](https://github.com/FATESAIKOU/MyAiStorage/tree/main/openspec/specs)）、歸檔 `openspec/changes/archive/<日期>-<change 名>/`（[連結](https://github.com/FATESAIKOU/MyAiStorage/tree/main/openspec/changes/archive)）、每個 change 有 `.openspec.yaml`＋`proposal.md`＋`design.md`＋`tasks.md`＋`specs/<capability>/spec.md`，以及 `.opencode/commands/opsx-*.md` 與 `.opencode/skills/openspec-*/SKILL.md` 這些給 opencode 用的包裝（[連結](https://github.com/FATESAIKOU/MyAiStorage/blob/main/.opencode/commands/opsx-propose.md)）。

### 5.2 前置步驟

1. **決定 root 與 AI 工具**：在 repo 根執行 `openspec init --tools opencode`（1.8.0 支援；若也要 AGENTS.md 系工具可加 `agents`）。這會建立 `openspec/`（`config.yaml`、`specs/`、`changes/`）並放置對應工具的 slash command／instruction 檔（例如 `.opencode/commands/opsx-propose.md`，見 [官方 docs/supported-tools.md](https://github.com/Fission-AI/OpenSpec/blob/main/docs/supported-tools.md)）。尚未執行——本步驟只調查。
2. **填 `openspec/config.yaml` 的 context**：1.8.0 的 config 支援 `context` 與 `rules`（[官方 customization 文件](https://github.com/Fission-AI/OpenSpec/blob/main/docs/customization.md)）；把本 repo 的規矩寫進去：這是 public repo、不寫 MyBrain 內容與私人資訊、不寫金鑰／token 實際值、工單規則以 MyPMO 為準（本 repo [AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md)）。
3. **建 change**：`openspec new change <kebab-case 名>`，依 spec-driven schema 產出 `proposal.md`、`specs/<capability>/spec.md`、`design.md`、`tasks.md`（[官方 getting-started](https://github.com/Fission-AI/OpenSpec/blob/main/docs/getting-started.md)；MyAiStorage 的 change 即是此形狀）。`openspec status`／`openspec instructions` 在 1.8.0 可以查每份 artifact 的要求（本機 CLI 實測）。命名與來歷要連到工單（例：`sidecar-llmgateway-interface`，proposal 說明對應 MyAiStorage#31／MyPMO#4）。
4. **驗證**：`openspec validate <change>`（本機 CLI 有此子命令）。寫作期間每份 artifact 隨時可改（OpenSpec 沒有硬性階段閘）。
5. **歸檔**：完成後 `openspec archive <change>` 會把 delta 併進 `openspec/specs/` 並移到 `openspec/changes/archive/<日期>-<change 名>/`（與 MyAiStorage 的歸檔慣例相同，見上）。
6. **（可選）store**：1.8.0 有 beta 的 store（跨 repo 的規劃根，[官方 guide](https://github.com/Fission-AI/OpenSpec/blob/main/docs/stores-beta/user-guide.md)）。若 LLMGateway 與 MyAiStorage 的 sidecar／Angareion change 想共用同一份規劃，可以評估；預設用就近的 `openspec/` 即可。
7. **與本 repo 的工單規則對齊**：本 repo 的 issue 是工單、有層級與狀態規矩（[AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md) 引 [MyPMO 工單規則](https://github.com/FATESAIKOU/MyPMO/blob/main/%E5%B7%A5%E5%96%AE%E8%A6%8F%E5%89%87.md)，private）——change 的 proposal 要能回答「為什麼／來源／相關」，與工單的內文段落對齊。

## 附錄：出處清單

- MyAiStorage：repo <https://github.com/FATESAIKOU/MyAiStorage>；README <https://github.com/FATESAIKOU/MyAiStorage/blob/main/README.md>；設計 <https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md>（5.4 continue：[L283](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L283)、[L295](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L295)、[L297](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L297)；TUI 中斷：[L418](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L418)；header：[3](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L37)、[3.1](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L41)、[3.2](https://github.com/FATESAIKOU/MyAiStorage/blob/main/docs/design.md#L58)）
- 工單：[#30 Angareion](https://github.com/FATESAIKOU/MyAiStorage/issues/30)、[#31 Agent sidecar](https://github.com/FATESAIKOU/MyAiStorage/issues/31)、[#32 Atelier＋harness](https://github.com/FATESAIKOU/MyAiStorage/issues/32)、[#33 Angareion 期 2](https://github.com/FATESAIKOU/MyAiStorage/issues/33)、[#34 sidecar 期 4](https://github.com/FATESAIKOU/MyAiStorage/issues/34)、[#28 continue 期間同步](https://github.com/FATESAIKOU/MyAiStorage/issues/28)、[#29 continue --fork](https://github.com/FATESAIKOU/MyAiStorage/issues/29)、[#17 Atelier 建立](https://github.com/FATESAIKOU/MyAiStorage/issues/17)
- LLMGateway：repo <https://github.com/FATESAIKOU/LLMGateway>；[README](https://github.com/FATESAIKOU/LLMGateway/blob/main/README.md)；[AGENTS.md](https://github.com/FATESAIKOU/LLMGateway/blob/main/AGENTS.md)
- OpenSpec：<https://github.com/Fission-AI/OpenSpec>；[openspec.dev](https://openspec.dev/)；[docs](https://github.com/Fission-AI/OpenSpec/tree/main/docs)；[supported tools](https://github.com/Fission-AI/OpenSpec/blob/main/docs/supported-tools.md)；[customization](https://github.com/Fission-AI/OpenSpec/blob/main/docs/customization.md)；[stores（beta）](https://github.com/Fission-AI/OpenSpec/blob/main/docs/stores-beta/user-guide.md)
- 本機實測（2026-10-10）：`openspec --version` → 1.8.0；`openspec context`（在 LLMGateway 根目錄）→ No OpenSpec root；`openspec init --help` 的 `--tools` 清單含 `opencode`、`agents`。
- 未讀（依規矩）：MyPMO（private；工單規則、MyPMO#4／#19／#23／#31）、MyLinuxPool（private；pool-runtime、MyLinuxPool#27）、任何憑證檔。
