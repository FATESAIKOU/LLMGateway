"""lgwd/usage_ollama.py（#60 tasks 4.1）：請求形狀、轉址不跟隨、逾時、429、新方案、白名單、canary。

HTTP 層一律是假的；不連網。
"""

import json
import logging
import socket
import unittest

from lgwd.usage_ollama import SOURCE, URL, OllamaUsageFetcher
from lgwd import usage_ollama

KEY = "canary-ollama-key-0001"


def balance(**included):
    return json.dumps({"included": included}).encode()


GOOD = balance(session={"remaining_percent": 75, "resets_at": "2026-10-25T07:00:00Z", "extra": 1},
               weekly={"remaining_percent": 40.5, "resets_at": "2026-10-28T00:00:00Z"})


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
    return OllamaUsageFetcher(http_get=http).fetch(key)


class OllamaFetchTest(unittest.TestCase):
    def test_請求形狀(self):
        http = FakeHttp()
        run(http)
        (url, headers, timeout), = http.requests
        self.assertEqual(url, "https://ollama.com/api/balance")
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(timeout, 5.0)

    def test_成功只取白名單欄位(self):
        got = run(FakeHttp())
        self.assertTrue(got.ok)
        self.assertEqual(got.windows["session"][0], 75)
        self.assertEqual(got.windows["weekly"][0], 40.5)
        self.assertEqual(sorted(got.windows), ["session", "weekly"])
        self.assertEqual(got.windows["session"][1].isoformat(), "2026-10-25T07:00:00+00:00")
        self.assertEqual(SOURCE, "ollama-api-balance")

    def test_只有一個窗口(self):
        got = run(FakeHttp(body=balance(weekly={"remaining_percent": 10, "resets_at": "2026-10-28T00:00:00Z"})))
        self.assertTrue(got.ok)
        self.assertIsNone(got.windows["session"])

    def test_新方案_no_windows(self):
        body = balance(balance_usd=12.3, allowance_usd=20, period="monthly")
        got = run(FakeHttp(body=body))
        self.assertEqual((got.ok, got.reason), (False, "no_windows"))

    def test_轉址不跟隨_upstream_error(self):
        http = FakeHttp(status=302, headers={"location": "https://evil.example/x"})
        got = run(http)
        self.assertEqual(got.reason, "upstream_error")
        self.assertEqual(len(http.requests), 1)

    def test_非200_upstream_error(self):
        for status in (401, 403, 500, 503):
            with self.subTest(status):
                self.assertEqual(run(FakeHttp(status=status)).reason, "upstream_error")

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
        self.assertIsNone(run(FakeHttp(status=429)).retry_after)

    def test_parse_error(self):
        bad = [b"not json", b"[]", b"{}", balance(session=1),
               balance(session={"remaining_percent": "75", "resets_at": "2026-10-25T07:00:00Z"}),
               balance(session={"remaining_percent": 75, "resets_at": "tomorrow"}),
               balance(session={"remaining_percent": 175, "resets_at": "2026-10-25T07:00:00Z"}),
               balance(session={"remaining_percent": True, "resets_at": "2026-10-25T07:00:00Z"}),
               json.dumps({"included": []}).encode()]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(run(FakeHttp(body=body)).reason, "parse_error")

    def test_失敗結果與log不含key或上游內容(self):
        secret = "canary-secret-body"
        http = FakeHttp(status=500, body=f"error {secret} {KEY}".encode())
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("lgwd.usage").info("probe")   # 確保 assertLogs 有東西可收
            got = run(http)
        text = repr(got) + "\n".join(logs.output)
        self.assertNotIn(secret, text)
        self.assertNotIn(KEY, text)

    def test_每次用傳入的新key(self):
        http = FakeHttp()
        fetcher = OllamaUsageFetcher(http_get=http)
        fetcher.fetch("canary-old"); fetcher.fetch("canary-new")
        self.assertEqual([r[1]["Authorization"] for r in http.requests],
                         ["Bearer canary-old", "Bearer canary-new"])

    def test_預設http層只連寫死的位址(self):
        import tests
        with self.assertRaises(ValueError):
            tests.REAL_DEFAULT_HTTP_GET("https://other.example/api/balance", {}, 1)

    def test_測試套件的安全網_預設http層不連線(self):
        got = OllamaUsageFetcher().fetch(KEY)        # 沒注入 http_get：用被換掉的預設
        self.assertEqual(got.reason, "upstream_error")


if __name__ == "__main__":
    unittest.main()
