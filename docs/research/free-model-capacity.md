# 免費模型容量：盤點、配置建議與紅線

- 對應工單：#80（`層級:d`）
- 調查日：2026-10-26（UTC）
- 範圍：所有「不用付錢」的模型來源；每一個結論都附公開出處（URL 加原文引用）或本 repo 的 `檔案:行號`
- 方法與界線：只讀公開頁面（官方文件、官方 repo、條款頁）。**沒有**對任何推論端點送請求（帶不帶 key 都沒有）、**沒有**打本機的 Gateway 或 sidecar、**沒有**讀憑證檔、**沒有**跑 `opencode` CLI。
- 引文標籤：
  - **官方**：產品文件或條款的原話（附 URL）
  - **觀測**：官方沒有明文，但有多個可重現的使用者回報（附 issue）
  - **推論**：我從前兩者推的，不是原文
  - **查不到**：沒有公開資料，不填猜測值

> 一句話結論：**不用付錢的來源裡，只有 opencode-zen 的 `*-free` 與 OpenRouter 的 `:free` 是我們現在真的接得上的；兩者的額度都比「三個 agent 同時跑一整天」小一個量級。要撐住團隊，缺的是「多來源」與「不要把一個模型用完當成整把用完」，不是再找更大的免費桶。**

---

## 1. 結論摘要

1. **zen 免費層不是「一個模型一個桶」，而是帳號／workspace／網路（觀測：再加一層 IP）共用一個桶。** 官方原話是「Free limits change with capacity on purpose, so we don't publish a fixed number.」（§3.1）→ 任何「先用便宜的模型探路、把好的留給長任務」的調度，在 zen 上都只是在同一個桶裡換位置。
2. **opencode-go 的「免費模型」不是免費來源，是 $10/月訂閱的附帶福利**（官方：`Go` `$10/month`）。今天線頁的免費清單只剩 **Beam Free**，而且 Beam Free 有區域限制（不含中國與港澳）。#78 的預設 `iceman-contributor-free` 在今天的線頁已看不到，**部署前要確認 id 還在不在**（§3.2）。
3. **OpenRouter `:free` 是目前唯一「有公布數字」的免費桶**：終身買過 10 credits 以上（官方門檻寫成未展開的模板 `{FREE_MODEL_CREDITS_THRESHOLD - 1}`，推論為 9）是 **20 RPM／1000 RPD**，沒買過是 **20 RPM／50 RPD**；按 UTC 日重置；官方明說**多開帳號或多把 key 不會提高**（"we govern capacity globally"）（§3.3）。
4. **Gemini 免費層在 opencode 端接得上、在 Gateway 還接不上，而且法律上不能碰真資料**：官方條款寫「Do not submit sensitive, confidential, or personal information to the Unpaid Services.」，而且人類審閱者可能讀你的 prompt 與輸出（讀之前會與帳號、key、專案脫鉤）（§3.4）。
5. **Groq 與 Cloudflare Workers AI 是兩個被忽略的免費來源，但只有 Cloudflare 的資料條款能撐「不訓練」**：Groq 免費方案有公布 RPM/RPD/TPM/TPD（`gpt-oss-120b`：30 RPM／1000 RPD／8K TPM／200K TPD），而且明文「預設不保留」，但**訓練政策那一頁沒寫**（查不到）；Cloudflare 是 **每天 10,000 neurons、00:00 UTC 重置、逐字寫明不訓練**（§3.6、§3.7）。兩者都要新增 Gateway 的 provider 才接得上。
6. **Cerebras、GitHub Models、Hugging Face、Mistral 都不成立**：Cerebras 官方 FAQ 直接寫「Is there a permanently free tier? No.」；GitHub Models 2026-07-30 已退役；HF 免費帳號的 Inference Providers 額度是 `None`；Mistral 的 Free 是 Vibe／Studio 產品方案，API 免費層**查不到**（§3.8～§3.11）。
7. **Antigravity（agy）的免費額度綁在個人 Google 帳號登入上，而且官方說不支援 BYOK**（§3.5）→ 以我們「不用本人登入」的紅線，**agy 不能當團隊供給**，不管它額度多大。
8. **#76 只做完了 Gateway 的一半。** 冷卻已經以模型為單位、resolve 不再擋人（§4.4.2），但 sidecar 送冷卻時**仍然沒有帶 `model`**（`sources.py:429-431`），所以一個免費模型 429 還是會把**整把**標成冷卻——只是現在這只影響提示與 sidecar 的選把，不會再擋住開隊員；而且 sidecar 的 fallback 是固定 1 小時（`core.py:139`），不是 #76 設計的「免費模型 600 秒起加倍、上限 3600」。

---

## 2. 盤點總表

「接得上」＝ Gateway 現在有沒有這個 provider、sidecar 的 opencode／claude-code 能不能用。細節與引文在 §3 各節。

| # | 來源 | 上限與重置 | 按什麼計 | 條款要點（訓練／自動化／多帳號） | 我們接不接得上 | 適合的工作 | 詳 |
|---|---|---|---|---|---|---|---|
| 1 | opencode-zen `*-free`（含 `big-pickle`、`jev-1.13-free`） | **數字不公布**（官方）；重置 **00:00 UTC**（觀測）；跨模型共用一桶（觀測） | 帳號／workspace／網路（官方）；另有 **IP** 一層（維護者） | 只准在 OpenCode 裡用（403）；未付費帳號的內容可被用於改進服務；部分免費模型會訓練、部分不會（逐模型不同） | **接得上**（`opencode-zen`，限 agent `opencode`） | 短任務、審查；**不要碰真資料**（依模型而定） | §3.1 |
| 2 | OpenCode Go 的限時免費模型（今天只有 `beam-free`） | 免費模型標 **Unlimited**（限時）；其餘模型是月額度切 5h／週／月三窗 | 與 Zen 共用同一種 OpenCode Console key（官方 issue #77 的標題「共用 Zen key」）；**是不是同一個桶查不到**（2026-10-26 一次觀測指向不同桶，§3.2） | Beam Free：不訓練、保留 30 天；**區域限制**（不含中國、港澳等） | **接得上但沒部署**（#78）；**需 $10/月 Go 訂閱** | 短任務、備援；不碰真資料 | §3.2 |
| 3 | OpenRouter `:free`（`*:free`、`openrouter/free`） | **20 RPM**；**50 RPD**（終身購買 <10 credits）／**1000 RPD**（≥10；官方原文寫 `{FREE_MODEL_CREDITS_THRESHOLD - 1}`，推論為 9 起） | 帳號（官方：全域容量，多帳號無效） | 多開帳號繞限制＝違規；模型條款各自決定訓練 | **接得上**（`openrouter`，限 `opencode` + `*:free`） | 短任務、審查（`openrouter/free` 會路由到不同上游） | §3.3 |
| 4 | Gemini API 免費層（AI Studio key，`gemini-3.8-flash` 等） | 官方頁**不列數字**，只在 AI Studio 顯示；RPD **太平洋時間午夜**重置 | **per project**（不是 per key） | 未付費服務的內容會被用來改進產品、人類審閱者可能讀（讀前會與帳號脫鉤）；**不要送敏感／機密／個資**；EEA／CH／UK 適用付費條款 | **接不上**（Gateway 無 gemini provider）；opencode 有 google provider | 審查、長任務（品質高）；**不碰真資料** | §3.4 |
| 5 | Antigravity CLI（agy） | 免費帳號「a meaningful quota, refreshed weekly」＋週限；數字不公布 | 個人 Google 帳號 | 需登入；官方：**不支援 BYOK**；額度可隨容量調整 | **接不上**（Gateway 是 api-key 形狀）；且不能用本人登入 | 不建議納入 | §3.5 |
| 6 | Groq 免費方案 | `gpt-oss-120b`／`qwen3.8-27b`：**30 RPM／1000 RPD／8K TPM／200K TPD**（官方表） | **organization** | 預設**不保留**推論資料（官方）；**訓練政策頁面沒寫（查不到）**；可靠性／濫用調查最多留 30 天；可開 ZDR | **接不上**（Gateway 無 groq）；opencode 官方有 Groq 教學 | 短任務、審查；**不要當成可以碰真資料的去處**（訓練政策查不到） | §3.6 |
| 7 | Cloudflare Workers AI 免費額度 | **10,000 neurons／天**，**00:00 UTC** 重置 | Cloudflare 帳號 | 明文 **Zero data retention**（有但書：你自己指定的儲存與 stateful 功能不在此限）、**不訓練** | **接不上**（Gateway 無）；opencode 是否內建該 provider**未查** | 短任務、審查、長任務的便宜後備；**可碰真資料**（明文 ZDR＋不訓練，但書見 §3.7） | §3.7 |
| 8 | Cerebras | 免費試用 **$5 credits、30 天**；**無永久免費** | 組織 | — | 不適用（用完就停） | 不納入 | §3.8 |
| 9 | GitHub Models | **2026-07-30 已退役** | — | — | 不適用 | 不納入 | §3.9 |
| 10 | Hugging Face Inference Providers | 免費帳號每月額度 **None** | 帳號 | — | 不適用（要先買 credits 或 PRO） | 不納入 | §3.10 |
| 11 | Mistral（API） | **查不到**免費 API 層；Free 是 Vibe／Studio 產品方案 | — | — | 不適用 | 不納入 | §3.11 |

**不列入「免費」但仍是鏈上後備**：`ollama-cloud`（付費）與 `claude-personal`（訂閱）。本文件不重查它們的額度，也不列價格（那屬本人訂閱方案，沒有公開出處）。

---

## 3. 逐來源

### 3.1 opencode-zen 的 `*-free`

**免費模型清單（官方，2026-10-26 抓 `https://opencode.ai/docs/zen`）**：`big-pickle`、`jev-1.13-free`、`glm-5.3-flash-free`、`deepseek-v4.1-flash-free`、`beam-free`、`flint-40b-free`、`mimo-v2.6-flash-free`、`nemotron-3-ultra-free`、`nemotron-3.5-lightning-free`、`nemotron-3.5-super-vl-free`。

同頁 Pricing 表把這些列的 input／output 都寫 `Free`。端點是 `https://opencode.ai/inference/openai/v1/chat/completions`（同一頁的 Endpoints 表；`jev-1.13-free` 走 `inference/systemone/v1/systemone`）。

**上限與重置**

- **官方不公布數字**。維護者（Member）2026-10-21，issue #55816：

  > Free limits change with capacity on purpose, so we don't publish a fixed number.
  > … Your current usage and reset times are in the console, and how the limits work is described at https://opencode.ai/docs/go.

  出處：<https://github.com/anomalyco/opencode/issues/55816>
- **重置時間是觀測，不是文件**。issue #58900 的 429 帶 `Retry-After: 65581 seconds`，指向 `2026-10-26 00:00 UTC`。

  出處：<https://github.com/anomalyco/opencode/issues/58900>
  → 本文件所有「00:00 UTC」都標成觀測；不要把它寫成政策檔裡的事實。

**按什麼計**

- 官方 bot：「The OpenCode Console (Zen) service sets and enforces this limit for **your account, workspace and network**.」（issue #58205）

  出處：<https://github.com/anomalyco/opencode/issues/58205>
- 維護者（Member）2026-10-20：「their rate limits apply **per IP address**. Signing in or adding credits doesn't raise them」（issue #43786）

  出處：<https://github.com/anomalyco/opencode/issues/43786>
- **跨免費模型共用一個桶**（觀測）：「all free models fail at the same time once it is exhausted」（#57540）；#58751、#58900 同向。

  出處：<https://github.com/anomalyco/opencode/issues/57540>、<https://github.com/anomalyco/opencode/issues/58751>
- 推論：所以在同一台機器、同一個出口 IP 上多開 agent **不會**變出更多額度，只會更快用完。這與今天「三個 agent 接力、約兩小時用完」一致。

**條款限制**

- **只准在 OpenCode 裡用**（官方，`https://opencode.ai/docs/zen`）：

  > Free-tier chat models require OpenCode; use paid models or [Go](/console/go) from other clients.

  資格不符是 **403**（`Generation.FreeTierError`），額度用完是 **429**（`FreeUsageLimitError`）；兩者訊息都提到「in OpenCode」，光看字看不出來，要看 HTTP status。
  出處：<https://github.com/anomalyco/opencode/blob/dev/services/www/src/docs/content/console/api/inference.mdx#L20-L23>、issue #50366、#58205
- **訓練：逐模型不同**（官方，`https://opencode.ai/docs/zen` 的「Free models」段，也就是免費模型清單；同頁「Privacy」段沒有 glm／deepseek 的條目，只重述了 Beam／NVIDIA 那幾個）：
  - `glm-5.3-flash-free`：「Its provider follows a zero-retention policy and does not use your data for model training.」
  - `deepseek-v4.1-flash-free`：同上句
  - `beam-free`：「Its provider does not use your data for model training and retains it for 30 days for safety reasons.」＋一長串不提供的國家（含中國、香港、澳門）
  - `big-pickle`／`flint-40b-free`／`mimo-v2.6-flash-free`：「During its free period, collected data may be used to improve the model.」
  - `nemotron-3-ultra-free`／`nemotron-3.5-lightning-free`／`nemotron-3.5-super-vl-free`（「Privacy」段，NVIDIA 免費端點）：「**Trial use only — do not submit personal or confidential data.**」
- 未付費帳號的內容使用（ToS「Your IP」）：

  > In addition, if you are using the Services through an unpaid account, we may use Content to further develop and improve our Services.

  出處：<https://opencode.ai/legal/terms-of-service>（「Your IP」段）

**我們接不接得上**：接得上。`policy/default.json:9-13` 的 `opencode-zen` 是 `agents: ["opencode"]`、`models.allow: ["*-free","big-pickle"]`；`lgwd/policy.py:95` 用 `fnmatch.fnmatchcase` 比對，所以 `*-free` 會涵蓋上面全部免費 id（含未來新增的），`big-pickle` 必須另外列（它不以 `-free` 結尾）。sidecar 端就是 agent `opencode`，沒有額外條件。

**適合的工作**：短任務、審查、跑測試與讀檔。**不適合**：長任務（桶一用完就整個團隊停擺）、任何有隱私內容的工作（NVIDIA 那三個模型明寫不要送機密資料；另外三個免費模型在免費期會用你的資料改進模型）。

---

### 3.2 OpenCode Go 的限時免費模型

**先講最重要的一點：Go 不是免費來源。** 官方（`https://opencode.ai/docs/go`）：

> | Plan | Price | Included usage |
> | **Go** | **$10/month** | Lower-cost access to the models below |
> | **Go Plus** | **$40/month** | Higher usage limits across the models below |

免費模型是訂閱者的附帶福利，官方在用量限制那段也這樣寫：

> Each model has the following usage limits: 5-hour — 20% of the monthly limit; weekly — 50%; and monthly — 100%.
> …
> <Callout type="tip">If you reach the usage limit, you can continue using the free models.</Callout>

**今天的免費模型只有 Beam Free。** 同頁模型清單：

> - **Beam Free** (limited time, limited regions)

Pricing 表：`Beam Free | Free | Free | Free | - | **Unlimited** limited time`；Requests 表：`Beam Free | Unlimited | Unlimited | Unlimited`。

**與 #78 預設值的落差（要處理）**：`policy/default.json:17` 的 `opencode-go` 預設是 `iceman-contributor-free`（#78 merge 的值），而**今天的官方線頁只剩 `beam-free`**，端點表也只剩 `Beam Free | beam-free`。我沒有打 models 端點（依 BRIEF 不打任何 API 端點），所以：

- 確定：線頁今天沒有 `iceman-contributor-free`。
- 查不到：這個 id 是「已下架」還是「還在、只是文件沒列」。
- **風險**：若這個 id 真的沒了，第一個開 Go 的隊員會拿到模型不存在。
- 2026-10-26 的實測（PM）：用 `iceman-contributor-free` 開隊員**沒有回應**；同一時段換 `beam-free` 開得起來、能回話。這與「線頁沒有它」同向，但一次觀測不能斷定是 id 不存在（也可能是那個上游當下壞了）。

**上限與重置**：免費模型標 `Unlimited`（限時）；付費模型是月額度制，切 5h／週／月三窗（上面引文）。

**按什麼計**：**查不到**。官方頁沒有寫 Go 的免費模型和 Zen 的 `*-free` 是不是同一個桶。能確定的是兩邊都是 OpenCode 的產品、都吃 OpenCode Console 的 API key（#77 的標題就是「共用 Zen key」）。

> 2026-10-26 的單一觀測（PM 實測）：同一時段，Go 的 `beam-free` 能回話，Zen 的 `beam-free` 已經回 429「Free usage limit reached」。**推論**：Go 的免費模型與 Zen 的免費桶看起來**不是**同一個。只有一次觀測，也不知道 Go 那條是不是走另一個上游，所以不當定論；但它足以推翻先前「同一把 key ⇒ 同一個桶」的推論，§4.1 與 §5 因此改成「先當成不確定」。

**條款**

- Beam Free：不訓練、保留 30 天（Go 頁 Privacy 表 `Beam Free | Not used | 30 days`）。
- 區域限制（官方）：

  > **Beam Free:** Free for a limited time. Not available in Angola, Belarus, Cambodia, **China (including Hong Kong and Macau)**, Cuba, … Russia, Syria, …

- Go 的 workspace 不能邀成員：

  > Go and Go Plus are for workspaces with a single member, such as the **Personal** workspace created when you sign up. A workspace with a Go subscription cannot invite members.

- 客戶端要求（官方）：

  > OpenCode Go is designed for OpenCode and other coding agents that produce similar types of requests. Traffic is monitored for abuse that degrades the experience for other users.

  同頁要求客戶端「Identify itself with its own user agent…」並送 `x-opencode-session`。→ 推論：把 Go 包成「多個 agent 共用一把 key 打同一條通道」在文件上是被接受的形狀（它明列 Claude Code 等 client），但**濫用監控是主觀判定**，不要用來做超出 coding agent 的批次工作。

**接不接得上**：接得上但**沒部署**（#78 merge 了 `lgwd/providers.py:122-127` 與 `policy/default.json:14-18`）。前提是本人真的有 Go 訂閱；沒訂閱時這把 key 打 Go 只會失敗。

**適合的工作**：短任務、備援。**不適合**：當主力（限時＋區域限制＋訂閱費）。

---

### 3.3 OpenRouter `:free`

**上限與重置（官方，`https://openrouter.ai/docs/api-reference/limits`）**：

> 1. **Free usage limits**: If you're using a free model variant (with an ID ending in `:free`), the following limits apply:
>
> | Credits purchased (all time) | Requests per minute | Requests per day |
> | Less than 10 | 20 | 50 |
> | At least 10 | 20 | 1000 |

> The `limit` tier is selected by all-time credits purchased, independently of `is_free_tier`. To absorb rounding and top-up fees, the higher daily ceiling is granted starting one credit below the table's threshold (currently {FREE_MODEL_CREDITS_THRESHOLD - 1} credits)…

→ 官方把「買過 $10 額度」寫成提高每日上限的手段。原文的門檻寫成 `{FREE_MODEL_CREDITS_THRESHOLD - 1}`（未展開的模板變數），**推論**是 9 credits 起；這點我沒有在別的頁面交叉驗證。這是本文兩個付費例外之一（§4）：**不買就照 50 RPD 排**。

重置是 **UTC 日**（官方）：`free_model_daily_requests.used` 註解為「Free-model requests recorded so far in the **current UTC day**」；budgets 段也寫「a daily budget resets at 00:00 UTC」。

**按什麼計**（官方，同頁最上方）：

> Making additional accounts or API keys will not affect your rate limits, as we govern capacity globally. We do however have different rate limits for different models, so you can share the load that way if you do run into issues.

→ **官方直接否掉「多把 key 分攤」這條路**。

另一個會咬人的地方（官方）：帳號餘額為負時，「you may see errors, including for free models」——免費模型也會被餘額擋。

**條款**

- ToS §7.3（官方，`https://openrouter.ai/terms`）：

  > create a false identity, misrepresent your identity, or create multiple accounts as a single user, for purposes of bypassing or circumventing use limits on the Site or Service or for any other reason;

- 訓練：OpenRouter 自己不訓練，但**模型條款各自決定**（ToS §6.1）：

  > Some Models may store or train on your Inputs for improving their own large language models and may allow you to opt-out of model training… Where possible, OpenRouter has opted out of model training with the Models it uses.

  → 推論：`openrouter/free` 這種「自動挑上游」的路由，**你無法預測這次是誰在服務**，所以不能承諾「這條不會訓練」。

**接不接得上**：接得上。`policy/default.json:19-23` 的 `openrouter` 是 `agents: ["opencode"]`、`allow: ["*:free","openrouter/free"]`；`lgwd/policy.py:145-146` 明文把這兩條寫死（「openrouter 的 agents 只能剛好是 `["opencode"]`」「models.allow 只能是 `["*:free", "openrouter/free"]`」）。

**適合的工作**：短任務、審查、多模型比較（同一把 key 換很多 `:free` 模型，官方也建議用不同模型分散負載）。**不適合**：長任務（50 RPD 會被一個長 session 吃掉）、任何需要「保證不訓練」的工作。

---

### 3.4 Gemini 免費層（Google AI Studio 的 API key）

**免費層存在，但只有部分模型**（官方 `https://ai.google.dev/gemini-api/docs/pricing`）：

- `gemini-3.8-flash`、`gemini-3.6-flash`、`gemini-3.5-flash-lite`、`gemini-3.1-flash-lite` 等的 Free Tier 欄是 **`Free of charge`**；同表 `Used to improve our products` 在 Free Tier 是 **`Yes`**。
- `gemini-4-argon`：「**Gemini 4 Argon is not available on the Free tier.**」
- `gemini-3.1-pro-preview`：Free Tier 全部 `Not available`。

**上限與重置（官方，`https://ai.google.dev/gemini-api/docs/rate-limits`）**：

> Rate limits are applied **per project, not per API key**. Requests per day (**RPD**) quotas reset at **midnight Pacific time**.

同頁對「數字」只給方向：

> Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio. … Specified rate limits are not guaranteed and actual capacity may vary.

→ **官方不再在文件裡列固定 RPM/RPD**（舊版有，現在指向 AI Studio）。所以「Gemini 免費層每天幾個 request」= **查不到**；能查到的只有「per project」「RPD 午夜太平洋重置」「超額回 429 `RESOURCE_EXHAUSTED`」。

推論（換算，不是原文）：太平洋時間午夜＝夏令約 **07:00 UTC**、冬令約 **08:00 UTC**。這是本文所有來源裡**重置最晚**的一個 → 它適合當「下午／傍晚」的來源，不適合當「00:00 UTC 之後搶先跑」的來源。

**按什麼計**：per project（不是 per key）→ 同一專案下多把 key 不增加額度（官方原文）。

**條款（這是重點）**（官方 `https://ai.google.dev/gemini-api/terms`，「Unpaid Services」段）：

> When you use Unpaid Services, including, for example, Google AI Studio and the unpaid quota on Gemini API, Google uses the content you submit to the Services and any generated responses to provide, improve, and develop Google products and services and machine learning technologies…

> To help with quality and improve our products, human reviewers may read, annotate, and process your API input and output. Google takes steps to protect your privacy as part of this process. This includes disconnecting this data from your Google Account, API key, and Cloud project before reviewers see or annotate it. **Do not submit sensitive, confidential, or personal information to the Unpaid Services.**

有兩條但書要看（同頁）：

> You may use only Paid Services when making API Clients available to users in the European Economic Area, Switzerland, or the United Kingdom.

> If you're in the European Economic Area, Switzerland, or the United Kingdom, the terms under "How Google uses Your Data" in "Paid Services" apply to all Services, including Google AI Studio and unpaid quota in the Gemini API, even though they are offered free of charge.

→ 在 EEA／瑞士／英國，連免費額度也套「付費服務」的資料條款（不拿去改進產品）。這條綁在使用者所在地，不是我們能選的開關，也不該為了省額度去改登記地，所以本文件一律照「免費層會用你的內容」處理。

→ 一句話：**免費層＝你的程式碼與 prompt 會被當成改進產品的素材，而且可能被人讀**（讀之前會與帳號、API key、專案脫鉤，但內容本身仍會被看）。對 public repo 的工作可以接受；對 private repo、客戶資料、憑證、私人文檔一律不行。

**接不接得上**：**接不上**。`lgwd/providers.py:130-137` 的 `PROVIDERS` 沒有 gemini；`lgwd/policy.py:18-19` 的 `SUBSCRIPTIONS` 白名單也沒有。要接就得新增 provider（OpenAI 相容或 google 原生端點）＋key＋政策，見 §5。

另外：`claude-code` 這條路吃的是 `ANTHROPIC_BASE_URL`／`ANTHROPIC_AUTH_TOKEN`（`lgwd/providers.py:74-86`）——**OpenAI 相容的免費來源接不進 claude-code**，只能給 `opencode`（推論自 `build_env` 的形狀）。

**適合的工作**：審查、讀大檔案、長任務（品質與 context 都好）。**不適合**：任何有隱私內容的工作。

---

### 3.5 Antigravity CLI（agy）

**額度（官方 `https://www.antigravity.google/docs/plans/`）**：

> Users not on Google AI Pro or Ultra plans receive the following benefits:
> - A meaningful quota, refreshed weekly
> - A weekly rate limit
>
> … Usage limits for this service are subject to modification.

→ 有免費額度，但**沒有數字**（查不到）。`/usage`（別名 `/quota`）會開 TUI 面板顯示「A breakdown of your usage limits and remaining requests/tokens for each supported model」（`https://www.antigravity.google/docs/cli/commands/usage/`），但**沒有非互動輸出**（`docs/research/quota-signals.md:105` 記過同一件事）。

**為什麼不能用（兩個獨立的理由）**：

1. **官方明說不支援 BYOK**（同 plans 頁）：

   > There is currently no support for the following options:
   > - Bring-your-own-key (BYOK) or bring-your-own-endpoint for additional rate limits

   → 沒有「一把 key 給 Gateway 用」的形狀；額度綁在登入的 Google 帳號上。
2. **我們的紅線是「不用本人的登入」**（§6）。把個人 Google 帳號的登入交給 agent 長期自動跑，正好落在 ToS「不得轉讓帳號」與「無人值守行程」那兩段（§6.1）。

**結論**：不納入建議配置。若要用，只能由本人自己互動使用，不進團隊供給。

---

### 3.6 Groq 免費方案

**上限（官方 `https://console.groq.com/docs/rate-limits`，Free Plan 表）**：

| model id | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `openai/gpt-oss-120b` | 30 | 1K | 8K | 200K |
| `openai/gpt-oss-20b` | 30 | 1K | 8K | 200K |
| `qwen/qwen3.8-27b` | 30 | 1K | 8K | 200K |
| `openai/gpt-oss-safeguard-20b` | 3 | 1K | 2K | 200K |

**按什麼計**（官方）：

> Rate limits apply at the **organization** level, not individual users. You can hit any limit type depending on which threshold you reach first.

**一個可能被低估的細節**（官方）：

> [Cached tokens](/docs/prompt-caching) do not count towards your rate limits.

→ 推論：coding agent 的 prompt 大部分是重複的前文，若 prompt caching 有命中，實際可用量比 200K TPD 的表面值大；但**TPM 8K 很小**，單次長 context 的請求可能一開始就被 TPM 擋住。

**重置**：官方只給 `x-ratelimit-reset-requests`（「Always refers to Requests Per Day (RPD)」）等 header，**沒有寫固定重置時刻**（查不到）。

**條款（官方 `https://console.groq.com/docs/your-data`）**：

> **By default, Groq does not retain customer data for inference requests.**

> We may temporarily log inputs and outputs **only when**: Troubleshooting errors…, or Investigating suspected abuse (e.g. rate-limit circumvention). These logs are retained for up to **30 days**…

> All customers may enable Zero Data Retention (ZDR) in Data Controls settings.

→ 保留條款是免費來源裡寫得最清楚的一個：預設不保留、可開 ZDR、濫用調查最多留 30 天。**但這一頁沒有講會不會拿資料訓練**——整頁沒有「train」字樣，從「沒寫」推不出「不訓練」。所以 Groq **不算**有「不訓練」的明文（這一頁的 ZDR 管的是保留，不是訓練）；要碰真資料前得另外查它的隱私政策，或只在開 ZDR 且自己確認過訓練條款之後使用。

**接不接得上**：**接不上**（Gateway 沒有 groq）。但 opencode 端**官方支援**（Groq 自己的文件 `https://console.groq.com/docs/coding-with-groq/opencode`）：

> Inside the OpenCode TUI, run `/connect`, search for **Groq** and select it, then enter your Groq API key when prompted.

→ 也就是只要 Gateway 新增一個 `groq` provider（OpenAI 相容 base `https://api.groq.com/openai/v1`，**推論**，未讀該頁），agent `opencode` 就能用。

**適合的工作**：短任務、審查、單檔修改。**不適合**：需要長 context 的任務（8K TPM 會直接擋）；**也不要當成可以碰真資料的去處**——訓練政策查不到，目前只有 Cloudflare 有逐字的不訓練明文。

---

### 3.7 Cloudflare Workers AI 免費額度

**上限（官方 `https://developers.cloudflare.com/workers-ai/platform/pricing/`）**：

> Our free allocation allows anyone to use a total of **10,000 Neurons per day at no charge**. … All limits reset daily at **00:00 UTC**.

部分模型要付費方案才能用（同頁）：

> Some models require a paid billing method. This applies to `@cf/moonshotai/kimi-k2.6`, `@cf/moonshotai/kimi-k2.7-code`, `@cf/zai-org/glm-5.2`, `@cf/zai-org/glm-5.3`, `@cf/zai-org/glm-5.3-flash`, `@cf/deepseek-ai/deepseek-v4-flash-0731`, `@cf/deepseek-ai/deepseek-v4.1-flash-1017`, and `@cf/deepseek-ai/deepseek-v4-pro-0813`.

→ 免費額度**不能**用在那些 frontier 模型上；能用的例如 `@cf/openai/gpt-oss-120b`（31,818 neurons／M input、68,182／M output）、`@cf/meta/llama-3.3-70b-instruct-fp8-fast`、`@cf/qwen/qwen3.8-27b`、`@cf/zai-org/glm-4.7-flash`。

推論（換算，不是原文）：10,000 neurons ≈ **0.31M input tokens／天**（用 `gpt-oss-120b` 的 input 單價算），或 ≈ **0.15M output tokens／天**。這是「一個短 session」的量級，不是「跑一整天」的量級。

**條款（官方 `https://developers.cloudflare.com/workers-ai/platform/data-usage/`）**：

> **Zero data retention:** Workers AI processes Customer Content only for as long as needed to provide the service. For standard inference requests that do not invoke a stateful feature, Cloudflare does not retain inputs or outputs after they are no longer needed to process the request and deliver the response, subject to the limited exceptions below.
>
> **No training:** Cloudflare does not use Customer Content processed by Workers AI to train AI models.

**ZDR 的但書**（同頁「When a feature stores Customer Content」）：這個承諾不阻止「你自己指定、或 stateful 功能所需的儲存」——

> Our zero data retention commitment does not prevent storage that you direct or that is necessary to provide a stateful feature you select.

例如把 Workers AI 接上 R2／KV／Durable Objects／Vectorize、在 AI Gateway 開 logging 或 caching、或用非同步／batch／上傳檔案那類功能，那些內容會依該功能的保留期留著。→ 實務上：**只送一次性推論請求、不要接儲存或 logging，才是真的 ZDR。**

**不訓練的部分沒有保留期那種但書**（同頁另外一段只說「依法令必須分析或保留」的內容也不會拿去訓練），所以這是本文唯一一家有逐字「不訓練」明文的免費來源。唯一的邊界是：**這個承諾只涵蓋 Workers AI 自己**——若用 AI Gateway 把內容轉給別的 provider，或把 Workers AI 接到第三方服務，就回到那家的條款（同頁第一段）。

**按什麼計**：Cloudflare 帳號（neuron 計費掛在帳號上）；官方沒有寫「per key」這種維度（查不到，但推論是同帳號一個池）。

**接不接得上**：**接不上**（Gateway 沒有 cloudflare-ai）。要接需要 Cloudflare 的 account id ＋ API token（**推論**：REST 路徑含 account id；我沒有讀 REST 認證文件，標查不到）。opencode 是否有內建該 provider：**未查**。

**適合的工作**：短任務、審查、當其他來源用完後的後備；**可以碰真資料**（明文 ZDR＋不訓練）——這是它最大的價值。

---

### 3.8 Cerebras：沒有永久免費

官方 `https://inference-docs.cerebras.ai/support/rate-limits` 的 FAQ：

> **Is there a permanently free tier?** No. The Free Trial is time- and credit-bounded: **$5 in credits that expire 30 days** after they're granted. Once your credits are used up or expire, you need to purchase credits (Pay as You Go)…

Free Trial 的限流仍有公布（例如 `gpt-oss-120b`：5 RPM／30K uncached TPM／90K total TPM／1M TPH／1M TPD），但那是「30 天試用」的限流。→ **不納入**（一次性，不能當穩定供給）。

---

### 3.9 GitHub Models：已退役

官方 `https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models`：

> GitHub Models has been retired.
> As of **July 30, 2026**, GitHub Models has been fully retired. The playground, model catalog, inference API, and bring your own key (BYOK) are no longer available to any customer.

→ **不納入**。任何舊筆記提到「GitHub Models 有免費額度」都是過期的。

---

### 3.10 Hugging Face Inference Providers：免費帳號沒有額度

官方 `https://huggingface.co/docs/inference-providers/pricing`：

> | Account Type | Monthly Credits | Can be spent on | Extra usage (pay-as-you-go) |
> | Free Users | **None** | - | yes (credits purchase required) |
> | PRO Users | $2.00 | All Hugging Face compute services | yes |

→ **不納入**（免費帳號要自己買 credits）。

---

### 3.11 Mistral：API 免費層查不到

`https://mistral.ai/pricing` 的 Free 方案是**產品**方案（Vibe／Studio 的有限使用），FAQ：

> Yes, Mistral's Free plan provides limited access to [Vibe](https://mistral.ai/products/vibe)… On the Free plan, you also get access to [Mistral Studio](https://mistral.ai/products/studio/).

同一頁的 `Model training` 列在 Free 是空的、Pro／Team／Enterprise 才是 `Opt-out`。**API 本身有沒有免費配額：查不到**（我讀到的頁面沒有任何 API 免費層的數字或條款）。→ **不納入**，除非本人另外查到官方的 API 免費層文件。

---

## 4. 建議配置

前提：

- **不為了分攤額度開重複帳號；每家來源最多一個帳號，由本人註冊。** 這不是「不開帳號」——要接 Groq／Cloudflare／Gemini 就得各開一個；不開的是同一家的第二個（§6.2）。
- **不用本人的登入**，一律用 API key（§6.1）。
- **付費只有兩個例外**，都是既有方案、不是為了本文新買的：
  - **OpenCode Go 每月 $10**（官方 `https://opencode.ai/docs/go`）：沒訂閱就沒有 Go 的免費模型。
  - **OpenRouter 終身買到 10 credits 以上才有 1000 RPD**（官方 `https://openrouter.ai/docs/api-reference/limits` 的限流表）；不買就是 50 RPD，照 50 排。
- 其餘來源一律不付費。

在這個前提下能動的只有「順序、分配、探測方式、排程」。

### 4.1 Gateway 的 priority

現值（`policy/default.json:3-29`）：`opencode-zen` 10、`opencode-go` 15、`openrouter` 20、`ollama-cloud` 30、`claude-personal` 40。

建議：

| priority | subscription | 為什麼是這個位置 |
|---|---|---|
| 10 | `opencode-zen` | 目前**接得上**的來源裡，品質夠好又不用另外付費的一把；但它是一個共用小桶，放第一是為了**搶在它用完之前**把短任務做完 |
| 15 | `opencode-go` | 位置不變。**只有本人有 Go 訂閱時才有意義**；與 zen 共用同一種 Console key（#77 的標題「共用 Zen key」），但**是不是同一個桶查不到**——2026-10-26 的一次觀測指向不同桶（§3.2）。所以在有第二個觀測之前，把它當「可能多一層」而不是「確定多一層」 |
| 20 | `openrouter` | 獨立額度、有公布數字；現在受 `*:free` 限制（`lgwd/policy.py:145-146`），50 RPD 當保險絲用 |
| 25 | `groq`（**要新增**） | 獨立額度、公布數字、保留條款清楚（但訓練政策查不到，不碰真資料）；RPD 1000 比 OpenRouter 的 50 大一個量級 |
| 28 | `cloudflare-ai`（**要新增**） | 每天 00:00 UTC 重置、ZDR、不訓練；額度小但可當「zen 用完後還能跑」的一層 |
| 29 | `gemini`（**要新增**） | 品質最好、重置最晚（07:00／08:00 UTC）；放這裡是**故意讓它在 zen 之後**，因為它的額度是「per project」而且免費層會拿你的內容去改進產品 |
| 30 | `ollama-cloud` | 付費後備，不動 |
| 40 | `claude-personal` | 訂閱後備，不動 |

三點提醒：

1. **priority 只決定「先問誰」，不決定「誰有額度」。** 免費桶一用完，Gateway 照樣給你 key（#76 之後冷卻只是提示，`lgwd/registry.py:142-143`），429 由 agent 吃。所以 priority 的實際作用只有「別讓好的來源被小事吃掉」。
2. **zen／go 不要當成兩倍的量。** 兩邊共用 OpenCode 的 Console key，但同不同桶查不到（§3.2：一次觀測指向不同桶）。保守做法是「先用 zen，zen 429 之後再試一次 go」，而不是兩把並排輪流打。要更保守就先把 `opencode-go` 移出鏈（政策檔改動，d 級）；本文的單只處理它的預設 id（§5 第 1 條）。
3. **新加的三個（groq／cloudflare-ai／gemini）都只能給 agent `opencode`。** `claude-code` 吃的是 `ANTHROPIC_BASE_URL`（`lgwd/providers.py:74-86`），OpenAI 相容端點接不進去（推論）。

### 4.2 sidecar 的輪替順序

sidecar 選把是 `PrioritySelector`：`available` 先篩、再 `min(priority, id)`（`selectors.py:20-36`）；換把時走 `resolve` 並帶 `exclude=<剛用完的那把>`（`swapping.py:255-262`），`resolve` 本身不看冷卻（`lgwd/registry.py:142-143`）。

建議的「用哪個、什麼時候用」：

1. **剛過 00:00 UTC**：全部短任務丟 zen。這是唯一「重置早、模型好」的時段。
2. **zen 的桶用完之後**：切 OpenRouter（`:free`）與 Groq。兩者都是「per 日」桶，但 OpenRouter 的 50 RPD 只夠一個 session，Groq 的 1000 RPD 才是這一段的主力。
3. **Cloudflare**：任何時候都可以當「還有一個便宜的後備」，反正它每天 00:00 UTC 自己回滿。
4. **Gemini**：放在下午／傍晚（它的日配額在 07:00／08:00 UTC 才重置，早上那段是「前一天的殘量」）。
5. **長任務**：優先排「重置早 ＋ 額度相對大」的那一格——也就是 zen 剛重置時，或直接放 Cloudflare／Gemini（品質換穩定）。**不要**把長任務排在 zen 已經用掉大半的時候：它一撞牆就是整個團隊停擺兩個小時（今天的實況）。
6. **同時開幾個 agent**：zen 的桶按帳號／網路計（§3.1），**同時開 N 個不會變快，只會把桶分成 N 份**。建議「同一時間只有一個 zen 隊員在做會吃 context 的事」，其他隊員排隊或走 OpenRouter／Groq。

### 4.3 每個來源分配給哪類工作

| 工作 | 首選 | 次選 | 理由 |
|---|---|---|---|
| 實作（改多檔、跑測試） | zen（重置後） | Groq 短回合 / Cloudflare | 要 tool calling 與多次來回；zen 的模型最像「coding agent 用」 |
| 審查（讀 diff、寫意見） | Gemini（品質） | Groq / Cloudflare | 審查要「讀得多、寫得少」，Gemini 的 context 與品質最好；而且審查多數在 public repo 上做，不需要碰真資料 |
| 長任務（整份文件、大重構） | Gemini 或 Cloudflare | zen（只有在剛重置時） | 長任務最怕中途撞牆；Gemini／Cloudflare 的桶重置固定、不會兩小時就沒 |
| 短任務（單檔、跑一次測試、翻譯） | zen | OpenRouter `:free` / Groq | 用最小的成本吃掉 |
| 需要隱私（private repo、真資料） | **Cloudflare**（唯一有「不訓練」明文） | 付費來源 | 只有 Cloudflare 的頁面逐字寫明不訓練（ZDR 的但書見 §3.7）；Groq 只寫了「預設不保留」，訓練政策查不到；zen 的部分免費模型與 Gemini 免費層都會拿內容去改進產品 |

**紅線提醒**：這份 repo 是 public，工作內容本身沒有隱私問題；但隊員也會處理 PMO 的 private repo 與本人的私人資料——那些工作**不要**落在「會訓練」的模型上（§6.4）。

### 4.4 避免浪費

#### 4.4.1 探測不吃額度

- **可以拿來探測的（官方，非推論端點）**：
  - OpenRouter：`GET /api/v1/key` 回 `free_model_daily_requests.{used,limit,remaining}` 與 `limit_remaining`（`https://openrouter.ai/docs/api-reference/limits`）→ 這**不消耗**免費請求數，是唯一「免費且官方」的剩餘量查詢。
  - Ollama Cloud：`GET /api/balance`（legacy 方案回 `remaining_percent`／`resets_at`），`docs/research/quota-signals.md:117` 已記。
  - Claude：statusline 的 `rate_limits.five_hour/seven_day`（`docs/research/quota-signals.md:48`）。
- **不能拿來探測的**：
  - **zen／go 沒有公開的額度查詢 API**（`docs/research/quota-signals.md:121-125`：Zen 餘額／用量查詢「截至 2026-10-11 查無」公開文件；`/zen/go/v1/usage` 只出現在 sub2api 這種第三方程式碼裡）。→ **不要用「送一個小請求看看通不通」來探測**，那會真的吃桶。
  - Gemini：官方文件沒有給「查剩餘 RPD」的 API（查不到）；超額就是 429 `RESOURCE_EXHAUSTED`。
- **repo 現況的缺口**：`lgwd/providers.py:91,110` 只有 `claude-personal` 與 `ollama-cloud` 有 usage fetcher；`openrouter`、`opencode-zen`、`opencode-go` 一律 `not_applicable`（`lgwd/usage.py:30`、`:161`）。**OpenRouter 的 `GET /api/v1/key` 是現成的、免費的、官方支援的，卻沒有接**——這是一張 `d`，見 §5 第 4 條。

#### 4.4.2 一個模型失敗不連帶冷卻別的來源

**#76 已經做完的（Gateway 側）**：

- 冷卻可以只針對一個模型：`POST /v1/subscriptions/{id}/cooldown` 的 body 多了選填 `model`（`openspec/changes/model-cooldown/proposal.md`「登錄冷卻的 body 多一個選填 `model`」）。
- 冷卻不再擋人：`resolve` 不再因冷卻回 409，冷卻也不參與排序（`lgwd/registry.py:142-143`：「冷卻只是提示：不參與候選，也不參與排序」）。
- 回應裡看得到：`meta.cooldown` 與 `meta.model_cooldowns`（`lgwd/registry.py:186`、`:411`、`:423`），底層是新表 `model_cooldowns(subscription, model, until)`（`lgwd/store.py:27`）。

**還缺的（sidecar 側）**——#76 之後冷卻只是提示、resolve 不擋人，所以這三項的影響只剩「提示不精確」與「sidecar 沒辦法據此換模型」，不會再擋住開隊員：

1. **sidecar 登錄冷卻時沒有帶 `model`**。`sources.py:429-431` 送的是 `POST /v1/subscriptions/{id}/cooldown {"until": ...}`，沒有 `model` 欄位；`swapping.py:215` 呼叫 `source.cooldown(rec.subscription, until)` 也沒有模型可傳。→ 結果：一個免費模型 429，`list`／`meta` 會顯示**整把**在冷卻，提示失真；resolve 仍會照 priority 把這把給出來。
2. **fallback 是固定的 1 小時**。`core.py:139` `QUOTA_FALLBACK_SECONDS = 3600.0`；`_cooldown_until`（`core.py:1789-1808`）在 `resets_at` 不可用時就直接給 `now + 1h`。→ 這個值對免費模型可能太長（10 分鐘後就有別的模型可用），也可能太短（桶要到 00:00 UTC 才回）；因為現在只是提示，代價是**提示寫錯**，不是擋人。
3. **開隊員前沒有讀 `model_cooldowns`**。sidecar 的 `resolve` 路徑完全不看冷卻（`swapping.py:255-262`），所以它會一直選「priority 最小」的那把，即使那把的所有模型都在冷卻。→ 缺的是「先讀 `list`／`meta.model_cooldowns`、挑同把裡沒冷卻的模型」。

建議（給 MyLinuxPool 的單，見 §5）：429 時送 `model`；免費模型的 fallback 從 600 秒起、每次加倍、上限 3600；開隊員前用 `model_cooldowns` 換模型而不是換把。

#### 4.4.3 配合重置時刻排工作

各來源的重置時刻（可用的都列出來，換算標推論）：

| 來源 | 重置 | 依據 |
|---|---|---|
| opencode-zen | **00:00 UTC** | 觀測（#58900 的 `Retry-After` 指向 00:00 UTC）；官方不公布 |
| OpenRouter `:free` | **00:00 UTC** | 官方（`free_model_daily_requests` 是 UTC 日；budget 也是 UTC） |
| Cloudflare Workers AI | **00:00 UTC** | 官方（"All limits reset daily at 00:00 UTC"） |
| Groq | 不確定（只有 `x-ratelimit-reset-requests` header） | 查不到 |
| Gemini 免費層 | **太平洋時間午夜**（≈ 07:00／08:00 UTC） | 官方；換算為推論 |

實務建議：

- **00:00 UTC 之後的兩小時是黃金時段**：把「今天非做完不可」的實作排在這段，其他時間只做短任務。
- **不要把長任務跨過 00:00 UTC**：它會在重置那一刻被 429 打斷，而 agent 的重試行為是退避＋上限 5 次（`docs/research/quota-signals.md:63`），不會自動等 24 小時。
- **Gemini 的黃金時段是 07:00 UTC 之後**：早上（UTC）跑 Gemini 等於在吃昨天的殘量。

#### 4.4.4 長任務不要放在快用完的桶上

- 判斷「快用完」的**唯一**可靠訊號是官方端點（OpenRouter 的 `free_model_daily_requests.remaining`）；zen／go 沒有（§4.4.1）。
- 沒有訊號時的保守規則（推論，非官方）：**同一把 zen 上「今天已經出現過一次 429」就不要再排長任務**。理由是跨模型共用一桶（§3.1），第一個 429 出現代表桶已經接近底，換模型只是換一條路徑撞同一面牆。
- 長任務放「重置固定、額度相對大」的來源（Cloudflare／Gemini），短任務放 zen 的爆量段。

---

## 5. 要改／要開的單

只寫在這裡，**沒有開任何 issue**。層級照工單規則（c＝好幾步、d＝一個 PR 做得完）。

### LLMGateway（本 repo）

| # | 層級 | 一句話範圍 |
|---|---|---|
| 1 | d | `policy/default.json` 的 `opencode-go` 預設 `iceman-contributor-free` 在今天官方線頁已不存在（§3.2），改成 `beam-free`（並在政策註解記「免費模型只在有 Go 訂閱時存在、Beam Free 有區域限制」）。**與 #77 的關係**：#77 是「登錄 Go（共用 Zen key）＋驗 Zen 端點」的來源單、#78 是它的實作（已 merge、未部署）；這張只是改預設 id，若 #77 還沒部署，可以併到部署前的那一步一起做，或先改值避免部署當天踩到不存在的 id |
| 2 | d | 在 `lgwd/policy.py` 與政策檔註解寫明「`*-free` 同時涵蓋 zen 與 go 的 id」這個 side effect（`policy/default.json:9-18`：zen 的 `models.allow` 是 `["*-free","big-pickle"]`、go 是 `["*-free"]`，`lgwd/policy.py:95` 用 `fnmatch` 比對）：想分開控制就得寫死清單，避免下一手以為兩把可以各自調 |
| 3 | c | 新增 `groq` 訂閱：`providers.py` 的 provider（OpenAI 相容）、`policy.py` 的 `SUBSCRIPTIONS` 白名單、`keys.py`、`policy/default.json`（priority 25、`agents: ["opencode"]`）；**不含** usage fetcher（Groq 沒有公布剩餘配額端點） |
| 4 | d | 新增 OpenRouter 的 usage fetcher：`GET /api/v1/key` 讀 `free_model_daily_requests` 與 `limit_remaining`，讓 `list`／`resolve` 的 `usage` 看得到剩餘（探測不吃額度，§4.4.1） |
| 5 | c | 新增 `cloudflare-ai` 訂閱（account id ＋ API token 形狀的 key、OpenAI 相容端點、priority 28、`agents: ["opencode"]`） |
| 6 | c | 新增 `gemini` 訂閱（AI Studio key、priority 29、`agents: ["opencode"]`），**政策註解寫明免費層會拿內容改進產品、不得處理機密資料**（§3.4） |
| 7 | d | 文件面：`docs/model-rotation.md` 的「計入週用量節流」欄位對免費來源要標「不計入」，並把 §3 的來源清單接進去（目前該表還是空的）。**這張與 #58（模型輪換表搬進 LLMGateway、每天自動調查 openrouter／opencode 免費模型）、#63（openspec model-rotation-survey）範圍重疊，建議併進 #58／#63，不要另開** |

### MyLinuxPool（sidecar，另一個 repo）

| # | 層級 | 一句話範圍 |
|---|---|---|
| 8 | c | 登錄冷卻時帶 `model`（`sources.py:420-440` 的 body 多一個欄位），並在 429 的處理路徑把當下的模型傳進去（`swapping.py:215`）；免費模型的 fallback 從 600 秒起、加倍、上限 3600（`core.py:139`） |
| 9 | c | 開隊員前讀 `list`／`resolve.meta.model_cooldowns`，同把有沒冷卻的模型就換模型而不是換把 |
| 10 | d | 併發節流：同一 provider 同時最多 N 個 agent（zen 的桶按帳號／網路計，§3.1），避免多開只加速把桶打乾 |

### 需要本人動作（不是單）

| # | 一句話範圍 |
|---|---|
| A | 實測「zen 與 go 是不是同一個免費桶」：同一時段、同一把 key，依序打 zen 的 `*-free` 與 go 的 `beam-free`，看第二個是否立刻 429。要帶 key，依 BRIEF 不由我做；2026-10-26 已有一次反方向的觀測（§3.2），但只有一次，還不足以定論 |
| B | 若 Groq／Cloudflare／Gemini 這三張要成立，需要本人提供對應的 key（Cloudflare 還要 account id）。**不要把本人任何登入給 agent**（§6） |

---

## 6. 條款紅線

這一節只列「不能做」與原文。不做法律判斷。

### 6.1 不用本人的登入

opencode ToS（`https://opencode.ai/legal/terms-of-service`）：

> You may not assign, delegate or transfer these Terms or your rights or obligations hereunder, **or your Services account**, in any way (by operation of law or otherwise) without OpenCode's prior written consent.

同份 ToS 的 restrictions (j)（最接近「無人值守自動化」的一條）：

> j runs Maillist, Listserv, any form of auto-responder or "spam" on the Services, or **any processes that run or are activated while you are not logged into the Services**, or that otherwise interfere with the proper working of the Services (including by placing an unreasonable load on the Services' infrastructure)

→ (j) 的字面有疑義：**用 API key 跑的 agent 也一樣是「你沒有登入時在跑的行程」**，所以不能只靠 (j) 區分「用 API key」與「用登入」。本文件的判準改成以**憑證種類**為準：

- **推論**（不是條文原話）：API key 是官方發給程式呼叫用的憑證，把它放進 Gateway 是本文件推薦的用法；OAuth 登入憑證則綁在個人帳號上，交給 Gateway 當自動化身分，會同時踩到上面「不得轉讓 **your Services account**」那段的字面。(j) 針對的是「冒用登入狀態去跑無人值守的東西」，不是「用 key 呼叫 API」。
- 結論不變：**一律用 API key，不用本人的登入**。
- **推論**：技術上做得到（把 token 當 key 放進 Gateway 的 key 檔就行），但**不做**；怎麼做的細節本文不寫，也不驗。

### 6.2 不開重複帳號

**前提是「不為了分攤額度開重複帳號」，不是「一個帳號都不能開」**：每家在本文的建議裡最多一個帳號、由本人註冊；Groq／Cloudflare／Gemini 各一個是「不同家」，不是繞限制。opencode ToS restrictions (d)：

> d creates, maintains, or uses accounts in bulk, or creates, maintains, or uses multiple accounts to circumvent usage limits, access restrictions, billing obligations, promotions, suspensions, or any other restriction or policy applicable to the Services

OpenRouter ToS §7.3：

> create a false identity, misrepresent your identity, or create multiple accounts as a single user, for purposes of bypassing or circumventing use limits on the Site or Service or for any other reason;

### 6.3 不用多把 key 分攤

- OpenRouter 官方直接說無效（`https://openrouter.ai/docs/api-reference/limits`）：

  > Making additional accounts or API keys will not affect your rate limits, as we govern capacity globally.

- opencode 的免費桶按**帳號／workspace／網路**（官方 bot，issue #58205）＋**per IP**（維護者，issue #43786）計。推論：同一台機器、同一個出口 IP 上換幾把 key 都是同一個桶，換 key 不只是違規風險，而是**技術上沒用**。

### 6.4 免費層不等於可以碰真資料

- Google（`https://ai.google.dev/gemini-api/terms`，Unpaid Services）：

  > **Do not submit sensitive, confidential, or personal information to the Unpaid Services.**

- opencode 的 NVIDIA 免費模型（`https://opencode.ai/docs/zen`）：

  > Trial use only — **do not submit personal or confidential data.**

- opencode 的 `big-pickle`／`flint-40b-free`／`mimo-v2.6-flash-free`：

  > During its free period, collected data may be used to improve the model.

- opencode ToS：

  > if you are using the Services through an unpaid account, we may use Content to further develop and improve our Services.

→ **操作規則**：免費來源只用在 **public repo、公開資料、自己寫的測試資料**。private repo、客戶資料、憑證、私人文檔一律走**有明文「不訓練」的來源**（目前只有 Cloudflare Workers AI，加上付費來源）。Groq 只有「預設不保留」，訓練政策頁沒寫（查不到），**不算**可以碰真資料。

這條與 **#4（「隱私界線：哪些內容能送公開／免費模型」）** 是同一件事：本文只給技術面的事實，界線要由 #4 那張單定。

### 6.5 一句總結

**可以做**：多來源並用、按重置時刻排程、把冷卻做得更細、用官方的免費查詢端點探測。
**不能做**：用本人的登入、為了分攤額度開重複帳號（每家一個是上限）、多把 key 分攤、把機密內容送進會訓練的免費層。

---

## 7. 沒查到／沒驗到的

1. **zen／go 的免費桶大小、單位、窗口**：官方明說不公布（§3.1）。本文件的「00:00 UTC」是觀測。
2. **zen 與 go 是不是同一個桶**：查不到。先前只有「同一種 key」＋「跨模型共用」兩條推論；2026-10-26 的一次觀測（Go 的 `beam-free` 回話、同時段 Zen 的 `beam-free` 429）反而指向不同桶，但只有一次，也不知道 Go 那條是不是走另一個上游。
3. **`iceman-contributor-free` 還在不在**：今天的官方線頁沒有它；我沒有打 models 端點（依 BRIEF 不打任何 API 端點），所以「已下架」與「文件漏列」無法區分。
4. **Gemini 免費層的實際 RPM／RPD**：官方頁已改成「去 AI Studio 看」，數字查不到 → 無法排「一天能跑幾個」。
5. **Groq 的重置時刻**：官方只給 header，沒有固定時刻（查不到）。
6. **Cloudflare Workers AI 的 REST 認證形狀**（account id ＋ token 怎麼放進 Gateway 的 key 檔）：未讀該頁，標查不到／推論。
7. **opencode 是否內建 Cloudflare Workers AI 的 provider**：未查（models.dev 的目錄沒有打）。
8. **MLP#460（agy driver）與 #462**：本機查不到這兩個 issue 的內容，**未讀**。
9. **沒有實測任何端點**（依 BRIEF），也沒有跑 repo 的測試（這張單只改文件）。所有「接不接得上」都是讀程式碼（`lgwd/providers.py`、`lgwd/policy.py`、`lgwd/registry.py`）的結論，不是跑出來的。
10. **sidecar 的引用只到 `modules/sidecar/src/sidecar/` 的現況**（`core.py:139`、`swapping.py:215`、`sources.py:420-440`、`selectors.py:20-36`）；我沒有跑它的測試，也沒有確認這些行號在最新分支上是否已變動。

---

## 8. 出處清單

### 官方文件

| 主題 | 出處 |
|---|---|
| Zen 免費模型清單、端點、逐模型隱私／訓練、區域限制 | <https://opencode.ai/docs/zen> |
| Zen 免費層只給 OpenCode、其他 client 403（含帶 key） | `dev` `services/www/src/docs/content/console/api/inference.mdx:20-23`、<https://github.com/anomalyco/opencode/blob/dev/services/www/src/docs/content/console/api/inference.mdx#L20-L23> |
| Go 的方案、免費模型、5h／週／月三窗、Privacy 表、客戶端要求、區域限制、workspace 不能邀成員 | <https://opencode.ai/docs/go> |
| Go 的免費模型在撞到上限後仍可用 | 同上（`<Callout type="tip">If you reach the usage limit, you can continue using the free models.</Callout>`） |
| opencode ToS：不得轉讓帳號、restrictions (d)(j)、未付費帳號的內容使用 | <https://opencode.ai/legal/terms-of-service> |
| OpenRouter 免費模型上限（20 RPM；50／1000 RPD）、UTC 日、多帳號無效、`GET /api/v1/key` 的 `free_model_daily_requests`、負餘額也會擋免費模型 | <https://openrouter.ai/docs/api-reference/limits> |
| OpenRouter ToS：§6.1 訓練、§7.3 多帳號繞限制 | <https://openrouter.ai/terms> |
| Gemini 免費層有哪些模型、`Used to improve our products: Yes`、Argon 不在免費層 | <https://ai.google.dev/gemini-api/docs/pricing> |
| Gemini rate limits：per project、RPD 午夜太平洋重置、數字看 AI Studio | <https://ai.google.dev/gemini-api/docs/rate-limits> |
| Gemini 未付費服務的資料使用、人類審閱（讀前與帳號脫鉤）、不要送敏感資料、EEA／CH／UK 適用付費條款 | <https://ai.google.dev/gemini-api/terms> |
| Antigravity 免費帳號的額度形狀、不支援 BYOK | <https://www.antigravity.google/docs/plans/> |
| Antigravity `/usage` 面板（只有 TUI，沒有非互動輸出） | <https://www.antigravity.google/docs/cli/commands/usage/> |
| Groq 免費方案 RPM／RPD／TPM／TPD、organization 級、cached token 不計、header | <https://console.groq.com/docs/rate-limits> |
| Groq 資料保留與 ZDR（**該頁沒有訓練政策**，所以「不訓練」查不到） | <https://console.groq.com/docs/your-data> |
| Groq 官方支援 OpenCode | <https://console.groq.com/docs/coding-with-groq/opencode> |
| Cloudflare Workers AI 免費 10,000 neurons／天、00:00 UTC 重置、部分模型要付費 | <https://developers.cloudflare.com/workers-ai/platform/pricing/> |
| Cloudflare Workers AI 的 ZDR（含「你指定的儲存／stateful 功能」但書）與不訓練 | <https://developers.cloudflare.com/workers-ai/platform/data-usage/> |
| Cerebras 無永久免費（$5／30 天） | <https://inference-docs.cerebras.ai/support/rate-limits> |
| GitHub Models 2026-07-30 退役 | <https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models> |
| Hugging Face 免費帳號額度 None | <https://huggingface.co/docs/inference-providers/pricing> |
| Mistral Free 是產品方案、API 免費層查不到 | <https://mistral.ai/pricing> |

### 觀測（官方沒有明文，用 issue 或既有研究）

| 主題 | 出處 |
|---|---|
| 免費額度按帳號／workspace／網路 | issue #58205、#58186 |
| 免費額度另按 IP（維護者） | issue #43786 |
| 數字不公布、隨容量變（維護者） | issue #55816 |
| 重置指向 00:00 UTC | issue #58900 |
| 跨免費模型共用一個桶 | issue #57540、#58751、#58900 |
| 403 `FreeTierError` 與 429 `FreeUsageLimitError` 的實例 | issue #50366、#58205、#58919 |
| agy 額度用完的字串（`Individual quota reached… Resets in …`） | `docs/research/quota-signals.md:95-99` |
| opencode 的 429 重試行為（退避、上限 5 次） | `docs/research/quota-signals.md:63` |
| Zen／Go 沒有公開額度查詢 API | `docs/research/quota-signals.md:121-125` |
| Go 的 `beam-free` 能回話、同時段 Zen 的 `beam-free` 已 429；Go 的 `iceman-contributor-free` 開隊員沒回應 | 2026-10-26 本團隊實測（PM），單一觀測，本文件標推論（§3.2） |

### 本 repo

| 主題 | 位置 |
|---|---|
| 現行 priority 與 allow | `policy/default.json:3-29` |
| `SUBSCRIPTIONS` 白名單、openrouter 的兩條硬限制、`fnmatch` 比對 | `lgwd/policy.py:18-19`、`:145-146`、`:95` |
| provider 對照表與 endpoint | `lgwd/providers.py:115-137` |
| claude-code 的 env 形狀（`ANTHROPIC_BASE_URL`） | `lgwd/providers.py:74-86` |
| usage fetcher 只有 claude／ollama | `lgwd/providers.py:91`、`:110`；`lgwd/usage.py:30`、`:161` |
| resolve 的選把（priority、冷卻不參與）與 `model_cooldowns` | `lgwd/registry.py:142-143`、`:157-160`、`:186`、`:411`、`:423` |
| `model_cooldowns` 表 | `lgwd/store.py:27` |
| #76 的設計（冷卻只是提示、以模型為單位、sidecar 待辦） | `openspec/changes/model-cooldown/proposal.md` |
| sidecar 的固定 fallback 3600 秒與 `_cooldown_until` | `core.py:139`、`core.py:1789-1808` |
| sidecar 選把（priority、id） | `selectors.py:20-36` |
| sidecar 換把帶 `exclude`、不看冷卻 | `swapping.py:255-262` |
| sidecar 送冷卻不帶 `model` | `sources.py:420-440`、`swapping.py:215` |

### 本 repo 的單（範圍與關係）

| 主題 | 位置 |
|---|---|
| Go 的來源單「登錄 subscription opencode-go（共用 Zen key）＋驗 Zen 端點」 | issue #77；實作在 #78（已 merge、未部署） |
| 模型輪換表與每日調查免費模型（本文 §5 第 7 條建議併入） | issue #58、#63 |
| 隱私界線：哪些內容能送公開／免費模型（本文 §6.4 的界線） | issue #4 |
