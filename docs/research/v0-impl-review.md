# v0 實作審查：客戶端連線（lgw connect／client）與隧道部署（reviewer, 2026-10-12）

> 範圍：`lgw/connect.py`、`lgw/client.py`、`pyproject.toml`（spec `connectivity`「客戶端連線程式」）；
> `deploy/llmgateway-tunnel`、`deploy/llmgateway-tunnel.service`、`deploy/install.sh`（spec `connectivity`
> 「服務端的隧道守護」「端對端健康探針」；design 決定 3）；`.github/workflows/deploy-keys.yml`、
> `deploy-service.yml`（spec `key-vault`；design 決定 2）；測試 `test_connect.py`、`test_client.py`、
> `test_package.py`、`test_tunnel.py`、`test_install.py`、`test_workflows.py`。
> 基準：`openspec/changes/gateway-v0/`（proposal、design、specs、tasks）與 `docs/deploy.md`。
>
> 行號以 37359be（本 worktree 的 HEAD）為準。規矩：只讀 repo、未讀憑證檔、未用真 key、未 ssh 真機器、
> 未跑 gh／未觸發 workflow。實驗都在 `.work/`（gitignored）：假 ssh 腳本、127.0.0.1 自開 sshd。
> 執行結果：`python3 -m unittest discover -s tests -t .` → **283 tests, OK**（32.5s）。
>
> 結論：**高 2 條、中 5 條、低 9 條**。測試覆蓋：connectivity 的 Scenario 有 2 個沒測到（見「測試覆蓋表」）。

## 測試覆蓋表（spec Scenario 對照）

| Spec Scenario | 實作 | 測試 | 說明 |
|---|---|---|---|
| connectivity 斷線重連 | `llmgateway-tunnel:232-237` | `test_tunnel.py:248-254` | 子行程結束→守護結束 rc=1；SIGTERM 收子行程另驗於實驗（未被正式測試涵蓋） |
| connectivity 遠端埠被佔用 | `llmgateway-tunnel:146`（ExitOnForwardFailure） | `test_tunnel.py:184-208`（argv） | 只有 argv 斷言，沒有「ssh 因轉發失敗而結束」的行為測試 |
| connectivity TCP 通但服務不通 | `llmgateway-tunnel:215-228` | `test_tunnel.py:226-246` | 3 次失敗重建、無回應也算失敗 ✓ |
| connectivity 經跳板連線 | `lgw/connect.py:382-383` | `test_connect.py:200-202`、`245-260` | 只驗 `-J` 參數帶對；經跳板的 host key 是否真的被 `-F` 套用**沒測** |
| connectivity 連不上 | `lgw/connect.py:137-149`、`457-468` | `test_connect.py:331-374`、`630-639` | ssh_exited／timeout／ssh_missing／CLI 不印位址 ✓ |
| connectivity 換一個會合點 | 設定驅動（`_split_target`） | **沒測** | 沒有「同一段程式、兩個設定、各連自己的假 ssh」的測試 |
| key-vault 部署 | 兩個 workflow | `test_workflows.py` | 只驗靜態文字（`-F`、`-J`、0600、清檔）；**值只經 env／stdin 沒測** |
| key-vault 權限 | `remote-deploy.sh`（範圍外） | `test_deploy_script.py:42-50` | 0600 ✓ |
| key-vault 經會合點連入 | 範圍外 | — | 服務端；本次不含 |
| key-vault 換 key／掃描 | — | — | 服務端；本次不含 |

## 高（2 條）

### H1. `lgw connect`（函式庫與 CLI）收到 SIGTERM 時，ssh 子行程與暫存目錄都不會被清掉

- 位置：`lgw/connect.py:415-454`（`_connected` 的 `finally` 清理）、`:471-500`（`main` 只接 `KeyboardInterrupt`）、
  `deploy/llmgateway-tunnel.service` 的運作模式對照（systemd 正常停用程序是 SIGTERM）。
- 問題：清理只在 Python 的例外／`with` 正常退出路徑上。行程被 SIGTERM 時，Python 直接結束，
  `finally` 根本不跑：`ssh` 子行程留著（還在跑、還佔著轉發埠），`mkdtemp` 的 `lgw-connect-*`（0700）
  連 `ssh_config`（0600）與 `host_keys` 寫的 `known_hosts`（0600）留在 `/tmp`。
- 證據（實驗 `.work/exp_cli_sigterm.py`、`.work/exp_lib_sigterm.py`，都是假 ssh、不連真機器）：
  - CLI：`rc: -15`、`fake ssh alive after SIGTERM: True`、`temp dir still exists: True`、
    leftover `['/var/folders/.../lgw-connect-r68c849x']`；
  - 函式庫（sidecar 的用法 `with connect(cfg)`）：`rc: -15`、ssh 子行程仍活著、暫存目錄仍在。
- 影響：sidecar（決定 4 說「每次操作才 connect、用完就關」）在重啟／關機時會漏子行程；
  反覆的 worker 重啟會累積暫存目錄與轉發埠（子行程死亡前埠不會釋放）。
- 建議修法：`main` 與 `connect()` 都裝 `signal.signal(SIGTERM, handler)`（handler 丟 `SystemExit` 或設旗標），
  讓 `finally`／`with` 有機會跑；或把清理搬到 `try/finally` 的訊號安全路徑＋CLI 在 `proc.wait()` 用
  `while proc.poll() is None: sleep` 讓 SIGTERM 有中斷點。並補一個「送 SIGTERM 後子行程死、目錄清」的測試。

### H2. `install.sh --check` 只探本機埠，與 design 決定 3「經會合點探得到」不符，且會給出假陽性

- 位置：`deploy/install.sh:218-236`（`check_port` + 本機 `curl http://127.0.0.1:${port}/v1/subscriptions`）。
- 問題：
  1. design 決定 3（`design.md:62`）：「`--check` 三態加上隧道：0＝兩個 unit 都在跑、版本對、**經會合點探得到**」。
     實作沒有「經會合點」這一步——`curl` 打的是本機 `127.0.0.1`。
  2. 假陽性：隧道斷了但 `llmgateway.service` 活著時，本機 curl 仍回 200 → `--check` 回 0。
     這時部署 workflow（`deploy-service.yml:75` 走會合點 4711）其實進不去，`--check` 卻說「正常」。
  3. 反向的假陰性：服務被別的行程佔用／別的東西回 200 也會被當成健康（沒有做端對端）。
- 證據：`deploy/install.sh:221` 的 URL 寫死 `127.0.0.1`；`test_install.py:374-458` 所有 `--check` 案例都用
  假 `curl` 驗本機碼，沒有任何「經會合點」的例子。
- 建議修法：`--check` 的「經會合點」那一步用與隧道守護同一套 ssh 參數（`tunnel.env` 已在：
  `RENDEZVOUS`／`IDENTITY`／`KNOWN_HOSTS`）跑一次 `ssh -W 127.0.0.1:<REMOTE_API>` 的 list 探針；
  分開回報「本機健康」與「經會合點健康」，後者失敗回 1／無法執行回 2。至少要在 docs 與 design 二選一對齊。

## 中（5 條）

### M1. 文件與程式不一致：design 決定 4／tasks 3.1c／deploy.md 說 `connect.env`（KEY=VALUE），程式吃 JSON

- 位置：`lgw/connect.py:15-100`（JSON、`schema: 1`）；`design.md:71`；`tasks.md:23`；`docs/deploy.md:200-217`、`:307`。
- 問題：deploy.md 3.1 教人建 `~/.config/lgw/connect.env` 並寫 `JUMPS=`／`RENDEZVOUS=`… 的 KEY=VALUE，
  但 `load_config` 只收 JSON（`connect.py:273-286`），拿 KEY=VALUE 去餵會回「config is not valid JSON」。
  照手冊做一定失敗；`docs/deploy.md:307` 的排錯步驟同樣指到不存在的 `connect.env`。
- 證據：`test_connect.py:548-560`（非 JSON → ConfigError）；`test_connect.py:607-651` 全部用 JSON 設定檔；
  `docs/deploy.md:202-208` 是 KEY=VALUE 樣板。
- 建議修法：決定一種格式（JSON 已實作且測過，傾向保留），把 design 決定 4、tasks 3.1c、deploy.md 3.1／5.5
  一併改成 JSON 範例與 `.json` 檔名；或反之改程式。不要三處各說各話。

### M2. 隧道守護沒有關掉系統 known_hosts（`GlobalKnownHostsFile /dev/null`），與 connect.py 和 workflow 不一致

- 位置：`deploy/llmgateway-tunnel:130-138`（`_ssh_common` 只有 `-o UserKnownHostsFile=… -o StrictHostKeyChecking=yes`）。
- 問題：跳過 `GlobalKnownHostsFile`，ssh 仍會讀 `/etc/ssh/ssh_known_hosts`。若那台機器（或發行版套件、
  別的管理流程）在系統檔裡有會合點的 host key，守護程式會接受它——「host key 只釘在自己的 known_hosts」
  的模型被放寬。connect.py 的 `-F` 檔（`lgw/connect.py:361`）與兩個 workflow（`deploy-keys.yml:50`、
  `deploy-service.yml:54`）都有 `/dev/null`，只有這裡沒有。
- 證據：實驗 `.work/exp_tunnel_knownhosts.py`：`GlobalKnownHostsFile in tunnel argv: False`，
  而 `connect.py has GlobalKnownHostsFile in ssh_config: True`。
- 建議修法：`_ssh_common` 加 `-o GlobalKnownHostsFile=/dev/null`，與另外兩處對齊；`test_tunnel.py:184-208`
  補一條 argv 斷言。

### M3. workflow 的位址遮蔽（sed）不是安全的樣式：位址含 regex 位元字元時會部分遮、甚至整段不遮

- 位置：`deploy-keys.yml:82`、`deploy-service.yml:76`（`sed -e "s|${jump_host}|[jump]|g; s|${target_host}|[target]|g"`）。
- 問題：把位址直接塞進 sed 的樣式（BRE）。`.`、`[`、`]`、`*`、`^`、`$` 等是樣式語法，不是字面：
  - 含 `[` 的 hostname（如 `jump[1].example`）→ sed 編譯失敗，`2> >(sed …)` 的替換整個不發生，
    原始位址照樣進 GitHub log（遮蔽承諾失效）；
  - `2001:db8::7`（未加括號的 IPv6）→ 只遮到 `2` 一個字元，其餘外洩。
- 證據（本機）：`jump_host='jump[1].example'` 時 sed 回 0 但仍輸出原文；`target_host='2001:db8::7'` 時輸出
  `[target]001:db8::7`。workflow 的註解自己承諾「ssh 的錯誤輸出也會把位址換成 [jump]／[target] 再印」
  （`deploy-keys.yml:15`），實際靠不住。
- 建議修法：跳脫樣式（或改用 `awk`／固定字串替換），並補測試（`test_workflows.py` 目前完全沒測這段）。

### M4. 延後重啟用固定 transient unit 名：60 秒內第二次 install 會失敗

- 位置：`deploy/install.sh:372-376`（`systemd-run --user --on-active=60 --unit=llmgateway-tunnel-restart`）。
- 問題：`--unit` 固定。真 systemd 在第一個（60 秒後才觸發的）transient unit 還在排隊時，第二次同名
  `systemd-run` 會以「unit already exists」失敗；`install.sh` 那行是 `|| { log ERROR …; exit 1; }`，
  於是整支 installer 回非零——即使安裝、切 symlink、重裝 unit 都已完成。部署 workflow 會看到失敗，
  但其實沒有補排新的重啟（要等下一次 install）。
- 證據：實驗 `.work/exp_systemd_run_collision.py`（假 systemd-run 模擬同名失敗）：第二次 install
  `rc=1`、`stderr: ERROR 排程隧道重啟失敗`。
- 建議修法：unit 名帶唯一後綴（`--unit=llmgateway-tunnel-restart-$(date +%s)`），或失敗時區分
  「排程失敗」與「install 失敗」（前者只 warning、留給下次）；至少不要讓 install 假失敗。

### M5. `test_connect.py:205-207` 只驗「argv 上沒有 `-i`／`-o`」，沒有驗「經跳板的 host key 真的被 `-F` 檢查」

- 位置：`tests/test_connect.py:200-213`（斷言 ssh_config 文字、斷言 argv 沒有 `-i`／`-o`／`accept-new`）。
- 問題：這條測試證明「設定寫對了」，不證明「跳板那一段真的套了」。`-F` 對 `-J` 的套用是本設計
  （設計決定 4 與 workflow header 都特別點名）唯一的 host key 防線，值得一個行為測試。
- 證據（本機實驗 `.work/exp_jump_knownhosts.sh`，只連 127.0.0.1 上自開的 sshd）：
  - A：跳板 host key 不在 `-F` 的 known_hosts → `Host key verification failed`（rc=255）✓；
  - B：跳板 host key 在裡面 → 通過 host key 檢查，才在認證階段 `Permission denied` ✓。
  行為對了，但正式測試沒有；而且 `test_connect.py:207` 的 `assertNotIn("StrictHostKeyChecking=yes", argv)`
  是檢查指令列字串，`argv` 元素其實是 `StrictHostKeyChecking yes`（沒有 `=`），這條斷言永遠為真、沒有鑑別力。
- 建議修法：補一條「跳板 host key 不在 known_hosts → connect 丟 `Unavailable('ssh_exited')`」的假 ssh 測試
  （假 ssh 檢查自己 argv 裡的 `-F` 檔內容再決定成敗）；`test_connect.py:207` 改斷言 `-o` 不在 argv。

## 低（9 條）

### L1. 隧道守護沒帶 `IdentitiesOnly=yes`

- 位置：`deploy/llmgateway-tunnel:130-138`。connect.py（`:357`）與 workflow（`deploy-keys.yml:48`）都有。
  沒有它，ssh 會依序試 agent／預設金鑰，可能撞 auth 失敗上限（`MaxAuthTries`）而連不上，
  或用到不該用的金鑰。建議補 `-o IdentitiesOnly=yes`；`test_tunnel.py:184-208` 加斷言。

### L2. `_free_port()`（bind 0 → 立刻釋放 → 之後才讓 ssh 綁）有 TOCTOU 窗口

- 位置：`lgw/connect.py:289-293`、`:419`。極小，但本機其他行程可搶先綁該埠：ssh 端會因
  `ExitOnForwardFailure` 失敗（→ `ssh_exited`，行為可接受）；運氣壞時搶先者的 listener 讓
  `_wait_ready` 誤以為就緒（`connect.py:392`）。建議：`_wait_ready` 除了埠開也要看 `proc.poll()`；
  或在成功後再驗一次 ssh 還在。

### L3. `deploy-keys.yml` 的 5 個 key secret 沒有「至少一個有值」或格式檢查

- 位置：`deploy-keys.yml:67-83`。五個都空的也會把五行空值送出去（遠端會拒，回非零；行為安全），但
  錯誤在遠端才發生。另 `K_*` 值在 runner 的環境變數裡（`proc/environ`），屬 GitHub-hosted runner 的
  可接受範圍，但與「值只經環境變數與 stdin」的承諾一致——建議在 workflow 檔註解更明確，或送前檢查。

### L4. `test_tunnel.py` 沒測 SIGTERM 路徑（systemd 就是送 SIGTERM）

- 位置：`tests/test_tunnel.py`（有 stop_daemon 用 terminate，但沒有斷言「子行程死了」）。
  實驗 `.work/exp_tunnel_sigterm_child.py`：SIGTERM → daemon rc=0、ssh child dead ✓；
  SIGKILL → child 留著（預期，無法處理）。建議把 SIGTERM 案例寫進正式測試。

### L5. `install.sh` 的 `--check` 沒涵蓋「`llmgateway-tunnel.restart` 排程中」與 tunnel unit failed 的狀態

- 位置：`deploy/install.sh:196-237`。`is-active` 只看 active；unit failed→inactive 會回 1（對），
  但「重啟排程中」時兩個 unit 都 active、本機 200 → 回 0（其實隧道馬上也許斷）。搭配 H2 一起修較合適。

### L6. 測試沒有「換一個會合點」（spec connectivity 最後一個 Scenario）

- 位置：`tests/test_connect.py`。設定驅動已實作，但沒有測試把「同一段程式碼、兩個不同 rendezvous
  設定、各連不同的假 ssh」跑一遍。建議補一個 10 行測試。

### L7. `lgwd` 目前仍 import `lgwd.leasing`（v4 tasks 1.5 的移除未完成）

- 位置：`lgwd/main.py:22`（`from .leasing import LeaseService`）、`lgwd/leasing.py`（37K）仍在樹上。
  範圍外（本次審查不含服務），但 tasks 1.5「移除租約核心…」還留著，且 `test_leasing.py`（43K）、
  `test_admin_api.py` 也還在跑。提醒追蹤：v0 對外只有三個介面。

### L8. `deploy-service.yml:69` 的 tag 驗證與 `install.sh` 的 `TAG_REGEX` 已一致，但兩處各有一份

- 位置：`.github/workflows/deploy-service.yml:69` vs `deploy/install.sh:13`。重複的字面 regex；已有一處
  改了另一處忘的風險。建議在 docs 記一條「改 tag 格式要同時改兩處」，或在 workflow 的註解寫明。

### L9. `Result.__repr__` 不印 `data`，但 `Client.__init__` 沒有限制 `base_url`

- 位置：`lgw/client.py:44-60`。`Client(base_url=…)` 會出現在 repr，若未來有人把含 token 的 URL 傳進來
  就會外洩——目前 v0 的 base_url 一定是 `http://127.0.0.1:<port>`（connect.py:450）所以實際無風險。
  可在 docstring 加一句「base_url 不得含憑證」的約定。

## 附：驗證方法與實驗記錄

- 全測試：`python3 -m unittest discover -s tests -t .` → 283 OK。
- `.work/exp_cli_sigterm.py`、`.work/exp_lib_sigterm.py`：假 ssh＋SIGTERM → 子行程存活、暫存目錄殘留（H1）。
- `.work/exp_tunnel_knownhosts.py`：印 `tunnel_argv`／`probe_argv` → 無 `GlobalKnownHostsFile`（M2）。
- `.work/exp_jump_knownhosts.sh` ＋ 127.0.0.1 自開 sshd：A 情境 host key 被拒、B 情境通過（M5 之證）。
- `.work/exp_systemd_run_collision.py`：假 systemd-run 模擬同名 transient unit → install rc=1（M4）。
- `.work/exp_tunnel_sigterm_child.py`：守護 SIGTERM→child dead；SIGKILL→child 殘留（L4）。
- sed 樣式實驗（shell）：`jump[1].example` 樣式失敗照印原文、`2001:db8::7` 部分替換（M3）。
- 未執行的：真 ssh、真 workflow、gh、任何憑證檔；位址與金鑰一律用 fake 佔位。
