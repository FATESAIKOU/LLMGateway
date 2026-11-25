"""`lgwd/auth.py` 的啟動組裝與持有者／生命週期測試（#222 D2 PR-6a；C-605、C-610、C-611）。

- C-605（程式庫層）：`open_runtime` 的順序、每種拒絕分類、warn 的降級與「不重開庫」。
- C-610：REPORT-D2c-arch §2.3 的 W-1～W-20（各案例在測試名或 docstring 標編號；
  已在其他檔驗過的案例在對應測試的 docstring 註明）。
- C-611：30 秒短版持續率（功能支一律執行；時間支只在機器未被**外部**共載時斷言：開始前
  就過載，或跑完後過載且開始前機器就不是閒置的；預設 5 秒商店期限與 2 秒驗章期限，不放寬）。

全部用暫存目錄、真的暫存金鑰與真的 `ssh-keygen`；不碰 `/var/lib`、`/etc`、不讀憑證、
不連真機。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from lgwd import auth, sshsig
from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths, now_seconds
from lgwd.auth_registry import parse_registry
from lgwd.clock import FakeClock, SystemClock
from lgwd.http import make_api_server
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer, b64url, key_entry, registry_bytes
from tests.test_auth_gate import FakeVerifier, Headers, make_framing, nonce

try:  # python3 -m pytest
    from .test_http import FakeService
except ImportError:  # python3 -m unittest discover -s tests
    from test_http import FakeService

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
TARGET = "/v1/subscriptions"
HOST = "127.0.0.1"


class BlockingVerifier(FakeVerifier):
    """`verify()` 進去就卡住，直到測試放行（給窗口與停止順序用）。"""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def verify(self, message, blob, entry, signers):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=10)
        return self.outcome


class RuntimeCase(unittest.TestCase):
    """共用兩把暫存簽章金鑰；每個測試一份狀態目錄、兩個庫與一份名冊。"""

    signer: Signer
    signer_b: Signer

    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = Signer(cls._keys.name, "k-a")
        cls.signer_b = Signer(cls._keys.name, "k-b")

    @classmethod
    def tearDownClass(cls):
        cls._keys.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        os.makedirs(self.state_dir)
        self.paths = auth_paths(
            self.state_dir, registry=os.path.join(self.dir, "auth-registry.json"))
        create_stores(self.paths.trust_db, self.paths.replay_db)
        self.clock = FakeClock(NOW)
        self.write_registry(self.registry_data(1))

    # --- 工具 ---

    def registry_data(self, generation: int, entries=None) -> bytes:
        return registry_bytes(
            generation, entries if entries is not None
            else [key_entry("k-a", self.signer)])

    def write_registry(self, raw: bytes) -> None:
        write_registry_file(self.paths.registry, raw)

    def config(self, mode: str = "enforce", **over) -> auth.AuthConfig:
        kwargs = dict(
            mode=mode,
            registry_path=self.paths.registry,
            state_dir=self.state_dir,
            per_key_max=600,
            poll_interval=0.0,
            collect_lock_waits=True,
        )
        kwargs.update(over)
        return auth.AuthConfig(**kwargs)

    def open(self, mode: str = "enforce", *, clock=None, owner_uid: int = 0,
             registry_owner_uid: int | None = None, **over):
        runtime = auth.open_runtime(
            self.config(mode, **over),
            clock if clock is not None else self.clock,
            owner_uid=owner_uid,
            registry_owner_uid=(
                os.getuid() if registry_owner_uid is None else registry_owner_uid),
        )
        self.addCleanup(runtime.stop)
        self.runtime = runtime
        return runtime

    def headers(self, *, key_id="k-a", signer=None, method="GET", target=TARGET,
                body=b"", timestamp=None, nonce_value=None):
        signer = signer or self.signer
        return Headers(signer.headers(
            key_id=key_id, method=method, target=target, body=body,
            timestamp=now_seconds(self.clock) if timestamp is None else timestamp,
            nonce=nonce_value,
        ))

    def auth(self, headers, *, gate=None, method="GET", target=TARGET, body=b""):
        gate = gate if gate is not None else self.runtime.gate
        return gate.authenticate(
            method=method, framing=make_framing(target), headers=headers, body=body)

    def status(self) -> dict:
        with open(self.paths.status, encoding="utf-8") as handle:
            return json.loads(handle.read())

    # --- 共用斷言 ---

    def assertRefused(self, kind: str, *, store=None):
        with self.assertRaises(auth.StartupRefused) as caught:
            self.open()
        self.assertEqual(caught.exception.kind, kind, caught.exception.message)
        if store is not None:
            self.assertEqual(caught.exception.store, store)
        return caught.exception


class ConfigTests(unittest.TestCase):
    """C-605 前置／C-606 的程式庫面：設定值不合法時丟 `ValueError`（6b 轉成結束碼 2）。"""

    def test_defaults_match_the_design(self):
        cfg = auth.AuthConfig()
        self.assertEqual(cfg.mode, "off")
        self.assertEqual(cfg.per_key_max, 600)
        self.assertEqual(cfg.verify_concurrency, 16)
        self.assertEqual(cfg.verify_timeout, 2.0)
        self.assertEqual(cfg.sshkeygen_path, "/usr/bin/ssh-keygen")
        self.assertTrue(os.path.isabs(cfg.state_dir))
        self.assertTrue(os.path.isabs(cfg.registry_path))

    def test_invalid_values_are_rejected(self):
        for over in ({"mode": "strict"}, {"mode": ""},
                     {"verify_concurrency": 0}, {"verify_concurrency": 33},
                     {"verify_concurrency": True}, {"verify_timeout": 0},
                     {"per_key_max": 0}, {"per_key_max": 1.5},
                     {"state_dir": "relative/path"}, {"sshkeygen_path": "ssh"},
                     {"busy_timeout": 0}, {"poll_interval": -1}):
            with self.subTest(**over):
                with self.assertRaises(ValueError):
                    auth.AuthConfig(**over)


# ---------------------------------------------------------------------------
# C-605：啟動順序、拒絕分類、warn 降級
# ---------------------------------------------------------------------------

class StartupEnforceTests(RuntimeCase):
    """C-605：enforce 的每一種拒絕（6b 轉成結束碼 78；埠不能先 bind）。"""

    def test_C605_lock_busy_refuses_in_both_modes(self):
        from lgwd.auth_db import acquire_store_lock
        holder = acquire_store_lock(self.paths.store_lock)
        self.addCleanup(holder.release)
        self.assertRefused(auth.KIND_LOCK_BUSY)
        with self.assertRaises(auth.StartupRefused) as caught:
            self.open(mode="warn")
        self.assertEqual(caught.exception.kind, auth.KIND_LOCK_BUSY)

    def test_C605_trust_store_missing_names_rebuild_trust(self):
        os.unlink(self.paths.trust_db)
        failure = self.assertRefused(auth.KIND_STORE_MISSING, store="trust")
        self.assertIn("rebuild-trust", failure.message)
        self.assertFalse(os.path.exists(self.paths.trust_db),
                         msg="lgwd 不得建庫")

    def test_C605_replay_store_missing_names_recover_store(self):
        os.unlink(self.paths.replay_db)
        failure = self.assertRefused(auth.KIND_STORE_MISSING, store="replay")
        self.assertIn("recover-store", failure.message)
        self.assertFalse(os.path.exists(self.paths.replay_db))

    def test_C605_unreadable_store_is_unavailable_not_missing(self):
        """W-9（第 1 段）：庫檔存在但打不開＝「檢查權限」，不是 recover-store。"""
        for path, store in ((self.paths.trust_db, "trust"),
                            (self.paths.replay_db, "replay")):
            with self.subTest(store=store):
                os.chmod(path, 0)
                self.addCleanup(os.chmod, path, 0o600)
                failure = self.assertRefused(auth.KIND_STORE_UNAVAILABLE, store=store)
                self.assertIn("permissions", failure.message)
                self.assertNotIn("recover-store", failure.message)
                self.assertNotIn("rebuild-trust", failure.message)
                os.chmod(path, 0o600)

    def test_C605_identity_mismatch_refuses(self):
        connection = sqlite3.connect(self.paths.replay_db)
        try:
            connection.execute(
                "UPDATE meta SET value = 'deadbeef' WHERE key = 'identity'")
            connection.commit()
        finally:
            connection.close()
        self.assertRefused(auth.KIND_IDENTITY_MISMATCH)

    def test_C605_registry_and_snapshot_both_invalid(self):
        self.write_registry(b"{ not json")
        failure = self.assertRefused(auth.KIND_REGISTRY)
        self.assertIn("parse_error", failure.message)

    def test_C605_missing_registry_file_is_registry_refusal(self):
        os.unlink(self.paths.registry)
        self.assertRefused(auth.KIND_REGISTRY)

    def test_C605_verifier_missing_refuses_enforce(self):
        with self.assertRaises(auth.StartupRefused) as caught:
            self.open(sshkeygen_path=os.path.join(self.dir, "no-such-ssh-keygen"))
        # 建構子只驗 spool 目錄；缺 binary 由自測發現（兩者都是 78 的分類）。
        self.assertIn(caught.exception.kind,
                      (auth.KIND_VERIFIER_BUILD, auth.KIND_VERIFIER_SELFTEST))
        self.assertIn("ssh-keygen", caught.exception.message)

    def test_C605_openssh_95_fails_selftest(self):
        bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(bin_dir)
        shutil.copy("/usr/bin/ssh-keygen", os.path.join(bin_dir, "ssh-keygen"))
        os.chmod(os.path.join(bin_dir, "ssh-keygen"), 0o755)
        with open(os.path.join(bin_dir, "ssh"), "w", encoding="ascii") as handle:
            handle.write("#!/bin/sh\necho OpenSSH_9.5p1 >&2\n")
        os.chmod(os.path.join(bin_dir, "ssh"), 0o755)
        with self.assertRaises(auth.StartupRefused) as caught:
            self.open(owner_uid=os.getuid(),
                      sshkeygen_path=os.path.join(bin_dir, "ssh-keygen"))
        self.assertEqual(caught.exception.kind, auth.KIND_VERIFIER_SELFTEST)

    def test_C605_lock_unavailable_refuses_enforce(self):
        os.chmod(self.state_dir, 0o555)
        self.addCleanup(os.chmod, self.state_dir, 0o755)
        with self.assertRaises(auth.StartupRefused) as caught:
            self.open()
        self.assertEqual(caught.exception.kind, auth.KIND_LOCK_UNAVAILABLE)

    def test_C605_off_must_not_call_open_runtime(self):
        """W-16（6a 的部分）：off 不建 runtime（子行程層由 6b 驗）。"""
        with self.assertRaises(ValueError):
            auth.open_runtime(auth.AuthConfig(mode="off", state_dir=self.state_dir,
                                              registry_path=self.paths.registry),
                              self.clock)
        self.assertFalse(os.path.exists(self.paths.store_lock),
                         msg="off 不得開鎖檔")

    def test_C605_startup_refusal_releases_the_lock(self):
        self.write_registry(b"{ not json")
        self.assertRefused(auth.KIND_REGISTRY)
        from lgwd.auth_db import acquire_store_lock
        handle = acquire_store_lock(self.paths.store_lock)
        handle.release()  # 沒被自己留下的 flock 擋住


class StartupWarnTests(RuntimeCase):
    """C-605／J-6：warn 在故障下降級、照舊啟動，而且請求路徑不重開庫。"""

    def test_C605_lock_unavailable_degrades(self):
        os.chmod(self.state_dir, 0o555)
        self.addCleanup(os.chmod, self.state_dir, 0o755)
        runtime = self.open(mode="warn")
        self.assertIn("stores", runtime.degraded)
        self.assertIsNone(runtime.gate.snapshot()[1])
        verdict = self.auth(self.headers())
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_UNAVAILABLE, auth.REASON_STORE_UNAVAILABLE))

    def test_C605_missing_stores_degrade_and_never_reopen(self):
        """W-10：warn 啟動時庫不可用，送 100 個請求，開庫次數為 0。"""
        os.unlink(self.paths.trust_db)
        os.unlink(self.paths.replay_db)
        runtime = self.open(mode="warn")
        self.assertIn("stores", runtime.degraded)
        self.assertIn("registry", runtime.degraded)
        boom = mock.Mock(side_effect=AssertionError("請求路徑不得開庫"))
        with mock.patch("lgwd.auth_trust.open_rw", boom), \
                mock.patch("lgwd.auth_replay.open_rw", boom):
            for index in range(100):
                verdict = self.auth(self.headers(nonce_value=b64url(bytes([index % 256]) * 32)))
                self.assertEqual(
                    (verdict.outcome, verdict.reason),
                    (auth.OUTCOME_UNAVAILABLE, auth.REASON_STORE_UNAVAILABLE))
        self.assertFalse(boom.called, "warn 降級後不得在請求路徑重開庫")
        # warn 的業務結果不變（apply 一律 continue）。
        self.assertEqual(auth.apply("warn", verdict).kind, auth.ACTION_CONTINUE)

    def test_C605_registry_invalid_degrades(self):
        self.write_registry(b"{ not json")
        runtime = self.open(mode="warn")
        self.assertIn("registry", runtime.degraded)
        verdict = self.auth(self.headers())
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_UNAVAILABLE, auth.REASON_REGISTRY_UNAVAILABLE))

    def test_C605_verifier_missing_degrades_and_requests_are_unavailable(self):
        runtime = self.open(
            mode="warn", sshkeygen_path=os.path.join(self.dir, "no-such-ssh-keygen"))
        self.assertIn("verifier", runtime.degraded)
        verdict = self.auth(self.headers())
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_UNAVAILABLE, auth.REASON_VERIFIER_UNAVAILABLE))

    def test_C605_openssh_95_degrades_but_business_continues(self):
        bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(bin_dir)
        shutil.copy("/usr/bin/ssh-keygen", os.path.join(bin_dir, "ssh-keygen"))
        os.chmod(os.path.join(bin_dir, "ssh-keygen"), 0o755)
        with open(os.path.join(bin_dir, "ssh"), "w", encoding="ascii") as handle:
            handle.write("#!/bin/sh\necho OpenSSH_9.5p1 >&2\n")
        os.chmod(os.path.join(bin_dir, "ssh"), 0o755)
        runtime = self.open(
            mode="warn", owner_uid=os.getuid(),
            sshkeygen_path=os.path.join(bin_dir, "ssh-keygen"))
        self.assertIn("verifier", runtime.degraded)
        verdict = self.auth(self.headers())
        self.assertEqual(verdict.outcome, auth.OUTCOME_UNAVAILABLE)
        self.assertEqual(auth.apply("warn", verdict).kind, auth.ACTION_CONTINUE)


class StartupOrderTests(RuntimeCase):
    """C-605／§5 決定 2：先建 verifier 並自測，再 load_initial（兩者都在 bind 之前）。"""

    def test_C605_selftest_runs_before_the_first_snapshot(self):
        order = []
        real_selftest = sshsig.Verifier.selftest
        real_write = sshsig.write_allowed_signers

        def selftest(self):
            order.append("selftest")
            return real_selftest(self)

        def write(*args, **kwargs):
            order.append("write_allowed_signers")
            return real_write(*args, **kwargs)

        with mock.patch.object(sshsig.Verifier, "selftest", selftest), \
                mock.patch.object(sshsig, "write_allowed_signers", write):
            runtime = self.open()
        self.assertEqual(order[:2], ["selftest", "write_allowed_signers"])
        self.assertTrue(runtime.gate.snapshot()[2])


class SpoolTests(RuntimeCase):
    """W-6：spool 是 `sshsig_dir`（不是 `state_dir`），`clean_spool` 只在啟動一次。"""

    def test_W6_spool_path_and_single_clean(self):
        runtime = self.open()
        verifier = runtime.verifier
        self.assertEqual(verifier.spool_dir, self.paths.sshsig_dir)
        self.assertEqual(verifier.spool_dir, os.path.join(self.state_dir, "sshsig"))
        self.assertNotEqual(verifier.spool_dir, self.state_dir)
        self.assertEqual(runtime.clean_spool_calls, 1)
        for _ in range(3):
            runtime._extra_tick()
        self.assertEqual(runtime.clean_spool_calls, 1, msg="自測不得清 spool")


# ---------------------------------------------------------------------------
# C-610：窗口（W-1～W-20）
# ---------------------------------------------------------------------------

class GenerationWindowTests(RuntimeCase):
    """W-1～W-4、W-17：換世代與 allowed_signers 的窗口。"""

    def _broken_case(self, where: str) -> None:
        runtime = self.open()
        gate = runtime.gate
        real_write = sshsig.write_allowed_signers

        def broken(*args, **kwargs):
            if where == "before_write":
                raise RuntimeError("inject")
            result = real_write(*args, **kwargs)
            raise RuntimeError("inject")   # during_write / before_publish 同形

        self.write_registry(self.registry_data(
            2, [key_entry("k-a", self.signer, status="revoked")]))
        with mock.patch.object(sshsig, "write_allowed_signers", broken):
            runtime.watcher.tick()      # 名冊已提交，回呼失敗
        verdict = self.auth(self.headers())
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_UNAVAILABLE, auth.REASON_VERIFIER_UNAVAILABLE),
                         msg="被撤銷的 key 從不通過")
        self.assertEqual(auth.apply("enforce", verdict).status, 503)
        self.assertFalse(gate.snapshot()[2])
        # extra_ticks 的重試成功後恢復（退避 5 秒）。
        with mock.patch.object(auth, "_monotonic",
                               lambda: time.monotonic() + 10 ** 6):
            runtime._extra_tick()
        self.assertTrue(gate.snapshot()[2])
        verdict = self.auth(self.headers())
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_DENY, auth.REASON_REVOKED))

    def test_W4_callback_fails_before_writing(self):
        self._broken_case("before_write")

    def test_W4_callback_fails_during_writing(self):
        self._broken_case("during_write")

    def test_W4_callback_fails_before_publishing(self):
        self._broken_case("before_publish")

    def test_W1_inflight_request_keeps_its_signers(self):
        runtime = self.open()
        gate = runtime.gate
        blocking = BlockingVerifier()
        gate.verifier = blocking
        old = gate.snapshot()[1]
        headers = self.headers()
        result = {}

        def request():
            result["verdict"] = self.auth(headers, gate=gate)

        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(blocking.entered.wait(timeout=5))
        revoked = parse_registry(self.registry_data(
            2, [key_entry("k-a", self.signer, status="revoked")]))
        runtime._on_snapshot(revoked)          # 換世代並 retire 舊檔
        self.assertTrue(os.path.exists(old.path), msg="在途引用未歸零前不得刪檔")
        blocking.release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["verdict"].outcome, auth.OUTCOME_PASS, msg="結論照舊")
        self.assertFalse(os.path.exists(old.path), msg="release 之後才刪")

    def test_W2_publish_window_has_no_fake_503_and_no_watcher_reads(self):
        runtime = self.open()
        watcher = SpyWatcher(runtime.watcher)
        runtime.watcher = watcher
        gate = runtime.gate
        entered = threading.Event()
        release = threading.Event()
        real_write = sshsig.write_allowed_signers

        def slow_write(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return real_write(*args, **kwargs)

        self.write_registry(self.registry_data(
            2, [key_entry("k-a", self.signer), key_entry("k-b", self.signer_b)]))
        with mock.patch.object(sshsig, "write_allowed_signers", slow_write):
            ticker = threading.Thread(target=runtime.watcher.tick)
            ticker.start()
            self.assertTrue(entered.wait(timeout=5))
            old_key = self.auth(self.headers())
            new_key = self.auth(self.headers(key_id="k-b", signer=self.signer_b))
            release.set()
            ticker.join(timeout=10)
        self.assertEqual(old_key.outcome, auth.OUTCOME_PASS)
        self.assertEqual((new_key.outcome, new_key.reason),
                         (auth.OUTCOME_DENY, auth.REASON_UNKNOWN_KEY))
        self.assertEqual(watcher.reads, 0, msg="請求路徑不得讀 watcher.snapshot")
        # 換世代完成之後，新 key 才通過（零 503）。
        self.assertEqual(self.auth(self.headers(key_id="k-b", signer=self.signer_b)).outcome,
                         auth.OUTCOME_PASS)

    def test_W3_status_before_effect(self):
        runtime = self.open()
        gate = runtime.gate
        blocking = BlockingVerifier()
        gate.verifier = blocking
        real_write = sshsig.write_allowed_signers
        entered = threading.Event()
        release = threading.Event()

        def slow_write(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return real_write(*args, **kwargs)

        self.write_registry(self.registry_data(
            2, [key_entry("k-a", self.signer, status="revoked")]))
        with mock.patch.object(sshsig, "write_allowed_signers", slow_write):
            ticker = threading.Thread(target=runtime.watcher.tick)
            ticker.start()
            self.assertTrue(entered.wait(timeout=5))
            self.assertEqual(self.status()["loaded_generation"], 1,
                             msg="回呼完成前 status 不得宣告新世代")
            release.set()
            ticker.join(timeout=10)
        self.assertEqual(self.status()["loaded_generation"], 2)
        # status 一顯示新世代，剛撤銷的 key 必為 401（enforce）。
        blocking.release.set()
        verdict = self.auth(self.headers())
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_DENY, auth.REASON_REVOKED))

    def test_W17_failed_write_keeps_the_old_signers_until_next_generation(self):
        runtime = self.open()
        gate = runtime.gate
        old = gate.snapshot()[1]
        real_write = sshsig.write_allowed_signers
        with mock.patch.object(sshsig, "write_allowed_signers",
                               side_effect=RuntimeError("inject")):
            runtime._on_snapshot(parse_registry(self.registry_data(2)))
        self.assertIs(gate.snapshot()[1], old, msg="舊的一組仍在 state")
        self.assertFalse(old._retired, msg="失敗時不得 retire 舊檔")
        self.assertTrue(os.path.exists(old.path))
        runtime._on_snapshot(parse_registry(self.registry_data(
            3, [key_entry("k-a", self.signer), key_entry("k-b", self.signer_b)])))
        self.assertTrue(old._retired)
        self.assertFalse(os.path.exists(old.path))
        self.assertEqual(real_write, sshsig.write_allowed_signers)


class G1RetryBackoffTests(RuntimeCase):
    """G1（agy 審查）：allowed_signers 寫檔失敗後的退避（5→300 秒）與到期才重試。"""

    def test_G1_retries_wait_for_the_backoff_and_resume_after_expiry(self):
        runtime = self.open()
        gate = runtime.gate
        base_writes = runtime.signers_writes
        now = [1000.0]
        attempts = []

        def failing(*args, **kwargs):
            attempts.append(now[0])
            raise RuntimeError("inject")

        registry2 = parse_registry(self.registry_data(2, [key_entry("k-a", self.signer)]))
        with mock.patch.object(auth, "_monotonic", lambda: now[0]):
            with mock.patch.object(sshsig, "write_allowed_signers", failing):
                runtime._on_snapshot(registry2)
                self.assertFalse(gate.snapshot()[2])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(gate.pending()[2],
                                 now[0] + auth.SIGNERS_BACKOFF_INITIAL)
                # 退避期內多次重試都不寫檔、也不恢復（5 秒內：1001～1004）
                for _ in range(4):
                    now[0] += 1.0
                    runtime._retry_signers()
                self.assertEqual(len(attempts), 1, msg="退避期內不得重試")
                self.assertEqual(runtime.signers_writes, base_writes)
                self.assertFalse(gate.snapshot()[2])
                # 到期才重試；再失敗一次 → 退避加倍（5 → 10）
                now[0] = gate.pending()[2]
                runtime._retry_signers()
                self.assertEqual(len(attempts), 2)
                self.assertEqual(gate.pending()[2], now[0] + 10.0)
                # 逐次加倍並封頂 300 秒
                for expected in (20.0, 40.0, 80.0, 160.0, 300.0, 300.0):
                    now[0] = gate.pending()[2]
                    runtime._retry_signers()
                    self.assertEqual(gate.pending()[2], now[0] + expected)
            # 檔案寫得回去之後，到期的那一次重試就恢復
            now[0] = gate.pending()[2]
            runtime._retry_signers()
        self.assertEqual(runtime.signers_writes, base_writes + 1)
        self.assertTrue(gate.snapshot()[2])


class ClockWindowTests(RuntimeCase):
    """W-7、W-8：自測與在途 verify 並行；交易內的 now 為準。"""

    def test_W7_selftest_during_inflight_verify_does_not_change_the_result(self):
        runtime = self.open()
        gate = runtime.gate
        blocking = BlockingVerifier()
        gate.verifier = blocking
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("verdict", self.auth(self.headers())))
        thread.start()
        self.assertTrue(blocking.entered.wait(timeout=5))
        runtime._extra_tick()      # recheck_if_changed → 自測/檢查
        blocking.release.set()
        thread.join(timeout=10)
        self.assertEqual(result["verdict"].outcome, auth.OUTCOME_PASS)

    def test_W8_transaction_uses_its_own_now(self):
        runtime = self.open()
        gate = runtime.gate
        verifier = FakeVerifier()

        def advance(_verifier):
            self.clock.advance(seconds=61)

        verifier.before_verify = advance
        gate.verifier = verifier
        verdict = self.auth(self.headers())
        # 粗篩時 ts 還在 ±60 內；交易內的 now 已 +61 → 以交易內為準（expired）。
        self.assertEqual((verdict.outcome, verdict.reason),
                         (auth.OUTCOME_DENY, auth.REASON_EXPIRED))


class StopAndRestartTests(RuntimeCase):
    """W-5、W-13、W-18、W-19；停止順序與鎖的釋放。"""

    def test_W5_heartbeat_keeps_beating_with_a_broken_registry_and_stops(self):
        runtime = self.open(poll_interval=0.02)
        runtime.start()
        self.write_registry(b"{ not json")
        deadline = time.monotonic() + 10
        first = None
        while time.monotonic() < deadline:
            try:
                document = self.status()
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            if document["result"] == "parse_error":
                first = document
                break
            time.sleep(0.02)
        self.assertIsNotNone(first, msg="壞名冊的 parse_error 一直沒寫進 status")
        self.clock.advance(seconds=120)
        deadline = time.monotonic() + 10
        document = first
        while time.monotonic() < deadline:
            document = self.status()
            if document["heartbeat"] != first["heartbeat"]:
                break
            time.sleep(0.02)
        else:
            self.fail("壞名冊留著時心跳沒有前進")
        self.assertEqual(document["result"], "parse_error")
        runtime.stop()
        with open(self.paths.status, "rb") as handle:
            before = handle.read()
        self.clock.advance(seconds=600)
        time.sleep(0.2)
        with open(self.paths.status, "rb") as handle:
            self.assertEqual(handle.read(), before, msg="stop 之後心跳不得再前進")

    def test_W13_status_write_failure_does_not_change_the_request_result(self):
        with mock.patch("lgwd.auth_reload._atomic_write", side_effect=OSError("inject")):
            runtime = self.open()
        self.assertEqual(self.auth(self.headers()).outcome, auth.OUTCOME_PASS)

    def test_W18_stop_with_inflight_request_and_lock_released_last(self):
        from lgwd.auth_db import LockBusy, acquire_store_lock

        runtime = self.open()
        gate = runtime.gate
        blocking = BlockingVerifier()
        gate.verifier = blocking
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("verdict", self.auth(self.headers())))
        thread.start()
        self.assertTrue(blocking.entered.wait(timeout=5))
        with self.assertRaises(LockBusy):        # 還在跑：第二個拿不到鎖
            acquire_store_lock(self.paths.store_lock)
        runtime.stop()
        blocking.release.set()
        thread.join(timeout=10)
        verdict = result["verdict"]
        self.assertEqual(verdict.outcome, auth.OUTCOME_UNAVAILABLE,
                         msg="停止中的在途請求得到 503 類的結論，不是 PASS")
        third = acquire_store_lock(self.paths.store_lock)
        third.release()

    def test_W19_warn_pass_inserts_nonce_so_enforce_sees_a_replay(self):
        warn = self.open(mode="warn")
        nonce_value = nonce(11)
        headers = self.headers(nonce_value=nonce_value)
        first = self.auth(headers, gate=warn.gate)
        self.assertEqual(first.outcome, auth.OUTCOME_PASS)
        self.assertEqual(auth.apply("warn", first).kind, auth.ACTION_CONTINUE)
        warn.stop()
        enforce = self.open(mode="enforce")
        second = self.auth(headers, gate=enforce.gate)
        self.assertEqual((second.outcome, second.reason),
                         (auth.OUTCOME_DENY, auth.REASON_REPLAY))


class WaterEventTests(RuntimeCase):
    """C-605：`H[key]` 在未來的 key 只記事件、不拒絕啟動。"""

    def test_keys_with_future_water_only_log_an_event(self):
        connection = sqlite3.connect(self.paths.replay_db)
        try:
            connection.execute("INSERT INTO water (key_id, h) VALUES ('k-a', ?)",
                               (now_seconds(self.clock) + 3600,))
            connection.commit()
        finally:
            connection.close()
        runtime = self.open()
        self.assertEqual(self.auth(self.headers()).outcome, auth.OUTCOME_DENY)
        self.assertIsNotNone(runtime.gate)


# ---------------------------------------------------------------------------
# C-611：30 秒短版持續率
# ---------------------------------------------------------------------------

def _load1() -> float | None:
    """目前 1 分鐘平均 load（load1）；讀不到（非 POSIX）回 None。"""
    try:
        return float(os.getloadavg()[0])
    except OSError:  # pragma: no cover - 非 POSIX
        return None


def _should_skip_time_branch(load_before: float | None, load_after: float | None,
                             cores: int | None) -> tuple[bool, str]:
    """時間支該不該 skip（純函式，不碰機器也不讀 load）：回 (skip, 說明)。

    skip 只有兩種情況，都是「機器在跑的期間被**外部**共載到量不出設計值」：

    1. 負載**開始之前**就過載：load_before > 2×核數。
    2. 跑完後過載，而且開始前機器就不是閒置的：load_after > 2×核數 且 load_before > 核數。
       開始前就超過核數代表機器本來就有別人在跑，跑完後再被推過 2×核數就視為外部共載。

    閒置的機器（load_before ≤ 核數）不得因為跑完後 load_after 被這一支自己的 36 條執行緒
    推高而 skip：例如 2 核、開始前 0.1、跑完 14.0 仍要照常量斷言，否則小核 CI 的時間判準
    會被無聲略過（#305）。

    為什麼不扣「自身貢獻」（load_after − own_threads×(1−e^(−duration/60))）：那個估計假設
    執行緒一直在跑、而且只算這一支，實際上 32 條 consumer 大半在等鎖／等 I/O，估計不準；
    在長期共載的機器上（例如本機 16 核、load1 約 30）扣完會讓 #263 那種邊緣失敗
    （p99=1.07s）重新照常量斷言，把 flaky 引回來（PM 2026-11-25 裁定不採該公式）。

    已知限制：開始前 load 介於核數與 2×核數之間的機器，自己的負載把 load_after 推過
    2×核數 時仍會 skip。這只會多 skip、不會多失敗，可接受。

    口徑與 PR-3 `tests/test_auth_replay.py` 的 `_oversubscribed` 同源（開始前的
    load1 > 2×核數），差別是這裡多看跑完後的 load。核數或 load 讀不到（None）時不 skip，
    照常量斷言；判準數值（rate≥50/s、p99<1s、30 秒、32 並發）不在這裡。
    """
    if cores is None or cores < 1 or load_before is None or load_after is None:
        return False, "核數或 load1 讀不到，照常量斷言（不 skip）"
    limit = 2.0 * cores
    detail = ("開始前 load1={0:.2f}、跑完 {1:.2f}；核數={2}、門檻 2×核數={3:.0f}".format(
        load_before, load_after, cores, limit))
    if load_before > limit:
        return True, "開始前就共載：" + detail
    if load_after > limit and load_before > cores:
        return True, "跑的期間有外部共載（跑完後過載且開始前非閒置）：" + detail
    return False, detail


class TimeBranchSkipRuleTests(unittest.TestCase):
    """C-611 時間支 skip 口徑的表格測試（純函式；不跑 30 秒負載、不讀機器）。"""

    def decide(self, load_before, load_after, cores):
        return _should_skip_time_branch(load_before, load_after, cores)

    def test_idle_small_machine_does_not_skip_itself(self):
        """閒置 2 核（門檻 4）、開始前 0.1、跑完 14.0 幾乎全是自己的執行緒：不得自我 skip。"""
        skip, detail = self.decide(0.1, 14.0, 2)
        self.assertFalse(skip, detail)

    def test_small_machine_not_idle_before_skips_when_pushed_over(self):
        """同型 2 核但開始前 3.0（> 核數、非閒置）、跑完 14.0（> 4）：視為外部共載而 skip。"""
        skip, detail = self.decide(3.0, 14.0, 2)
        self.assertTrue(skip, detail)
        self.assertIn("外部共載", detail)

    def test_idle_large_machine_does_not_skip(self):
        """閒置 16 核：開始前 0.1、跑完 14.0 低於門檻 32，不 skip。"""
        skip, detail = self.decide(0.1, 14.0, 16)
        self.assertFalse(skip, detail)

    def test_oversubscribed_before_skips(self):
        """開始前就過載（16 核、門檻 32、開始前 33）：跑完的 load 不影響這個判斷（這裡故意
        給低於門檻的 30），仍然 skip。"""
        skip, detail = self.decide(33.0, 30.0, 16)
        self.assertTrue(skip, detail)
        self.assertIn("開始前", detail)

    def test_263_case_skips(self):
        """#263 實跑（16 核、開始前 29.7、跑完 36.7）：before > 16 且 after > 32 → skip。"""
        skip, detail = self.decide(29.7, 36.7, 16)
        self.assertTrue(skip, detail)
        self.assertIn("外部共載", detail)

    def test_busy_before_but_not_over_after_does_not_skip(self):
        """16 核、開始前 20（> 核數）、跑完 20（≤ 32）：沒有過載，不 skip。"""
        skip, detail = self.decide(20.0, 20.0, 16)
        self.assertFalse(skip, detail)

    def test_pushed_over_after_with_busy_before_skips(self):
        """16 核、開始前 17（> 核數）、跑完 33（> 32）→ skip。"""
        skip, detail = self.decide(17.0, 33.0, 16)
        self.assertTrue(skip, detail)

    def test_threshold_boundaries_are_strict(self):
        """門檻是嚴格大於：剛好等於核數／2×核數都不算超過。"""
        cores = 4
        limit = 2.0 * cores
        # 開始前剛好等於 2×核數：不算過載；多一點才 skip。
        self.assertFalse(self.decide(limit, limit, cores)[0])
        self.assertTrue(self.decide(limit + 0.01, limit, cores)[0])
        # 開始前剛好等於核數：不算「非閒置」，跑完再高也不 skip；多一點才 skip。
        self.assertFalse(self.decide(float(cores), 100.0, cores)[0])
        self.assertTrue(self.decide(cores + 0.01, limit + 0.01, cores)[0])
        # 跑完剛好等於 2×核數：不算過載；多一點才 skip。
        self.assertFalse(self.decide(cores + 0.01, limit, cores)[0])
        self.assertTrue(self.decide(cores + 0.01, limit + 0.01, cores)[0])

    def test_unreadable_load_or_cores_does_not_skip(self):
        """核數或 load 讀不到：不 skip，照常量斷言。"""
        for load_before, load_after, cores in (
                (None, 99.0, 16), (0.1, None, 16), (0.1, 99.0, None),
                (0.1, 99.0, 0)):
            skip, detail = self.decide(load_before, load_after, cores)
            self.assertFalse(skip, "{0!r}/{1!r}/{2!r}: {3}".format(
                load_before, load_after, cores, detail))


class RateTests(unittest.TestCase):
    """C-611：並發 32、完整 HTTP＋`prlimit`＋`ssh-keygen`＋SQLite 路徑、30 秒。

    拆成兩支（慣例同 PR-3 中-3-3），共用同一次量測（`_run_sample`）：

    - 功能支（**永遠執行**，不受機器負載影響）：零例外、零假 503／非 200、每一筆都
      ACCEPTED（`200`）且都寫進 replay 庫（列數＝接受數）。
    - 時間支（外部共載時 skip）：接受率 ≥ 50 req/s、等鎖 p99 < 1 秒；只在機器沒有被
      **外部**共載時斷言——開始前 load1 就 > 2×核數，或跑完後 load1 > 2×核數而且開始前
      就 > 核數（口徑與表格見 `_should_skip_time_branch` 與 `TimeBranchSkipRuleTests`）。
      閒置的機器不會因為跑完後被這一支自己推高而 skip。共載時 `skipTest`，訊息帶量到的
      rate／p99／load1／核數。功能判準在另一支，不會跟著 skip。

    量到的數字一律印出來供報告引用。驗章期限用預設 2 秒、商店 `busy_timeout` 用預設
    5 秒（不放寬，也不拿放寬當時間判準的依據）。簽章由 4 條 producer 執行緒在視窗內
    即時產生（32 條 consumer 消費），避免單一 producer 成為速率上限。
    """

    DURATION = 30.0
    CONCURRENCY = 32
    PRODUCERS = 4

    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory()
        cls.signer = Signer(cls._keys.name, "rate")
        cls._sample = None  # 30 秒量測結果（兩支共用）或量測時逸出的例外
        cls._sample_lock = threading.Lock()

    @classmethod
    def tearDownClass(cls):
        cls._keys.cleanup()

    def _run_sample(self) -> dict:
        """跑一次 30 秒負載並快取；功能與時間兩支共用，同一個 class 只量一次。"""
        with RateTests._sample_lock:
            if isinstance(RateTests._sample, BaseException):
                raise RateTests._sample
            if RateTests._sample is None:
                try:
                    RateTests._sample = self._measure()
                except BaseException as exc:  # 另一支也看到同一個失敗，不重跑第二次
                    RateTests._sample = exc
                    raise
            return RateTests._sample

    def _measure(self) -> dict:
        import socket

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = os.path.join(tmp.name, "state")
        os.makedirs(state_dir)
        paths = auth_paths(state_dir, registry=os.path.join(tmp.name, "registry.json"))
        create_stores(paths.trust_db, paths.replay_db)
        write_registry_file(
            paths.registry, registry_bytes(1, [key_entry("rate-key", self.signer)]))
        runtime = auth.open_runtime(
            auth.AuthConfig(
                mode="enforce", registry_path=paths.registry, state_dir=state_dir,
                per_key_max=10 ** 6, verify_concurrency=16, verify_timeout=2.0,
                poll_interval=0.0, collect_lock_waits=True),
            SystemClock(), owner_uid=0, registry_owner_uid=os.getuid())
        self.addCleanup(runtime.stop)

        service = FakeService()
        server = make_api_server(service, HOST, 0, gate=runtime.gate)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        port = server.server_address[1]

        stop = threading.Event()
        queue: list = []
        ready = threading.Condition()
        produced = [0]
        results: list[int] = []
        errors: list[str] = []
        lock = threading.Lock()

        def producer():
            while not stop.is_set():
                try:
                    headers = self.signer.headers(
                        key_id="rate-key", method="GET", target=TARGET)
                except Exception as exc:  # 簽章端失敗也算失敗
                    with lock:
                        errors.append("sign:" + type(exc).__name__)
                    return
                raw = (
                    "GET {0} HTTP/1.0\r\n".format(TARGET)
                    + "".join("{0}: {1}\r\n".format(name, value)
                              for name, value in headers)
                    + "\r\n"
                ).encode("latin-1")
                with ready:
                    while not stop.is_set() and len(queue) >= 256:
                        ready.wait(timeout=0.1)
                    if stop.is_set():
                        return
                    queue.append(raw)
                    produced[0] += 1
                    ready.notify()

        def consumer():
            while True:
                with ready:
                    while not queue and not stop.is_set():
                        ready.wait(timeout=0.1)
                    if not queue:
                        if stop.is_set():
                            return
                        continue
                    raw = queue.pop()
                try:
                    with socket.create_connection((HOST, port), timeout=10) as sock:
                        sock.sendall(raw)
                        chunks = []
                        while True:
                            chunk = sock.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                    response = b"".join(chunks)
                    status = int(response.split(b" ", 2)[1])
                except Exception as exc:
                    with lock:
                        errors.append(type(exc).__name__)
                    return
                with lock:
                    results.append(status)

        producers = [threading.Thread(target=producer, daemon=True)
                     for _ in range(self.PRODUCERS)]
        # 共載判準取樣在啟動負載之前（開始前就過載＝共載）；跑完再取一次，跑完後過載而且
        # 開始前機器就不是閒置的，才視為跑的期間有外部共載（口徑見 _should_skip_time_branch）。
        load = _load1()
        cores = os.cpu_count()
        for worker in producers:
            worker.start()
        consumers = [threading.Thread(target=consumer, daemon=True)
                     for _ in range(self.CONCURRENCY)]
        for worker in consumers:
            worker.start()
        started = time.monotonic()
        time.sleep(self.DURATION)
        elapsed = time.monotonic() - started
        stop.set()
        with ready:
            ready.notify_all()
        for worker in producers + consumers:
            worker.join(timeout=30)
        waits = list(runtime.replay.db.lock_waits or ())
        rows = runtime.replay.row_count()
        runtime.stop()
        self._stop(server, thread)

        with lock:
            snapshot = list(results)
            failures = list(errors)
        load_after = _load1()
        rate = len(snapshot) / elapsed
        ordered = sorted(waits)
        p99 = ordered[int(len(ordered) * 0.99) - 1] if ordered else 0.0
        skip, skip_detail = _should_skip_time_branch(load, load_after, cores)
        print("C-611 elapsed={0:.1f}s accepted={1} rows={2} rate={3:.1f}/s "
              "p99_lock={4:.4f}s loadavg={5} (after {6}) cores={7} "
              "time_branch={8} ({9})".format(
                  elapsed, len(snapshot), rows, rate, p99,
                  "n/a" if load is None else "{0:.2f}".format(load),
                  "n/a" if load_after is None else "{0:.2f}".format(load_after),
                  cores, "skip" if skip else "run", skip_detail))
        return {
            "elapsed": elapsed,
            "statuses": snapshot,
            "rows": rows,
            "failures": failures,
            "produced": produced[0],
            "waits": ordered,
            "rate": rate,
            "p99": p99,
            "load": load,
            "load_after": load_after,
            "cores": cores,
            "skip": skip,
            "skip_detail": skip_detail,
        }

    def test_C611_throughput_functional_always_accepted(self):
        """C-611 功能（**永遠執行**，不受機器負載影響）：零例外、零假 503／非 200、
        每一筆都 ACCEPTED（`200`）且都寫進 replay 庫（列數＝接受數）。"""
        sample = self._run_sample()
        self.assertEqual(sample["failures"][:5], [], msg="不得有例外")
        self.assertTrue(sample["statuses"], "30 秒內沒有任何回應")
        bad = [status for status in sample["statuses"] if status != 200]
        self.assertEqual(bad[:5], [], msg="不得有假 503 或其他非 200")
        self.assertEqual(sample["rows"], len(sample["statuses"]),
                         msg="每筆 ACCEPTED 都應寫進 replay 庫（列數＝接受數）")
        self.assertGreaterEqual(sample["produced"], len(sample["statuses"]))

    def test_C611_throughput_hit_rate_and_lock_p99(self):
        """C-611 時間：接受率 ≥ 50 req/s、等鎖 p99 < 1 秒（判準不放寬）。

        只在機器沒有被**外部**共載時斷言：開始前 load1 就 > 2×核數，或跑完後 load1 > 2×核數
        而且開始前就 > 核數（開始前機器本來就有別人在跑）；口徑與表格見
        `_should_skip_time_branch` 與 `TimeBranchSkipRuleTests`。閒置的機器不會因為跑完後
        被這一支自己推高而 skip。共載時 `skipTest`，訊息帶量到的 rate／p99／load1／核數；
        功能判準在另一支，不會因為這一支 skip 而消失。
        """
        sample = self._run_sample()
        detail = ("rate={0:.1f}/s p99={1:.4f}s 開始前 load1={2}（跑完 {3}）"
                  " cores={4}").format(
                      sample["rate"], sample["p99"], sample["load"],
                      sample["load_after"], sample["cores"])
        if sample["skip"]:
            self.skipTest(
                "機器共載：{0}；持續率與等鎖 p99 量到的是共用資源競爭不是設計值；"
                "判準（rate≥50/s、p99<1s）不變：{1}".format(
                    sample["skip_detail"], detail))
        self.assertTrue(sample["waits"], "沒有收到等鎖樣本")
        self.assertGreaterEqual(sample["rate"], 50.0,
                                msg="接受率低於 50 req/s：" + detail)
        self.assertLess(sample["p99"], 1.0, msg="等鎖 p99 必須低於 1 秒：" + detail)

    @staticmethod
    def _stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SpyWatcher:
    """計數 `watcher.snapshot` 的讀取次數（W-2）。其他屬性照原物件。"""

    def __init__(self, real):
        self._real = real
        self.reads = 0

    @property
    def snapshot(self):
        self.reads += 1
        return self._real.snapshot

    def __getattr__(self, name):
        return getattr(self._real, name)


if __name__ == "__main__":
    unittest.main()
