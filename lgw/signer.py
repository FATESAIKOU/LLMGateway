"""SSHSIG 請求簽章（#222 D4a；契約 C-701～C-709）。

`Signer` 用本機 OpenSSH 的 `ssh-keygen -Y sign -n llmgateway` 對 lgw 自己組的
ASCII 八行訊息簽章，回五個 `X-LGW-*` 標頭。只用標準函式庫，不新增 Python 相依。

八行（每行一個 LF，design「線上格式」）：

    1  LGW-HTTP-SIG-V1
    2  llmgateway（audience，也是 SSHSIG 的 namespace）
    3  key ID
    4  Unix 秒（十進位、無前導零）
    5  nonce（32 bytes 的無 padding base64url，43 字元）
    6  HTTP 大寫 method
    7  實際送出的 origin-form request-target（含完整 query、ASCII 百分號編碼）
    8  實際送出 body bytes 的小寫 SHA-256 hex（GET 為空 bytes 的雜湊）

`ssh-keygen` 的 armor（`-----BEGIN/END SSH SIGNATURE-----`）解碼後必須恰為
180 bytes，再以無 padding 的 base64url 編成固定 240 字元的
`X-LGW-Signature`；長度不符就是 `SignerError`，呼叫方不得送出請求（C-703）。

兩種金鑰來源（C-704）：`private_key_path`（私鑰檔；有密語時 `ssh-keygen` 沒有 tty
會直接失敗，本模組不提示、不等待輸入）或 `public_key_path` 加 ssh-agent
（`agent_sock` 或環境的 `SSH_AUTH_SOCK`）。

子行程（C-701）：絕對路徑、argv 陣列固定、`shell=False`、訊息走 stdin；環境**恰為**
`LC_ALL=C`、`PATH=/usr/bin:/bin`、`HOME=<金鑰檔目錄>`，agent 模式另加
`SSH_AUTH_SOCK`；`start_new_session=True`（新 session，**不繼承呼叫端的 controlling
tty**）；逾時（預設 5 秒）就殺掉子行程。

`start_new_session=True` 是 C-704 的一部分：呼叫端在互動終端（或有 pty）時，
有密語的私鑰若還留著 controlling tty，`ssh-keygen -Y sign` 會開 `/dev/tty` 印
`Enter passphrase:` 並阻塞到逾時，變成 `timeout` 而不是 `sign_failed`（#266 審查
中項）。脫離 session 後它打不開 `/dev/tty`，立即以 `rc=255` 失敗，本模組回
`sign_failed`，不提示、不等待輸入。子行程環境已被收斂成上面幾個鍵，`SSH_ASKPASS`
一類的變數不會被繼承，所以沒有另設 `SSH_ASKPASS_REQUIRE=never`（多一個環境鍵會動到
C-701 的固定環境清單，卻沒有多餘保護）。

`HOME` 刻意指到**金鑰檔所在目錄**而不是使用者的家目錄（#266 架構審 低-2）：

- `ssh-keygen -Y sign` 不寫入 `HOME`，本模組也不必讀使用者的家目錄；
- OpenSSH 9.6p1 的 `-Y sign` 不讀 `~/.ssh/config`，所以即使金鑰在 `~/.ssh`，那個
  目錄只是被當成 `HOME`，不會改變簽章行為（本模組不讀、不寫其中的內容）；
- 若未來版本的 `-Y sign` 改成會讀 `HOME` 下的設定，這裡要改成一個空的暫存目錄
  （`tempfile.mkdtemp`，用完刪），並同步 `tests/test_lgw_signer.py` 的環境斷言。

不洩密（C-709）：`Signer`、`SignerError` 的 repr／訊息不含簽章、nonce、公鑰或私鑰
內容（私鑰**路徑**可以出現在 repr）；`ssh-keygen` 的 stdout／stderr 原文不進例外或
日誌，只回固定分類。本模組不寫任何日誌。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import subprocess
import time

#: SSHSIG namespace，也是八行訊息的第 2 行（audience）。
NAMESPACE = "llmgateway"
#: 八行訊息的第一行（版本綁定）。
SIG_LINE_1 = "LGW-HTTP-SIG-V1"
#: 預設的 `ssh-keygen`（絕對路徑；C-701）。
SSH_KEYGEN = "/usr/bin/ssh-keygen"
#: 子行程環境的固定 PATH（C-701）。
BASE_PATH = "/usr/bin:/bin"
#: `ssh-ed25519` 的 SSHSIG blob 固定大小（C-703）。
BLOB_BYTES = 180
#: 簽章 blob 編碼後的固定長度（C-703）。
SIGNATURE_CHARS = 240
#: 預設逾時（秒；C-701）。
DEFAULT_TIMEOUT = 5.0

#: 名冊的 key ID 格式（design「線上格式」；同時排除換行與 allowed_signers 樣式字元）。
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
#: HTTP method：只接受大寫英文字母。
_METHOD_RE = re.compile(r"[A-Z]+\Z")
#: 非 180 bytes 的 armor 或壞 base64 都回這個分類。
_BAD_SIGNATURE = "bad_signature"


class SignerError(Exception):
    """簽章失敗（固定分類；不含簽章、nonce、金鑰內容）。

    `reason` 是給呼叫方／測試判讀的固定字串：
    `invalid_key_id`、`invalid_method`、`invalid_target`、`invalid_body`、
    `sshkeygen_missing`、`sshkeygen_unavailable`、`timeout`、`sign_failed`、
    `agent_unavailable`、`bad_signature`。
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = str(reason)
        # detail 只由本模組以固定字串提供（不含子行程輸出）。
        self.detail = str(detail)
        super().__init__(self.detail or self.reason)

    def __repr__(self) -> str:  # 不印 detail（未來若帶入更多內容也不外洩）
        return "SignerError(reason={0!r})".format(self.reason)


def b64url(data: bytes) -> str:
    """無 padding 的 base64url（給 nonce 與簽章）。"""
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def build_signing_message(*, key_id: str, timestamp: str, nonce: str, method: str,
                          target: str, body: bytes = b"") -> bytes:
    """組 ASCII 八行（每行以 LF 結尾）；第 7 行原樣使用 `target`（不重編碼）。

    `body` 必須是 bytes；第 8 行是它的 SHA-256 小寫 hex（空 bytes 也算）。
    """
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise SignerError("invalid_body", "body must be bytes")
    raw = bytes(body)
    lines = (
        SIG_LINE_1,
        NAMESPACE,
        str(key_id),
        str(timestamp),
        str(nonce),
        str(method),
        str(target),
        hashlib.sha256(raw).hexdigest(),
    )
    for line in lines:
        if "\n" in line or "\r" in line:
            raise SignerError("invalid_target", "signing lines must not contain line breaks")
    try:
        return ("\n".join(lines) + "\n").encode("ascii")
    except UnicodeEncodeError:
        raise SignerError("invalid_target", "signing lines must be ASCII") from None


def _armor_to_b64url(text: str) -> str:
    """把 `ssh-keygen -Y sign` 的 armor 轉成 240 字元的 `X-LGW-Signature`（C-703）。

    解碼後必須恰為 180 bytes；不是 armor、base64 壞掉或長度不符都丟 `SignerError`。
    例外訊息與 repr 都不含 armor 內容。
    """
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if len(lines) < 3:
        raise SignerError(_BAD_SIGNATURE, "signature armor is malformed")
    if (not lines[0].startswith("-----BEGIN SSH SIGNATURE-----")
            or not lines[-1].startswith("-----END SSH SIGNATURE-----")):
        raise SignerError(_BAD_SIGNATURE, "signature armor is malformed")
    body = "".join(lines[1:-1])
    try:
        blob = base64.b64decode(body.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise SignerError(_BAD_SIGNATURE, "signature armor is not base64") from None
    if len(blob) != BLOB_BYTES:
        # 只回長度分類，不回內容（C-709）。
        raise SignerError(_BAD_SIGNATURE, "signature blob has the wrong length")
    return b64url(blob)


class Signer:
    """用本機 `ssh-keygen -Y sign` 簽 lgw 請求；一次 `headers()` 簽一個請求。

    只給一個金鑰來源：`private_key_path`（私鑰檔）或 `public_key_path`（公鑰檔，
    由 ssh-agent 簽）。`signer=None` 的客戶端行為不變（見 `lgw.client`）。
    """

    def __init__(self, *, key_id: str, private_key_path: str | None = None,
                 public_key_path: str | None = None,
                 sshkeygen: str = SSH_KEYGEN, timeout: float = DEFAULT_TIMEOUT,
                 agent_sock: str | None = None):
        if (private_key_path is None) == (public_key_path is None):
            raise ValueError("exactly one of private_key_path or public_key_path is required")
        self._key_id = str(key_id)
        self._private_key_path = (
            os.fspath(private_key_path) if private_key_path is not None else None)
        self._public_key_path = (
            os.fspath(public_key_path) if public_key_path is not None else None)
        self._sshkeygen = os.fspath(sshkeygen)
        self._timeout = float(timeout)
        self._agent_sock = (
            os.fspath(agent_sock) if agent_sock is not None else None)

    # --- 公開介面（⑥） ---

    def headers(self, *, method: str, target: str, body: bytes = b"",
                now: int | None = None) -> dict[str, str]:
        """簽一個請求，回五個 `X-LGW-*` 標頭（dict）。

        `target` 是**實際送出**的 request-target（含 query）；`body` 是**實際送出**
        的 bytes（GET 給空 bytes）。任何簽章失敗都丟 `SignerError`，呼叫方不得送出。
        """
        message, timestamp, nonce = self._message(
            method=method, target=target, body=body, now=now)
        signature = self._sign(message)
        return {
            "X-LGW-Auth-Version": "1",
            "X-LGW-Key-Id": self._key_id,
            "X-LGW-Timestamp": timestamp,
            "X-LGW-Nonce": nonce,
            "X-LGW-Signature": signature,
        }

    # --- 內部 ---

    def _message(self, *, method, target, body, now):
        if not _KEY_ID_RE.match(self._key_id):
            raise SignerError("invalid_key_id", "key id is not in the registry format")
        normalized_method = str(method).upper()
        if not _METHOD_RE.match(normalized_method):
            raise SignerError("invalid_method", "method must be ASCII letters")
        text_target = str(target)
        # origin-form：以單一 "/" 開頭、ASCII 可見字元、不含 fragment（design）。
        if (not text_target.startswith("/") or text_target.startswith("//")
                or "#" in text_target
                or any(not 0x21 <= ord(ch) <= 0x7E for ch in text_target)):
            raise SignerError("invalid_target", "target is not an ASCII origin-form path")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise SignerError("invalid_target", "timestamp must be non-negative")
        nonce = b64url(secrets.token_bytes(32))
        message = build_signing_message(
            key_id=self._key_id, timestamp=str(timestamp), nonce=nonce,
            method=normalized_method, target=text_target, body=body)
        return message, str(timestamp), nonce

    def _sign(self, message: bytes) -> str:
        keyfile = self._private_key_path or self._public_key_path
        assert keyfile is not None
        env = {
            "LC_ALL": "C",
            "PATH": BASE_PATH,
            "HOME": os.path.dirname(os.path.abspath(keyfile)),
        }
        if self._public_key_path is not None:
            sock = self._agent_sock or os.environ.get("SSH_AUTH_SOCK")
            if not sock:
                raise SignerError("agent_unavailable", "no ssh-agent socket")
            env["SSH_AUTH_SOCK"] = sock
        argv = [
            self._sshkeygen, "-Y", "sign", "-n", NAMESPACE, "-f", keyfile,
        ]
        try:
            completed = subprocess.run(
                argv, input=bytes(message), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, shell=False, timeout=self._timeout,
                # 脫離呼叫端的 controlling tty（新 session）：否則呼叫端有 tty 時，
                # 有密語的私鑰會讓 ssh-keygen 開 /dev/tty 提示並阻塞到逾時（C-704）。
                start_new_session=True,
            )
        except FileNotFoundError:
            raise SignerError("sshkeygen_missing", "ssh-keygen was not found") from None
        except subprocess.TimeoutExpired:
            raise SignerError("timeout", "ssh-keygen timed out") from None
        except OSError:
            raise SignerError("sshkeygen_unavailable", "ssh-keygen could not run") from None
        if completed.returncode != 0:
            # 包含有密語的私鑰（子行程已脫離 controlling tty，直接失敗不提示）與其他
            # 失敗；stderr 原文不傳出去。
            raise SignerError("sign_failed", "ssh-keygen could not sign")
        try:
            armor = completed.stdout.decode("ascii")
        except UnicodeDecodeError:
            raise SignerError(_BAD_SIGNATURE, "signature armor is not ASCII") from None
        return _armor_to_b64url(armor)

    def __repr__(self) -> str:  # 不含 key ID、nonce、簽章或金鑰內容（C-709）
        return "Signer(private_key_path={0!r}, public_key_path={1!r}, sshkeygen={2!r})".format(
            self._private_key_path, self._public_key_path, self._sshkeygen)
