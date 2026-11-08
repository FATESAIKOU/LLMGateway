# 模型輪換表

## 與訂閱分配的分工

- 這張表只管「哪個模型好」：能力、比例、額度標記與排序。它**不自動改**政策，也不決定哪個角色能用哪把訂閱。
- 角色×訂閱分配管「哪個角色、什麼資料等級可以用哪把訂閱、順序」：Gateway 政策檔的 `privacy`（每把訂閱的資料等級）與 `roles`（`floor`、`order`），由 `GET /v1/roles/{role}?agent=[&data=]` 讀出（`lgwctl usage --role …` 是它的呈現）。
- 兩者互不覆蓋：輪換表不列角色，分配表不列模型；改一張不等於改另一張。
- 挑模型時先由 `roles` 取候選訂閱（依 `rank`，資料等級不夠的已被 Gateway 篩掉、不會降級），再在這張表裡挑該把訂閱可用的模型。
- 分配表的規則與設計見 `openspec/changes/role-subscription-allocation/`，不在此重抄。

這張表是 PMO／PM 挑模型的參考，**不自動改** `policy/default.json`；政策變更仍走獨立 PR。

由 `/model-rotation-survey` 每週手動更新（PMO 週例執行，有變化才開 PR，本人 comment 後再套用）。涵蓋五家：OpenRouter／Zen 免費、Ollama Cloud（付費）、Claude（訂閱，只能用在原版 Claude Code）、ChatGPT／codex（訂閱，provider=openai，只用原版 Codex CLI）。

## 怎麼讀

- 「主觀」是 AI 判斷，不是實測。
- 每格標籤：
  - `官方數字`：來自官方或榜單，附 URL 與讀取日期。
  - `推估`：依資料推算，不是官方數字。
  - `共用`：額度是整個帳號共用，無法分到單一模型。
  - `查不到`：沒有公開資料；不填猜測值。
  - `主觀`：AI 判斷，不是實測。
- 另有兩個固定值：`待確認`（OpenRouter 的 `:free` 與 pricing 兩者不一致，無法判斷是否免費）、`依訂閱`（Ollama Cloud、Claude、ChatGPT／codex 的額度隨訂閱方案而變，不寫額度）。
- Ollama Cloud 的「計入週用量節流」現行方案一律 `否（每月用量額度，官方數字）`：現行方案以每月用量額度計（Pro／Max／Team 每月含固定美元額度，Pay as you go 按用量付費），另有並行請求上限（Pay as you go／Pro 3、Max／Team 10），官方說切換到新方案後「the session and weekly limits of the old plans no longer apply」（https://ollama.com/pricing，2026-11-07）；官方未逐字寫「新方案無週上限」，「否」是由上述原文推得。帳號若仍是舊方案（以 `/api/balance` 實際回應為準），`GET /api/balance` 的 `included` 仍會回 `session`／`weekly`（https://docs.ollama.com/api/balance，2026-11-07）。
- 能力欄尺度 0–100：terminal＝AA Terminal-Bench、coding＝AA Coding Index、抽象架構設計＝AA Intelligence Index（榜上沒有才標「主觀」）。
- 比例欄不設基準，只標來源。
- 排序：以「綜合」由高到低；綜合只看 Intelligence Index，榜上沒有的標「主觀」並照該值排序。

## 表

標「主觀」的是 AI 判斷，非實測。terminal、coding 兩欄：AA 的 Terminal-Bench／Coding Index 公開頁面本輪抓不到（對應 evaluations 頁 404 或只列 3 筆），一律「查不到」。「官方數字」的 Intelligence Index 取自 AA 榜單（頁面被截斷，榜上未見的標「主觀」）。排除：OpenRouter 的 `nvidia/nemotron-3.5-content-safety:free`（安全分類，非聊天）、`google/lyria-*`（音訊；pricing 皆 0 但無 `:free`，不一致）、`openrouter/free`（路由，非單一模型）。Zen 的 `jev-1.13`（輸入收費）不列；Zen 價格表列 Free 但 id 未出現在 `/inference/v1/models` 的 `nemotron-3-ultra-free`、`jev-1.13-free`、`pulse-alpha-free` 不列。

| 模型名 | 比例 | terminal | coding | 抽象架構設計 | 綜合 | 提供商 | 可用的 agent | 計入週用量節流 | 來源與讀取日期 |
|---|---|---|---|---|---|---|---|---|---|
| `claude-opus-5-5` | 依訂閱 | 查不到 | 查不到 | 59（官方數字） | 59（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-11-07 |
| `claude-sonnet-5-5` | 依訂閱 | 查不到 | 查不到 | 55（官方數字） | 55（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-11-07 |
| `gpt-6.1-sol` | 依訂閱 | 查不到 | 查不到 | 55（官方數字，max） | 55（官方數字，max） | ChatGPT／openai | codex | 是（週，推估） | 模型／提供商 https://learn.chatgpt.com/docs/models；比例／節流 https://learn.chatgpt.com/docs/pricing；terminal https://artificialanalysis.ai/evaluations/terminal-bench、coding https://artificialanalysis.ai/evaluations/coding-index（讀取失敗）；抽象／綜合 https://artificialanalysis.ai/leaderboards/models；agent https://github.com/FATESAIKOU/LLMGateway/blob/main/policy/default.json（以 policy/default.json 的 agents 為準；全格讀取日期 2026-11-08） |
| `claude-fable-5-1` | 依訂閱 | 查不到 | 查不到 | 50（官方數字） | 50（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-11-07 |
| `kimi-k3.1-pro` | 依訂閱 | 查不到 | 查不到 | 46（官方數字） | 46（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `kimi-k3` | 依訂閱 | 查不到 | 查不到 | 42（主觀） | 42（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `claude-haiku-5-5` | 依訂閱 | 查不到 | 查不到 | 36（官方數字） | 36（官方數字） | Claude | claude-code | 是（週） | 清單 https://platform.claude.com/docs/en/models/overview；分數 https://artificialanalysis.ai/leaderboards/models（取該模型最高 effort 檔）；2026-11-07 |
| `kimi-k2.7-code` | 依訂閱 | 查不到 | 查不到 | 35（主觀） | 35（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `deepseek-v4.1-flash-free` | 查不到 | 查不到 | 查不到 | 33（官方數字） | 33（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `deepseek-v4.1-flash` | 依訂閱 | 查不到 | 查不到 | 33（官方數字） | 33（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `deepseek-v4.1-flash:1017` | 依訂閱 | 查不到 | 查不到 | 33（官方數字） | 33（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `glm-5.3` | 依訂閱 | 查不到 | 查不到 | 32（官方數字） | 32（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `kimi-k2.6` | 依訂閱 | 查不到 | 查不到 | 30（主觀） | 30（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `mimo-v2.6-pro` | 依訂閱 | 查不到 | 查不到 | 30（官方數字） | 30（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `minimax-m3.1-flash` | 依訂閱 | 查不到 | 查不到 | 29（官方數字） | 29（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `big-pickle` | 查不到 | 查不到 | 查不到 | 28（主觀） | 28（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `glm-5.3-flash-free` | 查不到 | 查不到 | 查不到 | 28（官方數字） | 28（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `glm-5.3-flash` | 依訂閱 | 查不到 | 查不到 | 28（官方數字） | 28（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `gpt-6-luna` | 依訂閱 | 查不到 | 查不到 | 28（官方數字，max） | 28（官方數字，max） | ChatGPT／openai | codex | 是（週，推估） | 模型／提供商 https://learn.chatgpt.com/docs/models；比例／節流 https://learn.chatgpt.com/docs/pricing；terminal https://artificialanalysis.ai/evaluations/terminal-bench、coding https://artificialanalysis.ai/evaluations/coding-index（讀取失敗）；抽象／綜合 https://artificialanalysis.ai/leaderboards/models；agent https://github.com/FATESAIKOU/LLMGateway/blob/main/policy/default.json（以 policy/default.json 的 agents 為準；全格讀取日期 2026-11-08） |
| `mimo-v2.6-flash-free` | 查不到 | 查不到 | 查不到 | 27（官方數字） | 27（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `mimo-v2.6-flash` | 依訂閱 | 查不到 | 查不到 | 27（官方數字） | 27（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `mistral-large-4` | 依訂閱 | 查不到 | 查不到 | 26（官方數字） | 26（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `glm-5.2` | 依訂閱 | 查不到 | 查不到 | 25（主觀） | 25（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `gpt-oss:120b` | 依訂閱 | 查不到 | 查不到 | 25（主觀） | 25（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `muse-spark-open` | 依訂閱 | 查不到 | 查不到 | 25（官方數字） | 25（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `deepseek-v4-pro:0813` | 依訂閱 | 查不到 | 查不到 | 23（官方數字） | 23（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `minimax-m2.7` | 依訂閱 | 查不到 | 查不到 | 22（主觀） | 22（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `mistral-large-3:675b` | 依訂閱 | 查不到 | 查不到 | 20（主觀） | 20（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `minimax-m3` | 依訂閱 | 查不到 | 查不到 | 19（官方數字） | 19（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `beam-free` | 查不到 | 查不到 | 查不到 | 18（官方數字） | 18（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `beam` | 依訂閱 | 查不到 | 查不到 | 18（官方數字） | 18（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `clef:27b` | 依訂閱 | 查不到 | 查不到 | 15（主觀） | 15（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `gpt-oss:20b` | 依訂閱 | 查不到 | 查不到 | 15（主觀） | 15（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `nvidia/nemotron-3.5-super-vl:free` | 共用 | 查不到 | 查不到 | 14（官方數字） | 14（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `nemotron-3.5-super-vl-free` | 查不到 | 查不到 | 查不到 | 14（官方數字） | 14（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `nemotron-3-super` | 依訂閱 | 查不到 | 查不到 | 14（主觀） | 14（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 共用 | 查不到 | 查不到 | 12（官方數字） | 12（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `recursive/flint-v1.0:free` | 共用 | 查不到 | 查不到 | 12（主觀） | 12（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `flint-40b-free` | 查不到 | 查不到 | 查不到 | 12（主觀） | 12（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `nemotron-3-ultra` | 依訂閱 | 查不到 | 查不到 | 12（官方數字） | 12（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `xingchen-agi/xing4.0-29b-a4b:free` | 共用 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `google/gemma-4-26b-a4b-it:free` | 共用 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `iceman-free` | 查不到 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `crouton-alpha-free` | 查不到 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `stealth/crouton-alpha` | 待確認 | 查不到 | 查不到 | 10（主觀） | 10（主觀） | OpenRouter | opencode | 否（待確認） | 清單 https://openrouter.ai/api/v1/models；pricing 皆 0 但 id 無 `:free`，兩者不一致；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `dots-studio/dots-3-note-preview:free` | 共用 | 查不到 | 查不到 | 8（主觀） | 8（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 共用 | 查不到 | 查不到 | 8（主觀） | 8（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `nemotron-3-nano:30b` | 依訂閱 | 查不到 | 查不到 | 8（主觀） | 8（主觀） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 榜單前段未見，AI 判斷；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `nvidia/nemotron-3.5-lightning:free` | 共用 | 查不到 | 查不到 | 6（官方數字） | 6（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `nemotron-3.5-lightning-free` | 查不到 | 查不到 | 查不到 | 6（官方數字） | 6（官方數字） | Zen | opencode | 否 | 價格表 https://opencode.ai/v2/docs/console/models/（/docs/zen/ 已 302 轉到此）；id 核對 https://opencode.ai/inference/v1/models；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `google/gemma-4-31b-it:free` | 共用 | 查不到 | 查不到 | 4（官方數字） | 4（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `gemma4:31b` | 依訂閱 | 查不到 | 查不到 | 4（官方數字） | 4（官方數字） | Ollama Cloud | claude-code, opencode | 否（每月用量額度，官方數字） | 清單 https://ollama.com/api/tags；分數 https://artificialanalysis.ai/leaderboards/models；節流 https://ollama.com/pricing、https://docs.ollama.com/api/balance；2026-11-07 |
| `cohere/north-mini-code:free` | 共用 | 查不到 | 查不到 | 3（官方數字） | 3（官方數字） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 https://artificialanalysis.ai/leaderboards/models；2026-11-07 |
| `liquid/lfm-2.5-2.6b:free` | 共用 | 查不到 | 查不到 | 3（主觀） | 3（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |
| `liquid/lfm-3-1.7b:free` | 共用 | 查不到 | 查不到 | 3（主觀） | 3（主觀） | OpenRouter | opencode | 否 | 清單 https://openrouter.ai/api/v1/models；比例 https://openrouter.ai/docs/api-reference/limits；分數 榜單前段未見，AI 判斷；2026-11-07 |

## ChatGPT／codex 公開調查（2026-11-08）

本輪僅補查第五家，其他四家保留各列原讀取日期。worker 主模型為 `gpt-6.1-sol`、次模型為 `gpt-6-luna`（主觀：本單輪換建議，非 AA 名次）。官方亦列 `gpt-6.1-terra`、`gpt-6-astra`、`gpt-6-sol` 為公開候選；能否在特定登入使用仍待驗。`gpt-5.6-luna` 的訂閱可用性查不到。來源：https://learn.chatgpt.com/docs/models、https://learn.chatgpt.com/docs/pricing，讀取日期 2026-11-08。

`gpt-5.5` 已於 2026-10-14 從 ChatGPT 登入的 Codex 退役（官方數字），API key 使用不受此項退役影響；不列為訂閱可用模型。來源：https://learn.chatgpt.com/docs/models#gpt-55-retirement，讀取日期 2026-11-08。

額度／冷卻：公開文件有 5 小時窗口且部分方案例外，可能另有每週上限；本機與雲端用量共用（共用），不可推定不同模型共用單一桶。比例不估 token 數。「是（週，推估）」是保守的週節流分類；U0 目前回 `not_applicable`，並不表示已取得週用量或沒有上限。重置時刻與實際冷卻長度查不到，不由公開窗口長度推算。來源：https://learn.chatgpt.com/docs/pricing，讀取日期 2026-11-08；U0 契約見 [design Q7／Q8](../openspec/changes/codex-subscription/design.md)。

## chatgpt-personal allow 清單的實測狀態

以下為既有實測交接狀態，非本輪登入測試；allow 表示政策允許，不保證上游接受。本單不改政策。

| 模型 | 狀態 | 依據 |
|---|---|---|
| `gpt-6.1-sol` | 已實測 | MLP#797 B 段（依工作簡報交接，非本輪重驗） |
| `gpt-6-luna` | 已實測 | MLP#797 B 段（依工作簡報交接，非本輪重驗） |
| `gpt-6.1-terra` | 待驗 | policy allow；官方候選不等同實測 |
| `gpt-6-astra` | 待驗 | policy allow；官方候選不等同實測 |
| `gpt-6-sol` | 待驗 | policy allow；官方候選不等同實測 |
| `gpt-5.5` | 待驗 | allow 仍列出，但官方已退役；不建議再登入試此 id |
| `gpt-5.6-luna` | 待驗 | allow 仍列出，公開訂閱可用性查不到 |

清單與 agent 依據：[policy/default.json](../policy/default.json) 的 `chatgpt-personal.models.allow` 與 `agents=["codex"]`（2026-11-08 讀取）；退役及公開可用性來源同上官方 models 頁（2026-11-08）。

## 比較方法

目的：在試用期滿（約 2026-12-07）前比較「Codex 能否取代 Ollama Cloud」。建議每次執行在該工單的工作報告記錄 `provider`、`agent`、完整 `model`、實際 `effort`（未指定時記「預設（未指定）」；指定後仍無法確認才填「查不到」，不從模型名猜）、客戶端版本、任務類型、起訖時間、測試／驗收結果、人工修正次數、重試次數、額度中斷與恢復等待時間。敏感的用量記錄只存私有工作紀錄，公開報告僅放去識別化結果。effort 由誰決定另有 MLP 工單，本單不定，通用的 effort 介面待 tui／herdr 與 agy／codex 都上線後統一做；在那之前一律記「預設（未指定）」；Gateway 不把 effort 編入 model id。

建議挑相同基準版本、相同輸入、相同工具及驗收條件的實作與 QA 任務，Codex 與 Ollama Cloud 配對比較，分開統計不同 model＋effort；交錯執行順序、涵蓋多種難度並重複試跑，避免一次成功或最高 effort 榜分代表日常表現。先約定通過率、人工修正時間與交付時間的接受門檻，再記錄樣本數、失敗原因及額度中斷；證據不足就延後取代結論。AA 最高 effort 分數只作選模參考，不代替此驗收。
