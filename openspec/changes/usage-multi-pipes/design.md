## 1. 判斷：現行形狀表達不了

- 主 spec「usage 物件的形狀」：欄位**固定**七個、「沒有多餘鍵」（`openspec/specs/usage-reporting/spec.md:9,28`）；用量只有 `session`／`weekly`（同檔第 16 行）。
- 程式：`lgwd/usage.py:33` `WINDOWS = ("session", "weekly")`；`build_usage` 只迴圈這兩名（`usage.py:122-127`）；`FetchResult.windows` 的鍵就是這兩名（`usage.py:49-51` docstring）。三個 fetcher 都把上游硬塞進這兩格：Claude `_FIELDS = {five_hour→session, seven_day→weekly}`（`usage_claude.py:76`）、Ollama 取 `included.session／weekly`（`usage_ollama.py:56-57`）、OpenRouter 的免費日請求數硬放 `session`（`usage_openrouter.py:105`）。
- 因此：同一把 key 的第 3 根管子（OpenRouter credits）放不進去（`usage_openrouter.py:6-7` 刻意丟掉）；Antigravity 的三組各自有 session／weekly 類的窗口時，更是「組」×「窗口」，兩格表達不了。**結論：不能。**

## 2. 最小改法：加一個鍵 `pipes`

`usage` 變八鍵：原七鍵不動，加 `pipes`。

```
"pipes": [
  {"id": "five_hour", "models": ["*"], "models_except": [],
   "remaining_percent": 76.5, "resets_at": "2026-11-01T05:00:00Z"},
  ...
]
```

| 欄位 | 型別 | 意義 |
|---|---|---|
| `id` | 字串 | 管子的穩定識別，同一 subscription 內唯一；由 Gateway 程式內的固定表決定，**不來自上游回應原文** |
| `models` | 字串陣列 | 這根管子管哪些模型：fnmatch 樣式，比對 resolve 的 `meta.model`／政策檔 `models.allow` 用的同一種模型名。`["*"]`＝整把 key |
| `models_except` | 字串陣列 | 從 `models` 排除的樣式；無則 `[]`。**目前只有 OpenRouter credits 使用**（credits 不管 `:free` 模型；否則「同模型取 percent 最小」會讓 credits 0% 誤擋 `:free` 模型） |
| `remaining_percent` | 數字 | 0～100，原始小數，不用 0 代替讀不到 |
| `resets_at` | RFC 3339 UTC 或 null | 重置時刻；`credits` 恆為 null（`limit_reset` 語意未驗，之後另開單） |

（審查曾指出 `window_seconds` 沒有讀者，已刪；要用時再加鍵。）

規則：
- 管子**只有讀到才出現**，不放 null 佔位；沒有管子＝`[]`。
- `resets_at` 已過的管子從陣列拿掉；`resets_at` 為 null 的管子依 6 小時規則（`EXPIRE_AFTER`）一起消失。
- `status: ok`＝`session`／`weekly`／`pipes` 至少一項有值。
- `pipes` 內不放絕對數量、金額、key。
- credits 異常值（負數、大於 100%、型別不對、`limit` 非正數）**只丟棄 credits 這根**，不影響 `free_daily`、`status`、退避（不走 `failure(PARSE_ERROR)`）。

### 產生方式（N-1：採用較小的做法）

`FetchResult.windows` 仍是 `{"session","weekly"}` 加一個選填的第三項 `"credits"`（`(percent, None)`）。`pipes` 在 `build_usage` 由 windows 加上 `usage.py` 內的固定表產生：

```
PIPE_TABLE = {source: ((window_key, id, models, models_except), ...)}
```

`session`／`weekly` 照舊由 windows 導出，所以**只有一份資料**，沒有 compat 間接層，也不會分歧；`pipes` 與 `session`／`weekly` 共用同一個 `resets_at` 過期判斷。代價：固定表在 `usage.py`，新增 source 要在表裡加一列。

### 相容層（取捨）

- **保留 `session`／`weekly`**：sidecar 已在讀。代價：Claude／Ollama／OpenRouter 的這幾根在 `pipes` 與 `session`／`weekly` 各出現一次（值恆等）。
- `available_again_at` **不改**，仍只看 `session`／`weekly`。**目前讀者（sidecar）只讀 `session`／`weekly` 與 `available_again_at`、不讀 `pipes`**（審查者讀其程式所得），所以 credits 用完時 key 仍會被挑給付費模型——**在另開單讓 sidecar 消費 `pipes` 之前，這個缺口存在**。是否在本 change 就讓 resolve／`available_again_at` 看 `pipes`，列為 J-10。
- 日後若沒有讀者靠 `session`／`weekly`，另開 change 移除。

## 3. sidecar／resolve 怎麼知道某個 model 該看哪根管子

- 比對：模型名 `m` 屬於管子 `p` ⇔ `m` 符合 `p.models` 任一樣式，且不符合 `p.models_except` 任一樣式。
- 一個模型可屬於多根管子（例：Claude 的 `five_hour` 與 `seven_day` 都是 `["*"]`）。**限制該模型的是 `remaining_percent` 最小的那根**；任一屬於它的管子 `remaining_percent ≤ 0`（用原始數值）→ 該模型用完，可用時間＝那些 `≤ 0` 管子 `resets_at` 的最晚者（resets_at 為 null 的管子沒有重置時刻，則回復時刻未知）。
- 沒有任何管子屬於該模型＝這個模型沒有用量資訊（不是用完，也不是沒用完）。
- **匹配在讀者端（sidecar）做**，Gateway 不新增 per-model 輸出：resolve 的 `meta` 已同時有 `model` 與 `usage`（`lgwd/registry.py:262,269`），list 的每一筆有 `models.allow` 與 `usage`，讀者自己比對即可。這樣不新增欄位、不新增端點。

## 4. 管子的名稱與模型群從哪裡來

- `id` 與 `models` 寫死在 `usage.py` 的固定表（進版控），不取自上游回應原文：上游標籤可能改字，且有回顯上游內容的風險（同 `reason` MUST NOT 回顯上游內容）。
- 政策檔覆寫 `models`（原 J-8）：**之後再議**，沒有使用者，本 change 不做。
- 預設 `models` 與 `models.allow` 靠樣式對應；漏接的模型落入「沒有任何管子屬於它」（安全方向：不誤報用完）。

## 5. 現有 source 的對照表（新形狀下各有幾根）

| source | 管子（`id`） | `models` | `models_except` | percent 來源 | `resets_at` | `session`/`weekly` |
|---|---|---|---|---|---|---|
| `claude-statusline` | `five_hour` | `["*"]` | `[]` | `100 - rate_limits.five_hour.used_percentage`（`usage_claude.py:128`） | `resets_at`（epoch 秒） | `session` |
| 〃 | `seven_day` | `["*"]` | `[]` | `100 - rate_limits.seven_day.used_percentage` | 同上 | `weekly` |
| `ollama-api-balance` | `session` | `["*"]` | `[]` | `included.session.remaining_percent`（`usage_ollama.py:56-65`） | `included.session.resets_at` | `session` |
| 〃 | `weekly` | `["*"]` | `[]` | `included.weekly.remaining_percent` | `included.weekly.resets_at` | `weekly` |
| `openrouter-api-key` | `free_daily` | `["*:free","openrouter/free"]` | `[]` | `free_model_daily_requests.remaining / limit * 100` | 下一個 UTC 00:00 | `session`（維持現狀） |
| 〃 | `credits` | `["*"]` | `["*:free","openrouter/free"]` | `limit_remaining / limit * 100`，**僅當** `limit` 為正數、`limit_remaining` 為 0～`limit` 的數字 | **恆為 null** | 無 |

合計：Claude 2 根、Ollama 2 根（新方案只有金額＝0 根＝現行 `no_windows`）、OpenRouter 最多 2 根、zen／go／antigravity 0 根（`not_applicable`，`pipes: []`）。

**OpenRouter credits**（J-2＝百分比）：
- 有上限（`limit` 為正數）→ 一根管子，percent＝`limit_remaining / limit * 100`。異常值（負數、大於 `limit`、型別不對）→ **只丟棄 credits**，不影響 `free_daily`／`status`／退避。
- 無上限（`limit` 為 null／`limit_remaining` 為 null，官方「null＝無限」，`usage_openrouter.py:23`）→ **不出這根管子**（沒有「無限」的百分比）。讀者看不到 `credits` 就是「沒有上限可算」，不是「用完」。不另設 `unlimited` 旗標（最小；要的話是 J-7）。
- `resets_at`：**恆為 null**。`limit_reset` 的語意未驗，待驗證後另開單再算下一個 UTC 邊界。
- 現行 `usage_openrouter.py:6-7`「`limit_remaining` 不進 usage」的決定被本 change 取代（改成百分比進 `pipes`，絕對值仍不進）。

## 6. Antigravity（agy）：本 change 不滿足

- **本 change 實際新增的管子只有 OpenRouter credits；agy 三組不在 Gateway。**
- Gateway 沒有 usage fetcher：antigravity 是 local-login，`providers.py:159-165` 明寫「MUST NOT 覆寫 make_usage_fetcher」；`usage` 一直是 `not_applicable`。`/usage` 只能在 Mac 跑，Gateway 主機跑不了，也不該取得 Google 登入。
- sidecar 的 `UsageWindow`（name／used_percent／scope／applies；MyLinuxPool#552 設計 §3）與這裡的 pipe 形狀不同，其 agy 群組對應是機器設定。**兩邊是否共用一個管子形狀：待對齊（J-9）**，本 change 不承諾。
- 若日後要 Gateway 側顯示 agy 用量，需要新機制（sidecar 推送或另一台主機上的 fetcher），屬另一張 c。

## 7. 決定與替代方案

| 方案 | 取捨 |
|---|---|
| **A（採用）加 `pipes`，保留 `session`／`weekly`** | 只加不換；舊讀者零影響；代價是資料重複與鍵集 7→8 |
| B 把 `session`／`weekly` 改成 `windows` 陣列、移除舊鍵 | 破壞 sidecar 現讀；違反「只能加」 |
| C 每個 source 各加一個專屬鍵（`credits`、`gemini`…） | 鍵集隨 provider 增長，形狀不再固定，讀者要為每個 provider 寫分支 |
| D `pipes` 用物件（id→管子）而非陣列 | 可，但管子需帶 `models` 比對、順序有意義（呈現）時陣列較自然；不採用 |
| E Gateway 直接輸出「每個 allow 模型的剩餘」 | 要新增 per-model 欄位且政策檔 allow 含萬用字元（`*`）無法展開；匹配交給讀者較小 |

## 8. 風險與未決

- 鍵集 7→8：審查者讀 sidecar 程式，它不驗鍵集（其測試是否斷言未驗）。
- OpenRouter `limit_reset` 與 `limit` 欄位、Claude 是否另有依模型分的管子（例：Opus 專屬週額度）本 repo 未驗；`limit_remaining` 可否為負（推論）；若 Claude statusline 之後新增這類欄位，屬於新管子（加 `id`）而非改形狀——這正是用陣列的理由。
- 每個 fetcher 的管子 `id` 是對外契約，一旦出貨不可改名（可新增）。
