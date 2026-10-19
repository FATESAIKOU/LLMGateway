對應 #33（層級 c）。順序：1（LLMGateway）→ 2（sidecar）→ 3（實機驗收）→ 4（get 退場）。子單草稿標層級（c／d）；2、3 是 MyLinuxPool 的單，由 PM 去對齊（design 決定 6），本 repo 不代開。

## 1. LLMGateway：resolve（#33 的實作本體；層級 c，就是本單）

- [ ] 1.1 選把與錯誤碼：`lgwd/errors.py` 加 `model_not_allowed`、`no_model`、`cooling`（帶 `until`）；`lgwd/registry.py` 加 resolve 的候選篩選（provider＋agent＋allow 涵蓋＋已部署→排除冷卻→priority→id）與全部冷卻的最早 `until`
- [ ] 1.2 env 組成：`lgwd/providers.py`（或新 `envs.py`）依（provider、kind、agent）組憑證／端點／模型變數；opencode 的 provider id 對映搬進 Gateway；`claude-subscription`＋model null 不給模型變數
- [ ] 1.3 HTTP：`lgwd/http.py` 加 `GET /v1/resolve` 路由（query：`agent`、`provider`、`model`；未知參數忽略；body 上限不適用）；log 只記方法／路徑／狀態碼
- [ ] 1.4 稽核：resolve 記一筆（時刻、subscription、agent、provider、model）；env 值與 key 同等對待——不進稽核／log
- [ ] 1.5 測試：spec 每個服務端 Scenario（loopback、假 key、假時鐘）；canary：env 值不出現在 log／資料庫／稽核／list 回覆
- [ ] 1.6 `lgw.client.resolve(agent, provider, model=None)`；`Result` 的 repr 沿用不印 data；`get` 保留（並存期）
- [ ] 1.7 以 git tag 發版（`install.sh --tag` 流程照舊）；get 照常不動，退版＝回舊 tag

## 2. MyLinuxPool：sidecar 改用 resolve（子單草稿；層級 c）

- [ ] 2.1 來源：`LLMGatewaySource` 加 resolve（list／cooldown 不變）；錯誤對照補 `cooling`（帶 until）、`model_not_allowed`、`no_model`
- [ ] 2.2 `POST /agents`：選填 `provider`＋`model`（形狀照未決 3 定案）；`subscription: {"id":…}` 硬切 400 `invalid_request`（照 #216 對舊形狀的先例）；兩者同時省略＝sidecar 從 list 挑預設再 resolve
- [ ] 2.3 driver：`build_env` 改「resolve 的 env＋維運變數」；opencode 的 permission 併進 `OPENCODE_CONFIG_CONTENT`（未決 1 定案後）；model-mismatch 檢查照舊
- [ ] 2.4 換把：cooldown 用 `meta.subscription`；下一個候選→resolve；409 `cooling`（until）→ 自動重開 job 排到 until
- [ ] 2.5 `GET /agents` status 加 `provider`／`model`／`subscription`（不含 env 值）
- [ ] 2.6 測試：假來源覆蓋新錯誤碼；Mac 上臨時跑（loopback＋假 key 的 lgwd＋sidecar）驗「兩個模型、同一把」的形狀，跑完即停
- [ ] 2.7 與 #216／#229 的合併點：core 的 `POST /agents` 欄位白名單（ALLOWED_KEYS）對一次

## 3. 實機驗收（掛在 2 的驗收節，或 MyPMO 掛 d 單；**動服務那台與 worker，要本人授權**）

- [ ] 3.1 部署新版 lgwd 到服務那台（`install.sh --tag`；前後 `--check`；get 仍在、可退版）
- [ ] 3.2 worker：開一個實作隊員（`opencode-zen`＋免費 A 模型）＋一個審查隊員（`opencode-zen`＋免費 B 模型）——兩個都拿到正確的模型（status、第一個 step 的模型、model-mismatch 通過）；稽核兩筆 resolve（同 subscription、不同 model）；log／status 無 env 值
- [ ] 3.3 換把演練：假額度訊號 → cooldown → resolve 下一個 → 同一個 agora session continue

## 4. get 退場（子單草稿，本 repo；層級 d；2／3 完成後才開）

- [ ] 4.1 移除 `GET /v1/subscriptions/{id}?agent=` 與 `lgw.client.get` 及其測試；介面文件同步（終態：list、resolve、cooldown）

## 5. 銜接與紀錄

- [ ] 5.1 PM 把「對 sidecar 的介面需求與建議」（design 決定 6）交 MyLinuxPool 的 PM 對齊（不由設計作者代問）
- [ ] 5.2 MyPMO dispatch（若需要；層級 d）：PM 派工參數帶 provider＋model 的接法（對模型輪換表的對接）；只有 dispatch 有「開 agent 的直通」時才需要
- [ ] 5.3 與 #32 的先後照 design 決定 7（互不 blocked；#33 先行）
