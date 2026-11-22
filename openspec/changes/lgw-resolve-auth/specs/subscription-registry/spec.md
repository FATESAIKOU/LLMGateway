## MODIFIED Requirements

### Requirement: 稽核紀錄

每次**成功**（200）的 resolve 與每次登錄冷卻 SHALL 記一筆只追加的紀錄：時刻、subscription id、agent 種類（resolve）、provider 與 model（resolve；登錄冷卻記 `model`，沒給時為 null）、結束時刻 `until`（登錄冷卻記有效的 `until`），以及 nullable 的 `principal`：只有通過簽章驗證者記節點 principal，未驗證與舊列為 null，MUST NOT 記客戶端自稱的身分。紀錄 MUST NOT 含憑證的值或 `env` 的憑證值；provider／model 是中介資料，SHALL 記。失敗的回覆（4xx／409）不記稽核，診斷靠只含方法／路徑／狀態碼的 HTTP log，認證失敗另記不含祕密的安全事件。`action` 為 `get` 的歷史列保留不刪，移除後不再寫。

#### Scenario: resolve 留下紀錄
- **WHEN** 有客戶端以 `agent=opencode&provider=opencode-zen&model=big-pickle` resolve
- **THEN** 稽核紀錄有一筆（subscription、agent、provider、model），內容沒有憑證值或 env 的憑證值

#### Scenario: get 留下紀錄
- **WHEN** 呼叫已移除的單筆路徑（`GET /v1/subscriptions/{id}?agent=`，LLMGateway#48）
- **THEN** 回 404，稽核紀錄不新增任何一筆

#### Scenario: 舊 DB 沒有新欄位
- **WHEN** 以 v5 schema 的既有 DB 檔啟動新 lgwd，並寫入一筆 resolve
- **THEN** 啟動時補上 `provider`／`model` 欄位，寫入成功；舊資料的這兩個欄位為 null

#### Scenario: 舊 DB 補 principal 欄
- **WHEN** 以沒有 `principal` 欄的既有 DB 檔啟動新 lgwd，再寫入一筆已驗證的 resolve
- **THEN** 啟動時補上 `principal` 欄、舊列（含歷史 `get`）保留且該欄為 null，新列記驗證後的 principal

#### Scenario: 模型冷卻留下紀錄
- **WHEN** 以 `model=mimo-v2.6-flash-free` 登錄 `opencode-zen` 的冷卻
- **THEN** 稽核紀錄的 `action` 是 `cooldown`、`model` 是該模型 id、`until` 是有效值、`agent` 為 null

#### Scenario: 冷卻沒給 model
- **WHEN** 登錄冷卻時沒有給 `model`
- **THEN** 稽核紀錄的 `model` 為 null

#### Scenario: 未認證不記自稱身分
- **WHEN** off／warn 未驗證的請求成功，且請求自稱某個身分
- **THEN** principal 為 null，不使用自稱身分

#### Scenario: 認證失敗不進稽核
- **WHEN** enforce 下請求回 401 或 403
- **THEN** 稽核紀錄不新增任何一筆，只留不含祕密的安全事件
