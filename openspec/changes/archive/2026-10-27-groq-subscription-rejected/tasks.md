# tasks：groq 的登錄（#88）

## 1. 政策白名單與檢查

- [x] 1.1 `lgwd/policy.py`：`SUBSCRIPTIONS` 加 `groq`；`_OPENCODE_ONLY` 加 `groq`（`agents` 不是剛好 `["opencode"]` 就丟 `PolicyError`，訊息只帶固定訂閱 id）。驗證：`tests/test_policy.py`（出廠政策可載入、順序含 groq、`agents` 為 `["opencode", "claude-code"]`／`["claude-code"]`／`[]` 都拒收、剛好 `["opencode"]` 可載入）。
- [x] 1.2 `policy/default.json`：加 `groq`（priority 25、`agents` `["opencode"]`、`allow` 三個免費層 id、`default` `openai/gpt-oss-120b`）。驗證：`tests/test_policy.py` 逐筆釘住 default 與 allow 涵蓋；allow 不含萬用字元；`llama-3.1-8b-instant` 不被涵蓋。

## 2. provider 與 key

- [x] 2.1 `lgwd/providers.py`：新增 `GroqProvider`（`provider`／`opencode_provider_id` 都 `groq`、`kind` `api-key`、`endpoint` `https://api.groq.com/openai/v1`），加進 `PROVIDERS`；模組 docstring 一起更新；不含 usage fetcher。驗證：`tests/test_registry.py`（list／get 的 provider、endpoint、provider id；resolve 的 env；`usage` 為 `not_applicable`）。
- [x] 2.2 `lgwd/keys.py`：`KEY_NAMES["groq"] = "GROQ_API_KEY"`。驗證：`tests/test_keys.py`（鍵名、讀值、沒共用鍵名、值不進 repr／錯誤訊息）。

## 3. spec 與測試收尾

- [x] 3.1 新增 change `openspec/changes/groq-subscription/`（proposal＋delta spec＋tasks）：MODIFIED `subscription-registry` 的「endpoint 的語意」與「env 的組成」。驗證：`openspec validate groq-subscription --strict`。
- [x] 3.2 更新列舉 subscription 的既有測試（`test_registry.py`、`test_policy.py`、`test_keys.py`、`test_keysync.py`、`test_http.py`、`test_main.py`）：數量與順序一起改。驗證：`python3 -m pytest tests/test_policy.py tests/test_keys.py tests/test_registry.py tests/test_http.py tests/test_main.py tests/test_keysync.py -q`。
- [x] 3.3 改壞驗證（副本 `.work/mut/`）：(a) 拿掉 `_OPENCODE_ONLY` 的 `groq` → policy 測試轉紅；(b) 把 `GroqProvider.endpoint` 改成別的位址 → registry 的 endpoint／env 測試轉紅；(c) `KEY_NAMES["groq"]` 改成別人的鍵名 → key 測試轉紅。原 worktree 不動。
