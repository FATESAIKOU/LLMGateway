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
4. **切換中斷與狀態防護**：執行 `cutover` 後狀態即進入 `cutover-started`，若切換過程發生未預期中斷或卡住，此階段受到防護，系統會拒絕重新執行 `prepare-migration` 或再次 `cutover`；操作者必須執行 `llmgwctl rollback-to-user` 進行回滾，待回到終態（`reverted`）後方可重新準備與切換。
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
curl -s http://127.0.0.1:4710/v1/subscriptions  # 業務資料（不是探針）
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

其他路徑一律由 `auth-state-dir` 推導，沒有另外的旗標：`auth-replay.sqlite`（可丟棄的防重放庫）、`auth-trust.sqlite`（世代水位、墓碑、已驗證快照）、`auth-store.lock`、`.auth-registry.status`、`allowed_signers.<世代>`、`sshsig/`、`auth-audit.log`。

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

為了跑寫入指令而停掉 lgwd 時，`check` 的 `auth-heartbeat` 會 fail（心跳超過 180 秒），`auth-local` 也會失敗；這是預期的，啟動後恢復。`check` 的認證項目是 `auth-local`、`auth-heartbeat`、`auth-water`、`auth-stores`；非 root 時 `auth-local`、`auth-water`、`auth-stores` 記 `todo`（結束碼 2），不報 fail。

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

### 8.18 上線前清單

**root：** 要（真機操作）。**lgwd 停止：** 視項目。

正式主機啟用 warn／enforce 之前，逐項完成（單次真機授權由本人給；V-0 的每一項都由 PMO 申請、PM 執行，P-D4 由本人或 PMO 做）：

| # | 項目 | 判準 |
| --- | --- | --- |
| V-0.1 | `ssh -V` 是 OpenSSH 10.2p1；用臨時金鑰重跑分類表的五種輸出（Good、incorrect、兩種 Could not verify、缺 principal） | 字樣與 design 相同；不同就停下，不擴充分類表 |
| V-0.2 | lgwd 以 warn 啟動時自測通過 | 日誌沒有 self-test 失敗 |
| V-0.3 | `prlimit` 存在，且以 llmgw 身分能對子行程設限 | — |
| V-0.4 | `systemd --version` ≥ 250；`systemd-analyze verify` 乾淨 | `Type=exec` 與 unit 都有效 |
| V-0.5 | 壓測中執行 `systemctl stop` | 503 為 0；停止後 cgroup 沒有殘留行程 |
| V-0.6 | C-612 在正式主機重跑 | 段二的資源數值取兩邊較大者 |
| V-0.7 | `ProtectSystem=strict` 下的檔案存取 | lgwd 讀得到 `/etc/llmgateway/auth-registry.json`、寫得了 `/var/lib/llmgateway/` |
| P-D4 | Mac 與 worker 映像的 `ssh -V`、`-Y sign` 的 180 bytes、ssh-agent、有密語時不卡住 | 由本人或 PMO 做 |

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
