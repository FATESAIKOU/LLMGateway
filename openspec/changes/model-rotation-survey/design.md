## Context

動機見 proposal.md（#58）。現況（出處：本 repo `policy/default.json`、`openspec/specs/{key-vault,subscription-registry,connectivity}`；MyLinuxPool#99 與其 workflows 只讀）：

- Gateway 只綁 loopback，只能經 SSH 轉發連入；resolve 無應用層憑證，**能連到的就能 list、resolve**（key-vault 規格 :27）。resolve 回覆的 `env` 含 key，只准進 agent 子行程環境。
- Gateway 沒有「週用量百分比」訊號；稽核紀錄只有 subscription／action／agent／until。
- MyLinuxPool 的 workflows 全部跑在同一個 self-hosted runner，服務該 repo 的部署；LLMGateway 目前沒有用它。
- GitHub 上 LLM secret 已全部收掉（MyLinuxPool#99）。

查證日期：2026-10-24（作者），審查者同日另行核對。

## 1. 憑證走哪條路

### 一手來源

- S1 `https://code.claude.com/docs/en/legal-and-compliance`：OAuth「intended exclusively for purchasers of … subscription plans … ordinary use of Claude Code」；開發者做產品「should use API key authentication」，不可代使用者轉送或保管 Claude.ai 憑證；不阻止「an end user from signing in to the unmodified Claude Code binary with their own Claude subscription」；「Advertised usage limits for Pro and Max plans assume ordinary, individual usage」。
- S2 `https://code.claude.com/docs/en/authentication`：`claude setup-token` 產一年期 token，設為 `CLAUDE_CODE_OAUTH_TOKEN`「wherever you want to authenticate」，「for CI pipelines and scripts」；`apiKeyHelper` 可取「short-lived tokens fetched from a vault」。
- S3 `https://code.claude.com/docs/en/github-actions`：secret 名 `ANTHROPIC_API_KEY`／`CLAUDE_CODE_OAUTH_TOKEN`；OAuth token 用的是訂閱額度；觸發者須有 write access；fork PR 拿不到 secret；範例跑在 GitHub-hosted runner。
- S4 `https://github.com/anthropics/claude-code-action/blob/main/docs/security.md`：`schedule`／`workflow_dispatch` 不另查；公開 repo 用 `include_comments_by_actor` 限定哪些人的 comment 進得了 Claude；subprocess 環境的 secret 清除只是 best-effort，「reduces but does not eliminate」（該句講的是清除功能，不是整個 action）。
- S5 `https://docs.github.com/en/actions/reference/security/secure-use`：self-hosted runner「should almost never be used for public repositories」，任何人開 PR 都可能攻陷環境並取得 secrets。
- S6 `https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository`：fork PR 的設定名為「Require approval for all external contributors」（要求核准，不是關閉）。
- S7 `https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access`：「You can use policies to limit access to self-hosted runners that have been added to an organization.」；「Organization owners using the GitHub Team plan can create additional organization-level runner groups.」（runner group 與限定 workflow 屬 organization 功能）。本 repo 的 owner 是個人帳號（PM 以 `gh api users/<owner> -q .type` 核對為 `User`）。

### 選項表

| | (a) self-hosted runner＋原版 Claude Code＋向 Gateway resolve | (b) GitHub secret `CLAUDE_CODE_OAUTH_TOKEN` | (c) 免費或 Ollama Cloud 模型 | (d) 本機（Mac）排程跑原版 Claude Code，`gh` 開 PR |
|---|---|---|---|---|
| 取得登入 | job 經 SSH 轉發 `GET /v1/resolve?agent=claude-code&provider=anthropic`，把 `env` 給 `claude -p` 子行程。身分＝runner 的 SSH 金鑰 | 本人 `claude setup-token` 貼進 GitHub secret（S2、S3） | 同 (a)／(d)，provider 改 `openrouter`／`ollama-cloud`／`opencode-zen` | 兩個子選項，見下 |
| 條款（S1） | 可：原版未改、本人自己的訂閱與用量；OAuth 設環境變數給 CI／script 的依據是 S2。每天自動跑是否算「ordinary use」**未查證** | S3 明列的作法；同左 | 不涉 Claude 條款；各家免費條款未查證 | 同 (a) |
| 規矩「key 全放 LLMGateway、GitHub 無 LLM secret」 | 符合 | **違反**：MyLinuxPool#99 的結論與 key-vault 規格「其他地方不再放 key」 | 符合 | 符合 |
| 跑在哪、爆炸半徑 | self-hosted runner。**runner 被攻陷＝它能 resolve 得到的全部 key 外洩**（key-vault :27）；S5 說 public repo 幾乎不該用 | GitHub-hosted；token 一年期，洩漏即被用 | 同 (a)／(d) | Mac；權限大，靠工具白名單與乾淨 worktree 限制 |
| 會壞在哪 | 轉發斷、runner 離線或被攻陷、log 洩漏 | public log、prompt injection 讀走 token | 能力欄由 AI 評 AI，弱模型較不準 | Mac 關機或睡眠 |
| 成本 | Claude 訂閱用量＋runner 維運 | 同 (a)＋Actions 分鐘 | 免費或 Ollama 用量 | Claude 訂閱用量 |

(c) 與本人要的差別：本人指定「Claude Code」；(c) 換模型後能力欄可信度下降，但完全不碰 Claude 條款疑慮。

**(a) 的必要條件（不是選項）**：LLMGateway 用**專屬** runner、不與 MyLinuxPool 共用；runner group 只允許這一個 workflow（S7）；fork PR 設為「Require approval for all external contributors」（S6）；workflow 只開 `schedule`／`workflow_dispatch`／本人的 comment 觸發；用 `GITHUB_TOKEN` 並設最小 `permissions`。缺一條就不建議。**在個人帳號的 repo 下，runner group 與單一 workflow 限制做不到**（S7）；要滿足就得把 repo 移到 organization，或接受「只註冊在本 repo 的 runner、沒有單一 workflow 限制」（此時必要條件不成立，等於不建議 (a)）。

**(d) 的子選項**：
- (d1) 直接用 Mac 上本人已登入的原版 Claude Code：少一層、token 不經環境變數；但憑證在 Claude Code 自己的存放處，不經 Gateway，**不符合「key 全放 LLMGateway」的字面**（Mac 本機不是 GitHub，所以不違反 GitHub 那半句）。
- (d2) 先向 Gateway resolve，把 `env` 放進 `claude -p` 的環境：符合規矩，多一層。
- 排程用 launchd（`StartCalendarInterval` 睡眠後喚醒會補跑；這是推論，未實測），不用 cron。

### 建議

**(d)**，子選項見判斷點。理由：唯一同時符合規矩、不需把 runner 暴露給 public repo、又不放 GitHub 憑證的作法。若本人堅持 Actions，次選 (a) 須先把 repo 移到 organization 並滿足上列必要條件；否則不建議。不建議 (b)。

## 2. 讀 PR comment 再修改，只認本人

- **固定一個分支（`survey/current`）與一個開著的 PR**：有就更新，沒有就開。不會有多個互相衝突的調查 PR。
- **誰能觸發（兩路各自防）**：
  - 共通：`comment.user.login` ＝ owner 且 `author_association == OWNER`；不用字串（如 `@claude`）當授權；comment 內容只當資料（寫檔或 stdin），不插進 shell。
  - (a)：`issue_comment` 要先檢查 `github.event.issue.pull_request` 存在；機器人用 `GITHUB_TOKEN`，login 是 `github-actions[bot]`，不符 owner 條件，自然不會自觸發；另用 `include_comments_by_actor`（S4）。
  - (d)：`gh` 用本人登入，機器人的 comment 也是 OWNER，**不能靠身分區分**。結構上的作法：機器人的**所有** comment 第一行都以 `<!-- survey-bot` 開頭（回覆另加 ` in-reply-to:<comment id>`，如 `<!-- survey-bot in-reply-to:123 -->`）；讀取時凡帶此開頭的一律排除。
- **只處理本 repo 的 PR**：條件＝`isCrossRepository == false`（head repo 是本 repo），且 PR 作者是機器人或 owner，且分支為 `survey/current`。分支前綴不算數（fork 可取同名）。(a) 另避免 `pull_request_target`／`workflow_run` 簽出 fork 程式（S4、S5）。
- **兩步的輸入**（每日調查、套用 comment）都是「`survey/current` 上那兩個資料檔的現況」，只取這兩個檔（`docs/model-rotation.md`、`docs/model-rotation.json`），不用 base 上的舊表，不 checkout PR 的其他內容（避免 PR 帶進的 `CLAUDE.md`、`.claude/` 影響 Claude）；沒有 `survey/current` 時才以 base 為起點。Claude 只在一個**只放這兩個檔的暫存目錄**裡工作；腳本把結果放回 `survey/current` 的 worktree，以它最新的 commit 為 parent 再 commit（push 為 fast-forward，不 force push）。
- **每日調查與本人改過的格**：PR 可能開好幾天，本人用 comment 改過的內容不能被隔天的調查蓋掉。二選一，見判斷點 7：(i) 每日調查只更新可客觀取得的欄位（清單、價格、額度），本人改過的格保留；(ii) 只要有未 merge 的調查 PR，就暫停每日調查（只處理 comment）。
- **冪等（狀態從 GitHub 推出，不靠本機狀態檔）**：「已處理」與 push 綁在同一個動作：commit 訊息加 trailer `Survey-Comment: <comment id>`，判斷時看 `survey/current` 的 commit 紀錄；回覆 comment 只用來顯示。**處理完一律留下 commit**：沒有修改（提問、確認、判斷不需改、改失敗）時用 `git commit --allow-empty`，trailer 照樣帶，否則該 comment 每次執行都會被重處理。改失敗時，顯示用的回覆要寫明失敗與原因，本人可重留 comment。
- **每日調查與套用 comment 兩步都套用**同一組限制：工具白名單（抓取公開網頁、`Read(...)`／`Edit(...)` 路徑規則寫成只限那兩個檔，不給任意 shell；**`Read(...)` 規則能否限制到工作目錄內的其他檔未驗證（推論：預設可能允許讀取），所以主要靠「工作目錄裡本來只有那兩個檔」，規則只是第二層，實作時再驗**）與固定工作目錄；push 與 `gh pr create`／`gh pr comment` 由腳本在 Claude 結束後做，不交給 Claude。外部網頁內容也是 prompt injection 來源（S4），輸出會進 public PR。

## 3. 調查來源、推估與能力欄

- **OpenRouter 清單**：`GET https://openrouter.ai/api/v1/models`（文件 `https://openrouter.ai/docs/api-reference/models/get-models`）。審查者 2026-10-24 實測不帶 key 回 200。欄位：`id`、`pricing.prompt`／`pricing.completion`（字串）、`context_length`、`supported_parameters`，**沒有 free 旗標**；以 `id` 結尾 `:free` 或 pricing 皆 `"0"` 判定，不一致標「待確認」；需排除非聊天模型（實測有 pricing 為 0 的音樂模型）。
- **OpenRouter 免費額度**（`https://openrouter.ai/docs/api-reference/limits`）：免費模型每分鐘 20 次；每天 50 次或 1000 次（依累計購買 credits）。這是**整個帳號共用的請求數上限**，不是每個模型各一份。
- **OpenCode Zen**：文件 `https://opencode.ai/docs/zen/`，清單端點 `https://opencode.ai/inference/v1/models`（審查者實測不帶 key 回 200，每筆只有 `id`／`object`／`created`／`owned_by`，**沒有價格**，免費只能從名稱判斷）。官方**未公布數字額度**，只有「free for a limited time」與各模型的資料處理說明。
- **「推估每天可用免費 token 量」的比例欄（誠實說明）**：OpenRouter 各列共用同一個帳號上限，用「請求數×平均 token」算出的比例只會反映 context 長度，不是真的可用量；Zen 查不到。所以原公式**不成立**。選項見判斷點 3。無論選哪個，每格都標來源：`官方數字`（附 URL＋日期）、`共用`、`查不到`；不填猜測值。
- **能力欄**（terminal、coding、抽象架構設計、綜合）：每格 1–5 分（作者提議），旁標依據：指定的一個公開榜單（見判斷點 5）有收錄就引其分數並附 URL＋日期，沒收錄才標「主觀」。表頭固定標明「能力欄為 AI 評分，非實測」。排序以本人 prompt 的順序，綜合能力為主排序鍵。
- **位置與格式**：`docs/model-rotation.md`（人讀）＋`docs/model-rotation.json`（機器可讀，供差異比對）。PR 只改這兩個檔。
- 不寫本人的訂閱額度與用量、帳號、key。

## 4. 與政策檔、MyPMO 手冊的關係

- **表只當參考，不自動改政策**。`policy/default.json` 的 `models.allow`／`default` 仍走獨立的政策變更 PR，由本人審；理由：政策是守門（條款、計費），不該由含主觀欄位的每日產物驅動。
- **MyPMO 手冊**（private，只描述結構，不抄內容）：手冊現況包含：依角色與 session 種類分開的多張候選表、資料處理的隱私分級欄、以一個基準模型定的實作／審查門檻、依工作類型的經驗表、排除清單與理由、非免費來源的列、共通使用規則。本人 prompt 指定的欄位（比例、四項能力、提供商）**不含**隱私、角色、門檻。
- **建議的切分**：新表**只取代手冊各表的「候選模型與數字來源」**；角色分表、隱私分級、門檻、經驗表、排除清單、非免費列、共通規則**留在手冊**（或另行決定）。手冊該部分改成指向 `docs/model-rotation.md`；手冊的修改由 MyPMO 另開工單，不在本 change。
- **門檻與分數**：手冊的門檻是以「分數欄」定的；若分數改由新表（公開榜單）提供，兩邊要對同一個指數與版本。二選一：(i) 門檻引用的分數以新表為準；(ii) 分數欄留在手冊。見判斷點 4。

## 5. 每天跑的成本與超量跳過

- **一次調查的 token（推估，未實測）**：腳本先把兩邊清單與價格壓成小檔再交給 Claude；約 15–30 回合，輸入（含快取讀）約 200k–500k token，輸出約 15k–30k token。第一週以實際用量校正。
- **週用量佔比**：額度數字不寫進 public repo；佔比目前**未知**，需實測。每次受 `--max-turns` 限制。
- **70%／90% 規則**：Gateway 沒有用量百分比訊號，Claude Code 是否能程式化讀週用量**未查證**。因此只做**能觸發的分支**：開始前檢查「暫停旗標」（本人手動維護）或 Gateway 上該 subscription 的 cooldown，在就略過當天；resolve 遇到額度訊號時照既有路徑登錄 cooldown。若日後查到可讀的用量訊號，再加門檻。
- 預設**不**啟用 (c) 的免費模型 fallback。

## 6. 本人的判斷點

1. **憑證路線**：(d)（建議）／(a)／(b)（需本人明確放寬規矩）／(c)。(a) 在個人帳號的 repo 下做不到必要條件（runner group、單一 workflow），除非把 repo 移到 organization，或接受只註冊在本 repo 的 runner 且沒有單一 workflow 限制。
2. **(d) 的子選項**：(d1) 直接用 Mac 上本人的登入（少一層，但不經 Gateway）／(d2) 先 resolve 再設環境變數（符合規矩）。**建議 (d2)**，維持「key 全放 LLMGateway」一致；若覺得多一層不值得，可選 (d1) 並明文放寬 Mac 本機這一例。每天自動跑是否算「ordinary use」（S1 未明說）也請一併知悉。
3. **比例欄**：(i) 留欄位，OpenRouter 標「共用」、Zen 標「查不到」；(ii) 拿掉這欄；(iii) 改成列「官方公布的原始額度」不算比例。**建議 (i)**：保留本人要的欄位，但不假裝有推估。
4. **MyPMO 手冊搬與留的切分**：見第 4 節。**建議**「新表只取代候選模型與數字來源，其餘留在手冊」；是否要把手冊那些也搬過來，由本人決定。另：門檻引用的分數，(i) 以新表為準／(ii) 分數欄留在手冊；建議 (i)，單一來源，但新表要固定指數版本。
5. **能力欄的主要依據**：指定一個公開榜單（建議 Artificial Analysis 的 Intelligence Index），沒收錄的模型才標「主觀」。建議採用，可重現性比每天 AI 自由判斷高。
6. **權重與基準**：綜合能力是否由前三欄加權（作者詮釋）、權重多少（建議 1:1:1）；若比例欄保留，基準模型由本人定。
7. **每日調查 vs 本人改過的格**：(i) 只更新客觀欄位、保留本人改過的格／(ii) 有未 merge 的調查 PR 就暫停每日調查。**建議 (ii)**：不需要判斷哪些格是本人改的，邏輯最簡單；代價是 PR 未 merge 期間表不更新。
8. **comment 套用方式**：建議每則 comment 自動套用並回覆差異，由本人 merge 把關。

## 7. 作者提議、本人未指示

1. 機器可讀檔 `docs/model-rotation.json`。
2. 每格來源標籤（官方數字／共用／查不到／主觀），查不到不填猜測值。
3. 固定分支 `survey/current` 與單一開著的 PR；「已處理」由 commit trailer `Survey-Comment: <id>` 推出。
4. 機器人所有 comment 首行 `<!-- survey-bot`（回覆另加 `in-reply-to`），(d) 靠它避免自觸發。
5. 只處理 `isCrossRepository == false` 的 PR；在乾淨 worktree 只改兩個資料檔。
6. 兩步（調查、套用）共用的工具白名單與工作目錄限制。
7. 腳本先壓縮清單再交給 Claude，以降 token。
8. 綜合能力＝加權（作者的詮釋）、能力欄 1–5 分制。
9. 以旗標或 cooldown 作為超量略過機制。
10. 新表加一欄「資料政策（官方原文）」（Zen 文件每個模型都有這段，是公開資訊），以承接手冊的隱私分級需求。
11. 能力欄指定單一公開榜單為主要依據。
12. 可選的較短輪詢，讓套用 comment 不必等到隔天（預設關）。
13. (a) 的 `GITHUB_TOKEN` 最小 `permissions`、排程用 launchd、`--max-turns` 上限、「PR 作者是機器人或 owner」的條件。
14. `Read(...)`／`Edit(...)` 路徑規則限定兩個檔；push 與開 PR／回覆由腳本在 Claude 結束後做。
