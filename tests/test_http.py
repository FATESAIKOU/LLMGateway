"""lgwd/http.py 的測試：全用假 service（固定值或丟 ApiError），不用真 key。

涵蓋 spec subscription-registry 的路由、spec key-vault 的「不做應用層認證」與
共通錯誤行為（404、413、500、log 不含值）。
"""

import http.client
import json
import logging
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lgwd import errors
from lgwd.clock import FakeClock
from lgwd.http import make_api_server
from lgwd.keys import KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService

HOST = "127.0.0.1"

ITEM = {
    "schema": 1,
    "id": "claude-personal",
    "provider": "anthropic",
    "kind": "claude-subscription",
    "agents": ["claude-code"],
    "priority": 10,
    "models": {"allow": ["*"], "default": None},
    "endpoint": None,
    "key": "fake-key-in-get-reply",
    "cooldown": None,
    "available": True,
}
LIST = {
    "schema": 1,
    "subscriptions": [{k: v for k, v in ITEM.items() if k != "key"}],
}
RESOLVE = {
    "schema": 1,
    "env_schema": 1,
    "meta": {
        "subscription": "opencode-zen",
        "provider": "opencode-zen",
        "kind": "api-key",
        "agent": "opencode",
        "model": "mimo-v2.6-flash-free",
        "models": {"allow": ["*-free", "big-pickle"], "default": "mimo-v2.6-flash-free"},
        "endpoint": "https://opencode.ai/zen/v1",
        "priority": 10,
    },
    "env": {
        "OPENCODE_API_KEY": "fake-key-in-resolve-reply",
        "OPENCODE_PROVIDER_ID": "opencode",
        "OPENCODE_MODEL": "mimo-v2.6-flash-free",
        "OPENCODE_SMALL_MODEL": "mimo-v2.6-flash-free",
    },
}


class FakeService:
    """假 service：固定回覆；raise_for[方法名] 設例外就丟。會記下收到的呼叫。"""

    def __init__(self):
        self.calls = []
        self.raise_for = {}

    def _finish(self, name, payload, *args):
        self.calls.append((name, *args))
        if name in self.raise_for:
            raise self.raise_for[name]
        return payload

    def list_subscriptions(self):
        return self._finish("list_subscriptions", json.loads(json.dumps(LIST)))

    def get_subscription(self, sub_id, agent):
        return self._finish("get_subscription", dict(ITEM), sub_id, agent)

    def resolve(self, agent, provider, model=None, subscription=None, exclude=None):
        return self._finish("resolve", json.loads(json.dumps(RESOLVE)),
                            agent, provider, model, subscription, exclude)

    def register_cooldown(self, sub_id, body):
        return self._finish("register_cooldown", None, sub_id, body)


class ServerCase(unittest.TestCase):
    def serve(self, service):
        self._server = make_api_server(service, HOST, 0)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        return "http://{0}:{1}".format(host, port)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def call(self, method, path, body=None, raw=None, headers=None):
        """打 API；HTTPError 也解析成 (狀態碼, body)。預設不帶任何憑證。"""
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None
        )
        all_headers = dict(headers or {})
        if data is not None:
            all_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(
            self.base + path, data=data, headers=all_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class ApiTest(ServerCase):
    def setUp(self):
        self.fake = FakeService()
        self.base = self.serve(self.fake)

    def json_of(self, payload):
        return json.loads(payload.decode())

    # --- list ---
    def test_list_ok不帶憑證(self):
        status, payload = self.call("GET", "/v1/subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload), LIST)  # 不要求任何 token
        self.assertEqual(self.fake.calls, [("list_subscriptions",)])

    def test_list_query被忽略(self):
        status, _ = self.call("GET", "/v1/subscriptions?x=1&agent=opencode")
        self.assertEqual(status, 200)

    def test_list_200沒有不快取標頭(self):
        # design 決定 1：不快取標頭是 get／resolve 的 200 才帶；list 不變
        req = urllib.request.Request(self.base + "/v1/subscriptions")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIsNone(resp.headers.get("Cache-Control"))

    # --- resolve ---
    def test_resolve_ok(self):
        status, payload = self.call(
            "GET",
            "/v1/resolve?agent=opencode&provider=opencode-zen&model=big-pickle")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload), RESOLVE)
        self.assertEqual(self.fake.calls[-1],
                         ("resolve", "opencode", "opencode-zen", "big-pickle",
                          None, None))

    def test_resolve_query規則(self):
        # 未知參數忽略
        status, _ = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&flag=1&x=")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1],
                         ("resolve", "opencode", "opencode-zen", None, None, None))
        # 空白值＝沒給（subscription=x&exclude= 是合法釘住）
        status, _ = self.call(
            "GET", "/v1/resolve?agent=claude-code&provider=anthropic"
                   "&subscription=claude-company&exclude=")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1],
                         ("resolve", "claude-code", "anthropic", None,
                          "claude-company", None))
        # 已知參數重複出現 → 400
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&agent=claude-code&provider=opencode-zen")
        self.assertEqual(status, 400)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "bad_request")
        self.assertNotIn("env", data)
        self.assertNotIn("detail", data)
        status, _ = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=x&provider=y")
        self.assertEqual(status, 400)
        # 重複出現（其中一次是空白）也算重複，不是「空白＝沒給」就放行
        status, _ = self.call(
            "GET", "/v1/resolve?agent=&agent=opencode&provider=opencode-zen")
        self.assertEqual(status, 400)
        # %2B 由 server 的 parse_qs 解回 +（client 端編碼在 test_client 驗）
        status, _ = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&model=a%2Bb")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1][3], "a+b")

    def test_resolve_200帶不快取標頭(self):
        req = urllib.request.Request(
            self.base + "/v1/resolve?agent=opencode&provider=opencode-zen")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
            self.assertEqual(resp.headers.get("Pragma"), "no-cache")

    def test_get_200帶不快取標頭(self):
        req = urllib.request.Request(
            self.base + "/v1/subscriptions/claude-personal?agent=claude-code")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
            self.assertEqual(resp.headers.get("Pragma"), "no-cache")

    def test_resolve錯誤對應_不含env(self):
        cases = [
            (errors.bad_request(), 400, "bad_request"),
            (errors.no_model(), 400, "no_model"),
            (errors.model_not_allowed(), 403, "model_not_allowed"),
            (errors.not_found(), 404, "not_found"),
            (errors.not_deployed(), 409, "not_deployed"),
            (errors.cooling("2026-10-11T15:00:00Z"), 409, "cooling"),
            (errors.no_candidate(), 409, "no_candidate"),
        ]
        for exc, code, err in cases:
            with self.subTest(code=err):
                self.fake.raise_for["resolve"] = exc
                status, payload = self.call(
                    "GET", "/v1/resolve?agent=opencode&provider=opencode-zen")
                self.assertEqual(status, code)
                data = self.json_of(payload)
                self.assertEqual(data["error"], err)
                self.assertNotIn("env", data)
                self.assertNotIn("OPENCODE_API_KEY", data)
                self.assertNotIn("detail", data)
                if exc.extra.get("until"):
                    self.assertEqual(data["until"], exc.extra["until"])

    # --- get ---
    def test_get_ok(self):
        status, payload = self.call("GET", "/v1/subscriptions/claude-personal?agent=claude-code")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload), ITEM)
        self.assertEqual(self.fake.calls[-1],
                         ("get_subscription", "claude-personal", "claude-code"))

    def test_get_沒帶agent回400(self):
        self.fake.raise_for["get_subscription"] = errors.bad_request(missing=["agent"])
        status, payload = self.call("GET", "/v1/subscriptions/claude-personal")
        self.assertEqual(status, 400)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "bad_request")
        self.assertEqual(data["missing"], ["agent"])
        self.assertNotIn("detail", data)
        self.assertIsNone(self.fake.calls[-1][2])

    def test_get_空agent回400(self):
        self.fake.raise_for["get_subscription"] = errors.bad_request(missing=["agent"])
        status, payload = self.call("GET", "/v1/subscriptions/claude-personal?agent=")
        self.assertEqual(status, 400)
        self.assertNotIn("detail", self.json_of(payload))

    def test_get_id解碼(self):
        status, _ = self.call("GET", "/v1/subscriptions/ghost%2Did?agent=opencode")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1][1], "ghost-id")

    def test_get錯誤對應(self):
        cases = [
            (errors.agent_not_allowed(), 403, "agent_not_allowed"),
            (errors.not_found(), 404, "not_found"),
            (errors.not_deployed(), 409, "not_deployed"),
        ]
        for exc, code, err in cases:
            with self.subTest(code=err):
                self.fake.raise_for["get_subscription"] = exc
                status, payload = self.call(
                    "GET", "/v1/subscriptions/claude-personal?agent=opencode")
                self.assertEqual(status, code)
                self.assertEqual(self.json_of(payload)["error"], err)
                self.assertNotIn("key", self.json_of(payload))
                self.assertNotIn("detail", self.json_of(payload))

    # --- cooldown ---
    def test_cooldown_204(self):
        status, payload = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown",
            {"until": "2026-10-11T15:00:00Z"})
        self.assertEqual(status, 204)
        self.assertEqual(payload, b"")  # 204 不帶 body
        self.assertEqual(self.fake.calls[-1],
                         ("register_cooldown", "claude-personal",
                          {"until": "2026-10-11T15:00:00Z"}))

    def test_cooldown_空body到service(self):
        status, _ = self.call("POST", "/v1/subscriptions/claude-personal/cooldown")
        self.assertEqual(status, 204)
        self.assertEqual(self.fake.calls[-1][2], {})

    def test_cooldown錯誤對應(self):
        cases = [
            (errors.invalid_until(), 400, "invalid_until"),
            (errors.until_out_of_range(), 400, "until_out_of_range"),
            (errors.bad_request(missing=["until"]), 400, "bad_request"),
            (errors.not_found(), 404, "not_found"),
        ]
        for exc, code, err in cases:
            with self.subTest(code=err):
                self.fake.raise_for["register_cooldown"] = exc
                status, payload = self.call(
                    "POST", "/v1/subscriptions/ghost/cooldown", {"until": "2026-10-11T15:00:00Z"})
                self.assertEqual(status, code)
                data = self.json_of(payload)
                self.assertEqual(data["error"], err)
                self.assertNotIn("detail", data)
                if exc.extra.get("missing"):
                    self.assertEqual(data["missing"], exc.extra["missing"])

    def test_cooldown_bad_json回400(self):
        status, payload = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown", raw=b"{nope")
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["error"], "bad_request")
        self.assertNotIn("detail", self.json_of(payload))

    def test_json_list_body回400(self):
        status, payload = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown", raw=b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["error"], "bad_request")
        self.assertNotIn("detail", self.json_of(payload))

    # --- 其他路徑一律 404 ---
    def test_其他路徑404(self):
        cases = [
            ("GET", "/nope"), ("GET", "/v1/leases"), ("POST", "/v1/leases"),
            ("GET", "/v1/whoami"), ("GET", "/v1/board"), ("POST", "/v1/board"),
            ("GET", "/v1/admin/clients"), ("POST", "/v1/admin/clients"),
            ("DELETE", "/v1/admin/clients/w1"), ("POST", "/v1/leases/L1/start"),
            ("GET", "/v1/subscriptions/"), ("POST", "/v1/subscriptions"),
            ("POST", "/v1/subscriptions/claude-personal"),
            ("GET", "/v1/subscriptions/claude-personal/cooldown"),
            ("PUT", "/v1/subscriptions/claude-personal/cooldown"),
            ("POST", "/v1/resolve"), ("DELETE", "/v1/resolve"),
            ("GET", "/v1/resolve/"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, payload = self.call(method, path, {"x": 1})
                self.assertEqual(status, 404)
                self.assertEqual(self.json_of(payload)["error"], "not_found")
        self.assertFalse(self.fake.calls)  # 完全沒進 service

    # --- 錯誤處理 ---
    def test_unexpected_500(self):
        self.fake.raise_for["list_subscriptions"] = RuntimeError("boom")
        status, payload = self.call("GET", "/v1/subscriptions")
        self.assertEqual(status, 500)
        self.assertEqual(self.json_of(payload), {"error": "internal"})

    def test_500_logs_kind_not_value(self):
        secret = "CANARY-500-zz9"
        self.fake.raise_for["list_subscriptions"] = ValueError(secret)
        try:
            with self.assertLogs("lgwd.http", level="INFO") as logs:
                status, _ = self.call("GET", "/v1/subscriptions")
        finally:
            del self.fake.raise_for["list_subscriptions"]
        self.assertEqual(status, 500)
        text = "\n".join(logs.output)
        self.assertIn("ValueError", text)  # 只記種類
        self.assertNotIn(secret, text)

    def test_log_clean(self):
        secret = "s3cr3t-value-zz9"
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            self.call(
                "POST", "/v1/subscriptions/claude-personal/cooldown",
                {"until": secret},
                headers={"Authorization": "Bearer " + secret, "X-Canary": secret},
            )
        text = "\n".join(logs.output)
        self.assertNotIn(secret, text)  # 標頭與 body 都不進 log
        self.assertIn("POST /v1/subscriptions/claude-personal/cooldown", text)
        self.assertIn("204", text)

    def test_body_too_large(self):
        # 宣告 2 MB，只送 2 bytes：服務不讀 body，直接 413。
        host, port = self.base.rsplit(":", 1)
        conn = http.client.HTTPConnection(host.rsplit("/", 1)[-1], int(port), timeout=5)
        try:
            conn.putrequest("POST", "/v1/subscriptions/claude-personal/cooldown")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(2 * 1024 * 1024))
            conn.endheaders()
            conn.send(b"{}")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)
            self.assertEqual(json.loads(resp.read().decode())["error"], "body_too_large")
        finally:
            conn.close()
        self.assertFalse(self.fake.calls)  # 沒進 service

    def test_body_exactly_1mb_ok(self):
        prefix = b'{"until":"2026-10-11T15:00:00Z","pad":"'
        pad = 1024 * 1024 - len(prefix) - len(b'"}')
        raw = prefix + b"a" * pad + b'"}'
        self.assertEqual(len(raw), 1024 * 1024)
        status, _ = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown", raw=raw)
        self.assertEqual(status, 204)  # 邊界：超過才擋
        self.assertEqual(self.fake.calls[-1][2]["until"], "2026-10-11T15:00:00Z")


class RealServiceHttpTest(ServerCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        canary = (
            "# 測試用假 key\n"
            "CLAUDE_PERSONAL_OAUTH_TOKEN=fake-personal\n"
            "CLAUDE_COMPANY_OAUTH_TOKEN=fake-company\n"
            "OPENROUTER_API_KEY=fake-openrouter\n"
            "OPENCODE_ZEN_API_KEY=fake-zen\n"
            "OLLAMA_API_KEY=fake-ollama\n"
        )
        keys_path.write_text(canary, encoding="utf-8")
        policy_path = str(Path(__file__).resolve().parent.parent / "policy" / "default.json")
        self.clock = FakeClock()  # 預設 2026-10-11T00:00:00Z
        self.policy = load_policy(policy_path)
        self.keys = KeyStore(str(keys_path))
        self.service = RegistryService(str(root / "llm.db"), self.policy, self.keys, self.clock)
        self.base = self.serve(self.service)

    def tearDown(self):
        super().tearDown()
        self.service.close()
        self.tmp.cleanup()

    def json_of(self, payload):
        return json.loads(payload.decode())

    def test_http端到端_400碼表案例(self):
        # 1. bad_request: JSON 解析失敗
        status, payload = self.call("POST", "/v1/subscriptions/claude-personal/cooldown", raw=b"{broken")
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["error"], "bad_request")
        self.assertNotIn("detail", self.json_of(payload))

        # 2. bad_request: body 不是物件
        status, payload = self.call("POST", "/v1/subscriptions/claude-personal/cooldown", raw=b"[\"not-dict\"]")
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["error"], "bad_request")
        self.assertNotIn("detail", self.json_of(payload))

        # 3. bad_request: get 缺 agent
        status, payload = self.call("GET", "/v1/subscriptions/claude-personal")
        self.assertEqual(status, 400)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "bad_request")
        self.assertEqual(data["missing"], ["agent"])
        self.assertNotIn("detail", data)

        # 4. bad_request: cooldown 缺 until
        status, payload = self.call("POST", "/v1/subscriptions/claude-personal/cooldown", {})
        self.assertEqual(status, 400)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "bad_request")
        self.assertEqual(data["missing"], ["until"])
        self.assertNotIn("detail", data)

        # 5. invalid_until: 型別或格式不是 RFC 3339
        for until in ("not-a-time", "2026-99-99", 123, True):
            with self.subTest(until=until):
                status, payload = self.call(
                    "POST", "/v1/subscriptions/claude-personal/cooldown",
                    {"until": until}
                )
                self.assertEqual(status, 400)
                data = self.json_of(payload)
                self.assertEqual(data["error"], "invalid_until")
                self.assertNotIn("detail", data)

        # 6. until_out_of_range: 過去 1 秒（2026-10-10T23:59:59Z）
        status, payload = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown",
            {"until": "2026-10-10T23:59:59Z"}
        )
        self.assertEqual(status, 400)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "until_out_of_range")
        self.assertNotIn("detail", data)

        # 7. until_out_of_range: 超過 8 天 1 秒（2026-10-19T00:00:01Z）
        status, payload = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown",
            {"until": "2026-10-19T00:00:01Z"}
        )
        self.assertEqual(status, 400)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "until_out_of_range")
        self.assertNotIn("detail", data)

        # 8. 邊界成功：剛好 8 天（2026-10-19T00:00:00Z）
        status, payload = self.call(
            "POST", "/v1/subscriptions/claude-personal/cooldown",
            {"until": "2026-10-19T00:00:00Z"}
        )
        self.assertEqual(status, 204)

        # 9. 邊界成功：剛好現在（2026-10-11T00:00:00Z）
        status, payload = self.call(
            "POST", "/v1/subscriptions/opencode-zen/cooldown",
            {"until": "2026-10-11T00:00:00Z"}
        )
        self.assertEqual(status, 204)


class ResolveHttpTest(ServerCase):
    """resolve 經 HTTP 的端到端（真 RegistryService＋假 key＋FakeClock）。"""

    REPO_ROOT = Path(__file__).resolve().parent.parent
    FAKE_KEYS = {"claude-personal": "fake-personal", "claude-company": "fake-company",
                 "openrouter": "fake-openrouter", "opencode-zen": "fake-zen",
                 "opencode-go": "fake-zen",
                 "ollama-cloud": "fake-ollama"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        canary = (
            "# 測試用假 key\n"
            "CLAUDE_PERSONAL_OAUTH_TOKEN=fake-personal\n"
            "CLAUDE_COMPANY_OAUTH_TOKEN=fake-company\n"
            "OPENROUTER_API_KEY=fake-openrouter\n"
            "OPENCODE_ZEN_API_KEY=fake-zen\n"
            "OLLAMA_API_KEY=fake-ollama\n"
        )
        keys_path.write_text(canary, encoding="utf-8")
        policy_path = str(self.REPO_ROOT / "policy" / "default.json")
        self.clock = FakeClock()  # 預設 2026-10-11T00:00:00Z
        self.service = RegistryService(
            str(root / "llm.db"),
            load_policy(policy_path), KeyStore(str(keys_path)), self.clock,
        )
        self.base = self.serve(self.service)

    def tearDown(self):
        super().tearDown()
        self.service.close()
        self.tmp.cleanup()

    def json_of(self, payload):
        return json.loads(payload.decode())

    def test_resolve端到端_預設與釘住(self):
        # api-key 的預設：model 省略 → 該把的 models.default
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload)["meta"]["model"], "mimo-v2.6-flash-free")
        # #77：opencode-go 走自己的 endpoint 與 provider id，key 與 zen 共用
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-go")
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual(data["meta"]["subscription"], "opencode-go")
        self.assertEqual(data["meta"]["endpoint"],
                         "https://opencode.ai/inference/go/openai/v1")
        self.assertEqual(data["meta"]["model"], "iceman-contributor-free")
        self.assertEqual(data["env"]["OPENCODE_PROVIDER_ID"], "opencode-go")
        self.assertEqual(data["env"]["OPENCODE_API_KEY"], "fake-zen")
        # anthropic 只剩 personal：預設挑它，model 取政策預設
        status, payload = self.call(
            "GET", "/v1/resolve?agent=claude-code&provider=anthropic")
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual(data["meta"]["subscription"], "claude-personal")
        self.assertEqual(data["meta"]["model"], "claude-sonnet-5-5")
        self.assertEqual(data["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "fake-personal")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "claude-sonnet-5-5")
        self.assertEqual(data["env"]["ANTHROPIC_SMALL_FAST_MODEL"], "claude-sonnet-5-5")
        # 釘住已移除的公司那一把 → 404
        status, payload = self.call(
            "GET", "/v1/resolve?agent=claude-code&provider=anthropic&subscription=claude-company")
        self.assertEqual(status, 404)
        self.assertEqual(self.json_of(payload)["error"], "not_found")
        # exclude 掉唯一一把 → 409 no_candidate
        status, payload = self.call(
            "GET", "/v1/resolve?agent=claude-code&provider=anthropic&exclude=claude-personal")
        self.assertEqual(status, 409)
        self.assertEqual(self.json_of(payload)["error"], "no_candidate")

    def test_resolve端到端_錯誤precedence(self):
        cases = [
            # 缺必填（missing 列出缺的欄位）
            ("/v1/resolve?provider=opencode-zen", 400, "bad_request", {"missing": ["agent"]}),
            ("/v1/resolve", 400, "bad_request", {"missing": ["agent", "provider"]}),
            (# 未知的 agent 是輸入錯誤（不是 403）
             "/v1/resolve?agent=foo&provider=opencode-zen", 400, "bad_request", None),
            (# 輸入形狀錯誤先於查無：subscription 與 provider 不一致先於 provider 404
             "/v1/resolve?agent=claude-code&provider=NoSuchProvider&subscription=claude-personal",
             400, "bad_request", None),
            ("/v1/resolve?agent=claude-code&provider=anthropic&subscription=opencode-zen",
             400, "bad_request", None),
            ("/v1/resolve?agent=claude-code&provider=anthropic"
             "&subscription=claude-personal&exclude=ollama-cloud",
             400, "bad_request", None),
            (# 查無
             "/v1/resolve?agent=opencode&provider=NoSuchProvider", 404, "not_found", None),
            # 政策守門：model 不在 allow（含帶前綴的不正規化）
            ("/v1/resolve?agent=opencode&provider=opencode-zen&model=muse-spark-1.3",
             403, "model_not_allowed", None),
            ("/v1/resolve?agent=opencode&provider=opencode-zen&model=opencode%2Fbig-pickle",
             403, "model_not_allowed", None),
        ]
        for path, code, err, extra in cases:
            with self.subTest(path=path):
                status, payload = self.call("GET", path)
                self.assertEqual(status, code)
                data = self.json_of(payload)
                self.assertEqual(data["error"], err)
                self.assertNotIn("env", data)
                if extra:
                    self.assertEqual(data["missing"], extra["missing"])
                else:
                    self.assertNotIn("missing", data)

    def test_resolve端到端_錯誤不記稽核(self):
        self.call("GET", "/v1/resolve?agent=opencode&provider=opencode-zen&model=muse-spark-1.3")
        self.assertEqual(self.service._store.audit_all(), [])
        status, _ = self.call("GET", "/v1/resolve?agent=opencode&provider=opencode-zen")
        self.assertEqual(status, 200)
        self.assertEqual([r["action"] for r in self.service._store.audit_all()], ["resolve"])

    def test_resolve_log不含憑證值(self):
        # 值的紀律（中-1）：log 只記方法／路徑／狀態碼，env 的憑證值不進 log
        with self.assertLogs("lgwd.http", level="INFO") as logs:
            status, _ = self.call(
                "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&model=big-pickle")
        self.assertEqual(status, 200)
        text = "\n".join(logs.output)
        for value in self.FAKE_KEYS.values():
            self.assertNotIn(value, text)
        # log 只記方法／路徑／狀態碼（query 根本不進 log，嚴於「非機密」的要求）
        self.assertIn("GET /v1/resolve -> 200", text)
        self.assertNotIn("model", text)

    def test_resolve空白值與重複鍵(self):
        # 空白值＝沒給：exclude= 是合法釘住（透明語意）
        status, payload = self.call(
            "GET", "/v1/resolve?agent=claude-code&provider=anthropic"
                   "&subscription=claude-personal&exclude=")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload)["meta"]["subscription"], "claude-personal")
        # 空白的 model 視為省略
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&model=")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload)["meta"]["model"], "mimo-v2.6-flash-free")
        # 重複參數回 400
        for query in ("agent=x&agent=y&provider=opencode-zen",
                      "agent=opencode&provider=x&provider=y",
                      "agent=opencode&provider=opencode-zen&model=a&model=b",
                      "agent=opencode&provider=opencode-zen&exclude=a&exclude=b"):
            with self.subTest(query=query):
                status, payload = self.call("GET", "/v1/resolve?" + query)
                self.assertEqual(status, 400)
                self.assertEqual(self.json_of(payload)["error"], "bad_request")


class UsageHttpTest(ServerCase):
    """#60 tasks 6.2／6.3：用量欄位經 HTTP 的端到端（不加路由、標頭不變、log 不含值）。"""

    KEY = "canary-http-ollama-0001"

    def setUp(self):
        from tests.usage_fakes import FakeFetcher
        from lgwd.usage import UsageService
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(
            "CLAUDE_PERSONAL_OAUTH_TOKEN=canary-http-claude\n"
            "OPENROUTER_API_KEY=canary-http-or\n"
            "OPENCODE_ZEN_API_KEY=canary-http-zen\n"
            f"OLLAMA_API_KEY={self.KEY}\n", encoding="utf-8")
        policy_path = str(Path(__file__).resolve().parent.parent / "policy" / "default.json")
        clock = FakeClock()
        keys = KeyStore(str(keys_path))
        self.fetcher = FakeFetcher(base=clock.now())
        self.usage = UsageService(clock, fetchers={"ollama-cloud": self.fetcher},
                                  key_of=keys.get, deployed=keys.deployed)
        self.service = RegistryService(str(root / "llm.db"), load_policy(policy_path), keys,
                                       clock, usage=self.usage)
        self.base = self.serve(self.service)

    def tearDown(self):
        super().tearDown()
        self.usage.stop()
        self.service.close()
        self.tmp.cleanup()

    def raw(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.headers, json.loads(resp.read())

    def test_list帶usage_resolve的meta帶usage_標頭不變(self):
        status, _, body = self.raw("/v1/subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], 1)
        ollama = {i["id"]: i for i in body["subscriptions"]}["ollama-cloud"]
        self.assertEqual(ollama["usage"]["reason"], "not_fetched_yet")
        self.assertTrue(self.usage.wait_idle())
        status, headers, body = self.raw("/v1/resolve?agent=opencode&provider=ollama-cloud")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Pragma"), "no-cache")
        self.assertEqual(body["meta"]["usage"]["status"], "ok")
        self.assertNotIn("available_again_at", body["meta"])
        self.assertEqual(body["schema"], 1)

    def test_get沒有usage(self):
        status, _, body = self.raw("/v1/subscriptions/ollama-cloud?agent=opencode")
        self.assertEqual(status, 200)
        self.assertNotIn("usage", body)
        self.assertNotIn("available_again_at", body)

    def test_沒有新路由(self):
        for path in ("/v1/usage", "/v1/subscriptions/ollama-cloud/usage"):
            with self.subTest(path):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.raw(path)
                self.assertIn(ctx.exception.code, (404, 405))

    def test_取用量失敗後log與回覆不含key(self):
        self.fetcher.outcome = RuntimeError(f"leak {self.KEY}")
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("lgwd.usage").info("probe")
            self.raw("/v1/subscriptions")
            self.assertTrue(self.usage.wait_idle())
            _, _, listed = self.raw("/v1/subscriptions")
            _, _, resolved = self.raw("/v1/resolve?agent=opencode&provider=ollama-cloud")
        blob = json.dumps([listed, resolved["meta"]]) + "\n".join(logs.output)
        self.assertNotIn(self.KEY, blob)
        self.assertEqual(resolved["meta"]["usage"]["reason"], "internal")


if __name__ == "__main__":
    unittest.main()
