## MODIFIED Requirements

### Requirement: local-login 訂閱不進 key 檔
`kind` 為 `local-login` 的訂閱（`antigravity`、`chatgpt-personal`）MUST NOT 出現在 key 檔，MUST NOT 佔 `KEY_NAMES` 的鍵名；Gateway MUST NOT 為它拉取、保管或轉發任何值。`chatgpt-personal` 的 `auth.json` 含 refresh token，同樣 MUST NOT 進 key 檔。

#### Scenario: 沒有憑證的訂閱不進 key 檔
- **WHEN** 檢查 key 檔的鍵名與 `KEY_NAMES`
- **THEN** 沒有 `antigravity`、沒有 `chatgpt-personal`（也沒有任何 `local-login` 的訂閱）；它們的 resolve 仍回 200 與空 `env`，不讀 key 檔的值
