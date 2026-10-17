"""lgwd/keysync.py 的單元測試（#35 T2）：計時、狀態檔、密碼來源、關閉條件。

一律用假 key-sync（bash script：只記 argv 與密碼的 sha256，不記值；mode 檔控制
成功／失敗／卡住）與假密碼；不連網、不碰真憑證、不碰真機。
「用真的 lgwd 子程序驗 key 拉取不影響 API」在 tests/test_keysync_api.py。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
import unittest
import urllib.request

from lgwd.clock import FakeClock
from lgwd.keys import KEY_NAMES
from lgwd.keysync import (
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    KeySyncTimer,
    default_script,
    read_password,
    status_path_for,
)
from lgwd.main import Config, Gateway, load_config, make_keysync

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PASSWORD = "FakeKeysyncPassword_ForTests_0007"  # 假值，只在測試裡用
PASSWORD_ENV = "LLMGW_KEYS_PASSWORD"
# PYTHON_CMD 由測試換成 sys.executable（只記密碼的 sha256，不記值）
FAKE_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
set -uo pipefail
log="${FAKE_KS_LOG:-/dev/null}"
{
  printf 'argv:%s\\n' "$*"
  printf 'pid:%s\\n' "$$"
  printf 'keys_env: %s\\n' "${LGWD_KEYS:-none}"
  printf 'code_dir: %s\\n' "${LGWD_CODE_DIR:-none}"
  PYTHON_CMD -c 'import hashlib, os; pw = os.environ.get("LLMGW_KEYS_PASSWORD") or ""; print("password_sha: " + (hashlib.sha256(pw.encode()).hexdigest() if pw else "none"))'
} >> "$log"
mode="ok"
if [ -n "${FAKE_KS_MODE_FILE:-}" ] && [ -f "${FAKE_KS_MODE_FILE}" ]; then
  mode="$(cat "${FAKE_KS_MODE_FILE}")"
fi
case "$mode" in
  ok) exit 0 ;;
  fail) echo "failed=gh_auth gh 沒登入（假）" >&2; exit 5 ;;
  hang) exec sleep 30 ;;
esac
exit 1
"""


def key_text(prefix: str = "test-keysync-timer") -> str:
    lines = ["# 測試用假 key（不是真值）"]
    for sub, name in KEY_NAMES.items():
        lines.append(f"{name}={prefix}-{sub}")
    return "\n".join(lines) + "\n"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TimerCase(unittest.TestCase):
    """沙盒：假 key-sync＋假 key 檔；狀態檔寫在同一個 state 目錄。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.state = os.path.join(self.root, "state")
        self.fakebin = os.path.join(self.root, "bin")
        os.makedirs(self.state)
        os.makedirs(self.fakebin)
        self.log = os.path.join(self.root, "keysync.log")
        self.mode_file = os.path.join(self.root, "mode")
        self.set_mode("ok")
        self.script = os.path.join(self.fakebin, "key-sync")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(FAKE_SCRIPT_TEMPLATE.replace("PYTHON_CMD", shlex.quote(sys.executable)))
        os.chmod(self.script, 0o755)
        self.keys_path = os.path.join(self.state, "keys.env")
        with open(self.keys_path, "w", encoding="utf-8") as fh:
            fh.write(key_text())

    def set_mode(self, mode):
        with open(self.mode_file, "w", encoding="utf-8") as fh:
            fh.write(mode)

    def env(self, **overrides):
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.root,
            "FAKE_KS_LOG": self.log,
            "FAKE_KS_MODE_FILE": self.mode_file,
        }
        base.update(overrides)
        return base

    def new_timer(self, interval=600.0, **kwargs):
        kwargs.setdefault("timeout", 10.0)
        kwargs.setdefault("keys_path", self.keys_path)
        kwargs.setdefault("env", self.env())
        return KeySyncTimer(self.script, interval, **kwargs)

    def read_log(self):
        try:
            with open(self.log, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def status_file(self):
        return status_path_for(self.keys_path)

    def read_status(self):
        with open(self.status_file(), encoding="utf-8") as fh:
            return json.loads(fh.read())

    def last_child_pid(self):
        pids = [line.split(":", 1)[1] for line in self.read_log().splitlines()
                if line.startswith("pid:")]
        return int(pids[-1]) if pids else None

    # --- 成功／失敗／逾時 ---------------------------------------------------
    def test_成功跑一次_狀態檔一行JSON且0644(self):
        timer = self.new_timer()
        before = int(time.time())
        ok, reason = timer.run_once()
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(timer.runs, 1)
        with open(self.status_file(), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertEqual(raw.count("\n"), 1)
        data = json.loads(raw)
        self.assertEqual(set(data), {"ts", "ok", "reason"})
        self.assertTrue(data["ok"])
        self.assertEqual(data["reason"], "ok")
        self.assertLessEqual(before, data["ts"])
        self.assertLessEqual(data["ts"], int(time.time()))
        self.assertEqual(stat.S_IMODE(os.stat(self.status_file()).st_mode), 0o644)
        self.assertIn("password_sha: none", self.read_log())

    def test_失敗_記failed代碼且log與狀態不含值(self):
        self.set_mode("fail")
        timer = self.new_timer(env=self.env(**{PASSWORD_ENV: PASSWORD}))
        with self.assertLogs("lgwd.keysync", level="INFO") as logs:
            ok, reason = timer.run_once()
        self.assertFalse(ok)
        self.assertEqual(reason, "gh_auth")
        status = self.read_status()
        self.assertEqual((status["ok"], status["reason"]), (False, "gh_auth"))
        text = "\n".join(logs.output)
        self.assertIn("keysync failed reason=gh_auth", text)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(PASSWORD, self.read_log())
        self.assertIn("password_sha: " + sha256(PASSWORD), self.read_log())

    def test_逾時就殺掉子行程_狀態記timeout(self):
        self.set_mode("hang")
        timer = self.new_timer(interval=0.05, timeout=0.3, env=self.env())
        started = time.monotonic()
        ok, reason = timer.run_once()
        self.assertFalse(ok)
        self.assertEqual(reason, "timeout")
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(self.read_status()["reason"], "timeout")
        pid = self.last_child_pid()
        self.assertIsNotNone(pid)
        self.assertTrue(wait_until(lambda: not is_pid_alive(pid), timeout=5))

    def test_script被刪掉_記spawn_failed不丟例外(self):
        timer = KeySyncTimer(os.path.join(self.root, "nope"), 600.0, timeout=5.0,
                             keys_path=self.keys_path, env=self.env())
        ok, reason = timer.run_once()
        self.assertFalse(ok)
        self.assertEqual(reason, "spawn_failed")
        self.assertEqual(self.read_status()["reason"], "spawn_failed")

    # --- 計時啟動與關閉 ------------------------------------------------------
    def test_計時啟動後照interval跑_stop後不再跑(self):
        timer = self.new_timer(interval=0.05, timeout=5.0)
        timer.start()
        self.assertTrue(wait_until(lambda: timer.runs >= 2, timeout=15))
        self.assertTrue(timer.running)
        timer.stop()
        self.assertFalse(timer.running)
        runs = timer.runs
        time.sleep(0.3)
        self.assertEqual(timer.runs, runs)

    def test_stop打斷跑中的key_sync_不覆蓋舊狀態(self):
        self.new_timer().run_once()  # 先留下一次成功的狀態
        before = self.read_status()
        pids_before = self.read_log().count("pid:")
        self.set_mode("hang")
        timer = self.new_timer(interval=0.05, timeout=30.0)
        timer.start()
        try:
            self.assertTrue(wait_until(
                lambda: self.read_log().count("pid:") > pids_before, timeout=15))
            pid = self.last_child_pid()
            started = time.monotonic()
            timer.stop()
            self.assertLess(time.monotonic() - started, 8.0)
            self.assertFalse(timer.running)
            self.assertEqual(self.read_status(), before)  # 打斷的不記成失敗
            self.assertTrue(wait_until(lambda: not is_pid_alive(pid), timeout=5))
        finally:
            timer.stop()

    # --- 密碼來源與不外洩 ----------------------------------------------------
    def test_密碼_憑證目錄優先於環境變數(self):
        cred = os.path.join(self.root, "cred")
        os.makedirs(cred)
        with open(os.path.join(cred, "keys.pass"), "w", encoding="utf-8") as fh:
            fh.write(f"{PASSWORD_ENV}={PASSWORD}\n")
        env = self.env(**{"CREDENTIALS_DIRECTORY": cred, PASSWORD_ENV: "from-env-loses"})
        self.assertEqual(read_password(env), PASSWORD)
        ok, _ = self.new_timer(env=env).run_once()
        self.assertTrue(ok)
        self.assertIn("password_sha: " + sha256(PASSWORD), self.read_log())
        self.assertNotIn(PASSWORD, self.read_log())  # 假 script 只記 sha；值不落地

    def test_密碼_沒有憑證檔時看環境變數(self):
        env = self.env(**{PASSWORD_ENV: PASSWORD})
        self.assertEqual(read_password(env), PASSWORD)
        self.new_timer(env=env).run_once()
        self.assertIn("password_sha: " + sha256(PASSWORD), self.read_log())

    def test_密碼_兩邊都沒有時不放(self):
        env = self.env()
        self.assertIsNone(read_password(env))
        self.new_timer(env=env).run_once()
        self.assertIn("password_sha: none", self.read_log())

    def test_密碼_憑證檔格式不對或空值時回環境變數(self):
        cred = os.path.join(self.root, "cred")
        os.makedirs(cred)
        path = os.path.join(cred, "keys.pass")
        for content in ("not-a-password\n", f"{PASSWORD_ENV}=\n", "OTHER=1\n"):
            with self.subTest(content=content.strip()):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                env = self.env(**{"CREDENTIALS_DIRECTORY": cred, PASSWORD_ENV: PASSWORD})
                self.assertEqual(read_password(env), PASSWORD)

    def test_密碼_憑證檔讀不到時回環境變數(self):
        env = self.env(**{"CREDENTIALS_DIRECTORY": os.path.join(self.root, "missing"),
                          PASSWORD_ENV: PASSWORD})
        self.assertEqual(read_password(env), PASSWORD)

    # --- 告訴 key-sync 要用哪個 keys.env 與哪份程式（新版面） ------------------
    def test_子行程拿到同一個keys路徑與程式目錄(self):
        current = os.path.join(self.root, "opt", "llmgateway", "current")
        ops_dir = os.path.join(current, "ops-scripts")
        os.makedirs(ops_dir)
        script = os.path.join(ops_dir, "key-sync")
        shutil.copy(self.script, script)  # copy 保留執行權限
        timer = KeySyncTimer(script, 600.0, timeout=5.0, keys_path=self.keys_path,
                             env=self.env())
        self.assertEqual(timer.code_dir, current)
        ok, _ = timer.run_once()
        self.assertTrue(ok)
        log = self.read_log()
        self.assertIn("keys_env: " + os.path.abspath(self.keys_path), log)
        self.assertIn("code_dir: " + current, log)

    def test_自訂script路徑時不傳程式目錄(self):
        timer = self.new_timer()  # script 在 <root>/bin/key-sync，不是 ops-scripts
        self.assertIsNone(timer.code_dir)
        self.assertTrue(timer.run_once()[0])
        log = self.read_log()
        self.assertIn("keys_env: " + os.path.abspath(self.keys_path), log)
        self.assertIn("code_dir: none", log)

    # --- 狀態檔的原子寫 ------------------------------------------------------
    def test_狀態檔原子寫_rename失敗時原檔不動不留暫存(self):
        os.makedirs(self.status_file())  # 佔成目錄，rename 一定失敗
        keep = os.path.join(self.status_file(), "keep.txt")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("keep")
        with self.assertLogs("lgwd.keysync", level="WARNING") as logs:
            ok, _ = self.new_timer().run_once()
        self.assertTrue(ok)  # key-sync 本身成功，只有狀態檔寫不進去
        self.assertIn("keysync status write failed", "\n".join(logs.output))
        self.assertTrue(os.path.isdir(self.status_file()))
        with open(keep, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep")
        leftovers = [name for name in os.listdir(self.state)
                     if name.startswith(".keysync.status.")]
        self.assertEqual(leftovers, [])

    # --- Gateway 掛鉤 -------------------------------------------------------
    def test_Gateway掛鉤_計時器起來且API照樣200(self):
        config = Config(db=os.path.join(self.state, "llm.db"),
                        policy_path=os.path.join(REPO, "policy", "default.json"),
                        keys_path=self.keys_path, api_host="127.0.0.1", api_port=0,
                        keysync_script=self.script, keysync_interval=0.05,
                        keysync_timeout=5.0)
        gateway = Gateway(config, clock=FakeClock())
        self.assertIsNotNone(gateway.keysync)
        try:
            gateway.start()
            self.assertTrue(wait_until(lambda: gateway.keysync.runs >= 1, timeout=15))
            host, port = gateway.addr[:2]
            with urllib.request.urlopen(
                    f"http://{host}:{port}/v1/subscriptions", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            self.assertEqual(self.read_status()["reason"], "ok")
        finally:
            gateway.stop()
        self.assertFalse(gateway.keysync.running)


class ConfigKeysyncTest(unittest.TestCase):
    """load_config 的新參數與預設值（argv > env > 預設）。"""

    def test_預設(self):
        cfg = load_config([], env={})
        self.assertIsNone(cfg.keysync_script)
        self.assertEqual(cfg.keysync_interval, DEFAULT_INTERVAL)
        self.assertEqual(cfg.keysync_timeout, DEFAULT_TIMEOUT)

    def test_argv優先於env(self):
        cfg = load_config(
            ["--keysync-script", "/a/key-sync", "--keysync-interval", "60",
             "--keysync-timeout", "7"],
            env={"LGW_KEYSYNC_SCRIPT": "/b/key-sync", "LGW_KEYSYNC_INTERVAL": "30",
                 "LGW_KEYSYNC_TIMEOUT": "9"},
        )
        self.assertEqual((cfg.keysync_script, cfg.keysync_interval, cfg.keysync_timeout),
                         ("/a/key-sync", 60.0, 7.0))

    def test_env與interval0(self):
        cfg = load_config([], env={"LGW_KEYSYNC_SCRIPT": "/b/key-sync",
                                   "LGW_KEYSYNC_INTERVAL": "0",
                                   "LGW_KEYSYNC_TIMEOUT": "1.5"})
        self.assertEqual((cfg.keysync_script, cfg.keysync_interval, cfg.keysync_timeout),
                         ("/b/key-sync", 0.0, 1.5))

    def test_錯誤值丟ValueError(self):
        for argv, env in (
            (["--keysync-interval", "abc"], {}),
            (["--keysync-interval", "-1"], {}),
            ([], {"LGW_KEYSYNC_INTERVAL": "nan"}),
            ([], {"LGW_KEYSYNC_INTERVAL": "inf"}),
            (["--keysync-timeout", "0"], {}),
            (["--keysync-timeout", "-5"], {}),
            ([], {"LGW_KEYSYNC_TIMEOUT": "abc"}),
        ):
            with self.subTest(argv=argv, env=env):
                with self.assertRaises(ValueError):
                    load_config(argv, env=env)

    def test_預設路徑(self):
        self.assertEqual(default_script({}), "/opt/llmgateway/current/ops-scripts/key-sync")
        self.assertEqual(default_script({"LLMGW_PREFIX": "/tmp/lgw"}),
                         "/tmp/lgw/opt/llmgateway/current/ops-scripts/key-sync")
        self.assertEqual(default_script({"LLMGW_PREFIX": "/tmp/lgw/"}),
                         "/tmp/lgw/opt/llmgateway/current/ops-scripts/key-sync")
        self.assertEqual(status_path_for("/var/lib/llmgateway/keys.env"),
                         "/var/lib/llmgateway/.keysync.status")


class DisabledTest(unittest.TestCase):
    """關閉條件：interval 0、script 沒設或不可用（各記一行 log、回 None）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_interval0時關閉(self):
        with self.assertLogs("lgwd.main", level="INFO") as logs:
            timer = make_keysync(Config(keysync_interval=0), env={})
        self.assertIsNone(timer)
        self.assertIn("interval=0", "\n".join(logs.output))

    def test_script不存在時關閉(self):
        with self.assertLogs("lgwd.main", level="WARNING") as logs:
            timer = make_keysync(Config(keysync_script=os.path.join(self.root, "nope"),
                                        keysync_interval=600), env={})
        self.assertIsNone(timer)
        self.assertIn("script not usable", "\n".join(logs.output))

    def test_script不是執行檔時關閉(self):
        path = os.path.join(self.root, "key-sync")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o644)
        with self.assertLogs("lgwd.main", level="WARNING"):
            timer = make_keysync(Config(keysync_script=path, keysync_interval=600), env={})
        self.assertIsNone(timer)

    def test_預設路徑用LLMGW_PREFIX(self):
        scripts = os.path.join(self.root, "opt", "llmgateway", "current", "ops-scripts")
        os.makedirs(scripts)
        target = os.path.join(scripts, "key-sync")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(target, 0o755)
        timer = make_keysync(Config(keysync_interval=600),
                             env={"LLMGW_PREFIX": self.root})
        self.assertIsNotNone(timer)
        self.assertEqual(timer.script, target)

    def test_預設路徑不存在時關閉(self):
        with self.assertLogs("lgwd.main", level="WARNING"):
            timer = make_keysync(Config(keysync_interval=600),
                                 env={"LLMGW_PREFIX": self.root})
        self.assertIsNone(timer)


class GatewayDisabledTest(unittest.TestCase):
    """Gateway 在關閉條件下不建計時器（掛鉤是 None，服務照跑）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.keys_path = os.path.join(self.root, "keys.env")
        with open(self.keys_path, "w", encoding="utf-8") as fh:
            fh.write(key_text())
        self.config = dict(policy_path=os.path.join(REPO, "policy", "default.json"),
                           keys_path=self.keys_path, api_host="127.0.0.1", api_port=0)

    def _gateway(self, **overrides):
        config = dict(self.config, db=os.path.join(self.root, "llm.db"), **overrides)
        return Gateway(Config(**config), clock=FakeClock())

    def test_interval0_沒有計時器(self):
        with self.assertLogs("lgwd.main", level="INFO"):
            gateway = self._gateway(keysync_interval=0)
        self.assertIsNone(gateway.keysync)
        gateway.start()
        try:
            host, port = gateway.addr[:2]
            with urllib.request.urlopen(
                    f"http://{host}:{port}/v1/subscriptions", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            gateway.stop()

    def test_script不存在_沒有計時器(self):
        with self.assertLogs("lgwd.main", level="WARNING"):
            gateway = self._gateway(keysync_script=os.path.join(self.root, "nope"),
                                    keysync_interval=600)
        self.assertIsNone(gateway.keysync)
        # 沒 start 過就不要呼叫 stop()（shutdown() 會等 serve_forever，從沒跑過會卡住）
        gateway.api_server.server_close()

    def test_預設_沒有opt佈局時關閉(self):
        # 一般開發機沒有 /opt/llmgateway/current/...：預設不啟用，服務照常
        if os.path.exists("/opt/llmgateway/current/ops-scripts/key-sync"):
            self.skipTest("這台機器有真的 key-sync 路徑")
        with self.assertLogs("lgwd.main", level="WARNING"):
            gateway = self._gateway()
        self.assertIsNone(gateway.keysync)


if __name__ == "__main__":
    unittest.main()
