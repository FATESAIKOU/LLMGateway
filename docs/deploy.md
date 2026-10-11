# LLMGateway 部署與維運手冊

本手冊供本人進行「放服務的那台」之一致性開機設定、GitHub 部署環境配置、日常運作維護及故障排除。全文件嚴格遵守本公開儲存庫（public repo）之隱私與安全規範：
- 不記錄任何實際主機名稱、IP 位址、連接埠號或敏感金鑰值（皆以 `<埠>`、`<跳板機位址>` 等佔位符表示）。
- 實體機器一律稱為「放服務的那台」。
- 服務完全無資料面中轉，僅負責金鑰保管、租用分配與狀態追蹤。

---

## 1. 一次性開機設定（放服務的那台）

本節操作須具備放服務那台的 `root` 權限（或具備 `sudo`），旨在為 LLMGateway 建立專用低權限帳號與運行基底。

### 步驟 1.1：建立系統帳號 `llmgw`（不加進 docker 群組）

以 root 身分建立獨立的系統服務使用者：

```bash
useradd -m -s /bin/bash llmgw
```

- **為什麼要獨立帳號**：LLMGateway 集中保管模型存取金鑰（如 Claude OAuth token、OpenRouter API key 等），必須將金鑰檔案權限（`0600`）與執行權限嚴格限制在專用帳號下，與池管理帳號及其他服務完全隔離。
- **為什麼絕對不加進 docker 群組**：
  > [!CAUTION]
  > 在 Linux 系統中，具有存取 Docker daemon socket（即加入 `docker` 群組）權限之使用者，可藉由掛載宿主機根目錄或啟動特權容器輕易取得宿主機實質 `root` 權限。加進 `docker` 群組等同直接賦予 root 權限，將徹底打破系統帳號間的權限隔離防護。

---

### 步驟 1.2：開啟使用者 Session 常駐（Linger）

以 root 執行：

```bash
loginctl enable-linger llmgw
```

- **為什麼**：LLMGateway 作為 `llmgw` 的 systemd user unit（`systemctl --user`）運行。在 systemd 預設機制下，非登入使用者的 user manager 只會在互動登入時啟動，登出時隨即停止。開啟 `linger` 後，系統開機時即會為 `llmgw` 啟動 user manager 並持續常駐，確保服務在開機時自動執行且無使用者登入時仍穩定常駐。

---

### 步驟 1.3：確認 SSH 伺服器允許 `llmgw` 登入

檢查 `/etc/ssh/sshd_config` 或 `/etc/ssh/sshd_config.d/*.conf`：

1. 若設定有 `AllowUsers` 白名單，確認已包含 `llmgw`（例如：`AllowUsers ... llmgw`）。
2. 若設定有 `DenyUsers`，確認未封鎖 `llmgw`。
3. 確認已啟用公鑰認證（`PubkeyAuthentication yes`）。
4. 若有修改設定，重載 SSH 服務：
   ```bash
   systemctl reload sshd
   ```

- **為什麼**：GitHub Actions 自動化部署工作流程（`deploy-keys` 與 `deploy-service`）必須經由跳板機以 `llmgw` 使用者身分透過 SSH 連線至放服務的那台。若 sshd 存取控制阻擋該使用者，部署連線將直接被拒絕。

---

### 步驟 1.4：確認 Python 執行環境版本（>= 3.10）

檢查系統 Python 3 版本：

```bash
python3 --version
```

- **為什麼**：LLMGateway 伺服器與管理模組（`lgwd`、`lgw.admin`）完全依賴 Python 標準函式庫，並採用了 Python 3.10 引入之型別聯合語法（如 `str | None`，PEP 604）及標準模組能力。若 Python 版本低於 3.10，程式將在語法解析階段直接失敗。

---

### 步驟 1.5：首次安裝服務（以 `llmgw` 身分）

切換為 `llmgw` 使用者進行安裝：

```bash
sudo -u llmgw -i
```

拉取指定之固定發行版本（Git tag，例如 `lgw-v0.1.0`）：

```bash
mkdir -p ~/.local/share/llmgateway/releases
git clone --depth 1 --branch <tag> https://github.com/FATESAIKOU/LLMGateway.git ~/.local/share/llmgateway/releases/<tag>
```

執行安裝腳本進行原子切換與服務初始化：

```bash
~/.local/share/llmgateway/releases/<tag>/deploy/install.sh --tag <tag>
```

安裝腳本會完成：
1. 建立資料狀態目錄 `~/.local/state/llmgateway/`（權限 `0700`）。
2. 將 `current` 符號連結原子切換至該發行版：`~/.local/share/llmgateway/current -> ~/.local/share/llmgateway/releases/<tag>`。
3. 安裝 systemd user unit 檔案至 `~/.config/systemd/user/llmgateway.service`。
4. 載入並啟用服務：`systemctl --user daemon-reload && systemctl --user enable --now llmgateway.service`。

- **為什麼**：以服務帳號身分拉取固定發行標籤（tag），保證所有目錄與檔案擁有者皆為 `llmgw`。透過符號連結 `current` 指向版本，可確保部署切換與事後版本回滾的原子性。

---

> **綁定位址**：`~/.config/llmgateway/llmgateway.env` 的 `LGW_API_HOSTS` 要分別列出 `127.0.0.1` 與 docker bridge 的位址，**不要用 `0.0.0.0`**。管理 API 只接受綁在 loopback 上收到的請求，綁 `0.0.0.0` 時一律拒絕；v0 只支援 IPv4。

### 步驟 1.6：配置部署專用公鑰至 `authorized_keys`

在放服務的那台，切換為 `llmgw` 身分：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
```

編輯 `~/.ssh/authorized_keys`（權限 `0600`），填入以下單一行格式：

```text
command="/home/llmgw/.local/share/llmgateway/current/deploy/remote-deploy.sh",restrict <部署公鑰內容>
```

- **為什麼**：
  - `command="/home/llmgw/.../remote-deploy.sh"`：強制此公鑰登入時僅能執行專用部署腳本，不開放互動式 shell 或任意指令執行，徹底限縮部署通道之暴露面。腳本內部亦對 `SSH_ORIGINAL_COMMAND` 進行白名單檢查（僅放行 `keys` 與 `upgrade lgw-vX.Y.Z`）。
  - `restrict`：OpenSSH 限制旗標，停用 port-forwarding、agent-forwarding、X11-forwarding、pty 分配等所有非必要能力，符合最小特權原則。
  - 跳板機端與放服務的那台使用同一把部署金鑰，跳板端僅允許轉發至放服務那台的隧道埠，那台則強制綁定部署腳本。

---

### 步驟 1.7：簽發發證管理憑證（Issuer Token）

在放服務的那台，以 `llmgw` 身分執行管理工具，為 MyLinuxPool 建立發證者：

```bash
cd ~/.local/share/llmgateway/current
python3 -m lgw.admin client add mlp-issuer --role issuer
```

執行後標準輸出僅會印出一次憑證字串（32 bytes URL-safe 隨機字串）：

> [!IMPORTANT]
> **安全警示**：此發證憑證的明文僅在此印出唯一一次，資料庫內僅保存其 SHA-256 雜湊值。請本人立即複製並設為 MyLinuxPool 的 GitHub secret `GH_MYLINUXPOOL__LLMGATEWAY_ADMIN__ISSUE`，**絕對不要貼進任何對話紀錄、issue、PR、工單或檔案中**。

- **為什麼**：MyLinuxPool 的 `create-worker` 在建立 worker 容器時，需自動向 LLMGateway 管理 API 發行客戶端憑證，並在 `delete-worker` 時撤銷。`issuer` 角色僅能呼叫管理 API（且白名單限制只能簽發撤銷符合 `worker-*` 命名的客戶端），不具備租用模型的權限，落實職權劃分。

---

### 步驟 1.8：簽發 Mac 客戶端憑證

在放服務的那台，以 `llmgw` 身分簽發 Mac 本機使用的客戶端憑證：

```bash
cd ~/.local/share/llmgateway/current
python3 -m lgw.admin client add mac-client --role client
```

將終端機印出的憑證明文妥善複製到 Mac 本機，存入設定檔並限定權限：

```bash
mkdir -p ~/.config/lgw
printf '%s' '<憑證明文>' > ~/.config/lgw/client-token
chmod 600 ~/.config/lgw/client-token
```

- **為什麼**：Mac 本地執行 `sidecar agent run claude` 或 `sidecar agent run opencode` 時，內部 `lgw` 客戶端讀取 `~/.config/lgw/client-token` 進行 Bearer 身分驗證。權限設為 `0600` 可防止同機其他未授權使用者讀取。

---

## 2. 本人在 GitHub 的設定

為了讓 GitHub Actions 能夠安全連線放服務的那台以更新金鑰與程式碼，本人須在本 repo（public repo）的 Settings 進行環境保護與 Secrets 設定。

### 2.1 建立 Environment「`llmgateway-deploy`」

1. 前往本 repo 的 **Settings** -> **Environments**。
2. 點選 **New environment**，名稱填入 `llmgateway-deploy`。
3. 勾選 **Required reviewers**，並將**本人**加入審核者名單。

- **為什麼**：部署工作流程涉及伺服器登入私鑰與真實付費 API 金鑰。設定 Required reviewer 為本人，能確保每次工作流程觸發（手動 `workflow_dispatch`）時，皆須本人確認無誤後才釋放秘密與執行連線，防止意外或未經授權之秘密派發。

---

### 2.2 設定 9 個 Secrets

依據 tasks 3.3 與命名規範 `<存取方>__<被存取方>__<動作>`，在 `llmgateway-deploy` environment（或 repo secrets）中設定下列 9 個 secret：

#### (A) 部署連線設定（4 個）

| Secret 名稱 | 格式範例 / 規範 | 說明與為什麼 |
|---|---|---|
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY` | OpenSSH 私鑰 PEM 文字<br>（以 `-----BEGIN ... PRIVATE KEY-----` 開頭） | 部署專用私鑰。對應放服務那台 `~llmgw/.ssh/authorized_keys` 與跳板機授權之公鑰。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_KNOWN_HOSTS` | 標準 known_hosts 格式文字<br>（每行一個主機指紋條目） | 跳板機與放服務那台之 SSH host key 指紋。**必須使用 MyLinuxPool 事先提供的可信指紋建立，切勿使用 `ssh-keyscan` 現掃**（防止初次連線遭中間人攻擊 TOFU 缺陷）。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_JUMP` | `user@host:port` | 跳板機連線資訊，供 SSH `-J` 代理跳轉使用。 |
| `GH_LLMGATEWAY__POOL_SSH__DEPLOY_TARGET` | `llmgw@127.0.0.1:<埠>` | 放服務那台經由跳板看到的目標位址。**字面必須與跳板機 `permitopen` 設定之 `"127.0.0.1:<埠>"` 一模一樣**，OpenSSH 比對不一致將拒絕轉發。 |

> [!NOTE]
> 連線資訊之所以一併放入 GitHub Secrets 而非公開的 Repository Variables，是因為本 repo 為公開倉庫，不得洩漏私人基礎架構之跳板機位址與內部轉發埠口。

#### (B) 模型存取金鑰（5 個）

金鑰值由本人直接輸入，不可包含任何換行、空白字元或引號：

| Secret 名稱 | 內容與格式 | 說明 |
|---|---|---|
| `POOL_LLMGATEWAY__CLAUDE_PERSONAL__LLM` | Claude 個人訂閱 OAuth token | 對應 `CLAUDE_PERSONAL_OAUTH_TOKEN`。 |
| `POOL_LLMGATEWAY__CLAUDE_COMPANY__LLM` | Claude 公司訂閱 OAuth token | 對應 `CLAUDE_COMPANY_OAUTH_TOKEN`。 |
| `POOL_LLMGATEWAY__OPENROUTER_API__LLM` | OpenRouter API 金鑰 | 對應 `OPENROUTER_API_KEY`。 |
| `POOL_LLMGATEWAY__OPENCODE_ZEN__LLM` | OpenCode Zen API 金鑰 | 對應 `OPENCODE_ZEN_API_KEY`（若尚未開通可填空字串，系統將視為未部署）。 |
| `POOL_LLMGATEWAY__OLLAMA_CLOUD__LLM` | Ollama Cloud API 金鑰 | 對應 `OLLAMA_API_KEY`。 |

---

## 3. 日常維運

### 3.1 部署金鑰（執行 `deploy-keys`）

當需要更新或輪替任何模型 API 金鑰時：

1. 更新 GitHub 中對應的 `POOL_LLMGATEWAY__*` Secret。
2. 至 GitHub Actions 頁面，選擇 **deploy-keys** 工作流程，點擊 **Run workflow**。
3. 本人收到審核通知，確認後點擊核准執行。
4. 工作流程透過 SSH 將 5 把 key 經由標準輸入（stdin）安全傳入放服務的那台：
   - `remote-deploy.sh` 進行格式驗證。
   - 以 `0600` 權限寫入暫存檔，原子更名（rename）覆寫 `~/.local/state/llmgateway/keys.env`。
   - 送出 `SIGHUP` 訊號通知常駐服務熱重讀金鑰。

- **為什麼**：金鑰值不經過命令列參數（不暴露於 `ps` 程序表）、不記錄於 GitHub Actions 日誌、落盤採原子更名避免讀取到半寫入狀態，重讀亦不中斷現有服務連線。

---

### 3.2 升級服務程式碼（執行 `deploy-service`）

當本 repo 發布新版並打上發行標籤（格式 `lgw-vX.Y.Z`）時：

1. 至 GitHub Actions 頁面，選擇 **deploy-service** 工作流程，點擊 **Run workflow**。
2. 在輸入框填入欲部署的標籤名稱（例如 `lgw-v0.2.0`）。
3. 本人核准後，工作流程以 SSH 呼叫 `upgrade lgw-vX.Y.Z`：
   - 遠端腳本驗證標籤正規表示式。
   - 呼叫 `install.sh` 下載該標籤程式碼、切換 `current` 符號連結並重啟 user unit。

---

### 3.3 檢視 HTML 狀態板（Mac 經 SSH 埠轉送）

LLMGateway 內建只讀 HTML 狀態板（預設埠 8481），用於即時監控金鑰部署狀況、各訂閱租約數與冷卻倒數。

- **網路隔離策略**：狀態板 HTTP 伺服器嚴格僅綁定在放服務那台的 **loopback（`127.0.0.1`）**，不向池外公開，亦不開放給 worker 容器存取。
- **檢視方式（本機埠轉送）**：
  在 Mac 終端機執行 SSH 轉發：
  ```bash
  ssh -J <跳板機連線字串> -L 8481:127.0.0.1:8481 <放服務那台的使用者@主機>
  ```
  連線建立後，在 Mac 本機瀏覽器開啟：
  ```text
  http://127.0.0.1:8481/board
  ```

- **為什麼**：狀態板顯示 worker 機器名稱、使用中的租約與額度資訊，限制在 loopback 可確保僅具備跳板 SSH 存取身分之本人可觀看；且 HTML 狀態板不需代入客戶端 token，杜絕 token 洩漏於瀏覽器網址列或 Proxy 存取日誌之風險。

---

### 3.4 服務健康檢查（`--check` 三態）

在放服務的那台，可直接透過部署腳本的檢查介面檢驗服務運行狀態：

```bash
/home/llmgw/.local/share/llmgateway/current/deploy/install.sh --check
```

檢查結果以 Exit Code 表示三態語義：

| 狀態碼 | 狀態語義 | 意義與判定標準 |
|---|---|---|
| **0** | 健康正常 | 服務正在執行、版本與 current 一致，且本機測試 `GET /v1/board` 在不帶憑證情況下正確回傳 `401 Unauthorized`（表示服務正在監聽且身分驗證機制正常）。 |
| **1** | 確定不成立 | 服務明確異常：程序未運行、systemd unit 處於 failed 狀態、連接埠無法連線，或伺服器回應非預期狀態（例如未驗證請求未回 401）。 |
| **2** | 無法確認 | 因環境干擾或未明原因無法判定，例如檢查指令超時、無法連接本機 user bus 等。 |

- **為什麼**：定義嚴謹的三態語義可讓自動化監控與維運工具準確辨認「確定服務失效（1）」與「檢測環境未定（2）」，避免盲目重啟。

---

### 3.5 撤銷客戶端憑證

若某台機器退役、憑證洩漏或不再授權，在放服務的那台以 `llmgw` 身分執行撤銷：

```bash
cd ~/.local/share/llmgateway/current
python3 -m lgw.admin client rm <客戶端名稱>
```

查看目前已註冊之客戶端清單與撤銷時刻：

```bash
python3 -m lgw.admin client list
```

- **為什麼**：撤銷客戶端除了將其標記為失效阻斷未來的請求外，服務核心會啟動聯動清理（cascade），將該客戶端名下所有尚未結束的 active/granted 租約一併終止並釋放佔用配額。

---

### 3.6 版本回滾（Rollback）

若升級後發生相容性問題，可直接在放服務的那台執行秒級回滾：

1. 切換為 `llmgw` 身分：
   ```bash
   sudo -u llmgw -i
   ```
2. 將 `current` 符號連結指回前一個發行版本：
   ```bash
   ln -sfn ~/.local/share/llmgateway/releases/<前一個tag> ~/.local/share/llmgateway/current
   ```
3. 重啟服務單元：
   ```bash
   systemctl --user restart llmgateway.service
   ```
4. 執行健康檢查驗證：
   ```bash
   ~/.local/share/llmgateway/current/deploy/install.sh --check
   ```

- **為什麼**：過往版本均完整保留在 `releases/` 目錄中，透過原子符號連結更換與重啟即可迅速回到先前穩定版本，不需重新連網下載或重建設定。

---

## 4. 故障排除

### 4.1 `deploy-keys` 回傳 Exit Code 4（格式錯誤）

- **現象**：GitHub Actions 的 `deploy-keys` 工作流程執行失敗，Log 印出 `deploy-keys: ...` 錯誤且 exit code 為 `4`。
- **根本原因**：傳入的金鑰資料未通過 `remote-deploy.sh` 嚴格的防禦性格式檢驗：
  1. 行格式不符合 `KEY=VALUE`。
  2. 含有非預期的鍵名（不在允許的 5 把白名單內）。
  3. 出現重複定義的鍵名。
  4. 金鑰值包含空白字元、換行符號、單引號（`'`）或雙引號（`"`）。
  5. 缺少了 5 把必要金鑰名稱中的任何一把（即使未啟用也必須提供鍵名，值留空）。
- **處理方式**：
  1. 檢查 GitHub repository 中的 5 個 `POOL_LLMGATEWAY__*` Secret。
  2. 確保複製金鑰時未夾帶前後空格、換行或引號。
  3. 確認全部 5 個 Secret 皆已建立；若某金鑰暫無（如 Zen），請將該 Secret 內容設為空字串，勿留空未建。

---

### 4.2 `deploy-keys` 回傳 Exit Code 5（重讀失敗）

- **現象**：GitHub Actions 顯示紅燈，Log 印出：
  `deploy-keys: written, but reload failed (service will read it on next start)`
- **根本原因**：
  - 新的 `keys.env` 檔案已成功以 `0600` 權限寫入磁碟。
  - 但腳本執行重載指令 `systemctl --user kill -s HUP llmgateway.service` 失敗。
  - 此情況通常發生在非互動式（non-interactive）SSH session 中，系統未自動注入 user session 環境變數 `XDG_RUNTIME_DIR`（例如 `/run/user/<uid>`），導致 `systemctl --user` 無法與 user D-Bus 溝通。
- **處理方式**：
  - **安全性保證**：此為 safe-failure 機制，新金鑰已安全落盤，現有服務依然維持運行，並會在下次重啟時自動讀取新金鑰。
  - **手動介入重載**：本人或管理員登入放服務的那台，手動發送重讀訊號：
    ```bash
    systemctl --user kill -s HUP llmgateway.service
    ```
    或重啟服務：
    ```bash
    systemctl --user restart llmgateway.service
    ```
  - **根本改善**：檢查該機器的 `/etc/pam.d/sshd`，確保啟用 `pam_systemd.so`，以確保 SSH session 正確建立 systemd runtime 環境。

---

### 4.3 請求租約遭拒：`403 client_mismatch`

- **現象**：客戶端申請租約（`POST /v1/leases`）時收到 HTTP 狀態碼 `403`，錯誤碼為 `client_mismatch`。
- **根本原因**：
  - 客戶端在請求 JSON body 中提供的 `worker` 欄位值，與其攜帶的 `Authorization: Bearer <token>` 在伺服器端資料庫中登記的客戶端名稱（client name）**不一致**。
  - 設計決定 4 規範：客戶端不能冒用其他機器的名字租用模型。
- **處理方式**：
  1. 在客戶端使用 `whoami` 端點核對憑證身分：
     ```bash
     curl -H "Authorization: Bearer $(cat <憑證路徑>)" http://127.0.0.1:<埠>/v1/whoami
     ```
     伺服器將回傳 `{"client": "<登記名稱>", "role": "client"}`。
  2. 確認呼叫租約 API 時傳入之 `worker` 參數值與登記之 `<登記名稱>` 完全吻合。

---

### 4.4 狀態板顯示「未部署」或租約申請收到 `409 not_allowed`

- **現象**：
  - 在狀態板上某個訂閱的部署狀態顯示為「未部署」。
  - 客戶端指定該訂閱（或候選訂閱皆為此狀態）申請租約時，伺服器回傳 `409 {"error": {"reason": "not_allowed"}}`。
- **根本原因**：
  - 在 `~/.local/state/llmgateway/keys.env` 檔案中，該訂閱對應的環境變數值為空（例如 `OPENCODE_ZEN_API_KEY=`）或該鍵未在檔案中。
  - 服務端原則：未部署的金鑰一律不出租，並從候選順序中跳過；若無任何可用金鑰則拒絕。
- **處理方式**：
  1. 若該訂閱本來就尚未開通（例如暫無 Zen 帳號），此為預期中的防禦性降級行為。
  2. 若已擁有該金鑰但忘記派發，前往 GitHub 更新對應的 `POOL_LLMGATEWAY__*` Secret，並重新執行 `deploy-keys` 工作流程。
