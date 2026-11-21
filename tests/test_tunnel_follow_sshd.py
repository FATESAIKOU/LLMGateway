"""#226（#221 P2）(c)：真實 loopback sshd 的 C-5／C-6(④⑤)。

兩個真 sshd（G1：P1／K1，G2：P2／K2；非 root、ephemeral 埠、各自的主機金鑰）＋mock lgwd
（200）＋mock GitHub Variables API。沒有 /usr/sbin/sshd（或 /run/sshd）時 skipTest——CI 必須有。

「黑洞」用 TCP relay 模擬（見 tests/tunnel_fakes.py::Relay 的說明）：兩個 sshd 共用 loopback，
被 SIGSTOP 的舊 sshd 仍占著 -R 的遠端埠，新會合點會因 ExitOnForwardFailure 永遠建不起來；
relay 的黑洞把往 sshd 的那側關掉（釋放遠端埠），對 ssh 那側不回應也不關閉，效果相同。
"""
import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.tunnel_fakes import (
    Harness, MockGitHub, MockLgwd, REPO_ROOT, Relay, Sshd, TUNNEL, free_port, have_real_ssh, keygen,
    port_open, wait_until,
)
from tests.test_tunnel_follow import TOKEN, tun


def child_cmdlines(pid):
    """直接子行程的 argv（從 /proc 讀）。"""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry) as f:
                stat = f.read()
            if int(stat[stat.rfind(")") + 2:].split()[1]) != pid:
                continue
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                found.append((int(entry), f.read().decode().split("\0")[:-1]))
        except (OSError, ValueError):
            continue
    return found


def http_get_via(port):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(b"GET /v1/subscriptions HTTP/1.0\r\n\r\n")
        return s.recv(100).split(b"\r\n", 1)[0]


@unittest.skipUnless(have_real_ssh(), "需要真實 /usr/sbin/sshd、ssh、ssh-keygen 與 /run/sshd")
class RealSshdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.user = getpass.getuser()
        self.client_key = os.path.join(root, "client_key")
        keygen(self.client_key)
        self.sshd1 = Sshd(root, "g1", self.client_key + ".pub").start()
        self.addCleanup(self.sshd1.stop)
        self.sshd2 = Sshd(root, "g2", self.client_key + ".pub").start()
        self.addCleanup(self.sshd2.stop)
        self.relay1 = Relay(self.sshd1.port)
        self.addCleanup(self.relay1.stop)
        self.lgwd = MockLgwd().start()
        self.addCleanup(self.lgwd.stop)
        self.github = MockGitHub().start()
        self.addCleanup(self.github.stop)
        self.remote_api, self.remote_ssh = free_port(), free_port()
        self.h = Harness(root)
        self.addCleanup(self.h.stop)
        self.token_file = os.path.join(root, "token")
        with open(self.token_file, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)
        self.write_env()
        self.api_env = {"LGW_GATEWAY_API_BASE": self.github.base_url,
                        "LGW_ALLOW_INSECURE_API_BASE": "1"}
        self.seed_g1()

    # G1 對外的埠是 relay 的埠（可黑洞）；金鑰是 sshd1 的
    @property
    def p1(self):
        return self.relay1.port

    def gateway(self, port, key, generation, ip="127.0.0.1"):
        return {"ip": ip, "port": port, "tunnel_user": self.user, "host_key": key,
                "generation": generation, "published_at": "2026-11-21T00:00:00Z"}

    def seed_g1(self):
        record = {"ip": "127.0.0.1", "ssh_port": self.p1, "tunnel_user": self.user,
                  "host_key": self.sshd1.host_key, "generation": 1, "published_at": "2026-11-21T00:00:00Z"}
        self.h.write_state("gateway.json", record)

    def write_env(self, **extra):
        values = {
            "IDENTITY": self.client_key, "TUNNEL_USER": self.user,
            "GATEWAY_SOURCE_REPO": "FATESAIKOU/MyLinuxPool",
            "GATEWAY_SOURCE_PATH": "actions/variables/NODE_GATEWAY",
            "GATEWAY_GITHUB_KEY_FILE": self.token_file,
            "REMOTE_API": str(self.remote_api), "REMOTE_SSH": str(self.remote_ssh),
            "LOCAL_API": str(self.lgwd.port), "PROBE_INTERVAL": "1", "PROBE_FAILS": "3",
            "SSH_CONNECT_TIMEOUT": "3", "SERVER_ALIVE_INTERVAL": "1", "SERVER_ALIVE_COUNT_MAX": "2",
            "FETCH_TIMEOUT": "3", "FETCH_MIN_INTERVAL": "1", "FETCH_BACKOFF_MAX": "4",
        }
        values.update(extra)
        self.h.write_env(values)

    def start(self):
        return self.h.start(self.api_env)

    def tunnel_child(self):
        for pid, argv in child_cmdlines(self.h.proc.pid):
            if "-N" in argv:
                return pid, argv
        return None

    def verified(self):
        try:
            return self.h.read_json(".tunnel_generation")
        except (OSError, ValueError):
            return None

    def wait_verified(self, generation, timeout=30):
        return wait_until(lambda: (self.verified() or {}).get("generation") == generation, timeout)

    def known_hosts_text(self):
        return self.h.read_text(".tunnel_known_hosts")

    # ------------------------------------------------------------------
    def test_C5_rotation_between_two_real_sshds(self):
        self.github.set_gateway(**self.gateway(self.sshd2.port, self.sshd2.host_key, 2))
        self.start()
        # ② 健康：先驗通 G1（首次探針 200 才寫 verified），之後多個探針週期都不碰 GitHub
        self.assertTrue(self.wait_verified(1), self.h.stderr())
        pid1, argv1 = self.tunnel_child()
        # ① argv：ConnectTimeout、-l <user> -- <normalized host>
        self.assertIn("ConnectTimeout=3", argv1)
        self.assertEqual(argv1[-4:], ["-l", self.user, "--", "127.0.0.1"])
        self.assertIn("StrictHostKeyChecking=yes", argv1)
        self.assertIn("ExitOnForwardFailure=yes", argv1)
        self.assertNotIn("accept-new", " ".join(argv1))
        self.assertEqual(http_get_via(self.remote_api), b"HTTP/1.0 200 OK")      # 真的經 G1 轉到 mock lgwd
        self.assertTrue(port_open(self.remote_ssh))                                # 4711 同時轉發
        time.sleep(4)
        self.assertEqual(self.github.request_count, 0)
        self.assertEqual(self.tunnel_child()[0], pid1)
        # ④ --probe-once：只讀、回 0、不打 GitHub
        before = self.h.listing()
        probe = subprocess.run([sys.executable, TUNNEL, "--probe-once"], capture_output=True, text=True,
                               env=self.h.env(self.api_env), cwd=REPO_ROOT, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stderr, "")
        self.assertEqual(self.h.listing(), before)
        self.assertEqual(self.github.request_count, 0)
        # ③ G1 變黑洞、GitHub 已是 G2：同一行程內抓一次、重連 G2
        daemon_pid = self.h.proc.pid
        started = time.monotonic()
        self.relay1.blackhole()
        self.assertTrue(wait_until(lambda: "gateway_updated" in self.h.stderr(), 30), self.h.stderr())
        self.assertTrue(self.wait_verified(2, 30), self.h.stderr())
        rto = time.monotonic() - started
        self.assertLess(rto, 30)
        self.assertEqual(self.h.proc.pid, daemon_pid)
        self.assertIsNone(self.h.proc.poll())
        self.assertEqual(self.github.request_count, 1)
        fields = tun.validate_follow(
            {"generation": 2, "ip": "127.0.0.1", "ssh_port": self.sshd2.port, "tunnel_user": self.user,
             "host_key": self.sshd2.host_key}, self.user)
        self.assertEqual(self.verified(), {"generation": 2, "fingerprint": tun.fingerprint(fields)})
        self.assertEqual(self.known_hosts_text(), "[127.0.0.1]:%d %s\n" % (self.sshd2.port, self.sshd2.host_key))
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)
        pid2, argv2 = self.tunnel_child()
        self.assertNotEqual(pid2, pid1)
        self.assertEqual(argv2[-4:], ["-l", self.user, "--", "127.0.0.1"])
        self.assertIn(str(self.sshd2.port), argv2)
        self.assertEqual(http_get_via(self.remote_api), b"HTTP/1.0 200 OK")      # 現在是經 G2
        failures = self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"] \
            if self.h.exists(".tunnel_fetch_backoff") else 0
        self.assertEqual(failures, 0)
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "ok")
        self.assertEqual(self.h.stderr().splitlines().count("tunnel_up"), 2)
        self.assertNotIn(TOKEN, self.h.stderr())
        self.assertEqual(self.h.stop(), 0)

    def test_C6_wrong_host_key_fails_closed_and_keeps_last_known(self):
        # GitHub 宣稱 G2 用 K1（錯金鑰）：StrictHostKeyChecking=yes 拒連，不退回 accept-new
        self.github.set_gateway(**self.gateway(self.sshd2.port, self.sshd1.host_key, 2))
        self.start()
        self.assertTrue(self.wait_verified(1), self.h.stderr())
        self.relay1.blackhole()
        self.assertEqual(self.h.wait(60), 1)
        self.assertIn("gateway_updated", self.h.stderr())
        self.assertEqual(self.verified()["generation"], 1)                  # 沒驗通就不升水位
        self.assertFalse(port_open(self.remote_api), "不該有任何隧道建立在 G2 上")
        self.assertFalse(os.path.exists(os.path.join(self.h.home, ".ssh", "known_hosts")))
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)   # applied 已前進、verified 沒有

    def test_C6_follow_file_broken_while_healthy_then_recovers_with_new_generation(self):
        self.github.set_gateway(**self.gateway(self.sshd2.port, self.sshd2.host_key, 3))
        self.start()
        self.assertTrue(self.wait_verified(1), self.h.stderr())
        pid1, _ = self.tunnel_child()
        # ④ 運行中把 FOLLOW_FILE 寫壞，再寫成回退的世代：不打斷現有 ssh
        for broken in ("{not json", json.dumps(dict(self.h.read_json("gateway.json"), generation=0))):
            self.h.write_state("gateway.json", broken)
            time.sleep(3)
            self.assertIsNone(self.h.proc.poll())
            self.assertEqual(self.tunnel_child()[0], pid1)
            self.assertEqual(http_get_via(self.remote_api), b"HTTP/1.0 200 OK")
        self.assertEqual(self.github.request_count, 0)
        events = self.h.stderr().splitlines()
        self.assertEqual(sum(e.startswith("follow_") for e in events), 1)
        # ⑤ 連線真的斷了；GitHub 上已是有效新代 gen=3 → 自動恢復、通 G2、歸零
        self.h.write_state(".tunnel_fetch_backoff", {"next_allowed_epoch": 0, "consecutive_failures": 2})
        self.relay1.blackhole()
        self.assertTrue(self.wait_verified(3, 40), self.h.stderr())
        self.assertEqual(http_get_via(self.remote_api), b"HTTP/1.0 200 OK")
        self.assertTrue(wait_until(
            lambda: self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"] == 0, 5))
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 3)
        self.assertEqual(self.h.stop(), 0)

    def test_C5_only_static_rendezvous_start_then_rotate(self):
        # 靜態三鍵起步（沒有 FOLLOW_FILE）：第一次輪換才由 GitHub 接手
        os.unlink(self.h.path("gateway.json"))
        kh = os.path.join(self.tmp.name, "static_kh")
        with open(kh, "w") as f:
            f.write("[127.0.0.1]:%d %s\n" % (self.p1, self.sshd1.host_key))
        self.write_env(RENDEZVOUS="%s@127.0.0.1:%d" % (self.user, self.p1), KNOWN_HOSTS=kh)
        self.github.set_gateway(**self.gateway(self.sshd2.port, self.sshd2.host_key, 7))
        self.start()
        self.assertTrue(wait_until(lambda: self.tunnel_child() is not None, 10))
        time.sleep(3)
        self.assertEqual(http_get_via(self.remote_api), b"HTTP/1.0 200 OK")
        self.assertFalse(self.h.exists(".tunnel_generation"))
        self.assertEqual(self.github.request_count, 0)
        self.relay1.blackhole()
        self.assertTrue(self.wait_verified(7, 40), self.h.stderr())
        self.assertEqual(open(kh).read().count("\n"), 1)                      # 原靜態檔不動
        self.assertEqual(self.h.stop(), 0)


if __name__ == "__main__":
    unittest.main()
