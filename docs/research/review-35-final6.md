Claude Sonnet 5.5

# #35 第 6 輪 spot review（只看 4e9921e）

測試：`python3 -m unittest discover -s tests` → Ran 576 tests，OK（skipped=1）。

## 逐項結論

1. **中-1：修對。** `_rollback_locked` 的 reverted 分支現在三種情況都會啟動舊隧道並等穩定：
   - 新隧道 active：走 `_switch_to_old_tunnel`。這個函式只在新隧道 active 時才 stop，stop 失敗仍回 False。start 失敗、等不穩、探針失敗都會 stop 舊隧道再叫回新隧道，叫回的邏輯沒被繞過。
   - 新隧道 inactive：直接 `--user start` 舊隧道，等穩定。失敗就 stop 舊隧道並 `_recover_new_tunnel`。
   - 兩者皆無：同上一種，由測試 `…both_tunnels_inactive…` 涵蓋。
   - 舊隧道已 active 時再 start：對 service 是 no-op，不會重啟。基準在 start 後才取，也不會誤判。
2. **低-1：沒有過嚴。** 所有帶 base 的呼叫端，路徑都由 `OldLayout`（`os.path.join(old.home, …)`）或 `<dst>.bak-*` 組成，都在 old.home 底下。`path == base` 時迴圈不進入，回 False。`/home/llmgw2` 這類同前綴路徑現在正確被拒。不帶 base 的舊行為不變（只看最後一層）。
3. **低-2：正確。** 基準在 start 之後取（`_switch_to_old_tunnel`、reverted 的 else 分支都是），不會因手動 start 把 NRestarts 歸零而誤判。`_wait_old_tunnel` 仍會在 NRestarts 增加時重置穩定計時，所以仍擋得住 crash-loop。
4. **新測試：測到真行為。** 兩個測試我用「回到舊行為會失敗」的角度確認：
   - NRestarts 基準測試比對 `user start` 的 action 順序與取樣順序。舊程式基準在 start 之前，會失敗。
   - prune 測試的 symlink 備份檔檔名排序在最前面，若沒被拒絕就會被刪，所以有效。
   - 其餘（start 失敗、wait 失敗、recover 也失敗、symlink 元件）斷言了服務層狀態與 state 檔。

## 發現

### 高
無。

### 中
無。

### 低
- **低-1：reverted＋新隧道 inactive 的分支沒有做遠端探針，也沒有 disable 新隧道。** `_switch_to_old_tunnel` 成功時會 `probe_remote` 並 `disable` 新隧道。這個 else 分支只看 `_wait_old_tunnel`，就把 `tunnel_ok` 寫成 True。若上次是被打斷（新隧道已停但仍 enabled），重開機後新舊隧道會搶 4710/4711。這個分支與 `_switch_to_old_tunnel` 的 start／等待邏輯重複，建議抽成共用函式。
- **低-2：重跑測試沒覆蓋「reverted＋舊隧道已 active」。** 沒有斷言不會重複 start 或重啟。實際上 systemd 的 start 是 no-op，所以不影響正確性，只是少一個保護。

### 資訊
- `test_rollback_rerun_after_interrupted_at_stop_new_tunnel` 沒有把 state 設成 reverted，走的是一般 rollback 路徑（`_switch_to_old_tunnel` 略過 stop）。它測的是「stop 被略過」，不是「reverted 重跑」，測試名稱略有誤導。
- `_recover_new_tunnel` 在 outcome 為 missing 時，訊息會印空的 detail。
- 作者報告偏樂觀，但程式實際行為與其描述大致相符。

結論：放行
