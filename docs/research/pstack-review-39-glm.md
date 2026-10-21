GLM（GLM-5.3-Flash Free）

# pstack-review-39（LLMGateway#39 rollback-to-user 外層提早拒絕）

審查對象：`lgwctl/migrate.py`、`tests/test_lgwctl_migrate.py`（commit `521f803`）與 `docs/research/impl-39-report.md`（`d307113`），以 `git diff origin/main...HEAD` 對照實際程式（`rollback_to_user`、`_rollback_inner`、`_rollback_locked`、`_rollback_refusal`、`_stop_loss`、`insurance_unit_argv`、`read_state`、OldLayout）逐點驗證。

## Findings

### 1. [nit] `_rollback_refusal` 的檔案判定用 `os.path.isfile`，與 preflight 的 `_regular_file` 語意不一致（既有限制，非本次引入）
**Location**: lgwctl/migrate.py:2000-2001（`_rollback_refusal`），對照 lgwctl/migrate.py:1393-1394（preflight 用 `_regular_file`）與 lgwctl/migrate.py:721-728（`_regular_file` 的嚴格語意）
**Finding**: 共用判斷把「舊 unit 檔在不在」收成單一位置（這是好事），但它沿用的是原本 `_rollback_locked` 裡的 `os.path.isfile`——會跟隨 symlink、接受非一般檔的連結；同一台機器上 preflight 對同樣四個檔用的是「存在、是一般檔、自身與上層目錄都不是 symlink」的嚴格檢查。兩個函式現在並排實現「舊 unit 檔存在」這個概念，遇到 symlink 化的舊 unit 檔會給出相反答案。
**Evidence**: `isfile(old.unit_file(unit))`（isfile 跟隨 symlink）與 preflight 的 `_regular_file(old_layout.unit_file(unit), old_layout.home)`（lstat＋禁 symlink 元件）對 symlink 檔判定相反；且 preflight 的註解（行 1389）明說「缺則停損回滾會拒絕」，但「缺」的定義兩處不同。此語意是從舊程式原句搬遷，本次修改沒有引入新風險（原本 inner 就這樣判），所以只列 nit。
**Suggestion**: 不在本單範圍（已裁定不擴大）。若未來有 c 撿起 rollback 相關工作，把 `_rollback_refusal` 的 isfile 換成 `_regular_file(old.unit_file(unit), old.home)` 即可把 preflight 與 rollback 的「同一概念」對齊——但那會改變拒絕行為，要另開工單，不要在 d 單順手改。

## 驗證過、未構成 finding 的點

- **TOCTOU 方向（重點 1）**：外層 `rollback_to_user` 不持 flock 呼叫 `read_state`（行 1951）。兩個拒絕條件在單輪切換內是單調的：`finalized` 只由 finalize 持鎖寫入且是終態（行 2526），沒有任何路徑在原地把它改回非 finalized；舊 unit 檔只被 finalize 搬去 archive（`_move_step` 只搬不回，行 2534-2549），沒有任何路徑在原地重建它們。所以「外層拒絕但 inner 本會成功」在單輪內不存在；反方向（外層放行、排程與 inner 起跑之間狀態變差）由 inner 持鎖後 `_rollback_locked` 重判兜住（行 2018-2023）。提早拒絕只是 UX，不會造成誤放行或行為分歧。
- **停損與保險（重點 2）**：`_stop_loss`（行 1786）仍直接呼叫 `_rollback_locked`，而 `_rollback_locked` 內改成呼叫同一個 `_rollback_refusal`——停損的判斷內容與訊息逐字不變；保險 timer 的 argv 直接帶 `--inner`（行 1375-1376），走 inner 分支，不受外層新判斷影響；`--inner` 的 `INVOCATION_ID` 檢查仍在排任何東西之前（行 1942-1946）。
- **測試真實性（重點 3）**：三條新測都有實質斷言。「沒排 unit」是用 `self.assertEqual(self.box.order(), [])` 鎖住（假 `systemd-run` 會把呼叫寫進 order.log，order 為空即真的沒排）；test (a) 還把外層 fail 行與 inner fail 行逐行相等比對、並在 inner 重跑後仍應為 1／order 仍空；test (c) 用三個 subTest 驗證非拒絕情況仍排 `systemd-run --unit=lgw-rollback … --inner` 且回 0，防止提早拒絕擴大到不該擋的情況。單跑 `python3 -m unittest tests.test_lgwctl_migrate` → 138 tests OK（190s），無跳過掩蓋的失敗。
- **Code Quality（重點 4）**：抽取乾淨——`_rollback_refusal` 只判斷不動作、三行拒絕邏輯只存在一份；兩個呼叫端都是 3 行薄接線，不構成「薄包裝」（它買到了條件單一來源＋外層共用）。簽名 `(old, state)` 偏離設計例的 `(layout, old, state)` 合理：兩個條件確實只用得到 `old` 與 `state`，留 `layout` 就是一個死參數，作者在報告裡也如實標了出入處。行數淨增約 +21（migrate.py 已 >1000 行，本次沒有把它推向失控）。訊息字串一字未改、`report.fail(*refusal)` 與 `Report.fail(name, detail)` 簽名吻合。
