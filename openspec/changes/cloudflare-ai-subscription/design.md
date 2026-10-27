## Context

#89：新增 `cloudflare-ai`（Workers AI 免費額度，只給 opencode）。與 groq 前例（`openspec/changes/groq-subscription/`、commit 39a3527）的差別只有一個：**endpoint 含 account id、憑證有兩個值**。其餘（policy 白名單、`_OPENCODE_ONLY`、`KEY_NAMES`、無 usage fetcher）照前例。

現況（讀過的程式）：
- `lgwd/keys.py`：`KEY_NAMES` 訂閱→單一鍵名；`_parse` 只認 `KEY_NAMES.values()` 的鍵；`deployed()`＝該鍵非空。
- `lgwd/providers.py`：`endpoint` 是類別常數；`validate_endpoints()` 擋 `@` 與 `key=`／`token=`／`api_key=`／`access_token=`；`build_env` opencode 只回 `OPENCODE_*` 四鍵。
- `lgwd/registry.py` `resolve`：`provider_info.build_env(agent, key, model)`；`meta.endpoint = provider_info.endpoint`；稽核只記 agent／provider／model，**不記 endpoint**。`ENV_SCHEMA_VERSION = 1`，鍵集變更 MUST bump（舊 sidecar 對高版本拒收，且 `env_schema` 是全域的）。

## 官方事實（出處與讀取日期：2026-10-27）

| 事實 | 出處與原文 |
|---|---|
| OpenAI 相容 base | <https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/>：`https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions`（cURL 範例）；SDK 範例 `baseURL: \`https://api.cloudflare.com/client/v4/accounts/${env.CLOUDFLARE_ACCOUNT_ID}/ai/v1\``、`apiKey: env.CLOUDFLARE_API_KEY` |
| 認證 | 同頁：`Authorization: Bearer {api_token}` |
| 模型名格式 | 同頁：`@cf/meta/llama-3.1-8b-instruct`、`@cf/openai/gpt-oss-120b`（含 `@` 與 `/`） |
| 支援端點 | 同頁：`/v1/chat/completions`、`/v1/embeddings`、`/v1/responses`（僅 GPT-OSS、僅非串流） |
| opencode 有內建 Workers AI | <https://opencode.ai/docs/providers/>「Cloudflare Workers AI」：`/connect` 搜 Cloudflare Workers AI；環境變數 `CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_API_KEY`。**該頁沒給 config 用的 provider id**（另一個 `cloudflare-ai-gateway` 是不同 provider，要 `CLOUDFLARE_GATEWAY_ID`＋`CLOUDFLARE_API_TOKEN`，不是這張） |
| 付費才能用的模型 | <https://developers.cloudflare.com/workers-ai/platform/pricing/>：kimi-k2.6、kimi-k2.7-code、glm-5.2、glm-5.3、glm-5.3-flash、deepseek-v4-flash-0731、deepseek-v4.1-flash-1017、deepseek-v4-pro-0813（共 8 個） |
| 查剩餘額度 | 同頁：只有 dashboard（"You can monitor your Neuron usage in the Cloudflare Workers AI dashboard"）；**沒有程式可查的端點** |
| 重置 | 同頁："All limits reset daily at 00:00 UTC" |

**未查證**：opencode 內建 Workers AI 的 **config provider id**（推測 `cloudflare-workers-ai`，未驗證；實作前要用 opencode 原始碼或 models.dev 目錄確認）；各免費模型是否支援 tool calling（opencode 當 coding agent 需要）；account id 的格式（opencode 頁寫 "32-character account id"，只當格式檢查的依據）。

## 決定

### 1. account id 怎麼進來

| | A：key 檔多一鍵 `CLOUDFLARE_ACCOUNT_ID` | B：同一個值 `id:token` | C：account id 放 policy |
|---|---|---|---|
| endpoint 不內嵌憑證 | 是（樣板只有 `{account_id}`；置換後路徑段 `accounts/<id>`，無 `@`、無 `key=`；兩者都過 `validate_endpoints`） | 同 A | 同 A |
| `list`／`get` 的 endpoint 洩不洩 | 回樣板原樣就不洩（決定：樣板不置換） | 同 A | policy 檔在 public repo 的 `policy/default.json`——**實際值會進 repo**，直接違反「public repo 不寫私人資訊」；若改放部署端的另一份 policy 檔，要新增政策檔分層機制 |
| 稽核／log | 稽核不記 endpoint；置換值只出現在 resolve 回覆。要加 canary 測試釘住 | 同 A | policy 內容會被 loader／狀態板印出的機會較多 |
| key 檔解析與錯誤 | `_parse` 白名單加一鍵；`deployed` 改為「token 與 account id 都非空」；account id 格式不合視同格式錯誤（訊息只帶行號＋種類）；`ops-scripts/key-sync` 驗證用同一份 `keys.py`，白名單同步；key 檔解密來源要多放一行 | 一鍵不用動白名單，但自訂分隔語法：值內有 `:` 的 token 會被誤切；格式錯誤的訊息要小心不回顯；與「值整串是不透明憑證」的現有紀律不符 | 不動 key 檔，但 `deployed()` 之外多一個條件在 policy |
| 測試 | 假 token＋假 account id（例如 32 個 `a`）；canary 不進 list／audit／DB／log／repr | 同 A，另測分隔錯誤 | 測 loader；但出廠 policy 只能放假值，部署要另一份 |
| 對 sidecar `env` 介面 | 不破壞（見下 1.1） | 不破壞，但 token 不再是不透明 | 不破壞 |

**建議：A。** B 把不透明憑證變成要解析的複合值，C 讓私人值進 public repo 或逼出政策檔分層，都比 A 的「多一行」貴。

#### 1.1 account id 怎麼交給 sidecar（A 內的子選擇，**判斷點 J1**）

| | A-meta（建議） | A-env |
|---|---|---|
| 做法 | `meta.endpoint` 回置換後的完整 base；`env` 不變 | `env` 加 `CLOUDFLARE_ACCOUNT_ID`（opencode 內建 provider 原生讀這個變數） |
| `env_schema` | 不 bump（meta 加值不需要） | **必須 bump 到 2**；`env_schema` 是全域的，舊 sidecar 對高版本拒收＝所有 worker 的所有訂閱 resolve 都被拒，直到映像更新 |
| sidecar 工作 | sidecar（MyLinuxPool）要從 `meta.endpoint` 取出 account id（或把 endpoint 當 baseURL）給 opencode；現行 spec 說「不抄進 `options.baseURL`」（#28），這張要對 cloudflare-ai 開例外或讓 sidecar 解析 | sidecar 把已知鍵併入子行程環境即可（現行 overlay 機制：清單上的鍵全數併入） |
| 風險 | 跨 repo 依賴一個「meta.endpoint 要被消費」的新約定；沒改 sidecar 前，這把 opencode 會缺 account id 而失敗（不影響其他訂閱） | 風險集中在升版瞬間：Gateway 先升＝全部舊 sidecar 拒收 |

建議 A-meta：不 bump、失敗只限這一把。但這把能不能用取決於 sidecar 的 PR，這是本張最大的跨 repo 依存，需本人／PMO 決定是否另開 MyLinuxPool 單。若本人偏好把 account id 當憑證類，走 A-env＋bump 也可行，代價是同步升版。

Gateway 側的 `build_env` 與 `OPENCODE_API_KEY`＝token 兩案相同。

### 2. opencode 端怎麼接

- 官方文件有內建 Cloudflare Workers AI，吃 `CLOUDFLARE_ACCOUNT_ID`＋`CLOUDFLARE_API_KEY`（出處見上表）。
- 現行 `env` 的 opencode 片段是 `OPENCODE_API_KEY`／`OPENCODE_PROVIDER_ID`／`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`，由 sidecar 組 `OPENCODE_CONFIG_CONTENT`。建議 resolve 對 cloudflare-ai 回：`OPENCODE_API_KEY`＝token、`OPENCODE_PROVIDER_ID`＝opencode 的 Workers AI provider id（**未查證**，推測 `cloudflare-workers-ai`，實作前確認，**判斷點 J2**）、模型兩鍵＝選定模型（原樣含 `@cf/`）。
- sidecar 是否要把 `OPENCODE_API_KEY` 再映成 `CLOUDFLARE_API_KEY` 給內建 provider，取決於 sidecar 組 config 的方式（用 `options.apiKey` 還是環境變數）——未查證，列為 sidecar 單的驗收。Gateway 不多回 `CLOUDFLARE_API_KEY`（避免重複憑證鍵與鍵集變動）。

### 3. policy

- `priority` 28（介於 groq 25 與 ollama-cloud 30）、`agents` `["opencode"]`（`_OPENCODE_ONLY` 同一條檢查，`["opencode","claude-code"]` 等都拒收）。
- `models.allow` **逐字列、不放 `*`**。#80 §3.7 已查證可用：`@cf/openai/gpt-oss-120b`、`@cf/meta/llama-3.3-70b-instruct-fp8-fast`、`@cf/qwen/qwen3.8-27b`、`@cf/zai-org/glm-4.7-flash`。`@cf/openai/gpt-oss-20b` 在 OpenAI 相容頁出現，但**免費與否未在定價頁確認**（不在付費清單上，但「不在清單」不等於免費可用）→ **判斷點 J3**：建議先不放，實作時查定價表再補。`default`＝`@cf/openai/gpt-oss-120b`。付費 8 個一律不放；測試釘住 8 個都不被涵蓋。
- fnmatch 注意：模型含 `@` 與 `/`，逐字樣式沒有萬用字元，無影響；`@` 在 `fnmatch` 不是特殊字元。`resolve` 的 model 形狀驗證（長度、`<agent>/` 前綴、萬用字元）需用 `@cf/...` 實測過（測試涵蓋）。
- 沒有 usage fetcher：官方只有 dashboard。後續（不在本單）：若 Cloudflare 的 GraphQL Analytics API 能查 neurons 用量，可另開單；**未查證**，且需要另一個有 Analytics 讀取權限的 token，違反「一訂閱一鍵」，不建議。
- 冷卻：免費額度每天 00:00 UTC 重置，撞牆（429 一類）時 sidecar 以既有 cooldown 介面回報；無需新機制。

### 4. 測試計畫（全用假值，不接真端點）

- 假 token canary、假 account id（32 個 `a`，不是真值）。
- `test_policy`：出廠 policy 可載入、順序含 cloudflare-ai 且在 groq 與 ollama 之間、`agents` 非剛好 `["opencode"]` 拒收、allow 逐字且不涵蓋付費 8 個。
- `test_keys`：兩鍵部署才算部署；只有 token／只有 account id／account id 空／格式不符各自的結果；canary 不進 repr／錯誤訊息／log。
- `test_registry`：list／get 的 endpoint 為樣板且回覆全文不含 account id；resolve 的 `meta.endpoint` 已置換、`env` 鍵集與 groq 相同；model 含 `@cf/` 原樣；`usage` 為 `not_applicable`；稽核與 DB 全文不含 account id。
- `validate_endpoints`：樣板與置換後都過；仍擋含憑證樣式的假 endpoint（既有測試）。
- `test_keysync`、`test_http`、`test_main`：列舉數量與順序更新。
- 改壞驗證三項見 tasks 4.3。測試前景跑，指定檔，不跑全套。
- **部署**：key 與 account id 到位前不部署；部署與 key 檔由本人，PM 不碰；部署前要確認 sidecar 已支援 J1 的交付方式，否則上線也只會在 opencode 端失敗。

### 5. 沒有 key 檔時的行為

`CLOUDFLARE_API_KEY` 或 `CLOUDFLARE_ACCOUNT_ID` 缺／空＝未部署，`list` 的 `available` false，resolve 該把 409 `not_deployed`，與其他把一致，不影響其他訂閱。

### 6. 資料處理邊界（為什麼只有它能碰真資料）

#80 §3.7：ZDR＋不訓練的承諾只涵蓋「一次性推論」；接 R2／KV／Durable Objects／Vectorize、AI Gateway logging／caching、非同步 batch 都不在內。Gateway 管不到 sidecar 的使用方式，所以 `agents` 只有 `opencode` 這一條不足以保證；使用規則由操作端承擔（寫入 `docs/deploy.md` 時註明）。同時 account id 本身不是憑證，但是私人資訊；與 token 同等處理（見 spec ADDED）。

## 本人的判斷點

- **J1**：account id 交付方式——A-meta（不 bump，要 sidecar 單）或 A-env（bump `env_schema` 2，全 worker 同步升版）。建議 A-meta。
- **J2**：opencode 內建 Workers AI 的 provider id（未查證），以及是否授權作者／實作者去驗證。
- **J3**：`@cf/openai/gpt-oss-20b` 等「官方未明確標免費」的模型放不放；建議不放。
- **J4**：是否另開 MyLinuxPool 單處理 sidecar 消費 `meta.endpoint`（或 account id）。
- **J5**：account id 格式檢查（32 位十六進位）要不要做；建議做，錯誤訊息不回顯值。

## 作者提議、本人未指示

- 建議 A（key 檔多一鍵）而非 B／C。
- 建議 A-meta（不 bump `env_schema`）。
- `list`／`get` 回樣板原樣、`resolve` 才置換。
- allow 只放 §3.7 已查證的 4 個、不放 gpt-oss-20b；default 為 `@cf/openai/gpt-oss-120b`。
- account id 格式檢查。
- 不為 Analytics API 開 usage fetcher。
- 「附屬值」機制（KeyStore 支援單一訂閱多值）的具體介面留給實作 PR。
