## REMOVED Requirements

### Requirement: 過渡期的 get 不擋
**Reason**: LLMGateway#48 已移除單筆 get（change remove-subscription-get 已歸檔），此過渡期規定不再適用。
**Migration**: 改用 resolve（若要指定 subscription 則帶 `subscription=<id>`）；單筆 get 端點已移除（一律回 404）。

## MODIFIED Requirements

### Requirement: roles 為 advisory
`roles` 為 advisory：Gateway MUST NOT 代選，`order` MUST NOT 限制 `resolve` 的釘住，也 MUST NOT 讓釘住繞過隱私——`resolve` 帶 `data` 時 MUST 依 `subscription-registry` 的隱私步驟擋下 `privacy` 不足的候選（含釘住的那把）。

#### Scenario: 帶 data 的 resolve 仍擋釘住
- **WHEN** `roles.impl.order` 含 `privacy: public` 的 `opencode-zen`，以 `agent=opencode&provider=opencode-zen&subscription=opencode-zen&data=real` resolve
- **THEN** 回 403 `privacy_not_allowed`——advisory 的 `order` 不讓釘住繞過隱私
