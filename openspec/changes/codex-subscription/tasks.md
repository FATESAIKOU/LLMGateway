# tasks：Codex（ChatGPT 訂閱）納入 Gateway（#187）

契約編號 C-n 是各 PR 的測試要對得上的最小單位：測試名或註解寫 `C-n`，讓 spec、完成判準與測試可互查。P0 就是本 change（只有文件）。順序由 PM 定，下面只寫依存。

## 0. P0：本 change（文件）

- [ ] 0.1 `openspec/changes/codex-subscription/`：proposal、design、delta specs、tasks。完成判準：`openspec validate codex-subscription --strict` 與 `openspec validate --specs --strict` 的結果如實貼進 PR。無行為改變，無契約。
- [x] 0.2 本人決定 Q6（分發）、Q7（U0）、Q9（等級、真資料）、Q10（界線）、Q11（roles），經 PMO；本人 2026-11-07 已定（design 各節已把「待本人」改成定案）。Q7 的 U0 已確認；本人若改變主意不接受 U0，P2 的 C-6（`usage.status` 為 `not_applicable`）要跟著改，`resolve` 的「codex 的用量為 not_applicable」delta 也要重寫。

## 1. P1：泛化 local-login 條款（純重構；依存 P0）

- [ ] 1.1 `lgwd/policy.py`：`_AGY_ONLY` 改成「local-login 訂閱 → 唯一 agent」的對照表，這一步只有 `{"antigravity": "agy"}`；載入驗證改走對照表（訊息仍只帶固定訂閱 id）。
- [ ] 1.2 `lgwd/providers.py`：`build_env` 的防線改成「`agent` 屬於 local-login agent 集合，或 `kind` 為 `local-login`」，集合由同一張對照表導出。
- 契約：
  - C-1：agy 的既有測試與情境全部不變地通過（`tests/test_policy.py`、`tests/test_registry.py`、`tests/test_providers.py` 或等價檔）。
  - C-2：`antigravity.agents` 不是剛好 `["agy"]` → `PolicyError`。
  - C-3：其他訂閱的 `agents` 含 `agy` → `PolicyError`。
  - C-4：`build_env("agy", <key>, <model>)` 對 api-key provider 回 `{}`；`build_env("claude-code", null, <model>)` 對 `kind` 為 `local-login` 的 provider 回 `{}`（兩條判斷各自獨立可驗）。
- 完成判準：C-1～C-4 綠；`git diff` 只有重構，對外行為（list、resolve、roles 的輸出）逐字相同。只合這一步 main 不會更糟。

## 2. P2：新增 provider／訂閱／agent（依存 P1；Q10 本人 11-07 已定；P2 只實作綁定表的形狀〔C-9a〕，不實作使用界線〔C-9b〕）

- [ ] 2.1 `lgwd/providers.py`：新增 `OpenAIProvider`（`provider` `openai`，`kind` `local-login`，`endpoint` null，不實作 usage fetcher），加進 `PROVIDERS`。
- [ ] 2.2 `lgwd/policy.py`：`SUBSCRIPTIONS` 加 `chatgpt-personal`、`AGENTS` 加 `codex`；對照表加 `"chatgpt-personal": "codex"`。
- [ ] 2.3 `lgwd/registry.py`：resolve／roles 的 agent 驗證收 `codex`；resolve 對 codex 回 `env {}` 與 `usage.status` `not_applicable`。
- [ ] 2.4 測試用假政策（臨時政策 JSON）覆蓋；**`policy/default.json` 這一步不加這一筆**。
- 契約：
  - C-5：`agent=codex&provider=openai[&model=]` → 200，`meta.kind` `local-login`、`meta.subscription` `chatgpt-personal`、`env` `{}`、`env_schema` 1、模型只在 `meta.model`。
  - C-6：`meta.usage.status` `not_applicable`、`source` 與窗口 null。
  - C-7：list 的 `chatgpt-personal` row：`provider` `openai`、`endpoint` null、`agents` `["codex"]`、`available` true（無未來冷卻）、無 `key`。
  - C-8：`KEY_NAMES` 沒有 `chatgpt-personal`；key 檔沒有它的鍵時 resolve／list 不丟 `unknown subscription`、不回 `not_deployed`。
  - C-9a（形狀，P2）：local-login 對照表裡 `chatgpt-personal.agents` 不是剛好 `["codex"]` → `PolicyError`；其他訂閱含 `codex` → `PolicyError`。
  - C-9b（使用界線；Q10 本人 11-07 已定）：只准原版 Codex CLI、不允許 opencode；Gateway 不代理、不轉手 token、不用訂閱 token 打 API；登入檔絕不進 GitHub Actions／ghr runner。界線以 design Q10 的文字記錄，不實作、不寫測試。
  - C-10：`openai` 在 `PROVIDERS`、政策檔沒有任何 `openai` 訂閱時，`agent=codex&provider=openai` 回 404 `not_found`（既有 resolve 步驟 2 與 3 的 provider 與 agent），body 無 `env`。
  - C-11a：假政策下登錄 `chatgpt-personal` 冷卻後，`agent=codex&provider=openai` resolve 仍回 200 並帶 `meta.cooldown`。
  - C-11b：假政策下 roles 在 `order` 含 `chatgpt-personal` 且 `privacy`、`roles.impl.floor` 足夠（`public`）時才有候選，不在 `order` 時 `candidates` 為 `[]`。
  - C-12：`build_env("codex", <key>, <model>)` 對 api-key provider 回 `{}`；`build_env("claude-code", null, <model>)` 對 `OpenAIProvider` 回 `{}`。
  - C-12a：`GET /v1/roles/impl?agent=codex&data=public` 不回 400（回 200）；`agent=foo` 仍回 400。
  - C-12b：resolve 的 `agent=foo` 回 400 `bad_request`；`agent=codex&provider=openai` 不因 `agent` 回 400（`registry-errors`）。
  - C-12c：登錄冷卻 `model=codex/gpt-6.1-sol`（`chatgpt-personal`，`allow` 含該裸 id）→ 400 `bad_request`，冷卻不變（`subscription-cooldown`、`registry-errors`）。
- 完成判準：C-5～C-12c 綠；P1 的 C-1～C-4 仍綠；出廠政策載入後 list 沒有 `chatgpt-personal`。只合這一步 main 不會更糟。
- 部署：lgwd 升級是真機操作，經 PMO 取得本人單次授權後由 PM 執行，不在 PR 內。

## 3. P3：codex 額度判讀（Q8 的查證已在 design；與 P2 可並行）

- [ ] 3.1 `lgw/signals.py`：新增 `agent: "codex"` 的規則，排在通用規則之前。
- 契約：
  - C-13：「You've hit your usage limit …」類文字 → `exhausted`。
  - C-14：含日期但沒有時區的重置時刻（例 `try again at Feb 23rd, 2026 9:01 PM`）→ 退回「現在＋1 小時」，不換算（design Q8 定案 (ii)：真實樣本都沒標時區）。
  - C-15：只有時刻的重置（例 `try again at 1:36 PM`）→ 退回「現在＋1 小時」。
  - C-16：容量錯誤（"Selected model is at capacity" 類）不是 `exhausted`。
  - C-17：`claude-code`、`opencode` 與通用規則的既有分類全部不變（回歸）。
- 完成判準：C-13～C-17 綠；既有 `tests` 中 signals 相關檔全綠。測試用的樣本字串只用上面 design 引的公開錯誤文字，不含任何帳號資訊。

## 4. P4：政策資料（依存 P2；本人定 Q9，Q5 再對一次官方頁）

- [x] 4.1 `policy/default.json`：加 `chatgpt-personal`（`priority`、`privacy`＝`general`（Q9 已定：Plus、已關訓練→○；不放 `real`）、`models.allow`／`default`、`agents` `["codex"]`）。
- [x] 4.2 `roles.impl`、`roles.qa` 的 `order` 加 `chatgpt-personal`，排在 `claude-personal` 之前（Q11 已定：直接進輪換、impl／qa 優先 Codex）；**前提仍是 MyLinuxPool#797 能開**，#797 就緒前不放進 `order`。「撞上限退 Sonnet」只能寫成 `chatgpt-personal` 之後接 `claude-personal`；用哪個模型（Sonnet、Opus 審查）由呼叫方決定，Gateway 不新增『角色→模型』欄位（要加是新介面，另過設計審）。
- 契約：
  - C-18：出廠政策載入成功，list 多一筆 `chatgpt-personal`（`available` true，`privacy` 為 `general`）。
  - C-19a（測試）：`models.default` 被 `models.allow` 涵蓋（`subscription-registry`「models.default 必須被 allow 涵蓋」），且 `allow` 等於 P4 時核對過的清單。
  - C-19b（人工驗收，不是測試）：`allow` 的每個 id 是 Codex 實際接受的裸 id；起點 `gpt-6.1-sol`、`gpt-6.1-terra`、`gpt-6-astra`、`gpt-6-luna`、`gpt-6-sol`（依方案可用的不同，與 Q9 的方案種類連動）；合併前再對一次官方 models 頁，PR 附讀取日期。
  - C-20：`roles.impl`／`roles.qa` 的 `order` 含 `chatgpt-personal` 且排在 `claude-personal` 之前（#797 就緒後），既有角色的候選相對順序不變。
- 完成判準：C-18、C-19a、C-20 綠，C-19b 已人工核對並附日期；整套測試綠。只加訂閱、不進 order 時 main 不會更糟；進 order 前 MLP#797 要能開。

## 5. P5：model-rotation-survey（文件；依存 Q5）

- [ ] 5.1 `openspec/specs/model-rotation-survey/spec.md` 與其 skill 從四家改五家，`docs/model-rotation.md` 加 Codex 的列。
- 契約：C-21：`openspec validate --specs --strict` 與週更流程的既有檢查通過。
- 完成判準：C-21；無程式改動。

## 6. P6（之後，另開一張）：用量 U1

- 試用期用 U0（Q7 已定）；試用期滿要依用量決定時再開。選 U1 時：sidecar 讀 codex app-server 的 `account/rateLimits/read`，推給 Gateway；要新端點與新契約，另寫 change。不在試用期必要範圍。
