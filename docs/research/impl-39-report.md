MiMo-V2.6-Flash Free

# LLMGateway#39 實作報告：rollback-to-user 外層的誤導訊息

分支 `fix/issue39-rollback-outer`（從 main 開）；只在 worktree
`/Users/fatesaikou/testAI/LLMGateway-worktrees/fix-39` 讀寫，不 push、不碰 main。

## 問題

`phase=finalized` 之後人手跑 `llmgwctl rollback-to-user`：外層（只排一個
transient unit `lgw-rollback` 就結束的那一次）照樣排 unit、印
`ok rollback（已排入 lgw-rollback…）` 並回 0；實際由分離執行的 `--inner`
才拒絕（舊 unit 檔已被 finalize 搬走）。不會動任何東西，但外層訊息誤導
（「已排入」＋回 0，結果 inner 拒絕）。舊 unit 檔不在（人手刪掉、或 finalize
搬走）時同理。

## 做了什麼

照設計，不擴大範圍：

1. **抽出共用判斷**（`lgwctl/migrate.py`）：新增
   `_rollback_refusal(old, state) -> tuple[str, str] | None`，包住
   `_rollback_locked` 原本的兩個拒絕條件——phase 是 `finalized`、任一舊
   unit 檔不在。回 `(報告項目名, 訊息)`；可以回舊版就回 `None`。只判斷、
   不動任何東西，訊息一字不改（兩句都還是 `fail rollback（…）`）。
2. **`_rollback_locked` 改呼叫它**：`state = read_state(layout)` 之後
   `report.fail(*refusal)`。停損路徑（`_stop_loss`）由 inner 直接呼叫
   `_rollback_locked` 的行為不變。
3. **外層提早拒絕**：`rollback_to_user` 的 `inner=False` 分支，在排
   transient unit 之前先 `read_state(layout)`（只讀，不拿 flock）並呼叫同一個
   `_rollback_refusal`；有拒絕就 `report.fail` ＋回 1，**不排 unit**。
   真正的決定仍在 inner 持鎖之後再判一次（狀態在排程與 inner 起跑之間變了
   也擋得住）。
4. **文件**：`rollback_to_user` 的 docstring 加一句「一定會被拒絕的情況在
   外層就直接回 1、不排 unit」。

## 跟設計有出入處

- 設計例的簽名是 `_rollback_refusal(layout, old, state)`；實作是
  `_rollback_refusal(old, state)`。這兩個判斷只用得到 `old` 與 `state`
  （finalized 看 state、unit 檔看 `old.unit_file`），`layout` 在函式裡沒有
  用處，不留死參數。呼叫端仍照設計：外層 `read_state(layout)` 只讀、
  `_rollback_locked` 沿用自己讀到的 state。
- 其餘（判斷內容、訊息字串、回 1 不排 unit、不拿 flock、inner 不變）完全照設計。

## 測試對照

新增 3 條（`tests/test_lgwctl_migrate.py`，`RollbackToUserTests`）：

| 情況 | 測試 | 斷言 |
| --- | --- | --- |
| (a) finalized | `test_outer_rollback_refuses_after_finalize_without_scheduling` | 外層回 1、有 `fail rollback`、沒有 `ok rollback`、訊息與 inner 逐字相同、`order()==[]`（假 systemd-run 沒被呼叫）、unit 狀態與狀態檔沒動 |
| (b) 舊 unit 檔不在 | `test_outer_rollback_refuses_when_an_old_unit_file_is_missing` | 外層回 1、訊息含缺的檔名、沒排 unit、沒動任何 unit |
| (c) 正常情況 | `test_outer_rollback_still_schedules_outside_the_refusals` | phase=confirmed／reverted／沒有狀態檔時，外層照舊排 `systemd-run --unit=lgw-rollback … --inner` 並回 0（原本只有 pending-confirm 的既有測試） |
| (d) inner 直接跑 finalized | 既有 `test_rollback_refuses_after_finalize_without_stopping_new_units`、`test_rollback_refuses_when_an_old_unit_file_is_missing`（都走 `--inner`） | 不退化：仍回 1、停任何新 unit 之前就拒絕 |

既有行為的回歸：停損路徑仍由 inner 直接呼叫 `_rollback_locked`、`--inner`
仍要求 `INVOCATION_ID`、沒有狀態檔時照舊冪等回舊版
（`test_rollback_without_any_state_is_still_safe` 等）——都還在跑。

## 測試指令與通過數

- `python3 -m unittest tests.test_lgwctl_migrate tests.test_lgwctl_cli_sync`
  → 158 個：156 過、**2 個既有失敗**（`test_lgwctl_cli_sync` 的
  `MigrateDispatchTests.test_dispatch_to_fake_migrate_module` 與
  `test_module_without_main_is_a_clear_error`）。這兩個與本 issue 無關：在
  同一個分支把本次改動 `git stash` 後再跑同一條指令，155 個一樣這 2 個失敗
  （兩個模組一起跑、migrate 在前時的既有污染；單獨跑 `MigrateDispatchTests`
  3 個全過）。本次改動沒有新增失敗。
- `python3 -m unittest discover -s tests` → **649 個全過**（`OK (skipped=1)`；
  main 目前 646，+3 是本單新增）。

## commit 清單

- `521f803` fix(#39): rollback-to-user 外層排 unit 前先做同一套拒絕判斷
  （`lgwctl/migrate.py`、`tests/test_lgwctl_migrate.py`）
- 本報告：`docs/research/impl-39-report.md`（下一筆 commit）
