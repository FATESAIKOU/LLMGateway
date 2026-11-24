# connectivity Specification

## Purpose
讓 LLMGateway 的服務與客戶端只依賴「一台連得到的 Linux 機器＋SSH」（會合點）就能連上，不依賴 MyLinuxPool 的 `pool-tunnel` 與其他 `NODE_*` 設定：服務端自己用 ssh reverse tunnel 把埠公開到會合點並維持健康，客戶端自備本機通道（sidecar 直接 curl 本機 port）。**唯一例外**：僅在斷線恢復（連不上或無可用位址）時，服務端隧道守護才可唯讀讀取 MyLinuxPool 的 `NODE_GATEWAY`（限位址相關欄位 `ip`、`port`、`host_key`、`generation`，並比對 `tunnel_user`），用來跟隨會合點輪換；連線健康時不碰它。

> v5（2026-10-12）：`客戶端連線程式`搬到下面的 REMOVED；新增`客戶端自備通道`。服務端隧道守護、探針、會合點三條不變。探針已於 #246 改為 `/healthz`（依版本內的能力旗標 `deploy/capabilities.json` 決定 old／new；old 先探 `/healthz`、404 才退回 `/v1/subscriptions`）。

> v6（2026-11-21，#221）：隧道守護改為可選，並可在斷線恢復時唯讀讀取 `NODE_GATEWAY` 跟隨輪換（見 Purpose 的唯一例外）；新增服務本體啟停（`start`／`stop`／`restart`／`follow-reset`）與無隧道獨立生命週期。

> 2026-11-24（#222 D3c）：上面「探針」那句「不變」是 v5 當時的話；下面「端對端健康探針」那條 Requirement 還寫著 `GET /v1/subscriptions`，是尚未歸檔的舊文。change `lgw-resolve-auth` 的 delta 已改為 `GET /healthz`（#246 起部署的探針就是 `/healthz`：old 模式且 `/healthz` 回 404 才退回 list），歸檔時由 delta 取代該 Requirement；本註記不改 Requirement。

## Requirements

### Requirement: 服務端的可選會合點隧道守護
會合點隧道守護為可選（出貨 unit 不含 `LoadCredential=`），未啟用 MUST NOT 阻礙 `lgwd`。啟用時採 `StrictHostKeyChecking=yes`、`ExitOnForwardFailure=yes`、`ConnectTimeout` 與 `-l`/`--` 參數隔離，SSH 使用者以本機為準。健康時 MUST NOT 輪詢 GitHub；連不上或無位址時同行程安全抓 `NODE_GATEWAY`（限官方端點、逾時/上限、禁跨主機轉址），驗無 zone id 正規化 IP／`\Z` 主機名、非布林整數 `port`、一致 `tunnel_user`、wire-format `host_key` 與雙水位 `generation`（`applied`／`verified`）後重連，首探針 200 才寫 `.tunnel_generation` JSON 並歸零。同代未變不累加亦不重建；失敗／讀不到保留位址寫分類回 `1`（全缺或未授權端點 `78`）。`--probe-once` 與 `--fetch-check` 只讀且日誌不洩密。

#### Scenario: 連線健康時不輪詢 GitHub
- **WHEN** 隧道 SSH 連線與端對端 HTTP 200 探針皆維持健康，經過多個探針週期
- **THEN** 守護程式僅執行 `ssh -W` 健康探針，不對 GitHub API 發出任何請求

#### Scenario: 斷線重連
- **WHEN** 隧道 SSH 連線中斷（或探針連續失敗達門檻）且不在退避冷卻期
- **THEN** 守護程式在同一個行程內以唯讀 GitHub key 抓取目標位址與 `host_key`，驗證格式合法、與本機 SSH 使用者一致且 `generation` 高於 `.tunnel_generation` 已驗證水位後原子更新本機 follow 檔與私用 `known_hosts`（IP 一律使用正規化字串），隨即同行程以 `-o ConnectTimeout` 與 `-l`/`--` 重新建立 reverse tunnel，並於通過首次探針 200 後寫入 `.tunnel_generation` JSON 及歸零失敗計數

#### Scenario: 遠端埠被佔用
- **WHEN** 會合點上的遠端埠還被舊連線佔著
- **THEN** 新連線因 `ExitOnForwardFailure=yes` 立即失敗結束並重試，不在「沒有公開成功」的狀態下繼續跑

#### Scenario: 惡意或格式無效欄位與帳號不符時拒收且不執行 ssh
- **WHEN** `NODE_GATEWAY` 的 `tunnel_user` 或 `ip` 以 `-` 開頭、含 IPv6 zone id（`%`）、尾端換行或為 JSON 整數、`ip` 為最後一個 label 全為數字的主機名（如 `16909060`、`0x7f.1`、`1.2.3`，即使 `inet_aton` 可解析）、`tunnel_user` 與本機設定不符、`port`／`generation` 為布林值或越界／超長，或 `host_key` 含換行、第三欄、非標準 padding 或無效 wire-format blob
- **THEN** 守護程式拒收並記錄對應無效事件、不更新 follow 檔亦不帶入 `ssh` 子行程執行，保留最後已知位址以退出碼 `1` 結束重試

#### Scenario: host_key 缺漏或不符合時 fail-closed 拒連
- **WHEN** 抓取結果或 follow 檔未提供合法 `host_key`，或會合點出示的主機金鑰與 pinned 值不符
- **THEN** 守護程式拒絕套用或拒絕連線並以退出碼 `1` 結束重試，不退回 `accept-new` 亦不覆寫最後已知有效位址

#### Scenario: GitHub 抓取失敗、同代未變或回退至已驗證世代之處理
- **WHEN** 連不上觸發抓取時遇 GitHub API 不可達、金鑰檔讀不到（`credential_unreadable`）、`401`／無 rate-limit 標頭之 `403`（`auth_failed`）、`429`／限流、回應超限或格式無效，或抓回與 `applied`（follow 檔）同代同欄位，或先前套用新代尚未驗通而上游回退抓回與 `.tunnel_generation` 指紋相符的已驗證世代
- **THEN** 失敗或被拒時寫入抓取分類並累加退避計數，保留最後已知有效位址以退出碼 `1` 重試；與 `applied` 同代同欄位時不累加失敗計數亦不重建而僅設最短冷卻後交重試；回到指紋相符之已驗證世代時合法恢復本機檔案並同行程重建連線

#### Scenario: generation 低於已驗證世代或同代篡改時拒絕套用
- **WHEN** 抓取結果或本機 follow 檔的 `generation` 小於 `.tunnel_generation` 已驗證世代，或同代下連線欄位與已驗證指紋／已套用欄位不符
- **THEN** 守護程式記錄 `follow_regressed` 或 `follow_invalid`、寫入最近抓取分類、拒絕套用並維持最後已知位址，以退出碼 `1` 結束重試，不退回 `accept-new`

#### Scenario: 運行中 follow 檔變壞時不中斷健康連線
- **WHEN** 隧道已健康運行中而本機 follow 檔暫時缺失、JSON 損毀或 `generation` 回退
- **THEN** 守護程式僅於狀態轉換時記錄對應事件而不主動終止現有健康 SSH 連線，待連線實際斷開或探針失敗重啟時再依 fail-closed 規則處理

#### Scenario: 半套 GitHub 設定於靜態有效時不停機
- **WHEN** 靜態設定完整（或 `FOLLOW_FILE` 有效）但 GitHub 輸入僅填部分鍵
- **THEN** 守護程式不以 `78` 退出，照靜態設定建立隧道並記錄 `config_partial` 事件供管理查詢

#### Scenario: 只有必要設定完全缺失或端點未授權才以退出碼 78 停止重試
- **WHEN** `tunnel.env` 缺少 `IDENTITY`、缺少本機登入使用者、初始靜態位址與 GitHub 位址讀取設定兩者皆未設定（且無有效 follow 檔），或未設測試旗標／非 loopback 卻注入 `LGW_GATEWAY_API_BASE` 即啟動隧道守護
- **THEN** 守護程式僅記錄 `config_missing` 事件並以退出碼 `78` 結束，且日誌與狀態輸出不含真實會合點 IP、金鑰、token 或 ssh 原始除錯細節

#### Scenario: 探針與手動抓取驗證皆只讀不寫本機狀態
- **WHEN** 以 `--probe-once` 執行健康探針而私用 `known_hosts` 尚未建立或不符，或人工執行 `--fetch-check` 驗證遠端 `NODE_GATEWAY`
- **THEN** `--probe-once` 不改寫任何狀態檔且不打 GitHub 直接回傳 `2`（無法探）；`--fetch-check` 僅打一次 GitHub 驗證 200／欄位格式／標頭並輸出無祕密摘要（成功時提示若 `check` 仍紅可執行 `follow-reset --fetch-state`），不寫入任何本機狀態檔

### Requirement: 端對端健康探針
隧道守護啟用時，SHALL 定期經會合點對服務的 API 埠發一次 `GET /v1/subscriptions` 請求，收到 HTTP 200 才算健康；連續失敗達門檻（預設 3 次）時 MUST 重建隧道。只確認 TCP 連線存在不算健康。當服務未啟用隧道時，本機檢查只需驗證 `127.0.0.1:<本地API埠>` 回 HTTP 200，不要求遠端會合點探針。

#### Scenario: TCP 通但服務不通
- **WHEN** SSH 連線還在，但經會合點打 API 埠連續 3 次沒有 HTTP 200 回應
- **THEN** 守護程式重建隧道

#### Scenario: 未啟用隧道時只驗本機
- **WHEN** 機器未啟用隧道守護而執行健康檢查
- **THEN** 驗證本機 API 埠回 HTTP 200 即通過，不觸發遠端探針

### Requirement: 客戶端自備通道
sidecar SHALL 自己把會合點 API 埠的 HTTP 轉發放到本機 loopback 的某個 port，再用 `lgw.client`（base URL 由呼叫方給）打 list／usage／resolve／roles／登錄冷卻。本機 port 預設為 4710，建通道的一方可經設定改（worker 由 MyLinuxPool 的 capability script 建通道到這個 port；Mac 用 mlp 或 ssh -J 自己轉發；手機自行用 ssh -J 等方式轉發，LLMGateway 不提供程式）。無會合點時，客戶端亦可在本地直連服務。LLMGateway SHALL NOT 提供連線程式。轉發斷掉或本地服務不可達時客戶端 MUST 回報「無法確認」（`unreachable`），不讓呼叫方當掉。

#### Scenario: 直接打本機
- **WHEN** 本機已有到會合點 API 埠的轉發，或本地已直接啟動服務
- **THEN** 客戶端直接對本機 port 做 list／usage／resolve／roles／登錄冷卻，不經任何連線程式

#### Scenario: worker 本機 port 預設值
- **WHEN** 通道由 MyLinuxPool 的 capability script 建立且沒有另行設定
- **THEN** 本機 port 為 4710，sidecar 照此打 base URL

#### Scenario: 通道斷了
- **WHEN** 本機轉發斷掉或本地服務不可達
- **THEN** 客戶端回報「無法確認」（`unreachable`），由呼叫方等一下再試

### Requirement: 會合點與遠端埠
會合點 SHALL 由本機登入資訊與初始設定或連線失敗時唯讀抓取之 `NODE_GATEWAY`（正規化寫入本機 follow 檔）指定一台 SSH 主機與帳號。遠端埠 MUST 只綁會合點 loopback，避開 2220–2499 與 3000–3999；預設 API 埠 `4710`、管理 SSH 埠一律保留預設 `4711`（留空亦維持 `4711`）。服務端與客戶端 MUST NOT 依賴 MyLinuxPool 的 `pool-tunnel` 與其他 `NODE_*` 設定；唯一例外為服務端在連線失敗時唯讀讀取 `NODE_GATEWAY` 之位址相關欄位（`ip`、`port`、`host_key`、`generation`）與比對 `tunnel_user`。

#### Scenario: 換一個會合點
- **WHEN** `NODE_GATEWAY` 更新為另一台 SSH 主機的位址、`port`、`host_key`（含一致之 `tunnel_user`）與遞增之 `generation`
- **THEN** 隧道守護於斷線或探針失敗時自動向 GitHub 抓取並改連新會合點、客戶端自備通道跟著改，服務端與隧道程式不用改

#### Scenario: 管理 SSH 埠一律保留預設 4711
- **WHEN** 隧道設定未填 `REMOTE_SSH` 或維持留空（`REMOTE_SSH=`）或設為 `4711`
- **THEN** 守護程式同時轉發 API 埠 `4710` 與管理 SSH 埠 `4711` 至當前會合點

#### Scenario: 不接受關閉管理 SSH 埠
- **WHEN** 隧道設定嘗試將 `REMOTE_SSH` 設為 `off`（或非有效埠號值）
- **THEN** 守護程式拒絕該無效設定並以退出碼 `78` 停止，不關閉 `4711` 轉發

#### Scenario: 不讀取其他 NODE_* 欄位與設定
- **WHEN** `NODE_GATEWAY` 內夾帶 `user`、`key_secret`、`ssh_listen_ports`、`ports` 或遠端存在其他 `NODE_*` 變數
- **THEN** 守護程式忽略該等非位址欄位且不使用 `pool-tunnel` 設定

### Requirement: 服務本體啟停與無隧道獨立生命週期
管理介面 `llmgwctl` SHALL 支援不依賴隧道的純本地 `start`／`stop`／`restart`（冪等）與既有子命令，並提供需 root 的 `follow-reset`（含 `--fetch-state` 僅清抓取與退避）重置狀態；無隧道時檢查與升級 MUST 通過。存在隧道 unit 時，`check`／`status` SHALL 檢查（`--no-remote` 除外）；follow 異常或抓取被拒且隧道未跑 MUST 回 `fail`，運行中異常、`auth_failed` 或金鑰讀不到至少回 `todo`，非 root 無權限降為無法確認而不誤報。未運行遇 `auth_failed` 回 `fail`、運行中回 `todo`；到期門檻內或格式不合回 `todo`（不合理標頭視為未知）。輸出 MUST NOT 洩密。

#### Scenario: 冪等啟停本地服務
- **WHEN** `llmgateway.service` 已啟動且本機 `/v1/subscriptions` 回 HTTP 200 時再次執行 `start`（或已停止時執行 `stop`）
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
