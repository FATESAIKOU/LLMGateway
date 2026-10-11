## REMOVED Requirements

### Requirement: 租約事件是權威紀錄
**Reason**: 本人 2026-10-11 夜的裁定：不再有租約，LLMGateway 只有 list、get、登錄冷卻。
**Migration**: 沒有租約；誰在何時拿了哪一把 key 由 get 的稽核紀錄回答。

### Requirement: agent 端回報帶租約 id
**Reason**: 本人 2026-10-11 夜的裁定：不再有租約，LLMGateway 只有 list、get、登錄冷卻。
**Migration**: 沒有租約 id；v0 不做 token／成本的計量。

### Requirement: 紀錄不含憑證與工作內容
**Reason**: 本人 2026-10-11 夜的裁定：不再有租約，LLMGateway 只有 list、get、登錄冷卻。
**Migration**: 同樣的要求移到 `key-vault`「值不外洩」與 `subscription-registry`「稽核紀錄」。

### Requirement: 查詢與彙總
**Reason**: 本人 2026-10-11 夜的裁定：不再有租約，LLMGateway 只有 list、get、登錄冷卻。
**Migration**: 改由 list 呈現每把的冷卻與能不能用；用量彙總不在 v0。
