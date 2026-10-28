對應工單：#117（層級 d）「resolve 加選填 data」；設計來源：LLMGateway#102 的設計草稿（本人已裁定**缺省 B**，其餘 8 項照草稿 §6 的建議）；上層：#100 的 role-allocation（已由 #115 歸檔）。

## Why

`resolve` 是發憑證的兩個入口之一，但只看 provider／agent／model／exclude，**不看訂閱的 `privacy`**（`lgwd/registry.py` 的 `_pick`）。`roles` 端點會依 `privacy` 篩候選，但釘住與 `get` 都不擋，所以舊 sidecar、有 bug 的挑法、任何能打 loopback 的 client，都能釘住 `privacy: public` 的那把拿到憑證去送真資料。

第一層（`role-subscription-allocation`）的設計已寫明這是限度：Gateway 不擋釘住。第二層只解一件事：**呼叫方宣告了資料等級時，在發憑證的邊界被擋**，不讓隱私等級只靠 sidecar 自律。

## What Changes

- `GET /v1/resolve` 加選填 `data=<public|general|real>`：值不在三者之內 → 400 `bad_request`；有給時候選限於 `privacy` 不低於它，一個都不剩 → 403 `privacy_not_allowed`（新碼）。**沒給 `data` 完全不看 privacy**，行為與現在相同（缺省 B）。
- 隱私步驟插在選把的第 3 步（agent）與第 4 步（model）之間（編號沿用 3.5）：agent 的 403 先於 privacy 的 403，privacy 的 403 先於 model 的 403；也先於步驟 5 `exclude` 的 409 `no_candidate` 與步驟 6 的 409 `not_deployed`。隱私不參與 priority／id 排序。
- `resolve` 的 `meta` 加 `data`（請求值原樣，沒給時 `null`；不是「生效等級」——resolve 沒有 role／floor，與 `roles` 回覆的 `data` 語意不同）。只新增欄位，`schema` 不 bump。
- `lgwd/errors.py` 加 `privacy_not_allowed()`（403）與碼表一行；`lgw/client.py` 的 `resolve` 尾巴加 `data: str | None = None`（`is not None` 才進 query）。
- spec：`subscription-registry` 的「resolve」「resolve 的選把規則」「錯誤回覆」用 MODIFIED 加 `data` 與上面每一條；`role-allocation` 的「角色分配表」與「角色候選端點」同步兩句（roles 的 advisory 敘述、`data` 進 `_RESOLVE_PARAMS` 後不再另立集合），兩份 spec 不互相矛盾。
- 測試：`tests/test_registry.py`、`tests/test_http.py`、`tests/test_client.py` 補 `data` 的案例；`roles` 與 `resolve` 的一致性（roles 回覆的每個 candidate 用回覆的 `data` 釘住 resolve 不得被隱私擋）釘住「resolve 不會比 roles 更寬」。

## What Does Not Change

- **不做** `get` 的過濾（它同樣發憑證，但它是過渡期介面、由 #48 移除；spec 註明洞仍在）。
- **不記** 稽核的 `data`（稽核只記成功的 resolve，失敗的 403 不記；B 缺省下沒帶 `data` 的呼叫本來就記不出等級）。稽核規格不動。
- **不做** `models.known`／判斷點 7（草稿 §5 只列問題與選項，本人裁定不做）：`roles` 不帶已知模型全集，`models.allow` 的萬用字元照舊不展開。
- 不加 `role=`（resolve 沒有 floor、不代選；選把鍵仍是 `priority`）。
- 不動 `get`、`list`、冷卻、`env` 的鍵集與 `env_schema`；不改 `lgwctl`、`docs/`、`policy/`（出廠政策檔的 `privacy` 已足夠）。
- 不翻缺省成 `real`：那是後續的 d（等 MLP 都帶 `data` 之後另開）。

## Impact

- 舊呼叫方（不帶 `data`）行為完全不變，任一順序上線都安全；帶了 `data` 的呼叫才可能第一次收到 403 `privacy_not_allowed`。
- 新的錯誤碼 `privacy_not_allowed`（403）：消費端不認得時照 403 處理即可；`lgw.client` 把它列進碼表說明。
- `meta` 多一個鍵：只讀精確鍵集的消費端測試要同步（本 repo 的 `tests/test_registry.py` 已改）；`schema` 不 bump。
- 失敗的 403 不進稽核（規格本來如此），所以被擋的企圖只留 HTTP log（不含請求值）。
