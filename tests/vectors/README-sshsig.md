# 固定 SSHSIG 向量（`sshsig_v1.json`）

對應 LGW#222 D2 PR-5（tasks C-507）與 design「向量產生程序」（#247 G-13）。
測試 `tests/test_sshsig.py` 會斷言這份檔與 `lgwd/sshsig.py` 的 `SELFTEST_VECTOR`
逐欄相同；啟動自測用它真的 exec 一次 `ssh-keygen -Y verify`。

## 內容

| 欄位 | 意思 |
|---|---|
| `schema` | 1 |
| `key_id` | 名冊 key ID（沿用原型 `docs/prototypes/sign-schemes/vectors.json` 的 `test-key-236`） |
| `public_key` | OpenSSH 公鑰行（`ssh-ed25519 <base64>`，無註解） |
| `signing_data` | 八行 ASCII 簽章資料，每行以 LF 結尾（欄位沿用同一個原型向量） |
| `signature` | SSHSIG 二進位 blob（180 bytes）的無 padding base64url，240 字元 |
| `bad_signature` | 同上，但簽章 R 的第一個 bit 反轉；S 未動，所以**仍通過預解析**，用來驗 401 分類 |

`signing_data` 的第 8 行是空 body 的 SHA-256（`e3b0c442…b855`），不是把 body 放進來。

## 產生程序（可重現步驟，簽章不可重現）

在 0700 的臨時目錄、只用臨時產生的金鑰，產完即刪；私鑰不進 repo、不進任何輸出。

```sh
TMP=$(mktemp -d)            # 0700
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
ssh-keygen -q -t ed25519 -N '' -C '' -f key
# 八行訊息取自 docs/prototypes/sign-schemes/vectors.json 的 signing_data（沿用原型欄位）
python3 -c 'import json,sys; open("msg","wb").write(json.load(open(sys.argv[1]))["signing_data"].encode())' \
  /path/to/repo/docs/prototypes/sign-schemes/vectors.json
ssh-keygen -Y sign -n llmgateway -f key < msg > sig.asc
```

（驗證由 `tests/test_sshsig.py` 的 `test_c507_real_*` 做；`allowed_signers` 的一行是
`<key ID> namespaces="llmgateway" ssh-ed25519 <base64>`，`<key ID>` 是 `test-key-236`。）

1. `sig.asc` 去掉 `-----BEGIN/END SSH SIGNATURE-----` 兩行後 base64 解碼，得到 180 bytes。
2. `signature` 是這 180 bytes 的無 padding base64url（`base64.urlsafe_b64encode(...).rstrip('=')`）。
3. `bad_signature` 是同一份 blob 把**簽章 R 的第一個 byte** 反轉一個 bit（S 不動）；R 從
   信封最後 64 bytes 的第 0 byte 開始。改完仍會通過預解析，用來驗 401 分類。
4. `public_key` 是 `key.pub` 的第一、二欄（不含註解）。

Ed25519 簽章不可重現，換金鑰或換訊息就要整份重產並經架構師審。這份向量於
OpenSSH_9.6p1（worker 容器，Ubuntu 3ubuntu13.20）產生；驗證結果見
`tests/test_sshsig.py` 的 `test_c507_real_*`。
