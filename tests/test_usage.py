import unittest

from lgwd.clock import FakeClock
from lgwd.usage import UsagePoller

CANARY = "canary-key-value-should-never-appear"


class FakeKeys:
    def __init__(self, values):
        self.values = values

    def get(self, subscription):
        return self.values.get(subscription)


class UsagePollerTests(unittest.TestCase):
    def test_openrouter_ok_keeps_only_usage_fields(self):
        def fetch(url, key, timeout):
            self.assertEqual(key, CANARY)
            if "openrouter" in url:
                return 200, {"data": {"usage": 1.5, "limit": 10, "limit_remaining": 8.5, "label": "secret-label"}}
            return 200, {"included": {"session": {"remaining_percent": 40, "resets_at": "2026-10-11T05:00:00Z"}}}

        poller = UsagePoller(FakeKeys({"openrouter": CANARY, "ollama-cloud": CANARY}), FakeClock(), fetch)
        poller.poll_once()
        snap = poller.snapshot()
        self.assertEqual(snap["openrouter"]["state"], "ok")
        self.assertEqual(snap["openrouter"]["summary"], {"usage": 1.5, "limit": 10, "limit_remaining": 8.5})
        self.assertEqual(snap["ollama-cloud"]["summary"]["session"]["remaining_percent"], 40)
        self.assertNotIn(CANARY, repr(snap))

    def test_unknown_for_claude_and_zen(self):
        poller = UsagePoller(FakeKeys({}), FakeClock(), lambda *a: (200, {}))
        snap = poller.snapshot()
        for name in ("claude-personal", "claude-company", "opencode-zen"):
            self.assertEqual(snap[name]["state"], "unknown")

    def test_invalid_key_and_errors_do_not_raise(self):
        responses = {"openrouter": (401, None)}

        def fetch(url, key, timeout):
            if "openrouter" in url:
                return responses["openrouter"]
            raise TimeoutError()

        clock = FakeClock()
        poller = UsagePoller(FakeKeys({"openrouter": CANARY, "ollama-cloud": CANARY}), clock, fetch)
        poller.poll_once()
        snap = poller.snapshot()
        self.assertEqual(snap["openrouter"]["state"], "invalid_key")
        self.assertEqual(snap["ollama-cloud"]["state"], "error")
        self.assertEqual(snap["ollama-cloud"]["error"], "timeout")

    def test_last_ok_kept_after_failure(self):
        state = {"ok": True}

        def fetch(url, key, timeout):
            if state["ok"]:
                return 200, {"data": {"usage": 1}}
            return 500, None

        clock = FakeClock()
        poller = UsagePoller(FakeKeys({"openrouter": CANARY}), clock, fetch)
        poller.poll_once()
        first_ok = poller.snapshot()["openrouter"]["last_ok_at"]
        state["ok"] = False
        clock.advance(minutes=5)
        poller.poll_once()
        snap = poller.snapshot()["openrouter"]
        self.assertEqual(snap["state"], "error")
        self.assertEqual(snap["error"], "http_500")
        self.assertEqual(snap["last_ok_at"], first_ok)

    def test_not_deployed(self):
        poller = UsagePoller(FakeKeys({}), FakeClock(), lambda *a: self.fail("must not fetch"))
        poller.poll_once()
        self.assertEqual(poller.snapshot()["openrouter"]["error"], "not_deployed")

    def test_disabled_does_nothing(self):
        poller = UsagePoller(FakeKeys({"openrouter": CANARY}), FakeClock(), lambda *a: self.fail("disabled"), enabled=False)
        poller.poll_once()
        self.assertEqual(poller.snapshot()["openrouter"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
