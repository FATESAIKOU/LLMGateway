對應工單：#60「list／resolve 回應帶每把 key 的用量與『再次能用的時間』（Gateway 自己取 Ollama Cloud 與 Claude 訂閱的用量）」；相關：#48（get 退場）、#58（輪換表的「計入週用量節流」欄）、#2（多訂閱無縫切換）。

> 本 change 只有 proposal 與 design（設計 PR）；本人已於 2026-10-25 裁定 design 的判斷點，specs 與 tasks 之後再寫。

## Why

本人（2026-10-25，經 PMO）：查 subscription 時順便回傳用量與「再次能用的時間」，不另外加介面；Claude 的用量由 LLMGateway 自己取得，不靠別處回報。現況：Gateway 只知道「呼叫方登錄的冷卻 `until`」，不知道每把還剩多少、什麼時候重置；PM 與人要換把或排程時只能猜。

## What Changes

- **list 的每一筆多一個 `usage` 物件與一個 `available_again_at`**（design 決定 1）：用量以 `session`／`weekly` 兩個窗口表示（`remaining_percent`、`resets_at`），附 `source`、`fetched_at`；讀不到回 `unknown`（帶原因碼），沒有用量來源的 provider 回 `not_applicable`，**不回 0%**。list 仍然不回 key。
- **resolve 的 `meta` 多帶同一個 `usage`**（本人已決定；同 list，只讀快取，快取過期時觸發一次非阻塞（作者提議，見待決 F）的取用量，design 決定 1、4）。**get 不帶**（#48 退場）：`usage`／`available_again_at` 在 `list_subscriptions` 內合入，不經 get 也用的 `_item`。
- **Gateway 自己取用量**（design 決定 2～4）：只在 list／resolve 被呼叫、而且快取過期時才取（本人已決定 E1；不做定時排程，Claude 快取 30 分鐘；Ollama 120 秒且被查到才取為作者提議），取用量在另一條執行緒做、不阻塞回應（作者提議，是否接受見 design「待本人決定 F」），結果放記憶體快取；上游慢或壞不影響 list／resolve。
  - Ollama Cloud：官方 `GET /api/balance`，用 vault 的 key 當 Bearer；
  - Claude 訂閱：在 Gateway 主機跑**原版 Claude Code**，讀 statusline 的 `rate_limits`（有文件）；`/usage` 為退路（沒文件）；不拿訂閱 token 打沒有文件的 usage endpoint。
- **其他 provider**（OpenRouter、OpenCode Zen）：`not_applicable`（design 決定 5）。
- `schema` 不 bump（只新增欄位，消費端忽略不認得的欄位）。`available` 的語意不變、resolve 的選把規則不變。

## Capabilities

### New Capabilities

- `usage-reporting`：用量取得器（每個 provider 一個）、快取與 TTL、讀不到時的回報規則、與冷卻合併成 `available_again_at` 的規則。

### Modified Capabilities

- `subscription-registry`：MODIFIED（list 的形狀、resolve 的 `meta`、共通 JSON schema 加 `usage`／`available_again_at`）。
- `key-vault`：MODIFIED（`值不外洩`加上：用量取得器讀 key 只在行程內，不進 log、稽核、回覆；新增的外連只到官方 API）。

## Impact

- **lgwd**：新增用量取得模組與快取；`providers.py` 每個 provider 多一個取用量的實作（沒有就 `not_applicable`）；`registry.py` 的 `list_subscriptions` 與 resolve 路徑讀快取（`_item` 不動，get 不受影響）；用量取得與 `keysync.py` 只共用子行程護欄（起停、killpg、stderr 尾端；計時不共用），`Gateway.stop` 要收掉仍在跑的取用量子行程；`http.py` 不加路由。
- **Gateway 主機（真機動作）**：要裝原版 Claude Code、讓服務帳號可執行、政策檔多一段 `usage` 設定。部署計畫交 PMO，由本人授權；在授權前 Claude 取用量預設關閉（`unknown`／`disabled`）。
- **呼叫方**：sidecar、PMO 的 list 消費端忽略不認得的欄位即可；要用 `available_again_at` 的（#58、#2）之後再接。
- **額度成本**：Ollama 取用量不耗額度；Claude 的 statusline 管道要一次 API 回應，會耗少量訂閱額度，但只在被查到且快取（30 分鐘）過期時才取，沒人查就是 0（design 決定 3、4）。
- **本人已決定**（2026-10-25）：A～D 照建議、E1 被查到才取、E2 維持現狀不另開單（design 文首）。

## 不做

- **不加新端點、不加應用層認證**；存取控制仍靠 SSH。
- **不用用量自動登錄冷卻、不改 `available`、不改 resolve 的選把**（design「作者提議」）。
- **不取 Ollama 的 `/api/usage` 明細**（依模型的請求數與 token；本人已決定 D 只取 `/api/balance`），新方案的金額也不帶 `credits`（回 `unknown`／`no_windows`）。
- **不做 OpenRouter／Zen 的用量**（沒有查證過可用的官方管道，不在本次範圍）。
- **不拿訂閱 token 直接打沒有文件的 Claude usage endpoint**，也不收集、轉手 Claude 的 session token。
