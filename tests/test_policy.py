"""policy/default.json 載入與驗證規則的測試。只用假資料。"""

import json
import tempfile
import unittest
from pathlib import Path

from lgwd.policy import PolicyError, load_policy

# 假資料：絕不是真的 key
CANARY = "canary-9f8e7d6c-value-never-real"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_json(tmpdir, obj, name="policy.json"):
    path = Path(tmpdir) / name
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def _good_policy(**over):
    base = {
        "subscriptions": {
            "claude-personal": {
                "agents": ["claude-code"],
                "models": {"allow": ["*"], "default": None},
                "cap": 2,
                "default_cooldown_seconds": 18000,
            },
            "openrouter": {
                "agents": ["opencode"],
                "models": {"allow": ["*:free"], "default": None},
                "cap": None,
                "default_cooldown_seconds": 3600,
            },
        },
        "order": {
            "claude-code": ["claude-personal"],
            "opencode": ["openrouter"],
        },
        "cooldown_buffer_seconds": 120,
        "lease_ttl_seconds": 43200,
        "confirm_within_seconds": 600,
        "resets_at_max_ahead_seconds": 691200,
    }
    base.update(over)
    return base


class LoadDefaultPolicyTests(unittest.TestCase):
    def test_default_policy_loads(self):
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        self.assertEqual(policy.candidates("claude-code"), ("claude-personal", "claude-company"))
        self.assertEqual(policy.candidates("opencode"), ("opencode-zen", "openrouter", "ollama-cloud"))
        self.assertEqual(policy.subscriptions["claude-personal"].cap, 2)
        self.assertIsNone(policy.subscriptions["openrouter"].cap)
        self.assertTrue(policy.allows("openrouter", "opencode", "anything:free"))
        self.assertFalse(policy.allows("openrouter", "opencode", "anything"))
        self.assertFalse(policy.allows("openrouter", "claude-code", None))
        self.assertFalse(policy.allows("nope", "opencode", None))
        self.assertEqual(policy.candidates("nope"), ())

    def test_claude_code_order_with_openrouter_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            obj = _good_policy()
            obj["order"]["claude-code"] = ["claude-personal", "openrouter"]
            obj["subscriptions"]["openrouter"]["agents"] = ["claude-code", "opencode"]
            with self.assertRaises(PolicyError):
                load_policy(_write_json(tmpdir, obj))

    def test_claude_code_order_with_ollama_cloud_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            obj = _good_policy()
            obj["subscriptions"]["ollama-cloud"] = {
                "agents": ["claude-code"],
                "models": {"allow": ["*"], "default": None},
                "cap": None,
                "default_cooldown_seconds": 18000,
            }
            obj["order"]["claude-code"] = ["ollama-cloud"]
            with self.assertRaises(PolicyError):
                load_policy(_write_json(tmpdir, obj))


class PolicyValidationTests(unittest.TestCase):
    def _reload(self, obj=None, raw=None):
        with tempfile.TemporaryDirectory() as tmpdir:
            if raw is not None:
                path = Path(tmpdir) / "policy.json"
                path.write_text(raw, encoding="utf-8")
            else:
                path = _write_json(tmpdir, obj if obj is not None else _good_policy())
            return load_policy(path)

    def test_unknown_subscription_key_rejected(self):
        with self.assertRaises(PolicyError):
            obj = _good_policy()
            obj["subscriptions"]["something-else"] = obj["subscriptions"].pop("openrouter")
            self._reload(obj)

    def test_order_unknown_agent_rejected(self):
        with self.assertRaises(PolicyError):
            obj = _good_policy()
            obj["order"]["something-else"] = ["openrouter"]
            self._reload(obj)

    def test_order_unknown_subscription_rejected(self):
        with self.assertRaises(PolicyError):
            obj = _good_policy()
            obj["order"]["opencode"] = ["openrouter", "ghost"]
            self._reload(obj)

    def test_claude_agents_restricted(self):
        with self.assertRaises(PolicyError):
            obj = _good_policy()
            obj["subscriptions"]["claude-personal"]["agents"] = ["claude-code", "opencode"]
            self._reload(obj)

    def test_zen_must_not_allow_claude_code(self):
        with self.assertRaises(PolicyError):
            obj = _good_policy()
            obj["subscriptions"]["opencode-zen"] = {
                "agents": ["opencode", "claude-code"],
                "models": {"allow": ["*"], "default": None},
                "cap": None,
                "default_cooldown_seconds": 3600,
            }
            self._reload(obj)

    def test_bad_numbers_rejected(self):
        for mutate in (
            lambda o: o["subscriptions"]["openrouter"].update(cap=0),
            lambda o: o["subscriptions"]["openrouter"].update(cap=-1),
            lambda o: o["subscriptions"]["openrouter"].update(cap=True),
            lambda o: o["subscriptions"]["openrouter"].update(cap="2"),
            lambda o: o["subscriptions"]["openrouter"].update(default_cooldown_seconds=0),
            lambda o: o.update(cooldown_buffer_seconds=0),
            lambda o: o.update(lease_ttl_seconds=None),
            lambda o: o.pop("order"),
        ):
            with self.subTest(mutate=getattr(mutate, "__name__", "?")):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    mutate(obj)
                    self._reload(obj)

    def test_bad_json_and_missing_file_rejected(self):
        with self.assertRaises(PolicyError):
            self._reload(raw="{not json")
        with self.assertRaises(PolicyError):
            load_policy("/nonexistent-dir-xyz/policy.json")

    def test_error_messages_carry_no_values(self):
        # canary 放在各個字串位置，例外訊息都不准出現
        cases = []
        obj = _good_policy()
        obj["subscriptions"][CANARY] = obj["subscriptions"].pop("openrouter")
        cases.append(obj)
        obj = _good_policy()
        obj["subscriptions"]["openrouter"]["agents"] = [CANARY]
        cases.append(obj)
        obj = _good_policy()
        obj["order"]["opencode"] = [CANARY]
        cases.append(obj)
        obj = _good_policy()
        obj["subscriptions"]["openrouter"]["models"]["allow"] = [CANARY]
        cases.append(obj)  # 合法字串，不該報錯但順便確認可載入
        for obj in cases[:3]:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    load_policy(_write_json(tmpdir, obj))
                except PolicyError as exc:
                    self.assertNotIn(CANARY, str(exc))
                else:
                    self.fail("expected PolicyError")
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = load_policy(_write_json(tmpdir, cases[3]))
            self.assertTrue(policy.allows("openrouter", "opencode", CANARY))
            # 註：policy 檔本來就在 repo（公開），不含 key 值；repr 不在不含值保證範圍


class AllowsCandidatesTests(unittest.TestCase):
    def test_model_none_uses_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            obj = _good_policy()
            obj["subscriptions"]["openrouter"]["models"]["default"] = "picked:free"
            policy = load_policy(_write_json(tmpdir, obj))
            self.assertTrue(policy.allows("openrouter", "opencode", None))
            self.assertFalse(policy.allows("openrouter", "opencode", "other"))

    def test_model_none_without_default_allows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = load_policy(_write_json(tmpdir, _good_policy()))
            self.assertTrue(policy.allows("openrouter", "opencode", None))
            self.assertFalse(policy.allows("openrouter", "opencode", 123))

    def test_candidates_unknown_agent_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = load_policy(_write_json(tmpdir, _good_policy()))
            self.assertEqual(policy.candidates("ghost"), ())
