對應工單：LLMGateway#162（層級 d）「role-allocation 主 spec 清掉 get 退場後的殘留」；相關 #48、#156。

## Why

#48（get 退場）的 openspec delta 只涵蓋 subscription-registry、key-vault、connectivity，沒有涵蓋 role-allocation。所以歸檔之後，`openspec/specs/role-allocation/spec.md` 還殘留：
- 第 72 行 Requirement「roles 為 advisory」的 Scenario「帶 data 的 resolve 仍擋釘住」THEN 結尾「（`get` 是另一回事，見 `subscription-registry`）」；
- 第 81-87 行 Requirement「過渡期的 get 不擋」＋ Scenario「過渡期呼叫 get」；
- 第 4 行 Purpose 的「不擋 `resolve`／`get` 的釘住」（Purpose 不在 requirement 內，delta 管不到）。

## What Changes

- `openspec/specs/role-allocation/spec.md` 的 Requirement「過渡期的 get 不擋」：REMOVED，Reason 註明 #48 已移除單筆 get，Migration 註明改用 resolve。
- `openspec/specs/role-allocation/spec.md` 的 Requirement「roles 為 advisory」：MODIFIED，完整保留主 spec 規範，僅移除 Scenario「帶 data 的 resolve 仍擋釘住」THEN 結尾括號內的「（`get` 是另一回事，見 `subscription-registry`）」。
- Purpose（第 4 行）：openspec 的 delta 沒有 Purpose 的語法，歸檔時由 PM 在歸檔 commit 手改，把「不擋 `resolve`／`get` 的釘住」改成「不擋 `resolve` 的釘住」（刪掉斜線與 `get` 兩側的反引號），前例 2d92f53（#48／#147：Purpose 手改成終態）。
- 不改 `lgwd/`；只改 spec 文字。
- 不歸檔此 change（兩階段流程：第一個 PR 只提 change，第二階段由 PM 歸檔並同步主 spec）。

## What Does Not Change

- 不改對外介面、不改模組界線、不改行為；get 端點本來就不存在（一律 404）。
- 不改其他 Requirement 與 Scenario；原有的其他 Requirement 與 Scenario 全部保留。

## Impact

- 主 spec `role-allocation` 不再把 get 當成現存介面或過渡期介面。
- 測試：本 change 不改 `lgwd/`，不新增測試。
