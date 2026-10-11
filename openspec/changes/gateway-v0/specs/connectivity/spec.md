## Purpose

讓 LLMGateway 的服務與客戶端只依賴「一台連得到的 Linux 機器＋SSH」（會合點）就能連上，不依賴 MyLinuxPool：服務端自己用 ssh reverse tunnel 把埠公開到會合點並維持健康，客戶端用 LLMGateway 提供的連線程式（可經跳板）連過去。

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
守護程式 SHALL 定期經會合點對服務的 API 埠發一次請求，收到任何 HTTP 回應才算健康；連續失敗達門檻時 MUST 重建隧道。只確認 TCP 連線存在不算健康。

#### Scenario: TCP 通但服務不通
- **WHEN** SSH 連線還在，但經會合點打 API 埠連續 3 次沒有 HTTP 回應
- **THEN** 守護程式重建隧道

### Requirement: 客戶端連線程式
LLMGateway SHALL 提供客戶端連線程式（函式庫與指令），依設定經零到多個跳板連到會合點，在本機開一個轉發到會合點上 API 埠的連線，回傳本機的 base URL。連不上時 MUST 回報「無法確認」，不讓呼叫方當掉。設定 SHALL 包含：跳板清單、會合點、金鑰路徑、known_hosts 路徑、遠端 API 埠。worker 用 pool-tunnel 既有的 sshproxy 金鑰，LLMGateway 不另外發金鑰給客戶端。

#### Scenario: 經跳板連線
- **WHEN** 設定了一個跳板與會合點
- **THEN** 連線程式經跳板連到會合點，回傳 `http://127.0.0.1:<本機埠>`，之後的 list／get／登錄冷卻都走這個位址

#### Scenario: 連不上
- **WHEN** 會合點或跳板連不上
- **THEN** 連線程式回報「無法確認」，不丟出未處理的例外

### Requirement: 會合點與遠端埠
會合點 SHALL 是用設定指定的一台 SSH 主機與帳號；v0 是池跳板機上現有的 sshproxy 帳號（本人指定）。遠端埠 MUST 只綁會合點的 loopback，並避開 2220–2499 與 3000–3999；預設 API 埠 4710、SSH 埠 4711。服務端與客戶端都 MUST NOT 依賴 MyLinuxPool 的 pool-tunnel 或 NODE_* 設定。

#### Scenario: 換一個會合點
- **WHEN** 把設定改成另一台 SSH 主機
- **THEN** 隧道守護與客戶端連線程式改連那台，程式不用改
