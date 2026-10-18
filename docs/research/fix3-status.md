# fix3 對帳（第 3 輪修正；基準 commit d3f312b）

審查報告：`review-35-final.md`（1 高 11 中 13 低）。任務：`mig-fix3-35.md`＋`mig-fix2-35.md` 的兩個演練補充。
判定只看 `d3f312b` 的程式與測試，不看前一位的記憶。行號是 `d3f312b` 當下的行號；「缺什麼」是這次要補的。

判定：**已做**＝觸發路徑已關掉；**部分**＝主體在但仍有可重現殘留；**未做**＝程式沒動。

## 高

| 項 | 已做 | 證據檔案:行 | 缺什麼 |
|---|---|---|---|
| 高-1 rollback 停掉自己走的隧道 | 部分 | `lgwctl/migrate.py:1625-1674`（人手一律 `systemd-run --unit=lgw-rollback`）、`:1677-1693`（inner 持同一把鎖）、`:1696-1745`（先停新 service、restore、起舊、探 200，最後才停新隧道）、`:101-102`（module 說明）；測試 `tests/test_lgwctl_migrate.py:1520-1530`（argv 分離）、`:1128-1167`（順序，但沒斷言隧道最後停） | 缺「停新隧道之前確認舊隧道真的 active」：舊隧道若起不來，現在照樣切斷唯一的管理連線。缺「新隧道在 curl 200 之後才停」的斷言。 |

## 中

| 項 | 已做 | 證據檔案:行 | 缺什麼 |
|---|---|---|---|
| 中-1 keysync 等待用「現在」 | 已做 | `lgwctl/migrate.py:1389-1390`（啟動新服務前記 `cutover_started`）、`:1428-1431`（用它當 `since`）；假 systemctl 會模擬啟動時寫狀態 `tests/test_lgwctl_migrate.py:102-107` | 沒有測試（grace 非 0＋啟動時已有新狀態檔，斷言不出現假 todo）。 |
| 中-2 保險可能在切換中等鎖死掉 | 部分 | `lgwctl/migrate.py:1438-1449`（寫 pending 前確認 timer 還 active，不在就 fail＋todo）、`:164-169`（detached 等鎖 5 分鐘）、`:1681-1686` | 沒有測試「timer 在切換中消失」。保險仍是一次性 transient、不重試（只能明確告知），這一點要留在回報。 |
| 中-3 手動 rollback 沒換回舊 unit | 已做 | `lgwctl/upgrade.py:149-200`（先驗 previous 的 `deploy/units`，切 link 前拒絕；切完裝 unit＋reload，失敗連 unit／link 一起回復）、`lgwctl/units.py:31-75` | 測試只斷言 restart 次數（`tests/test_lgwctl_upgrade.py:236-246`），沒斷言 unit 檔內容回到舊 tag。 |
| 中-4 狀態機沒有重設／終態守門 | 部分 | `lgwctl/migrate.py:668-700`（歸檔成 `.cutover.state.<時間戳>`）、`:688-699`＋`:937-943`（prepare 開新一輪）、`:1297-1304`（cutover 拒絕進行中階段，reverted／aborted 可再切）、`:1707-1717`（finalized／舊 unit 檔不在，停新 unit 前拒絕）、`:1898-1902`（enable missing 算 fail）；測試 `tests/test_lgwctl_migrate.py:780-806` | `cutover` 的階段守門只在 inner；outer 仍會先排 transient。缺 finalized 後 rollback 不呼叫任何 stop 的測試、缺 prepare 歸檔的測試 `lgwctl/status.py` 仍不顯示 phase。 |
| 中-5 切換窗口重開機 | 部分 | `lgwctl/migrate.py:1333-1338`（(b) 之前印 reboot-window todo） | 只做了「警告」；沒有 boot-time 檢查／persistent 保險（審查建議的「更好做法」）。缺測試斷言警告存在。 |
| 中-6 pre-flight 沒檢查 gh 登入 | 已做 | `lgwctl/migrate.py:1212-1214`（`install._gh_logged_in` false 就擋）、`:1250-1255`（outer 不排程）、`:1305-1310`（inner 再擋） | 沒有測試（`FAKE_GH_AUTH_RC=1` 時 outer 不排 transient、不動舊服務）。 |
| 中-7 pre-flight 沒跑 policy dry-run | 已做 | `lgwctl/migrate.py:1215-1218`（`upgrade._dry_run_policy`）、`:912-914`（prepare 也跑）、`lgwctl/upgrade.py:49-63` | 沒有測試（壞 policy 時 cutover 在停舊之前就失敗）。 |
| 中-8 最後的 check 又單探遠端 | 已做 | `lgwctl/migrate.py:1434`（`check.run(..., no_remote=True)`）；測試 `tests/test_lgwctl_migrate.py:963-973`（3 次 probe，check 不再探） | 無。 |
| 中-9 symlink TOCTOU | 已做 | `lgwctl/migrate.py:287-311`（`O_NOFOLLOW`＋同 fd `fstat`）、`:314-334`（從 fd 讀）、`:344-376`（copy 從 fd）、`:385-465`（SQLite 用 `/proc/self/fd`／唯讀 URI，退回 fd 複製）、`:479-525`（暫存 inode 再驗一次）；文件說明限制 `:288-297` | 沒有直接測試 helper 拒絕 symlink／真的用 `O_NOFOLLOW`。 |
| 中-10 舊機器誤跑 install | 部分 | `lgwctl/install.py:310-326`（四個舊 user unit 任一 active 就拒絕）、`:408-410`（在動任何東西前擋）、`:299-305`（start 後 `is-active` 確認不是舊服務在回答） | 沒有測試 install 被舊 user unit 擋下。`prepare` 看到目標 `llm.db` 已存在仍只 skip（`lgwctl/migrate.py:1001-1005`），沒有警告「可能是新服務自己建的空 db」；因 install 已先擋，殘留風險較小，這次列為不修。 |
| 中-11 回舊版 key-sync.timer 只 enable | 已做 | `lgwctl/migrate.py:125`（timer 在 `OLD_START_ORDER`）、`:1885-1912`（enable 四個＋start service／tunnel／timer）；測試 `tests/test_lgwctl_migrate.py:1136` 只斷言 enable | 缺 `user start key-sync.timer` 的斷言。 |

## 低

| 項 | 已做 | 證據檔案:行 | 缺什麼 |
|---|---|---|---|
| 低-1 `/etc/llmgateway` 權限 | 已做 | `lgwctl/install.py:195-198`（`chmod etc_dir 0755`） | 缺測試（目錄先 0700，install 後 0755）。 |
| 低-2 enable missing 當成功 | 已做 | `lgwctl/migrate.py:1898-1902`（missing→fail）、`:1711-1717`（回滾前先驗四個舊 unit 檔） | 缺測試（舊 unit 檔不在時 rollback 不呼叫任何 stop）。 |
| 低-3 rollback 有 fail 行卻回 0 | 已做 | `lgwctl/migrate.py:1743-1745`（`has_fail` 影響 rc）；測試 `tests/test_lgwctl_migrate.py:1268-1280` | 無。 |
| 低-4 finalize／prepare 沒拿鎖 | 部分 | `lgwctl/migrate.py:1936-1949`（finalize 拿鎖）、`:653-658`（註解說 prepare 在鎖內） | `prepare` 的 `_start_new_round`＋`write_state`（`:937-943`）仍沒拿鎖，與註解不符。缺 prepare 與 finalize 的鎖測試。 |
| 低-5 finalize 只看本機 list | 已做 | `lgwctl/migrate.py:1984-1999`（本機 200 後跑完整 `check.run`，非 0 不收） | 測試只蓋本機 500（`tests/test_lgwctl_migrate.py:1368`），缺「本機 200 但 check 其他項 fail（例如 gh）就不搬」。 |
| 低-6 keys.env 沒擋重複鍵 | 已做 | `lgwctl/migrate.py:554-589`（白名單＋`seen` 擋重複） | 缺重複鍵測試。 |
| 低-7 `_restore_db` 沒 `new_service_ts` 時比 mtime | 未做 | `lgwctl/migrate.py:1828-1842`（沒有 `new_service_ts` 就比 mtime，prepare 快照可能被當成較新） | 改成「沒有切換時間就不覆蓋」＋專屬 skip 訊息＋測試。 |
| 低-8 check 原樣印 keysync reason | 已做 | `lgwctl/check.py:35-47`（白名單）、`:137`（套用）；測試 `tests/test_lgwctl_check.py:188-199` | 無。 |
| 低-9 prepare 對已填 keys.pass 只 skip | 未做 | `lgwctl/migrate.py:1057-1061`（有值就 skip，沒 chmod／chown） | skip 分支也要校正 0640／root:<群組>，失敗要 fail；補測試。 |
| 低-10 目標斷鏈被當「已存在」 | 未做 | `lgwctl/migrate.py:1003,1023`（`_exists`＝`lexists`） | 目標改用「一般檔（非 symlink）」判斷；斷鏈要替換。補 `llm.db`／`tunnel.env` 測試。 |
| 低-11 install 重跑不 restart／不提醒 | 已做 | `lgwctl/install.py:278-286`（active＋keys.pass 有值就 todo restart） | 缺測試。 |
| 低-12 sync 狀態檔寫失敗仍回 0 | 已做 | `lgwctl/sync.py:88-92`（`problem` 非空回 1）；測試 `tests/test_lgwctl_cli_sync.py:160-169` | 無。 |
| 低-13 confirm 的毫秒級 race／failed 保險 | 已做 | `lgwctl/migrate.py:1600-1611`（停 timer 後再驗 busy，`failed` 要求人工）；測試 `tests/test_lgwctl_migrate.py:1003-1024`（停完再驗） | 缺 failed 保險的拒絕測試（`write_state` 仍非 CAS，屬殘留，這次不改）。 |

## 任務原文的兩個演練補充

| 項 | 已做 | 證據檔案:行 | 缺什麼 |
|---|---|---|---|
| reverted 後可再 cutover | 已做 | `lgwctl/migrate.py:668-700`、`:1297-1304`；測試 `tests/test_lgwctl_migrate.py:780-789`（reverted 歸檔後 pending-confirm） | prepare 的歸檔路徑（`:937-943`）沒有直接測試。 |
| 沒有狀態檔時 rollback 不印假 fail | 已做 | `lgwctl/migrate.py:1718-1720`（`ok rollback-state`）、`:1743-1745`（rc 看舊服務是否真的回來）；測試 `tests/test_lgwctl_migrate.py:1282-1287` | 測試沒斷言「沒有 `fail revert-unit`」與「沒有『不是認得的值』」。 |

## 這次的處理順序

1. 先補高-1 的舊隧道健康檢查與順序測試。
2. 中-1、中-2、中-3、中-6、中-7、中-9、中-10、中-11 補測試（程式已做，缺覆蓋）。
3. 中-4：outer 也做階段守門，補 finalized／prepare 測試。
4. 低-4（prepare 拿鎖）、低-7、低-9、低-10 修程式＋測試；其餘低項補測試。
5. 全部 `python3 -m unittest discover -s tests` 跑一次。
