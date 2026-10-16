# 審查報告：fix/policy-agents-allow（#23＋#26）

審查模型：Claude Sonnet 5.5（公司帳號）。唯讀審查，未改檔、未 commit。
範圍：main..HEAD 的 diff，加上對 lgwd/policy.py、policy/default.json、tests/test_policy.py 的確認。

## 正確性

- **（資訊）** fnmatch 與既有做法一致。lgwd 裡目前只有 `lgwd/policy.py:86` 一處用 `fnmatch.fnmatchcase` 比對 allow。`registry.py:135` 只是原樣回傳 allow，沒有別處用不同方式比對。`models_allow` 的註解（`policy.py:38`）已寫明是 fnmatch 樣式。測試用的 `_covered` 與 lgwd 的檢查是同一種比對。
- **（資訊）** 出廠的五筆都能載入：
  - claude-personal、claude-company：default 為 null，不檢查。
  - opencode-zen：`muse-spark-1.3-contributor-free` 符合 `*-free`。
  - openrouter：`openrouter/free` 是 allow 裡的字面值，也符合 `_OPENROUTER_ALLOW`。
  - ollama-cloud：allow 是 `*`。
  - PM 回報的 266 個測試全過，其中有 `test_default_policy_loads`。
- **（資訊）** 例外訊息沒有回顯檔案裡的值。`policy.py:98-100` 只放 `name`。`load_policy` 在呼叫 `_parse_subscription` 之前，已經檢查過 `name` 必須在 `SUBSCRIPTIONS` 裡（`policy.py:124-126`），所以 `name` 一定是固定的訂閱 id，default 和 allow 的內容都不會進訊息。模組 docstring 已同步改成「固定字串與固定的訂閱 id」。
- **（資訊）** 檢查放在 default 的型別驗證之後、allow 的型別驗證之後，所以 `fnmatchcase` 拿到的一定是 str。這個順序是對的。
- **（資訊）** `fnmatchcase` 對 `[`、`?` 這類樣式的行為，和 allow 樣式的定義（fnmatch）一致，沒有新增風險。

## 測試涵蓋

- **（低）** `tests/test_policy.py:220-231` 的 `test_default必須被allow涵蓋` 只驗訊息含訂閱 id，沒驗「訊息不含 default 的值」。這是本次要求的重點之一（例外不回顯檔案裡的值）。建議補一行 `assertNotIn("muse-spark-1.3", str(ctx.exception))`。目前實作是對的，這只是防回歸。
- **（低）** 沒有「allow 裡有多個樣式、只有後面的樣式涵蓋 default」的案例，也沒有「default 恰好等於 allow 的字面值」的案例。`any()` 的邏輯很單純，風險小，可以不補。出廠的 openrouter 其實已間接涵蓋字面值這種情況。
- **（資訊）** #23 有兩層測試：`tests/test_registry.py` 的 `test_ClaudeCode不能拿openrouter` 和 `tests/test_main.py` 的 `test_ClaudeCode拿不到openrouter`（HTTP 層）。兩者互相重複但不衝突。出廠政策的 `agents` 斷言也已從 `assertIn` 改成 `assertEqual(("opencode",))`，符合 B 案。
- **（資訊）** `test_zen_allow涵蓋default與免費模型` 涵蓋了 `*-free`、`big-pickle`，也涵蓋了付費 id 不被涵蓋的反例，足夠。
- **（資訊）** `test_default是null時不檢查涵蓋` 涵蓋了 null 的豁免。

## 文件和出廠值是否一致

- **（資訊）** design.md、spec.md、tasks.md、deploy.md 5.5 以及 `policy/default.json` 對 openrouter 只給 opencode、zen 的 allow 是 `["*-free", "big-pickle"]` 的說法一致。deploy.md 4.2 也補了 default 必須被 allow 涵蓋。
- **（資訊）** deploy.md 補的兩條待辦都在：`sudo -u llmgw -i` 要先匯出 `XDG_RUNTIME_DIR` 與 `DBUS_SESSION_BUS_ADDRESS`（4.4）；跳板機 sshd 的 unit 名稱不固定，查 log 要用全量 `journalctl`（5.2）。
- **（資訊）** `docs/research/` 底下的舊研究文件（`v0-design-review.md`、`upstream-claude-code-compat.md`）仍寫 OpenRouter 可接 Claude Code。它們是當時的研究紀錄，不是現行規格，不用改。若想避免日後誤讀，可以在檔頭加一行「已被 #23 裁定取代」，但不阻擋。
- **（資訊）** spec.md 新增的 Scenario 範例（`muse-spark-1.3` 對 `*-free`、`big-pickle`）與測試和 `policy/default.json` 的內容相符。

## public repo 規矩

- **（資訊）** diff 的新增行裡沒有機器名、IP、key 的值。範例只用 `llmgw` 這種角色帳號、`<跳板機>` 這種佔位，與既有文件寫法一致。測試用的 key 是既有的 CANARY 假值，沒有新增。

## 發現總表

| 嚴重度 | 檔案:行 | 內容 |
|---|---|---|
| 阻擋 | 無 | |
| 低 | tests/test_policy.py:220-231 | 補「訊息不含 default 值」的斷言 |
| 低 | tests/test_policy.py（新增測試區） | 可選：補多樣式、字面值的涵蓋案例 |
| 資訊 | docs/research/* | 舊研究文件仍寫 OpenRouter 可接 Claude Code，屬歷史紀錄 |

結論：可以開 PR

處理：低-1 已補（5efb8e2）；低-2 不做（any() 邏輯單純，出廠 openrouter 已間接涵蓋字面值）；資訊-docs/research 不改（歷史紀錄）。
