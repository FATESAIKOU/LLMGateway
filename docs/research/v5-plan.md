# v5 實作計畫（C）：金鑰自己拉、初次引導、估工、疑點

前言：v5 五件事見 `openspec/changes/gateway-v0/proposal.md`「v5 變更」；spec 見同目錄三份 delta；檔案盤點見 `v5-inventory.md`。本檔是 v5.4（金鑰正本改 private repo 加密檔、服務自己拉）的實作提案，外加估工與本人第二輪裁定（2026-10-12 同日）。

這是 public repo：本檔只有變數名、檔名、流程，沒有任何值、機器名、IP、私人系統名稱。

## 1. 金鑰拉取流程（本人第二輪：`gh` 方式，不用 deploy key）

### 1.1 元件

- **key-sync**：新增小 script（放 `ops-scripts/`；標準函式庫以外只用 `gh` 與 `openssl` 兩個外部指令；不用 `git`，本機不留 mirror），跑在服務那台的 `llmgw` 身分，由 systemd timer 約每 10 分鐘觸發一次；也允許本人手動觸發跑一次（提早生效，不用等 10 分鐘）。
- **key 檔**：沿用 `~/.local/state/llmgateway/keys.env`（0600，`llmgw`），格式不變（`lgwd/keys.py` 照用）。
- **版本記錄**：`~/.local/state/llmgateway/.keysync.sha`，記上次套用的 blob `sha`（純文字，本人讀得到）。

### 1.2 時序（每次觸發）

1. **拉取**：`gh api repos/FATESAIKOU/LLMGateway-keys/contents/keys.env.enc`（走 `llmgw` 的登入狀態，見 §2），取回 base64 內容與 blob `sha`。不用 clone／fetch：少在本機留一份 mirror，少管一份狀態。
2. **比對版本**：`sha` 與 `.keysync.sha` 相同 → 直接結束（不解碼、不解密、不碰 key 檔）。不同才往下。
3. **解碼＋解密**：base64 解碼 → `openssl enc -d -aes-256-cbc -pbkdf2 -iter 10000 -a -pass env:LLMGW_KEYS_PASSWORD` 解密。密碼只經環境變數傳，**不用 `-pass pass:`**（會上命令列、`ps` 看得到）。
4. **驗證格式**：解密結果丟給現有的 key 檔解析（同 `lgwd/keys.py` 規則：五個 `KEY=VALUE` 都要有、不重複、值不含空白引號）。不過就停。
5. **原子替換**：驗過才寫暫存檔（0600）→ rename 成 `keys.env`；再把新 `sha` 寫進 `.keysync.sha`。
6. **`lgwd` 重讀（提案：自己偵測）**：`lgwd` 每次處理請求前 `stat` 一次 `keys.env` 的 mtime，變了就重讀（沿用現有 `reload()` 語意：失敗沿用舊的、記 log、不含值）。**不推薦**由 key-sync 送 SIGHUP：多一個跨行程約定，mtime 偵測少一個動件。`llmgateway.service` 的 `ExecReload` HUP 行一併拿掉。
7. **失敗時保留舊版**：上面 1–5 任一步失敗（`gh` 沒登入、token 失效、斷網、`sha` 讀不到、解密失敗、格式不過）→ key 檔不動、`sha` 不寫、記一行 log（不含值），服務照用舊 key；下一次 timer 再試。連續失敗才需要人看（看 log，不看 key）。

### 1.3 加密檔格式

- **位置**：private repo `FATESAIKOU/LLMGateway-keys` 根目錄，**檔名 `keys.env.enc`**（已定）。
- **加密（本人在自己電腦做）**：`openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt -pass env:…`（32 字元隨機英數密碼，同 MyLinuxPool 做法；`-pass` 只用 `env:`／`fd:`）。
- **解密後內容**：五行 `KEY=VALUE`（名字沿用：`CLAUDE_PERSONAL_OAUTH_TOKEN`、`CLAUDE_COMPANY_OAUTH_TOKEN`、`OPENROUTER_API_KEY`、`OPENCODE_ZEN_API_KEY`、`OLLAMA_API_KEY`），外加一行人類看的版本註解（例 `# version: 2026-10-12-01`；機器不靠它判斷，只供本人對帳）。
- **版本判斷**：機器只認 `gh api` 回傳的 blob `sha`（§1.2 第 2 步）＋解密後內容的 sha256（與現行 `keys.env` 比，不同才寫，避免 mtime 空轉）。不另設版本號欄位，加密檔格式以後想加欄位也不用改比對邏輯。

## 2. 初次啟動（`gh` 前提；LLMGateway 不做登入引導）

前提：`keys.env` 不存在時 `lgwd` 照舊不啟動。順序：`gh` 登入 → 放解密密碼 → 手動跑一次 key-sync 拉下首版 → `--check` 過。

### 2.1 `gh` 登入（本人，以 `llmgw` 身分）

- `gh` 的登入跟著 Unix 使用者走；key-sync 以 `llmgw` 跑，所以本人 `sudo -u llmgw -i` 後執行 `gh auth login`，token 存在 `~llmgw/.config/gh/`（0600）。
- **認證方式本人已定（2026-10-12，選 B）**：`llmgw` 用瀏覽器 `gh auth login` 登入，**不用** `--with-token` 的 token 檔；憑證由 `gh` 自己管在 `~llmgw/.config/gh/`（0600），LLMGateway 不碰、不複製、不轉發它的值。
- 選 B 的後果（**本人已知並接受**）：`gh` 的登入狀態對應本人帳號的一般權限，拿著這台 `llmgw` 登入狀態的人可能讀寫本人其他 repo，所以這台不能當成完全隔離的機器。相應地，`install.sh --check` 除了問「登入了沒」，還要實際問一次讀不讀得到 `FATESAIKOU/LLMGateway-keys`（`gh api repos/… --jq .full_name`，只問 repo 本身、不讀檔案內容）；讀不到就是不能用。
- 不用 deploy key（本人裁定）：免在 repo 登記公鑰、免管 ssh host key；代價是服務那台要有 `gh` 且登入狀態有效，失效時 key 凍在舊版（§1.2 第 7 步）。

### 2.2 解密密碼的放置

- 本人提供解密密碼（bootstrap 引導：互動輸入或 `--decrypt-pass-file <檔>`，檔 0600、用完就刪），寫入 `~/.config/llmgateway/keysync.env`（**0600，擁有者 `llmgw`**），內容只有一行 `LLMGW_KEYS_PASSWORD=…`。
- key-sync 的 systemd unit 用 `EnvironmentFile=` 讀它，再以 `-pass env:LLMGW_KEYS_PASSWORD` 調 openssl。密碼不進 repo、不進 log、不上命令列。
- 密碼遺失＝加密檔打不開＝換不了 key：重建方式是本人重設新密碼、整檔重加密後 push（§3.3 同流程）。所以密碼另有一份在 GitHub secret 備份（§3.2）。

## 3. 需要本人做的事

### 3.1 開 private repo `FATESAIKOU/LLMGateway-keys`（已定）

- private（key 正本，不進 public repo；只有本人用）。
- 根目錄放 `keys.env.enc`（§1.3）。

### 3.2 secret（命名照 `<存取方>__<被存取方>__<動作>`；乙已定）

- 新增 **`GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`**（解密密碼的備份；參考既有的 `GH_MYLINUXPOOL__LINODE_GATEWAYFILES__DECRYPT`）：放本 repo 的 `llmgateway-deploy` environment（required reviewer＝本人）；AI 不經手值。
- 收掉的：五把 `POOL_LLMGATEWAY__*__LLM`、部署連線 4 個（`GH_LLMGATEWAY__POOL_SSH__DEPLOY*`）。
- MyLinuxPool 那側 `TUNNEL_EXTRA_PUBKEYS` 裡的**部署公鑰**請 mylinuxpool-pm 拿掉；隧道公鑰照留。

### 3.3 更新金鑰檔的步驟（本人，每次換 key）

1. 在自己電腦寫明文（五行 `KEY=VALUE`，值怎麼拿照舊；**AI 不經手值**）。
2. `openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt` 加密成 `keys.env.enc`（密碼經 `env:` 傳）。
3. 刪掉明文（`shred -u` 或同等）。
4. commit＋push 到 `FATESAIKOU/LLMGateway-keys`。
5. 等一次拉取（≤約 10 分鐘；趕時間就上服務那台手動觸發 key-sync 跑一次）。
6. 驗證：`curl` 經本機通道打 list（`available` 回來了）或等 sidecar 換把成功。log 只看 key-sync 的成敗行。

## 4. 本人要做的事（照順序）

1. **建 repo**：開 private repo `FATESAIKOU/LLMGateway-keys`（只有本人用）。
2. **加密上傳 `keys.env.enc`**（本人在自己電腦；AI 不經手值）：
   ```bash
   export LLMGW_KEYS_PASSWORD='...'  # 32 字元隨機英數，本人自己產生；用完就 unset
   openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -a -salt -pass env:LLMGW_KEYS_PASSWORD -in keys.env -out keys.env.enc
   unset LLMGW_KEYS_PASSWORD
   shred -u keys.env  # 刪明文
   git add keys.env.enc && git commit -m 'update keys' && git push
   ```
3. **`gh` 登入（以 `llmgw` 身分）**：`sudo -u llmgw -i` 後 `gh auth login`（選 B：用瀏覽器登入，不放 token 檔；說明見 §2.1）。
4. **加 secret**：本 repo `llmgateway-deploy` environment 加 `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`（解密密碼備份；AI 不經手值）；收掉五把 key secret、部署連線 4 個；請 mylinuxpool-pm 拿掉部署公鑰登記（隧道公鑰保留）。
5. **之後換 key**：照 §3.3（改明文→重加密→刪明文→push→等一次拉取；趕時間手動觸發 key-sync→驗證）。

## 5. 受影響的程式與測試（對照 A：`v5-inventory.md`）

- **服務 until-only**：修改 `lgwd/registry.py`、`store.py`、`errors.py`、`http.py`、`policy.py`、`main.py`（mtime 重讀、拿掉 SIGHUP）；`keys.py`、`clock.py`、`providers.py` 保留。測試改 `test_registry.py`、`test_http.py`、`test_policy.py`、`test_main.py`；`test_keys.py` 保留。
- **客戶端**：修改 `lgw/client.py`、`signals.py`、`__init__.py`、`pyproject.toml`；拿掉 `lgw/connect.py`、`tests/test_connect.py`；測試改 `test_client.py`、`test_signals.py`、`test_package.py`。
- **金鑰管線**：拿掉 `.github/workflows/deploy-keys.yml`、`deploy-service.yml`、`deploy/remote-deploy.sh`、`tests/test_deploy_script.py`、`tests/test_workflows.py`；新增 key-sync（`ops-scripts/`）＋timer＋`test_keysync.py`（實作階段新檔）。
- **搬移與安裝**：`llmgateway-tunnel`＋unit 搬 `ops-scripts/`；改 `install.sh`、`bootstrap.sh`（拿掉部署公鑰步驟、改放解密密碼；`gh` 登入由本人手動做）、`llmgateway.service`（拿掉 `ExecReload`）；測試改 `test_install.py`、`test_bootstrap.py`、`test_tunnel.py`。
- **文件**：`docs/deploy.md` 全文重寫（大綱已在本 repo `deploy.md` 頂端）；`policy/default.json` 改政策欄位。

## 6. 估工（一個實作隊員，單位：工作段）

| 塊 | 內容 | 段 |
|---|---|---|
| 服務 until-only | registry／store／errors／http／policy／main＋測試 | 2 |
| 客戶端 | client／signals／拿掉 connect／packaging＋測試 | 1 |
| key-sync 管線 | `gh api` 拉取＋timer＋mtime 重讀＋測試（含 `gh` 失效演練） | 2 |
| 移除部署入口 | remote-deploy、兩 workflow、bootstrap deploy-key、測試刪除 | 1 |
| ops-scripts 搬移＋install／bootstrap | 隧道搬移引用更新＋install／bootstrap 調整＋測試 | 1 |
| 文件 | deploy.md 全文重寫 | 1 |
| 驗收 | Mac／worker 換把、手動升級、拉取失敗演練、秘密掃描、退回演練 | 1.5 |
| **合計** | | **約 9.5（抓 10）** |

建議切兩單：單一「until-only＋拿掉 connect」（約 3 段）先行，單二「key-sync、移除部署入口、ops-scripts 搬移、install／bootstrap、文件」（約 5 段＋驗收 1.5）；兩單都只碰文件與程式，不 merge（照本次規矩由本人合）。

## 7. 原疑點（本人 2026-10-12 第二輪已全部裁定，無未決）

1. 升級選**乙**：拿掉部署入口整組，改手動 `install.sh --tag`。
2. worker 本機 port 預設 **4710**，可改。
3. key-sync 放 **`ops-scripts/`**。
4. 拉取頻率約 **10 分鐘**＋可手動觸發。
5. 不用 deploy key：`gh` 拉，登入是使用前提（`llmgw` 身分，選 B：`gh auth login` 用瀏覽器登入、不放 token 檔，權限風險由本人接受）。
6. 名稱：`FATESAIKOU/LLMGateway-keys`、`keys.env.enc`、`GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`。
7. MyAiEntry#147 **關掉**，手機自行轉發。
