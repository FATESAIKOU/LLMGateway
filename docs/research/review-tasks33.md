模型：Claude Sonnet 5.5（claude-sonnet-5-5）

# 審查：tasks33 worktree 的 `git diff HEAD~1`（7e97e1c）

範圍：`openspec/changes/agent-provider-model-interface/tasks.md` 第 3 節、`design.md` 決定 6.6、Migration 3c／3d、OQ 4。另 grep 整個 change 目錄與 `docs/research/design-33-overview.md`。只讀，未改檔、未連遠端。MyLinuxPool 的測試與 PR 本身我讀不到，凡是引用它們的地方只能審「文件寫得夠不夠清楚」，不能審「測試是否真的存在」。

結論：與裁定方向一致，沒有殘留「手動 cooldown 模擬額度訊號就會換把」的說法；但 3.3 的理由被延伸到 3.4 時說過頭，3.2 的打勾有幾項標準沒寫證據，overview 有一句過時。

## critical

無。

## warning

### W1　3.4／3d 把「冷卻後的 waiting-quota 要等 agent 額度訊號」說得太滿
- 位置：`tasks.md` 3.4 末句「釘住冷卻後的 waiting-quota 行為以測試為證、實機觀察待補（同 3.3 的理由：冷卻是 agent 額度訊號觸發）」；`design.md` 3d 末句同義。
- 發現：這個理由只對**換把觸發**成立。但 `design.md` 6.6 自己寫的 waiting-quota 另有一條入口：釘住那把在 resolve 時回 409 `cooling`＋`until`，sidecar 就當額度用完處理。這條入口的觸發是 **Gateway 的冷卻狀態**（開隊員或自動重開時 resolve 當下），不需要 agent 的額度訊號。也就是說，在釘住的那把上手動登錄 cooldown 再用 `subscription=` 開隊員，理論上能在實機走到 waiting-quota 的「到期再 resolve 同一把」那一半，不必等真的額度事件。
- 影響：文件宣稱整段都只能等真的額度事件，會把本來可以實機補驗的部分一起延後；或者反過來，若這條路徑實機試過而不成立，文件也沒說。
- 建議：把 3.4 拆成兩句。(a) agent 額度訊號 → 記 cooldown → 保留 agent：以測試為證、待補。(b) 釘住且 resolve 回 409 `cooling` 的路徑：說明這次有沒有實機試過，沒試就寫「未試，可用手動 cooldown 補」，試過就寫結果。若 MyLinuxPool#244 的發現其實也包含 (b) 不成立，要在 6.6 寫明。

### W2　3.2 打勾，但「實際」只交代了部分通過條件
- 位置：`tasks.md` 3.2。
- 發現：原通過條件有五項：lgwd 與 sidecar 版本都對上、status 的 model、第一個 step 的模型與 model-mismatch 通過、稽核兩筆 resolve（同 subscription、不同 model）、log／status 無憑證值。「實際」只寫了兩個隊員的 model 與稽核兩筆 resolve。版本對上、第一個 step／model-mismatch、無憑證值三項沒有交代結果。
- 影響：[x] 讀起來是整條都通過，實際只有一部分有寫證據。無憑證值是這個 change 的 canary 重點，未寫結果特別顯眼。
- 建議：補一句各項的結果（含版本字串或 commit 對應、mismatch 檢查、log／status 掃描結果）；若沒驗就明寫「未驗」，不要靠打勾涵蓋。

### W3　overview 仍寫「沒有實機證據」，與新打勾矛盾
- 位置：`docs/research/design-33-overview.md:145`：「服務那台與 worker 的現況：沒有連線…部署與驗收是計畫，沒有實機證據；3.1 的 worker 映像重建也尚未做。」
- 發現：這是 2026-10-20 修訂 4 的快照，但同一頁沒有標註「已過時」，而 tasks 3.0／3.1／3.2／3.4 現在已寫成 2026-10-21 實機通過。另外 overview 沒有任何地方帶到這次的新發現（換把由 agent 訊號觸發），讀者只看 overview 會以為手動 cooldown 能驅動換把（時序圖雖然把「額度用完」畫在 agent 那一側，沒有明講）。
- 建議：把該行改成「已於 2026-10-21 驗過 3.0／3.1／3.2／3.4，3.3 以測試為證」，或加「此節為 10-20 快照」的標註；時序圖的「（額度用完）」旁補一句「由 agent 自己的額度訊號觸發」。

## nit

### N1　3.3 打勾但沒有指向任何具體測試
`[x] 3.3` 的證據是「MyLinuxPool 的單元／整合測試（假來源的換把測試，含 exclude…）」，沒有給 PR、檔名或測試名稱；3.1 倒是給了 PR #265／#266／#270。標籤「以測試為證；實機觀察待補」寫得清楚，這點沒有過度宣稱；但可驗證性差，之後查這張勾的人找不到測試。建議補測試路徑或 PR 編號。另外 `QUOTA_EXHAUSTED`、`StopFailure` rate_limit 這兩個訊號名稱在本 change 其他地方沒有出現（design.md:12 只提 Zen 的 429 `FreeUsageLimitError`），後面又接「之類」，語氣不確定；來源若是 MyLinuxPool 的 driver，請標明。

### N2　OQ 4 與 tasks 4.1 的 gate 用字不一致
design.md OQ 4 現在寫「3b、3d 實機驗收與 3c 的測試為證之後」，語意清楚；`tasks.md` 4.1 仍寫「3 驗收完成」，而 3.3 是 [x] 但實機待補。照字面 4.1 會被視為已滿足。建議 4.1 同步改成與 OQ 4 同樣的說法。

### N3　3.0／3.1 的「實際」沒有對應原本的通過條件
3.0 原條件「前後 `--check`、get 仍在可退版」；3.1 原條件「`sidecar --version` 對上 c 單 commit」。「實際」只寫部署了 lgw-v0.3.0、映像用批次 A／B／C 重建，沒寫 `--check` 與 `--version` 的結果。與 W2 同類，較輕。

### N4　design.md 的 3a／3b 沒有跟著補實機結果
tasks 的 3.0／3.1／3.2 補了「實際」，design 的 Migration 3.0／3a／3b 沒有（只有 3d 補了），兩邊紀錄密度不一致。若 design 只講設計、結果只放 tasks，那 3d 也不必補；請擇一。

## 逐項回答

1. **與裁定一致、有無殘留？** 一致。grep 整個 change 目錄與 overview 找「cooldown／換把／模擬／額度訊號／3c／3d／3.3」，沒有殘留「手動 cooldown 模擬額度訊號會換把」。新句在 tasks 3.3、design 6.6、design 3c 三處一致，且都寫明手動 `POST …/cooldown` 只影響之後的 resolve 選把。仍需注意 W1 的邊界。
2. **有無過度宣稱？** 3.3 的 [x] 旁明確標了「以測試為證；實機觀察待補」，標示足夠。過度宣稱的風險在 3.2（W2）與 3.4 的理由（W1）；3.3 只有可驗證性問題（N1）。
3. **與 design 其他段落有無矛盾？** 沒有直接矛盾：design.md:10、:12、:156、:199、OQ 1／2 都相容。兩處不齊：overview:145（W3）、tasks 4.1 的 gate 字句（N2）。

## 條數
critical 0／warning 3／nit 4
