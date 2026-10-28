## MODIFIED Requirements

### Requirement: key 只保管在一處
key 的正本 SHALL 是 private repo `FATESAIKOU/LLMGateway-keys` 根目錄的 `keys.env.enc`（格式與加密方式見 `archive/2026-10-24-gateway-v0/design.md` 的 v5 變更；值由本人更新，AI 不經手值）。使用前提是服務那台以 `llmgw` 身分登入過 `gh`（`gh auth login`，憑證在該帳號家目錄；LLMGateway 不做登入引導、不碰憑證值）。LLMGateway SHALL 經由定期拉取（本檔「拉取與輪替」），把解密驗證過的內容寫到放服務那台、只有服務帳號讀得到的檔案（權限 0600）。拉取與解密過程中 MUST NOT 把值放在命令列或寫進 log。`kind` 為 `local-login` 的訂閱沒有憑證（本 change 的 `antigravity`：agy 用呼叫方那一台機器的本機登入，見 spec `subscription-registry`「沒有憑證的訂閱」）：它 MUST NOT 出現在 key 檔、MUST NOT 佔 `KEY_NAMES` 的鍵名，Gateway MUST NOT 為它拉取、保管或轉發任何值；它的「已部署」由 kind 判定，不由 key 檔判定。

#### Scenario: 部署
- **WHEN** 本人更新加密檔並等一次拉取
- **THEN** 那台的 key 檔更新，服務重讀後 get 回新值，log 裡不出現任何值

#### Scenario: 權限
- **WHEN** 檢查那台的 key 檔
- **THEN** 檔案權限是 0600，擁有者是服務帳號

#### Scenario: 沒有憑證的訂閱不進 key 檔
- **WHEN** 檢查 key 檔的鍵名與 `KEY_NAMES`
- **THEN** 沒有 `antigravity`（也沒有任何 `local-login` 的訂閱）；它的 resolve 仍回 200 與空 `env`，get 回 409 `not_deployed`，兩者都不讀 key 檔的值
