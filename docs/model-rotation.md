# 模型輪換表

這張表是 PMO／PM 挑模型的參考，**不自動改** `policy/default.json`；政策變更仍走獨立 PR。

由 `/model-rotation-survey` 每週手動更新（PMO 週例執行，有變化才開 PR，本人 comment 後再套用）。涵蓋四家：OpenRouter／Zen 免費、Ollama Cloud（付費）、Claude（訂閱，只能用在原版 Claude Code）。

## 怎麼讀

- 「主觀」是 AI 判斷，不是實測。
- 每格標籤：
  - `官方數字`：來自官方或榜單，附 URL 與讀取日期。
  - `推估`：依資料推算，不是官方數字。
  - `共用`：額度是整個帳號共用，無法分到單一模型。
  - `查不到`：沒有公開資料；不填猜測值。
  - `主觀`：AI 判斷，不是實測。
- 另有兩個固定值：`待確認`（OpenRouter 的 `:free` 與 pricing 兩者不一致，無法判斷是否免費）、`依訂閱`（Ollama Cloud、Claude 的額度隨訂閱方案而變，不寫額度）。
- 能力欄尺度 0–100：terminal＝AA Terminal-Bench、coding＝AA Coding Index、抽象架構設計＝AA Intelligence Index（榜上沒有才標「主觀」）。
- 比例欄不設基準，只標來源。
- 排序：以「綜合」由高到低；綜合只看 Intelligence Index，榜上沒有的標「主觀」並照該值排序。

## 表

| 模型名 | 比例 | terminal | coding | 抽象架構設計 | 綜合 | 提供商 | 可用的 agent | 計入週用量節流 | 來源與讀取日期 |
|---|---|---|---|---|---|---|---|---|---|

尚未調查（首次由 PMO 週例執行）
