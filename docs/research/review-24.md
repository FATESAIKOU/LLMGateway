# 審查 #24：models.default 出廠值與 spec 語意（llmgateway-arch）

審查對象：`git diff main...HEAD`（branch `review/models-default`＝工單 #24，4 個 commit；HEAD `4c533d5`、main `1f32fec`，diff 5 檔 +123／-3 行）。

- `1e23814` policy：出廠預設改 ollama-cloud 與 opencode-zen、Claude 訂閱維持 null（#24）
- `16a904c` spec：subscription-registry 補 models.default 的語意（#24）
- `4b693ef` design：決定 7 補 models.default 的說明（#24）
- `4c533d5` tests：models.default 出廠值、帶出與 spec 兩 Scenario 的測試（#24）

審查者：llmgateway-arch（作者是 llmgateway-impl 與 llmgateway-test，不是審查者；PM 是 LLMGateway PM）。只審查，未改任何程式。未讀任何憑證檔或密碼檔；未 ssh／gh／push／觸發 workflow；實驗輸出在 `.work/`（gitignored，不進本 commit）。動態查證用本機 `opencode v2.0.29`、`models.dev/api.json`（2026-10-16 抓）與官方文件，皆非憑證。

## 結論

**可以開 PR。** 高 0、中 0、低 1、資訊 3；低與資訊皆不擋（低-1 若 PM 想在本單定案，只需改 spec／design 措辭，值不動）。

六個指定重點逐條有證據：政策檔只有兩個 `default` 葉子變、值與本人裁定一致、三個非 null 值都被同一筆 `allow` 涵蓋（fnmatchcase 實測）；opencode-zen 寫完整 id 的判斷**正確**（opencode 的 `--model` 就是 `provider/model`，Zen 的 provider id 是 `opencode`，該模型在 `opencode models` 逐字存在）；spec 新 Requirement 與兩 Scenario 清楚、和 design 決定 7 一致、`openspec validate --strict` 過；測試逐值逐語意釘住（含 Claude 兩筆 JSON `null`）；README／deploy.md／lgw 客戶端 docstring 查證都不用跟改（唯一落後的是 design 的示意 JSON，前單已知，資訊-1）；全量 261 tests OK。

## 測試

`python3 -m unittest discover -s tests -t .`（exit 0；140.8s）

```
Ran 261 tests in 140.832s

OK
```

（main 為 #21 審查時的 257 tests；本單淨增 4 條＝`test_出廠五筆訂閱的models_default`、`test_get與list照出廠值帶出models_default`、`test_Claude訂閱沒有預設`、`test_其他kind沒有預設`，257＋4＝261 對得上。log 中的 `keys reload failed` 是既有測試的預期輸出。）

## 逐項核對

### 1. 政策檔：五筆的值、其他欄位、allow 涵蓋 ✅

`policy/default.json` 與 main 逐鍵 JSON 比對（值用 Python 走訪，不只 grep）：

```
CHANGED subscriptions/opencode-zen/models/default old= None new= opencode/muse-spark-1.3-contributor-free
CHANGED subscriptions/ollama-cloud/models/default old= None new= deepseek-v4.1-flash
（其餘全部 same：claude-personal／claude-company 的 priority／agents／allow／default、
 openrouter 四欄、opencode-zen 的 priority／agents／allow、ollama-cloud 的 priority／agents／allow；
 頂層 version、until_max_ahead_seconds 相同）
```

- 五筆值：`claude-personal`＝null、`claude-company`＝null（`:7`、`:12`）；`opencode-zen`＝`"opencode/muse-spark-1.3-contributor-free"`（`:17`）；`openrouter`＝`"openrouter/free"`（`:22`，本單未動）；`ollama-cloud`＝`"deepseek-v4.1-flash"`（`:27`）。與本人裁定逐字一致。
- allow 涵蓋（真 loader ＋ `fnmatch.fnmatchcase`）：`openrouter/free` 被同一筆的 literal `openrouter/free` 蓋（`*:free` 蓋不中，作者理由正確）；`opencode/muse-spark-1.3-contributor-free` 與 `deepseek-v4.1-flash` 都被 `"*"` 蓋（fnmatch 的 `*` 含 `/`），所以 zen／ollama 的 allow 本來就不用動。

### 2. opencode-zen 寫成完整 id 的判斷：正確 ✅（另見低-1）

- opencode 的模型參數是 `provider/model`：CLI 文件（<https://opencode.ai/docs/cli>，2026-10-15 更新）TUI 與 `run` 的 `--model`／`-m` 都寫「Model to use in the form of provider/model」；`opencode models` 的輸出格式也是 `provider/model`。Models 文件（<https://opencode.ai/docs/models>）：「Here the full ID is `provider_id/model_id`. For example, if you're using OpenCode Zen, you would use `opencode/gpt-5.1-codex`」。
- Zen 的 model id 是 `muse-spark-1.3-contributor-free`：Zen／Console 頁（<https://opencode.ai/docs/zen>）模型表有「Muse Spark 1.3 Contributor Free｜`muse-spark-1.3-contributor-free`」且是免費列；同頁寫「To select a model in OpenCode, use the format `opencode/<model-id>`」。models.dev `api.json`（今日抓）的 provider `opencode` 有 model key `muse-spark-1.3-contributor-free`（cost input／output／cache_read 皆 0）。
- 動態實測（本機 `opencode models`，逐行取樣存 `.work/opencode-models.txt`）：

  ```
  134:opencode/muse-spark-1.3-contributor-free
  400:openrouter/openrouter/free
  51:ollama-cloud/deepseek-v4.1-flash
  ```

  即 sidecar 把 `models.default` 直接當 `--model` 傳時，`opencode/muse-spark-1.3-contributor-free` 逐字就是 opencode 認得的全 id；只寫 `muse-spark-1.3-contributor-free` 會少 provider（作者的 commit 理由成立）。repo 內既有同結論的調查：`docs/research/agent-endpoints.md:137`、`docs/research/upstream-claude-code-compat.md:137`。
- 對照 `openrouter/free`：它是 OpenRouter 端的 model id（Free Models Router，`docs/research/review-21.md` 已查證），不是 opencode 的全 id——opencode 的全 id 是 `openrouter/openrouter/free`（`opencode models` 第 400 行；`opencode models | grep '^openrouter/free$'` 為 0 筆）。這個不對稱見低-1。

### 3. spec 新 Requirement／兩 Scenario 與 design 決定 7 ✅

- `spec.md:35-44`：Requirement 先把值定義成「agent 直接拿去用的完整模型 id」、允許 null，再依 `kind` 分兩種 null 語意；兩個 Scenario 分別是 Claude 訂閱（`:38-40`）與其他 kind（`:42-44`），都寫成 WHEN／THEN、可測，且與 `design.md:172` 的新句一一對應（Claude＝agent 自己的預設、呼叫方不帶模型參數；其他＝服務不代選、呼叫方依 allow 選或不用）。與 design 決定 7 的共通 schema 說明一致。
- `openspec validate gateway-v0 --strict` → `Change 'gateway-v0' is valid`，exit 0。
- 唯一措辭問題是「不是只寫 provider 之後的部分」與三筆值的對齊，見低-1。

### 4. 測試釘住值與語意 ✅（兩點註記）

- `tests/test_policy.py:69-92`（`test_出廠五筆訂閱的models_default`）：讀**真出廠檔**，五筆逐值 `assertEqual`，非 null 者再用 `fnmatchcase` 確認被同一筆 allow 涵蓋（正是本單要的不變式）；`:3` 新增 `import fnmatch`。
- `tests/test_registry.py:132-140`（`test_models欄位`）：把原本 `opencode-zen.default is None` 的期望更新成新值，openrouter 的 allow／default 續釘。
- `:142-172`（`test_get與list照出廠值帶出models_default`）：list 與 get 兩介面、五筆逐值；每個 payload 都 `assertIn("default", …)`（不缺欄），Claude 兩筆另驗 JSON 往返仍是 `null` 且序列化字串含 `"default": null`（不是空字串）——**spec／ticket 指到的「list 裡 Claude 兩筆是 JSON null」有釘到**。
- `:175-219`（`ModelsDefaultSemanticsTest`）：兩個 Scenario 各一測；非 Claude 的 null 用「真出廠檔複本＋`ollama-cloud` 改 null」的假政策檔驗（不動出廠檔、finally close），並確認 allow 非空（呼叫方有東西可選）。
- 兩點註記：loader 不檢查 `default ∈ allow`，此不變式目前只有測試守（資訊-2）；`:163` 的 `source` 變數未使用（資訊-3）。

### 5. 其他文件與程式碼是否需要跟改 ✅（一處可選，資訊-1）

- `README.md`：只有三行簡介，無模型值 → 不用改。
- `docs/deploy.md`：全檔與 models／default 相關的只有 `:462`（政策檔跟著 tag 換）、`:577`、`:584`（id 與 403 說明），沒有訂閱表或模型值 → 不用改。
- `lgw/client.py` docstring（`:1-20`）：只列三個介面與回傳形狀，沒有模型值 → 不用改。`lgw/` 全目錄 grep 無模型字面值（review-21 亦已確認）。
- `lgwd/registry.py:134-136`：`_item` 泛用帶出 policy 值，不用改；`lgwd/policy.py:81-83` 的 `default` 驗證（非空字串或 null）已足夠承載 `opencode/…` 這種含 `/`、`.` 的值（真出廠檔載入即驗證）。
- 唯一落後的是 `openspec/changes/gateway-v0/design.md:160` 的示意 JSON（前單已知），見資訊-1。

### 6. 全量測試 ✅

見上「測試」節：261 tests、140.8s、OK；exit 0。

## 發現

### 低-1　「完整模型 id」的定義與三筆值、agent 命名空間不完全對齊

- **檔案:行號**：`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:36`（配 `openspec/changes/gateway-v0/design.md:172`、`policy/default.json:17,22,27`）
- **問題**：spec 句「`models.default` SHALL 是 agent 直接拿去用的完整模型 id（不是只寫 provider 之後的部分）」對 opencode 這個 agent（`--model` 需 `provider/model`）逐字只成立於 opencode-zen 一筆；`openrouter/free` 是 OpenRouter 端的 model id（opencode 全 id 為 `openrouter/openrouter/free`），`deepseek-v4.1-flash` 沒有 provider 前綴（opencode 全 id 為 `ollama-cloud/deepseek-v4.1-flash`）。兩筆都能被 claude-code 那條路直接使用，但「sidecar 直接當 --model 傳」對 opencode＋openrouter／openrouter＋ollama-cloud 不成立。
- **證據**：`opencode models` 輸出（本文件 §2：`:400 openrouter/openrouter/free`、`:51 ollama-cloud/deepseek-v4.1-flash`、`:134 opencode/muse-spark-1.3-contributor-free`）；opencode 文件「full ID is `provider_id/model_id`」「form of provider/model」；models.dev 的 provider／model 分層（openrouter 的 model id 本身就是 `openrouter/free`）。
- **建議**：值不動（#21 與本單的值都是本人裁定）。可選（a）把 spec／design 的「完整模型 id」寫成 per-agent 的定義（opencode 傳 `provider/model`，Zen 的值已含 `opencode/`；其他 provider 的值由 sidecar 的 AgentDriver 依 provider 組前綴或供 claude-code 用），或（b）文字不動，把 openrouter／ollama-cloud 配 opencode 的模型字串留給 sidecar 整合驗證（`tasks.md` 4.3 已有「opencode 對 Zen、OpenRouter（免費模型）、Ollama 各跑通一次」）。不擋 PR。

### 資訊-1　design.md 的示意 JSON 仍落後（含本單前的既有落差）

- **檔案:行號**：`openspec/changes/gateway-v0/design.md:160`（同區塊 `:159` priority 30、`:163` cooldown 5h／1w）
- **問題**：範例寫 `"models": {"allow": ["*:free"], "default": null}`，與出廠的 `["*:free","openrouter/free"]`／`"openrouter/free"` 不同；priority 與 cooldown 形狀也分別是舊值／v4 形狀。本單在 `:172` 補了新說明，使這個示意例更容易被當成現況。
- **證據**：`policy/default.json:19-23`（現值）、本單 diff 未動 `:152-166` 的區塊（review-21.md 的資訊-1 已提過同一處）。
- **建議**：可選——順手把範例更新成與出廠檔一致（或標成歷史示意）。不擋。

### 資訊-2　`default ∈ allow` 目前只有測試守門，loader 不檢查

- **檔案:行號**：`lgwd/policy.py:81-83`（收任何非空字串／null）配 `tests/test_policy.py:88-92`（fnmatchcase 檢查）
- **問題**：若有人日後手改政策檔、把 default 改成 allow 蓋不中的值，`lgwd` 照樣啟動（只有 openrouter 的 allow 是精確白名單、失敗即拒），服務不會代選所以不會立刻壞，但 sidecar 挑到這把時會沒有可用的 default──本單的不變式只在出廠檔的測試裡。
- **證據**：`lgwd/policy.py:123-124` 只對 openrouter allow 做等值檢查；`registry.py` 只搬值不驗涵蓋。
- **建議**：可開 d 單在載入時加「非 null default 必須被同筆 allow 樣式涵蓋」的 fail-closed 檢查（與 openrouter 白名單同精神）。不擋。

### 資訊-3　測試迴圈變數 `source` 未使用

- **檔案:行號**：`tests/test_registry.py:163`
- **問題**：`for source, payload in (("list", …), ("get", …))` 的 `source` 沒用到（無害）。
- **證據**：同檔 `:161-172`。
- **建議**：可選——拿掉或改成在失敗訊息裡用。不擋。

## 補充觀察（非發現）

- fnmatch 的 `*` 會匹配 `/`，所以 `allow: ["*"]` 天然涵蓋含 provider 前綴的 id；這也是本單 zen／ollama 只需改 `default`、`allow` 不必跟改的原因（與 openrouter 必須另列 literal 的設計差異一致）。
- 動態查證用的 opencode 是本機 v2.0.29；`design.md:274` Open Questions 仍寫「opencode 1.18.35」（2026-10-11 的版本註記）——屬 sidecar 實作時要再確認的範圍，本單不用動。
- 四個新測試逐一對應 spec Scenario 或出廠值，命名與 repo 慣例一致；測試全用假 key（canary）、FakeClock、暫存目錄，無外部相依。

## 複核（Step 5 Preview）

複核對象：第二輪 `4183a7d..HEAD`（同 branch `review/models-default`＝工單 #24 依上報告案做的修訂，5 個 commit，5 檔 +24／-11 行）：

- `6468372` policy：opencode-zen 的 models.default 改供應商自己的模型 id（#24）
- `a8345b3` spec 與 design：models.default 改供應商自己的模型 id（#24）
- `0d81409` design：決定 7 示意 JSON 對齊出廠政策與 v5 冷卻形狀（#24）
- `ea1fcea` test：未使用的迴圈變數 source 放進斷言訊息（#24）
- `bbcd9d1` test：出廠 zen 值改新 id、default 不帶 agent 前綴（#24）

複核者：llmgateway-arch（非作者複核；上輪與本輪同為 llmgateway-arch，但這輪模型是 Step 5 Preview Free；作者是 llmgateway-impl／llmgateway-test，模型 DeepSeek V4.1 Flash 與 Muse Spark 1.3，都不是複核者；PM 是 LLMGateway PM）。只審查，未改任何程式。未讀任何憑證檔或密碼檔；未 ssh／gh／push／觸發 workflow；實驗輸出在 `.work/`（gitignored，不進 commit）。未遇 rate limit。

### 複核結論

**可以開 PR。** 高 0、中 0、低 1、資訊 3（其中上輪 資訊-2 仍 open）；低與資訊皆不擋。

### 逐項核對（4 點）

**1. 低-1、資訊-1、資訊-3 照定案修好 ✅**

- **低-1（已修）**：定案是「`models.default` 與 `allow` 一律寫供應商自己的模型 id、前綴由 sidecar 的 OpencodeDriver 自己加」。實作與文字一致：`policy/default.json:17` zen 的 default 由 `"opencode/muse-spark-1.3-contributor-free"` 改成 `"muse-spark-1.3-contributor-free"`；`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:36` 整句改寫成「供應商自己的模型 id（provider 端 API 認得的名稱）……MUST NOT 帶 agent 專用的 provider 前綴……組 agent 字串由呼叫方的 agent driver 負責」；`design.md:172` 換成同句（例 `openrouter/free`、`muse-spark-1.3-contributor-free`、`deepseek-v4.1-flash`）。repo 內前後的舊說法「agent 直接拿去用的完整模型 id」已全數消失（grep 只剩本檔上輪紀錄），spec 與 design 兩邊用語逐字對齊。
- **資訊-1（已修）**：`design.md:152-165` 決定 7 的示意 JSON 對齊出廠政策——`priority` 30→20、`models` 改 `["*:free", "openrouter/free"]`／`"openrouter/free"`、`cooldown` 由 v4 的 `{"5h":…,"1w":…}` 改 v5 的單一 `{"until": …}`；與 `policy/default.json:19-23` 逐欄一致。前輪資訊-1 結案。
- **資訊-3（已修）**：`tests/test_registry.py:165-168` 把 `source` 放進 `assertIn` 與 `assertEqual` 的失敗訊息（list／get 可分辨），變數不再閒置，斷言行為未變（diff 只碰訊息參數）。

**2. spec 與 design 的新說法一致、Scenario 清楚 ✅**

- 兩邊同句：spec `:36` 與 design `:172` 只差「MUST NOT」的規範化措辭與括號例，語意相同（供應商自己的 id、不帶 agent 前綴、由呼叫方 agent driver 組成）。
- Scenario 三個都寫成 WHEN／THEN、可測：**供應商自己的模型 id**（`spec.md:38-40`，讀 `opencode-zen`，明講「不帶 `opencode/` 前綴」＋「`opencode/<模型>` 由呼叫方的 agent driver 自己組成」）、**Claude 訂閱沒有預設**（`:42-44`）、**其他 kind 沒有預設**（`:46-48`）。null 的三段語意與 design 一致。Scenario 的「讀取」沿用本 spec 既有寫法（如 `:61` `Claude 訂閱的描述` 的「讀取」），list 與 get 兩介面都涵蓋，語意清楚。

**3. 測試釘住新值＋「default 不帶 agent 前綴」斷言 ✅**

- 新值：`tests/test_policy.py:78`、`tests/test_registry.py:139` 與 `:148` 三處舊期望值同步成 `"muse-spark-1.3-contributor-free"`（repo 內 grep 已無舊全 id 殘留）。
- 不帶前綴：`tests/test_policy.py:96-99` 新增 `assertFalse(default.startswith("opencode/"))`，且放在 `if default is not None:` 之內（`:87`）——Claude 兩筆 null 不會對 `None` 呼叫而誤爆；把新 spec 的 MUST NOT 變成可執行守門。政策層五筆逐值＋allow 涵蓋＋前綴三項都在同一個 subTest 迴圈裡。
- 帶出層不變：`tests/test_registry.py:142-172` 仍逐筆跑 list 與 get（含 JSON 往返的 `null` 檢查），service 側只是搬值，無需新斷言。
- 動態佐證（本機 opencode v2.0.29，見 §2 的 `.work/opencode-models.txt`）：三個供應商 id 經 driver 加前綴後都真實存在——`opencode/muse-spark-1.3-contributor-free`、`openrouter/openrouter/free`、`ollama-cloud/deepseek-v4.1-flash`。即新定案對三筆都成立，上輪低-1 的命名空間不對稱已消除。

**4. 有無帶進新問題：無阻擋項，新增低-2（用詞）、複核資訊-1（spec 釘資料值）、複核資訊-2（窄代理斷言）、複核資訊-3（Scenario 無同名測試）；上輪 資訊-2 仍 open。**（見「複核發現」）

### 測試與規格校驗（複核這輪）

`python3 -m unittest discover -s tests -t .`（exit 0）：

```
Ran 261 tests in 135.953s

OK
```

`openspec validate gateway-v0 --strict`：

```
Change 'gateway-v0' is valid
exit=0
```

261＝main 257＋第一輪 4 條新測試；第二輪只改期望值與失敗訊息，測試數不變。

### 複核發現

#### 低-2（複核新增，不擋）design 的 models 說法少了一個例子條件

- **檔案:行號**：`openspec/changes/gateway-v0/design.md:172`（spec `:36` 無此問題）
- **問題**：design 說「例 `openrouter/free`、`muse-spark-1.3-contributor-free`、`deepseek-v4.1-flash`」——三個都對；但句子沒點明「這些例子的 `/` 是供應商自己的命名（OpenRouter 的 author/model），不是 agent 前綴」。讀者看到 `openrouter/free` 裡的 `openrouter/` 與「不帶 agent 專用的 provider 前綴」並排，仍可能誤解；spec 用「（例如 opencode 的 `opencode/`）」把例子範圍收了，design 沒有對應括號。
- **證據**：`models.dev/api.json`（今日抓）的 openrouter provider 的 model id 就是 `openrouter/free`；`opencode models` 的輸出第 400 行是 `openrouter/openrouter/free`——前段 `openrouter/` 既是 opencode 的 provider id 也是 OpenRouter 自己的 author namespace，兩種讀法都可能。
- **建議**：design `:172` 比照 spec 補括號明講例外（「OpenRouter 的 `openrouter/` 是供應商自己的命名，不算 agent 前綴」）。不擋 PR。

#### 複核資訊-1（不擋）spec 的 Scenario THEN 釘了出廠資料值

- **檔案:行號**：`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:38-40`
- **問題**：新 Scenario 的 THEN 把出廠值 `muse-spark-1.3-contributor-free` 寫死；本 spec 其餘條文只談形狀與語意（上輪審 `#21` 時也以「spec 不釘值」為由判定不必改）。日後出廠政策換掉這筆 default（例如換免費模型）就要記得到回來說法，否則 spec 會落後。
- **證據**：`spec.md:36` 的例句也列出三個 id，同屬資料值。
- **建議**：可選——Scenario 改成不點名的寫法（「`models.default` 不帶 `opencode/` 前綴」），或維持現狀但在該條上方註「例為出廠值」。不擋。

#### 複核資訊-2（不擋）「不帶 agent 前綴」的斷言是窄代理

- **檔案:行號**：`tests/test_policy.py:96-99`
- **問題**：`assertFalse(startswith("opencode/"))` 只擋 opencode 的 provider id 這一種。對 claude-code 沒有對應前綴可擋（它本來就不帶前綴），而 openrouter 的 `openrouter/` 雖「長得像」opencode 的前綴、卻是供應商自己的命名，不能無條件檔——現寫法不會誤判（`openrouter/free` 不以 `opencode/` 開頭），但理解上要知道這是針對 zen 的窄檢查。
- **證據**：policy 檔五筆只有 zen 曾被寫成帶 `opencode/`；`opencode models` 的清單顯示 `openrouter/openrouter/free`、`ollama-cloud/deepseek-v4.1-flash` 的構成。
- **建議**：可選——把測試註解寫明「只看 `opencode/`，其他前綴是供應商自己的命名，不在此擋」（註解已提 opencode/，可再加一句）。不擋。

#### 複核資訊-3（不擋）新 Scenario 沒有同名的測試方法

- **檔案:行號**：`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:38-40`（測試見 `tests/test_policy.py:69-99` 與 `tests/test_registry.py:142-172`）
- **問題**：repo 慣例是測試名稱對應 spec Scenario（`tests/test_registry.py:5` 的模組說明），本輪新增的「供應商自己的模型 id」Scenario 沒有同名人稱測試；其斷言被兩個既有測試分散覆蓋（政策層釘值＋不帶前綴、registry 層 list／get 帶出）。
- **證據**：`grep 'def test_' tests/test_registry.py tests/test_policy.py` 無對應名稱。
- **建議**：可選——在 `ModelsDefaultSemanticsTest` 補一個 `test_供應商自己的模型id`（service 讀 `opencode-zen` 的 default 不等於 `opencode/…`），語意不變。不擋。

#### 資訊-2（上輪，這輪仍未處理，不擋）`default ∈ allow` 只有測試守門

- **檔案:行號**：`lgwd/policy.py:81-83`（配 `tests/test_policy.py:87-92`）
- **問題**：loader 仍不檢查非 null default 是否被同筆 allow 樣式涵蓋，此不變式只有出廠檔測試守；第二輪沒有擴到這裡（diff 只動 spec／design／測試，未碰 `lgwd/`，合理）。
- **證據**：第二輪 5 個 commit 的 `--stat` 不含 `lgwd/`；新值 `muse-spark-1.3-contributor-free` 仍被 `["*"]` 涵蓋。
- **建議**：維持原判——可另開 d 單在載入時 fail-closed，或留待有人手改政策時再議。不擋。

### 複核處理（PM 定案的落實檢驗）

把上輪建議的兩個方案與第二輪實作對照：上輪低-1 建議 (a)「把 spec／design 寫成 per-agent 定義（前綴由 driver 加）」——第二輪正是走 (a)，且比建議更徹底：連 `policy` 的值也回歸供應商 id（zen 改 `muse-spark-1.3-contributor-free`），讓 spec「供應商自己的模型 id」這個名詞在三筆值上全部字面成立；claude-code 那條路本來就用供應商 id，不受影響（`openrouter/free`、`deepseek-v4.1-flash` 照舊）。OpencodeDriver 自行加前綴的說法由 `spec.md:36` 明訂，`tasks.md` 4.3 的驗收（opencode 對 Zen／OpenRouter／Ollama 各跑通一次）現在有了明確的組字規則可驗。

### 複核補充觀察（非發現）

- 第二輪沒有動任何程式碼（`lgwd/`、`lgw/`、`deploy/`、`ops-scripts/` 全未碰）——spec 只談資料語意與客戶端責任，介面未變，正確。
- 新 Scenario 的 WHEN「讀取 `opencode-zen` 的共通 JSON」覆蓋 list 與 get 兩條介面（測試亦是）；有 `agents` 守門，opencode 拿得到、claude-code 拿不到（403）與本條文無關，不影響。
- 測試全用假 key／FakeClock、無外部相依，未讀任何憑證；複核動態查證只讀 `opencode models` 與公開的 models.dev／官方文件頁。

### 複核最終結論

**可以開 PR。**（高 0、中 0、低 1、資訊 3；三條資訊含上輪仍 open 的一條；低與資訊皆不擋，可選順手。）
