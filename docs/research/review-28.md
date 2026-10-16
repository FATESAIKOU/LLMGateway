# 審查 #28：endpoint 的語意（llmgateway-arch）

**審查模型：DeepSeek V4.1 Flash（與作者同模型、不同 session；Zen 限流，本人允許）**

審查對象：`git diff main...HEAD`（branch `docs/endpoint-semantics`＝工單 #28，3 個 commit；main `5c54252`）。

- `830cd1a` spec：加 endpoint 的語意 Requirement 與三個 Scenario（#28）
- `0ce8b24` design：決定 7 補 endpoint 的根位址原則（與 models.default 並列）（#28）
- `788f2c2` test：出廠 endpoint 值（list 與 get）（#28）

審查者：llmgateway-arch（PM 是 llmgateway-pm；作者是另一個 session 的 DeepSeek V4.1 Flash）。只讀，未改程式；未讀任何憑證檔或密碼檔；未 ssh／gh／push；未 git switch。本報告是唯一新增的檔案；實驗輸出在系統暫存目錄、不進 repo。未遇 rate limit。

## 結論

**可以開 PR。** 阻擋 0、低 1、資訊 2；低與資訊皆不擋（低-1 建議順手修，一句話）。

改動面極小且與前提一致：只動 spec／design／測試、不碰 `lgwd/`（值與介面不變）；spec 與 design 的新說法彼此一致、和 `lgwd/providers.py` 的出廠值一致；格式與同檔其他 Requirement 相同；`openspec validate --strict` 過；新的 `test_endpoint出廠值` 同時走 list 與 get，且只斷言 `endpoint`、不會把 key 印出來；全量 267 tests OK。

## 測試

`python3 -m unittest discover -s tests -t .`（exit 0；141.4s）

```
Ran 267 tests in 141.390s

OK
```

`openspec validate gateway-v0 --strict`：

```
Change 'gateway-v0' is valid
exit=0
```

## 逐項核對（4 點）

### 1. spec／design 的出廠值與形狀 vs `lgwd/providers.py`；描述正確性

值逐筆一致（spec `:59-60`、design `:173`、示意 JSON `:161` ↔ `lgwd/providers.py:28,34,40,46`）：

| subscription | providers.py | spec／design | 協定相容性描述 |
|---|---|---|---|
| claude-personal／company | `None`（`:28`） | null（「由 agent 直連」） | —（MUST null，一致） |
| openrouter | `https://openrouter.ai/api`（`:34`） | 同值 | Anthropic 相容 ✓（見下） |
| ollama-cloud | `https://ollama.com`（`:40`） | 同值 | Anthropic 相容 ✓（見下） |
| opencode-zen | `https://opencode.ai/zen/v1`（`:46`） | 同值 | OpenAI 相容（現行性見資訊-2） |

「openrouter、ollama-cloud 的值是 Anthropic 相容的 base；opencode-zen 是 OpenAI 相容的 base」這段描述**正確（值本身的性質）**，出處：

- openrouter：官方 Claude Code 整合頁明文 `ANTHROPIC_BASE_URL="https://openrouter.ai/api"`（不含 `/v1`；Claude Code 自己接 `/v1/messages`），OpenAI 相容 base 是 `https://openrouter.ai/api/v1`（models.dev 的 openrouter provider `api`；repo 亦有 `docs/research/review-notes-v0.md:12`、`upstream-claude-code-compat.md:11`）。spec 說「OpenAI 相容的路徑要由呼叫方自己組（例如再接 `/v1`）」與值一致。
- ollama-cloud：官方 `ANTHROPIC_BASE_URL=https://ollama.com`（Ollama Cloud，bearer 認證；repo：`upstream-claude-code-compat.md` §2、`review-notes-v0.md:12`），OpenAI 相容 base 是 `https://ollama.com/v1`（models.dev 的 ollama-cloud provider `api`）。「再接 `/v1`」一致。
- opencode-zen：repo 研究（`docs/research/agent-endpoints.md:139`、`upstream-claude-code-compat.md` §3.1）說 Zen 有 OpenAI 相容端點、值 `https://opencode.ai/zen/v1`；現行官方文件與 models.dev 指向 `/inference/*`，值得日後驗一次（資訊-2，非本單）。

「寫太死」的部分見資訊-1：Requirement 的清單把三個 URL 與協議標籤釘進 spec 本體；規範核心（根位址、MUST NOT 假設完整路徑、claude-subscription MUST null）與 provider 無關、不用跟改。

### 2. Requirement／Scenario 格式，與同檔其他 Requirement 一致；validate ✅

- 格式與同檔一致：一段規範句＋「出廠值與形狀」清單（同「共通 JSON schema」的子彈清單寫法）＋一段 MUST NOT＋`#### Scenario:` 區塊；每條 Scenario 都有 WHEN／THEN；段落留白、bullet 標點（`；`／`。`）與其他 Requirement 相同。
- 新 Requirement 放在 ADDED（和 `models.default` 的兩條並列），位置合理；`openspec validate gateway-v0 --strict` 通過。
- 三個 Scenario 都清楚；其中兩個（opencode driver、Claude Code 拿到 ollama-cloud 的 endpoint）是**呼叫方（agent driver）的行為**、與本 repo 的 `models.default` 條文同一種寫法（本 repo 測不到）；第三個（Claude 訂閱沒有 endpoint）是服務側、被新測試與既有的 `test_Claude訂閱的描述` 覆蓋。

### 3. 新測試真的走 list 與 get；不印 key ✅

- `tests/test_registry.py:118-141`（`test_endpoint出廠值`）：五筆先驗 list（`:135-139`），再逐筆驗 get（`:141`）；get 用的 agent 都合法（openrouter／ollama-cloud／opencode-zen→`opencode`，Claude 兩筆→`claude-code`），不會誤觸 403。
- key 不外洩：get 的回應雖然含 key，測試只取 `["endpoint"]`（`:141`），沒有 `str()`／`print()` 整個 payload；`assertEqual` 失敗時訊息只會有 endpoint 字串，不會有 key。測試全用 canary 假 key（`RegistryCase`）。
- 小重疊（非問題）：list 側的 endpoint 斷言與既有 `test_provider欄位`（`:102-116`）重複；新測試真正的增量是 get 那條路與 Claude 兩筆的 null，重疊讓新測試自成一體，無害。

### 4. public repo 規矩 ✅

新增文字只有 provider 的公開網址（openrouter.ai／ollama.com／opencode.ai）、沒有機器名、IP、key 的值或任何憑證；測試沒有新增字面 key；commit 訊息同樣乾淨。（本報告提到的網址亦只列公開頁面。）

## 發現

### 低-1（不擋；建議本單順手修）openrouter 被寫成可以給 Claude Code 用，和 #23 定案衝突

- **檔案:行號**：`openspec/changes/gateway-v0/specs/subscription-registry/spec.md:59`（配 `design.md:173`）
- **問題**：新 Requirement 把 openrouter 與 ollama-cloud 並列為「Anthropic 相容的 base，可以直接當 Claude Code 的 `ANTHROPIC_BASE_URL`」。但 #23 定案（已併入 main）是 openrouter 只給 opencode、Claude Code 只用 Claude 訂閱與 Ollama；同一份 spec 的 `:89`（openrouter 的 agents MUST 只有 opencode）與 `:95-97`（get openrouter＋claude-code 回 403）就是這樣寫的；`design.md:171` 也特別註「OpenRouter 只給 opencode，見上」。描述「值的性質」正確，但拿 Claude Code 當 openrouter 的用法例子在系統裡不成立，讀者容易把 endpoint 的相容性誤讀成「可以用」。
- **證據**：`design.md:169`（openrouter 只有 opencode……Claude Code 碰到 `openrouter/free` 會 unrecognized_model）、`design.md:171`、`spec.md:89`、`spec.md:95-97`；`policy/default.json` openrouter 的 agents 只有 `["opencode"]`；OpenRouter 官方頁（2026-10-16 讀）確認 `https://openrouter.ai/api` 本身可當 Claude Code 的 base——兩件事不矛盾，是措辭要收斂。
- **建議**：把 openrouter 從那半句挪開，例如「`ollama-cloud` 的 `https://ollama.com` 可以直接當 Claude Code 的 `ANTHROPIC_BASE_URL`；`openrouter` 的 `https://openrouter.ai/api` 同屬 Anthropic 相容的 base（本系統只給 opencode，接法由 opencode driver 決定）；兩者的 OpenAI 相容路徑由呼叫方自己組（例如再接 `/v1`）」。design `:173` 同句一起改。不擋。

### 資訊-1（不擋）spec 把出廠 URL 與協議標籤釘進本體，換 provider 時要跟改的地方

- **檔案:行號**：`spec.md:58-60`（值也在 `design.md:161`、`:173`、`tests/test_registry.py:124-126`）
- **問題**：Requirement 的清單把三個 URL 與「Anthropic／OpenAI 相容」的標籤寫死；日後 provider 換網址、改路徑或換協議，spec／design／測試三處要同步。（規範核心——根位址、MUST NOT 假設完整路徑、claude-subscription MUST null——與 provider 無關，不用跟改。）另外 `spec.md:58` 用「值以 `lgwd/providers.py` 為準」引用實作檔；design 有此先例（`design.md:134`「實作在 `lgwd/errors.py`」），spec 則第一次。
- **證據**：三處 URL 各出現一次（全 repo grep，排除 `docs/research/`）；`lgwd/providers.py:34,40,46` 是真來源。
- **建議**：可留（標題已寫「出廠值與形狀」、並已指明以 `providers.py` 為準）；若想更耐用，把 URL 清單留在 design，spec 只留規則與「形狀」的約束。不擋。

### 資訊-2（不擋）opencode-zen 的端點值現行性值得一次確認

- **檔案:行號**：`spec.md:60`（值本體在 `lgwd/providers.py:46`）
- **問題**：repo 研究寫 Zen 端點是 `https://opencode.ai/zen/v1`；現行官方 Console 文件列的端點是 `https://opencode.ai/inference/*`（OpenAI 相容 `…/inference/openai/v1`），models.dev 的 opencode provider `api` 也是 `https://opencode.ai/inference/openai/v1`。兩個路徑都活著（未帶 key 皆回 401、假路徑回 404），不能斷定舊值失效，但「哪個才是現行 OpenAI 相容 base」值得在 sidecar 整合（`tasks.md` 4.3 的 opencode 對 Zen 跑通）時驗一次。本單沒有改值，只是把既有值寫進 spec。
- **證據**：`curl` 對 `/zen/v1/models`＝401、`/zen/v1/nonexistent-xyz`＝404（2026-10-16）；官方 Console 文件與 models.dev（2026-10-16 讀）。
- **建議**：不改本單；若驗出要換值，另開單（會連動 `lgwd/providers.py`、design 與測試）。不擋。

## 補充觀察（非發現）

- 3 個 commit 都只動 spec／design／測試（`--name-only` 證實未碰 `lgwd/`）；訊息與 Co-Authored-By 齊全，與「不改值、不改介面」的前提一致。
- 未發現漏改：全 repo grep，endpoint 值的出處只有 `lgwd/providers.py`、測試與 design 示意（`:161`）；`tasks.md`／`deploy.md`／`diagrams.md`／`proposal.md` 只提欄位名、沒有值。
- 兩個呼叫方 Scenario 的「不覆寫內建 provider 的 baseURL」寫法，正好對應這次的 404 事故（把 `https://ollama.com` 直接當 OpenAI baseURL）；語意收在 spec，driver 端要怎麼做留給 sidecar，範圍拿捏正確。

可以開 PR

處理：低-1 已修（5569edd）；資訊-1、資訊-2 不改（資訊-2 交給 MyLinuxPool#176 實機驗證時確認）
