對應工單：#77（層級 d）「OpenCode Go 免費模型登錄成 subscription `opencode-go`（共用 Zen key）＋驗 Zen 端點」。

## Why

本人要把 OpenCode Go 的免費模型登錄成 Gateway 的另一把 subscription `opencode-go`，和 OpenCode Zen 用同一把 key（本人 2026-10-26 經 PMO 定案：priority 15、`agents` 只有 `opencode`、`models.allow` 為 `["*-free"]`、`models.default` 為 `iceman-contributor-free`；不加新鍵名）。現行 spec 的 endpoint 與 `OPENCODE_PROVIDER_ID` 對照只列到 `opencode-zen`，不寫進 spec 就沒有可對照的介面約定。

## What Changes

- `lgwd/policy.py` 的 `SUBSCRIPTIONS` 白名單加 `opencode-go`；與 `opencode-zen` 同一條檢查，`agents` 不是剛好 `["opencode"]` 就丢 `PolicyError`。
- `lgwd/providers.py` 加 `OpenCodeGoProvider`：`provider`／`opencode_provider_id` 都是 `opencode-go`，`kind` 為 `api-key`，`endpoint` 為 `https://opencode.ai/inference/go/openai/v1`。
- `lgwd/keys.py` 的 `KEY_NAMES` 讓 `opencode-go` 對到 `OPENCODE_ZEN_API_KEY`（共用鍵名；key 檔仍只寫一行）。
- `policy/default.json` 加這一筆（priority 15、`agents` `["opencode"]`、`allow` `["*-free"]`、`default` `iceman-contributor-free`）。
- spec `subscription-registry`：MODIFIED「endpoint 的語意」與「env 的組成」，把 Go 的 endpoint 與 provider id 補進既有清單。

## What Does Not Change

- 不改 Zen 的 endpoint（`https://opencode.ai/zen/v1`）；舊路徑是否還通要用 key 實測（另一份工作）。
- 不改 resolve／list／get／冷卻介面，不改 `env` 的鍵集（同一個 `env_schema`）。
- 不加新鍵名、不改 key 檔格式、不加新的用量來源（Go 的 `usage` 仍是 `not_applicable`）。
- 不寫實測腳本（依工單，那是下一份工作）。

## Impact

- 政策檔多一筆，resolve 的候選多一把；既有 priority 不變（zen 10 → go 15 → openrouter 20 → ollama 30 → claude-personal 40）。
- Go 與 Zen 共用同一個 key 值：`KeyStore.get`／`deployed` 以訂閱 id 查同一個鍵名，key 檔只寫一行，不是重複鍵；list 回應不含 key，沒有反查鍵名的介面。
- 測試：resolve 回的 env 是 Go 的 endpoint 與 provider id；allow 以外的模型（例 `kimi-k3`）被拒；`agents` 含 `claude-code` 的政策檔被拒收；key 對到 Zen 的鍵名。
