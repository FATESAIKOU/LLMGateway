"""lgwctl usage 的測試：list＋roles 依 id 合併、not_applicable 的標示、冷卻旁列、
rank 順序、roles 404 與參數／連線失敗；輸出不含 key。

假 service 只綁 127.0.0.1 的隨機埠（in-process HTTP），不連外網、不碰真機。
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from .lgwctl_fakes import Sandbox
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import Sandbox

HOST = "127.0.0.1"
CANARY = "FakeUsageKey_DoNotUse_101"

CLAUDE_USAGE = {
    "status": "ok", "source": "claude-statusline",
    "fetched_at": "2026-10-12T02:00:00Z", "stale": False, "reason": None,
    "session": {"remaining_percent": 76.5, "resets_at": "2026-10-25T07:00:00Z"},
    "weekly": {"remaining_percent": 39, "resets_at": "2026-10-28T00:00:00Z"},
}
ZEN_USAGE = {
    "status": "not_applicable", "source": None, "fetched_at": None,
    "stale": None, "reason": None, "session": None, "weekly": None,
}
OLLAMA_USAGE = {
    "status": "unknown", "source": "ollama-api-balance", "fetched_at": None,
    "stale": None, "reason": "no_key", "session": None, "weekly": None,
}
OPENROUTER_USAGE = {
    "status": "ok", "source": "openrouter-credits",
    "fetched_at": "2026-10-12T02:00:00Z", "stale": False, "reason": None,
    "session": None,
    "weekly": {"remaining_percent": 10, "resets_at": "2026-10-19T00:00:00Z"},
}

# 故意每筆都塞 key：list 正常不會有，但 CLI 即使收到也不能印出來。
SUBSCRIPTIONS = [
    {
        "schema": 1, "id": "claude-personal", "provider": "anthropic",
        "kind": "claude-subscription", "agents": ["claude-code"], "priority": 10,
        "models": {"allow": ["*"], "default": None}, "endpoint": None,
        "cooldown": None, "model_cooldowns": {}, "available": True,
        "available_again_at": None, "usage": CLAUDE_USAGE, "key": CANARY,
    },
    {
        "schema": 1, "id": "opencode-zen", "provider": "opencode-zen",
        "kind": "api-key", "agents": ["opencode"], "priority": 20,
        "models": {"allow": ["*-free"], "default": "big-pickle"}, "endpoint": None,
        "cooldown": {"until": "2026-10-12T03:00:00Z"}, "model_cooldowns": {},
        "available": False, "available_again_at": "2026-10-12T03:00:00Z",
        "usage": ZEN_USAGE, "key": CANARY,
    },
    {
        "schema": 1, "id": "opencode-go", "provider": "opencode-go",
        "kind": "api-key", "agents": ["opencode"], "priority": 30,
        "models": {"allow": ["beam-free"], "default": "beam-free"}, "endpoint": None,
        "cooldown": None, "model_cooldowns": {}, "available": True,
        "available_again_at": None, "usage": ZEN_USAGE, "key": CANARY,
    },
    {
        "schema": 1, "id": "openrouter", "provider": "openrouter",
        "kind": "api-key", "agents": ["opencode"], "priority": 40,
        "models": {"allow": ["openrouter/free"], "default": None}, "endpoint": None,
        "cooldown": None, "model_cooldowns": {}, "available": True,
        "available_again_at": None, "usage": OPENROUTER_USAGE, "key": CANARY,
    },
    {
        "schema": 1, "id": "ollama-cloud", "provider": "ollama-cloud",
        "kind": "api-key", "agents": ["opencode", "claude-code"], "priority": 50,
        "models": {"allow": ["*"], "default": None}, "endpoint": None,
        "cooldown": None,
        "model_cooldowns": {"m2-small": {"until": "2026-10-12T04:00:00Z"}},
        "available": True, "available_again_at": None,
        "usage": OLLAMA_USAGE, "key": CANARY,
    },
]

# roles.impl（agent=opencode、data=public）：rank 順序才是呈現順序，與 list 的
# priority 不同（zen 在 list 是 20、ollama 是 50，這裡故意把 ollama 排在中間）。
IMPL_CANDIDATES = [
    {"id": "opencode-zen", "provider": "opencode-zen", "rank": 1,
     "privacy": "public", "available": False,
     "cooldown": {"until": "2026-10-12T03:00:00Z"}, "model_cooldowns": {}},
    {"id": "ollama-cloud", "provider": "ollama-cloud", "rank": 2,
     "privacy": "real", "available": True, "cooldown": None,
     "model_cooldowns": {"m2-small": {"until": "2026-10-12T04:00:00Z"}}},
    {"id": "opencode-go", "provider": "opencode-go", "rank": 3,
     "privacy": "general", "available": True, "cooldown": None,
     "model_cooldowns": {}},
]


class FakeService:
    def __init__(self):
        self.requests = []
        self.roles_status = 200
        self.roles_error = "not_found"

    def handle(self, method, path, query, body, authorization):
        self.requests.append((method, path, query, body, authorization))
        if method == "GET" and path == "/v1/subscriptions":
            return 200, {"schema": 1,
                         "subscriptions": [dict(item) for item in SUBSCRIPTIONS]}
        if method == "GET" and path.startswith("/v1/roles/"):
            role = urllib.parse.unquote(path.rsplit("/", 1)[1])
            if self.roles_status != 200:
                return self.roles_status, {"error": self.roles_error}
            if role != "impl":
                return 404, {"error": "not_found"}
            agent = query.get("agent", [None])[0]
            if not agent:
                return 400, {"error": "bad_request", "missing": ["agent"]}
            data = query.get("data", [None])[0] or "real"
            return 200, {
                "schema": 1, "role": role, "data": data, "agent": agent,
                "candidates": [dict(item) for item in IMPL_CANDIDATES],
            }
        return 404, {"error": "not_found"}


def make_server(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _dispatch(self, method):
            parsed = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            status, payload = fake.handle(
                method, parsed.path, urllib.parse.parse_qs(parsed.query), body,
                self.headers.get("Authorization"),
            )
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

    server = ThreadingHTTPServer((HOST, 0), Handler)
    server.daemon_threads = True
    return server


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.fake = FakeService()
        self.server = make_server(self.fake)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server)
        _, port = self.server.server_address[:2]
        self.box.write(self.box.etc("llmgateway.env"), f"LGW_API_PORT={port}\n")

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def zen_line(self, out):
        return next(line for line in out.splitlines()
                    if line.startswith("opencode-zen |"))

    def test_without_role_lists_list_usage(self):
        rc, out, err = self.box.run_cli(["usage"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("claude-personal", out)
        self.assertIn("session 76.5%（reset 2026-10-25T07:00:00Z）", out)
        self.assertIn("weekly 39%（reset 2026-10-28T00:00:00Z）", out)
        self.assertIn("weekly 10%", out)          # 只有一個窗口時不補另一個
        self.assertIn("未知（no_key）", out)       # unknown 照實標，不猜數字
        # not_applicable：標「無額度 API」，整行不帶百分比（不是 100%）
        zen = self.zen_line(out)
        self.assertIn("無額度 API", zen)
        self.assertNotIn("%", zen)
        self.assertNotIn("100%", out)
        # 冷卻旁列：整把與各模型分開
        self.assertIn("整把 2026-10-12T03:00:00Z", zen)
        ollama = next(line for line in out.splitlines()
                      if line.startswith("ollama-cloud |"))
        self.assertIn("模型 m2-small 2026-10-12T04:00:00Z", ollama)
        # 沒有 key
        self.assertNotIn(CANARY, out + err)
        # list 的順序照服務（priority），沒給 role 不呼叫 roles
        self.assertEqual([req[1] for req in self.fake.requests], ["/v1/subscriptions"])

    def test_role_merges_by_id_and_orders_by_rank(self):
        rc, out, err = self.box.run_cli(
            ["usage", "--role", "impl", "--agent", "opencode", "--data", "public"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("role=impl agent=opencode data=public", out)
        self.assertIn("rank | id | provider | 額度 | 冷卻", out)
        # 順序照 roles 的 rank（zen 1、ollama 2、go 3），不是 list 的 priority
        self.assertLess(out.index("opencode-zen"), out.index("ollama-cloud"))
        self.assertLess(out.index("ollama-cloud"), out.index("opencode-go"))
        # 只列角色候選；claude-personal 不在 impl/opencode 的候選裡
        self.assertNotIn("claude-personal", out)
        # usage 仍是 list 的（roles 不帶 usage）
        self.assertIn("無額度 API", out)
        self.assertIn("未知（no_key）", out)
        self.assertIn("模型 m2-small 2026-10-12T04:00:00Z", out)
        # roles 的查詢參數與 list 都在；沒有認證標頭
        roles_req = [req for req in self.fake.requests
                     if req[1].startswith("/v1/roles/")][-1]
        self.assertEqual(roles_req[2], {"agent": ["opencode"], "data": ["public"]})
        self.assertIsNone(roles_req[4])
        self.assertNotIn(CANARY, out + err)

    def test_roles_404_is_readable_and_nonzero(self):
        self.fake.roles_status = 404
        self.fake.roles_error = "not_found"
        rc, out, err = self.box.run_cli(
            ["usage", "--role", "PMO", "--agent", "claude-code"])
        self.assertEqual(rc, 1)
        self.assertIn("404", err)
        self.assertIn("not_found", err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(out, "")

    def test_role_requires_agent(self):
        rc, out, err = self.box.run_cli(["usage", "--role", "impl"])
        self.assertEqual(rc, 2)
        self.assertIn("--agent", err)
        self.assertEqual(self.fake.requests, [])

    def test_data_requires_role(self):
        rc, out, err = self.box.run_cli(["usage", "--data", "public"])
        self.assertEqual(rc, 2)
        self.assertIn("--data", err)
        self.assertEqual(self.fake.requests, [])

    def test_unreachable_is_readable_and_nonzero(self):
        # 找一個現在沒人聽的埠（綁完就關），避免連到任何服務
        sock = socket.socket()
        sock.bind((HOST, 0))
        port = sock.getsockname()[1]
        sock.close()
        box = Sandbox()
        self.addCleanup(box.close)
        box.write(box.etc("llmgateway.env"), f"LGW_API_PORT={port}\n")
        rc, out, err = box.run_cli(["usage"])
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", err)
        self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
