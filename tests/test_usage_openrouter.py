"""lgwd/usage_openrouter.py（#85）：請求形狀、逾時、429、欄位缺漏、非 JSON、canary。

HTTP 層一律是假的；不連網。
"""

import json
import logging
import socket
import unittest
from datetime import datetime, timezone

from lgwd.clock import FakeClock
from lgwd.usage_openrouter import SOURCE, URL, OpenRouterUsageFetcher

KEY = "canary-openrouter-key-0001"


def body_with(fmdr=None, **extra):
    data = {}
    if fmdr is not None:
        data["free_model_daily_requests"] = fmdr
    data.update(extra)
    return json.dumps({"data": data}).encode()


GOOD = body_with(fmdr={"used": 10, "limit": 50, "remaining": 40})


class FakeHttp:
    def __init__(self, status=200, headers=None, body=GOOD, raises=None):
        self.status, self.headers, self.body, self.raises = status, headers or {}, body, raises
        self.requests = []

    def __call__(self, url, headers, timeout):
        self.requests.append((url, dict(headers), timeout))
        if self.raises:
            raise self.raises
        return self.status, self.headers, self.body


def run(http, key=KEY):
    return OpenRouterUsageFetcher(http_get=http).fetch(key)


class OpenRouterFetchTest(unittest.TestCase):
    def test_請求形狀(self):
        http = FakeHttp()
        run(http)
        (url, headers, timeout), = http.requests
        self.assertEqual(url, "https://openrouter.ai/api/v1/key")
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(timeout, 5.0)

    def test_成功(self):
        got = run(FakeHttp())
        self.assertTrue(got.ok)
        self.assertEqual(got.windows["session"][0], 80.0)
        self.assertIsNone(got.windows["weekly"])
        self.assertEqual(SOURCE, "openrouter-api-key")

    def test_非200_upstream_error(self):
        for status in (401, 403, 500, 503):
            with self.subTest(status):
                self.assertEqual(run(FakeHttp(status=status)).reason, "upstream_error")

    def test_轉址不跟隨_upstream_error(self):
        http = FakeHttp(status=302, headers={"location": "https://evil.example/x"})
        self.assertEqual(run(http).reason, "upstream_error")
        self.assertEqual(len(http.requests), 1)

    def test_連線失敗_upstream_error(self):
        self.assertEqual(run(FakeHttp(raises=ConnectionRefusedError(f"boom {KEY}"))).reason, "upstream_error")

    def test_逾時(self):
        self.assertEqual(run(FakeHttp(raises=socket.timeout(f"slow {KEY}"))).reason, "timeout")
        self.assertEqual(run(FakeHttp(raises=TimeoutError())).reason, "timeout")

    def test_429帶RetryAfter(self):
        got = run(FakeHttp(status=429, headers={"retry-after": "600"}))
        self.assertEqual((got.reason, got.retry_after), ("rate_limited", 600.0))

    def test_429的RetryAfter不是秒數_當沒有(self):
        got = run(FakeHttp(status=429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        self.assertEqual((got.reason, got.retry_after), ("rate_limited", None))

    def test_缺欄位_no_windows(self):
        # 沒有 free_model_daily_requests：沒有窗口
        self.assertEqual(run(FakeHttp(body=body_with())).reason, "no_windows")

    def test_欄位缺漏_parse_error(self):
        # missing remaining
        b = body_with(fmdr={"used": 10, "limit": 50})
        self.assertEqual(run(FakeHttp(body=b)).reason, "parse_error")
        # remaining not number
        b = body_with(fmdr={"used": 10, "limit": 50, "remaining": "40"})
        self.assertEqual(run(FakeHttp(body=b)).reason, "parse_error")
        # limit 0
        b = body_with(fmdr={"used": 10, "limit": 0, "remaining": 0})
        self.assertEqual(run(FakeHttp(body=b)).reason, "parse_error")
        # fmdr not dict
        b = body_with(fmdr=1)
        self.assertEqual(run(FakeHttp(body=b)).reason, "parse_error")

    def test_非JSON(self):
        for body in (b"not json", b"[]", b"{}", b'{"data": []}'):
            with self.subTest(body=body):
                self.assertEqual(run(FakeHttp(body=body)).reason, "parse_error")

    def test_失敗結果與log不含key或上游內容(self):
        secret = "canary-secret-body"
        http = FakeHttp(status=500, body=f"error {secret} {KEY}".encode())
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("lgwd.usage").info("probe")
            got = run(http)
        text = repr(got) + "\n".join(logs.output)
        self.assertNotIn(secret, text)
        self.assertNotIn(KEY, text)

    def test_每次用傳入的新key(self):
        http = FakeHttp()
        fetcher = OpenRouterUsageFetcher(http_get=http)
        fetcher.fetch("canary-old"); fetcher.fetch("canary-new")
        self.assertEqual([r[1]["Authorization"] for r in http.requests],
                         ["Bearer canary-old", "Bearer canary-new"])

    def test_預設http層只連寫死的位址(self):
        import tests
        with self.assertRaises(ValueError):
            tests.REAL_DEFAULT_OPENROUTER_HTTP_GET("https://other.example/api/v1/key", {}, 1)

    def test_測試套件的安全網_預設http層不連線(self):
        got = OpenRouterUsageFetcher().fetch(KEY)
        self.assertEqual(got.reason, "upstream_error")

    def test_resets_at_下一個UTC午夜_aware(self):
        fc = FakeClock(start=datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc))
        http = FakeHttp()
        got = OpenRouterUsageFetcher(http_get=http, clock=fc).fetch(KEY)
        self.assertTrue(got.ok)
        resets = got.windows["session"][1]
        self.assertEqual(resets, datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(resets.tzinfo)

class OpenRouterProviderHookTest(unittest.TestCase):
    def test_make_usage_fetcher_回傳_OpenRouterUsageFetcher(self):
        from lgwd import providers
        from lgwd.providers import UsageDeps
        fetcher = providers.OpenRouterProvider().make_usage_fetcher(UsageDeps())
        self.assertIsInstance(fetcher, OpenRouterUsageFetcher)

    def test_常數值(self):
        self.assertTrue(OpenRouterUsageFetcher.default_enabled)
        self.assertEqual(OpenRouterUsageFetcher.default_ttl, 120)
        self.assertEqual(OpenRouterUsageFetcher.min_ttl, 60)


if __name__ == "__main__":
    unittest.main()
