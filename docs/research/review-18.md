# 審查 #18：sudo 快取跟著 parent pid＋sshd -T 失敗記 fail（reviewer-18）

審查對象：`git diff main...HEAD`（branch `review/fix-sudo`；#18 的修正，共 4 個 commit）。

- `3ddface` fix：sudo 快取跟著 parent pid，每次 `sudo -n` 前先在同層級驗證
- `4e0a1aa` fix：`sshd -T` 拿不到輸出記 fail，不當成未啟用 PubkeyAuthentication
- `4d71da8` test：假 sudo 模擬快取跟著 parent pid，補 #18 三個情境
- `aa74dcb` docs：1.A.2 註明 annotated tag 的 `is not a commit!` 是正常警告

審查者：reviewer-18（作者是 Step 5 Preview，不是審查者；PM 是 llmgateway-f8）。只審查，未改任何程式。未讀任何憑證檔或密碼檔；未 ssh／gh／push／觸發 workflow；實驗檔在 `.work/`（gitignored，不進 PR）。

## 結論

**可以開 PR。** 高 0、中 0、低 2、資訊 3。無阻擋項；低／資訊項可直接開 PR，後續順手修或記錄即可。

## 測試

`python3 -m unittest discover -s tests -t .`

```
Ran 250 tests in 222.986s

OK
```

（`tests.test_bootstrap` 單檔：28 tests，164s，OK；三個新案例單獨跑：3 tests，34s，OK。見 §4 證據。）

## 逐項核對（6 點，都有證據）

### 1. 每個 `sudo -n` 之前，同一 shell 層級都有 `sudo -S -p '' -v`（重點 1）✅

- 直接 `sudo` 呼叫全檔只有 5 處（`deploy/bootstrap.sh:149,155,178,183,191`）：
  - `:149` `sudo_refresh` 有密碼檔分支（`-S -p '' -v < "$SUDO_PASS_FILE"`）。
  - `:155` `sudo_refresh` 無密碼檔分支（`sudo -v`，互動）。
  - `:178` `sudo_precheck` 本體（`-S -p '' -v < "$SUDO_PASS_FILE"`，無密碼檔時 `:177` 直接 `return 0`）。
  - `:183`、`：191` 是僅有的兩個 `sudo -n`，分別在 `run_root`（`:181-184`）與 `run_as_llmgw`（`:186-192`）內，且各自第一行就是 `sudo_precheck || return 1`。同一函式、同一層級，中間沒有多一層 subshell（`run_as_llmgw` 中間的 `uid="$(id …)"` 只是本地 `id`，不經 sudo，不換 shell pid）。
- 沒有漏網的直接 `sudo -n`：`grep -nE '^\s*sudo '` 全檔只有上述 5 行；管線 `grep sudo | grep '|'` 無 `sudo` 進管線；`$(run_root …)`／`$(run_as_llmgw …)` 共 15 處（`:353,430,554,667,836,850,987,1037` 等）全部走兩個包裝函式，每進一次 `$(…)` 就在該子 shell 內先 `-v` 再 `-n`，正是本次要修的語義。
- 測試側守衛：`tests/test_bootstrap.py` 新增 `assert_every_sudo_n_was_validated`（`:1188-1203`，讀 `sudo-ppid.log` 逐 PPID 驗證），三個新案例最後都調它（`:1239` 等）；新碼全過（§4）。

### 2. 密碼不上命令列、不進 log、不進錯誤訊息；無密碼檔行為不變（重點 2）✅

- `SUDO_PASS_FILE` 在程式中只出現在 4 處（`:43` 賦值、`:82` 存在檢查、`:149`、`:178`），兩次使用都是 `sudo -S -p '' -v < "$SUDO_PASS_FILE"`：密碼只從 stdin 進，argv 只有 `-S -p '' -v`（`-p ''` 連提示都不印），不上 `ps`。
- `run_root` 本體仍是 `sudo -n "$@" </dev/null`（`:183`）；`run_as_llmgw` 的 `sudo -n … </dev/null`（`:191`）不變。precheck 的 `< "$SUDO_PASS_FILE"` 是前一個指令的重導向，不會餵進 `sshd -T` 等被包裝指令（`:428-429` 註解已寫明）。
- log／錯誤訊息：`sudo-argv.log` 記的是 `$*`（選項＋子命令，無密碼值）；`log_step … fail` 訊息只有靜態中文（`:150`、`：432` 只有 `rc=數字`）；`test_password_never_leaks`（舊測試）在本次全量 250  tests 中照常通過。
- 無密碼檔行為不變：`sudo_precheck` 首行 `[[ -n "$SUDO_PASS_FILE" ]] || return 0`（`:177`，shell 驗證回 0 無輸出）；`sudo_refresh` 的 `else` 分支（`:155`）原封未動；各步驟開頭的 `sudo_refresh` 共 9 處（`:298,326,349,385,420,576,633,730,895,962`）一行未動。無密碼檔的舊測試（大多數案例都不帶 `--sudo-pass-file`）全過。

### 3. `sshd -T` 拿不到輸出記 fail，不記 todo（重點 3）✅

- `deploy/bootstrap.sh:427-435`：`local sshd_out="" sshd_rc=0` 後 `sshd_out="$(run_root sshd -T … 2>/dev/null)" || sshd_rc=$?`，再 `if [[ $sshd_rc -ne 0 || -z "${sshd_out//[[:space:]]/}" ]]` 即 `log_step sshd fail "無法執行 sshd -T（rc=${sshd_rc}），不判定 sshd 設定"`＋`step_failed=1`＋`return 1`。rc 非零與空白輸出（ zawieszone 含只有空白）兩路都 fail；不再掉進下面的 `PubkeyAuthentication`／`DenyUsers` 等 todo 分類。
- `set -euo pipefail` 語義已驗：`local` 拆在前一行，賦值行 `o="$(false)" || rc=$?` 的 `||` 能接住，rc=1 且不退出（本地 bash 實測 `rc=1 o=[] ok1`）；`2>/dev/null` 只蓋錯誤字串，rc 照樣進變數，訊息只含數字無敏感值。
- 舊碼對照：`main` 版是 `sshd_out="$(run_root … || true)"`（吞掉 rc），空輸出接著被判 `pubkey_auth != yes` → 恆 todo；新碼修的正是此路。測試 `test_sshd_T_failure_is_fail_not_todo`（`:1258-`）斷言 `[sshd] fail 無法執行 sshd -T` 且 `NotIn [sshd] todo`／`NotIn PubkeyAuthentication`，新碼通過、舊碼失敗（見 §4）。

### 4. 假 sudo 的 PPID 模擬忠實，三個新案例在舊程式上真的失敗（重點 4）✅

- 假 sudo（`tests/test_bootstrap.py:143-153`）：`LGW_SUDO_CACHE_PPID=1` 時 `-v` 記 `$PPID` 到單檔、`-n` 比對 `$PPID`，不符即 `sudo: interactive authentication is required`＋exit 1。方向正確：重現「`$(…)` 子 shell 的 PPID 與主腳本不同、只在步驟開頭刷新的快取拿不到」。簡化見低-2，但不影響本次判定。
- 三個新案例：
  - `test_sudo_cache_ppid_first_run_no_rendezvous`（`:1218-1239`）：第一次跑、PPID 模式、rc=3，真 todo（gh 未登入）而非假 fail，摘要讀得到隧道公鑰，`[sshd] ok`，`[check] todo` 非 fail，最後 `assert_every_sudo_n_was_validated`。
  - `test_sudo_cache_ppid_rerun_reads_version_file`（`:1241-`）：第二次跑讀得到版本檔、`[install] ok 程式版本已是 …`、走到 rendezvous 並寫好 `tunnel.env`（真機曾 fail 處）。
  - `test_sshd_T_failure_is_fail_not_todo`（`:1258-`）：`sshd_T_fail=1` → rc=1、`[sshd] fail` 非 todo。
- 舊碼重現（`.work/repro_old.py`，`git show main:deploy/bootstrap.sh`＋新測試 harness，`.work/` 不進 PR）：
  - `Ran 3 tests … FAILED (failures=3)`，三條全紅（含 `sudo: interactive authentication is required` 與舊 sshd 的 `[sshd] todo … PubkeyAuthentication` 誤判）。
- 新碼對照：同三條在 HEAD 上 `Ran 3 tests … OK`（34s）。測試有效：會紅也會綠。

### 5. 前幾輪修好的東西沒被改壞（重點 5）✅

- `git diff main...HEAD -- deploy/bootstrap.sh` 的 `@@` 只有兩段：`@@ -160,12 +160,32 @@`（sudo 包裝）與 `@@ -400,8 +420,19 @@`（sshd）。其餘零變更。
- F2（`llmgw_exists` 三態＋經 sudo 讀）：`:200-215` 原封，15 處 `$(run_as_llmgw cat …)` 照舊；M-F3（讀不到 `tunnel.env` 記 fail 不分類）：`:987-1000` 原封；H6（`run_as_llmgw` 先 `cd BOOT_HOME || cd /`）：`:191` 原封；M-F2（`ensure_llmgw_dir` 經 llmgw 身分 `mkdir -p -m`＋`chmod`）：`:222-227` 原封；H3（`run_step` 接 fail 才印摘要後 `exit 1`）：`:1076-1085` 原封；M-F1（`step_sshd`／`step_gh_auth` 開頭 `sudo_refresh`）：`:349`、`:420` 原封；gh-auth 步驟（沒裝 fail／沒登入 todo＋rc 3／已登入 ok，`gh auth status` 不印輸出、不代做登入）：`:346-366` 原封。全量測試中舊的 `test_sudo_no_cache…`、`test_home_reads_go_through_sudo`、`test_gh沒登入時記todo…` 等照常通過（250 OK）。

### 6. `docs/deploy.md` 的 annotated tag 註解（第 4 個 commit）✅（小註解見資訊-3）

- `docs/deploy.md:67` 新增一行：annotated tag 的 `is not a commit!` 是正常警告，照樣跑 1.A.3。與本次 sudo／sshd 主線無交互；措辭與周圍流程一致，無機器名／IP／key 值。

## 發現

### 低-1　註解混入簡體字「判断」

- **檔案:行號**：`deploy/bootstrap.sh:425`
- **問題**：`# sudo 驗證失敗都屬於這類），後者才是照設定判断。`的「判断」是簡體，周圍皆繁體。
- **證據**：`sed -n '423,426p' deploy/bootstrap.sh` 可見；`grep -n 判断 deploy/bootstrap.sh` 僅此一處。
- **建議**：改為「判斷」。不阻擋，可與下批順手修。

### 低-2　假 sudo 是單槽快取，真機是多槽；本次判定不受影響

- **檔案:行號**：`tests/test_bootstrap.py:143-153`（寫 `sudo-v-ppid` 單檔、每次 `-v` 覆寫）
- **問題**：真機沒有 tty 時的快取是「每個 PPID 各記一筆」；假 sudo 只留最後一次 `-v` 的 PPID。若出現 A 子 shell `-v`→主 shell `-v`（覆掉 A）→A 子 shell `-n` 的交錯，假 sudo 會判失敗而真機會過。bootstrap 是循序執行且新碼每個 `-n` 前同 PPID 必有 `-v`，所以本次三案例不受影響（新碼 OK、舊碼 3 全紅已驗），僅是模擬精度說明。
- **證據**：同 §4 的新舊碼對照結果；`assert_every_sudo_n_was_validated`（`:1188-1203`）是逐 PPID 集合驗證，比假 sudo 的單槽更嚴，新碼兩者都過。
- **建議**：二選一即可：（a）註解加一句「單槽簡化：bootstrap 循序執行，每個 `-n` 前同層級必有 `-v`，精度足夠」；或（b）改為每 PPID 一檔（`sudo-v-$PPID`）。不阻擋。

### 資訊-1　`sudo_precheck` 失敗時的 stderr 會進捕獲變數，僅 mid-run 失效才觸發

- **檔案:行號**：`deploy/bootstrap.sh:176-179`、`：353`（`out="$(run_as_llmgw gh auth status 2>&1)"`）
- **問題**：`sudo_precheck` 的 `sudo -S … -v` 未 `2>/dev/null`；若密碼在步驟中途失效（步驟開頭 `sudo_refresh` 之後才壞，實務幾乎不會發生），`gh-auth` 的 `2>&1` 捕獲會把 sudo 錯誤字串當 gh 輸出，且 `rc=1` 會被判「尚未登入」todo 而非 fail。`sshd` 路因 `2>/dev/null`＋rc 分支不受影響；`$(run_as_llmgw cat …)` 路的 stderr 直接上終端再記 fail，分類正確。
- **證據**：程式碼走讀；各步驟開頭 `sudo_refresh`（9 處）已先擋掉「一開始就錯」的情況，全量測試無此路徑。
- **建議**：保持現狀即可；若要修，加 `2>/dev/null` 一行並補一條「密碼中途失效記 fail」測試，但邊際效益低。

### 資訊-2　`llmgw_exists` 在 precheck 失敗時回 1 而非 2，僅 mid-run 失效才觸發

- **檔案:行號**：`deploy/bootstrap.sh:200-215` 配 `:181-192`
- **問題**：`llmgw_exists` 內兩次 `run_as_llmgw test …` 若因 precheck 失敗回 1，會走 `if ! … test -d` → `return 1`（不存在）而非 `return 2`（無法判斷），呼叫者可能把「讀不到」當「不存在」。同樣被步驟開頭 `sudo_refresh` 遮蔽，僅 mid-run 失效才觸發。
- **證據**：走讀；`step_install:552-563`、`step_check:984-1000` 等讀不到→fail 的分支在正常路徑不受影響。
- **建議**：記錄即可；真要修需讓 `run_as` 區分「指令失敗」與「precheck 失敗」，改動大於收益。

### 資訊-3　annotated tag 警告在本地 `file://` 未重現，不推翻文件

- **檔案:行號**：`docs/deploy.md:67`
- **問題**：本地以 `file://`＋`--depth 1 --branch` 測 annotated tag 未見 `is not a commit!`（直接 detached HEAD 成功），可能與 git 版本或 GitHub 傳輸有關。真機實測看到警告即以真機為準，文件寫「可能印一行…是正常的」已留餘地。
- **證據**：`.work/` 外的 `/tmp` 對照實驗（`git clone --depth 1 --branch <annotated-tag> file://…`，exit 0、無該警告；`git log -1` 即 tag 標記的 commit）。
- **建議**：可選：補一句 git 版本或完整警告行（例如真機看到的前綴 `warning: …`），方便後人對照；不補也不阻擋。

## 補充觀察（非發現）

- 每次 `sudo -n` 多一次同層級 `sudo -v`（讀一次密碼檔＋一次 fork）：bootstrap 數十次呼叫，可接受；換來的是真機無 tty 下子 shell 快取正確。無需改。
- 效能數據：全量 250 tests／約 223s、`test_bootstrap` 28 tests／約 164s，三新案例／約 35s；耗時主要在反覆起 `bash bootstrap.sh` 沙箱，屬正常。
