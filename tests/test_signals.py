"""lgw/signals.py 的測試：樣本照 docs/research/quota-signals.md，正反兩面。"""

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lgw.signals as signals_mod
from lgw.signals import RULES, Classification, classify

UTC = timezone.utc
TZ8 = timezone(timedelta(hours=8))  # 固定時區，測「轉 UTC」與「隔天」


def at_utc(*args):
    return datetime(*args, tzinfo=UTC)


class ClaudeTest(unittest.TestCase):
    def test_session_limit(self):
        now = datetime(2026, 10, 11, 15, 0, tzinfo=TZ8)  # 週日
        res = classify("claude-code", "You've hit your session limit · resets 3:45pm",
                       now=now, tz=TZ8)
        self.assertEqual(res.kind, "exhausted")
        self.assertEqual(res.signal, "claude_limit")
        self.assertEqual(res.resets_at, at_utc(2026, 10, 11, 7, 45))

    def test_weekly_limit_next_monday(self):
        now = datetime(2026, 10, 11, 15, 0, tzinfo=TZ8)  # 週日
        res = classify("claude-code", "You've hit your weekly limit · resets Mon 12:00am",
                       now=now, tz=TZ8)
        self.assertEqual(res.signal, "claude_limit")
        self.assertEqual(res.resets_at, at_utc(2026, 10, 11, 16, 0))

    def test_opus_sonnet(self):
        now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
        for family in ("Opus", "Sonnet"):
            with self.subTest(family=family):
                res = classify(
                    "claude-code",
                    "You've hit your {0} limit · resets 3:45pm".format(family),
                    now=now, tz=UTC)
                self.assertEqual((res.kind, res.signal), ("exhausted", "claude_limit"))
                self.assertEqual(res.resets_at, at_utc(2026, 10, 11, 15, 45))

    def test_past_today_goes_tomorrow(self):
        now = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)  # 已過 3:45pm
        res = classify("claude-code", "You've hit your session limit · resets 3:45pm",
                       now=now, tz=UTC)
        self.assertEqual(res.resets_at, at_utc(2026, 10, 12, 15, 45))

    def test_same_weekday_past_goes_next_week(self):
        now = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)  # 週一，已過 00:00
        res = classify("claude-code", "You've hit your weekly limit · resets Mon 12:00am",
                       now=now, tz=UTC)
        self.assertEqual(res.resets_at, at_utc(2026, 10, 19, 0, 0))

    def test_spend_limit_no_reset(self):
        now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
        texts = [
            "You've hit your monthly spend limit · raise it at https://example.com",
            "You've hit your individual spend limit · ask your admin for a higher limit",
            "You've hit your team's shared budget · ask your admin to raise it",
            "spend limit reached (daily; resets 2026-08-09 00:00 UTC)",
            "spend limit reached",
            "You've hit your individual usage limit",
        ]
        for text in texts:
            with self.subTest(text=text[:40]):
                res = classify("claude-code", text, now=now, tz=UTC)
                self.assertEqual(res.kind, "exhausted")
                self.assertEqual(res.signal, "claude_limit")
                self.assertIsNone(res.resets_at)  # 用政策預設冷卻，不拿訊息時間

    def test_warning_is_normal(self):
        # 85% 預警不是用完，不換把
        res = classify("claude-code",
                       "You've used 85% of your session limit · resets 3:45pm")
        self.assertEqual(res.kind, "normal")
        self.assertIsNone(res.signal)


class ZenTest(unittest.TestCase):
    def test_free_type_only(self):
        res = classify("opencode", '{"type":"error","error":{"type":"FreeUsageLimitError",'
                                   '"message":"Error from provider (Console): Rate limit '
                                   'exceeded. Please try again later."}}')
        self.assertEqual((res.kind, res.signal), ("exhausted", "zen_free_limit"))
        self.assertIsNone(res.resets_at)  # 沒 footer 就拿不到時間

    def test_free_with_retrying_in(self):
        now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
        res = classify("opencode", "Free usage exceeded, subscribe to Go "
                                   "[retrying in 15h 30m attempt #1]",
                       now=now, tz=UTC)
        self.assertEqual((res.kind, res.signal), ("exhausted", "zen_free_limit"))
        self.assertEqual(res.resets_at, at_utc(2026, 10, 12, 1, 30))

    def test_free_retrying_without_duration(self):
        res = classify("opencode", "Free usage exceeded, subscribe to Go "
                                   "[retrying attempt #2]")
        self.assertEqual((res.kind, res.signal), ("exhausted", "zen_free_limit"))
        self.assertIsNone(res.resets_at)

    def test_go_limit(self):
        now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
        res = classify("opencode", "5 hour usage limit reached. It will reset in "
                                   "5 hours 23 minutes. To continue using this model now, "
                                   "enable usage from your available balance - https://x",
                       now=now, tz=UTC)
        self.assertEqual((res.kind, res.signal), ("exhausted", "zen_go_limit"))
        self.assertEqual(res.resets_at, at_utc(2026, 10, 11, 15, 23))

    def test_go_limit_short(self):
        now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
        res = classify("opencode", '{"type":"error","error":{"type":"GoUsageLimitError",'
                                   '"message":"Subscription quota exceeded."}}',
                       now=now, tz=UTC)
        self.assertEqual((res.kind, res.signal), ("exhausted", "zen_go_limit"))
        self.assertIsNone(res.resets_at)

    def test_credit_limit(self):
        res = classify("opencode", '{"type":"error","error":{"type":"CreditLimitExceeded",'
                                   '"message":"Account budget exceeded."}}')
        self.assertEqual((res.kind, res.signal), ("exhausted", "credit_limit"))
        self.assertIsNone(res.resets_at)


class ProviderTest(unittest.TestCase):
    def test_openrouter_402(self):
        res = classify("opencode", '{"error":{"code":402,"message":"Insufficient credits.",'
                                   '"metadata":{"limit_source":"openrouter_credits"}}}')
        self.assertEqual((res.kind, res.signal), ("exhausted", "openrouter_limit"))

    def test_openrouter_in_flight_is_transient(self):
        res = classify("opencode", '{"error":{"code":402,"message":"in-flight",'
                                   '"metadata":{"reason":"in_flight_budget_exhausted",'
                                   '"limit_source":"openrouter_in_flight_budget"}}}')
        self.assertEqual(res.kind, "transient")
        self.assertIsNone(res.signal)

    def test_ollama_session_limit(self):
        res = classify("opencode", 'HTTP 429: {"error":"you (bob) have reached your '
                                   'session usage limit, upgrade for higher limits: '
                                   'https://ollama.com/upgrade"}')
        self.assertEqual((res.kind, res.signal), ("exhausted", "ollama_limit"))

    def test_ollama_weekly_limit(self):
        res = classify("opencode", "weekly usage limit exceeded, retry later")
        self.assertEqual((res.kind, res.signal), ("exhausted", "ollama_limit"))


class TransientTest(unittest.TestCase):
    def test_markers(self):
        texts = [
            "API Error: Request rejected (429) · this may be a temporary capacity issue.",
            '{"error":{"code":429,"message":"Rate limit exceeded"}}',
            "Rate limit exceeded, please try again later",
            "Provider is overloaded",
            "Error: 429: Too Many Requests",
        ]
        for text in texts:
            with self.subTest(text=text[:40]):
                res = classify("opencode", text)
                self.assertEqual(res.kind, "transient")
                self.assertIsNone(res.signal)
                self.assertIsNone(res.resets_at)

    def test_claude_agent_also_transient(self):
        res = classify("claude-code", "API Error: Request rejected (429)")
        self.assertEqual(res.kind, "transient")


class NormalTest(unittest.TestCase):
    def test_normal_texts(self):
        texts = [
            "Hello, world",
            "",
            "Credit balance is too low",  # API key 配錯，不是額度用完
            "API Error: Usage credits required for 1M context · run /usage-credits",
            "API Error: Server is temporarily limiting requests (not your usage limit)",
        ]
        for text in texts:
            with self.subTest(text=text[:40]):
                res = classify("opencode", text)
                self.assertEqual(res, Classification(kind="normal"))

    def test_agent_scope(self):
        # 规则只认自己 agent 的画面：拿錯 agent 就判不出（fail closed）
        res = classify("opencode", "You've hit your session limit · resets 3:45pm")
        self.assertEqual(res.kind, "normal")
        res = classify("claude-code", "Free usage exceeded, subscribe to Go")
        self.assertEqual(res.kind, "normal")
        res = classify("unknown-agent", "You've hit your session limit · resets 3:45pm")
        self.assertEqual(res.kind, "normal")

    def test_none_text(self):
        self.assertEqual(classify("opencode", None).kind, "normal")


class TableTest(unittest.TestCase):
    def test_rules_shape(self):
        kinds = {"exhausted", "transient"}
        for rule in RULES:
            with self.subTest(rule=rule.get("signal") or rule["kind"]):
                self.assertIn("agent", rule)
                self.assertIn("pattern", rule)
                self.assertIn("signal", rule)
                self.assertIn(rule["kind"], kinds)
                self.assertIn("reset", rule)
                rule["pattern"].search("")  # regex 可編譯可用
                if rule.get("reset_pattern") is not None:
                    rule["reset_pattern"].search("")

    def test_no_agent_tool_imports(self):
        """signals 不引用 subprocess、herdr、agora，不做平台判斷。」"""
        tree = ast.parse(Path(signals_mod.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            if isinstance(node, ast.Attribute) and node.attr == "platform":
                self.fail("sys.platform 判断不允许")
        self.assertTrue(imported.isdisjoint({"subprocess", "herdr", "agora", "sys"}),
                        imported)


if __name__ == "__main__":
    unittest.main()
