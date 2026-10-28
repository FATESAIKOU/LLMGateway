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
    "model_cooldowns": {},
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
ROLES = {
    "schema": 1,
    "role": "impl",
    "data": "public",
    "agent": "opencode",
    "candidates": [
        {"id": "opencode-zen", "provider": "opencode-zen", "rank": 1,
         "privacy": "public", "available": True, "cooldown": None,
         "model_cooldowns": {}},
    ],
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

    def resolve(self, agent, provider, model=None, subscription=None, exclude=None,
                data=None):
        return self._finish("resolve", json.loads(json.dumps(RESOLVE)),
                            agent, provider, model, subscription, exclude, data)

    def role_candidates(self, role, agent, data=None):
        return self._finish("role_candidates", json.loads(json.dumps(ROLES)),
                            role, agent, data)

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
                          None, None, None))

    def test_resolve_query規則(self):
        # 未知參數忽略
        status, _ = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&flag=1&x=")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1],
                         ("resolve", "opencode", "opencode-zen", None, None, None,
                          None))
        # 空白值＝沒給（subscription=x&exclude= 是合法釘住）
        status, _ = self.call(
            "GET", "/v1/resolve?agent=claude-code&provider=anthropic"
                   "&subscription=claude-company&exclude=")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1],
                         ("resolve", "claude-code", "anthropic", None,
                          "claude-company", None, None))
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

    def test_resolve_agy_query原樣傳給service(self):
        # #107：agent=agy 不被 HTTP 層擋下，值原樣交給 service（是不是 200 由政策決定）
        status, _ = self.call(
            "GET", "/v1/resolve?agent=agy&provider=google&model=gemini-test-id")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1],
                         ("resolve", "agy", "google", "gemini-test-id",
                          None, None, None))

    def test_resolve_data原樣傳到service(self):
        # #102：data 是已知參數，值原樣交給 service 驗（HTTP 層只管形狀）
        for value, expected in (("public", "public"), ("general", "general"),
                                ("real", "real"), ("private", "private"),
                                ("", None)):
            with self.subTest(data=value):
                status, _ = self.call(
                    "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&data="
                           + value)
                self.assertEqual(status, 200)
                self.assertEqual(self.fake.calls[-1][6], expected)
        # 重複出現（含空白的那一次）→ 400
        for query in ("data=public&data=real", "data=&data=public"):
            with self.subTest(query=query):
                status, payload = self.call(
                    "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&" + query)
                self.assertEqual(status, 400)
                self.assertEqual(self.json_of(payload)["error"], "bad_request")

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
            (errors.privacy_not_allowed(), 403, "privacy_not_allowed"),
            (errors.not_found(), 404, "not_found"),
            (errors.not_deployed(), 409, "not_deployed"),
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

    # --- roles ---
    def test_roles_ok(self):
        status, payload = self.call(
            "GET", "/v1/roles/impl?agent=opencode&data=public")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload), ROLES)
        self.assertEqual(self.fake.calls[-1],
                         ("role_candidates", "impl", "opencode", "public"))
        blob = json.dumps(self.json_of(payload))
        self.assertNotIn('"key"', blob)
        self.assertNotIn("usage", blob)
        self.assertNotIn("available_again_at", blob)

    def test_roles_data省略與空值(self):
        # data 省略 → 不帶（服務取最嚴）
        status, _ = self.call("GET", "/v1/roles/impl?agent=opencode")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ("role_candidates", "impl", "opencode", None))
        # 空白值＝沒給（與 resolve 的 query 規則相同）
        status, _ = self.call("GET", "/v1/roles/impl?agent=opencode&data=")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ("role_candidates", "impl", "opencode", None))

    def test_roles_query規則(self):
        # 未知參數忽略（重複也不算錯）
        status, _ = self.call(
            "GET", "/v1/roles/impl?agent=opencode&flag=1&x=2&x=3")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ("role_candidates", "impl", "opencode", None))
        # 已知參數重複出現 → 400（含空白的那一次）
        for query in ("agent=opencode&agent=claude-code&data=public",
                      "agent=opencode&data=public&data=real",
                      "agent=&agent=opencode"):
            with self.subTest(query=query):
                status, payload = self.call("GET", "/v1/roles/impl?" + query)
                self.assertEqual(status, 400)
                self.assertEqual(self.json_of(payload)["error"], "bad_request")

    def test_roles_role解碼(self):
        status, _ = self.call("GET", "/v1/roles/a%2Fb?agent=opencode")
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1][1], "a/b")

    def test_roles錯誤對應(self):
        cases = [
            (errors.bad_request(missing=["agent"]), 400, "bad_request"),
            (errors.bad_request(), 400, "bad_request"),
            (errors.not_found(), 404, "not_found"),
        ]
        for exc, code, err in cases:
            with self.subTest(code=err):
                self.fake.raise_for["role_candidates"] = exc
                status, payload = self.call(
                    "GET", "/v1/roles/impl?agent=opencode&data=real")
                self.assertEqual(status, code)
                data = self.json_of(payload)
                self.assertEqual(data["error"], err)
                self.assertNotIn("detail", data)
                self.assertNotIn("candidates", data)
                if exc.extra.get("missing"):
                    self.assertEqual(data["missing"], exc.extra["missing"])

    def test_roles非GET與缺role404(self):
        for method, path in (("POST", "/v1/roles/impl"), ("PUT", "/v1/roles/impl"),
                             ("DELETE", "/v1/roles/impl"), ("GET", "/v1/roles"),
                             ("GET", "/v1/roles/")):
            with self.subTest(method=method, path=path):
                status, payload = self.call(method, path, {"x": 1})
                self.assertEqual(status, 404)
                self.assertEqual(self.json_of(payload)["error"], "not_found")
        self.assertFalse(self.fake.calls)  # 完全沒進 service

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

    def test_cooldown帶model原樣傳到service(self):
        # HTTP 層不對 model 做任何特別處理（整個 body 照傳，未知欄位由 service 忽略）
        body = {"until": "2026-10-11T15:00:00Z", "model": "big-pickle",
                "window": "5h", "extra": 1}
        status, payload = self.call(
            "POST", "/v1/subscriptions/opencode-zen/cooldown", body)
        self.assertEqual(status, 204)
        self.assertEqual(payload, b"")
        self.assertEqual(self.fake.calls[-1],
                         ("register_cooldown", "opencode-zen", body))

    def test_cooldown_DELETE仍404(self):
        # 沒有提前解除：DELETE 沒有路由（路由仍是四個）
        status, payload = self.call(
            "DELETE", "/v1/subscriptions/opencode-zen/cooldown",
            {"until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.assertEqual(status, 404)
        self.assertEqual(self.json_of(payload)["error"], "not_found")
        self.assertFalse(self.fake.calls)

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
            ("DELETE", "/v1/subscriptions/claude-personal/cooldown"),
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

    def test_http_cooldown帶model端到端(self):
        # task 6.1／design 23：body 的 model 原樣到 service，list 看得到
        status, payload = self.call(
            "POST", "/v1/subscriptions/opencode-zen/cooldown",
            {"until": "2026-10-11T15:00:00Z", "model": "big-pickle"})
        self.assertEqual(status, 204)
        self.assertEqual(payload, b"")
        status, payload = self.call("GET", "/v1/subscriptions")
        self.assertEqual(status, 200)
        items = {i["id"]: i for i in self.json_of(payload)["subscriptions"]}
        self.assertEqual(items["opencode-zen"]["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})
        self.assertIsNone(items["opencode-zen"]["cooldown"])
        self.assertTrue(items["opencode-zen"]["available"])
        # 模型不在 allow → 400 bad_request（不是 403）
        status, payload = self.call(
            "POST", "/v1/subscriptions/opencode-zen/cooldown",
            {"until": "2026-10-11T15:00:00Z", "model": "muse-spark-1.3"})
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["error"], "bad_request")
        # 冷卻不再擋 resolve：同一個模型仍 200，meta 附冷卻資訊
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&model=big-pickle")
        self.assertEqual(status, 200)
        meta = self.json_of(payload)["meta"]
        self.assertEqual(meta["model"], "big-pickle")
        self.assertEqual(meta["model_cooldowns"],
                         {"big-pickle": {"until": "2026-10-11T15:00:00Z"}})


class ResolveHttpTest(ServerCase):
    """resolve 經 HTTP 的端到端（真 RegistryService＋假 key＋FakeClock）。"""

    REPO_ROOT = Path(__file__).resolve().parent.parent
    FAKE_KEYS = {"claude-personal": "fake-personal", "claude-company": "fake-company",
                 "openrouter": "fake-openrouter", "opencode-zen": "fake-zen",
                 "opencode-go": "fake-zen", "groq": "fake-groq",
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
            "GROQ_API_KEY=fake-groq\n"
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
        self.assertEqual(data["meta"]["model"], "beam-free")
        self.assertEqual(data["env"]["OPENCODE_PROVIDER_ID"], "opencode-go")
        self.assertEqual(data["env"]["OPENCODE_API_KEY"], "fake-zen")
        # #88：groq 走自己的 endpoint 與 provider id，key 是自己的 GROQ_API_KEY
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=groq")
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual(data["meta"]["subscription"], "groq")
        self.assertEqual(data["meta"]["endpoint"], "https://api.groq.com/openai/v1")
        self.assertEqual(data["meta"]["model"], "openai/gpt-oss-120b")
        self.assertEqual(data["env"]["OPENCODE_PROVIDER_ID"], "groq")
        self.assertEqual(data["env"]["OPENCODE_API_KEY"], "fake-groq")
        self.assertEqual(data["env"]["OPENCODE_MODEL"], "openai/gpt-oss-120b")
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

    def test_resolve端到端_data(self):
        # 不帶 data：不濾，行為不變（缺省 B）
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen")
        self.assertEqual(status, 200)
        self.assertIsNone(self.json_of(payload)["meta"]["data"])
        # 宣告超過那把的 privacy（zen 是 public）→ 403，不含 env
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&data=real")
        self.assertEqual(status, 403)
        data = self.json_of(payload)
        self.assertEqual(data["error"], "privacy_not_allowed")
        self.assertNotIn("env", data)
        # 宣告得起的等級 → 200，meta.data 是請求值原樣
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&data=public")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload)["meta"]["data"], "public")
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=ollama-cloud&data=real")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(payload)["meta"]["data"], "real")
        # 空白 data 視為沒給
        status, payload = self.call(
            "GET", "/v1/resolve?agent=opencode&provider=opencode-zen&data=")
        self.assertEqual(status, 200)
        self.assertIsNone(self.json_of(payload)["meta"]["data"])

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
            # #102：宣告 data 後的隱私 403；agent 的 403 先於 privacy 的 403
            ("/v1/resolve?agent=claude-code&provider=opencode-zen"
             "&subscription=opencode-zen&data=real", 403, "agent_not_allowed", None),
            ("/v1/resolve?agent=opencode&provider=opencode-zen"
             "&subscription=opencode-zen&data=real", 403, "privacy_not_allowed", None),
            ("/v1/resolve?agent=opencode&provider=opencode-zen&data=private",
             400, "bad_request", None),
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


class RolesHttpTest(ServerCase):
    """GET /v1/roles/{role} 經 HTTP 的端到端（真 RegistryService＋假 key＋FakeClock）。"""

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(
            "# 測試用假 key\n"
            "CLAUDE_PERSONAL_OAUTH_TOKEN=fake-personal\n"
            "OPENCODE_ZEN_API_KEY=fake-zen\n"
            "OLLAMA_API_KEY=fake-ollama\n", encoding="utf-8")
        self.service = RegistryService(
            str(root / "llm.db"),
            load_policy(str(self.REPO_ROOT / "policy" / "default.json")),
            KeyStore(str(keys_path)), FakeClock(),
        )
        self.base = self.serve(self.service)

    def tearDown(self):
        super().tearDown()
        self.service.close()
        self.tmp.cleanup()

    def json_of(self, payload):
        return json.loads(payload.decode())

    def test_roles端到端_候選與資格外洩(self):
        status, payload = self.call(
            "GET", "/v1/roles/impl?agent=opencode&data=real")
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual((data["schema"], data["role"], data["data"], data["agent"]),
                         (1, "impl", "real", "opencode"))
        self.assertEqual([row["id"] for row in data["candidates"]], ["ollama-cloud"])
        row = data["candidates"][0]
        self.assertEqual(row["provider"], "ollama-cloud")
        self.assertEqual(row["rank"], 1)
        self.assertEqual(set(row), {"id", "provider", "rank", "privacy", "available",
                                    "cooldown", "model_cooldowns"})
        blob = json.dumps(data, ensure_ascii=False)
        for absent in ('"key"', "usage", "available_again_at", "fake-ollama"):
            self.assertNotIn(absent, blob)

    def test_roles端到端_缺省data(self):
        status, payload = self.call("GET", "/v1/roles/qa?agent=opencode")
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual(data["data"], "real")
        self.assertEqual([row["id"] for row in data["candidates"]], ["ollama-cloud"])
        # PMO 的 floor 是 real：請求 public 也回 real
        status, payload = self.call(
            "GET", "/v1/roles/PMO?agent=claude-code&data=public")
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual(data["data"], "real")
        self.assertEqual([row["id"] for row in data["candidates"]],
                         ["claude-personal", "ollama-cloud"])
        self.assertEqual(data["candidates"][0]["provider"], "anthropic")

    def test_roles端到端_錯誤(self):
        cases = [
            ("/v1/roles/impl?agent=clade-code", 400, "bad_request"),
            ("/v1/roles/impl?agent=opencode&data=private", 400, "bad_request"),
            ("/v1/roles/impl", 400, "bad_request"),
            ("/v1/roles/boss?agent=opencode", 404, "not_found"),
            # #104：400 先於 404——未知角色＋壞 agent 仍回 400
            ("/v1/roles/boss?agent=clade-code", 400, "bad_request"),
            ("/v1/roles/pmo?agent=opencode", 404, "not_found"),
            ("/v1/roles/impl?agent=opencode&agent=claude-code", 400, "bad_request"),
            ("/v1/roles/impl?agent=opencode&data=public&data=real", 400, "bad_request"),
        ]
        for path, code, err in cases:
            with self.subTest(path=path):
                status, payload = self.call("GET", path)
                self.assertEqual(status, code)
                data = self.json_of(payload)
                self.assertEqual(data["error"], err)
                self.assertNotIn("candidates", data)
                self.assertNotIn("detail", data)
        # 缺 agent 帶 missing
        status, payload = self.call("GET", "/v1/roles/impl")
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["missing"], ["agent"])

    def test_roles端到端_不記稽核(self):
        self.call("GET", "/v1/roles/impl?agent=opencode&data=public")
        self.assertEqual(self.service._store.audit_all(), [])


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


class LocalLoginHttpTest(ServerCase):
    """#107：agent=agy 經真 RegistryService＋臨時政策檔的端到端（PR 1 不動出廠政策）。"""

    MODEL = "gemini-test-id"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys_path = root / "keys.env"
        keys_path.write_text(
            "# 測試用假 key（沒有 antigravity：它本來就沒有憑證）\n"
            "OPENCODE_ZEN_API_KEY=fake-zen\n",
            encoding="utf-8")
        with open(Path(__file__).resolve().parent.parent / "policy" / "default.json",
                  encoding="utf-8") as fh:
            obj = json.load(fh)
        obj["subscriptions"]["antigravity"] = {
            "priority": 35,
            "privacy": "public",
            "agents": ["agy"],
            "models": {"allow": [self.MODEL], "default": self.MODEL},
        }
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(obj), encoding="utf-8")
        self.clock = FakeClock()
        self.service = RegistryService(
            str(root / "llm.db"), load_policy(str(policy_path)),
            KeyStore(str(keys_path)), self.clock)
        self.base = self.serve(self.service)

    def tearDown(self):
        super().tearDown()
        self.service.close()
        self.tmp.cleanup()

    def json_of(self, payload):
        return json.loads(payload.decode())

    def test_resolve回200且env為空物件(self):
        status, payload = self.call(
            "GET", "/v1/resolve?agent=agy&provider=google&model=" + self.MODEL)
        self.assertEqual(status, 200)
        data = self.json_of(payload)
        self.assertEqual(data["env"], {})
        self.assertEqual(data["env_schema"], 1)
        meta = data["meta"]
        self.assertEqual(meta["subscription"], "antigravity")
        self.assertEqual(meta["provider"], "google")
        self.assertEqual(meta["kind"], "local-login")
        self.assertEqual(meta["agent"], "agy")
        self.assertEqual(meta["model"], self.MODEL)
        self.assertIsNone(meta["endpoint"])
        self.assertEqual(meta["usage"]["status"], "not_applicable")

    def test_list的available為true(self):
        status, payload = self.call("GET", "/v1/subscriptions")
        self.assertEqual(status, 200)
        item = {i["id"]: i for i in self.json_of(payload)["subscriptions"]}[
            "antigravity"]
        self.assertTrue(item["available"])
        self.assertIsNone(item["available_again_at"])
        self.assertEqual(item["usage"]["status"], "not_applicable")
        self.assertNotIn("key", item)

    def test_get回409且不含key(self):
        status, payload = self.call(
            "GET", "/v1/subscriptions/antigravity?agent=agy")
        self.assertEqual(status, 409)
        self.assertEqual(self.json_of(payload), {"error": "not_deployed"})

    def test_未知agent仍400(self):
        status, payload = self.call(
            "GET", "/v1/resolve?agent=agy2&provider=google")
        self.assertEqual(status, 400)
        self.assertEqual(self.json_of(payload)["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
