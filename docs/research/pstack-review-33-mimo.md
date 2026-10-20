MiMo-V2.6-Flash（mimo）

# #33 設計審查（第 4 輪，pstack）：provider＋model 介面

審查對象：worktree `review-33p`（branch `review/design-33`）的
`openspec/changes/agent-provider-model-interface/`（proposal／design.md 223 行／tasks／specs/subscription-registry/spec.md）與
`docs/research/design-33-overview.md`。對照現況：`lgwd/`（http／registry／providers／policy／store／errors／main）、`lgw/client.py`、`policy/default.json`。
只讀；未改被審對象、未 commit、未讀憑證檔、未連遠端機器。前三輪（review-33／r2／r3）已修項目不重提；本人 2026-10-20 七題裁定不挑戰。

結論先講：**需修**；critical 0、warning 3、nit 6。

## Findings

### 1. [warning] resolve 404→get 的回退拿「404」當舊 Gateway 的判別訊號，但 404 語意過載，對新形狀請求未定義
**Location**: design.md:172（決定 6.2「resolve 404 時回 get」）、tasks.md:21（2.1）；對照 lgwd/http.py:113-117、lgwd/errors.py:55-57
**Finding**: sidecar 對 resolve 的 404 一律回 get。但舊 Gateway 對不存在的路徑回的就是 404 `{"error":"not_found"}`（http.py 對 `_match_route` 回 None 的路徑回 not_found），新 Gateway 對未知 provider／未知 subscription id 回的也是 404 `not_found`——狀態碼與 body 完全相同，sidecar 無法分辨「這台是舊 Gateway」與「這次輸入錯了」。而且對**新形狀**請求（只有 provider＋model、沒有 subscription id）這個回退沒有可執行的動作：get 需要 subscription id，sidecar 手上沒有（6.1 的組合矩陣已把「只給 subscription」的舊形狀直接導向 get）。6.2 的回退實際上只在「新 sidecar＋退版 Gateway＋新形狀請求」這個組合下被觸發，而那正是它做不了的組合。
**Evidence**: (a) 兩種 404 的 body 逐位元相同，唯一差異在**觸發條件**而不是回覆；(b) 結果有二：真輸入錯誤（打錯 provider）被靜默轉進舊流程或未定義的例外，除錯時看到的不是 resolve 的 404；退版後每筆新形狀請求都先打一次注定 404 的 resolve 才回退，錯誤驅動的控制流把「版本偵測」散進每個請求。
**Suggestion**: 把判別改成一次性的能力探測：sidecar 嘗試 `GET /v1/resolve`（不帶參數）——舊 Gateway 回 404（路徑不存在），新 Gateway 回 400 `bad_request`（缺 agent／provider，spec 的缺必填場景）。探到舊 Gateway 就進 sticky 的 v5 模式（list→挑→get）並記 log；探到新 Gateway 則 resolve 的 404 一律視為輸入錯誤、不再回退。「支援退版」從每請求的錯誤分支收成一個啟動時的狀態。

### 2. [warning] `schema` 的「形狀改變 MUST bump＋sidecar 對高版本拒收」把 meta 的 additive 變更變成部署 breaking，與反轉 v0 決定 7 的理由（sidecar 不動、Gateway 獨立升級）直接打架
**Location**: spec.md:17（`schema`／`env_schema` 條文）、design.md:54-56；對照 design.md:93-96（反轉理由）
**Finding**: 規格現在就寫死「resolve 形狀改變時 MUST bump」「sidecar MUST 對不認得的更高 `schema` 拒收」，而「meta additive 變更放寬成讀已知欄位、忽略新欄位」被寫成「未來保留的空間」（design.md:56）。今天之後任何 meta 加欄位都要 bump `schema`，舊 sidecar 一律拒收 200——worker 映像不動就不能升 Gateway 的 meta。這正是本設計要反轉的 v0 限制：design.md:94 說「加 provider＝改 Gateway，sidecar 不動」，同一份 spec 卻讓「meta 加一個欄位」需要 sidecar 跟著動。
**Evidence**: design.md:94「移進 Gateway 後，加 provider＝改 Gateway（政策＋providers＋env 組成），sidecar 不動（opencode 的 config 形狀仍由 sidecar 依版本組，這是刻意的邊界）」；spec.md:17 卻要求 meta 形狀任何變更 bump＋嚴格拒收。兩個陳述對「Gateway 獨立升級」給出相反的部署後果，而嚴格版是寫進 spec、會成為驗收依據的那一版。放寬被推給未來， trap 卻已入規格。
**Suggestion**: 現在就把 `schema` 的相容規則寫成 additive-tolerant：meta 欄位只加不改不 bump（sidecar MUST 忽略不認識的 meta 欄位——JSON 消費者本來就該如此）；移除／改名／改語意才 bump，那時才拒收。`env_schema` 的嚴格護欄維持不動（鍵集變更真的不相容）。這與 OQ 7 的兩欄裁定相容，且讓兩欄的分工與「sidecar 不動」的理由一致。

### 3. [warning] delta spec 寫進一批「sidecar MUST…」的消費端義務；歸檔後主規格會把別人的系統寫成 LLMGateway 的義務
**Location**: spec.md:17（sidecar MUST 拒收高版本）、spec.md:63-65（「env 組成版本升高」Scenario）、spec.md:125（sidecar 對未知鍵 MUST 忽略＋WARN）、spec.md:127（sidecar 組 config SHALL、子行程環境 MUST NOT 漏進繼承變數）、spec.md:137-139、141-147（三個 sidecar 行為的 Scenario）
**Finding**: 這份 spec.md 是要歸檔進 LLMGateway 主規格 `subscription-registry` 的 delta，裡面卻有至少四處以 sidecar 為主詞的 MUST/SHALL 與四個只驗得到 sidecar 的 Scenario（拒收版本、組 config、未知鍵、只注入這一把）。LLMGateway 的實作與測試（tasks 1.0–1.7）無法滿足、也無法驗證這些條文；對應工作全在 MyLinuxPool 的 c 單（tasks 2.3／2.6）。歸檔後，照主規格檢查的人或 agent 會把「LLMGateway 沒做未知鍵 WARN／沒拒收高 env_schema」列為缺口。
**Evidence**: 與「值的紀律」同類的消費端規則在 design 決定 2（L77-91）已完整寫過一次；spec 再抄一遍，同一決定有了第二個 source of truth，且抄進了會歸檔的那一份。tasks 1.x 沒有任何一項對應這些 MUST——它們在本 repo 不可驗證，這是「驗證」鏡頭下的缺口（規格聲稱的義務沒有任何測試線）。
**Suggestion**: delta 裡把消費端條文收成一句「`env` 的消費規則（未知鍵、overlay、config 組裝、版本拒收）見 design 決定 6，由 sidecar 側規格承接」，MUST 與 Scenario 移到 MyLinuxPool 的單／規格；subscription-registry 只留 Gateway 自己能實作與驗證的義務。

### 4. [nit] 空白與重複 query 參數的語意未定，`parse_qs` 預設把 `exclude=` 當沒給
**Location**: spec.md:11（「未知 query 參數 SHALL 忽略」）、tasks.md:12（`parse_qs` form 解碼）、design.md:57
**Finding**: `parse_qs` 預設 `keep_blank_values=False`：`?subscription=claude-company&exclude=` 的 `exclude` 會被丟掉，請求變成合法釘住，**不會**撞 spec 的「`subscription` 與 `exclude` 同時給 MUST 400」；`?model=` 同理變「省略 model」。重複參數（`model=a&model=b`）回 `['a','b']`，取哪個沒寫。三輪審查處理了 `+` 的編碼，沒有處理空白值與重複鍵。
**Evidence**: tasks 1.3 把 server 側解碼釘成 `parse_qs` 預設行為，所以這個邊界是規格性的（不是實作細節）；spec 的 400 規則對「給了但空」沒有答案。
**Suggestion**: spec 補兩句寫死：空值＝沒給（並註明 `subscription=x&exclude=` 因此是合法釘住）或空值＝400，二選一；重複參數取第一個或 400。tasks 1.3 加對應測試。

### 5. [nit] 「排除後沒有候選」的 Scenario 用了規則上會落到別的錯誤碼的「不可用」
**Location**: spec.md:43-45
**Finding**: Scenario 寫「`claude-company` 不存在或**不可用** → `no_candidate`」。照選把規則：company 若只是冷卻中（未被 exclude），斷在第 7 步 → 409 `cooling`；key 未部署 → 409 `not_deployed`；只有「不在政策檔」才走第 5 步的 `no_candidate`。驗收者照 Scenario 字面寫測試會與規則衝突——這正是前兩輪反覆修的「Scenario 與規則各說各話」型別。
**Evidence**: spec.md:75-77（選把步驟 5-7）與 spec.md:44（Scenario 的「或不可用」）。
**Suggestion**: 把「或不可用」刪掉，或改成「`claude-company` 不在政策檔」。

### 6. [nit] 稽核「每次 resolve」是否含失敗的 resolve 未定
**Location**: spec.md:169、tasks.md:13
**Finding**: 現行 get 只在成功路徑記稽核（registry.py:76-78，檢查全過才 `audit_add`）。spec 的「每次 resolve…SHALL 記一筆」照字面含 4xx，但 `no_candidate`／`agent_not_allowed` 根本沒有選中的 subscription id 可記（規格同時要求記 subscription id）。兩種讀法導出不同的實作與稽核量，canary 與「稽核兩筆 resolve」的驗收（tasks 3.2）也會不同。
**Evidence**: spec.md:169 vs registry.py 的既有慣例；spec 的 scenario 只覆蓋 200。
**Suggestion**: 寫明「只有 200 的 resolve 記稽核（與 get 同）」；若真要記失敗，另定沒有 subscription 時記什麼。

### 7. [nit] env 組成的落點在 tasks 裡還懸而未決（`providers.py` 或新 `envs.py`）
**Location**: tasks.md:11（1.2「`lgwd/providers.py`（或新 `envs.py`）」）
**Finding**: 組成的輸入是（provider、kind、agent、model）；kind／endpoint 已是 `CredentialProvider` 的欄位（providers.py:13-22），`OPENCODE_PROVIDER_ID` 也是 provider 的靜態描述。放進 providers.py 讓每個 provider 自帶 `build_env(agent, model)`，選擇自然多型化；另開 `envs.py` 要嘛複製一份 provider 事實、要嘛反向 import，多出一個平行的條件表（model-the-domain 說的「if/else 再長一條」）。設計既強調「介面與實作分離」，不該把落點留給實作當場猜。
**Evidence**: tasks.md:11 的「或新 envs.py」；providers.py 現況（describe() 已是同一族事實的家）。
**Suggestion**: tasks 1.2 定案 `providers.py`（`CredentialProvider.build_env(agent, model)`），刪掉「或新 envs.py」的選項。

### 8. [nit] `lgw.client.resolve` 的 `exclude` 參數型別（清單或逗號字串）未定
**Location**: tasks.md:15（1.6）
**Finding**: resolve 的 `exclude` 是「逗號分隔的 id 清單」，client 簽名寫 `exclude=None`；take list+join 還是 take string，會決定測試腳本／PM 腳本怎麼傳，也決定要不要防「id 內含逗號」（design 已說值內逗號不合法）。
**Evidence**: tasks.md:15 與 design.md:51 的參數定義只有一邊講了形狀。
**Suggestion**: 寫死 `exclude: list[str] | None`，client 負責 `",".join` 並對每個 id `quote(safe="")`。

### 9. [nit] precedence 的「輸入形狀 400 先於查無 404」在「未知 subscription id＋未知 provider」時無法照字面成立
**Location**: spec.md:80、207-209；design.md:134-135
**Finding**: 「`subscription` 與 `provider` 不一致」的檢查必須先知道那把的 provider；id 未知時無從查起，實作只能回 404。spec 的兩個 precedence 例子（`NoSuchProvider&subscription=claude-personal`、`未知 id&exclude=x`）都避開了這個組合，但規則文字「輸入形狀先於查無」涵蓋不到它，字面讀者會期待 400。
**Evidence**: design.md:50（不一致→400 需要該把存在才能算）、design.md:127（未知 id→404）；兩者的交集組合沒有條文。
**Suggestion**: 補一句「不一致檢查只在 `subscription` 存在時成立；id 未知回 404」，或在 precedence 段舉這個反例。

## 其他核對（Code Quality Lens 的機械檢查；無發現）

- **檔案行數**：`wc -l`——http.py 150、registry.py 151、providers.py 61、store.py 99、policy.py 141、client.py 119。tasks 1.1–1.4 的增量（resolve 路由、選把、env 組成、稽核欄位）就算全疊上去也遠低於 1000 行；本輪沒有 1000 行的問題。
- **啟動路徑可銜接**：tasks 1.0 的 `validate_endpoints` ValueError→`PolicyError` 與 main.py:290 的 `except (PolicyError, …)` 一致——「不合法不啟動」在現行程式成立；store 的 additive 遷移（PRAGMA＋ALTER）與 `CREATE TABLE IF NOT EXISTS`、舊 lgwd 的指定欄位 SELECT 相容。
- **與裁定的關係**：本輪 9 條都沒有挑戰七題裁定（釘住不換把、冷卻粒度、personal 先、並存期、meta 到期、provider 必填、兩欄版本），也沒有 [衝突裁定N] 項。
- **public repo 規矩**：被審文件掃過，無金鑰值、無私人資訊；工單代號引用可放行。

結論：需修（critical 0、warning 3、nit 6）。
