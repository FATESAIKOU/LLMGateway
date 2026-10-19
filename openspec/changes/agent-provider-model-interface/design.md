## Context

動機見 proposal.md（#33）。影響做法的現況與限制（出處：本 repo 的 `docs/research/`、`openspec/changes/gateway-v0`、`policy/default.json`、`lgwd/`；sidecar 的事實只寫**介面層級**——它收什麼、呼叫哪個端點、環境怎麼組——來自 MyLinuxPool `modules/sidecar` 的現行文件與程式（private repo，不整段引用）：

- **現行 v5 介面**（`gateway-v0`，#8）：`GET /v1/subscriptions`（list，不含 key）、`GET /v1/subscriptions/{id}?agent=`（get，含 key）、`POST /v1/subscriptions/{id}/cooldown {until}`。共通 JSON row：`schema`、`id`、`provider`、`kind`、`agents`、`priority`、`models{allow,default}`、`endpoint`、`key`（只在 get）、`cooldown{until|null}`、`available`。**row 不帶環境變數名**（v0 決定 7）：env 名跟 agent 版本走，而 agent 與 sidecar 一起釘在 worker 映像裡。
- **政策檔**（`policy/default.json`）：五把 subscription（`claude-personal`／`claude-company`＝provider `anthropic`、`opencode-zen`、`openrouter`、`ollama-cloud`）；每把 `priority`、`agents`、`models.allow`（fnmatch 樣式）、`models.default`。openrouter 只允許免費模型；Claude 兩把 `agents` 只有 `claude-code`（條款）；zen 只有 `opencode`。
- **sidecar 現況（介面層級）**：worker／Mac 上的 daemon；`POST /agents` 收 `id`、`agent`、`workdir`、`session`、`subscription: {"id": …}`（選填，省略時 sidecar 自己挑）、`input`。它對 LLMGateway 打三個端點（list／get／cooldown）。driver 的 `build_env(subscription)` 把（provider、kind、endpoint、key、models）組成子行程環境：claude 系＝`CLAUDE_CODE_OAUTH_TOKEN`（訂閱）或 `ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_BASE_URL`（api-key），模型進 `ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`；opencode＝`OPENCODE_CONFIG_CONTENT`（provider apiKey、model、permission）＋server password 等維運變數。模型＝`models.default`，否則 allow 第一個具體項；`claude-subscription`＋default null＝用 agent 自己的預設。換把＝額度訊號 → cooldown → list 挑下一個（priority）→ get → 同一個 agora session continue。
- **痛點**：指定只能到 subscription；同一把上別的免費模型（例：zen 的 `*-free`）點不到，審查隊員換不到別的免費模型。
- **進行中的 sidecar 工單**（MyLinuxPool）：#216（AI 名、身分變數、`POST /agents` 的 harness 欄位）、#229（互動版 Claude PM：sidecar 自己的終端＋hook 回報狀態）。兩者動的是**身分與終端**，與 provider＋model 正交；交會只有 `POST /agents` 的欄位白名單兩邊都會動到（合併時對一次）。
- **額度訊號**（`quota-signals.md`）：Zen 免費＝429 `FreeUsageLimitError`＋`retry-after`；sidecar 判讀不出恢復時間時報「現在＋1 小時」。**已知上游限制**（本人 2026-10-17 的觀察，MyAiStorage#85）：Zen 免費層只收 opencode 原生新開的 session；經匯入／接續的 session 用免費模型會被上游拒。換把在接續 session 上換到另一個 Zen 免費模型可能被上游拒——那是上游行為，resolve 擋不了，屬 sidecar 階梯設定的事（未決 4）。
- **#32（provider 追加可用模型）**：把政策檔各 provider 的 `models.allow` 補上具體模型；與本 change 互補（決定 7）。

## Goals / Non-Goals

**Goals**

- 呼叫方以 provider＋model（＋agent 種類）點菜；LLMGateway 回 meta＋必要的環境變數。審查可用同一把的不同免費模型。
- 介面「必要且只有這些」：終態對外仍是三個介面（list、resolve、cooldown）。
- 政策守門不變：`models.allow` 擋不該用的模型（例：會計費的），點了就當場拒，不代選。
- 值的紀律：resolve 回覆的 env 值只進 sidecar 建的子行程環境；不進 log、不進 status、不進稽核。

**Non-Goals**

- 不給 command（指令、參數、終端、agora 接續＝sidecar 的事）。
- 不做資料面（請求代轉）、用量／額度查詢、限流。
- 不做租約式管控（同時上限、「誰正在用」）。
- 不處理 agy（政策無此 subscription；要支援另開單）。

## Decisions

### 決定 1：新介面是 `GET /v1/resolve`——輸入 agent＋provider＋model，輸出 meta＋env

| | |
|---|---|
| 誰呼叫 | sidecar（worker／Mac）；測試與 Mac 工具經 `lgw.client.resolve` |
| 操作 | `GET /v1/resolve?agent=<種類>&provider=<provider>[&model=<模型 id>]` |
| 範例 | `GET /v1/resolve?agent=opencode&provider=opencode-zen&model=big-pickle` |
| 回傳 | 200 `{schema:1, meta:{…}, env:{…}}`；錯誤 `{"error":"<碼>", …}`（決定 3） |

- `agent`：必填，`claude-code`｜`opencode`（與 v5 get 的 `agent` 參數同義）。
- `provider`：必填，政策裡的 provider 名（`anthropic`、`opencode-zen`、`openrouter`、`ollama-cloud`）。**`anthropic` 蓋兩把 subscription**——選哪一把見決定 3。
- `model`：選填，**供應商自己的模型 id**（與 `models.default` 同一命名空間；例 `mimo-v2.6-flash-free`、`openrouter/free`——可含 `/`）。省略時的語意見決定 4。
- `meta`（200 時）：`subscription`（選中的 id；冷卻登錄用它）、`provider`、`kind`、`agent`、`model`（選定的，或 null）、`models`（該把的 `allow` 與 `default`——「限制」）、`endpoint`、`priority`、`cooldown`、`available`。200 時 `cooldown` 恆 null、`available` 恆 true（選把已排除冷卻與未部署；欄位保留是為了與 list 的 row 同形、可直接回顯）。
- `env`（200 時）：名稱→值（決定 2）。錯誤回覆 MUST NOT 含 `env`。
- **為什麼 GET＋query**：v5 的 get 已是「GET＋query 回 key」的先例；model id 可含 `/`（例 `openrouter/free`），放 query 參數不必處理路徑段的 percent-encoding。
- **為什麼第四個端點、終態仍是三個**：resolve 取代 get（決定 5）。
- **放棄的選項**：
  - POST＋body：查詢性的呼叫多一條 body 解析與 413 路徑，沒有對應的好處。
  - `GET /v1/providers/{p}/models/{m}`：model 帶 `/` 時路徑段要 percent-encode，容易做錯；query 一律安全。
  - 端點叫 `/v1/env`：名字只講了輸出的一半（meta 也是重點）；resolve 講的是「把 provider＋model 解析成那一把＋環境」這個動作。

### 決定 2：env 由 LLMGateway 組（名稱＋值）；邊界＝「憑證／端點／模型」 vs 「維運／行為」

LLMGateway 依（provider、kind、agent）組 `env`：

| agent＋kind | env |
|---|---|
| claude-code＋claude-subscription | `CLAUDE_CODE_OAUTH_TOKEN=<key>`；model 非 null 時 `ANTHROPIC_MODEL=<model>`、`ANTHROPIC_SMALL_FAST_MODEL=<model>` |
| claude-code＋api-key（anthropic） | `ANTHROPIC_API_KEY=<key>`；model 變數同上 |
| claude-code＋api-key（其他 provider） | `ANTHROPIC_AUTH_TOKEN=<key>`、`ANTHROPIC_API_KEY=""`；`endpoint` 非 null 時 `ANTHROPIC_BASE_URL=<endpoint>`；model 變數同上 |
| opencode（api-key） | `OPENCODE_CONFIG_CONTENT`＝完整 JSON 設定字串：`provider.<opencode id>.options.apiKey`、`provider.<id>.models.<model>.name`、`model`／`small_model`＝`<opencode id>/<model>` |

- opencode 的 provider id 對映（`opencode-zen`→`opencode`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud`）與「`endpoint` 是 provider 根位址、不抄進 baseURL」（#28）的知識，從 sidecar driver 搬進 Gateway：加新 provider 時只改 Gateway 一處，全體 sidecar 立即可用。
- **sidecar 疊自己的維運／行為變數**：auto-update／non-essential traffic 開關、`OPENCODE_SERVER_PASSWORD`、`AGORA_CLAUDE_CMD`、`MLP_AGENT_NAME`／`ANGAREION_IDENTITY`（#216）、permission（併進 `OPENCODE_CONFIG_CONTENT`；歸屬見未決 1）。
- **值的紀律**：resolve 回覆的 env 值 MUST NOT 進 LLMGateway 的 log 與稽核，MUST NOT 進 sidecar 的 log 與 `GET /agents` status；值只存在 sidecar 建的子行程環境（不上命令列、不寫檔——sidecar 既有紀律＋resolve 的值同一套）。HTTP log 照 v5 只記方法／路徑／狀態碼（query 只有 provider／model／agent，非機密）。
- **為什麼反轉 v0 決定 7（row 不帶環境變數名）**：
  1. 痛點本體是「模型選擇＋憑證接法綁在 sidecar driver」：新 provider／新接法都要改 sidecar、重建 worker 映像。移進 Gateway 後，加 provider＝改 Gateway（政策＋providers＋env 組成），sidecar 不動。
  2. 版本耦合是雙向的：v0 的理由是「env 名跟 agent 版本走、sidecar 與 agent 同映像」；但 Gateway 也是唯一升級點（tag），組成集中後「上游／政策變了全體跟上」不用逐台 worker 重建。組成只依各 agent 的公開穩定變數（`ANTHROPIC_*`、`OPENCODE_CONFIG_CONTENT`），偏移風險與緩解見 Risks。
  3. issue 明文：輸出就是「必要的環境變數」；「不給 command」已把行為邊界劃給 sidecar。
- **放棄的選項**：
  - 維持 v0（Gateway 只給 key＋meta、sidecar 組 env）：痛點仍在（審查換模型還是要動 sidecar），與 issue 相違。
  - Gateway 組「完整子行程環境」：`AGORA_CLAUDE_CMD`、`OPENCODE_SERVER_PASSWORD`、身分變數是 sidecar 的維運狀態，Gateway 不知道也不該知道。
  - env 給名稱清單、值另開一個端點取：多一個介面（違反「必要且只有這些」），值終究走同一條線。

### 決定 3：選把規則與錯誤碼（同 provider 多把、冷卻、allow 不含）

**選把**（依序）：

1. 候選＝`provider` 相同、`agents` 含請求的 agent、（`model` 有給時）model 被 `models.allow` 樣式（fnmatch）涵蓋、key 已部署。
2. 候選中排除冷卻中的（`cooldown` 有未來的 `until`）。
3. 挑 `priority` 最小；同分比 id（與 v5 sidecar 的選擇規則同一機械；政策檔是權威）。
4. 沒有可挑的 → 依「哪一步斷掉」回錯誤（下表）。

**錯誤碼**（疊在 v5 的表上；錯誤回覆 MUST NOT 含 `env`，訊息固定字串、不回顯請求值）：

| 狀態 | 碼 | 什麼時候 | 附帶 |
|---|---|---|---|
| 400 | `bad_request` | 缺 `agent`／`provider` | `missing` |
| 404 | `not_found` | 沒有任何 subscription 是這個 provider | |
| 403 | `agent_not_allowed` | 有該 provider 的 subscription，但無一允許這個 agent | |
| 403 | `model_not_allowed` | 有 provider＋agent 的候選，但 model 不被任何候選的 `allow` 涵蓋（政策守門：擋不開放／會計費的模型） | |
| 409 | `not_deployed` | 有涵蓋 model 的候選，但全部 key 未部署 | |
| 409 | `cooling` | 有部署的候選，但全部冷卻中 | `until`＝最早恢復時刻 |
| 400 | `no_model` | `model` 省略且推不出模型（決定 4） | |

- **為什麼排除冷卻、全部冷卻回 409 帶 `until`**：一次往返把「挑」做完（sidecar 不用自己迴圈試）；`until` 讓 sidecar 直接把自動重開 job 排到那個時刻（同今天「全部冷卻 → waiting-quota＋最早解除時刻」的行為）。
- **放棄的選項**：
  - get 語意（冷卻中照回、`available:false`、呼叫方自己決定）：呼叫方要自己迴圈，而且會拿到不該用的 key。「指定一把就算冷卻也照辦」的舊能力在 provider＋model 的介面裡沒有對應物，也沒有正當使用場景——冷卻＝額度已死，`until` 都給了。
  - 平均分攤（agent-lease 時代的選法）：v5 起沒有用量資料可攤；priority＋冷卻已達成「兩把都用得到」（一把額度死 → 冷卻 → 下一把自動接力）。

### 決定 4：model 省略時的語意（sidecar 現行的推導原封不動搬進來）

`model` 省略時：用選中那把的 `models.default`；default 為 null 時——kind 為 `claude-subscription` → 用 agent 自己的預設（`meta.model` null、env 不帶模型變數）；其他 kind → `models.allow` 第一個不含萬用字元的項；兩者都沒有 → 400 `no_model`（不代選）。

- **為什麼**：「不指定模型」的既有開法（例：worker 的 claude-code 兩把、zen 的預設）行為不變；規則與 sidecar 現行 `build_env` 的推導一致，搬的是同一個機械。
- **放棄的選項**：`model` 必填——`claude-subscription`「用 agent 自己的預設」就沒有入口，破壞現行用法；v5 spec（models.default 的語意）已定義 null 的意思，照用。

### 決定 5：跟 v5 的關係——並存遷移，終態 resolve 取代 get

- **並存期**：resolve 上線後 get 照常（舊 sidecar 釘在 worker 映像裡）；list、cooldown 不動；`lgw.client` 加 `resolve`、`get` 保留。
- **終態**：sidecar 遷移＋實機驗收後，另開 d 單移除 get 與 `lgw.client.get`；對外又是三個介面：**list、resolve、cooldown**。
- **list 不變的理由**：它是菜單與階梯的資料來源（priority、`models.allow`／default、cooldown、available）——「不指定 provider＋model 時 sidecar 挑預設」與「換把挑下一個」「自動重開算最早解除時刻」都還要它。
- **cooldown 不變的理由**：冷卻的主體是 subscription 的額度；resolve 的 meta 給 `subscription`，剛好接上（sidecar 換把時用它登錄）。
- **為什麼並存**：worker 映像裡的舊 sidecar 不能即時換；Gateway 先加後減，任何時刻都有一側可用；退版＝`install.sh --tag` 回舊 tag。
- **放棄的選項**：直接換掉（舊 sidecar 全 404，worker 停擺）；永遠並存（違反「必要且只有這些」）。

### 決定 6：對 sidecar 的介面需求與建議（給 MyLinuxPool 的 PM 對齊；本文只寫介面層級）

**需求**（這些成立，本設計才完整）：

1. `POST /agents`：`subscription: {"id": …}` 退場，改選填 `provider`＋`model`（形狀建議見未決 3；舊形狀照 #216 對 id 的先例硬切 400 `invalid_request`）。兩者可同時省略——sidecar 從 list 挑預設（priority＋可用性），再帶 provider＋model 去 resolve。
2. 來源（subscription source）：對 LLMGateway 改呼 list／resolve／cooldown；錯誤對照補 409 `cooling`（帶 `until` → 自動重開排到 until）、403 `model_not_allowed`、400 `no_model`（回給控制者）；其餘對照照舊。
3. driver：`build_env` 的職責改為「resolve 的 env＋sidecar 維運變數」；opencode 的 permission 併進 `OPENCODE_CONFIG_CONTENT`（歸屬見未決 1）；model-mismatch 檢查照舊（第一個 step 的模型＝`meta.model` 的期待值）。
4. `GET /agents` status：加 `provider`／`model`／`subscription`（echo meta；**不含 env 值**）。
5. agora 接續：換把後同 session continue 時，resolve 來的模型／憑證變數（例 `ANTHROPIC_MODEL`）照今天一樣要讓 `agora continue` 起的行程看到；#229 的互動終端路徑同樣只吃「疊好的 env」。
6. 值的紀律照舊並擴及 resolve 的值：不上命令列、不進 log、不寫進任何檔案、不進 status。

**建議**（非必須）：錯誤對照表補 `cooling`／`model_not_allowed` 的映射；`GET /jobs` 的 open job 顯示 provider／model。

**不撞進行中的工單**：#216（id／harness／身分）與 #229（互動 PM、hook 狀態）與本設計正交；唯一交會是 core 的 `POST /agents` 欄位白名單（ALLOWED_KEYS）兩邊都會動到，合併時對一次即可。

### 決定 7：跟 #32 的關係（介面與菜單；先後不互相依賴）

- **#33 先做也先解痛**：resolve 以精確模型名對 `models.allow` 樣式比對，現行 allow（`*-free`、`big-pickle`）已經點得到同一把的不同免費模型——審查換模型**不需要等 #32**。
- **#32 跟著補菜單**：把各 provider 的 allow 補上具體模型後，list 變成可列舉的菜單（人與 sidecar 都能枚舉「有哪些 model 可點」），也為 (provider, model) 粒度的換把階梯鋪路（未決 4）。
- **不互相依賴**：#32 只改政策檔內容（介面形狀不動）；#33 不改政策語意。要一起做的只有驗收時的範例模型選擇——#32 前後都能驗（用 allow 已蓋得到的模型）。
- **之後另開單**：(provider, model) 粒度的 fallback 階梯（例：Zen 免費 A 限流 → Zen 免費 B → 下一層），需要 #32 的具體清單＋sidecar 的階梯設定，還要考慮「接續 session 不能用 Zen 免費」的上游限制（Context）。

## Risks / Trade-offs

- **[env 組成與 agent 版本偏移]**（Gateway 獨立升級、agent 釘在 worker 映像）→ 組成只依各 agent 公開穩定的變數（`ANTHROPIC_*`、`OPENCODE_CONFIG_CONTENT`）；resolve 回覆帶 `schema`，sidecar 拒收更高的；worker 換釘 agent 版本時，resolve 的組成要跟著驗一輪（進該換釘單的驗收）。一人公司兩個 repo 都由本人協調，偏移窗口小。
- **[resolve 回覆帶值]** → 與 v5 get 同等的暴露面（SSH 通道內、無應用層認證——本人已接受的 v5 決定 5a）；紀律把稽核／log 納入「值不出現」的 canary 掃描。
- **[403 `model_not_allowed` 擋到人]** → 控制者點了不在 allow 的模型會被拒——這是政策守門的本意（例：免費額度外會計費的模型）；要開就改政策檔（本人），不是介面的事。
- **[選把移進 Gateway]** → 與 v5「Gateway 不挑把」的說法相反：輸入已是 provider＋model，「挑哪一把 subscription」無法留在呼叫方；規則是政策檔（priority＋冷卻）的機械套用，沒有新的裁量。
- **[同一把上多模型同時跑]** → 一把 subscription 上多個模型的額度是否獨立（例：Zen 免費層的限流單位）查無公開資料；冷卻以 subscription 為單位，可能「一個模型限流 → 整把冷卻 → 其他模型也被跳過」。這是現行 cooldown 粒度不變的已知限制；要細粒度等上游有 API（`quota-signals.md`：查無）。

## Migration Plan

1. **離線（不碰服務那台）**：本 repo 實作 resolve（決定 1–4）＋`lgw.client.resolve`＋測試（loopback＋假 key＋假時鐘；spec 每個服務端 Scenario；canary：env 值不出現在 log／資料庫／稽核／list 回覆）。全在測試程序裡跑。
2. **sidecar（MyLinuxPool，c 單）**：決定 6 的需求；單元測試用假來源；Mac 上臨時跑（loopback＋假 key 的 lgwd＋sidecar）驗「兩個模型、同一把」的形狀，跑完即停。
3. **實機（動服務那台與 worker；要本人授權，先問再動）**：
   - 3a 部署新版 lgwd（`install.sh --tag`，前後 `--check`；get 仍在，退版＝回舊 tag）；
   - 3b worker 驗收（建議標準）：經 sidecar 開一個**實作隊員**（`provider=opencode-zen`＋免費 A 模型，例：`mimo-v2.6-flash-free`）＋一個**審查隊員**（同 provider＋免費 B 模型，例：`big-pickle`；任何兩個 allow 涵蓋的不同模型皆可）——**兩個都拿到正確的模型**：status 的 model 各自正確、第一個 step 的模型＝所點（model-mismatch 通過）、稽核兩筆 resolve（同 subscription、不同 model）、log／status 無 env 值；
   - 3c 換把演練：假額度訊號 → cooldown（`meta.subscription`）→ resolve 下一個 → 同一個 agora session continue。
4. **退場**：另開 d 單移除 get（決定 5）；本 change 歸檔。

## Open Questions（未決，請本人裁定；每題附建議與理由）

1. **opencode 的 permission 歸屬**：建議 Gateway 組憑證＋模型、sidecar 併 `permission`（MyLinuxPool#233 的擁有權與值留在 sidecar；Gateway 不碰 opencode 的行為政策；sidecar 反正要解析 config 做 model-mismatch 檢查，併一個鍵不是新機制）。替代：Gateway 全組（sidecar 對 opencode 完全盲、更省事，但 opencode 的行為政策跨到 Gateway，且 Gateway 不知道 worker 釘的 opencode 版本對 permission 形狀的驗證）。→ 要 MyLinuxPool 的 PM 同意。
2. **get 的退場時點**：建議 sidecar 遷移＋實機驗收（3b／3c）過後就開 d 單移除，不要拖（並存介面越多，「必要且只有這些」破得越久）。
3. **`POST /agents` 的欄位形狀**：建議平面 `provider`＋`model` 兩欄位（閱讀最直覺、與 `session` 的字串語意一致）；替代 `model: {"provider": …, "model": …}` 物件（將來要加欄位時不用再改形狀）。→ MyLinuxPool 定，LLMGateway 無意見。
4. **換把階梯的粒度**：建議這次先維持 subscription 粒度（換把行為與今天相同）；(provider, model) 粒度（Zen 免費 A→B→下一層，還要處理「接續 session 不能用 Zen 免費」）等 #32＋sidecar 階梯設定，另開單。
5. **meta「到期」的語意**：本設計解讀為**冷卻的 `until`**（409 `cooling` 帶、list 帶、meta 帶 null）。若本人指的是別的到期（例：key 的到期），Gateway 目前沒有那資料（key-sync 只換檔、不記到期），要另做——請確認意圖。
6. **resolve 是否也收「不指定 provider」**（Gateway 連 provider 都代挑）：建議不要——issue 的輸入就是 provider＋model；不指定時由 sidecar 從 list 的 priority 挑好再 resolve，Gateway 的裁量不擴大。
