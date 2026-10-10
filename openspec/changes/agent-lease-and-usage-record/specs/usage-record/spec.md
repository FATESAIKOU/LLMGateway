## Purpose

以租約為主鍵，記錄並彙整每個 agent 用了多少模型額度，讓本人與 PMO 能回答「哪台機器、什麼角色、哪個訂閱、哪個 coding agent、哪個模型，用了多少」，而紀錄本身不洩漏憑證與工作內容。

## ADDED Requirements

### Requirement: 租約事件是權威紀錄
「誰在何時用了哪個訂閱、哪個 agent、哪個模型」SHALL 以租約事件為準。用量資料 MUST 以租約 id 歸因；無法對到租約的用量 SHALL 標為未歸因，而不是猜測歸屬。未歸因的用量只有在之後出現相同租約 id 的租約時才能改歸屬。

#### Scenario: 用量對到租約
- **WHEN** 一筆用量回報帶有租約 id
- **THEN** 該筆用量歸到該租約的 worker、角色、訂閱、coding agent、模型

#### Scenario: 用量沒有租約 id
- **WHEN** 一筆用量回報沒有租約 id 或租約 id 不存在
- **THEN** 該筆用量記為未歸因，並出現在未歸因清單

#### Scenario: 用量晚到
- **WHEN** 一張租約 released 之後才收到帶該租約 id 的用量
- **THEN** 用量併入該租約，彙總隨之更新

### Requirement: agent 端回報帶租約 id
launcher SHALL 讓 agent 的用量回報帶上租約 id 與 worker、角色：Claude Code 以 OpenTelemetry 的資源屬性帶出，並由 launcher 在租約結束時從本機匯出端取得該租約的 token 與成本；opencode 在租約結束時由 launcher 以該 session 的 id 擷取 token 與成本。兩者都附在 released（或 superseded）事件上。

#### Scenario: Claude Code 的 OpenTelemetry
- **WHEN** launcher 以租約啟動 Claude Code 並啟用遙測
- **THEN** 每筆 token 與成本指標都帶有租約 id、worker、角色的資源屬性，且租約結束事件附有該租約的 token 與成本合計

#### Scenario: opencode 結束時附上統計
- **WHEN** 以 opencode 執行的租約被 released
- **THEN** released 事件附有該 session（只有該 session）的輸入、輸出 token 與成本

### Requirement: 紀錄不含憑證與工作內容
用量紀錄與租約事件 MUST NOT 含憑證的值、prompt、回應內容或檔案內容；只能有識別欄位、計數、成本、時刻與理由。理由欄位 SHALL 從固定的理由代碼中選一個，加上不超過 200 字的說明；說明 MUST NOT 直接貼上 agent 的錯誤輸出。

#### Scenario: 掃描紀錄
- **WHEN** 以憑證樣式（token 前綴、私鑰、密碼欄位）與內容標記掃描整份紀錄
- **THEN** 沒有任何命中

### Requirement: 查詢與彙總
LLMGateway SHALL 能依 worker、角色、訂閱、coding agent、模型、期間任一組合列出租約與彙總用量，並列出目前 active 的租約。期間包含起點、不含終點。

#### Scenario: 看現在誰在跑
- **WHEN** 查詢 active 租約
- **THEN** 回傳每張 active 租約的 worker、角色、訂閱、coding agent、模型與開始時刻，已過期的不在其中

#### Scenario: 依訂閱彙總
- **WHEN** 查詢某期間依訂閱彙總的用量
- **THEN** 回傳每個訂閱的 token、成本與租約數，未歸因的另列一行
