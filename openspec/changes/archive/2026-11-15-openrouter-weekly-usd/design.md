# 設計：OpenRouter 本週 USD

## D1 介面與範圍
採用 `GET /v1/usage` 每筆固定新增 `weekly_used_usd`（JSON 數字或 null）與 `weekly_used_usd_fetched_at`（RFC 3339 UTC 或 null）。只有使用 OpenRouter fetcher 的 subscription 可有非 null；由受信任的 provider/fetcher 身分判定，不能只靠上游資料或名稱。每筆固定兩鍵方便消費端；null 不是金額。它是上游 `data.usage_weekly` 原值，單位 USD，不換算、不取整，不代表餘額或配額。

為什麼：週報已有一覽入口；金額與窗口各自可能失敗，獨立時間讓讀者知道金額何時取得，不誤用窗口的 `fetched_at`。放棄在九鍵 `usage` 新增欄位：會把金額送到 list、resolve 與 sidecar，擴大暴露及相容面。放棄只加數字不加時間：金額可在窗口失敗時成功，窗口時間不能可靠代表金額時間。放棄放入 `pipes`：管子是剩餘百分比，且禁止絕對數量。

## D2 解析、快取與錯誤
200 且 `data` 是物件時，接受有限、非負的 JSON number，包括 0 與小數；布林、字串、null、缺欄、負數、NaN、Infinity、1e400（解析為 inf）等非有限數皆為 null，不回顯原文。金額無效只丟棄金額，不改窗口解析結果；即使窗口無效或全缺，合法金額仍可獨立更新。金額解析不改窗口的 status、reason、fetched_at、退避及 single-flight。上游非 200、逾時、無法解析整體 JSON 或 `data` 不是物件時，依既有窗口失敗碼，金額清空為 null；成功回應金額缺或無效也清空，不沿用舊金額。停用、key 未部署、未取過或內部例外時均回 null，時間也為 null。非 null 金額的時間是該次成功讀到有效金額的時刻；清空時時間一併清空。

實作注意（2026-11-15 讀 `lgwd/usage_openrouter.py` 查得）：標準庫 `json.loads` 預設接受 `NaN`、`Infinity` 字面值，須逐值以 `math.isfinite` 檢查；`parse_constant` 只被這些字面值呼叫，攔不到 `1e400`（它解析成 inf，同屬非有限，仍由逐值檢查攔下），且不可讓它拋錯中斷整份解析（否則連窗口一起判壞）；`bool` 是 `int` 子類，須先排除。金額須與窗口共用同一次 HTTP 回應，但不得混入 `FetchResult.windows`，須另設獨立欄位，否則窗口的 `no_windows` 判定會被金額影響。

實作契約（給 PR 2）：`FetchResult` 另設獨立金額欄位（例如 `weekly_usd`，值為 `(金額, 取得時刻)` 或 None），不得放入 `windows`。`_parse` 先獨立解析金額，再判斷窗口；窗口欄位壞或缺（含 `free_model_daily_requests` 型別壞）時，不得因此丟掉已合法的金額，金額保留且帶自己的時間。因此 `_parse` 在「200 且 `data` 是物件、金額合法、窗口壞或缺」時，仍要回帶金額的 `FetchResult`（窗口結果照常為 `PARSE_ERROR`、`no_windows` 或其他既有碼）。`_finish` 只看 `FetchResult`、不看 HTTP status：`FetchResult` 帶金額就寫入（連同時間），沒帶就清空；清空只會遇到 `_finish` 真的會遇到的情形：上游非 200、逾時、整體 JSON 無法解析、`data` 不是物件、金額缺或無效、內部例外（停用、key 未部署與未取過不經 `_finish`，見下段投影）。投影兩鍵（一覽讀取路徑）時先套用與窗口相同的 `_enabled`／`_deployed` 判斷：停用或 key 未部署一律 null；金額快取本身不動。金額的取得時刻用請求前取得的 `now`（與 `_parse` 算 `resets_at` 的那個），不用 `_finish` 的 `now`：回應跨過週一 00:00 UTC 時，前者只會讓跨週規則更早失效，後者會把上週的值標成新週而誤用。金額的寫入與清空不改 `fetched_at`、`last_reason`、退避與 single-flight 的既有行為。

金額僅留記憶體，同一把鎖下與時間一起更新；與既有查詢觸發、TTL、退避、single-flight 共用請求，但其有效性獨立於窗口結果。超過六小時（以 `weekly_used_usd_fetched_at` 判斷）一律投影為 null（不改快取）；不因窗口重置或窗口 `stale` 而變 null，金額沒有自己的 `stale` 欄位，有效性只由自己的時間與跨週規則決定。一覽在同一個 `now` 下算窗口與金額，不各自取樣。

跨週失效（已定案採用）：OpenRouter 官方 `GET /api/v1/key` 型別說明寫 `usage_weekly` 為「current UTC week, starting Monday」；limits 頁的預算週期窗口寫「a weekly one on Monday 00:00 UTC」。頁面未明寫 `usage_weekly` 本身的重置時刻，故判斷方式為：以 `weekly_used_usd_fetched_at` 推出其所屬 UTC 週的週一 00:00 UTC（時間減去 `weekday()` 天後時分秒歸零），與讀取時 `now` 的同一基準比較；不同週即金額與時間皆回 null，不得以跨週的舊值代替。不另存週起點。被放棄：失敗時保留舊金額，因為缺獨立 stale／錯誤碼且可能誤認為新值；把無效金額當整次 parse_error，會拖累原有窗口。

## D3 相容與版本
新增欄位依既有新增欄位不 bump 的原則，一覽 `schema` 維持 1；但嚴格檢查十鍵的外部客戶端會壞，部署前須通知週報消費者並確認容忍未知欄位。list 的九鍵、resolve 的 `meta.usage` 和 schema、`env_schema` 均不變。`lgw/client.py` 僅驗頂層 dict，原樣透傳；需驗證新欄位和舊回應皆可讀。`lgwctl usage` 讀 list、不顯示金額，維持現狀。先前 archive 記載 sidecar 使用 `.get()` 且不比鍵數，但本次未重新查驗；由於 list／resolve 不變，不需修改 sidecar，仍須在部署計畫確認。`USAGE_SUMMARY_KEYS` 只投影九鍵 usage 的八個欄位，新兩鍵須由受限的獨立快取投影，不能盲目加入該 tuple。`lgwd/http.py` 路由不變。

放棄 bump 一覽 schema：此為新增欄位，既有約定是忽略未知欄位；缺點是不能靠版本辨別部署，讀者應以欄位是否存在判斷舊服務。放棄為所有端點加欄位：放大破壞面。

## D4 安全與規格
僅白名單取 `usage_weekly`，不複製上游物件；其他 subscription 不回金額，所有 pipes、note、list、resolve 的 usage 均不帶金額。上游 key、token、帳號識別與原文不進一覽、usage、log、例外或稽核；resolve 原有 `env` 是獨立既有憑證契約，不在此禁止之列。放棄按名稱過濾整份上游 JSON：未知欄位仍可能洩漏。

## D5 金額 canary 設計
測試以互異、非整數、不與合法值（0、1.25）撞號的假值注入：OpenRouter `usage_weekly` 用 `7.3191`；其他訂閱的金額與 OpenRouter `limit`、`limit_remaining`、`usage_daily` 各用不同的值（如 `5.8127`、`9.4403`、`3.1467`）；key、token、帳號各用含可辨識前綴的假字串。掃描對象為 list、resolve、`GET /v1/usage`、log、稽核與快取檔（若有），同時比對十進位字面與 JSON 數字形式。只有一覽的 `weekly_used_usd` 欄位允許出現 `7.3191`；其他 canary 任何位置出現即失敗。resolve 原有 `env` 不在掃描中豁免憑證以外的值。

delta MODIFIED「用量一覽（GET /v1/usage）」、「用量一覽的內容邊界」，以及兩條只加例外的既有 Requirement：「note 的語意」（Scenario「不拿金額或帳號」的 THEN 加「唯一例外是 `GET /v1/usage` 的 `weekly_used_usd`」）、「usage 的 session 與 weekly 窗口」（Scenario「方案沒有 session／weekly」的「回應不帶金額」限定為 list 的 `usage`）；ADDED「OpenRouter 本週已用 USD」。九鍵 usage、OpenRouter 不納入其他欄位、pipes 與 note 的既有限制仍成立。歸檔時手動在主 spec Purpose 補一句正面說明：一覽另帶 OpenRouter 本週已用 USD（null 不是金額，其他欄位仍不含金額）；不更動其他 spec 的九鍵規定。

## 介面範例
以下為假值；其他既有欄位仍存在，範例省略其他訂閱：
```json
{"schema":1,"subscriptions":[{"id":"openrouter","kind":"api-key","status":"ok","source":"openrouter-api-key","fetched_at":"2026-11-15T01:00:00Z","stale":false,"reason":null,"note":null,"session":null,"weekly":null,"weekly_used_usd":0,"weekly_used_usd_fetched_at":"2026-11-15T01:00:00Z"}]}
```
上述 `ok` 可對應有 credits 管子而一覽不顯示 pipes。缺值時兩個新欄位均為 null；窗口失敗但金額合法時可為 `unknown`／`no_windows` 與非 null 金額，應以金額自己的時間判讀。查詢不等待背景請求，第一次通常回 null；下次讀取才看到結果。

## 未決
官方週界已查證並寫進 D2（官方只明寫 `usage_weekly` 為「current UTC week, starting Monday」與 limits 的 Monday 00:00 UTC，未明寫 `usage_weekly` 本身的重置時刻；本設計以 `weekly_used_usd_fetched_at` 推週界作契約）。PMO 與非作者審查須確認欄位、暴露範圍、schema 與錯誤語意；週報消費者容忍新鍵及部署計畫須確認；本設計沒有實帳號驗證。
