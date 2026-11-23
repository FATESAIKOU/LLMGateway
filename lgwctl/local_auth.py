"""llmgwctl 的本機管理 principal 簽章（#222 D4b；契約 C-710、C-714）。

`llmgwctl usage` 與 `check` 的 `auth-local` 以本機管理私鑰簽章（D4a 的
``lgw.signer.Signer``）。私鑰的預設路徑**寫死**為
``/etc/llmgateway/local-admin-signing-key``（`layout` 會加上 ``LLMGW_PREFIX``），
測試與部署可以用 ``LLMGW_LOCAL_ADMIN_KEY`` 覆寫；key ID 預設
``llmgwctl-local-admin``，可用 ``LLMGW_LOCAL_ADMIN_KEY_ID`` 覆寫（必須與名冊登記的
key ID 一致）。

C-710 的檢查（每一項都在同一個 fd 上再驗一次，避免 lstat 與開啟之間被換掉）：

- 呼叫者必須是 root（只有 root 讀得到）；否則回 ``not_root``，不碰檔案；
- 存在、不是 symlink（**不跟隨**；``O_NOFOLLOW``）、是一般檔；
- 擁有者是 ``root:root``（``expected_uid``／``expected_gid`` 只給測試用）；
- 權限恰為 ``0600``（不是「沒有群組／其他人權限」就好，也不接受 0640）；
- 讀得到（``O_RDONLY`` 開得起來，C-714）。

任何一項不符都回「沒有 signer」與一個固定分類（見 `DETAILS`）；呼叫方就**沒有
signer**：`check` 的 `auth-local` 不送；`usage` 照 design :262 送一次無簽請求。任何
呼叫方都**不得**先簽、失敗後再改送無簽，也不得重試（C-710、C-714）。本模組只 import
標準函式庫與 ``lgw.signer``，不 import ``lgwd`` 或 ``tests``。

同一個 fd 的 `fstat` 重驗只防「`lstat` 之後、開檔之前」的換檔。之後 `Signer` 拿的是
**路徑**，`ssh-keygen` 簽章時會以路徑**再開一次**（而且會跟隨 symlink）；fd 驗過的檔
與實際用來簽的檔之間因此還有一個窗口。這裡依賴 `/etc/llmgateway` 只有 root 可寫
（能換檔的人本來就是 root，不構成跨權限邊界）；要徹底消除得讓 D4a 的 `Signer` 支援
傳 fd（`pass_fds` 加 `-f /dev/fd/N`），那是介面變更，不在 D4b。

不洩密：回傳的分類只含固定文字與**路徑**；私鑰內容、簽章與 nonce 都不進輸出
（``Signer`` 的 repr 由 D4a 負責）。
"""

from __future__ import annotations

import os
import stat

from lgw.signer import Signer

#: 管理私鑰的固定路徑（design「域分隔與管理工具」；`layout` 會加 prefix）。
DEFAULT_KEY_PATH = "/etc/llmgateway/local-admin-signing-key"
#: 預設 key ID（名冊的 `key_id` 要登記同一個值）。
DEFAULT_KEY_ID = "llmgwctl-local-admin"
#: 管理私鑰的權限必須恰為這個值（C-710）。
REQUIRED_MODE = 0o600

# --- 固定分類（呼叫方與測試判讀用；不放進輸出以外的資料） ---
NOT_ROOT = "not_root"
MISSING = "missing"
SYMLINK = "symlink"
NOT_REGULAR = "not_regular"
OWNER = "owner"
MODE = "mode"
UNREADABLE = "unreadable"

#: 給人看的一句話（含路徑；不含金鑰資料）。
DETAILS = {
    NOT_ROOT: "需要 root 才讀得到管理私鑰：{path}",
    MISSING: "管理私鑰不在：{path}",
    SYMLINK: "管理私鑰是 symlink（不跟隨）：{path}",
    NOT_REGULAR: "管理私鑰不是一般檔：{path}",
    OWNER: "管理私鑰的擁有者不是 root:root：{path}",
    MODE: "管理私鑰的權限不是 0600：{path}",
    UNREADABLE: "管理私鑰讀不到：{path}",
}

#: 不含路徑的簡短說明（給非 root 的 `usage` 用：不印金鑰路徑，見 design :262）。
SHORT_DETAILS = {
    NOT_ROOT: "沒有管理私鑰（需要 root）",
    MISSING: "管理私鑰不在",
    SYMLINK: "管理私鑰是 symlink（不跟隨）",
    NOT_REGULAR: "管理私鑰不是一般檔",
    OWNER: "管理私鑰的擁有者不是 root:root",
    MODE: "管理私鑰的權限不是 0600",
    UNREADABLE: "管理私鑰讀不到",
}


def detail(code: str, path: str) -> str:
    """固定分類加上路徑（呼叫方已確定可以印路徑時用）。"""
    template = DETAILS.get(code, "管理私鑰不可用：{path}")
    return template.format(path=path)


def short_detail(code: str) -> str:
    """固定分類、不含路徑（非 root 的 `usage` 用；也當未知分類的後備）。"""
    return SHORT_DETAILS.get(code, "管理私鑰不可用")


def _classify(info, expected_uid: int, expected_gid: int) -> str | None:
    """依 `stat` 的結果回分類；合格回 None。"""
    if stat.S_ISLNK(info.st_mode):
        return SYMLINK
    if not stat.S_ISREG(info.st_mode):
        return NOT_REGULAR
    if info.st_uid != expected_uid or info.st_gid != expected_gid:
        return OWNER
    if stat.S_IMODE(info.st_mode) != REQUIRED_MODE:
        return MODE
    return None


def build_signer(layout, *, euid=None, key_path: str | None = None,
                 key_id: str | None = None, expected_uid: int | None = None,
                 expected_gid: int | None = None) -> tuple[Signer | None, str | None]:
    """照 C-710 檢查後建一個 `Signer`；不合格回 `(None, 分類)`。

    `euid`、`key_path`、`key_id` 省略時取 `layout` 的值（`layout.euid` 是啟動時
    捕捉的呼叫者 uid）；`expected_uid`／`expected_gid` 預設是 root:root，只由
    `layout.local_admin_key_owner` 覆寫（**測試接縫**：測試機不能 chown 到 root；
    正式流程沒有地方設它）。**不合格時不建 `Signer`、不簽、也不讀金鑰內容。**
    """
    caller = layout.euid if euid is None else int(euid)
    path = os.fspath(layout.local_admin_key_path if key_path is None else key_path)
    if expected_uid is None or expected_gid is None:
        owner = getattr(layout, "local_admin_key_owner", (0, 0))
        default_uid, default_gid = owner
        expected_uid = default_uid if expected_uid is None else expected_uid
        expected_gid = default_gid if expected_gid is None else expected_gid
    if caller != 0:
        # 非 root：照 design 是「沒有 signer」；連 stat 都不做。
        return None, NOT_ROOT
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, MISSING
    except OSError:
        return None, UNREADABLE
    code = _classify(info, expected_uid, expected_gid)
    if code is not None:
        return None, code
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, MISSING
    except OSError:
        # 包含 ELOOP（lstat 之後被換成 symlink）、EACCES（讀不到；C-714）。
        return None, UNREADABLE
    try:
        opened = os.fstat(fd)
        code = _classify(opened, expected_uid, expected_gid)
        if code is not None:
            return None, code
    finally:
        os.close(fd)
    signer_key_id = layout.local_admin_key_id if key_id is None else key_id
    return Signer(key_id=signer_key_id, private_key_path=path), None


__all__ = [
    "DEFAULT_KEY_ID",
    "DEFAULT_KEY_PATH",
    "DETAILS",
    "MISSING",
    "MODE",
    "NOT_REGULAR",
    "NOT_ROOT",
    "OWNER",
    "REQUIRED_MODE",
    "SHORT_DETAILS",
    "SYMLINK",
    "UNREADABLE",
    "build_signer",
    "detail",
    "short_detail",
]
