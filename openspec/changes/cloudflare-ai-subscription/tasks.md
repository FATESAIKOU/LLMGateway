# tasks：cloudflare-ai 的登錄（#89）

> 前置：本人裁定 `design.md`「本人的判斷點」。以下以建議案寫；裁定不同則改任務再開工。實作另開 PR，本 change 的 PR 只含設計。

## 1. 設計與裁定

- [ ] 1.1 本人裁定判斷點 J1～J5（`design.md`）。驗證：issue #89 留言或 PMO 轉達。
- [ ] 1.2 `openspec validate cloudflare-ai-subscription --strict` 綠。

## 2. 政策白名單與檢查

- [ ] 2.1 `lgwd/policy.py`：`SUBSCRIPTIONS`、`_OPENCODE_ONLY` 加 `cloudflare-ai`。驗證：`tests/test_policy.py`（`agents` 不是剛好 `["opencode"]` 都拒收；順序含 cloudflare-ai）。
- [ ] 2.2 `policy/default.json`：priority 28、`agents` `["opencode"]`、`allow` 逐字列免費可用模型、`default` `@cf/openai/gpt-oss-120b`。驗證：`tests/test_policy.py` 釘住 default 與 allow；allow 不含萬用字元；八個付費模型都不被涵蓋。

## 3. provider 與 key

- [ ] 3.1 `lgwd/providers.py`：`CloudflareAIProvider`（樣板 endpoint、`opencode_provider_id`＝J2 查證後的值）；`validate_endpoints` 仍過。驗證：`tests/test_registry.py`（list／get 的 endpoint 是樣板且不含 account id；resolve 的 `meta.endpoint` 已置換；`env` 鍵集不變；`usage` 為 `not_applicable`）。
- [ ] 3.2 `lgwd/keys.py`：`CLOUDFLARE_API_KEY`＋附屬值 `CLOUDFLARE_ACCOUNT_ID`；任一缺或空＝未部署；account id 格式驗證（32 位十六進位，不合法視同格式錯誤，訊息只帶行號與種類）。驗證：`tests/test_keys.py`（canary 不進 repr／錯誤訊息／log）。
- [ ] 3.3 `ops-scripts/key-sync` 與 `tests/test_keysync.py`：鍵名白名單同步。驗證：既有 keysync 測試加兩個鍵。

## 4. spec 與測試收尾

- [ ] 4.1 更新列舉 subscription 的既有測試（`test_registry.py`、`test_policy.py`、`test_keys.py`、`test_keysync.py`、`test_http.py`、`test_main.py`）。驗證：該六檔 pytest（前景，不跑全套）。
- [ ] 4.2 canary：假 account id 不出現在 list／get 回覆、稽核、資料庫、log；只出現在 resolve 的 `meta.endpoint`。
- [ ] 4.3 改壞驗證（副本 `.work/mut/`）：(a) 拿掉 `_OPENCODE_ONLY` 的 `cloudflare-ai`；(b) 置換不做；(c) 缺 account id 仍當作已部署。各自轉紅。
- [ ] 4.4 非作者審查通過；key 與 account id 到位前不部署（部署與 key 檔由本人）。
