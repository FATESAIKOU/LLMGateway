# 簽章方案離線原型（非正式程式）

此目錄不被 lgwd 載入，也不是可部署驗章器。`schemes.py` 的純 Python 曲線碼僅供比較，未受密碼學審查；HTTP 解析、欄位格式、重複標頭、nonce、防重放及授權都未實作。重複認證標頭須在 HTTP 層拒絕。

固定向量（`vectors.json`）記載八行 ASCII 簽章資料、公開鑰與簽章。**簽章不可再生**：產生它的私鑰是 cryptography 臨時產生後即丟棄，repo 內沒有產生腳本，所以無法從原始碼重算同一份簽章；它只能被驗證，不能被重現。要換向量須重新產生並更新 `vectors.json`。A、B 使用相同的固定原始 Ed25519 簽章。

C（SSHSIG）每次執行都用**臨時鑰**：`run_vectors.py` 與 `benchmark.py` 在臨時目錄以 `ssh-keygen` 產生私鑰與 allowed_signers，簽同一則訊息，測完目錄即自動刪除。因此 C 沒有可跨 Mac／worker 比對的固定 SSHSIG 向量；它的線上格式與 namespace 隔離由每次執行時的簽章測試，不靠固定檔。A 存在時另產一把臨時 Ed25519 鑰，確認 A 簽出的訊息可由 A 和 B 驗證。

從 repo 根目錄執行（用 `-I -B` 避免匯入當前目錄與寫入 `__pycache__`）：

```sh
python3 -I -B docs/prototypes/sign-schemes/run_vectors.py
```

需要 Python 3 及支援 `-Y` 的 ssh-keygen。缺 `cryptography` 時 A 標示 `A SKIP`（末尾印 `A SKIP count`），B／C 繼續，exit 0。要測 A，用已安裝 cryptography 的直譯器替換 `python3`。若 CI 或部署環境必須包含 A，加 `--require-a`：缺 `cryptography` 時立即以 exit 1 失敗，不會靜默少測。

獨立效能測試（同樣要 `-I -B`；A 存在時才測 A）：

```sh
python3 -I -B docs/prototypes/sign-schemes/benchmark.py
```

單筆延遲分冷啟（首次呼叫）與暖機後 20 筆樣本；p99 以 nearest-rank 取最大值，誤差很大。吞吐為 32 worker 的有限批次、5 次，列中位數與範圍。僅測驗章，不代表 10 分鐘端到端服務持續率。詳見 COMPARISON.md。
