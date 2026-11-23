"""lgwd/framing.py 的純函式測試（#222 D2 PR-1；C-102～C-105 的判讀面）。

只測 `inspect`（request-target 原文、標頭至多一次、Content-Length、1 MiB）與
`read_body`（單調時鐘的**總**期限）。伺服器端的行為（誰回 401／503／413、號誌、
`/healthz` 豁免、off 差分）在 tests/test_http_framing.py。
"""

import subprocess
import sys
import time
import unittest
from email.message import Message
from email.parser import Parser
from pathlib import Path
from unittest import mock

from lgwd import framing

MIB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parent.parent


def headers(*pairs):
    """建 email.message.Message；同名重複 append（get_all 才看得到兩次）。"""
    msg = Message()
    for name, value in pairs:
        msg[name] = value
    return msg


class FakeReader:
    """假 rfile：read1 依序吐 chunks（bytes），吐完之後回 b""。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read1(self, size):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class SilentReader:
    """假 rfile：read1 一律拋 TimeoutError（模擬 recv 逾時）。"""

    def read1(self, size):
        raise TimeoutError("timed out")


class FakeSock:
    def __init__(self, timeout=None):
        self.timeout = timeout
        self.set_calls = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value
        self.set_calls.append(value)


class C102TargetTests(unittest.TestCase):
    """C-102：request-target 取原文第二段、只收 origin-form。"""

    def test_origin_form_ok_and_raw_path_kept(self):
        fr = framing.inspect("GET /v1/resolve?agent=x HTTP/1.1", headers())
        self.assertTrue(fr.target_ok)
        self.assertTrue(fr.ok)
        # 路由 path 取自同一段原文：不解 %2F、不經 stdlib 改寫
        self.assertEqual(fr.target, "/v1/resolve?agent=x")
        self.assertEqual(fr.path, "/v1/resolve")
        self.assertEqual(fr.query, "agent=x")
        fr = framing.inspect("GET /v1/roles/a%2Fb?agent=opencode HTTP/1.0", headers())
        self.assertTrue(fr.ok)
        self.assertEqual(fr.target, "/v1/roles/a%2Fb?agent=opencode")
        self.assertEqual(fr.path, "/v1/roles/a%2Fb")
        self.assertEqual(fr.query, "agent=opencode")

    def test_rejects_non_origin_forms(self):
        cases = {
            "double_slash": "GET //v1/resolve HTTP/1.0",
            "absolute_uri": "GET http://example.invalid/v1/resolve HTTP/1.0",
            "absolute_uri_https": "GET https://example.invalid/healthz HTTP/1.1",
            "authority_form": "CONNECT example.invalid:443 HTTP/1.1",
            "asterisk": "OPTIONS * HTTP/1.0",
            "http09": "GET /v1/resolve",
            "query_only": "GET ?agent=x HTTP/1.0",
            "two_spaces": "GET  /v1/resolve HTTP/1.0",
            "four_tokens": "GET /v1/resolve HTTP/1.0 extra",
            "empty": "",
        }
        for name, line in cases.items():
            with self.subTest(name=name):
                fr = framing.inspect(line, headers())
                self.assertFalse(fr.target_ok, line)
                self.assertFalse(fr.ok, line)

    def test_rejects_forbidden_target_bytes(self):
        for name, target in {
            "fragment": "/v1/resolve#frag",
            "non_ascii": "/v1/roles/é",
            "control": "/v1/resolve\x01",
            "nul": "/v1/resolve\x00",
        }.items():
            with self.subTest(name=name):
                fr = framing.inspect("GET {} HTTP/1.0".format(target), headers())
                self.assertFalse(fr.target_ok, target)

    def test_rejects_control_bytes_in_version(self):
        fr = framing.inspect("GET /v1/resolve HTTP/1.0\r\n", headers())
        self.assertFalse(fr.target_ok)


class C103ContentLengthTests(unittest.TestCase):
    """C-103：Content-Length 形狀、GET 不得非零、> 1 MiB、Transfer-Encoding 即拒。"""

    def test_single_numeric_length_ok(self):
        fr = framing.inspect("POST /v1/x HTTP/1.0", headers(("Content-Length", "5")))
        self.assertTrue(fr.ok)
        self.assertEqual(fr.length, 5)
        self.assertFalse(fr.too_large)

    def test_missing_length_is_none_but_ok(self):
        fr = framing.inspect("POST /v1/x HTTP/1.0", headers())
        self.assertTrue(fr.ok)
        self.assertIsNone(fr.length)
        # GET 沒有 CL 也合法
        fr = framing.inspect("GET /v1/x HTTP/1.0", headers())
        self.assertTrue(fr.ok)

    def test_bad_length_values_rejected(self):
        for value in ("+5", "05x", "", " 5", "5 ", "-1", "5.0", "0x10", "5,5"):
            with self.subTest(value=value):
                fr = framing.inspect("POST /v1/x HTTP/1.0",
                                     headers(("Content-Length", value)))
                self.assertFalse(fr.ok, value)
                self.assertIsNone(fr.length, value)

    def test_duplicate_length_rejected(self):
        fr = framing.inspect("POST /v1/x HTTP/1.0",
                             headers(("Content-Length", "2"), ("Content-Length", "2")))
        self.assertFalse(fr.ok)
        self.assertIsNone(fr.length)

    def test_get_with_nonzero_length_rejected(self):
        for value in ("1", "2"):
            with self.subTest(value=value):
                fr = framing.inspect("GET /v1/x HTTP/1.0",
                                     headers(("Content-Length", value)))
                self.assertFalse(fr.ok)
        # GET + CL: 0 是允許的
        fr = framing.inspect("GET /v1/x HTTP/1.0", headers(("Content-Length", "0")))
        self.assertTrue(fr.ok)
        self.assertEqual(fr.length, 0)

    def test_transfer_encoding_rejected(self):
        for pairs in ((("Transfer-Encoding", "chunked"),),
                      (("Transfer-Encoding", "identity"),),
                      (("Transfer-Encoding", ""),),
                      (("Transfer-Encoding", "chunked"),
                       ("Transfer-Encoding", "chunked"))):
            with self.subTest(pairs=pairs):
                fr = framing.inspect("POST /v1/x HTTP/1.0", headers(*pairs))
                self.assertFalse(fr.ok)

    def test_one_mib_boundary(self):
        fr = framing.inspect("POST /v1/x HTTP/1.0",
                             headers(("Content-Length", str(MIB))))
        self.assertTrue(fr.ok)
        self.assertFalse(fr.too_large)
        fr = framing.inspect("POST /v1/x HTTP/1.0",
                             headers(("Content-Length", str(MIB + 1))))
        self.assertTrue(fr.target_ok)
        self.assertTrue(fr.too_large)
        self.assertFalse(fr.ok)
        self.assertEqual(fr.length, MIB + 1)


class C104DuplicateHeaderTests(unittest.TestCase):
    """C-104：CL／TE／Host／五個 X-LGW-* 以 get_all 判各至多一次，不分大小寫。"""

    def test_duplicate_host_rejected(self):
        fr = framing.inspect("GET /v1/x HTTP/1.1",
                             headers(("Host", "a"), ("Host", "b")))
        self.assertFalse(fr.ok)
        fr = framing.inspect("GET /v1/x HTTP/1.1", headers(("Host", "a")))
        self.assertTrue(fr.ok)

    def test_each_signed_header_duplicate_rejected(self):
        for name in ("X-LGW-Auth-Version", "X-LGW-Key-Id", "X-LGW-Timestamp",
                     "X-LGW-Nonce", "X-LGW-Signature"):
            with self.subTest(name=name):
                fr = framing.inspect("GET /v1/x HTTP/1.0",
                                     headers((name, "a"), (name, "b")))
                self.assertFalse(fr.ok, name)

    def test_all_signed_headers_single_ok(self):
        pairs = [("X-LGW-Auth-Version", "1"), ("X-LGW-Key-Id", "k"),
                 ("X-LGW-Timestamp", "1795392000"),
                 ("X-LGW-Nonce", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"),
                 ("X-LGW-Signature", "x" * 240), ("Host", "lgw")]
        fr = framing.inspect("GET /v1/x HTTP/1.0", headers(*pairs))
        self.assertTrue(fr.ok)

    def test_header_names_case_insensitive(self):
        fr = framing.inspect("POST /v1/x HTTP/1.0",
                             headers(("Content-Length", "2"), ("content-length", "2")))
        self.assertFalse(fr.ok)
        fr = framing.inspect("GET /v1/x HTTP/1.1",
                             headers(("Host", "a"), ("HOST", "b")))
        self.assertFalse(fr.ok)
        fr = framing.inspect("GET /v1/x HTTP/1.0",
                             headers(("x-lgw-key-id", "a"), ("X-LGW-KEY-ID", "b")))
        self.assertFalse(fr.ok)


class HeaderNameTokenTests(unittest.TestCase):
    """agy 觀察 1：標頭名稱須為 RFC 7230 token（冒號前空白、控制字元一律拒）。"""

    def test_non_token_header_names_rejected(self):
        for name in ("Transfer-Encoding ", "Content-Length ", "X LGW", "Bad\x01",
                     "X-LGW-Key-Id\t"):
            with self.subTest(name=repr(name)):
                fr = framing.inspect("GET /v1/x HTTP/1.0", headers((name, "x")))
                self.assertFalse(fr.ok, repr(name))

    def test_token_header_names_accepted(self):
        # tchar 全形：!#$%&'*+-.^_`|~ 與英數
        name = "!#$%&'*+-.^_`|~09AZaz"
        fr = framing.inspect("GET /v1/x HTTP/1.0", headers((name, "x")))
        self.assertTrue(fr.ok)
        fr = framing.inspect("GET /v1/x HTTP/1.0", headers(("X-LGW-Key-Id", "k")))
        self.assertTrue(fr.ok)

    def test_dropped_bad_header_name_is_rejected(self):
        """有些 email.parser 會把冒號前空白的標頭整行丟掉，只留 defect。"""
        msg = Parser().parsestr("Host: x\r\nTransfer-Encoding : chunked\r\n\r\n")
        self.assertTrue(msg.defects)
        fr = framing.inspect("POST /v1/x HTTP/1.0", msg)
        self.assertFalse(fr.ok)


class ReadBodyTests(unittest.TestCase):
    """C-105 的讀取面：總期限不因每次 recv 重新起算。"""

    def test_reads_exact_length(self):
        sock = FakeSock()
        deadline = time.monotonic() + 10.0
        raw = framing.read_body(FakeReader([b"ab", b"cd"]), 4, deadline, sock=sock)
        self.assertEqual(raw, b"abcd")

    def test_sock_is_required(self):
        """低-1-3：不從 rfile 推導 socket；沒傳 sock 就丟錯，不靜默降級。"""
        with self.assertRaises(TypeError):
            framing.read_body(FakeReader([b"ab"]), 2, time.monotonic() + 10.0)

    def test_zero_length_touches_nothing(self):
        reader = SilentReader()
        self.assertEqual(framing.read_body(reader, 0, 0.0, sock=FakeSock()), b"")

    def test_deadline_in_the_past(self):
        with mock.patch("lgwd.framing.time.monotonic", return_value=100.0):
            with self.assertRaises(framing.ReadDeadline):
                framing.read_body(FakeReader([b"ab"]), 4, 99.0, sock=FakeSock())

    def test_silent_socket_is_read_deadline(self):
        with self.assertRaises(framing.ReadDeadline):
            framing.read_body(SilentReader(), 4, time.monotonic() + 100.0, sock=FakeSock())

    def test_early_eof_is_short_body(self):
        with self.assertRaises(framing.ShortBody):
            framing.read_body(FakeReader([]), 4, time.monotonic() + 100.0, sock=FakeSock())

    def test_timeout_shrinks_with_the_fixed_deadline(self):
        """每次 recv 的逾時＝截止時刻−現在，不是每次都給滿（C-105 的變異點）。"""
        sock = FakeSock()
        clock = [9.0, 9.5, 9.9]
        with mock.patch("lgwd.framing.time.monotonic", side_effect=clock):
            raw = framing.read_body(FakeReader([b"a", b"b", b"c"]), 3, 10.0, sock=sock)
        self.assertEqual(raw, b"abc")
        for got, want in zip(sock.set_calls, (1.0, 0.5, 0.1)):
            self.assertAlmostEqual(got, want, places=6)
        # 收尾把連線原本的逾時還原（不給後續請求留下新逾時）
        self.assertIsNone(sock.timeout)


class ImportBoundaryTests(unittest.TestCase):
    """低-1-1：framing 是純判讀模組，不 import http／main／registry（沒有環）。"""

    def test_framing_imports_no_forbidden_modules(self):
        code = (
            "import sys, lgwd.framing;"
            "bad=[m for m in ('lgwd.http','lgwd.main','lgwd.registry')"
            " if m in sys.modules];"
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


if __name__ == "__main__":
    unittest.main()
