審查模型：GLM-5.3-Flash Free。

# 審查 #35 整合分支 `impl/install-simplify`（第二輪最終審查）

**審查對象**：`impl/install-simplify` 目前的 `review/final-35b` worktree，HEAD `92feedf`（merge `impl/migrate`）、`e1dddb7`（merge `impl/keysync-tunnel`）；第 3 輪修正的範圍是 T1 `2437170..b80292f`、T4 `f2154b5..4e1285c`、T2 `08c6abd`。核對基準是上一輪最終報告（`review-35-final.md` @ `6510782`：新高 1、中 11、低 13、資訊 7）與 `fix3-status.md` 的逐項對帳。

**任務**：(1) 對上一輪的高／中逐項判定已修／未修／部分（以本 branch 的檔案:行為證據）；(2) 裁定作者宣告的三個「不修／部分」項（中-2 保險一次性、中-5 只印重開機警告、中-10 prepare 對已存在的目標 db 只 skip）同意與否；(3) 只針對「真機切換」補掃新問題；(4) 跑全量測試。

**方法**：本 session 逐檔重讀 `lgwctl/migrate.py`（全檔）、`install.py`、`upgrade.py`、`check.py`、`sync.py`、`units.py`、`layout.py`、`release.py` 的相關函式與 `tests/test_lgwctl_migrate.py`（全檔）、`test_lgwctl_upgrade.py`、`test_lgwctl_install.py`、`test_lgwctl_check.py`、`test_lgwctl_cli_sync.py` 的對應段落；`fix3-status.md` 每一條的程式與測試行號都用本 session 的讀檔核過，不是照抄。沒有改被審程式、沒有 push、沒有碰真機；本報告是唯一新增檔。

**驗證**：

| 項目 | 結果 |
|---|---|
| `python3 -m unittest discover -s tests`（第 1 次，背景執行；僅末 5 行留存） | `Ran 551 tests in 483.493s` → `FAILED (failures=1, skipped=1)`（失敗明細被截掉了，見資訊-1） |
| `python3 -m unittest discover -s tests`（第 2 次，完整輸出留存） | `Ran 551 tests in 533.623s` → `OK (skipped=1)`（Python 3.12.3） |
| skip 的那一個 | root-only 的真 chown 測試（非 root 機器預期跳過；與 `fix3-status.md:7` 同一說法） |

測試數 551＝第 3 輪修完的 508 之上，再併入 `impl/migrate`（92feedf）與 `impl/keysync-tunnel`（e1dddb7）的測試。**第 1 次的 1 個 failure 在第 2 次沒有重現**——我傾向視為環境性 flake（詳見資訊-1），但在根因確認前不能當零。

**等級數量（新發現）**：高 0、中 1、低 0、資訊 3。

---

## 一、上一輪「高／中」逐項核對

判定沿用上一輪的定義：**已修**＝觸發路徑關閉且有測試；**部分**＝主體已修但仍有可重現的殘留；**未修**＝程式沒動。

| 項 | 判定 | 證據（本分支；皆為本 session 逐行核過） |
|---|---|---|
| 高-1 `rollback-to-user` 停掉自己走的隧道 | **已修** | 人手一律分離執行：`migrate.py:1687-1721`（排 `lgw-rollback` 後立即返回）；argv 測試 `tests/test_lgwctl_migrate.py:1790`（detached）；inner 要求 `INVOCATION_ID`（`:1702-1706`）；inner 持同一把 flock、等鎖上限放寬（`:1724-1740`）；本體順序完成重排：先拒絕 finalized／舊 unit 檔不在（`:1754-1764`）→ 停新 service 讓埠 → restore → enable／start 舊四個 → 探本機 200 → **舊隧道 active 之後才停新隧道**（`:1792-1806`；`_wait_old_tunnel` 輪詢到 deadline 為止 `:2003-2022`）；舊隧道起不來時**不停新隧道**、明確 fail 並提示重跑（`:1779-1781`, `:1804-1806`）；rc 把 `has_fail` 算進去（`:1813-1815`）。測試 `:1258`（新隧道在 curl 200 之後才停）、`:1395`（舊隧道起不來→新隧道留著→重跑補完）、`:1440`（舊服務起不來 rc 1） |
| 中-1 keysync 等待用「現在」當門檻 | **已修** | `cutover_started` 在啟動新服務（(c) 之前）記下（`migrate.py:1437`），`_wait_file(..., since=cutover_started)`（`:1475-1477`）；測試 `:956`（grace 5 秒＋啟動當下寫狀態→不印假 todo） |
| 中-2 保險可能在切換中等鎖死掉、卻仍宣稱保險在 | **部分（守門已修；「一次性不重試」宣告不修——本輪同意，見二）** | 寫 pending 前用 `units.is_active` 確認 timer 還在，不在就 fail＋todo「沒有自動回舊版」（`migrate.py:1487-1496`）；`--revert-after` 下限 5 分鐘＋理由（`:1276-1280`）；回滾側等鎖上限放寬給保險路徑（`:1728-1733`）。測試 `:972`（timer 消失→rc 1、`insurance` fail、`no-insurance` todo） |
| 中-3 手動 `rollback` 沒換回舊 unit | **已修** | `upgrade.py:149-200`：**切 link 之前**先驗 previous 有 `deploy/units`（`:168-175`，缺就拒絕且不動 current）；切完裝 unit（`:188`）＋reload（`:191`）才 restart；任一步失敗用 snapshot 連 unit／link 一起回復（`:189-198`）。測試 `tests/test_lgwctl_upgrade.py:236`（unit 內容回到舊 tag）、`:254`（previous 缺 unit 檔→先拒絕、current 不動） |
| 中-4 狀態機沒有重設／終態守門 | **部分（守門與歸檔已修；限制＝`status` 不顯示 phase，宣告不修——本輪接受）** | `OPEN_PHASES`（`migrate.py:138`）；outer 排程前守門（`:1291-1296`）＋inner 同守門（`:1346-1350`）；finalized 拒絕 rollback（`:1754-1757`）、舊 unit 檔不在先拒絕再說（`:1758-1764`）；`reverted`／終態先歸檔再開新一輪（`:686-718`）；enable 的 missing 算 fail（`:1976-1980`）。測試 `:691`（歸檔）、`:836`（inner 歸檔後可再切）、`:847`／`:864`（pending／confirmed／finalized 拒絕）、`:1473`（finalized 後 rollback 不 stop）、`:1485`（舊 unit 檔不在先拒絕） |
| 中-5 切換窗口重開機什麼都不會起 | **部分（警告已修；boot-time 檢查宣告不修——本輪同意，見二）** | `reboot-window` todo 印在**停舊服務之前**（`migrate.py:1383-1385`，在 `OLD_STOP_ORDER` 迴圈 `:1388` 之前）。測試 `:909`（警告存在且在 stop-old 之前） |
| 中-6 pre-flight 沒檢查 gh 登入 | **已修** | preflight 列 `problems` 在排 transient 之前：`install._gh_logged_in` false 就擋（`migrate.py:1253-1255`）。測試 `:981`（gh 沒登入→outer 不排程、舊服務沒動） |
| 中-7 pre-flight 沒跑 policy dry-run | **已修** | `upgrade._dry_run_policy` 進 preflight（`migrate.py:1256-1259`）；prepare 也提前跑（`:948-950`）。測試 `:990`（壞 policy 在停舊之前就 fail） |
| 中-8 最後的 check 又單次探遠端 | **已修** | cutover 內緊接的改為 `check.run(layout, no_remote=True)`（`migrate.py:1481`；遠端已由前面 3 次重試探針確認過）。測試 `:1084`（3 次重試、check 不再探） |
| 中-9 symlink TOCTOU（提權） | **部分（主體已修；殘留 1 處＝本輪新-中1）** | fd 化主體：`_open_regular`＝`O_NOFOLLOW`＋同一 fd `fstat`（`migrate.py:305-329`）；`_copy_into_place` 從 fd 讀再原子放（`:362-394`）；`_copy_db_consistent` fd→`/proc/self/fd` 唯讀 URI／退回 fd 複製（`:506-543`）；`_read_regular`（`:332-341`）與 `_keys_env_valid` 用「驗過的內容」驗鍵（`:578-607`）；目標端 `_target_exists` 擋斷鏈／空檔（`:291-302`）。舊端逐一核過：keys.env `:1025`、db `:1045`、tunnel／gateway env `:1077`、`.keysync.sha` `:1147` 都走 fd helper。測試 `:1869`（檢查後才換 symlink→仍讀原 inode）、`:1873`。**殘留**：`_step_keys_pass` 仍以路徑重開舊 `keysync.env`（`migrate.py:1103-1107`），見新-中1 |
| 中-10 舊機器誤跑 `install` | **部分（install 已擋；prepare 的 skip 保留＝宣告不修——本輪同意，見二）** | `install._step_refuse_migration`：四個舊 user unit 任一 active 就拒絕並指示改用 prepare（`install.py:310-326`），且放在任何動作之前（`:409`）；start 後再加 `is-active` 確認不是舊服務在答（`:299-305`）。測試 `tests/test_lgwctl_install.py:282`（舊機器→拒絕、不抓碼、git 沒動）、`:295`（list 200 但 unit 不 active→fail） |
| 中-11 回舊版 key-sync.timer 只 enable 沒 start | **已修** | `OLD_START_ORDER` 含 timer（`migrate.py:125`），`OLD_UNIT_NAMES` 四個全部 enable（`:126`、`:1971-1982`），`_start_old_units` 對 service／tunnel／**timer** 都 start（`:1983-1989`）。測試 `:1258`（actions 含 `user start key-sync.timer`） |

**低項（13）**：全部處理（`fix3-status.md:31-47`）。本 session 另抽核：低-1（`install.py:195-198`＋測試 `:303`）、低-2（`:1976-1980`）、低-3（`:1813-1815`＋測試 `:1449`）、低-4（prepare 拿鎖 `:924-935`、finalize 拿鎖 `:2038-2050`，測試 `:735`／`:1683`）、低-5（finalize 跑完整 check `:2087-2099`＋測試 `:1626`）、低-6（重複鍵 `:600-604`＋測試 `:1416`）、低-7（無切換時間不覆蓋 `:1876-1884`＋測試 `:1429`）、低-8（`check.py:35-47`＋測試 `:188`）、低-9（skip 前仍 chmod／chown `:1091-1102`）、低-10（`:291-302`＋`:1035`／`:1055`／`:1136`）、低-11（`install.py:278-286`＋測試 `:311`）、低-12（`sync.py:88-92`）、低-13（停 timer 後再驗 busy＋failed 拒絕 `:1632-1658`＋測試 `:1207`）。任務原文的兩個演練補充（reverted 後可再切、無狀態檔的 rollback rc）核過：`:686-718`＋`:1346-1350`／測試 `:691`、`:836`；`:1765-1767`＋`:1813-1815`／測試 `:1495`、`:1506`。

---

## 二、作者宣告「不修／部分」的三項裁定

| 項 | 作者主張 | 本輪裁定 | 理由 |
|---|---|---|---|
| 中-2 保險一次性 transient、不重試 | 不修（只保證不宣稱不存在的保險） | **同意不修** | 一次性 transient 是設計取捨（`install-simplify-design.md` §7）；若改成 persistent，等於給「保險到點但無人 confirm」的狀態加一份永久自動回滾，反而要處理它與 confirm/finalize 的競爭。本輪驗證真正危險的一半已關掉：timer 在切換中死掉時不再宣稱保險還在（`migrate.py:1487-1496`，rc 1＋fail＋todo 指名 confirm／rollback）。殘留是「保險死掉後也沒有第二次機會」——這個風險現在是**明示的**而不隱瞞的。但正因無自動重試，資訊-3 的演練項（故意拖過 revert-after、看保險實際行為）從「建議」升級為合併前應做的驗證。 |
| 中-5 窗口內重開機無 boot-time 補救 | 不修（只印警告） | **同意不修** | 警告已移到停舊服務**之前**（`migrate.py:1383-1385`、測試 `:909`）。分析窗口本身：(b) stop／disable 舊四個與 (c) enable 新兩個之間是**秒級**的死窗；(c) 之後重開機，新 unit 已 enable，systemd 開機會自動把它們拉起來，機器不是空的，只是狀態停在 `cutover-started`——`confirm` 拒絕但 `rollback-to-user` 這條路完整可用（非 finalized 都可行，`migrate.py:1743-1815`）。boot-check unit 是安全的長期改善，但它自己也是一個 boot 路徑元件（要驗證、要避免它跟 rollback 搶），這輪為了一個秒級窗口上線不划算。條件：演練 runbook 必須含「窗口內不要重開機；若在窗口內 crash／重啟，console 進去看 `status`，處置是 rollback-to-user」。 |
| 中-10 prepare 對目標 db 已存在只 skip | 不修（install 已先擋舊機器） | **同意不修** | 原問題的主觸發路徑（舊機器上先 install 出空 db，再 prepare 看到已存在而 skip）已被 `install._step_refuse_migration`（`install.py:310-326`，在任何動作之前 `:409`）關閉。剩下的 skip 分支（`migrate.py:1035-1037`）服務的是「重跑 prepare 不要覆蓋既有資料」的正確需求，且輸出有 skip 行可見、`--force` 有明確出口。建議（不擋合併）：skip 訊息補半句「若是新服務自己建的空 db，請本人確認或 `--force`」，把這個殘留的可觀察性也補上。 |

---

## 三、新發現

### 中（1）

#### 新-中1 中-9 的 fd 化漏了一處：`_step_keys_pass` 還是用路徑重開舊 `keysync.env`，TOCTOU 提權殘留

- **檔案:行**：`lgwctl/migrate.py:1103-1107`（`_regular_file` lstat 檢查 → `parse_env_file(old.keysync_env_file)`）、`lgwctl/layout.py:127-138`（`parse_env_file` 是**照路徑** `open`）、`migrate.py:92-93`。
- **問題**：中-9 的修法是「開了 fd 之後只信 fd」（`_open_regular`，`migrate.py:305-329`），並把所有讀舊檔的地方改成 fd／`parse_env_text`（文件字串自己也寫了這個意圖：`layout.py:105-106`「從驗過的 fd 讀進來的文字直接解析，**不再用路徑重開**」）。但 `_step_keys_pass` 的檢查（`:1103`）與取值（`:1107`）**不是同一個 fd**：lstat 通過後，`parse_env_file` 再照路徑 `open` 一次。`migrate.py:93` 明明已經 import 了 `parse_env_text`，卻沒有在這支函式用上——修法的最後一步漏了這個呼叫點。窗口期內 llmgw 把 `~llmgw/.../keysync.env` 在一般檔與「指向 root 可讀檔的 symlink」之間切換，趕在 `:1103` 的 lstat 與 `:1107` 的 open 之間換成 symlink，root 就會把目標檔裡 `LLMGW_KEYS_PASSWORD=` 那行的值當成舊 key 密碼，寫進 `keys.pass`（root:llmgw 0640），**llmgw 讀得到**。（全倉掃過：其餘讀舊檔的路徑都已 fd 化——keys.env `:1025`、db `:1045`、env `:1077`、sha `:1147`、`_keys_env_valid` `:585`；`:1107` 是唯一殘留。）
- **情境**：與中-9 同一威脅模型（被入侵的 llmgw）。prepare 的密碼步驟每次重跑都在窗口；llmgw 不需贏每一次，贏一次就把 root 文件裡的一行密碼納入可控內容。競態窗比原本「檢查→copy2」窄（lstat 與 open 之間，微秒～毫秒級），需要循環重試，但攻擊方有的是窗口前的空閒時間。
- **建議**：`_step_keys_pass` 改成：`raw = _read_regular(old.keysync_env_file, MAX_KEYS_ENV_BYTES + 1)`（它就是 `O_NOFOLLOW`＋同 fd `fstat`＋從 fd 讀，`migrate.py:332-341`；檔案不在回 `None`，正好接上既有「沒有值→todo」分支 `:1108-1120`），再把 `raw.decode()` 的文字丟給已 import 的 `parse_env_text`（`layout.py:102`）取值；`:1103-1106` 的 lstat 步驟可以拿掉（判準已含在 `_read_regular` 內）。若 `parse_env_file` 自此不再被 migrate 使用，順手把 `migrate.py:92` 的 import 移除。測試：沿用 `tests/test_lgwctl_migrate.py:1869`（檢查後才換成 symlink→仍讀原 inode）的鉤子，補 keys-pass 一例；並補「keysync.env 是 symlink」直接拒絕的斷言。

### 低（0）

本輪沒有可確認的新低項。

### 資訊（3）

#### 資訊-1 測試：一次 failure、重跑全綠；數量與環境要交代

第 1 次（背景）：`Ran 551 tests in 483.493s` → `FAILED (failures=1, skipped=1)`；可惜我當時的指令用 `| tail -5` 收尾，failure 名單與 traceback 沒留住。第 2 次（前台、完整輸出）：`Ran 551 tests in 533.623s` → `OK (skipped=1)`（Python 3.12.3）。skip 的那一個是 root-only 真 chown 測試，非 root 機器預期行為（`fix3-status.md:7` 相同）。兩次之間程式沒有改動，failure 沒有重現，我判為 flake；合理嫌疑人選是這套件多個靠 sleep／期限的時間敏感測試，以及當時機器上**另一個 session 殘留的 `python3 -m unittest tests.test_bootstrap -v`（自 Oct 12 起掛了 6 天的行程）**與本輪並存。輸出裡另有一個無礙的雜音：一次 `ResourceWarning: unclosed <socket.socket ...>`（threading；laddr 127.0.0.1:64500，本機 loopback）。建議：合併前在乾淨環境（無並存測試行程）再跑一次做最終記錄；如果持續出現同則 failure，那才是真回了 regression。

#### 資訊-2 `docs/deploy.md` 仍是舊 user 層流程（上一輪資訊-5，未落地）

`grep llmgwctl|prepare-migration docs/deploy.md` 零命中：文件仍寫 `~/.config/systemd/user`、`install.sh --tag` 的舊世界。中-10 的誤用面已被程式擋掉大半，但操作者照這份文件走，與新指令完全接不上。真機切換前這份文件要改寫（或明確指向新的流程文件）。

#### 資訊-3 保險全鏈（timer→service→rollback）仍無自動化覆蓋；配合中-2／中-5 的裁定，演練清單應加兩項

假 `systemd-run` 只記 argv 不真排程，保險實際到點―等鎖―回滾的行為只有演練能驗（上一輪資訊-2 位置，本輪仍在）。既然中-2 採「保險一次性不重試」、中-5 採「只印警告」，演練清單要有：(1) 切換故意拖過 `--revert-after`，確認保險與鎖的實際互動與 state 終態；(2) 窗口內 kill/Ctrl-C 切換兩次，確認 crash／重啟後 `status` 顯示、`rollback-to-user` 可完整收尾。

---

## 四、結論

上一輪的 1 高＋11 中：**6 項已修、5 項部分**；部分的五項裡，中-2／中-5／中-10 的殘留是作者宣告的不修，本輪逐項裁定同意（條件寫在二）；中-4 的殘留只是可觀察性（status 不顯 phase），可接受；**中-9 的殘留是一處真實的安全競態（新-中1）**：`_step_keys_pass` 仍以路徑重開服務帳號控制下的檔案，fd 化的修法明明已備好（`parse_env_text` import 了沒用）。這不是新戰場，是把中-9 收完的最後一步，改動一個函式、十行以內，測試模式現成（`:1869`）。13 個低與兩個演練補充全部核實已修。測試 551 個，重跑 OK（flake 見資訊-1，建議合併前乾淨重跑一次做記錄）。

結論：需修（僅 新-中1 一處；修掉即可合併，不必再開一輪完整審查，spot review 該函式＋全量測試綠即可）。
