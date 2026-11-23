"""`lgw/signer.py` 的測試（#222 D4a；契約 C-701～C-704、C-702、C-703、C-708、C-709）。

用真的 `ssh-keygen`、暫存目錄裡臨時產生的一次性金鑰與臨時 ssh-agent；不讀
`~/.ssh`、不碰真鑰、不連網。八行訊息另用 `tests/auth_signer.py` 的獨立組法交叉
比對（`lgw` 不 import 它，也不 import `lgwd`）。
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from lgw import signer as signer_mod
from lgw.signer import Signer, SignerError, _armor_to_b64url, build_signing_message
from tests.auth_signer import Signer as KeyFactory
from tests.auth_signer import signing_message

REPO = Path(__file__).resolve().parent.parent
VECTOR = json.loads(
    (REPO / "tests" / "vectors" / "sshsig_v1.json").read_text(encoding="utf-8"))
KEY_ID = "lgw-client-test"

#: 在**有 controlling tty** 的子行程裡跑 `Signer`（C-704 的 pty 版測試）。
#: 子行程本身用 `start_new_session=True` 起，才能 `TIOCSCTTY` 把 pty 掛成自己的
#: controlling tty；被測的是 `Signer` 內的 `ssh-keygen` 有沒有脫離這個 tty。
PTY_CHILD = r'''
import fcntl, json, os, pty, sys, termios, time
from lgw.signer import Signer, SignerError

key_path, key_id, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd_master, fd_slave = pty.openpty()
if os.getsid(0) != os.getpid():
    try:
        os.setsid()
    except OSError:
        pass
assert os.getsid(0) == os.getpid(), "child must be a session leader"
fcntl.ioctl(fd_slave, termios.TIOCSCTTY, 0)
try:
    os.close(os.open("/dev/tty", os.O_RDWR))
    controlling_tty = True
except OSError:
    controlling_tty = False
started = time.monotonic()
try:
    Signer(key_id=key_id, private_key_path=key_path, timeout=timeout).headers(
        method="GET", target="/v1/x")
except SignerError as exc:
    reason = exc.reason
except Exception as exc:
    reason = "other:" + type(exc).__name__
else:
    reason = "signed"
print(json.dumps({"controlling_tty": controlling_tty, "reason": reason,
                  "elapsed": round(time.monotonic() - started, 3)}), flush=True)
'''


def armor(blob: bytes) -> str:
    """把 blob 包成 `ssh-keygen -Y sign` 形狀的 armor（測試用）。"""
    encoded = base64.b64encode(blob).decode("ascii")
    lines = [encoded[index:index + 70] for index in range(0, len(encoded), 70)]
    return ("-----BEGIN SSH SIGNATURE-----\n" + "\n".join(lines)
            + "\n-----END SSH SIGNATURE-----\n")


def vector_blob() -> bytes:
    return base64.urlsafe_b64decode(VECTOR["signature"])


def vector_armor() -> str:
    return armor(vector_blob())


class MessageTest(unittest.TestCase):
    """C-702／C-708：八行的內容（第 7 行原樣、第 8 行 body 雜湊）。"""

    def test_C708_八行與固定向量逐byte相同(self):
        lines = VECTOR["signing_data"].splitlines()
        message = build_signing_message(
            key_id=lines[2], timestamp=lines[3], nonce=lines[4], method=lines[5],
            target=lines[6], body=b"")
        self.assertEqual(message, VECTOR["signing_data"].encode("ascii"))
        self.assertTrue(message.endswith(b"\n"))
        self.assertEqual(len(message.decode("ascii").splitlines()), 8)

    def test_C702_第7行是傳進來的target_不再編碼(self):
        target = "/v1/resolve?agent=opencode&provider=x&model=a%2Bb%2Fc%E4%B8%AD"
        message = build_signing_message(
            key_id=KEY_ID, timestamp="1", nonce="n", method="GET", target=target)
        self.assertEqual(message.decode("ascii").splitlines()[6], target)

    def test_C702_第8行是body的sha256_空body也一樣(self):
        import hashlib

        body = "中".encode("utf-8")
        lines = build_signing_message(
            key_id=KEY_ID, timestamp="1", nonce="n", method="POST",
            target="/v1/x", body=body).decode("ascii").splitlines()
        self.assertEqual(lines[7], hashlib.sha256(body).hexdigest())
        self.assertEqual(
            build_signing_message(
                key_id=KEY_ID, timestamp="1", nonce="n", method="GET",
                target="/v1/x").decode("ascii").splitlines()[7],
            hashlib.sha256(b"").hexdigest(),
        )

    def test_C702_非ASCII的target是錯誤(self):
        for target in ("/v1/\u4e2d", "/v1/x#f", "v1/x", "//v1/x", "/v1/x\n"):
            with self.subTest(target=target):
                with self.assertRaises(SignerError) as caught:
                    Signer(key_id=KEY_ID, private_key_path="/nonexistent").headers(
                        method="GET", target=target)
                self.assertEqual(caught.exception.reason, "invalid_target")

    def test_C702_body必須是bytes(self):
        with self.assertRaises(SignerError) as caught:
            build_signing_message(
                key_id=KEY_ID, timestamp="1", nonce="n", method="GET",
                target="/v1/x", body="not-bytes")
        self.assertEqual(caught.exception.reason, "invalid_body")


class ArmorTest(unittest.TestCase):
    """C-703／C-708：armor → 固定 240 字元的 base64url。"""

    def test_C708_固定向量重算(self):
        self.assertEqual(len(vector_blob()), 180)
        self.assertEqual(_armor_to_b64url(vector_armor()), VECTOR["signature"])
        self.assertEqual(len(_armor_to_b64url(vector_armor())), 240)

    def test_C703_179與181bytes都拒絕(self):
        for size in (179, 181):
            with self.subTest(size=size):
                with self.assertRaises(SignerError) as caught:
                    _armor_to_b64url(armor(b"\x00" * size))
                self.assertEqual(caught.exception.reason, "bad_signature")

    def test_C703_壞base64與壞標記都拒絕(self):
        bad = [
            "-----BEGIN SSH SIGNATURE-----\n!!!\n-----END SSH SIGNATURE-----\n",
            "not an armor\n",
            "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(
                b"\x00" * 180).decode() + "\n-----END SSH SIGNATURE-----\nEXTRA\n",
        ]
        for text in bad:
            with self.subTest(text=text[:20]):
                with self.assertRaises(SignerError) as caught:
                    _armor_to_b64url(text)
                self.assertEqual(caught.exception.reason, "bad_signature")


class SubprocessTest(unittest.TestCase):
    """C-701：argv、shell、stdin、固定環境與逾時。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.key = KeyFactory(self.dir, "k")
        self.private = self.key.private_path

    def _fake_run(self):
        armor_bytes = vector_armor().encode("ascii")
        return mock.patch(
            "lgw.signer.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=armor_bytes, stderr=b""),
        )

    def test_C701_argv固定_shell為False_訊息走stdin(self):
        signer = Signer(key_id=KEY_ID, private_key_path=self.private)
        body = '{"until":"2026-11-23T18:00:00Z"}'.encode("utf-8")
        with self._fake_run() as run:
            headers = signer.headers(method="POST", target="/v1/x?a=1", body=body,
                                     now=1000)
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            ["/usr/bin/ssh-keygen", "-Y", "sign", "-n", "llmgateway", "-f", self.private],
        )
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["input"], signing_message(
            key_id=KEY_ID, timestamp=1000, nonce=headers["X-LGW-Nonce"],
            method="POST", target="/v1/x?a=1", body=body))
        self.assertEqual(int(kwargs["timeout"]), 5)
        self.assertEqual(headers["X-LGW-Timestamp"], "1000")
        self.assertEqual(headers["X-LGW-Signature"], VECTOR["signature"])

    def test_C701_環境恰為三個鍵_不繼承SSH_AUTH_SOCK(self):
        signer = Signer(key_id=KEY_ID, private_key_path=self.private)
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/not-used"}):
            with self._fake_run() as run:
                signer.headers(method="GET", target="/v1/x", now=1)
        env = run.call_args.kwargs["env"]
        self.assertEqual(set(env), {"LC_ALL", "PATH", "HOME"})
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["HOME"], self.dir)

    def test_C704_agent模式環境多SSH_AUTH_SOCK且用公鑰檔(self):
        public = self.key.public_path
        signer = Signer(key_id=KEY_ID, public_key_path=public,
                        agent_sock="/tmp/agent-test.sock")
        with self._fake_run() as run:
            signer.headers(method="GET", target="/v1/x", now=1)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], public)
        self.assertEqual(set(kwargs["env"]),
                         {"LC_ALL", "PATH", "HOME", "SSH_AUTH_SOCK"})
        self.assertEqual(kwargs["env"]["SSH_AUTH_SOCK"], "/tmp/agent-test.sock")

    def test_C704_公鑰模式沒有agent就失敗(self):
        signer = Signer(key_id=KEY_ID, public_key_path=self.key.public_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SignerError) as caught:
                signer.headers(method="GET", target="/v1/x")
        self.assertEqual(caught.exception.reason, "agent_unavailable")

    def test_C701_ssh_keygen不存在(self):
        signer = Signer(key_id=KEY_ID, private_key_path=self.private,
                        sshkeygen="/nonexistent/ssh-keygen")
        with self.assertRaises(SignerError) as caught:
            signer.headers(method="GET", target="/v1/x")
        self.assertEqual(caught.exception.reason, "sshkeygen_missing")

    def test_C701_逾時會殺掉子行程(self):
        pid_file = os.path.join(self.dir, "pid")
        script = os.path.join(self.dir, "slow-ssh-keygen")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "#!{0}\nimport os, time\nopen({1!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(30)\n".format(sys.executable, pid_file))
        os.chmod(script, stat.S_IRWXU)
        signer = Signer(key_id=KEY_ID, private_key_path=self.private,
                        sshkeygen=script, timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(SignerError) as caught:
            signer.headers(method="GET", target="/v1/x")
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(time.monotonic() - started, 5)
        pid = int(Path(pid_file).read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class RealKeyTest(unittest.TestCase):
    """C-704：真的私鑰檔、有密語的私鑰、公鑰檔＋臨時 ssh-agent。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_C703_真簽章是240字元且解回180bytes(self):
        key = KeyFactory(self.dir, "k")
        signer = Signer(key_id=KEY_ID, private_key_path=key.private_path)
        headers = signer.headers(method="GET", target="/v1/subscriptions", now=1)
        signature = headers["X-LGW-Signature"]
        self.assertEqual(len(signature), 240)
        self.assertEqual(len(base64.urlsafe_b64decode(signature)), 180)
        self.assertEqual(len(headers["X-LGW-Nonce"]), 43)

    def test_C704_有密語的私鑰直接失敗不等待輸入(self):
        path = os.path.join(self.dir, "enc")
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "secret-pass",
             "-C", "", "-f", path],
            check=True, capture_output=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": self.dir},
            timeout=30,
        )
        signer = Signer(key_id=KEY_ID, private_key_path=path, timeout=5.0)
        started = time.monotonic()
        with self.assertRaises(SignerError) as caught:
            signer.headers(method="GET", target="/v1/x")
        self.assertEqual(caught.exception.reason, "sign_failed")
        self.assertLess(time.monotonic() - started, 5)
        # 不把 ssh-keygen 的 stderr（含提示字樣）放進例外。
        self.assertNotIn("passphrase", str(caught.exception).lower())

    def test_C704_有controlling_tty時有密語的私鑰也直接失敗(self):
        """呼叫端有 controlling tty 時，有密語的私鑰仍立即 `sign_failed`（不提示）。

        #266 審查中項：以前 `ssh-keygen` 留在呼叫端的 session 裡，有 tty 時它會開
        `/dev/tty` 印 `Enter passphrase:` 並阻塞到逾時（`timeout`）；`Signer` 現在
        以 `start_new_session=True` 起子行程，pty 版測試守住這件事。
        """
        path = os.path.join(self.dir, "enc-tty")
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "secret-pass",
             "-C", "", "-f", path],
            check=True, capture_output=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": self.dir},
            timeout=30,
        )
        script = os.path.join(self.dir, "pty-child.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(PTY_CHILD)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        completed = subprocess.run(
            [sys.executable, script, path, KEY_ID, "3.0"],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
            # 讓子行程當 session leader，它才能把 pty 掛成 controlling tty。
            start_new_session=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(result["controlling_tty"], result)
        self.assertEqual(result["reason"], "sign_failed", result)
        self.assertLess(result["elapsed"], 2.5, result)

    def test_C704_公鑰檔加臨時ssh_agent可以簽(self):
        key = KeyFactory(self.dir, "agent-key")
        sock = os.path.join(self.dir, "agent.sock")
        agent = subprocess.Popen(
            ["/usr/bin/ssh-agent", "-D", "-a", sock],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_agent, agent)
        self._wait_socket(sock)
        subprocess.run(
            ["/usr/bin/ssh-add", key.private_path], check=True, capture_output=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": self.dir,
                 "SSH_AUTH_SOCK": sock},
            timeout=30,
        )
        signer = Signer(key_id=KEY_ID, public_key_path=key.public_path,
                        agent_sock=sock)
        headers = signer.headers(method="GET", target="/v1/x", now=1)
        self.assertEqual(len(headers["X-LGW-Signature"]), 240)

    def test_C704_兩種來源只能給一個(self):
        with self.assertRaises(ValueError):
            Signer(key_id=KEY_ID)
        with self.assertRaises(ValueError):
            Signer(key_id=KEY_ID, private_key_path="/a", public_key_path="/b")

    @staticmethod
    def _wait_socket(sock: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.exists(sock):
                return
            time.sleep(0.02)
        raise AssertionError("ssh-agent socket did not appear")

    @staticmethod
    def _stop_agent(agent) -> None:
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait(timeout=5)


class LeakTest(unittest.TestCase):
    """C-709：repr／例外不含簽章、nonce、公鑰或私鑰內容。"""

    def test_C709_repr只出現路徑與ssh_keygen(self):
        signer = Signer(key_id="very-secret-key-id", private_key_path="/tmp/private-key")
        text = repr(signer)
        self.assertIn("/tmp/private-key", text)
        self.assertNotIn("very-secret-key-id", text)
        self.assertNotIn("nonce", text.lower())

    def test_C709_armor錯誤的例外不含內容(self):
        canary = b"CANARY-SECRET-BYTES"
        with self.assertRaises(SignerError) as caught:
            _armor_to_b64url(armor(canary))
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertNotIn("CANARY", repr(caught.exception))
        self.assertNotIn(base64.b64encode(canary).decode(), str(caught.exception))

    def test_C709_repr不印detail(self):
        error = SignerError("bad_signature", "CANARY-DETAIL")
        self.assertNotIn("CANARY", repr(error))
        self.assertEqual(error.reason, "bad_signature")

    def test_C709_版號與空相依(self):
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["version"], "0.2.0")
        self.assertEqual(data["project"].get("dependencies", []), [])

    def test_C709_lgw不import_lgwd或tests(self):
        import ast
        for path in sorted((REPO / "lgw").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    roots = [node.module.split(".")[0]] if node.module else []
                else:
                    continue
                for root in roots:
                    self.assertNotIn(root, {"lgwd", "tests"},
                                     "{0}: {1}".format(path.name, root))


if __name__ == "__main__":
    unittest.main()
