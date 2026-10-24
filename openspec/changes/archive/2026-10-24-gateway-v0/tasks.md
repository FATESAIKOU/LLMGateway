v4 定案（2026-10-11 深夜）。v5 增補（2026-10-12）見最下面一節；被取代的舊項旁標了「（v5：…）」。

## 1. 服務改成三個介面（本 repo；假 key、假時鐘）

- [x] 1.1（v5：改 until-only，見 v5 增補 1.1′） `GET /v1/subscriptions`（list，不含 key，依 priority 排序）、`GET /v1/subscriptions/{id}?agent=`（get，含 key；agent 不符 403、未部署 409）、`POST /v1/subscriptions/{id}/cooldown`（5h／1w、resets_at＋2 分鐘、預設長度、不合理時刻 400、取較晚、自動解除） **作廢：被 1.1′ 取代**（until-only）。
- [x] 1.2（v5：政策檔拿掉 buffer、max-ahead 更名，見 v5 增補） 共通 JSON schema（design 決定 7）；內部 `CredentialProvider`（四種 provider）與政策檔（priority、models、agents） **作廢：被 1.2′ 取代**（政策檔拿掉 buffer、max-ahead 更名）。
- [x] 1.3 不做應用層認證（本人選 (a)）；服務只綁 127.0.0.1 **證據**：`lgwd/main.py` 的 `LOOPBACK = "127.0.0.1"`，`--api-host` 只收 127.0.0.1；`lgwd/` 沒有任何認證碼（PR #16，lgw-v0.1.0 起）。
- [x] 1.4（v5：`cooldowns` 拿掉 window 欄，見 v5 增補） SQLite 只剩 `cooldowns` 與 `audit`；稽核紀錄不含 key **作廢：被 1.4′ 取代**（`cooldowns` 拿掉 window 欄）。
- [x] 1.5 移除 impl branch 上的租約核心、管理 API、whoami、狀態板、用量查詢與相關測試 **證據**：`lgwd/` 現只有 clock／errors／http／keys／keysync／main／policy／providers／registry／store，沒有租約核心、管理 API、whoami、狀態板、用量查詢（PR #16）；`openspec/specs/agent-lease`、`usage-record` 的舊語意由本 change 歸檔時處理。
- [x] 1.6 測試：spec 每個服務端 Scenario；canary key 不出現在 list、資料庫、log **作廢：被 1.6′ 取代**（Scenario 改 until-only 重寫）。

## 2. `lgw` 客戶端

- [x] 2.1 list／get／登錄冷卻的客戶端（標準函式庫；連不上回「無法確認」，不丟例外）
- [x] 2.2 訊號規則（只有帶額度標記的 402／429 才算用完），給 sidecar 的 AgentDriver 當函式庫用
- [x] 2.3 以 git tag `lgw-vX.Y.Z` 發版 **證據**：tag `lgw-v0.1.0`～`lgw-v0.1.4`、`lgw-v0.2.0`、`lgw-v0.3.0`、`lgw-v0.3.1` 都已存在；lgwd 以 `install.sh --tag` 部署（LLMGateway#8 留言的升級紀錄）。

## 3. 連線與部署（會合點＝sshproxy）

- [x] 3.1 本人選定放服務的那台；LLMGateway 是獨立服務
- [x] 3.1a `deploy/`：安裝程式與 `remote-deploy.sh`（`keys`、`upgrade <tag>`）；已用假資料測試（v5 乙：`remote-deploy.sh` 整檔拿掉，見 3.1d′）
- [x] 3.1b 隧道守護 `llmgateway-tunnel.service`（v5：搬到 `ops-scripts/`，行為不變，見 v5 增補）：自己的金鑰與連線 `ssh -N -R 127.0.0.1:4710:… -R 127.0.0.1:4711:127.0.0.1:22 sshproxy@<跳板機>`，`ExitOnForwardFailure`／`ServerAlive*`；端對端探針（每 60 秒，連續 3 次失敗就重建）；設定檔 `tunnel.env`；`--check` 加上隧道 **作廢：被 3.1b′ 取代**（搬到 `ops-scripts/`，行為不變）。
- [x] 3.1c（v5：拿掉，見 v5 增補） `lgw connect`（函式庫＋指令）：`ssh -F … -N -L … [-J …] <帳號>@<會合點>`，回 base URL；連不上回「無法確認」；設定檔是 JSON（`connect.json`，schema 1：`rendezvous`、`jumps`、`identity`、`known_hosts` 或 `host_keys`、`remote_api`；`load_config` 每次重讀），`rendezvous` 要明確的 `user@host:port`、不讀 `~/.ssh/config`；worker 的設定檔由 MyLinuxPool 的 unit 依 gateway.json 產生，Mac 手寫
- [x] 3.1d workflow 改走會合點（`ssh -J sshproxy@<跳板機> -p 4711 llmgw@127.0.0.1`）（v5 乙：deploy-service 拿掉，改手動升級，見 3.1d′）
- [x] 3.1e lgwd 只綁 127.0.0.1（拿掉 docker bridge 綁定） **證據**：`lgwd/main.py` 只綁 `LOOPBACK`（127.0.0.1），`deploy/` 裡沒有 docker bridge 綁定；`tests/test_main.py`。
- [x] 3.2 MyLinuxPool#77（mylinuxpool-pm，進行中）：`TUNNEL_EXTRA_PUBKEYS`（裸公鑰，refresh／rotate 都會收）與 `NODE_GATEWAY.ports.llmgateway: [4710, 4711]`；本人或 PMO 設 var 並 dispatch refresh-authorized-keys。worker 的 id_tunnel 已確認讀得到（#67～#70 已作廢）（v5 乙：部署公鑰不登記了，只剩隧道公鑰） **移交／證據**：MyLinuxPool#77 已於 2026-10-11 關閉；隧道公鑰登記 MyLinuxPool#96 於 2026-10-12 關閉，LLMGateway#8 留言「隧道公鑰已在 Gateway 生效」；worker 通道 MyLinuxPool#91（2026-10-12 關閉）。部署公鑰不登記（v5 乙）。
- [x] 3.3 一次性開機設定（root，本人做或本人授權）：服務那台建 `llmgw`（不加 docker 群組）、linger、產生隧道金鑰（公鑰交給 MyLinuxPool 登記）、第一次安裝（v5 乙：放部署公鑰拿掉；`gh` 登入由本人手動做，bootstrap 不做） **證據**：LLMGateway#8 留言（2026-10-12）「第一次 bootstrap 完成：llmgw 使用者、服務、key-sync timer、隧道金鑰、解密密碼」；第二次開機設定（lgw-v0.1.1）EXIT 0；v5 乙的調整見 3.3′。
- [x] 3.4（v5：整組換掉，見 v5 增補 3.4′） 本人設定（AI 不經手值）： **作廢：被 3.4′ 取代**（整組換掉）。
  - environment「llmgateway-deploy」（required reviewer＝本人）；
  - key 5 個：`POOL_LLMGATEWAY__CLAUDE_PERSONAL__LLM`、`POOL_LLMGATEWAY__CLAUDE_COMPANY__LLM`、`POOL_LLMGATEWAY__OPENROUTER_API__LLM`、`POOL_LLMGATEWAY__OPENCODE_ZEN__LLM`、`POOL_LLMGATEWAY__OLLAMA_CLOUD__LLM`；
  - 部署 4 個：`GH_LLMGATEWAY__POOL_SSH__DEPLOY`（部署私鑰）、`…_KNOWN_HOSTS`、`…_JUMP`（`sshproxy@<跳板機>:<埠>`）、`…_TARGET`（`llmgw@127.0.0.1:4711`）；
  - 隧道金鑰**不是** secret：在服務那台產生，私鑰不離開那台（照 MyLinuxPool 的原則），只登記公鑰。

## 4. 驗收

- [x] 4.1 Mac：sidecar 以 list／get 開 Claude Code，回得了一則最短的訊息 **作廢：被 4.1′ 取代**（v5 起不再經 `lgw connect`；Mac 驗收走自備通道）。
- [x] 4.2 換把：用假的額度用完訊號 → sidecar 登錄冷卻（v5：`{until}`） → list 顯示 `claude-personal` 不能用 → sidecar 挑到 `claude-company` 重開並接回；冷卻到期後 list 恢復 **證據**：LLMGateway#8 留言（2026-10-16，worker 版）：假額度訊號 → 登錄冷卻 → list 顯示不能用 → 挑下一把重開並接回 → 冷卻到期 list 恢復，四點通過（當時換把方向是 company → ollama-cloud）。**作廢的部分**：「換到 claude-company」前提不存在（claude-company 已於 #50／lgw-v0.3.1 移除）；換把的實機驗收現行版在 #33 tasks 3.3／3.4（MyLinuxPool#244）。
- [x] 4.3 opencode 拿不到 Claude 訂閱（403）；opencode 對 Zen、OpenRouter（免費模型）、Ollama 各跑通一次 **證據**：LLMGateway#8 留言：Claude 兩把 403（2026-10-12，PMO）；opencode 經 sidecar 跑 Zen、Ollama 通過（2026-10-16）；Zen 預設模型更換後再驗通過（2026-10-17）。**作廢的部分**：OpenRouter 免費模型那一支，本人 2026-10-21 同意結案（#8 留言）：已由 lgw-v0.2.0／v0.3.0（#35、#41）取代，不另補驗。
- [x] 4.4 要用 key 的驗證（PM 或本人）：Ollama Cloud 讓 Claude Code 跑一則最短的訊息；OpenRouter（只經過 opencode）與 Zen 由 opencode 讀 key **證據**：LLMGateway#8 留言：Ollama Cloud × Claude Code 通過（本人，2026-10-12、2026-10-17）；opencode × OpenRouter 回 OK（2026-10-12）。**作廢的部分**：Claude Code × OpenRouter，本人 2026-10-21 同意結案（#8 留言）：已由 lgw-v0.2.0／v0.3.0（#35、#41）取代，不另補驗。
- [x] 4.5 worker：在一台 worker 用自備通道（v5：不再經 `lgw connect`）連線並驗收換把 **證據**：LLMGateway#8 留言：worker 通道 MyLinuxPool#91 實機通過（2026-10-12，127.0.0.1:4710 回 200）；sidecar 在 worker 上換把通過（2026-10-16 worker 版 4.2）。
- [x] 4.5a 隧道：殺掉隧道連線後自動恢復；佔住遠端埠時守護程式重試而不假裝健康 **證據**：LLMGateway#8 留言：kill 隧道 ssh 後約 11 秒自動恢復（2026-10-12）；佔住遠端埠時守護程式每約 10 秒重試、對外回 000 不假裝健康、放掉後 7 秒內恢復（2026-10-13，本人）。
- [x] 4.6 秘密掃描：repo、資料庫、log、list 的回覆沒有任何 key 的值 **證據**：LLMGateway#8 留言（2026-10-12）：服務那台的資料庫、journal、list 回覆比對真值命中 0；repo 的 tree 與全部歷史只命中測試用的假私鑰檔頭（`tests/test_bootstrap.py`）。v5 版見 4.6′。
- [x] 4.7 退回演練 **證據**：LLMGateway#8 留言（2026-10-12）：`install.sh --tag lgw-v0.1.0` → `--check` rc 0、list 200；再 `--tag lgw-v0.1.1` → rc 0、list 200。

## 5. 銜接

- [x] 5.1 把共通 JSON schema 與三個介面的最後版本交給 aistorage-pm（已在 v3 對齊草案；v5：改為 until-only 版） **證據／移交**：LLMGateway#8 留言（2026-10-12）：v5 最終介面已交給 sidecar 的維護方（sidecar 已搬到 MyLinuxPool#92，2026-10-12 關閉）；後續對齊見 #33。
- [x] 5.2 MyAiEntry#147 已關掉（本人裁定）：不做 App 內轉發
- [x] 5.3 另開單：把 MyLinuxPool#50 的 key 來源換成 LLMGateway，收掉 MyLinuxPool 裡的 key **移交**：MyLinuxPool#99「worker 的 LLM key 改由 LLMGateway 提供」已開，2026-10-23 關閉。
- [x] 5.4 通知 MyAiEntry PM：#147 已關掉（不做 App 內轉發）；手機自行用 ssh -J 等方式轉發＋list／get，LLMGateway 不提供程式 **證據／移交**：MyAiEntry#147 於 2026-10-12 關閉時的留言（本人裁定不做 App 內轉發，手機自己用 mlp 或 ssh -J 轉發）；2026-10-24 LLMGateway PM 另外直接通知了 MyAiEntry PM，內容含現行介面 list／resolve／cooldown，記錄在 LLMGateway#52 的留言。
- [x] 5.5（v5 新增）通知 mylinuxpool-pm：worker 通道改由 MyLinuxPool 的 LLMGateway capability 附小 script 建立，建到本機 loopback 4710（預設，可改）；LLMGateway 只提這個需求 **移交／證據**：MyLinuxPool#91「LLMGateway capability：worker 本機 port（預設 4710）的通道 script（v5）」於 2026-10-12 關閉。

## v5 增補（2026-10-12；取代或新增上面標了 v5 的項）

- [x] 1.1′ 登錄冷卻 until-only：`POST …/cooldown {until}`（必填 RFC 3339；缺→400 `bad_request`＋`missing: ["until"]`；格式錯→400 `invalid_until`；過去或 8 天以後→400 `until_out_of_range`；未知欄位忽略）；重複登錄取較晚；過期自動解除；`cooldown` 欄位改單一 `until`／null；`available`＝已部署且無未來 `until` **證據**：`lgwd/registry.py`（`invalid_until`、`until_out_of_range`、取較晚、過期自動解除）、`tests/test_registry.py`、`tests/test_http.py`；PR #14、#16。
- [x] 1.2′ 政策檔：拿掉 `cooldown_buffer_seconds`，`resets_at_max_ahead_seconds` 更名（until 語意，值不變） **證據**：`policy/default.json` 只有 `until_max_ahead_seconds`（691200），`lgwd/policy.py` 無 `cooldown_buffer`；PR #14。
- [x] 1.4′ SQLite：`cooldowns` 拿掉 `window` 欄（subscription → until），`audit` 拿掉 `window` 欄 **證據**：`lgwd/store.py` 無 `window`；PR #14。
- [x] 1.6′ 測試：spec 每個服務端 Scenario 重寫（until-only）；canary key 不出現在 list、資料庫、log **證據**：`tests/test_registry.py`、`tests/test_http.py`、`tests/test_keys.py` 以 canary key 為斷言；PR #14、#16、#42。
- [x] 2.1′ `lgw.client.cooldown(id, until)` 只送 `{until}`；拿掉 window 相關錯誤碼 **證據**：`lgw/client.py` 的 `cooldown(id, until)` 只送 `{until}`；`tests/test_client.py`。
- [x] 2.2′ `lgw.signals`：`Signal` 拿掉 `window`，`to_cooldown_body()` 改產 `{until}`（判讀不出時呼叫方用現在＋1 小時） **證據**：`lgw/signals.py` 的 `Signal.to_cooldown_body()` 產 `{until}`，判讀不出時丟 `ValueError` 由呼叫方改用現在＋1 小時；`tests/test_signals.py`。
- [x] 2.4′（新增）拿掉 `lgw/connect.py`、`tests/test_connect.py`；`lgw/__init__.py`、`pyproject.toml`（描述）、`tests/test_package.py` 跟著改 **證據**：`lgw/connect.py`、`tests/test_connect.py` 不存在；`lgw/` 只剩 `__init__`、`client`、`signals`（PR #14）。
- [x] 3.1b′ 隧道守護與其 unit 從 `deploy/` 搬到 `ops-scripts/`（內容不變）；`install.sh`、`bootstrap.sh`、`test_install.py`、`test_bootstrap.py`、`test_tunnel.py` 改引用新路徑 **證據**：`ops-scripts/llmgateway-tunnel`、`ops-scripts/llmgateway-tunnel.service`；`tests/test_tunnel.py`（PR #15）。
- [x] 3.1d′（v5 乙）拿掉 `deploy-service` workflow 與 `remote-deploy.sh` 整檔；升級改管理帳號手動 ssh 上去跑 `install.sh --tag`（手冊步驟，不寫機器名）；`tests/test_deploy_script.py`、`tests/test_workflows.py` 一併拿掉 **證據**：repo 無 `.github/`、`deploy/remote-deploy.sh`、`tests/test_deploy_script.py`、`tests/test_workflows.py`；升級用 `install.sh --tag`（見 4.2′）。
- [x] 3.3′ bootstrap 拿掉部署公鑰／forced command 步驟（`--deploy-pubkey-file`、`step_deploy_key`）；初次啟動改放解密密碼（`--decrypt-pass-file` 或互動輸入，寫 `keysync.env`）；`gh` 登入由本人手動做，bootstrap 不做 **證據**：`deploy/bootstrap.sh` 有 `--decrypt-pass-file`、無 `--deploy-pubkey-file`；`tests/test_bootstrap.py`（PR #15）。
- [x] 3.4′ 本人設定（AI 不經手值）：environment「llmgateway-deploy」留著，只放 `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT`（解密密碼備份）；收掉五把 `POOL_LLMGATEWAY__*__LLM`、部署連線 4 個（`GH_LLMGATEWAY__POOL_SSH__DEPLOY*`）、MyLinuxPool 那側部署公鑰登記（隧道公鑰保留） **證據**：2026-10-24 本人刪除了 environment `llmgateway-deploy` 的四把 `POOL_LLMGATEWAY__*__LLM`；現況是 environment 沒有 secret，解密密碼備份 `GH_LLMGATEWAY__LLMGATEWAY_KEYS__DECRYPT` 放在 repo 層級（與原文「放在 environment」不同，現況為 repo 層級）；部署連線的 `GH_LLMGATEWAY__POOL_SSH__DEPLOY*` 也已不在（`gh secret list` 只看名稱，2026-10-24）；查證（2026-10-24，MyLinuxPool PM 唯讀）：MyLinuxPool 的 TUNNEL_EXTRA_PUBKEYS 只剩名為 llmgateway 的一把 ED25519（指紋前綴 SHA256:gUPQ4y），即 deploy/bootstrap.sh 第 6 步產生的現役反向隧道公鑰，保留不收；v5 以前的部署公鑰已不在。以 `gh secret list` 只看名稱為證。
- [x] 3.5′（新增）key-sync（放 `ops-scripts/`）：小 script＋systemd timer（約每 10 分鐘＋可手動觸發）：`gh api` 拉→比對 blob `sha`→解碼→解密（`-pass env:`）→驗證格式→原子替換→`lgwd` 依 mtime 重讀；失敗（含 `gh` 沒登入／token 失效）保留舊版；`lgwd` 拿掉 SIGHUP（`llmgateway.service` 拿掉 `ExecReload`）；`--check` 看得出「gh 不能用」 **證據**：`ops-scripts/key-sync`、`key-sync.service`、`key-sync.timer`，`lgwd/keysync.py`；`tests/test_keysync*.py`；`deploy/units/llmgateway.service` 無 `ExecReload`、`lgwd` 無 SIGHUP（PR #15）。
- [x] 4.1′ Mac 驗收改走自備通道（手寫 ssh -L 或 mlp）；換把驗收用 `{until}` 登錄 **證據**：LLMGateway#8 留言（2026-10-21，本人結案）：Mac 經 ssh -L 4710 自備通道，Mac 上的 sidecar 取得 opencode-zen 與 claude-personal，開 agent、poke、close 都通。
- [x] 4.2′ 升級改手動：管理帳號 ssh 上去跑 `install.sh --tag`，前後各一次 `--check` **證據**：LLMGateway#8 留言的 lgw-v0.1.2／v0.1.3／v0.1.4 升級紀錄：管理帳號 ssh 上去跑 `install.sh --tag`，前後各一次 `--check` rc 0。
- [x] 4.6′ 秘密掃描：private repo 加密檔、解密密碼不進本 repo；repo、資料庫、log、list 回覆無 key 的值 **證據**：同 4.6（2026-10-12，lgw-v0.1.1 即 v5）：repo 無 key 值；解密密碼只在 GitHub secret 與服務那台的 `keysync.env`，不進本 repo。
