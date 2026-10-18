Claude Sonnet 5.5（claude-sonnet-5-5）

# #35 第 3 輪最終審查（放行前最後一道；分支 review/final-35c，基準 f7f8959）

範圍：`lgwctl/{migrate,install,release,upgrade,units,check,probe,sync,layout,atomic}.py`、`lgwd/keysync.py`、`deploy/units/*.service`、`ops-scripts/llmgateway-tunnel`、`docs/deploy.md`；對照 spec-35、設計 §4 §5 §7、`fix3-status.md`。
測試：`python3 -m unittest discover -s tests` → `Ran 554 tests in 82.251s` → `OK (skipped=1)`（沒有失敗）。
威脅模型：服務帳號 `llmgw` 可能被攻破（lgwd 吃的是 loopback 上的請求）；`prepare-migration`／`cutover`／`rollback-to-user`／`finalize-migration` 都以 root 跑，且大量碰 llmgw 擁有的路徑。

---

## 高（1）

### 高-1 root 對 llmgw 可控路徑做 `chmod`／`chown`（跟 symlink）＝ llmgw → root 提權

- **檔案:行**：`lgwctl/install.py:181-198`（`_step_dirs`：`os.makedirs(ssh_dir, exist_ok=True)`、`os.chmod(ssh_dir, 0o700)`、`_chown_service(ssh_dir)`；`prepare` 在 `lgwctl/migrate.py:942` 呼叫它）、`install.py:90-97`（`_chown_service` ＝ `os.chown`，跟 symlink）、`install.py:231-232`（`_chmod_quiet(keys_env)`）、`lgwctl/migrate.py:361-393`（`_copy_into_place` 在 llmgw 擁有的目錄內 `mkstemp` → `os.chmod(tmp)`／`os.utime(tmp)`（:380-381，用路徑）→ `os.replace` → 呼叫端再 `_chown_service(dst)`：`:1025`、`:1045`、`:1160`、`:1908`、`:1944`、`:1968`）、`lgwctl/atomic.py:17-20`（同型：`os.chmod(tmp)` 用路徑）。
- **問題**：前幾輪把「root 讀舊檔」補成 `O_NOFOLLOW`＋fd（中-9），但**寫／改權限／改擁有者**這一側還是用路徑、跟 symlink。`~llmgw`（含 `~llmgw/.ssh`、`~llmgw/.local/state/llmgateway`）與 `/var/lib/llmgateway`（0700 llmgw）全在 llmgw 手上。
- **情境**（不需要競態）：已被攻破的 llmgw 事先把 `~llmgw/.ssh` 換成指向 `/etc`（或 `/etc/sudoers.d`、`/usr/local/bin`）的 symlink。PM 在真機跑 `prepare-migration`（或重跑 `install`）→ `os.makedirs(..., exist_ok=True)` 因為 symlink 指到目錄而通過 → `os.chmod(ssh_dir, 0o700)`＋`os.chown(ssh_dir, llmgw)` 作用在 `/etc` → llmgw 擁有 `/etc`，改 `sudoers`／`passwd` 即 root。我在 scratchpad 用同樣兩行驗過：`makedirs(exist_ok)`＋`chmod` 會改到 symlink 指向的目錄（0755→0700）。有競態的變體：`_copy_into_place` 的 `os.replace(tmp, dst)` 與緊接的 `os.chown(dst)` 之間，llmgw（擁有該目錄）把 `dst` 換成指向 `/etc/shadow` 的 symlink，root 就把 `/etc/shadow` chown 給 llmgw（`rollback-to-user` 寫的是 `~llmgw/.local/state/llmgateway/`，同樣可被這樣打；`os.chmod(tmp)` 同理可被換成 symlink）。
- **建議**（改法小、都在這兩個檔）：(1) 凡是 llmgw 擁有的目錄／檔，一律「先開 fd 再改」：目錄用 `os.open(path, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)` 後 `os.fchmod`／`os.fchown`；`_copy_into_place`／`atomic.write_bytes` 在 `os.replace` **之前**對暫存檔的 fd 做 `fchmod`／`fchown`（owner 以參數傳入），之後不再用路徑 chown；(2) `_step_dirs` 對 `.ssh`／state 先 `lstat` 拒絕 symlink（`fail`＋說明），`_chmod_quiet` 同；(3) 補一個測試：`~svc/.ssh` 是 symlink 時 `prepare`／`install` 必須 fail 且目標目錄權限不變。
- **若 PM 決定不先改碼就切**（不建議）：切換前在真機人工確認 `find ~llmgw/.ssh ~llmgw/.local/state/llmgateway /var/lib/llmgateway -maxdepth 1 -type l` 沒有輸出、`stat -c '%U %a' ~llmgw/.ssh` 正常，且 prepare 到 cutover 期間不要讓不明行程以 llmgw 身分在跑（`ps -u llmgw`）；這只能降低、不能消除競態變體。

---

## 中（4）

### 中-1 `prepare-migration` 沒有階段守門：`--force`（或換 tag）在切換後會用舊資料覆蓋正在跑的新 `llm.db`／`keys.env`／設定

- **檔案:行**：`lgwctl/migrate.py:937-980`（`_prepare_locked` 只在 `_start_new_round` 處理終態 reverted／aborted，沒有檢查 `OPEN_PHASES`）、`:1005`／`:1034`／`:1054`／`:1090`（`--force` 時略過「已存在不覆蓋」）、`:951`（`switch_current`）。
- **問題**：`cutover` 有守門（`:1304-1308`、`:1358-1361`），`prepare` 沒有。`pending-confirm`／`confirmed` 期間（新服務已在跑、舊的已 disable）手滑再跑 `prepare-migration --tag X --force`，會把**舊** state 的 `keys.env`、`llm.db`、`tunnel.env`、`keysync.env` 蓋到新位置。`llm.db` 是 `os.replace`：新服務還握著舊 inode 繼續寫（寫進已被 unlink 的檔），下次重啟才讀到舊資料＝觀察期內的冷卻與稽核遺失，且沒有任何警示。換不同 tag 的 prepare 也會切 `current` 並換 unit 檔。
- **情境**：觀察期第 2 天，PM 想「重新準備一下」→ `prepare-migration --force`；服務看起來正常，下一次 `upgrade`／重啟後冷卻資料倒回切換前。
- **建議**：`prepare` 拿到鎖後，若 `phase in OPEN_PHASES`（cutover-started／pending-confirm／confirmed／finalized）就 fail 並指示用 `rollback-to-user` 開新一輪；至少在這些階段拒絕 `--force`。補測試（phase=confirmed ＋ --force → 不動任何檔）。

### 中-2 `cutover` 不重新快照 `llm.db`：prepare 與 cutover 之間舊服務寫的冷卻／稽核都帶不過去，重跑 prepare 也不會刷新

- **檔案:行**：`lgwctl/migrate.py:1032-1049`（`_step_db` 只在 prepare 快照一次，目標已存在就 skip）、`:1352-1519`（`_cutover_locked` 停完舊服務後沒有任何資料同步）。
- **問題**：設計 §5 把「複製 `llm.db`」放在準備階段，而準備與切換之間可以隔數小時到數天（規格也允許）。舊服務持續寫 `cooldowns`／`audit`；切換時新服務用 prepare 當下的舊快照啟動，中間的冷卻就不見（可能把還在冷卻的訂閱當成可用 → agent 吃 429），稽核缺一段。第二次 prepare 因為目標已存在而 skip，同樣不刷新。回滾時 `_restore_db` 反過來用「新 db」覆蓋舊 db（舊的只留 `.bak-*`），所以間隙資料只存在於 `.bak` 或舊檔，沒有人會去合併。實機沒有舊檔就沒這個問題，但這台有。
- **情境**：週一 prepare、週三 cutover；週二的冷卻登錄在新服務看不到。
- **建議**（擇一）：(a) 在 `_cutover_locked` 停完舊 unit（`stopped-check` 之後、`(c)` 之前）且新服務從未寫過（`new_service_ts` 剛設）時，再做一次 `_copy_db_consistent(old.db → new db)`＋複製較新的 `keys.env`／`.keysync.sha`（此時舊服務已停，沒有寫入者，也沒有「新 db 已被寫過」的風險）；(b) 最低限度：deploy.md 要求「prepare 與 cutover 之間不超過幾分鐘，且 cutover 前一刻再跑一次 `prepare-migration --force`」（注意 --force 受中-1 影響）。

### 中-3 `preflight` 沒檢查四個舊 unit 檔都在；缺一個時，停損回滾會拒絕執行 → 舊的已停、新的壞，沒有自動回復

- **檔案:行**：`lgwctl/migrate.py:1236-1272`（`preflight` 無此檢查）、`:1400-1403`（停舊時 missing 當 skip 繼續）、`:1770-1776`（`_rollback_locked` 舊 unit 檔缺任一個就 `fail` 並 return 1，「不動新 unit」）。
- **問題**：`_rollback_locked` 為了不在 finalize 後把機器弄到沒服務，要求 `OLD_UNIT_NAMES` 四個檔都在。但切換時缺檔只會被當「本來就沒在跑」放過。若真機的舊版缺任何一個（例如 `key-sync.timer` 被人工改名／刪除），一旦停損觸發（或 10 分鐘保險到點），回滾在 `:1770` 就拒絕：舊服務已停＋disable、新服務若壞則什麼都沒有；保險只有一發，也同樣拒絕。
- **情境**：舊版 4 個 unit 檔少一個 → cutover 在 `(d)` 本機 list 非 200 → 停損 → `rollback` 印「舊 unit 檔不在」→ API 與隧道都不在，要靠 console。
- **建議**：`preflight` 加「`OLD_UNIT_NAMES` 的檔都存在且是一般檔」（缺則在停舊之前拒絕）。這是一行檢查，把一個靜默的無回復路徑變成明確拒絕。

### 中-4 回滾時「舊隧道是否活著」的把關無效：它取樣 `is-active`，crash-loop 的舊隧道也會過；過了就停掉新隧道

- **檔案:行**：`lgwctl/migrate.py:2015-2034`（`_wait_old_tunnel` 只要某一次輪詢看到 `active` 就回 True）、`:1813-1818`、`:1786-1794`；隧道單位 `Restart=always RestartSec=10`＋`ExitOnForwardFailure=yes`（`ops-scripts/llmgateway-tunnel`：`tunnel_argv`、結束碼 1；舊 user unit 同）。
- **問題**：fix3 高-1 要的保證是「舊隧道真的好了才斷新隧道（管理連線）」。但：(1) 回滾時新隧道還佔著會合點的 4710／4711，舊隧道起來的 `ssh -R` 會因 `ExitOnForwardFailure` 在 1～3 秒內退出，下一輪 10 秒後重來——unit 在每個循環裡有 1～3 秒是 `active`，30 秒內每秒輪詢幾乎一定抓到一次，所以 `tunnel_ok=True`、立刻停新隧道，之後舊隧道才（靠自己的重試）連上；它「剛好能用」是靠運氣而不是靠把關。(2) 反過來，舊隧道真的壞了（金鑰被動過、known_hosts 不對、設定檔壞）時，它一樣是 1～3 秒就退出的 crash-loop，同樣的取樣照樣過，新隧道被停掉 → API 隧道與管理連線同時消失，且沒有重試（高-1 想擋的情境，仍然存在）。(3) 取樣沒抓到時走 fail 分支：`rc=1`、新隧道留著、訊息叫人「修好後再跑一次」，但重跑（`reverted` 分支，`:1780-1802`）一樣得等舊隧道在新隧道還佔著埠時 `active`，並沒有補完機制；需要人手 `systemctl stop llmgateway-tunnel`。
- **情境**：保險到點的回滾，舊隧道因 `known_hosts` 路徑問題 crash-loop → `is-active` 取樣到 active → 新隧道被停＋disable → 會合點上什麼都沒有，PM 失聯。
- **建議**：改成「停新隧道 → 等舊隧道穩定 → 失敗就把新隧道叫回來」：舊服務 list 200 後，(1) 記下 `ActiveEnterTimestamp`／`NRestarts`，停新隧道；(2) 輪詢到舊隧道連續 ≥ 15～20 秒維持 `active` 且 `NRestarts` 不增，**並**用 `probe.remote`（此時佔著會合點埠的只剩舊隧道）回 200；(3) 逾時就 `enable`＋`start` 新隧道（回到「管理連線一定有一條」的不變式）、`fail` 並結束。這也讓 `reverted` 的重跑有意義。

---

## 低（5）

### 低-1 `docs/deploy.md` 與實際 CLI 不一致／缺漏（讀過每個子指令對照）

- `docs/deploy.md` 步驟 5：「搬移歸檔至 `~llmgw/old-user-layout/`」 ↔ 程式是 `<state>/old-user-layout`＝`/var/lib/llmgateway/old-user-layout`（`migrate.py:2126`）。
- §5.2：「`status` 印出…以及金鑰同步記錄」 ↔ `lgwctl/status.py` 只印 current／previous／兩個 unit 狀態，沒有 keysync、也沒有遷移 phase（fix3 已承認 status 不顯示 phase）；文件沒告訴人怎麼看 phase（`/var/lib/llmgateway-ctl/.cutover.state`、`journalctl -u lgw-cutover|lgw-rollback|lgw-revert`）。
- 步驟 2：沒說 `cutover` 人手那一下**立刻返回**、結果要重連後看 `journalctl -u lgw-cutover`，沒說最後成功的標誌是 `ok confirm-needed`；沒列 preflight 會擋的條件（`keys.pass` 要有值、`tunnel.env` 就緒、`gh` 已登入、`current` 要等於 tag、policy dry-run）；沒列旗標 `--no-remote`、`--old-home`、`prepare-migration --force`。
- 步驟 2「停損：切換後 5 分鐘內…」 ↔ 程式沒有 5 分鐘計時：是 60 秒本機、30 秒隧道、3×10 秒遠端、最多 90 秒等 keysync、再 `check`（`migrate.py:1449-1495`）；真正的兜底是保險 timer（預設 10 分鐘）。建議照程式寫。
- §5.5／§6.3：`llmgwctl sync` 後或改 `keys.pass` 後**要 `systemctl restart llmgateway`**（`LoadCredential` 是啟動快照；程式印 `todo restart`，文件沒寫）。
- §1.3：`useradd -m -s /bin/bash llmgw` ↔ 程式用 `useradd -m -U ...`（要有同名群組給 `Group=llmgw`；多數發行版預設就有，但 `USERGROUPS_ENAB=no` 時會不一致）。
- 沒有「切換前後操作者要做什麼」（見資訊-2 的清單）。

### 低-2 root 讀／寫 llmgw 可控目錄時只擋最後一段 symlink，上層目錄元件沒擋

- **檔案:行**：`lgwctl/migrate.py:304-328`（`O_NOFOLLOW` 只對最後一個路徑元件有效）、`:609-614`、`:1181-1212`（`OldLayout` 的各路徑都在 `~llmgw/.config`、`.local/state`、`.local/share` 底下）。
- **問題／情境**：llmgw 把 `~llmgw/.local/state/llmgateway`（或其上層）換成指向別的目錄的 symlink：prepare 會去讀那個目錄裡名叫 `keys.env`／`llm.db`／`keysync.env`／`tunnel.env`／`llmgateway.env`／`.keysync.sha` 的檔（root 可讀的同名檔被交給 llmgw，例如別處的 `tunnel.env`；`keysync.env` 的 `LLMGW_KEYS_PASSWORD` 會被抄進 `keys.pass`）；`rollback-to-user` 則會把 `llm.db`／`keys.env`／`llm.db.bak-*` 寫進那個目錄（與高-1 同源）。檔名固定、內容有限，所以只列低；修高-1 時一併處理最好。
- **建議**：用 `os.open(home, O_DIRECTORY|O_NOFOLLOW)` 為根，逐段 `openat`（`dir_fd=`，`O_NOFOLLOW`）；或至少 `os.path.realpath(dir)` 必須在 `old.home` 底下且每段 `lstat` 非 symlink。

### 低-3 `_copy_into_place` 在 `mkstemp` 失敗時洩漏已開的來源 fd

- **檔案:行**：`lgwctl/migrate.py:370-373`（`src_fd` 開了之後，`tempfile.mkstemp` 在 `try` 之外；它丟 `OSError`（磁碟滿／目錄權限）就不會走到 `finally` 關 `src_fd`）。
- **影響**：行程短命，只是洩漏一個 fd 然後以例外結束；`_step_*` 的 `except OSError` 會接住。把 `mkstemp` 移進 `try` 即可。

### 低-4 以 root 開 llmgw 寫的 SQLite 檔（`PRAGMA integrity_check`、backup 來源）

- **檔案:行**：`lgwctl/migrate.py:402-419`、`:484-493`、`:505-542`。
- **問題**：新 `llm.db`（回滾時）與舊 `llm.db`（prepare 時）都是 llmgw 可任意寫的內容，root 用預設設定的 `sqlite3` 打開並跑 `integrity_check`／`backup`（`trusted_schema` 預設開）。SQLite 對惡意 schema（view／trigger）有已知攻擊面；這裡沒有載入擴充、也沒有跑使用者查詢，風險低。
- **建議**：連線後先 `PRAGMA trusted_schema=OFF`（Python 3.12+ 可用 `setconfig(SQLITE_DBCONFIG_DEFENSIVE)`）；或把快照步驟改成以 llmgw 身分的子行程做。

### 低-5 回滾時舊服務起不來沒有「回頭把新服務叫回來」

- **檔案:行**：`lgwctl/migrate.py:1805-1827`（`_stop_new_service` 先停＋disable 新服務；舊服務 `start`／list 失敗後只 `fail` 結束）。
- **影響**：手動回滾（觀察期、新服務原本健康）若舊服務起不來，結果是 API 沒有任何服務在回答（管理連線因新隧道留著還在，所以人還進得來）。非停損路徑時可以考慮在 `ok` 為 False 時 `enable`＋`start` 新服務當退路。只是品質改善，不擋放行。
- 另：`cutover` 在 `(c)` 之後被中斷／重開機，狀態會停在 `cutover-started`：`confirm` 拒絕、`cutover` 拒絕，只能 `rollback-to-user`（無「接手」路徑）。可接受，但 deploy.md 應寫明。

---

## 資訊

### 資訊-1 對任務問題的逐項判斷

**1. 時序與失敗留下的狀態**（逐步想過，除高-1／中-3／中-4 外沒有新的「API＋隧道同時下線且無回復」路徑）

| 步驟失敗／中斷 | 留下的狀態 | 回復 |
|---|---|---|
| `prepare` 任一步 | 只多了 `/opt`、`/etc`、`/var/lib` 與未啟用的 unit；舊的完全沒動 | 重跑（已存在不覆蓋；注意中-1、中-2） |
| `cutover` outer（preflight／排 transient） | 舊的沒動 | 修好重跑 |
| inner：排保險失敗 | 舊的沒動、不切（`:1372-1381`） | — |
| inner：停舊／disable 失敗 | `_restore_old_units`＋取消保險＋`aborted`（`:1405-1437`） | 自動 |
| inner：停舊後行程被殺／機器掛掉（未重開機） | 狀態 `cutover-started`，保險 timer 還在 → 10 分鐘後 `rollback-to-user` 拿鎖回舊版 | 自動（受中-3 條件影響） |
| inner：新服務／隧道起不來、list 非 200、check≠0 | `_stop_loss` → `_rollback_locked` | 自動（隧道把關見中-4） |
| `confirm` 之前 SSH 斷線 | 不影響（transient unit 分離執行） | — |
| pending-confirm 重開機 | 新版自己起（已 enable）、舊的已 disable，不會雙跑；保險 timer 消失；不 confirm 也不會自動回滾，永遠停在 pending | 操作者 `confirm` 或 `rollback-to-user` |
| 窗口內重開機（見下） | 兩邊都不起 | **console** |

**已知殘留一：保險是一次性 transient timer（不重試）——真機上可接受**，條件是：保險真正回滾失敗時，程式的設計是「新隧道留著」（`:1816-1818`、`:1789-1793`），管理連線仍在、API 可能停擺，不會同時失聯——前提是中-4 修掉（目前把關無效，這個前提不成立）。保險跑到一半失敗時 `confirm` 也會拒絕（`:1666-1670`）。

**已知殘留二：窗口內重開機只印警告——可接受，但警告字句高估了範圍。** 真正「兩邊都不起」的只有「第一次 `disable` 舊 unit」到「`(c)` 的 `enable` 新 unit」之間（數秒到最多一兩分鐘：停舊服務＋is-active 確認）。`(c)` 之後新 unit 已 enable，重開機會起新版（舊的已 disable）；只是保險沒了、狀態停在 `cutover-started`／`pending-confirm`。`:1395-1397` 的警告寫成「(b)～(e) 期間」過於嚴格，不影響安全；可改寫成「(b)～(c)」。

**操作上的緩解（PM 在切換前後要做的）**
1. 切換前確認有**不經隧道的進場方式**（雲端 console／區網 ssh）。隧道是唯一管理通道時，任何兩邊都不起的路徑（重開機窗口、中-3、中-4）都只能靠 console。
2. 切換前：`ls /var/run/reboot-required`、`systemctl list-timers`、檢查 `unattended-upgrades` 沒有排自動重開機；暫停同機其他維護作業。`systemctl list-units 'lgw-*'` 確認沒有上次留下的 transient；`cat /var/lib/llmgateway-ctl/.cutover.state` 確認不是進行中階段。
3. 高-1 的人工預檢（見上）；`ps -u llmgw` 確認只有預期行程。
4. `prepare-migration` 與 `cutover` 之間盡量短（中-2 沒修時），cutover 前一刻再備份舊 state（`sqlite3 <舊 llm.db> ".backup /root/llm.db.pre-cutover"`＋`cp -a` 舊 `keys.env`）。
5. `--revert-after 15m`（下限 5m；預設 10m 對「重連＋讀 journal＋跑 check＋confirm」偏緊）。人手那一下一返回連線就會斷（隧道要換）；第二個終端機準備好，約 3～7 分鐘後重連（走新隧道），依序看 `journalctl -u lgw-cutover -n 100`、`llmgwctl check`，再 `llmgwctl confirm`。連不上就等保險（或 console），不要在窗口中重開機。
6. `confirm` 之後觀察期內重開機是安全的；pending-confirm 期間重開機後要立刻 `confirm` 或 `rollback-to-user`。
7. 在 #35 留「要切了」與切完結果（不含值），觀察期 3 天再 `finalize-migration --yes`。

**2. root／llmgw 邊界**
- 好的：控制檔與鎖在 `/var/lib/llmgateway-ctl`（0700 root）（`migrate.py:104-106`、`:651-657`）；狀態檔裡的 unit 名只認白名單（`:139-141`、`:719-722`）；`--inner` 要 `INVOCATION_ID`；transient 的參數只有驗過的 `tag`（`TAG_RE`）、`revert-after`（正規式＋下限）、操作者給的 `old-home`（argv 不經 shell），環境只轉送三個 `LLMGW_*`；`keys.pass` 先 `root:root 0640` 再 chown 成 `root:llmgw`（寫入時不會短暫 llmgw 可讀；`install._step_config`、`migrate._step_keys_pass`）；版本目錄 `normalize_tree` 為 root:root 0755／0644、移除 `.git`、不跟 symlink（`release.py:101-129`）；隧道 unit／服務 unit 的 `User=llmgw`、`NoNewPrivileges`、`ProtectSystem=strict`、`ReadWritePaths` 合規。
- 問題：高-1、低-2、低-4。
- `keys.pass` 是 `root:llmgw 0640`，服務帳號本來就讀得到（`LoadCredential` 只是不進環境／argv，不是對 llmgw 隱藏）；這與舊版（llmgw 自有 0600）相比沒有退步，符合 spec，列為資訊。`keys.pass` 的值若含 shell 特殊字元，`sync` 的 `. keys.pass`（以 llmgw 身分，無提權）會誤解析；文件寫的是 32 位英數，不構成問題。

**3. 一致性**
- `llm.db`：prepare 與回滾都走 SQLite backup＋`integrity_check`＋重試＋inode 驗證（`:449-542`），寫入中的舊服務不會得到撕裂影像；回滾前先把舊 db 一致快照成 `.bak-*`（保留 3 份）。回寫只在新 db mtime 晚於 `new_service_ts` 時發生（新服務啟動時的 `CREATE TABLE IF NOT EXISTS` 不改檔，所以不會誤觸）。缺口在中-1／中-2。
- `keys.env`：回滾只在「新的較新且格式／鍵名／不重複驗得過」才覆蓋舊的（`:1949-1972`）；prepare 不覆蓋已存在的（`:1005`；`--force` 見中-1）；新服務啟動後 30 秒內 key 拉取會刷新。`.keysync.sha` 隨 prepare 複製，不會造成錯誤覆蓋（最差是再拉一次）。冷卻資料：除中-2 外沒有丟失路徑。

**4. docs/deploy.md ↔ CLI**：見低-1（沒有寫了不存在的子指令；錯的是路徑、`status` 內容、停損描述與缺漏的前置條件／旗標）。

**5. 新程式碼**：除低-3 外，`_Lock`（fd 在 `acquire` 失敗路徑都關）、`write_state`（原子寫）、`KeySyncTimer`（鎖內 spawn／登記、SIGTERM 清理、stderr 4KB 尾巴、`killpg` 護欄）沒有看到明顯錯誤。lgwd 的 keysync 失敗不影響 API（獨立執行緒、例外全吞）。

### 資訊-2 真機首次實際走過 gh（演練全是假 gh）

fh-l／Lima 演練用假 `gh`（`rehearsal-35-findings.md` 第 3 點），所以 `gh` 在 system unit（`User=llmgw`＋`ProtectSystem=strict`，HOME 唯讀）下的行為（token 存放位置、狀態檔寫入）第一次真的跑是在切換的 `(e)`。安全網存在：lgwd 30 秒內第一次拉取失敗 → `.keysync.status` `ok:false` → `check`≠0 → 停損自動回舊版，代價是一次約數分鐘的中斷。建議切換前在真機先以 llmgw 身分跑 `gh api repos/FATESAIKOU/LLMGateway-keys --jq .full_name` 確認 token 在 `~llmgw/.config/gh/hosts.yml`（不是 keyring）；`preflight` 目前只跑 `gh auth status`。

### 資訊-3 其他

- `deploy/units/llmgateway-tunnel.service` 沒有加固（`ProtectSystem`／`NoNewPrivileges`）；spec 只要求服務 unit 加固，故不算缺陷。
- `release.fetch`、`upgrade._dry_run_policy` 以 root 執行 git tag 的內容（policy 載入器）——這是部署流程固有的信任（tag 來源 repo），與 repo 權限管理一體。
- 前幾輪報告（`review-35-t1/t2t3/t4.md`）在這個 worktree 的 `docs/research/` 下不存在（它們在各自的 review worktree）；我對照的是 `fix3-status.md` 的對帳表與程式。對帳表列的項目我抽查了程式與測試（中-2 保險 timer 檢查、高-1 回滾順序、中-4 歸檔／守門、低-13 confirm），與表上描述一致。

---

結論：**需修**（高 1、中 4、低 5、資訊 3）——高-1（root 對 llmgw 可控路徑 chmod／chown 跟 symlink，可提權）修掉再切；中-3（preflight 加舊 unit 檔檢查）與中-4（回滾隧道把關）會讓正式服務失去回復路徑，建議同批修；中-1、中-2 是資料遺失風險，修不及時至少照資訊-1 的操作緩解。
