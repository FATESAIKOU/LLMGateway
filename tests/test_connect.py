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
import stat
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

    def _config_path(self, argv):
        self.assertEqual(argv.count("-F"), 1)
        return Path(argv[argv.index("-F") + 1])

    def _config_text(self, argv):
        return self._config_path(argv).read_text(encoding="utf-8")

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


def _ssh_config(identity, known_hosts_path):
    return (
        "Host *\n"
        "    IdentityFile \"{0}\"\n"
        "    IdentitiesOnly yes\n"
        "    UserKnownHostsFile \"{1}\"\n"
        "    GlobalKnownHostsFile /dev/null\n"
        "    StrictHostKeyChecking yes\n"
        "    BatchMode yes\n"
        "    ExitOnForwardFailure yes\n"
        "    ServerAliveInterval 30\n"
        "    ServerAliveCountMax 3\n"
    ).format(identity, known_hosts_path)


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
            config_path = self._config_path(argv)
            temp_dir = config_path.parent
            # ssh -F <ssh_config> -N -L … [-J …] -p … user@host；不再有 -i／-o
            self.assertEqual(argv[0], str(self.ssh_ok))
            self.assertEqual(argv[1], "-F")
            self.assertIn("-N", argv)
            self.assertEqual(
                argv[argv.index("-L") + 1],
                "127.0.0.1:{0}:127.0.0.1:4710".format(port),
            )
            self.assertEqual(
                argv[argv.index("-J") + 1], "jump1@hop1:2200,jump2@hop2:2200"
            )
            self.assertEqual(argv[argv.index("-p") + 1], "2222")
            self.assertEqual(argv[-1], "tester@fake-rendezvous")
            self.assertNotIn("-i", argv)
            self.assertFalse([a for a in argv if "=" in a and "UserKnownHosts" in a])
            self.assertNotIn("StrictHostKeyChecking=yes", argv)
            self.assertNotIn("accept-new", " ".join(argv))
            # ssh_config：內容、0700 目錄、0600 檔
            self.assertEqual(
                self._config_text(argv),
                _ssh_config(self.cfg.identity, self.cfg.known_hosts),
            )
            self.assertTrue(temp_dir.name.startswith("lgw-connect-"))
            self.assertEqual(stat.S_IMODE(temp_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertFalse(temp_dir.exists())  # 結束後整個暫存目錄刪掉
        self._assert_dead(self.ssh_ok)  # 離開後 ssh 子行程已結束

    def test_ssh_config_quotes_paths_with_spaces(self):
        spaced = self.root / "dir with space"
        spaced.mkdir()
        identity = spaced / "id fake"
        identity.write_text("fake", encoding="utf-8")
        known_hosts = spaced / "known_hosts fake"
        known_hosts.write_text("[fake-rendezvous]:2222 ssh-ed25519 A\n",
                               encoding="utf-8")
        cfg = ConnectConfig(
            rendezvous=self.cfg.rendezvous,
            identity=str(identity),
            known_hosts=str(known_hosts),
            ssh_path=str(self.ssh_ok),
        )
        with connect(cfg) as base_url:
            argv = self._argv_of(self.ssh_ok)
            text = self._config_text(argv)
            self.assertIn('IdentityFile "{0}"'.format(identity), text)
            self.assertIn('UserKnownHostsFile "{0}"'.format(known_hosts), text)
            with urllib.request.urlopen(
                base_url + "/v1/subscriptions", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
        self._assert_dead(self.ssh_ok)

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

    def test_host_keys_temp_file_0600_and_cleanup(self):
        host_keys = [
            "[fake-rendezvous]:2222 ssh-ed25519 AAAAFAKEKEY0001",
            "[fake-rendezvous]:2222 ssh-ed25519 AAAAFAKEKEY0002",
        ]
        cfg = ConnectConfig(
            rendezvous=self.cfg.rendezvous,
            identity=self.cfg.identity,
            host_keys=host_keys,
            ssh_path=str(self.ssh_ok),
        )
        with connect(cfg) as base_url:
            argv = self._argv_of(self.ssh_ok)
            config_path = self._config_path(argv)
            temp_dir = config_path.parent
            known_hosts = None
            for line in self._config_text(argv).splitlines():
                if line.strip().startswith("UserKnownHostsFile"):
                    known_hosts = Path(line.split('"')[1])
            self.assertIsNotNone(known_hosts)
            self.assertEqual(known_hosts.parent, temp_dir)  # 同一個暫存目錄
            self.assertTrue(known_hosts.exists())
            self.assertEqual(
                known_hosts.read_text(encoding="utf-8"),
                "\n".join(host_keys) + "\n",
            )
            self.assertEqual(stat.S_IMODE(known_hosts.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(temp_dir.stat().st_mode), 0o700)
            with urllib.request.urlopen(
                base_url + "/v1/subscriptions", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
        # 結束後檔案與整個目錄都刪掉
        self.assertFalse(known_hosts.exists())
        self.assertFalse(temp_dir.exists())
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
        bad = ConnectConfig(
            rendezvous="tester@fake-rendezvous:2222",
            identity='x"y',
            known_hosts=str(self.root / "known_hosts_fake"),
            ssh_path=str(self.ssh_ok),
        )
        with self.assertRaises(Unavailable) as ctx:
            with connect(bad, timeout=1):
                self.fail("connect 不該成功")
        self.assertEqual(ctx.exception.kind, "bad_config")  # 引號進不了 ssh_config

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


def _base(**overrides):
    config = {
        "schema": 1,
        "rendezvous": "sshproxy@fake-rendezvous:2222",
        "identity": "/tmp/fake-id",
        "known_hosts": "/tmp/fake-known-hosts",
    }
    config.update(overrides)
    return config


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "connect.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def _error(self, payload):
        self._write(payload)
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        return str(ctx.exception)

    def test_full_known_hosts(self):
        cfg = load_config(self._write(_base(
            jumps=["jump1@hop1:2200", "jump2@hop2:2200"],
            remote_api=5000,
        )))
        self.assertEqual(cfg.jumps, ["jump1@hop1:2200", "jump2@hop2:2200"])
        self.assertEqual(cfg.rendezvous, "sshproxy@fake-rendezvous:2222")
        self.assertEqual(cfg.identity, "/tmp/fake-id")
        self.assertEqual(cfg.known_hosts, "/tmp/fake-known-hosts")
        self.assertIsNone(cfg.host_keys)
        self.assertEqual(cfg.remote_api, 5000)
        self.assertEqual(cfg.ssh_path, "ssh")

    def test_minimal_host_keys_defaults(self):
        cfg = load_config(self._write({
            "schema": 1,
            "rendezvous": "sshproxy@fake-rendezvous:2222",
            "identity": "/tmp/fake-id",
            "host_keys": ["[fake-rendezvous]:2222 ssh-ed25519 AAAAFAKEKEY0001"],
        }))
        self.assertEqual(cfg.jumps, [])
        self.assertIsNone(cfg.known_hosts)
        self.assertEqual(
            cfg.host_keys,
            ["[fake-rendezvous]:2222 ssh-ed25519 AAAAFAKEKEY0001"],
        )
        self.assertEqual(cfg.remote_api, 4710)

    def test_from_dict_direct(self):
        cfg = ConnectConfig.from_dict(_base())
        self.assertEqual(cfg.rendezvous, "sshproxy@fake-rendezvous:2222")
        with self.assertRaises(ConfigError):
            ConnectConfig.from_dict(_base(remote_api="4710"))

    def test_rereads_file_every_call(self):
        path = self._write(_base(remote_api=5000))
        self.assertEqual(load_config(path).remote_api, 5000)
        self._write(_base(remote_api=5001))
        self.assertEqual(load_config(path).remote_api, 5001)

    def test_unknown_field_no_value(self):
        text = self._error(_base(frobnicate="canary-value-zz9"))
        self.assertIn("unknown field", text)
        self.assertIn("frobnicate", text)  # 欄位名可以寫
        self.assertNotIn("canary-value-zz9", text)  # 不印值

    def test_known_hosts_and_host_keys_conflict(self):
        text = self._error(_base(host_keys=["[fake-rendezvous]:2222 ssh-ed25519 A"]))
        self.assertIn("mutually exclusive", text)
        text = self._error({"schema": 1, "rendezvous": "sshproxy@fake-rendezvous:2222",
                            "identity": "/tmp/fake-id"})  # 都沒給
        self.assertIn("known_hosts", text)

    def test_schema_errors(self):
        text = self._error({k: v for k, v in _base().items() if k != "schema"})
        self.assertIn("schema", text)
        for bad in (2, "1", True, 1.0, None):
            with self.assertRaises(ConfigError) as ctx:
                ConnectConfig.from_dict(_base(schema=bad))
            self.assertIn("schema", str(ctx.exception))

    def test_missing_fields(self):
        for field in ("rendezvous", "identity"):
            payload = {k: v for k, v in _base().items() if k != field}
            with self.assertRaises(ConfigError) as ctx:
                ConnectConfig.from_dict(payload)
            self.assertIn("missing field", str(ctx.exception))
            self.assertIn(field, str(ctx.exception))

    def test_bad_types_no_values(self):
        cases = [
            (_base(rendezvous=42), "rendezvous", "42"),
            (_base(rendezvous="no-port-here"), "rendezvous", "no-port-here"),
            (_base(identity=17), "identity", "17"),
            (_base(identity='has"quote'), "identity", 'has"quote'),
            (_base(identity="line\nbreak"), "identity", "line\nbreak"),
            (_base(jumps="jump1@hop1:2200"), "jumps", "jump1@hop1:2200"),
            (_base(jumps=["jump1@hop1:2200", "no-port-here"]), "jumps[1]",
             "no-port-here"),
            (_base(known_hosts=""), "known_hosts", None),
            (_base(known_hosts=5), "known_hosts", "5"),
            (_base(known_hosts='kh"quote'), "known_hosts", 'kh"quote'),
            (_base(remote_api="4710"), "remote_api", "4710"),
            (_base(remote_api=0), "remote_api", "0"),
            (_base(remote_api=65536), "remote_api", "65536"),
            (_base(remote_api=True), "remote_api", "True"),
        ]
        for payload, field, value in cases:
            with self.assertRaises(ConfigError) as ctx:
                ConnectConfig.from_dict(payload)
            text = str(ctx.exception)
            self.assertIn(field, text)
            if value is not None:
                self.assertNotIn(value, text)

    def test_host_keys_errors(self):
        def host_only(host_keys):
            payload = _base()
            del payload["known_hosts"]
            payload["host_keys"] = host_keys
            return payload

        cases = [
            (host_only("[fake-rendezvous]:2222 ssh-ed25519 A"), "host_keys"),
            (host_only([]), "host_keys"),
            (host_only([5]), "host_keys[0]"),
            (host_only(["[fake-rendezvous]:2222 ssh-ed25519 A", ""]),
             "host_keys[1]"),
        ]
        for payload, field in cases:
            with self.assertRaises(ConfigError) as ctx:
                ConnectConfig.from_dict(payload)
            self.assertIn(field, str(ctx.exception))

    def test_not_json_or_not_object_no_content(self):
        text = self._error('{"rendezvous": "canary-value-zz9"')
        self.assertIn("JSON", text)
        self.assertNotIn("canary-value-zz9", text)
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write("[1, 2]"))
        self.assertIn("object", str(ctx.exception))


class CliTest(ConnectCase):
    def _config_env(self, rendezvous):
        return self._write_config(json.dumps({
            "schema": 1,
            "rendezvous": rendezvous,
            "jumps": [],
            "identity": str(self.root / "id_fake"),
            "known_hosts": str(self.root / "known_hosts_fake"),
            "remote_api": 4710,
        }))

    def _write_config(self, text):
        path = self.root / "connect.json"
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

    def test_cli_bad_config(self):
        config_path = self._write_config('{"schema": 1, "rendezvous": "canary-value-zz9"}')
        proc = self._run_cli(config_path, self.ssh_ok)
        rc = proc.wait(timeout=10)
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(rc, 2)
        self.assertNotIn("canary-value-zz9", err)  # 錯誤不印值


if __name__ == "__main__":
    unittest.main()
