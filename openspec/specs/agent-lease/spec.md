# agent-lease Specification

## Purpose
讓所有 AI agent（worker 上由 sidecar 開的、Mac 上由本人或 PM 開的）在啟動前向 LLMGateway 取得租約，使「哪台機器、什麼角色、哪個訂閱、哪個 coding agent、哪個模型」在開與關的時點都有紀錄，並讓憑證只在租約允許的範圍內被注入。
## Requirements
### Requirement: 租約欄位
申請時訂閱可以不指定，由 LLMGateway 依政策選定（見「依政策選定訂閱」）。每張核准的租約 SHALL 帶有以下欄位：租約 id、worker（worker 名稱，Mac 以 `mac:<主機名>` 表示）、角色（不透明字串；格式待 Angareion 的身分表示定案後對齊）、訂閱（訂閱 id，例如 `claude-personal`、`claude-company`、`ollama-cloud`、`openrouter`、`opencode-zen`、`antigravity`）、coding agent（例如 `claude-code`、`opencode`、`agy`）、模型、來由（工單參照，可空）、申請者、申請時刻、有效期限、狀態。租約 MUST NOT 含任何憑證的實際值；需要指出憑證時只能用憑證來源的識別鍵。

#### Scenario: 申請內容齊全
- **WHEN** launcher 以 worker、角色、訂閱、coding agent、模型申請租約
- **THEN** LLMGateway 回覆的租約含全部欄位，且憑證只以來源識別鍵出現

#### Scenario: 欄位缺漏
- **WHEN** 申請缺少 worker、角色、coding agent 或模型其中之一
- **THEN** LLMGateway 拒絕申請、指出缺少的欄位集合，且不留下任何租約事件

### Requirement: 依政策核准
LLMGateway SHALL 依政策檔（角色 × coding agent × 訂閱 × 模型 的允許清單，以及每組角色 × coding agent 的訂閱選定規則）決定核准或拒絕；政策檔的內容就是允許集合的權威。政策內的申請 MUST 自動核准；政策外的申請 MUST 拒絕並說明理由，不得退而給另一個未申請的訂閱。期 1 的初始政策檔 SHALL 列入當時 worker 能力已提供的全部組合。

#### Scenario: 政策內自動核准
- **WHEN** 申請的組合在政策允許清單內
- **THEN** 租約狀態為 granted，不需要人介入

#### Scenario: 政策外拒絕
- **WHEN** 申請的組合不在允許清單內
- **THEN** 租約狀態為 denied，回覆帶理由，且不發出任何替代租約

#### Scenario: 指定的訂閱在冷卻中
- **WHEN** 申請指定了某個訂閱，而該訂閱正處於額度冷卻（之前的租約因額度結束，恢復時刻未到）
- **THEN** 租約狀態為 denied，理由為冷卻中並附恢復時刻

#### Scenario: 政策收緊
- **WHEN** 政策檔移除了某個組合，而該組合有 active 租約
- **THEN** 該租約在下一次與 LLMGateway 往來時記為 revoked（觸發者：政策），launcher 終止該 agent

### Requirement: 依政策選定訂閱
同一組角色 × coding agent 可用的多個訂閱 SHALL 地位相同，彼此之間沒有主從之分；申請沒有指定訂閱時，LLMGateway SHALL 依政策檔的選定規則，從允許清單中排除冷卻中的訂閱後選出一個。冷卻是指該訂閱最近一張因額度結束的租約，其恢復時刻還沒到。選定規則 SHALL 至少支援「固定順序」與「平均分攤」：平均分攤時，選出目前這個額度窗口內已用量最少的訂閱；用量相同時，選最久沒被選到的。選出的訂閱與選定理由 MUST 記在 granted 事件。期 1 的初始政策對 Claude 的兩個訂閱（`claude-personal`、`claude-company`）SHALL 採平均分攤。

#### Scenario: 不指定訂閱
- **WHEN** 申請沒有指定訂閱，政策對該組合允許 `claude-personal` 與 `claude-company`，採平均分攤，且兩者都不在冷卻中
- **THEN** LLMGateway 選出這個額度窗口內用量較少的那一個，granted 事件記下選定理由

#### Scenario: 一個訂閱在冷卻中
- **WHEN** 申請沒有指定訂閱，而允許清單中有一個訂閱在冷卻中
- **THEN** LLMGateway 只從其他不在冷卻中的訂閱選定

#### Scenario: 全部都在冷卻中
- **WHEN** 申請沒有指定訂閱，而允許清單中的訂閱全部在冷卻中
- **THEN** 租約狀態為 denied，理由為全部冷卻中，並附最早的恢復時刻

### Requirement: 租約狀態與事件
租約 SHALL 依序經過 requested → granted 或 denied → active → released、expired、revoked 或 superseded 其中之一；唯一的例外是 granted 之後沒有啟動 agent 就結束，此時直接由 granted 記為 released（理由：未啟動）。沒有租約就啟動的 agent 另以「未租約啟動」事件記錄，不進入租約的狀態機。每次狀態變化與每次續期 MUST 以只追加的事件記錄，事件帶單調遞增的序號、時刻與觸發者；事件的先後以序號為準，時刻只作註記。launcher 的動作與事件的對應 SHALL 為：start → active、stop → released、switch → 原租約 superseded 並申請新租約、renew → 有效期限延長（狀態不變）、revoke → revoked。

#### Scenario: 正常一輪
- **WHEN** launcher 取得 granted 租約、以 start 回報 agent 已啟動，之後以 stop 回報 agent 結束
- **THEN** 紀錄依序有 requested、granted、active、released 四個事件，序號遞增

#### Scenario: 續期
- **WHEN** launcher 對 active 租約 renew
- **THEN** 紀錄多一筆續期事件，有效期限延長，狀態仍是 active

#### Scenario: launcher 沒回報結束
- **WHEN** active 租約超過有效期限且 launcher 沒有續期
- **THEN** 在下一次任何人與 LLMGateway 往來（申請、查詢、續期、回報）時，該租約先被記為 expired，再處理該次往來

#### Scenario: 對已結束的租約續期
- **WHEN** launcher 對 expired、revoked 或 superseded 的租約 renew
- **THEN** LLMGateway 拒絕續期，launcher 終止該 agent

#### Scenario: 撤銷
- **WHEN** 本人或 PMO 以 revoke 撤銷一張 active 租約
- **THEN** 租約記為 revoked，且負責的 launcher 在下一次與 LLMGateway 往來時得知並終止該 agent

#### Scenario: 重複回報
- **WHEN** launcher 因重試把同一個 start 或 stop 送了兩次
- **THEN** 紀錄只有一筆對應事件

### Requirement: 回報暫時失敗時保存後補送
launcher 回報 start、stop、switch、renew 或用量時若 LLMGateway 暫時無法連線，SHALL 將回報保存在本機、恢復後依原順序補送；補送不得造成事件重複。續期在有效期限前一直無法送達時，已啟用強制的 launcher MUST 在有效期限到達時終止 agent；尚未啟用強制的 launcher 讓 agent 繼續，並在恢復後補送。

#### Scenario: 結束時連不上
- **WHEN** agent 結束時 launcher 的 stop 回報失敗，之後 LLMGateway 恢復
- **THEN** launcher 補送 stop，紀錄最終恰有一筆 released 事件，用量不漏

#### Scenario: 強制階段續期一直失敗
- **WHEN** 已啟用強制，launcher 在有效期限到達前都無法送出續期
- **THEN** launcher 在有效期限到達時終止 agent，恢復後補送的續期被拒絕，租約記為 expired

### Requirement: 憑證由 launcher 注入
既有的上游訂閱與供應商憑證的值 MUST NOT 經過、存放於 LLMGateway。launcher SHALL 在取得 granted 租約後，只從本機憑證來源取出租約指定的那一個訂閱的憑證，以該 coding agent 實際讀取的變數名注入 agent 的環境；其他訂閱的憑證 MUST NOT 出現在該 agent 的環境裡。

#### Scenario: 只注入一把
- **WHEN** 租約的訂閱是 `claude-company`，coding agent 是 `claude-code`
- **THEN** agent 的環境以 `CLAUDE_CODE_OAUTH_TOKEN` 帶公司那一把，沒有個人那一把，也沒有任何 Anthropic API key 或閘道憑證變數

#### Scenario: 憑證來源缺失
- **WHEN** 租約 granted，但本機憑證來源找不到該訂閱的憑證
- **THEN** launcher 不啟動 agent、不做部分注入，租約由 granted 直接記為 released（理由：未啟動，說明為憑證來源缺失）

#### Scenario: 強制階段沒有租約
- **WHEN** 已啟用強制，而申請被拒或 LLMGateway 無法連線
- **THEN** launcher 不啟動 agent，並回報失敗原因

#### Scenario: 只記錄階段沒有租約
- **WHEN** 尚未啟用強制，而 LLMGateway 無法連線或拒絕申請
- **THEN** launcher 照樣啟動 agent，在本機留下警告，並記錄（連不上時於恢復後補送）一筆「未租約啟動」事件，事件帶 worker、角色、訂閱、coding agent、模型與未取得租約的理由

### Requirement: 訂閱切換不得安靜發生
當 agent 因額度或限流必須改用另一個訂閱時，launcher SHALL 先安全結束原 agent，將原租約記為 superseded（帶理由與恢復時刻），以不指定訂閱的方式申請新租約（原訂閱因此在冷卻中，不會被選回），再以新憑證接續原 session；每次切換 MUST 對 PMO 發出通知，通知帶新舊租約 id。通知失敗 MUST NOT 讓切換回滾，通知 SHALL 留在本機重試直到送出。切換不改變任何訂閱的地位；之後新開的 session 一律照「依政策選定訂閱」選定。

#### Scenario: 一個訂閱額度用完
- **WHEN** 以 `claude-personal` 或 `claude-company` 其中之一執行的 agent 出現額度用完的訊號，而另一個不在冷卻中
- **THEN** 原租約記為 superseded（理由：額度、恢復時刻），新租約訂閱為另一個，agent 接續原 session，PMO 收到一則帶新舊租約 id 的切換通知

#### Scenario: 可用的訂閱都用完
- **WHEN** 允許清單中其他訂閱都在冷卻中
- **THEN** 租約記為 released（理由：額度），agent 停止，PMO 收到通知，不自動改用政策外的訂閱

#### Scenario: 通知管道失效
- **WHEN** 切換時通知送不出去
- **THEN** 切換照常完成、事件完整，通知在恢復後補送恰一則

#### Scenario: 切換後新開 session
- **WHEN** 某個 agent 從 `claude-personal` 切換到 `claude-company` 之後，同一 worker 與角色又新開一個 Claude Code session，且 `claude-personal` 仍在冷卻中
- **THEN** 新 session 的租約訂閱是 `claude-company`；`claude-personal` 的恢復時刻過了之後，再新開的 session 依平均分攤重新選定

### Requirement: Mac 與 worker 走同一套
在 Mac 上開 agent SHALL 使用同一份租約申請與回報介面；worker 欄位以 `mac:<主機名>` 區分。

#### Scenario: Mac 上開 agent
- **WHEN** 本人或 PM 在 Mac 上透過啟動包裝開一個 coding agent
- **THEN** LLMGateway 有一張 worker 為 `mac:<主機名>` 的租約，事件與 worker 上的相同

