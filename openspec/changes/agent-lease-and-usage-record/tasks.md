期 1 只做設計；以下是設計核可後的實作步驟，開工前另開工單。

## 1. 對齊與裁定

- [ ] 1.1 和 MyAiStorage#31（sidecar）的設計對齊 launcher 介面：啟動前申請、結束後回報、安全終止後才以新租約接續
- [ ] 1.2 本人確認：公司那把 Claude 訂閱給自己的 agent 當備援，是否符合公司訂閱的合約與內部政策
- [ ] 1.3 定下 v0 後端的 private repo 與政策檔格式

## 2. CLI 與 v0 後端

- [ ] 2.1 `lgw lease request|start|stop|switch|renew|revoke` 與只追加的事件紀錄
- [ ] 2.2 政策檔：角色 × coding agent × 訂閱 × 模型的允許清單與訂閱優先順序；預設全給
- [ ] 2.3 延後結算的逾期、撤銷（`lgw lease revoke`）、事件序號與去重、launcher 本機待送佇列
- [ ] 2.4 `lgw ls`（active 租約）與 `lgw usage`（依欄位彙總、未歸因另列）
- [ ] 2.5 紀錄的憑證樣式掃描測試（假資料）

## 3. 第 1 階段：只記錄

- [ ] 3.1 Mac 啟動包裝：申請租約後用 herdr 開 agent，結束時回報；失敗只發警告
- [ ] 3.2 sidecar 接上同一個 CLI
- [ ] 3.3 Claude Code 的 `OTEL_RESOURCE_ATTRIBUTES` 帶租約 id；opencode 結束時以 `opencode export <sessionID>` 附上該 session 的用量

## 4. 第 2 階段：照租約注入

- [ ] 4.1 launcher 只注入租約指定的那一把憑證；沒有租約就不啟動
- [ ] 4.2 Claude 個人額度用完時的切換：`StopFailure` hook → `lgw lease switch` → 接續 session → Angareion 通知 PMO
- [ ] 4.3 MyLinuxPool 的能力改成只讓 launcher 讀得到

## 5. 第 3 階段：照租約發放（只對 OpenRouter）

- [ ] 5.1 本人裁定 OpenRouter Management API key 的保管者
- [ ] 5.2 租約核准時發一把有額度上限的 OpenRouter key，租約結束（含逾期、撤銷、切換）時刪除
