"""#226（#221 P2）(c)：llmgateway-tunnel 狀態機——以假 ssh 驗 C-6／C-7／C-8／C-9。

假 ssh 把每個 argv 記下，探針回應與隧道存亡由控制檔決定；GitHub 是 loopback mock。
真實 sshd 的 C-5／C-6(④⑤) 在 tests/test_tunnel_follow_sshd.py。
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_tunnel_follow import TOKEN, ed25519_key, tun
from tests.tunnel_fakes import Harness, MockGitHub, REPO_ROOT, TUNNEL, wait_until

SVC = "svc"
OLD_IP, NEW_IP = "203.0.113.10", "203.0.113.20"
K_OLD, K_NEW = ed25519_key(b"1"), ed25519_key(b"2")
ALLOWED_EVENTS = {
    "tunnel_up", "gateway_updated", "gateway_reverted_to_verified", "unverified_retry", "fetch_ok",
    "fetch_failed", "fetch_auth_failed", "fetch_rate_limited", "fetch_invalid", "fetch_skipped",
    "follow_missing", "follow_invalid", "follow_regressed", "hostkey_missing", "probe_fail",
    "restart", "config_missing", "config_partial", "spawn_fail",
    "verified_invalid", "verified_unreadable", "verified_write_failed",
}


def gw(ip=NEW_IP, generation=2, key=K_NEW, port=22, **extra):
    data = {"ip": ip, "port": port, "tunnel_user": SVC, "host_key": key, "generation": generation,
            "user": "ignored", "key_secret": "ignored", "ports": {"llmgateway": [4710, 4711]}}
    data.update(extra)
    return data


def follow_record(ip, generation, key, port=22):
    return {"ip": ip, "ssh_port": port, "tunnel_user": SVC, "host_key": key,
            "generation": generation, "published_at": "2026-11-21T00:00:00Z"}


class StateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Harness(self.tmp.name, fake_ssh=True)
        self.addCleanup(self.h.stop)
        self.github = MockGitHub().start()
        self.addCleanup(self.github.stop)
        self.token_file = os.path.join(self.tmp.name, "token")
        with open(self.token_file, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)
        self.api_env = {"LGW_GATEWAY_API_BASE": self.github.base_url,
                        "LGW_ALLOW_INSECURE_API_BASE": "1"}
        self.write_env()
        self.github.set_gateway(**gw())

    def env_values(self, **extra):
        values = {
            "IDENTITY": "/nonexistent/id", "RENDEZVOUS": "%s@static.example:22" % SVC,
            "KNOWN_HOSTS": os.path.join(self.tmp.name, "static_known_hosts"),
            "SSH_PATH": self.h.fake_ssh, "PROBE_INTERVAL": "0.2", "PROBE_FAILS": "100",
            "GATEWAY_SOURCE_REPO": "FATESAIKOU/MyLinuxPool",
            "GATEWAY_SOURCE_PATH": "actions/variables/NODE_GATEWAY",
            "GATEWAY_GITHUB_KEY_FILE": self.token_file, "FETCH_TIMEOUT": "2",
            "FETCH_MIN_INTERVAL": "1", "FETCH_BACKOFF_MAX": "4",
        }
        values.update(extra)
        return values

    def write_env(self, **extra):
        self.h.write_env(self.env_values(**extra))

    def start(self, extra_env=None, args=()):
        self.run_started = time.time()
        env = dict(self.api_env)
        env.update(extra_env or {})
        return self.h.start(env, args)

    def run_to_exit(self, extra_env=None, timeout=30):
        self.start(extra_env)
        return self.h.wait(timeout)

    def reset_state(self):
        for name in os.listdir(self.h.state):
            path = os.path.join(self.h.state, name)
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.unlink(path)
        if os.path.exists(os.path.join(self.h.fake_dir, "ssh.log")):
            os.unlink(os.path.join(self.h.fake_dir, "ssh.log"))
        self.github.requests.clear()

    # 種子狀態（等同守護程式先前寫下的）
    def seed_follow(self, ip=OLD_IP, generation=1, key=K_OLD, port=22):
        record = follow_record(ip, generation, key, port)
        self.h.write_state("gateway.json", record)
        fields = tun.validate_follow(record, SVC)
        self.h.write_state(".tunnel_known_hosts", tun.known_hosts_line(fields))
        return fields

    def seed_verified(self, fields):
        self.h.write_state(".tunnel_generation", {"generation": fields["generation"],
                                                  "fingerprint": tun.fingerprint(fields)})

    def seed_backoff(self, failures, next_allowed=0):
        self.h.write_state(".tunnel_fetch_backoff", {"next_allowed_epoch": next_allowed,
                                                     "consecutive_failures": failures})

    def ssh_lines(self):
        # 假 ssh 是一段一段寫一行（argv:、逐個 [..]、最後才換行）；只取已寫完的行，免得讀到半行
        return self.h.ssh_log().split("\n")[:-1]

    def tunnel_argvs(self):
        return [line for line in self.ssh_lines() if "[-N]" in line]

    def probe_count(self):
        return sum("[-W]" in line for line in self.ssh_lines())

    def stop_daemon(self, grace=20):
        # 先送 SIGTERM，等它自己結束（最多 grace 秒）；rc 照舊要是 0
        proc = self.h.proc
        if proc.poll() is None:
            proc.terminate()
        self.assertTrue(wait_until(lambda: proc.poll() is not None, grace),
                        "SIGTERM 後 %s 秒仍未結束" % grace)
        return self.h.stop()

    def assert_only_fixed_events(self):
        lines = [l for l in self.h.stderr().splitlines() if l]
        self.assertTrue(set(lines) <= ALLOWED_EVENTS, set(lines) - ALLOWED_EVENTS)


class ZeroPollingAndRotationTests(StateBase):
    def test_healthy_connection_never_touches_github(self):
        self.seed_follow()
        self.start()
        self.assertTrue(wait_until(lambda: self.probe_count() >= 5, 15))   # 等到 ≥5 個探針週期，不靠固定 sleep
        self.assertIsNone(self.h.proc.poll())
        self.assertGreaterEqual(self.probe_count(), 5)
        self.assertEqual(self.github.request_count, 0)
        self.assertEqual(self.stop_daemon(), 0)

    def test_static_baseline_healthy_also_never_touches_github(self):
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1 and self.probe_count() >= 2, 15))
        self.assertEqual(self.github.request_count, 0)
        self.assertIn("[-l] [svc] [--] [static.example]", self.tunnel_argvs()[0])
        self.assertFalse(self.h.exists(".tunnel_generation"))     # 靜態基準沒有世代可記
        self.assertEqual(self.stop_daemon(), 0)

    def test_disconnect_fetch_reconnect_same_process_then_verify_after_probe(self):
        old = self.seed_follow()
        self.seed_verified(old)
        self.seed_backoff(failures=2)
        self.h.set_fake("probe", "503")                      # 新代未驗通前探針一律失敗
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 10))
        self.assertEqual(self.github.request_count, 0)
        self.h.set_fake("tunnel_die", "1")                  # 舊連線斷掉
        self.assertTrue(wait_until(lambda: "gateway_updated" in self.h.stderr(), 10))
        # gateway_updated 先記錄、之後才啟動第二次 ssh：高負載下第二個 argv 可能晚於事件出現
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 2, 10))
        probes_at_switch = self.probe_count()
        pid = self.h.proc.pid
        self.assertIsNone(self.h.proc.poll(), "同一個行程內重連，不是結束後讓 systemd 重開")
        self.assertEqual(self.github.request_count, 1)
        argvs = self.tunnel_argvs()
        self.assertEqual(len(argvs), 2)
        self.assertTrue(argvs[1].endswith("[-l] [svc] [--] [%s]" % NEW_IP), argvs[1])
        self.assertIn("[-o] [ConnectTimeout=10]", argvs[1])
        self.assertIn("[-o] [StrictHostKeyChecking=yes]", argvs[1])
        self.assertNotIn("accept-new", self.h.ssh_log())
        # 原子更新的狀態檔
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)
        self.assertEqual(self.h.read_text(".tunnel_known_hosts"), "%s %s\n" % (NEW_IP, K_NEW))
        self.assertEqual(stat.S_IMODE(os.stat(self.h.path("gateway.json")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.h.path(".tunnel_known_hosts")).st_mode), 0o600)
        # 首次探針 200 之前不預寫新世代、也不歸零連敗：等新連線實際探了 3 次（都是 503）再看
        self.assertTrue(wait_until(lambda: self.probe_count() >= probes_at_switch + 3, 10))
        self.assertEqual(self.h.read_json(".tunnel_generation")["generation"], 1)
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 2)
        self.h.set_fake("probe", "200")
        new = tun.validate_follow(follow_record(NEW_IP, 2, K_NEW), SVC)
        self.assertTrue(wait_until(lambda: self.h.read_json(".tunnel_generation")["generation"] == 2, 10))
        self.assertEqual(self.h.read_json(".tunnel_generation"),
                         {"generation": 2, "fingerprint": tun.fingerprint(new)})
        self.assertTrue(wait_until(
            lambda: self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"] == 0, 5))
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "ok")
        self.assertEqual(self.github.request_count, 1)
        self.assertEqual(self.h.proc.pid, pid)
        self.assertEqual(self.h.stop(), 0)
        self.assert_only_fixed_events()

    def test_ipv6_target_is_normalized_in_known_hosts_and_argv(self):
        self.seed_follow()
        self.github.set_gateway(**gw(ip="2001:DB8:0::5", port=2200))
        self.h.set_fake("tunnel_exit", "1")
        self.run_to_exit()
        self.assertEqual(self.h.read_text(".tunnel_known_hosts"), "[2001:db8::5]:2200 %s\n" % K_NEW)
        self.assertIn("[-p] [2200] [-l] [svc] [--] [2001:db8::5]", self.h.ssh_log())

    def test_no_local_address_fetches_first_then_connects(self):
        self.write_env(RENDEZVOUS=None, KNOWN_HOSTS=None, TUNNEL_USER=SVC)
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 10))
        self.assertEqual(self.github.request_count, 1)
        self.assertTrue(self.tunnel_argvs()[0].endswith("[--] [%s]" % NEW_IP))
        self.assertEqual(self.h.stop(), 0)

    def test_stale_follow_when_connection_unreachable_fetches_and_switches(self):
        self.seed_follow()
        self.h.set_fake("tunnel_exit", "1")                 # 舊位址連不上
        rc = self.run_to_exit()
        self.assertEqual(rc, 1)
        argvs = self.tunnel_argvs()
        self.assertTrue(argvs[0].endswith("[%s]" % OLD_IP))
        self.assertTrue(argvs[1].endswith("[%s]" % NEW_IP))   # 同行程重連到新位址

    def test_probe_failures_trigger_fetch_too(self):
        self.write_env(PROBE_FAILS="2")
        self.seed_follow()
        self.h.set_fake("probe", "503")
        self.start()
        self.assertTrue(wait_until(lambda: "gateway_updated" in self.h.stderr(), 10))
        self.assertIn("restart", self.h.stderr())
        self.assertEqual(self.h.stop(), 0)

    def test_first_probe_200_records_verified_for_seeded_follow(self):
        fields = self.seed_follow()
        self.start()
        self.assertTrue(wait_until(lambda: self.h.exists(".tunnel_generation"), 5))
        from lgwctl import layout
        self.assertEqual(self.h.read_json(".tunnel_generation"),
                         {"generation": 1, "fingerprint": layout.follow_fingerprint(follow_record(OLD_IP, 1, K_OLD))})
        self.assertEqual(self.github.request_count, 0)
        self.assertEqual(self.h.stop(), 0)

    def test_sigterm_during_normal_run_still_exits_0(self):
        self.seed_follow()
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 15))
        self.assertEqual(self.stop_daemon(), 0)
        self.assertTrue(wait_until(lambda: "tunnel_terminated" in self.h.ssh_log(), 5))


class RejectedGatewayTests(StateBase):
    INVALID = [
        ("tunnel_user", "-oProxyCommand=touch /tmp/pwned"), ("tunnel_user", "svc\n"),
        ("tunnel_user", "other"), ("tunnel_user", None), ("tunnel_user", 5),
        ("ip", "-oProxyCommand=touch /tmp/pwned"), ("ip", "-o"),
        ("ip", "fe80::1%a\n* ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVIL"),
        ("ip", "fe80::1%a b"), ("ip", "fe80::1%x@evil.example"), ("ip", "fe80::1%a,*"),
        ("ip", "fe80::1%-oProxyCommand=x"), ("ip", "fe80::1%eth0"),
        ("ip", 16909060), ("ip", "evil.example\n"), ("ip", "1\n"), ("ip", None), ("ip", ""),
        ("ip", "16909060"), ("ip", "0x7f.1"), ("ip", "0177.0.0.1"), ("ip", "1.2.3"),
        ("ip", "01.2.3.4"), ("ip", "-evil.example"), ("ip", "a b.example"), ("ip", "evil,*"),
        ("port", True), ("port", False), ("port", 0), ("port", 70000), ("port", "22"), ("port", 1.5),
        ("generation", True), ("generation", False), ("generation", "9" * 19), ("generation", -1),
        ("generation", "1 "), ("generation", None), ("generation", 2.5),
    ]
    HOSTKEY = [
        ("host_key", None), ("host_key", ""), ("host_key", K_NEW + "\n"),
        ("host_key", K_NEW + "\n* " + K_OLD), ("host_key", K_NEW + " third-column"),
        ("host_key", K_NEW.replace(" ", "  ")), ("host_key", K_NEW + "="),
        ("host_key", "ssh-ed25519 AAAA"), ("host_key", "ssh-ed25519 !!!!"), ("host_key", 7),
        ("host_key", "ssh-rsa " + K_NEW.split(" ")[1]),
    ]
    MALICIOUS = ("ProxyCommand", "pwned", "evil", "fe80", "16909060", "0x7f", "0177", "AAAAC3NzaC1lZDI1NTE5AAAAIEVIL")

    def assert_rejected(self, cls):
        rc = self.h.wait(30)
        self.assertEqual(rc, 1, "必須是 1（重試），不是 78")
        log = self.h.ssh_log()
        argvs = self.tunnel_argvs()
        self.assertEqual(len(argvs), 1, log)                                 # 只跑了最後已知位址那一次
        self.assertIn("[-l] [svc] [--] [static.example]", argvs[0])
        for needle in self.MALICIOUS:
            self.assertNotIn(needle, log)
        self.assertNotIn("accept-new", log)
        self.assertFalse(self.h.exists(".tunnel_known_hosts"))
        self.assertFalse(self.h.exists("gateway.json"))                       # 不覆寫最後已知位址
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], cls)
        backoff = self.h.read_json(".tunnel_fetch_backoff")
        self.assertEqual(backoff["consecutive_failures"], 1)
        self.assertGreaterEqual(backoff["next_allowed_epoch"], self.run_started + 1)   # FETCH_MIN_INTERVAL=1
        stderr = self.h.stderr()
        self.assertNotIn(TOKEN, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assert_only_fixed_events()

    def run_payload(self, override, cls):
        self.reset_state()
        data = gw()
        key, value = override
        if value is None and key in ("port", "generation", "ip", "tunnel_user", "host_key"):
            data[key] = None
        else:
            data[key] = value
        self.github.set_gateway(**data)
        self.h.set_fake("tunnel_exit", "1")
        self.start()
        self.assert_rejected(cls)

    def test_injection_and_malformed_fields_are_rejected_without_running_ssh(self):
        for override in self.INVALID:
            with self.subTest(override=override):
                self.run_payload(override, "invalid")

    def test_host_key_problems_fail_closed(self):
        for override in self.HOSTKEY:
            with self.subTest(override=override):
                self.run_payload(override, "hostkey_missing")
        self.reset_state()
        data = gw()
        del data["host_key"]
        self.github.set_gateway(**data)
        self.h.set_fake("tunnel_exit", "1")
        self.start()
        self.assert_rejected("hostkey_missing")

    def test_non_dict_inner_value_is_invalid(self):
        for value in ("[]", "null", '"x"', "5", "not json"):
            with self.subTest(value=value):
                self.reset_state()
                self.github.value = value
                self.github.status = 200
                self.h.set_fake("tunnel_exit", "1")
                self.start()
                self.assert_rejected("invalid")

    def test_http_failures_keep_last_known_address_and_back_off(self):
        cases = [
            (401, {}, "auth_failed"), (403, {}, "auth_failed"),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 3)}, "rate_limited"),
            (403, {"Retry-After": "2"}, "rate_limited"), (429, {"Retry-After": "2"}, "rate_limited"),
            (404, {}, "unreachable"), (500, {}, "unreachable"),
        ]
        for status, headers, cls in cases:
            with self.subTest(status=status, headers=headers):
                self.reset_state()
                self.github.reset_behavior()
                self.github.status = status
                self.github.headers = headers
                self.h.set_fake("tunnel_exit", "1")
                self.start()
                self.assert_rejected(cls)

    def test_rate_limit_headers_extend_the_persisted_backoff(self):
        self.write_env(FETCH_MIN_INTERVAL="1", FETCH_BACKOFF_MAX="2")
        self.github.status = 429
        self.github.headers = {"Retry-After": "30"}
        self.h.set_fake("tunnel_exit", "1")
        before = time.time()
        self.assertEqual(self.run_to_exit(), 1)
        self.assertGreater(self.h.read_json(".tunnel_fetch_backoff")["next_allowed_epoch"], before + 25)

    def test_timeout_oversize_and_cross_host_redirect(self):
        other = MockGitHub().start()
        self.addCleanup(other.stop)
        other.set_gateway(**gw())
        self.write_env(FETCH_TIMEOUT="0.5")
        scenarios = [
            ("timeout", lambda: setattr(self.github, "delay", 3), "unreachable"),
            ("oversize", lambda: setattr(self.github, "raw_body", b"x" * 70000), "invalid"),
            ("redirect", lambda: setattr(self.github, "redirect_to", other.base_url + "/x"), "invalid"),
        ]
        for name, arrange, cls in scenarios:
            with self.subTest(name=name):
                self.reset_state()
                self.github.reset_behavior()
                self.github.set_gateway(**gw())
                arrange()
                self.h.set_fake("tunnel_exit", "1")
                self.start()
                self.assert_rejected(cls)
        self.assertEqual(other.request_count, 0)

    def test_backoff_gates_the_next_attempt_until_it_expires(self):
        self.seed_backoff(failures=3, next_allowed=time.time() + 600)
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)
        self.assertEqual(self.github.request_count, 0)          # 冷卻中不打 GitHub
        self.assertIn("fetch_skipped", self.h.stderr())
        self.assertFalse(self.h.exists("gateway.json"))
        # 第二次執行把最短抓取間隔拉長：同一次執行內抓完一次後不會因負載拖慢、間隔過了而再抓第二次（避免對負載敏感）
        self.write_env(FETCH_MIN_INTERVAL="3600", FETCH_BACKOFF_MAX="3600")
        self.seed_backoff(failures=3, next_allowed=time.time() - 1)
        self.assertEqual(self.run_to_exit(), 1)
        self.assertEqual(self.github.request_count, 1)          # 冷卻過了才抓（且只抓一次）
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)

    def test_success_does_not_reset_failures_until_probe_200(self):
        self.seed_backoff(failures=3)
        self.h.set_fake("probe", "503")                     # 不給 200：連線若活過一個探針週期，200 會歸零計數（另一條規則）
        self.h.set_fake("tunnel_exit", "1")
        self.run_to_exit()
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 3)


class DualWatermarkTests(StateBase):
    """C-6 ②③：applied（FOLLOW_FILE）與 verified（.tunnel_generation）的比對規則。"""

    def setup_unverified_new_generation(self):
        """verified=gen1（OLD）、applied=gen2（NEW、沒驗通）。"""
        old = self.seed_follow(OLD_IP, 1, K_OLD)
        self.seed_verified(old)
        new = self.seed_follow(NEW_IP, 2, K_NEW)
        self.seed_verified(old)
        return old, new

    def test_a_upstream_reverts_to_verified_generation_is_accepted(self):
        old, new = self.setup_unverified_new_generation()
        self.github.set_gateway(**gw(OLD_IP, 1, K_OLD))
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("gateway_reverted_to_verified", self.h.stderr())
        argvs = self.tunnel_argvs()
        self.assertEqual(len(argvs), 2)
        self.assertTrue(argvs[0].endswith("[%s]" % NEW_IP))
        self.assertTrue(argvs[1].endswith("[%s]" % OLD_IP))              # 同行程重建、連回已驗證世代
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 1)
        self.assertEqual(self.h.read_text(".tunnel_known_hosts"), "%s %s\n" % (OLD_IP, K_OLD))
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "ok")

    def test_b_same_applied_generation_is_not_a_new_generation(self):
        old, new = self.setup_unverified_new_generation()
        self.seed_backoff(failures=2)
        before = os.stat(self.h.path("gateway.json")).st_mtime_ns
        known_before = self.h.read_text(".tunnel_known_hosts")
        self.github.set_gateway(**gw(NEW_IP, 2, K_NEW))
        self.h.set_fake("tunnel_exit", "1")
        started = time.time()
        self.start()
        # 冷卻的錨點是「實際抓取」的時間（守護程式在抓之前取 now），不是測試開始的時間：
        # 高負載下啟動到抓取可能要好幾秒，拿測試開始當錨點會誤判
        self.assertTrue(wait_until(lambda: self.github.request_count == 1, 20))
        fetched_by = time.time()
        self.assertEqual(self.h.wait(30), 1)
        self.assertIn("unverified_retry", self.h.stderr())
        self.assertEqual(len(self.tunnel_argvs()), 1)                       # 不重建
        backoff = self.h.read_json(".tunnel_fetch_backoff")
        self.assertEqual(backoff["consecutive_failures"], 2)                # 不累加
        self.assertGreaterEqual(backoff["next_allowed_epoch"], started + 1)   # 最短冷卻：不早於測試開始 + 1
        self.assertLessEqual(backoff["next_allowed_epoch"], fetched_by + 1)   # 不得更長：now 在抓取前記下，至多 = 抓取 + 1
        self.assertEqual(os.stat(self.h.path("gateway.json")).st_mtime_ns, before)  # 不覆寫
        self.assertEqual(self.h.read_text(".tunnel_known_hosts"), known_before)
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "ok")

    def test_c_verified_generation_with_different_fields_is_follow_invalid(self):
        old, new = self.setup_unverified_new_generation()
        self.github.set_gateway(**gw("203.0.113.99", 1, K_OLD))
        self.h.set_fake("tunnel_exit", "1")
        before = self.h.listing()["gateway.json"]
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("follow_invalid", self.h.stderr())
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "invalid")
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 1)
        self.assertEqual(self.h.listing()["gateway.json"], before)
        self.assertEqual(len(self.tunnel_argvs()), 1)

    def test_same_applied_generation_with_changed_fields_is_follow_invalid(self):
        old, new = self.setup_unverified_new_generation()
        self.github.set_gateway(**gw("203.0.113.98", 2, K_NEW))
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("follow_invalid", self.h.stderr())
        self.assertEqual(self.h.read_json("gateway.json")["ip"], NEW_IP)
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 1)

    def test_generation_below_verified_is_follow_regressed(self):
        old = self.seed_follow(NEW_IP, 5, K_NEW)
        self.seed_verified(old)
        self.github.set_gateway(**gw(OLD_IP, 4, K_OLD))
        self.h.set_fake("tunnel_exit", "1")
        before = self.h.listing()["gateway.json"]
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("follow_regressed", self.h.stderr())
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "regressed")
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 1)
        self.assertEqual(self.h.listing()["gateway.json"], before)
        self.assertEqual(self.h.read_json(".tunnel_generation")["generation"], 5)
        self.assertEqual(len(self.tunnel_argvs()), 1)

    def test_generation_string_is_accepted_and_ordered_numerically(self):
        old = self.seed_follow(OLD_IP, 9, K_OLD)
        self.seed_verified(old)
        self.github.set_gateway(**gw(NEW_IP, "10", K_NEW))
        self.h.set_fake("tunnel_exit", "1")
        self.run_to_exit()
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 10)
        self.assertIn("gateway_updated", self.h.stderr())

    def test_flap_with_same_generation_does_not_accumulate_failures(self):
        old = self.seed_follow(OLD_IP, 1, K_OLD)
        self.seed_verified(old)
        self.github.set_gateway(**gw(OLD_IP, 1, K_OLD))
        self.h.set_fake("tunnel_exit", "1")
        for _ in range(3):
            time.sleep(1.1)                                   # 讓最短冷卻過期
            self.assertEqual(self.run_to_exit(), 1)
            self.assertFalse(self.h.exists(".tunnel_fetch_backoff")
                             and self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"] > 0)
        self.assertEqual(self.github.request_count, 3)
        self.assertNotIn("gateway_updated", self.h.stderr())
        # 之後真的輪換：不會被拖到最大退避，冷卻一過就抓得到
        self.github.set_gateway(**gw(NEW_IP, 2, K_NEW))
        time.sleep(1.1)
        self.run_to_exit()
        self.assertIn("gateway_updated", self.h.stderr())
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)

    def test_follow_file_broken_while_healthy_does_not_interrupt_ssh(self):
        old = self.seed_follow()
        self.seed_verified(old)
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        for broken in ("{not json", json.dumps(follow_record(OLD_IP, 0, K_OLD)), ""):
            with self.subTest(broken=broken[:12]):
                self.h.write_state("gateway.json", broken)
                time.sleep(0.8)
                self.assertIsNone(self.h.proc.poll())
                self.assertEqual(len(self.tunnel_argvs()), 1)
                self.assertNotIn("tunnel_terminated", self.h.ssh_log())
        self.assertEqual(self.github.request_count, 0)
        events = self.h.stderr().splitlines()
        self.assertEqual(sum(e.startswith("follow_") for e in events), 1)   # 只記狀態轉換一次
        # 修復：連線真的斷掉才處理——抓到更新一代，自動恢復
        self.github.set_gateway(**gw(NEW_IP, 3, K_NEW))
        self.h.set_fake("tunnel_die", "1")
        self.assertTrue(wait_until(lambda: "gateway_updated" in self.h.stderr(), 10))
        self.assertTrue(wait_until(lambda: self.h.read_json(".tunnel_generation")["generation"] == 3, 10))
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 3)
        self.assertEqual(self.h.stop(), 0)

    def test_corrupt_follow_file_at_restart_falls_back_then_fetches(self):
        old = self.seed_follow()
        self.seed_verified(old)
        self.h.write_state("gateway.json", "{broken")
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("follow_invalid", self.h.stderr())
        self.assertTrue(self.tunnel_argvs()[0].endswith("[static.example]"))   # 最後已知基準
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)  # 抓到 2 > verified 1 → 自癒


class VerifiedWatermarkIntegrityTests(StateBase):
    """已驗證水位（.tunnel_generation）：只有「不存在」才算首次啟動；壞檔／symlink／
    過大／不可讀一律 fail-closed（不套用新位址、不覆寫水位、固定事件、exit 1）。"""

    def corrupt_verified(self, kind):
        """把 .tunnel_generation 弄成 kind；回預期的固定事件。"""
        path = self.h.path(".tunnel_generation")
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        elif os.path.lexists(path):
            os.unlink(path)
        if kind == "garbage":
            self.h.write_state(".tunnel_generation", "corrupt")
        elif kind == "wrong_shape":
            self.h.write_state(".tunnel_generation", {"generation": "1", "fingerprint": "zz"})
        elif kind == "oversize":
            self.h.write_state(".tunnel_generation", " " * 70000)
        elif kind == "symlink":
            real = os.path.join(self.tmp.name, "elsewhere")
            with open(real, "w") as f:
                json.dump({"generation": 1, "fingerprint": "a" * 64}, f)
            os.symlink(real, path)
        elif kind == "directory":
            os.mkdir(path)
        elif kind == "unreadable":
            self.h.write_state(".tunnel_generation", {"generation": 1, "fingerprint": "a" * 64})
            os.chmod(path, 0)
            self.addCleanup(lambda: os.path.lexists(path) and not os.path.islink(path)
                            and os.chmod(path, 0o600))
            return "verified_unreadable"
        return "verified_invalid"

    KINDS = ("garbage", "wrong_shape", "oversize", "symlink", "directory", "unreadable")

    def snapshot(self):
        path = self.h.path(".tunnel_generation")
        if os.path.islink(path):
            return ("link", os.readlink(path))
        if os.path.isdir(path):
            return ("dir",)
        st = os.stat(path)
        return (st.st_mode, st.st_size, st.st_mtime_ns)

    def test_bad_watermark_blocks_newer_generation_and_keeps_everything(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.reset_state()
                self.seed_follow(OLD_IP, 1, K_OLD)
                event = self.corrupt_verified(kind)
                follow_before = self.h.listing()["gateway.json"]
                before = self.snapshot()
                self.github.set_gateway(**gw(NEW_IP, 2, K_NEW))
                self.h.set_fake("tunnel_exit", "1")
                self.assertEqual(self.run_to_exit(), 1)
                self.assertIn(event, self.h.stderr())
                self.assertEqual(self.h.listing()["gateway.json"], follow_before)
                self.assertEqual(self.h.read_json("gateway.json")["generation"], 1)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "invalid")
                self.assertEqual(len(self.tunnel_argvs()), 1)         # 沒有重建到新位址
                self.assertNotIn(NEW_IP, self.h.ssh_log())
                self.assertNotIn("gateway_updated", self.h.stderr())
                self.assert_only_fixed_events()

    def test_bad_watermark_blocks_older_generation(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.reset_state()
                self.seed_follow(NEW_IP, 5, K_NEW)
                event = self.corrupt_verified(kind)
                before = self.snapshot()
                self.github.set_gateway(**gw(OLD_IP, 4, K_OLD))        # 回退
                self.h.set_fake("tunnel_exit", "1")
                self.assertEqual(self.run_to_exit(), 1)
                self.assertIn(event, self.h.stderr())
                self.assertEqual(self.h.read_json("gateway.json")["generation"], 5)
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn(OLD_IP, self.h.ssh_log())

    def test_bad_watermark_with_same_generation_different_fields_is_not_applied(self):
        for kind in ("garbage", "symlink", "unreadable"):
            with self.subTest(kind=kind):
                self.reset_state()
                self.seed_follow(OLD_IP, 3, K_OLD)
                self.corrupt_verified(kind)
                self.github.set_gateway(**gw("203.0.113.99", 3, K_NEW))
                self.h.set_fake("tunnel_exit", "1")
                self.assertEqual(self.run_to_exit(), 1)
                self.assertEqual(self.h.read_json("gateway.json")["ip"], OLD_IP)

    def test_bad_watermark_without_follow_file_still_blocks_fetch_result(self):
        for kind in ("garbage", "unreadable"):
            with self.subTest(kind=kind):
                self.reset_state()
                self.corrupt_verified(kind)
                self.h.set_fake("tunnel_exit", "1")
                self.assertEqual(self.run_to_exit(), 1)
                self.assertFalse(self.h.exists("gateway.json"))
                self.assertTrue(self.tunnel_argvs()[0].endswith("[static.example]"))

    def test_bad_watermark_at_start_uses_static_baseline_not_follow_file(self):
        self.seed_follow(OLD_IP, 1, K_OLD)
        self.corrupt_verified("garbage")
        self.h.set_fake("tunnel_exit", "1")
        self.run_to_exit()
        self.assertTrue(self.tunnel_argvs()[0].endswith("[static.example]"))

    def test_missing_watermark_is_first_start_and_still_works(self):
        self.seed_follow(OLD_IP, 1, K_OLD)
        self.assertFalse(self.h.exists(".tunnel_generation"))
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("gateway_updated", self.h.stderr())
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)
        self.assertNotIn("verified_invalid", self.h.stderr())

    def test_watermark_gone_bad_while_running_is_never_overwritten(self):
        self.seed_follow(OLD_IP, 1, K_OLD)
        self.h.set_fake("probe", "503")
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.corrupt_verified("garbage")
        self.h.set_fake("probe", "200")
        self.assertTrue(wait_until(lambda: "verified_invalid" in self.h.stderr(), 5))
        time.sleep(1.0)
        self.assertEqual(self.h.read_text(".tunnel_generation"), "corrupt")
        self.assertEqual(self.h.stderr().splitlines().count("verified_invalid"), 1)   # 不洗版
        self.assertIsNone(self.h.proc.poll())
        self.assertEqual(self.h.stop(), 0)

    def test_watermark_repaired_while_running_is_recorded_later(self):
        fields = self.seed_follow(OLD_IP, 1, K_OLD)
        self.h.set_fake("probe", "503")
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.corrupt_verified("garbage")
        self.h.set_fake("probe", "200")
        self.assertTrue(wait_until(lambda: "verified_invalid" in self.h.stderr(), 5))
        os.unlink(self.h.path(".tunnel_generation"))          # 操作人清掉壞檔
        self.assertTrue(wait_until(lambda: self.h.exists(".tunnel_generation"), 5))
        self.assertEqual(self.h.read_json(".tunnel_generation")["fingerprint"], tun.fingerprint(fields))
        self.assertEqual(self.h.stop(), 0)

    def test_read_verified_distinguishes_missing_invalid_unreadable(self):
        cfg = {"state_dir": self.h.state}
        self.assertEqual(tun.read_verified(cfg), ("missing", None))
        for kind, expected in (("garbage", "invalid"), ("wrong_shape", "invalid"),
                               ("oversize", "invalid"), ("symlink", "invalid"),
                               ("directory", "invalid"), ("unreadable", "unreadable")):
            with self.subTest(kind=kind):
                self.corrupt_verified(kind)
                self.assertEqual(tun.read_verified(cfg), (expected, None))
                if kind == "unreadable":
                    os.chmod(self.h.path(".tunnel_generation"), 0o600)
        os.rmdir(self.h.path(".tunnel_generation")) if os.path.isdir(self.h.path(".tunnel_generation")) else None
        self.h.write_state(".tunnel_generation", {"generation": 4, "fingerprint": "b" * 64})
        self.assertEqual(tun.read_verified(cfg),
                         ("ok", {"generation": 4, "fingerprint": "b" * 64}))


class VerifiedWriteFailureTests(StateBase):
    """已驗證水位寫入失敗：不標記完成、不清退避，並在後續探針重試。"""

    def lock_state_dir(self):
        os.chmod(self.h.state, 0o500)
        self.addCleanup(lambda: os.chmod(self.h.state, 0o700))

    def test_write_failure_is_not_marked_done_and_retried_after_recovery(self):
        fields = self.seed_follow(OLD_IP, 1, K_OLD)
        self.seed_backoff(failures=2)
        self.h.set_fake("probe", "503")
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.lock_state_dir()                               # 磁碟滿／權限
        self.h.set_fake("probe", "200")
        self.assertTrue(wait_until(lambda: "verified_write_failed" in self.h.stderr(), 5))
        time.sleep(1.0)                                     # 多個探針週期：仍沒寫成、不洗版
        self.assertFalse(self.h.exists(".tunnel_generation"))
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 2)
        self.assertEqual(self.h.stderr().splitlines().count("verified_write_failed"), 1)
        self.assertIsNone(self.h.proc.poll())               # 寫不進去不弄垮隧道
        os.chmod(self.h.state, 0o700)                       # 恢復
        self.assertTrue(wait_until(lambda: self.h.exists(".tunnel_generation"), 5))
        self.assertEqual(self.h.read_json(".tunnel_generation"),
                         {"generation": 1, "fingerprint": tun.fingerprint(fields)})
        self.assertTrue(wait_until(
            lambda: self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"] == 0, 5))
        self.assertEqual(self.h.stop(), 0)
        self.assert_only_fixed_events()

    def test_old_watermark_stays_effective_when_new_one_cannot_be_written(self):
        old = self.seed_follow(OLD_IP, 1, K_OLD)
        self.seed_verified(old)
        self.seed_follow(NEW_IP, 2, K_NEW)                  # applied=2、verified=1
        self.h.set_fake("probe", "503")
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.lock_state_dir()
        self.h.set_fake("probe", "200")
        self.assertTrue(wait_until(lambda: "verified_write_failed" in self.h.stderr(), 5))
        self.assertEqual(self.h.read_json(".tunnel_generation")["generation"], 1)   # 沒被半寫
        self.assertEqual(self.h.stop(), 0)

    def test_backoff_clear_failure_is_also_retried(self):
        fields = self.seed_follow(OLD_IP, 1, K_OLD)
        self.seed_verified(fields)                          # 水位已是最新，只剩清退避
        self.seed_backoff(failures=3)
        self.h.set_fake("probe", "503")
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.lock_state_dir()
        self.h.set_fake("probe", "200")
        self.assertTrue(wait_until(lambda: "verified_write_failed" in self.h.stderr(), 5))
        os.chmod(self.h.state, 0o700)
        self.assertTrue(wait_until(
            lambda: self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"] == 0, 5))
        self.assertEqual(self.h.stop(), 0)

    def test_write_verified_reports_failure_unit(self):
        cfg = {"state_dir": os.path.join(self.tmp.name, "no", "such", "ro")}
        os.makedirs(os.path.dirname(cfg["state_dir"]))
        os.chmod(os.path.dirname(cfg["state_dir"]), 0o500)
        self.addCleanup(lambda: os.chmod(os.path.dirname(cfg["state_dir"]), 0o700))
        fields = tun.validate_follow(follow_record(OLD_IP, 1, K_OLD), SVC)
        self.assertIs(tun.write_verified(cfg, fields), False)


class ExitCodeAndConfigTests(StateBase):
    def run_daemon(self, **env_overrides):
        self.write_env(**env_overrides)
        self.start()
        return self.h.wait(15)

    def test_78_only_for_unusable_static_configuration(self):
        cases = {
            "no IDENTITY": {"IDENTITY": None},
            "no local user": {"RENDEZVOUS": "static.example:22", "KNOWN_HOSTS": None},
            "no static, no github, no follow": {"RENDEZVOUS": None, "KNOWN_HOSTS": None,
                                                "TUNNEL_USER": SVC, "GATEWAY_SOURCE_REPO": None,
                                                "GATEWAY_SOURCE_PATH": None, "GATEWAY_GITHUB_KEY_FILE": None},
            "REMOTE_SSH=off": {"REMOTE_SSH": "off"},
            "user conflict": {"TUNNEL_USER": "someoneelse"},
            "bad rendezvous": {"RENDEZVOUS": "svc@-oProxyCommand=x:22"},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.run_daemon(**overrides), 78)
                self.assertEqual(self.h.stderr(), "config_missing\n")
                self.assertEqual(self.h.ssh_log(), "")
                self.assertEqual(os.listdir(self.h.state), [])
                self.assertEqual(self.github.request_count, 0)

    def test_78_for_unauthorized_api_base(self):
        self.write_env()
        for env in ({"LGW_ALLOW_INSECURE_API_BASE": None},
                    {"LGW_GATEWAY_API_BASE": "http://evil.example:80"},
                    {"LGW_GATEWAY_API_BASE": "http://10.1.2.3:80"}):
            with self.subTest(env=env):
                self.h.start({**self.api_env, **env})
                self.assertEqual(self.h.wait(15), 78)
                self.assertEqual(self.h.stderr(), "config_missing\n")
                self.assertEqual(self.h.ssh_log(), "")
        self.assertEqual(self.github.request_count, 0)

    def test_follow_file_alone_is_enough_to_start(self):
        self.write_env(RENDEZVOUS=None, KNOWN_HOSTS=None, TUNNEL_USER=SVC, GATEWAY_SOURCE_REPO=None,
                       GATEWAY_SOURCE_PATH=None, GATEWAY_GITHUB_KEY_FILE=None)
        self.seed_follow()
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.assertEqual(self.h.stop(), 0)

    def test_partial_github_config_does_not_stop_and_records_config_partial(self):
        self.write_env(GATEWAY_GITHUB_KEY_FILE=None)
        self.start()
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.assertIsNone(self.h.proc.poll())
        self.assertIn("config_partial", self.h.stderr())
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "config_partial")
        self.assertEqual(self.h.stop(), 0)
        self.assertEqual(self.github.request_count, 0)

    def test_partial_github_config_does_not_fetch_when_connection_drops(self):
        self.write_env(GATEWAY_GITHUB_KEY_FILE=None)
        self.h.set_fake("tunnel_exit", "1")
        self.start()
        self.assertEqual(self.h.wait(15), 1)
        self.assertEqual(self.github.request_count, 0)

    def test_runtime_failures_exit_1_never_78(self):
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)                       # 連線失敗＋抓到有效但被拒
        self.github.status = 500
        self.reset_state()
        self.assertEqual(self.run_to_exit(), 1)

    def test_remote_ssh_blank_and_4711_forward_both_ports_and_off_is_refused(self):
        for value in ("", "4711"):
            with self.subTest(value=value):
                self.reset_state()
                self.write_env(REMOTE_SSH=value)
                self.start()
                self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
                argv = self.tunnel_argvs()[0]
                self.assertIn("[127.0.0.1:4710:127.0.0.1:8480]", argv)
                self.assertIn("[127.0.0.1:4711:127.0.0.1:22]", argv)
                self.assertEqual(argv.count("[-R]"), 2)
                self.h.stop()
        self.assertEqual(self.run_daemon(REMOTE_SSH="off"), 78)


class CredentialTests(StateBase):
    def assert_credential_failure(self):
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)                       # exit 1，不是 78、不是例外
        stderr = self.h.stderr()
        self.assertIn("fetch_failed", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.h.read_json(".tunnel_fetch_state")["status"], "credential_unreadable")
        self.assertEqual(self.h.read_json(".tunnel_fetch_backoff")["consecutive_failures"], 1)
        self.assertEqual(self.github.request_count, 0)
        self.assertFalse(self.h.exists("gateway.json"))
        self.assertEqual(len(self.tunnel_argvs()), 1)                 # 仍用最後已知位址連過
        self.assert_only_fixed_events()

    def test_missing_token_file(self):
        os.unlink(self.token_file)
        self.assert_credential_failure()

    def test_permission_error_on_token_file(self):
        os.chmod(self.token_file, 0)
        self.assert_credential_failure()

    def test_credentials_directory_is_preferred_over_key_file(self):
        creds = os.path.join(self.tmp.name, "creds")
        os.mkdir(creds)
        with open(os.path.join(creds, "gateway-token"), "w") as f:
            f.write("CRED_DIR_TOKEN\n")
        os.unlink(self.token_file)                                    # key file 不在也沒關係
        self.h.set_fake("tunnel_exit", "1")
        self.run_to_exit({"CREDENTIALS_DIRECTORY": creds})
        self.assertEqual(self.github.requests[0][1], "Bearer CRED_DIR_TOKEN")
        self.assertEqual(self.h.read_json("gateway.json")["generation"], 2)

    def test_static_only_start_needs_no_token_file(self):
        self.write_env(GATEWAY_SOURCE_REPO=None, GATEWAY_SOURCE_PATH=None, GATEWAY_GITHUB_KEY_FILE=None)
        self.start({"CREDENTIALS_DIRECTORY": None})
        self.assertTrue(wait_until(lambda: len(self.tunnel_argvs()) == 1, 5))
        self.assertIsNone(self.h.proc.poll())
        self.assertEqual(self.h.stop(), 0)


class UnitFileTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "deploy", "units", "llmgateway-tunnel.service"), encoding="utf-8") as f:
            self.text = f.read()
        self.lines = [l.strip() for l in self.text.splitlines()]

    def test_no_load_credential_in_shipped_unit(self):
        self.assertNotIn("LoadCredential", self.text)
        self.assertNotIn("mlp-gateway.token", self.text)

    def test_hardening_and_78_semantics(self):
        for line in ("NoNewPrivileges=true", "ProtectSystem=strict", "ReadWritePaths=/var/lib/llmgateway",
                     "PrivateTmp=true", "User=llmgw", "RestartPreventExitStatus=78", "RestartSec=10",
                     "Restart=always"):
            self.assertIn(line, self.lines)

    def test_ssh_can_still_read_identity_and_known_hosts_under_protect_system(self):
        # ProtectSystem=strict 只讓檔案系統唯讀（不是隱藏）；ssh 只需讀 IDENTITY／KNOWN_HOSTS，
        # 且不能有 ProtectHome=yes 之類把 ~llmgw/.ssh 藏起來的設定。
        self.assertNotIn("ProtectHome=", self.text)
        self.assertNotIn("InaccessiblePaths", self.text)


class LeakAndReadOnlyTests(StateBase):
    def all_state_text(self, skip=()):
        out = ""
        for name in os.listdir(self.h.state):
            if name not in skip:
                out += self.h.read_text(name)
        return out

    def test_no_secret_or_topology_in_log_or_derived_state(self):
        self.seed_follow()
        secret_ip = "198.51.100.77"
        secret_key = ed25519_key(b"s")
        self.github.set_gateway(**gw(secret_ip, 2, secret_key))
        self.h.set_fake("tunnel_exit", "1")
        self.assertEqual(self.run_to_exit(), 1)
        self.assertIn("gateway_updated", self.h.stderr())
        everything = self.h.stderr() + self.all_state_text(skip=("gateway.json", ".tunnel_known_hosts"))
        for secret in (TOKEN, secret_ip, secret_key, secret_key.split(" ")[1], "static.example",
                       "key_secret", "ignored"):
            self.assertNotIn(secret, everything)
        # 狀態檔裡 token 一個字都不會出現（含 gateway.json 與 known_hosts）
        for name in os.listdir(self.h.state):
            self.assertNotIn(TOKEN, self.h.read_text(name))
            self.assertNotIn("ignored", self.h.read_text(name))
        self.assertTrue(set(self.h.read_json("gateway.json")) <= {
            "ip", "ssh_port", "tunnel_user", "host_key", "generation", "published_at"})
        self.assert_only_fixed_events()

    def test_failure_paths_do_not_leak_either(self):
        self.github.set_gateway(**gw("198.51.100.78", 2, K_NEW, tunnel_user="mallory"))
        self.h.set_fake("tunnel_exit", "1")
        self.run_to_exit()
        everything = self.h.stderr() + self.all_state_text()
        for secret in (TOKEN, "198.51.100.78", "mallory", K_NEW.split(" ")[1]):
            self.assertNotIn(secret, everything)

    def test_probe_once_with_unready_private_known_hosts_returns_2_and_writes_nothing(self):
        self.seed_follow()
        os.unlink(self.h.path(".tunnel_known_hosts"))
        before = self.h.listing()
        proc = subprocess.run([sys.executable, TUNNEL, "--probe-once"], capture_output=True, text=True,
                              env=self.h.env(self.api_env), cwd=REPO_ROOT, timeout=30)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertFalse(self.h.exists(".tunnel_known_hosts"))
        self.assertEqual(self.h.listing(), before)
        self.assertEqual(self.github.request_count, 0)
        self.assertEqual(self.h.ssh_log(), "")

    def test_probe_once_with_mismatched_known_hosts_returns_2_and_keeps_file(self):
        self.seed_follow()
        self.h.write_state(".tunnel_known_hosts", "other ssh-ed25519 AAAA\n")
        before = self.h.listing()
        proc = subprocess.run([sys.executable, TUNNEL, "--probe-once"], capture_output=True, text=True,
                              env=self.h.env(self.api_env), cwd=REPO_ROOT, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.h.listing(), before)

    def test_probe_once_healthy_is_read_only_and_never_calls_github(self):
        self.seed_follow()
        before = self.h.listing()
        proc = subprocess.run([sys.executable, TUNNEL, "--probe-once"], capture_output=True, text=True,
                              env=self.h.env(self.api_env), cwd=REPO_ROOT, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(self.h.listing(), before)
        self.assertEqual(self.github.request_count, 0)
        self.assertIn("[-l] [svc] [--] [%s]" % OLD_IP, self.h.ssh_log())
        self.h.set_fake("probe", "503")
        proc = subprocess.run([sys.executable, TUNNEL, "--probe-once"], capture_output=True, text=True,
                              env=self.h.env(self.api_env), cwd=REPO_ROOT, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.h.listing(), before)

    def test_probe_once_static_baseline_uses_configured_known_hosts(self):
        proc = subprocess.run([sys.executable, TUNNEL, "--probe-once"], capture_output=True, text=True,
                              env=self.h.env(self.api_env), cwd=REPO_ROOT, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("UserKnownHostsFile=" + os.path.join(self.tmp.name, "static_known_hosts"),
                      self.h.ssh_log())
        self.assertEqual(os.listdir(self.h.state), [])


if __name__ == "__main__":
    unittest.main()
