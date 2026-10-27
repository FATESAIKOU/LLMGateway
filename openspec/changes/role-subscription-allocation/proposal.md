對應工單：#97（層級 b，只做設計）；上層 MyPMO 期 5 M3、MyPMO#163。相關：#58（模型輪換表）、#76／#93／MyLinuxPool#479（模型冷卻）、#2／#3。

> 本 change **只有設計**（proposal、design、delta spec、tasks）。實作要等本人核准。

## Why

期 5 M3 的驗收是一張表：每個角色（PMO／PM／impl／rev／qa）用哪個訂閱、sidecar 照表開、冷卻自動跳、額度看得到。現況缺三塊：

1. 沒有「角色 → 訂閱」的真相。`policy/default.json` 只有全域 `priority`，不分角色、也不分資料能不能碰真資料；誰該用哪把只寫在人讀的草稿裡（推論、未證實）。
2. 隱私等級沒有機器可讀的形式。zen／openrouter 不能碰真資料，靠每個人記得。
3. 額度資料已在 `list`（`usage`），但沒有「照角色看」的視角，zen／go 又沒有額度 API。

## What Changes

- 政策檔：每個 subscription 多選填 `privacy`（`public`／`general`／`real`，預設 `public`）；頂層多 `roles`（五個角色，各有 `floor` 與有序的 `order`）。載入時 fail-closed 驗證（JSON → `privacy` → 訂閱存在 → 角色鍵 → `floor` 必填且合法 → `order` 非空、不重複、id 存在）。
- 新端點 `GET /v1/roles/{role}?agent=[&data=]`：回該角色在這個資料等級下的**有序候選**，每筆 `{id, provider, rank, privacy, available, cooldown, model_cooldowns}`。唯讀、不給 key、**不帶 `usage`／`available_again_at`**（額度只由 `list` 與 resolve `meta` 提供，client 用 id 合併）。
- sidecar 取得候選後，用既有的 `resolve(subscription=…, model=…)` 取憑證；`resolve` 本身不改選把規則。
- 額度頁：不加端點，落在 `lgwctl` 新子命令，用 `list`＋`roles` 的資料排版；zen／go 標「無額度 API」。
- 新 capability `role-allocation`（delta spec 全是 ADDED，不改 `subscription-registry`／`usage-reporting` 的既有要求）。

## 不做

- 不改 `resolve` 的選把與冷卻語意（冷卻仍只是提示，#76 決定 2）；不重做冷卻。
- **不由 Gateway 強制隱私 gate**：`roles` 是可被程式讀取、由 sidecar 遵守的分配表（advisory），`resolve`／`get` 仍可釘低等級訂閱。是否在發憑證邊界加 `data` 是本人的判斷點（design §2.1），不在本 change。
- 不自動改政策、不自動依額度改順序；順序只由政策 PR 改。
- 不把模型放進分配表（屬 #58）；不展開 `allow` 的萬用字元。
- 不降級資料等級：某角色在某等級下沒有候選時回空，不自動改用隱私較低的訂閱。
- groq／Cloudflare／Gemini 不進輪替（本人 2026-10-27 裁定）：程式保留，不給 agent、不放 key。
- 不設計 MyLinuxPool sidecar 的內部，只寫對它的請求清單。
- 不查額度以外的主機資料（用量／429 的主機稽核未查）。

## Impact

- `lgwd/policy.py`、`lgwd/registry.py`、`lgwd/http.py`、`policy/default.json`、`lgwctl/`：新增，既有欄位與端點不變，`schema` 都不 bump。
- 舊 sidecar 不受影響（不叫新端點就是現況）。新 sidecar 打舊 Gateway 會收到 404，需有退回 `list`＋`resolve` 的 fallback（design §3.3 (f)）；有 fallback，Gateway 與 sidecar 才可任一順序上線。
