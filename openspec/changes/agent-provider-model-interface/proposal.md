對應工單：#33「agent 用的介面改成：輸入 provider＋model，輸出 JSON（meta 資訊、必要的環境變數）」；上層：#30（LLMGateway 擴張）；源頭：MyPMO#31（AiCompany 期 1）、MyPMO#4（啟動 LLMGateway）。

> 本 change 疊在 `gateway-v0`（#8，v5）之上：那份 change 尚未歸檔，本檔的 delta specs 以它的 `subscription-registry` delta 為底。**歸檔順序：`gateway-v0` 先歸檔、本 change 後歸檔**；在 `gateway-v0` 歸檔前，主規格 `agent-lease` 仍是 v0 的舊語意（Claude 兩把平均分攤），本 change 的 priority 選把要等 `gateway-v0` 歸檔後才成為主規格。這是 tasks 5.4 的歸檔 gate。
>
> **本 change 不 `REMOVED` get**：get 在並存期仍要服務舊 sidecar；把它列為 REMOVED 會讓 delta 套用後的主規格說「get 不存在」，實作卻還有 get。get 的移除移到 sidecar 遷移＋實機驗收後的 d 單（tasks 4）。終態仍是三個介面（list、resolve、cooldown）。

## Why

本人（2026-10-17）：給使用 AI agent 的場景，介面改成「**輸入 provider＋model，輸出 JSON（meta 資訊、必要的環境變數）**」；**不給 command**——command 由 sidecar 組，而且要考慮 agora 的接續（continue）。

實際痛點有兩個：

1. **同一把上換模型**：sidecar 開 agent 時只能指定「哪一把 subscription」（`POST /agents` 的 `subscription: {"id": …}`），模型由那把的 `models.default` 決定。同一把（例：`opencode-zen`）上有多個免費模型，隊員卻只能吃那一個預設（例：`mimo-v2.6-flash-free`）；審查隊員想換別的免費模型，沒有介面可講。
2. **指定帳號**：`provider=anthropic` 蓋 `claude-personal`／`claude-company` 兩把；兩把的 `agents`、`models.allow`、`kind` 都相同，唯一差別是 priority。若 resolve 只收 provider＋model，個人那把永遠優先，呼叫方無法要求「這次用公司帳號」或「這次用個人帳號」。公司／個人帳號的分離（計費、隱私、額度保留）本來就是兩把存在的理由；這也是除錯／驗收（只測某一把的 token 或冷卻反應）的手段。所以 resolve 與 `POST /agents` 都要保留「釘住某一把」的選填入口。

做好後：呼叫方以 provider＋model 點菜，也可以用 `subscription` 釘住某一把；審查可以用不同的免費模型；政策仍由 `models.allow` 擋住不該用的（例：會計費的）模型；換把時可以排除剛耗盡的那一把，不會在原地打轉。

## What Changes

- **新介面 `resolve`**（spec `subscription-registry`）：`GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>][&subscription=<id>][&exclude=<id>[,<id>…]]` → `{schema, env_schema, meta, env}`。
  - 輸入 provider（必填）＋model（選填）＋agent 種類（必填）；`subscription` 選填＝釘住某一把（必須與 `provider` 一致，不一致 400）；`exclude` 選填＝換把時排除剛耗盡的那一把（不需要新端點）。
  - 輸出 meta（選中的 subscription、provider、kind、model、models（限制）、endpoint、priority）＋env（把這個 provider＋model 用起來的必要環境變數，名稱→值，依 agent 種類組成）。
  - 釘住的那把冷卻中 → 409 `cooling`＋`until`，**不代換另一把**；`exclude` 後沒有候選 → 409 `no_candidate`，sidecar 停下回報（不原地打轉）。
  - `schema` 是該端點回覆形狀的版本；`env_schema` 是 env 組成（鍵集與語意）的版本；sidecar 對不認得的高版本拒收。
- **選把移進 LLMGateway**：輸入已是 provider＋model，「挑哪一把 subscription」無法留在呼叫方；規則＝政策檔的機械套用（候選＝同 provider＋agent＋allow 涵蓋＋已部署→排除冷卻→priority→id）。全部冷卻中回 409 `cooling` 帶最早恢復時刻。冷卻登錄仍以 subscription id（resolve 的 meta 給）。政策載入補上 openrouter 的 `agents` MUST 只有 `opencode` 的檢查（目前 loader 只驗 `models.allow`）。
- **env 的組成移進 LLMGateway**（反轉 v0 決定 7「row 不帶環境變數名」）：憑證、端點、模型的環境變數由 resolve 回（claude 系的 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_*`；opencode 的 `OPENCODE_API_KEY`／`OPENCODE_PROVIDER_ID`／`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL` 片段）。**Gateway 不回整包 `OPENCODE_CONFIG_CONTENT`**：opencode 的 config 由 sidecar 用這些片段＋自己的 `permission`／`instructions`＋worker 釘的 opencode 版本組；同名鍵 sidecar 贏。sidecar 疊自己的維運／行為變數（auto-update 開關、server password、`AGORA_CLAUDE_CMD`、身分變數）。**憑證值只進 sidecar 建的子行程環境**：不進 log、不進 status、不進稽核；model／provider／endpoint 是中介資料，稽核可記。
- **稽核**：resolve 記（時刻、subscription、agent、provider、model）；audit 表加 `provider`／`model` 兩個 nullable 欄位，啟動時以 `PRAGMA table_info` 檢查並 `ALTER TABLE` 補上（additive，支援 `install.sh --tag` 退版）。
- **get 退場（不是本 change）**：`GET /v1/subscriptions/{id}?agent=` 在 sidecar 遷移＋實機驗收後由 d 單移除；本 change 只保留並存期與終態說明。終態對外還是三個介面：list、resolve、cooldown。
- **list、cooldown、政策檔語意不變**（政策檔只補 loader 的既有約束）；`lgw.client` 加 `resolve`、`get` 保留到 d 單。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `subscription-registry`：ADDED（resolve、釘住／排除、選把規則、env 的組成、model 省略語意、schema／env_schema）；MODIFIED（稽核紀錄、錯誤回覆、共通 JSON schema 的 env 歸屬與政策 loader 約束、models.default 的語意）。**沒有 REMOVED**（get 的移除在 d 單）。

## Impact

- **MyLinuxPool sidecar**（#114 一系）：`POST /agents` 的新形狀是平面 `provider`＋選填 `model`，並可帶選填 `subscription` 釘住（`subscription` 沿用 `{"id": …}`）；三者都省略時 sidecar 從 list 挑預設再 resolve。舊形狀（只給 `subscription`）在過渡期走現行 get，兩者不混用（組合矩陣見 design 決定 6.1）。來源改呼 resolve（resolve 404 時可回 get，支援 Gateway 退版）、錯誤對照補 `cooling`（帶 until）、`model_not_allowed`、`no_model`、`no_candidate`；driver 改為「sidecar 完整環境＋resolve env overlay＋維運變數」，opencode 的 `OPENCODE_CONFIG_CONTENT` 由 sidecar 組；換把用 `exclude` 排除剛耗盡那把。寫成「對 sidecar 的介面需求與建議」（design 決定 6），由 PM 與 MyLinuxPool 的 PM 對齊。
- **排序**：sidecar 的實作排在 MyLinuxPool#216 階段 2.5a（換把／自動重開重 render harness）merge 之後，避免 `_swap` 與 `POST /agents` 欄位白名單同時改；這是排序建議，**排序以 PMO 為準**（tasks 2.0／5.1）。
- **進行中的 sidecar 工單不撞**：MyLinuxPool#216（AI 名／身分／harness）、#229（互動版 PM）動的是身分與終端，與 provider＋model 正交（design 決定 6 末節）；交會是 `POST /agents` 欄位白名單與 `_swap` 路徑，合併時對一次。
- **#32（provider 追加可用模型）**：政策檔內容的擴充，與本 change 的介面互補；先後不互相依賴（design 決定 7）。
- **agora 接續**：換把後同 session continue 時，resolve 來的模型／憑證變數要照今天一樣讓接續行程看到（design 決定 6.5）。
- **部署與退版**：Gateway 先上（舊 sidecar 仍用 get），worker 映像後上（新 sidecar 用 resolve）；退版＝`install.sh --tag` 回舊 Gateway，新 sidecar 在 resolve 404 時回 get（design 6.2）。

## 不做

- **不給 command**：怎麼起 agent（指令、參數、終端、agora 接續的細節）都是 sidecar 的事。
- **不做資料面**：不代轉模型請求；resolve 只給「用哪一把＋環境變數」。
- **不做用量／額度查詢**（Zen／Ollama 查無公開 API，`docs/research/quota-signals.md`）；meta 的「限制／到期」只有 LLMGateway 自己知道的東西（`models.allow`、冷卻 until）。
- **不做租約式管控**（同時使用上限、「誰正在用」）：維持 v5 的稽核（誰在何時 resolve 過什麼）。
- **不處理 Antigravity（agy）**：政策裡沒有 agy 的 subscription（期 1 就排除了）；要支援時另開單。
- **不改 list／cooldown 的形狀、不改政策檔的語意**（只補 loader 對既有 MUST 的檢查，以及 #32 加內容）。
- **不做冷卻細粒度**：本次維持 subscription 粒度的排除；`(subscription, model)` 粒度列未決 2，另開單。
