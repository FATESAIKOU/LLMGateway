## Context

動機與範圍見 proposal.md（v4 定案）。影響做法的現況與限制：

- **Claude 條款**：訂閱的 OAuth 是給原版 Claude Code 這類 Anthropic 原生 app 的一般使用；第三方不得代為路由、收集、存放、中轉 Claude.ai 的 token（`docs/research/claude-failover.md` §7）。LLMGateway 不代轉請求，Claude 訂閱的 key 只給 `agent=claude-code` 的 get。
- **sidecar v4**（MyAiStorage，aistorage-pm）：一台機器一個 daemon（Mac 也能跑），對外四個動作（open／close／status／poke）加 HTTP 控制，不基於 herdr。它用 LLMGateway 的 list／get／登錄冷卻；挑把與換把在它內部；key 變成哪些環境變數由它的 AgentDriver 決定。
- **服務的位置**（本人裁定）：本人選定的常駐 provider，LLMGateway 跟 MyLinuxPool 無關，用自己的帳號 `llmgw` 跑。
- **v4（本人定案）**（本人對 v3 的第 5～7 點與後續的裁定）：
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

### 決定 2：會合點＝池跳板機上現有的 sshproxy 帳號（v4 定案）

- LLMGateway 只依賴「一台連得到的 Linux 機器＋SSH」，用設定指定。本人指定用池跳板機上**現有的 sshproxy 帳號**（「直接用 sshproxy 吧」），不另開帳號。
  - LLMGateway 不用 pool-tunnel、NODE_*，只是把 sshproxy 當成一個會合點。
  - 跳板機的 `GatewayPorts` 是 no，`-R` 只綁 loopback，正好符合需要。
- **遠端埠**：避開 2220–2499（池的隧道）與 3000–3999（公開服務預留）。預設 API 埠 4710、SSH 埠 4711，用設定改。MyLinuxPool 會把它保留成 `NODE_GATEWAY.ports.llmgateway: [4710, 4711]`（只保留，不配發）。
- **金鑰的登記**（mylinuxpool-pm 提的最小做法）：MyLinuxPool 新增 repo var `TUNNEL_EXTRA_PUBKEYS`（JSON：名字 → 一行公鑰），由 sshproxy 的組裝一起收進去（refresh 與 rotate 共用同一個函式，所以重建後也還在）。第一版只放裸公鑰；帶選項的收窄另開 MyLinuxPool 的 d 單，不擋第一版。var 由本人或 PMO 設，設好後 dispatch refresh-authorized-keys 套用。兩把是：
  - 服務那台的隧道金鑰：在 `llmgw` 帳號產生，私鑰不離開那台，只登記公鑰；
  - 部署金鑰：私鑰是本 repo 的 secret，登記公鑰。
  - 能帶選項就收窄：隧道金鑰只准 `-R` 那兩個埠並探 API 埠，部署金鑰只准轉發到 SSH 埠。不能帶也可以，本人已接受 sshproxy 不收窄。
- **服務本身只綁那台的 127.0.0.1**，不綁 docker bridge；同一台的容器也走會合點。
- **附註**：
  - sshproxy 要登記兩把公鑰（隧道與部署）。
  - 4711 會讓服務那台的 sshd 出現在跳板機的 loopback 上，但要進去仍然得通過 `llmgw` 帳號的 SSH 認證，而且只會跑 forced command 的部署腳本（`keys`／`upgrade`）。

### 決定 3：伺服器端：隧道守護與健康維持

- 服務那台跑兩個 systemd user unit（帳號 `llmgw`）：
  - `llmgateway.service`：lgwd，只綁 127.0.0.1:<本機 API 埠>。
  - `llmgateway-tunnel.service`：用**自己的金鑰與自己的連線** `ssh -N -R 127.0.0.1:4710:127.0.0.1:<本機 API 埠> -R 127.0.0.1:4711:127.0.0.1:22 sshproxy@<跳板機>`，帶 `ExitOnForwardFailure=yes`、`ServerAliveInterval=30`、`ServerAliveCountMax=3`。斷線就結束，由 systemd 立即重啟（`Restart=always`）。**不和池的 pool-tunnel 共用連線**。
- **健康探針**：只證明 TCP 連著不夠（舊的 listener 可能還在、或被別人佔走），所以探針做端對端的檢查。
  - 每 60 秒經跳板機 `ssh -W 127.0.0.1:4710` 打一次 `GET /v1/subscriptions`，收到 HTTP 200 才算健康。
  - 連續 3 次失敗就殺掉隧道連線，讓 systemd 重開。
  - 這個檢查不需要新的介面。
- 設定 `~/.config/llmgateway/tunnel.env`：會合點（`sshproxy@host:port`）、金鑰路徑、known_hosts 路徑、兩個遠端埠、探針間隔。
- `--check` 三態加上隧道：0＝兩個 unit 都在跑、版本對、經會合點探得到；1＝確定不成立；2＝無法確認。
- 舊的 `-R` 還沒被清掉時，新的會因 `ExitOnForwardFailure` 失敗，systemd 會一直重試，直到舊的逾時被清掉。

### 決定 4：客戶端：連線程式 `lgw connect`

- `lgw` 提供連線程式，函式庫與指令兩種用法。sidecar 的 SubscriptionSource 用函式庫。
  - 函式庫：`with lgw.connect(cfg) as base_url:`，在本機開一個轉發，回 `http://127.0.0.1:<臨時埠>`；離開時關掉。
  - 指令：`lgw connect`，前景維持轉發並印出 base URL。
- 做法：`ssh -F <自己產生的設定> -N -L 127.0.0.1:<臨時埠>:127.0.0.1:4710 [-J <跳板…>] <帳號>@<會合點> -p <埠>`，設定裡有 `StrictHostKeyChecking yes`、`BatchMode yes`、`ExitOnForwardFailure yes`。連不上或中途斷掉就回「無法確認」，由呼叫方等一下再試。
  - 用 `-F` 而不是命令列的 `-i`／`-o`：OpenSSH 用 `-J` 時，命令列上的 `-i`／`-o` 不會套到跳板那一段。所以所有跳板與會合點共用同一把 identity 與同一份 known_hosts。
  - 自己的設定檔意味著**不讀 `~/.ssh/config`**：別名、`Host` 區塊裡的帳號與埠都沒用，`rendezvous` 與 `jumps` 必須寫成明確的 `user@host:port`。帳號由設定指定，不固定是 `sshproxy`。
- **設定檔是 JSON**（慣例叫 `connect.json`，格式的唯一定義在 `lgw/connect.py` 的模組說明）。`lgw.connect.load_config(path)` 每次呼叫都重讀檔案、不快取；sidecar 每次操作才 connect，所以設定檔被改寫後，下一次操作自然用新的。欄位就是這些，多一個都不行：
  - `schema`：必填，只能是 `1`；
  - `rendezvous`：必填，`user@host:port`；
  - `jumps`：選填，字串陣列，每個 `user@host:port`，照順序，預設空；
  - `identity`：必填，私鑰路徑；
  - `known_hosts`（檔案路徑）與 `host_keys`（known_hosts 格式的一行一字串，陣列）：**恰給一個**；`host_keys` 讓設定檔自成一體，連線時寫進私有暫存目錄的 0600 檔；
  - `remote_api`：選填，預設 4710。
  - host key 的主機寫法照 known_hosts：埠不是 22 時是 `[host]:port`，是 22 時只寫 `host`。
- **設定檔從哪來**：sidecar 不讀 MyLinuxPool 的任何檔，只讀這份 JSON，`lgw` 也一樣，所以兩者都不依賴 MyLinuxPool。
  - worker：由 MyLinuxPool 的一個 unit 依 `/run/mlp-gateway/gateway.json`（`ip`、`tunnel_user`、`ssh_port`、`host_key` 等）產生這份 JSON，rotate 後重新發布時整檔原子改寫（所以用 `host_keys` 自成一體），sidecar 下次操作就讀到新的。host key 釘在這份設定裡，不和 pool-tunnel 共用，也不用 accept-new。
  - Mac：手寫。
- 用哪把金鑰：
  - worker 用 **pool-tunnel 既有的 sshproxy 金鑰** `/home/worker/.ssh/id_tunnel`（設定的 `identity`）。擁有者 worker、權限 0600；sidecar 也以 worker 身分跑，讀得到（mylinuxpool-pm 確認）。
  - Mac 用它自己能登入 sshproxy 的金鑰。
  - LLMGateway 不另外發金鑰給客戶端。

### 決定 5：部署：也走會合點

- 本 repo 的 workflow：`ssh -J sshproxy@<跳板機> -p 4711 llmgw@127.0.0.1`。
  - 第一段用部署金鑰登入 sshproxy，只做轉發；
  - 第二段由服務那台 `llmgw` 的 authorized_keys 以 `command="…/current/deploy/remote-deploy.sh",restrict` 強制執行部署腳本，動詞 `keys`／`upgrade <tag>`，照舊。
- 一次性開機設定（root，本人做或本人授權）：
  - 服務那台：建 `llmgw`（不加 docker 群組）、linger、產生隧道金鑰並把公鑰交給 MyLinuxPool 登記、放部署公鑰、第一次安裝；
  - 跳板機：由 MyLinuxPool 登記兩把公鑰（決定 2）。

### 決定 5a：認證：不做應用層認證，只靠 SSH（本人選 (a)）

- 本人：「沒事，我選 a，反正洩漏我換 key 很簡單」。
- **本人已接受的風險**：sshproxy 上的所有金鑰都連得到 API 埠，也就讀得到全部五把 key。這包括所有 provider、worker 的隧道金鑰，以及家人維修機共用的那把。
- **發現外洩時**：在 LLMGateway 的 secret 換掉五把 key，重新部署。換 key 不需要動任何客戶端，因為客戶端不持有 key。（v5：改更新加密檔等一次拉取，見下「決定 5／5b 的取代」）
- **客戶端裝置遺失時**（例如手機）：裝置上有 sshproxy 的金鑰就等於 key 已外洩。先請 MyLinuxPool 把那把金鑰從 sshproxy 撤掉，再換掉五把 key 並重新部署；撤銷之前那把金鑰仍然讀得到。（v5：後半改更新加密檔等一次拉取）
- 稽核紀錄只看得到「從會合點來的連線」，分不出是哪一把 SSH 金鑰（本人接受，v0 不分辨是哪台 worker）。

### 決定 5b：key 的保管與服務的形狀

- **key 的來源**：本 repo 的 GitHub secret，五把，值由本人設，名稱照本人的命名規則（tasks 3.4）。
- **部署腳本**（`remote-deploy.sh`，從 `SSH_ORIGINAL_COMMAND` 以白名單解析）：
  - `keys`：stdin → 驗證 → 暫存檔 0600 → rename `keys.env` → 重讀。
  - `upgrade <tag>`：固定 tag 取碼、current 原子切換、重啟。
  - 值用 stdin 傳、不上命令列；workflow 的 log 只有 secret 名稱；ssh 的錯誤輸出會遮掉位址。
- **服務**：
  - Python 標準函式庫，HTTP 用 `ThreadingHTTPServer`、資料用 `sqlite3`，沒有第三方套件；
  - 帳號 `llmgw` 的 systemd user unit（linger），`llmgw` 不加 docker 群組；
  - 安裝、unit、run wrapper、版本、`--check` 都在本 repo 的 `deploy/`；
  - SQLite 只有 `cooldowns`（subscription × 窗口 → 結束時刻）與 `audit`（只追加）；
  - key 只在 key 檔。

### 決定 6：三個介面

JSON over HTTP，沒有應用層認證（決定 5a）。

| 方法 | 路徑 | 成功 | 錯誤 |
|---|---|---|---|
| GET | `/v1/subscriptions` | `200 {schema, subscriptions: [ … ]}`，依 `priority` 排序，不含 `key` | — |
| GET | `/v1/subscriptions/{id}?agent=claude-code\|opencode` | `200 { … , key}` | 400 `bad_request`（缺 `agent`，帶 `missing`）、403 `agent_not_allowed`、404 `not_found`、409 `not_deployed` |
| POST | `/v1/subscriptions/{id}/cooldown` | `204`；body `{window: "5h"\|"1w", resets_at?}` | 400（`bad_request`、`invalid_window`、`invalid_resets_at`、`resets_at_out_of_range`）、404 `not_found`、413 `body_too_large` |

- 時刻一律是 RFC 3339 UTC。
- 請求 body 上限 1 MB。
- 冷卻中的 subscription 仍可 get，`available` 為 false，由呼叫方決定。

**錯誤碼表**（實作在 `lgwd/errors.py`）：回覆一律是 `{"error": "<碼>", …}`，訊息是固定字串，不回顯請求裡的值。

| 狀態 | 碼 | 什麼時候 |
|---|---|---|
| 400 | `bad_request` | JSON 解析失敗、body 不是物件、缺必填（缺的欄位名放在 `"missing": [...]`，get 缺 `agent` 也是這個） |
| 400 | `invalid_window` | `window` 不是 `"5h"` 或 `"1w"` |
| 400 | `invalid_resets_at` | `resets_at` 不是 RFC 3339 字串（或型別不對） |
| 400 | `resets_at_out_of_range` | `resets_at` 在過去，或超過 8 天以後 |
| 403 | `agent_not_allowed` | `agent` 不在該筆的 `agents` 裡 |
| 404 | `not_found` | 不存在的 id 或未知路徑（路徑對、方法不對也是 404） |
| 409 | `not_deployed` | 該筆的 key 尚未部署 |
| 413 | `body_too_large` | body 超過 1 MB |
| 500 | `internal` | 伺服器內部非預期錯誤（只記例外種類到 log） |

客戶端（`lgw.client` 的 `Result.error`）照實回上面的碼，另外有兩個**客戶端自己判讀**的值：`unreachable`（連不上或逾時，`Result.unreachable` 為 true，不丟例外）與 `bad_response`（回覆不是 JSON 物件）。呼叫方遇到 `unreachable` 要當成「無法確認」。

### 決定 7：共通 JSON schema（和 aistorage-pm 一致）

```json
{
  "schema": 1,
  "id": "openrouter",
  "provider": "openrouter",
  "kind": "api-key",
  "agents": ["opencode"],
  "priority": 20,
  "models": {"allow": ["*:free", "openrouter/free"], "default": "openrouter/free"},
  "endpoint": "https://openrouter.ai/api",
  "key": "（只在 get 回）",
  "cooldown": {"until": "2026-10-18T00:02:00Z"},
  "available": false
}
```

- `kind` 是 `claude-subscription` 或 `api-key`。
- Claude 訂閱的 `agents` 只有 `claude-code`；Zen 只有 `opencode`（沒有官方的 Claude Code 接法）；openrouter 只有 `opencode`（Claude Code 只用 Claude 訂閱與 Ollama，免費模型只經過 opencode 使用；Claude Code 碰到 `openrouter/free` 會 unrecognized_model）。
- `priority` 是預設挑選順序。本人說之後可能改成外部注入的函式計算；挑選本來就在 sidecar（可替換的小介面），LLMGateway 照樣回這個欄位。claude-code 是個人 10 → 公司 20；opencode 是 Zen 10 → OpenRouter 20（只允許免費模型）→ Ollama 30。這些定義在政策檔，不是介面。
- **不帶環境變數名**：由 sidecar 的 AgentDriver 依 `provider`、`kind`、`endpoint`、`key`、`models.default` 推出。例如 Claude 訂閱推出 `CLAUDE_CODE_OAUTH_TOKEN`，Ollama Cloud 給 claude-code 推出 `ANTHROPIC_BASE_URL`＋`ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`（OpenRouter 只給 opencode，見上）。理由是環境變數名跟著 agent 版本變，而 agent 和 sidecar 一起釘在映像裡。
- `models.default` 是**供應商自己的模型 id**，也就是 provider 端 API 認得的名字（例 `openrouter/free`、`muse-spark-1.3-contributor-free`、`deepseek-v4.1-flash`），不帶 agent 專用的 provider 前綴；要組成 agent 專用的字串（例如 opencode 的 `provider/model`）時，由呼叫方的 agent driver 負責。非 null 的 `default` 必須被同一筆 `allow` 的樣式涵蓋，載入政策檔時 fail-closed 檢查、蓋不進就拒收（`PolicyError`）。為 null 時：`kind` 是 `claude-subscription` 表示用 agent 自己的預設（呼叫方不帶模型參數），其他 `kind` 表示沒有預設，服務不代選，呼叫方自己依 `allow` 選或拒絕。
- 內部仍分兩個介面（實作分離）：
  - `CredentialProvider`：每種 provider 一個實作，負責 schema 中與 provider 有關的欄位；
  - 政策檔：priority、models、agents。

### 決定 8：冷卻

- 窗口只有 `5h` 與 `1w`（Claude 的 five_hour／seven_day、Ollama 的 session／weekly，其他 provider 的限制也歸到這兩個窗口）。
- 有 `resets_at` 時冷卻到它加 2 分鐘；沒有時用窗口的預設長度（5 小時、7 天）。過去或 8 天以後的時刻拒收。同一窗口取較晚的結束時刻；到時自動解除。
- `available` = key 已部署，而且兩個窗口都沒在冷卻。

### 決定 9：換把由 sidecar 做（MyAiStorage v4）

- sidecar 的 AgentDriver 偵測到額度用完 → sidecar 登錄冷卻 → 從 list 挑 `available` 而且 `agents` 含自己種類、`priority` 最前的 → get → 停掉 agent（寫回 agora）→ 用新 key 重開並 continue 同一個 Session。
- 之後依 harness 的優先模型與底線挑，也在 sidecar 這一層。
- `lgw.signals`（只有帶額度標記的 402／429 才算用完）可以給 AgentDriver 當函式庫用。
- LLMGateway 不參與挑選，也不知道誰正在用哪一把。

### 決定 10：稽核紀錄

- 每次 get 與登錄冷卻記一筆，內容是時刻、subscription id、agent（get）、窗口與結束時刻（冷卻）。不記 key。
- 這是「紀錄全留」在 v4 能做到的部分：看得到誰在何時拿過哪一把、誰登錄過冷卻；看不到現在還在用（沒有租約）。

### 決定 11：拿掉的與理由

| 拿掉的 | 理由 | 會失去的 |
|---|---|---|
| 租約五個動作、10 分鐘確認、去重 | 本人要求只有三個介面 | 「誰正在用」、逾期回收 |
| 同時租用上限 | 沒有租約就算不出「幾個在用」；本人定案 v0 不做 | 本人先前提過的風險控管之一 |
| 管理 API（issuer）、whoami、應用層 token | 本人選 (a)：只靠 SSH | 分辨是哪一台 worker |
| 狀態板（JSON／HTML）、用量查詢 | list 已含冷卻與能不能用；用量 % 本人說沒有也行 | 用量 % |

impl branch 上已寫好的租約核心、管理 API、狀態板、用量查詢，照 v4 移除或改寫（tasks 1.x）。部署腳本、安裝程式、維運手冊保留。

## v5 變更（本人，2026-10-12；以 v4 為底，只列改的地方）

### 決定 4 的取代：不提供客戶端連線程式

- 拿掉 `lgw connect`（函式庫＋指令）與 `connect.json` 整套格式。sidecar 直接 curl 本機的 port。
- LLMGateway 對通道只提一個需求：sidecar 所在機器本機 loopback 上的某個 port，必須是會合點 API 埠的 HTTP 轉發（純 HTTP、無認證）。本機 port 預設 4710，建通道的一方可經設定改（worker 由 MyLinuxPool 的 capability script 建到這個 port）。注意這和會合點上的 4710 同號純屬巧合（不同機器），兩邊各自設定。
- worker：由 MyLinuxPool 的 LLMGateway capability 附建通道小 script（那是 MyLinuxPool 的工作）；Mac／手機：用 mlp 或 ssh -J 自己轉發。
- `lgw` 剩 `client`（list／get／登錄冷卻，base URL 由呼叫方給）與 `signals`（判讀結果轉 `{until}` 用）。

### 決定 3 的補充：隧道守護搬到 ops-scripts/

- `deploy/llmgateway-tunnel` 與 `deploy/llmgateway-tunnel.service` 搬到 `ops-scripts/`（維運工具定位），內容不變；`install.sh`、`bootstrap.sh` 改從新路徑裝。決定 3 的行為（探針、重啟語意）不變。

### 決定 5／5b 的取代：金鑰自己拉（本人第二輪，2026-10-12：`gh` 方式；升級選乙見下）

- 正本：private repo `FATESAIKOU/LLMGateway-keys` 根目錄 `keys.env.enc`（只有本人用；`openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt`，密碼經 `-pass env:`／`fd:` 傳，不上命令列）。
- 使用前提：服務那台以 `llmgw` 身分登入過 `gh`（`gh` 的登入跟著 Unix 使用者走；本人 `sudo -u llmgw -i` 後 `gh auth login`，token 存在 `~llmgw/.config/gh/` 0600）。LLMGateway 本身不做登入引導。
- 認證方式本人已定（2026-10-12，選 B）：`llmgw` 用瀏覽器 `gh auth login` 登入，不用 `--with-token` 的 token 檔；憑證由 `gh` 自己管在 `~llmgw/.config/gh/`（0600），LLMGateway 不碰、不複製、不轉發其值。
- 權限風險（**本人已知並接受**）：`gh` 的登入狀態對應本人帳號的一般權限，拿著這台登入狀態的人可能讀寫本人其他 repo，因此這台不能當成完全隔離的機器。相應地 `install.sh --check` 除問「登入了沒」，還要實際驗一次讀得到 `FATESAIKOU/LLMGateway-keys`（`gh api repos/… --jq .full_name`，只問 repo 本身、不讀檔案內容）；讀不到即判不能用。見 v5-plan §2。
- 拉取：key-sync（放 `ops-scripts/` 的小 script＋systemd timer，約每 10 分鐘＋可手動觸發）跑 `gh api repos/FATESAIKOU/LLMGateway-keys/contents/keys.env.enc`，回傳的 blob `sha` 即版本（一樣就不動；不用 clone——免在本機留 mirror 少管一份狀態）：base64 解碼→openssl 解密→驗證格式→原子替換 `keys.env`→`lgwd` 依 mtime 重讀；失敗保留舊版、記 log。`gh` 沒登入或 token 失效同失敗處理；`install.sh --check` 看得出「gh 不能用」。
- 拿掉（乙）：`deploy-keys`／`deploy-service` 兩個 workflow、`remote-deploy.sh` 整檔、SIGHUP 重讀（`llmgateway.service` 的 `ExecReload` 一併拿掉）、bootstrap 的部署公鑰／forced command 步驟（`--deploy-pubkey-file`、`step_deploy_key`）、部署連線 4 secret（`GH_LLMGATEWAY__POOL_SSH__DEPLOY*`）、MyLinuxPool 那側的部署公鑰登記（隧道公鑰保留）。
- 服務程式本身怎麼升級（乙）：管理帳號手動 ssh 上去跑 `install.sh --tag`（手冊步驟，不寫機器名），前後各跑一次 `--check`。
- 初次啟動：先以 `llmgw` 身分 `gh auth login`（選 B：用瀏覽器登入，不放 token 檔，說明見 v5-plan §2）＋放好解密密碼（`keysync.env`，見 v5-plan），再手動跑一次 key-sync 拉下首版，`--check` 才會過。bootstrap 有一個 `gh-auth` 步驟只查登入狀態（沒登入記 todo、gh 沒裝記 fail），登入本身由本人做；LLMGateway 不做登入引導。

### 決定 7／8 的取代：冷卻 until-only

- 登錄冷卻的 body 改 `{until}`（必填，RFC 3339 UTC）：缺 → 400 `bad_request`（`missing: ["until"]`）；不是 RFC 3339 → 400 `invalid_until`；在過去或超過 8 天以後 → 400 `until_out_of_range`；未知欄位忽略。
- 同一 subscription 重複登錄取較晚的 `until`；過了自動解除。`cooldown` 欄位改 `{"until": …｜null}`；`available`＝已部署且沒有未來的 `until`。
- 政策檔：拿掉 `cooldown_buffer_seconds`，`resets_at_max_ahead_seconds` 更名（until 語意，值不變）。DB：`cooldowns` 剩 subscription → until，稽核拿掉 `window`。
- sidecar 拿不到恢復時間時報「現在＋1 小時」（sidecar 的行為；`lgw.signals` 只把判讀結果轉成 `{until}`）。

### 說法（決定 2 相關）

- sshproxy 是既有設施（MyLinuxPool 維運，LLMGateway 只當會合點用）；sidecar 內部的 SubscriptionSource 是 sidecar 的事，diagrams 只寫「sidecar 內部自理」。

### v5 新增的風險

- [key-sync 拉失敗] → 保留舊版、記 log，服務照常用舊 key；連續失敗才需要人看。
- [private repo 權限外洩] → 加密檔本身還要解密密碼才打得開；解密密碼只在服務那台（0600），不進 repo。
- [拿掉 `lgw connect` 後通道各搞各的] → LLMGateway 只認「本機 loopback 某 port 即 HTTP API」；通道斷了客戶端照舊回「無法確認」。

## Risks / Trade-offs

- [公司帳號用完換另一個是灰色地帶，v0 沒有同時上限] → 本人已接受灰色地帶，也定案 v0 不做上限。冷卻中不硬撞仍成立：sidecar 只挑 `available` 的。
- [sshproxy 上任何金鑰都讀得到全部 key] → 本人已接受（決定 5a）；外洩時換五把 key 並重新部署，客戶端不用動。
- [跳板機整台重建] → sshproxy 會由 MyLinuxPool 重建，兩把公鑰只要在 MyLinuxPool 的登記裡就會回來；重建期間隧道守護會一直重試，`--check` 回 1。
- [舊的 `-R` listener 沒被清掉] → `ExitOnForwardFailure` 讓守護程式重試，直到會合點清掉舊連線；端對端探針確保不是「TCP 通、服務不通」。
- [sidecar 登錄冷卻依賴它的判讀] → 判讀錯了會讓某把閒置到重置時刻；稽核紀錄看得到是誰登錄的。
- [key 集中在一台] → 只綁內部介面、只經 SSH 進入、檔案 0600、輪替只要改 secret 並重新部署。

## Migration Plan

1. 本人已定案 v4（2026-10-11 深夜）。
2. 本 repo：
   - 服務改成三個介面、只綁 127.0.0.1；
   - 移除租約、管理 API、狀態板、用量查詢；
   - 加上隧道守護與 `lgw connect`；
   - workflow 改走會合點；
   - `lgw` 提供 list／get／登錄冷卻的客戶端。
3. 本人做一次性的開機設定（決定 5），設 environment 與 secret（tasks 3.4）。
4. MyLinuxPool：`TUNNEL_EXTRA_PUBKEYS` 與 `NODE_GATEWAY.ports.llmgateway`（mylinuxpool-pm 開 d 單；var 的值由本人或 PMO 設）。#67～#70 已作廢。
5. sidecar v4 接上 list／get／登錄冷卻，在 Mac 與一台 worker 驗收換把。
6. 另開單把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key。

- **退回**：停服務；worker 在第 6 步之前仍有 MyLinuxPool#50 的 key。

## Open Questions

- opencode 讀 key 與 baseURL 的方式（例如 `OPENCODE_CONFIG_CONTENT`）：屬於 sidecar 的 AgentDriver，實作時在 opencode 1.18.35 上驗證。
- v5 第二輪後無未決：升級方式、worker port、ops-scripts 邊界、拉取頻率、repo 與 secret 名、手機通道皆已裁定（見 v5-plan §7）。
