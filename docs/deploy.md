# LLMGateway 部署與維運手冊

給本人（或本人授權的人）做「放服務的那台」的安裝設定、舊版遷移、日常維運與排錯。

本手冊全面改以 `llmgwctl`（system 層 unit 架構）新流程為主。舊版以 user unit 運作的手動與 bootstrap 流程已移至文末附錄。

這是 public repo，本手冊寫作遵循以下原則：
- 不寫實際的機器名、IP、key 的值、真實密碼；位址一律用 `<跳板機>`、`<埠>` 這類佔位符號。
- 機器一律稱為「放服務的那台」。
- 「會合點」指池跳板機上現有的 `sshproxy` 帳號（本人指定）。
- 命令一律使用實際存在的 `llmgwctl`（或 `python3 -m lgwctl`）子指令與參數。

---

## 0. 全貌與架構

服務只做這幾件事：list、resolve、登錄冷卻（`GET /v1/subscriptions`、`GET /v1/resolve?agent=&provider=…`、`POST /v1/subscriptions/{id}/cooldown`）。單筆取回 key 的 `GET /v1/subscriptions/{id}?agent=` 已於 #48 移除，現在一律 404；要拿 key 改用 resolve 回的 `env`。冷卻只是提示，resolve 不因冷卻拒絕（冷卻資訊在 `meta`／`list` 的 `cooldown`、`model_cooldowns`；要不要等、換哪個模型由呼叫方決定）。**預設不做應用層認證**（`LGWD_AUTH_MODE=off`），存取控制只靠 SSH 與本機綁定；`warn`／`enforce` 見第 8 節：

- `lgwd` 只綁放服務那台的 `127.0.0.1:<API 埠>`（預設 8480），不綁任何對外或容器網路的介面。
- 隧道守護 `llmgateway-tunnel.service`（**可選**：沒有它 `lgwd` 照常運作，`llmgwctl start`／`stop`／`restart`／`check`／`upgrade` 都不依賴它）用自己的金鑰 `ssh -R` 到會合點，把兩個埠公開在**會合點的 127.0.0.1**：
  - `127.0.0.1:4710` → `lgwd` 的 API；
  - `127.0.0.1:4711` → 放服務那台的 sshd（管理用：手動升級、維護）。`4711` **一律保留**，不支援關閉（`REMOTE_SSH=off` 會被拒，結束碼 `78`）。
- **連線健康時不碰 GitHub**：只有 ssh 連線斷掉、端對端探針連續失敗，或完全沒有可用位址時，才在同一個行程內唯讀抓取 MyLinuxPool 的 `NODE_GATEWAY`（會合點的新位址與主機金鑰），嚴格驗證後寫入本機並立刻重連；抓不到就沿用最後已知位址、以結束碼 `1` 讓 systemd 重開。這是 `connectivity` 規格「不依賴 MyLinuxPool」的**唯一例外**（只唯讀 `NODE_GATEWAY` 的位址欄位），細節與 token 見第 7 節。
- 客戶端（Mac、worker 的 sidecar、手機）**自備通道**：把會合點的 4710 轉發到自己機器的 loopback（本機埠預設 4710），再直接打 `http://127.0.0.1:<本機埠>`。LLMGateway 不提供連線程式（見第 4 節）。
- key 的正本是 private repo `FATESAIKOU/LLMGateway-keys` 的 `keys.env.enc`（加密檔）。放服務那台以 `llmgw` 身分使用 `gh` 拉取、解密、驗證後原子換掉 `keys.env`；`lgwd` 發現檔案變更後自動重讀。使用前提：以 `llmgw` 身分登入過 `gh`。
- 管理指令全面統一為 `llmgwctl`（封裝於 `/usr/local/bin/llmgwctl`，或在當版目錄執行 `python3 -m lgwctl`）。

---

## 1. 部署前提與版面配置

### 1.1 系統前提

- 作業系統：Linux，具備 systemd（system 層管理）。
- 工具需求：`python3`（3.10 以上，僅使用標準函式庫）、`git`、`curl`、`ssh`、`ssh-keygen`、`openssl`，以及金鑰同步所需的 `gh`（GitHub CLI）。
- 獨立服務帳號：`llmgw`（不能加入 `docker` 群組，確保隔離性）。
- `gh` 登入：由本人親自以 `llmgw` 身分登入（`sudo -u llmgw -i` 後執行 `gh auth login`，使用瀏覽器登入，不放 token 檔）。這條只管 `LLMGateway-keys` 金鑰同步，**另有一個受控例外**，見下一條。
- **`NODE_GATEWAY` 讀取用 token 檔（受控例外，#221；可選，只在要讓隧道跟隨 MLP 輪換時才放）**：一把 GitHub fine-grained PAT，只授權 `FATESAIKOU/MyLinuxPool` 的 `Variables: Read-only`（這是最小授權，但**仍可讀該 repo 全部 Actions variables**，含其他 `NODE_*` 的位址拓樸，不含 Secrets）；檔案 `/etc/llmgateway/mlp-gateway.token`（`root:root`、`0600`），以 systemd drop-in `LoadCredential` 交給隧道守護，不給 `llmgw` 直接讀。平穩期（連線一直健康）token 可能悄悄到期，直到下一次輪換才發現，需靠 `TOKEN_EXPIRES_AT` 與人工 `--fetch-check` 降低風險。完整規格、建立與換 token 流程見 7.2。
- 隧道金鑰：維持在 `~llmgw/.ssh/`（私鑰 `~llmgw/.ssh/id_llmgw_tunnel`、known_hosts 檔 `~llmgw/.ssh/known_hosts_rendezvous`）。私鑰絕不離開該台機器。跟隨輪換後，連線改用 `/var/lib/llmgateway/.tunnel_known_hosts`（程式依 `NODE_GATEWAY` 的 `host_key` 產生）；`known_hosts_rendezvous` 只在沒有有效位址檔時當最後已知位址用（P5 後已收起，見 7.6）。

### 1.2 檔案系統版面配置（Layout）

新版架構將程式、設定、狀態與控制檔分層獨立，嚴格限制權限：

| 路徑 | 擁有者與權限 | 用途說明 |
|---|---|---|
| `/opt/llmgateway/<tag>/` | `root:root` 0755 | 各版本程式目錄（從 git tag 抓取） |
| `/opt/llmgateway/current` | `root:root` symlink | 指向目前生效版本的符號連結 |
| `/opt/llmgateway/previous` | `root:root` symlink | 指向上一版版本的符號連結（供 rollback 使用） |
| `/etc/llmgateway/llmgateway.env` | `root:llmgw` 0640 | 服務設定檔（例如 `LGW_API_PORT=8480`） |
| `/etc/llmgateway/tunnel.env` | `root:llmgw` 0640 | 隧道設定檔（`RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS` 等） |
| `/etc/llmgateway/mlp-gateway.token` | `root:root` 0600 | （可選）`NODE_GATEWAY` 讀取用唯讀 PAT，單行（見 7.2） |
| `/etc/systemd/system/llmgateway-tunnel.service.d/credential.conf` | `root:root` 0644 | （可選）drop-in `LoadCredential=`；由 `llmgwctl tunnel-apply --new-dropin` 放（見 7.3），出貨 unit 不含 |
| `/etc/llmgateway/keys.pass` | `root:llmgw` 0640 | 單行解密密碼（`LLMGW_KEYS_PASSWORD=...`，透過 systemd `LoadCredential` 傳遞） |
| `/var/lib/llmgateway/keys.env` | `llmgw:llmgw` 0600 | 解密後的五把金鑰環境變數檔 |
| `/var/lib/llmgateway/llm.db` | `llmgw:llmgw` 0600 | SQLite 資料庫（冷卻紀錄與稽核） |
| `/var/lib/llmgateway/.keysync.sha` | `llmgw:llmgw` 0600 | 上次套用的金鑰 blob sha 記錄 |
| `/var/lib/llmgateway/.keysync.status`| `llmgw:llmgw` 0644 | 金鑰同步狀態檔（時間戳與成敗原因，不含值） |
| `/var/lib/llmgateway/.auth-busy.status` | `llmgw:llmgw` 0644 | 認證防重放庫忙碌（store busy）的計數檔（只有次數與時間，不含請求資料；見 8.22） |
| `/var/lib/llmgateway/gateway.json`、`.tunnel_known_hosts`、`.tunnel_generation`、`.tunnel_fetch_state`、`.tunnel_fetch_backoff`、`.tunnel_token_meta` | `llmgw:llmgw` 0600 | 隧道跟隨狀態（位址檔、私用 known_hosts、已驗證水位、最近抓取分類與退避、token 到期標頭；不含 token，見 7.1） |
| `/var/lib/llmgateway-ctl/` | `root:root` 0700 | 遷移與控制狀態目錄（`.cutover.state`、`.cutover.lock`）；`tunnel-apply` 的 `.tunnel-apply.lock`、`.tunnel-apply.state` 與備份目錄 `tunnel-apply.bak/` |
| `/etc/systemd/system/` | `root:root` 0644 | 系統層 unit：`llmgateway.service`、`llmgateway-tunnel.service` |
| `/usr/local/bin/llmgwctl` | `root:root` 0755 | 管理指令 wrapper 腳本（自動切進 current 執行） |

### 1.3 全新機器安裝（非遷移情境）

在全新建置的機器上安裝 LLMGateway（需要 root 權限）：

```bash
# 1. 確保已建立服務帳號 llmgw（-U 確保建立同名群組）
id -u llmgw >/dev/null 2>&1 || useradd -m -U -s /bin/bash llmgw

# 2. 本人以服務帳號登入 gh
sudo -u llmgw -i
gh auth login        # 選瀏覽器登入
exit

# 3. 準備臨時原始碼並執行第一次安裝（以 release tag）
git clone --depth 1 --branch lgw-vX.Y.Z https://github.com/FATESAIKOU/LLMGateway.git /tmp/lgw-setup
cd /tmp/lgw-setup
python3 -m lgwctl install --tag lgw-vX.Y.Z
rm -rf /tmp/lgw-setup

# 4. 補齊設定檔（依提示填寫 /etc/llmgateway/ 下的設定與 keys.pass）
# 填寫完成後執行檢查：
llmgwctl check
```

---

## 2. 舊版遷移（從 v0.1.x user unit 升級至新版）

若目標機器目前正在以舊版 user unit（`~/.config/systemd/user/` 下的 `llmgateway.service` 等）運作，請遵循標準五部曲遷移流程。

### 2.1 遷移全貌與狀態機

```
[舊版運作中]
     │
     ▼ (1) prepare-migration
[準備完成（新舊並存，舊版未動）]
     │
     ▼ (2) cutover（排程反向保險 timer）
[切換窗口中]
     ├─────────────────────────────────┐
     │ 停損條件成立（探針失敗／check 非 0）│ 保險到期未 confirm（預設 10m）
     ▼ 或手動 rollback-to-user          ▼
[自動回滾至舊版 user unit]       [反向保險到期自動回舊版]
     │
     ▼ (3) confirm
[觀察期（3 天，仍可用 rollback-to-user）]
     │
     ▼ (4) finalize-migration
[遷移完成（封裝歸檔、關閉 linger）]
```

### 2.2 切換前後操作者注意事項

1. **進場備援**：切換前務必確認具備**不經隧道的進場方式**（例如雲端供應商 Web Console、本機終端或區網 SSH）。若隧道為唯一進場通道，一旦遇到雙邊暫時下線的路徑將只能依賴 Console 救援。
2. **切換前系統檢查**：
   - 檢查系統無待重啟任務：`ls /var/run/reboot-required` 不存在。
   - 檢視定時器：`systemctl list-timers`，確認 `unattended-upgrades` 未排程自動重開機。
   - 確認無殘留單元：`systemctl list-units 'lgw-*'` 確認無過去留存的 transient 單元。
   - 確認控制狀態：檢視 `/var/lib/llmgateway-ctl/.cutover.state`，確認目前不處於進行中階段（`cutover-started`、`pending-confirm`）。
   - 暫停同機其他維護排程，並以 `ps -u llmgw` 確認服務帳號行程符合預期。
3. **窗口內切勿重開機**：自舊版 unit 被 disable 到新版 unit 被 enable 之間（數秒至一分鐘），新舊皆未開機自動啟用，且反向保險為 transient timer；此期間重開機將導致新舊服務均無法自啟。
4. **切換中斷與狀態防護**：執行 `cutover` 後狀態即進入 `cutover-started`，若切換過程發生未預期中斷或卡住，此階段受到防護，系統會拒絕重新執行 `prepare-migration` 或再次 `cutover`；操作者必須執行 `llmgwctl rollback-to-user` 進行回滾。回到終態（`reverted`）後，**可以直接再跑一次 `cutover`**（它會把舊狀態歸檔、開新一輪，不必重做 prepare），也可以先重跑 `prepare-migration`。回滾對新版的 unit 只做 `disable`、**不刪檔**（與 `llmgateway.service` 的處理一致），所以再切換時會重新 `enable`。
5. **隧道交接短暫空窗**：因會合點的 4710/4711 埠無法由兩條隧道同時綁定，回滾時在舊服務 list 200 確認正常後，會先停止新隧道讓出該埠（暫不 disable），隨後啟動舊隧道進行連續穩定（≥15～20 秒且 NRestarts 不增）與遠端 `probe.remote` 探針驗證（成功才 disable 新隧道；失敗則立刻重新啟動新隧道保留管理通道）。此交接過程存在數十秒的隧道中斷短暫空窗。

### 2.3 遷移步驟

所有遷移步驟均由管理帳號以 `sudo` 執行。

#### 步驟 1：真機準備（`prepare-migration`）

此步驟會抓取新版本程式、建立 `/opt`、`/etc`、`/var/lib` 版面、安裝新版 system unit（但不 enable、不 start），並自舊版安全複製 key 檔、資料庫一致快照與設定檔：

```bash
# 以特定 tag 執行準備
llmgwctl prepare-migration --tag lgw-vX.Y.Z [--old-home <path>] [--force]
```

- 若尚未安裝 `/usr/local/bin/llmgwctl`，可先 clone 該 tag 到暫存目錄執行 `python3 -m lgwctl prepare-migration --tag lgw-vX.Y.Z`。
- **安全性與隔離**：舊版設定檔與上層目錄元件皆經過 `O_NOFOLLOW`、`stat.S_ISLNK` 與檔案描述子（fd）檢驗，防止符號連結（symlink）提權；舊資料庫透過 SQLite backup API 進行一致快照，舊服務仍能正常寫入。
- **不影響舊版**：此步驟不會修改或停止任何既有的 user unit，舊服務維持對外提供服務。
- **階段守門**：若切換已進入 `cutover-started`、`pending-confirm`、`confirmed` 或 `finalized` 階段，`prepare-migration`（包含 `--force`）將直接拒絕執行，避免以舊資料覆蓋正在運行之新資料庫或金鑰。
- **參數說明**：
  - `--old-home <path>`：指定舊服務帳號的家目錄（預設為服務帳號家目錄）。
  - `--force`：強制覆蓋既有已複製檔案（若已進入進行中階段則拒絕）。

#### 步驟 2：切換窗口（`cutover`）

```bash
llmgwctl cutover --tag lgw-vX.Y.Z [--revert-after 10m] [--no-remote] [--old-home <path>]
```

- **分離執行與連線中斷**：人手執行的 `llmgwctl cutover` 指令**只負責排程 transient unit（`lgw-cutover`）並立即返回**。真正的切換工作在 systemd 背景單元內執行。
  > [!IMPORTANT]
  > 由於切換會停止舊隧道並啟動新隧道，管理者透過舊隧道連線的 SSH 會在數秒至數分鐘內中斷。請勿慌張或手動重開機。請準備第二終端機，約 3～7 分鐘後（走新隧道）重新連線，並執行 `journalctl -u lgw-cutover -n 100` 檢視切換進度。成功的最後標誌為 `ok confirm-needed`。
- **Preflight 預檢條件**（任一不符合則在動舊服務前明確拒絕）：
  1. `/etc/llmgateway/keys.pass` 必須包含非空之密碼。
  2. `/etc/llmgateway/tunnel.env` 設定就緒。
  3. `gh` 認證已登入（`gh auth status` 通過）。
  4. `current` 符號連結指向指定的 tag。
  5. 新版 policy dry-run 預檢通過。
  6. 舊版 4 個 unit 檔案（`llmgateway.service`、`llmgateway-tunnel.service`、`key-sync.service`、`key-sync.timer`）皆存在且為一般檔。
- **反向保險機制**：啟動時立即排定反向保險定時器 `lgw-revert.timer`（預設等待 10 分鐘，最低限制 5 分鐘；由 `--revert-after` 指定）。
- **停舊、重快照與啟新**：
  1. 停止並停用（disable）舊版四個 user unit，逐一透過 `is-active` 確認停妥。
  2. **最新資料同步**：在舊服務停止後、啟動新服務前，再次對舊 `llm.db` 進行一致快照（帶入 prepare 至切換間的最新冷卻與稽核），並同步較新的 `keys.env` 與 `.keysync.sha`。
  3. 啟用並啟動新版兩個 system unit：`llmgateway.service`、`llmgateway-tunnel.service`。
- **停損順序與條件（失敗立即自動回舊版）**：
  1. 本機探針（`/healthz`；模式依能力旗標，見 §5.12）200（最多等 60 秒）。
  2. 隧道 active 探針（最多等 30 秒）。
  3. 經會合點遠端探針（重試 3 次，間隔 10 秒；`--no-remote` 可略過）。
  4. 等待新版第一次 key 拉取結果寫入（最多等 90 秒）。
  5. 全量健康檢查 `check` 結束碼須為 0。
  若上述任一項未達標，或在保險時限內未執行 `confirm`，系統將自動觸發停損回滾，停止新服務並拉回舊版 user unit。

#### 步驟 3：確認切換（`confirm`）

在切換完成、重新連線並由管理者確認新服務運作無誤後，必須在保險時限內執行確認：

```bash
llmgwctl confirm
```

- 此命令會取消 `lgw-revert.timer` 反向保險排程。
- 將遷移狀態切換為 `confirmed`，正式進入為期 **3 天的觀察期**。

#### 步驟 4：觀察期應急回滾（`rollback-to-user`）

若在 3 天觀察期內發現不可預期的問題，可手動退回舊版 user unit：

```bash
llmgwctl rollback-to-user [--old-home <path>]
```

- 同樣以 transient unit（`lgw-rollback`）分離執行，避免操作者連線中斷影響回滾。
- 順序：停止新服務讓出 8480 埠 → 還原資料庫（舊 db 先備份為 `.bak-*`）與 `keys.env`（若新版有較新修改）→ 啟用舊版 user unit 並啟動舊版服務與 `key-sync.timer` → 驗證本機 list 200 → 記錄舊隧道重啟次數（`NRestarts`）→ **停止新隧道（暫不 disable，讓出會合點 4710/4711 埠；此時有數十秒之短暫交接空窗）** → 啟動舊隧道 → 等待舊隧道連續穩定 active（≥15～20 秒且 `NRestarts` 不增）並以 `probe.remote` 經會合點驗證回 200 → 成功才 disable 新隧道。若舊隧道起不來或探針失敗，程式會立即重新啟動（`start`）新隧道以保留管理連線通道，並清楚報錯供排查。

#### 步驟 5：遷移收尾（`finalize-migration`）

在觀察期滿 3 天、確認系統完全穩定後，執行最後收尾：

```bash
llmgwctl finalize-migration --yes [--old-home <path>]
```

- 自動執行全量健康檢查 `check`。
- 將舊版 user unit 檔案與目錄搬移歸檔至 `/var/lib/llmgateway/old-user-layout/`（安全封裝、不刪除）。
- 執行 `loginctl disable-linger llmgw`，停用服務帳號常駐，釋放 user manager 資源。
- 標記遷移終態為 `finalized`，完成全部遷移工作。

---

## 3. 本人在 GitHub 的設定

### 3.1 Environment 與 Secret 備份

在 GitHub repo 的 Settings → Environments 建立 `llmgateway-deploy`，並設定 Required reviewers 為本人。

| Secret 名稱 | 內容 | 用途 |
|---|---|---|
| `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT` | 32 字元隨機英數密碼 | 解密密碼之離線備份，遺失時供本人重建 |

*註：五把金鑰明文與部署連線用的公私鑰 secret 已全數廢除，不儲存於 GitHub Actions。*

### 3.2 Private repo `FATESAIKOU/LLMGateway-keys`

金鑰正本存放於 private repo 根目錄的 `keys.env.enc`：
- 加密指令：`openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt`。
- 明文格式為五行 `KEY=VALUE`。機器僅依 `gh api` 取得之 blob `sha` 比對版本。

---

## 4. 客戶端設定（自備通道）

LLMGateway 僅規範通訊協定，不提供額外的客戶端連線程式。客戶端環境（Mac、worker sidecar、手機）需自行將會合點的 4710 埠轉發至本機 loopback：

### 4.1 Mac 客戶端

```bash
ssh -N -i ~/.ssh/<能登入 sshproxy 的金鑰> -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=~/.ssh/known_hosts_rendezvous \
  -p <埠> -L 127.0.0.1:4710:127.0.0.1:4710 sshproxy@<跳板機>
```

測試連線（#246 起服務有 `/healthz`：只證明行程活著、能回應，不讀 DB）：
```bash
curl -s http://127.0.0.1:4710/healthz          # {"status": "ok"}
curl -s http://127.0.0.1:4710/v1/subscriptions  # 業務資料（不是探針）；無簽只在 off／warn 可用，enforce 之後要改用 §4.2 的 lgw（帶 signer）
```

> [!NOTE]
> **Mac 轉發指引現況（Q-MLP-E）**：MLP 目前**尚未**提供基於 `NODE_GATEWAY` 的 Mac 轉發指令，所以這裡仍是上面的手動寫法。隧道跟隨輪換只更新**服務端**；輪換後會合點位址與主機金鑰變了，Mac 這條通道的 `<跳板機>`／`<埠>` 與 `known_hosts_rendezvous` 要自己更新（或沿用 MLP 的 worker 通道）。MLP 提供指令後再改寫這一節。worker 的 sidecar 維持綁會合點的 `127.0.0.1:4710`，不用改。

### 4.2 Python 客戶端程式庫（`lgw.client`）

```python
from lgw import client

c = client.Client("http://127.0.0.1:4710")
c.list()                                              # 取得可用訂閱（不含 key）
c.cooldown("claude-personal", "2026-10-18T15:00:00Z") # 登錄冷卻（整把）
c.cooldown("opencode-zen", "2026-10-18T15:00:00Z",    # 只冷卻一個模型
           model="mimo-v2.6-flash-free")
```

`lgw` 0.2.0（#222 D4a）起可以簽章（服務端 warn／enforce 時需要）：

```python
from lgw import client, signer

s = signer.Signer(key_id="<名冊的 key ID>",
                  private_key_path="<OpenSSH 私鑰檔>")   # 私鑰檔
# 或：公鑰檔＋ssh-agent
# s = signer.Signer(key_id="<名冊的 key ID>", public_key_path="<公鑰檔>",
#                   agent_sock=os.environ["SSH_AUTH_SOCK"])

c = client.Client("http://127.0.0.1:4710", signer=s)
c.list()
```

- 簽的是**實際送出**的 request-target（含 query，**也含 base_url 的路徑前綴**）與
  **body bytes**；五個 `X-LGW-*` 標頭一次簽好，GET 也簽（空 body 的 SHA-256）。
- `signer=None` 時行為與 0.1.0 相同（不帶認證標頭）；只多了**不跟隨 redirect**
  （3xx 原樣回 `error="redirect"`），所有呼叫都適用，這是 0.2.0 的行為變更。
- 簽章失敗（找不到 `ssh-keygen`、逾時、私鑰有密語、簽章長度不是 240 字元）回
  `Result(ok=False, error="signer_unavailable")`，**不送出**、也不改送無簽版本。
- 認證錯誤：401 `unauthorized`（`data` 另附 `server_date` 與 `clock_skew_s`，只提示
  可能時鐘偏差，不自動調時）、403 `forbidden`、429 `rate_limited`
  （`data["retry_after"]` 是整數秒）、503 `auth_unavailable`。client **不自動重試**；
  要重試就整個重新呼叫（新的 nonce）。
- 另一個 0.2.0 的行為變更（#266 架構審 低-1）：`signer=None` 時，401／403／429／503
  且回覆不是 JSON 物件或沒有 `error` 鍵，`error` 現在是上列的固定字串（0.1.0 是
  `bad_response` 或狀態碼字串），`data` 另含 `server_date`／`clock_skew_s`／
  `retry_after`。狀態碼本身不變，呼叫方若只比對 `ok`／`status` 不受影響。
- 私鑰只由本機 `ssh-keygen -Y sign -n llmgateway` 使用；`lgw` 不新增 Python 相依。

---

## 5. 日常維運與管理指令

日常維運一律使用 `/usr/local/bin/llmgwctl`：

### 5.1 檢查服務狀態（`check`）

```bash
llmgwctl check [--no-remote] [--tag <tag>]
```

輸出以三態呈現（`ok`、`todo`、`fail`），結束碼定義：
- `0`：全部正常通過。
- `1`：確定失敗（unit 未運行、探針非 200、gh 認證失效等）。
- `2`：存在需人工介入處理之 `todo`。
參數 `--no-remote` 可略過跨會合點之遠端探針（隧道相關項目一併不列）。

`list-local`／`list-remote` 的探針模式由 `current` 的能力旗標決定（#246，見
§5.12）：`new` 只探 `GET /healthz`；`old` 先探 `/healthz`、404 才退回
`GET /v1/subscriptions`。旗標損毀／讀不到時照樣以 `old` 探，但 `list-local`
至少是 `fail`／`todo`，detail 會寫「能力旗標損毀／讀不到（以 old 探：…）」。
`/healthz` 只證明行程活著、能回應（lgwd 已過啟動時的 policy／keys／DB 初始化），
**不代表 DB 此刻可寫**——這一項比以前的 list 探針弱，是 #246 接受下來的取捨；
DB 與 key 的健康仍看 `keysync`／`policy` 與業務回應。

**隧道是可選的**：機器上沒有 `llmgateway-tunnel.service` 也沒設定隧道時，`check` 不列任何隧道項目，其餘項目全通過即回 `0`。只要隧道 unit 存在或已設定隧道，`check`／`status` 就照常檢查（除非 `--no-remote`）。

隧道有設定位址追蹤（`FOLLOW_FILE` 或任何 `GATEWAY_*` 鍵）時，`check` 另列：

| 項目 | 內容與判法 |
|---|---|
| `tunnel-follow` | **follow 六態**：`unconfigured`（沒設追蹤，不列）、`ok`（顯示 `ok (gen=<n>)`，`<n>` 是已驗證水位，尚未驗通顯示 `pending`）、`missing`（位址檔不在）、`invalid`（位址檔壞掉、是 symlink／過大，或同代但欄位與已驗證指紋不符）、`regressed`（位址檔 `generation` 低於已驗證水位）、`unreadable`（非 root 讀不到 `0700` 的狀態目錄，顯示「需 root 或 llmgw 才能讀」，**不會誤報 `missing`**） |
| `tunnel-fetch` | **最近抓取分類**：`ok`、`regressed`、`invalid`、`hostkey_missing`、`auth_failed`、`rate_limited`、`unreachable`、`config_partial`、`credential_unreadable`（沒抓過顯示 `none`） |
| `tunnel-token` | token 到期：`ok`、`expiry_unknown`（沒有可用日期，不告警）、`expiry_due`（剩 ≤ `TOKEN_EXPIRY_WARN_DAYS`，預設 7 天，或已到期）、`expiry_invalid`（`TOKEN_EXPIRES_AT` 格式不合，或到期資訊無效）、`unreadable` |
| `tunnel-credential` | `GATEWAY_GITHUB_KEY_FILE` 讀不到或沒權限時 `todo`（提示檢查路徑、權限或 drop-in `LoadCredential`） |

等級：隧道**未 `active`** 時，`tunnel-follow` 為 `missing`／`invalid`／`regressed`，或抓取分類為 `regressed`／`invalid`／`hostkey_missing`／`auth_failed`，一律 `fail`（結束碼 `1`），訊息含處置（確認為上游回退時 `llmgwctl follow-reset`；`auth_failed` 換好 token 後 `llmgwctl follow-reset --fetch-state`）。隧道**仍 `active`** 時同樣的情況降為 `todo`（現存連線靠最後已知位址維持，需人工確認 `NODE_GATEWAY`；`auth_failed` 提示跑 `llmgateway-tunnel --fetch-check`），不會靜默全綠。`config_partial`、`credential_unreadable`、`expiry_due`、`expiry_invalid`（`TOKEN_EXPIRES_AT 格式不合`）、`unreadable` 皆為 `todo`（結束碼 `2`）。輸出只含這些固定字串，不含位址、金鑰或 token。

### 5.2 檢視版本與運行狀態（`status`）

本指令不需要 root 權限：

```bash
llmgwctl status
```

印出目前 `current` 與 `previous` 的版本 tag，以及兩個 system unit（`llmgateway.service`、`llmgateway-tunnel.service`）的 active 狀態。
- 隧道存在或已設定時另有幾列：`llmgateway-tunnel.service`（active 狀態）、`tunnel-follow`（follow 六態，正常為 `ok (gen=<n>)`）、`tunnel-fetch`（最近抓取分類）、`tunnel-token`（到期狀態）、`list-remote`（經會合點探針：`ok`／`fail`／`todo`）。沒有設定追蹤（也沒有抓取紀錄）時不列 `tunnel-follow`／`tunnel-fetch`／`tunnel-token` 三列；`--no-remote` 不列任何隧道相關列。非 root 讀不到狀態目錄時顯示 `unreadable（需 root 或 llmgw 才能讀）`。
- 若需檢視遷移階段狀態，可查看 `/var/lib/llmgateway-ctl/.cutover.state` 或 transient unit 紀錄（`journalctl -u lgw-cutover` / `lgw-rollback`）。
- 若需檢視詳細金鑰同步狀態，請查看 `/var/lib/llmgateway/.keysync.status` 或執行 `llmgwctl check`。

### 5.3 版本升級（`upgrade`）

```bash
llmgwctl upgrade --tag lgw-vX.Y.Z [--repo <repo>]
```

1. 自動自 GitHub 抓取新版本 tag 至 `/opt/llmgateway/<tag>/`。
2. 自動對新版之政策檔執行 dry-run 預檢。
3. 備份目前 unit 設定，原子切換 `/opt/llmgateway/current` 連結。
4. 安裝新 unit 檔並執行 `systemctl daemon-reload`。
5. 重啟 `llmgateway.service`。
6. **失敗自動回滾**：升級過程中任何步驟失敗，立即自動回復至上一版本（`previous`）。

> [!IMPORTANT]
> **跨 repo 的升級順序：MyLinuxPool 的 sidecar 先上，Gateway 後上（或同一個維護窗一起上）。**
> 舊 sidecar ＋ 新 Gateway 的代價：開啟時多一次啟動與一次 429；同一個 provider 有多把且都在冷卻時會來回換（每輪一次啟動加一次 429）。單一把 provider 的換把結果不變（`exclude` 之後沒有候選 → `409 no_candidate`）。新 sidecar ＋ 舊 Gateway 只是退回現行行為（舊 Gateway 仍會回 `409 cooling`），不會更糟。

### 5.4 手動版本回滾（`rollback`）

```bash
llmgwctl rollback
```

切回 `/opt/llmgateway/previous` 指向的版本，還原 unit 檔案、重載 daemon 並重啟服務，最後自動執行 `check` 驗證。

### 5.5 手動觸發金鑰同步（`sync`）

```bash
llmgwctl sync
```

讀取 `/etc/llmgateway/keys.pass`，以 `llmgw` 身分執行金鑰拉取、解密並更新 `/var/lib/llmgateway/keys.env`，記錄狀態至 `.keysync.status`。

> [!NOTE]
> 若更新了 `/etc/llmgateway/keys.pass` 的密碼內容，必須執行 `sudo systemctl restart llmgateway`。因為 systemd 的 `LoadCredential` 是在服務啟動時建立憑證快照（snapshot），單純 `llmgwctl sync` 雖能以新密碼解密新檔，但運行中的主服務若需重新載入憑證仍需重啟。

### 5.6 更新金鑰（上游改動）

由本人在本地端處理（AI 不碰明文值）：

```bash
export LLMGW_KEYS_PASSWORD='<解密密碼>'
openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt \
  -pass env:LLMGW_KEYS_PASSWORD -in keys.env -out keys.env.enc
unset LLMGW_KEYS_PASSWORD
shred -u keys.env
git add keys.env.enc && git commit -m 'update keys' && git push
```

推送後，放服務的機器會在排程（或手動執行 `llmgwctl sync`）時自動同步。

---

### 5.7 用量（`usage`）與政策檔的 `usage` 段

list 的每一筆多 `usage` 與 `available_again_at`，resolve 的 `meta` 多同形狀的 `usage`。`usage` 是附加資訊：取用量壞了、卡住，list／resolve 照常回應，該筆只是 `unknown`。

- **來源**：Ollama Cloud 取官方 `GET /api/balance`（用 vault 的 key，只在 lgwd 行程內；`source` 為 `ollama-api-balance`）；Claude 訂閱執行原版 Claude Code 讀 statusline 的 `rate_limits`（`claude-statusline`）；OpenRouter 用部署的 key 取額度（`openrouter-api-key`，預設開啟）。OpenCode Zen、OpenCode Go、Antigravity、ChatGPT 個人（`chatgpt-personal`）沒有取用量實作，為 `not_applicable`，`source` 為 null。
- **`reason`**（`status` 為 `unknown` 時必有；`not_applicable` 時必有 `no_quota_api` 或 `unavailable`；`ok` 時為 null，但 `stale: true` 的 `ok` 沿用最近一次失敗的碼）：

  | `reason` | 意思 |
  |---|---|
  | `not_fetched_yet` | 還沒成功取過（重啟後第一次查） |
  | `disabled` | 政策 `usage` 段關閉 |
  | `no_key` | 這把 key 還沒部署 |
  | `upstream_error` | 上游回非 200 或連線失敗 |
  | `rate_limited` | 上游回 429（照 `Retry-After` 退避） |
  | `timeout` | 取用量逾時 |
  | `parse_error` | 上游回應不是預期結構 |
  | `no_windows` | 回應裡沒有可讀的窗口 |
  | `stale_expired` | 最後一次成功超過 6 小時 |
  | `internal` | 取用量路徑內部例外（只記型別名） |
  | `no_quota_api` | `not_applicable`：研究文件或官方文件明載上游沒有額度資訊來源。出廠政策目前沒有訂閱符合，碼保留給日後有實證者 |
  | `unavailable` | `not_applicable`：上游有額度資訊，但 Gateway 取不到（例如只在帳戶 session 或呼叫方本機登入的介面上）。出廠的 `opencode-zen`、`opencode-go`、`antigravity`、`chatgpt-personal` 都是這一類 |

  **注意**：`not_applicable` 的 `reason` 由舊版的 `null` 變為上表兩個碼之一；讀取方若以「`reason` 為 null」判斷，要改。

- **`note`**（第九個鍵）：只在 `reason` 為 `no_quota_api` 或 `unavailable` 時是一句固定說明，說明為何拿不到；其餘情況為 null。說明來自程式內的固定表（依 subscription id 查），不取上游回應，不含 key、token、帳號或金額。政策檔有、但固定表沒收錄的 `not_applicable` 訂閱，`reason` 為 `unavailable`、`note` 為通用說明，並在 log 記一條 warning（同一個 id 只記一次），lgwd 照常啟動。
- **語意（重要）**：只在有人 list／resolve、而且快取過期時才在**背景**取一次，回應不等它。零星查詢時，多半拿到的是**上一次查詢觸發的快照**（`stale: true`；超過 6 小時轉 `unknown`／`stale_expired`）；重啟後第一次查一律 `unknown`／`not_fetched_yet`，要看新值得等取完（Ollama 約 5 秒內、Claude 最多約 60 秒）再查一次。
- **TTL**：Ollama 預設 120 秒（下限 60 秒）；Claude 預設與下限都是 30 分鐘（每次取耗少量訂閱額度，低於 30 分鐘會被夾到 30 分鐘並記 log）。失敗後依 TTL 加倍退避（上限 max(30 分鐘, TTL)），429 照 `Retry-After`。
- **政策檔**（選填，頂層 `usage`；鍵是 subscription id，不合法 lgwd 不啟動，改檔後先經 `llmgwctl` 的 policy 預檢）：

  ```json
  "usage": {"ollama-cloud": {"enabled": true, "ttl_seconds": 300},
            "claude-personal": {"enabled": false}}
  ```

  只認 `enabled`（布林）與 `ttl_seconds`（正整數），其他鍵一律拒收。**程式**在政策沒有設定時預設關閉（`enabled: false`）；**出廠政策檔**（`policy/default.json`）自 #60 階段 C 起開啟 Claude（經本人授權）；部署前需完成 10.1／10.2 與 C1（階段 C 的前置，即以服務帳號裝原版 Claude Code、trust 固定目錄、服務的 PATH 與 `LGW_USAGE_CLAUDE_CWD` 或 `--usage-claude-cwd`，見 `openspec/changes/archive/2026-10-26-usage-in-responses/tasks.md` 第 10 節），未完成時 `usage` 會是 `unknown`／`upstream_error`。
- **`available_again_at`**：`cooldown.until`（未來）與「用完（`remaining_percent` ≤ 0）的窗口之 `resets_at`」中最晚者；`available` 不因用量為 0 而變 false，選把規則也不變。
- **`GET /v1/usage`（用量一覽）**：唯讀，只接受 GET（`POST`、`PUT`、`DELETE`、`PATCH` 回 404；其他未實作的方法如 `HEAD`、`OPTIONS` 由 Python 標準函式庫回 501）；回 `{"schema": 1, "subscriptions": [...]}`，順序與 list 相同（依 `priority`）。每筆恰十二鍵：`id`、`kind`，加上 list 的 `usage` 八個共用欄位（`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`，同一快取狀態下與 list 逐值相等），再加 `weekly_used_usd`、`weekly_used_usd_fetched_at`。沒有 `pipes`。存取控制與 list 相同：`off` 時沒有應用層認證，只靠 SSH 與本機綁定；`warn`／`enforce` 見第 8 節。用途是週報或 AI 取一覽，不必抓整份 list。
- **`weekly_used_usd`／`weekly_used_usd_fetched_at`**：只有 OpenRouter 可以是非 null。`weekly_used_usd` 是上游 `usage_weekly` 的原值，單位 USD，不換算、不取整，不是餘額或配額。`weekly_used_usd_fetched_at` 是該金額的取得時刻（RFC 3339 UTC），與窗口的 `fetched_at` 各自獨立；金額可能在窗口失敗時仍有值，應以金額自己的時間判讀。`0` 是真的 0，表示上游回報本週用量為 0；`null` 是拿不到，**不是金額**，不得當 0 用。拿不到的原因包括：欄位缺、值無效、上游失敗、未取過、停用或未部署、超過 6 小時、跨 UTC 週一 00:00 之後（金額屬上一週）、取用量內部例外。兩鍵同時為 null。其他訂閱、list、resolve、pipes、note 仍不含金額。
- **讀法（消費者）**：查詢不等背景請求，第一次可能是 null。先呼叫一次 `GET /v1/usage` 觸發取值；間隔 ≥ 6 秒（上游逾時為 5 秒）再讀；仍為 null 再間隔 ≥ 6 秒讀第二次；第二次也是 null 就記「金額未取得」（附窗口的 `status` 與 `reason`），不以 0 代替，也不再等待。
- **相容**：兩鍵是新增欄位，舊消費端忽略即可。但嚴格檢查舊的十鍵一覽的外部讀者會壞；週報讀取端尚未實作，部署前須確認它容忍未知鍵。
- 退版（`install.sh --tag` 回前一版）安全：欄位是新增的，舊消費端忽略即可。

### 5.8 政策檔的 `models.allow`（fnmatch 樣式）

`policy/default.json` 裡每個 subscription 的 `models.allow` 是一組 **fnmatch 樣式**（`*`、`?`、`[...]`）。比對用的是 `fnmatch.fnmatchcase`：載入政策時，`models.default` 的涵蓋檢查在 `lgwd/policy.py` 約第 98 行的 inline fnmatch；resolve 時 model 是否被 allow 涵蓋的比對在 `lgwd/registry.py` 的 `_covered`。有三件事要特別注意：

- **fnmatch 的 `*` 會匹配任何字元，包含 `/`**。所以 `*-free` 這種樣式不只匹配 `beam-free`，也會匹配 `opencode/anything-free` 之類帶斜線的 id。
- **2026-10-27 #95 起 opencode-go 只允許 beam-free（iceman-contributor-free 以授權訓練 prompt 換免費，不進輪替）；opencode-zen 的 `*-free` 不變**：出廠政策檔裡，`opencode-zen` 的 `models.allow` 仍是 `["*-free","big-pickle"]`，`opencode-go` 改為 `["beam-free"]`。Go 不再自動放行上游新增的 `*-free` 模型；zen 維持原有萬用字元行為。
- **2026-11-01 #149 起 opencode-go 再加上 big-pickle（明列）**：`opencode-go` 的 `models.allow` 改為 `["beam-free","big-pickle"]`，default 維持 `beam-free`；iceman-contributor-free 仍不涵蓋。
- **想讓兩把分開控制，就得寫死清單**：把 `*-free` 換成每把各自的一份明列清單，例如 zen 的 `models.allow` 用 `["mimo-v2.6-flash-free", "glm-5.3-flash-free", "big-pickle"]`、go 的用 `["beam-free","big-pickle"]`，兩把才不會互相牽動。allow 是每把各自判斷，同一個 id 放在兩份清單裡，代表兩把都可以用它；要分開控制的是「萬用字元會把上游新增的模型一起放進來」這件事。改完 `models.allow` 之後，`models.default` 必須仍被涵蓋（見上方載入時驗證）。政策檔是 JSON，不能加註解，所以這個副作用與「要分開控制就寫死清單」的做法記在這裡。

`models.default`（非 null）載入時必須被同一筆的 `models.allow` 樣式涵蓋，否則 lgwd 不啟動（fail-closed）。

### 5.9 沒有憑證的訂閱（`antigravity`）的政策範例與限度

`antigravity` 是 agy（Antigravity CLI）用的訂閱：**沒有憑證**，agy 自己在 Mac 上用本機登入，Gateway 不代管、不匯出 OAuth 憑證。它的 `kind` 是 `local-login`、`endpoint` 為 `null`，`agents` 只能剛好是 `["agy"]`；模型只走 resolve 的 `meta.model`（裸 id，不能帶 `agy/` 或 `google/` 前綴）。

政策檔的一筆範例如下（只有 id，不含任何憑證）：

```json
"antigravity": {
  "priority": 35,
  "privacy": "public",
  "agents": ["agy"],
  "models": {
    "allow": [
      "gemini-3.8-flash-low",
      "gemini-3.8-flash-medium",
      "gemini-3.8-flash-high",
      "gemini-4-argon-low",
      "gemini-4-argon-medium",
      "gemini-4-argon-high",
      "gemini-4-hydrogen-low",
      "gemini-4-hydrogen-medium",
      "gemini-4-hydrogen-high",
      "gemini-4-hydrogen-xhigh",
      "gemini-4-neon-low",
      "claude-opus-5-5-low",
      "claude-opus-5-5-medium",
      "claude-opus-5-5-high",
      "claude-sonnet-5-5-low",
      "claude-sonnet-5-5-medium",
      "claude-sonnet-5-5-high"
    ],
    "default": "gemini-3.8-flash-low"
  }
}
```

`models.allow` 寫的是**字面 id**（不含萬用字元）：清單外的 id（含帶前綴的）在 resolve 就是 403 `model_not_allowed`，錯誤不會延到 agy 才爆。`models.default` 是 `model` 省略時的推導值，必須被同一筆的 `allow` 涵蓋。

> [!IMPORTANT]
> **限度（advisory，不是存取控制）：這把只在 Mac 能用，但 Gateway 擋不住誤用。**
> 出廠政策是所有實例共用的一份（同一份 `policy/default.json`），所以 worker 實例的 `GET /v1/subscriptions` 也會對 `antigravity` 顯示 `available: true`、`usage.status` 為 `not_applicable`——那是「沒有憑證但恆算已部署」，不是「這台可以用」。worker 誤用時會拿到 200 與空的 `env`，直到 agy 在該台找不到而失敗（503）才會發現。Gateway 的 `off` 模式沒有呼叫方／主機概念，也不驗應用層認證（只有 loopback＋SSH 轉發；`warn`／`enforce` 見第 8 節），因此「只給 Mac」只能靠部署與自律（worker 的 sidecar 不要求 agy），不能寫成擋得住。


### 5.10 啟動、停止與重啟服務（`start`／`stop`／`restart`）

```bash
sudo llmgwctl start
sudo llmgwctl stop
sudo llmgwctl restart
```

只管 `llmgateway.service`（不碰隧道），以 root／`sudo` 執行（背後是 `systemctl`）。`start` 與 `restart` 之後會等本機探針（`GET /healthz`；模式依能力旗標，見 §5.12）回 200 才回 `0`；冪等：已 active 且本機探針 200 時再 `start` 不重啟、回 `0`；已停止時再 `stop` 回 `0`（`stop` 不探針）。其他情況（起不來、等不到 200）回 `1`。

### 5.11 重設隧道跟隨狀態（`follow-reset`）

```bash
sudo llmgwctl follow-reset                # 清 .tunnel_generation、.tunnel_fetch_backoff、.tunnel_fetch_state
sudo llmgwctl follow-reset --fetch-state  # 只清 .tunnel_fetch_state 與 .tunnel_fetch_backoff，保留已驗證水位
```

- **需 root**：非 root 印「`follow-reset 需要 root 權限`」並回 `1`。
- 預設模式會連已驗證水位（`.tunnel_generation`）一起清掉：只在**人工確認 `NODE_GATEWAY` 的回退是合法**（`tunnel-follow` 為 `regressed` 而非被竄改）時用，用完通常要 `sudo systemctl restart llmgateway-tunnel`。
- `--fetch-state` 用在換好 token、清掉 `auth_failed` 告警（或 `--fetch-check` 成功後 `check` 仍紅）：**不動**世代水位與位址檔 `FOLLOW_FILE`。
- 兩種模式都不動 `FOLLOW_FILE`、不重啟任何 unit；狀態目錄不存在時回 `0`。

### 5.12 能力旗標（`deploy/capabilities.json`）

探針要打哪個路徑**不是猜的，也不是由部署器決定**，而是讀版本目錄裡的追蹤檔
`/opt/llmgateway/current/deploy/capabilities.json`（#246）：

```json
{"schema": 1, "probe": "healthz"}
```

- 這個檔是 **tag 內容的一部分**：`current`／`previous` 由 symlink＋`os.replace`
  原子切換，所以旗標跟著版本一起換，不需要任何新的寫入步驟；`install`／
  `upgrade`／`rollback`／`cutover`／`deploy/install.sh` 都**不寫、不改**它（只切
  `current`）。開發者要把探針改成 `/healthz` 時，在同一個 PR 提交這個檔。
- 解析只有四種結果：`ok`（格式正確）、`missing`（檔案不在，**舊 tag 的正常狀態**）、
  `unreadable`（權限等讀不到）、`invalid`（其他任何形狀：多餘的鍵、`schema` 不是
  整數 1、未知的 `probe`、重複鍵、超過 4 KiB、最後一段是 symlink、是目錄…）。
- 模式：**只有 `ok` 才是 `new`，其他三態一律 `old`**。
  - `new`：只探 `GET /healthz`；非 200（含 404）就是失敗，**不退回 list**。
  - `old`：先探 `GET /healthz`，**只有**拿到 404 才退回 `GET /v1/subscriptions`
    （200 才算健康）。old 對新舊服務都能正確判活，所以異常一律降為 old——
    寧可多打一個 404，也不要讓隧道因為誤判而反覆重建（4710 反覆中斷）。
- 誰讀：
  - `llmgwctl` 的 `check`／`status`／`start`／`restart`／`install`／`upgrade`／
    `rollback`／`tunnel-apply` 與遷移指令：探「新服務」時讀 `current`（install／
    upgrade 是讀**要切過去的那個 tag**），而 `rollback-to-user` 探的是 **user 層的
    舊服務**，一律寫死 `old`。
  - `deploy/install.sh` 的 `--check`：**一律 old**（user 層是遷移前的舊佈局，
    不會跑新的認證模式）。
  - 隧道守護：每次探針之前重讀一次這個檔（system unit 以 `LGW_CAPABILITIES_FILE`
    指向 `current` 的旗標；user 層沒設，一律 old）。升級後隧道尚未重啟時，仍跑
    舊腳本、探 list（效果等同 old）；要等隧道重啟才換成新腳本（見 R-4）。
- **旗標有問題時的反應**：`check` 的 `list-local` 會以 old 探，但等級至少
  `fail`（`invalid`）或 `todo`（`unreadable`），detail 寫「能力旗標損毀／讀不到
  （以 old 探：…）」；`upgrade`／`install` 在**切 `current` 之前**就 `fail
  capabilities` 停手（不切版、不動 unit）；`rollback` 是救急，旗標損毀**照樣
  回滾**，之後 `check` 會照實報 fail。**不要手動改這個檔來「修好」探針**——
  它跟著版本走，手改會在下一次切版時被換掉。

> [!IMPORTANT]
> **停損 SOP（升級中斷的空窗）**：`upgrade` 切完 `current`、還沒重啟服務的那幾
> 秒，`current` 已是新版、跑的還是舊服務。這段空窗本來就有，但 #246 之後它也會
> 影響隧道（新版旗標是 `new`，舊服務沒有 `/healthz`）。空窗只有幾秒，遠低於
> 隧道的連敗門檻（`PROBE_FAILS`＝3 × 60 秒），正常不會重建。
> 若看到 `llmgwctl check` 的 `list-local fail（healthz 回 404）`，或
> `journalctl -u llmgateway-tunnel` 一直出現 `probe_fail`／`restart`：
> **立刻 `sudo llmgwctl restart`（讓服務跟上 `current`）或 `sudo llmgwctl
> rollback`**；不要手動改 `deploy/capabilities.json`，也不要手動 restart 隧道
> 去繞過。

## 6. 故障排除

### 6.1 不需要設定 `XDG_RUNTIME_DIR` 或 `DBUS`

在新版架構中，所有服務已全面遷移至 systemd 系統層管理：
- **切勿**也不需要再執行 `export XDG_RUNTIME_DIR=/run/user/...` 或 `DBUS_SESSION_BUS_ADDRESS=...`。
- 所有管理指令均直接透過標準系統界面溝通，無需依賴任何 user manager bus。

### 6.2 服務紀錄查看

```bash
# 查看 LLMGateway 服務紀錄
journalctl -u llmgateway -n 50 -f

# 查看隧道守護紀錄
journalctl -u llmgateway-tunnel -n 50 -f

# 查看手動同步狀態
cat /var/lib/llmgateway/.keysync.status
```

### 6.3 金鑰同步未更新

- 檢視 `llmgwctl check` 輸出中之金鑰檢查項目。
- 以 `llmgw` 身分檢視 `gh auth status`，確認 GitHub CLI 登入憑證是否過期。
- 檢查 `/etc/llmgateway/keys.pass` 權限是否為 `0640` 且擁有者為 `root:llmgw`。
- **密碼變更需重啟**：若變更了 `keys.pass`，記得執行 `systemctl restart llmgateway`，以使 systemd 的 `LoadCredential` 快照重新載入。
- 執行 `llmgwctl sync` 觀察是否有 `failed=decrypt_failed` 或 `failed=gh_auth`。

### 6.4 隧道持續重開或斷線

先看 `sudo llmgwctl check` 與 `llmgwctl status` 的 `tunnel-follow`／`tunnel-fetch`／`tunnel-token` 列（第 5.1 節），再看 `journalctl -u llmgateway-tunnel -n 100`（只有固定事件名，見 7.1），依下表處理，詳細處置見 7.5：

| 現象 | 可能原因與處置 |
|---|---|
| 結束碼 `78`、journal 有 `config_missing`，unit 停在 failed 不再重開 | 設定缺（`IDENTITY`、本機使用者、靜態位址與 GitHub 來源兩者皆無且沒有有效 `FOLLOW_FILE`），或 `REMOTE_SSH=off`／其他無效值、未授權的 API base。修好 `/etc/llmgateway/tunnel.env` 後手動 `sudo systemctl restart llmgateway-tunnel`（`RestartPreventExitStatus=78` 不會自己重開） |
| 反覆 `probe_fail`→`restart`→`fetch_*` | 連線斷了正在找新位址；看 `fetch_ok`／`gateway_updated` 有沒有出現。抓不到時按 `tunnel-fetch` 分類處理 |
| `fetch_auth_failed` | token 失效、被撤銷或權限不對：`--fetch-check`，換 token（7.2） |
| `fetch_rate_limited`／`fetch_skipped` | 限流或退避期間，守護自己會重試（`60s`～`900s`），不用處理 |
| `credential_unreadable` | token 檔缺或沒權限，或 drop-in 沒生效（`daemon-reload` 後 `systemctl show llmgateway-tunnel -p LoadCredential`） |
| `follow_regressed`／`follow_invalid`／`hostkey_missing` | 位址檔或 `NODE_GATEWAY` 被回退、同代改欄位或 `host_key` 有問題：人工確認 `NODE_GATEWAY`，確認回退後 `sudo llmgwctl follow-reset`（7.5） |
| `gateway_updated` 後反覆 `tunnel_up` 又斷 | 先查 `NODE_GATEWAY` 的 `host_key` 是否仍是上一代金鑰（7.5） |
| `check` 的 `list-remote` 是 `todo`（`無法探`） | 探針回 `2`：設定不能用、找不到 `ssh`，或位址檔有效但私用 `.tunnel_known_hosts` 缺或不符（探針只讀，不會重寫；起守護程式一次就會重寫）。不是會合點壞掉 |
| `check` 的 `list-remote` 是 `fail` | 經會合點不是 200：看隧道是否 active、本機 `127.0.0.1:<API 埠>` 是否 200 |
| journal 出現 `capability_invalid` | `LGW_CAPABILITIES_FILE` 指的旗標檔損毀或讀不到：守護以 `old` 探、不會因此重建，但 `check` 會報 `list-local` fail／todo。不要手改 `deploy/capabilities.json`；確認 `current` 指到正確版本，必要時 `sudo llmgwctl upgrade`／`rollback` 重切（見 §5.12） |

仍須檢查的老項目：

- `/etc/llmgateway/tunnel.env` 設定值是否正確；`~llmgw/.ssh/id_llmgw_tunnel` 是否存在且權限為 `0600`。
- 沒有有效位址檔、走靜態位址時，`~llmgw/.ssh/known_hosts_rendezvous` 是否包含正確的會合點主機金鑰。
- 會合點上的 4710 或 4711 埠是否殘留舊連線佔用（`ExitOnForwardFailure=yes` 會讓新連線立刻失敗重試；舊埠釋放依設計估計需 60–120 秒）。
- 設定變更的切換一律走 `llmgwctl tunnel-apply`（7.3），不要手動改了就重啟。

## 7. 隧道跟隨 MLP 輪換與 `NODE_GATEWAY` 讀取（#221）

本節是 LLMGateway 與 MyLinuxPool（MLP）脫鉤後的隧道運作與本人在 `<服務主機>` 要做的動作。標記「**未在真機驗證，P4 第一次執行時逐步確認**」的指令，代表只有真的 systemd／root／真機才跑得起來，沒在沙箱實跑過；第一次執行時請一步一步確認輸出，不要整段貼上。

> [!IMPORTANT]
> 本節的「切換」（7.3）與「觀察」（7.5）是**本人動作**，不是自動流程。上線前先確認有**不經隧道的進場方式**（雲端 Web Console、本機終端或區網 SSH）：`4711` 是唯一的遠端管理入口，切換時它會中斷。

### 7.1 運作方式、設定鍵與結束碼

**狀態機（確定性，連線健康時完全不碰 GitHub）**：

1. 啟動：有效的本機位址檔（`FOLLOW_FILE`）優先；沒有就用初始 `RENDEZVOUS` ＋ `KNOWN_HOSTS`（最後已知位址）直接連，不先打 GitHub。
2. 健康期間只跑 `ssh -W` 端對端探針（`GET /v1/subscriptions` 要 200），零 GitHub 請求。（#246 起探針改成先打 `GET /healthz`，模式由能力旗標決定，見 §5.12；隧道守護每次探前重讀旗標，system unit 經 `LGW_CAPABILITIES_FILE` 指向 `current` 的旗標，user 層一律 old；升級後隧道尚未重啟時仍跑舊腳本、探 list。）
3. `ssh` 退出、探針連續失敗達 `PROBE_FAILS`（預設 3），或完全沒有可用位址時，**同一行程內**才去抓 `NODE_GATEWAY`（`GATEWAY_SOURCE_*` 三個鍵都填才啟用）。抓取前先看持久化退避（`.tunnel_fetch_backoff`）與 `Retry-After`／`X-RateLimit-Reset`，期間記 `fetch_skipped` 不打 GitHub。
4. 抓到後嚴格驗證（`ip`、`port`、`tunnel_user`、`host_key`、`generation`），通過才原子寫 `FOLLOW_FILE` 與私用 `.tunnel_known_hosts`，**立刻同行程重連**；首次探針 200 之後才把 `generation` 寫進 `.tunnel_generation`（已驗證水位）並把連敗歸零。
5. 抓不到、被拒或驗證不過：寫抓取分類、保留最後已知位址，以結束碼 `1` 結束，由 systemd 10 秒後重開。

`4710`（API）與 `4711`（管理 SSH）**一律同時轉發**：`REMOTE_SSH` 未設、留空或 `4711` 都是 `4711`；**不支援 `off`**（`REMOTE_SSH=off` 會被當成無效設定，結束碼 `78`）。

**`/etc/llmgateway/tunnel.env` 設定鍵**（`KEY=VALUE`；程式優先讀環境變數 `LGW_TUNNEL_ENV` 指向的檔）：

| 組別 | 鍵 | 說明 |
|---|---|---|
| 輸入①（連線） | `IDENTITY`（必填） | 隧道私鑰路徑（`~llmgw/.ssh/id_llmgw_tunnel`） |
| | `RENDEZVOUS`、`TUNNEL_USER` | 本機 SSH 使用者＝`RENDEZVOUS` 的 `user@` 或 `TUNNEL_USER`，至少一個；兩個都設必須相同。`NODE_GATEWAY.tunnel_user` 只拿來比對，不會覆蓋本機帳號 |
| | `RENDEZVOUS`、`KNOWN_HOSTS` | 初始位址（`[user@]host:port`）與其 known_hosts：沒有有效 `FOLLOW_FILE` 時用的最後已知位址基準 |
| 輸入②（位址來源） | `GATEWAY_SOURCE_REPO` | `FATESAIKOU/MyLinuxPool` |
| | `GATEWAY_SOURCE_PATH` | `actions/variables/NODE_GATEWAY` |
| | `GATEWAY_GITHUB_KEY_FILE` | token 檔路徑（`/etc/llmgateway/mlp-gateway.token`）。即使用 drop-in `LoadCredential` 也要填，三個鍵缺一就不啟用 |
| | `FOLLOW_FILE`（選填） | 預設 `/var/lib/llmgateway/gateway.json`（`0600`）。**必須位於 `/var/lib/llmgateway` 之下**，否則要在 drop-in 補 `ReadWritePaths`（見 7.2） |
| | `TOKEN_EXPIRES_AT`（選填） | token 到期日，給 `llmgwctl check`／`status` 告警用（守護程式本身不讀）。建議寫 `YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SSZ`；其他格式（例如 `2026-12-31 00:00:00 UTC`）會被判為格式不合 |
| | `TOKEN_EXPIRY_WARN_DAYS`（選填） | 告警門檻天數，預設 `7`（`llmgwctl` 讀） |
| 埠與時間 | `REMOTE_API=4710`、`REMOTE_SSH=4711`、`LOCAL_API=8480` | 預設值如左 |
| | `PROBE_INTERVAL=60`、`PROBE_FAILS=3` | 探針間隔（秒）與連敗門檻 |
| | `SSH_PATH=ssh`、`SSH_CONNECT_TIMEOUT=10`、`SERVER_ALIVE_INTERVAL=30`、`SERVER_ALIVE_COUNT_MAX=3` | ssh 連線參數 |
| | `FETCH_TIMEOUT=10`、`FETCH_MIN_INTERVAL=60`、`FETCH_BACKOFF_MAX=900` | 抓取逾時與退避（`60s`×2ⁿ，上限 `900s`） |

只填部分 `GATEWAY_*` 鍵（靜態位址仍有效）時守護程式**不停機**，記 `config_partial` 並照靜態位址連線；`check` 會是 `todo`。舊的只有靜態三鍵（`RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS`）的 `tunnel.env` 原樣可用。

**結束碼**：

| 對象 | 碼 | 意思 |
|---|---|---|
| 守護程式 | `0` | 收到 SIGTERM／SIGINT 正常結束 |
| | `1` | 執行期失敗（連不上且抓不到、被拒、`ssh` 起不來…）；systemd 10 秒後重開 |
| | `78` | 設定本身不能啟動（缺 `IDENTITY`、缺本機使用者、靜態位址與 GitHub 來源兩者皆無且沒有有效 `FOLLOW_FILE`、參數非法含 `REMOTE_SSH=off`、未授權的 API base）。unit 的 `RestartPreventExitStatus=78` 讓它**不會**反覆重開；修好設定後要手動 `systemctl restart llmgateway-tunnel` |
| `--probe-once` | `0`／`1`／`2` | HTTP 200／不是 200 或沒回應／無法探（設定不能用、找不到 `ssh`、`FOLLOW_FILE` 有效但私用 `.tunnel_known_hosts` 缺或不符、或 `LGW_PROBE_API_MODE` 不是恰好 `old`／`new`）。嚴格只讀：不寫檔、不打 GitHub、**不重寫** `.tunnel_known_hosts` |
| `--fetch-check` | `0`／`1`／`78` | 可用／抓取或格式失敗（印分類，不印位址與 token）／設定不能用（沒啟用 GitHub 來源也是 `78`）。嚴格只讀、只打一次 GitHub、不寫任何狀態檔 |

**探針與能力旗標（#246）**：守護程式**每次探針之前**都重讀
`LGW_CAPABILITIES_FILE`（system unit 設為
`/opt/llmgateway/current/deploy/capabilities.json`），決定這次要走 `new` 還是
`old`；沒設就是 `old`（user 層與 `deploy/install.sh` 的檢查都是這樣）。

- `new`：只打 `GET /healthz`；非 200（含 404）就是失敗，**不退回 list**。
- `old`：先打 `/healthz`，**只有**拿到 404 才退回 `GET /v1/subscriptions`，200
  才算健康。兩次請求共用同一個期限，第二次剩不到 1 秒就算失敗。
- 旗標是 `invalid`／`unreadable` 時照樣以 `old` 探，並在**每條連線**只記一次
  `capability_invalid`（不洗版）。旗標載體、四態與升級中的停損 SOP 見 §5.12。
- daemon **忽略** `LGW_PROBE_API_MODE`（免得寫死的值凍住模式）；只有
  `--probe-once` 用它：值必須恰好是 `old`／`new`，其他值結束碼 `2` 且不連線；
  沒有這個變數時才讀旗標檔。
- 腳本收到不認得的 argv 一律結束碼 `2`：不起守護程式、不寫檔、不寫 log、不連線
  （舊版 `main()` 遇到多一個參數會直接起第二條隧道，搶 4710／4711）。

**狀態檔（`/var/lib/llmgateway/`，`llmgw` 擁有、`0600`、不含 token）**：

| 檔案 | 內容 |
|---|---|
| `gateway.json`（`FOLLOW_FILE`） | 目前套用的位址、`ssh_port`、`tunnel_user`、`host_key`、`generation` |
| `.tunnel_known_hosts` | 由 `FOLLOW_FILE` 產生的私用 known_hosts（單行） |
| `.tunnel_generation` | 已驗證水位（`generation`＋指紋），首次探針 200 後才寫 |
| `.tunnel_fetch_state` | 最近一次抓取分類：`ok`、`regressed`、`invalid`、`hostkey_missing`、`auth_failed`、`rate_limited`、`unreachable`、`config_partial`、`credential_unreadable` |
| `.tunnel_fetch_backoff` | 下次允許抓取時間與連敗次數 |
| `.tunnel_token_meta` | 最近一次成功抓取的時間與 token 到期標頭（若有） |

**journal 事件**（`journalctl -u llmgateway-tunnel`；只寫固定事件名，不寫位址、IP、token 或 ssh 的 stderr）：`tunnel_up`、`probe_fail`、`restart`、`capability_invalid`、`fetch_ok`、`fetch_skipped`、`fetch_failed`、`fetch_auth_failed`、`fetch_rate_limited`、`fetch_invalid`、`gateway_updated`、`gateway_reverted_to_verified`、`unverified_retry`、`follow_missing`、`follow_invalid`、`follow_regressed`、`verified_invalid`、`verified_unreadable`、`verified_write_failed`、`hostkey_missing`、`config_partial`、`config_missing`、`spawn_fail`。

### 7.2 讀取 `NODE_GATEWAY` 的 token（修訂 §1.1「不放 token 檔」）

**受控例外**：§1.1 的「`gh` 登入不放 token 檔」仍只適用於 `LLMGateway-keys` 金鑰同步。要讓隧道跟隨輪換，需要另放**一把** GitHub fine-grained PAT 檔，規格如下，其他一律不變：

- **建立**（GitHub → Settings → Developer settings → Fine-grained tokens）：
  - Repository access 選 **Only select repositories**，只勾 `FATESAIKOU/MyLinuxPool`（**不要**勾 `LLMGateway-keys` 或任何其他 repo）。
  - Repository permissions 只給 **`Variables: Read-only`**（`Metadata: Read-only` 是 GitHub 自動附帶）；不給 `Contents`、`Actions`、`Secrets` 或任何 Write。
  - **記下到期日**，之後填進 `TOKEN_EXPIRES_AT`。
  - **已知限度（本人已接受的風險）**：這是 fine-grained PAT 能給的最小授權，但它**仍可讀取該 repo 全部 Actions variables**（含其他 `NODE_*` 的位址拓樸，不含 Secrets），無法限縮到只有 `NODE_GATEWAY`。
- **放置**：`/etc/llmgateway/mlp-gateway.token`，`root:root`、`0600`，單行純 token。**不要把 token 放進指令列參數或 shell 歷史**，用標準輸入貼：

  ```bash
  sudo sh -c 'umask 077; cat > /etc/llmgateway/mlp-gateway.token'   # 貼上 token，換行後按 Ctrl-D
  sudo ls -l /etc/llmgateway/mlp-gateway.token                      # 確認 -rw------- root root
  ```

  （`umask 077; cat >` 這段已在沙箱用暫存路徑實跑確認權限為 `-rw-------`；`sudo` 與 `root:root` 擁有者在真機才確認。）
- **讀取方式：drop-in `LoadCredential`**。出貨的 `llmgateway-tunnel.service` **不含** `LoadCredential=`（沒有 token 檔的機器否則重啟隧道會直接啟動失敗）；改由本人在切換時（7.3）以 `tunnel-apply` 放 drop-in `/etc/systemd/system/llmgateway-tunnel.service.d/credential.conf`。程式優先讀 `$CREDENTIALS_DIRECTORY/gateway-token`，否則讀 `GATEWAY_GITHUB_KEY_FILE`；讀不到（缺檔或沒權限；`llmgw` 讀不了 `root:root 0600` 的檔，所以沒放 drop-in 就會是這樣）只算**抓取失敗**（分類 `credential_unreadable`、結束碼 `1`），不會是 `78`。暫存 drop-in 內容：

  ```ini
  [Service]
  LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token
  ```

  若 `FOLLOW_FILE` 要放在 `/var/lib/llmgateway` 以外，同一個 drop-in 要再加一行 `ReadWritePaths=<FOLLOW_FILE 所在目錄>`（unit 有 `ProtectSystem=strict`，只有 `/var/lib/llmgateway` 可寫；寫不進去會被記成抓取失敗 `unreachable`、位址永遠換不成）。**預設值最省事：不要自訂 `FOLLOW_FILE`。** 此 drop-in 寫法**未在真機驗證，P4 第一次執行時逐步確認**。
- **出貨 unit 的加固**：`NoNewPrivileges=true`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/llmgateway`、`PrivateTmp=true`。`IDENTITY`、`KNOWN_HOSTS`（`~llmgw/.ssh/`）仍是唯讀可讀。
- **token 不出現在任何輸出**：`check`、`status`、`.tunnel_*` 狀態檔、journal 都不含 token；只有 `Authorization` 標頭在記憶體中用到它。

**平穩期悄悄到期的限制（務必知道）**：連線健康時守護**不碰 GitHub**，所以 token 在平穩無斷線的那幾個月裡過期，**沒有任何訊號**，要到下一次輪換斷線、需要抓位址的那一刻才收到 `401`／`403` 而發現。降低這個風險的做法：

1. **`TOKEN_EXPIRES_AT`**：填進 `tunnel.env`，剩 ≤ 7 天（`TOKEN_EXPIRY_WARN_DAYS` 可調）`llmgwctl check` 回 `todo`；格式不合回 `todo`（`TOKEN_EXPIRES_AT 格式不合`）。
2. **人工 `--fetch-check`**（建議定期跑、token 到期前一定跑）：用 `llmgw` 身分＋`LoadCredential` 做一次只讀的真實請求，驗 token 能讀 `NODE_GATEWAY`、欄位格式合規：

   ```bash
   # <臨時設定檔> 先用 /etc/llmgateway/tunnel.env（日常檢查）；7.3 切換前用 tunnel.env.new
   sudo systemd-run --wait --pipe -p User=llmgw \
     -p LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token \
     -E LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env \
     /opt/llmgateway/current/ops-scripts/llmgateway-tunnel --fetch-check
   ```

   **必須帶 `-E LGW_TUNNEL_ENV=<設定檔>`**：transient unit 不會繼承 unit 檔的 `Environment=`，不帶就找不到設定而結束碼 `78`。成功輸出（用 mock GitHub 在沙箱實跑，位址與金鑰不會印出）：

   ```
   fetch_check: ok
   http_status: 200
   format: ok
   generation: <n>
   expiry_header: present|absent
   如 check 仍紅請執行 llmgwctl follow-reset --fetch-state
   ```

   失敗只印 `fetch_check: failed (<分類>)`，結束碼 `1`；沒有啟用 GitHub 來源或設定不能用印 `config_missing`、結束碼 `78`。`systemd-run` 這整條指令**未在真機驗證，P4 第一次執行時逐步確認**（沙箱沒有 systemd，只實跑了 `--fetch-check` 本身）。
3. **標頭到期日**：每次成功抓取會把 `github-authentication-token-expiration` 記進 `.tunnel_token_meta`；日期 ≤ 抓取當下時間視為 `expiry_unknown`，不誤報。但這只在發生過成功抓取後才有值，不能取代前兩項。

**換 token（或 token 失效後處理）**：

1. 把新 token 放在 `/etc/llmgateway/mlp-gateway.token.new`（同上 `umask 077`），先用 `-p LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token.new` 跑一次 `--fetch-check` 確認可用。
2. 再 `sudo mv /etc/llmgateway/mlp-gateway.token.new /etc/llmgateway/mlp-gateway.token`。`LoadCredential` 在服務**啟動時**取快照，運行中的隧道仍持有舊 token；要讓它生效就得重啟隧道（4710／4711 會中斷，建議用 7.3 的 `tunnel-apply` 保險來重啟，`--new-env` 帶 `tunnel.env` 的複本）。這一步依 systemd `LoadCredential` 語意推論（與 §5.5 的 `keys.pass` 同理），**未在真機驗證，P4 第一次執行時逐步確認**。
3. 換好後執行 `sudo llmgwctl follow-reset --fetch-state` 清掉 `auth_failed`（只清 `.tunnel_fetch_state` 與 `.tunnel_fetch_backoff`，**不動** `.tunnel_generation` 水位與 `FOLLOW_FILE`）；`--fetch-check` 成功時也會印這句提示。

### 7.3 切換到跟隨模式（P4，本人動作；必用反向保險）

重啟隧道就會斷掉你走的 `4711`，若新設定寫錯就失聯。所以切換**一律**用 `llmgwctl tunnel-apply`，不要手動改 `tunnel.env` 再重啟。

**`tunnel-apply` 流程**（程式實際行為；需 root；`<ctl>`＝`/var/lib/llmgateway-ctl/`）：

1. 以 `flock`（`<ctl>/.tunnel-apply.lock`）互斥；檢查 `--revert-after`（預設 `10m`，**下限 `5m`**，更短拒收；格式如 `10m`、`600s`）與暫存檔（必須是一般檔，不是 symlink）。
2. **先備份**到 `<ctl>/tunnel-apply.bak/`（目錄 `0700`、檔 `0600`）：目前的 `tunnel.env`、drop-in `credential.conf`、切換前已存在的 `FOLLOW_FILE`（含預設路徑；新舊路徑不同兩個都記）與 `.tunnel_known_hosts`，逐項記路徑與存在性（原本不存在記為「不存在」）。備份失敗就不切換。
3. 起獨立 transient unit `lgw-tunnel-apply`，之後的步驟都在裡面做：**前景指令被 SIGHUP／SIGTERM 殺掉（例如 4711 連線被重啟斷開）也會做完**。
4. **先排保險** `lgw-tunnel-revert.timer`（`--on-active=<revert-after>`）；排不起來就中止，**不套用任何新檔**。
5. **以複製套用**暫存檔（`--new-env`→`/etc/llmgateway/tunnel.env`；有 `--new-dropin` 才放 drop-in）；`.new` 暫存檔保留，失敗後可重試。
6. `systemctl daemon-reload`、重啟 `llmgateway-tunnel.service`，然後探針（經會合點 `4710` 要 200）最多等 **180 秒**（正式環境下限 90 秒）。探針回 `rc=2`（無法探）**視為失敗**。
7. 失敗或逾時立刻還原（備份的 `tunnel.env`；drop-in 原不存在就只刪本次放的 `credential.conf`，目錄是本次新建且已空才 `rmdir`；`FOLLOW_FILE`／`.tunnel_known_hosts` 切換前已存在者還原成切換前內容，本次新建者改名為 `*.reverted-<時間戳>`，不刪），再 `daemon-reload` 並重啟隧道。
8. 通過時狀態為 `pending-confirm`，指令回 `0`；其餘結果回 `1`。你重連 `<會合點>:4711` 登入、確認後執行 `llmgwctl tunnel-apply --confirm` 取消保險。前景的 `tunnel-apply` 最多追蹤內層約 5 分鐘；逾時會印「還在進行」並回 `1`，那不代表失敗，改看 `journalctl -u lgw-tunnel-apply`。**`--confirm` 之前保險到期（或還原已觸發），`--confirm` 會拒絕並改走還原**，要重來請重新跑 `tunnel-apply --new-env …`。

狀態檔 `<ctl>/.tunnel-apply.state` 的階段名：`apply-scheduled`、`applying`、`pending-confirm`、`confirmed`、`reverted`、`revert-failed`、`aborted`。看進度：`journalctl -u lgw-tunnel-apply -n 100`；保險還原看 `journalctl -u lgw-tunnel-revert`。

> [!WARNING]
> **`lgw-tunnel-revert.timer` 是 transient timer，重開機後即失效。`tunnel-apply` 窗口內（從下指令到 `--confirm`）切勿重開機。**
> **4.2 與 4.3 之間（下面步驟 2 到 3）不要改運行中的 `tunnel.env`**：步驟 3 會用暫存檔取代它（備份是在套用前做的，備份的是當下那份）。

**還原失敗**：還原本身失敗時狀態變 `revert-failed`（屬「進行中」，會擋住新的一輪 `tunnel-apply`，備份原樣保留），程式另排 `lgw-tunnel-revert-retry<N>.timer` 每 60 秒重試（最多 5 次）。用盡或排不起來會印固定的人工恢復指引。修好原因後可手動重試：

```bash
sudo llmgwctl tunnel-apply --retry-revert     # 只在 revert-failed 可用；成功才進 reverted
```

**切換步驟**（`<服務主機>`；除 `llmgwctl` 子命令的旗標外，`systemd-run`、`systemctl`、`sudo`、檔案擁有者都**未在真機驗證，P4 第一次執行時逐步確認**）：

1. **放 token 與暫存 drop-in**（不直接裝進 `/etc/systemd/system/`，由步驟 3 在備份後套用）：

   ```bash
   # token：見 7.2（root:root 0600 的 /etc/llmgateway/mlp-gateway.token）
   printf '[Service]\nLoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token\n' \
     | sudo tee /etc/llmgateway/credential.conf.new >/dev/null
   ```

2. **升級、做暫存設定並先驗 token**（運行中的 `tunnel.env` 與 drop-in 這一步**完全不動**）：

   ```bash
   sudo llmgwctl upgrade --tag <新版>        # 只重啟 llmgateway.service，不碰正在跑的隧道
   # 複製成暫存檔；擁有者與權限要和 tunnel.env 一樣（root:llmgw 0640），下一步 llmgw 才讀得到
   sudo install -m 0640 -o root -g llmgw /etc/llmgateway/tunnel.env /etc/llmgateway/tunnel.env.new
   sudoedit /etc/llmgateway/tunnel.env.new   # 在暫存檔加入輸入②，見下
   sudo systemd-run --wait --pipe -p User=llmgw \
     -p LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token \
     -E LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env.new \
     /opt/llmgateway/current/ops-scripts/llmgateway-tunnel --fetch-check
   ```

   暫存檔要加的鍵（其餘原樣保留，**特別是既有靜態 `RENDEZVOUS`、`KNOWN_HOSTS` 與 `REMOTE_SSH=4711`，切換期間保留；P5 清掉靜態鍵之後不再保留，見 7.4、7.6**；`TUNNEL_USER` 或 `RENDEZVOUS` 的 `user@` 要有本機 SSH 帳號）：

   ```
   GATEWAY_SOURCE_REPO=FATESAIKOU/MyLinuxPool
   GATEWAY_SOURCE_PATH=actions/variables/NODE_GATEWAY
   GATEWAY_GITHUB_KEY_FILE=/etc/llmgateway/mlp-gateway.token
   TOKEN_EXPIRES_AT=<token 到期日，例 2027-06-30>
   FOLLOW_FILE=/var/lib/llmgateway/gateway.json
   ```

   `--fetch-check` 要看到 `fetch_check: ok` 才往下；`failed (auth_failed)`＝token 權限或到期有問題，`failed (credential_unreadable)`＝`LoadCredential` 沒帶到或 token 檔不在。

3. **套用（保險下）**：

   ```bash
   sudo llmgwctl tunnel-apply --new-env /etc/llmgateway/tunnel.env.new \
     --new-dropin /etc/llmgateway/credential.conf.new [--revert-after 10m]
   ```

   指令會斷在你的 `4711` 連線上：這是預期的。等幾分鐘後用第二個終端機重連 `<會合點>:4711`（或用進場備援），看 `journalctl -u lgw-tunnel-apply -n 100`。套用後先 `sudo systemctl daemon-reload`，**再**確認 drop-in 生效（沒有 `daemon-reload` 之前 `show` 看到的是舊值；`tunnel-apply` 本身在套用後已做過一次）：

   ```bash
   sudo systemctl daemon-reload
   systemctl show llmgateway-tunnel -p LoadCredential      # 預期含 gateway-token:/etc/llmgateway/mlp-gateway.token
   ```

4. **驗收後確認**（保險時限內）：

   ```bash
   sudo llmgwctl check
   llmgwctl status
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<本機埠>/healthz   # 經你自備的 4710 通道，預期 200
   sudo llmgwctl tunnel-apply --confirm
   ```

   **切換後第一次輪換之前，`tunnel-follow` 會顯示 `missing`、`check` 為 `todo`（結束碼 `2`）**：這是目前程式的實際行為，不是切換失敗。`FOLLOW_FILE` 只有在第一次連不上而抓到新位址時才會寫出，所以 `gen=<n>` 與 `tunnel-fetch` 的 `ok` 要等第一次真實輪換之後才看得到（沙箱以 `llmgwctl check` 實跑確認過「隧道 active＋`FOLLOW_FILE` 缺」會是 `todo tunnel-follow（follow 檔缺失，現存連線靠最後已知位址維持；需人工確認 NODE_GATEWAY）`）。切換驗收看 `list-remote` 是 `ok`、隧道 `active`、`tunnel-token` 不是 `expiry_due`／`expiry_invalid`。

`llmgwctl tunnel-apply` 的 `--help`（沙箱實跑）旗標只有 `--new-env`、`--new-dropin`、`--revert-after`、`--confirm`、`--retry-revert`；其餘旗標是給 transient unit 內部用的，**不要手打**。

### 7.4 靜態鍵（P5 之後已移除）

切換後到 P5 之前，`tunnel.env` **保留**原本的靜態 `RENDEZVOUS`、`KNOWN_HOSTS`（以及 `~llmgw/.ssh/known_hosts_rendezvous` 檔）。`REMOTE_SSH=4711`（會合點上轉回本機 sshd 的管理埠）不屬於靜態位址，一直保留。P5 之前保留它們的原因：

- `llmgwctl rollback` 若回到 `lgw-v0.8.0` **之前**的版本（隧道腳本不認 `FOLLOW_FILE`／GitHub 抓取），舊腳本只認這幾個靜態鍵；回到 `lgw-v0.8.0`（含）之後的版本不受影響。
- 沒有有效 `FOLLOW_FILE` 時（第一次輪換之前）守護本來就用它們連線；`tunnel-apply` 的探針也靠它們（`FOLLOW_FILE` 缺失時走靜態位址）。
- 注意：靜態值是切換當時的位址，**輪換過之後就過時了**，不能直接當退路用。

**P5 之後（#221，2026-11-23 於服務主機完成並確認）**：靜態鍵的角色結束，`RENDEZVOUS`、`KNOWN_HOSTS` 已從 `tunnel.env` 移除，`~llmgw/.ssh/known_hosts_rendezvous` 已收起。位址來源只剩 `gateway.json` 與 GitHub 抓取，**沒有靜態後備位址**；本機 SSH 使用者改由 `TUNNEL_USER` 提供（必須存在）。移除的程序與後果見 7.6。

### 7.5 第一次真實輪換觀察與退回 SOP

`rotate-gateway` 的 `dry_run` 不動 `NODE_GATEWAY`，端對端只能在**真的輪換**時觀察。下一次 MLP 實際輪換時，由本人（或 MLP PMO）在 `<服務主機>` 看：

```bash
journalctl -u llmgateway-tunnel --since '<輪換時間>' | grep -E 'probe_fail|restart|fetch_|gateway_|tunnel_up|follow_|hostkey_missing|unverified_retry'
sudo llmgwctl check
llmgwctl status
```

**預期順序**：（`probe_fail` … `restart`，或 ssh 直接掉線）→ `fetch_ok` → `gateway_updated` → `tunnel_up`；沒有 `fetch_auth_failed`／`hostkey_missing`／`follow_regressed`。事後 `status` 應顯示 `tunnel-follow ok (gen=<新代>)`、`tunnel-fetch ok`，`check` 全綠。**RTO 預期約 2–3.5 分鐘**（含會合點上舊埠釋放需 60–120 秒；這是設計估算，**尚未實測**，請記錄實際值）。

| 看到的事件／狀態 | 意思 | 處置 |
|---|---|---|
| `gateway_updated` 後反覆 `tunnel_up`→斷線→`unverified_retry`（退避期間內則是 `fetch_skipped`），每約 10 秒一輪 | 新代已套用但連不通 | **先查 `NODE_GATEWAY` 的 `host_key` 是否殘留上一代（舊機）金鑰**（依設計階段對 MLP 輪換腳本的查證：`host_key` 為空時它會保留舊值）；請 MLP 修正後會再抓到更新的 `generation` |
| `fetch_auth_failed`、`tunnel-fetch auth_failed` | token 失效、被撤銷或權限不對 | 7.2「換 token」流程；換好後 `sudo llmgwctl follow-reset --fetch-state` |
| `fetch_rate_limited` | GitHub 限流或 `Retry-After` | 等；守護自己依退避重試（`60s`～`900s`） |
| `follow_regressed`（`tunnel-follow regressed`） | 抓到或本機檔的 `generation` 低於已驗證水位（MLP 回退） | 先**人工確認 `NODE_GATEWAY`** 是否為合法的回退；確認後修好 `NODE_GATEWAY`，`sudo llmgwctl follow-reset`（清 `.tunnel_generation`、`.tunnel_fetch_backoff`、`.tunnel_fetch_state`）並 `sudo systemctl restart llmgateway-tunnel` |
| `follow_invalid`／`hostkey_missing`／`fetch_invalid` | 同代欄位被改、`host_key` 缺漏或格式不合、欄位驗證不過（含 `tunnel_user` 與本機不符） | 人工確認 `NODE_GATEWAY`，請 MLP 修正；守護保留最後已知位址持續重試，不會連到未通過驗證的位址 |
| `credential_unreadable` | token 檔缺、沒權限，或 drop-in `LoadCredential` 沒生效 | 7.2；`sudo systemctl daemon-reload` 後 `systemctl show llmgateway-tunnel -p LoadCredential` |

**退回（應急止血）**：若自動跟隨失敗又等不及。**P5 之後沒有靜態後備位址**，退回是按根因恢復，不是換回靜態位址：

1. 先照上表修好根因（`NODE_GATEWAY`、token）。
2. 按根因恢復：
   - token 失效、被撤銷或權限不對：7.2「換 token」流程；照 7.2 的流程做完（含用 `tunnel-apply` 保險重啟隧道），再 `sudo llmgwctl follow-reset --fetch-state`。
   - `NODE_GATEWAY` 殘留舊代金鑰、格式不合：請 MLP 修正；新的一代抓到即可，**不需要** `follow-reset`。
   - `follow_regressed`（MLP 回退）：先人工確認是否為合法回退，確認並修好來源後才 `sudo llmgwctl follow-reset`（不帶 `--fetch-state` 會清已驗證水位，降低回退檢查），再重啟隧道。
   - 修好後守護會從 GitHub 重抓。`gateway.json` 缺失或損毀時也會自動取回（需 GitHub 可讀、token 有效、水位相容）；這些條件成立時不必手動改檔。
   - 仍恢復不了（抓取持續失敗，或水位檔損毀、回退不通過）：由 MLP PMO 提供目前位址與主機金鑰，由 PM 寫成新的 `tunnel.env.new` 並走 `tunnel-apply --new-env`（見 7.6 的保險程序；不要手動只改單一檔）：此時要在 `tunnel.env.new` **暫時加回** `RENDEZVOUS=[user@]host:port` 與 `KNOWN_HOSTS=<檔案路徑>`（檔案內容為 MLP PMO 提供的主機金鑰，必要時由 `known_hosts_rendezvous.retired-*` 取回或重寫；`TUNNEL_USER` 與 `RENDEZVOUS` 的 user 必須相同），否則 `tunnel-apply` 的探針沒有位址可用、必定失敗並自動還原；恢復後再依 7.6 移除。**若 `gateway.json` 格式與水位都合法但位址已過時（例如輪換後 token 又失效、一直抓不到新位址），守護與探針會優先用它而忽略靜態鍵**：要先把它移開（`sudo mv /var/lib/llmgateway/gateway.json /var/lib/llmgateway/gateway.json.bad`），再跑 `tunnel-apply`；注意 `tunnel-apply` 的備份是套用當下才做，保險自動還原時**不會**把這個已移開的檔放回來。這是目前唯一人工注入位址的手段。
   - 舊做法（**僅適用 P5 之前、靜態鍵仍在且靜態值仍正確的期間**；此步驟未在真機驗證）：把 `FOLLOW_FILE` 改名移開（`sudo mv /var/lib/llmgateway/gateway.json /var/lib/llmgateway/gateway.json.bad`）再重啟，守護才會退回靜態 `RENDEZVOUS`／`KNOWN_HOSTS`。P5 之後這個做法不再退回靜態位址，守護只會去 GitHub 重抓。
3. 真的要退回舊版：`sudo llmgwctl rollback` 只切版本、安裝該版 unit 並重啟 `llmgateway.service`，不改 `tunnel.env`、不重啟隧道。**回到 `lgw-v0.8.0` 之前的版本在 P5 之後不可行**（舊隧道腳本需要靜態 `RENDEZVOUS`／`KNOWN_HOSTS`；隧道下次重啟會以設定缺漏結束碼 78 失敗，見 7.4、7.6）；回到 `lgw-v0.8.0`（含）之後的版本不受影響。

### 7.6 P5：清舊靜態鍵（已完成，#221）

觀察期結束後，從 `tunnel.env` 移除過時靜態鍵 `RENDEZVOUS`、`KNOWN_HOSTS`，並收起 `~llmgw/.ssh/known_hosts_rendezvous`。移除的程序（修改 `tunnel.env` 一律走 `tunnel-apply --new-env`，讓保險在重啟隧道時守著）：

1. 在暫存檔刪掉 `RENDEZVOUS=` 與 `KNOWN_HOSTS=` 兩行；`TUNNEL_USER=<本機 SSH 使用者>` 必須存在（本機使用者只能由它提供）。`REMOTE_SSH=4711`、`GATEWAY_*`、`IDENTITY` 等其他鍵原樣保留。
2. 以 `grep -c` 確認舊兩行真的刪了（預期 0），`TUNNEL_USER` 恰一行；再以 `--fetch-check`（`LGW_TUNNEL_ENV` 指向暫存檔）驗 token。`--fetch-check` 通過**不證明**舊鍵已刪，所以要靠 `grep` 驗。
3. `sudo llmgwctl tunnel-apply --new-env /etc/llmgateway/tunnel.env.new`：保險下套用，步驟與驗收同 7.3 第 3、4 步；**`tunnel-apply --confirm` 之前不算完成**。
4. 確認後：`rm /etc/llmgateway/tunnel.env.new`（套用後留著，可能被誤當有效暫存檔重套）；`~llmgw/.ssh/known_hosts_rendezvous` 用 `mv` 收成 `.retired-<時間>`，不用 `rm`（等再過一輪輪換確認沒事後才可真的刪）。守護連線用的是 `/var/lib/llmgateway/.tunnel_known_hosts`，收起舊檔不影響。**不要刪私鑰 `id_llmgw_tunnel`。**
5. 通知 MLP PMO：「輪換後人工照貼新位址與 `known_hosts_rendezvous`」那一塊作廢（MLP#1511 相關部分由 MLP PMO 決定是否關閉）。**`rotate-gateway` 仍須保留 `TUNNEL_EXTRA_PUBKEYS` 與完整寫入 `NODE_GATEWAY`（含新機 `host_key`）的契約，不可拆。**

**P5 之後的後果**：

- **沒有靜態後備位址**。位址來源只剩 `gateway.json`（有效時啟動就用它）與 GitHub。`gateway.json` 缺失或損毀時，守護會嘗試從 GitHub 取回；只要 GitHub 可讀、token 有效、資料與水位檔相容且可寫入，就能自動恢復。
- **抓取失敗或水位檔損毀時會連不上**：抓取失敗指 token 失效、被限流、網路不通；水位檔損毀或回退不通過也一樣。守護依退避反覆重啟（程序結束後 10 秒重啟，但 GitHub 請求有退避，不是每 10 秒一次），4710／4711 會斷到原因排除為止，需人工按原因處理（見 7.5 表）。已建立的連線不會因 token 稍後失效而立刻中斷。
- **token 在平穩期悄悄到期，沒有即時訊號**：要定期看 `sudo llmgwctl check` 的 `tunnel-token`（見 7.3），不要等斷線才查。
- **`llmgwctl rollback` 回到 `lgw-v0.8.0` 之前的版本不再可行**（舊隧道腳本需要靜態鍵）；回到 `lgw-v0.8.0`（含）之後的版本不受影響。
- **備份目錄會被下一次 `tunnel-apply` 整個清空重建**：要留的備份先複製出來（root 0600，複製時用 `sudo`）。
- 舊 `known_hosts_rendezvous` 已收起（`.retired-<時間>`），不是有效的退路。

repo 側的舊靜態鍵文件與腳本殘留，另開 d 單清理；清理前不要對真機執行會清舊設定的 `llmgwctl upgrade`。

---

## 8. 應用層認證

本章涵蓋 `LGWD_AUTH_MODE` 的設定、首次啟用、名冊（principal／key）的維護、輪替與撤銷、回退與故障修復。預設 `off`，行為與以前相同。每一節開頭寫明「要 root 嗎」「lgwd 要停嗎」；管理指令一律由 root 在當版目錄（`/opt/llmgateway/current`）以 `sudo llmgwctl` 執行。

### 8.1 認證設定（a）

**root：** 要（寫 `/etc/llmgateway/llmgateway.env`）。**lgwd 停止：** 不用，改完 `sudo llmgwctl restart`。

`lgwd` 沒有設定檔，認證設定與既有設定同一套慣例：命令列優先，其次環境變數，最後預設值（正式部署用 unit 的 `EnvironmentFile`）。路徑須為絕對路徑。

| 旗標 | 環境變數 | 預設值 |
| --- | --- | --- |
| `--auth-mode` | `LGWD_AUTH_MODE` | `off`（另有 `warn`、`enforce`） |
| `--auth-registry` | `LGWD_AUTH_REGISTRY` | `/etc/llmgateway/auth-registry.json` |
| `--auth-state-dir` | `LGWD_AUTH_STATE_DIR` | `/var/lib/llmgateway` |
| `--auth-sshkeygen-path` | `LGWD_AUTH_SSHKEYGEN_PATH` | `/usr/bin/ssh-keygen` |
| `--auth-per-key-max` | `LGWD_AUTH_PER_KEY_MAX` | `600`（正整數） |
| `--auth-verify-concurrency` | `LGWD_AUTH_VERIFY_CONCURRENCY` | `16`（1 至 32） |
| `--auth-verify-timeout` | `LGWD_AUTH_VERIFY_TIMEOUT` | `2`（正有限數，秒） |

- `off`：不驗證；啟動只記一行 `auth mode=off`。存取控制只靠 loopback 與 SSH 轉發。
- `warn`：有名冊就驗章與授權，並把 would-deny 記進日誌，但**所有結果照舊處理業務**（仍會洩漏憑證）。名冊、`ssh-keygen` 或兩個庫不可用時不阻止啟動，記 `auth_unavailable` 並標示驗證不可判定，不誤報成功。
- `enforce`：五條業務路由先驗章與授權才進業務。啟動須有有效名冊（或信任庫的已驗證快照）、可用且自測通過的 `ssh-keygen`、已存在且庫識別一致的兩個庫，否則拒絕啟動。

**認證日誌怎麼讀（#315）**：行格式 `auth mode=… outcome=… reason=… route=… principal=… count=…`。`count` 是「這一行之前還沒被任何一行記錄的事件數」，所以同一組鍵（mode、outcome、reason、路由、principal）所有行的 `count` **加總就是事件次數**，不要取最後一行或單看一行。每組鍵在一個 60 秒視窗內最多兩行：第一筆事件立刻寫 `count=1`，視窗內其餘事件只累加，等視窗到期才寫出 `count=N−1`（沒有其餘事件就不寫）。收尾行最晚在視窗加上輪詢間隔（預設約 62 秒）之後出現；關機時會全部補寫。warn 降級（信任庫打不開）時沒有背景輪詢，收尾行只在同一組鍵的下一筆事件或關機時才出現。

其他路徑一律由 `auth-state-dir` 推導，沒有另外的旗標：`auth-replay.sqlite`（可丟棄的防重放庫）、`auth-trust.sqlite`（世代水位、墓碑、已驗證快照）、`auth-store.lock`、`.auth-registry.status`、`.auth-busy.status`、`allowed_signers.<世代>`、`sshsig/`、`auth-audit.log`。

結束碼：設定值不合法（含未知模式）以結束碼 2 結束；因認證理由拒絕啟動以結束碼 78 結束。2 仍照舊讓 systemd 重啟（OSError 等可能是暫時性的，例如 bind 失敗）；78 由 unit 的 `RestartPreventExitStatus=78` 擋下，不反覆重啟（見 8.17）。

### 8.2 首次啟用（離線可重現）（b）

**root：** 要。**lgwd 停止：** 要（`auth-store init` 是離線寫入）。

1. 停服務：
```bash
sudo llmgwctl stop
```
2. 建兩個庫（任一庫已存在就拒絕；兩庫都遺失時要先確認這真的是首次啟用）：
```bash
sudo llmgwctl auth-store init --confirm
```
3. 備妥名冊，`root:llmgw` 0640（內容見 8.4）。候選檔放在**別的位置**（例如 root 的家目錄 `/root/auth-registry.json`）；第 7 步的 `install` 會把它原子寫進正式路徑 `/etc/llmgateway/auth-registry.json`，不要直接拿正式路徑當候選檔。若尚未建立本機管理私鑰，先照 8.3 產生 `/etc/llmgateway/local-admin-signing-key`，並把它的公鑰填進名冊的 `llmgwctl-local-admin`。
4. 離線驗名冊（lgwd 啟動後、安裝前後都可以再跑一次；`--against-trust` 需 root，會比對信任庫的世代水位、墓碑與內容雜湊）：
```bash
sudo llmgwctl auth-registry check <檔>
```
5. 把 `LGWD_AUTH_MODE=warn` 寫進 `/etc/llmgateway/llmgateway.env`。
6. 啟動：
```bash
sudo llmgwctl start
```
7. 安裝名冊並等 lgwd 載入確認（回 0 才算完成）：
```bash
sudo llmgwctl auth-registry install <檔>
```
8. 檢查：
```bash
sudo llmgwctl check
```
`warn` 下 `auth-local` 會報 `todo`、`check` 結束碼為 2，屬正常；切到 `enforce` 之後才會全綠。

首次啟用的名冊若是 **seed 名冊**（`generation 1`、只含 `local-admin` principal），步驟 3 的候選檔就是它；seed 只能在 warn 開了之後 install（lgwd 不載入 warn 以外的名冊），開 warn 的順序與授權見 `docs/auth-rollout.md` §5 第 1～3 步。之後加入 MLP 的 worker 名冊，改走 8.20 的首次使用。

`auth-store init` 建庫時把鎖檔與兩個庫設成 `llmgw` 擁有、0600；lgwd 在 warn／enforce 啟動時以 `llmgw` 身分開鎖檔（`O_RDWR|O_CREAT`，0600）並取獨佔 `flock`，所以 init 之後 lgwd 拿得到鎖。lgwd **在任何模式都不建庫**：任一庫缺檔或識別不一致，enforce 拒絕啟動、warn 告警並以驗證不可判定照舊處理。

### 8.3 本機管理 principal（c）

**root：** 要（建私鑰與安裝名冊）。**lgwd 停止：** 不用。

`llmgwctl usage` 與 `check` 的 `auth-local` 以本機管理 principal 簽章：

- 私鑰：`/etc/llmgateway/local-admin-signing-key`，`root:root`，權限**恰為 0600**。0400 也會被拒，因為檢查是「恰等於」，不是「沒有群組／其他人權限」。
- 型別：`ssh-ed25519`、**沒有密語**、不載入 ssh-agent。
- key ID：`llmgwctl-local-admin`（可用 `LLMGW_LOCAL_ADMIN_KEY_ID` 覆寫；覆寫時必須與名冊登記的 `key_id` 一致）。
- 名冊登記的權限：`ops` 為 `["list","usage","roles"]`、`resolve_agents` 為 `[]`、`roles` 列出全部角色與 agent（讓 `usage --role` 可用）。

建私鑰（不覆蓋既有的）：
```bash
sudo ssh-keygen -q -t ed25519 -N '' -C '' -f /etc/llmgateway/local-admin-signing-key
sudo chown root:root /etc/llmgateway/local-admin-signing-key
sudo chmod 0600 /etc/llmgateway/local-admin-signing-key
```
非 root 讀不到私鑰時，`usage` 照 design 只送一次無簽請求（enforce 下回 401、結束碼 1 並提示用 `sudo`），`check` 的 `auth-local` 記 `todo`；**不得**先簽、失敗後再改送無簽，也不重試。

### 8.4 名冊預設（B）與範例（d）

**root：** 要。**lgwd 停止：** 不用（`install` 不必停）。

本人裁定 B：節點 principal 預設五種操作全開，`resolve_agents` 與 `roles` 填滿。名冊是 UTF-8 JSON（無 BOM、≤1 MiB、物件內重複鍵即拒絕），schema 1，頂層鍵恰為 `schema`、`generation`、`principals`、`keys`。`generation` 是 1～2^53−1 的整數，每次變更（新增、輪替、撤銷）都要加一。

範例有兩個 principal（節點與本機管理）與一筆 `revoked` 鍵（墓碑示範；撤銷的 key 不得從名冊刪除）：

```json
{
  "schema": 1,
  "generation": 12,
  "principals": {
    "w1-worker": {
      "ops": ["list", "usage", "roles", "resolve", "cooldown"],
      "resolve_agents": ["claude-code", "opencode", "agy", "codex"],
      "roles": {
        "PMO": ["claude-code", "opencode", "agy", "codex"],
        "PM": ["claude-code", "opencode", "agy", "codex"],
        "impl": ["claude-code", "opencode", "agy", "codex"],
        "rev": ["claude-code", "opencode", "agy", "codex"],
        "qa": ["claude-code", "opencode", "agy", "codex"]
      }
    },
    "local-admin": {
      "ops": ["list", "usage", "roles"],
      "resolve_agents": [],
      "roles": {
        "PMO": ["claude-code", "opencode", "agy", "codex"],
        "PM": ["claude-code", "opencode", "agy", "codex"],
        "impl": ["claude-code", "opencode", "agy", "codex"],
        "rev": ["claude-code", "opencode", "agy", "codex"],
        "qa": ["claude-code", "opencode", "agy", "codex"]
      }
    }
  },
  "keys": [
    {"key_id": "w1-tunnel-a", "principal": "w1-worker", "public_key": "<NODE_PUBKEY>", "status": "active"},
    {"key_id": "w1-tunnel-old", "principal": "w1-worker", "public_key": "<REVOKED_PUBKEY>", "status": "revoked"},
    {"key_id": "llmgwctl-local-admin", "principal": "local-admin", "public_key": "<LOCAL_PUBKEY>", "status": "active"}
  ]
}
```

### 8.5 resolve_agents 與 roles 的用法（e）

**root：** 不用（純讀名冊格式）。**lgwd 停止：** 不用。

`ops` 不含 `resolve` 時 `resolve_agents` 必須是空陣列；不含 `roles` 時 `roles` 必須是空物件，否則整份名冊被拒。`roles` 的格式是 `{角色: [agent ...]}`，角色取自 `lgwd/policy.py` 的 `ROLES`，agent 取自 `AGENTS`；未列在該 principal 的 `resolve_agents`／`roles` 的呼叫一律拒絕。

### 8.6 resolve 不能依角色限制（f）

**root：** 不用。**lgwd 停止：** 不用。

resolve 的請求不帶角色，名冊只能限制 agent（`resolve_agents`）。要依角色限制 resolve，需要 schema 2，不在本期範圍；不要把 role 字串當成可信來源。

### 8.7 cooldown 粒度粗（g）

**root：** 不用。**lgwd 停止：** 不用。

`cooldown` 是全有或全無，不能限定到某個訂閱。要每個訂閱各自的權限，需要 schema 2。

### 8.8 warn 到 enforce 與回退（h）

**root：** 要（改 env 檔）。**lgwd 停止：** 不用，每步都是 `restart`。

- 觀察期要涵蓋**所有已知呼叫方至少各一次 `outcome=pass`**（日誌可以看到已驗章的 principal），而且之後至少一個完整工作日沒有 `deny` 或 `forbidden`，才切 `enforce`。觀察期多長由維運依呼叫方清單決定，不能只看一個 60 秒窗口。
- 切換方法：把 `/etc/llmgateway/llmgateway.env` 的 `LGWD_AUTH_MODE` 改成 `warn` 或 `enforce`，再：
```bash
sudo llmgwctl restart
```
- 回退順序是 enforce → warn → off，每一步都 `restart`。**回退等於恢復免認證，不是安全措施**；若懷疑盜鑰，先撤銷並換鑰，不為便利降級。
- 降級會被告警：`check` 的 `auth-degraded` 看降級多久，從 `enforce` 降到 `warn` 或 `off` 滿 **3600 秒**（1 小時）就 fail，未滿是 todo（結束碼 2）。判定用 `/var/lib/llmgateway/auth-mode-history.json`（`root:root` 0644，一行 JSON，沒有祕密），root 每次跑 `check` 會依 systemd 的啟動時刻補記。
- **切模式要用 `llmgwctl restart` 才會被記錄**：模式只在重啟時改變，`start`／`restart`／`upgrade`／`rollback` 在服務起來且本機探針 ok 之後寫入；只改 env 檔而沒有重啟時，`check` 判「設定已改成 warn 但服務尚未重啟，目前仍是 enforce」。繞過 `llmgwctl`、直接 `systemctl restart` 的情況由 `check` 讀 `systemctl show -p ActiveEnterTimestamp --timestamp=unix` 補上（需要 systemd ≥ 247）。
- 刪除或替換歷史檔要有 root 或 `llmgw`（狀態目錄的擁有者）；`root:root` 只防得了直接改寫內容，`auth-audit.log` 也一樣。在降級中刪掉它，`check` 會判「未曾 enforce」而失去告警。每次模式轉換另外附加一筆到 `auth-audit.log`，歷史檔被刪了，轉換紀錄仍留在那裡。
- 結束碼 2 仍會照常重啟（見 8.1）：OSError 等暫時性錯誤重啟可能自己好。

### 8.9 回滾版本的副作用（i）

**root：** 要（`rollback`）。**lgwd 停止：** 不用（指令自己處理）。

`llmgwctl rollback` 或升級失敗自動切回時，會一併換回**該版本的 unit**（unit 是版本的一部分）。這些內部里程碑沒有對應的發行 tag（用 `git tag --contains <該版 commit>` 查是空的），所以看**版本目錄的靜態條件**辨認，不要打請求、也不要用請求確認（那會實際取得憑證）：

```bash
# D5d 之前：沒有這一行，78 會再度被反覆重啟
grep -q '^RestartPreventExitStatus=78' <版本目錄>/deploy/units/llmgateway.service
# D3a 之前（含 D2）：沒有授權判斷；只驗章、不授權，
# 任何已登記的 principal 都能做五種操作，包括本機管理 principal 拿到 resolve
test -f <版本目錄>/lgwd/auth_authz.py
# D2 之前：沒有認證模組，lgwd 不認得 LGWD_AUTH_*，等於 off 而且不會告警
test -f <版本目錄>/lgwd/auth.py
```

上面三個指令回非 0 就代表是那一種版本。`<版本目錄>` 是 `/opt/llmgateway/<tag>` 的版本目錄。

enforce 期間若要回滾到這些版本，先照 8.8 降到 `off`，並記錄風險。

### 8.10 新增鍵與輪替（j）

**root：** 要（install）。**lgwd 停止：** 不用。

雙鍵輪替：

1. `generation` ＋1，加新鍵（同一個 principal，`status` 為 `active`），install 回 0。
2. 客戶端換用新鍵。
2.5 在**客戶端**確認新鍵已經在用：每個使用這個 principal 的客戶端，設定裡的 key ID 都已換成新的，而且各用新鍵成功打過一次（`enforce` 下回 200；`warn` 下看 lgwd 日誌，這個 principal 有 `outcome=pass`、沒有 deny）。lgwd 日誌不記 key ID，伺服端無法分辨新舊鍵的流量。
3. `generation` ＋1，把舊鍵改成 `revoked`（**不得刪掉這一列**，墓碑要靠它），install 回 0。

```bash
sudo llmgwctl auth-registry check <檔>
sudo llmgwctl auth-registry install <檔>
```

### 8.11 撤銷與撤銷延遲（k）

**root：** 要（install）。**lgwd 停止：** 不用。

撤銷＝`generation` ＋1 並把該鍵改成 `revoked`，`install` 回 0 才算完成。用 `--no-wait` 或 `install` 逾時都**不得**宣告撤銷完成（它會印「名冊已寫入但服務尚未載入／載入失敗」）。

延遲：lgwd 每 2 秒查一次名冊檔，`install` 等載入確認最多 30 秒（預設 `--timeout 30`）。載入前已經通過驗章的請求照常完成。`warn` 下撤銷不會擋請求，只記 deny；`off` 下名冊不生效。

```bash
sudo llmgwctl auth-registry install --timeout 30 <檔>
sudo llmgwctl auth-registry status
```

### 8.12 不得用備份還原（l）

**root：** 要（修復指令）。**lgwd 停止：** 要。

任何一個庫都**不得**用備份檔還原：還原舊的防重放庫，等於讓已被截存的簽章可以再送一次；還原舊的信任庫，會讓墓碑與世代水位倒退。一律走 `recover-store` 或 `rebuild-trust`；這兩個指令印出「回退防護已失效」時，要人工核驗名冊與可信來源一致。

### 8.13 故障修復（m）

**root：** 要。**lgwd 停止：** 要；每個指令都要 `--confirm`，做完再手動 `start`。

| 症狀 | 指令 |
| --- | --- |
| 防重放庫壞了（`auth-stores`／啟動說 replay 庫不可用） | `sudo llmgwctl auth-replay recover-store --confirm` |
| 信任庫壞了（墓碑與水位要重建） | `sudo llmgwctl auth-registry rebuild-trust --confirm <已核驗名冊>` |
| 誤裝高世代（例如誤裝 generation 100，可信的是 12） | `sudo llmgwctl auth-registry reset-generation --confirm <已核驗名冊>` |
| 時鐘跳到未來（`auth-water` 報某把 key 的 H 在未來） | `sudo llmgwctl auth-replay reset-key-water --confirm --key <key ID>`（或 `--all-future`） |

修復完成後：
```bash
sudo llmgwctl start
```

`recover-store` 會把每把 key 的 H 設成 now＋60；`reset-key-water` 會印伺服器時間與新舊 H，供確認時鐘已校正。lgwd 啟動時拿不到鎖（另一個 lgwd 或寫入工具持有）會以 78 結束，而且**不會自動重試**（unit 的 `RestartPreventExitStatus=78`）。

### 8.14 維護期間的 check（n）

**root：** 不用（`check` 可非 root 跑，但 `auth-*` 需 root 才是確定結果）。**lgwd 停止：** 這是預期結果。

為了跑寫入指令而停掉 lgwd 時，`check` 的 `auth-heartbeat` 會 fail（心跳超過 180 秒），`auth-local` 也會失敗；這是預期的，啟動後恢復。`check` 的認證項目是 `auth-local`、`auth-heartbeat`、`auth-water`、`auth-stores`、`auth-busy`、`auth-degraded`；非 root 時 `auth-local`、`auth-water`、`auth-stores` 記 `todo`（結束碼 2），不報 fail。`auth-degraded` 的門檻是 **3600 秒**：從 enforce 降到 warn／off 滿 1 小時即 fail；切模式要用 `llmgwctl restart` 才會被記錄，刪除或替換歷史檔要有 root 或 `llmgw`（狀態目錄的擁有者），判定細節見 8.8。

`auth-busy` 讀 `.auth-busy.status` 的計數：enforce 下近 30 分鐘 StoreBusy 達 **5 次**記 fail；enforce 近 60 分鐘有 **1 次**以上、或 warn 近 60 分鐘有 1 次以上，記 todo（warn 只會 todo，不會 fail）。檔案缺失、壞檔或過期都記 todo，不記 fail。**todo 不會通知，只有 fail 會通知**（見 8.21）。判定與原因見 8.22。

### 8.15 policy 新增 agent 或角色時（o）

**root：** 要（install）。**lgwd 停止：** 不用。

`AGENTS` 與 `ROLES` 寫在程式裡（`lgwd/policy.py`）。新版本加了 agent 或角色時，名冊要先把它列進 `resolve_agents` 或 `roles` 並 install；否則 enforce 下用新 agent 的呼叫會 403。反過來，名冊列了舊版不認得的 agent，舊版 parser 會整份拒絕，所以回滾前也要先檢查名冊。

### 8.16 warn 的恢復（p）

**root：** 不用。**lgwd 停止：** 名冊與 `ssh-keygen` 不用；鎖與兩個庫要。

名冊（背景每 2 秒輪詢）與 `ssh-keygen`（stat 改變重跑自測）在執行中會自動恢復。鎖與兩個庫如果在**啟動時**就不可用，整個執行期維持「不可判定」，要 `restart` 才會再試。

### 8.17 停止與日誌（q）

**root：** 要（`stop`／`restart`）。**lgwd 停止：** 這一節就是停止行為。

停止（SIGTERM／SIGINT）時先不再接受新連線，最多等 20 秒讓已進來的請求做完，再關閉兩個認證庫並釋放鎖。閒置連線與只送了一半標頭的連線不會讓停止卡住，第二次 SIGTERM／SIGINT 也不會加速或打斷收尾：兩個訊號共用同一個 handler，而且從註冊後一路安裝到停止流程跑完、行程要退出之前，都不還原預設處置；重複的訊號只是再設一次停止事件。unit 的 `TimeoutStopSec=30` 不得小於 30 秒。

unit 另外設 `KillMode=mixed`：停止時只對主行程送 SIGTERM，等它收尾（最多 20 秒），**主行程結束後或期限到時**，才對 cgroup 裡剩下的行程送 SIGKILL。預設的 `control-group` 會同時殺掉在途的驗章子行程（`ssh-keygen`、`prlimit`），把停止那一刻正在驗章的請求變成 503，也讓 #263 的「最多等 20 秒」白做；`mixed` 不會留下孤兒。

handler 在啟動完成後才註冊：啟動期間（含 `ssh-keygen` 自測）收到 SIGTERM 會照預設行為立刻結束，鎖由作業系統釋放，SQLite 的 WAL 不受影響。

日誌：

- `auth-audit.log`（`/var/lib/llmgateway/auth-audit.log`）是 `root:root` 0600 的附加式歸責紀錄，一行一筆 JSON；root 改得了，所以不是不可竄改的證據。
- 寫入型 CLI 同時送 journal，ident 是 `llmgwctl-auth`、facility 是 `authpriv`；用 `journalctl -t llmgwctl-auth` 查。

### 8.17.1 資源上限

**root：** 要（unit 檔與 install）。**lgwd 停止：** 要（改完重啟才生效）。

unit 在 `[Service]` 段設三個資源上限。它們是**保護整台主機**的上限，不是貼著用量限流：目的是擋住失控（fork 洪水、記憶體洩漏、fd 洩漏），所以三個值都比實測高峰寬很多，off 模式與現行負載的用量都遠低於它們（#247 J-5）。`TasksMax` 與 `MemoryMax` 是 cgroup 層級，把 lgwd 和它所有的子行程一起算；`LimitNOFILE` 是每個行程各自的上限。

算式用 C-612 的低負載複測（`PM-C612-samples.csv`，5860 個樣本，`pytest_rc 0`）：lgwd 最多 37 個 task（pids 1、threads 高峰 37）、RSS 高峰約 38 MB、fd 高峰 89。

- `TasksMax=512`：下限估算 37（lgwd）＋16（驗章）＋約 15（用量抓取的 claude CLI）＋約 20（key-sync、gh）≈ 88，取兩倍約 176；512 約是實測高峰的 5.8 倍，同時讓沒有上限的連線執行緒實際停在約 400 條。systemd 預設的 `DefaultTasksMax` 是 pid 上限的 15%（通常是幾千），所以 512 是真的在擋，不是沒有作用。
- `MemoryMax=1536M`：最壞估算 lgwd 100 MB（連線多時）＋驗章 16×約 10 MB＋claude 約 300 MB＋key-sync、gh 約 80 MB ≈ 640 MB，約 2.4 倍取整到 1.5 GiB。**不能**只看 lgwd 的 RSS 高峰乘 2：C-612 實測 38 MB，乘 2 只有約 77 MB，第一次抓用量（`claude` CLI，off 模式也會跑）就會 OOM。
- `LimitNOFILE=4096`（單一值，soft＝hard）：實測 fd 高峰 89；連線數受 `TasksMax=512` 限制，需要 ≥512＋約 100，取 4096。systemd 服務預設的 soft 是 1024、hard 是 524288；設 4096 會把 soft 提高、hard 降低。子行程會繼承這個值（node 會把 soft 拉到 hard，也就是 4096，夠用）；`ssh-keygen` 由 `prlimit --nofile=32` 另外限制，不受影響。

碰到上限時會怎樣：`TasksMax` 用完時開不了新執行緒或新行程，該連線失敗，或驗章回 503，lgwd 本身不會掛；`MemoryMax` 用完時是 cgroup 內 OOM（page cache 也算在內，但會先回收）；`LimitNOFILE` 用完時 `accept()` 或開檔失敗（EMFILE）。

`MemoryMax` 這一段另外設了 `OOMPolicy=continue`。systemd 243 起沒設 `OOMPolicy` 時用 `DefaultOOMPolicy`，預設是 `stop`：cgroup 裡**任何一個**行程被核心 OOM 殺掉，systemd 就把**整個 unit** 停掉（結果是 `oom-kill`），再由 `Restart=on-failure` 重啟，正在處理的請求（連在途驗章）都會被斷。這對本服務不適合：`claude` 或 gh 只是週期性工作，不值得讓整個 Gateway 陪它重啟。改成 `continue` 之後，子行程（`claude` CLI、gh、`ssh-keygen`）被殺掉時只有那一次工作失敗（用量抓取跳過、驗章回 503），lgwd 照常服務；如果被殺的是 lgwd 主行程，行程結束後仍由 `Restart=on-failure` 重啟。實際行為在 V-0 確認（worker 上沒有 systemd 可實測）。

為什麼用 cgroup 保護整台而不是貼著用量：最大來源不是驗章，而是 off 模式也會跑的 `claude` CLI 與 keysync 的 gh，貼著 lgwd 的用量設會讓正常尖峰就撞上限。cgroup 的上限涵蓋所有子行程，某一個子行程失控也擋得住。

不用 `MemoryHigh`：它會在接近上限時讓整個 cgroup 變慢，驗章延遲可能讓請求逾時變成 503，比單純的上限更難診斷。等 V-0.6 有正式主機的高峰再考慮。

這三個值先合，V-0.6 再以 cgroup 的 peak 驗證（見 8.18）；高峰超過上限的一半就開 d 單調高，不要放寬到 `infinity`。

### 8.17.2 enforce 的磁碟與 IO 前提（#313）

**root：** 不用（這一節是部署前提與量測，不改設定）。**lgwd 停止：** 不用。

認證庫（信任庫與防重放庫）是**單寫者**：每次開啟都設 `journal_mode=WAL`、`synchronous=FULL` 並讀回驗證（`lgwd/auth_db.py:183`、`lgwd/auth_db.py:186`、`lgwd/auth_db.py:188`），交易一律明確 `BEGIN IMMEDIATE`（`lgwd/auth_db.py:255`），一庫一條連線、一把行程內 `threading.Lock`（`lgwd/auth_db.py:169`）。`threading.Lock` 不保證先到先得，排在後面的請求可能一直拿不到鎖。

等待期限：`busy_timeout` 預設 **5.0 秒**（`lgwd/auth_db.py:32`）。它是**共用的總期限**：行程內鎖的等待與 SQLite 的 busy 重試排在同一個期限裡（`lgwd/auth_db.py:220`）。

會變成 503 的有兩條路徑：

- **認證庫路徑**：排在單寫者鎖上的請求等滿 `busy_timeout`，StoreBusy 被 `except (StoreError, OSError)` 轉成 `unavailable`（`lgwd/auth.py:754`、`lgwd/auth.py:755`），enforce 回 **503 `auth_unavailable`**。
- **號誌路徑**：預驗章 framing 的號誌是 **32**（`lgwd/http.py:106`），取號誌最多等 **1 秒**（`lgwd/http.py:107`，`lgwd/http.py:416`）；等不到就以 `REASON_BUSY` 回 **503 `auth_unavailable`**（`lgwd/http.py:420`）。這一條與 `busy_timeout` 無關。驗章子行程的號誌是 **16**（`lgwd/sshsig.py:84`）。

兩條都是 **fail-closed**：不放行、不重放，客戶端可以重試。warn 模式只記錄、照舊處理。

32 條請求同時排在單寫者的鎖上，若再加上磁碟 fsync 壓力（同一顆磁碟上有重 fsync 的負載），尾端等待就可能超過 5 秒；號誌被佔滿時，新請求等滿 1 秒就會走 `REASON_BUSY` 的 503。

目前的證據（#310 調查，報告不在 repo 內；兩組都是 C-611 的 30 秒短版，不是 10 分鐘版）：

- 完整 HTTP＋`ssh-keygen`＋SQLite 路徑、32 路並發，開始前 load1 3.20：**268.1 筆/秒、等鎖 p99 0.553 秒**，零非 200。
- 同一條路徑，開始前 load1 36.95（外部共載）：**102.4 筆/秒、p99 0.988 秒**。這次時間支因共載 skip，數字取自 skip 訊息（`PM-RUN-305.txt:39`）。p99 離 1 秒只差 1.2%，**高負載下已貼近判準**。
- 單元路徑（32 條執行緒直接呼叫 accept），一份測試加 2 個 writer（Python 寫檔＋fsync 行程）：**StoreBusy 0～8 次、p99 1.73～2.56 秒**（n=3，三次都超過判準：StoreBusy 或 p99 ≥ 1 秒）。開始前 load1 只有 4.69，但跑期間被這些 writer 自己推到 27，所以這個樣本不能說是與負載無關，只能說磁碟壓力足以觸發。
- **部署機的磁碟種類與 fsync 延遲還沒有量過**。上面的數字不能直接當成部署機的前提，要在部署機照 8.18 的 V-0.6b 量。

上線前提（照這幾點做，不要只調數字）：

1. 認證庫所在的磁碟**不與重 fsync 的負載共用**（例如大量寫檔、資料庫、日誌）。做不到，就要先以 V-0.6b 量過，結果達標才開 enforce。
2. **不要只調大 `busy_timeout`**。理由涵蓋兩條路徑：StoreBusy 那一條只會把 503 換成更長的等待；拉長的等待又更久佔住 32 路的號誌，號誌滿了就更常走 `REASON_BUSY` 的 503。號誌那一條與 `busy_timeout` 無關，調大它只會讓這條 503 更常發生。
3. 量測未達判準：**先不開 enforce**（維持 warn），或先調號誌或期限，並把量測寫進部署紀錄。調整要開 d 單，不在正式主機上直接改。
4. 把 FIFO（公平）鎖換掉、或改成多連線／分片，會動到單寫者與 WAL 的契約（C-302、C-304），屬於 b／c 的設計變更，不在這一節的範圍。

### 8.18 上線前清單

**root：** 要（真機操作）。**lgwd 停止：** 視項目。

正式主機啟用 warn／enforce 之前，逐項完成（單次真機授權由本人給；V-0 的每一項都由 PMO 申請、PM 執行，P-D4 由本人或 PMO 做）：

| # | 項目 | 判準 |
| --- | --- | --- |
| V-0.1 | `ssh -V` 是 OpenSSH 10.2p1；用臨時金鑰重跑分類表的五種輸出（Good、incorrect、兩種 Could not verify、缺 principal） | 字樣與 design 相同；不同就停下，不擴充分類表 |
| V-0.2 | lgwd 以 warn 啟動時自測通過 | 日誌沒有 self-test 失敗，也沒有 `auth event=degraded` |
| V-0.3 | `prlimit` 存在，且以 llmgw 身分能對子行程設限 | — |
| V-0.4 | `systemd --version` ≥ 251（失敗通知的 `MONITOR_*` 需要；< 251 時腳本改用 `systemctl show` 的 `InvocationID`，要實測 fallback 有拿到那一次 check 的行）；確認 systemd 版本支援 `OOMPolicy`（243 以上）；`systemd-analyze verify` 乾淨 | `Type=exec` 與 unit 都有效 |
| V-0.5 | 壓測中執行 `systemctl stop` | 503 為 0；停止後 cgroup 沒有殘留行程 |
| V-0.6 | C-612 在正式主機重跑，量**兩組、分開標示**，判準用兩組的和。**A＝scope 組（主法，含客戶端的上界）**：用 `sudo systemd-run --scope` 包住整個 C-612（含 unittest 客戶端；所以是上界，不是 lgwd 本身），量該 scope 的 `pids.peak`／`memory.peak`；scope 在結束前取樣（cgroup 行程全部結束後就被移除，所以取樣與最後那次讀取都在 scope 之內）；scope 的 cgroup 路徑用 `systemctl show -p ControlGroup --value <scope>` 取得；lgwd 的 fd 高峰每秒取樣 lgwd 行程的 `/proc/<pid>/fd`。`pids.peak` 或 `memory.peak` 任一不存在（kernel < 5.19 時 `memory.peak` 沒有）就用取樣上界：每秒取樣該 scope 的 `memory.current`、`pids.current`，寫最大值（記取樣行程的 pid，結束時只 kill 那個 pid）；兩者不存在就都用取樣。**B＝服務 cgroup 基線**：讀 `/sys/fs/cgroup/system.slice/llmgateway.service/` 的 `pids.peak`／`memory.peak`（含它平常起的 `claude` CLI、keysync 子行程；任一不存在就每秒取樣 `memory.current`、`pids.current` 寫最大值）；期間實際觸發一次 keysync 與一次用量抓取（本人確認過才觸發）後讀取。這兩個 peak 是自服務啟動以來的累計最大值，紀錄要寫服務的啟動時間。為什麼 A 不是服務 cgroup：`tests/test_main_auth_rate_263.py:44` 的 `subprocess.Popen` 讓 C-612 的 lgwd 繼承 unittest 行程的 cgroup，**推論**是它不在服務 cgroup 裡（成立條件是 unittest 行程本身不在服務 cgroup 裡）；scope 方法不依賴這個推論。步驟與指令範本見下方「V-0.6 量測步驟」 | 判準不變，用 A＋B 的和比較（保守上界，不放寬）：`pids.peak` ≤ 256、`memory.peak` ≤ 768M、lgwd 的 fd 高峰 ≤ 2048；超過任何一項就開 d 單調高，不放寬到 `infinity` |
| V-0.6b | 認證庫 IO 前提（見 8.17.2；與 V-0.6 的 cgroup 量測並列，不取代）。在正式主機以 `LGW_SLOW_TESTS=1` 跑 C-612 的 10 分鐘版（`tests/test_main_auth_rate_263.py`，`stop.wait(600)`；repo 內的 C-611 只有 30 秒版，不算 10 分鐘版）。**跑之前**先把 `TMPDIR` 指到與認證庫（`/var/lib/llmgateway`）同一個檔案系統的目錄：C-612 把自己的庫建在 `tempfile.TemporaryDirectory()`，也就是 `$TMPDIR`（預設 `/tmp`），不是正式庫的目錄；`/tmp` 與認證庫可能不同檔案系統，也可能是 tmpfs（不 fsync 到磁碟，測出來漂亮但無意義）；出貨 unit 另有 `PrivateTmp=true`（第 7.2 節「出貨 unit 的加固」），服務行程看到的 /tmp 不是主機共用的那個，所以不能拿它的 /tmp 當代表。用 `findmnt -T /var/lib/llmgateway -no SOURCE,TARGET` 與 `findmnt -T <TMPDIR> -no SOURCE,TARGET` 比對，對不上就換目錄重跑；兩邊的結果都記入部署紀錄。期間記錄：認證庫所在檔案系統的 FSTYPE 與 OPTIONS（同一支 findmnt 加 `-no FSTYPE,OPTIONS`）；磁碟是否旋轉用 `lsblk -s -no NAME,ROTA <SOURCE>` 追到底層實體裝置（SOURCE 是 overlay 時不是區塊裝置，改用 upperdir 所在的掛載點重做 findmnt；LVM／dm 用 `lsblk -s` 追到底）。貼 OPTIONS 之前自己看一眼：overlay 的 OPTIONS 會帶 lowerdir／upperdir 的長路徑，不要原樣貼進紀錄。每秒取樣一次 `/proc/pressure/io` 的 `some`／`full` 的 `avg10`，記最大值；取樣行程要記下 pid，結束時只 kill 那個 pid（不用 pkill）。C-612 印出的 `statuses`（503 數）、`wait_count`、等鎖 `p50`／`p99`、`rate`，原樣記入部署紀錄。503 數是 StoreBusy＋號誌 `REASON_BUSY`＋驗章不可用的**總和，是上界**，不是 StoreBusy 數（#312 起 lgwd 另記 StoreBusy 計數，寫在 `.auth-busy.status`，見 8.22）。跑的期間每分鐘讀暫存 state dir 的 `.auth-busy.status`，結束前讀（暫存目錄結束即刪）。V-0.6 與 V-0.6b 可以共用同一次 C-612 執行、同一個 scope 同時取兩組量測（V-0.6b 的 IO 量測照常），授權以單次計；但兩組量的東西不同（V-0.6 量 C-612 的 scope，V-0.6b 量認證庫的 IO），部署紀錄要分開標示；scope 方法不依賴該推論（見 V-0.6） | 判準不變：接受率 ≥ 50 筆/秒、零假 503（零非 200）、等鎖 p99 < 1 秒（與 C-612 的三條斷言相同）；未達就先不開 enforce，或先調號誌／期限並把量測寫進部署紀錄；**不得只調大 `busy_timeout` 來過判準**；上界的 503 數不得當成 StoreBusy 數寫進紀錄 |
| V-0.7 | `ProtectSystem=strict` 下的檔案存取 | lgwd 讀得到 `/etc/llmgateway/auth-registry.json`、寫得了 `/var/lib/llmgateway/` |
| P-D4 | Mac 與 worker 映像的 `ssh -V`、`-Y sign` 的 180 bytes、ssh-agent、有密語時不卡住 | 由本人或 PMO 做 |

### 8.18.1 V-0.5：transient service 的 stop 驗證（#320）

**root：** 要（`systemd-run` 建 transient service）。**lgwd 停止：** 不停正式服務；只停 transient service `lgw-v0`。

V-0.5 不在正式服務上做：正式的 `llmgateway.service` 不停、mode 與名冊不改。做法是另起一個 transient service `lgw-v0`，它的 lgwd 用**假 keys**、**暫存名冊**、**暫存庫**，policy 是「所有用量抓取都關掉」的那一份（list／resolve 也可能觸發 `claude` 子行程，見 8.18 的 V-0 架構師補充）。負載來自 repo 內的外部埠客戶端 `tests.v0_load_client`，它只當客戶端，不起 lgwd。

**先決條件：** 目標 tag 另外 clone 到 `/var/lib/llmgateway-v0/src`（與認證庫同一個檔案系統；不碰 `/opt/llmgateway/current`），以 llmgw 身分操作，並記下 commit sha。

**屬性對照：** 下面的範本逐項對照 `deploy/units/llmgateway.service`（以那個檔為準；改了 unit，範本要跟著改，`tests/test_deploy_doc_auth.py` 會比對）。

| unit 屬性 | 範本 | 不同之處與理由 |
| --- | --- | --- |
| `User`／`Group`＝`llmgw` | 同 | — |
| `TasksMax`／`MemoryMax`／`LimitNOFILE` | 同（512／1536M／4096） | — |
| `OOMPolicy`＝`continue`、`KillMode`＝`mixed`、`TimeoutStopSec`＝30 | 同 | — |
| `NoNewPrivileges`、`ProtectSystem=strict`、`PrivateTmp` | 同 | — |
| `ReadWritePaths` | `/var/lib/llmgateway-v0` | 不同路徑，避免碰正式的 `/var/lib/llmgateway` |
| `Type=exec` | `--service-type=exec` | 同義 |
| `WorkingDirectory` | `/var/lib/llmgateway-v0/src` | 跑的是目標 tag 的 clone，不是 `current` |
| `Restart=on-failure` | `Restart=no` | 壓測中的 lgwd 若死掉，要留現場，不自動重啟 |
| `EnvironmentFile`、`LoadCredential` | 不設 | 假 keys 與埠直接由參數給；不碰 `/etc/llmgateway` |

```bash
systemd-run --unit=lgw-v0 --service-type=exec --working-directory=/var/lib/llmgateway-v0/src \
  --property=Restart=no \
  --property=User=llmgw --property=Group=llmgw \
  --property=TasksMax=512 --property=MemoryMax=1536M --property=LimitNOFILE=4096 \
  --property=OOMPolicy=continue --property=TimeoutStopSec=30 --property=KillMode=mixed \
  --property=NoNewPrivileges=true --property=ProtectSystem=strict \
  --property=ReadWritePaths=/var/lib/llmgateway-v0 --property=PrivateTmp=true \
  --setenv=PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m lgwd <貼上 v0_prepare 印出的 lgwd-args 那一行>
```

**步驟：**

1. 建空目錄 `/var/lib/llmgateway-v0/run`（用 `mkdir` 新建，不重用舊的）。
2. 準備：在 `/var/lib/llmgateway-v0/src` 下執行 `PYTHONDONTWRITEBYTECODE=1 python3 -m tests.v0_prepare --dir /var/lib/llmgateway-v0/run --mode enforce --port <埠>`。它印出三行：`lgwd-args:`（貼進上面的 `systemd-run`）、`client-env:`（客戶端要的環境變數名，只有名字沒有值）與 `client-key-id:`（簽章金鑰的 ID，記進部署紀錄；客戶端不靠它給參數，它寫死在 `tests/v0_prepare.py`）。埠要是 127.0.0.1 上沒有別人在用的埠。輸出不含金鑰內容；`keys.env` 與私鑰是 0600。`v0_prepare` 會多給 `--auth-per-key-max 1000000`（與 C-612 同值）：出廠 600 是 65 秒視窗（約 9 筆／秒），負載會被 429 擋掉，量到的就不是 V-0.5 要的東西。stderr 另會印一行 `note: registry.json must be owned by root …` 提醒，它不是 stdout 的一部分，stdout 仍是三行。
   - **名冊擁有者（啟動前必做）**：執行 `sudo chown root:root /var/lib/llmgateway-v0/run/registry.json`。lgwd 要求名冊由 root 擁有；`v0_prepare` 產生的檔屬於執行者（llmgw），不改的話 warn 會降級，enforce 會因 `insecure_file` 以 exit 78 結束。**檔案**不得 group／world 可寫（0644 即可）；lgwd 只檢查名冊檔（擁有者、權限、一般檔、非 symlink），不檢查所在目錄。目錄權限另是安全建議（能寫目錄的人可以換掉檔案，建議目錄也不給 group／world 寫），不是 lgwd 的判準。若 lgwd 仍報 `insecure_file`，先看檔案的擁有者與權限。
3. 啟動：貼上面的 `systemd-run`；等 `127.0.0.1:<埠>` 的 `/healthz` 回 200 之後才開負載（這一步只確認服務起來，不拿它當驗收）。起來後看 journal（`journalctl -u lgw-v0 --no-pager`）：**不得出現 `auth event=degraded`**（warn 降級，area=registry）。**停損**：出現 `auth event=degraded`，或 exit 78（`cannot start authentication`），就停：取 journal 原樣回報，不開負載、不改權限硬過。
4. 負載（分三行。不寫成 `cd … && … &`：那樣 `$!` 會是子 shell，不是客戶端）：

   ```bash
   cd /var/lib/llmgateway-v0/src
   LGW_V0_DIR=/var/lib/llmgateway-v0/run LGW_V0_PORT=<埠> PYTHONDONTWRITEBYTECODE=1 python3 -m tests.v0_load_client --duration 120 --concurrency 32 > /var/lib/llmgateway-v0/load.json 2> /var/lib/llmgateway-v0/load.err &
   LOAD_PID=$!
   ps -p "$LOAD_PID" -o cmd=
   ```

   `ps` 的輸出必須是 `python3 -m tests.v0_load_client …`；如果是 `bash`／`sh` 或別的東西，就停下、不 kill 任何東西，回報。停的時候只 kill 這個 `LOAD_PID`（不用 pkill）。（推論：單一簡單指令前面加環境變數時，`$!` 是 python 本身；未在真機驗，所以用 `ps` 確認。）
5. 負載跑約 60 秒後，先記下停止開始的時刻 `STOP_EPOCH=$(date +%s.%N)` 與 `T0=$(cut -d' ' -f1 /proc/uptime)`，再執行 `systemctl stop lgw-v0`。
6. 等客戶端自己結束（它在 120 秒時結束，或收到 SIGTERM 時乾淨結束並印出 JSON）。讀 `load.json`。

**三項斷言（全部成立才算 V-0.5 通過）：**

1. **期間無 503，切斷類錯誤只能出現在停止之後**：`load.json` 的 `statuses` 沒有 `"503"` 鍵；`connection_refused` 單獨計，停止後的新連線被拒**不算** 503。`load.json` 的 `errors` 原樣寫進部署紀錄（整個物件，不只挑幾項）。另看 `connection_reset`（`ConnectionResetError`）與 `bad_status_line`：它們在 `error_window` 裡的 `first`（epoch 秒）**不得早於** `STOP_EPOCH`；之後出現的才算停止過程中的正常現象。停止之前出現任一項即未達。
2. **主行程 20 秒內結束**：用單調時間差算。`(systemctl show lgw-v0 -p ExecMainExitTimestampMonotonic --value 的值 ÷ 1000000) − T0 ≤ 20` 才算過（微秒值，自開機起算；`T0` 是步驟 5 停止前讀的 `/proc/uptime` 第一欄，也是自開機起算的秒數）。值為 `0` 表示主行程沒有結束過，視為未達。`TimeoutStopSec=30` 是上限，超過就是停損。（推論：`/proc/uptime` 與 systemd 的單調時間同一個起點；未在真機驗。若真機差距明顯，改用 `InactiveEnterTimestampMonotonic` 同樣取法，並把差異記入紀錄。）
3. **cgroup 沒有殘留**：`cat /sys/fs/cgroup/system.slice/lgw-v0.service/cgroup.procs` 為空；**檔案不存在**（unit 已 inactive、cgroup 已移除）也算空；或 `systemctl is-active lgw-v0` 是 `inactive`。檔案存在且有 pid 即未達。

**停損：** 任一項未達，就**不要**改正式服務，照下面的清理做，把原樣輸出與未達的那一項寫進部署紀錄。不得為了過斷言而放寬 `TimeoutStopSec`、改 `KillMode`，或把連線被拒算成 200。

**清理：** kill 自己記下的客戶端 pid；`systemctl stop lgw-v0`（若還在）；確認 `systemctl is-active llmgateway` 仍是 `active`、`/healthz` 仍是 200、正式的 mode 與名冊 sha 與 S0 記錄相同；刪除 `/var/lib/llmgateway-v0/run` 與 `/var/lib/llmgateway-v0/load.*`（刪之前先看一眼路徑，確認是這一次建的）。

**沒有驗到的：** 這一節的工具（`v0_prepare`、`v0_load_client`）與範本的屬性對照，在 repo 裡以離線測試驗過（真起 lgwd 子行程，見 `tests/test_v0_tools.py`）。**transient service 在正式主機上的行為**（`systemd-run` 的屬性是否全部生效、stop 時主行程的實際結束時間、cgroup 的殘留）沒有實測；以 V-0.5 的真機結果為準。

#### V-0.6 量測步驟（scope 方法）

**root：** 要（`systemd-run --scope`、讀 cgroup、root 寫取樣檔）。**lgwd 停止：** 不用（C-612 自己起 lgwd；B 那一組在服務照跑時量）。

**未在真機驗證**：沙箱沒有 systemd，下面的語法依 systemd 已知行為寫（推論）。第一次執行時逐步確認；任一步不如預期就停下回報，不換量法硬過。

這一節的 `<repo>` 是另 clone 的目標 tag（不用 `/opt/llmgateway/current`），`<TMPDIR>` 是與 V-0.6b 同一套檢查後選定的目錄（llmgw 可寫）。取樣檔與 unittest 輸出都寫在 `<TMPDIR>`。

**順序很重要**：步驟 4 的 `systemd-run` 是前景指令，會阻塞約 10 分鐘。所以 A 取樣（步驟 2）、B 取樣與「前一次讀 peak」（步驟 3）要在步驟 4 **之前**起來；keysync 與用量抓取（步驟 5）在步驟 4 **執行期間**從另一個終端觸發；步驟 6 在步驟 4 結束後、收尾之前再讀一次 B 的 peak。步驟 2 起來後**立刻**跑步驟 4（中間不要停）：A 取樣只等 scope 出現 `A_WAIT` 秒（預設 60），超過就寫錯誤並結束，之後的 scope 就沒有取樣。

0. **取 repo 並記 sha**：

```bash
git clone --depth 1 --branch <tag> https://github.com/FATESAIKOU/LLMGateway.git <repo>
git -C <repo> rev-parse HEAD
```

1. **TMPDIR**：照 V-0.6b，`findmnt -T /var/lib/llmgateway -no SOURCE,TARGET` 與 `findmnt -T <TMPDIR> -no SOURCE,TARGET` 對得上才用；建好並讓 llmgw 可寫：`sudo install -d -o llmgw -g llmgw <TMPDIR>`。先 `sudo -v` 把 sudo 憑證快取起來，後面的背景 `sudo` 才不會卡在密碼。

2. **A 組取樣**（在 scope 之前，背景）。整段在 `sudo sh` 裡跑，取樣檔也由它寫，所以不靠呼叫者的寫入權限。等 scope 出現最多 `A_WAIT` 秒（預設 60；要留更久就先 `export A_WAIT=300`）；起不來就寫一行錯誤並結束。每秒記 `memory.current`、`pids.current`，以及每個 lgwd 行程的 fd 數；結束時寫 fd、memory、pids 的最大值（取樣上界）。scope 不見了就自己結束。`echo "A: pid=$$"` 會把取樣行程的 pid 寫進檔，收尾時只 kill 那個 pid。

```bash
SCOPE=lgw-v06-c612-$(date +%s).scope
OUT=<TMPDIR>/v06-samples.txt
sudo sh -c '
  i=0; CG=""
  while [ $i -lt "$3" ]; do CG=$(systemctl show -p ControlGroup --value "$1"); [ -n "$CG" ] && [ -d /sys/fs/cgroup$CG ] && break; i=$((i+1)); sleep 1; done
  if [ -z "$CG" ] || [ ! -d /sys/fs/cgroup$CG ]; then echo "A: scope $1 not started within $3s" >> "$2"; exit 1; fi
  echo "A: pid=$$ scope=$1 cg=$CG" >> "$2"
  fdmax=0; memmax=0; pidsmax=0
  while [ -d /sys/fs/cgroup$CG ]; do
    for p in $(cat /sys/fs/cgroup$CG/cgroup.procs 2>/dev/null); do
      grep -q lgwd /proc/$p/cmdline 2>/dev/null || continue
      n=$(ls /proc/$p/fd 2>/dev/null | wc -l); [ "$n" -gt "$fdmax" ] && fdmax=$n
      echo "$(date +%T) A lgwd_pid=$p fd=$n" >> "$2"
    done
    m=$(cat /sys/fs/cgroup$CG/memory.current 2>/dev/null || echo 0); [ "$m" -gt "$memmax" ] && memmax=$m
    q=$(cat /sys/fs/cgroup$CG/pids.current 2>/dev/null || echo 0); [ "$q" -gt "$pidsmax" ] && pidsmax=$q
    echo "$(date +%T) A mem=$m pids=$q" >> "$2"
    sleep 1
  done
  echo "A: max fd=$fdmax mem.current=$memmax pids.current=$pidsmax（取樣上界）" >> "$2"' sampler "$SCOPE" "$OUT" "${A_WAIT:-60}" &
```

3. **B 組：服務 cgroup 基線取樣，與「前一次讀 peak」**（在步驟 4 之前，背景）。取樣在 `sudo sh` 裡，每秒記 `memory.current`、`pids.current`；收到 TERM 時寫最大值，然後結束。它跑到被 kill 為止，pid 寫在檔裡。`pids.peak`／`memory.peak` 任一不存在，該欄就用這組的最大值。起來之後先讀一次服務 cgroup 的 peak（前一次），這一行是步驟 6 的對照基準。

```bash
SVC=/sys/fs/cgroup/system.slice/llmgateway.service
sudo sh -c '
  memmax=0; pidsmax=0
  trap "echo \"B: max mem.current=\$memmax pids.current=\$pidsmax（取樣上界）\" >> \"\$2\"; exit 0" TERM
  echo "B: pid=$$ svc=$1" >> "$2"
  while [ -d "$1" ]; do
    m=$(cat "$1/memory.current" 2>/dev/null || echo 0); [ "$m" -gt "$memmax" ] && memmax=$m
    q=$(cat "$1/pids.current" 2>/dev/null || echo 0); [ "$q" -gt "$pidsmax" ] && pidsmax=$q
    echo "$(date +%T) B mem=$m pids=$q" >> "$2"
    sleep 1 & wait $!
  done' sampler-b "$SVC" "$OUT" &
cat $SVC/pids.peak $SVC/memory.peak 2>/dev/null
```

4. **C-612 跑在 A 組 scope 裡**（前景，約 10 分鐘，阻塞；步驟 2、3 已起來之後才跑）。外層 `sh` 也在 scope 裡，測試結束後它還能讀到 scope 的 `pids.peak`／`memory.peak`，然後才退出（scope 一結束 cgroup 就沒了）。`-p` 只開 accounting，**不設** `MemoryMax`／`TasksMax`，否則會把量到的高峰壓成上限。scope 不套服務層的 `User=`、`ProtectSystem=`、`PrivateTmp=`（推論，未實測），所以身分用 `runuser -u llmgw` 換、TMPDIR 用 `env` 指定；這些屬性的實際行為歸 V-0.7 與 transient service（#320）。unittest 的輸出寫進 `<TMPDIR>/c612-unittest.txt`。

```bash
sudo systemd-run --scope --unit="$SCOPE" \
  -p MemoryAccounting=yes -p TasksAccounting=yes -p IOAccounting=yes \
  sh -c 'cd <repo> && runuser -u llmgw -- env TMPDIR=<TMPDIR> LGW_SLOW_TESTS=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_main_auth_rate_263 >> <TMPDIR>/c612-unittest.txt 2>&1; rc=$?
  echo "ControlGroup=$(systemctl show -p ControlGroup --value "$1")"
  CG=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); echo "scope=$CG"
  cat $CG/pids.peak $CG/memory.peak 2>/dev/null || echo "peak 檔有缺：該欄改用 A 組取樣上界"
  exit $rc' sh "$SCOPE"
echo "systemd-run exit=$?"
```

判讀 unittest：`c612-unittest.txt` 最後一行要是 `OK`，而且沒有 `skipped=`；出現 `skipped`（沒設 `LGW_SLOW_TESTS=1`）或 `FAILED` 這次作廢。

5. **觸發 keysync 與用量抓取**（在步驟 4 執行期間，**另開一個終端**，同一台主機）。各觸發一次，時間點記下來。**觸發的指令本文件目前沒有定義，未知**：要 PMO 或本人指定，而且要本人確認過才觸發，PM 不自己猜。

6. **收尾前再讀 B 的 peak**（步驟 4 結束後、kill B 之前；服務 cgroup 還在）。與步驟 3 的「前一次」一起記下來，兩次之差看的是步驟 5 的影響。

```bash
cat $SVC/pids.peak $SVC/memory.peak 2>/dev/null
```

讀到的 peak 是自服務啟動以來的累計最大值；紀錄要寫服務的啟動時間（`systemctl show -p ActiveEnterTimestamp --value llmgateway.service`）。要乾淨數字需要本人決定是否重啟服務，PM 不自行決定。

7. **收尾**：
- B 組：`sudo kill <B 的 pid>`（從 `OUT` 的 `B: pid=` 行取；只 kill 這個 pid），它會寫入最大值後結束。
- A 組：`systemctl is-active "$SCOPE"` 應該是 inactive，A 組取樣行程應已自行結束；還在跑的話，`sudo kill <A 的 pid>`（從 `A: pid=` 行取）。
- 讀 `OUT` 與 `c612-unittest.txt` 原樣記入部署紀錄。

8. **紀錄要寫的值**：repo tag 與 sha（步驟 0）；`ControlGroup=` 與 `scope=` 原樣（步驟 4）；A 的 `pids.peak`、`memory.peak`，缺的欄用 A 組上界；lgwd 的 fd 高峰用 A 組 log 的 `max fd`（每秒一次的上界，取所有 lgwd pid 的最大值）；B 的步驟 3 與步驟 6 兩次 peak、B 組上界、服務啟動時間，以及步驟 5 的觸發時間。兩組分表列，不加總顯示；判準用 A＋B 的和（見上表）。

### 8.19 定時檢查（timer）（r）

**root：** 要（timer 以 root 跑 `check`）。**lgwd 停止：** 不用。

`deploy/units/` 有三個 unit：`llmgateway-check.service`（`Type=oneshot`、`User=root`、跑 `/usr/local/bin/llmgwctl check`）、`llmgateway-check.timer`（開機 5 分鐘後第一次，之後每 15 分鐘一次）與失敗通知的 `llmgateway-check-notify.service`（#296；見 8.21）。service 沒有 `[Install]` 段，由 timer 啟動；只 enable／disable timer。install 子指令會把這些檔裝進 `/etc/systemd/system/`，並在 `llmgateway.service` 啟動之後對 timer 做 `enable --now`；重跑 install 是冪等的（timer 已在跑就不動它）。

為什麼要 root：`check` 的 `auth-local`（讀 `root:root` 0600 的管理私鑰簽章）、`auth-water`／`auth-stores`（`llmgw` 0600 的兩個庫）與 `auth-degraded` 的補記（root 寫歷史檔）都需要 root；非 root 跑會全是 `todo`，等於沒有告警。這與本人手動跑 `sudo llmgwctl check` 是同一份程式、同一個信任基礎。unit 另外加固：`NoNewPrivileges=true`、`ProtectSystem=strict`（只開 `/var/lib/llmgateway` 可寫）、`PrivateTmp=true`、`ProtectHome=read-only`；不設 `KillMode`（oneshot 逾時就整組殺掉，不會留下 `gh`／`ssh` 的孤兒）。

**第一次從舊版升級後要再跑一次 install。** 升級是由「目前在跑的那一版」的 `llmgwctl` 執行，舊版的 unit 清單沒有 timer，所以升級本身不會裝它：

```bash
sudo llmgwctl install --tag <tag>
```

新版 `check` 的 `check-timer`（這一版的 `deploy/units/` 有 timer 檔時才列）會在 timer 不是「enabled 而且 active」時報 `todo`，訊息分成「未安裝」（附 `install` 指令）、「已安裝但未啟用」（附 `systemctl enable --now llmgateway-check.timer`）與「已安裝但沒在跑」（附 `systemctl start llmgateway-check.timer`）；`systemctl` 不能用時該項也是 `todo`（無法確認）。照上面的指令補跑一次就會變 `ok`。

結束碼與告警：`check` 有 `fail` 時結束碼 1，unit 進入 failed，`systemctl --failed` 與 `systemctl status llmgateway-check.service` 看得到；只有 `todo` 時結束碼 2，unit 的 `SuccessExitStatus=2` 把它視為成功，不會反覆 failed。每 15 分鐘一次，journal 的量不大。

journal：`SyslogIdentifier=llmgwctl-check`，查 `journalctl -t llmgwctl-check`。unit 設了 `LLMGW_CHECK_JOURNAL=1`，`check` 會依等級在每行前面加 syslog 優先級（`fail`→`<3>`、`todo`→`<4>`、`ok`→`<6>`），所以：

```bash
journalctl -t llmgwctl-check -p warning
```

只看 `todo` 與 `fail`。手動跑 `check` 沒設這個變數時，輸出與以前逐 byte 相同；舊版的 `check` 不認得這個變數，會忽略它（回滾不需要換 unit）。

維護期間：為了跑寫入指令而停掉 lgwd 時，timer 照樣會跑，`check` 會 fail（心跳逾時、`auth-local` 失敗），unit 進入 failed 是預期的，服務啟動後會自己恢復。要避免這段 failed 狀態，維護前停 timer、結束後再啟動：

```bash
sudo systemctl stop llmgateway-check.timer
sudo systemctl start llmgateway-check.timer
```

回滾：unit 是版本的一部分。`llmgwctl rollback` 回滾到**沒有**這些 check unit 的版本時，會先對 timer 做 `disable --now`、再刪檔、`daemon-reload`（不會留下一個指向已刪 unit 的計時器）；notify unit 同理被移除（它沒有 `[Install]`，`disable --now` 對它是 no-op）。回滾到**有** timer 的版本時照裝並 enable（已在跑就不動）。從 v0.1.x 的 user unit 遷移時，`cutover` 會在新服務健康之後、跑 `check` 之前 `enable --now` timer（不然 `check-timer` 的 todo 會讓遷移被判停損）；`rollback-to-user` 與停損回滾會對這兩個 check unit 做 `disable --now`（timer 先於 service，**不刪 unit 檔**，與 `llmgateway.service` 的回退處理一致），所以從 `reverted` 直接再跑 `cutover` 時會重新 enable；若回到舊版後不再切回新版，這兩個 unit 檔留在 `/etc/systemd/system` 但不會執行。

### 8.20 從 MLP 清單同步名冊（外圍腳本 `lgw-registry-sync`，#292）

**root：** 要（寫候選檔與審計、跑 `check --against-trust`）。**lgwd 停止：** 不用。

外圍腳本 `ops-scripts/lgw-registry-sync` 把 MLP#1739 的公鑰清單轉成 LGW 名冊候選檔；**v1 只做 plan**，不自己 install。清單由執行的人用自己環境裡的唯讀 `gh` 取得（腳本不碰 token、不讀 `/run` 的憑證檔）：

```bash
gh api <MLP 取清單的路徑> > /root/mlp-keys.json
sudo ops-scripts/lgw-registry-sync plan --list /root/mlp-keys.json \
    --out /root/auth-registry.candidate.json
```

輸入是 MLP#1739 的 JSON 陣列；`version` 必須是 1、`name` 不是 `?` 且符合 `[A-Za-z0-9][A-Za-z0-9._-]{0,46}`、`status` 是 `ok`／`invalid`／`duplicate`。`status=ok` 的那一筆會重算 fingerprint，不符就整份停。`duplicate`、`?`、同一把公鑰出現在兩個 name、或同一個 name 出現兩次：整份停，不產生候選檔。`status=invalid` 的那一台不新增、不撤銷、不換鑰，摘要列 `skip <name> (invalid)`。

轉換規則（只動 `mlp-` 開頭的 principal 與 key）：

- principal 是 `mlp-<name>`、key ID 是 `mlp-<name>-<fp12>`（`fp12` 是公鑰 blob 的 SHA-256 前 12 個 hex）。非 `mlp-` 的 principal 與 key、以及本機管理 principal，原封不動。
- 同一個 name 換了新公鑰＝**換 key ID**：新的 active 進名冊，舊的標 `revoked`（那一列與 principal 都保留，墓碑要靠它）。清單出現名冊裡已撤銷的公鑰（例如重放舊清單）→ 停。
- 名冊裡有、清單裡**不見了**的 `mlp-<name>`：**不自動撤銷**，只列 `missing`。要撤銷必須另外點名（被點名的 name 必須真的不在清單裡）：

```bash
sudo ops-scripts/lgw-registry-sync plan --list /root/mlp-keys.json \
    --out /root/auth-registry.candidate.json --revoke w4
```

- 新的 name 預設上限 2；一次加入更多要人工確認後用 `--allow-new N` 放行。
- 基準是**目前已載入**的名冊（檔案雜湊要等於 `auth-registry status` 的 `loaded_hash`，且 `loaded_generation` 要等於名冊的 generation；腳本檢查的就是這兩項）；有變更時世代＋1，沒有變更就不產生候選檔（回 0）。
- 候選檔一定先過 `check --against-trust`（回 0 才算 plan 成功），先寫暫存檔、驗過才 rename 成 `--out`（root 0640）。腳本寫一筆 `auth-audit.log`，狀態檔只記上次輸入的雜湊與時刻。摘要會印候選檔的內容雜湊（`candidate generation <N> (sha256:…)`）；install 前用 `check` 印出的 `content_hash` 再對一次，避免 plan 與 install 之間被換檔。
- **正式機不要用 `--llmgwctl` 覆寫**：`check` 的判定必須來自正式安裝的 `llmgwctl`（sudo 預設也會清掉 `LGW_SYNC_LLMGWCTL`）；這個旗標只給沒有 wrapper 的 checkout 與離線測試。
- **每次用新的 `--out` 檔名，或先刪舊檔**：「無變更」與失敗時不會刪掉舊的候選檔；拿上一次留下的檔案去 install 會被 `check` 的雜湊比對或 `generation_not_increased` 擋下，但不要靠它。

**warn 期先人工，PMO 核可後才 install**：v1 沒有自動 install。看摘要裡的 `add`／`replace`／`revoke`／`missing`，**每一筆 add 與 replace 都要做 fingerprint 帶外比對**（由該 worker 的 PM 或 sidecar 回報 `ssh-keygen -lf` 的輸出，與摘要逐字相同），再請 PMO 核可，然後：

```bash
sudo llmgwctl auth-registry check --against-trust <候選檔>
sudo llmgwctl auth-registry install <候選檔>
```

`install` 回 0 才算完成。若 plan 之後別人先裝了別的名冊，install 會以 `generation_not_increased` 回 1；重跑 plan 再核可，不要手改世代。**不提供「撤回上一次」**：撤銷是不可逆的（墓碑規則），誤撤銷的 worker 只能換一把新鑰重新加入。名冊也不得用備份還原（8.12）。

#### 首次使用（seed 名冊之後的第一次同步，#337）

名冊剛換成 seed（`generation 1`、只含 `local-admin`）時，第一次 plan 的規則：

- **基準要先載入**：`plan` 會先確認 `auth-registry status` 的 `loaded_hash` 等於現行檔案雜湊、`loaded_generation` 等於名冊的 generation；seed 沒 install、或 lgwd 沒在 warn 下載入，plan 會以 `registry_not_loaded` 停（不產生候選檔）。
- **新 name 數量**：首次幾乎全部是新 name。預設上限 2，依 #337 的規劃首次約 9 台，要本人確認台數後才以 `--allow-new <確認的台數>` 放行；不要為了省事寫一個大數字。
- **取清單**（MLP#1739，確切方法由 MLP 提供）：在有 MLP repo Variables: read 權限的環境（Mac 或 provider）執行下面指令，**不能在 worker 上跑**（worker 低權 token 是 403，設計如此）：

  ```
  gh api 'repos/FATESAIKOU/MyLinuxPool/actions/variables?per_page=100' --paginate | <MLP 工作副本>/scripts/list-tunnel-keys.sh --json
  ```

  輸出是 JSON 陣列（`version`、`name`、`public_key`、`fingerprint`、`source`、`status`）；`duplicate`、`invalid` 不要匯入（`duplicate` 會讓 sync 整份停，`invalid` 只會被 skip）。`worker` 重建會換隧道鑰，同 name 換鑰由「換 key ID、舊的標 revoked」處理；worker 被刪除時 MLP 不通知 LGW，撤銷要靠明確 `--revoke <name>`（MLP 的 `--since` 輸出 `removed` 可協助找）。`SIDECAR_SIGNERS`（MLP#1529）是另一條，不混進來。
- **key ID 規則（sidecar 簽章必須逐字相同，#337 補充 2）**：principal＝`mlp-<name>`；key ID＝`mlp-<name>-<fp12>`。`<name>` 是清單的 `name` 原樣（不改大小寫、不加減前綴）；`fp12` 是公鑰 SSH blob 的 SHA-256 前 12 個**小寫** hex。等價算法：清單的 `fingerprint`（`SHA256:<b64>`）去掉 `SHA256:` 前綴、補上 `=` 到 4 的倍數、base64 解碼，取 hex 前 12 個。名冊的 key ID 合法字元是 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`（≤64）；`mlp-`（4）＋ name（≤47）＋ `-`（1）＋ 12 剛好 64。sidecar 必須在簽章當下由**自己現在的公鑰**算 key ID，不得寫死；換鑰＝換 key ID，舊的標 `revoked`。

  測試向量（`tests/test_deploy_doc_auth.py` 用 `ops-scripts/lgw-registry-sync` 的 `_parse_public_key`、`_fp12` 實算，向量一變測試就紅）：

  | 項目 | 值 |
  | --- | --- |
  | public_key | `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAZ6BM9XEM+FOeS1stgh0YAH2U4YdfTdx/6yS6w1B/+O` |
  | fingerprint | `SHA256:vzs1gYCu8dp09iou3qFMOfppx1ZZX82uZvIuNyPizmU` |
  | fp12 | `bf3b358180ae` |
  | name | `mlp-fh-l-default-45799726300` |
  | principal | `mlp-mlp-fh-l-default-45799726300` |
  | key ID | `mlp-mlp-fh-l-default-45799726300-bf3b358180ae`（45 字元） |

- **順序**：plan → 每一筆 add 做 fingerprint 帶外比對 → PMO 核可 → `check --against-trust` → `install`；完整步驟見 `docs/auth-rollout.md` §5 第 4 步。

### 8.21 check 失敗時通知 PMO（失敗通知 unit，#296）

**root：** 要（`sudo llmgwctl install --tag <tag>`）；日常通知以 `llmgw` 身分跑。**lgwd 停止：** 不用。

`llmgateway-check.service` 的 `[Unit]` 段有 `OnFailure=llmgateway-check-notify.service`：只有 check **進入 failed** 才觸發（結束碼 1＝有 fail、逾時、被訊號殺掉）；只有 todo 時結束碼 2 由 `SuccessExitStatus=2` 算成功，不會通知。`llmgateway-check-notify.service` 是 `Type=oneshot`、`User=llmgw`、`SupplementaryGroups=systemd-journal`（要讀 check 那一次的 journal）、`TimeoutStartSec=60`；它沒有 `[Install]`（不單獨 enable，也不需要 enable）、沒有再觸發通知或重試的設定——通知失敗不會變成另一種失敗。安裝（`install --tag`）與升級（`upgrade --tag`）都會跟其他 unit 一起裝它，回滾到沒有它的版本時一起移除。

設定不進 repo：放 `/etc/llmgateway/check-notify.env`（root:root 0644）。範本是版本目錄的 `deploy/check-notify.env.example`：

```bash
sudo install -m 0644 /opt/llmgateway/current/deploy/check-notify.env.example \
    /etc/llmgateway/check-notify.env
sudoedit /etc/llmgateway/check-notify.env
```

| 鍵 | 說明 |
| --- | --- |
| `LGW_NOTIFY_CHANNEL` | aicomm 的頻道（`send --channel`） |
| `LGW_NOTIFY_TO` | 收件人（`send --to`） |
| `ANGAREION_IDENTITY` | 送件身分（aicomm 讀的環境變數） |
| `LGW_NOTIFY_LABEL` | 這一台的代號，會填進訊息（1～32 字元；只收英數與 `. _ -`，格式不對就當成沒設：腳本跳過不送、`check-notify` 是 `todo`） |

這些值都不是祕密，也不得放 token、金鑰或身分檔的路徑。**值仍是範本的 `place-holder-*` 時視為未設定**（去頭尾空白、不分大小寫；複製範本後忘了改就是這種情況）：`check` 的 `check-notify` 會是 `todo` 並提示是哪一個鍵還是佔位值（只寫鍵名、不印值），通知腳本則與「設定不存在」相同——寫一行 `notify skipped: not configured`、結束碼 0、不送訊息。`check` 的 `check-notify` 項目（只在這一版有 notify unit 檔時才列）在三個鍵齊全、`LGW_NOTIFY_LABEL` 符合 `[A-Za-z0-9._-]{1,32}`、`PATH` 上找得到 `aicomm` 時是 `ok`，否則 `todo`（缺哪一項就寫哪一項）——它不會 `fail`，也不送訊息。

訊息只有這幾段（一行、白名單字元、最長 300 字元；不含主機名、位址、路徑、帳號、金鑰或 fail 行的說明文字）：

```text
llmgateway-check failed label=<本機代號> result=<類別> items=<項目名,…|-> at=<UTC 時間>
```

`items` 只取 `llmgwctl-check` 的 fail 行開頭 `fail <項目名>`（例如 `auth-degraded`、`list-local`），括號裡的說明一律丟掉；最多 10 項，其餘以 `+N` 表示。`result` 只認 `exit-code`、`timeout`、`signal`、`core-dump`、`watchdog`、`oom-kill`、`start-limit-hit`、`resources`，其他一律寫 `other`。摘要取自那一次 check 的 journal：systemd ≥ 251 的 `MONITOR_*` 環境變數，較舊的版本退回 `systemctl show -p InvocationID`（見 V-0.4）。第一版**不送「恢復」通知**：check 回到全好時不發訊息，用 `systemctl is-failed llmgateway-check.service` 或 journal 確認。

三個外部指令各有逾時（`systemctl` 10 秒、`journalctl` 15 秒、`aicomm` 20 秒），整趟另外受 45 秒的總預算限制：三個都卡住時也不會超過 unit 的 `TimeoutStartSec=60`，notify 不會被 systemd 殺掉。

任何不可用的情況都只寫一行 journal、結束碼 0（通知失敗不該再引發一次通知）：

| journal 行 | 意思 |
| --- | --- |
| `notify sent` | 已送出（同時寫入去重狀態） |
| `notify skipped: not configured` | 設定檔不在、鍵不齊，或值還是範本的 `place-holder-*` |
| `notify skipped: invalid label` | `LGW_NOTIFY_LABEL` 不是 `[A-Za-z0-9._-]{1,32}`（當成沒設，不送） |
| `notify skipped: aicomm not available` | `PATH` 上找不到 `aicomm` |
| `notify failed: aicomm exit <n>` | `aicomm send` 回非 0（下一次 check 失敗會再試） |
| `notify suppressed: same failure within 1h` | 同一組失敗一小時內已經送過 |
| `notify skipped: lock busy`／`notify skipped: state unavailable` | 拿不到鎖／狀態目錄不能寫 |

看通知的 journal：`journalctl -t llmgwctl-check-notify`。

去重：`/var/lib/llmgateway/check-notify.state`（llmgw 0600，一行 JSON）記「同一組失敗上次送出的時間」。key 是 unit 名稱、`result` 與排序後項目名的 SHA-256 前 16 個 hex，所以項目多了或少了就是新的失敗、會立刻通知；一小時內同一組只送一次（每次 check 最多一則）。只有 `aicomm send` 回 0 才寫入；狀態檔不存在、壞掉、schema 不對、讀不到或時鐘倒退都當成沒送過（寧可重送也不漏報），寫入時丟掉超過 7 天的 key。同時只會有一個通知在跑（`check-notify.lock` 的 `flock`）。

`aicomm` 與送件身分要在**這一台主機**上可用（aicomm 是 worker 映像的工具，正式主機不一定有）。沒有的話 `check` 照常運作，失敗通知只會在 journal 留一行 `notify skipped: aicomm not available`。要真的送訊息，得在主機上裝好 aicomm 並把身分設好；`User=` 要換成別的使用者時用 drop-in（`/etc/systemd/system/llmgateway-check-notify.service.d/*.conf`，不進 repo）。unit 是 `ProtectSystem=strict`、只開 `/var/lib/llmgateway` 可寫：aicomm 若需要寫自己的目錄（例如它的狀態或快取），用同一個 drop-in 加 `ReadWritePaths=`；這一點要在 V-0 實測（見 8.18）。

升級與回滾：從 #300 的版本升級上來時，跑升級的是 #300 的 `llmgwctl`，它的 unit 清單沒有 notify unit，**升級本身不會裝它**；但新的 `llmgateway-check.service` 已經帶了 `OnFailure=`，check 失敗時 systemd 只在 journal 記一行「找不到 llmgateway-check-notify.service」，check 本身不受影響。升級後補跑一次 install（`check-timer` 會是 `todo` 提醒）：

```bash
sudo llmgwctl install --tag <tag>
```

回滾到沒有 notify unit 的舊版時，`llmgwctl rollback` 會照 check unit 的規則先 `disable --now` 再刪檔（notify unit 沒有 `[Install]`，這一步對它是 no-op）。從 v0.1.x 遷移的回退（`rollback-to-user`）只對這些 unit 做 `disable --now`、**不刪 unit 檔**，與 `llmgateway.service` 的回退處理一致。遷移的 `cutover`／`finalize` 會跑 `check`，但 `check-notify` 的 `todo` 不算新服務壞了（通知的設定是之後才放的），不會因此停損；其他 `todo` 與 `fail` 照舊讓遷移停損。

### 8.22 store busy 計數與 `auth-busy`（#312）

**root：** 不用（計數檔 0644，非 root 讀得到，但 `auth-busy` 要讀 `llmgateway.env` 的模式，非 root 跑出來是 todo）。**lgwd 停止：** 不用；計數檔只在 lgwd 運作時寫。

防重放庫忙碌到期限（StoreBusy，行程內鎖或 SQLite 忙碌）時，請求會得到 503 `auth_unavailable`，回應與其他不可用的 503 逐 byte 相同；lgwd 在日誌裡記 `reason=store_busy`，並把次數寫進 `.auth-busy.status`（每 60 秒一次，只有次數、窗口與時間）。`auth-busy` 只看這個檔，不開庫。

- 偶爾一次 StoreBusy：`auth-busy` 記 todo（或 warn 模式下記 todo）；這是可以重試的尾端，不需要動作。
- enforce 下近 30 分鐘 ≥ 5 次：`auth-busy` 記 fail 並通知。這代表呼叫方反覆吃到 503。先看 #313 的磁碟前提（磁碟或檔案系統是否忙、寫入是否變慢），**不要調 `busy_timeout`**：調大只會讓請求等更久，不會減少忙碌。
- 重啟 lgwd 會把窗口歸零；`auth-busy` 的 detail 會寫「啟動多久以前」，重啟前的事件留在 journal。
- 計數檔過期（超過 180 秒沒更新，或時間晚於現在超過 180 秒）時記 todo「計數檔過期」，這時看 `auth-heartbeat`；lgwd 沒在跑時 `auth-heartbeat` 已經會 fail，這裡不重複通知。

---

## 附錄：舊版（v0.1.x，遷移完成後不再使用）

> [!WARNING]
> 本節為舊版 user unit 時代（v0.1.x）之遺留記錄。當目標機器已成功執行 `llmgwctl finalize-migration --yes` 並完成 3 天觀察後，本附錄內容即可完全刪除。

舊版架構將所有服務執行於 `llmgw` 帳號之 user unit：
- 服務位置：`~/.config/systemd/user/llmgateway.service`
- 隧道守護：`~/.config/systemd/user/llmgateway-tunnel.service`
- 金鑰拉取：`~/.config/systemd/user/key-sync.service` 與 `key-sync.timer`
- 安裝路徑：`~/.local/share/llmgateway/<tag>/`
- 設定與狀態：`~/.config/llmgateway/` 與 `~/.local/state/llmgateway/`

舊版管理特性（新版已廢棄）：
1. 必須以 `loginctl enable-linger llmgw` 維持 user manager 常駐。
2. 以 `sudo -u llmgw -i` 操作時，必須手動設定環境變數：
   ```bash
   export XDG_RUNTIME_DIR=/run/user/$(id -u)
   export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
   ```
3. 舊版升級依賴 `deploy/install.sh --tag <tag>`，無自動 policy dry-run 與 rollback 機制。
4. 解密密碼存放於 `~/.config/llmgateway/keysync.env`（0600，llmgw 自有）。
