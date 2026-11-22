## Purpose

以可撤銷的節點金鑰身分保護 LLMGateway 業務 HTTP 介面，限制未授權的訂閱憑證取得與狀態修改，並透過可觀測的分階段切換和獨立健康檢查保留現有通道。簽章方案由本人選定；選定前本 change 不歸檔。

## ADDED Requirements

### Requirement: HTTP 呼叫身分
服務 SHALL 對五個既有業務路由提供本人選定方案的節點金鑰簽章 v1 驗證，簽入固定 audience、key ID、時間、隨機 nonce、method、原始含 query 的 request-target 與實際 body 雜湊。enforce MUST 拒絕缺漏、畸形、過期、未知、撤銷或驗簽失敗之請求並在業務處理前統一回 401，不洩漏原因。

#### Scenario: 已授權身分的完整請求
- **WHEN** 已登記的 key 簽署合法且未過期的新請求
- **THEN** 驗證通過後繼續授權與業務處理

#### Scenario: 請求被改寫或沒有簽章
- **WHEN** 請求無簽章、簽章不正確，或簽章後 query 或 body 遭改動
- **THEN** 回 401 且不執行業務操作或回傳憑證

#### Scenario: 時間或 method 被篡改
- **WHEN** 簽章 timestamp 的偏差超過 60 秒，或以 POST 的簽章送 GET
- **THEN** enforce 回 401 且不執行業務

#### Scenario: 重複標頭或絕對 URI
- **WHEN** enforce 收到重複認證標頭、歧義 Content-Length 或絕對 URI request-target
- **THEN** 拒絕請求，不執行業務或發放憑證

### Requirement: 重放與故障
服務 SHALL 在驗簽後、執行業務前以跨重啟持久、原子唯一的 key ID 與 nonce 防止重放。時窗、該 key 清除水位與記錄 nonce MUST 在同一個序列化的寫入 transaction 內、以同一個伺服器時間判斷；時窗為整數秒 `|now−ts|≤60`，timestamp MUST 大於清除水位。nonce MUST 保留超過其 timestamp 加 60 秒才可清除，清除與水位更新同一 transaction。MUST NOT 以伺服器時間水位讓全體停擺。資料庫忙碌 MUST 重試至期限才算不可寫；enforce 的持久庫不可用或容量不足 MUST fail closed；warn MUST 記錄不可判定並保持舊業務行為。

#### Scenario: 同一簽章並行重放
- **WHEN** 多個執行緒或重啟後再次收到同一有效簽章
- **THEN** 至多一次通過驗證，其餘回 401

#### Scenario: 到期前一刻重放與清除交錯
- **WHEN** 已記錄的簽章在其 timestamp 加 60 秒前一刻被重送，同時清除執行緒正在清除到期 nonce
- **THEN** 重送回 401，業務至多執行一次

#### Scenario: 時窗與水位邊界
- **WHEN** 伺服器時間等於 timestamp 加 60、等於 timestamp 加 61，或 timestamp 等於該 key 的清除水位
- **THEN** 第一種接受，後兩種回 401

#### Scenario: 高並行不誤判故障
- **WHEN** 大量合法簽章請求並行抵達，資料庫只是暫時忙碌
- **THEN** 在期限內等待後照常處理，不回 503

#### Scenario: 伺服器時鐘錯誤後校正
- **WHEN** 伺服器時鐘跳到未來或落後，期間清除了 nonce，之後時鐘校正
- **THEN** 被清除 nonce 的舊簽章重送回 401；校正後新簽章請求照常通過，不回 503，也不要求換鑰

#### Scenario: 共用時間來源一起跳到未來
- **WHEN** 客戶端與伺服器共用的時間來源跳到未來後校正，故障期間有流量的多把 key 清除水位都晚於現在加 60 秒
- **THEN** 這些 key 都回 401 並告警，直到真實時間追上或經有審計的重設；其他 key 不受影響，不回 503

#### Scenario: 防重放儲存失效
- **WHEN** enforce 模式下 nonce 持久庫重試至期限仍不可寫，或已損毀
- **THEN** 回 503，禁止執行業務並告警

#### Scenario: warn 儲存失效
- **WHEN** warn 的 nonce 儲存或驗證相依不可用，包含服務啟動時
- **THEN** 告警並標示驗證不可判定，服務照舊啟動並處理業務，不因認證故障回 503

### Requirement: 公鑰名冊與撤銷
服務 SHALL 使用管理者維護的本地名冊，將唯一 key 對應 principal 和明列權限；MUST NOT 在執行期讀取 MLP node 名冊。服務以背景輪詢自行偵測名冊檔變更（不依賴請求流量），原子載入經驗證的較高世代；同世代且內容相同視為無變更，不經 signal 或 HTTP 管理端點。重啟載入 MUST 與執行中偵測用同一套判斷。撤銷 MUST 標為 revoked 並永久保留；任何世代移除或恢復已撤銷 key 時 MUST 整份拒絕。撤銷墓碑、世代水位與已驗證快照 MUST 存於與防重放庫分開的持久庫。世代重置 MUST 照驗墓碑，並在同一 transaction 內一起換掉世代水位與已驗證快照。enforce 無有效名冊也無已驗證快照時 MUST 拒絕啟動。兩個持久庫缺檔或庫識別不符 MUST 視為遺失；服務 MUST NOT 自行建空庫，只有明確的初始化或恢復指令可建庫；enforce 遇遺失 MUST 拒絕啟動。

#### Scenario: worker 更換金鑰
- **WHEN** 管理員核驗新公鑰並載入允許重疊的新世代
- **THEN** 新舊有效鍵均可依各自權限使用，直到舊鍵被撤銷

#### Scenario: 撤銷與名冊損毀
- **WHEN** 已撤銷金鑰發出請求，或更新名冊格式不合法
- **THEN** 撤銷鍵回 401；損毀更新不取代最後有效名冊並告警

#### Scenario: 較高世代想讓已撤銷鍵復活
- **WHEN** 新世代名冊刪掉已撤銷 key 的紀錄、把它改回有效，或以新 key ID 重用同一公鑰
- **THEN** 整份名冊拒絕載入、沿用最後有效快照並告警，該 key 仍回 401

#### Scenario: 重建防重放庫後撤銷仍有效
- **WHEN** 防重放庫重建後，載入一份刪掉已撤銷 key 的較高世代名冊
- **THEN** 墓碑仍在，整份名冊拒絕載入，該 key 仍回 401

#### Scenario: 沒有流量時也載入
- **WHEN** 服務閒置或只收到 `/healthz` 時安裝了新世代名冊
- **THEN** 服務仍在數秒內載入並更新狀態檔

#### Scenario: 同一份名冊再讀一次
- **WHEN** 服務再次讀到與已載入相同世代、相同內容的名冊
- **THEN** 視為無變更、不記錯誤；狀態檔已是 ok 時不改寫，若停在前一份壞檔留下的錯誤則改回 ok

#### Scenario: 同世代內容被改動後重啟
- **WHEN** 名冊檔被原地改成與已驗證快照同世代、內容不同（例如把已撤銷 key 改回有效），之後服務重啟
- **THEN** 與執行中相同，整份拒絕、沿用已驗證快照並告警，該 key 仍回 401

#### Scenario: enforce 下信任庫或防重放庫缺失
- **WHEN** enforce 啟動時信任庫或防重放庫不存在，或兩庫的庫識別不一致
- **THEN** 服務拒絕啟動並告警，不建立空庫；warn 下告警並標示驗證不可判定、照舊處理

#### Scenario: 誤裝高世代後重置世代
- **WHEN** 誤裝了較高世代的名冊，管理員停服務後以已核驗的較低世代名冊執行世代重置，再重啟服務
- **THEN** 服務載入該較低世代名冊，其中有效的 key 可用；原有撤銷墓碑仍在，重置名冊若把已撤銷 key 改回有效則整個拒絕、不改任何狀態

#### Scenario: 壞名冊已裝入後重啟
- **WHEN** 名冊檔無效且服務重啟，但持久庫有已驗證快照
- **THEN** 沿用該快照並告警，enforce 不因此拒絕啟動

#### Scenario: enforce 缺少有效名冊
- **WHEN** enforce 啟動且名冊檔無效、也沒有已驗證快照
- **THEN** 服務拒絕啟動並告警，不默默降級

### Requirement: 每 key 防重放配額
服務 SHALL 在防重放 transaction 內限制每把 key 尚在保留期的紀錄數，只計 timestamp 不早於伺服器現在減 65 秒的列，待清除的過期列 MUST NOT 計入。重放判斷 MUST 先於配額。超過上限時 enforce MUST 回 429、不寫入、不執行業務；warn 記錄預計拒絕、不寫入並照舊處理。單一 key 達上限 MUST NOT 使其他 key 被拒絕。因配額、時窗或儲存不可用而未入庫的有效簽章，日後至多被接受一次。

#### Scenario: 單一 key 灌量
- **WHEN** key A 的未過期紀錄已達上限，key A 與 key B 都送出新的合法簽章
- **THEN** key A 回 429 且不執行業務，key B 照常通過

#### Scenario: 清除落後時的過期紀錄
- **WHEN** 庫中仍留有該 key 早於現在減 65 秒、尚未清除的紀錄
- **THEN** 這些紀錄不計入配額，合法請求不因此回 429

#### Scenario: 已達上限時的重放
- **WHEN** key 已達上限時收到已記錄過的簽章
- **THEN** 回 401，不回 429

#### Scenario: 配額拒絕的簽章日後重送
- **WHEN** 曾被 429 拒絕的簽章在配額空出後、仍在時窗內被重送多次
- **THEN** 至多一次通過，其餘回 401

### Requirement: principal 授權
服務 SHALL 在驗章後依 principal 明列的操作、resolve agent 及 roles 的 role／agent 執行預設拒絕授權，然後套用既有訂閱政策；resolve 沒有可信 role，不得宣稱已依 role 限權。

#### Scenario: 有操作和 agent 權限
- **WHEN** principal 獲准 resolve 指定 agent 且既有政策允許
- **THEN** 按既有規則執行 resolve

#### Scenario: 認證成功但權限不足
- **WHEN** 簽章有效但操作、agent 或 role 不在其允許清單
- **THEN** 回 403 且不執行業務操作

### Requirement: 漸進切換和觀測
服務 SHALL 提供 off、warn、enforce；off 維持現況，warn 驗證並記錄預計拒絕但保留原業務行為，enforce 才拒絕。日誌 MUST NOT 包含金鑰、簽章、nonce、query、body 或憑證；錯誤不得觸發自動免認證回退。

#### Scenario: 舊客戶端在 warn
- **WHEN** 未簽章的舊客戶端在 warn 模式呼叫
- **THEN** 記錄預計拒絕並按舊業務規則處理

#### Scenario: enforce 驗證失敗
- **WHEN** enforce 模式認證失敗
- **THEN** 請求被拒且服務和客戶端均不自動降回免認證

#### Scenario: 日誌與憑證
- **WHEN** 成功或失敗的認證與業務請求被記錄
- **THEN** 日誌不包含私鑰、簽章、nonce、query、body 或憑證值

### Requirement: 本機管理工具的身分
`llmgwctl usage` 與 `check` 的已簽章項 SHALL 使用只有 root 讀得到、僅限唯讀操作的本機管理 principal。讀不到時 MUST NOT 先簽失敗再無簽重送；非 root 的 `check` 該項 MUST 回 `todo` 而非 `fail`；enforce 下非 root 的 `usage` MUST 以結束碼 1 提示需 root。off／warn 下任何使用者的 `usage` 照舊可用。

#### Scenario: root 執行檢查
- **WHEN** root 在 enforce 下執行 `llmgwctl check` 且簽章 list 成功
- **THEN** 該項為 ok，輸出不含私鑰或簽章

#### Scenario: 非 root 執行檢查
- **WHEN** 非 root 在 enforce 下執行 `llmgwctl check`
- **THEN** 已簽章項為 `todo` 並註明需 root，不試打業務端點、不報 `fail`

#### Scenario: 非 root 在 enforce 跑 usage
- **WHEN** 非 root 在 enforce 下執行 `llmgwctl usage`
- **THEN** 結束碼 1 並提示需 root，不無簽重送、不印額度

### Requirement: 無敏感資料的健康檢查
服務 SHALL 提供免認證 GET `/healthz`，只回固定存活狀態、不回名冊、訂閱或憑證；它只證明 HTTP 存活，不代表可 resolve。

#### Scenario: 認證開啟時健康探針
- **WHEN** enforce 啟用且未簽章探針請求 `/healthz`
- **THEN** 服務回 200 與固定狀態

#### Scenario: 健康請求帶未知簽章
- **WHEN** enforce 的 `/healthz` 請求帶未知 key 的簽章
- **THEN** 忽略認證標頭並回固定 200，不代表業務認證可用

#### Scenario: 探針不揭露業務資料
- **WHEN** 未簽章呼叫 `/healthz`
- **THEN** 回應不包含訂閱、身分、金鑰或授權資料
