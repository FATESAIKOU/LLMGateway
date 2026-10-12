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
- token 二選一（請本人擇一）：
  - （甲，推薦）只讀 `FATESAIKOU/LLMGateway-keys` 單一 repo 的 fine-grained token：`gh auth login --with-token` 讀 token 登入；權限最小，就算這台的登入狀態外洩也只影響該 repo 的讀。
  - （乙）接受預設：`gh auth login` 互動登入，token 拿到本人帳號的 repo 全權限；拿著這台登入狀態的人能讀寫本人所有 repo，本人接受此風險才選。
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

## 4. 受影響的程式與測試（對照 A：`v5-inventory.md`）

- **服務 until-only**：修改 `lgwd/registry.py`、`store.py`、`errors.py`、`http.py`、`policy.py`、`main.py`（mtime 重讀、拿掉 SIGHUP）；`keys.py`、`clock.py`、`providers.py` 保留。測試改 `test_registry.py`、`test_http.py`、`test_policy.py`、`test_main.py`；`test_keys.py` 保留。
- **客戶端**：修改 `lgw/client.py`、`signals.py`、`__init__.py`、`pyproject.toml`；拿掉 `lgw/connect.py`、`tests/test_connect.py`；測試改 `test_client.py`、`test_signals.py`、`test_package.py`。
- **金鑰管線**：拿掉 `.github/workflows/deploy-keys.yml`；`remote-deploy.sh` 拿掉 `keys` 動詞（`upgrade` 待定）；新增 key-sync＋timer＋`test_keysync.py`（實作階段新檔）；`test_deploy_script.py`、`test_workflows.py` 跟著改。
- **搬移與安裝**：`llmgateway-tunnel`＋unit 搬 `ops-scripts/`；改 `install.sh`、`bootstrap.sh`（＋初次引導步驟、拿掉部署公鑰步驟——但疑點 1 選甲則保留 deploy-key 步驟只為 upgrade）、`llmgateway.service`（拿掉 `ExecReload`）；測試改 `test_install.py`、`test_bootstrap.py`、`test_tunnel.py`。
- **文件**：`docs/deploy.md` 全文重寫（大綱已在本 repo `deploy.md` 頂端）；`policy/default.json` 改政策欄位。

## 5. 估工（一個實作隊員，單位：工作段）

| 塊 | 內容 | 段 |
|---|---|---|
| 服務 until-only | registry／store／errors／http／policy／main＋測試 | 2 |
| 客戶端 | client／signals／拿掉 connect／packaging＋測試 | 1 |
| key-sync 管線 | key-sync＋timer＋mtime 重讀＋測試（含失敗保留舊版演練） | 2 |
| 部署與安裝 | remote-deploy／install／bootstrap（含初次引導）／deploy-service 去留實作＋測試 | 1.5 |
| 搬移 | ops-scripts 搬移＋引用更新＋測試 | 0.5 |
| 文件 | deploy.md 全文重寫 | 1 |
| 驗收 | Mac 換把、worker 換把、拉取失敗演練、秘密掃描、退回演練 | 1.5 |
| **合計** | | **約 9.5（抓 10）** |

建議切兩單：單一「until-only＋拿掉 connect」（約 3.5 段）先行，單二「key-sync＋部署＋文件」（約 6 段）；兩單都只碰文件與程式，不 merge（照本次規矩由本人合）。

## 6. 疑點與需要本人決定的地方

1. **服務程式本身怎麼升級（`upgrade` 動詞與 deploy-service 去留）**：甲＝精簡保留（只剩 upgrade；部署公鑰、forced command、bootstrap deploy-key 步驟、部署連線 4 secret 全留）；乙＝整組拿掉（升級改本人手動經會合點 ssh 跑 `install.sh`）。提案甲（改動最小、已有測試）。連帶：A 盤點的 bootstrap／deploy-service 條目以此決定為準（選甲則 bootstrap 保留 deploy-key 步驟）。
2. **worker 本機 port 的約定**：提案 LLMGateway 不規定（只認「本機 loopback 某 port 即 HTTP API」），由 MyLinuxPool capability 與 sidecar 約定；要不要給預設值（如 8480）請本人定。
3. **反向隧道與 ops-scripts 的邊界**：提案 key-sync 也放 `ops-scripts/`（跟隧道作伴，都是維運工具），`install.sh` 從 `deploy/` 裝服務、`ops-scripts/` 裝維運工具；或 key-sync 留 `deploy/`。請本人定。
4. **拉取頻率**：提案約 10 分鐘（換 key 最長等 10 分鐘，趕時間手動觸發）；要更短／更長請本人定。
5. **初次引導的 deploy keypair**：提案由 bootstrap 在服務那台產生（私鑰不離開，印公鑰給本人登記），同隧道金鑰模式；請本人確認。
6. **private repo 名與 secret 名**：提案 `LLMGateway-keys` 與 `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`；請本人定（定了才開得了 repo 與 secret）。
7. **手機通道**：v5.2 後手機用 mlp 或 ssh -J 自備通道（MyAiEntry#147 原定的 App 內轉發是否照走），請本人確認 MyAiEntry PM 已知。
