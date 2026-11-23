# 實作前置

- [ ] 本設計經非作者審查、MLP 協商、PMO 轉本人裁定；本人同意前不得實作。下列均為提議的 d 級單一 PR，不在本設計開工。每單均需另一隊員同意及一般 review。
- [ ] 本人已選 SSHSIG（`ssh-keygen -Y`）並裁定 worker 沿用隧道鑰（未決 2／4，MyPMO PR #509）；本 change 已依 LGW#242（併 #240 的 F1／F2／F4 與 F3）修訂 spec 線上格式、名冊公鑰格式、SSHSIG 驗章子行程與 D2／D4 完成判準。此修訂經非作者（非 Claude 家族）審查通過後才勾選；在那之前不歸檔。原 F5（cryptography 客戶端安裝）因未選 A 而取消。
- [ ] 歸檔前（或在歸檔的同一 PR 內）必須同步更新兩處 delta 改不到的 Purpose 註記：`openspec/specs/key-vault/spec.md:5`（寫「`不做應用層認證`…不變」，該 Requirement 歸檔後已改名）與 `openspec/specs/connectivity/spec.md:6`（寫探針「不變」，本 change 已改為 `/healthz`）；未改不得歸檔。

# 依存順序及驗收

- [ ] D1：健康端點與雙版本探針（無依存）。完成：`/healthz` 無敏感資料；本機、隧道、install／upgrade／rollback 探針按服務版本工作；部署器依安裝版本的明確能力旗標設定 old/new（版本切換時一起原子更新）；old 時僅 healthz 404 才退回 list，new 的非 200 與 old 的其他錯誤不退回；假的新舊服務回退及 404／401／503 測試通過，4710/4711 與 #221 行為不變；同一 PR 更新 `openspec/specs/connectivity/spec.md:6` 的 Purpose 註記（探針改為 `/healthz`），歸檔前置那條才算對這處完成。架構師審：是，涉及 #221 的跨 repo 通道健康與回退契約。D1 契約（REPORT-246-arch）H-5 第 3 條已由 #247 J-1 修訂為「`/healthz` 在三種模式都不受 D2 framing 約束」，D1 本身的實作不受影響（見 D2）。
- [ ] D2：簽章規格、名冊、replay 驗證與 off/warn/enforce 伺服端（依 D1、本人 11-23 核准依賴與 wire format）。依 #247 拆成下列 6 個可獨立合併的 d 級 PR；每個 PR 都「只合這一步，main 不會更糟」，都只用標準函式庫（`pyproject.toml` 的 `dependencies` 維持空），預設 off 不改行為，架構師審（安全邊界）。共通：時間一律經注入的 `lgwd.clock.Clock`（整數秒＝`floor(clock.now().timestamp())`，新程式不直接呼叫 `time.time()`）；日誌不得含標頭值、簽章、nonce、query、body、公鑰全文；每個 PR 報告附 C-編號對測試名稱表、變異測試清單（改了哪裡、哪個測試轉紅，至少 3 處）、skip 數與原因、全套 `python3 -m unittest discover -s tests` 結果；實作與審查用不同 `(provider, model)`。設計依據見 design「名冊檔格式」「SSHSIG 驗章」「防重放交易」「預驗章請求框架」「啟動、結束碼與 warn 的恢復」「D2 實作切分」。並行：PR-1、PR-2、PR-3 可同時開工；PR-5 依 PR-2；PR-4 依 PR-2＋PR-3；PR-6 依全部與 D1；PR-1、PR-6 須在 D1 的 `lgwd/http.py` 改動合併後 rebase。**D1 契約修訂**：REPORT-246-arch 的 H-5 第 3 條「`/healthz` 仍受 D2 的 framing 前置檢查約束」改為「`/healthz` 在三種模式都不受 framing 約束、不佔並發名額」（#247 J-1；理由見 design「預驗章請求框架」）；H-5 其餘各條不變。
  - [ ] PR-1　預驗章 HTTP framing（新增 `lgwd/framing.py`；改 `lgwd/http.py`：`make_api_server(service, host, port, *, gate=None)`，`gate` 只需 `.mode`，`None`＝off）。規模約 3–4 檔、程式 150–250 行、測試 300–500 行；作者 impl 25–30。
    - C-101 framing 只對五條業務路由、只在 warn／enforce 生效；`/healthz` 三模式都走 D1 行為；off 用 `self.path` 與 `_read_json`，與 D1 逐 byte 相同。
    - C-102 request-target 取 `requestline` 第二段原文（stdlib 會改寫 `//`）；只收 origin-form（`/` 開頭非 `//`、ASCII 0x21–0x7E、無 `#`）；拒絕絕對 URI、authority-form、`*`、HTTP/0.9；路由 path 取自同一段原文。
    - C-103 `Content-Length` 至多一個且 `^[0-9]+$`、GET 不得非零、超過 1 MiB 照現行 413（各模式相同）；有 `Transfer-Encoding` 即拒。
    - C-104 `Content-Length`、`Transfer-Encoding`、`Host`、五個 `X-LGW-*` 以 `get_all` 判各至多一次，不分大小寫。
    - C-105 warn／enforce 業務請求從接受連線到 body 讀完共用 10 秒總期限（單調時鐘截止，不是每次 recv 逾時）；逾時 enforce 503、warn 記不可判定並關閉；off 不設新逾時。
    - C-106 讀 body＋驗章階段取號誌（上限 32、等 1 秒）；取不到 enforce 503、warn 不可判定並照舊處理；`/healthz` 不取。
    - C-107 framing 不合 enforce 回 401 `unauthorized`（不進 service），warn 記 would-deny `framing` 並照舊處理。
    - C-108 off 差分測試：至少 25 筆原始請求語料（重複 CL／TE、`//` 路徑、絕對 URI、帶 body 的 GET、壞 CL、HTTP/0.9）在 off 下的回應 bytes 與合併前逐筆相同。
    - 測試重點：負向含 slowloris（每 0.5 秒 1 byte）在期限切斷、第 33 個並發取不到名額、enforce 拒絕時 service 呼叫數 0、enforce 下帶 TE／重複 X-LGW 的 `/healthz` 仍回 D1 的 16 bytes；變異：改用 `self.path`、拿掉 TE 檢查、期限改成每次 recv、off 也套 framing。架構師審：原文判讀、off 差分涵蓋度、`/healthz` 豁免、總期限。
  - [ ] PR-2　名冊 parser＋公鑰驗證 F1（新增 `lgwd/auth_registry.py`：`validate_ed25519_pubkey`、`parse_registry`、`load_registry_file`、`content_hash`、`check_tombstones`、型別 `Registry`／`KeyEntry`；向量 `tests/vectors/ed25519_pubkeys.json`）。無依存；規模約 2–3 檔、程式 250–350 行、測試 400–600 行；作者 impl 30–35。
    - C-201 單一驗證函式：blob 恰為 `string "ssh-ed25519"`＋`string(32)`、無多餘 bytes；y（去符號位元）< 2^255−19；y∈{1, 2^255−20} 時符號位元 0；不在 8 個小階點規範編碼常數表；拒 RSA、ECDSA、`sk-*`、`*-cert-v01@openssh.com`、長度不符；錯誤分類為固定字串、不含輸入值。
    - C-202 小階常數表以 test-only 純 Python 曲線運算獨立推導 8 點並逐一比對（`lgwd` 不得 import 該碼）。
    - C-203 `parse_registry` 照 design「名冊檔格式」schema 1 嚴格解析（未知鍵、重複 JSON 鍵、權限欄形狀與一致性、ID 格式、`generation` 範圍、`public_key` 字串的 base64 往返）。
    - C-204 唯一性比驗證後 32 bytes：同 key ID 重複、同 bytes 兩個 key ID、同 bytes 兩個 principal 都整份拒絕。
    - C-205 `check_tombstones`：每個墓碑 `(key_id, bytes32)` 必須以同 ID、同 bytes 標 revoked；新 ID 用到墓碑 bytes 即拒。
    - C-206 `content_hash(raw)`＝`"sha256:"`＋原始 bytes 小寫 hex。
    - C-207 檔案檢查：`O_NOFOLLOW`、一般檔、擁有者 uid（預設 0，僅建構子參數可改）、群組與其他人不可寫、≤1 MiB；不符分別為 `insecure_file`／`too_large`。純函式部分不 import `lgwd.http`／`lgwd.main`／`sqlite3`。
    - 測試重點：正向含輪替多鑰與 revoked 列；負向含單位元、y＝1＋P、x＝0 且符號位元 1、order-2、order-4／8 各點、原型六個負向向量、cert、多／少 1 byte、未知鍵、權限欄矛盾、同點換編碼繞墓碑；變異：逐條拿掉 C-201 五條規則、唯一性改比 base64 字串、墓碑只比 key ID。架構師審：唯一函式、bytes 比對、常數表交叉驗、未知鍵嚴拒。
  - [ ] PR-3　SQLite 基礎＋防重放庫（新增 `lgwd/auth_db.py`、`lgwd/auth_replay.py`、`lgwd/auth_paths.py`）。無依存；規模約 4–5 檔、程式 400–550 行、測試 600–900 行；作者 impl 35–40。
    - C-301 `open_rw` 只用 `file:<path>?mode=rw` URI，缺檔丟 `StoreMissing` 且檔案仍不存在；模組內除 `create_stores` 外不得有不帶 `mode=rw`／`mode=ro` 的 `sqlite3.connect(`（靜態掃描測試）。
    - C-302 每次開啟設 `journal_mode=WAL`、`synchronous=FULL` 並讀回驗證，`isolation_level=None`，交易一律明確 `BEGIN IMMEDIATE`。
    - C-303 `create_stores(trust_path, replay_path)` 建兩庫 schema、在兩庫 `meta` 寫同一個 128 位元隨機庫識別；任一已存在或兩路徑 `samefile` 即拒。lgwd 永不呼叫（給測試與 D5 `auth-store init`）。
    - C-304 行程內鎖等待與 SQLite 忙碌重試到同一期限（預設 5 秒）才算 `busy_timeout`；錯誤以**訊息字串**分類（design「錯誤分類」表；不用 `sqlite_errorcode`，兼容 Python 3.10），忙碌以外立即 `unavailable(kind)`，表外訊息＝未知故障。
    - C-305 `accept`：取鎖並 `BEGIN IMMEDIATE` 後才讀一次 `now`；依序 `|now−ts|≤60`（否則 EXPIRED）→ `ts>H[key]`（否則 WATER）→ 已存在（REPLAY；唯一約束衝突亦同）→ `ts≥now−65` 列數達 `per_key_max`（RATE_LIMITED）→ INSERT、COMMIT（ACCEPTED）；非 ACCEPTED 一律 rollback。
    - C-306 `retry_after`＝`min(126, max(1, 計入配額的最小 ts＋66−now))`。
    - C-307 `purge_once`：自己的交易內讀 `now`，每批至多 5000 列刪 `ts<now−65`，同交易 `H[key]=max(H[key], 被刪最大 ts)`；批間放鎖，同一輪清到沒有為止；`PurgeTimer` 每 30 秒，啟停比照 `KeySyncTimer`。
    - C-308 不變式：庫中每列 `ts>H[key]`；每次清除後 `H[key] < 該次 now−65`。
    - C-309 `acquire_store_lock`：`O_RDWR|O_CREAT, 0o600`＋`flock(LOCK_EX|LOCK_NB)`；被持有丟 `LockBusy`、開不了丟 `LockUnavailable`（型別不同）。
    - C-310 `auth_paths` 由 `auth_state_dir` 推導兩庫、鎖檔、status、`allowed_signers.<世代>`、`sshsig/`、`auth-audit.log`（lgwd 與 D5 共用）；`now` 一律經注入 Clock。
    - 測試重點：邊界向量（`now=ts+60` 收、`+61` 拒、`ts−60` 收、`ts−61` 拒、`ts=H` 拒、`H+1` 收、同秒不同 nonce 收、`+65` 不清、`+66` 可清）；32 執行緒同 (key, nonce) 恰 1 次 ACCEPTED；**並行「清除 vs 到期前一刻重放」1000 輪每輪至多一次**；32 執行緒各 500 個不同 nonce 零 unavailable、等鎖 p99<1 秒；已滿時重放回 REPLAY 不回 RATE_LIMITED；清除落後的過期列不計入配額；重啟後重放；`PRAGMA max_page_count` 造滿、改壞檔頭、另一連線持寫鎖 4 秒（須成功，不得 unavailable）與 6 秒（busy_timeout）；每個訊息分類都以真實觸發測到。變異：配額排在重放前、清除門檻改 `now−60`、交易外讀 `now`（以取鎖與讀時鐘之間推進的 FakeClock 構造）、busy 立即 unavailable。架構師審：交易順序、單一 `now`、清除與 H 同交易、訊息分類、無 `connect(path)`。
  - [ ] PR-4　信任庫＋背景 reload＋status 檔（新增 `lgwd/auth_trust.py`、`lgwd/auth_reload.py`）。依 PR-2、PR-3；規模約 3–4 檔、程式 400–500 行、測試 600–900 行；作者 impl 35–40。
    - C-401 唯一決策函式 `decide(file_result, trust_state)`：無快照且檔有效＝COMMIT_FIRST；同世代同雜湊＝UNCHANGED；同世代異雜湊或較低世代＝REJECT(`generation_not_increased`)；較高世代先驗墓碑，過＝COMMIT、不過＝REJECT(`tombstone_violation`)；檔無效＝REJECT(分類)；檔與快照皆無效＝NO_REGISTRY。啟動 `load_initial` 與執行中 `tick` 都只呼叫它。
    - C-402 世代水位、墓碑（只 `INSERT OR IGNORE`、從不 DELETE）、快照（原始 bytes＋雜湊）同一交易；交易完成後才換記憶體快照，再呼叫 `on_snapshot`。
    - C-403 背景執行緒每 2 秒 stat `(mtime_ns,size,ino)`，變了才讀；失敗記 stamp、同 stamp 不重試；只有 `trust_unavailable`／`io_error` 這類暫時性錯誤退避 5→10→…→300 秒；請求路徑不觸發讀檔；提供 `extra_ticks`（給 PR-6 掛 allowed_signers 重產與 `ssh-keygen` stat）。
    - C-404 status 檔照 design 固定格式（一行 JSON、`schema` 1、0644、原子寫、不含公鑰與 key ID）；只在 `loaded_*`／`attempted_hash`／`result` 改變時寫，心跳至多每 60 秒；UNCHANGED 而目前為錯誤時改回 ok 並把 `attempted_hash` 設為這份。
    - C-405 `result` 白名單：`ok`、`parse_error`、`invalid_key`、`duplicate`、`insecure_file`、`too_large`、`tombstone_violation`、`generation_not_increased`、`io_error`、`trust_unavailable`；這是給 D5 的介面，改動經架構師。
    - C-406 請求在鎖外取一次不可變快照參照，單一請求全程用同一份。
    - 測試重點：閒置（無請求）安裝新世代 4 秒內載入並寫 status；同世代同內容不寫 status；壞檔→還原為已載入內容→status 回 ok；較高世代刪墓碑／改回有效／新 ID 重用皆整份拒絕且快照不變；**同世代內容被改動：執行中與重啟後都拒絕、沿用快照、該 key 仍不在有效集合**；較低世代拒；壞名冊後重啟沿用快照；同一壞 stamp 不重讀（計數）；退避序列（假時鐘）；輪詢執行緒停止後心跳不再前進。變異：`load_initial` 改用另一套判斷、墓碑整表覆寫、UNCHANGED 不改回 ok、失敗後推進 stamp。架構師審：同一 `decide`、status 寫回、墓碑只增、status 格式。
  - [ ] PR-5　SSHSIG 驗章（新增 `lgwd/sshsig.py`；向量 `tests/vectors/sshsig_v1.json`＋產生說明 README）。依 PR-2；可與 PR-3／PR-4 並行；規模約 3–4 檔、程式 400–550 行、測試 600–900 行（含假 `ssh-keygen`／`prlimit` 腳本）；作者 impl 35–40。
    - C-501 預解析：base64url 無 padding且往返相同、長 240、解得 180 bytes；魔數 `SSHSIG`、版本 1、公鑰 blob 與 entry 逐 byte 相同、namespace `llmgateway`、reserved 空、`sha512`、`ssh-ed25519`、64 bytes 且 S（後 32 bytes LE）<L、無多餘 bytes；不符丟 `Reject`、**不 fork**（spawn 計數 0）。
    - C-502 allowed_signers：只寫有效 key、每行 `<key ID> namespaces="llmgateway" ssh-ed25519 <base64>`、`allowed_signers.<世代>` 同目錄暫存＋fsync＋rename、0600；**rename 前讀回自檢**（行數＝有效 key 數、逐行格式、key ID 與公鑰與快照一一相同），不符不換檔並判 `verifier_unavailable`；換上後記 `(dev,ino,size,mtime_ns)`，每次驗章前 stat 比對，不同＝503＋告警；`SignersFile` 引用計數，舊檔引用歸零才刪。
    - C-503 子行程 argv 固定 `[/usr/bin/prlimit, --cpu=2, --as=268435456, --nofile=32, --, <sshkeygen>, -Y, verify, -f, <signers>, -I, <entry.key_id>, -n, llmgateway, -s, <sigfile>]`（數值以實測為準並寫入報告）；`shell=False`、`close_fds=True`、環境恰為 `{LC_ALL=C, PATH=/usr/bin:/bin, HOME=<私有目錄>}`、訊息走 stdin、stdout／stderr 各至多讀 4 KiB；簽章檔 `mkstemp` 於 0700 目錄、`finally` 刪除、啟動清殘檔；不用 `preexec_fn`。
    - C-504 分類是唯一的 401 出口：通過＝exit 0 **且** stdout 恰一行 Good 行、其 key ID 等於 entry、fp 等於 lgwd 對快照 blob 自算的 `base64(sha256(blob))`（exit 0 單獨不算數）；401＝exit 255 **且** stderr 整份等於 design 判定規則的兩種形式之一、stdout 整份為空或恰為 `Could not verify signature.`、該行兩串流合計至多一次；其餘一律 UNAVAILABLE（含只有 `Could not verify signature.` 的兩種）；stderr 原文不進日誌。
    - C-505 號誌預設 16（≤32），等號誌與執行合計 2 秒，逾時 SIGKILL 行程群並回收，判 UNAVAILABLE。
    - C-506 啟動自測：路徑存在可執行、擁有者 uid（預設 0，僅建構子可改）、群組與其他人不可寫；同目錄 `ssh -V` 版本 ≥9.6（讀不到記 `openssh_version_unknown`，以自測為準）；以固定向量真的 exec 四個案例——正確簽章＝通過、R 改 1 bit＝401、principal 不在自測 allowed_signers＝503、自測 allowed_signers 損壞＝503；任一不符即自測失敗（**字樣守門**：10.2p1 等版本字樣若變，結果是失敗而不是誤判 401）；`recheck_if_changed()` 於 `(mtime_ns,size,ino)` 改變時重跑，失敗後 `healthy=False` 直到再通過。
    - C-507 向量依 design「向量產生程序」產生，私鑰不進 repo 與任何輸出。
    - 測試重點：真 `ssh-keygen` 測試 `skipUnless` 版本 ≥9.6（skip 列報告）；不 fork 的負向（別 namespace、別把鑰冒用、長度 239／241、padding、非規範 base64、S＋L 延展、多 1 byte）；假 `ssh-keygen` 分類（逾時、exec 失敗、exit 255 空 stderr、只有 `Could not verify signature.`、表外 stderr、9.6 單行＋多一行表外字樣、exit 1、SIGSEGV、exit 0 但 key ID 或 fp 不符）全部 UNAVAILABLE、沒有一個是 401；9.6p1 形式（stderr 一行＋stdout `Could not verify signature.`）與 stderr 兩行形式各自為 401；`Could not verify signature.` 同時出現在兩串流＝503；逾時與例外後暫存目錄為空；argv／env 逐項斷言（假 prlimit 記錄）；版本 9.5 自測失敗；改 mtime 重跑自測；allowed_signers 自檢不符與外部改檔皆 503。變異：拿掉 S<L、401 改子字串比對、通過只看 exit 0、`-I` 改取請求 key ID、拿掉 `finally` 刪檔、拿掉 allowed_signers 讀回自檢。停損：`prlimit` 不可用，或本機 `ssh-keygen` 輸出不屬已知形式時停下回報，不擴充分類表。架構師審：argv／env／`-I`／`-n` 來源、唯一 401 出口、自測四案例、暫存檔、無 `preexec_fn`。
  - [ ] PR-6　模式整合（新增 `lgwd/auth.py`；改 `lgwd/http.py`、`lgwd/main.py`、`lgwd/errors.py`；慢測腳本）。依 PR-1～PR-5 與 D1；規模約 5–7 檔、程式 450–650 行、測試 1000–1500 行；作者 impl 35–40；若超過約 2000 行，拆成「gate＋apply（不接 http）」與「http／main 接線＋端到端測試」兩個 PR。
    - C-601 順序：原始 request line → 路由 → `/healthz` 直接回 D1 回應（不經 gate、不取號誌、不記認證日誌、不計配額）→ 業務路由才進 framing → gate → service。
    - C-602 gate 流程：五個標頭齊全且版本恰為 `1` → key ID 格式 → 快照查表（未知 `unknown_key`、撤銷 `revoked`）→ timestamp 十進位無前導零、nonce 為 32 bytes base64url → 交易外粗篩 ±60 → 預解析 → 組八行（第 7 行＝C-102 原文、第 8 行＝實際 body 的 sha256）→ `verify` → `accept`；回 `Verdict{outcome, reason, principal, retry_after}`。
    - C-603 模式對應只在 `apply` 一處：PASS 繼續；DENY enforce 401、warn would-deny 後繼續；RATE_LIMITED enforce 429＋`Retry-After`、warn would-deny 後繼續；UNAVAILABLE enforce 503 `auth_unavailable`、warn 不可判定後繼續；off 不呼叫 gate。warn 的 PASS 照常 INSERT。
    - C-604 enforce 的 401 body 對所有 reason 逐 byte 相同，不加 `WWW-Authenticate`。
    - C-605 啟動（warn／enforce；off 全略過）依 design「啟動、結束碼與 warn 的恢復」：flock → 兩庫 → `load_initial` → 自測 → bind；`LockBusy` 任何模式結束碼 78；enforce 其餘失敗結束碼 78 且埠未 bind、訊息指名哪一庫及該用 recover-store 或 rebuild-trust；warn 告警並標不可判定、鎖與兩庫要重啟才重試；`H[key]` 在未來的 key 只記事件。
    - C-606 設定依 design「設定」：CLI＞環境變數＞預設；不合法（含未知模式）結束碼 2。
    - C-607 認證日誌只記 `mode`、路由模板、`outcome`、白名單 `reason`、已驗證 principal；同鍵每 60 秒至多一行附計數；off 只記啟動一行 `auth mode=off`。
    - C-608 `Verdict.principal` 已算出但不傳進 service、不寫稽核（D3 掛點）。
    - C-609 三模式下 `GET /healthz`（無簽、有簽、未知簽章、重複 X-LGW、TE）回應 bytes 與 D1 相同（扣除 `Date`／`Server`）。
    - 測試重點：五路由 enforce 合法簽章 200／204；warn 未簽舊客戶端照舊並有 would-deny；off 不碰鎖、庫、名冊、`ssh-keygen`（不存在的路徑＋spawn 計數 0）；無簽、錯簽、改 query、改 body、POST 簽章送 GET、重複標頭、過期、撤銷、重放（含重啟後）皆 401 且 service 呼叫數 0；429＋`Retry-After` 正確、等那麼久重簽可過；**key A 以並發上限灌量時 key B 照常（無 429／503）**；被 429 拒絕的簽章配額空出後重送至多一次；**enforce 任一庫缺檔或識別不符拒絕啟動（78）且檔案仍不存在**；第二個 lgwd 拿不到 flock 拒絕啟動、第一個照常；鎖檔開不了 enforce 拒絕、warn 照舊；滿碟或壞檔 enforce 503、warn 照舊 200；`ssh-keygen` 缺失或版本 9.5 enforce 拒絕、warn 照舊、off 照常；名冊輪詢在只有 `/healthz` 流量時仍載入；時鐘五情況（伺服器落後、伺服器跳未來再校正零 503 且不需換鑰、主從同跳未來時**多把 key 同時受影響**且其他 key 照常、聯合回撥、庫遺失時 enforce 503／拒絕啟動）與 warn 期間時鐘出錯後切 enforce 只拒絕 `H` 在未來的 key；日誌不洩密（收集全部日誌搜測試鑰 base64、簽章、nonce、query 值、body、env 值皆 0 筆）。**持續率**：預設 skip、`LGW_SLOW_TESTS=1` 才跑的 10 分鐘測試（並發 32、完整 HTTP＋`prlimit`＋`ssh-keygen`＋SQLite 路徑，接受 ≥50 req/s、零假 503、等鎖 p99<1 秒，附 load average 與機器規格），另有一律執行的 30 秒短版。變異：warn 遇 UNAVAILABLE 改回 503、`/healthz` 移到 gate 後、拒絕啟動前先 bind、401 body 帶 reason。架構師審：模式對應唯一一處、拒絕啟動不 bind、warn 不改業務結果、`/healthz` bytes 相同、D3 掛點。
  - 停損（六個 PR 共通；遇到就停下回報架構師，不自行改設計）：高並行出現假 503 且調號誌與期限後仍在（不放寬判準、不改 `synchronous=NORMAL`）；10 分鐘持續率 <50 req/s 且調號誌與期限後仍不達標（不改方案）；`prlimit` 不可用或 `ssh-keygen` 輸出不屬分類表已知形式（不擴充分類表）；需要新增 Python 相依、`preexec_fn`、shell，或讓 lgwd 建庫；需要真鑰或連真機（一律臨時產生的金鑰）；需要改 unit 檔（屬 D5，見 J-5）。
  - 不在 D2、移到 D5 驗的項目（#247 G-6）：`auth-store init` 之後以 llmgw 啟動能取得鎖；輪詢心跳逾時由 `auth-registry status` 與 `check` 報出（D2 只驗心跳停止）；`llmgwctl check` 把 `H[key]` 在未來的 key 報為 fail（D2 只驗啟動事件）；replay 庫遺失後以 `recover-store` 恢復（D2 只驗 lgwd 的 503／拒絕啟動）。
- [ ] D3：principal 權限與現有 policy 疊加（依 D2、權限矩陣裁定）。完成：各操作、agent、role 負向測試，resolve 無 role 的限制記錄，未配置預設拒絕；warn 記 would-deny；成功 resolve／cooldown 稽核 nullable principal，舊 DB／歷史 get 保留，未驗證不記自稱身分；同步更新舊認證註解（`lgwd/http.py:12-13`、`lgw/client.py:31,93`）與 key-vault Purpose 的 v5 註記（`openspec/specs/key-vault/spec.md:5`，delta 機制改不到）。架構師審：是，授權邊界。
- [ ] D4：`lgw` 客戶端 signer、錯誤與版號（依 D2）；完成：五方法簽字節、禁止 redirect／降級、舊服務相容與固定向量、0.2.0 發版準備；signer 呼叫本機 `ssh-keygen -Y sign -n llmgateway`（絕對路徑、argv 陣列、訊息走 stdin、`LC_ALL=C`），把 armor 轉成固定 240 字元的 `X-LGW-Signature`，不新增 Python 相依、不做 `lgw[auth]` extra；支援私鑰檔與「公鑰檔＋ssh-agent」兩種，私鑰有密語時不提示、直接失敗；Mac 與 worker 映像的 OpenSSH 版本與 `-Y sign` 實測列入報告（客戶端只需 `-Y sign`，最低版本 9.6 只約束伺服器）；401 可用 Date 提示可能時差，不自動調時；429 呈現 `Retry-After`，client 不自動重試，呼叫方重試時重簽新 nonce。`llmgwctl usage` 與簽章 check 使用專用本機唯讀 principal、root 擁有 0600 私鑰且無 resolve／cooldown 權限；讀不到私鑰時不簽、不做失敗後無簽重送；非 root 的 `check` 的 `auth-local` 為 todo（不報 fail），enforce 下非 root 的 `usage` 結束碼 1 並提示需 root，off／warn 下照舊可用；install／upgrade／rollback 的健康探針免簽。私鑰不進輸出。架構師審：是，公開介面與密碼學互通。
- [ ] D5：操作介面、名冊輪替／撤銷及回退文件（依 D1–D4）。完成：`llmgwctl auth-registry check|install|status` 離線驗格式、公鑰（呼叫 D2 的同一個驗證函式，不另寫）、唯一性與撤銷墓碑，顯示 fingerprint、世代遞增、防回退，install 原子寫檔並等 status 檔的「最近嘗試的雜湊」與「已載入的雜湊」都等於該檔內容雜湊且結果為 ok 才回 0（嘗試的是該檔但結果錯誤時立即結束碼 1；逾時結束碼 1；兩者都不得宣告撤銷完成），測試含**同世代同內容重裝立即回 0**、壞檔 install 結束碼 1、壞檔後重裝原內容回 0；root 限制、lgwd 須停止（flock 檢查）的 `auth-store init --confirm`（首次建立兩庫、共用的庫識別與鎖檔，任一庫已存在就拒絕；偵測到審計紀錄或世代大於 1 的名冊時明示「回退防護已失效」；測試含已存在時拒絕、建出的兩庫與鎖檔擁有者為 llmgw 且 0600、非首次時印出警示）；root 限制、lgwd 須停止（flock 檢查）的 `auth-registry reset-generation --confirm <已核驗名冊>`（照 reload 規則驗墓碑，不通過就什麼都不改；先原子寫名冊檔，再在信任庫**同一個 transaction** 內把世代水位**與已驗證快照（內容、雜湊）一起**換成該名冊並補墓碑，墓碑只增不減；舊水位與舊快照雜湊進審計），測試含**誤裝高世代名冊（例如 100）後以可信低世代（例如 12）reset，重啟後載入世代 12、status 顯示世代 12 與其雜湊、只在世代 12 有效的 key 通過、只在世代 100 出現的 key 回 401、原有墓碑仍在**；誤裝世代撤銷過、可信名冊卻列為有效的 key 使 reset 整個拒絕且信任庫不變；寫名冊檔後、transaction 前當機，重啟沿用舊快照並告警、重跑同一指令成功（冪等）；`auth-registry rebuild-trust --confirm <已核驗名冊>`（信任庫損毀時：先搬出墓碑與水位，搬不出以名冊 revoked 列重建，並明示「回退防護已失效，名冊須人工核驗」）、`auth-replay recover-store --confirm`（只重建 replay 庫，既有但損毀或識別不符的 replay 庫先改名為 `.broken-<時刻>` 再建新庫、寫入信任庫的庫識別，把各 key 的 `H` 設為現在＋60 秒；信任庫缺失時拒絕，要先 rebuild-trust）、`auth-replay reset-key-water --confirm (--key <id>…|--all-future)`（批次把 `H` 設為 now−65，執行前印出目前伺服器時間與各 key 的 `H` 供確認時鐘已校正，審計記誰、何時、哪些 key、新舊值，並在輸出明示代價：故障期間被截存、紀錄已清除的簽章各可重放一次；重設期間 lgwd 停止、全部 key 暫停），寫入 CLI 的審計都附加到 `/var/lib/llmgateway/auth-audit.log`（root:root 0600）與 journal，文件寫明它是歸責紀錄、root 改得了，以及唯讀的 `auth-replay status`（列出 `H[key]` 在未來的 key），均含審計與故障測試；**`recover-store` 後墓碑仍在、刪掉已撤銷 key 的較高世代仍整份拒絕**的測試；另驗由 D2 移來的項目（#247 G-6）：`auth-store init` 之後以 llmgw 啟動 lgwd 能取得鎖、`auth-registry status` 與 `check` 報出輪詢心跳逾時（>180 秒）、`check` 把 `H[key]` 在未來的 key 報為 fail、replay 庫遺失後 `recover-store` 恢復；CLI 一律經 `lgwd/auth_paths.py` 推導路徑、讀 D2 固定格式的 status 檔。unit 檔改動（#247 J-5）：`RestartPreventExitStatus=78`，以及依 D2 持續率測試與正式主機實測定值的 `TasksMax`／`MemoryMax`／`LimitNOFILE`，在本單或 V-0 前另開 d 單，不在 D2。可按離線假環境重現首次 `auth-store init`、新鍵匯入、撤銷、健康檢查、warn→enforce 及手動回退，明示撤銷延遲與風險，並寫明「不得用備份檔還原任一庫，一律走 recover-store／rebuild-trust」；部署檢查不印秘密。架構師審：是；雖有文件，工具亦影響名冊信任、撤銷和安全回退。
- [ ] 跨 repo 前置：MLP 另行排定 sidecar signer、capability 健康探針及客戶清冊遷移（依 D1、D2、D4；不由 LGW PR 冒稱完成）；MLP 定 sidecar 的 namespace／audience（不得是 `llmgateway`），交付互拒 SSHSIG 向量；回答 design「給 MLP PM 的問題」8（隧道公鑰取得、重建換鑰時序、`ForwardAgent no`）；聯合固定向量與假服務正負案例通過。架構師審：是，跨服務介面。
- [ ] D6：離線整合驗收（依 D1–D5 與跨 repo 前置）。完成：所有已知呼叫方在 fake loopback 完成三模式、簽章／授權／輪替／重放／回退矩陣，無隧道中斷與洩密；未遷移客戶清單為空。架構師審：是，enforce 準備判定。
- [ ] V-0 前置：正式主機的 OpenSSH 實測（真機操作；**需本人另行授權**，由 PMO 執行；在 D2 的 PR-5 合併後、任何 warn／enforce 啟用前做）。背景：Gateway 正式主機為 Ubuntu 26.04.1、Python 3.14.4、OpenSSH_10.2p1（PMO 11-23 實測版本字串），design 分類表只有 9.6p1 實測與 10.3p1 Mac 取樣（未分串流）。目的：(1) 取得 10.2p1 的 `ssh-keygen -Y verify` 各情況 rc／stdout／stderr 原文，補進 design 分類表的 10.2p1 欄；(2) 記錄 10.2p1 對小階公鑰偽造簽章與 S＋L 延展簽章的行為（只作紀錄：F1 名冊拒絕與預解析 S<L **不論結果都保留**，兩邊都擋）。腳本規格：只用 `/usr/bin/ssh-keygen`、`/usr/bin/ssh` 與 Python 標準庫；在 `/tmp` 下 `mkdtemp`（0700）建臨時目錄，臨時金鑰只在其中產生，`HOME` 指向它、環境只有 `LC_ALL=C`／`PATH`／`HOME`（不帶 `SSH_AUTH_SOCK`）；不讀、不列、不碰任何既有金鑰或 `/etc`、`/var/lib/llmgateway`、`~/.ssh`；不連網、不動服務；輸出把臨時路徑換成 `<TMP>`、只含臨時金鑰的 fingerprint；`finally` 刪掉整個臨時目錄並印出 `tempdir removed: True`。以 `python3 -I v0probe.py` 執行（不要在 repo 或含其他 `.py` 的目錄下執行），把完整輸出交架構師。下列腳本已在 worker 的 OpenSSH 9.6p1 跑過（C2、C3 為 401 形式、C9 與四個 F1 案例 rc=0，與 REPORT-242 一致），可照貼：

    ```python
    # LGW V-0 前置：ssh-keygen -Y 分類字樣與 F1 小階公鑰行為（唯讀；只用臨時金鑰；用完刪）。
    # 執行：python3 -I v0probe.py > v0probe.out ; 不讀任何既有金鑰、不連網、不寫 /tmp 臨時目錄以外的地方。
    import base64, hashlib, os, shutil, struct, subprocess, tempfile
    KG, SSH = '/usr/bin/ssh-keygen', '/usr/bin/ssh'
    P = 2**255 - 19
    L = 2**252 + 27742317777372353535851937790883648493
    def s(b): return struct.pack('>I', len(b)) + b
    def armor(blob):
        e = base64.b64encode(blob).decode()
        return '-----BEGIN SSH SIGNATURE-----\n' + '\n'.join(e[i:i+70] for i in range(0, len(e), 70)) + '\n-----END SSH SIGNATURE-----\n'
    def dearmor(text): return base64.b64decode(''.join(text.strip().splitlines()[1:-1]))
    def sshsig(pk, sig64): return b'SSHSIG' + struct.pack('>I', 1) + s(s(b'ssh-ed25519') + s(pk)) + s(b'llmgateway') + s(b'') + s(b'sha512') + s(s(b'ssh-ed25519') + s(sig64))
    def h_even(R, A, msg):
        M = b'SSHSIG' + s(b'llmgateway') + s(b'') + s(b'sha512') + s(hashlib.sha512(msg).digest())
        return (int.from_bytes(hashlib.sha512(R + A + M).digest(), 'little') % L) % 2 == 0
    t = tempfile.mkdtemp(prefix='lgw-v0-', dir='/tmp'); os.chmod(t, 0o700)
    ENV = {'LC_ALL': 'C', 'PATH': '/usr/bin:/bin', 'HOME': t}
    def red(x): return x.replace(t, '<TMP>')
    def run(tag, args, inp=b''):
        r = subprocess.run(args, input=inp, capture_output=True, env=ENV, timeout=10, cwd=t)
        print(f'{tag}\n  rc={r.returncode}\n  stdout={red(r.stdout.decode(errors="replace"))!r}\n  stderr={red(r.stderr.decode(errors="replace"))!r}')
    try:
        run('version', [SSH, '-V'])
        for k in ('k', 'k2'):
            subprocess.run([KG, '-q', '-t', 'ed25519', '-N', '', '-C', 'tmp', '-f', f'{t}/{k}'], check=True, env=ENV, capture_output=True)
        msg = (b'LGW-HTTP-SIG-V1\nllmgateway\nv0-key\n1795392000\nAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8\nGET\n/v1/subscriptions\n'
               + hashlib.sha256(b'').hexdigest().encode() + b'\n')
        sig = subprocess.run([KG, '-Y', 'sign', '-n', 'llmgateway', '-f', f'{t}/k'], input=msg, capture_output=True, env=ENV, check=True).stdout.decode()
        blob = dearmor(sig)
        pub = open(f'{t}/k.pub').read().split(); pub2 = open(f'{t}/k2.pub').read().split()
        pk = base64.b64decode(pub[1])[-32:]
        def allowed(name, line): open(f'{t}/{name}', 'w').write(line + '\n')
        allowed('ok', f'v0-key namespaces="llmgateway" {pub[0]} {pub[1]}')
        allowed('other-key', f'v0-key namespaces="llmgateway" {pub2[0]} {pub2[1]}')
        allowed('garbage', 'this is not allowed_signers')
        def sigfile(name, b): open(f'{t}/{name}', 'w').write(armor(b))
        sigfile('good.sig', blob)
        rflip = bytearray(blob); rflip[-64] ^= 1; sigfile('rflip.sig', bytes(rflip))
        S = int.from_bytes(blob[-32:], 'little'); sigfile('splusl.sig', blob[:-32] + (S + L).to_bytes(32, 'little'))
        open(f'{t}/garbage.sig', 'w').write('not an SSH signature\n')
        V = lambda a, i, n, sg: [KG, '-Y', 'verify', '-f', f'{t}/{a}', '-I', i, '-n', n, '-s', f'{t}/{sg}']
        print('blob_len', len(blob))
        run('C1 good', V('ok', 'v0-key', 'llmgateway', 'good.sig'), msg)
        run('C2 tampered message', V('ok', 'v0-key', 'llmgateway', 'good.sig'), msg.replace(b'GET', b'PUT'))
        run('C3 R bit flipped', V('ok', 'v0-key', 'llmgateway', 'rflip.sig'), msg)
        run('C4 wrong namespace (-n)', V('ok', 'v0-key', 'mlp-sidecar', 'good.sig'), msg)
        run('C5 principal not in allowed_signers (-I)', V('ok', 'other-id', 'llmgateway', 'good.sig'), msg)
        run('C6 key not in allowed_signers', V('other-key', 'v0-key', 'llmgateway', 'good.sig'), msg)
        run('C7 allowed_signers garbage', V('garbage', 'v0-key', 'llmgateway', 'good.sig'), msg)
        run('C8 garbage signature file', V('ok', 'v0-key', 'llmgateway', 'garbage.sig'), msg)
        run('C9 S+L malleated', V('ok', 'v0-key', 'llmgateway', 'splusl.sig'), msg)
        ID = (1).to_bytes(32, 'little'); ZERO = bytes(32)
        small = {'identity y=1': ID, 'identity y=1+P': (1 + P).to_bytes(32, 'little'),
                 'identity x=0 sign=1': (1 | (1 << 255)).to_bytes(32, 'little'), 'order-2 y=P-1': (P - 1).to_bytes(32, 'little')}
        for name, A in small.items():
            m = msg
            for i in range(64):
                m = msg.replace(b'AAEC', b'A%03d' % i)
                if h_even(ID, A, m): break
            allowed('small', 'v0-key namespaces="llmgateway" ssh-ed25519 ' + base64.b64encode(s(b'ssh-ed25519') + s(A)).decode())
            sigfile('forged.sig', sshsig(A, ID + ZERO))
            run(f'F1 forged R=identity S=0, {name}', V('small', 'v0-key', 'llmgateway', 'forged.sig'), m)
    finally:
        shutil.rmtree(t, ignore_errors=True)
        print('tempdir removed:', not os.path.exists(t))
    ```

  完成：輸出（無祕密）附在 V-0 前置的報告；架構師據此在 design 分類表補 10.2p1 欄。C1 不是 Good 行、或 C2／C3 不屬 design 判定規則的 401 形式、或 C4～C8 會落到 401 時，**先改 design 並經審查再改程式**，不得在 V-0 現場調整分類。若 C2／C3 的 `Could not verify signature.` 串流與 9.6p1 相同，順帶把判定規則中為 Mac 取樣保留的寬鬆形式刪除。
- [ ] V-0：真機驗收與啟用（依 D6、V-0 前置、PMO 明列維護窗及**本人另行授權**）。完成：在指定機器用測試身分驗通／拒絕／回退及 4710 探針，留無秘密證據；enforce 的切換和回退均由授權者操作。架構師審：是；這是操作步驟，不是實作 PR。
