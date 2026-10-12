# LLMGateway 部署與維運手冊（v4）

給本人（或本人授權的人）做「放服務的那台」的一次性設定、GitHub 的設定、日常維運與排錯。設計見 `openspec/changes/gateway-v0/`（design 決定 2、3、4、5a、5b）。

這是 public repo，本手冊：
- 不寫實際的機器名、IP、key 的值；位址一律用 `<跳板機>`、`<埠>` 這類佔位；
- 機器一律稱為「放服務的那台」；
- 「會合點」指池跳板機上現有的 `sshproxy` 帳號（本人指定）。

## 0. 全貌

服務只做三件事：list、get、登錄冷卻（`GET /v1/subscriptions`、`GET /v1/subscriptions/{id}?agent=`、`POST /v1/subscriptions/{id}/cooldown`）。**不做應用層認證**，存取控制只靠 SSH：

- `lgwd` 只綁放服務那台的 `127.0.0.1:<API 埠>`（預設 8480），不綁任何對外或容器網路的介面。
- 隧道守護 `llmgateway-tunnel.service` 用自己的金鑰 `ssh -R` 到會合點，把兩個埠公開在**會合點的 127.0.0.1**：
  - `127.0.0.1:4710` → `lgwd` 的 API；
  - `127.0.0.1:4711` → 放服務那台的 sshd（部署用）。
- 客戶端（Mac、worker 的 sidecar）用 `lgw connect` 經會合點轉發到 4710。
- GitHub 的部署 workflow 用 `ssh -J sshproxy@<跳板機>:<埠> -p 4711 llmgw@127.0.0.1` 登入，只能跑 `remote-deploy.sh`。

放服務的那台上，帳號 `llmgw` 底下有：

| 東西 | 位置 |
|---|---|
| 服務 `lgwd` | `~/.config/systemd/user/llmgateway.service`，執行 `~/.local/bin/llmgateway-run` |
| 隧道守護 | `~/.config/systemd/user/llmgateway-tunnel.service`，執行 `~/.local/bin/llmgateway-tunnel` |
| 程式 | `~/.local/share/llmgateway/<tag>/`，`current` 指著用中的版本 |
| 服務設定 | `~/.config/llmgateway/llmgateway.env`（`LGW_API_PORT`） |
| 隧道設定 | `~/.config/llmgateway/tunnel.env` |
| 五把 key | `~/.local/state/llmgateway/keys.env`（0600） |
| 冷卻與稽核 | `~/.local/state/llmgateway/llm.db`（SQLite，只有 `cooldowns` 與 `audit`，不含 key） |
| 部署入口 | `~/.ssh/authorized_keys` 的 forced command → `current/deploy/remote-deploy.sh` |

## 1. 一次性開機設定（放服務的那台）

依 tasks 3.3，由本人或本人授權的人（PMO）做。**主要做法是 `deploy/bootstrap.sh`（1.A）**：用一個有 sudo 權限的一般帳號（下稱管理帳號）登入放服務的那台執行，一個指令做完建帳號到安裝、金鑰、設定。1.1–1.10 是同一批動作的手動版：腳本做了什麼、腳本出 `todo` 或 `fail` 時怎麼手動補，以及沒辦法用腳本時的備援。

### 1.A 用 `bootstrap.sh`（主要做法）

#### 1.A.1 前提

- 登入放服務的那台，用的是有 sudo 權限的管理帳號（不是 `llmgw`，也不要直接用 root）。
- 那台是 Linux、有 systemd，並且有 `python3`（3.10 以上）、`git`、`curl`、`ssh`、`ssh-keygen`；缺了腳本第一步就 `fail`。
- 要裝的版本已經打好 tag `lgw-vX.Y.Z` 並 push（腳本在 release tag 裡，見 1.A.2）。
- 那台連得到 GitHub（`git clone`）。
- 管理帳號的 sudo 密碼：互動輸入，或放進一個只有你讀得到的密碼檔（`<密碼檔>`，權限 0600，建在你的家目錄底下）。用完就刪。密碼只用來驗證 sudo，不會出現在命令列、輸出或 log 裡；**不要把密碼檔的內容或路徑貼到工單、對話或 repo**。
- 部署公鑰檔（`<部署公鑰檔>`，單行 OpenSSH 公鑰）：由本人產生（見 1.9、2.2）。第一次執行可以先不給。
- 會合點資訊：`sshproxy@<跳板機>:<埠>`，和一個含跳板機 host key 的 known_hosts 檔（`<檔>`，格式見 1.7，**不要用 `ssh-keyscan` 現掃**）。這些由 MyLinuxPool 提供；第一次執行可以先不給。

#### 1.A.2 取得腳本

腳本在 release tag 裡，所以先把那個 tag clone 到管理帳號家目錄底下的暫存目錄（不要放 `/tmp`）：

```bash
git clone --depth 1 --branch lgw-vX.Y.Z https://github.com/FATESAIKOU/LLMGateway.git ~/lgw-boot
```

#### 1.A.3 執行

```bash
bash ~/lgw-boot/deploy/bootstrap.sh \
  --tag lgw-vX.Y.Z \
  --sudo-pass-file <密碼檔> \
  [--deploy-pubkey-file <部署公鑰檔>] \
  [--rendezvous sshproxy@<跳板機>:<埠> --rendezvous-known-hosts <檔>]
```

- `--tag`：要裝的版本，格式 `lgw-vX.Y.Z`；不是 `--check` 時必填。
- `--sudo-pass-file`：選填；沒給就用一般互動的 `sudo`（會問一次密碼）。
- `--deploy-pubkey-file`：選填；給了才會把部署公鑰放進 `~llmgw/.ssh/authorized_keys`（1.9 第 2 點）。
- `--rendezvous` 與 `--rendezvous-known-hosts`：選填，**要一起給**；給了才會填 `tunnel.env`（1.8）並重啟隧道守護。
- `--check`：只檢查、不改任何東西，印出每一步的狀態。
- `--reinstall`：已經是同一個 tag 也重跑 `install.sh`。
- 路徑請用絕對路徑。

**可以重複執行**：每一步先看現狀，已經是目標狀態就印 `ok` 不動它。絕不刪除或覆蓋既有的隧道金鑰、`keys.env`、資料庫。所以中途失敗、補了條件之後，同樣的指令再跑一次就好。

#### 1.A.4 建議的執行順序

隧道金鑰要先產生，才能拿去給 MyLinuxPool 登記；MyLinuxPool 登記完才能接通。所以分兩次跑，最後再 `--check`：

1. **第一次：不帶部署公鑰與會合點。**

   ```bash
   bash ~/lgw-boot/deploy/bootstrap.sh --tag lgw-vX.Y.Z --sudo-pass-file <密碼檔>
   ```

   做完建帳號、linger、檢查 sshd、第一次安裝、產隧道金鑰。結尾摘要會印兩樣東西，各自交給對的人：
   - **隧道公鑰**（一行 `ssh-ed25519 … llmgateway-tunnel`）：經 PMO 交給 mylinuxpool-pm，登記到 MyLinuxPool 的 `TUNNEL_EXTRA_PUBKEYS`（MyLinuxPool#77）。部署公鑰也要一併請他登記（1.9 第 1 點）。
   - **`[127.0.0.1]:4711 ssh-ed25519 …` 那一行**：給本人，放進 secret `GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS`（2.2）。那個 secret 共兩行，另一行是跳板機的，由 MyLinuxPool 提供。

   第一次一定會有 `todo`（還沒給部署公鑰與會合點），結束碼是 3，這是預期的。
2. **等 MyLinuxPool 登記好兩把公鑰（隧道公鑰、部署公鑰）後，第二次：補上部署公鑰與會合點。**

   ```bash
   bash ~/lgw-boot/deploy/bootstrap.sh --tag lgw-vX.Y.Z --sudo-pass-file <密碼檔> \
     --deploy-pubkey-file <部署公鑰檔> \
     --rendezvous sshproxy@<跳板機>:<埠> --rendezvous-known-hosts <檔>
   ```

   這次會寫 `authorized_keys`、複製 known_hosts、填 `tunnel.env`，然後重啟 `llmgateway-tunnel.service`。
3. **`--check` 確認**：帶和第二次一樣的參數再加 `--check`（沒帶的參數，對應的那一步會被當成 `todo`）。

   ```bash
   bash ~/lgw-boot/deploy/bootstrap.sh --tag lgw-vX.Y.Z --sudo-pass-file <密碼檔> \
     --deploy-pubkey-file <部署公鑰檔> \
     --rendezvous sshproxy@<跳板機>:<埠> --rendezvous-known-hosts <檔> --check
   ```

   全部 `ok`、結束碼 0，再照 1.10 驗證，並從 Mac 照 3.1 連一次。確認完把 `~/lgw-boot` 與 `<密碼檔>` 刪掉。

#### 1.A.5 看懂輸出

每一步印一行 `[步驟] 狀態 說明`。步驟依序是 `prereq`（前提）、`user`（帳號）、`linger`、`sshd`、`install`（安裝）、`tunnel-key`（隧道金鑰）、`deploy-key`（部署公鑰）、`rendezvous`（會合點）、`check`（`install.sh --check`）。

| 狀態 | 意思 |
|---|---|
| `ok` | 已經是目標狀態，沒動它 |
| `changed` | 這次改了東西 |
| `todo` | 需要人做（或缺參數）；腳本不會擅自處理，結尾摘要會再列一次 |
| `fail` | 這一步失敗，腳本停在這裡，說明裡有原因 |

結束碼：

| 碼 | 意思 | 怎麼辦 |
|---|---|---|
| 0 | 全部完成，沒有 `todo` | 沒事了 |
| 1 | 某一步 `fail` | 看那一行的說明，排除後重跑 |
| 2 | 參數錯 | 看錯誤訊息（例如 `--rendezvous` 與 `--rendezvous-known-hosts` 沒有一起給、檔案不存在、tag 格式不對） |
| 3 | 完成了，但還有 `todo` | 照 1.A.6 處理後重跑 |

第二次起，條件沒變時 `changed` 應該是 0。

#### 1.A.6 每種 `todo`、`fail` 怎麼辦

| 步驟 | 訊息 | 怎麼辦 |
|---|---|---|
| `sshd` | `todo`：未啟用 `PubkeyAuthentication`、`AllowUsers`／`DenyUsers` 擋了 `llmgw`、沒在 `127.0.0.1:22` 聽 | 腳本**只檢查、不改 `sshd_config`**。照 1.3 自己改 `/etc/ssh/sshd_config` 或 `sshd_config.d/*.conf`，`sshd -t` 通過後 reload，再重跑腳本。 |
| `user` | `todo`（`--check` 時）：帳號不存在，或在 `docker` 群組 | 不加 `--check` 重跑，腳本會建帳號或把 `llmgw` 移出 `docker` 群組（1.1）。 |
| `linger` | `fail`：等 `/run/user/<uid>` 逾時 | `loginctl show-user llmgw -p Linger` 看是否 `yes`；等一下再重跑。 |
| `install` | `fail` | 多半是 clone 不到 tag（tag 沒 push、網路）或 `install.sh` 出錯；看輸出裡 `ERROR` 那行，排除後重跑；已經裝好卻想重裝加 `--reinstall`。 |
| `deploy-key` | `todo`：未給 `--deploy-pubkey-file` | 等部署公鑰就緒，第二次帶上。 |
| `deploy-key` | `fail`：不是單行公鑰 | 檔案只能有一行 OpenSSH 公鑰（`ssh-keygen -l -f <部署公鑰檔>` 要能讀）；不要給私鑰、不要多行。 |
| `rendezvous` | `todo`：未給會合點 | 等 MyLinuxPool 登記好，第二次帶上 `--rendezvous` 與 `--rendezvous-known-hosts`。 |
| `check` | `fail`：`install.sh --check` 不是 0 | 看 4.4 的三態（含經會合點的探針）。隧道還沒設定時隧道守護停在 failed（`config_missing`，見 1.5），所以第一次執行時 `check` 不通過是預期的；全部步驟做完後仍不通過，照 5.4 排錯。 |

摘要最後有一份剩餘 `todo` 清單，照著一條條做完再跑一次即可。

#### 1.A.7 腳本沒做的事

- 不改 `sshd_config`（只檢查）。
- 不碰 `keys.env` 的內容（key 由 `deploy-keys` workflow 部署，4.1）。
- 不登記 MyLinuxPool 的 `TUNNEL_EXTRA_PUBKEYS`、不設 GitHub secrets（2.2）：這些由本人或 mylinuxpool-pm 做。

### 手動步驟（備援與說明）

以下 1.1–1.10 是 `bootstrap.sh` 自動化的動作，需要 root。腳本壞掉、或腳本回報 `todo`／`fail` 需要手動補時照這裡做。

### 1.1 建帳號 `llmgw`（不加 docker 群組）

```bash
useradd -m -s /bin/bash llmgw
```

- 獨立帳號，key 檔只有它讀得到，和池的管理帳號分開。
- **不要加進 `docker` 群組**：能用 docker socket 就等於 root，隔離就沒了。

### 1.2 開 linger

```bash
loginctl enable-linger llmgw
```

兩個服務都是 `llmgw` 的 systemd user unit。開了 linger，開機就會起 user manager，沒人登入也常駐。

### 1.3 sshd 允許 `llmgw` 從 loopback 用公鑰登入

部署連線經隧道進來，在那台看起來是**從 127.0.0.1 連到 127.0.0.1:22**。檢查 `/etc/ssh/sshd_config` 與 `sshd_config.d/*.conf`：

1. sshd 有在 `127.0.0.1:22` 聽（`ListenAddress` 有設時要包含它）；
2. `AllowUsers`／`AllowGroups` 有設時要放行 `llmgw`；`DenyUsers` 不能擋它；
3. `PubkeyAuthentication yes`；
4. 有改就 `systemctl reload sshd`（有的發行版叫 `ssh`）。

### 1.4 確認工具

`python3 --version` 要 3.10 以上（程式只用標準函式庫）；另外要有 `git`、`curl`、`ssh`。

### 1.5 第一次安裝（以 `llmgw` 身分）

```bash
sudo -u llmgw -i
mkdir -p ~/.local/share/llmgateway
git clone --depth 1 --branch <tag> https://github.com/FATESAIKOU/LLMGateway.git ~/lgw-bootstrap
~/lgw-bootstrap/deploy/install.sh --tag <tag>
rm -rf ~/lgw-bootstrap
```

`install.sh` 會自己再抓一次固定 tag 放進 `~/.local/share/llmgateway/<tag>/`，然後：
1. 建 `~/.local/state/llmgateway/`（0700）、空的 `keys.env`（0600）；
2. 沒有設定檔時建範本：`llmgateway.env`、`tunnel.env`（值留空，不含任何位址）；
3. 原子切換 `current`、裝 `llmgateway-run`、`llmgateway-tunnel` 與兩個 unit；
4. `daemon-reload`；`llmgateway.service` 在跑就重啟、沒跑就啟用並啟動；`llmgateway-tunnel.service` 只 `enable`，沒在跑才 `start`，**不會當場重啟它**（部署連線本身走這條隧道，見 4.2 第 4 點）。

這時 key 還沒部署，list 裡每一把的 `available` 都是 false；`tunnel.env` 的必填三項（`RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS`）還是空的，隧道守護起來後在 log 寫一行 `config_missing` 就以結束碼 78 結束。unit 有 `RestartPreventExitStatus=78`，78 不會觸發自動重開，所以不會在設定填好前反覆重啟，`systemctl --user status llmgateway-tunnel` 會看到它停在 failed。這是預期的，1.8 填好設定後手動重啟即可。（`bootstrap.sh` 帶 `--rendezvous` 時會代做 1.8 的填設定與重啟，見 1.A.4。）

### 1.6 產生隧道金鑰（私鑰不離開那台）

以 `llmgw` 身分：

```bash
ssh-keygen -t ed25519 -N '' -C llmgateway-tunnel -f ~/.ssh/id_llmgw_tunnel
cat ~/.ssh/id_llmgw_tunnel.pub
```

- 私鑰只放在這台，**不是** GitHub secret（照 MyLinuxPool 的原則）。
- 把 `.pub` 那一行交給 mylinuxpool-pm（經 PMO），登記到 MyLinuxPool 的 `TUNNEL_EXTRA_PUBKEYS`（MyLinuxPool#77）；由本人或 PMO 設 var 並 dispatch `refresh-authorized-keys`。公鑰本身不是秘密，但註解只寫 `llmgateway-tunnel`，不要帶機器名。

### 1.7 準備會合點的 known_hosts

用 MyLinuxPool 提供的跳板機 host key 建 `~/.ssh/known_hosts_rendezvous`，一行的格式：

```text
[<跳板機>]:<埠> ssh-ed25519 <跳板機的 host key>
```

- 埠不是 22 時主機寫成 `[host]:port`；**埠是 22 時只寫 `host`**（OpenSSH 對預設埠不加括號，寫成 `[host]:22` 對不上）。2.2 的 `DEPLOY_KNOWN_HOSTS` 與 3.1 的 `host_keys` 也是同一個規則。

**不要用 `ssh-keyscan` 現掃、不要用 `accept-new`**；隧道守護是 `StrictHostKeyChecking=yes`，對不上就不連。

### 1.8 填 `tunnel.env`，啟動隧道守護

編輯 `~/.config/llmgateway/tunnel.env`：

```sh
RENDEZVOUS=sshproxy@<跳板機>:<埠>
IDENTITY=/home/llmgw/.ssh/id_llmgw_tunnel
KNOWN_HOSTS=/home/llmgw/.ssh/known_hosts_rendezvous
REMOTE_API=4710
REMOTE_SSH=4711
LOCAL_API=8480
PROBE_INTERVAL=60
PROBE_FAILS=3
```

- 必填只有 `RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS` 三項；其餘留空（`install.sh` 的範本就是空值）就用預設值（4710、4711、8480、60、3）。
- 任何一項缺、`RENDEZVOUS` 不是 `user@host:port`、埠不在 1–65535、`PROBE_*` 不是正數，都算設定不能用，一樣是 `config_missing`、結束碼 78。
- `LOCAL_API` 要和 `llmgateway.env` 的 `LGW_API_PORT` 一樣。
- 4710、4711 是 MyLinuxPool 在會合點上替 LLMGateway 保留的埠（`NODE_GATEWAY.ports.llmgateway`），避開 2220–2499 與 3000–3999。

填好後重啟（78 不會自動重開，一定要手動做這一步；沒有 `config_missing` 的情況下也可以用同一個指令）：

```bash
systemctl --user restart llmgateway-tunnel.service
journalctl --user -u llmgateway-tunnel -n 20
```

看到 `tunnel_up`、之後沒有 `probe_fail` 就是通了。守護程式的行為：
- 隧道用 `ExitOnForwardFailure=yes`：遠端埠公開不了，ssh 直接結束，systemd 10 秒後重開；
- 每 `PROBE_INTERVAL` 秒經會合點 `ssh -W 127.0.0.1:4710` 打一次 `GET /v1/subscriptions`，**只有 HTTP 200 算健康**；
- 連續 `PROBE_FAILS` 次不健康，就殺掉隧道、以非零結束，讓 systemd 重建。

### 1.9 放部署公鑰

部署金鑰由本人產生：私鑰設成 GitHub secret（見 2.2），公鑰放兩個地方。

1. **會合點**：一樣經 mylinuxpool-pm 登記到 `TUNNEL_EXTRA_PUBKEYS`，workflow 才能用 `-J` 經過 sshproxy。
2. **放服務那台的 `~llmgw/.ssh/authorized_keys`**（目錄 0700、檔案 0600），只有一行：

```text
command="/home/llmgw/.local/share/llmgateway/current/deploy/remote-deploy.sh",restrict <部署公鑰>
```

- `command=`：這把公鑰登入時只能跑部署腳本，腳本只接受 `keys` 與 `upgrade lgw-vX.Y.Z` 兩個動詞（單行、tag 格式也檢查）。
- `restrict`：關掉轉發、agent forwarding、pty 等全部額外能力。

### 1.10 驗證

在放服務的那台：

```bash
~/.local/share/llmgateway/current/deploy/install.sh --check; echo $?
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8480/v1/subscriptions
```

`--check` 回 0（本機與經會合點的探針都回 200）、`curl` 印 200。接著從 Mac 照 3.1 連一次。

## 2. 本人在 GitHub 的設定

### 2.1 Environment `llmgateway-deploy`

Settings → Environments → New environment，名稱 `llmgateway-deploy`，勾 **Required reviewers** 並加本人。兩個 workflow 都只能手動觸發（`workflow_dispatch`），每次都要本人核准才會拿到 secret。

### 2.2 Secrets（放在 `llmgateway-deploy` environment）

命名照本人的規則 `<存取方>__<被存取方>__<動作>`。值由本人設，AI 不經手。

**部署連線（4 個）**

| Secret | 格式 | 說明 |
|---|---|---|
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY` | OpenSSH 私鑰全文 | 部署私鑰，公鑰見 1.9。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS` | known_hosts，兩行 | `[<跳板機>]:<埠> <跳板機的 host key>`（埠是 22 時寫 `<跳板機> <host key>`，見 1.7），與 `[127.0.0.1]:4711 <放服務那台的 host key>`。用 MyLinuxPool 提供的指紋與那台的 `/etc/ssh/ssh_host_ed25519_key.pub` 建，不要現掃。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_JUMP` | `sshproxy@<跳板機>:<埠>` | 會合點。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_TARGET` | `llmgw@127.0.0.1:4711` | 固定值：經會合點看到的、放服務那台的 sshd。 |

連線資訊也放 secret，是因為 repo 是 public，不把池的位址放在公開的 vars；workflow 印 ssh 錯誤時也會把位址換成 `[jump]`／`[target]`。

**五把 key（5 個）**

值不能有空白、換行、引號。**還沒有的那把就不要建 secret**。「沒建 secret ＝ 空值 ＝ 沒部署」整條路是這樣走的：
1. workflow：沒建的 secret 在 GitHub 展開成空字串，照樣送 `NAME=`（五行一定都在）；
2. `remote-deploy.sh keys`：只檢查名字（五個都要有、不重複）與值不含空白或引號，空值通過，寫進 `keys.env`；
3. `lgwd`（`lgwd/keys.py`）：值為空視同沒部署，list 的 `available` 為 false，get 回 409 `not_deployed`。

| Secret | 內容 | 寫進 `keys.env` 的名字 |
|---|---|---|
| `POOL_LLMGATEWAY__CLAUDE_PERSONAL__LLM` | Claude 個人訂閱的 setup-token | `CLAUDE_PERSONAL_OAUTH_TOKEN` |
| `POOL_LLMGATEWAY__CLAUDE_COMPANY__LLM` | Claude 公司訂閱的 setup-token | `CLAUDE_COMPANY_OAUTH_TOKEN` |
| `POOL_LLMGATEWAY__OPENROUTER_API__LLM` | OpenRouter key | `OPENROUTER_API_KEY` |
| `POOL_LLMGATEWAY__OPENCODE_ZEN__LLM` | OpenCode Zen key | `OPENCODE_ZEN_API_KEY` |
| `POOL_LLMGATEWAY__OLLAMA_CLOUD__LLM` | Ollama Cloud key | `OLLAMA_API_KEY` |

`keys.env` 的名字只是檔案裡的鍵名，不是給 agent 用的環境變數；agent 的環境變數由 sidecar 的 AgentDriver 從 get 的結果推出。

## 3. 客戶端

### 3.1 Mac

Mac 用自己已經能登入 sshproxy 的金鑰。連線設定是一個 **JSON 檔**（格式由 `lgw/connect.py` 的模組說明定義），手寫 `~/.config/lgw/connect.json`：

```json
{
  "schema": 1,
  "rendezvous": "sshproxy@<跳板機>:<埠>",
  "identity": "/Users/<你>/.ssh/<能登入 sshproxy 的金鑰>",
  "known_hosts": "/Users/<你>/.ssh/known_hosts_rendezvous",
  "remote_api": 4710
}
```

- 欄位只有這些，多一個、缺必填、`schema` 不是 1 都會 `ConfigError`。`jumps`（字串陣列，每個 `user@host:port`）要先經過其他跳板時才加，直接連得到會合點就省略；`remote_api` 省略時是 4710。
- `rendezvous` 一律寫完整的 `user@host:port`：`lgw connect` 用自己的 ssh 設定（`-F`），**不讀 `~/.ssh/config`**，所以別名、`Host` 區塊裡的帳號與埠都沒用；帳號可以改，不固定是 `sshproxy`。
- host key 二擇一，不能兩個都給或都不給：`known_hosts`（檔案路徑，內容照 1.7 的格式）或 `host_keys`（known_hosts 格式的字串陣列，設定檔自成一體）。埠不是 22 時寫 `[host]:port`，是 22 時只寫 `host`；用跳板時，`host_keys` 要包含每個跳板的 host key（所有跳板與會合點共用同一把 identity 與同一份 known_hosts）。
- 一律嚴格檢查（`StrictHostKeyChecking yes`），用 MyLinuxPool 提供的 host key，不現掃。

```bash
python3 -m lgw.connect --config ~/.config/lgw/connect.json
# 印出一行 http://127.0.0.1:<臨時埠>，Ctrl-C 結束
curl -s http://127.0.0.1:<臨時埠>/v1/subscriptions
```

程式裡則是 `with lgw.connect.connect(lgw.connect.load_config(path)) as base_url:`，再用 `lgw.client` 的 list／get／cooldown。`load_config` 每次呼叫都重讀檔案，不快取。

### 3.2 worker

worker 的 sidecar 用 pool-tunnel 既有的 `/home/worker/.ssh/id_tunnel`（`identity`）。sidecar **不讀 MyLinuxPool 的任何檔**，只讀同一種 JSON 連線設定（3.1）：

- 這份 JSON 由 MyLinuxPool 的一個 unit 依 `/run/mlp-gateway/gateway.json`（位址、帳號、埠、host key）產生；rotate 後重新發布時整檔原子改寫，所以用 `host_keys`（自成一體）而不是 `known_hosts` 檔。
- `rendezvous` 要寫成完整的 `user@host:port`，帳號用 gateway.json 給的（不固定是 `sshproxy`）；非 22 埠的 host key 主機寫 `[host]:port`。
- `load_config` 每次操作都重讀，所以改寫後不用重啟 sidecar。
- host key 固定在這份設定裡，不用 accept-new。

這部分在 MyLinuxPool 與 sidecar（MyAiStorage）那一側，LLMGateway 不需要知道有新 worker。

## 4. 日常維運

### 4.1 換 key（`deploy-keys`）

1. 更新對應的 `POOL_LLMGATEWAY__*` secret。
2. Actions → **deploy-keys** → Run workflow，本人核准。
3. workflow 把五行 `NAME=value` 從 stdin 送到 `remote-deploy.sh keys`：
   - 驗格式（只收五個名字、都要有、不重複、值不含空白或引號）；
   - 寫暫存檔（0600）→ rename 成 `keys.env`；
   - 送 `SIGHUP` 給 `lgwd` 重讀。

值不上命令列、不進 log。客戶端不持有 key，換 key 後不用改任何客戶端設定。

### 4.2 升級（`deploy-service`）

1. 打 tag `lgw-vX.Y.Z` 並 push。
2. Actions → **deploy-service** → Run workflow，輸入 tag，本人核准。
3. 那台跑 `install.sh --tag <tag>`：已有完整的就重用、沒有就抓到暫存再搬；原子切換 `current`；重裝兩個 unit 與執行檔；重啟 `lgwd`。只留用中與前一個版本。
4. 隧道守護有變更時，`install.sh` 不當場重啟它（部署連線本身就走這條隧道，當場重啟會切斷部署），而是用 `systemd-run --user --on-active=60` 排一個約 60 秒後的重啟（只有隧道執行檔或 unit 與已裝的不同時才排；隧道沒在跑就直接 start）。已經有 `llmgateway-tunnel-restart` 排程時就跳過不重排（那次重啟會載到新檔）；排程失敗只記警告，不會讓安裝失敗，留給下一次。所以 workflow 結束後約一分鐘隧道會重連，這段時間 list／get 會斷一下，客戶端重連即可。

### 4.3 看狀態

- 誰能不能用：在那台 `curl -s http://127.0.0.1:8480/v1/subscriptions`，或從 Mac 照 3.1。每一筆有 `cooldown`（`5h`、`1w` 各為 `{until}` 或 null）與 `available`。
- 誰在何時 get 過哪一把、誰登錄過冷卻：`llm.db` 的 `audit` 表（不分客戶端，因為不做認證）。
- 「誰正在用哪一把」：看各 sidecar 的 status，LLMGateway 不追蹤。
- log：`journalctl --user -u llmgateway -u llmgateway-tunnel`。log 裡不會有 key 的值，也不會有主機名。

### 4.4 `install.sh --check`（三態）

`--check` 依序檢查：兩個 unit 都 active（有給 `--tag` 時版本要與 `current` 一致）→ 本機 `GET /v1/subscriptions` 回 200 → 經會合點的同一個 list 探針回 200。本機與經會合點的結果分開印。

| 結果 | 意思 |
|---|---|
| 0 | 兩個 unit 都 active、版本一致（有給 `--tag` 時）、本機與經會合點 list 探針都回 200 |
| 1 | 確定不正常：unit 沒在跑或 failed、版本不對、本機或經會合點的探針不是 200 |
| 2 | 無法確認：連不上 user bus、本機 curl 逾時、經會合點探針無法執行（`tunnel.env` 不能用或找不到 ssh 等） |

- 經會合點那一步是 `~/.local/bin/llmgateway-tunnel --probe-once`：讀 `tunnel.env`，用與隧道守護同一套 ssh 參數跑一次 `ssh -W 127.0.0.1:<REMOTE_API>`；回 0＝HTTP 200、1＝不是 200 或沒回應、2＝無法執行。
- 本機不是 200 時不會跑經會合點那一步（本機就確定不正常了）；本機 200 但經會合點不是 200 表示隧道斷了或會合點的 4710 探不到，照 5.4 查。兩段探針都只證明 `GET /v1/subscriptions` 回 200。

### 4.5 外洩時

- **key 外洩**（或懷疑）：在上游作廢並換新 → 更新 secret → `deploy-keys`。本人已接受「能登入 sshproxy 的金鑰都讀得到全部 key」（design 決定 5a），換 key 是這種情況的標準處理。
- **客戶端裝置遺失**（例如手機）：服務沒有應用層認證，任何能登入會合點並開得了轉發的金鑰都能讀全部 key，所以裝置上有 sshproxy 的金鑰就要當成 key 已外洩。依序：
  1. 請 MyLinuxPool（mylinuxpool-pm，經 PMO）把那把金鑰從 sshproxy 撤掉；
  2. 在上游作廢並換新五把 key，更新五個 secret；
  3. 跑 `deploy-keys` 重新部署。

  客戶端不持有 key，不用改任何客戶端設定。步驟 1 做完之前，那把金鑰仍然讀得到。
- **隧道私鑰外洩**：請 mylinuxpool-pm 從 `TUNNEL_EXTRA_PUBKEYS` 拿掉舊公鑰，照 1.6 重產並登記新的，改 `tunnel.env` 後 `systemctl --user restart llmgateway-tunnel`。
- **部署私鑰外洩**：從 `TUNNEL_EXTRA_PUBKEYS` 與 `~llmgw/.ssh/authorized_keys` 拿掉，重產一對，更新 secret。

### 4.6 退回前一版

在那台以 `llmgw` 身分：

```bash
~/.local/share/llmgateway/current/deploy/install.sh --tag <前一個 tag>
~/.local/share/llmgateway/current/deploy/install.sh --check; echo $?
```

前一版還在 `~/.local/share/llmgateway/<tag>/`，`install.sh` 會直接重用，不用連網；執行檔與 unit 也會換回那一版的。隧道守護有變更時一樣排約 60 秒後重啟（見 4.2 第 4 點）。`keys.env` 與 `tunnel.env` 不隨版本，退回不會動它們。也可以從 GitHub 用 `deploy-service` 輸入前一個 tag。

## 5. 排錯

### 5.1 `deploy-keys` 結束碼 2、3、4（輸入不合）

- 2：`SSH_ORIGINAL_COMMAND` 不是 `keys` 或 `upgrade lgw-vX.Y.Z`（多行、tag 格式不對、沒有動詞）。多半是那台 `authorized_keys` 的 `command=` 沒設好（1.9）。
- 3：stdin 是空的，或超過 64 KiB。
- 4：`remote-deploy.sh` 拒收：行不是 `NAME=value`、名字不在五個裡、重複、缺名字、值有空白／換行／引號。檢查五個 `POOL_LLMGATEWAY__*` secret 有沒有夾到前後空白或換行。沒有的那把不要建 secret（見 2.2）。

### 5.2 `deploy-keys` 結束碼 5（寫好了但重讀失敗）

`keys.env` 已經寫好，但 `systemctl --user kill -s HUP llmgateway.service` 失敗，通常是非互動 SSH 沒有 `XDG_RUNTIME_DIR`，連不上 user bus。服務照常跑，下次重啟會讀到新的。

- 馬上要生效：登入那台以 `llmgw` 跑 `systemctl --user kill -s HUP llmgateway.service`。
- 根本處理：確認 `/etc/pam.d/sshd` 有 `pam_systemd.so`。

### 5.3 workflow 的 ssh 連不上（結束碼 255）

- `Host key verification failed`：`…_DEPLOY_KNOWN_HOSTS` 少了 `[<跳板機>]:<埠>` 或 `[127.0.0.1]:4711` 那行，或那台重裝後 host key 變了。
- 跳板那一段 `Permission denied`：部署公鑰還沒登記到 sshproxy（1.9 第 1 點）。
- 跳板過了但 `Connection refused`／`channel open failed`：會合點的 4711 沒人在聽，表示隧道斷了，看 5.4。這時部署也進不去，要另外登入那台處理。
- 目標那一段 `Permission denied`：`authorized_keys` 沒有部署公鑰，或 sshd 擋了 `llmgw`（1.3）。

### 5.4 隧道守護一直重開

`journalctl --user -u llmgateway-tunnel` 看事件：

- `config_missing`（結束碼 78）：`tunnel.env` 的必填三項沒填，或值不能用（1.8）。這種情況**不會**自動重開，unit 停在 failed；改好 `~/.config/llmgateway/tunnel.env` 後執行 `systemctl --user restart llmgateway-tunnel.service`。（log 只寫事件種類，不會說是哪一項。）
- 一起來就結束、反覆重開：多半是遠端埠被佔住（會合點上還留著舊的隧道連線），`ExitOnForwardFailure` 讓 ssh 直接結束。舊連線在 ServerAlive 逾時（約 90 秒）後會被會合點清掉，之後自動恢復；一直不恢復就請 mylinuxpool-pm 看會合點上 4710／4711 是誰在聽。
- `probe_fail` 連續出現後重開：隧道在，但端對端打不通 `lgwd`。先看 `systemctl --user status llmgateway`，以及 `tunnel.env` 的 `LOCAL_API` 是否等於 `LGW_API_PORT`。
- 認證或 host key 失敗：隧道公鑰還沒登記（1.6），或 `KNOWN_HOSTS` 不對（1.7）。

### 5.5 `lgw connect` 回「無法確認」（`Unavailable`）

訊息只有錯誤種類（`ssh_missing`、`bad_config`、`temp_failed`、`ssh_exited`、`timeout`），不含位址。依序檢查：

- 設定檔是 JSON、不是 KEY=VALUE（3.1）：不是 JSON 或欄位不對會是 `ConfigError`，檔案不存在或沒權限是 `OSError`，都在連線之前就停；
- `rendezvous` 有沒有寫成完整的 `user@host:port`（不會讀 `~/.ssh/config` 的別名）、`identity` 路徑與權限、`known_hosts`／`host_keys` 的格式（非 22 埠要 `[host]:port`）；
- 手動 `ssh -i <金鑰> -p <埠> <帳號>@<跳板機>` 能不能過；
- 伺服器端隧道（5.4）。

### 5.6 API 回的錯誤碼

回覆是 `{"error": "<碼>"}`，完整表在 design 決定 6。

| 狀態 | 碼 | 怎麼辦 |
|---|---|---|
| 400 | `bad_request` | JSON 壞掉、body 不是物件或缺必填（看回覆的 `missing`；get 少了 `?agent=` 也是這個）。檢查呼叫方送的內容。 |
| 400 | `invalid_window` | `window` 只能是 `5h` 或 `1w`。 |
| 400 | `invalid_resets_at` | `resets_at` 要是 RFC 3339 字串（例如 `2026-10-18T00:00:00Z`）。 |
| 400 | `resets_at_out_of_range` | `resets_at` 在過去或超過 8 天以後。多半是客戶端判讀錯或時鐘不準；查呼叫方，不是服務的問題。 |
| 403 | `agent_not_allowed` | 見下。 |
| 404 | `not_found` | id 不存在或路徑錯（路徑對但方法錯也是 404）。id 見 `policy/default.json`。 |
| 409 | `not_deployed` | 見 5.7。 |
| 413 | `body_too_large` | body 超過 1 MB。 |
| 500 | `internal` | 服務內部錯誤，只有例外種類在 log，看 `journalctl --user -u llmgateway`。 |

客戶端側（`lgw.client` 的 `Result.error`）另有兩個自己判讀的值：`unreachable`（連不上或逾時，先看 5.5 與 5.4）與 `bad_response`（回覆不是 JSON 物件，通常是連到不是 `lgwd` 的東西，查會合點上 4710 是誰在聽）。

**403 `agent_not_allowed`**：`agent` 不在那一把的 `agents` 裡。Claude 的兩把只給 `claude-code`（Claude 條款的守門），opencode 拿 Claude 訂閱一定是 403，這是預期的。其他組合要改 `policy/default.json` 並發新版。

### 5.7 `available` 為 false，或 get 回 409 `not_deployed`

- `cooldown` 有值：在冷卻中，到 `until` 自動解除，不用做任何事。冷卻中仍然 get 得到，要不要用由 sidecar 決定。
- `cooldown` 都是 null：那把的 key 沒部署（`keys.env` 裡是空值）。有 key 的話更新 secret 再跑 `deploy-keys`。
