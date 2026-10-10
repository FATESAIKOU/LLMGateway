# Review notes：`sub2api.md` 與 `agent-endpoints.md` 查證

> 審查者：LLMGateway 團隊 reviewer（工作 session）。日期：2026-10-10。
> 審查對象：`docs/research/sub2api.md`（33,179 bytes，17:36:07）、`docs/research/agent-endpoints.md`（23,431 bytes，17:37:32）；另回報密鑰／私人資訊檢查（含 `docs/research/sidecar-angareion.md`）。
> 方法：只讀公開資料——sub2api 以 tag `v0.2.15` shallow clone 逐檔對照（程式碼 URL 該 tag 已凍結）、GitHub 公開 API；Claude Code 官方文件 5 頁、opencode／Ollama／OpenRouter／Antigravity 官方文件由子代理逐頁核對。未讀任何憑證檔，未寫入任何金鑰或 token 實際值。
> 結論：**沒有重大錯誤**；兩份檔可保留使用。以下問題都是低嚴重度（數量、欄位名精確度、出處註記、措辭耐久性），建議原作者順手修。

## 一、`sub2api.md`

### S1. `sub2api.md` §1.1（技術棧）— migrations 數量
- **問題**：「約 300+ 支 SQL migration」，實際沒有 300+。
- **正確說法**：快照 `v0.2.15` 的 `backend/migrations/` 有 **292 支 `.sql`**（目錄項目共 315 個）。建議改「近 300 支（292）」或「290+ 支」。
- **出處**：<https://github.com/Wei-Shaw/sub2api/tree/v0.2.15/backend/migrations>（本次以 `rg --files backend/migrations -g '*.sql' | wc -l` 點數；全目錄 `ls | wc -l` 為 315）。

### S2. `sub2api.md` §2.6（額度用完時的行為）— reason JSON 欄位名
- **問題**：「（JSON，含 `used_percent`、`threshold`、`until`）」——後兩個欄位名不精確。
- **正確說法**：`temp_unschedulable_reason` 的 JSON 欄位是 `used_percent`、`threshold_percent`、`until_unix`（另有 `source`、`error_message` 等）。
- **出處**：<https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/backend/internal/service/account_scheduling_threshold_reason.go>（`ThresholdPercent`／`UsedPercent`／`UntilUnix` 的 JSON tag）。

### S3. `sub2api.md` §6.2（維護狀況）— release 數字的措辭
- **問題**：「59 個 release（第一頁）」——59 是**總數**，不是第一頁的數量。
- **正確說法**：`releases?per_page=1` 的回應 Link header 的 last 為第 59 頁，即 2026-10-10 當下共 59 個 release。
- **出處**：<https://api.github.com/repos/Wei-Shaw/sub2api/releases>（本次實測 `Link: ...page=59>; rel="last"`）。

### S4.（建議，非錯誤）`sub2api.md` §3.3 — `multi_select` 的型別清單
- **問題**：清單正確（`text／textarea／number／email／url／date／select／multi_select` 全數存在），但出處只給了 schema 檔；型別列舉實際定義在前端型別檔。
- **建議**：可補 <https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/types/index.ts>（`UserAttributeType`）。
- **出處**：同上；另 <https://github.com/Wei-Shaw/sub2api/blob/v0.2.15/frontend/src/components/user/UserAttributesConfigModal.vue>。

## 二、`agent-endpoints.md`

### E1. `agent-endpoints.md` §3.1（上游三家）— Ollama Cloud「Anthropic 相容」的出處
- **問題**：該列的引用是 `docs.ollama.com/cloud` 與 `docs.ollama.com/api/openai-compatibility`，但「Anthropic 相容」不在 openai-compatibility 頁；官方另有專頁。
- **正確說法**：陳述本身正確，建議補上專頁出處：<https://docs.ollama.com/api/anthropic-compatibility>。
- **出處**：<https://docs.ollama.com/api/openai-compatibility>（無 Anthropic 字樣）、<https://docs.ollama.com/api/anthropic-compatibility>（專頁存在）。

### E2. `agent-endpoints.md` §0、§2.2、§3.2、§4.3 — 負面主張的時效
- **問題**：「官方文件未載明／未公開 usage API」屬「截至調查日查無」型主張；官方之後補文件就會過期。
- **正確說法**：陳述在 2026-10-10 成立（子代理逐頁查證同結論）；建議改寫成「截至 2026-10-10 查無公開 usage／quota API」，把日期寫進句子。
- **出處**：<https://opencode.ai/docs/zen/>、<https://docs.ollama.com/cloud>、<https://antigravity.google/docs/settings>（本次查證皆未見公開查詢 API 記載）。

## 三、密鑰／私人資訊檢查（兩份檔＋本檔）

- **方法**：以 regex 掃描 `sk-` 長字串、`ghp_*`／`github_pat_*`、`AKIA*`、`xox[bp]-*`、`AIza*`、`ya29.*`、JWT（`eyJ….`）、`admin-<hex>`、`wos-session=<值>`、長 `Bearer`、40+ 連續 hex、email／電話樣式。
- **結果：0 命中**。文內出現的都只是**名稱或佔位符**——例如 `sk-…`、`ANTHROPIC_AUTH_TOKEN="sk-…"`、`admin-<64hex>`、`wos-session`（cookie 名稱）、`$OLLAMA_API_KEY`（變數名）；沒有任何實際值。
- **私人資訊**：未見 MyBrain 內容、email、電話。`sub2api.md` 提到的作者署名（Wesley Liddick）、貢獻者、贊助商名稱，皆為公開 GitHub 頁面可見的資訊，符合 public repo 規矩。
- 三份檔皆在檔頭自我聲明「不含金鑰／token 實際值」，與掃描結果一致。

## 四、已抽查屬實（覆蓋範圍；節錄）

- **`sub2api.md`**：以 tag `v0.2.15` clone 對照通過——`main@3a6fd1c9` 與 tag 僅差 `backend/cmd/server/VERSION` 一行（GitHub compare 證實）；13 個平台清單與順序、六種帳號型別、`api_protocol` 四值；account／api_key／group／usage_log／user_attribute schema 欄位；`claude.com/cai/oauth/authorize`、setup-token 三端點、batch 路由（含 batch-clear-error／batch-refresh）、`sk-` 前綴與 `name` 100 字、`used_percent` 以外多數欄位；OpenCode Zen/Go URL、`api.anthropic.com/api/oauth/usage`、`anthropic-ratelimit-unified-5h-utilization`、`zen/go/v1/usage`、CRS `crs_account_id`、`X-Session-Id`／`X-Conversation-ID`／`X-Claude-Code-Session-Id`、`metadata.user_id`＋UA 的 `claude_code_only` 判定、usage 無匯出端點；Dockerfile 基底映像與 deploy 檔；LICENSE LGPL v3（or later）＋CLA「irrevocable、可 proprietary 再授權」；stars 43,617／forks 9,359／issues 3,568／contributors 首位 2,782 筆，全數與 GitHub API 相符。
- **`agent-endpoints.md`**：Claude Code 五頁官方文件逐條核對——`ANTHROPIC_BASE_URL` 覆寫與 MCP tool search／Remote Control（v2.1.196）停用、憑證四變數與 header、認證優先序 7 級＋Claude apps gateway 例外、setup-token 一年期與限制、`ANTHROPIC_CUSTOM_HEADERS`（v2.1.227+）、attribution block／`CLAUDE_CODE_ATTRIBUTION_HEADER=0`、OTel 指標與 `api_error` 只在放棄重試後、exporter 變數在專案 settings 被忽略、閘道下不回報 Console analytics；opencode／Ollama／OpenRouter／Antigravity 各節（§2–§4）由子代理逐頁核對，全數 VERIFIED（僅 E1 出處註記）。

## 五、裁決

- `sub2api.md`：**通過**（3 個小修正＋1 建議）。
- `agent-endpoints.md`：**通過**（1 個出處補充＋1 個措辭耐久性建議）。
- 密鑰／私人資訊：**通過**。
- 不需退回重寫；修正可在原檔順手完成或留給後續整理。

## 六、追加審查（二輪）：`claude-failover.md`

> 審查對象：31,323 bytes（17:42:54，靜止 ≥3 分鐘後審）。方法：官方文件逐頁核對（errors／interactive-mode／headless／hooks／statusline／cli-reference／settings-reference／llm-gateway-protocol／agent-sdk-troubleshooting）＋條款原文逐字比對（legal-and-compliance／consumer-terms／commercial-terms）。
> 結論：**沒有重大錯誤**。文件對合規段落「只整理原文、不代為定論」，並建議到 MyPMO 開裁定單（§4.4），符合要求。以下 2 條為精確度建議。

### F1. `claude-failover.md` §4.1 — Consumer Terms 自動化條文的節號與引導句
- **問題**：「（Section 3.7）」這個節號不存在；該句是 **Section 3（Use of our Services）清單的第 7 點**，且引句省略了引導句，單看引句會誤以為它可獨立成句。
- **正確說法**：完整結構是 "You may not access or use, or help another person to access or use, our Services in the following ways: … 7. Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise."；建議補引導句、標成「Section 3, item 7」。
- **出處**：<https://www.anthropic.com/legal/consumer-terms>（Section 3；本次逐字核對）。

### F2. `claude-failover.md` §3.3 — apiKeyHelper 兩個重跑條件的版本註記
- **問題**：「（後兩者只在『helper 輸出就是生效憑證』且 `ANTHROPIC_AUTH_TOKEN` 未設時適用，v2.1.246+）」把 401/403 重跑與 JWT 過期重跑一起掛上 v2.1.246+。
- **正確說法**：官方的 v2.1.246+ 註記只掛在「快取輸出是過期 JWT、下一請求前重跑」這一項；401/403 重跑條目本身沒有版本註記（「helper 輸出即生效憑證且未設 `ANTHROPIC_AUTH_TOKEN`」的適用條件句亦無）。建議拆開寫。
- **出處**：<https://code.claude.com/docs/en/settings-reference#apikeyhelper>。

**已抽查屬實（節錄）**：限流訊息與 session／weekly 跨模型共用語意、85% 警告、`Server is temporarily limiting requests` 與 unified headers 的關聯、`/usage`／`/usage-credits`、exit code 語意、互動自動續跑（v2.1.234+、不重送、最多 2 次、`autoContinueAtUsageLimit`、四類限制）、SIGTERM 143／`SessionEnd`／`CLAUDE_CODE_RESUME_INTERRUPTED_TURN`、`--resume` 跨目錄（v2.1.223+）、`api_retry` 欄位與 apiKeyHelper 前兩次安靜重試、`StopFailure` matcher 清單與「無 decision control」、`Notification` 三 subtype、statusline `rate_limits` 出現條件與 `spend_limit` 版本（v2.1.251+／v2.1.284+）、`auth status` 值域與 v2.1.268+、gateway-protocol 的 response headers 逐字引句、「can't tell which upstream」、open-lists、OAuth capability 401 語意、SDK 錯誤型別；條款面：legal-and-compliance 六段引文逐字、Consumer Terms 分享帳號與 bypass 句、Commercial Terms D.4／D.5（D.4 引句用「…」略去 "and must not attempt to"，屬已標示的省略）皆與原文相符。

## 七、追加審查（二輪）：`gateway-alternatives.md`

> 審查對象：24,195 bytes（17:43:58，靜止 ≥3 分鐘後審）。方法：以文件指定的 commit 快照（LiteLLM `5282aed0`、new-api `c6741c36`、one-api `8df4a267`、CCR `057abaa1`）逐檔核對＋官方文件＋GitHub API。
> 結論：**沒有重大錯誤**；A1–A10、B1–B10、C1–C6、D1–D7、E1–E3 全數與快照相符（統計數字誤差 ≤5）。以下 1 條為路徑精度建議。

### G1. `gateway-alternatives.md` §4.2 — api-key-authorizer 路徑
- **問題**：文中寫 `gateway/api-key-authorizer`。
- **正確說法**：實際路徑是 `packages/core/src/gateway/auth/api-key-authorizer.ts`。
- **出處**：<https://github.com/musistudio/claude-code-router/tree/057abaa1/packages/core/src/gateway/auth>。

**範圍附註（非錯誤）**：E3「LiteLLM 官方文件沒有條款警示」是負向查證，僅涵蓋 README 與 docs 首頁；B10「無用量匯出」是 controller 範圍（前端另有兌換碼匯出，與用量紀錄無關）。兩者在文件中的「截至 2026-10-10」寫法已適當限縮。

## 八、前次審查項目的修正狀態（2026-10-10 檢查）

- `sub2api.md`：S1 已改成「292 支 `.sql`（目錄共 315 個項目）」、S2 已改成 `threshold_percent`／`until_unix`、S3 已改成「共 59 個 release（Link header 第 59 頁）」——全部已修。S4（補型別出處）未見變更，屬建議、可不做。
- `agent-endpoints.md`：E1 已補「Ollama 的 Anthropic 相容專頁」連結、E2 已在負面主張加「截至 2026-10-10」——兩項已修。
