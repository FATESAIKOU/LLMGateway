"""lgwd/http.py 的預驗章 framing（#222 D2 PR-1）：C-101～C-108 的伺服器面。

判讀的純函式在 tests/test_framing.py；這裡驗「誰回 401／503／413、什麼時候不進
service、`/healthz` 的豁免、off 的零變化」。

**C-108 的基準**：`tests/vectors/http_framing_off_baseline.json` 是在**改動前**
的 `lgwd/http.py`（D1 版，`make_api_server` 還沒有 `gate` 參數）上，用本檔的
``CORPUS_REQUESTS`` 逐筆打真 loopback 錄下來的（重錄方法寫在該 JSON 的
``note``）。只把 `Date`／`Server` 的**值**正規化，其餘 bytes 逐筆比對。
"""

import base64
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lgwd.http import make_api_server

try:  # python3 -m pytest
    from .test_http import FakeService
except ImportError:  # python3 -m unittest discover -s tests
    from test_http import FakeService

HOST = "127.0.0.1"
BASELINE = Path(__file__).resolve().parent / "vectors" / "http_framing_off_baseline.json"
UNAUTHORIZED = b'{"error": "unauthorized"}'
AUTH_UNAVAILABLE = b'{"error": "auth_unavailable"}'
BODY_TOO_LARGE = b'{"error": "body_too_large"}'
COOLDOWN = "/v1/subscriptions/claude-personal/cooldown"
JSON_BODY = b'{"until": "2026-10-11T15:00:00Z"}'


class FakeGate:
    """gate 只需要 .mode（C-101）；本 PR 不實作簽章與名冊。"""

    def __init__(self, mode):
        self.mode = mode


def req(method, target, extra=(), body=b"", version="HTTP/1.0"):
    """組原始請求 bytes（刻意不經 http.client，才能送怪東西）。"""
    lines = ["{} {} {}".format(method, target, version)]
    lines.extend("{}: {}".format(name, value) for name, value in extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def exchange(port, raw, settle=5.0):
    """送一份原始請求、把回應讀到 EOF（含連線被重設的情況）。"""
    with socket.create_connection((HOST, port), timeout=settle) as sock:
        try:
            sock.sendall(raw)
        except OSError:
            pass
        sock.settimeout(settle)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def recv_until_eof(sock):
    """把已連線的 socket 讀到 EOF（含逾期／連線重設），回 bytes。"""
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, ConnectionResetError):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def normalize(response):
    """把 Date／Server 的值正規化（其餘逐 byte 保留）。"""
    out = []
    for line in response.split(b"\r\n"):
        lowered = line.lower()
        if lowered.startswith(b"date:"):
            line = b"Date: <normalized>"
        elif lowered.startswith(b"server:"):
            line = b"Server: <normalized>"
        out.append(line)
    return b"\r\n".join(out)


def parse_response(response):
    """回 (status, body, headers dict)；HTTP/0.9 的回應沒有狀態列。"""
    head, _, body = response.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").lower()] = value.strip().decode("latin-1")
    return status, body, headers


def start(fake, gate=None):
    """起一個 loopback 伺服器；回 (server, thread, port)。"""
    server = make_api_server(fake, HOST, 0, gate=gate)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class FramingCase(unittest.TestCase):
    gate = None

    def setUp(self):
        self.fake = FakeService()
        self.server, self._thread, self.port = start(self.fake, self.gate)

    def tearDown(self):
        stop(self.server, self._thread)

    def raw(self, method, target, extra=(), body=b"", version="HTTP/1.0"):
        return exchange(self.port, req(method, target, extra, body, version))

    def call(self, method, target, extra=(), body=b"", version="HTTP/1.0"):
        return parse_response(self.raw(method, target, extra, body, version))


# ---------------------------------------------------------------- C-108 語料
# 每個都是「合併前 D1 行為」的原始請求；基準回應錄在
# tests/vectors/http_framing_off_baseline.json（C-108 要求至少 25 筆）。
CORPUS_REQUESTS = [
    ("list_ok", req("GET", "/v1/subscriptions")),
    ("list_ok_http11", req("GET", "/v1/subscriptions",
                           [("Host", "lgw"), ("Connection", "close")],
                           version="HTTP/1.1")),
    ("list_query", req("GET", "/v1/subscriptions?x=1&agent=opencode")),
    ("usage_ok", req("GET", "/v1/usage")),
    ("resolve_ok", req("GET", "/v1/resolve?agent=opencode&provider=opencode-zen")),
    ("resolve_pinned", req(
        "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&model=big-pickle&exclude=")),
    ("resolve_bad_query", req("GET", "/v1/resolve?agent=x&agent=y&provider=opencode-zen")),
    ("roles_ok", req("GET", "/v1/roles/a%2Fb?agent=opencode")),
    ("roles_missing_agent", req("GET", "/v1/roles/impl")),
    ("cooldown_ok", req("POST", COOLDOWN,
                        [("Content-Type", "application/json"),
                         ("Content-Length", str(len(JSON_BODY)))], JSON_BODY)),
    ("cooldown_bad_json", req("POST", COOLDOWN, [("Content-Length", "5")], b"{nope")),
    ("cooldown_json_list", req("POST", COOLDOWN, [("Content-Length", "3")], b"[1]")),
    ("cooldown_no_length", req("POST", COOLDOWN)),
    ("cooldown_dup_content_length", req(
        "POST", COOLDOWN, [("Content-Length", "2"), ("Content-Length", "2")], b"{}")),
    ("cooldown_bad_content_length", req(
        "POST", COOLDOWN, [("Content-Length", "+5")], b"{}abc")),
    ("cooldown_empty_content_length", req(
        "POST", COOLDOWN, [("Content-Length", "")], b"{}")),
    ("too_large_declared", req(
        "POST", COOLDOWN, [("Content-Length", str(2 * 1024 * 1024))], b"{}")),
    ("healthz_ok", req("GET", "/healthz")),
    ("healthz_post", req("POST", "/healthz", [("Content-Length", "2")], b"{}")),
    ("unknown_path", req("GET", "/v1/nope")),
    ("unknown_path_te", req("GET", "/v1/nope", [("Transfer-Encoding", "chunked")])),
    ("head_list", req("HEAD", "/v1/subscriptions")),
    ("options_asterisk", req("OPTIONS", "*")),
    ("dup_transfer_encoding", req(
        "GET", "/v1/subscriptions",
        [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")])),
    ("header_name_space_before_colon", req(
        "GET", "/v1/subscriptions", [("Transfer-Encoding ", "chunked")])),
    ("transfer_encoding_chunked", req(
        "POST", COOLDOWN, [("Transfer-Encoding", "chunked")], b"0\r\n\r\n")),
    ("double_slash_resolve", req(
        "GET", "//v1/resolve?agent=opencode&provider=opencode-zen")),
    ("double_slash_roles", req("GET", "//v1/roles/a%2Fb?agent=opencode")),
    ("absolute_uri", req("GET", "http://example.invalid/v1/subscriptions")),
    ("http09_no_version", b"GET /v1/subscriptions\r\n\r\n"),
    ("get_with_body", req("GET", "/v1/subscriptions", [("Content-Length", "3")], b"abc")),
    ("get_nonzero_length_resolve", req(
        "GET", "/v1/resolve?agent=opencode&provider=opencode-zen",
        [("Content-Length", "1")], b"x")),
    ("fragment", req("GET", "/v1/resolve?agent=opencode&provider=opencode-zen#frag")),
    ("dup_host_http11", req("GET", "/v1/subscriptions",
                            [("Host", "a"), ("Host", "b"), ("Connection", "close")],
                            version="HTTP/1.1")),
    ("dup_xlgw_key_id", req("GET", "/v1/subscriptions",
                            [("X-LGW-Key-Id", "a"), ("X-LGW-Key-Id", "b")])),
    ("non_ascii_path", b"GET /v1/roles/\xff HTTP/1.0\r\n\r\n"),
    ("put_unknown_method", req("PUT", "/v1/subscriptions", version="HTTP/1.0")),
    ("missing_host_http11", req("GET", "/v1/subscriptions",
                                [("Connection", "close")], version="HTTP/1.1")),
    ("cooldown_get_method", req("GET", COOLDOWN)),
]


def load_baseline():
    with BASELINE.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["schema"] == 1, data["schema"]
    return {case["name"]: base64.b64decode(case["response_b64"])
            for case in data["cases"]}


class C108OffBaselineTests(unittest.TestCase):
    """C-108（＋C-101）：off 下每一筆怪請求的回應 bytes 與合併前逐筆相同。"""

    def test_corpus_has_at_least_25_cases(self):
        names = [name for name, _ in CORPUS_REQUESTS]
        self.assertGreaterEqual(len(names), 25)
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(set(load_baseline()), set(names))

    def _replay(self, gate):
        fake = FakeService()
        server, thread, port = start(fake, gate)
        try:
            for name, raw in CORPUS_REQUESTS:
                with self.subTest(case=name):
                    got = normalize(exchange(port, raw))
                    self.assertEqual(got, load_baseline()[name])
        finally:
            stop(server, thread)

    def test_C108_off_gate_none_matches_d1_baseline(self):
        self._replay(None)

    def test_C101_off_gate_object_matches_d1_baseline(self):
        """gate 物件但 mode=off：與 gate=None 逐 byte 相同（C-101）。"""
        self._replay(FakeGate("off"))


class C101OffBehaviourTests(FramingCase):
    """C-101：off 完全不套 framing（重複標頭、TE 照舊進 service）。"""

    def test_duplicate_content_length_still_served(self):
        status, body, _ = self.call(
            "POST", COOLDOWN,
            [("Content-Length", "2"), ("Content-Length", "2")], b"{}")
        self.assertEqual((status, body), (204, b""))  # 照舊進 service，不是 401
        self.assertEqual(self.fake.calls,
                         [("register_cooldown", "claude-personal", {})])

    def test_transfer_encoding_still_served(self):
        status, _, _ = self.call(
            "GET", "/v1/subscriptions", [("Transfer-Encoding", "chunked")])
        self.assertEqual(status, 200)

    def test_double_slash_still_routed_like_d1(self):
        status, _, _ = self.call(
            "GET", "//v1/resolve?agent=opencode&provider=opencode-zen")
        self.assertEqual(status, 200)  # stdlib 把 // 改寫成 /

    def test_off_has_no_new_deadline(self):
        """off 不設新逾時（C-105／C-101）：慢慢送 body 仍照舊完成。"""
        with mock.patch("lgwd.http._READ_DEADLINE_SECONDS", 0.2):
            with socket.create_connection((HOST, self.port), timeout=5) as sock:
                head = req("POST", COOLDOWN,
                           [("Content-Type", "application/json"),
                            ("Content-Length", str(len(JSON_BODY)))])
                sock.sendall(head + JSON_BODY[:5])
                time.sleep(0.4)
                sock.sendall(JSON_BODY[5:])
                sock.settimeout(5)
                resp = sock.recv(65536)
        status, _, _ = parse_response(resp)
        self.assertEqual(status, 204)


class C102EnforceTargetTests(FramingCase):
    """C-102：enforce 下非 origin-form 的 request-target 一律 401，不進 service。"""

    gate = FakeGate("enforce")

    def rejected(self, raw):
        resp = exchange(self.port, raw)
        status, body, headers = parse_response(resp)
        self.assertEqual(status, 401, raw)
        self.assertEqual(body, UNAUTHORIZED)
        self.assertNotIn("www-authenticate", headers)

    def test_double_slash(self):
        self.rejected(req("GET", "//v1/resolve?agent=opencode&provider=opencode-zen"))
        self.assertEqual(self.fake.calls, [])

    def test_absolute_uri(self):
        self.rejected(req("GET", "http://example.invalid/v1/subscriptions"))
        self.assertEqual(self.fake.calls, [])

    def test_http09(self):
        """HTTP/0.9 沒有狀態列（stdlib 對 0.9 不送標頭），拒絕只剩 body。"""
        resp = exchange(self.port, b"GET /v1/subscriptions\r\n\r\n")
        self.assertEqual(resp, UNAUTHORIZED)
        self.assertNotIn(b"HTTP/", resp)
        self.assertEqual(self.fake.calls, [])

    def test_authority_form_and_asterisk(self):
        # 未實作的方法（CONNECT／OPTIONS…）根本不進 handler（維持 501，見 design）；
        # 這裡用實作的 GET 送 authority-form 與 * 才走得到 framing。
        self.rejected(req("GET", "example.invalid:443"))
        self.rejected(req("GET", "*"))
        self.assertEqual(self.fake.calls, [])

    def test_fragment_and_non_ascii(self):
        self.rejected(req("GET", "/v1/resolve#frag"))
        self.rejected(b"GET /v1/roles/\xff HTTP/1.0\r\n\r\n")
        self.assertEqual(self.fake.calls, [])

    def test_raw_target_is_used_for_routing(self):
        status, _, _ = self.call("GET", "/v1/roles/a%2Fb?agent=opencode")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ("role_candidates", "a/b", "opencode", None))


class C103BodyFramingTests(FramingCase):
    """C-103／C-104／C-107：Content-Length、Transfer-Encoding、重複標頭。"""

    gate = FakeGate("enforce")

    def test_bad_content_length(self):
        for value in ("+5", "05x", "", "5 "):
            with self.subTest(value=value):
                status, body, headers = self.call(
                    "POST", COOLDOWN, [("Content-Length", value)], b"{}")
                self.assertEqual(status, 401)
                self.assertEqual(body, UNAUTHORIZED)
                self.assertNotIn("www-authenticate", headers)
        self.assertEqual(self.fake.calls, [])

    def test_duplicate_content_length(self):
        status, body, _ = self.call(
            "POST", COOLDOWN, [("Content-Length", "2"), ("Content-Length", "2")], b"{}")
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertEqual(self.fake.calls, [])

    def test_transfer_encoding(self):
        for extra in ([("Transfer-Encoding", "chunked")],
                      [("Transfer-Encoding", "identity")],
                      [("Transfer-Encoding", "chunked"),
                       ("Transfer-Encoding", "chunked")]):
            with self.subTest(extra=extra):
                status, body, _ = self.call(
                    "POST", COOLDOWN, extra + [("Content-Length", "2")], b"{}")
                self.assertEqual((status, body), (401, UNAUTHORIZED))
        status, _, _ = self.call("GET", "/v1/subscriptions",
                                 [("Transfer-Encoding", "chunked")])
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.calls, [])

    def test_get_with_nonzero_content_length(self):
        status, _, _ = self.call("GET", "/v1/subscriptions",
                                 [("Content-Length", "1")], b"x")
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.calls, [])
        # GET + CL: 0 是合法的（framing 不過是「非零」）
        status, _, _ = self.call("GET", "/v1/subscriptions",
                                 [("Content-Length", "0")])
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls, [("list_subscriptions",)])

    def test_duplicate_host_and_signed_headers(self):
        cases = {
            "host": [("Host", "a"), ("Host", "b")],
            "auth_version": [("X-LGW-Auth-Version", "1"), ("X-LGW-Auth-Version", "1")],
            "key_id": [("X-LGW-Key-Id", "a"), ("x-lgw-key-id", "b")],
            "timestamp": [("X-LGW-Timestamp", "1"), ("X-LGW-Timestamp", "2")],
            "nonce": [("X-LGW-Nonce", "a"), ("X-LGW-Nonce", "b")],
            "signature": [("X-LGW-Signature", "a"), ("X-LGW-Signature", "b")],
        }
        for name, extra in cases.items():
            with self.subTest(name=name):
                status, _, _ = self.call("GET", "/v1/subscriptions", extra)
                self.assertEqual(status, 401)
        self.assertEqual(self.fake.calls, [])

    def test_single_signed_headers_pass_framing(self):
        """PR-1 只看「至多一次、形狀」；值的驗證是 PR-6 的 gate。"""
        extra = [("X-LGW-Auth-Version", "1"), ("X-LGW-Key-Id", "k"),
                 ("X-LGW-Timestamp", "1795392000"),
                 ("X-LGW-Nonce", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"),
                 ("X-LGW-Signature", "x" * 240)]
        status, _, _ = self.call("GET", "/v1/subscriptions", extra)
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls, [("list_subscriptions",)])

    def test_valid_cooldown_passes(self):
        status, body, _ = self.call(
            "POST", COOLDOWN,
            [("Content-Type", "application/json"),
             ("Content-Length", str(len(JSON_BODY)))], JSON_BODY)
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(self.fake.calls,
                         [("register_cooldown", "claude-personal",
                           {"until": "2026-10-11T15:00:00Z"})])

    def test_valid_body_but_bad_json_is_400_not_401(self):
        """framing 過了之後的錯誤仍走既有 400（不是認證失敗）。"""
        status, body, _ = self.call("POST", COOLDOWN, [("Content-Length", "5")], b"{nope")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body.decode())["error"], "bad_request")


class C103TooLargeTests(unittest.TestCase):
    """C-103：body 超過 1 MiB 照現行 413，三種模式相同（大小檢查在讀 body 前）。"""

    def test_too_large_is_413_in_all_modes(self):
        for gate in (None, FakeGate("off"), FakeGate("warn"), FakeGate("enforce")):
            mode = "none" if gate is None else gate.mode
            with self.subTest(mode=mode):
                fake = FakeService()
                server, thread, port = start(fake, gate)
                try:
                    resp = exchange(port, req(
                        "POST", COOLDOWN,
                        [("Content-Length", str(2 * 1024 * 1024))], b"{}"))
                finally:
                    stop(server, thread)
                status, body, _ = parse_response(resp)
                self.assertEqual((status, body), (413, BODY_TOO_LARGE))
                self.assertEqual(fake.calls, [])

    def test_exactly_one_mib_is_accepted(self):
        for gate in (FakeGate("warn"), FakeGate("enforce")):
            with self.subTest(mode=gate.mode):
                fake = FakeService()
                server, thread, port = start(fake, gate)
                prefix = b'{"until":"2026-10-11T15:00:00Z","pad":"'
                pad = 1024 * 1024 - len(prefix) - len(b'"}')
                raw = prefix + b"a" * pad + b'"}'
                try:
                    resp = exchange(port, req(
                        "POST", COOLDOWN, [("Content-Length", str(len(raw)))], raw))
                finally:
                    stop(server, thread)
                status, _, _ = parse_response(resp)
                self.assertEqual(status, 204)


class C107WarnTests(FramingCase):
    """C-107：warn 記 would-deny（mode=warn outcome=deny）但照舊處理業務。"""

    gate = FakeGate("warn")

    def test_would_deny_then_serve_like_d1(self):
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, body, _ = self.call(
                "POST", COOLDOWN,
                [("Content-Length", "2"), ("Content-Length", "2")], b"{}")
        self.assertEqual((status, body), (204, b""))  # 照舊進 service
        self.assertEqual(self.fake.calls,
                         [("register_cooldown", "claude-personal", {})])
        text = "\n".join(logs.output)
        # 中-1-2：outcome 只用白名單值，would-deny 記成 mode=warn outcome=deny。
        self.assertIn("mode=warn", text)
        self.assertIn("outcome=deny", text)
        self.assertIn("reason=framing", text)
        self.assertNotIn("would-deny", text)
        self.assertNotIn("until", text)  # 不記 body／標頭值
        self.assertEqual(self.fake.calls,
                         [("register_cooldown", "claude-personal", {})])

    def test_would_deny_then_serve_double_slash(self):
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, _, _ = self.call(
                "GET", "//v1/resolve?agent=opencode&provider=opencode-zen")
        self.assertEqual(status, 200)
        text = "\n".join(logs.output)
        self.assertIn("mode=warn", text)
        self.assertIn("outcome=deny", text)
        self.assertNotIn("would-deny", text)

    def test_transfer_encoding_still_served(self):
        status, _, _ = self.call("GET", "/v1/subscriptions",
                                 [("Transfer-Encoding", "chunked")])
        self.assertEqual(status, 200)


class HeaderNameTokenTests(unittest.TestCase):
    """agy 觀察 1：標頭名稱須符合 RFC 7230 token（含冒號前空白）。

    stdlib 的 email.parser 會把 ``Transfer-Encoding : chunked`` 解析成名為
    ``"Transfer-Encoding "``（尾端空白）的標頭，因此 ``get_all`` 看不到它；framing
    以名稱檢查擋下，不倚賴個別標頭的形狀。off 不經 framing（C-108 的語料另有一筆
    鎖住 off 的行為）。
    """

    RAW = b"GET /v1/subscriptions HTTP/1.0\r\nTransfer-Encoding : chunked\r\n\r\n"

    def test_enforce_rejects_space_before_colon(self):
        fake = FakeService()
        server, thread, port = start(fake, FakeGate("enforce"))
        try:
            resp = exchange(port, self.RAW)
        finally:
            stop(server, thread)
        status, body, headers = parse_response(resp)
        self.assertEqual((status, body), (401, UNAUTHORIZED))
        self.assertNotIn("www-authenticate", headers)
        self.assertEqual(fake.calls, [])

    def test_warn_denies_then_serves(self):
        fake = FakeService()
        server, thread, port = start(fake, FakeGate("warn"))
        try:
            with self.assertLogs("lgwd.http", level="INFO") as logs:
                resp = exchange(port, self.RAW)
        finally:
            stop(server, thread)
        self.assertEqual(parse_response(resp)[0], 200)
        self.assertEqual(fake.calls, [("list_subscriptions",)])
        text = "\n".join(logs.output)
        self.assertIn("mode=warn", text)
        self.assertIn("outcome=deny", text)
        self.assertIn("reason=framing", text)


class AuthLogPathTests(unittest.TestCase):
    """中-1-3：gated 的 log 不記 query；原文不可信時連 path 都記固定 -。"""

    def test_enforce_target_rejection_logs_no_query(self):
        fake = FakeService()
        server, thread, port = start(fake, FakeGate("enforce"))
        try:
            with self.assertLogs("lgwd.http", level="INFO") as logs:
                resp = exchange(port, req(
                    "GET", "//v1/resolve?agent=opencode&secret=TOPSECRET"))
        finally:
            stop(server, thread)
        self.assertEqual(parse_response(resp)[0], 401)
        text = "\n".join(logs.output)
        self.assertNotIn("TOPSECRET", text)
        self.assertIn("route=-", text)        # 原文無法路由：固定哨兵
        self.assertIn("GET - -> 401", text)   # access log 也不記不可信的 path
        self.assertEqual(fake.calls, [])

    def test_enforce_read_timeout_logs_no_query(self):
        fake = FakeService()
        server, thread, port = start(fake, FakeGate("enforce"))
        try:
            head = req("POST", COOLDOWN + "?secret=TOPSECRET",
                       [("Content-Length", "8")])
            with mock.patch("lgwd.http._READ_DEADLINE_SECONDS", 0.2):
                with socket.create_connection((HOST, port), timeout=5) as sock:
                    with self.assertLogs("lgwd.http", level="INFO") as logs:
                        sock.sendall(head)
                        sock.settimeout(5)
                        resp = recv_until_eof(sock)
        finally:
            stop(server, thread)
        status, body, _ = parse_response(resp)
        self.assertEqual((status, body), (503, AUTH_UNAVAILABLE))
        text = "\n".join(logs.output)
        self.assertNotIn("TOPSECRET", text)
        self.assertIn(COOLDOWN + " -> 503", text)  # 只記不含 query 的 path
        self.assertEqual(fake.calls, [])


class HealthzExemptionTests(unittest.TestCase):
    """C-101（J-1）：`/healthz` 三種模式都不套 framing、不取號誌。"""

    def test_healthz_ignores_te_and_duplicate_headers(self):
        for gate in (None, FakeGate("off"), FakeGate("warn"), FakeGate("enforce")):
            mode = "none" if gate is None else gate.mode
            with self.subTest(mode=mode):
                fake = FakeService()
                server, thread, port = start(fake, gate)
                try:
                    for name, raw in {
                        "plain": req("GET", "/healthz"),
                        "te": req("GET", "/healthz", [("Transfer-Encoding", "chunked")]),
                        "dup_xlgw": req("GET", "/healthz",
                                        [("X-LGW-Key-Id", "a"), ("X-LGW-Key-Id", "b")]),
                        "signed": req("GET", "/healthz", [
                            ("X-LGW-Auth-Version", "1"), ("X-LGW-Key-Id", "k"),
                            ("X-LGW-Timestamp", "1795392000"),
                            ("X-LGW-Nonce", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"),
                            ("X-LGW-Signature", "x" * 240)]),
                    }.items():
                        with self.subTest(case=name):
                            status, body, _ = parse_response(exchange(port, raw))
                            self.assertEqual(status, 200)
                            self.assertEqual(body, b'{"status": "ok"}')
                            self.assertEqual(len(body), 16)
                finally:
                    stop(server, thread)
                self.assertEqual(fake.calls, [])

    def test_non_healthz_business_route_still_framed(self):
        """framing 的豁免只在 /healthz；五條業務路由照樣受約束（C-101）。"""
        fake = FakeService()
        server, thread, port = start(fake, FakeGate("enforce"))
        try:
            resp = exchange(port, req("GET", "/v1/subscriptions",
                                      [("Transfer-Encoding", "chunked")]))
        finally:
            stop(server, thread)
        self.assertEqual(parse_response(resp)[0], 401)
        self.assertEqual(fake.calls, [])


class DeadlineTests(FramingCase):
    """C-105：warn／enforce 的業務請求共用一個總期限（單調時鐘截止）。"""

    gate = FakeGate("enforce")

    def _slowloris(self, port, head, body, delay):
        """送出 head，然後每 delay 秒送 1 byte；同時讀回應（回 (resp, elapsed)）。"""
        with socket.create_connection((HOST, port), timeout=5) as sock:
            sock.sendall(head)

            def drip():
                for byte in body:
                    time.sleep(delay)
                    try:
                        sock.sendall(bytes([byte]))
                    except OSError:
                        return

            sender = threading.Thread(target=drip, daemon=True)
            started = time.monotonic()
            sender.start()
            sock.settimeout(30)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, ConnectionResetError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            elapsed = time.monotonic() - started
            sender.join(timeout=10)
            return b"".join(chunks), elapsed

    def test_slowloris_cut_at_the_deadline(self):
        head = req("POST", COOLDOWN, [("Content-Length", "8")])
        with mock.patch("lgwd.http._READ_DEADLINE_SECONDS", 1.0):
            resp, elapsed = self._slowloris(self.port, head, b'{"a": 1}', 0.25)
        status, body, _ = parse_response(resp)
        self.assertEqual(status, 503)
        self.assertEqual(body, AUTH_UNAVAILABLE)
        # 逐 byte 慢送也被同一個截止時刻切斷（不是每次 recv 重新起算）
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self.fake.calls, [])


class WarnDeadlineTests(FramingCase):
    """C-105：warn 逾時記不可判定並關閉連線（body 沒讀完，無法照舊處理）。"""

    gate = FakeGate("warn")

    def test_slowloris_closes_without_serving(self):
        head = req("POST", COOLDOWN, [("Content-Length", "8")])
        with mock.patch("lgwd.http._READ_DEADLINE_SECONDS", 1.0):
            with socket.create_connection((HOST, self.port), timeout=5) as sock:
                sock.sendall(head)
                with self.assertLogs("lgwd.http", level="INFO") as logs:
                    started = time.monotonic()
                    sender = threading.Thread(
                        target=self._drip, args=(sock, b'{"a": 1}', 0.25), daemon=True)
                    sender.start()
                    sock.settimeout(30)
                    chunks = []
                    while True:
                        try:
                            chunk = sock.recv(65536)
                        except (socket.timeout, ConnectionResetError):
                            break
                        if not chunk:
                            break
                        chunks.append(chunk)
                    elapsed = time.monotonic() - started
                    sender.join(timeout=10)
        self.assertEqual(b"".join(chunks), b"")  # 關閉，不回應
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 5.0)
        text = "\n".join(logs.output)
        self.assertIn("outcome=unavailable", text)
        self.assertIn("reason=framing", text)
        self.assertEqual(self.fake.calls, [])

    @staticmethod
    def _drip(sock, body, delay):
        for byte in body:
            time.sleep(delay)
            try:
                sock.sendall(bytes([byte]))
            except OSError:
                return


class PermitTests(unittest.TestCase):
    """C-106：讀 body＋驗章階段的號誌（上限 32、等 1 秒）；`/healthz` 不取。"""

    def setUp(self):
        self.fake = FakeService()

    def _hold_all(self, server):
        held = 0
        while server.framing_semaphore.acquire(blocking=False):
            held += 1
        self.assertEqual(held, 32)
        return held

    def _release_all(self, server, held):
        for _ in range(held):
            server.framing_semaphore.release()

    def test_enforce_503_when_permits_exhausted(self):
        server, thread, port = start(self.fake, FakeGate("enforce"))
        try:
            held = self._hold_all(server)
            try:
                started = time.monotonic()
                resp = exchange(port, req("GET", "/v1/subscriptions"))
                elapsed = time.monotonic() - started
            finally:
                self._release_all(server, held)
        finally:
            stop(server, thread)
        status, body, _ = parse_response(resp)
        self.assertEqual((status, body), (503, AUTH_UNAVAILABLE))
        self.assertGreaterEqual(elapsed, 0.9)  # 等滿 1 秒才放棄
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self.fake.calls, [])

    def test_warn_continues_when_permits_exhausted(self):
        server, thread, port = start(self.fake, FakeGate("warn"))
        try:
            held = self._hold_all(server)
            try:
                with self.assertLogs("lgwd.http", level="INFO") as logs:
                    resp = exchange(port, req("GET", "/v1/subscriptions"))
            finally:
                self._release_all(server, held)
        finally:
            stop(server, thread)
        status, _, _ = parse_response(resp)
        self.assertEqual(status, 200)  # warn 不改業務結果
        self.assertEqual(self.fake.calls, [("list_subscriptions",)])
        text = "\n".join(logs.output)
        self.assertIn("outcome=unavailable", text)  # 記不可判定（C-106）
        self.assertIn("reason=busy", text)

    def test_healthz_needs_no_permit(self):
        server, thread, port = start(self.fake, FakeGate("enforce"))
        try:
            held = self._hold_all(server)
            try:
                status, body, _ = parse_response(exchange(port, req("GET", "/healthz")))
            finally:
                self._release_all(server, held)
        finally:
            stop(server, thread)
        self.assertEqual((status, len(body)), (200, 16))
        self.assertEqual(self.fake.calls, [])


class GateModeTests(unittest.TestCase):
    """gate 只讀 .mode；不認得的值在建伺服器時就失敗（C-606 的 fail-fast 前置）。"""

    def test_unknown_mode_is_rejected(self):
        for mode in ("bogus", "", None, 1):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    make_api_server(FakeService(), HOST, 0, gate=FakeGate(mode))


if __name__ == "__main__":
    unittest.main()
