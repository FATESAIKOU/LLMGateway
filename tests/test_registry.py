"""lgwd/registry.py 的測試：真 RegistryService（暫存 SQLite、假 key 檔、FakeClock）。

測試名稱照 spec subscription-registry／key-vault 的 Scenario（v5：until-only）。key 檔的值全是假 canary。
"""

import datetime as _dt
import json
import logging
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lgwd import providers as lgwd_providers
from lgwd.clock import FakeClock
from lgwd.errors import ApiError
from lgwd.keys import KEY_NAMES, KeyStore
from lgwd.policy import UsageSetting, load_policy
from lgwd.providers import AnthropicProvider, UsageDeps
from lgwd.registry import RegistryService
from lgwd.usage import FetchResult, UsageService, failure
from tests.usage_fakes import FakeFetcher, windows

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = str(REPO_ROOT / "policy" / "default.json")
UTC = _dt.timezone.utc

# 假資料：絕不是真的 key
CANARY = {
    "claude-personal": "canary-registry-personal-0001",
    "claude-company": "canary-registry-company-0002",
    "openrouter": "canary-registry-openrouter-0003",
    "opencode-zen": "canary-registry-zen-0004",
    # #77：opencode-go 與 zen 共用同一個鍵名，值只有一份
    "opencode-go": "canary-registry-zen-0004",
    "groq": "canary-registry-groq-0006",
    "ollama-cloud": "canary-registry-ollama-0005",
}


def key_text(values: dict[str, str]) -> str:
    """組 key 檔；同一個鍵名只寫一行（opencode-zen／opencode-go 共用 Zen 的鍵名）。"""
    by_name: dict[str, str] = {}
    for sub, value in values.items():
        by_name.setdefault(KEY_NAMES[sub], value)
    lines = ["# 測試用假 key"]
    for name, value in by_name.items():
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def _by_id(items):
    return {item["id"]: item for item in items}


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.keys_path = self.root / "keys.env"
        self.keys_path.write_text(key_text(CANARY), encoding="utf-8")
        self.db_path = str(self.root / "llm.db")
        self.clock = FakeClock()
        self.policy = load_policy(POLICY_PATH)
        self.keys = KeyStore(str(self.keys_path))
        self.service = RegistryService(self.db_path, self.policy, self.keys, self.clock)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    # --- 小工具 ---
    def list_items(self):
        payload = self.service.list_subscriptions()
        self.assertEqual(payload["schema"], 1)
        return payload["subscriptions"]

    def get(self, subscription, agent):
        return self.service.get_subscription(subscription, agent)

    def resolve(self, agent, provider, model=None, subscription=None, exclude=None):
        return self.service.resolve(agent, provider, model=model,
                                    subscription=subscription, exclude=exclude)

    def cooldown(self, subscription, until):
        return self.service.register_cooldown(subscription, {"until": until})

    def cooldown_raw(self, subscription, body):
        return self.service.register_cooldown(subscription, body)

    def assert_api_error(self, code, err, fn, *args, **kwargs):
        with self.assertRaises(ApiError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status, code)
        self.assertEqual(ctx.exception.code, err)
        return ctx.exception

    def db_bytes(self) -> bytes:
        return self.root.joinpath("llm.db").read_bytes()


class JsonSchemaTest(RegistryCase):
    def test_Claude訂閱的描述(self):
        item = self.get("claude-personal", "claude-code")
        self.assertEqual(item["provider"], "anthropic")
        self.assertEqual(item["kind"], "claude-subscription")
        self.assertEqual(item["agents"], ["claude-code"])
        self.assertIsNone(item["endpoint"])
        self.assertIsNone(item["cooldown"])
        blob = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("OAUTH_TOKEN", blob)  # 不帶環境變數名
        self.assertNotIn("ANTHROPIC_", blob)
        self.assertEqual(
            set(item),
            {"schema", "id", "provider", "kind", "agents", "priority", "models",
             "endpoint", "key", "cooldown", "model_cooldowns", "available"},
        )

    def test_provider欄位(self):
        expected = {
            "claude-personal": ("anthropic", "claude-subscription", None),
            "openrouter": ("openrouter", "api-key", "https://openrouter.ai/api"),
            "opencode-zen": ("opencode-zen", "api-key", "https://opencode.ai/zen/v1"),
            "opencode-go": ("opencode-go", "api-key",
                            "https://opencode.ai/inference/go/openai/v1"),
            "groq": ("groq", "api-key", "https://api.groq.com/openai/v1"),
            "ollama-cloud": ("ollama-cloud", "api-key", "https://ollama.com"),
        }
        items = _by_id(self.list_items())
        for sub, (provider, kind, endpoint) in expected.items():
            with self.subTest(sub=sub):
                self.assertEqual(items[sub]["provider"], provider)
                self.assertEqual(items[sub]["kind"], kind)
                self.assertEqual(items[sub]["endpoint"], endpoint)
        self.assertEqual(set(items), set(expected))

    def test_endpoint出廠值(self):
        # spec subscription-registry：endpoint 是 provider 的根位址（值以
        # lgwd/providers.py 為準）；Claude 訂閱是 null（由 agent 直連）。
        expected = {
            "claude-personal": None,
            "openrouter": "https://openrouter.ai/api",
            "ollama-cloud": "https://ollama.com",
            "opencode-zen": "https://opencode.ai/zen/v1",
            "opencode-go": "https://opencode.ai/inference/go/openai/v1",
            "groq": "https://api.groq.com/openai/v1",
        }
        agents = {
            "claude-personal": "claude-code",
            "openrouter": "opencode",
            "ollama-cloud": "opencode",
            "opencode-zen": "opencode",
            "opencode-go": "opencode",
            "groq": "opencode",
        }
        listed = _by_id(self.list_items())
        self.assertEqual(set(listed), set(expected))
        for sub, endpoint in expected.items():
            with self.subTest(subscription=sub):
                self.assertEqual(listed[sub]["endpoint"], endpoint)
                # get 也一樣（含 key 的那條路）
                self.assertEqual(self.get(sub, agents[sub])["endpoint"], endpoint)

    def test_能不能用(self):
        items = _by_id(self.list_items())
        self.assertTrue(items["claude-personal"]["available"])
        # key 沒部署 → false；opencode-go 與 zen 共用同一個鍵名，一起變 false
        self.keys_path.write_text(
            key_text({k: v for k, v in CANARY.items()
                      if k not in ("opencode-zen", "opencode-go")}),
            encoding="utf-8",
        )
        self.keys.reload()
        items = _by_id(self.list_items())
        self.assertFalse(items["opencode-zen"]["available"])
        self.assertFalse(items["opencode-go"]["available"])
        # 有未來的 until → false
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        self.assertFalse(_by_id(self.list_items())["claude-personal"]["available"])

    def test_models欄位(self):
        items = _by_id(self.list_items())
        self.assertEqual(items["openrouter"]["models"]["allow"], ["*:free", "openrouter/free"])
        self.assertEqual(items["openrouter"]["models"]["default"], "openrouter/free")
        self.assertEqual(items["opencode-zen"]["models"]["allow"], ["*-free", "big-pickle"])
        self.assertEqual(
            items["opencode-zen"]["models"]["default"],
            "mimo-v2.6-flash-free",
        )
        self.assertEqual(items["opencode-go"]["models"]["allow"], ["beam-free"])
        self.assertEqual(
            items["opencode-go"]["models"]["default"],
            "beam-free",
        )
        # #88：groq 的 allow 只放免費層可用的模型（不含 "*"）
        self.assertEqual(
            items["groq"]["models"]["allow"],
            ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
        )
        self.assertEqual(items["groq"]["models"]["default"], "openai/gpt-oss-120b")

    def test_get與list照出廠值帶出models_default(self):
        # get 與 list 的回覆逐筆帶出出廠的 models.default（#50 起 claude-personal
        # 預設 claude-sonnet-5-5）。
        expected = {
            "claude-personal": "claude-sonnet-5-5",
            "opencode-zen": "mimo-v2.6-flash-free",
            "opencode-go": "beam-free",
            "groq": "openai/gpt-oss-120b",
            "openrouter": "openrouter/free",
            "ollama-cloud": "deepseek-v4.1-flash",
        }
        agents = {
            "claude-personal": "claude-code",
            "opencode-zen": "opencode",
            "opencode-go": "opencode",
            "groq": "opencode",
            "openrouter": "opencode",
            "ollama-cloud": "opencode",
        }
        listed = _by_id(self.list_items())
        self.assertEqual(set(listed), set(expected))
        for sub, default in expected.items():
            with self.subTest(subscription=sub):
                for source, payload in (("list", listed[sub]),
                                        ("get", self.get(sub, agents[sub]))):
                    self.assertIn("default", payload["models"],
                                  f"{sub} 的 {source} 缺 default 欄")
                    self.assertEqual(payload["models"]["default"], default,
                                     f"{sub} 的 {source} 回傳的 default 不對")
                    # JSON 往返後照樣
                    roundtrip = json.loads(json.dumps(payload, ensure_ascii=False))
                    self.assertEqual(roundtrip["models"]["default"], default)
                    if default is None:
                        self.assertIsNone(roundtrip["models"]["default"])
                        self.assertIn('"default": null', json.dumps(payload))


class ModelsDefaultSemanticsTest(RegistryCase):
    """spec subscription-registry「models.default 的語意」的兩個 Scenario。"""

    def _service_with_policy(self, obj) -> RegistryService:
        path = self.root / "policy-under-test.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return RegistryService(self.db_path, load_policy(str(path)),
                               self.keys, self.clock)

    def test_Claude訂閱預設Sonnet(self):
        # #50：出廠政策把 claude-personal 的 models.default 設成 claude-sonnet-5-5
        # （原本是 null＝用 Claude Code 自己的預設）。
        item = self.get("claude-personal", "claude-code")
        self.assertEqual(item["kind"], "claude-subscription")
        self.assertEqual(item["models"]["default"], "claude-sonnet-5-5")
        listed = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(listed["models"]["default"], "claude-sonnet-5-5")

    def test_Claude訂閱default為null時照回null(self):
        # 假政策檔（claude-personal 的 default 改成 null），不動出廠政策檔。
        with open(REPO_ROOT / "policy" / "default.json", encoding="utf-8") as fh:
            obj = json.load(fh)
        obj["subscriptions"]["claude-personal"]["models"]["default"] = None
        service = self._service_with_policy(obj)
        try:
            item = service.get_subscription("claude-personal", "claude-code")
            self.assertIn("default", item["models"])
            self.assertIsNone(item["models"]["default"])
            # JSON 的 null，不是空字串
            self.assertIn('"default": null', json.dumps(item, ensure_ascii=False))
        finally:
            service.close()

    def test_其他kind沒有預設(self):
        # Scenario: 其他 kind 沒有預設—非 claude-subscription 的 default 為
        # null 時服務不代選（照樣回 null），呼叫方自己依非空的 allow 選一個
        # 模型來用，或不用這一筆。用假政策檔（ollama-cloud 的 default 改成
        # null），不動出廠政策檔。
        with open(REPO_ROOT / "policy" / "default.json", encoding="utf-8") as fh:
            obj = json.load(fh)
        obj["subscriptions"]["ollama-cloud"]["models"]["default"] = None
        service = self._service_with_policy(obj)
        try:
            item = service.get_subscription("ollama-cloud", "opencode")
            self.assertEqual(item["kind"], "api-key")
            self.assertNotEqual(item["kind"], "claude-subscription")
            self.assertIn("default", item["models"])
            self.assertIsNone(item["models"]["default"])
            self.assertTrue(item["models"]["allow"])  # 呼叫方有 allow 可選
            listed = _by_id(service.list_subscriptions()["subscriptions"])["ollama-cloud"]
            self.assertIsNone(listed["models"]["default"])
            self.assertIn('"default": null',
                          json.dumps(listed, ensure_ascii=False))
        finally:
            service.close()


class ListTest(RegistryCase):
    def test_不含秘密(self):
        items = self.list_items()
        blob = json.dumps(items, ensure_ascii=False)
        for item in items:
            self.assertNotIn("key", item)
        for value in CANARY.values():
            self.assertNotIn(value, blob)

    def test_依priority排序(self):
        items = self.list_items()
        priorities = [item["priority"] for item in items]
        self.assertEqual(priorities, sorted(priorities))
        self.assertEqual(
            [item["id"] for item in items],
            ["opencode-zen", "opencode-go", "openrouter", "groq", "ollama-cloud",
             "claude-personal"],
        )

    def test_冷卻反映在list(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertFalse(item["available"])
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        # 到期前 1 秒還在冷卻
        self.clock.set(_dt.datetime(2026, 10, 11, 14, 59, 59, tzinfo=UTC))
        self.assertFalse(_by_id(self.list_items())["claude-personal"]["available"])
        # 到期（含相等）自動解除
        self.clock.set(_dt.datetime(2026, 10, 11, 15, 0, 0, tzinfo=UTC))
        item = _by_id(self.list_items())["claude-personal"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["cooldown"])


class GetTest(RegistryCase):
    def test_ClaudeCode拿Claude訂閱(self):
        item = self.get("claude-personal", "claude-code")
        self.assertEqual(item["key"], CANARY["claude-personal"])

    def test_opencode不能拿Claude訂閱(self):
        err = self.assert_api_error(403, "agent_not_allowed", self.get, "claude-personal", "opencode")
        self.assertNotIn("key", err.body())

    def test_冷卻中仍可get(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        item = self.get("claude-personal", "claude-code")
        self.assertFalse(item["available"])
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(item["key"], CANARY["claude-personal"])

    def test_缺agent回400(self):
        for agent in (None, ""):
            with self.subTest(agent=agent):
                err = self.assert_api_error(400, "bad_request", self.get, "claude-personal", agent)
                self.assertEqual(err.body()["missing"], ["agent"])
                self.assertNotIn("detail", err.body())

    def test_不存在的id回404(self):
        self.assert_api_error(404, "not_found", self.get, "ghost", "opencode")

    def test_已移除的claude_company回404(self):
        # #50：claude-company 還在 lgwd 的名單與 key 檔，但不在政策檔 → 404
        err = self.assert_api_error(
            404, "not_found", self.get, "claude-company", "claude-code")
        self.assertNotIn("key", err.body())
        err = self.assert_api_error(
            404, "not_found", self.cooldown, "claude-company",
            "2026-10-11T15:00:00Z")
        self.assertNotIn("key", err.body())

    def test_key沒部署回409(self):
        self.keys_path.write_text(
            key_text({k: v for k, v in CANARY.items() if k != "claude-personal"}),
            encoding="utf-8",
        )
        self.keys.reload()
        err = self.assert_api_error(409, "not_deployed", self.get, "claude-personal", "claude-code")
        self.assertNotIn("key", err.body())

    def test_get_openrouter回傳default模型(self):
        item = self.get("openrouter", "opencode")
        self.assertEqual(item["key"], CANARY["openrouter"])
        self.assertEqual(item["models"]["default"], "openrouter/free")
        self.assertEqual(item["models"]["allow"], ["*:free", "openrouter/free"])
        self.assertEqual(self.get("opencode-zen", "opencode")["key"], CANARY["opencode-zen"])
        # #77：opencode-go 與 zen 共用同一個鍵名，get 回同一個值
        self.assertEqual(self.get("opencode-go", "opencode")["key"], CANARY["opencode-zen"])
        # #88：groq 用自己的鍵名
        self.assertEqual(self.get("groq", "opencode")["key"], CANARY["groq"])
        self.assertEqual(self.get("ollama-cloud", "claude-code")["key"], CANARY["ollama-cloud"])

    def test_ClaudeCode不能拿openrouter(self):
        # 出廠政策（#23）：openrouter 只給 opencode（Claude Code 碰到
        # openrouter/free 會 unrecognized_model）
        err = self.assert_api_error(
            403, "agent_not_allowed", self.get, "openrouter", "claude-code")
        self.assertNotIn("key", err.body())


class RoleCandidatesTest(RegistryCase):
    """spec role-allocation「角色候選端點」的 Scenario（#100 tasks 2.1）。"""

    def role(self, role, agent, data=None):
        return self.service.role_candidates(role, agent, data)

    def ids(self, payload):
        return [row["id"] for row in payload["candidates"]]

    def _service_with(self, obj, usage=None):
        path = self.root / "policy-roles.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return RegistryService(self.db_path, load_policy(str(path)),
                               self.keys, self.clock, usage=usage)

    def _default_policy_obj(self):
        return json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))

    def _restrictive_policy(self):
        """qa 的 order 只有 privacy < real 的 opencode 訂閱（spec「隱私不足時不降級」）。"""
        return {
            "subscriptions": {
                "opencode-zen": _privacy_sub(10, "public"),
                "opencode-go": _privacy_sub(15, "general"),
                "openrouter": _privacy_sub(20, "public",
                                           allow=("*:free", "openrouter/free")),
            },
            "roles": {"qa": {"floor": "public",
                             "order": ["opencode-zen", "opencode-go", "openrouter"]}},
            "until_max_ahead_seconds": 691200,
        }

    # --- 候選與順序 ---

    def test_impl取真資料候選(self):
        payload = self.role("impl", "opencode", "real")
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["role"], "impl")
        self.assertEqual(payload["data"], "real")
        self.assertEqual(payload["agent"], "opencode")
        # order: zen(public) → go(general) → openrouter(public) → ollama(real)
        # → claude-personal(real；只給 claude-code)
        self.assertEqual(self.ids(payload), ["ollama-cloud"])
        self.assertEqual([row["rank"] for row in payload["candidates"]], [1])
        blob = json.dumps(payload, ensure_ascii=False)
        for absent in ("opencode-zen", "opencode-go", "openrouter",
                       "claude-personal", "groq"):
            self.assertNotIn(absent, blob)

    def test_impl取public候選順序照order(self):
        payload = self.role("impl", "opencode", "public")
        self.assertEqual(self.ids(payload),
                         ["opencode-zen", "opencode-go", "openrouter", "ollama-cloud"])
        self.assertEqual([row["rank"] for row in payload["candidates"]], [1, 2, 3, 4])
        self.assertEqual([row["privacy"] for row in payload["candidates"]],
                         ["public", "general", "public", "real"])
        # groq 不在任何 order；claude-personal 不給 opencode
        self.assertNotIn("groq", self.ids(payload))
        self.assertNotIn("claude-personal", self.ids(payload))

    def test_PMO想用較低等級(self):
        payload = self.role("PMO", "claude-code", "public")
        self.assertEqual(payload["data"], "real")  # floor 較嚴
        self.assertEqual(self.ids(payload), ["claude-personal", "ollama-cloud"])

    def test_缺省data取最嚴(self):
        payload = self.role("qa", "opencode")
        self.assertEqual(payload["data"], "real")
        self.assertEqual(self.ids(payload), ["ollama-cloud"])

    def test_rank在篩選後連續(self):
        obj = {
            "subscriptions": {
                "opencode-zen": _privacy_sub(10, "public"),
                "ollama-cloud": _privacy_sub(30, "real"),
                "opencode-go": _privacy_sub(15, "general"),
            },
            "roles": {"qa": {"floor": "public",
                             "order": ["opencode-zen", "ollama-cloud", "opencode-go"]}},
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with(obj)
        try:
            real = service.role_candidates("qa", "opencode", "real")
            self.assertEqual([(r["id"], r["rank"]) for r in real["candidates"]],
                             [("ollama-cloud", 1)])
            general = service.role_candidates("qa", "opencode", "general")
            self.assertEqual([(r["id"], r["rank"]) for r in general["candidates"]],
                             [("ollama-cloud", 1), ("opencode-go", 2)])
        finally:
            service.close()

    def test_冷卻不改候選(self):
        self.cooldown("opencode-zen", "2026-10-11T15:00:00Z")
        self.cooldown_raw("opencode-zen",
                          {"until": "2026-10-11T16:00:00Z", "model": "big-pickle"})
        payload = self.role("impl", "opencode", "public")
        zen = payload["candidates"][0]
        self.assertEqual(zen["id"], "opencode-zen")
        self.assertEqual(zen["rank"], 1)  # 成員與順序不變
        self.assertEqual(zen["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(zen["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T16:00:00Z"}})
        self.assertFalse(zen["available"])
        self.assertEqual(self.ids(payload),
                         ["opencode-zen", "opencode-go", "openrouter", "ollama-cloud"])

    # --- 隱私 gate ---

    def test_隱私不足時不降級(self):
        service = self._service_with(self._restrictive_policy())
        try:
            payload = service.role_candidates("qa", "opencode", "real")
            self.assertEqual(payload["data"], "real")
            self.assertEqual(payload["candidates"], [])
            blob = json.dumps(payload, ensure_ascii=False)
            for absent in ("opencode-zen", "opencode-go", "openrouter"):
                self.assertNotIn(absent, blob)
        finally:
            service.close()

    def test_低等級時高隱私訂閱仍可出現(self):
        # 同一份 policy（zen public、go general、ollama real）：過濾方向是「不低於」
        obj = {
            "subscriptions": {
                "opencode-zen": _privacy_sub(10, "public"),
                "opencode-go": _privacy_sub(15, "general"),
                "ollama-cloud": _privacy_sub(30, "real"),
            },
            "roles": {"qa": {"floor": "public",
                             "order": ["opencode-zen", "opencode-go", "ollama-cloud"]}},
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with(obj)
        try:
            public = service.role_candidates("qa", "opencode", "public")
            self.assertEqual(self.ids(public),
                             ["opencode-zen", "opencode-go", "ollama-cloud"])
            general = service.role_candidates("qa", "opencode", "general")
            self.assertEqual(self.ids(general), ["opencode-go", "ollama-cloud"])
        finally:
            service.close()

    def test_缺省privacy視為public(self):
        obj = {
            "subscriptions": {
                # 沒有 privacy 鍵 → public（fail-closed）
                "ollama-cloud": {
                    "priority": 30,
                    "agents": ["opencode"],
                    "models": {"allow": ["*"], "default": None},
                },
                "opencode-go": _privacy_sub(15, "general"),
            },
            "roles": {"impl": {"floor": "public",
                               "order": ["ollama-cloud", "opencode-go"]}},
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with(obj)
        try:
            public = service.role_candidates("impl", "opencode", "public")
            self.assertEqual(
                [(r["id"], r["privacy"]) for r in public["candidates"]],
                [("ollama-cloud", "public"), ("opencode-go", "general")])
            general = service.role_candidates("impl", "opencode", "general")
            self.assertEqual(self.ids(general), ["opencode-go"])
        finally:
            service.close()

    # --- row 形狀 ---

    def test_row帶provider且不等於id(self):
        payload = self.role("PMO", "claude-code")
        rows = {row["id"]: row for row in payload["candidates"]}
        self.assertEqual(rows["claude-personal"]["provider"], "anthropic")
        self.assertEqual(rows["claude-personal"]["id"], "claude-personal")
        self.assertEqual(rows["ollama-cloud"]["provider"], "ollama-cloud")
        self.assertEqual(set(rows["claude-personal"]),
                         {"id", "provider", "rank", "privacy", "available",
                          "cooldown", "model_cooldowns"})

    def test_不含key與額度(self):
        class ExplodingUsage:
            def usage_for(self, subscription):
                raise AssertionError("roles 不應觸發取用量")

            def source_of(self, subscription):
                raise AssertionError("roles 不應觸發取用量")

        service = self._service_with(self._default_policy_obj(), usage=ExplodingUsage())
        try:
            payload = service.role_candidates("impl", "opencode", "public")
        finally:
            service.close()
        self.assertTrue(payload["candidates"])
        for row in payload["candidates"]:
            self.assertEqual(set(row),
                             {"id", "provider", "rank", "privacy", "available",
                              "cooldown", "model_cooldowns"})
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('"key"', blob)
        self.assertNotIn("usage", blob)
        self.assertNotIn("available_again_at", blob)
        self.assertNotIn("API_KEY", blob)
        for value in CANARY.values():
            self.assertNotIn(value, blob)

    # --- 錯誤 ---

    def test_缺或空agent回400(self):
        for agent in (None, ""):
            with self.subTest(agent=agent):
                err = self.assert_api_error(
                    400, "bad_request", self.service.role_candidates, "impl", agent)
                self.assertEqual(err.body()["missing"], ["agent"])

    def test_agent種類不合法(self):
        for agent in ("clade-code", "claude", "OpenCode"):
            with self.subTest(agent=agent):
                err = self.assert_api_error(
                    400, "bad_request", self.service.role_candidates, "impl", agent)
                self.assertNotIn("missing", err.body())

    def test_data不合法(self):
        for data in ("private", "PUBLIC", "", " real"):
            with self.subTest(data=data):
                err = self.assert_api_error(
                    400, "bad_request", self.service.role_candidates,
                    "impl", "opencode", data)
                self.assertNotIn("candidates", err.body())

    def test_角色未知或不在roles段回404(self):
        # 五個之外
        self.assert_api_error(404, "not_found", self.service.role_candidates,
                              "boss", "opencode")
        self.assert_api_error(404, "not_found", self.service.role_candidates,
                              "pmo", "opencode")
        # 五個之內但政策檔沒寫
        obj = self._default_policy_obj()
        obj["roles"] = {"impl": obj["roles"]["impl"]}
        service = self._service_with(obj)
        try:
            self.assert_api_error(404, "not_found", service.role_candidates,
                                  "PMO", "claude-code")
        finally:
            service.close()

    def test_沒有roles段回404(self):
        obj = self._default_policy_obj()
        del obj["roles"]
        service = self._service_with(obj)
        try:
            for role in ("impl", "PMO", "qa"):
                with self.subTest(role=role):
                    self.assert_api_error(404, "not_found",
                                          service.role_candidates, role, "opencode")
        finally:
            service.close()


def _privacy_sub(priority, privacy, agents=("opencode",), allow=("*",)):
    """假政策的訂閱：指定 privacy（測試用；default null 不參與 roles）。"""
    return {
        "priority": priority,
        "privacy": privacy,
        "agents": list(agents),
        "models": {"allow": list(allow), "default": None},
    }


class CooldownTest(RegistryCase):
    def test_帶恢復時刻(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertFalse(item["available"])
        self.assertEqual(self.cooldown("claude-personal", "2026-10-11T15:00:00Z"), None)

    def test_until型別或格式不對回invalid_until(self):
        for until in ("not-a-time", "2026-99-99", "2026-10-11T15:00:00",
                      123, True, ["2026-10-11T15:00:00Z"]):
            with self.subTest(until=until):
                err = self.assert_api_error(
                    400, "invalid_until", self.cooldown_raw, "claude-personal", {"until": until})
                self.assertNotIn("detail", err.body())
                item = _by_id(self.list_items())["claude-personal"]
                self.assertIsNone(item["cooldown"])  # 冷卻不變

    def test_until在過去或超過八天回until_out_of_range(self):
        # 邊界：過去 1 秒、超過 8 天 1 秒，以及遠過去與遠未來
        cases = [
            ("2026-10-10T23:59:59Z", "過去1秒"),
            ("2026-10-19T00:00:01Z", "超過8天1秒"),
            ("2020-01-01T00:00:00Z", "遠過去"),
            ("2099-01-01T00:00:00Z", "遠未來"),
        ]
        for until, label in cases:
            with self.subTest(label=label, until=until):
                err = self.assert_api_error(
                    400, "until_out_of_range", self.cooldown, "claude-personal", until)
                self.assertNotIn("detail", err.body())
                item = _by_id(self.list_items())["claude-personal"]
                self.assertIsNone(item["cooldown"])

    def test_八天整可以剛好超過不行(self):
        self.cooldown("claude-personal", "2026-10-19T00:00:00Z")  # 剛好 8 天
        self.assertEqual(
            _by_id(self.list_items())["claude-personal"]["cooldown"],
            {"until": "2026-10-19T00:00:00Z"})
        err = self.assert_api_error(
            400, "until_out_of_range", self.cooldown, "claude-personal", "2026-10-19T00:00:01Z")
        self.assertNotIn("detail", err.body())

    def test_剛好現在的時刻可以(self):
        # 不是過去就收；等於現在不是「未來」，登錄後立即不冷卻
        self.cooldown("claude-personal", "2026-10-11T00:00:00Z")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertIsNone(item["cooldown"])
        self.assertTrue(item["available"])

    def test_同一筆重複登錄取較晚的(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        self.cooldown("claude-personal", "2026-10-11T14:00:00Z")  # 較早，不覆蓋
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.cooldown("claude-personal", "2026-10-11T16:00:00Z")  # 較晚，覆蓋
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T16:00:00Z"})
        # 稽核記的是有效的（較晚的）時刻
        rows = self.service._store.audit_all()
        self.assertEqual(
            [r["until"] for r in rows],
            ["2026-10-11T15:00:00Z", "2026-10-11T15:00:00Z", "2026-10-11T16:00:00Z"])

    def test_到時自動解除(self):
        self.cooldown("claude-personal", "2026-10-18T00:00:00Z")
        self.assertFalse(_by_id(self.list_items())["claude-personal"]["available"])
        self.clock.set(_dt.datetime(2026, 10, 18, 0, 0, 0, tzinfo=UTC))
        item = _by_id(self.list_items())["claude-personal"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["cooldown"])

    def test_未知欄位忽略(self):
        self.cooldown_raw("claude-personal", {
            "until": "2026-10-11T15:00:00Z", "window": "5h", "resets_at": "x", "extra": 1,
        })
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})

    def test_缺until欄位回bad_request(self):
        for body in ({}, {"window": "5h"}, {"resets_at": "2026-10-11T15:00:00Z"}):
            with self.subTest(body=body):
                err = self.assert_api_error(
                    400, "bad_request", self.cooldown_raw, "claude-personal", body)
                self.assertEqual(err.body()["missing"], ["until"])
                self.assertNotIn("detail", err.body())

    def test_帶時差的時刻正規化成UTC(self):
        self.cooldown("claude-personal", "2026-10-11T23:00:00+08:00")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})

    def test_不存在的id回404(self):
        self.assert_api_error(
            404, "not_found", self.cooldown, "ghost", "2026-10-11T15:00:00Z")

    def test_冷卻跨重啟(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        self.service.close()
        reopened = RegistryService(self.db_path, self.policy, self.keys, self.clock)
        try:
            item = _by_id(reopened.list_subscriptions()["subscriptions"])["claude-personal"]
            self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        finally:
            reopened.close()


class ModelCooldownTest(RegistryCase):
    """spec「登錄冷卻」的 model 與 store 的模型冷卻表（design 決定 1、6）。"""

    def model_cooldowns(self, sub):
        return _by_id(self.list_items())[sub]["model_cooldowns"]

    def test_只冷卻一個模型(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"})
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["model_cooldowns"],
                         {"mimo-v2.6-flash-free": {"until": "2026-10-11T15:00:00Z"}})
        self.assertIsNone(item["cooldown"])   # 整把沒被動到
        self.assertTrue(item["available"])    # 模型冷卻不改 available

    def test_opencode_go的iceman登錄冷卻被拒(self):
        # #95：iceman-contributor-free 不再涵蓋，登錄冷卻 model 也應被拒（400 bad_request）
        err = self.assert_api_error(
            400, "bad_request", self.cooldown_raw, "opencode-go",
            {"until": "2026-10-11T15:00:00Z", "model": "iceman-contributor-free"})
        self.assertEqual(self.model_cooldowns("opencode-go"), {})

    def test_同一模型重複登錄取較晚的(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T14:00:00Z", "model": "big-pickle"})   # 較早，不覆蓋
        self.assertEqual(self.model_cooldowns("opencode-zen"),
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "big-pickle"})   # 較晚，覆蓋
        self.assertEqual(self.model_cooldowns("opencode-zen"),
                         {"big-pickle": {"until": "2026-10-11T16:00:00Z"}})
        # 稽核記的是有效的（較晚的）時刻
        self.assertEqual(
            [r["until"] for r in self.service._store.audit_all()],
            ["2026-10-11T15:00:00Z", "2026-10-11T15:00:00Z", "2026-10-11T16:00:00Z"])

    def test_兩個模型的冷卻互不影響(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "mimo-v2.6-flash-free"})
        self.assertEqual(self.model_cooldowns("opencode-zen"), {
            "big-pickle": {"until": "2026-10-11T15:00:00Z"},
            "mimo-v2.6-flash-free": {"until": "2026-10-11T16:00:00Z"},
        })

    def test_整把與模型並存(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "big-pickle"})
        self.cooldown("opencode-zen", "2026-10-11T15:00:00Z")
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(item["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T16:00:00Z"}})
        # 反向：先有整把再登錄模型，整把不被縮短或刪除
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T14:00:00Z", "model": "mimo-v2.6-flash-free"})
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(item["model_cooldowns"], {
            "big-pickle": {"until": "2026-10-11T16:00:00Z"},
            "mimo-v2.6-flash-free": {"until": "2026-10-11T14:00:00Z"},
        })

    def test_model不在allow回400(self):
        # 不是 403 model_not_allowed（那是 resolve 的政策拒絕碼）
        err = self.assert_api_error(400, "bad_request", self.cooldown_raw, "opencode-zen",
                                    {"until": "2026-10-11T15:00:00Z",
                                     "model": "muse-spark-1.3"})
        self.assertNotIn("detail", err.body())
        self.assertEqual(self.model_cooldowns("opencode-zen"), {})

    def test_model帶前綴回400(self):
        for model in ("opencode/big-pickle",
                      "opencode/glm-5.3-flash-free",   # 被 *-free 涵蓋，只有前綴規則擋得到
                      "claude-code/anything"):
            with self.subTest(model=model):
                err = self.assert_api_error(
                    400, "bad_request", self.cooldown_raw, "opencode-zen",
                    {"until": "2026-10-11T15:00:00Z", "model": model})
                self.assertNotIn("detail", err.body())
                self.assertEqual(self.model_cooldowns("opencode-zen"), {})

    def test_model大小寫不符回400(self):
        # _covered 是大小寫敏感的 fnmatch；Gateway 不正規化
        self.assert_api_error(400, "bad_request", self.cooldown_raw, "opencode-zen",
                              {"until": "2026-10-11T15:00:00Z", "model": "Big-Pickle"})
        self.assertEqual(self.model_cooldowns("opencode-zen"), {})

    def test_model是萬用字元回400(self):
        for model in ("*-free", "big?pickle", "[b]ig-pickle"):
            with self.subTest(model=model):
                self.assert_api_error(400, "bad_request", self.cooldown_raw, "opencode-zen",
                                      {"until": "2026-10-11T15:00:00Z", "model": model})
                self.assertEqual(self.model_cooldowns("opencode-zen"), {})

    def test_model超過200字元回400(self):
        ok = "m" * 200
        too_long = "m" * 201
        err = self.assert_api_error(
            400, "bad_request", self.cooldown_raw, "claude-personal",
            {"until": "2026-10-11T15:00:00Z", "model": too_long})
        self.assertNotIn("detail", err.body())
        self.assertEqual(self.model_cooldowns("claude-personal"), {})
        self.cooldown_raw("claude-personal",
                          {"until": "2026-10-11T15:00:00Z", "model": ok})
        self.assertEqual(self.model_cooldowns("claude-personal"),
                         {ok: {"until": "2026-10-11T15:00:00Z"}})

    def test_model是null時記整把(self):
        self.cooldown_raw("opencode-zen", {"until": "2026-10-11T15:00:00Z", "model": None})
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(item["model_cooldowns"], {})
        self.assertIsNone(self.service._store.audit_all()[0]["model"])

    def test_model形狀不對回400(self):
        for model in ("", 123, ["x"], {"m": 1}, True):
            with self.subTest(model=model):
                err = self.assert_api_error(
                    400, "bad_request", self.cooldown_raw, "opencode-zen",
                    {"until": "2026-10-11T15:00:00Z", "model": model})
                self.assertNotIn("detail", err.body())
                self.assertEqual(self.model_cooldowns("opencode-zen"), {})
                self.assertEqual(
                    _by_id(self.list_items())["opencode-zen"]["model_cooldowns"], {})

    def test_未知欄位仍忽略(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle",
            "window": "5h", "resets_at": "x", "extra": 1,
        })
        self.assertEqual(self.model_cooldowns("opencode-zen"),
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})

    def test_未知id先於model的400(self):
        # 查無 404 先於 model 的 400（沿用現行程式的順序）
        self.assert_api_error(
            404, "not_found", self.cooldown_raw, "ghost",
            {"until": "2026-10-11T15:00:00Z", "model": "opencode/big-pickle"})

    def test_壞的until先於壞的model(self):
        self.assert_api_error(
            400, "invalid_until", self.cooldown_raw, "opencode-zen",
            {"until": "not-a-time", "model": "opencode/big-pickle"})
        self.assertEqual(self.model_cooldowns("opencode-zen"), {})
        self.assert_api_error(
            400, "until_out_of_range", self.cooldown_raw, "opencode-zen",
            {"until": "2026-10-10T23:59:59Z", "model": "opencode/big-pickle"})

    def test_模型冷卻跨重啟(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.service.close()
        reopened = RegistryService(self.db_path, self.policy, self.keys, self.clock)
        try:
            item = _by_id(reopened.list_subscriptions()["subscriptions"])["opencode-zen"]
            self.assertEqual(item["model_cooldowns"],
                             {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})
        finally:
            reopened.close()

    def test_store層的模型冷卻(self):
        # 2.1 的 store 層：UPSERT 只在較晚時更新、查詢回該把全部列
        store = self.service._store
        with store.txn() as cur:
            store.cooldown_set_model(cur, "opencode-zen", "big-pickle",
                                     "2026-10-11T15:00:00Z")
            self.assertEqual(
                store.cooldown_get_model(cur, "opencode-zen", "big-pickle")["until"],
                "2026-10-11T15:00:00Z")
            store.cooldown_set_model(cur, "opencode-zen", "big-pickle",
                                     "2026-10-11T14:00:00Z")   # 較早，不覆蓋
            self.assertEqual(
                store.cooldown_get_model(cur, "opencode-zen", "big-pickle")["until"],
                "2026-10-11T15:00:00Z")
            store.cooldown_set_model(cur, "opencode-zen", "mimo-v2.6-flash-free",
                                     "2026-10-11T16:00:00Z")
            rows = store.model_cooldowns_of(cur, "opencode-zen")
            self.assertEqual([(r["model"], r["until"]) for r in rows],
                             [("big-pickle", "2026-10-11T15:00:00Z"),
                              ("mimo-v2.6-flash-free", "2026-10-11T16:00:00Z")])
            self.assertEqual(store.model_cooldowns_of(cur, "ollama-cloud"), [])
            self.assertIsNone(
                store.cooldown_get_model(cur, "opencode-zen", "not-a-model"))
            # 整把那張表一行都沒動
            self.assertIsNone(store.cooldown_get(cur, "opencode-zen"))

    def test_稽核記模型冷卻(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"})
        rows = self.service._store.audit_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "cooldown")
        self.assertEqual(row["model"], "mimo-v2.6-flash-free")
        self.assertEqual(row["until"], "2026-10-11T15:00:00Z")
        self.assertIsNone(row["agent"])
        self.assertNotIn("window", row)
        # 沒有 cooldown_release 這種 action
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "big-pickle"})
        self.assertEqual(
            [r["action"] for r in self.service._store.audit_all()],
            ["cooldown", "cooldown"])


class ModelCooldownListTest(RegistryCase):
    """spec「list」／「共通 JSON schema」的 model_cooldowns（design 決定 5）。"""

    def test_list列出各模型與整把冷卻(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "mimo-v2.6-flash-free"})
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["cooldown"])
        self.assertEqual(item["model_cooldowns"], {
            "big-pickle": {"until": "2026-10-11T15:00:00Z"},
            "mimo-v2.6-flash-free": {"until": "2026-10-11T16:00:00Z"},
        })
        # 推過期：只剩還沒過的那一個
        self.clock.set(_dt.datetime(2026, 10, 11, 15, 0, 0, tzinfo=UTC))
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["model_cooldowns"],
                         {"mimo-v2.6-flash-free": {"until": "2026-10-11T16:00:00Z"}})
        # 再登錄整把 → 兩個欄位同時出現
        self.clock.set(_dt.datetime(2026, 10, 11, 14, 0, 0, tzinfo=UTC))
        self.cooldown("opencode-zen", "2026-10-11T17:00:00Z")
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T17:00:00Z"})
        self.assertEqual(item["model_cooldowns"], {
            "big-pickle": {"until": "2026-10-11T15:00:00Z"},
            "mimo-v2.6-flash-free": {"until": "2026-10-11T16:00:00Z"},
        })
        self.assertFalse(item["available"])   # 只受整把影響

    def test_模型冷卻的形狀(self):
        # 沒有時是 {}（欄位恆在）
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["model_cooldowns"], {})
        self.assertIn("model_cooldowns", item)
        # 兩個都過期 → {}
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.clock.set(_dt.datetime(2026, 10, 11, 15, 0, 0, tzinfo=UTC))
        self.assertEqual(_by_id(self.list_items())["opencode-zen"]["model_cooldowns"], {})

    def test_模型冷卻不改available(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["cooldown"])
        self.assertEqual(item["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})
        self.assertEqual(item["schema"], 1)   # 不 bump

    def test_模型id可含斜線與冒號(self):
        self.cooldown_raw("ollama-cloud", {
            "until": "2026-10-11T15:00:00Z", "model": "a:b/c"})
        self.cooldown_raw("openrouter", {
            "until": "2026-10-11T16:00:00Z", "model": "openrouter/free"})
        items = _by_id(self.list_items())
        self.assertEqual(items["ollama-cloud"]["model_cooldowns"],
                         {"a:b/c": {"until": "2026-10-11T15:00:00Z"}})
        self.assertEqual(items["openrouter"]["model_cooldowns"],
                         {"openrouter/free": {"until": "2026-10-11T16:00:00Z"}})
        # 不影響 models 的內容
        self.assertEqual(items["openrouter"]["models"]["allow"],
                         ["*:free", "openrouter/free"])

    def test_get也帶模型冷卻但沒有usage(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        item = self.get("opencode-zen", "opencode")
        self.assertEqual(item["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})
        self.assertNotIn("usage", item)
        self.assertNotIn("available_again_at", item)


class AuditTest(RegistryCase):
    def test_get留下紀錄(self):
        self.get("claude-personal", "claude-code")
        rows = self.service._store.audit_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "get")
        self.assertEqual(row["subscription"], "claude-personal")
        self.assertEqual(row["agent"], "claude-code")
        self.assertEqual(row["ts"], "2026-10-11T00:00:00Z")
        self.assertIsNone(row["until"])
        self.assertNotIn("window", row)  # 窗口欄位沒了

    def test_登錄冷卻留下紀錄(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        rows = self.service._store.audit_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "cooldown")
        self.assertEqual(row["until"], "2026-10-11T15:00:00Z")
        self.assertIsNone(row["agent"])
        self.assertNotIn("window", row)

    def test_稽核只追加且沒有key(self):
        self.get("claude-personal", "claude-code")
        self.get("openrouter", "opencode")
        self.cooldown("openrouter", "2026-10-18T00:00:00Z")
        rows = self.service._store.audit_all()
        self.assertEqual([r["action"] for r in rows], ["get", "get", "cooldown"])
        # 資料庫檔案（含所有表）沒有任何 key 的值
        raw = self.db_bytes()
        for value in CANARY.values():
            self.assertNotIn(value.encode(), raw)
        blob = json.dumps(rows, ensure_ascii=False)
        for value in CANARY.values():
            self.assertNotIn(value, blob)
        self.assertNotIn("client", blob)  # 不記客戶端


class KeyRotationTest(RegistryCase):
    def test_換key(self):
        self.keys_path.write_text(
            key_text(dict(CANARY, **{"claude-personal": "canary-rotated-9999"})),
            encoding="utf-8",
        )
        self.keys.reload()
        self.assertEqual(self.get("claude-personal", "claude-code")["key"],
                         "canary-rotated-9999")
        self.assertNotIn("canary-rotated-9999", json.dumps(self.list_items()))


class _FakeApiKeyProvider(lgwd_providers.CredentialProvider):
    """假 provider 表用的 api-key provider（兩把 id 映射到同一個 provider 時用）。"""

    provider = "fake-api"
    kind = "api-key"
    endpoint = "https://fake.example/api"
    opencode_provider_id = "fake-api"


class ResolveTest(RegistryCase):
    """spec subscription-registry「resolve」的需求（design 決定 1～4；選把集中 registry）。"""

    def _service_with_policy(self, obj) -> RegistryService:
        path = self.root / "policy-under-resolve.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return RegistryService(self.db_path, load_policy(str(path)),
                               self.keys, self.clock)

    def _policy_with_company(self):
        """真政策為底，把 claude-company 加回去（假政策；出廠政策已移除它）。

        lgwd 仍認得 claude-company，選把規則（冷卻、exclude、最早 until）
        要有兩把才測得到。personal 的 priority 設回 10（出廠是 40），讓
        「personal 優先、company 次之」成立，Scenario 才照原意。
        """
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        obj["subscriptions"]["claude-personal"]["priority"] = 10
        obj["subscriptions"]["claude-company"] = {
            "priority": 20,
            "agents": ["claude-code"],
            "models": {"allow": ["*"], "default": None},
        }
        return obj

    def _patch_two_api_key_subscriptions(self):
        """把 Claude 兩把映射成同一個非 claude-subscription 的 provider。

        「同 provider 兩把 api-key」在現行政策下不可達（claude 兩把恆為
        claude-subscription），spec 的 Scenario 也明說用假政策；假政策需要
        provider 表同時支援（policy loader 的鍵集是固定的），所以 monkeypatch
        PROVIDERS。
        """
        original = lgwd_providers.PROVIDERS
        lgwd_providers.PROVIDERS = {
            "claude-personal": _FakeApiKeyProvider(),
            "claude-company": _FakeApiKeyProvider(),
            "openrouter": original["openrouter"],
            "opencode-zen": original["opencode-zen"],
            "opencode-go": original["opencode-go"],
            "groq": original["groq"],
            "ollama-cloud": original["ollama-cloud"],
        }
        self.addCleanup(setattr, lgwd_providers, "PROVIDERS", original)

    # --- resolve 需求的 Scenario ---

    def test_同一把的兩個免費模型(self):
        a = self.resolve("opencode", "opencode-zen", model="mimo-v2.6-flash-free")
        b = self.resolve("opencode", "opencode-zen", model="big-pickle")
        for payload, model in ((a, "mimo-v2.6-flash-free"), (b, "big-pickle")):
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(payload["env_schema"], 1)
            self.assertEqual(payload["meta"]["subscription"], "opencode-zen")
            self.assertEqual(payload["meta"]["model"], model)
        self.assertNotEqual(a["env"], b["env"])  # 模型設定不同

    def test_釘住已移除的公司那一把(self):
        # #50：claude-company 已不在出廠政策檔 → 404
        err = self.assert_api_error(
            404, "not_found", self.resolve, "claude-code", "anthropic",
            subscription="claude-company")
        self.assertNotIn("env", err.body())

    def test_釘住的那把冷卻中(self):
        # 兩把的假政策：釘住的 personal 冷卻中，仍回 200＋附冷卻資訊（不代換成 company）
        service = self._service_with_policy(self._policy_with_company())
        try:
            service.register_cooldown(
                "claude-personal", {"until": "2026-10-11T15:00:00Z"})
            payload = service.resolve("claude-code", "anthropic",
                                      subscription="claude-personal")
            self.assertEqual(payload["meta"]["subscription"], "claude-personal")
            self.assertEqual(payload["meta"]["cooldown"],
                             {"until": "2026-10-11T15:00:00Z"})
            self.assertEqual(payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                             CANARY["claude-personal"])
            self.assertNotIn("error", payload)
        finally:
            service.close()

    def test_釘住的subscription與provider不一致(self):
        err = self.assert_api_error(
            400, "bad_request", self.resolve, "claude-code", "anthropic",
            subscription="opencode-zen")
        self.assertNotIn("env", err.body())

    def test_釘住與排除同時給(self):
        err = self.assert_api_error(
            400, "bad_request", self.resolve, "claude-code", "anthropic",
            subscription="claude-personal", exclude="ollama-cloud")
        self.assertNotIn("env", err.body())

    def test_排除唯一一把回no_candidate(self):
        # claude-company 已不在出廠政策檔，anthropic 只剩 claude-personal；
        # exclude 掉它就沒有候選
        err = self.assert_api_error(
            409, "no_candidate", self.resolve, "claude-code", "anthropic",
            exclude="claude-personal")
        self.assertNotIn("env", err.body())
        # 被排除的那把冷卻中也不回 cooling：no_candidate 是 exclude 造成的
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        err = self.assert_api_error(
            409, "no_candidate", self.resolve, "claude-code", "anthropic",
            exclude="claude-personal")
        self.assertNotIn("env", err.body())
        self.assertNotIn("until", err.body())  # 不回 cooling（沒有恢復時刻）

    def test_換把時排除剛耗盡的那一把(self):
        service = self._service_with_policy(self._policy_with_company())
        try:
            payload = service.resolve(
                "claude-code", "anthropic", exclude="claude-personal")
            self.assertEqual(payload["meta"]["subscription"], "claude-company")
        finally:
            service.close()

    def test_缺必填(self):
        cases = [
            ((None, "opencode-zen"), ["agent"]),
            ((None, None), ["agent", "provider"]),
            (("opencode", None), ["provider"]),
        ]
        for (agent, provider), missing in cases:
            with self.subTest(agent=agent, provider=provider):
                err = self.assert_api_error(400, "bad_request", self.resolve, agent, provider)
                self.assertEqual(err.body()["missing"], sorted(missing))
                self.assertNotIn("env", err.body())
        # service 層只對 agent／provider 做 falsy 檢查，空字串也是沒給；model／subscription 的空值歸一化在 HTTP 層（test_http）
        err = self.assert_api_error(400, "bad_request", self.resolve, "", "opencode-zen")
        self.assertEqual(err.body()["missing"], ["agent"])

    def test_未知的agent(self):
        err = self.assert_api_error(400, "bad_request", self.resolve, "foo", "opencode-zen")
        self.assertNotEqual(err.status, 403)  # 不是 403 agent_not_allowed

    def test_meta只帶有資訊的欄位(self):
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(
            sorted(payload["meta"]),
            ["agent", "cooldown", "endpoint", "kind", "model", "model_cooldowns",
             "models", "priority", "provider", "subscription", "usage"],
        )
        self.assertIsNone(payload["meta"]["cooldown"])
        self.assertEqual(payload["meta"]["model_cooldowns"], {})
        self.assertNotIn("available", payload["meta"])
        self.assertNotIn("available_again_at", payload["meta"])

    def test_值不外洩(self):
        # 憑證樣式掃描稽核、list 回覆與 DB；endpoint 的值只掃憑證樣式（providers 已擋）
        # 模型冷卻走同一條紀律（task 5.2）：list／get／稽核／SQLite 都不帶 key 的值
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.resolve("claude-code", "anthropic")
        get_item = dict(self.get("opencode-zen", "opencode"))
        get_item.pop("key")
        rows = self.service._store.audit_all()
        blobs = [
            json.dumps(rows, ensure_ascii=False),
            json.dumps(self.list_items(), ensure_ascii=False),
            json.dumps(get_item, ensure_ascii=False),
        ]
        for blob in blobs:
            for value in CANARY.values():
                self.assertNotIn(value, blob)
            for mark in ("key=", "token=", "api_key=", "access_token=", "@"):
                self.assertNotIn(mark, blob)
        raw = self.db_bytes()
        for value in CANARY.values():
            self.assertNotIn(value.encode(), raw)

    # --- resolve 的選把規則的 Scenario ---

    def test_唯一一把冷卻中(self):
        # #50：出廠政策 anthropic 只剩 claude-personal；冷卻只是提示，仍回 200＋附資訊
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        payload = self.resolve("claude-code", "anthropic")
        self.assertEqual(payload["meta"]["subscription"], "claude-personal")
        self.assertEqual(payload["meta"]["cooldown"],
                         {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                         CANARY["claude-personal"])

    def test_同provider兩把一把冷卻中(self):
        # 冷卻不影響選把：仍選 priority 較小的 personal（10），不換成 company（20）
        service = self._service_with_policy(self._policy_with_company())
        try:
            service.register_cooldown(
                "claude-personal", {"until": "2026-10-11T15:00:00Z"})
            payload = service.resolve("claude-code", "anthropic")
            self.assertEqual(payload["meta"]["subscription"], "claude-personal")
            self.assertEqual(payload["meta"]["priority"], 10)
            self.assertEqual(payload["meta"]["cooldown"],
                             {"until": "2026-10-11T15:00:00Z"})
        finally:
            service.close()

    def test_全部候選冷卻中(self):
        service = self._service_with_policy(self._policy_with_company())
        try:
            service.register_cooldown(
                "claude-personal", {"until": "2026-10-11T16:00:00Z"})
            service.register_cooldown(
                "claude-company", {"until": "2026-10-11T15:00:00Z"})
            payload = service.resolve("claude-code", "anthropic")
            # 200、選 priority 最小者（personal 10），附它自己的冷卻（不是最早的）
            self.assertEqual(payload["meta"]["subscription"], "claude-personal")
            self.assertEqual(payload["meta"]["cooldown"],
                             {"until": "2026-10-11T16:00:00Z"})
            self.assertTrue(payload["env"])
            self.assertNotIn("error", payload)
        finally:
            service.close()

    def test_model不在allow(self):
        err = self.assert_api_error(
            403, "model_not_allowed", self.resolve,
            "opencode", "opencode-zen", model="muse-spark-1.3")
        self.assertNotIn("env", err.body())

    def test_帶provider前綴的model(self):
        # model 不是供應商 id（opencode/big-pickle）；Gateway 不正規化前綴
        err = self.assert_api_error(
            403, "model_not_allowed", self.resolve,
            "opencode", "opencode-zen", model="opencode/big-pickle")
        self.assertNotIn("env", err.body())

    def test_供應商自己的模型id(self):
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(payload["meta"]["model"], "big-pickle")  # 供應商 id，不帶 opencode/ 前綴

    def test_opencode_go的env是go的endpoint與provider_id(self):
        # #77：Go 走自己的 provider id 與 endpoint；key 與 zen 共用同一個值
        # #95：iceman-contributor-free 不再 allow，改用 beam-free
        payload = self.resolve("opencode", "opencode-go",
                               model="beam-free")
        self.assertEqual(payload["meta"]["subscription"], "opencode-go")
        self.assertEqual(payload["meta"]["provider"], "opencode-go")
        self.assertEqual(payload["meta"]["endpoint"],
                         "https://opencode.ai/inference/go/openai/v1")
        env = payload["env"]
        self.assertEqual(env["OPENCODE_PROVIDER_ID"], "opencode-go")
        self.assertEqual(env["OPENCODE_API_KEY"], CANARY["opencode-zen"])
        self.assertEqual(env["OPENCODE_MODEL"], "beam-free")
        self.assertEqual(env["OPENCODE_SMALL_MODEL"], "beam-free")
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", payload)

    def test_opencode_go的iceman模型被拒(self):
        # #95：iceman-contributor-free 不再涵蓋（以授權訓練 prompt 換免費、不進輪替）
        err = self.assert_api_error(
            403, "model_not_allowed", self.resolve,
            "opencode", "opencode-go", model="iceman-contributor-free")
        self.assertNotIn("env", err.body())

    def test_opencode_go的預設模型(self):
        payload = self.resolve("opencode", "opencode-go")
        self.assertEqual(payload["meta"]["model"], "beam-free")

    def test_opencode_go_allow以外的模型被拒(self):
        # kimi-k3 不符合 *-free（Issue #77 的驗收）
        err = self.assert_api_error(
            403, "model_not_allowed", self.resolve,
            "opencode", "opencode-go", model="kimi-k3")
        self.assertNotIn("env", err.body())

    def test_opencode_go只給opencode(self):
        err = self.assert_api_error(
            403, "agent_not_allowed", self.resolve, "claude-code", "opencode-go")
        self.assertNotIn("env", err.body())

    def test_groq的env是groq的endpoint與provider_id(self):
        # #88：groq 走自己的 provider id 與 endpoint，key 是自己的 GROQ_API_KEY
        payload = self.resolve("opencode", "groq", model="openai/gpt-oss-20b")
        self.assertEqual(payload["meta"]["subscription"], "groq")
        self.assertEqual(payload["meta"]["provider"], "groq")
        self.assertEqual(payload["meta"]["endpoint"],
                         "https://api.groq.com/openai/v1")
        env = payload["env"]
        self.assertEqual(env["OPENCODE_PROVIDER_ID"], "groq")
        self.assertEqual(env["OPENCODE_API_KEY"], CANARY["groq"])
        # 供應商 id 含 "/"（openai/gpt-oss-20b）照原樣進模型變數，不切前綴
        self.assertEqual(env["OPENCODE_MODEL"], "openai/gpt-oss-20b")
        self.assertEqual(env["OPENCODE_SMALL_MODEL"], "openai/gpt-oss-20b")
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", payload)

    def test_groq的預設模型(self):
        payload = self.resolve("opencode", "groq")
        self.assertEqual(payload["meta"]["model"], "openai/gpt-oss-120b")

    def test_groq_allow以外的模型被拒(self):
        # Free Plan 的 rate limits 表上沒有的 id（企業方案才有的 llama-3.1-8b-instant）
        err = self.assert_api_error(
            403, "model_not_allowed", self.resolve,
            "opencode", "groq", model="llama-3.1-8b-instant")
        self.assertNotIn("env", err.body())

    def test_groq只給opencode(self):
        err = self.assert_api_error(
            403, "agent_not_allowed", self.resolve, "claude-code", "groq")
        self.assertNotIn("env", err.body())

    def test_groq沒有usage來源(self):
        # Groq 沒有公布查剩餘配額的端點 → 沿用預設（not_applicable）
        payload = self.resolve("opencode", "groq")
        self.assertEqual(payload["meta"]["usage"]["status"], "not_applicable")

    def test_未知provider(self):
        self.assert_api_error(404, "not_found", self.resolve, "opencode", "NoSuchProvider")

    def test_provider在政策檔沒有subscription(self):
        # provider 存在於 PROVIDERS，但政策檔沒有它的一把 → 404（步驟 2）
        obj = {
            "subscriptions": {"opencode-zen": _sub_like(10, "m", ["*"], agents=("opencode",))},
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            self.assert_api_error(404, "not_found", service.resolve, "claude-code", "anthropic")
        finally:
            service.close()

    def test_agent不被provider允許(self):
        # 步驟 3：有候選但沒有一把允許這個 agent → 403（釘住時只看該把）
        for kwargs in ({}, {"subscription": "claude-personal"}):
            with self.subTest(subscription=kwargs.get("subscription")):
                err = self.assert_api_error(
                    403, "agent_not_allowed", self.resolve, "opencode", "anthropic", **kwargs)
                self.assertNotIn("env", err.body())

    def test_釘住未知id(self):
        err = self.assert_api_error(
            404, "not_found", self.resolve, "claude-code", "anthropic",
            subscription="not-a-subscription")
        self.assertNotIn("env", err.body())
        # 形狀錯誤（同時給 exclude）先於未知 id 的 404
        self.assert_api_error(
            400, "bad_request", self.resolve, "claude-code", "anthropic",
            subscription="not-a-subscription", exclude="claude-personal")

    def test_exclude未知id忽略(self):
        payload = self.resolve("claude-code", "anthropic", exclude="not-a-subscription")
        self.assertEqual(payload["meta"]["subscription"], "claude-personal")

    def test_最高priority的候選推不出模型選下一個(self):
        self._patch_two_api_key_subscriptions()
        obj = {
            "subscriptions": {
                "claude-personal": _sub_like(10, None, ["*"]),
                "claude-company": _sub_like(20, "m2", ["*"]),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            payload = service.resolve("claude-code", "fake-api")  # model 省略
            self.assertEqual(payload["meta"]["subscription"], "claude-company")
            self.assertEqual(payload["meta"]["model"], "m2")
        finally:
            service.close()

    def test_釘住那把推不出模型不代換(self):
        # 同 provider 的另一把可推導也不代換（釘住在第 1 步先限縮候選）
        self._patch_two_api_key_subscriptions()
        obj = {
            "subscriptions": {
                "claude-personal": _sub_like(10, None, ["*"]),
                "claude-company": _sub_like(20, "m2", ["*"]),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            err = self.assert_api_error(
                400, "no_model", service.resolve,
                "claude-code", "fake-api", subscription="claude-personal")
            self.assertNotIn("env", err.body())
        finally:
            service.close()

    def test_同priority同provider以id排序(self):
        # 步驟 8：priority 最小；同分比 id（claude-company < claude-personal）
        self._patch_two_api_key_subscriptions()
        obj = {
            "subscriptions": {
                "claude-personal": _sub_like(10, "m", ["*"]),
                "claude-company": _sub_like(10, "m", ["*"]),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            payload = service.resolve("claude-code", "fake-api")
            self.assertEqual(payload["meta"]["subscription"], "claude-company")
            self.assertEqual(payload["meta"]["provider"], "fake-api")
            self.assertEqual(payload["meta"]["model"], "m")
        finally:
            service.close()

    # --- 冷卻只是提示（design 決定 2、3） ---

    def test_A模型冷卻時指定A仍給key並附冷卻資訊(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"})
        payload = self.resolve("opencode", "opencode-zen",
                               model="mimo-v2.6-flash-free")
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["meta"]["subscription"], "opencode-zen")
        self.assertEqual(payload["meta"]["model"], "mimo-v2.6-flash-free")
        self.assertEqual(payload["env"]["OPENCODE_MODEL"], "mimo-v2.6-flash-free")
        self.assertIsNone(payload["meta"]["cooldown"])
        self.assertEqual(
            payload["meta"]["model_cooldowns"],
            {"mimo-v2.6-flash-free": {"until": "2026-10-11T15:00:00Z"}})

    def test_A模型冷卻時指定B模型仍選得到同一把(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"})
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(payload["meta"]["subscription"], "opencode-zen")
        self.assertEqual(payload["meta"]["model"], "big-pickle")
        self.assertEqual(payload["env"]["OPENCODE_MODEL"], "big-pickle")
        # 換模型不會讓 A 的冷卻資訊消失
        self.assertEqual(
            payload["meta"]["model_cooldowns"],
            {"mimo-v2.6-flash-free": {"until": "2026-10-11T15:00:00Z"}})

    def test_整把冷卻時resolve仍給key並附資訊(self):
        self.cooldown("opencode-zen", "2026-10-11T15:00:00Z")
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "big-pickle"})
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(payload["meta"]["subscription"], "opencode-zen")
        self.assertEqual(payload["meta"]["cooldown"],
                         {"until": "2026-10-11T15:00:00Z"})
        # 整把與模型兩層都附，互不覆蓋
        self.assertEqual(payload["meta"]["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T16:00:00Z"}})
        self.assertEqual(payload["env"]["OPENCODE_MODEL"], "big-pickle")

    def test_不帶model的登錄仍記整把(self):
        self.cooldown("opencode-zen", "2026-10-11T15:00:00Z")
        item = _by_id(self.list_items())["opencode-zen"]
        self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
        self.assertFalse(item["available"])
        self.assertEqual(item["model_cooldowns"], {})
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(payload["meta"]["cooldown"],
                         {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(payload["meta"]["model_cooldowns"], {})
        # 稽核的 model 欄是 null
        row = self.service._store.audit_all()[0]
        self.assertEqual(row["action"], "cooldown")
        self.assertIsNone(row["model"])

    def test_冷卻不影響選把的priority(self):
        self._patch_two_api_key_subscriptions()
        obj = {
            "subscriptions": {
                "claude-personal": _sub_like(10, "m", ["*"]),
                "claude-company": _sub_like(20, "m", ["*"]),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            service.register_cooldown(
                "claude-personal", {"until": "2026-10-11T15:00:00Z"})
            payload = service.resolve("claude-code", "fake-api")
            # priority 較小者冷卻中仍選它，不是選沒冷卻的較低 priority
            self.assertEqual(payload["meta"]["subscription"], "claude-personal")
            self.assertEqual(payload["meta"]["priority"], 10)
            self.assertEqual(payload["meta"]["cooldown"],
                             {"until": "2026-10-11T15:00:00Z"})
        finally:
            service.close()

    def test_同priority時冷卻不是排序鍵(self):
        self._patch_two_api_key_subscriptions()
        obj = {
            "subscriptions": {
                "claude-personal": _sub_like(10, "m", ["*"]),
                "claude-company": _sub_like(10, "m", ["*"]),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            # id 較小者（claude-company）冷卻中 → 仍選它（排序只看 priority／id）
            service.register_cooldown(
                "claude-company", {"until": "2026-10-11T15:00:00Z"})
            payload = service.resolve("claude-code", "fake-api")
            self.assertEqual(payload["meta"]["subscription"], "claude-company")
            self.assertEqual(payload["meta"]["cooldown"],
                             {"until": "2026-10-11T15:00:00Z"})
        finally:
            service.close()

    def test_不指定model時default冷卻仍200(self):
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "mimo-v2.6-flash-free"})
        payload = self.resolve("opencode", "opencode-zen")
        self.assertEqual(payload["meta"]["model"], "mimo-v2.6-flash-free")
        self.assertEqual(
            payload["meta"]["model_cooldowns"],
            {"mimo-v2.6-flash-free": {"until": "2026-10-11T15:00:00Z"}})

    def test_沒有模型可對時仍200(self):
        # claude-subscription＋default null：meta.model 為 null，模型冷卻只是資訊
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        obj["subscriptions"]["claude-personal"]["models"]["default"] = None
        service = self._service_with_policy(obj)
        try:
            service.register_cooldown("claude-personal", {
                "until": "2026-10-11T15:00:00Z", "model": "claude-sonnet-5-5"})
            payload = service.resolve("claude-code", "anthropic")
            self.assertIsNone(payload["meta"]["model"])
            self.assertIsNone(payload["meta"]["cooldown"])
            self.assertEqual(payload["meta"]["model_cooldowns"],
                             {"claude-sonnet-5-5": {"until": "2026-10-11T15:00:00Z"}})
            self.assertTrue(payload["env"])
        finally:
            service.close()

    def test_模型未知時冷卻只是資訊(self):
        # 釘住的那把：整把沒冷卻、只有某模型冷卻、model 省略（推導出的模型不是它）
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        payload = self.resolve("opencode", "opencode-zen",
                               model="mimo-v2.6-flash-free")
        self.assertEqual(payload["meta"]["model"], "mimo-v2.6-flash-free")
        self.assertEqual(payload["meta"]["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})

    def test_meta含冷卻且不含available(self):
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        meta = payload["meta"]
        self.assertIn("cooldown", meta)
        self.assertIn("model_cooldowns", meta)
        self.assertIsNone(meta["cooldown"])
        self.assertEqual(meta["model_cooldowns"], {})
        self.assertNotIn("available", meta)
        self.assertNotIn("available_again_at", meta)
        self.assertEqual(payload["schema"], 1)

    def test_釘住的key未部署(self):
        self.keys_path.write_text(
            key_text({k: v for k, v in CANARY.items() if k != "claude-personal"}),
            encoding="utf-8",
        )
        self.keys.reload()
        err = self.assert_api_error(
            409, "not_deployed", self.resolve,
            "claude-code", "anthropic", subscription="claude-personal")
        self.assertNotIn("env", err.body())

    def test_全部候選未部署(self):
        self.keys_path.write_text("# 沒有任何 key\n", encoding="utf-8")
        self.keys.reload()
        err = self.assert_api_error(409, "not_deployed", self.resolve,
                                    "claude-code", "anthropic")
        self.assertNotIn("env", err.body())

    def test_resolve每把key只讀一次(self):
        # 替身模擬 keysync 在「已部署檢查」與「取值」之間換檔：deployed() 被呼叫過
        # 之後，get() 就回 None。resolve 只讀一次 get()，所以不會組出 null 憑證。
        class _FlipKeys:
            def __init__(self, values):
                self._values = values
                self.reads = 0
                self.deployed_calls = 0

            def get(self, subscription):
                self.reads += 1
                if self.deployed_calls:
                    return None
                return self._values[subscription]

            def deployed(self, subscription):
                self.deployed_calls += 1
                return subscription in self._values

        keys = _FlipKeys(CANARY)
        service = RegistryService(self.db_path, self.policy, keys, self.clock)
        try:
            payload = service.resolve(
                "claude-code", "anthropic", subscription="claude-personal")
        finally:
            service.close()
        self.assertEqual(
            payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"], CANARY["claude-personal"])
        self.assertEqual(keys.reads, 1)
        self.assertEqual(keys.deployed_calls, 0)

    # --- env 的組成的 Scenario ---

    def test_claude訂閱帶預設模型變數(self):
        # #50：claude-personal 有 default（claude-sonnet-5-5），env 多模型變數
        payload = self.resolve("claude-code", "anthropic", subscription="claude-personal")
        self.assertEqual(sorted(payload["env"]),
                         ["ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                          "CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual(payload["env"]["ANTHROPIC_MODEL"], "claude-sonnet-5-5")
        self.assertEqual(payload["env"]["ANTHROPIC_SMALL_FAST_MODEL"], "claude-sonnet-5-5")
        self.assertEqual(payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"], CANARY["claude-personal"])

    def test_opencode的兩個模型_片段(self):
        a = self.resolve("opencode", "opencode-zen", model="mimo-v2.6-flash-free")
        b = self.resolve("opencode", "opencode-zen", model="big-pickle")
        for payload, model in ((a, "mimo-v2.6-flash-free"), (b, "big-pickle")):
            env = payload["env"]
            self.assertEqual(sorted(env),
                             ["OPENCODE_API_KEY", "OPENCODE_MODEL",
                              "OPENCODE_PROVIDER_ID", "OPENCODE_SMALL_MODEL"])
            self.assertEqual(env["OPENCODE_PROVIDER_ID"], "opencode")
            self.assertEqual(env["OPENCODE_API_KEY"], CANARY["opencode-zen"])
            self.assertEqual(env["OPENCODE_MODEL"], model)
            self.assertEqual(env["OPENCODE_SMALL_MODEL"], model)
            blob = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", blob)
            self.assertNotIn("baseURL", blob)

    def test_opencode的ollama_env(self):
        # opencode 的片段同一套鍵；endpoint 不抄進 env（provider 根位址由 sidecar 決定接法）
        payload = self.resolve("opencode", "ollama-cloud", model="deepseek-v4.1-flash")
        env = payload["env"]
        self.assertEqual(env["OPENCODE_PROVIDER_ID"], "ollama-cloud")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", payload)

    def test_claude_code的api_key形狀(self):
        payload = self.resolve("claude-code", "ollama-cloud", model="deepseek-v4.1-flash")
        env = payload["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], CANARY["ollama-cloud"])
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")  # 刻意佔位
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://ollama.com")
        self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-v4.1-flash")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "deepseek-v4.1-flash")

    # --- model 省略時的語意的 Scenario ---

    def test_claude_subscription的預設(self):
        payload = self.resolve("claude-code", "anthropic", subscription="claude-personal")
        self.assertEqual(payload["meta"]["model"], "claude-sonnet-5-5")  # #50 的政策預設
        self.assertEqual(payload["env"]["ANTHROPIC_MODEL"], "claude-sonnet-5-5")

    def test_claude_subscription的預設為null(self):
        # 假政策（default null）：agent 自己的預設，不帶 ANTHROPIC_MODEL
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        obj["subscriptions"]["claude-personal"]["models"]["default"] = None
        service = self._service_with_policy(obj)
        try:
            payload = service.resolve("claude-code", "anthropic")
            self.assertIsNone(payload["meta"]["model"])
            self.assertNotIn("ANTHROPIC_MODEL", payload["env"])
        finally:
            service.close()

    def test_api_key的預設(self):
        payload = self.resolve("opencode", "opencode-zen")
        self.assertEqual(payload["meta"]["model"], "mimo-v2.6-flash-free")

    def test_api_key的預設_allow第一個具體項(self):
        # default 為 null 時用 allow 第一個不含萬用字元的樣式（design 決定 4）
        obj = {
            "subscriptions": {
                "ollama-cloud": _sub_like(
                    10, None, ["*-free", "big-pickle"], agents=("opencode",)),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            payload = service.resolve("opencode", "ollama-cloud")
            self.assertEqual(payload["meta"]["model"], "big-pickle")
        finally:
            service.close()

    def test_推不出模型_假政策(self):
        # 非 claude-subscription 且 default null、allow 全萬用字元（現行政策不可達）
        obj = {
            "subscriptions": {
                "ollama-cloud": _sub_like(10, None, ["*"], agents=("opencode",)),
            },
            "until_max_ahead_seconds": 691200,
        }
        service = self._service_with_policy(obj)
        try:
            err = self.assert_api_error(400, "no_model", service.resolve,
                                        "opencode", "ollama-cloud")
            self.assertNotIn("env", err.body())
        finally:
            service.close()

    # --- 稽核紀錄（MODIFIED）的 Scenario ---

    def test_resolve留下紀錄(self):
        self.resolve("opencode", "opencode-zen", model="big-pickle")
        rows = self.service._store.audit_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "resolve")
        self.assertEqual(row["subscription"], "opencode-zen")
        self.assertEqual(row["agent"], "opencode")
        self.assertEqual(row["provider"], "opencode-zen")
        self.assertEqual(row["model"], "big-pickle")
        self.assertEqual(row["ts"], "2026-10-11T00:00:00Z")
        blob = json.dumps(rows, ensure_ascii=False)
        for value in CANARY.values():
            self.assertNotIn(value, blob)

    def test_resolve失敗不記稽核(self):
        self.assert_api_error(
            403, "model_not_allowed", self.resolve,
            "opencode", "opencode-zen", model="muse-spark-1.3")
        self.assertEqual(self.service._store.audit_all(), [])

    def test_resolve留紀錄model為null(self):
        # claude-subscription 用 agent 預設（政策 default 為 null）：model 欄位是
        # null，不是空字串
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        obj["subscriptions"]["claude-personal"]["models"]["default"] = None
        service = self._service_with_policy(obj)
        try:
            service.resolve("claude-code", "anthropic")
            row = service._store.audit_all()[0]
            self.assertIsNone(row["model"])
            self.assertEqual(row["provider"], "anthropic")
        finally:
            service.close()

    def test_get與cooldown的稽核照舊(self):
        self.get("claude-personal", "claude-code")
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        rows = self.service._store.audit_all()
        self.assertIsNone(rows[0]["provider"])  # get（過渡期）照舊只記 agent
        self.assertIsNone(rows[0]["model"])
        self.assertIsNone(rows[1]["provider"])  # 冷卻照舊記 until
        self.assertIsNone(rows[1]["model"])
        self.assertEqual(rows[1]["until"], "2026-10-11T15:00:00Z")


def _sub_like(priority, default, allow, agents=("claude-code",)):
    return {
        "priority": priority,
        "agents": list(agents),
        "models": {"allow": list(allow), "default": default},
    }


class AuditMigrationTest(RegistryCase):
    """spec 稽核紀錄 Scenario「舊 DB 沒有新欄位」＋退版（舊 lgwd 讀新 DB 不受影響）。"""

    # v5（gateway-v0）的 audit 沒有 provider／model
    _V5_SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldowns (
    subscription TEXT PRIMARY KEY,
    until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    subscription TEXT NOT NULL,
    action TEXT NOT NULL,
    agent TEXT,
    until TEXT
);
"""

    def test_從v5schema的DB檔啟動並寫入resolve(self):
        self.service.close()
        Path(self.db_path).unlink()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(self._V5_SCHEMA)
            conn.execute(
                "INSERT INTO audit (ts, subscription, action, agent)"
                " VALUES ('2026-10-11T00:00:00Z', 'claude-personal', 'get', 'claude-code')")
            conn.commit()
        finally:
            conn.close()

        reopened = RegistryService(self.db_path, self.policy, self.keys, self.clock)
        try:
            reopened.resolve("opencode", "opencode-zen", model="big-pickle")
            rows = reopened._store.audit_all()
            self.assertEqual([r["action"] for r in rows], ["get", "resolve"])
            # 舊資料的新欄位是 null
            self.assertIsNone(rows[0]["provider"])
            self.assertIsNone(rows[0]["model"])
            self.assertEqual(rows[1]["provider"], "opencode-zen")
            self.assertEqual(rows[1]["model"], "big-pickle")

            # 舊 lgwd 的 SELECT 指定欄位；讀到多出的欄位不受影響（退版支援）
            conn = sqlite3.connect(self.db_path)
            try:
                legacy = conn.execute(
                    "SELECT seq, ts, subscription, action, agent, until"
                    " FROM audit ORDER BY seq").fetchall()
            finally:
                conn.close()
            self.assertEqual(len(legacy), 2)
        finally:
            reopened.close()

    def test_舊DB沒有新表也能啟動(self):
        # 2.2：v5 的 DB（只有 cooldowns＋audit）→ 新 lgwd 起得來、新表被建立、舊冷卻照舊
        self.service.close()
        Path(self.db_path).unlink()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(self._V5_SCHEMA)
            conn.execute(
                "INSERT INTO cooldowns (subscription, until)"
                " VALUES ('claude-personal', '2026-10-11T15:00:00Z')")
            conn.execute(
                "INSERT INTO audit (ts, subscription, action, agent)"
                " VALUES ('2026-10-11T00:00:00Z', 'claude-personal', 'get', 'claude-code')")
            conn.commit()
        finally:
            conn.close()

        reopened = RegistryService(self.db_path, self.policy, self.keys, self.clock)
        try:
            # 新表被建立（三欄）
            conn = sqlite3.connect(self.db_path)
            try:
                columns = [row[1] for row in
                           conn.execute("PRAGMA table_info(model_cooldowns)")]
            finally:
                conn.close()
            self.assertEqual(columns, ["subscription", "model", "until"])
            # 舊的整把冷卻照舊生效；模型冷卻表是空的
            item = _by_id(reopened.list_subscriptions()["subscriptions"])["claude-personal"]
            self.assertEqual(item["cooldown"], {"until": "2026-10-11T15:00:00Z"})
            self.assertFalse(item["available"])
            self.assertEqual(item["model_cooldowns"], {})
            # 舊稽核照舊讀得到，新欄位為 null
            rows = reopened._store.audit_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "get")
            self.assertIsNone(rows[0]["provider"])
            self.assertIsNone(rows[0]["model"])
            # 新程式可以寫入模型冷卻
            reopened.register_cooldown("opencode-zen", {
                "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
            rows = reopened._store.audit_all()
            self.assertEqual(rows[-1]["model"], "big-pickle")
        finally:
            reopened.close()

    def test_退版讀新DB不受影響(self):
        # 2.2：新 DB 有模型列 → 用舊程式的 SQL 讀，仍是一把一列、舊欄位照舊
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.cooldown_raw("opencode-zen", {
            "until": "2026-10-11T16:00:00Z", "model": "mimo-v2.6-flash-free"})
        conn = sqlite3.connect(self.db_path)
        try:
            legacy_cooldowns = conn.execute(
                "SELECT subscription, until FROM cooldowns").fetchall()
            legacy_audit = conn.execute(
                "SELECT seq, ts, subscription, action, agent, until"
                " FROM audit ORDER BY seq").fetchall()
        finally:
            conn.close()
        # 整把仍是一把一列（模型列在另一張表，舊程式看不到）
        self.assertEqual(legacy_cooldowns,
                         [("claude-personal", "2026-10-11T15:00:00Z")])
        self.assertEqual([row[3] for row in legacy_audit],
                         ["cooldown", "cooldown", "cooldown"])
        self.assertEqual(legacy_audit[1][5], "2026-10-11T15:00:00Z")

SHAPE = {"status", "source", "fetched_at", "stale", "reason", "session", "weekly"}


class UsageRegistryCase(RegistryCase):
    """真 RegistryService＋真 UsageService＋假 fetcher（#60 tasks 6.x）。"""

    def setUp(self):
        super().setUp()
        self.ollama = FakeFetcher(base=self.clock.now())
        self.claude = FakeFetcher(enabled=False, base=self.clock.now())
        self.claude.source = "fake-claude"
        self.openrouter = FakeFetcher(base=self.clock.now())
        self.openrouter.source = "fake-openrouter"
        self.build()
        self.addCleanup(self.usage.stop)

    def build(self, policy=None, **settings):
        self.usage = UsageService(
            self.clock,
            fetchers={"ollama-cloud": self.ollama, "claude-personal": self.claude,
                      "openrouter": self.openrouter},
            settings=settings,
            key_of=lambda s: self.keys.get(s), deployed=self.keys.deployed)
        self.service.close()
        self.service = RegistryService(self.db_path, policy or self.policy, self.keys,
                                       self.clock, usage=self.usage)

    def settle(self):
        self.assertTrue(self.usage.wait_idle())

    def item(self, sub):
        return _by_id(self.list_items())[sub]


class ListUsageTest(UsageRegistryCase):
    def test_每筆都有usage與available_again_at(self):
        for item in self.list_items():
            with self.subTest(item["id"]):
                self.assertEqual(set(item["usage"]), SHAPE)
                self.assertIn("available_again_at", item)
        self.assertEqual(self.item("opencode-zen")["usage"],
                         {"status": "not_applicable", "source": None, "fetched_at": None,
                          "stale": None, "reason": None, "session": None, "weekly": None})
        self.settle()
        self.assertEqual(self.item("openrouter")["usage"]["status"], "ok")
        self.assertEqual(self.item("opencode-go")["usage"]["status"], "not_applicable")
        self.assertEqual(self.item("groq")["usage"]["status"], "not_applicable")

    def test_schema仍為1且既有欄位不變(self):
        item = self.item("ollama-cloud")
        self.assertEqual(item["schema"], 1)
        self.assertEqual(set(item) - {"usage", "available_again_at"},
                         {"schema", "id", "provider", "kind", "agents", "priority", "models",
                          "endpoint", "cooldown", "model_cooldowns", "available"})

    def test_第一次查詢_not_fetched_yet_下一次看到新值(self):
        first = self.item("ollama-cloud")["usage"]
        self.assertEqual((first["status"], first["reason"]), ("unknown", "not_fetched_yet"))
        self.settle()
        second = self.item("ollama-cloud")["usage"]
        self.assertEqual(second["status"], "ok")
        self.assertEqual(self.ollama.calls, 1)

    def test_Claude預設disabled且不觸發_真的providers掛點(self):
        # 預設關閉由 UsageService settings 守（不依政策段）；出廠已開另測
        fetcher = AnthropicProvider().make_usage_fetcher(UsageDeps())
        self.assertFalse(fetcher.default_enabled)
        self.usage = UsageService(self.clock,
                                  settings={"claude-personal": UsageSetting(enabled=False)},
                                  fetchers={"claude-personal": fetcher},
                                  key_of=self.keys.get, deployed=self.keys.deployed)
        self.service.close()
        self.service = RegistryService(self.db_path, self.policy, self.keys,
                                       self.clock, usage=self.usage)
        got = self.item("claude-personal")["usage"]
        self.assertEqual((got["status"], got["reason"], got["source"]),
                         ("unknown", "disabled", "claude-statusline"))
        self.assertTrue(self.usage.wait_idle())
        self.assertEqual(self.usage._threads, [])       # 沒有任何取用量被觸發

    def test_政策開啟Claude後觸發(self):
        self.build(**{"claude-personal": UsageSetting(enabled=True)})
        self.assertEqual(self.item("claude-personal")["usage"]["reason"], "not_fetched_yet")
        self.settle()
        self.assertEqual(self.claude.calls, 1)

    def test_key沒部署_no_key且不觸發(self):
        self.keys_path.write_text(key_text({**CANARY, "ollama-cloud": ""}), encoding="utf-8")
        self.keys.reload()
        item = self.item("ollama-cloud")
        self.assertEqual((item["usage"]["status"], item["usage"]["reason"]), ("unknown", "no_key"))
        self.assertIsNone(item["available_again_at"])
        self.settle()
        self.assertEqual(self.ollama.calls, 0)

    def test_key沒部署但快取有舊值_不回舊值且不觸發(self):
        self.item("ollama-cloud"); self.settle()
        self.assertEqual(self.item("ollama-cloud")["usage"]["status"], "ok")
        self.keys_path.write_text(key_text({**CANARY, "ollama-cloud": ""}), encoding="utf-8")
        self.keys.reload()
        got = self.item("ollama-cloud")["usage"]
        self.assertEqual((got["status"], got["reason"], got["session"], got["weekly"]),
                         ("unknown", "no_key", None, None))
        self.settle()
        self.assertEqual(self.ollama.calls, 1)

    def test_disabled優先於no_key(self):
        self.build(**{"ollama-cloud": UsageSetting(enabled=False)})
        self.keys_path.write_text(key_text({**CANARY, "ollama-cloud": ""}), encoding="utf-8")
        self.keys.reload()
        self.assertEqual(self.item("ollama-cloud")["usage"]["reason"], "disabled")

    def _fill(self, session, weekly):
        self.ollama.outcome = FetchResult(windows=windows(session=session, weekly=weekly,
                                                          base=self.clock.now()))
        self.item("ollama-cloud"); self.settle()

    def test_available_again_at_額度用完且available仍true(self):
        reset = self.clock.now() + _dt.timedelta(days=1)
        self._fill((50.0, None), (0.0, reset))
        item = self.item("ollama-cloud")
        self.assertEqual(item["available_again_at"], "2026-10-12T00:00:00Z")
        self.assertTrue(item["available"])

    def test_available_again_at_取冷卻與額度中最晚者(self):
        reset = self.clock.now() + _dt.timedelta(hours=2)
        self._fill((0.0, reset), (40.0, None))
        self.cooldown("ollama-cloud", "2026-10-11T05:00:00Z")
        self.assertEqual(self.item("ollama-cloud")["available_again_at"], "2026-10-11T05:00:00Z")
        self.cooldown("ollama-cloud", "2026-10-11T01:00:00Z")      # 較早：取較晚者不變
        self.assertEqual(self.item("ollama-cloud")["available_again_at"], "2026-10-11T05:00:00Z")

    def test_available_again_at_只有冷卻(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        item = self.item("claude-personal")
        self.assertEqual(item["available_again_at"], "2026-10-11T15:00:00Z")
        self.assertFalse(item["available"])

    def test_available_again_at_小數0與unknown不推測(self):
        self._fill((0.2, None), (40.0, None))
        self.assertIsNone(self.item("ollama-cloud")["available_again_at"])
        self.assertIsNone(self.item("opencode-zen")["available_again_at"])

    def test_get不帶新欄位(self):
        item = self.get("ollama-cloud", "opencode")
        self.assertNotIn("usage", item)
        self.assertNotIn("available_again_at", item)
        self.get("ollama-cloud", "opencode")
        self.assertEqual(self.ollama.calls, 0)

    def test_get與登錄冷卻不觸發(self):
        self.cooldown("ollama-cloud", "2026-10-11T15:00:00Z")
        self.get("ollama-cloud", "opencode")
        self.assertEqual(self.ollama.calls, 0)

    def test_取用量卡住時list照回_內容除usage外一致(self):
        self.ollama.gate = threading.Event()
        self.addCleanup(self.ollama.gate.set)
        baseline = [{k: v for k, v in i.items() if k not in ("usage", "available_again_at")}
                    for i in self.list_items()]
        started = time.monotonic()
        items = self.list_items()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([{k: v for k, v in i.items() if k not in ("usage", "available_again_at")}
                          for i in items], baseline)
        self.assertEqual(_by_id(items)["ollama-cloud"]["usage"]["reason"], "not_fetched_yet")

    def test_取用量拋例外時list照回(self):
        self.ollama.outcome = RuntimeError("boom")
        with self.assertLogs("lgwd.usage", level="WARNING"):
            self.list_items(); self.settle()
        got = self.item("ollama-cloud")
        self.assertEqual(got["usage"]["status"], "unknown")
        self.assertTrue(got["available"])

    def test_組裝出錯_該筆internal_其他筆不受影響(self):
        real = self.usage.usage_for

        def broken(sub):
            if sub == "ollama-cloud":
                raise RuntimeError("canary-secret-assemble")
            return real(sub)
        self.usage.usage_for = broken
        with self.assertLogs("lgwd.registry", level="WARNING") as logs:
            items = _by_id(self.list_items())
        self.assertEqual((items["ollama-cloud"]["usage"]["status"], items["ollama-cloud"]["usage"]["reason"]),
                         ("unknown", "internal"))
        self.assertEqual(items["ollama-cloud"]["usage"]["source"], "fake-source")   # 保底也帶 source
        self.assertIsNone(items["ollama-cloud"]["available_again_at"])
        self.assertEqual(items["opencode-zen"]["usage"]["status"], "not_applicable")
        self.assertNotIn("canary-secret", "\n".join(logs.output))


class ResolveUsageTest(UsageRegistryCase):
    def test_meta帶usage與冷卻_不含available與available_again_at(self):
        payload = self.resolve("opencode", "ollama-cloud")
        meta = payload["meta"]
        self.assertEqual(set(meta["usage"]), SHAPE)
        self.assertEqual(meta["usage"]["reason"], "not_fetched_yet")
        for name in ("available", "available_again_at"):
            self.assertNotIn(name, meta)
        # 冷卻資訊是提示，恆在
        self.assertIn("cooldown", meta)
        self.assertIn("model_cooldowns", meta)
        self.settle()
        self.assertEqual(self.resolve("opencode", "ollama-cloud")["meta"]["usage"],
                         self.item("ollama-cloud")["usage"])

    def test_選中沒有用量來源的provider(self):
        meta = self.resolve("opencode", "opencode-zen")["meta"]
        self.assertEqual(meta["usage"]["status"], "not_applicable")

    def test_成功resolve觸發一次_不等(self):
        self.ollama.gate = threading.Event()
        self.addCleanup(self.ollama.gate.set)
        started = time.monotonic()
        for _ in range(5):
            self.resolve("opencode", "ollama-cloud")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(self.ollama.entered.wait(5))
        self.assertEqual(self.ollama.calls, 1)

    def test_非200不組裝usage也不觸發(self):
        # 400 bad_request／404／403 agent_not_allowed／403 model_not_allowed／409 no_candidate／
        # 409 not_deployed／400 no_model：取用量實作都不能被呼叫
        cases = [
            (400, "bad_request", dict(agent="opencode", provider=None)),
            (404, "not_found", dict(agent="opencode", provider="nope")),
            (403, "agent_not_allowed", dict(agent="claude-code", provider="openrouter")),
            (403, "model_not_allowed", dict(agent="opencode", provider="openrouter", model="gpt")),
            (409, "no_candidate", dict(agent="opencode", provider="ollama-cloud",
                                       exclude="ollama-cloud")),
        ]
        for status, code, kw in cases:
            with self.subTest(code=code):
                err = self.assert_api_error(status, code, self.resolve, **kw)
                self.assertNotIn("usage", err.body())
        # 400 no_model：另一份政策（ollama-cloud 沒 default 且 allow 只有萬用字元）
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        obj["subscriptions"]["ollama-cloud"]["models"]["default"] = None
        path = self.root / "p-no-model.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        self.build(policy=load_policy(str(path)))
        err = self.assert_api_error(400, "no_model", self.resolve, "opencode", "ollama-cloud")
        self.assertNotIn("usage", err.body())
        self.build()
        # 409 not_deployed
        self.keys_path.write_text(key_text({**CANARY, "ollama-cloud": ""}), encoding="utf-8")
        self.keys.reload()
        err = self.assert_api_error(409, "not_deployed", self.resolve, "opencode", "ollama-cloud")
        self.assertNotIn("usage", err.body())
        self.settle()
        self.assertEqual(self.ollama.calls, 0)
        self.assertEqual(self.claude.calls, 0)
        # 冷卻不是 409（不再是「非 200」的一員）：整把冷卻仍回 200、照常組裝 usage。
        # 放在最後，因為 200 會真的觸發一次取用量。
        self.keys_path.write_text(key_text(CANARY), encoding="utf-8")
        self.keys.reload()
        self.cooldown("ollama-cloud", "2026-10-11T15:00:00Z")
        payload = self.resolve("opencode", "ollama-cloud")
        self.assertEqual(payload["meta"]["cooldown"],
                         {"until": "2026-10-11T15:00:00Z"})
        self.assertTrue(payload["env"])
        self.assertIn("usage", payload["meta"])
        self.settle()

    def test_用量為0仍選它_不自動冷卻(self):
        self.ollama.outcome = FetchResult(windows=windows(
            session=(0.0, None), weekly=(0.0, None), base=self.clock.now()))
        self.resolve("opencode", "ollama-cloud"); self.settle()
        payload = self.resolve("opencode", "ollama-cloud")
        self.assertEqual(payload["meta"]["subscription"], "ollama-cloud")
        self.assertEqual(payload["meta"]["usage"]["session"]["remaining_percent"], 0.0)
        self.assertIsNone(self.item("ollama-cloud")["cooldown"])

    def test_取用量壞了resolve的env與選把不變(self):
        baseline = self.resolve("opencode", "ollama-cloud")
        for broken in (RuntimeError("boom"), "hang"):
            with self.subTest(broken=repr(broken)):
                if broken == "hang":
                    self.ollama.gate = threading.Event()
                    self.addCleanup(self.ollama.gate.set)
                else:
                    self.ollama.outcome = broken
                self.build()
                started = time.monotonic()
                got = self.resolve("opencode", "ollama-cloud")
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertEqual(got["env"], baseline["env"])
                self.assertEqual({k: v for k, v in got["meta"].items() if k != "usage"},
                                 {k: v for k, v in baseline["meta"].items() if k != "usage"})
                self.usage.wait_idle(0.2)

    def test_組裝出錯resolve照回200(self):
        self.usage.usage_for = lambda sub: 1 / 0
        got = self.resolve("opencode", "ollama-cloud")
        self.assertEqual((got["meta"]["usage"]["status"], got["meta"]["usage"]["reason"]),
                         ("unknown", "internal"))
        self.assertTrue(got["env"])
        self.assertEqual(got["meta"]["usage"]["source"], "fake-source")


class UsageSecretsTest(UsageRegistryCase):
    def test_值不外洩_新欄位與稽核與DB(self):
        for fetch_outcome in (None, failure("upstream_error"), failure("rate_limited", 60),
                              RuntimeError(f"leak {CANARY['ollama-cloud']}")):
            if fetch_outcome is not None:
                self.ollama.outcome = fetch_outcome
            self.clock.advance(hours=1)
            with self.assertLogs(level="DEBUG") as logs:
                logging.getLogger("lgwd.usage").info("probe")
                self.list_items(); self.settle()
                meta = self.resolve("opencode", "ollama-cloud")["meta"]
            blob = json.dumps([self.list_items(), meta], ensure_ascii=False)
            blob += json.dumps(self.service._store.audit_all(), ensure_ascii=False)
            blob += "\n".join(logs.output)
            for value in CANARY.values():
                self.assertNotIn(value, blob)
                self.assertNotIn(value.encode(), self.db_bytes())


class RealFetcherTtlTest(RegistryCase):
    """真的 provider 掛點上的 TTL 常數（spec「TTL 與下限」）：Claude 下限 1800、Ollama 下限 60。"""

    def usage_for(self, drop_usage=False, **usage_policy):
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        if drop_usage:
            obj.pop("usage", None)
        elif usage_policy:
            obj["usage"] = usage_policy
        path = self.root / "policy-ttl.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        policy = load_policy(str(path))
        fetchers = {name: lgwd_providers.provider_for(name).make_usage_fetcher(UsageDeps())
                    for name in ("claude-personal", "ollama-cloud", "openrouter")}
        return UsageService(self.clock, fetchers=fetchers, settings=policy.usage)

    def test_沒有usage段_真的無段_Ollama120_Claude1800(self):
        # 真的沒有 usage 段 → 預設關閉（由 settings fallback 守）
        service = self.usage_for(drop_usage=True)
        self.assertEqual(service._ttl["ollama-cloud"], 120)
        self.assertEqual(service._ttl["claude-personal"], 1800)
        self.assertEqual(service._ttl["openrouter"], 120)
        self.assertFalse(service._enabled["claude-personal"])
        self.assertTrue(service._enabled["ollama-cloud"])
        self.assertTrue(service._enabled["openrouter"])

    def test_明確disabled_Claude1800(self):
        service = self.usage_for(**{"claude-personal": {"enabled": False}})
        self.assertEqual(service._ttl["claude-personal"], 1800)
        self.assertEqual(service._ttl["openrouter"], 120)
        self.assertFalse(service._enabled["claude-personal"])
        self.assertTrue(service._enabled["ollama-cloud"])
        self.assertTrue(service._enabled["openrouter"])

    def test_出廠政策開啟_Claude不是disabled(self):
        service = self.usage_for()
        self.assertTrue(service._enabled["claude-personal"])
        self.assertTrue(service._enabled["openrouter"])
        self.assertEqual(service._ttl["claude-personal"], 1800)
        self.assertEqual(service._ttl["openrouter"], 120)

    def test_出廠政策開啟_已部署key_list_not_fetched_yet_觸發一次(self):
        # C3 行為：出廠政策（enabled: true）＋已部署的 key → list 回 not_fetched_yet
        # 並觸發一次取用量；用假 fetcher，不 exec 真的 claude。
        fake = FakeFetcher(enabled=True, base=self.clock.now())
        fake.source = "claude-statusline"
        usage = UsageService(self.clock, fetchers={"claude-personal": fake},
                             settings=self.policy.usage, key_of=self.keys.get,
                             deployed=self.keys.deployed)
        self.addCleanup(usage.stop)
        service = RegistryService(self.db_path, self.policy, self.keys, self.clock,
                                  usage=usage)
        self.addCleanup(service.close)
        got = _by_id(service.list_subscriptions()["subscriptions"])["claude-personal"]["usage"]
        self.assertEqual((got["status"], got["reason"], got["source"]),
                         ("unknown", "not_fetched_yet", "claude-statusline"))
        self.assertTrue(usage.wait_idle())
        self.assertEqual(fake.calls, 1)

    def test_Claude政策300_夾到1800並記log(self):
        with self.assertLogs("lgwd.usage", level="WARNING") as logs:
            service = self.usage_for(**{"claude-personal": {"ttl_seconds": 300}})
        self.assertEqual(service._ttl["claude-personal"], 1800)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(CANARY["claude-personal"], logs.output[0])

    def test_Claude調高有效(self):
        self.assertEqual(self.usage_for(**{"claude-personal": {"ttl_seconds": 7200}})._ttl["claude-personal"], 7200)

    def test_Ollama設1_夾到60並記log(self):
        with self.assertLogs("lgwd.usage", level="WARNING") as logs:
            service = self.usage_for(**{"ollama-cloud": {"ttl_seconds": 1}})
        self.assertEqual(service._ttl["ollama-cloud"], 60)
        self.assertEqual(len(logs.output), 1)

    def test_Ollama調高有效(self):
        self.assertEqual(self.usage_for(**{"ollama-cloud": {"ttl_seconds": 300}})._ttl["ollama-cloud"], 300)


if __name__ == "__main__":
    unittest.main()
