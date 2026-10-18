# #35 安裝簡單化：設計（本人已決定方向 A）

狀態：本人 2026-10-18 已決定（見 §7 與 [#35 的決定留言](https://github.com/FATESAIKOU/LLMGateway/issues/35#issuecomment-6242655225)）；實作在後續 PR。公開 repo，不寫機器名、位址、key 值。

## 1. 現在的安裝有哪些步驟、哪裡麻煩

放服務的那台，現況（v0.1.4）：

| 步驟 | 現在怎麼做 | 麻煩在哪 |
|---|---|---|
| 帳號 | 建 `llmgw`，再 `enable-linger` | 要靠 linger 才能讓 user manager 常駐 |
| 執行單位 | 3 個 systemd **user** unit（服務、隧道、key-sync）＋1 個 timer | 管理動作都得先 `sudo -u llmgw -i`，再自己 export `XDG_RUNTIME_DIR`、`DBUS_SESSION_BUS_ADDRESS`，否則 `systemctl --user` 與 `--check` 連不上 bus（踩坑：LLMGateway#26 的待辦） |
| 程式佈署 | `install.sh --tag`：抓 tag、放 `~/.local/share/llmgateway/<tag>`、原子切 `current`、清舊版 | 邏輯多（559 行），其中一部分只為了遷就「隧道承載管理連線」（隧道只能 60 秒後用 systemd-run 排重啟） |
| 一次性設定 | `bootstrap.sh`（1138 行）：帳號、sshd、隧道金鑰、known_hosts、tunnel.env、gh 登入提示、密碼 | 步驟多、輸出分 ok／todo／fail，有好幾個要本人動手的點 |
| key 拉取 | `key-sync.timer` 每 10 分鐘叫 `key-sync.service` | 多一個 timer、一個 service、一個 EnvironmentFile |
| 檢查 | `install.sh --check` | 要在 `llmgw` 的 shell、要 export 兩個變數，才查得到 user unit |
| 升級／回滾 | `install.sh --tag <新／舊>` 人工跑，前後各一次 `--check` | 要記步驟；隧道重啟後連線會斷一下 |

共同根因：把「常駐程序」放在 **user manager** 底下，管理的人卻在 **system** 層操作。

## 2. 目標與不做的事

目標：安裝＝「起一個服務（`lgwd`，內含 key 拉取）＋一條 reverse tunnel」；升級、回滾、檢查各一個指令、不需要 export 任何變數、不需要 `sudo -u llmgw -i`。

不做：不改三支 API 與 policy 的語意；不改 key 的流向（私有 repo 的 `keys.env.enc` → `gh` 拉 → openssl 解密 → `keys.env`）；不增加應用層認證；不引入容器執行環境（見 3）。

安全邊界（不變，且要有測試）：
- key 的值、解密密碼不經過 AI；`--check`／狀態輸出只印有無、名稱與時間，不印值；驗證用 list，不用會帶 key 的 get。
- policy 載入 fail-closed（default 必須被 allow 涵蓋，否則拒收、服務不起）。
- `lgwd` 只綁 127.0.0.1；服務帳號 `llmgw` 不進 docker 群組、沒有 sudo。
- 隧道金鑰私鑰不離開那台。

## 3. 做法選項

- **A. system 層 unit（`User=llmgw`）＋ 內建 key 拉取＋ 一支管理指令（建議）**
  執行單位從 user manager 搬到 system manager：不需要 linger、不需要 `XDG_RUNTIME_DIR`／DBUS；管理的人直接 `sudo systemctl …`、`sudo journalctl -u …`。一個服務 unit（`lgwd` 自己有內部計時，每 10 分鐘叫一次現有的 `key-sync` 程式，取代 timer＋service）、一個隧道 unit（精簡成一條 `ssh -N -R`＋保活）。
- B. 容器（compose）。要有 docker 與映像來源（要新增 CI 或在主機 build），gh 登入狀態與 0600 密碼檔要掛進容器；而且 repo 規則是 `llmgw` 不碰 docker 群組。多出的東西比省下的多，不建議。
- C. 只調整腳本、仍用 user unit。省不掉 export 的問題，不符合目標。

以下以 A 為準。

## 4. A 的新樣貌

### 4.1 目錄與 unit

| 東西 | 位置 |
|---|---|
| 程式（每版一個目錄） | `/opt/llmgateway/<tag>/`，`/opt/llmgateway/current` 指著用中的版本（root 擁有，`llmgw` 只讀） |
| 設定 | `/etc/llmgateway/llmgateway.env`（埠）、`tunnel.env` |
| 解密密碼 | `/etc/llmgateway/keys.pass`（root:llmgw 0640，單行），由 unit 的 `LoadCredential=` 交給服務（程序環境裡不出現） |
| 狀態 | `/var/lib/llmgateway/`（`keys.env` 0600、`llm.db`、`.keysync.sha`），擁有者 `llmgw` |
| 隧道金鑰與 known_hosts | `/etc/llmgateway/tunnel/`（金鑰 root:llmgw 0640 或維持在 `~llmgw/.ssh`，擇一，見判斷點 3） |
| 服務 | `llmgateway.service`（`User=llmgw`、`Restart=on-failure`、`WantedBy=multi-user.target`） |
| 隧道 | `llmgateway-tunnel.service`（`ssh -N -R` 兩個埠，`ServerAliveInterval`＋`ExitOnForwardFailure=yes`、`Restart=always`） |

unit 加固：`NoNewPrivileges`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/llmgateway`、`PrivateTmp`。

`gh` 登入仍由本人以 `llmgw` 用瀏覽器做一次（沿用 v5 的決定 B），存在 `llmgw` 的 HOME；system unit 設 `User=llmgw` 時 HOME 就是它，`gh` 照用。

### 4.2 一支管理指令 `llmgwctl`（放 `/opt/llmgateway/current/bin/`，由 `/usr/local/bin/llmgwctl` 連過去）

| 指令 | 做什麼 |
|---|---|
| `sudo llmgwctl install --tag X` | 第一次：建帳號、目錄、unit，放進設定範本，`daemon-reload`＋enable＋start；缺本人動作的項目（gh 登入、密碼、隧道金鑰註冊）列成 todo，不當失敗 |
| `sudo llmgwctl check` | `systemctl is-active`（system 層，不需變數）＋本機 list 200＋經會合點 list 200（`--no-remote` 可略過）＋key 拉取最後狀態與 keys.env 年齡＋policy 載入檢查＋`gh` 可讀私有 repo；結束碼同現在（0 全好） |
| `sudo llmgwctl upgrade --tag X` | 抓 tag 放新目錄→policy 預檢（dry-run 載入，不過就不切）→切 `current`→只 restart `llmgateway.service`→等本機 list 200；不過就**自動切回上一版**並以非零結束 |
| `sudo llmgwctl rollback` | 切回上一版、restart 服務、跑 check |
| `llmgwctl status` | 目前版本（`.lgw-tag`）、上一版、服務與隧道狀態；不需 sudo |

重點：升級只重啟服務，**不碰隧道**，所以不再需要「60 秒後 systemd-run 重啟隧道」的繞法；管理連線走隧道時，升級也不會把自己切斷。隧道單位本身不隨版本變動（要改隧道時才另外 `llmgwctl tunnel-restart`，會提醒連線會斷）。

### 4.3 key 拉取內建

`lgwd` 起一個內部計時（預設 10 分鐘，`LGW_KEYSYNC_INTERVAL` 可調），用 subprocess 叫現有的 `key-sync`（沿用已測過的 gh＋openssl 邏輯與「失敗不動舊 key」規則）。timer unit 與 service unit 移除；`key-sync` 仍可手動跑（`llmgwctl sync`）。log 規則不變：不含值。

## 5. 遷移與回滾（動真機）

前提：真機動作照新規則——計畫（含停損）由 PMO 問本人、本人授權那一次，由我自己跑；我先把腳本與演練證據備齊。目前真機是 user unit 版（v0.1.4）。

階段：
1. **演練（不碰真機）**：worker 的 PID 1 不是 systemd，不能用。第一輪在 Mac 的 Lima 一次性 Ubuntu VM 跑過（已跑）；因 Mac 負載，重演練搬到 fh-l 的一次性 systemd 容器（假 key、假會合點、跑完刪除）。整套 `install → check → upgrade → rollback → 自動回滾` 與遷移（含反向保險、停損）都要跑過；腳本邏輯另有假 systemctl 的單元測試。
2. **準備（真機，不影響服務）**：只放新程式與設定到 `/opt`、`/etc`、`/var/lib`（複製 `keys.env`、`llm.db`、`tunnel.env`、密碼；用 `cp -a`，值不印出）；新 unit 先不 enable。舊 user unit 全程不動。
3. **切換窗口（約 5 分鐘，有停損）**：切換由一支「分離執行」的腳本做（`systemd-run` 起，不隨 ssh 連線斷掉而中止）：先排一個**反向保險** `systemd-run --on-active=10min revert.sh`（停新 unit、啟動舊 user unit）；停舊 user 的服務與隧道；啟新 unit；本機與經會合點 list 各探一次。通過就 `llmgwctl confirm` 取消保險；沒人 confirm，10 分鐘後自動回到舊版。
4. **觀察期（建議 3 天）**：舊 user unit 與舊目錄保留但停用。期間如有問題，`llmgwctl rollback-to-user`（停新啟舊）一個指令回去。
5. **收尾**：觀察期過了，移除舊 user unit、舊目錄、`disable-linger`，把舊 `install.sh`／`bootstrap.sh` 換成新的精簡版。

停損條件（任一成立就回舊版）：切換後 5 分鐘內本機 list 非 200、經會合點 list 非 200、`check` 非 0、或 10 分鐘內沒人 confirm。

回滾後狀態＝切換前（key、冷卻資料沿用複製前的舊檔；觀察期內新單位寫入的冷卻若要保留，`rollback-to-user` 會把 `llm.db` 複製回去）。

## 6. 風險

- 管理連線經隧道進來，切換會重啟隧道：由分離執行＋反向保險處理；萬一連線斷了，保險仍會在 10 分鐘內把舊版開回來。
- `gh` 登入狀態搬到 system 層服務後要確認 `llmgw` 的 HOME 與權限（演練要驗）。
- 密碼檔改位置後的權限；`LoadCredential=` 需要 systemd ≥ 250（真機為 259，已確認；演練 VM 要記錄版本）。
- 「內建 key 拉取」改動 `lgwd`：失敗不得影響 API 服務（獨立執行緒＋逾時＋subprocess），要有測試。

## 7. 已決定（本人 2026-10-18，經 PMO）

1. 方向 A（system 層 unit＋`lgwd` 內建拉 key＋`llmgwctl`）；B、C 不做。照 §8 拆 T1〜T4。
2. 演練機：worker 不能用（PID 1 不是 systemd）。第一輪在 Mac 上用 Lima 開一台一次性 Ubuntu VM（有 systemd，已跑）；之後因 Mac 負載，重演練搬到 fh-l 的一次性 systemd 容器。都用假 key、假會合點，把 install→check→upgrade→rollback→自動回滾→反向保險（不 confirm 時 10 分鐘自動回舊版）都跑過，跑完刪 VM。演練紀錄寫進 #35。
3. 隧道金鑰維持原位（`~llmgw/.ssh`），不搬 `/etc`。
4. 真機授權：本人已授權，條件是「演練通過＋非作者審查過且高、中都修完」；之後由 PM 自己挑時間切，不用再問。停損照 §5（5 分鐘內本機或經會合點 list 非 200、`check` 非 0、10 分鐘沒 confirm → 自動回舊版）；觀察期 3 天，期內 `rollback-to-user` 一個指令回去，過了才拆舊 unit 與 linger。切換前在 #35 留一行「要切了」，切完把結果（含 `check` 輸出，不含值）留在 #35。

已確認：真機 systemd 版本為 259，`LoadCredential=` 可用（§6 原本標「推測」的部分成立）。演練 VM 的 systemd 版本要不低於 250，演練時記錄。

## 8. 工作切分（同意方向後）

- T1（d）：`llmgwctl`（install／check／upgrade／rollback／status／confirm／rollback-to-user）＋unit 範本＋單元測試（假 systemctl／假檔案系統）。
- T2（d）：`lgwd` 內建 key 拉取計時＋測試。
- T3（d）：隧道 unit 精簡（保活取代自寫探針；`--check` 的端對端探針移到 `llmgwctl check`）。
- T4（c 收尾）：docs/deploy.md 改寫、bootstrap 精簡、遷移腳本；演練紀錄。
- 真機切換：本人授權後由我跑；結果記在 #35。

模型與審查依本人 10-18 的規則（免費模型→Zen/Go→agy→Ollama DeepSeek→Claude Sonnet；非作者審查；高、中修完才開 PR）。
