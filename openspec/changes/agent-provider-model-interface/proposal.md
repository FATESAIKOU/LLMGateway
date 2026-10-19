對應工單：#33「agent 用的介面改成：輸入 provider＋model，輸出 JSON（meta 資訊、必要的環境變數）」；上層：#30（LLMGateway 擴張）；源頭：MyPMO#31（AiCompany 期 1）、MyPMO#4（啟動 LLMGateway）。

> 本 change 疊在 `gateway-v0`（#8，v5）之上：那份 change 尚未歸檔，本檔的 delta specs 以它的 `subscription-registry` delta 為底；若它先歸檔，本 delta 套用到合併後的主規格，內容不變。

## Why

本人（2026-10-17）：給使用 AI agent 的場景，介面改成「**輸入 provider＋model，輸出 JSON（meta 資訊、必要的環境變數）**」；**不給 command**——command 由 sidecar 組，而且要考慮 agora 的接續（continue）。

實際痛點：sidecar 開 agent 時只能指定「哪一把 subscription」（`POST /agents` 的 `subscription: {"id": …}`），模型由那把的 `models.default` 決定。同一把（例：`opencode-zen`）上有多個免費模型，隊員卻只能吃那一個預設（例：`mimo-v2.6-flash-free`）；審查隊員想換別的免費模型，沒有介面可講。做好後：呼叫方以 provider＋model 點菜，審查可以用不同的免費模型，政策仍由 `models.allow` 擋住不該用的（例：會計費的）模型。

## What Changes

- **新介面 `resolve`**（spec `subscription-registry`）：`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>]` → `{schema, meta, env}`。輸入 provider（必填）＋model（選填）＋agent 種類（必填）；輸出 meta（選中的 subscription、provider、kind、model、models（限制）、endpoint、priority、cooldown、available）＋env（把這個 provider＋model 用起來的必要環境變數，名稱→值，依 agent 種類組成）。
- **選把移進 LLMGateway**：輸入已是 provider＋model，「挑哪一把 subscription」無法留在呼叫方；規則＝政策檔的機械套用（候選＝同 provider＋agent＋allow 涵蓋＋已部署 → 排除冷卻 → priority → id）。全部冷卻中回 409 `cooling` 帶最早恢復時刻。冷卻登錄仍以 subscription id（resolve 的 meta 給）。
- **env 的組成移進 LLMGateway**（反轉 v0 決定 7「row 不帶環境變數名」）：憑證、端點、模型的環境變數由 resolve 回（claude 系的 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`；opencode 的 `OPENCODE_CONFIG_CONTENT`）；sidecar 疊自己的維運／行為變數（auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數、permission）。**值只進 sidecar 建的子行程環境**：不進 log、不進 status、不進稽核。
- **get 退場**：`GET /v1/subscriptions/{id}?agent=` 在 sidecar 遷移＋實機驗收後移除；終態對外還是三個介面：list、resolve、cooldown。
- **list、cooldown、政策檔語意不變**；`lgw.client` 加 `resolve`。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `subscription-registry`：ADDED（resolve、選把規則、env 的組成、model 省略語意）；MODIFIED（稽核紀錄、錯誤回覆、共通 JSON schema 的 env 歸屬說法、models.default 的語意）；REMOVED（get）。

## Impact

- **MyLinuxPool sidecar**（#114 一系）：`POST /agents` 的 `subscription` 改成 `provider`＋`model`、來源改呼 resolve、driver 的組成改為「併入」、換把流程改經 resolve、status 加欄位。寫成「對 sidecar 的介面需求與建議」（design 決定 6），由 PM 與 MyLinuxPool 的 PM 對齊。
- **進行中的 sidecar 工單不撞**：MyLinuxPool#216（AI 名／身分／harness）、#229（互動版 PM）動的是身分與終端，與 provider＋model 正交（design 決定 6 末節）。
- **#32（provider 追加可用模型）**：政策檔內容的擴充，與本 change 的介面互補；先後不互相依賴（design 決定 7）。
- **agora 接續**：換把後同 session continue 時，resolve 來的模型／憑證變數要照今天一樣讓接續行程看到（design 決定 6）。

## 不做

- **不給 command**：怎麼起 agent（指令、參數、終端、agora 接續的細節）都是 sidecar 的事。
- **不做資料面**：不代轉模型請求；resolve 只給「用哪一把＋環境變數」。
- **不做用量／額度查詢**（Zen／Ollama 查無公開 API，`docs/research/quota-signals.md`）；meta 的「限制／到期」只有 LLMGateway 自己知道的東西（`models.allow`、冷卻 until）。
- **不做租約式管控**（同時使用上限、「誰正在用」）：維持 v5 的稽核（誰在何時 resolve 過什麼）。
- **不處理 Antigravity（agy）**：政策裡沒有 agy 的 subscription（期 1 就排除了）；要支援時另開單。
- **不改 list／cooldown 的形狀、不改政策檔的語意**（只會因 #32 加內容）。
