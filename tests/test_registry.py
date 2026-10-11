"""lgwd/registry.py 的測試：真 RegistryService（暫存 SQLite、假 key 檔、FakeClock）。

測試名稱照 spec subscription-registry／key-vault 的 Scenario。key 檔的值全是假 canary。
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

    def cooldown(self, subscription, window, resets_at=None):
        body = {"window": window}
        if resets_at is not None:
            body["resets_at"] = resets_at
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
        # 其中一個窗口在冷卻 → false
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        self.assertFalse(_by_id(self.list_items())["claude-personal"]["available"])

    def test_models欄位(self):
        items = _by_id(self.list_items())
        self.assertEqual(items["openrouter"]["models"]["allow"], ["*:free"])
        self.assertIsNone(items["openrouter"]["models"]["default"])
        self.assertEqual(items["opencode-zen"]["models"]["allow"], ["*"])


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
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertFalse(item["available"])
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T15:02:00Z"})
        self.assertIsNone(item["cooldown"]["1w"])
        self.clock.set(_dt.datetime(2026, 10, 11, 15, 2, 0, tzinfo=_dt.timezone.utc))
        item = _by_id(self.list_items())["claude-personal"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["cooldown"]["5h"])


class GetTest(RegistryCase):
    def test_ClaudeCode拿Claude訂閱(self):
        item = self.get("claude-personal", "claude-code")
        self.assertEqual(item["key"], CANARY["claude-personal"])

    def test_opencode不能拿Claude訂閱(self):
        err = self.assert_api_error(403, "agent_not_allowed", self.get, "claude-company", "opencode")
        self.assertNotIn("key", err.body())

    def test_冷卻中仍可get(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        item = self.get("claude-personal", "claude-code")
        self.assertFalse(item["available"])
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

    def test_opencode拿Zen與OpenRouter(self):
        self.assertEqual(self.get("opencode-zen", "opencode")["key"], CANARY["opencode-zen"])
        self.assertEqual(self.get("openrouter", "opencode")["key"], CANARY["openrouter"])
        self.assertEqual(self.get("openrouter", "claude-code")["key"], CANARY["openrouter"])
        self.assertEqual(self.get("ollama-cloud", "claude-code")["key"], CANARY["ollama-cloud"])


class CooldownTest(RegistryCase):
    def test_帶重置時刻(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T15:02:00Z"})
        self.assertFalse(item["available"])
        self.assertEqual(self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z"), None)

    def test_resets_at型別或格式不對回invalid_resets_at(self):
        for resets in ("not-a-time", "2026-99-99", 123, True, ["2026-10-11T15:00:00Z"]):
            with self.subTest(resets=resets):
                err = self.assert_api_error(
                    400, "invalid_resets_at", self.cooldown, "claude-personal", "5h", resets)
                self.assertNotIn("detail", err.body())
                item = _by_id(self.list_items())["claude-personal"]
                self.assertIsNone(item["cooldown"]["5h"])  # 冷卻不變

    def test_resets_at在過去或超過八天回resets_at_out_of_range(self):
        # 邊界：過去 1 秒、超過 8 天 1 秒，以及遠過去與遠未來
        cases = [
            ("2026-10-10T23:59:59Z", "過去1秒"),
            ("2026-10-19T00:00:01Z", "超過8天1秒"),
            ("2020-01-01T00:00:00Z", "遠過去"),
            ("2099-01-01T00:00:00Z", "遠未來"),
        ]
        for resets, label in cases:
            with self.subTest(label=label, resets=resets):
                err = self.assert_api_error(
                    400, "resets_at_out_of_range", self.cooldown, "claude-personal", "5h", resets)
                self.assertNotIn("detail", err.body())
                item = _by_id(self.list_items())["claude-personal"]
                self.assertIsNone(item["cooldown"]["5h"])

    def test_八天整可以剛好超過不行(self):
        self.cooldown("claude-personal", "5h", "2026-10-19T00:00:00Z")  # 剛好 8 天
        self.assertIsNotNone(
            _by_id(self.list_items())["claude-personal"]["cooldown"]["5h"])
        err = self.assert_api_error(
            400, "resets_at_out_of_range", self.cooldown, "claude-personal", "5h", "2026-10-19T00:00:01Z")
        self.assertNotIn("detail", err.body())

    def test_剛好現在的時刻可以(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T00:00:00Z")  # 剛好現在（00:00:00）
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T00:02:00Z"})

    def test_沒有重置時刻(self):
        self.cooldown("claude-personal", "1w")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["1w"], {"until": "2026-10-18T00:00:00Z"})
        self.assertIsNone(item["cooldown"]["5h"])

    def test_沒給resets的5h用5小時(self):
        self.cooldown("claude-personal", "5h")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T05:00:00Z"})

    def test_同一窗口取較晚的(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        self.cooldown("claude-personal", "5h", "2026-10-11T14:00:00Z")  # 較早，不覆蓋
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T15:02:00Z"})
        self.cooldown("claude-personal", "5h", "2026-10-11T16:00:00Z")  # 較晚，覆蓋
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T16:02:00Z"})

    def test_窗口各自獨立(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        self.cooldown("claude-personal", "1w", "2026-10-14T00:00:00Z")
        item = _by_id(self.list_items())["claude-personal"]
        self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T15:02:00Z"})
        self.assertEqual(item["cooldown"]["1w"], {"until": "2026-10-14T00:02:00Z"})

    def test_到時自動解除(self):
        self.cooldown("claude-personal", "1w")
        self.assertFalse(_by_id(self.list_items())["claude-personal"]["available"])
        self.clock.set(_dt.datetime(2026, 10, 18, 0, 0, 0, tzinfo=_dt.timezone.utc))
        item = _by_id(self.list_items())["claude-personal"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["cooldown"]["1w"])

    def test_窗口不是5h或1w回invalid_window(self):
        for window in (None, "", "24h", "1d", 5, True):
            with self.subTest(window=window):
                err = self.assert_api_error(400, "invalid_window", self.cooldown, "claude-personal", window)
                self.assertNotIn("detail", err.body())

    def test_缺window欄位回bad_request(self):
        err = self.assert_api_error(
            400, "bad_request", self.service.register_cooldown, "claude-personal", {})
        self.assertEqual(err.body()["missing"], ["window"])
        self.assertNotIn("detail", err.body())

    def test_不存在的id回404(self):
        self.assert_api_error(
            404, "not_found", self.cooldown, "ghost", "5h", "2026-10-11T15:00:00Z")

    def test_冷卻跨重啟(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        self.service.close()
        reopened = RegistryService(self.db_path, self.policy, self.keys, self.clock)
        try:
            item = _by_id(reopened.list_subscriptions()["subscriptions"])["claude-personal"]
            self.assertEqual(item["cooldown"]["5h"], {"until": "2026-10-11T15:02:00Z"})
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
        self.assertIsNone(row["window"])
        self.assertIsNone(row["until"])

    def test_登錄冷卻留下紀錄(self):
        self.cooldown("claude-personal", "5h", "2026-10-11T15:00:00Z")
        rows = self.service._store.audit_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "cooldown")
        self.assertEqual(row["window"], "5h")
        self.assertEqual(row["until"], "2026-10-11T15:02:00Z")
        self.assertIsNone(row["agent"])

    def test_稽核只追加且沒有key(self):
        self.get("claude-personal", "claude-code")
        self.get("openrouter", "opencode")
        self.cooldown("openrouter", "1w")
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
