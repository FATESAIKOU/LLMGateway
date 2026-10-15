# LLMGateway 部署與維運手冊（v5）

給本人（或本人授權的人）做「放服務的那台」的一次性設定、GitHub 的設定、日常維運與排錯。設計見 `openspec/changes/gateway-v0/`（design 決定 2、3、4、5a、5b、6，以及 v5 增補），v5 的做法見 `docs/research/v5-plan.md`。

v5（2026-10-12）跟 v4 的差別，一句話：**key 改由服務自己用 `gh` 從 private repo 拉**（不再有部署 workflow、部署公鑰與部署連線 secret），**客戶端自備通道**（LLMGateway 不再提供連線程式），**升級改手動**，隧道守護與 key-sync 放 `ops-scripts/`。

這是 public repo，本手冊：
- 不寫實際的機器名、IP、key 的值；位址一律用 `<跳板機>`、`<埠>` 這類佔位；
- 機器一律稱為「放服務的那台」；
- 「會合點」指池跳板機上現有的 `sshproxy` 帳號（本人指定）。

## 0. 全貌

服務只做三件事：list、get、登錄冷卻（`GET /v1/subscriptions`、`GET /v1/subscriptions/{id}?agent=`、`POST /v1/subscriptions/{id}/cooldown`）。**不做應用層認證**，存取控制只靠 SSH：

- `lgwd` 只綁放服務那台的 `127.0.0.1:<API 埠>`（預設 8480），不綁任何對外或容器網路的介面。
- 隧道守護 `llmgateway-tunnel.service` 用自己的金鑰 `ssh -R` 到會合點，把兩個埠公開在**會合點的 127.0.0.1**：
  - `127.0.0.1:4710` → `lgwd` 的 API；
  - `127.0.0.1:4711` → 放服務那台的 sshd（管理用：手動升級、維修）。
- 客戶端（Mac、worker 的 sidecar、手機）**自備通道**：把會合點的 4710 轉發到自己機器的 loopback（本機埠預設 4710），再直接打 `http://127.0.0.1:<本機埠>`。LLMGateway 不提供連線程式（3.1–3.3）。
- key 的正本是 private repo `FATESAIKOU/LLMGateway-keys` 的 `keys.env.enc`（加密檔）。放服務那台的 `key-sync`（systemd timer，約每 10 分鐘）用 `gh` 拉下來、解密、驗證後原子換掉 `keys.env`；`lgwd` 自己發現檔案換了會重讀。使用前提：以 `llmgw` 身分登入過 `gh`（1.9）。
- 服務程式的升級：管理帳號用手動 `ssh` 上去跑 `install.sh --tag`（4.2）。兩個部署 workflow 與 `remote-deploy.sh` 在 v5 整組拿掉了。

放服務的那台上，帳號 `llmgw` 底下有：

| 東西 | 位置 |
|---|---|
| 服務 `lgwd` | `~/.config/systemd/user/llmgateway.service`，執行 `~/.local/bin/llmgateway-run` |
| 隧道守護 | `~/.config/systemd/user/llmgateway-tunnel.service`，執行 `~/.local/bin/llmgateway-tunnel` |
| key 拉取 | `~/.config/systemd/user/key-sync.timer`（約每 10 分鐘）＋ `key-sync.service`，執行 `~/.local/bin/key-sync` |
| 程式 | `~/.local/share/llmgateway/<tag>/`，`current` 指著用中的版本 |
| 服務設定 | `~/.config/llmgateway/llmgateway.env`（`LGW_API_PORT`） |
| 隧道設定 | `~/.config/llmgateway/tunnel.env` |
| 解密密碼 | `~/.config/llmgateway/keysync.env`（**0600**，只有一行 `LLMGW_KEYS_PASSWORD=…`） |
| 五把 key | `~/.local/state/llmgateway/keys.env`（0600，由 key-sync 換） |
| 拉取版本記錄 | `~/.local/state/llmgateway/.keysync.sha`（純文字，上次套用的 blob `sha`） |
| 冷卻與稽核 | `~/.local/state/llmgateway/llm.db`（SQLite，只有 `cooldowns` 與 `audit`，不含 key） |

維運工具（隧道守護與 key-sync）在 repo 的 `ops-scripts/`；`install.sh` 與 `bootstrap.sh` 仍在 `deploy/`。

## 1. 一次性開機設定（放服務的那台）

依 tasks 3.3，由本人或本人授權的人（PMO）做。**主要做法是 `deploy/bootstrap.sh`（1.A）**：用一個有 sudo 權限的一般帳號（下稱管理帳號）登入放服務的那台執行，一個指令做完建帳號到安裝、隧道金鑰、解密密碼。1.1–1.10 是同一批動作的手動版：腳本做了什麼、腳本出 `todo` 或 `fail` 時怎麼手動補，以及沒辦法用腳本時的備援。

`gh` 登入（1.9）**bootstrap 不做**，本人自己來。

### 1.A 用 `bootstrap.sh`（主要做法）

#### 1.A.1 前提

- 登入放服務的那台，用的是有 sudo 權限的管理帳號（不是 `llmgw`，也不要直接用 root）。
- 那台是 Linux、有 systemd，並且有 `python3`（3.10 以上）、`git`、`curl`、`ssh`、`ssh-keygen`；缺了腳本第一步就 `fail`。
- 要裝的版本已經打好 tag `lgw-vX.Y.Z` 並 push（腳本在 release tag 裡，見 1.A.2）。
- 那台連得到 GitHub（`git clone`；key-sync 另外要連得到 `gh` 的 API）。
- 管理帳號的 sudo 密碼：互動輸入，或放進一個只有你讀得到的密碼檔（`<密碼檔>`，權限 0600，建在你的家目錄底下）。用完就刪。密碼只用來驗證 sudo，不會出現在命令列、輸出或 log 裡；**不要把密碼檔的內容或路徑貼到工單、對話或 repo**。
- 解密密碼：本人自己產生的 32 字元隨機英數（1.9）。可以放進一個 0600 的單行檔案（`<解密密碼檔>`）給 `--decrypt-pass-file`，或在接得到終端機時互動輸入。**這個值不進 repo、不進 secret、不貼給人**。
- 會合點資訊：`sshproxy@<跳板機>:<埠>`，和一個含跳板機 host key 的 known_hosts 檔（`<檔>`，格式見 1.7，**不要用 `ssh-keyscan` 現掃**）。這些由 MyLinuxPool 提供；第一次執行可以先不給。

#### 1.A.2 取得腳本

腳本在 release tag 裡，所以先把那個 tag clone 到管理帳號家目錄底下的暫存目錄（不要放 `/tmp`）：

```bash
git clone --depth 1 --branch lgw-vX.Y.Z https://github.com/FATESAIKOU/LLMGateway.git ~/lgw-boot
```

`lgw-vX.Y.Z` 是 annotated tag（指向 tag 物件，不是 commit 本身）。clone 時 git 可能印一行 `warning: refs/tags/lgw-vX.Y.Z … is not a commit!`：**這是正常的警告**，工作目錄拿到的就是那個 tag 標記的 commit，照樣跑 1.A.3 即可。

#### 1.A.3 執行

```bash
bash ~/lgw-boot/deploy/bootstrap.sh \
  --tag lgw-vX.Y.Z \
  --sudo-pass-file <密碼檔> \
  [--decrypt-pass-file <解密密碼檔>] \
  [--rendezvous sshproxy@<跳板機>:<埠> --rendezvous-known-hosts <檔>]
```

- `--tag`：要裝的版本，格式 `lgw-vX.Y.Z`；不是 `--check` 時必填。
- `--sudo-pass-file`：選填；沒給就用一般互動的 `sudo`（會問一次密碼）。
- `--decrypt-pass-file`：選填；給了就會把解密密碼寫進 `~/.config/llmgateway/keysync.env`（0600）。沒給時，如果接得到終端機（stdin 是 tty）會問一次（不回顯；直接 Enter 跳過），否則那一步記 `todo`。
- `--rendezvous` 與 `--rendezvous-known-hosts`：選填，**要一起給**；給了才會填 `tunnel.env`（1.8）並重啟隧道守護。
- `--check`：只檢查、不改任何東西，印出每一步的狀態。
- `--reinstall`：已經是同一個 tag 也重跑 `install.sh`。
- 路徑請用絕對路徑。

**可以重複執行**：每一步先看現狀，已經是目標狀態就印 `ok` 不動它。絕不刪除或覆蓋既有的隧道金鑰、`keys.env`、`keysync.env`、資料庫。所以中途失敗、補了條件之後，同樣的指令再跑一次就好。

#### 1.A.4 建議的執行順序

隧道金鑰要先產生，才能拿去給 MyLinuxPool 登記；MyLinuxPool 登記完才能接通。所以分兩次跑，`gh` 登入與解密密碼放好之後再 `--check`：

1. **第一次：不帶會合點。**

   ```bash
   bash ~/lgw-boot/deploy/bootstrap.sh --tag lgw-vX.Y.Z --sudo-pass-file <密碼檔> \
     --decrypt-pass-file <解密密碼檔>
   ```

   做完建帳號、linger、檢查 sshd、第一次安裝（含 key-sync 的 timer）、產隧道金鑰、寫解密密碼。結尾摘要會印兩樣東西，各自交給對的人：
   - **隧道公鑰**（一行 `ssh-ed25519 … llmgateway-tunnel`）：經 PMO 交給 mylinuxpool-pm，登記到 MyLinuxPool 的 `TUNNEL_EXTRA_PUBKEYS`（MyLinuxPool#77）。v5 不用登記部署公鑰。
   - **那台的 ed25519 host 公鑰**：給本人自己，之後手動 ssh 進去（4.2）時比對指紋用。

   第一次一定會有 `todo`（還沒給會合點、還沒登入 `gh`），結束碼是 3，這是預期的；`gh` 登入完照第 3 步重跑一次即可。
2. **等 MyLinuxPool 登記好隧道公鑰後，第二次：補上會合點。**

   ```bash
   bash ~/lgw-boot/deploy/bootstrap.sh --tag lgw-vX.Y.Z --sudo-pass-file <密碼檔> \
     --decrypt-pass-file <解密密碼檔> \
     --rendezvous sshproxy@<跳板機>:<埠> --rendezvous-known-hosts <檔>
   ```

   這次會複製 known_hosts、填 `tunnel.env`，然後重啟 `llmgateway-tunnel.service`。
3. **`gh` 登入（本人做；bootstrap 只看狀態、不代做）**：

   bootstrap 有一步 `gh-auth`，以 `llmgw` 身分問 `gh auth status`：

   | 狀態 | 意思 |
   |---|---|
   | `ok` | `llmgw` 已登入 `gh` |
   | `todo` | 還沒登入。**結束碼 3**，後面的步驟照跑、摘要照印；照下面登入後重跑本腳本即可 |
   | `fail` | 找不到 `gh`（該身分要裝 `gh` 才拉得到金鑰）。腳本停在這裡，結束碼 1 |

   本人照這段登入（v5 選 (B)：用瀏覽器登入，不放 token 檔）：

   ```bash
   sudo -u llmgw -i
   gh auth login
   ```

   登入完重跑第一次那條 `bootstrap.sh`，這次 `gh-auth` 會是 `ok`。
4. **第一次拉取**（登入完可以立刻拉，不必等 10 分鐘）：

   ```bash
   sudo -u llmgw -i
   systemctl --user start key-sync.service
   journalctl --user -u key-sync -n 5
   ```

   log 看到 `ok 已換 keys.env` 就是拉下首版；`failed=gh_auth` 表示 `gh` 沒登入或 token 失效（5.1）。
5. **`--check` 確認**：帶和第二次一樣的參數再加 `--check`（沒帶的參數，對應的那一步會被當成 `todo`）。

   ```bash
   bash ~/lgw-boot/deploy/bootstrap.sh --tag lgw-vX.Y.Z --sudo-pass-file <密碼檔> \
     --decrypt-pass-file <解密密碼檔> \
     --rendezvous sshproxy@<跳板機>:<埠> --rendezvous-known-hosts <檔> --check
   ```

   全部 `ok`、結束碼 0，再照 1.10 驗證，並從 Mac 照 3.1 連一次。確認完把 `~/lgw-boot`、`<密碼檔>`、`<解密密碼檔>` 刪掉。

#### 1.A.5 看懂輸出

每一步印一行 `[步驟] 狀態 說明`。步驟依序是 `prereq`（前提）、`user`（帳號）、`gh-auth`（`llmgw` 的 `gh` 登入狀態）、`linger`、`sshd`、`install`（安裝）、`tunnel-key`（隧道金鑰）、`keysync`（解密密碼）、`rendezvous`（會合點）、`check`（`install.sh --check`）。

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
| `gh-auth` | `todo`：`llmgw` 的 `gh` 尚未登入 | 本人 `sudo -u llmgw -i` 後 `gh auth login`（用瀏覽器登入，不放 token 檔），再重跑本腳本。腳本不代做登入、也不碰 token 值。 |
| `gh-auth` | `fail`：找不到 `gh` | 該身分要裝 `gh` 才拉得到金鑰；裝好再重跑（這一步失敗腳本會停在這裡，後面的步驟不跑）。 |
| `keysync` | `todo`：未提供解密密碼 | 給 `--decrypt-pass-file`（單行、0600），或接在終端機上重跑讓它問一次。已經有 `keysync.env` 時這一步是 `ok`，不會覆蓋。 |
| `keysync` | `fail`：解密密碼檔格式無效 | 檔案只能有一行、值不含空白（1.9）。修正後重跑；既有 `keysync.env` 不會被動到。 |
| `rendezvous` | `todo`：未給會合點 | 等 MyLinuxPool 登記好隧道公鑰，第二次帶上 `--rendezvous` 與 `--rendezvous-known-hosts`。 |
| `check` | `fail`：`install.sh --check` 不是 0 | 看 4.4 的三態（含經會合點的探針與 key-sync 的判斷）。隧道還沒設定、或 `gh` 還沒登入／解密密碼還沒放時不通過是預期的；全部做完後仍不通過，照 5.1–5.3 排錯。 |

摘要最後有一份剩餘 `todo` 清單，照著一條條做完再跑一次即可。

#### 1.A.7 腳本沒做的事

- 不改 `sshd_config`（只檢查）。
- 不做 `gh` 登入（本人以 `llmgw` 身分自己做，1.9）；不碰 `keys.env` 的內容（key 由 key-sync 拉，4.1）。
- 不登記 MyLinuxPool 的 `TUNNEL_EXTRA_PUBKEYS`、不設 GitHub secret（2.2）：這些由本人或 mylinuxpool-pm 做。

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

三個服務都是 `llmgw` 的 systemd user unit。開了 linger，開機就會起 user manager，沒人登入也常駐。

### 1.3 sshd 允許 `llmgw` 從 loopback 用公鑰登入

管理連線經隧道進來，在那台看起來是**從 127.0.0.1 連到 127.0.0.1:22**。檢查 `/etc/ssh/sshd_config` 與 `sshd_config.d/*.conf`：

1. sshd 有在 `127.0.0.1:22` 聽（`ListenAddress` 有設時要包含它）；
2. `AllowUsers`／`AllowGroups` 有設時要放行 `llmgw`；`DenyUsers` 不能擋它；
3. `PubkeyAuthentication yes`；
4. 有改就 `systemctl reload sshd`（有的發行版叫 `ssh`）。

### 1.4 確認工具

`python3 --version` 要 3.10 以上（程式只用標準函式庫）；另外要有 `git`、`curl`、`ssh`，以及 key-sync 用的 **`gh`** 與 **`openssl`**。`gh` 裝好、登入是 1.9 的事。

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
3. 原子切換 `current`、裝 `llmgateway-run`、`llmgateway.service`，以及 `ops-scripts/` 來的 `llmgateway-tunnel`＋unit、`key-sync`＋unit／timer；
4. `daemon-reload`；`llmgateway.service` 在跑就重啟、沒跑就啟用並啟動；`llmgateway-tunnel.service` 只 `enable`，沒在跑才 `start`，**不會當場重啟它**（管理連線本身走這條隧道，見 4.2 第 4 點）；`key-sync.timer` 只 `enable`，沒在跑才 `start`（已在跑就不打斷它的排程）。

這時 key 還沒拉（`keys.env` 是空的），list 裡每一把的 `available` 都是 false；`tunnel.env` 的必填三項（`RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS`）還是空的，隧道守護起來後在 log 寫一行 `config_missing` 就以結束碼 78 結束。unit 有 `RestartPreventExitStatus=78`，78 不會觸發自動重開，所以不會在設定填好前反覆重啟，`systemctl --user status llmgateway-tunnel` 會看到它停在 failed。這是預期的，1.8 填好設定後手動重啟即可。（`bootstrap.sh` 帶 `--rendezvous` 時會代做 1.8 的填設定與重啟，見 1.A.4。）

### 1.6 產生隧道金鑰（私鑰不離開那台）

以 `llmgw` 身分：

```bash
ssh-keygen -t ed25519 -N '' -C llmgateway-tunnel -f ~/.ssh/id_llmgw_tunnel
cat ~/.ssh/id_llmgw_tunnel.pub
```

- 私鑰只放在這台，**不是** GitHub secret（照 MyLinuxPool 的原則）。
- 把 `.pub` 那一行交給 mylinuxpool-pm（經 PMO），登記到 MyLinuxPool 的 `TUNNEL_EXTRA_PUBKEYS`（MyLinuxPool#77）；由本人或 PMO 設 var 並 dispatch `refresh-authorized-keys`。公鑰本身不是秘密，但註解只寫 `llmgateway-tunnel`，不要帶機器名。
- v5 不產生、不登記部署公鑰。

### 1.7 準備會合點的 known_hosts

用 MyLinuxPool 提供的跳板機 host key 建 `~/.ssh/known_hosts_rendezvous`，一行的格式：

```text
[<跳板機>]:<埠> ssh-ed25519 <跳板機的 host key>
```

- 埠不是 22 時主機寫成 `[host]:port`；**埠是 22 時只寫 `host`**（OpenSSH 對預設埠不加括號，寫成 `[host]:22` 對不上）。
- **不要用 `ssh-keyscan` 現掃、不要用 `accept-new`**；隧道守護是 `StrictHostKeyChecking=yes`，對不上就不連。

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

### 1.9 `gh` 登入、放解密密碼、第一次拉取（v5 的核心）

**`gh` 登入（本人，以 `llmgw` 身分）**：`gh` 的登入跟著 Unix 使用者走，key-sync 以 `llmgw` 跑，所以：

```bash
sudo -u llmgw -i
gh auth login        # 選 (B)：用瀏覽器登入，不放 token 檔
```

v5 的 `gh` 認證**本人已選 (B)**（2026-10-12）：`llmgw` 自己用瀏覽器 `gh auth login` 登入，不用 `--with-token` 的 token 檔。token 存在 `~llmgw/.config/gh/`（0600），由 `gh` 自己管，LLMGateway 不碰、不複製、不轉發它的值。

- 風險（**本人已知並接受**）：`gh` 的登入狀態拿到的是本人帳號的一般權限，拿著這台 `llmgw` 的登入狀態的人可能讀寫本人其他 repo；也因此這台不能當成完全隔離的機器來放。
- 因此 `install.sh --check` 除了問「登入了沒」，還會實際問一次**讀不讀得到** `FATESAIKOU/LLMGateway-keys`（只問 repo 本身，不讀檔案內容），讀不到就是不能用（4.4）。
- **不用 deploy key**：免登記公鑰、免管 ssh host key；代價是登入狀態失效時 key 凍在舊版（4.1）。
- 確認登入好了：

  ```bash
  sudo -u llmgw -i
  gh auth status
  gh api repos/FATESAIKOU/LLMGateway-keys --jq .full_name
  ```

**解密密碼**：`bootstrap.sh --decrypt-pass-file` 或互動輸入會寫進 `~/.config/llmgateway/keysync.env`（0600、擁有者 `llmgw`），內容只有一行：

```sh
LLMGW_KEYS_PASSWORD=<32 字元隨機英數，與加密時用的同一個>
```

`key-sync.service` 用 `EnvironmentFile=` 讀它，再以 `-pass env:LLMGW_KEYS_PASSWORD` 調 openssl。密碼不進 repo、不進 log、不上命令列。密碼遺失＝加密檔打不開＝換不了 key：重設新密碼、整檔重加密後 push（3.3 的流程），所以密碼另有一份在 GitHub secret 備份（2.2）。

**第一次拉取**（不必等 timer）：

```bash
systemctl --user start key-sync.service
journalctl --user -u key-sync -n 5
```

看到 `ok 已換 keys.env` 就好了；`failed=gh_auth` 是 `gh` 沒登入／token 失效，`failed=password_missing` 是密碼沒放，`failed=format_failed`／`decrypt_failed` 見 5.1。失敗不會動舊 key，下一次 timer（約 10 分鐘）再試。

### 1.10 驗證

在放服務的那台：

```bash
~/.local/share/llmgateway/current/deploy/install.sh --check; echo $?
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8480/v1/subscriptions
journalctl --user -u key-sync -n 5
```

`--check` 回 0（兩個 unit active、本機與經會合點的探針都回 200、`gh` 能用、解密密碼放了、上次拉取沒失敗）、`curl` 印 200、key-sync 的 log 沒有 `failed=`。接著從 Mac 照 3.1 連一次。

## 2. 本人在 GitHub 的設定

### 2.1 Environment `llmgateway-deploy`

Settings → Environments → New environment，名稱 `llmgateway-deploy`，勾 **Required reviewers** 並加本人。v5 只剩解密密碼的備份 secret 用它。

### 2.2 Secrets（放在 `llmgateway-deploy` environment）

命名照本人的規則 `<存取方>__<被存取方>__<動作>`。值由本人設，AI 不經手。

| Secret | 內容 | 用途 |
|---|---|---|
| `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT` | 解密密碼備份（一行） | 密碼遺失時重建用；不參與服務運作 |

**收掉的**（v5 乙）：五把 key 的 secret（`POOL_LLMGATEWAY__*__LLM`）與部署連線 4 個（`GH_LLMGATEWAY__POOL_SSH__DEPLOY`、`…_DEPLOY_KNOWN_HOSTS`、`…_DEPLOY_JUMP`、`…_DEPLOY_TARGET`）。順帶請 mylinuxpool-pm 拿掉 MyLinuxPool `TUNNEL_EXTRA_PUBKEYS` 裡的**部署**公鑰（隧道公鑰照留）。

### 2.3 Private repo `FATESAIKOU/LLMGateway-keys`

key 的正本：private repo（只有本人用）根目錄的 `keys.env.enc`。

- 加密格式：`openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt`，密碼經 `-pass env:` 傳；解密後的內容是五行 `KEY=VALUE`（`CLAUDE_PERSONAL_OAUTH_TOKEN`、`CLAUDE_COMPANY_OAUTH_TOKEN`、`OPENROUTER_API_KEY`、`OPENCODE_ZEN_API_KEY`、`OLLAMA_API_KEY`），外加一行人類看的版本註解（例 `# version: 2026-10-12-01`；機器不靠它判斷）。
- 版本判斷：機器只認 `gh api` 回傳的 blob `sha`（加上解密後內容的 sha256 比對，避免 mtime 空轉）。
- 沒有那把 key 就不寫那一行（或寫空值）＝沒部署：list 的 `available` 為 false，get 回 409 `not_deployed`。

## 3. 客戶端（自備通道）

LLMGateway 不提供客戶端連線程式，也不管通道怎麼建；它對通道只提一個需求：**客戶端所在機器本機 loopback 的某個 port 是會合點 API 埠（4710）的 HTTP 轉發**。本機 port 預設 **4710**（與會合點上的 4710 同號純屬巧合，不同機器），建通道的一方可以改；改了 sidecar 的 base URL 就跟著改。

客端程式用 `lgw.client`（base URL 由呼叫方給）：

```python
from lgw import client

c = client.Client("http://127.0.0.1:4710")
c.list()                                    # 不含 key
c.get("claude-personal", "claude-code")     # 含 key；agent 必填
c.cooldown("claude-personal", "2026-10-12T15:00:00Z")   # 登錄 until（RFC 3339 UTC）
```

`Client` 回 `Result`（`ok`、`status`、`data`、`error`、`unreachable`），連不上或逾時回 `unreachable=True`、**不丟例外**；`Result` 的 repr 不印 `data`（get 的 `data` 裡有 key），不要 log 整個 `Result`。

### 3.1 Mac

用自己已經能登入 sshproxy 的金鑰，把會合點的 4710 轉發到本機：

```bash
ssh -N -i ~/.ssh/<能登入 sshproxy 的金鑰> -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=~/.ssh/known_hosts_rendezvous \
  -p <埠> -L 127.0.0.1:4710:127.0.0.1:4710 sshproxy@<跳板機>
curl -s http://127.0.0.1:4710/v1/subscriptions
```

- host key 用 MyLinuxPool 提供的，寫進自己的 known_hosts（埠不是 22 時 `[host]:port`）；不要 `accept-new`。
- 要經過別的跳板時自己加 `-J`。
- 也可以直接用 `mlp`（MyLinuxPool 的工具）開同一條轉發。

### 3.2 worker

worker 的 sidecar 自己打本機 loopback 的 port（預設 4710）：

- 通道由 **MyLinuxPool 的 LLMGateway capability** 附的小 script 建立（那是 MyLinuxPool 的工作；LLMGateway 只說「本機 loopback 某 port 即 HTTP API」）。
- sidecar 用 pool-tunnel 既有的 sshproxy 金鑰，不讀 MyLinuxPool 的設定檔；通道斷了 sidecar 照舊回報「無法確認」，等一下再試。
- 這部分在 MyLinuxPool 與 sidecar（MyAiStorage）那一側，LLMGateway 不需要知道有新 worker。

### 3.3 手機

手機自行用 `ssh -J` 等方式把會合點的 4710 轉發到手機的 loopback，再打那個 port。LLMGateway 不提供程式（MyAiEntry#147 已關掉）。

## 4. 日常維運

### 4.1 換 key（在 private repo 改加密檔）

本人自己（AI 不經手值）：

```bash
export LLMGW_KEYS_PASSWORD='...'   # 與 keysync.env 裡同一個；用完就 unset
# 寫明文 keys.env（五行 KEY=VALUE）
openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt \
  -pass env:LLMGW_KEYS_PASSWORD -in keys.env -out keys.env.enc
unset LLMGW_KEYS_PASSWORD
shred -u keys.env                  # 刪明文
git add keys.env.enc && git commit -m 'update keys' && git push
```

之後：

1. 等一次拉取（≤ 約 10 分鐘）；趕時間就上服務那台手動觸發：`sudo -u llmgw -i` 後 `systemctl --user start key-sync.service`。
2. 驗證：`curl` 經本機通道打 list（`available` 回來了）或等 sidecar 換把成功；log 只看 key-sync 的成敗行（`journalctl --user -u key-sync`）。

key-sync 的流程是：`gh api` 取 base64 內容與 blob `sha` → 與 `.keysync.sha` 相同就不動 → base64 解碼 → openssl 解密（密碼只經環境變數）→ 用 `lgwd/keys.py` 的規則驗證 → 0600 暫存檔 rename 成 `keys.env` → 記新 `sha`。**任何一步失敗都保留舊 key**、不寫 `sha`，只記一行不含值的 log；`lgwd` 自己發現 `keys.env` 換了（mtime／inode）就重讀，失敗沿用舊的。值不上命令列、不進 log。客戶端不持有 key，換 key 後不用改任何客戶端設定。

### 4.2 升級（手動）

1. 打 tag `lgw-vX.Y.Z` 並 push。
2. 用管理帳號 ssh 上去（例如經會合點的 4711：`ssh -J sshproxy@<跳板機>:<埠> -p 4711 <管理帳號>@127.0.0.1`；host key 自己比對）。
3. 跑：

   ```bash
   ~/.local/share/llmgateway/current/deploy/install.sh --check
   ~/.local/share/llmgateway/current/deploy/install.sh --tag lgw-vX.Y.Z
   ~/.local/share/llmgateway/current/deploy/install.sh --check
   ```

   已有完整的 tag 目錄就重用、沒有就抓到暫存再搬；原子切換 `current`；重裝 unit 與執行檔（含 `ops-scripts/` 來的隧道與 key-sync）；重啟 `lgwd`；`key-sync.timer` 已在跑就不動它。
4. 隧道守護有變更時，`install.sh` 不當場重啟它（管理連線本身就走這條隧道，當場重啟會切斷連線），而是用 `systemd-run --user --on-active=60` 排一個約 60 秒後的重啟（只有隧道執行檔或 unit 與已裝的不同時才排；隧道沒在跑就直接 start）。已經有 `llmgateway-tunnel-restart` 排程時就跳過不重排；排程失敗只記警告，不會讓安裝失敗。所以升級後約一分鐘隧道會重連，這段時間 list／get 會斷一下，客戶端重連即可。

政策檔跟著 tag 一起換：`lgwd` 讀的是 `~/.local/share/llmgateway/current/policy/default.json`（`--policy` 指向 `current`），所以升級就是把那一版的 `policy/default.json` 換上，不用（也不該）手動搬舊政策檔。`lgwd` 只在啟動時讀政策（v5 沒有熱重載），`install.sh` 升級時會重啟 `lgwd`，所以新版政策升級後就生效；反過來說，單獨手改政策檔要重啟 `lgwd` 才生效。

政策檔是 **fail-closed** 的，被拒的是**缺欄位**：v4 的政策檔沒有 `until_max_ahead_seconds`，所以會被拒（`lgwd` 起不來，`--check` 不會過）。要自己改過政策檔（跨版沿用同一份），得補上這個新欄位；舊欄位（`cooldown_buffer_seconds`、`resets_at_max_ahead_seconds`）**留著無妨**，loader 會忽略多出來的欄位。所以：不要想著「把舊欄位清掉就好」，要補的是新欄位。

### 4.3 看狀態

- 誰能不能用：在那台 `curl -s http://127.0.0.1:8480/v1/subscriptions`，或從 Mac 照 3.1。每一筆有 `cooldown`（`{"until": …}` 或 null）與 `available`。
- 誰在何時 get 過哪一把、誰登錄過冷卻：`llm.db` 的 `audit` 表（不分客戶端，因為不做認證）。
- 「誰正在用哪一把」：看各 sidecar 的 status，LLMGateway 不追蹤。
- log：`journalctl --user -u llmgateway -u llmgateway-tunnel`，以及 key 拉取 `journalctl --user -u key-sync`。log 裡不會有 key 的值，也不會有主機名。
- 上一次套用的版本：`cat ~/.local/state/llmgateway/.keysync.sha`（blob sha，本人讀得到）。

### 4.4 `install.sh --check`（三態）

`--check` 依序檢查：兩個 unit 都 active（有給 `--tag` 時版本要與 `current` 一致）→ 本機 `GET /v1/subscriptions` 回 200 → 經會合點的同一個 list 探針回 200 → key-sync 能不能用（以服務帳號問 `gh` 有沒有登入、讀不讀得到 `FATESAIKOU/LLMGateway-keys`、`keysync.env` 有沒有非空的解密密碼、`key-sync.service` 上次有沒有失敗）。每一項分開印。

| 結果 | 意思 |
|---|---|
| 0 | 兩個 unit 都 active、版本一致（有給 `--tag` 時）、本機與經會合點 list 探針都回 200、key 拉取那條路可用 |
| 1 | 確定不正常：unit 沒在跑或 failed、版本不對、探針不是 200、`gh` 沒登入／讀不到金鑰 repo、解密密碼沒放、上次拉取失敗 |
| 2 | 無法確認：連不上 user bus、本機 curl 逾時、經會合點探針無法執行、**`gh` 不存在或查詢逾時** |

- 經會合點那一步是 `~/.local/bin/llmgateway-tunnel --probe-once`：讀 `tunnel.env`，用與隧道守護同一套 ssh 參數跑一次 `ssh -W 127.0.0.1:<REMOTE_API>`；回 0＝HTTP 200、1＝不是 200 或沒回應、2＝無法執行。
- 本機不是 200 時不會跑經會合點那一步；本機 200 但經會合點不是 200 表示隧道斷了或會合點的 4710 探不到，照 5.3 查。兩段探針都只證明 `GET /v1/subscriptions` 回 200。
- key-sync 那一段（v5.4）以**服務帳號**（`llmgw`）的身分問 `gh`——`gh` 的登入跟著 Unix 使用者走，用管理帳號問沒意義（`--check` 是服務帳號跑的話直接問，否則 `sudo -n -u llmgw` 過去）：
  - 沒登入／token 失效、或讀不到 `FATESAIKOU/LLMGateway-keys`（用 `gh api repos/… --jq .full_name` 問 repo 本身，**不讀任何檔案內容**）→ 回 1，印出是哪一項（照 5.1 補）。
  - 找不到 `gh`，或 `gh` 查詢逾時（`timeout`，可經 `LGW_GH_TIMEOUT` 調，預設 30 秒）→ 回 2（無法確認，不是確定壞掉）。
  - `keysync.env` 沒放、沒有非空的值、**或讀不到該檔**（權限壞掉）→ 回 1，三種分別印不同的訊息。
  - `key-sync.service` 上次失敗 → 回 1。

### 4.5 外洩時

- **key 外洩**（或懷疑）：在上游作廢並換新 → 照 4.1 更新加密檔並等一次拉取。本人已接受「能登入 sshproxy 的金鑰都讀得到全部 key」（design 決定 5a），換 key 是這種情況的標準處理。
- **客戶端裝置遺失**（例如手機）：服務沒有應用層認證，任何能登入會合點並開得了轉發的金鑰都能讀全部 key，所以裝置上有 sshproxy 的金鑰就要當成 key 已外洩。依序：
  1. 請 MyLinuxPool（mylinuxpool-pm，經 PMO）把那把金鑰從 sshproxy 撤掉；
  2. 在上游作廢並換新五把 key，照 4.1 更新加密檔等一次拉取。

  客戶端不持有 key，不用改任何客戶端設定。步驟 1 做完之前，那把金鑰仍然讀得到。
- **解密密碼外洩**：重設新密碼、整檔重加密後 push（4.1 的流程，密碼換新的），再更新 2.2 的備份 secret；服務那台的 `keysync.env` 也要換成新密碼（本人以 `llmgw` 身分改）。
- **隧道私鑰外洩**：請 mylinuxpool-pm 從 `TUNNEL_EXTRA_PUBKEYS` 拿掉舊公鑰，照 1.6 重產並登記新的，改 `tunnel.env` 後 `systemctl --user restart llmgateway-tunnel`。
- **`gh` 登入狀態外洩**：請本人以 `llmgw` 身分重新 `gh auth login`（登入狀態在 `~llmgw/.config/gh/`，LLMGateway 不碰它的值）；必要的話在上游撤掉舊的憑證。

### 4.6 退回前一版

在那台以 `llmgw` 身分：

```bash
~/.local/share/llmgateway/current/deploy/install.sh --tag <前一個 tag>
~/.local/share/llmgateway/current/deploy/install.sh --check; echo $?
```

前一版還在 `~/.local/share/llmgateway/<tag>/`，`install.sh` 會直接重用，不用連網；執行檔與 unit 也會換回那一版的。隧道守護有變更時一樣排約 60 秒後重啟（見 4.2 第 4 點）。`keys.env`、`keysync.env` 與 `tunnel.env` 不隨版本，退回不會動它們。

## 5. 排錯

### 5.1 key 沒有換（key-sync 的 log）

看 `journalctl --user -u key-sync -n 20`，每一行是 `ok …`、`failed=<種類> …`或 `bad_config …`（都不含值）。對照：

| log | 意思 | 怎麼辦 |
|---|---|---|
| `ok sha 未變，不動作` | 加密檔沒換（blob `sha` 與 `.keysync.sha` 相同） | 沒事；本人還沒 push，或已經套用過 |
| `ok 已換 keys.env`／`ok 內容沒變，不換檔` | 這次拉到新版 | 沒事 |
| `failed=gh_auth` | `gh` 沒登入或 token 失效（可能帶 `（HTTP 401）`） | 以 `llmgw` 身分重新 `gh auth login`（1.9）；`install.sh --check` 也看得出來 |
| `failed=gh_missing` | 找不到 `gh` | 裝 `gh`（1.4） |
| `failed=gh_failed`／`gh_timeout` | `gh` 執行失敗或逾時（可能是網路） | 手動跑一次 `gh api repos/FATESAIKOU/LLMGateway-keys/contents/keys.env.enc >/dev/null` 看網路；下一次 timer 再試 |
| `failed=gh_not_found` | repo 或檔案路徑不對（HTTP 404） | 確認 private repo 名稱與檔名（2.3）。**登入了但讀不到那個 repo**：以 `llmgw` 身分跑 `gh api repos/FATESAIKOU/LLMGateway-keys --jq .full_name`（只問 repo、不讀檔案內容）；404 或沒讀權限就是登入的帳號看不到這個 private repo，請本人確認 repo 名稱與帳號權限 |
| `failed=password_missing` | `keysync.env` 沒放或沒讀到 | 照 1.9 放解密密碼 |
| `failed=decrypt_failed` | 解不開（密碼錯或檔壞） | 確認 `keysync.env` 的密碼與加密時一致（1.9）；重加密後再 push（4.1） |
| `failed=format_failed` | 解密出來不是合法的 key 檔（訊息只有行號與種類） | 檢查 `keys.env.enc` 的明文格式（2.3）；重新加密後再 push |
| `failed=bad_response`／`decode_failed` | `gh api` 的回覆不是預期的 JSON／base64 | 手動跑一次同一條 `gh api` 指令看回什麼；檔案太大（> 1 MB）也會這樣 |
| `failed=write_failed` | 寫 `keys.env` 失敗 | 看 `~/.local/state/llmgateway/` 的權限與磁碟空間 |
| `failed=openssl_missing`／`code_missing`／`unexpected` | 環境缺東西（找不到 `openssl`、載不到 `lgwd` 套件）或未預期錯誤 | 照訊息補環境（1.4、1.5）；`unexpected` 要看訊息裡的例外種類再判斷 |
| `bad_config …`（結束碼 3） | 設定錯（例如 `LGWSYNC_TIMEOUT` 非法：不是數字、非有限或 ≤0），不是同步失敗 | 修正環境變數（或 CLI `--timeout` 覆蓋）；舊 key 不動，下一次 timer 再試 |

key-sync 失敗**不會**動舊 key：服務照用舊版。連續失敗才需要人看（看 log，不看 key）。要立刻套用：`systemctl --user start key-sync.service`。

### 5.2 升級／手動 ssh 進不去

- `ssh -J sshproxy@<跳板機>:<埠> -p 4711 …` 的跳板那一段 `Permission denied`：管理帳號的金鑰還沒登記到 sshproxy（請 mylinuxpool-pm 加）。
- 跳板過了但 `Connection refused`：會合點的 4711 沒人在聽，表示隧道斷了，看 5.3。這時 ssh 也進不去，要另外登入那台處理。
- `Host key verification failed`：自己的 known_hosts 少了 `[<跳板機>]:<埠>` 或（換機後）那台的 host key 變了；用 1.A.4 摘要印出的 host 公鑰比對。
- 目標那一段 `Permission denied`：sshd 擋了 `llmgw`／管理帳號（1.3），或帳號沒有該做的事的權限。

### 5.3 隧道守護一直重開

`journalctl --user -u llmgateway-tunnel` 看事件：

- `config_missing`（結束碼 78）：`tunnel.env` 的必填三項沒填，或值不能用（1.8）。這種情況**不會**自動重開，unit 停在 failed；改好 `~/.config/llmgateway/tunnel.env` 後執行 `systemctl --user restart llmgateway-tunnel.service`。（log 只寫事件種類，不會說是哪一項。）
- 一起來就結束、反覆重開：多半是遠端埠被佔住（會合點上還留著舊的隧道連線），`ExitOnForwardFailure` 讓 ssh 直接結束。舊連線在 ServerAlive 逾時（約 90 秒）後會被會合點清掉，之後自動恢復；一直不恢復就請 mylinuxpool-pm 看會合點上 4710／4711 是誰在聽。
- `probe_fail` 連續出現後重開：隧道在，但端對端打不通 `lgwd`。先看 `systemctl --user status llmgateway`，以及 `tunnel.env` 的 `LOCAL_API` 是否等於 `LGW_API_PORT`；`lgwd` 起不來時看 `journalctl --user -u llmgateway`（`keys.env` 格式壞會起不來，但 key-sync 驗過才換檔，正常不會發生）。
- 認證或 host key 失敗：隧道公鑰還沒登記（1.6），或 `KNOWN_HOSTS` 不對（1.7）。

### 5.4 自備通道打不通

依序檢查（客戶端那台）：

1. 本機有沒有在聽那個 port：`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4710/v1/subscriptions`；連不上＝轉發沒建起來。
2. 轉發那條 ssh 是否還活著（通常就是一個前景／背景的 `ssh -N -L …`）；斷了就重開。
3. 手動 ssh 到會合點本身能不能過（金鑰、帳號、埠、host key）。
4. 會合點的 4710 是否是 LLMGateway（不是別的服務）＋隧道是否活著：在會合點上 `ss -ltn | grep 4710`，或照 5.3。
5. 程式端拿到的 base URL 是否指到那個本機 port（預設 4710；改過就要跟著改）。

`lgw.client` 連不上或逾時回 `unreachable=True`（不丟例外），呼叫方當成「無法確認」，等一下再試。

### 5.5 API 回的錯誤碼

回覆是 `{"error": "<碼>"}`，完整表在 spec `subscription-registry`（design 決定 6 的 v5 版）。

| 狀態 | 碼 | 怎麼辦 |
|---|---|---|
| 400 | `bad_request` | JSON 壞掉、body 不是物件或缺必填（看回覆的 `missing`；get 少了 `?agent=`、冷卻少了 `until` 都是這個）。檢查呼叫方送的內容。 |
| 400 | `invalid_until` | `until` 要是 RFC 3339 UTC 字串（例如 `2026-10-18T00:00:00Z`）。 |
| 400 | `until_out_of_range` | `until` 在過去或超過 8 天以後。多半是客戶端判讀錯或時鐘不準；查呼叫方，不是服務的問題。 |
| 403 | `agent_not_allowed` | 見下。 |
| 404 | `not_found` | id 不存在或路徑錯（路徑對但方法錯也是 404）。id 見 `policy/default.json`。 |
| 409 | `not_deployed` | 見 5.6。 |
| 413 | `body_too_large` | body 超過 1 MB。 |
| 500 | `internal` | 服務內部錯誤，只有例外種類在 log，看 `journalctl --user -u llmgateway`。 |

客戶端側（`lgw.client` 的 `Result.error`）另有兩個自己判讀的值：`unreachable`（連不上或逾時，先看 5.4 與 5.3）與 `bad_response`（回覆不是 JSON 物件，通常是連到不是 `lgwd` 的東西，查會合點上 4710 是誰在聽）。

**403 `agent_not_allowed`**：`agent` 不在那一把的 `agents` 裡。Claude 的兩把只給 `claude-code`（Claude 條款的守門），opencode 拿 Claude 訂閱一定是 403；openrouter 只給 `opencode`（Claude Code 只用 Claude 訂閱與 Ollama，免費模型只經過 opencode 使用），claude-code 拿 OpenRouter 也是 403。這些都是預期的。其他組合要改 `policy/default.json` 並發新版。

### 5.6 `available` 為 false，或 get 回 409 `not_deployed`

- `cooldown` 有值：在冷卻中，到 `until` 自動解除，不用做任何事。冷卻中仍然 get 得到，要不要用由 sidecar 決定。
- `cooldown` 是 null：那把的 key 沒部署（`keys.env` 裡是空值或沒那行）。有 key 的話照 4.1 更新加密檔，等一次拉取；拉取失敗看 5.1。
