# v0 審查：`deploy/bootstrap.sh`（reviewer, 2026-10-12）

> 範圍：`deploy/bootstrap.sh`（tasks 3.3）與 `tests/test_bootstrap.py`；對照需求（PM 給實作者的 bootstrap 任務單）、`deploy/install.sh`、`deploy/llmgateway-tunnel`、`docs/deploy.md` §1。
> 行號以 impl/gateway-v0-core 合併後的 `deploy/bootstrap.sh`（712 行）為準。
> 規矩：只讀 repo；未讀憑證檔、未用真 key 或密碼、未 ssh、未跑 gh。實驗都在 `.work/`（gitignored），只用假指令。
> 執行結果：`bash -n deploy/bootstrap.sh` 通過；`python3 -m unittest discover -s tests -t .` → 248 tests，**7 個失敗**（`test_bootstrap` 的 7 個，其餘全過）。
>
> 結論：**高 6 條、中 5 條、低 7 條**。密碼處理、`authorized_keys` 那一行的格式、不重產隧道金鑰、不印私鑰這幾項**符合需求**（見「符合需求的項目」）。但腳本是用「目前使用者就是 llmgw、sudo 是假的」的測試環境驗證的；在真機上，管理帳號讀不到 `llmgw` 的家目錄、`set -e` 被 `|| true` 關掉、`--check` 沒有 sudo，這幾個問題測試完全看不到，真機會出事。

## 7 個失敗測試：原因不只一個

已知原因成立：合併後的 `install.sh` 要求 release 有 `deploy/llmgateway-tunnel` 與 `deploy/llmgateway-tunnel.service`，測試裡假 `git clone`（`tests/test_bootstrap.py:293-308`）建的 release 沒有，install.sh 以 `取到的版本缺 deploy/llmgateway-tunnel…` 結束碼 1。

**但這不是唯一原因。** 我在 `.work/exp/` 的複本裡只補上這兩個檔，7 個仍失敗；失敗訊息變成 `[check] fail install.sh --check 未通過 (rc=1)`、`list 探針回 401，不是 200`。原因是假 curl 預設回 401（`tests/test_bootstrap.py:71`、`:333`），而合併後的 `install.sh --check` 只認 200（`deploy/install.sh:227`）。再把假 curl 預設改成 200，10 個測試全過。所以要修測試的兩處：假 release 補隧道執行檔與 unit；假 curl 預設回 200。

## 發現

### 高

#### H1　`step_x || true` 關掉了步驟裡的 `set -e`，任何指令失敗都被吞掉，還會印 `changed`

- 位置：`deploy/bootstrap.sh:659-702`（`step_prereq || true` … `step_check || true`），影響每個步驟函式裡的 `run_root`、`run_as_llmgw`、`sudo_refresh`。
- 問題：bash 在「`||` 左側」的脈絡裡（包含函式本體）一律不觸發 `errexit`。所以 `useradd` 失敗、`sudo -v` 密碼錯、`git clone` 失敗、`install.sh` 失敗、`systemctl restart` 失敗，函式都會繼續往下跑，最後照印 `[步驟] changed …` 並 `changed_count++`。只有明確寫了 `step_failed=1` 的幾處才會失敗。
- 證據：`.work/exp/e.sh`：`set -euo pipefail; f(){ false; echo "still ran"; }; f || true` 印出 `still ran`。測試沒抓到，因為假指令全都成功。
- 後果：密碼檔寫錯，`sudo -v` 失敗，之後每個 `sudo -n` 都失敗，但摘要全是 `changed`、結束碼可能是 0/3，PMO 以為裝好了。
- 建議：函式內每個會改東西的指令自己接 `|| { log_step … fail …; step_failed=1; return 1; }`；或主程序改成 `step_x; rc=$?`（不用 `|| true`）並在函式內用 `set -e` 不被關掉的寫法（例如在子 shell 外判斷回傳值）。並加一個測試：假 `sudo -n` 對某指令回非 0，腳本要回 1 且不印該步 `changed`。

#### H2　管理帳號直接讀 `llmgw` 家目錄底下的東西；家目錄是 0700／0750 時全部判斷失準

- 位置：`step_install` `:355`（讀 `current/.lgw-tag`）、`step_tunnel_key` `:388`（`-f` 隧道私鑰）、`step_deploy_key` `:439-449,468`（讀、`grep` `authorized_keys`）、`step_rendezvous` `:497,553`（`cmp` known_hosts 與 tunnel.env、python 讀 tunnel.env）、`print_summary` `:629-630`（`cat id_llmgw_tunnel.pub`）。
- 問題：這些路徑在 `~llmgw/` 底下，`~llmgw/.ssh` 是 0700 且屬於 llmgw；`useradd -m` 的家目錄在 RHEL 系是 0700、Ubuntu 22.04 起是 0750，管理帳號（非 root、非 llmgw）進不去。`[[ -f … ]]` 因無權限回 false，不是錯誤，所以一律被當成「不存在」。
- 後果（第二次執行 `changed` 不為 0，需求 1 不成立）：
  1. 隧道金鑰：每次都走「沒有」那條，再呼叫 `ssh-keygen`。目前沒毀掉金鑰只是碰巧：`ssh-keygen` 在 stdin 是 `/dev/null` 時問「Overwrite (y/n)?」讀到 EOF 就放棄；但這個失敗被 H1 吞掉並印 `changed 已產生隧道金鑰`。需求「已經有就絕不重產」要靠明確的存在檢查，不是靠這個巧合。
  2. `authorized_keys`：讀不到就當作不存在，`:467-471` 走 `: > "$tmp_auth"`，結果檔案被**只剩一行**覆蓋，其他行（例如 PMO 另外放的）被刪掉，違反「其他行保留」。
  3. `tunnel.env`：`:504-550` 的 python 只抓 `FileNotFoundError`；`PermissionError` 會讓 python 失敗、stdout 已被導向 `$tmp_env` 而變成空檔，H1 又吞掉錯誤，`:574` 用空檔覆蓋 `tunnel.env`，**把 `llmgw` 原有的設定清掉**。
  4. `known_hosts_rendezvous`、`current/.lgw-tag`：每次都判成不同，每次 `changed`、每次重啟隧道，也每次重跑 `install.sh`。
  5. `print_summary`：`-f $tunnel_pub` 為 false 印「（尚未產生）」，PMO 拿不到最要緊的隧道公鑰。
- 證據：測試是在「目前使用者＝家目錄擁有者」下跑的（`tests/test_bootstrap.py` 的假 sudo 以目前使用者執行），看不到權限問題。我無法在本機建立另一個使用者來重現，但行為是 POSIX 目錄權限的直接結果。
- 建議：所有讀 `~llmgw` 的動作都經 `run_as_llmgw`（`test -f`、`cat`、`cmp`）或 `run_root`；讀 `authorized_keys` 與 `tunnel.env` 改成 `run_as_llmgw cat` 到暫存變數；python 內容生成也改成吃 stdin。讀取失敗與「不存在」要分開（`test -e` 先判斷）。測試補一個「家目錄 0700、屬於另一個假身分」的情境（假 sudo 要真的切換 uid 不容易，可以用假 `run_as` 檢查 argv 是否經 sudo）。

#### H3　`install.sh --check` 在第一次執行必然不是 0，腳本因此以結束碼 1 收場，而且**摘要沒印**

- 位置：`:586-616`（`step_check`）、`:699-704`（主程序在 `print_summary` 之前就 `exit 1`）。
- 問題：建議的第一次執行是「不帶 `--rendezvous`」。這時 `tunnel.env` 是空的，隧道守護以 78 結束、停在 failed（`docs/deploy.md` 1.5），`install.sh --check` 的 `check_unit`（`deploy/install.sh:197-207`）對 `llmgateway-tunnel.service` 的 `is-active` 回 3，回傳 1。bootstrap 把 rc≠0 都當 `fail`（`:613-614`），`:700-702` 直接 `exit 1`，**不印隧道公鑰與 host key**，而這兩樣正是第一次執行要拿去交給 mylinuxpool-pm 與設 secret 的。
- 證據：`.work/exp/` 複本在 curl 回 401 時，stdout 結尾是 `[check] fail install.sh --check 未通過 (rc=1)`，之後沒有摘要，結束碼 1。一般情況（隧道未設定）也會是同樣的路徑。
- 另外：任何一步 `step_failed=1` 都 `exit 1` 而不印摘要（`:659-702` 每步後面各一個 `exit 1`），連已經成功產生的隧道公鑰也看不到。
- 建議：(1) 隧道設定還有 todo（`rendezvous` 未給或未就緒）時，`install.sh --check` 的 rc 1 應記為 `todo`（說明「隧道尚未設定，check 暫時不通過」）而不是 `fail`；rc=2（無法確認）也記 todo 或 fail 視情況，但要印出 rc。(2) 不論成功或失敗，離開前都印摘要（至少印已存在的公鑰與 host key），用 `trap … EXIT` 或把 `print_summary` 移到判斷之前。

#### H4　`--check` 不做 `sudo -v`，但之後的 `sudo -n` 需要快取；`--check` 在沒有 NOPASSWD 時一定失敗

- 位置：`:124-127`（`sudo_refresh` 在 `CHECK_ONLY` 直接 return 0）、`:142`（`run_as_llmgw` 用 `sudo -n`）、`:599-606`（`step_check` 在 check 模式下仍以 `run_as_llmgw` 跑 `install.sh --check`）。
- 問題：`--check` 要驗證時，`step_check` 需要 `sudo -n -u llmgw …`，但 check 模式從不 `-v`，也不讀密碼檔。沒有 NOPASSWD 也沒有先前快取時，`sudo -n` 回 1，腳本印 `[check] fail install.sh --check 未通過 (rc=1)`，結束碼 1。而 `docs/deploy.md` 建議的最後一步正是 `--check`。同理，H2 改成經 sudo 讀 `~llmgw` 之後，`--check` 也需要 sudo。
- 「只檢查」不代表不能 `sudo -v`：`-v` 只驗證、不改任何東西（需求第 3 點也只說密碼只能這樣用）。
- 建議：`sudo_refresh` 在 check 模式一樣執行（`-v` 不改系統狀態）；測試的「`--check` 不改任何東西」改成檢查 argv 沒有 `useradd`、`enable-linger`、`install`、`restart` 等，允許 `-v` 與唯讀指令。

#### H5　`step_sshd` 以一般使用者跑 `sshd -T`，在真機上會永遠 `todo`

- 位置：`:295`、`:330`。
- 問題：`sshd -T` 要載入 host key，非 root 會失敗（`no hostkeys available`）；Debian 系的 `/usr/sbin` 也常不在一般使用者的 PATH。輸出被 `2>/dev/null || true` 吞掉，`sshd_out` 為空，`pubkey_auth` 為空，於是**每次都**印 `todo sshd 未啟用 PubkeyAuthentication`。結果：結束碼永遠是 3，`changed=0、todo=0` 的「完全完成」無法達成；而且訊息是錯的（sshd 其實啟用了）。
- 建議：以 `run_root sshd -T -C …` 執行（在 check 模式也 `sudo -v`，見 H4）；`sshd -T` 失敗（命令不存在或非 0）時，印 `todo` 並寫「無法判斷 sshd 設定（原因：…）」，不要把「讀不到」當成「沒開」。`ss` 同理在 `/usr/sbin` 的系統用 `PATH=$PATH:/usr/sbin:/sbin`。

#### H6　以 `llmgw` 身分跑指令時，cwd 是管理帳號的目錄，`llmgw` 讀不到

- 位置：`:142`（`run_as_llmgw`）。
- 問題：`sudo -u llmgw …` 不改 cwd。手冊（新 1.A）要求從管理帳號家目錄底下的暫存 clone 執行腳本，cwd 通常就是那個目錄（Ubuntu 22.04 起家目錄預設 0750，`llmgw` 進不去）。`git clone` 在這種情況會以 `fatal: Unable to read current working directory: Permission denied` 結束；bash 與 python 也會抱怨 `getcwd`。這個失敗又被 H1 吞掉，印 `changed 已安裝版本`，但 `current` 不存在，到 `step_check` 才以「找不到 install.sh」收場。
- 建議：`run_as_llmgw` 改成先切到 `llmgw` 可讀的目錄，例如 `sudo -n -u llmgw env … sh -c 'cd "$HOME" && exec "$@"' sh "$@"`，或腳本一開始 `cd /`（root 的 `sudo` 繼承的就是 `/`）。腳本內其餘的相對路徑要先轉成絕對路徑（`--sudo-pass-file`、`--deploy-pubkey-file`、`--rendezvous-known-hosts` 若給相對路徑，`cd /` 之後會找不到，要在參數解析時用 `realpath` 轉絕對）。

### 中

#### M1　`install -d … "$share_dir"` 可能把 `~/.local`、`~/.local/share` 建成 root 所有

- 位置：`:371`。
- 問題：新帳號的家目錄通常沒有 `~/.local`。GNU `install -d -o llmgw <深層路徑>` 只把最後一層設成 llmgw，途中建出來的父目錄是執行者（root）的。之後 `install.sh` 以 `llmgw` 身分 `mkdir -p ~/.local/state`、`~/.local/bin`（`deploy/install.sh:295`）會因為 `~/.local` 屬於 root 而失敗。
- 備註：這是我對 GNU coreutils 行為的理解，沒有在 Linux 實機驗證（本機是 macOS 的 `install`）；建議在測試機確認。
- 建議：不要自己建，直接讓 `install.sh` 建（它本來就 `mkdir -p`）；要先建就用 `run_as_llmgw mkdir -p`。`~/.ssh`、`~/.config/llmgateway` 兩處（`:400,463,571,573`）家目錄第一層以下只有一層，影響較小，但同樣改成 `run_as_llmgw mkdir -p -m 700` 比較不會出錯。

#### M2　摘要裡的 host key 行帶著 `.pub` 的註解，註解通常是機器名

- 位置：`:637-640`。
- 問題：`/etc/ssh/ssh_host_ed25519_key.pub` 的內容是 `ssh-ed25519 <key> root@<機器名>`。腳本整行附在 `[127.0.0.1]:4711 ` 後面印出來。PMO 把輸出貼進工單或對話（public repo）時會帶出機器名，違反「不寫機器名」。需求寫的是 `[127.0.0.1]:4711 ssh-ed25519 …`。
- 建議：只印前兩欄（`awk '{print $1, $2}'`）。

#### M3　sshd 檢查的判斷太粗

- 位置：`:303-327`。
- 問題：(a) `AllowGroups`／`DenyGroups` 完全沒查，而手冊 1.3 要求要查；(b) `AllowUsers`／`DenyUsers` 只做完全相等比對，不認萬用字元（`*`、`?`）與 `user@host` 形式，會誤報 `todo`（如 `AllowUsers pmo llmgw@127.0.0.1`）或漏掉 `DenyUsers *`；(c) `sshd -T` 失敗時整串訊息誤導（見 H5）。
- 建議：至少處理 `AllowGroups`／`DenyGroups`（用 `id -Gn llmgw` 比對）、萬用字元（`case` 的 glob 比對）與 `user@host`（取 `@` 前面）；其餘無法判斷的情形印 `todo` 並請人看。

#### M4　`--rendezvous` 與 known_hosts 內容沒有驗證

- 位置：`:77-95`、`:484-581`。
- 問題：只檢查兩個參數同時給與檔案存在。`--rendezvous` 不是 `user@host:port`、或 known_hosts 是空檔、格式亂，腳本照樣寫進 `tunnel.env` 並重啟隧道，隧道守護就 `config_missing`（78）、不會自動重開。PMO 要到 journal 才發現，而訊息又不說是哪項。
- 建議：參數解析時用和 `deploy/llmgateway-tunnel` 的 `parse_rendezvous` 一樣的規則檢查（`user@host:port`，埠 1–65535），known_hosts 至少要有一行非註解、可被 `ssh-keygen -l -f` 讀的行；格式錯回 2。

#### M5　測試覆蓋不到上面的真機問題，且有兩處前提過時

- 位置：`tests/test_bootstrap.py:71,293-308,333`。
- 問題：(a) 假 release 沒有隧道檔、假 curl 預設 401，見上方；(b) 假 sudo 以目前使用者執行，看不到 H2、H6 這類身分／權限問題；(c) 沒有「某個指令失敗時腳本要回 1」的測試（H1）；(d) 沒有「第一次不給 rendezvous 時摘要仍要印出」的測試（H3，目前假 `systemctl is-active` 一律 0，掩蓋了隧道 inactive 的情況）。
- 建議：除了補假 release 與假 curl，加上：假 `systemctl is-active llmgateway-tunnel` 回 3 的情境；某個 `sudo -n` 指令失敗的情境；`--check` 沒有 NOPASSWD 的情境（假 sudo `-n` 無快取時回 1，`-v` 後才成功）。

### 低

- **L1**　`--check` 沒給 `--tag` 時（`:359,364-367`），`step_install` 一律印 `todo 尚未安裝指定版本` 並加進 todo，即使已經裝了。建議沒給 tag 時，只要 `current/.lgw-tag` 存在就 `ok`。
- **L2**　`install -m 600 -o llmgw`（`:474,574`）沒給 `-g`，檔案群組是 root。sshd 的 `StrictModes` 只看擁有者與可寫位元，所以能用，但 `llmgw` 的 `authorized_keys` 群組是 root 不乾淨；建議 `-g "$(id -gn llmgw)"`。
- **L3**　暫存檔：`mktemp` 失敗時退回系統暫存目錄（`:466,502`），內含 `authorized_keys` 內容或 `tunnel.env`（含會合點位址）；`run_root install` 失敗時 `rm -f` 沒被執行（`:474-475,574-575`）。建議用 `trap` 清掉，並把暫存檔建在管理帳號自己的家目錄或 `$TMPDIR` 下 0700 目錄。
- **L4**　`id -u llmgw` 失敗時退回 uid `1000`（`:141,268`），在 `llmgw` 還不存在（check 模式）時 `XDG_RUNTIME_DIR` 會指到錯的目錄。check 模式帳號不存在就不該往下執行需要它的指令。
- **L5**　隧道私鑰存在但 `.pub` 不在時（`:388`），摘要印「尚未產生」。建議 `.pub` 不在時用 `ssh-keygen -y -f` 重建（以 `llmgw` 身分）。
- **L6**　`LGW_BOOT_REPO` 環境變數可以改 clone 來源（`:103`），是為了測試。環境變數不會被 sudo 帶進去，風險低，但建議在說明裡標明只供測試。
- **L7**　`step_install` 用 `rm -rf "$bootstrap_clone"`（`:374,377`）與 `$TAG` 組路徑，`TAG` 已用 `^lgw-v…$` 驗證，所以安全；若日後放寬 tag 格式要重新檢查。

## 符合需求的項目

- **密碼處理**：密碼檔只出現在 `:129` 的 `sudo -S -p '' -v < "$SUDO_PASS_FILE"`；所有真指令都是 `sudo -n … </dev/null`（`:136,142`）；每個 root 步驟前都 `sudo_refresh`（check 模式例外，見 H4）；沒有 `set -x`（`grep 'set -x\|xtrace'` 零筆）；密碼檔路徑不寫在腳本與測試裡，參數錯誤訊息不印路徑；沒有地方把密碼檔內容放進 argv、輸出、log 或檔案。
- **`authorized_keys`**：一行格式與任務單一致 `command="${BOOT_HOME}/.local/share/llmgateway/current/deploy/remote-deploy.sh",restrict <公鑰>`（預設就是 `/home/llmgw/…`）；以 base64 欄位比對、舊行換掉、其他行保留（不計 H2）；用暫存檔加 `install -m 600 -o` 寫入；`~/.ssh` 用 `install -d -m 700 -o`。
- **冪等（在可讀的前提下）**：測試的第一次、第二次，第二次 `changed=0` 成立。
- **隧道金鑰**：只在 `-f` 不存在時才 `ssh-keygen`（見 H2 對「看得到」的前提）；腳本從不刪 `keys.env`、資料庫與金鑰。
- **步驟 5**：以 `llmgw` 跑 `install.sh`，帶 `XDG_RUNTIME_DIR=${RUNDIR}/${uid}`；暫存 clone 放 `~llmgw/.local/share/llmgateway/.bootstrap-<tag>` 並在成功後刪除。
- **步驟 8**：寫入 `RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS`，其他鍵保留（含 `install.sh` 範本的空值鍵）；內容不同才 changed，之後 `systemctl --user restart llmgateway-tunnel.service`。
- **摘要**：印隧道公鑰與 `[127.0.0.1]:4711 …`，沒有任何路徑會印私鑰、`keys.env` 或密碼（M2 是註解帶機器名的問題）。
- **結束碼**：0／1／2／3 與需求一致（H1、H3 影響 1 與 3 的正確性）；參數錯回 2。
- **與 install.sh、隧道守護的交互**：`install.sh` 只 enable／start 隧道（沒在跑才 start，有變更才排 60 秒後重啟），不會當場重啟；`tunnel.env` 空時隧道以 78 結束並停在 failed，bootstrap 步驟 8 的手動 `restart` 是對的。唯一不協調處是 H3：`install.sh --check` 把「隧道 failed」當 1。

## 建議的修補順序

1. H1（錯誤被吞）與 H2（以正確身分讀檔）：這兩條不修，其他都沒法在真機上驗證。
2. H4、H5（`sudo -v` 與 root 讀 sshd 設定）、H6（cwd）：讓 `--check` 與第一次執行能在真機走完。
3. H3（第一次的 check 與摘要）。
4. 測試（M5）：補假 release、假 curl，加失敗情境。
5. M1–M4、低。
