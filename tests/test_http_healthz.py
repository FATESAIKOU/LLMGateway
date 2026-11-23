"""lgwd/http.py 的 ``/healthz``（#246 D1）：H-1～H-4 與 T-1～T-7。

探針用的健康檢查：只證明行程活著、能回應（而且 lgwd 已經過了啟動時的 policy／
keys／DB 初始化）。body 固定一個鍵、不碰 service、不加任何額外標頭；它**不代表**
DB 此刻可寫（那是接受下來的取捨，見 docs/deploy.md）。

測試不建真 DB：service 用「任何屬性一被碰就炸」的替身，證明 /healthz 完全不讀
service。
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from lgwd import errors
from lgwd.http import make_api_server

try:
    from .lgwctl_fakes import REPO_ROOT
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import REPO_ROOT

HOST = "127.0.0.1"


class ExplodingService:
    """任何屬性存取都 raise：/healthz 不該碰 service。"""

    def __getattr__(self, name):
        raise AssertionError(f"/healthz 不該碰 service.{name}")


class NoisyService:
    """有「機密」屬性的替身：/healthz 的回應不得帶到任何一個。"""

    tag = "lgw-v0.0.0-should-not-leak"
    version = "0.0.0-should-not-leak"
    policy = "policy-should-not-leak"
    key = "KEY-should-not-leak"
    hostname = "host-should-not-leak"

    def list_subscriptions(self):
        raise AssertionError("list_subscriptions 不該被呼叫")


class HealthzCase(unittest.TestCase):
    def serve(self, service):
        self._server = make_api_server(service, HOST, 0)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.base = "http://{0}:{1}".format(host, port)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers


class HealthzRouteTests(HealthzCase):
    """T-1～T-6：成功回應、方法、不碰 service、不洩漏。"""

    def setUp(self):
        self.serve(ExplodingService())

    def test_T1_200_json_and_service_untouched(self):
        """T-1：200、body 是 {"status": "ok"}、≤64 bytes、service 沒被碰。"""
        status, body, headers = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"status": "ok"})
        self.assertLessEqual(len(body), 64)
        # 固定 16 bytes（H-2：由既有的 _send_json 輸出）
        self.assertEqual(len(body), 16)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(headers.get("Content-Length"), str(len(body)))

    def test_T2_query_is_ignored(self):
        """T-2：query 不參與路由（/healthz?x=1 也是 200）。"""
        status, body, _ = self.call("GET", "/healthz?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode()), {"status": "ok"})

    def test_T3_lookalike_paths_are_404(self):
        """T-3：/healthz/ 與 /v1/healthz 是 404 not_found。"""
        for path in ("/healthz/", "/v1/healthz", "/healthz/x"):
            with self.subTest(path=path):
                status, body, _ = self.call("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body.decode()), errors.not_found().body())

    def test_T4_other_methods_match_usage(self):
        """T-4：POST／PUT／DELETE／PATCH 的 404 與 /v1/usage 同方法相同（H-3）。"""
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status_h, body_h, _ = self.call(method, "/healthz")
                status_u, body_u, _ = self.call(method, "/v1/usage")
                self.assertEqual(status_h, 404)
                self.assertEqual(status_h, status_u)
                self.assertEqual(body_h, body_u)

    def test_T5_head_is_not_added(self):
        """T-5：D1 不新增 do_HEAD；HEAD /healthz 與 HEAD /v1/subscriptions 同碼。"""
        status_h, _, _ = self.call("HEAD", "/healthz")
        status_l, _, _ = self.call("HEAD", "/v1/subscriptions")
        self.assertEqual(status_h, status_l)
        # 現狀是 http.server 回的 501（沒有 do_HEAD）
        self.assertEqual(status_h, 501)

    def test_T6_body_has_only_status(self):
        """T-6：只有一個鍵、沒有 tag／版本／policy／key／主機名、沒有額外標頭。"""
        self.tearDown()
        self.serve(NoisyService())
        status, body, headers = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertRegex(text, r'^\{"status": "ok"\}$')
        self.assertEqual(list(json.loads(text)), ["status"])
        for leak in ("should-not-leak", "lgw-v", "policy", "KEY-", "host-"):
            self.assertNotIn(leak, text)
        # 不加額外標頭（也沒有 Cache-Control：內容沒有敏感資料，與 list 一致）
        self.assertIsNone(headers.get("Cache-Control"))
        self.assertIsNone(headers.get("Pragma"))
        self.assertIsNone(headers.get("WWW-Authenticate"))
        extra = set(headers.keys()) - {"Server", "Date", "Content-Type", "Content-Length"}
        self.assertEqual(extra, set(), extra)


class HealthzCapabilityAgreementTests(HealthzCase):
    """T-7：repo 的 deploy/capabilities.json 與 /healthz 的回答一致（防兩邊漂移）。"""

    def test_T7_flag_says_new_and_route_answers(self):
        from lgwctl import capability

        cap = capability.read(str(REPO_ROOT))
        self.assertEqual(cap.state, "ok", "repo 的 deploy/capabilities.json 不見或壞了")
        self.assertEqual(cap.mode, "new")
        self.serve(ExplodingService())
        status, body, _ = self.call("GET", "/healthz")
        # 同一個檔案兩邊都讀：旗標說 new 就要有 /healthz，反之亦然。
        self.assertEqual(cap.mode == "new", status == 200)
        self.assertEqual(json.loads(body.decode()), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
