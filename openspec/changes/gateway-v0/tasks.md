v4 定案（2026-10-11 深夜）。v5 增補（2026-10-12）見最下面一節；被取代的舊項旁標了「（v5：…）」。

## 1. 服務改成三個介面（本 repo；假 key、假時鐘）

- [ ] 1.1（v5：改 until-only，見 v5 增補 1.1′） `GET /v1/subscriptions`（list，不含 key，依 priority 排序）、`GET /v1/subscriptions/{id}?agent=`（get，含 key；agent 不符 403、未部署 409）、`POST /v1/subscriptions/{id}/cooldown`（5h／1w、resets_at＋2 分鐘、預設長度、不合理時刻 400、取較晚、自動解除）
- [ ] 1.2（v5：政策檔拿掉 buffer、max-ahead 更名，見 v5 增補） 共通 JSON schema（design 決定 7）；內部 `CredentialProvider`（四種 provider）與政策檔（priority、models、agents）
- [ ] 1.3 不做應用層認證（本人選 (a)）；服務只綁 127.0.0.1
- [ ] 1.4（v5：`cooldowns` 拿掉 window 欄，見 v5 增補） SQLite 只剩 `cooldowns` 與 `audit`；稽核紀錄不含 key
- [ ] 1.5 移除 impl branch 上的租約核心、管理 API、whoami、狀態板、用量查詢與相關測試
- [ ] 1.6 測試：spec 每個服務端 Scenario；canary key 不出現在 list、資料庫、log

## 2. `lgw` 客戶端

- [x] 2.1 list／get／登錄冷卻的客戶端（標準函式庫；連不上回「無法確認」，不丟例外）
- [x] 2.2 訊號規則（只有帶額度標記的 402／429 才算用完），給 sidecar 的 AgentDriver 當函式庫用
- [ ] 2.3 以 git tag `lgw-vX.Y.Z` 發版

## 3. 連線與部署（會合點＝sshproxy）

- [x] 3.1 本人選定放服務的那台；LLMGateway 是獨立服務
- [x] 3.1a `deploy/`：安裝程式與 `remote-deploy.sh`（`keys`、`upgrade <tag>`）；已用假資料測試（v5 乙：`remote-deploy.sh` 整檔拿掉，見 3.1d′）
- [ ] 3.1b 隧道守護 `llmgateway-tunnel.service`（v5：搬到 `ops-scripts/`，行為不變，見 v5 增補）：自己的金鑰與連線 `ssh -N -R 127.0.0.1:4710:… -R 127.0.0.1:4711:127.0.0.1:22 sshproxy@<跳板機>`，`ExitOnForwardFailure`／`ServerAlive*`；端對端探針（每 60 秒，連續 3 次失敗就重建）；設定檔 `tunnel.env`；`--check` 加上隧道
- [x] 3.1c（v5：拿掉，見 v5 增補） `lgw connect`（函式庫＋指令）：`ssh -F … -N -L … [-J …] <帳號>@<會合點>`，回 base URL；連不上回「無法確認」；設定檔是 JSON（`connect.json`，schema 1：`rendezvous`、`jumps`、`identity`、`known_hosts` 或 `host_keys`、`remote_api`；`load_config` 每次重讀），`rendezvous` 要明確的 `user@host:port`、不讀 `~/.ssh/config`；worker 的設定檔由 MyLinuxPool 的 unit 依 gateway.json 產生，Mac 手寫
- [x] 3.1d workflow 改走會合點（`ssh -J sshproxy@<跳板機> -p 4711 llmgw@127.0.0.1`）（v5 乙：deploy-service 拿掉，改手動升級，見 3.1d′）
- [ ] 3.1e lgwd 只綁 127.0.0.1（拿掉 docker bridge 綁定）
- [ ] 3.2 MyLinuxPool#77（mylinuxpool-pm，進行中）：`TUNNEL_EXTRA_PUBKEYS`（裸公鑰，refresh／rotate 都會收）與 `NODE_GATEWAY.ports.llmgateway: [4710, 4711]`；本人或 PMO 設 var 並 dispatch refresh-authorized-keys。worker 的 id_tunnel 已確認讀得到（#67～#70 已作廢）（v5 乙：部署公鑰不登記了，只剩隧道公鑰）
- [ ] 3.3 一次性開機設定（root，本人做或本人授權）：服務那台建 `llmgw`（不加 docker 群組）、linger、產生隧道金鑰（公鑰交給 MyLinuxPool 登記）、第一次安裝（v5 乙：放部署公鑰拿掉；`gh` 登入由本人手動做，bootstrap 不做）
- [ ] 3.4（v5：整組換掉，見 v5 增補 3.4′） 本人設定（AI 不經手值）：
  - environment「llmgateway-deploy」（required reviewer＝本人）；
  - key 5 個：`POOL_LLMGATEWAY__CLAUDE_PERSONAL__LLM`、`POOL_LLMGATEWAY__CLAUDE_COMPANY__LLM`、`POOL_LLMGATEWAY__OPENROUTER_API__LLM`、`POOL_LLMGATEWAY__OPENCODE_ZEN__LLM`、`POOL_LLMGATEWAY__OLLAMA_CLOUD__LLM`；
  - 部署 4 個：`GH_LLMGATEWAY__POOL_SSH__DEPLOY`（部署私鑰）、`…_KNOWN_HOSTS`、`…_JUMP`（`sshproxy@<跳板機>:<埠>`）、`…_TARGET`（`llmgw@127.0.0.1:4711`）；
  - 隧道金鑰**不是** secret：在服務那台產生，私鑰不離開那台（照 MyLinuxPool 的原則），只登記公鑰。

## 4. 驗收

- [ ] 4.1 Mac：sidecar 以 list／get 開 Claude Code，回得了一則最短的訊息
- [ ] 4.2 換把：用假的額度用完訊號 → sidecar 登錄冷卻（v5：`{until}`） → list 顯示 `claude-personal` 不能用 → sidecar 挑到 `claude-company` 重開並接回；冷卻到期後 list 恢復
- [ ] 4.3 opencode 拿不到 Claude 訂閱（403）；opencode 對 Zen、OpenRouter（免費模型）、Ollama 各跑通一次
- [ ] 4.4 要用 key 的驗證（PM 或本人）：OpenRouter、Ollama Cloud 讓 Claude Code 跑一則最短的訊息；opencode 讀 key 的方式
- [ ] 4.5 worker：在一台 worker 用自備通道（v5：不再經 `lgw connect`）連線並驗收換把
- [ ] 4.5a 隧道：殺掉隧道連線後自動恢復；佔住遠端埠時守護程式重試而不假裝健康
- [ ] 4.6 秘密掃描：repo、資料庫、log、list 的回覆沒有任何 key 的值
- [ ] 4.7 退回演練

## 5. 銜接

- [ ] 5.1 把共通 JSON schema 與三個介面的最後版本交給 aistorage-pm（已在 v3 對齊草案；v5：改為 until-only 版）
- [x] 5.2 MyAiEntry#147 已關掉（本人裁定）：不做 App 內轉發
- [ ] 5.3 另開單：把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key
- [ ] 5.4 通知 MyAiEntry PM：#147 已關掉（不做 App 內轉發）；手機自行用 ssh -J 等方式轉發＋list／get，LLMGateway 不提供程式
- [ ] 5.5（v5 新增）通知 mylinuxpool-pm：worker 通道改由 MyLinuxPool 的 LLMGateway capability 附小 script 建立，建到本機 loopback 4710（預設，可改）；LLMGateway 只提這個需求

## v5 增補（2026-10-12；取代或新增上面標了 v5 的項）

- [ ] 1.1′ 登錄冷卻 until-only：`POST …/cooldown {until}`（必填 RFC 3339；缺→400 `bad_request`＋`missing: ["until"]`；格式錯→400 `invalid_until`；過去或 8 天以後→400 `until_out_of_range`；未知欄位忽略）；重複登錄取較晚；過期自動解除；`cooldown` 欄位改單一 `until`／null；`available`＝已部署且無未來 `until`
- [ ] 1.2′ 政策檔：拿掉 `cooldown_buffer_seconds`，`resets_at_max_ahead_seconds` 更名（until 語意，值不變）
- [ ] 1.4′ SQLite：`cooldowns` 拿掉 `window` 欄（subscription → until），`audit` 拿掉 `window` 欄
- [ ] 1.6′ 測試：spec 每個服務端 Scenario 重寫（until-only）；canary key 不出現在 list、資料庫、log
- [ ] 2.1′ `lgw.client.cooldown(id, until)` 只送 `{until}`；拿掉 window 相關錯誤碼
- [ ] 2.2′ `lgw.signals`：`Signal` 拿掉 `window`，`to_cooldown_body()` 改產 `{until}`（判讀不出時呼叫方用現在＋1 小時）
- [ ] 2.4′（新增）拿掉 `lgw/connect.py`、`tests/test_connect.py`；`lgw/__init__.py`、`pyproject.toml`（描述）、`tests/test_package.py` 跟著改
- [ ] 3.1b′ 隧道守護與其 unit 從 `deploy/` 搬到 `ops-scripts/`（內容不變）；`install.sh`、`bootstrap.sh`、`test_install.py`、`test_bootstrap.py`、`test_tunnel.py` 改引用新路徑
- [ ] 3.1d′（v5 乙）拿掉 `deploy-service` workflow 與 `remote-deploy.sh` 整檔；升級改管理帳號手動 ssh 上去跑 `install.sh --tag`（手冊步驟，不寫機器名）；`tests/test_deploy_script.py`、`tests/test_workflows.py` 一併拿掉
- [ ] 3.3′ bootstrap 拿掉部署公鑰／forced command 步驟（`--deploy-pubkey-file`、`step_deploy_key`）；初次啟動改放解密密碼（`--decrypt-pass-file` 或互動輸入，寫 `keysync.env`）；`gh` 登入由本人手動做，bootstrap 不做
- [ ] 3.4′ 本人設定（AI 不經手值）：environment「llmgateway-deploy」留著，只放 `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`（解密密碼備份）；收掉五把 `POOL_LLMGATEWAY__*__LLM`、部署連線 4 個（`GH_LLMGATEWAY__POOL_SSH__DEPLOY*`）、MyLinuxPool 那側部署公鑰登記（隧道公鑰保留）
- [ ] 3.5′（新增）key-sync（放 `ops-scripts/`）：小 script＋systemd timer（約每 10 分鐘＋可手動觸發）：`gh api` 拉→比對 blob `sha`→解碼→解密（`-pass env:`）→驗證格式→原子替換→`lgwd` 依 mtime 重讀；失敗（含 `gh` 沒登入／token 失效）保留舊版；`lgwd` 拿掉 SIGHUP（`llmgateway.service` 拿掉 `ExecReload`）；`--check` 看得出「gh 不能用」
- [ ] 4.1′ Mac 驗收改走自備通道（手寫 ssh -L 或 mlp）；換把驗收用 `{until}` 登錄
- [ ] 4.2′ 升級改手動：管理帳號 ssh 上去跑 `install.sh --tag`，前後各一次 `--check`
- [ ] 4.6′ 秘密掃描：private repo 加密檔、解密密碼不進本 repo；repo、資料庫、log、list 回覆無 key 的值
