"""usage-overview（#204 PR2）：GET /v1/usage 的服務層與 HTTP 端到端（C-5～C-10）。

用真的 RegistryService 與 make_api_server，取用量一律用 FakeFetcher（不連網、不起子行程）。
Client.usage() 的 C-11 在 tests/test_client.py。
"""

import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from lgwd.clock import FakeClock
from lgwd.http import make_api_server
from lgwd.keys import KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService
from lgwd.usage import FetchResult, UsageService, failure
from tests.usage_fakes import BASE, FakeFetcher, at

HOST = "127.0.0.1"
POLICY = str(Path(__file__).resolve().parent.parent / "policy" / "default.json")
CANARY = "canary-usage-ov-ollama-0001"
ROW_KEYS = {"id", "kind", "status", "source", "fetched_at", "stale",
            "reason", "note", "session", "weekly"}
USAGE_KEYS = ROW_KEYS - {"id", "kind"}


class Harness(unittest.TestCase):
    """真 RegistryService＋UsageService（只注入 ollama-cloud 的假 fetcher；openrouter 可選）。"""

    def build(self, *, deployed=True, enabled=True, openrouter=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        lines = ["OPENROUTER_API_KEY=canary-usage-ov-or\n"]
        if deployed:
            lines.append(f"OLLAMA_API_KEY={CANARY}\n")
        keys_path.write_text("".join(lines), encoding="utf-8")
        self.clock = FakeClock(BASE)
        self.keys = KeyStore(str(keys_path))
        self.fetcher = FakeFetcher(base=BASE, enabled=enabled)
        fetchers = {"ollama-cloud": self.fetcher}
        if openrouter is not None:
            fetchers["openrouter"] = openrouter
        self.usage = UsageService(self.clock, fetchers=fetchers,
                                  key_of=self.keys.get, deployed=self.keys.deployed)
        self.service = RegistryService(str(root / "llm.db"), load_policy(POLICY),
                                       self.keys, self.clock, usage=self.usage)
        self.addCleanup(self.service.close)
        self.addCleanup(self.usage.stop)
        return self.service

    def row(self, summary, sub_id):
        return {item["id"]: item for item in summary["subscriptions"]}[sub_id]


class HttpHarness(Harness):
    def serve(self):
        server = make_api_server(self.service, HOST, 0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        host, port = server.server_address[:2]
        self.base = "http://{0}:{1}".format(host, port)

    def get(self, path, method="GET", data=None):
        """回 (狀態碼, 原始 body bytes)；HTTPError 也照讀。"""
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class ShapeTest(Harness):
    def test_C5_恰十鍵_schema為1_不含pipes(self):
        service = self.build()
        summary = service.list_usage_summary()
        self.assertEqual(set(summary), {"schema", "subscriptions"})
        self.assertEqual(summary["schema"], 1)
        for item in summary["subscriptions"]:
            with self.subTest(id=item["id"]):
                self.assertEqual(set(item), ROW_KEYS)
                self.assertNotIn("pipes", item)

    def test_C5_順序依priority(self):
        service = self.build()
        ids = [item["id"] for item in service.list_usage_summary()["subscriptions"]]
        expected = [sub.name for sub in load_policy(POLICY).in_priority_order()]
        self.assertEqual(ids, expected)


class ParityTest(Harness):
    def test_C6_與list同一筆逐值相等(self):
        service = self.build()
        service.list_subscriptions()          # 觸發取用量
        self.assertTrue(self.usage.wait_idle())
        listed = {item["id"]: item for item in service.list_subscriptions()["subscriptions"]}
        self.assertTrue(self.usage.wait_idle())
        summary = service.list_usage_summary()
        self.assertEqual(self.row(summary, "ollama-cloud")["status"], "ok")  # 確認有真的資料
        for item in summary["subscriptions"]:
            with self.subTest(id=item["id"]):
                usage = listed[item["id"]]["usage"]
                for key in USAGE_KEYS:
                    self.assertEqual(item[key], usage[key], key)



class CanaryTest(Harness):
    def test_C7_canary掃描無命中(self):
        service = self.build()
        # 成功、各種失敗與例外都跑過一輪，每一輪都掃描一次回覆。
        outcomes = [
            None,                                    # 成功（FakeFetcher 預設）
            failure("upstream_error"),
            failure("timeout"),
            failure("rate_limited", retry_after=60),
            RuntimeError(f"leak {CANARY}"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__ if outcome else "ok"):
                self.clock.advance(hours=7)      # 超過 TTL、退避與 6 小時舊值上限
                if outcome is not None:
                    self.fetcher.outcome = outcome
                service.list_usage_summary()     # 觸發
                self.assertTrue(self.usage.wait_idle())
                body = json.dumps(service.list_usage_summary(), ensure_ascii=False)
                self.assertNotIn(CANARY, body)
                self.assertNotIn("canary", body)
                self.assertNotIn("pipes", body)
                self.assertNotIn("credits", body)
                self.assertNotIn("$", body)
                self.assertNotIn("USD", body)
                self.assertIsNone(re.search(r"\d\s*(USD|usd|元|美元)", body))
                self.assertIsNone(re.search(r"Bearer|sk-", body))

    def test_C7_有pipes與credits時一覽仍不帶(self):
        # 只注入 ollama 時 pipes 恆空，掃描空轉；這裡讓 openrouter 的 pipes 非空且含 credits。
        openrouter = FakeFetcher(base=BASE)
        openrouter.source = "openrouter-api-key"
        openrouter.outcome = FetchResult(windows={
            "session": (75.0, at(days=1)),
            "weekly": None,
            "credits": (60.0, at(days=3)),
        })
        service = self.build(openrouter=openrouter)
        service.list_subscriptions()          # 觸發取用量
        self.assertTrue(self.usage.wait_idle())
        listed = {item["id"]: item for item in service.list_subscriptions()["subscriptions"]}
        self.assertTrue(self.usage.wait_idle())
        listed_usage = listed["openrouter"]["usage"]
        self.assertEqual(listed_usage["status"], "ok")  # 確認有真的資料
        self.assertIn("credits", [pipe["id"] for pipe in listed_usage["pipes"]])
        summary = service.list_usage_summary()
        row = self.row(summary, "openrouter")
        self.assertNotIn("pipes", row)
        self.assertEqual(row["session"], listed_usage["session"])
        body = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("pipes", body)
        self.assertNotIn("credits", body)


class FailureIsolationTest(Harness):
    def test_C9_卡住的取用量不拖慢一覽(self):
        service = self.build()
        self.fetcher.gate = threading.Event()
        self.addCleanup(self.fetcher.gate.set)   # 測試結束前放開，讓執行緒收乾淨
        started = time.monotonic()
        summary = service.list_usage_summary()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(self.fetcher.entered.wait(2))  # 確實觸發了一次
        row = self.row(summary, "ollama-cloud")
        self.assertEqual((row["status"], row["reason"]), ("unknown", "not_fetched_yet"))
        self.fetcher.gate.set()
        self.assertTrue(self.usage.wait_idle())

    def test_C9_單筆組裝拋例外只影響該筆(self):
        service = self.build()
        original = self.usage.usage_for

        def flaky(subscription):
            if subscription == "ollama-cloud":
                raise RuntimeError("boom")
            return original(subscription)

        with mock.patch.object(self.usage, "usage_for", side_effect=flaky):
            summary = service.list_usage_summary()
        row = self.row(summary, "ollama-cloud")
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["reason"], "internal")
        self.assertIsNone(row["note"])
        self.assertEqual(len(summary["subscriptions"]), len(load_policy(POLICY).subscriptions))
        others = [item for item in summary["subscriptions"] if item["id"] != "ollama-cloud"]
        self.assertTrue(others)
        for item in others:
            with self.subTest(id=item["id"]):
                self.assertNotEqual(item["reason"], "internal")


class TriggerTest(Harness):
    """C-10：觸發規則與 list 相同（只有 GET /v1/usage 這條路徑在測）。"""

    def test_C10_過期且啟用且已部署_觸發一次(self):
        service = self.build()
        row = self.row(service.list_usage_summary(), "ollama-cloud")
        self.assertEqual(row["reason"], "not_fetched_yet")
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 1)
        self.assertEqual(self.row(service.list_usage_summary(), "ollama-cloud")["status"], "ok")

    def test_C10_快取新鮮_不觸發(self):
        service = self.build()
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.clock.advance(seconds=30)           # 小於 TTL（120 秒）
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 1)

    def test_C10_過期後再觸發(self):
        service = self.build()
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.clock.advance(seconds=121)          # 超過 TTL（120 秒）
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 2)

    def test_C10_key沒部署_不觸發(self):
        service = self.build(deployed=False)
        row = self.row(service.list_usage_summary(), "ollama-cloud")
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 0)
        self.assertEqual((row["status"], row["reason"]), ("unknown", "no_key"))

    def test_C10_enabled為false_不觸發(self):
        service = self.build(enabled=False)
        row = self.row(service.list_usage_summary(), "ollama-cloud")
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 0)
        self.assertEqual(row["reason"], "disabled")

    def test_C10_單飛_併發十次只觸發一次(self):
        service = self.build()
        self.fetcher.gate = threading.Event()
        self.addCleanup(self.fetcher.gate.set)
        threads = [threading.Thread(target=service.list_usage_summary) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(self.fetcher.entered.wait(2))
        self.fetcher.gate.set()
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 1)


class HttpRouteTest(HttpHarness):
    def test_C6_位元組數小於list(self):
        service = self.build()
        self.serve()
        service.list_subscriptions()
        self.assertTrue(self.usage.wait_idle())
        status_list, body_list = self.get("/v1/subscriptions")
        status_usage, body_usage = self.get("/v1/usage")
        self.assertEqual((status_list, status_usage), (200, 200))
        self.assertLess(len(body_usage), len(body_list))

    def test_C5_HTTP_GET回200與一覽(self):
        service = self.build()
        self.serve()
        status, body = self.get("/v1/usage")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["schema"], 1)
        self.assertEqual([item["id"] for item in data["subscriptions"]],
                         [item["id"] for item in service.list_usage_summary()["subscriptions"]])
        for item in data["subscriptions"]:
            self.assertEqual(set(item), ROW_KEYS)

    def test_C8_非GET與其他路徑404_假fetcher零呼叫(self):
        self.build()
        self.serve()
        cases = [
            ("POST", "/v1/usage", b"{}"),
            ("PUT", "/v1/usage", b"{}"),
            ("DELETE", "/v1/usage", None),
            ("PATCH", "/v1/usage", b"{}"),
            ("GET", "/v1/usage/", None),
            ("GET", "/v1/usage/ollama-cloud", None),
        ]
        for method, path, data in cases:
            with self.subTest(method=method, path=path):
                status, _ = self.get(path, method=method, data=data)
                self.assertEqual(status, 404)
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.fetcher.calls, 0)
        self.assertFalse(self.fetcher.entered.is_set())


if __name__ == "__main__":
    unittest.main()
