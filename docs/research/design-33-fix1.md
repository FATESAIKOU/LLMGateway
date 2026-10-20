DeepSeek V4.1 Flash

# #33 設計修訂報告（fix1）

審查對象：`branch design/issue33-iface`（基準 `1f9993d`）的 `openspec/changes/agent-provider-model-interface/` 與 `docs/research/design-33-overview.md`。本輪只改文件，未改程式；未 push、未對 main commit、未讀憑證檔、未連遠端機器。

驗證：`openspec validate --strict agent-provider-model-interface` → `Change 'agent-provider-model-interface' is valid`（exit 0）。

設計 commit：`663162d`（本報告另成一 commit）。

## 對照表

### 高（1）

| 審查項 | 改了什麼（檔案／位置） | 沒改的原因 |
|---|---|---|
| 高-1 `provider=anthropic` 只剩 priority，指定 `claude-personal`／`claude-company` 的能力消失 | `design.md` 決定 1（resolve 加選填 `subscription`，與 provider 不一致 400、未知 404、釘住冷卻 409 不代換）、決定 3（釘住只驗該把、不代換）、決定 6.1（`POST /agents` 新形狀也可釘住）；`spec.md` ADDED resolve 的「釘住公司那一把」「釘住的那把冷卻中」「釘住與 provider 不一致」Scenario；`tasks.md` 3.4（釘住演練）；`design-33-overview.md` 實體／時序／介面圖與未決 1、3 | — |

### 中（10）＋B-5 排序建議

| 審查項 | 改了什麼（檔案／位置） | 沒改的原因 |
|---|---|---|
| 中-1 env 值與稽核 MUST 矛盾（model 同時是 env 值與稽核欄位） | `spec.md` resolve：改為「**憑證值** MUST NOT 出現在 list／稽核／log／`GET /agents` status；model／provider／endpoint 是中介資料，稽核可記」；稽核 Requirement 同步；`design.md` 決定 2「值的紀律」明列憑證鍵（含過渡期 get 的 `OPENCODE_CONFIG_CONTENT` apiKey）；`tasks.md` 1.5 canary 只掃憑證值；overview 的 canary 說法同步 | — |
| 中-2 稽核表沒有 provider／model，tasks 沒有 DB 遷移 | `tasks.md` 1.4：`SCHEMA` 加 `provider TEXT`／`model TEXT`、啟動時 `PRAGMA table_info(audit)` 檢查並 `ALTER TABLE audit ADD COLUMN`（additive）、`audit_add`／`audit_all` 加欄位；1.5 加「從 v5 schema DB 啟動寫入」「舊 DB 沒有新欄位」「舊 lgwd 讀新 DB」測試；`spec.md` 稽核 Requirement 加 provider／model 與舊 DB Scenario；`design.md` Risks 補 additive 遷移與退版 | — |
| 中-3 `REMOVED get` 與「並存期 get 照常」矛盾；歸檔時序沒有護欄 | `spec.md` 刪掉整個 `REMOVED Requirements`（本 change 不再 REMOVED get）；Purpose／稽核／共通 schema 的 get 改成「過渡期」條款；`proposal.md` 開頭明寫本 change 不 REMOVED、get 由 d 單移除；`design.md` 決定 5 加「本 change 不移除 get」與歸檔 gate；`tasks.md` 4.1（d 單開單條件）與 5.4（`gateway-v0` 先歸檔＋1／2／3 完成才 archive）；proposal／design／tasks 5.4 註明 `gateway-v0` 歸檔前主規格 `agent-lease` 仍是平均分攤舊語意 | — |
| 中-4 sidecar 的「完整子行程環境」可能漏 PATH／HOME／allowlist | `design.md` 決定 2 明寫 sidecar 子行程＝完整 base env（PATH／HOME／Locale…）＋維運變數＋resolve env 的**不篩選 overlay**；sidecar 自己行程繼承來的 `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 不得原樣漏進（維運變數另外疊）；`spec.md` env 的組成同步；`tasks.md` 2.3、2.6 加測試（sidecar 自己有 `ANTHROPIC_API_KEY=canary`，子行程只看到 resolve 的值或沒有）；overview 實體圖同步 | — |
| 中-5 實機驗收 3b／3c 缺新 sidecar 映像、Zen 接續限制、假額度訊號做法 | `tasks.md` 加 3.0（建／部署含新 sidecar 的 worker 映像、`sidecar --version` 對上 c 單 commit）；3.2 明寫**新開 session**、若 upstream 以 Zen 接續限制拒絕不是 resolve 失敗；3.3 改**手動 `POST /v1/subscriptions/{id}/cooldown`** 模擬額度訊號，並加一次 cooldown 登錄失敗驗 `exclude`；3.4 釘住演練；`design.md` Migration Plan 同步 | — |
| 中-6 policy loader 沒擋 `openrouter.agents` | `tasks.md` 1.0 加 `openrouter.agents == ("opencode",)` 的 loader 檢查與負向測試；`spec.md` 共通 JSON schema 加「政策載入 MUST 拒絕」與 Scenario；`design.md` Context／決定 3 補缺口 | — |
| 中-7 冷卻以 subscription 為單位，會擋掉同一把的另一個免費模型；未決沒問 | `design.md` Risks 明示限制；Open Questions 2 新增「冷卻粒度」並建議本次維持 subscription、稽核已記 model 留資料、下一單再決定；`tasks.md` 3.2 明寫只證明初始選擇、額度事件後換模型不在本驗收；overview 未決 2 與「沒查到」同步 | 沒有改 `cooldown` 形狀加 model：本次維持「必要且只有這些」，且 Zen 限流單位未證實；稽核的 model 已保留可分析資料，細粒度另開單（理由寫在未決 2 的替代） |
| 中-8 resolve 沒有排除剛用完那把的參數；cooldown 失敗會原地打轉 | `design.md` 決定 1／3 加選填 `exclude=<id>[,<id>]`（query 參數、非新端點）、排除後無候選回 409 `no_candidate`、決定 6.6 換把流程「cooldown 失敗也帶 exclude」；`spec.md` ADDED resolve 與選把規則的 Scenario；`tasks.md` 2.4；overview 時序圖 | — |
| 中-9 sidecar `POST /agents` 遷移不足：`subscription` 硬切、`model` 命名未定 | `design.md` 決定 6.1 給完整組合矩陣（只給 `subscription`＝舊形狀走 get；三者都省略＝list 挑預設；`provider`＝新形狀，`subscription` 是釘住；殘缺／矛盾 400）；明寫 `subscription` 沿用 `{"id": …}`；`design.md` 決定 4 明寫 model 是供應商 id、sidecar 正規化 `<provider>/<model>` 或 400；`tasks.md` 2.2 加呼叫端清冊與過渡期、2.6 加 sidecar 測試；`spec.md` models.default 加前綴 Scenario；proposal Impact 同步 | — |
| 中-10 `schema` 只版本化回覆形狀，沒有版本化 env 組成 | `design.md` 決定 1 加 `env_schema`（鍵集／語意版本、bump 規則、sidecar 拒收高版本），並定義 `schema` 是「該端點回覆形狀」的版本；`spec.md` resolve 同步並加 Scenario；`tasks.md` 1.3／1.5／2.6；overview 介面圖同步 | — |
| B-5 sidecar 實作應排在 #216 階段 2.5a merge 之後（排序建議） | `tasks.md` 開頭「排序與依存」、2.0 排序 gate、5.1 交付 PM；`proposal.md` Impact 的「排序」；`design.md` 決定 6.8；三處都標「排序以 PMO 為準」 | — |

### 低（12）

| 審查項 | 改了什麼（檔案／位置） | 沒改的原因 |
|---|---|---|
| 低-1 `GET /agents` status 的 `subscription` 不是新欄位 | `design.md` 決定 6.4 改成「**保留** `subscription`，加 `provider`／`model`」；`tasks.md` 2.5 同步；overview status 行同步 | — |
| 低-2 overview 的 `POST /agents` 示意缺必填 `id`／`workdir` | `design-33-overview.md` 實體圖補 `id`／`workdir`，時序圖補 `id`、加「workdir／session／input 省略」 | — |
| 低-3 `no_model` 在選中後才判斷，可能錯過另一個候選 | `spec.md` 選把規則第 3 步把「能推導模型」加進候選條件；`design.md` 決定 3／4 同步；加「最高 priority 推不出、下一個可以」Scenario | — |
| 低-4 未知 `agent` 回 403 而不是 400 | `design.md` 決定 1／3 錯誤表：未知 agent → 400 `bad_request`；`spec.md` resolve 與錯誤回覆加 Scenario；`tasks.md` 1.1 | — |
| 低-5 `schema: 1` 同時代表 list row 與 resolve 回覆 | `design.md` 決定 1 定義 `schema` 是「該端點回覆的版本」、resolve 與 list 各自獨立；`spec.md` 同步；另加 `env_schema` 區分 env 組成 | — |
| 低-6 resolve 200 含 key 但沒有 `Cache-Control: no-store` | `design.md` 決定 1／Risks；`spec.md` resolve 加標頭要求；`tasks.md` 1.3／1.5 加 get／resolve 標頭與斷言；overview 介面圖 | — |
| 低-7 opencode 的 `provider`／`small_model` 在 v2 schema 已 deprecated | 選了「Gateway 只回片段、sidecar 組 config」：`design.md` 決定 2 與 Risks、Context；`spec.md` env 的組成；overview「沒查到」；不讓 Gateway 碰 legacy config 形狀 | 沒有現在改 `providers`／`settings`：worker 釘的 opencode 版本是否接受 legacy 鍵沒有實機證據，片段方案讓 sidecar 依版本決定；列「沒查到」 |
| 低-8 query 只講 `/`，`+`／`&` 編碼沒講 | `design.md` 決定 1 明寫 client percent-encode（`safe=""`）、`+`→`%2B`、server `parse_qs` form 解碼；`tasks.md` 1.3／1.6；`spec.md` resolve；加 `model=a+b` 測試 | — |
| 低-9 overview 時序圖「(provider, model)〔或 subscription〕」與 resolve 不一致 | overview 時序改成「resolve 的 `meta.subscription` 為準；與 list 的選擇不同→記 log／回報」；`design.md` 決定 6.2 同步 | — |
| 低-10 overview 說 canary「守著」sidecar，但本 repo canary 掃不到 | `design.md` 決定 2 明寫 Gateway canary 掃 Gateway、sidecar log／status 由 MyLinuxPool 的 c 單；`tasks.md` 2.6／5.5；overview 介面圖下方與「沒查到」 | — |
| 低-11 tasks 的實機驗收沒有固定層級／擁有者 | `tasks.md` 開頭固定：3.0／3.2／3.3／3.4 是 MyLinuxPool c 單驗收節、3.1 是本 repo c 單部署；若 PMO 另掛 d 單以 PMO 為準 | — |
| 低-12 `meta.cooldown`／`meta.available` 是恆定值 | `design.md` 決定 1 移除 resolve meta 的 `cooldown`／`available`；`spec.md` resolve／Scenario 同步；overview 介面圖同步；list 仍保留（菜單要顯示） | — |

### 資訊（6；看過，有需要就補）

| 審查項 | 改了什麼（檔案／位置） | 沒改的原因 |
|---|---|---|
| 資訊-1 `openspec validate --strict` 存在且通過 | 本輪重跑，通過（見上）；報告記錄 | — |
| 資訊-2 設計對現行 v5 的事實大致一致 | 沒有改事實；`design.md` Context 保留並補稽核欄位、loader 缺口、兩把相同 | 審查已確認可對照範圍沒有事實錯誤 |
| 資訊-3 `LGW-V5-INTERFACE.md` 兩邊都找不到 | overview「沒查到」保留並改寫成修訂輪版本 | 找不到，不能假裝有 |
| 資訊-4 opencode v2 schema 的合法鍵與 deprecated | 用於低-7：`design.md` Context／Risks、`spec.md` env 片段；overview「沒查到」記 pinned 版本未驗 | — |
| 資訊-5 `agent-lease` 主規格仍是平均分攤舊語意 | `proposal.md` 開頭、`design.md` Context／決定 5、`tasks.md` 5.4 都加「`gateway-v0` 歸檔前主規格仍是舊語意」 | — |
| 資訊-6 sidecar 的巢狀 error 形狀與 Gateway 平面 error 不同 | `design.md` 決定 6.2 加「sidecar 對控制者仍用巢狀 `{"error":{"code","message"}}`；Gateway 的碼是內部映射」 | — |

## 未決清單（重新整理；design 與 overview 都是 6 題）

1. **釘住的那把額度用完時，失敗還是換另一把？** 建議失敗回報、不自動換（釘住是明確帳號選擇；自動換會違反隱私／計費／額度保留意圖）。替代＝自動換，但要通知控制者並定換把規則。
2. **冷卻粒度要不要細到 `(subscription, model)`？** 建議先維持 subscription 粒度（保守；Zen 限流單位查無）；稽核已記 model，下一單再決定。限制：同一把的另一個免費模型也會被冷卻；3.2 只證明初始選擇。
3. **未釘住時 `anthropic` 兩把的預設選法**：建議維持 priority（personal 先、company 後），因為釘住已提供明確入口；平均分攤要用量資料（v5 已移除），角色規則要 resolve 有身分（會擴介面）。
4. **get 的退場時點**：建議 sidecar 遷移＋3 驗收後開 d 單；本 change 不 REMOVED get，d 單的 delta 以那時的主規格為底。
5. **meta「到期」的語意**：本設計解讀為冷卻 `until`；若指 key 到期，Gateway 沒有那資料，要另做。
6. **resolve 是否也收「不指定 provider」**：建議不要（issue 的輸入就是 provider＋model；不指定時由 sidecar 從 list 挑好再 resolve）。

原「opencode permission 歸屬」未決已由 B-2 關閉：sidecar 擁有 `permission`／`instructions` 與 config 形狀，Gateway 只回片段。

## 沒查到的地方

- **opencode pinned 版本（2.0.26／2.0.32）對 legacy `provider`／`small_model` 的接受度**：只以官網 config schema（2026-10-20 抓）確認 deprecated；沒有實機 binary 可驗。這是選「片段」而非「整包 JSON」的原因之一。
- **sidecar `build_env` 的內部細節**：`ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL` 是否由現行 driver 設定、`models.default` 為 null 時「`allow` 第一個不含萬用字元的項」的實際順序、`OPENCODE_CONFIG_CONTENT` 除 apiKey／model／permission 外還帶什麼——sidecar.md 只寫介面層級，沒有實作細節。
- **MyLinuxPool#216 階段 2.5a 的 `_swap` 實作、2a／2.6／2.8／階段 3 與 #229 的細節**：只讀到 issue 內文與 master 現況；B-5 的排序是建議，實際依存以 PMO 為準。
- **OpenSpec 的 `archive`／`apply` 實際行為**：只跑 `validate --strict`，沒有真的 archive／apply；歸檔 gate 是依 delta／REMOVED 語意推理（中-3）。
- **`#33` 的 issue 全文與「meta 到期」的原意**：只有工單標題與 prompt 摘要，沒有讀 issue 內文；未決 5 無法核對。
- **服務那台與 worker 的現況**：沒有連線、沒有讀 `keys.env`、沒有跑 `install.sh --check`；3.0 的 worker 映像重建、3.2／3.3／3.4 都尚未執行。
- **Zen 免費層的限流單位**（per 帳號還是 per 模型）查無公開資料；影響冷卻粒度（未決 2）。
- **`LGW-V5-INTERFACE.md`**：本 repo 與 MyLinuxPool master 都找不到；v5 事實以本 repo 的 spec／程式＋sidecar 文件交叉確認。

## 驗證紀錄

```
$ openspec validate --strict agent-provider-model-interface
Change 'agent-provider-model-interface' is valid   # exit 0
```

## Commit

- `663162d` docs(#33): 修訂設計：釘住 subscription、換把 exclude、opencode env 片段、稽核遷移與未決重整（proposal／design／tasks／spec／overview）
- 本報告另成一 commit（見 branch 最新）。
