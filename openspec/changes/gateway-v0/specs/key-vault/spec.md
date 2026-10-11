## Purpose

把五把上游 key（Claude 個人與公司訂閱、OpenRouter、OpenCode Zen、Ollama Cloud）收在 LLMGateway 一個地方保管；客戶端經會合點的 SSH 轉發用 get 拿到，其他地方不再放 key。

## ADDED Requirements

### Requirement: key 只保管在一處
五把 key 的來源 SHALL 是 LLMGateway repo 的 GitHub secret，值由本人設定。LLMGateway SHALL 經由一個只能手動觸發的部署流程，把 key 寫到放服務那台、只有服務帳號讀得到的檔案（權限 0600）。部署流程 MUST NOT 把值放在命令列或寫進 log。

#### Scenario: 部署
- **WHEN** 本人改了某把 key 的 secret 並手動觸發部署
- **THEN** 那台的 key 檔更新，服務重讀後 get 回新值，部署的 log 裡只出現 secret 名稱

#### Scenario: 權限
- **WHEN** 檢查那台的 key 檔
- **THEN** 檔案權限是 0600，擁有者是服務帳號

### Requirement: 值不外洩
key 的值 MUST NOT 出現在 repo、服務的資料庫或稽核紀錄、服務的 log、list 的回覆、錯誤訊息。值只出現在 get 的回覆，以及由 sidecar 開的 agent 的環境。

#### Scenario: 掃描
- **WHEN** 以憑證樣式掃描 repo、服務資料庫、服務 log 與 list 的回覆
- **THEN** 沒有任何命中

### Requirement: 不做應用層認證
服務 SHALL NOT 要求應用層的憑證；能連到服務的就能 list、get、登錄冷卻。存取控制只靠 SSH：服務只綁 127.0.0.1，只能經會合點的 SSH 轉發連入（本人選定，design 決定 5a）。發現外洩時，以換掉五把 key 並重新部署因應；客戶端不持有 key，不用改。

#### Scenario: 經會合點連入
- **WHEN** 持有會合點 SSH 金鑰的客戶端經轉發呼叫 list
- **THEN** 服務回 200，不要求任何 token

#### Scenario: 換 key
- **WHEN** 本人換掉某把 key 的 secret 並重新部署
- **THEN** 之後的 get 回新值，客戶端的設定不用改

### Requirement: 服務不對外
服務 SHALL 只綁在服務那台的 127.0.0.1；MUST NOT 綁任何對外或容器網路的介面。客戶端只能經會合機（spec `connectivity`）連入。

#### Scenario: 從其他介面連
- **WHEN** 從服務那台以外、或那台的非 loopback 介面連服務的埠
- **THEN** 連不上
