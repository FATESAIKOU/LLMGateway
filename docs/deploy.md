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

需要 root。依 tasks 3.3，由本人做或本人授權後做。

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
4. `daemon-reload`；啟用並重啟 `llmgateway.service`；啟用 `llmgateway-tunnel.service`。

這時 key 還沒部署，list 裡每一把的 `available` 都是 false；隧道設定也還是空的，隧道守護起不來，這是預期的，1.8 填好就好。

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

- `LOCAL_API` 要和 `llmgateway.env` 的 `LGW_API_PORT` 一樣。
- 4710、4711 是 MyLinuxPool 在會合點上替 LLMGateway 保留的埠（`NODE_GATEWAY.ports.llmgateway`），避開 2220–2499 與 3000–3999。

```bash
systemctl --user enable --now llmgateway-tunnel.service
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

`--check` 回 0、`curl` 印 200。接著從 Mac 照 3.1 連一次。

## 2. 本人在 GitHub 的設定

### 2.1 Environment `llmgateway-deploy`

Settings → Environments → New environment，名稱 `llmgateway-deploy`，勾 **Required reviewers** 並加本人。兩個 workflow 都只能手動觸發（`workflow_dispatch`），每次都要本人核准才會拿到 secret。

### 2.2 Secrets（放在 `llmgateway-deploy` environment）

命名照本人的規則 `<存取方>__<被存取方>__<動作>`。值由本人設，AI 不經手。

**部署連線（4 個）**

| Secret | 格式 | 說明 |
|---|---|---|
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY` | OpenSSH 私鑰全文 | 部署私鑰，公鑰見 1.9。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS` | known_hosts，兩行 | `[<跳板機>]:<埠> <跳板機的 host key>`，與 `[127.0.0.1]:4711 <放服務那台的 host key>`。用 MyLinuxPool 提供的指紋與那台的 `/etc/ssh/ssh_host_ed25519_key.pub` 建，不要現掃。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_JUMP` | `sshproxy@<跳板機>:<埠>` | 會合點。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_TARGET` | `llmgw@127.0.0.1:4711` | 固定值：經會合點看到的、放服務那台的 sshd。 |

連線資訊也放 secret，是因為 repo 是 public，不把池的位址放在公開的 vars；workflow 印 ssh 錯誤時也會把位址換成 `[jump]`／`[target]`。

**五把 key（5 個）**

值不能有空白、換行、引號。**還沒有的那把就不要建 secret**：workflow 會送出空值，服務視為「沒部署」（list 的 `available` 為 false，get 回 409）。

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

Mac 用自己已經能登入 sshproxy 的金鑰。建 `~/.config/lgw/connect.env`：

```sh
JUMPS=
RENDEZVOUS=sshproxy@<跳板機>:<埠>
IDENTITY=/Users/<你>/.ssh/<能登入 sshproxy 的金鑰>
KNOWN_HOSTS=/Users/<你>/.ssh/known_hosts_rendezvous
REMOTE_API=4710
```

- `JUMPS`：要先經過其他跳板時，用逗號分隔的 `user@host:port`；直接連得到會合點就留空。
- `KNOWN_HOSTS` 和 1.7 一樣，用 MyLinuxPool 提供的 host key。

```bash
python3 -m lgw.connect --config ~/.config/lgw/connect.env
# 印出一行 http://127.0.0.1:<臨時埠>，Ctrl-C 結束
curl -s http://127.0.0.1:<臨時埠>/v1/subscriptions
```

程式裡則是 `with lgw.connect.connect(cfg) as base_url:`，再用 `lgw.client` 的 list／get／cooldown。

### 3.2 worker

worker 的 sidecar 用 pool-tunnel 既有的 `/home/worker/.ssh/id_tunnel`，連線資訊從 `/run/mlp-gateway/gateway.json`（位址、帳號、埠、host key）填進 `ConnectConfig`，並固定 host key。這部分在 sidecar 那一側（MyAiStorage），LLMGateway 不需要知道有新 worker。

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
4. 隧道守護有變更時，`install.sh` 不當場重啟它（部署連線本身就走這條隧道，當場重啟會切斷部署），而是排一個約 60 秒後的重啟；這段時間 list／get 會斷一下，客戶端重連即可。

### 4.3 看狀態

- 誰能不能用：在那台 `curl -s http://127.0.0.1:8480/v1/subscriptions`，或從 Mac 照 3.1。每一筆有 `cooldown`（`5h`、`1w` 各為 `{until}` 或 null）與 `available`。
- 誰在何時 get 過哪一把、誰登錄過冷卻：`llm.db` 的 `audit` 表（不分客戶端，因為不做認證）。
- 「誰正在用哪一把」：看各 sidecar 的 status，LLMGateway 不追蹤。
- log：`journalctl --user -u llmgateway -u llmgateway-tunnel`。log 裡不會有 key 的值，也不會有主機名。

### 4.4 `install.sh --check`（三態）

| 結果 | 意思 |
|---|---|
| 0 | 兩個 unit 都 active、版本與 `current` 一致（有給 `--tag` 時）、本機 `GET /v1/subscriptions` 回 200 |
| 1 | 確定不正常：unit 沒在跑或 failed、版本不對、探針不是 200 |
| 2 | 無法確認：連不上 user bus、curl 逾時等 |

### 4.5 外洩時

- **key 外洩**（或懷疑）：在上游作廢並換新 → 更新 secret → `deploy-keys`。本人已接受「能登入 sshproxy 的金鑰都讀得到全部 key」（design 決定 5a），換 key 是這種情況的標準處理。
- **隧道私鑰外洩**：請 mylinuxpool-pm 從 `TUNNEL_EXTRA_PUBKEYS` 拿掉舊公鑰，照 1.6 重產並登記新的，改 `tunnel.env` 後 `systemctl --user restart llmgateway-tunnel`。
- **部署私鑰外洩**：從 `TUNNEL_EXTRA_PUBKEYS` 與 `~llmgw/.ssh/authorized_keys` 拿掉，重產一對，更新 secret。

### 4.6 退回前一版

在那台以 `llmgw` 身分：

```bash
~/.local/share/llmgateway/current/deploy/install.sh --tag <前一個 tag>
~/.local/share/llmgateway/current/deploy/install.sh --check; echo $?
```

前一版還在 `~/.local/share/llmgateway/<tag>/`，`install.sh` 會直接重用，不用連網；執行檔與 unit 也會換回那一版的。也可以從 GitHub 用 `deploy-service` 輸入前一個 tag。

## 5. 排錯

### 5.1 `deploy-keys` 結束碼 4（格式錯誤）

`remote-deploy.sh` 拒收：行不是 `NAME=value`、名字不在五個裡、重複、缺名字、值有空白／換行／引號。檢查五個 `POOL_LLMGATEWAY__*` secret 有沒有夾到前後空白或換行。沒有的那把不要建 secret（見 2.2）。

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

- 一起來就結束、反覆重開：多半是遠端埠被佔住（會合點上還留著舊的隧道連線），`ExitOnForwardFailure` 讓 ssh 直接結束。舊連線在 ServerAlive 逾時（約 90 秒）後會被會合點清掉，之後自動恢復；一直不恢復就請 mylinuxpool-pm 看會合點上 4710／4711 是誰在聽。
- `probe_fail` 連續出現後重開：隧道在，但端對端打不通 `lgwd`。先看 `systemctl --user status llmgateway`，以及 `tunnel.env` 的 `LOCAL_API` 是否等於 `LGW_API_PORT`。
- 認證或 host key 失敗：隧道公鑰還沒登記（1.6），或 `KNOWN_HOSTS` 不對（1.7）。

### 5.5 `lgw connect` 回「無法確認」（`Unavailable`）

訊息只有錯誤種類，不含位址。依序檢查：`connect.env` 的 `RENDEZVOUS`／`IDENTITY`／`KNOWN_HOSTS`；手動 `ssh -i <金鑰> -p <埠> sshproxy@<跳板機>` 能不能過；伺服器端隧道（5.4）。

### 5.6 get 回 403 `agent_not_allowed`

`agent` 不在那一把的 `agents` 裡。Claude 的兩把只給 `claude-code`（Claude 條款的守門），opencode 拿 Claude 訂閱一定是 403，這是預期的。其他組合要改 `policy/default.json` 並發新版。

### 5.7 `available` 為 false，或 get 回 409 `not_deployed`

- `cooldown` 有值：在冷卻中，到 `until` 自動解除，不用做任何事。冷卻中仍然 get 得到，要不要用由 sidecar 決定。
- `cooldown` 都是 null：那把的 key 沒部署（`keys.env` 裡是空值）。有 key 的話更新 secret 再跑 `deploy-keys`。
