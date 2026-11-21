# tasks：LLMGateway 與 MLP 脫鉤（#221，主方案 M 最小集）

契約編號 C-n 供各實作 PR 測試與 spec 互查使用。**本文件僅為規劃（P0），對應主方案 M（最小集）；開工門檻分層：P1（除依存 P2-B 之 `FOLLOW_FILE` 認知項外）與 P2-A 只需本人核准 P0／Q1 即可開工；P2-B、P2b、P1 `FOLLOW_FILE` 認知與 P4 才須等待 Q2／Q-MLP-1,2,3,5,6,7 定案。**

## 0. P0：本 change（設計與規格文件，不改程式）

- [x] 0.1 完成 `openspec/changes/lgw-mlp-decouple/` 內 `proposal.md`、`design.md`、`specs/connectivity/spec.md`、`tasks.md`。
- [ ] 0.2 **門檻（等本人／PMO 與 MLP PM 裁定）**：
  - 本人回答「給本人的 3 題」：**題目全文、選項白話代價與單一建議以 [`proposal.md`](proposal.md)「給本人的 3 題（決策清單，單一正本）」為唯一正本**（Q1 脫鉤層級、Q2 授權 PM 先問 MLP PM 並依可行性採 (2) 或 (1a)、Q3 帶外進場事實題）。PM 預設（U-2 選 M、U-4 保留 `mac-fallback`、U-5 暫緩 `MLP#1511`）不反對即照做。
  - MLP PM 回覆 Q-MLP-1（推送方式）、Q-MLP-2（`TUNNEL_EXTRA_PUBKEYS`）、Q-MLP-3（`4710`/`4711`）、Q-MLP-5（Mac 接入與 `#1511`）、Q-MLP-6（輪換能否保留 IP＋host key 與機敏搬運邊界）、Q-MLP-7（`rotate-gateway` 能否保證每次都寫入**新機** `host_key`，避免殘留上一代舊金鑰）。*註：Q-MLP-4 已由現行 `ops-scripts/llmgateway-tunnel:172-176` `ssh -W` 探針自證，不列阻塞前置。*

## 1. P1：擴充 `llmgwctl`（script a）並將隧道改為可選（依存 P0；除 `FOLLOW_FILE` 認知項外可先做）

- [ ] 1.1 修改檔案清單：`lgwctl/cli.py`、`lgwctl/units.py`、`lgwctl/layout.py`、`lgwctl/release.py`、`lgwctl/install.py`、`lgwctl/check.py`、`lgwctl/status.py`、`lgwctl/probe.py`、`lgwctl/upgrade.py`（**`lgwctl/migrate.py` 採不動遷移五指令邏輯**，保留現有 `TUNNEL_SERVICE_NAME` 與 `tunnel_config_state() -> tuple[bool, str]` 介面）；以及更新 `tests/` 相關 9 檔（`test_lgwctl_{check,install,upgrade,migrate,layout}.py`、`test_tunnel.py`、`test_install.py`、`test_bootstrap.py`、`lgwctl_fakes.py`）。
  - 在 `cli.py` 新增 `start`、`stop`、`restart` 子命令（`units.py` 補 `stop`，復用既有 `units.start`／`units.restart`），僅管理 `llmgateway.service` 並驗本機 200。
  - `release.py`：`REQUIRED_FILES` 於本階段即移除必含 `ops-scripts/llmgateway-tunnel` 與 `deploy/units/llmgateway-tunnel.service`（純本地發布包可直接通過）；當版本目錄或機器存在隧道 unit 時仍照常安裝快照與檢查。
  - `probe.py`：`remote()` 對應 `rc=2` 之訊息改為中性「`無法探`」（因 `rc=2` 涵蓋未設定、找不到 `ssh` 與私用 `known_hosts` 未就緒）。
  - **`layout.py`、`check.py`、`status.py`（一般可選隧道）**：**只要機器上存在隧道 unit 或已設定隧道，`check` 與 `status` 仍照常檢查（除非 `--no-remote`）**；保留 `tunnel_config_state() -> tuple[bool, str]` 既有簽名供 `install.py`／`migrate.py` 相容。
  - **`FOLLOW_FILE` 四態與無權限認知（依存 P2-B，僅 Q2＝(1)/(3) 實作；Q2＝(2) 跳過）**：`layout.py` 新增 `tunnel_follow_state() -> tuple[Literal["unconfigured", "ok", "missing", "invalid", "regressed", "unreadable"], int | None]`（並讓 `tunnel_config_state()` 認 `FOLLOW_FILE`）；**當 `FOLLOW_FILE` 已設定且隧道未跑時**：`missing`／`invalid`／`regressed` 一律回 **`fail`**（絕不降級 `todo`，`regressed` 須提示 `.tunnel_generation` 重置方式）；**遇 `PermissionError`（非 root 讀不到 `0700` `STATE_DIR`）回 `unreadable`**：`status` `tunnel-follow` 列顯示 `unreadable（需 root 或 llmgw 才能讀）`（**絕不誤報 `missing`**），`check` 降為 `todo` 並註明需權限；輸出只准印固定白名單狀態字串。
  - 保留對含舊 unit 之 `previous` 版本 `rollback` 兼容，以及 `migrate.py` 既有測試通過。
- 契約：
  - **C-1**：`llmgwctl start`／`stop`／`restart` 冪等（已 active 且本機 200 重跑 `start` 不重啟回 `0`；已停重跑 `stop` 回 `0`）。
  - **C-2**：① 無 `tunnel.env` 且無 `llmgateway-tunnel.service` 的純本地沙箱下，`install`、`check`、`status`、`upgrade`、`rollback` 皆通過（`check` 回 `0`，無隧道 fail/todo）；② **當 `llmgateway-tunnel.service` 存在於系統 unit 目錄時，`check` 與 `status` 仍檢查其狀態與遠端探針**；③ **（依存 P2-B）已設定 `FOLLOW_FILE` 時：`missing`／`invalid`／`regressed` 且隧道未跑時 `check` 回 `fail`（exit `1`）並顯示對應固定原因、`status` 顯示 `missing`/`invalid`/`regressed`；非 root 無權限讀取時 `status` 顯示「需 root 或 llmgw 才能讀」（不誤報 `missing`）且 `check` 回 `todo`**。
  - **C-3**：`upgrade` 的 policy dry-run 與本機 200 失敗回滾測試維持全綠；對 `previous` 含舊 unit 時 `rollback` 不崩潰；`test_lgwctl_migrate.py` 維持全綠（含 `tunnel_config_state` 維持簽名與 `tunnel_follow_state` 六態／含無權限測試〔依存 P2-B〕）。
  - **C-4**：所有子命令輸出不含金鑰／解密密碼／`FOLLOW_FILE` 內容原文。**驗證方法**：於測試 fixture 寫入可辨識假密碼（如 `TEST_PASS_SENTINEL`）、假 API key（如 `sk-test-sentinel-xyz`）及假 `FOLLOW_FILE`／`.tunnel_generation` 測試字串（如 `SENTINEL_HOST_KEY`、`TEST_IP_SENTINEL`、`MALICIOUS_ESC`），執行各子命令（含 root 身分跑 `check`／`status`）並對完整 stdout／stderr 執行字串搜尋斷言零命中。
- 完成判準：C-1～C-4 與既有 `lgwctl` 相關測試全綠。

## 2. P2：改造 `ops-scripts/llmgateway-tunnel`（拆為 P2-A 必做與 P2-B 條件式 follow 檔）

### 2.1 P2-A（必做，不依存 Q2）：`REMOTE_SSH` 解析對齊與 4711 關閉支援

- [ ] 2a.1 修改 `ops-scripts/llmgateway-tunnel` 與 `tests/test_tunnel.py`：
  - **`REMOTE_SSH` 語意對齊**：保留「未設或留空（`REMOTE_SSH=`）＝預設 `4711`」，新增支援顯式 **`REMOTE_SSH=off`／`none`（大小寫不敏感）** 不轉發管理 SSH 埠（消滅舊腳本 `int("off")` 拋 `ValueError` 觸發 exit `78` 永久停擺）；切換過渡預設保留 `4711`。
- 契約：
  - **C-7（`REMOTE_SSH` 留空＝4711 與顯式關閉）**：設定 `REMOTE_SSH=4711` 或留空 `REMOTE_SSH=` 時均轉發 API 與 SSH 兩埠；設 `REMOTE_SSH=off`、`OFF`、`none`、`None` 時只建立 API 埠 `-R` 轉發，不綁 `4711`（不觸發 exit `78`）。

### 2.2 P2-B（僅 Q2＝(1)/(3) 執行；若 Q2＝(2) 則本節取消）：本機 follow 檔機制

> **條件分岐（依 Q2 / U-3 裁定）**：
> - **若 Q2 選 (2)（MLP 輪換保留 IP＋主機金鑰）**：**本節 2.2（P2-B）與 2.3（P2b）取消**（僅執行 P1〔不含 `FOLLOW_FILE` 認知〕、P2-A 與 P3）。
> - **若 Q2 選 (1)（推送）**：執行下方 2b.1；若選 **(1a) 經 `LLMGateway-keys` 拉取**，須同時執行 **2.3 P2b**（取檔器）或於裁定時明確將取檔器劃歸另單處理。
> - **若 Q2 選 (3)（拉取 `NODE_GATEWAY`）**：須先放寬 `connectivity` spec「MUST NOT 依賴 `NODE_*`」條款再實作取檔器。

- [ ] 2b.1 修改檔案清單：`ops-scripts/llmgateway-tunnel`、`tests/test_tunnel.py`（續用 `deploy/units/llmgateway-tunnel.service`）：
  - 支援設定鍵 `FOLLOW_FILE`（**優先序高於靜態 `RENDEZVOUS`／`KNOWN_HOSTS`；`FOLLOW_FILE` 缺失或無效時嚴禁退回靜態鍵**），讀取 `gateway.json` 形狀（`ip`、`ssh_port`、`tunnel_user`、`host_key`、`generation`〔整數或純數字字串一律轉整數〕、`published_at`）。
  - `.tunnel_generation` 缺失或損毀時視為無上一代紀錄（`last_gen = None`），接受任何有效 `generation >= 0`；每次啟動、重連或 watch 迴圈偵測 `FOLLOW_FILE` 合法更新（`new_gen > last_gen`）時，依 `[<ip>]:<ssh_port> <host_key>`（22 埠用 `<ip> <host_key>`）原子寫入固定私用檔 `/var/lib/llmgateway/.tunnel_known_hosts`（`0600`）與 `/var/lib/llmgateway/.tunnel_generation`（`0600`），結束舊 `ssh` 並以 exit `1` 讓 systemd 重起；採 `StrictHostKeyChecking=yes`、`ExitOnForwardFailure=yes`。
  - **運行中損毀處置**：watch 迴圈中若健康連線仍在而 `FOLLOW_FILE` 暫時缺失、損毀或 `generation` 回退，僅於狀態轉換時記一次事件（不打斷既有健康 `ssh`），待斷線重啟時再 fail-closed。
  - **`78` vs `1` 退出碼分離**：只有靜態設定鍵缺失（缺 `IDENTITY`，或 `FOLLOW_FILE` 與 `RENDEZVOUS` 皆未設定）才回 exit `78`（`config_missing`）；follow 檔缺失（`follow_missing`）、壞檔／同代欄位篡改（`follow_invalid`）、`generation` 回退（獨立事件 `follow_regressed`）、`host_key` 缺漏或無效（`hostkey_missing`）回 **exit `1`**（systemd `RestartSec=10` 自癒，**絕不退回 `accept-new`**）。
  - **`--probe-once` 只讀不寫**：嚴格禁止以呼叫者身分建立或覆寫 `/var/lib/llmgateway/.tunnel_known_hosts`；該檔不存在或與當前 `FOLLOW_FILE` 不符時直接回 exit `2`（無法探）。
- 契約：
  - **C-5（雙真實 `sshd` 模擬輪換與 RTO 實測證明）**：在具備 `/usr/sbin/sshd`、`/usr/bin/ssh`、`/usr/bin/ssh-keygen` 的環境下，於 loopback 啟動兩個非 root 真實 `sshd`（G1：`port=P1`、真實 ed25519 key `K1`；G2：`port=P2`、真實 ed25519 key `K2`）搭配本地 mock HTTP 200 `lgwd`：follow 檔指向 G1（`gen=1`）時，真實 `ssh -R` 建通、`/var/lib/llmgateway/.tunnel_known_hosts` 寫入 `[127.0.0.1]:P1 K1` 並經 G1 探針回 200（`--probe-once` 回 0 且不寫該檔）；停 G1 並將 follow 檔切為 G2（`gen=2`）後，daemon 結束舊 `ssh` 以 exit `1` 退出、重啟後自動更新私用 `known_hosts` 為 `[127.0.0.1]:P2 K2` 並經 G2 探針回 200（註明 CI 須安裝 OpenSSH server/client，並記錄切換耗時驗證符合 RTO）。
  - **C-6（真實 `sshd` 負向安全、exit `1` 自癒與回退防護）**：① follow 檔 `host_key` 清空、篡改為 `K1`（對端 G2 `K2`）、follow 檔不存在、JSON 損毀、`.tunnel_generation` 缺失/損毀（應正常接受）、`generation` 回退（如 `2` 降回 `1`，應記 `follow_regressed`）或**同代連線欄位變更**（`gen==2` 但 `ip`/`port`/`host_key` 改動，應記 `follow_invalid`）時，**一律拒絕連線、絕不退回 `accept-new` 亦不退回並存之靜態 `RENDEZVOUS`**，以 **exit `1`**（非 `78`）退出並記對應事件；② 運行中健康連線存在時將 follow 檔寫壞／回退，驗證不打斷現有 `ssh` 連線且僅於狀態轉換記事件；③ 隨後將 follow 檔修復為有效 `gen=3` 時，再次啟動自動連上 G2 回 200；④ `--probe-once` 於私用 `known_hosts` 缺失時只讀回 `2` 且不建立檔案。
  - **C-8（靜態設定缺失 78 與零洩漏）**：僅在缺 `IDENTITY` 或 `FOLLOW_FILE`/`RENDEZVOUS` 皆未設定時回 exit `78`（記 `config_missing`）；**驗證方法**：於測試中注入固定假會合點 IP 與假 `host_key` 字串，執行失敗與成功路徑後對完整 stderr／日誌／狀態檔做 grep 斷言該 IP 與金鑰字串零命中。
- 完成判準：P2-A（C-7）必綠；若執行 P2-B，C-5～C-8 在真實 loopback `sshd`＋`ssh` 與 mock `lgwd` 環境全綠。

### 2.3 P2b（僅於 Q2 選 (1a) 寫入 `LLMGateway-keys` 且納入本單時執行）

> 若 Q2 選 **(2)**（保留 IP＋host key）或 **(1b)**（既有通道直推），**本節取消**；若選 **(1a)** 而由 MLP／另開子單處理，本節亦標記轉交該單並只對接 `gateway.json` 檔案格式。

- [ ] 2b.1 修改／新增檔案清單（若在本單實作）：`ops-scripts/key-sync`／`lgwd/keysync.py`（或獨立輕量取檔腳本與 unit/timer）及對應測試，由 `llmgw` 既有 `gh` 登入拉取 `FATESAIKOU/LLMGateway-keys` 之發佈檔，驗證／正規化 `ssh_port` 與整數 `generation` 後原子寫入 `FOLLOW_FILE`（權限固定 `0600`、`llmgw:llmgw`），不新增 token 檔。
- 契約：
  - **C-8b（來源→follow 檔原子寫入與故障不覆蓋）**：拉取成功時原子寫入完整 `gateway.json`（含 `ssh_port` 與 `host_key`）；上游暫時不可達或內容缺欄位時保留既有 `FOLLOW_FILE` 不寫壞檔案，且不洩漏任何金鑰或 token。

## 3. P3：文件更新、切換／反向保險與規格歸檔步驟（依存 P1 + P2-A +〔P2-B 若執行〕）

- [ ] 3.1 更新 `docs/deploy.md`：反映 `llmgwctl start/stop/restart`、可選隧道檢查（含 P2-B 時之 `FOLLOW_FILE` 缺失／回退／無權限處置）、`REMOTE_SSH=off` 與觀察期 rollback 順序、Mac 端最新轉發指引（依 Q-MLP-5），以及 M 方案切換／觀察期保留靜態鍵／第一次真實輪換觀察與退回 SOP。
- [ ] 3.2 若採無帶外進場之過渡保護（重啟 `llmgateway-tunnel.service` 需反向保險），提供復用既有 `lgw-cutover`／`lgw-revert` transient timer（與 `.cutover.lock` 互斥）的切換腳本／步驟：探針等待上限 **≥ 90 秒**，明文警示「**transient timer 重開機後失效，窗口內切勿重開機**」。
- [ ] 3.3 **OpenSpec 歸檔同步（含 Q2＝(2) 條件刪減）**：
  - **若 Q2 選 (2)（P2-B 取消）**：執行 `openspec archive` **前**，先改 `openspec/changes/lgw-mlp-decouple/specs/connectivity/spec.md`，逐項（follow 檔的 MUST 不只在 Scenario，更在三條 Requirement 的本文，只刪 Scenario 會把未實作的規格歸檔進主 spec）：
    1. **ADDED「服務本體啟停與無隧道獨立生命週期」本文**：刪去「若已設定 `FOLLOW_FILE`…`check` MUST 回報失敗…（非 root 無權限讀取…）」那一句；Scenario 刪「已設 FOLLOW_FILE 但檔案缺失、無效或回退時回報失敗」與「非 root 無權限讀取 FOLLOW_FILE 時不誤報缺失」。
    2. **MODIFIED「服務端的可選會合點隧道守護」本文（RENAMED 的目標，必須留下並改寫）**：刪去全部 follow 檔語意（`FOLLOW_FILE`、`host_key` 自動寫入私用 `known_hosts`、generation、檔缺失／無效的 fail-closed 與非 78 重試）；保留「可選、未啟用不阻礙 `lgwd`、`StrictHostKeyChecking=yes`、`ExitOnForwardFailure=yes`、斷線重連、僅靜態設定鍵缺失才回 78」。Scenario 保留「斷線重連」「遠端埠被佔用」「只有靜態設定鍵缺失才以退出碼 78 停止重試」，刪「follow 檔變更後重連」「host_key 缺漏或不符合時拒絕連線」「follow 檔缺失或無效時不永久停擺並於修復後自癒」「同代覆寫改變連線欄位時拒絕套用」「運行中 follow 檔變壞時不中斷健康連線」「探針只讀不寫私用 known_hosts」中依存 follow 檔者（探針條款若只剩靜態鍵語意則保留並改寫）。
    3. **MODIFIED「會合點與遠端埠」本文**：把「由靜態設定或本機 follow 檔（`ip`、`ssh_port`、…`generation`）指定」改為「由靜態設定指定」；Scenario「換一個會合點」改寫為「改靜態鍵並重啟隧道守護」（不是 follow 檔變更）；保留「管理 SSH 埠留空維持預設 4711」「關閉管理 SSH 埠轉發（`REMOTE_SSH=off`）」。
    3b. 另：「斷線重連」Scenario 的 THEN 去掉「或 follow 檔」；「只有靜態設定鍵缺失才以退出碼 78 停止重試」的 WHEN 括號改為靜態鍵缺失；「探針只讀不寫私用 known_hosts」整條刪除；一併移除 `spec.md` 頂端與 follow 檔有關的註解（審查 9.3 建議）。
    4. 刪減後：每條 Requirement 本文 ≤500 字且至少留一個 Scenario；**必須重跑** `openspec validate lgw-mlp-decouple --strict` 與（在副本）archive 模擬後 `openspec validate --specs --strict`，貼原樣輸出。
  - 實作完成執行 `openspec archive` 後，手動同步更新 `openspec/specs/connectivity/spec.md` 的 `## Purpose` 與版本註記（`v6`），使主 spec Purpose 涵蓋實作定案內容並通過 `openspec validate --specs --strict`。
- 契約：
  - **C-9（反向保險回退與不誤判）**：以 fake `systemctl`／`systemd-run` 驗證於探針失敗或逾時（等待門檻 ≥ 90s 設定可測）時自動還原舊 `tunnel.env` 並重啟 `llmgateway-tunnel.service`。

## 4. P4：真機切換、觀察與第一次真實輪換（`狀態:等本人` / `等本人:動作`，非 PR）

> 本階段所有項目均標記 **`狀態:等本人`／`等本人:動作`**，集中列出本人於真機要做的動作檢查點：

- [ ] 4.1 **帶外進場盤點與實測（解答 Q3 / U-1）**：確認並實測不經 `<會合點>:4711` 的真機登入方式（直連 SSH／VPN／Cloud Console）。
- [ ] 4.2 **部署與設定（依 Q2 / U-3 定案）**：在 `<服務主機>` 執行 `llmgwctl upgrade --tag <新版>` → 若採 P2-B 則放入 follow 檔來源設定（若採 (1a) 則確認不新增違規 token 檔）並更新 `/etc/llmgateway/tunnel.env` 加入 `FOLLOW_FILE=...`；**觀察期內保留既有靜態 `RENDEZVOUS`／`KNOWN_HOSTS` 供 `llmgwctl rollback` 回退相容，且保持 `REMOTE_SSH=4711` 或留空**。
- [ ] 4.3 **重啟切換與驗收**：透過帶外連線（或啟動 `lgw-revert` 保險窗口，窗口內勿重開機）執行 `systemctl restart llmgateway-tunnel.service`，確認 `llmgwctl check`、`llmgwctl status` 與經 `<會合點>:4710` 探針 200（若有開保險則執行取消）。
- [ ] 4.4 **帶外確認後關閉 4711（等本人決定）**：觀察期確認穩定且帶外進場已驗證可靠後，由本人決定於 `/etc/llmgateway/tunnel.env` 明確設 **`REMOTE_SSH=off`**、重啟 `llmgateway-tunnel.service`；通知 MLP PMO 關閉 `MLP#1511` 與評估移除 `4711` 保留段。**SOP 注意：`REMOTE_SSH=off` 與清理舊靜態鍵同時才算觀察期結束；在設完 `REMOTE_SSH=off` 後若尚未清完而須 `llmgwctl rollback` 回退舊腳本，必須先把 `REMOTE_SSH` 改回 `4711`（或留空），否則舊腳本會因 `int("off")` 以 exit `78` 永久停擺**。
- [ ] 4.5 **第一次真實輪換觀察**：下一次 MLP 實際執行 `rotate-gateway` 時，由本人／MLP PMO 觀察 `<服務主機>` `journalctl -u llmgateway-tunnel` 自動切換為新 `<會合點>` 與 HTTP 200 恢復（驗證 RTO 符合預期；**若出現連續 `tunnel_up` 失敗重啟且 `generation` 已升，先檢查 follow 檔／`NODE_GATEWAY` 之 `host_key` 是否殘留上一代舊金鑰**；失敗時依 `design.md` §5.4 退回 SOP 止血）。

## 5. P5：Repo 舊設定與相容遺留清理 PR（依存 P4 真機觀察期完全結束）

> **注意**：在 P4 觀察期完全結束並確認無須退回靜態設定或舊版本前，**不得對真機執行 P5 版本的 `llmgwctl upgrade`**。

- [ ] 5.1 依實際收斂結果清理 `deploy/{install,bootstrap}.sh` 與 `docs/deploy.md` 中過時的靜態 `RENDEZVOUS`／`known_hosts_rendezvous` 遺留說明，以及歸檔清理真機舊 `known_hosts_rendezvous`。
- 契約：
  - **C-10**：全量單元／整合測試全綠、`openspec validate --specs --strict` 全綠。
