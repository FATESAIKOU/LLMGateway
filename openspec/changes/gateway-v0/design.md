## Context

動機與範圍見 proposal.md（v3）。影響做法的現況與限制：

- **Claude 條款**：訂閱的 OAuth 是給原版 Claude Code 這類 Anthropic 原生 app 的一般使用；第三方不得代為路由、收集、存放、中轉 Claude.ai 的 token（`docs/research/claude-failover.md` §7）。LLMGateway 不代轉請求，Claude 訂閱的 key 只給 `agent=claude-code` 的 get。
- **sidecar v3**（MyAiStorage，aistorage-pm）：一台機器一個 daemon（Mac 也能跑），對外五個操作加 HTTP 控制，不基於 herdr。它用 LLMGateway 的 list／get／登錄冷卻；挑把與換把在它內部；key 變成哪些環境變數由它的 AgentDriver 決定。
- **服務的位置**（本人裁定）：本人選定的常駐 provider，LLMGateway 跟 MyLinuxPool 無關，用自己的帳號 `llmgw` 跑。
- **期 1 設計**（已封存為 `openspec/specs/agent-lease`、`usage-record`）：以租約為中心。v3 不再有租約，這兩份規格在本 change 移除。

## Goals / Non-Goals

**Goals**
- 對外只有 list、get、登錄冷卻三個介面（本人：「不需要多餘介面」）。
- 五把 key 只放一處；值只出現在 get 的回覆。
- 服務只用 Python 標準函式庫＋SQLite。

**Non-Goals**
- 不轉送模型請求；不挑把（由 sidecar 挑）；不計量 token／成本；不做 agent 專用的環境變數對應。

## Decisions

### 決定 1：LLMGateway＝保管＋列出＋記冷卻

- 服務只做三件事：保管 key、列出 subscription（含冷卻與能不能用）、記冷卻。誰用哪一把、什麼時候換，都由 sidecar 決定。
- **為什麼**：
  - 本人的新方向；
  - 換把需要的資訊（額度用完、重置時刻）本來就在 sidecar 手上；
  - LLMGateway 不必知道 agent、租約、機器。
- **放棄的選項**：
  - 租約（PR #9 上一版）：要多五個介面與一套狀態機，本人要求不要多餘介面。
  - 資料面代轉（第 1 版）：違反 Claude 條款。

### 決定 2：key 的保管與部署（照舊）

- 來源是本 repo 的 GitHub secret，值由本人設，名稱照本人的命名規則（tasks 3.3）。
- 部署的路：本 repo 的 workflow 經池的 SSH 跳板，以 `llmgw` 登入放服務的那台。
  - 跳板上的部署金鑰只能轉發到那台的隧道埠（MyLinuxPool#68）。
  - 那台上 `~llmgw/.ssh/authorized_keys` 以 `command="…/current/deploy/remote-deploy.sh",restrict` 強制執行部署腳本。
- 部署腳本的動詞（從 `SSH_ORIGINAL_COMMAND` 以白名單解析）：
  - `keys`：stdin → 驗證 → 暫存檔 0600 → rename → 重讀。v3 起 stdin 除了五把 key，還帶各類機器的讀取 token（決定 4）。
  - `upgrade <tag>`：固定 tag 取碼、切換 current、重啟。
- 值用 stdin 傳、不上命令列；workflow 的 log 只有 secret 名稱；ssh 的錯誤輸出會遮掉位址。

### 決定 3：服務的形狀（照舊，資料更少）

- Python 標準函式庫，HTTP 用 `http.server.ThreadingHTTPServer`、資料用 `sqlite3`，沒有第三方套件。
- 在那台用帳號 `llmgw` 跑 systemd user unit（linger），`llmgw` 不加 docker 群組。安裝、unit、run wrapper、版本、`--check` 在本 repo 的 `deploy/`。
- 只綁 loopback 與同一台容器看得到的 docker bridge 位址。
- **資料**：SQLite 只有兩張表：
  - `cooldowns`（subscription × 窗口 → 結束時刻）；
  - `audit`（只追加：get 與登錄冷卻的紀錄）。

  key 與 token 只在 key 檔，服務讀進記憶體，token 只存雜湊。
- `--check` 三態：0＝服務在跑、版本對、`GET /v1/subscriptions` 不帶 token 回 401；1＝確定不成立；2＝無法確認。

### 決定 4：認證：一類機器一把讀取 token

- 三類：`pool-worker`（池的所有 worker 共用）、`mac`、之後的 `mobile`。
- token 的值是 GitHub secret，由本人產生並設定，和 key 一起經 `deploy-keys` 放進那台的 key 檔，服務讀進來只存雜湊。換 token＝改 secret＋重新部署，舊的立即失效。
- 同一個 token 值也要放到用的那一邊：
  - `pool-worker` 的由 MyLinuxPool 像其他 `POOL_WORKER__*` 一樣掛進 worker（MyLinuxPool#70）；
  - Mac 的放 `~/.config/lgw/token`（0600）。
- **沒有發證、撤證的 API**，也沒有角色：持有 token 的就能 list、get、登錄冷卻。
- **為什麼**：本人要求最少的方式。worker 是拋棄式的，而且每台 worker 本來就拿得到同樣的五把 key，分開發證換不到多少安全性。
- **代價**：稽核紀錄只分得出類別（`pool-worker`、`mac`），分不出是哪一台 worker（proposal「要本人判斷的」第 3 點）。

### 決定 5：怎麼連到服務（照舊）

- Mac：沿用 `mlp` 的 SSH 跳板做本機埠轉送。
- worker：sidecar 一律連 `http://127.0.0.1:<固定埠>`。同一台走 docker bridge；別台經 MyLinuxPool 的隧道（MyLinuxPool#69）。
- 手機：之後由 MyAiEntry 的 App 經同一條 SSH 跳板做埠轉送（MyAiEntry#147）。
- 服務從不對外。

### 決定 6：三個介面

JSON over HTTP，`Authorization: Bearer <token>`。

| 方法 | 路徑 | 成功 | 錯誤 |
|---|---|---|---|
| GET | `/v1/subscriptions` | `200 {schema, subscriptions: [ … ]}`，依 `priority` 排序，不含 `key` | 401 |
| GET | `/v1/subscriptions/{id}?agent=claude-code\|opencode` | `200 { … , key}` | 400（缺 `agent`）、401、403 `agent_not_allowed`、404、409 `not_deployed` |
| POST | `/v1/subscriptions/{id}/cooldown` | `204`；body `{window: "5h"\|"1w", resets_at?}` | 400（窗口不對、時刻不合理）、401、404 |

- 時刻一律是 RFC 3339 UTC。
- 請求 body 上限 1 MB。
- 冷卻中的 subscription 仍可 get，`available` 為 false，由呼叫方決定。

### 決定 7：共通 JSON schema（和 aistorage-pm 一致）

```json
{
  "schema": 1,
  "id": "openrouter",
  "provider": "openrouter",
  "kind": "api-key",
  "agents": ["claude-code", "opencode"],
  "priority": 30,
  "models": {"allow": ["*:free"], "default": null},
  "endpoint": "https://openrouter.ai/api",
  "key": "（只在 get 回）",
  "cooldown": {"5h": null, "1w": {"until": "2026-10-18T00:02:00Z"}},
  "available": false
}
```

- `kind` 是 `claude-subscription` 或 `api-key`。
- Claude 訂閱的 `agents` 只有 `claude-code`；Zen 只有 `opencode`（沒有官方的 Claude Code 接法）。
- `priority` 是預設挑選順序。claude-code 是個人 10 → 公司 20；opencode 是 Zen 10 → OpenRouter 20（只允許免費模型）→ Ollama 30。這些定義在政策檔，不是介面。
- **不帶環境變數名**：由 sidecar 的 AgentDriver 依 `provider`、`kind`、`endpoint`、`key`、`models.default` 推出。例如 Claude 訂閱推出 `CLAUDE_CODE_OAUTH_TOKEN`，OpenRouter 給 claude-code 推出 `ANTHROPIC_BASE_URL`＋`ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`。理由是環境變數名跟著 agent 版本變，而 agent 和 sidecar 一起釘在映像裡。
- 內部仍分兩個介面（實作分離）：
  - `CredentialProvider`：每種 provider 一個實作，負責 schema 中與 provider 有關的欄位；
  - 政策檔：priority、models、agents。

### 決定 8：冷卻

- 窗口只有 `5h` 與 `1w`（Claude 的 five_hour／seven_day、Ollama 的 session／weekly，其他 provider 的限制也歸到這兩個窗口）。
- 有 `resets_at` 時冷卻到它加 2 分鐘；沒有時用窗口的預設長度（5 小時、7 天）。過去或 8 天以後的時刻拒收。同一窗口取較晚的結束時刻；到時自動解除。
- `available` = key 已部署，而且兩個窗口都沒在冷卻。

### 決定 9：換把由 sidecar 做（MyAiStorage v3）

- sidecar 的 AgentDriver 偵測到額度用完 → sidecar 登錄冷卻 → 從 list 挑 `available` 而且 `agents` 含自己種類、`priority` 最前的 → get → 停掉 agent（寫回 agora）→ 用新 key 重開並 continue 同一個 Session。
- 之後依 harness 的優先模型與底線挑，也在 sidecar 這一層。
- `lgw.signals`（只有帶額度標記的 402／429 才算用完）可以給 AgentDriver 當函式庫用。
- LLMGateway 不參與挑選，也不知道誰正在用哪一把。

### 決定 10：稽核紀錄

- 每次 get 與登錄冷卻記一筆，內容是時刻、token 類別、subscription id、agent（get）、窗口與結束時刻（冷卻）。不記 key。
- 這是「紀錄全留」在 v3 能做到的部分：看得到誰在何時拿過哪一把、誰登錄過冷卻；看不到現在還在用（沒有租約）。

### 決定 11：拿掉的與理由

| 拿掉的 | 理由 | 會失去的 |
|---|---|---|
| 租約五個動作、10 分鐘確認、去重 | 本人要求只有三個介面 | 「誰正在用」、逾期回收 |
| 同時租用上限 | 沒有租約就算不出「幾個在用」 | 本人先前的風險控管之一（proposal 第 1 點） |
| 管理 API（issuer）、whoami | 改成一類機器一把 token，部署時放好 | 分辨是哪一台 worker |
| 狀態板（JSON／HTML）、用量查詢 | list 已含冷卻與能不能用；用量 % 本人說沒有也行 | 用量 % |

impl branch 上已寫好的租約核心、管理 API、狀態板、用量查詢，等 v3 定案後移除或改寫（tasks 1.x）。部署腳本、安裝程式、維運手冊保留。

## Risks / Trade-offs

- [公司帳號用完換另一個是灰色地帶，v3 沒有同時上限] → 本人先前已接受灰色地帶；上限的取捨請本人確認（proposal 第 1 點）。冷卻中不硬撞仍成立：sidecar 只挑 `available` 的。
- [共用 token 外洩 = 所有 worker 能拿的 key 外洩] → 和現在 MyLinuxPool#50 每台 worker 都掛五把 key 的情況相同。換 token 只要改 secret 並重新部署。
- [sidecar 登錄冷卻依賴它的判讀] → 判讀錯了會讓某把閒置到重置時刻；稽核紀錄看得到是誰登錄的。
- [key 集中在一台] → 只綁內部介面、只經 SSH 進入、檔案 0600、輪替只要改 secret 並重新部署。

## Migration Plan

1. 本人確認 v3（含 proposal「要本人判斷的」三點）。
2. 本 repo：
   - 服務改成三個介面；
   - 移除租約、管理 API、狀態板、用量查詢；
   - `deploy-keys` 加上 token；
   - `lgw` 提供 list／get／登錄冷卻的客戶端。
3. 本人做一次性的開機設定（決定 3），設 environment 與 secret（tasks 3.3）。
4. MyLinuxPool：#68、#69 照舊，#70 改成掛 `pool-worker` 的 token。
5. sidecar v3 接上 list／get／登錄冷卻，在 Mac 與一台 worker 驗收換把。
6. 另開單把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key。

- **退回**：停服務；worker 在第 6 步之前仍有 MyLinuxPool#50 的 key。

## Open Questions

- opencode 讀 key 與 baseURL 的方式（例如 `OPENCODE_CONFIG_CONTENT`）：屬於 sidecar 的 AgentDriver，實作時在 opencode 1.18.35 上驗證。
