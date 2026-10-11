"""build_env 對照表的測試：七種組合、拒絕、模型變數、不含值保證。只用假資料。"""

import unittest

from lgwd.envmap import OLLAMA_ANTHROPIC_BASE_URL, build_env

# 假資料：絕不是真的 key
CANARY = "canary-b7e1a2-value-never-real"


class BuildEnvTableTests(unittest.TestCase):
    def test_claude_personal_for_claude_code(self):
        env, unset = build_env("claude-personal", "claude-code", CANARY, None)
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": CANARY})
        self.assertEqual(unset, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])

    def test_claude_company_for_claude_code(self):
        env, unset = build_env("claude-company", "claude-code", CANARY, None)
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": CANARY})
        self.assertEqual(unset, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])

    def test_openrouter_for_claude_code(self):
        env, unset = build_env("openrouter", "claude-code", CANARY, None)
        self.assertEqual(
            env,
            {
                "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                "ANTHROPIC_AUTH_TOKEN": CANARY,
                "ANTHROPIC_API_KEY": "",
            },
        )
        self.assertEqual(unset, ["CLAUDE_CODE_OAUTH_TOKEN"])

    def test_ollama_cloud_for_claude_code(self):
        env, unset = build_env("ollama-cloud", "claude-code", CANARY, None)
        self.assertEqual(
            env,
            {
                "ANTHROPIC_BASE_URL": OLLAMA_ANTHROPIC_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": CANARY,
                "ANTHROPIC_API_KEY": "",
            },
        )
        self.assertEqual(unset, ["CLAUDE_CODE_OAUTH_TOKEN"])

    def test_openrouter_for_opencode(self):
        env, unset = build_env("openrouter", "opencode", CANARY, None)
        self.assertEqual(env, {"OPENROUTER_API_KEY": CANARY})
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", unset)

    def test_ollama_cloud_for_opencode(self):
        env, unset = build_env("ollama-cloud", "opencode", CANARY, None)
        self.assertEqual(env, {"OLLAMA_API_KEY": CANARY})
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", unset)

    def test_zen_for_opencode(self):
        env, unset = build_env("opencode-zen", "opencode", CANARY, None)
        self.assertEqual(env, {"OPENCODE_API_KEY": CANARY})
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", unset)

    def test_opencode_unset_covers_all_four(self):
        _, unset = build_env("opencode-zen", "opencode", CANARY, None)
        self.assertEqual(
            unset,
            ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"],
        )

    def test_refusals(self):
        for subscription, agent in (
            ("claude-personal", "opencode"),
            ("claude-company", "opencode"),
            ("opencode-zen", "claude-code"),
            ("ghost", "claude-code"),
            ("openrouter", "ghost"),
        ):
            with self.subTest(subscription=subscription, agent=agent):
                with self.assertRaises(ValueError):
                    build_env(subscription, agent, CANARY, None)

    def test_model_vars(self):
        env, _ = build_env("claude-personal", "claude-code", CANARY, "opus")
        self.assertEqual(env["ANTHROPIC_MODEL"], "opus")
        env, _ = build_env("openrouter", "opencode", CANARY, "m:free")
        self.assertEqual(env["LGW_MODEL"], "m:free")
        env, _ = build_env("openrouter", "opencode", CANARY, None)
        self.assertNotIn("LGW_MODEL", env)
        env, _ = build_env("openrouter", "opencode", CANARY, "")
        self.assertNotIn("LGW_MODEL", env)

    def test_returns_fresh_containers(self):
        env, unset = build_env("openrouter", "opencode", CANARY, None)
        env["X"] = "y"
        unset.append("X")
        env2, unset2 = build_env("openrouter", "opencode", CANARY, None)
        self.assertEqual(env2, {"OPENROUTER_API_KEY": CANARY})
        self.assertNotIn("X", unset2)

    def test_errors_carry_no_values(self):
        for subscription, agent in (("ghost", "claude-code"), ("openrouter", "ghost"), (CANARY, CANARY)):
            with self.subTest(subscription=subscription, agent=agent):
                try:
                    build_env(subscription, agent, CANARY, CANARY)
                except ValueError as exc:
                    self.assertNotIn(CANARY, str(exc))
                else:
                    self.fail("expected ValueError")
