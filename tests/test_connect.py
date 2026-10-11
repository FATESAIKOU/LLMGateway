"""lgw/connect.py 的測試（tasks 3.1c）：假 ssh 腳本；不 ssh 真機器、不用真金鑰。

假 ssh（測試當場產生、可執行）：
- ok：記 argv，在 -L 指定的本機埠開一個小 HTTP server 冒充轉發，直到被關掉；
- fail：記 argv 後立刻結束；
- hang：記 argv 後永遠不開埠。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from lgw.client import Client
from lgw.connect import ConnectConfig, ConfigError, Unavailable, connect, load_config

REPO = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"

FAKE_OK = '''#!/usr/bin/env python3
"""假的 ssh（測試用）：記錄 argv；在 -L 的本機埠開 HTTP server 冒充轉發。"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__ + ".argv")
PID = Path(__file__ + ".pid")


def local_port(argv):
    for i, arg in enumerate(argv):
        if arg == "-L":
            return int(argv[i + 1].split(":")[1])
    raise SystemExit("fake ssh: no -L")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/subscriptions":
            self._json(200, {"schema": 1, "subscriptions": []})
        elif path.startswith("/v1/subscriptions/"):
            self._json(200, {"schema": 1, "id": path.rsplit("/", 1)[1],
                             "key": "fake-key-value"})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    argv = sys.argv
    LOG.write_text(json.dumps(argv), encoding="utf-8")
    PID.write_text(str(os.getpid()), encoding="utf-8")
    ThreadingHTTPServer(("127.0.0.1", local_port(argv)), Handler).serve_forever()


main()
'''

FAKE_FAIL = '''#!/usr/bin/env python3
"""假的 ssh（測試用）：記錄 argv 後立刻失敗。"""
import json
import sys
from pathlib import Path

Path(__file__ + ".argv").write_text(json.dumps(sys.argv), encoding="utf-8")
raise SystemExit(1)
'''

FAKE_HANG = '''#!/usr/bin/env python3
"""假的 ssh（測試用）：記錄 argv 後永遠不開埠（等被關掉）。"""
import json
import os
import signal
import sys
from pathlib import Path

Path(__file__ + ".argv").write_text(json.dumps(sys.argv), encoding="utf-8")
Path(__file__ + ".pid").write_text(str(os.getpid()), encoding="utf-8")
signal.pause()
'''


class ConnectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ssh_ok = self._script("fake-ssh-ok", FAKE_OK)
        self.ssh_fail = self._script("fake-ssh-fail", FAKE_FAIL)
        self.ssh_hang = self._script("fake-ssh-hang", FAKE_HANG)
        self.cfg = ConnectConfig(
            jumps=["jump1@hop1:2200", "jump2@hop2:2200"],
            rendezvous="tester@fake-rendezvous:2222",
            identity=str(self.root / "id_fake"),
            known_hosts=str(self.root / "known_hosts_fake"),
            ssh_path=str(self.ssh_ok),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _argv_of(self, script):
        return json.loads(Path(str(script) + ".argv").read_text(encoding="utf-8"))

    def _assert_dead(self, script):
        """斷言假 ssh 的子行程已經結束（讀它開頭寫的 pid 檔）。"""
        pid_path = Path(str(script) + ".pid")
        deadline = time.monotonic() + 5
        while not pid_path.exists():
            if time.monotonic() > deadline:
                self.fail("fake ssh never wrote its pid")
            time.sleep(0.02)
        pid = int(pid_path.read_text(encoding="utf-8"))
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("fake ssh pid {0} still alive".format(pid))


class ConnectTest(ConnectCase):
    def test_argv_and_base_url(self):
        with connect(self.cfg) as base_url:
            self.assertRegex(base_url, r"^http://127\.0\.0\.1:\d+$")
            port = int(base_url.rsplit(":", 1)[1])
            with urllib.request.urlopen(
                base_url + "/v1/subscriptions", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read())["schema"], 1)
            argv = self._argv_of(self.ssh_ok)
        # argv：ssh -N -L 127.0.0.1:<臨時埠>:127.0.0.1:4710 [-J …] -i … -o … -p … user@host
        self.assertEqual(argv[0], str(self.ssh_ok))
        self.assertIn("-N", argv)
        self.assertEqual(
            argv[argv.index("-L") + 1],
            "127.0.0.1:{0}:127.0.0.1:4710".format(port),
        )
        self.assertEqual(
            argv[argv.index("-J") + 1], "jump1@hop1:2200,jump2@hop2:2200"
        )
        self.assertEqual(argv[argv.index("-i") + 1], str(self.root / "id_fake"))
        self.assertIn(
            "UserKnownHostsFile={0}".format(self.root / "known_hosts_fake"), argv
        )
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("ServerAliveInterval=30", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[-1], "tester@fake-rendezvous")
        self.assertNotIn("accept-new", " ".join(argv))
        self._assert_dead(self.ssh_ok)  # 離開後 ssh 子行程已結束

    def test_without_jumps_omits_J(self):
        cfg = ConnectConfig(
            jumps=[],
            rendezvous=self.cfg.rendezvous,
            identity=self.cfg.identity,
            known_hosts=self.cfg.known_hosts,
            ssh_path=str(self.ssh_ok),
        )
        with connect(cfg) as base_url:
            argv = self._argv_of(self.ssh_ok)
            self.assertNotIn("-J", argv)
            with urllib.request.urlopen(
                base_url + "/v1/subscriptions", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
        self._assert_dead(self.ssh_ok)

    def test_client_through_tunnel(self):
        """list／get／登錄冷卻都走 connect 回的位址。"""
        with connect(self.cfg) as base_url:
            client = Client(base_url)
            res = client.list()
            self.assertTrue(res.ok)
            self.assertEqual(res.data["schema"], 1)
            res = client.get("claude-personal", "claude-code")
            self.assertTrue(res.ok)
            self.assertEqual(res.data["key"], "fake-key-value")
            res = client.cooldown("claude-personal", "5h")
            self.assertTrue(res.ok)
            self.assertEqual(res.status, 204)
        self._assert_dead(self.ssh_ok)

    def test_ssh_exits_immediately(self):
        cfg = ConnectConfig(
            rendezvous="tester@fake-secret-host:2222",
            identity=self.cfg.identity,
            known_hosts=self.cfg.known_hosts,
            ssh_path=str(self.ssh_fail),
        )
        with self.assertRaises(Unavailable) as ctx:
            with connect(cfg, timeout=5):
                self.fail("connect 不該成功")
        self.assertEqual(ctx.exception.kind, "ssh_exited")
        text = str(ctx.exception)
        self.assertNotIn("fake-secret-host", text)  # 訊息不含主機名
        self.assertNotIn("2222", text)
        self.assertEqual(self._argv_of(self.ssh_fail)[-1], "tester@fake-secret-host")

    def test_timeout(self):
        cfg = ConnectConfig(
            rendezvous="tester@fake-hang-host:2222",
            identity=self.cfg.identity,
            known_hosts=self.cfg.known_hosts,
            ssh_path=str(self.ssh_hang),
        )
        start = time.monotonic()
        with self.assertRaises(Unavailable) as ctx:
            with connect(cfg, timeout=1.5):
                self.fail("connect 不該成功")
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertNotIn("fake-hang-host", str(ctx.exception))
        self.assertLess(elapsed, 5)
        self._assert_dead(self.ssh_hang)  # 逾時也要關掉 ssh

    def test_ssh_missing(self):
        cfg = ConnectConfig(
            rendezvous=self.cfg.rendezvous,
            identity=self.cfg.identity,
            known_hosts=self.cfg.known_hosts,
            ssh_path=str(self.root / "no-such-ssh"),
        )
        with self.assertRaises(Unavailable) as ctx:
            with connect(cfg, timeout=1):
                self.fail("connect 不該成功")
        self.assertEqual(ctx.exception.kind, "ssh_missing")

    def test_bad_config(self):
        with self.assertRaises(Unavailable) as ctx:
            with connect(ConnectConfig(ssh_path=str(self.ssh_ok)), timeout=1):
                self.fail("connect 不該成功")
        self.assertEqual(ctx.exception.kind, "bad_config")

    def test_port_freed_after_exit(self):
        with connect(self.cfg) as base_url:
            port = int(base_url.rsplit(":", 1)[1])
        sock = socket.socket()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HOST, port))
            self.assertEqual(sock.getsockname()[1], port)
        finally:
            sock.close()


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "connect.env"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_full(self):
        self._write(
            "# 註解\n"
            "\n"
            "JUMPS=jump1@hop1:2200, jump2@hop2:2200\n"
            "RENDEZVOUS=sshproxy@fake-rendezvous:2222\n"
            "IDENTITY=/tmp/fake-id\n"
            "KNOWN_HOSTS=/tmp/fake-known-hosts\n"
            "REMOTE_API=5000\n"
        )
        cfg = load_config(self.path)
        self.assertEqual(cfg.jumps, ["jump1@hop1:2200", "jump2@hop2:2200"])
        self.assertEqual(cfg.rendezvous, "sshproxy@fake-rendezvous:2222")
        self.assertEqual(cfg.identity, "/tmp/fake-id")
        self.assertEqual(cfg.known_hosts, "/tmp/fake-known-hosts")
        self.assertEqual(cfg.remote_api, 5000)
        self.assertEqual(cfg.ssh_path, "ssh")

    def test_defaults(self):
        self._write(
            "JUMPS=\n"
            "RENDEZVOUS=sshproxy@fake-rendezvous:2222\n"
            "IDENTITY=/tmp/fake-id\n"
            "KNOWN_HOSTS=/tmp/fake-known-hosts\n"
        )
        cfg = load_config(self.path)
        self.assertEqual(cfg.jumps, [])
        self.assertEqual(cfg.remote_api, 4710)

    def test_unknown_key_no_value(self):
        self._write(
            "RENDEZVOUS=sshproxy@fake-rendezvous:2222\n"
            "IDENTITY=/tmp/fake-id\n"
            "KNOWN_HOSTS=/tmp/fake-known-hosts\n"
            "SECRET_THING=canary-value-zz9\n"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        text = str(ctx.exception)
        self.assertIn("line 4", text)
        self.assertIn("SECRET_THING", text)  # 只寫行號與鍵名
        self.assertNotIn("canary-value-zz9", text)  # 不印值

    def test_bad_rendezvous_no_value(self):
        self._write(
            "RENDEZVOUS=no-port-here\n"
            "IDENTITY=/tmp/fake-id\n"
            "KNOWN_HOSTS=/tmp/fake-known-hosts\n"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("RENDEZVOUS", str(ctx.exception))
        self.assertNotIn("no-port-here", str(ctx.exception))

    def test_bad_jumps_no_value(self):
        self._write(
            "JUMPS=jump1@hop1:2200,no-port-here\n"
            "RENDEZVOUS=sshproxy@fake-rendezvous:2222\n"
            "IDENTITY=/tmp/fake-id\n"
            "KNOWN_HOSTS=/tmp/fake-known-hosts\n"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("JUMPS", str(ctx.exception))
        self.assertNotIn("no-port-here", str(ctx.exception))

    def test_bad_remote_api_no_value(self):
        self._write(
            "RENDEZVOUS=sshproxy@fake-rendezvous:2222\n"
            "IDENTITY=/tmp/fake-id\n"
            "KNOWN_HOSTS=/tmp/fake-known-hosts\n"
            "REMOTE_API=not-a-number\n"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("REMOTE_API", str(ctx.exception))
        self.assertNotIn("not-a-number", str(ctx.exception))

    def test_missing_and_not_keyvalue(self):
        self._write("RENDEZVOUS=sshproxy@fake-rendezvous:2222\nIDENTITY=/tmp/fake-id\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("KNOWN_HOSTS", str(ctx.exception))
        self._write("JUSTAKEY\nRENDEZVOUS=x\nIDENTITY=x\nKNOWN_HOSTS=x\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("line 1", str(ctx.exception))


class CliTest(ConnectCase):
    def _config_env(self, rendezvous):
        return self._write_config(
            "JUMPS=\n"
            "RENDEZVOUS={0}\n"
            "IDENTITY={1}\n"
            "KNOWN_HOSTS={2}\n"
            "REMOTE_API=4710\n".format(
                rendezvous, self.root / "id_fake", self.root / "known_hosts_fake"
            )
        )

    def _write_config(self, text):
        path = self.root / "connect.env"
        path.write_text(text, encoding="utf-8")
        return path

    def _bindir_with_ssh(self, script):
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        target = bindir / "ssh"
        target.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
        return bindir

    def _run_cli(self, config_path, ssh_script):
        bindir = self._bindir_with_ssh(ssh_script)
        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            [sys.executable, "-m", "lgw.connect", "--config", str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO),
            env=env,
        )

    def test_cli_prints_base_url_and_stops_on_sigint(self):
        config_path = self._config_env("tester@fake-rendezvous:2222")
        proc = self._run_cli(config_path, self.ssh_ok)
        try:
            line = proc.stdout.readline()
            self.assertRegex(line.strip(), r"^http://127\.0\.0\.1:\d+$")
            base_url = line.strip()
            with urllib.request.urlopen(
                base_url + "/v1/subscriptions", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
            proc.send_signal(signal.SIGINT)  # Ctrl-C
            self.assertEqual(proc.wait(timeout=10), 0)
            err = proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(err, "")
        self._assert_dead(self.root / "bin" / "ssh")

    def test_cli_unavailable(self):
        config_path = self._config_env("tester@fake-secret-host:2222")
        proc = self._run_cli(config_path, self.ssh_fail)
        rc = proc.wait(timeout=10)
        self.assertNotEqual(rc, 0)
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        self.assertIn("unavailable: ssh_exited", err)
        self.assertNotIn("fake-secret-host", err)  # 錯誤不印位址


if __name__ == "__main__":
    unittest.main()
