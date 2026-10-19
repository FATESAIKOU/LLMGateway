對應 #33（層級 c）。順序：1（LLMGateway）→ 2（sidecar）→ 3（實機驗收）→ 4（get 退場，d 單）。子單草稿標層級（c／d）；2、3 是 MyLinuxPool 的單，由 PM 去對齊（design 決定 6），本 repo 不代開。

**排序與依存（B-5）**：2 的 sidecar 實作排在 MyLinuxPool#216 階段 2.5a（換把／自動重開重 render harness，改 `_swap`）merge 之後；#216 的 2a／2.6／2.8（opencode pin 2.0.32）／階段 3、#229 也會碰到 `POST /agents` 或 `_swap`，合併時對一次。**這是排序建議，排序以 PMO 為準**；若 PMO 決定先做 #33，2.7 的合併點要先對。

**3 的層級／擁有者（低-11）**：3.0／3.2／3.3／3.4 是 **MyLinuxPool 的 c 單驗收節**（sidecar 釘在 worker 映像裡，動 worker 要那個 repo 的單）；3.1 是本 repo c 單的部署步驟。LLMGateway 這一側只提供驗收腳本／檢查清單，不動 worker。若 PMO 要另掛 d 單，以 PMO 為準。

## 1. LLMGateway：resolve（#33 的實作本體；層級 c，就是本單）

- [ ] 1.0 政策 loader（中-6）：`lgwd/policy.py` 加 openrouter 的 `agents == ("opencode",)` 檢查（不合法丟 `PolicyError`，固定字串）；`tests/test_policy.py` 加負向案例（`["claude-code","opencode"]` 被拒）
- [ ] 1.1 選把與錯誤碼（高-1、中-8、低-3、低-4）：`lgwd/errors.py` 加 `model_not_allowed`、`no_model`、`cooling`（帶 `until`）、`no_candidate`；`agent` 列舉先驗（未知 → 400 `bad_request`）；`lgwd/registry.py` 加 resolve 的候選篩選（provider＋agent＋allow 涵蓋／能推導模型 → 釘住或 exclude → 已部署 → 非冷卻 → priority→id）與錯誤 precedence；釘住時只驗該把、冷卻回 409 不代換；exclude 後無候選回 409 `no_candidate`；全部冷卻的最早 `until`
- [ ] 1.2 env 組成（中-1、中-4、低-7、B-2）：`lgwd/providers.py`（或新 `envs.py`）依（provider、kind、agent）組憑證／端點／模型變數；claude 系照 v5；opencode **只回片段** `OPENCODE_API_KEY`／`OPENCODE_PROVIDER_ID`／`OPENCODE_MODEL`／`OPENCODE_SMALL_MODEL`（不回 `OPENCODE_CONFIG_CONTENT`、不回 `baseURL`）；`claude-subscription`＋model null 不給模型變數；provider id 對映搬進 Gateway
- [ ] 1.3 HTTP（低-5、低-6、低-8）：`lgwd/http.py` 加 `GET /v1/resolve` 路由（query：`agent`、`provider`、`model`、`subscription`、`exclude`；未知參數忽略；body 上限不適用）；query 用 `parse_qs` form 解碼、`+` 由 client 編成 `%2B`；resolve 與 get 的 200 加 `Cache-Control: no-store`、`Pragma: no-cache`；回覆帶 `schema`（端點版本）與 `env_schema`（env 鍵集版本）；log 只記方法／路徑／狀態碼
- [ ] 1.4 稽核與 DB 遷移（中-1、中-2）：`lgwd/store.py` 的 `SCHEMA` 加 `provider TEXT`／`model TEXT`（nullable）；啟動時 `PRAGMA table_info(audit)` 檢查，缺欄位就 `ALTER TABLE audit ADD COLUMN …`（additive）；`audit_add`／`audit_all` 加欄位；resolve 記（時刻、subscription、agent、provider、model）；get（過渡期）照舊記 agent；冷卻照舊記 until
- [ ] 1.5 測試（中-1、中-2、中-10、低-6、低-8）：spec 每個服務端 Scenario（loopback、假 key、假時鐘）；canary **只掃憑證值**（`CLAUDE_CODE_OAUTH_TOKEN`／`ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`OPENCODE_API_KEY`；過渡期 get 的 `OPENCODE_CONFIG_CONTENT` 內 apiKey 也算）不出現在 log／資料庫／稽核／list 回覆；`env_schema` 升高時 sidecar 拒收的測試（替身）；`model=a+b` 編碼；`Cache-Control` 斷言；從 v5 schema 的 DB 檔啟動並寫入 resolve；舊 DB 沒有新欄位的測試；舊 lgwd（v5 tag）讀新 DB 不受影響
- [ ] 1.6 `lgw.client.resolve(agent, provider, model=None, subscription=None, exclude=None)`；query 值用 `urllib.parse.quote(value, safe="")`；`Result` 的 repr 沿用不印 data；`get` 保留（並存期）
- [ ] 1.7 以 git tag 發版（`install.sh --tag` 流程照舊）；get 照常不動，退版＝回舊 tag；部署順序：lgwd 先、worker 映像後

## 2. MyLinuxPool：sidecar 改用 resolve（子單草稿；層級 c）

- [ ] 2.0 **排序 gate**：等 MyLinuxPool#216 階段 2.5a merge 後開工（B-5；以 PMO 為準）；開工前對一次 `_swap` 與 `POST /agents` 的欄位白名單
- [ ] 2.1 來源：`LLMGatewaySource` 加 resolve（list／cooldown 不變）；resolve 404 時回 get（舊 Gateway／退版）；錯誤對照補 `cooling`（帶 until）、`no_candidate`（停下回報）、`model_not_allowed`、`no_model`；sidecar 對控制者仍用巢狀 `{"error":{"code","message"}}`（Gateway 的平面碼是內部映射）
- [ ] 2.2 `POST /agents`（中-9、B-1、B-3、低-1、低-2）：新形狀平面 `provider`＋選填 `model`，並可帶選填 `subscription` 釘住；`subscription` 沿用現行物件形狀 `{"id": …}`（sidecar 取 `.id` 傳給 resolve）。組合矩陣照 design 決定 6.1（只給 `subscription`＝舊形狀走 get；三者都省略＝sidecar 從 list 挑預設再 resolve；給 `provider`＝新形狀，`subscription` 解讀為釘住；`subscription`＋`model` 無 `provider`、`model` 無 `provider`、provider 不一致 → 400 `invalid_request`）；呼叫端清冊（PM 腳本、dispatch、驗收腳本）＋過渡期（舊形狀至少保留到 d 單，不一次硬切合法功能）；`model` 是供應商 id，收到 `<provider>/<model>` 要正規化或回 400；`id` 必填；欄位白名單兩處（`http.py` open 欄位集合、core `_JOB_ARGS["open"]`）加 `provider`／`model`，並確認 `subscription` 這個舊鍵仍被接受（新形狀的釘住沿用同一個鍵）
- [ ] 2.3 driver（中-4、低-7、B-2）：`build_env` 改「sidecar 完整 base env（PATH／HOME／Locale…）＋sidecar 維運變數＋resolve env 的不篩選 overlay」；sidecar 自己行程**繼承來的** `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 不得原樣進子行程（維運變數另外疊）；opencode 的 `OPENCODE_CONFIG_CONTENT` 由 sidecar 用 resolve 片段＋`permission`／`instructions` 組（sidecar 擁有這兩個鍵與 config 形狀）；過渡期收到舊 Gateway 的整包 `OPENCODE_CONFIG_CONTENT` 照舊解析；model-mismatch 檢查照舊（第一個 step 的模型＝`meta.model`）
- [ ] 2.4 換把（中-8、未決 1）：cooldown 用 `meta.subscription`；下一個候選→resolve 帶 `exclude=<剛耗盡的 id>`（cooldown 登錄失敗也帶）；409 `cooling`（until）→ 自動重開 job 排到 until；409 `no_candidate` → 停下回報；釘住的那把額度用完 → 依未決 1 的安全預設不自動換、回報失敗
- [ ] 2.5 `GET /agents` status：**保留** `subscription`，加 `provider`／`model`（不含 env 值）
- [ ] 2.6 測試：假來源覆蓋新錯誤碼（含 `no_candidate`）；Mac 上臨時跑（loopback＋假 key 的 lgwd＋sidecar）驗「兩個模型、同一把」的形狀，跑完即停；sidecar 自己環境的 `ANTHROPIC_API_KEY=canary` 不進子行程；`env_schema` 升高拒收；`model=opencode/big-pickle` 的正規化／400；sidecar 的 log／status 憑證值 canary（低-10，MyLinuxPool 的 c 單）
- [ ] 2.7 與 #216／#229 的合併點：core 的 `POST /agents` 欄位白名單（ALLOWED_KEYS）與 `_swap` 路徑各對一次；#216 2.8 的 opencode pin 2.0.32 要驗片段到 config 的映射

## 3. 實機驗收（MyLinuxPool c 單的驗收節；3.1 是本 repo 的部署；**動服務那台與 worker，要本人授權**）

- [ ] 3.0 建／部署含新 sidecar 的 worker 映像，`sidecar --version` 對上 MyLinuxPool 的 c 單 commit（中-5；沒有這步，3.2／3.3 會拿舊 sidecar 驗新介面）
- [ ] 3.1 部署新版 lgwd 到服務那台（`install.sh --tag`；前後 `--check`；get 仍在、可退版）；順序在 worker 映像之前
- [ ] 3.2 worker：**新開 session**（省略 `session`，避開 Zen 免費層「只收原生新開 session」的上游限制；若 upstream 仍拒，改用別家模型重跑，這不是 resolve 的失敗）開一個實作隊員（`opencode-zen`＋免費 A 模型）＋一個審查隊員（`opencode-zen`＋免費 B 模型）——兩個都拿到正確的模型（status、第一個 step 的模型、model-mismatch 通過）；稽核兩筆 resolve（同 subscription、不同 model）；log／status 無憑證值。**這只證明初始選擇**；額度事件後換另一個免費模型不在本驗收（未決 2）
- [ ] 3.3 換把演練（中-5、中-8）：**手動** `POST /v1/subscriptions/{id}/cooldown`（用 `meta.subscription`）模擬額度訊號（不假裝 agent 訊號）→ resolve 帶 `exclude=<剛耗盡的 id>` → 下一個候選 → 同一個 agora session continue；另一次讓 cooldown 登錄失敗（例：送過去時間或暫時停 lgwd），確認 `exclude` 仍換把、`no_candidate` 時 sidecar 停下回報
- [ ] 3.4 釘住演練（高-1）：以 `subscription=claude-company` 開一個隊員，確認 `meta.subscription` 是 company（不是 priority 較高的 personal）；額度用完的行為依未決 1（預設不自動換、回報）

## 4. get 退場（子單草稿，本 repo；層級 d；2／3 完成後才開）

- [ ] 4.1 開單條件：sidecar 遷移＋3 驗收完成＋get 呼叫為零；移除 `GET /v1/subscriptions/{id}?agent=` 與 `lgw.client.get` 及其測試；介面文件同步（終態：list、resolve、cooldown）；delta spec 以那時的主規格為底 `REMOVED` get（本 change 不做，中-3）

## 5. 銜接與紀錄

- [ ] 5.1 PM 把「對 sidecar 的介面需求與建議」（design 決定 6）交 MyLinuxPool 的 PM 對齊（不由設計作者代問）；把 B-5 的排序建議（2 排在 #216 2.5a 之後）一起交，**排序以 PMO 為準**
- [ ] 5.2 MyPMO dispatch（若需要；層級 d）：PM 派工參數帶 provider＋model＋選填 subscription 的接法（對模型輪換表的對接）；只有 dispatch 有「開 agent 的直通」時才需要
- [ ] 5.3 與 #32 的先後照 design 決定 7（互不 blocked；#33 先行）
- [ ] 5.4 歸檔 gate（中-3）：`gateway-v0` 已歸檔（本 delta 的基準存在於主規格）＋1 實作完成＋2／3 完成，才 `openspec archive agent-provider-model-interface`；get 的移除不是本 change 的歸檔條件（d 單可後做）；`gateway-v0` 歸檔前，主規格 `agent-lease` 仍是平均分攤舊語意
- [ ] 5.5 sidecar 的值紀律 canary（低-10）：在 MyLinuxPool 的 c 單加「sidecar log／`GET /agents` status 不含憑證值」的測試；本 repo 的 canary 只掃 Gateway
