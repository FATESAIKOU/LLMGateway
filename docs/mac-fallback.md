# Mac 本機退路：主機不可用時在 Mac 起 LLMGateway（runbook）

對應工單 #154。正式 Gateway 留在原本的服務主機；主機不可用時，退路是直接在 Mac 本機起 lgwd，只聽 `127.0.0.1:4710`。
腳本在 `ops-scripts/mac-fallback/`：`start`、`stop`、`switch`（換版本）、`lib.sh`（共用函式）、`check-scripts`（檢查）、`mac-fallback.conf.example`（設定範例）。

本文件只寫程序與原則。**實際值（程式目錄、密碼檔路徑、狀態目錄、名稱對應）全在設定檔裡，設定檔放 repo 外**，預設是 `~/.config/llmgateway/mac-fallback.conf`，可用環境變數 `LGW_MAC_FALLBACK_CONF` 指定。

## 1. 什麼時候切過去

同時符合兩件事才切：

- 本機 `127.0.0.1:4710` 不通（`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4710/v1/subscriptions` 不是 200）；
- 主機顯示 down（由本人或 PMO 確認，AI 不 ssh 去查）。

主機只是慢或只是隧道斷，不切；先確認隧道。

## 2. 前置（會動到本人的密碼與隧道，先由本人處理）

1. **本人停掉 Mac 上轉發 4710 的隧道**（autossh 或 `ssh -L`）。`start` 會先檢查，有人在聽就不啟動（rc=1）。
2. **密碼檔若已加密，本人先解密**，再 `start`。`stop` 會刪掉 `creds/keys.pass`，所以 `stop` 之後要再起，一樣要先解密。解密後的檔案路徑由設定檔的 `PWFILE` 指定。
3. 設定檔：從 `mac-fallback.conf.example` 複製到 repo 外，把佔位值換成本機實際值，**並 `chmod 600`**（設定檔會被 source 執行；屬於別人或 group／world 可寫的，`start`、`stop`、`switch` 都會拒絕載入）。名稱對應（密碼檔裡的來源名稱 → lgwd 的 key 名稱）由本人對出，不寫進 repo。
4. lgwd 的 checkout：在 repo 本體目錄建一個 detached worktree，HEAD 正好是設定檔的 `TAG`：
   `git worktree add --detach <CODE> <TAG>`
   （這一步 AI 的安全檢查會擋，由本人用 `!` 跑。）
5. 檢查腳本：`ops-scripts/mac-fallback/check-scripts`（bash -n ＋ 全形字檢查，應該全部 OK）。

## 3. 切過去

```
ops-scripts/mac-fallback/start
```

- 路線 1（密碼檔有 `PASS_SOURCE`）：寫 `creds/keys.pass`（0600），由 lgwd 內建的 keysync 拉加密正本解成 `keys.env`。
- 路線 2（沒有 `PASS_SOURCE`）：把 `KEY_MAP` 的值直接寫進 `keys.env`（0600），關掉 keysync。`KEY_MAP` 每一組都必須在密碼檔裡，缺一個就停（rc=1）。
- 啟動前會刪掉 `.keysync.sha` 與 `.keysync.status`（見第 8 節的坑）。
- 腳本只印名稱與「有／沒有」，不印任何值。

## 4. 驗收

`start` 印出 `RESULT: OK` 才算過（rc=0）。它做的驗收是：

- `/v1/subscriptions` 回 200，且有列；
- 沒有冷卻的列都是 `available=True`。

之後再做：

- `curl -s http://127.0.0.1:4710/v1/subscriptions` 看列數與狀態；
- 由 PM 用 sidecar 開一個測試隊員，確認開得起來（Mac 上的隊員都靠這個 4710）。

## 5. 停損

- **`start` 自己停損**：`WAIT_SECONDS`（預設 60 秒）內沒有 200，或 keysync 結果是 fail／舊的／keys.env 是空的，就只停自己記下的 pid、刪掉 `keys.env` 與 `keys.pass`，印 `STOP: 原因`，rc=2。之後 4710 是空的，隧道可以照原樣開回去。
- **跑起來之後有問題**：`ops-scripts/mac-fallback/stop`。它只停 pid 檔裡的那一個（且先確認是 `python3 -m lgwd`），刪掉 key 檔，保留 `llm.db` 給稽核用。
- Mac 睡眠時 lgwd 會停。本人不在、要讓它跑的時候，Mac 不要睡。做法（由本人做，與 lgwd 行程無關，程式不會替你防睡）：
  - 在另一個終端機視窗，用 `!` 跑 `caffeinate -dims`（或在這個 shell 用 `caffeinate -dims &`）。`-d` 防顯示器睡、`-i` 防閒置睡、`-m` 防磁碟睡、`-s` 防系統睡；`-s` 只在接電源時有效，`-i` 在電池時也有效。
  - 不需要時停掉它：在那個視窗按 Ctrl-C，或 `kill <caffeinate 的 pid>`。**切回主機時也要停掉**（它只是讓 Mac 不睡，不會影響 4710）。

## 6. 換版本（switch）

**switch 要本人用 `!` 跑**（它會停掉 lgwd、動 key 檔，AI 的安全檢查會擋）。AI 只能做唯讀的部分（`check-scripts`、`bash -n`、curl 驗收）。

```
ops-scripts/mac-fallback/switch <舊程式目錄> <舊 tag> <新程式目錄> <新 tag>
```

（不給參數就讀設定檔的 `SWITCH_OLD_*`／`SWITCH_NEW_*`。）順序：

1. 前置：兩個 checkout 的 HEAD 都是指定的 tag（舊版也要在，回滾用）；**密碼檔可用**（路線 1 的 `PASS_SOURCE` 或路線 2 的 `KEY_MAP` 全部名稱都在檔裡；只比對名稱）。不過就 rc=1，**舊版沒動**，多半是密碼檔加密或不見了，先請本人解密。
2. **稽核（唯讀）**：Mac 這台的 `llm.db` 裡，`action='get'` 且 `ts >= AUDIT_SINCE` 的筆數。**不是 0 就不換，rc=3。**
3. stop → start 新版（stop 回 rc=1，例如 pid 檔指到別的行程，就不換版本、rc=1）。
4. 驗收：list 200 且有列；可選的檢查項（`VERIFY_PIPES`、`VERIFY_OLD_GET_ID`、`VERIFY_RESOLVE`＋`VERIFY_RESOLVE_MODEL`，留空就不查）。
5. 任一步失敗 → 停損：stop、用舊版 start、驗 list。成功 rc=5（`ROLLED-BACK`），也失敗 rc=6（`ROLLBACK-FAILED`，4710 可能沒有服務，要回報）。

換完之後，設定檔的 `CODE`／`TAG` 要改成新版，下次 `start` 才會起新版。

## 7. 切回主機（不能兩邊同時當主）

Mac 上的 lgwd 與隧道搶的是同一個本機埠 `127.0.0.1:4710`，同一時間只能有一個在聽。順序固定：

1. **先確認主機的 lgwd 起來了**（由本人或照原授權處理，AI 不 ssh）。
2. 在 Mac 跑 `ops-scripts/mac-fallback/stop`：停掉 Mac 的 lgwd，刪掉 key 檔。
3. 本人重開 Mac 的 4710 隧道。
4. 驗 `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4710/v1/subscriptions` 回 200（這時回答的是主機）。

Mac 期間登錄的冷卻不會帶回主機（只是提示，遺失只會讓剛撞牆的那把再被試一次）。主機的 key 檔沒有動過，不用輪替。

**切回後，對 Mac 的 `llm.db` 也要做一次 get 稽核**（要併入整體稽核）。唯讀查詢，把路徑換成設定檔的 `STATE`：

```
sqlite3 "file:<STATE>/llm.db?mode=ro" "SELECT COUNT(*) FROM audit WHERE action='get' AND ts >= '<AUDIT_SINCE>';"
```

結果是 0 才算這一段乾淨。主機那一段由主機自己查，兩段都要看。

## 8. 已知的坑

1. **變數後面直接接全形字**：`set -u` 下，`$VAR（中文）` 會被當成變數名的一部分而出錯。一律寫成 `${VAR}`。`ops-scripts/mac-fallback/check-scripts` 會掃這個，改動腳本後都要跑一次。
2. **殘留的 `.keysync.sha`**：刪了 keys.env 卻留著 `.keysync.sha`，keysync 會以為已經是最新、不寫檔，keys.env 變成空的。`start` 每次都會刪 sha 與 status；手動清理時兩個都要刪。
3. **舊的 `.keysync.status`**：殘留的 status 會騙過啟動檢查。`start` 只認 `ts` 晚於這次啟動時刻的 status，而且 keys.env 不能是空的。
4. **密碼檔加密**：加密之後，`start` 前要先請本人解密（`stop` 會刪 keys.pass）。跑著的 lgwd 不受影響。
5. **Mac 睡眠**：睡眠時 lgwd 也會停，不是只有隧道斷。

## 9. 哪些步驟會被 AI 的安全檢查擋下（要本人用 `!` 跑）

- 建 lgwd 的 checkout（`git worktree add …`）。
- `start`（會讀密碼檔）。
- `stop`、`switch`（會停掉 lgwd、動 key 檔）。
- 停掉或重開 Mac 的隧道、解密密碼檔：一律由本人做。

AI 能做的：`check-scripts`、`bash -n`、唯讀的 curl 驗收、`llm.db` 的唯讀稽核、用 sidecar 開隊員驗證、回報。被擋下的不換方式、不換身分再試，回報給 PMO。
