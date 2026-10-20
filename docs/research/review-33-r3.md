DeepSeek V4.1 Flash（同模型、不同 session 審查）

# #33 設計修訂審查（第 3 輪，收尾 spot review）：provider＋model 介面

審查對象：worktree `review-33`（branch `review/design-33`）的
`openspec/changes/agent-provider-model-interface/`（proposal／design／tasks／specs/subscription-registry/spec.md）
與 `docs/research/design-33-overview.md`，即 `aa67051 → 78c08c6` 的修訂（含第 2、3 輪）。
只讀文件與本 repo 程式；未改被審對象、未 push、未讀憑證檔、未連遠端機器。

驗證方式：`openspec validate --strict agent-provider-model-interface`（通過，exit 0）；
`git -C <wt> diff aa67051 78c08c6 -- openspec docs/research/design-33-overview.md` 逐條對照；
對照 `.work/notes-33-align.md` 第二輪；交叉核對 `lgwd/policy.py`、`lgwd/providers.py`、
`policy/default.json`、`lgwd/store.py`、`lgwd/errors.py`、`openspec/changes/gateway-v0`。
不以 `design-33-fix2.md` 的自述為準。

結論先講：**需修**；新問題 高 0、中 2、低 4（第 2 輪的新-中-1／新-中-2 全修，新-低-1／新-低-5 只算部分）。

## 第 2 輪項目的驗證

| 項目 | 結果 | 依據 |
|---|---|---|
| 新-中-1 `subscription`＋`exclude` 同時給：全部文件一致 400 | 已修 | spec L11（MUST 400）、L35–37（Scenario 400）、L71（選把步驟 1「此步不套 `exclude`，同時給已在參數驗證回 400」）；design L51、L107；tasks L10；proposal L21；overview L100–101。全庫已無「跳過 `exclude`」的殘留 |
| 新-中-2 部署順序：先 lgwd、後 worker 映像；通過條件寫了版本對上 | 已修 | tasks L31（3.0 部署 lgwd，順序 lgwd 先、映像後）、L32（3.1 建／部署映像在 3.0 之後）、L33（3.2 通過條件：`install.sh --check` 與 `sidecar --version` 都對上）；design L207–208（3.0／3a 同序，3a 寫明映像可先建、部署在 3.0 之後、兩版本都對上）；overview L140 的 3.1 引用同步 |
| 新-低-1 釘住＋該把推不出模型、同 provider 另一把可推導：交集為空、回碼未定 | **部分** | 規則已修：spec L71（釘住先限縮）、L74（釘住時就是該把）；design L107、L110、L142；tasks L10；overview L53–55。**但新增的 Scenario L106–108 例子寫錯**：`claude-personal` 是 `claude-subscription`，本來就能用 agent 預設（見新-中-1） |
| 新-低-2 400／404 precedence 未定 | 已修 | spec L80、L189（precedence）、L207–209（新 Scenario）；design L134（同序＋兩例）；tasks L10；overview L105–106 |
| 新-低-3 proposal 的 meta 清單漏 `agent` | 已修 | proposal L22 已含 `agent`，與 design L52／spec L13／overview L91–92 一致 |
| 新-低-4 `schema`／`env_schema` 兩個版本欄位 | 已修 | design L56（兩欄的理由）、L222（未決 7）；spec L17；overview L128（第 7 題）；未決 7 題兩邊同序 |
| 新-低-5 endpoint 內嵌憑證會進稽核／meta、canary 掃不到 | **部分** | 條文與測試已加：spec L15、L213、L223–225；design L92；tasks L9（loader 檢查＋負向測試）、L14（canary 樣式掃描）。**但檢查位置指到 `lgwd/policy.py`，而 endpoint 不在政策檔**（見新-中-2） |
| 新-低-6 組合矩陣是對 PM B-1 的重新解讀，宜列 2.2 開工條件 | 已修 | tasks L22 已改成「B-1／B-3 的合併解讀已與 MyLinuxPool PM 確認（2026-10-20；新-低-6 的開工前確認已完成）」；design L168 同步；notes-33-align 第二輪第 1 點與此一致 |
| overview 的 private repo 措辭 | 已修 | overview L133 已收斂為「出自本人觀察（2026-10-17；MyAiStorage#85）」，private repo 的來源描述已移除；工單代號可留 |

## 第 3 輪三點的驗證

| 點 | 結果 | 依據 |
|---|---|---|
| (a) spec 寫出 env 片段鍵名／型別、未知鍵忽略＋WARN（只記鍵名）、permission／instructions 由 sidecar 擁有且 sidecar 贏 | 已修 | spec L112–127：10 個鍵名（claude 6 鍵、opencode 4 鍵）分 8 列寫型別／空值／出現條件、L125 未知鍵規則＋permission／instructions、L146–147 新 Scenario；design L77（鍵集契約）、L88（overlay 第 3 層）、L79（permission／instructions）；tasks L23、L26；overview L98。鍵名在 spec／design／tasks／overview／proposal 沒有別名（overview 的 `ANTHROPIC_MODEL…` 是省略號，不是另一個鍵名） |
| (b) 釘住且冷卻中：Gateway 409 `cooling`＋`until`、sidecar 當 waiting-quota、釘住記在 AgentRecord、未決 1 仍在 | 已修 | Gateway 層：spec L80、L186；design L116、L117。sidecar 層：design L175（6.6 waiting-quota：記 cooldown、保留 agent、到期再 resolve 同一把、不換把；「釘住」記在 AgentRecord）、L198（Risks）、L216（未決 1）；tasks L24、L35；overview L72–73、L122。**殘留：design 3d L211 仍寫「回報」**（低-1） |
| (c) 排序寫了 #216 階段 2.5a（#237）與階段 3 之後、標「排序以 PMO 為準」，6.1 與 tasks 2.2 已改「已確認」 | 已修 | 排序：tasks L3、L20、L43；design L177（6.8）、L205（Migration 2）；proposal L44；overview L137——都寫「階段 2.5a（MyLinuxPool#237）與階段 3 merge 之後」＋「排序以 PMO 為準」。B-1：tasks L22、design L168 都改「已與 MyLinuxPool PM 確認（2026-10-20）」；「新形狀＋`subscription`＝釘住」與 notes-33-align 第二輪第 1 點一致 |

## 新問題

### 高

（無）

### 中

#### 新-中-1（r3）　「釘住＋推不出模型」的新 Scenario 例與既有 Scenario／規則相反

- **位置**：spec L106–108（新 Scenario：`provider=anthropic&subscription=claude-personal`、model 省略、default null、allow 全萬用字元 → 400 `no_model`）vs spec L153–155（既有 Scenario：同一個 `claude-personal`、model 省略、default null → 200、`meta.model` null、env 不帶模型變數）；規則本身在 spec L74（「kind 為 `claude-subscription`，或 `models.default` 非 null，或 `allow` 至少一個不含萬用字元」＝能推導）與 L151（`claude-subscription` default null → 用 agent 預設，不是 `no_model`）。design L107／L110／L142、tasks L10、overview L55 也重述「釘住那把推不出 → 400」但沒有區分 kind。
- **事實**：`claude-personal` 的 kind 是 `claude-subscription`（`lgwd/providers.py` L25–28）；`policy/default.json` L4–8 的 `claude-personal` 是 `allow: ["*"]`、`default: null`。因此新 Scenario 的「allow 全是萬用字元樣式（推不出模型）」正是目前政策；但「kind 為 `claude-subscription`」已使它成為能推導（用 agent 預設），所以同一個請求不可能同時是 200（L153–155）與 400（L106–108）。
- **為什麼是問題**：spec 的 Scenario 是驗收依據；兩個 Scenario 對同一個請求給相反的結果，照哪一個寫測試都會讓另一個失敗。這也是第 2 輪新-低-1 的驗收例，例錯等於那個修正沒有被正確釘住。規則本身（釘住先限縮、不代換）沒有問題，問題是例子挑錯 kind。
- **建議**：把新 Scenario 改成「假設政策有一個非 `claude-subscription` 的 provider 有兩把 api-key subscription」（或改成單元測試的假政策），不要用 `claude-personal`；同時在 design L107／tasks L10／overview L55 補一句「`claude-subscription`＋default null 是合法的 200（agent 預設），`no_model` 只適用非 `claude-subscription` 且 default null、allow 全萬用字元」。spec L161–163 的「推不出模型」Scenario 也應同樣標明是 api-key 的形狀，否則它在現行政策下同樣不可達。

#### 新-中-2（r3）　endpoint 憑證檢查指到 `lgwd/policy.py`，但 endpoint 不在政策檔

- **位置**：spec L15、L213、L223–225（都寫「政策載入 MUST 拒絕 endpoint 值…的政策檔」）；design L92（「政策檔的 endpoint 值…政策 loader 載入時檢查並拒絕」）；tasks L9（「`lgwd/policy.py` 加…endpoint 值的憑證樣式檢查」＋負向測試）。
- **事實**：`lgwd/policy.py` L4 自己寫「政策檔只管 priority、agents、models（與 provider 有關的欄位在 `lgwd/providers.py`）」；`policy/default.json` 五筆都沒有 `endpoint`；endpoint 是 `lgwd/providers.py` L13–46 的 `CredentialProvider.endpoint` 常數（openrouter／ollama-cloud／opencode-zen；anthropic 為 null）。`load_policy`（L102–137）只讀 priority／agents／models，`_parse_subscription` 也不讀 endpoint。
- **為什麼是問題**：照 tasks L9 在 `policy.py` 加檢查，政策檔裡沒有 endpoint 可檢查，檢查會是 no-op；若改成在政策檔新增 endpoint 欄位，則等於改政策 schema（設計沒有要這樣做），且 `providers.py` 的實際 endpoint 仍不受檢。spec 的 MUST 與 Scenario「政策檔某把的 endpoint 值含…」因此在現行資料模型下無法成立。這是第 2 輪新-低-5 只修一半的地方（條文有、位置錯）。
- **建議**：把檢查放到 endpoint 的真正來源——在 `policy.py` 載入時 import `PROVIDERS` 逐筆驗 `CredentialProvider.endpoint`，或在 `providers.py` 加啟動時驗證（固定字串錯誤，必要時把 `PolicyError` 的定義移到共用處避免循環 import）；spec／design／tasks 的措辭改成「providers 載入／啟動時」而不是「政策檔的 endpoint」；測試用假 provider 表（或 monkeypatch `PROVIDERS`）而不是假政策檔。canary 仍是第二道，維持。另可順便把樣式從 `key=`／`token=`／`@` 加寬到 `api_key=`／`access_token=` 等（可選）。

### 低

#### 新-低-1（r3）　design Migration 3d 仍寫「回報」，與 waiting-quota 不一致

- **位置**：design L211（3d 釘住演練：「額度用完的行為依未決 1（預設不自動換、回報）」）vs L117／L175／L198／L216、tasks L35、overview L72–73／L122（都是 waiting-quota：記 cooldown、保留 agent、到期再 resolve 同一把）。
- **問題**：3d 是演練步驟，卻還留著第 2 輪的「回報」語意；照 3d 驗收會驗到「回報失敗」，照 6.6／tasks 3.4 則是「保留 agent 等待」。tasks 3.4 已改對，design 3d 漏改。
- **建議**：3d 改成「預設不換＝waiting-quota（保留 agent、到期再 resolve 同一把）」，與 tasks 3.4 同文。

#### 新-低-2（r3）　spec env 組成「只含憑證、端點、模型三類」與鍵集清單的 `OPENCODE_PROVIDER_ID` 分類措辭

- **位置**：spec L112（「只含憑證、端點、模型三類變數」）vs L120（鍵集包含 `OPENCODE_PROVIDER_ID`）。第 3 輪改寫時刪掉了舊版「（opencode 的 `OPENCODE_PROVIDER_ID` 是模型命名的一部分）」的括號。
- **問題**：`OPENCODE_PROVIDER_ID` 是 provider id，不是憑證、端點或模型值；雖然它確實是模型命名的一部分，但新文字沒有再說，嚴格讀者會覺得鍵集清單與「三類」互相矛盾。
- **建議**：把括號加回，或改成「只含憑證、端點、模型命名（含 opencode 的 provider id）三類變數」。

#### 新-低-3（r3）　design 6.6 通用換把句與釘住段沒有明寫「釘住時不帶 `exclude`」

- **位置**：design L175 第一句（「額度訊號 → cooldown → resolve 帶 `exclude=<剛耗盡的 id>`」）與同段釘住子句（waiting-quota、帶同樣的釘住、不換另一把）；spec L11／L71／L80（`subscription`＋`exclude` 同時給 400）；tasks L24；overview L68。
- **問題**：通用句沒有標「未釘住時」；若實作者把它套到釘住路徑，會同時送 `subscription` 與 `exclude`，直接撞 spec 的 400 規則。釘住段的「帶同樣的釘住」已暗示不帶 exclude，但沒有寫死。
- **建議**：在 6.6、tasks 2.4、overview 換把段各加一句「釘住時 resolve 只帶 `subscription`（釘住），不帶 `exclude`（同時給 400）」。

#### 新-低-4（r3）　overview 作者標註把第 3 輪小修記成 GLM-5.3

- **位置**：overview L1（「GLM-5.3（修訂 2／3 輪；修訂 1 DeepSeek V4.1 Flash；原設計 GLM-5.3）」）。
- **問題**：第 3 輪小修的作者是 DeepSeek V4.1 Flash（prompt 明示；本次審查與其同模型、不同 session），不是 GLM-5.3。這一輪的 overview 開頭因此標錯。
- **建議**：改成「GLM-5.3（修訂 2 輪；修訂 1／3 DeepSeek V4.1 Flash；原設計 GLM-5.3）」或等價寫法。

## 其他核對

- **鍵名清單一致性**：spec L114–121、design L72–75、tasks L11、overview L93–98、proposal L26 的鍵名一致（claude 6 鍵：`CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`ANTHROPIC_BASE_URL`／`ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL`；opencode 4 鍵：`OPENCODE_API_KEY`／`OPENCODE_PROVIDER_ID`／`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`）。沒有鍵在某處叫另一個名字。overview 的 claude 清單用 `ANTHROPIC_MODEL…` 省略 `ANTHROPIC_SMALL_FAST_MODEL`，是省略號不是別名；若要逐字一致可補上。
- **鍵值與程式事實**：`OPENCODE_PROVIDER_ID` 的 `opencode-zen`→`opencode`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud` 與 `docs/research/review-24.md` 的 `opencode models` 證據一致；`ANTHROPIC_API_KEY=""`＋`ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_BASE_URL` 的 Ollama Cloud 接法與 `openspec/changes/gateway-v0/design.md` L171、`docs/research/review-notes-v0.md` L12 一致。tasks 1.2 的「claude 系照 v5」因此與新鍵集不衝突（`ANTHROPIC_API_KEY=""` 是 v5 既有）。
- **錯誤碼與 Scenario**：`subscription`＋`exclude` 400、precedence、`no_candidate` 只在未釘住、`cooling` 釘住時只該把，四份文件一致；除新-中-1 的新 Scenario 外沒有新衝突。spec 錯誤回覆清單（L181–187）仍只列 `bad_request`／`no_model`／`model_not_allowed`／`not_found`／`cooling`／`no_candidate`，沒列 `not_deployed`／`agent_not_allowed`；這兩個在 v5 表與 design L127–129 已有，屬既有寫法（第 2 輪已接受），本輪不算新問題。
- **值的紀律**：憑證四鍵（`CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`OPENCODE_API_KEY`）在 spec L15／L169、design L92、tasks L14 的 canary 範圍一致；新鍵集沒有把任何憑證鍵降級成「中介資料」。**唯一缺口是 endpoint 的 loader 位置**（新-中-2）；canary 對 endpoint 的 `key=`／`token=`／`@` 掃描本身可實作。另提醒實作：canary 掃 `ANTHROPIC_API_KEY` 時要跳過空字串（`ANTHROPIC_API_KEY=""` 是刻意佔位），否則會對每一行 log 誤報；這是實作注意事項，不是文件矛盾。
- **未決題數**：design Open Questions L216–222 共 7 題；overview L122–128 共 7 題、同序；fix2 也說 7 題。一致。
- **tasks 編號／依存**：1.0–1.7、2.0–2.7、3.0–3.4、4.1、5.1–5.5 無跳號；3.0 是本 repo、3.1–3.4 是 MyLinuxPool 的 c 單節（tasks L5）；5.4 歸檔 gate 與 design 決定 5 一致。
- **public repo**：新增文字掃過 diff（金鑰、token 值、絕對路徑、帳號樣式）沒有實際值；`MyLinuxPool#216`／`#237`、`MyPMO`、`MyAiStorage#85` 是工單／repo 代號；「private repo」只在 design Context L3 作為出處說明，未引用內容。可以放行這一項。

## 驗證紀錄

```
$ openspec validate --strict agent-provider-model-interface
Change 'agent-provider-model-interface' is valid   # exit 0

$ git -C <wt> diff --stat aa67051 78c08c6 -- openspec docs/research/design-33-overview.md
 design-33-overview.md | 27 +++++-----
 design.md             | 51 +++++++++++---------
 proposal.md           |  8 ++--
 spec.md               | 55 +++++++++++++++++++++-----
 tasks.md              | 32 ++++++++-------
（5 files；第 2 輪 9b383f7／40eb9a0 與第 3 輪 78c08c6 都在範圍內）
```

## 沒查到的地方

- **sidecar 的實作細節**：`build_env`／`_swap`／AgentRecord 的實際欄位、`POST /agents` 的 `ALLOWED_KEYS` 現況——本輪只讀介面層級文件，沒有 MyLinuxPool 的程式可核對；第 3 輪 (b) 的 AgentRecord「釘住」欄位因此只到設計層級。
- **PM 的 B-1／B-3 直接證據**：只有 `.work/notes-33-align.md` 的第二輪摘要，沒有 MyLinuxPool PM 的原始回覆；design／tasks 的「已確認」以該摘要為依據。
- **opencode pinned 版本（2.0.26／2.0.32）對 legacy `provider`／`small_model` 的接受度**：仍只以官網 schema 與 review-24 的 `opencode models` 輸出核對，沒有實機 binary。
- **`install.sh --check` 的版本輸出形狀**、**服務那台與 worker 的現況**：沒有連線、沒有跑 `--check`、沒有讀 `keys.env`；3.0–3.4 都未執行。
- **OpenSpec `archive`／`apply` 的實際行為**：只跑 `validate --strict`，沒有真的 archive／apply。

結論：需修（高 0、中 2、低 4）。

需修
