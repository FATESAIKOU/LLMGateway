# LLMGateway v0：實體圖、控制時序圖、介面圖

> 依本人 2026-10-11 晚的回饋整理：介面簡潔、需要抽象時介面與實作分離、不要脆弱的相依、不基於 herdr、LLMGateway 跟 MyLinuxPool 無關。
> sidecar 那一側的完整設計在 MyAiStorage 的 `docs/proposals/aicompany-design-v2.md`（aistorage-pm），本檔只畫 LLMGateway 與它的接點，兩份用同一套名詞。
> 機器名不寫：「放服務的那台」是本人選定的常駐 provider。

## 1. 實體圖：有哪些東西、誰碰誰

```mermaid
flowchart LR
  subgraph GH["GitHub（LLMGateway repo）"]
    SEC["secret：五把 key＋部署用連線資訊"]
    WF["workflow：deploy-keys、deploy-service<br/>（手動觸發、本人核准）"]
  end
  subgraph JUMP["池的跳板機（MyLinuxPool）"]
    FWD["部署金鑰：只能轉發到那台的隧道埠<br/>（MyLinuxPool#68）"]
  end
  subgraph HOST["放服務的那台（跑兩個服務）"]
    subgraph LG["帳號 llmgw（LLMGateway）"]
      DEP["remote-deploy.sh<br/>（keys／upgrade）"]
      SVC["lgwd：保管＋租用＋冷卻＋狀態板"]
      KF["keys.env（0600）"]
      DB["SQLite：事件與租約"]
    end
    POOL["池的管理帳號（MyLinuxPool 的服務）"]
  end
  subgraph WK["worker 容器（MyLinuxPool 建立）"]
    SCW["sidecar daemon<br/>＋ LgwCredentialSource"]
    AW["agent（Claude Code／opencode）"]
  end
  subgraph MAC["Mac"]
    SCM["sidecar daemon<br/>＋ LgwCredentialSource"]
    AM["agent"]
  end
  UP["上游：Anthropic、OpenRouter、Ollama Cloud、OpenCode Zen"]
  MLPWF["MyLinuxPool 的 create-／delete-worker"]

  SEC --> WF
  WF -- "SSH 經跳板，以 llmgw 登入" --> FWD --> DEP
  DEP -- "寫入" --> KF
  DEP -- "升級＋重啟" --> SVC
  SVC -- "讀" --> KF
  SVC --- DB
  SCW -- "租用 API（經 MyLinuxPool#69 的路）" --> SVC
  SCM -- "租用 API（經 SSH 埠轉送）" --> SVC
  MLPWF -- "管理 API（發撤 worker 憑證，MyLinuxPool#70）" --> SVC
  SCW -- "子行程＋env" --> AW
  SCM -- "子行程＋env" --> AM
  AW -- "直接連（用租到的 key）" --> UP
  AM -- "直接連" --> UP
```

| 誰 | 碰誰 | 怎麼碰 | 不做什麼 |
|---|---|---|---|
| deploy workflow | 那台的 `llmgw` | SSH 經跳板；`command=` 只能跑部署腳本的兩個動詞 | 不開 shell、不讀 key 以外的東西 |
| lgwd | key 檔、SQLite | 讀 key；記事件 | **不轉送模型請求**、不碰 sidecar、不碰 agent |
| sidecar | lgwd | 透過 `CredentialSource`（實作 `LgwCredentialSource`）呼叫租用 API | 不保存 key；sidecar 本體不知道 LLMGateway |
| agent | 上游 | 用 sidecar 給的 env 直接連 | 不經 LLMGateway；Claude 訂閱 token 只給原版 Claude Code |
| MyLinuxPool | lgwd | 管理 API（只綁 loopback、只限 `worker-*`） | 不碰 LLMGateway 的資料庫與 key |

## 2. 控制時序圖

### 2.1 開一個 agent（拿到一把 key）

```mermaid
sequenceDiagram
  participant C as 控制者（PMO／人／排程）
  participant SC as sidecar
  participant CS as LgwCredentialSource
  participant L as lgwd
  participant D as AgentDriver
  C->>SC: open（agent 種類、Session、模型偏好）
  SC->>CS: acquire（agent 種類、模型偏好）
  CS->>L: POST /v1/leases（worker、角色、agent、instance、preferences）
  L-->>CS: lease（env、unset、效期）或拒絕
  CS-->>SC: env／unset
  SC->>D: launch（子行程，env 不上命令列）
  SC->>CS: started
  CS->>L: POST /v1/leases/{id}/start
  loop 每小時
    CS->>L: POST /v1/leases/{id}/renew
  end
```

### 2.2 額度用完、換一把（全部在 sidecar 內部；不是新的對外介面）

```mermaid
sequenceDiagram
  participant D as AgentDriver
  participant SC as sidecar
  participant CS as LgwCredentialSource
  participant L as lgwd
  participant AG as agora
  D-->>SC: state = 額度用完（重置時刻）
  SC->>CS: exhausted（訊號、重置時刻）
  CS->>L: POST /v1/leases/{id}/exhausted
  Note over L: 同一筆交易：那把冷卻到重置＋2 分鐘、原租約 superseded、依順序選下一把
  alt 有下一把
    L-->>CS: next（env、unset）
    CS-->>SC: 新的 env
    SC->>D: stop（溫和）→ 寫回 agora
    SC->>AG: prepare（continue 同一個 Session）
    SC->>D: launch（新的 env）
    SC->>CS: started → POST start
  else 全部冷卻
    L-->>CS: 拒絕（最早重置時刻）
    SC->>D: stop → 寫回；狀態記成「等額度」
  end
```

### 2.3 建 worker 時發憑證（MyLinuxPool#70）

```mermaid
sequenceDiagram
  participant WF as MyLinuxPool create-worker
  participant H as 放服務的那台（pool-ssh）
  participant L as lgwd（loopback）
  participant W as worker 容器
  WF->>H: pool-ssh，stdin 帶 issuer 憑證
  H->>L: POST /v1/admin/clients {name: worker-<名>}
  L-->>H: token（同名就撤舊發新）
  H->>W: stdin 寫入 /run/mlp-secrets/llmgateway-client-token
  Note over W: sidecar 啟動時 GET /v1/whoami，用回來的 client 當租約的 worker
```

### 2.4 部署 key 與升級

```mermaid
sequenceDiagram
  participant P as 本人
  participant WF as deploy-keys／deploy-service
  participant J as 跳板機
  participant R as remote-deploy.sh（llmgw）
  participant L as lgwd
  P->>WF: 手動觸發＋在 environment 核准
  WF->>J: ssh -J（只能轉發）
  J->>R: 強制命令；動詞 keys 或 upgrade <tag>
  alt keys
    R->>R: 驗證 stdin → 暫存檔 0600 → rename keys.env
    R->>L: SIGHUP 重讀
  else upgrade <tag>
    R->>R: install.sh --tag：固定 tag 取碼、current 原子切換、裝 unit
    R->>L: 重啟
  end
```

## 3. 介面圖（必要而且只有這些）

（類別圖裡的參數與回傳只寫主要的；完整欄位見 design 決定 6、表格見下。）

```mermaid
classDiagram
  class LeaseAPI {
    <<external lease>>
    request_lease(worker, role, agent, instance, preferences) lease
    start(lease_id)
    renew(lease_id)
    exhausted(lease_id, signal, resets_at) next
    release(lease_id)
  }
  class AdminAPI {
    <<external admin loopback>>
    issue(name) token
    revoke(name)
  }
  class ReadAPI {
    <<external read>>
    board()
    whoami()
  }
  class CredentialProvider {
    <<internal upstream kind>>
    env_for(agent, key, model) env
    usage(key) usage
  }
  class ClaudeSubscription
  class OpenRouter
  class OllamaCloud
  class OpencodeZen
  CredentialProvider <|.. ClaudeSubscription
  CredentialProvider <|.. OpenRouter
  CredentialProvider <|.. OllamaCloud
  CredentialProvider <|.. OpencodeZen
  class SelectionPolicy {
    <<internal selection>>
    choose(agent, candidates, preferences) subscription
  }
  class CredentialSource {
    <<sidecar internal interface>>
    acquire(agent, preferences) env
    started()
    renew()
    exhausted(signal, resets_at) env
    release()
  }
  class LgwCredentialSource {
    <<implementation in lgw>>
  }
  CredentialSource <|.. LgwCredentialSource
  LgwCredentialSource ..> LeaseAPI : calls
  LeaseAPI ..> SelectionPolicy : uses
  LeaseAPI ..> CredentialProvider : uses
  ReadAPI ..> CredentialProvider : usage
```

| 抽象 | 介面 | 這次的實作 | 換實作時不受影響的部分 |
|---|---|---|---|
| 租用（對外） | `LeaseAPI` 五個動作 | lgwd（HTTP＋SQLite） | sidecar、`LgwCredentialSource` 以外的所有人 |
| 發證（對外） | `AdminAPI` 兩個動作 | lgwd | MyLinuxPool 的 create-／delete-worker |
| 上游的種類 | `CredentialProvider` | Claude 訂閱、OpenRouter、Ollama Cloud、OpenCode Zen | 租用 API、選把規則、sidecar |
| 選把規則 | `SelectionPolicy` | 固定順序＋冷卻＋上限（之後：優先模型＋底線） | 租用 API、上游實作 |
| 拿 key 的方式（sidecar 側） | `CredentialSource` | `LgwCredentialSource`（本機開發可換成讀環境變數） | sidecar 本體、agent driver |
| 額度用完的判讀 | sidecar 的 `AgentDriver` 回報正規化狀態 | Claude Code、opencode 的 driver | sidecar 本體、LLMGateway |

**和前一版相比拿掉的**：
- `usage` 回報端點：v0 沒有人用；
- `lgw` 裡的換把程序：改在 sidecar 內部；
- 一切經 herdr 的做法：改成子行程。

**加上的**：
- `CredentialProvider`、`SelectionPolicy` 兩個內部介面：上游與選把規則都是實作。
