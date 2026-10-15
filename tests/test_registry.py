"""lgwd/registry.py 的測試：真 RegistryService（暫存 SQLite、假 key 檔、FakeClock）。

測試名稱照 spec subscription-registry／key-vault 的 Scenario（v5：until-only）。key 檔的值全是假 canary。
"""

import datetime as _dt
import json
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(items["opencode-zen"]["models"]["allow"], ["*"])
        self.assertEqual(
            items["opencode-zen"]["models"]["default"],
            "opencode/muse-spark-1.3-contributor-free",
        )

    def test_get與list照出廠值帶出models_default(self):
        # get 與 list 的回覆逐筆帶出出廠的 models.default；Claude 兩筆是 JSON
        # 的 null（不是空字串、也沒有缺欄）。
        expected = {
            "claude-personal": None,
            "claude-company": None,
            "opencode-zen": "opencode/muse-spark-1.3-contributor-free",
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
        for agent in ("opencode", "claude-code"):
            with self.subTest(agent=agent):
                item = self.get("openrouter", agent)
                self.assertEqual(item["key"], CANARY["openrouter"])
                self.assertEqual(item["models"]["default"], "openrouter/free")
                self.assertEqual(item["models"]["allow"], ["*:free", "openrouter/free"])
        self.assertEqual(self.get("opencode-zen", "opencode")["key"], CANARY["opencode-zen"])
        self.assertEqual(self.get("openrouter", "opencode")["key"], CANARY["openrouter"])
        self.assertEqual(self.get("openrouter", "claude-code")["key"], CANARY["openrouter"])
        self.assertEqual(self.get("ollama-cloud", "claude-code")["key"], CANARY["ollama-cloud"])


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


if __name__ == "__main__":
    unittest.main()
