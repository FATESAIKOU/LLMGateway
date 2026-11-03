# registry-errors Specification

## Purpose
定義 LLMGateway 的共通錯誤回覆格式、HTTP 狀態碼表、錯誤優先順序規則，以及登錄冷卻與退場錯誤處理。

## Requirements
### Requirement: 錯誤回覆

所有錯誤 SHALL 以 `{"error": "<碼>"}` 的 JSON 回覆，訊息不得回顯請求裡的值；resolve 的錯誤回覆 MUST NOT 含 `env`。

#### Scenario: 錯誤 body 只有 error
- **WHEN** resolve 回任何錯誤
- **THEN** body 只有 `error` 鍵，沒有 `env`、沒有 `usage`、沒有 `until`

### Requirement: 400 bad_request 的輸入形狀
`400 bad_request` SHALL 用於 JSON 解析失敗、body 不是物件、缺必填（帶 `missing: [...]`）；resolve 的輸入形狀錯（缺 `agent`／`provider`、`agent` 不是 `claude-code`／`opencode`／`agy`、`data` 有給但不在 `public`／`general`／`real` 之內、`subscription` 與 `provider` 不一致、`subscription` 與 `exclude` 同時給、同一 query 參數重複）。

#### Scenario: 缺必填
- **WHEN** 登錄冷卻的 body 沒有 `until`
- **THEN** 回 400 `bad_request`，`missing` 含 `until`

#### Scenario: 未知 agent 是輸入錯誤
- **WHEN** resolve 的 `agent` 不是 `claude-code`、`opencode` 或 `agy`
- **THEN** 回 400 `bad_request`

### Requirement: 400 bad_request 的登錄冷卻 model 形狀
登錄冷卻的 `model` 不是非空字串（`null` 除外——那是「沒給」）、超過 200 字元、以 `<agent>/` 開頭（`opencode/`、`claude-code/`、`agy/`）、含萬用字元，或不被該把 `models.allow` 涵蓋時，MUST 回 400 `bad_request`。

#### Scenario: 登錄冷卻的 model 被政策擋下
- **WHEN** 以不被該把 `models.allow` 涵蓋的 `model` 登錄冷卻
- **THEN** 回 400 `bad_request`（不是 403 `model_not_allowed`），冷卻不變

### Requirement: 400 invalid_until 與 until_out_of_range
`until` 不是 RFC 3339 字串（或型別不對）MUST 回 400 `invalid_until`；`until` 在過去或超過 8 天以後 MUST 回 400 `until_out_of_range`。

#### Scenario: 400 invalid_until：時刻格式不對
- **WHEN** `until` 不是 RFC 3339 字串
- **THEN** 回 400 `invalid_until`，冷卻不變

#### Scenario: 過去的 until
- **WHEN** `until` 是過去的時刻
- **THEN** 回 400 `until_out_of_range`，冷卻不變

### Requirement: 400 no_model
resolve 的 `model` 省略且沒有候選能推導模型時 MUST 回 400 `no_model`（不代選）。

#### Scenario: 推不出模型回 400
- **WHEN** resolve 省略 `model`，且所有候選都推不出模型
- **THEN** 回 400 `no_model`，不代選任何模型

### Requirement: 403 政策拒絕碼
resolve 的政策拒絕 SHALL 回下列 403 之一：`agent_not_allowed`（`agent` 不在該筆的 `agents` 裡，選把步驟 3）、`model_not_allowed`（`model` 不被任何候選的 `models.allow` 涵蓋）、`privacy_not_allowed`（宣告的 `data` 高於所有候選的 `privacy`，步驟 3.5；`data` 不在三個等級是 400，不是這個碼）。

#### Scenario: 模型被政策擋下
- **WHEN** resolve 的 model 不在候選的 `models.allow`
- **THEN** 回 403 `model_not_allowed`，不含 `env`，不代選其他模型

#### Scenario: 隱私被政策擋下
- **WHEN** resolve 宣告的 `data` 高於所有候選的 `privacy`
- **THEN** 回 403 `privacy_not_allowed`，body 只有 `error`（沒有 `env`、沒有 `usage`、沒有 `until`），不代選其他訂閱

### Requirement: 404 與 409 not_deployed
未知路徑，或不存在的 subscription id（resolve 的未知 provider／未知 `subscription` 同用）MUST 回 404 `not_found`。key 尚未部署 MUST 回 409 `not_deployed`（`kind` 為 `local-login` 的訂閱沒有憑證，但 resolve 不會因它回 409）。

#### Scenario: 404 與 409 not_deployed：未知 provider
- **WHEN** 以 `provider=NoSuchProvider` resolve
- **THEN** 回 404 `not_found`

#### Scenario: 沒有憑證的訂閱不是 not_deployed 的錯誤
- **WHEN** 以 `agent=agy&provider=google` resolve `antigravity`
- **THEN** 回 200（不是 409 `not_deployed`）

### Requirement: 409 no_candidate
resolve 的 `exclude` 排除後沒有候選（沒有恢復時刻）MUST 回 409 `no_candidate`；`exclude` 後沒有候選 MUST NOT 選回被排除的那把。

#### Scenario: 排除後沒有候選
- **WHEN** resolve 的 `exclude` 排除了最後一個可用候選
- **THEN** 回 409 `no_candidate`，不含 `env`，不選回被排除的那把

### Requirement: 413 與 500 碼
body 超過 1 MB MUST 回 413 `body_too_large`；伺服器內部非預期錯誤 MUST 回 500 `internal`（不帶細節）。

#### Scenario: 413 與 500 碼：body 超過 1 MB
- **WHEN** 任一端點的 body 超過 1 MB
- **THEN** 回 413 `body_too_large`

#### Scenario: 內部非預期錯誤
- **WHEN** 伺服器發生非預期的內部錯誤
- **THEN** 回 500 `internal`，回覆不帶細節

### Requirement: 不回 409 cooling
新 Gateway MUST NOT 回 409 `cooling`：冷卻不是錯誤（見 `resolve`「resolve 的選把規則」），錯誤回覆也不再有 `until`。

#### Scenario: 候選全部冷卻中
- **WHEN** resolve 的所有候選都在冷卻中
- **THEN** 回 200（冷卻不是錯誤），`meta` 附 `cooldown`／`model_cooldowns`，不會出現 `error` 或 `until`

### Requirement: 舊版 cooling 的判讀保留
舊版 Gateway 仍可能回 409 `cooling`（body 帶 `until`）；`lgw.client` 與側車 MUST 保留對這個碼的判讀，直到所有 Gateway 都升到不回它的版本。

#### Scenario: 舊版 Gateway 回 cooling
- **WHEN** 客戶端收到舊版 Gateway 的 409 `cooling`（body 帶 `until`）
- **THEN** `lgw.client` 與側車仍照判讀，不把它當成未知錯誤

### Requirement: 客戶端的兩種判讀結果
客戶端（`lgw.client`）MUST 另外判讀兩種結果，不是服務回的碼：`unreachable`（連不上或逾時，呼叫方不會拿到例外）、`bad_response`（回覆不是 JSON 物件）。

#### Scenario: 連不上
- **WHEN** 客戶端連不上服務
- **THEN** 結果的 `error` 為 `unreachable`，呼叫方沒有拿到例外

### Requirement: 錯誤的優先序
多個錯誤同時成立時 SHALL 回優先序最前者：輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403，與 400 `no_model`）先於候選狀態（409 `no_candidate`／`not_deployed`）——與 `resolve`「resolve 的選把規則」的步驟同一軸。

#### Scenario: 輸入形狀錯誤先於查無
- **WHEN** 以 `provider=NoSuchProvider&subscription=claude-personal` resolve（`claude-personal` 存在、其 provider 是 `anthropic`，與請求的 `provider` 不一致；`NoSuchProvider` 也不是政策裡的 provider）
- **THEN** 回 400 `bad_request`（`subscription` 與 `provider` 不一致先於 provider 不存在的 404）

### Requirement: 政策拒絕的三個 403 的先後
政策拒絕的三個 403 之間 SHALL 是 `agent_not_allowed` 先於 `privacy_not_allowed` 先於 `model_not_allowed`；`privacy_not_allowed` 也 MUST 先於兩種 409。

#### Scenario: agent 與隱私同時不符
- **WHEN** 以 `agent=claude-code&provider=opencode-zen&subscription=opencode-zen&data=real` resolve（zen 不給 `claude-code`，且 `privacy` 是 `public`）
- **THEN** 回 403 `agent_not_allowed`（不是 `privacy_not_allowed`）

### Requirement: 登錄冷卻不在通則內
登錄冷卻不在這條通則內：它的順序 SHALL 沿用現行程式（查無 404 先於 `until`／`model` 的 400，見 `subscription-cooldown`「登錄冷卻」），因為那是既有行為、不是本 change 要改的語意。

#### Scenario: 登錄冷卻不在通則內：未知的 subscription 與缺 until
- **WHEN** 登錄一個不存在的 subscription id，body 沒有 `until`
- **THEN** 回 404 `not_found`（與「錯誤回覆」通則的「輸入形狀 400 先於查無 404」相反，是登錄冷卻的既有順序）

