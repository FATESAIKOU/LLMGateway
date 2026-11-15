# 工作與可驗收契約

## PR 1：設計（本 PR）
- [x] C-1：完成 proposal、方案比較、相容性、delta 與本工作拆分；strict 驗證通過。
- [x] C-2：交 PMO 及非作者審查，確認範圍與介面；跨週規則已定案採用（design D2），審查結論寫在 #210 留言後才進 PR 2。
- 完成判準：`openspec validate openrouter-weekly-usd --strict` 與 `openspec validate --specs --strict` 通過；在副本跑 `openspec archive openrouter-weekly-usd --yes` 後 `openspec validate --specs --strict` 通過，報告貼含 warning 的原樣輸出；`git status -s` 只有該 change 目錄；C-2 的審查結論寫在 #210 留言。

## PR 2：實作與測試（依審查後契約）
- 完成判準：C-3～C-7 綠；PR 1 的 strict 驗證仍通過；JSON 解析拒絕 NaN／Infinity 的測試存在。
- [x] C-3：一覽每筆十二鍵、schema 1；同快取狀態八個原 usage 值與 list 相同，原九鍵 usage、resolve meta 與 env_schema 不變。
- [x] C-4：假上游分別測正數、0、缺欄、null、字串、布林、負數、NaN、Infinity、1e400（解析為 inf）；精確區分 0 與 null，時間成對。
- [x] C-5：測窗口與金額各自成功失敗、單筆例外、上游非 200／429／逾時／壞 JSON／壞 data、停用、無 key、未取及超過六小時；必列一格「200＋合法 `usage_weekly`＋`free_model_daily_requests` 缺或壞」，金額保留且帶自己的時間；另測窗口全缺＋金額合法、跨週（以 `weekly_used_usd_fetched_at` 所屬週一 00:00 UTC 與 now 比較，不同週即 null）；確認金額清空、既有窗口行為、single-flight／退避與不等待。
- [x] C-6：注入互異的金額、key、token、帳號 canary；測所有非 OpenRouter 訂閱、一覽其他欄位、list／resolve usage、所有 pipes、note、log、稽核及快取不落地；僅指定一覽欄位允許指定金額，resolve 原有 env 除外。
- [x] C-7：測 client 透傳新舊回應、lgwctl 保持可用與舊行為；覆蓋每個 delta Scenario，跑相關測試與全套必要檢查。

## PR 3：部署計畫
- 完成判準：部署計畫文件齊全（消費者、sidecar、回退）且經 PMO 轉達本人決定；未取得決定前不部署。
- [ ] C-9：（部署前確認；見 #210。消費者容忍新鍵、部署計畫經本人決定尚未完成，未勾選）確認週報消費者容忍新鍵、sidecar 介面不變與回退方式；消費者讀法寫明：查詢不等背景請求，第一次可能回 null，週報應先呼叫一次觸發，間隔 ≥6 秒（上游逾時為 5 秒，須大於單次取用量的最長時間）讀一次；仍為 null 再間隔 ≥6 秒讀第二次；第二次也 null 則本週報記為「金額未取得」（附窗口的 status 與 reason），不以 0 代替、不再等待；提交部署計畫經 PMO 問本人，取得決定前不部署。
- [x] C-10：更新 `docs/deploy.md` §5.7，補 `GET /v1/usage` 新欄位 `weekly_used_usd`、`weekly_used_usd_fetched_at` 的說明（null 不是金額、零與取得失敗可區分）。

## 歸檔（PR 3 合併後 archive）
- 完成判準：歸檔後 `openspec validate --specs --strict` 通過；主 spec 的 Scenario 標題與內文一致；歸檔前先在副本跑 archive 模擬並貼含 warning 的原樣輸出。
- [x] C-8：PR 3 合併後執行 `openspec archive openrouter-weekly-usd --yes`；手動更新 usage-reporting Purpose（補一句正面說明：一覽另帶 OpenRouter 本週已用 USD，null 不是金額，其他欄位仍不含金額）；確認主 spec 的 `note 的語意`「不拿金額或帳號」Scenario 的 THEN 已帶「唯一例外是 `GET /v1/usage` 的 `weekly_used_usd`」，`usage 的 session 與 weekly 窗口`「方案沒有 session／weekly」的 THEN 已限定為 list 的 `usage`（兩條由本 change 的 MODIFIED 帶入，歸檔後應逐字見到）；直接改 `openspec/specs/usage-reporting/spec.md` 的 Scenario 標題「每筆的鍵集恰為十個」為「每筆的鍵集恰為十二個」（該名稱只見於主 spec、本 change 與已歸檔的 #204 副本，無活文件引用；#204 tasks 4.1 有 archive 後手動改 Purpose 的先例）；確認九鍵與其他主 spec 無矛盾；再跑 `openspec validate --specs --strict`。
