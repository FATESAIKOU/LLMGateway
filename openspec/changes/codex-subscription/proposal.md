對應工單：#187（設計單；只寫文件，實作拆成 tasks 的 PR P1～P6）。上層：MyPMO#31「AiCompany 期 1」與 MyPMO#4「啟動 LLMGateway」（依本 repo 的 AGENTS.md）。相關：#119（agy 的 worker 登入，同一套分發路線）、MyLinuxPool#797（sidecar 的 codex driver）。架構範圍：`.work/187-arch-scope.md`（不進 git；其結論已搬進 design）。

## Why

本人 2026-11-07 開了 ChatGPT 一個月試用，要把 Codex CLI（ChatGPT 訂閱登入，不是 API key）當實作隊員的後端，分擔 Claude 週額度。Gateway 現在沒有這把訂閱：

- `AGENTS`、`SUBSCRIPTIONS` 兩個固定集合都沒有 `codex`／`chatgpt-personal`，含新 row 的政策檔載入失敗（見 `subscription-registry`「新增訂閱需兩處一致」）；`agent=codex` 在 resolve 回 400。
- `local-login` 的條款（`lgwd/policy.py` 的 `_AGY_ONLY`）與 `build_env` 的防線都寫死 `agy`；Codex 的形狀與 agy 相同（憑證在呼叫方那台機器、Gateway 不碰），卻要重寫一份。
- 登入檔 `auth.json` 會被 Codex 自己更新並輪替 refresh token，多處使用會互相撞（design Q6），需要先把「誰負責」寫死，才不會有人以為 Gateway 要保管它。
- 用量、冷卻、隱私等級、使用界線都沒有決定。

## What Changes

- 不新增 kind：沿用 `local-login`，把「agy 專用」泛化成「每把 local-login 訂閱綁一個 agent」：`{antigravity: agy, chatgpt-personal: codex}`（design Q1、Q3）。
- 新增 provider `openai`、訂閱 `chatgpt-personal`、agent `codex`；resolve 對它回 `env: {}`、模型只走 `meta.model`、`usage` 為 `not_applicable`（design Q2、Q4）。
- `lgw/signals.py` 加 codex 的額度判讀（P3，design Q8）；本 change 的 spec 不新增端點。
- delta spec：`subscription-registry`、`resolve`、`resolve-env`、`role-allocation`、`key-vault`、`registry-errors`、`subscription-cooldown` 各補 codex（見 `specs/`）；使用界線（Q10）不在 delta：本人已定『只用原版 Codex CLI』，以 design 文字記錄。
- design 記錄 Q1～Q11 的查證結果、定案與被捨棄的方案；需要本人決定的（Q6、Q7、Q9、Q10、Q11）附選項與建議，本人 2026-11-07 已全部定案（見 design）。
- tasks 依 P1～P6 切 PR，每個 PR 有可驗的完成判準與契約編號（C-1…）。

## What Does Not Change

- 不在本 change 改任何程式、測試、政策檔（`lgwd/`、`lgw/`、`tests/`、`policy/` 都不動）；`policy/default.json` 加這一筆是 P4，且要等本人定隱私等級。
- Gateway 不保管、不分發、不轉手 `auth.json`；它不進 key 檔、不佔 `KEY_NAMES`。保管與分發歸 MyLinuxPool 的 capability。
- 不新增端點、不 bump `schema`／`env_schema`。
- 不做 5 小時／每週用量的讀取（P6；Q7 已定試用期 U0，期滿再議）。
- `model-rotation-survey` 的 spec 與 `docs/model-rotation.md` 屬 P5，不在本 change。
- 不接 OpenAI API key（若日後要，同一個 provider 以 api-key kind 另開一把，到時 bump `env_schema`）。
- 不替本人定隱私等級與使用界線。

## Impact

- 受影響的 capability：`subscription-registry`、`resolve`、`resolve-env`、`role-allocation`、`key-vault`、`registry-errors`、`subscription-cooldown`（皆為補文字與泛化，既有 agy 情境不變）。
- 受影響的將來程式（本 change 不改）：`lgwd/policy.py`、`lgwd/providers.py`、`lgwd/registry.py`、`lgw/signals.py`、`policy/default.json`。
- 部署：P2、P4 合併後要升級 Gateway 主機的 lgwd，是真機操作，經 PMO 取得本人單次授權後由 PM 執行，不在 PR 內。
