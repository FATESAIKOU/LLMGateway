## MODIFIED Requirements

### Requirement: 客戶端自備通道
sidecar SHALL 自己把會合點 API 埠的 HTTP 轉發放到本機 loopback 的某個 port，再用 `lgw.client`（base URL 由呼叫方給）打 list／resolve／登錄冷卻。本機 port 預設為 4710，建通道的一方可經設定改（worker 由 MyLinuxPool 的 capability script 建通道到這個 port；Mac 用 mlp 或 ssh -J 自己轉發；手機自行用 ssh -J 等方式轉發，LLMGateway 不提供程式）。LLMGateway SHALL NOT 提供連線程式。轉發斷掉時客戶端 MUST 回報「無法確認」，不讓呼叫方當掉。

#### Scenario: 直接打本機
- **WHEN** 本機已有到會合點 API 埠的轉發
- **THEN** 客戶端直接對本機 port 做 list／resolve／登錄冷卻，不經任何連線程式

#### Scenario: worker 本機 port 預設值
- **WHEN** 通道由 MyLinuxPool 的 capability script 建立且沒有另行設定
- **THEN** 本機 port 為 4710，sidecar 照此打 base URL

#### Scenario: 通道斷了
- **WHEN** 本機轉發斷掉
- **THEN** 客戶端回報「無法確認」，由呼叫方等一下再試
