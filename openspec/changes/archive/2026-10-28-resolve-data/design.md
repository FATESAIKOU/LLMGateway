# 設計：`resolve` 加選填 `data`（#117，照 #102 的裁定）

完整推導、選項比較與行號見 #102 的草稿（`.work/archive-102/DRAFT-resolve-data.md`，本 change 只摘結論）。本檔只記「為什麼這樣做」與被放棄的選項。

## 1. 缺省：B（缺省不濾）

不帶 `data` 就是沒宣告等級，**完全不過濾**；帶了才一定不降級。

- 為什麼：用最嚴去猜等於替呼叫方宣告。A（缺省當 `real`）會讓舊 sidecar 的 zen／openrouter／groq／opencode-go 全不可用，必須 MLP 先發、Gateway 後發，反了是全域阻斷；而它要解的只是「個別呼叫釘太低」。B 與 `role-subscription-allocation` 的「舊 sidecar 不受影響、任一順序上線」一致，且有明確終點（MLP 都帶 `data` 之後另開 d 翻成 `real`）。
- 放棄：A（fail-closed、吵且好查，但上線順序被綁死、只能降版回滾）；C（政策檔開關 `resolve.default_data`，可只改政策檔切換，但多一個要驗的欄位、是否免發版未查）。

## 2. 驗證順序：agent → privacy → model

隱私步驟插在 `_pick` 的第 3 步（agent）與第 4 步（model）之間，編號記 3.5，**4～7 不變**（與程式註解、既有測試的步驟編號對齊）。

- 為什麼：agent 最便宜、不動既有 precedence 測試；反過來會把「這把不給這個 agent」說成「這把不夠乾淨」。
- 3.5 在步驟 5（`exclude` → 409 `no_candidate`）與步驟 6（key 未部署 → 409 `not_deployed`）之前：隱私不足回 403，不會被 `exclude` 救成 409，也不會因為剛好沒部署 key 變成 409。釘住時第 1 步已限縮成那把，3.5 只判斷這一把——那正是要擋的洞。
- 放棄：privacy 最先（改寫既有 precedence 與語意）。

## 3. 錯誤碼：400 `bad_request` / 403 `privacy_not_allowed`

- `data` 不在三個等級＝請求形狀錯 → 400 `bad_request`（沿用形狀錯、不新增碼，與 `roles` 同一條）。放棄 `invalid_data`（多一個碼、沒有好處）。
- 隱私不夠＝政策拒絕 → 403。碼名 `privacy_not_allowed`：政策欄位是 `privacy`，與 `agent_not_allowed`／`model_not_allowed` 成三兄弟。放棄 `data_not_allowed`（欄位錯名）、併進 `agent_not_allowed`（混語意）。

## 4. `meta.data`：回請求值原樣

resolve 沒有 role／floor，所以不是「生效等級」；沒給時 `null`。呼叫方與事後追查只讀得到「這次宣告了什麼」。只新增欄位，`schema` 不 bump；代價是精確鍵集的測試要同步。

- 放棄：不回（呼叫方與 log 都沒有宣告紀錄）。
- 稽核**不**記 `data`：稽核只記成功的 resolve（4xx 不記），B 缺省下沒帶 `data` 的呼叫也記不出等級；它能回答的只有「宣告了什麼」，不能回答真資料有沒有走過 public 那把。

## 5. 不做 `get`、不做 `models.known`

- `get` 同樣發憑證、同樣不擋，但它是過渡期介面、由 #48 移除；本 change 只把洞寫進 spec，不續命。
- `models.known`（判斷點 7）本人裁定不做：先讓第二層落地，萬用字元展開另議。
