Claude Sonnet 5.5（claude-sonnet-5-5）

# pstack 審查：LLMGateway#41（impl/issue41-resolve）

範圍：`git diff origin/main...HEAD` 的 lgwd／lgw／tests，對照 spec／design／tasks 1.0～1.6。作者報告未採信。未跑全套測試，只讀碼與對照。

## Findings

### 1. [warning] client.resolve 讀不到 409 cooling 的 `until`
**Location**: lgw/client.py:79-90（`_finish`）、lgw/client.py:112-133（`resolve`）、lgw/client.py 檔頭 docstring
**Finding**: `_finish` 對非 2xx 一律只回 `Result(ok=False, status, error)`，不放 `data`。因此 `cooling` 的 `until`（server 已放在 body）到不了呼叫方。docstring 還寫明「until 在錯誤回覆的 body，錯誤一律不進 Result.data」。
**Evidence**: spec 把 `until`＝最早恢復時刻當作 cooling 的用途（「呼叫方把重試排到那個時刻」；errors.py 的註解同）。sidecar 透過 `client.resolve` 拿到 `error="cooling"` 後沒有任何欄位能取得時刻，只能再打一次 `list`。這等於 tasks 1.x 要求的 `client.resolve` 少了一半契約。測試 tests/test_client.py 也只驗到 error 碼，沒驗 until 可取得。
**Suggestion**: 錯誤回覆除 `error` 外，把非 `error` 的欄位（`until`、`missing`）放進 `Result.data`；不含 `env`，因為 server 保證錯誤回覆沒有 env。補一個 cooling 的 client 測試。

### 2. [warning] 「已部署」檢查與取值分兩次讀 key（TOCTOU），可能 200 帶 null 憑證
**Location**: lgwd/registry.py（步驟 6 `self._keys.deployed(...)` 與 txn 內 `self._keys.get(selected.name)`）
**Finding**: 步驟 6 用 `deployed()` 過濾，幾十行後才用 `get()` 取值。兩次呼叫各自拿 KeyStore 的鎖，中間 `keysync`／mtime 重讀可能把該把 key 換成缺鍵或空字串。
**Evidence**: `KeyStore.get` 在沒部署時回 `None`。路徑：請求 A 通過 deployed → 重讀換成空 key → `get()` 回 None → `build_env(agent, None, model)` 組出 `{"OPENCODE_API_KEY": None}` → 200 並寫稽核。違反「env 的憑證值不可空」，且 `if key is None` 的防禦在 get_subscription 有、resolve 沒有。註解寫「deployed 檢查已保證 key 非 null」在並行下不成立。
**Suggestion**: 候選階段只取一次 `key = self._keys.get(name)`（None＝未部署），用 `(sub, key)` 往下傳；或至少在 build_env 前對 None 丟 `not_deployed()`。

### 3. [warning] `_derivable` 與 `_derive_model` 把同一條「推導模型」規則寫兩份
**Location**: lgwd/registry.py `_derivable`（約 65-72 行）、`RegistryService._derive_model`
**Finding**: 兩處都在判斷 kind=claude-subscription／default 非 null／allow 有無不含萬用字元的樣式。前者回 bool 供過濾，後者回值；後者結尾還要加 `raise no_model()  # pragma: no cover` 的防禦分支，因為作者知道兩份規則得人工保持同步。
**Evidence**: spec「model 省略時的語意」是一條規則。兩份實作＋一個不可達分支＝將來改規則（例如加新 kind）漏改其一，就會出現「過濾通過、推導時 400」，正是 design 決定 3 要避免的情形。
**Suggestion**: 一個函式 `_derive_model(sub, kind) -> tuple[bool, str | None]`（或回 sentinel），步驟 4 對每個候選算一次並保留 `(sub, model)`；步驟 8 直接用被選中的那組，刪除 `_derivable`、不可達的 raise 與 pragma。

### 4. [warning] `EnvSchemaContractTest` 是空測
**Location**: tests/test_registry.py:912-931
**Finding**: 「拒收」的邏輯寫在測試自己的 `_sidecar_double`（`payload["env_schema"] > KNOWN`），測試只是驗證這個比較式。Gateway 程式沒有任何行為被它覆蓋，刪掉 Gateway 的 env_schema 檢查邏輯，或把 `ENV_SCHEMA_VERSION` 改成 5，測試仍會（在第一個 assert 以外）通過。
**Evidence**: 唯一碰到 Gateway 的是 `payload["env_schema"]` 為整數（可比較）。這個性質用 `assertIsInstance(payload["env_schema"], int)`／`assertEqual(..., 1)` 一行就能表達；其餘屬於 sidecar 側（design 決定 6，本單不負責）。
**Suggestion**: 刪掉替身，改成對 `schema`／`env_schema` 值的直接斷言（若 test_meta 已涵蓋就併入）。

### 5. [warning] 開機檢查的例外型別與 spec 不符，且把 main 的 except 範圍放寬到所有 ValueError
**Location**: lgwd/providers.py `validate_endpoints`（raise ValueError）、lgwd/main.py:289-293（except 加入 `ValueError`）
**Finding**: spec「共通 JSON schema」寫的是拒絕啟動（`PolicyError`）。實作丟 `ValueError`，為了讓 main 顯示「cannot start」而把 `except` 加上 `ValueError`。
**Evidence**: `ValueError` 是極廣的型別；Gateway 建構期間任何其他地方（例如解析、datetime、sqlite 包裝）冒出的 ValueError 現在都會被當成「正常的設定錯誤」印成 `cannot start: ...` 並回 2，而不是帶 traceback 的真正 bug。作者的理由是 providers.py 不 import policy 避免循環，但可以把 PolicyError 搬到不依賴 providers 的位置（例如 errors.py），或定義一個專用的 `EndpointError`。
**Suggestion**: 用 PolicyError（或專用例外）並還原 main 的 except；test_main 對應改 assertRaises 的型別。

### 6. [warning] `RegistryService.resolve` 一個方法塞了驗證、8 步選把、txn、組 env、稽核、組 meta
**Location**: lgwd/registry.py resolve（約 105-235 行，≈130 行）
**Finding**: 整段是依序縮減 `candidates` 的 list comprehension，每步都現場呼叫 `provider_for(sub.name)`（共 5 處），釘住／未釘住用 `if subscription is not None` 在四處重複分岔，與 I/O（keys、store txn、稽核）混在同一個方法。
**Evidence**: 選把規則本身（步驟 1～5、8）是純函式：輸入政策、請求參數，輸出候選；步驟 6、7 才碰 keys／DB。現在 reader 必須在 130 行裡追 `candidates` 的狀態；要測 precedence 也得走完整 service。registry.py 331 行，離 1k 還遠，但增量集中在這一個方法，是 Code Quality Lens 說的 ad-hoc 分支成長。
**Suggestion**: 抽 `_pick(...)`（純函式：形狀驗證＋步驟 1～5，回 `[(sub, provider_info, model)]`），`resolve` 只留 keys／冷卻／稽核／組回覆。順手預先算好 `provider_info`，消掉重複的 `provider_for` 呼叫。

### 7. [nit] 孤兒註解與簡繁混用
**Location**: lgwd/registry.py:`_has_wildcard` 上方的「# resolve 認得的 agent 與 AGENTS 同一張表…」；resolve docstring 與步驟 5 註解的「没有」（簡體）
**Finding**: 該註解原本屬於某個常數，現在下面沒有東西可註解，成了孤兒；「没有」與全檔用的「沒有」不一致。
**Suggestion**: 刪孤兒註解，統一成「沒有」。

### 8. [nit] http.py 的 get 與 resolve 用兩套 query 解析
**Location**: lgwd/http.py（get 分支 `parse_qs(split.query).get("agent") or [None]` 與新的 `_resolve_query`）
**Finding**: 同檔兩種 query 規則（get 不認重複、不管空值；resolve 嚴格）。get 是過渡期且 spec 沒要求動，這點可接受，但註解沒標明為何刻意不同，之後 d 單移除 get 前容易有人「順手統一」而改變 get 行為。
**Suggestion**: 在 get 分支加一行註解說明刻意不動，或不處理（過渡期）。

### 9. [nit] model 值沒有字元限制就直接進 env 與稽核
**Location**: lgwd/registry.py `_covered`／resolve、lgwd/providers.py `build_env`
**Finding**: `*-free`、`*:free` 這類 allow 樣式的 fnmatch（`*` 對應 `.*` 且含換行）會放行含換行、NUL、任意長度的 model 字串，原樣寫進 `env["ANTHROPIC_MODEL"]` 與 audit。
**Evidence**: 呼叫端是自家 sidecar、走 SSH 通道，風險低；但 sidecar 把值放進子行程環境時，含 NUL 會讓 `subprocess` 丟 ValueError，含換行會污染 `.env` 型輸出。spec 沒要求，故只列 nit（若要處理是 [質疑設計]，由 lead 判斷）。

### 10. [nit] `validate_endpoints` 的樣式是子字串比對
**Location**: lgwd/providers.py `_ENDPOINT_CREDENTIAL_MARKS`
**Finding**: `"key=" in lowered` 會命中 `monkey=`、`turkey=`（誤擋），而 `apikey=`、`secret=`、`password=`、`sig=` 不會命中（漏擋）。spec 的樣式表就是這四個，所以實作與 spec 一致；僅提醒這是第一道淺防線，注釋已說明 canary 掃描是第二道，無需動作。

## 核對結果（無問題的項目，僅供 lead 對照）
precedence（400→404→403／no_model→409）、釘住不代換、exclude 在釘住時不套用、no_candidate 不回 until、稽核只記成功、audit 欄位遷移（PRAGMA＋ALTER，冪等）、`+`／重複參數／空值規則、200 的 no-store 標頭，經對照 spec 與測試名稱未見偏差。
