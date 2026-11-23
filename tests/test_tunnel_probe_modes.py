"""#246 PR-B：隧道探針的 old／new 與能力旗標（T-9、T-12～T-20、T-22）。

契約見 `/home/worker/work/LLMGateway-team/REPORT-246-arch.md` 的 K-1／K-5／K-8／
P-2／P-3；本檔只碰隧道腳本這一半。

- T-9：同一張旗標向量表餵 `lgwctl.capability.read`（PR-A）與隧道腳本的解析器，
  兩邊逐格相同。PR-A 尚未合併時，用 `git show origin/feat/246-healthz:lgwctl/
  capability.py` 把那份內容放進暫存目錄 import；兩者都拿不到才 skip。
- T-12：`probe_healthy` 的 P-3 矩陣（new／old × 200／404／401／503／500／逾時／
  連不上），逐格斷言結果**與請求序列**；old 的兩次 ssh 共用期限。
- T-13：`--probe-once` 的 `LGW_PROBE_API_MODE`（new／old 照表；其他值結束碼 2、
  不連線）。
- T-14：K-8，不認得的 argv 回 2，不起 `-N`、不寫狀態檔。
- T-15～T-20：守護程式的旗標（每次探都重讀、忽略 `LGW_PROBE_API_MODE`、沒設＝
  old、invalid 每條連線只記一次 `capability_invalid`）。
- T-22：v0.8.0 行為的替身（多一個 argv 就進 daemon 分支），用 K-7 的呼叫方式
  （argv 恰好 `--probe-once`、模式走環境變數）不會進 daemon。llmgwctl 側的
  整合由 PR-A 的 T-21 覆蓋；本檔守的是「替身不會因為多一個參數就起第二條隧道」。
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_tunnel_follow import ed25519_key, load_tunnel
from tests.tunnel_fakes import FAKE_SSH, Harness, REPO_ROOT, TUNNEL, wait_until

tun = load_tunnel()

HEALTH = "/healthz"
LIST = "/v1/subscriptions"
VALID = '{"schema": 1, "probe": "healthz"}\n'
K_HOST = ed25519_key(b"h")


def probe_requests(log):
    """假 ssh 記下的探針請求行 → 路徑序列（例：['/healthz', '/v1/subscriptions']）。"""
    paths = []
    for line in log.splitlines():
        if line.startswith("request: "):
            paths.append(line.split()[2])
    return paths


def probe_argv_count(log):
    return log.count("[-W]")


class DirectProbeBase(unittest.TestCase):
    """直接呼叫 `probe_healthy`；假 ssh 用 tunnel_fakes 的 FAKE_SSH。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.fake_dir = os.path.join(self.root, "fake")
        os.makedirs(self.fake_dir)
        self.ssh = os.path.join(self.root, "fake-ssh")
        with open(self.ssh, "w", encoding="utf-8") as f:
            f.write(FAKE_SSH)
        os.chmod(self.ssh, 0o755)
        self._old_env = os.environ.get("FAKE_DIR")
        os.environ["FAKE_DIR"] = self.fake_dir
        self.addCleanup(self._restore_env)
        self.target = {
            "host": "static.example",
            "port": 22,
            "known_hosts": os.path.join(self.root, "known_hosts"),
            "follow": None,
        }

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("FAKE_DIR", None)
        else:
            os.environ["FAKE_DIR"] = self._old_env

    def cfg(self, interval=0.2):
        return {
            "ssh_path": self.ssh,
            "identity": "/nonexistent/id",
            "connect_timeout": 5,
            "local_user": "svc",
            "remote_api": 4710,
            "probe_interval": interval,
        }

    def set_fake(self, name, value=None):
        path = os.path.join(self.fake_dir, name)
        if value is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(value)

    def set_codes(self, healthz=None, fallback=None):
        self.set_fake("probe.healthz", healthz)
        self.set_fake("probe.v1_subscriptions", fallback)

    def log(self):
        try:
            with open(os.path.join(self.fake_dir, "ssh.log"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def reset_log(self):
        try:
            os.unlink(os.path.join(self.fake_dir, "ssh.log"))
        except FileNotFoundError:
            pass

    def probe(self, mode, interval=0.2):
        return tun.probe_healthy(self.cfg(interval), self.target, mode)


class HttpStatusParsingTests(unittest.TestCase):
    """T-12 的狀態碼解析：`HTTP/` 開頭、第二個 token 三位數字。"""

    def test_well_formed_status_line(self):
        self.assertEqual(tun.http_status(b"HTTP/1.0 200 OK\r\n\r\n"), 200)
        self.assertEqual(tun.http_status(b"HTTP/1.1 404 Not Found\r\n"), 404)

    def test_bad_shape_is_none(self):
        for raw in (b"", b"garbage\r\n", b"HTTP/1.0\r\n", b"HTTP/1.0 20 OK\r\n",
                    b"HTTP/1.0 2000 OK\r\n", b"HTTP/1.0 2O0 OK\r\n", b"200 OK\r\n"):
            with self.subTest(raw=raw):
                self.assertIsNone(tun.http_status(raw))


class ProbeMatrixTests(DirectProbeBase):
    """T-12：P-3 的 14 格＋old 的共用期限。"""

    def cell(self, mode, healthz, fallback=None):
        self.reset_log()
        self.set_codes(healthz, fallback)
        return self.probe(mode), probe_requests(self.log())

    def test_healthz_200_is_healthy_and_never_touches_list(self):
        for mode in ("new", "old"):
            with self.subTest(mode=mode):
                healthy, requests = self.cell(mode, "200", "200")
                self.assertTrue(healthy)
                self.assertEqual(requests, [HEALTH])

    def test_new_404_fails_without_fallback(self):
        healthy, requests = self.cell("new", "404", "200")
        self.assertFalse(healthy)
        self.assertEqual(requests, [HEALTH])
        self.assertEqual(len(requests), 1)

    def test_old_404_falls_back_and_needs_list_200(self):
        healthy, requests = self.cell("old", "404", "200")
        self.assertTrue(healthy)
        self.assertEqual(requests, [HEALTH, LIST])

        healthy, requests = self.cell("old", "404", "503")
        self.assertFalse(healthy)
        self.assertEqual(requests, [HEALTH, LIST])

    def test_other_codes_fail_without_fallback(self):
        """401／503／500 與連不上（ssh 非 0 結束）：兩種模式都失敗、都不打 list。"""
        for mode in ("new", "old"):
            for code in ("401", "503", "500", "fail"):
                with self.subTest(mode=mode, code=code):
                    healthy, requests = self.cell(mode, code, "200")
                    self.assertFalse(healthy)
                    self.assertEqual(requests, [HEALTH])

    def test_timeout_is_failure_without_fallback(self):
        for mode in ("new", "old"):
            with self.subTest(mode=mode):
                started = time.monotonic()
                healthy, requests = self.cell(mode, "hang", "200")
                self.assertFalse(healthy)
                self.assertEqual(requests, [HEALTH])
                self.assertLess(time.monotonic() - started, 2.0 + 1.0)

    def test_old_shares_one_deadline_between_both_ssh_calls(self):
        """old＋404 後 list 慢回應：合計不超過期限（2.0s）＋0.5s 誤差。"""
        self.reset_log()
        self.set_codes("404", "hang")
        started = time.monotonic()
        healthy = self.probe("old", interval=0.2)
        elapsed = time.monotonic() - started
        self.assertFalse(healthy)
        self.assertLessEqual(elapsed, 2.0 + 0.5, elapsed)
        self.assertEqual(probe_requests(self.log()), [HEALTH, LIST])

    def test_mode_is_validated(self):
        for bad in ("x", "", None):
            with self.subTest(mode=bad):
                with self.assertRaises(ValueError):
                    self.probe(bad)


class HarnessBase(unittest.TestCase):
    """跑真的隧道行程（假 ssh）的沙箱。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.h = Harness(self.root, fake_ssh=True)
        self.addCleanup(self.h.stop)
        self.write_env()

    def write_env(self, **extra):
        values = {
            "IDENTITY": "/nonexistent/id",
            "RENDEZVOUS": "svc@static.example:22",
            "KNOWN_HOSTS": os.path.join(self.root, "static_known_hosts"),
            "SSH_PATH": self.h.fake_ssh,
            "PROBE_INTERVAL": "0.2",
            "PROBE_FAILS": "3",
        }
        values.update(extra)
        self.h.write_env(values)

    def start(self, extra_env=None):
        return self.h.start(extra_env)

    def run_tunnel(self, args, extra_env=None):
        return subprocess.run(
            [sys.executable, TUNNEL, *args],
            capture_output=True, text=True, env=self.h.env(extra_env), cwd=REPO_ROOT)

    def requests(self):
        return probe_requests(self.h.ssh_log())

    def probe_count(self):
        return probe_argv_count(self.h.ssh_log())

    def wait_probes(self, count, timeout=15):
        self.assertTrue(
            wait_until(lambda: self.probe_count() >= count, timeout),
            "只看到 %d 次探針" % self.probe_count())

    def state_files(self):
        try:
            return sorted(os.listdir(self.h.state))
        except FileNotFoundError:
            return []

    def write_capability(self, directory, content=VALID):
        """在 `<directory>/deploy/capabilities.json` 建旗標；回旗標檔的絕對路徑。"""
        deploy = os.path.join(directory, "deploy")
        os.makedirs(deploy, exist_ok=True)
        path = os.path.join(deploy, "capabilities.json")
        if content is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return path


class ProbeOnceModeTests(HarnessBase):
    """T-13：`--probe-once` 的模式來源與值域。"""

    def test_new_mode_200_is_zero_and_does_not_fall_back(self):
        self.h.set_fake("probe.healthz", "200")
        res = self.run_tunnel(["--probe-once"], {"LGW_PROBE_API_MODE": "new"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.requests(), [HEALTH])

    def test_new_mode_404_is_one_without_list(self):
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        res = self.run_tunnel(["--probe-once"], {"LGW_PROBE_API_MODE": "new"})
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(self.requests(), [HEALTH])

    def test_old_mode_404_falls_back_to_list(self):
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        res = self.run_tunnel(["--probe-once"], {"LGW_PROBE_API_MODE": "old"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.requests(), [HEALTH, LIST])

    def test_env_mode_wins_over_the_capability_file(self):
        """有 LGW_PROBE_API_MODE 就不看檔案（檔案故意寫成 invalid）。"""
        cap = self.write_capability(os.path.join(self.root, "bad"), "not json\n")
        self.h.set_fake("probe.healthz", "200")
        res = self.run_tunnel(
            ["--probe-once"],
            {"LGW_PROBE_API_MODE": "new", "LGW_CAPABILITIES_FILE": cap})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.requests(), [HEALTH])

    def test_invalid_env_values_exit_2_without_connecting(self):
        for value in ("", "NEW", "xx", "new "):
            with self.subTest(value=value):
                self.h.set_fake("probe.healthz", "200")
                res = self.run_tunnel(["--probe-once"], {"LGW_PROBE_API_MODE": value})
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertEqual(self.h.ssh_log(), "")
                self.assertEqual(self.state_files(), [])

    def test_no_env_reads_the_capability_file(self):
        cap = self.write_capability(os.path.join(self.root, "new"))
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        res = self.run_tunnel(["--probe-once"], {"LGW_CAPABILITIES_FILE": cap})
        # 旗標是 new：404 是失敗、不打 list。
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(self.requests(), [HEALTH])


class UnknownArgvTests(HarnessBase):
    """T-14（K-8）：不認得的 argv 一律 2，不起 daemon、不寫檔、不連線。"""

    def test_unknown_argv_exits_2_without_side_effects(self):
        self.write_capability(os.path.join(self.root, "new"))
        for args in (["--probe-once", "--x"], ["--bogus"],
                     ["--probe-once", "--api-mode", "new"]):
            with self.subTest(args=args):
                res = self.run_tunnel(args)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertEqual(self.h.ssh_log(), "")
                self.assertEqual(self.state_files(), [])
                self.assertEqual(self.h.stderr(), "")


class DaemonCapabilityTests(HarnessBase):
    """T-15～T-20：守護程式每次探都重讀能力旗標。"""

    def setUp(self):
        super().setUp()
        self.seed_follow()

    def seed_follow(self):
        record = {
            "ip": "203.0.113.10",
            "ssh_port": 22,
            "tunnel_user": "svc",
            "host_key": K_HOST,
            "generation": 1,
            "published_at": "2026-11-21T00:00:00Z",
        }
        fields = tun.validate_follow(record, "svc")
        self.h.write_state("gateway.json", record)
        self.h.write_state(".tunnel_known_hosts", tun.known_hosts_line(fields))

    def test_T15_new_flag_healthz_200_no_restart_and_marks_verified(self):
        cap = self.write_capability(os.path.join(self.root, "new"))
        self.h.set_fake("probe.healthz", "200")
        self.start({"LGW_CAPABILITIES_FILE": cap})
        self.wait_probes(2)
        self.assertIsNone(self.h.proc.poll(), "daemon 不該重建")
        self.assertTrue(self.h.exists(".tunnel_generation"),
                        self.state_files())
        stderr = self.h.stderr()
        self.assertNotIn("probe_fail", stderr)
        self.assertNotIn("restart", stderr)
        self.h.stop()

    def test_T16_switch_current_to_old_version_at_runtime_is_reread(self):
        root = os.path.join(self.root, "versions")
        new_dir = os.path.join(root, "lgw-v1.2.3")
        old_dir = os.path.join(root, "lgw-v1.1.0")
        self.write_capability(new_dir)
        os.makedirs(old_dir, exist_ok=True)             # 舊版沒有旗標檔
        current = os.path.join(self.root, "current")
        os.symlink(new_dir, current)
        cap = os.path.join(current, "deploy", "capabilities.json")
        self.h.set_fake("probe.healthz", "200")
        self.start({"LGW_CAPABILITIES_FILE": cap})
        self.wait_probes(2)
        # 原子把 current 換成舊版，服務同步換成舊服務（/healthz 404、list 200）。
        tmp = os.path.join(self.root, "current.new")
        os.symlink(old_dir, tmp)
        os.replace(tmp, current)
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        self.assertTrue(wait_until(lambda: LIST in self.requests(), 15),
                        self.h.ssh_log())
        self.assertIsNone(self.h.proc.poll(), "切到 old 後不該重建")
        self.assertNotIn("restart", self.h.stderr())
        self.h.stop()

    def test_T17_invalid_flag_probes_old_and_notes_event_once(self):
        cap = self.write_capability(os.path.join(self.root, "bad"), "not json\n")
        self.write_env(PROBE_INTERVAL="0.1", PROBE_FAILS="2")
        # invalid→old：404 退回 list 200 就健康；若被誤判成 new 會連敗重建。
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        self.start({"LGW_CAPABILITIES_FILE": cap})
        self.wait_probes(3)
        self.assertIsNone(self.h.proc.poll(), "invalid 旗標要降為 old、不重建")
        stderr = self.h.stderr()
        self.assertEqual(stderr.count("capability_invalid"), 1, stderr)
        self.assertNotIn("restart", stderr)
        self.assertEqual(self.requests()[:2], [HEALTH, LIST])
        self.h.stop()

    def test_T18_new_flag_against_old_service_restarts_without_list(self):
        cap = self.write_capability(os.path.join(self.root, "new"))
        self.write_env(PROBE_INTERVAL="0.1", PROBE_FAILS="2")
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        self.start({"LGW_CAPABILITIES_FILE": cap})
        self.assertEqual(self.h.wait(20), 1)
        stderr = self.h.stderr()
        self.assertEqual(stderr.count("probe_fail"), 2, stderr)
        self.assertIn("restart", stderr)
        self.assertEqual(self.requests(), [HEALTH, HEALTH])

    def test_T19_transient_failures_do_not_restart_while_crossing_over(self):
        cap = self.write_capability(os.path.join(self.root, "new"))
        self.write_env(PROBE_INTERVAL="0.4", PROBE_FAILS="3")
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        self.start({"LGW_CAPABILITIES_FILE": cap})
        self.wait_probes(2)                              # 已經連敗 2 次
        self.h.set_fake("probe.healthz", "200")          # 服務起來了
        self.assertTrue(wait_until(lambda: self.h.exists(".tunnel_generation"), 10))
        self.assertIsNone(self.h.proc.poll(), "短暫失敗不該重建")
        stderr = self.h.stderr()
        self.assertNotIn("restart", stderr)
        self.assertLessEqual(stderr.count("probe_fail"), 2, stderr)
        self.h.stop()

    def test_T20_without_env_is_old_and_falls_back(self):
        self.write_env(PROBE_INTERVAL="0.1", PROBE_FAILS="2")
        self.h.set_fake("probe.healthz", "404")
        self.h.set_fake("probe.v1_subscriptions", "200")
        self.start()
        self.wait_probes(3)
        self.assertIsNone(self.h.proc.poll(), "沒設就是 old：404 退回 list 200 仍健康")
        self.assertNotIn("restart", self.h.stderr())
        self.assertEqual(self.requests()[:2], [HEALTH, LIST])
        self.h.stop()


class LegacyCallShapeTests(HarnessBase):
    """T-22：v0.8.0 的替身收到多一個 argv 會進 daemon 分支；K-7 的呼叫不會。"""

    def setUp(self):
        super().setUp()
        self.legacy = os.path.join(self.root, "legacy-tunnel")
        self.legacy_log = os.path.join(self.root, "legacy.log")
        with open(self.legacy, "w", encoding="utf-8") as f:
            f.write("""#!/usr/bin/env bash
if [[ "$#" -eq 0 ]]; then printf 'daemon\\n' >> "%s"; exit 0; fi
if [[ "$#" -eq 1 && "$1" == "--probe-once" ]]; then printf 'probe\\n' >> "%s"; exit 0; fi
printf 'daemon\\n' >> "%s"
""" % (self.legacy_log, self.legacy_log, self.legacy_log))
        os.chmod(self.legacy, 0o755)

    def run_legacy(self, args):
        env = dict(os.environ)
        env["LGW_PROBE_API_MODE"] = "new"
        return subprocess.run([self.legacy, *args], env=env, capture_output=True)

    def read_log(self):
        try:
            with open(self.legacy_log, encoding="utf-8") as f:
                return f.read().split()
        except FileNotFoundError:
            return []

    def test_mode_via_env_keeps_argv_exactly_probe_once(self):
        """K-7：模式走環境變數，argv 只剩 --probe-once → 舊腳本走探針。"""
        self.assertEqual(self.run_legacy(["--probe-once"]).returncode, 0)
        self.assertEqual(self.read_log(), ["probe"])

    def test_extra_argv_would_start_a_second_daemon(self):
        """反例：把模式塞進 argv 會讓舊腳本起出第二條隧道（所以 K-7 不能用 argv）。"""
        self.assertEqual(
            self.run_legacy(["--probe-once", "--api-mode", "new"]).returncode, 0)
        self.assertEqual(self.read_log(), ["daemon"])

    def test_new_script_accepts_exactly_probe_once(self):
        """新腳本只認 --probe-once／--fetch-check；多一個參數就回 2（T-14 的同源）。"""
        res = self.run_tunnel(["--probe-once", "--api-mode", "new"])
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)


class UnitFileCapabilityTests(unittest.TestCase):
    """system unit 帶能力旗標；user unit 不改。"""

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_system_unit_sets_capabilities_file(self):
        unit = self.read(os.path.join(REPO_ROOT, "deploy", "units",
                                      "llmgateway-tunnel.service"))
        self.assertIn(
            "Environment=LGW_CAPABILITIES_FILE="
            "/opt/llmgateway/current/deploy/capabilities.json", unit)

    def test_user_unit_does_not_set_capabilities_file(self):
        unit = self.read(os.path.join(REPO_ROOT, "ops-scripts",
                                      "llmgateway-tunnel.service"))
        self.assertNotIn("LGW_CAPABILITIES_FILE", unit)


# ---------------------------------------------------------------------------
# T-9：兩份解析器的同一張向量表
# ---------------------------------------------------------------------------

def _load_pra_capability(workdir):
    """PR-A 的 `lgwctl.capability`：已合併就直接 import，否則從分支取來源。"""
    try:
        from lgwctl import capability
        return capability
    except ImportError:
        pass
    for ref in ("origin/feat/246-healthz", "feat/246-healthz"):
        res = subprocess.run(
            ["git", "show", "%s:lgwctl/capability.py" % ref],
            cwd=REPO_ROOT, capture_output=True)
        if res.returncode != 0:
            continue
        path = os.path.join(workdir, "capability_pra.py")
        with open(path, "wb") as f:
            f.write(res.stdout)
        loader = importlib.machinery.SourceFileLoader(
            "lgwctl_capability_pra", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module       # dataclass 需要 module 先註冊
        loader.exec_module(module)
        return module
    return None


class CapabilityVectorTests(unittest.TestCase):
    """T-9（K-1／K-6）：正負形狀餵兩份解析器，逐格比對。"""

    @classmethod
    def setUpClass(cls):
        cls._tool_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tool_tmp.cleanup)
        cls.lgwctl = _load_pra_capability(cls._tool_tmp.name)

    def setUp(self):
        if self.lgwctl is None:
            self.skipTest(
                "與 lgwctl 側一致性待 PR-A 合併後補一行："
                "lgwctl/capability.py 與 feat/246-healthz 都拿不到")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def version_dir(self, name):
        return os.path.join(self.root, name)

    def capability_file(self, version, content=None, kind="file"):
        deploy = os.path.join(version, "deploy")
        os.makedirs(deploy, exist_ok=True)
        path = os.path.join(deploy, "capabilities.json")
        if kind == "missing":
            return path
        if kind == "dir":
            os.makedirs(path)
            return path
        if kind == "symlink":
            target = os.path.join(self.root, "elsewhere.json")
            with open(target, "w", encoding="utf-8") as f:
                f.write(VALID)
            os.symlink(target, path)
            return path
        raw = content.encode("utf-8") if isinstance(content, str) else content
        with open(path, "wb") as f:
            f.write(raw)
        if kind == "eacces":
            os.chmod(path, 0)
        return path

    def compare(self, name, **kwargs):
        version = self.version_dir(name)
        path = self.capability_file(version, **kwargs)
        tunnel_result = tun.read_capability(path)
        lgw_result = self.lgwctl.read(version)
        self.assertEqual(
            (tunnel_result.state, tunnel_result.mode),
            (lgw_result.state, lgw_result.mode),
            "%s：隧道 %r vs lgwctl %r" % (name, tunnel_result, lgw_result))
        return tunnel_result.state, tunnel_result.mode

    def test_positive(self):
        self.assertEqual(self.compare("positive", content=VALID), ("ok", "new"))

    def test_missing_is_missing_old(self):
        self.assertEqual(
            self.compare("missing", kind="missing"), ("missing", "old"))

    def test_invalid_shapes_agree(self):
        cases = {
            "empty": "",
            "non-utf8": b'{"schema": 1, "probe": "healthz\xff"}\n',
            "not-json": "not json\n",
            "array": "[1]\n",
            "schema-2": '{"schema": 2, "probe": "healthz"}\n',
            "schema-true": '{"schema": true, "probe": "healthz"}\n',
            "schema-string": '{"schema": "1", "probe": "healthz"}\n',
            "probe-list": '{"schema": 1, "probe": "list"}\n',
            "extra-key": '{"schema": 1, "probe": "healthz", "x": 1}\n',
            "missing-key": '{"schema": 1}\n',
            "duplicate-key": '{"schema": 1, "probe": "healthz", "probe": "healthz"}\n',
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.compare(name, content=content),
                                 ("invalid", "old"))

    def test_size_boundary_agrees(self):
        body = VALID.rstrip("\n")
        exactly = body + " " * (tun.CAPABILITY_MAX_BYTES - len(body))
        self.assertEqual(len(exactly), tun.CAPABILITY_MAX_BYTES)
        self.assertEqual(self.compare("exactly-4096", content=exactly),
                         ("ok", "new"))
        over = exactly + " "
        self.assertEqual(self.compare("over-4096", content=over),
                         ("invalid", "old"))

    def test_symlink_dir_and_eacces_agree(self):
        self.assertEqual(self.compare("symlink", kind="symlink"),
                         ("invalid", "old"))
        self.assertEqual(self.compare("directory", kind="dir"),
                         ("invalid", "old"))
        if os.geteuid() != 0:
            self.assertEqual(self.compare("eacces", content=VALID, kind="eacces"),
                             ("unreadable", "old"))


if __name__ == "__main__":
    unittest.main()
