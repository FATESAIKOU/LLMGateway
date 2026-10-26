對應 #60（層級 c）。順序：1 子行程護欄 → 2 政策檔設定 → 3 用量核心 → 4 Ollama → 5 Claude → 6 接進 list／resolve → 7 stop → 8 文件 → 9 實作 PR 驗收；第 10 節是部署，**不在實作 PR 內**。

**測試約定**：每一步只跑該步列的檔，指令形如 `python -m pytest tests/test_xxx.py -q`（Mac 負載限制，**不跑全套**；整套由 PM 跑）。取用量一律用假的 HTTP 層、假的 Claude 可執行檔（腳本）、假時鐘（沿用 `lgwd/clock.py` 與 `tests/` 既有的做法）；測試不碰真 key、不連外、不呼叫真的 Claude Code。每個測試只收掉自己記下 pid 的行程，**不依名稱殺行程**。每一步的改壞驗證＝在副本拿掉關鍵修正，確認相關測試轉紅。

## 1. 子行程護欄抽出（design 決定 4「子行程護欄」）

- [x] 1.1 新增 `lgwd/subproc.py`：從 `lgwd/keysync.py` 抽出子行程起停（`start_new_session`）、`killpg` 護欄（不打到自己的群）、stderr 尾端讀取（`_spawn`、`_signal_child`、`_read_tail`）；環境由呼叫方給。`lgwd/keysync.py` 改用它，**但保留模組層的 `_spawn(script, env)`，`run_once` 經由它呼叫（裡面再轉給 `lgwd/subproc.py`）**，因為 `tests/test_keysync_timer.py` 以 `keysync_module._spawn = slow_spawn` 換掉它來驗「spawn 還沒回來時 stop」，換掉簽名或改成直接呼叫 subproc 會讓該測試悄悄失效（仍綠但不再驗那個窗口）；**對外行為與狀態檔不變**，計時留在 keysync。驗證：`python -m pytest tests/test_keysync.py tests/test_keysync_timer.py tests/test_keysync_api.py -q` 維持原樣通過（不改這些測試）；新增 `tests/test_subproc.py`（起停、killpg 只打自己記下 pid 的群、不打到自己的群、stderr 只留尾端）：`python -m pytest tests/test_subproc.py -q`。

## 2. 政策檔 `usage` 設定段（spec `usage-reporting`「政策檔的 usage 設定」）

- [x] 2.1 `lgwd/policy.py`：解析頂層選填 `usage`（鍵＝政策裡的 subscription id，值 `{enabled, ttl_seconds}`），不合法丟 `PolicyError`；`Policy` 帶出各 subscription 的設定（未設定時的預設在 3.x 套用）。驗證：`python -m pytest tests/test_policy.py -q`（新增：合法、未知 id、`usage` 非 object、值非 object、內層未知鍵（如 `enable`）、`enabled` 非布林、`ttl_seconds` 為 0／負數／非整數，一律 `PolicyError`；沒有 `usage` 段維持原行為）。
- [x] 2.2 `policy/default.json`：出廠值改為**開啟** Claude（`usage.claude-personal.enabled: true`，無 `ttl_seconds`＝預設 30 分鐘）；Ollama 用預設。驗證：`python -m pytest tests/test_policy.py -q`（出廠可載入）與 `tests/test_registry.py`（`test_Claude預設disabled` 改為用 `UsageService(settings={"claude-personal": UsageSetting(enabled=False)})` 守預設關閉、不依政策段測；沒有 `usage` 段另由 `test_沒有usage段_真的無段_Ollama120_Claude1800` 守；另加 `test_出廠政策開啟_Claude不是disabled` 與行為測試 `test_出廠政策開啟_已部署key_list_not_fetched_yet_觸發一次`）。

## 3. 用量核心（spec `usage-reporting` 的形狀、stale、available_again_at、觸發、single-flight、退避、TTL、隔離）

- [x] 3.1 新增 `lgwd/usage.py` 的純函式：`usage` 物件組裝（七個鍵恆在、`status`／`reason` 判定、窗口 `resets_at` 已過回 null、全過期＝`stale_expired`、`stale`、6 小時轉 `unknown`／`stale_expired`）與 `available_again_at` 合併（取最晚、用原始數值判斷 ≤ 0、`unknown` 不推測、key 沒部署為 null）。驗證：新增 `tests/test_usage.py`：`python -m pytest tests/test_usage.py -q`（涵蓋 `ok`／`unknown`／`not_applicable`、各 `reason`、`stale`、6 小時邊界、小數 0.2、過去的冷卻）。
- [x] 3.2 `lgwd/usage.py` 的快取與觸發：記憶體 dict（鍵＝subscription id）、單一鎖保護「觸發判斷＋in-flight 旗標＋已停止旗標」、在背景執行緒取、例外轉 `unknown`、成功重置退避、失敗退避＝max(退避, `Retry-After`)（加倍、上限 max(30 分鐘, TTL)）、Claude TTL 下限夾到 30 分鐘、Ollama TTL 下限夾到 60 秒並各記 log。取用量的實作以參數注入（`fetcher` 介面：回成功的窗口或固定碼的失敗，含選填的 `retry_after`）。驗證：`python -m pytest tests/test_usage.py -q`（新增：過期後連續多次查詢只觸發一次含多執行緒、退避期間內不觸發、退避加倍與上限、上限隨 TTL、429 的 `Retry-After` 取較大者、成功重置、沒人查不取、Claude TTL 夾到 30 分鐘與調高、Ollama TTL 夾到 60 秒與調高、取用量卡住時查詢不被拖住、取用量拋例外轉 `unknown`）。
- [x] 3.3 `lgwd/providers.py`：`CredentialProvider` 加選填的取用量實作掛點（預設沒有＝`not_applicable`）；`AnthropicProvider` 與 `OllamaCloudProvider` 之後各掛一個，`OpenRouterProvider`／`OpenCodeZenProvider` 不掛。**`validate_endpoints()` 與 `PROVIDERS` 的既有行為不變。** 驗證：`python -m pytest tests/test_usage.py tests/test_registry.py -q -k "provider or not_applicable or endpoint"`。

## 4. Ollama Cloud 取用量（spec「Ollama Cloud 取用量」）

- [x] 4.1 新增 `lgwd/usage_ollama.py`：寫死的 `https://ollama.com/api/balance`、`Authorization: Bearer`、逾時 5 秒、不跟隨轉址、不重試；欄位白名單（只取 `included.session`／`weekly` 的 `remaining_percent`、`resets_at`）；沒有這兩個物件 → `no_windows`；429 讀 `Retry-After`（秒數）；其餘錯誤對應 `upstream_error`／`timeout`／`parse_error`；key 不進例外訊息與 log；每次取之前 `reload_keys_if_changed()`。HTTP 層可注入（測試用假的）。驗證：新增 `tests/test_usage_ollama.py`：`python -m pytest tests/test_usage_ollama.py -q`（請求形狀、轉址不跟隨、逾時、429＋`Retry-After`、新方案 `no_windows`、欄位白名單、`parse_error`、換 key 後用新值、body 含 `canary-secret` 時不外洩）。

## 5. Claude 取用量（spec「Claude 取用量」；實作用假的 Claude 可執行檔，真機探測在第 10 節）

- [x] 5.1 新增 `lgwd/usage_claude.py`：經 `lgwd/subproc.py` 起子行程，環境白名單＋僅 `CLAUDE_CODE_OAUTH_TOKEN`；`--settings` 指定把 `rate_limits` 寫進暫存檔的 statusLine 命令；解析 `five_hour`／`seven_day` → `session`／`weekly`（`remaining_percent = 100 - used`）；逾時 60 秒、只終止自己記下 pid 的行程群組；stdout／stderr 不進 log；找不到可執行檔回 `upstream_error`；暫存目錄為服務帳號目錄下的固定前綴，啟動時掃殘留。管道 2（`/usage`）的 stub 已移除（2026-10-25 階段 B 探測 (b) 實測不可用，只回 session cost、沒有窗口）。驗證：新增 `tests/test_usage_claude.py`（用 `tests/` 下的假 claude 腳本）：`python -m pytest tests/test_usage_claude.py -q`（換算、欄位缺席 `no_windows`、`parse_error`、token 只在環境變數而不在 argv／檔案、環境不繼承（測試行程設假 `GH_TOKEN`）、逾時只收自己的群組、找不到可執行檔、子行程輸出不進 log、啟動掃殘留、不補 0）。 **子行程 cwd 用 lgwd `Config.usage_claude_cwd`（env `LGW_USAGE_CLAUDE_CWD`／CLI `--usage-claude-cwd`，不放政策檔；部署時 trust 的就是這個路徑，不每次新建；沒設定→`unknown`／`upstream_error`）；已鎖定 pty（#68）；政策預設仍 `disabled`，開啟見階段 C；測試加「兩次取用量的 cwd 是同一個固定路徑」「目錄不存在或沒設定→`upstream_error`」。**

## 6. 接進 list 與 resolve（spec `subscription-registry` 的 MODIFIED、`key-vault` 的 MODIFIED）

- [x] 6.1 `lgwd/registry.py`：`list_subscriptions` 逐筆合入 `usage` 與 `available_again_at`（**`_item` 不動**，get 不受影響）；組裝與觸發包 try，失敗該筆 `unknown`／`internal`＋`available_again_at` null；`RegistryService` 持有用量快取（由 `lgwd/main.py` 組裝時注入）。驗證：`python -m pytest tests/test_registry.py -q`（新增：每筆有 `usage`／`available_again_at`、`not_applicable`、Claude 預設 `disabled`、key 沒部署 `no_key` 且不觸發、`available_again_at` 的情境、`available` 不因 0% 變 false、get 不帶新欄位、`schema` 仍為 1、取用量拋例外／卡住時 list 的結果與延遲不變、組裝出錯該筆 `internal`）。 另加：key 沒部署但快取有舊值→`unknown`／`no_key` 不回舊值且不觸發；`enabled: false` 且 key 沒部署→`disabled`（優先於 `no_key`）。
- [x] 6.2 `lgwd/registry.py`：resolve 成功路徑在 `meta` 加 `usage`（不加 `available_again_at`）並觸發；409 `cooling`／`no_candidate`／`not_deployed`、404、400 `bad_request`／`no_model`、403 `agent_not_allowed`／`model_not_allowed` 不組裝也不觸發；選把規則、`env` 不變。驗證：`python -m pytest tests/test_registry.py tests/test_http.py -q`（新增：`meta` 鍵集含 `usage`、不含 `cooldown`／`available`／`available_again_at`；上述每種非 200（含 `not_deployed`、`model_not_allowed`、`no_model`）取用量實作都未被呼叫；用量為 0% 仍選它；取用量壞了 resolve 的 `env` 不變、仍 200）。
- [x] 6.3 `lgwd/http.py`：確認**不加路由**，序列化新欄位後 200 的標頭（`Cache-Control: no-store`、`Pragma: no-cache`）不變。驗證：`python -m pytest tests/test_http.py -q`。
- [x] 6.4 值紀律（canary）：在既有的掃描測試擴充涵蓋 list／resolve 新欄位、log、稽核、SQLite 檔，以及取用量成功與各種失敗後的狀態。驗證：`python -m pytest "tests/test_registry.py::ResolveTest::test_值不外洩" "tests/test_http.py::ResolveHttpTest::test_resolve_log不含憑證值" -q`（既有的兩支，擴充涵蓋新欄位與取用量後的 log）；另加 `tests/test_usage_ollama.py`、`tests/test_usage_claude.py` 內新增的 canary 測試（整檔跑）。
- [x] 6.5 用戶端：`lgw/` 的 client 對新增欄位照單全收或忽略（不因不認得的欄位失敗）。驗證：`python -m pytest tests/test_client.py -q`。

## 7. 啟停（spec「stop 之後不觸發，並收掉子行程」）

- [x] 7.1 `lgwd/main.py`：`Config` 加 `usage_claude_cwd`（env `LGW_USAGE_CLAUDE_CWD`、CLI `--usage-claude-cwd`，沿用既有的 `load_config` 慣例）並傳給 Claude 取用量；`Gateway.__init__` 組裝用量快取與取用量實作（Claude 依政策啟用）；`Gateway.stop` 先設「已停止」旗標、一律終止在跑的取用量子行程（SIGTERM→join→SIGKILL，只終止自己記下 pid 的群組），再 `keysync.stop()`／`api_server.shutdown()`；spawn 與登記 pid 在同一把鎖內、spawn 前再檢查旗標。驗證：`python -m pytest tests/test_main.py tests/test_signals.py -q`（新增：stop 後再查不觸發、stop 收掉假的長時間子行程且在有限時間內返回、stop 與 spawn 交錯不留殘留）。收尾自查：`ps` 只看自己記下的 pid，確認沒有殘留。

## 8. 文件

- [x] 8.1 `docs/deploy.md`：政策檔 `usage` 段、Claude 取用量預設關閉與開啟前提（見第 10 節）、`usage` 欄位說明與「多半拿到的是上一次查詢觸發的快照」的語意（design 決定 4）。驗證：`rtk proxy git diff --stat -- docs/deploy.md` 只動該檔；人工讀過。

## 9. 實作 PR 的驗收（不跑全套）

- [x] 9.1 逐步列出的測試檔全部通過，貼原樣輸出與 exit code：`python -m pytest tests/test_subproc.py tests/test_keysync.py tests/test_keysync_timer.py tests/test_keysync_api.py tests/test_policy.py tests/test_usage.py tests/test_usage_ollama.py tests/test_usage_claude.py tests/test_registry.py tests/test_http.py tests/test_main.py tests/test_signals.py tests/test_client.py tests/test_keys.py -q`（單一測試超過 5 分鐘就停下回報）。
- [x] 9.2 改壞驗證（在副本做，驗完刪除）：(a) 拿掉 single-flight 旗標；(b) 把 `list_subscriptions` 改成同步等取用量；(c) 拿掉 Claude TTL 下限夾制；(d) 拿掉 `Gateway.stop` 的已停止旗標；(e) resolve 非 200 也觸發；(f) 讓 `stale` 6 小時不轉 `stale_expired`；(g) `available_again_at` 改取最早者；(h) `_item` 加入 `usage`（get 會帶）；(i) 拿掉 keysync 的 spawn／stop 同鎖——`tests/test_keysync_timer.py` 那支「spawn 還沒回來時 stop」仍須轉紅——各自應有測試轉紅；沒轉紅的列在報告裡。 **結果**：審查報告的「9.2 改壞驗證」表列 9 項，(a)(b)(d)(e)(f)(g)(h)(i) 8 項轉紅；(c) 沒轉紅——夾制邏輯本身有測，但真的 fetcher 上的 TTL 常數沒有測試，之後補了 `tests/test_registry.py` 的 `RealFetcherTtlTest`（class 在 1396 行；`test_Claude政策300_夾到1800並記log` 1449 行、`test_Ollama設1_夾到60並記log` 1459 行、`test_沒有usage段_真的無段_Ollama120_Claude1800` 1412 行），釘住兩把真 fetcher 的預設值與下限。
- [x] 9.3 自查：`git status` 只有本 change 的檔案；`validate`：`openspec validate usage-in-responses --strict`。

## 10. 部署與探測（真機，需本人授權，計畫交 PMO；**實作 PR 不做這一節**）

- [x] 10.1 真機，需本人授權，計畫交 PMO：在 Gateway 主機以服務帳號裝**原版** Claude Code（官方安裝方式、不修改、不重新包裝）；`install.sh --check` 補一項「Claude Code 可執行」（若納入，另拆工單）。 **結果（2026-10-25 階段 C1，本人授權）**：在 Gateway 主機以服務帳號照官方安裝方式裝原版 Claude Code，完成，結果 OK。
- [x] 10.2 真機，需本人授權，計畫交 PMO：trust 固定工作目錄（就是 lgwd 設定 `LGW_USAGE_CLAUDE_CWD`／`--usage-claude-cwd` 指定的路徑；在 `~/.claude.json` 把 `projects["<path>"].hasTrustDialogAccepted` 設成 `true`；出處見 design 決定 6）。 **結果（2026-10-25 階段 C1，本人授權）**：固定工作目錄已 trust，服務設定 `LGW_USAGE_CLAUDE_CWD` 指到同一個路徑，結果 OK。
- [x] 10.3 真機，需本人授權，計畫交 PMO：**探測**（design 決定 3），結果回報後才鎖定 5.1 的實作：(a) trust 關卡（`-p` 與 pty 下 statusline 會不會跑）；(b) `/usage` 的 `-p` 輸出；(c) `~/.claude` 的 session 紀錄去向與累積量；(d) 子行程連了哪些主機、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 是否影響 `rate_limits`。探測失敗（取不到值）時 Claude 一律維持 `unknown`。 **結果（2026-10-25 階段 B）**：`-p` 不跑 statusline → 改 pty（#68）；`/usage` 只回 session cost、沒有窗口 → 管道 2 不採用；trust 關卡存在；(c) `-p` 每次約留一個 150KB 量級的 session 檔，`--no-session-persistence` 下不留 session 檔（但該模式 statusline 也不跑），pty 每次約 +1 個 session 檔；(d) 取樣到的連線：預設除 API 主機外還有一個第三方 log 服務，設 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 後只剩 API 主機（取樣有盲區），對 `rate_limits` 的影響未比較（`-p` 下 statusline 不跑）；服務沙箱（e）待階段 C 前置探測（pty）再定。
- [ ] 10.4 真機，需本人授權，計畫交 PMO：政策檔開啟 Claude 取用量（`usage.claude-personal.enabled: true`），觀察一輪（被查到→背景取→下一次查到新值），確認 `usage` 的值與 Claude Code 自己顯示的相符、log 與稽核沒有秘密。 改政策檔後**先經 lgwctl 的 policy 預檢（或升級流程）通過才重啟**（`usage` 段不合法會讓 lgwd 起不來）。 **結果（2026-10-25 階段 C2／C3，本人授權）**：C2（PR #71）開啟出廠政策、C3 部署 lgw-v0.4.2（經升級流程預檢後重啟），結果 OK；秘密檢查 scan 全部 0 次命中。觀察一輪見 `.work/c4-step5.log`：01:33:07Z `stale: true`（`fetched_at` 01:03:06Z）→ 01:34:47Z 新值、`stale: false`、`fetched_at` 01:33:15Z；另一筆 01:03:06Z 的重取距上一次約 30 分鐘。**未驗：本人比對**（「與 Claude Code 自己顯示的相符」這一點要本人比對）。
- [x] 10.5 真機，需本人授權，計畫交 PMO：Ollama 的現行方案是舊方案（有 `session`／`weekly`）還是新方案（`no_windows`），部署後第一次 list 查看並回報；退版＝`install.sh --tag` 回前一版（欄位是新增的，舊消費端不受影響）。 **結果**：部署 v0.4.0 時第一次 list 記錄 `OLLAMA-PLAN=old`（有 `session`／`weekly` 窗口＝舊方案）。
