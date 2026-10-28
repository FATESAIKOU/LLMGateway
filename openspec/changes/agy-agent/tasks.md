# tasks：agy 的登錄（#107）

## 1. 設計（本單，只寫介面）

- [x] 1.1 新增 change `openspec/changes/agy-agent/`（proposal＋design＋tasks＋delta）：`subscription-registry` 新增「沒有憑證的訂閱」並 MODIFIED `get`／`共通 JSON schema`／`endpoint 的語意`／`登錄冷卻`／`錯誤回覆`／`resolve`／`resolve 的選把規則`／`env 的組成`；`role-allocation` MODIFIED `角色候選端點`；`key-vault` MODIFIED `key 只保管在一處`。驗證：`openspec validate agy-agent --strict`。
- [x] 1.2 調查與引用：`RESEARCH-107b.md`（認證、ToS、`-p`）、`OUT-research-486.md` §1.3／§2（driver 只吃 `meta.model`、sidecar 的請求與回覆形狀、`env` 的處理）、本 repo 的 `policy.py`／`providers.py`／`keys.py`／`registry.py`／`policy/default.json` 逐行引用。驗證：design 的每個介面句都有檔案行號或報告段落。

## 2. PR 1：介面骨架（不改出廠政策）

- [ ] 2.1 `lgwd/policy.py`：`AGENTS` 加 `agy`；新增 `_AGY_ONLY = ("antigravity",)` 與「`agents` 必須剛好 `("agy",)`」的檢查（訊息固定、不含值）；`load_policy` 的 docstring 與模組說明同步。驗證：`tests/test_policy.py`（`agents` 為 `["agy"]` 可載入；`["agy","opencode"]`／`["claude-code"]`／`[]` 拒收；其他訂閱列 `agy` 也拒收）。
- [ ] 2.2 `lgwd/providers.py`：新增 `GoogleProvider`（`provider="google"`、`kind="local-login"`、`endpoint=None`、`opencode_provider_id=""`），加進 `PROVIDERS`；模組 docstring 的 provider 清單同步；`build_env` 在 `agent == "agy"`（或 `kind == "local-login"`）直接回 `{}`，不落進 claude-code 分支。驗證：`tests/test_registry.py`（agy 的 env 為 `{}`、resolve 的 env 不含任何鍵）。
- [ ] 2.3 `lgwd/registry.py`：加一個部署判斷（`kind == "local-login"` → 已部署，否則 `self._keys.deployed`）並用在 `role_candidates`（`:183`）、`resolve` 步驟 6（`:224-228`，對 local-login 不讀 key）、`_usage_fields`（`:381`）、`_item`（`:484`）；`get_subscription` 對 local-login 在讀 key 之前回 409 `not_deployed`；模組與方法 docstring 同步。驗證：`tests/test_registry.py`（list 的 `available` true、`available_again_at` 只由冷卻算、resolve 200、get 409、roles 的 `available` true，且都沒有 `KeyFileError`）。
- [ ] 2.4 `lgwd/errors.py` 的碼表說明：400 的 agent 前綴加 `agy/`、409 `not_deployed` 補「沒有憑證的訂閱（`local-login`）的 get 一律回這個碼」。`lgw/client.py` 的 docstring 補 `agy` 的範例與同樣的說明（不改程式）。驗證：`tests/test_http.py`（`agent=agy` 的 200；未知 agent 仍 400）。
- [ ] 2.5 測試：`tests/test_registry.py` 的 provider 三欄表、list 順序、agy 的 resolve／get／roles；`tests/test_keys.py` 的 `KEY_NAMES` 集合**不變**（正好證明沒有新增鍵名），並加一條「list／resolve 對 `antigravity` 不會呼叫 `keys.deployed`／`keys.get`（不丟 `KeyFileError`）」；`tests/test_http.py` 的 query 傳遞。驗收指令：`python3 -m pytest tests/test_policy.py tests/test_registry.py tests/test_http.py tests/test_keys.py -q`。
- [ ] 2.6 改壞驗證（副本 `.work/mut/`）：(a) 拿掉 kind `local-login` 的部署判斷 → list／resolve 的 agy 測試轉紅；(b) `build_env` 拿掉 agy 的 `{}` → env 洩漏 `ANTHROPIC_*` 的測試轉紅；(c) 拿掉 `_AGY_ONLY` → policy 條款測試轉紅。原 worktree 不動。

## 3. PR 2：打開介面（只動政策資料與文件）

- [ ] 3.1 `policy/default.json` 加 `antigravity`：`priority` 35、`agents: ["agy"]`、`privacy: "public"`、`models.allow` 與 `default` 由本人提供（一模型一 row、裸 id）。驗證：`tests/test_policy.py`（出廠 7 筆、順序、default 被 allow 涵蓋、`agents` 條款）；`tests/test_registry.py`、`tests/test_main.py`（list 7 筆、agy 的 resolve／get）。
- [ ] 3.2 端到端：以出廠政策起 lgwd，`GET /v1/resolve?agent=agy&provider=google&model=<id>` 回 200、`env` 為 `{}`、`meta.agent`／`meta.provider`／`meta.kind`／`meta.model` 符合；`GET /v1/subscriptions` 的該筆 `available` 為 true、`usage.status` 為 `not_applicable`。驗證：`tests/test_http.py` 的 Resolve/List 端到端。
- [ ] 3.3 文件：README／`docs/deploy.md` 的政策範例補一筆（不含任何憑證）；client docstring 完成。驗證：`git diff --check`、`openspec validate agy-agent --strict`。

## 4. PR 3：等本人（選配）

- [ ] 4.1 roles：本人若決定納入，只在 `policy/default.json` 的 `roles.<role>.order` 加 `antigravity`（政策資料，不改程式）；`GET /v1/roles/<role>?agent=agy` 的 candidates 與 rank 由既有程式產生。驗證：`tests/test_registry.py` 的 roles 案例（若納入）。
- [ ] 4.2 `privacy`：本人若裁定提高，只改政策檔；resolve 不帶 `data` 時仍不過濾（spec 已寫明），要另外決定是否翻缺省。

## 5. 不做

- [ ] 5.1 不代管、不匯出、不讀寫 agy 的任何 OAuth 憑證；不加 `KEY_NAMES`、不進 key 檔、不改 key-sync。
- [ ] 5.2 不改 `env_schema`／`schema`；不加 usage fetcher；不改冷卻端點的語意；不改 `resolve` 的選把鍵。
- [ ] 5.3 不寫部署計畫、不改 MyLinuxPool 的 sidecar／config；worker 不用 agy（PMO 另寫）。
