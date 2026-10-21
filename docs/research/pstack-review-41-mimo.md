MiMo（opencode/mimo-v2.6-flash-free）

# pstack 對抗審查第 2 輪：LLMGateway#41（review/pstack-41）

範圍：`git diff origin/main...HEAD` 的 lgwd／lgw／tests／docs，對照 spec（subscription-registry）、design 決定 1～7、tasks 1.0～1.6。未採信作者報告；先讀過 `docs/research/pstack-review-41-sonnet.md` 的 1～10 號發現，其中 1～8 已在 `92c00e4`～`63fd291` 修正（逐一驗證過：error_data 帶 until／missing 且有 test_client:234；key 只讀一次且 test_registry 斷言 deployed_calls==0；_pick 抽出、resolve 單測直測；Gateway.__init__ 轉 PolicyError 而 main:289-293 的 except 還原成 `(PolicyError, KeyFileError, OSError)`；EnvSchemaContractTest 已刪、env_schema==1 由 test_registry:540 直接斷言），9～10 為刻意不修。本輪不重提這 10 項，只列新發現。

實測：`python3 -m unittest tests.test_registry`（75）、`tests.test_http tests.test_client tests.test_policy`（78）、`tests.test_main`（29）全綠，共 182。

## 結論先講

對抗性角度逐條比對 spec 的選把 8 步、錯誤 precedence、錯誤碼表、env 組成、稽核欄位、no-store 標頭、migration 與 tasks 1.0～1.6，**沒有找到 critical／warning 級的行為錯誤**：所有 spec Scenario 都有對應測試；`_pick`（步驟 0～5）與 key／冷卻（6～7）的順序、釘住語意（404 先於 400 不一致、不代換、no_model 不代換）、exclude 優先於 cooling、not_deployed 先於 cooling、model 守門先於 exclude 等 precedence 全部與 spec 一致；`_cooldown` 的 `> now` 判定、earliest 非 None 的不變量、audit 在 txn 內與選擇原子化、KeyStore 每把只讀一次等邊角也驗過。以下是文件與測試品質層級的發現。

## Findings

### 1. [nit] `Client` 類別 docstring 還寫「打三個介面」，模組層已改成四個呼叫
**Location**: lgw/client.py:60（類別 docstring）；lgw/client.py:1-8（模組 docstring）；lgw/__init__.py:7-10
**Finding**: 本 change 新增 `Client.resolve` 後，模組 docstring 與 lgw/__init__.py 都改成「四個呼叫」，但 `Client` 類別 docstring 仍是「只用 urllib 打三個介面」。另外 lgw/__init__.py:10 的「規格：openspec/changes/gateway-v0/」仍只引 gateway-v0，而 lgw.client 現在也實作 `agent-provider-model-interface` 的 resolve。都在本 change 觸碰過的檔案裡，屬漏改的文件漂移。
**Suggestion**: 類別 docstring 同步為四個介面；規格行補上 agent-provider-model-interface。

### 2. [nit] `test_缺必填` 的「空字串也是沒給」子案例實際重複傳 None，且該句在 service 層本來就不成立
**Location**: tests/test_registry.py:603-605；lgwd/registry.py:167-168
**Finding**: 註解宣稱測空字串，呼叫卻是 `self.resolve(None, "opencode-zen")`——與上方 missing-agent 案例完全重複，service 層的空字串分支（`if not value`）從未被測到。而且這句話在 service 層是錯的：`agent=""`／`provider=""` 會變 missing（400），但 `model=""` 被當成有給（zen 的 allow 不涵蓋空字串 → 403 model_not_allowed）、`subscription=""` 會走釘住分支 → 404 not_found。「空值視為沒給」只在 HTTP 層（`_resolve_query` 丟空白值）成立，service 層對 model／subscription 並未歸一化。
**Suggestion**: 要嘛把子案例改成真的傳 `""` 驗 agent／provider 的 400 missing，要嘛刪掉重複區塊並把註解改為「空值歸一化在 HTTP 層（test_http:624-632），service 層只對 agent／provider 做 falsy 檢查」。

### 3. [nit] `store.txn` 的 docstring 承諾「ApiError 都 commit」，resolve 現在是第一個在 txn 內丟 ApiError 的呼叫點，程式實際是 rollback
**Location**: lgwd/store.py:69-70（txn docstring，未被本 diff 改動）；lgwd/registry.py:146-147（txn 內 `raise cooling(...)`）
**Finding**: 本 change 前，所有 ApiError 都在 `with txn()` 之外丟（get 的 not_deployed 在 txn 前、register_cooldown 的驗證也在 txn 前），docstring 與行為恰好不衝突。resolve 的步驟 7 首次在 txn 區塊內丟 `cooling`，走的是 `except Exception: rollback`——今天無害（raise 前沒有任何寫入，rollback 的是空交易），但 docstring 現在與可達路徑的真實行為相反；之後若有人在 raise 前加了稽核或冷卻寫入，會照 docstring 的預期以為會 commit。
**Suggestion**: 要嘛把 docstring 改為「成功 commit；任何例外（含 ApiError）rollback」，要嘛把 cooling 的 raise 移出 txn（讀完 cooldowns 先算出 earliest，出 txn 再丟）。

### 4. [nit] `_has_wildcard` 的 docstring 與實作不符（「開頭字元的 [」vs 任何位置）
**Location**: lgwd/registry.py:54-56
**Finding**: 實作是 `any(ch in pattern for ch in ("*", "?", "["))`——任何位置的 `[` 都算；fnmatch.translate 在任何位置的 `[` 都開字元類，實作是對的，是 docstring 錯。這是決定 `_derive_model` 用不用 models.default 的判準（樣式含萬用字元＝不可推導），文件誤導會直接誤導改政策檔的人。
**Suggestion**: docstring 改為「樣式含 fnmatch 萬用字元（*、?、[）即不可推導」。

### 5. [nit] docs/deploy.md 仍寫「服務只做三件事」，resolve 部署當天起文件即過時
**Location**: docs/deploy.md:17
**Finding**: `## 部署` 開頭寫「服務只做三件事：list、get、登錄冷卻」。tasks 只把「介面文件同步」排在 4.1（get 退場的 d 單），所以 3.0 部署 resolve 之後、d 單完成之前，正式文件與實際介面（四個呼叫）會在整個過渡期不一致。這是照 tasks 排程走的結果，不是實作錯誤，但過渡期正是 sidecar 指著文件接 resolve 的時候。
**Suggestion**: 要嘛在 1.x 補一行 deploy.md 的過渡期註記（list／resolve／get／cooldown），要嘛把 4.1 的文件同步拆一半提前到 3.0。

## 未列為 finding 的觀察（確認過不是問題）

- `provider_for(sub.name)` 對政策鍵集的 KeyError→500 耦合（SUBSCRIPTIONS／PROVIDERS／KEY_NAMES 三張平行表）是 v5 既有模式，list／get 早有，本 change 未惡化；resolve 步驟 2 新增的呼叫點走同一約束。
- service 層 `subscription=""`→404、`model=""`→可能 403 的空值不一致已折進發現 2；HTTP 生產路徑符合 spec「空值視為沒給」。
- `CredentialProvider.describe()` 是 origin/main 上就存在的死碼，本 change 未新增呼叫也未移除，不在審查範圍。
- test_main:436 `test_出廠provider表組裝成功` 只呼叫 `validate_endpoints()` 不組裝 Gateway，但其自身註解已誠實標明「這裡只作正面的最小確認」，且正面路徑被其餘 28 個建 Gateway 的測試間接覆蓋，不列。
- impl-41-report.md 含 `/Users/fatesaikou/...` 本機路徑：docs/research 下既有檔（review-33-r2.md、admin-api-review.md 等）同樣寫法，是既有慣例；無金鑰、無 MyBrain 私人內容。
- 並行性：Store 單連線＋RLock、txn 為 BEGIN IMMEDIATE，resolve 的稽核與冷卻讀取同 txn 原子；KeyStore 每把只讀一次（get 內含 mtime reload）；min 的 tie-break（priority, name）確定性。皆已驗。
- spec 的 `no_model`（400）先於 `no_candidate`（409）、`model_not_allowed`（403）先於 exclude 等 precedence 與 design 決定 3 的軸一致，逐條與 test_registry 的 ResolveTest 對照過。
