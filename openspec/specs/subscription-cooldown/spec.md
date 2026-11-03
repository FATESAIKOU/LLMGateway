# subscription-cooldown Specification

## Purpose
定義 LLMGateway 的登錄冷卻介面（`POST /v1/subscriptions/{id}/cooldown`），包含 `until` 與 `model` 參數驗證、整把與模型兩層獨立冷卻、重複登錄規則與自動解除。

> v5（2026-10-12）：冷卻 until-only（`登錄冷卻`以 until-only 語意為準）。
## Requirements
### Requirement: 登錄冷卻
`POST /v1/subscriptions/{id}/cooldown {until[, model]}` SHALL 登錄冷卻；成功 MUST 回 204（無 body）。

#### Scenario: 帶恢復時刻
- **WHEN** 以 `until=15:00` 登錄 `claude-personal`
- **THEN** 它的冷卻到 15:00，list 顯示 `available` 為 false

### Requirement: 登錄冷卻的 until 必填與格式
`until` MUST 是必填的 RFC 3339 UTC 字串（何時能再用）；缺少 MUST 回 400 `bad_request`（`missing` 含 `until`）；`until` 不是 RFC 3339 字串（或型別不對）時 MUST 回 400 `invalid_until`，不登錄。

#### Scenario: 缺 until
- **WHEN** 登錄冷卻的 body 沒有 `until`
- **THEN** 回 400 `bad_request`，`missing` 含 `until`，冷卻不變

#### Scenario: 登錄冷卻：時刻格式不對
- **WHEN** `until` 不是 RFC 3339 字串
- **THEN** 回 400 `invalid_until`，冷卻不變

### Requirement: 登錄冷卻的 until 範圍
`until` 在過去或超過 8 天以後 MUST 回 400 `until_out_of_range`，不登錄（模型冷卻用同一個上限）。

#### Scenario: 不合理的時刻
- **WHEN** `until` 是過去的時刻
- **THEN** 回 400 `until_out_of_range`，冷卻不變

### Requirement: 登錄冷卻的 model 選填與 null
`model` 選填：有給時 MUST 只冷卻那個模型，沒有時冷卻整把（現行語意）；`"model": null` MUST 視為沒給（記整把），與空字串／數字／陣列不是同一類（後三者是「有給但形狀錯」）。

#### Scenario: 只冷卻一個模型
- **WHEN** 以 `{"until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"}` 登錄 `opencode-zen`
- **THEN** 回 204；`list` 的 `cooldown` 為 null、`model_cooldowns` 有 `mimo-v2.6-flash-free`；以 `model=big-pickle` resolve 同一把回 200，以同一個模型 resolve 也回 200（附冷卻資訊）

#### Scenario: model 是 null
- **WHEN** 以 `{"until": "2026-10-11T15:00:00Z", "model": null}` 登錄 `opencode-zen`
- **THEN** 回 204，且記的是整把冷卻（`cooldown` 有值、`model_cooldowns` 為 `{}`）——`null` 等於沒給

### Requirement: 登錄冷卻的 model 必須是供應商 id
`model` 有給（且不是 null）時 MUST 是非空字串，而且是供應商自己的模型 id（不帶 agent 專用前綴，語意同 resolve 的 `model`）。

#### Scenario: model 形狀不對
- **WHEN** body 的 `model` 是空字串、數字或陣列
- **THEN** 回 400 `bad_request`，冷卻不變

### Requirement: 登錄冷卻的 model 被擋時回 400
`model` 不是非空字串、長度超過 **200 字元**、以 `<agent>/` 開頭（agent ∈ `AGENTS`，即 `opencode/`、`claude-code/`、`agy/`；這個形狀 MUST 被擋，因為 allow 的 `*` 會匹配 `/`，只靠 allow 涵蓋檢查擋不到）、含 fnmatch 萬用字元（`*`、`?`、`[`），或不被該把 `models.allow` 的樣式（fnmatch）涵蓋，MUST 回 400 `bad_request`（**不是** 403 `model_not_allowed`——那是 resolve 的政策拒絕碼），不登錄。

#### Scenario: model 不在 allow
- **WHEN** 以 `model=muse-spark-1.3` 登錄 `opencode-zen`（只 allow `*-free` 與 `big-pickle`）
- **THEN** 回 400 `bad_request`（不是 403），冷卻不變

#### Scenario: model 帶 agent 前綴
- **WHEN** 以 `model=opencode/big-pickle` 登錄 `opencode-zen`
- **THEN** 回 400 `bad_request`（Gateway 不正規化前綴，與 resolve 同一條規則）

#### Scenario: model 帶 agy 前綴
- **WHEN** 以 `model=agy/gemini-3.8-flash-low` 登錄 `antigravity`（`allow` 只有該裸 id）
- **THEN** 回 400 `bad_request`，冷卻不變——`agy/` 由前綴規則擋下，不只看 allow 涵蓋

#### Scenario: model 帶前綴而且被 allow 樣式涵蓋
- **WHEN** 以 `model=opencode/glm-5.3-flash-free` 登錄 `opencode-zen`（allow 是 `["*-free", "big-pickle"]`，而 fnmatch 的 `*` 會匹配 `/`，所以它「被涵蓋」）
- **THEN** 回 400 `bad_request`，冷卻不變——前綴規則必須自己擋下這一種，allow 涵蓋檢查擋不到

#### Scenario: model 太長
- **WHEN** 以 201 個字元的 `model` 登錄 `claude-personal`（allow 是 `["*"]`），以及以 200 個字元的 `model` 登錄同一把
- **THEN** 前者回 400 `bad_request`、冷卻不變；後者回 204

#### Scenario: model 是萬用字元
- **WHEN** 以 `model=*-free` 登錄 `opencode-zen`
- **THEN** 回 400 `bad_request`，冷卻不變（它不是供應商 id，存進去永遠命不中）

### Requirement: 登錄冷卻不正規化 model
Gateway MUST NOT 正規化 `model`（不去前綴、不改大小寫）。

#### Scenario: model 大小寫不符
- **WHEN** 以 `model=Big-Pickle` 登錄 `opencode-zen`（allow 的樣式是 `big-pickle`，`_covered` 用大小寫敏感的 fnmatch）
- **THEN** 回 400 `bad_request`，冷卻不變（Gateway 不做大小寫正規化）

### Requirement: 登錄冷卻的驗證順序
驗證順序 MUST 與現行程式一致、不重排：未知 subscription id → 404 `not_found`，**先於** `until`／`model` 的 400；缺 `until` → 400 `bad_request`；`until` 格式／範圍 → 400；最後才是 `model`（null 除外，見上）→ 400。

#### Scenario: 登錄冷卻的驗證順序：未知的 subscription 與壞的 model
- **WHEN** 以 `model=opencode/big-pickle`（形狀會 400）登錄一個不存在的 subscription id
- **THEN** 回 404 `not_found`（查無先於形狀錯誤，與現行程式一致）

#### Scenario: 壞的 until 與壞的 model
- **WHEN** 以不是 RFC 3339 的 `until` 搭配 `model=opencode/big-pickle` 登錄一個存在的 subscription
- **THEN** 回 400 `invalid_until`（`until` 的驗證先於 `model`），冷卻不變

### Requirement: HTTP 層先於端點的驗證
body 解析失敗、body 不是物件、超過 1 MB 的 413 `body_too_large`，仍 MUST 由 HTTP 層先回（它們不是這個端點的 service 層驗證，所以「404 先於 400」不涵蓋它們）。這個順序與 `registry-errors`「錯誤回覆」的通則（輸入形狀 400 先於查無 404）不同，是刻意沿用現況（404 是現行程式的第一道檢查）。

#### Scenario: HTTP 層先於端點的驗證：body 超過 1 MB
- **WHEN** 登錄冷卻的 body 超過 1 MB
- **THEN** 回 413 `body_too_large`，由 HTTP 層先回，冷卻不變

### Requirement: 同一（把、模型）重複登錄取較晚
同一（把、模型）重複登錄時 MUST 取較晚的 `until`。

#### Scenario: 同一模型重複登錄取較晚的
- **WHEN** 同一（把、模型）先登錄 15:00、再登錄 14:00、再登錄 16:00
- **THEN** 第二次（較早）不覆蓋、第三次（較晚）覆蓋；稽核記的有效 `until` 依序是 15:00、15:00、16:00

### Requirement: 整把與模型兩層獨立
整把與模型兩層獨立，登錄其中一層 MUST NOT 刪除或縮短另一層既有的冷卻。

#### Scenario: 兩個模型的冷卻互不影響
- **WHEN** 同一把先登錄模型 A 到 15:00，再登錄模型 B 到 16:00
- **THEN** 兩筆都在 `model_cooldowns`，登錄 B 不改 A 的 `until`

#### Scenario: 整把與模型並存
- **WHEN** 某把已有模型 A 的冷卻，再登錄整把的冷卻
- **THEN** 兩層都在（`cooldown` 與 `model_cooldowns` 的 A）；登錄整把 MUST NOT 刪掉模型 A 的列

### Requirement: 未知欄位忽略
未知欄位 MUST 忽略。

#### Scenario: 未知欄位不影響登錄
- **WHEN** 登錄冷卻的 body 帶有未知欄位（例如 `foo`）
- **THEN** 回 204，未知欄位不影響 `until` 與 `model` 的結果

### Requirement: 冷卻自動解除
過了 `until` MUST 自動解除，不需要另一個動作（整把與每個模型各自解除）。

#### Scenario: 過期自動解除
- **WHEN** 某把的整把冷卻與某模型的冷卻都過了 `until`
- **THEN** list 的 `cooldown` 為 null，`model_cooldowns` 不再有那一筆

### Requirement: 判讀不出恢復時間的預設
呼叫方拿不到恢復時間時 SHALL 以呼叫當下＋1 小時為 `until`（約定；`lgw.signals` 只負責把判讀結果轉成這個形狀）。

#### Scenario: 判讀不出恢復時間
- **WHEN** 呼叫方判讀不出恢復時間
- **THEN** 以呼叫當下＋1 小時為 `until` 登錄

### Requirement: 冷卻只是提示
冷卻只是提示：登錄成功 MUST NOT 讓之後的 resolve 排除這把或這個模型（見 `resolve`「resolve 的選把規則」）；本端點 MUST NOT 提供提前解除。

#### Scenario: 冷卻不擋下一次 resolve
- **WHEN** 先以 `model=big-pickle` 登錄 `opencode-zen` 的冷卻，再以同一個模型 resolve
- **THEN** 回 200，`env` 照常，`meta.model_cooldowns` 有那一筆（冷卻只記錄、不拒絕）

### Requirement: local-login 訂閱也可登錄冷卻
`kind` 為 `local-login` 的訂閱 MUST 可以用（它沒有用量來源，冷卻常是唯一的訊號）。

#### Scenario: 冷卻仍可用
- **WHEN** 對 `antigravity` 登錄整把冷卻到未來
- **THEN** 回 204；list 的 `available` 為 false、`cooldown.until` 照實，`available_again_at` 為該時刻；resolve 仍回 200 並附冷卻資訊（冷卻不擋選把）

