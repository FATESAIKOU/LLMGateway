## Context

動機與範圍見 proposal.md（v3）。影響做法的現況與限制：

- **Claude 條款**：訂閱的 OAuth 是給原版 Claude Code 這類 Anthropic 原生 app 的一般使用；第三方不得代為路由、收集、存放、中轉 Claude.ai 的 token（`docs/research/claude-failover.md` §7）。LLMGateway 不代轉請求，Claude 訂閱的 key 只給 `agent=claude-code` 的 get。
- **sidecar v3**（MyAiStorage，aistorage-pm）：一台機器一個 daemon（Mac 也能跑），對外五個操作加 HTTP 控制，不基於 herdr。它用 LLMGateway 的 list／get／登錄冷卻；挑把與換把在它內部；key 變成哪些環境變數由它的 AgentDriver 決定。
- **服務的位置**（本人裁定）：本人選定的常駐 provider，LLMGateway 跟 MyLinuxPool 無關，用自己的帳號 `llmgw` 跑。
- **v4**（本人對 v3 的第 5～7 點）：
  - 部署與啟動不依賴 MyLinuxPool，只依賴「一台連得到的 Linux 機器＋SSH」；
  - 伺服器端自己用 ssh reverse tunnel 公開埠並維持健康；
  - LLMGateway 提供客戶端連線程式，支援跳板。
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

### 決定 2：只依賴「一台連得到的 Linux 機器＋SSH」（v4，本人第 6 點）

- **會合機（rendezvous）**：一台大家都連得到的 Linux 機器，只要有 sshd。用設定指定主機、埠、帳號；**不依賴 MyLinuxPool**（不用 pool-tunnel、NODE_*、池的帳號）。
  - 候選是池的跳板機，但只把它當「一台連得到的機器」。要注意它可能整台重建，重建後要重做會合機的開機設定（見 Risks）。
- 在會合機上**開一個專用帳號**（暫名 `llmgw-relay`），authorized_keys 由 LLMGateway 的開機設定放、不交給池管：
  - 服務那台的隧道金鑰：`restrict,port-forwarding,permitlisten="127.0.0.1:<API 埠>",permitlisten="127.0.0.1:<SSH 埠>",permitopen="127.0.0.1:<API 埠>"`。permitopen 給健康探針用。
  - 客戶端金鑰（worker、Mac、之後手機，一類一把）：`restrict,port-forwarding,permitopen="127.0.0.1:<API 埠>"`。
  - 部署金鑰：`restrict,port-forwarding,permitopen="127.0.0.1:<SSH 埠>"`。
  - 這些金鑰都不能開 shell，只能做寫明的那幾種轉發。
- **服務本身只綁那台的 127.0.0.1**。不再綁 docker bridge，同一台的容器也走會合機。

### 決定 3：伺服器端：隧道守護與健康維持（v4）

- 服務那台跑兩個 systemd user unit（帳號 `llmgw`）：
  - `llmgateway.service`：lgwd，只綁 127.0.0.1:<本機 API 埠>。
  - `llmgateway-tunnel.service`：用自己的連線 `ssh -N -R 127.0.0.1:<API 埠>:127.0.0.1:<本機 API 埠> -R 127.0.0.1:<SSH 埠>:127.0.0.1:22 llmgw-relay@<會合機>`，帶 `ExitOnForwardFailure=yes`、`ServerAliveInterval=30`、`ServerAliveCountMax=3`。斷線就結束，由 systemd 立即重啟（`Restart=always`）。**不和池的 pool-tunnel 共用連線**。
- **健康探針**：只證明 TCP 連著不夠（舊的 listener 可能還在、或被別人佔走），所以探針做端對端的檢查。
  - 每 60 秒由守護程式經會合機 `ssh -W 127.0.0.1:<API 埠>` 打一次 `GET /v1/subscriptions`，收到任何 HTTP 回應（200 或 401）才算健康。
  - 連續 3 次失敗就殺掉隧道連線，讓 systemd 重開。
  - 這個檢查不需要新的介面。
- 守護程式的設定在 `~/.config/llmgateway/tunnel.env`：會合機（`user@host:port`）、金鑰路徑、known_hosts 路徑、會合機上的 API 埠與 SSH 埠、探針間隔。
- `--check` 三態加上隧道：0＝兩個 unit 都在跑、版本對、經會合機探得到；1＝確定不成立；2＝無法確認。
- 舊的 `-R` 還沒被會合機清掉時，新的會因 `ExitOnForwardFailure` 失敗，systemd 會一直重試，直到舊的逾時被清掉（會合機的 `ClientAliveInterval`）。

### 決定 4：客戶端：連線程式 `lgw connect`（v4，本人第 7 點）

- `lgw` 提供連線程式，函式庫與指令兩種用法。sidecar 的 SubscriptionSource 用函式庫。
  - 函式庫：`with lgw.connect(cfg) as base_url:`，在本機開一個轉發，回 `http://127.0.0.1:<臨時埠>`；離開時關掉。
  - 指令：`lgw connect`，前景維持轉發並印出 base URL，給 Mac 與手動除錯用。
- 做法：`ssh -N -L 127.0.0.1:<臨時埠>:127.0.0.1:<API 埠> [-J <跳板1>,<跳板2>] llmgw-relay@<會合機>`，帶 `ExitOnForwardFailure=yes`、`BatchMode=yes`、`StrictHostKeyChecking=yes`。連不上或中途斷掉就回「無法確認」，由呼叫方等一下再試。
- 設定（`~/.config/lgw/connect.env`，或 sidecar 設定裡的同名欄位）：
  - `jumps`：零到多個 `user@host:port`；
  - `rendezvous`：`llmgw-relay@host:port`；
  - `identity`：這一類機器的客戶端金鑰路徑；
  - `known_hosts`：會合機與跳板的 host key；
  - `remote_api`：會合機上的 API 埠；
  - `token`：讀取 token 的路徑，只有選 (b) 時才有。
- worker 與 Mac 都直接連會合機的公開 SSH 埠；需要跳板的環境用 `jumps`。

### 決定 5：部署：也走會合機（v4）

- 本 repo 的 workflow 改成 `ssh -J llmgw-relay@<會合機> -p <SSH 埠> llmgw@127.0.0.1`：
  - 第一段是會合機上的部署金鑰，只能轉發到 SSH 埠；
  - 第二段是服務那台 `llmgw` 的 authorized_keys，以 `command="…/current/deploy/remote-deploy.sh",restrict` 強制執行部署腳本，動詞 `keys`／`upgrade <tag>`，照舊。
- 不再經池的跳板，MyLinuxPool#68 不需要。
- 一次性開機設定（root，本人做或本人授權）：
  - 會合機：建 `llmgw-relay`、放三種金鑰；
  - 服務那台：建 `llmgw`（不加 docker 群組）、linger、放部署公鑰、第一次安裝；
  - 兩邊確認 sshd 不擋這些帳號。

### 決定 5a：認證，兩案給本人選（本人第 5 點）

| | (a) 去掉應用層認證，安全只靠 SSH | (b) 留一把共用 token（**建議**） |
|---|---|---|
| 客戶端要帶的 | 會合機的客戶端金鑰 | 會合機的客戶端金鑰＋共用 token |
| 誰連得到 API 埠 | 會合機上**所有**能開 TCP 轉發的帳號。若會合機是池的跳板機，包括 sshproxy（所有 provider、worker 的隧道金鑰，以及家人維修機共用的那把）與管理帳號，它們都**沒有** permitopen 限制，都讀得到全部的 key | 同左，但沒有 token 就只回 401 |
| 要怎麼收窄才安全 | 給會合機上其他能轉發的帳號加 permitopen（池的跳板機上，就是改 MyLinuxPool 的 sshproxy，又回到依賴 MyLinuxPool）；或改用 unix socket 轉發，並調整會合機 sshd 的 `StreamLocalBindUnlink`；或選一台沒有其他人能轉發的會合機 | 不需要；會合機上有誰都不影響 |
| 換憑證 | 換客戶端金鑰（改會合機的 authorized_keys） | 換 token（改 secret＋重新部署，並更新客戶端） |

- **建議 (b)**：多一把 token，換來「會合機上還有誰」都不影響安全，也不用動 MyLinuxPool。(a) 只有在會合機是一台只給 LLMGateway 用的機器時才划算。
- token 只有一把，由本人產生，和 key 一起經 `deploy-keys` 放到服務那台（服務只存雜湊），同一個值也放到各客戶端。

### 決定 5b：key 的保管與服務的形狀（照舊）

- **key 的來源**：本 repo 的 GitHub secret，值由本人設，名稱照本人的命名規則（認證方式定了再定 token 的名稱）。
- **部署腳本**（`remote-deploy.sh`，從 `SSH_ORIGINAL_COMMAND` 以白名單解析）：
  - `keys`：stdin → 驗證 → 暫存檔 0600 → rename `keys.env` → 重讀。選 (b) 時 stdin 也帶 token。
  - `upgrade <tag>`：固定 tag 取碼、current 原子切換、重啟。
  - 值用 stdin 傳、不上命令列；workflow 的 log 只有 secret 名稱；ssh 的錯誤輸出會遮掉位址。
- **服務**：
  - Python 標準函式庫，HTTP 用 `ThreadingHTTPServer`、資料用 `sqlite3`，沒有第三方套件；
  - 帳號 `llmgw` 的 systemd user unit（linger），`llmgw` 不加 docker 群組；
  - 安裝、unit、run wrapper、版本、`--check` 都在本 repo 的 `deploy/`；
  - SQLite 只有 `cooldowns`（subscription × 窗口 → 結束時刻）與 `audit`（只追加）；
  - key 與 token 只在 key 檔，token 只存雜湊。

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
| 管理 API（issuer）、whoami | 改成一把共用 token（或只靠 SSH，見決定 5a），部署時放好 | 分辨是哪一台 worker |
| 狀態板（JSON／HTML）、用量查詢 | list 已含冷卻與能不能用；用量 % 本人說沒有也行 | 用量 % |

impl branch 上已寫好的租約核心、管理 API、狀態板、用量查詢，等 v3 定案後移除或改寫（tasks 1.x）。部署腳本、安裝程式、維運手冊保留。

## Risks / Trade-offs

- [公司帳號用完換另一個是灰色地帶，v3 沒有同時上限] → 本人先前已接受灰色地帶；上限的取捨請本人確認（proposal 第 1 點）。冷卻中不硬撞仍成立：sidecar 只挑 `available` 的。
- [共用 token 外洩 = 所有客戶端能拿的 key 外洩] → 和現在 MyLinuxPool#50 每台 worker 都掛五把 key 的情況相同；而且還要有會合機的客戶端金鑰才連得到。換 token 只要改 secret 並重新部署。
- [會合機若是池的跳板機，可能整台重建] → 重建後 `llmgw-relay` 帳號會不見，要重做會合機的開機設定；隧道守護會一直重試，`--check` 回 1，看得出來。要完全不受影響，就選一台不會被重建的機器當會合機。
- [舊的 `-R` listener 沒被清掉] → `ExitOnForwardFailure` 讓守護程式重試，直到會合機清掉舊連線；端對端探針確保不是「TCP 通、服務不通」。
- [sidecar 登錄冷卻依賴它的判讀] → 判讀錯了會讓某把閒置到重置時刻；稽核紀錄看得到是誰登錄的。
- [key 集中在一台] → 只綁內部介面、只經 SSH 進入、檔案 0600、輪替只要改 secret 並重新部署。

## Migration Plan

1. 本人確認 v3（含 proposal「要本人判斷的」三點）。
2. 本 repo：
   - 服務改成三個介面；
   - 移除租約、管理 API、狀態板、用量查詢；
   - `deploy-keys` 加上 token；
   - `lgw` 提供 list／get／登錄冷卻的客戶端。
3. 本人做一次性的開機設定（決定 5：會合機與服務那台），設 environment 與 secret（tasks 3.3）。
4. MyLinuxPool：#68、#69 不需要（部署與連線都走會合機）；#70 縮成把客戶端憑證（會合機的客戶端金鑰；選 (b) 時再加 token）掛進 worker。由 PMO 協調。
5. sidecar v3 接上 list／get／登錄冷卻，在 Mac 與一台 worker 驗收換把。
6. 另開單把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key。

- **退回**：停服務；worker 在第 6 步之前仍有 MyLinuxPool#50 的 key。

## Open Questions

- opencode 讀 key 與 baseURL 的方式（例如 `OPENCODE_CONFIG_CONTENT`）：屬於 sidecar 的 AgentDriver，實作時在 opencode 1.18.35 上驗證。
