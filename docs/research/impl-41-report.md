DeepSeek V4.1 Flash

# LLMGateway #41 實作報告：`GET /v1/resolve`

- 日期：2026-10-20
- 分支：`impl/issue41-resolve`（從 `main` 開；本報告與修正都 commit 到這個分支）
- 範圍：`openspec/changes/agent-provider-model-interface/tasks.md` 第 1 節 1.0～1.6。1.7（發版）與第 2～5 節（sidecar、實機驗收、get 退場、銜接）不在本單。
- 設計與規格：`openspec/changes/agent-provider-model-interface/`（spec 的每個服務端 Scenario 都有對應測試，見下）。

## 做了什麼（依 1.0～1.6）

### 1.0 providers 的啟動檢查

- `lgwd/policy.py`：`load_policy` 補 openrouter 的 `agents == ("opencode",)` 檢查，不合法丟固定字串 `PolicyError`（`openrouter must only allow opencode`）。
- `lgwd/providers.py`：新增 `validate_endpoints()`，走 `PROVIDERS` 逐筆驗 `CredentialProvider.endpoint`；含 `key=`／`token=`／`api_key=`／`access_token=` query 樣式或 userinfo `@` 即丟固定字串 `ValueError`（不回顯值）。
- `lgwd/main.py`：`Gateway.__init__` 在 `load_policy` 之後呼叫 `validate_endpoints()`；`ValueError` 在組裝根轉成固定字串 `PolicyError`（providers 不 import policy），`main()` 只接 `PolicyError`／`KeyFileError`／`OSError`（不合法不啟動）。`load_policy` 不呼叫它，`PolicyError` 留在 policy.py。

### 1.1 選把與錯誤碼

- `lgwd/errors.py`：新增 `model_not_allowed`（403）、`no_model`（400）、`cooling(until)`（409，帶 `until`）、`no_candidate`（409）。
- `lgwd/registry.py`：新增 `RegistryService.resolve(agent, provider, model=None, subscription=None, exclude=None)`，選把集中在這一個方法：
  1. 輸入形狀（缺 `agent`／`provider` 帶 `missing`、未知 agent、`subscription`＋`exclude` 同時給、`subscription` 與 `provider` 不一致）→ 400 `bad_request`；
  2. 未知 `subscription` id、未知 provider／provider 無 subscription → 404 `not_found`；
  3. 釘住先限縮成該把，再過 provider／agent；無一允許 → 403 `agent_not_allowed`；
  4. `model` 有給：`models.allow` fnmatch 涵蓋，無一涵蓋 → 403 `model_not_allowed`；`model` 省略：限於能推導模型的候選（`claude-subscription`、default 非 null、或 allow 至少一個不含萬用字元的樣式），無一 → 400 `no_model`（釘住時只驗該把，不代換）；
  5. 未釘住時套 `exclude`（未知 id 忽略），無候選 → 409 `no_candidate`；
  6. 排除 key 未部署 → 409 `not_deployed`；
  7. 排除冷卻中，全部冷卻 → 409 `cooling`（`until`＝最早恢復時刻）；
  8. `priority` 最小、同分比 id。
- 錯誤 precedence 與步驟同軸：形狀 400 → 查無 404 → 政策 403／`no_model` → 狀態 409。

### 1.2 env 組成

- `lgwd/providers.py`：`CredentialProvider.build_env(agent, model)` 依（provider、kind、agent）組 env：
  - claude-code＋claude-subscription：`CLAUDE_CODE_OAUTH_TOKEN`；
  - claude-code＋api-key（anthropic，現行不可達的預留形狀）：`ANTHROPIC_API_KEY`；
  - claude-code＋api-key（其他 provider）：`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY=""`（刻意佔位）、endpoint 非 null 時的 `ANTHROPIC_BASE_URL`；
  - 模型非 null 時 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`（同值）；
  - opencode：只回片段 `OPENCODE_API_KEY`／`OPENCODE_PROVIDER_ID`／`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`；不回 `OPENCODE_CONFIG_CONTENT`、不回 `baseURL`。
- provider id 對映在 providers：`opencode-zen → opencode`、`openrouter → openrouter`、`ollama-cloud → ollama-cloud`。

### 1.3 HTTP

- `lgwd/http.py`：新增 `GET /v1/resolve` 路由；query 認得 `agent`／`provider`／`model`／`subscription`／`exclude`，未知參數忽略；`parse_qs` form 解碼，空值視為沒給（`subscription=x&exclude=` 是合法釘住）；已知參數重複出現（含其中一次是空值）→ 400 `bad_request`。
- 200 回 `{schema, env_schema, meta, env}`；resolve 與過渡期 get 的 200 都加 `Cache-Control: no-store`、`Pragma: no-cache`。
- log 只記方法／路徑／狀態碼（query 不進 log）；500 另記例外種類，不記值。
- `model` 的 `+` 由呼叫端編成 `%2B`，server 解回；帶 `/` 的 model 照 allow 比對（不正規化）。

### 1.4 稽核與 DB 遷移

- `lgwd/store.py`：`SCHEMA` 的 audit 加 `provider TEXT`／`model TEXT`（nullable）；啟動時 `PRAGMA table_info(audit)` 檢查，缺欄位以 `ALTER TABLE audit ADD COLUMN …` 補上（只增不改）。
- `audit_add`／`audit_all` 帶新欄位；resolve 記（時刻、subscription、agent、provider、model），get（過渡期）照舊只記 agent，冷卻照舊記 `until`；失敗的 resolve 不記稽核。

### 1.5 測試

- spec 的服務端 Scenario 全部有測試（對照表見下），loopback＋假 key＋假時鐘。
- canary 只掃憑證值：`CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`OPENCODE_API_KEY` 的值不出現在 log／DB／稽核／list 回覆；另掃 endpoint 值的憑證樣式（`key=`／`token=`／`api_key=`／`access_token=`／`@`）。
- 舊 DB：從 v5 schema 的 DB 檔啟動新 lgwd，補欄位後寫入 resolve，舊資料新欄位為 null；以 v5 的欄位清單 SELECT 驗退版可讀。
- `model=a+b` 編碼（client 編成 `%2B`、server 解回）、Cache-Control 斷言；`schema`／`env_schema` 由 resolve 回覆直接斷言（pstack 第 1 輪刪除 sidecar 替身空測）。

### 1.6 客戶端

- `lgw/client.py`：新增 `resolve(agent, provider, model=None, subscription=None, exclude: list[str] | None = None)`；`exclude` 由 client 用 `",".join` 合併；每個 query 值用 `urllib.parse.quote(value, safe="")`；`Result` 的 repr 沿用不印 data；`get` 保留（並存期）。resolve 的非 2xx 回覆另把 `error` 以外的欄位放進 `Result.data`（pstack 第 1 輪）。

## 收尾修正（本次接手）

前一位作者（GLM-5.3-Flash Free）的 checkpoint `aa6ce5c` 已有約 1280 行；本次接手做了：

1. 修正全測試的 1 error：`tests/test_registry.py::EnvSchemaContractTest` 繼承 `RegistryCase`，但 `resolve()` 小工具只定義在 `ResolveTest`。把 `resolve()` 上移到 `RegistryCase`（與 `get()`／`cooldown()` 同一區），`EnvSchemaContractTest` 與其他 case 都能用（該空測後來在 pstack 第 1 輪刪除，見末節）。
2. `lgwd/http.py`：重複 query 參數的判定改用 `parse_qs(..., keep_blank_values=True)` 計次，讓 `agent=&agent=opencode` 也回 400（空白仍視為沒給；`subscription=x&exclude=` 不受影響）。
3. `lgwd/registry.py`：移除前作者留下、未被使用也與 `provider_for` 不一致的 `providers=` 注入參數（design 沒有這個接縫）。
4. `tests/test_http.py`：移除一個沒有斷言的重複 resolve 呼叫；補「重複出現含空白」的案例。
5. `tests/test_client.py`：修正 cooling 測試的註解；該行為在 pstack 第 1 輪改為 resolve 保留 `until`（見末節）。
6. `tests/test_registry.py`：補規格有寫但沒測到的規則——`agent_not_allowed`（未釘住／釘住）、未知 pinned id（404；同時給 `exclude` 時 400 先於 404）、`exclude` 未知 id 忽略、provider 在政策檔沒有 subscription（404）、同 priority 同 provider 以 id 排序、`allow` 第一個不含萬用字元的項當預設、`exclude` 後無候選時不因被排除那把冷卻而回 `cooling`。

## Scenario → 測試對照

測試檔都在 `tests/`；下表用 `檔案::類別::方法`。

### Requirement: resolve

| Scenario | 測試 |
|---|---|
| 同一把的兩個免費模型 | `test_registry.py::ResolveTest::test_同一把的兩個免費模型`；`…::test_opencode的兩個模型_片段` |
| 釘住公司那一把 | `test_registry.py::ResolveTest::test_釘住公司那一把`；`test_http.py::ResolveHttpTest::test_resolve端到端_預設與釘住` |
| 釘住的那把冷卻中 | `test_registry.py::ResolveTest::test_釘住的那把冷卻中` |
| 釘住的 subscription 與 provider 不一致 | `test_registry.py::ResolveTest::test_釘住的subscription與provider不一致`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |
| 釘住與排除同時給 | `test_registry.py::ResolveTest::test_釘住與排除同時給`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |
| 換把時排除剛耗盡的那一把 | `test_registry.py::ResolveTest::test_換把時排除剛耗盡的那一把` |
| 排除後沒有候選 | `test_registry.py::ResolveTest::test_排除後沒有候選` |
| 缺必填 | `test_registry.py::ResolveTest::test_缺必填`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |
| 未知的 agent | `test_registry.py::ResolveTest::test_未知的agent`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |
| meta 只帶有資訊的欄位 | `test_registry.py::ResolveTest::test_meta只帶有資訊的欄位` |
| 值不外洩與不快取 | `test_registry.py::ResolveTest::test_值不外洩`（稽核／DB／list）；`test_http.py::ResolveHttpTest::test_resolve_log不含憑證值`（log）；`test_http.py::ApiTest::test_resolve_200帶不快取標頭`、`…::test_get_200帶不快取標頭` |

### Requirement: resolve 的選把規則

| Scenario | 測試 |
|---|---|
| 同 provider 兩把，一把冷卻中 | `test_registry.py::ResolveTest::test_同provider兩把一把冷卻中` |
| 全部候選冷卻中 | `test_registry.py::ResolveTest::test_全部候選冷卻中`；`test_http.py::ApiTest::test_resolve錯誤對應_不含env` |
| model 不在 allow | `test_registry.py::ResolveTest::test_model不在allow` |
| 帶 provider 前綴的 model | `test_registry.py::ResolveTest::test_帶provider前綴的model`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |
| 未知 provider | `test_registry.py::ResolveTest::test_未知provider`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |
| 最高 priority 的候選推不出模型 | `test_registry.py::ResolveTest::test_最高priority的候選推不出模型選下一個` |
| 釘住那把推不出模型，同 provider 另一把可推導 | `test_registry.py::ResolveTest::test_釘住那把推不出模型不代換` |

### Requirement: env 的組成

| Scenario | 測試 |
|---|---|
| claude 訂閱不帶模型變數 | `test_registry.py::ResolveTest::test_claude訂閱不帶模型變數` |
| opencode 的兩個模型（片段） | `test_registry.py::ResolveTest::test_opencode的兩個模型_片段`（另有 `test_opencode的ollama_env`、`test_claude_code的api_key形狀` 補鍵集） |

### Requirement: model 省略時的語意

| Scenario | 測試 |
|---|---|
| claude-subscription 的預設 | `test_registry.py::ResolveTest::test_claude_subscription的預設` |
| api-key 的預設 | `test_registry.py::ResolveTest::test_api_key的預設`；`test_registry.py::ResolveTest::test_api_key的預設_allow第一個具體項` |
| 推不出模型 | `test_registry.py::ResolveTest::test_推不出模型_假政策`；`…::test_釘住那把推不出模型不代換` |

### Requirement: 稽核紀錄（MODIFIED）

| Scenario | 測試 |
|---|---|
| resolve 留下紀錄 | `test_registry.py::ResolveTest::test_resolve留下紀錄`；`…::test_resolve失敗不記稽核`；`…::test_resolve留紀錄model為null`；`…::test_get與cooldown的稽核照舊` |
| 舊 DB 沒有新欄位 | `test_registry.py::AuditMigrationTest::test_從v5schema的DB檔啟動並寫入resolve` |

### Requirement: 錯誤回覆

| Scenario | 測試 |
|---|---|
| 候選全部冷卻中 | `test_registry.py::ResolveTest::test_全部候選冷卻中` |
| 模型被政策擋下 | `test_registry.py::ResolveTest::test_model不在allow` |
| 排除後沒有候選 | `test_registry.py::ResolveTest::test_排除後沒有候選` |
| 未知 agent 是輸入錯誤 | `test_registry.py::ResolveTest::test_未知的agent` |
| 輸入形狀錯誤先於查無 | `test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence`；`test_registry.py::ResolveTest::test_釘住未知id` |

### Requirement: 共通 JSON schema

| Scenario | 測試 |
|---|---|
| list 不變 | `test_registry.py::JsonSchemaTest::test_Claude訂閱的描述`（既有）；`test_registry.py::ListTest::test_不含秘密`、`…::test_依priority排序`（既有） |
| 政策檔把 openrouter 的 agents 改寬 | `test_policy.py::PolicyValidationTests::test_openrouter_agents_must_only_allow_opencode` |
| provider 表的 endpoint 內嵌憑證 | `test_policy.py::ValidateEndpointsTests::test_query憑證樣式被拒`、`…::test_userinfo樣式被拒`、`…::test_error_message_carries_no_values`；`test_main.py::ProviderEndpointGateTest::test_假provider表endpoint內嵌憑證_組裝失敗` |

### Requirement: models.default 的語意

| Scenario | 測試 |
|---|---|
| 供應商自己的模型 id | `test_registry.py::ResolveTest::test_供應商自己的模型id` |
| 帶前綴的 model 不自動正規化 | `test_registry.py::ResolveTest::test_帶provider前綴的model`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤precedence` |

### 1.5 另外要求的測試（非 Scenario）

| 要求 | 測試 |
|---|---|
| query 空白值與重複鍵 | `test_http.py::ApiTest::test_resolve_query規則`；`test_http.py::ResolveHttpTest::test_resolve空白值與重複鍵` |
| `model=a+b` 編碼 | `test_client.py::ResolveTest::test_resolve_model_plus編碼`；`test_http.py::ApiTest::test_resolve_query規則` |
| `schema`／`env_schema` 的值 | `test_registry.py::ResolveTest::test_同一把的兩個免費模型`（直接斷言皆為 1） |
| resolve 的錯誤碼對應與不含 `env` | `test_http.py::ApiTest::test_resolve錯誤對應_不含env`；`test_http.py::ResolveHttpTest::test_resolve端到端_錯誤不記稽核` |
| 客戶端 resolve 形狀 | `test_client.py::ResolveTest`（ok、省略參數、`+`、斜線、exclude 合併、釘住、錯誤對照、cooling 的 `until`、400 的 `missing`） |
| resolve 端到端（真 service） | `test_http.py::ResolveHttpTest`（預設與釘住、錯誤 precedence、不記稽核、log 無憑證值、空白值與重複鍵） |

## 跟設計有出入或不確定處

1. **endpoint 檢查的例外型別**：spec 要 `PolicyError`，design／tasks 要 `providers.validate_endpoints()` 丟固定字串 `ValueError`。現在 providers 仍丟 `ValueError`（不 import policy），由 `Gateway.__init__` 轉成 `PolicyError` 後重丟，兩份文字都成立（pstack 第 1 輪修正）。
2. **`GET /agents` status 的憑證 canary 不在本 repo**：那是 MyLinuxPool 的 c 單（sidecar）；本 repo 只驗 Gateway 的 log／DB／稽核／list。spec 的 Scenario 已把消費端義務切給 sidecar 規格。
3. **「舊 lgwd（v5 tag）讀新 DB 不受影響」**：以 v5 的欄位清單 `SELECT seq, ts, subscription, action, agent, until` 對遷移後的 DB 驗證（`AuditMigrationTest`），沒有真的 checkout／執行 v5 tag（這個 worktree 不連遠端）。
4. **`env_schema` 的消費端拒收**：sidecar 實作在 MyLinuxPool；本 repo 只直接斷言 Gateway 回覆的 `schema`／`env_schema` 值（原替身空測已在 pstack 第 1 輪刪除）。
5. **`lgw.client.Result` 的錯誤 data**：`resolve` 的非 2xx 回覆保留 `error` 以外的欄位（`until`、`missing`），`get` 的錯誤回覆照舊不進 `data`；`repr` 一律不印 data。
6. **`validate_endpoints()` 對 `@` 的檢查較寬**：design 說的是 userinfo `@`，實作只要 endpoint 含 `@` 就拒絕（fail-closed；避免漏掉 `user:pass@` 的變形）。
7. **`no_candidate` 先於 `cooling`**：實作照 spec 步驟（exclude 在冷卻之前），並補了「被排除的那把冷卻中」的測試確認不回 `cooling`。

## 測試結果

指令與結果：

```
cd /Users/fatesaikou/testAI/LLMGateway-worktrees/impl-41
python3 -m unittest discover -s tests
Ran 646 tests
OK (skipped=1)
```

- `main` 基準 578 個全過；前作者 checkpoint 639 個（1 error），修正 helper 後 639 全綠，再補 6 個規則測試成 645；pstack 修正（第 1 輪）後 646（+2 −1，見下一節）。
- `skipped=1` 是既有的 root-only 測試（非 root 身分略過），與本單無關。
- `openspec validate agent-provider-model-interface --strict`：`Change 'agent-provider-model-interface' is valid`（未改設計／規格檔）。
- 沒有新增依賴；只用標準函式庫。

## commit 清單

- `aa6ce5c` `wip(#41): resolve 實作 checkpoint（作者 GLM-5.3-Flash Free 限流中斷；639 測試 1 error，待修）`（前作者）
- `508abfa` `test(#41): 修 EnvSchemaContractTest helper、補 resolve 規則測試（645 全綠）`
- `b2caa20` `docs(#41): 收尾報告（Scenario→測試對照、測試結果、與設計的出入）`
- pstack 修正（第 1 輪）：`c4201e5`、`a3895e4`、`993cf6d`、`81b7ec8`、`27225aa`、`e3015d3`、`92c00e4`、`a5d3818`（依序為發現 1～8）
- 本節與上述修正的報告更新：`docs(#41): 報告 pstack 審查修正（第 1 輪）`（分支 HEAD；hash 見交付回覆）

## pstack 審查修正（第 1 輪）

DeepSeek V4.1 Flash

審查報告：`docs/research/pstack-review-41-sonnet.md`（未 commit，僅供對照）。依 PM 核實的 8 項修，一項一 commit；第 9、10 項不修。

1. **發現 1（client 讀不到 cooling 的 `until`）**：`lgw/client.py` 的 `_send`／`_finish` 加 `error_data` 旗標，只有 `resolve` 傳 True；非 2xx 時把 body 裡 `error`／`env` 以外的欄位（`until`、`missing`）放進 `Result.data`，`get`／`list`／`cooldown` 照舊空 dict。`Result.__repr__` 不變（不印 data）；檔頭 docstring 改掉「錯誤一律不進 `Result.data`」。補 `test_resolve_400帶missing`，`test_resolve_cooling帶until` 改為斷言 `data["until"]`，`test_get_409_not_deployed` 加一條 data 為空的守門。
2. **發現 2（deployed／get 兩次讀的 TOCTOU）**：`lgwd/registry.py` 的 resolve 步驟 6 改成每把只呼叫一次 `self._keys.get(name)`（None＝未部署），把 key 與候選一起往後傳；步驟 7、8 不再碰 KeyStore，選中那把直接用同一份 key。None 的處理仍在步驟 6（409 `not_deployed`），precedence 不變；刪掉「deployed 檢查已保證 key 非 null」的註解。補 `test_resolve每把key只讀一次`：替身 `deployed()` 被呼叫後 `get()` 就回 None，resolve 仍回真 key，且 `get` 只讀 1 次、`deployed` 0 次。
3. **發現 3（推導規則兩份）**：`_derivable` 刪除，`_derive_model(sub, kind) -> (ok, model)` 成為唯一一份規則；`_pick` 步驟 4 對每個候選算一次並保留 `(sub, provider_info, model)`，步驟 8 直接用選中那組；不可達的 `raise no_model()  # pragma: no cover` 與 pragma 刪除。
4. **發現 4（`EnvSchemaContractTest` 空測）**：刪掉自帶 `_sidecar_double` 的類別；`schema`／`env_schema` 的直接斷言已併入 `ResolveTest::test_同一把的兩個免費模型`（`assertEqual(..., 1)`），不重複；sidecar 的拒收行為不在本單測。
5. **發現 5（例外型別與 main 的 except 過寬）**：`Gateway.__init__` 在 `load_policy` 後 `try: validate_endpoints() except ValueError as exc: raise PolicyError(str(exc)) from None`；`main()` 的 except 元組還原成 `(PolicyError, KeyFileError, OSError)`，不再接所有 `ValueError`；`providers.validate_endpoints()` 仍丟固定字串 `ValueError`、不 import policy。`test_main.ProviderEndpointGateTest` 改 `assertRaises(PolicyError)`。
6. **發現 6（resolve 過長）**：新增 `RegistryService._pick(...)`：形狀驗證＋步驟 1～5（釘住／provider／agent／model 涵蓋與推導／exclude），回 `[(sub, provider_info, model)]`，每個候選的 `provider_for` 只呼叫一次；`resolve` 只剩步驟 6～8（key、冷卻、挑選）、組 env／meta 與稽核。行為與 precedence 不變，既有測試原封不動全過；沒有加薄包裝。
7. **發現 7（孤兒註解與簡繁）**：刪除 `_has_wildcard` 上方屬於舊常數的註解；resolve docstring 與步驟 5 註解的「没有」改成「沒有」（隨第 6 項的段落改寫一起落地）。
8. **發現 8（get 與 resolve 兩套 query 規則）**：`lgwd/http.py` 的 get 分支加一行註解：過渡期、刻意不動，d 單移除 get 前不要順手統一。行為不動。
9. **第 9 項不修（model 值沒有字元限制）**：spec 沒要求字元限制；維持現狀，列為給設計的 Consider（若要加是設計變更，不在本修正範圍）。
10. **第 10 項不修（endpoint 子字串比對）**：與 spec 的樣式表一致（`key=`／`token=`／`api_key=`／`access_token=`），canary 是第二道；不擴大比對以免偏離設計。

全測試（修正後，最後跑一次）：

```
cd /Users/fatesaikou/testAI/LLMGateway-worktrees/impl-41
python3 -m unittest discover -s tests
Ran 646 tests
OK (skipped=1)
```

645 → 646：第 1 項 +1（`missing`）、第 2 項 +1（單次讀 key）、第 4 項 −1（刪空測）；其餘為既有測試改斷言／重構，不減覆蓋。
