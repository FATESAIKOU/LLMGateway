## Purpose

把五把上游 key（Claude 個人與公司訂閱、OpenRouter、OpenCode Zen、Ollama Cloud）收在 LLMGateway 一個地方保管；只有持有讀取 token 的客戶端能經 get 拿到，其他地方不再放 key。

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

### Requirement: 一類機器一把讀取 token
服務 SHALL 以「一類機器一把」的讀取 token 認證客戶端：
- 池的 worker 共用一把（`pool-worker`）；
- Mac 一把；
- 之後的手機一把。

這些 token 與 key 一起由部署流程放到那台，服務只保存雜湊。換掉某一類的 token（改 secret、重新部署）SHALL 讓舊的立即失效。沒有發證或撤證的 API。

#### Scenario: 沒有 token
- **WHEN** 請求沒有帶 token 或帶錯
- **THEN** 服務回 401，不回任何 subscription 資料

#### Scenario: 換掉 worker 的 token
- **WHEN** 本人換掉 `pool-worker` 的 token 並重新部署
- **THEN** 舊 token 之後一律 401，新 token 可以用

### Requirement: 服務不對外
服務 SHALL 只綁在那台的 loopback，以及同一台容器看得到的 docker bridge 位址；MUST NOT 對網際網路開放。Mac 與手機 SHALL 經池的 SSH 跳板以埠轉送連入；別台的 worker 經 MyLinuxPool 的隧道連入。

#### Scenario: 從外部連
- **WHEN** 從池外直接連服務的埠
- **THEN** 連不上
