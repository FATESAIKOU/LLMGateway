對應工單：#8「LLMGateway v0：切 scope、接上訂閱，讓 Mac 的 opencode／Claude Code 經由 Gateway 使用」；上層：MyPMO#4。

> **v3（2026-10-11 夜）**，照本人的新方向改寫。之前的版本（資料面代轉、租約）見 git 歷史與 #8 的留言。sidecar 那一側見 MyAiStorage 的 `docs/proposals/aicompany-design-v3.md`（aistorage-pm），兩邊的共通 JSON schema 已一致。

## Why

本人的原始需求：AI 在 worker 上工作到一半 token 用完時，由外部的確定性程式自動換一把。可以換的有五把：

| subscription id | provider | 是什麼 |
|---|---|---|
| `claude-personal` | anthropic | Claude Code 個人訂閱（setup-token） |
| `claude-company` | anthropic | Claude Code 公司訂閱（setup-token） |
| `openrouter` | openrouter | OpenRouter key |
| `opencode-zen` | opencode-zen | OpenCode Zen key |
| `ollama-cloud` | ollama-cloud | Ollama Cloud key |

本人的新方向：

> 「LLMGateway 我想現在應該需要的介面是（不需要多餘介面喔）：
> - 列出 Provider-Subscription 一覽
> - 取得 Provider-Subscription 的詳細情報（共通 schema 的 json），sidecar 的 aiagent 生命週期控制介面吃這個共通 json schema 開 aiagent
> - 登錄這 subscription 的 1w／5h 冷卻時間，這樣 list 才能列出這資訊（現在能不能用）」

相依只有 sidecar→agora、sidecar→LLMGateway。

## What Changes

- **LLMGateway 對外只有三個介面**（spec `subscription-registry`）：
  - `list`：所有 subscription 的共通 JSON，不含 key，含冷卻與 `available`；
  - `get(id, agent)`：一筆的共通 JSON，含 key。`agent` 必填，不在該筆的 `agents` 裡就拒絕；這是 Claude 條款的守門：Claude 訂閱只給 `claude-code`。
  - 登錄冷卻：`5h`／`1w` 的重置時刻，過了自動解除。
- **共通 JSON schema**（和 aistorage-pm 一致）：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models`、`endpoint`、`key`（只在 get）、`cooldown`（`5h`、`1w`）、`available`。**不帶 agent 專用的環境變數**：把 key 變成哪些環境變數，由 sidecar 的 agent driver 決定，跟著 agent 版本走。
- **換把由 sidecar 做**：偵測到用完 → 登錄冷卻 → 從 list 挑 `available` 而且 `agents` 含自己種類、`priority` 最前的 → get → 重開並 continue 同一個 Session。LLMGateway 只保管、列出、記冷卻。
- **認證**：一類機器一把讀取 token（`pool-worker` 共用一把、Mac 一把、之後手機一把），和 key 一起由部署流程放好。**沒有發證、撤證的 API**。
- **部署照舊**（spec `key-vault`）：獨立帳號 `llmgw`、`deploy-keys`／`deploy-service`、`remote-deploy.sh` 的 `keys`／`upgrade` 動詞、forced command。
- **拿掉**：租約（request／start／renew／exhausted／release）、同時租用上限、10 分鐘確認、狀態板、管理 API（issuer）、用量查詢。期 1 的 `agent-lease`、`usage-record` 規格一併移除。

## Capabilities

### New Capabilities

- `subscription-registry`：list、get、登錄冷卻、共通 JSON schema、稽核紀錄。
- `key-vault`：key 只放一處、部署、值不外洩、一類機器一把 token、服務不對外。

### Modified Capabilities

- `agent-lease`：全部移除（REMOVED），改由 `subscription-registry` 與 sidecar 取代。
- `usage-record`：全部移除（REMOVED）。

## 要本人判斷的（拿掉之後會失去的東西）

1. **同時租用上限**。本人先前為了「公司帳號用完換另一個」的灰色地帶，要求每把有同時使用的上限（Claude 兩把各 2）。沒有租約，LLMGateway 就不知道一把正被幾個 agent 用，所以這個上限**做不到**；sidecar 也只看得到自己這台。要嘛接受拿掉，要嘛之後在 sidecar 側另想辦法。
2. **「誰正在用哪一把」**。原本狀態板的必備欄位。沒有租約就只剩稽核紀錄裡的「誰在何時 get 過哪一把」，看不到「現在還在用」。
3. **worker 的身分**。worker 共用一把 token，稽核紀錄只看得到 `pool-worker`，分不出是哪一台。要分的話，得回到「每台一把＋發撤 API」的做法。

以上三點都不是「非有不可」，所以 v3 先拿掉，請本人確認可以接受。

## 銜接

- **MyAiStorage（sidecar v3）**：用 list、get、登錄冷卻；挑選與換把在 sidecar 內部；env 對應在 AgentDriver。
- **MyLinuxPool**：
  - #68（跳板機上的轉發條目）、#69（worker 連到服務的路）照舊；
  - #70 變簡單：不再呼叫管理 API，改成把 `pool-worker` 的讀取 token 像其他 `POOL_WORKER__*` 一樣掛進 worker。
- **MyAiEntry#147**：手機一樣用讀取 token＋list／get。

## 不做

- 資料面代轉、格式轉換（LiteLLM）。
- 依內容或效能改送（#3、#4）。
- token／成本計量、用量 %。
- Antigravity（agy）。
