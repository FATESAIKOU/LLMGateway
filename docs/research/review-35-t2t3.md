# 審查 #35 T2／T3：lgwd 內建 key 拉取、隧道 system 層 unit

**審查對象**：`git diff design/install-simplify...impl/keysync-tunnel`（3 個 commit：`6621a4b` T2 主體、`f7e92fa` T3、`2aea35f` T2 補新面版路徑；7 個檔、+1228／−5，`--name-status` 只有 `deploy/units/llmgateway-tunnel.service`、`lgwd/keysync.py`、`lgwd/main.py`、`ops-scripts/llmgateway-tunnel`、`tests/test_keysync_api.py`、`tests/test_keysync_timer.py`、`tests/test_tunnel.py`）。

**審查模型**：DeepSeek V4.1 Flash（opencode 審查員；非作者）。**審查方式**：唯讀（本報告是唯一新增檔），未改被審程式碼、未 push、未 `git switch`、未碰真機、未讀任何憑證檔或真 key；所有實驗都在系統暫存目錄（`/var/folders/.../T/opencode/keysync-e2e/`），不進 repo。**沒有遇到 rate limit。**

**結論**：高 0、中 0、低 6、資訊 7。**可以合併**；低-1～低-6 建議順手修，低-5 與資訊-4～6 是跨工單 T1 的接縫，要在 #35 記一條給 T1／T4。

## 我自己跑過的測試與實驗

| 項目 | 指令 | 結果 |
|---|---|---|
| 全量 | `python3 -m unittest discover -s tests`（Python 3.12.3） | `Ran 305 tests in 62.980s` → `OK`（基線 267＋新增 38＝305 ✓：`test_keysync_timer.py` 28、`test_keysync_api.py` 4、`test_tunnel.py` 13→19） |
| 時間成分（連跑 6 輪） | `python3 -m unittest tests.test_keysync_timer tests.test_keysync_api tests.test_tunnel` ×6 | 6 輪都 `Ran 51 tests … OK`，沒有間歇失敗（約 13s／輪） |
| 端到端接**真的** `ops-scripts/key-sync`（假 gh／假 openssl／假憑證目錄；把 repo 的 `lgwd/` 與 `ops-scripts/key-sync` 複製成 `<prefix>/opt/llmgateway/current/` 的形狀） | 自寫腳本（暫存目錄） | `run_once -> True ok`；`code_dir` 推導成 `<prefix>/opt/llmgateway/current` ✓；`keys.env` 以 0600 寫成、內容＝假明文 ✓；`.keysync.sha` 在**同一個 state 目錄** ✓；`.keysync.status` 一行 JSON、mode 644 ✓；第二次跑＝`ok`（same_sha）；gh／openssl 的 log 與狀態檔**不含**密碼與 key 值 ✓；openssl 收到的是 `-pass env:LLMGW_KEYS_PASSWORD`（只有變數名） ✓ |
| 邊界 A：逾時殺「整組」 | 假 key-sync 先背景起一個 `sleep`，timeout=0.6s | `timeout` ✓，孫行程也在 6 秒內消失 ✓（`start_new_session`＋`killpg` 真的有效） |
| 邊界 B：`Popen` 回傳前就被 `stop()` | 把 `keysync.subprocess.Popen` 包一層延遲 0.8s 重現窗口 | 100% 重現：`stop()` 耗滿 5s、子行程**活著**（孤兒）、`running` 卻回 False → 低-1 |

## 規格符合度（逐條核對）

### T2（spec-35.md:23）

| 規格 | 實作 | 判定 |
|---|---|---|
| 內部計時（執行緒、預設 600 秒、`LGW_KEYSYNC_INTERVAL` 或 `--keysync-interval`、argv > env > 預設） | `lgwd/keysync.py:38`、`156-192`、`205-212`；`lgwd/main.py:121-132` | ✓ |
| `0`＝關閉 | `main.py:154-156`（回 None＋一行 log）、`keysync.py:185-188`（保險） | ✓ |
| script 未設→關閉並記一行 log | `main.py:157-160`（沒設→用預設；預設不存在／不可執行→關掉＋WARNING） | ✓（解讀見資訊-1） |
| `subprocess` 呼叫既有 `ops-scripts/key-sync` | `keysync.py:230-237`（`[self.script]`，無 argv 值） | ✓ |
| 路徑 `--keysync-script`／`LGW_KEYSYNC_SCRIPT`，預設 `<current>/ops-scripts/key-sync` | `main.py:105, 142`、`keysync.py:47-63`（前綴 `LLMGW_PREFIX`） | ✓ |
| 啟動後 30 秒內先跑一次 | `main.py:230-231` → `keysync.py:205-208`（立刻跑） | ✓ |
| 子行程環境放 `LLMGW_KEYS_PASSWORD`（`$CREDENTIALS_DIRECTORY/keys.pass` 優先，其次自身環境，都沒有就不放） | `keysync.py:84-103, 218-221`；格式／空值／讀不到都回退（`test_keysync_timer.py:239-278`） | ✓ |
| 逾時 120 秒就殺 | `keysync.py:39`（預設 120）、`246-253`＋`_signal_child`（`115-132`） | ✓ |
| 失敗與逾時**不得影響 API**（獨立執行緒、例外全吞、log 不含值） | `keysync.py:205-212`（執行緒＋保險）、`238-239`、`248-253`；`test_keysync_api.py` 三條（成功／失敗／逾時都 200） | ✓（殘留風險見低-1、低-3） |
| 狀態檔 `<state dir>/.keysync.status`：一行 JSON `ts/ok/reason`、0644、原子寫、不含值 | `keysync.py:66-68, 135-153, 279-284`；`test_keysync_timer.py:151-168, 304-319` | ✓（我另外用真的 key-sync 驗過一遍） |
| SIGTERM 計時乾淨結束 | `main.py:233-235` → `keysync.py:194-203`、`269-272`（打斷的那次不寫狀態）；`test_keysync_api.py:267-282` | ✓（窗口見低-1） |
| 既有 `key-sync` 行為與測試不改 | `git diff --name-status` 未動 `ops-scripts/key-sync`，`tests/test_keysync.py` 未動 | ✓ |

### T3（spec-35.md:24、10）

| 規格 | 實作 | 判定 |
|---|---|---|
| 新增 `deploy/units/llmgateway-tunnel.service`（system 層） | 新檔；`User=llmgw`、`Restart=always`、`RestartSec=10`、`RestartPreventExitStatus=78`、`After=llmgateway.service`、`WantedBy=multi-user.target` 全在 | ✓ |
| 設定路徑讀 `/etc/llmgateway/tunnel.env` | `llmgateway-tunnel.service:9`（`Environment=LGW_TUNNEL_ENV=…`） | ✓ |
| `LGW_TUNNEL_ENV` 覆寫、預設維持現行 | `ops-scripts/llmgateway-tunnel:137-145`（空字串→回舊路徑；指到不存在的檔不回退） | ✓ |
| 其他行為不改（ssh 參數、探針、78） | diff 只動 `config_path()`；`tunnel_argv`／`probe_argv`／`EXIT_CONFIG_MISSING=78` 未動 | ✓ |
| 補測試 | `tests/test_tunnel.py:382-448`（5 條行為＋1 條 unit 靜態檢查） | ✓ |

## 高（0）

無。

## 中（0）

無。

## 低（6）

### 低-1 `stop()` 與 `Popen` 之間的窗口：子行程可能變孤兒，且 `running` 會說謊

- **檔案:行**：`lgwd/keysync.py:194-203`（`stop()`）、`230-243`（`Popen` → `self._proc = proc`）、`205-212`（`_loop`）
- **問題**：`_proc` 是在 `Popen()` 回傳**之後**才寫進去，`stop()` 讀 `_proc` 前沒有跟 spawn 互斥。落在這個窗口裡時：`stop()` 送不出 SIGTERM → `communicate(timeout=120)` 繼續等 → `join(timeout=5)` 逾時 → `self._thread` 已被設成 None（`running` 回 False，但執行緒還在跑）→ `main()` 結束、直譯器收掉 daemon 執行緒，**子行程沒人殺**。`stop()` 也只送 SIGTERM，沒有在 grace 之後升級成 SIGKILL。
- **情境**：`systemctl restart/stop` 的那一刻剛好落在 fork/exec 與 `_proc = proc` 之間（我把 `Popen` 包一層延遲就 100% 重現；實務機率極低）。後果是服務已經結束、卻還有一支 `key-sync` 最長再跑 120 秒；此時新服務若已起來，兩支 `key-sync` 會搶同一份 `keys.env`（`key-sync` 假設單一實例：`keys.env` 與 `.keysync.sha` 不是同一筆交易，理論上可能留下「sha 與內容不一致」而不會自我修復的狀態）。
- **建議**：把 `Popen()` 與 `self._proc = proc` 放在同一把鎖裡，`stop()` 先拿同一把鎖再讀 `_proc`（等 fork/exec 的毫秒級時間而已）；`join()` 逾時後對該行程群升級送 SIGKILL，並留一行 log（不要用 `_thread = None` 讓 `running` 看起來是乾淨的）。約 10 行改動。

### 低-2 `_signal_child()` 在「要殺的時候」才查 pgid，PID 回收再配置時可能 killpg 打到無關的行程群

- **檔案:行**：`lgwd/keysync.py:115-132`（`os.getpgid(proc.pid)` → `os.killpg`）
- **問題**：`start_new_session=True` 已經保證子行程的 pgid＝它的 pid，卻在殺的時候用 `getpgid(proc.pid)` 重查。子行程若在此刻剛好被 `run_once()` 回收、PID 立刻被別的行程用掉，`killpg` 目標就變成那個無關行程的行程群（`pgid != getpgid(0)` 的護欄只擋「打到自己的群」這一種）。
- **情境**：機率極低（PID 要在微秒級被重用），但 killpg 打錯群是「打到別的東西」，不是「少殺一個」。逾時與 `stop()` 兩條路徑都會走到。
- **建議**：spawn 當下就記下 pgid，或直接用 `proc.pid` 當 pgid（`start_new_session=True` 已保證同值）。

### 低-3 子行程輸出全進 API 行程的記憶體（`stdout=PIPE` 根本沒用到）

- **檔案:行**：`lgwd/keysync.py:230-237`（`stdout=subprocess.PIPE`、`stderr=subprocess.PIPE`）、`246`（`communicate()`）
- **問題**：`communicate()` 會把子行程的 stdout＋stderr 完整讀進 lgwd 的記憶體，只有 120 秒的時間上限、沒有容量上限。舊設計裡 `key-sync` 是自己一個 unit、輸出由 journal 收；現在輸出變成 API 行程的緩衝區。
- **情境**：現在的 `key-sync` 只印幾行，所以是防禦性問題；但哪天有人在 `key-sync`（或它叫的 `gh`）加了大量輸出，120 秒內就能把 lgwd 灌到 OOM——那正好是「失敗影響 API 服務」這條 MUST NOT。
- **建議**：`stdout=subprocess.DEVNULL`（完全不需要）；stderr 只留尾巴（例如讀固定上限，或寫暫存檔後取最後一段）——`_failed_reason` 只需要最後一個 `failed=`。

### 低-4 `reason` 是從子行程 stderr 抽出來的，不是固定白名單

- **檔案:行**：`lgwd/keysync.py:55`（`_FAILED_RE`）、`106-112`（`_failed_reason`）
- **問題**：`failed=([A-Za-z_][A-Za-z0-9_]*)` 會把「`failed=` 後面第一個識別字」原樣寫進狀態檔（0644）與 journal。今天的 `key-sync` 只印固定 tag（`gh_auth`…），沒問題；但這是「輸出格式剛好安全」而不是「只准固定值」。
- **情境**：防禦性。若日後有人在 `failed=` 後面直接印含敏感值的 token（或密碼），前綴會落到狀態檔與 log；狀態檔在同機若目錄可讀就是別的使用者也讀得到。
- **建議**：改成白名單（`key-sync` 既有 tag 集合＋`timeout`／`spawn_failed`／`exit_N`／`signal_N`），不認得的一律記 `unknown`。

### 低-5「啟動後立刻跑」碰上還沒起來的網路，會留下假的 `ok:false`

- **檔案:行**：`lgwd/main.py:230-231`、`lgwd/keysync.py:205-212`（與 `deploy/units/llmgateway-tunnel.service` 無關；服務 unit 是 T1 的）
- **問題**：計時在 lgwd 裡，不受 `After=network-online.target` 保護（舊的 `ops-scripts/key-sync.service` 有這個排序；舊 timer 是 `OnActiveSec=10min`，開機後正常情況 10 分鐘才第一次跑，`Persistent=true` 只在停機期間錯過 tick 時才補跑）。現在開機後立刻跑：網路／DNS 還沒好就會寫下 `ok:false`，一直到下一個 600 秒週期才會翻回 ok。
- **情境**：重開機後（不是切換窗口——切換時機器本來就在服務）跑 `llmgwctl check`，key 拉取那行會是 fail（依 T1 的規格：最後狀態失敗＝fail、結束碼 1）；運氣不好會讓人以為要停損回滾。
- **建議**：T1 的 `llmgateway.service` 補 `After=network-online.target` 與 `Wants=network-online.target`（照舊 `key-sync.service` 的寫法，一行）。T2 這邊**不用改**——「30 秒內先跑一次」是 spec 指定位元（見下面對「不確定 2」的判斷）。

### 低-6 測試小競態：`interval=0.2s` 之下斷言「log 的 run 次數恰好 1」

- **檔案:行**：`tests/test_keysync_api.py:119`（`LGW_KEYSYNC_INTERVAL="0.2"`）、`:221`（`assertEqual(self.read_log().count("run"), 1)`）
- **問題**：狀態檔與 log 是子行程寫的，斷言「恰好 1」的窗口只有 0.2 秒；機器忙或平行跑測試時，第二次（interval 到期）可能已經插進來 → 假失敗。我連跑 6 輪沒重現，但這是純時間成分。
- **情境**：CI／滿載的機器上偶爾紅燈，而且紅的是「啟動後先跑一次」這條本來想驗的行為。
- **建議**：這一條在 `:212` 的 `self.start(FAKE_KS_MODE="ok")` 單獨覆寫 `LGW_KEYSYNC_INTERVAL="5"`，`==1` 的斷言就有 5 秒餘裕，後面 `wait_until(count >= 2)`（`:223`）也還在 15 秒內會過。

## 資訊（7）

### 資訊-1「script 不可用＝整個關掉」只有一行 WARNING，`check` 分不出「關著」與「沒跑過」

- **檔案:行**：`lgwd/main.py:148-162`（`isfile`＋`access(X_OK)`）
- **問題**：spec 那句「沒設就整個關閉並記一行 log」我讀成「沒有可用的 script 就關掉」（作者也是這樣讀——不然「預設路徑」永遠用不到，兩句會互相矛盾）。目前實作關掉時**不寫狀態檔**，所以 `llmgwctl check` 只能靠「狀態檔不存在／很舊」與「keys.env 年齡」間接看出來；另外這行是 WARNING，在一般開發機／沒有 `/opt` 佈局的機器上每次建 Gateway 都會印（光是 `tests/test_main.py` 的 27 個測試就印了 23 行這種噪音，全量跑更多）。
- **情境**：真機若 install 沒把 `ops-scripts/` 放到版本目錄，key 拉取會靜靜地整個關掉，直到有人看 journal 或發現 keys.env 一直沒更新。
- **建議**：留 WARNING（值得被看到，也照 spec「記一行」）；請 T4 在文件寫「看到這行＝key 拉取整個關掉」；並在 #35 提醒 T1：`check` 對「沒有狀態檔」要明確給 todo／fail，不要當 ok。

### 資訊-2 interval 是「跑完之後再等 N 秒」，不是固定週期

- **檔案:行**：`lgwd/keysync.py:211`（`self._stop.wait(self.interval)` 放在 `run_once()` 之後）
- **問題**：跑一次最長 120 秒時，實際週期可到 720 秒；舊 timer 是 start-to-start。spec 沒指定，兩種都合理。
- **建議**：不改；T4 文件寫「約每 10 分鐘（上一次跑完之後起算）」。

### 資訊-3 外層 120 秒 vs `key-sync` 內部兩個外部指令各 60 秒

- **檔案:行**：`lgwd/keysync.py:39`；`ops-scripts/key-sync:49`（`DEFAULT_TIMEOUT = 60.0`）
- **問題**：`gh` 60s＋`openssl` 60s 的最壞情況會超過外層 120 秒，那一次會被 SIGKILL 記成 `timeout`（其實可能本來會成功）。失敗本身是安全的（`key-sync` 原子換檔、不動舊 key），只是狀態會誤導。
- **建議**：不改（120 是 spec 的固定值）；或日後由 lgwd 一併設定子行程的 `LGWSYNC_TIMEOUT`。

### 資訊-4（跨工單 T1）狀態檔路徑綁在 keys.env 的目錄上

- **檔案:行**：`lgwd/keysync.py:66-68`
- **問題**：狀態檔＝`dirname(keys_path)/.keysync.status`。T1 的 unit／env 只要把 `LGWD_KEYS` 指到 `/var/lib/llmgateway/keys.env`（本來就必須，否則 lgwd 讀不到 key），`check` 讀的 `/var/lib/llmgateway/.keysync.status` 就會對上；但這是**隱式**耦合，沒有參數可以單獨指定狀態檔路徑。
- **建議**：不用改；在 #35 記一行給 T1（`check` 的狀態檔路徑＝`LGWD_KEYS` 同目錄）。

### 資訊-5（跨工單 T1）`--probe-once` 要自己帶 `LGW_TUNNEL_ENV`

- **檔案:行**：`ops-scripts/llmgateway-tunnel:137-145`；`deploy/units/llmgateway-tunnel.service:9`
- **問題**：T3 照 spec 保留「預設路徑＝`$HOME/.config/llmgateway/tunnel.env`」。所以 `llmgwctl check` 呼叫 `llmgateway-tunnel --probe-once` 時，若沒帶 `LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env`（以 root 跑時 HOME 是 root 的家），會讀不到設定 → 回 2＝fail。
- **建議**：T3 不改（照 spec）；請 T1 的 `check` 在呼叫探針時帶這個環境變數，或在 #35 寫明「探針要用新面版設定檔」。

### 資訊-6（跨工單 T1）unit 的落地與新 sandbox 對 `gh` 的影響

- **檔案:行**：`deploy/units/llmgateway-tunnel.service:1-17`；`lgwd/keysync.py:218-237`
- **問題**：兩件事：(a) 這支新 unit 現在沒有任何人會把它放進 `/etc/systemd/system/`（spec §版面把兩個 unit 都算在 install 的責任，T1 的 install 要一起放＋`daemon-reload`）；(b) `lgwd` 內的 key-sync 子行程吃的是 `llmgateway.service` 的 sandbox——`ProtectSystem=strict` 只讓 `/home` 變唯讀，`gh` 讀 `~llmgw/.config/gh` 沒問題，但**不要**加 `ProtectHome=yes`（會讓 key 拉取變 `failed=gh_auth`），且 `gh` token 到期要寫回設定檔時在唯讀 `/home` 下可能失敗。設計 §6 已把「確認 `llmgw` 的 HOME 與權限」列進演練，建議演練清單就直接加這兩條（Lima 上真的跑一次 key-sync）。
- **建議**：記在 #35 給 T1／T4；T2／T3 不需改動。

### 資訊-7 子行程環境是「整包繼承」＋我對不確定 3 的判斷

- **檔案:行**：`lgwd/keysync.py:218-226`；`lgwd/main.py:105-107, 127-132`
- **問題**：子行程環境＝lgwd 環境的完整複本＋`LLMGW_KEYS_PASSWORD`＋`LGWD_KEYS`＋`LGWD_CODE_DIR`；連 `CREDENTIALS_DIRECTORY`（憑證目錄路徑）都傳下去了。key-sync 以同一個帳號跑、本來就讀得到那個目錄，不算新增權限，但要清楚這是「把密碼檔路徑交給子行程」。
- **建議**：不改（整包繼承是既有風格、也讓 `PATH`／`HOME` 之類的照舊可用）；若想收緊，最小作法是 spawn 前把 `CREDENTIALS_DIRECTORY` 從 `child_env` 拿掉。詳見下面「不確定 3」。

## 對作者三個「不確定」的判斷

1. **`ExecStart`／預設 script 路徑 `/opt/llmgateway/current/ops-scripts/key-sync`** — **判斷：對的，但要靠 T1 的 install 兌現**。spec §版面把整份程式放在 `/opt/llmgateway/<tag>/`、`current` 是 symlink；舊 `install.sh` 也是整包 repo（含 `ops-scripts/`）放到版本目錄（`deploy/install.sh:8, 513-523`），所以新面版這樣指是合理且一致的。我照這個形狀做端到端驗證，`LGWD_CODE_DIR` 推導、`LGWD_KEYS` 傳遞、狀態檔落點全部正確。風險只有「T1 的 install 沒把 `ops-scripts/` 放進版本目錄／沒保留執行權」——那時會**靜靜地整個關掉**（資訊-1），所以演練時 `llmgwctl check` 要看得到 key 拉取那行是 ok，而不是只有 keys.env 年齡。
2. **「啟動後立刻跑」** — **判斷：留著（spec 指定位元），但要把 低-5 的排序補在 T1 的 unit**。spec 寫的是「啟動後 30 秒內先跑一次」；立刻跑最單純，也讓升級／重啟後很快拿到新 key。副作用只有開機初期網路未起時會留下一筆假失敗（低-5）。若要更保守，等 5 秒並不違規，但**真正有效的修法是服務 unit 排 `network-online.target`**，不是延後幾秒。
3. **額外旋鈕 `--keysync-timeout`、子行程多帶 `LGWD_KEYS`／`LGWD_CODE_DIR`** — **判斷：兩個都留，理由充分**。(a) timeout 旋鈕：spec 把 120 秒當預設值，而逾時路徑要能被測（不然測試得等 120 秒），有旋鈕才測得起來；預設值仍是 120、unit 也不要設它（建議 T4 文件寫「測試／除錯用」）。(b) `LGWD_KEYS`／`LGWD_CODE_DIR`：**必要**——`key-sync` 自己的預設是舊面版（`~/.local/state/llmgateway/keys.env`、`~/.local/share/llmgateway/current`），不傳的話會解密到舊路徑、載不到 `lgwd/keys.py`（`failed=code_missing`）。我用真的 `key-sync` 驗過：新路徑下 `keys.env` 以 0600 原子換檔、`.keysync.sha` 落在同一個 state 目錄、狀態檔內容正確。唯一要記的是 `code_dir_for()` 靠「目錄名＝`ops-scripts`」推導（`lgwd/keysync.py:71-81`）——T1 若哪天改佈局要一起改，失敗會是看得見的 `failed=code_missing`（不是靜默）。

## 其他核對（非發現）

- public repo 規矩：新增的行沒有機器名、IP、key 的值；測試用 `rendezvous.invalid`、`other-target.invalid`、假密碼字串；沒動真機、沒讀憑證檔。
- 沒有動到別人的檔（`--name-status` 只有 T2／T3 該動的 7 個檔）；`ops-scripts/key-sync` 與 `tests/test_keysync.py` 完全沒動（向下相容要求 ✓）。
- 我沒有跑 `openspec validate`（本單沒動 `openspec/`）。

結論：可合併（高 0、中 0；低-1～低-6 建議順手修，低-5 與資訊-4～6 請在 #35 記一條給 T1／T4）。
