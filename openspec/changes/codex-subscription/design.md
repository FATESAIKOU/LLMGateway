# design：Codex（ChatGPT 訂閱）納入 Gateway（#187）

查證一律只用公開官方文件，讀取日期 2026-11-07；沒有讀任何憑證值或本機登入檔。官方文件網址 `developers.openai.com/codex/...` 會 308 轉到 `learn.chatgpt.com/docs/...`，下面兩種都寫。引文是讀取工具回傳的短引文（每段 ≤125 字元）；查不到的寫「查不到」，推論標「推論」。完整查證表（含 URL）在作者報告，本檔只留會影響決定的事實。

## 0. 前提與名詞

- Codex 的形狀與 agy 相同：憑證由呼叫方那一台機器自己保管，Gateway 不搬、不匯出、不代管。
- 「本人」指一人公司的本人；「待本人」是本 change 不替本人定的決定。

## Q1 kind：沿用 `local-login`

決定：不新增 kind。`local-login` 的定義（`subscription-registry`「沒有憑證的訂閱」）就是「憑證由呼叫方那一台機器保管」，與 #187 第 2 項「值只走腳本、不經 AI」吻合。真正的差異只有「哪個 agent 綁哪把訂閱」，用條款表就能表達。

被捨棄：另開 `codex-login` kind。`build_env`、`deployed`、usage 都會多一條分支，行為卻與 `local-login` 完全一樣。

## Q2 命名

provider `openai`、訂閱 `chatgpt-personal`（日後有公司方案就加 `chatgpt-company`）、agent `codex`。與 MyLinuxPool 對齊的字串見 §介面 B1。

被捨棄：訂閱叫 `codex`（訂閱是方案，不是 agent；與 `claude-personal` 並列才一致）。日後若接 OpenAI API key，同一個 provider 以 api-key kind 另開一把並 bump `env_schema`；這次不預留。

## Q3 泛化 local-login 的條款

決定：`_AGY_ONLY`（單一訂閱名）改成「local-login 訂閱 → 唯一 agent」的對照表 `{"antigravity": "agy", "chatgpt-personal": "codex"}`。每一把的 `agents` MUST 剛好是那一個；`agy`、`codex` 不得出現在其他訂閱。`build_env` 的雙重防線改成「`agent` 屬於 local-login agent 集合，**或** `kind` 為 `local-login`，任一成立就回 `{}`」。agy 的既有情境全部不變（P1 是純重構，C-1）。

為什麼雙重防線不能少：只判 agent 擋「codex 進了某筆 api-key row」；只判 kind 擋「`chatgpt-personal` 的 `agents` 被改弱成含 `claude-code`」。兩條合起來，任一邊被改弱仍回 `{}`。

被捨棄：每個 local-login 訂閱各寫一個常數（`_AGY_ONLY`、`_CODEX_ONLY`…）。第三把出現時又要複製一份。

同理，`role-allocation` 的「agy 的候選」與本 change 加的「codex 的候選」各寫一條，沒有合併成通則：第三把 local-login 出現時再合併（不在本 change）。

## Q4 resolve 回什麼

`agent=codex&provider=openai[&model=]` → 200，`meta.kind` 為 `local-login`，`env` 為 `{}`，`env_schema` 仍是 1，模型只走 `meta.model`。`CODEX_HOME` 與 config 由 sidecar 決定，不進 env（`resolve-env`「env 不含呼叫方的維運變數」）。`meta.usage.status` 為 `not_applicable`。

## Q5 模型 id 與 effort（查證）

事實：
- 模型 id：官方 models 頁（`developers.openai.com/codex/models` → `learn.chatgpt.com/docs/models`，2026-11-07）列出 `codex -m gpt-6-astra`、`codex -m gpt-6.1-sol`、`codex -m gpt-6.1-terra`、`codex -m gpt-6-luna`；另有 `gpt-6-sol`（同頁：「Replace gpt-5.4 with gpt-6-sol and gpt-5.4-mini with gpt-6-luna when available to your plan and client.」，審查者核對原文；pricing 表也列「GPT-6 Sol」）；頁面並寫模型「available to your plan」，即**依方案而不同**（models 頁另寫：「On Free and Go plans, choose GPT-6 Luna ( gpt-6-luna ) in the desktop app when available.」，那是 gpt-5.5 下架的替代建議，不是『只有』的證據），所以可用清單與 Q9 的方案種類連動；非互動也是 `codex exec -m gpt-6.1-sol "…"`；config 可設 `model = "gpt-6.1-sol"`。「GPT-6.1 Sol」是顯示名，id 是 **`gpt-6.1-sol`**（小寫、點號、連字號）。該頁**不是**完整清單（讀取工具回報only shows these commands for the recommended models）；完整的「可接受 id」清單：查不到。
- 版本：「GPT-6.1 Terra requires Codex CLI 0.159.0 or later.」
- effort：models 頁用的是互動選單（`/model`），頁面沒有 `model_reasoning_effort` 這個鍵名。設定鍵見設定參考頁（`developers.openai.com/codex/config-reference` → `learn.chatgpt.com/docs/config-file/config-reference`；審查者 2026-11-07 抓頁核對，作者先前讀到 404 是轉址路徑不同），原文：「model_reasoning_effort string Reasoning effort advertised by the selected model, such as low , medium , high , xhigh , max , or ultra . Available levels depend on the model and client.」可用 `codex -c model_reasoning_effort="xhigh"` 覆寫（搜尋摘要，未在官方頁原文確認：推論）。「Astra's Light setting is `low`」（models 頁）。

決定：`models.allow` 放裸 id（上述五個 `gpt-6.1-sol`、`gpt-6.1-terra`、`gpt-6-astra`、`gpt-6-luna`、`gpt-6-sol` 為起點，P4 時再對一次官方頁，且要依 Q9 的方案種類取實際可用者），**effort 不進 id**，由 sidecar 用 `model_reasoning_effort` 設定。理由：Codex 的 effort 是獨立設定，且各模型的可用值不同；編進 id 會讓 Gateway 替 agent 拆字串。

與 agy 先例不一致的理由：agy 的 id 本身就帶 `-low`／`-medium`／`-high`（agy 只從 `meta.model` 取模型，沒有別的通道）；Codex 有獨立設定通道，所以不必。Gateway 因此不知道 effort（B3）。

被捨棄：把 effort 編進 id（`gpt-6.1-sol-high`）。

## Q6 同一份 auth.json 多處使用（查證；分發方式待本人）

事實：
- 儲存：「Codex caches login details locally in a plaintext file at `~/.codex/auth.json` or in your OS-specific credential store.」設定 `cli_auth_credentials_store`：`file | keyring | auto | ephemeral`；`file`＝「stores credentials in `auth.json` under `CODEX_HOME` (defaults to `~/.codex`)」；`auto`＝「uses the OS credential store when available, otherwise falls back to `auth.json`」。管理員可強制此設定。（auth 頁，`developers.openai.com/codex/auth` → `learn.chatgpt.com/docs/auth`）
- 自動更新：「Codex refreshes tokens automatically during use before they expire…」（同頁）
- 序列化原文（CI/CD 進階頁，`developers.openai.com/codex/auth/ci-cd-auth` → `learn.chatgpt.com/docs/auth/ci-cd-auth`）：「Use one `auth.json` per runner or per serialized workflow stream.」「Do not share the same file across concurrent jobs or multiple machines.」「only one machine or serialized job stream will use a given `auth.json` copy」。
- 刷新時機（ci-cd-auth 頁，審查者核對原文）：「Codex loads the local auth cache from auth.json if last_refresh is older than about 8 days, Codex refreshes the token bundle…」與「if a request gets a 401 , Codex also has a built-in refresh-and-retry path」。所以互撞主要發生在約 8 天一次的刷新與 401 重試，對下面 (b) 的判斷與 B5 的急迫程度有幫助；不改結論。
- 輪替：該頁列出需要重新種 `auth.json` 的情況，含「another machine or concurrent job rotated the token first」與「the refresh token was revoked or expired」，**間接**說明 refresh token 會輪替；頁面沒有出現「rotation」字樣，也沒有「refresh token has already been used」這句（讀取工具回報）。搜尋摘要寫「Codex rotates the refresh token and retires the previous one」並引該錯誤訊息，來源是搜尋摘要與第三方整理：標推論。「重用是否作廢」：官方頁沒有明說，查不到；與輪替合起來的推論是「舊 token 被另一份先換掉後，這一份會失效」。
- 搬移：auth 頁把「Authenticate locally and copy your auth cache」列為 headless 的備案（在有瀏覽器的機器 `codex login`，再把 `~/.codex/auth.json` 複製到同一路徑；「Treat `~/.codex/auth.json` like a password: it contains access tokens.」）；若 OS 用 credential store，這法「may not apply」，可改用 file 儲存。CI 進階頁的前置：種檔用 `cli_auth_credentials_store = "file"` 登入，確認 `auth_mode` 是 `"chatgpt"` 且 `has_refresh_token` 為 `true`；「seed `auth.json` only if it is missing」。
- device code：auth 頁稱「prefer device code authentication (beta)」用於 headless；`codex login --device-auth`；要先在 ChatGPT 安全設定（個人）或工作區權限（管理員）啟用 device code 登入。**多台 device code 登入能否並存：官方頁沒有寫，查不到**；推論是各登入各有一組 token，不共用 refresh token（沒驗證）。
- Linux 無桌面：`auto` 在沒有 credential store 時回退到 `auth.json`（原文如上），`file` 則固定用檔案；「Linux 無桌面實際會走哪條」沒有官方實測敘述，查不到——所以 sidecar 應固定 `cli_auth_credentials_store = "file"`（B6）。

選項（待本人選）：
- **(a) 每台 worker 各自 device code 登入**：每台有自己的 session，沒有共用 refresh token，符合「one auth.json per runner」；代價是每台要本人核准一次（先在 ChatGPT 安全設定啟用 device code）。注意：同一台 worker 上若有多個 codex 共用同一個 `CODEX_HOME`，仍會互撞，所以仍需 sidecar 對同一個 `CODEX_HOME` 序列化或限制並行為 1（B5）。
- **(b) 只複製一份**：Mac 不再用，只給一台 worker，並行數 1。簡單，但吞吐量最小，且 Mac 上的 codex 要停用。
- **(c) 同一份複製到多台**：官方明寫不要（「Do not share the same file across concurrent jobs or multiple machines」），**不建議**。

建議：(a)。(a) 做不到（例如帳號不允許 device code）時退 (b)。不論哪個選項，Gateway 都不參與，也不序列化；這是呼叫方（sidecar／capability）的責任，以描述句寫進 delta（subscription-registry 的『沒有憑證的訂閱』），不是 Gateway 的行為。

被捨棄：**由 Gateway 保管並發放 auth.json**。(1) Codex 自己更新並輪替 refresh token，Gateway 那份在第一次更新後就過期，每次 resolve 發一份副本等於把衝突放大；(2) key 檔格式是五行 `KEY=VALUE`，放不下 JSON 檔；(3) refresh token 會走 resolve 的 SSH 通道進每個 agent 的環境，暴露面比 Claude 的 OAuth token 大；(4) 與 #119 的分發路線不一致。

## Q7 用量讀取（查證；U0 夠不夠待本人）

事實：
- `codex exec --json` 的事件為 `thread.started`、`turn.started`、`turn.completed`、`turn.failed`、`item.*`、`error`；`turn.completed` 只帶 token 用量（`input_tokens`、`cached_input_tokens`、`output_tokens`、`reasoning_output_tokens`）。**沒有 rate limit 資料**（non-interactive 頁，`developers.openai.com/codex/noninteractive` → `learn.chatgpt.com/docs/non-interactive-mode`）。
- 互動式：「In an active CLI session, … `/status` to see remaining limits.」，儀表板在 `chatgpt.com/codex/settings/usage`（pricing 頁）。
- 有機器可讀的出口：app-server 的 `account/rateLimits/read`（「fetch ChatGPT rate limits」）與通知 `account/rateLimits/updated`（「emitted whenever a user's ChatGPT rate limits change」）。快照欄位：`usedPercent`（窗口內已用百分比）、`windowDurationMins`（窗口長度）、`resetsAt`（Unix 秒）、`limitId`、`limitName`、`planType`、`rateLimitReachedType`，另有 `rateLimits`（單桶相容視圖）與 `rateLimitsByLimitId`（多桶視圖）。範例回覆中 `primary` 有 `usedPercent`／`windowDurationMins`／`resetsAt`，`secondary` 為 `null`；`primary`／`secondary` 與「5 小時／每週」的對應，頁面沒有文字說明，查不到。（app-server 頁，`developers.openai.com/codex/app-server` → `learn.chatgpt.com/docs/app-server`；此頁有 147590 字元，只讀到後段的 rate limits 片段。）

作者提議（待本人）：試用期用 **U0**——`usage` 為 `not_applicable`，加冷卻訊號，與 agy 相同。不新增取用量實作，所以 `usage-reporting` 的文字不必改。

其他選項（本 change 不做）：
- **U1**：sidecar 啟動 `codex app-server` 呼叫 `account/rateLimits/read`，再把結果推給 Gateway。要新端點與新契約（P6）。Gateway 不持有登入，符合 Q1。
- **U2**：讓 Gateway 那台自己登入一份，比照 Claude 跑原版 CLI 讀用量。Gateway 就成了「持有 ChatGPT 登入」，違反 Q1 的前提，且多一個使用者與一份 auth.json，**不建議**。

待本人：試用期 U0 夠不夠？建議夠（只靠冷卻；人工讀 `/status` 或儀表板）；期滿要「依用量決定」時再開 P6（U1）。

## Q8 冷卻怎麼標（查證）

事實：額度用完的錯誤文字有多種版本，來源是 openai/codex 的 GitHub issue #12299 與搜尋摘要（官方文件沒有寫錯誤原文，查不到官方原文）：
- 「You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Feb 23rd, 2026 9:01 PM.」
- 「You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 1:36 PM.」（只有時刻、沒有日期）
- 搜尋摘要另列一種逐限額的版本：「You've hit your usage limit for <limit>. Switch to another model now, or try again at ...」，未在原文確認。
- 時刻格式：舊的報告含日期（`Feb 23rd, 2026 9:01 PM`），新的只有時刻（`4:01 AM`、`1:36 PM`）；**時區都沒有標**。
- 程式介面：app-server 的 turn 失敗帶 `codexErrorInfo` 為 `usageLimitExceeded`（「Common `codexErrorInfo` values include `contextWindowExceeded`, `usageLimitExceeded`, `badRequest`, `unauthorized`, and `sandboxError`」）。
- 容量錯誤是另一類：「Selected model is at capacity」不是額度用完（第三方整理，未在官方確認），規則不可誤判成 `exhausted`。

決定（Q8 定案＝(ii)）：不改 Gateway 端點。`lgw/signals.py` 加 `agent: "codex"` 的規則（P3），排在通用規則之前：含「usage limit」的文字 → `exhausted`；**codex 的額度錯誤文字一律退回「現在＋1 小時」**（`subscription-cooldown`「判讀不出恢復時間的預設」），**含日期但無時區的也一樣**，不從文字換算 `resets_at`。理由：所有已知的真實錯誤文字（上面的 `Feb 23rd, 2026 9:01 PM`、`4:01 AM`、`1:36 PM`、`Feb 25th, 2026 8:07 PM`）都沒有標時區，而時刻是 Codex 所在機器的本地時間或帳號時區，沒有任何官方文件說明；猜錯時區的 `resets_at` 比預設的 1 小時更糟（冷卻過長就浪費額度，過短就重打）。

後續選項（i），本 change 不做：比照 claude-code，由 sidecar 傳 `classify(agent, text, now=None, tz=None)` 的 `tz`（`lgw/signals.py`，呼叫方傳 agent 所在機器的 tz），有日期與只有時刻都換算。前提是 codex 印的是 agent 所在機器的本機時間，這一點**未查證**，已列進 B7 待查；查證後再開一張。

冷卻粒度（推論）：5 小時與每週的上限先落在**整把冷卻**。依據只有 pricing 頁原文「Local messages and cloud chats share your plan's usage allowance. Weekly limits may also apply.」——講的是本機與雲端共用，**沒有說各模型共用**；app-server 頁的範例回覆有多個額度桶：`"rateLimitsByLimitId" : { "codex" : {…}, "codex_other" : { "limitId" : "codex_other" , … "windowDurationMins" : 60 …} }`，欄位說明為「rateLimitsByLimitId (when present) is the multi-bucket view keyed by metered limit_id」；上面逐額度的錯誤文字也暗示有分桶。所以「整把共用一個額度」**是推論**，整把冷卻只是保守預設（某一桶用完就冷卻整把，會浪費其他桶的額度）；`model_cooldowns` 只在官方證實某個模型有獨立額度時才用（pricing 頁的範例表對各模型列不同的估計範圍，但說明那是「not fixed message limits」，不構成獨立額度的證據）。5 小時窗口依方案而異：pricing 頁「Pro plans currently have no five-hour limit.」。

被捨棄：只有時刻時假設為本機時區換算。時區未標，錯誤的 `resets_at` 比預設的 1 小時更糟。

## Q9 隱私等級（查證；等級與能不能碰真資料待本人）

事實：
- 個人方案（Free／Plus／Pro）：「Improve the model for everyone」也適用 Codex 任務；關閉位置：ChatGPT Settings → Data Controls，或隱私入口的「Do not train on my content」。關閉後新的對話與 Codex 任務不用於訓練；只對**新**對話生效；對回覆按讚／倒讚可能讓整段對話被用於訓練。（搜尋摘要，引自 help.openai.com 的 Data Controls FAQ 與 5722486 兩篇；讀取工具與審查者對這兩篇都回 403，**未讀到原文**，標推論。）
- Codex 另有獨立設定 "Include environments"（Codex cloud 設定頁），控制能否拿完整環境訓練；改 ChatGPT 設定不會改它。（同上，搜尋摘要；本地 CLI 是否適用：查不到。）
- Business／Enterprise／Edu：pricing 頁官方原文「No training on your business data by default.」（審查者核對）；pricing 表只有 Enterprise 列「Data retention and data residency controls」。零保留的證據：**目前無**。
- Codex CLI 送出什麼：模型請求與驗證請求走 client 本身（agent-approvals-security 頁：「the client's model and authentication requests」不經網路 proxy 過濾）；具體 payload（提示、檔案內容）官方頁沒有逐項列，查不到。推論：工作目錄內被 agent 讀取的檔案內容與提示必然送出。
- 遙測（agent-approvals-security 頁、設定參考頁，審查者核對）：「Telemetry is off by default; enable it explicitly in your configuration.」OTel 預設關。`[otel]`：`exporter`（預設 `none`）、`log_user_prompt = false` 會遮蔽提示。但設定參考頁寫「otel.metrics_exporter … The default, statsig , follows analytics.enabled」，即 metrics 預設是 statsig、要靠 `analytics.enabled = false` 關，所以也要設 `otel.metrics_exporter = "none"`。`[analytics] enabled = false`（匿名使用與健康資料送 OpenAI；設定參考頁有 `analytics.enabled` 的原文）。`[history] persistence = "none"`（本機歷史，`save-all`｜`none`；設定參考頁有原文）與 `history.max_bytes`。
- 保留期：auth 頁（審查者核對）：「When you sign in with ChatGPT, Codex usage follows your ChatGPT workspace permissions, role-based access control (RBAC), and ChatGPT Enterprise retention and residency settings.」即 ChatGPT 登入的 CLI 走 ChatGPT 帳號的保留設定；具體保留天數查不到（搜尋到的 30 天是其他功能）。
- 本人試用的是哪一種方案：工單沒寫，**不知道**；它決定上面哪一組條款適用，也決定 Q5 的可用模型。
- 隱私等級對照（PMO 給的）：◎ 零保留且不訓練＝可碰真資料（real）；○ 不訓練但保留一段時間＝程式與一般資料（general）；△ 匿名或限時預覽；✗ 可能訓練＝只公開程式與假資料（public）。

決定（形狀）：等級寫進 `privacy`（`role-allocation`「訂閱的隱私等級」），不加新欄位。Gateway 的 `privacy` 假設 sidecar 已固定把 `[analytics] enabled=false`、`[otel] exporter="none"`、`otel.metrics_exporter="none"`、`log_user_prompt=false`、`history.persistence="none"` 寫進 codex config（B8）；這是前提，要寫進 sidecar 的契約。

待本人（結論不替本人定；下面是依 PMO 對照表的條件式對應）：
- 個人方案、訓練未關或未確認＝✗→`public`（只公開程式與假資料）。
- 個人方案、本人確認已關訓練（且知道只對新對話生效、有按讚例外、"Include environments" 另有開關）＝○→最多 `general`。
- Business＝○（預設不訓練，但保留）→`general`。
- `real` 只有證實零保留（◎）才給；目前沒有任何方案的證據，所以不給。
- 在本人確認方案與訓練設定前，P4 先放最保守的 `public`；能不能碰真資料：目前不能。
- 本人試用的方案種類（Plus／Pro／Business）與訓練設定：請 PMO 向本人確認。

## Q10 使用界線（查證；界線待本人）

事實：
- 官方認可 `codex exec` 用於腳本與 CI：「from scripts (for example, continuous integration (CI) jobs)」。預設「`codex exec` reuses saved CLI authentication」。
- 官方偏好 API key：「API keys are the right default for automation because they are simpler to provision and rotate.」（non-interactive 頁；auth 頁同說 API keys 仍是自動化的建議預設）。
- 官方也寫了用 ChatGPT 帳號跑 CI 的進階路（`ci-cd-auth`）：用於「trusted runners」、想用 ChatGPT／Codex 額度而非 API key 的情況；non-interactive 頁與 ci-cd-auth 頁都有原文（審查者核對）：「Do not use this workflow for public or open-source repositories.」它緊接在 auth.json 種檔流程之後，指的是「用 ChatGPT 帳號登入跑 CI」這條路。
- OpenAI 使用條款：搜尋摘要稱禁止「automatically or programmatically extract data or Output」、禁止分享帳號憑證或讓他人使用你的帳號、禁止規避速率限制。條款頁（openai.com/policies/row-terms-of-use）讀取回 403，**未讀到原文**，只能引搜尋摘要，標推論。
- 第三方（非官方）討論：在 openai/codex 的 Discussion #8338，維護者回覆稱條款與授權「quite permissive」，並不是政策聲明。OpenAI 沒有像 Anthropic 那樣明文禁止第三方工具使用訂閱 token（搜尋摘要；查不到官方原文）。

對照本 repo 對 Claude 訂閱的寫法（`usage-reporting`「Claude 取用量」）：只執行**未修改的原版 Claude Code**；Gateway MUST NOT 自己用訂閱 token 對 API 發請求，MUST NOT 收集或轉手 session token。

架構與界線的分工：delta 只保留「local-login 一對一綁定表：`chatgpt-personal.agents` 剛好是 `["codex"]`」的**形狀**（`subscription-registry`「local-login 的 agents 條款」，屬架構，P2 實作）。下面的**使用界線**不在 delta，屬本人，待 Q10 決定：

作者提議（待本人，Q10）：比照 Claude，只用**原版 Codex CLI** 搭配本人的訂閱登入；Gateway 不代理、不轉手 token、不用訂閱 token 打 API；不接 opencode 或其他第三方客戶端。若本人選 (ii)（允許 opencode 的 ChatGPT 登入外掛），綁定表的形狀要跟著改（`chatgpt-personal` 就不再只綁一個 agent），tasks 的 C-9b 也要改。

待本人：
- 界線是否照上面？選項：(i) 只原版 Codex CLI（建議）；(ii) 也允許 opencode 的 ChatGPT 登入外掛（官方未明文禁止，但屬第三方客戶端用訂閱 token，比照 Claude 的風險；不建議）。
- worker 上跑 codex 的 repo 若是公開 repo（本 repo 即是），上面「不要用在公開或開源 repo」的官方提醒是否適用？該提醒針對 CI 上執行 repo 控制的程式碼；隊員在 worker 上跑、不接外部 PR，推論不同，但請本人裁定。

## Q11 roles

`roles.impl` 與 `roles.rev` 的 `order` 要不要加 `chatgpt-personal`、排第幾，是政策資料，Q9 定了才放（floor 會篩）；MLP 的 codex driver（#797）能開之前不放進 `order`，否則有人照 roles 挑到它卻開不起來。

待本人／PM：
- 選項：(i) 先只登錄訂閱、不進任何 `order`（建議；P4 只做這一步，main 不會更糟）；(ii) 進 `impl`、`rev` 的 `order`，排在 `claude-personal` 之前（分擔週額度，目標與工單一致；需 Q9 等級與 #797 就緒）；(iii) 只進 `impl`（審查隊員要與實作隊員用不同的 `(provider, model)`——worker 環境說明的選模規則；codex 只有一把訂閱，兩個角色都用它時模型要不同）。
- 排序、priority 數字：PM 與本人在 P4 決定。

## 介面（與 MyLinuxPool；待 PM 確認後對齊）

Gateway 負責：訂閱登錄（list）、resolve 契約（`agent=codex`、`provider=openai`、`env {}`、`meta.model`、冷卻提示）、冷卻端點、隱私與 roles 的篩選、`lgw.signals` 的 codex 判讀。MLP（sidecar codex driver #797、capability 分發）負責：`auth.json` 的保管、分發、放置與權限，token 更新衝突（序列化或各用一份），codex 的 config 與執行參數，額度錯誤發生後登錄冷卻。

- B1：agent `codex`、provider `openai`；sidecar 遇到 `provider=openai` 時改走 resolve（`agent=codex`）。
- B2：sidecar 消費 `env {}`：不期待憑證或端點，模型只取 `meta.model`，用 `-m` 或 config 傳；前綴正規化照 `subscription-registry`「models.default 的語意」。
- B3：effort 由 sidecar 用 `model_reasoning_effort` 設定，Gateway 不知道。
- B4：Q6 選的分發方式落在 MLP。值只走腳本、不經 AI；PMO 不經手；更新後的 `auth.json` 不回寫正本；憑證失效時 `exit.reason` 怎麼標、怎麼請本人重新登入由 MLP 定。
- B5：同一個 `CODEX_HOME` 的並行上限為 1（或序列化）。
- B6：sidecar 固定 `cli_auth_credentials_store = "file"`（Linux 無桌面不依賴 keyring）。
- B7：額度錯誤用 `lgw.signals.classify("codex", …)` 判讀，再以 `POST /v1/subscriptions/chatgpt-personal/cooldown` 登錄（`model` 不帶 `codex/` 前綴，帶了回 400）。**待查**：codex 錯誤文字裡的時刻是 agent 所在機器的本機時間還是帳號時區（Q8 後續選項 (i) 的前提）；未查證前一律「現在＋1 小時」。
- B8：遙測、分析、歷史設定（Q9）由 sidecar 固定：`analytics.enabled=false`、`otel.exporter="none"`、`otel.metrics_exporter="none"`、`log_user_prompt=false`、`history.persistence="none"`。
- B9：#798 與 #119 共用同一套 capability 分發；Gateway 在 P1 泛化後，agy 與 codex 同形狀。

## 風險與未驗證

- 所有外部事實來自公開網頁，其中官方頁經讀取工具轉述（短引文）、help.openai.com 與條款頁讀取被 403 擋下，改引搜尋摘要；這些項目已標「推論」。
- 模型 id 清單、"GPT-6.1 Sol" 以外的 id 是否都被所有方案接受，P4 時對一次官方頁再定。
- 本 change 沒有任何程式，所以沒有跑測試；契約編號對應的測試在各 PR 補。
