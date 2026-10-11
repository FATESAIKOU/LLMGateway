## REMOVED Requirements

### Requirement: 租約欄位
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: 改由 `subscription-registry` 的共通 JSON schema 描述一個 subscription。

### Requirement: 依政策核准
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: 改由 sidecar 依 list 的 `agents`、`available`、`priority` 自己挑；條款限制由 get 的 `agent` 檢查擋住。

### Requirement: 依政策選定訂閱
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: 挑選邏輯移到 sidecar 內部（MyAiStorage v3）；LLMGateway 只提供 `priority` 與 `available`。

### Requirement: 租約狀態與事件
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: 沒有租約狀態；get 與登錄冷卻寫進稽核紀錄（`subscription-registry`「稽核紀錄」）。

### Requirement: 回報暫時失敗時保存後補送
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: 沒有需要補送的租約回報；sidecar 連不上 LLMGateway 時照「等一下再試」處理。

### Requirement: 憑證由 launcher 注入
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: sidecar 用 get 拿到 key，由它的 AgentDriver 推出環境變數並交給子行程（MyAiStorage v3）。

### Requirement: 訂閱切換不得安靜發生
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: 換把在 sidecar 內部：偵測到用完 → 登錄冷卻 → 從 list 挑下一把 → get → 重開；冷卻的登錄與每次 get 都留紀錄。

### Requirement: Mac 與 worker 走同一套
**Reason**: 本人 2026-10-11 夜的裁定：LLMGateway 對外只有 list、get、登錄冷卻三個介面，不再有租約。
**Migration**: Mac 跑同一個 sidecar，用同一組三個介面。
