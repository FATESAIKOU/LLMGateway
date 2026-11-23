"""SSHSIG 信封預解析、allowed_signers 與 `ssh-keygen -Y verify` 子行程（D2 PR-5）。

對應 `openspec/changes/lgw-resolve-auth/tasks.md` 的 D2 PR-5（C-501～C-507）與
design「SSHSIG 驗章（F3）」「向量產生程序」、spec「SSHSIG 驗章子行程」、REPORT-D2-arch
§2 PR-5 與 §3 PR-5 測試矩陣、REPORT-247-arch 的分串流 401 判定規則。

這是純新增的程式庫模組：`lgwd/main.py`、`lgwd/http.py` 都不 import 它，也沒有任何
執行路徑呼叫它，所以合併後 main 的行為逐 byte 不變。PR-6 的模式整合才會把
`Verifier`／`write_allowed_signers` 接上執行路徑。

對外介面：
- `preparse(sig_b64url, entry) -> bytes`：信封預解析，回傳 180 bytes 的 blob，否則
  丟 `Reject`。**不 fork**，這是 401 的安全邊界。
- `public_key_blob(entry)`／`fingerprint(entry)`：薄包裝，真正的實作在
  `lgwd.auth_registry.ssh_public_key_blob`／`fingerprint`（D5 也共用那一份；公鑰 blob
  與 fingerprint 只有一份，不會兩份算出差異）。
- `write_allowed_signers(entries, directory, generation) -> SignersFile`：原子寫入、
  讀回自檢、引用計數與換世代刪舊檔（C-502）。
- `classify(returncode, stdout, stderr, entry) -> Outcome`：**唯一的 401 出口**
  （C-504）。只比整份輸出，不比子字串。
- `Verifier`：`verify`（C-503～C-505）、`selftest`（C-506）、`recheck_if_changed`、
  `openssh_version`、`clean_spool`。
- `SELFTEST_VECTOR`：固定向量（與 `tests/vectors/sshsig_v1.json` 一致，測試會對）；
  啟動自測真的 exec 它。

錯誤分類一律是固定字串，不含輸入值、不含公鑰全文、不含 key ID；stderr／stdout 原文
不進日誌。除了 `lgwd.auth_registry`（公鑰 blob／fingerprint 與 `KeyEntry` 型別）與
`lgwd.subproc.signal_child`（killpg 護欄，design 依賴圖本來就有 sshsig→subproc）之外
只用標準函式庫；不 import `lgwd.http`／`lgwd.main`／`lgwd.registry`，也不用
`preexec_fn`（多執行緒服務裡 fork 後跑 Python 不安全，資源上限交給 `prlimit`）。
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from enum import Enum

from lgwd.auth_registry import (
    KeyEntry,
    fingerprint as _fingerprint32,
    ssh_public_key_blob,
)
from lgwd.subproc import signal_child

# ---------------------------------------------------------------------------
# 固定值（design「協議與選項」「SSHSIG 驗章」）
# ---------------------------------------------------------------------------

#: SSHSIG namespace，也是八行訊息的第 2 行（audience）；MLP sidecar 必須用不同的值。
NAMESPACE = "llmgateway"
NAMESPACE_BYTES = NAMESPACE.encode("ascii")

MAGIC = b"SSHSIG"
VERSION = 1
HASH_ALGORITHM = b"sha512"
SIGNATURE_TYPE = b"ssh-ed25519"
SIGNATURE_LEN = 64
PUBLIC_KEY_TYPE = b"ssh-ed25519"
BLOB_LEN = 180
SIG_B64URL_LEN = 240

#: Ed25519 的 group order L = 2^252 + 27742317777372353535851937790883648493。
#: S（簽章後 32 bytes，little-endian）必須 < L；OpenSSH 9.6 會接受 S+L 的延展簽章，
#: 所以這條必須在預解析擋下（C-501）。
ED25519_ORDER = 2 ** 252 + 27742317777372353535851937790883648493

#: 子行程輸出各最多讀這麼多 bytes；超過就判表外（C-503）。
MAX_OUTPUT = 4096

#: 等號誌＋執行合計的單次期限（C-505；design：2 秒，實測約 4 ms）。
DEFAULT_TIMEOUT = 2.0

#: 子行程並發上限（C-505；預設 16，不超過預驗章並發上限 32）。
DEFAULT_CONCURRENCY = 16

#: 最低支援的 OpenSSH 版本（C-506；分類表只在 9.6p1 實測）。
MIN_OPENSSH = (9, 6)

#: 分類用的固定字樣（C-504）。只認這兩句，不比子字串。
GOOD_TEMPLATE = 'Good "{ns}" signature for {key_id} with ED25519 key SHA256:{fp}'
GOOD_PREFIX = 'Good "%s" signature for ' % NAMESPACE
GOOD_MIDDLE = " with ED25519 key SHA256:"
INCORRECT = "Signature verification failed: incorrect signature"
COULD_NOT = "Could not verify signature."

#: 預設的固定絕對路徑（design：`auth_sshkeygen_path` 預設 `/usr/bin/ssh-keygen`）。
DEFAULT_SSHKEYGEN = "/usr/bin/ssh-keygen"
DEFAULT_PRLIMIT = "/usr/bin/prlimit"

#: 固定向量（C-507）。這份與 `tests/vectors/sshsig_v1.json` 逐欄相同，測試會斷言；
#: 啟動自測用它真的 exec 一次（C-506）。私鑰不存在於 repo、任何輸出或這份常數。
SELFTEST_VECTOR = {
    "schema": 1,
    "key_id": "test-key-236",
    "public_key": (
        "ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAINYhxE76ZZvw9CRPpiRc5ogKIXt9Q9prAyqedQtuN2Wg"
    ),
    "signing_data": (
        "LGW-HTTP-SIG-V1\n"
        "llmgateway\n"
        "test-key-236\n"
        "1795392000\n"
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8\n"
        "GET\n"
        "/v1/resolve?a=1&b=2\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    ),
    "signature": (
        "U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg1iHETvplm_D0JE-mJFzmiAohe31D"
        "2msDKp51C243ZaAAAAAKbGxtZ2F0ZXdheQAAAAAAAAAGc2hhNTEyAAAAUwAAAAtzc2gtZWQy"
        "NTUxOQAAAECnzk5p9broiwd56yl77-f0SSGANSK7ninxhGcGmfKXaUalWNRF7kKDgxlgc9tHl8"
        "LV_VVzLu1CH4eTj03wqgAM"
    ),
    "bad_signature": (
        "U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg1iHETvplm_D0JE-mJFzmiAohe31D"
        "2msDKp51C243ZaAAAAAKbGxtZ2F0ZXdheQAAAAAAAAAGc2hhNTEyAAAAUwAAAAtzc2gtZWQy"
        "NTUxOQAAAECmzk5p9broiwd56yl77-f0SSGANSK7ninxhGcGmfKXaUalWNRF7kKDgxlgc9tHl8"
        "LV_VVzLu1CH4eTj03wqgAM"
    ),
}

# ---------------------------------------------------------------------------
# 錯誤與結果型別
# ---------------------------------------------------------------------------

KIND_BAD_ENCODING = "bad_encoding"
KIND_BAD_LENGTH = "bad_length"
KIND_BAD_STRUCTURE = "bad_structure"
KIND_BAD_MAGIC = "bad_magic"
KIND_BAD_VERSION = "bad_version"
KIND_PUBLIC_KEY_MISMATCH = "public_key_mismatch"
KIND_BAD_NAMESPACE = "bad_namespace"
KIND_RESERVED_NOT_EMPTY = "reserved_not_empty"
KIND_BAD_HASH_ALGORITHM = "bad_hash_algorithm"
KIND_BAD_SIGNATURE_TYPE = "bad_signature_type"
KIND_SIGNATURE_NOT_CANONICAL = "signature_not_canonical"
KIND_TRAILING_BYTES = "trailing_bytes"
KIND_VERIFIER_UNAVAILABLE = "verifier_unavailable"


class Reject(ValueError):
    """信封預解析的拒絕。`kind` 是固定分類字串（不含輸入值）。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class VerifierUnavailable(RuntimeError):
    """驗章工具或 allowed_signers 這條路徑不可用（呼叫端在 enforce 回 503）。"""

    kind = KIND_VERIFIER_UNAVAILABLE


class Outcome(str, Enum):
    """驗章結果。只有 `BAD_SIG` 會落到 401（`classify` 是唯一出口）。"""

    PASS = "pass"
    BAD_SIG = "bad_signature"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# 公鑰 blob 與 fingerprint（實作在 lgwd.auth_registry，這裡只做 KeyEntry 包裝）
# ---------------------------------------------------------------------------


def public_key_blob(entry: KeyEntry) -> bytes:
    """由驗證後的 32 bytes 重建規範的 OpenSSH 公鑰 blob。

    實作只有一份：`lgwd.auth_registry.ssh_public_key_blob`（D5 的 fingerprint 也用它）。
    `auth_registry.validate_ed25519_pubkey` 只接受規範編碼，所以重建的 blob 與名冊裡
    原本的 blob 逐 byte 相同；預解析用它和信封內的公鑰比對（C-501）。
    """
    return ssh_public_key_blob(entry.public_key)


def fingerprint(entry: KeyEntry) -> str:
    """`base64(sha256(公鑰 blob))`，去掉 padding；分類時與 Good 行比對（C-504）。

    實作只有一份：`lgwd.auth_registry.fingerprint`。
    """
    return _fingerprint32(entry.public_key)


def good_line(entry: KeyEntry) -> str:
    """通過時 stdout 必須逐字等於的那一行（C-504）。"""
    return GOOD_TEMPLATE.format(
        ns=NAMESPACE, key_id=entry.key_id, fp=fingerprint(entry)
    )


def _entry_from_public_line(line: str, *, principal: str = "selftest") -> KeyEntry:
    """把 OpenSSH 公鑰行轉成 `KeyEntry`（只給自測與測試用）。"""
    parts = line.split()
    if len(parts) < 2 or parts[0] != PUBLIC_KEY_TYPE.decode("ascii"):
        raise ValueError("public key line")
    blob = base64.b64decode(parts[1], validate=True)
    if len(blob) != 4 + len(PUBLIC_KEY_TYPE) + 4 + 32:
        raise ValueError("public key length")
    return KeyEntry(
        key_id=SELFTEST_VECTOR["key_id"],
        principal=principal,
        public_key=blob[-32:],
        status="active",
    )


# ---------------------------------------------------------------------------
# C-501 信封預解析
# ---------------------------------------------------------------------------

_B64URL = re.compile(r"[A-Za-z0-9_-]+\Z")


def _u32(buf: bytes, pos: int) -> int:
    if pos + 4 > len(buf):
        raise Reject(KIND_BAD_STRUCTURE)
    return int.from_bytes(buf[pos:pos + 4], "big")


def _read_string(buf: bytes, pos: int) -> tuple[bytes, int]:
    length = _u32(buf, pos)
    start = pos + 4
    end = start + length
    if end > len(buf):
        raise Reject(KIND_BAD_STRUCTURE)
    return buf[start:end], end


def _decode_b64url(sig_b64url: str) -> bytes:
    """嚴格解 `X-LGW-Signature`：無 padding、240 字元、解碼後再編碼必須相同。"""
    if not isinstance(sig_b64url, str):
        raise Reject(KIND_BAD_ENCODING)
    if len(sig_b64url) != SIG_B64URL_LEN:
        raise Reject(KIND_BAD_LENGTH)
    if not _B64URL.match(sig_b64url):
        raise Reject(KIND_BAD_ENCODING)
    try:
        blob = base64.urlsafe_b64decode(sig_b64url + "==")
    except (binascii.Error, ValueError):
        raise Reject(KIND_BAD_ENCODING) from None
    if base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii") != sig_b64url:
        raise Reject(KIND_BAD_ENCODING)
    return blob


def preparse(sig_b64url: str, entry: KeyEntry) -> bytes:
    """C-501：信封預解析，成功回傳 180 bytes 的 blob，否則丟 `Reject`（不 fork）。

    依序：base64url（無 padding、往返相同、長度 240）→ 180 bytes → 魔數／版本 →
    公鑰 blob 與 `entry` 逐 byte 相同 → namespace → reserved 空 → `sha512` →
    簽章型別 `ssh-ed25519`、64 bytes、S < L → 沒有多餘 bytes。
    """
    blob = _decode_b64url(sig_b64url)
    if len(blob) != BLOB_LEN:
        raise Reject(KIND_BAD_LENGTH)

    pos = 0
    magic = blob[pos:pos + 6]
    pos += 6
    if magic != MAGIC:
        raise Reject(KIND_BAD_MAGIC)
    version = _u32(blob, pos)
    pos += 4
    if version != VERSION:
        raise Reject(KIND_BAD_VERSION)

    public_key, pos = _read_string(blob, pos)
    if public_key != public_key_blob(entry):
        raise Reject(KIND_PUBLIC_KEY_MISMATCH)

    namespace, pos = _read_string(blob, pos)
    if namespace != NAMESPACE_BYTES:
        raise Reject(KIND_BAD_NAMESPACE)

    reserved, pos = _read_string(blob, pos)
    if reserved != b"":
        raise Reject(KIND_RESERVED_NOT_EMPTY)

    hash_algorithm, pos = _read_string(blob, pos)
    if hash_algorithm != HASH_ALGORITHM:
        raise Reject(KIND_BAD_HASH_ALGORITHM)

    signature_blob, pos = _read_string(blob, pos)
    if pos != len(blob):
        raise Reject(KIND_TRAILING_BYTES)

    sig_pos = 0
    sig_type, sig_pos = _read_string(signature_blob, sig_pos)
    if sig_type != SIGNATURE_TYPE:
        raise Reject(KIND_BAD_SIGNATURE_TYPE)
    signature, sig_pos = _read_string(signature_blob, sig_pos)
    if sig_pos != len(signature_blob):
        raise Reject(KIND_TRAILING_BYTES)
    if len(signature) != SIGNATURE_LEN:
        raise Reject(KIND_BAD_LENGTH)
    # OpenSSH 9.6 會接受 S+L 的延展簽章（只擋最高 3 位元），所以這條由預解析擋。
    if int.from_bytes(signature[32:], "little") >= ED25519_ORDER:
        raise Reject(KIND_SIGNATURE_NOT_CANONICAL)
    return blob


# ---------------------------------------------------------------------------
# C-502 allowed_signers
# ---------------------------------------------------------------------------


def _format_signers_line(entry: KeyEntry) -> str:
    """`<key ID> namespaces="llmgateway" ssh-ed25519 <base64>`（C-502）。"""
    blob_b64 = base64.b64encode(public_key_blob(entry)).decode("ascii")
    return '%s namespaces="%s" %s %s' % (
        entry.key_id,
        NAMESPACE,
        PUBLIC_KEY_TYPE.decode("ascii"),
        blob_b64,
    )


def _stat_id(path: str) -> tuple[int, int, int, int]:
    """回傳 `(dev, ino, size, mtime_ns)`；檔案不見時丟 `OSError`。"""
    st = os.stat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class SignersFile:
    """一份 `allowed_signers.<generation>` 與它的引用計數（C-502）。

    請求用自己讀到的那份快照的檔；換世代後舊檔在引用歸零時才刪除，避免在途請求
    讀到新檔而誤判。`stat_id` 是換上後記下的 `(dev, ino, size, mtime_ns)`，
    `Verifier.verify` 每次驗章前 stat 比對（被外部改過＝503）。

    引用責任：**呼叫端在拿到快照後、可能被換世代之前就必須 `acquire()`**（PR-6 在
    同一把鎖內取快照並 acquire），並持有到請求結束才 `release()`。`Verifier.verify`
    **不**自己 acquire／release，只以 `refs >= 1` 當前置信檢查：呼叫端沒持有就判
    UNAVAILABLE、不 fork。`acquire()` 對已 `retire()` 的檔回 `False`（檔案可能已被
    刪），呼叫端要判 UNAVAILABLE，不要再拿這個路徑去 exec。

    `acquire`／`release`／`retire` 全部在同一把 `threading.Lock` 內改計數與刪檔，
    所以請求執行緒並發進出也不會遺失更新或提早刪檔。
    """

    def __init__(self, path: str, generation: int, stat_id: tuple[int, int, int, int],
                 entries: tuple[KeyEntry, ...]):
        self.path = path
        self.generation = generation
        self.stat_id = stat_id
        self.entries = entries
        self._lock = threading.Lock()
        self._refs = 0
        self._retired = False

    def acquire(self) -> bool:
        """取得一個引用；已 retire（可能已刪檔）回 `False`，呼叫端判 UNAVAILABLE。"""
        with self._lock:
            if self._retired:
                return False
            self._refs += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._refs <= 0:
                return
            self._refs -= 1
            if self._refs == 0 and self._retired:
                self._unlink()

    def retire(self) -> None:
        """標記這個世代已換掉；沒有在途引用就立刻刪檔。"""
        with self._lock:
            self._retired = True
            if self._refs == 0:
                self._unlink()

    def _unlink(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def __enter__(self) -> "SignersFile":
        if not self.acquire():
            raise VerifierUnavailable("signers_retired")
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    @property
    def refs(self) -> int:
        """目前引用數（測試與除錯用；讀取也在鎖內）。"""
        with self._lock:
            return self._refs

    def __repr__(self) -> str:  # pragma: no cover - 只給除錯
        return "SignersFile(%r, generation=%d, refs=%d)" % (
            self.path, self.generation, self._refs)


def _read_lines(path: str) -> list[str]:
    with open(path, "rb") as handle:
        raw = handle.read()
    return raw.decode("utf-8", "replace").splitlines()


_SIGNERS_LINE = re.compile(
    r'([A-Za-z0-9][A-Za-z0-9._-]{0,63}) namespaces="%s" ssh-ed25519 '
    r'([A-Za-z0-9+/]+={0,2})\Z' % NAMESPACE
)


def _signers_self_check(path: str, active: tuple[KeyEntry, ...]) -> bool:
    """獨立於產生端驗回寫的檔：行數、逐行格式、key ID 與公鑰一一相同（C-502）。

    這裡**不**拿產生端的字串直接比，而是重新解析每一行；所以產生端若寫錯格式，
    自檢就會失敗（不是自己跟自己比）。
    """
    try:
        lines = _read_lines(path)
    except OSError:
        return False
    if len(lines) != len(active):
        return False
    seen: list[tuple[str, bytes]] = []
    for line in lines:
        match = _SIGNERS_LINE.match(line)
        if match is None:
            return False
        key_id, blob_b64 = match.groups()
        try:
            blob = base64.b64decode(blob_b64, validate=True)
        except (binascii.Error, ValueError):
            return False
        seen.append((key_id, blob))
    expected = [(entry.key_id, public_key_blob(entry)) for entry in active]
    return seen == expected


def write_allowed_signers(entries, directory, generation: int) -> SignersFile:
    """C-502：由快照的有效 key 產生 allowed_signers，原子寫入並讀回自檢。

    `entries` 可以是 `Registry.active_keys()` 或整份 `registry.keys`（已撤銷的會濾掉）。
    檔名 `allowed_signers.<generation>`、權限 0600、同目錄暫存檔＋fsync＋rename。
    rename 前把暫存檔讀回逐行驗證；不符就**不換檔**，丟 `VerifierUnavailable`。
    """
    directory = os.fspath(directory)
    active = tuple(entry for entry in entries if entry.active)
    expected = [_format_signers_line(entry) for entry in active]
    target = os.path.join(directory, "allowed_signers.%d" % generation)

    if not os.path.isdir(directory):
        raise VerifierUnavailable("signers directory")

    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".allowed_signers.%d." % generation, suffix=".tmp")
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
        data = "".join(line + "\n" for line in expected).encode("utf-8")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # 讀回自檢：行數、逐行格式、key ID 與公鑰都必須與快照一一相同。
        if not _signers_self_check(tmp_path, active):
            raise VerifierUnavailable("signers self-check")
        os.replace(tmp_path, target)
        tmp_path = None
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    return SignersFile(target, generation, _stat_id(target), active)


# ---------------------------------------------------------------------------
# C-504 分類（唯一的 401 出口）
# ---------------------------------------------------------------------------


def _lines(raw: bytes) -> list[str]:
    """以 LF 切行、去掉 CR 與空的末行（C-504）。其餘原樣保留。"""
    lines = raw.decode("utf-8", "replace").split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _parse_good(line: str) -> tuple[str, str] | None:
    if not line.startswith(GOOD_PREFIX):
        return None
    rest = line[len(GOOD_PREFIX):]
    key_id, sep, tail = rest.partition(GOOD_MIDDLE)
    if not sep or not key_id or not tail:
        return None
    return key_id, tail


def classify(returncode: int, stdout: bytes, stderr: bytes,
             entry: KeyEntry) -> Outcome:
    """C-504：把子行程結果分類成 `Outcome`，**這是唯一的 401 出口**。

    - PASS：exit 0，stdout 恰一行 Good 行，key ID 與 fingerprint 都相符，stderr 空。
    - BAD_SIG：exit 255，stderr 整份等於 `incorrect signature` 一行（或再加一行
      `Could not verify signature.`），而且 stdout 空或恰好是 `Could not verify
      signature.`，該行兩串流合計至多一次。
    - 其餘一律 UNAVAILABLE（含只有 `Could not verify signature.` 的兩種）。
    """
    out_lines = _lines(stdout)
    err_lines = _lines(stderr)

    if returncode == 0:
        if len(out_lines) == 1 and not err_lines:
            parsed = _parse_good(out_lines[0])
            if parsed is not None:
                key_id, fp = parsed
                if key_id == entry.key_id and fp == fingerprint(entry):
                    return Outcome.PASS
        return Outcome.UNAVAILABLE

    if returncode == 255:
        could_not_count = out_lines.count(COULD_NOT) + err_lines.count(COULD_NOT)
        stderr_ok = err_lines == [INCORRECT] or err_lines == [INCORRECT, COULD_NOT]
        stdout_ok = out_lines == [] or out_lines == [COULD_NOT]
        if stderr_ok and stdout_ok and could_not_count <= 1:
            return Outcome.BAD_SIG

    return Outcome.UNAVAILABLE


# ---------------------------------------------------------------------------
# C-503／C-505／C-506 子行程與自測
# ---------------------------------------------------------------------------


def _armor(blob: bytes) -> bytes:
    """把 180 bytes 的 blob 重新包成 `ssh-keygen -Y sign` 的 armor（C-503）。"""
    body = base64.b64encode(blob).decode("ascii")
    lines = [body[pos:pos + 70] for pos in range(0, len(body), 70)]
    return (
        "-----BEGIN SSH SIGNATURE-----\n"
        + "\n".join(lines)
        + "\n-----END SSH SIGNATURE-----\n"
    ).encode("ascii")


def _kill_group(process: subprocess.Popen) -> None:
    """逾時只殺自己 spawn 的行程群（`start_new_session=True`，pgid＝pid）。

    交給 `lgwd.subproc.signal_child`（低-3）：用 spawn 當下記下的 pid 當 pgid、多一條
    「不打自己行程群」的護欄，不在這裡重寫一份 killpg。
    """
    signal_child(process, signal.SIGKILL)


class Verifier:
    """`ssh-keygen -Y verify` 的執行器：號誌、總期限、最小環境、暫存檔即刪。

    `spool_dir` **必須是專用目錄**（`auth_paths.sshsig_dir`；不要傳 `state_dir`）：
    簽章暫存檔與自測目錄都建在這裡，`clean_spool()` 只認自己的命名樣式。

    建構子參數的 uid 檢查（`owner_uid`）只給測試用，不開 CLI 或環境變數（C-506）；
    正式路徑由 PR-6 以預設值建立。
    """

    def __init__(self, *, sshkeygen_path: str = DEFAULT_SSHKEYGEN,
                 prlimit_path: str = DEFAULT_PRLIMIT,
                 spool_dir: str, home_dir: str | None = None,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 timeout: float = DEFAULT_TIMEOUT,
                 owner_uid: int = 0):
        if concurrency < 1:
            raise ValueError("concurrency")
        if timeout <= 0:
            raise ValueError("timeout")
        self._sshkeygen_path = os.fspath(sshkeygen_path)
        self._prlimit_path = os.fspath(prlimit_path)
        self._spool_dir = os.fspath(spool_dir)
        self._home_dir = os.fspath(home_dir if home_dir is not None else spool_dir)
        if os.path.isdir(self._spool_dir):
            mode = stat.S_IMODE(os.stat(self._spool_dir).st_mode)
            if mode & 0o077:
                raise ValueError("spool_dir must be private (0700)")
        self._timeout = float(timeout)
        self._owner_uid = int(owner_uid)
        self._semaphore = threading.BoundedSemaphore(concurrency)
        self._healthy: bool | None = None
        self._binary_id: tuple[int, int, int, int] | None = None
        self._version_unknown = False

    # -- 狀態 ------------------------------------------------------------

    @property
    def healthy(self) -> bool | None:
        """`None`＝還沒跑過自測；`False`＝自測失敗，驗章一律 503 直到再通過。"""
        return self._healthy

    @property
    def spool_dir(self) -> str:
        return self._spool_dir

    @property
    def version_unknown(self) -> bool:
        """上次自測時讀不到 `ssh -V` 版本（C-506；PR-6 據此記 `openssh_version_unknown`）。"""
        return self._version_unknown

    def _env(self) -> dict[str, str]:
        # 恰好這三個（C-503）；不傳 SSH_AUTH_SOCK 等其他變數。
        return {"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": self._home_dir}

    # -- 清理 ------------------------------------------------------------

    def clean_spool(self) -> int:
        """清掉本模組自己的殘檔，回傳刪了幾個項目（PR-6 只在啟動時呼叫一次）。

        spool **必須是專用目錄**（`auth_paths.sshsig_dir`，與 `auth_paths.state_dir`
        分開）。這裡只刪自己的命名樣式：`sig-*.asc` 一般檔／symlink 與 `selftest-*`
        目錄；其他檔一律不碰，所以即使誤把別的目錄當成 spool，也不會刪掉別人的檔。
        `selftest()` **不**呼叫本函式：在途請求的 `sig-*.asc` 才不會被執行中的自測
        刪掉（套件升級觸發 `recheck_if_changed` 時）。
        """
        removed = 0
        try:
            names = os.listdir(self._spool_dir)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(self._spool_dir, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    if name.startswith("selftest-"):
                        shutil.rmtree(path, ignore_errors=True)
                        removed += 1
                    continue
                if not (name.startswith("sig-") and name.endswith(".asc")):
                    continue
                if os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        return removed

    # -- C-503 子行程 ----------------------------------------------------

    def _argv(self, key_id: str, signers_path: str, signature_path: str) -> list[str]:
        """argv 固定；`-I` 取快照的 entry key ID、`-n` 是常數，都不取請求 bytes。"""
        return [
            self._prlimit_path,
            "--cpu=2",
            "--as=268435456",
            "--nofile=32",
            "--",
            self._sshkeygen_path,
            "-Y", "verify",
            "-f", signers_path,
            "-I", key_id,
            "-n", NAMESPACE,
            "-s", signature_path,
        ]

    def _run(self, message: bytes, blob: bytes, entry: KeyEntry,
             signers_path: str, timeout: float) -> Outcome:
        """真的 exec 一次。暫存簽章檔一定在 `finally` 刪除。"""
        if blob is None or len(blob) != BLOB_LEN:
            return Outcome.UNAVAILABLE
        signature_path = None
        try:
            fd, signature_path = tempfile.mkstemp(
                dir=self._spool_dir, prefix="sig-", suffix=".asc")
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                os.close(fd)
                return Outcome.UNAVAILABLE
            payload = _armor(blob)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()

            with tempfile.TemporaryFile(dir=self._spool_dir) as out_file, \
                    tempfile.TemporaryFile(dir=self._spool_dir) as err_file:
                try:
                    process = subprocess.Popen(
                        self._argv(entry.key_id, signers_path, signature_path),
                        stdin=subprocess.PIPE,
                        stdout=out_file,
                        stderr=err_file,
                        env=self._env(),
                        close_fds=True,
                        start_new_session=True,
                        cwd=self._spool_dir,
                    )
                except OSError:
                    return Outcome.UNAVAILABLE
                try:
                    process.communicate(message, timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_group(process)
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
                    return Outcome.UNAVAILABLE
                out_file.seek(0)
                stdout = out_file.read(MAX_OUTPUT + 1)
                err_file.seek(0)
                stderr = err_file.read(MAX_OUTPUT + 1)
            if len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT:
                return Outcome.UNAVAILABLE  # 表外輸出：不可能是這兩種正常形式
            return classify(process.returncode, stdout, stderr, entry)
        except OSError:
            return Outcome.UNAVAILABLE
        finally:
            if signature_path is not None:
                try:
                    os.unlink(signature_path)
                except FileNotFoundError:
                    pass

    def _execute(self, message: bytes, blob: bytes, entry: KeyEntry,
                 signers_path: str) -> Outcome:
        """等號誌＋執行共用一個總期限（C-505）；逾時判 UNAVAILABLE。"""
        deadline = time.monotonic() + self._timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._semaphore.acquire(timeout=remaining):
            return Outcome.UNAVAILABLE
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Outcome.UNAVAILABLE
            return self._run(message, blob, entry, signers_path, remaining)
        finally:
            self._semaphore.release()

    def verify(self, message: bytes, blob: bytes, entry: KeyEntry,
               signers: SignersFile) -> Outcome:
        """驗一個已預解析的 blob；`signers` 是該請求快照的那一份（C-502／C-504）。

        **呼叫端必須在整個 verify 期間持有 `signers` 的引用**：取快照時就
        `acquire()`，請求結束（含本函式返回後）才 `release()`；見 `SignersFile` 的
        說明。這裡**不**自己 acquire／release，只以 `signers.refs >= 1` 當前置檢查：
        呼叫端沒持有就判 UNAVAILABLE、不 fork，讓漏持有立刻現形（中-4）。呼叫端的
        引用同時保證本函式執行期間檔案不會被刪。

        `entry` 必須是這份快照裡的 key（低-1）：`-I` 與 allowed_signers 同源，不一致
        就判 UNAVAILABLE、不 fork。allowed_signers 在使用前 stat 比對
        `(dev, ino, size, mtime_ns)`，被外部改過就判 UNAVAILABLE，不開子行程。
        """
        if self._healthy is False:
            return Outcome.UNAVAILABLE
        if not isinstance(message, (bytes, bytearray, memoryview)):
            return Outcome.UNAVAILABLE
        if not isinstance(blob, (bytes, bytearray, memoryview)) or len(blob) != BLOB_LEN:
            return Outcome.UNAVAILABLE
        if entry not in signers.entries:
            return Outcome.UNAVAILABLE
        if signers.refs < 1:
            # 中-4：呼叫端沒持有引用（快照與 verify 之間的窗口可能已被換世代），
            # 不拿路徑去 exec。
            return Outcome.UNAVAILABLE
        try:
            current = _stat_id(signers.path)
        except OSError:
            return Outcome.UNAVAILABLE
        if current != signers.stat_id:
            return Outcome.UNAVAILABLE
        return self._execute(bytes(message), bytes(blob), entry, signers.path)

    # -- C-506 自測 ------------------------------------------------------

    def openssh_version(self) -> tuple[int, int] | None:
        """讀同目錄 `ssh -V` 的版本；讀不到或解析不了回 None（記 `openssh_version_unknown`）。"""
        ssh_path = os.path.join(os.path.dirname(self._sshkeygen_path), "ssh")
        try:
            process = subprocess.run(
                [ssh_path, "-V"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env(),
                timeout=self._timeout,
                close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = (process.stdout + process.stderr).decode("ascii", "replace")
        match = re.search(r"OpenSSH_(\d+)\.(\d+)", text)
        if match is None:
            return None
        return (int(match.group(1)), int(match.group(2)))

    def _check_binary(self) -> bool:
        try:
            st = os.stat(self._sshkeygen_path)
        except OSError:
            return False
        if not os.path.isfile(self._sshkeygen_path):
            return False
        if not os.access(self._sshkeygen_path, os.X_OK):
            return False
        if st.st_uid != self._owner_uid:
            return False
        if st.st_mode & 0o022:
            return False
        return True

    def _version_ok(self) -> bool:
        version = self.openssh_version()
        if version is None:
            self._version_unknown = True
            return True  # 讀不到就以自測為準（C-506）
        # 低-4：重跑自測時讀回正常版本要把「未知」重設，否則一次讀不到就永遠標記。
        self._version_unknown = False
        return version >= MIN_OPENSSH

    def _selftest_entry(self) -> KeyEntry:
        return _entry_from_public_line(SELFTEST_VECTOR["public_key"])

    def _run_selftest_cases(self) -> bool:
        """四個案例都必須符合分類表，否則自測失敗（C-506；字樣守門）。"""
        entry = self._selftest_entry()
        good_blob = base64.urlsafe_b64decode(SELFTEST_VECTOR["signature"] + "==")
        bad_blob = base64.urlsafe_b64decode(SELFTEST_VECTOR["bad_signature"] + "==")
        message = SELFTEST_VECTOR["signing_data"].encode("ascii")
        directory = None
        try:
            directory = tempfile.mkdtemp(dir=self._spool_dir, prefix="selftest-")
            os.chmod(directory, 0o700)
            good_signers = os.path.join(directory, "good.allowed")
            with open(good_signers, "w", encoding="ascii") as handle:
                handle.write(_format_signers_line(entry) + "\n")
            missing_signers = os.path.join(directory, "missing.allowed")
            other = KeyEntry(key_id="selftest-absent", principal="selftest",
                             public_key=entry.public_key, status="active")
            with open(missing_signers, "w", encoding="ascii") as handle:
                handle.write(_format_signers_line(other) + "\n")
            broken_signers = os.path.join(directory, "broken.allowed")
            with open(broken_signers, "w", encoding="ascii") as handle:
                handle.write("this is not an allowed_signers file\n")

            cases = (
                (message, good_blob, entry, good_signers, Outcome.PASS),
                (message, bad_blob, entry, good_signers, Outcome.BAD_SIG),
                (message, good_blob, entry, missing_signers, Outcome.UNAVAILABLE),
                (message, good_blob, entry, broken_signers, Outcome.UNAVAILABLE),
            )
            for msg, blob, case_entry, signers_path, expected in cases:
                if self._selftest_verify(msg, blob, case_entry, signers_path) != expected:
                    return False
            return True
        except OSError:
            return False
        finally:
            if directory is not None:
                for name in os.listdir(directory):
                    try:
                        os.unlink(os.path.join(directory, name))
                    except OSError:
                        pass
                try:
                    os.rmdir(directory)
                except OSError:
                    pass

    def _selftest_verify(self, message: bytes, blob: bytes, entry: KeyEntry,
                         signers_path: str) -> Outcome:
        """自測用：直接走號誌與子行程，但 signers 是自測自己寫的檔（不做 stat 比對）。"""
        deadline = time.monotonic() + self._timeout
        if not self._semaphore.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return Outcome.UNAVAILABLE
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Outcome.UNAVAILABLE
            return self._run(message, blob, entry, signers_path, remaining)
        finally:
            self._semaphore.release()

    def selftest(self) -> bool:
        """C-506：路徑／擁有者／權限、版本、四個子行程案例；結果存成 `healthy`。

        **不**清 spool（`clean_spool` 只由啟動流程呼叫一次）；在途請求的簽章檔不受
        影響。
        """
        try:
            self._binary_id = _stat_id(self._sshkeygen_path)
        except OSError:
            self._binary_id = None
        ok = self._check_binary() and self._version_ok() and self._run_selftest_cases()
        self._healthy = ok
        return ok

    def recheck_if_changed(self) -> bool:
        """`(mtime_ns, size, ino)` 沒變就沿用上次結果；變了（例如套件升級）就重跑自測。"""
        try:
            current = _stat_id(self._sshkeygen_path)
        except OSError:
            current = None
        if current == self._binary_id and self._healthy is not None:
            return self._healthy
        return self.selftest()
