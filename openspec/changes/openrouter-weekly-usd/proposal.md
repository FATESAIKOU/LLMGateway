# #210 OpenRouter 本週 USD（上層 #204、MyPMO#386）

## Why
本人已決定允許回傳 OpenRouter 本週已用 USD，讓成本週報取得實際值；零與取得失敗必須可區分。現行契約禁止一覽中的金額。

## What Changes
提議只在 `GET /v1/usage` 每筆新增 `weekly_used_usd` 與 `weekly_used_usd_fetched_at`；其他訂閱兩者為 null。保留 list、resolve 與 pipes 的形狀和不含金額的邊界。金額由現有背景請求取得，與窗口獨立判定，僅保存在記憶體；金額所屬 UTC 週與讀取時的 UTC 週不同時（跨週）兩值均為 null。設計需先交 PMO 與非作者審查後才實作。

## 不做
不修改程式、測試或政策檔；不新增持久儲存、不修改 sidecar 或 MyLinuxPool、不部署、不處理 claude-personal。部署前另交計畫由 PMO 問本人。

## 影響
一覽每筆由十鍵變十二鍵，既有九鍵 usage 不變。金額與其時間欄位的規則、失敗處理和可驗收契約見 design、delta 與 tasks。
