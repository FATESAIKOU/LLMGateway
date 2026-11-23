"""名冊 parser 與 Ed25519 公鑰驗證（D2 PR-2；tasks C-201～C-207）。

這個模組是純新增的：`lgwd/main.py`、`lgwd/http.py` 都不 import 它，也沒有任何執行
路徑呼叫它，所以合併後 main 的行為逐 byte 不變。D5 的 `auth-registry check|install`
與 PR-4 的 reload 之後都共用這裡的同一個驗證函式（design「名冊公鑰驗證（F1）」）。

對外介面：
- `validate_ed25519_pubkey(blob) -> bytes`：唯一的公鑰驗證函式，回傳驗證後的 32 bytes。
- `ssh_public_key_blob(public_key32) -> bytes`：由驗證後的 32 bytes 重建規範的 OpenSSH
  公鑰 blob（D2 PR-5 的預解析／allowed_signers 與 D5 的 fingerprint 共用這一份）。
- `fingerprint(public_key32) -> str`：`base64(sha256(公鑰 blob))` 去掉 padding（OpenSSH
  的 `SHA256:` fingerprint）。只 import hashlib／base64，不帶進子行程或 spool。
- `parse_registry(raw) -> Registry`：嚴格解析 design「名冊檔格式」schema 1。
- `read_registry_file(path, *, owner_uid=0) -> bytes`：只做檔案檢查與限量讀取，回傳原始
  bytes（不解析；內容好壞由呼叫端決定）。
- `load_registry_file(path, *, owner_uid=0) -> (raw, Registry)`：`read_registry_file`＋
  `parse_registry` 的便利函式。
- `content_hash(raw) -> str`：`sha256:` ＋原始 bytes 的小寫 hex（不做正規化）。
- `check_tombstones(registry, tombstones)`：撤銷墓碑比對，違反丟 `RegistryError`。

錯誤：一律是固定分類字串（`kind`／`reason`），訊息不含輸入值、不含公鑰全文、不含
key ID；呼叫端（PR-4 的 status 檔、D5 的 CLI）只依這些字串分類。只 import 標準
函式庫、`lgwd.auth_paths`（`MAX_GENERATION` 的唯一來源，後者只 import `lgwd.clock`）
與 `lgwd.policy`（`AGENTS`／`ROLES` 的唯一來源）；不 import HTTP／main 模組或
sqlite 驅動，也不做任何曲線運算（小階點用常數表，獨立推導只在測試）。
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from lgwd.auth_paths import MAX_GENERATION  # `generation` 範圍的唯一來源（C-203）
from lgwd.policy import AGENTS, ROLES

#: 名冊檔大小上限（design「名冊檔格式」；C-207）。
MAX_REGISTRY_BYTES = 1024 * 1024

#: 名冊 schema 版本（design G-1；改版本要升版並另審）。
SCHEMA_VERSION = 1

#: principal ID 與 key ID 的格式（design「名冊檔格式」；C-203）。
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: principal 的 `ops` 允許值（design；C-203）。
OPS = ("list", "usage", "roles", "resolve", "cooldown")

# 錯誤分類（C-405 的介面白名單用同一組字串；這裡只用其中屬於本模組的幾個）。
KIND_PARSE_ERROR = "parse_error"
KIND_INVALID_KEY = "invalid_key"
KIND_DUPLICATE = "duplicate"
KIND_TOO_LARGE = "too_large"
KIND_INSECURE_FILE = "insecure_file"
KIND_TOMBSTONE_VIOLATION = "tombstone_violation"
KIND_IO_ERROR = "io_error"

# 公鑰拒絕的固定原因（C-201：固定字串、不含輸入值）。
REASON_NOT_ED25519 = "not_ssh_ed25519"
REASON_INVALID_LENGTH = "invalid_length"
REASON_NONCANONICAL_Y = "noncanonical_y"
REASON_ZERO_X_SIGN_BIT = "zero_x_with_sign_bit"
REASON_SMALL_ORDER = "small_order_point"

_ED25519_TYPE = b"ssh-ed25519"
_P = 2**255 - 19
_SIGN_BIT = 1 << 255
_X_ZERO_YS = (1, _P - 1)

#: Ed25519 的 8 個小階點（規範編碼，32 bytes）。常數表是程式唯一的判斷依據；
#: `tests/test_auth_registry.py` 用 test-only 的純 Python 曲線運算獨立推導後逐一
#: 比對（C-202），`lgwd` 不 import 那段程式。
SMALL_ORDER_PUBKEYS = frozenset({
    bytes.fromhex("0100000000000000000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000080"),
    bytes.fromhex("ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"),
    bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
    bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85"),
    bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
    bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"),
})


class InvalidPublicKey(ValueError):
    """公鑰不合 F1。`reason` 是固定字串；訊息與它相同，不含輸入值。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RegistryError(ValueError):
    """名冊解析／檔案／墓碑的拒絕。`kind` 是固定分類字串（C-405 白名單的一部分）。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _u32(buf: bytes, pos: int) -> int:
    if pos + 4 > len(buf):
        raise InvalidPublicKey(REASON_INVALID_LENGTH)
    return int.from_bytes(buf[pos:pos + 4], "big")


def validate_ed25519_pubkey(blob: bytes) -> bytes:
    """驗證 OpenSSH 公鑰 blob，回傳規範編碼的 32 bytes（C-201）。

    blob 必須恰好是 `string "ssh-ed25519"` ＋ `string(32)`，沒有多餘 bytes；32 bytes
    去掉符號位元後的 y 必須 < 2^255−19；y 為 1 或 2^255−20（x＝0）時符號位元必須是
    0；不得是 8 個小階點之一。其他型別（RSA、ECDSA、`sk-*`、`*-cert-v01@openssh.com`）
    與任何長度不符都以固定原因拒絕。不在曲線上的點不另外擋（design：沒有私鑰的人
    造不出能驗過的簽章）。
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise InvalidPublicKey(REASON_INVALID_LENGTH)
    blob = bytes(blob)
    type_len = _u32(blob, 0)
    if 4 + type_len > len(blob):
        raise InvalidPublicKey(REASON_INVALID_LENGTH)
    if blob[4:4 + type_len] != _ED25519_TYPE:
        raise InvalidPublicKey(REASON_NOT_ED25519)
    pos = 4 + type_len
    key_len = _u32(blob, pos)
    if key_len != 32 or pos + 4 + key_len != len(blob):
        # 多 1 byte、少 1 byte、鍵長不是 32 都在這裡。
        raise InvalidPublicKey(REASON_INVALID_LENGTH)
    raw = blob[pos + 4:pos + 4 + key_len]
    encoded = int.from_bytes(raw, "little")
    sign = encoded >> 255
    y = encoded & (_SIGN_BIT - 1)
    if y >= _P:
        raise InvalidPublicKey(REASON_NONCANONICAL_Y)
    if y in _X_ZERO_YS and sign:
        raise InvalidPublicKey(REASON_ZERO_X_SIGN_BIT)
    if raw in SMALL_ORDER_PUBKEYS:
        raise InvalidPublicKey(REASON_SMALL_ORDER)
    return raw


def ssh_public_key_blob(public_key32) -> bytes:
    """由驗證後的 32 bytes 重建規範的 OpenSSH 公鑰 blob（C-201；D2 PR-5 共用）。

    `validate_ed25519_pubkey` 只接受規範編碼，所以重建的 blob 與名冊裡原本的 blob 逐
    byte 相同；SSHSIG 的預解析用它和信封內的公鑰比對（C-501）。長度不是 32 bytes 時
    以 `invalid_length` 拒絕。這是唯一一份實作：`lgwd.sshsig` 與 D5 的 fingerprint
    都呼叫它，不另外再寫一份。
    """
    if not isinstance(public_key32, (bytes, bytearray, memoryview)):
        raise InvalidPublicKey(REASON_INVALID_LENGTH)
    raw = bytes(public_key32)
    if len(raw) != 32:
        raise InvalidPublicKey(REASON_INVALID_LENGTH)
    return (
        len(_ED25519_TYPE).to_bytes(4, "big")
        + _ED25519_TYPE
        + len(raw).to_bytes(4, "big")
        + raw
    )


def fingerprint(public_key32) -> str:
    """`base64(sha256(ssh_public_key_blob(public_key32)))` 去掉 padding（C-504）。

    與 `ssh-keygen -l` 印出的 `SHA256:` 後半逐字相同（`tests/test_auth_registry.py`
    用真的 `ssh-keygen -l` 對固定向量比對）。只依賴 hashlib 與 base64。
    """
    digest = hashlib.sha256(ssh_public_key_blob(public_key32)).digest()
    return base64.b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class KeyEntry:
    """名冊裡的一把 key。`public_key` 是驗證後的 32 bytes（不是 base64 字串）。"""

    key_id: str
    principal: str
    public_key: bytes
    status: str  # "active" | "revoked"

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class PrincipalEntry:
    """名冊裡一個 principal 的權限形狀（值的意義由 D3 執行；D2 只驗形狀）。"""

    ops: frozenset[str]
    resolve_agents: tuple[str, ...]
    roles: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class Registry:
    """已驗證的名冊。`keys` 保留檔案順序；`principals` 是不可變映射。

    `key_by_id` 的索引由 `keys` 在建立時算出、不從建構子傳入，所以直接以 `keys`
    建 `Registry`（PR-4 測試、D5 工具）時查詢也是對的，不會靜默變成全部未知 key。
    """

    schema: int
    generation: int
    principals: Mapping[str, PrincipalEntry]
    keys: tuple[KeyEntry, ...]
    _by_key_id: Mapping[str, KeyEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_by_key_id",
            MappingProxyType({key.key_id: key for key in self.keys}),
        )

    def key_by_id(self, key_id: str) -> KeyEntry | None:
        """以 key ID 查一把 key（未知回 None）。"""
        return self._by_key_id.get(key_id)

    def active_keys(self) -> tuple[KeyEntry, ...]:
        """有效（未撤銷）的 key，依檔案順序。allowed_signers 由這一份產生。"""
        return tuple(k for k in self.keys if k.active)


def content_hash(raw: bytes) -> str:
    """名冊內容雜湊：`sha256:` ＋原始 bytes 的小寫 hex（C-206；不做正規化）。"""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class _JsonReject(Exception):
    """`json.loads` 的 hook 用來中斷解析：重複鍵、NaN／Infinity。"""


def _pairs_no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _JsonReject()
        out[key] = value
    return out


def _reject_constant(_value):
    # NaN／Infinity 不是合法 JSON（stdlib 預設接受），一律當 parse_error。
    raise _JsonReject()


def _bad(kind: str = KIND_PARSE_ERROR):
    return RegistryError(kind)


def _check_id(value: object) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise _bad()
    return value


def _object_keys(value: object, expected: tuple[str, ...]) -> dict:
    if not isinstance(value, dict) or set(value.keys()) != set(expected) or len(value) != len(expected):
        raise _bad()
    return value


def _string_list(value: object, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _bad()
    out = []
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            raise _bad()
        out.append(item)
    if len(set(out)) != len(out):
        raise _bad()
    return tuple(out)


def _parse_principal(value: object) -> PrincipalEntry:
    entry = _object_keys(value, ("ops", "resolve_agents", "roles"))
    ops = _string_list(entry["ops"], OPS)
    ops_set = frozenset(ops)
    resolve_agents = _string_list(entry["resolve_agents"], AGENTS)
    roles_obj = entry["roles"]
    if not isinstance(roles_obj, dict):
        raise _bad()
    roles: dict[str, tuple[str, ...]] = {}
    for role, agents in roles_obj.items():
        if role not in ROLES:
            raise _bad()
        roles[role] = _string_list(agents, AGENTS)
    # 一致性（design：ops 不含 resolve 時 resolve_agents 必須為空；不含 roles 時 roles 為空）。
    if "resolve" not in ops_set and resolve_agents:
        raise _bad()
    if "roles" not in ops_set and roles:
        raise _bad()
    return PrincipalEntry(
        ops=ops_set,
        resolve_agents=resolve_agents,
        roles=MappingProxyType(roles),
    )


def _decode_public_key(value: object) -> bytes:
    """把名冊的 `public_key` 字串解成驗證後的 32 bytes（C-203／C-201）。"""
    if not isinstance(value, str):
        raise _bad(KIND_INVALID_KEY)
    type_part, sep, encoded = value.partition(" ")
    if not sep or type_part != "ssh-ed25519" or not encoded or " " in encoded:
        raise _bad(KIND_INVALID_KEY)
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _bad(KIND_INVALID_KEY) from None
    # 標準 base64（含 padding）：解碼後再編碼必須與原字串相同，否則視為同一個值換編碼。
    if base64.b64encode(blob).decode("ascii") != encoded:
        raise _bad(KIND_INVALID_KEY)
    try:
        return validate_ed25519_pubkey(blob)
    except InvalidPublicKey as exc:
        raise _bad(KIND_INVALID_KEY) from exc


def _parse_key(value: object, principals: Mapping[str, PrincipalEntry]) -> KeyEntry:
    entry = _object_keys(value, ("key_id", "principal", "public_key", "status"))
    key_id = _check_id(entry["key_id"])
    principal = entry["principal"]
    if not isinstance(principal, str) or principal not in principals:
        raise _bad()
    status = entry["status"]
    if status not in ("active", "revoked"):
        raise _bad()
    public_key = _decode_public_key(entry["public_key"])
    return KeyEntry(key_id=key_id, principal=principal, public_key=public_key, status=status)


def parse_registry(raw: bytes) -> Registry:
    """嚴格解析名冊（design「名冊檔格式」schema 1；C-203～C-205）。

    UTF-8（不要 BOM）、≤1 MiB、物件內重複鍵即拒絕；任何物件出現未列的鍵整份拒絕。
    任一公鑰不合 C-201 整份拒絕；同 key ID 重複、同一組驗證後 32 bytes 對到兩個 key
    ID（因此也對到兩個 principal）整份拒絕。不合法一律丟 `RegistryError`，`kind`
    是固定分類（`parse_error`／`invalid_key`／`duplicate`／`too_large`）。
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise _bad()
    raw = bytes(raw)
    if len(raw) > MAX_REGISTRY_BYTES:
        raise _bad(KIND_TOO_LARGE)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _bad()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad() from None
    try:
        data = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (_JsonReject, ValueError, RecursionError):
        raise _bad() from None
    if not isinstance(data, dict):
        raise _bad()
    top = _object_keys(data, ("schema", "generation", "principals", "keys"))
    schema = top["schema"]
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise _bad()
    generation = top["generation"]
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not 1 <= generation <= MAX_GENERATION
    ):
        raise _bad()
    principals_obj = top["principals"]
    if not isinstance(principals_obj, dict):
        raise _bad()
    principals: dict[str, PrincipalEntry] = {}
    for name, spec in principals_obj.items():
        principals[_check_id(name)] = _parse_principal(spec)
    keys_obj = top["keys"]
    if not isinstance(keys_obj, list):
        raise _bad()
    keys: list[KeyEntry] = []
    seen_ids: set[str] = set()
    seen_bytes: dict[bytes, str] = {}
    for item in keys_obj:
        key = _parse_key(item, principals)
        if key.key_id in seen_ids:
            raise _bad(KIND_DUPLICATE)
        seen_ids.add(key.key_id)
        other = seen_bytes.get(key.public_key)
        if other is not None:
            # 同一個點換成不同 key ID（因此也換到不同 principal）一律整份拒絕。
            raise _bad(KIND_DUPLICATE)
        seen_bytes[key.public_key] = key.key_id
        keys.append(key)
    return Registry(
        schema=schema,
        generation=generation,
        principals=MappingProxyType(principals),
        keys=tuple(keys),
    )


def _read_limited(fd: int, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise RegistryError(KIND_TOO_LARGE)
        chunks.append(chunk)


def read_registry_file(path: str, *, owner_uid: int = 0) -> bytes:
    """讀名冊檔並做 C-207 的檔案檢查，回傳原始 bytes（不解析）。

    檔案檢查：以 `O_NOFOLLOW` 開（拒 symlink）、必須是一般檔、擁有者 uid 等於
    `owner_uid`（預設 0，只有這個關鍵字參數能改，給測試用）、群組與其他人不可寫、
    大小 ≤ 1 MiB。不合分別是 `insecure_file`／`too_large`；開檔失敗是 `io_error`。

    內容是否為合法名冊不在這裡判斷：PR-4 的 reload 與 D5 的 install 要在解析失敗時
    仍取得「最近一次嘗試載入的檔案內容雜湊」（`content_hash`），所以讀檔與解析分成
    兩步；檔案檢查只有這一份，呼叫端不要自己再開一次檔。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise RegistryError(KIND_INSECURE_FILE) from None
        raise RegistryError(KIND_IO_ERROR) from None
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise RegistryError(KIND_IO_ERROR) from None
        if not stat.S_ISREG(info.st_mode):
            raise RegistryError(KIND_INSECURE_FILE)
        if info.st_uid != owner_uid:
            raise RegistryError(KIND_INSECURE_FILE)
        if info.st_mode & 0o022:
            raise RegistryError(KIND_INSECURE_FILE)
        if info.st_size > MAX_REGISTRY_BYTES:
            raise RegistryError(KIND_TOO_LARGE)
        return _read_limited(fd, MAX_REGISTRY_BYTES)
    finally:
        os.close(fd)


def load_registry_file(path: str, *, owner_uid: int = 0) -> tuple[bytes, Registry]:
    """讀名冊檔並解析（C-207）：`read_registry_file`＋`parse_registry` 的便利函式。

    回傳原始 bytes 與 `Registry`（原始 bytes 給 `content_hash` 與快照用）。內容不合法
    時丟 `RegistryError`（分類同 `parse_registry`）；要在失敗時仍拿到原始 bytes，改用
    `read_registry_file` 再自行解析。
    """
    raw = read_registry_file(path, owner_uid=owner_uid)
    return raw, parse_registry(raw)


def check_tombstones(registry: Registry, tombstones: Iterable[tuple[str, bytes]]) -> None:
    """撤銷墓碑比對（C-205）。

    `tombstones` 是 `(key_id, bytes32)` 的序列（信任庫裡每個已撤銷 key 的 ID 與驗證後
    公鑰 bytes）。每個墓碑都必須在名冊中以**同一個 key ID、同一組 bytes** 標為
    `revoked`；而且任何其他 key ID（不論 active／revoked）用到墓碑的 bytes 都算「以
    新 key ID 重用已撤銷公鑰」。違反一律丟 `RegistryError("tombstone_violation")`。
    """
    by_id = {k.key_id: k for k in registry.keys}
    by_bytes: dict[bytes, list[str]] = {}
    for key in registry.keys:
        by_bytes.setdefault(key.public_key, []).append(key.key_id)
    tombstones = list(tombstones)
    for key_id, public_key in tombstones:
        if not isinstance(key_id, str) or not isinstance(
            public_key, (bytes, bytearray, memoryview)
        ):
            raise RegistryError(KIND_TOMBSTONE_VIOLATION)
        public_key = bytes(public_key)
        entry = by_id.get(key_id)
        if entry is None or entry.status != "revoked" or entry.public_key != public_key:
            raise RegistryError(KIND_TOMBSTONE_VIOLATION)
    for key_id, public_key in tombstones:
        for other in by_bytes.get(bytes(public_key), ()):
            if other != key_id:
                raise RegistryError(KIND_TOMBSTONE_VIOLATION)
