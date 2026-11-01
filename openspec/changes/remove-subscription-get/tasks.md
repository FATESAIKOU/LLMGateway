# Tasks

## 1. Gateway 端移除 get
- [x] 1.1 `lgwd/http.py`：移除 `GET /v1/subscriptions/{id}?agent=` 的路由與 dispatch 分支；模組文件改為四個路由。
- [x] 1.2 `lgwd/registry.py`：移除 `get_subscription`；模組文件與 `_is_local_login` 的說明保留 local-login 的規則（resolve 與 list 仍用）。
- [x] 1.3 `lgwd/store.py`：只改文件，說明 `action='get'` 是歷史列；schema 與遷移不動。

## 2. Client 端移除 get
- [x] 2.1 `lgw/client.py`：移除 `Client.get` 與模組文件的 get 條目。

## 3. 測試
- [x] 3.1 刪除 get 的測試（`tests/test_http.py`、`test_registry.py`、`test_client.py`、`test_main.py`）；get 專用的 fixture 一併刪除。
- [x] 3.2 `ReloadTest` 的換 key 觀察改看 resolve 回的 `env`（`CLAUDE_CODE_OAUTH_TOKEN`），同樣經過請求前重讀。
- [x] 3.3 新增：單筆路徑回 404 `not_found`、不寫稽核、回覆不含 key（假 service 與真 RegistryService 兩層）。
- [x] 3.4 新增：`lgw.client.Client` 沒有 `get` 屬性。
- [x] 3.5 list／resolve／cooldown 既有測試照樣通過。

## 4. 規格與文件
- [x] 4.1 本 change 的 spec delta：`subscription-registry`（REMOVED「get」，MODIFIED 五個 Requirement 內對 get 的敘述）；`connectivity`（MODIFIED「客戶端自備通道」）；`key-vault`（MODIFIED 四個 Requirement：拉取與輪替、值不外洩、不做應用層認證、key 只保管在一處）。MODIFIED 都以主 spec 全文為底，只改 get 相關句子，scenario 逐條保留。
- [x] 4.2 `docs/deploy.md`：服務介面改為 list、resolve、登錄冷卻。
- [ ] 4.3 歸檔：**不在本 change 內做**（PM／本人決定時機）。主 spec 的 Purpose 段仍寫「get 是過渡期介面」，歸檔時一併更新。

## 5. 驗證
- [x] 5.1 `openspec validate remove-subscription-get`、`openspec validate --specs`。
- [x] 5.2 指定的測試檔全綠（見 .work/REPORT.md）。
- [x] 5.3 改壞驗證：在副本加回 get 路由，「404」測試轉紅（見 .work/REPORT.md）。
