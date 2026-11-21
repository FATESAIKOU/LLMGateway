<!-- 條件歸檔說明：本 delta 中與 `FOLLOW_FILE`、`host_key`、`generation`、`--probe-once` 只讀相關之條款與 Scenario **僅於 Q2＝(1)/(3) 時歸檔**；若本人／PM 依 Q2＝(2)（取消 P2-B）定案，執行 `openspec archive` 前須先依 `tasks.md` 3.3 刪除該等條款。 -->

## ADDED Requirements

### Requirement: 服務本體啟停與無隧道獨立生命週期
管理介面 `llmgwctl` SHALL 支援不依賴任何外部隧道的純本地生命週期操作，包含冪等之 `start`、`stop`、`restart` 與既有 `install`、`upgrade`、`rollback`、`status`、`check`、`sync`。當機器未安裝或未設定隧道守護時，本地服務檢查與升級 MUST 正常通過；當機器存在隧道 unit 時，`check` 與 `status` SHALL 照常納入檢查（`--no-remote` 除外），且若已設定 `FOLLOW_FILE` 但 follow 檔缺失、無效或 generation 回退且隧道未運行，`check` MUST 回報失敗而非靜默忽略（非 root 無權限讀取時降為無法確認且不誤報缺失）。所有操作與輸出 MUST NOT 包含金鑰原文或解密密碼。

#### Scenario: 冪等啟停本地服務
- **WHEN** `llmgateway.service` 已啟動且本機 `/v1/subscriptions` 回 HTTP 200 時再次執行 `start`（或已停止時執行 `stop`）
- **THEN** 指令不重複動作且以退出碼 `0` 結束

#### Scenario: 無隧道純本地檢查通過
- **WHEN** 機器未安裝且未設定隧道守護，執行 `llmgwctl check`
- **THEN** 本機服務、本機 API 200、`keysync`、`policy` 與 `gh-keys-repo` 全數通過後即回退出碼 `0`

#### Scenario: 隧道存在時仍受監看
- **WHEN** 機器上已安裝隧道 unit（過渡期或已啟用），執行 `llmgwctl check`（未帶 `--no-remote`）
- **THEN** 檢查仍回報該隧道 unit 狀態與遠端探針結果，不因隧道改為可選而靜默漏檢

#### Scenario: 已設 FOLLOW_FILE 但檔案缺失、無效或回退時回報失敗
- **WHEN** `tunnel.env` 已設定 `FOLLOW_FILE` 但該檔案不存在、無效或 `generation` 回退，且 `llmgateway-tunnel.service` 未處於運行狀態（且呼叫者具讀取權限）
- **THEN** `llmgwctl check` 回報 `fail`（退出碼非 `0`）並指出對應異常原因，`llmgwctl status` 亦顯示該 follow 檔狀態（`missing`／`invalid`／`regressed`）而不洩漏 IP 或金鑰

#### Scenario: 非 root 無權限讀取 FOLLOW_FILE 時不誤報缺失
- **WHEN** 非 root 呼叫者因 `STATE_DIR` 權限限制無法讀取 `FOLLOW_FILE` 而執行 `llmgwctl status` 或 `check`
- **THEN** `status` 顯示「需 root 或 llmgw 才能讀」且 MUST NOT 誤報為 `missing`，`check`（隧道未運行時）降為 `todo` 並註明需權限

#### Scenario: 管理輸出不洩漏金鑰與密碼
- **WHEN** 執行 `llmgwctl` 任一子命令（含 `check`、`status`、`upgrade`、`sync`）
- **THEN** 標準輸出與標準錯誤皆不包含任何訂閱 API key 原文或解密密碼原文

## RENAMED Requirements

- FROM: `### Requirement: 服務端的隧道守護`
- TO: `### Requirement: 服務端的可選會合點隧道守護`

## MODIFIED Requirements

### Requirement: 服務端的可選會合點隧道守護
會合點隧道守護在服務端為可選元件，未啟用時 MUST NOT 阻礙 `lgwd` 運作。啟用時，守護程式 SHALL 以自有金鑰建立獨立 SSH reverse tunnel 公開本機埠到會合點 `127.0.0.1`；其位址與 `host_key` MUST 支援從本機 follow 檔（`ip`、`ssh_port`、`tunnel_user`、`host_key`、`generation`）解析並自動寫入私用 `known_hosts`（`--probe-once` 只讀不寫），採 `StrictHostKeyChecking=yes` 與 `ExitOnForwardFailure=yes`。設 `FOLLOW_FILE` 時優先於靜態鍵，檔缺失、無效、`host_key` 缺漏或 `generation` 回退時 MUST fail-closed 拒連並以非 `78` 退出碼重試，MUST NOT 退回靜態鍵或 `accept-new`；僅靜態鍵缺失回退出碼 `78`。日誌與狀態輸出 MUST NOT 洩漏 IP 或金鑰原文。

#### Scenario: 斷線重連
- **WHEN** 隧道守護與會合點的 SSH 連線中斷
- **THEN** 守護程式結束、被重新啟動，並依最新設定或 follow 檔重新建立 reverse tunnel

#### Scenario: follow 檔變更後重連
- **WHEN** 本機 follow 檔的會合點位址、`ssh_port`、`host_key` 更新且 `generation` 嚴格遞增
- **THEN** 守護程式結束舊連線並以非 `0`（非 `78`）退出觸發重啟，重讀最新 follow 檔、原子更新私用 `known_hosts` 並於目標恢復時間（≤ 3 分鐘）內重建 reverse tunnel

#### Scenario: 遠端埠被佔用
- **WHEN** 會合點上的遠端埠還被舊連線佔著
- **THEN** 新連線因 `ExitOnForwardFailure=yes` 立即失敗結束並重試，不在「沒有公開成功」的狀態下繼續跑

#### Scenario: host_key 缺漏或不符合時拒絕連線
- **WHEN** follow 檔未提供 `host_key`，或會合點出示的主機金鑰與 pinned 值不符
- **THEN** 守護程式因 `StrictHostKeyChecking=yes` 或 fail-closed 檢查拒絕連線並以退出碼 `1` 結束重試，不退回 `accept-new` 亦不退回靜態設定

#### Scenario: follow 檔缺失或無效時不永久停擺並於修復後自癒
- **WHEN** `tunnel.env` 已設定 `FOLLOW_FILE`，但啟動或重連前該 follow 檔缺失、JSON 損毀或 `generation` 回退（小於已套用世代）
- **THEN** 守護程式記錄對應事件（回退記 `follow_regressed`）並以退出碼 `1`（非 `78`）結束，不退回靜態設定或 `accept-new`；待檔案寫入或修復為有效較高世代後，於程序管理器重試時自動建通

#### Scenario: 同代覆寫改變連線欄位時拒絕套用
- **WHEN** 本機 follow 檔的 `generation` 與已套用世代相同，但 `ip`、`ssh_port`、`tunnel_user` 或 `host_key` 任一欄位變更
- **THEN** 守護程式記錄 `follow_invalid` 並以退出碼 `1` 拒絕套用，不退回靜態設定或 `accept-new`

#### Scenario: 運行中 follow 檔變壞時不中斷健康連線
- **WHEN** 隧道已健康運行中而本機 follow 檔暫時缺失、JSON 損毀或 `generation` 回退
- **THEN** 守護程式僅於狀態轉換時記錄對應事件而不主動終止現有健康 SSH 連線，待連線實際斷開或探針失敗重啟時再依 fail-closed 規則處理

#### Scenario: 只有靜態設定鍵缺失才以退出碼 78 停止重試
- **WHEN** `tunnel.env` 缺少 `IDENTITY`（或 `FOLLOW_FILE` 與靜態 `RENDEZVOUS` 皆未設定）即啟動隧道守護
- **THEN** 守護程式僅記錄 `config_missing` 事件並以退出碼 `78` 結束，且日誌與狀態輸出不含真實會合點 IP、金鑰內容或 ssh 原始除錯細節

#### Scenario: 探針只讀不寫私用 known_hosts
- **WHEN** 以 `--probe-once` 執行健康探針而私用 `known_hosts` 尚未建立或與當前 follow 檔不符
- **THEN** 探針不建立或不覆寫私用 `known_hosts`，並回傳退出碼 `2`（無法探）

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
會合點 SHALL 是由靜態設定或本機 follow 檔（`ip`、`ssh_port`、`tunnel_user`、`host_key`、`generation`）指定的一台 SSH 主機與帳號；v0 是池跳板機上現有的 sshproxy 帳號（本人指定）。遠端埠 MUST 只綁會合點的 loopback，並避開 2220–2499 與 3000–3999；預設 API 埠 4710、管理 SSH 埠預設為 4711（未設或留空時仍維持預設 4711，須顯式設關閉值如 `REMOTE_SSH=off` 才不轉發；切換過渡期保持開啟並於帶外驗收後由本人決定關閉）。服務端與客戶端都 MUST NOT 依賴 MyLinuxPool 的 pool-tunnel 或 NODE_* 設定。

#### Scenario: 換一個會合點
- **WHEN** 本機 follow 檔更新為另一台 SSH 主機的位址、`ssh_port`、`host_key` 與遞增之 `generation`
- **THEN** 隧道守護自動重讀並改連那台新會合點、客戶端自備通道跟著改，服務端與隧道程式不用改

#### Scenario: 管理 SSH 埠留空維持預設 4711
- **WHEN** 隧道設定未填 `REMOTE_SSH` 或維持留空（`REMOTE_SSH=`）
- **THEN** 守護程式依預設同時轉發管理 SSH 埠 `4711`，升級切換不致意外失去管理通道

#### Scenario: 關閉管理 SSH 埠轉發
- **WHEN** 隧道設定明確指定關閉管理 SSH 遠端埠（如 `REMOTE_SSH=off`）
- **THEN** 守護程式只轉發 API 埠，不在會合點開啟 4711
