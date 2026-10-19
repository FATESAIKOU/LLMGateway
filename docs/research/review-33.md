DeepSeek V4.1 Flash

# #33 設計審查：agent 介面改成 provider＋model（獨立審查）

審查對象：branch `review/design-33`（commit `1f9993d`，同作者分支 `design/issue33-iface`）的
`openspec/changes/agent-provider-model-interface/`（proposal、design、tasks、
specs/subscription-registry/spec.md）與 `docs/research/design-33-overview.md`。只讀文件，
未改被審對象；未 push、未連遠端機器、未讀憑證檔。

驗證方式：`openspec validate --strict agent-provider-model-interface`（通過）；用
`policy/default.json` 跑真正的 `lgwd.policy.load_policy` 與 `fnmatch`；用暫存 DB 看
`lgwd.store` 的 audit 欄位；以唯讀方式讀 MyLinuxPool `origin/master` 的
`modules/sidecar/docs/sidecar.md`（§1、§2、§4–§7）；opencode 設定形狀以
`https://opencode.ai/config.json`（2026-10-20 抓）核對。沒有對服務那台或 worker 做任何操作。

結論先講：**需修**；高 1、中 10、低 12、資訊 6。

## 高

### 高-1　`provider=anthropic` 只剩 priority：指定 `claude-personal`／`claude-company` 的能力消失，設計未處理也未列入未決

- **位置**：`design.md:41`（`anthropic` 蓋兩把）、`design.md:79-81`（候選只按 provider／agent／allow／已部署→非冷卻→priority→id）、`design.md:97-99`（把「指定一把」貶為「沒有正當使用場景」）、`design.md:157-164`（六題未決沒有這題）；`proposal.md:9` 承認今天可指定 id。事實：`policy/default.json:4-13`（兩把同 provider、同 `agents`、同 `models.allow: ["*"]`、同 `default: null`，priority 10／20）、`lgwd/keys.py:14-16`（兩把是兩個不同的 key）、`lgwd/providers.py:50-55`（兩把都是 `AnthropicProvider`、`kind=claude-subscription`、`endpoint=null`）；sidecar 文件 §2.2（`subscription: {"id": …}` 選填，**指定的一把就算在冷卻中也照辦**）、§5（換把挑下一把）。

- **問題**：新介面只收 provider＋model；對 `provider=anthropic`，兩把的 `agents`、`allow`、`kind`、`endpoint` 都相同，唯一差別是 priority。resolve 在 `claude-personal` 的 key 已部署且未冷卻時永遠選 personal；`claude-company` 只有在 personal 冷卻或 key 未部署時才被選到。呼叫方無法要求「這次用公司帳號」或「這次用個人帳號」。設計把這個能力說成「沒有正當使用場景」，但：
  1. 公司／個人帳號的分離（計費、隱私、額度保留）本來就是兩把存在的理由；`openspec/specs/agent-lease/spec.md:37` 與 archive 的 v0 design（`openspec/changes/archive/2026-10-11-agent-lease-and-usage-record/design.md:5`）曾明說「兩把地位相同」，平均分攤是為了「兩把的額度窗口都用得到」；v5 改成 priority 後至少還保留 explicit id，本 change 把它完全移除。
  2. 指定一把也是除錯／驗收手段（例如只測 company token、只測某一把的冷卻反應）。
  3. 設計的「放棄的選項」只用「冷卻＝額度已死」反駁「指定冷卻中的一把」，但**帳號選擇與冷卻無關**；這是把兩個不同的能力一起丟掉。

- **為什麼是問題**：這是行為退步，而且會在「不該用個人帳號」時用個人帳號（或反之）；設計沒有在未決清單請本人裁定，實作照做後才發現就太晚。`provider=anthropic` 是目前唯一同 provider 多把的情況，但未來加 subscription 也會遇到同一個洞。

- **建議**：在 resolve 加**選填** `subscription` query 參數（不是新端點）：有給時必須是候選（provider 相同、`agents` 含 agent、model 被 `allow` 涵蓋、key 已部署），選它；冷卻中照樣回 409 `cooling`（若要保留「指定冷卻也照辦」，另開明確的 `force`，不建議這次做）。終態仍是 list／resolve／cooldown 三個介面，但保留帳號選擇。若本人堅持 provider＋model only，至少要把「personal 永遠優先、company 只在 personal 冷卻時使用」寫成明示決定，並把這題放進未決清單；替代是政策檔加「哪個 team／角色用哪一把」的規則，但 resolve 沒有身分，會擴介面。三者都比現在的沉默好。

## 中

### 中-1　env 值與稽核紀錄的 MUST 互相矛盾：model 同時是 env 值與稽核欄位

- **位置**：`spec.md:8`（`env` 的值 MUST NOT 出現在 list／稽核／log）、`spec.md:79`（稽核記 provider 與 model）、`design.md:65`（值不進 log／稽核）、`tasks.md:8-9`（env 值與 key 同等對待；canary 掃 env 值）。
- **問題**：`claude-code` 在 model 非 null 時，`env.ANTHROPIC_MODEL` 就是 `meta.model`；`opencode` 的 `OPENCODE_CONFIG_CONTENT` 內也含 model。稽核被要求記 model，於是「env 的值」必然出現在稽核。照字面實作，要嘛稽核不能記 model（違反同一份 spec 的稽核要求），要嘛 canary 會誤報；兩者都讓驗收無法通過。
- **為什麼是問題**：這是核心「值的紀律」條文的內部矛盾；實作與測試會各自解讀，最後可能為了過 canary 而刪掉 model 稽核，失去「誰用了哪個模型」的紀錄。
- **建議**：把條文限定為**憑證值**（key）：`env` 的憑證值 MUST NOT 出現在 list／稽核／log／status；model、provider、endpoint 是中介資料，稽核可記。canary 只掃 key 值（含 `OPENCODE_CONFIG_CONTENT` 內的 apiKey），不要掃整個 env map。同步改 `tasks.md:8-9` 與 overview 的 canary 說法。若真的要做到「任何 env 值都不進稽核」，則稽核只能記 subscription／agent，不能記 model；那要明說並放棄 model 稽核。

### 中-2　稽核表沒有 provider／model 欄位，tasks 也沒有 DB 遷移

- **位置**：`lgwd/store.py:23-30`（audit 只有 `seq/ts/subscription/action/agent/until`）、`:78-91`（INSERT 固定五欄）、`:93-98`（SELECT 固定六欄）；`spec.md:79`（稽核要 provider／model）、`tasks.md:8`（1.4 稽核）。
- **問題**：tasks 只說「resolve 記一筆（時刻、subscription、agent、provider、model）」，沒有說要改 `SCHEMA`／`audit_add`／`audit_all`。服務那台的既有 DB 是 v5 schema；`CREATE TABLE IF NOT EXISTS` 不會補欄位，resolve 第一次寫入就會 `no such column: provider`（或實作改成把 JSON 塞進現有欄位，但那不在設計裡）。
- **為什麼是問題**：這是 resolve 上線即壞的實作缺口；也會讓 rollback 到舊 tag 的相容性沒被驗證。
- **建議**：tasks 1.1／1.4 加「`ALTER TABLE audit ADD COLUMN provider TEXT`、`model TEXT`（nullable、additive；啟動時檢查 `PRAGMA table_info` 並補）」，`audit_add`／`audit_all` 加欄位，測試從一個 v5 schema 的 DB 檔啟動並寫入；保持 additive 以支援 `install.sh --tag` 退版。另加一筆「舊 DB 沒有新欄位」的測試。

### 中-3　`REMOVED get` 與「並存期 get 照常」互相打架；歸檔／套用時序沒有護欄

- **位置**：`proposal.md:3`（本 change 疊在未歸檔的 gateway-v0）、`design.md:108-115`（並存期 get 照常；終態另開 d 單移除）、`spec.md:113-117`（本 change 直接 REMOVED get）、`tasks.md:29-31`（get 退場是 d 子單）、`design.md:155`（本 change 歸檔）。
- **問題**：delta spec 已經把 get 列為 REMOVED，但設計說 get 在並存期仍要服務；如果本 change 在 d 單之前歸檔／套用，主規格會說 get 不存在，實作卻還有 get；如果 gateway-v0 還沒歸檔，main spec 根本沒有 get 可以 REMOVED（REMOVED 的基準是 gateway-v0 的 delta）。`openspec validate` 不會抓這個（它只驗 change 語法）。
- **為什麼是問題**：這是規格與實作的時序矛盾；也會讓 `openspec archive` 的結果取決於 gateway-v0 與 d 單的先後，而不是設計說的那樣。
- **建議**：二選一：(a) 把 `REMOVED get` 移到 d 單（本 change 只 ADDED resolve／MODIFIED 其他，get 保留在 main spec 直到 d 單）；(b) 本 change 的 scope 明確包含 get 移除，並把 d 單的工作併進來。不論哪個，tasks 加一個「歸檔 gate」：gateway-v0 已歸檔＋所有 worker 已遷移＋get 呼叫為零，才 archive。另把 `共通 JSON schema`／`稽核紀錄` 的「並存期」措辭改成過渡條款，避免它變成終態主規格。

### 中-4　sidecar 的「完整子行程環境」被寫成「resolve env＋維運變數」，可能漏掉 PATH／HOME／allowlist

- **位置**：`design.md:64`（sidecar 疊維運／行為變數）、`design.md:123`（build_env 職責改為 resolve env＋sidecar 維運變數）、`spec.md:42`（env 只含憑證／端點／模型；sidecar 疊維運變數）；事實：sidecar 文件 §1（`build_env` 回 credentials／env／model 三件組；子行程環境是「完整環境」加上 PATH／HOME 等）、§8（sidecar 自己環境的 `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 不會漏進子行程）。
- **問題**：設計的「疊」只列了 auto-update、server password、`AGORA_CLAUDE_CMD`、身分、permission；沒有說 sidecar 仍要提供 PATH／HOME 等 base env，也沒有說它對 resolve 的 env 是「不篩選地併入」還是「只取已知鍵」。照字面實作，子行程可能缺 PATH／HOME，或 Gateway 新增的鍵被 sidecar 的 allowlist 丟掉；反過來，若 sidecar 的 allowlist 放寬，sidecar 自己環境的 `ANTHROPIC_*` 可能漏進子行程，破壞「只注入這一把」。
- **為什麼是問題**：這是「env 的組成移進 Gateway」後最容易出錯的邊界；worker 上的 agent 起不來或拿到錯的憑證，會是實機驗收才發現。
- **建議**：在決定 2／6.3 明寫：sidecar 仍以現行「完整環境＋allowlist」為底（PATH／HOME／Locale 等），resolve 的 env 是**不篩選的 overlay**（或明列允許的鍵），sidecar 自己的 `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 仍不得進子行程；加一個測試：sidecar 行程自己有 `ANTHROPIC_API_KEY=canary`，子行程只看到 resolve 的值（或沒有）。這也讓「值只進子行程」的邊界可測。

### 中-5　實機驗收 3b／3c 缺新 sidecar 映像、Zen 接續限制、假額度訊號的做法

- **位置**：`design.md:10`（Zen 免費只收 opencode 原生新開 session，接續會被拒）、`design.md:150-154`（3b／3c）、`tasks.md:23-27`（3.1–3.3）；事實：sidecar 文件 §7（worker 的 sidecar 釘在映像裡，換版要重建 worker 映像）、§6（`new` 模式 vs `continue`）。
- **問題**：3.1 只部署新版 lgwd；3.2 假設 worker 上已經是「會用 resolve 的新 sidecar」，但 sidecar 在 worker 映像裡，計畫沒有「重建／部署含新 sidecar 的 worker 映像並驗 `sidecar --version`」這步。3b 用 `opencode-zen` 兩個免費模型；若驗收用既有的 agora session（continue／import），依設計自己引用的上游限制，Zen 免費模型會被拒，測試失敗原因不是介面。3c 的「假額度訊號」沒有說怎麼造假；sidecar 的訊號來自 agent 輸出，沒有測試接縫時很難在實機重現。
- **為什麼是問題**：驗收標準本身是對的（兩個隊員、兩個模型），但照這個計畫跑，可能因為映像沒換或上游限制而無法驗，或誤判成介面失敗。
- **建議**：在 3 之前加 3.0「建／部署含新 sidecar 的 worker 映像，`sidecar --version` 對上 MyLinuxPool 的 c 單 commit」；3b 明確用**新開 session**（省略 `session`）或改用不受 Zen 接續限制的 provider／模型；3c 用「手動 `POST /v1/subscriptions/{id}/cooldown` 模擬額度訊號」或 MyLinuxPool 提供的測試接縫，不要假裝 agent 訊號；並在 3b／3c 的通過條件註明「若 upstream 以 Zen 接續限制拒絕，這不是 resolve 的失敗，改用新 session／別家模型重跑」。

### 中-6　policy loader 沒有擋 `openrouter.agents`，resolve 的 agent 守門會放行

- **位置**：`lgwd/policy.py:127-134`（只檢查 claude／zen 的 `agents` 與 openrouter 的 `models.allow`，沒有檢查 `openrouter.agents`）、`policy/default.json:19-23`（目前是 `["opencode"]`）、`spec.md:100`（openrouter 的 `agents` MUST 只有 opencode）、`design.md:6`（政策檔事實）。
- **問題**：v5 spec 與 #33 的 MODIFIED `共通 JSON schema` 都說 openrouter 的 `agents` MUST 只有 opencode；但 loader 不驗，policy 檔若被改成 `["claude-code","opencode"]`，resolve 對 `provider=openrouter&agent=claude-code` 會回 200（`allow` 的 `*:free` 可能蓋到），把 Claude Code 指到 OpenRouter，違反條款／spec。
- **為什麼是問題**：resolve 把 `agents` 當成唯一的 agent 守門；政策檔是權威，但權威檔的約束沒有被程式強制，這是「政策守門」的缺口。
- **建議**：`load_policy` 加 openrouter 的 `agents == ("opencode",)` 檢查，加 `test_policy` 負向案例；若認為 openrouter 的 `agents` 不需要硬擋，則改 spec 的 MUST 措辭，不要讓 spec 與 loader 各說各話。

### 中-7　冷卻以 subscription 為單位，會把「同一把的另一個免費模型」一起擋掉；未決清單沒問

- **位置**：`design.md:145`（風險）、`design.md:162`（未決 4 只說先維持 subscription 粒度）、`quota-signals.md:60-67`（Zen 免費 429／`retry-after`）；sidecar 文件 §5（cooldown 以 subscription id 登錄）。
- **問題**：本單的痛點是「同一把 zen 上換另一個免費模型」；但一旦某個模型 429，sidecar 用 `meta.subscription` 登錄冷卻，resolve 會把整把 zen 排除，另一個免費模型也點不到。設計說「要細粒度等上游有 API」，但 sidecar 已經知道 `meta.model`，Gateway 也可以存 `(subscription, model)` 冷卻；這不是上游 API 的問題，是介面／儲存粒度的選擇。設計的未決 4 只問「換把階梯粒度」，沒有問「冷卻粒度」。
- **為什麼是問題**：使用者在意的「審查用不同免費模型」在第一次額度事件後就無法用；這會讓本單的價值打折，且使用者沒有被問過要不要接受。
- **建議**：加一個未決題：「冷卻要不要細到 `(subscription, model)`？」建議這次先維持 subscription 的排除規則（保守），但把 `meta.model` 一起登錄到冷卻紀錄，讓 Gateway 保有細粒度的資料；下一單再決定是否只排除該 model。或至少把「同一把的另一個模型也會被冷卻」寫成明示限制。3b 仍可照做，因為它證明的是**初始選擇**（兩個模型、同一把）；但不要把它當成「額度事件後還能換模型」的證明；若要涵蓋後者，另加 3d 或明確寫出這是不做的範圍。這一點要請本人裁定。

### 中-8　resolve 沒有「排除剛用完那一把」的參數；冷卻登錄失敗時會原地打轉

- **位置**：`design.md:79-81`（候選只排除冷卻）、`design.md:121-122`（sidecar 換把：cooldown→resolve）、sidecar 文件 §5（現行「不是剛用完那一把」；「登錄失敗也繼續換把」）。
- **問題**：現行 sidecar 換把時會跳過剛用完的 subscription，即使 cooldown 登錄失敗；新設計把「跳過」寄託在 cooldown 成功上。若 `POST /cooldown` 失敗（400／409／網路），resolve 會再選回同一把（personal 未冷卻、priority 10），sidecar 可能立刻重開同一把，形成迴圈。
- **為什麼是問題**：這是換把路徑的退步；sidecar 的現行行為明確寫了「登錄失敗也繼續換把」和「不是剛用完那一把」，設計沒有對應。
- **建議**：resolve 加選填 `exclude=<subscription id>`（可重複），或 sidecar 在 cooldown 失敗時不要換把（停下、回報、重試 cooldown）；兩者選一並寫進決定 6.2。若加 `exclude`，它是查詢參數、不是新端點，仍符合「三個介面」。

### 中-9　sidecar `POST /agents` 的遷移規格不足：`subscription` 硬切、`model` 命名未定

- **位置**：`design.md:121`（舊形狀硬切 400）、`tasks.md:16`（2.2）、`design.md:161`（未決 3 只問平面 vs 物件）、`overview.md:71`（resolve 的 model 是供應商 id）、`spec.md:107`（model 不帶前綴）。
- **問題**：(a) `subscription: {"id": …}` 是現行有效功能，硬切 400 會讓所有既有呼叫端（PM 腳本、dispatch、驗收腳本）在同一天壞掉；設計只說「照 #216 對 id 的先例」，但 #216 切的是**不合法**的 id，這裡切的是**合法**功能，類比不成立。設計沒有呼叫端清冊、沒有過渡期。(b) 未決 3 只決定形狀（平面 vs 物件），沒有決定 `model` 的命名；PM 很可能傳 opencode 的完整 id `opencode/big-pickle`，resolve 拿它去對 `*-free`／`big-pickle` 會 403。設計應明說 sidecar 的 `model` 是供應商 id，或由 sidecar 正規化（去掉 provider 前綴）並回清楚錯誤。
- **為什麼是問題**：這是兩個 repo 的介面交接點；規格沒定，實作就會各自猜，最常見的錯就是 `model` 前綴。
- **建議**：tasks 2.2 加「呼叫端清冊＋過渡期（至少一個 release 接受舊 `subscription` 並映射到 provider＋model，或先回 400 帶明確訊息）」；(b) 在未決 3 或決定 6.1 明寫 `model` 是供應商 id（`big-pickle`、`openrouter/free`），sidecar 若收到 `<provider>/<model>` 要正規化或回 400；加一個 sidecar 測試。

### 中-10　`schema` 只版本化回覆形狀，沒有版本化 env 的組成；Gateway 獨立升級可能讓舊 worker 的 agent 拿到不同變數

- **位置**：`design.md:141`（風險：resolve 帶 schema，sidecar 拒收更高；組成只依公開穩定變數）、`spec.md:8`（schema）、`design.md:68`（組成集中後上游／政策變了全體跟上）。
- **問題**：如果 Gateway 改的是「同一個變數的值」或「新增／移除一個變數」（例如 `ANTHROPIC_SMALL_FAST_MODEL` 不再給、或 opencode 的 config 形狀改了），回覆形狀沒變，`schema` 仍是 1，sidecar 不會拒收；舊 worker 的 agent 版本可能不認得新變數或需要舊變數。設計的緩解「worker 換釘 agent 版本時驗一輪」只涵蓋 worker 換版，沒有涵蓋 Gateway 獨立換版。
- **為什麼是問題**：這正是 v0 決定 7 當初把 env 名留在 sidecar 的理由（env 名跟 agent 版本走）；反轉它就要有版本護欄，否則偏移窗口從「worker 換版」變成「任何 Gateway 換版」。
- **建議**：在 resolve 回覆加 `env_schema`（或把 `schema` 的語意定義為「回覆＋env 組成」並在任一變數增刪時 bump），sidecar 對不認得的 `env_schema` 拒收；或把 env 的鍵集寫成一個版本表，Gateway 只在同一 `env_schema` 內改值、不改鍵。加一個測試：`env_schema` 升高時 sidecar 拒收。這比「只依公開穩定變數」具體。

## 低

### 低-1　`GET /agents` status 的 `subscription` 不是新欄位

- **位置**：`design.md:124`（決定 6.4 說「加 provider／model／subscription」）vs sidecar 文件 §2.1 的 agent 物件已有 `"subscription": "claude-personal"`。
- **問題**：`subscription` 已經存在；寫「加」會讓實作重複加或文件與現況不符。
- **為什麼是問題**：小，但這是介面事實；tasks 2.5 也會照抄。
- **建議**：改成「保留 `subscription`，加 `provider`／`model`」；tasks 2.5 同步。

### 低-2　overview 的 `POST /agents` 示意缺必填 `id`／`workdir`

- **位置**：`overview.md:15-16`、`overview.md:41-42`。
- **問題**：`POST /agents` 的示意缺了 `id`（必填、唯一）；實體圖有 `workdir`，時序圖則把 `workdir`／`session`／`input` 也省了（後三者是示意可接受，但 `id` 是必填，不該省）。
- **為什麼是問題**：讀圖的人可能照抄成不完整請求；`id` 是 sidecar 的必填欄位。
- **建議**：兩張圖都補 `id`；時序圖若要省略其他欄位，加一句「其餘欄位省略」。

### 低-3　`no_model` 在選中後才判斷，可能錯過另一個能推導模型的候選

- **位置**：`spec.md:23`（候選沒有「能推導模型」條件）、`spec.md:62`（選中後才 no_model）。
- **問題**：若最高 priority 的候選 `default` 為 null 且 `allow` 全萬用字元，但下一個候選有 `default`，resolve 會 400 `no_model`，不會試下一個。
- **為什麼是問題**：對呼叫方來說「這個 provider 有可用模型」卻被拒；目前五把不會觸發（anthropic 是 `claude-subscription`、其他 `default` 非 null），但 #32／未來加 subscription 會。
- **建議**：把「能推導模型」加進候選條件（`claude-subscription` 或 `default` 非 null 或 `allow` 有具體項），或在 `no_model` 前換下一個候選；加一個政策替身測試。

### 低-4　未知 `agent` 會回 403，而不是 400

- **位置**：`design.md:40`（agent 只能是 `claude-code`／`opencode`）、`design.md:88-90`（400 只列缺欄位）。
- **問題**：`agent=foo` 會走到 `agent_not_allowed` 403，而不是 400 `bad_request`；403 的語意是政策不允許，不是輸入錯誤。
- **為什麼是問題**：控制者打錯字會看到「政策不允許」而不是「你打錯 agent」；sidecar 的 `POST /agents` 對不支援的 agent 是 400。
- **建議**：resolve 先驗 agent 列舉，未知回 400 `bad_request`；或明說沿用 v5 的 403 並在錯誤碼表加註。

### 低-5　`schema: 1` 同時代表 list row 與 resolve 回覆，語意不清

- **位置**：`design.md:38`、`spec.md:8`。
- **問題**：resolve 的回覆形狀（`{schema, meta, env}`）與 list／get 的 row 不同，但都用 `schema: 1`；sidecar 的「拒收更高」檢查無法區分這是哪一種 schema。
- **為什麼是問題**：未來改 resolve 形狀時，bump 1→2 會讓只認 list schema 的舊 sidecar 也拒收；不 bump 則新舊 sidecar 分不出。
- **建議**：定義 `schema` 是「這個端點的回覆版本」並在 resolve 形狀變時 bump；或加 `kind:"resolve"`／`resolve_schema`。文件寫清楚。

### 低-6　resolve 的 200 body 含 key，但沒有 `Cache-Control: no-store`

- **位置**：`lgwd/http.py:49-58`（只設 Content-Type／Content-Length）、`design.md:142`。
- **問題**：resolve 的 200 body 含 key，GET 回應沒有 `Cache-Control: no-store`；若有 HTTP 快取／中介（目前是 SSH 直通，風險低）或本機工具快取，可能留下值。
- **為什麼是問題**：與「值只在子行程環境」的紀律不一致；get 目前也有同樣問題，但 resolve 是新的值出口，順手補。
- **建議**：resolve（以及 get）回應加 `Cache-Control: no-store`、`Pragma: no-cache`；測試斷言標頭。

### 低-7　opencode 的 `provider`／`small_model` 在 v2 schema 已標 deprecated

- **位置**：`design.md:61`、`spec.md:45`；opencode config schema（2026-10-20 抓，`https://opencode.ai/config.json`）把 top-level `provider` 標 `deprecated`、`small_model` 也 `deprecated`，新的 `providers` 用 `settings` 而非 `options`。
- **問題**：設計的 env 組成用 legacy `provider`／`small_model`；pinned 2.0.26 可能仍接受，但 Gateway 獨立升級、worker 版本不動，未來 opencode 移除 legacy 鍵時，resolve 的 config 會失效。
- **為什麼是問題**：這是 env 組成集中到 Gateway 後的版本耦合；設計的 Risk 只說「公開穩定變數」，沒有說 config 鍵的 deprecation。
- **建議**：在 Risk／Migration 註明「以 worker 釘的 opencode 版本為準；升級 opencode 時要驗 `provider`／`small_model` 仍被接受；若要用 `providers`，先在 pinned 版本驗」。加一個 schema／實機測試；不要現在就換，除非 pinned 版本支援。

### 低-8　query 只講了 `/`，`+`／`&` 的編碼沒講

- **位置**：`design.md:45,49`。
- **問題**：設計說 model 可含 `/`，放 query 不必處理路徑 percent-encoding；但 query 還有 `+`（`parse_qs` 會解成空白）、`&`、`#` 等；`lgw.client` 要對 model 做 `quote`（`safe=""`），server 的 `parse_qs` 才會拿到原值。
- **為什麼是問題**：模型 id 可能含 `+` 或 `:`；`+` 被解成空白會導致 403 或錯誤模型。
- **建議**：在決定 1 的「為什麼 GET＋query」補「client MUST percent-encode；server 用 `parse_qs` 預設 form 解碼，`+` 要編成 `%2B`」；`lgw.client.resolve` 用 `urllib.parse.quote(..., safe="")`；加一個 `model=a+b` 的測試。

### 低-9　overview 時序圖的「(provider, model)〔或 subscription〕」與 resolve 介面不一致

- **位置**：`overview.md:56`。
- **問題**：這句話暗示換把可以按 subscription 挑，但 resolve 不收 subscription；若照未決 4 的建議維持 subscription 粒度，sidecar 從 list 挑到某把後，只能用它的 provider＋model 去 resolve，resolve 可能再挑別把（priority 更高、未冷卻），兩邊的選擇不一致。
- **為什麼是問題**：這是控制時序圖與介面的矛盾；實作 sidecar 的人會不知道該以 list 的選擇為準還是 resolve 的結果為準。
- **建議**：改成「list 取候選 → 以該候選的 provider＋model resolve；resolve 的 `meta.subscription` 為準；若與 list 的選擇不同，記 log／回報」；或若真的要指定 subscription，見高-1 的選填參數。

### 低-10　overview 說 canary「守著」sidecar 的 log／status，但本 repo 的 canary 掃不到

- **位置**：`overview.md:90-91`。
- **問題**：說「canary 掃描守著」sidecar 的 log／status；但本 repo 的 canary 只能掃 Gateway 的 log／DB／稽核／list，sidecar 的 log／status 在 MyLinuxPool，需另一個 repo 的測試。
- **為什麼是問題**：會讓人以為 #33 的測試已涵蓋 sidecar；實際上 sidecar 的值紀律要在 MyLinuxPool 的 c 單驗。
- **建議**：改成「Gateway 的 canary 掃 Gateway；sidecar 的 log／status 由 MyLinuxPool 的 c 單加 canary」，並在 tasks 2.6 加一條 sidecar canary。

### 低-11　tasks 的實機驗收沒有固定層級／擁有者

- **位置**：`tasks.md:23`。
- **問題**：3 說「掛在 2 的驗收節，或 MyPMO 掛 d 單」，沒有固定的層級／擁有者；工單規則要求每個子單有層級（c／d）。
- **為什麼是問題**：PM 無法據此開單／排程；驗收可能沒人做。
- **建議**：定為「MyLinuxPool c 單的驗收節（2 的 acceptance）」或「本 repo 的 d 單」，二選一；若是前者，LLMGateway 的 tasks 只留「提供驗收腳本／檢查清單」，不動服務那台。

### 低-12　`meta.cooldown`／`meta.available` 是恆定值，違反「必要且只有這些」

- **位置**：`design.md:43`、`spec.md:20`。
- **問題**：resolve 200 的 `meta.cooldown` 恆 null、`meta.available` 恆 true；它們不帶資訊，卻要測、要維護，違反「介面必要且只有這些」。
- **為什麼是問題**：使用者原則明確說「必要且只有這些」；保留是為了與 list 同形，但 resolve 的回覆本來就不是 list row（多了 env、少了 key），同形的理由不強。
- **建議**：要嘛移除這兩個欄位，要嘛在 meta 只保留有資訊的（subscription、provider、kind、agent、model、models、endpoint、priority）；若真的要和 list 同形，則在文件明說它們是恆定值、不作為判斷依據。

## 資訊

- **資訊-1**：`openspec validate --strict agent-provider-model-interface` 存在且通過（`Change 'agent-provider-model-interface' is valid`，exit 0）。這只驗 change 語法，不驗 M3 的歸檔時序。
- **資訊-2**：設計對「現行 v5」的主要事實與 `policy/default.json`、`lgwd/`、gateway-v0 的 delta spec 一致：五把 subscription、priority 10／20／10／20／30、`agents`、`models.allow/default`、list／get／cooldown 三個端點、row 欄位、`cooldown{until|null}`、`available`、`key` 只在 get、get 冷卻中仍回、`models.default` 的 null 語意。在可對照的範圍內沒有發現事實錯誤；sidecar 的內部細節另列「沒查到的地方」。
- **資訊-3**：`LGW-V5-INTERFACE.md` 在本 repo 與 MyLinuxPool `origin/master` 都找不到（與作者 overview 的說法一致）；sidecar.md §4 的端點／欄位表足以交叉核對，沒有缺口。
- **資訊-4**：opencode v2 config schema 確認 `OPENCODE_CONFIG_CONTENT`、`provider.<id>.options.apiKey`、`provider.<id>.models.<model>.name`、`model`、`small_model` 都是合法鍵；設計的 env 形狀可實作。但 `provider`／`small_model` 已 deprecated（見低-7）。
- **資訊-5**：`openspec/specs/agent-lease/spec.md:37` 仍要求 Claude 兩把「地位相同、平均分攤」；這是 v0 的主規格，gateway-v0 的 delta 已把它列為 REMOVED，但 gateway-v0 尚未歸檔。設計只說「本 change 疊在 gateway-v0 上」，沒有提到這個並存狀態；在 gateway-v0 歸檔前，repo 裡同時有「平均分攤」與「priority」兩套選把規則。建議在 proposal／tasks 加一句「gateway-v0 歸檔前，main spec 的 agent-lease 仍是舊語意」或先把 gateway-v0 歸檔。
- **資訊-6**：sidecar 文件 §2.2 的錯誤格式是 `{"error": {"code": "...", "message": "..."}}`，與 Gateway 的 `{"error": "<碼>"}` 不同；設計 6.2 的「錯誤對照」只說補碼，沒有說 sidecar 回應給控制者的形狀。這不是 #33 的錯，但兩邊的文件放在一起看容易混；建議在 6.2 加一句「sidecar 對外仍用 sidecar 的巢狀 error 形狀，碼是內部映射」。

## 沒查到的地方

- **sidecar `build_env` 的內部細節**：`ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL` 是否由現行 driver 設定、`models.default` 為 null 時「`allow` 第一個不含萬用字元的項」的實際順序、`OPENCODE_CONFIG_CONTENT` 除 `apiKey`／model／permission 外還帶什麼——sidecar.md 只寫介面層級（key 的三個變數、`OPENCODE_CONFIG_CONTENT` 的 apiKey、`OPENCODE_SERVER_PASSWORD`、permission），沒有這些實作細節。依 prompt 只讀 sidecar.md，故標「查不到」，沒有拿作者結論當事實。
- **MyLinuxPool#216 的 `harness` 欄位、#229 的終端／hook 細節、core 的 `ALLOWED_KEYS` 符號**：master 的 sidecar.md 沒有這些字樣；design／overview 的說法來自 issue 內文或後續 PR 的預期，未讀到實作，標「查不到」。
- **`LGW-V5-INTERFACE.md`**：兩邊 repo 都找不到（與作者一致）。
- **worker 上 pinned 的 opencode 版本對 legacy `provider`／`small_model` 的接受度**：只以 opencode 官網 schema（2026-10-20）驗形狀；沒有實機 binary 可驗。sidecar.md 說 worker 釘 2.0.26、Mac 2.0.32，但沒有說這兩個版本對 deprecated 鍵的行為。
- **`#33` 的 issue 全文與「meta 到期」的原意**：只有工單標題與 prompt 摘要，沒有讀 issue 內文；design 的未決 5（「到期」是否指冷卻 `until`）無法核對。
- **OpenSpec 的 `archive`／`apply` 實際行為**：只跑了 `validate --strict`，沒有真的 archive／apply（避免改動被審對象與主規格）；M3 是依 OpenSpec 的 delta／REMOVED 語意推理，沒有用 `openspec archive` 實測。
- **服務那台與 worker 的現況**：沒有連線、沒有讀 `keys.env`、沒有跑 `install.sh --check`；所以 M2／M5 是從程式與文件推論，沒有實機證據。

## 驗證紀錄（摘要）

```
$ openspec validate --strict agent-provider-model-interface
Change 'agent-provider-model-interface' is valid   # exit 0

$ python3 - <<'PY'  # 真 loader + fnmatch
claude-personal 10 ('claude-code',) ('*',) None
opencode-zen 10 ('opencode',) ('*-free', 'big-pickle') mimo-v2.6-flash-free
claude-company 20 ('claude-code',) ('*',) None
openrouter 20 ('opencode',) ('*:free', 'openrouter/free') openrouter/free
ollama-cloud 30 ('claude-code', 'opencode') ('*',) deepseek-v4.1-flash
fnmatch big-pickle: *-free=False, big-pickle=True
fnmatch openrouter/free: *:free=False, openrouter/free=True
KEY_NAMES: claude-personal/CLAUDE_PERSONAL_OAUTH_TOKEN, claude-company/CLAUDE_COMPANY_OAUTH_TOKEN, ...
audit columns: seq, ts, subscription, action, agent, until
PY
```

上述 `audit columns` 是 `PRAGMA table_info(audit)` 的實際輸出，證實中-2 的欄位缺口。

結論：需修
