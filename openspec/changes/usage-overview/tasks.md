# tasks：各訂閱用量查詢，補強現有 usage（#204）

契約編號 C-n 是各 PR 的測試要對得上的最小單位：測試名或註解寫 `C-n`，讓 spec、完成判準與測試可互查。順序由 PM 定，下面只寫依存。實作 PR 都不在本 change 內；本 change 只有文件。

## 0. P0：本 change（文件）

- [x] 0.1 `openspec/changes/usage-overview/`：proposal、design、delta specs（`specs/usage-reporting`、`specs/role-allocation`、`specs/subscription-registry`）、tasks。完成判準：`openspec validate usage-overview --strict` 與 `openspec validate --specs --strict` 的原樣輸出與 exit code 寫進報告；**模擬 archive 後 `openspec validate --specs --strict` 通過**（在副本上跑 `openspec archive usage-overview --yes`，再跑 validate，無 500 字警告）。無行為改變，無契約。
- [ ] 0.2 本人決定未決 1（OpenRouter `usage_weekly` 是否放寬「不回金額」）。經 PMO 轉達。在此之前 PR1～PR3 都不碰金額。
- [x] 0.3 MyLinuxPool sidecar 相容性：已確認相容。mylinuxpool-pm-w3 讀碼：usage 全程 dict `.get()`、不比對鍵數、不讀 `reason`（來源：team-mylinuxpool-w3 頻道 2026-11-15 回覆，由 PM 轉述，原回覆貼在 #204 留言）。MyPMO#386 尚未實作，不受影響。結論見 design §3、§3.1。
- [x] 0.4 `subscription-registry` 的 `list` 一條以 MODIFIED 改寫為九鍵（原文「七個鍵齊全」與現行八鍵不符）。`specs/subscription-registry/spec.md` 已在本 change 內，全文抄自現行 spec。PR1 不再改該 spec；archive 時套用。
- [x] 0.5 `role-allocation` 的「額度的顯示不新增端點」改為「額度的顯示與用量一覽」（RENAMED＋MODIFIED，`specs/role-allocation/spec.md`）。Purpose 不能用 delta 改，見 §4。

## 1. PR1：usage 九鍵、reason 細分、note（依存 0.3，已完成）

- [ ] 1.1 `lgwd/usage.py`：`_not_applicable(subscription)` 依固定表回 `reason`（`no_quota_api` 或 `unavailable`）與 `note`；`_unknown()` 與 `build_usage()` 補 `note: null`；`reason` 碼表加兩個新碼。
- [ ] 1.2 `lgwd/usage.py`：固定表（subscription id → reason、note）。未收錄的 `not_applicable` subscription 依 design D9 降級為 `reason: unavailable`＋固定通用 note＋一條 warning log（**每個 subscription id 只記一次**，模組層已記集合），**不啟動失敗**；`UsageService` 不新增參數，不做建構時檢查。`reason`／`note` 的查找順序：先查固定表（表內用專屬 note），查不到才用通用 note。
- [ ] 1.3 `lgwd/registry.py`：`meta.usage` 與 list 的 `usage` 共用同一個組裝函式，確保 resolve 也帶 `note`。
- [ ] 1.4 更新會壞的測試（清單來自 `grep -n "SHAPE\|not_applicable\|\"reason\": None"`，不是猜的）：
  - `tests/test_usage.py`：L24 `SHAPE` 與使用它的 L46、L264、L388–400、L574 改為九鍵；L148–152（整個 `usage` 字典相等，`reason: None`）改為 `reason: unavailable`＋通用 note。
  - `tests/test_registry.py`：L2412 `SHAPE` 與使用它的 L2450、L2600、L2619、L2626 改為九鍵；L2453–2455（`opencode-zen` 整個 `usage` 字典相等，`reason: None`，無 `note`）改為 `reason: unavailable`＋非空 `note`。
  - 其餘 `not_applicable` 斷言只看 `status`（`tests/test_http.py` L1074、L1083、L1177；`tests/test_registry.py` 其他行），不會壞。
  - `tests/test_codex.py` **不需改**：只逐鍵斷言 `status`、`source`、`session`、`weekly`，不斷言 `reason` 或整個 `usage` 字典。
- [ ] 1.5 （原「改寫 subscription-registry 的 list」已移到 0.4，PR1 不再處理。）
- [ ] 1.6 `docs/deploy.md` §5.7：列出 `reason` 碼與 `note`，校正 OpenRouter 的描述（見 design §3）。
- 契約：
  - C-1：`_not_applicable()`、`_unknown()`、`build_usage()` 回傳的 `usage` 鍵集恰為九個（`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`、`pipes`）；沒有多餘鍵。
  - C-2：`opencode-zen`、`opencode-go`、`antigravity`、`chatgpt-personal` 的 `usage` 為 `status: not_applicable`、`reason: unavailable`、`source: null`、`note` 為固定表的非空字串、`session`／`weekly` 為 null、`pipes` 為 `[]`。
  - C-2a（負向）：固定表沒有收錄的 `not_applicable` subscription（測試以假 subscription id 驗證）→ 程式照常啟動；該筆 `usage` 為 `reason: unavailable`、非空的通用 `note`，並記一條 warning（同一 id 只記一次：連續呼叫 list 十次，warning 只出現一次）；不會出現 `reason: null`。空的 `UsageService` 下：表內 id（`opencode-zen`）得專屬 `note`，表外 id（假 id，或 `groq`）得通用 `note`，同此規則（`tests/test_usage.py` L148 的情形）。
  - C-3：`note` 只在 `reason` 為 `no_quota_api`／`unavailable` 時非 null；以全部出廠訂閱掃描驗證；`note` 不含測試埋入的 canary（假上游回應 body 含 `canary-secret`）。
  - C-4（負向）：`status` 為 `ok` 或 `unknown` 時，`reason` 不得是 `no_quota_api` 或 `unavailable`。
  - C-4a：claude-personal 的 `upstream_error`、`disabled`、`not_fetched_yet` 行為與 PR1 之前逐值相等，`note` 為 null。
  - C-4b：resolve 的 `meta.usage` 與 list 同一把的 `usage` 逐值相等（同快取狀態下）。
  - C-4c：list 與 resolve 的非 `usage` 欄位與 PR1 之前的快照逐值相等。快照以 PR1 前的現況輸出手抄成常數（fixture），寫在測試檔內，不引用舊版程式。對應測試（PR1 新增）：`tests/test_registry.py::test_C4c_list與resolve非usage欄位逐值不變`。
- 完成判準：C-1～C-4c 綠；`openspec validate usage-overview --strict` 通過；`docs/deploy.md` §5.7 已更新（1.6）。
- 依存：0.3（已完成）。與 `codex-subscription` 無依存（見 design §3）。

## 2. PR2：`GET /v1/usage`（依存 PR1）

- [ ] 2.1 `lgwd/registry.py`：`list_usage_summary()`，只讀 `usage_for`／`_usage_fields` 的結果，取出十個鍵（不含 `pipes`）；順序依 priority。
- [ ] 2.2 `lgwd/http.py`：新增 `GET /v1/usage` 路由；檔頭路由表同步；非 GET 與其他路徑 404。
- [ ] 2.3 `lgw/client.py`：`usage()` → `GET /v1/usage`，回 `Result`，連線失敗行為與 `list()` 相同。（順手項，可刪；不在 PMO 範圍①～③，見 design §3。）
- [ ] 2.4 測試：`tests/test_http.py`、`tests/test_client.py`、`tests/test_usage.py`（新增）。
- 契約：
  - C-5：`GET /v1/usage` 回 `schema: 1` 與 `subscriptions`；每筆鍵恰為 `id`、`kind`、`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`；沒有 `pipes`。
  - C-6：同一快取狀態下，每筆的 `status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly` 與 list 同一筆的 `usage` 逐值相等；`/v1/usage` 的位元組數小於同一狀態下 list 的位元組數（不設絕對門檻）。
  - C-7：canary 掃描（憑證樣式、金額樣式、假 key 值）對 `/v1/usage` 回覆沒有命中；回覆不含字串 `pipes`、`credits`。
  - C-8：非 GET（`POST`、`PUT`、`DELETE`、`PATCH`）與其他路徑 → 404，且假 fetcher 呼叫次數為 0。
  - C-9：假取用量永遠不返回，`/v1/usage` 仍立刻回 200，該筆 `reason: not_fetched_yet`；單筆組裝拋例外 → 該筆 `reason: internal`、`note: null`，其他筆照常回 200。
  - C-10：觸發規則與 list 相同：快取過期且啟用且 key 已部署 → 觸發一次；快取新鮮 → 不觸發；key 沒部署 → 不觸發；`enabled: false` → 不觸發；單飛（連續 10 次只觸發一次）。
  - C-11：`Client.usage()` 在服務關閉時回 unreachable（與 `list()` 同），回 200 時 `data` 是原樣的回應 dict。
- 完成判準：C-5～C-11 綠；`lgwd` 的 list 與 resolve 測試（PR1 之後）維持綠；`openspec validate usage-overview --strict` 通過。
- 依存：PR1。

## 3. PR3：lgwctl 的呈現（依存 PR1；與 PR2 可並行）

- [ ] 3.1 `lgwctl/usage.py` `_usage_text()`：`not_applicable` 依 `reason` 分流——`no_quota_api` → 「無額度 API」；`unavailable` → 「無法取得」加上 `note`；其他（或缺值）→ 「未知（reason）」。一律不顯示百分比。同時更新檔頭註解「（七鍵）」。
- [ ] 3.2 `tests/test_lgwctl_usage.py` L33、L196 更新。
- 契約：
  - C-12：`chatgpt-personal`、`antigravity`、`opencode-zen`、`opencode-go` 的列顯示「無法取得」與 `note` 原文，且不含「無額度 API」與「100%」。
  - C-13：`no_quota_api` 的列（測試用假資料）顯示「無額度 API」，不含 `note` 的 unavailable 文字。
  - C-14：`ok`、`unknown` 的列與 PR1 之前逐字相同（回歸測試）。
- 完成判準：C-12～C-14 綠；`lgwctl usage` 的輸出只改 `not_applicable` 的兩種列，其他列逐字相同。
- 依存：PR1。

## 4. 不在任何 PR 內

- 部署：lgwd 升級到 Gateway 主機是真機操作，需經 PMO 取得本人單次授權後由 PM 執行，不在 PR 內。
- 每週快照：由 MyPMO#386 呼叫後自己存，Gateway 不存（不新增儲存）。
- 未決 2（opencode-zen 是否 `no_quota_api`）：需要實證或官方文件明載，實證後另開單，改固定表與 C-2。
- 未決 3（`/v1/usage` 認證）、未決 4（`available_again_at` 放進一覽）：依 design §5，另議。
- [ ] 4.1 **PR3 合併後** archive（負責人：PM）：`openspec archive usage-overview`，再手動改兩份 Purpose（`openspec/specs/role-allocation/spec.md`、`openspec/specs/usage-reporting/spec.md`，文字見 design §3.2；OpenSpec 的 delta 不能改 Purpose），最後跑 `openspec validate --specs --strict`。**archive 必須在 PR1＋PR3 之後**：主 spec `role-allocation` 會要求 lgwctl 對 `unavailable` 顯示「無法取得」，提前 archive 則主 spec 與程式不符。
- [ ] 4.2 PM 在 MyPMO#386 留言提醒：not_applicable 的 `reason` 現為固定碼，不可視為異常（不要用 `reason != null` 判斷告警；見 design §3.1）。負責人：PM。
