## MODIFIED Requirements

### Requirement: 租約欄位
申請時訂閱可以不指定，由 LLMGateway 依政策選定（見「依政策選定訂閱」）。申請 MAY 帶 `preferences`（優先模型清單、底線模型）；v0 只記錄、不影響選定。每張核准的租約 SHALL 帶有以下欄位：
- 租約 id；
- 客戶端（發出申請的客戶端憑證所登記的名稱）；
- worker（worker 名稱，Mac 以 `mac:<主機名>` 表示）；
- 角色（不透明字串；格式待 Angareion 的身分表示定案後對齊）；
- 訂閱（`claude-personal`、`claude-company`、`openrouter`、`opencode-zen`、`ollama-cloud` 其中之一）；
- coding agent（`claude-code` 或 `opencode`）；
- 模型；
- 來由（工單參照，可空）；
- preferences（可空）；
- 申請時刻、有效期限、狀態。

核准的回覆 SHALL 另帶 `env`（要設給 agent 的環境變數與值）與 `unset`（啟動前要從 agent 環境去掉的變數名）。`env` 的值只出現在給客戶端的這一次回覆裡；租約紀錄、事件、log 與狀態板 MUST NOT 含任何憑證的值。

#### Scenario: 申請內容齊全
- **WHEN** 客戶端以 worker、角色、coding agent 申請租約，政策允許且有可用的訂閱
- **THEN** LLMGateway 回覆租約 id、選到的訂閱、`env`、`unset` 與有效期限，而事件紀錄裡沒有 `env` 的值

#### Scenario: 欄位缺漏
- **WHEN** 申請缺少 worker、角色或 coding agent 其中之一
- **THEN** LLMGateway 拒絕申請、指出缺少的欄位集合，且不留下任何租約事件

#### Scenario: 帶 preferences
- **WHEN** 申請帶有優先模型清單與底線模型
- **THEN** granted 事件記下 preferences，選到的訂閱與沒帶 preferences 時相同

#### Scenario: 客戶端冒用 worker 名稱
- **WHEN** 客戶端憑證登記的名稱與申請的 worker 不符
- **THEN** LLMGateway 拒絕申請，理由為 `client_mismatch`

### Requirement: 依政策選定訂閱
申請沒有指定訂閱時，LLMGateway SHALL 依政策檔中該 coding agent 的訂閱順序，選出第一個「不在冷卻中、未達同時租用上限、政策允許」的訂閱。指定了訂閱時，只考慮那一個。冷卻是指該訂閱最近一次被回報額度用完，而其冷卻結束時刻還沒到。選定規則 SHALL 支援「固定順序」；「平均分攤」保留在政策檔中可選。v0 的初始政策 SHALL 是：
- `claude-code`：`claude-personal` → `claude-company`；
- `opencode`：`opencode-zen` → `openrouter`（只允許免費模型）→ `ollama-cloud`。

選出的訂閱與選定理由 MUST 記在 granted 事件。

#### Scenario: 不指定訂閱
- **WHEN** coding agent 為 `claude-code`，申請沒有指定訂閱，且 `claude-personal` 不在冷卻中、未達上限
- **THEN** LLMGateway 選出 `claude-personal`，granted 事件記下選定理由

#### Scenario: 一個訂閱在冷卻中
- **WHEN** coding agent 為 `claude-code`，`claude-personal` 在冷卻中，`claude-company` 可用
- **THEN** LLMGateway 選出 `claude-company`

#### Scenario: 全部都在冷卻中
- **WHEN** 該 coding agent 順序中的訂閱全部在冷卻中
- **THEN** 租約狀態為 denied，理由為 `all_cooling`，並附最早的冷卻結束時刻

#### Scenario: 冷卻結束後回到第一順位
- **WHEN** `claude-personal` 的冷卻結束時刻已過，之後有新的 `claude-code` 申請
- **THEN** LLMGateway 選出 `claude-personal`

### Requirement: 憑證由 launcher 注入
五把上游 key 的值 SHALL 只保管在 LLMGateway。launcher（worker 上常駐的 sidecar、Mac 上前景執行的 `sidecar agent run`）SHALL 只透過租約的 `env` 取得那一把 key，並在啟動 agent 前：
- 套用 `env`；
- 從 agent 的環境中去掉 `unset` 列出的變數；
- 不把值寫進任何檔案、log 或命令列；
- 租約結束或換把時丟掉那個值。

其他訂閱的 key MUST NOT 出現在該 agent 的環境裡。拿不到租約時，launcher MUST NOT 啟動 agent。

#### Scenario: 只注入一把
- **WHEN** 租約的訂閱是 `claude-company`，coding agent 是 `claude-code`
- **THEN** agent 的環境以 `CLAUDE_CODE_OAUTH_TOKEN` 帶公司那一把，沒有個人那一把，也沒有 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_BASE_URL`

#### Scenario: 值不上命令列
- **WHEN** launcher 啟動 agent
- **THEN** 主機上任何程序的命令列與 herdr 的 pane、tab 資訊裡都看不到 `env` 的值

#### Scenario: 憑證來源缺失
- **WHEN** 服務的 key 檔裡沒有某個訂閱的 key（例如本人還沒設那把 secret）
- **THEN** 選定時把那個訂閱當作不可用、跳過它，狀態板顯示「未部署」，絕不回傳空的或部分的 `env`

#### Scenario: 強制階段沒有租約
- **WHEN** 申請被拒或 LLMGateway 無法連線
- **THEN** launcher 不啟動 agent，並回報原因（被拒時附 `earliest_reset`）

#### Scenario: 只記錄階段沒有租約
- **WHEN** 過渡期間有 agent 不是由 launcher 開的，而是照舊用 MyLinuxPool#50 掛在 worker 上的 key
- **THEN** 那個 agent 不算 LLMGateway 的租約，也不出現在狀態板；v0 沒有「只記錄」階段，凡由 launcher 開的 agent 一律先拿到租約才啟動

#### Scenario: opencode 不能拿 Claude 訂閱
- **WHEN** coding agent 為 `opencode`，申請指定了 `claude-personal` 或 `claude-company`
- **THEN** LLMGateway 拒絕申請，理由為 `not_allowed`

### Requirement: 訂閱切換不得安靜發生
只有在 launcher 判讀為「額度用完」時才 SHALL 換把；短暫的限流由 agent 自己重試，不觸發換把。換把時：
1. launcher SHALL 先安全結束原 agent，再向 LLMGateway 回報額度用完（帶訊號與判讀得到的重置時刻）。
2. LLMGateway SHALL 讓該訂閱進入冷卻，把原租約記為 superseded（帶理由與冷卻結束時刻），並在同一次回覆中依「依政策選定訂閱」給出下一張租約或拒絕。
3. launcher 以新 env 重開 agent，並以 `agora continue` 接回原 session。

其他規則：
- 冷卻結束時刻 SHALL 是回報的重置時刻加 2 分鐘；沒有重置時刻時，用政策檔該訂閱的預設冷卻時間。冷卻中的訂閱 MUST NOT 被出租，也 MUST NOT 被用來試探。
- 同一個 agent MUST NOT 同時持有兩張租約。
- 正在順利執行的 agent MUST NOT 因為冷卻結束而被換回；換回只在下一次新開或重開時，依選定規則發生。
- 由 launcher 開的 Claude Code SHALL 關掉額度重置後自動續跑的設定，換把只依 launcher 的判斷。
- 每次冷卻、換把、因全部冷卻而停下，都 MUST 留下事件，並出現在狀態板上。
- launcher 的通知（Angareion 或 Mac 的畫面）送不出去時，MUST NOT 讓換把回滾；通知留在本機重試直到送出。

#### Scenario: 一個訂閱額度用完
- **WHEN** 以 `claude-personal` 執行的 Claude Code 畫面出現 `You've hit your session limit · resets 3:45pm`，而 `claude-company` 可用
- **THEN** 原租約記為 superseded，`claude-personal` 冷卻到 3:47pm，下一張租約訂閱為 `claude-company`，agent 以新 env 重開並接回原 session

#### Scenario: 短暫限流不換把
- **WHEN** agent 出現暫時性的 429 並自行重試
- **THEN** launcher 不回報額度用完，租約不變

#### Scenario: 可用的訂閱都用完
- **WHEN** 回報額度用完時，順序中其他訂閱都在冷卻中
- **THEN** 原租約結束，回覆為拒絕 `all_cooling` 並附最早的冷卻結束時刻，launcher 停下 agent 並發出通知，不改用政策外的訂閱

#### Scenario: 沒有重置時刻
- **WHEN** 回報額度用完但判讀不出重置時刻
- **THEN** 該訂閱冷卻到「現在＋政策檔的預設冷卻時間」，期間不被出租

#### Scenario: 通知管道失效
- **WHEN** 換把時 launcher 的通知送不出去
- **THEN** 換把照常完成、事件完整，通知在恢復後補送恰一則

#### Scenario: 切換後新開 session
- **WHEN** 某個 agent 從 `claude-personal` 換到 `claude-company` 之後，又新開一個 Claude Code，而 `claude-personal` 仍在冷卻中
- **THEN** 新的租約訂閱是 `claude-company`；`claude-personal` 的冷卻結束後，再新開的拿到 `claude-personal`

#### Scenario: 冷卻結束不打斷
- **WHEN** 某個 agent 正以 `claude-company` 順利執行，而 `claude-personal` 的冷卻結束了
- **THEN** 該 agent 繼續使用 `claude-company`；下一個新開的 Claude Code 才拿到 `claude-personal`

### Requirement: Mac 與 worker 走同一套
在 Mac 上開 agent SHALL 使用與 worker 相同的租用介面與相同的換把程序（同一份 sidecar agent 宿主的程式，判讀規則來自 `lgw`）；worker 欄位以 `mac:<主機名>` 區分。實作中 MUST NOT 有只在 Mac 才走的換把分支；額度用完時，Mac 上的 agent 也會自動重開並接回 session。

#### Scenario: Mac 上開 agent
- **WHEN** 本人或 PM 在 Mac 上用 `sidecar agent run claude` 開一個 Claude Code
- **THEN** LLMGateway 有一張 worker 為 `mac:<主機名>` 的租約，事件與 worker 上的相同

#### Scenario: Mac 上額度用完
- **WHEN** Mac 上由 `sidecar agent run` 開的 agent 額度用完
- **THEN** 和 worker 上一樣：回報、冷卻、拿下一張、自動重開並接回 session

## ADDED Requirements

### Requirement: 同時租用上限
政策檔 SHALL 能為每個訂閱設定同時 active 的租約上限。達到上限的訂閱 MUST NOT 再被出租；選定時視同不可用。v0 的初始上限 SHALL 是 `claude-personal`、`claude-company` 各 2，其他訂閱不設上限。

#### Scenario: 達到上限
- **WHEN** `claude-personal` 已有 2 張 active 租約，又有一個 `claude-code` 申請
- **THEN** LLMGateway 選 `claude-company`；兩把都達上限時拒絕，理由為 `cap_reached`

#### Scenario: 結束後釋出
- **WHEN** `claude-personal` 的一張租約 released 或 expired
- **THEN** 下一個 `claude-code` 申請可以再拿到 `claude-personal`
