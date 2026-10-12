# v5 審查 #13：key-sync＋timer、mtime 重讀、部署入口移除（reviewer-13）

審查對象：`git diff origin/scope/gateway-v5...HEAD`（`review/v5-keysync`；#13：ops-scripts/key-sync＋timer、lgwd 看 mtime 重讀並拿掉 SIGHUP、拿掉兩個 workflow 與 remote-deploy.sh、bootstrap 改放解密密碼、隧道與 key-sync 搬到 ops-scripts/、install.sh --check 加 key-sync 判斷、docs/deploy.md 全文重寫、test_policy 註解修正）。
基準：`openspec/changes/gateway-v0/`（v5）、`docs/research/v5-plan.md`、`v5-inventory.md`、`docs/research/v0-*-review.md`、`v5-review-12.md`。
審查者：reviewer-13（opencode＋Muse Spark 1.3；作者是 DeepSeek，不是審查者）。只審查，未改任何程式。

## 結論

**修了中-1 再開。** 高 0、中 1、低 7。中-1 是 key-sync 在 `same_sha` 短路時不看本機 key 檔狀態的沉默卡死邊界（spec 相容的修法，見建議）；其餘皆為小修或記錄即可。

## 測試

`python3 -m unittest discover -s tests -t .`

```
Ran 234 tests in 105.589s

OK
```

`openspec validate gateway-v0 --strict`

```
Change 'gateway-v0' is valid
```

（#12 時 239 個；#13 刪 `test_deploy_script.py`、`test_workflows.py`，新增 `test_keysync.py` 24 條，淨 −5，合理。）

## 逐項核對（都有證據，通過的就不列為發現）

1. **密碼路徑**：`ops-scripts/key-sync:191` 命令列只有 `-pass env:<名>`，值只進子行程 env（`env[password_env] = password`）；log（`:352`、`:355`、`:358-362`）只有 tag 與 12 字元 sha 前綴；例外訊息皆為靜態中文＋種類（`format_failed` 夾帶的 `KeyFileError` 經 `lgwd/keys.py:6` 保證只寫行號與種類）。`tests/test_keysync.py:201` `assert_no_leak` 逐條斷言密碼及其 base64 不在 stdout／stderr／argv 紀錄／sha 檔／keys.env。✅
2. **sha 短路順序**：`sync()`（`key-sync:302-304`）先比 sha 再讀密碼，相同直接回 `same_sha`（不解碼、不解密、不碰 key 檔，符合 v5-plan §1.2 第 2 步）。✅（但見中-1）
3. **失敗分類**：14 種 tag（`gh_auth`／`gh_missing`／`gh_failed`／`gh_timeout`／`gh_not_found`／`bad_response`／`decode_failed`／`decrypt_failed`／`format_failed`／`write_failed`／`password_missing`／`openssl_missing`／`code_missing`／`unexpected`）全部保留舊 key、不寫 sha；`docs/deploy.md:489-502` 的排錯表 14 種全覆蓋（含 d743f81 補的三種）。`gh` 沒登入經 returncode 4／401／特徵字串判 `gh_auth`（`:133-138`），且不回顯 gh 輸出。✅
4. **原子＋權限＋暫存**：`_atomic_write`（`:233-251`）與 `_install_plaintext`（`:254-286`）皆同目錄 mkstemp → fsync → chmod 0600 → rename，finally 必清暫存；`test_*.py` 有 `leftovers()` 斷言無殘留；內容相同時不換檔只寫 sha（防 mtime 空轉，符合 §1.3）。✅
5. **逾時**：gh 與 openssl 共用 `--timeout`（預設 60s，`LGWSYNC_TIMEOUT` 可覆）；gh 逾時記 `gh_timeout`，openssl 逾時記 `decrypt_failed` 但訊息寫明「逾時」，皆保留舊 key。✅（tag 共用見觀察，不列發現；`LGWSYNC_TIMEOUT` 非法值見低-3）
6. **lgwd mtime 重讀**：`(mtime_ns, size, ino)` 三元戳（`lgwd/main.py:117-127`）、啟動先記戳再讀檔、double-checked locking、失敗沿用舊值且 log 不含值（`:177-188`）；`KeyStore` 本來就是 lock 保護（`lgwd/keys.py:53-75`），鎖順序單一（`_keys_lock` → 內部鎖），無反轉；`test_main.py:222-263` 覆盖換檔重讀／未變不讀／壞檔沿用／下次再變重試、`test_policy不再熱重載`。`ExecReload` 已刪（`deploy/llmgateway.service`），SIGHUP 處理已拿掉。✅（重試語意見低-5；`policy.py` 殘留註解見低-1）
7. **部署入口移除**：`.github/` 在基準只含兩個 deploy workflow，現整個目錄消失（`git ls-tree` 對過）；`remote-deploy.sh` 整檔刪；程式與測試無 `remote-deploy`／`deploy-keys`／`deploy-service`／`deploy-pubkey`／`authorized_keys` 殘留（grep 空）；`llmgateway-tunnel`＋unit 是純搬移（rename，0 行變更）；`test_tunnel.py` 只改路徑一行。✅
8. **bootstrap keysync**：既有檔先經 `llmgw_exists` 三態判斷（`deploy/bootstrap.sh:601`）；寫入走 root `install -m 600 -o llmgw`（`:686`，擁有者 llmgw、0600）；密碼只經 shell 變數與暫存檔（builtin `printf`，不上 argv），`test_password_never_leaks` 守著；F2／H6／M-F1／M-F2／H3／M-F3 註解與程式皆原封未動。✅（註解誇大見低-4）
9. **install.sh**：取碼改驗 `ops-scripts/` 三件（`:174-185`），安裝來源改 `TAG_DIR/ops-scripts/`；`--check` 仍三態（`check_unit` 0／1／2，`check_keysync` 只回 0／1——皆為可判定項，無需 2）；隧道仍絕不當場 restart（`:447-461` 延後 60 秒＋已排跳過＋失敗只警告）。v5 改手動升級後部署 session 仍走隧道（docs 4.2 經 4711 的 `ssh -J`），所以這條仍然成立且仍被遵守。✅
10. **docs/deploy.md**：流程（拉取→sha→解碼→解密→驗證→原子換→mtime 重讀→失敗保留）、檔名、變數名、openssl 參數、token 甲乙、secret 增收表、fail-closed 段（#12 中-1(3) 已補在 4.2）、三態表、`failed=` 全表，皆與 v5-plan／實作一致；無 IPv4（除 127.0.0.1／0.0.0.0）、無機器名、無 key 值（只有變數名與 `<…>` 佔位）。✅（436 措辭見低-2）
11. **test_policy**：註解已照實改＋新增 `test_v4政策檔被拒`（#12 中-1(1)(2) 已修）。✅

## 發現

### 中-1　`same_sha` 短路不看本機 key 檔，key 檔單獨損毀會沉默卡死

- **檔案:行號**：`ops-scripts/key-sync:302-304`
- **問題**：sha 相同就直接回 `ok`，不確認 `keys.env` 是否存在、是否還驗得過。若 `keys.env` 在帶外被刪或寫壞（key-sync 自己永遠不會這樣做，觸發需人工誤刪或同類事故；同目錄整丟反而會因 `.keysync.sha` 一起丟而自癒，只有單獨損毀會卡）：
  - 刪失：log 報 `ok sha 未變`，`keys.env` 永遠不回來；之後若有人升級（`install.sh` 會補空 `keys.env` 並重啟 lgwd），服務帶著零把 key 上線、`available` 全 false，直到下一次有人 push 新加密檔。
  - 寫壞：lgwd 重讀失敗沿用記憶體舊值（服務暫時沒事），但下次重啟 `lgwd` 直接起不來（`KeyStore` 讀不到就丟）。
  - 最壞的是修復陷阱：照文件手動 `systemctl --user start key-sync.service` 同樣回 `ok`，操作者得自己想到去刪 `.keysync.sha`。
- **證據**（`.work/review13/`，假 gh，sha 與 `.keysync.sha` 相同、`keys.env` 不存在）：
  ```
  keysync INFO ok sha 未變，不動作（sha=11f6ad8ec52a）
  rc=0
  ```
  跑完 `state/` 底下仍無 `keys.env`。測試也只蓋到「缺 key 檔＋sha 變」（`test_keys_env不存在時建立且0600`），缺「缺 key 檔＋sha 同」的反向斷言。
- **建議**：spec（v5-plan §1.2 第 2 步「不碰 key 檔」指不修改）相容的修法——`same_sha` 分支先對本機檔做唯讀驗證（存在＋`KeyStore` 驗得過）；不過才走完整拉取流程（大概率以 `same_content` 收斂並順手寫回 sha）。成本是每次 tick 多一次 stat＋parse。另加一條 `test_sha相同但本機key檔缺失時重拉`。

### 低-1　`lgwd/policy.py` 的 docstring 還在講 SIGHUP

- **檔案:行號**：`lgwd/policy.py:3`
- **問題**：「服務啟動時不啟動、SIGHUP 時沿用舊的」——SIGHUP 整組已拿掉（`main.py` 無 handler、`llmgateway.service` 無 `ExecReload`），後半句現在是錯的。
- **證據**：`grep -n 'SIGHUP' lgwd/*.py` 只剩 `main.py:7`（歷史說明，正確）與本行。
- **建議**：改成「啟動時不合法就不啟動；v5 無熱重載，換政策要重啟（見 docs/deploy.md 4.2）」。

### 低-2　`docs/deploy.md:436` 把 fail-closed 的機制講反了

- **檔案:行號**：`docs/deploy.md:436`
- **問題**：「v4 的欄位（`cooldown_buffer_seconds`、`resets_at_max_ahead_seconds`）在 v5 會被拒」。實際機制是缺 `until_max_ahead_seconds` 才被拒；v5 檔外加兩個舊欄位照樣載入（`tests/test_policy.py:122-127` 剛釘住）。照字面讀，後人會以為「清掉舊欄位就好」，但真正要補的是新欄位。
- **建議**：改成「v4 政策檔因缺 `until_max_ahead_seconds` 會被拒（fail-closed，`lgwd` 起不來）；自訂過政策檔要補新欄位，舊欄位留著無妨」。

### 低-3　`LGWSYNC_TIMEOUT` 非法值讓 key-sync 連 `--help` 都 traceback

- **檔案:行號**：`ops-scripts/key-sync:339-341`
- **問題**：`default=float(os.environ.get(ENV_TIMEOUT) or DEFAULT_TIMEOUT)` 在 `build_parser()` 裡裸跑；值非法（例 `abc`）時 `ValueError` 在 `main()` 的 try 之外炸開，連 `--help` 都印 traceback（exit 2 的 argparse 流程走不到）。無秘密外洩（traceback 只有原始碼行），純粹是操作體驗與 timer 誤報（`unexpected`）。
- **證據**：`.work/review13/` 實測 `LGWSYNC_TIMEOUT=abc … key-sync --keys …` → `ValueError: could not convert string to float: 'abc'` 全頁 traceback。
- **建議**：解析失敗就退回預設並記一行 warning（CLI `--timeout` 的 `type=float` 照舊由 argparse 擋）。

### 低-4　bootstrap「既有的檔案絕不覆蓋」註解言過其實

- **檔案:行號**：`deploy/bootstrap.sh:591-594`（註解）對 `:620-623`＋`:686`
- **問題**：註解說既有 `keysync.env` 絕不覆蓋；實際是「有非空密碼行才不動」——既有檔存在但無密碼行（空檔、只剩註解、`VAR=` 空值）時會被 `install` 蓋掉。行為本身合理（把缺的補上），錯的是註解的絕對化說法；沿用此模式的 rendezvous 段沒這種註解。
- **建議**：註解改成「已有非空密碼行就不動；否則寫入（空檔／缺行視為沒放）」，與 `:620` 的 grep 語意一致。

### 低-5　lgwd 重讀失敗把新戳先記下，下次請求不再重試

- **檔案:行號**：`lgwd/main.py:195-202`
- **問題**：`reload_keys_if_changed` 在 `reload_keys()` 之前就把 `_keys_stamp` 推進到新值；失敗後戳已是新的，後續請求直接 `return False`，要等檔案「再變一次」才重試（docstring 有如實記載）。key-sync 只寫驗過的檔，所以實務觸發限於人工手改寫壞；且寫壞修好必動 mtime／inode，重試自然發生。屬可接受的語意，但與舊 SIGHUP「每次都試」不同。
- **建議**：記錄即可；若要改，成功才推進戳（代價：修好前每個請求多一次失敗重讀）。不擋 PR。

### 低-6　`install.sh --check` 把「讀不到 keysync.env」報成「沒放密碼」

- **檔案:行號**：`deploy/install.sh:296-299`
- **問題**：`grep` 讀檔失敗（權限被改壞等）與「檔裡沒那行」走同一個分支、同一個訊息（「沒有非空的 …」）。`--check` 的三態分類不受影響（都是 1），只是排錯訊息把人往錯的方向指。
- **建議**：先判可讀性（`[[ -r … ]]`），讀不到就另報一行（參考 bootstrap `step_check` 的 M-F3 做法：分不出就不分類，記 fail 的精神）。

### 低-7　`key-sync.timer` 無 `Persistent=true`，重開機後首拉晚 10 分鐘

- **檔案:行號**：`ops-scripts/key-sync.timer:1-12`
- **問題**：`OnActiveSec=10min`＋`OnUnitActiveSec=10min`，機器停機期間若有人 push 新 key，開機後還要再等約 10 分鐘才拉；`lgwd` 這段時間照用舊 key（不斷線，只是舊）。`install.sh` 的 enable＋「沒在跑才 start」邏輯不受影響。
- **建議**：`[Timer]` 加 `Persistent=true`（停機期間錯過的 tick 開機後補跑一次）。一行，不擋 PR。

## 附錄：本次審查做過的驗證

- `python3 -m unittest discover -s tests -t .` → 234 tests OK；`openspec validate gateway-v0 --strict` → valid。
- `git diff origin/scope/gateway-v5...HEAD` 全 21 檔逐檔讀過（含 `ops-scripts/key-sync` 367 行全文、`lgwd/main.py`、`bootstrap.sh` keysync 段、`install.sh` keysync 段、`docs/deploy.md` 全文）。
- 全庫 grep：`remote-deploy`／`deploy-keys`／`deploy-service`／`forced command`／部署 secret 名／`SIGHUP`／`ExecReload`／`authorized_keys`／`deploy-pubkey`（殘留皆為歷史敘述或 plan／design 基準文件）；IPv4 掃描（除 loopback／0.0.0.0 無命中）；`deploy/` 舊路徑引用（無命中）。
- 實驗（`.work/review13/`，假 gh／假 openssl，不碰真 gh／ssh／網路）：(1) same_sha＋缺 keys.env → `ok` 且檔仍缺（中-1 證據）；(2) `LGWSYNC_TIMEOUT=abc` → traceback（低-3 證據）。
- v5-plan §1.2／§1.3／§2.1／§2.2／§3.2／§7 與實作逐條對過；#12 中-1 的三項（測試改名＋新測試＋文件段）確認本單都已落實。

未讀任何憑證檔或密碼檔；未使用 ssh／gh／push；未觸發 workflow；只在 `review/v5-keysync` 寫檔與 commit，未動其他 branch、未 `git switch`。

## 複審（2026-10-12，`2eaafbe..b7cc9f1`）

複審者：reviewer-13（opencode＋Muse Spark 1.3 Contributor；作者是 DeepSeek 與 Space Bunny，不是審查者）。只審查，除本節外未改任何程式。

範圍：上一輪報告之後的 12 個 commit（`git log 2eaafbe..HEAD`）：`0be2555`（中-1）、`14e45a2`（低-1）、`35089b9`（低-2）、`e16fe89`（低-3）、`74a9426`（低-4）、`06e5614`（低-5）、`c838061`（低-6）、`de3a737`（低-7）、`5688524`（bootstrap gh-auth）、`481a7ff`（install.sh --check 驗 repo）、`cda29a5`（deploy.md）、`b7cc9f1`（v5-plan＋openspec）。

### 1. 中-1、低 1～7 逐條核對（全修好）

- **中-1（same_sha 自我修復）**：`ops-scripts/key-sync:292-301` 新增 `_local_keys_intact()`（唯讀 `KeyStore(keys_path)`，`KeyFileError`／`OSError` 回 False，不寫任何東西）；`:318-323` 的 same_sha 分支先驗本機檔，完好才短路，否則往下解密重寫。模組說明 `:7-9` 照實改（「不碰」是不修改）。空檔算完好（`lgwd/keys.py:27-42` 的 `_parse` 把空行／註解略過，空檔驗得過＝什麼都沒部署），不會每 10 分鐘重寫。測試 `tests/test_keysync.py:230`（缺檔重拉）、`:247`（寫壞重拉）、`:258`（空檔仍短路、mtime 不動）：三條連同 timeout 六條一起跑全過（`Ran 6 tests … OK`）。
- **低-1（policy.py SIGHUP 殘留）**：`lgwd/policy.py:1-10` 改成「只在啟動時讀；v5 沒有熱重載、沒有 SIGHUP，換政策要重啟；key 檔另由 mtime 重讀」。`grep -n SIGHUP lgwd/*.py` 只剩 `policy.py:7`（寫明沒有）與 `main.py:7`（寫明不再用）；`deploy/llmgateway.service` 無 `ExecReload`。測試 `tests/test_main.py:213` 斷言三個模組的 SIGHUP 提及必須帶「沒有／不再／拿掉／移除／v5／無熱重載」字樣。✅
- **低-2（fail-closed 講反）**：`docs/deploy.md:462` 改成「被拒的是缺欄位：v4 檔缺 `until_max_ahead_seconds` 才被拒；舊欄位留著無妨，loader 忽略」。與 `lgwd/policy.py:122-124`（只驗新欄位為正整數）及 `tests/test_policy.py:122-127`（舊欄位照樣載入）一致。✅（文件改，無新測試，合理）
- **低-3（LGWSYNC_TIMEOUT 非法值 traceback）**：`ops-scripts/key-sync:337-352` 新增 `_timeout_from_env()`（非數字／非有限／≤0 回中文錯誤），`:355` 的 `build_parser(timeout_default)` 不再裸 `float()`，`:381-390` 在 argparse 之後印 `bad_config` 回 `EXIT_CONFIG=3`；`--help` 因 argparse 先 `SystemExit(0)` 而不受影響。測試 `tests/test_keysync.py:509-539`（abc/0/-5/nan/inf：非零、`bad_config`、無 Traceback、舊 key＋舊 sha 不動、gh 未被叫；`--help` 仍 0；合法 0.5s 真觸發 `gh_timeout`）。✅
- **低-4（bootstrap「絕不覆蓋」誇大）**：`deploy/bootstrap.sh:620-625` 註解改成「已有非空密碼行就不動；空檔／只有註解／`VAR=` 空值視為沒放，會被寫掉補上」。行為（`:652` 的 grep）本來就是這個判準。測試 `tests/test_bootstrap.py:724` 蓋三種狀態（寫入＋0600）。✅
- **低-5（重讀失敗先推進戳）**：`lgwd/main.py:190-206` 改成成功才推進 `_keys_stamp`，失敗戳不動→下個請求再試；docstring 照實改。測試 `tests/test_main.py:284`（三請求三次重試、期間舊 key、修好下個請求讀到、戳推進後不再重讀）。✅（每請求多一次失敗重讀是接受的代價）
- **低-6（讀不到 vs 沒那行同訊息）**：`deploy/install.sh:340-343` 先 `[[ ! -r … ]]` 另報「讀不到解密密碼檔（…，檢查權限）」，三態不變（都是 1）。測試 `tests/test_install.py:812-824`（000 權限時見「讀不到／檢查權限」且不見「沒有非空的」；root 讀得到 000 時 skip）。✅
- **低-7（timer 無 Persistent）**：`ops-scripts/key-sync.timer:10-12` 加 `Persistent=true`。測試 `tests/test_keysync.py:500` 斷言。✅

### 2. gh (B) 實作檢查（全符合）

- **以 llmgw 身分執行**：bootstrap `deploy/bootstrap.sh:167-172` 的 `run_as_llmgw` 即 `sudo -n -u "$USER_NAME"`（預設 llmgw），`step_gh_auth`（`:326-346`）經它跑 `gh auth status`，開頭先 `sudo_refresh` 防假 todo；install.sh `deploy/install.sh:292-303` 目前是服務帳號就直跑，否則 `${LGW_SUDO:-sudo} -n -u "$service_user"`（`service_user` 預設由 `HOME_DIR` 推，`:286`），`timeout` 有才包。✅
- **只讀 metadata，不讀檔案內容**：`grep contents/ deploy/install.sh` 零命中；`install.sh:323` 只跑 `gh api "repos/${KEYS_REPO}" --jq .full_name`（驗 `repo_out == KEYS_REPO`）；bootstrap 只跑 `gh auth status`（`:333`），連 repo 都不問。讀檔案內容的是 key-sync 本體（`key-sync:149` 的 `contents/keys.env.enc`），不在這兩支的職責內。✅
- **未登入 → todo、結束碼 3、照常印摘要**：`step_gh_auth` 非零（非 127）走 `:339-343`：`log_step gh-auth todo`＋`record_todo`（寫明 `sudo -u llmgw -i` 後 `gh auth login` 再重跑），`return 0` 不設 `step_failed`，後面步驟照跑；主程序 `:1071-1072` 有 TODOS 即 exit 3，`run_step`（`:1045-1054`）只在 `step_failed` 才中途停，摘要（`print_summary`）兩條路都印。測試 `tests/test_bootstrap.py:1070`（rc=3、`[gh-auth] todo`、含 `gh auth login`／`sudo -u llmgw -i`／重跑、`[linger]` 照跑、摘要照印、無 fail）與 `:1095`（`--check` 模式同樣 todo＋3 且 `useradd`／`install` log 全空）。✅
- **--check 三態正確**：`install.sh:307-331`：rc=127（找不到 gh）→2；rc=124（逾時，`LGW_GH_TIMEOUT` 預設 30s）→2；rc≠0（沒登入／token 失效）→1；`repo` rc=124→2，rc≠0 或回值≠repo→1；其餘密碼／failed 段不變。測試 `tests/test_install.py:752`（`test_check_keysync_states`）：ok→0（含 `gh 已登入，且讀得到 FATESAIKOU/LLMGateway-keys` 與 argv 含 `api repos/FATESAIKOU/LLMGateway-keys --jq .full_name`）、logged_out→1、repo_denied→1、missing→2、slow→2，全過。✅
- **沒有 token 或密碼進 argv／log**：bootstrap 的 `out`（`:333`）只為吞掉輸出，從未被印出（`grep "$out"` 無輸出引用）；`record_todo` 只寫固定指令字串。install.sh 的 `auth_out`／`repo_out`（`:305-323`）只做 rc 與字串比對，`log INFO` 行全是固定中文（`grep "log INFO.*auth_out\|repo_out"` 零命中）。實做面：bootstrap 假 gh 測試斷言 gh-argv 無 `auth login`／`auth token`（`:1049` 前後）；install 測試斷言 argv 只有 `auth status` 與 `api repos/… --jq .full_name`。`gh auth status` 輸出本身不含 token 值，註解（`:332`）有寫明仍不印。✅
- **文件一致**：`docs/deploy.md` 1.A.4 第 3 步（`:114-129`，gh-auth 三態表＋`sudo -u llmgw -i` 登入）、1.A.5 步驟清單（`:151`）、1.A.6 兩列（`:179-180`）、1.9（`:312` 本人已選 B＋風險接受＋確認指令）、4.4（`:484-487` 服務帳號＋只問 repo＋三態＋keysync.env 三種訊息）、4.5（登入外洩重登入）、5.1（`gh_not_found` 補 repo 確認指令）；`docs/research/v5-plan.md:39-40`（選 B＋後果＋--check 驗 repo）、`:82`、`:115`；openspec `key-vault/spec.md`（使用前提＋B＋讀不到 repo＋服務帳號驗）、`design.md:222-227`（B＋風險＋gh-auth 只查不登入）、`diagrams.md`、`proposal.md` 皆同步。`openspec validate gateway-v0 --strict` → valid（見 §4）。✅

### 3. 新問題（1 低＋2 觀察，不擋 PR）

- **新-低-1　管理帳號直跑 `install.sh --check` 時 `sudo -n` 失敗被算成「gh 沒登入」（`deploy/install.sh:298-317`）**：`current_user != service_user` 且 sudo 快取缺時，`sudo -n -u llmgw gh …` 回 rc=1（`sudo: a password is required`），掉進 `:315` 的「沒登入或 token 失效」回 1；同理 sudo 本體缺席的 127 會被報成「找不到 gh」。分類仍是非零（服務本身不受影響），錯的只是指錯方向。bootstrap 路徑（以 llmgw 跑，直連無 sudo）與 `LGW_SERVICE_USER` 覆寫的測試路徑都不踩這條。建議：判 `sudo` 自己的失敗（stderr 含 `sudo:` 或外層先 `command -v sudo`）回 2「無法確認」。記錄即可，不擋 PR。
- **觀察-1　`bad_config` 不在 5.1 排錯表**：低-3 引入的新前綴（exit 3，非 `failed=` 體系），`grep bad_config docs/` 零命中。屬 config 錯誤而非同步失敗，不列發現；要補一行亦可。
- **觀察-2　`repo_out == KEYS_REPO` 的字串比對偏嚴**：真 `gh` 的 `--jq .full_name` 預期回裸名（假 gh 照此模擬），若某版 gh 包引號或多空白會誤判 1。不能用真 gh 驗（規矩），測試內一致；僅提示，不列發現。

### 4. 測試（本次實跑，直接貼）

`python3 -m unittest discover -s tests -t .`

```
Ran 247 tests in 166.862s

OK
```

（上一輪 234；本次 +13：中-1 三條、低-3 三條、低-4／低-5／低-6 各一、低-1 兩條合一計、gh-auth 四條、install 三態擴充，淨 +13 合理。）

`openspec validate gateway-v0 --strict`

```
Change 'gateway-v0' is valid
```

針對性抽跑（證據）：`test_keysync` 六條（same_sha 三＋timeout 三）`OK`；`test_main`＋`test_bootstrap` 七條（SIGHUP、重試、補密碼、gh-auth ok/todo/fail/check）`OK`；`test_install.test_check_keysync_states` `OK`。

### 結論

**可以開 PR。**中-1 與低 1～7 皆真修好（檔案:行號＋測試如 §1）；gh (B) 五項全符合（§2）；新問題只有一低二觀察，不擋（§3）；全量 247 tests OK，openspec valid（§4）。

本次複審未讀任何憑證檔；未用 ssh／gh／push；未觸發 workflow；實驗只用既有測試（未新增 `.work/` 檔）；只在 `review/v5-keysync` 活動。
