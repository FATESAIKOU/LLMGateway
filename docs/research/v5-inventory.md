# v5 檔案盤點（A）：impl/gateway-v0-core 每個檔案在 v5 下的處置

基準：`scope/gateway-v5` worktree 當前狀態（v4 實作完成）。v5 以 v4 為底（本人 2026-10-12 兩輪裁定）：
v5.1 冷卻只收 `until`（拿掉 `window` 5h／1w；list／get 的 `cooldown` 欄位改成單一 `until` 或 null）；
v5.2 拿掉 `lgw connect`（sidecar 直接 curl 本機 port，通道由 MyLinuxPool capability／mlp／ssh -J 提供）；
v5.3 反向隧道（`llmgateway-tunnel` 與其 unit）搬到 `ops-scripts/`；
v5.4 金鑰正本是 `FATESAIKOU/LLMGateway-keys` 的 `keys.env.enc`，key-sync（放 `ops-scripts/`）約每 10 分鐘用 `gh` 拉（使用前提：以 `llmgw` 身分登入過 `gh`，token 二選一）；拿掉兩個 workflow、`remote-deploy.sh` 整檔、SIGHUP 重讀、部署公鑰／forced command、部署連線 secret（升級選乙：管理帳號手動 ssh 上去跑 `install.sh --tag`）；
v5.5 worker 本機 port 預設 4710（可改）；手機自行用 ssh -J 等方式轉發（MyAiEntry#147 關掉，LLMGateway 不提供程式）；sidecar 內部層次與 sshproxy 只改文件說法。

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

- `deploy/bootstrap.sh`：**修改**——拿掉部署公鑰步驟（`--deploy-pubkey-file`、`step_deploy_key`、authorized_keys forced command）；隧道執行檔與 unit 的引用路徑改到 `ops-scripts/`；改放解密密碼（`--decrypt-pass-file` 或互動輸入，寫 `keysync.env`）；`gh` 登入由本人手動做，bootstrap 不做。
- `deploy/install.sh`：**修改**——隧道執行檔與 unit 改從 `ops-scripts/` 安裝；`--check` 加上「`gh` 能不能用」；其餘（tag 取碼、current 切換、unit 安裝）不變。
- `deploy/llmgateway-run`：**保留**——組 `python3 -m lgwd` 參數的 wrapper，與 v5 五件事都無關。
- `deploy/llmgateway-tunnel`：**搬移**——內容不變，搬到 `ops-scripts/`（維運工具定位，v5.3）。
- `deploy/llmgateway-tunnel.service`：**搬移**——同上，跟著隧道守護走。
- `deploy/llmgateway.service`：**修改**——只拿掉 `ExecReload … HUP` 那一行（SIGHUP 重讀沒了）；其餘不變。
- `deploy/remote-deploy.sh`：**拿掉**——升級選乙，部署入口整組不用了，升級改手動跑 `install.sh --tag`。

## .github/workflows/

- `.github/workflows/deploy-keys.yml`：**拿掉**——金鑰改由 key-sync 用 `gh` 從 private repo 定期拉，不再經 workflow 送。
- `.github/workflows/deploy-service.yml`：**拿掉**——升級選乙，改手動跑 `install.sh --tag`。

## tests/

- `tests/__init__.py`：**保留**——空檔。
- `tests/test_bootstrap.py`：**修改**——拿掉 deploy-key 步驟的測試，隧道路徑改 `ops-scripts/`。
- `tests/test_client.py`：**修改**——cooldown 新 body `{until}` 與新錯誤碼。
- `tests/test_connect.py`：**拿掉**——跟著 `lgw/connect.py` 走。
- `tests/test_deploy_script.py`：**拿掉**——跟著 `remote-deploy.sh` 整檔走。
- `tests/test_http.py`：**修改**——冷卻路由的新語意與錯誤碼。
- `tests/test_install.py`：**修改**——隧道檔改從 `ops-scripts/` 裝的斷言，加上「`gh` 能不能用」的 `--check` 斷言。
- `tests/test_keys.py`：**保留**——格式驗證邏輯不變。
- `tests/test_main.py`：**修改**——拿掉 SIGHUP 重讀的測試，改測 mtime 偵測重讀（提案見 v5-plan）。
- `tests/test_package.py`：**修改**——子模組剩三個（`__init__`、`client`、`signals`），拿掉 `lgw.connect` 斷言。
- `tests/test_policy.py`：**修改**——政策欄位更名／拿掉 buffer。
- `tests/test_registry.py`：**修改**——單一 `until` 的登錄、查詢、`available`。
- `tests/test_signals.py`：**修改**——until-based 的 `Signal` 與 fallback（現在＋1 小時在 sidecar，函式庫只負責轉 `{until}`）。
- `tests/test_tunnel.py`：**修改**——測試留 `tests/`，只改被測檔路徑到 `ops-scripts/`。
- `tests/test_workflows.py`：**拿掉**——兩個 workflow 都沒了，無檔可測。

## docs/

- `docs/deploy.md`：**修改（只寫改寫大綱，不全文重寫）**——§0 全貌（自備通道、ops-scripts、`gh` 前提、手動升級）、§1（1.9 放部署公鑰整節刪、`gh` 登入與解密密碼、隧道路徑）、§2（部署連線與 key secret 整組收、只剩 DECRYPT 備份）、§3（自備通道、本機 port 預設 4710、手機自行轉發）、§4（4.1 換 key 改等拉取、4.2 升級改手動、4.5 外洩改加密檔流程）、§5（workflow 排錯整節刪、加 `gh` 排錯）都要重寫；大綱見 deploy.md 頂端橫幅。
- `docs/research/*.md`（既有研究）：**保留**——歷史調查，`quota-signals` 與 `claude-failover` 仍是 sidecar 判讀額度訊號的依據。
- `docs/research/v5-plan.md`：**新增（C 件）**——金鑰拉取流程、初次啟動引導、要本人做的事、估工、疑點。

## policy/

- `policy/default.json`：**修改**——拿掉 `cooldown_buffer_seconds`，`resets_at_max_ahead_seconds` 更名為 until 語意；priority／models／agents 不變。

## pyproject.toml

- **修改，但保留**——sidecar 仍用 `pip install "lgw @ git+…@lgw-vX.Y.Z"` 依賴 `lgw`（`client`＋`signals`），所以打包設定要留；只改 `description`（拿掉 connect 字樣）。`packages = ["lgw"]` 不變。

## 特別問題（逐一回答）

- `deploy-keys.yml`：拿掉（key-sync 用 `gh` 拉，不再經 workflow 送）。
- `deploy-service.yml`：拿掉（升級選乙，改手動）。
- `remote-deploy.sh` 整檔：拿掉（同上）。
- `bootstrap.sh` 的部署公鑰與 forced command 步驟：拿掉（部署私鑰／公鑰整組不用了；改放解密密碼，`gh` 登入由本人手動做）。
- `lgw/connect.py`：拿掉（v5.2；worker 通道是 MyLinuxPool capability 的事，Mac／手機用 mlp 或 ssh -J）。
- `pyproject`：還需要（`lgw` 照樣給 sidecar 當函式庫用，只改描述）。

## 實作階段的新增檔案（不在 v4 盤點內，先列供 C 件估工用）

- `ops-scripts/` 目錄（從 `deploy/` 搬入隧道守護與其 unit；key-sync 也放這裡，用 `gh`＋`openssl`，不用 `git`）。
- private repo `FATESAIKOU/LLMGateway-keys` 根目錄的 `keys.env.enc`（檔格式見 v5-plan §1.3）。
