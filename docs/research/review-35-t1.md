# 審查 #35 T1：lgwctl 管理指令與 llmgateway.service

**審查對象**：`git diff design/install-simplify...impl/lgwctl`（21 個檔、+3370；含 `lgwctl/` 套件、`deploy/units/llmgateway.service`、`tests/test_lgwctl_*`、`tests/lgwctl_fakes.py`）。T2／T3 已另審過，本報告只看它們與 T1 的接縫。

**審查分支**：`review/lgwctl` @ `47eeb14`（內容同 `impl/install-simplify`；T1 本體是 `c92167a`）。

**審查模型**：DeepSeek V4.1 Flash（Ollama；與 T1 作者同模型、不同 session。Zen 免費模型當時 429／不穩，PM 已知）。

**方法**：逐檔讀 T1 程式與測試，對照 `spec-35.md`、`install-simplify-design.md` §4／§5／§7、T2 的 `lgwd/keysync.py`／`lgwd/main.py`、T3 的隧道 unit 與 `ops-scripts/llmgateway-tunnel`；自己跑全量測試。沒有改被審程式碼（本報告是唯一新增檔）、沒有 push、沒有碰真機、沒有讀任何憑證檔或真 key。

**驗證**：

| 項目 | 結果 |
|---|---|
| `python3 -m unittest discover -s tests` | `Ran 404 tests in 120.446s` → `OK`（Python 3.12.3；程式使用 `from __future__ import annotations` 與標準函式庫，3.10 語法相容） |
| 規格／設計對照 | 逐條見「規格逐條核對」 |
| T2／T3 接縫 | 逐項見「T2／T3 接縫核對」 |
| Lima 演練紀錄的 0700／CHDIR | 與程式推理一致，見高-1；演練已重現 |

**等級數量**：高 1、中 7、低 11、資訊 7。

---

## 高（1）

### 高-1 `fetch` 把 `mkdtemp` 的 0700 目錄直接 rename 成版本目錄，`User=llmgw` 的服務進不去 `current`（CHDIR 200）

- **檔案:行**：`lgwctl/release.py:99`（`tempfile.mkdtemp(prefix=f".{tag}.", dir=layout.opt_dir)`）、`lgwctl/release.py:69-87`（`_move_into_place` 只 rename）、`lgwctl/install.py:133-148`（只校正 state／ssh 兩個目錄）、`deploy/units/llmgateway.service:14`（`WorkingDirectory=/opt/llmgateway/current`）。
- **問題**：`tempfile.mkdtemp()` 建立的目錄權限是 `0700`、擁有者 root；`_move_into_place()` 只把暫存目錄 rename 成 `/opt/llmgateway/<tag>`，之後沒有任何地方把版本樹改成服務帳號可讀。`llmgateway.service` 以 `User=llmgw` 啟動、`WorkingDirectory` 指到 `current` symlink，llmgw 進不去 `0700` 的目錄，systemd 會以 `status=200/CHDIR` 結束；同一個原因也會讓 T2 的 `os.access(<current>/ops-scripts/key-sync, X_OK)` 失敗，key 拉取被靜默關閉。`install` 與 `upgrade` 共用 `fetch`，兩條路徑都中。
- **情境**：全新安裝或升級到任何新 tag；Lima Ubuntu 24.04／systemd 255 演練已重現（install 後 unit 起不來，手動 chmod 後才繼續）。測試沒抓到，是因為 fake git 用 `cp -a` 複製樣板（目錄本來就是 0755），沒有模擬 `mkdtemp` 的 0700。
- **建議**：在 `fetch()` 完成、`switch_current()` 之前把版本樹正規化：目錄 `0755`、一般檔 `0644`、原本有執行位的維持 `0755`，擁有者 `root:root`（`LLMGW_PREFIX` 測試模式只 chmod、不 chown），並移除不需要的 `.git`；`/opt/llmgateway` 本身也明確 `0755`（root 的 umask 很嚴時 `os.makedirs` 會是 0700）。已存在的完整版本目錄重跑 install／upgrade 也要修（舊版留下的 0700）。補測試：用真的 `mkdtemp` 或把 fake git 的 dest 先 chmod 0700，斷言版本目錄與子目錄是 0755、一般檔沒有 group／other 寫位、執行檔仍是 0755；`upgrade` 也要斷言新目錄權限。作者工作區已有未 commit 的 `normalize_tree` 修法（整棵正規化＋移除 `.git`），方向正確；commit 後要重跑全量與再審一次。

---

## 中（7）

### 中-1 `systemctl is-active` 對「unit 不存在」回 4，目前被當成 `unknown`／todo，不是 fail

- **檔案:行**：`lgwctl/units.py:63-73`（只把 rc 0 當 active、rc 3 當 inactive，其餘都 `unknown`）、`lgwctl/check.py:55-66`（`unknown` → `todo <unit>（systemctl 不能用，無法確認）`）、`lgwctl/status.py:17-18`。
- **問題**：systemd 的 `systemctl is-active` 在 unit 檔不存在時回 **4**（LSB「program or service status is unknown」；systemd 原始碼 `check_unit_generic()` 的 `not_found` 分支），不是 3。目前 4 被歸成 `unknown`，`check` 會說「systemctl 不能用」並以 todo（結束碼 2）回報；但「unit 不在」是確定不成立，應該是 fail（結束碼 1）。`status` 也顯示 `unknown`，看不出是「沒裝」。
- **情境**：還沒 install、unit 檔被刪、換到別台機器、或 install 失敗只完成一半時，`check` 不會 fail；自動化（例如切換窗口的停損「`check` 非 0 就回滾」）會把「沒裝」誤當「無法確認」。隧道 unit 也一樣：`unknown` 分支在「設定未填→todo」之前，missing 不會走到 tunnel 的特例。
- **建議**：`unit_state()` 增加 `missing`（rc 4）；`_check_units()` 對 missing 一律 `fail <unit>（unit 不在，沒裝）`（含隧道，install 本來就該放兩個 unit）；只有 rc 1／其他（D-Bus 不通等）才是 `unknown`／todo。`status` 顯示 `missing`。fake systemctl 補 rc 4 測試，並補「兩個 unit 都不在時 check 回 1」的測試。

### 中-2 `upgrade` 只切 current／restart，不換 `/etc/systemd/system` 的 unit，也不 `daemon-reload`

- **檔案:行**：`lgwctl/upgrade.py:48-89`（沒有 `units.install_unit_files`、沒有 `units.daemon_reload`）、`lgwctl/units.py:41-54`（unit 檔的安裝只在 install 用）；測試 `tests/test_lgwctl_upgrade.py:78-80` 甚至斷言 unit 仍是舊版內容。
- **問題**：`deploy/units/llmgateway.service` 是版本的一部分（ExecStart 參數、`LoadCredential`、加固都可能在下一版改），但 `upgrade` 只換程式目錄並重啟服務，`/etc/systemd/system/llmgateway.service` 永遠停在第一次 install 的版本。升級成功後服務跑新程式，但 unit 的啟動參數／sandbox 仍是舊的；重開機也還是舊 unit（檔案沒換）。
- **情境**：下一版新增 ExecStart 參數、改 `--keys` 路徑、或改 `ProtectSystem`／`ReadWritePaths`；`upgrade` 回 ok、list 200，但新 unit 完全沒生效，直到有人再跑一次 `install`。反過來，如果新 unit 有錯，升級也不會發現。
- **建議**：`upgrade` 在 restart 前把當版兩個 unit 檔裝進 `/etc/systemd/system` 並 `daemon-reload`；失敗就 `restore_links` 回舊版並把舊 unit 檔裝回去（或先備份），再非零結束。若刻意維持「unit 只由 install 換」，至少要在 `check` 比對 tag 目錄與 `/etc/systemd/system` 的 unit 內容，不同就 fail／todo，並在文件寫明「改 unit 要重跑 install」。測試要改成「新版 unit 會被換掉」或「不同時 check 會說」。

### 中-3 已安裝的服務在跑時，`install --tag <新 tag>` 換了 current 但不 restart，status／check 會說新版本、process 還是舊的

- **檔案:行**：`lgwctl/install.py:311-319`（`switch_current` 換 current）、`lgwctl/install.py:223-232`（`is_active` 為真就 `ok start ... 已在跑（不動它）`）。
- **問題**：install 的冪等設計是「已裝過再跑要安全」，但重跑時若帶了不同的 tag，會先把 `current` 切到新 tag，再因為服務已在跑而不重啟。process 的 `WorkingDirectory`／程式碼還是啟動時那一版，但 `status`／`check --tag` 讀的是 `current/.lgw-tag`，會回報新 tag。這是「安裝成功、實際跑舊碼」的假象。
- **情境**：機器壞掉用 `install --tag` 重裝／換版（而不是用 upgrade）；或操作者不知道要用 upgrade。之後 process crash 被 systemd 重開（或重開機）才突然換成新程式；在那之前 `status` 都說新版本。
- **建議**：`install` 若 `old_tag != tag` 且 `llmgateway.service` 正在跑，就 restart（並像 upgrade 一樣等本機 list 200），或至少列 `todo start（要 restart 服務才會換版）`；也可以直接拒絕換 tag 並提示「換版請用 upgrade」。補測試：先裝 tag A、再 install tag B，斷言有 restart 或明確 todo，且 status 不會誤報。`install` 的「已在跑（不動它）」對同 tag 重跑是對的，對換 tag 不是。

### 中-4 `keys.pass` 被 chown 成 `llmgw:llmgw`，不是規格的 `root:llmgw 0640`

- **檔案:行**：`lgwctl/install.py:176-184`（`_chown(layout, layout.keys_pass_file)`、`_chown(layout, layout.keys_env)`）、`lgwctl/install.py:92-97`（`_chown` 把 uid 與 gid 都設成服務帳號）；規格 `spec-35.md:7`（`keys.pass` root:llmgw 0640）。
- **問題**：`_chown()` 只有一種行為：`os.chown(path, llmgw_uid, llmgw_gid)`。結果 `keys.pass` 是 `llmgw:llmgw 0640`，不是 `root:llmgw 0640`；而且**既有的** keys.pass 每次重跑 install 都會被改成 llmgw 所有，管理員刻意設的 root:llmgw 會被蓋掉。`keys.env` 給 llmgw 是對的（state 目錄本來就 llmgw），但解密密碼檔不該由服務帳號擁有。
- **情境**：服務帳號被入侵或程式有寫檔漏洞時，可以改寫自己的解密密碼（至少是 DoS／換密碼，讓下次重啟解不開），也讓「服務只能讀、不能改自己的 credential」這層 defense-in-depth 消失；另外重跑 install 會默默破壞管理員設定的權限。
- **建議**：把 `_chown` 改成可指定 uid／gid（或加 `_chown_root_group`）：`keys.pass` → `root:<llmgw gid>`、0640；`keys.env` → `llmgw:llmgw`、0600。補 root-only 測試（`os.geteuid() == 0` 時驗 uid／gid，非 root skip），並在 todo／文件寫明 keys.pass 要 root:llmgw。`_step_dirs` 的 state／ssh 用 `_chown`（llmgw:llmgw）是對的；這次只要把 keys.pass 改成 root:<llmgw gid>，不要改到 keys.env／state／ssh。

### 中-5 `sync` 不寫 `.keysync.status`，手動拉完 key 後 `check` 仍說「沒有／過期」；服務的 LoadCredential 快照也要 restart 才換

- **檔案:行**：`lgwctl/sync.py:22-53`（直接跑 `ops-scripts/key-sync`，只轉印輸出、回 rc）、`lgwctl/check.py:87-133`（只信 `.keysync.status`）、T2 `lgwd/keysync.py:346-351`（狀態檔只有 `KeySyncTimer` 會寫）。
- **問題**：`llmgwctl sync` 是「手動觸發一次 key 拉取」，但 key-sync 本身只寫 `keys.env`／`.keysync.sha`，不會寫 T2 的 `.keysync.status`；`check` 的 keysync 判定完全依賴那個狀態檔。因此手動 sync 成功後，`check` 仍會看到「狀態檔不在」→ todo，或看到舊的 `ok:false`／過期 → fail。另一個相關點：`lgwd` 的 `LoadCredential` 是服務啟動時的快照，本人填完 keys.pass 後若只跑 sync、沒有 restart，內建計時每 10 分鐘仍會用舊的（沒有密碼的）快照去跑，之後把狀態檔蓋成 `ok:false`。
- **情境**：install 後本人填 keys.pass，先跑 `sudo llmgwctl sync` 確認 key 沒問題（很自然），`check` 卻說 key 拉取沒成功／沒更新；或切換窗口時 sync 成功但 `check` 非 0，停損規則誤觸。若沒 restart，10 分鐘後狀態又變 fail，即使 keys.env 是新的。
- **建議**：`sync` 成功後用 T2 的格式（`{"ts": epoch, "ok": true, "reason": "ok"}`、0644、原子寫）更新 `.keysync.status`；失敗時比照 key-sync 的 `failed=` 寫 `ok:false`（或至少保留舊值，不要假成功）。輸出加一行 todo：「要 `systemctl restart llmgateway.service` 讓 LoadCredential 換新快照」；或 sync 成功後直接 restart（若要維持「sync 只拉 key」的語意，就前者）。補測試：sync 成功後 check 的 keysync 是 ok，且狀態檔內容不含值；再補「只跑 sync 沒 restart，計時仍用舊快照」的文件說明。

### 中-6 `--repo` 值未防 `-` 開頭，`git clone` 會把它當選項（root 執行時是參數注入面）

- **檔案:行**：`lgwctl/release.py:96-105`（`git clone --depth 1 --branch <tag> <repo_url> <tmp>`）、`lgwctl/cli.py:33-35`／`44-46`（`--repo` 直接進 argparse）、`lgwctl/run.py:31-43`（subprocess list，但沒有 `--`）。
- **問題**：`git clone` 的 `<repo>` 位置若以 `-` 開頭，會被 git 當成選項。argparse 允許 `--repo=--upload-pack=/some/path` 這種寫法；到了 git 就變成 `git clone ... --upload-pack=/some/path <tmp>`，git 會執行 `/some/path` 當 upload-pack（以 root 身分，因為 install／upgrade 都要求 root）。同理還有 `--config=...` 等其他 clone 選項。`LLMGW_REPO` 環境變數也走同一條路。
- **情境**：目前是單人專案、能跑 `sudo llmgwctl install` 的人本來就有很大的權限，風險低；但若未來 sudoers 只授權 `llmgwctl`（或有多人），這就是繞過 sudoers 的 root 任意命令執行。即使只給本人用，也不該留這種介面。
- **建議**：在 repo 參數前加 `--`（`git clone ... -- <repo> <tmp>`，先確認發行版 git 支援；現代 git 有），並在 `fetch()` 拒絕以 `-` 開頭或含 NUL 的 repo；更嚴的話只接受 `https://` 或絕對路徑（白名單）。`LLMGW_REPO` 套同一檢查。補測試：fake git 斷言 argv 有 `--`、或 `--repo=-...` 被擋下。

### 中-7 unit 的 `Type` 註解是錯的；`install` 只信 `systemctl start` 的結束碼，服務 app 層失敗仍可能印 `ok start`

- **檔案:行**：`deploy/units/llmgateway.service:9-18`（註解「Type 不寫：有 LoadCredential= 時 systemd 用 exec 語意」、`Restart=on-failure`）、`lgwctl/install.py:223-232`（start 成功就 ok）、`lgwctl/units.py:94-103`。
- **問題**：`LoadCredential=` 不會讓 systemd 改用 exec 語意；沒寫 `Type=` 時預設是 `simple`。`systemctl start` 在 fork 後就回 0，`lgwd` 之後才因為 policy 載入失敗、keys 檔格式錯、`--api-port=` 解析失敗等原因退出——`install` 仍會印 `ok start`。`Type=exec` 也只能保證 exec 成功，不能保證 app 起來，但至少比 simple 準；真正的確認要 poll API。隧道 unit 同理（`Type=simple`＋`Restart=always`），`start` 回 0 不代表 ssh 連上。
- **情境**：新 tag 的 policy 壞掉、keys.env 格式錯、或 `LGW_API_PORT` 展開成空字串；install 看起來成功，服務其實 crash-loop，直到有人跑 `check`（或看 journal）才發現。升級路徑有 `wait_local_ok` 兜底，install 沒有。
- **建議**：unit 改 `Type=exec`（systemd 250+ 有；真機 259、演練 255）並修正註解；`install._step_service` 在 start 後用 `probe.wait_local_ok(layout, 短秒數)`（例如 10 秒）或至少 `is-active` 再確認一次，失敗就 `fail start`；`_step_tunnel` 也至少看 `is-active`。可順便加 `RestartSec=5` 避免 100ms 連打（非必要）。

---

## 低（11）

### 低-1 `tunnel.env` 範本的 `LOCAL_API` 固定用 8480，不跟 `llmgateway.env` 的 `LGW_API_PORT` 連動

- **檔案:行**：`lgwctl/install.py:45-56`（`TUNNEL_ENV_TEMPLATE`）、`lgwctl/install.py:151-165`（`port=DEFAULT_API_PORT`）、`ops-scripts/llmgateway-tunnel:116-119`（`LOCAL_API` 決定 reverse tunnel 的本機埠）。
- **問題**：`llmgateway.env` 可以改 `LGW_API_PORT`（install 也會保留既有的），但 `tunnel.env` 範本的 `LOCAL_API` 永遠寫 `DEFAULT_API_PORT`（8480）。改了 API 埠的機器，隧道會把遠端 4710 轉到本機 8480，遠端 list 探針失敗。
- **情境**：本人或舊設定把 API 埠改成非 8480，重新 install 時 `llmgateway.env` 被保留、`tunnel.env` 卻用預設；本機 list ok、經會合點 fail。
- **建議**：寫 `tunnel.env` 範本時用 `layout.api_port()`（先寫／讀 `llmgateway.env` 再算），或在 `check` 比對 `tunnel.env` 的 `LOCAL_API` 與 `llmgateway.env` 的 `LGW_API_PORT`，不一致就 fail／todo；文件寫明兩者要一致。

### 低-2 隧道設定未齊時 install 只列 todo，沒有說「填好後要 start／重跑 install」；填好後隧道不會自己起來

- **檔案:行**：`lgwctl/install.py:235-249`（未 ready 只 `todo tunnel-key`，沒有 start 指示）、`deploy/units/llmgateway-tunnel.service:11-14`（`RestartPreventExitStatus=78`）、`ops-scripts/llmgateway-tunnel:221-223`（設定缺 → exit 78）。
- **問題**：install 在隧道設定未齊時會 enable 但不 start；本人事後補 `tunnel.env` 與金鑰後，unit 仍是 inactive，`RestartPreventExitStatus=78` 讓它不會自動重試，也沒有 timer。要等重開機（enabled unit 這次設定已齊會成功）或手動 start／再跑 install。todo 只說「補齊」，沒說下一步。
- **情境**：全新安裝的標準流程（install → 本人補隧道設定），補完後以為會自己起來，結果 `check` 的隧道 unit／list-remote fail。
- **建議**：todo 文字加「填好後跑 `sudo systemctl start llmgateway-tunnel.service`（或重跑 `llmgwctl install`）」；或 install 在下次重跑時看到 ready 就 start（目前會，但要人知道可以重跑）。

### 低-3 非 root 跑 `check` 讀不到 0700 的 state／llmgw 的 key，會得到誤導的 todo／fail

- **檔案:行**：`lgwctl/install.py:141-143`（state dir 0700、llmgw）、`lgwctl/check.py:91-99`／`109-114`（讀 `.keysync.status`、`keys.env`）、`lgwctl/probe.py:58-70`（以執行者身分跑 ssh，key 在 `~llmgw/.ssh`）。
- **問題**：`check` 沒有 root 檢查；一般使用者執行時，state 目錄 0700 讀不到 → keysync 顯示「狀態檔不在」、keys.env「不在」；探針用一般使用者的身分讀 llmgw 的 0600 私鑰會失敗 → list-remote fail。`status` 不受影響，但 `check` 會給人「壞了」的假訊號。
- **情境**：運維只記 `llmgwctl check`（沒加 sudo）；或 wrapper 在 PATH 裡，順手就跑。spec／design 寫的是 `sudo llmgwctl check`，但 CLI 沒有提示。
- **建議**：`check` 在 `euid != 0` 且不是服務帳號時，直接給 `fail check（要 sudo）` 或清楚 todo；或把讀 state／跑探針改用 `sudo -n -u llmgw`。文件與 `--help` 寫明 check 要 root／sudo。

### 低-4 `child_env()` 會把呼叫者環境裡的 `LLMGW_KEYS_PASSWORD` 帶給所有子行程

- **檔案:行**：`lgwctl/layout.py:352-360`（`child_env` 複製整份 env）、`lgwctl/run.py:31-43`、`lgwctl/sync.py:36-41`。
- **問題**：如果操作者的 shell 有 export `LLMGW_KEYS_PASSWORD`（舊流程或手動測試可能這樣做），install／check／upgrade 呼叫的 `git`、`curl`、`gh`、`systemctl`、`useradd`、探針全部會繼承這個變數。它不是 argv、也不會被印出，但屬於不必要的曝露（`/proc/<pid>/environ`、core dump、子行程外掛）。spec 的模型是「密碼只經 LoadCredential 給服務」。
- **情境**：本人在同一個 shell 先 export 密碼再跑 `sudo llmgwctl ...`（sudo 多數情況會清掉，但 `sudo -E`／root 直接跑就不會），或測試腳本忘了 unset。
- **建議**：`child_env()` 預設把 `LLMGW_KEYS_PASSWORD` 拿掉（必要時讓 `sync` 明確加回，且只給 key-sync 的子行程）；測試斷言 fake git／gh／systemctl 的 env 裡沒有這個變數。

### 低-5 `upgrade` 的 policy dry-run 用 current（舊版）的 `lgwd.policy` 驗新 tag 的 policy

- **檔案:行**：`lgwctl/upgrade.py:20-37`（`from lgwd.policy import ...`，cwd 是 current）、`lgwctl/check.py:136-150`（同理，但 check 驗的是 current，正確）。
- **問題**：upgrade 時 cwd／`sys.path` 是舊版目錄，載入的是舊版 `lgwd.policy`，卻拿它驗新 tag 的 `policy/default.json`。若新 tag 改了 policy 的規則／欄位，舊 loader 可能誤擋（升級被拒絕）或誤放（重啟後新 loader 才失敗，靠 list 200 自動回滾兜底）。
- **情境**：新 tag 新增 policy 欄位或放寬規則；舊 loader 判不過 → upgrade 非零、不切版，但其實新版本是好的；反過來舊 loader 放過、新 loader 失敗，會走自動回滾（結果安全，但多一次重啟）。
- **建議**：dry-run 用新 tag 的 `lgwd`（例如 `subprocess` 以 `cwd=tag_dir` 跑 `python3 -c 'from lgwd.policy import load_policy; ...'`，或把 `tag_dir` 插到 `sys.path` 最前面再 import）。補測試：新舊 loader 行為不同時，應以新 tag 為準。

### 低-6 自動回滾路徑的 `release.restore_links` 沒有 try／except，極端環境會 traceback

- **檔案:行**：`lgwctl/upgrade.py:75-84`（restart 失敗與 list 非 200 兩處都直接呼叫 `restore_links`，只有第二次 restart 是 best effort）。
- **問題**：`restore_links` 會 `write_link`／`os.unlink`，可能丟 `OSError`（權限、I/O、link 被佔用）。這個路徑沒有包 try；例外會冒出 `cli.main`，使用者看到 traceback，且當下 links 的狀態不確定（可能已切回一半、服務還在新版）。
- **情境**：/opt 權限被改壞、磁碟滿、或 link 被手動換成目錄；剛好升級失敗要自動回滾時，回滾本身 traceback。
- **建議**：兩處 `restore_links` 都包 try／except OSError，失敗就 `fail rollback（type）` 並盡力 restart 舊版；測試用不可寫的 opt 目錄重現（現有 `test_unwritable_opt_dir...` 只蓋 fetch 階段）。

### 低-7 `check` 的 keysync 新鮮度固定 1800 秒，但 interval 可由 `LGW_KEYSYNC_INTERVAL` 調大

- **檔案:行**：`lgwctl/layout.py:83-86`（`DEFAULT_KEYSYNC_STALE = 1800`）、`lgwctl/check.py:124-129`（`age > keysync_stale` → fail）、T2 `lgwd/main.py:121-132`（interval 可由環境／參數調）。
- **問題**：預設 600 秒 × 3＝1800 是對的；但 spec／T2 允許把 interval 調成 0（關閉）或更大（例如 3600），`check` 仍用 1800 判新鮮度。調大 interval 後，健康的機器會被判 fail；調成 0 時狀態檔不更新（目前 missing→todo，還算合理）。`LLMGW_KEYSYNC_STALE` 可以覆寫，但沒有跟 interval 連動，容易忘。
- **情境**：為了減少 gh 呼叫把 interval 設 1800 或 3600；`check` 在每個週期尾端都 fail，停損規則誤觸。
- **建議**：`check` 從環境／unit（或新增的 `llmgateway.env` 鍵）讀 `LGW_KEYSYNC_INTERVAL`，stale 取 `max(3 × interval, 60)`；或至少在 `--help`／文件寫明調 interval 要一起調 `LLMGW_KEYSYNC_STALE`。

### 低-8 `REQUIRED_FILES` 只驗 6 個檔，缺 `lgwctl/__main__.py`、`lgwd/main.py` 等也能被當成「完整」

- **檔案:行**：`lgwctl/release.py:19-26`（`REQUIRED_FILES`）。
- **問題**：清單有 `policy/default.json`、`lgwd/__main__.py`、`ops-scripts/key-sync`、`ops-scripts/llmgateway-tunnel`、兩個 unit，但沒有 `lgwctl/__main__.py`（wrapper 要跑的管理指令本體）、`lgwd/main.py`／`lgwd/policy.py`／`lgwd/keys.py`（`lgwd/__main__.py` 會 import）。若 tag 不完整（鏡像、手動 repo、打包漏檔），`fetch` 會成功、`install` 可能一路 ok，之後 wrapper 或服務才失敗；管理指令本身沒了，連升級都難。
- **情境**：`--repo` 指到不完整的鏡像或自製 tag；install 說 ok，`llmgwctl` 下次跑就 traceback／ImportError。
- **建議**：至少加 `lgwctl/__main__.py`、`lgwd/main.py`、`lgwd/policy.py`（`lgwd/keys.py` 也建議）；install 在切 current／start 前也跑一次 policy dry-run（和 upgrade 共用），把「版本不完整／政策壞」擋在切版前。

### 低-9 `useradd` 沒指定 `-U`，`Group=llmgw` 依賴發行版的 `USERGROUPS_ENAB` 預設

- **檔案:行**：`lgwctl/install.py:121-122`（`useradd -m -s /bin/bash`）、`deploy/units/llmgateway.service:13`（`Group=llmgw`）。
- **問題**：Debian／Ubuntu 的 `useradd` 預設會建同名群組（`USERGROUPS_ENAB yes`），但不是所有發行版／設定都如此；若群組不存在，unit 的 `Group=llmgw` 會讓服務起不來，而 install 不會先檢查群組。
- **情境**：精簡容器、非 Debian 基底、或 `USERGROUPS_ENAB no` 的機器；帳號建了、unit 起不來。
- **建議**：`useradd -m -U -s /bin/bash`，並在 `_step_account` 用 `grp.getgrnam(layout.service_user)` 確認群組存在（不存在就明確 fail／建立）；或 `Group=` 改成 `User=llmgw` 的主群組名稱（仍需存在）。

### 低-10 wrapper 的 `cd {current}` 沒有引號，路徑有空白／特殊字元時會壞

- **檔案:行**：`lgwctl/install.py:66-71`（`WRAPPER_TEMPLATE` 的 `cd {current} || exit 1`）、`lgwctl/install.py:195-203`。
- **問題**：正式路徑 `/opt/llmgateway/current` 沒有空白，所以現在不會壞；但 `LLMGW_PREFIX` 測試沙箱或未來的路徑若有空白，wrapper 會 `cd` 到錯的位置。shell 變數展開也沒有加引號。
- **情境**：用含空白的 prefix 跑 install 後執行 wrapper；測試沒蓋到。
- **建議**：用 `shlex.quote` 產生 `cd -- <quoted>`，或在 wrapper 用 `exec python3 -m lgwctl` 前先 `cd` 到以環境變數傳入的路徑（仍要 quote）；補一個含空白 prefix 的入口測試（可選）。

### 低-11 `fetch()` 在中斷（SIGINT／OOM／kill）時會留下 `.tag.XXXX` 暫存目錄，cleanup 不會清

- **檔案:行**：`lgwctl/release.py:99`（mkdtemp）、`lgwctl/release.py:106-113`／`122-125`（只清有處理到的錯誤）、`lgwctl/release.py:164-166`（cleanup 跳過 `.` 開頭的名稱）。
- **問題**：`mkdtemp` 到 `_move_into_place` 之間若行程被 SIGINT／OOM／kill，`except` 不會跑，`.lgw-vX.Y.Z.XXXX` 留在 `/opt/llmgateway`；`cleanup_old_versions` 明確跳過 dot 名稱，永久累積。每次中斷留一份完整 clone，久了佔空間；也讓 `ls /opt/llmgateway` 很亂。
- **情境**：網路慢時按 Ctrl-C、或 OOM killer 在 clone 期間殺掉；重試幾次留幾份。
- **建議**：把整個 `fetch` 的暫存生命週期包在 `try/finally`（tmp 還在就 `rmtree`），或在 `cleanup_old_versions` 清掉超過一天的 `.tag.*`／`.*.stale.*`；測試可 mock `run` 丟 KeyboardInterrupt 驗證。

---

## 資訊（7）

### 資訊-1 keys.pass 先放「只有註解」的空殼是對的；空 keys.env 也能讓服務起來

- **檔案:行**：`lgwctl/install.py:58-64`（`KEYS_PASS_TEMPLATE`）、`lgwctl/install.py:163-164`（keys.env 空檔）、`lgwctl/install.py:280-293`（`keys-pass` todo）。
- **問題**：核對「空殼 keys.pass＋空 keys.env」是否會讓服務起不來，不是缺陷。
- **情境**：全新安裝、本人還沒填密碼時。
- **建議**：不用改。`LoadCredential=` 的來源檔不存在時 unit 會直接起不來，所以 install 先放 comment-only 的 keys.pass 是必要且正確的；`lgwd` 的 `KeyStore` 對空 `keys.env` 不會丟錯（空檔＝沒有 key），服務可以起來並提供 list，T2 的 key-sync 之後才因 `password_missing` 失敗。todo 已寫「填值後 restart」，和「LoadCredential 是啟動快照」相符；文件可把「空殼→服務可起、key 拉取尚未成功」寫清楚。

### 資訊-2 `check` 的遠端探針以 root 身分跑、使用 llmgw 的私鑰，實務上可行

- **檔案:行**：`lgwctl/probe.py:58-70`（直接 `run(argv, env=...)`，沒有 `sudo -u llmgw`）、`ops-scripts/llmgateway-tunnel:148-158`（`-i <IDENTITY>`）。
- **問題**：核對「root 跑探針、讀 llmgw 的 0600 私鑰」是否會被 ssh 拒絕，不是缺陷。
- **情境**：`sudo llmgwctl check`（root）跑經會合點的探針。
- **建議**：不用改。OpenSSH 的 `sshkey_perm_ok()` 只在私鑰擁有者等於執行者 uid 時檢查 `077` 權限；root 讀 llmgw 擁有的 0600 金鑰不會被拒（擁有者不同就不檢查）。Lima 演練的 `check` 含 list-remote 全 ok 也印證了。功能上可接受；若要最小權限，可改成 `sudo -n -u llmgw` 跑探針（和 gh 檢查一致），但不是必須。

### 資訊-3 `ProtectSystem=strict` 下 `gh` 的 HOME 可讀，寫入（state.yml／token 更新）會被擋；演練只蓋了有效 token

- **檔案:行**：`deploy/units/llmgateway.service:24-28`（strict、ReadWritePaths 只有 state）、T2 `lgwd/keysync.py:96-115`（讀 `$CREDENTIALS_DIRECTORY`）、`lgwd/main.py`（key-sync 子行程繼承 sandbox）。
- **問題**：核對加固對 `gh` 的影響：strict 下 `/home` 是唯讀（不是不可讀），讀 `~llmgw/.config/gh/hosts.yml` 可以；`gh` 寫 update state／token 更新會失敗。演練只測了有效 token、沒有測到期／重登，所以這一項是「待確認」而非確定缺陷。
- **情境**：開機後 key 拉取、token 到期後以 llmgw 重新登入、或 `gh` 想寫 state.yml。
- **建議**：演練再加一條「token 到期／重新登入後，服務內的 gh 仍可讀」；若發現 gh 因寫入失敗而回非零，才加 `ReadWritePaths=/home/llmgw/.config/gh`。目前演練的 key 拉取與 `gh api` 都成功，所以先不改。**不要**加 `ProtectHome=yes`（T2/T3 審查已指出會讓 key 拉取變 gh_auth 失敗）。

### 資訊-4 T2／T3 接縫逐項對照：全部對得上

- **檔案:行**：`deploy/units/llmgateway.service:18-23`、`lgwctl/layout.py:273-283`、`lgwctl/check.py:87-133`、`lgwctl/probe.py:58-70`、`lgwctl/sync.py:36-38`。
- **問題**：核對 T1 與 T2／T3 的介面是否一致，不是缺陷。
- **情境**：T1 的 unit／check／sync 接 T2 的 keysync 與 T3 的隧道 unit。
- **建議**：不用改；逐項如下。

| 接縫 | T1 | T2／T3 | 判定 |
|---|---|---|---|
| 服務啟動參數 | `--policy /opt/llmgateway/current/policy/default.json`、`--keys /var/lib/llmgateway/keys.env`、`--db …/llm.db`、`--api-port=${LGW_API_PORT}` | `lgwd/main.py` 的 `--policy/--keys/--db/--api-port` | ✓（`${}` 由 `EnvironmentFile` 供值；Lima 已驗展開） |
| key-sync 預設路徑 | unit 不傳 `--keysync-*`；`fetch` 的 `REQUIRED_FILES` 保證 `<current>/ops-scripts/key-sync` 存在且可執行 | `lgwd/keysync.py:71-75` 預設 `<prefix>/opt/llmgateway/current/ops-scripts/key-sync` | ✓ |
| 狀態檔 | `check` 讀 `<prefix>/var/lib/llmgateway/.keysync.status` | `status_path_for(keys_path)`＝keys.env 同目錄；unit 的 `--keys` 就是該路徑 | ✓（一行 JSON `ts/ok/reason`，T1 用 `json.load` 可讀） |
| keys.pass 格式 | install 範本與 todo 都是單行 `LLMGW_KEYS_PASSWORD=…` | `read_password()` 逐行找同名變數，空值／讀不到回 None | ✓（格式一致；擁有者見中-4） |
| 隧道設定 | 探針帶 `LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env`、`HOME=service_home` | T3 `config_path()` 優先讀 `LGW_TUNNEL_ENV`；exit 0／1／2 對應 ok／fail／todo | ✓ |
| 兩個 unit 的落地 | install 從當版 `deploy/units/` 複製兩個檔、`daemon-reload`、enable／start | T3 的 tunnel unit 在當版目錄裡 | ✓（升級不換 unit 見中-2） |
| 開機順序 | `llmgateway.service` 有 `After/Wants=network-online.target` | T2 的 key-sync 在 lgwd 執行緒內，沒有自己的 unit | ✓（T2/T3 審查的低-5 已補） |
| `sync` 參數 | `--keys`、`--code-dir`、`--gh` | `ops-scripts/key-sync` 三個參數都存在（`--gh` 是既有參數） | ✓ |
| 單元 hardening | `ProtectSystem=strict`、`ReadWritePaths=/var/lib/llmgateway`、`PrivateTmp`、`NoNewPrivileges`、`LoadCredential` | T2 只寫 state 目錄、讀 credential | ✓（gh 寫入見資訊-3） |

### 資訊-5 測試 404 個全過，但 fake git 沒有模擬 `mkdtemp` 的 0700，所以漏掉高-1；其他覆蓋也有幾個缺口

- **檔案:行**：`tests/lgwctl_fakes.py:61-94`（fake git 用 `cp -a` 複製樣板）、`tests/test_lgwctl_install.py:64-113`（沒有 mode 斷言）、`tests/test_lgwctl_upgrade.py:64-80`（斷言 unit 不換）。
- **問題**：測試數量與涵蓋率的核對；404 個全過，但測試替身與真實 `mkdtemp` 有差異，所以漏掉高-1。
- **情境**：PM 已在整合分支跑 404 個測試 OK；這台 Mac 的 `python3 -m unittest discover -s tests` 也是 404 OK（120.446s）。
- **建議**：除了高-1 的 mode 測試，建議補：`systemctl is-active` rc 4（中-1）、`install` 換 tag 是否 restart（中-3）、`sync` 後的 `.keysync.status`（中-5）、`--repo=-...` 被擋（中-6）、`check` 在非 root 的訊息（低-3）。這些都不需要真機。

### 資訊-6 輸出／log／例外／argv 的值檢查：沒有發現 key 值或密碼；`sync` 會轉印 key-sync 的輸出（T2/T3 已審）

- **檔案:行**：`lgwctl/report.py:10,25-39`、`lgwctl/run.py:20-28`（`RunError` 只帶指令名）、`lgwctl/check.py:153-183`（gh 只印 repo 名／狀態）、`lgwctl/install.py:266-293`（gh auth 只看 rc）、`lgwctl/sync.py:45-48`（轉印子行程輸出）。
- **問題**：核對是否有 key 值／密碼可能出現在輸出、log、例外或 argv；沒有找到。
- **情境**：install／check／status／upgrade／rollback／sync 的正常與失敗路徑。
- **建議**：不用改。T1 自己的訊息只寫有無、名稱、時間、種類；`keys.pass` 的值只被 `_keys_pass_filled()` 讀來判斷非空，沒有進輸出；`sync` 的 argv 只有路徑，密碼是在 `sudo -u llmgw` 的子 shell 內 source 後放進 key-sync 的環境（不進 argv）。`sync` 會把 key-sync 的 stdout／stderr 原樣轉印，這點由 T2/T3 的 key-sync log 規則保證不含值；本報告重看沒有發現值。`--repo` 若被塞進含帳密的 URL，T1 不印，但 git 可能寫進 `.git/config`（高-1 的修法會移除 `.git`）；不要把憑證放 URL。

### 資訊-7 systemd 250+ 的語法與加固在演練機（255）可用；`Type` 的誤解見中-7

- **檔案:行**：`deploy/units/llmgateway.service:1-31`、`deploy/units/llmgateway-tunnel.service`（T3）。
- **問題**：核對 unit 檔語法、`Type` 預設、`WorkingDirectory`、`/usr/bin/python3`、`After/Wants` 與 systemd 版本需求；沒有語法缺陷（`Type` 的註解錯見中-7）。
- **情境**：Ubuntu 24.04／systemd 255 演練機；真機 systemd 259。
- **建議**：不用改語法。`[Unit]/[Service]/[Install]`、`EnvironmentFile`、`ExecStart` 的 `${LGW_API_PORT}`、`LoadCredential=keys.pass:/etc/llmgateway/keys.pass`、`ProtectSystem=strict`、`ReadWritePaths`、`PrivateTmp`、`NoNewPrivileges`、`User/Group`、`WorkingDirectory`、`After/Wants` 都是合法指令；`LoadCredential` 需 systemd ≥ 250（真機 259、演練 255），符合。`/usr/bin/python3` 在 Ubuntu 24.04 存在；`-m lgwd` 由 `WorkingDirectory` 的 cwd 進 `sys.path`。唯一要改的是 `Type` 的註解與「start 結束碼代表 app 起來」的假設（中-7），以及高-1 的目錄權限。

---

## 規格逐條核對

| 規格（`spec-35.md`） | 實作 | 判定 |
|---|---|---|
| install：建帳號（已存在略過）、目錄與權限、取 tag、切 current、放 unit、設定範本不覆蓋、daemon-reload、enable＋start 兩個 unit、缺本人動作列 todo、冪等 | `install.py:296-337`、`release.py:90-127`、`units.py:41-54` | △ 高-1（目錄權限）、中-3（換 tag 不 restart）、中-7（start 只看 rc）；其餘 ✓ |
| install 的 `keys.pass` root:llmgw 0640 | `install.py:92-97,176-184` | ✗ 中-4（實際 llmgw:llmgw） |
| check：兩個 unit 的 is-active、本機 list 200、經會合點探針（`--no-remote` 可略）、keysync 狀態＋keys.env 年齡、policy dry-run、gh 讀私有 repo、每項一行三態、結束碼 0／1／2、輸出不含值 | `check.py:55-198`、`probe.py`、`layout.py:321-350` | △ 中-1（rc 4 誤判 todo）、低-3（非 root 誤導）；其餘 ✓ |
| status：current／previous tag、兩個 unit 狀態、不需 root | `status.py:11-21` | ✓（missing 顯示 unknown 見中-1） |
| upgrade：抓 tag、對新目錄 policy dry-run、切 current、只 restart 服務、等 30 秒本機 list 200、失敗切回並 restart、成功只留兩版、不碰隧道 | `upgrade.py:48-89` | △ 中-2（不換 unit）、低-5（舊 loader 驗新 policy）、低-6（回滾 traceback）；其餘 ✓ |
| rollback：切回 previous、restart、跑 check | `upgrade.py:92-118` | ✓（連跑兩次會把 current／previous 對調；設計上可接受，文件可註明） |
| sync：手動觸發一次 key 拉取，等同 key-sync | `sync.py:22-53`、`layout.py:273-279` | △ 中-5（不寫狀態檔）；其餘 ✓ |
| 遷移子指令預留分派、migrate 缺席時明確錯誤 | `cli.py:21,54-68` | ✓（結束碼 3） |
| 版面固定值、`LLMGW_PREFIX`、外部指令可覆寫 | `layout.py:28-86,165-204,206-293` | ✓（單元裡的絕對路徑不受 prefix 影響是預期：unit 只裝在真系統層） |
| unit 固定值：User、Restart=on-failure、WantedBy、LoadCredential、加固、Wants/After | `deploy/units/llmgateway.service:4-31` | ✓（Type 註解見中-7） |
| 安全：輸出／log／例外／argv 不含 key 值或密碼；keys.pass 權限；root 下的路徑／symlink／tar-clone 風險 | `report.py`、`run.py`、`install.py`、`release.py` | △ 中-4（權限）、中-6（`--repo` 注入）、低-4（env 繼承）；其餘 ✓（沒有發現值外洩） |

---

## 作者回報的 7 個「不確定」（依程式與 commit 歸納）逐項判斷

> 說明：作者回報的原文沒有落在 repo、工單 #35 或交接檔（我找到的只有 commit 訊息與 `upgrade.py` 的「見回報的不確定」）。以下 7 項是從程式、commit 與規格可辨識的設計不確定點，逐項給判斷；若 PM 手上的原文清單不同，請用上面的「檔案:行」對照，結論不變。

1. **install 只校正安全相關目錄（state、ssh），其他沿用系統預設** — **判斷：不成立，這是高-1。** 版本目錄不是「系統預設」而是 `mkdtemp` 的 0700；整棵版本樹都要正規化成 0755／0644、root:root，否則服務 `WorkingDirectory` 進不去、key-sync 也看不到。作者工作區已有未 commit 的 `normalize_tree`，commit 後要補測試與重跑。
2. **upgrade 只 restart、不換 unit 檔** — **判斷：可理解但建議改（中-2）。** unit 是版本的一部分；只 restart 會讓新版 unit 的 ExecStart／sandbox 變更靜默不生效。至少要在 `check` 比對 unit 內容／在文件寫明「改 unit 要 install」；最好是 upgrade 時裝 unit＋daemon-reload，失敗就回滾。
3. **install 在服務已跑時不 restart** — **判斷：同 tag 重跑正確，換 tag 不正確（中-3）。** 換 tag 時 current 與 process 會不一致，status 會說新版本。建議換 tag 且 active 時 restart（或明確 todo／拒絕，提示用 upgrade）。
4. **keys.pass 的擁有者** — **判斷：目前錯（中-4）。** 應是 root:llmgw 0640，不是 llmgw:llmgw；`_chown` 要能分開 uid／gid，且不要覆蓋管理員已設好的 root:llmgw。
5. **check 的 `systemctl is-active` 非 0／3 都算 unknown** — **判斷：要區分 rc 4（中-1）。** rc 4 是 unit 不存在（systemd 原始碼的 `not_found`），應 fail「unit 不在」；只有 rc 1／其他才是 unknown／todo。隧道 unit 也一樣。
6. **sync 要不要寫 `.keysync.status`** — **判斷：要（中-5）。** 不寫的話手動 sync 成功、check 仍說沒有／過期；而且服務的 LoadCredential 是啟動快照，填完 keys.pass 只跑 sync 不會換，內建計時之後會把狀態蓋成失敗。sync 應寫狀態並提示（或直接）restart。
7. **policy dry-run 用哪一版的 lgwd 規則** — **判斷：應用新 tag 的（低-5）。** 目前用 current 舊版；新舊 loader 不同時會誤擋或延後發現。改用新 tag 的 `lgwd.policy`；list 200 的自動回滾是最後兜底。

---

## 結論

高-1 是實機必炸的阻擋項（Lima 演練已重現），中-1～中-7 建議在合併前修（多數是小的區域改動與測試）；低／資訊不擋。T2／T3 的接縫（ExecStart、key-sync 路徑、狀態檔、credential 格式、`LGW_TUNNEL_ENV`、兩個 unit 的落地）逐項對得上。

結論：需修
