# tasks：`resolve` 加選填 `data`（#117）

## 1. spec 與 change

- [x] 1.1 新增 change `openspec/changes/resolve-data/`（proposal＋design＋tasks＋delta）：`subscription-registry` 的「resolve」「resolve 的選把規則」「錯誤回覆」MODIFIED 加 `data`、隱私步驟與新碼，scenario 照 #102 草稿 §4.1 的每一條（含 roles×resolve 一致性、agent 先於 privacy、privacy 的 403 先於 409 `not_deployed`）；`role-allocation` 的「角色分配表」「角色候選端點」同步兩句。驗證：`openspec validate resolve-data --strict`。

## 2. 程式

- [x] 2.1 `lgwd/errors.py`：加 `privacy_not_allowed()`（403）與模組碼表一行；`bad_request` 的說明補「`data` 不在三個等級」。驗證：`tests/test_http.py` 的錯誤對應案例。
- [x] 2.2 `lgwd/http.py`：`_RESOLVE_PARAMS` 加 `data`；`_ROLES_PARAMS` 改成 `_RESOLVE_PARAMS` 的別名（不再重複）；docstring 與 `_resolve_query`／`_roles_query` 的說明同步；`service.resolve(...)` 傳 `data=q.get("data")`。驗證：`tests/test_http.py`（data 原樣傳到 service、空白值＝沒給、重複 400、roles 的 query 規則照舊）。
- [x] 2.3 `lgwd/registry.py` 的 `_pick`：形狀驗證段在 agent 之後加「`data` 不在 `PRIVACY_LEVELS` → 400」；步驟 3 與 4 之間加步驟 3.5（有給 `data` 時候選限於 `privacy` 不低於它，一個都不剩 → 403 `privacy_not_allowed`；不參與排序）。`resolve` 簽名加 `data=None`、傳進 `_pick`、`meta` 加 `data`；模組 docstring 與方法 docstring 同步。驗證：`tests/test_registry.py` 的 resolve 段。
- [x] 2.4 `lgw/client.py` 的 `resolve`：尾巴加 `data: str | None = None`，`data is not None` 才進 query（空字串照帶、服務端視為沒給）；模組 docstring 的簽名說明、範例與錯誤碼清單同步。驗證：`tests/test_client.py`。

## 3. 測試

- [x] 3.1 `tests/test_registry.py`：`resolve()` helper 加 `data`；`meta` 精確鍵集加 `data`（其餘斷言不弱化）；新案例——`data` 不合法 400、缺省不濾、`meta.data` 原樣／null、釘住被擋、未釘住被篩掉、全部不足 403、agent 先於 privacy、privacy 先於 model、privacy 先於 `not_deployed` 的 409、隱私不改 priority 排序；`RoleCandidatesTest` 加 roles×resolve 一致性。
- [x] 3.2 `tests/test_http.py`：`FakeService.resolve` 加 `data`（呼叫記錄從 6 項變 7 項，既有斷言同步）；`data` 原樣傳遞／空白／重複；`ResolveHttpTest` 端到端加 403 `privacy_not_allowed`、400 與 `meta.data`。
- [x] 3.3 `tests/test_client.py`：`test_resolve帶data`、編碼、省略不帶，以及空字串照帶（不經 server 的 query 形狀）。
- [x] 3.4 驗收指令：`python -m pytest tests/test_registry.py tests/test_http.py tests/test_client.py tests/test_policy.py tests/test_lgwctl_usage.py -q`（不跑全套；輸出貼在報告）。

## 4. 改壞驗證

- [x] 4.1 由審查者做（本單派工明定）；作者只跑正常路徑與拒絕條件。
