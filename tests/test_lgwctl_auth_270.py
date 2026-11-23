"""`llmgwctl auth-registry check|install|status` 與 `auth-replay status` 的測試。

#270／#222 D5b；契約 C-810～C-817（對應見每個測試的 docstring 與名稱）：

- C-810 離線 check（D2 的同一個驗證函式、fingerprint 單一來源、檔案權限只是警示）
- C-811 `check --against-trust`（root、`mode=ro`、世代水位／墓碑／同世代異雜湊、變更摘要）
- C-812 install 的原子寫入與權限、install 鎖、**不取 store flock**
- C-813 install 的載入確認（status 檔的五種結局、W-D1～W-D4、逾時、`--no-wait`）
- C-814 `auth-registry status`（唯讀、心跳 180 秒、attempted＝loaded、結束碼）
- C-815 `auth-replay status`（root、`mode=ro`、各 key 的 H、未來 H → 1、缺庫與權限 → 2）
- C-816 結束碼 0／1／2 與非 root 拒絕；status／replay 在 store 鎖被持有時仍可用
- C-817 輸出與審計不含公鑰；fingerprint 只由 `auth_registry.fingerprint` 產生

全部在 `tests/lgwctl_fakes.Sandbox` 的 prefix 沙箱裡跑：不碰真機、不讀憑證、
不呼叫真的 systemctl，也不動 `/var/lib/llmgateway`。root 身分以 patch
`os.geteuid` 模擬（chown 在 prefix 模式一律跳過）。
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import unittest
from unittest import mock

try:
    from .auth_fakes import (
        VALID_A,
        VALID_B,
        VALID_C,
        key,
        public_key_line,
        registry_bytes,
        write_registry_file,
    )
    from .lgwctl_fakes import Sandbox
except ImportError:  # python3 -m unittest discover -s tests
    from auth_fakes import (
        VALID_A,
        VALID_B,
        VALID_C,
        key,
        public_key_line,
        registry_bytes,
        write_registry_file,
    )
    from lgwctl_fakes import Sandbox

from lgwd.auth_db import LockBusy, acquire_store_lock, create_stores, open_rw
from lgwd.auth_paths import auth_paths
from lgwd.auth_registry import content_hash, fingerprint, parse_registry
from lgwd.auth_trust import TrustStore
from lgwd.clock import SystemClock
from lgwd.auth_paths import now_seconds
from lgwd.auth_admin import AuditLog
from lgwctl import auth_cli

HEARTBEAT_STALE = 180


# --- 共用夾具 ---------------------------------------------------------------


def sandbox_paths(box):
    return auth_paths(box.state(), registry=box.etc("auth-registry.json"))


def seed_stores(box, loaded: bytes | None = None, *, replay: bool = True):
    """建兩庫（0600）並可選地把 `loaded` 當成已載入快照提交進信任庫。"""
    paths = sandbox_paths(box)
    os.makedirs(box.state(), exist_ok=True)
    os.makedirs(box.etc(), exist_ok=True)
    create_stores(paths.trust_db, paths.replay_db)
    if loaded is not None:
        store = TrustStore(paths.trust_db)
        try:
            registry = parse_registry(loaded)
            store.commit(registry, loaded, content_hash(loaded))
        finally:
            store.close()
    return paths


def candidate_file(box, data: bytes, name: str = "candidate.json") -> str:
    path = os.path.join(box.root, name)
    write_registry_file(path, data)
    return path


def status_document(**over):
    digest = "sha256:" + "0" * 64
    document = {
        "schema": 1,
        "loaded_generation": 3,
        "loaded_hash": digest,
        "attempted_hash": digest,
        "result": "ok",
        "heartbeat": int(time.time()),
    }
    document.update(over)
    return document


def write_status(box, document, mode: int = 0o644) -> str:
    return box.write(sandbox_paths(box).status,
                     json.dumps(document, separators=(",", ":")), mode)


def run(box, argv, *, root: bool = False):
    """在行程內跑 llmgwctl；`root=True` 以 patch `os.geteuid` 假裝 root。"""
    env = dict(box.env)
    with mock.patch("os.geteuid", return_value=0 if root else 1000):
        return box.run_cli(list(argv), env=env)


def lock_is_free(path: str) -> bool:
    try:
        handle = acquire_store_lock(path)
    except LockBusy:
        return False
    handle.release()
    return True


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeWatcher(threading.Thread):
    """假的 lgwd 名冊 watcher：讀名冊檔並原子寫 status 檔（install 的窗口測這個）。

    `mode`：`ok`（照實回報）、`reject`（attempted＝目前檔案雜湊但結果固定錯）、
    `insecure`（`insecure_file` 且 attempted 為 null）、`delay`（前 `delay` 秒寫
    暫時性錯誤，之後照實回報；用來製造「install 正在等」的窗口）。
    """

    def __init__(self, paths, *, mode: str = "ok", interval: float = 0.05,
                 heartbeat_age: int = 0, delay: float = 0.0):
        super().__init__(daemon=True)
        self.paths = paths
        self.mode = mode
        self.interval = interval
        self.heartbeat_age = heartbeat_age
        self._release_at = time.monotonic() + delay
        self._stopped = threading.Event()
        self.error = None

    def stop(self) -> None:
        self._stopped.set()
        self.join(2)

    def run(self) -> None:
        try:
            while not self._stopped.is_set():
                self.write_once()
                self._stopped.wait(self.interval)
        except Exception as exc:  # pragma: no cover - 出錯時由測試斷言
            self.error = exc

    def write_once(self) -> None:
        heartbeat = int(time.time()) - self.heartbeat_age
        if self.mode == "insecure":
            document = {
                "schema": 1, "loaded_generation": None, "loaded_hash": None,
                "attempted_hash": None, "result": "insecure_file", "heartbeat": heartbeat,
            }
        elif self.mode == "delay" and time.monotonic() < self._release_at:
            document = {
                "schema": 1, "loaded_generation": None, "loaded_hash": None,
                "attempted_hash": None, "result": "io_error", "heartbeat": heartbeat,
            }
        else:
            try:
                with open(self.paths.registry, "rb") as handle:
                    raw = handle.read()
            except OSError:
                raw = None
            if raw is None:
                document = {
                    "schema": 1, "loaded_generation": None, "loaded_hash": None,
                    "attempted_hash": None, "result": "io_error", "heartbeat": heartbeat,
                }
            else:
                digest = content_hash(raw)
                result = "ok"
                generation = None
                try:
                    generation = parse_registry(raw).generation
                except Exception as exc:  # RegistryError 的分類
                    result = getattr(exc, "kind", "parse_error")
                if self.mode == "reject":
                    result = "tombstone_violation"
                document = {
                    "schema": 1,
                    "loaded_generation": generation if result == "ok" else None,
                    "loaded_hash": digest if result == "ok" else None,
                    "attempted_hash": digest,
                    "result": result,
                    "heartbeat": heartbeat,
                }
        _write_json_atomic(self.paths.status, document)


def _write_json_atomic(path: str, document: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def b64_of(raw32: bytes) -> str:
    return public_key_line(raw32).split()[1]


# --- C-810：離線 check ------------------------------------------------------


class CheckOfflineTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.raw = registry_bytes(3)
        self.path = candidate_file(self.box, self.raw)

    def test_C810_valid_candidate_reports_generation_hash_and_fingerprint(self):
        """C-810：合格名冊回 0，印世代、內容雜湊與每把 key 的 fingerprint。"""
        rc, out, err = run(self.box, ["auth-registry", "check", self.path])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("generation 3", out)
        self.assertIn(content_hash(self.raw), out)
        self.assertIn("fingerprint=SHA256:" + fingerprint(VALID_A), out)
        self.assertNotIn(b64_of(VALID_A), out)   # 輸出不含公鑰
        self.assertNotIn(b64_of(VALID_B), out)

    def test_C810_same_validation_function_rejects_bad_public_key(self):
        """C-810：公鑰不合格交回 D2 的 `invalid_key` 分類（不是另一份驗證）。"""
        bad = registry_bytes(3, keys=[key(raw=b"\x00" * 32)])
        path = candidate_file(self.box, bad, "bad-key.json")
        rc, out, err = run(self.box, ["auth-registry", "check", path])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("invalid_key", out)

    def test_C810_duplicate_and_parse_errors_are_exit_1(self):
        """C-810：同公鑰兩個 key ID 是 `duplicate`；壞 JSON 是 `parse_error`。"""
        duplicate = registry_bytes(3, keys=[key(), key("w1-tunnel-b", raw=VALID_A)])
        dup_path = candidate_file(self.box, duplicate, "dup.json")
        rc, out, _ = run(self.box, ["auth-registry", "check", dup_path])
        self.assertEqual(rc, 1)
        self.assertIn("duplicate", out)
        broken = candidate_file(self.box, b"{not json", "broken.json")
        rc, out, _ = run(self.box, ["auth-registry", "check", broken])
        self.assertEqual(rc, 1)
        self.assertIn("parse_error", out)

    def test_C810_missing_file_is_exit_2(self):
        """C-810：讀不到檔回 2（不是驗證失敗 1）。"""
        rc, out, err = run(self.box, ["auth-registry", "check",
                                      os.path.join(self.box.root, "nope.json")])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("讀不到", err)

    def test_C810_symlink_candidate_is_rejected(self):
        """C-810：symlink 一律 `insecure_file`（不是照著跟隨）。"""
        link = os.path.join(self.box.root, "link.json")
        os.symlink(self.path, link)
        rc, out, err = run(self.box, ["auth-registry", "check", link])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("insecure_file", out)

    def test_C810_insecure_permissions_are_only_a_warning(self):
        """C-810：候選檔在操作者暫存位置很常見，權限不符只警示、仍可驗。"""
        os.chmod(self.path, 0o666)
        rc, out, err = run(self.box, ["auth-registry", "check", self.path])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("warn", out)
        self.assertIn("generation 3", out)

    def test_C810_check_is_offline_and_creates_nothing(self):
        """C-810：check 不需要信任庫、不建任何狀態檔。"""
        before = sorted(os.listdir(self.box.state())) if os.path.isdir(self.box.state()) else []
        rc, out, err = run(self.box, ["auth-registry", "check", self.path])
        self.assertEqual(rc, 0, out + err)
        after = sorted(os.listdir(self.box.state())) if os.path.isdir(self.box.state()) else []
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(sandbox_paths(self.box).trust_db))


# --- C-811：--against-trust -------------------------------------------------


class CheckAgainstTrustTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.loaded = registry_bytes(5, keys=[key(), key("w1-tunnel-b", raw=VALID_B)])

    def test_C811_requires_root(self):
        """C-811：信任庫是 llmgw 0600，`--against-trust` 非 root 回 2。"""
        seed_stores(self.box, self.loaded)
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("root", err)

    def test_C811_valid_higher_generation_prints_the_diff(self):
        """C-811：較高世代且墓碑過關回 0，並印新增／撤銷／移除。"""
        seed_stores(self.box, self.loaded)
        proposed = registry_bytes(6, keys=[
            key(), key("w1-tunnel-b", raw=VALID_B, status="revoked"),
            key("w1-tunnel-c", raw=VALID_C),
        ])
        path = candidate_file(self.box, proposed)
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("loaded_generation 5", out)
        self.assertIn("added w1-tunnel-c", out)
        self.assertIn("revoked w1-tunnel-b", out)
        self.assertIn("removed （無）", out)

    def test_C811_same_generation_same_content_is_ok(self):
        """C-811：同世代同雜湊是重裝（不是 `generation_not_increased`）。"""
        seed_stores(self.box, self.loaded)
        path = candidate_file(self.box, self.loaded, "same.json")
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertEqual(rc, 0, out + err)

    def test_C811_generation_not_increased_is_exit_1(self):
        """C-811：同世代異雜湊與較低世代都回 1（`generation_not_increased`）。"""
        seed_stores(self.box, self.loaded)
        same_generation = registry_bytes(5, keys=[key(), key("w1-tunnel-c", raw=VALID_C)])
        path = candidate_file(self.box, same_generation, "same-gen.json")
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("generation_not_increased", out)
        lower = candidate_file(self.box, registry_bytes(4), "lower.json")
        rc, out, _ = run(self.box, ["auth-registry", "check", lower, "--against-trust"],
                         root=True)
        self.assertEqual(rc, 1)
        self.assertIn("generation_not_increased", out)

    def test_C811_tombstone_violation_is_exit_1(self):
        """C-811：已撤銷的 key 在候選裡改回有效＝`tombstone_violation`。"""
        revoked = registry_bytes(5, keys=[key(status="revoked")])
        seed_stores(self.box, revoked)
        path = candidate_file(self.box, registry_bytes(6, keys=[key()]), "active-again.json")
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)

    def test_C811_missing_trust_store_is_exit_1(self):
        """C-811：信任庫不存在時不能驗水位與墓碑，回 1 並指向 init／rebuild-trust。"""
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("rebuild-trust", out)

    def test_C811_trust_store_is_opened_read_only_without_new_files(self):
        """C-811／W-D10：乾淨的信任庫以唯讀開，不留下 -wal／-shm，也不建檔。"""
        paths = seed_stores(self.box, self.loaded)
        self.assertFalse(os.path.exists(paths.trust_db + "-wal"))
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(paths.trust_db + "-wal"))
        self.assertFalse(os.path.exists(paths.trust_db + "-shm"))


# --- C-812／C-813：install ------------------------------------------------


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.loaded = registry_bytes(5)
        self.paths = seed_stores(self.box, self.loaded)
        write_registry_file(self.paths.registry, self.loaded)

    def test_C812_non_root_refused_and_nothing_written(self):
        """C-812：install 需 root，非 root 回 2 且不寫名冊、不寫審計。"""
        candidate = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "install", candidate])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("root", err)
        with open(self.paths.registry, "rb") as handle:
            self.assertEqual(handle.read(), self.loaded)
        self.assertFalse(os.path.exists(self.paths.audit_log))

    def test_C812_installs_atomically_with_0640_and_no_temp_left(self):
        """C-812：原子安裝（同目錄暫存＋fsync＋rename）、0640、不留暫存檔。"""
        candidate = registry_bytes(6)
        path = candidate_file(self.box, candidate)
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        with open(self.paths.registry, "rb") as handle:
            self.assertEqual(handle.read(), candidate)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.registry).st_mode), 0o640)
        leftovers = [name for name in os.listdir(self.box.etc())
                     if name.startswith(".auth-registry.json.")]
        self.assertEqual(leftovers, [])

    def test_C812_installs_0640_even_when_the_candidate_is_world_writable(self):
        """C-812／W-D2：候選檔 0666 仍以 root:<服務群組> 0640 寫入（權限問題不會帶進安裝後的名冊）。"""
        path = candidate_file(self.box, registry_bytes(6))
        os.chmod(path, 0o666)
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.registry).st_mode), 0o640)

    def test_C812_install_does_not_take_the_store_flock(self):
        """C-812：install 不取 `auth-store.lock`（lgwd 執行中要能 install）。"""
        store_lock = acquire_store_lock(self.paths.store_lock)
        self.addCleanup(store_lock.release)
        path = candidate_file(self.box, registry_bytes(6))
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(lock_is_free(self.paths.store_lock))  # 仍是我們的鎖

    def test_C812_install_lock_busy_is_exit_2(self):
        """C-812／W-D3：另一個 install 持有 install 鎖時回 2。"""
        lock = acquire_store_lock(os.path.join(self.paths.state_dir,
                                               "auth-registry-install.lock"))
        self.addCleanup(lock.release)
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "install", path], root=True)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("正在進行", out)

    def test_C812_concurrent_installs_never_both_return_0(self):
        """C-812／W-D3：兩個 install 併發時只有一個成功，另一個回 2。"""
        path = candidate_file(self.box, registry_bytes(6))
        watcher = FakeWatcher(self.paths, mode="delay", interval=0.05, delay=0.6)
        watcher.start()
        self.addCleanup(watcher.stop)
        results = {}

        def first():
            results["first"] = run(
                self.box, ["auth-registry", "install", path, "--timeout", "5"], root=True)[0]

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(wait_until(
            lambda: not lock_is_free(os.path.join(self.paths.state_dir,
                                                  "auth-registry-install.lock"))))
        results["second"] = run(
            self.box, ["auth-registry", "install", path, "--timeout", "5"], root=True)[0]
        thread.join(10)
        self.assertEqual(sorted(results.values()), [0, 2], results)

    def test_C812_bad_candidate_is_exit_1_and_registry_unchanged(self):
        """C-812：壞檔 install 回 1，名冊檔完全不動。"""
        bad = candidate_file(self.box, b'{"schema": 1}', "bad.json")
        rc, out, err = run(self.box, ["auth-registry", "install", bad], root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("parse_error", out)
        with open(self.paths.registry, "rb") as handle:
            self.assertEqual(handle.read(), self.loaded)

    def test_C812_candidate_not_increased_is_not_installed(self):
        """C-812：預檢（同一個 check）不過就不寫。"""
        path = candidate_file(self.box, registry_bytes(4), "lower.json")
        rc, out, err = run(self.box, ["auth-registry", "install", path], root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("generation_not_increased", out)
        with open(self.paths.registry, "rb") as handle:
            self.assertEqual(handle.read(), self.loaded)

    def test_C812_writes_the_audit_line_first(self):
        """C-812／C-817／中-2：install 寫一筆 `AuditLog` 形狀的審計（一行 JSON、0600）。"""
        candidate = registry_bytes(6)
        path = candidate_file(self.box, candidate)
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.audit_log).st_mode), 0o600)
        with open(self.paths.audit_log, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        # 與 `auth_admin.AuditLog` 逐鍵相同（不是另一套扁平欄位）。
        self.assertEqual(
            set(entry), {"time", "uid", "sudo_user", "command", "args", "old", "new"})
        self.assertEqual(entry["command"], "auth-registry install")
        self.assertEqual(entry["args"], {"source": "file"})
        self.assertEqual(entry["new"], {"generation": 6, "snapshot_hash": content_hash(candidate)})
        self.assertEqual(
            entry["old"],
            {"generation": 5, "snapshot_hash": content_hash(self.loaded)},
        )
        self.assertNotIn(b64_of(VALID_A), lines[0])

    def test_C812_install_accepts_candidate_name_with_spaces_and_non_ascii(self):
        """低-1（archreview2）：審計不記檔名，含空白／非 ASCII 的合法名冊也裝得進去。

        修正前 `args={"file": basename}` 會被 `AuditLog` 的白名單擋下（AuditValueError，
        回 2 且未寫入），合法的名冊被意外拒絕。這裡同時驗審計行形狀不變。
        """
        candidate = registry_bytes(6)
        path = candidate_file(self.box, candidate, name="我的 名冊 candidate.json")
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--no-wait"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        with open(self.paths.registry, "rb") as handle:
            self.assertEqual(handle.read(), candidate)
        with open(self.paths.audit_log, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(
            set(entry), {"time", "uid", "sudo_user", "command", "args", "old", "new"})
        self.assertEqual(entry["command"], "auth-registry install")
        self.assertEqual(entry["args"], {"source": "file"})
        self.assertEqual(entry["new"], {"generation": 6, "snapshot_hash": content_hash(candidate)})

    # -- C-813：載入確認 ---------------------------------------------------

    def test_C813_same_generation_same_content_returns_0_immediately(self):
        """C-813：同世代同內容重裝，status 已是 ok → 第一次讀就回 0。"""
        path = candidate_file(self.box, self.loaded, "reinstall.json")
        digest = content_hash(self.loaded)
        write_status(self.box, status_document(
            loaded_generation=5, loaded_hash=digest, attempted_hash=digest))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "0.5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("已載入世代 5", out)

    def test_C813_loaded_matches_but_latest_attempt_is_another_hash_is_not_success(self):
        """C-813：只比 `loaded_hash` 不夠——最近一次嘗試是別份且失敗時不得回 0。"""
        proposed = registry_bytes(6)
        path = candidate_file(self.box, proposed)
        digest = content_hash(proposed)
        write_status(self.box, status_document(
            loaded_generation=6, loaded_hash=digest,
            attempted_hash="sha256:" + "2" * 64, result="ok"))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "0.3"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("不得宣告撤銷完成", out)

    def test_C813_waits_for_attempted_and_loaded_to_be_our_hash(self):
        """C-813：attempted 與 loaded 都等於自己這份、result ok 才回 0。"""
        candidate = registry_bytes(6)
        path = candidate_file(self.box, candidate)
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(content_hash(candidate), out)

    def test_C813_W_D1_old_error_classification_does_not_fail_the_install(self):
        """C-813／W-D1：舊的壞檔留下 parse_error，install 只認 attempted＝自己的雜湊。"""
        write_registry_file(self.paths.registry, b"{broken")
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertTrue(wait_until(
            lambda: _status_result(self.paths.status) == "parse_error"))
        candidate = registry_bytes(6)
        path = candidate_file(self.box, candidate)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("已載入世代 6", out)

    def test_C813_W_D2_insecure_file_is_exit_1_and_names_the_permission(self):
        """C-813／W-D2：status 是 insecure_file、attempted 為 null、心跳夠新 → 立即 1。

        低-1：心跳要比 install 起跑（`t0`）至少新 3 秒才採信，所以假 watcher 的心跳
        設在 `t0＋4`（`heartbeat_age=-4`）；否則那是「前一份名冊的舊狀態」。
        """
        path = candidate_file(self.box, registry_bytes(6))
        watcher = FakeWatcher(self.paths, mode="insecure", heartbeat_age=-4)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("insecure_file", out)
        self.assertIn("權限", out)
        self.assertIn("不得宣告撤銷完成", out)

    def test_C813_W_D2_old_insecure_status_is_not_trusted_before_t0_plus_3(self):
        """低-1：心跳只到 `t0`（不是 `t0＋3` 之後）的舊 insecure_file 不得當成這次的失敗。"""
        path = candidate_file(self.box, registry_bytes(6))
        watcher = FakeWatcher(self.paths, mode="insecure", heartbeat_age=0)
        watcher.start()
        self.addCleanup(watcher.stop)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "1"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("lgwd 拒絕載入", out)
        self.assertIn("尚未載入", out)

    def test_C813_W_D4_stale_heartbeat_is_exit_1(self):
        """C-813／W-D4：lgwd off（心跳超過 180 秒）→ 回 1，不宣告撤銷完成。"""
        path = candidate_file(self.box, registry_bytes(6))
        write_status(self.box, status_document(heartbeat=int(time.time()) - 300))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("未在輪詢", out)
        self.assertIn("不得宣告撤銷完成", out)

    def test_C813_timeout_without_status_is_exit_1(self):
        """C-813：完全沒有 status 檔（lgwd 沒跑）逾時回 1。"""
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "0.2"],
                           root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("未在輪詢", out)
        self.assertIn("不得宣告撤銷完成", out)

    def test_C813_attempted_is_our_hash_but_result_is_an_error_returns_1(self):
        """C-813：attempted＝自己的雜湊但結果錯 → 立即 1（不等逾時、不宣告完成）。"""
        path = candidate_file(self.box, registry_bytes(6))
        watcher = FakeWatcher(self.paths, mode="reject")
        watcher.start()
        self.addCleanup(watcher.stop)
        started = time.monotonic()
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "10"],
                           root=True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)
        self.assertIn("不得宣告撤銷完成", out)

    def test_C813_no_wait_returns_0_without_claiming_success(self):
        """C-813：`--no-wait` 寫完回 0，但明印未確認載入、不得宣告撤銷完成。"""
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--no-wait"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("未確認載入", out)
        self.assertIn("不得宣告撤銷完成", out)
        self.assertFalse(os.path.exists(self.paths.status))

    def test_C813_bad_install_then_reinstall_of_the_same_content_returns_0(self):
        """C-813：壞檔 install 回 1；之後重裝原內容回 0。"""
        bad = candidate_file(self.box, b"{broken", "bad.json")
        rc, out, err = run(self.box, ["auth-registry", "install", bad], root=True)
        self.assertEqual(rc, 1, out + err)
        watcher = FakeWatcher(self.paths)
        watcher.start()
        self.addCleanup(watcher.stop)
        path = candidate_file(self.box, self.loaded, "good-again.json")
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--timeout", "5"],
                           root=True)
        self.assertEqual(rc, 0, out + err)


def _status_result(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("result")
    except (OSError, ValueError):
        return None


# --- C-814：auth-registry status -------------------------------------------


class RegistryStatusTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.paths = sandbox_paths(self.box)

    def test_C814_ok_is_exit_0(self):
        """C-814：result ok、心跳新鮮、attempted＝loaded → 0。"""
        write_status(self.box, status_document())
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("loaded_generation 3", out)
        self.assertIn("result ok", out)
        self.assertIn("heartbeat_age_s", out)

    def test_C814_heartbeat_older_than_180_seconds_is_exit_1(self):
        """C-814／design :275：心跳超過 180 秒要報出來（輪詢執行緒停了）。"""
        write_status(self.box, status_document(heartbeat=int(time.time()) - 181))
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("未在輪詢", out)

    def test_C814_just_inside_the_heartbeat_window_is_ok(self):
        """C-814：179 秒還在窗內。"""
        write_status(self.box, status_document(heartbeat=int(time.time()) - 179))
        self.assertEqual(run(self.box, ["auth-registry", "status"])[0], 0)

    def test_C814_attempted_different_from_loaded_is_exit_1(self):
        """C-814：最近嘗試的雜湊與已載入不同＝載入沒有生效。"""
        document = status_document()
        document["attempted_hash"] = "sha256:" + "1" * 64
        write_status(self.box, document)
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 1, out + err)

    def test_C814_error_result_is_exit_1(self):
        """C-814：result 是錯誤分類回 1。"""
        write_status(self.box, status_document(result="parse_error"))
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("parse_error", out)

    def test_C814_missing_file_is_exit_2(self):
        """C-814：沒有 status 檔回 2。"""
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 2, out + err)

    def test_C814_broken_or_unknown_status_is_exit_2(self):
        """C-814：壞 JSON 與白名單外的 result 都回 2（不當成 ok）。"""
        self.box.write(self.paths.status, "{not json")
        self.assertEqual(run(self.box, ["auth-registry", "status"])[0], 2)
        write_status(self.box, status_document(result="invented"))
        self.assertEqual(run(self.box, ["auth-registry", "status"])[0], 2)

    def test_C814_needs_no_root_and_creates_nothing(self):
        """C-814：不用 root、只讀、不建檔、不取鎖。"""
        write_status(self.box, status_document())
        before = sorted(os.listdir(self.box.state()))
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(before, sorted(os.listdir(self.box.state())))

    def test_C814_output_has_no_key_ids_or_public_keys(self):
        """C-814／C-817：status 輸出不含公鑰，也不含 key ID。"""
        write_status(self.box, status_document())
        rc, out, err = run(self.box, ["auth-registry", "status"])
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("w1-tunnel-a", out)
        self.assertNotIn(b64_of(VALID_A), out)


# --- C-815：auth-replay status ---------------------------------------------


def set_water(path: str, key_id: str, h: int) -> None:
    db = open_rw(path)
    try:
        with db.txn() as cur:
            cur.execute(
                "INSERT INTO water (key_id, h) VALUES (?, ?)"
                " ON CONFLICT(key_id) DO UPDATE SET h = excluded.h",
                (key_id, h),
            )
    finally:
        db.close()


def add_nonce(path: str, key_id: str, nonce: str, ts: int) -> None:
    db = open_rw(path)
    try:
        with db.txn() as cur:
            cur.execute("INSERT INTO nonces (key_id, nonce, ts) VALUES (?, ?, ?)",
                        (key_id, nonce, ts))
    finally:
        db.close()


def make_crashed_replay(replay_db: str, *, with_shm: bool) -> list[str]:
    """把 replay 庫換成「上次未正常關閉」的檔案狀態（有 -wal；`-shm` 可有可無）。

    做法：真的寫一筆（WAL 有內容），趁連線還開著把檔案抄下來，再關連線；把抄本
    放回去（不抄 `-shm` 就是第三種狀態）。回傳放回去之後狀態目錄的檔案清單。
    """
    db = open_rw(replay_db)
    copies = {}
    try:
        with db.txn() as cur:
            cur.execute("INSERT INTO water (key_id, h) VALUES (?, ?)",
                        ("w1-tunnel-a", 1000))
        suffixes = ("", "-wal", "-shm") if with_shm else ("", "-wal")
        for suffix in suffixes:
            with open(replay_db + suffix, "rb") as handle:
                copies[suffix] = handle.read()
    finally:
        db.close()
    for suffix in ("", "-wal", "-shm"):
        path = replay_db + suffix
        if os.path.exists(path):
            os.unlink(path)
    for suffix, data in copies.items():
        with open(replay_db + suffix, "wb") as handle:
            handle.write(data)
    return sorted(os.listdir(os.path.dirname(replay_db)))


class ReplayStatusTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.paths = seed_stores(self.box)

    def test_C815_requires_root(self):
        """C-815：`mode=ro` 開庫要 root（庫是 llmgw 0600）。"""
        rc, out, err = run(self.box, ["auth-replay", "status"])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("root", err)

    def test_C815_lists_water_and_nonce_counts(self):
        """C-815：印伺服器時間與每 key 的 H、列數；沒有未來 H 回 0。"""
        now = now_seconds(SystemClock())
        set_water(self.paths.replay_db, "w1-tunnel-a", now - 100)
        add_nonce(self.paths.replay_db, "w1-tunnel-a", "n1", now - 10)
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("server_time", out)
        self.assertIn("key w1-tunnel-a h={0} nonces=1".format(now - 100), out)
        self.assertNotIn(b64_of(VALID_A), out)

    def test_C815_future_water_is_exit_1(self):
        """C-815：有 key 的 H 在現在＋60 秒之後 → 回 1 並列出。"""
        now = now_seconds(SystemClock())
        set_water(self.paths.replay_db, "w1-tunnel-a", now + 3600)
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("future w1-tunnel-a", out)

    def test_C815_key_without_water_is_shown_with_a_dash(self):
        """C-815：只有 nonce、還沒有清除水位的 key 也要列（H 為 -）。"""
        now = now_seconds(SystemClock())
        add_nonce(self.paths.replay_db, "w1-tunnel-b", "n2", now - 5)
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("key w1-tunnel-b h=- nonces=1", out)

    def test_C815_missing_store_is_exit_2_with_recover_pointer(self):
        """C-815：缺庫回 2 並指向 recover-store（不是悄悄建空庫）。"""
        os.unlink(self.paths.replay_db)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.paths.replay_db + suffix):
                os.unlink(self.paths.replay_db + suffix)
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("recover-store", err)
        self.assertFalse(os.path.exists(self.paths.replay_db))

    def test_C815_corrupt_store_is_exit_2(self):
        """C-815：壞庫回 2（與缺庫分開）。"""
        with open(self.paths.replay_db, "wb") as handle:
            handle.write(b"this is not a database")
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 2, out + err)

    def test_C815_permission_problem_is_exit_2_and_not_treated_as_missing(self):
        """C-815：打不開（權限）回 2 並說檢查權限，不叫人跑 recover-store。"""
        os.chmod(self.paths.replay_db, 0o000)
        self.addCleanup(os.chmod, self.paths.replay_db, 0o600)
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("權限", err)
        self.assertNotIn("recover-store", err)

    def test_C815_does_not_take_the_store_flock(self):
        """C-815：不取 `auth-store.lock`（與 lgwd 並存時仍可讀）。"""
        lock = acquire_store_lock(self.paths.store_lock)
        self.addCleanup(lock.release)
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)

    def test_C815_W_D10_clean_store_reads_without_creating_sidecars(self):
        """C-815／W-D10：lgwd 停止（沒有 -shm、也沒有 -wal）也讀得到，且不建檔。"""
        self.assertFalse(os.path.exists(self.paths.replay_db + "-wal"))
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.paths.replay_db + "-wal"))
        self.assertFalse(os.path.exists(self.paths.replay_db + "-shm"))

    def test_C815_W_D10_wal_store_while_lgwd_running_reads(self):
        """C-815／W-D10：lgwd 執行中（有 -wal／-shm）以 `mode=ro` 讀得到。"""
        db = open_rw(self.paths.replay_db)
        self.addCleanup(db.close)
        now = now_seconds(SystemClock())
        with db.txn() as cur:
            cur.execute("INSERT INTO water (key_id, h) VALUES (?, ?)",
                        ("w1-tunnel-a", now - 5))
        self.assertTrue(os.path.exists(self.paths.replay_db + "-wal"))
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("key w1-tunnel-a h={0}".format(now - 5), out)
        self.assertTrue(os.path.exists(self.paths.replay_db + "-shm"))  # 不動別人的

    def test_C815_W_D10_wal_without_shm_is_exit_2_and_creates_nothing(self):
        """C-815／W-D10：有 `-wal` 但沒有 `-shm`（上次未正常結束）→ 回 2，不開庫、不建檔。"""
        before = make_crashed_replay(self.paths.replay_db, with_shm=False)
        self.assertFalse(os.path.exists(self.paths.replay_db + "-shm"))
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("未正常關閉", err)
        self.assertFalse(os.path.exists(self.paths.replay_db + "-shm"))
        self.assertEqual(sorted(os.listdir(self.box.state())), before)


# --- 中-1：唯讀開庫的三種檔案狀態 -----------------------------------------


class ReadOnlyOpenStateTests(unittest.TestCase):
    """`auth_db.open_ro` 的三種狀態（經 CLI）：開庫前後目錄檔案清單完全相同。

    定案（REPORT-270-archreview §1）：沒有 `-wal` → `immutable=1`；`-wal` 與
    `-shm` 都在 → `mode=ro`；有 `-wal` 沒有 `-shm` → 回 2、不開庫、不建檔。
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.paths = seed_stores(self.box)

    def _listing(self):
        return sorted(os.listdir(self.box.state()))

    def test_state1_without_wal_reads_and_changes_no_files(self):
        """第一種：唯讀開庫前後，狀態目錄的檔案清單完全相同（不建 -wal／-shm）。"""
        self.assertFalse(os.path.exists(self.paths.replay_db + "-wal"))
        before = self._listing()
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._listing(), before)

    def test_state2_wal_and_shm_reads_and_changes_no_files(self):
        """第二種：`-wal` 與 `-shm` 都在（lgwd 執行中）→ 讀得到，清單不變。"""
        before = make_crashed_replay(self.paths.replay_db, with_shm=True)
        self.assertTrue(os.path.exists(self.paths.replay_db + "-shm"))
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("key w1-tunnel-a h=1000", out)
        self.assertEqual(self._listing(), before)

    def test_state3_wal_without_shm_is_exit_2_and_changes_no_files(self):
        """第三種：有 `-wal` 沒有 `-shm` → 回 2，不開庫、不建檔、不刪檔。"""
        before = make_crashed_replay(self.paths.replay_db, with_shm=False)
        self.assertFalse(os.path.exists(self.paths.replay_db + "-shm"))
        rc, out, err = run(self.box, ["auth-replay", "status"], root=True)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("未正常關閉", err)
        self.assertEqual(self._listing(), before)
        self.assertFalse(os.path.exists(self.paths.replay_db + "-shm"))


# --- C-816：結束碼與持有者 ------------------------------------------------


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.loaded = registry_bytes(5)
        self.paths = seed_stores(self.box, self.loaded)
        write_registry_file(self.paths.registry, self.loaded)

    def test_C816_status_and_replay_work_while_the_store_lock_is_held(self):
        """C-816：lgwd 持有 store 鎖時，`status` 與 `auth-replay status` 仍可用。"""
        lock = acquire_store_lock(self.paths.store_lock)
        self.addCleanup(lock.release)
        write_status(self.box, status_document())
        self.assertEqual(run(self.box, ["auth-registry", "status"])[0], 0)
        self.assertEqual(run(self.box, ["auth-replay", "status"], root=True)[0], 0)

    def test_C816_non_root_commands_are_exit_2(self):
        """C-816：非 root 的 install／replay status／--against-trust 一律 2（清楚拒絕）。"""
        path = candidate_file(self.box, registry_bytes(6))
        self.assertEqual(run(self.box, ["auth-registry", "install", path])[0], 2)
        self.assertEqual(run(self.box, ["auth-replay", "status"])[0], 2)
        self.assertEqual(run(self.box, ["auth-registry", "check", path,
                                        "--against-trust"])[0], 2)

    def test_C816_install_keeps_the_install_lock_0600(self):
        """C-816：install 鎖檔留在狀態目錄且是 0600。"""
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--no-wait"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        lock_path = os.path.join(self.paths.state_dir, "auth-registry-install.lock")
        self.assertEqual(stat.S_IMODE(os.stat(lock_path).st_mode), 0o600)

    def test_C816_install_lock_is_root_owned_in_production_but_not_in_prefix(self):
        """低-2：正式機把 install 鎖檔改成 `root:root`；prefix 沙箱（測試）不 chown。"""
        lock_path = os.path.join(self.paths.state_dir, "auth-registry-install.lock")
        with open(lock_path, "wb") as handle:
            handle.write(b"")
        os.chmod(lock_path, 0o600)

        def chown_calls(layout):
            calls = []
            with mock.patch("lgwctl.auth_cli.os.fchown",
                            side_effect=lambda fd, uid, gid: calls.append((uid, gid))):
                auth_cli._chown_install_lock(layout, lock_path)
            return calls

        self.assertEqual(chown_calls(mock.Mock(prefix="", euid=0)), [(0, 0)])
        self.assertEqual(chown_calls(mock.Mock(prefix=self.box.prefix, euid=0)), [])
        self.assertEqual(chown_calls(mock.Mock(prefix="", euid=1000)), [])


# --- C-817：輸出與審計不含公鑰 --------------------------------------------


class OutputHygieneTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.loaded = registry_bytes(5)
        self.paths = seed_stores(self.box, self.loaded)

    def test_C817_check_output_never_contains_public_keys(self):
        """C-817：check 只印 fingerprint，不印公鑰全文。"""
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "check", path, "--against-trust"],
                           root=True)
        self.assertIn(rc, (0, 1))
        for raw in (VALID_A, VALID_B, VALID_C):
            self.assertNotIn(b64_of(raw), out + err)

    def test_C817_fingerprint_comes_from_the_single_source_function(self):
        """C-817：顯示的 fingerprint 與 `auth_registry.fingerprint` 逐字相同。"""
        path = candidate_file(self.box, registry_bytes(3))
        rc, out, err = run(self.box, ["auth-registry", "check", path])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("fingerprint=SHA256:" + fingerprint(VALID_A), out)

    def test_C817_audit_has_no_public_keys(self):
        """C-817：審計只放世代、雜湊與去敏參數。"""
        path = candidate_file(self.box, registry_bytes(6))
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--no-wait"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        with open(self.paths.audit_log, encoding="utf-8") as handle:
            text = handle.read()
        for raw in (VALID_A, VALID_B, VALID_C):
            self.assertNotIn(b64_of(raw), text)
        self.assertNotIn("public_key", text)

    def test_C817_install_audit_uses_the_auditlog_whitelist(self):
        """中-2：install 的記錄與 `AuditLog.append` 同一形狀、同一套去敏（逐欄相等）。"""
        candidate = registry_bytes(6)
        path = candidate_file(self.box, candidate)
        rc, out, err = run(self.box, ["auth-registry", "install", path, "--no-wait"],
                           root=True)
        self.assertEqual(rc, 0, out + err)
        with open(self.paths.audit_log, encoding="utf-8") as handle:
            entry = json.loads([line for line in handle.read().splitlines() if line][0])
        expected = AuditLog(os.path.join(self.box.root, "reference-audit.log"),
                            clock=SystemClock()).append(
            "auth-registry install",
            args={"source": "file"},
            old={"generation": 5, "snapshot_hash": content_hash(self.loaded)},
            new={"generation": 6, "snapshot_hash": content_hash(candidate)},
        )
        for field in ("command", "args", "old", "new"):
            self.assertEqual(entry[field], expected[field])
        self.assertEqual(set(entry), set(expected))


if __name__ == "__main__":
    unittest.main()
