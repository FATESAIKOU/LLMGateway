# 教訓：寫給真機的運維腳本（2026-10-21，#38 finalize 與 lgw-v0.3.0 部署）

背景：#38（拆舊家）與 #41（lgw-v0.3.0 的部署）都是「先寫腳本、審查、再由本人在自己的終端執行」。腳本一律「一條 ssh、BatchMode、遠端 base64 傳 bash、輸出遮罩、任何錯誤就 STOP」。過程中的坑與解法如下；做同型工作前先看。

## 1. 白名單要用真機實測的清單，不要憑想像
- 事件：#38 的 A 調查（llmgw 的 user manager 底下有沒有依賴 linger 的東西）第一版白名單只認 pipewire、dbus、gpg-agent，結果真機是 Ubuntu 桌面預設加 snap 的整套 session 服務（gnome-keyring、gcr-ssh-agent、xdg portal、snapd-desktop-integration、ubuntu-insights…），腳本回 STOP-DEP（停得對，但等於白跑一輪）。
- 做法：先寫一支**只讀、不遮 unit 名**的調查腳本，取回真機實際清單，再依清單寫白名單；白名單只列實際看到的名稱，**每個單元的 `.service`／`.socket`／`.timer`／`.path` 成對都列**（enabled 清單多是 socket／timer，running 清單多是 service）。
- 白名單只回答「這是不是已知的預設」，不回答「有沒有外部行程依賴它」；後者要另外查（連到該 runtime 目錄 socket 的客戶端、容器 mount、系統設定有沒有引用），並在腳本註解寫明前提。
- 行程名（comm）會截斷成 15 字元；比對要改看 cgroup 最後一段的單元名，comm 清單只當後備。

## 2. 遮罩不要吃掉你要看的名稱
- 事件：遮罩用「任何帶點的詞 + 英文尾段 = 網域」，結果 `org.freedesktop.IBus.session.GNOME.service`、`snap.xxx.service` 之類的 unit 名被換成 `<host>`，3 個 unit 的名字看不到，無法下結論。
- 做法：**主要防線是「源頭不印」**（只印固定標籤、計數、rc、明確允許的欄位）；遮罩只是第二層，而且是黑名單——它擋不住 URL 的 userinfo／query、`Authorization`／`Cookie`、小寫的 `api_key=`／`password=`、壓縮的 IPv6、短 token、沒有點的主機別名。所以**呼叫會回憑證的 API（這個專案的 `get`／`resolve` 的 200）時，body 絕不落地、不進 log、不進 `set -x`**，只用 Python 取白名單欄位（狀態碼、schema、meta 的非憑證欄、env 的「鍵名」）。
- 先決定輸出裡**哪些東西必須原樣可見**（unit 名、路徑），遮罩只針對 IP、金鑰樣式、長 token、**有字界的**常見 TLD 網域；不要用「任何尾段」。要印含點名稱的腳本，用自己的遮罩規則，別共用給別支。
- 長 token 規則（例：32 字元以上）會吃掉 64 碼容器 id 與長 cgroup 路徑；印之前先把 id 縮成前 12 碼。

## 3. 不要 export 大量輸出
- 事件：把 `ss -xanp` 的結果 `export` 成環境變數給 awk 用，真機上輸出太大，之後每個 exec 都回 `Argument list too long`（E2BIG），第 3 到第 5 段全部沒結果，錯誤文字還混在輸出裡不顯眼。
- 做法：大輸出一律寫進暫存目錄裡的檔案（`d=$(mktemp -d) || 停`；`trap 'rm -rf "$d"' EXIT`；檔案放在 `$d/` 底下），不要 export，也不要用 `awk -v` 傳多行字串。
- awk 讀兩個檔的 `NR==FNR` 寫法有個邊角：**第一個檔是空的時，第二個檔也會被當成「第一個檔」整個吃掉**（沒有輸出）。「沒有東西可比對＝沒有輸出」剛好是對的答案時它碰巧沒事，但要印別的東西就會錯；改用 `FILENAME==ARGV[1]`，並測「第一個檔為空」。

## 4. 遮罩函式改了要驗：sed 的群組編號
- 事件：遮罩的 sed 規則替換字串寫了 `\5`，但該規則只有四個群組；sed 在**編譯**時就失敗，**整份輸出一行都不會處理**——在不可逆的步驟（finalize）之後看不到任何東西（fail-blind）。審查者在第 4 輪才抓到。
- 做法：改動遮罩後，用假資料在本地跑一次（`printf '假資料' | mask`）；審查清單列「每條 sed 規則的群組數與替換字串對得上嗎」；結論行（`RESULT: …`）從未遮罩的輸出另外取，確保結束碼與判斷不依賴遮罩。

## 5. 腳本不能用 `bash -n` 時，靠審查補
- 事件：自動模式的安全檢查擋下我對部署腳本跑 `bash -n`／`chmod`（分類為 Production Deploy）。**不繞過**。環境允許時，`bash -n`／`shellcheck` 仍是第一道（可對複本、在沙箱或請本人先跑）；被擋時才靠下面的審查補。這不是「用執行取代語法檢查」：腳本是「前段只讀的基線、改動在後」，語法錯誤最多停在改動之前（bash 逐段解析執行，所以改動步驟必須排在唯讀段之後）。交給本人時只用 `! bash <路徑>`，不要 chmod 後直接執行。
- 補法：
  1. 用**不同模型**多輪、逐行推演的審查（Claude Sonnet 4 輪＋Zen 免費 MiMo 2 輪），審查者只讀、不准執行、不准 bash -n；
  2. 審查重點寫進 prompt：會不會印 key 值或位址、每個失敗點的出口與結束碼、BatchMode 單一連線、**對照被呼叫程式的真實行為**（例如 `llmgwctl check` 的 rc 1＝fail、2＝只有 todo）；
  3. 分階段：先「只讀調查」腳本，再「改動」腳本；改動腳本的第一段是唯讀基線，不符合就 STOP；
  4. 每種結果有明確的 `RESULT:` 行與對應的本地結束碼。
- 審查實際抓到的重大問題：以 root 寫可預測的 `/tmp` 檔（symlink 攻擊，critical）、遮罩的 `\5`（critical）、`check` rc=2（只有 todo）被當 fail 而自動回滾、ROLLED-BACK 在回滾失敗時照印、輸出被 `| head` 的 SIGPIPE 殺到一半、linger 驗證靠錯誤文字比對。

## 6. 其他通則（這次成形的）
- 遠端腳本開頭：`exec </dev/null`（sudo 已讀完密碼，子行程不該再碰 stdin）、`exec 2>&1`（stderr 併進 stdout 再過遮罩；ssh 自己的錯誤在本地丟掉）、`trap '' HUP PIPE`（連線中斷或管線關閉不要殺到一半的改動）。Ctrl-C 只停本地，遠端會繼續跑——腳本要印警語。
- 會改動的指令，輸出先落檔再 `head`，不要 `cmd | head`（head 先結束會讓 cmd 吃 SIGPIPE，成功被判成失敗）。
- 自動回滾只由「可歸因於這次改動」的項目觸發；`check` 只有 todo（rc=2）停下回報，不回滾；失敗前先重試幾次（重啟後有寬限期）。
- 回滾本身要驗：`RESULT: ROLLED-BACK` 只在 rollback rc、check、list、current 版本都對時才印，否則 `ROLLBACK-FAILED`。
- 結果碼要對映到本地結束碼，不要讓 `echo "== end"` 蓋掉遠端的失敗。
- 唯讀驗證若會寫稽核（get／resolve 會 append），註明「除稽核 append 外不改狀態」。
- 產生補丁時用 `rtk proxy git diff`：rtk 會壓縮 `git diff` 的輸出，存下來的補丁是空的（本次 #33 的補丁就吃過這個虧）。
