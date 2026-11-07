"""#187 P2：Codex（ChatGPT 訂閱 chatgpt-personal，provider openai，agent codex）的測試。

契約編號 C-n 對 openspec/changes/codex-subscription/tasks.md。出廠政策（policy/default.json）
在 P2 不含 chatgpt-personal，所以一律用臨時政策檔；key 檔裡從來沒有它的鍵。
"""

import json
import tempfile
import unittest
from pathlib import Path

from lgwd.clock import FakeClock
from lgwd.errors import ApiError
from lgwd.keys import KEY_NAMES, KeyStore
from lgwd.policy import LOCAL_LOGIN_AGENT, PolicyError, load_policy
from lgwd.providers import (
    PROVIDERS, GoogleProvider, OpenAIProvider, UsageDeps, provider_for)
from lgwd.registry import RegistryService
from tests.test_http import ServerCase
from tests.test_registry import CANARY, POLICY_PATH, key_text

MODEL = "gpt-test-id"
SUB = "chatgpt-personal"


def _factory_obj():
    return json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))


def _codex_sub(agents=("codex",), privacy="public", allow=(MODEL,), default=MODEL):
    return {"priority": 36, "privacy": privacy, "agents": list(agents),
            "models": {"allow": list(allow), "default": default}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.keys_path = self.root / "keys.env"
        # 沒有 chatgpt-personal 的鍵（C-8）
        self.keys_path.write_text(key_text(CANARY), encoding="utf-8")
        self.clock = FakeClock()
        self.keys = KeyStore(str(self.keys_path))
        self._services = []

    def tearDown(self):
        for service in self._services:
            service.close()
        self.tmp.cleanup()

    def policy_path(self, obj, name="policy.json"):
        path = self.root / name
        path.write_text(json.dumps(obj), encoding="utf-8")
        return str(path)

    def service(self, obj):
        service = RegistryService(
            str(self.root / f"llm{len(self._services)}.db"),
            load_policy(self.policy_path(obj)), self.keys, self.clock)
        self._services.append(service)
        return service

    def with_codex(self, in_impl_order=False, **kw):
        obj = _factory_obj()
        obj["subscriptions"][SUB] = _codex_sub(**kw)
        if in_impl_order:
            obj["roles"]["impl"]["order"].append(SUB)
        return obj

    def item(self, service, sub=SUB):
        return {i["id"]: i for i in service.list_subscriptions()["subscriptions"]}[sub]

    def assert_api_error(self, code, err, fn, *args, **kwargs):
        with self.assertRaises(ApiError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status, code)
        self.assertEqual(ctx.exception.code, err)
        return ctx.exception


class CodexResolveTest(_Base):
    def test_C5_resolve回200_env空_模型只在meta(self):
        service = self.service(self.with_codex())
        payload = service.resolve("codex", "openai", model=MODEL)
        self.assertEqual(payload["env"], {})
        self.assertEqual(payload["env_schema"], 1)
        meta = payload["meta"]
        self.assertEqual(meta["kind"], "local-login")
        self.assertEqual(meta["subscription"], SUB)
        self.assertEqual(meta["provider"], "openai")
        self.assertEqual(meta["agent"], "codex")
        self.assertEqual(meta["model"], MODEL)
        self.assertIsNone(meta["endpoint"])
        # 不帶 model：用 default
        payload = service.resolve("codex", "openai")
        self.assertEqual(payload["meta"]["model"], MODEL)
        self.assertEqual(payload["env"], {})

    def test_C6_usage為not_applicable(self):
        service = self.service(self.with_codex())
        usage = service.resolve("codex", "openai")["meta"]["usage"]
        self.assertEqual(usage["status"], "not_applicable")
        self.assertIsNone(usage["source"])
        # resolve 這段在沒注入 usage 時恆為 not_applicable；守點是最後一行（沒有 fetcher）
        self.assertIsNone(usage["session"])
        self.assertIsNone(usage["weekly"])
        self.assertIsNone(provider_for(SUB).make_usage_fetcher(UsageDeps()))

    def test_C8_KEY_NAMES沒有它_resolve與list不碰key檔的鍵(self):
        self.assertNotIn(SUB, KEY_NAMES)
        service = self.service(self.with_codex())
        # key 檔沒有它的鍵：list 不丟 unknown subscription、resolve 不回 not_deployed
        self.assertTrue(self.item(service)["available"])
        service.resolve("codex", "openai")

    def test_C10_政策沒有openai訂閱時回404(self):
        self.assertIn(SUB, PROVIDERS)
        service = self.service(_factory_obj())
        err = self.assert_api_error(
            404, "not_found", service.resolve, "codex", "openai")
        self.assertNotIn("env", err.body())

    def test_C11a_冷卻仍200並帶cooldown(self):
        service = self.service(self.with_codex())
        until = "2026-10-11T15:00:00Z"
        service.register_cooldown(SUB, {"until": until})
        payload = service.resolve("codex", "openai")
        self.assertEqual(payload["meta"]["cooldown"], {"until": until})
        self.assertEqual(payload["env"], {})

    def test_C12b_agent驗證(self):
        service = self.service(self.with_codex())
        self.assert_api_error(
            400, "bad_request", service.resolve, "foo", "openai")
        # codex 不因 agent 回 400
        service.resolve("codex", "openai")


class CodexListTest(_Base):
    def test_C7_list的row(self):
        service = self.service(self.with_codex())
        item = self.item(service)
        self.assertEqual(item["provider"], "openai")
        self.assertEqual(item["kind"], "local-login")
        self.assertIsNone(item["endpoint"])
        self.assertEqual(item["agents"], ["codex"])
        self.assertTrue(item["available"])
        self.assertNotIn("key", item)
        self.assertEqual(item["usage"]["status"], "not_applicable")

    def test_C7_出廠政策沒有chatgpt_personal(self):
        # P2 不動 policy/default.json
        policy = load_policy(POLICY_PATH)
        self.assertNotIn(SUB, policy.subscriptions)
        service = self.service(_factory_obj())
        ids = [i["id"] for i in service.list_subscriptions()["subscriptions"]]
        self.assertNotIn(SUB, ids)


class CodexPolicyShapeTest(_Base):
    def _load(self, obj):
        return load_policy(self.policy_path(obj, "shape.json"))

    def test_C9a_對照表(self):
        self.assertEqual(LOCAL_LOGIN_AGENT[SUB], "codex")
        self.assertEqual(self._load(self.with_codex()).subscriptions[SUB].agents,
                         ("codex",))

    def test_C9a_chatgpt_personal的agents必須剛好codex(self):
        for agents in (["codex", "opencode"], ["opencode"], ["claude-code"],
                       ["agy"], ["codex", "agy"]):
            with self.subTest(agents=agents):
                with self.assertRaises(PolicyError) as ctx:
                    self._load(self.with_codex(agents=agents))
                self.assertNotIn(MODEL, str(ctx.exception))
        with self.assertRaises(PolicyError) as ctx:
            self._load(self.with_codex(agents=["opencode"]))
        self.assertEqual(str(ctx.exception), f"{SUB} must only allow codex")

    def test_C9a_其他訂閱含codex拒收(self):
        obj = _factory_obj()
        obj["subscriptions"]["ollama-cloud"]["agents"] = ["opencode", "codex"]
        with self.assertRaises(PolicyError) as ctx:
            self._load(obj)
        self.assertEqual(str(ctx.exception), f"codex is only allowed for {SUB}")
        # antigravity 也不能含 codex
        obj = _factory_obj()
        obj["subscriptions"]["antigravity"]["agents"] = ["agy", "codex"]
        with self.assertRaises(PolicyError):
            self._load(obj)


class CodexRolesTest(_Base):
    def test_C11b_在order且privacy足夠才有候選(self):
        service = self.service(self.with_codex(in_impl_order=True))
        payload = service.role_candidates("impl", "codex", "public")
        self.assertEqual([c["id"] for c in payload["candidates"]], [SUB])
        self.assertTrue(payload["candidates"][0]["available"])
        # privacy 不足（public 訂閱、請求 general）→ 篩掉
        payload = service.role_candidates("impl", "codex", "general")
        self.assertEqual(payload["candidates"], [])

    def test_C11b_不在order時空候選(self):
        service = self.service(self.with_codex(in_impl_order=False))
        payload = service.role_candidates("impl", "codex", "public")
        self.assertEqual(payload["candidates"], [])

    def test_C12a_roles的agent驗證(self):
        service = self.service(self.with_codex())
        service.role_candidates("impl", "codex", "public")
        self.assert_api_error(
            400, "bad_request", service.role_candidates, "impl", "foo", "public")


class CodexCooldownModelTest(_Base):
    def test_C12c_model帶codex前綴回400_冷卻不變(self):
        service = self.service(self.with_codex(allow=(MODEL, "codex/gpt-6.1-sol", "*")))
        # allow 涵蓋得了 codex/…；只有 agent 前綴規則擋得到
        self.assert_api_error(
            400, "bad_request", service.register_cooldown, SUB,
            {"until": "2026-10-11T15:00:00Z", "model": "codex/gpt-6.1-sol"})
        self.assertEqual(self.item(service)["model_cooldowns"], {})
        self.assertIsNone(self.item(service)["cooldown"])
        # 對照：裸 id 可以登錄
        service.register_cooldown(
            SUB, {"until": "2026-10-11T15:00:00Z", "model": MODEL})
        self.assertEqual(self.item(service)["model_cooldowns"],
                         {MODEL: {"until": "2026-10-11T15:00:00Z"}})


class CodexBuildEnvTest(unittest.TestCase):
    def test_C12_codex對api_key_provider回空(self):
        self.assertEqual(
            provider_for("opencode-zen").build_env("codex", "fake-key", MODEL), {})

    def test_C12_claude_code對OpenAIProvider回空(self):
        self.assertEqual(OpenAIProvider().build_env("claude-code", None, MODEL), {})
        self.assertEqual(OpenAIProvider().build_env("codex", None, MODEL), {})
        # P1 的 C-4 仍成立
        self.assertEqual(GoogleProvider().build_env("claude-code", None, MODEL), {})


class CodexHttpTest(ServerCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(key_text(CANARY), encoding="utf-8")
        obj = _factory_obj()
        obj["subscriptions"][SUB] = _codex_sub()
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(obj), encoding="utf-8")
        self.service = RegistryService(
            str(root / "llm.db"), load_policy(str(policy_path)),
            KeyStore(str(keys_path)), FakeClock())
        self.base = self.serve(self.service)

    def tearDown(self):
        super().tearDown()
        self.service.close()
        self.tmp.cleanup()

    def get(self, path):
        status, payload = self.call("GET", path)
        return status, json.loads(payload.decode())

    def test_C5_resolve經HTTP(self):
        status, data = self.get("/v1/resolve?agent=codex&provider=openai&model=" + MODEL)
        self.assertEqual(status, 200)
        self.assertEqual(data["env"], {})
        self.assertEqual(data["meta"]["subscription"], SUB)

    def test_C12a_roles_codex不回400_foo回400(self):
        status, data = self.get("/v1/roles/impl?agent=codex&data=public")
        self.assertEqual(status, 200)
        self.assertEqual(data["agent"], "codex")
        status, _ = self.get("/v1/roles/impl?agent=foo&data=public")
        self.assertEqual(status, 400)

    def test_C12b_resolve_foo回400(self):
        status, data = self.get("/v1/resolve?agent=foo&provider=openai")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
