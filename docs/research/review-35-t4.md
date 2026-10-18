# 審查 #35 T4：遷移子指令 lgwctl/migrate.py

**被審對象**：分支 `impl/migrate` 的三個 commit — `699421f`（分派接上與 `units.raw`）、`aeae5c7`（migrate.py 主體）、`76ac507`（測試）。範圍：`lgwctl/migrate.py`（1126 行，新）、`tests/test_lgwctl_migrate.py`（1079 行，新）、`lgwctl/cli.py` 的分派接線。

**審查分支**：`review/migrate`（worktree `/Users/fatesaikou/testAI/LLMGateway-worktrees/review-migrate`，內容同被審分支）。

**審查模型**：Space Bunny Free（opencode / opencode）。

**方法**：逐段讀 `migrate.py`、測試、T1 的 `layout/install/release/units/probe/run/check`，對照 `spec-35.md`、T4 任務書（`.work/prompts/migrate-35.md`）、`install-simplify-design.md` §5／§7、`deploy/install.sh`（舊 user unit 的真實樣貌）、`lgwd/store.py`。另外用沙箱（`tests/lgwctl_fakes.py` 的 `Sandbox`）實跑幾個情境做驗證（下面標「實測」），並用真的 `git clone` 驗證 `release.fetch` 的目錄權限。**沒有改被審程式碼、沒有 push、沒有碰真機、沒有讀憑證檔。**

> 本檔取代同一 worktree 早前 session 產出的同名報告（commit `4a6dd6d`）；結論與分級重新獨立做過一遍，並補上四項先前沒有的驗證（`git clone` 權限實測、斷 symlink traceback、symlink 來源跟隨、狀態檔注入）。

**驗證紀錄**

| 項目 | 結果 |
|---|---|
| `python3 -m unittest discover -s tests` | `Ran 412 tests in 165.188s` → `OK`（Python 3.12.3；數量與任務書預期一致） |
| 版本目錄權限 | 真的 `git clone --depth 1 --branch …` 進 `mkdtemp`（0700）建的目錄 → 實測 `drwx------`（見高-1） |
| 狀態檔注入 | 實測 `revert_unit: "sshd.service"` → `order.log` 出現 `systemctl stop sshd.service`（見高-3） |
| 斷掉的 symlink | 實測 `FileNotFoundError` traceback（見低-1） |
| symlink 來源 | 實測舊 `keys.env` 是 symlink 時 prepare 把目標檔內容複製成新的 `keys.env`（見中-5） |

**等級數量**：高 4、中 9、低 10、資訊 4。

---

## 高（4）

### 高-1 `/opt/llmgateway/<tag>` 會是 0700 root-only，服務帳號進不去 → 真機切換必定失敗；而測試用的假 git 剛好把這個洞掩蓋掉

- **檔案:行**：`lgwctl/migrate.py:446`（`prepare` 走 `release.fetch`）、`lgwctl/release.py:99`（`tempfile.mkdtemp` → 0700）、`lgwctl/release.py:69-87`（`_move_into_place` 只 `rename`，不改 mode）、`lgwctl/install.py:133-149`（`_step_dirs` 只校正 `state_dir` 與 `.ssh` 的權限）、`deploy/units/llmgateway.service:14`（`WorkingDirectory=/opt/llmgateway/current`、`User=llmgw`）、`tests/lgwctl_fakes.py:84`（假 git：`cp -a "${REAL_TEMPLATE:?}/." "$dest/"`）。
- **問題**：`fetch` 用 `mkdtemp` 產生 0700 的目錄當版本目錄，`git clone` 進**已存在**的目錄不會改它的 mode，`rename` 也保留 mode。實測：`mkdir -m 700 dest; git clone --depth 1 file://… dest` → `drwx------`。所以真機上 `prepare-migration` 產出的 `/opt/llmgateway/<tag>` 是 `drwx------ root root`，`User=llmgw` 的 `llmgateway.service` 連 `chdir` 都做不到（`status=200/CHDIR`），`lgwd` 的 policy／ops-scripts 也讀不到。`cutover` 會一路走到 (c) 啟新 unit，然後 (d) 等不到本機 200 → 停損 → 回舊版，也就是「每次切換都失敗」，而且 journal 只會有一行 CHDIR，看起來像服務本身的問題。
  - **測試為什麼沒抓到（這裡就是「測了寄居蟹」）**：假 git 用 `cp -a template/. dest/`，`cp -a` 會把來源目錄的權限套到 dest，所以沙箱裡的版本目錄一律是 0755（實測 `prepare` 後 `tag dir mode = 0o755`）。假的 git 反而修好了真的 bug，於是 412 個測試全過而真機必壞。
- **情境**：真機切換窗口。`prepare` 印一堆 `ok`、`check` 的 policy 項也過（那是 root 讀的），切換時才爆。
- **建議**：(1) 修在共用那一邊——`release.fetch`（或 `install._step_dirs`）在 `_move_into_place` 之後把版本目錄權限正規化為 0755 root，並在 T1 的整合分支一起帶進來；`prepare` 不該各自修一份。(2) `prepare` 在 `fetch` 之後自己再驗一次「服務帳號能不能進去」（`os.stat(tag_dir).st_mode & 0o005` 或以 `runuser -u llmgw -- test -x` 試），不符就 `fail` —— 讓它在**不影響舊服務**的階段就停住，而不是切換時才停損。(3) 補測試：讓假 git 不要改 dest 的權限（`cp -R` 而非 `cp -a`，或事後 `chmod 700 "$dest"`），再加一條斷言版本目錄 mode 的測試。

### 高-2 `cutover` 只 `stop` 舊 user unit、不 `disable`：觀察期內任何一次重開機都會新舊雙跑，而且反向保險會跟著消失

- **檔案:行**：`lgwctl/migrate.py:88-92`（`OLD_STOP_ORDER`／`OLD_START_ORDER`）、`lgwctl/migrate.py:701-717`（(b) 只呼叫 `_stop_unit`）、`lgwctl/migrate.py:877-890`（`_stop_new_units` 有 `disable`，舊的沒有）、`deploy/install.sh:530,535,550`（真機上舊的三個 user unit 都是 `systemctl --user enable` 過的）。
- **問題**：停掉之後 enable 狀態還在。新的 system unit 在 (c) 被 `enable`；於是觀察期（第 4 天、甚至切換當天）任何一次重開機都會同時啟動舊 user 服務與新 system 服務：兩個 `lgwd` 搶 `127.0.0.1:8480`、兩條隧道搶會合點的 4710／4711、兩個 `llm.db`（新舊各自的 state）分岔寫入。新的會 crash-loop 進 `failed`，但 `llmgateway.service` 與舊 user unit 哪個在服務 requests 端看是一樣的（舊版會回應），所以「看起來還活著」。
  - 附帶：`lgw-cutover` 與 `lgw-revert` 都是 transient unit，**重開機就沒了**。切換窗口內重開機等於停損消失，且舊服務也因為 linger 的 user manager 沒起來而不確定還在不在。
- **情境**：`confirm` 成功、第 2 天本人更新開機 → 舊版繼續服務，新版失敗迴圈；`llmgwctl check` 只看新的兩個 unit（`fail`），但實際提供服務的是舊的。
- **建議**：(1) (b) 停完舊 unit 後順便 `disable` 三個舊 user unit（`systemctl --user disable` 對已 stop 的 unit 有效；`rollback-to-user` 之後 `start` 對 disabled 的 unit 一樣有效，所以回滾路徑不用改）。(2) 保險機制不要只靠 transient timer：把 `pending-confirm` 的狀態當成開機後的待辦（例如在新的 `llmgateway.service` 加 `ExecStartPost`／或一個小的開機檢查），開機時若看到 `pending-confirm` 就自己觸發回舊版；至少在切換當天把「不要重開機」寫進 #35 的操作步驟。(3) `finalize` 在舊 unit 仍 enable 時不要寫 `finalized`，報 fail 並提示。(4) 這是設計 §5 只寫「停」但真機上必須決定的點，建議回 PMO 補一條決定。

### 高-3 `.cutover.state`／`.cutover.lock` 放在服務帳號可寫的目錄，而 `revert_unit`／`phase`／`confirmed_ts` 被當成 root 的輸入

- **檔案:行**：`lgwctl/migrate.py:291-296`（狀態檔與 lock 都在 `<state>` 底下）、`lgwctl/migrate.py:309-317`（`write_state` 寫 0644，owner 是建立它的 root）、`lgwctl/install.py:141-143`（`/var/lib/llmgateway` 是 `llmgw:llmgw 0700`）、`lgwctl/migrate.py:816-819`（`confirm` 讀 `state["revert_unit"]` 並交給 `_cancel_revert`）、`lgwctl/migrate.py:793-800`（`systemctl stop <unit>`）、`lgwctl/migrate.py:873-874`（`_state_revert_unit`）、`lgwctl/migrate.py:320-346`（`_Lock`）、`lgwctl/migrate.py:975-989`（`finalize` 信任 `phase` 與 `confirmed_ts`）。
- **問題**：目錄是 `llmgw` 擁有且 0700，所以服務帳號可以 unlink 並重寫 `.cutover.state`，也可以刪掉 `.cutover.lock` 重新造一個（`flock` 對已被刪的 inode 仍有效，但新檔案是新的鎖 → 互斥失效）。而這些欄位被 root 的指令直接使用：
  - `{"phase":"pending-confirm","revert_unit":"sshd.service"}` → **實測**：操作者一跑 `llmgwctl confirm`，`order.log` 出現 `systemctl stop sshd.service`；值也可以寫成 `--host=…` 之類的**選項注入**。
  - `{"phase":"confirmed","confirmed_ts":<10 天前>}` → **實測**：`finalize-migration --yes` 直接過 3 天條件、把舊目錄整個搬走（從此回不了舊版）。
- **情境**：gateway 對外可達，服務帳號被拿下（lgwd 的 RCE、key-sync 被誘導……）就足以換成「root 可停任意 unit」與「root 幫你收掉舊版」。目前沒有已知 trigger，但這個邊界開口只要幾行就能關上，且這是在真機上跑一次的程式。
- **建議**：(1) **不要**讀檔裡的 unit 名：`revert_unit` 改用常數 `REVERT_TIMER_UNIT`（或白名單 `^lgw-revert(\.timer|\.service)?$`，不符就 fail）。(2) 狀態檔與 lock 移到 root 專屬位置（例如 `<state>/.root/`，`0700 root:root`，只有 root 寫得到）。(3) 測試：狀態檔塞 `revert_unit: "sshd.service"` 時 `confirm` 不得呼叫 systemctl；塞 `phase: "confirmed"` 但 `confirmed_ts` 很新時 `finalize` 必須拒絕。

### 高-4 `prepare` 在舊服務還在寫的時候用 `copy2` 複製 `llm.db`（rollback journal 模式），可能複製到不一致的資料庫

- **檔案:行**：`lgwctl/migrate.py:518-532`（`_step_db`）、`lgwctl/migrate.py:217-234`（`_copy_into_place` → `shutil.copy2`，無任何一致性處理）、`lgwctl/migrate.py:470`（「舊 user unit 與舊目錄都沒動」＝複製時舊服務一定在跑）、`lgwd/store.py:36-38`（`sqlite3.connect` 預設 journal 模式，**不是 WAL**）、`lgwd/store.py:1-4`（每次 `GET /v1/subscriptions` 都寫 audit 列）。
- **問題**：rollback journal 模式下，寫入交易期間主檔是**就地修改**的，原始頁放在 `-journal`。此時 `copy2` 主檔會拿到「新舊頁混合」的檔案，而且 `-journal` 不會被一起複製 → 得到一份可能不一致的 db。`prepare` 的定義就是「不影響舊服務」，所以這個複製一定發生在舊 `lgwd` 還在服務請求的狀態；而 audit 是每次 list 都寫，寫入並不稀少。這份副本之後會被新服務當資料庫開啟（可能 `database disk image is malformed` → 新服務起不來 → 停損），或被 `rollback-to-user` 複製回舊 state。
  - 任一方向都不是「無害」：真壞掉時唯一能救的是 `llm.db.bak`，而那要人去發現並手動還原。
  - 註：設計 §5／任務書寫的是「用 `cp -a`」，所以這是**規格本身的缺口**，不是實作者違規——但要在真機上切正式服務就該在這裡停一下並回報。
- **情境**：`prepare` 剛好落在一次 `GET /v1/subscriptions` 的交易中間 → `/var/lib/llmgateway/llm.db` 是不一致的；`cutover` 啟動新服務失敗 → 停損回舊版（此時舊 db 被覆蓋成本來那一份，見中-6）→ 服務中斷＋要人工修 db。
- **建議**：(1) 改用 SQLite 的線上備份 API（標準函式庫就夠：`sqlite3.connect(src).backup(sqlite3.connect(dst))`），對活著的寫入是安全的；或在 (b) 停掉舊服務之後才複製（但那就不是「準備」階段了）。(2) 至少要驗：複製完對新檔跑 `PRAGMA integrity_check`，不是 `ok` 就重試幾次、再不行就 `fail` 並叫本人處理，不要留一份壞的當成功。(3) 測試補一條：用真的 sqlite 檔、保持一個寫入交易開著，跑 prepare 後斷言 `integrity_check` 通過（現在的假檔案 `old-db\n` 完全測不到這件事）。

---

## 中（9）

### 中-1 分離執行的 transient unit 不會拿到呼叫端的環境變數（`LLMGW_PREFIX` 等），演練／沙箱會打到真實路徑

- **檔案:行**：`lgwctl/migrate.py:612-625`（`cutover_unit_argv`）、`lgwctl/migrate.py:628-637`（`insurance_unit_argv`）、`lgwctl/migrate.py:662`（`runmod.run(argv, env=layout.child_env())`）。
- **問題**：`systemd-run` 只把**服務管理器的預設環境**交給 transient unit，呼叫端的環境不會轉送（要 `--setenv=`／`--property=Environment=` 才會）。所以外層那次 `cutover` 是在 `LLMGW_PREFIX=/tmp/xxx` 下跑的沒錯，但真正做事的 `lgw-cutover` 與保險的 `lgw-revert` 會用**預設（真實）路徑** `/opt/llmgateway`、`/etc/llmgateway`、`/var/lib/llmgateway`。
- **情境**：§7 指定的演練（Lima 的 Ubuntu VM，假 key、假會合點）。若演練時用 `LLMGW_PREFIX` 隔離（測試就是這樣隔離的），人手下的那次切換看起來成功（`ok cutover 已排入 lgw-cutover`），實際上 detached 單元在動 VM 的真路徑 → 演練等於真的切了 VM，或在權限不足時以一種沒人預期的方式失敗。
- **建議**：(1) `cutover_unit_argv`／`insurance_unit_argv` 加上 `--setenv=LLMGW_PREFIX=…`（以及 `LLMGW_SERVICE_USER`／`LLMGW_SERVICE_HOME` 等少數真機上會用到的），或 (2) `--inner` 開頭檢查一次：有 `LLMGW_PREFIX` 卻在真路徑上跑就明確 fail 並說明「transient unit 沒拿到環境變數」。(3) 演練步驟寫死：**cutover 一定在沒有 `LLMGW_*` 環境變數的乾淨 shell 裡跑**。

### 中-2 `_UNIT_MISSING_MARKERS` 太寬：user bus 連不上、指令找不到都會被當成「本來就沒在跑」

- **檔案:行**：`lgwctl/migrate.py:117`（`("not loaded", "not found", "No such file", "could not be found")`）、`lgwctl/migrate.py:352-359`（`_judge`）、`lgwctl/migrate.py:420-426`（`_stop_unit`）、`lgwctl/migrate.py:701-717`（(b) 的分支）、`lgwctl/migrate.py:371-384`（`runuser … env … systemctl --user`）。
- **問題**：`systemctl --user` 連不到 user bus 時印的是 `Failed to connect to bus: Failed to connect to socket /run/user/<uid>/bus: No such file or directory`（含 `No such file`）；`runuser`/`env` 之下 PATH 沒有 systemctl 時是 `env: 'systemctl': No such file or directory` 或 `command not found`（含 `not found`）。兩者都被判成 **missing → skip**，也就是「本來就沒在跑」，(b) 就這樣過去了。
- **情境**：任務書 §2(b) 點名的 linger 情境。若 `/run/user/<uid>` 不存在（linger 沒開、user manager 沒起來、或這台機器的舊服務是靠某個 session 撐著），舊 unit 確實沒在跑、skip 恰好無害；但同一個判斷也把「指令找不到」「bus 存在但 unit 沒載入」等非預期狀況吞成 skip，然後照樣往下切 —— 正是「保險還在、但舊服務其實還在跑」的組合。
- **建議**：missing 收窄成明確的 unit 不存在字串（`Unit <name> not loaded`、`Loaded not-found`、`does not exist`），`Failed to connect to bus` 與 `command not found` 一律算 **failed**（在 (b) 就 fail → `_restore_old_units` + 取消保險 + `aborted`，這是安全的一邊）。另外 (b) 停完之後用 `systemctl --user is-active` 逐一確認真的不跑了再進 (c)。測試補一條：假 systemctl 回 1 並印 `Failed to connect to bus: … No such file or directory` 時 (b) 必須 fail 而非 skip。

### 中-3 `inner` cutover 不參與 flock：保險提早到、或切換中人工回滾，兩邊會同時動 unit 並互相覆寫狀態

- **檔案:行**：`lgwctl/migrate.py:674-761`（`_cutover_inner` 全程沒拿 `_Lock`）、`lgwctl/migrate.py:839-846`（`rollback_to_user` 拿不到鎖就 `ok` 跳過）、`lgwctl/migrate.py:120`（`_REVERT_AFTER_RE` 允許 `0`／`30s`）、`lgwctl/migrate.py:764-768`（`_stop_loss` 直接呼叫 `rollback_to_user`）、`lgwctl/migrate.py:309-317`（`write_state` 是 read-merge-write，無鎖）。
- **問題**：鎖只保護 `rollback-to-user` 彼此（測試 `test_rollback_skips_when_insurance_holds_the_lock` 有覆蓋）。於是：
  - 保險在切換還沒跑完就到期（`--revert-after` 沒有下限，regex 允許 `0`、`30s`；而切換最壞要 60 秒本機 + 90 秒 keysync + 一次遠端探針）：保險把新 unit 停掉、舊 unit 起了來，inner 接著又把新 unit enable+start，最後還把狀態寫成 `pending-confirm` —— 實際上是舊版在跑，狀態卻說「等 confirm」。
  - 切換中操作者手動跑 `rollback-to-user`：同上，而且 rollback 寫的 `reverted` 會被 inner 尾端寫的 `pending-confirm` 蓋掉。
  - 順帶：拿不到鎖時 `rollback_to_user` 回 0 並印「另一個回舊版正在跑」，在「保險與切換同時發生」的情境裡等於**保險被吃掉卻沒有真的回滾**。
- **情境**：真機切換窗口（人會慌、會按保險、也會手動 rollback）。時間軸：切換 10:00 開始、保險 10:10 到期，inner 因假停損拖到 10:11。
- **建議**：`inner` 也拿同一把鎖（或在每個不可逆步驟前確認 phase 仍是 `cutover-started`，看到 `reverted` 就中止並報「已被回滾」）；鎖拿不到不要回 0 了事，改成短暫重試；`--revert-after` 給一個下限（至少大於切換最壞時間，或在使用者給太小值時 `todo` 提醒）。測試補兩條：切換中狀態變 `reverted` 時 inner 中止；保險與 inner 同時跑時最後狀態必須是 `reverted`。

### 中-4 啟動隧道後立刻單次探經會合點 → 大概率假停損

- **檔案:行**：`lgwctl/migrate.py:726-728`（start tunnel）、`lgwctl/migrate.py:738-744`（緊接著 `probe.remote`）、`lgwctl/probe.py:58-81`（單次探測，rc 1＝非 200）。
- **問題**：本機 list 通常幾秒內就 200，所以 (d) 的遠端探針很可能在「start 之後一兩秒」就發出。此時新隧道的 ssh 可能還沒連上會合點（連線、認證、reverse forward 建立），探針拿到 connection refused → rc 1 → 立刻 `_stop_loss`。另外舊隧道剛停掉時，會合點上的 4710／4711 也可能還沒釋放，新隧道第一次連線就因 `ExitOnForwardFailure` 退出，要等 `RestartSec` 才重試 —— 一樣會被這次單次探針判死。
- **情境**：真機切換窗口；結果是「每次切換都在遠端探針失敗 → 回舊版」，但 journal 裡新隧道 10 秒後就起來了。這會讓 §5 的切換根本做不成，而且每次都浪費一次停損。
- **建議**：啟動隧道後先等 unit active（`units.unit_state` 輪詢）再探；遠端探針改成有間隔的重試（例如 3 次、間隔 10 秒、總 30 秒內），全部失敗才算停損，並在輸出行寫「探了幾次、最後一次結果」。這是設計 §5「經會合點非 200 → 回舊版」的合理落地細節，值得明確寫成重試而不是單次。

### 中-5 從舊 HOME 複製檔案會跟 symlink（來源在服務帳號控制下）→ 被入侵的 llmgw 能讓 root 把任意 root 可讀檔變成 llmgw 可讀

- **檔案:行**：`lgwctl/migrate.py:217-234`（`_copy_into_place` 用 `shutil.copy2`，預設 `follow_symlinks=True`）、`lgwctl/migrate.py:498,523,540`（先 `os.path.isfile` 再複製，跟著 symlink 走）、`lgwctl/migrate.py:566`（`parse_env_file(old.keysync_env_file)` 整檔讀進記憶體）。
- **問題**：舊佈局在 `~llmgw/.config/…`／`~/.local/state/…`，全在服務帳號控制下，而 `prepare` 以 root 執行。**實測**：把舊 `keys.env` 做成指向外部檔的 symlink 後跑 `prepare`，新的 `/var/lib/llmgateway/keys.env`（0600、owner llmgw）內容變成那個外部檔的內容 —— 等於把任意 root 可讀檔交給服務帳號。同樣的手法可以用在 `llm.db`（→ llmgw 可讀的 db）、`tunnel.env`（→ `/etc/llmgateway/tunnel.env`，而且 migrate 還會把它 chown 給 llmgw，見中-7）。
- **情境**：`llmgw` 被拿下之後，等本人跑 `prepare-migration` 就完成提權。觸發條件不是本流程產生的，而是「服務帳號不可信」這個既有邊界。
- **建議**：複製前先 `os.lstat` 判斷來源是不是 symlink，是就 `fail`／`todo` 叫本人確認，不要跟；或用 `os.open(src, os.O_RDONLY | os.O_NOFOLLOW)` 讀 fd 再寫目標。`keysync.env` 改成逐行讀並限制行數／大小（低-2）。測試補一條：舊 `keys.env` 是 symlink 時 prepare 必須拒絕。

### 中-6 回舊版時 `llm.db` 會被「prepare 時的舊快照」覆蓋掉（可能比舊 state 的更舊），而且 `.bak` 只留第一份

- **檔案:行**：`lgwctl/migrate.py:893-912`（`_restore_db`：`if os.path.isfile(dst) and not _exists(bak)` → 只備份一次，之後直接覆蓋）、`lgwctl/migrate.py:518-532`（prepare 時的複製）、`lgwctl/migrate.py:859`（rollback 呼叫 `_restore_db`）。
- **問題**：`rollback-to-user` 無條件把新 state 的 `llm.db` 覆蓋回舊 state，沒有比較新舊。切換失敗時（正是最需要 rollback 的情境）新服務可能根本沒寫過東西 → 複製回去的是 **prepare 時刻的快照**，而舊服務在 prepare 之後到 cutover 之間還寫過 audit／冷卻（每次 list 都寫）。於是回舊版時舊服務是在一份**比它當時的內容更舊**的 db 上起來。資料本身還在（`.bak` 存的是 rollback 當下的舊 db），但要人去發現並手動還原。
  - `.bak` 只留第一份：修好後再切一次、又回滾時，中間版本沒有留下。
- **情境**：`prepare` 星期一跑，星期三切換失敗 → 回舊版；舊 db 被換成星期一的快照；星期三下午有人清掉 `llm.db.bak`（或再切再回滾），星期一到三的 audit／冷卻資料就沒了。
- **建議**：只在新的**確實比較新**（mtime 比較，或更好：在狀態檔記 `new-service-started`／第一次寫入時間）才覆蓋，否則 `skip` 並明說「新的沒有比較新，不覆蓋」——與 `keys.env` 的規則一致（`migrate.py:915-938` 那條就是對的）；備份改成帶時間戳、保留最近 N 份（`llm.db.bak-YYYYmmddHHMMSS`），並在輸出行指出該看哪一份。

### 中-7 `keys.pass` 的擁有者規則與 `install` 相反；`gid` 取不到時靜默變 `root:root`；`/etc` 的設定檔被 chown 給服務帳號；而 prefix 模式下 chown 全被跳過，測試等於沒驗 owner

- **檔案:行**：`lgwctl/migrate.py:277-285`（`_chown_root_group`，`except KeyError: gid = 0`）、`lgwctl/migrate.py:561-586`（`_step_keys_pass`）、`lgwctl/migrate.py:550-558`（`_step_env_file` 把 `/etc/llmgateway/*.env` chown 成 llmgw）、`lgwctl/install.py:92-97`＋`176-184`（T1 的 `_chown` 把 `keys.pass` chown 成 `llmgw:llmgw`、mode 0640）、`tests/test_lgwctl_migrate.py:395`（只斷言 mode 0640）。
- **問題**：(a) `migrate` 很仔細地照規格做 `root:llmgw 0640`（好），但 T1 的 `install`／`_step_config` 另一條路會把它 chown 成 `llmgw:llmgw` —— 真機流程只要有人跑一次 `install --tag`，剛建立好的權限就被改掉，解密密碼變成服務帳號可寫。兩支工具對同一個檔的規則相反。(b) `pwd.getpwnam()` 失敗時 `gid = 0`，安靜地產生 `root:root 0640`，輸出照樣 `ok keys-pass`，而 `llmgwctl sync` 是以服務帳號讀這個檔，會失敗。(c) `/etc/llmgateway/tunnel.env`、`llmgateway.env` 被 chown 成 `llmgw`（`tunnel.env` 沿用舊的 0600，服務帳號因此可改 `RENDEZVOUS`；`/etc` 下的設定檔按規格隱含應是 root 擁有、必要時 `root:llmgw 0640`）。(d) `_chown_root_group`／`_chown` 在 `layout.prefix` 有值時直接 return，所以**現有測試完全沒有驗過任何 owner**。
- **情境**：整合後真機上 `prepare-migration` → 之後有人跑 `install --tag`（或將來別的工具）；或 `LLMGW_SERVICE_USER` 寫錯時 keys.pass 變 `root:root`，人工填值與 `llmgwctl sync` 流程失敗而 prepare 只印 `ok`。
- **建議**：把「檔案要什麼 uid/gid/mode」收成 `layout` 裡的一個共用函式（例如 `layout.chown_secret(path)`／`layout.chown_service(path)`），`install` 與 `migrate` 都呼叫它（順便解掉 T1 的同一個問題）；`_chown_root_group` 拿不到 gid 時要 `fail` 而不是 `gid = 0`；`prepare` 的 `keys-pass` 行印出實際擁有者前綴；在 root 環境（無 prefix，或測試開一個「真的 chown 到 sandbox uid」的開關）補一條斷言 uid/gid 的測試。

### 中-8 `cutover` 只有一道 pre-flight（比對 current 的 tag），缺前置條件要切下去才發現 → 白白停一次舊服務

- **檔案:行**：`lgwctl/migrate.py:656-658`（outer 只檢查 `layout.installed_tag() != tag`）、`lgwctl/migrate.py:677-679`（inner 重複同一個檢查）、`lgwctl/migrate.py:701-717`（(b) 停舊服務在所有驗證之前）。
- **問題**：`prepare-migration` 的很多項目是 `todo` 而不是 `fail`（沒有舊 `keysync.env`、`tunnel.env` 沒填、gh 沒登入……），`cutover` 完全不檢查就往下走：先停舊服務，再由新服務起不來 → 停損 → 回舊版。功能上是安全的（fail-safe），但每次缺前置都會白白製造一次服務中斷與一次無謂的回滾，而且「回舊版」本身也有風險（中-6）。
- **情境**：`prepare` 印了 `todo keys-pass（要本人填進 /etc/llmgateway/keys.pass）`，本人忘了填就照排切換窗口 → (b) 停掉舊服務 → 新服務因為 `LoadCredential` 來源沒有值而失敗 → 停損回舊版。整套流程白跑一趟。
- **建議**：outer 在排 transient unit **之前**做完整的 pre-flight 並在有問題時**拒絕排程**（連線都還在、舊服務還在跑，正是最該拒絕的時候）：`keys.pass` 有非空值、`tunnel.env` 就緒（`layout.tunnel_config_state`）、`keys.env`／`llm.db` 在位、版本目錄服務帳號可進（高-1）、`.lgw-tag` 相符。把 pre-flight 抽成一個函式，outer 與 inner 共用（inner 保留同一組檢查，因為 detached 之間可能有人改東西）。

### 中-9 `confirm` 只停 timer，不看保險是不是已經開跑，也不確認此刻在跑的是新版

- **檔案:行**：`lgwctl/migrate.py:806-827`（只 `_cancel_revert` + 寫 `confirmed`）、`lgwctl/migrate.py:793-800`（`_cancel_revert` 只 `systemctl stop lgw-revert.timer`）、`lgwctl/migrate.py:309-317`（`write_state` 是 read-merge-write，無鎖、無 compare-and-swap）。
- **問題**：`systemd-run --on-active` 會產生 `lgw-revert.timer` 與（到點後）`lgw-revert.service`。`confirm` 只停 timer：
  - 若保險已經到點、`lgw-revert.service` 正在跑 rollback，`stop lgw-revert.timer` 會回「not loaded / 沒東西」→ 被當作「當作已取消」→ `confirm` 回 0，並把 phase 寫成 `confirmed`，**蓋掉 rollback 正在寫的 `reverted`**。結果：實際跑的是舊版，狀態說「觀察期開始」。
  - 停掉 timer 也不會停掉已經觸發的 service。
- **情境**：切換窗口拖到保險快到期（人還在跑 `check`／等結果），保險在 T+10:00 開跑，人在 T+10:05 看到 `check` 差不多過了就 `confirm`。之後 `finalize-migration` 三天後會把舊目錄搬走，而 `rollback-to-user` 會被 `finalize` 的停損條件擋住。
- **建議**：`confirm` 在停 timer 之前先 `systemctl is-active lgw-revert.service`（或 `is-failed`），若保險已在跑就**拒絕**並提示「保險已經在回舊版，先等它跑完」；另外在寫 `confirmed` 之前確認此刻 `llmgateway.service` 是 active、舊的 user unit 不是 active（本機 list 200 也可），不符就 fail。`write_state` 也加鎖（或寫前確認 phase 仍是自己預期的那個）。

---

## 低（10）

### 低-1 目標是斷掉的 symlink 時 `prepare` 會 traceback

- **檔案:行**：`lgwctl/migrate.py:495`（`if _exists(dst) and not force and os.path.getsize(dst) > 0:`）、`lgwctl/migrate.py:213-214`（`_exists` 用 `lexists`）。
- **問題**：`_exists` 用 `lexists`（對斷掉的 symlink 回 True），接著 `getsize` 丟 `FileNotFoundError`，整段沒有 `try`。**實測**：`/var/lib/llmgateway/keys.env` 是指向不存在路徑的 symlink 時，`prepare-migration` 印完整 traceback（違反 T1「壞環境不 traceback」的既定修法）。
- **情境**：`keys.env` 是人工誤建的 symlink，或某次搬移／回滾留下斷鏈。
- **建議**：改用 `os.path.isfile(dst) and os.path.getsize(dst) > 0`，或整段包 `try/except OSError` 記 fail；補一條測試（`keys.env` 指向不存在的路徑）。

### 低-2 `parse_env_file` 對舊 `keysync.env` 是整檔讀進記憶體

- **檔案:行**：`lgwctl/migrate.py:566`（`parse_env_file(old.keysync_env_file)`）、`lgwctl/layout.py:96-116`（`fh.read().splitlines()`，沒有大小上限）。
- **問題**：`keysync.env` 在 llmgw 控制下；指向 `/dev/zero`、`/proc/…` 或超大檔時，root 行程會卡住或吃滿記憶體（DoS，不是外洩）。`parse_env_file` 是 T1 的共用函式，`tunnel.env`／`llmgateway.env` 也走同一條。
- **情境**：`keysync.env` 被換成指向 `/dev/zero` 的 symlink → `prepare-migration` 掛住，操作者以為機器卡死。
- **建議**：`parse_env_file` 改成逐行讀並限制總行數／位元組（例如讀滿 64 KiB 就停、逾限視為壞檔回空字典），或至少在 migrate 這一路先 `os.path.getsize` 擋掉。

### 低-3 `finalize` 沒有把 `old-home` 綁在狀態檔，也沒有先確認新版健康就開始搬

- **檔案:行**：`lgwctl/migrate.py:693`（`write_state` 只存 phase／tag／revert_unit）、`lgwctl/migrate.py:991`（`OldLayout(old_home or layout.service_home)`）、`lgwctl/migrate.py:994-1031`（停舊 → 搬檔 → disable-linger → 寫 finalized）。
- **問題**：收尾的目標目錄來自命令列的 `--old-home`，和切換當時用的值沒有綁定；打錯就搬錯的目錄（或什麼都沒搬、印一堆 skip 卻仍寫 `finalized`）。整個收尾也沒有跑一次 `check`，新版此刻壞掉照樣把舊的收走。
- **情境**：`finalize-migration --old-home /home/llmgwe --yes`（打錯一個字母）→ 舊目錄沒被搬、狀態寫成 finalized、輸出看起來成功，之後再也回不了舊版。
- **建議**：`prepare`／`cutover` 時把正規化後的 `old_home` 寫進狀態檔，`finalize` 沒給就用狀態檔的值、給了但不一致就 fail；`finalize` 開頭先跑一次 `check`（或至少本機 list 200），非 0 就拒絕並提示 `rollback-to-user`。

### 低-4 `finalize` 只搬四個已知的舊 unit 檔；跨檔案系統搬移時 owner 變 root

- **檔案:行**：`lgwctl/migrate.py:1012-1015`（只搬 `OLD_UNIT_NAMES`）、`lgwctl/migrate.py:1020-1024`（user manager 沒活著時 `daemon-reload` 只 todo）、`lgwctl/migrate.py:1035-1050`（`_move_step` 用 `shutil.move`）。
- **問題**：(a) `~/.config/systemd/user/` 裡若有其他檔（自訂 unit、`*.wants` 之外的手寫設定）會留在原位，收尾不乾淨；(b) `shutil.move` 跨檔案系統時退化成 `copytree`＋`rmtree`，`copy2` **不保留 owner**，搬過去的檔案變 root:root（`/home` 獨立磁碟的機器）；(c) user manager 不可用時 `daemon-reload` 不生效，但檔案已搬走（要重開機才生效），只印 todo。
- **建議**：把整個 `old.unit_dir` 當一個目錄搬（`move-unit-dir`）；搬完對含敏感檔的子樹 `os.chown` 回 llmgw（或改用 `_copy_into_place` 逐檔搬再刪，自己控制 owner／mode）；user manager 不可用時明確 todo「重開機後生效」。

### 低-5 `_wait_file` 只等檔案「存在」，舊的 `.keysync.status` 會讓 90 秒等待立刻通過

- **檔案:行**：`lgwctl/migrate.py:749-751`、`lgwctl/migrate.py:771-780`（`_wait_file` 只看 `os.path.isfile`）、`lgwctl/check.py:124-129`（stale > 1800 秒 → fail）。
- **問題**：切換前若 `/var/lib/llmgateway/.keysync.status` 已存在（先手動起過新版、或某次 rollback 留下），等待立刻回 True，接著 `check` 用那個舊的 `ts` 判新鮮度 → 大概率 fail → 停損回舊版，而且 log 看起來像「key 拉取失敗」。
- **建議**：記下切換開始時間，只接受 `mtime` 晚於它的狀態檔（或等待前把舊狀態檔改名留檔）；逾時就明確 todo，並在 `check` 之前印一行這次狀態檔的 mtime。

### 低-6 `--inner` 是人手可以自己加的旗標；`--revert-after` 允許 0 或極小值而沒有下限提醒

- **檔案:行**：`lgwctl/migrate.py:1093-1094`（`--inner`）、`lgwctl/migrate.py:653-655`、`lgwctl/migrate.py:120`（regex 允許 `0`、`30s`）、`lgwctl/migrate.py:649-651`（只擋格式）。
- **問題**：(a) 在 ssh 上直接照抄完整指令（含 `--inner`）就沒有分離執行，連線一斷切換停在中間（保險還在，所以會被接回舊版，但工作沒做完、輸出也沒人看得到）。(b) `--revert-after 0` 會讓保險在切換進行中就開跑（中-3 的時間軸），而程式只擋「格式不認得」。
- **建議**：`--inner` 需要一個只有 transient unit 才會有的環境變數（例如 systemd 設的 `INVOCATION_ID`）才接受，或改名成不外顯的內部旗標並在 `--help` 標明；`--revert-after` 給一個合理下限（例如 ≥ 120 秒），太小就 fail 或 todo 提醒「會在切換進行中就回滾」。

### 低-7 `_keys_env_valid` 只驗語法不驗鍵名，語法合法但鍵名錯的檔會被複製回舊 state

- **檔案:行**：`lgwctl/migrate.py:251-274`（只檢查 `NAME=VALUE`、至少一個有值、無 NUL）、`lgwctl/migrate.py:929-938`（驗過就覆蓋舊的 `keys.env`）、`lgwd/keys.py`（真正的鍵名白名單 `KEY_NAMES`）。
- **問題**：新服務寫出的檔若含未知鍵（例如人工貼錯），這裡算「驗得過」就覆蓋舊的 `keys.env`；舊服務啟動時 `KeyStore` 會丟 `KeyFileError` → 回舊版起不來 —— 正好打在最不能壞的那一步。
- **建議**：用 `lgwd.keys.KEY_NAMES` 當白名單驗一次（或在暫存檔上真的 `KeyStore(...)` 載入一次再決定），驗不過就 todo 不要覆蓋；測試補一條「鍵名不在白名單」。

### 低-8 `prepare` 不複製 `.keysync.sha`；`write_state` 沒有互斥

- **檔案:行**：`lgwctl/migrate.py:474-490`（`_copy_old_files` 只複製 keys.env／llm.db／兩個 env／keys.pass）、`lgwctl/layout.py:44-46`（`.keysync.sha` 在版面裡）、`lgwctl/migrate.py:309-317`。
- **問題**：(a) 規格的 state 清單包含 `.keysync.sha`，沒複製 → 第一次 key-sync 會全量重拉（`ops-scripts/key-sync` 的行為，見其說明第 2 點），功能上沒錯，只是多一次拉取；順帶 `keys.env` 會被新版改寫，之後回滾時新的 `keys.env` 比較新 → 被複製回舊 state（無害，但值得知道）。(b) `write_state` 是「讀整個檔 → 合併 → 寫回」，沒有鎖也不是 compare-and-swap，兩個行程同時寫時後寫者會蓋掉先寫者的欄位（中-3、中-9 的同一個機制）。
- **建議**：(a) 要嘛一起複製（省一次全量拉取），要嘛在輸出一行 `todo keysync-sha` 說明會全量重拉；(b) `write_state` 用與 `_Lock` 同一把鎖包起來。

### 低-9 測試用的旋鈕（觀察期天數、等待時間）在真機上也能被環境變數改掉

- **檔案:行**：`lgwctl/migrate.py:103-108`、`lgwctl/migrate.py:179-199`（`_wait_seconds`／`_observe_seconds`）、`lgwctl/migrate.py:986-989`（finalize 的 3 天條件）。
- **問題**：`LLMGW_OBSERVE_DAYS=0` 就能讓 `finalize-migration --yes` 立刻過 3 天條件；`LLMGW_CUTOVER_WAIT=0` 讓回舊版只探一次。這些是測試需要的旋鈕，但沒有任何「只在測試模式生效」的保護。
- **情境**：真機 shell（或某個 unit 的 `Environment=`）裡剛好有同名變數，觀察期與等待時間被無聲改掉。
- **建議**：只在 `layout.prefix` 有設時才讀這些等待／觀察期變數（測試本來都有設 prefix），或真機值生效時印一行說明目前用多少。

### 低-10 測試缺五個關鍵覆蓋（都對應上面中高項目）

- **檔案:行**：`tests/test_lgwctl_migrate.py`（全檔）、`tests/lgwctl_fakes.py:84`（假 git）、`tests/test_lgwctl_migrate.py:55-107`（假 systemctl）。
- **問題**：沒有的覆蓋：(1) 假 systemctl 回 1 並印 `Failed to connect to bus: … No such file or directory` 時 (b) 必須 fail（中-2）；(2) 狀態檔塞 `revert_unit: "sshd.service"` 時 `confirm` 不得呼叫 systemctl（高-3）；(3) 舊檔是 symlink 時 prepare 拒絕（中-5）；(4) `keys.pass` 的 uid/gid（中-7，prefix 模式會跳過 chown，現在只驗 mode）；(5) 版本目錄權限（高-1，而且**假 git 的 `cp -a` 目前正好掩蓋它**，要先把假 git 修成不改 mode 才測得到）。另外 `test_rollback_is_idempotent_when_insurance_fires_twice` 只測「人工連按兩次」，沒有測「保險與切換並行」（中-3）。
- **建議**：照上面五條各補一個測試；順序斷言本身寫得不錯（`order.log` 由假指令寫、`index_of` 找相對位置），新增的測試請沿用同一個機制，不要改成在行程內呼叫函式後自己排序（那才是寄居蟹）。

---

## 資訊（4）

### 資訊-1 值與密碼沒有外洩：argv／輸出／log／狀態檔／例外都查過了

- **檔案:行**：`lgwctl/migrate.py:561-586`（讀舊 `keysync.env` → 寫 `keys.pass`，輸出只印路徑與「值不印出」）、`lgwctl/migrate.py:237-248`（`_has_password` 只判有無）、`lgwctl/migrate.py:251-274`（`_keys_env_valid` 只讀不印）、`lgwctl/migrate.py:309-317`（狀態檔欄位只有 phase／tag／unit／ts／reason／local_ok）、`lgwctl/migrate.py:612-637`（兩支 `systemd-run` 的 argv 只有路徑、tag、unit 名、時間）、`lgwctl/migrate.py:410-417`（失敗只印 `rc=`）。
- **判斷**：沒有發現密碼或 key 值出現在 argv、輸出、log、狀態檔或例外訊息（例外一律只印 `type(exc).__name__`）。測試端也有 `assert_no_values` 對輸出、假指令 log、狀態檔做假值 grep。**不用改。**
- **唯一順帶提醒**：低-2 的整檔讀取是可用性問題，不是外洩。

### 資訊-2 保險確實先於停舊服務、切換確實分離執行、rollback 確實冪等（這三項任務書特別點名的都對）

- **檔案:行**：`lgwctl/migrate.py:681-699`（(a) 先排保險，排不起來就 `fail` 且不碰舊服務）、`lgwctl/migrate.py:612-671`（outer 只 `systemd-run` 就結束）、`lgwctl/migrate.py:833-871`（flock ＋ `phase == reverted` 分支不動資料）、`tests/test_lgwctl_migrate.py:539-571`（保險 index < 第一次 `--user stop`；停完才 `systemctl enable`）、`tests/test_lgwctl_migrate.py:611-620`（保險排不起來時 `actions()` 是空的）。
- **判斷**：(a) 保險的順序斷言是**真的在斷言 migrate 的呼叫順序**（`order.log` 由假指令寫、`index_of` 找相對位置），不是在自己的 helper 之間對照 → 有意義，不是寄居蟹；(b) `runuser` 的 argv 與 `XDG_RUNTIME_DIR`／`DBUS_SESSION_BUS_ADDRESS` 有被斷言（`test_lgwctl_migrate.py:551-553, 759-762`），做法正確 —— 不過「linger 沒開」這個失敗模式會被中-2 的寬鬆判斷吞成 skip，實機上要另外演練；(c) 冪等：flock ＋ `reverted` 短路正確，`.bak` 只備份一次在資料語意上是中-6 的問題但對「重複觸發不重複動作」這個要求是對的。
- **建議**：不用改（除了中-2、中-3 的相關部分）。可以補一條：假 systemd-run **真的**在 `--on-active` 到點時把指令丟去背景執行（用 `sleep $秒數` 之類），這樣「保險真的會自己回舊版」才有自動化覆蓋；目前這條路徑完全沒有測試，只有演練能驗。

### 資訊-3 舊 user unit 與 `/opt` 無關，回滾不依賴 `current` symlink；`prepare` 沒動到任何舊檔

- **檔案:行**：`deploy/llmgateway.service:7`（`ExecStart=%h/.local/bin/llmgateway-run`）、`deploy/install.sh:501`（舊 unit 檔在 `~/.config/systemd/user/`）、`lgwctl/migrate.py:470`（`old-untouched`）、`tests/test_lgwctl_migrate.py:404-416`（逐檔快照比對）、`tests/test_lgwctl_migrate.py:360-381`（prepare 沒有 `enable`／`start`／`stop`，order 裡沒有 `--user` 與 `runuser`）。
- **判斷**：舊服務走 `%h/.local/bin/llmgateway-run` 與 `~/.local/share/llmgateway`，所以 `prepare` 切 `current`、搬 `/opt` 都不影響舊版，`rollback-to-user` 也不需要把 `current` 切回去 —— 這條接縫是對的。「prepare 全程不影響舊服務」有逐檔快照測試撐著，值不外洩也有假值 grep。**不用改。**

### 資訊-4 測試數字與時間成分

- **檔案:行**：`tests/test_lgwctl_migrate.py:194-201`（`LLMGW_CUTOVER_WAIT=0`、`LLMGW_KEYSYNC_GRACE=0`）、`tests/lgwctl_fakes.py:237`（`LLMGW_LIST_WAIT=0`）、`tests/test_lgwctl_migrate.py:1044-1066`（狀態檔一行 JSON、合併寫入）。
- **判斷**：全量 `Ran 412 tests in 165.188s → OK`（Python 3.12.3），與任務書預期一致。遷移相關的等待都被調成 0（只探一次），沒有 sleep 競態；`finalize` 的 3 天條件靠 `confirmed_ts = now - 4*86400`，是真實的時間流程而不是把函式 mock 掉，這點是對的。假指令的狀態（active／fail／missing）用檔案代表，測試沒有靠改程式碼的行為來製造情境。
- **唯一會影響正確性的寄居蟹是假 git 的 `cp -a`（高-1）**，其餘順序斷言都成立。

---

## 規格逐條核對

| 任務書／規格 | 實作 | 判定 |
|---|---|---|
| `prepare-migration`：放新佈局、不 enable、不 start、複製 keys.env／llm.db／tunnel.env／llmgateway.env／舊 keysync.env 的密碼→keys.pass（root:llmgw 0640、單行）、已存在不覆蓋（`--force` 除外）、值不印出、舊 unit 與舊目錄不動 | `migrate.py:432-606` | △ 版本目錄權限（高-1）、在線複製 db（高-4）、跟 symlink（中-5）、斷 symlink traceback（低-1）、owner 規則（中-7）；其餘 ✓（有逐檔快照測試） |
| `cutover`：分離執行、先排保險再停舊、(c) 啟新、(d) 本機＋會合點各探一次、(e) 任一不過立刻 rollback、全過寫 pending-confirm 並提示 confirm | `migrate.py:612-768` | △ 保險與切換無互斥（中-3）、遠端探針單次（中-4）、舊 unit 不 disable（高-2）、transient 環境（中-1）、pre-flight 太薄（中-8）、版本目錄權限（高-1）；順序與「排不起來就不切」✓ |
| `confirm`：非 pending-confirm 就拒絕；停 `lgw-revert.timer`（找不到當已取消）；狀態改 confirmed | `migrate.py:806-827` | △ 讀檔裡的 unit 名（高-3）、不看保險是否已開跑（中-9）；流程 ✓ |
| `rollback-to-user`：停（並 disable）新 unit、db 複製回舊 state（先備份）、keys.env 僅在更新且格式過時覆蓋、以 llmgw 身分啟舊 unit、探本機 200、取消保險、狀態 reverted、冪等 | `migrate.py:833-961` | △ 可能覆蓋較新的 db（中-6）、鍵名不驗（低-7）、狀態寫入無互斥（低-8）、bus 失敗被當 skip（中-2）；冪等與 flock ✓ |
| `finalize-migration`：狀態要 confirmed 且過 3 天、需 `--yes`、舊 unit／目錄搬移不刪除、disable-linger | `migrate.py:967-1067` | △ 沒綁 old_home、不先確認健康（低-3）、跨 fs owner 與只搬四個 unit（低-4）；條件與「搬移不刪除」✓ |
| 安全：值／密碼不進 argv、log、狀態檔、例外 | 見資訊-1 | ✓ |
| 檔案權限與擁有者：keys.pass root:llmgw 0640、keys.env 0600 llmgw | 中-7 | △ prepare 這條對，與 install 相反 |
| 以 root 代 llmgw 操作 user unit（runuser ＋ XDG／DBUS） | `migrate.py:371-384` | ✓ 做法正確；失敗模式被中-2 的寬鬆判斷吞掉 |
| 測試：假指令＋順序斷言、值不外洩 grep、冪等 | `tests/test_lgwctl_migrate.py` | △ 412 個全過；缺低-10 列的五項，其中假 git 掩蓋了高-1 |

---

## 結論

停損的基本骨架是對的：反向保險確實排在停舊服務之前、切換確實跑在 detached 的 transient unit、冪等有 flock 與 `phase=reverted` 短路撐著、值沒有外洩、舊 user unit 與 `/opt` 的接縫也乾淨。要在真機上切正式服務之前必須處理的是四個高項：**版本目錄 0700 會讓服務根本起不來（而且測試的假 git 把它掩蓋了）、舊 user unit 沒 disable 會在觀察期重開機時新舊雙跑、服務帳號可寫的狀態檔被當成 root 的 systemctl 參數與 3 天條件、`prepare` 在舊服務寫入中複製 SQLite**。中項九件建議一併修，其中中-1（transient unit 不帶環境變數）會直接影響 §7 指定的演練怎麼跑，最好在演練之前就有明確的作法。

結論：需修
