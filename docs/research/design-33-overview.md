GLM-5.3（修訂 2／3 輪；修訂 1 DeepSeek V4.1 Flash；原設計 GLM-5.3）

# LLMGateway #33 設計概覽：agent 用的介面改成輸入 provider＋model

> 設計作者的工作報告（2026-10-20，修訂 3）。完整設計在 `openspec/changes/agent-provider-model-interface/`（proposal／design／tasks／specs）；本頁是概覽：結論、三張圖、未決、沒查到的地方。修訂 1 的重點：resolve／`POST /agents` 可釘住某一把（`subscription`）、換把可排除剛耗盡那把（`exclude`）、opencode 改回片段由 sidecar 組 config、稽核補 provider／model 與 DB 遷移、get 的移除移到 d 單。修訂 2 的重點（第 2 輪審查 8 項全修）：`subscription`＋`exclude` 同時給＝400 寫死（選把步驟不套 `exclude`）、部署順序統一（先 lgwd、後 worker 映像，版本都對上才驗收）、釘住先限縮候選（該把推不出模型 → 400 `no_model` 不代換）、錯誤 precedence（形狀 400 → 查無 404 → 政策 403 → 狀態 409）、`endpoint` MUST NOT 內嵌憑證（loader 檢查＋canary 樣式掃描）、未決 7（`schema`／`env_schema` 兩欄）。修訂 3 的重點（PM 第二輪對齊）：env 鍵集（型別／空值／出現條件）寫死在 spec、未知鍵 sidecar 忽略＋WARN；釘住且冷卻＝sidecar waiting-quota（不換把）；排序（排在 #216 階段 2.5a（#237）與階段 3 之後）與 B-1 釘住解讀已由 PM 確認。

## 結論（一句）

新增 `GET /v1/resolve?agent=&provider=[&model=][&subscription=][&exclude=]`：LLMGateway 依政策挑（或釘住）subscription、回 `{schema, env_schema, meta, env}`（憑證／端點／模型的環境變數，名稱＋值；opencode 只回片段，config 由 sidecar 組），env 的憑證值只進 sidecar 建的子行程環境；get 在並存期保留、由 d 單退場，終態仍是三個介面（list、resolve、cooldown）。

## 實體圖（誰在哪、誰碰誰）

```
┌──────────────────── worker ／ Mac ─────────────────────┐
│  控制者(PM) ── POST /agents {id, agent, workdir,       │
│      │               provider?, model?, subscription?, │
│      │               session?, input?}                   │
│      │  （subscription 沿用 {"id": …}；其餘欄位省略）    │
│      ▼                                                │
│   sidecar（一台機器一個）                               │
│      │  ①list（菜單）②resolve（點菜；可釘住／排除）     │
│      │  ③cooldown（額度）                               │
│      │  env＝完整 base env＋維運變數＋resolve env       │
│      │  （不篩選 overlay；值只在這裡疊；不進 log／status）│
│      │  opencode：用 resolve 片段＋permission／          │
│      │  instructions 組 OPENCODE_CONFIG_CONTENT          │
│      ├── agent 子行程（claude-code／opencode）          │
│      └── agora continue（同 session 接續；#229 互動     │
│           終端同樣只吃疊好的 env）                       │
└────────┼────────────────────────────────────────────── ┘
         │ SSH 通道（會合點 → loopback 4710；無應用層認證）
┌────────▼──────── LLMGateway（lgwd，服務那台）────────── ┐
│ resolve：釘住／排除＋選把（provider＋agent＋allow＋      │
│   已部署→非冷卻→priority→id）＋組 env 片段（憑證／       │
│   端點／模型）                                           │
│ list：菜單（priority、models、cooldown、available）      │
│ cooldown：記 until（以 subscription id；粒度見未決 2）   │
│ policy/default.json．keys.env(key-sync)．SQLite          │
│ （cooldowns、audit＋provider／model；稽核不含憑證值）    │
└─────────────────────────────────────────────────────────┘
```

## 控制時序圖（開隊員與換把）

```
控制者        sidecar                        lgwd                 agent 子行程
  │ POST /agents ─►│                            │                     │
  │ {id, agent, provider?, model?, subscription?}                    │
  │ （workdir／session／input 省略）                                 │
  │            │ （新形狀）GET /v1/resolve?agent&provider&model       │
  │            │            [&subscription=<釘住>]                    │
  │            │  ───────────────────────────► │                     │
  │            │      選把：釘住先限縮＝只看該把；同 provider＋allow │
  │            │      蓋 model／能推導＋已部署＋非冷卻 → (priority,  │
  │            │      id) 最小；釘住那把推不出 → 400 no_model        │
  │            │      （不代換）；那把冷卻 → 409 cooling＋until      │
  │            │      全部冷卻 → 409 cooling＋until（最早）           │
  │            │      排除後無候選 → 409 no_candidate                  │
  │            │  ◄── {schema, env_schema, meta:{subscription,…}, env}│
  │            │ meta.subscription 為準；與 list 的選擇不同→記 log／回報│
  │            │ 完整 base env＋維運變數＋resolve env overlay           │
  │            │ opencode：片段＋permission／instructions→config       │
  │            │ ── 子行程 env（值只在這裡）──► spawn／agora continue ─►│
  │ ◄─ 201 status {provider, model, subscription}（無 env 值）        │
  │                                                │                   │
  │            （額度用完）◄────────────────────────────────────────── │
  │            │ cooldown(meta.subscription, until) ─► lgwd 記 until  │
  │            │ （登錄失敗也照做）resolve 帶 exclude=剛耗盡的 id        │
  │            │  ───────────────────────────► │ 不會選回同一把       │
  │            │  ◄── 新 meta／env（或 409 cooling／no_candidate）     │
  │            │ 溫和停 → agora 寫回 → 同一個 session continue、新 env ─►│
  │            │ 釘住那把用完／冷卻中：waiting-quota（記 cooldown、      │
  │            │ 保留 agent、到期再 resolve 同一把；不換把（未決 1）     │
```

## 介面圖（終態的三個介面；get 在過渡期保留）

```
LLMGateway
──────────────────────────────────────────────────────────────────
GET  /v1/subscriptions                        list：菜單（無 key）
     → 200 {schema, subscriptions:[row…]}      row＝v5 形狀不變
                                                （cooldown／available 仍在）

GET  /v1/resolve?agent=<claude-code|opencode>
              &provider=<anthropic|opencode-zen|openrouter|ollama-cloud>
              [&model=<供應商自己的模型 id>]    ← 可含 "/"、"+"
              [&subscription=<id>]              ← 釘住某一把（provider 要一致）
              [&exclude=<id>[,<id>…]]           ← 換把排除剛耗盡那把
     → 200 {schema, env_schema,
            meta:{subscription, provider, kind, agent,
                  model, models, endpoint, priority},
            env:{  claude-code：CLAUDE_CODE_OAUTH_TOKEN /
                               ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY /
                               ANTHROPIC_BASE_URL / ANTHROPIC_MODEL…，
                   opencode：OPENCODE_API_KEY / OPENCODE_PROVIDER_ID /
                             OPENCODE_MODEL / OPENCODE_SMALL_MODEL
                             （片段；未知鍵 sidecar 忽略＋WARN；config 由 sidecar 組）}}
        Cache-Control: no-store、Pragma: no-cache
     → 400 bad_request（缺欄位／未知 agent／subscription 與 provider 不一致／
                        subscription＋exclude）｜no_model
       404 not_found（未知 provider／未知 subscription）
       403 agent_not_allowed｜model_not_allowed（政策守門）
       409 not_deployed｜cooling（帶 until）｜no_candidate（exclude 後無候選）
       多個同時成立 → 回最前一層：輸入形狀 400 → 查無 404 → 政策
       403／no_model → 狀態 409（與選把步驟同一軸）

POST /v1/subscriptions/{id}/cooldown {until}   ← id 來自 resolve 的 meta／list
     → 204                                    （形狀不變；v5 同款）

GET  /v1/subscriptions/{id}?agent=             get：過渡期保留，
                                               sidecar 遷移＋驗收後由 d 單移除
──────────────────────────────────────────────────────────────────
值不外洩：env 的憑證值只在 sidecar 建的子行程環境；不進 lgwd 的
log／稽核，不進 sidecar 的 log／GET /agents status。canary 分工：
Gateway 掃 Gateway（log／DB／稽核／list）；sidecar 的 log／status
由 MyLinuxPool 的 c 單加 canary。
```

## 未決（給本人；每題附建議）

1. **釘住的那把額度用完時，是換另一把還是等這一把？** 建議不自動換——sidecar 當額度用完處理（waiting-quota：記 cooldown、保留 agent、到期再 resolve 同一把；釘住是明確的帳號選擇，自動換會違反隱私／計費／額度保留的意圖）。替代＝自動換，但要通知控制者，且要定「換到哪一把」。
2. **冷卻粒度要不要細到 `(subscription, model)`？** 建議先維持 subscription 粒度（保守；Zen 限流單位查無）；resolve 稽核已記 model，未來可分析。限制：同一把的另一個免費模型也會被冷卻；3b 只證明初始選擇。→ 下一單再決定。
3. **未釘住時 `anthropic` 兩把的預設選法**：建議維持 priority（personal 先、company 後），因為釘住已提供明確入口；平均分攤要用量資料（v5 已移除），角色規則要 resolve 有身分（會擴介面）。
4. **get 的退場時點**：建議 sidecar 遷移＋實機驗收（3b／3c／3d）後開 d 單；本 change 不 REMOVED get，d 單的 delta 以那時的主規格為底。
5. **meta「到期」的語意**：本設計解讀為冷卻 until（409 帶、list 帶；resolve 的 meta 不再帶 `cooldown`／`available`）；若本人指 key 到期，Gateway 沒有那資料，要另做。
6. **resolve 是否也收「不指定 provider」**：建議不要（issue 的輸入就是 provider＋model；不指定時由 sidecar 從 list 挑好再 resolve）。
7. **`schema`／`env_schema` 兩個版本欄位**（新-低-4）：建議維持兩欄——meta 形狀與 env 鍵集獨立演進（env 鍵集變才逼 sidecar 重驗 env；meta 的 additive 變更未來可放寬成「讀已知欄位、忽略新欄位」）。替代＝合併一個版本號（少一個欄位與測試線，但 meta 小變更會逼整體回覆拒收）。

## 沒查到／不確定的地方

- **Zen 免費層的限流單位**（per 帳號還是 per 模型）查無公開資料；影響：冷卻以 subscription 為單位，一個模型限流可能讓整把的其他模型也被跳過（未決 2）。
- **Zen 免費層「只收 opencode 原生新開 session」**（接續 session 會被上游拒）：出自本人觀察（2026-10-17；MyAiStorage#85），非官方文件；只影響 sidecar 的換把階梯與 3b 的 session 選擇，不影響 resolve 本身。
- **opencode pinned 版本對 legacy `provider`／`small_model` 的接受度**：只以 opencode 官網 config schema（2026-10-20 抓）確認 `provider`／`small_model` 已標 deprecated、新的 `providers` 用 `settings`；worker 釘的 2.0.26、Mac 2.0.32 是否仍接受 legacy 鍵，沒有實機 binary 可驗（低-7）。本設計把 config 形狀留給 sidecar，就是為了不讓 Gateway 綁這個版本。
- **sidecar `build_env` 的內部細節**：`ANTHROPIC_MODEL`／`ANTHROPIC_SMALL_FAST_MODEL` 是否由現行 driver 設定、`models.default` 為 null 時「`allow` 第一個不含萬用字元的項」的實際順序、`OPENCODE_CONFIG_CONTENT` 除 `apiKey`／model／permission 外還帶什麼——sidecar.md 只寫介面層級，沒有這些實作細節；本設計只依介面層級，沒有拿作者結論當事實。
- **`LGW-V5-INTERFACE.md`**（sidecar 文件提到的介面契約檔）在本 repo 與 MyLinuxPool 的 master 都找不到（可能在別的 repo 或未推）；v5 介面的事實改以本 repo 的 spec／程式＋sidecar 文件本文交叉確認，沒有缺口。
- **MyLinuxPool#216 的 `harness` 欄位、#229 的終端／hook 細節、#216 階段 2.5a 的 `_swap` 實作**：只讀到 issue 內文與 master 現況；排序已由 MyLinuxPool PM 確認（2026-10-20：#33 sidecar 實作排在 #216 階段 2.5a（#237）與階段 3 merge 之後），實際依存以 PMO 為準。
- **OpenSpec 的 `archive`／`apply` 實際行為**：只跑 `validate --strict`，沒有真的 archive／apply；歸檔 gate 是依 delta／REMOVED 語意推理，沒有用 `openspec archive` 實測（中-3）。
- **`#33` 的 issue 全文與「meta 到期」的原意**：只有工單標題與 prompt 摘要，沒有讀 issue 內文；未決 5 無法核對。
- **服務那台與 worker 的現況**：沒有連線、沒有讀 `keys.env`、沒有跑 `install.sh --check`；部署與驗收是計畫，沒有實機證據；3.1 的 worker 映像重建也尚未做。
