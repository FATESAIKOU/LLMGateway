# tasks：agy 的登錄（#107）

## 0. 前置（不滿足就不開工）

- [ ] 0.1 本人已裁定 design §6 的第 1（訂閱 id／provider）、5（「不需 key」機制）、6（`get` 的回覆）項；本檔 2.x 的內容才成立。未裁定前維持「設計待修」，不開 PR 1。
- [ ] 0.3 PR 2（3.x，動出廠 policy/default.json 與部署）的前置：design §6 的第 2（`priority`）、3（`privacy`）、4（模型 id）、7（只給 Mac 的 A／B）項已由本人裁定；3.1 要寫入的值都是這幾項的結果，不得由作者代選。
- [ ] 0.2 `openspec/changes/resolve-data/`（#117）已歸檔：本 change 的 `resolve`／`resolve 的選把規則`／`錯誤回覆`／role-allocation 的「角色候選端點」以它的文字為基底，先後順序顛倒會整段覆蓋（proposal Impact）。驗證：`openspec list`／`ls openspec/changes` 沒有 `resolve-data`。

## 1. 設計（本單，只寫介面）

- [x] 1.1 新增 change `openspec/changes/agy-agent/`（proposal＋design＋tasks＋delta）：`subscription-registry` 新增「沒有憑證的訂閱」並 MODIFIED `get`／`共通 JSON schema`／`endpoint 的語意`／`登錄冷卻`／`錯誤回覆`／`resolve`／`resolve 的選把規則`／`env 的組成`；`role-allocation` MODIFIED `角色候選端點`；`key-vault` MODIFIED `key 只保管在一處`。驗證：`openspec validate agy-agent --strict`。
- [x] 1.2 調查與引用：`RESEARCH-107b.md`（認證、ToS、`-p`）、`OUT-research-486.md` §1.3／§2（driver 只吃 `meta.model`、sidecar 的請求與回覆形狀、`env` 的處理）、本 repo 的 `policy.py`／`providers.py`／`keys.py`／`registry.py`／`main.py`／`policy/default.json` 逐行引用。驗證：design 的每個介面句都有檔案行號或報告段落。

## 2. PR 1：介面骨架（不改出廠政策）

- [x] 2.1 `lgwd/policy.py`：`AGENTS` 加 `agy`，**`SUBSCRIPTIONS`（`:18`）同期加 `antigravity`**（政策載入的白名單在 `:253-255`，沒加就 `PolicyError("policy.subscriptions has unknown key")`、lgwd 不啟動、臨時政策 JSON 的測試也一樣）；新增 `_AGY_ONLY = ("antigravity",)` 與「`agents` 必須剛好 `("agy",)`」的檢查（訊息固定、不含值），以及其他訂閱列 `agy` 也拒收；`load_policy` 的 docstring（`:226` 的「鍵只能是 `SUBSCRIPTIONS`」）與模組說明同步。**兩個白名單不分岔落地**（中間狀態仍要能載入出廠政策）。驗證：`tests/test_policy.py`（含 `antigravity` 的政策檔可載入；`agents` 為 `["agy"]` 可載入；`["agy","opencode"]`／`["claude-code"]`／`[]` 拒收；其他訂閱列 `agy` 也拒收；把測試政策的鍵改成不在 `SUBSCRIPTIONS` 的值仍拒收）。
- [x] 2.2 `lgwd/providers.py`：新增 `GoogleProvider`（`provider="google"`、`kind="local-login"`、`endpoint=None`、`opencode_provider_id=""`），`PROVIDERS`（`:151-159`）加 `"antigravity": GoogleProvider()`；**不覆寫 `make_usage_fetcher`**（基底回 None，見 design §2.1 的不變式）；模組 docstring 的 provider 清單同步；`build_env` 在 `agent == "agy"` **或** `self.kind == "local-login"` 時直接回 `{}`（**兩條判斷都要寫**，任一成立即回；防線不依賴單一條件，兩條各擋一個方向，見 design §2.3），不落進 claude-code 分支。驗證：`tests/test_registry.py`（agy 的 env 為 `{}`、resolve 的 env 不含任何鍵；另以 `GoogleProvider` 直接呼叫 `build_env("claude-code", …)` 仍回 `{}`——`kind` 方向）。
- [x] 2.3 `lgwd/registry.py`：加一個部署判斷（`kind == "local-login"` → 已部署，否則 `self._keys.deployed`）並用在 `role_candidates`（`:183`）、`resolve` 步驟 6（`:224-228`，對 local-login 不讀 key）、`_usage_fields`（`:381`）、`_item`（`:484`）；`get_subscription` 對 local-login 在讀 key 之前回 409 `not_deployed`；模組與方法 docstring 同步。驗證：`tests/test_registry.py`（list 的 `available` true、`available_again_at` 只由冷卻算、resolve 200、get 409、roles 的 `available` true，且都沒有 `KeyFileError`）。
- [x] 2.4 `lgwd/errors.py` 的碼表說明：400 的 agent 前綴加 `agy/`、409 `not_deployed` 補「沒有憑證的訂閱（`local-login`）的 get 一律回這個碼」。`lgw/client.py` 的 docstring 補 `agy` 的範例與同樣的說明（不改程式）。驗證：`tests/test_http.py`（`agent=agy` 的 200；未知 agent 仍 400）。
- [x] 2.5 測試：`tests/test_registry.py` 的 provider 三欄表、list 順序、agy 的 resolve／get／roles；冷卻前綴收緊的案例（以 `model=agy/<id>` 登錄冷卻回 400 `bad_request`，即使 `allow` 的樣式涵蓋得了，對照 `tests/test_registry.py` 既有的 `opencode/` 案例）；`tests/test_keys.py` 的 `KEY_NAMES` 集合**不變**（正好證明沒有新增鍵名），把 `:55` 的測試名改成不暗示「所有訂閱都在 `KEY_NAMES`」（只改名、不改斷言），並加一條「list／resolve 對 `antigravity` 不會呼叫 `keys.deployed`／`keys.get`（不丟 `KeyFileError`）」；再加一條斷言 `provider_for("antigravity").make_usage_fetcher(deps)` 為 None（守住 design §2.1 的不變式，`main.py:240`／`:245` 的兩個呼叫點才不會被觸發）；`tests/test_http.py` 的 query 傳遞。驗收指令：`python3 -m pytest tests/test_policy.py tests/test_registry.py tests/test_http.py tests/test_keys.py -q`。
- [ ] 2.6 改壞驗證（副本 `.work/mut/`）：(a) 拿掉 kind `local-login` 的部署判斷 → list／resolve 的 agy 測試轉紅；(b) `build_env` 拿掉 agy 的 `{}` → env 洩漏 `ANTHROPIC_*` 的測試轉紅；(c) 拿掉 `_AGY_ONLY` → policy 條款測試轉紅；(d) 把 `SUBSCRIPTIONS` 的 `antigravity` 拿掉 → 政策載入測試轉紅（H1 的守門）。原 worktree 不動。

## 3. PR 2：打開介面（只動政策資料與文件）

- [ ] 3.1 `policy/default.json` 加 `antigravity`：`priority` 35、`agents: ["agy"]`、`privacy: "public"`、`models.allow` 與 `default` 由本人提供（一模型一 row、裸 id）。**這是 design §3 B（同一份政策、靠自律）的落點**：出廠政策所有實例共用，所以 worker 也會看到這把；若本人改判 A，本項改成「新增 Mac 專用政策範例檔、不動 `policy/default.json`」，並另加兩份政策同步的維護項與測試。驗證：`tests/test_policy.py`（出廠 7 筆、順序、default 被 allow 涵蓋、`agents` 條款）；`tests/test_registry.py`、`tests/test_main.py`（list 7 筆、agy 的 resolve／get）。
- [ ] 3.2 端到端：以出廠政策起 lgwd，`GET /v1/resolve?agent=agy&provider=google&model=<id>` 回 200、`env` 為 `{}`、`meta.agent`／`meta.provider`／`meta.kind`／`meta.model` 符合；`GET /v1/subscriptions` 的該筆 `available` 為 true、`usage.status` 為 `not_applicable`。驗證：`tests/test_http.py` 的 Resolve/List 端到端。
- [ ] 3.3 文件：README／`docs/deploy.md` 的政策範例補一筆（不含任何憑證）；**`docs/deploy.md` 寫明 advisory 的限度**——這把只在 Mac 能用，但所有實例的 list 都會顯示 `available: true`，worker 誤用會拿到 200＋空 env 之後 503（design §3 B、proposal Impact）；client docstring 完成。驗證：`git diff --check`、`openspec validate agy-agent --strict`。

## 4. PR 3：等本人（選配）

- [ ] 4.1 roles：本人若決定納入，只在 `policy/default.json` 的 `roles.<role>.order` 加 `antigravity`（政策資料，不改程式）；`GET /v1/roles/<role>?agent=agy` 的 candidates 與 rank 由既有程式產生。驗證：`tests/test_registry.py` 的 roles 案例（若納入）。
- [ ] 4.2 `privacy`：本人若裁定提高，只改政策檔；resolve 不帶 `data` 時仍不過濾（spec 已寫明），要另外決定是否翻缺省。

## 5. 不做

- [ ] 5.1 不代管、不匯出、不讀寫 agy 的任何 OAuth 憑證；不加 `KEY_NAMES`、不進 key 檔、不改 key-sync。
- [ ] 5.2 不改 `env_schema`／`schema`；不加 usage fetcher；不改冷卻端點的語意；不改 `resolve` 的選把鍵。
- [ ] 5.3 不寫部署計畫、不改 MyLinuxPool 的 sidecar／config；worker 不用 agy（PMO 另寫）。
