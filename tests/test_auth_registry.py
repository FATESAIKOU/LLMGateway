"""名冊 parser 與 Ed25519 公鑰驗證（F1）的測試；對應 D2 PR-2 的 C-201～C-207。

全部用假資料與臨時目錄；私鑰不在這裡、也沒有任何真正的金鑰。測試名都帶 C 編號，
報告的「C 編號對測試名稱」表直接由這裡對出來。

C-202 的小階點推導是 test-only 的純 Python 曲線運算（照 docs/prototypes/sign-schemes/
schemes.py 的做法）：`lgwd.auth_registry` 只比常數表，不 import 這段程式。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lgwd import auth_registry
from lgwd.auth_registry import (
    MAX_REGISTRY_BYTES,
    SMALL_ORDER_PUBKEYS,
    InvalidPublicKey,
    KeyEntry,
    Registry,
    RegistryError,
    check_tombstones,
    content_hash,
    load_registry_file,
    parse_registry,
    read_registry_file,
    validate_ed25519_pubkey,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads(
    (REPO_ROOT / "tests" / "vectors" / "ed25519_pubkeys.json").read_text(encoding="utf-8")
)["public_key_vectors"]
VECTOR = {v["name"]: v for v in VECTORS}
VALID_A = base64.b64decode(VECTOR["valid_ed25519_a"]["blob_b64"])[-32:]
VALID_B = base64.b64decode(VECTOR["valid_ed25519_b"]["blob_b64"])[-32:]
VALID_C = hashlib.sha256(b"test-only third key").digest()


def _blob(raw32: bytes) -> bytes:
    return (
        b"\x00\x00\x00\x0bssh-ed25519"
        + b"\x00\x00\x00\x20"
        + raw32
    )


def _public_key(raw32: bytes) -> str:
    return "ssh-ed25519 " + base64.b64encode(_blob(raw32)).decode("ascii")


def _principal(**over):
    base = {
        "ops": ["list", "usage", "roles", "resolve", "cooldown"],
        "resolve_agents": ["claude-code", "opencode"],
        "roles": {"PM": ["claude-code"], "impl": ["opencode"]},
    }
    base.update(over)
    return base


def _key(key_id="w1-tunnel-a", principal="w1-worker", raw=None, status="active"):
    return {
        "key_id": key_id,
        "principal": principal,
        "public_key": _public_key(VALID_A if raw is None else raw),
        "status": status,
    }


def _registry_obj(generation=1, principals=None, keys=None):
    return {
        "schema": 1,
        "generation": generation,
        "principals": {"w1-worker": _principal()} if principals is None else principals,
        "keys": [_key()] if keys is None else keys,
    }


def _raw(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _two_keys():
    """同一 principal 的兩把輪替 key（舊的已撤銷）。"""
    return [
        _key("w1-tunnel-a", raw=VALID_A),
        _key("w1-tunnel-old", raw=VALID_B, status="revoked"),
    ]


class ValidateEd25519PubkeyTests(unittest.TestCase):
    """C-201：單一驗證函式與它的五條規則。"""

    def test_c201_vectors_positive_and_negative(self):
        for vector in VECTORS:
            blob = base64.b64decode(vector["blob_b64"])
            with self.subTest(vector=vector["name"]):
                if vector["expect"] == "ok":
                    self.assertEqual(validate_ed25519_pubkey(blob), blob[-32:])
                else:
                    with self.assertRaises(InvalidPublicKey) as ctx:
                        validate_ed25519_pubkey(blob)
                    self.assertEqual(ctx.exception.reason, vector["expect"])

    def test_c201_returns_exact_32_bytes(self):
        blob = _blob(VALID_A)
        self.assertEqual(len(blob), 51)
        self.assertEqual(validate_ed25519_pubkey(blob), VALID_A)

    def test_c201_rejects_other_key_types(self):
        for name in ("rsa", "ecdsa", "sk_ed25519", "ed25519_cert"):
            blob = base64.b64decode(VECTOR[name]["blob_b64"])
            with self.subTest(kind=name):
                with self.assertRaises(InvalidPublicKey) as ctx:
                    validate_ed25519_pubkey(blob)
                self.assertEqual(ctx.exception.reason, auth_registry.REASON_NOT_ED25519)

    def test_c201_rejects_extra_and_missing_bytes(self):
        blob = _blob(VALID_A)
        short = (
            b"",  # 連 type_len 都放不下
            b"\x00\x01",  # type_len 欄位被截斷
            b"\x00\x00\x00\x0bssh-ed25519",  # 缺 key_len 欄位
            b"\x00\x00\x00\xff" + b"ssh-ed25519",  # type_len 超過 blob 長度
        )
        for bad in (
            blob + b"\x00",
            blob[:-1],
            b"\x00\x00\x00\x0bssh-ed25519" + b"\x00\x00\x00\x1f" + VALID_A[:-1],
            *short,
        ):
            with self.subTest(length=len(bad)):
                with self.assertRaises(InvalidPublicKey) as ctx:
                    validate_ed25519_pubkey(bad)
                self.assertEqual(ctx.exception.reason, auth_registry.REASON_INVALID_LENGTH)

    def test_c201_rejects_noncanonical_y(self):
        # 邊界：y 恰等於 2^255−19（P）與 1＋P 都不是規範編碼；前者鎖住 `>=` 寫成 `>`。
        for y in (2**255 - 19, 1 + (2**255 - 19)):
            blob = _blob(y.to_bytes(32, "little"))
            with self.subTest(y=y):
                with self.assertRaises(InvalidPublicKey) as ctx:
                    validate_ed25519_pubkey(blob)
                self.assertEqual(ctx.exception.reason, auth_registry.REASON_NONCANONICAL_Y)

    def test_c201_rejects_zero_x_with_sign_bit(self):
        for y in (1, 2**255 - 20):
            blob = _blob((y | (1 << 255)).to_bytes(32, "little"))
            with self.subTest(y=y):
                with self.assertRaises(InvalidPublicKey) as ctx:
                    validate_ed25519_pubkey(blob)
                self.assertEqual(ctx.exception.reason, auth_registry.REASON_ZERO_X_SIGN_BIT)

    def test_c201_rejects_all_eight_small_order_points(self):
        self.assertEqual(len(SMALL_ORDER_PUBKEYS), 8)
        for raw in sorted(SMALL_ORDER_PUBKEYS):
            with self.subTest(raw=raw.hex()):
                with self.assertRaises(InvalidPublicKey) as ctx:
                    validate_ed25519_pubkey(_blob(raw))
                self.assertEqual(ctx.exception.reason, auth_registry.REASON_SMALL_ORDER)

    def test_c201_error_strings_are_fixed_and_do_not_echo_input(self):
        blob = _blob((1 + (2**255 - 19)).to_bytes(32, "little"))
        with self.assertRaises(InvalidPublicKey) as ctx:
            validate_ed25519_pubkey(blob)
        message = str(ctx.exception)
        self.assertEqual(message, auth_registry.REASON_NONCANONICAL_Y)
        self.assertNotIn(blob.hex(), message)
        self.assertLess(len(message), 64)

    def test_c201_parse_registry_uses_the_single_validator(self):
        calls = []
        original = auth_registry.validate_ed25519_pubkey

        def spy(blob):
            calls.append(blob)
            return original(blob)

        auth_registry.validate_ed25519_pubkey = spy
        try:
            registry = parse_registry(_raw(_registry_obj(keys=_two_keys())))
        finally:
            auth_registry.validate_ed25519_pubkey = original
        self.assertEqual(len(calls), 2)
        self.assertEqual([k.public_key for k in registry.keys], [VALID_A, VALID_B])


class SmallOrderDerivationTests(unittest.TestCase):
    """C-202：用 test-only 曲線運算獨立推導 8 個小階點，逐一比對常數表。"""

    P = 2**255 - 19
    L = 2**252 + 27742317777372353535851937790883648493
    D = -121665 * pow(121666, P - 2, P) % P
    I = pow(2, (P - 1) // 4, P)
    IDENTITY = (0, 1)

    def _recover(self, y, sign):
        if y >= self.P:
            raise ValueError("y")
        x2 = (y * y - 1) * pow(self.D * y * y + 1, self.P - 2, self.P) % self.P
        x = pow(x2, (self.P + 3) // 8, self.P)
        if (x * x - x2) % self.P:
            x = x * self.I % self.P
        if (x * x - x2) % self.P or (x == 0 and sign):
            raise ValueError("bad point")
        if x & 1 != sign:
            x = self.P - x
        return (x, y)

    def _add(self, a, b):
        x, y = a
        u, v = b
        t = self.D * x * u * y * v % self.P
        return (
            (x * v + u * y) * pow(1 + t, self.P - 2, self.P) % self.P,
            (y * v + x * u) * pow(1 - t, self.P - 2, self.P) % self.P,
        )

    def _mul(self, point, scalar):
        result = self.IDENTITY
        while scalar:
            if scalar & 1:
                result = self._add(result, point)
            point = self._add(point, point)
            scalar >>= 1
        return result

    def _encode(self, point):
        x, y = point
        return (y | ((x & 1) << 255)).to_bytes(32, "little")

    def _torsion_component(self, raw32=None):
        # 任一不在主群組的點乘上 L 就落在 torsion 子群；先找一個 order 8 的生成元。
        # 用固定的小 y 搜尋，結果與執行環境無關（同樣的 y 第一個可用點）。
        for y in range(2, 200):
            try:
                point = self._recover(y, 0)
            except ValueError:
                continue
            candidate = self._mul(point, self.L)
            if self._mul(candidate, 4) != self.IDENTITY:
                return candidate
        raise AssertionError("no order-8 point found")

    def test_c202_constants_match_independent_derivation(self):
        generator = self._torsion_component()
        points = []
        current = generator
        for _ in range(8):
            points.append(current)
            current = self._add(current, generator)
        self.assertEqual(len(set(points)), 8)
        derived = {self._encode(p) for p in points}
        self.assertEqual(derived, set(SMALL_ORDER_PUBKEYS))
        # 每一個都真的是小階點（8P＝單位元），而且 8 個互不相同。
        for raw in derived:
            encoded = int.from_bytes(raw, "little")
            point = self._recover(encoded & ((1 << 255) - 1), encoded >> 255)
            self.assertEqual(self._mul(point, 8), self.IDENTITY)

    def test_c202_lgwd_module_does_not_import_curve_code(self):
        source = (REPO_ROOT / "lgwd" / "auth_registry.py").read_text(encoding="utf-8")
        self.assertNotIn("pow(", source)
        self.assertNotIn("torsion", source.lower())


class ParseRegistryTests(unittest.TestCase):
    """C-203：schema 1 嚴格解析。"""

    def _parse(self, obj):
        return parse_registry(_raw(obj))

    def test_c203_valid_registry_with_rotation_and_revoked_row(self):
        registry = self._parse(_registry_obj(generation=12, keys=_two_keys()))
        self.assertEqual(registry.schema, 1)
        self.assertEqual(registry.generation, 12)
        self.assertEqual([k.key_id for k in registry.keys], ["w1-tunnel-a", "w1-tunnel-old"])
        self.assertEqual([k.status for k in registry.keys], ["active", "revoked"])
        self.assertEqual(registry.active_keys(), (registry.keys[0],))
        self.assertEqual(registry.key_by_id("w1-tunnel-old").public_key, VALID_B)
        self.assertIsNone(registry.key_by_id("nope"))
        principal = registry.principals["w1-worker"]
        self.assertEqual(principal.ops, {"list", "usage", "roles", "resolve", "cooldown"})
        self.assertEqual(principal.resolve_agents, ("claude-code", "opencode"))
        self.assertEqual(principal.roles["impl"], ("opencode",))

    def test_c203_minimal_registry(self):
        registry = self._parse(
            {
                "schema": 1,
                "generation": 1,
                "principals": {},
                "keys": [],
            }
        )
        self.assertEqual(registry.keys, ())
        self.assertEqual(dict(registry.principals), {})

    def test_c203_registry_built_directly_still_indexes_keys(self):
        # 低-2-1：索引由 keys 推導，不經 parse_registry 直接建 Registry 也查得到。
        key = KeyEntry(
            key_id="k1", principal="w1-worker", public_key=VALID_A, status="active"
        )
        registry = Registry(schema=1, generation=1, principals={}, keys=(key,))
        self.assertIs(registry.key_by_id("k1"), key)
        self.assertIsNone(registry.key_by_id("nope"))

    def test_c203_rejects_unknown_keys(self):
        cases = []
        obj = _registry_obj()
        obj["extra"] = 1
        cases.append(("top", obj))
        obj = _registry_obj(principals={"w1-worker": {**_principal(), "memo": "x"}})
        cases.append(("principal", obj))
        obj = _registry_obj(keys=[{**_key(), "comment": "x"}])
        cases.append(("key", obj))
        for label, obj in cases:
            with self.subTest(where=label):
                with self.assertRaises(RegistryError) as ctx:
                    self._parse(obj)
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c203_rejects_duplicate_json_keys(self):
        text = (
            b'{"schema":1,"generation":1,"generation":1,'
            b'"principals":{},"keys":[]}'
        )
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(text)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c203_rejects_bad_id_format(self):
        for bad_id in ("-a", ".a", "a*b", "a?b", "a!b", 'a"b', "a,b", "a b", "a" * 65, ""):
            with self.subTest(key_id=bad_id):
                with self.assertRaises(RegistryError) as ctx:
                    self._parse(_registry_obj(keys=[_key(bad_id)]))
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)
        with self.assertRaises(RegistryError):
            self._parse(_registry_obj(principals={"-bad": _principal()}))

    def test_c203_rejects_bad_generation_and_schema(self):
        for generation in (0, -1, 2**53, True, 1.0, "1"):
            with self.subTest(generation=generation):
                with self.assertRaises(RegistryError) as ctx:
                    self._parse(_registry_obj(generation=generation))
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)
        for schema in (0, 2, True, "1"):
            with self.subTest(schema=schema):
                obj = _registry_obj()
                obj["schema"] = schema
                with self.assertRaises(RegistryError):
                    self._parse(obj)

    def test_c203_rejects_inconsistent_permissions(self):
        cases = [
            _principal(ops=["list"], resolve_agents=["claude-code"]),
            _principal(ops=["list"], roles={"PM": ["claude-code"]}),
            _principal(roles={"Unknown": ["claude-code"]}),
            _principal(ops=["list", "bogus"]),
            _principal(resolve_agents=["not-an-agent"]),
            _principal(roles={"PM": ["not-an-agent"]}),
            _principal(ops=["list", "list"]),
            _principal(roles={"PM": ["claude-code", "claude-code"]}),
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(RegistryError) as ctx:
                    self._parse(_registry_obj(principals={"w1-worker": spec}))
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c203_missing_principal_fields(self):
        for field in ("ops", "resolve_agents", "roles"):
            spec = _principal()
            spec.pop(field)
            with self.subTest(field=field):
                with self.assertRaises(RegistryError):
                    self._parse(_registry_obj(principals={"w1-worker": spec}))

    def test_c203_key_must_reference_known_principal(self):
        with self.assertRaises(RegistryError) as ctx:
            self._parse(_registry_obj(keys=[_key(principal="ghost")]))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c203_rejects_bad_public_key_strings(self):
        blob_b64 = base64.b64encode(_blob(VALID_A)).decode("ascii")
        cases = [
            "ssh-ed25519 " + blob_b64 + "====",  # 多餘 padding：解碼後再編碼不相同
            "ssh-ed25519  " + blob_b64,  # 兩個空白
            "ssh-ed25519 " + blob_b64 + " comment",
            "ssh-ed25519 " + blob_b64 + "\n",
            "ssh-ed25519",
            "ssh-ed25519 ",
            "SSH-ED25519 " + blob_b64,
            "ssh-rsa " + blob_b64,
            blob_b64,
        ]
        for value in cases:
            with self.subTest(value=value[:20]):
                keys = [{"key_id": "k1", "principal": "w1-worker",
                         "public_key": value, "status": "active"}]
                with self.assertRaises(RegistryError) as ctx:
                    self._parse(_registry_obj(keys=keys))
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_INVALID_KEY)

    def test_c203_small_order_public_key_rejects_whole_registry(self):
        for raw in sorted(SMALL_ORDER_PUBKEYS)[:2]:
            with self.subTest(raw=raw.hex()):
                with self.assertRaises(RegistryError) as ctx:
                    self._parse(_registry_obj(keys=[_key(), _key("k2", raw=raw)]))
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_INVALID_KEY)

    def test_c203_rejects_bom_and_non_utf8(self):
        for raw in (b"\xef\xbb\xbf" + _raw(_registry_obj()), b'{"schema":1,"generation":\xff}'):
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(RegistryError) as ctx:
                    parse_registry(raw)
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c203_rejects_too_large_and_non_object(self):
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(b" " * (MAX_REGISTRY_BYTES + 1))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOO_LARGE)
        for raw in (b"[]", b'"x"', b"{}", b"{", b"NaN"):
            with self.subTest(raw=raw):
                with self.assertRaises(RegistryError) as ctx:
                    parse_registry(raw)
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)


class UniquenessTests(unittest.TestCase):
    """C-204：唯一性比驗證後的 32 bytes。"""

    def test_c204_same_key_id_twice_rejects(self):
        keys = [_key("dup", raw=VALID_A), _key("dup", raw=VALID_B)]
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(_raw(_registry_obj(keys=keys)))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_DUPLICATE)

    def test_c204_same_bytes_two_key_ids_rejects(self):
        keys = [_key("k1", raw=VALID_A), _key("k2", raw=VALID_A)]
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(_raw(_registry_obj(keys=keys)))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_DUPLICATE)

    def test_c204_same_bytes_two_principals_rejects(self):
        principals = {"w1-worker": _principal(), "w2-worker": _principal()}
        keys = [_key("k1", "w1-worker", raw=VALID_A), _key("k2", "w2-worker", raw=VALID_A)]
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(_raw(_registry_obj(principals=principals, keys=keys)))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_DUPLICATE)

    def test_c204_same_principal_rotation_is_allowed(self):
        registry = parse_registry(_raw(_registry_obj(keys=_two_keys())))
        self.assertEqual(len(registry.keys), 2)


class TombstoneTests(unittest.TestCase):
    """C-205：墓碑以同 ID、同 bytes 標 revoked；新 ID 重用墓碑 bytes 即拒。"""

    def test_c205_revoked_row_satisfies_tombstone(self):
        registry = parse_registry(_raw(_registry_obj(keys=_two_keys())))
        check_tombstones(registry, [("w1-tunnel-old", VALID_B)])

    def test_c205_empty_tombstones_pass(self):
        registry = parse_registry(_raw(_registry_obj(keys=_two_keys())))
        check_tombstones(registry, [])

    def test_c205_missing_revoked_row_rejects(self):
        registry = parse_registry(_raw(_registry_obj(keys=[_key("w1-tunnel-a", raw=VALID_A)])))
        with self.assertRaises(RegistryError) as ctx:
            check_tombstones(registry, [("w1-tunnel-old", VALID_B)])
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOMBSTONE_VIOLATION)

    def test_c205_revoked_back_to_active_rejects(self):
        registry = parse_registry(_raw(_registry_obj(keys=[
            _key("w1-tunnel-a", raw=VALID_A),
            _key("w1-tunnel-old", raw=VALID_B, status="active"),
        ])))
        with self.assertRaises(RegistryError) as ctx:
            check_tombstones(registry, [("w1-tunnel-old", VALID_B)])
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOMBSTONE_VIOLATION)

    def test_c205_new_key_id_reusing_tombstoned_bytes_rejects(self):
        # 「以新 key ID 重用已撤銷公鑰」：墓碑的 ID 不在名冊（被刪掉）、bytes 改登記在新
        # ID 之下。這個案例同時被「同 ID 同 bytes 必須 revoked」與「重用 bytes」兩條擋；
        # 兩條都保留（後者是 defense-in-depth，見報告的變異證據）。
        registry = parse_registry(_raw(_registry_obj(keys=[
            _key("w1-tunnel-a", raw=VALID_A),
            _key("w1-tunnel-reused", raw=VALID_B, status="active"),
        ])))
        with self.assertRaises(RegistryError) as ctx:
            check_tombstones(registry, [("w1-tunnel-old", VALID_B)])
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOMBSTONE_VIOLATION)

    def test_c205_tombstoned_id_with_other_bytes_rejects(self):
        # 同一個 key ID 被換成另一把公鑰（墓碑記的是舊 bytes）也是整份拒絕。
        registry = parse_registry(_raw(_registry_obj(keys=[
            _key("w1-tunnel-old", raw=VALID_A, status="revoked"),
            _key("w1-tunnel-new", raw=VALID_B, status="active"),
        ])))
        with self.assertRaises(RegistryError) as ctx:
            check_tombstones(registry, [("w1-tunnel-old", VALID_B)])
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOMBSTONE_VIOLATION)

    def test_c205_tombstone_with_different_bytes_rejects(self):
        registry = parse_registry(_raw(_registry_obj(keys=_two_keys())))
        with self.assertRaises(RegistryError) as ctx:
            check_tombstones(registry, [("w1-tunnel-old", VALID_C)])
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOMBSTONE_VIOLATION)

    def test_c205_noncanonical_encoding_of_same_point_is_rejected_by_parser(self):
        # 同一個點換編碼（y+P）不是規範編碼，parser 就整份拒絕，墓碑沒有機會被繞過。
        raw = (1 + (2**255 - 19)).to_bytes(32, "little")
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(_raw(_registry_obj(keys=[
                _key("w1-tunnel-a", raw=VALID_A),
                _key("w1-tunnel-new", raw=raw, status="active"),
            ])))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INVALID_KEY)


class ContentHashTests(unittest.TestCase):
    """C-206：sha256 原始 bytes，不做正規化。"""

    def test_c206_content_hash_is_raw_sha256(self):
        raw = _raw(_registry_obj())
        self.assertEqual(content_hash(raw), "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertRegex(content_hash(raw), r"\Asha256:[0-9a-f]{64}\Z")

    def test_c206_whitespace_change_changes_hash(self):
        raw = _raw(_registry_obj())
        pretty = json.dumps(_registry_obj(), indent=2).encode("utf-8")
        self.assertEqual(parse_registry(raw).generation, parse_registry(pretty).generation)
        self.assertNotEqual(content_hash(raw), content_hash(pretty))


class RegistryFileTests(unittest.TestCase):
    """C-207：檔案檢查與純函式邊界。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lgw-test-249-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.uid = os.getuid()

    def _write(self, data=None, name="auth-registry.json", mode=0o640):
        path = self.dir / name
        path.write_bytes(_raw(_registry_obj()) if data is None else data)
        os.chmod(path, mode)
        return path

    def test_c207_good_file_loads(self):
        path = self._write()
        raw, registry = load_registry_file(str(path), owner_uid=self.uid)
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual(registry.generation, 1)
        self.assertEqual(content_hash(raw), content_hash(path.read_bytes()))

    def test_c207_wrong_owner_rejects(self):
        path = self._write()
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(path), owner_uid=self.uid + 1)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)

    def test_c207_default_owner_uid_is_root(self):
        if os.getuid() == 0:
            self.skipTest("running as root: worker-owned file is root-owned too")
        path = self._write()
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(path))
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)

    def test_c207_group_or_other_writable_rejects(self):
        for mode in (0o660, 0o642, 0o602, 0o646):
            path = self._write(mode=mode)
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(RegistryError) as ctx:
                    load_registry_file(str(path), owner_uid=self.uid)
                self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)

    def test_c207_symlink_rejects(self):
        target = self._write()
        link = self.dir / "link.json"
        link.symlink_to(target)
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(link), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)

    def test_c207_directory_rejects(self):
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(self.dir), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)

    def test_c207_too_large_rejects(self):
        path = self._write(data=b"{" + b" " * MAX_REGISTRY_BYTES + b"}")
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(path), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_TOO_LARGE)

    def test_c207_missing_file_is_io_error(self):
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(self.dir / "absent.json"), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_IO_ERROR)
        self.assertFalse((self.dir / "absent.json").exists())

    def test_c207_bad_content_maps_to_parse_error(self):
        path = self._write(data=b"{}")
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(path), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c207_read_registry_file_returns_raw_when_content_is_invalid(self):
        # 中-2-1：內容無效時仍拿得到與檔案相同的 raw，能算出 attempted_hash。
        path = self._write(data=b"{}")
        raw = read_registry_file(str(path), owner_uid=self.uid)
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual(
            content_hash(raw), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
        with self.assertRaises(RegistryError) as ctx:
            load_registry_file(str(path), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c207_read_registry_file_does_not_parse(self):
        # 非 UTF-8 的內容也讀得回來：讀檔與解析分開，PR-4 才能對壞檔算雜湊。
        data = b"\xff not utf-8"
        path = self._write(data=data)
        raw = read_registry_file(str(path), owner_uid=self.uid)
        self.assertEqual(raw, data)
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(raw)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_PARSE_ERROR)

    def test_c207_read_registry_file_applies_the_same_checks(self):
        path = self._write()
        link = self.dir / "link-raw.json"
        link.symlink_to(path)
        with self.assertRaises(RegistryError) as ctx:
            read_registry_file(str(link), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)
        with self.assertRaises(RegistryError) as ctx:
            read_registry_file(str(self.dir / "absent-raw.json"), owner_uid=self.uid)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_IO_ERROR)
        with self.assertRaises(RegistryError) as ctx:
            read_registry_file(str(path), owner_uid=self.uid + 1)
        self.assertEqual(ctx.exception.kind, auth_registry.KIND_INSECURE_FILE)

    def test_c207_pure_module_imports_no_forbidden_modules(self):
        source = (REPO_ROOT / "lgwd" / "auth_registry.py").read_text(encoding="utf-8")
        for forbidden in ("import sqlite3", "lgwd.http", "lgwd.main", "lgwd.registry"):
            self.assertNotIn(forbidden, source)
        code = (
            "import sys, lgwd.auth_registry;"
            "bad=[m for m in ('sqlite3','lgwd.http','lgwd.main') if m in sys.modules];"
            "print(','.join(bad)); raise SystemExit(1 if bad else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class PublicKeyBlobAndFingerprintTests(unittest.TestCase):
    """D2 PR-5 移到 `auth_registry` 的兩個函式：blob 與 fingerprint 只有這一份實作。"""

    SSHSIG_VECTOR = REPO_ROOT / "tests" / "vectors" / "sshsig_v1.json"
    SSH_KEYGEN = "/usr/bin/ssh-keygen"

    def test_ssh_public_key_blob_rebuilds_canonical_blob(self):
        self.assertEqual(auth_registry.ssh_public_key_blob(VALID_A), _blob(VALID_A))
        self.assertEqual(
            auth_registry.validate_ed25519_pubkey(
                auth_registry.ssh_public_key_blob(VALID_A)
            ),
            VALID_A,
        )

    def test_ssh_public_key_blob_rejects_non_32_bytes(self):
        for value in (b"", b"\x00" * 31, b"\x00" * 33):
            with self.subTest(length=len(value)):
                with self.assertRaises(InvalidPublicKey) as ctx:
                    auth_registry.ssh_public_key_blob(value)
                self.assertEqual(ctx.exception.reason, auth_registry.REASON_INVALID_LENGTH)
        with self.assertRaises(InvalidPublicKey):
            auth_registry.ssh_public_key_blob("not bytes")

    def test_fingerprint_matches_ssh_keygen_l(self):
        vector = json.loads(self.SSHSIG_VECTOR.read_text(encoding="utf-8"))
        raw = base64.b64decode(vector["public_key"].split()[1])[-32:]
        expected = auth_registry.fingerprint(raw)
        self.assertEqual(expected, "aJNFOHQV7gykg5qlLvJszgg3JWG9Kf529FjlcpaG+OI")
        if not os.path.exists(self.SSH_KEYGEN):
            self.skipTest("no ssh-keygen")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".pub", delete=False, encoding="ascii"
        ) as handle:
            handle.write(vector["public_key"] + "\n")
            path = handle.name
        self.addCleanup(os.unlink, path)
        result = subprocess.run(
            [self.SSH_KEYGEN, "-l", "-f", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SHA256:" + expected, result.stdout)

    def test_fingerprint_is_base64_without_padding(self):
        value = auth_registry.fingerprint(VALID_B)
        self.assertRegex(value, r"\A[A-Za-z0-9+/]{43}\Z")
        self.assertNotEqual(value, auth_registry.fingerprint(VALID_C))


if __name__ == "__main__":
    unittest.main()
