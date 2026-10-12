# v0 最終審查「低」盤點（followups，#17-A）

> 範圍：`docs/research/v0-final-review.md` §2 低（L-R1～L-R11，共 11 條）。
> 對照基準：本 worktree HEAD `97ac602`（branch `fix/v0-followups`，從 `main=lgw-v0.1.1` 開出）。
> v5 已拿掉 `lgw/connect.py`、部署 workflow（`.github/` 整組）、`remote-deploy.sh`；
> 隧道與 key-sync 在 `ops-scripts/`（`install.sh` 與 `bootstrap.sh` 仍在 `deploy/`）。
> 規矩：只寫程式與假資料；未讀憑證檔或密碼檔；未 ssh 真機器、未跑 gh／push／觸發 workflow；
> 實驗檔放 worktree 的 `.work/`；public repo 不寫機器名、IP、key 的值。
> 行號以 HEAD `97ac602` 為準。

## 判定說明

- `v5 已不適用`：該條依附的檔案／流程在 v5 已拿掉，無程式可修。
- `已修`：現況程式已符合建議，附證據 `檔案:行號`。
- `要修`：現況仍有缺口，本單 B-1 修（皆小修；若實作發現某條其實很大就不修，在表裡寫理由，PM 另開單）。

## 盤點表

| 項 | v0-final-review 位置 | 現況（HEAD `97ac602`） | 判定 | 理由／證據 |
|---|---|---|---|---|
| L-R1（H1 殘留：函式庫不裝全域 handler，sidecar 側跟一條） | §2 低 L-R1（`lgw/connect.py:481-484`） | `lgw/` 底下已無 `connect.py`（僅 `__init__.py`、`client.py`、`signals.py`）；`grep -rn connect lgw/ lgwd/ deploy/ ops-scripts/ tests/` 無連線程式殘留 | v5 已不適用（連線程式整組拿掉，v5 #12） | v5 起客戶端自備通道，LLMGateway 不提供連線程式（`docs/deploy.md:20`、`lgw/__init__.py`）；無 sidecar 側可跟 |
| L-R2（impl L2 殘留：`_wait_ready` 先判埠開，搶埠誤認就緒） | §2 低 L-R2（`lgw/connect.py:399-409`，文件 `:96-99` 誠實揭露） | 同上，`lgw/connect.py` 不存在，`_wait_ready` 無對象 | v5 已不適用（檔案已刪） | 同 L-R1；呼叫方重試的建議已無載體 |
| L-R3（impl L5 殘留：`check_unit` 不知 restart timer 排程中，回 0 偏樂觀） | §2 低 L-R3（`deploy/install.sh:196-254`） | `deploy/install.sh:213-275`（`check_unit`：unit→本機→經會合點→key-sync）全無 `restart` 字樣；排程邏輯在 `:502-507`（`is-active llmgateway-tunnel-restart.timer` 則跳過＋`已排程`） | 要修（小修：check 遇排程中加一行 `restart pending` 警告，三態不變，仍回 0） | 建議即原文「印一行警告」；不改 0／1／2 分類，只免得把「約 60 秒後會重連」當成完全沒事 |
| L-R4（impl L8 殘留：`TAG_REGEX` 兩處字面重複，無互指註解） | §2 低 L-R4（`deploy-service.yml:69` vs `deploy/install.sh:13`） | `.github/` 整組不存在（`ls .github` 無此目錄）；原 pair 已消失；現 `TAG_REGEX` 在 `deploy/install.sh:16` 與 `deploy/bootstrap.sh:8` 字面相同，兩邊皆無互指註解 | 要修（小修：兩處各加一行互指註解） | 原 pair v5 已不適用（workflow 拿掉），但同值重複的模式仍在（install vs bootstrap）；加註解即結案 |
| L-R5（impl L9 殘留：`Client.__repr__` 印 `base_url`，無「不得含憑證」約定） | §2 低 L-R5（`lgw/client.py:57-60`） | `lgw/client.py:55-58` 仍 `return "Client(base_url={0!r}, ...)"`；模組 docstring `:1-20` 與類 docstring `:49` 皆無憑證約定；`tests/test_client.py:279-286` 只守 `Result.__repr__` 不印 data | 要修（小修：`Client` docstring 加一句「base_url 不得含憑證；v0 只傳 loopback URL」） | v0 實際只傳 `127.0.0.1` URL，無實際風險；原文即「docstring 加一句即可」 |
| L-R6（H5 殘留：`sshd -T` 空輸出判「沒開」；`ss` 未經 sudo 且缺 `/usr/sbin` PATH） | §2 低 L-R6（`:336-340`、`:425`） | 前半已修：`deploy/bootstrap.sh:431-435`（rc≠0 或輸出全空白即 `fail 無法執行 sshd -T`，不往下分類；#18 `4e0a1aa`）；後半未修：`:525-526` 仍 `ss_out="$(ss -ltn 2>/dev/null \|\| true)"`（直接跑，未經 `run_root`，PATH 未含 `/usr/sbin`） | 要修（小修：`ss` 先經 `run_root` 試，失敗退回直接跑；仍只判 `127.0.0.1:22` 監聽，分類不變） | M-F1 修完後屬純顯示問題；缺 `/usr/sbin` 時 `ss` 找不到會誤記 todo「未監聽」 |
| L-R7（`run_as_llmgw` 未把 `BOOT_HOME` 送進 sudo env） | §2 低 L-R7（`:165-170`） | `deploy/bootstrap.sh:186-192`：`sudo -n -u … env HOME="$BOOT_HOME" XDG_RUNTIME_DIR=… bash -c 'cd "${BOOT_HOME:-/home/llmgw}" \|\| cd /; …'`——`env` 未帶 `BOOT_HOME`，內層恆 fallback 到 `/home/llmgw` 再 `/`；真機恰好 `/home/llmgw` 所以能動 | 要修（小修：`env` 加 `BOOT_HOME="$BOOT_HOME"` 一項） | H6 已保證 `run_as` 路徑皆絕對，cwd 不影響正確性；修的是「碰巧正確」，零風險 |
| L-R8（M2 未測：摘要 host key 去註解無斷言） | §2 低 L-R8（`:891-897`、`tests/test_bootstrap.py:52`） | 程式已修：`deploy/bootstrap.sh:1048-1057`（`awk '{print $1, $2}'` 只印前兩欄，去掉可能帶機器名的註解）；測試未補：`tests/test_bootstrap.py:52-53` fixture 含 `root@mockhost` 註解，但全檔無「摘要只印兩欄」斷言（`grep -n 摘要 tests/test_bootstrap.py` 只有三段存在性檢查） | 要修（小修：補一條測試，fixture 註解不得出現在摘要輸出） | 行為早對，缺的是把保證釘住的測試；fixture 正好可用 |
| L-R9（M3 殘留：`DenyUsers` 只全等比對，`*`／`user@host` 漏判→假 ok） | §2 低 L-R9（`:342-351`；`AllowUsers` 側 `:398-422` 已妥） | `deploy/bootstrap.sh:443-452` 仍 `if [[ "$u" == "$USER_NAME" ]]` 全等；`AllowUsers` 側 `:499-523` 已處理 `user@host` 取 `@` 前、`*/ ?` 轉 todo | 要修（小修：`DenyUsers` 照 `AllowUsers` 的做法，`*/ ? / *@*` 一律轉 todo，否則全等比對） | 漏判會把「被擋」看成 ok；轉 todo 即請人確認，不自己解析 glob |
| L-R10（空 commit 誤導記帳） | §2 低 L-R10（`b77c9b3` 與 `eea905f` 差集為空卻寫修了 L1／L2） | 流程項，無程式影響；本 branch 以 `git log --oneline` 可見每 commit 皆有實質 diff（`97ac602` 起回溯無空 commit 寫修東西） | v5 已不適用（流程建議，無程式可修） | 記帳習慣：空 commit 勿寫修了什麼；本單每條一個 commit、訊息只寫真的做的事 |
| L-R11（bootstrap L3 殘留：`step_rendezvous` 早敗不清 `$tmp_env`，有 /tmp 退路且無 trap） | §2 低 L-R11（`:747-786`、`:675`） | `deploy/bootstrap.sh:866-867`（`mktemp "${conf_dir}/.env.XXXXXX" 2>/dev/null \|\| mktemp`：失敗退到 /tmp）；建出後早敗不清：`:895`（`sudo_refresh` 失敗直接 `return 1`）、`:898-907`（`ensure_llmgw_dir` 失敗直接 `return 1`）皆未 `rm -f "$tmp_env"`；全檔 `grep -w trap` 零命中；成功／`gen_env` 失敗／`CHECK_ONLY`／`install` 失敗路徑有清（`:872`、`:883`、`:889`、`:924`、`:930`、`:936`） | 要修（小修：早敗路徑逐一補 `rm -f`；另加函式內 RETURN trap 兜底；暫存優先建輸入檔同目錄，輸目錄不可寫時退回 /tmp（0600＋返回即清，不常駐）） | `$tmp_env` 含會合點位址；量小但屬暫存衛生；改動限 `step_rendezvous` 內 |

## 備查（不計入 11 條，原文即「記錄備查」）

- 兩條硬化（皆低，均非外洩實錘）：(a) 摘要印既有隧道 `.pub` 全文（`deploy/bootstrap.sh:1037-1038` `printf '%s\n' "$pub_content"`）——手產 `.pub` 註解含機器名會跟著輸出被轉貼，建議同 M2 取前兩欄（本單 B-1 順手修，與 L-R8 同手法）；(b) 參數錯誤訊息回顯 `--rendezvous` 值（`:99-106`）——只到操作者本機 stderr，可接受，不修。
- bootstrap L1／L4／L5／L6 照舊未修（`:565-573` 實已 tag 感知、` :189`／`:393` uid 1000 退路、`:1042-1043`「尚未產生」、`:124` `LGW_BOOT_REPO` 僅註解），皆低、非擋 PR 項，不修。

## B-1 修清單（本單要動的 8 條＋順手 1 條）

`要修` 共 8 條：L-R3、L-R4、L-R5、L-R6（後半）、L-R7、L-R8（補測試）、L-R9、L-R11。
另順手修備查 (a) 隧道 `.pub` 摘要取前兩欄（與 L-R8 同手法、一行）。
L-R1、L-R2、L-R10 為 `v5 已不適用`，不動程式。

## 順手（別單低項，branch 實際多修的 4 項）

審查 #17 低-1 指正：以下 4 項不在 11 條內，但同屬「前幾輪審查低項」級別，已在本 branch 修好，一併記帳。

| 項 | 出處 | 修法（commit） |
|---|---|---|
| #13 新-低-1：管理帳號直跑 `--check`、sudo 快取缺時 `sudo -n` 失敗被算成「gh 沒登入」 | `docs/research/v5-review-13.md:142` | `deploy/install.sh:318-323`、`342-347`（sudo 自己失敗回 2「無法確認」）＋`tests/test_install.py:847-931` 四案測試（`dc5e2af`） |
| #13 觀察-2：`repo_out == KEYS_REPO` 字串比對偏嚴（引號／空白誤判 1） | `docs/research/v5-review-13.md:144` | `deploy/install.sh:354-365`（純 bash 修剪）＋假 gh `FAKE_GH_DECORATE`（`:197-210`）＋`tests/test_install.py:933-951` 五態測試（`e55a5c2`） |
| #19 低-2：假 sudo 單槽快取改每 PPID 一檔 | `docs/research/review-18.md:82-87` | `tests/test_bootstrap.py:143-157`（`sudo-v-ppid-$PPID` 各記一筆）；既有三情境照過（`e631357`） |
| `docs/deploy.md` 5.1 排錯表加 `bad_config` 列 | #13 低-3（`v5-review-13.md:73-78`）／觀察-1（`:143`） | `docs/deploy.md:519,535`（設定錯、結束碼 3，非 `failed=` 體系）（`eefc994`） |
