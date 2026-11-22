## RENAMED Requirements

- FROM: `### Requirement: 不做應用層認證`
- TO: `### Requirement: 應用層認證依模式`

## MODIFIED Requirements

### Requirement: 應用層認證依模式
服務 SHALL 依 spec `resolve-auth` 的認證模式決定是否要求應用層憑證：只有 off 不要求，能連到服務的就能 list、resolve、登錄冷卻；warn 驗證並記錄但保留舊結果；enforce MUST 拒絕未簽章或未授權的業務請求。SSH 仍是第一層：服務只綁 127.0.0.1，只能經會合點的 SSH 轉發連入（gateway-v0 決定 5a，經 #222 本人核准後修訂）。發現訂閱 key 外洩時，以更新加密檔並等一次拉取因應，客戶端不持有訂閱 key、不用改。客戶端裝置遺失時，先由 MyLinuxPool 撤掉那把 sshproxy 金鑰並在名冊撤銷其簽章金鑰，再更新加密檔等一次拉取。

#### Scenario: 經會合點連入
- **WHEN** off 模式下持有會合點 SSH 金鑰的客戶端經轉發呼叫 list
- **THEN** 服務回 200，不要求任何應用層憑證

#### Scenario: enforce 下只有 SSH 不夠
- **WHEN** enforce 模式下客戶端經會合點轉發、未帶簽章或用已撤銷的金鑰呼叫 list 或 resolve
- **THEN** 服務回 401，不回訂閱清單或憑證

#### Scenario: 換 key
- **WHEN** 本人更新 private repo 的加密檔並等一次拉取
- **THEN** 之後的 resolve 回的 `env` 是新值，客戶端的設定不用改

#### Scenario: 裝置遺失
- **WHEN** 客戶端裝置遺失，其 sshproxy 金鑰已撤、簽章金鑰已在名冊標為撤銷
- **THEN** enforce 下持有該裝置簽章金鑰的請求回 401
