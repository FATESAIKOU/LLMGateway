# 應用層認證上線：呼叫方清冊與跨 repo 簽回（#222 D6c）

這份文件回答一個問題：**切到 `enforce` 之前，我們怎麼知道「所有會呼叫 Gateway 的程式都改好了」？**
repo 內的呼叫方由測試守著（`tests/test_caller_inventory.py`，契約 C-961）；跨 repo 與
執行期沒辦法在離線環境證明，要靠別的團隊簽回與正式主機的觀察期，寫在這裡（C-962）。

- 規劃來源：`REPORT-D6-arch.md` §2.1（呼叫方清單 R1～R9、X1～X5）、§2.3（三層判定）、
  §3.1～§3.3（enforce 準備判定、V-0、白話版）、C-961、C-962。
- 跨 repo 的現況依據：**MLP 11-24 回覆**（透過 PMO 轉）。
- 相關工單：MyLinuxPool#1740（sidecar 簽章）、MyLinuxPool#1741（capability 探針）、
  LLMGateway#292（名冊匯入）；上層是 #222。

## 0. 怎麼讀這份文件

- **寫「已完成」的列一定要有依據**（測試會擋）：依據欄空白、寫「未」或破折號都算違規。
- **還沒拿到簽回的，一律寫「未」**，不要寫成「應該沒問題」或「已完成（推定）」。
- 三層判定（§3）各自獨立：① 過了不代表 ②、③ 過了；切 `enforce` 要三層都成立。

## 1. 現在可以切 enforce 嗎

| 項目 | 狀態 | 依據 |
| --- | --- | --- |
| ① repo 內呼叫方清冊（D6c） | 已完成（已合，PR #299） | `tests/test_caller_inventory.py`（C-961），PR #299（`fe5f60f`，11-24 merged） |
| ② 跨 repo 簽回 | 未 | MLP 11-24 回覆：sidecar 還沒改好；capability 探針 #1741 已 11-24 合併，但還沒簽回（§2） |
| ③ 正式主機 warn 觀察期 | 未 | 還沒開 warn；V-0 已於 2026-11-26 通過（附缺口 #331、#332，§5），warn 觀察期要再由本人決定（§3、§6） |
| 結論 | 未 | ② 與 ③ 都還沒成立，現在不切 `enforce` |

## 2. 跨 repo 呼叫方清單與簽回

MLP 11-24 回覆的重點：**MLP 只有兩個呼叫方**——sidecar（X1，簽章對象）與 capability
探針（X2）；除此之外沒有固定的呼叫方。這一節的每一列都等 MLP 或本人簽回，簽回之前
狀態就是「未」。

| # | 呼叫方 | 打什麼 | enforce 下的處置 | 誰改 | 誰簽回 | 日期 | 依據 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| X1 | MLP sidecar（自己用 urllib 送 HTTP） | list、resolve、cooldown | 簽章（sidecar signer） | MLP | 未 | 未 | MLP 11-24 回覆：這是簽章對象，MyLinuxPool#1740 還沒完成 |
| X2 | MLP capability 健康檢查 | 先 `healthz`，拿到 404 才退回 list | 改成只打 healthz | MLP | 未 | 未 | MyLinuxPool#1741 已 MERGED（11-24 05:31Z）；仍未由 MLP 簽回，故誰簽回與日期維持「未」；新服務一定有 healthz，不會退回 |
| X3 | 本人的 Mac（curl 或 Python client，經 SSH 轉發） | 全部 | 用 Mac 的私鑰簽（P-D4） | 本人 | 未 | 未 | 本人還沒實測（MLP 11-24 回覆轉述）：ssh-agent 與「金鑰帶密語時不會卡住」都還沒測（P-D4） |
| X4 | worker 內的 PM 與 agent 程序，經本機埠或 sidecar | 全部 | 由 X1 的 sidecar 路徑涵蓋 | MLP 盤點 | 未 | 未 | MLP 11-24 回覆：除 sidecar 外沒有固定呼叫方；是否有人用腳本直接打 4710，只能靠 warn 觀察期的 `reason=missing` 發現（§3 ③） |
| X5 | 手機轉發、臨時 curl、Actions | 視情況 | 臨時查詢改用 `lgw` 或 `llmgwctl usage` | 本人與 MLP | 未 | 未 | MLP 11-24 回覆：手機在設計上到不了 4710、Actions 不呼叫；臨時 curl 改用會簽章的 `lgw` 或本機管理 principal 的 `llmgwctl usage` |

補充（MLP 11-24 回覆）：

- **名冊的來源**：名冊由 LGW 的外圍腳本讀 MLP 的公鑰清單產生（LLMGateway#292，本人決定），
  由 PMO 核可後 install。Gateway 只負責安裝與確認名冊，這條路徑不在 X1～X5 的清單裡。
- **殘餘風險**：隧道用的金鑰是容器內同 uid 的私鑰檔，簽章只證明「這是這台 worker」，
  擋不住同一個 uid 的其他程序。這是已知的殘餘風險，不是簽回項目；切 `enforce` 不會
  消除它，只讓「沒有簽章的呼叫」進不了業務路由。

## 3. 三層判定

「未遷移客戶清單為空」不能在離線環境一次證明；它拆成三層，各自有證明方式與時間點。

| 層 | 怎麼證明 | 什麼時候 | 誰簽回 | 日期 | 依據 |
| --- | --- | --- | --- | --- | --- |
| ① repo 內 | 靜態掃描 `lgw`、`lgwctl`、`ops-scripts`、`deploy`、`docs`，每一處打 lgwd 的地方都必須在清冊裡並分類（簽章／healthz／只限 off 的本機／舊版）；新增一處沒分類的就紅 | D6c | 不適用（repo 內測試） | 2026-11-24 | `tests/test_caller_inventory.py` 的 `INVENTORY`（C-961），已完成（已合，PR #299）；變異 8 會轉紅 |
| ② 跨 repo | MLP 書面回答「所有呼叫方、版本 pin、誰改」，並簽回 X1～X5 都改好；本人確認 X3、X5 | V-0 之前（跨 repo 前置） | MLP 與本人 | 未 | 未；MLP 11-24 回覆：sidecar 還沒改好，capability 探針 #1741 已合併，但還不是簽回（§2） |
| ③ 執行期 | 正式主機開 `warn` 觀察：所有已知 principal 都是 `outcome=pass`，而且至少一個完整工作日沒有 `reason=missing` 的 deny。lgwd 不記來源，看到「有人沒簽」就回到 ② 找人 | V-0 之後、`enforce` 之前 | 本人（看觀察期結果決定） | 未 | 未；還沒開 warn；觀察期定義見 §6 |

① 的邊界：靜態掃描抓不到把路由字樣拆開拼接（例如字串相加）或動態產生的路徑；這種情況由 ③ 的 `warn` 觀察期（`reason=missing`）兜底。

## 4. enforce 準備判定（REPORT-D6-arch §3.1）

全部成立，才請本人授權 V-0；V-0 不是「打開強制認證」，只是上線前的實機檢查。

| # | 條件 | 狀態 | 依據 |
| --- | --- | --- | --- |
| 1 | D6a、D6b、D6c 都已合，驗收報告每一格都有測試名稱，全套綠，skip 的原因只是共載或 Mac 專用 | 已合（D6a／b／c 三張）；驗收報告與全套綠：未驗 | D6a：PR #293（`c73ed97`，11-24 merged）；D6b：PR #301（`b9c7c46`，11-24 merged）；D6c：PR #299（`fe5f60f`，11-24 merged）。以上是 `gh pr view` 與 `git log` 查到的合併事實；驗收報告每一格與全套綠未在本單重新查證 |
| 2 | d 單-1 已合；d 單-2 已合，或本人書面接受不做 | 已完成 | d 單-1：PR #290（`7e90320`，`docs/mac-fallback.md` 的判斷指令改打 `/healthz`）；d 單-2：PR #291（`d966aae`，#288 的降級滿 1 小時告警 `auth-degraded`）。兩個 commit 都在 git log 上，本單重新查證 |
| 3 | D5d 段二已合：C-612 在低負載下重跑，報告附原始取樣；資源三鍵已定值 | 已合（段二）；C-612 複測：未驗 | 段二：PR #307（`26fd98f`，11-25 merged），資源三鍵 `TasksMax=512`、`MemoryMax=1536M`、`LimitNOFILE=4096` 已定值（`deploy/units/llmgateway.service`）。C-612 低負載複測：依工單說法 11-24 完成，`docs/deploy.md` 8.17.1 引用 `PM-C612-samples.csv`（5860 個樣本）；本單在本機找不到該檔，完成日期與取樣內容未驗 |
| 4 | P-D4 結果已記錄：Mac 與 worker 的 `ssh -V`、`-Y sign` 的 180 bytes、ssh-agent、有密語時不卡住 | 未 | MLP 11-24 回覆：Mac 的 ssh-agent 與帶密語不卡住都還沒測 |
| 5 | 跨 repo：MLP 的 sidecar signer 與 capability 探針已合到 MLP，聯合向量已通過，問題 4 的清單已簽回 | 未 | MyLinuxPool#1740（X1 sidecar 簽章）仍 OPEN；#1741（X2 capability 探針）已 MERGED，但 X1 未合，條件仍不成立（§2）；問題 4 的清單只回到「兩個呼叫方」 |
| 6 | `docs/deploy.md` 8.18 的 V-0 清單（V-0.1～V-0.7，含 V-0.6b）作為 V-0 的執行計畫 | 未 | V-0 已於 2026-11-26 依 8.18 清單執行（§5）；V-0 不停正式服務。V-0.5 斷言 2 有偏差（改量 `systemctl stop` 耗時），`docs/deploy.md` 8.18 尚未同步；要 PM 確認這個偏差與 8.18 的文字之後，這一列才算成立（原與 `REPORT-D6-arch.md` §3.2 一致的說法已不成立） |

## 5. V-0 前置（真機檢查，要本人單次授權）

V-0 是「可不可以在正式主機上做上線前的實機檢查」，不是「打開強制認證」。每一項都要
本人單次授權，由 PMO 申請、PM 執行；字樣不同就停下，不擴充分類表。細項判準見
`docs/deploy.md` 8.18。

**正式服務現況（2026-11-27）**：正式服務已從 `lgw-v0.8.0` 換成 `lgw-v0.9.0`（11-26 15:35 UTC 完成；auth 維持 `off`）。warn 與 enforce **都還沒開**；P-D4、X1、X2 **都還沒完成**。check-notify 的通知路徑待決：正式主機沒有 aicomm，失敗通知目前只寫 journal。

**V-0 結果（2026-11-26）**：目標 tag `lgw-v0.9.0`（`4090b20…`）；文件／工具修正 #329（PR #330，merge `32352b5…`）；V-0 當時正式服務版本 `lgw-v0.8.0`（之後已換，見上）。V-0 驗證的是 `lgw-v0.9.0` 的新程式（臨時 transient service）加上正式服務 `lgw-v0.8.0` 的資源基線（B 組）。結論：**通過，附兩個缺口**（#331、#332）；warn 觀察期還沒開。

| # | 內容 | 狀態 | 依據 |
| --- | --- | --- | --- |
| V-0.1 | OpenSSH 10.2p1 的分類字樣與 design 相同；不同就停下 | 通過（2026-11-26） | `OpenSSH_10.2p1 Ubuntu-2ubuntu3.8, OpenSSL 3.5.5`；`tests.test_sshsig` Ran 77 tests OK、skipped 0；c506 selftest 與 c507 四個真工具向量 ok（V-0 部署紀錄） |
| V-0.2 | lgwd 以 warn 啟動時自測通過 | 通過（2026-11-26） | warn 啟動 `healthz=200（2s）`、日誌 `serving api`、degraded 0、self-test 失敗 0；收尾 inactive。第一次因名冊權限沒起來，已修（名冊要 root 擁有，#329） |
| V-0.3 | `prlimit` 存在，且以 llmgw 身分能對子行程設限 | 通過（2026-11-26） | prlimit、ssh-keygen 皆 root、可執行；在 `--cpu=2 --as=268435456 --nofile=32` 與 sandbox 下 `ssh-keygen: /nonexistent-v0-probe: No such file or directory`，rc=255（非 124） |
| V-0.4 | `systemd --version` ≥ 251（失敗通知的 `MONITOR_*` 需要），`systemd-analyze verify` 乾淨 | 通過（2026-11-26，附註） | systemd 259（≥251、≥243）。verify：current＝`lgw-v0.8.0` 時 `llmgateway-check-notify.service: Command … is not executable`，rc=1（check-notify 是 #296，只在 v0.9.0）；換成 v0.9.0 路徑後 5 個 unit 一起驗 `verify_rc=0` |
| V-0.5 | 壓測中執行 `systemctl stop`，503 為 0，停止後 cgroup 沒有殘留行程 | 通過（2026-11-26；偏差：斷言 2 改量 `systemctl stop` 耗時） | lgw-v0（enforce）；`systemctl stop 花了 0.89s`；statuses {"200":6563} 無 503；ConnectionResetError 6 筆、`error_window.first=1795705701.14` 晚於 `STOP_EPOCH=1795705700.392`（停止後 0.75 s）；connection_refused 21998（停止後，不算 503）；unit inactive、cgroup.procs 不存在。偏差：文件寫的 `ExecMainExitTimestampMonotonic` 在 transient unit 停止後即回收、讀不到，改量 `systemctl stop` 耗時（等到主行程結束且 cgroup 清乾淨才返回，更嚴） |
| V-0.6 | C-612 在正式主機重跑：用 `systemd-run --scope` 量 C-612 的 scope 組（A，含客戶端的上界），另量 llmgateway.service 的 cgroup 基線（B，服務 cgroup 基線），判準用 A＋B 的和、不放寬（步驟見 `docs/deploy.md` 8.18） | A＋B 判準 PASS；B 組未量到 claude 子行程（#331）（2026-11-26） | A（scope，含客戶端上界）`pids.peak` 94、`memory.peak` 81641472（取樣上界 fd 74、mem.current 79896576、pids 87）；B（正式 `lgw-v0.8.0` cgroup，自啟動累計 peak）前後皆 pids 16、memory 105402368（取樣上界 mem 61849600、pids 12）；和：pids 110≤256、memory ≈187 MB≤768M、fd 74≤2048。缺口 1：B 的 peak 在 10 分鐘（含 keysync 與 2 次用量抓取）前後完全不變，沒有 claude 子行程被起來，「含 claude／gh 子行程的合計高峰」未量到 |
| V-0.6b | 認證庫 IO 前提（#313）：C-612 的 10 分鐘版，TMPDIR 與認證庫同一檔案系統，記錄磁碟種類與 `/proc/pressure/io`，503 數為上界 | 通過（2026-11-26） | `Ran 1 test in 603.788s OK`；`elapsed=602.47 rate=104.19/s statuses={200: 62769} errors=[] wait_count=62789 p50=0.2127 p99=0.5873 max=0.7972`；`load_start=(0.264, 1.169, 0.800) load_end=(7.234, 6.708, 4.074) cores=4`；IO `full avg10` 最大 9.84；fs ext4、ROTA=0；TMPDIR 與認證庫同檔案系統 |
| V-0.7 | `ProtectSystem=strict` 下的檔案存取：讀得到名冊、寫得了 `/var/lib/llmgateway/` | 寫入 PASS；名冊讀取未驗（#332）（2026-11-26） | `ProtectSystem=strict`：寫入 OK、刪除 OK；`/etc/llmgateway/.should-fail: Read-only file system`；名冊 `/etc/llmgateway/auth-registry.json` 不存在，讀取項未驗（缺口 2：安裝名冊後、開 warn 前補驗） |
| P-D4 | Mac 與 worker 的 `ssh -V`、`-Y sign` 的 180 bytes、ssh-agent、有密語時不卡住 | 未 | 未做；本人或 PMO；與 V-0 其他項無依賴；MLP 11-24 回覆說 ssh-agent 與帶密語還沒測 |

V-0 已通過（附缺口 #331、#332，見上）。下一步是在正式主機開 `warn` 觀察期；warn 還沒開，
觀察期與切 `enforce` 都要再問本人（§6）。

## 6. warn 觀察期與切 enforce 的決定

- **觀察期多長**：所有呼叫方都改好，**加上至少一個工作日沒有未簽章的呼叫**
  （`reason=missing` 的 deny 為 0）。這是本人決定的下限，不是「看天數」。
- **切 `enforce` 之前要再問本人一次**：開 `warn` 不等於同意 `enforce`；觀察期結束後
  由 PMO 把結果送本人，本人同意才切。這是本人決定。
- **觀察期看到未簽呼叫怎麼辦**：lgwd 不記來源，只能確定「有人沒簽」。回到 §3 ②，
  請 MLP 或本人盤點是誰（X4 就是這種情況），不要直接切 `enforce`。
- **降級要有人看到**：切過 `enforce` 又降回 `warn`／`off`，等於暫時恢復成不驗證；
  `check` 的 `auth-degraded`（#288）會在滿 1 小時時報 fail。
- **回退等於恢復免認證**：要記錄風險，並在修好後切回；不能停在 `warn` 當成常態。

## 7. 白話版（可以原樣轉給本人）

> **現在要請您決定的，是「可不可以在正式主機上做上線前的實機檢查（V-0）」，還不是
> 「打開強制認證」。**
>
> 測試環境目前完成了第一段（D6a）：用真的程式、真的簽章工具、假的金鑰，驗過三種模式
> （關閉、只記錄、強制）下每一個會呼叫 Gateway 的程式都照預期運作，而且所有輸出裡都
> 找不到簽章、金鑰或憑證。第二段（D6b：換鑰、撤銷、重放攻擊、回退與資料庫損毀後的
> 修復）的測試已寫好並審查通過（#294），已於 11-24 合併（PR #301）；這一段的驗證證據
> 以合併後的驗收報告為準，本文件尚未逐格核對。
>
> 請您授權 V-0 之前，還要這幾件事都完成：
>
> 1. 測試環境的整合驗收全部通過：D6a 已完成；**D6b（#294，PR #301 已合併）的驗收報告尚未逐格核對**；D6c（呼叫方清冊，PR #299 已合併）隨這份文件一起交付。
> 2. 服務的資源上限已經用低負載的實測定好（D5d 段二）。
> 3. 您的 Mac 能正常產生簽章（包含用 ssh-agent，以及金鑰有密語時不會卡住）。
> 4. MyLinuxPool 那邊的呼叫程式已經改成會簽章，並書面確認沒有漏掉的呼叫方
>    （MLP 只有 sidecar 與 capability 探針兩個呼叫方；capability 探針 #1741 已合併，sidecar 簽章 #1740 還沒改好，也還沒書面確認）。
>
> V-0 會在正式主機做 8 項檢查（8.18 的 V-0.1～V-0.7，其中 V-0.6 另加 V-0.6b 的 IO 量測），最重要的是：正式主機的 OpenSSH（10.2p1）回報的訊息
> 和我們測過的版本一致；字樣不一樣就停下，不會為了通過而放寬。V-0 只檢查，
> **不會打開強制認證**。
>
> V-0 通過後，下一步是在正式主機開「只記錄」模式觀察：每個呼叫方都看到成功簽章，
> 而且至少一整個工作日沒有未簽章的請求，才會再來請您同意打開「強制」。
>
> **做完之後仍然存在的風險**
>
> - worker 上任何以同一個帳號執行的程式，都能讀到那台的隧道金鑰並替它簽章；認證只能
>   證明「這是這台 worker」，不能證明是哪一個程式，所以那台上的任何程式都能拿到訂閱
>   憑證（B 的預設權限就是這樣）。
> - 打開強制之後如果降回去，在降回去的期間就等於沒有認證；系統會在降級滿 1 小時時
>   告警。
