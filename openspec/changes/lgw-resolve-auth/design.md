# 設計：作者提案，待審查與本人裁定

## 現況與一手證據

`lgwd/http.py:95-113,146-199` 在路由後直接進 service，沒有驗證或授權中介層：GET `/v1/subscriptions`（清單及用量，不含 key，`lgwd/registry.py:136-150`）、GET `/v1/usage`（用量）、GET `/v1/resolve`（選把）、GET `/v1/roles/{role}`（候選）、POST `/v1/subscriptions/{id}/cooldown`（修改冷卻）均無應用認證。已實作的 GET／POST／PUT／DELETE／PATCH 對未知路由回 404；未實作的 HEAD／OPTIONS 等由 stdlib 回 501，單筆 get 已移除；非 POST resolve，而是 **GET**。resolve 成功回 `{schema,env_schema,meta,env}`，api-key 類 env 帶憑證，local-login 例外為空；meta 含 subscription、provider、kind、agent、model、models、endpoint、priority、data、cooldown、model_cooldowns、usage（`lgwd/registry.py:232-310`）。`http.py:19-22,209-215` 對 resolve 加 no-store；`http.py:36-37,47-56` body 上限 1 MiB 是大小限制，不是速率限制。`main.py:42-44,103-118` 僅允許綁 127.0.0.1，程式預設 8480；轉發會合點是 4710。`http.py:146-224` 沒有按來源、速率或連線身分限制；SSH／作業系統邊界另論。`policy.py:23,29` agent 白名單為 claude-code、opencode、agy、codex；roles 為 PMO、PM、impl、rev、qa。現有 policy 限訂閱允許的 agent／model／privacy，並不知道請求身分；resolve 無 role 參數，data 可省略（`registry.py:14-22,247-250`）。

呼叫方：MLP sidecar `modules/sidecar/src/sidecar/adapters/subscription/llmgateway/sources.py:115-120,156-159,312-325,465-480` 自己用 urllib 打本地轉發的 list／resolve／cooldown，不是現行 `lgw.client`；MLP capability 的 `docs/CAPABILITY-DESIGN.md:63` 用 curl 打 list 作健康檢查。Mac 使用自備 SSH forward 和 curl 或 Python client（`docs/deploy.md:238-261`）；worker 內 PM 等程序能透過相同本機埠或 sidecar 使用，但無法從程式證明實際所有呼叫清單，需 MLP 盤點。`lgw/client.py:79-98,135-198` 各 API 共用 urllib `_send`、無認證；`lgwctl/probe.py:16-20`、`lgwctl/check.py:9`、`deploy/install.sh:240` 以 list 200 探針，`lgwctl/usage.py:159` 使用 client。服務端隧道遠端探針亦需遷移；手機轉發與臨時 curl、安裝／回滾工具不可漏。引用 MLP 均為本次唯讀 `origin/master`。

## 圖一：實體／元件與信任

```text
 [MLP 控制面] --核驗公鑰（帶外）--> [LGW 管理員/本地名冊]
 [worker / Mac 私鑰]                       |
      | HTTPSIG 或原始 Ed25519 簽 HTTP       v
      +-- SSH 本機轉發 --> [會合點 loopback 4710] <-- SSH 反向轉發 -- [lgwd]
                               可見明文                         | 驗章/nonce/授權
 [無簽名健康探針] -------------同一通道----------------------->|--> policy --> env
```

兩段 SSH 分別保護各段而非端到端；會合點可讀或改 HTTP。健康探針免簽，管理工具的業務呼叫另有專用身分。

## 身分、名冊、生命週期

建議 worker 沿用其本機 OpenSSH `ssh-ed25519` 隧道金鑰（MLP `docs/POOL_RUNTIME_SPEC.md:169-171,662-666（MLP origin/master 6d6c178）`；`shared-configs/pool-runtime/files/pool-tunnel:577-595`）；Mac 必須登記專用 Ed25519 應用公鑰，不共用 SSH／ssh-agent 的隧道鑰。私鑰只在持有端簽署；名冊的 principal 是不可冒用的管理指定穩定 ID，每把 fingerprint 唯一映射一個 principal，可有輪替中的多把。不要信 header 自稱的 node 名。MLP worker 公鑰登錄 `POOL_WORKERS[]`、provider 用 `NODE_<NAME>`（MLP `docs/KEY-DESIGN.md:51-58`）；worker 重建時才新產、單純重啟保留（MLP `entrypoint.sh:46-61`），不是每次重啟都換。MLP 文件歷史設計與 entrypoint 描述註冊次序有落差，以實際部署的建立流程待 MLP 確認。

LGW 名冊獨立由授權管理員透過受審核帶外流程驗證來源、匯出公鑰並原子安裝於本地；不直接抓取 `NODE_*`／`POOL_WORKERS[]`，既有名冊的執行期驗章不依賴 MLP 可用性，也不新增 GitHub token。**若 LGW 直接依賴這些資料就違反 #221 connectivity 對其他 NODE_* 的禁令**；人工輸入的本地獨立授權資料不是服務對 MLP 設定的執行期依賴，但其管理作業仍有來源依賴，需審查此詮釋。名冊版本單調、格式嚴驗、所有權與權限限管理者寫入；原子替換後由 lgwd 自行偵測並載入（機制見「域分隔與管理工具」），驗證成功才生效，錯誤保留最後有效快照並告警。已驗證的快照同時落盤在信任庫；重啟時名冊檔與信任庫快照走與執行中 reload **同一套判斷**（見「reload 機制」的「重啟」條），名冊檔沒通過就沿用信任庫裡的已驗證快照並告警。enforce 只有在名冊檔與信任庫快照都無效時才拒絕啟動。信任庫或防重放庫缺檔一律當成遺失、不自動建空庫（見「庫的建立與遺失」）。快照包含 principal、key ID、public key、權限、狀態及世代；禁重複 key ID／同公鑰多身分。撤銷一律在新世代把該 key 標 `revoked`，並永久保留其 key ID 與 fingerprint，不得從名冊移除；lgwd 載入時另把已撤銷的 key ID／fingerprint 寫進持久墓碑表。墓碑、名冊世代水位與最後一份已驗證快照放在**信任庫** `auth-trust.sqlite`（同一 transaction 提交），與可丟棄的防重放庫 `auth-replay.sqlite` 分開，理由見「信任庫與防重放庫分開」。之後任何世代（較高世代、reset-generation 或 recover-store 之後亦同）若缺少墓碑列、把它改回有效、或以新 key ID 重用同一公鑰，整份名冊拒絕載入並告警，避免較高世代讓已撤銷鑰復活；載入成功後即拒絕該鍵，並保留防回退水位；公鑰不是密碼，但不在一般日誌輸出。沒有自動同步就沒有宣稱的撤銷時限，監控待處理變更，失效前暫不再發憑證須靠操作決策。

重建順序：舊 worker 停用或標風險→新容器本機生成私鑰→MLP 公鑰登記完成→管理員核驗並加入 LGW 快照→`auth-registry install` 回 0（status 顯示已載入這份名冊的內容雜湊）→新 worker 簽章驗收→舊 key 在下一世代標 `revoked`（永久保留，不移除）；新鍵尚未匯入時 enforce 明確 401，重試並告警，絕不回退免認證。若舊鍵疑似洩露先撤銷而非重疊，接受中斷；若只是計畫輪替可短時間雙鍵並設定截止。不可用舊已撤銷快照恢復。維修 VM 共用金鑰（MLP `profiles/provider/repair/repair-tunnel-launch:370-375` 只證實 fallback 使用身分路徑；共用事實由 #222 轉述，需 MLP 確認）無法區分 VM：建議另發各 VM 專用身分，或將共用鍵當單一低權限群組且不得聲稱單機歸責；不授權即拒絕。

## 協定與選項

建議先以原始 Ed25519 HTTP request-signature v1 作為可審查的候選，搭配 `cryptography`；**本人尚未選定**。spec 因此只寫方案中立的「節點金鑰簽章 v1」；下列線上格式是 (a)／(b) 的候選，本人選定方案前本 change **不歸檔**（tasks 實作前置）。表中 SSHSIG、HMAC 的 wire format 不相容，若選它們須先修訂本提案的 v1 規格、固定向量與 MLP 協議，不能聲稱可互換。Python stdlib 沒有 Ed25519 原生 API。

| 方案 | 交付與成本 | MLP#1529／跨平台 | 供應鏈與可驗證性 |
|---|---|---|---|
| (a) `cryptography` | 新增 Python 及原生相依。伺服端非 `pyproject` 包裝：以部署版本專屬 venv 與經審核 pinned wheel／hash 安裝，unit 指向 venv Python；或 distro 套件，需查各 distro 版本、更新及安全支援，無法保證同 pin。測試安裝、升級、回退與離線可得性。client 用 `lgw[auth]` optional extra。 | 原始 Ed25519 向量易共用；確認 Mac／Linux 支援及相依可得性。 | 成熟函式庫、更新責任和 wheel 來源需審查；驗公開測試向量及相互實作。 |
| (b) 純 stdlib Ed25519 只驗章 | 無伺服器套件、無 subprocess，但須引入或維護驗證碼；客戶端簽章仍需工具。 | 公鑰格式、原始 wire 可共用，跨 Python 平台容易移植。 | 驗章不處理伺服器祕密，側信道考量不同於簽章；但解析、canonical encoding、曲線與小階點驗證仍可能出錯。須第三方審查及 RFC 8032 正反向測試向量，不能只靠短參考程式。 |
| (c) `ssh-keygen -Y` SSHSIG | 系統已要求 ssh-keygen；每次請求開 subprocess，有延遲與 timeout／資源管控成本，需限定 binary／環境並確認 `-Y` 最低版本。 | 原生 OpenSSH 金鑰、namespace；Mac／worker 需驗版本與 MLP 同意相同 SSHSIG 格式；相比原始簽章需另定 wire。 | 沿用 OpenSSH 更新供應鏈，避免自寫密碼；離線固定向量測 namespace、格式、版本與失敗狀態。 |
| (d) 每節點 HMAC | stdlib、無 subprocess；須安全發放、儲存、輪替與撤銷每節點共享祕密。 | 無法共用 MLP#1529 公鑰身分；跨平台容易，但須另建祕密分發。 | HMAC 實作成熟，可測標準向量；Gateway 持有所有 HMAC 祕密，伺服器或備份洩漏會暴露全部節點身分且可偽造請求，訂閱憑證本已在伺服器不代表身分冒用風險相同。 |

D2 的 off 不可載入或檢查新相依，缺少它不得影響原本啟動；warn 缺相依須告警但放行，enforce 才 fail closed。實施 venv 路徑前須本人核准並驗證 rollback；不宣稱 distro 已有套件。mTLS 有憑證及端到端伺服器驗證優勢，代價是 PKI 與部署；SSH certificate 若作 HTTP 簽章的短期憑證也可行，需獨立驗證其 CA、principal、時效及撤銷，不能以「SSH 通道不傳身分」否決。

簽章不加密回應、不認證伺服器；會合點或本機轉發遭控制可讀改流量及憑證，惡意代理還可搶先轉送簽過的請求。若此風險不可接受須另議端到端 TLS 與伺服器身分。

線上格式固定，不讓兩端自行正規化：所有標頭各且僅一次，`X-LGW-Auth-Version: 1`、`X-LGW-Key-Id`（名冊配置的 ASCII `[A-Za-z0-9._-]{1,64}`）、`X-LGW-Timestamp`（十進位 Unix 秒，無前導零）、`X-LGW-Nonce`（32 個密碼學隨機 bytes 的無 padding base64url）、`X-LGW-Signature`（64 bytes 簽章的無 padding base64url）。簽入 ASCII 八行、每行以 LF 結尾：（1）`LGW-HTTP-SIG-V1`；（2）固定 audience `llmgateway`；（3）key ID；（4）timestamp；（5）nonce；（6）HTTP 大寫 method；（7）原始 origin-form request-target（完整 query，ASCII 原始百分號編碼，禁 fragment、CR/LF、非 ASCII、絕對 URI）；（8）實際 body bytes 的小寫 SHA-256 hex。版本標頭本身未簽入，但第一行已綁定 v1，驗證器必須要求標頭為 1 且只出現一次；對這些 bytes 以 Ed25519 簽名。GET 禁止非零 Content-Length，空 body hash 為空 bytes hash；POST 先序列化一次，簽的 bytes 即送出的 bytes。拒絕重複標頭、畸形編碼、超限 body、Content-Length 歧義／Transfer-Encoding、非 origin-form；不得在簽後改寫 URL／body，禁 redirect 重送簽章。簽名綁完整 query 防篡改；固定 audience 防跨服務重放，MLP sidecar 必須用不同 audience，即使沿用同一把 key。固定向量在實作 PR 與跨 repo 測試交付。

伺服器的驗證分兩段。

**一、交易外（不持鎖，只計算）**：驗格式與大小 → 查名冊快照 → 以當下時鐘粗篩 ±60 秒（只為早拒，不是最終判斷）→ 驗簽。任何一步失敗即 401，不碰持久庫。

**二、防重放交易（唯一寫入點，序列化）**：

- **單寫者紀律**：lgwd 對 `auth-replay.sqlite` 只開一條寫入連線，所有寫入（接受請求、清除）與交易內的配額計數都先取同一把行程內鎖，再 `BEGIN IMMEDIATE`；沿用現行 `lgwd/store.py:50-89` 的 `Store.txn`（單一連線＋`RLock`＋`BEGIN IMMEDIATE`＋`busy_timeout`），另加鎖等待期限（現行 `with self._lock` 沒有期限）。**兩個庫**（`auth-replay.sqlite` 與 `auth-trust.sqlite`）都用 `journal_mode=WAL`、`synchronous=FULL`（WAL＋NORMAL 斷電可能丟掉最後提交的 nonce 或墓碑，與「跨重啟持久」不符；墓碑的持久性比 nonce 更重要；程序崩潰不受影響）。兩庫都以 `sqlite3.connect("file:<path>?mode=rw", uri=True)` 開啟，缺檔就失敗，**不用會靜默建空庫的 `sqlite3.connect(path)`**（見「庫的建立與遺失」）。
- **交易內依序**：拿到鎖並 `BEGIN IMMEDIATE` 之後才讀一次伺服器時鐘 `now = floor(time.time())`（整數秒，整個交易只用這一個值）→ 驗 `now − 60 ≤ ts ≤ now + 60` → 驗 `ts > H[key]`（**每 key 清除水位**：該 key 已從 replay 庫清除的 nonce 中最大的簽章 timestamp）→ 查 `(key_id, nonce)` 是否已在庫（在就是重放）→ 驗該 key 的配額（定義見「容量、配額與多實例」）→ `INSERT (key_id, nonce, ts)`（`(key_id, nonce)` 唯一約束，是重放判斷的最終依據）→ COMMIT。COMMIT 成功才進授權與 service；時窗、`H` 不符或重放（含唯一約束衝突）就 rollback 並回 401；配額超過就 rollback 並回 429。重放判斷排在配額之前，所以重放一律是 401，不會因 key 已達上限而變成 429。
- **清除**：背景清除執行緒每 30 秒一次，用同一把鎖、自己的 `BEGIN IMMEDIATE` 交易，交易內讀自己的 `now`，刪除 `ts < now − 65` 的列（60 秒時窗＋5 秒安全邊際），並在同一交易內把每個 key 更新為 `H[key] = max(H[key], 被刪列的最大 ts)`。每批限量（建議 5000 列）以保持交易短；批與批之間放鎖讓請求交易插隊，同一輪重複到沒有可清的列為止，所以積壓不會跨輪累積。即使清除落後，配額只計 `ts ≥ now − 65` 的列，過期未清的列不會讓合法請求被配額誤拒。
- **為什麼擋得住「到期前一刻重放、與清除交錯」**：接受與清除被同一把鎖和 `BEGIN IMMEDIATE` 序列化。重放若排在清除之前，原 nonce 還在，唯一約束擋下；若排在清除之後，`H[key] ≥ ts`，水位擋下。時窗與水位都用交易內的 `now`，不用交易外粗篩的值，所以請求在鎖上等多久都不影響結論。5 秒邊際是防禦縱深（例如兩交易之間時鐘小幅往前跳），不是正確性的前提。不變式（推論，D2 測）：庫中每列都是 `ts > H[key]`；清除後 `H[key] < 該次清除的 now − 65`。
- **忙碌不是故障**：行程內鎖的等待與 SQLite 的 `SQLITE_BUSY` 都重試到同一個期限（建議 5 秒，與現行 `busy_timeout = 5000` 一致）。期限內完成就照常；**期限到仍拿不到，或 I/O 錯誤、磁碟滿、損毀，才算「不可寫」**：enforce 回 503，warn 記不可判定、照舊處理。交易只有一次唯一性查詢、一次配額計數（索引 `(key_id, ts)` 的範圍查詢）、一筆 INSERT 與一次 fsync，正常並行不應出現假 503；D2 以高並行驗收量測。**持續請求率目標**：在 D2 的測試機上，以預驗章並發上限（建議 32）持續送合法簽章請求 10 分鐘，接受的請求至少 50 筆／秒、零假 503、交易等鎖時間 p99 低於 1 秒。依據（推論）：單寫者吞吐上限約為 1／fsync 延遲（常見 1–10 毫秒，即每秒約 100–1000 筆）；現有呼叫方是 sidecar 的 list 輪詢與開 agent 時的 resolve，推論全體遠低於 50 筆／秒，MLP 問題 4 盤點後再調。
- **外部工具不搶寫、禁止多實例**：`auth-replay status` 用唯讀連線（WAL 下不擋寫入者）。鎖檔放在兩庫旁的 `/var/lib/llmgateway/auth-store.lock`（這個目錄本來就存在且持久，不依賴 `/run` 的 RuntimeDirectory 由誰建）。鎖檔由 `auth-store init` 建立並設 `llmgw` 擁有、0600（root 執行的 CLI 仍開得了；`recover-store`、`rebuild-trust` 發現鎖檔缺失或擁有者不對時一併修正）。lgwd 在 warn／enforce 啟動時以 `O_RDWR|O_CREAT`（0600）開啟、`flock(LOCK_EX|LOCK_NB)` 取得**獨佔**鎖並持有到結束。兩種失敗分開處理：**鎖被別人持有**（`EWOULDBLOCK`）＝另一個 lgwd 或寫入工具正在使用，任何模式都拒絕啟動（已在跑的那一個照常服務），這是「禁止多實例」的強制手段；**開不了鎖檔**（權限、唯讀檔案系統等）＝環境錯誤，與 replay store 不可用同類：enforce 拒絕啟動，warn 告警、標示驗證不可判定並照舊處理業務（不寫兩庫）。會寫入的 CLI（`auth-store init`、`recover-store`、`reset-key-water`、`auth-registry reset-generation`、`rebuild-trust`）同樣以 `LOCK_EX|LOCK_NB` 取鎖，拿不到就拒絕執行，所以只在 lgwd 停止時可用，不靠「記得先停服務」。
- **邊界寫死（D2 向量測試）**：ts 與 now 都是整數秒。`now = ts + 60` 接受、`now = ts + 61` 拒；`now = ts − 60` 接受、`now = ts − 61` 拒；`ts = H[key]` 拒、`ts = H[key] + 1` 接受；同一秒、同一 key、不同 nonce 都接受；列在 `now = ts + 65` 仍保留，`now = ts + 66` 起才可被清除。

**時鐘事故**（推論，D2 以 fake clock 測）。不設全域的伺服器時間水位（不採用「全域水位＝`max(水位, now)`」的做法：它讓伺服器時鐘錯一次就全體 503，而且 warn 期間的錯誤會帶進 enforce）。改用 `H[key]` 後：

- **(i) 伺服器時鐘落後 Δ 秒（Δ>60）**：期間合法請求的 ts 看起來「在未來」，被 401（可用性事故，不是安全事故），不入庫。仍在庫中的 nonce 只是保留更久；ts ≤ `H[key]` 的舊簽章一律拒，不需 503。被拒的請求見殘餘 R1。
- **(ii) 伺服器時鐘跳到未來再校正**：期間合法請求因「過舊」被 401。被提早清除的 nonce，其 ts 是真實時間，校正後 ≤ `H[key]`，仍被拒。校正後新請求立即恢復，**不需換鑰，也不需 503**。被拒的請求見 R1。
- **(iii) 客戶端與伺服器一起跳到未來**（典型原因是共用時間來源：同一個 NTP、同一台宿主上的容器或 VM 共用主機時鐘）：**凡在故障期間成功送過請求、而且那筆紀錄在故障期間被清除（請求早於校正 65 秒以上）的 key**，`H[key]` 都落在未來；校正後這些 key 一律 401，直到真實時間追過 `H[key]`（最多約 Δ 秒）。**共用時間來源時，可能是全部有流量的 key，不是一把。** 處理方式由授權維運者選：
  1. 等真實時間追上。
  2. 換鑰：走名冊人工流程，空窗見「維運成本」。
  3. 批次重設：root 執行 `llmgwctl auth-replay reset-key-water --confirm (--key <id>… | --all-future)`（lgwd 須停止，所以重設期間**全部 key** 都暫停），把指定 key 的 `H` 設成 `now − 65`，並寫審計（誰、何時、哪些 key、舊值與新值）。**前置條件**：先確認伺服器時鐘已校正（例如與另一個可信時間來源比對）；時鐘仍錯時重設，會把 `H` 又設在錯的位置。指令會印出目前伺服器時間與各 key 的 `H`，由執行者確認。審計寫兩處：附加式的 `/var/lib/llmgateway/auth-audit.log`（root:root 0600，一行一筆 JSON）與 journal；root 改得了，所以它是**歸責紀錄，不是防竄改**。**代價**：故障期間被會合點截存、而紀錄已被清除的簽章，在各自的 ts 進入時窗時**各可被代送一次**。內容是該客戶端原本要做的事，但可能是 resolve，也就是多發一次憑證。只在確認故障期間會合點可信，或可以接受這個代價時使用。
- **(iv) 客戶端與伺服器一起往過去跳 Δ 秒（聯合回撥）**：新請求的 ts 可能 ≤ `H[key]`，最近有清除的 key（實務上是所有有流量的 key）被 401，最多約 Δ−65 秒。這段期間簽出的 ts 都很舊，校正後被時窗擋下，不構成重放，不需換鑰。校正後若不想等這段 401，同樣可用 (iii)-3 的 `reset-key-water`（`H` 對校正後的時鐘仍在未來），代價與前置條件相同。
- **(v) replay 庫遺失或損毀**：enforce 回 503（啟動時則拒絕啟動），warn 告警並放行。root 執行 `llmgwctl auth-replay recover-store --confirm`（lgwd 須停止），**只重建 `auth-replay.sqlite`**（replay 庫已存在但打不開、損毀或識別不符時，先改名為 `auth-replay.sqlite.broken-<時刻>`（`-wal`／`-shm` 一併）留作事故分析、不得拿來還原，再建新庫；建好後設回 `llmgw` 擁有、0600，`-wal`／`-shm` 一併），寫入信任庫的庫識別，把信任庫快照內每把 key 的 `H` 設為「現在＋60 秒」。信任庫也缺失或損毀時拒絕執行，要先 `rebuild-trust`。不得用備份檔還原 replay 庫：舊備份缺少備份之後的 nonce、`H` 也較低，等於遺失，一律走 recover-store。恢復後約 1–2 分鐘內簽章請求會 401，客戶端重簽即可。**信任庫（墓碑、世代水位、已驗證快照）不在這個檔裡，不受影響**（見「信任庫與防重放庫分開」）。若無法確認遺失前伺服器時鐘正確，再列出受影響的 key 換鑰。
- **warn 與 enforce 共用 replay 庫**：nonce 與 `H` 只來自已驗證的簽章和清除，不含對伺服器時鐘的猜測。切到 enforce 時，lgwd 啟動檢查會列出 `H[key]` 超過「現在＋60 秒」的 key（記事件，也是 `llmgwctl check` 的 fail 項），只拒絕那些 key，不因此全體停擺。

**容量、配額與多實例**：

- **每 key 配額**：交易內以同一個 `now` 計算該 key 在庫中 `ts ≥ now − 65` 的列數（過期待清的列不計），達到上限 `auth_replay_per_key_max`（建議預設 600，即每 key 約每秒 9 筆的持續量）就拒絕：enforce 回 **429** `{"error":"rate_limited"}` 加 `Retry-After`：值為該 key 計入配額的列中最小 ts 離開計數範圍所需的秒數，即 `min(126, max(1, 最小 ts + 66 − now))`（ts 可能在未來至多 60 秒，所以上限 126），不是固定值，免得客戶端照固定短間隔連續撞 429；不寫入、不執行業務；warn 記 would-deny `rate_limited`、不寫入，照舊執行業務。只有已驗簽的請求會走到這一步，所以 429 不洩漏未驗證者的任何資訊。ts 在未來的列（客戶端時鐘超前）也計入，且要更久才離開範圍，所以時鐘超前的 key 實際可持續的量低於每秒 9 筆（推論：超前 60 秒時約每秒 4.8 筆）；這是刻意的，超前的時鐘本來就該校正。依據（推論）：現有呼叫方每 key 每分鐘的量推論遠低於 600；MLP 問題 4 盤點後調整，這是技術參數，不列本人裁定。
- **單一 key 不擠掉別人**：不另設全域列數上限；庫的大小上限自然是「名冊有效 key 數 × 每 key 上限」（例如 50 把 × 600 列，約數 MB），所以單一 key 灌滿自己的配額不會讓其他 key 被 429 或 503。仍會互相影響的只有真實的磁碟滿（503）與鎖／CPU 爭用（由預驗章並發上限與 5 秒期限限住，見上方持續率目標）；這是 DoS 範圍，不宣稱防住。
- **磁碟滿、壞或不可寫**（定義見上）：enforce 回 503 並告警，絕不降級；warn 下記驗證不可判定，照舊執行業務。未驗證的 nonce 不佔持久庫；不提早刪除仍有效的紀錄。
- **多實例**：多實例須共用強一致的 nonce 存儲，本設計不提供，所以禁止多實例；以上方的獨佔 flock 強制，第二個 lgwd 起不來。
- 客戶端每次實際重試（含 429 後）都重簽新 nonce；校時故障回 401，不會自動放寬容忍範圍。

**殘餘風險**：
- **R1　任何已驗簽（或簽章本身有效）、但沒入庫就被拒的簽章**：只有 INSERT 成功的簽章才被記成「用過」。凡是簽章有效（伺服器已驗過，或被交易外的時窗粗篩先擋下而還沒驗）、卻在入庫前被拒的簽章，若被會合點截存，日後只要落入時窗、`ts > H[key]`、而且這次入庫成功，就可被代送一次（每則至多一次，內容是該客戶端原本要做的事，可能是 resolve）。三類：
  1. **時鐘**：客戶端時鐘跑到未來；伺服器時鐘落後（i，待伺服器時間追上或校正後）；伺服器跳到未來後在 60 秒內就校正（ii）。
  2. **配額**（429）：不需任何時鐘故障，該 key 的配額一空出來、簽章仍在時窗內就能代送。
  3. **忙碌逾時或儲存不可寫**（503）：儲存恢復後、簽章仍在時窗內就能代送。
  `ts ≤ H[key]` 被拒的簽章不屬此類：`H` 只會上升（除 R2 的人工重設），它永遠不會再被接受。接受 R1 的理由：要消除它，就得把被拒的簽章也入庫，配額與故障時的寫入就無法拒絕；而會合點本來就能搶先轉送簽過的請求（見「協定與選項」），R1 沒有給它新的能力，只是延後：每則簽章總共仍至多執行一次（第一次被拒、沒有執行）。
- **R2　`reset-key-water` 的代價**：見 (iii)-3。
- **R3　共用時間來源**：(iii)、(iv) 可能同時影響全部 key；Gateway 與客戶端是否共用時間來源，列入給 MLP 的問題 7。

認證失敗（缺／未知／撤銷／格式／過期／錯簽／重放）統一 401 `{"error":"unauthorized"}`，不回具體原因或公鑰，回應內容一致但不保證時序一致（key ID 非祕密）；已驗證但權限不足 403 `{"error":"forbidden"}`，現有業務 policy 的 403 保留；已驗證但超過每 key 配額 429 `{"error":"rate_limited"}`。enforce 驗章系統不可用 503 `{"error":"auth_unavailable"}`；不回憑證。僅安全事件記固定原因分類、時間、路由模板、結果、已驗證 principal（不記標頭、簽章、nonce、query、body、私鑰或 env）；聚合與速率限制日誌避免灌爆。已實作方法的未知路徑仍 404；HEAD／OPTIONS 等未實作方法目前為 501。這不是網路限流，防 DoS 要另議。

## 授權

建議最小可行：名冊每 principal 明列允許的操作集（list、usage、roles、resolve、cooldown），resolve 再明列 agent 集合；未列即拒絕。roles 路徑的 role 與 agent 必須各在該 principal 明列集合，通過後仍套現有 policy。無 role 的 resolve 不能假裝已按 role 限權；不以可偽造的 role 字串代替可信來源。node 授權代表持鑰環境全體程序，並非角色隔離；role→agent 綁定或每角色密鑰是後續提案。捨棄「驗證後所有人全權」：被攻破節點可多拿憑證；也不在本單設計強制 role claim，缺可信身分來源會製造錯誤安全感。健康檢查採獨立、無敏感資料的 GET `/healthz` 200 固定 `{"status":"ok"}`，不驗認證、不返回名冊或訂閱，只表示 HTTP 活著，不表示可 resolve。`llmgwctl check` 另設已簽章 list 及認證後端狀態檢查（`auth-local`）：root 執行時 enforce 任何驗證、名冊、時間或 replay 故障報 fail，warn 不可判定報 todo；非 root 讀不到管理私鑰一律 todo（見「域分隔與管理工具」）；絕不因這些認證檢查失敗而重建健康的 SSH 通道。

## 圖二：控制時序

```text
client                lgwd / nonce庫                    policy
  |-- 簽章請求 ---------->|                               |
  |                       |-- 格式、名冊、粗篩時間、驗簽  |
  |<-- 401 失敗 ----------|                               |
  |                       |-- 交易：時窗+H[key]+重放+配額(429)+INSERT nonce；期限後仍不可寫 -> 503
  |<-- 401 重放 ----------|                               |
  |                       |-- 檢查 principal 權限 -------->| 
  |<-- 403 未授權 --------|                               |
  |                       |------------------------------>| 業務政策
  |<-- 200 或業務錯誤 ----|<------------------------------|
  |  warn 時認證失敗或不可用只記錄，繼續按舊業務處理       |
```

## 圖三：介面與遷移

```text
管理者: lgwd --auth-mode off|warn|enforce --auth-registry <path>
       LGWD_AUTH_MODE / LGWD_AUTH_REGISTRY     [CLI 優先 env]
       /etc/llmgateway/auth-registry.json -> 公鑰、principal、權限、世代
       /var/lib/llmgateway/auth-replay.sqlite -> nonce、H[key]（可丟棄；recover-store 只重建它）
       /var/lib/llmgateway/auth-trust.sqlite  -> 世代水位、撤銷墓碑、已驗證快照（不隨 replay 庫重建）
       /var/lib/llmgateway/.auth-registry.status -> lgwd 寫：已載入世代與雜湊、最近嘗試的雜湊、ok/錯誤分類、輪詢心跳
       /var/lib/llmgateway/auth-store.lock -> lgwd 執行期獨佔 flock（寫入 CLI 拿不到就拒絕）
       /var/lib/llmgateway/auth-audit.log  -> 寫入 CLI 的審計（root:root 0600，附加式）
       auth-store init --confirm                               [首次建兩庫；需 root、停服務；任一庫已存在就拒絕]
       auth-registry check|install|status                      [install 需 root]
       auth-registry reset-generation --confirm <名冊>         [需 root、停服務；水位與快照同一 transaction 換掉]
       auth-registry rebuild-trust --confirm <名冊>            [需 root、停服務]
       auth-replay status / recover-store --confirm / reset-key-water --confirm  [寫入者需 root、停服務]
       llmgwctl check: api-local(/healthz) + auth-local(簽章 list；非 root → todo)
客戶端: Client(base_url, ..., signer=...) / LGW_AUTH_KEY_ID + LGW_AUTH_KEY_FILE
HTTP: X-LGW-Auth-Version / Key-Id / Timestamp / Nonce / Signature
探針: GET /healthz       業務: 既有五條 /v1/*，新標頭
```

以上為**提議介面**，不是現存指令。設定檔提供 `auth_replay_db`、`--auth-replay-db`／`LGWD_AUTH_REPLAY_DB` 與 `auth_trust_db`、`--auth-trust-db`／`LGWD_AUTH_TRUST_DB`（兩者不得是同一檔，啟動時檢查），以及 `auth_replay_per_key_max`（預設 600）；私鑰路徑不傳金鑰值、不可寫 log，客戶端無 signer 預設維持舊行為。off 預設、完全不驗且記 aggregate off；warn 有名冊、驗簽／授權並記 would-deny，但所有結果按舊業務處理，明示仍會洩漏憑證；enforce 全部五條先驗章與授權。warn 的名冊、驗章相依或 replay store 缺失／壞掉均不得阻止啟動或業務，必須記 `auth_unavailable` 與驗證不可判定，不能誤報驗證成功；enforce 啟動須有效名冊（或信任庫的已驗證快照）、可用驗章相依、已存在且庫識別一致的 replay store 與信任庫，否則拒絕啟動；lgwd 在任何模式都不自行建庫（見「庫的建立與遺失」）。管理者以部署設定切換並觀測無簽、錯簽、拒絕、健康、授權結果（無敏感值）。當 warn 無法識別未簽呼叫者時，以部署清單和協同升級確認，不能靠零錯誤推斷所有客戶端已遷移。

每個探針針對目標端點以受控設定明確標為 old 或 new；部署器以已安裝版本的能力清單設定，不從網路 404 推測版本。遠端 MLP 的 probe 必須由該部署負責者同步設定，版本與設定不一致就停止切換；切版／回退須將這項設定納入交易及演練，避免錯把新服務當舊服務。先釋出同時提供 `/healthz` 與 off 的服務；更新 LGW 本機／遠端 probe、install、upgrade、rollback、MLP capability 與手動 SOP，僅部署器明確標示的舊服務且 healthz 回 404 時暫用 list；更新 MLP sidecar 的自有 urllib signer、Mac／手機及直接 client，所有路徑完成簽章與權限後進 warn，核對真實操作與清單，再切 enforce。不得在新服務 enforce 時因業務認證失敗重建 #221 通道；真正 `/healthz` 連續失敗仍按既有門檻重建；端到端 200 仍經同一 4710 到新 `/healthz`。老服務回滾時探針要受控切回舊 list；新 enforce 不可用 list 當免認證探針。認證限制會影響舊版 client：升 `lgw` 0.1.0→0.2.0（建議）、發新版 tag 與 MLP pin，獨立簽章 wire v1；resolve `schema`／`env_schema` 不變，HTTP `/v1` 不變。`lgw/client.py:_send` 加選填 signer，序列化一次後簽五種方法，明確呈現 401/403/429/503 並保留 Result 不洩密（429 依 `Retry-After` 由呼叫方決定是否重簽重試，client 不自動重試）；不得在認證失敗自動重送無簽版本。sidecar 不使用該 client，必須獨立修改或採共用 signer；是否共用 package 由 MLP 決定。

回退按操作手冊：保留 4710/4711 與現有通道，先確認可用管理路徑與新健康探針、告警並記錄風險，受控從 enforce 降 warn 或 off；此即恢復免認證，不能視為安全措施。若懷疑盜鑰，先撤銷並換鑰，不為便利降級；無安全回退就維持拒絕並升報。版本回退須確保探針相容與不恢復過期名冊；別在自動失敗處理中降級。


## 舊決定與稽核資料遷移

真正的舊決定在 `openspec/specs/key-vault/spec.md:54-61` 的 Requirement「不做應用層認證」（本人選定，gateway-v0 決定 5a；同檔 :5 的 v5 註記亦寫它「不變」）；`subscription-registry/spec.md:224` 的「不做認證，紀錄不分是哪個客戶端」只是它在稽核上的推論，`lgwd/http.py:12-13`、`lgw/client.py:31,93` 是程式註解。本 change 以 key-vault delta 把該 Requirement 改名為「應用層認證依模式」並改寫為「僅 off 不要求應用層憑證；warn／enforce 依 resolve-auth」，以 subscription-registry delta 改稽核；於本人核准並上線後取代舊決定。key-vault :5 的 Purpose 註記不在 delta 機制內，由 D3 的實作 PR 同步改，連同上述註解。成功 resolve／cooldown 稽核新增 nullable `principal` 欄，只有真正驗證成功才寫節點 principal；off／warn 未驗證與舊資料為 null，絕不把客戶端自稱當已驗身分。歷史 `get` 保留不動；舊 DB 原子 migration，維持舊列與 API 相容，並測回退及升級。被拒請求仍只記不含祕密的安全事件，不進成功業務稽核；principal 僅節點歸責，不代表使用者身分。

解析在 route／`urlsplit` 前執行：只接受 origin-form、拒絕絕對 URI 與 CR/LF，重複敏感標頭用 `get_all()` 驗只有一份；Content-Length 若存在須唯一且符合 `^[0-9]+$`，拒絕 Transfer-Encoding、GET 非零長度及超限。POST 必須在驗章前讀最多 1 MiB 才能雜湊，未認證者因此可佔用執行緒與流量；timeout、併發限制及測試列入 D2，不能宣稱完全抗 DoS。enforce 未知身分可消耗資源；單一已認證 key 灌量由每 key 配額限住，不會擠掉其他 key（定義見「容量、配額與多實例」），並告警、不提早清除有效 nonce，仍非完整抗 DoS。401 本身不透露原因，客戶端可以利用回應 Date 估算時差並提示「可能時鐘偏差」，不自動調時；伺服器安全日誌供授權維運查固定分類，不保證時間側通道不可枚舉。臨時降 warn／off 須有到期告警（建議 N=1 小時，待本人定案）與升報。

## 威脅、驗證與四點

攻擊者包括可連 4710 的任一 worker 內程序、其他 node、會合點及被攻破 worker。未持有授權私鑰者無法偽造有效請求；持鑰或有簽章代理存取權的程序、同機其他有權讀私鑰使用者、已侵入節點、惡意會合點、竊得 resolve 回應者仍能取用其權限內的憑證；不提供 confidentiality、前向安全、抗 DoS 或已發憑證撤回。

1. 頂尖專家：將驗章、授權與傳輸分層；fail closed、跨重啟防重放、嚴格 canonicalization、雙鍵輪替與可觀測回退均為安全邊界，明確寫出伺服器認證和共用 key 的缺口。
2. 一手資料：以上逐行引用本 repo、MLP `origin/master` 和四張工單；現況是靜態讀碼實證，沒有真機推斷為已測事實。
3. 方法工具：離線 fake clock、臨時自產 Ed25519 key／名冊、loopback HTTP、獨立 SQLite、受控 request bytes 及跨實作固定向量；不使用真鑰或服務。
4. 怎麼證明有用：實作後 fake 環境測五個端點 off/warn/enforce、合法簽章成功、無簽／錯鑰／改 query／body／重複 header／過期／撤銷皆拒絕、並行及重啟重放拒絕、到期前一刻重放與清除交錯仍至多一次、時窗與水位邊界向量、高並行無假 503 與持續請求率目標、每 key 配額（429、過期列不計入、單 key 灌量不影響他 key、重放先於配額、配額拒絕的簽章日後至多一次）、兩庫缺檔或庫識別不符時 enforce 拒絕啟動且不建空庫、enforce 的 store failure fail closed（時鐘回撥只回 401，不 503）、warn 故障照舊處理、角色／agent 拒絕、輪換與 reload 原子性（背景輪詢偵測、閒置時也載入、同世代同內容為無變更、壞檔沿用舊快照且退避、還原成已載入內容時 status 回到 ok、status 只在狀態變化時寫並有輪詢心跳、重啟與執行中用同一套判斷：同世代內容被改動後重啟仍拒絕並沿用信任庫快照）、已撤銷 key 在較高世代被移除或改回有效時整份拒絕、時鐘五情況（伺服器落後、伺服器跳未來再校正不需換鑰且無 503、主從同跳未來時所有故障期間有流量的 key 同時受影響且可批次重設、聯合回撥、replay 庫遺失後 recover-store 且墓碑仍在）、warn 期間的伺服器時鐘錯誤切 enforce 後不造成全體停擺、非 root 的 check 為 todo（結束碼 2）與 usage 在 enforce 結束碼 1 且不無簽重送、舊客戶端 warn／新 client 舊服務、健康與回退不重建通道，斷言不洩密。V-0 真機：**待本人授權**後在指定維護窗用測試身分經 4710 測上述正負案例、輪換及探針，事前備可用管理通道與回退；本設計不執行。

## 給 MLP PM 的問題

1. #1529 能否以同一 OpenSSH Ed25519 公鑰、key ID 與不同 audience 共用簽章格式及固定向量？sidecar 端重放存儲與時鐘要如何支援？
2. 誰核驗與轉交 POOL_WORKERS／NODE 公鑰及撤銷，發佈延遲與故障升報 SLA？新 worker 建立的實際註冊先後及舊鍵何時刪除？
3. Mac、手機、維修 VM 各有哪些身分與可用私鑰，repair 是否實際共用，能否發各機專鑰？
4. 請列 sidecar、capability probe、PM／手動 curl、Actions 等全部呼叫與版本 pin，誰改、何時完成，是否能先部署 `/healthz` 相容探針？
5. MLP 是否願意共用 signer 套件與 cryptography 相依或 SSHSIG？在所有平台如何保護 key、校時、回傳 401/403？
6. worker 私鑰屬於哪個 uid？sidecar 和 agent 是否同 uid、是否能讀 key 或 ssh-agent socket？能否以獨立 uid／受限簽章 daemon 與呼叫者授權隔離？被攻破 sidecar 或 daemon 能否變成替 agent 簽章的代理？請實測而非假設。
7. 每週每 worker 的重建頻率及 P95、上游公鑰登記延遲？跨重建持久鑰、受認證單向推送或短期 CA 憑證何者可行，誰值班，能承諾什麼撤銷及 401 空窗？Gateway 主機與各 worker／Mac 的時間來源是否共用（同一 NTP、同一宿主時鐘）？這決定時鐘事故 (iii)、(iv) 會不會一次影響全部 key。

## 維運成本與中間選項

基準目前無法估值：MLP 尚未提供每 worker 每日／週重建次數與分布、LGW 管理員是誰、人工核驗／安裝每次幾分鐘，或 401 可接受上限。每次新鍵在 MLP 登錄後到 LGW 新世代載入前，enforce 必然 401；空窗 = 上游登記延遲 + 排隊／核驗時間 + 發布／reload 時間，人工離線可能無上限。要求 PMO 與 MLP 先量測正常與 P95／最壞、定義值班／升報，空窗上限未獲本人同意前不得 enforce。人工分鐘及頻率不可臆測。

可選：（a）人工名冊：運作簡單但每次重建須排隊，無保證 SLA；（b）讓私鑰跨重建持久：降低變更頻率，但需安全持久儲存、備份與移機／撤銷，違失竊隔離利益；（c）MLP 單向推公鑰至固定收件點，LGW 不讀 MLP 設定，但要受認證的發佈者、完整性、世代、防回退、撤銷、失敗與監測，新增推送控制面，依賴上游可用性；（d）管理員 CA 簽短期 HTTP 身分證書：LGW 信任 CA、單次重建不改每機名冊，但憑證簽發身分、保護 CA、短效期離線可用性與即時撤銷仍須設計。這些不保證不依賴 MLP 運維；#221 不依賴的結論只指 LGW 執行期不讀 MLP node 設定。

## 域分隔與管理工具

原始 Ed25519 標籤加 audience 和每項長度／字元限制作跨協定域分隔；現行 SSH userauth 的二進位前綴不同，但這是推論而非形式證明。SSHSIG 的 namespace 是替代做法；LGW、MLP sidecar 與其他服務必須使用不同 audience／namespace，互拒固定向量涵蓋兩服務。ssh-agent 是簽章 oracle，可存取 socket 的程序可能借用；Mac 應用金鑰不得同時用於 SSH 或放入一般共用 ssh-agent，是否提供隔離 signer 須另議。worker 若共用 tunnel key，同機讀鑰或 agent socket 的程序仍可簽；sidecar 或簽章 daemon 若被攻破或缺少呼叫者授權可被當代理，uid 隔離本身並不足夠。

`llmgwctl usage` 呼叫 list／roles 等業務端點，`check` 的已簽章 list 也一樣；enforce 下兩者須以已登記、唯讀權限（list、usage、必要 roles，不給 resolve／cooldown）的本機管理 principal 簽章。建議由 root 建置專用應用私鑰，存 `/etc/llmgateway/local-admin-signing-key`，root:root 0600，只有 root 執行的管理命令讀取；一般使用者不得藉 setuid 代理或被賦予讀取權。保護私鑰不能防 root／同 uid 已遭攻破；安裝前核驗權限及以正式 principal 測試。**非 root 的行為（對齊 connectivity「非 root 無權限降為無法確認而不誤報」）**：讀不到管理私鑰時，客戶端就是沒有 signer，送出無簽請求，不做「先簽、失敗再無簽重送」。`llmgwctl check`：`api-local`（本機 `/healthz`，免簽）照舊判 ok/fail；`auth-local` 在非 root 時一律 `todo`「需 root 才能做已簽章檢查」，不試打業務端點、不報 fail；root 時 enforce 的 401／403／503 或 `H[key]` 在未來報 `fail`，warn 不可判定報 `todo`，off 報 ok 並註明「未啟用認證」。結束碼語意不變（0 全好、1 有 fail、2 只有 todo），所以非 root 在 enforce 下最好是 2，不會被誤當故障。`llmgwctl usage`：off／warn 任何使用者照舊可用；enforce 下只有 root（或本人另行核准的受限 signer）可用，非 root 收到 401 時結束碼 1 並印「enforce 下需 sudo 執行」，不印原因細節或金鑰路徑內容。這是**功能迴歸**（現在任何使用者都能跑 usage），已寫入 proposal；若本人不接受，替代選項是把私鑰設為 root:llmgw-ops 0640 給一個維運群組（等於該群組全體可借用該身分），列入未決第 5 項。純存活探針及 install／upgrade／rollback 的 `/healthz` 免簽，舊版按版本規則 list。

名冊 CLI 提議 `llmgwctl auth-registry check <file>`：離線驗 schema、唯一性、墓碑（已撤銷 key 必須仍在且為 revoked），顯示 fingerprint、世代、內容雜湊與變更摘要。`install <file>` 需 root：先跑同一套 check，再以「同目錄暫存檔＋fsync＋rename」原子安裝到 `/etc/llmgateway/auth-registry.json`（root:llmgw 0640）。名冊的增刪與撤銷都走 install，lgwd 不必停。

**reload 機制**（不用 signal、不開 HTTP 控制端點，所以沒有「管理端點本身怎麼認證」的問題）：

- **誰觸發**：lgwd 內一條**背景輪詢執行緒**（沿用 `lgwd/keysync.py:163` `KeySyncTimer` 的背景執行緒先例），每 2 秒 stat 一次名冊檔的 `(mtime_ns, size, ino)`，變了才讀。**請求路徑不觸發重讀**：不像 v5.4 key 檔掛在 `_Current.__getattr__`（`lgwd/main.py:183-196,296-312`）、有人呼叫 service 才檢查。因此服務閒置、或只有 `/healthz` 進來時也會載入；未驗證的請求也無法觸發任何重讀或寫入。
- **載入鎖**：啟動時的首次載入與背景執行緒共用一把 `_registry_lock`（同 `_keys_lock` 的做法），同一時間只有一個載入在跑；請求只在鎖外讀一次目前快照的參照。
- **判斷順序**：解析並驗證 → 比對信任庫的世代水位與內容雜湊：
  - 同世代且內容雜湊相同：**無變更，不是錯誤**（例如 stat 在舊檔、讀到新檔、重裝同一份，或維運把壞名冊還原成已載入的內容）。只更新 stamp，不寫信任庫；status 若已是 ok 就不改，若目前是錯誤分類（前一份壞檔留下的）就改回 ok、把「最近嘗試的雜湊」改成這份，並寫一次，否則回滾壞名冊這個最常見的修復動作之後，告警會停在錯誤直到下一次世代變更。
  - 同世代但雜湊不同，或世代較低：拒絕（`generation_not_increased`）。
  - 較高世代：再驗墓碑（不得移除、改回有效或以新 key ID 重用已撤銷公鑰）→ 在信任庫的同一 transaction 提交新世代水位、墓碑與這份已驗證快照（內容與雜湊）→ 換掉記憶體快照。
- **失敗與退避**：失敗時沿用舊快照，記下失敗的 stamp。**同一個 stamp 不重試**，等檔案再變；若失敗原因是暫時性的（信任庫忙碌或 I/O），以 5 秒起、加倍、上限 5 分鐘退避重試。
- **status 只在狀態變化時寫，另有低頻心跳**：lgwd 以原子 rename 寫 `/var/lib/llmgateway/.auth-registry.status`（llmgw 0644，不含公鑰），內容是已載入的世代與內容雜湊、**最近一次嘗試載入的檔案內容雜湊**、ok 或固定錯誤分類，以及輪詢心跳時刻。「已載入世代／雜湊、嘗試的雜湊、結果分類」任一改變時立即寫；心跳每 60 秒至多寫一次。兩種寫入都只由背景執行緒觸發，與請求量無關，沒有寫入放大。背景執行緒若意外結束，心跳就停：`auth-registry status` 在心跳超過 180 秒時結束碼 1，`llmgwctl check` 的 `auth-local`（root 執行時；非 root 照舊為 todo）報 fail。
- **install 的確認**：install 寫完後輪詢 status 檔最多 30 秒：「最近嘗試的雜湊」與「已載入的雜湊」都等於**自己寫入那份的內容雜湊**、且結果為 ok，才回 0（同世代同內容重裝時 status 已是這個狀態，立刻回 0；若先前是錯誤分類，依上一條會改回 ok）。「最近嘗試的雜湊」是自己這份但結果為錯誤時立即結束碼 1；逾時同樣結束碼 1，並印「名冊已寫入但服務尚未載入／載入失敗（分類）」，不得宣告撤銷完成。`auth-registry status` 只讀這個 status 檔。
- **快照的使用**：新快照生效後，新進請求用新快照；單一請求只讀一次快照，已在途的請求可用舊快照完成。
- **重啟**：與執行中 reload **同一套判斷**，比較對象是信任庫的世代水位與快照雜湊：先載入信任庫快照，再讀名冊檔——同世代同雜湊＝用它（就是快照本身）；同世代異雜湊或較低世代＝拒絕（`generation_not_increased`），沿用快照並告警；較高世代＝驗墓碑後在信任庫提交，再用它；檔案無效＝沿用快照並告警。所以一份同世代、內容被原地改過（例如把已撤銷鍵改回有效而沒加世代）的名冊檔，重啟時與執行中一樣被拒，撤銷保證不取決於有沒有剛好重啟。名冊檔與快照都無效時，enforce 拒絕啟動，warn 告警並放行。因此「壞名冊已裝入後 lgwd 又重啟」不會變成全面中斷。信任庫裡還沒有快照（剛 `auth-store init`）時，名冊檔有效就當第一份提交。

**transaction 與恢復**：多步驟裡只有「rename 名冊檔」和「lgwd 在信任庫的 SQLite transaction」兩個各自原子的動作。兩者之間當機，重啟時 lgwd 重新讀檔載入即可，不需要額外 journal（因此不採用「持鎖＋journal、未完成 transaction 拒絕啟動」的做法）。世代水位誤設過高時（例如誤裝了世代 100，可信的是世代 12），root 執行 `llmgwctl auth-registry reset-generation --confirm <已核驗名冊>`（lgwd 須停止），依序：

1. 解析並驗證該名冊，照 reload 的規則驗墓碑（信任庫每個墓碑都必須在該名冊中為 revoked、不得以新 key ID 重用已撤銷公鑰）；不通過就整個拒絕、什麼都不改。誤裝的那份名冊若撤銷過某把 key，墓碑已寫入，可信名冊也必須把它列為 revoked，否則拒絕——**reset 不能讓已撤銷 key 復活**。
2. 把該名冊原子寫到名冊路徑（rename；內容已相同就跳過）。
3. 先把舊水位與舊快照雜湊寫入審計（`auth-audit.log` 與 journal），再在信任庫的**同一個 transaction** 內把世代水位**與已驗證快照（內容與雜湊）一起**換成這份名冊，並把它的 revoked 列補進墓碑（墓碑只增不減）。只改水位、不換快照是錯的：重啟時名冊檔與快照「同世代異雜湊」，會被拒絕並沿用誤裝的快照，reset 無效且水位與快照矛盾。
4. 啟動 lgwd 驗證：重啟判斷看到「同世代同雜湊」，就用這份名冊；`auth-registry status` 顯示已載入世代 12 與其雜湊。

步驟 2 與 3 之間當機：名冊檔是世代 12、信任庫仍是世代 100，重啟時被當成較低世代拒絕、沿用世代 100 快照並告警（與 reset 前相同，不會更糟），重跑同一條指令即可（冪等）。

`auth-replay status` 列出各 key 的 `H`，以及哪些 key 的 `H` 在未來（不印 nonce）。

**信任庫與防重放庫分開**（選①：墓碑、世代水位、已驗證快照另存 `auth-trust.sqlite`；不選②「recover-store 先搬出，搬不出就以名冊的 revoked 列重建」）。理由：

1. **失效後果不對稱**：nonce 庫可丟棄，遺失後只要把 `H` 設成現在＋60 秒，安全性就恢復。信任資料遺失則會讓已撤銷的鍵可能復活，而且無法從名冊自動確認。
2. **寫入型態不同**：nonce 庫每個請求都寫，量大，最常滿、最常壞。信任庫只在名冊載入時寫，很小。分開之後，最常見的人工恢復（`recover-store`）完全不碰信任資料。
3. **② 會把洞變成作業程序**：它讓最常見的故障把系統帶進「回退防護失效、名冊要人工核驗」的降級狀態。

代價：多一個檔與兩項啟動檢查（兩庫不得是同一檔；兩庫的庫識別必須一致）。兩庫之間沒有需要跨庫原子的寫入：名冊載入只寫信任庫；接受與清除只寫 replay 庫；`recover-store` 只讀目前的名冊快照，取得 key 清單。

信任庫本身損毀時，enforce 拒絕啟動，warn 告警放行。恢復用 root 的 `llmgwctl auth-registry rebuild-trust --confirm <已核驗名冊>`（lgwd 須停止）：先從舊檔盡量搬出墓碑與水位；搬不出的部分，以該名冊中的 revoked 列與其世代重建。輸出、審計與 status 都明示「回退防護已失效，名冊須人工核驗與可信來源一致」。新信任庫沿用 replay 庫的庫識別（replay 庫讀不到時產生新識別，之後須再 `recover-store`）。這是②的做法，但只用在罕見的信任庫損毀，不用在 nonce 庫。

**庫的建立與遺失**（lgwd 怎麼分辨「從沒建過」與「被刪、換碟、還原備份」）：

- **lgwd 在任何模式都不建庫**：兩庫都以 `mode=rw` 開啟，缺檔即失敗。這擋住「`sqlite3.connect(path)` 靜默建出空庫」：空 replay 庫等於所有 `H` 歸零，跳過 recover-store 的保護；空信任庫等於墓碑與世代水位歸零，把目前名冊檔當第一次載入，悄悄拿掉回退防護。
- **只有明確的指令可建庫**：首次啟用（off→warn 之前）由 root 執行 `llmgwctl auth-store init --confirm`（lgwd 須停止）：任一庫已存在就拒絕；建立兩庫、設 `llmgw` 擁有、0600，並在兩庫的 meta 表寫入同一個隨機 128 位元**庫識別**，寫審計。若偵測到這不像首次啟用（`auth-audit.log` 已有紀錄，或名冊檔已存在且世代大於 1），照 `rebuild-trust` 一樣在輸出與審計明示「回退防護已失效，名冊須人工核驗與可信來源一致」（只警示、不擋，因為兩庫都遺失時它也是合法的出路）；兩庫都遺失的事故應優先走 `rebuild-trust`→`recover-store`。之後只有 `recover-store`（建 replay 庫）與 `rebuild-trust`（建信任庫）會建庫。
- **缺檔或識別不符＝遺失**：啟動時任一庫缺檔、打不開或兩庫識別不同，enforce 拒絕啟動並告警（訊息列出是哪一庫、該用 recover-store 或 rebuild-trust），warn 告警並以驗證不可判定照舊處理，off 不檢查。識別不符時無法自動判斷是哪一庫被換過，由維運者依訊息與審計判斷。
- **不得用備份還原任一庫**：備份比現況舊，等於遺失（replay 庫缺 nonce；信任庫缺較新的墓碑）。識別擋不住同一套庫的舊備份，所以這是作業規則，寫入 D5 的回退文件。

時鐘事故的處理已併入「協定與選項」的 `H[key]` 段；不採用「時鐘事故後換全部鑰並隔離 120 秒」的做法：它的換鑰理由只在 nonce 已遺失時成立，與「保留 nonce」自相矛盾。現在換鑰只在 `H[key]` 落在未來（情況 iii，也可改用等待或有審計的 `reset-key-water`）或 replay 庫遺失且無法確認當時時鐘（情況 v）時，且只換受影響的 key；受影響的範圍是「故障期間有流量的 key」，共用時間來源時可能是全部。

## 未決（選項與建議；共六項，與 proposal「建議與代價」表一一對應）

1. 本人：能否接受會合點看到憑證明文，以及 worker 裡任何能讀或借用金鑰的程式仍能拿到其允許的憑證（同 uid 下可能是所有 agent，隔離未證實）？建議：明確接受這兩項殘餘風險再往下做；若不能接受會合點明文，先另設端到端 TLS 與服務端身分，不以此簽章設計聲稱已解決；agent 隔離另議每角色身分。
2. 本人：worker 重用隧道鍵或另發應用鍵？建議：先請 MLP 實測 uid／agent 存取再定；Mac 一律使用獨立應用鍵（硬規則，不改須重新審查）。
3. 本人：人工名冊、跨重建持久鑰、MLP 單向推送、或管理 CA 短期憑證？建議：先測重建頻率與人工分鐘、訂責任人與 401 空窗上限，再選；未有可接受 SLA 不上 enforce。
4. 本人：選 cryptography＋venv／distro、stdlib 驗章、SSHSIG 或 HMAC？建議：先以 cryptography 做固定向量與交付原型，並和 MLP 比較 SSHSIG；選定前不實作、不歸檔。
5. PMO／本人：principal 權限矩陣（含 cooldown 可影響所有訂閱的粗粒度；可選新增每訂閱範圍，需改介面），以及 enforce 下 `llmgwctl usage` 誰能用。建議：最小發權逐項裁定；管理 principal 不含 resolve；usage 在 enforce 下只給 root（sudo），不開維運群組。
6. PMO／本人：warn 觀察期、enforce 啟用與緊急降級時間上限。建議：以客戶清冊驗收而非只看天數；臨時降級到 warn／off 滿 N=1 小時告警，實際 N 與升報對象由本人定。

時鐘事故的處理（`H[key]`、交易範圍、信任庫與防重放庫分開）與非 root 的 check 結果碼是本設計的技術決定，不列為本人裁定；其後果已寫入 proposal。`reset-key-water` 何時使用由授權維運者在事故當下判斷，代價（截存的簽章各可重放一次）已寫入 proposal 的時鐘後果（甲）。每 key 配額的預設值（600）與持續請求率目標（50 筆／秒）是技術參數，MLP 盤點呼叫量後由架構師調整，不列本人裁定；R1 的範圍（含配額與 503）已寫入 proposal。
