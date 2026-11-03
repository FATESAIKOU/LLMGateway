## MODIFIED Requirements

### Requirement: 比例與能力評分欄填法
「比例」欄位 MUST 依提供商標記（OpenRouter 免費額度為帳號共用標「共用」、Zen 官方未公布標「查不到」、Ollama Cloud 與 Claude 標「依訂閱」），不設定基準值；「terminal」與「coding」欄位採 AA Terminal-Bench 與 Coding Index，若頁面無法取得或未收錄 MUST 標為「查不到」；「抽象架構設計」欄位 MUST 採 AA Intelligence Index，榜上未收錄時才標記為「主觀」並填入 0–100 評分；同一模型在 AA 榜單有多個 effort 檔位時，各 AA 指標欄（含「綜合」）MUST 取最高檔分數。

#### Scenario: 比例欄填法
- **WHEN** 填寫各提供商之比例欄
- **THEN** OpenRouter 填「共用」、Zen 填「查不到」、Ollama Cloud 與 Claude 填「依訂閱」

#### Scenario: 抽象架構設計欄填法
- **WHEN** 填寫模型之「抽象架構設計」欄位
- **THEN** 採 AA Intelligence Index 分數，榜上未收錄時標記為「主觀」並給予 0–100 評分

#### Scenario: AA 頁面抓不到時能力標查不到
- **WHEN** Terminal-Bench 或 Coding Index 公開頁面無法取得或未列入該模型
- **THEN** 對應欄位標記為「查不到」

#### Scenario: 多 effort 檔位取最高檔
- **WHEN** 某模型在 AA 榜單有 max 與 non-reasoning 兩個檔位（假分數 32／19）
- **THEN** 該格採 32（綜合欄同理）
