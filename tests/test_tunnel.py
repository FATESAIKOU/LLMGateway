import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.proc_util import alive as is_pid_alive   # 殭屍算死（#136）

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TUNNEL = os.path.join(REPO_ROOT, "ops-scripts", "llmgateway-tunnel")
TUNNEL_UNIT = os.path.join(REPO_ROOT, "deploy", "units", "llmgateway-tunnel.service")

FAKE_USER = "fakeuser"
FAKE_HOST = "rendezvous.invalid"
FAKE_PORT = "2200"
FAKE_IDENTITY = "/tmp/does-not-exist/id_fake_tunnel"
FAKE_KNOWN_HOSTS = "/tmp/does-not-exist/known_hosts_fake"


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TunnelDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.sandbox = self.tmpdir.name
        self.home = os.path.join(self.sandbox, "home")
        self.config = os.path.join(self.home, ".config", "llmgateway", "tunnel.env")
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        self.ssh_log = os.path.join(self.sandbox, "ssh-argv.log")
        self.stderr_path = os.path.join(self.sandbox, "tunnel.stderr")
        self.fake_ssh = os.path.join(self.sandbox, "fake-ssh")
        self._write_fake_ssh()
        self.proc = None
        self.stderr_fh = None
        self.addCleanup(self._cleanup)

    def _write_fake_ssh(self):
        script = """#!/usr/bin/env bash
printf 'argv: %s\\n' "$*" >> "${FAKE_SSH_LOG:?}"
mode=""
for arg in "$@"; do
    case "$arg" in
        -N) mode=tunnel ;;
        -W) mode=probe ;;
    esac
done
if [[ "$mode" == "tunnel" ]]; then
    printf 'tunnel_pid: %s\\n' "$$" >> "${FAKE_SSH_LOG}"
fi
if [[ "$mode" == "probe" ]]; then
    request="$(cat)"
    printf 'request: %s\\n' "$request" >> "${FAKE_SSH_LOG}"
    case "${FAKE_PROBE_CODE:-200}" in
        200) printf 'HTTP/1.0 200 OK\\r\\n\\r\\n' ;;
        503) printf 'HTTP/1.0 503 Service Unavailable\\r\\n\\r\\n' ;;
        none) ;;
        hang) sleep 30 ;;
    esac
    exit 0
fi
if [[ "${FAKE_TUNNEL_EXIT:-run}" == "exit" ]]; then
    sleep 0.05
    exit 0
fi
trap 'echo tunnel_terminated >> "${FAKE_SSH_LOG}"; exit 143' TERM
while :; do sleep 0.05; done
"""
        with open(self.fake_ssh, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(self.fake_ssh, 0o755)

    def _cleanup(self):
        self.stop_daemon()
        if self.stderr_fh is not None:
            self.stderr_fh.close()
            self.stderr_fh = None

    def write_config(self, path=None, **overrides):
        path = self.config if path is None else path
        rendezvous = overrides.pop(
            "RENDEZVOUS", "%s@%s:%s" % (FAKE_USER, FAKE_HOST, FAKE_PORT)
        )
        lines = [
            "RENDEZVOUS=%s" % rendezvous,
            "IDENTITY=%s" % overrides.pop("IDENTITY", FAKE_IDENTITY),
            "KNOWN_HOSTS=%s" % overrides.pop("KNOWN_HOSTS", FAKE_KNOWN_HOSTS),
            "SSH_PATH=%s" % overrides.pop("SSH_PATH", self.fake_ssh),
            "PROBE_INTERVAL=%s" % overrides.pop("PROBE_INTERVAL", "0.2"),
            "PROBE_FAILS=%s" % overrides.pop("PROBE_FAILS", "3"),
        ]
        for key, value in overrides.items():
            lines.append("%s=%s" % (key, value))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def start_daemon(self, env=None):
        full_env = {
            "HOME": self.home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FAKE_SSH_LOG": self.ssh_log,
            "FAKE_PROBE_CODE": "200",
            "FAKE_TUNNEL_EXIT": "run",
        }
        if env:
            full_env.update(env)
        if self.stderr_fh is not None:
            self.stderr_fh.close()
        self.stderr_fh = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, TUNNEL],
            stdout=subprocess.DEVNULL,
            stderr=self.stderr_fh,
            env=full_env,
            cwd=REPO_ROOT,
        )
        return self.proc

    def stop_daemon(self):
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        return self.proc.returncode

    def run_probe_once(self, env=None, base=None):
        full_env = {
            "HOME": self.home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FAKE_SSH_LOG": self.ssh_log,
            "FAKE_PROBE_CODE": "200",
        }
        if base is not None:
            full_env = dict(base)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, TUNNEL, "--probe-once"],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=REPO_ROOT,
        )

    def wait_exit(self, timeout=20):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail("daemon did not exit in time")
        return self.proc.returncode

    def read_stderr(self):
        try:
            with open(self.stderr_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def read_ssh_log(self):
        try:
            with open(self.ssh_log, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_daemon_file_is_executable_python(self):
        self.assertTrue(os.path.isfile(TUNNEL))
        self.assertTrue(os.access(TUNNEL, os.X_OK))
        with open(TUNNEL, "r", encoding="utf-8") as f:
            shebang = f.readline().strip()
        self.assertEqual(shebang, "#!/usr/bin/env python3")
        res = subprocess.run(
            [sys.executable, "-m", "py_compile", TUNNEL],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_missing_config_exits_78_and_logs_only_config_missing(self):
        scenarios = [
            ("no file", None),
            ("empty rendezvous", {"RENDEZVOUS": ""}),
            ("empty identity", {"IDENTITY": ""}),
            ("empty known_hosts", {"KNOWN_HOSTS": ""}),
        ]
        for name, overrides in scenarios:
            with self.subTest(name=name):
                self.stop_daemon()
                if os.path.exists(self.config):
                    os.remove(self.config)
                if overrides is not None:
                    self.write_config(**overrides)
                self.start_daemon()
                self.assertEqual(self.wait_exit(10), 78)
                self.assertEqual(self.read_stderr(), "config_missing\n")
                self.assertEqual(self.read_ssh_log(), "")

    def test_tunnel_argv_and_log_has_no_hostname(self):
        self.write_config()
        self.start_daemon()
        self.assertTrue(wait_until(lambda: "tunnel_up" in self.read_stderr()))
        self.assertTrue(wait_until(lambda: "argv:" in self.read_ssh_log()))
        ssh_log = self.read_ssh_log()
        self.assertIn("-N", ssh_log)
        self.assertIn("127.0.0.1:4710:127.0.0.1:8480", ssh_log)
        self.assertIn("127.0.0.1:4711:127.0.0.1:22", ssh_log)
        self.assertIn("-o ExitOnForwardFailure=yes", ssh_log)
        self.assertIn("-o StrictHostKeyChecking=yes", ssh_log)
        self.assertIn("-o BatchMode=yes", ssh_log)
        self.assertIn("-o GlobalKnownHostsFile=/dev/null", ssh_log)
        self.assertIn("-o IdentitiesOnly=yes", ssh_log)
        self.assertIn("-o ServerAliveInterval=30", ssh_log)
        self.assertIn("-o ServerAliveCountMax=3", ssh_log)
        self.assertIn("UserKnownHostsFile=" + FAKE_KNOWN_HOSTS, ssh_log)
        self.assertIn("-i " + FAKE_IDENTITY, ssh_log)
        self.assertIn("-p " + FAKE_PORT, ssh_log)
        self.assertIn(FAKE_USER + "@" + FAKE_HOST, ssh_log)
        self.assertNotIn("accept-new", ssh_log)

        stderr = self.read_stderr()
        self.assertNotIn(FAKE_HOST, stderr)
        self.assertNotIn(FAKE_USER, stderr)
        self.assertNotIn(FAKE_IDENTITY, stderr)
        self.assertEqual(self.stop_daemon(), 0)

    def test_probe_200_keeps_running(self):
        self.write_config()
        self.start_daemon(env={"FAKE_PROBE_CODE": "200"})
        self.assertTrue(
            wait_until(lambda: self.read_ssh_log().count("argv:") >= 3, timeout=15)
        )
        time.sleep(0.3)
        self.assertIsNone(self.proc.poll(), "daemon should keep running")
        stderr = self.read_stderr()
        self.assertNotIn("probe_fail", stderr)
        self.assertNotIn("restart", stderr)
        ssh_log = self.read_ssh_log()
        self.assertIn("-W 127.0.0.1:4710", ssh_log)
        self.assertIn("GET /v1/subscriptions HTTP/1.0", ssh_log)
        self.assertEqual(self.stop_daemon(), 0)

    def test_three_probe_failures_restart_tunnel(self):
        self.write_config(PROBE_INTERVAL="0.2", PROBE_FAILS="3")
        self.start_daemon(env={"FAKE_PROBE_CODE": "503"})
        rc = self.wait_exit(20)
        self.assertEqual(rc, 1)
        stderr = self.read_stderr()
        self.assertIn("tunnel_up", stderr)
        self.assertEqual(stderr.count("probe_fail"), 3)
        self.assertIn("restart", stderr)
        self.assertNotIn(FAKE_HOST, stderr)
        self.assertTrue(
            wait_until(lambda: "tunnel_terminated" in self.read_ssh_log(), timeout=5)
        )

    def test_probe_without_response_is_failure(self):
        self.write_config(PROBE_INTERVAL="0.2", PROBE_FAILS="1")
        self.start_daemon(env={"FAKE_PROBE_CODE": "none"})
        self.assertEqual(self.wait_exit(20), 1)
        stderr = self.read_stderr()
        self.assertIn("probe_fail", stderr)
        self.assertIn("restart", stderr)

    def test_probe_once_returns_0_and_uses_daemon_ssh_options(self):
        self.write_config()
        res = self.run_probe_once()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        ssh_log = self.read_ssh_log()
        self.assertIn("-W 127.0.0.1:4710", ssh_log)
        self.assertIn("GET /v1/subscriptions HTTP/1.0", ssh_log)
        self.assertIn("-i " + FAKE_IDENTITY, ssh_log)
        self.assertIn("-o UserKnownHostsFile=" + FAKE_KNOWN_HOSTS, ssh_log)
        self.assertIn("-o GlobalKnownHostsFile=/dev/null", ssh_log)
        self.assertIn("-o IdentitiesOnly=yes", ssh_log)
        self.assertIn("-o StrictHostKeyChecking=yes", ssh_log)
        self.assertIn("-o BatchMode=yes", ssh_log)
        self.assertIn("-p " + FAKE_PORT, ssh_log)
        self.assertIn(FAKE_USER + "@" + FAKE_HOST, ssh_log)
        self.assertNotIn("-N", ssh_log)

    def test_probe_once_returns_1_on_non_200_or_no_response(self):
        for code in ("503", "none"):
            with self.subTest(code=code):
                self.write_config()
                res = self.run_probe_once(env={"FAKE_PROBE_CODE": code})
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)

    def test_probe_once_returns_1_on_timeout(self):
        self.write_config(PROBE_INTERVAL="0.1")
        res = self.run_probe_once(env={"FAKE_PROBE_CODE": "hang"})
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)

    def test_probe_once_returns_2_when_cannot_execute(self):
        # 設定檔不存在
        res = self.run_probe_once()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(self.read_ssh_log(), "")

        # 必填缺一（RENDEZVOUS 空）
        self.write_config(RENDEZVOUS="")
        res = self.run_probe_once()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(self.read_ssh_log(), "")

        # 找不到 ssh
        self.write_config(SSH_PATH="/nonexistent/no-such-ssh")
        res = self.run_probe_once()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(self.read_ssh_log(), "")

    def test_tunnel_child_exit_ends_daemon(self):
        self.write_config()
        self.start_daemon(env={"FAKE_TUNNEL_EXIT": "exit"})
        self.assertEqual(self.wait_exit(10), 1)
        stderr = self.read_stderr()
        self.assertIn("tunnel_up", stderr)
        self.assertNotIn("restart", stderr)

    def test_sigterm_stops_daemon_and_ssh_child(self):
        self.write_config()
        self.start_daemon()
        self.assertTrue(wait_until(lambda: "tunnel_up" in self.read_stderr()))
        self.assertTrue(wait_until(lambda: "tunnel_pid:" in self.read_ssh_log()))
        m = re.search(r"tunnel_pid: (\d+)", self.read_ssh_log())
        self.assertIsNotNone(m)
        child_pid = int(m.group(1))
        self.assertTrue(is_pid_alive(child_pid))

        self.proc.send_signal(signal.SIGTERM)
        rc = self.wait_exit(timeout=5)
        self.assertEqual(rc, 0)

        self.assertTrue(
            wait_until(lambda: "tunnel_terminated" in self.read_ssh_log(), timeout=5)
        )
        self.assertTrue(wait_until(lambda: not is_pid_alive(child_pid), timeout=5))

    def test_switch_rendezvous_target_updates_argv(self):
        other_user = "otheruser"
        other_host = "other-target.invalid"
        other_port = "2222"
        self.write_config(
            RENDEZVOUS="%s@%s:%s" % (other_user, other_host, other_port)
        )
        self.start_daemon()
        self.assertTrue(wait_until(lambda: "tunnel_up" in self.read_stderr()))
        self.assertTrue(wait_until(lambda: "argv:" in self.read_ssh_log()))
        ssh_log = self.read_ssh_log()
        self.assertIn("-p " + other_port, ssh_log)
        self.assertIn(other_user + "@" + other_host, ssh_log)
        self.assertNotIn(FAKE_HOST, ssh_log)
        self.assertEqual(self.stop_daemon(), 0)

    # --- #35 T3：LGW_TUNNEL_ENV 換設定檔路徑（system unit 用） -----------------
    def _write_override_config(self, host="other-target.invalid", port="2999"):
        path = os.path.join(self.sandbox, "other-tunnel.env")
        self.write_config(path, RENDEZVOUS="otheruser@%s:%s" % (host, port))
        return path

    def test_LGW_TUNNEL_ENV覆寫設定檔路徑_探針(self):
        other = self._write_override_config()
        self.write_config()  # 預設路徑也有一份合法的，確保真的用了覆寫那份
        res = self.run_probe_once(env={"LGW_TUNNEL_ENV": other})
        self.assertEqual(res.returncode, 0, res.stderr)
        ssh_log = self.read_ssh_log()
        self.assertIn("other-target.invalid", ssh_log)
        self.assertIn("-p 2999", ssh_log)
        self.assertNotIn(FAKE_HOST, ssh_log)

    def test_LGW_TUNNEL_ENV覆寫設定檔路徑_守護程式(self):
        other = self._write_override_config()
        self.start_daemon(env={"LGW_TUNNEL_ENV": other})
        self.assertTrue(wait_until(lambda: "tunnel_up" in self.read_stderr()))
        self.assertTrue(wait_until(lambda: "argv:" in self.read_ssh_log()))
        ssh_log = self.read_ssh_log()
        self.assertIn("other-target.invalid", ssh_log)
        self.assertNotIn(FAKE_HOST, ssh_log)
        self.assertEqual(self.stop_daemon(), 0)

    def test_LGW_TUNNEL_ENV空字串時用預設路徑(self):
        self.write_config()
        res = self.run_probe_once(env={"LGW_TUNNEL_ENV": ""})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(FAKE_HOST, self.read_ssh_log())

    def test_LGW_TUNNEL_ENV指到不存在的檔不回頭用預設(self):
        self.write_config()  # 預設路徑合法；覆寫指到不存在的檔時不可以拿它來用
        res = self.run_probe_once(
            env={"LGW_TUNNEL_ENV": os.path.join(self.sandbox, "no-such-tunnel.env")})
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(self.read_ssh_log(), "")

    def test_LGW_TUNNEL_ENV覆寫時不需要HOME(self):
        other = self._write_override_config()
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FAKE_SSH_LOG": self.ssh_log,
            "FAKE_PROBE_CODE": "200",
        }
        res = self.run_probe_once(base=base, env={"LGW_TUNNEL_ENV": other})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("other-target.invalid", self.read_ssh_log())


class TunnelUnitFileTests(unittest.TestCase):
    """#35 T3：system 層的隧道 unit 檔（新版面 /etc/llmgateway/tunnel.env）。"""

    def read_unit(self):
        with open(TUNNEL_UNIT, "r", encoding="utf-8") as f:
            return f.read()

    def test_system層unit內容(self):
        unit = self.read_unit()
        self.assertIn("User=llmgw", unit)
        self.assertIn("Environment=LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env", unit)
        self.assertIn("ExecStart=/opt/llmgateway/current/ops-scripts/llmgateway-tunnel",
                      unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("RestartSec=10", unit)
        self.assertIn("RestartPreventExitStatus=78", unit)
        self.assertIn("After=llmgateway.service", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("WantedBy=default.target", unit)  # 這是 system 層，不是 user unit
        self.assertNotIn("%h/", unit)  # 不用 user 層的 %h 路徑


if __name__ == "__main__":
    unittest.main()
