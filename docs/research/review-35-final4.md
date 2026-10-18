Claude Sonnet 5.5（claude-sonnet-5-5）

# #35 第 4 輪複核（分支 review/final-35d，HEAD dcab45c，基準 f7f8959）

範圍：作者 5 個修正 commit（bc1e95b、b2f8505、1a960f8、e72d861、dcab45c）的程式、測試、`docs/deploy.md`；對照 `review-35-final3.md` 與作者報告 `agy-mig6.md`。
測試：`python3 -m unittest discover -s tests` → `Ran 563 tests in 78.726s` → `OK (skipped=1)`。
威脅模型同上輪：服務帳號 `llmgw` 可能被攻破；prepare／cutover／rollback／finalize 以 root 跑。

## 上一輪項目核對

| 項目 | 結論 | 依據 |
|---|---|---|
| 高-1 root chmod／chown 跟 symlink | **大致修對；寫入側上層目錄仍有缺口（見中-1）** | `atomic.write_bytes`、`_copy_into_place`、`_copy_db_consistent` 改為 rename 前對暫存檔 fd `fchmod`／`fchown`；`_step_dirs` 先 `lstat` 拒 symlink、再 `O_DIRECTORY\|O_NOFOLLOW`＋`fchmod`；`_chown_service`／`chown_root_group`／`_chmod_quiet`／`_step_keys_pass` 改 fd。原本的攻擊（`~llmgw/.ssh` 換成 symlink → chmod／chown `/etc`）確實擋住，且有測試（`.ssh` 是 symlink → fail、目標權限不變，真的建 symlink，不是 mock）。殘留：`_chown_service`／`chown_root_group` 在 open 失敗且「非 ELOOP」時退回路徑式 `os.chown`（為了相容 mock 而加）；`_copy_into_place`／`_copy_db_consistent` 的**寫入目的地**沒有上層元件檢查（中-1）；prepare 的 keys.env／llm.db 來源複製沒傳 `base`（低-2）。 |
| 中-1 prepare 階段守門 | **修對** | `_prepare_locked` 在持鎖後、任何動作前，`phase in OPEN_PHASES` 即 fail（含 `--force`、換 tag）；測試用真檔驗證 keys.env／tunnel.env 沒被覆蓋。 |
| 中-2 cutover 重新快照 | **修對** | `_sync_final_cutover_data` 在 `stopped-check` 之後、`(c)` 啟新之前（舊服務已停、無寫入者）；`llm.db` 走 `_copy_db_consistent`（backup API＋integrity_check＋重試，原子 rename，owner 在 rename 前設）；`keys.env`／`.keysync.sha` 僅在 mtime 較新時覆蓋（prepare 複製時保留 mtime，所以相等不會誤覆蓋）。失敗 → `_restore_old_units`＋取消保險＋`aborted`（舊 unit 回復）。快照發生在 `cutover_started = time.time()` 之前，所以回滾時 `_db_has_new_writes` 不會把這份快照誤判成「新服務寫過」。測試用真 SQLite 檔驗證標記帶過去。**未涵蓋**：失敗回復路徑沒有測試（只測成功）。 |
| 中-3 preflight 舊 unit 檔 | **修對** | `preflight(…, old=old)` 檢查 4 個舊 unit 檔存在且非 symlink 一般檔，outer／inner 都跑；缺檔在停舊之前拒絕；測試涵蓋（舊服務沒動、`order()` 為空）。 |
| 中-4 舊隧道穩定判定 | **判定本身有進步，但與實際埠競爭矛盾，正常回滾路徑會被它擋死（見中-2）；穩定窗口也偏短** | `_wait_old_tunnel` 加 `NRestarts` 與連續 `active` 窗口（預設 5 秒，`LLMGW_TUNNEL_STABLE`）。crash-loop 的一般樣態（active 1～3 秒 → 重啟等待 10 秒）確實過不了。測試只 mock `_user_unit_restarts`，不涉真 systemd／埠。 |
| 低-1 deploy.md | **大致一致；2 處仍不符（見低-4）** | 其餘（歸檔路徑、`status` 內容、`--old-home`／`--no-remote`／`--force`／`--revert-after` 旗標、preflight 條件、停損順序與時限、`LoadCredential` 重啟、`useradd -m -U`）逐一對照 `migrate.py`／`install.py` 一致。 |
| 低-2 上層目錄元件 | **部分修** | `_has_symlink_component(path, base)` 逐層 `lstat` 到 `base`（不含）；`_open_regular`／`_regular_file` 用它。只有檢查（TOCTOU，見低-2）且只用在讀取側與「目的地已存在」時。 |
| 低-3 src_fd 洩漏 | **修對** | `mkstemp` 移進 `try`；測試只驗丟例外，不驗 fd（弱，但程式正確）。 |
| 低-4 SQLite trusted_schema | **修對** | 所有連線加 `PRAGMA trusted_schema=OFF`；測試只驗「SQLite 本來就支援這個 pragma」，沒驗 migrate 的連線真的設了（弱）。 |
| 低-5 不叫回新服務 | 作者不修（理由合理） | 接受，不擋放行。 |

---

## 高（0）

無。

## 中（2）

### 中-1 寫入側的上層目錄元件仍跟 symlink：回滾時 root 可被導去寫別的目錄（高-1 殘留）

- **檔案:行**：`lgwctl/migrate.py:2090-2108`（`_restore_db`：`_regular_file(dst, old.home)` 只在 `os.path.isfile(dst)` 時才跑；dst 不存在就直接 `_copy_db_consistent(src, dst, owner=…)`，且沒傳 `base`）、`:2152-2170`（`_restore_keys_env` 同型）、`:405`／`:571`（`os.makedirs(directory, exist_ok=True)` 跟 symlink）、`:615`（`_prune_db_backups` 的 `os.unlink`）。
- **問題**：作者修的是「最後一段」與「目的地已存在」兩種情況。若 llmgw 把 `~llmgw/.local/state/llmgateway`（或 `.local`、`.local/state`）換成 symlink 指向一個 root 擁有的目錄、且該目錄裡沒有 `llm.db`／`keys.env`，`rollback-to-user` 就會：`makedirs` 通過、`mkstemp` 在**目標目錄**建暫存檔、`fchown` 給 llmgw、`os.replace` 成 `llm.db`／`keys.env`／`llm.db.bak-*`，並刪除 `llm.db.bak-*` 舊備份。檔名固定、內容是 llmgw 自己的資料，所以不是直接拿 root；但等於「root 在任意 root 擁有的目錄放一個 llmgw 擁有、內容受控的檔」，並覆蓋同名檔。我沒找到可直接利用的目標目錄（`sudoers.d`／`cron.d`／`profile.d` 都因檔名含 `.` 或副檔名被忽略），所以列中不列高；但這就是你問的「上層目錄元件也擋」，沒有完全做到。
- **建議**：寫入前對 `old.state_dir` 做一次與讀取側相同的 `_has_symlink_component(dst_dir, old.home)`（目的地是否存在都檢查），並把 `base=old.home` 一路傳進 `_copy_db_consistent`／`_copy_into_place` 的寫入路徑；更穩的做法是以 `O_DIRECTORY|O_NOFOLLOW` 開 `old.home` 後逐段 `openat`。補一個測試：`state` 目錄是 symlink 且 dst 不存在時，rollback 不寫任何檔。

### 中-2 回滾時新隧道還佔著會合點埠，舊隧道一定 crash-loop：新的「穩定判定」使正常的回滾也永遠過不了，新隧道不會被停，回滾 rc=1

- **檔案:行**：`lgwctl/migrate.py:2009-2014`、`:2217-2271`（`_wait_old_tunnel`）、`ops-scripts/llmgateway-tunnel`（`-R REMOTE_API:127.0.0.1:LOCAL_API`、`ExitOnForwardFailure=yes`；新舊都用 tunnel.env，預設 4710／4711）、`docs/deploy.md` 步驟 4 的順序描述。
- **問題**：`_rollback_locked` 的順序是「啟舊隧道 → `_wait_old_tunnel` → 之後才 `_stop_new_tunnel`」。但新隧道（system 層）這時還活著、持有會合點的 4710／4711，舊隧道的 `ssh -R` 同埠必定因 `ExitOnForwardFailure` 結束，每 10 秒重試一次，直到新隧道被停才可能起來。上一輪我指出「舊隧道靠運氣才過」；現在改成連續穩定判定，運氣沒了，**這個把關在新隧道仍在時永遠不會成立**：停損或手動回滾 → `tunnel-old fail`、rc=1、新隧道留著、狀態 `tunnel_ok=false`；重跑（`reverted` 分支）同樣過不了。結果：安全（管理連線不斷，且新隧道會把 API 導到 8480，那時已是舊服務），但回滾**永遠需要人手 `systemctl stop／disable llmgateway-tunnel` 才收尾**，且新隧道仍是 enabled（之後重開機新舊隧道同時起、互搶埠）。文件把它寫成會自動收尾，與行為不符。
- **情境**：保險到點的回滾：舊服務回來、list 200，隧道 30 秒（預設）等不到穩定 → fail、新隧道留著；PM 看到 `rc=1` 與「舊隧道起不來」的訊息，其實舊隧道沒壞，只是被擋。
- **建議**（沿用上輪的方向）：改成「舊服務 list 200 → 記錄舊隧道 `NRestarts` → 停新隧道（不 disable）→ 等舊隧道連續穩定 ≥ 15～20 秒且 `NRestarts` 不增、並以 `probe.remote` 回 200 → 成功才 disable 新隧道；失敗就 `start` 新隧道叫回來」。這樣管理連線只有數十秒空窗，且「起不來」時有回復路徑；或改設計成兩條隧道用不同的 remote 埠（較大改動）。至少 deploy.md 與錯誤訊息要如實寫「舊隧道要等新隧道停掉才可能起來」。
- **測試落差**：`test_rollback_keeps_new_tunnel_when_old_tunnel_crash_loops` 只 mock `_user_unit_restarts`；其他回滾測試的假 systemd 讓舊隧道恆 active，所以沒有任何測試涵蓋「新隧道佔著埠」。這是 mock 與真機行為分離的典型落點。

## 低（4）

### 低-1 `_user_unit_restarts` 吞掉所有例外：讀不到 `NRestarts` 時 crash-loop 偵測靜默失效

- `migrate.py:2217-2227`：`except Exception: return None`；呼叫端 `None` 時不做重啟比較，只剩 `is-active` 連續 5 秒。對 `systemd` 版本太舊（< 235）或 `--user` 連線有問題時，把關降級卻沒有任何輸出。建議讀不到時 `report.skip`／`todo` 一行。另預設窗口 5 秒偏短：遠端高延遲時 ssh 在 `ExitOnForwardFailure` 觸發前可能就已超過 3～5 秒；上輪建議 15～20 秒。

### 低-2 `_copy_into_place`／`_copy_db_consistent` 的 `base` 只有部分呼叫端有傳

- `_step_keys_env`（`:1120`）、`_step_db`（`:1142`）沒傳 `base=old.home`，只靠前面的 `_regular_file(…, old.home)`（檢查後才開檔，TOCTOU）；`_step_env_file`、`_sync_final_cutover_data` 有傳。上層目錄在檢查與開啟之間被換成 symlink 時，讀到同名的別處檔（檔名固定、內容有限，同上輪低-2）。建議統一都傳 `base`（`_open_regular` 內部已會檢查），或前面提的逐段 `openat`。

### 低-3 為了相容 mock 保留的退路與測試專用分支削弱修正

- `install._chown_service`／`chown_root_group`（`install.py:90-135`）在 `open` 失敗且非 symlink／ELOOP 時改走路徑式 `os.chown`；`migrate._open_regular(path)` 與 `(path, base=…)` 兩種呼叫只為了讓既有 mock 通過。實際上 root 下 `open` 失敗的非 ELOOP 原因很少，風險低；但這是「為測試放寬安全程式」的味道，建議改測試去 mock `os.open`／`os.fchown`，把退路拿掉。另：`O_RDONLY` 開 llmgw 可換成 FIFO 的路徑會永遠卡住（DoS，非提權）；建議加 `O_NONBLOCK` 再 `fstat` 驗型別（`_open_regular` 有驗型別，`_chown_service` 沒有）。
- `_prepare_locked` 現在在 `_step_account` 之前就 `_start_new_round`（`:1037-1038`），結尾又呼叫一次（`:1072`）；中途失敗時舊終態檔已被歸檔、無妨，但重複呼叫應二擇一。

### 低-4 deploy.md 剩 2 處與實作不符

- 步驟 4 與 §2.1：「驗證舊隧道連續穩定 active 後才停用新隧道」——實際無法成立（中-2）；請依中-2 的修法更新或如實說明「需人手 `systemctl stop／disable llmgateway-tunnel`」。
- §2.2 第 3 點「新舊皆未開機自動啟用（disable 到 enable 之間）」「數秒至一分鐘」與程式一致；但「切換窗口」之後 `cutover-started` 狀態卡住只能 `rollback-to-user` 的說明仍沒有（上輪低-5 提過，可選）。

## 資訊

### 資訊-1 本輪新增程式的競態／半套狀態檢查

- `_sync_final_cutover_data` 失敗路徑：只還原舊 unit、取消保險、寫 `aborted`；新位置的 `llm.db` 可能已被換成快照（`os.replace` 原子，無半套檔），下次 cutover 會再覆蓋——無妨。`sync-keys-env` 失敗時 db 已換、keys 沒換：兩者下次 cutover 都會再同步，無妨。
- `_copy_into_place` 的 `utime(..., follow_symlinks=False)` 對 fd 暫存檔路徑操作，包在 `except (OSError, NotImplementedError)`：靜默失敗會讓 `keys.env` mtime 不被保留，回滾的「誰比較新」會誤判成較新而多覆蓋一次（只在 llmgw 能插手該路徑時；影響小）。
- `atomic.write_bytes`：`fchmod`／`fchown` 在 `fdopen` 之前、例外時 `finally` 會 `os.unlink(tmp)` 但 `fd` 若在 `fdopen` 之前失敗不會被關（`os.fchmod` 失敗時洩漏一個 fd，短命行程，無妨）。
- `_wait_old_tunnel` 每 0.2 秒 fork 兩次 `systemctl --user`（約 150 次／30 秒）；負載無妨，可改 1 秒。

### 資訊-2 上輪「PM 操作清單」仍適用

進場備援（console）、切換前不要有重開機排程、`ps -u llmgw`、`--revert-after 15m`、第二終端機重連；再加一條：**回滾後若 `tunnel-old` fail，需人手 `systemctl stop llmgateway-tunnel && systemctl disable llmgateway-tunnel`（確認舊隧道起來後）**——在中-2 修掉前這是每次回滾的必要動作。

---

結論：**需修**（高 0、中 2、低 4、資訊 2）——中-2（回滾時舊隧道必 crash-loop，穩定判定使正常回滾永遠 rc=1、新隧道不收，文件與實際不符）建議切換前修掉或在 deploy.md 與操作清單中明文為人工收尾步驟；中-1（寫入側上層目錄未擋）為高-1 的殘留，建議同批修；其餘為低。高-1 的原始攻擊、中-1～中-3 已確實修好。
