## RENAMED Requirements

- FROM: `### Requirement: 額度的顯示不新增端點`
- TO: `### Requirement: 額度的顯示與用量一覽`

## MODIFIED Requirements

### Requirement: 額度的顯示與用量一覽
額度資料 SHALL 由 `list` 與 `resolve` 的 `meta` 的 `usage` 提供（形狀與規則見 `usage-reporting`），並可由唯讀的 `GET /v1/usage` 取得精簡一覽（見 `usage-reporting`「用量一覽（GET /v1/usage）」）；`roles` 不提供額度。呈現層以 `id` 合併 `list` 與 `roles`。`usage.status` 為 `not_applicable` 的訂閱 MUST NOT 被呈現成「剩 100%」或任何數字；呈現層依 `usage.reason` 分流：`no_quota_api` MUST 標示「無額度 API」；`unavailable` MUST 標示「無法取得」並附 `usage.note`；其他 reason 或缺值 MUST 標示「未知」並附 reason。兩種 `not_applicable` 都可附 `cooldown`／`model_cooldowns`。呈現入口為 `lgwctl` 的 `usage` 子命令。

#### Scenario: zen 的額度欄
- **WHEN** 渲染 `opencode-zen`（`usage.status` 是 `not_applicable`，`usage.reason` 是 `unavailable`）
- **THEN** 額度欄顯示「無法取得」並附 `usage.note`，不顯示百分比，也不顯示「無額度 API」

#### Scenario: no_quota_api 的額度欄
- **WHEN** 渲染一筆 `usage.reason` 是 `no_quota_api` 的訂閱（出廠政策目前沒有，以假資料測）
- **THEN** 額度欄顯示「無額度 API」，不顯示百分比
