DeepSeek V4.1 Flash

# 指定審查：sidecar allow-list base env 與 get／resolve 過渡規則

審查對象：worktree `/Users/fatesaikou/testAI/LLMGateway-worktrees/spec-base-env`（HEAD `8777b1a`）的未提交 diff：`openspec/changes/agent-provider-model-interface/design.md`、`tasks.md`（6 insertions / 6 deletions）。只讀；未改檔、未連遠端、未讀憑證檔。

事實對照：MyLinuxPool `origin/master`（`19cbf99`）的 `modules/sidecar/src/sidecar/subproc.py`（`KEEP_ENV`、`KEEP_PREFIXES`、`child_env`），以及本 repo 的 `openspec/changes/agent-provider-model-interface/specs/subscription-registry/spec.md`、`lgwd/providers.py`、`docs/research/design-33-overview.md`。

驗證：`openspec validate --strict agent-provider-model-interface` 通過（exit 0）；`git diff --check` 乾淨；grep 指定檔案與全 repo 的「完整環境／完整 base env／繼承整個環境」措辭。

**結論：需修（變更很小，但有一條核心誤述）。critical 1、warning 3、nit 3。**

| 嚴重度 | 條數 | 條目 |
|---|---:|---|
| critical | 1 | W1 |
| warning | 3 | W2–W4 |
| nit | 3 | N1–N3 |

## 事實核對（subproc.py）

| 項目 | 文件寫法 | subproc.py | 結果 |
|---|---|---|---|
| `KEEP_ENV` 名單 | PATH、HOME、LANG、TMPDIR、USER、LOGNAME、SHELL、TERM、TZ | 同 9 個，無多無少 | 一致 |
| 前綴 | `AGORA_`／`XDG_`／`LC_`／`FAKE_` | `KEEP_PREFIXES = ("AGORA_", "XDG_", "LC_", "FAKE_")`，用 `startswith` | 一致 |
| base env 的來源 | allow-list，不是整個環境 | `os.environ` 經 `key in KEEP_ENV or key.startswith(KEEP_PREFIXES)` 過濾 | 一致 |
| overlay 順序 | resolve env 覆蓋 base env | `merged.update(env)`，後蓋前 | 一致 |
| 排除例 | `GH_TOKEN`、`SSH_AUTH_SOCK`、`AWS_*`、`ANTHROPIC_*`、`OPENAI_*`、`OPENCODE_*`、`CLAUDE_CODE_OAUTH_TOKEN` | 都不在 `KEEP_ENV`／前綴內（除非 driver 另外疊） | 一致 |
| proxy／CA | `HTTP(S)_PROXY`、`NO_PROXY`、`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE` 不在 base env | 都不在 allow-list | 一致 |

新的第 1 項本體（design.md:86）與 tasks 2.3 的變數名單、前綴是準確的；問題不在這一行，而在同檔其他殘留與過渡規則。

## Critical

### W1　design.md:9 仍把子行程環境寫成「完整環境＋PATH／HOME／Locale」

- **位置**：`design.md:9`（Context，本次 diff 未動）。
- **原文**：「**sidecar 的完整環境**：子行程環境是「完整環境」加上 PATH／HOME／Locale 等，再疊 sidecar 的維運／行為變數……」
- **事實**：`subproc.child_env()` 是 `os.environ` 經 `KEEP_ENV`／`KEEP_PREFIXES` 過濾後再 `merged.update(env)`；base 不是整個環境，也不存在「完整環境再加 PATH／HOME／Locale」這種形狀。
- **為什麼是 critical**：這是本次要淘汰的同一誤述，且與同檔決定 2（L86「不是繼承 sidecar 行程的整個環境」）直接相反。只讀 Context 的人（或把 Context 引給 MyLinuxPool 的人）會得到「繼承整個環境、只擋憑證」的黑名單模型——正是 allow-list 要取代的東西。這不是措辭美觀問題，是文件對現況的事實斷言錯誤。
- **建議**：改成「子行程環境＝allow-list base env（`KEEP_ENV`＋前綴）＋sidecar 維運變數＋resolve env overlay；不是繼承 sidecar 行程的整個環境」，或直接把這條指向決定 2，不重述。

## Warning

### W2　「完整 base env／完整環境」仍殘留 4 處（標題與 PM 會讀的概覽）

- **位置**：
  - `design.md:85` 標題「**sidecar 的完整環境與 overlay（中-4）**」——本節本體已改成 allow-list，標題仍是舊詞。
  - `proposal.md:43`「driver 改為『sidecar 完整環境＋resolve env overlay＋維運變數』」。
  - `docs/research/design-33-overview.md:23`「env＝完整 base env＋維運變數＋resolve env」。
  - `docs/research/design-33-overview.md:64`「完整 base env＋維運變數＋resolve env overlay」。
- **為什麼是問題**：本體改了，但 MyLinuxPool PM 最先讀的概覽與本節標題仍是舊公式；grep「完整 base env」還是會命中，會被讀成「這次沒改乾淨」。
- **建議**：標題改「sidecar 的 allow-list base env 與 overlay」；proposal 改「allow-list base env」；overview 兩張圖同步，或在頁首加「修訂 5：base env 改 allow-list，以 `design.md` 為準」。`docs/research/review-33*.md`、`design-33-fix1.md` 是歷史審查紀錄，不建議改，也不計入。

### W3　過渡期「整包 `OPENCODE_CONFIG_CONTENT` 照舊解析（僅 get）」與同句「get 路徑由 sidecar 用 row 自己組 config」矛盾；v5 get 也不回這個鍵

- **位置**：`design.md:91`、`design.md:173`（決定 6.3）、`tasks.md:23`（2.3）。
- **原文（design.md:91）**：「過渡期：整包 `OPENCODE_CONFIG_CONTENT` 的照舊解析**只適用 get 路徑**（舊 Gateway 沒有 resolve → 404 → 退回 get；**get 路徑由 sidecar 用 row 自己組 config**）。」
- **事實**：v5 get 回的是 subscription row（design Context L5；spec「共通 JSON schema」L195）；`OPENCODE_CONFIG_CONTENT` 的唯一生產者是 sidecar 自己（design L79；`lgwd/providers.py` 只回 `OPENCODE_*` 片段）。沒有任何 Gateway 版本會在 get 回整包 config。
- **為什麼是問題**：同一句給了兩個互斥指令——「照舊解析 Gateway 給的整包 config」與「sidecar 自己用 row 組 config」。sidecar 實作者若照前者保留一條接受 Gateway config 的路徑，會越過 `permission`／`instructions` 的擁有邊界，也讓「resolve 帶這個鍵一律忽略」的規則變成看路徑而不看內容。即使現行 Gateway 不觸發，這也是死的規格。
- **建議**：刪掉「整包照舊解析」，改成：「get 路徑照現行：sidecar 用 row 組 config；resolve 若帶 `OPENCODE_CONFIG_CONTENT`，一律當未知鍵忽略＋WARN（不解析、不覆蓋）。沒有任何 Gateway 版本回整包 config；若指某個舊 tag，請寫明 tag／版本。」三處同步。

### W4　測試斷言「子行程環境名單不得有 `CLAUDE_CODE_OAUTH_TOKEN`」與 spec 鍵集衝突

- **位置**：`design.md:90`、`tasks.md:26`。
- **原文**：「sidecar 行程另含 `GH_TOKEN`／`SSH_AUTH_SOCK`／`CLAUDE_CODE_OAUTH_TOKEN` 等 canary 時，子行程環境的名單枚舉不得出現它們。」
- **事實**：spec「env 的組成」L110：`claude-code`＋`claude-subscription` 的 `CLAUDE_CODE_OAUTH_TOKEN`「不可空；該組合必然出現」。L111 的 `ANTHROPIC_API_KEY` 在非 anthropic 的 api-key 是刻意空字串——名字會出現、值為空。
- **為什麼是問題**：照字面在 `claude-code` 路徑列舉子行程 env，會看到 resolve 給的 `CLAUDE_CODE_OAUTH_TOKEN`（值不是 canary）與可能的 `ANTHROPIC_API_KEY=""`；測試會誤判正確實作，或逼實作把必要鍵拿掉。若測試其實是 `child_env({})` 的 allow-list 單元測試，則不會有 overlay，但文字沒有寫明是哪一層。
- **建議**：寫明測的是哪一層：
  - allow-list 單元測試：對 `child_env({})` 列舉，三個名字都不得出現；
  - driver 整合測試：用 `agent=opencode`，斷言三個名字都不出現；`claude-code` 路徑則改斷言「值 ≠ canary」且 `CLAUDE_CODE_OAUTH_TOKEN` 等於 resolve 給的值。

## Nit

### N1　「由 worker 映像／設定把名字加進 `KEEP_ENV`」不是可用的機制

- **位置**：`design.md:86`、`tasks.md:23`。
- **事實**：`KEEP_ENV` 是 sidecar 原始碼的 module-level `frozenset`；worker 映像／設定不能直接加名字，必須改 sidecar 原始碼並重建 worker 映像。
- **建議**：改成「改 sidecar 的 `KEEP_ENV`（重建 worker 映像）」，或「由 sidecar 版本加名字」。

### N2　新規則「resolve 帶 `OPENCODE_CONFIG_CONTENT` → 忽略＋WARN」沒有專屬測試

- **位置**：`tasks.md:26` 只有泛用「`env` 含未知鍵 → 忽略＋WARN」；`design.md:91` 的規則是針對這個鍵的特別陷阱（sidecar 很容易反射性地解析它）。
- **建議**：加一個 case：resolve 的 `env` 帶 `OPENCODE_CONFIG_CONTENT`，斷言不進 config／子行程，WARN 只記鍵名不記值。

### N3　「其餘一律不進子行程（含 `OPENCODE_*`、`CLAUDE_CODE_OAUTH_TOKEN`）」字面上與維運變數／resolve 憑證衝突

- **位置**：`design.md:86` vs L87（`OPENCODE_SERVER_PASSWORD` 是維運變數，會疊）與 L90（`CLAUDE_CODE_OAUTH_TOKEN` 由 resolve 給，子行程會看到）。
- **問題**：L86 的「一律」沒有加「繼承來的」；同一段又說 sidecar 維運變數另外疊、子行程會看到 resolve 的憑證。literal reader 會得到「這兩個名字永遠不在子行程」的錯誤結論。
- **建議**：改成「其餘**繼承來的**變數一律不進 base env；resolve 的憑證與 sidecar 維運變數另外疊」。

## public repo 規矩

未發現金鑰、token 值、MyBrain 內容或個人資訊。新增的是標準環境變數名稱（`GH_TOKEN`、`SSH_AUTH_SOCK` 等）與 MyLinuxPool 的檔案／常數名稱（`subproc.py`、`KEEP_ENV`），沒有貼程式或值。`design.md:3` 自述「sidecar 的事實只寫介面層級」，而「環境怎麼組」本來就在介面層級，因此不列 finding；若 PM 要更保守，可只留行為描述、把檔案／常數名放來源註記。

## 未列為 finding 的檢查

- `design.md:99`「Gateway 組『完整子行程環境』」是「放棄的選項」，指的是 Gateway 不該組整個子行程環境，不是 sidecar 繼承整個環境；語意正確，不需改。
- `docs/research/review-33.md`、`review-33-r2.md`、`design-33-fix1.md` 內的「完整 base env」是歷史審查紀錄的快照，不應回頭改寫；本次只要求 `design-33-overview.md` 同步或加註。
- 本次 diff 只動文件，不動程式；`openspec validate --strict` 通過，沒有規格語法或歸檔時序的新問題。
