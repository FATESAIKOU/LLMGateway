"""整合測試（PM 指派）：真 LeaseService＋make_api_server＋LeaseClient 全流程。

- 真 service（暫存 SQLite、FakeClock、測試 key 檔、policy/default.json）。
- key 檔的值全是假 canary；絕不用真 key。
- LeaseService（impl）完成前：建構或呼叫遇到 NotImplementedError 就 skip。
"""

import functools
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from lgw.client import LeaseClient
from lgwd.clock import FakeClock, to_rfc3339
from lgwd.http import make_api_server
from lgwd.keys import KeyStore
from lgwd.leasing import LeaseService
from lgwd.policy import load_policy

HOST = "127.0.0.1"

# 測試 key 檔（假 canary；鍵名見 lgwd.keys.KEY_NAMES）
PERSONAL_KEY = "test-key-personal-001"
COMPANY_KEY = "test-key-company-002"
KEY_FILE_TEXT = (
    "# 整合測試用假 key\n"
    "CLAUDE_PERSONAL_OAUTH_TOKEN={0}\n"
    "CLAUDE_COMPANY_OAUTH_TOKEN={1}\n"
    "OPENROUTER_API_KEY=test-key-openrouter-003\n"
    "OPENCODE_ZEN_API_KEY=test-key-zen-004\n"
    "OLLAMA_API_KEY=test-key-ollama-005\n"
).format(PERSONAL_KEY, COMPANY_KEY)

POLICY_PATH = str(Path(__file__).resolve().parent.parent / "policy" / "default.json")


def _needs_impl(fn):
    """service 半成品時：中途遇到 NotImplementedError 就 skip，不算失敗。」"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except NotImplementedError:
            self.skipTest("LeaseService 未完成（impl 實作中）")
    return wrapper


class IntegrationCase(unittest.TestCase):
    def setUp(self):
        try:
            self.tmp = tempfile.TemporaryDirectory()
            root = Path(self.tmp.name)
            (root / "keys.env").write_text(KEY_FILE_TEXT, encoding="utf-8")
            keys = KeyStore(str(root / "keys.env"))
            policy = load_policy(POLICY_PATH)
            self.clock = FakeClock()
            self.service = LeaseService(
                str(root / "llm.db"), policy, keys, self.clock
            )
            token = self.service.add_client("worker-a")
            token_path = root / "client-token"
            token_path.write_text(token + "\n", encoding="utf-8")
            self.server = make_api_server(self.service, HOST, 0)
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            self.thread.start()
            _, port = self.server.server_address[:2]
            self.client = LeaseClient(
                "http://{0}:{1}".format(HOST, port), str(token_path)
            )
        except NotImplementedError:
            self.skipTest("LeaseService 未完成（impl 實作中）")

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def _request(self, instance="i1", **kwargs):
        params = {"worker": "worker-a", "role": "coder", "agent": "claude-code",
                  "instance": instance}
        params.update(kwargs)
        return self.client.request_lease(**params)

    def _sub(self, name):
        board = self.client.board()
        self.assertTrue(board.ok)
        for sub in board.data["subscriptions"]:
            if sub["name"] == name:
                return sub
        self.fail("board 缺少 " + name)


class FullFlowTest(IntegrationCase):
    @_needs_impl
    def test_flow(self):
        # 申請→personal；env 帶個人假 key
        res = self._request()
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 201)
        self.assertEqual(res.data["lease"]["subscription"], "claude-personal")
        self.assertEqual(
            res.data["lease"]["env"], {"CLAUDE_CODE_OAUTH_TOKEN": PERSONAL_KEY}
        )
        self.assertEqual(
            set(res.data["lease"]["unset"]),
            {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"},
        )
        lease_id = res.data["lease"]["lease_id"]

        # start→active
        res = self.client.start(lease_id)
        self.assertTrue(res.ok)
        self.assertIn("expires_at", res.data)

        # exhausted（重置 1 小時後）→冷卻到 +2 分鐘，下一張是 company
        resets = to_rfc3339(self.clock.now() + timedelta(hours=1))
        res = self.client.exhausted(lease_id, "claude_limit", resets)
        self.assertTrue(res.ok)
        self.assertEqual(res.data["next"]["subscription"], "claude-company")
        self.assertEqual(
            res.data["next"]["env"], {"CLAUDE_CODE_OAUTH_TOKEN": COMPANY_KEY}
        )
        next_id = res.data["next"]["lease_id"]

        # 新租約 start→renew→release
        self.assertTrue(self.client.start(next_id).ok)
        res = self.client.renew(next_id)
        self.assertTrue(res.ok)
        self.assertIn("expires_at", res.data)
        res = self.client.release(next_id, reason="done")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, 204)

        # board：personal 冷卻到 01:02，值不外洩
        personal = self._sub("claude-personal")
        self.assertTrue(personal["cooling"])
        self.assertEqual(personal["cooling_until"], "2026-10-11T01:02:00Z")
        company = self._sub("claude-company")
        self.assertFalse(company["cooling"])
        dumped = json.dumps(self.client.board().data)
        self.assertNotIn(PERSONAL_KEY, dumped)
        self.assertNotIn(COMPANY_KEY, dumped)

    @_needs_impl
    def test_whoami(self):
        res = self.client.whoami()
        self.assertTrue(res.ok)
        self.assertEqual(res.data, {"client": "worker-a"})


class RefusalTest(IntegrationCase):
    @_needs_impl
    def test_401(self):
        bad_path = str(Path(self.tmp.name) / "bad-token")
        Path(bad_path).write_text("nope\n", encoding="utf-8")
        bad = LeaseClient(self.client._base_url, bad_path)
        res = bad.request_lease("worker-a", "coder", "claude-code", "i9")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 401)
        res = bad.board()
        self.assertEqual(res.status, 401)

    @_needs_impl
    def test_client_mismatch(self):
        res = self._request(worker="worker-b")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 403)
        self.assertEqual(res.error, "client_mismatch")

    @_needs_impl
    def test_instance_busy(self):
        res = self._request(instance="i1")
        self.assertTrue(res.ok)
        self.assertTrue(self.client.start(res.data["lease"]["lease_id"]).ok)
        res = self._request(instance="i1")  # 同 instance 第二張
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 409)
        self.assertIsNotNone(res.refusal)
        self.assertEqual(res.refusal.reason, "instance_busy")

    @_needs_impl
    def test_all_cooling(self):
        # 兩把都送進冷卻：company 重置較早，所以最早結束是它
        res = self._request(instance="i1")
        self.assertTrue(self.client.start(res.data["lease"]["lease_id"]).ok)
        early = to_rfc3339(self.clock.now() + timedelta(hours=1))
        late = to_rfc3339(self.clock.now() + timedelta(hours=2))
        res = self.client.exhausted(res.data["lease"]["lease_id"], "claude_limit", late)
        self.assertEqual(res.data["next"]["subscription"], "claude-company")
        company_id = res.data["next"]["lease_id"]
        self.assertTrue(self.client.start(company_id).ok)
        res = self.client.exhausted(company_id, "claude_limit", early)
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.refusal)
        self.assertEqual(res.refusal.reason, "all_cooling")
        self.assertEqual(res.refusal.earliest_reset, "2026-10-11T01:02:00Z")
        # 再申請也一樣被拒，附同一個最早結束
        res = self._request(instance="i2")
        self.assertFalse(res.ok)
        self.assertEqual(res.refusal.reason, "all_cooling")
        self.assertEqual(res.refusal.earliest_reset, "2026-10-11T01:02:00Z")


if __name__ == "__main__":
    unittest.main()
