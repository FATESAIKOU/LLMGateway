<!-- 「服務本體啟停與無隧道獨立生命週期」僅改 Scenario「冪等啟停」的探針路徑；其餘原樣保留。 -->

## MODIFIED Requirements

### Requirement: 端對端健康探針
隧道守護啟用時，SHALL 定期經會合點對服務的 API 埠發一次 `GET /healthz` 請求，收到 HTTP 200 才算健康；連續失敗達門檻（預設 3 次）時 MUST 重建隧道。只確認 TCP 連線存在不算健康。部署器依已安裝版本的明確能力旗標指定 old/new，探針不得靠 HTTP 反應自行猜版本。old 模式且 `/healthz` 回 404 才可測舊 list；new 模式的 `/healthz` 非 200 一律失敗且不得退回 list。當服務未啟用隧道時，本機檢查只需驗證 `127.0.0.1:<本地API埠>` 回 HTTP 200，不要求遠端會合點探針。

#### Scenario: TCP 通但服務不通
- **WHEN** SSH 連線還在，但經會合點打 API 埠連續 3 次沒有 HTTP 200 回應
- **THEN** 守護程式重建隧道

#### Scenario: 舊版本明確標記且端點不存在
- **WHEN** 部署器指定 old 模式，且 `/healthz` 回 404
- **THEN** 探針測舊版 `/v1/subscriptions`，以其 200 判定成功

#### Scenario: 新服務探針失敗不得降級
- **WHEN** 部署器指定 new 模式，而 `/healthz` 回 404、401、503 或逾時
- **THEN** 此次探針失敗且不請求 `/v1/subscriptions`；依既有連續失敗門檻處置

#### Scenario: 舊服務探針其他錯誤
- **WHEN** old 模式的 `/healthz` 不是 200 或 404
- **THEN** 此次探針失敗，不測 list

#### Scenario: 未啟用隧道時只驗本機
- **WHEN** 機器未啟用隧道守護而執行健康檢查
- **THEN** 驗證本機 API 埠回 HTTP 200 即通過，不觸發遠端探針

### Requirement: 客戶端自備通道
sidecar SHALL 自己把會合點 API 埠的 HTTP 轉發放到本機 loopback 的某個 port，再用 `lgw.client` 或符合相同協定的 sidecar HTTP 客戶端（base URL 由呼叫方給）打 list／usage／resolve／roles／登錄冷卻。本機 port 預設為 4710，建通道的一方可經設定改（worker 由 MyLinuxPool 的 capability script 建通道到這個 port；Mac 用 mlp 或 ssh -J 自己轉發；手機自行用 ssh -J 等方式轉發，LLMGateway 不提供程式）。無會合點時，客戶端亦可在本地直連服務。enforce 時業務呼叫方 MUST 具備已登記身分並簽章。LLMGateway SHALL NOT 提供連線程式。轉發斷掉或本地服務不可達時客戶端 MUST 回報「無法確認」（`unreachable`），不讓呼叫方當掉。

#### Scenario: 直接打本機
- **WHEN** 本機已有到會合點 API 埠的轉發，或本地已直接啟動服務
- **THEN** 客戶端直接對本機 port 做 list／usage／resolve／roles／登錄冷卻，不經任何連線程式

#### Scenario: worker 本機 port 預設值
- **WHEN** 通道由 MyLinuxPool 的 capability script 建立且沒有另行設定
- **THEN** 本機 port 為 4710，sidecar 照此打 base URL

#### Scenario: 通道斷了
- **WHEN** 本機轉發斷掉或本地服務不可達
- **THEN** 客戶端回報「無法確認」（`unreachable`），由呼叫方等一下再試


#### Scenario: 新模式下未登記的呼叫者
- **WHEN** enforce 模式下未登記或未簽章的 sidecar 經已建立的通道呼叫業務端點
- **THEN** 服務拒絕該請求且不發放憑證

### Requirement: 服務本體啟停與無隧道獨立生命週期
管理介面 `llmgwctl` SHALL 支援不依賴隧道的純本地 `start`／`stop`／`restart`（冪等）與既有子命令，並提供需 root 的 `follow-reset`（含 `--fetch-state` 僅清抓取與退避）重置狀態；無隧道時檢查與升級 MUST 通過。存在隧道 unit 時，`check`／`status` SHALL 檢查（`--no-remote` 除外）；follow 異常或抓取被拒且隧道未跑 MUST 回 `fail`，運行中異常、`auth_failed` 或金鑰讀不到至少回 `todo`，非 root 無權限降為無法確認而不誤報。未運行遇 `auth_failed` 回 `fail`、運行中回 `todo`；到期門檻內或格式不合回 `todo`（不合理標頭視為未知）。輸出 MUST NOT 洩密。

#### Scenario: 冪等啟停本地服務
- **WHEN** `llmgateway.service` 已啟動且本機 `/healthz` 回 HTTP 200 時再次執行 `start`（或已停止時執行 `stop`）
- **THEN** 指令不重複動作且以退出碼 `0` 結束

#### Scenario: 無隧道純本地檢查通過
- **WHEN** 機器未安裝且未設定隧道守護，執行 `llmgwctl check`
- **THEN** 本機服務、本機 API 200、`keysync`、`policy` 與 `gh-keys-repo` 全數通過後即回退出碼 `0`

#### Scenario: 隧道存在時仍受監看
- **WHEN** 機器上已安裝隧道 unit（過渡期或已啟用），執行 `llmgwctl check`（未帶 `--no-remote`）
- **THEN** 檢查仍回報該隧道 unit 狀態與遠端探針結果，不因隧道改為可選而靜默漏檢

#### Scenario: 已設位址追蹤但 follow 檔異常或最近抓取被拒且隧道未跑時回報失敗
- **WHEN** 隧道已設定位址追蹤，但 follow 檔異常（含 `generation` 小於 `.tunnel_generation` 已驗證水位）或最近抓取分類為被拒（`regressed`／`invalid`／`hostkey_missing`／`auth_failed`），且 `llmgateway-tunnel.service` 未處於運行狀態（且呼叫者具讀取權限）
- **THEN** `llmgwctl check` 回報 `fail`（退出碼非 `0`）並指出處置方式（確認為上游回退時執行 `llmgwctl follow-reset`；`auth_failed` 換好 token 後執行 `llmgwctl follow-reset --fetch-state`），`llmgwctl status` 顯示對應狀態而不洩漏 IP、金鑰或 token

#### Scenario: 隧道運行中異常、設定半套、缺金鑰或認證失敗時不靜默通過
- **WHEN** `llmgateway-tunnel.service` 仍在運行中，但 follow 檔為 `missing`／`invalid`／`regressed`，或最近抓取為 `auth_failed`／`config_partial`／`regressed`／`invalid`／`hostkey_missing`，或金鑰檔缺漏／無讀權（`credential_unreadable`）
- **THEN** `llmgwctl check` 回報 `todo`（附固定警示原因，如 `auth_failed` 提示 token 可能失效請執行 `llmgateway-tunnel --fetch-check` 並於換妥後跑 `llmgwctl follow-reset --fetch-state`；`regressed`／`invalid`／`hostkey_missing` 提示現存連線靠最後已知位址維持、需人工確認 `NODE_GATEWAY`）而不靜默全綠通過

#### Scenario: 非 root 無權限讀取狀態目錄時不誤報缺失
- **WHEN** 非 root 呼叫者因 `STATE_DIR` 權限限制無法讀取 follow 檔而執行 `llmgwctl status` 或 `check`
- **THEN** `status` 顯示「需 root 或 llmgw 才能讀」且 MUST NOT 誤報為 `missing`，`check`（隧道未運行時）降為 `todo` 並註明需權限

#### Scenario: 需 root 顯式重置跟隨或抓取狀態
- **WHEN** 管理員以 root 執行 `llmgwctl follow-reset` 或 `llmgwctl follow-reset --fetch-state`（或非 root 嘗試執行）
- **THEN** root 執行預設模式時安全移除 `.tunnel_generation` 與 `.tunnel_fetch_backoff` 並重置 `.tunnel_fetch_state`；帶 `--fetch-state` 時只清除 `.tunnel_fetch_state` 與 `.tunnel_fetch_backoff` 而保留世代水位；非 root 執行時拒絕並回報需 root 權限

#### Scenario: GitHub token 到期提醒、格式檢查與認證失敗分級
- **WHEN** `TOKEN_EXPIRES_AT` 剩餘天數在門檻內或格式不合、標頭日期早於或等於抓取當下時間、或最近抓取錄得 `auth_failed`（`401` 或無限流標頭之 `403`）
- **THEN** 門檻內到期或 `TOKEN_EXPIRES_AT` 格式不合（`TOKEN_EXPIRES_AT 格式不合`）使 `check` 回 `todo`；早於或等於抓取當下時間的標頭視為 `expiry_unknown` 不誤報 `fail`；`auth_failed` 於隧道未 `active` 時回 `fail`、隧道仍 `active` 時回 `todo`，且 `status` 皆不洩漏 token 原文

#### Scenario: 管理輸出不洩漏金鑰、密碼與 token
- **WHEN** 執行 `llmgwctl` 任一子命令（含 `check`、`status`、`upgrade`、`sync`、`follow-reset`）
- **THEN** 標準輸出與標準錯誤皆不包含任何訂閱 API key 原文、解密密碼原文或讀取用 GitHub token 原文
