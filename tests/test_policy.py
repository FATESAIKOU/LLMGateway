"""policy/default.json 載入與驗證規則的測試。只用假資料。"""

import fnmatch
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


def _sub(priority=10, agents=("opencode",), allow=("*",), default=None):
    return {
        "priority": priority,
        "agents": list(agents),
        "models": {"allow": list(allow), "default": default},
    }


def _good_policy(**over):
    base = {
        "subscriptions": {
            "claude-personal": _sub(10, ("claude-code",)),
            "openrouter": _sub(20, ("claude-code", "opencode"),
                               ("*:free", "openrouter/free"), "openrouter/free"),
        },
        "until_max_ahead_seconds": 691200,
    }
    base.update(over)
    return base


class LoadDefaultPolicyTests(unittest.TestCase):
    def test_default_policy_loads(self):
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        order = [s.name for s in policy.in_priority_order()]
        self.assertEqual(order, ["claude-personal", "opencode-zen",
                                 "claude-company", "openrouter", "ollama-cloud"])
        self.assertEqual(policy.subscriptions["claude-personal"].agents, ("claude-code",))
        self.assertEqual(policy.subscriptions["opencode-zen"].agents, ("opencode",))
        self.assertEqual(policy.subscriptions["openrouter"].models_allow,
                         ("*:free", "openrouter/free"))
        self.assertEqual(policy.subscriptions["openrouter"].agents, ("opencode",))
        self.assertIn("claude-code", policy.subscriptions["ollama-cloud"].agents)
        self.assertEqual(policy.until_max_ahead_seconds, 691200)
        self.assertFalse(hasattr(policy, "cooldown_buffer_seconds"))  # 窗口欄位拿掉
        self.assertFalse(hasattr(policy, "resets_at_max_ahead_seconds"))

    def test_openrouter出廠預設openrouter_free(self):
        # 出廠政策檔：openrouter 的 default 是 openrouter/free，allow 含 *:free 與
        # openrouter/free 兩個樣式（literal id 也要在樣式裡才選得到）
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        or_sub = policy.subscriptions["openrouter"]
        self.assertEqual(or_sub.models_default, "openrouter/free")
        self.assertIn("*:free", or_sub.models_allow)
        self.assertIn("openrouter/free", or_sub.models_allow)
        self.assertEqual(len(or_sub.models_allow), 2)

    def test_出廠五筆訂閱的models_default(self):
        # 出廠政策檔逐筆釘住：Claude 兩筆維持 null（kind=claude-subscription 時
        # 表示用 Claude Code 自己的預設），其餘三筆各自有 default。非 null 的
        # default 必須被同一筆的 allow 樣式涵蓋（allow 是 fnmatch 樣式；
        # lgwd 沒有現成的比對函式，測試內用 fnmatchcase 檢查）。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        expected = {
            "claude-personal": None,
            "claude-company": None,
            "opencode-zen": "muse-spark-1.3-contributor-free",
            "openrouter": "openrouter/free",
            "ollama-cloud": "deepseek-v4.1-flash",
        }
        self.assertEqual(set(policy.subscriptions), set(expected))
        for name, default in expected.items():
            with self.subTest(subscription=name):
                sub = policy.subscriptions[name]
                self.assertEqual(sub.models_default, default)
                if default is not None:
                    self.assertTrue(
                        any(fnmatch.fnmatchcase(default, pattern)
                            for pattern in sub.models_allow),
                        f"{name} 的 default 沒有被 models.allow 涵蓋",
                    )
                    # 供應商自己的模型 id：不帶 agent 專用的 provider 前綴
                    # （例如 opencode 的 "opencode/"；前綴由呼叫方的 agent
                    # driver 自己加）
                    self.assertFalse(
                        default.startswith("opencode/"),
                        f"{name} 的 default 帶了 agent 專用的 provider 前綴",
                    )

    def test_priority_order_stable_on_ties(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            obj = _good_policy()
            obj["subscriptions"]["ollama-cloud"] = _sub(10, ("opencode",))
            policy = load_policy(_write_json(tmpdir, obj))
            self.assertEqual(
                [s.name for s in policy.in_priority_order()],
                ["claude-personal", "ollama-cloud", "openrouter"],
            )


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

    def test_claude_agents_restricted(self):
        for agents in (["claude-code", "opencode"], ["opencode"], []):
            with self.subTest(agents=agents):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    obj["subscriptions"]["claude-personal"]["agents"] = agents
                    self._reload(obj)

    def test_zen_must_only_allow_opencode(self):
        with self.assertRaises(PolicyError):
            obj = _good_policy()
            obj["subscriptions"]["opencode-zen"] = _sub(10, ("opencode", "claude-code"))
            self._reload(obj)

    def test_openrouter_only_free_models(self):
        for allow in (["*"], ["*:free", "*"], ["openrouter/free"], ["*:free", "openrouter/free", "*"]):
            with self.subTest(allow=allow):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    obj["subscriptions"]["openrouter"]["models"]["allow"] = allow
                    self._reload(obj)

    def test_bad_numbers_rejected(self):
        for mutate in (
            lambda o: o["subscriptions"]["openrouter"].update(priority=-1),
            lambda o: o["subscriptions"]["openrouter"].update(priority="20"),
            lambda o: o["subscriptions"]["openrouter"].update(priority=True),
            lambda o: o["subscriptions"]["openrouter"].pop("priority"),
            lambda o: o.update(until_max_ahead_seconds=0),
            lambda o: o.update(until_max_ahead_seconds=None),
            lambda o: o.update(until_max_ahead_seconds=True),
            lambda o: o.pop("until_max_ahead_seconds"),
            lambda o: o.pop("subscriptions"),
        ):
            with self.subTest(mutate=getattr(mutate, "__name__", "?")):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    mutate(obj)
                    self._reload(obj)

    def test_舊窗口欄位被忽略(self):
        # 多出來的舊欄位（cooldown_buffer_seconds／resets_at_max_ahead_seconds）會被忽略，
        # 不影響載入；但真正的 v4 政策檔會被拒絕（缺 until_max_ahead_seconds），
        # 見 test_v4政策檔被拒（刻意 fail-closed）。
        obj = _good_policy()
        obj["cooldown_buffer_seconds"] = 120
        obj["resets_at_max_ahead_seconds"] = 691200
        policy = self._reload(obj)
        self.assertEqual(policy.until_max_ahead_seconds, 691200)

    def test_v4政策檔被拒(self):
        # v4 政策檔：只有舊欄位名、沒有 until_max_ahead_seconds → 拒絕（fail-closed），
        # 不會靜靜地吃預設值。升級時政策檔跟著 tag 一起換（docs/deploy.md 4.2）。
        with tempfile.TemporaryDirectory() as tmpdir:
            obj = _good_policy()
            obj.pop("until_max_ahead_seconds")
            obj["cooldown_buffer_seconds"] = 120
            obj["resets_at_max_ahead_seconds"] = 691200
            with self.assertRaises(PolicyError):
                load_policy(_write_json(tmpdir, obj))

    def test_bad_models_rejected(self):
        for mutate in (
            lambda o: o["subscriptions"]["openrouter"].update(models=[]),
            lambda o: o["subscriptions"]["openrouter"]["models"].update(allow=[]),
            lambda o: o["subscriptions"]["openrouter"]["models"].update(allow=[1]),
            lambda o: o["subscriptions"]["openrouter"]["models"].update(default=7),
            lambda o: o["subscriptions"]["openrouter"].pop("models"),
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
        obj["subscriptions"]["openrouter"]["models"]["allow"] = [CANARY]
        cases.append(obj)
        obj = _good_policy()
        obj["subscriptions"]["openrouter"]["models"]["default"] = 5
        cases.append(obj)
        for obj in cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    policy = load_policy(_write_json(tmpdir, obj))
                except PolicyError as exc:
                    self.assertNotIn(CANARY, str(exc))
                else:
                    # 合法值（models.allow）可載入也確認一次
                    self.assertIn(CANARY, policy.subscriptions["openrouter"].models_allow)


if __name__ == "__main__":
    unittest.main()
