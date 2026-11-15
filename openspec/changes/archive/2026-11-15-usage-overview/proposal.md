對應工單：#204「各訂閱用量查詢：補齊現有 usage 的缺口」（層級 c；本 change 只寫設計文件，實作拆成 tasks 的 PR）。上層：MyPMO#386（成本週報，需要每週讀一次各訂閱用量）。相關：MyLinuxPool#1259（用量資料來源）、LGW#202（用量資料調查，`docs/research/usage-data-sources.md`）、spec `usage-reporting`。

## Why

Gateway 已經有 `usage`（`usage-reporting` spec；`lgwd/usage*.py`），ollama-cloud 與 openrouter 有數字，claude-personal 是 `unknown`／`upstream_error`。剩下的缺口有三個：

1. **讀取面**：AI 與 PMO 沒有一個好用的「各訂閱用量一覽」讀法。`llmgwctl usage` 只在 Gateway 主機上能跑；`GET /v1/subscriptions` 的回覆含 `cooldown`、`model_cooldowns`、`pipes` 等，每筆都很大，週報要的只是窗口。
2. **拿不到的原因混在一起**：opencode-zen、opencode-go、antigravity、chatgpt-personal 現在都是 `not_applicable`、`reason: null`。「確定沒有額度 API」與「有額度資訊，但 Gateway 取不到」讀起來一樣，lgwctl 還把兩者都顯示成「無額度 API」。
3. **拿不到時沒有說明**：`reason` 是固定碼，沒有一句話說明為何拿不到，讀的人只能去翻 spec。

## What Changes

- **精簡一覽**：新增唯讀的 `GET /v1/usage`，每筆只有 subscription id、kind、status、source、fetched_at、stale、reason、note、session、weekly；不含 pipes、key、帳號、金額。被查到才取的規則不變。（design D1、D2）
- **細分 not_applicable**：`status` 的值域不變，`not_applicable` 的 `reason` 改為 `no_quota_api`（確定沒有額度查詢途徑）或 `unavailable`（有額度資訊但 Gateway 取不到）。出廠分類：opencode-zen、opencode-go、antigravity、chatgpt-personal 皆為 `unavailable`；目前沒有出廠訂閱有證據支持 `no_quota_api`。（design D3、D5）
- **note**：`usage` 增加第九個鍵 `note`，只在拿不到（`no_quota_api`／`unavailable`）時是固定表中的一句話，不取上游內容。（design D4）
- **schema 不 bump**：新增的鍵與新端點都只是新增，依既有 `schema 版本不因新增欄位而 bump` 規則；`/v1/usage` 首版 `schema` 為 1。（design D7）
- **delta spec**：`usage-reporting` 的四條 Requirement 用 MODIFIED 改寫（形狀、status 與 source、原因碼、觸發），新增五條 ADDED（`not_applicable 的 reason 與固定表`、`note 的語意`、`用量一覽（GET /v1/usage）`、`用量一覽的內容邊界`、`用量一覽的讀取語意`）；`role-allocation` 的「額度的顯示不新增端點」改名為「額度的顯示與用量一覽」（RENAMED＋MODIFIED）；`subscription-registry` 的 `list` 一條 MODIFIED（七個鍵改為九個鍵）。
- **不改** claude-personal 的 `upstream_error`：它的 `reason` 照現行原樣帶出，本 change 不處理來源（sidecar statusline）。

## What Does Not Change

- 不改任何程式、測試、政策檔（`lgwd/`、`lgw/`、`lgwctl/`、`tests/`、`policy/` 都不動）；本 change 只新增 `openspec/changes/usage-overview/`。
- 不新增儲存：用量仍只在記憶體快取，不進 SQLite、不進稽核。每週快照由 MyPMO#386 呼叫後自己存，Gateway 不存。
- 不碰 sidecar（MyLinuxPool）與 Claude 取用量來源。
- 不部署：lgwd 升級到 Gateway 主機是真機操作，需經 PMO 取得本人單次授權後由 PM 執行，不在任何 PR 內。
- 不改 `available` 與 `available_again_at` 的算法（仍只看冷卻與 `session`／`weekly`）。
- **OpenRouter 的「本週已用 USD」（`usage_weekly`）不在本 change**：spec 目前規定「不回金額」，要不要放寬由本人決定，本 change 只在 design「未決」節留選項與建議。
- 不把 `/v1/usage` 做成 list 的 query 參數，也不加寫入端點。
- 不在這裡決定 `chatgpt-personal` 的隱私等級或使用界線（那是 `codex-subscription` 的事）。

## Impact

- 受影響的 capability（delta 數量與 `specs/` 內的檔案一致）：
  - `usage-reporting`：MODIFIED 四條、ADDED 五條（N1：「not_applicable 的 reason 與固定表」由 MODIFIED「usage 的 status 與 source」拆出，讓該條本文回到 500 字內）。
  - `role-allocation`：RENAMED 一條（「額度的顯示不新增端點」→「額度的顯示與用量一覽」）、MODIFIED 一條。原標題與本 change 的 `GET /v1/usage` 直接牴觸；Purpose 那句不是 delta 可改的 Requirement，依 design §3 在 archive 時手動改寫。
  - `subscription-registry`：MODIFIED 一條（`list`；原文「七個鍵齊全」與現行八鍵已不符，本 change 後為九鍵；整段全文抄自現行 spec）。
- 與 `codex-subscription` 的依存：它的 C-6 要求 `chatgpt-personal` 的 `usage.status` 為 `not_applicable`，本 change 維持該 status，只多 `reason: unavailable` 與 `note`；`tests/test_codex.py` 只逐鍵斷言 `status`、`source`、`session`、`weekly`，不斷言 `reason` 或整個 `usage` 字典，不需改，兩 change 無合併順序依存。
- 受影響的程式（將來的 PR，本 change 不改）：`lgwd/usage.py`（reason 與 note 的固定表、九鍵）、`lgwd/registry.py`（`list_usage_summary`、`usage_for` 的 not_applicable 分類）、`lgwd/http.py`（`GET /v1/usage` 路由）、`lgw/client.py`（`usage()`）、`lgwctl/usage.py`（`unavailable` 的呈現）。

## Non-goals

- 不做預測、不做「還剩幾天可用」的推算（`available_again_at` 不變）。
- 不做多帳號、多 key 的彙總。
