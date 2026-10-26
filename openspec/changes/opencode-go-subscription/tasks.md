# tasks：opencode-go 的登錄（#77）

## 1. 政策白名單與檢查

- [x] 1.1 `lgwd/policy.py`：`SUBSCRIPTIONS` 加 `opencode-go`；把 `_ZEN_ONLY` 改成 `_OPENCODE_ONLY = ("opencode-zen", "opencode-go")`，`agents` 不是剛好 `["opencode"]` 就丢 `PolicyError`（訊息仍只帶固定訂閱 id）。驗證：`tests/test_policy.py`（出廠政策可載入、順序含 go、`agents` 為 `["opencode", "claude-code"]`／`["claude-code"]`／`[]` 都拒收、剛好 `["opencode"]` 可載入）。
- [x] 1.2 `policy/default.json`：加 `opencode-go`（priority 15、`agents` `["opencode"]`、`allow` `["*-free"]`、`default` `iceman-contributor-free`）。驗證：`tests/test_policy.py` 逐筆釘住 default 與 allow 涵蓋；`kimi-k3` 不被涵蓋。

## 2. provider 與 key

- [x] 2.1 `lgwd/providers.py`：新增 `OpenCodeGoProvider`（`provider`／`opencode_provider_id` 都 `opencode-go`、`kind` `api-key`、`endpoint` `https://opencode.ai/inference/go/openai/v1`），加進 `PROVIDERS`；模組 docstring 一起更新。驗證：`tests/test_registry.py`（list／get 的 provider、endpoint、provider id；resolve 的 env）。
- [x] 2.2 `lgwd/keys.py`：`KEY_NAMES["opencode-go"] = "OPENCODE_ZEN_API_KEY"`，docstring 註明多個訂閱可共用一個鍵名、key 檔仍只寫一行。驗證：`tests/test_keys.py`（鍵名對到 Zen、兩個訂閱讀到同一個值、key 檔一行不算重複鍵）；`tests/test_keysync.py`（去重後的 key 檔照常驗證通過，重複鍵仍被拒）。

## 3. spec 與測試收尾

- [x] 3.1 新增 change `openspec/changes/opencode-go-subscription/`（proposal＋delta spec＋tasks）：MODIFIED `subscription-registry` 的「endpoint 的語意」與「env 的組成」。驗證：`openspec validate --all --strict`（只有既有的 `model-rotation-survey` 紅）。
- [x] 3.2 更新列舉 subscription 的既有測試（`test_registry.py`、`test_policy.py`、`test_keys.py`、`test_keysync.py`、`test_keysync_api.py`、`test_keysync_timer.py`、`test_http.py`、`test_main.py`）：數量與順序一起改，共用鍵名的 fixture 依鍵名去重。驗證：`python -m pytest tests/test_policy.py tests/test_registry.py tests/test_keys.py tests/test_keysync.py tests/test_http.py tests/test_client.py tests/test_main.py -q` 與 `tests/test_keysync_api.py tests/test_keysync_timer.py`。
- [x] 3.3 改壞驗證（副本 `.work/mut/`）：(a) 拿掉 `opencode-go` 的 agents 檢查 → policy 測試轉紅；(b) 把 `OpenCodeGoProvider.opencode_provider_id` 改成 `opencode` → registry 的 env 測試轉紅。原 worktree 不動。
