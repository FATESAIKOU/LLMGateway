Claude Sonnet 5.5（claude-sonnet-5-5）

# #35 第 5 輪複核（分支 review/final-35e，HEAD 86cd61c，基準 dcab45c）

範圍：f617001、a645a85、86cd61c 的程式、測試、deploy.md；對照 `review-35-final4.md` 與作者報告 `agy-mig7.md`。
測試：`python3 -m unittest discover -s tests` → `Ran 568 tests in 107.882s` → `OK (skipped=1)`（作者報告寫 525，實際整合分支 568；數字不符但全綠）。
威脅模型同前：`llmgw` 可能被攻破；prepare／cutover／rollback／finalize 以 root 跑；回滾以 `systemd-run` 分離執行（SIGHUP 不會殺它）。

## 上一輪項目核對

| 項目 | 結論 | 依據 |
|---|---|---|
| 中-2 回滾隧道交接 | **修對（正常路徑與兩種失敗路徑）；再跑補救路徑有缺口（新中-1）** | `_switch_to_old_tunnel`：舊服務 list 200 才動 → 記 NRestarts → stop 新隧道（不 disable）→ start 舊隧道 → `_wait_old_tunnel`（連續 active 15 秒、45 秒上限、NRestarts 不增）→ `probe.remote` 重試 → 成功才 disable 新隧道；任一步失敗：停舊隧道、`_recover_new_tunnel` start 新隧道、rc=1、狀態 `tunnel_ok=false`、保險照常取消。新舊隧道不會同時持埠。cutover 方向本來就是 stop-old → disable-old → 確認全停 → start-new → 探針，對稱且失敗走 `_stop_loss`→回滾，沒有雙持埠的路徑。 |
| 中-1 寫入側 symlink | **修對；殘留只剩檢查後使用（TOCTOU）** | `_restore_db`／`_restore_keys_env` 不論 dst 存不存在都檢查 `dst_dir`（及存在的 dst）；`_copy_db_consistent`／`_copy_into_place` 在 `makedirs` 前再檢查一次；`_backup_old_db` 帶 `dst_base`；`_prune_db_backups` 先查目錄鏈，且 `islink` 的備份不刪。所有寫入點都覆蓋（我逐一對過：restore db、restore keys.env、backup db、prune、prepare 的複製、cutover 的最終同步）。 |
| 低-1 NRestarts／窗口 | **修對，有一個小缺口（低-2）** | 讀不到 NRestarts 印 `todo`；窗口 5→15 秒、總時限 45 秒。 |
| 低-2 base 傳遞 | **修對** | `_step_keys_env`、`_step_db`、`_step_keys_pass` 都傳 `base=old.home`。 |
| 低-3 mock 退路／FIFO／重複 round | **修對** | chown 退路移除；`O_NONBLOCK`＋`fstat(S_ISREG)`；`_start_new_round` 只剩一次。測試改 mock `os.open／fstat／fchown`；FIFO 拒絕有測。 |
| 低-4 deploy.md | **修對** | 步驟 4／§2.2 改成「停新→啟舊→穩定→才 disable」並寫明 5～15 秒空窗與失敗叫回新隧道（以 diff 與程式對照一致）。 |

### 中-2 逐點驗證（你要的實況推演）

- **同埠互斥**：新隧道（system）與舊隧道（user）都 `ssh -R 4710/4711`＋`ExitOnForwardFailure=yes`；程式現在先 stop 新、再 start 舊，順序正確。
- **空窗**：成功路徑 = 舊隧道連線建立（約 1～3 秒）＋穩定窗口 15 秒＋ probe 一次（成功即回）≈ **17～25 秒**。失敗路徑最壞 = 45 秒（穩定判定上限）＋ probe 3 次×10 秒間隔（約 20～30 秒，多半在 `_wait_old_tunnel` 就失敗而不會到 probe）≈ 45～75 秒，之後新隧道被叫回，新隧道自己 `ExitOnForwardFailure` 重試（unit 重啟間隔 10 秒），所以管理連線再恢復約 +1～10 秒。**空窗期間管理 ssh 會斷**；回滾行程是 transient unit，不受影響。
- **失敗復原**：停舊隧道後立刻 start 新隧道；舊 ssh 被殺後遠端會合點埠通常立即釋放，即使稍晚也由新隧道的重試補上。`_recover_new_tunnel` 自己 start 失敗會印「請以 console 登入處理」，不會假裝成功。
- **狀態檔／保險／rc**：失敗路徑寫 `reverted`＋`tunnel_ok=false`、rc=1、`report.fail` 有記；保險（revert timer）不論成功失敗都取消（舊服務已回來，正確）。再跑 `rollback-to-user` 時新隧道若 active 會重走整個交接（冪等，有測）。成功路徑 disable 新隧道失敗只 `report.fail`（rc=1）但 `tunnel_ok=true`，無害。
- **測試真偽**：假 systemctl 在「另一層同名隧道 active 時 start 立刻 exit 1」，這是**真的互斥模擬**，不是只 mock 函式；`test_rollback_handles_tunnel_port_handover` 驗順序與最終狀態（新停、舊起），`..._recovers_new_tunnel_when_old_tunnel_probe_fails` 驗新隧道被叫回、舊隧道被停。限制：模擬的是「start 當下衝突」，不是動態 crash-loop；`start-old-tunnel` 失敗分支與 `_recover_new_tunnel` 本身失敗分支沒有測試（低-3）。

## 高（0）

無。

## 中（1）

### 中-1 回滾重跑時「新隧道已停、舊隧道沒人啟動」：補救路徑永遠過不了

- **檔案:行**：`lgwctl/migrate.py:2002-2010`（reverted 分支）、`:2197-2215`（`_start_old_units` 只起 `OLD_START_SERVICES`，不含隧道）、`:2321-2336`（`_wait_old_tunnel` 只等不啟動）。
- **問題**：這輪把舊隧道從 `_start_old_units` 拿掉，改由 `_switch_to_old_tunnel` 才啟動。但 reverted 分支在「新隧道不是 active」時走 `else`：只呼叫 `_wait_old_tunnel`，**沒有任何人啟動舊隧道**。何時發生：回滾行程在「stop 新隧道」之後、「start 舊隧道」之前被打斷（主機重開、OOM、`systemctl kill`）；或舊隧道失敗後 `_recover_new_tunnel` 也失敗（兩條都下線，這時人本來就得走 console）；或新隧道事先自己掛了。重跑只會等 45 秒、印「舊隧道仍然起不來」，其實根本沒人啟動它。
- **影響**：只在「兩條隧道都沒了」的狀態下才踩到，那時本來就需要 console；但程式與訊息都誘導「修好後再跑一次」，而再跑修不了。不會造成資料損失或雙跑。
- **建議**：else 分支先 `_user_systemctl_outcome(layout, "start", TUNNEL_SERVICE_NAME)` 再等；或不論新隧道 active 與否都走同一個 `_switch_to_old_tunnel`（其內 stop 新隧道遇 missing 已能處理）。補一個「新舊隧道都 inactive 時重跑 rollback」的測試。
- **緩解**：操作清單 §C-3（console 手動 `systemctl --user start llmgateway-tunnel`）。不擋放行。

## 低（3）

### 低-1 寫入側的檢查仍是「檢查後使用」

`_has_symlink_component` 之後才 `makedirs`／`mkstemp`／`os.replace`（`migrate.py:405-413`、`:571-583`），`llmgw` 若在這個極短視窗內換掉上層目錄仍可導向。回滾時新服務已停、舊服務尚未起，但 `llmgw` 的其他行程（舊 key-sync、殘留 shell）可能還在。可行性很低（要在毫秒級視窗內贏過競態且目標目錄需 root 擁有、檔名固定、內容受控）；完整解法是 `O_DIRECTORY|O_NOFOLLOW` 逐段 `openat`。以操作程序緩解（`ps -u llmgw` 確認沒有行程再回滾，§A-4）。另 `_has_symlink_component` 用字串 `startswith` 比對 base（`/home/llmgw2` 會被當成在 `/home/llmgw` 底下）且 path 不在 base 底下時回 False（fail-open），目前呼叫端都在 `old.home` 底下，無實害。

### 低-2 NRestarts 基準在 stop 狀態取得，start 會歸零

`_switch_to_old_tunnel` 在 start 之前讀 `initial_restarts`；systemd 手動 start 會把 `NRestarts` 歸零。若舊隧道先前在 crash-loop 殘留一個較大的計數（例如 30），歸零後新增的重啟都比 30 小，計數器判斷失效，只剩連續 15 秒 `is-active`（對 10 秒重啟間隔的 crash-loop 仍擋得住，因 active 期間通常 <3 秒）。建議 start 之後才取基準。

### 低-3 測試缺口

`start-old-tunnel` 失敗分支、`_wait_old_tunnel` 失敗分支（假 systemd 讓舊隧道 start 成功但隨後 inactive）、`_recover_new_tunnel` 自己失敗、`_prune_db_backups` 的 symlink 拒絕，沒有測試。程式讀起來正確，但沒有回歸保護。

## 資訊

- 資訊-1：作者報告稱測試 525 個，整合分支實跑 568 個，全綠；報告以程式與測試為準，無誇大成分。
- 資訊-2：`_restore_old_units`（cutover 中止）會 start 舊隧道，此時新隧道尚未啟動，不會與埠衝突；已確認。
- 資訊-3：回滾在新隧道停掉的數十秒內，若 PM 的 ssh 是走這條隧道，連線會斷；回滾本身不受影響，但 PM 看不到進度（見操作清單）。
- 資訊-4：回滾重新 enable 舊 unit（含舊隧道）後、到「disable 新隧道」之前，新舊隧道都是 enabled；此視窗內重開機會雙起互搶埠（舊的會 crash-loop、新的先搶到時管理連線不斷，但回滾「成功」的狀態是假的）。操作清單禁止此視窗重開機。

## 放行判斷

高 0、中 1（只影響「兩條隧道都沒了」的二次補救，且有 console 緩解）、低 3。高-1（root 跟 symlink）、中-1（寫入側）、中-2（隧道交接）與其餘上輪項目均已確實修好，全量測試綠。殘留風險集中在「回滾空窗期間管理連線中斷」與「極端失敗需 console」，可由下列程序緩解。**建議放行，真機切換必須照操作清單執行**（新中-1 建議之後的小修補上，不阻擋）。

## 真機切換操作清單（PM 照做）

### A. 切換前（cutover 之前）
1. 確認有 **console／IPMI／雲端 serial console 備援**可登入該機（不靠這條隧道）；先實際登入一次測試。
2. 記錄現況：`systemctl --user -M llmgw@ status llmgateway.service llmgateway-tunnel.service`（或對應的 user 層指令）與 `systemctl status llmgateway.service llmgateway-tunnel.service`，截圖或貼下來；確認舊 unit 檔四個都在、舊 `llm.db`／`keys.env` 在。
3. 確認**沒有排定的重開機**（cron、unattended-upgrades、`shutdown -c` 檢查）。
4. `ps -u llmgw` 確認只有預期的舊服務／隧道／key-sync；沒有殘留 shell 或可疑行程。
5. 先跑 `llmgwctl prepare` 並確認所有項目 ok／無 fail；再跑 preflight。
6. 開**第二個終端機**（不同 ssh 連線）預備重連；通知相關人這段時間管理連線會短暫斷。
7. cutover 用 `--revert-after 15m`（或更短，視觀察需要），不要用太長。

### B. 切換中
1. 在 `llmgwctl cutover` 期間與 `pending-confirm` 觀察期內**不要重開機**。
2. cutover 結束看到 `confirm-needed`：用**第二個終端機**驗證 API 本機與經會合點都 200、`llmgwctl check` 為 0；沒問題才 `llmgwctl confirm`。
3. 若 cutover 印出停損／rc≠0：它會自動回滾（分離執行）。**預期管理 ssh 會斷 20～75 秒**；不要重複下指令，用 console 或重連後看 `journalctl -u lgw-rollback.service` 與 `llmgwctl status`。

### C. 回滾後驗證與收尾（手動或保險觸發都一樣）
1. 等 `lgw-rollback.service` 結束（`systemctl is-active lgw-rollback.service` 為 inactive）。`llmgwctl status` 應是 `reverted`、`tunnel_ok=true`。
2. 若 `tunnel_ok=false`／日誌有 `tunnel-old fail`：新隧道多半已被叫回；先確認管理連線還在（能 ssh 就用，不能就 console）。再執行一次 `llmgwctl rollback-to-user`（新隧道 active 時會完整重走交接）。
3. 若新舊隧道**都不 active**（console 登入）：`systemctl --user -M llmgw@ start llmgateway-tunnel.service`（舊隧道），等 20 秒確認 `is-active`；確認舊隧道連得上後執行 `systemctl disable llmgateway-tunnel.service`（新隧道）；若舊隧道起不來，改 `systemctl start llmgateway-tunnel.service`（新）保住管理通道，再排查。
4. 驗證清單：舊服務本機 list 200；經會合點 list 200；舊 key-sync.timer active；`systemctl is-enabled llmgateway-tunnel.service`（新）為 disabled；user 層舊隧道 enabled 且 active；`ps -u llmgw` 只有舊 unit 的行程；舊 `llm.db`／`keys.env` 權限、owner 正常。
5. 在新隧道被 disable、舊隧道穩定前**不要重開機**。
6. 回滾成功後才處理 finalize；`finalized` 之後不能再回舊版，所以 finalize 前再做一次上述驗證清單。

結論：放行（高 0、中 1、低 3、資訊 4）——真機切換須有 console 備援並照操作清單執行；中-1（重跑時舊隧道無人啟動）建議之後補修，不阻擋。
