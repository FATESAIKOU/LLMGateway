# fix3 對帳與收尾（第 3 輪修正）

基準：`d3f312b`（MiMo 未提交進度的檢查點）。審查：`review-35-final.md`（1 高 11 中 13 低）＋任務原文的兩個演練補充。
判定只看程式與測試；「原判定」是 `d3f312b` 的狀態，後面是這次補的東西。行號是最終 commit 的。

- 基線測試：480（分支只有 T1／T4，沒有整合分支的 T2／T3 測試）。
- 本輪結束：`python3 -m unittest discover -s tests` → 見回報（最終數字）。

## 高

| 項 | 原判定 | 證據（程式:行） | 這次補的測試／修正 |
|---|---|---|---|
| 高-1 rollback 停掉自己走的隧道 | 部分 | `lgwctl/migrate.py:1687-1722`（人手一律分離執行 `lgw-rollback`）、`:1724-1741`（inner 持同一把鎖）、`:1743-1830`（先停新 service→restore→起舊→探 200／舊隧道 active→**最後**才停新隧道）、`:2003-2025`（`_wait_old_tunnel`） | 程式：舊隧道起不來時**不停新隧道**（管理連線／8480 導向舊服務），明確 fail；修好後再跑一次可補完。測試 `tests/test_lgwctl_migrate.py:1258`（新隧道在 curl 200 之後才停）、`:1395`（舊隧道起不來→新隧道留著→重跑補完）、`:1761`（argv 分離）。 |

## 中

| 項 | 原判定 | 證據（程式:行） | 這次補的測試／修正 |
|---|---|---|---|
| 中-1 keysync 等待用「現在」 | 已做 | `lgwctl/migrate.py:1437`（啟動新服務前記 `cutover_started`）、`:1474-1477` | 測試 `tests/test_lgwctl_migrate.py:956`（grace 5 秒＋啟動當下寫狀態，斷言不出現假 todo）。 |
| 中-2 保險可能在切換中等鎖死掉 | 部分 | `lgwctl/migrate.py:1495-1506`（寫 pending 前確認 timer 還在，不在就 fail＋todo）、`:164-169`、`:1728-1733` | 測試 `:972`（timer 在切換中消失→rc 1、`insurance` fail、`no-insurance` todo）。限制：保險仍是一次性 transient、不重試；只保證「不會宣稱不存在的保險」。 |
| 中-3 手動 rollback 沒換回舊 unit | 已做 | `lgwctl/upgrade.py:149-200`（切 link 前驗 previous 的 `deploy/units`；切完裝 unit＋reload，失敗連 unit／link 一起回復） | 測試 `tests/test_lgwctl_upgrade.py:236`（rollback 後 unit 內容回到舊 tag）、`:254`（previous 沒有 unit 檔時先拒絕、current 不動）。 |
| 中-4 狀態機沒有重設／終態守門 | 部分 | `lgwctl/migrate.py:138`（`OPEN_PHASES`）、`:686-706`（歸檔 `.cutover.state.<時間戳>`）、`:706-718`（`_start_new_round`）、`:1293-1296`（outer 也守門）、`:1346-1350`（inner）、`:1754-1764`（finalized／舊 unit 檔不在，停新 unit 前拒絕）、`:1990-1994`（enable missing 算 fail） | 程式：outer 在排 transient 前就拒絕進行中階段。測試 `tests/test_lgwctl_migrate.py:691`（prepare 歸檔 reverted）、`:836`（inner reverted 再切）、`:847`／`:864`（inner／outer 拒絕 pending／confirmed／finalized）、`:1463`（finalized 後 rollback 不呼叫任何 stop）、`:1475`（舊 unit 檔不在先拒絕）。限制：`status` 仍不顯示 phase（只是顯示，不影響守門）。 |
| 中-5 切換窗口重開機 | 部分 | `lgwctl/migrate.py:1402-1408`（(b) 之前印 reboot-window todo） | 測試 `:909` 斷言警告存在且在 `stop-old` 之前。限制：沒有 boot-time 檢查／persistent 保險（審查的「更好做法」），仍是明確警告。 |
| 中-6 pre-flight 沒檢查 gh 登入 | 已做 | `lgwctl/migrate.py:1248-1250`（`install._gh_logged_in` false 就擋）、`:1279-1284`、`:1352-1357` | 測試 `:981`（`FAKE_GH_AUTH_RC=1`：outer 不排 transient、舊服務沒動）。 |
| 中-7 pre-flight 沒跑 policy dry-run | 已做 | `lgwctl/migrate.py:1251-1254`（`upgrade._dry_run_policy`）、`:944-946`（prepare 也跑） | 測試 `:990`（壞 policy 在停舊之前就 fail）。 |
| 中-8 最後的 check 又單探遠端 | 已做 | `lgwctl/migrate.py:1481`（`check.run(..., no_remote=True)`） | 既有測試 `:1084`（3 次 probe、check 不再探）。 |
| 中-9 symlink TOCTOU | 已做 | `lgwctl/migrate.py:291-305`（`_target_exists`）、`:305-341`（`O_NOFOLLOW`＋同 fd `fstat`＋從 fd 讀）、`:362-394`（copy 從 fd）、`:405-501`（SQLite 用 `/proc/self/fd`／唯讀 URI，退回 fd 複製）、`:506-543`（暫存 inode 再驗） | 測試 `tests/test_lgwctl_migrate.py:1840`（檢查後才換成 symlink，複製仍讀原 inode）、`:1873`（SQLite 來源／複製拒絕 symlink）、`:1884`（保留 mtime）。 |
| 中-10 舊機器誤跑 install | 部分 | `lgwctl/install.py:310-326`（四個舊 user unit 任一 active 就拒絕）、`:408-410`（動任何東西前擋）、`:299-305`（start 後 `is-active` 確認不是舊服務回答） | 測試 `tests/test_lgwctl_install.py:282`（舊 user unit 在跑→拒絕、不抓碼）、`:295`（list 200 但 unit 不 active→fail）。限制：prepare 看到目標 `llm.db` 已存在仍只 skip，沒有額外警告「可能是新服務自己建的空 db」；install 已先擋，殘留風險小。 |
| 中-11 回舊版 key-sync.timer 只 enable | 已做 | `lgwctl/migrate.py:125`（timer 在 `OLD_START_ORDER`）、`:1963-1990`（enable 四個＋start service／tunnel／timer） | 測試 `:1258` 新增 `user start key-sync.timer` 斷言。 |

## 低（13 項全部處理）

| 項 | 原判定 | 證據（程式:行） | 測試 |
|---|---|---|---|
| 低-1 `/etc/llmgateway` 權限 | 已做 | `lgwctl/install.py:195-198` | `tests/test_lgwctl_install.py:303`（目錄先 0700→install 後 0755）。 |
| 低-2 enable missing 當成功 | 已做 | `lgwctl/migrate.py:1990-1994`、`:1754-1764` | `tests/test_lgwctl_migrate.py:1475`（舊 unit 檔不在→rollback 不呼叫任何 stop）。 |
| 低-3 rollback 有 fail 行卻回 0 | 已做 | `lgwctl/migrate.py:1812-1814` | `tests/test_lgwctl_migrate.py:1449`（有 fail 行→rc 1）。 |
| 低-4 finalize／prepare 沒拿鎖 | 未做→已修 | `lgwctl/migrate.py:913-931`（prepare 拿鎖）、`:2038-2050`（finalize 拿鎖）、`:653-658`（註解一致） | `tests/test_lgwctl_migrate.py:735`（prepare 等不到鎖→fail、不抓碼）、`:1654`（finalize 等不到鎖→fail）。 |
| 低-5 finalize 只看本機 list | 已做 | `lgwctl/migrate.py:2081-2098`（本機 200 後跑完整 `check.run`） | `tests/test_lgwctl_migrate.py:1597`（本機 200、gh fail→不收舊版）。 |
| 低-6 keys.env 沒擋重複鍵 | 已做 | `lgwctl/migrate.py:583-589`（`seen` 擋重複） | `tests/test_lgwctl_migrate.py:1416`（重複鍵→不覆蓋舊的、todo）。 |
| 低-7 沒有 `new_service_ts` 時比 mtime | 未做→已修 | `lgwctl/migrate.py:1876-1919`（沒有切換時間就不覆蓋；`_db_has_new_writes` 只接受 `started`） | `tests/test_lgwctl_migrate.py:1429`（prepare 後直接回滾：不比 mtime、不留備份、skip 訊息寫明）。 |
| 低-8 check 原樣印 keysync reason | 已做 | `lgwctl/check.py:35-47`、`:137` | `tests/test_lgwctl_check.py:188`（escape／假值不印出）。 |
| 低-9 prepare 對已填 keys.pass 只 skip | 未做→已修 | `lgwctl/migrate.py:1089-1103`（skip 前 chmod 0640＋chown root:<群組>，失敗 fail） | `tests/test_lgwctl_migrate.py:705`（已填、mode 0600→skip 後 0640，chown 有呼叫）。 |
| 低-10 目標斷鏈被當「已存在」 | 未做→已修 | `lgwctl/migrate.py:291-305`（`_target_exists`：一般檔、非 symlink、size>0）、`:1033`／`:1053`／`:1133` | `tests/test_lgwctl_migrate.py:721`（`llm.db`／`tunnel.env` 斷鏈→替換成真檔）。 |
| 低-11 install 重跑不 restart／不提醒 | 已做 | `lgwctl/install.py:278-286` | `tests/test_lgwctl_install.py:311`（填完 keys.pass 重跑→todo restart、不重啟）。 |
| 低-12 sync 狀態檔寫失敗仍回 0 | 已做 | `lgwctl/sync.py:88-92` | `tests/test_lgwctl_cli_sync.py:160`。 |
| 低-13 confirm 的毫秒級 race／failed 保險 | 已做 | `lgwctl/migrate.py:1632-1658`（停 timer 後再驗 busy＋failed 要求人工） | `tests/test_lgwctl_migrate.py:1207`（failed 保險→拒絕 confirm）。限制：`write_state` 仍非 compare-and-swap（毫秒窗口只剩停 timer 前那一次；審查建議的 CAS 未做）。 |

## 任務原文的兩個演練補充

| 項 | 原判定 | 證據（程式:行） | 測試 |
|---|---|---|---|
| reverted 後可再 cutover | 已做 | `lgwctl/migrate.py:686-718`、`:1346-1350` | `tests/test_lgwctl_migrate.py:691`（prepare 歸檔）、`:836`（inner 歸檔後 pending-confirm）。 |
| 沒有狀態檔時 rollback 不印假 fail | 已做 | `lgwctl/migrate.py:1765-1767`（`ok rollback-state`）、`:1822-1826`（rc 看舊服務是否真的回來） | `tests/test_lgwctl_migrate.py:1485`（無狀態：ok、無 `fail revert-unit`、無「不是認得的值」）、`:1496`（舊服務起不來→rc 1）。 |

## 明確不修／限制（回報會再列一次）

- 中-2：反向保險仍是一次性 transient timer，不重試；本輪只保證「切換結束前確認 timer 還在，不在就 fail＋請人立刻 confirm／rollback」。
- 中-5：沒有 boot-time 檢查或 persistent 保險；只在停舊服務前印警告。
- 中-10：prepare 的「目標 db 已存在就 skip」沒有額外警告；install 已先擋舊 user 版，故殘留風險小。
- 中-4／低-13：`status` 不顯示 phase；`write_state` 沒有 compare-and-swap。兩者都不影響守門，只影響可觀察性／極小 race。
