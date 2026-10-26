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

服務只做這幾件事：list、resolve、get（過渡期，之後由 d 單移除）、登錄冷卻（`GET /v1/subscriptions`、`GET /v1/resolve?agent=&provider=…`、`GET /v1/subscriptions/{id}?agent=`、`POST /v1/subscriptions/{id}/cooldown`）。冷卻只是提示，resolve 不因冷卻拒絕（冷卻資訊在 `meta`／`list` 的 `cooldown`、`model_cooldowns`；要不要等、換哪個模型由呼叫方決定）。**不做應用層認證**，存取控制只靠 SSH 與本機綁定：

- `lgwd` 只綁放服務那台的 `127.0.0.1:<API 埠>`（預設 8480），不綁任何對外或容器網路的介面。
- 隧道守護 `llmgateway-tunnel.service` 用自己的金鑰 `ssh -R` 到會合點，把兩個埠公開在**會合點的 127.0.0.1**：
  - `127.0.0.1:4710` → `lgwd` 的 API；
  - `127.0.0.1:4711` → 放服務那台的 sshd（管理用：手動升級、維護）。
- 客戶端（Mac、worker 的 sidecar、手機）**自備通道**：把會合點的 4710 轉發到自己機器的 loopback（本機埠預設 4710），再直接打 `http://127.0.0.1:<本機埠>`。LLMGateway 不提供連線程式（見第 4 節）。
- key 的正本是 private repo `FATESAIKOU/LLMGateway-keys` 的 `keys.env.enc`（加密檔）。放服務那台以 `llmgw` 身分使用 `gh` 拉取、解密、驗證後原子換掉 `keys.env`；`lgwd` 發現檔案變更後自動重讀。使用前提：以 `llmgw` 身分登入過 `gh`。
- 管理指令全面統一為 `llmgwctl`（封裝於 `/usr/local/bin/llmgwctl`，或在當版目錄執行 `python3 -m lgwctl`）。

---

## 1. 部署前提與版面配置

### 1.1 系統前提

- 作業系統：Linux，具備 systemd（system 層管理）。
- 工具需求：`python3`（3.10 以上，僅使用標準函式庫）、`git`、`curl`、`ssh`、`ssh-keygen`、`openssl`，以及金鑰同步所需的 `gh`（GitHub CLI）。
- 獨立服務帳號：`llmgw`（不能加入 `docker` 群組，確保隔離性）。
- `gh` 登入：由本人親自以 `llmgw` 身分登入（`sudo -u llmgw -i` 後執行 `gh auth login`，使用瀏覽器登入，不放 token 檔）。
- 隧道金鑰：維持在 `~llmgw/.ssh/`（私鑰 `~llmgw/.ssh/id_llmgw_tunnel`、known_hosts 檔 `~llmgw/.ssh/known_hosts_rendezvous`）。私鑰絕不離開該台機器。

### 1.2 檔案系統版面配置（Layout）

新版架構將程式、設定、狀態與控制檔分層獨立，嚴格限制權限：

| 路徑 | 擁有者與權限 | 用途說明 |
|---|---|---|
| `/opt/llmgateway/<tag>/` | `root:root` 0755 | 各版本程式目錄（從 git tag 抓取） |
| `/opt/llmgateway/current` | `root:root` symlink | 指向目前生效版本的符號連結 |
| `/opt/llmgateway/previous` | `root:root` symlink | 指向上一版版本的符號連結（供 rollback 使用） |
| `/etc/llmgateway/llmgateway.env` | `root:llmgw` 0640 | 服務設定檔（例如 `LGW_API_PORT=8480`） |
| `/etc/llmgateway/tunnel.env` | `root:llmgw` 0640 | 隧道設定檔（`RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS` 等） |
| `/etc/llmgateway/keys.pass` | `root:llmgw` 0640 | 單行解密密碼（`LLMGW_KEYS_PASSWORD=...`，透過 systemd `LoadCredential` 傳遞） |
| `/var/lib/llmgateway/keys.env` | `llmgw:llmgw` 0600 | 解密後的五把金鑰環境變數檔 |
| `/var/lib/llmgateway/llm.db` | `llmgw:llmgw` 0600 | SQLite 資料庫（冷卻紀錄與稽核） |
| `/var/lib/llmgateway/.keysync.sha` | `llmgw:llmgw` 0600 | 上次套用的金鑰 blob sha 記錄 |
| `/var/lib/llmgateway/.keysync.status`| `llmgw:llmgw` 0644 | 金鑰同步狀態檔（時間戳與成敗原因，不含值） |
| `/var/lib/llmgateway-ctl/` | `root:root` 0700 | 遷移與控制狀態目錄（`.cutover.state`、`.cutover.lock`） |
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
  1. 本機 list 200 探針（最多等 60 秒）。
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

測試連線：
```bash
curl -s http://127.0.0.1:4710/v1/subscriptions
```

### 4.2 Python 客戶端程式庫（`lgw.client`）

```python
from lgw import client

c = client.Client("http://127.0.0.1:4710")
c.list()                                              # 取得可用訂閱（不含 key）
c.get("claude-personal", "claude-code")               # 取得 key 明文（需指定 agent）
c.cooldown("claude-personal", "2026-10-18T15:00:00Z") # 登錄冷卻（整把）
c.cooldown("opencode-zen", "2026-10-18T15:00:00Z",    # 只冷卻一個模型
           model="mimo-v2.6-flash-free")
```

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
參數 `--no-remote` 可略過跨會合點之遠端探針。

### 5.2 檢視版本與運行狀態（`status`）

本指令不需要 root 權限：

```bash
llmgwctl status
```

印出目前 `current` 與 `previous` 的版本 tag，以及兩個 system unit（`llmgateway.service`、`llmgateway-tunnel.service`）的 active 狀態。
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

list 的每一筆多 `usage` 與 `available_again_at`，resolve 的 `meta` 多同形狀的 `usage`（get 不帶）。`usage` 是附加資訊：取用量壞了、卡住，list／resolve 照常回應，該筆只是 `unknown`。

- **來源**：Ollama Cloud 取官方 `GET /api/balance`（用 vault 的 key，只在 lgwd 行程內）；Claude 訂閱執行原版 Claude Code 讀 statusline 的 `rate_limits`；OpenRouter、OpenCode Zen 為 `not_applicable`。
- **語意（重要）**：只在有人 list／resolve、而且快取過期時才在**背景**取一次，回應不等它。零星查詢時，多半拿到的是**上一次查詢觸發的快照**（`stale: true`；超過 6 小時轉 `unknown`／`stale_expired`）；重啟後第一次查一律 `unknown`／`not_fetched_yet`，要看新值得等取完（Ollama 約 5 秒內、Claude 最多約 60 秒）再查一次。
- **TTL**：Ollama 預設 120 秒（下限 60 秒）；Claude 預設與下限都是 30 分鐘（每次取耗少量訂閱額度，低於 30 分鐘會被夾到 30 分鐘並記 log）。失敗後依 TTL 加倍退避（上限 max(30 分鐘, TTL)），429 照 `Retry-After`。
- **政策檔**（選填，頂層 `usage`；鍵是 subscription id，不合法 lgwd 不啟動，改檔後先經 `llmgwctl` 的 policy 預檢）：

  ```json
  "usage": {"ollama-cloud": {"enabled": true, "ttl_seconds": 300},
            "claude-personal": {"enabled": false}}
  ```

  只認 `enabled`（布林）與 `ttl_seconds`（正整數），其他鍵一律拒收。**程式**在政策沒有設定時預設關閉（`enabled: false`）；**出廠政策檔**（`policy/default.json`）自 #60 階段 C 起開啟 Claude（經本人授權）；部署前需完成 10.1／10.2 與 C1（階段 C 的前置，即以服務帳號裝原版 Claude Code、trust 固定目錄、服務的 PATH 與 `LGW_USAGE_CLAUDE_CWD` 或 `--usage-claude-cwd`，見 `openspec/changes/archive/2026-10-26-usage-in-responses/tasks.md` 第 10 節），未完成時 `usage` 會是 `unknown`／`upstream_error`。
- **`available_again_at`**：`cooldown.until`（未來）與「用完（`remaining_percent` ≤ 0）的窗口之 `resets_at`」中最晚者；`available` 不因用量為 0 而變 false，選把規則也不變。
- 退版（`install.sh --tag` 回前一版）安全：欄位是新增的，舊消費端忽略即可。

### 5.8 政策檔的 `models.allow`（fnmatch 樣式）

`policy/default.json` 裡每個 subscription 的 `models.allow` 是一組 **fnmatch 樣式**（`*`、`?`、`[...]`）。比對用的是 `fnmatch.fnmatchcase`：載入政策時，`models.default` 的涵蓋檢查在 `lgwd/policy.py` 約第 98 行的 inline fnmatch；resolve 時 model 是否被 allow 涵蓋的比對在 `lgwd/registry.py` 的 `_covered`。有三件事要特別注意：

- **fnmatch 的 `*` 會匹配任何字元，包含 `/`**。所以 `*-free` 這種樣式不只匹配 `beam-free`，也會匹配 `opencode/anything-free` 之類帶斜線的 id。
- **`*-free` 同時涵蓋 zen 與 go 兩把**：出廠政策檔裡，`opencode-zen` 的 `models.allow` 是 `["*-free","big-pickle"]`，`opencode-go` 是 `["*-free"]`，兩邊都用同一個萬用字元。結果是 `beam-free` 之類的 id 在兩把都會被放行；上游只要新增 `*-free` 模型，兩把也會**自動**同時放行，不需要改政策檔。
- **想讓兩把分開控制，就得寫死清單**：把 `*-free` 換成每把各自的一份明列清單，例如 zen 的 `models.allow` 用 `["mimo-v2.6-flash-free", "glm-5.3-flash-free", "big-pickle"]`、go 的用 `["beam-free"]`，兩把才不會互相牽動。兩份清單裡不能放同一個 id，否則照樣會互相放行；改完 `models.allow` 之後，`models.default` 必須仍被涵蓋（見上方載入時驗證）。政策檔是 JSON，不能加註解，所以這個副作用與「要分開控制就寫死清單」的做法記在這裡。

`models.default`（非 null）載入時必須被同一筆的 `models.allow` 樣式涵蓋，否則 lgwd 不啟動（fail-closed）。

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

- 檢查 `/etc/llmgateway/tunnel.env` 設定值是否正確。
- 檢查 `~llmgw/.ssh/id_llmgw_tunnel` 是否存在且權限為 `0600`。
- 檢查 `~llmgw/.ssh/known_hosts_rendezvous` 是否包含正確之會合點主機金鑰。
- 確認會合點上的 4710 或 4711 埠是否殘留舊連線佔用。

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
