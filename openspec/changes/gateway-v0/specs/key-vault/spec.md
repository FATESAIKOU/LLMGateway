## Purpose

把五把上游 key（Claude 個人與公司訂閱、OpenRouter、OpenCode Zen、Ollama Cloud）收在 LLMGateway 一個地方保管，讓 worker、Mac 與之後的手機都只在租約期間透過租用拿到其中一把，其他地方不再放 key。

## ADDED Requirements

### Requirement: key 只保管在一處
五把 key 的來源 SHALL 是 LLMGateway repo 的 GitHub secret，值由本人設定。LLMGateway SHALL 經由一個只能手動觸發的部署流程，把 key 寫到池內常駐 provider 上、只有服務帳號讀得到的檔案（權限 0600）。部署流程 MUST NOT 把值放在命令列或寫進 log。

#### Scenario: 部署
- **WHEN** 本人改了某把 key 的 secret 並手動觸發部署
- **THEN** provider 上的 key 檔更新，服務重讀後新出租的租約帶新值，部署的 log 裡只出現 secret 名稱

#### Scenario: 權限
- **WHEN** 檢查 provider 上的 key 檔
- **THEN** 檔案權限是 0600，擁有者是服務帳號

### Requirement: 值不外洩
key 的值 MUST NOT 出現在 repo、服務的事件紀錄、服務的 log、狀態板、錯誤訊息或任何客戶端的檔案與 log。值只出現在核准租約時給客戶端的那一次回覆，以及 agent 的環境。

#### Scenario: 掃描
- **WHEN** 以憑證樣式掃描 repo、事件紀錄、服務 log 與狀態板的輸出
- **THEN** 沒有任何命中

#### Scenario: 錯誤時
- **WHEN** 某個上游的狀態查詢失敗
- **THEN** 服務記下錯誤類型與 HTTP 狀態碼，不記下請求標頭或含 key 的內容

### Requirement: 客戶端憑證
每個客戶端（每台 worker 的 sidecar、Mac、之後的手機）SHALL 持有自己的客戶端憑證，用來向服務證明身分。服務 MUST 只保存客戶端憑證的雜湊，並能單獨撤銷任一客戶端。客戶端憑證不是上游 key，不能直接打任何上游。

#### Scenario: 撤銷
- **WHEN** 某台 worker 的客戶端憑證被撤銷
- **THEN** 該客戶端所有未結束的租約記為 revoked，之後的申請、續期、回報都被拒，其他客戶端不受影響

#### Scenario: 核對憑證
- **WHEN** 持有有效客戶端憑證的客戶端呼叫 whoami
- **THEN** 服務回 200 與該客戶端的名稱，不產生租約事件，也不打任何上游

#### Scenario: 沒有憑證
- **WHEN** 請求沒有帶客戶端憑證或帶錯
- **THEN** 服務回 401，不留下租約事件

### Requirement: 服務不對外
租用服務 SHALL 只綁在 provider 的 loopback，以及池內客戶端連得到的內部介面；MUST NOT 對網際網路開放。Mac 與手機 SHALL 經池的 SSH 跳板以埠轉送連入。

#### Scenario: 從外部連
- **WHEN** 從池外直接連服務的埠
- **THEN** 連不上
