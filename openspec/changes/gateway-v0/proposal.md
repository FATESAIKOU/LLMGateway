對應工單：#8「LLMGateway v0：切 scope、接上訂閱，讓 Mac 的 opencode／Claude Code 經由 Gateway 使用」；上層：MyPMO#4。

> scope 提案第 2 版（2026-10-11），照 PMO 轉達的本人裁定改寫（裁定全文見 #8 的留言）。確認後才補 design、specs、tasks 並實作。第 1 版「Gateway 持有 Claude 訂閱 token 代轉」因為 Claude 條款不採用，理由見 `docs/research/claude-failover.md` §7。

## Why

本人的原始需求：AI 在 worker 上工作到一半 token 用完時，由外部的確定性程式自動換一把。可以換的有五把：

| 訂閱 id | 是什麼 |
|---|---|
| `claude-personal` | Claude Code 個人訂閱 |
| `claude-company` | Claude Code 公司訂閱 |
| `openrouter` | OpenRouter key |
| `opencode-zen` | OpenCode Zen key |
| `ollama-cloud` | Ollama Cloud key |

Claude 訂閱的 token 只能交給原版 Claude Code 自己用，不能經第三方代轉。所以 LLMGateway 不轉送模型請求，而是**保管 key、出租 key、決定換哪一把、記錄誰在用**。換的動作由 worker 上的 sidecar（MyAiStorage#31）執行。這是期 1 設計「租約＋紀錄」的延伸。

## v0 做什麼

### 1. 保管處：五把 key 只放在 LLMGateway 一處

- 來源是本 repo 的 GitHub secret，值由本人設。
- 一支部署 workflow 經池的 SSH 跳板，把 key 寫進池內常駐 provider 上的一個 0600 檔，只有 LLMGateway 服務讀得到。
- repo、log、紀錄、狀態板都不出現值。
- 其他地方（MyLinuxPool、Mac 的設定檔）之後都不再放 key。收掉的時機見「銜接」。

### 2. 租用服務：跑在池內常駐的 provider，只綁 loopback

- 一個小 HTTP 服務，加上 SQLite 的只追加事件紀錄。池內有常駐主機以後，期 1 設計「後端用 private GitHub repo」改成本機 SQLite，因為冷卻與同時租用上限都要即時判斷。
- 客戶端各持一把「客戶端憑證」，只用來認身分，不是上游 key：worker 的 sidecar 一把、Mac 一把、之後手機一把。客戶端都經 SSH 埠轉送連入。
- 介面（給 sidecar 的 `ModelBroker` 與 Mac 的包裝指令共用）：

| 動作 | 輸入 | 回傳 |
|---|---|---|
| `request_lease` | worker、角色、coding agent、模型（可空）、訂閱（可空，空的就照順序選）、來由 | 租約 id、選到的訂閱、**要設給 agent 的環境變數**、效期；或拒絕（理由；全部冷卻時附最早的重置時間） |
| `report_exhausted` | 租約 id、偵測到的訊號、重置時間（有就給） | 那一把進冷卻，原租約結束；**順便回傳下一張租約**（或拒絕） |
| `renew` | 租約 id | 新效期 |
| `report_usage` | 租約 id、用量快照（例如 Claude status line 的 `rate_limits`） | — |
| `release` | 租約 id | — |
| `board` | — | 狀態板（JSON；另有一頁唯讀 HTML） |

- 回傳的環境變數（實際變數名以試做 c 的結果定案）：

| 訂閱 | 給 Claude Code | 給 opencode |
|---|---|---|
| `claude-personal`／`claude-company` | `CLAUDE_CODE_OAUTH_TOKEN`（Claude Code 直接連 Anthropic，Gateway 不代轉） | 不提供（條款） |
| `openrouter`、`ollama-cloud` | `ANTHROPIC_BASE_URL`＋`ANTHROPIC_AUTH_TOKEN`（`ANTHROPIC_API_KEY` 設成空字串）。兩家都有官方的 Claude Code 接法 | 各自的連線位址與 key |
| `opencode-zen` | 不提供：官方沒有 Claude Code 接法，只有社群做法 | 連線位址與 key（opencode 官方支援） |

- sidecar 和包裝指令只把環境變數交給 agent，**不寫進檔案、不保存**；租約結束或換把時就丟掉。同時確保 agent 環境裡沒有其他把的值，例如 `ANTHROPIC_API_KEY` 會蓋過訂閱 token，一律清掉。

### 3. 選把與切換（判斷在 LLMGateway，執行在 sidecar）

- **順序**（提案，政策檔可以改）：
  - Claude Code：`claude-personal` → `claude-company`。個人優先，因為那是本人自己的訂閱；公司那把用在個人的 agent 上比較灰，所以只在個人冷卻時才用。兩把都冷卻就拒絕並通知。Claude Code 也接得上 OpenRouter、Ollama Cloud，但那會換掉模型（OpenRouter 上的 Claude 模型還要付費），所以預設不接在 Claude 後面；要接由政策檔打開。
  - opencode：`opencode-zen`（免費模型）→ `openrouter`（只用 `:free` 模型）→ `ollama-cloud`。照本人的用量原則，Ollama 最後才用。
- **切換流程**：
  1. sidecar 偵測到額度用完的訊號（試做 d）。
  2. `report_exhausted`：那一把進冷卻，有重置時間就記下。
  3. LLMGateway 照順序回下一張租約。
  4. sidecar 安全結束 agent，以新環境變數重開。
  5. `agora continue` 接回同一個 session。
  - 正在跑的那一輪會中斷（本人已知）。
- **換回**：冷卻結束後，**下一次新開或重開** agent 時照順序選，所以會回到優先度高的那一把。正在順利跑的 agent 不會為了換回而被打斷。
- **降低風險的做法**（本人要求寫進設計）：
  - 冷卻中不硬撞：在重置時間（再加一點緩衝）之前，那一把一律不出租，也不重試。拿不到重置時間的，用保守的預設冷卻時間。
  - 只有「額度用完」才切換。短暫的 429 由 agent 自己重試，不觸發切換，避免來回跳。
  - 每把有同時租用的上限（政策檔設；Claude 兩把預設各 2），超過就拒絕或排隊。
  - 同一個 agent 不會同時拿兩把 Claude。
  - 每一次出租、冷卻、切換、拒絕都留下紀錄，並通知 PMO（Angareion 可用後走 Angareion；之前寫在狀態板與 log）。

### 4. 狀態板

- 每把 key 一定有：誰在用（worker、角色、agent、從何時開始）、幾個在用／上限、是否冷卻、冷卻何時結束。這些都來自 LLMGateway 自己的紀錄。
- 用量 %，有更好、沒有也行：
  - Claude：sidecar 掛 status line 腳本，回報 `rate_limits.five_hour`、`rate_limits.seven_day` 的已用 % 與重置時間。官方文件寫只有 claude.ai Pro／Max 才有，公司方案與 setup-token 下有沒有，要試做 a。
  - OpenRouter：服務定期查 `GET /api/v1/key`。
  - Ollama Cloud：文件上有 `GET /api/balance`（有剩餘 % 與重置時間），試做 b 用 key 確認。
  - OpenCode Zen：截至 2026-10-11 查無公開的用量 API，顯示「未知」。

### 5. Mac 走同一套

- 包裝指令（暫名 `lgw run claude`、`lgw run opencode`）：先租用、把環境變數交給 agent、結束時 release。
- 額度用完時，v0 在 Mac 上只做「回報＋拿下一張＋提示用 `lgw run -c` 接續」，不自動重開，因為 Mac 上通常有人在看。
- 原本的 `claude`、`opencode` 不動，隨時可以退回。

### 6. 程式

- 核心（保管處、租用、冷卻、狀態板、包裝指令）自己寫。
- 不用 sub2api：它的價值在持有訂閱 token 代轉，對 Claude 不能用。
- LiteLLM 只在「某個上游接不上 Claude Code、又一定要用 Claude Code」時才加，做格式轉換。要加就固定版本、用雜湊或 image digest 查核，因為 PyPI 上曾有被植入的版本（`docs/research/litellm-supply-chain.md`，查證中）。

## 要先試做確認的（結果寫進 `docs/research/`，影響 design 的細節，不影響上面的形狀）

| 項目 | 怎麼做 | 誰做 |
|---|---|---|
| (a) setup-token 下 status line 有沒有 `rate_limits`；公司方案有沒有 | 用本人的 token 開一個原版 Claude Code，掛只把 `rate_limits` 寫進檔案的 status line 腳本，送一則最短的訊息 | PM（不印出 token 的值）或本人 |
| (b) Ollama Cloud、Zen 有沒有查用量的 API | 查官方文件；有的話再用 key 打一次 | 文件：隊員（`quota-signals.md`）；實打：PM |
| (c) 三家能不能讓 Claude Code 直接接 | 文件結果（`upstream-claude-code-compat.md`）：OpenRouter、Ollama Cloud 有官方接法；Zen 沒有。前兩家再用 key 讓 Claude Code 跑一則最短的訊息確認 | 文件：隊員（完成）；實打：PM |
| (d) 各 agent 額度用完的訊息格式、能不能拿到重置時間 | 文件結果（`quota-signals.md`）：Claude Code 的 `You've hit your … limit · resets …`、opencode 的 `session.error`（402／429 與 `retry-after`）、agy 的 `Individual quota reached … Resets in …`，大多拿得到重置時間。實際用完當下的輸出要等真的碰到時記錄 | 隊員（完成）＋PM |

隊員不碰憑證，也不打需要 key 的 API。

## 手機 App 怎麼連上（評估，v0 不實作）

前提：在新方向下，手機**不需要讓模型流量經過 Gateway**。OpenRouter、Ollama Cloud、Zen 本身就是 OpenAI 相容，手機租到 key 就直接打上游；Claude 訂閱不能給手機 App 用（條款）。所以手機只需要連到**租用服務**，流量很小。

| | (1) 另寫一個等同 mlp 的手機 App，把機器池控制整個拆出來，在裡面做 port forward | (2) 擴充既有 App（MyAiEntry）的機器池功能，加上 port forward |
|---|---|---|
| 可沿用的 | 要重做 SSH、池的解析、私鑰保管 | 既有 App 已有 SSH（jsch，本身支援本機埠轉送）、池的解析、私鑰保管、機器池控制畫面 |
| 連線 | AI App 連另一個 App 開的本機埠；兩個 App 都要活著，Android 背景限制要處理兩次 | 同一個 App 內：開轉送、租用、打上游都在一起 |
| 職責 | 機器池控制和 AI 對話分開，比較乾淨；之後其他 App 也能用這條轉送 | AI App 變得更大；但機器池控制本來就在裡面 |
| 成本 | 新 App 的建置、簽章、發佈、測試全套 | 加一個轉送功能（含前景服務與斷線重連） |

**建議 (2)**：成本最低，而且需要轉送的只有這個 App。等出現第二個需要池內服務的手機 App，再把轉送拆成 (1)。實際由 MyAiEntry 那邊開單（接 MyAiEntry#31、#33）；本 repo 只提供租用介面與手機用的客戶端憑證。

## v0 不做

| 項目 | 進不進 v0 | 理由 |
|---|---|---|
| #2 多訂閱無縫切換 | **部分進** | 自動換把會做（本提案的核心），但不是無縫：那一輪會中斷，靠 `agora continue` 接回 |
| #3 免費模型依效能自動切換 | 不進 | blocked by #4 |
| #4 隱私界線 | 不進 | v0 只照固定順序換把，不依內容改送別的模型。opencode 那條鏈本來就以免費模型為主，等 #4 定案再收斂 |
| #6 和 sidecar 對齊 | **進** | 本提案的租用介面就是 `ModelBroker` 要對齊的形狀；草擬好後直接和 aistorage-pm 對齊 |
| #7 手機 App 的入口 | 只評估 | 見上一節；實作在 MyAiEntry |
| Antigravity（agy） | 不進 | 不在這五把裡（用 Google 帳號登入） |
| 資料面代轉、格式轉換 | 不進 | 只在試做 c 證明「非加不可」時才另外提 |

## 和期 1 設計的差別

- **決定 2「上游憑證值不經過 LLMGateway」→ 改成「上游 key 只放在 LLMGateway 一處，出租給 launcher」。** launcher 仍然只注入租約指定的那一把；差別在 key 的來源從各機器的能力掛載，改成向 LLMGateway 租用。
- **決定 3「v0 後端用 private GitHub repo」→ 改成常駐 provider 上的服務＋SQLite。**
- **決定 4（切換）不變**：launcher 執行、紀錄與通知照舊；選把加上「順序＋冷卻＋同時租用上限」。
- **決定 1（不做資料面）不變。**
- 期 1 的 change 在 v0 的 design 裡以 MODIFIED 改。

## 驗收

1. **租用**：Mac 上 `lgw run claude -p "只回 OK"` 成功。紀錄有租約（客戶端＝Mac、訂閱＝`claude-personal`）與 release；agent 環境裡只有那一把。
2. **切換**：用假的額度用完訊號（不真的燒額度）→ `claude-personal` 進冷卻 → 下一張是 `claude-company` → 狀態板顯示冷卻與重置時間 → 冷卻到期後新開的租約回到 `claude-personal`。
3. **sidecar**：sidecar 的假 broker 換成真的租用服務後，跑一次「額度用完 → 換把 → `agora continue` 接回」。可以在一台 worker 上做，或用測試替身做。
4. **opencode**：`lgw run opencode` 對三家 key 型上游各跑通一次。
5. **狀態板**：顯示五把的使用者、數量／上限、冷卻、用量 %（拿不到的顯示「未知」）。
6. **秘密**：repo、log、紀錄、狀態板掃描不到任何 key 的值。
7. **退回**：停用服務、Mac 改回用原本的指令，一切照舊。

## 銜接

- **MyLinuxPool#50**：先照原樣做，當過渡（本人裁定）。租用做好後另開單，把 worker 的 key 來源換成租用，並收掉 MyLinuxPool 裡的 key；屆時 #50 大概縮成「確認 worker 連得到 LLMGateway」。
- **MyLinuxPool**：
  - 部署 workflow 要一把能經跳板登入 provider 的 Actions 身分；
  - 服務要做成 provider 上的一個單元；
  - worker 容器要連得到服務。
  - 這三件另開 MyLinuxPool 的工單。
- **MyAiStorage#31**：照上面的租用介面和 aistorage-pm 對齊 `ModelBroker` 的形狀（`request_lease` 回傳環境變數、`report_exhausted`、`release`）。
- **MyAiEntry#31／#33**：手機用的轉送照「手機 App」一節的建議在那邊開單。

## 要本人確認

1. 換把順序：Claude Code「個人 → 公司」、opencode「Zen → OpenRouter（只用免費模型）→ Ollama Cloud」。
2. 同時租用上限的預設：Claude 兩把各 2；其他三把先不設上限。
3. Mac 上額度用完時只提示、不自動重開。
4. 手機 App 走 (2)：擴充既有 App。
