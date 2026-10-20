GLM-5.3

# #33 設計修訂報告（fix2，第 2 輪審查）

修訂對象：branch `design/issue33-iface`（基準 `aa67051`）的 `openspec/changes/agent-provider-model-interface/`（proposal／design／tasks／specs/subscription-registry/spec.md）與 `docs/research/design-33-overview.md`。輸入：第 2 輪審查報告 `review-33-r2.md`（新-中-1～2、新-低-1～6；另加 overview 的 public repo 收斂 1 項）。全部照審查建議修，無不同意項；只改文件，未改程式；未 push、未對 main commit、未讀憑證檔、未連遠端機器。

驗證：`openspec validate --strict agent-provider-model-interface` → `Change 'agent-provider-model-interface' is valid`（exit 0）。

設計 commit：`9b383f7`（本報告另成一 commit）。

## 對照表

| 審查項 | 改了什麼（檔案／位置） | 沒改的原因 |
|---|---|---|
| 新-中-1 `subscription`＋`exclude` 同時給：一處 400、一處「跳過 `exclude`」 | 採報告建議的前者（400 寫死）：`design.md` 決定 3 選把步驟 1（釘住限縮）改寫「此步不套 `exclude`——同時給已在參數驗證回 400 `bad_request`」；`spec.md` 選把規則步驟 1 同文；spec Scenario「釘住與排除同時給」原已預期 400（不動）；`tasks.md` 1.1 補「同時給在參數驗證回 400（選把步驟不套 `exclude`）」；`proposal.md` What Changes 補「與 `subscription` 互斥，同時給 400」；overview 介面圖原已列（不動） | — |
| 新-中-2 3.0／3.1 部署順序自相矛盾 | 採「改編號順序為 lgwd 部署→worker 映像部署」：`tasks.md` 3.0＝部署新版 lgwd（寫明「部署順序：lgwd 先、worker 映像後；映像可先建好」）、3.1＝建含新 sidecar 的 worker 映像並部署（`sidecar --version` 對上 c 單 commit；部署在 3.0 之後）、3.2 開頭加通過條件「lgwd 版本（`install.sh --check`）與 worker 映像的 sidecar 版本（`sidecar --version`）都對上」；第 3 節標題與低-11 註記同步（3.0 是本 repo；3.1～3.4 是 MyLinuxPool 的 c 單節）；`design.md` Migration 3.0／3a 同步互換（3.0 部署 lgwd 寫明部署順序與順序反了的後果；3a 建映像可先建、部署在 3.0 之後、通過條件兩版本都對上）；overview「沒查到」段的 worker 映像引用 3.0→3.1。design 的 3b／3c／3d 標籤不動（未重編驗收編號，兩處引用不受影響） | — |
| 新-低-1 釘住＋該把推不出模型、同 provider 另一把可推導：交集為空、回碼未定 | 採「釘住限縮提到最前」：`spec.md` 選把規則把釘住限縮改為步驟 1（先限縮候選，再過濾 provider／agent／model／可推導模型／部署／冷卻），步驟 4 補「無一能推導 → 400 `no_model`（釘住時就是該把——同 provider 另一把可推導也不代換）」；新增 Scenario「釘住那把推不出模型，同 provider 另一把可推導」→ 400 `no_model` 不代換；`design.md` 決定 3 同步重排（步驟 1 寫明釘住先限縮與不套 `exclude` 的理由）＋決定 4 的步驟引用改「第 4 步」並補釘住路徑（先限縮、推不出直接 400）；spec「model 省略時的語意」同步；`tasks.md` 1.1 候選篩選順序同步；overview 時序圖選把段同步（「釘住先限縮＝只看該把」＋400 `no_model`） | — |
| 新-低-2 400 與 404 同時成立時回哪個未定 | 照報告建議寫死 precedence：**輸入形狀（400 `bad_request`）先於查無（404 `not_found`）先於政策拒絕（403 `agent_not_allowed`／`model_not_allowed`，與 400 `no_model`）先於候選狀態（409 `no_candidate`／`not_deployed`／`cooling`）**。`design.md` 錯誤表後加「錯誤 precedence」段（`provider=NoSuchProvider&subscription=claude-personal` → 400、`subscription=<未知 id>&exclude=x` → 400 兩例；與選把步驟同一軸）；`spec.md` 選把規則末段＋「錯誤回覆」Requirement 各加一句；新增 Scenario「輸入形狀錯誤先於查無」（`provider=NoSuchProvider&subscription=claude-personal` → 400）；`tasks.md` 1.1 寫明同一順序；overview 介面圖錯誤清單後加一行 | — |
| 新-低-3 proposal 的 meta 欄位清單漏 `agent` | `proposal.md` What Changes 的 meta 清單補 `agent`（與 design 決定 1／spec／overview 對齊） | — |
| 新-低-4 `schema`／`env_schema` 兩個版本欄位可再議 | 採報告 (a) 維持兩欄：`design.md` 決定 1 補「為什麼兩個版本欄」——meta 形狀與 env 鍵集獨立演進的場景（加 meta 欄位只 bump `schema`、env 處理不需重驗；改 env 鍵集只 bump `env_schema`、回覆形狀版本不抬；保留未來放寬 `schema` 一側而不動 `env_schema` 嚴格護欄的空間）；design 未決加第 7 題（建議維持兩欄；合併的優缺點；合併屬客戶端相容政策的重定，請本人裁定）；overview 未決同序加第 7 題（兩邊自本輪起 7 題同序）；spec 的兩欄條文不動（維持現狀即採 (a)） | — |
| 新-低-5 endpoint 若內嵌憑證會隨「中介資料」進稽核／meta、canary 掃不到 | `design.md` 決定 2 值的紀律補「`endpoint` MUST NOT 內嵌憑證」（query `key=`／`token=`、userinfo `@`；政策 loader 載入檢查拒絕＋canary 對 endpoint 值做同樣式的掃描＝第二道）；`spec.md` resolve 的值紀律段補「前提是 endpoint MUST NOT 內嵌憑證（政策載入時拒絕）」、「共通 JSON schema」補政策載入 MUST 拒絕條文＋新 Scenario「政策檔的 endpoint 內嵌憑證」（`PolicyError`、lgwd 不啟動）；`tasks.md` 1.0 加 loader 的 endpoint 樣式檢查與負向測試（`https://host/?key=…`、`https://user:pass@host`）、1.5 加 canary 的 endpoint 樣式掃描 | — |
| 新-低-6 組合矩陣是對 PM B-1 的重新解讀，宜列 2.2 開工條件 | `tasks.md` 2.2 開頭加「**開工條件**——PM 對 B-1／B-3 的合併解讀（design 決定 6.1 的組合矩陣）確認後才開工（PM 堅持字面的話，釘住入口在 sidecar 層要另定，先回報再動）」；`design.md` 決定 6.1 的 B-1 收斂段補與 tasks 2.2 的互引（確認前不開工），「若 PM 要 B-1 字面的全面 400，過渡期就沒有釘住入口，需另定」的註記保留。PM 之間的確認由下單者轉達，本輪只改文件 | — |
| 另外：overview L128「來源是 private repo 的輪換表筆記」 | overview「沒查到」段收斂成「出自本人觀察（2026-10-17；MyAiStorage#85）」——private repo 的來源描述移除、工單代號保留 | — |

## 新增的未決

- **未決 7（新-低-4）**：`schema` 與 `env_schema` 要不要合併成一個版本號？建議維持兩欄（meta 形狀與 env 鍵集獨立演進；分開的版本讓 sidecar 知道哪一側變了、env 鍵集變才重驗 env；合併會讓 meta 的小變更造成不必要的整體回覆拒收）。合併屬客戶端相容政策的重定，請本人裁定。design 與 overview 同序第 7 題（fix1 時兩邊 6 題，本輪起 7 題）。

## 沒查到的地方

- 同第 2 輪審查的「沒查到」：sidecar `build_env`／`_swap` 的實作細節、MyLinuxPool PM 對 B-1／B-3 的真意（tasks 2.2 的開工條件就是為此；PM 之間的確認由下單者轉達）、opencode pinned 版本對 legacy `provider`／`small_model` 的接受度、OpenSpec `archive`／`apply` 實際行為、`#33` issue 全文與「meta 到期」原意、服務那台與 worker 的現況——本輪只改文件，未新增查證。
- **`install.sh --check` 的「版本對上」輸出形狀**：`deploy/install.sh` 有 `--check`（僅檢查現狀、不修改系統狀態；usage L31、解析 L43），「lgwd 版本對上」的具體輸出（tag 字串對比或其他信號）未細讀驗證——3.2 的通過條件以它為準，屬實作單 1.7／3.0 的驗收。

結論：第 2 輪審查 8 項（中 2、低 6）＋收斂 1 項全數照建議修畢，無不同意項；新增未決 1 題（未決 7）。

## 第 3 輪（小修；2026-10-20，輸入：MyLinuxPool PM 第二輪對齊）

1. **env 片段鍵名／型別寫進 spec＋未知鍵規則**：`specs/subscription-registry/spec.md`「env 的組成」改寫成鍵集清單（每鍵：型別／空值／出現條件；claude 系 `CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`ANTHROPIC_BASE_URL`／`ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`、opencode `OPENCODE_API_KEY`／`OPENCODE_PROVIDER_ID`／`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`；`ANTHROPIC_API_KEY=""` 標為刻意佔位）＋「未知鍵」段（sidecar 忽略＋WARN：記鍵名不記值、不塞進 config 或子行程環境；`permission`／`instructions` sidecar 擁有、同名 sidecar 贏）＋新 Scenario「env 出現未知鍵」；`design.md` 決定 2（鍵集契約 bullet、overlay 第 3 層改寫：已知鍵不篩選、未知鍵丟棄）、6.3；`tasks.md` 2.3／2.6；`design-33-overview.md` 介面圖（未知鍵註記）與修訂說明。
2. **釘住且冷卻中 sidecar＝waiting-quota**：`design.md` 決定 3（安全預設句）、6.2（409 對照加「釘住時＝額度用完處理，見 6.6」）、6.6（waiting-quota：記 cooldown、保留 agent、到期由重試 job 再 resolve 同一把；「釘住」記在 AgentRecord）、Risks、未決 1（題目改「換另一把還是等這一把」、建議不換＝waiting-quota）；`tasks.md` 2.4／3.4；`design-33-overview.md` 時序圖（末兩行）＋未決 1。Gateway 層（409 `cooling`＋`until` 不代換）不動。
3. **排序與 B-1 確認**：`tasks.md` 開頭排序段（#33 sidecar 實作排在 #216 階段 2.5a（MyLinuxPool#237）與階段 3 merge 之後）、2.0、2.2（新-低-6 開工條件→已確認，含 PM 確認的四種組合）、5.1；`design.md` 決定 6 標題（兩輪已對齊）、6.1（B-1 收斂段改「已確認」；不一致補「agent 不支援」）、6.8；`design-33-overview.md`「沒查到」段的排序行。均仍標「排序以 PMO 為準」。

## 第 4 輪（小修；2026-10-20，輸入：第 3 輪審查報告）

1. **新-中-1（r3）「釘住＋推不出模型」例挑錯 kind**：`specs/subscription-registry/spec.md` 的「釘住那把推不出模型」Scenario 改成非 `claude-subscription` 的 api-key 假政策形狀（標明現行政策不可達）、「推不出模型」Scenario 標 api-key；spec 選把規則第 4 步、`design.md` 決定 3 第 4 步與決定 4、`tasks.md` 1.1、`design-33-overview.md` 時序圖補「`claude-subscription`＋default null 是合法 200（用 agent 預設）；`no_model` 只適用非 claude-subscription 且 default null、allow 全萬用字元」。
2. **新-中-2（r3）endpoint 檢查位置**：`specs/subscription-registry/spec.md` 的值紀律與「共通 JSON schema」（Scenario 改「provider 表的 endpoint 內嵌憑證」）、`design.md` 決定 2 值的紀律與決定 3 新 bullet（`providers.validate_endpoints()` 逐筆驗、`load_policy` 轉 `PolicyError`；providers.py 不 import policy.py，避免循環 import）、`tasks.md` 1.0（改 providers 載入、測試 monkeypatch `PROVIDERS`）與 1.5（canary 樣式加寬）；樣式加寬為 `key=`／`token=`／`api_key=`／`access_token=`／`@`。
3. **新-低-1（r3）3d 仍寫「回報」**：`design.md` Migration 3d 改成「預設不換＝waiting-quota（保留 agent、到期再 resolve 同一把）」，與 `tasks.md` 3.4 同文。
4. **新-低-2（r3）三類變數與 `OPENCODE_PROVIDER_ID`**：`specs/subscription-registry/spec.md`「env 的組成」改成「只含憑證、端點、模型命名（含 opencode 的 provider id）三類變數」。
5. **新-低-3（r3）釘住時不帶 `exclude`**：`design.md` 6.6、`tasks.md` 2.4、`design-33-overview.md` 時序圖各加「釘住時 resolve 只帶 `subscription`（釘住），不帶 `exclude`（同時給回 400 `bad_request`）」。
6. **新-低-4（r3）overview 作者標註**：`design-33-overview.md` 第 1 行改成「原設計 GLM-5.3；修訂 1 DeepSeek V4.1 Flash；修訂 2 GLM-5.3；修訂 3、4 DeepSeek V4.1 Flash」；同頁修訂說明同步為修訂 4。

驗證：`openspec validate --strict agent-provider-model-interface` 通過（exit 0）；grep 確認設計文件（openspec 與 overview）無三類舊措辭殘留（政策檔層級的 endpoint 檢查、3d 的失敗回報語意、釘住例用 `claude-personal`）。六點全照報告建議修，無不同意項；只改文件，未改程式。
