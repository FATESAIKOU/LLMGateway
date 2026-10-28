對應工單：#107（設計單；只寫介面，實作拆成 tasks 的 PR 1～3）。上層：MyPMO#4「啟動 LLMGateway」（依本 repo 的 AGENTS.md）；跨 repo 對應 MyLinuxPool#460（agy driver）與 #486（Mac 的 `POST /agents` 寫法調查）。

## Why

本人 2026-10-28 裁定：agy（Antigravity CLI）**只在 Mac 用**，由 Mac sidecar 開、用 Mac 本機登入；不搬、不匯出、不代管任何 OAuth 憑證；worker 不用 agy。Gateway 這一版只做三件事：把 `agy` 加進 agent 詞彙、登錄一筆「不需要 key」的訂閱、讓 resolve 對它回空 env。

現況做不到，而且卡在四個地方（引用皆為本 repo 現行檔案）：

- `lgwd/policy.py:20` 的 `AGENTS = ("claude-code", "opencode")`：`agent=agy` 在 resolve（`lgwd/registry.py:407`）與 roles（`:158`）都回 400 `bad_request`；`agents` 驗證（`lgwd/policy.py:112`）也不收 `"agy"`。
- 每個 subscription 都必須在 `lgwd/keys.py:16-24` 的 `KEY_NAMES` 有鍵名，否則 `get`／`deployed` 丟 `KeyFileError("unknown subscription")`（`lgwd/keys.py:81-85`）；resolve 步驟 6 又用「讀得到 key」當「已部署」（`lgwd/registry.py:224-228`）。
- `build_env` 對非 opencode 的 agent 一律走 claude-code 分支（`lgwd/providers.py:67-84`），會回 `ANTHROPIC_*`；agy 一個都用不到，sidecar 也會整批丟掉（OUT-research-486 §2.2）。
- sidecar 的 agy driver **只從 resolve 的 `meta.model` 拿模型**（`agy.py:440-450`，引在 OUT-research-486 §1.3），沒有模型就 400 `no_model`；所以 row 必須能推導模型，而 Gateway 目前沒有 agy 的 row。

## What Changes

- `AGENTS` 加 `agy`（三選一）；連帶改到的 400 路徑與 roles 見 design §1.1。登錄冷卻的 `<agent>/` 前綴因此多擋 `agy/`（行為收緊，spec 與測試一起改）。
- 新增一筆訂閱 `antigravity`（**作者提議**；id／provider 名是本人要選的）：`provider=google`、`kind=local-login`、`endpoint=null`、`agents=["agy"]`、`privacy=public`、`priority=35`、`models` 為一個具體 id。`agents` 仿 `claude-personal` 的條款式檢查，必須剛好 `["agy"]`，否則 `PolicyError`。
- 「不需 key」走**新 kind `local-login`**（設計比較見 design §2）：`deployed` 由 kind 判定，不讀 key 檔；`KEY_NAMES` 不加它；resolve 步驟 6 不因沒有 key 回 409；`get` 對它回 409 `not_deployed`（沒有可交付的值）。
- `build_env` 對 `agent=agy` 回 `{}`；模型只走 `meta.model`。`env_schema` **維持 1**（沒有新增任何 env 鍵；bump 會讓現在的 Mac sidecar 直接拒收）。
- spec delta：`subscription-registry` 新增「沒有憑證的訂閱」並 MODIFIED `get`、`共通 JSON schema`、`endpoint 的語意`、`登錄冷卻`、`錯誤回覆`、`resolve`、`resolve 的選把規則`、`env 的組成`；`role-allocation` MODIFIED `角色候選端點`；`key-vault` MODIFIED `key 只保管在一處`。
- 測試：出廠政策 6 筆 → 7 筆、list 順序、provider 三欄表、resolve／get／roles 的 agy 案例；`KEY_NAMES` 的集合**不變**（正好證明沒有新增鍵名）。

## What Does Not Change

- **本 change 不含實作**：不改 `lgwd`／`lgw`／`lgwctl`／`policy` 的任何程式或出廠政策；後續 PR 的內容只列在 `tasks.md`。
- 不代管、不匯出、不讀、不寫任何 OAuth 憑證；agy 不進 key 檔、不佔鍵名、`list`／`resolve`／`get` 都不會回它的任何值。
- 不改 `list` 的欄位、冷卻端點、`usage` 形狀、`env` 的既有鍵集、`schema`／`env_schema` 版本。
- 不改 roles 的程式與 `roles.order`（要不要納入 agy 是 design §4 的待裁定）；不改 `resolve` 的選把鍵（仍是 priority＋id）。
- 不寫部署計畫、不做 Mac sidecar／MyLinuxPool 的程式與 config（PMO 另寫；sidecar 的硬條件見 OUT-research-486 §3.1）。

## Impact

- 舊呼叫方行為不變；不帶 `data` 的 resolve 仍完全不看 `privacy`（`lgwd/registry.py:445-450`）。
- `provider=google` 今天回 404；登錄後對 `claude-code`／`opencode` 會變成 403 `agent_not_allowed`（同為錯誤，碼改變，消費端要能處理 403）。
- `list` 多一筆：`available` 為 true（沒有憑證但恆算已部署）、`usage.status` 為 `not_applicable`、`available_again_at` 只由冷卻算。
- Mac sidecar 目前只接受 `schema`／`env_schema` ≤ 1（OUT-research-486 §2.2），所以這一版**不能** bump 版本。
- ToS／資料條款仍有未定項（個人訂閱用於原版 agy 是否合規、方案的資料使用），所以 `privacy` 先建議 `public`，且它不帶 `data` 時不擋真資料——這兩點都要讓 PMO 知道（design §1.6）。
- delta 的 `resolve`／`resolve 的選把規則`／`錯誤回覆`／`role-allocation` 的「角色候選端點」以 #117 `resolve-data`（已完成、尚未歸檔）的文字為基底：**#117 要先歸檔**，否則本 change 的文字會少掉 `data`，或反過來被它覆蓋。
