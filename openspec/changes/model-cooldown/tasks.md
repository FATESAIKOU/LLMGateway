對應 #76（層級 c）。順序：1 前置審查 → 2 store → 3 登錄 → 4 resolve 不再擋 → 5 list／get → 6 HTTP／client → 7 文件 → 8 實作 PR 驗收；第 9 節是交 MyLinuxPool 的請求，**不在本 PR**。

**語意基準**：冷卻只是提示，不是閘門（本人 2026-10-26 裁定）。resolve 不因冷卻回 409、選把也不因冷卻改變；冷卻只在 `meta`／`list` 裡。本版**沒有**提前解除、沒有 `DELETE`、沒有 `cooldown_release` 稽核、沒有為冷卻新增的呼叫方身分。

**測試約定**：每一步只跑該步列的檔，指令形如 `python -m pytest tests/test_xxx.py -q`（Mac 負載限制，**不跑全套**；整套由 PM 跑）。一律用假時鐘（`lgwd/clock.py` 的 `FakeClock`）、臨時 DB、臨時 `keys.env`；不連外、不碰真 key、不對任何端點送請求。每一步的改壞驗證＝在副本拿掉關鍵修正，確認相關測試轉紅（副本驗完刪除）。規格來源：本 change 的 `specs/subscription-registry/spec.md`（7 條 MODIFIED、0 條 ADDED），設計來源：`design.md` 的決定 1～8。

## 1. 前置（不是程式；擋住整個實作）

- [x] 1.1 本人裁定（2026-10-26，經 PMO 轉達）：「冷卻只是讓叫人方省麻煩、知道下次何時再叫；他真要再叫，Gateway 也擋不了，也沒必要擋。」本版設計已照此改寫：拿掉提前解除（`DELETE`／`cooldown_release`）、為冷卻做的呼叫方身分、resolve 的 409 `cooling`；只留「登錄時驗證模型 id → 400」。**不需要再等裁定**。驗證：本 change 的 design 與 spec 已反映。
- [ ] 1.2 設計經**非作者**審查通過（工單 #76 的規則）。審查要對照本版的 spec delta（7 條 MODIFIED、沒有 ADDED）與 design 的決定 1～8。驗證：審查意見全部結案，或列成 1.3。
- [ ] 1.3 把審查沒收的意見寫回 design（決定與否決選項都要留），不默默改。驗證：`openspec validate model-cooldown --strict`。

## 2. 儲存（spec「登錄冷卻」「稽核紀錄」；design 決定 6）

- [ ] 2.1 `lgwd/store.py`：`SCHEMA` 加 `model_cooldowns (subscription, model, until, PRIMARY KEY (subscription, model))`（`CREATE TABLE IF NOT EXISTS`，additive、無資料搬移）；加 `cooldown_get_model`／`cooldown_set_model`（UPSERT、只在新值較晚時更新，與整把同一條 SQL 形狀）／`model_cooldowns_of`。**`cooldowns` 表與 `cooldown_get`／`cooldown_set` 一行都不改**；`audit` 表不加欄位（`model` 欄本來就有）；**不要**加 `cooldown_delete*`（本版沒有解除）。驗證：`python -m pytest tests/test_registry.py -q`（既有測試的冷卻案例先不改，見 4.1），另加 store 層的新測試。
- [ ] 2.2 舊 DB 相容與退版：用 v5 schema（只有 `cooldowns`＋`audit`）建 DB、塞一筆整把冷卻與一筆舊稽核 → 新 lgwd 起得來、新表被建立、舊冷卻照舊生效；再用「舊程式的 SQL」讀新 DB（`SELECT subscription, until FROM cooldowns` 一把一列、`SELECT seq, ts, subscription, action, agent, until FROM audit`）確認退版可讀。驗證：`python -m pytest tests/test_registry.py -q -k "舊DB or 退版"`。

## 3. 登錄冷卻加 `model`（spec「登錄冷卻」；design 決定 1）

- [ ] 3.1 `lgwd/registry.py` 的 `register_cooldown`：body 多認選填 `model`——有給（且不是 `null`）時驗形狀（非空字串，否則 400 `bad_request`）、**長度 ≤ 200 字元**（超過 400）、拒絕 agent 前綴（以 `opencode/`／`claude-code/` 開頭，400）、拒絕萬用字元（`*`／`?`／`[`，400）、驗 `models.allow` 涵蓋（用既有的 `_covered`，不涵蓋 400 `bad_request`——**不是** 403），只寫（把、模型）那一筆；`"model": null` 等於沒給，走整把路徑；沒給時行為完全不變。驗證順序照舊（未知 id 404 → 缺 until 400 → until 格式／範圍 400 → model 400）。兩層互不刪除、互不縮短。稽核的 `model` 欄記有效值（沒給時 null）。驗證：`python -m pytest tests/test_registry.py -q -k "cooldown or 冷卻 or 登錄"`（新增：只冷卻一個模型、同一模型取較晚、兩模型互不影響、整把與模型並存、model 不在 allow 回 400、帶前綴回 400（含 `opencode/glm-5.3-flash-free` 這個被 `*-free` 涵蓋的案例）、`claude-code/…` 回 400、大小寫不符（`Big-Pickle`）回 400、超過 200 字元回 400、`model: null` 記整把、形狀不對回 400；既有 `test_未知欄位忽略`、`invalid_until`、`until_out_of_range`、8 天邊界、時差正規化照舊綠）。
- [ ] 3.2 稽核：登錄冷卻的紀錄帶 `model`（沒給時 null），`until` 仍是有效值；`agent` 仍為 null；沒有 `cooldown_release` 這種 action。驗證：`python -m pytest tests/test_registry.py -q -k "稽核 or audit"`。

## 4. resolve 不再擋（spec「resolve」「resolve 的選把規則」；design 決定 2、3、4）

- [ ] 4.1 `lgwd/registry.py` 的 `resolve`：**刪掉第 7 步**（`lgwd/registry.py:148-161` 的冷卻排除與 `raise cooling(...)`），原第 8 步變成第 7 步；`priority` 與 id 是唯二排序鍵，`cooldowns`／`model_cooldowns` 不進候選、不進 `min()`。`meta` 多組 `cooldown`（該把整把，`{"until"}` 或 null）與 `model_cooldowns`（該把各模型、只列未過期、沒有時 `{}`），形狀與 `_item` 相同；`meta` 仍不含 `available`／`available_again_at`；`schema` 不 bump。其餘步驟、precedence、`env` 完全不動。驗證：`python -m pytest tests/test_registry.py -q -k "resolve or 選把 or 冷卻"`（新增：A 模型冷卻時指定 A 仍 200＋附資訊、指定 B 仍 200 且 B 的資訊在、整把冷卻仍 200＋附資訊、不指定 model 時 default 冷卻仍 200、冷卻不影響 priority、同 priority 時冷卻不是排序鍵、模型未知時冷卻只是資訊；**改寫**既有 `test_釘住的那把冷卻中`、`test_唯一一把冷卻中`、`test_同provider兩把一把冷卻中`、`test_全部候選冷卻中`、`test_非200不組裝usage也不觸發` 的冷卻案例；其餘選把測試一行不改）。
- [ ] 4.2 `lgwd/errors.py`：刪掉 `cooling()`（沒有生產者）；docstring 的錯誤碼表移除 `409 cooling`、把 400 `bad_request` 補上「登錄冷卻的 model 形狀／萬用字元／allow」。`lgw/client.py` 的 docstring 仍列 `cooling`，但標成「只有舊 Gateway 會回」（legacy），程式不必改。驗證：`python -m pytest tests/test_http.py -q -k "error or cooling"`（`tests/test_http.py:218` 的對照表要移除 cooling）。
- [ ] 4.3 改壞驗證（在副本做）：(a) 把第 7 步「排除冷卻中的候選」加回去 → 4.1 的「指定冷卻中的 A 仍 200」「整把冷卻仍 200」「冷卻不影響 priority」必須轉紅；(b) 把冷卻加進排序（同 priority 時沒冷卻優先）→「冷卻不是排序鍵」必須轉紅；(c) 把 `meta.cooldown`／`meta.model_cooldowns` 拿掉 → 附資訊的測試必須轉紅。沒轉紅的列進 PR 報告。驗證：`python -m pytest tests/test_registry.py -q -k "冷卻"`（每項一次）。

## 5. list／get 顯示模型冷卻（spec「list」「共通 JSON schema」；design 決定 5）

- [ ] 5.1 `lgwd/registry.py` 的 `_item`：多接一次「該把的模型列」查詢，輸出 `model_cooldowns`＝`{model: {"until"}}`（只列未過期；沒有時 `{}`）。**`cooldown` 與 `available` 的計算一行都不改**（`available` 不因模型冷卻變 false）；`usage`／`available_again_at` 仍只在 `list`；`schema` 不 bump。驗證：`python -m pytest tests/test_registry.py -q -k "list or 冷卻 or schema or 形狀"`（新增：形狀、過期不列、空物件、整把與模型同時列、`available` 不變、`schema` 仍 1、`cooldown` 沒被塞模型、`/` 與 `:` 當鍵、get 也帶但沒有 `usage`）。
- [ ] 5.2 值紀律：模型冷卻不讓 key 出現在 list／get／稽核／SQLite 檔（擴充既有的 canary 掃描）。驗證：`python -m pytest "tests/test_registry.py::ResolveTest::test_值不外洩" -q`（既有那支，擴充涵蓋新欄位）。

## 6. HTTP 與 client：`model` 的傳遞（spec「登錄冷卻」；design 決定 1、4）

- [ ] 6.1 `lgwd/http.py`：**不新增路由**（沒有 DELETE、沒有 `.../cooldown/clear`）；cooldown 路由照舊把整個 body 傳給 service（`model` 不必特別處理，`_read_json` 已經照收），確認未知欄位忽略、body 不是物件仍 400、1 MB 上限不變；`do_DELETE` 對 `/v1/subscriptions/{id}/cooldown` 仍 404。驗證：`python -m pytest tests/test_http.py -q -k "cooldown or delete or route"`。
- [ ] 6.2 `lgw/client.py`：`cooldown(id, until, model=None)`（有給才放進 body）；`Result` 的 repr 仍不印 data；docstring 的錯誤碼清單把 `cooling` 標成 legacy（舊 Gateway）。驗證：`python -m pytest tests/test_client.py -q`（新增：有／沒有 model 的 body 形狀；既有 `test_resolve_cooling帶until` 保留，因為那是「對舊 Gateway」的判讀）。

## 7. 文件

- [ ] 7.1 `docs/deploy.md`：第 0 節的服務描述補一句「冷卻只是提示，resolve 不因冷卻拒絕」（`docs/deploy.md:17`）；第 4 節的 client 範例加一行 `c.cooldown("claude-personal", "2026-10-18T15:00:00Z", model="…")`（`docs/deploy.md:256`）；升級順序補「sidecar 先、Gateway 後（或同一窗）」（design「部署」）。驗證：`git diff --stat -- docs/deploy.md` 只動該檔；人工讀過。
- [ ] 7.2 `docs/model-rotation.md`（若已存在）：不動。模型冷卻與輪換表無關（design 不做「計入節流」的推導）。驗證：`git status --short` 不含該檔。
- [ ] 7.3 **主 spec 的 Purpose 與程式 docstring 的舊語意**（`openspec/specs/subscription-registry/spec.md:4` 的 Purpose 仍是「登錄冷卻（只收何時能再用 `until`）」「記冷卻、挑把」；本 change 的 delta 只有 MODIFIED Requirement、沒有 Purpose，而歸檔**不會**用 delta 的 Purpose 改既有 spec 的 Purpose——spec 已存在時它會 `warn "delta Purpose ignored"`（`dist/core/specs-apply.js:197-212`：「A delta Purpose only seeds a spec that does not exist yet」）。所以這件事只能在歸檔之後手改）：
  - **歸檔之後，在歸檔的那個 commit 裡手改主 spec 的 Purpose**（不在實作 PR 裡手改，也不要期待 `openspec archive` 會自己改）：把「只收 `until`」改成「收 `until` 與選填 `model`」、把「記冷卻、挑把」改成「記冷卻（提示）、照常挑把並附冷卻資訊」。
  - 改程式 docstring 的舊說法：`lgwd/registry.py:6`（「meta 不含 cooldown」）、`:8`（「非冷卻 → priority」）、`:11-12`、`lgwd/store.py:3-7`（「只剩兩張表」→ 三張）、`lgw/client.py:19`。驗證：`git grep -n "meta 不含 cooldown\|非冷卻\|只剩兩張表" -- lgwd lgw` 為空；`openspec validate model-cooldown --strict` 綠。
  - Purpose 的驗證（在歸檔那個 commit 之後）：`grep -n "只收何時能再用" openspec/specs/subscription-registry/spec.md` 為空，且 `grep -n "選填 .model." openspec/specs/subscription-registry/spec.md` 命中 Purpose 那一行。

## 8. 實作 PR 的驗收（不跑全套）

- [ ] 8.1 逐步列出的測試檔全部通過，貼原樣輸出與 exit code：`python -m pytest tests/test_registry.py tests/test_http.py tests/test_client.py -q`（單一測試超過 5 分鐘就停下回報）。規格對照：`openspec validate model-cooldown --strict`。
- [ ] 8.2 改壞驗證的結果寫進 PR（4.3 的三項，加上：拿掉 `model` 的 allow 檢查 → 「model 不在 allow」轉紅；把 `available` 改成只看部署 → 「模型冷卻不改 available」轉紅；把 `model_cooldowns` 塞進 `cooldown` → 形狀迴歸轉紅）。沒轉紅的列出來。
- [ ] 8.3 自查：`git status` 只有本 change 的檔案與程式；`openspec validate model-cooldown --strict` 綠；`git diff` 沒有動到 `openspec/specs/`（**這條只限實作 PR**：主 spec 在實作 PR 裡不動；歸檔那一步不在這個 PR 內，歸檔的那個 commit 會手改主 spec 的 Purpose，見 7.3）；`git grep -n "cooling" -- lgwd` 為空（生產者已刪）。
- [ ] 8.4 部署順序自查：release note 寫明「MyLinuxPool sidecar 先、Gateway 後（或同一窗）」，並註明舊 sidecar + 新 Gateway 的代價——開啟時多一次啟動與一次 429、同 provider 多把都在冷卻時會來回換；單一把 provider 的換把結果不變（design 決定 4）。
- [ ] 8.5 **部署後驗收（等本人部署；不是 PR 的一部分）**：在**測試用的那把**登錄一筆模型冷卻（`POST /v1/subscriptions/{id}/cooldown` 帶 `model`），確認 `list` 的 `model_cooldowns` 出現那一筆、`cooldown` 與 `available` 不受影響，把原樣輸出貼到 #76。**正式環境要不要寫一筆提示、要寫在哪一把，由 PMO／本人決定**，實作者不自行決定。驗證：#76 有一則帶輸出的留言。

## 9. 交 MyLinuxPool 的請求（**不在本 PR**；經 PMO 轉，見 design 決定 7）

- [ ] 9.1 把 design 決定 7 的九條整理成一張請求（給 PMO 轉 MyLinuxPool 的 PM）：開隊員前看 list 的 `model_cooldowns` 避開冷卻模型／挑同把其他模型、429 時送實際啟動的 `model`（沒有模型時送 `null` 或省略都可以，Gateway 兩者都當沒給）、Claude 不送 `model`、`DriverState` 加選填 `model`、免費模型 fallback 600 秒起加倍（上限 3600）、**`bad_request` 維持不重送而 `invalid_until`／`until_out_of_range` 重送時要帶同一個 `model`**、保留對舊 Gateway 409 的判讀、不要用 `model_cooldowns` 判斷部署、換把前先試同一把的其他模型、**resolve 回 200 但 `meta` 的冷卻還在未來時，建議順序依冷卻的種類分開：整把冷卻（`meta.cooldown.until` 在未來）時「換把（用 `exclude=<所有冷卻中的把>` 重新 resolve，或從 `list` 挑一把沒冷卻的、用 `subscription=` 指定）→ 沒有其他可用的把時才等到那個時刻」（這時換模型沒有用），只有選中的模型冷卻（`meta.model_cooldowns[meta.model].until` 在未來）時「換同把的其他模型 → 換把 → 都沒有其他可用的把時才等」，兩種都不要直接開**。驗證：訊息在 Angareion 的對應頻道留痕，回報到 #76。
- [ ] 9.2 追蹤對端是否接受 fallback 數字（判斷點 B4）；不接受就以對端的窗口資訊為準，並把結論寫回 #76 的留言。驗證：#76 有一則結論留言。
