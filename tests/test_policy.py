"""policy/default.json 載入與驗證規則的測試。只用假資料。

1.0 的 endpoint 檢查在這裡：monkeypatch PROVIDERS 的假 provider 表（不是假政策檔）
＋直接呼叫 validate_endpoints；組裝點（Gateway）的行為在 test_main。
"""

import fnmatch
import json
import tempfile
import unittest
from pathlib import Path

from lgwd import providers as lgwd_providers
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
            "openrouter": _sub(20, ("opencode",),
                               ("*:free", "openrouter/free"), "openrouter/free"),
        },
        "until_max_ahead_seconds": 691200,
    }
    base.update(over)
    return base


def _covered(sub, model_id):
    """model_id 是否被該筆的 allow 樣式涵蓋（與 lgwd 的檢查同一個 fnmatch）。"""
    return any(fnmatch.fnmatchcase(model_id, pattern) for pattern in sub.models_allow)


class LoadDefaultPolicyTests(unittest.TestCase):
    def test_default_policy_loads(self):
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        order = [s.name for s in policy.in_priority_order()]
        self.assertEqual(order, ["opencode-zen", "opencode-go", "openrouter", "groq",
                                 "ollama-cloud", "antigravity", "claude-personal"])
        self.assertEqual(policy.subscriptions["claude-personal"].agents, ("claude-code",))
        self.assertEqual(policy.subscriptions["opencode-zen"].agents, ("opencode",))
        self.assertEqual(policy.subscriptions["openrouter"].models_allow,
                         ("*:free", "openrouter/free"))
        self.assertEqual(policy.subscriptions["openrouter"].agents, ("opencode",))
        self.assertIn("claude-code", policy.subscriptions["ollama-cloud"].agents)
        self.assertEqual(policy.subscriptions["antigravity"].agents, ("agy",))
        self.assertEqual(policy.until_max_ahead_seconds, 691200)
        self.assertFalse(hasattr(policy, "cooldown_buffer_seconds"))  # 窗口欄位拿掉
        self.assertFalse(hasattr(policy, "resets_at_max_ahead_seconds"))

    def test_出廠政策移除claude_company且claude_personal排最後(self):
        # #50：拔掉公司 Claude 訂閱；claude-personal 排在 Ollama 之後並預設 Sonnet
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        self.assertEqual(set(policy.subscriptions),
                         {"claude-personal", "opencode-zen", "opencode-go",
                          "openrouter", "groq", "ollama-cloud", "antigravity"})
        self.assertEqual([s.name for s in policy.in_priority_order()],
                         ["opencode-zen", "opencode-go", "openrouter", "groq",
                          "ollama-cloud", "antigravity", "claude-personal"])
        self.assertEqual(policy.subscriptions["claude-personal"].models_default,
                         "claude-sonnet-5-5")

    def test_openrouter出廠預設openrouter_free(self):
        # 出廠政策檔：openrouter 的 default 是 openrouter/free，allow 含 *:free 與
        # openrouter/free 兩個樣式（literal id 也要在樣式裡才選得到）
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        or_sub = policy.subscriptions["openrouter"]
        self.assertEqual(or_sub.models_default, "openrouter/free")
        self.assertIn("*:free", or_sub.models_allow)
        self.assertIn("openrouter/free", or_sub.models_allow)
        self.assertEqual(len(or_sub.models_allow), 2)

    def test_出廠七筆訂閱的models_default(self):
        # 出廠政策檔逐筆釘住：七筆各自有 default（#50 起 claude-personal 預設
        # claude-sonnet-5-5，claude-company 已移除；#77 加 opencode-go；#88 加 groq；
        # #107 加 antigravity）。非 null 的 default 必須被同一筆的 allow 樣式涵蓋
        # （allow 是 fnmatch 樣式；lgwd 沒有現成的比對函式，測試內用 fnmatchcase 檢查）。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        expected = {
            "claude-personal": "claude-sonnet-5-5",
            "opencode-zen": "mimo-v2.6-flash-free",
            "opencode-go": "beam-free",
            "openrouter": "openrouter/free",
            "groq": "openai/gpt-oss-120b",
            "ollama-cloud": "deepseek-v4.1-flash",
            "antigravity": "gemini-3.8-flash-low",
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

    def test_zen_allow涵蓋default與免費模型(self):
        # 出廠政策（#26）：zen 的 allow 是 ["*-free", "big-pickle"]；default 與
        # big-pickle 被涵蓋，其他 *-free 的 id 也被涵蓋；付費的 id（例如
        # muse-spark-1.3）不被涵蓋。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        zen = policy.subscriptions["opencode-zen"]
        self.assertEqual(zen.models_allow, ("*-free", "big-pickle"))
        self.assertTrue(_covered(zen, "mimo-v2.6-flash-free"))  # default
        self.assertTrue(_covered(zen, "big-pickle"))
        self.assertTrue(_covered(zen, "spark-2-free"))  # 其他 *-free 的 id
        self.assertFalse(_covered(zen, "muse-spark-1.3"))  # 付費的 id

    def test_go_allow涵蓋default與免費模型(self):
        # #95：opencode-go 的 allow 是 ["beam-free"]；default 被涵蓋，
        # iceman-contributor-free 不再涵蓋（以授權訓練 prompt 換免費、不進輪替），
        # allow 以外的（付費的 kimi-k3）不被涵蓋。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        go = policy.subscriptions["opencode-go"]
        self.assertEqual(go.models_allow, ("beam-free",))
        self.assertFalse(_covered(go, "iceman-contributor-free"))  # #95 起 iceman 不再涵蓋
        self.assertTrue(_covered(go, "beam-free"))  # default
        self.assertFalse(_covered(go, "kimi-k3"))  # 付費的 id

    def test_groq_allow只放免費層的模型(self):
        # #88：groq 的 allow 寫死免費層可用的 id（官方 Free Plan 的 rate limits 表：
        # 2026-10-27 讀 console.groq.com/docs/rate-limits），不放 "*" 也不放寬樣式；
        # 免費層表上沒有的 id（例如企業方案才有的 llama-3.1-8b-instant）不被涵蓋。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        groq = policy.subscriptions["groq"]
        self.assertEqual(groq.agents, ("opencode",))
        self.assertEqual(
            groq.models_allow,
            ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"),
        )
        for pattern in groq.models_allow:
            self.assertNotIn("*", pattern)  # 不放 "*"／萬用字元
            self.assertNotIn("?", pattern)
        self.assertEqual(groq.models_default, "openai/gpt-oss-120b")
        self.assertTrue(_covered(groq, "openai/gpt-oss-120b"))  # default
        self.assertTrue(_covered(groq, "openai/gpt-oss-20b"))
        self.assertTrue(_covered(groq, "qwen/qwen3.8-27b"))
        self.assertFalse(_covered(groq, "llama-3.1-8b-instant"))  # Free Plan 表上沒有
        self.assertFalse(_covered(groq, "meta-models/muse-spark-open"))

    def test_antigravity出廠12個字面模型id(self):
        # #107 本人裁定：本人從 `agy models` 抄的 12 個裸 id，照抄、不增不減不改。
        # 出廠政策只放字面 id（不含萬用字元）：錯字在 resolve 就是 403，不會延到 agy。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        sub = policy.subscriptions["antigravity"]
        expected = (
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-4-argon-low",
            "gemini-4-argon-medium",
            "gemini-4-argon-high",
            "claude-opus-5-5-low",
            "claude-opus-5-5-medium",
            "claude-opus-5-5-high",
            "claude-sonnet-5-5-low",
            "claude-sonnet-5-5-medium",
            "claude-sonnet-5-5-high",
        )
        self.assertEqual(sub.models_allow, expected)
        self.assertEqual(sub.models_default, "gemini-3.8-flash-low")
        self.assertEqual(sub.agents, ("agy",))
        self.assertEqual(sub.priority, 35)
        self.assertEqual(sub.privacy, "public")
        for model_id in expected:
            with self.subTest(model=model_id):
                self.assertTrue(_covered(sub, model_id))
                self.assertNotIn("*", model_id)
                self.assertNotIn("?", model_id)
        # 不在清單裡的（含前綴與 GPT-OSS）都不被涵蓋
        self.assertFalse(_covered(sub, "gemini-9-fake"))
        self.assertFalse(_covered(sub, "google/gemini-3.8-flash-low"))
        self.assertFalse(_covered(sub, "gpt-oss-120b"))

    def test_roles不含antigravity(self):
        # #107 本人裁定：roles 不納入（不改 roles 段）。
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        for role, rp in policy.roles.items():
            with self.subTest(role=role):
                self.assertNotIn("antigravity", rp.order)

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

    def test_go_must_only_allow_opencode(self):
        # #77：opencode-go 和 zen 一樣，agents 不是剛好 ["opencode"] 就拒收
        for agents in (["opencode", "claude-code"], ["claude-code"], []):
            with self.subTest(agents=agents):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    obj["subscriptions"]["opencode-go"] = _sub(15, agents)
                    self._reload(obj)

    def test_go_agents剛好opencode可載入(self):
        obj = _good_policy()
        obj["subscriptions"]["opencode-go"] = _sub(15, ("opencode",))
        policy = self._reload(obj)
        self.assertEqual(policy.subscriptions["opencode-go"].agents, ("opencode",))

    def test_groq_must_only_allow_opencode(self):
        # #88：groq 的端點是 OpenAI 相容的，claude-code 接不進去；agents 不是剛好
        # ["opencode"] 的政策檔一律拒收（比照 zen／go）
        for agents in (["opencode", "claude-code"], ["claude-code"], []):
            with self.subTest(agents=agents):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    obj["subscriptions"]["groq"] = _sub(25, agents)
                    self._reload(obj)

    def test_groq_agents剛好opencode可載入(self):
        obj = _good_policy()
        obj["subscriptions"]["groq"] = _sub(
            25, ("opencode",), ("openai/gpt-oss-120b",), "openai/gpt-oss-120b")
        policy = self._reload(obj)
        self.assertEqual(policy.subscriptions["groq"].agents, ("opencode",))
        self.assertEqual(policy.subscriptions["groq"].models_default,
                         "openai/gpt-oss-120b")

    def test_openrouter_only_free_models(self):
        for allow in (["*"], ["*:free", "*"], ["openrouter/free"], ["*:free", "openrouter/free", "*"]):
            with self.subTest(allow=allow):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    obj["subscriptions"]["openrouter"]["models"]["allow"] = allow
                    self._reload(obj)

    def test_openrouter_agents_must_only_allow_opencode(self):
        # 政策檔把 openrouter 的 agents 改寫 → 拒收（免費模型只經過 opencode；
        # resolve 才不會把 Claude Code 指到 OpenRouter）
        for agents in (["claude-code", "opencode"], ["claude-code"], []):
            with self.subTest(agents=agents):
                with self.assertRaises(PolicyError):
                    obj = _good_policy()
                    obj["subscriptions"]["openrouter"]["agents"] = agents
                    self._reload(obj)

    def test_openrouter_agents剛好opencode可載入(self):
        obj = _good_policy()
        policy = self._reload(obj)
        self.assertEqual(policy.subscriptions["openrouter"].agents, ("opencode",))

    def test_antigravity可載入且agents只給agy(self):
        # #107：沒有憑證的訂閱（kind local-login）。PR 1 不動出廠政策，用臨時政策檔。
        obj = _good_policy()
        obj["subscriptions"]["antigravity"] = _sub(
            35, ("agy",), ("gemini-test-id",), "gemini-test-id")
        policy = self._reload(obj)
        self.assertEqual(policy.subscriptions["antigravity"].agents, ("agy",))
        self.assertEqual(policy.subscriptions["antigravity"].models_default,
                         "gemini-test-id")

    def test_antigravity的agents必須剛好agy(self):
        for agents in (["agy", "opencode"], ["claude-code"], ["opencode"], []):
            with self.subTest(agents=agents):
                with self.assertRaises(PolicyError) as ctx:
                    obj = _good_policy()
                    obj["subscriptions"]["antigravity"] = _sub(
                        35, agents, ("gemini-test-id",), None)
                    self._reload(obj)
                # 例外訊息不含檔案裡的值
                self.assertNotIn("gemini-test-id", str(ctx.exception))

    def test_其他訂閱的agents含agy拒收(self):
        # 反向條款：有憑證的 row 不能允許 agy（不然它會用空 env 被選中）
        obj = _good_policy()
        obj["subscriptions"]["ollama-cloud"] = _sub(30, ("agy",))
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("agy", str(ctx.exception))

    def test_agy以外的新鍵仍拒收(self):
        # 白名單仍是固定集合：只有 antigravity 被接受，其他新鍵照樣拒收
        obj = _good_policy()
        obj["subscriptions"]["google"] = obj["subscriptions"].pop("openrouter")
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("unknown key", str(ctx.exception))

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

    def test_default必須被allow涵蓋(self):
        # 非 null 的 default 蓋不進同一筆的 allow → 拒收（fail-closed），訊息指出
        # 是哪一筆；這裡用合法的訂閱 id，不是檔案裡的自由字串。
        obj = _good_policy()
        obj["subscriptions"]["claude-personal"]["models"] = {
            "allow": ["*-free"], "default": "muse-spark-1.3",
        }
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("claude-personal", str(ctx.exception))
        # 例外不回顯檔案裡的值：訊息不含 default 的值（審查低-1）
        self.assertNotIn("muse-spark-1.3", str(ctx.exception))

    def test_default是null時不檢查涵蓋(self):
        # default 為 null 時不做涵蓋檢查（呼叫方自己選）
        obj = _good_policy()
        obj["subscriptions"]["opencode-zen"] = _sub(
            10, ("opencode",), ("*-free", "big-pickle"), None)
        policy = self._reload(obj)
        self.assertIsNone(policy.subscriptions["opencode-zen"].models_default)

    # --- #60 usage 段（spec usage-reporting「政策檔的 usage 設定」） ---
    def _usage(self, usage):
        obj = _good_policy()
        obj["usage"] = usage
        return self._reload(obj)

    def test_沒有usage段時維持原行為(self):
        policy = self._reload()
        self.assertEqual(policy.usage, {})

    def test_usage為null時拒收(self):
        with self.assertRaises(PolicyError):
            self._usage(None)

    def test_usage合法(self):
        policy = self._usage({"claude-personal": {"enabled": True},
                              "openrouter": {"enabled": False, "ttl_seconds": 600}})
        self.assertIs(policy.usage["claude-personal"].enabled, True)
        self.assertIsNone(policy.usage["claude-personal"].ttl_seconds)
        self.assertEqual(policy.usage["openrouter"].ttl_seconds, 600)

    def test_usage_明確關閉_解析為False(self):
        policy = self._usage({"claude-personal": {"enabled": False}})
        self.assertIs(policy.usage["claude-personal"].enabled, False)

    def test_usage不合法一律PolicyError(self):
        bad = {
            "usage不是object": [],
            "usage是字串": "x",
            "未知id": {"nope": {"enabled": True}},
            "政策裡沒有的id": {"ollama-cloud": {"enabled": True}},
            "值不是object": {"claude-personal": True},
            "內層未知鍵": {"claude-personal": {"enable": False}},
            "enabled是字串": {"claude-personal": {"enabled": "yes"}},
            "enabled是整數": {"claude-personal": {"enabled": 1}},
            "ttl為0": {"claude-personal": {"ttl_seconds": 0}},
            "ttl為負": {"claude-personal": {"ttl_seconds": -5}},
            "ttl是字串": {"claude-personal": {"ttl_seconds": "60"}},
            "ttl是浮點": {"claude-personal": {"ttl_seconds": 60.5}},
            "ttl是布林": {"claude-personal": {"ttl_seconds": True}},
        }
        for label, usage in bad.items():
            with self.subTest(label), self.assertRaises(PolicyError):
                self._usage(usage)

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


class _FakeEndpointProvider(lgwd_providers.CredentialProvider):
    """假 provider：只為了塞一個 endpoint 值進 PROVIDERS 表（1.0 的檢查在這臉上跑）。"""

    def __init__(self, provider: str, endpoint):
        self.provider = provider
        self.kind = "api-key"
        self.endpoint = endpoint


class ValidateEndpointsTests(unittest.TestCase):
    """providers 表的 endpoint 不得內嵌憑證（design 決定 2、3；新-低-5、新-中-2）。

    以 monkeypatch PROVIDERS 的假 provider 表為測樣（不是假政策檔）；組裝點的
    行為（Gateway 不啟動）在 tests/test_main。
    """

    def _endpoint(self, endpoint) -> str:
        table = {"opencode-zen": _FakeEndpointProvider("fake", endpoint)}
        original = lgwd_providers.PROVIDERS
        lgwd_providers.PROVIDERS = table
        try:
            lgwd_providers.validate_endpoints()
        finally:
            lgwd_providers.PROVIDERS = original

    def test_出廠endpoint與null都通過(self):
        # 沒有丟錯即可
        self._endpoint("https://opencode.ai/zen/v1")
        self._endpoint("https://openrouter.ai/api")
        self._endpoint(None)  # claude-subscription：agent 直連

    def test_query憑證樣式被拒(self):
        for endpoint in ("https://host/?key=abc", "https://host/?token=abc",
                         "https://host/?api_key=abc", "https://host/?access_token=abc"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    self._endpoint(endpoint)

    def test_userinfo樣式被拒(self):
        for endpoint in ("https://user:pass@host", "https://me@host"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    self._endpoint(endpoint)

    def test_error_message_carries_no_values(self):
        # 固定字串：不回顯 endpoint 的值（值永不進例外）
        canary = "canary-3b1f-endpoint-value-never-real"
        try:
            self._endpoint(f"https://host/?key={canary}")
        except ValueError as exc:
            self.assertNotIn(canary, str(exc))
            self.assertNotIn("host", str(exc))
        else:
            self.fail("endpoint with credentials must be rejected")


class RoleAndPrivacyPolicyTests(PolicyValidationTests):
    pass
    def test_privacy缺省為public(self):
        policy = self._reload()
        for sub in policy.subscriptions.values():
            self.assertEqual(sub.privacy, "public")

    def test_roles缺省為空(self):
        policy = self._reload()
        self.assertEqual(policy.roles, {})

    def test_部分角色表可載入(self):
        obj = _good_policy()
        obj["roles"] = {"impl": {"floor": "public", "order": ["openrouter"]}}
        policy = self._reload(obj)
        self.assertEqual(set(policy.roles), {"impl"})

    def test_floor缺漏拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"order": ["openrouter"]}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_角色物件多餘鍵拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "real", "order": ["openrouter"], "x": 1}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_order空拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "real", "order": []}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_order重複拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "real", "order": ["openrouter", "openrouter"]}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_order_id不存在拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "real", "order": ["nope"]}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_privacy非法拒收(self):
        obj = _good_policy()
        obj["subscriptions"]["openrouter"]["privacy"] = "private"
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_floor值非法_private拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "private", "order": ["openrouter"]}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_floor值非法_null拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": None, "order": ["openrouter"]}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_roles非object拒收(self):
        obj = _good_policy()
        obj["roles"] = ["PMO"]
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_entry非object拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": "x"}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_order缺拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "real"}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_order元素非字串拒收(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"floor": "real", "order": [1]}}
        with self.assertRaises(PolicyError):
            self._reload(obj)

    def test_出廠政策privacy與roles整張表(self):
        policy = load_policy(str(REPO_ROOT / "policy" / "default.json"))
        self.assertEqual(
            {name: policy.subscriptions[name].privacy for name in policy.subscriptions},
            {
                "claude-personal": "real",
                "opencode-zen": "public",
                "opencode-go": "general",
                "openrouter": "public",
                "groq": "public",
                "ollama-cloud": "real",
                "antigravity": "public",
            },
        )
        self.assertEqual(
            {role: (rp.floor, rp.order) for role, rp in policy.roles.items()},
            {
                "PMO": ("real", ("claude-personal", "ollama-cloud")),
                "PM": ("real", ("claude-personal", "ollama-cloud")),
                "impl": ("public", ("opencode-zen", "opencode-go", "openrouter",
                                    "ollama-cloud", "claude-personal")),
                "rev": ("public", ("openrouter", "opencode-zen", "opencode-go",
                                   "ollama-cloud", "claude-personal")),
                "qa": ("public", ("opencode-zen", "opencode-go", "openrouter",
                                  "ollama-cloud")),
            },
        )
        # groq 不在任何 order
        self.assertFalse(any("groq" in r.order for r in policy.roles.values()))
        # real floor 角色的 order 只含 real 訂閱
        real_subs = {name for name, s in policy.subscriptions.items()
                     if s.privacy == "real"}
        for role, rp in policy.roles.items():
            if rp.floor != "real":
                continue
            for sid in rp.order:
                self.assertIn(sid, real_subs, (role, sid))

    def test_驗證順序_privacy先於訂閱id(self):
        obj = _good_policy()
        obj["subscriptions"]["claude-personal"]["privacy"] = "private"
        obj["subscriptions"]["nope"] = _sub(10, ("opencode",))
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("privacy", str(ctx.exception))

    def test_驗證順序_訂閱id先於角色鍵(self):
        obj = _good_policy()
        obj["subscriptions"]["nope"] = _sub(10, ("opencode",))
        obj["roles"] = {"foo": {"floor": "public", "order": ["openrouter"]}}
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("unknown key", str(ctx.exception))

    def test_驗證順序_角色鍵先於floor(self):
        obj = _good_policy()
        obj["roles"] = {"foo": {"floor": "public", "order": ["openrouter"]},
                        "PMO": {"order": ["openrouter"]}}
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("unknown role", str(ctx.exception))

    def test_驗證順序_floor先於order(self):
        obj = _good_policy()
        obj["roles"] = {"PMO": {"order": ["openrouter"]}}
        with self.assertRaises(PolicyError) as ctx:
            self._reload(obj)
        self.assertIn("floor is required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
