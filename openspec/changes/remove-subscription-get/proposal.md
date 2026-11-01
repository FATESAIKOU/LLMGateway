# remove-subscription-get

對應工單：LLMGateway#48（層級 d）。依據：#33 的設計 `openspec/changes/archive/2026-10-24-agent-provider-model-interface/`（`tasks.md` 4.1、`design.md` OQ 4）——終態介面只有 list、resolve、登錄冷卻（roles 另見 role-allocation）。

## Why

`GET /v1/subscriptions/{id}?agent=` 是過渡期介面，為的是讓 sidecar 從舊的「取 key」搬到 resolve。開單條件已全部滿足：稽核 get＝0（#48 留言）、sidecar 已不呼叫 get（MyLinuxPool#541、#348 已關）。留著只會讓一個回傳憑證值的端點繼續存在。

## What Changes

- 移除 Gateway 端 `GET /v1/subscriptions/{id}?agent=` 的路由與 `RegistryService.get_subscription`；該路徑現在一律 404 `not_found`，不進 service、不寫稽核、不回任何值。
- 移除 `lgw.client` 的 `get`。
- 主 spec `subscription-registry` 的「get」Requirement 移除（REMOVED），並把其他 Requirement 內對 get 的敘述改為不再提 get（MODIFIED：共通 JSON schema、稽核紀錄、錯誤回覆、resolve、沒有憑證的訂閱）。
- 主 spec `connectivity` 的「客戶端自備通道」（MODIFIED：打 list／get／登錄冷卻 改為 list／resolve／登錄冷卻）。
- 主 spec `key-vault` 的四條 Requirement（MODIFIED：拉取與輪替、值不外洩、不做應用層認證、key 只保管在一處）：把「get 回新值」「get 回 409」等敘述改為透過 resolve 的 `env`，過渡期 get 的敘述刪除；「服務不對外」沒有 get，不在 delta 內。
- 介面文件（`docs/deploy.md`）改為終態：list、resolve、登錄冷卻。

## What Does Not Change

- list、resolve、登錄冷卻、roles 的行為與回覆形狀不變；`schema`、`env_schema` 不 bump。
- 稽核 `action='get'` 的歷史列不刪、不遷移（`lgwd/store.py` 的 schema 不動）。
- 歷史文件（`docs/research/`、`docs/lessons/`、`openspec/changes/archive/`）不改。
- 不歸檔本 change；不改版本號、不打 tag、不部署（PM 另寫計畫，經本人授權）。

## Impact

- 破壞性：任何仍呼叫 `GET /v1/subscriptions/{id}?agent=` 的呼叫方會拿到 404。依據上面的稽核（get＝0）與 sidecar 遷移，目前沒有已知呼叫方；部署前由 PM 再確認。
- 測試：`tests/test_http.py`、`test_registry.py`、`test_client.py`、`test_main.py` 的 get 測試移除；新增「單筆路徑回 404、不寫稽核、不含 key」與「client 沒有 get」的斷言。
