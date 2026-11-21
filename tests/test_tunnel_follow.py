"""#226（#221 P2）：llmgateway-tunnel 的 follow 追蹤。

分三段（對應 tasks.md §2 與 BRIEF 第 2 版補充）：
  (a) 設定解析／欄位驗證純函式、`-l … --` argv、原子寫入、指紋 golden
  (b) GitHub 抓取（urllib、逾時／大小／轉址、token、分類、退避、--fetch-check）
  (c) 狀態機整合（真實 loopback sshd）——見 tests/test_tunnel_follow_sshd.py
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import subprocess
import time
import unittest

from tests.tunnel_fakes import MockGitHub

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TUNNEL = os.path.join(REPO_ROOT, "ops-scripts", "llmgateway-tunnel")


def load_tunnel():
    loader = importlib.machinery.SourceFileLoader("llmgateway_tunnel_mod", TUNNEL)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


tun = load_tunnel()


def ed25519_key(fill=b"x"):
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + fill * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


_REAL_KEYS = {}
_REAL_KEY_DIR = tempfile.TemporaryDirectory()


def real_key(kind, bits=None, fresh=False):
    """真實 ssh-keygen 生成的 host 公鑰（行程內快取；fresh＝重新生成一把）。"""
    cache_key = (kind, bits)
    if fresh or cache_key not in _REAL_KEYS:
        path = os.path.join(_REAL_KEY_DIR.name, "%s-%s-%d" % (kind, bits, len(os.listdir(_REAL_KEY_DIR.name))))
        argv = ["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", "", "-f", path]
        if bits:
            argv[4:4] = ["-b", str(bits)]
        subprocess.run(argv, check=True)
        with open(path + ".pub") as f:
            _REAL_KEYS[cache_key] = " ".join(f.read().split()[:2])
    return _REAL_KEYS[cache_key]


def rsa_key(bits=2048):
    return real_key("rsa", bits)


def ecdsa_key(bits=256):
    return real_key("ecdsa", bits)


def ssh_keygen_reads(key):
    """ssh-keygen -lf 能不能讀這把公鑰（OpenSSH 實際接受的對照組）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "k.pub")
        with open(path, "w") as f:
            f.write(key + "\n")
        return subprocess.run(["ssh-keygen", "-lf", path], capture_output=True).returncode == 0


def _ssh_string(data):
    return struct.pack(">I", len(data)) + data


def _mpint(value):
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return b"\x00" + raw if raw[0] & 0x80 else raw


def blob_key(kind, *fields, tail=b""):
    blob = b"".join(_ssh_string(x) for x in (kind.encode(),) + fields) + tail
    return kind + " " + base64.b64encode(blob).decode()


def split_blob(key):
    blob = base64.b64decode(key.split(" ")[1])
    fields, index = [], 0
    while index < len(blob):
        length = struct.unpack_from(">I", blob, index)[0]
        fields.append(blob[index + 4:index + 4 + length])
        index += 4 + length
    return fields


GOLDEN_FIELDS = {"ip": "example.invalid", "ssh_port": 22, "tunnel_user": "user",
                 "host_key": ed25519_key()}
GOLDEN_FINGERPRINT = "73d0194f7c979828b6da5f50f5bf40722637ae923471e173dc5794c969a344dd"


class ValidatorTests(unittest.TestCase):
    def test_host_accepts_normalizes(self):
        self.assertEqual(tun.validate_host("203.0.113.5"), "203.0.113.5")
        self.assertEqual(tun.validate_host("2001:DB8:0::1"), "2001:db8::1")
        self.assertEqual(tun.validate_host("Rendezvous.Example.COM"), "rendezvous.example.com")
        self.assertEqual(tun.validate_host("localhost"), "localhost")

    def test_host_rejects_injection_and_ambiguous_forms(self):
        bad = [
            "-oProxyCommand=x", "-o", "", " 1.2.3.4", "1.2.3.4 ", "evil.example\n", "1\n",
            "fe80::1%a\n* ssh-ed25519 AAAA", "fe80::1%a b", "fe80::1%x@evil.example",
            "fe80::1%a,*", "fe80::1%-oProxyCommand=x", "fe80::1%eth0", "a b.example",
            "host,*", "host*", "host!", "user@host", "host:22", "a" * 64 + ".example",
            "16909060", "0x7f.1", "0177.0.0.1", "1.2.3", "01.2.3.4", "-host.example",
            "host-.example", ".example", "example..com", "ho$t",
            16909060, None, True, ["a"], b"1.2.3.4",
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tun.validate_host(value)

    def test_ambiguous_numeric_hosts_are_really_parsed_by_inet_aton(self):
        # 這些都吻合主機名 regex，但 ssh 底層會把它們當成 IPv4——所以必須拒收
        for value in ("16909060", "0x7f.1", "0177.0.0.1", "1.2.3", "01.2.3.4"):
            with self.subTest(value=value):
                socket.inet_aton(value)
                with self.assertRaises(ValueError):
                    tun.validate_host(value)

    def test_port(self):
        self.assertEqual(tun.validate_port(None, default=22), 22)
        self.assertEqual(tun.validate_port(4711), 4711)
        for value in (True, False, 0, 65536, -1, "22", 22.0, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tun.validate_port(value, default=22)

    def test_generation(self):
        self.assertEqual(tun.validate_generation(0), 0)
        self.assertEqual(tun.validate_generation(7), 7)
        self.assertEqual(tun.validate_generation("007"), 7)
        self.assertEqual(tun.validate_generation("9" * 18), 10 ** 18 - 1)
        for value in (True, False, -1, "", "-1", "1.0", "1 ", "1\n", "9" * 19, "٣", 1.5, None, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tun.validate_generation(value)

    def test_host_key(self):
        for key in (ed25519_key(), rsa_key(), ecdsa_key()):
            self.assertEqual(tun.validate_host_key(key), key)
        good = ed25519_key()
        kind, b64 = good.split(" ")
        bad = [
            good + "\n", good + " comment", good.replace(" ", "  "), "* " + good, kind, b64,
            "ssh-dss " + b64, kind + " " + b64 + "=", kind + " " + b64[:-2], kind + " !!!!",
            kind + " " + base64.b64encode(b"ssh-ed25519").decode(),
            kind + " " + base64.b64encode(struct.pack(">I", 11) + b"ssh-ed25519").decode(),
            # 型別與宣告不符、尾端多餘位元組、長度欄為 0
            "ssh-rsa " + b64,
            kind + " " + base64.b64encode(
                struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"x" * 32 + b"\x00").decode(),
            "ssh-ed25519 " + base64.b64encode(
                struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 0)).decode(),
            None, 5, ["x"],
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tun.validate_host_key(value)

    def real_keys(self):
        keys = [real_key("ed25519"), rsa_key(1024), rsa_key(2048), rsa_key(3072),
                ecdsa_key(256), ecdsa_key(384), ecdsa_key(521)]
        # 同一種金鑰再多生幾把：點座標前導零、mpint 補 0 的各種長度都會出現
        for i in range(6):
            for kind, bits in (("rsa", 2048), ("ecdsa", 256), ("ecdsa", 384), ("ecdsa", 521)):
                keys.append(real_key(kind, bits, fresh=True))
        return keys

    def test_host_key_real_ssh_keygen_keys_are_accepted_and_readable(self):
        for key in self.real_keys():
            with self.subTest(kind=key.split(" ")[0], tail=key[-12:]):
                self.assertTrue(ssh_keygen_reads(key))
                self.assertEqual(tun.validate_host_key(key), key)
                from lgwctl import layout
                layout.follow_fingerprint({"ip": "203.0.113.5", "ssh_port": 22,
                                           "tunnel_user": "u", "host_key": key})

    def malformed_host_keys(self):
        rsa = split_blob(rsa_key(2048))
        e, n = rsa[1], rsa[2]
        ec = split_blob(ecdsa_key(256))
        ec384 = split_blob(ecdsa_key(384))
        ec521 = split_blob(ecdsa_key(521))
        point = ec[2]
        n_int = int.from_bytes(n, "big")
        bad = {
            "rsa 4-byte modulus": blob_key("ssh-rsa", b"\x01\x00\x01", b"\x00\xaa\xbb\xcc"),
            "rsa 1022-bit n": blob_key("ssh-rsa", e, _mpint((1 << 1021) | 1)),
            "rsa 16385-bit n": blob_key("ssh-rsa", e, _mpint((1 << 16384) | 1)),
            "rsa negative n": blob_key("ssh-rsa", e, bytes([0x80]) + n[1:]),
            "rsa negative e": blob_key("ssh-rsa", b"\x80\x00\x01", n),
            "rsa excess leading zero in e": blob_key("ssh-rsa", b"\x00" + e, n),
            "rsa excess leading zero in n": blob_key("ssh-rsa", e, b"\x00" + n),
            "rsa e too small": blob_key("ssh-rsa", b"\x01", n),
            "rsa e even": blob_key("ssh-rsa", b"\x01\x00\x00", n),
            "rsa e not below n": blob_key("ssh-rsa", n, n),
            "rsa n even": blob_key("ssh-rsa", e, _mpint(n_int - 1)),
            "rsa truncated": blob_key("ssh-rsa", e)[:-2],
            "rsa missing n": blob_key("ssh-rsa", e),
            "rsa trailing bytes": blob_key("ssh-rsa", e, n, tail=b"\x00"),
            "rsa extra field": blob_key("ssh-rsa", e, n, b"x"),
            "ecdsa point abc": blob_key("ecdsa-sha2-nistp256", b"nistp256", b"abc"),
            "ecdsa point not on curve": blob_key("ecdsa-sha2-nistp256", b"nistp256",
                                                 point[:-1] + bytes([point[-1] ^ 1])),
            "ecdsa compressed point": blob_key("ecdsa-sha2-nistp256", b"nistp256",
                                               b"\x02" + point[1:33]),
            "ecdsa hybrid prefix": blob_key("ecdsa-sha2-nistp256", b"nistp256", b"\x06" + point[1:]),
            "ecdsa short point": blob_key("ecdsa-sha2-nistp256", b"nistp256", point[:-1]),
            "ecdsa long point": blob_key("ecdsa-sha2-nistp256", b"nistp256", point + b"\x00"),
            "ecdsa zero point": blob_key("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + bytes(64)),
            "ecdsa coordinates >= p": blob_key("ecdsa-sha2-nistp256", b"nistp256",
                                               b"\x04" + b"\xff" * 64),
            "ecdsa curve name mismatch": blob_key("ecdsa-sha2-nistp256", b"nistp384", point),
            "ecdsa 384 point under 256 name": blob_key("ecdsa-sha2-nistp256", b"nistp256", ec384[2]),
            "ecdsa 256 point under 521 name": blob_key("ecdsa-sha2-nistp521", b"nistp521", point),
            "ecdsa 521 off curve": blob_key("ecdsa-sha2-nistp521", b"nistp521",
                                            ec521[2][:-1] + bytes([ec521[2][-1] ^ 1])),
            "ecdsa trailing bytes": blob_key("ecdsa-sha2-nistp256", b"nistp256", point, tail=b"\x00"),
            "ecdsa truncated": blob_key("ecdsa-sha2-nistp256", b"nistp256", point)[:-2],
            "ecdsa missing point": blob_key("ecdsa-sha2-nistp256", b"nistp256"),
            "ed25519 31 bytes": blob_key("ssh-ed25519", b"x" * 31),
            "ed25519 33 bytes": blob_key("ssh-ed25519", b"x" * 33),
        }
        return bad

    # 比 OpenSSH 更嚴的條件（真實金鑰不會出現）：ssh-keygen 仍讀得動，其餘畸形值它也拒收
    STRICTER_THAN_OPENSSH = {
        "rsa excess leading zero in e", "rsa excess leading zero in n", "rsa e too small",
        "rsa e even", "rsa e not below n", "rsa n even",
    }

    def test_host_key_malformed_wire_formats_are_rejected(self):
        for name, key in self.malformed_host_keys().items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    tun.validate_host_key(key)
                if name not in self.STRICTER_THAN_OPENSSH:
                    self.assertFalse(ssh_keygen_reads(key), "OpenSSH 也不收的才算畸形：" + name)

    def test_host_key_malformed_wire_formats_are_rejected_by_lgwctl_layout_too(self):
        from lgwctl import layout
        for name, key in self.malformed_host_keys().items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    layout.follow_fingerprint({"ip": "203.0.113.5", "ssh_port": 22,
                                               "tunnel_user": "u", "host_key": key})

    def test_what_we_accept_ssh_keygen_reads_over_mutations(self):
        # 對真實金鑰逐位元組改動：只要我們放行，ssh-keygen -lf 就必須讀得動
        import random
        rng = random.Random(226)
        seeds = [rsa_key(1024), ecdsa_key(256), ecdsa_key(384), ecdsa_key(521), ed25519_key()]
        checked = 0
        for key in seeds:
            kind, b64 = key.split(" ")
            blob = bytearray(base64.b64decode(b64))
            for _ in range(60):
                mutated = bytearray(blob)
                mutated[rng.randrange(len(mutated))] ^= 1 << rng.randrange(8)
                candidate = kind + " " + base64.b64encode(bytes(mutated)).decode()
                try:
                    tun.validate_host_key(candidate)
                except ValueError:
                    continue
                checked += 1
                self.assertTrue(ssh_keygen_reads(candidate), candidate)
        self.assertGreaterEqual(checked, 0)

    def gateway(self, **overrides):
        data = {"ip": "203.0.113.5", "port": 2222, "tunnel_user": "sshproxy",
                "host_key": ed25519_key(), "generation": 3,
                "user": "ignored", "key_secret": "ignored", "ports": {"a": 1}}
        data.update(overrides)
        return data

    def test_gateway_ok_and_ignores_other_fields(self):
        fields = tun.validate_gateway(self.gateway(), "sshproxy")
        self.assertEqual(fields["ip"], "203.0.113.5")
        self.assertEqual(fields["ssh_port"], 2222)
        self.assertEqual(fields["generation"], 3)
        self.assertNotIn("key_secret", fields)
        self.assertEqual(tun.validate_gateway(self.gateway(port=None), "sshproxy")["ssh_port"], 22)
        data = self.gateway()
        del data["port"]
        self.assertEqual(tun.validate_gateway(data, "sshproxy")["ssh_port"], 22)
        self.assertEqual(tun.validate_gateway(self.gateway(generation="5"), "sshproxy")["generation"], 5)

    def test_gateway_ssh_port_key_is_not_read(self):
        data = self.gateway()
        del data["port"]
        data["ssh_port"] = 2222
        self.assertEqual(tun.validate_gateway(data, "sshproxy")["ssh_port"], 22)

    def test_gateway_rejects(self):
        cases = {
            "invalid": [
                {"tunnel_user": "-oProxyCommand=x"}, {"tunnel_user": "other"},
                {"tunnel_user": "sshproxy\n"}, {"tunnel_user": 5}, {"tunnel_user": None},
                {"ip": "-oProxyCommand=x"}, {"ip": 16909060}, {"ip": "fe80::1%a"},
                {"ip": "evil.example\n"}, {"ip": None}, {"port": True}, {"port": False},
                {"port": 0}, {"port": "22"}, {"generation": True}, {"generation": False},
                {"generation": "9" * 19}, {"generation": -1}, {"generation": None},
            ],
            "hostkey_missing": [
                {"host_key": None}, {"host_key": ""}, {"host_key": ed25519_key() + "\n"},
                {"host_key": ed25519_key() + " comment"}, {"host_key": "ssh-ed25519 AAAA"},
            ],
        }
        for expected, overrides_list in cases.items():
            for overrides in overrides_list:
                with self.subTest(overrides=overrides):
                    with self.assertRaises(ValueError) as ctx:
                        tun.validate_gateway(self.gateway(**overrides), "sshproxy")
                    self.assertEqual(str(ctx.exception), expected)
        data = self.gateway()
        del data["host_key"]
        with self.assertRaises(ValueError) as ctx:
            tun.validate_gateway(data, "sshproxy")
        self.assertEqual(str(ctx.exception), "hostkey_missing")
        for not_dict in ([], "x", 5, None):
            with self.assertRaises(ValueError):
                tun.validate_gateway(not_dict, "sshproxy")

    def test_follow_requires_normalized_form(self):
        good = {"generation": 2, "ip": "203.0.113.5", "ssh_port": 22,
                "tunnel_user": "u", "host_key": ed25519_key(), "published_at": "t"}
        self.assertEqual(tun.validate_follow(good, "u")["generation"], 2)
        for key, value in (("generation", "2"), ("generation", True), ("ssh_port", None),
                           ("ssh_port", True), ("ip", "FE80::1"), ("ip", "Example.COM"),
                           ("tunnel_user", "v"), ("host_key", "x")):
            data = dict(good)
            data[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    tun.validate_follow(data, "u")
        data = dict(good)
        del data["ssh_port"]
        with self.assertRaises(ValueError):
            tun.validate_follow(data, "u")


class FingerprintTests(unittest.TestCase):
    def test_golden_matches_p1(self):
        # 與 tests/test_lgwctl_225_follow.py::test_follow_fingerprint_golden 同一組向量
        self.assertEqual(tun.fingerprint(GOLDEN_FIELDS), GOLDEN_FINGERPRINT)

    def test_agrees_with_lgwctl_layout_on_several_vectors(self):
        from lgwctl import layout
        vectors = [
            GOLDEN_FIELDS,
            {"ip": "203.0.113.5", "ssh_port": 2222, "tunnel_user": "sshproxy", "host_key": rsa_key()},
            {"ip": "2001:db8::1", "ssh_port": 22, "tunnel_user": "a.b-c", "host_key": ecdsa_key()},
            {"ip": "rendezvous.example.com", "ssh_port": 65535, "tunnel_user": "_u", "host_key": ed25519_key(b"y")},
        ]
        for fields in vectors:
            with self.subTest(ip=fields["ip"]):
                data = dict(fields, generation=1)
                self.assertEqual(tun.fingerprint(tun.validate_follow(data, fields["tunnel_user"])),
                                 layout.follow_fingerprint(data))

    def test_fingerprint_ignores_generation_and_changes_with_each_field(self):
        base = dict(GOLDEN_FIELDS)
        self.assertEqual(tun.fingerprint(dict(base, generation=9)), tun.fingerprint(base))
        for key, value in (("ip", "example.test"), ("ssh_port", 23), ("tunnel_user", "u2"),
                           ("host_key", ed25519_key(b"z"))):
            self.assertNotEqual(tun.fingerprint(dict(base, **{key: value})), tun.fingerprint(base))


class KnownHostsAndArgvTests(unittest.TestCase):
    def test_known_hosts_line(self):
        self.assertEqual(tun.known_hosts_line(dict(GOLDEN_FIELDS)),
                         "example.invalid " + GOLDEN_FIELDS["host_key"] + "\n")
        line = tun.known_hosts_line(dict(GOLDEN_FIELDS, ip="2001:db8::1", ssh_port=2222))
        self.assertEqual(line, "[2001:db8::1]:2222 " + GOLDEN_FIELDS["host_key"] + "\n")
        self.assertEqual(line.count("\n"), 1)

    def cfg(self, **kw):
        base = {"ssh_path": "ssh", "identity": "/id", "remote_api": 4710, "remote_ssh": 4711,
                "local_api": 8480, "connect_timeout": 10, "alive_interval": 30,
                "alive_count": 3, "local_user": "svc"}
        base.update(kw)
        return base

    def test_argv_isolates_user_and_host(self):
        target = {"host": "203.0.113.5", "port": 2222, "known_hosts": "/kh"}
        for argv in (tun.tunnel_argv(self.cfg(), target), tun.probe_argv(self.cfg(), target)):
            self.assertEqual(argv[-4:], ["-l", "svc", "--", "203.0.113.5"])
            self.assertIn("ConnectTimeout=10", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("UserKnownHostsFile=/kh", argv)
            self.assertNotIn("accept-new", " ".join(argv))
            self.assertFalse(any("@" in part for part in argv))
        tunnel = tun.tunnel_argv(self.cfg(), target)
        self.assertIn("ExitOnForwardFailure=yes", tunnel)
        self.assertIn("127.0.0.1:4710:127.0.0.1:8480", tunnel)
        self.assertIn("127.0.0.1:4711:127.0.0.1:22", tunnel)
        self.assertIn("ServerAliveInterval=30", tunnel)
        self.assertIn("ServerAliveCountMax=3", tunnel)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.old_state = os.environ.get("LGW_STATE_DIR")
        os.environ["LGW_STATE_DIR"] = os.path.join(self.dir, "state")
        os.makedirs(os.environ["LGW_STATE_DIR"])
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for name in ("LGW_GATEWAY_API_BASE", "LGW_ALLOW_INSECURE_API_BASE"):
            os.environ.pop(name, None)
        if self.old_state is None:
            os.environ.pop("LGW_STATE_DIR", None)
        else:
            os.environ["LGW_STATE_DIR"] = self.old_state

    def load(self, **values):
        path = os.path.join(self.dir, "tunnel.env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join("%s=%s\n" % kv for kv in values.items()))
        return tun.load_config(path)

    STATIC = {"IDENTITY": "/id", "RENDEZVOUS": "svc@host.example:2200", "KNOWN_HOSTS": "/kh"}
    GITHUB = {"GATEWAY_SOURCE_REPO": "o/r", "GATEWAY_SOURCE_PATH": "actions/variables/NODE_GATEWAY",
              "GATEWAY_GITHUB_KEY_FILE": "/k"}

    def test_static_three_keys(self):
        cfg = self.load(**self.STATIC)
        self.assertEqual(cfg["local_user"], "svc")
        self.assertEqual(cfg["static"]["host"], "host.example")
        self.assertEqual(cfg["static"]["port"], 2200)
        self.assertFalse(cfg["github"])
        self.assertEqual((cfg["connect_timeout"], cfg["alive_interval"], cfg["alive_count"]), (10, 30, 3))
        self.assertEqual(cfg["remote_ssh"], 4711)

    def test_remote_ssh_blank_is_4711_and_off_is_rejected(self):
        self.assertEqual(self.load(REMOTE_SSH="", **self.STATIC)["remote_ssh"], 4711)
        self.assertEqual(self.load(REMOTE_SSH="4711", **self.STATIC)["remote_ssh"], 4711)
        self.assertIsNone(self.load(REMOTE_SSH="off", **self.STATIC))

    def test_local_user_sources(self):
        no_user = dict(self.STATIC, RENDEZVOUS="host.example:2200")
        self.assertIsNone(self.load(**no_user))
        self.assertEqual(self.load(TUNNEL_USER="tu", **no_user)["local_user"], "tu")
        self.assertEqual(self.load(TUNNEL_USER="svc", **self.STATIC)["local_user"], "svc")
        self.assertIsNone(self.load(TUNNEL_USER="other", **self.STATIC))
        for bad in ("-oProxyCommand=x", "a b", "bad\\n", "x" * 65, "-x"):
            with self.subTest(user=bad):
                self.assertIsNone(self.load(TUNNEL_USER=bad, **no_user))
                self.assertIsNone(self.load(**dict(self.STATIC, RENDEZVOUS=bad + "@host.example:22")))

    def test_missing_pieces_are_none(self):
        self.assertIsNone(self.load(**dict(self.STATIC, IDENTITY="")))
        self.assertIsNone(self.load(**dict(self.STATIC, KNOWN_HOSTS="")))
        self.assertIsNone(self.load(**dict(self.STATIC, RENDEZVOUS="")))
        self.assertIsNone(self.load(IDENTITY="/id", TUNNEL_USER="svc"))
        self.assertIsNone(self.load(**dict(self.STATIC, RENDEZVOUS="svc@-bad.example:22")))
        self.assertIsNone(self.load(**dict(self.STATIC, RENDEZVOUS="svc@host.example:0")))

    def test_github_only_config_is_enough(self):
        cfg = self.load(IDENTITY="/id", TUNNEL_USER="svc", **self.GITHUB)
        self.assertIsNotNone(cfg)
        self.assertTrue(cfg["github"])
        self.assertIsNone(cfg["static"])
        self.assertEqual(cfg["api_base"], "https://api.github.com")

    def test_github_partial_with_static_is_not_fatal(self):
        cfg = self.load(GATEWAY_SOURCE_REPO="o/r", **self.STATIC)
        self.assertIsNotNone(cfg)
        self.assertTrue(cfg["github_partial"])
        self.assertFalse(cfg["github"])
        self.assertIsNone(self.load(IDENTITY="/id", TUNNEL_USER="svc", GATEWAY_SOURCE_REPO="o/r"))

    def test_github_source_syntax(self):
        for key, value in (("GATEWAY_SOURCE_REPO", "../x"), ("GATEWAY_SOURCE_REPO", "a/b/c"),
                           ("GATEWAY_SOURCE_PATH", "a/../b"), ("GATEWAY_SOURCE_PATH", "a b"),
                           ("GATEWAY_SOURCE_PATH", "a?x=1")):
            with self.subTest(key=key, value=value):
                self.assertIsNone(self.load(**dict(self.STATIC, **dict(self.GITHUB, **{key: value}))))

    def test_valid_follow_file_alone_is_enough(self):
        self.assertIsNone(self.load(IDENTITY="/id", TUNNEL_USER="svc"))
        follow = os.path.join(os.environ["LGW_STATE_DIR"], "gateway.json")
        with open(follow, "w") as f:
            json.dump({"generation": 1, "ip": "203.0.113.5", "ssh_port": 22,
                       "tunnel_user": "svc", "host_key": ed25519_key()}, f)
        self.assertIsNotNone(self.load(IDENTITY="/id", TUNNEL_USER="svc"))

    def test_api_base_authorization(self):
        os.environ["LGW_GATEWAY_API_BASE"] = "http://127.0.0.1:9"
        self.assertIsNone(self.load(**dict(self.STATIC, **self.GITHUB)))      # 沒旗標
        os.environ["LGW_ALLOW_INSECURE_API_BASE"] = "1"
        self.assertEqual(self.load(**dict(self.STATIC, **self.GITHUB))["api_base"], "http://127.0.0.1:9")
        for bad in ("http://evil.example:9", "http://127.0.0.1.evil.example", "ftp://127.0.0.1",
                    "http://user@127.0.0.1:9", "http://127.0.0.1:9/x", "http://10.0.0.1:9", "garbage"):
            with self.subTest(base=bad):
                os.environ["LGW_GATEWAY_API_BASE"] = bad
                self.assertIsNone(self.load(**dict(self.STATIC, **self.GITHUB)))
        os.environ["LGW_GATEWAY_API_BASE"] = "http://[::1]:9"
        self.assertIsNotNone(self.load(**dict(self.STATIC, **self.GITHUB)))


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_atomic_write_mode_and_no_leftovers(self):
        path = os.path.join(self.dir, "sub", "f.json")
        tun.write_atomic(path, "one")
        tun.write_atomic(path, "two")
        self.assertEqual(open(path).read(), "two")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["f.json"])

    def test_atomic_write_replaces_symlink_instead_of_following(self):
        target = os.path.join(self.dir, "victim")
        open(target, "w").write("keep")
        link = os.path.join(self.dir, "link")
        os.symlink(target, link)
        tun.write_atomic(link, "new")
        self.assertEqual(open(target).read(), "keep")
        self.assertFalse(os.path.islink(link))
        self.assertEqual(open(link).read(), "new")

    def test_read_refuses_symlink_oversize_and_non_regular(self):
        target = os.path.join(self.dir, "t")
        open(target, "w").write('{"a": 1}')
        link = os.path.join(self.dir, "l")
        os.symlink(target, link)
        self.assertEqual(tun.read_bounded(link), ("invalid", None))
        big = os.path.join(self.dir, "big")
        open(big, "w").write("x" * 70000)
        self.assertEqual(tun.read_bounded(big), ("invalid", None))
        self.assertEqual(tun.read_bounded(self.dir), ("invalid", None))
        self.assertEqual(tun.read_bounded(os.path.join(self.dir, "none")), ("missing", None))
        self.assertEqual(tun.read_json(target), ("ok", {"a": 1}))

    def test_safe_write_swallows_errors(self):
        blocker = os.path.join(self.dir, "file")
        open(blocker, "w").write("x")
        self.assertFalse(tun.safe_write(os.path.join(blocker, "child"), "x"))


class ChooseTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        self.cfg = {
            "local_user": "svc", "state_dir": self.state,
            "follow_file": os.path.join(self.state, "gateway.json"),
            "github": False, "static": {"host": "static.example", "port": 22,
                                         "known_hosts": "/static-kh", "follow": None},
        }

    def write_follow(self, generation=2, **kw):
        data = {"generation": generation, "ip": "203.0.113.5", "ssh_port": 2222,
                "tunnel_user": "svc", "host_key": ed25519_key(), "published_at": "t"}
        data.update(kw)
        with open(self.cfg["follow_file"], "w") as f:
            json.dump(data, f)
        return tun.validate_follow(data, "svc")

    def write_verified(self, generation, fp):
        with open(os.path.join(self.state, ".tunnel_generation"), "w") as f:
            json.dump({"generation": generation, "fingerprint": fp}, f)

    def test_static_when_no_follow(self):
        self.assertEqual(tun.choose_target(self.cfg)["host"], "static.example")

    def test_valid_follow_wins_and_private_known_hosts_is_written(self):
        fields = self.write_follow()
        target = tun.choose_target(self.cfg)
        self.assertEqual((target["host"], target["port"]), ("203.0.113.5", 2222))
        kh = os.path.join(self.state, ".tunnel_known_hosts")
        self.assertEqual(target["known_hosts"], kh)
        self.assertEqual(open(kh).read(), tun.known_hosts_line(fields))
        self.assertEqual(stat.S_IMODE(os.stat(kh).st_mode), 0o600)

    def test_read_only_mode_never_creates_known_hosts(self):
        self.write_follow()
        self.assertIsNone(tun.choose_target(self.cfg, write=False, notify=False))
        self.assertFalse(os.path.exists(os.path.join(self.state, ".tunnel_known_hosts")))
        tun.choose_target(self.cfg)
        self.assertIsNotNone(tun.choose_target(self.cfg, write=False, notify=False))

    def test_tampered_known_hosts_is_rewritten_from_follow(self):
        fields = self.write_follow()
        kh = os.path.join(self.state, ".tunnel_known_hosts")
        open(kh, "w").write("evil ssh-ed25519 AAAA\n")
        tun.choose_target(self.cfg)
        self.assertEqual(open(kh).read(), tun.known_hosts_line(fields))

    def test_regressed_or_tampered_follow_falls_back_to_static(self):
        fields = self.write_follow(generation=2)
        self.write_verified(3, tun.fingerprint(fields))
        self.assertEqual(tun.follow_status(self.cfg)[0], "regressed")
        self.assertEqual(tun.choose_target(self.cfg, notify=False)["host"], "static.example")
        self.write_verified(2, "a" * 64)
        self.assertEqual(tun.follow_status(self.cfg)[0], "invalid")
        self.assertEqual(tun.choose_target(self.cfg, notify=False)["host"], "static.example")
        self.write_verified(1, "a" * 64)
        self.assertEqual(tun.follow_status(self.cfg)[0], "ok")
        self.write_verified(2, tun.fingerprint(fields))
        self.assertEqual(tun.follow_status(self.cfg)[0], "ok")

    def test_nothing_usable_is_none(self):
        self.cfg["static"] = None
        self.assertIsNone(tun.choose_target(self.cfg))
        open(self.cfg["follow_file"], "w").write("{")
        self.assertIsNone(tun.choose_target(self.cfg, notify=False))


TOKEN = "TEST_GH_TOKEN_SENTINEL"
SECRET_IP = "203.0.113.77"


class GitHubFetchBase(unittest.TestCase):
    """(b) 以 loopback mock GitHub 測抓取；環境變數開測試旗標。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        self.github = MockGitHub().start()
        self.addCleanup(self.github.stop)
        self.token_file = os.path.join(self.dir, "token")
        with open(self.token_file, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)
        self.env_path = os.path.join(self.dir, "tunnel.env")
        self.saved_env = dict(os.environ)
        self.addCleanup(self.restore_env)
        os.environ["LGW_STATE_DIR"] = self.state
        os.environ["LGW_GATEWAY_API_BASE"] = self.github.base_url
        os.environ["LGW_ALLOW_INSECURE_API_BASE"] = "1"
        os.environ.pop("CREDENTIALS_DIRECTORY", None)
        self.github.set_gateway(**self.gateway())

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    def gateway(self, **overrides):
        data = {"ip": SECRET_IP, "port": 2222, "tunnel_user": "svc",
                "host_key": ed25519_key(), "generation": 4, "user": "x", "key_secret": "y"}
        data.update(overrides)
        return data

    def write_env(self, **extra):
        values = {"IDENTITY": "/id", "RENDEZVOUS": "svc@static.example:22", "KNOWN_HOSTS": "/kh",
                  "GATEWAY_SOURCE_REPO": "FATESAIKOU/MyLinuxPool",
                  "GATEWAY_SOURCE_PATH": "actions/variables/NODE_GATEWAY",
                  "GATEWAY_GITHUB_KEY_FILE": self.token_file,
                  "FETCH_TIMEOUT": "2"}
        values.update(extra)
        with open(self.env_path, "w") as f:
            f.write("".join("%s=%s\n" % kv for kv in values.items()))
        return self.env_path

    def cfg(self, **extra):
        cfg = tun.load_config(self.write_env(**extra))
        self.assertIsNotNone(cfg)
        return cfg

    def state_listing(self):
        return {name: (os.stat(os.path.join(self.state, name)).st_mtime_ns,
                       open(os.path.join(self.state, name), "rb").read())
                for name in sorted(os.listdir(self.state))}


class FetchTests(GitHubFetchBase):
    def fetch_cls(self, **cfg_extra):
        with self.assertRaises(tun.FetchError) as ctx:
            tun.fetch_gateway(self.cfg(**cfg_extra))
        return ctx.exception

    def test_success_uses_variables_api_with_bearer_token(self):
        fields, headers = tun.fetch_gateway(self.cfg())
        self.assertEqual((fields["ip"], fields["ssh_port"], fields["generation"]), (SECRET_IP, 2222, 4))
        self.assertEqual(self.github.requests,
                         [("/repos/FATESAIKOU/MyLinuxPool/actions/variables/NODE_GATEWAY",
                           "Bearer " + TOKEN)])

    def test_http_status_classification(self):
        cases = [
            (401, {}, "auth_failed", None),
            (403, {}, "auth_failed", None),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 100)},
             "rate_limited", 100),
            (403, {"Retry-After": "30"}, "rate_limited", 30),
            (429, {"Retry-After": "120"}, "rate_limited", 120),
            (429, {}, "rate_limited", None),
            (404, {}, "unreachable", None),
            (500, {}, "unreachable", None),
            (502, {}, "unreachable", None),
        ]
        for status, headers, cls, delay in cases:
            with self.subTest(status=status, headers=headers):
                self.github.reset_behavior()
                self.github.status = status
                self.github.headers = headers
                err = self.fetch_cls()
                self.assertEqual(err.cls, cls)
                if delay is None:
                    self.assertIsNone(err.retry_after)
                else:
                    self.assertAlmostEqual(err.retry_after, delay, delta=5)

    def test_retry_after_is_capped(self):
        self.github.status = 429
        self.github.headers = {"Retry-After": "999999"}
        self.assertEqual(self.fetch_cls().retry_after, 3600.0)

    def test_malformed_bodies_are_invalid(self):
        bodies = [b"not json", b"[]", b'{"value": 5}', b'{"value": "not json"}', b'{"value": "[]"}',
                  b'{"name": "x"}', b'\xff\xfe', json.dumps({"value": json.dumps(self.gateway(ip=16909060))}).encode(),
                  json.dumps({"value": json.dumps(self.gateway(tunnel_user="other"))}).encode(),
                  json.dumps({"value": json.dumps(self.gateway(generation=True))}).encode()]
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.github.raw_body = body
                self.assertEqual(self.fetch_cls().cls, "invalid")

    def test_missing_or_bad_host_key_is_hostkey_missing(self):
        for override in ({"host_key": None}, {"host_key": ed25519_key() + "\nx"},
                         {"host_key": ed25519_key() + " comment"}):
            with self.subTest(override=override):
                self.github.set_gateway(**self.gateway(**override))
                self.assertEqual(self.fetch_cls().cls, "hostkey_missing")
        data = self.gateway()
        del data["host_key"]
        self.github.set_gateway(**data)
        self.assertEqual(self.fetch_cls().cls, "hostkey_missing")

    def test_response_over_64kib_is_rejected(self):
        self.github.raw_body = b"x" * 65537
        self.assertEqual(self.fetch_cls().cls, "invalid")
        padded = json.dumps({"value": json.dumps(self.gateway()), "pad": "p" * 70000}).encode()
        self.github.raw_body = padded
        self.assertEqual(self.fetch_cls().cls, "invalid")

    def test_timeout_is_unreachable(self):
        self.github.delay = 3
        started = time.monotonic()
        self.assertEqual(self.fetch_cls(FETCH_TIMEOUT="0.5").cls, "unreachable")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_unreachable_server(self):
        self.github.stop()
        self.assertEqual(self.fetch_cls().cls, "unreachable")

    def test_cross_host_redirect_is_not_followed_and_token_not_forwarded(self):
        other = MockGitHub().start()
        self.addCleanup(other.stop)
        other.set_gateway(**self.gateway())
        self.github.redirect_to = other.base_url + "/steal"
        self.assertEqual(self.fetch_cls().cls, "invalid")
        self.assertEqual(other.request_count, 0)
        # 同主機不同埠也算別的主機
        self.github.redirect_to = "http://localhost:%s/x" % other.base_url.rsplit(":", 1)[1]
        self.assertEqual(self.fetch_cls().cls, "invalid")
        self.assertEqual(other.request_count, 0)

    def test_same_host_redirect_loop_is_bounded(self):
        self.github.redirect_to = self.github.base_url + VARIABLE
        # 同主機轉址會被跟隨，但 mock 一律回 302 → urllib 的轉址上限擋下，不會無限迴圈
        self.assertEqual(self.fetch_cls().cls, "invalid")
        self.assertGreaterEqual(self.github.request_count, 2)

    def test_credential_unreadable_cases(self):
        os.unlink(self.token_file)
        self.assertEqual(self.fetch_cls().cls, "credential_unreadable")
        self.assertEqual(self.github.request_count, 0)
        for content in ("", "  \n", "two words", "line1\nline2", "tok\x00en", "é"):
            with self.subTest(content=content):
                with open(self.token_file, "w", encoding="utf-8") as f:
                    f.write(content)
                self.assertEqual(self.fetch_cls().cls, "credential_unreadable")
        self.assertEqual(self.github.request_count, 0)

    def test_permission_error_and_directory_are_credential_unreadable(self):
        os.chmod(self.token_file, 0)
        self.assertEqual(self.fetch_cls().cls, "credential_unreadable")
        os.chmod(self.token_file, 0o600)
        os.unlink(self.token_file)
        os.mkdir(self.token_file)
        self.assertEqual(self.fetch_cls().cls, "credential_unreadable")

    def test_symlinked_token_file_is_refused(self):
        real = os.path.join(self.dir, "real")
        os.rename(self.token_file, real)
        os.symlink(real, self.token_file)
        self.assertEqual(self.fetch_cls().cls, "credential_unreadable")

    def test_credentials_directory_takes_priority(self):
        creds = os.path.join(self.dir, "creds")
        os.mkdir(creds)
        with open(os.path.join(creds, "gateway-token"), "w") as f:
            f.write("CRED_DIR_TOKEN\n")
        os.environ["CREDENTIALS_DIRECTORY"] = creds
        tun.fetch_gateway(self.cfg())
        self.assertEqual(self.github.requests[-1][1], "Bearer CRED_DIR_TOKEN")
        os.unlink(os.path.join(creds, "gateway-token"))
        tun.fetch_gateway(self.cfg())
        self.assertEqual(self.github.requests[-1][1], "Bearer " + TOKEN)


VARIABLE = "/repos/FATESAIKOU/MyLinuxPool/actions/variables/NODE_GATEWAY"


class BackoffAndMetaTests(GitHubFetchBase):
    def read(self, name):
        return json.load(open(os.path.join(self.state, name)))

    def run_failure(self, cfg, cls, retry_after=None):
        tun.record_failure(cfg, cls, retry_after)

    def test_exponential_backoff_60_to_900_and_persisted(self):
        cfg = self.cfg(FETCH_MIN_INTERVAL="60", FETCH_BACKOFF_MAX="900")
        delays = []
        for _ in range(7):
            before = time.time()
            self.run_failure(cfg, "unreachable")
            data = self.read(".tunnel_fetch_backoff")
            delays.append(round(data["next_allowed_epoch"] - before))
        self.assertEqual(delays, [60, 120, 240, 480, 900, 900, 900])
        self.assertEqual(data["consecutive_failures"], 7)
        self.assertEqual(self.read(".tunnel_fetch_state")["status"], "unreachable")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, ".tunnel_fetch_backoff")).st_mode), 0o600)

    def test_retry_after_extends_but_never_shortens(self):
        cfg = self.cfg(FETCH_MIN_INTERVAL="60")
        before = time.time()
        self.run_failure(cfg, "rate_limited", 1800)
        self.assertAlmostEqual(self.read(".tunnel_fetch_backoff")["next_allowed_epoch"] - before, 1800, delta=5)
        os.unlink(os.path.join(self.state, ".tunnel_fetch_backoff"))
        before = time.time()
        self.run_failure(cfg, "rate_limited", 5)
        self.assertAlmostEqual(self.read(".tunnel_fetch_backoff")["next_allowed_epoch"] - before, 60, delta=5)

    def test_state_classes_are_only_the_fixed_whitelist(self):
        cfg = self.cfg()
        for cls in tun.FETCH_CLASSES:
            tun.write_fetch_state(cfg, cls)
            self.assertEqual(self.read(".tunnel_fetch_state")["status"], cls)
        os.unlink(os.path.join(self.state, ".tunnel_fetch_state"))
        tun.write_fetch_state(cfg, "SOMETHING_ELSE")
        self.assertFalse(os.path.exists(os.path.join(self.state, ".tunnel_fetch_state")))

    def test_token_meta_header_handling(self):
        cfg = self.cfg()
        now = time.time()
        tun.write_token_meta(cfg, {"github-authentication-token-expiration": "2099-02-01 00:00:00 UTC"}, now)
        meta = self.read(".tunnel_token_meta")
        self.assertEqual(meta["expires_at"], "2099-02-01T00:00:00Z")
        self.assertEqual(meta["http_status"], 200)
        self.assertIn("fetched_at", meta)
        for header in (None, "", "garbage", "1999-01-01 00:00:00 UTC", tun.utc_iso(now)):
            with self.subTest(header=header):
                tun.write_token_meta(cfg, {"github-authentication-token-expiration": header}, now + 1)
                self.assertNotIn("expires_at", self.read(".tunnel_token_meta"))
        blob = open(os.path.join(self.state, ".tunnel_token_meta")).read()
        self.assertNotIn(TOKEN, blob)


class FetchCheckTests(GitHubFetchBase):
    def run_check(self, **env):
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.dir,
                "LGW_TUNNEL_ENV": self.env_path, "LGW_STATE_DIR": self.state,
                "LGW_GATEWAY_API_BASE": self.github.base_url, "LGW_ALLOW_INSECURE_API_BASE": "1"}
        full.update(env)
        full = {k: v for k, v in full.items() if v is not None}
        return subprocess.run([sys.executable, TUNNEL, "--fetch-check"], capture_output=True,
                              text=True, env=full, cwd=REPO_ROOT, timeout=30)

    def snapshot(self):
        files = {}
        for root, _, names in os.walk(self.dir):
            for name in names:
                path = os.path.join(root, name)
                files[path] = (os.stat(path).st_mtime_ns, open(path, "rb").read())
        return files

    def test_success_is_read_only_and_leak_free(self):
        self.write_env()
        # 預先放一份既有狀態，確認一個位元組都不動
        for name, content in (("gateway.json", "{}"), (".tunnel_generation", "{}"),
                              (".tunnel_fetch_state", '{"status":"auth_failed"}')):
            open(os.path.join(self.state, name), "w").write(content)
        before = self.snapshot()
        res = self.run_check()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("如 check 仍紅請執行 llmgwctl follow-reset --fetch-state", res.stdout)
        self.assertIn("http_status: 200", res.stdout)
        self.assertEqual(self.github.request_count, 1)
        self.assertEqual(self.snapshot(), before)
        for leak in (TOKEN, SECRET_IP, ed25519_key(), ed25519_key().split(" ")[1], "static.example"):
            self.assertNotIn(leak, res.stdout)
            self.assertNotIn(leak, res.stderr)

    def test_empty_state_dir_stays_empty(self):
        self.write_env()
        self.assertEqual(self.run_check().returncode, 0)
        self.assertEqual(os.listdir(self.state), [])

    def test_failures_exit_1_and_name_only_the_class(self):
        self.write_env()
        for status, cls in ((401, "auth_failed"), (429, "rate_limited"), (500, "unreachable")):
            with self.subTest(status=status):
                self.github.reset_behavior()
                self.github.status = status
                res = self.run_check()
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn(cls, res.stdout)
                self.assertNotIn(TOKEN, res.stdout + res.stderr)
                self.assertEqual(os.listdir(self.state), [])
        self.github.reset_behavior()
        self.github.set_gateway(**self.gateway(tunnel_user="other"))
        res = self.run_check()
        self.assertEqual(res.returncode, 1)
        self.assertIn("invalid", res.stdout)
        self.assertNotIn("other", res.stdout.replace("fetch_check", ""))

    def test_config_problems_exit_78(self):
        self.write_env(GATEWAY_GITHUB_KEY_FILE="")
        self.assertEqual(self.run_check().returncode, 78)
        self.write_env()
        self.assertEqual(self.run_check(LGW_ALLOW_INSECURE_API_BASE=None).returncode, 78)
        self.assertEqual(self.run_check(LGW_GATEWAY_API_BASE="http://evil.example").returncode, 78)
        self.assertEqual(self.github.request_count, 0)

    def test_token_file_unreadable_exit_1_not_78(self):
        self.write_env()
        os.unlink(self.token_file)
        res = self.run_check()
        self.assertEqual(res.returncode, 1)
        self.assertIn("credential_unreadable", res.stdout)
        self.assertEqual(os.listdir(self.state), [])


if __name__ == "__main__":
    unittest.main()
