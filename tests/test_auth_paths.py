"""lgwd/auth_paths.py 的單元測試（D2 PR-3，契約 C-310）。

驗的是「單一 `auth_state_dir` 推導全部認證狀態路徑」與「時間一律經注入的 Clock」；
不碰 /var/lib/llmgateway、不讀憑證、只用暫存目錄與 FakeClock。
"""

from __future__ import annotations

import datetime as _dt
import ast
import inspect
import os
import tempfile
import unittest

import lgwd.auth_paths as auth_paths_module
from lgwd.auth_paths import (
    AUDIT_LOG_NAME,
    REPLAY_DB_NAME,
    STATUS_NAME,
    STORE_LOCK_NAME,
    TRUST_DB_NAME,
    auth_paths,
    now_seconds,
    sqlite_sidecars,
)
from lgwd.clock import FakeClock

UTC = _dt.timezone.utc


class AuthPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, mode=0o700)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_c310_derives_every_state_file_under_state_dir(self) -> None:
        """C-310：兩庫、鎖檔、status、sshsig/、審計檔都在 auth_state_dir 底下。"""
        paths = auth_paths(self.state_dir)
        self.assertEqual(paths.replay_db, os.path.join(self.state_dir, REPLAY_DB_NAME))
        self.assertEqual(paths.trust_db, os.path.join(self.state_dir, TRUST_DB_NAME))
        self.assertEqual(paths.store_lock, os.path.join(self.state_dir, STORE_LOCK_NAME))
        self.assertEqual(paths.status, os.path.join(self.state_dir, STATUS_NAME))
        self.assertEqual(paths.audit_log, os.path.join(self.state_dir, AUDIT_LOG_NAME))
        self.assertEqual(paths.sshsig_dir, os.path.join(self.state_dir, "sshsig"))
        # 兩庫在預設版面就是 design 寫的那兩個檔名。
        self.assertEqual(REPLAY_DB_NAME, "auth-replay.sqlite")
        self.assertEqual(TRUST_DB_NAME, "auth-trust.sqlite")
        self.assertEqual(STORE_LOCK_NAME, "auth-store.lock")
        self.assertEqual(STATUS_NAME, ".auth-registry.status")
        self.assertEqual(AUDIT_LOG_NAME, "auth-audit.log")

    def test_c310_allowed_signers_name_carries_generation(self) -> None:
        """C-310：`allowed_signers.<世代>`；世代不合法就拒絕組路徑。"""
        paths = auth_paths(self.state_dir)
        self.assertEqual(
            paths.allowed_signers(12), os.path.join(self.state_dir, "allowed_signers.12")
        )
        for bad in (0, -1, 2**53, True, False, "12", 1.0, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                paths.allowed_signers(bad)

    def test_c310_registry_path_defaults_and_is_configurable(self) -> None:
        """C-310：名冊檔不在狀態目錄底下，預設 /etc/llmgateway/auth-registry.json。"""
        self.assertEqual(auth_paths(self.state_dir).registry, "/etc/llmgateway/auth-registry.json")
        custom = os.path.join(self._tmp.name, "registry.json")
        self.assertEqual(auth_paths(self.state_dir, registry=custom).registry, custom)

    def test_c310_requires_absolute_paths_and_normalises(self) -> None:
        """C-310：兩個路徑都必須絕對（design「設定」）；`..`／結尾斜線會正規化。"""
        for kwargs in ({"registry": "auth-registry.json"},):
            with self.assertRaises(ValueError):
                auth_paths(self.state_dir, **kwargs)
        with self.assertRaises(ValueError):
            auth_paths("relative/state")
        noisy = os.path.join(self.state_dir, "sub", "..") + "/"
        self.assertEqual(auth_paths(noisy).state_dir, self.state_dir)

    def test_c310_broken_path_uses_injected_clock(self) -> None:
        """C-310：事故檔名 `.broken-<整數秒>`，時間來自注入的 Clock（不是 time.time）。"""
        clock = FakeClock()
        paths = auth_paths(self.state_dir)
        stamp = now_seconds(clock)
        self.assertEqual(
            paths.broken_path(paths.replay_db, clock),
            os.path.join(self.state_dir, "auth-replay.sqlite.broken-{0}".format(stamp)),
        )
        clock.advance(seconds=5)
        self.assertTrue(
            paths.broken_path(paths.replay_db, clock).endswith(".broken-{0}".format(stamp + 5))
        )
        wal, shm = sqlite_sidecars(paths.replay_db)
        self.assertEqual(wal, paths.replay_db + "-wal")
        self.assertEqual(shm, paths.replay_db + "-shm")

    def test_c310_now_seconds_is_floor_of_clock(self) -> None:
        """C-310：整數秒＝floor(clock.now().timestamp())，小數秒無條件捨去。"""
        clock = FakeClock(_dt.datetime(2026, 10, 11, 0, 0, 0, 900000, tzinfo=UTC))
        self.assertEqual(now_seconds(clock), 1791676800)

    def test_c310_module_never_reads_time_time(self) -> None:
        """C-310：時間一律經注入的 Clock；模組不得呼叫 time.time()／time.monotonic()／datetime.now()。"""
        tree = ast.parse(inspect.getsource(auth_paths_module))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in (
                    ("time", "time"), ("time", "monotonic"), ("datetime", "now")
                ):
                    offenders.append("{0}.{1}".format(owner.id, node.func.attr))
        self.assertEqual(offenders, [])
        self.assertIn("clock.now()", inspect.getsource(auth_paths_module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
