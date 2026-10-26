對應工單：#88（層級 c）「新增 groq 訂閱（免費層，opencode 專用）」。

## Why

#80 的調查（`docs/research/free-model-capacity.md` §3.6、§5 #3）指出 Groq 免費方案有公布上限（`openai/gpt-oss-120b`：30 RPM／1000 RPD／8K TPM／200K TPD），RPD 比 OpenRouter 免費層大一個量級，可以在 zen 用完之後接手短任務與審查。本人經 PMO（2026-10-27）同意加。

現行 spec 的 endpoint 與 `OPENCODE_PROVIDER_ID` 對照沒有 groq，不寫進 spec 就沒有可對照的介面約定。

## What Changes

- `lgwd/policy.py` 的 `SUBSCRIPTIONS` 白名單加 `groq`；與 `opencode-zen`／`opencode-go` 同一條檢查，`agents` 不是剛好 `["opencode"]` 就丟 `PolicyError`（`_OPENCODE_ONLY`）。模組內註明「models.allow 只放免費層的模型」是政策檔的資料面約定、以及免費層的訓練政策頁查不到。
- `lgwd/providers.py` 加 `GroqProvider`：`provider`／`opencode_provider_id` 都是 `groq`，`kind` 為 `api-key`，`endpoint` 為 `https://api.groq.com/openai/v1`（出處見程式註解與報告）。`make_usage_fetcher` 沿用預設（回 `None`，`usage` 為 `not_applicable`）。
- `lgwd/keys.py` 的 `KEY_NAMES` 加 `groq` → `GROQ_API_KEY`（自己一個鍵名，不與其他訂閱共用）。
- `policy/default.json` 加這一筆：priority 25、`agents` `["opencode"]`、`models.allow` 寫死免費層可用的三個 id（`openai/gpt-oss-120b`、`openai/gpt-oss-20b`、`qwen/qwen3.8-27b`）、`models.default` 為 `openai/gpt-oss-120b`。不放 `*`。
- spec `subscription-registry`：MODIFIED「endpoint 的語意」與「env 的組成」，把 groq 的 endpoint 與 provider id 補進既有清單。
- 測試：provider 解析（list／get 的 provider、endpoint、`models`）、policy 驗證（`claude-code` 不得使用該訂閱；allow 只放免費層 id、不含萬用字元）、key 檔解析與值紀律（canary 不進 repr／錯誤訊息）、既有「訂閱清單」類斷言一起更新。

## What Does Not Change

- 不加 usage fetcher（Groq 沒有公布查剩餘配額的端點；官方 rate-limits 頁只有回應標頭）。
- 不改 resolve／list／get／冷卻介面，不改 `env` 的鍵集（同一個 `env_schema`；`OPENCODE_PROVIDER_ID` 只是多一個值）。
- 不改 key 檔格式；不動既有訂閱的 priority（zen 10 → go 15 → openrouter 20 → groq 25 → ollama 30 → claude-personal 40）。
- 不改 `openspec/specs/**`（只動本 change 的 delta）；不部署、不需要任何 key 值。

## Impact

- 政策檔多一筆，`opencode` 的 resolve 候選多一把；`claude-code` 的候選不變（groq 只給 opencode）。
- 新的一個鍵名 `GROQ_API_KEY`；沒部署時 `list` 的 `available` 為 false，resolve 對它回 409 `not_deployed`（與其他把相同）。
- 免費層的**訓練政策頁查不到**（`console.groq.com/docs/your-data` 只寫「預設不保留」與 ZDR，沒有 training 字樣）：這把只給 public repo 與假資料的工作，不碰真資料。
- 測試：resolve 回的 env 是 groq 的 endpoint 與 provider id；allow 以外的模型（例 `llama-3.1-8b-instant`）被拒；`agents` 含 `claude-code` 的政策檔被拒收。
