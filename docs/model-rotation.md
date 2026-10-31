# 模型輪換表

## 與訂閱分配的分工

- 這張表只管「哪個模型好」：能力、比例、額度標記與排序。它**不自動改**政策，也不決定哪個角色能用哪把訂閱。
- 角色×訂閱分配管「哪個角色、什麼資料等級可以用哪把訂閱、順序」：Gateway 政策檔的 `privacy`（每把訂閱的資料等級）與 `roles`（`floor`、`order`），由 `GET /v1/roles/{role}?agent=[&data=]` 讀出（`lgwctl usage --role …` 是它的呈現）。
- 兩者互不覆蓋：輪換表不列角色，分配表不列模型；改一張不等於改另一張。
- 挑模型時先由 `roles` 取候選訂閱（依 `rank`，資料等級不夠的已被 Gateway 篩掉、不會降級），再在這張表裡挑該把訂閱可用的模型。
- 分配表的規則與設計見 `openspec/changes/role-subscription-allocation/`，不在此重抄。

這張表是 PMO／PM 挑模型的參考，**不自動改** `policy/default.json`；政策變更仍走獨立 PR。

由 `/model-rotation-survey` 每週手動更新（PMO 週例執行，有變化才開 PR，本人 comment 後再套用）。涵蓋四家：OpenRouter／Zen 免費、Ollama Cloud（付費）、Claude（訂閱，只能用在原版 Claude Code）。

## 怎麼讀

- 「主觀」是 AI 判斷，不是實測。
- 每格標籤：
  - `官方數字`：來自官方或榜單，附 URL 與讀取日期。
  - `推估`：依資料推算，不是官方數字。
  - `共用`：額度是整個帳號共用，無法分到單一模型。
  - `查不到`：沒有公開資料；不填猜測值。
  - `主觀`：AI 判斷，不是實測。
- 另有兩個固定值：`待確認`（OpenRouter 的 `:free` 與 pricing 兩者不一致，無法判斷是否免費）、`依訂閱`（Ollama Cloud、Claude 的額度隨訂閱方案而變，不寫額度）。
- 能力欄尺度 0–100：terminal＝AA Terminal-Bench、coding＝AA Coding Index、抽象架構設計＝AA Intelligence Index（榜上沒有才標「主觀」）。
- 比例欄不設基準，只標來源。
- 排序：以「綜合」由高到低；綜合只看 Intelligence Index，榜上沒有的標「主觀」並照該值排序。

## 表

標「主觀」的是 AI 判斷，非實測。terminal、coding 兩欄：AA 的 Terminal-Bench／Coding Index 公開頁面本輪抓不到（對應 evaluations 頁 404 或只列 3 筆），一律「查不到」。「官方數字」的 Intelligence Index 取自 AA 榜單（頁面被截斷，榜上未見的標「主觀」）。排除：OpenRouter 的 `nvidia/nemotron-3.5-content-safety:free`（安全分類，非聊天）、`google/lyria-*`（音訊；pricing 皆 0 但無 `:free`，不一致）、`openrouter/free`（路由，非單一模型）。Zen 的 `jev-1.13`（輸入收費）不列。

| 模型名 | 比例 | terminal | coding | 抽象架構設計 | 綜合 | 提供商 | 可用的 agent | 計入週用量節流 | 來源與讀取日期 |
|---|---|---|---|---|---|---|---|---|---|
| `claude-opus-5-5` | 依訂閱 | 查不到 | 查不到 | 58（官方數字） | 58（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-10-31 |
| `claude-sonnet-5-5` | 依訂閱 | 查不到 | 查不到 | 54（官方數字） | 54（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-10-31 |
| `claude-fable-5-1` | 依訂閱 | 查不到 | 查不到 | 50（官方數字） | 50（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-10-31 |
| `kimi-k3` | 依訂閱 | 查不到 | 查不到 | 42（主觀） | 42（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `claude-haiku-5-5` | 依訂閱 | 查不到 | 查不到 | 36（官方數字） | 36（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-10-31 |
| `kimi-k2.7-code` | 依訂閱 | 查不到 | 查不到 | 35（主觀） | 35（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `deepseek-v4.1-flash-free` | 查不到 | 查不到 | 查不到 | 32（官方數字） | 32（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `glm-5.3` | 依訂閱 | 查不到 | 查不到 | 32（官方數字） | 32（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `deepseek-v4.1-flash` | 依訂閱 | 查不到 | 查不到 | 32（官方數字） | 32（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `deepseek-v4.1-flash:1017` | 依訂閱 | 查不到 | 查不到 | 32（官方數字） | 32（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `kimi-k2.6` | 依訂閱 | 查不到 | 查不到 | 30（主觀） | 30（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `mimo-v2.6-pro` | 依訂閱 | 查不到 | 查不到 | 30（官方數字） | 30（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `minimax-m3.1-flash` | 依訂閱 | 查不到 | 查不到 | 29（官方數字） | 29（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `big-pickle` | 查不到 | 查不到 | 查不到 | 28（主觀） | 28（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `glm-5.3-flash-free` | 查不到 | 查不到 | 查不到 | 28（官方數字） | 28（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `glm-5.3-flash` | 依訂閱 | 查不到 | 查不到 | 28（官方數字） | 28（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `mimo-v2.6-flash-free` | 查不到 | 查不到 | 查不到 | 27（官方數字） | 27（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `mimo-v2.6-flash` | 依訂閱 | 查不到 | 查不到 | 27（官方數字） | 27（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `mistral-large-4` | 依訂閱 | 查不到 | 查不到 | 26（官方數字） | 26（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `glm-5.2` | 依訂閱 | 查不到 | 查不到 | 25（主觀） | 25（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `gpt-oss:120b` | 依訂閱 | 查不到 | 查不到 | 25（主觀） | 25（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `muse-spark-open` | 依訂閱 | 查不到 | 查不到 | 25（官方數字） | 25（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `deepseek-v4-pro:0813` | 依訂閱 | 查不到 | 查不到 | 23（官方數字） | 23（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `minimax-m2.7` | 依訂閱 | 查不到 | 查不到 | 22（主觀） | 22（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `mistral-large-3:675b` | 依訂閱 | 查不到 | 查不到 | 20（主觀） | 20（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `minimax-m3` | 依訂閱 | 查不到 | 查不到 | 19（官方數字） | 19（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `beam-free` | 查不到 | 查不到 | 查不到 | 18（官方數字） | 18（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `beam` | 依訂閱 | 查不到 | 查不到 | 18（官方數字） | 18（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `poolside/laguna-s-2.1:free` | 共用 | 查不到 | 查不到 | 15（主觀） | 15（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `clef:27b` | 依訂閱 | 查不到 | 查不到 | 15（主觀） | 15（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `gpt-oss:20b` | 依訂閱 | 查不到 | 查不到 | 15（主觀） | 15（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `nvidia/nemotron-3.5-super-vl:free` | 共用 | 查不到 | 查不到 | 14（官方數字） | 14（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `google/gemma-4-31b-it:free` | 共用 | 查不到 | 查不到 | 14（主觀） | 14（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `nemotron-3.5-super-vl-free` | 查不到 | 查不到 | 查不到 | 14（官方數字） | 14（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `nemotron-3-super` | 依訂閱 | 查不到 | 查不到 | 14（主觀） | 14（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `gemma4:31b` | 依訂閱 | 查不到 | 查不到 | 14（主觀） | 14（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `thinkingmachines/inkling-small:free` | 共用 | 查不到 | 查不到 | 13（官方數字） | 13（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 共用 | 查不到 | 查不到 | 12（官方數字） | 12（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `thinkingmachines/inkling:free` | 共用 | 查不到 | 查不到 | 12（官方數字） | 12（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `recursive/flint-v1.0:free` | 共用 | 查不到 | 查不到 | 12（主觀） | 12（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `flint-40b-free` | 查不到 | 查不到 | 查不到 | 12（主觀） | 12（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `nemotron-3-ultra-free` | 查不到 | 查不到 | 查不到 | 12（官方數字） | 12（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；id 未出現在 /inference/v1/models，待核對；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `nemotron-3-ultra` | 依訂閱 | 查不到 | 查不到 | 12（官方數字） | 12（官方數字） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `xingchen-agi/xing4.0-29b-a4b:free` | 共用 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `poolside/laguna-xs-2.1:free` | 共用 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `cohere/north-mini-code:free` | 共用 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `google/gemma-4-26b-a4b-it:free` | 共用 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `jev-1.13-free` | 查不到 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；id 未出現在 /inference/v1/models，待核對；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `dots-studio/dots-3-note-preview:free` | 共用 | 查不到 | 查不到 | 8（主觀） | 8（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 共用 | 查不到 | 查不到 | 8（主觀） | 8（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
| `nemotron-3-nano:30b` | 依訂閱 | 查不到 | 查不到 | 8（主觀） | 8（主觀） | Ollama Cloud | claude-code, opencode | 是（週，另有 5 小時 session 上限） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流：本人帳號設定頁 2026-10-25；2026-10-31 |
| `nvidia/nemotron-3.5-lightning:free` | 共用 | 查不到 | 查不到 | 6（官方數字） | 6（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `nemotron-3.5-lightning-free` | 查不到 | 查不到 | 查不到 | 6（官方數字） | 6（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-10-31 |
| `liquid/lfm-2.5-2.6b:free` | 共用 | 查不到 | 查不到 | 3（主觀） | 3（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-10-31 |
