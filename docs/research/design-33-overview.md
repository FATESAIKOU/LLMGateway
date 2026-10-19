GLM-5.3

# LLMGateway #33 設計概覽：agent 用的介面改成輸入 provider＋model

> 設計作者的工作報告（2026-10-20）。完整設計在 `openspec/changes/agent-provider-model-interface/`（proposal／design／tasks／specs）；本頁是概覽：結論、三張圖、未決、沒查到的地方。

## 結論（一句）

新增 `GET /v1/resolve?agent=&provider=[&model=]`：LLMGateway 依政策挑 subscription、回 `meta`＋`env`（憑證／端點／模型的環境變數，名稱＋值），env 的值只進 sidecar 建的子行程環境；終態 get 退場，對外仍是三個介面（list、resolve、cooldown）。

## 實體圖（誰在哪、誰碰誰）

```
┌──────────────────── worker ／ Mac ─────────────────────┐
│  控制者(PM) ── POST /agents {agent, provider?, model?, │
│      │               session, workdir, input}          │
│      ▼                                                │
│   sidecar（一台機器一個）                               │
│      │  ①list（菜單）②resolve（點菜）③cooldown（額度） │
│      │  env＝resolve 的 env ＋ sidecar 維運變數         │
│      │  （值只在這裡疊；不進 log／status）               │
│      ├── agent 子行程（claude-code／opencode）          │
│      └── agora continue（同 session 接續；#229 互動     │
│           終端同樣只吃疊好的 env）                       │
└────────┼────────────────────────────────────────────── ┘
         │ SSH 通道（會合點 → loopback 4710；無應用層認證）
┌────────▼──────── LLMGateway（lgwd，服務那台）────────── ┐
│ resolve：選把（provider＋agent＋allow＋已部署→非冷卻     │
│   →priority→id）＋ 組 env（憑證／端點／模型）             │
│ list：菜單（priority、models、cooldown、available）      │
│ cooldown：記 until（以 subscription id）                 │
│ policy/default.json．keys.env(key-sync)．SQLite         │
│ （cooldowns、audit；稽核不含 env 值）                    │
└─────────────────────────────────────────────────────────┘
```

## 控制時序圖（開隊員與換把）

```
控制者        sidecar                        lgwd                 agent 子行程
  │ POST /agents ─►│                            │                     │
  │ {agent, provider, model}                    │                     │
  │            │ GET /v1/resolve?agent&provider&model               │
  │            │  ───────────────────────────► │                     │
  │            │      選把：同 provider＋allow 蓋 model＋已部署        │
  │            │      ＋非冷卻 → (priority, id) 最小；                │
  │            │      全部冷卻 → 409 cooling＋until（最早）           │
  │            │  ◄── {meta:{subscription, model, …}, env:{…}}      │
  │            │ 疊維運變數（AGORA_CLAUDE_CMD、permission、           │
  │            │ server password、身分變數…）                         │
  │            │ ── 子行程 env（值只在這裡）──► spawn／agora continue ─►│
  │ ◄─ 201 status {provider, model, subscription}（無 env 值）        │
  │                                                │                   │
  │            （額度用完）◄────────────────────────────────────────── │
  │            │ cooldown(meta.subscription, until) ─► lgwd 記 until  │
  │            │ list → 階梯挑下一個 (provider, model)〔或 subscription〕
  │            │ GET /v1/resolve?…（冷卻那把自動被排除）                │
  │            │ 溫和停 → agora 寫回 → 同一個 session continue、新 env ─►│
```

## 介面圖（終態的三個介面）

```
LLMGateway
──────────────────────────────────────────────────────────────────
GET  /v1/subscriptions                        list：菜單（無 key）
     → 200 {schema, subscriptions:[row…]}      row＝v5 形狀不變

GET  /v1/resolve?agent=<claude-code|opencode>
              &provider=<anthropic|opencode-zen|openrouter|ollama-cloud>
              [&model=<供應商自己的模型 id>]     ← model 可含 "/"（query 安全）
     → 200 {schema, meta:{subscription, provider, kind, agent,
                          model, models, endpoint, priority,
                          cooldown, available},
            env:{  claude-code：CLAUDE_CODE_OAUTH_TOKEN /
                               ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY /
                               ANTHROPIC_BASE_URL / ANTHROPIC_MODEL…，
                  opencode：OPENCODE_CONFIG_CONTENT（完整設定 JSON）}}
     → 400 bad_request（缺 agent/provider）｜no_model
       404 not_found（未知 provider）
       403 agent_not_allowed｜model_not_allowed（政策守門）
       409 not_deployed｜cooling（帶 until＝最早恢復）

POST /v1/subscriptions/{id}/cooldown {until}   ← id 來自 resolve 的 meta／list
     → 204                                    （形狀不變；v5 同款）

GET  /v1/subscriptions/{id}?agent=             get：並存期保留，
                                               sidecar 遷移＋驗收後移除
──────────────────────────────────────────────────────────────────
值不外洩：env 的值只在 sidecar 建的子行程環境；不進 lgwd 的 log／稽核，
不進 sidecar 的 log／GET /agents status（canary 掃描守著）。
```

## 未決（給本人；每題附建議）

1. opencode 的 permission 歸屬：建議 Gateway 組憑證＋模型、sidecar 併 permission（#233 的擁有權留在 sidecar）；替代＝Gateway 全組。→ 要 MyLinuxPool 同意。
2. get 的退場時點：建議 sidecar 遷移＋實機驗收後立刻開 d 單移除。
3. sidecar `POST /agents` 的欄位形狀：建議平面 `provider`＋`model`；替代＝`model:{provider, model}` 物件。→ MyLinuxPool 定。
4. 換把階梯的粒度：建議先維持 subscription 粒度；(provider, model) 粒度等 #32＋sidecar 階梯設定另開單（含「接續 session 不能用 Zen 免費」的限制）。
5. meta「到期」的語意：本設計解讀為冷卻 until；若本人指別的到期（例 key 到期），Gateway 沒有那資料，要另做。
6. resolve 是否也收「不指定 provider」：建議不要（issue 的輸入就是 provider＋model）。

## 沒查到／不確定的地方

- **Zen 免費層的限流單位**（per 帳號還是 per 模型）查無公開資料；影響：冷卻以 subscription 為單位，一個模型限流可能讓整把的其他模型也被跳過（設計列為已知限制）。
- **Zen 免費層「只收 opencode 原生新開 session」**（接續 session 會被上游拒）：出自本人 2026-10-17 的觀察（MyAiStorage#85；來源是 private repo 的輪換表筆記），非官方文件；只影響 sidecar 的換把階梯（未決 4），不影響 resolve 本身。
- **`LGW-V5-INTERFACE.md`**（sidecar 文件提到的介面契約檔）在本 repo 與 MyLinuxPool 的 master 都找不到（可能在別的 repo 或未推）；v5 介面的事實我改以本 repo 的 spec／程式＋sidecar 文件本文交叉確認，沒有缺口。
- **MyLinuxPool#216 的階段 2／3** 只讀到 issue 內文與 master 現況；若後續 PR 再動 `POST /agents` 的欄位，與 provider／model 欄位仍是正交合併（設計已註明合併點：core 的 ALLOWED_KEYS）。
- **worker 上實機驗收**（部署新版 lgwd、開兩個隊員）動服務那台與 worker：照工單規則寫成計畫、標明要問本人授權，本次未執行。
