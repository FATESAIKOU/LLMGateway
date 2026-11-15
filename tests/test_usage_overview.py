"""usage-overview（#204 PR2）：GET /v1/usage 的服務層與 HTTP 端到端（C-5～C-10）。

用真的 RegistryService 與 make_api_server，取用量一律用 FakeFetcher（不連網、不起子行程）。
Client.usage() 的 C-11 在 tests/test_client.py。
"""

import datetime as _dt
import json
import logging
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from lgwd.clock import FakeClock, to_rfc3339
from lgwd.http import make_api_server
from lgwd.keys import KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService
import lgwd.usage as lgwd_usage
from lgwd.usage import FetchResult, UsageService, failure
from lgwd.usage_openrouter import OpenRouterUsageFetcher
from tests.usage_fakes import BASE, FakeFetcher, at, windows

HOST = "127.0.0.1"
POLICY = str(Path(__file__).resolve().parent.parent / "policy" / "default.json")
CANARY = "canary-usage-ov-ollama-0001"
OR_CANARY = "canary-usage-ov-or"
UTC = _dt.timezone.utc
# 一覽的 usage 八鍵（list 的 usage 另有 pipes）；另外兩鍵是 OpenRouter 金額（design D1）。
USAGE_KEYS = {"status", "source", "fetched_at", "stale", "reason", "note", "session", "weekly"}
USD_KEYS = {"weekly_used_usd", "weekly_used_usd_fetched_at"}
ROW_KEYS = {"id", "kind"} | USAGE_KEYS | USD_KEYS


class Harness(unittest.TestCase):
    """真 RegistryService＋UsageService（只注入 ollama-cloud 的假 fetcher；openrouter 可選）。"""

    def build(self, *, deployed=True, enabled=True, openrouter=None, openrouter_http=None,
              start=BASE):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        lines = ["OPENROUTER_API_KEY=canary-usage-ov-or\n"]
        if deployed:
            lines.append(f"OLLAMA_API_KEY={CANARY}\n")
        keys_path.write_text("".join(lines), encoding="utf-8")
        self.clock = FakeClock(start)
        self.keys = KeyStore(str(keys_path))
        self.fetcher = FakeFetcher(base=BASE, enabled=enabled)
        fetchers = {"ollama-cloud": self.fetcher}
        if openrouter is not None:
            fetchers["openrouter"] = openrouter
        if openrouter_http is not None:          # 真的 OpenRouter fetcher，同一把 FakeClock
            fetchers["openrouter"] = OpenRouterUsageFetcher(http_get=openrouter_http, clock=self.clock)
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
    def test_C5_恰十二鍵_schema為1_不含pipes(self):
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

        def flaky(subscription, now=None):
            if subscription == "ollama-cloud":
                raise RuntimeError("boom")
            return original(subscription, now)

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


class ScriptedHttp:
    """假的 OpenRouter HTTP 層：回固定 status／body；raises 設了就丟例外；gate 設了就卡到放行。"""

    def __init__(self, body, status=200):
        self.body, self.status, self.raises, self.gate = body, status, None, None
        self.calls = 0

    def __call__(self, url, headers, timeout):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        if self.raises is not None:
            raise self.raises
        return self.status, {}, self.body


FMDR_OK = '"free_model_daily_requests": {"used": 10, "limit": 50, "remaining": 40}'


def raw_body(usd=None, fmdr=FMDR_OK, extra=""):
    """usd 是 JSON 字面值（None＝欄位不給）；字面值原樣放進 body，NaN／1e400 等也照寫。"""
    parts = [p for p in (fmdr, None if usd is None else f'"usage_weekly": {usd}', extra) if p]
    return ("{\"data\": {" + ", ".join(parts) + "}}").encode()


class WeeklyUsdHarness(Harness):
    def settle(self, service):
        """一次讀取（觸發背景取用量）→ 等它做完 → 再讀一次回傳一覽。"""
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        return service.list_usage_summary()

    def hold(self, http):
        """之後的取用量卡住（不完成），讓金額與時間只靠讀取時的判斷。"""
        http.gate = threading.Event()
        self.addCleanup(http.gate.set)


class WeeklyUsdValueTest(WeeklyUsdHarness):
    def test_C3_第一次讀取回null_下次讀取才有值(self):
        http = ScriptedHttp(raw_body("7.3191"))
        service = self.build(openrouter_http=http)
        self.hold(http)                              # 第一次讀取時取用量尚未完成（不靠時序）
        first = self.row(service.list_usage_summary(), "openrouter")
        self.assertEqual((first["weekly_used_usd"], first["weekly_used_usd_fetched_at"]), (None, None))
        http.gate.set()
        self.assertTrue(self.usage.wait_idle())
        second = self.row(service.list_usage_summary(), "openrouter")
        self.assertEqual(second["weekly_used_usd"], 7.3191)
        self.assertEqual(second["weekly_used_usd_fetched_at"], "2026-10-25T03:00:00Z")

    def test_有值與零_零不是null(self):
        # delta「有值與零」：1.25 與 0 各自回值與取得時刻
        for literal, expected in (("1.25", 1.25), ("0", 0), ("0.0", 0.0)):
            with self.subTest(literal=literal):
                service = self.build(openrouter_http=ScriptedHttp(raw_body(literal)))
                row = self.row(self.settle(service), "openrouter")
                self.assertIsNotNone(row["weekly_used_usd"])
                self.assertEqual(row["weekly_used_usd"], expected)
                self.assertEqual(row["weekly_used_usd_fetched_at"], "2026-10-25T03:00:00Z")

    def test_C4_無效欄位金額與時間皆null_窗口不受影響(self):
        # delta「無效欄位」；1e400 解析成 inf、超大整數也要被擋，且不能拋錯中斷整份解析
        cases = ["null", '"1.25"', "true", "false", "-1", "NaN", "Infinity", "-Infinity",
                 "1e400", "-1e400", "1" + "0" * 400, "[1]", "{}"]
        for literal in cases:
            with self.subTest(literal=literal):
                service = self.build(openrouter_http=ScriptedHttp(raw_body(literal)))
                row = self.row(self.settle(service), "openrouter")
                self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))
                self.assertEqual(row["status"], "ok")          # 有效窗口照常
                self.assertIsNotNone(row["session"])

    def test_C4_缺欄位金額null(self):
        service = self.build(openrouter_http=ScriptedHttp(raw_body(None)))
        row = self.row(self.settle(service), "openrouter")
        self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))
        self.assertEqual(row["status"], "ok")

    def test_C5_窗口與金額各自成功失敗(self):
        # 窗口壞、金額好：金額保留且帶自己的時間；窗口缺、金額好：同；金額壞、窗口好：金額 null
        cases = [
            (raw_body("7.3191", fmdr='"free_model_daily_requests": 1'), "unknown", "parse_error", 7.3191),
            (raw_body("7.3191", fmdr=None), "unknown", "no_windows", 7.3191),
            (raw_body("NaN"), "ok", None, None),
        ]
        for body, status, reason, amount in cases:
            with self.subTest(body=body):
                service = self.build(openrouter_http=ScriptedHttp(body))
                row = self.row(self.settle(service), "openrouter")
                self.assertEqual((row["status"], row["reason"]), (status, reason))
                self.assertEqual(row["weekly_used_usd"], amount)
                self.assertEqual(row["weekly_used_usd_fetched_at"],
                                 None if amount is None else "2026-10-25T03:00:00Z")

    def test_C5_窗口失敗的退避照舊_金額仍寫入(self):
        # 窗口 parse_error 仍走退避：60 秒內不再觸發；金額照寫（同一次請求的結果）
        http = ScriptedHttp(raw_body("7.3191", fmdr=None))
        service = self.build(openrouter_http=http)
        self.settle(service)
        self.assertEqual(http.calls, 1)
        self.assertEqual(self.row(service.list_usage_summary(), "openrouter")["weekly_used_usd"], 7.3191)
        self.clock.advance(seconds=60)
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(http.calls, 1)

    def test_C5_上游失敗與壞資料清空金額(self):
        failing = {
            "非200": lambda h: setattr(h, "status", 500),
            "429": lambda h: setattr(h, "status", 429),
            "逾時": lambda h: setattr(h, "raises", socket.timeout("slow")),
            "內部例外": lambda h: setattr(h, "raises", RuntimeError("boom")),
            "壞JSON": lambda h: setattr(h, "body", b"not json"),
            "data非物件": lambda h: setattr(h, "body", b'{"data": []}'),
        }
        for name, break_it in failing.items():
            with self.subTest(name):
                http = ScriptedHttp(raw_body("7.3191"))
                service = self.build(openrouter_http=http)
                self.settle(service)
                self.assertEqual(self.row(service.list_usage_summary(), "openrouter")["weekly_used_usd"], 7.3191)
                self.clock.advance(seconds=121)            # 超過 TTL；退避只從失敗起算
                break_it(http)
                row = self.row(self.settle(service), "openrouter")
                self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))

    def test_C5_先有舊金額_後續成功回應缺或無效欄位時金額與時間清空(self):
        # 回歸（契約核對缺口）：不是失敗，而是 200 成功但 usage_weekly 缺或無效；舊金額不得殘留
        for literal in ("None", "-1", "NaN", '"7"', "null"):
            with self.subTest(later=literal):
                http = ScriptedHttp(raw_body("7.3191"))
                service = self.build(openrouter_http=http)
                self.settle(service)
                self.assertEqual(self.row(service.list_usage_summary(), "openrouter")["weekly_used_usd"], 7.3191)
                self.clock.advance(seconds=121)            # 超過 TTL；成功回應不走退避
                http.body = raw_body(None if literal == "None" else literal)
                row = self.row(self.settle(service), "openrouter")
                self.assertEqual(row["status"], "ok")      # 窗口照常成功
                self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))

    def test_C5_超過六小時清空_恰六小時仍有效(self):
        http = ScriptedHttp(raw_body("7.3191"))
        service = self.build(openrouter_http=http)
        self.settle(service)
        self.hold(http)                              # 之後不再成功取得，只看讀取時的判斷
        self.clock.advance(hours=6)
        self.assertEqual(self.row(service.list_usage_summary(), "openrouter")["weekly_used_usd"], 7.3191)
        self.clock.advance(seconds=1)
        row = self.row(service.list_usage_summary(), "openrouter")
        self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))

    def test_C5_跨UTC週清空_同週仍有效(self):
        # 2026-10-25 是週日；23:00 取得，00:30 已是週一（距取得 1.5 小時，未超過六小時）
        http = ScriptedHttp(raw_body("7.3191"))
        service = self.build(openrouter_http=http, start=_dt.datetime(2026, 10, 25, 23, 0, tzinfo=UTC))
        self.settle(service)
        self.hold(http)
        self.clock.advance(minutes=59)               # 週日 23:59，同一週
        self.assertEqual(self.row(service.list_usage_summary(), "openrouter")["weekly_used_usd"], 7.3191)
        self.clock.advance(minutes=31)               # 週一 00:30，新的 UTC 週
        row = self.row(service.list_usage_summary(), "openrouter")
        self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))

    def test_C5_停用與未部署_清空投影(self):
        http = ScriptedHttp(raw_body("7.3191"))
        service = self.build(openrouter_http=http)
        self.settle(service)
        self.hold(http)
        self.usage._enabled["openrouter"] = False
        row = self.row(service.list_usage_summary(), "openrouter")
        self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))
        self.usage._enabled["openrouter"] = True
        self.usage._deployed = lambda subscription: False
        row = self.row(service.list_usage_summary(), "openrouter")
        self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))

    def test_C5_單筆金額組裝例外只影響該筆(self):
        service = self.build(openrouter_http=ScriptedHttp(raw_body("7.3191")))
        self.settle(service)
        with mock.patch.object(self.usage, "usd_fields", side_effect=RuntimeError("boom")):
            summary = service.list_usage_summary()
        row = self.row(summary, "openrouter")
        self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))
        self.assertEqual(row["status"], "ok")               # 窗口不受影響
        self.assertEqual(len(summary["subscriptions"]), len(load_policy(POLICY).subscriptions))

    def test_C5_OpenRouter組裝internal時金額也投影null(self):
        # 中-1：已有金額，但 OpenRouter 本身的 usage 組裝失敗（internal），一覽金額兩鍵必須 null
        service = self.build(openrouter_http=ScriptedHttp(raw_body("7.3191")))
        before = self.row(self.settle(service), "openrouter")
        self.assertEqual(before["weekly_used_usd"], 7.3191)       # 先確認金額確實在手上
        real_usage_for = self.usage.usage_for
        real_build = lgwd_usage.build_usage

        def boom_for_openrouter(subscription, now=None):
            if subscription == "openrouter":
                raise RuntimeError("boom")
            return real_usage_for(subscription, now)

        def boom_build_for_openrouter(source, entry, now, ttl):
            if source == "openrouter-api-key":
                raise RuntimeError("boom")
            return real_build(source, entry, now, ttl)

        injections = {
            "usage_for 拋例外（registry 的 internal_usage 路徑）":
                mock.patch.object(self.usage, "usage_for", side_effect=boom_for_openrouter),
            "UsageService 內部捕捉（_unknown internal）":
                mock.patch.object(lgwd_usage, "build_usage", side_effect=boom_build_for_openrouter),
        }
        for name, patch in injections.items():
            with self.subTest(name), patch:
                summary = service.list_usage_summary()
                row = self.row(summary, "openrouter")
                self.assertEqual((row["status"], row["reason"]), ("unknown", "internal"))
                self.assertEqual((row["weekly_used_usd"], row["weekly_used_usd_fetched_at"]), (None, None))
                self.assertEqual(len(summary["subscriptions"]), len(load_policy(POLICY).subscriptions))

    def test_C5_一覽同一個now算窗口與金額(self):
        service = self.build(openrouter_http=ScriptedHttp(raw_body("7.3191")))
        self.settle(service)
        with mock.patch.object(self.usage, "usage_for", wraps=self.usage.usage_for) as usage_for, \
                mock.patch.object(self.usage, "usd_fields", wraps=self.usage.usd_fields) as usd:
            service.list_usage_summary()
        nows = {call.args[1] for call in usage_for.call_args_list + usd.call_args_list}
        self.assertEqual(len(nows), 1)
        self.assertEqual(len(usd.call_args_list), len(load_policy(POLICY).subscriptions))


class WeeklyUsdBoundaryTest(WeeklyUsdHarness):
    def test_C6_金額canary只在一覽的weekly_used_usd出現(self):
        # D5：互異、非整數、不撞合法值；OpenRouter 的其他數字與其他訂閱的金額都不得出現在任何回覆
        or_body = ('{"data": {' + FMDR_OK + ', "usage_weekly": 7.3191, "usage_daily": 5.8127,'
                   ' "limit": 9.4403, "limit_remaining": 3.1467, "label": "%s"}}' % OR_CANARY).encode()
        service = self.build(openrouter_http=ScriptedHttp(or_body))
        self.fetcher.outcome = FetchResult(windows=windows(), weekly_usd=(5.8127, BASE))  # 非 OpenRouter 不寫入
        summary = self.settle(service)
        listed = service.list_subscriptions()
        resolved = service.resolve("opencode", "openrouter", subscription="openrouter")
        # resolve 只豁免既有 env（D4 的憑證契約）；其餘欄位照掃
        resolved_rest = {k: v for k, v in resolved.items() if k != "env"}
        bodies = {
            "usage": json.dumps(summary, ensure_ascii=False),
            "list": json.dumps(listed, ensure_ascii=False),
            "resolve": json.dumps(resolved_rest, ensure_ascii=False),
        }
        self.assertEqual(self.row(summary, "openrouter")["weekly_used_usd"], 7.3191)
        self.assertIsNone(self.row(summary, "ollama-cloud")["weekly_used_usd"])
        self.assertEqual(bodies["usage"].count("7.3191"), 1)   # 只在 weekly_used_usd
        for name, body in bodies.items():
            with self.subTest(where=name):
                for literal in ("5.8127", "9.4403", "3.1467", CANARY):
                    self.assertNotIn(literal, body)
                self.assertNotIn(OR_CANARY, body)
        for name in ("list", "resolve"):
            with self.subTest(where=name):
                self.assertNotIn("7.3191", bodies[name])
                self.assertNotIn("weekly_used_usd", bodies[name])
        # list 的 usage 不帶金額鍵；resolve 的 meta.usage 鍵集與 list 相同（八鍵＋pipes）
        for item in listed["subscriptions"]:
            self.assertFalse(USD_KEYS & set(item["usage"]), item["id"])
        self.assertEqual(set(resolved["meta"]["usage"]), USAGE_KEYS | {"pipes"})

    def test_C6_非OpenRouter的金額不寫入(self):
        self.build()
        self.fetcher.outcome = FetchResult(windows=windows(), weekly_usd=(5.8127, BASE))
        row = self.row(self.settle(self.service), "ollama-cloud")
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["weekly_used_usd"])
        self.assertIsNone(row["weekly_used_usd_fetched_at"])


class WeeklyUsdOverflowTest(WeeklyUsdHarness):
    def test_超大credits整數_合法金額與窗口照常一覽(self):
        # 回歸：超過 float 範圍的 credits 整數不得讓合法 usage_weekly 與取得時刻一起消失
        big = "1" + "0" * 400
        body = ('{"data": {' + FMDR_OK + ', "usage_weekly": 1.25,'
                ' "limit": %s, "limit_remaining": 1}}' % big).encode()
        service = self.build(openrouter_http=ScriptedHttp(body))
        row = self.row(self.settle(service), "openrouter")
        self.assertEqual(row["weekly_used_usd"], 1.25)
        self.assertEqual(row["weekly_used_usd_fetched_at"], "2026-10-25T03:00:00Z")
        self.assertEqual(row["status"], "ok")
        self.assertIsNotNone(row["session"])
        listed = self.row(service.list_subscriptions(), "openrouter")
        self.assertEqual([pipe["id"] for pipe in listed["usage"]["pipes"]], ["free_daily"])


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


# D5 金額 canary：互異、非整數、不撞合法值（0、1.25）；只有 OpenRouter 的 weekly 允許出現在一覽。
OR_KEY = "canary-key-or-0002"
OL_KEY = "canary-key-ol-0002"
AG_KEY = "canary-key-ag-0002"
TOKEN = "canary-token-or-0003"
ACCOUNT = "canary-acct-or-0004"
OR_WEEKLY, OR_DAILY, OR_LIMIT, OR_LIMIT_REMAINING = "7.3191", "5.8127", "9.4403", "3.1467"
OL_USD, AG_USD = "4.0219", "6.2841"
AMOUNT_CANARIES = (OR_WEEKLY, OR_DAILY, OR_LIMIT, OR_LIMIT_REMAINING, OL_USD, AG_USD)
CREDENTIAL_CANARIES = (OR_KEY, OL_KEY, AG_KEY, TOKEN, ACCOUNT)


def canary_literals():
    """十進位字面與 JSON 數字形式都要掃（例如 7.3191 的 json.dumps 形式）。"""
    out = set(CREDENTIAL_CANARIES)
    for literal in AMOUNT_CANARIES:
        out.add(literal)
        out.add(json.dumps(float(literal)))
    return sorted(out)


class UsdCanaryTest(HttpHarness):
    """D5：金額、key、token、帳號的 canary 掃描 list、resolve（只豁免既有 env 的 OR key）、
    GET /v1/usage、log 與磁碟上的 SQLite 檔。只有一覽的 openrouter weekly_used_usd 允許 7.3191。"""

    def build_canary(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(f"OPENROUTER_API_KEY={OR_KEY}\nOLLAMA_API_KEY={OL_KEY}\n", encoding="utf-8")
        self.clock = FakeClock(BASE)
        self.keys = KeyStore(str(keys_path))
        key_by_sub = {"openrouter": OR_KEY, "ollama-cloud": OL_KEY, "antigravity": AG_KEY}
        or_body = ('{"data": {' + FMDR_OK + ', "usage_weekly": %s, "usage_daily": %s,'
                   ' "limit": %s, "limit_remaining": %s, "token": "%s", "label": "%s"}}'
                   % (OR_WEEKLY, OR_DAILY, OR_LIMIT, OR_LIMIT_REMAINING, TOKEN, ACCOUNT)).encode()
        self.or_http = ScriptedHttp(or_body)
        # 非 OpenRouter 的兩把假 fetcher 也帶金額；reports_weekly_usd 為 False，金額必須被擋下
        self.ollama = FakeFetcher(base=BASE)
        self.ollama.outcome = FetchResult(windows=windows(), weekly_usd=(float(OL_USD), BASE))
        self.antigravity = FakeFetcher(base=BASE)
        self.antigravity.outcome = FetchResult(windows=windows(), weekly_usd=(float(AG_USD), BASE))
        fetchers = {
            "openrouter": OpenRouterUsageFetcher(http_get=self.or_http, clock=self.clock),
            "ollama-cloud": self.ollama,
            "antigravity": self.antigravity,
        }
        self.usage = UsageService(self.clock, fetchers=fetchers, key_of=key_by_sub.get,
                                  deployed=lambda sub: sub in key_by_sub)
        self.service = RegistryService(str(root / "llm.db"), load_policy(POLICY),
                                       self.keys, self.clock, usage=self.usage)
        self.addCleanup(self.service.close)
        self.addCleanup(self.usage.stop)
        self.root = root
        return self.service

    def scan(self, where, text):
        for literal in canary_literals():
            with self.subTest(where=where, literal=literal):
                self.assertNotIn(literal, text)

    def test_D5_金額與憑證canary只在允許的位置出現(self):
        service = self.build_canary()
        self.serve()
        capture = _LogCapture()
        logger = logging.getLogger("lgwd")
        previous_level = logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, capture)
        self.addCleanup(logger.setLevel, previous_level)

        # 第一輪：各自失敗，失敗例外內含 canary（走 log）；第二輪：恢復成功（OpenRouter 與非 OpenRouter 都帶金額）
        ok_antigravity = self.antigravity.outcome
        self.or_http.raises = RuntimeError(f"leak {OR_KEY} {TOKEN} {ACCOUNT}")
        self.antigravity.outcome = RuntimeError(f"leak {AG_KEY} {AG_USD}")
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.or_http.raises = None
        self.antigravity.outcome = ok_antigravity
        self.clock.advance(hours=1)                  # 超過退避
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())

        summary = service.list_usage_summary()
        listed = service.list_subscriptions()
        resolved = service.resolve("opencode", "openrouter", subscription="openrouter")
        http_status, http_body = self.get("/v1/usage")
        self.assertEqual(http_status, 200)

        # 一覽：OpenRouter 的 weekly_used_usd 是唯一允許的 7.3191；其他欄位掃描為零命中
        self.assertEqual(self.row(summary, "openrouter")["weekly_used_usd"], float(OR_WEEKLY))
        self.assertEqual(self.row(summary, "ollama-cloud")["weekly_used_usd"], None)
        self.assertEqual(self.row(summary, "antigravity")["weekly_used_usd"], None)
        def without_allowed_amount(doc):
            """只拿掉 openrouter 的 weekly_used_usd（唯一允許的位置），其餘照掃。"""
            doc = json.loads(json.dumps(doc))
            for row in doc["subscriptions"]:
                if row["id"] == "openrouter":
                    row["weekly_used_usd"] = None
            return json.dumps(doc, ensure_ascii=False)

        self.assertEqual(json.loads(http_body), summary)
        self.assertEqual(json.dumps(summary, ensure_ascii=False).count(json.dumps(float(OR_WEEKLY))), 1)
        self.scan("usage", without_allowed_amount(summary))
        self.scan("GET /v1/usage", without_allowed_amount(json.loads(http_body)))
        self.scan("list", json.dumps(listed, ensure_ascii=False))

        # resolve：只豁免既有 env（OR key 的憑證值）；meta 與其他欄位照掃
        rest = {k: v for k, v in resolved.items() if k != "env"}
        self.scan("resolve", json.dumps(rest, ensure_ascii=False))
        env_text = json.dumps(resolved["env"], ensure_ascii=False).replace(OR_KEY, "")
        self.scan("resolve.env（去掉既有 OR key）", env_text)

        # log：只記固定碼與型別名，不含金額、key、token、帳號
        self.assertTrue(capture.lines)
        self.scan("log", "\n".join(capture.lines))

        # 磁碟上的 SQLite（與 WAL）：稽核不含金額與憑證；keys.env 是輸入，不在掃描範圍
        for path in self.root.iterdir():
            if path.name == "keys.env":
                continue
            with self.subTest(file=path.name):
                data = path.read_bytes().decode("latin-1")
                for literal in canary_literals():
                    self.assertNotIn(literal, data)


# lgwd/usage.py build_usage 組裝每根 pipe 時的鍵（pipes.append 的 dict），除此之外不得有任何鍵。
PIPE_KEYS = {"id", "models", "models_except", "remaining_percent", "resets_at"}


class UsdPipesNoteCanaryTest(HttpHarness):
    """D5 補強：canary 注入 pipes 與 note。
    (a) not_applicable 的 note 來自程式固定表：同一服務裡其他訂閱的上游失敗與金額 canary 不出現在任何 note。
    (b) 有 pipes 的訂閱：上游（OpenRouter 的 token／label、各家金額）帶 canary；pipes 除 remaining_percent
        外不含 canary；list、resolve 的 pipes 有真的管子，GET /v1/usage 不含 pipes。"""

    def build_pipes_canary(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(f"OPENROUTER_API_KEY={OR_KEY}\nOLLAMA_API_KEY={OL_KEY}\n", encoding="utf-8")
        self.clock = FakeClock(BASE)
        self.keys = KeyStore(str(keys_path))
        key_by_sub = {"openrouter": OR_KEY, "ollama-cloud": OL_KEY}
        or_body = ('{"data": {' + FMDR_OK + ', "usage_weekly": %s, "usage_daily": %s,'
                   ' "limit": %s, "limit_remaining": %s, "token": "%s", "label": "%s"}}'
                   % (OR_WEEKLY, OR_DAILY, OR_LIMIT, OR_LIMIT_REMAINING, TOKEN, ACCOUNT)).encode()
        self.or_http = ScriptedHttp(or_body)
        # 假 fetcher 取 PIPE_TABLE 內的 source，讓 ollama 也有 pipes；窗口百分比與金額互異
        self.ollama = FakeFetcher(base=BASE)
        self.ollama.source = "ollama-api-balance"
        self.ollama_ok = FetchResult(windows={"session": (61.5, at(days=1)), "weekly": (22.25, at(days=3))},
                                     weekly_usd=(float(OL_USD), BASE))
        self.ollama.outcome = self.ollama_ok
        fetchers = {
            "openrouter": OpenRouterUsageFetcher(http_get=self.or_http, clock=self.clock),
            "ollama-cloud": self.ollama,
        }
        self.usage = UsageService(self.clock, fetchers=fetchers, key_of=key_by_sub.get,
                                  deployed=lambda sub: sub in key_by_sub)
        self.service = RegistryService(str(root / "llm.db"), load_policy(POLICY),
                                       self.keys, self.clock, usage=self.usage)
        self.addCleanup(self.service.close)
        self.addCleanup(self.usage.stop)
        return self.service

    def scan(self, where, text):
        for literal in canary_literals():
            with self.subTest(where=where, literal=literal):
                self.assertNotIn(literal, text)

    def test_D5補強_pipes與note不含金額與憑證canary(self):
        service = self.build_pipes_canary()
        self.serve()
        # 第一輪：上游失敗，例外內含 canary；第二輪：恢復成功（超過退避後重取）
        self.or_http.raises = RuntimeError(f"leak {OR_KEY} {TOKEN} {ACCOUNT} {OR_LIMIT}")
        self.ollama.outcome = RuntimeError(f"leak {OL_KEY} {OL_USD}")
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())
        self.or_http.raises = None
        self.ollama.outcome = self.ollama_ok
        self.clock.advance(hours=1)
        service.list_usage_summary()
        self.assertTrue(self.usage.wait_idle())

        summary = service.list_usage_summary()
        listed = service.list_subscriptions()
        resolved = service.resolve("opencode", "openrouter", subscription="openrouter")
        http_status, http_body = self.get("/v1/usage")
        self.assertEqual(http_status, 200)
        http_doc = json.loads(http_body)
        self.assertEqual(http_doc, summary)

        # 確認 pipes 真的存在（否則掃描空轉）
        rows = {item["id"]: item for item in listed["subscriptions"]}
        self.assertEqual([p["id"] for p in rows["ollama-cloud"]["usage"]["pipes"]], ["session", "weekly"])
        self.assertIn("credits", [p["id"] for p in rows["openrouter"]["usage"]["pipes"]])
        self.assertEqual(rows["ollama-cloud"]["usage"]["pipes"][1]["remaining_percent"], 22.25)

        # pipes 結構證明：每根 pipe 的鍵集恰為 PIPE_KEYS，掃描時全欄位照掃（含 remaining_percent，
        # 不豁免）。合法百分比 61.5、22.25、80.0 與金額 canary（7.3191 等）互異，不會誤判。
        # 這兩條（鍵集、全欄位掃描）就是 pipes 無法夾帶任何注入文字或金額的結構證明。
        list_pipes = {sub: rows[sub]["usage"]["pipes"] for sub in ("ollama-cloud", "openrouter")}
        resolved_pipes = resolved["meta"]["usage"]["pipes"]
        self.assertTrue(resolved_pipes)
        for pipes in (*list_pipes.values(), resolved_pipes):
            for pipe in pipes:
                self.assertEqual(set(pipe), PIPE_KEYS)
        for sub, pipes in list_pipes.items():
            self.scan(f"list pipes {sub}", json.dumps(pipes, ensure_ascii=False))
        self.scan("resolve meta.usage.pipes", json.dumps(resolved_pipes, ensure_ascii=False))

        # GET /v1/usage 不含 pipes 鍵，也不含 canary（只拿掉 openrouter 的 weekly_used_usd）
        self.assertNotIn('"pipes"', http_body.decode("utf-8"))
        for row in http_doc["subscriptions"]:
            self.assertNotIn("pipes", row)
            if row["id"] == "openrouter":
                row["weekly_used_usd"] = None
        self.scan("GET /v1/usage", json.dumps(http_doc, ensure_ascii=False))

        # note：not_applicable 的 note 必須逐字等於固定表原文（表外為 GENERIC_NOTE），
        # 一覽、list、resolve 都套用同一斷言；加上下方的 note 全文掃描，note 無法夾帶任何注入文字或金額。
        def expected_note(sub):
            return lgwd_usage.NOT_APPLICABLE_TABLE.get(sub, (None, lgwd_usage.GENERIC_NOTE))[1]

        na_rows = [row for row in summary["subscriptions"] if row["status"] == "not_applicable"]
        self.assertTrue(na_rows)
        for row in na_rows:
            with self.subTest(id=row["id"]):
                self.assertEqual(row["note"], expected_note(row["id"]))
        for item in listed["subscriptions"]:
            if item["usage"]["status"] == "not_applicable":
                with self.subTest(id=item["id"]):
                    self.assertEqual(item["usage"]["note"], expected_note(item["id"]))
        if resolved["meta"]["usage"]["status"] == "not_applicable":
            self.assertEqual(resolved["meta"]["usage"]["note"], expected_note("openrouter"))
        self.scan("一覽 note", "\n".join(row["note"] for row in summary["subscriptions"] if row["note"]))
        self.scan("list note", "\n".join(item["usage"]["note"] for item in listed["subscriptions"]
                                         if item["usage"]["note"]))
        self.scan("resolve meta.usage.note", resolved["meta"]["usage"]["note"] or "")


if __name__ == "__main__":
    unittest.main()
