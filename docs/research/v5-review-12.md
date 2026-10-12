# v5 審查 #12：until-only＋拿掉 connect（reviewer-12）

審查對象：`git diff scope/gateway-v5...HEAD`（5 個 commit：`31564c3`、`dea99a1`、`394ed1d`、`1c31ace`、`0a89d19`）。
基準：`openspec/changes/gateway-v0/`（v5 版的 design、specs/subscription-registry、connectivity）、`docs/research/v5-plan.md`、`docs/research/v5-inventory.md`。
審查者：reviewer-12（opencode＋Space Bunny）。只審查，未改任何程式。

## 結論

**可以開 PR。** 高 0、中 2、低 6。中 2 都不是 #12 的程式缺陷：一個是測試名稱／註解在講另一件事（一行就能修），一個是 `docs/deploy.md` 仍描述 v4——那是 #13 的檔，#12 正確地沒有越界碰它，動作在 #13 與合併順序。

## 測試

`python3 -m unittest discover -s tests -t .`

```
Ran 239 tests in 105.315s

OK
```

（另外三次獨立跑也都 OK。輸出中夾的 `internal error kind=RuntimeError`、`keys reload failed…`、`policy reload failed…` 是測試刻意觸發的 log 輸出，不是失敗。）

## 規格逐條核對（spec subscription-registry／connectivity 的每個 Scenario）

| spec 位置 | Scenario | 實作 | 測試 | 判定 |
|---|---|---|---|---|
| spec.md:12-14 | list 不含秘密 | `lgwd/registry.py:140-141`（key 只在 get 加） | `tests/test_registry.py:140` `test_不含秘密` | ✅ |
| spec.md:16-18 | 冷卻反映在 list | `lgwd/registry.py:146-151` | `tests/test_registry.py:157` `test_冷卻反映在list`（並多測「到期前 1 秒」「到期含相等」） | ✅ |
| spec.md:23-25 | Claude Code 拿 Claude 訂閱 | `lgwd/registry.py:70-74` | `tests/test_registry.py:173` | ✅ |
| spec.md:27-29 | opencode 不能拿 Claude 訂閱（403，不含 key） | `lgwd/registry.py:70-71` | `tests/test_registry.py:177` | ✅ |
| spec.md:31-33 | 冷卻中仍可 get | `lgwd/registry.py:63-79` | `tests/test_registry.py:181` `test_冷卻中仍可get`（並多斷言 `cooldown` 欄位） | ✅ |
| spec.md:50-52 | Claude 訂閱的描述 | `lgwd/providers.py`（未動） | `tests/test_registry.py:86` | ✅ |
| spec.md:54-56 | 能不能用（`available`） | `lgwd/registry.py:143` `deployed and cooldown is None` | `tests/test_registry.py:118` `test_能不能用`、`:157-168` | ✅ |
| spec.md:68-70 | 帶恢復時刻（204、list 顯示 available false） | `lgwd/registry.py:81-101` | `tests/test_registry.py:215` `test_帶恢復時刻` | ✅ |
| spec.md:72-74 | 不合理的時刻（過去／超過 8 天 → 400 `until_out_of_range`，冷卻不變） | `lgwd/registry.py:111-115` | `tests/test_registry.py:232`、`:248` | ⚠️ 見低-3（測試強度） |
| spec.md:76-78 | 判讀不出恢復時間（呼叫當下＋1 小時） | 無（依 design 決定 7／8 的取代第 4 點，屬 sidecar 行為） | 僅測到 `lgw/signals.py:54` 丟 ValueError | ⚠️ 見低-2 |
| spec.md:63 | 未知欄位 MUST 忽略 | `lgwd/registry.py:88`（只看 `until`） | `tests/test_registry.py:286` `test_未知欄位忽略`（body 裡塞 `window`／`resets_at`／`extra`） | ✅ |
| spec.md:64 | 重複登錄取較晚的 until | `lgwd/registry.py:94-98`＋`lgwd/store.py:66-72` | `tests/test_registry.py:264` `test_同一筆重複登錄取較晚的`（並驗 audit 記的是有效值） | ✅ |
| spec.md:65 | 過了 until 自動解除 | `lgwd/registry.py:149`（`> now`，含相等即解除） | `tests/test_registry.py:157`、`:278` `test_到時自動解除` | ✅ |
| spec.md:83-85 | get 留下紀錄、不含 key | `lgwd/registry.py:78`＋`lgwd/store.py:78-92` | `tests/test_registry.py:322` `test_get留下紀錄`、`:344` `test_稽核只追加且沒有key` | ✅ |
| spec.md:98-100 | 缺必填 → 400 `bad_request`，`missing` 含 `until` | `lgwd/registry.py:88-89` | `tests/test_registry.py:293`、`:298`、`tests/test_http.py:343-349` | ✅ |
| spec.md:102-104 | 時刻格式不對 → 400 `invalid_until`，冷卻不變 | `lgwd/registry.py:105-110` | `tests/test_registry.py:222`、`:236`、`:351-360` | ⚠️ 見低-3 |
| spec.md:106-108 | 連不上 → `unreachable`，不丟例外 | `lgw/client.py:79-80` | `tests/test_client.py:247`、`:271`（未動） | ✅ |
| spec.md:60 | `until` 必填、RFC 3339 UTC | `lgwd/registry.py:103-116`（含時差正規化） | `tests/test_registry.py:222`（無時區字串→`invalid_until`）、`:301` `test_帶時差的時刻正規化成UTC` | ✅ |
| connectivity spec.md:28 | 客戶端自備通道，LLMGateway 不提供連線程式 | `lgw/__init__.py:11`、`lgw/connect.py` 已刪 | `tests/test_package.py:46` `assertFalse(hasattr(lgw, "connect"))`、`:62` 描述不含 connect | ✅ |

### 錯誤碼對照 design 決定 6 的碼表

`lgwd/errors.py:5-13` 的模組 docstring 表 ＋ `lgwd/errors.py:40-47` 的實作，對照 design.md:230（決定 7／8 的取代）與 spec.md:89-94：

| design／spec 要求的碼 | 實作 | 狀態 |
|---|---|---|
| 400 `bad_request`（`missing: ["until"]`） | `lgwd/errors.py:33`／`lgwd/registry.py:88-89` | ✅ |
| 400 `invalid_until` | `lgwd/errors.py:41` | ✅ 取代舊 `invalid_window`＋`invalid_resets_at` |
| 400 `until_out_of_range` | `lgwd/errors.py:46` | ✅ 取代舊 `resets_at_out_of_range` |
| 403 `agent_not_allowed` | `lgwd/errors.py:51` | ✅ 未動 |
| 404 `not_found` | `lgwd/errors.py:56` | ✅ 未動 |
| 409 `not_deployed` | `lgwd/errors.py:61` | ✅ 未動 |
| 413 `body_too_large` | `lgwd/errors.py:66` | ✅ 未動 |
| 500 `internal` | `lgwd/http.py:134` | ✅ 未動 |
| 客戶端 `unreachable`／`bad_response` | `lgw/client.py:80`／`:88-90` | ✅ 未動 |

`lgwd/http.py` 整個未被這 5 個 commit 碰到（路由、`_read_json`、1 MB 上限、log 只記方法／路徑／狀態碼都原封不動）。舊的三個 window 錯誤碼已無任何殘留呼叫點（`grep invalid_window|invalid_resets_at|resets_at_out_of_range` 在 `lgw/`、`lgwd/`、`tests/`、`policy/` 全無命中）。

## 殘留物掃描（window／resets_at／5h／1w／connect）

`grep -rnE '\b(window|resets_at|5h|1w|connect|cooldown_buffer_seconds|resets_at_max_ahead_seconds)\b'`，排除 `.git`／`docs/research`：

- **`docs/research/*`**：歷史調查檔，依 v5-inventory 第 69 行保留，不在範圍。✅
- **`lgwd/store.py:36`／`lgwd/http.py:57,67`**：`sqlite3.connect`、`BrokenPipeError`、`ConnectionResetError` — 無關字詞。✅
- **`tests/test_workflows.py:135,149`／`tests/test_install.py:532`／`tests/test_http.py:268`**：SSH／curl 的連線錯誤字串。✅
- **`tests/test_registry.py:288,294,332,342`**、`tests/test_policy.py:53,121-125`、`tests/test_package.py:46,63-64`：**刻意的反向斷言**（送舊欄位確認被忽略、斷言舊欄位不存在）。✅
- **`resets_at`**：`lgw/signals.py` 的 `Classification.resets_at`／`Signal.resets_at` 仍存在，這是**正確的**——spec.md:66 明定「`lgw.signals` 只負責把判讀結果轉成這個形狀」，上游訊號裡的欄位名本來就叫 resets_at，要到 `to_cooldown_body()` 才轉成 `{until}`。✅
- **`lgw/signals.py:127`、`tests/test_signals.py:81`**：註解仍寫「用政策預設冷卻」。見低-1。
- **`docs/deploy.md`**：整份文件仍描述 `lgw connect`／`connect.json`／舊錯誤碼。見中-2。
- **`README.md`**：`grep -iE 'connect|window|resets_at|5h|1w'` 無命中。✅
- **`pyproject.toml:8`**：描述已拿掉 connect，且有測試守著（`tests/test_package.py:62-64`）。✅

## key 值與複雜度

- **`lgwd/http.py:93,115,129,133,134,137`**：log 只印方法、路徑、狀態碼、例外種類；不印標頭、body、值。`tests/test_http.py:252-263` `test_log_clean` 把 canary 塞進 `until` 欄位與標頭並斷言不在 log（改欄位後反而更有意義——canary 現在會真的進 request body）。`tests/test_http.py:241-250` 斷言 500 只記例外種類。✅
- **`lgw/client.py:42-45`**：`Result.__repr__` 只印 ok/status/error，不印 `data`（`get` 的 key 在 `data` 裡）。✅ `Client.__repr__` 只印 base_url／timeout。✅
- **`lgwd/registry.py`**：`key` 只在 `get_subscription` 的回覆 dict 裡，沒有 `__repr__`／`__str__` 會印到它。稽核 `audit_add`（`lgwd/store.py:78-92`）只吃 ts／subscription／action／agent／until，沒有 key 參數。✅
- **複雜度**：整體是**淨簡化**。`lgwd/registry.py` 淨少 9 行（+37／−46：拿掉 `_cooldown_until` 的分支、`WINDOWS`／`WINDOW_DEFAULT_SECONDS` 常數）；`lgwd/store.py` 淨少 12 行（+16／−28：`cooldown_get`（`:63`）由 `fetchall` 收成 `fetchone`、少了 `cooldown_for`、`audit` 少一個欄位）；`lgw/connect.py`（539 行）與 `tests/test_connect.py`（780 行）整檔拿掉共 1319 行。沒有為了 until-only 額外堆抽象。✅
  - 唯一可挑的是 `lgwd/registry.py:96-97` 把 `from_rfc3339(prev["until"])` 算兩次，可抽成一個區域變數；純粹是潔癖，不影響正確性（讀取無副作用、同一 txn 內）。列為觀察，不列為發現。
  - `lgwd/store.py:73` 的 `WHERE excluded.until > cooldowns.until` 在 `registry.py:94-98` 已先算過 max 之後看似多餘，但它是 DB 層自己的不變量（讓 `cooldown_set` 單獨呼叫仍正確），刻意留著是合理的。列為觀察，不列為發現。

## 範圍

`git diff --name-only scope/gateway-v5...HEAD` 共 18 個檔，逐一手動核對：

```
lgw/__init__.py        lgw/client.py       lgw/connect.py(刪)   lgw/signals.py
lgwd/errors.py         lgwd/policy.py      lgwd/registry.py      lgwd/store.py
policy/default.json    pyproject.toml
tests/test_client.py   tests/test_connect.py(刪)  tests/test_http.py  tests/test_main.py
tests/test_package.py  tests/test_policy.py  tests/test_registry.py  tests/test_signals.py
```

**#13 範圍（`lgwd/main.py`、`lgwd/keys.py`、`deploy/`、`.github/`、`ops-scripts/`、`docs/deploy.md`）一個都沒被碰到**（`git diff --name-only | grep -E '^(lgwd/main\.py|lgwd/keys\.py|deploy/|\.github/|ops-scripts/|docs/deploy\.md)'` → 空）。✅

`openspec/` 與 `docs/` 也完全沒動（v5 的 spec/design/tasks 已在 `scope/gateway-v5` 就位，本單是實作它們）。這是正確的——改規格不是實作單的權限。

檔案清單也與 v5-inventory 的處置表一一對上（`lgw/__init__.py`／`client.py`／`connect.py`(拿掉)／`signals.py`、`lgwd/errors.py`／`policy.py`／`registry.py`／`store.py`、`policy/default.json`、`pyproject.toml`、對應 7 個測試檔）。沒有多做、也沒有少做。

## 發現

### 中-1　舊政策檔的 fail-closed 行為沒有被正確記錄，測試在講另一件事

- **檔案:行號**：`tests/test_policy.py:121-127`（配合 `lgwd/policy.py:118-120`）
- **問題**：`test_舊窗口欄位被忽略` 的註解寫「舊政策檔留著 `cooldown_buffer_seconds`／`resets_at_max_ahead_seconds` 也能載入」。但 `_good_policy()` 已經含 `until_max_ahead_seconds`，這個測試只是**再加兩個舊欄位**，所以它實際證明的是「未知欄位被容忍」，不是「舊政策檔能載入」。真正的 v4 政策檔（只有舊兩個欄位、沒有 `until_max_ahead_seconds`）會被直接拒。
- **證據**（`.work/probe_policy.py`，實驗檔留在 worktree 的 `.work/`）：
  ```
  RESULT: 舊政策檔載入失敗 -> policy.until_max_ahead_seconds must be a positive integer
  ```
  即 `load_policy` 走 `lgwd/policy.py:119-120` 丟 `PolicyError`。
- **影響**：**不是 bug，是刻意的 fail-closed**（寧可開不了機也不要默默用錯上限）。而且 `deploy/install.sh:155` 要求 tag 裡有 `policy/default.json`、`deploy/llmgateway-run:17` 指的是 `${CURRENT}/policy/default.json`，所以一般升級會自動帶到新欄位。真正會踩到的只有「自己手改過政策檔」的機器。問題在於：(a) 測試名稱與註解會讓後人誤以為相容性有保證；(b) 升級手冊沒提這件事。
- **建議**：(1) 把該測試改名為 `test_未知欄位被忽略`（或加註「這裡的『舊』只是多兩個欄位，不是真的舊格式」）；(2) 另加一條 `test_純舊欄位政策檔被拒` 明確記錄 fail-closed；(3) 在 #13 重寫 `docs/deploy.md` 的升級步驟加一句「政策檔欄位更名過（`resets_at_max_ahead_seconds` → `until_max_ahead_seconds`），自訂過政策檔的機器要跟著改，否則開不了機」。第 (1)(2) 項可以在本 PR 修，第 (3) 項掛在 #13。

### 中-2　`docs/deploy.md` 仍完整描述已刪掉的 `lgw connect` 與舊錯誤碼表（#13 的檔，非 #12 缺陷）

- **檔案:行號**：`docs/deploy.md:26`、`:338`、`:351`、`:356`、`:361`、`:396`、`:468`、`:484-486`、`:499-500`
- **問題**：文件仍教人寫 `~/.config/lgw/connect.json`、跑 `python3 -m lgw.connect --config …`、`with lgw.connect.connect(lgw.connect.load_config(path)) as base_url:`；§5 錯誤碼表仍列 `invalid_window`／`invalid_resets_at`／`resets_at_out_of_range`；§4 仍說「每一筆有 `cooldown`（`5h`、`1w` 各為 `{until}` 或 null）」；§5.5 標題還是「`lgw connect` 回『無法確認』」。
- **證據**：以上行號的 grep 命中（`docs/deploy.md` 是本次唯一還在講 v4 介面的大檔）。
- **影響**：**`docs/deploy.md` 依 v5-inventory:68 明列是 #13 的工作**，而且文件頂端已經放了 v5 改寫大綱橫幅（`docs/deploy.md:4,7`）。所以 #12 沒有越界碰它是**正確的**。風險在合併順序：v5-plan §6 說「兩單都只碰文件與程式，不 merge（照本次規矩由本人合）」，若 #12 先合而 #13 未合，main 會短暫有一份指向已刪程式的文件。
- **建議**：#12 不需要改。開 PR 的說明裡請寫一句「`docs/deploy.md` 仍是 v4 版，屬 #13；兩單一起合才一致」，避免下游 review 或驗收誤判成 #12 漏做。若 #12 必須先合，請在 PR 描述標成 known-gap 並掛到 #13。

### 低-1　殘留「政策預設冷卻」說法（v5 已無此概念）

- **檔案:行號**：`lgw/signals.py:127`、`tests/test_signals.py:81`
- **問題**：兩處註解都寫「用政策預設冷卻」。v5 拿掉了 `cooldown_buffer_seconds` 與 `WINDOW_DEFAULT_SECONDS`，服務端已無任何「政策預設冷卻長度」；spec.md:66 與 design.md:233 明定這時候是**呼叫方**以「呼叫當下＋1 小時」為 `until`。這個殘留說法會讓 sidecar 隊員去找一個不存在的政策欄位。
- **證據**：`lgw/signals.py:127` `# Claude spend limit 類：換把但不帶重置時刻（用政策預設冷卻）`；`tests/test_signals.py:81` `self.assertIsNone(res.resets_at)  # 用政策預設冷卻，不拿訊息時間`。
- **建議**：兩處都改成「呼叫方用現在＋1 小時」。純註解文字，改動零風險。

### 低-2　spec 的「判讀不出恢復時間」Scenario 沒有可測對象

- **檔案:行號**：`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:76-78`；實作端 `lgw/signals.py:48-59`；測試端 `tests/test_signals.py:511-513`
- **問題**：Scenario 要求「以呼叫當下＋1 小時為 `until` 登錄」。design.md:233 與 tasks.md:60 都把這件事划給 sidecar，`lgw.signals` 只負責轉換形狀。因此本 repo 只能測到「`resets_at is None` 時 `to_cooldown_body()` 丟 ValueError」（`tests/test_signals.py:512` 有測到），Scenario 的後半段在本 repo 沒有對象可測。
- **證據**：`grep -rn 'now + 1 hour\|現在＋1 小時' lgw/ lgwd/ tests/` → 只有 `lgw/signals.py:8-9,40,51-52` 的 docstring 與 `tests/test_signals.py:511` 的 ValueError 斷言。
- **建議**：這是規格切邊的必然結果，不算漏做。兩個選項：(1) 在本 repo 加一條服務端整合斷言——「`until = now + 1h` 必然被接受且 `available` 為 false」（現在＋1 小時落在 8 天內，`lgwd/registry.py:114` 不會擋），把「約定在值域內」這件事釘住；(2) 在 tasks.md 的驗收項（4.1′／4.5）標明由 sidecar 側確認。建議做 (1)，成本一行。

### 低-3　「冷卻不變」的斷言強度不足（沒有真的驗「已存在的冷卻沒被蓋掉」）

- **檔案:行號**：`tests/test_registry.py:222-231`（`test_until型別或格式不對回invalid_until`）、`:232-247`（`test_until在過去或超過八天回until_out_of_range`）
- **問題**：兩個 spec Scenario 都寫「…冷卻不變」。但測試是在**本來就沒有冷卻**的狀態下斷言 `item["cooldown"] is None`，等於只驗證「沒有寫進去」，沒有驗證「已經登錄的冷卻沒被這次失敗的請求蓋掉或清掉」。
- **證據**：`tests/test_registry.py:236-237` 與 `:244-245` 都是 `self.assertIsNone(item["cooldown"])`，而此時 `claude-personal` 從未被登錄過冷卻。
- **影響**：**不是 bug**。結構上不可能發生——`lgwd/registry.py:91` 的 `_cooldown_until()` 在 `:93` 的 `with self._store.txn()` **之前**就丟 `ApiError`，失敗路徑完全不進 DB，`audit_add` 也不會被呼叫。純粹是測試沒把這個保證釘住。
- **建議**：加一條 `test_被拒的請求不動既有冷卻`：先 `cooldown("claude-personal", "2026-10-11T15:00:00Z")`，再送 `{"until": "2020-01-01T00:00:00Z"}`，斷言仍為 `{"until": "2026-10-11T15:00:00Z"}` 且 `audit_all()` 長度沒有增加。一條測試同時補上「冷卻不變」與「稽核只追加且失敗不寫」兩個保證。

### 低-4　`lgwd/errors.py` 對 design 的引用不精確

- **檔案:行號**：`lgwd/errors.py:1`
- **問題**：寫「v5 的錯誤碼表見 design **決定 8** 的取代」，但 `openspec/changes/gateway-v0/design.md:228` 的章節標題是「**決定 7／8 的取代**：冷卻 until-only」，而且該節同時涵蓋 `available`／共通 JSON schema（出自決定 7）。單寫「決定 8」會讓人找不到正確段落（決定 8 本身在 `design.md:176-180` 仍是「窗口只有 5h 與 1w」的舊版）。
- **證據**：`design.md:228` 的原文標題。
- **建議**：改成「v5 的錯誤碼表見 design 決定 7／8 的取代」。`lgwd/registry.py:1` 與 `lgwd/store.py:1` 已寫「決定 6、7、8、10 與 v5 的決定 8 取代」，同樣建議一併統一成「7／8」。

### 低-5　v5 增補的七個 task 未勾選

- **檔案:行號**：`openspec/changes/gateway-v0/tasks.md:55-61`
- **問題**：`1.1′`、`1.2′`、`1.4′`、`1.6′`、`2.1′`、`2.2′`、`2.4′` 七項正是本單的內容，卻都還是 `- [ ]`。repo 有勾選的先例：`31a601b tasks: mark 2.1 (three-interface client) and 3.1c (lgw connect) done`。
- **但**：v4 的 `1.1`～`1.6`（`tasks.md:5-10`）從頭到尾也從來沒被勾過，所以「實作單要勾 1.x」這個慣例並不存在。
- **建議**：**PM 裁決**。勾了對日後驗收追溯有幫助（1.6′ 尤其能對應本檔的規格核對表），但因為 v4 留下同樣的缺口，我傾向標成「可選」而不是「#12 漏做」。

### 低-6　`_cooldown`／`_cooldown_until` 的註解密度略高（無風險）

- **檔案:行號**：`lgwd/registry.py:103-104`、`lgwd/registry.py:146-147`
- **問題**：`_cooldown_until` 的 docstring 把三種錯誤的分支條件完整重述了一遍，而程式碼下一行就寫著同樣的內容。`_cooldown` 的「過了 until（含相等）自動解除」是有價資訊（邊界語意），但寫成註解不如寫成斷言——事實上 `tests/test_registry.py:162-168` 已經斷言了含相等的邊界。
- **影響**：無。純粹是「註解會過期」的普遍風險，這兩條還不算嚴重。
- **建議**：**不必修**。若要收斂，`_cooldown_until` 的 docstring 可縮成一句「驗 until；型別／格式不對→`invalid_until`，超範圍→`until_out_of_range`」，把細節交給 spec 與上面的核對表。

## 附錄：本次審查做過的驗證

- `python3 -m unittest discover -s tests -t .` × 4 → 皆 OK，239 tests。
- `git diff scope/gateway-v5...HEAD` 全 18 檔逐檔讀過（含 `lgw/connect.py` 539 行刪除內容、`tests/test_connect.py` 780 行刪除內容）。
- `openspec/changes/gateway-v0/design.md` 決定 6 的錯誤碼表（`:120-148`）與 v5 變更段（`:205-237`）對照 `lgwd/errors.py` 全表。
- `openspec/changes/gateway-v0/specs/subscription-registry/spec.md` 與 `specs/connectivity/spec.md` 全篇逐個 Scenario 核對（上表）。
- 全庫 grep：`window`／`resets_at`／`5h`／`1w`／`connect`／`窗口`／`政策預設`／舊錯誤碼／舊政策欄位名。
- `lgwd/http.py`、`lgwd/clock.py`、`lgwd/main.py`（唯讀，確認 log 與時鐘行為未被本次改動影響）；`deploy/install.sh`、`deploy/llmgateway-run`（唯讀，確認政策檔來源）。
- 實驗：`.work/probe_policy.py`（中-1 的 fail-closed 驗證）。

未讀任何憑證檔或密碼檔；未使用 ssh／gh／push；未觸發 workflow；未動其他 branch、未 `git switch`。