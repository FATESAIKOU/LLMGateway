對應工單：#76（c）「cooldown 以模型為單位：一個免費模型 429 不要冷卻整把 subscription」；上層：MyPMO#163（2026-10-26 本人裁定升為最優先）；相關：#60（list／resolve 帶用量與 `available_again_at`）、#58（輪換表的「計入週用量節流」欄）、#48（get 退場）、#2（多訂閱無縫切換）。查證日期 2026-10-26。

> 本 change **只有設計**（proposal、design、spec delta、tasks），**不寫程式**。流程：設計 PR → 非作者審查 → 本人確認介面語意 → 才開實作單。設計裡的「本人的判斷點」與「作者提議、本人未指示」各有一節。
>
> **改版 1**（2026-10-26）：本人裁定「冷卻只是提示，不是閘門」後，在初版設計上重寫；初版的提前解除（`DELETE` 端點與 `cooldown_release` 稽核）、為冷卻做的呼叫方身分、以及 resolve 的 409 `cooling` 全部拿掉。本版只保留「登錄時驗證模型 id」這一項新驗證。

## Why

2026-10-26 opencode-zen 的 `glm-5.3-flash-free` 一個模型用完（opencode 回 `Free usage limit reached`），sidecar 依「整把」語意把整個 `opencode-zen` 冷卻一小時（`until` 由 07:23:12Z 再延到 09:24:36Z）；同一時間 `beam-free` 還能用，卻因整把被排除，所有 PM 都開不了 zen 隊員。事後本人的裁定是：

> 冷卻只是讓叫人方省麻煩、知道下次何時再叫；他真要再叫，Gateway 也擋不了，也沒必要擋。

所以這個 change 不是把「擋」做得更精準，而是把 Gateway 的角色收回成**記錄與告知**：

1. 冷卻以**模型**為單位記錄；一個模型的 429 不要把整把標成冷卻（沒帶 `model` 的舊呼叫方仍記整把，相容）。
2. `resolve`／`get` **不再因冷卻回 409**：照常選把、給 key，並在回應裡附上冷卻資訊（哪個模型、到何時；整把冷卻也一樣附）。要不要等、換哪個模型由呼叫方決定。
3. `list` 看得到每個模型與整把的冷卻時刻，讓人與 sidecar 在開隊員前自己避開。

現況（已查證）：`POST /v1/subscriptions/{id}/cooldown` 只收 `until`（`lgwd/registry.py:213-233`），重複登錄取較晚者（`lgwd/registry.py:226-230`），`resolve` 把整把冷卻中的候選全部排除並在全部冷卻時回 409 `cooling`（`lgwd/registry.py:148-161`、`openspec/specs/subscription-registry/spec.md:317`）。同一個模型的 429 因此波及同一把底下還活著的模型。

## What Changes

- **登錄冷卻的 body 多一個選填 `model`**（design 決定 1）：有給＝只冷卻那個模型；沒給（含 `"model": null`）＝整把（現行語意，完全相容）。同一（把、模型）重複登錄取較晚的 `until`；整把與模型兩層各自獨立、互不刪除、互不縮短。`model` 必須是非空字串、不超過 200 字元、是供應商自己的模型 id（**帶 `opencode/`／`claude-code/` 這類 agent 前綴就拒收**——allow 的 `*` 會匹配 `/`，只靠 allow 檢查擋不到）、不含萬用字元，且被該把 `models.allow` 的樣式涵蓋；任一不符回 **400 `bad_request`**（不是 403——那會和 resolve 的 `model_not_allowed` 政策碼混淆，也讓側車誤記成 `agent_not_allowed`），不登錄。
- **resolve 不再因冷卻擋人**（design 決定 2）：選把的「排除冷卻」這一步刪掉；冷卻也不參與排序（`priority` 與 id 是唯二排序鍵）。整把冷卻、指定模型冷卻、全部候選冷卻，一律照常回 200，附 key 與 `env`。
- **resolve 的 `meta` 附冷卻資訊**（design 決定 3）：`meta.cooldown`（該把整把的 `{"until"}` 或 null）與 `meta.model_cooldowns`（該把各模型的冷卻，形狀與 list 同一欄位、只列未過期、沒有時 `{}`）。`meta` 原本「MUST NOT 含 `cooldown`」那條改寫：不再含 `available`／`available_again_at`，但要含這兩個冷卻欄位。
- **409 `cooling` 從新 Gateway 的錯誤碼表移除**（design 決定 4）：新 Gateway 不再產生它；`until` 也不再從錯誤回覆出來。`lgw.client` 與側車**保留**對舊 Gateway 的 409 判讀（升版空窗），文件標成 legacy。
- **list／get 每把多 `model_cooldowns`**（design 決定 5）：物件（模型 id → `{"until"}`），只列還沒過的；整把的冷卻照舊在 `cooldown`，兩個都列。`available` **定義不變**（key 已部署而且整把沒有未來的 `until`；不因模型冷卻改變）——但要注意它的範圍：`available` 只保護 **list／get 的路徑**（舊 sidecar 的部署判斷與選把），**resolve 路徑不受它保護**，只要請求帶了 `provider`，選哪一把完全由 Gateway 決定，而新 Gateway 不看冷卻，所以舊 sidecar 仍可能開到冷卻中的那把（design 決定 4）。`schema` 不 bump（sidecar 拒絕 `schema > 1`，見 design 風險）。
- **拿掉提前解除與呼叫方身分**（本人 2026-10-26 裁定）：沒有 `DELETE` 端點、沒有 `cooldown_release` 稽核、沒有為冷卻新增的呼叫方身分；「呼叫方不指定模型就得等」這個舊代價也自然消失，因為 resolve 不再擋。
- **儲存**（design 決定 6）：新表 `model_cooldowns(subscription, model, until)`，additive migration（`CREATE TABLE IF NOT EXISTS`），舊 DB 直接可用、退版不受影響；整把的 `cooldowns` 表與函式一行不改。
- **交 MyLinuxPool 的請求**（design 決定 7，不在本 repo 改）：sidecar 把冷卻當提示——開隊員前先看 `list`，避開冷卻中的模型、挑同把的其他模型；收到 429 才登記「該模型」的冷卻；免費模型 fallback 600 秒起加倍（上限 3600）；保留對舊 Gateway 409 的判讀。
- **測試計畫**（design 決定 8）：至少釘住「A 模型冷卻時 resolve 指定 A 仍給 key 並附冷卻資訊」「指定 B 不受影響」「整把冷卻時 resolve 仍給 key 並附資訊」「不帶 `model` 的登錄仍記整把」「`model` 不在 allow → 400」「list 列出各模型與整把冷卻」「舊 409 路徑已不存在（或保留的條件）」。

## Capabilities

### New Capabilities

（無。行為變更全落在既有的 `subscription-registry`。）

### Modified Capabilities

- `subscription-registry`：7 條 MODIFIED（`list`、`登錄冷卻`、`稽核紀錄`、`錯誤回覆`、`resolve`、`resolve 的選把規則`、`共通 JSON schema`），**沒有 ADDED**。delta 對 `openspec/specs/subscription-registry/spec.md`，每條都完整重貼原條文再改。

## Impact

- **lgwd**：`registry.py`（登錄多接 `model`、resolve 刪掉第 7 步並在 meta 附冷卻、`_item` 多組 `model_cooldowns`）、`store.py`（新表與新查詢；稽核 `model` 欄沿用）、`errors.py`（**移除** `cooling()`）、`http.py`（**不新增路由**；cooldown body 原樣轉給 service）。
- **呼叫方**：舊 client（`lgw/client.py` 的 `cooldown(id, until)`）簽名與行為不變；新增選填 `model`。舊 sidecar（不送 `model`）→ 登錄仍是整把。它依賴的 409 `cooling` 在新 Gateway 不再出現，代價要照實講：**開啟時的 409 原本是拒絕（不是排隊）**，所以舊 sidecar 會先開起來、第一次呼叫吃 429（多一次啟動與一次 429）；只有一把的 provider 換把時今天就是 `no_candidate`，結果不變；**新代價是同一個 provider 有多把而且都在冷卻時會來回換**，每輪一次啟動加一次 429。升級順序因此仍是 sidecar 先、Gateway 後（見 design 決定 4 與「部署」）。
- **規格**：主 spec 7 條改寫、0 條新增；本 change 的 delta 已完整重貼，歸檔時直接套用。
- **MyLinuxPool**：要跟著改才有完整效果（看 list／meta 的提示、429 送 `model`、縮短免費模型 fallback）；屬 private repo，經 PMO 轉，不在本 PR。
- **部署**：只動程式與 DB schema（additive），沒有新設定、沒有新依賴；政策檔不用改。Gateway 與 sidecar 的升級順序在 design 的部署一節寫明。

## 不做

- **不做提前解除**：沒有 `DELETE`、沒有「允許登錄更早的 `until`」、沒有 `cooldown_release` 稽核。本人裁定冷卻不擋人，解除就沒有意義；要「再叫」直接叫，Gateway 照給。
- **不做呼叫方身分**：冷卻從閘門變成提示後，「誰能登錄冷卻」的濫用面只剩「留下不準的提示」，不再能擋住別人；為此新增認證是本末倒置。身分若要做，是另一個題目。
- **不改政策的形狀**：`models.allow`／`models.default` 不動；不做「每個模型一個 priority」。
- **不做「Gateway 代選模型」**：resolve 不因冷卻換模型、也不因冷卻換把；呼叫方要別的模型就指名，要別把就用 `subscription`／`exclude`。
- **不改 `until` 的 8 天上限**（`until_max_ahead_seconds`），模型冷卻也照同一個上限。
- **不做「cooldown 窗口」（5h／1w）**：v5 已經是 until-only（`openspec/specs/subscription-registry/spec.md:8`），本 change 不把它帶回來。
- **不清理過期的模型冷卻列**：與整把一致，讀的時候視為無效（design 決定 6 的否決選項）。
- **不動 `usage`／`available_again_at` 與 #60 的取用量邏輯**；resolve 的 `usage` 照舊。
