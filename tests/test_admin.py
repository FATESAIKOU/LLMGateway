"""lgw/admin.py 的測試：假 key 檔、真 policy/default.json、隨機埠。"""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from lgw import admin
from lgwd.http import make_api_server
from lgwd.keys import KEY_NAMES

REPO = Path(__file__).resolve().parent.parent
POLICY = str(REPO / "policy" / "default.json")
FAKE_KEYS = {sub: f"test-admin-{sub}-000" for sub in
             ("claude-personal", "claude-company", "openrouter",
              "opencode-zen", "ollama-cloud")}


def key_text(values):
    lines = ["# 測試用假 key"]
    for sub, name in KEY_NAMES.items():
        if sub in values:
            lines.append(f"{name}={values[sub]}")
    return "\n".join(lines) + "\n"


def whoami(base, token):
    req = urllib.request.Request(base + "/v1/whoami",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class AdminCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "keys.env").write_text(key_text(FAKE_KEYS), encoding="utf-8")
        self.db = str(self.root / "llm.db")
        self.flags = ["--db", self.db, "--policy", POLICY,
                      "--keys", str(self.root / "keys.env")]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = admin.main([*self.flags, *argv])
        return rc, out.getvalue(), err.getvalue()

    # --- add／list ---
    def test_add輸出格式(self):
        rc, out, err = self._run("client", "add", "w1")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.endswith("\n"))
        (token,) = out.split()
        self.assertRegex(token, r"^[A-Za-z0-9_-]+$")
        self.assertNotIn(token, err)

    def test_add重複(self):
        self._run("client", "add", "w1")
        rc, _, _ = self._run("client", "add", "w1")
        self.assertEqual(rc, 1)

    def test_list不印雜湊(self):
        rc, out, _ = self._run("client", "add", "w1")
        token = out.split()[0]
        rc, out, _ = self._run("client", "list")
        self.assertEqual(rc, 0)
        self.assertIn("w1", out)
        self.assertNotIn(token, out)
        self.assertNotIn("cred_hash", out)
        for line in out.strip().split("\n"):
            self.assertEqual(len(line.split("\t")), 4)  # name、role、created_at、revoked_at

    def test_add_拒絕worker前綴(self):
        rc, out, err = self._run("client", "add", "worker-mac")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("admin API", err)

    # --- rm 與 whoami ---
    def test_rm之後whoami回401(self):
        rc, out, _ = self._run("client", "add", "w1")
        token = out.split()[0]
        service = admin.open_service(self.db, POLICY, str(self.root / "keys.env"))
        server = make_api_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            base = f"http://{host}:{port}"
            self.assertEqual(whoami(base, token), 200)
            rc, _, _ = self._run("client", "rm", "w1")
            self.assertEqual(rc, 0)
            self.assertEqual(whoami(base, token), 401)
            rc, _, _ = self._run("client", "rm", "w1")  # 重複撤銷不報錯
            self.assertEqual(rc, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            service.close()

    def test_rm未知客戶端(self):
        rc, _, err = self._run("client", "rm", "ghost")
        self.assertEqual(rc, 1)
        self.assertIn("unknown client", err)

    def test_revoke同義(self):
        self._run("client", "add", "w1")
        rc, _, _ = self._run("client", "revoke", "w1")
        self.assertEqual(rc, 0)

    def test_python_m_lgw_admin_help(self):
        proc = subprocess.run([sys.executable, "-m", "lgw.admin", "--help"],
                              capture_output=True, timeout=30, cwd=str(REPO))
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"usage: lgw.admin", proc.stdout)


if __name__ == "__main__":
    unittest.main()
