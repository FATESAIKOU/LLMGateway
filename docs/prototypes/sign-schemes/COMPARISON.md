# 簽章方案比較（原型，決定由本人做）

量測：Linux x86_64（16 核，與其他 session 共享，吞吐隨負載浮動）、Python 3.12.3、cryptography 50.0.2、OpenSSH 9.6p1。單筆延遲：先冷啟（首次呼叫，A 含首次 import）、再暖機後 20 筆樣本；p99 以 nearest-rank 取 20 筆的最大值，誤差大。吞吐：32 執行緒、有限批次（A 320 次、B 32 次、C 320 次），每方案跑 5 次，列中位數與範圍（min–max），含執行緒建立；C 每次呼叫啟動 ssh-keygen。B 為保守純 Python affine 實作。此為短測，非 10 分鐘服務測試，不含 HTTP、SQLite、fsync、防重放或負載控制。以下只看數量級，不看兩位小數。

## 比較表

| 比較項 | A cryptography | B stdlib 只驗 | C SSHSIG | D HMAC |
|---|---|---|---|---|
| 交付 | 推論：需 pinned wheel/venv 或 distro 套件；現 systemd 直接系統 Python 無 pip 步驟。實測：系統 Python 缺套件（`A SKIP`） | 推論：不須新增套件，須交付及維護驗章程式 | 推論：須部署有 -Y 的 OpenSSH、限制子程序與 timeout；實測：本機可用 | 推論：stdlib 可用；未做 |
| Mac／worker | 推論：需安裝支援的 cryptography | 推論：驗章僅 Python，簽章仍需其他工具 | 推論：需確認各端 OpenSSH -Y 版本及環境 | 推論：可攜但需共享秘密；未做 |
| 單次延遲：冷啟／暖機 p50／暖機 p99（最大值） | 實測（隨負載浮動數倍）：冷啟約 8–17 ms（含 import）；暖機 p50 約 0.1–0.2 ms，p99（最大值）約 0.1–1.8 ms | 實測：冷啟約 0.3–0.6 s；暖機 p50 約 0.3–0.6 s | 實測：冷啟與暖機 p50 約 4 ms，p99（最大值）約 4–7 ms | 未做（推論：stdlib 可量） |
| 並發 32 吞吐（數量級；32 執行緒短批次） | 實測：數千～三萬 req/s（不同時段 4 千～3 萬），n=320／次 | 實測：約 2–3 req/s，n=32／次 | 實測：約 1–1.6 千 req/s，n=320／次 | 未做（推論：須另測） |
| 域分隔 | 實測：改 audience 被拒；推論：固定 label 和 audience 隔離 | 實測：改 audience 被拒；推論：同 A | 實測：改 audience 或 namespace 均拒絕（stderr 分別為 `incorrect signature`、`namespace does not match`） | 推論：須獨立金鑰及 label／audience |
| 金鑰格式／重用隧道鑰（未決 2） | 推論：客戶端要讀 OpenSSH 私鑰格式（如 `load_ssh_private_key`）或走 ssh-agent 原始簽章；與 SSH userauth 的分隔只靠 `LGW-HTTP-SIG-V1` 標籤，非形式證明 | 推論：同 A（只驗） | 推論：直接用 OpenSSH 私鑰或 ssh-agent，名冊即 `allowed_signers`；SSHSIG 魔數＋namespace 是 OpenSSH 文件明寫的分隔機制 | 推論：須獨立祕密，不重用隧道鑰 |
| MLP#1529 固定向量 | 實測：原始簽章向量與 B 相容；推論：跨 repo 可共用，須 MLP 同意不同 audience | 實測：驗 A 簽章；推論：可共用原始向量 | 實測：可簽相同八行；推論：SSHSIG 是不同格式，須另定向量及 namespace，不可與 A／B 的簽章互換 | 推論：需另一向量和秘密分配，無法共用公鑰身分 |
| 供應鏈／維護 | 推論：成熟實作但須管理原生依賴、更新與回滾；wheel 自帶 OpenSSL，更新節奏與系統 OpenSSL 不同 | 推論：無外部依賴但需獨立密碼學審查、標準及畸形向量；原型不可上線 | 推論：依 OpenSSH 供應鏈、需處理 subprocess、超時和資源限制；實測：`ssh-keygen -Y verify` 的 exit code 不區分驗簽失敗與工具故障（皆 255），正式化時 401／503 的分界只能靠 stderr 字樣（隨 OpenSSH 版本變，須固定 `LC_ALL=C` 與版本）；每次驗章還要一個簽章暫存檔（`-s` 只收檔案） | 推論：實作簡單但服務端持有全部共享祕密，洩露可冒用各節點 |

吞吐數量級：A（數千～數萬／秒）≫ C（約 1 千／秒）≫ 50／秒 ≫ B（約 2–3／秒）。**數字隨機器負載浮動數倍**：同一次執行內的 5 次 run 彼此相關（同一負載窗），不能代表跨時段的變異；不同時段重跑 A 為 4 千～3 萬 req/s、B 暖機延遲差約 2 倍；上表只保證數量級順序，不保證絕對值。要精確數字須在閒置、固定負載的機器上量，並附 load average。

## 對畸形與小階輸入的行為（實測，`run_vectors.py` 的負向向量）

同一組向量在 A（OpenSSL，`cryptography`）與 B（本原型純 Python）上的實際結果（`result` 是驗章函式的回傳值）：

| 向量 | A | B | 說明 |
|---|---|---|---|
| s+L（延展性） | False | False | 兩者都拒絕 |
| 非規範 y（y≥P） | True | False | A 接受非規範公鑰編碼 |
| 小階公鑰（單位元）＋單位元簽章 | True | False | A 接受；此公鑰可對任何訊息「簽」出通過的簽章 |
| 小階 order-2 公鑰＋偽造簽章 | True | False | A 接受 |
| x=0 且 sign=1 的公鑰 | True | False | A 接受 |
| 非規範單位元公鑰 | True | False | A 接受 |

含意與對策：
- 誠實簽章下 A 與 B 結果一致（固定向量與 RFC 8032 §7.1 前三個向量兩邊都過）；**對抗輸入兩者分歧**。
- 若選 **A**：名冊（公鑰登記）路徑必須拒絕小階與非規範公鑰（至少：y<P、非恆等、`[L]P`＝恆等的檢查，或等價的白名單），否則任何被登記的 key 都可能被假冒；這個檢查不在 `cryptography` 內，需另寫並測。
- 若選 **B**：上列 B 的檢查（`schemes.py` 的 y<P、s<L、`[L]P` 小階檢查）是唯一擋住分歧的程式，已有負向向量守住（改壞 s<L、[L]P、y<P 各自都會轉紅）。
- 若選 **C**：`ssh-keygen` 的行為由 OpenSSH 決定，本原型未測其對上列輸入的結果。

## 注意

- B 的慢是「此保守 affine 實作」的數字（每次加法兩次模反元素，滿階純量乘）；它不代表 Ed25519 驗章的下限。改用 extended 座標或 cryptography 會大幅改變，但尚須審查成本。
- SSHSIG 的 C 每次呼叫都啟動 ssh-keygen，數字包含行程啟動成本。

## 結論

**結論取決於未決 2（worker 簽章金鑰）**：若選另發應用鑰，A 優先；若選重用隧道鑰，C 的域分隔有文件依據、A 只有推論，兩者差距縮小，須重新權衡。另：`run_vectors.py` 中 A 的畸形向量只印 `OBSERVED`（觀察而非斷言）；B 的 `[L]R` 與 `x==0∧sign` 沒有獨立向量（review2 低項，已知）。

建議先以 A 為正式候選，釐清可審核的部署及回滾，並在名冊登記路徑加上對小階／非規範公鑰的拒絕，再與 MLP 確認向量與相依；C 可作備選但需平台版本與 subprocess 限流實測。B 此原型遠低於每秒 50 次且安全審查成本高；D 未實作且密鑰管理不同。A／C 的短測遠超過 50 次／秒，但這不證明設計要求的 10 分鐘服務持續率，仍須在 D2 整體環境測試。選擇留給本人。
