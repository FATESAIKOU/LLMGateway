# deploy-keys 草稿審查（workflow／provider 腳本／測試 vs design 決定 2、key-vault）

> 範圍：`.github/workflows/deploy-keys.yml`（71 行）、`deploy/remote-deploy-keys.sh`（73 行）、
> `tests/test_deploy_script.py`（99 行），皆取 HEAD 最新版；對照 design 決定 2 與 specs/key-vault。
> MyLinuxPool#68 原文不在本 repo（不跑 gh、不跨 repo 讀），第 4 節以 design 決定 2 所載的 #68 條件
> （跳板 `restrict,port-forwarding,permitopen=…`；provider `command="…",restrict`）加 OpenSSH 公開行為審查。
> 規矩：只讀 repo；未讀憑證、未跑 `gh`、未 commit。只寫本檔，不改程式。
> 每條標【必修】／【建議】並附檔名:行號。結論：**必修 0 條，建議 9 條**——沒有擋上線的問題，
> 9 條皆為縱深、測試補強與跨 repo 確認事項。

## 0. 先說已驗無誤的部分（對應 PM 四點，不需改）

- **(1) 值不進命令列／log**：workflow 以 shell 內建 `printf 'NAME=%s\n'` 組裝（`.github/workflows/deploy-keys.yml:56-62`），
  值只在 env 與 pipe；`set -eu` 無 `-x`（`:36,53`）；失敗時 GitHub 印的是輸出而非指令，指令文本身無值。
  五把 key 的 secret 只以名稱出現（`:7-11` 註解、`env:` 引用）。
- **(1) 檔內無池位址**：除 `ubuntu-latest` 外無字面主機名／IP／使用者／埠；JUMP／TARGET 全放 secrets（`:45-46`），
  與檔頭註解 `:12`（public repo 不放位址）一致。
- **(2) 不看 SSH_ORIGINAL_COMMAND**：腳本全檔無引用（`deploy/remote-deploy-keys.sh` 全文）；測試直送惡意值驗證忽略
  （`tests/test_deploy_script.py:81-88`）。
- **(2) stdin 上限**：`head -c 65537` 先讀（`remote-deploy-keys.sh:22`），超限 exit 3（`:24-27`），空輸入拒絕（`:28-31`）。
- **(2) 四種驗證全有且舊檔保留**：未知鍵（`:51-54`）、非 KEY=VALUE（`:41-45`）、重複鍵（`:55-58`，邊界比對正確）、
  值含空白或引號（`:59-61`）；任一失敗 exit 4 且 `mv` 未發生（`:64-65` 在驗證之後），暫存檔由 trap 清掉（`:19,66`）；
  測試逐項覆蓋舊檔保留、無殘留、無外洩（`test_deploy_script.py:48-74`）。
- **(2) 原子替換＋0600**：同目錄 mktemp（`:18`，同檔案系統保證 rename 原子）→ 驗證 → `chmod 600`（`:64`）
  → `mv -f`（`:65`）；`umask 077`（`:16`）＋`mkdir -p`（`:17`，目錄 700）。`mv` 失敗時 trap 仍在（`:66` 在 `:65` 之後），
  暫存檔不會殘留。pre-existing 檔權限不影響結果（rename 換掉 inode）。
- **(2) reload 語義**：成功 exit 0；reload 失敗 exit 5 但保留新檔（`:68-73`），與 design 決定 12（SIGHUP 失敗沿用、
  下次啟動讀新檔）相容；測試覆蓋（`test_deploy_script.py:75-79`）。
- **(2) 空值＝未部署**：空字串行通過驗證（`:60` 只擋空白字元，空值無字元），與 `KeyStore`（空視同沒部署）一致；
  workflow 的 secret 未設時即走此路（空行），屬 fail-closed 的漸進部署；互通測試佐證
  （`test_deploy_script.py:25,90-95`）。
- **(3) 觸發與序列化**：只有 `workflow_dispatch`（`:15-16`）；`concurrency` 同名序列化不取消（`:21-23`）；
  `timeout-minutes: 5`（`:29`）；`environment: llmgateway-deploy`（`:28`）。
- **(3) SSH 兩跳皆嚴格驗證**：`StrictHostKeyChecking=yes`＋自訂 `UserKnownHostsFile`（`:65`，取代 runner 預設檔），
  命令列 `-o` 同時適用 `-J` 跳板連線與目標連線；`IdentitiesOnly=yes`（`:64`）、`BatchMode=yes`（`:66`，不掛密碼提示）、
  `ConnectTimeout=20`（`:66`）。secret 內多行 known_hosts 可用（`printf '%s\n'` 原樣保留，`:40`）。
- **(3) 私鑰清理**：`umask 077`＋600 落檔（`:37-39`），`if: always()` 刪除（`:69-71`）；前置檢查 `test -s` 兩檔非空（`:41`）。
- **(3) 失敗即紅燈**：任一步非零即整 job 紅；exit 5（reload 失敗但檔已換）亦紅——是對的，舊 key 繼續服務比靜默過期好。
- **鍵名兩邊一致**：腳本 `ALLOWED`（`:14`）＝ `lgwd/keys.py` `KEY_NAMES` 的值集合，逐字對過。
- **尾端 `deploy` 參數是 seatbelt，不是贅字**：`ssh … "$target_user_host" deploy`（`:67`）。`command=` 正常時它被忽略；
  萬一 provider 端授權行遺失 `command=`，sshd 執行的是 `sh -c "deploy"`（命令不存在，exit 127），stdin 不會被當
  script 執行——否則無參數的 `ssh host < key-material` 會把 key 值餵給 login shell 逐行執行。務必保留（見 S4）。

## A. 建議（9 條）

### S1. 連線失敗時 ssh 自身的 stderr 可能把池位址印進公開 log【建議】

- 位置：`.github/workflows/deploy-keys.yml:63-67`（整條 ssh，無 stderr 處理）。
- 問題：值本身不進 log，但**連線資訊**會。無 `-v` 已是對的；然而 DNS 失敗印
  `Could not resolve hostname <HOST>`、連線被拒／逾時印 `connect to host <HOST> port <PORT>`、
  host key 不符印主機名——HOST／PORT 正是 secrets 裡的 TARGET／JUMP。GitHub 的 secret 遮罩只認整串
  `user@host:port` 完全一致，裸主機名不會被遮；而 public repo 的 workflow log 可被公開檢視。
  觸發條件是部署失敗（自然抖動、輪替失誤時），不需攻擊者動作，等一次紅燈就可能外洩內部位址。
- 修法（三選一）：(a) ssh 加 `2>/dev/null`，只留 exit code（除錯靠重跑與 exit code；最嚴）；
  (b) stderr 過 `sed` 把 `$JUMP`／`$TARGET` 及其 host 部分換成 `[jump]`／`[target]`（指令行只出現變數名，無值）；
  (c) 接受現狀並記錄「失敗 log 可能含內部位址」。建議 (b)，診斷與隱私兼顧。
- 嚴重度：建議（key 值零風險；位址低敏感、失敗時才出現）。

### S2. `permissions: contents: read` 可再縮【建議】

- 位置：`.github/workflows/deploy-keys.yml:18-19`。
- 問題：本 workflow 無 `actions/checkout`，不讀 repo 內容；`contents: read` 用不到。public repo 應最小權限。
- 修法：改 `permissions: {}`。
- 嚴重度：建議。

### S3. 補 `-T` 與 `pipefail`（ hygiene 小項）【建議】

- 位置：`.github/workflows/deploy-keys.yml:53,63-67`。
- 問題：(a) 無 `-T`：實務上 stdin 是 pipe 故不會配 pty，但顯式 `-T` 才不依賴此假設；
  (b) `set -eu` 無 `pipefail`：理論上 printf 段失敗會被 ssh 的 exit 蓋掉（實務 printf 幾乎不失敗）。
- 修法：ssh 加 `-T`；`run:` 加 `set -o pipefail`（二選一也可，只做 `-T` 也行）。
- 嚴重度：建議。

### S4. 尾端 `deploy` 參數加註解，防未來被當贅字清掉【建議】

- 位置：`.github/workflows/deploy-keys.yml:67`。
- 問題：見 §0 末段——它是 provider 授權行遺失 `command=` 時的 seatbelt（`sh -c "deploy"` 直接失敗，
  stdin 不被執行）。無註解的話後人極可能順手刪除。
- 修法：加一行註解說明意圖。
- 嚴重度：建議。

### S5. 要求五把鍵名齊全（防子集部署）【建議】

- 位置：`deploy/remote-deploy-keys.sh:33-62`（驗證段）；`tests/test_deploy_script.py`（缺此案例）。
- 問題：現行驗證只擋「多餘／錯誤」，不擋「缺少」。傳輸在行邊界截斷且 ssh 仍回 0 的組合雖難湊
  （傳輸中斷通常連帶非零 exit），但「缺鍵子集」一旦落盤就是靜默降級（缺的訂閱變未部署）。
  workflow 本來就固定送 5 行（含空值行），要求齊全與正常流程完全相容。
- 修法：驗證結束後檢查 `seen` 恰為五鍵（順序不限）；測試加缺一行／只剩三行的 exit-4 案例。
- 嚴重度：建議（縱深；現行已靠 ssh exit code 擋住絕大多數截斷）。

### S6. `=` 結尾 token（base64 padding）補測試【建議】

- 位置：`deploy/remote-deploy-keys.sh:46`（首個 `=` 分割，值內 `=` 合法）；`tests/test_deploy_script.py:21-27,63-67`。
- 問題：實作是對的（真實 token 常見尾端 `=`），但測試沒覆蓋；將來有人把分割改嚴（如整行禁 `=`）會靜默炸掉部署。
- 修法：加一個值含 `=` 的成功案例。
- 嚴重度：建議。

### S7. 鍵名同步來回測試【建議】

- 位置：`tests/test_deploy_script.py:90-95`（已有 KeyStore 單向互通）。
- 問題：腳本 `ALLOWED` 與 `keys.py` `KEY_NAMES` 各硬編碼五鍵名；將來加第六把時只改一邊，另一邊 loud-fail
  （部署拒絕或服務 KeyError）。單向測試只 cover 腳本→KeyStore。
- 修法：加反向測試——對 `KEY_NAMES` 每個值部署一行並預期成功（任一邊增減即紅燈）。
- 嚴重度：建議。

### S8. 與 MyLinuxPool#68 的相容條件（本 repo 不改程式，逐條確認）【建議】

- 前提（design 決定 2 所載）：跳板 `restrict,port-forwarding,permitopen=…`、無 shell；provider
  `command="<腳本>",restrict`。以下四條須在 #68 側成立，否則部署 loud-fail：
  1. **跳板 key 不得加 `command=`**：`ssh -J` 在 server 端走 session 通道做 `-W` 轉發，`command=` 會用強制命令
     取代該通道用途，`-J` 即斷。只留 `restrict,port-forwarding,permitopen` 即通（現行 workflow `:67` 的用法）。
  2. **`permitopen` 須與 TARGET 的 host:port 字面一致**（含非 22 埠）：OpenSSH 按 `-J`／目標實際使用的字串比對，
     DNS 別名、IP／主機名混用即不通。TARGET 既含 port（`:10,54-55`），#68 側照抄即可。
  3. **同一把部署金鑰在兩端各有一行授權**：workflow 只帶一個 `-i`（`:64`），design 決定 2 寫「一把部署專用」；
     跳板行（轉發專用）與 provider 行（command 專用）須是同一把公鑰。若 #68 發兩把不同金鑰，workflow 才需改
     （ProxyCommand 分鑰）——先確認，不預改。
  4. **reload 路徑有效**：`systemctl --user kill -s HUP` 需該 ssh session 下 `XDG_RUNTIME_DIR` 可用；
     否則每次部署 exit 5（檔已換、服務未重讀，靠下次啟動才生效）。腳本已優雅降級（`:68-73`），但 #68 應驗一次；
     另確認腳本檔有執行位（`command="…/remote-deploy-keys.sh"` 直接執行需 +x，否則改 `command="sh …"`）。
- 嚴重度：建議（全是 loud-fail，不會靜默錯；但任一不成立部署即紅燈，先對齊省時間）。

### S9. 環境保護與 known_hosts 來源確認（repo 設定面，檔內無法驗）【建議】

- 位置：`.github/workflows/deploy-keys.yml:28`（`environment: llmgateway-deploy`）；`:8`（KNOWN_HOSTS secret）。
- 問題：(a) `environment` 只有名字；required reviewers／保護規則在 repo 設定裡，檔內看不見——請確認已設審核者，
  否則「只能手動觸發」少一道人閘；(b) KNOWN_HOSTS 須由可信通道 provision（如 #68 直接給指紋），勿在某處用
  `ssh-keyscan` 現掃（TOFU），否則 StrictHostKeyChecking 形同虛設。
- 嚴重度：建議。

## B. 方法註記

- `#68` 原文不在本 repo，本節第 4 點以 design 決定 2 所載條件為準；`ssh -J` 與 `command=`／`restrict`／`permitopen`
  的互動按 OpenSSH 公開行為判定。
- 行號取 HEAD 最新版（yml 71 行、sh 73 行、test 99 行）。
