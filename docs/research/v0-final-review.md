# v0 最終審查（final-reviewer，架構師，2026-10-12）

> 範圍：`main...HEAD` 完整 diff（`lgw/`、`lgwd/`、`deploy/`、`.github/`、`tests/`、`pyproject.toml`），
> 對照基準 `openspec/changes/gateway-v0/`（proposal、design、specs、tasks）與 `docs/deploy.md`。
> 前兩輪：`docs/research/v0-impl-review.md`（impl 審查：高 2、中 5、低 9）、
> `docs/research/v0-bootstrap-review.md`（bootstrap 審查：高 6、中 5、低 7）。
>
> 行號以本 worktree HEAD（`b77c9b3`）為準。規矩：只審查、不改程式；未讀憑證檔、
> 未用真 key 或密碼、未 ssh 真機器、未跑 gh／push／workflow。
> 實驗只用假 ssh／127.0.0.1 關閉埠／純本機語義驗證，實驗檔放 `.work/`（gitignored）；
> 本次只有一個純 Python 語義實驗（`.work/exp_py_oserror.py`，已清）。
>
> 執行結果：
> - `python3 -m unittest discover -s tests -t .` → **Ran 262 tests，FAILED (failures=6)**，
>   6 個全在 `test_bootstrap`，同一根因（見 F1）。註：impl 審查時是 283 OK；
>   數量差是租約／管理 API 測試隨 L7 移除而消失（`lgwd` 已無 `leasing`，grep 零筆），不是漏跑。
> - `openspec validate gateway-v0 --strict` → **valid**。
>
> 結論：**修了再開**。高 2、中 4、低 11（含殘留與新問題）。擋 PR 的是 F1（套件紅燈）
> 與 F2（真機第二次執行必敗／崩潰）；F3、F4 也建議同批修。

## 1. 前兩輪「已修」核對表（看程式＋測試，有證據）

### 1.1 impl 審查的重點項

| 項 | 宣稱 | 核對 | 證據 |
|---|---|---|---|
| H1 SIGTERM 清理 | CLI 修好；函式庫改文件約定 | 修好（CLI）＋測試 | `lgw/connect.py:508-509`（handler 丟 `SystemExit(0)`）、`:512-513`（裝 SIGTERM／SIGHUP）、`:526`（`except (KeyboardInterrupt, SystemExit)` 回 0，`__exit__` 已清理）、`:462-465`（`finally` 關子行程＋刪暫存目錄）；`tests/test_connect.py:733-755`（SIGTERM→rc 0、ssh 死、暫存目錄消失）。殘留見 L-R1 |
| H2 `--check` 經會合點 | 修好＋測試＋文件 | 修好 | `deploy/install.sh:233-254`（`--probe-once` 分支，0／1／2 三態）；`deploy/llmgateway-tunnel:199-207`（`probe_once`）；`tests/test_install.py:423-456`、`565-572`（三態×有／無 tag）；`tests/test_tunnel.py:281-325`（probe-once 0／1／2）；`docs/deploy.md:393-404`（4.4 三態表） |
| M1 文件 JSON | 修好 | 修好 | `docs/deploy.md:330-340`（3.1 已是 JSON 範例）、`:463-465`（5.5）；`design.md:73-80`（決定 4 JSON）；`tasks.md` 3.1c JSON |
| M2 tunnel 缺 GlobalKnownHostsFile | 修好＋測試 | 修好 | `deploy/llmgateway-tunnel:146`（`GlobalKnownHostsFile=/dev/null`）；`tests/test_tunnel.py:227`、`:291`（argv 斷言） |
| M3 sed 遮蔽 | 修好＋行為測試 | 修好 | `.github/workflows/deploy-keys.yml:86`、同 `deploy-service.yml:76`（`${line//"$var"/…}` 純字串替換）；`tests/test_workflows.py:119-151`（實際跑 bash 餵含 `[`、`::` 的位址，全被換掉） |
| M4 固定 transient unit 名 | 修好＋測試 | 修好 | `deploy/install.sh:391-396`（timer 已排則跳過＋`已排程`；`systemd-run` 失敗只 `WARN` 不 exit）；`tests/test_install.py:726-758`（跳過＋WARN 兩案）；`docs/deploy.md:384`（4.2 對齊） |
| M5 跳板 host key 無行為測試 | 修好＋測試 | 修好 | `tests/test_connect.py:252`（`assertNotIn("-o", argv)`，取代永遠為真的舊斷言）、`:308-339`（缺跳板 key→`ssh_exited`／有→200） |
| L3 全空 secret | 修好＋行為測試 | 修好 | `deploy-keys.yml:70-73`（五空→exit 2，只印 secret 名）；`tests/test_workflows.py:161-200`（空→2、有→0，實際跑 bash 驗） |
| L1（impl）IdentitiesOnly | 修好＋測試 | 修好 | `deploy/llmgateway-tunnel:147`；`tests/test_tunnel.py:228`、`:292` |
| L4（impl）tunnel SIGTERM 測試 | 修好＋測試 | 修好 | `tests/test_tunnel.py:337-354`（SIGTERM→daemon rc 0、ssh child 死） |
| L6（impl）換會合點測試 | 修好＋測試 | 修好 | `tests/test_connect.py:341-363`、`tests/test_tunnel.py:356-374` |
| L2（impl）TOCTOU | 只寫文件 | 殘留，接受（低） | `lgw/connect.py:96-99`、`296-301` 說明窗口；`_wait_ready`（`:399-409`）仍先判埠開後判 `poll`，被搶時可能誤認就緒。見 L-R2 |
| L5（impl）`--check` 不知 restart 排程 | 未修 | 殘留（低） | `deploy/install.sh:196-254` 無 timer 感知。見 L-R3 |
| L7（impl）leasing 未移除 | 修好 | 修好（範圍外順帶確認） | `lgwd/` 內 `leasing`／`LeaseService` grep 零筆；租約測試已消失（總數 283→262 的主因） |
| L8 TAG_REGEX 兩處重複 | 未修 | 殘留（低） | `deploy-service.yml:69` vs `deploy/install.sh:13` 字面相同，兩邊皆無互指註解。見 L-R4 |
| L9 Client repr | 未修 | 殘留（低） | `lgw/client.py:57-60` 仍印 `base_url`，無「不得含憑證」約定；v0 實際只傳 `127.0.0.1` URL，無風險。見 L-R5 |

### 1.2 bootstrap 審查的 H1～H6 與中條目

| 項 | 宣稱 | 核對 | 證據 |
|---|---|---|---|
| H1 `step_x \|\| true` 吞錯 | 修好（程式）；**未補測試** | 修好未測 | 主程序 `:916-941` 直接調步、每步內改動指令各自 `if ! …; fail; return 1`（如 `:469-479`、`:483-501`）；剩餘 `\|\| true` 25 處全在取值／檢查管線（`:131`、`287`、`334-425`、`451`、`560`、`590-632` 等），語義良性。缺「某指令失敗→回 1」測試（假工具全成功）。見 M-F4 |
| H2 經正確身分讀寫 | **只修一半** | 部分 | 修好：`step_deploy_key` 經 `sudo -n -u llmgw cat`（`:584`）＋讀不到就 fail 不覆蓋（`:584-588`、`:634-639`）；寫入全經 `run_root`。未修：直接讀 `~llmgw` 留在 `step_install:450-452`（tag）、`step_tunnel_key:512`（私鑰存在檢查）、`step_rendezvous:670`／`:726`（`cmp`）、`:677-723`（python 直讀，只接 `FileNotFoundError`）、`step_check:815`／`:847`、`print_summary:883-884`。見 **F2（高）** |
| H3 首次 check＋摘要 | 修好（程式＋半測試） | 修好，帶分類瑕疵 | `step_check:856-858`（tunnel 未填＋主服務 active→todo）、主程序每步後 `print_summary`（`:917-941`）；`tests/test_bootstrap.py:653-665`（無 rendezvous→rc 3＋摘要有 todo）。分類讀 `tunnel.env` 走直接 `grep`（`:847`），真機讀不到時真失敗也被記 todo。見 M-F3 |
| H4 check 也 sudo -v | 修好一半 | 部分 | `sudo_refresh` 不再跳過 check（`:145-159`），`step_check:826` 有調。但 `step_sshd`（`:331-334`）無預刷，且在主程序第 4 步、早於任何可能觸發刷新的改動步；check 模式與「無變更的第二次執行」在無快取時 `sudo -n` 必敗→空輸出→假 todo。見 **M-F1（中）** |
| H5 root 讀 sshd | 修好一半 | 部分 | `run_root sshd -T`（`:334`）✓。仍缺：空輸出≠「沒開」的區分（`:336-340`）、`ss` 未經 sudo 且無 `/usr/sbin` PATH（`:424-425`）。見 L-R6 |
| H6 cwd | 修好 | 修好 | `run_as_llmgw` 先 `cd BOOT_HOME \|\| cd /`（`:169`）；所有 `run_as` 路徑皆絕對（`:482`、`:488`、`:493`、`:540`、`:804`、`:827-830`）。小疵見 L-R7 |
| M1 父目錄擁有者 | 只加 `-g`，核心未動 | 部分（中） | `-o/-g` 已全補（`:469`、`:528`、`:615`、`:760`、`:775`、`:788` 等）。但原訴求「改 `run_as_llmgw mkdir -p`」未採；父層是否真歸 llmgw 取決 GNU `install -d` 語義，本機（darwin）驗不出，測試假 `install` 還剝掉 `-o/-g`（`test_bootstrap.py:324-342`）。見 **M-F2（中）** |
| M2 host key 去註解 | 修好（程式）；未測 | 修好未測 | `:891-897`（`awk '{print $1, $2}'`）；fixture 有註解（`test_bootstrap.py:52`）但無斷言。低，見 L-R8 |
| M3 sshd 粗判 | 修大半 | 部分 | AllowGroups／DenyGroups＋AllowUsers 萬用字元→todo（`:353-422`）✓。洞：DenyUsers 只全等比對（`:342-351`），`*` 或 `user@host` 漏判→假 ok。見 L-R9 |
| M4 rendezvous 驗證 | 修好（程式）；格式未測 | 修好未測 | 格式＋埠範圍（`:98-108`）＋`ssh-keygen -l -f`（`:110-116`）✓；參數測試只覆蓋缺檔（`test_bootstrap.py:635-651`）。低，併 M-F4 |
| M5 測試缺口 | 宣稱補，實際沒補關鍵三案 | 部分（中） | 補了：假 release 複製全部 deploy 檔（`:305-322`）、假 curl 預設 200（`:344-351`）、tunnel 感知 systemctl（`:288-304`）。**沒補**：sudo 失敗傳播、無快取 check、身分／權限分離（假 sudo 永遠成功，`:95-134`）；`eea905f` 訊息寫的「隧道 inactive、sudo -n 失敗、無快取」三情境在檔內不存在。見 M-F4；另 `b77c9b3` 是**空 commit**（與 `eea905f` 差集為空）卻寫「L1 已修、L2 已修」，屬誤導記帳，見 L-R10 |
| L1（bootstrap）無 tag check | 未修 | 殘留（低） | `:459-463` 仍一律 todo。見 L-R1 |
| L2（bootstrap）`-g` | 修好 | 修好 | 同 M1 行號 |
| L3 暫存清理 | 修一半 | 部分（低） | deploy-key 全路徑清（`:636`、`:644`、`:648`）✓；rendezvous 早敗路徑（`:747-786`）不清 `$tmp_env`、無 trap、`:675` 有 /tmp 退路。見 L-R11 |
| L4 uid 1000 退路 | 未修 | 殘留（低） | `:167`、`:307` 照舊 |
| L5 .pub 重建 | 未修 | 殘留（低） | `:883-887` 照舊印「尚未產生」 |
| L6 LGW_BOOT_REPO | 未修 | 殘留（低） | `:124` 照舊，僅註解「支援測試覆寫」，手冊未提 |
| L7 rm -rf TAG | 無需修 | 安全 | TAG 經 `TAG_REGEX`（`:64-69`），`:482-498` 安全 |

## 2. 新問題（本次 diff 全掃，任務指定的五個重點）

### 高

#### F1. 套件紅燈：6 個 `test_bootstrap` 失敗，同一根因（真 ssh 被拿去連夾具位址）

- 位置：`tests/test_bootstrap.py:17-22`（`TOOLS_LIST` 含真 `ssh`，fakebin 無假 `ssh`）、
  `:417-458`、`:460-496`、`:498-516`、`:518-549`、`:551-609`、`:667-685`（6 個失敗案例）；
  被測鏈：`deploy/install.sh:239`（`--check` 調 `$probe_bin --probe-once`）→
  `deploy/llmgateway-tunnel:199-207`（`probe_once` 用真 `ssh -W`）→ 連不存在的夾具位址必敗 → `[check] fail (rc=1)`。
- 證據：`python3 -m unittest discover -s tests -t .` → `Ran 262 tests, FAILED (failures=6)`，
  6 個全是 `test_bootstrap`（`FAIL: test_fresh_machine_run_and_idempotence`、
  `test_password_never_leaks`、`test_docker_group_removal`、
  `test_tunnel_key_not_regenerated_if_exists`、
  `test_authorized_keys_management_and_permissions`、`test_sshd_todo_reporting`）；
  單跑可見 `[check] fail install.sh --check 未通過 (rc=1)`＋`經會合點 list 探針失敗`。
  有 rendezvous 的全量情境因此全滅——正是 H2 修完最需要測的那條路，現在測不到。
- 建議：fakebin 加假 `ssh`（`-W` 時回固定 HTTP 200／可配 `FAKE_PROBE_CODE`），
  或測時經 `SSH_PATH` 覆寫探針 ssh（`tunnel.env` 支援 `SSH_PATH`，`llmgateway-tunnel:41`）。
  修完重跑全套件，必須回到全綠才開 PR。

#### F2. H2 只修一半：真機家目錄 0700／0750 時，第二次執行必敗，rendezvous 路徑直接崩潰

- 位置：`deploy/bootstrap.sh:450-452`、`512`、`669-672`、`677-723`（尤其 `:701` 只接
  `FileNotFoundError`）、`725-728`、`815`、`847`、`883-884`。
- 問題：
  1. `step_tunnel_key:512` 直接 `[[ -f 私鑰 ]]`：真機讀不到→判「沒有」→調
     `run_as_llmgw ssh-keygen -f 既有檔`，stdin 是 `/dev/null`，ssh-keygen 問
     Overwrite 讀到 EOF 放棄、回非零→`:540-544` 記 fail、結束碼 1。金鑰靠
     ssh-keygen 自保沒被毀，但「可重複執行」破功（第二次必紅）。
  2. `step_rendezvous` 的 python 直讀 `$target_env`：`PermissionError` 不被 `:701`
     接住→traceback→`set -e`（`:6`）整支腳本當場死亡，無 `log_step`、無摘要。
     已用 `.work/exp_py_oserror.py` 證明同類 `OSError`（`IsADirectoryError`，同為
     `FileNotFoundError` 之外的 `OSError`）必經此路徑（rc=1＋traceback）。
  3. `step_install:450-452` 同理讀不到 tag→每次重裝（`changed` 永不清零，需求「第二次
     `changed=0`」不成立）；`step_check:847` 讀不到 `tunnel.env` 導致真失敗被誤記 todo（見 M-F3）。
- 證據：上列行號皆為管理帳號直讀 `~llmgw`（`BOOT_HOME` 下），與已修的 `:584`
 （`sudo -n -u llmgw cat`）形成對照；測試全在同 uid 跑，看不見（`test_bootstrap.py:95-134`
  假 sudo 不分身分）。
- 建議：所有讀 `~llmgw` 改經 `run_as_llmgw test/cat/cmp`（讀失敗與「不存在」分開）；
  python 生成改吃 stdin／或整段經 `run_as_llmgw` 跑；`cmp` 不存在與讀不到要區分。
  另補「家目錄不可讀」測試（不斷言权鑑別力，至少斷言 argv 經 sudo）。

### 中

#### M-F1. `step_sshd` 前沒人 `sudo -v`：真機無快取時 sshd 永遠假 todo，exit 0 永不可達

- 位置：`deploy/bootstrap.sh:331-334`（無 `sudo_refresh`）、主程序順序 `:925`（sshd
  在第 4 步，早於 `:928` 起才可能發生的刷新）。
- 問題：check 模式或「帳號＋linger 都已就緒的重跑」在刷新前就跑
  `sudo -n sshd -T`；無 NOPASSWD 且無快取時回 1→輸出被 `:334` 的 `2>/dev/null || true`
  吃掉→`sshd_out` 空→必記 `todo 未啟用 PubkeyAuthentication`，結束碼恆 3。
  這正是 bootstrap 審查 H5「完全完成無法達成」的借屍還魂，只是換成 H4 的缺口。
- 建議：`step_sshd` 開頭加 `sudo_refresh`（`-v` 只驗證，check 模式也合法，H4 已定調）；
  補「無快取 check」測試（假 sudo 記 `-v` 有無被調）。

#### M-F2. M1 核心沒動：新帳號無 `~/.local` 時 `install.sh`（llmgw 身分）可能建不出狀態目錄

- 位置：`deploy/bootstrap.sh:469`、`:528` 等（`install -d -m … -o … -g …` 只保證最後一層）；
  `deploy/install.sh:314`（以 llmgw 身分 `mkdir -p … ~/.local/state/…`）。
- 問題：若 GNU `install -d` 如前輪理解只 chown 最後一層，中途建出的 `~/.local`、
  `~/.local/share` 屬 root，後續 llmgw 的 `mkdir -p` 因父目錄不可寫而敗，
  新機第一次安裝直接紅燈。本機 darwin 驗不出 GNU 語義，測試假 `install` 又剝掉
  `-o/-g`，此路徑零覆蓋。
- 建議：改 `run_as_llmgw mkdir -p -m`（前輪原建議，語義無歧義）；另在 Linux 實機確認
  GNU 行為後再決定是否保留 `install -d` 寫法。

#### M-F3. H3 的 todo／fail 分類器在真機讀不到檔時把真失敗記成 todo

- 位置：`deploy/bootstrap.sh:844-862`（`:847` 直接 `grep tunnel.env`、`:852` 經 sudo 查主服務）。
- 問題：`:847` 在 0700／0750 家目錄恆敗→`tunnel_configured=0`；只要主服務 active，
  任何 check rc=1（隧道真斷、探針真敗）都進 `:856-858` 記 todo 而非 fail。
  首跑碰巧是對的（本來就沒填），之後全錯。
- 建議：隨 F2 把 `:847` 改經 sudo 讀；讀不到時記 fail（無法分類）而非 todo。

#### M-F4. 測試缺口：H1 失敗傳播、H2 身分分離、M4 格式驗證全無測試，且修補訊息 overclaim

- 位置：`tests/test_bootstrap.py:95-134`（假 sudo 永成功）、`:635-651`（參數錯只測缺檔）、
  全檔無「sudo 失敗」「無快取」「不可讀家目錄」案例；`eea905f` 訊息自稱新增三情境，
  檔內（689 行）無一存在；`b77c9b3` 與 `eea905f` 差集為空（空 commit）卻寫「L1、L2 已修」。
- 建議：假 sudo 加失敗注入（某指令 `sudo -n` 回非零→腳本回 1 且無該步 `changed`）；
  `--rendezvous` 亂格式／空 known_hosts 回 2 的案例；修正記帳習慣（空 commit 勿寫修了什麼）。

### 低

- **L-R1**（H1 殘留）：函式庫 `connect()` 刻意不裝全域 handler，`lgw/connect.py:481-484`
  要呼叫方把 SIGTERM 轉例外。sidecar（決定 4 的短命 connect 用法）若沒做，重啟照漏。
  建議 sidecar 側跟一條（本 repo 端已在 docstring 約定，可接受）。
- **L-R2**（impl L2 殘留）：`_wait_ready`（`lgw/connect.py:399-409`）先判埠開，搶埠者監聽時
  誤認就緒；文件已誠實揭露（`:96-99`）。接受，呼叫方重試即可。
- **L-R3**（impl L5 殘留）：`check_unit`（`deploy/install.sh:196-254`）不知
  `llmgateway-tunnel-restart.timer` 排程中；排程待觸發時回 0 偏樂觀。接受為低，
  要修則在 check 印一行 `restart pending` 警告。
- **L-R4**（impl L8 殘留）：`TAG_REGEX` 兩處字面重複（`deploy-service.yml:69`、
  `deploy/install.sh:13`），無互指註解。加一行註解即可。
- **L-R5**（impl L9 殘留）：`Client.__repr__` 印 `base_url`（`lgw/client.py:57-60`），
  無「不得含憑證」約定；v0 實傳 `127.0.0.1`，無實際風險。docstring 加一句即可。
- **L-R6**（H5 殘留）：`sshd -T` 空輸出即判「沒開」（`:336-340`），應分「無法判斷」
  與「沒開」；`ss`（`:425`）未經 sudo 且缺 `/usr/sbin` PATH。M-F1 修完後降為純顯示問題。
- **L-R7**：`run_as_llmgw`（`:165-170`）未把 `BOOT_HOME` 送進 sudo env，
  `cd "${BOOT_HOME:-/home/llmgw}"` 在覆寫 HOME 的測試環境恆 fallback 到 `/`。
  真機恰好是 `/home/llmgw` 所以能動，屬碰巧正確；建議 `env` 加 `BOOT_HOME`。
- **L-R8**（M2 未測）：摘要 host key 去註解無斷言；fixture 註解（`test_bootstrap.py:52`）
  正好可拿來斷言 `[127.0.0.1]:4711` 行只有兩欄。
- **L-R9**（M3 殘留）：`DenyUsers` 只全等比對（`:342-351`），`*`／`user@host` 漏判→假 ok。
  `AllowUsers` 側已妥（`:398-422`）。補 glob 比對或一律轉 todo。
- **L-R10**：`b77c9b3` 空 commit 誤導記帳（見 M-F4）。流程建議，無程式影響。
- **L-R11**（bootstrap L3 殘留）：`step_rendezvous` 早敗路徑（`:747-786`）不清
  `$tmp_env`（含會合點位址），`:675` 有 /tmp 退路且無 trap。加 `trap` 統一清，
  暫存建在管理帳號自家 0700 目錄。
- 另兩條硬化（皆低，均非外洩實錘）：摘要印既有隧道 `.pub` 全文（`:883-884`）——若
  該 `.pub` 是手產且註解含機器名，會跟著輸出被轉貼；建議同 M2 取前兩欄。
  參數錯誤訊息回顯 `--rendezvous` 值（`:100-106`）——只到操作者本機 stderr，
  可接受，記錄備查。
- bootstrap L1／L4／L5／L6 照舊未修（`:459-463`、`:167`／`:307`、`:883-887`、`:124`），
  皆低、非本次擋 PR 項，記錄備查。

## 3. 任務五重點的逐項結論

- **祕密外洩**：沒找到實錘。key／私鑰／密碼只經 env／stdin（workflows `:60-86`、
  `:62-76`；`remote-deploy.sh:44-100`；`bootstrap.sh:145-163`），不上 argv；
  無 `set -x`（deploy 全目錄零筆）；`lgwd` 只記方法／路徑／狀態碼（`lgwd/http.py:10`），
  key 檔錯誤只寫行號種類（`lgwd/keys.py:6`）；`Result.__repr__` 不印 data
  （`lgw/client.py:44-47`）且有測試（`test_client.py:277-286`）；
  密碼金絲雀測試（`test_bootstrap.py:460-496`）掃 stdout／stderr／argv／生成檔。
  兩個低硬化見上（L-R8 相關、L-R11）。
- **host key 嚴格比對**：四處一致——`lgw/connect.py:364-378`（`-F`＋`StrictHostKeyChecking yes`
  ＋`GlobalKnownHostsFile /dev/null`，跳板同套，行為測試 `:308-339`）、
  `llmgateway-tunnel:142-152`（同組，測試 `:227-228`、`:291-292`）、兩 workflow
  config（`GlobalKnownHostsFile /dev/null`＋`StrictHostKeyChecking yes`，測試 `:88-89`）、
  bootstrap 只接受既有 known_hosts 檔（`:110-116`，禁現掃，手冊 `:229` 明令）。
- **暫存與子行程**：connect（`:347-357` O_EXCL＋0600、`:462-465` 必清，測試 `:263-264`）、
  tunnel 守護（SIGTERM→terminate 子行程，測試 `:337-354`；probe 逾時 kill `:186-196`）、
  install.sh（`:80-101` 失敗清暫存）、remote-deploy（`:47` trap）。缺口只在
  bootstrap rendezvous 暫存（L-R11）與 F2 崩潰路徑。
- **bootstrap 真機行為**：sudo（M-F1 缺預刷）、權限（F2 直接讀）、冪等（F2 第二次必敗）、
  結束碼（H3 三態已對 `:943-951`，0／1／2／3 與手冊 `:123-130` 一致，分類器瑕疵見 M-F3）。
- **install.sh 與隧道重啟**：保證成立。部署路徑只有 enable／沒跑才 start／有變更才
  `systemd-run --on-active=60` 延後重啟（`:383-397`），無任何當場 restart／stop；
  失敗只 WARN（`:394-395`）；測試斷言無直接重啟（`test_install.py:327`、`:363`、
  `:712`）與延後排程（`:691-712`）。`remote-deploy.sh:29` upgrade 只調
  `install.sh --tag`，同保證。bootstrap `:804` 的當場 restart 走管理登入、非部署
  session，符合設計（手冊 1.8）。

## 4. 結論

**修了 F1、F2 再開 PR；M-F1、M-F2 強烈建議同批。**

開 PR 前檢查表：

1. fakebin 加假 `ssh`（F1），全套件回全綠（現在 262 跑 6 紅）；
2. bootstrap 讀 `~llmgw` 全經 sudo＋讀不到與不存在分開（F2），python 接 `OSError` 而非僅
   `FileNotFoundError`，真機跑兩次、第二次 `changed=0`；
3. `step_sshd` 開頭 `sudo_refresh`（M-F1），無快取 check 不再假 todo；
4. `mkdir` 改 `run_as_llmgw mkdir -p -m` 或提出 GNU 語義實證（M-F2）；
5. 重跑 `python3 -m unittest discover -s tests -t .` 全綠＋`openspec validate gateway-v0 --strict` valid，
   把兩段輸出貼進 PR。

（統計：高 2、中 4、低 11。新問題編號 F1／F2／M-F1～M-F4／L-R1～L-R11；
前兩輪已修項的修復狀態見 §1 表。）
