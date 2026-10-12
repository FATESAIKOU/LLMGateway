# 審查 #21：openrouter 出廠預設改 openrouter/free、allow 加其樣式（reviewer-21）

審查對象：`git diff main...HEAD`（branch `review/openrouter-default`＝工單 #21，2 個 commit）。

- `d267c34` policy：openrouter 出廠預設改 openrouter/free、allow 加該樣式（#21）
- `251dff2` test：補 openrouter 出廠預設與 get 回傳的測試（#21）

審查者：reviewer-21（作者是 Step 5 Preview，不是審查者；PM 是 llmgateway-f8）。只審查，未改任何程式。未讀任何憑證檔或密碼檔；未 ssh／gh／push／觸發 workflow；實驗輸出在系統暫存目錄，不進本 commit。

## 結論

**可以開 PR。** 高 0、中 0、低 0、資訊 3。

五個指定重點逐條有證據：其他 4 個訂閱逐鍵未動；load／list／get 都正確帶出新 default；測試真的釘住值（含負向）；spec 沒釘值、`docs/deploy.md` 查無訂閱表，兩者都不用跟改；全量 257 tests OK。改動面極小（程式 2 檔 +6／-3 行、測試 2 檔 +30／-9），無阻擋項；3 條資訊項皆不擋，可選順手處理。

## 測試

`python3 -m unittest discover -s tests -t .`

```
Ran 257 tests in 154.447s

OK
```

（首次跑 120s 被我自己的 timeout 砍掉，是我給太短——整包約 155 秒；單跑 `tests.test_install.InstallScriptTests.test_check_keysync_states` 5.1s OK。log 中的 `internal error kind=RuntimeError` 與 `keys reload failed` 是既有測試的預期輸出，不是失敗。）

## 逐項核對（5 點，都有證據）

### 1. 其他訂閱沒有動到 ✅

- `git diff main...HEAD --stat` 只有 4 檔：`policy/default.json`（+1／-1 行）、`lgwd/policy.py`（+5／-2 行）、`tests/test_policy.py`、`tests/test_registry.py`。
- `policy/default.json` 與 main 逐鍵 JSON 比對：`claude-personal`／`claude-company`／`opencode-zen`／`ollama-cloud` 全部 **unchanged**、`until_max_ahead_seconds` unchanged、頂層鍵相同；唯一 `CHANGED` 是 `openrouter`。
- `lgwd/policy.py` 的改動只有三處：新常數 `_OPENROUTER_ALLOW`（`:25`）、`load_policy` docstring 一行（`:98`）、openrouter 白名單等值比對換成常數（`:123-124`）。`_CLAUDE_ONLY`／`_ZEN_ONLY` 與其檢查行原封。

### 2. `lgwd/policy.py` 載入、registry get／list 正確帶出 default ✅

- 載入：`_parse_subscription` 照舊收 `models.default`（非空字串或 null）；openrouter 允許清單改成精確等值 `("*:free", "openrouter/free")`（`policy.py:25,123-124`）。
- registry：`_item` 把 `models.allow`／`models.default` 放進共通 JSON（`registry.py:134-137`）；list 與 get 共用同一個 `_item`，get 多一把 key（`registry.py:140-141`）。
- 動態實測（假 canary key、FakeClock、暫存 SQLite，未碰真實 key 檔）：
  - `load_policy("policy/default.json")` → `models_allow=('*:free','openrouter/free')`、`models_default='openrouter/free'`；
  - list 的 openrouter：`{"allow": ["*:free", "openrouter/free"], "default": "openrouter/free"}`；
  - get(`openrouter`, `opencode`) 與 get(`openrouter`, `claude-code`)：同值且帶 canary key。
- 負向對照：main 的舊政策檔（allow 只 `["*:free"]`）給**新 loader** 載會 `PolicyError: openrouter must only allow *:free models` → 精確白名單的 fail-closed 行為保留，舊值混不進新程式。
- 值本身外部查證（2026-10-12）：`openrouter/free` 是 OpenRouter 官方的 Free Models Router（零價、自動挑免費模型）；它是 literal id、不含 `:`，`*:free` 樣式蓋不中，所以必須把自己放進 allow 才選得到——與 commit 訊息的理由一致。出處：<https://openrouter.ai/openrouter/free>、<https://openrouter.ai/docs/guides/routing/routers/free-router>。

### 3. 測試釘住該值 ✅

- `tests/test_policy.py`：
  - `LoadDefaultPolicyTests.test_openrouter出廠預設openrouter_free`（`:58-66`）：讀**真的出廠檔** `policy/default.json`，斷言 `default=="openrouter/free"`、allow 含 `*:free` 與 `openrouter/free`、`len==2`。
  - `test_openrouter_only_free_models`（`:109-114`）：4 個負向子案例 `["*"]`、`["*:free","*"]`、只 `["openrouter/free"]`、多帶 `"*"` 都必須 `PolicyError` → 白名單不接受子集也不接受超集。
  - `_good_policy` 基準值同步新值（`:33-34`）。
- `tests/test_registry.py`：`RegistryCase` 用真 `policy/default.json`（`:19,51`）＋假 key，等於端到端釘住。
  - `JsonSchemaTest.test_models欄位`（`:132-137`）：list 的 openrouter `allow`／`default` 斷言新值，另補 `opencode-zen.default` 為 null。
  - `GetTest.test_get_openrouter回傳default模型`（`:208-214`）：openrouter 給 `opencode` 與 `claude-code` 兩個 agent，get 回 canary key 且 `models.default=="openrouter/free"`、`allow` 同值。
- 涵蓋判斷：list／get 兩介面都有、兩個 agent 都有、`default` 與 `allow` 都有、負向有、而且測的是真出廠檔——值一旦回歸會紅。

### 4. spec／docs 是否該跟改 ✅（一處可選，見資訊-1）

- spec：`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:42` 只定義形狀「`models`：`allow` 與 `default`」，沒有釘任何值 → 不需改。`openspec/specs/` 的現有主規格（agent-lease、usage-record）與此無關。
- `docs/deploy.md`：全檔查證**沒有列出各訂閱 allow／default 的「訂閱表」**——`openrouter` 只出現在 §2.3 的 key 名稱清單（`:376`）與 §5.5 的 403 說明（`:584` 附近），都不是要跟改的值 → 不需改。（若工單指的是 MyPMO 側的表，不在本 repo。）
- 唯一落後的檔案是 `openspec/changes/gateway-v0/design.md:160` 決定 7 的示意 JSON（openrouter 仍寫 `"allow": ["*:free"], "default": null`）；但該例的 `"priority": 30` 也早就與出廠政策的 20 不同步，屬歷史決策記錄的示意例、不是規格來源——可選順手更新，不阻擋。

### 5. 全量測試 ✅

- 見上「測試」節：257 tests、154.4s、OK；exit code 0。

## 發現

### 資訊-1　`design.md` 的示意 JSON 已落後出廠政策（含本單前的既有落差）

- **檔案:行號**：`openspec/changes/gateway-v0/design.md:160`
- **問題**：範例寫 `"models": {"allow": ["*:free"], "default": null}`，與新出廠值不同；同範例的 `priority: 30` 也和出廠的 20 不同（本單之前就如此）。
- **證據**：`git diff main...HEAD --stat` 不含 design.md（本 diff 未動它）；`policy/default.json:20-23` 為新值對照。
- **建議**：可選——順手把該例更新成與出廠檔一致（`["*:free", "openrouter/free"]`／`"openrouter/free"`／`20`），或留待該 change 歸檔時處理。不擋 PR。

### 資訊-2　換版提醒：舊政策檔的 openrouter allow 會被新程式拒絕

- **檔案:行號**：`lgwd/policy.py:123-124` 配 `docs/deploy.md:462-464`
- **問題**：白名單是精確比對，allow 仍是舊值 `["*:free"]` 的政策檔在新 `lgwd` 下會載入失敗（`lgwd` 起不來）；這是既有的 fail-closed 設計（政策檔跟著 tag 換），不是本次新增的風險。
- **證據**：負向對照實測（本文件 §2）；deploy.md 4.2 已寫「升級就是把那一版的 `policy/default.json` 換上」「換政策要重啟 lgwd」。
- **建議**：不需要改碼；若有人「跨版沿用同一份政策檔」，除了補 `until_max_ahead_seconds` 外，也要把 openrouter 的 `allow`／`default` 換成新值。可選：在 4.2 那句括號補一句。

### 資訊-3　`test_get_openrouter回傳default模型` 名實略不副（小殘留）

- **檔案:行號**：`tests/test_registry.py:208-218`
- **問題**：改名後主軸是 openrouter 的 default，但尾巴仍留著 `opencode-zen`／`ollama-cloud` 的 key 斷言（原「opencode 拿 Zen 與 OpenRouter」測試的遺留）。
- **證據**：`:215-218`。
- **建議**：無實害（那些斷言仍有意義：證明 get 沒被改壞）。要順手可把尾巴三行拆去 `GetTest.test_opencode拿Zen與OpenRouter` 之類的小案例。不擋 PR。

## 補充觀察（非發現）

- 白名單是**精確等值**：未來要再收新樣式（例如其他 router 的 literal）都得同時改 `policy.py` 常數與政策檔，不能只改資料檔——既有設計，非本次引入，本次照同一模式做。
- 測試面：新增／改寫的斷言集中在「出廠檔值」與「list／get 帶出值」兩層，正是這張單的風險點；`tests/test_http.py` 與 `lgw/` 客戶端不含任何模型字面值（repo 全檔 grep 佐證），所以沒有第三個地方會偷偷釘住舊值。
- 本單在測試面淨增 1 條方法（`d267c34`／`251dff2` 合計：新增 `test_openrouter出廠預設openrouter_free`、`test_get_openrouter回傳default模型` 為原測試改名），其餘測試數變化來自 main 上的其他修正。
