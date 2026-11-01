# #139 設計（c）：list／resolve 回傳 key 層級用量（MyLinuxPool#552 D2）

架構師：llmgateway-arch-m1-1　日期：2026-11-01　狀態：待 PM／本人核准

## 結論

**第 2 輪（2026-11-01，本人補充「每把 key 的用量管子不只一根」）後結論翻轉：現行 `usage` 形狀表達不了，Gateway 要改，改法是在 `usage` 加一個鍵 `pipes`（只加不換），change 為 `openspec/changes/usage-multi-pipes/`（之後另開 d 實作）。**

- 本人已回答 J-2＝百分比就夠、J-1＝sidecar 吸收名稱差異、J-3＝sidecar 自己算 `exhausted`、J-4 交 MyLinuxPool；因此第 1 輪的「名稱不同」差異（§1 G-2～G-4）仍由 sidecar 轉接層吸收（§2），**不**為此改 Gateway。
- 要改 Gateway 的唯一理由是新需求：一把 key 多根獨立額度桶（各有名稱／所屬模型群、百分比、重置時刻）。主 spec 固定七鍵、只有 `session`／`weekly`（`openspec/specs/usage-reporting/spec.md:9,28`；`lgwd/usage.py:33`），放不下 OpenRouter 的 credits 與 Antigravity 的三組。
- `session`／`weekly` 原樣保留作相容層；`pipes` 是新增鍵（`pipes` 由 `build_usage` 從 windows 加固定表產生，不另設 compat 層）。
- **誠實的範圍：本 change 實際新增的管子只有 OpenRouter credits；Antigravity 三組不在 Gateway**（local-login、無憑證、`providers.py:159-165` 禁止覆寫 fetcher；`/usage` 只能在 Mac 跑，Gateway 主機跑不了）。sidecar 的 `UsageWindow`（name／used_percent／scope／applies）與這裡的 pipe 形狀不同；兩邊要不要共用一個形狀待對齊（J-9）。
- 目前 sidecar 只讀 `session`／`weekly` 與 `available_again_at`，不讀 `pipes`；在另開單消費前，credits 用完對挑 key 沒有效果（J-10）。
- 細節、對照表（每個 source 幾根管子）、model→管子的比對規則見 change 的 `design.md`；本文件只放缺口分析、判斷點與跨 repo。

第 1 輪的缺口分析（§1）與對照表（§2）仍有效，下文保留。另有一個行為上的提醒（輪詢節奏，§3），屬 sidecar 的用法。

## 1. 缺口分析

引用皆為本 worktree（main 01b414e）原樣。「期待」欄指 MyLinuxPool#552 的設計 §3／§10。

| # | 期待 | Gateway 現況（路徑:行號） | 判定 |
|---|---|---|---|
| G-1 | key 層級用量隨 list／resolve 回傳，不新增端點 | list：`lgwd/registry.py:141-143` `item["available_again_at"] = again` / `item["usage"] = usage`；resolve：`lgwd/registry.py:269` `"usage": self._safe_usage(selected.name)`（在 `meta` 內）。`get`（`/v1/subscriptions/{id}`）與 `roles` **不帶**（`lgwd/registry.py:139`、spec 第 152 行情境「get 與冷卻登錄不觸發」） | **已有**。sidecar 取用量只能讀 list 或 resolve，不要用 get |
| G-2 | `observed_at`（Gateway 自己的觀察時刻） | `usage.fetched_at`：「最近一次**成功**取得的時刻（RFC 3339 UTC），從沒成功過為 null」（spec 第 13 行）；`lgwd/usage.py:267` 成功時 `entry.fetched_at = now`（Gateway 時鐘） | **名稱不同**（`fetched_at` ≙ `observed_at`，語意相同） |
| G-3 | 窗口陣列 `windows[{name, used_percent, remaining, unit, resets_at}]` | 固定兩個具名窗口 `session`／`weekly`，各為 `{"remaining_percent": 0..100, "resets_at": RFC3339}` 或 null（spec 第 16 行）。無陣列、無 `used_percent`、無 `remaining`／`unit` | **名稱不同**：`used_percent = 100 - remaining_percent`（sidecar 算；Claude 端本來就是 `remaining = 100 - used`，spec 第 47 行）；`resets_at` 是字串而非 epoch，sidecar 轉。`remaining`／`unit`（絕對數量）**缺**，但 §3 寫明「有數量時才填」，可選 → 不補（判斷點 J-2） |
| G-4 | 窗口 `name` 為 `"5h"｜"1w"｜"daily"｜…` | 名稱是 `session`／`weekly`，**意義依 source 而不同**：Claude `five_hour→session`、`seven_day→weekly`（spec 第 47 行）；OpenRouter 的 `session` 是 UTC 日的免費請求數（`lgwd/usage_openrouter.py:1-8`、`:105` `windows["session"] = (percent, resets)`）；Ollama 取 `included.session`／`included.weekly`（`lgwd/usage_ollama.py:3-5`） | **名稱不同**，且是陷阱：sidecar **必須依 `usage.source` 對照**，不可把 `session` 一律當 5h。見 §2 對照表 |
| G-5 | 15 分鐘過期、以 `observed_at` 為準 | Gateway 只回 `fetched_at` 與自己的 `stale`（`now - fetched_at > TTL`，OpenRouter／Ollama TTL 預設 120 秒，`usage_openrouter.py:113`、`usage_ollama.py:80`；Claude 1800 秒，`usage_claude.py:168`）。超過 6 小時改回 `unknown／stale_expired`（`lgwd/usage.py:45,124-125`） | **已有**（資料足夠，過期判斷是 sidecar 的）。`usage.stale` 是 Gateway TTL 的訊號，**不等於**「sidecar 的 15 分鐘過期」：sidecar 應只用 `fetched_at` 自算。輪詢節奏另見 §3 |
| G-6 | `source` 字串 | `source`：`ollama-api-balance`、`claude-statusline`（`usage_claude.py:54`）、`openrouter-api-key`（`usage_openrouter.py:49`）；`not_applicable` 時 null（spec 第 12 行） | **已有**。注意 `claude-statusline` 與 sidecar 自己的 claude 來源同名但不同層（Gateway 的是對 key 層級的 Claude 訂閱查，且預設關閉，`usage_claude.py:170`）；sidecar 去重以 subscription id＋`fetched_at` 為準，不以 source 字串 |
| G-7 | `exhausted: bool｜None` | 無此欄位。有 `available_again_at`（list 限定，spec 第 84-85 行）＝「`remaining_percent ≤ 0` 的窗口之 `resets_at`」與冷卻的最晚者（`lgwd/usage.py:139-155`） | **Gateway 不該做**：`exhausted` 是 sidecar 由窗口算（任一窗口 `remaining_percent ≤ 0` → true；有窗口且都 >0 → false；無窗口 → None）。Gateway 的 `cooldown`／`available_again_at` 是另一種語意（撞牆登錄），不要混進 `exhausted` |
| G-8 | 查不到的 provider 回 null | `opencode-zen`、`opencode-go`：`status: not_applicable`，其餘六鍵 null（`lgwd/usage.py:102-104`；#85 change 把情境改為 zen／go：`openspec/changes/openrouter-usage-fetcher/specs/usage-reporting/spec.md:25-27`，原樣「**WHEN** list 含 `opencode-zen` 與 `opencode-go`」）。**Ollama 不是查不到**：已有 `ollama-api-balance`（`usage_ollama.py:30`）；工單文字「Ollama 回 null」已過時 | **已有**（語意：`not_applicable`／`unknown` 都對應 sidecar 的 `usage: null`；`unknown` 時 `reason` 可顯示） |
| G-9 | 只有數字與時刻，不含 key 值 | spec「用量不外洩秘密」（spec 第 363-378 行）；`lgwd/usage.py:10` 「key／token 不進回應、log、例外訊息」；`usage_openrouter.py:7` 「`limit_remaining` 等其他欄位不進 usage」 | **已有** |
| G-10 | 解析失敗不影響主功能 | `usage_for` 內任何例外回 `unknown／internal`（`lgwd/usage.py:218-220`）；resolve 另有 `_safe_usage`（`registry.py:385-391`） | **已有** |
| G-11 | 每 10 分鐘呼叫一次 `list` 就能刷新 | 取用量由「被查到且快取過期」才在背景觸發，且**本次回覆仍回舊值**：`lgwd/usage.py:215-216` 先 `build_usage(...)` 再 `_maybe_trigger(...)`（「不等取用量」見 `usage.py:4` docstring 與 215-216）。新值要下一次查詢才看到（spec 第 132 行「下一次查詢才看到新值」） | **已有**，但產生輪詢時序問題，見 §3 |

小計：已有 6（G-1、G-5、G-6、G-8、G-9、G-10）；名稱不同 3（G-2、G-3、G-4）；缺 1 且不補（G-3 的 `remaining`／`unit`）；Gateway 不該做 1（G-7）；G-11 是用法提醒。

## 2. 給 MyLinuxPool 的對照表（sidecar 轉接層）

資料來源：list 每一筆的 `usage`（或 resolve `meta.usage`），以 `subscription` id 為鍵。

| sidecar（#552 §3） | 取自 Gateway `usage` | 規則 |
|---|---|---|
| `subscription` | list 項的 `id`／resolve `meta.subscription` | 原樣 |
| `observed_at` | `usage.fetched_at` | RFC 3339 → epoch；null → 該訂閱 `usage` 為 null |
| `source` | `usage.source` | 原樣，可加前綴如 `gateway:` 以免與 sidecar 本地來源同名 |
| `windows[].used_percent` | `100 - window.remaining_percent` | 保留小數，不取整 |
| `windows[].resets_at` | `window.resets_at` | RFC 3339 → epoch |
| `windows[].remaining`／`unit` | —— | 一律 None |
| `windows[].name` | 依 `source` 查表（下） | 查不到的 source：原樣用 `session`／`weekly` |
| `scope`／`applies`（#552 §3 r3） | —— | Gateway 沒有，等同 None |
| `exhausted` | 任一窗口 `remaining_percent ≤ 0` | 有窗口且都 >0 → False；兩窗口皆 null → None |
| 整個 `usage` 為 null | `status ∈ {not_applicable, unknown}` 且 `fetched_at` null | `unknown` 但 `fetched_at` 有值（含 `stale_expired`）→ 無窗口，`usage` 可為 None，`reason` 可顯示 |
| 15 分鐘過期 | `now - fetched_at > 15 分鐘` | **只用 `fetched_at` 自算**；不用 `usage.stale`（那是 Gateway 的 TTL，OpenRouter 只有 2 分鐘） |

顯示時自行四捨五入（`100 - 99.9` 會是 `0.09999999999999432`）；`exhausted` 用 `remaining_percent <= 0` 的原值判斷，不用算出的 `used_percent`。

過了 `resets_at` 的窗口，Gateway 端直接變 null（`lgwd/usage.py:129`）；OpenRouter 過 UTC 00:00 會變 `unknown／stale_expired` 且 `fetched_at` 有值，`exhausted` 因此從 true 變 None，這是預期（#552 §10 規則 3「不猜已恢復」）。#552 §10「Gateway 必須帶 observed_at」由 `fetched_at` 滿足（G-2）。

窗口名稱表（key＝`usage.source`）：

| source | `session` → | `weekly` → | 備註 |
|---|---|---|---|
| `openrouter-api-key` | `daily` | （恆為 null） | 僅 `:free` 模型的每日請求數（`usage_openrouter.py:9-15`）；不是整把 key 的可用量，顯示時不要說「額度」 |
| `claude-statusline` | `5h` | `1w` | Gateway 預設關閉（`usage_claude.py:170`）；若與 sidecar 本地 claude 值並存，以較新的 `observed_at` 為準（#552 §10） |
| `ollama-api-balance` | `session`（原樣） | `weekly`（原樣） | 窗口實際長度本 repo 未驗（見未驗） |

### 2.1 第 2 輪：管子對照（change 實作後；實作前 sidecar 照上表）

`usage.pipes[]` 每根 `{id, models, models_except, remaining_percent, resets_at}`。sidecar 的 `windows[]` 改由 `pipes` 產生：`name`＝`id`（`five_hour`／`seven_day`／`session`／`weekly`／`free_daily`／`credits`）、`used_percent`＝`100 - remaining_percent`、`remaining`／`unit` 仍 None。`exhausted` 改為**逐模型**：模型名符合某管子的 `models` 且不符合 `models_except`，該管子 `remaining_percent ≤ 0` → 該模型用完；沒有管子屬於該模型 → None。`session`／`weekly` 仍在，舊的轉接層不必立刻改。

| source | 管子 id（個數） |
|---|---|
| `claude-statusline` | `five_hour`、`seven_day`（2） |
| `ollama-api-balance` | `session`、`weekly`（2；新方案只有金額＝0） |
| `openrouter-api-key` | `free_daily`（1）＋`credits`（僅 `limit` 有上限時，共 2） |
| zen／go／antigravity | 0（`pipes: []`） |
| antigravity | Gateway 0 根；sidecar 自取，形狀待對齊（J-9），組名未驗 |

## 3. 輪詢節奏（G-5／G-11）

1. #552 §10 寫「有 agent 時每 10 分鐘 `list` 一次」。每次 list 都回「上一輪取到的值」，同時觸發背景重取；所以收到的 `fetched_at` 約等於**上一次輪詢的時刻**，年齡 ≈ 10 分鐘，離 15 分鐘只剩 5 分鐘。取用量失敗一次（退避從 TTL 起算、連續失敗加倍，`lgwd/usage.py:269-275`）就超過 15 分鐘 → sidecar 依自己的 15 分鐘標過期，這是對的。
2. 若要「每次輪詢都盡量新」，二擇一（MyLinuxPool／本人決定）：(a) 輪詢縮到 5 分鐘（資料年齡 ≈ 5 分鐘，餘裕 10 分鐘）；(b) 每次 list 後固定 5～10 秒補讀一次（同一端點）。(b) 的成本：每輪多一次 list；Gateway 不會多打上游，因為補讀時年齡 < TTL 不觸發重取。**不要**用 `usage.stale` 當補讀條件：OpenRouter／Ollama TTL 120 秒，10 分鐘輪詢下 `stale` 恆為 true，沒有鑑別力。`stale` 只是 Gateway 的 TTL 訊號，sidecar 不使用它。Gateway 側不為此改（改成「同步等取用量」違反 spec「不等取用量」與 `usage.py` 開頭原則）。
3. 第一次 list（lgwd 剛啟動或首次被查）：`unknown／not_fetched_yet`，約幾秒後再讀即有值。

## 4. 判斷點

### 已回答（本人 2026-11-01，經 PMO）
J-1＝A（sidecar 吸收名稱差異）；J-2＝A（百分比就夠，不回絕對值）；J-3＝A（sidecar 算 `exhausted`）；J-4 交 MyLinuxPool 選（B1 輪詢 5 分鐘／B2 補讀一次；不選 C）。

### 新的本人判斷點（第 2 輪）
| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| J-5 | 形狀怎麼擴？ | A：`usage` 加 `pipes`，保留 `session`／`weekly`（鍵集 7→8）；B：改成 `windows` 陣列並移除舊鍵；C：每個 provider 專屬鍵 | **A**（只加不換；B 破壞 sidecar 現讀，C 形狀隨 provider 增長）。**但要知道：實際受益只有 OpenRouter credits 一根**；若覺得為一根新增通用鍵太大，可選 D：本 change 縮成「只加 credits 專用鍵」或暫不做（取捨：D 形狀不通用，日後 agy 若要進 Gateway 還要再改） |
| J-6 | Antigravity 的用量誰取？ | A：sidecar（Mac）取，Gateway 不碰；B：Gateway 側另起機制（推送／別台主機） | **A**（`providers.py:159-165`；`/usage` 只能在 Mac 跑）。B 是另一張 c |
| J-7 | OpenRouter credits 無上限（`limit` null）怎麼表示？ | A：不出 `credits`；B：另加 `unlimited` 旗標 | **A**（最小） |
| J-9 | 兩邊（Gateway pipe 與 sidecar `UsageWindow`）要不要共用一個管子形狀？ | A：共用、兩邊對齊（讀者只有一套 model→桶比對；代價：sidecar 與 Gateway 都要改，且 agy 的 `scope`／`applies` 要併進來）；B：各自一套、sidecar 轉接（代價：兩套比對邏輯，但互不牽動） | **B**（現在只有一根 credits，不值得為它動 sidecar 的 `UsageWindow`；等 agy 也要進 Gateway 時再議）。**請本人／MyLinuxPool 決定** |
| J-10 | 要不要在本 change 就讓 resolve／`available_again_at` 看 `pipes`？ | A：不看，另開單讓 sidecar 消費（之前 credits 用完無效果）；B：本 change 讓 resolve 對屬於選中模型的管子用完時排除／標示 | **A**（B 會改選 key 的語意：「某組用完≠整把不能用」，且需要逐模型判斷，範圍變成 c） |

### 作者提議、本人未指示
- 管子「只有讀到才出現」、不放 null 佔位；`resets_at` 可為 null（credits 恆 null，`limit_reset` 語意未驗，之後另開單）。
- model→管子的比對在**讀者端**做；限制某模型的是 `remaining_percent` 最小的屬於它的管子。
- `available_again_at` 不看 `pipes`（見 J-10）。
- `models_except` 欄位（**目前只有 credits 用**）。
- credits 異常值只丟棄該管子，不影響 `free_daily`／`status`／退避。
- `pipes` 由 windows＋固定表在 `build_usage` 產生（比 compat 欄位小，一份資料）。
- 已刪（審查 W-3）：`window_seconds`、政策檔覆寫 pipes（J-8／C-11 舊）；後者「之後再議」。
- `openrouter-usage-fetcher` 尚未歸檔，是本 change 的前置。

## 5. 跨 repo（MyLinuxPool，PM 經 PMO 轉）

1. 實作 §2 轉接層：以 `list` 為來源；`get` 與 `roles` 沒有 `usage`。
2. 過期只用 `fetched_at`＋15 分鐘；不使用 `usage.stale`。
3. 窗口名稱依 `source` 查表；OpenRouter 的 `daily` 只代表 `:free` 請求數。
4. 輪詢節奏（§3）由 MyLinuxPool 決定 J-4。
5. 與本機 claude 來源重疊時（`claude-statusline`），以 `observed_at` 較新者為準，等時 `exhausted=true` 優先（#552 §10 規則 1、2）。
6. Gateway 回 `not_applicable`／`unknown` 不要當錯誤；Zen、Go 為 `not_applicable`，`reason` 是固定碼，可顯示。
7. **（第 2 輪）接 `pipes`**：change 實作後 `usage` 多一個 `pipes` 鍵；可先不動，繼續讀 `session`／`weekly`，之後再改讀 `pipes`。
8. **（第 2 輪）逐模型 `exhausted`**：用 resolve 的 `meta.model`（或 list 的 `models.allow`）對 `pipes[].models`／`models_except` 做 fnmatch；沒有管子屬於該模型＝None。
9. **（第 2 輪）Antigravity 由 sidecar 在 Mac 取 `/usage`**；是否與 Gateway 的 pipe 形狀對齊待決（J-9）。sidecar 現有 `UsageWindow` 與 agy 群組設定是它自己的一套。
10. **（第 2 輪）消費 `pipes` 要另開單**（J-10）：在那之前 credits 用完對挑 key 沒有效果。審查者讀 sidecar 程式：它不驗 `usage` 鍵集（加第 8 鍵不會壞；其測試是否斷言鍵集未驗）。

## 6. 給實作的契約

見 `openspec/changes/usage-multi-pipes/tasks.md`（C-1～C-12 與測試策略）。

## 未驗

- （第 2 輪）OpenRouter `/api/v1/key` 的 `limit`、`limit_reset` 欄位語意、Claude statusline 是否另有依模型分的管子、agy `/usage` 的組名與輸出、sidecar 測試是否斷言 usage 鍵集：皆未驗（審查者讀 sidecar 程式，不驗鍵集）；本輪未打外部 API、未跑測試。

- 未打任何外部 API、未讀憑證；上游欄位語意（OpenRouter `free_model_daily_requests`、Ollama `included.session`）只引用既有程式註解與 spec。
- Ollama `session`／`weekly` 的實際長度（是否 5h／7d）本 repo 未驗。
- 取用量時序（§3）依程式閱讀推論，未實跑。
- 未跑測試；`openspec validate openrouter-usage-fetcher` 輸出：`Change 'openrouter-usage-fetcher' is valid`（此為既有 change；本設計未新增 change，故無新 change 可驗）。
- 只讀了 MyLinuxPool#552 設計的 §2、§3、§7、§10，其餘章節未對照。
