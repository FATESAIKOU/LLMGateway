"""lgwd/registry.py 的測試：真 RegistryService（暫存 SQLite、假 key 檔、FakeClock）。

測試名稱照 spec subscription-registry／key-vault 的 Scenario（v5：until-only）。key 檔的值全是假 canary。
"""

import datetime as _dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lgwd import providers as lgwd_providers
from lgwd.clock import FakeClock
from lgwd.errors import ApiError
from lgwd.keys import KEY_NAMES, KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = str(REPO_ROOT / "policy" / "default.json")
UTC = _dt.timezone.utc

# 假資料：絕不是真的 key
CANARY = {
    "claude-personal": "canary-registry-personal-0001",
    "claude-company": "canary-registry-company-0002",
    "openrouter": "canary-registry-openrouter-0003",
    "opencode-zen": "canary-registry-zen-0004",
    "ollama-cloud": "canary-registry-ollama-0005",
}


def key_text(values: dict[str, str]) -> str:
    lines = ["# 測試用假 key"]
    for sub, value in values.items():
        lines.append(f"{KEY_NAMES[sub]}={value}")
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
             "endpoint", "key", "cooldown", "available"},
        )

    def test_provider欄位(self):
        expected = {
            "claude-personal": ("anthropic", "claude-subscription", None),
            "claude-company": ("anthropic", "claude-subscription", None),
            "openrouter": ("openrouter", "api-key", "https://openrouter.ai/api"),
            "opencode-zen": ("opencode-zen", "api-key", "https://opencode.ai/zen/v1"),
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
            "claude-company": None,
            "openrouter": "https://openrouter.ai/api",
            "ollama-cloud": "https://ollama.com",
            "opencode-zen": "https://opencode.ai/zen/v1",
        }
        agents = {
            "claude-personal": "claude-code",
            "claude-company": "claude-code",
            "openrouter": "opencode",
            "ollama-cloud": "opencode",
            "opencode-zen": "opencode",
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
        # key 沒部署 → false
        self.keys_path.write_text(
            key_text({k: v for k, v in CANARY.items() if k != "opencode-zen"}),
            encoding="utf-8",
        )
        self.keys.reload()
        self.assertFalse(_by_id(self.list_items())["opencode-zen"]["available"])
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

    def test_get與list照出廠值帶出models_default(self):
        # get 與 list 的回覆逐筆帶出出廠的 models.default；Claude 兩筆是 JSON
        # 的 null（不是空字串、也沒有缺欄）。
        expected = {
            "claude-personal": None,
            "claude-company": None,
            "opencode-zen": "mimo-v2.6-flash-free",
            "openrouter": "openrouter/free",
            "ollama-cloud": "deepseek-v4.1-flash",
        }
        agents = {
            "claude-personal": "claude-code",
            "claude-company": "claude-code",
            "opencode-zen": "opencode",
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
                    # JSON 往返後照樣（Claude 兩筆仍是 null）
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

    def test_Claude訂閱沒有預設(self):
        # Scenario: Claude 訂閱沒有預設—讀 claude-personal 的共通 JSON，
        # models.default 為 null；呼叫方不帶模型參數，用 Claude Code 自己的預設。
        item = self.get("claude-personal", "claude-code")
        self.assertEqual(item["kind"], "claude-subscription")
        self.assertIn("default", item["models"])
        self.assertIsNone(item["models"]["default"])
        listed = _by_id(self.list_items())["claude-personal"]
        self.assertIsNone(listed["models"]["default"])
        # JSON 的 null，不是空字串
        blob = json.dumps(item, ensure_ascii=False)
        self.assertIn('"default": null', blob)
        self.assertEqual(json.loads(blob)["models"]["default"], None)

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
            ["claude-personal", "opencode-zen", "claude-company", "openrouter", "ollama-cloud"],
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
        err = self.assert_api_error(403, "agent_not_allowed", self.get, "claude-company", "opencode")
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

    def test_key沒部署回409(self):
        self.keys_path.write_text(
            key_text({k: v for k, v in CANARY.items() if k != "claude-company"}),
            encoding="utf-8",
        )
        self.keys.reload()
        err = self.assert_api_error(409, "not_deployed", self.get, "claude-company", "claude-code")
        self.assertNotIn("key", err.body())

    def test_get_openrouter回傳default模型(self):
        item = self.get("openrouter", "opencode")
        self.assertEqual(item["key"], CANARY["openrouter"])
        self.assertEqual(item["models"]["default"], "openrouter/free")
        self.assertEqual(item["models"]["allow"], ["*:free", "openrouter/free"])
        self.assertEqual(self.get("opencode-zen", "opencode")["key"], CANARY["opencode-zen"])
        self.assertEqual(self.get("ollama-cloud", "claude-code")["key"], CANARY["ollama-cloud"])

    def test_ClaudeCode不能拿openrouter(self):
        # 出廠政策（#23）：openrouter 只給 opencode（Claude Code 碰到
        # openrouter/free 會 unrecognized_model）
        err = self.assert_api_error(
            403, "agent_not_allowed", self.get, "openrouter", "claude-code")
        self.assertNotIn("key", err.body())


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

    def resolve(self, agent, provider, model=None, subscription=None, exclude=None):
        return self.service.resolve(agent, provider, model=model,
                                    subscription=subscription, exclude=exclude)

    def _service_with_policy(self, obj) -> RegistryService:
        path = self.root / "policy-under-resolve.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return RegistryService(self.db_path, load_policy(str(path)),
                               self.keys, self.clock)

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

    def test_釘住公司那一把(self):
        # claude-personal（priority 10）未冷卻、已部署；釘住後仍選 company
        payload = self.resolve("claude-code", "anthropic", subscription="claude-company")
        self.assertEqual(payload["meta"]["subscription"], "claude-company")

    def test_釘住的那把冷卻中(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        err = self.assert_api_error(
            409, "cooling", self.resolve, "claude-code", "anthropic",
            subscription="claude-personal")
        self.assertEqual(err.body()["until"], "2026-10-11T15:00:00Z")
        self.assertNotIn("env", err.body())

    def test_釘住的subscription與provider不一致(self):
        err = self.assert_api_error(
            400, "bad_request", self.resolve, "claude-code", "anthropic",
            subscription="opencode-zen")
        self.assertNotIn("env", err.body())

    def test_釘住與排除同時給(self):
        err = self.assert_api_error(
            400, "bad_request", self.resolve, "claude-code", "anthropic",
            subscription="claude-company", exclude="claude-personal")
        self.assertNotIn("env", err.body())

    def test_換把時排除剛耗盡的那一把(self):
        # cooldown 登錄失敗＝沒有進冷卻；exclude 在選把先擋掉 personal
        payload = self.resolve("claude-code", "anthropic", exclude="claude-personal")
        self.assertEqual(payload["meta"]["subscription"], "claude-company")

    def test_排除後沒有候選(self):
        # claude-company 不在政策檔；exclude 掉 personal 後沒有候選
        obj = json.loads(Path(POLICY_PATH).read_text(encoding="utf-8"))
        del obj["subscriptions"]["claude-company"]
        service = self._service_with_policy(obj)
        try:
            err = self.assert_api_error(
                409, "no_candidate", service.resolve,
                "claude-code", "anthropic", exclude="claude-personal")
            self.assertNotIn("env", err.body())
            self.assertNotIn("until", err.body())  # 不回 cooling（沒有恢復時刻）
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
        # 空字串也是沒給（HTTP 層後同一個入口）
        err = self.assert_api_error(400, "bad_request", self.resolve, None, "opencode-zen")
        self.assertEqual(err.body()["missing"], ["agent"])

    def test_未知的agent(self):
        err = self.assert_api_error(400, "bad_request", self.resolve, "foo", "opencode-zen")
        self.assertNotEqual(err.status, 403)  # 不是 403 agent_not_allowed

    def test_meta只帶有資訊的欄位(self):
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(
            sorted(payload["meta"]),
            ["agent", "endpoint", "kind", "model", "models", "priority",
             "provider", "subscription"],
        )
        self.assertNotIn("cooldown", payload["meta"])
        self.assertNotIn("available", payload["meta"])

    def test_值不外洩(self):
        # 憑證樣式掃描稽核、list 回覆與 DB；endpoint 的值只掃憑證樣式（providers 已擋）
        self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.resolve("claude-code", "anthropic")
        rows = self.service._store.audit_all()
        blobs = [
            json.dumps(rows, ensure_ascii=False),
            json.dumps(self.list_items(), ensure_ascii=False),
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

    def test_同provider兩把一把冷卻中(self):
        self.cooldown("claude-personal", "2026-10-11T15:00:00Z")
        payload = self.resolve("claude-code", "anthropic")
        self.assertEqual(payload["meta"]["subscription"], "claude-company")  # priority 20

    def test_全部候選冷卻中(self):
        self.cooldown("claude-personal", "2026-10-11T16:00:00Z")
        self.cooldown("claude-company", "2026-10-11T15:00:00Z")
        err = self.assert_api_error(409, "cooling", self.resolve, "claude-code", "anthropic")
        self.assertEqual(err.body()["until"], "2026-10-11T15:00:00Z")  # 最早恢復時刻
        self.assertNotIn("env", err.body())

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

    def test_未知provider(self):
        self.assert_api_error(404, "not_found", self.resolve, "opencode", "NoSuchProvider")

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

    def test_釘住的key未部署(self):
        self.keys_path.write_text(
            key_text({k: v for k, v in CANARY.items() if k != "claude-company"}),
            encoding="utf-8",
        )
        self.keys.reload()
        err = self.assert_api_error(
            409, "not_deployed", self.resolve,
            "claude-code", "anthropic", subscription="claude-company")
        self.assertNotIn("env", err.body())

    def test_全部候選未部署(self):
        self.keys_path.write_text("# 沒有任何 key\n", encoding="utf-8")
        self.keys.reload()
        err = self.assert_api_error(409, "not_deployed", self.resolve,
                                    "claude-code", "anthropic")
        self.assertNotIn("env", err.body())

    # --- env 的組成的 Scenario ---

    def test_claude訂閱不帶模型變數(self):
        payload = self.resolve("claude-code", "anthropic", subscription="claude-personal")
        self.assertEqual(sorted(payload["env"]), ["CLAUDE_CODE_OAUTH_TOKEN"])
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
        self.assertEqual(payload["meta"]["model"], None)  # agent 自己的預設
        self.assertNotIn("ANTHROPIC_MODEL", payload["env"])

    def test_api_key的預設(self):
        payload = self.resolve("opencode", "opencode-zen")
        self.assertEqual(payload["meta"]["model"], "mimo-v2.6-flash-free")

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
        # claude-subscription 用 agent 預設：model 欄位是 null，不是空字串
        self.resolve("claude-code", "anthropic", subscription="claude-company")
        row = self.service._store.audit_all()[0]
        self.assertIsNone(row["model"])
        self.assertEqual(row["provider"], "anthropic")

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


class EnvSchemaContractTest(RegistryCase):
    """design 決定 1：env_schema 的消費端契約（sidecar 對不認得的更高版本拒收）。

    sidecar 的實作在 MyLinuxPool（私有 repo）；這裡用替身把「拒收規則」釘住，
    保證 Gateway 的回覆帶這個欄位、且它是可比較的整數版本。
    """

    # 替身（＝worker 映像裡釘住的 sidecar）認得的鍵集版本
    KNOWN = 1

    def _sidecar_double(self, payload: dict) -> str:
        if payload["env_schema"] > self.KNOWN:
            return "refuse: unknown env_schema"
        return "ok"

    def test_替身對更高env_schema拒收(self):
        payload = self.resolve("opencode", "opencode-zen", model="big-pickle")
        self.assertEqual(self._sidecar_double(payload), "ok")
        bumped = dict(payload, env_schema=payload["env_schema"] + 1)
        self.assertNotEqual(self._sidecar_double(bumped), "ok")


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


if __name__ == "__main__":
    unittest.main()
