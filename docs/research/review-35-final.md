審查模型：DeepSeek V4.1 Flash，與作者同模型不同 session，因 Zen 免費模型 429（本輪改用 Ollama Cloud）。

# 審查 #35 整合分支 `impl/install-simplify`（最終輪）

**審查對象**：`impl/install-simplify` @ `a717e8d`（merge T1／T2／T3／T4；本 worktree `review/final-35` 內容相同）。重點是上一輪審查（T1 `review-35-t1.md`、T2／T3 `review-35-t2t3.md`、T4 `review-35-t4.md`）之後的修正，以及**新發現**的切換／回滾問題。

**方法**：逐檔讀 `lgwctl/`、`lgwd/keysync.py`、`lgwd/main.py`、`deploy/units/`、`ops-scripts/`、`tests/test_lgwctl_*`／`tests/lgwctl_fakes.py`，對照 `spec-35.md`、`install-simplify-design.md` §4／§5／§7 與三份上一輪報告；另用 `tests/test_lgwctl_migrate.py` 的沙箱實跑幾個情境（下面標「實測」）。**沒有改被審程式碼、沒有 push、沒有碰真機、沒有讀任何憑證檔或真 key；本報告是唯一新增檔。**

**驗證**：

| 項目 | 結果 |
|---|---|
| `python3 -m unittest discover -s tests` | `Ran 519 tests in 202.305s` → `OK (skipped=1)`（Python 3.12.3；與提示的期望 519／skipped=1 一致） |
| 測試裡的 root-only chown 測試 | 非 root 執行，`skipped=1` 就是它（`tests/test_lgwctl_install.py:353`） |
| 沙箱實測 | 切換後 keysync 等待（新-中1）、回舊版後 key-sync.timer（新-中11）、upgrade 後 rollback 的 unit 檔（新-中3）、finalize 後 rollback 的狀態（新-中4） |

**等級數量（新發現）**：高 1、中 11、低 13、資訊 7。

---

## 一、上一輪「高／中」逐項核對

判定：**已修**＝原問題的觸發路徑已關掉且有測試；**部分**＝主體已修但仍有可重現的殘留；**未修**＝程式沒動。

### T1（`review-35-t1.md`）

| 項 | 判定 | 證據（本分支） |
|---|---|---|
| 高-1 版本目錄 0700、`User=llmgw` 進不去 | **已修** | `lgwctl/release.py:86-115`（`normalize_tree`：目錄 0755、檔 0644／原執行位 0755、root:root、移除 `.git`）、`release.py:146-193`（新抓與沿用都會跑）、`lgwctl/install.py:193`（`opt_dir` 0755）；測試 `tests/test_lgwctl_install.py`（mode 斷言）、`tests/test_lgwctl_migrate.py:638-652`（假 git 故意留 0700） |
| 中-1 `is-active` rc 4 被當 unknown／todo | **已修** | `lgwctl/units.py:83-99`（新增 `missing`）、`lgwctl/check.py:61-64`（missing → fail「unit 不在（沒裝）」）、`status.py:17-18`；測試 `tests/test_lgwctl_check.py:126-140`（含隧道） |
| 中-2 upgrade 不換 unit／不 daemon-reload | **已修（主體）** | `lgwctl/units.py:30-43`（`install_unit_files`）、`units.py:46-74`（snapshot／restore）、`lgwctl/upgrade.py:123-143`（換 unit→reload→restart，失敗連 unit 一起回復）；測試 `tests/test_lgwctl_upgrade.py:79-88,145-177`。殘留：**手動 `llmgwctl rollback` 沒換回舊版 unit**，見新-中3 |
| 中-3 install 換 tag 不 restart | **已修** | `lgwctl/install.py:366-369`：`current` 已是別的 tag 就直接 fail「換版請用 upgrade」，不再切 current；測試 `tests/test_lgwctl_install.py`（tag 變更拒絕） |
| 中-4 keys.pass 被 chown 成 llmgw:llmgw | **已修** | `lgwctl/install.py:97-118`（`chown_root_group`：root:<服務 gid>）、`install.py:224-233`（每次 install 都校正）；測試 `tests/test_lgwctl_install.py:327-372`（mock uid/gid＋root-only 真 chown） |
| 中-5 sync 不寫 `.keysync.status`、沒 restart 提示 | **已修** | `lgwctl/sync.py:31-43`（T2 格式、原子寫、0644）、`sync.py:78-90`（成功寫 `ok`＋`todo restart`、失敗寫 `ok:false`）；`lgwctl/layout.py:355-370`（`child_env` 預設移除密碼）；測試 `tests/test_lgwctl_cli_sync.py` |
| 中-6 `--repo` 參數注入 | **已修** | `lgwctl/release.py:38-44`（`bad_repo` 擋 `-`／NUL）、`release.py:167-170`（`git clone … -- <repo> <tmp>`）、`release.py:23-34`（必備檔補 `lgwctl/__main__.py`、`lgwd/main.py`、`policy.py`、`keys.py`）；測試 fake git 斷言 argv |
| 中-7 `Type` 註解錯、start 只看 rc | **已修** | `deploy/units/llmgateway.service:10-12`（`Type=exec`＋修正註解）、`lgwctl/install.py:272-289`（start 後等本機 list 200）、`install.py:292-310`（隧道 start 後再 `is-active`）；測試 `tests/test_lgwctl_install.py`（`start_noop`） |

T1 低項：低-1（tunnel.env `LOCAL_API` 跟 `api_port()`）已修（`install.py:205-217`）；低-2（隧道 todo 加「填好後 start／重跑 install」）已修（`install.py:292-310`）；低-3（非 root `check` 誤導）**未修**（`check.py:190-201` 沒有 root 檢查）；低-4 已修（`layout.py:355-370`）；低-5 已修（`upgrade.py:22-34,49-63`）；低-6 已修（`upgrade.py:74-93`）；低-7（keysync stale 固定 1800）**未修**（`layout.py:86-89`）；低-8 已修；低-9 已修（`install.py:141-177`）；低-10（wrapper `cd {current}` 未引號）**未修**（`install.py:71-76`）；低-11（中斷留下 `.tag.*` 暫存）**未修**（`release.py:160-193` 沒有 `try/finally`，`cleanup_old_versions` 跳過 dot 名稱 230-232）。

### T2／T3（`review-35-t2t3.md`）

上一輪 T2／T3 沒有高、中也沒有需要這次核對的項目；低-1／2／3／4／6 在 `08c6abd` 已修（`lgwd/keysync.py:131-162` 只留 stderr 尾巴、`165-181` 用 `proc.pid` 當 pgid、`245-266` stop 與 spawn 同鎖＋SIGKILL、`58-64,118-128` reason 白名單；測試 interval 已調 5 秒），低-5 由 T1 的 `After/Wants=network-online.target` 補上（`deploy/units/llmgateway.service:6-7`）。T3 的 unit／`LGW_TUNNEL_ENV` 與 T1 的接縫（探針帶 `LGW_TUNNEL_ENV`、狀態檔同目錄、`sudo -u` 的 gh）都仍對得上（`lgwctl/probe.py:58-70`、`lgwctl/check.py:157-187`）。

### T4（`review-35-t4.md`）

| 項 | 判定 | 證據（本分支） |
|---|---|---|
| 高-1 版本目錄 0700、切換必 CHDIR 失敗；假 git `cp -a` 掩蓋 | **已修** | `lgwctl/release.py:86-115,146-193`；`tests/test_lgwctl_migrate.py:237-246` 把假 git 的 dest 改回 0700，`:638-652` 斷言 0755；實測 prepare 後目錄 0755 |
| 高-2 舊 user unit 只 stop 不 disable；重開機新舊雙跑 | **已修（主體）** | `lgwctl/migrate.py:1124-1165`（stop 後 disable 四個、`is-active` 逐一確認）、`migrate.py:1550-1572`（回舊版再 enable）、`migrate.py:1226-1229`（重開機提醒）；測試 `tests/test_lgwctl_migrate.py:795-835,898-908`。殘留：重開機窗口與 transient 保險消失見新-中5；回舊版沒 start timer 見新-中11 |
| 高-3 狀態檔在 llmgw 可寫目錄、unit 名被當 root 輸入 | **已修** | `lgwctl/migrate.py:89-93`（`/var/lib/llmgateway-ctl`）、`migrate.py:450-469`（0700 root、狀態 0600）、`migrate.py:497-500,1349-1353`（只接受 `lgw-revert.timer`／`.service`）、`migrate.py:1430-1439`（回滾用固定值）；測試 `tests/test_lgwctl_migrate.py:1012-1020,1499-1514`、注入 `sshd.service` 的測試 |
| 高-4 prepare 在舊服務寫入中 copy `llm.db` | **已修** | `lgwctl/migrate.py:297-307`（SQLite backup API）、`migrate.py:322-353`（暫存＋`integrity_check`＋重試）、`migrate.py:809-826`；測試 `tests/test_lgwctl_migrate.py:619-636`（開著寫入交易，只看到已提交資料、integrity ok） |
| 中-1 transient unit 不帶呼叫端環境 | **已修（真機所需三項）** | `lgwctl/migrate.py:945-957`（`--setenv` 只轉 `LLMGW_PREFIX`／`SERVICE_USER`／`SERVICE_HOME`）、`migrate.py:960-988`；其他 `LLMGW_*` 測試旋鈕仍不轉送（見資訊-4） |
| 中-2 `_UNIT_MISSING_MARKERS` 太寬，bus 失敗被當 missing | **已修** | `lgwctl/migrate.py:158-164`（fatal 先判）、`migrate.py:547-556`（`_judge`）、`migrate.py:600-617`（user 層 is-active 的 unknown）；測試 `tests/test_lgwctl_migrate.py:909-919`（bus failure 是 fail 不是 skip） |
| 中-3 inner 不參與 flock、保險／人工回滾互撞 | **已修** | `lgwctl/migrate.py:1068-1087`（inner 拿同一把鎖）、`migrate.py:1167-1172`（不可逆步驟前確認 phase）、`migrate.py:1389-1402`（拿不到鎖明確失敗）、`migrate.py:148-150`（`--revert-after` 下限 5 分鐘）；測試 `:766-776,1140-1151`。**副作用**：保險可能在切換中因等不到鎖而死，見新-中2 |
| 中-4 隧道 start 後單次遠端探測 | **已修** | `lgwctl/migrate.py:620-629`（先等 unit active）、`migrate.py:1193-1205`、`migrate.py:1245-1262`（預設 3 次、間隔 10 秒）；測試 `:921-931`。殘留：後面的 `check` 又單次探，見新-中8 |
| 中-5 root 讀舊檔跟 symlink（提權） | **部分** | `lgwctl/migrate.py:421-430`（`lstat`＋`_refuse_symlink`）、`migrate.py:785-806,814-826,834-862,870-897,906-915` 都先檢查；測試 `:539-568`。**但檢查與 `copy2`／`sqlite3.connect`／`open` 不是同一個 fd，TOCTOU 仍在**，見新-中9 |
| 中-6 回滾用 prepare 舊快照蓋掉較新的舊 db；`.bak` 只留一份 | **已修** | `lgwctl/migrate.py:1458-1490`（只在新的真的寫過才覆蓋）、`migrate.py:1493-1507`（`new_service_ts`）、`migrate.py:1510-1521`（時間戳備份＋保留 3 份）；測試 `:1153-1176`。殘留：沒有 state 的 fallback，見新-低7 |
| 中-7 keys.pass owner 規則與 install 相反、gid 取不到變 root:root、`/etc` 被 chown llmgw | **已修** | `lgwctl/install.py:97-108`（共用 `chown_root_group`）、`lgwctl/migrate.py:433-444`（KeyError → fail）、`migrate.py:829-862`（`/etc` 設定 root:<群組> 0640）；測試 `:590-615,1537-1563` |
| 中-8 cutover pre-flight 太薄 | **已修（原清單）** | `lgwctl/migrate.py:991-1016`（tag／版本目錄可進／keys.env／keys.pass 有值／tunnel.env）、`migrate.py:1047-1052`（outer 排程前）、`migrate.py:1096-1101`（inner 再跑）；測試 `:729-756`。**仍缺 gh 登入與 policy dry-run**，見新-中6、新-中7 |
| 中-9 confirm 不看保險是否已開跑、不確認在跑新版 | **已修** | `lgwctl/migrate.py:1341-1377`（phase、unit 白名單、`_system_unit_busy`、新服務 active、舊 user 不 active、停 timer 後才寫 confirmed）、`migrate.py:632-645`（activating／reloading）；測試 `:961-1047`。微小 race 見新-低13 |

T4 低項：低-1 已修（`migrate.py:776-781`）；低-2（`parse_env_file` 整檔讀）**未修**（`migrate.py:874` → `lgwctl/layout.py:92-119` 沒有大小上限）；低-3 **部分**（old_home 綁定 `migrate.py:746-751,1612-1624`，但 finalize 只探本機 list `:1626-1634`，沒有完整 `check`）；低-4 **部分**（三個目錄整個搬 `:1649-1661`，但四個 unit 檔仍逐一搬 `:1655-1658`，跨檔案系統 `shutil.move` 不保 owner `:1678-1693`）；低-5 **部分**（`since` 用錯時間點，見新-中1）；低-6 已修（`migrate.py:1028-1044,135-136`）；低-7 **部分**（`migrate.py:384-418` 用 `KEY_NAMES` 白名單，但沒擋重複鍵，見新-低6）；低-8a 已修（`migrate.py:900-919`）、低-8b **部分**（confirm／cutover／rollback 有鎖；finalize／prepare 的 `write_state` 仍沒鎖，見新-低4）；低-9 **未修**（`migrate.py:230-250` 的測試旋鈕在真機也生效）；低-10 已修（測試補了 bus failure、狀態注入、symlink、chown、版本目錄）。

---

## 二、新發現

### 高（1）

#### 高-1 `rollback-to-user` 會停掉自己正在走的隧道：SSH session 在回滾中途被切斷，舊 unit 可能還沒被叫回來

- **檔案:行**：`lgwctl/migrate.py:1383-1402`（`rollback_to_user` 是人手直接跑的普通指令，沒有分離執行）、`migrate.py:1442-1455`（`_stop_new_units` 先停／disable 新的 `llmgateway.service`，再停／disable 新的 `llmgateway-tunnel.service`）、`migrate.py:1405-1427`（停完才 restore db／keys、`_start_old_units`）、`migrate.py:1550-1572`（舊 unit 在這裡才 enable／start）、`ops-scripts/llmgateway-tunnel:165`（隧道的第二條 `-R` 把會合點的 4711 轉到本機 sshd 22）、`docs/research/install-simplify-design.md:93`（設計前提：管理連線經隧道進來）、`install-simplify-design.md:84`（觀察期內用 `rollback-to-user` 一個指令回去）。
- **問題**：切換成功後，唯一的管理通道是**新的 system 隧道**（舊 user 隧道已被 cutover 停掉並 disable）。`rollback-to-user` 在 `_stop_new_units()` 裡把這個隧道停掉／disable，等於把操作者自己的 SSH 連線切斷。sshd 在連線斷掉時會對 session 的 process group 送 SIGHUP，`sudo llmgwctl rollback-to-user` 這個前景行程很可能在「新服務已停、舊服務還沒 enable／start」之間被殺掉；而 cutover 已把四個舊 unit 全部 disable（`migrate.py:1144-1155`），所以接下來不會有任何東西自動回來：API 與隧道同時下線，只能靠 console／LAN 進去救。即使沒被立刻殺掉，`_restore_db`／`_restore_keys_env` 也還沒跑，資料狀態不完整。反向保險（`lgw-revert.service`）是 transient system unit，不受這個問題影響；**只有人手在觀察期跑的 `rollback-to-user` 有這個洞**，而這正是設計指定的回滾方式。
- **情境**：觀察期第 2 天發現新版本有問題，操作者照 §5.4 在隧道的 SSH session 裡跑 `sudo llmgwctl rollback-to-user`。`_stop_new_units` 停掉新服務（API 先下線），接著停新隧道（SSH 斷線），行程被 SIGHUP；舊的 `llmgateway.service`／`llmgateway-tunnel.service` 仍是 disabled，機器沒有任何服務，遠端也進不去。若行程剛好活過 disable 新隧道，開機時新隧道會起、新服務不會（被 disable），API 仍下線。
- **建議**：把 `rollback-to-user` 也做成**分離執行**（像 cutover 一樣 `systemd-run --unit=lgw-rollback …` 後立刻返回，操作者重連後看 journal／`status`）；或至少重排成「restore db／keys → enable 舊 unit → 停新 service、起舊 service、確認本機 200 → 寫 `reverted` → 最後才停新隧道／起舊隧道」，讓管理連線斷掉時所有關鍵工作已完成。文件與 `--help` 要寫明「這個指令會切斷你正在走的隧道，請用分離執行／console」。

---

### 中（11）

#### 中-1 切換後的 keysync 等待用「現在」當門檻，正常情況一定等滿 90 秒並印出假的 todo

- **檔案:行**：`lgwctl/migrate.py:1209-1214`（`started = time.time()` 才在 (d) 之後取，接著 `_wait_file(..., since=started)`）、`migrate.py:1275-1293`（只接受 `mtime >= since` 的狀態檔）、`migrate.py:1217`（之後才 `check`）。
- **問題**：新的 `lgwd` 在 (c) 啟動後 30 秒內就會跑第一次 key 拉取並寫 `.keysync.status`；但 `started` 是在 (d)（本機 list、隧道 active、遠端探針）之後才取，通常已經比狀態檔晚 5～40 秒。於是那個「剛寫好的狀態檔」被當成太舊，`_wait_file` 會等滿 `LLMGW_KEYSYNC_GRACE`（預設 90 秒）才放行，然後印 `todo keysync（等不到這次切換後第一次拉取的結果）`；緊接著的 `check` 再讀同一個檔案，卻是 `ok keysync`。這讓每次切換多花 90 秒（吃掉 5 分鐘窗口的一大段），也給操作者一個「key 拉取失敗」的假訊號。上一輪低-5 的建議是「記下**切換開始**時間」；實作記成了「等待開始」時間。
- **實測**：用 migrate 沙箱把 grace 設 1 秒，切換輸出同時有 `todo keysync（等不到這次切換後第一次拉取的結果…）` 與下一行 `ok keysync（最後一次 3s前…）`；預設 90 秒時就是等滿 90 秒。
- **情境**：每一次 cutover；`--revert-after 5m` 時這 90 秒直接壓縮保險的餘裕（與中-2 疊加）。
- **建議**：在啟動新服務**之前**（或至少在 (b) 之前）記 `cutover_started`，用它當 `since`；或直接等「狀態檔的 `ts` 大於服務啟動時間」即可。測試要把 grace 設成非 0，並在 (c) 前先放一個新鮮的狀態檔，斷言不會出現這個 todo。

#### 中-2 保險可能在切換進行中就因等不到 flock 而死；切換成功後仍宣稱「10 分鐘沒 confirm 會自動回舊版」

- **檔案:行**：`lgwctl/migrate.py:1103-1122`（先排 `lgw-revert.timer`）、`migrate.py:1075-1087`（inner 從頭到尾持有 `.cutover.lock`）、`migrate.py:1389-1395`（保險到點後跑的 `rollback-to-user` 只等 `LLMGW_LOCK_WAIT`（預設 30 秒），拿不到就 fail 結束）、`migrate.py:148-150`（`--revert-after` 下限只有 300 秒）、`migrate.py:1210-1219`（等待＋`check` 最壞可達數分鐘）、`migrate.py:1221-1229`（成功後直接寫 `pending-confirm` 並印「10 分鐘後自動回舊版」）。
- **問題**：`systemd-run --on-active` 的 timer 是**一次性**的；到點後 `lgw-revert.service` 會嘗試拿同一把 flock，但 inner 還持有鎖（切換最壞要本機 60 秒＋隧道 30 秒＋遠端探針 3×（每次最多 60 秒）＋keysync 90 秒＋`check` 再一次遠端探針，粗估可超過 300 秒）。保險等 30 秒後失敗退出，timer 已經消耗掉；inner 之後照樣寫 `pending-confirm` 並說「沒 confirm 會自動回舊版」，但實際上已經沒有保險了。若操作者因為某個原因沒 confirm，什麼都不會發生；若之後才發現問題，也沒有自動回舊版。`--revert-after 5m`（合法下限）時這個窗口很現實。
- **情境**：切換窗口用 `--revert-after 5m`，遠端探針較慢（或中-1 多等 90 秒），inner 跑了 5 分 20 秒；保險在 5:00 到點、等鎖 30 秒後失敗；inner 在 5:20 寫 pending 並說保險還在。
- **建議**：(1) 成功寫 pending 前確認 `lgw-revert.timer` 真的還在（`systemctl is-active`／`list-timers`），不在就明確 fail／todo 並提示「沒有自動回滾，請立刻 confirm 或 rollback-to-user」；(2) 讓保險可重試（例如 persistent timer／service 迴圈等鎖，而不是一次性 transient）；(3) `--revert-after` 下限至少大於切換最壞時間（或由 inner 依剩餘時間拒絕切換）。測試要模擬「保險到點時鎖還被持有」與「切換後 timer 不見」。

#### 中-3 手動 `llmgwctl rollback` 只切 current／restart，沒有把舊版的 unit 檔換回來

- **檔案:行**：`lgwctl/upgrade.py:123-143`（upgrade 會把新 tag 的兩個 unit 檔裝進 `/etc/systemd/system`）、`lgwctl/upgrade.py:149-175`（`rollback` 只寫 current／previous link、restart、跑 check，沒有 `units.install_unit_files`／`snapshot_units`）、`lgwctl/units.py:30-43`（unit 一律從當版目錄裝）。
- **問題**：上一輪中-2 把 unit 定義成「版本的一部分」，upgrade 會換 unit；但**手動回滾**（觀察期發現新版有問題，或自動回滾之外的復原）切回舊 tag 後，`/etc/systemd/system/llmgateway.service` 仍是新 tag 的內容。舊程式跑在新 unit 的 ExecStart／sandbox／`LoadCredential` 參數下，只要新舊版的 unit 有差異（新增旗標、改路徑、改 hardening），回滾就可能起不來或行為不同；而且 `check` 的 `policy` 也是用啟動時（新 tag）的 loader 去載舊 tag 的 policy（`lgwctl/check.py:140-154`）。
- **實測**：沙箱 upgrade 到 `lgw-v1.2.4` 後跑 `rollback`，current 回到 `lgw-v1.2.3`，但 `/etc/systemd/system/llmgateway.service` 仍是 `lgw-v1.2.4` 的內容；`tests/test_lgwctl_upgrade.py:236-246` 也只斷言 restart 次數，沒有斷言 unit 內容。
- **情境**：新 tag 的 unit 改了 `ExecStart` 或 `ProtectSystem`；新版有 bug，操作者用 `rollback` 回舊版 → systemd 用新 unit 啟動舊程式，可能直接失敗，回滾路徑反而比 upgrade 的自動回滾更脆弱。
- **建議**：`rollback` 在 restart 前用 previous tag 目錄的 `deploy/units/` 裝回兩個 unit＋`daemon-reload`；失敗就切回原 current／unit 並非零結束。若 previous 沒有 `deploy/units`（舊版格式），要在切 link **之前**就 fail 並提示「這一版無法用 rollback，請手動處理」。補測試：upgrade 後 rollback 斷言 unit 內容回到舊 tag。

#### 中-4 狀態機沒有「重設」與終態守門：停損後不能再切、finalize 後 rollback 會先把新版停掉

- **檔案:行**：`lgwctl/migrate.py:1092-1101`（cutover 只拒絕 `phase == reverted`）、`migrate.py:1407-1416`（rollback 只特別處理 `reverted`，沒有拒絕 `finalized`）、`migrate.py:1550-1572`（`enable` 回 missing 也印 ok）、`migrate.py:1596-1610`（finalize 只收 confirmed＋3 天）、`migrate.py:746-751`（prepare 只補 `old_home`，不重設 phase）、`migrate.py:708-753`（prepare 沒有檢查目前 phase／新服務是否在跑）。
- **問題**：
  1. **停損後的重試死路**：任何 `_stop_loss` 都會走 `_rollback_locked` 並把 phase 寫成 `reverted`（`migrate.py:1424`）。之後再跑 `cutover` 會直接 fail「狀態是 reverted；不重複切換」；`prepare` 也不會清掉 phase，`confirm`／`finalize` 也不接受。沒有 `reset` 子指令，唯一的辦法是 root 手動 `rm /var/lib/llmgateway-ctl/.cutover.state`（文件沒寫）。而且就算刪了，`prepare` 預設不覆蓋已存在的 `llm.db`／`keys.env`（`migrate.py:776-826`），會拿第一次失敗前的舊快照重切；要 `--force` 才會重抓，但 `--force` 又可能在觀察期覆蓋正在使用的新 db。這是切換失敗後最常走的路，現在等於需要手動狀態手術。
  2. **finalize 後 rollback**：`finalize` 已把舊 unit 檔與舊目錄搬到 archive；若之後跑 `rollback-to-user`，`_stop_new_units` 會先把新的 service／tunnel 停掉並 disable，`_start_old_units` 才發現舊 unit 檔不存在；`enable` 回 missing 被當成 ok，`start` 失敗 → 最後新版停了、舊版起不來。`lgwctl status` 也不會提醒 phase 是 finalized。同理，`cutover` 在 `confirmed`／`finalized` 狀態仍會排新保險、停舊、啟新，把狀態改成 pending（可能重新觸發自動回滾）。
  3. **prepare 在 cutover 前跑 rollback**：舊服務本來就在跑，`_rollback_locked` 會把 phase 寫成 `reverted`，之後 cutover 永久拒絕；這其實只是「取消遷移」，不該污染狀態。
- **情境**：切換窗口停損（本機 list 非 200）→ 回舊版；PM 修好問題後想當天再切，`cutover` 說 reverted，只能人工刪狀態檔。或觀察期結束跑完 finalize，某天有人照舊文件跑 `rollback-to-user` → 生產服務被停、舊版起不來。
- **建議**：加一個明確的 `reset-migration`（或 `prepare --reset`）只清 ctl 狀態並要求重新 `prepare --force`；`rollback-to-user` 在 phase `finalized`（或舊 unit 檔不存在）時**在停新 unit 之前**就拒絕；`cutover` 在 `pending-confirm`／`confirmed`／`finalized` 時拒絕（除非 `--force` 並有明確警告）；`_start_old_units` 把 enable 的 missing 當 fail（見低-2）。測試：reverted 後 reset 可再切；finalized 後 rollback 不呼叫任何 stop；confirmed 後 cutover 被拒。

#### 中-5 切換窗口內重開機：舊已 disable、新還沒 enable 時什麼都不會起；重開機後也沒有 boot-time 補救

- **檔案:行**：`lgwctl/migrate.py:1124-1155`（先停／disable 舊四個）、`migrate.py:1174-1183`（之後才 enable／start 新兩個）、`migrate.py:1075-1087`（`lgw-cutover` 是 transient unit）、`migrate.py:1226-1229`（「觀察期內不要重開機」只在成功後印）、`docs/research/install-simplify-design.md:83`（保險是 transient timer）。
- **問題**：cutover 的 (b) 先把舊 unit disable，(c) 才 enable 新 unit；這中間（以及 (c) 之後的檢查階段）如果機器重開機，`lgw-cutover`／`lgw-revert` 兩個 transient unit 都不存在。若重開機落在 (b) 與 (c) 之間，舊 disabled、新未 enable → **API 與隧道都不起**。若落在 (c) 之後，新 unit 會起，但狀態停在 `cutover-started`，`confirm` 會拒絕（不是 pending），也沒有保險；操作者必須知道要跑 `rollback-to-user`（或手動清狀態）。成功後的提醒只覆蓋 pending 階段，窗口內的風險沒有講。
- **情境**：切換窗口 5 分鐘內主機因核心更新／電力／panic 重開；或操作者看到 (b) 的輸出以為可以重開。之後 console 進去才發現沒有服務。
- **建議**：至少把「**(b)～(e) 期間不要重開機**」印在 (b) 之前；更好的做法是留一個 boot-time 檢查（例如 `llmgateway-boot-check.service` 讀 `.cutover.state`：`cutover-started` 就完成切換或自動回舊版、`pending-confirm` 就依政策回舊版），或把保險做成 persistent unit。測試可覆蓋「state=cutover-started 時 boot check 的決策」。

#### 中-6 pre-flight 沒檢查 `gh` 登入；沒有 gh 時會先停舊服務，再被 `check` 的 todo 判停損

- **檔案:行**：`lgwctl/migrate.py:991-1016`（preflight 沒有 gh）、`migrate.py:922-930`（prepare 只把 gh 登入列 todo）、`lgwctl/check.py:157-187`（`gh-keys-repo` 沒登入是 **todo**）、`lgwctl/migrate.py:1217-1219`（`check` 回非 0 → `_stop_loss`）。
- **問題**：設計的停損是「`check` 非 0 → 回舊版」，而 `check` 把「gh 沒登入」算 todo（結束碼 2）。因此一台 gh token 過期／沒登入的機器，`prepare` 只列 todo（不是 fail），操作者照著跑 cutover：停舊服務、啟新服務，然後 `check` 因為 gh todo 回 2 → 立刻停損回舊版。這是一次完全可以避免的服務中斷與回滾；preflight 應該在**還沒停舊服務**時就拒絕。
- **情境**：舊版的 gh token 過期，`prepare` 印 `todo gh-login`；操作者以為 todo 可以之後再處理，排了切換窗口 → 白停一次。
- **建議**：preflight 用 `install._gh_logged_in(layout)` 檢查，false 就 fail（不是 todo）；或把 `check` 的 `gh-keys-repo` 在 cutover 的 stop-loss 判準裡排除 todo（但設計寫「check 非 0」，較安全的作法仍是 preflight fail）。測試：`FAKE_GH_AUTH_RC=1` 時 outer 不排 transient unit、不動舊服務。

#### 中-7 pre-flight 沒跑 policy dry-run；壞 policy 會在停舊服務後才發現

- **檔案:行**：`lgwctl/migrate.py:991-1016`（preflight 沒載 policy）、`lgwctl/upgrade.py:49-63`（upgrade 已有 `_dry_run_policy`，用新 tag 的 loader）、`lgwctl/migrate.py:1174-1183`（停舊之後才 start 新服務）；`lgwctl/release.py:23-34` 只保證 `policy/default.json` 檔案在，不保證載得起來。
- **問題**：`prepare`／`cutover` 都沒有像 upgrade 一樣對新 tag 做 policy dry-run。若新 tag 的 policy 壞掉（或新 tag 的 `lgwd.policy` 與政策格式不相容），cutover 會先停掉舊服務、啟動新服務，新服務在 `Gateway.__init__` 載 policy 時失敗 → list 非 200 → 停損回舊版。功能上安全，但這是一次可避免的中斷＋回滾，而且回滾本身有中-4 的狀態成本。
- **情境**：新 tag 改了 policy 欄位但有 typo；CI 沒蓋到（policy 檔是資料，不是程式）。切換窗口白停一次。
- **建議**：在 `prepare` 與 `preflight` 呼叫 `upgrade._dry_run_policy(layout, tag_dir, report)`（它只讀檔、用新 tag 的 loader，無副作用）；失敗就 fail，不排程。測試：`FAKE_GIT_BAD_POLICY` 或壞 loader 時 cutover 在停舊之前就 fail。

#### 中-8 cutover 最後的 `check` 又單次探遠端；與前面「重試 3 次」的保護不一致

- **檔案:行**：`lgwctl/migrate.py:1202-1205`（前面用 `_probe_remote_with_retry`）、`migrate.py:1217`（接著 `check.run`）、`lgwctl/check.py:190-201`（`_check_list_remote` 單次）、`lgwctl/probe.py:58-81`（單次 ssh probe，逾時 60 秒）。
- **問題**：中-4 的修正讓「啟動隧道後」的探針重試 3 次，但 cutover 緊接著跑的完整 `check` 又會做一次**單次**遠端探針；隧道只要在這一瞬間抖一下（或會合點慢），`check` 就回 1，`_stop_loss` 立刻回舊版。這與前一步的保護矛盾，等於把中-4 的 false stop-loss 風險從第一步移到第二步。
- **情境**：新隧道剛 active、第一次 probe 成功；幾秒後 ssh 連線短暫重連（ServerAlive／網路），`check` 的 probe 失敗 → 切換被判定失敗。實際上隧道 10 秒後就恢復。
- **建議**：既然前面已用重試確認過遠端，最後的 `check` 用 `no_remote=True`（或把已確認的 remote 結果傳給 check），不要重探；若要重探，就套用同一個「3 次、間隔 10 秒」的 helper。測試：第一次 probe 成功、`check` 的 probe 失敗時不應停損（或反過來明確定義）。

#### 中-9 上一輪中-5 的 symlink 防護是 TOCTOU：`lstat` 之後才用 `copy2`／`sqlite3.connect`／`open` 重開路徑

- **檔案:行**：`lgwctl/migrate.py:421-430`（`_regular_file` 用 `os.lstat`，`_refuse_symlink`）、`migrate.py:268-286`（`_copy_into_place` 用 `shutil.copy2(src, tmp)`，會跟 symlink）、`migrate.py:297-307`（`sqlite3.connect(src)` 用路徑重開）、`migrate.py:870-874`（`parse_env_file(old.keysync_env_file)` 用路徑重開）、`migrate.py:785-806,814-826,834-862,906-915`（都是「先檢查、後重開」）。
- **問題**：舊檔在 `~llmgw`（服務帳號可控制）。`_regular_file()` 回 true 之後、`copy2`／`sqlite3.connect`／`open` 之前，llmgw 可以把該路徑換成指向任意 root 可讀檔的 symlink；root 就會把那個檔的內容複製進 llmgw 可讀的新 state（`keys.env`、`llm.db`、`tunnel.env`、`llmgateway.env`、`.keysync.sha`；`keys.pass` 只會取檔內的 `LLMGW_KEYS_PASSWORD=` 一行，但若目標檔有這一行，值也會被交給 llmgw）。上一輪的中-5 修了「直接是 symlink」的簡單情況，但沒有用 `O_NOFOLLOW` 或同一個 fd，所以提權路徑仍在，只是需要競態。對「被入侵的 llmgw」這個既有威脅模型，這是實際可嘗試的。
- **情境**：llmgw 被拿下後，反覆把 `~/.local/state/llmgateway/keys.env` 在一般檔與指向 root 可讀檔的 symlink 之間切換；本人跑 `prepare-migration` 時比賽，贏一次就把該檔交給 llmgw（新 `keys.env` 0600 llmgw）。`llm.db` 版本還要求目標是合法 SQLite 且有 `integrity_check`，較難但並非不可能（例如其他 root 可讀的 SQLite db）。
- **建議**：把「開舊檔」收成一個 helper：`fd = os.open(path, O_RDONLY | O_NOFOLLOW)` → `fstat` 確認一般檔 → 從 fd 讀／複製（`shutil.copyfileobj`）；SQLite 可用 `/proc/self/fd/<fd>` 或先 `O_NOFOLLOW` 複製到 root-only 暫存檔再 `backup`。`keysync.env` 也走同一條。測試用「檢查後才換成 symlink」的鉤子重現（或至少斷言 helper 用 `O_NOFOLLOW`）。

#### 中-10 在舊 user 版機器上誤跑 `install`：新服務會先起來（空 db），`prepare` 之後又跳過複製，造成資料遺失且 install 假成功

- **檔案:行**：`lgwctl/install.py:272-289`（start 後只用本機 list 200 確認）、`install.py:357-404`（install 流程會 enable＋start 新服務）、`lgwctl/migrate.py:809-813`（`_step_db` 看到目標 db 已存在就 skip）、`deploy/units/llmgateway.service:19`（新服務的 `--db /var/lib/llmgateway/llm.db`）。
- **問題**：舊機器上舊的 user `lgwd` 仍佔著 8480。若操作者把「安裝新版」理解成先跑 `install --tag`（而不是 `prepare-migration`），新 system 服務啟動時會 bind 失敗（或 crash-loop）；`_step_service` 的 `probe.wait_local_ok` 若在 start 被 systemd 接受之後執行，會打到**舊服務**的 200，於是 install 可能印 `ok start`；即使 start 直接失敗，`lgwd` 在建 `Store` 時也已經在 `/var/lib/llmgateway/llm.db` 建立空 schema。接下來跑 `prepare`，`_step_db` 看到 db 已存在就 `skip`，舊的 audit／cooldown 不會被複製；cutover 後新服務就用空 db 服務。舊 db 還在舊 state，且 rollback 會先備份，但這是可避免的資料遺失，而且 install 的「成功」可能是假的。
- **情境**：操作者第一次接觸新指令，照舊手冊的精神先「install 新 tag」；或 install 在 prepare 之後被重跑（也會先把新服務起來，與舊服務搶 8480，並讓 probe 打到舊服務）。
- **建議**：(1) `install` 在 start 前偵測舊 user `llmgateway.service`／`llmgateway-tunnel.service` 是否 active（用 `runuser -u llmgw -- systemctl --user is-active`），是就拒絕並提示「這是遷移，請用 prepare-migration」；(2) `prepare` 若發現新的 `llm.db` 已存在、但舊 `llm.db` 也存在且 `phase` 不是 confirmed／finalized，要求 `--force` 或明確警告「目標 db 可能是新服務自己建的，會被跳過」；(3) `install`／`upgrade` 的 list 探針無法區分是不是新服務在回答，至少要用 `systemctl is-active` 確認新 unit 真的 active（install 已有 `Type=exec`，但仍可能 crash-loop；可在 probe 前檢查 `unit_state`）。

#### 中-11 回舊版後 `key-sync.timer` 只 enable、沒有 start：舊版的 key 輪替停到下次重開機

- **檔案:行**：`lgwctl/migrate.py:1550-1572`（`_start_old_units` 對 `OLD_UNIT_NAMES` 全部 `enable`，但只 `start` `OLD_START_ORDER`＝service／tunnel）、`migrate.py:105-108`（`OLD_START_ORDER` 沒有 timer）、`ops-scripts/key-sync.timer:8-10`（`OnActiveSec=10min`、`OnUnitActiveSec=10min`）；commit `ea0f965` 的訊息說「回舊版時也 enable key-sync.timer（不然舊版的 key 不會再拉）」。
- **問題**：cutover 會 stop＋disable 舊的 `key-sync.timer`；`rollback-to-user` 只把它 `enable` 回來，**沒有 `start`**。`enable` 只建立開機時的 wants symlink，不會讓 timer 立刻開始計時；所以在回舊版後，舊的 key-sync 不會再跑，直到下一次重開機（或手動 start）。觀察期可能長達 3 天，期間私鑰輪替完全停止；`check` 只看新的 system unit／新的狀態檔，不會提醒。反向保險觸發的 rollback 也走同一條路。
- **實測**：沙箱 rollback 後 actions 只有 `user enable key-sync.timer`，沒有 `user start key-sync.timer`，`active` 為 false。
- **情境**：切換失敗自動回舊版（或觀察期手動 rollback），舊服務正常服務但 key 不再更新；三天後 private repo 輪替金鑰，舊服務仍用舊 key。
- **建議**：`_start_old_units` 在 enable 後對 `key-sync.timer` 明確 `systemctl --user start key-sync.timer`（若已在跑，start 回 0），並在輸出寫一行；測試斷言 rollback 的 actions 包含 start timer（目前 `tests/test_lgwctl_migrate.py` 沒斷言）。中止路徑的 `_restore_old_units` 因為 timer 在 `stopped` 清單裡已有 start，可順便統一成同一個 helper。

---

### 低（13）

#### 低-1 `/etc/llmgateway` 目錄權限沒有校正；root umask 嚴格時 `keys.pass` 群組讀不到，`sync` 會失敗

- **檔案:行**：`lgwctl/install.py:180-198`（`makedirs` 後只 chmod state／ssh，`opt_dir` 0755；沒有 chmod `etc_dir`）、`lgwctl/migrate.py:718-721`（prepare 也用 `_step_dirs`）、`lgwctl/sync.py:27-28`（以 llmgw 身分 `. keys.pass`）。
- **問題**：`keys.pass` 是 root:llmgw 0640，但 llmgw 要能**穿過** `/etc/llmgateway`。`os.makedirs` 用 `0777 & ~umask`；root 若用 077（或目錄已存在且是 0700），llmgw 進不去，`sudo -u llmgw sh -c '. keys.pass'` 會 exit 3（沒有有用的訊息），`llmgwctl sync` 失敗。單元本身用 `LoadCredential`（systemd 以 root 讀）不受影響，所以只有手動 sync 受影響。
- **情境**：hardened 機器 root umask 077，或管理員把 `/etc/llmgateway` 設成 0700；install 說 ok，sync 卻讀不到密碼。
- **建議**：`_step_dirs` 明確 `os.chmod(layout.etc_dir, 0o755)`（必要時 chown root:root）；或在 `_step_config` 檢查目錄的 o+x／g+x 並修正。補測試：目錄先設 0700，install 後變 0755。

#### 低-2 `_start_old_units` 把 `enable` 的 missing 當成功；舊 unit 檔不在時會印「開機會自動起」

- **檔案:行**：`lgwctl/migrate.py:1557-1564`（`outcome == "failed"` 才算失敗，`missing` 走 else 印 ok）、`lgwctl/migrate.py:674-681`（`disable`／`enable` 的 missing 語意）。
- **問題**：`systemctl --user enable` 對不存在的 unit 會回「Unit file … does not exist」，`_judge` 判成 `missing`；`_start_old_units` 把它當 ok。若舊 unit 檔已被 `finalize` 搬走（或管理員刪掉），rollback 會先停掉新版，再宣稱舊版「開機會自動起」，實際上 start 會失敗。這會誤導操作者，也讓「先停新再起舊」的順序更危險（見中-4）。
- **情境**：finalize 後誤跑 rollback，或舊 unit 檔被清掉；輸出先 ok 再 fail，操作者可能只看前面的 ok。
- **建議**：`missing` 在 `_start_old_units` 應算 fail（或至少 todo 並在停新 unit **之前**就檢查）；`rollback-to-user` 的 preflight 先確認四個舊 unit 檔存在，不存在就拒絕，不要動新 unit。

#### 低-3 `rollback-to-user` 可能有 `fail` 行（db 備份／複製、keys.env）卻回 0

- **檔案:行**：`lgwctl/migrate.py:1405-1427`（回傳值只看 `_wait_old_list` 與 `_start_old_units`）、`migrate.py:1474-1489`（db 備份失敗只 `report.fail` 後 return）、`migrate.py:1541-1546`（keys.env 複製失敗只 fail）。
- **問題**：`_restore_db` 的 `db-bak` 失敗（不覆蓋舊 db）或 `_restore_keys_env` 失敗時，舊服務仍可能起來、list 200，函式回 0；自動化或操作者若只看結束碼，會以為資料也回去了。`report` 裡有 fail 行但 rc 0。
- **情境**：磁碟滿／權限壞導致舊 db 備份失敗；rollback rc 0，舊 db 沒被新 db 覆蓋（其實是安全的一邊），但操作者不知道。
- **建議**：回傳 `report.exit_code(todo_ok=True)`（或至少 `1 if report.has_fail else ...`），讓 fail 行反映在 rc；若要維持「舊服務起來就算成功」，在輸出與文件明說資料複製的成敗不影響 rc。

#### 低-4 `finalize` 沒拿 flock；`prepare` 的 `write_state` 也沒鎖，與 `write_state` 的註解不一致

- **檔案:行**：`lgwctl/migrate.py:482-494`（`write_state` 說「互斥由呼叫方的 `_Lock` 負責」）、`migrate.py:1588-1675`（finalize 全程沒有 `_Lock`）、`migrate.py:746-751`（prepare 直接 `write_state`）。
- **問題**：confirm／cutover／rollback 都在鎖內寫狀態，但 finalize 與 prepare 沒有。若 finalize 與 rollback（或殘留的保險）同時跑，兩邊會 read-merge-write 同一個狀態檔，後寫者蓋掉先寫者；finalize 也可能在 rollback 停新 unit 的同時搬舊目錄。機率低（人手兩個指令同時跑），但這是狀態檔互斥的破口。
- **情境**：操作者在 finalize 的 3 天條件跑過的同時，另一個 session 執行 rollback-to-user；狀態與檔案搬移交錯。
- **建議**：finalize（與 prepare 的狀態寫入）也拿同一把 `_Lock`；`write_state` 改成 compare-and-swap（帶入預期 phase）或至少全部呼叫端都在鎖內。

#### 低-5 `finalize` 只探本機 list 200 就開始搬舊版；沒有確認隧道／keysync 也健康

- **檔案:行**：`lgwctl/migrate.py:1626-1634`（只 `probe.wait_local_ok`）、`migrate.py:1649-1674`（搬舊目錄／unit、disable-linger）、`lgwctl/check.py:190-201`（完整 check 已有 remote／keysync／gh）。
- **問題**：finalize 是不可逆的收尾（舊 unit 檔被搬走後，rollback-to-user 起不來）。目前只看本機 200；如果新的**隧道**壞了、或 key 拉取已經失敗，仍然會把舊版收掉。上一輪低-3 建議「先跑一次 check（或至少本機 list）」，實作只做了括號裡的最小版本。
- **情境**：新服務本機正常，但新隧道的 ssh 設定壞掉（例如會合點換 key），finalize 照樣收舊版；之後遠端管理與 API 都不通，只能 console。
- **建議**：finalize 前跑 `check.run`（含 remote；若人在 console 可 `--no-remote` 但要有明確旗標）並要求 rc 0；至少要求 tunnel unit active 且 `keysync` 不是 fail。測試可用 remote probe 失敗時斷言不搬。

#### 低-6 `_keys_env_valid` 只驗鍵名白名單，沒擋重複鍵；舊 `KeyStore` 會拒收

- **檔案:行**：`lgwctl/migrate.py:384-418`（逐行驗 `name in allowed`、至少一個有值；沒有 duplicate 檢查）、`lgwd/keys.py:37-40`（`KeyStore._parse` 對重複鍵丟 `KeyFileError`）。
- **問題**：回滾覆蓋舊 `keys.env` 前會用 `_keys_env_valid` 驗新的；若新檔有重複鍵（人工貼上兩次），這裡會放行，覆蓋後舊服務的 `KeyStore` 拒收 → 回舊版起不來，正好打在最不能壞的一步。上一輪低-7 的建議是用真正的 `KeyStore` 驗；目前只做了白名單，少數規則（重複、空值後的重複）仍不一致。
- **情境**：新的 key-sync／人工在新 keys.env 留下兩行同一個 `CLAUDE_PERSONAL_OAUTH_TOKEN`；回滾時複製回舊 state，舊服務啟動失敗。
- **建議**：用 `lgwd.keys.KeyStore` 在暫存檔上真的載入一次（或補 duplicate 檢查），與 `_parse` 完全一致；測試補「重複鍵」案例。

#### 低-7 `_restore_db` 沒有 `new_service_ts` 時用 mtime 比較；prepare 的快照 mtime 是「建立時間」，可能被誤判成較新

- **檔案:行**：`lgwctl/migrate.py:1493-1507`（沒有 `new_service_ts` 就比 mtime）、`migrate.py:322-353`（`_copy_db_consistent` 的暫存檔 mtime 是 prepare 當下）、`migrate.py:1458-1490`。
- **問題**：在「prepare 完、還沒 cutover」就手動 `rollback-to-user` 的情境，state 沒有 `new_service_ts`，fallback 會拿新 db（prepare 快照，mtime＝prepare 時間）和舊 db 比；只要舊服務在 prepare 後沒有寫入，快照的 mtime 就比較新，於是回滾會把 prepare 時的快照覆蓋回舊 state，蓋掉 prepare 之後舊服務寫的 audit／冷卻（有 `.bak-*` 可救，但要人工）。這是中-6 修法在有 state 時正確、沒 state 時的殘留。
- **情境**：prepare 後決定取消遷移，跑 rollback；舊 db 被換成 prepare 時的快照。
- **建議**：沒有 `new_service_ts` 時不要覆蓋（skip 並說明），或在 prepare 時把 snapshot 的來源 mtime／digest 寫進 state，回滾時只在新 db 真的被新服務寫過（可用 `.lgw-tag`＋系統 unit active 判斷）才複製。

#### 低-8 `check` 把 `.keysync.status` 的 `reason` 原樣印出；該檔在 llmgw 可寫目錄

- **檔案:行**：`lgwctl/check.py:121-123`（`reason = str(data.get("reason") or "unknown")` 直接進輸出）、`lgwd/keysync.py:58-64,118-128`（writer 有白名單）、`lgwctl/install.py:141-143`（state dir 0700 llmgw）。
- **問題**：T2 的低-4 修了 **writer**（lgwd）不把任意字串寫進狀態檔，但 `check` 仍信任檔案內容並原樣印出。狀態檔在 llmgw 擁有的 0700 目錄，被入侵的 llmgw 可以寫入任意 `reason`（含 ANSI escape、終端控制字元，甚至它已知的 key 值），root 跑 `llmgwctl check` 時會把它印到終端／log。威脅模型裡 llmgw 本來就能讀 key，所以不是新外洩，但違反「輸出絕不含值」與終端安全。
- **情境**：llmgw 被拿下，寫 `{"ok":false,"reason":"…\u001b[…"}`；操作者跑 check 看到被操縱的輸出。
- **建議**：`check` 對 `reason` 套同一個白名單（`ok`／`timeout`／`unknown`／`exit_N`／`signal_N`／`manual_sync rc=N` 等），不認得就顯示 `unknown`；並移除控制字元／限制長度。測試：狀態檔塞 `reason` 含假值／escape 時輸出不含它。

#### 低-9 `prepare` 對已填值的 `keys.pass` 直接 skip，不會順便修正 owner／mode

- **檔案:行**：`lgwctl/migrate.py:865-869`（`_has_password(dst)` 就 skip，沒有 `_chmod`／`_chown_keys_pass`）、`lgwctl/install.py:224-233`（install 每次都會校正）。
- **問題**：若 `keys.pass` 是人工建立、owner 是 root:root 0600（或舊版 install 留下的 llmgw:llmgw），prepare 看到有值就跳過，不修正；結果 `llmgwctl sync` 以 llmgw 身分讀不到（root:root 0600）或權限不對。install 的路徑會修正，prepare 不會，兩支工具不一致。
- **情境**：本人先手動寫 keys.pass 再跑 prepare；或從舊版升上來，keys.pass 是舊權限。
- **建議**：skip 分支也呼叫 `os.chmod(0640)` 與 `_chown_keys_pass`（失敗就 fail），和 install 對齊。

#### 低-10 目標是斷掉的 symlink 時，`_step_env_file`／`_step_db` 會當成「已存在」跳過

- **檔案:行**：`lgwctl/migrate.py:264-265`（`_exists` 用 `lexists`）、`migrate.py:829-837`（`_step_env_file` 的 `if _exists(dst) and not force`）、`migrate.py:809-813`（`_step_db`）。
- **問題**：上一輪低-1 修了 `keys.env` 的 `getsize` traceback，但其他目標（`llm.db`、`tunnel.env`、`llmgateway.env`）只要是指向不存在路徑的 symlink，`lexists` 就回 true，prepare 會 skip 並留下斷鏈。之後 systemd 的 `EnvironmentFile`（`llmgateway.service`）或新服務開 db 時會失敗／把資料建到別的地方。
- **情境**：某次搬移／回滾留下 `/etc/llmgateway/tunnel.env` 或 `/var/lib/llmgateway/llm.db` 的斷鏈；prepare 說「已存在（不覆蓋）」，切換時 unit 起不來。
- **建議**：對目標也用「一般檔（非 symlink）且大小>0」判斷；斷鏈視為不存在並替換（或明確 fail 請本人處理）。測試補 `llm.db`／`tunnel.env` 斷鏈。

#### 低-11 `install` 重跑時若 keys.pass 已填、服務已在跑，不會 restart 也不會提醒

- **檔案:行**：`lgwctl/install.py:341-347`（只有「還沒填」才印 `todo keys-pass … 再 restart`）、`install.py:272-275`（服務 active 就 `ok start（不動它）`）、`lgwctl/sync.py:81-83`（restart 提示只在 sync）。
- **問題**：本人 install → 填 keys.pass → 再跑一次 install（很自然的冪等確認）：`_step_todos` 看到有值印 `ok keys-pass`，`_step_service` 看到 active 不 restart；但 `LoadCredential` 是啟動時的快照，服務內的 key 拉取仍用舊的（沒有密碼）快照，直到有人 restart。install 的輸出沒有任何提示，下一個 10 分鐘週期會把 `.keysync.status` 蓋成 `ok:false`，check 顯示 key 拉取失敗。
- **情境**：install 後填密碼、重跑 install 驗證；以為好了，實際 key 拉取一直失敗。
- **建議**：服務 active 且 `_keys_pass_filled` 為真時，印一行 `todo restart（keys.pass 是啟動快照，填／改完要 restart）`，或偵測 keys.pass mtime 晚於服務啟動時間（可用 `systemctl show -p ActiveEnterTimestamp`）就 restart；文件也寫明。

#### 低-12 `sync` 的結束碼只看 key-sync，狀態檔寫失敗仍回 0

- **檔案:行**：`lgwctl/sync.py:78-90`（成功路徑 `report.ok` + `todo restart`，`problem` 只 `report.fail`，最後 `return proc.returncode`）。
- **問題**：key-sync 成功但 `.keysync.status` 寫不進去（磁碟滿、權限）時，輸出有 `fail status`，結束碼卻是 0；自動化會以為 sync 完整成功，`check` 之後仍看到舊狀態。
- **情境**：state 目錄暫時滿了；sync 回 0，check 說 keysync 過期。
- **建議**：`problem` 不為 None 時回非零（或至少 `report.exit_code(todo_ok=True)`），同時保留 key-sync 的 rc 資訊在輸出。

#### 低-13 `confirm` 的「保險是否已開跑」與「停 timer」之間仍有毫秒級 race；`failed` 的保險服務不算 busy

- **檔案:行**：`lgwctl/migrate.py:1354-1371`（先 `_system_unit_busy`，再 `_cancel_revert`，再寫 confirmed）、`migrate.py:632-645`（只看 active／activating／reloading；`failed` 不算）。
- **問題**：timer 可能在 busy 檢查之後、`stop timer` 之前觸發，`lgw-revert.service` 開始跑並等在 flock（confirm 持有）；confirm 寫下 `confirmed` 後釋放鎖，該 service 進入 `_rollback_locked`，看到 phase 不是 reverted 就照樣回滾，最後把 `confirmed` 蓋成 `reverted`。窗口很小，但「已 confirm 卻被回滾」是違反語意的。另一個小問題：保險服務若已是 `failed`（跑到一半失敗），`_system_unit_busy` 回 false，confirm 可能在半回滾狀態下通過（會再檢查新服務 active／舊 user 不 active，多半會擋下）。
- **情境**：操作者在保險到點前幾毫秒按 confirm；或保險服務失敗後殘留。
- **建議**：confirm 在停 timer 後**再**檢查一次 `lgw-revert.service` 不在 active／activating／reloading（並考慮 `failed` 且 state 不是 reverted 時要求人工確認）；或直接 `systemctl stop lgw-revert.service` 再寫 confirmed。`write_state` 用 compare-and-swap 確認 phase 仍是 pending。

---

### 資訊（7）

#### 資訊-1 測試與版本

`python3 -m unittest discover -s tests`：`Ran 519 tests in 202.305s` → `OK (skipped=1)`（Python 3.12.3）。skip 的是 root-only 的真 chown 測試（`tests/test_lgwctl_install.py:353`），在非 root 的這台機器上預期會跳過。這與提示的期望 519／skipped=1 一致；上一輪的 404／412 都已包含在內。

#### 資訊-2 保險「真的到點後自動回滾」仍沒有自動化覆蓋

`tests/test_lgwctl_migrate.py` 的假 `systemd-run`（`:117-127`）只記下要跑的指令，不真的排程；測試是用「再叫一次 `rollback-to-user`」模擬保險到點。這代表 `lgw-revert.timer` → `lgw-revert.service` → `rollback-to-user` 的實際 systemd 行為（包含中-2 的鎖競爭）只有 Lima 演練能驗。上一輪資訊-2 已提過，這次仍未加；既然中-2 是新的風險，演練清單應加一條「切換故意拖過 revert-after，確認保險的結果與狀態」。

#### 資訊-3 假 user systemctl 不模擬 rc 4／unit 檔不存在，`_start_old_units` 的 missing 路徑測不到

`tests/test_lgwctl_migrate.py:59-115` 的假 systemctl 對 `is-active` 只回 0／3，對 `enable`／`start` 也不看 unit 檔是否存在；所以**新-中4** 的「finalize 後 rollback」與低-2 的 missing→ok 在沙箱裡不會失敗（我實測 finalize 後 rollback 回 0、舊 timer 仍是 inactive）。真機的 systemctl 會回 rc 4／"does not exist"，行為不同；這是測試替身與真實 systemd 的差異，不是程式錯，但會讓上面兩項在 CI 綠燈。

#### 資訊-4 transient unit 只轉送三個 `LLMGW_*`；其他測試旋鈕（等待秒數、假指令路徑）不會進 detached unit

`lgwctl/migrate.py:138-139`（`TRANSIENT_ENV_NAMES`）只轉 `LLMGW_PREFIX`／`LLMGW_SERVICE_USER`／`LLMGW_SERVICE_HOME`；`LLMGW_CUTOVER_WAIT`、`LLMGW_KEYSYNC_GRACE`、`LLMGW_REMOTE_TRIES`、`LLMGW_SYSTEMCTL` 等不會過去。真機不需要這些（用預設與真的 systemctl），但**演練**若想用 prefix 隔離＋縮短等待，detached 的 inner 會用預設 60／90 秒，與外層的行為不同。建議演練腳本把要用的變數放進 systemd 的環境（或寫清楚只用預設）。

#### 資訊-5 `docs/deploy.md` 仍是舊 user 層流程

本分支的 `docs/deploy.md` 仍寫 `~/.config/systemd/user/`、`~/.local/share/llmgateway`、`install.sh --tag`、`key-sync.timer`（見檔頭與 §0 表格），沒有 `llmgwctl`、`/opt/llmgateway`、`prepare-migration`。如果 T4 的「docs/deploy.md 改寫」不在這個整合分支的範圍，請確認它會在真機切換前落地；否則操作者照文件會走舊路徑（也與新-中10 的誤用風險相關）。

#### 資訊-6 值與密碼的外洩檢查：本輪沒有發現新外洩

重看 `lgwctl/report.py`、`run.py`、`check.py`、`install.py`、`sync.py`、`migrate.py` 的輸出／argv／例外：`prepare` 只印路徑、owner、「值不印出」；`sync` 的密碼只在 `sh -c` 的環境（`keys.pass` source 後）與 key-sync 的 `-pass env:`，不進 argv；狀態檔只有 `ts/ok/reason`；`check` 的 detail 只有碼、年齡、名稱（除了低-8 的 `reason` 原樣印出）；例外只印 `type(exc).__name__`。T2 的 writer 白名單與測試的 `assert_no_values` 仍在。**低-8 是輸出衛生的殘留，不是新的值外洩。**

#### 資訊-7 `llmgateway-tunnel.service` 沒有 hardening；`llmgateway.service` 有規格要求的那一組

`deploy/units/llmgateway-tunnel.service:1-17` 只有 `User=llmgw`、`Restart=always`、`RestartSec=10`、`RestartPreventExitStatus=78`、`Environment=LGW_TUNNEL_ENV`，沒有 `NoNewPrivileges`／`ProtectSystem`／`PrivateTmp`；規格 §4.1 的加固只點名 `llmgateway.service`，所以不算違規，但隧道也吃 ssh 與 `~/.ssh`，日後可以考慮同樣的加固（尤其是 `NoNewPrivileges`）。本輪不當缺陷。

---

## 三、結論

上一輪的 5 個高（T1 高-1、T4 高-1～4）與 16 個中，**20 項已修、1 項部分（T4 中-5 的 symlink TOCTOU）**：版本目錄權限、舊 unit disable、狀態檔搬到 root 專屬目錄、SQLite backup API、transient 環境轉送、flock、pre-flight、隧道路徑重試、confirm 的保險檢查，都有對應程式與測試；T2／T3 的低項也補齊。測試 519 個全過（skipped=1）。

但整合後仍有 **1 個新高**：`rollback-to-user` 會停掉自己正在走的隧道，SSH session 在「新服務已停、舊服務還沒起來」之間被切斷；在設計指定的隧道管理情境下，這是觀察期回滾的必經路徑，可能讓 API 與管理通道同時下線。另有 11 個中，集中在切換窗口的時間／保險（中-1、中-2、中-5、中-8）、手動 rollback 的 unit 與狀態機（中-3、中-4）、pre-flight 缺 gh／policy（中-6、中-7）、symlink TOCTOU（中-9）、誤跑 install 的資料風險（中-10）、回舊版後 key 輪替停止（中-11）。這些不是風格問題，而是真機切換與回滾會遇到的行為；依本人「高、中都修完才切」的條件，**這一輪不能合併／不能切真機**。

**數量**：新發現 高 1、中 11、低 13、資訊 7；上一輪高／中核對：已修 20、部分 1（T4 中-5）。另有 3 個已修項的殘留已另列新問題：T1 中-2 → 新-中3（manual rollback 的 unit）、T4 高-2 → 新-中5（重開機窗口）／新-中11（回舊版 timer）、T4 中-3 → 新-中2（保險被鎖死）。

結論：需修
