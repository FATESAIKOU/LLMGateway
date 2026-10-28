# tasks：角色×訂閱分配表（#97；實作要等本人核准）

## 1. 政策

- [x] 1.1 `lgwd/policy.py`：`SubscriptionPolicy` 加 `privacy`（缺省 `public`）；`Policy` 加 `roles`；`_parse_roles` fail-closed，驗證順序 `privacy` → 訂閱 id → 角色鍵 → `floor`（必填、合法，無預設）→ `order`（非空、不重複、id 存在）；角色物件有 `floor`／`order` 以外的鍵即拒收。驗證：`tests/test_policy.py`（含 `floor` 缺漏、部分角色表）。
- [x] 1.2 `policy/default.json`：加各訂閱 `privacy` 與 `roles`（內容見 design 的提議表，等本人核定）。groq 保留但不進任何 `order`。驗證：出廠政策可載入；`real` 等級的候選不含 zen／openrouter／groq。

## 2. 端點與呈現

- [x] 2.1 `lgwd/registry.py`：`role_candidates(role, agent, data)`，回 `{id, provider, rank, privacy, available, cooldown, model_cooldowns}`，不含 key、`usage`、`available_again_at`，不觸發取用量；`rank` 篩選後連續。驗證：`tests/test_registry.py`（spec 各 scenario，含「隱私不足時不降級」與其對照）。
- [x] 2.2 `lgwd/http.py`：加 `GET /v1/roles/{role}`；`agent` 非法 400、角色未知／不在 `roles` 段 404；query 解析規則同 `resolve`，已知參數集合加上 `data`（另立含 `data` 的集合，不共用 `_RESOLVE_PARAMS`）。驗證：`tests/test_http.py`。
- [x] 2.3 `lgw/client.py`：`roles(role, agent, data=None)`。驗證：`tests/test_client.py`。
- [x] 2.4 `lgwctl/cli.py`：新增 `usage` 子命令，`list`＋`roles` 依 id 合併排版（`not_applicable` 標「無額度 API」）。驗證：`lgwctl` 對應測試。

## 3. 文件與對端

- [x] 3.1 `docs/model-rotation.md` 開頭加一段「與訂閱分配的分工」；MyPMO handbook 改為引用 Gateway 的 `roles`（由 PMO 另行修改）。
- [x] 3.2 寫給 MyLinuxPool 的請求書（對照 design §3.3 (a)–(g)，含 200 但冷卻在未來的分支（#93 需求 9；判讀見 design §3.2.5，驗證靠 spec 的 Gateway 側 scenario）、「不得繞過 `roles` 釘低等級訂閱」、404 fallback、`usage` 0% 規則），經 PMO 轉。
- [x] 3.3 `openspec validate role-subscription-allocation --strict`，核准後 archive。

## 4. 本人決定後才開的後續單（不在本 change）

- [ ] 4.1 `resolve` 加選填 `data`（design §2.1 第二層；判斷點 6）。
- [ ] 4.2 政策 `models.known` 以展開萬用字元（判斷點 7）。
