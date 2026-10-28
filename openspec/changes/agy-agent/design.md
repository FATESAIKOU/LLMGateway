# 設計：`agy`（Antigravity CLI）登錄（#107）

範圍：只寫介面。來源＝#107 的裁定、`RESEARCH-107b.md`、`OUT-research-486.md`（唯讀）。標「推論」的是讀程式或報告得到的判斷；標「作者提議、本人未指示」的不是裁定。

## 1. 介面

### 1.1 agent 詞彙：`AGENTS` 加 `agy`

現況 `lgwd/policy.py:20`：`AGENTS = ("claude-code", "opencode")`。改成三個，`agy` 放最後（順序無語意）。所有 `agent not in AGENTS` 的路徑一起變：

- 政策驗證 `lgwd/policy.py:112`：`agents` 清單只收 `AGENTS` 的值，新 row 的 `["agy"]` 才合法。
- resolve `lgwd/registry.py:407`：`agent=agy` 從 400 變成可進入選把；其他未知值仍 400。
- roles `lgwd/registry.py:158`：`agent=agy` 從 400 變成 200（候選照 `roles.order` 篩，見 §4）。
- 冷卻的模型前綴 `lgwd/registry.py:337`：`model.startswith(agent + "/")` 逐 agent 檢查，`agy/…` 由「可能被 allow 涵蓋」變成 400（**收緊**，spec／測試要一起改）。
- `get` 只檢查該筆的 `agents`（`lgwd/registry.py:277`），沒有 AGENTS 檢查；`?agent=agy` 收不收由 row 決定。
- `lgw/client.py` 不驗 agent（只組 query）；`lgwctl` 的說明寫「可用值見政策檔的 subscriptions[].agents」（`lgwctl/cli.py:58-60`）。兩支都不用改程式，只補 client docstring 與 `lgwd/errors.py` 的碼表說明。推論：把 `agy` 放進全域 `AGENTS` 是唯一不新增第二套詞彙的做法；代價就是冷卻前綴那條一起收緊。

### 1.2 訂閱 id 與 provider（本人選）

| 選項 | 訂閱 id | provider | 說明 |
|---|---|---|---|
| A（建議） | `antigravity` | `google` | 與 Mac sidecar 已在送的 `provider=google` 一致 |
| B | `antigravity` | `antigravity` | 命名更精確、避開未來 Gemini API key 的 `google`；代價是 MLP 文件與呼叫端要改 |
| C | `agy` | `google` | id 與 agent 同名，較短 |

建議 A 的理由：sidecar 送 `provider=google` 被 400 的那次（OUT-research-486 §2.5 問題 1、§3.2），只要 Gateway 登錄 `google` 就通了；且 resolve 的 `meta.provider` 必須等於請求值（MLP 的 `modules/sidecar/src/sidecar/sources.py:396-399`，引在報告），兩邊對名字的認知不一致時 sidecar 會把 200 當 503。同名風險（推論）：日後再加 Gemini API key 的 row（`kind=api-key`、provider 也 `google`）時，`_pick` 會把兩把都列為候選再用 `agents` 篩（`lgwd/registry.py:426-438`）——agy 只准 `agy`、API key 只准 `opencode`，不會串；不想共享命名空間就選 B。
MLP 帶 `provider`＋`model` 時只打一支 `GET /v1/resolve?agent=agy&provider=google&model=<裸 id>`（OUT-research-486 §2.1 的原樣字串），不帶 `subscription`／`exclude`；`harness:false` 是 sidecar 的 body，不經 Gateway。

`endpoint` MUST 為 `null`（agy 自己連 Google，Gateway 不代理）；`validate_endpoints` 對 `None` 直接跳過（`lgwd/providers.py:180-181`）。`PROVIDERS`（`lgwd/providers.py:151-159`）加一筆 `GoogleProvider`：`provider="google"`、`kind="local-login"`、`endpoint=None`、`opencode_provider_id=""`。

### 1.3 `agents` 條款

仿 claude 的條款式檢查（`lgwd/policy.py:257-259` 的 `_CLAUDE_ONLY` 寫法）新增 `_AGY_ONLY = ("antigravity",)`：`agents` 必須剛好 `("agy",)`，否則 `PolicyError("antigravity must only allow agy")`（固定字串、不含值）；反過來，任何其他訂閱的 `agents` 含 `agy` 也拒收（不然 api-key 的 row 會用空 env 被選中）。原樣（`lgwd/policy.py:258-259`）：`if name in parsed_subs and parsed_subs[name].agents != ("claude-code",):`／`raise PolicyError("claude subscriptions allow claude-code only")`。理由：這把沒有憑證，給 claude-code／opencode 拿到只會得到 200＋空 env，比 403 更難查；條款也保證它不混進現有角色的候選。

### 1.4 模型：`models.allow`／`default`

sidecar 的 agy driver **只從 `meta.model` 拿模型**（MLP 的 `modules/sidecar/src/sidecar/drivers/agy.py:440-450`：`resolved.model is None` → `NoModelError`；`--model` 是唯一通道），所以 row 必須**能推導模型**：`models.default` 非 null，或 `allow` 至少一個不含萬用字元的 id（`lgwd/registry.py:90-104`）。模型 id 的來源是 agy 自己的 `agy models`（RESEARCH-107 §① 的旗標清單有 `models` 子命令），不是 Gateway 能查證的清單；本單沒有執行 agy，**不抄任何未查證的 id**（MLP 測試假資料用過 `gemini-3.8-flash-low`，那只是假的，OUT-research-486 §1.2）。

- A（建議）一模型一 row：`allow: ["<agy models 的裸 id>"]`、`default` 同值。理由同 MLP 的 U1 選 A（`agents` 是唯一的閘；舊形狀 `subscription:{"id":…}` 也指得到這一把）。B `allow: ["*"]`＋具體 `default`（像 `claude-personal`）：不用維護，但錯字會被 200 放行，錯誤延到 agy 才爆。C 多模型一 row：`model` 省略時只會挑 default；row 一多，priority 就要排。

`model` 參數只收**裸 id**：Gateway 不正規化前綴（spec「models.default 的語意」），sidecar 的 agy driver 也沒有前綴正規化（`normalise prefixed model -> google/gemini-3.8-flash-low`，OUT-research-486 §1.3）；寫 `google/…` 會 403 `model_not_allowed`。

### 1.5 priority

只有「同 provider＋同 agent」的候選互相比 priority（`lgwd/registry.py:428-434`、`:233-234`）；agy 的 `agents` 只有 `agy`，**不會**搶 zen／go／openrouter／groq／ollama／claude 的候選。建議 35（介於 `ollama-cloud` 30 與 `claude-personal` 40；作者提議、本人未指示）：agy 是個人訂閱、不是共享免費池，list 排在 ollama 之後讀起來符合「本機專用」，同 agent 的多把 row 之間仍有先後。其他：5（最前，但 list 第一眼就是它）、45（最後）。

### 1.6 privacy

選項 `public`（建議）／`general`／`real`。建議 `public`：內容經 Google／Antigravity 處理，個人訂閱用於原版 agy 的合規性仍待本人裁定（brief；RESEARCH-107b §三、§四），方案的資料使用條款本輪沒有逐字讀到（§五）；缺省 `public` 是 fail-closed（`lgwd/policy.py:98`）。
**限度（要告訴 PMO）**：`privacy` 只在 roles 與「resolve 帶 `data`」時生效；不帶 `data` 的 resolve 完全不過濾（`lgwd/registry.py:445-450`、spec「resolve 的選把規則」3.5）。設 `public` 不會自動擋住真資料。

## 2. 「不需 key」的機制

### 2.1 現況四個卡點

`keys.get`／`deployed` 先查 `KEY_NAMES`，未登記就 `KeyFileError("unknown subscription")`（`lgwd/keys.py:81-99`）；resolve 步驟 6 用「`keys.get(...)` 非 None」當已部署（`lgwd/registry.py:224-228`）；list 的 `available`、`available_again_at`、`_item` 都呼叫 `deployed`（`:183`、`:381`、`:484`），`_usage_fields` 的例外會被吞成 `unknown/internal`；`get` 直接讀 key 後回 `key`（`:279-284`）。原樣（`lgwd/registry.py:226-228`）：`candidates = [item for item in candidates if item[3] is not None]`／`if not candidates:`／`raise not_deployed()`。

### 2.2 選項

- **(a) 新 kind `local-login`（建議）**：`kind` 由 `CredentialProvider` 常數決定，不是政策資料；registry 用一個 `_deployed(sub, info)` 判斷（`kind == "local-login"` → True，否則 `keys.deployed`），resolve 步驟 6 對它不讀 key。改動集中在 `registry.py` 四個呼叫點＋`build_env`，`keys.py` 不動。
- **(b) 空值 key 檔——不可行**：`lgwd/keys.py:4` 明寫「值為空字串視同沒部署」、`:99` 是 `bool(self._values.get(name))`；填非空假值則等於在 vault 放假憑證，`get` 還會把它交出去。要讓 (b) 成立，改的程式與 (a) 一樣多，還多一個謊。
- **(c) 政策欄位（如 `requires_key: false`）**：把安全性質放進資料，打錯一格就能讓真的 key row 變成「不需 key」；還要新增驗證。另立部署表模組則是多一層同步。兩者在這一版都不建議。

### 2.3 要寫進 spec 的規則（建議）

- kind `local-login` ⇒ 恆已部署：list 的 `available` 只受整把冷卻影響；`available_again_at` 只由冷卻算；resolve 步驟 6 不排除它、不讀 key。
- resolve 的 `env` 恆 `{}`：`build_env` 在 `agent == "agy"`（或 `kind == "local-login"`）直接回空字典，不落進 `lgwd/providers.py:67-84` 的 `ANTHROPIC_*` 分支。模型只走 `meta.model`。
- `get` 回 409 `not_deployed`：沒有可交付的值；不回 200 帶 `"key": null`（會破壞「`key` 是值」的既有形狀）。
- `KEY_NAMES` 不加它：key 檔與 `get` 永遠沒有它的值，`tests/test_keys.py:59-62` 的集合不變。
- `env_schema` 維持 1：沒有新增 env 鍵；bump 成 2 會讓現在的 Mac sidecar 直接拒收（MLP 的 `sources.py` 只接受 ≤1，OUT-research-486 §2.2）。
- 稽核照舊記 provider／model；沒有值可記，不新增欄位。

## 3. 「只給 Mac sidecar」怎麼表達

Gateway 沒有 caller／host 概念，也不做應用層認證（spec key-vault「不做應用層認證」；只有 loopback＋SSH 轉發），所以**無法強制**，只能 advisory。

- A（建議）**每個實例自己的政策檔**：Mac 的 lgwd 政策含這把，worker 實例的不含（政策檔啟動時讀、換檔要重啟，`lgwd/policy.py:7-9`）。強制點在部署面，PMO 另寫計畫。
- B 同一份政策，worker 的 sidecar 不要求 agy：零部署差異，但靠自律；誤送時 worker 會拿到 200＋空 env，接著因為沒有 agy 而 503（MLP 的 `modules/sidecar/src/sidecar/drivers/agy.py:452-461` 的 `_ensure_command`，引在報告），不會洩漏任何值。
- C 新增呼叫方區分（`host=`／token）：與「不做應用層認證」衝突、query 可偽造，不建議。

不論 A 或 B，spec 都要寫成「Gateway 不保證呼叫方在哪一台」，不能寫成擋得住。

## 4. usage、冷卻、roles

- **usage**：不加 fetcher ⇒ `not_applicable`（`lgwd/usage.py:198-205`）；`available_again_at` 只由冷卻算（`:139-155`）。政策檔的 `usage` 段可以寫它但不生效（usage-reporting 最後一句）。
- **冷卻**：沿用既有端點，整把與模型兩層都可用；模型 id 必須被該把 `allow` 涵蓋（`lgwd/registry.py:323-343`）。agy 的額度訊號還沒接（OUT-research-486 §4），只能人工登錄——那是 sidecar／dispatch 的事。
- **roles（不決定）**：A（建議第一步）**不進任何 `roles.order`**，`GET /v1/roles/<role>?agent=agy` 回 200 `[]`；agy 的開法本來就不經 roles（MLP 的 agy driver 直接打 resolve）。B 進 `impl`／`rev`／`qa`：`privacy: public` 只在 `data=public` 時出現（`lgwd/registry.py:174`），等於提醒「只碰 public」。C 進 `PM`／`PMO`：`floor: real` 會把它篩掉（等級不足），除非 `privacy` 也改成 `real`；現在加等於無效。三者都只是政策資料，不改程式。

## 5. 影響、測試、PR 切法

### 5.1 既有 spec（逐條）

- `subscription-registry`：MODIFIED `get`、`共通 JSON schema`、`endpoint 的語意`、`登錄冷卻`、`錯誤回覆`、`resolve`、`resolve 的選把規則`、`env 的組成`；ADDED「沒有憑證的訂閱」。不改：`list`（欄位不變）、`models.default 的語意`、`models.default 必須被 allow 涵蓋`、`稽核紀錄`、`model 省略時的語意`（agy 走 default／allow 的既有推導）。
- `role-allocation`：MODIFIED `角色候選端點`（agent 三選一、agy 的空候選）；`角色分配表` 不改（要不要寫進 order 是 §4 的待裁定）。
- `key-vault`：MODIFIED `key 只保管在一處`（沒有憑證的訂閱不進 key 檔、不佔鍵名）；`拉取與輪替`／`值不外洩`／`不做應用層認證`／`服務不對外` 不改。
- `usage-reporting`、`connectivity`：不改。

### 5.2 測試影響

`tests/test_policy.py:55-57`、`:73-76`、`:97` 的 6 筆與順序加 `antigravity`；`tests/test_registry.py:315-318`（list 順序）、`:134-142`（provider 三欄表）加一列，並新增 agy 的 resolve／get 409／available／roles；`tests/test_main.py:161` 的 `len(...) == 6` → 7（`FAKE_KEYS` 不變）；`tests/test_keys.py:59-62` 不改（集合不變，正好證明沒有新鍵名）；`tests/test_http.py` 加 `agent=agy` 的 200 與未知 agent 的 400。`test_client`／`test_lgwctl_usage`／`test_usage*`／`test_keysync*`／`subprocess_policy.py` 不改（client／lgwctl 不驗 agent；`GoogleProvider` 沒有 fetcher）。

### 5.3 PR 切法（每步一個，main 不會更糟）

1. **PR 1 介面骨架，不改出廠政策**：`AGENTS`＋`_AGY_ONLY`＋`GoogleProvider`＋`local-login` 的部署／`build_env`／`get` 規則＋registry 四個呼叫點；測試用臨時政策 JSON。出廠 6 筆行為不變；唯一可觀察的收緊是冷卻 `agy/…` 變 400。
2. **PR 2 打開介面，只動政策資料**：`policy/default.json` 加 `antigravity`；list／resolve／get 的 E2E；client 與 errors 的說明。回退容易。
3. **PR 3 選配、等本人**：roles 的 order、`privacy`、README／docs 的政策範例。不改程式語意。

## 6. 要本人裁定的

1. 訂閱 id／provider 名（§1.2，建議 `antigravity`／`google`）——作者提議、本人未指示。
2. `models` 的模型 id 與「一模型一 row」（§1.4）——id 只能由本人從 `agy models` 抄，作者未查證。
3. `priority`（§1.5，建議 35）——作者提議、本人未指示。
4. `privacy`（§1.6，建議先 `public`）——與 ToS／資料條款一起裁；作者提議、本人未指示。
5. 「不需 key」的機制（§2，建議 kind `local-login`＋registry 判斷、不碰 key 檔）——作者提議、本人未指示。
6. `get` 對它的回覆（§2.3，建議 409 `not_deployed`；另一選項是 200 帶 `key: null`，但要動 schema）。
7. 「只給 Mac」的表達（§3，建議每個實例自己的政策檔；Gateway 只能 advisory）——部署面由 PMO 另寫。
8. roles 與 `privacy` 的組合（§4）——不決定。
