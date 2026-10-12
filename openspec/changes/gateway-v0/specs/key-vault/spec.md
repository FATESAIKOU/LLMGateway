## Purpose

把五把上游 key（Claude 個人與公司訂閱、OpenRouter、OpenCode Zen、Ollama Cloud）收在 LLMGateway 一個地方保管；正本是 private repo 的加密檔，服務定期自己拉；客戶端經會合點的 SSH 轉發用 get 拿到，其他地方不再放 key。

> v5（2026-10-12）：`key 只保管在一處`搬到下面的 MODIFIED（全文更新）；新增`拉取與輪替`。`值不外洩`、`不做應用層認證`、`服務不對外`不變。

## ADDED Requirements

### Requirement: 拉取與輪替
服務 SHALL 約每 10 分鐘從 private repo 拉一次加密檔，流程固定為：拉取→比對版本（解密後內容沒變就不動）→解密→驗證格式（同 `keys.env` 五個 `KEY=VALUE`）→原子替換 `keys.env`→`lgwd` 重讀。任一步失敗 MUST 保留舊版並記 log，不讓服務斷 key。值在過程中 MUST NOT 上命令列、不寫進 log。

#### Scenario: 拉到新版
- **WHEN** private repo 的加密檔更新，本人改了某把 key
- **THEN** 下一次拉取後那台的 key 檔更新，`lgwd` 重讀後 get 回新值

#### Scenario: 拉取失敗
- **WHEN** 拉取、解密或驗證任一步失敗
- **THEN** `keys.env` 不動，服務照用舊 key，只多一行 log

### Requirement: 值不外洩
key 的值 MUST NOT 出現在 repo、服務的資料庫或稽核紀錄、服務的 log、list 的回覆、錯誤訊息。值只出現在 get 的回覆，以及由 sidecar 開的 agent 的環境。

#### Scenario: 掃描
- **WHEN** 以憑證樣式掃描 repo、服務資料庫、服務 log 與 list 的回覆
- **THEN** 沒有任何命中

### Requirement: 不做應用層認證
服務 SHALL NOT 要求應用層的憑證；能連到服務的就能 list、get、登錄冷卻。存取控制只靠 SSH：服務只綁 127.0.0.1，只能經會合點的 SSH 轉發連入（本人選定，design 決定 5a）。發現外洩時，以更新加密檔並等一次拉取因應；客戶端不持有 key，不用改。客戶端裝置（例如手機）遺失時，先由 MyLinuxPool 撤掉那把 sshproxy 金鑰，再更新加密檔等一次拉取。

#### Scenario: 經會合點連入
- **WHEN** 持有會合點 SSH 金鑰的客戶端經轉發呼叫 list
- **THEN** 服務回 200，不要求任何 token

#### Scenario: 換 key
- **WHEN** 本人更新 private repo 的加密檔並等一次拉取
- **THEN** 之後的 get 回新值，客戶端的設定不用改

### Requirement: 服務不對外
服務 SHALL 只綁在服務那台的 127.0.0.1；MUST NOT 綁任何對外或容器網路的介面。客戶端只能經會合機（spec `connectivity`）連入。

#### Scenario: 從其他介面連
- **WHEN** 從服務那台以外、或那台的非 loopback 介面連服務的埠
- **THEN** 連不上

## MODIFIED Requirements

### Requirement: key 只保管在一處
五把 key 的正本 SHALL 是 private repo 裡的加密檔（格式與加密方式見 design v5；值由本人更新，AI 不經手值）。LLMGateway SHALL 經由定期拉取（本檔「拉取與輪替」），把解密驗證過的內容寫到放服務那台、只有服務帳號讀得到的檔案（權限 0600）。拉取與解密過程中 MUST NOT 把值放在命令列或寫進 log。

#### Scenario: 部署
- **WHEN** 本人更新加密檔並等一次拉取
- **THEN** 那台的 key 檔更新，服務重讀後 get 回新值，log 裡不出現任何值

#### Scenario: 權限
- **WHEN** 檢查那台的 key 檔
- **THEN** 檔案權限是 0600，擁有者是服務帳號
