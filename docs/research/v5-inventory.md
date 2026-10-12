# v5 檔案盤點（A）：impl/gateway-v0-core 每個檔案在 v5 下的處置

基準：`scope/gateway-v5` worktree 當前狀態（v4 實作完成）。v5 以 v4 為底，只改五件事：
v5.1 冷卻只收 `until`（拿掉 `window` 5h／1w；list／get 的 `cooldown` 欄位改成單一 `until` 或 null）；
v5.2 拿掉 `lgw connect`（sidecar 直接 curl 本機 port，通道由 MyLinuxPool capability／mlp／ssh -J 提供）；
v5.3 反向隧道（`llmgateway-tunnel` 與其 unit）搬到 `ops-scripts/`；
v5.4 金鑰正本改放 private repo 加密檔，服務自己定期拉（拿掉 deploy-keys workflow、`keys` 動詞、SIGHUP 重讀）；
v5.5 sidecar 內部的 SubscriptionSource 與 sshproxy 不是 LLMGateway 的事（只改文件說法）。

處置只有四種：保留／修改／拿掉／搬移。每項一句理由。

## lgw/（客戶端函式庫）

- `lgw/__init__.py`：**修改**——子模組剩 `client`、`signals`，拿掉 `connect` 的 lazy 載入。
- `lgw/client.py`：**修改**——`cooldown(id, window, resets_at)` 改為 `cooldown(id, until)`，只送 `{until}`；錯誤碼拿掉 window 相關。
- `lgw/connect.py`：**拿掉**——v5 不再提供通用連線程式，sidecar 直接 curl 本機 port。
- `lgw/signals.py`：**修改**——`Signal` 拿掉 `window` 欄位，`to_cooldown_body()` 改產 `{until}`，`resets_at` 判讀不出時由呼叫方用現在＋1 小時。

## lgwd/（服務）

- `lgwd/__init__.py`：**保留**——套件標記，與 v5 無關。
- `lgwd/__main__.py`：**保留**——啟動入口不變。
- `lgwd/clock.py`：**保留**——時刻工具給單一 `until` 照用。
- `lgwd/errors.py`：**修改**——拿掉 `invalid_window`，`invalid_resets_at`／`resets_at_out_of_range` 改名為 until 語意。
- `lgwd/http.py`：**修改**——路由結構不變，冷卻 body 語意與錯誤表跟著 v5.1 改。
- `lgwd/keys.py`：**保留**——解密後的 `KEY=VALUE` 驗證邏輯不變，key-sync 直接複用。
- `lgwd/main.py`：**修改**——拿掉 SIGHUP 重讀（v5.4 改由 key-sync 換檔後 lgwd 自己偵測 mtime 重讀，細節見 v5-plan）。
- `lgwd/policy.py`：**修改**——拿掉 `cooldown_buffer_seconds`（until 即何時能再用，不再加 2 分鐘），`resets_at_max_ahead_seconds` 更名為 until 語意。
- `lgwd/providers.py`：**保留**——provider 欄位與冷卻、部署、金鑰來源都無關。
- `lgwd/registry.py`：**修改**——核心：單一 `until` 的登錄／查詢／`available` 判定，共通 JSON 的 `cooldown` 欄位改單一 `until` 或 null。
- `lgwd/store.py`：**修改**——`cooldowns` 表拿掉 `window` 欄（subscription → until），`audit` 表拿掉 `window` 欄。

## deploy/（服務那台的安裝與部署入口）

- `deploy/bootstrap.sh`：**修改**——拿掉部署公鑰步驟（`--deploy-pubkey-file`、`step_deploy_key`、authorized_keys forced command）；隧道執行檔與 unit 的引用路徑改到 `ops-scripts/`；初次啟動引導（GitHub 登入、解密密碼放置）是新增步驟，實作階段加。
- `deploy/install.sh`：**修改**——隧道執行檔與 unit 改從 `ops-scripts/` 安裝；其餘（tag 取碼、current 切換、unit 安裝）不變。
- `deploy/llmgateway-run`：**保留**——組 `python3 -m lgwd` 參數的 wrapper，與 v5 五件事都無關。
- `deploy/llmgateway-tunnel`：**搬移**——內容不變，搬到 `ops-scripts/`（維運工具定位，v5.3）。
- `deploy/llmgateway-tunnel.service`：**搬移**——同上，跟著隧道守護走。
- `deploy/llmgateway.service`：**修改**——只拿掉 `ExecReload … HUP` 那一行（SIGHUP 重讀沒了）；其餘不變。
- `deploy/remote-deploy.sh`：**修改**——拿掉 `keys` 動詞（金鑰不再經 stdin 送）；`upgrade` 動詞去留待本人決定（見 v5-plan 疑點：服務程式本身怎麼升級、還要不要 deploy-service）。

## .github/workflows/

- `.github/workflows/deploy-keys.yml`：**拿掉**——金鑰改由服務自己從 private repo 定期拉，不再經 workflow 送。
- `.github/workflows/deploy-service.yml`：**待本人決定（修改或拿掉二選一）**——若保留自助升級就修改（只剩 upgrade），若服務升級改走別的方式就拿掉；見 v5-plan 疑點。

## tests/

- `tests/__init__.py`：**保留**——空檔。
- `tests/test_bootstrap.py`：**修改**——拿掉 deploy-key 步驟的測試，隧道路徑改 `ops-scripts/`。
- `tests/test_client.py`：**修改**——cooldown 新 body `{until}` 與新錯誤碼。
- `tests/test_connect.py`：**拿掉**——跟著 `lgw/connect.py` 走。
- `tests/test_deploy_script.py`：**修改**——拿掉 `keys` 動詞的測試；`upgrade` 測試去留跟著 remote-deploy.sh 待定；key-sync 的測試是新增，實作階段加。
- `tests/test_http.py`：**修改**——冷卻路由的新語意與錯誤碼。
- `tests/test_install.py`：**修改**——隧道檔改從 `ops-scripts/` 裝的斷言。
- `tests/test_keys.py`：**保留**——格式驗證邏輯不變。
- `tests/test_main.py`：**修改**——拿掉 SIGHUP 重讀的測試，改測 mtime 偵測重讀（提案見 v5-plan）。
- `tests/test_package.py`：**修改**——子模組剩三個（`__init__`、`client`、`signals`），拿掉 `lgw.connect` 斷言。
- `tests/test_policy.py`：**修改**——政策欄位更名／拿掉 buffer。
- `tests/test_registry.py`：**修改**——單一 `until` 的登錄、查詢、`available`。
- `tests/test_signals.py`：**修改**——until-based 的 `Signal` 與 fallback（現在＋1 小時在 sidecar，函式庫只負責轉 `{until}`）。
- `tests/test_tunnel.py`：**修改**——測試留 `tests/`，只改被測檔路徑到 `ops-scripts/`。
- `tests/test_workflows.py`：**修改**——拿掉 deploy-keys 的斷言；deploy-service 的斷言去留跟著 workflow 待定。

## docs/

- `docs/deploy.md`：**修改（只寫改寫大綱，不全文重寫）**——§1.9 放部署公鑰、§2 部署連線 secrets、§4.1 換 key、§4.2 升級（待定）、§4.5 外洩時、§5.1–5.3 排錯都要重寫；大綱見本檔 B 件（deploy.md 頂端 v5 橫幅）。
- `docs/research/*.md`（既有研究）：**保留**——歷史調查，`quota-signals` 與 `claude-failover` 仍是 sidecar 判讀額度訊號的依據。
- `docs/research/v5-plan.md`：**新增（C 件）**——金鑰拉取流程、初次啟動引導、要本人做的事、估工、疑點。

## policy/

- `policy/default.json`：**修改**——拿掉 `cooldown_buffer_seconds`，`resets_at_max_ahead_seconds` 更名為 until 語意；priority／models／agents 不變。

## pyproject.toml

- **修改，但保留**——sidecar 仍用 `pip install "lgw @ git+…@lgw-vX.Y.Z"` 依賴 `lgw`（`client`＋`signals`），所以打包設定要留；只改 `description`（拿掉 connect 字樣）。`packages = ["lgw"]` 不變。

## 特別問題（逐一回答）

- `deploy-keys.yml`：拿掉（理由同上，不再經 workflow 送 key）。
- `deploy-service.yml`：待本人決定；服務程式本身的升級方式未定（v5-plan 疑點第一條）。
- `remote-deploy.sh` 的 `keys` 動詞：拿掉；`upgrade` 動詞：跟著上一條待定。
- `bootstrap.sh` 的部署公鑰與 forced command 步驟：拿掉（部署私鑰／公鑰整組不用了；初次引導改放 GitHub deploy key 與解密密碼）。
- `lgw/connect.py`：拿掉（v5.2；worker 通道是 MyLinuxPool capability 的事，Mac／手機用 mlp 或 ssh -J）。
- `pyproject`：還需要（`lgw` 照樣給 sidecar 當函式庫用，只改描述）。

## 實作階段的新增檔案（不在 v4 盤點內，先列供 C 件估工用）

- `ops-scripts/` 目錄（從 `deploy/` 搬入隧道守護與其 unit）。
- key-sync 小程式＋systemd timer（每約 10 分鐘拉 private repo 加密檔、解密、驗證、原子替換；失敗保留舊版）。
- private repo 的加密檔正本（檔名與格式提案見 v5-plan）。
