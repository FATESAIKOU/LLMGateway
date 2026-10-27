對應工單：#97（b，只設計）。引用 `檔案:行號` 指 `main`（`c774d6f`）；`.work/…` 指 PMO 派工的調查檔（不進 repo）。標 **[事實]** 的有出處，標 **[提議]** 的是作者判斷、本人未指示。

## 1. 角色 × 訂閱分配表

### 1.1 隱私等級（gate）

**[提議]** 選填訂閱屬性 `privacy`，三級方案與缺省 `public`（fail-closed）都是作者提議。`public` 只送 public repo 與假資料；`general` 可送一般工單內文／決定檔／交接檔；`real` 可送一般真資料。**任何等級都不送憑證、金鑰**。

| 訂閱 | 等級 | 依據 |
|---|---|---|
| claude-personal | `real` **[提議]** | **[事實]** 本人 2026-10-27 口頭確認 Model Improvement 已關（opt out）；關閉時保留 30 天（code.claude.com/docs/en/data-usage）。另有 safety flag 例外（privacy policy §2 的措辭，非 data-usage 頁）。Gateway 看不出這個開關，只能信本人確認；殘餘風險是否接受見判斷點 2 |
| ollama-cloud | `real` **[提議]** | **[事實]** 出處只撐「官方明文不訓練」、請求結束不保留（`.work/reports/p5-m3-subscriptions.md` §4）；保留天數官方沒寫，定 `real` 是推斷 |
| opencode-go | `general` | **[事實]** allow 只剩 `beam-free`（#95 已 merge、**等部署**）；官方 Not used／保留 30 天（§2）。**[提議]** 有保留且無 ZDR 承諾，定 `general` 而非 `real` |
| opencode-zen | `public` | **[事實]** 未付費帳號可用內容改進；多數免費模型明寫會訓練（§1） |
| openrouter | `public` | **[事實]** 上游各自決定、`openrouter/free` 自動路由無法預測（§3） |
| groq | `public` **[提議]** | 程式保留、**不給 agent（本人 2026-10-27 裁定）**，不進任何 `order`、不放 key |

### 1.2 角色分配表 **[提議]**

`floor`＝該角色最低資料等級（請求想放寬也放不下去）。`order`＝偏好順序；隱私不夠、`agents` 對不上的會被濾掉（claude-personal 只有 `claude-code`，zen／go／openrouter 只有 `opencode`，ollama-cloud 兩者皆可）。

| 角色 | floor | order | 理由 |
|---|---|---|---|
| PMO | real | claude-personal → ollama-cloud | 長推理、整天碰決定檔與私有工單 |
| PM | real | claude-personal → ollama-cloud | 同上 |
| impl | public | zen → go → openrouter → ollama-cloud → claude-personal | 免費桶先用，真資料時自然剩 ollama→claude |
| rev | public | openrouter → zen → go → ollama-cloud → claude-personal | 讀多寫少，適合 openrouter 的 50 RPD |
| qa | public | zen → go → openrouter → ollama-cloud | 跑測試；不排 claude（留給 PMO／PM） |

呼叫方帶 `data=`（缺省 `real`）。例：impl 的 public repo 單 `data=public` → 從 zen 起；含私有內容 `data=real` → 只剩 ollama-cloud、claude-personal。與 #58 分工：分配表粒度＝訂閱／角色、不列模型；`docs/model-rotation.md` 粒度＝模型、不列角色。

## 2. 分配表放哪 **[提議]**

選 Gateway 政策檔當機器可讀的真相，MyPMO handbook 只寫規則與連結、**不抄數字**。理由：sidecar 能讀、有載入驗證、改動走 PR；只放 handbook 則 sidecar 讀不到。

### 2.1 這個 gate 能保證什麼（不 overclaim）

- **第一層（本 change）**：`roles` 端點讓分配表與隱私等級**可被程式讀取，由 sidecar 遵守（advisory）**。Gateway 不擋 `resolve`／`get` 的釘住（`lgwd/registry.py:341-349`、`:203-208` 不看 privacy）；舊 sidecar、有 bug 的挑法、任何能打 loopback 的 client 仍能釘低等級訂閱。MLP 請求書明列「不得繞過 `roles` 直接釘低等級訂閱」。
- **第二層（不在本 change，判斷點 6）**：在發憑證的邊界加選填 `data` 到 `resolve`，釘住的訂閱 `privacy` 低於生效等級就回 403。缺省取捨：缺省當 `real` → 舊 sidecar 不帶參數也被收窄，行為改變；缺省不濾 → 舊 client 繞過仍在。建議：另開 c／d，缺省不濾（不破壞相容），新 sidecar 一律帶 `data`；全部 sidecar 升級後再議是否改缺省。

## 3. sidecar 怎麼挑、冷卻怎麼跳

### 3.1 Gateway 介面 **[提議]**

`GET /v1/roles/{role}?agent=[&data=]`（spec `role-allocation`）。回有序候選，每筆 `{id, provider, rank, privacy, available, cooldown, model_cooldowns}`（`provider` 是 resolve 要的中介資料、非 key，且 `provider ≠ id`，例：claude-personal → anthropic）；**不帶 `usage`／`available_again_at`**，也不觸發取用量。`rank` 在篩選後連續 `1..n`。額度、模型清單由 client 用 `list` 依 id 合併（`usage` 在既有 spec 只在 list 與 resolve `meta`、`available_again_at` 只在 list；不修改既有 Requirement）。冷卻與額度不影響成員與順序（#76 決定 2：Gateway 告知、不代選）。不給 `resolve` 加 `role=`：`provider` 必填、選把鍵是 `priority`（`lgwd/registry.py:315-340`、`:164`），會讓 Gateway 變成代選的一方。

### 3.2 sidecar 的挑法（Gateway 側規格）

1. 帶 `role`、`data`、`agent` 取 `roles` 候選，再 `list` 合併 `usage`／`models`。
2. 依 `rank`：`cooldown` 整把或 `available=false` → 跳；`usage` 有窗口 `remaining_percent`＝0 → 視同整把不可用，直到 `resets_at`；`usage.status` 為 `unknown`（含 `reason: no_key`）／`not_applicable` → 照試（有 key 才 resolve 得到）。
3. 同一把內：要用的模型（`default` 或上一步選定的）在 `model_cooldowns` → 換 `models.allow` 內**字面 id**、沒冷卻的另一個模型；都冷卻 → 下一把。`allow` 的萬用字元（如 zen 的 `*-free`）**本 change 不展開**（Gateway 只列已冷卻模型，沒有全集）；代價是 zen 只會試 `default` 與字面 id。要展開需政策加 `models.known`（判斷點 7）。
4. `resolve(agent, provider=<row.provider>, subscription=<id>, model=<m>)` 取憑證（照舊：冷卻不擋、`meta` 附冷卻）。**Claude 不送 `model`**（帳號級窗口，送了只冷卻單一模型、整把仍被選中；`.work/reports/93-request.md:26`）。
5. 200 後看 `meta.cooldown`／`meta.model_cooldowns[meta.model]` 是否在未來：整把在未來 → 換把；只有該模型 → 先換同把其他模型、再換把；都不行才等（#93 需求 9，不直接開）。
6. 429：登錄冷卻（opencode 送 model、Claude 不送），回步驟 3（先用剛登記的冷卻更新本地狀態）對**同一把先試其他未冷卻模型**（#93 需求 8／9(b)）；都沒有才往下一個 rank，並重取 `roles`。
7. 全部不可用 → 等（最早的 `available_again_at`）或停手回報 PM；**不放寬資料等級**，放寬是本人／PMO 的決定。

### 3.3 對 MyLinuxPool 的請求清單（交 PMO 轉）

(a) `POST /agents` 帶 `role`、`data`（缺省 `real`）；(b) 候選改用 `roles`＋`list`；(c) 保留 #93 九條（需求 9 的判讀見 3.2.5）；本 change 只改候選來源（`roles`＋`list`）與 429 的同把重試，3.2.3 的「不展開」是 Gateway 不提供模型全集，sidecar 要不要自己展開由 MLP 決定；(d) 候選空或全冷卻時自訂等／停，不得降等級；(e) **不得繞過 `roles` 直接釘低等級訂閱**；(f) `roles` 回 404（舊 Gateway 或無 `roles` 段）時退回 `list`＋`resolve`＋#93 九條；(g) `usage` 0% 跳整把、`unknown`（含 `no_key`）照試（3.2.2）。

## 4. 額度看得到

- **現況 [事實]**：`list` 每筆有 `usage`（七鍵：`status`／`source`／`fetched_at`／`stale`／`reason`／`session`／`weekly`；`lgwd/registry.py:115-129`）。claude-personal、ollama-cloud、openrouter 有數字；zen、go 是 `not_applicable`（`list` 裡的 groq 同為 `not_applicable`，只是不進輪替）。
- **[提議]** 不加 `/v1/usage`。落點：`lgwctl` 新子命令 `usage`（`lgw` 是函式庫、`lgwctl` 才是管理指令；`lgwctl/cli.py:17,43`），用 `list` 與 `roles` 合併排版；`not_applicable` 顯示「無額度 API」不顯示 100%，旁列冷卻。zen／go 的 00:00 UTC 重置只是觀測。

## 5. 本人的判斷點

1. **放哪**：Gateway 真相＋handbook 引用，還是只放 handbook。
2. **claude-personal 定 `real`**：是否接受例外（safety flag）與 30 天保留的殘餘風險；Gateway 看不出該開關。隊員經 sidecar 用個人訂閱**合規**：sidecar 開的是原版 Claude Code；opencode 上不使用 Claude 訂閱（`docs/research/claude-failover.md` §4）。不接受則改 `general`，PMO／PM 只剩 ollama-cloud。
3. **opencode-go 定 `general`**（只限 `beam-free`）。
4. **PMO／PM 的 floor 是 `real`**。
5. **缺省 `data=real`**：沒帶就最嚴。
6. **要不要程式保證 gate**：只做 advisory（本 change），或另開單給 `resolve` 加 `data`（§2.1，建議缺省不濾）。
7. **萬用字元模型是否展開**：不展開（現況），或另開單加 `models.known`。

## 6. 未查／未實作

- 主機的用量與 429 稽核未查（不連主機）；隊員經 sidecar 用個人訂閱合規（sidecar 開的是原版 Claude Code、opencode 上不使用 Claude 訂閱），見判斷點 2 與 `docs/research/claude-failover.md` §4。
- 端點、載入驗證、`lgwctl usage` 未實作，tasks 為待辦。
