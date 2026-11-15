# design：各訂閱用量查詢，補強現有 usage（#204）

對應工單 #204；上層 MyPMO#386。spec 是規格本體（`specs/usage-reporting/spec.md`）；本文件只寫決定、為什麼、被放棄的選項，與介面草案。

寫作約定：「事實」指repo 內可讀到的程式、spec 或 `docs/research/usage-data-sources.md` 的查證文字；「提議」指本 change 的決定，本人未指示。本次**沒有**讀任何憑證檔、本機登入檔，也沒有執行 opencode CLI 或 agy CLI；官方文件只用 research 文件已引用的查證結果，沒有重新抓取。

## 0. 現況（事實）

- `lgwd/usage.py`：`_not_applicable()` 固定回 `reason: null`（約 L120–122）；`_unknown(source, reason)` 保留 source（L125）；`PIPE_TABLE` 只有三個 source（L51–64）；`available_again_at()` 只看 `cooldown.until` 與 `session`／`weekly`（L174）。
- `lgwd/registry.py`：`list_subscriptions()` 逐筆合入 `usage` 與 `available_again_at`（L131–144，L375–384）；`_safe_usage()` 在 resolve 組 `meta.usage`（L361–367）。
- `lgwd/http.py`：只有四條路由（檔頭 L4–8），其他一律 404；沒有應用層認證，存取控制靠 SSH（檔頭 L9）。
- `lgwctl/usage.py`：`_usage_text()` 把 `not_applicable` 一律顯示成「無額度 API」（不分原因）；它只在 Gateway 主機上跑（`lgw/client.py` 連 `127.0.0.1`）。
- `lgw/client.py`：只有 `list`、`resolve`、`roles`、`cooldown` 四個方法；讀取不做鍵集檢查。
- 政策檔 `policy/default.json`：`usage` 段只有 `claude-personal: {enabled: true}`。
- 出廠訂閱（政策檔）：claude-personal、opencode-zen、opencode-go、openrouter、ollama-cloud、antigravity、chatgpt-personal。`chatgpt-personal` 已在 `policy/default.json`（第 60 行起，commit `f72092d`，#187 P4 加入）；它的 `codex-subscription` change 與本 change 對它的 `not_applicable` 分類一致。
- `subscription-registry`「schema 版本不因新增欄位而 bump」：list 與 resolve 的 `schema` 目前都是 1（`lgwd/registry.py` L59、L63）。
- 歷史決定：`openspec/changes/archive/2026-10-28-role-subscription-allocation/design.md` §4 寫過「不加 `/v1/usage`」，理由是用 `lgwctl` 子命令讀。本 change 推翻這一條，理由見 D1。

## 1. 要解的問題（事實＋提議）

讀的人有三種：本人（在 Gateway 主機上用 `lgwctl`）、AI 隊員（用 HTTP，經 SSH 或內網）、MyPMO#386 的週報（每週一次，需要窗口與讀取時間，要自己存快照）。現在只有第一種有順手的讀法。第二、三種要拿整個 list 再自己挑 `usage`，而 list 的每筆還帶 `cooldown`、`model_cooldowns`、`pipes`，回覆很大。

另外，四個 `not_applicable` 訂閱的「為什麼拿不到」沒有說明；lgwctl 把它們都顯示成「無額度 API」，對 antigravity、chatgpt-personal 這種「有額度，只是 Gateway 讀不到」的情況是錯的。

## D1. 介面形式：新增 `GET /v1/usage`（提議）

比較三個方案加一個被放棄的方案：

| 方案 | 內容 | 結論 |
|---|---|---|
| A. 只加 CLI | `lgwctl usage --json`，從 list 過濾 | **放棄**。仍只在 Gateway 主機上跑，AI 與 PMO 讀不到；仍要拉整個 list。 |
| A0. 零改動基準線 | 呼叫方自己 `GET /v1/subscriptions`，再挑 `usage` 與 `id` | **放棄**。不改任何端點、最省事；但每次都拉整個 list（含 `cooldown`、`model_cooldowns`、`pipes`），消費端要依賴 list 的大結構，list 之後加欄位會連帶牽動週報與 AI 的解析。 |
| **B. 新端點 `GET /v1/usage`** | 唯讀摘要，回 `{schema, subscriptions}` | **採用**。讀法最直接，形狀獨立，不改 list 的形狀。 |
| C. list 加 `?view=usage` | 同一端點、query 決定形狀 | **放棄**。list 的形狀目前是「固定欄位」（`subscription-registry`「list」與「共通 JSON schema」）；依 query 變形會讓 `schema` 規則失效，消費端要先判斷參數。 |
| D. 把用量寫檔或推送 | 由 Gateway 存每週快照 | **放棄**。違反「不新增儲存」；快照由 MyPMO#386 自存（工單範圍）。 |

採用 B 時，`lgwctl usage` 維持現狀（只改 `unavailable` 的呈現，見 PR3）；`lgw/client.py` 新增 `usage()` 讓程式呼叫方能用；遠端讀取直接打 HTTP。不做新的 CLI 子命令，因為 `lgw` 是函式庫、`lgwctl` 是管理指令，兩者都不是 AI 的讀取入口。

**為什麼 A0 之外仍選 B（取捨的誠實說明）。**
- 穩定與耦合：B 的形狀只有十個鍵，給 AI 與 MyPMO#386 週報用；消費端不依賴 list 的大結構，list 之後加欄位不會牽動週報。這是主要理由。
- 大小：**list 的實際回覆大小未量測**（本 change 無法在本機連 4710，見 §6）。A0 比 B 多傳多少，本文件沒有數字；「list 很大」只是推測，不是採用 B 的唯一依據。C-6 只要求 `/v1/usage` 比 list 小，不設絕對門檻。
- 與既有決定的衝突：archive design §4 寫「不加 `/v1/usage`」，並升格為 role-allocation「額度的顯示不新增端點」。本 change 推翻它，對應的 role-allocation delta 見 §3。結構／介面疑點（新端點值不值得、與 role-allocation 的衝突）**已交 PMO，PMO 已同意採用新端點**。

## D2. 一覽的欄位：精簡，但多一個 `status`（提議，與工單草案不同之處）

工單草案列的是 `id`、`kind`、session／weekly、source、fetched_at、stale、reason／note。本 change 採用這些，並**多一個 `status`**：

- 理由：只看 `reason` 無法分辨「`unknown`＋`upstream_error`」與「`ok`＋`stale`＋`upstream_error`」，也分不出 `not_applicable`。`status` 是三值 enum，不含敏感資訊，多一個鍵的成本低。
- 被放棄：不含 `status`（讀的人要從 `reason` 反推，等於重新發明 status）。

不含：`pipes`（窗口已足夠週報；管子是給 resolve 消費端挑選用的，見「管子（pipes）」）、`available_again_at`（與冷卻耦合，不是用量；日後要加另議）、`cooldown`、`model_cooldowns`、`endpoint`、`agents`、`models`、`key`、任何金額。

`id` 是政策檔的 subscription 名稱（例如 `ollama-cloud`），不是帳號名或 key 名；`kind` 是 `api-key`／`claude-subscription`／`local-login` 之一。兩者都已在 list 中公開，不是新增的識別資訊。

## D3. `not_applicable` 的細分：放在 `reason`，`status` 值域不變（提議）

- 做法：`status` 仍只有 `ok`、`unknown`、`not_applicable` 三個值。`not_applicable` 時 `reason` 必為 `no_quota_api` 或 `unavailable`。
- 被放棄的 (a)：新增 `status: unavailable`。會改動 `usage-reporting` 的值域，lgwctl 與所有依 `status` 分支的消費端要一起改；而且舊消費端遇到新值通常會落到「未知」分支，靜默出錯。
- 被放棄的 (b)：`not_applicable` 改成 `unknown`＋`reason: unavailable`。`unknown` 的定義是「有取用量實作但讀不到」（`usage-reporting`「usage 的 status 與 source」），`usage_for()` 的 `disabled` 分支也依賴這個定義；改它等於重寫三處語意。
- 好處：`not_applicable` 的字面意思「沒有取用量實作」仍然成立；`reason` 才是「為什麼」，正是 `reason` 的用途。

## D4. `note`：第九個鍵，只在拿不到時有值（提議）

- 做法：`usage` 增加 `note`。`reason` 為 `no_quota_api`／`unavailable` 時，`note` 是程式內固定表的一句話（依 subscription id 查，見 D5）；其餘為 null。
- 不取上游內容，所以不會有「錯誤訊息夾帶金額或 key」的風險（`usage-reporting`「用量不外洩秘密」）。
- 被放棄的 (a)：把說明塞進 `reason`（例如 `unavailable:antigravity-login`）。會破壞「`reason` 是固定碼、不回顯上游」的規則，也讓 reason 的 enum 失去可枚舉性。
- 被放棄的 (b)：`note` 只放在 `GET /v1/usage`。`resolve` 的 `meta.usage` 與 list 同形狀（`usage-reporting`「resolve」），選中 `chatgpt-personal` 時 `meta.usage` 也會是拿不到，應該也能說明原因。
- 代價：`usage` 從八鍵變九鍵，`usage-reporting` 的「八個鍵固定」要 MODIFIED；按 `schema 版本不因新增欄位而 bump`，`schema` 不 bump（D7）。

## D5. 每個出廠 `not_applicable` 訂閱的分類與證據（提議）

依 `docs/research/usage-data-sources.md`「訂閱用量」表（查證等級：該表標示「已查證文件」，本次未實跑）：

| 訂閱 | reason | 證據 | 固定 note |
|---|---|---|---|
| `opencode-zen` | `unavailable` | 同列於「OpenCode Zen／Console／Go」：額度與預算只見於本機 server `GET /api/experimental/usage/limits` 與 Console 網頁；公開 REST 查不到 | 額度與預算只在 opencode 的本機 server 端點與 Console 網頁，Gateway 取不到。 |
| `opencode-go` | `unavailable` | 同上列；`go.weekly.percent` 在本機 server 端點，需 server 認證 | 週額度只在 opencode 的本機 server 端點（需 server 認證），Gateway 取不到。 |
| `antigravity` | `unavailable` | agy 的 `/usage`（`/quota`）與 Settings 有額度；公開 API 查不到；帳戶 session 在呼叫方機器 | 額度只在 agy 的 /usage 與設定頁，沒有公開 API；登入在呼叫方機器上，Gateway 取不到。 |
| `chatgpt-personal` | `unavailable` | app-server RPC `account/rateLimits/read` 有窗口欄位；需帳戶 session；實帳號窗口未查證 | 額度只在 Codex 的 app-server 介面（需帳戶 session，登入在呼叫方機器上），Gateway 取不到。 |

**分界的定義（可判別）。** `no_quota_api`＝研究文件或官方文件**明載**上游沒有額度資訊來源；其餘（上游有額度資訊，但 Gateway 取不到）＝`unavailable`。文件沒有明載時一律歸 `unavailable`，「查不到」不等於「沒有」。

**出廠政策下目前沒有訂閱符合 `no_quota_api`，碼為預留。** 研究文件對 Zen 與 Go 的證據只說到「外部公開 REST 查不到」，並沒有明載「沒有額度資訊來源」；本機 server 的端點與 Console 網頁仍是額度的來源。依證據強度，先歸 `unavailable`。要改成 `no_quota_api` 須先實證或查到官方文件（見未決 2）。碼本身保留在 spec 裡，給日後有實證的 subscription。

groq：出廠政策檔沒有它，研究文件也沒有它的額度列（grep 零命中）；但 `lgwd/providers.py` 的 PROVIDERS 與 `tests/test_registry.py` 的 groq 測試已存在，該測試的 `usage` 為 `not_applicable`（註解「Groq 沒有公布查剩餘配額的端點」）。依上面的定義，研究文件沒有明載，若歸類應是 `unavailable`；本 change 不為 groq 特別處理，它落在 D9 的通用情形。

## D9. 固定表未收錄的 `not_applicable` 訂閱（提議；取代先前「建構時失敗」）
- 做法：`reason` 與 `note` 的查找順序是**先查固定表（表內用專屬 note）；查不到才用通用 note**。不在固定表的 `not_applicable` subscription 以 `reason: unavailable` 與固定的通用說明（「這個訂閱的額度來源未登記在 Gateway 的說明表，Gateway 取不到。」）降級，並記一條 warning；**每個 subscription id 只記一次**（`UsageService` 實例上的已記集合）。原因：`usage_for` 在每次 list／resolve／`GET /v1/usage` 都會呼叫，不限次數會刷屏。固定表的鍵是 **subscription id**（政策檔的名稱），不是 provider。
- 不做建構時檢查，因此 `UsageService` 不需要新參數。
- 空的 `UsageService`（測試或未注入）：所有 subscription 仍是 `not_applicable`，依上面的順序查找。表內 id（例如 `opencode-zen`）得專屬 note；表外 id（例如 `groq`）得通用 note。`tests/test_usage.py` L148–151 的字典相等會因此改寫（見 §3）。
- 被放棄的 (a)：建構時檢查、缺項即失敗。`UsageService.__init__` 只收 `fetchers`、`settings`、`key_of`、`deployed`（`lgwd/usage.py:204–205`），不認得政策檔的 subscription 清單，要新增參數；而 `RegistryService` 沒注入時用空的 `UsageService`（`lgwd/registry.py:122–124`），測試與多數 test_registry／test_http 依賴這條。若要求建構時全部歸類，測試中空的 `UsageService` 會讓 `openrouter`、`claude-personal` 也失敗。「載入失敗」還會讓 lgwd 起不來，違反「用量是附加資訊，壞了不能拖累 list／resolve」（`usage-reporting` Purpose）。
- 被放棄的 (b)：回 `unknown`＋`internal`。`unknown` 的定義是「有取用量實作但讀不到」（`usage-reporting`「usage 的 status 與 source」），不適用於沒有實作的訂閱。

## D6. `GET /v1/usage` 也觸發取用量（提議）

- 做法：與 list 相同，只在快取過期、已啟用、key 已部署、不在退避時觸發；不等結果。
- 代價：重啟後第一次呼叫會得到 `unknown／not_fetched_yet`。週報讀取方需要先預熱一次，或接受第一次是空的。
- 被放棄：只讀快取、不觸發。讀取方會永遠看到 `not_fetched_yet` 或舊值，除非有人另外呼叫 list；「被查到才取」的語意會被破壞（`usage-reporting`「被查到且過期才取」）。

## D7. `schema` 不 bump（提議）

- `list` 與 `resolve` 只是 `usage` 多一個鍵，依 `subscription-registry`「schema 版本不因新增欄位而 bump」，`schema` 仍為 1。
- `GET /v1/usage` 首版 `schema` 為 1（它是新端點，有獨立的版本，與 resolve 的 `schema` 獨立計的做法相同）。
- 但 `usage-reporting` 的「八個鍵固定」是**這個 spec 自己的**限制，要 MODIFIED（D4）；不能用「新增欄位」來規避它。

## D8. 認證（提議，未決 3）

`GET /v1/usage` 與 list 一樣不做應用層認證，存取控制靠 SSH（`lgwd/http.py` 檔頭）。它回的內容不比 list 多（`usage` 本來就在 list 中）。這是說明，不是 spec 的規範：spec 不寫認證。若本人要求改動，另開單。

## 2. 介面草案（JSON 範例，假值）

`GET /v1/usage`

```json
{
  "schema": 1,
  "subscriptions": [
    {
      "id": "ollama-cloud",
      "kind": "api-key",
      "status": "ok",
      "source": "ollama-api-balance",
      "fetched_at": "2026-11-15T01:00:00Z",
      "stale": false,
      "reason": null,
      "note": null,
      "session": {"remaining_percent": 75, "resets_at": "2026-11-15T06:00:00Z"},
      "weekly": {"remaining_percent": 40, "resets_at": "2026-11-18T00:00:00Z"}
    },
    {
      "id": "claude-personal",
      "kind": "claude-subscription",
      "status": "unknown",
      "source": "claude-statusline",
      "fetched_at": null,
      "stale": null,
      "reason": "upstream_error",
      "note": null,
      "session": null,
      "weekly": null
    },
    {
      "id": "chatgpt-personal",
      "kind": "local-login",
      "status": "not_applicable",
      "source": null,
      "fetched_at": null,
      "stale": null,
      "reason": "unavailable",
      "note": "額度只在 Codex 的 app-server 介面（需帳戶 session，登入在呼叫方機器上），Gateway 取不到。",
      "session": null,
      "weekly": null
    }
  ]
}
```

（`claude-personal` 的 `source` 與 `unknown` 行為由現行 `_unknown()` 保留；本 change 不改。）

`usage` 物件（list 與 resolve `meta`）在 `ollama-cloud` 那一筆的鍵集：`status`、`source`、`fetched_at`、`stale`、`reason`、`note`、`session`、`weekly`、`pipes`。

### 錯誤情形

| 情形 | 回應 |
|---|---|
| 正常 | 200，`schema: 1`，`subscriptions` 依 priority |
| 某一筆組裝例外 | 200；該筆 `status: unknown`、`reason: internal`、`note: null`，其他筆照常 |
| 整個處理函式例外 | 500（與 list 同路徑；log 只記例外型別名） |
| `POST`／`PUT`／`DELETE`／`PATCH`，或其他路徑 | 404，不觸發取用量 |
| query 參數未知 | 忽略，照常 200 |
| lgwd 沒跑或埠不對 | 連線失敗（與 list 同；由呼叫端判斷） |

## 3. 相容性

| 消費者 | 影響 | 處置 |
|---|---|---|
| `lgwd/usage.py` `_not_applicable()` | `reason` 由 null 變固定碼；`note` 新鍵 | PR1 |
| `lgwd/registry.py`（`usage_for` 的 not_applicable 分類、`list_subscriptions`、`_safe_usage`） | 同上；未收錄者依 D9 降級 | PR1 |
| `lgwctl/usage.py` `_usage_text()` | `unavailable` 仍被顯示成「無額度 API」，不正確；檔頭註解「（七鍵）」也已過時 | PR3：依 reason 分流，`unavailable` 顯示「無法取得」與 note；同時改註解 |
| `lgw/client.py` | 讀取不做鍵集檢查，新鍵無影響 | PR2 新增 `usage()`（順手項，可刪；不在 PMO 範圍①～③） |
| `ops-scripts/mac-fallback/switch` L59 | 只檢查 `usage.pipes` 是 list，不看 reason／note | 無影響 |
| `tests/test_usage.py` L24 `SHAPE`（用於 L46、L264、L388–400、L574）；L148–152（整個 `usage` 字典相等，`reason: None`） | 會失敗 | PR1 更新 |
| `tests/test_registry.py` L2412 `SHAPE`（用於 L2450、L2600、L2619、L2626）；L2453–2455（`opencode-zen` 整個 `usage` 字典相等，`reason: None`，無 `note`） | 會失敗 | PR1 更新 |
| `tests/test_lgwctl_usage.py` L33（fixture）、L196 | `not_applicable` 的呈現 | PR3 更新 |
| `tests/test_codex.py` L113–121 | 只逐鍵斷言 `status`、`source`、`session`、`weekly`，不斷言 `reason` 或整個 `usage` 字典 | **不需改**；與 `codex-subscription` 無合併順序依存 |
| `tests/test_http.py` | 新路由；既有 `not_applicable` 斷言只看 `status` | PR2 新增 |
| `docs/deploy.md` §5.7 | 描述 usage 欄位與來源；其中「OpenRouter、OpenCode Zen 為 `not_applicable`」與 usage-reporting 的 OpenRouter 取用量需求不一致 | PR1 一併更新（列出 reason 碼與 note，校正 OpenRouter 描述） |
| `openspec/specs/role-allocation`「額度的顯示不新增端點」與 Purpose | 直接牴觸：新端點與「zen 顯示無額度 API」 | 本 change 的 `specs/role-allocation/spec.md`（RENAMED＋MODIFIED）；Purpose 見 §3.1 |
| `openspec/specs/subscription-registry` `list` 的「七個鍵齊全」 | 與現行八鍵已不符（歷史遺留） | 本 change 的 `specs/subscription-registry/spec.md`（MODIFIED，改為九鍵；tasks 0.4） |
| MyLinuxPool sidecar | **已確認相容**：mylinuxpool-pm-w3 讀碼：usage 全程 dict `.get()`、不比對鍵數、不讀 `reason`（來源：team-mylinuxpool-w3 頻道 2026-11-15 回覆）。tasks 0.3 已完成 | 無；不 bump 的結論以此為據 |
| MyPMO#386（成本週報） | 尚未實作，不受影響 | 實作時依本 change 的契約（`/v1/usage` 與 `reason` 規則）寫 |

### 3.1 `reason` 由 null 變碼：schema 不 bump 的理由（M3）
`usage.reason` 在 `not_applicable` 時由 null 變為 `no_quota_api`／`unavailable`，並新增 `note`。這是行為變更，不只是新增欄位。`schema` 不 bump 的依據是：repo 內消費端都容忍（`lgw/client.py` 的 `_finish` 只驗 body 為 dict；`lgwctl/usage.py` 在讀 `reason` 之前就 return；`ops-scripts/mac-fallback/switch` 只查 `pipes`），外部消費端 MyLinuxPool sidecar 已確認不讀 `reason`（見上表）。若 MyPMO#386 實作時改為「`reason != null` 即異常」，會靜默地把 not_applicable 當成告警；這條風險由 PM 在 MyPMO#386 留言提醒（見 tasks §4），不在本 repo 強制。

### 3.2 主 spec 的 Purpose（archive 時手動）
OpenSpec 的 delta 不能改 Purpose（只能改 Requirement）。archive 時手動改兩份主 spec 的 Purpose，列在 tasks §4：
- `role-allocation`：「額度仍由既有 list 與 resolve `meta` 提供，不新增端點，呈現落在 `lgwctl usage`。」→「額度仍由 list 與 resolve `meta` 提供，並由唯讀的 `GET /v1/usage` 提供精簡一覽，呈現落在 `lgwctl usage`。」
- `usage-reporting`：Purpose 開頭「讓 list 與 resolve 的回覆帶…」→「讓 list、resolve 與唯讀的 `GET /v1/usage` 的回覆帶…」；並把「`not_applicable` 的原因以 `no_quota_api`／`unavailable` 細分，附 `note`」補進去。

## 4. 一覽（四點）

**頂尖專家會怎麼做。** 讀與取分開：讀取只是快取的投影，不做 I/O，觸發規則沿用既有單一來源（`build_usage`）；鍵集用測試釘死，不靠文件約定；拿不到時說出原因，而不是空白；新端點不改既有端點的語意。

**一手資料。** `lgwd/usage.py`、`lgwd/registry.py`、`lgwd/http.py`、`lgwctl/usage.py`、`lgw/client.py`（本文 §0 有行號）；`openspec/specs/usage-reporting/spec.md`；`docs/research/usage-data-sources.md`「訂閱用量」表與「未查證／拿不到」節；`openspec/changes/archive/2026-10-28-role-subscription-allocation/design.md` §4（被推翻的舊決定）。

**方法工具。** `openspec validate usage-overview --strict` 與 `openspec validate --specs --strict`；對 `usage` 鍵集與 `not_applicable` 的所有出現處做 grep，列在 §3；每條 Scenario 對應一個契約編號 C-n（見 tasks），測試名或註解寫 C-n 以便互查。

**怎麼證明有用。** 
1. 讀取方一次呼叫就拿到每把的窗口：PR2 的 C-5 測試（與 list 逐值相等）。
2. 拿不到的每一筆都有 `reason`，`unavailable` 的每一筆都有 `note`：PR1 的 C-3 測試，掃描全部出廠訂閱。
3. 回覆比 list 小：PR2 的 C-6 測試，同一快取狀態下比較位元組數，只要求 `/v1/usage` 小於 list，不設絕對門檻。
4. 最終有用與否由 MyPMO#386 的第一次週報判定（不是本 change 能驗證的）；上線由 PM 經 PMO 取得授權後執行。

## 5. 未決（不要自己決定）

**1. OpenRouter 的「本週已用 USD」（`usage_weekly`）要不要放寬「不回金額」。** 本 change 不做，只留選項：

- (a) 維持現況：不回金額。**建議，先維持。** 週報要的是「剩幾％」，USD 對不上窗口；現有 `credits` 管子已經是「相對於 key 上限的百分比」，不需要金額。
- (b) 只在 `limit` 存在時，把 `usage_weekly` 換算成「已用 %」放進 `pipes`。不帶金額。前提是 `usage_weekly` 的計算期間與 `limit` 的重置週期一致，**未查證**。
- (c) 新增 `spend` 物件放絕對 USD。需要放寬 `usage-reporting` 的「不回金額」與「管子不帶絕對數量」，`/v1/usage` 也會出現金額。**不建議**，要本人決定。

建議：先 (a)；若本人要週消費金額，選 (c) 並另開單，不併入本 change。

**2. opencode-zen 是否確實沒有額度 API。** 目前證據不足，先歸 `unavailable`；要改 `no_quota_api` 需要實跑或查到官方文件（本次依規定未執行 opencode CLI）。

**3. `GET /v1/usage` 是否需要認證。** 沿用 list 的做法（無應用層認證，靠 SSH）。若要改，影響 MyPMO#386 的呼叫方式，另開單。

**4. `available_again_at` 是否要放進一覽。** 本 change 不放（D2）。若 MyPMO#386 需要，另議。

## 6. 風險與未驗證

- MyLinuxPool sidecar 與 MyPMO 的解析方式不在本 repo，未驗證；見 §3。
- 研究文件的查證等級是「已查證文件」，沒有實跑；`chatgpt-personal` 的實帳號窗口、opencode 的 Zen 額度端點皆為未查證。
- 本 change 沒有量測 Gateway 主機上 list 的實際回覆大小（無法在本機連 4710）；C-6 只比較同一環境內的相對大小。
- `codex-subscription` 與本 change 之間沒有需要協調的測試或合併順序（`tests/test_codex.py` 不需改，見 §3）。
- 研究文件的 groq 與 zen 證據只到「文件沒有明載」，`no_quota_api` 的分界依賴未來的實證（§5 未決 2）。
