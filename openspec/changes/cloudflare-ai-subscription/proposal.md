對應工單：#89（層級 c）「新增 cloudflare-ai 訂閱（Workers AI 免費額度，opencode 專用）」。**本 change 只是設計**：實作在本人裁定「判斷點」之後另開 PR。

## Why

#80（`docs/research/free-model-capacity.md` §3.7、§5 #5）：Cloudflare Workers AI 每天 10,000 neurons、00:00 UTC 重置，官方明文 ZDR＋不訓練——免費來源裡唯一能碰真資料的（但書見 §3.7）。本人經 PMO（2026-10-27）同意加。

和 groq 不同，這家的 OpenAI 相容 endpoint 路徑含 **account id**，所以需要兩個值（API token＋account id），而現行 `KEY_NAMES` 是「一訂閱一鍵」、`endpoint` 是固定字串。怎麼接是這張的設計點，取捨在 `design.md`。

## What Changes（建議案；待本人裁定「判斷點」）

- `lgwd/policy.py`：`SUBSCRIPTIONS` 與 `_OPENCODE_ONLY` 加 `cloudflare-ai`。
- `lgwd/providers.py`：加 `CloudflareAIProvider`，`endpoint` 為樣板 `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1`；加進 `PROVIDERS`；resolve 時把 `{account_id}` 置換進 `meta.endpoint`。
- `lgwd/keys.py`：`KEY_NAMES["cloudflare-ai"] = "CLOUDFLARE_API_KEY"`，另加「附屬值」`CLOUDFLARE_ACCOUNT_ID`（key 檔多一行）；兩者都在才算部署。
- `policy/default.json`：priority 28、`agents` `["opencode"]`、`models.allow` 只放免費額度可用的模型、不放 `*`。
- spec `subscription-registry`：MODIFIED「endpoint 的語意」「env 的組成」，ADDED「endpoint 樣板與 account id」。
- 測試（假值，不接真端點）見 `design.md` §5。

## What Does Not Change

- 不加 usage fetcher（官方只有 dashboard，沒有查剩餘 neurons 的端點；見 `design.md` §4）。
- 建議案不改 `env` 鍵集、不 bump `env_schema`；不改 resolve／list／get／冷卻介面。
- 不改 `openspec/specs/**`；不動既有訂閱 priority（zen 10 → go 15 → openrouter 20 → groq 25 → **cloudflare-ai 28** → ollama 30 → claude-personal 40）。
- 本 change 不部署、不需要任何 key 值；key 未到前不部署。

## Impact

- 政策檔多一筆；`opencode` 的候選多一把；`claude-code` 的候選不變。
- key 檔多兩個鍵名（`CLOUDFLARE_API_KEY`、`CLOUDFLARE_ACCOUNT_ID`）；`ops-scripts/key-sync` 用同一份 `keys.py` 驗證，鍵名白名單要同步。
- sidecar（MyLinuxPool）要能用 `meta.endpoint` 組出 Cloudflare 的 account id（見 `design.md` §1、§2）——跨 repo 依存，是判斷點。
- 這把可碰真資料的前提是「只送一次性推論、不接 R2／KV／AI Gateway logging」；Gateway 管不到 sidecar 之外的設定，這點寫在 `design.md` §6。
