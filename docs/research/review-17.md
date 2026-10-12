# 審查 #17：v0 最終審查「低」11 條 followups（reviewer-17）

審查對象：`git diff main...HEAD`（branch `review/v0-followups`＝工單 #17 的修正，14 個 commit）＋作者的盤點表 `docs/research/v0-followups.md`。

commit 一覽（皆標 `(#17)`）：

- `5c11610` docs：盤點表 ｜ `e87b3c5` docs：Client docstring ｜ `16b6ac6` docs：TAG_REGEX 互指
- `3e35b1d` docs：簡體「判断」改「判斷」 ｜ `2d6226f` fix：BOOT_HOME 送進 sudo env
- `4005036` fix：sshd 監聽檢查先經 sudo ｜ `cd35167` fix：DenyUsers 萬用字元轉 todo
- `70da48e` fix：rendezvous 暫存與早敗清理 ｜ `2af31ef` fix：--check 重啟排程提醒
- `8c4e3d7` fix：摘要兩處公鑰取前兩欄＋測試 ｜ `dc5e2af` fix：經 sudo 問 gh 失敗回 2＋測試
- `e55a5c2` fix：repo 名修剪＋測試 ｜ `eefc994` docs：排錯表加 bad_config ｜ `e631357` test：假 sudo 每 PPID 一檔

審查者：reviewer-17（作者是 Muse Spark，不是審查者；PM 是 llmgateway-f8）。只審查，未改任何程式；未讀任何憑證檔或密碼檔；未 ssh／gh／push／觸發 workflow；實驗檔在 `.work/`（gitignored，不進 PR）。

## 結論

**可以開 PR。** 高 0、中 0、低 5、資訊 4。

11 條判定全部查證屬實（3 條「v5 已不適用」都有檔案級證據；8 條「要修」全部落實）；8 個指定重點逐條實測或走讀通過；密碼不上命令列、不進 log；前幾輪修好的東西（#18 sudo 快取、F2、H6、gh-auth 等）沒有被改壞；範圍是 14 個小修、無不該在這張單的大改動。5 個低項都不擋 PR，建議合併前順手修低-2／低-4／低-5，低-1／低-3 可合併後補（見各條建議）。

## 測試

`python3 -m unittest discover -s tests -t .`

```
Ran 253 tests in 256.951s

OK
```

（`.work/` 另跑 6 條針對性驗證：L-R9 萬用字元／user@host 兩案例記 todo、無關字名不誤判、L-R3 提醒出現、L-R11 早敗與成功路徑暫存皆清零——全過；其中 L-R9 萬用字元案例拿 base 的 `bootstrap.sh` 重跑確實失敗，證明修的是真洞。拆法見 §2。）

## 逐項核對

### 1. 盤點表 11 條判斷正確性（重點 1）✅

- **L-R1／L-R2（v5 已不適用）屬實**：`lgw/` 底下只有 `__init__.py`、`client.py`、`signals.py`，`connect.py` 已整組拿掉；兩條都依附該檔，無程式可修。
- **L-R4 的「原 pair 已消失」屬實**：repo 無 `.github/`；字面重複的模式確實移生到 `deploy/install.sh:16` ↔ `deploy/bootstrap.sh:8`（對 base `97ac602` 實測行號完全相符）。
- **L-R6 前半「已修」有證據且歸屬正確**：`deploy/bootstrap.sh:433-437`（rc≠0 或輸出全空白即記 fail）是 #18 的 `4e0a1aa`，`git merge-base --is-ancestor 4e0a1aa main` 確認在 main 內——功在 #18，不是本 branch 重做。
- **8 條「要修」全部為真且已修**（逐條修法見 §2）；「皆小修、沒發現其實很大」的自我檢驗也成立：最大的一段是 rendezvous 暫存（約 30 行含 trap）。
- **L-R10（空 commit）**：`main..HEAD` 14 個 commit 逐個 `--numstat` 皆有實質 diff（最少 1 檔），「本 branch 無空 commit 寫修東西」的宣稱屬實。
- **引用行號抽查 8 處全對**（base 上）：TAG_REGEX `install.sh:16`／`bootstrap.sh:8`、rendezvous mktemp `:867`、`ss` 舊碼 `:525-526`、DenyUsers 全等判斷 `:447`、tunnel-restart 排程邏輯 `:502-504`、隧道 `.pub` `printf` `:1038`、`pub_content` 註解。盤點表的功課做得很實。

### 2. 每個修正的正確性（重點 2）✅

- **L-R7 BOOT_HOME 送進 sudo env**（`bootstrap.sh:191-193`）：`env HOME=… BOOT_HOME="$BOOT_HOME"` 後，內層 `cd "${BOOT_HOME:-/home/llmgw}"` 拿得到真值；引號包住，BOOT_HOME 帶空白也不會破。額外實證：本 review 拿 base 版跑測試 harness 時反覆出現 `cd: /home/llmgw: No such file or directory`——fallback 路徑真實存在，只是測試環境碰巧無害，順手證明這條不是潔癖。
- **L-R9 DenyUsers 萬用字元轉 todo**（`:447-464`）：`*\**｜*\?*｜*@*` 一律進 todo、其餘才全等比對，與 `AllowUsers`（`:515-535`）／`DenyGroups`（`:471-486`）同手法；`reasons` 在 `step_sshd` 一律走 todo 不是 fail（`:545-554`），分類語意與 M3 既定一致。實測：`llmgw*`、`someone@example.com` 皆記 todo；無關字名不誤判（`.work/verify…`，6 tests OK）。
- **L-R6 後半 ss 經 sudo**（`:537-540`）：先 `PATH=/usr/sbin:/sbin:$PATH run_root ss`（sudo 自身走 secure_path，多數 Linux 預設含 `/usr/sbin`），失敗退直接跑並帶 extended PATH——兩道防線都不依賴呼叫者 PATH。`set -euo pipefail` 下 `$(A || B || true)` 賦值安全；`PATH` 前綴賦值在函式呼叫會留在 shell，但整行在 `$( )` 內、不外洩（資訊-4）。測試走 fake ss 全綠。
- **L-R11 rendezvous 暫存與清理**（`:880-891`、早敗五處）：mktemp 主位置改為「輸入 known_hosts 同目錄」（比舊碼實效：舊碼主位置是 llmgw 的 `.config`，管理帳號寫不進，實測恆退 /tmp）；早敗路徑逐一 `rm -f`；`trap 'rm -f "$tmp_env"; trap - RETURN' RETURN` 兜底。實測三件事：(a) bash 3.2.57 **支援** RETURN trap 且觸發時 `local` 仍可見（作者註解「bash 3.2 無此 trap」與實測不符，見低-4）；(b) 管線／`$( )` 子 shell 不會誤觸發（`gen_env` 管線與 `$(cat …)` 安全）；(c) 注入 sudo 失敗與成功兩路，暫存檔皆清零。
- **#13 新-低-1 經 sudo 問 gh 失敗回 2**（`install.sh:314-323`、`:339-347`）：`use_sudo=1` 時 rc=127 或輸出含 `sudo:` → 回 2「無法確認」，不再誤报「gh 沒登入」；包 `timeout` 時 127 語義仍成立；`LGW_SUDO` 正常轉交仍回 0（四案例都有測試：要密碼／本體缺席／第二步才失效／轉交成功）。殘差：見低-5、資訊-1。
- **觀察-2 repo 名修剪**（`:352-366`）：ltrim/rtrim → 去成對引號（單引號或雙引號）→ 再 trim；純 bash 無外部依賴。推演邊緣（單個引號字元、空輸出）行為與舊版一致；`quoted/squoted/spaced/quoted_spaced` 四態皆有測試。
- **L-R3 重啟排程提醒**（`:274-277`）：排在 probe 全過之後、`check_keysync` 之前，三態不變、只加一行 INFO；`is-active` 對 transient timer 在等候中為 active，語意正確（與 `:540` 既有用法一致）；提醒不進三態分類。
- **L-R4 TAG_REGEX 互指註解** / **L-R5 Client docstring** / **L-R8＋備查(a) 公鑰取前兩欄** / **#19 低-2 假 sudo 每 PPID 一檔**：皆正確。公鑰 awk 對空內容／單欄邊緣與舊版等價；假 sudo 每 PPID 一檔更貼真機且 `assert_every_sudo_n_was_validated` 逐 PPID 嚴驗仍在，state dir 每案 fresh 無跨案汙染。
- **L-R11 早敗五路**逐一走讀：`sudo_refresh` 失敗 `:919`、`.ssh` 失敗 `:922-927`、conf_dir 失敗 `:928-933`、known_hosts install 兩分支 `:934-948`、tunnel.env install 兩分支 `:949-963` 全部先 `rm` 再記 fail；`:964` 成功也 rm；唯一 set -e 致命點（`:966` `run_as_llmgw systemctl restart`）在 rm 之後，不會留殘留。

### 3. 密碼與 key、前幾輪成果（重點 3）✅

- **密碼不上命令列、不進 log**：sudo 密碼只走 `sudo -S -p '' -v < "$SUDO_PASS_FILE"`（stdin，`:150`、`:179`）；解密密碼只進 0600 暫存後 `install` 到目標並 `unset password`（`:751-769`）；新碼沒有任何把密碼值放進 argv 或 log 的路徑。公鑰印出反而更保守（兩處都去掉註解，降低機器名被轉貼）。
- **新測試也不引敏感值**：fake sudo／fake gh 只記 `$*`（選項＋路徑）；假資料全是 `example.com`／`Fake…` 前綴。
- **前幾輪修好的東西沒被改壞**：`git diff main...HEAD` 的 hunk 全落在預期區（sudo 包裝、sshd、rendezvous、摘要、install check、測試）；#18 的 `sudo_precheck`／`sudo -n` 包裝與 `4e0a1aa` 的 sshd -T fail、F2（`llmgw_exists` 三態）、H6（`cd BOOT_HOME || cd /`，本 branch 只增強）、M-F1/F2/F3、H3、gh-auth 步驟全部原封；舊守衛測試（`test_sudo_no_cache_check_refreshes_before_sshd`、`test_home_reads_go_through_sudo`、`test_交互…不外洩`、`test_gh沒登入時記todo…`、ppid 三情境）在 253 tests 全量中照常通過。

### 4. 範圍（重點 4）✅（記帳小瑕見低-1）

14 commits、7 files、275 insertions——全部是「前幾輪審查低項」級別的小修（最大一段 rendezvous 約 30 行），沒有架構改動、沒有跨出停看聽範圍的檔案。但 branch 實際比盤點表多修了 4 項（#13 新-低-1、觀察-2、#19 低-2、deploy.md bad_config 列），表與 branch 對不起來，見低-1。

### 5. 測試有效性

新測試都「會紅也會綠」：L-R9 案例在 base 上失敗（舊碼全等比對漏萬用字元）；install 兩條在 base 的舊 check_keysync 上同樣回錯分類。假 sudo 每 PPID 一檔後，舊的三個 #18 情境仍全過，嚴謹度沒被放水。

## 發現

### 低-1　盤點表記帳不全：branch 比表多修了 4 項

- **檔案:行號**：`docs/research/v0-followups.md:38-42`（B-1 修清單只列 8 條＋順手 1 條）
- **問題**：實際 branch 還修了 #13 新-低-1（`deploy/install.sh:314-323,339-347`）、觀察-2（`:352-366`）、#19 低-2（`tests/test_bootstrap.py:143-154`），外加 `docs/deploy.md` 的 bad_config 列（`eefc994`）。四項都不在 11 條或「順手」清單裡；表末「L-R1、L-R2、L-R10 為 v5 已不適用，不動程式」讓人以為其餘就是全部。
- **證據**：commit 訊息與程式碼引用（#13／#19 出處分別在 `docs/research/v5-review-13.md:142,144`、`docs/research/review-18.md:82-87`）；盤點表全文無這四項。
- **建議**：在表加一節「順手（別單低項）」列出四項與出處。一行成本，換來 audit 時表＝branch。不擋 PR。

### 低-2　rendezvous 暫存：/tmp 退路仍在，與表上宣稱的修法不一致

- **檔案:行號**：`deploy/bootstrap.sh:885`、盤點表 `:31`
- **問題**：盤點表寫「不經 /tmp 退路改為失敗即 fail」，實際是 `mktemp <輸入檔目錄>/.llmgw-env.XXXXXX 2>/dev/null || mktemp 2>/dev/null`——第一個失敗仍靜默退回 /tmp，只有兩個都失敗才 fail。另「建在管理帳號自家」也不精確：實際建在**操作者给的 `--rendezvous-known-hosts` 所在目錄**（可能是別人的目錄、唯讀目錄或奇怪檔案系統）。
- **證據**：`:885` 第二個 `mktemp` 無目錄參數即 /tmp；暫存內容只有會合點位址與路徑（無金鑰、無密碼），0600，退回 /tmp 的實質風險低——所以是記帳／註解問題不是安全洞。
- **建議**：二選一——(a) 真的拿掉第二個 mktemp，第一個失敗即 `fail`（符合原表）；或 (b) 留下退路但把註解改成實情（「輸目錄不可寫時退回 /tmp，0600」）。低-3 的測試補好後， whichever 選都測得到。

### 低-3　新分支沒有測試釘住：L-R9 萬用字元、L-R11 清理、L-R3 提醒

- **檔案:行號**：`tests/test_bootstrap.py`（`grep -n "sshd_deny"` 只有一處設值 `:1156`＝精確名；無 `.llmgw-env`／暫存斷言；fake systemctl 對 restart timer 恆回 0 但無斷言）
- **問題**：L-R9 的新 `case` 分支（萬用字元／user@host）在整個測試套件從未被執行；L-R11 的清理行為（本單核心）沒有斷言；L-R3 的提醒行有跑到但沒人斷言。L-R8 補了測試，同單的 L-R9／L-R11 反而沒有——標準不一致。
- **證據**：本 review 的 `.work/verify_lr9_lr3_lr11.py` 六條（含 base 對照會紅）證明這些案例便宜又可測；fixture（`sshd_deny` state、`sudo_fail_substr` 注入）現成。
- **建議**：把 `.work/` 那六條（或至少 L-R9 兩條＋L-R11 早敗一條＋L-R3 一條）併進 `tests/test_bootstrap.py`。不擋 PR，但這是本單最值得補的安全網。

### 低-4　「bash 3.2 無此 trap」註解與實測不符

- **檔案:行號**：`deploy/bootstrap.sh:883-884`
- **問題**：註解稱 RETURN trap 在 bash 3.2 不存在、被 `2>/dev/null || true` 吃掉；實測 macOS 內建 bash 3.2.57 **支援** RETURN trap，觸發時 `local` 變數仍可見、`rm` 確實執行。註解誤導維護者以為只有顯式 rm 在守（實際上真機多半是 bash 4/5，trap 是第二道真實防線）。
- **證據**：`.work/trap_test.sh`、`.work/trap_pipeline_test.sh`、`.work/trap_exact_test.sh`：3.2.57 上 trap 觸發、local 可見、管線／cmdsubst 不誤觸、殘留為 0。
- **建議**：把註解改成「RETURN trap 在 bash 3.2+ 可用，這裡當兜底；早敗路徑仍逐一顯式 rm」。不擋 PR。

### 低-5　check_keysync 的 sudo 失敗偵測漏 not-in-sudoers 樣式

- **檔案:行號**：`deploy/install.sh:318-323`、`:342-347`
- **問題**：啟發式是「rc=127 **或**輸出含 `sudo:`」。要密碼（`sudo: a password is required`）與本體缺席（127）都有；但 sudoers 政策拒絕（使用者不在 sudoers）若該版 sudo 的訊息沒有 `sudo:` 前綴，仍會掉進「gh 沒登入」回 1——正是本條要修的方向錯，只是少數分支。
- **證據**：走讀；建議的判據源自 `docs/research/v5-review-13.md:142`（原建議就是子字串比對）；分類結論（非零）不變，受影響的只有訊息方向。
- **建議**：比對樣式補 `*"not in the sudoers file"*`、`*"command not found"*`（後者已含 `sudo:` 前綴，錦上添花）或直接把訊息改成「無法以 ${service_user} 身分問 gh（sudo 失敗？gh 未安裝？）」。不擋 PR。

## 資訊

- **資訊-1**（`deploy/install.sh:319`）：sudo 轉交成功但該使用者沒有 `gh` 時，輸出 `sudo: gh: command not found` 含 `sudo:` → 回 2。比舊版回 1（「沒登入」）合理，但訊息「需要密碼或失敗」對這個成因略失準；可接受。
- **資訊-2**（`docs/deploy.md:519`）：首句宣稱 log「（都不含值）」，但新列 `bad_config …` 的訊息其實帶非法 `LGWSYNC_TIMEOUT` 的原值（`ops-scripts/key-sync:349,351`）——不是密碼，但嚴格說不是「無值」。doc 精度小瑕。
- **資訊-3**（`docs/deploy.md:519`）：「`…`或」缺全形空格（同句其他地方都有）。
- **資訊-4**（`deploy/bootstrap.sh:540`）：`PATH=… run_root …` 對函式呼叫的前綴賦值在 bash 會留在當前 shell；本行包在 `$( )` 內故無外洩，記錄語義備忘。

## 補充觀察（非發現）

- base 版在測試 harness 反覆印 `cd: /home/llmgw: No such file or directory`——L-R7 的 fallback 在 BOOT_HOME 非預設值時是真的會踩（真機恰好 `/home/llmgw` 所以能動），這條修的價值比表上寫的「零風險」再高一格。
- L-R3 的提醒在測試 harness 恆出現（假 systemctl 對 restart timer 一律回 0），略誇張但無害；真要模擬「沒排程」可加 state。
- 暫存檔現在短暫出現在操作者輸入檔同目錄；SIGKILL 才會留殘留（內容無金鑰／密碼，0600）。可接受。
- 全量 253 tests／約 257s，耗時集中在反覆起 sandbox，正常。

（實驗檔：`.work/trap_test.sh`、`.work/trap_pipeline_test.sh`、`.work/trap_exact_test.sh`、`.work/verify_lr9_lr3_lr11.py`、`.work/verify_base_check.py`、`.work/bootstrap_base.sh`——gitignored，不進 PR。）
