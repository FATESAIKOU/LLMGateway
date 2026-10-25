## Context

動機見 proposal.md（#60）。現況（讀自 `openspec/specs/*`、`lgwd/*`）：

- list 回 `{schema, subscriptions:[…]}`，每筆有 `cooldown`（`{until}`／null）與 `available`；不含 key。resolve 回 `{schema, env_schema, meta, env}`，`meta` 目前不含 `cooldown`／`available`；409 `cooling` 的 body 帶 `until`。get 含 key，由 #48 退場。
- 冷卻只有「呼叫方登錄的 `until`」；Gateway 沒有任何上游用量資料。`providers.py` 每個 provider 一個 `CredentialProvider`；key 由 `KeyStore` 從 vault 檔讀。
- 原則：list／resolve 是主要功能，用量是附加資訊，**壞了不能拖累主要功能**。

## 一手來源（2026-10-25 以 `curl -sSL <URL>.md` 抓取）

- Ollama balance（`https://docs.ollama.com/api/balance`）：`curl https://ollama.com/api/balance -H "Authorization: Bearer $OLLAMA_API_KEY"`；「Plans with session and weekly limits return `session` and `weekly` in `included`」（標題為 *Legacy plans*）；「This endpoint allows 10 requests per minute per user, shared across API keys and devices. We recommend polling once per minute.」；429 帶 `Retry-After`。**新方案**回 `included.balance_usd`／`allowance_usd`／`period`，沒有 session／weekly。
- Ollama usage（`https://docs.ollama.com/api/cloud-usage`）：`GET /api/usage?range=24h|7d|30d[&group_by=model]`，需 Bearer，同樣「10 requests per minute per user」；回請求數、token、`usage_usd` 的分桶。
- Claude 合規（`https://code.claude.com/docs/en/legal-and-compliance`，下稱 S1）：「The Claude Code binary must not be modified. Claude Code must be installed and run as published by Anthropic」；「OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans」；「developers may not collect, store, or intermediate Claude.ai credentials or session tokens」。
- statusline（`https://code.claude.com/docs/en/statusline`）：`rate_limits.five_hour.used_percentage`／`resets_at`（epoch 秒）、`seven_day` 同；「appears only for claude.ai Pro and Max subscribers… and only after the first API response in the session」；每個窗口可獨立缺席。
- `/usage`（`…/docs/en/commands`、`…/headless`）：`/usage` 是互動指令；headless 文件寫「Built-in commands that only run in the terminal interface, such as `/login`, aren't available」，**`-p` 下 `/usage` 是否可用與輸出格式沒有文件**。

## Goals / Non-Goals

- Goals：list（與 resolve）帶每把的用量、來源、取得時刻與「再次能用的時間」；讀不到說「未知」；上游失敗不影響主要功能。
- Non-Goals：不加端點；不依用量改選把或自動登錄冷卻；不做 OpenRouter／Zen 的用量。

## 本人的決定（2026-10-25，經 PMO 轉達）

- **A～D 都照建議**：A 用量放在 list＋resolve（舊 get 退場）；B 額度用完不自動把 key 標成不可用（交給既有冷卻）；C Claude 用量由 Gateway 上的原版 Claude Code 取（statusline；`/usage` 退路 2026-10-25 實測不可用，已不採用）；D Ollama 只取 `/api/balance`。
- **E1**：只在有人查時才取，快取 30 分鐘（不做每 30 分鐘的主動排程）：list／resolve 被呼叫、而且快取過期，才取一次。細節見決定 4。
- **E2**：維持現狀，不另開單。本人原話：「實際上使用者只有我，並且 key 是實際上會配到機器上，LLM 通訊不經過 LLMGateway，所以應該沒關係？」理由與結論見決定 6。
- **F**：都不等、背景取——list／resolve 不等取用量，回上次快照並標 `stale`（最舊 6 小時）；Ollama 與 Claude 一律如此，不做有上限的同步等待。細節見決定 4。

## 決定 1：介面放在哪（本人已決定 A）

| 選項 | 優點 | 缺點 |
|---|---|---|
| get | 本人原話 | #48 已決定退場，新功能放進去等於替它續命 |
| 只放 list | 最小；PMO 與人看菜單就有；resolve 完全不動 | sidecar 換把時看不到選中那把的用量 |
| list＋resolve（`meta.usage`） | 換把的人在拿 env 時順便知道；與 list 同一份快取、同一個形狀 | resolve 形狀多一塊；`meta` 原則是「只帶有資訊的欄位」 |

**本人已決定：list＋resolve。** resolve 與 list 一樣只讀快取、並依決定 4 在快取過期時觸發一次非阻塞（本人 10-25 裁定 F）的取用量（回應本身不等）；`available_again_at` 不放 resolve（200 代表可用，恆為 null，沿用 `cooldown`／`available` 不進 meta 的理由）。
**get 不帶**：`_item` 是 list 與 get 共用（`registry.py`），所以不改 `_item`；`usage`／`available_again_at` 在 `list_subscriptions` 內逐筆合入，resolve 的 `meta.usage` 在 resolve 路徑組裝。get 的回應形狀不變。

list 每筆新增兩個欄位（示例數字皆為假值）：

```json
{"id": "ollama-cloud", "...": "…既有欄位不變…",
 "cooldown": null, "available": true,
 "available_again_at": null,
 "usage": {
   "status": "ok", "source": "ollama-api-balance",
   "fetched_at": "2026-10-25T03:30:00Z", "stale": false, "reason": null,
   "session": {"remaining_percent": 75, "resets_at": "2026-10-25T07:00:00Z"},
   "weekly":  {"remaining_percent": 40, "resets_at": "2026-10-28T00:00:00Z"}}}
```

讀不到、或不適用（`session`／`weekly` 恆在，值為 null；**絕不用 0 代替**）：

```json
"usage": {"status": "unknown", "source": "claude-statusline", "fetched_at": null,
          "stale": null, "reason": "not_fetched_yet", "session": null, "weekly": null}
"usage": {"status": "not_applicable", "source": null, "fetched_at": null,
          "stale": null, "reason": null, "session": null, "weekly": null}
```

- `status`：`ok`（至少一個窗口讀到）／`unknown`（有來源但這次讀不到，含窗口全部已過期）／`not_applicable`（這個 provider 沒有用量來源）。
- `reason`（固定碼，不回顯上游內容）：`not_fetched_yet`、`disabled`、`no_key`、`upstream_error`、`rate_limited`、`timeout`、`parse_error`、`no_windows`（方案沒有 session／weekly，例如 Ollama 新方案；Claude 非 Pro／Max 或欄位缺席也歸此，分不出差別）、`stale_expired`（含窗口全部已過期）、`internal`。
- 窗口命名統一：Claude 的 `five_hour`→`session`、`seven_day`→`weekly`；Claude 給的是 used，換算 `remaining_percent = 100 - used`（保留原始小數，顯示端自行取整）；`resets_at` 一律 RFC 3339 UTC。窗口的 `resets_at` 已過而尚未重取時，該窗口回 null（不假設已回到 100%，Claude Code 自己也在 `resets_at` 後丟掉該窗口）。
- `stale`：`now - fetched_at` 超過 TTL 為 true（已過期、重取尚未完成或失敗；沒人查時值本來就會變舊）；超過 6 小時轉 `unknown`／`stale_expired`，不再顯示舊值。`status: ok` 且 `stale: true` 時，`reason` 帶**最近一次取用量失敗的碼**（例如 `upstream_error`）；最近一次沒有失敗（只是過期、重取還沒完成）則為 null。
- **與冷卻合併**：`cooldown.until` 與 `available` 照舊（只反映登錄的冷卻與是否部署）。`available_again_at`＝ 取 `cooldown.until`（未來才算）與「`remaining_percent` ≤ 0（用原始數值判斷，不先取整）的窗口之 `resets_at`」中**最晚**者；都沒有為 null；key 沒部署（`available=false` 的另一個原因）時一律 null。`unknown` 時不推測（null，不是「現在」）。`available` **不**因用量 0% 而變 false（本人已決定 B：額度用完不自動標成不可用，交給既有冷卻）。

## 決定 2：Ollama Cloud

- 取用量：`GET https://ollama.com/api/balance`，URL 寫死在程式裡（不來自政策檔、不跟隨轉址到別的主機）。key 從 `KeyStore` 取，只放進該次請求的 `Authorization` header；不進 log、不進例外訊息、不進回應。
- 只取 `included.session`／`included.weekly` 的 `remaining_percent`、`resets_at`（欄位白名單，其餘丟掉）。回應沒有這兩個物件（新方案）→ `unknown`／`no_windows`。這是風險：**本人現行方案是舊方案還是新方案，本設計沒有查（不讀憑證、不呼叫需要 key 的 API）**；若是新方案，session／weekly 恆 null；**本人已決定 D：新方案的金額（`balance_usd`／`allowance_usd`）不取、不帶 `credits`，一律 `unknown`／`no_windows`**。
- 限流：10 次／分鐘／使用者（跨 key 與裝置共用）；我們一把 key，TTL 120 秒、被查到且過期才取，單一 subscription 同時一個在跑，所以上限約 0.5 次／分鐘（低於官方建議的每分鐘 1 次）。收到 429 依 `Retry-After` 停到時間後才重試（`rate_limited`）。逾時 5 秒，不重試。
- `/api/usage`（PMO 方向有列）：**本人已決定 D：不取**（list 不需要請求數，也不多佔 10 次／分的限額）。

## 決定 3：Claude 訂閱（本人已決定 C）

合規前提（S1）：訂閱 OAuth token 只給**未修改的原版 Claude Code**；Gateway 不得自己拿 token 打 Anthropic 的 API／usage endpoint，也不得收集或轉手 session token。因此 Gateway 不自己寫 HTTP 呼叫，而是**執行原版 Claude Code 並讀它的輸出**。本 change 新增「Gateway 主機上自己也跑一個」；現況（Gateway 保管並分發 token）是否合規**不在此認定**；本人已決定維持現狀、不另開單，見決定 6。

| | 管道 1：statusline `rate_limits` | 管道 2：`claude -p "/usage" --output-format json` |
|---|---|---|
| 文件 | 有（欄位、單位、缺席規則） | 互動指令有文件；`-p` 下可用與輸出格式**沒有** |
| 穩定性 | 官方欄位，版本升級有變更記錄 | 版本一改可能就壞 |
| 成本 | 要第一個 API 回應才有值 → 每次取用量耗少量訂閱額度（只在被查到且快取過期時才取，見決定 4） | 可能不耗，但未知；`/usage` 自己也會被限流（文件：顯示 last-known usage） |
| 未驗證處 | statusline 在 `-p` 是否執行沒有文件（已探測：不跑，見下方「探測結果」）；互動式 pty 又有 workspace trust 關卡 | 同上 |

**本人已決定：管道 1 為主、管道 2 為退路、都失敗回 `unknown`；先實測再鎖定。（2026-10-25 探測後，管道 2 已不採用，見「探測結果」；現在只用管道 1。）** 做法：最小環境下跑 Claude Code（最便宜的模型、一則最短的輸入），以 `--settings` 指定一個只把 stdin JSON 的 `rate_limits` 寫進暫存檔的 statusLine 命令。statusline 文件寫「Until you accept the workspace trust dialog, the status line stays blank」，所以**不能每次新建空目錄**：互動式（pty）改用服務帳號下一個**固定、事先 trust 過**的工作目錄；暫存檔放服務帳號自己目錄下的固定前綴子目錄，lgwd 啟動時掃掉前綴相符的舊殘留。實作第一步是在真機做一次**探測**，結果回報後才鎖定實作；探測需本人授權。探測項目：(a) trust 關卡（`-p` 與 pty 下 statusline 會不會跑）；(b) `/usage` 的 `-p` 輸出；(c) `~/.claude` 的 session 紀錄去向與累積量；(d) 子行程連了哪些主機，以及 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 是否影響 `rate_limits`。**探測失敗（statusline 取不到值）時，C 一律回 `unknown`。**

**探測結果（2026-10-25）**（#68；只記結論）：
- `claude -p` 下 statusline **一次都不跑**，所以 `rate_limits` 拿不到 → 改用 **pty** 啟動互動式 claude（不加 `-p`），cwd 是事先 trust 的固定目錄；第一個 API 回應之後 statusline 才帶 `rate_limits`（第 1 次呼叫沒有，之後有）。
- `claude -p "/usage"` 只回 session cost，**沒有窗口**（沒有 used／remaining／resets）→ 管道 2 實測不可用，不採用（已從 spec 與程式拿掉）。
- 互動式 claude 回完 `ok` **不會自己退出**，所以結束條件不依賴它：rate 檔（statusline 只在帶 `rate_limits` 時以 tmp＋mv 寫入）出現就收；statusline 每次呼叫另留一個記號，送出輸入後記號增加、rate 檔卻遲遲不出現（再等一段寬限）→ `no_windows`（非 Pro／Max）；收行程時先送 `/exit`，短暫等待沒退出再終止自己記下的行程群組。畫面只在記憶體判斷關卡（trust／主題／登入、送出後的 `run /login` 錯誤），不進 log。
- 以上尚待階段 C 在真機確認的：寬限秒數、關卡字樣是否對得上真畫面。

硬規則：
1. Gateway 主機要裝**原版** Claude Code（依官方安裝方式、不修改、不重新包裝），由服務帳號執行；版本號只記在 log（不記其他輸出）。
2. token 只以子行程的**環境變數** `CLAUDE_CODE_OAUTH_TOKEN` 傳入，不放 argv、不寫檔；子行程環境用白名單（不繼承服務的環境）。
3. 子行程 stdout／stderr 不進 log；只留 `rate_limits` 的四個數字欄位。解析失敗（欄位缺、型別不對、非 Pro／Max）→ `unknown`（`parse_error`／`no_windows`）；**不猜、不補 0**。
4. 逾時 60 秒；逾時只終止**自己記下 pid 的那個行程群組**，不依名稱殺行程。
5. 不呼叫沒有文件的 usage endpoint；管道 1 取不到時就是 `unknown`，不再找別條路（管道 2 已不採用，見探測結果）。預設關閉（`reason: "disabled"`），本人授權部署後在政策檔開。

## 決定 4：取得頻率、快取、隔離

本人已決定 E1：**只在有人查時才取**，不做定時的主動排程。

- **觸發**：list／resolve 組裝某個 subscription 的 `usage` 時，若該 subscription 有取用量實作、已啟用，而且快取不存在或 `now - fetched_at` 超過 TTL，就觸發一次取用量。沒有人查就不取；重啟後從 `not_fetched_yet` 開始，也不在啟動時預取。resolve 回非 200（409 `cooling`、404）時不組裝 `meta.usage`，也不觸發。
- **非阻塞（本人 10-25 裁定 F）**：觸發只是「叫一條取用量執行緒去做」，這次 list／resolve 的回應**不等它**：
  - 有舊值 → 回舊值（`stale: true`，`fetched_at` 照實）；
  - 從沒取過 → 回 `unknown`／`not_fetched_yet`；
  - 新值在取完後寫進快取，**下一次**查詢才看到。Claude 取一次最多 60 秒，若阻塞，list 與 resolve 會被拖住，違反「用量壞了不能拖累主要功能」。
  - 同一個 subscription **同時只有一個**取用量在跑（記憶體裡一個 in-flight 旗標）；執行中再被查到不重複觸發。觸發判斷、in-flight 旗標與一個「已停止」旗標用**同一把鎖**；`Gateway.stop` 設下「已停止」之後不再觸發任何取用量。spawn 子行程與登記該子行程（pid）在同一把鎖內，spawn 前再檢查一次「已停止」旗標，避免 stop 與 spawn 交錯而漏收。
  - 失敗（含 `rate_limited`）記下「下次最早可再觸發的時間」＝ max(退避, `Retry-After`)：連續失敗時退避間隔加倍（從 TTL 起算），上限 max(30 分鐘, TTL)；成功即重置；在這段時間內被查到不觸發，回的仍是舊值或 `unknown`（`reason` 帶最近一次失敗的碼）。Claude 的 TTL 已是 30 分鐘＝退避上限，所以其退避實際上就等於 TTL；真正會延後的只有 `Retry-After` 更晚的情況（以及 Ollama）。
- **非阻塞的後果（讀者要知道）**：推估：本人的用法是零星查詢（使用者只有一人），查詢間隔常超過 TTL，所以多半**每次拿到的是上一次查詢那一刻觸發的快照**，`stale: true`（最舊 6 小時，之後轉 `unknown`／`stale_expired`）；重啟後第一次查一律是 `unknown`／`not_fetched_yet`；sidecar 換把時 resolve 拿到的也多半是舊值。要看到新值，得在第一次查觸發後，等取完（Claude 最多約 60 秒、Ollama 最多約 5 秒）再查一次。這個語意已由本人 10-25 裁定 F（都不等、背景取）。
- **TTL**：Claude 30 分鐘（本人指定；每次耗額度，窗口以小時／天計）；政策檔**不能調低**，下限 30 分鐘（可調高）；設定值低於 30 分鐘時夾到 30 分鐘並記一筆 log。Ollama 120 秒（作者提議，同樣改成「被查到才取」；不耗額度、查詢本來就稀疏，TTL 只是防止連續查詢打爆 10 次／分的限額）。
- **子行程護欄**：殺行程的護欄（`start_new_session`＋`killpg`，不打到自己的群）、stderr 只留尾端、秘密只放子行程環境，這些與 `lgwd/keysync.py` 的 `KeySyncTimer` 同一類需求。沒有定時排程以後 **runner 不需要「計時」**，所以作者提議：只把子行程起停、killpg 護欄、stderr 尾端抽成共同函式讓 keysync 與 Claude 取用量共用（不另寫一套），計時留在 keysync；環境由呼叫方給（keysync 照舊繼承，Claude 用白名單）；**keysync 的對外行為與狀態檔都不變**。取用量執行緒由觸發時才建立；`Gateway.stop`（`lgwd/main.py`）先設「已停止」旗標，再**一律終止**仍在跑的取用量子行程，不等它跑完：SIGTERM→join→SIGKILL，同 `KeySyncTimer.stop`，只終止自己記下 pid 的群組；順序在 `api_server.shutdown()` 之前，也不會因還在處理的 list 而再起新子行程（已停止旗標擋掉）。每次取之前先 `reload_keys_if_changed()`，免得換 key 後一直用舊 key；結果放記憶體 dict（鍵＝subscription id），不進 SQLite、不進稽核。
- **逾時**：Ollama 5 秒、Claude 60 秒。
- **額度估算（推估）**：Claude 每次取耗少量訂閱額度；上限是每 30 分鐘一次（若全天都有人查，最多約 48 次／天），實際次數取決於查詢頻率，沒人查就是 0（推估，未實測；實際耗量待探測）。這是「ordinary usage」判斷的依據之一：不是固定的每日 48 次。
- **隔離**：list／resolve 只做一次字典讀取、純計算（`available_again_at`、`stale`）與「是否觸發」的判斷（旗標檢查＋建一條執行緒），不做網路／子行程 I/O、不拿取用量執行緒的鎖超過一次複製的時間；取用量的任何例外在執行緒內捕捉並轉成 `unknown`；組裝 `usage` 欄位與觸發判斷本身再包一層 try，失敗時該欄位回 `unknown`／`internal`，list 與 resolve 仍 200。測試要有「取用量卡住或拋例外時 list 與 resolve 的延遲與結果不變」，以及：「過期後連續多次查詢只觸發一次取用量」；「失敗後退避期間內查詢不觸發」；「429 照 `Retry-After` 延後」；「stop 之後再查不觸發，在跑的子行程被收掉」。

## 決定 5：其他訂閱

規則：`CredentialProvider` 沒有取用量實作 → `not_applicable`；有實作但讀不到 → `unknown`。現行 OpenRouter、OpenCode Zen 沒有實作，回 `not_applicable`（本 change 沒有查證它們的官方用量管道，**未知，不代表沒有**；要做另開單）。現行政策只有一把（claude-personal）；日後重新加入的 Claude id 各自取、各自快取。

## 決定 6：安全與部署

- key／token 不進回應、log、稽核；既有的憑證樣式掃描要涵蓋 list 與 resolve 新增欄位及 log。錯誤只記固定碼（`reason`）與 provider id，不記上游回應內容。
- 新增外連：lgwd 直連只有 `ollama.com`（與既有的 endpoint 同主機）；Claude 的外連由原版 Claude Code 自己發出，Gateway 不另開連線；非必要流量（更新、遙測）以探測 (d) 確認。
- **E2 token 保管的現況（本人已決定：維持現狀，不另開單）**：S1 寫「developers may not collect, store, or intermediate Claude.ai credentials or session tokens」，Gateway 本來就保管並分發 token。本人的判斷：使用者只有本人一人；key 實際上是配到機器上使用；LLM 通訊不經過 LLMGateway（Gateway 不代轉對模型的請求）。據此結論為維持現狀，不另開單。本 change 不改變它，本設計也不替它做合規認定。
- 部署是真機動作：安裝 Claude Code、trust 固定目錄（官方做法：在 `~/.claude.json` 把 `projects["<path>"].hasTrustDialogAccepted` 設成 `true`；出處 `https://code.claude.com/docs/en/permissions`，2026-10-25 curl 核對原文）、開 Claude 取用量、探測。計畫交 PMO，由本人授權；授權前程式碼可先上線且 Claude 取用量保持 `disabled`。退版＝`install.sh --tag` 回前一版（欄位是新增的，舊消費端不受影響）。

## 本人的判斷點

無。A～D、E1、E2 皆已由本人於 2026-10-25 裁定，見文首「本人的決定」；Claude 探測失敗時一律 `unknown`（決定 3）。S1 對 ordinary usage 的原文是「Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK」。本人裁定做 C，頻率照 E1（被查到且過期才取、快取 30 分鐘）；本設計據此視為 ordinary usage 的處理（作者解讀，本人未明說）。

## 作者提議、本人未指示

- 回應帶 `stale`、`reason`；超過 6 小時的舊值不再顯示。
- 取用量的 TTL（Claude 30 分鐘除外，那是本人指定）、逾時、退避數字，以及政策檔新增 `usage` 設定段（開關與 TTL）。
- `available_again_at` 的「取最晚者」合併規則。
- Claude 取用量預設關閉、需本人授權才開。
- 不依用量自動登錄冷卻（避免與呼叫方登錄的 `until` 互相覆蓋）。
- **E1 的實作方式**：同一 subscription 同時只取一次、失敗後退避；本人只指示「被查到且過期才取、快取 30 分鐘」。（非阻塞已由本人 F 裁定，見文首。）
- Ollama 的 TTL 120 秒也改成「被查到才取」。
- 不做定時排程後，只共用子行程護欄（起停、killpg、stderr 尾端），不共用 `KeySyncTimer` 的計時。

## 待本人決定

無。（F 已由本人 10-25 裁定，見文首「本人的決定」。）
