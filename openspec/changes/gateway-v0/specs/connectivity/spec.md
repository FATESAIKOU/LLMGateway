## Purpose

讓 LLMGateway 的服務與客戶端只依賴「一台連得到的 Linux 機器＋SSH」（會合點）就能連上，不依賴 MyLinuxPool：服務端自己用 ssh reverse tunnel 把埠公開到會合點並維持健康，客戶端自備本機通道（sidecar 直接 curl 本機 port）。

> v5（2026-10-12）：`客戶端連線程式`搬到下面的 REMOVED；新增`客戶端自備通道`。服務端隧道守護、探針、會合點三條不變。

## ADDED Requirements

### Requirement: 服務端的隧道守護
服務那台 SHALL 有一個隧道守護程式，用它自己的 SSH 連線，把服務的 API 埠（以及部署用的 SSH 埠）以 reverse tunnel 公開到會合點的 127.0.0.1。連線斷掉或公開失敗時 MUST 結束並由程序管理器重開。這個連線 MUST NOT 和其他系統的 SSH 隧道共用。會合點、金鑰、遠端埠由設定檔指定，不寫在程式裡。

#### Scenario: 斷線重連
- **WHEN** 守護程式與會合點的 SSH 連線中斷
- **THEN** 守護程式結束、被重新啟動，重新建立 reverse tunnel

#### Scenario: 遠端埠被佔用
- **WHEN** 會合點上的遠端埠還被舊連線佔著
- **THEN** 新連線公開失敗即結束並重試，不會在「沒有公開成功」的狀態下繼續跑

### Requirement: 端對端健康探針
守護程式 SHALL 定期經會合點對服務的 API 埠發一次 list 請求，收到 HTTP 200 才算健康；連續失敗達門檻時 MUST 重建隧道。只確認 TCP 連線存在不算健康。

#### Scenario: TCP 通但服務不通
- **WHEN** SSH 連線還在，但經會合點打 API 埠連續 3 次沒有 HTTP 回應
- **THEN** 守護程式重建隧道

### Requirement: 客戶端自備通道
sidecar SHALL 自己把會合點 API 埠的 HTTP 轉發放到本機 loopback 的某個 port，再用 `lgw.client`（base URL 由呼叫方給）打 list／get／登錄冷卻。本機 port 預設為 4710，建通道的一方可經設定改（worker 由 MyLinuxPool 的 capability script 建通道到這個 port；Mac／手機用 mlp 或 ssh -J 自己轉發）。LLMGateway SHALL NOT 提供連線程式。轉發斷掉時客戶端 MUST 回報「無法確認」，不讓呼叫方當掉。

#### Scenario: 直接打本機
- **WHEN** 本機已有到會合點 API 埠的轉發
- **THEN** 客戶端直接對本機 port 做 list／get／登錄冷卻，不經任何連線程式

#### Scenario: worker 本機 port 預設值
- **WHEN** 通道由 MyLinuxPool 的 capability script 建立且沒有另行設定
- **THEN** 本機 port 為 4710，sidecar 照此打 base URL

#### Scenario: 通道斷了
- **WHEN** 本機轉發斷掉
- **THEN** 客戶端回報「無法確認」，由呼叫方等一下再試

### Requirement: 會合點與遠端埠
會合點 SHALL 是用設定指定的一台 SSH 主機與帳號；v0 是池跳板機上現有的 sshproxy 帳號（本人指定）。遠端埠 MUST 只綁會合點的 loopback，並避開 2220–2499 與 3000–3999；預設 API 埠 4710、SSH 埠 4711。服務端與客戶端都 MUST NOT 依賴 MyLinuxPool 的 pool-tunnel 或 NODE_* 設定。

#### Scenario: 換一個會合點
- **WHEN** 把設定改成另一台 SSH 主機
- **THEN** 隧道守護改連那台、客戶端自備通道跟著改，程式不用改

## REMOVED Requirements

### Requirement: 客戶端連線程式
**Reason**: 本人 2026-10-12 的裁定（v5.2）：拿掉 `lgw connect` 通用程式；sidecar 直接 curl 本機的 port，通道由 MyLinuxPool capability／mlp／ssh -J 提供。
**Migration**: 改由本檔新增的「客戶端自備通道」：LLMGateway 只規定本機 loopback 某 port 即 HTTP API，不再提供連線程式與 `connect.json` 格式。
