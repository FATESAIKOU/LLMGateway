"""`ops-scripts/lgw-registry-sync` 的離線測試（#292；契約 C-970～C-979）。

契約 C 編號對測試名稱（也列在 REPORT-292-impl.md）：

- C-970（輸入是 MLP#1739 的 JSON 陣列、version／name／status／公鑰／fingerprint、
  `?` 與 duplicate 整份停、invalid 略過）：
  `test_c970_add_new_worker_from_list`、`test_c970_invalid_worker_is_skipped`、
  `test_c970_fingerprint_mismatch_stops_whole`、
  `test_c970_bad_input_stops_whole`、`test_c970_duplicate_or_question_stops_whole`、
  `test_c970_empty_or_unreadable_list_stops`、`test_c970_control_characters_in_name_are_not_echoed`、
  `test_c970_oversized_name_is_not_echoed`、`test_c970_list_over_1mib_stops`（修正第 1 輪）。
- C-971（只管 `mlp-` 條目、非 `mlp-` 原封不動）：
  `test_c971_non_mlp_entries_preserved`。
- C-972（ID 規則、換鑰換 key ID、舊的 revoked、墓碑擋重放）：
  `test_c972_rotation_uses_new_key_id_and_revokes_old`、
  `test_c972_tombstone_replay_stops`、`test_c972_key_id_collision_stops`。
- C-973（新 principal 用 B、既有的權限不放寬）：
  `test_c973_new_principal_gets_b_existing_not_widened`。
- C-974（以已載入名冊為基準、世代＋1、無變更不產生）：
  `test_c974_no_change_writes_nothing_and_is_idempotent`、
  `test_c974_not_loaded_stops`。
- C-975（空清單、消失的不自動撤銷、`--revoke`、新 name 上限）：
  `test_c975_missing_not_revoked_until_flagged`、
  `test_c975_revoke_requires_absent_name`、`test_c975_new_name_limit_and_allow_new`、
  `test_c975_repeated_revoke_is_deduped`（修正第 1 輪）、`test_c970_invalid_worker_is_skipped`。
- C-976（候選檔一定經過真的 `llmgwctl auth-registry check --against-trust`）：
  `test_c976_candidate_passes_real_check`、`test_c976_check_failure_writes_nothing`。
- C-977（摘要固定格式、不印公鑰全文、註解與 token；清單字串跳脫與截斷）：
  `test_c977_output_has_no_public_key_or_comment`、`test_c977_source_and_name_are_sanitized`、
  `test_c977_display_is_safe_and_bounded`（修正第 2 輪；直接測 `_display` 與 `--revoke` 的錯誤訊息）。
- C-978（審計一筆、狀態檔只記雜湊與時刻、鎖、狀態檔不當守門）：
  `test_c978_audit_state_and_lock`、`test_c978_state_is_not_a_gate`。
- C-979（不 install、不呼叫 gh、不讀 `/run`；`--list -` 讀 stdin）：
  `test_c979_never_installs_or_calls_gh`、`test_c979_stdin_list_supported`。

全部在 `tests/lgwctl_fakes.Sandbox` 的 prefix 沙箱裡跑：名冊、兩庫、狀態檔與候選檔
都在暫存目錄；`llmgwctl` 用一個只把 `os.geteuid` 換成 0 的包裝行程去跑**真的**
`lgwctl`（`check --against-trust` 的 root 檢查是唯一的模擬），不碰真機、不讀憑證、
不呼叫真的 `install`。
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

try:
    from .auth_fakes import VALID_A, VALID_B, VALID_C, public_key_line
    from .lgwctl_fakes import Sandbox
except ImportError:  # python3 -m unittest discover -s tests
    from auth_fakes import VALID_A, VALID_B, VALID_C, public_key_line
    from lgwctl_fakes import Sandbox

from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths
from lgwd.auth_registry import OPS, content_hash, fingerprint, parse_registry
from lgwd.auth_trust import TrustStore
from lgwd.policy import AGENTS, ROLES

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ops-scripts" / "lgw-registry-sync"

_SCRIPT_MODULE = None


def script_module():
    """把沒有 `.py` 副檔名的腳本載進來，直接測 `_display` 這類內部函式。"""
    global _SCRIPT_MODULE
    if _SCRIPT_MODULE is None:
        loader = SourceFileLoader("lgw_registry_sync_292_under_test", str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _SCRIPT_MODULE = module
    return _SCRIPT_MODULE


def fp(raw32: bytes) -> str:
    return "SHA256:" + fingerprint(raw32)


def fp12(raw32: bytes) -> str:
    from lgwd.auth_registry import ssh_public_key_blob

    return hashlib.sha256(ssh_public_key_blob(raw32)).hexdigest()[:12]


def fake_key(tag: str) -> bytes:
    """測試用的固定 32 bytes（與 repo 其他測試的假金鑰不重複；沒有私鑰）。"""
    return hashlib.sha256(("lgw-registry-sync-292/" + tag).encode("utf-8")).digest()


def b_principal() -> dict:
    """B 預設（值從程式匯入，測試不寫死）。"""
    return {
        "ops": list(OPS),
        "resolve_agents": list(AGENTS),
        "roles": {role: list(AGENTS) for role in ROLES},
    }


def key_entry(key_id: str, principal: str, raw32: bytes, status: str = "active") -> dict:
    return {
        "key_id": key_id,
        "principal": principal,
        "public_key": public_key_line(raw32),
        "status": status,
    }


def worker(name: str, raw32: bytes | None = None, *, status: str = "ok",
           source: str = "worker", comment: str | None = None,
           fingerprint_value: str | None = None) -> dict:
    if status != "ok":
        return {"version": 1, "name": name, "public_key": "", "fingerprint": "",
                "source": source, "status": status}
    line = public_key_line(raw32)
    if comment:
        line += " " + comment
    return {
        "version": 1,
        "name": name,
        "public_key": line,
        "fingerprint": fp(raw32) if fingerprint_value is None else fingerprint_value,
        "source": source,
        "status": "ok",
    }


WRAPPER = """#!/usr/bin/env python3
import os, sys
os.geteuid = lambda: 0
sys.path.insert(0, {root!r})
from lgwctl import cli
with open(os.environ["LGW_SYNC_ARGV_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(cli.main(sys.argv[1:]))
"""

WRAPPER_CHECK_FAILS = """#!/usr/bin/env python3
import os, sys
argv = sys.argv[1:]
with open(os.environ["LGW_SYNC_ARGV_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(argv) + "\\n")
if "check" in argv:
    print("fail 名冊不合格（generation_not_increased）")
    sys.exit(1)
os.geteuid = lambda: 0
sys.path.insert(0, {root!r})
from lgwctl import cli
sys.exit(cli.main(argv))
"""


class Machine:
    """一個沙箱：prefix＋兩庫＋已載入名冊＋狀態檔＋可記錄 argv 的 llmgwctl。"""

    def __init__(self, *, check_fails: bool = False):
        self.box = Sandbox()
        self.root = self.box.root
        self.argv_log = os.path.join(self.root, "llmgwctl-argv.log")
        text = (WRAPPER_CHECK_FAILS if check_fails else WRAPPER).format(root=str(REPO_ROOT))
        self.wrapper = self.box.write(os.path.join(self.root, "llmgwctl"), text, 0o755)
        self.env = dict(self.box.env)
        self.env["LGW_SYNC_LLMGWCTL"] = self.wrapper
        self.env["LGW_SYNC_ARGV_LOG"] = self.argv_log
        self.out = os.path.join(self.root, "candidate.json")

    def close(self) -> None:
        self.box.close()

    # -- 路徑 ---------------------------------------------------------------

    def paths(self):
        return auth_paths(self.box.state(), registry=self.box.etc("auth-registry.json"))

    def list_path(self, name: str = "mlp-keys.json") -> str:
        return os.path.join(self.root, name)

    # -- 建狀態 -------------------------------------------------------------

    def seed(self, registry_obj: dict) -> bytes:
        os.makedirs(self.box.state(), exist_ok=True)
        os.makedirs(self.box.etc(), exist_ok=True)
        paths = self.paths()
        if not os.path.exists(paths.trust_db):
            create_stores(paths.trust_db, paths.replay_db)
        raw = (json.dumps(registry_obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.box.write(paths.registry, raw.decode("utf-8"), 0o644)
        store = TrustStore(paths.trust_db)
        try:
            store.commit(parse_registry(raw), raw, content_hash(raw))
        finally:
            store.close()
        self.write_status(generation=registry_obj["generation"],
                          loaded_hash=content_hash(raw))
        return raw

    def write_status(self, *, generation: int, loaded_hash: str, result: str = "ok",
                     attempted_hash: str | None = None) -> str:
        document = {
            "schema": 1,
            "loaded_generation": generation,
            "loaded_hash": loaded_hash,
            "attempted_hash": loaded_hash if attempted_hash is None else attempted_hash,
            "result": result,
            "heartbeat": int(time.time()),
        }
        return self.box.write(self.paths().status,
                              json.dumps(document, separators=(",", ":")), 0o644)

    def write_list(self, entries, name: str | None = None) -> str:
        path = self.list_path(name or "mlp-keys.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False)
        return path

    def run(self, entries=None, *, list_path: str | None = None, extra=(),
            out: str | None = None, stdin_text: str | None = None):
        if list_path is None:
            list_path = self.write_list([] if entries is None else entries)
        argv = [sys.executable, str(SCRIPT), "plan", "--list", list_path,
                "--out", self.out if out is None else out, *extra]
        return subprocess.run(argv, cwd=str(REPO_ROOT), env=self.env,
                              capture_output=True, text=True, timeout=120,
                              input=stdin_text)

    # -- 讀結果 -------------------------------------------------------------

    def candidate(self) -> dict | None:
        try:
            with open(self.out, encoding="utf-8") as fh:
                return json.load(fh)
        except OSError:
            return None

    def audit_lines(self) -> list[dict]:
        try:
            with open(self.paths().audit_log, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def state(self) -> dict | None:
        path = os.path.join(self.paths().state_dir, "registry-sync.state")
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except OSError:
            return None

    def state_text(self) -> str:
        with open(os.path.join(self.paths().state_dir, "registry-sync.state"),
                  encoding="utf-8") as fh:
            return fh.read()

    def write_state_text(self, text: str) -> str:
        path = os.path.join(self.paths().state_dir, "registry-sync.state")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o600)
        return path

    def argv(self) -> list[str]:
        try:
            with open(self.argv_log, encoding="utf-8") as fh:
                return [line for line in fh.read().splitlines() if line]
        except OSError:
            return []


def base_registry(generation: int = 12, *, keys=None, principals=None,
                  narrow: bool = False) -> dict:
    """兩個 worker（w1 有效、w4 有效）＋本機管理 principal 與一筆人工金鑰。"""
    if principals is None:
        principals = {
            "mlp-w1": b_principal(),
            "mlp-w4": b_principal(),
            "local-admin": {"ops": ["list", "usage"], "resolve_agents": [], "roles": {}},
        }
        if narrow:
            principals["mlp-w1"] = {"ops": ["list"], "resolve_agents": [], "roles": {}}
    if keys is None:
        keys = [
            key_entry("mlp-w1-aaaaaaaaaaaa", "mlp-w1", VALID_A),
            key_entry("mlp-w4-cccccccccccc", "mlp-w4", VALID_C),
            key_entry("llmgwctl-local-admin", "local-admin", VALID_B),
        ]
    return {"schema": 1, "generation": generation,
            "principals": principals, "keys": keys}


class RegistrySyncTests(unittest.TestCase):
    def setUp(self):
        self.machines: list[Machine] = []

    def tearDown(self):
        for machine in self.machines:
            machine.close()

    def machine(self, *, check_fails: bool = False) -> Machine:
        machine = Machine(check_fails=check_fails)
        self.machines.append(machine)
        return machine

    # -- C-970 --------------------------------------------------------------

    def test_c970_add_new_worker_from_list(self):
        """新 name：加 principal（B）與 `mlp-<name>-<fp12>`，世代＋1、check ok。"""
        m = self.machine()
        m.seed(base_registry())
        new_key = fake_key("w9")
        proc = m.run([worker("w1", VALID_A), worker("w9", new_key, source="node")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("candidate generation 13", proc.stdout)
        self.assertIn("candidate generation 13 (sha256:", proc.stdout)
        self.assertIn("add       mlp-w9-" + fp12(new_key), proc.stdout)
        self.assertIn("check     ok", proc.stdout)
        candidate = m.candidate()
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["generation"], 13)
        self.assertEqual(candidate["principals"]["mlp-w9"], b_principal())
        row = next(key for key in candidate["keys"] if key["key_id"]
                   == "mlp-w9-" + fp12(new_key))
        self.assertEqual(row["principal"], "mlp-w9")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["public_key"], public_key_line(new_key))

    def test_c970_invalid_worker_is_skipped(self):
        """invalid 那一台不新增、不撤銷、不換鑰；其他 worker 照常處理。"""
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A), worker("w4", status="invalid"),
                      worker("w9", fake_key("w9-invalid"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skip      w4  (invalid)", proc.stdout)
        candidate = m.candidate()
        self.assertIsNotNone(candidate)
        w4 = next(key for key in candidate["keys"] if key["principal"] == "mlp-w4")
        self.assertEqual(w4["status"], "active", "invalid 不可以撤銷")
        self.assertNotIn("revoke    " + w4["key_id"], proc.stdout)

    def test_c970_fingerprint_mismatch_stops_whole(self):
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A, fingerprint_value="SHA256:wrong")])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("fingerprint", proc.stderr)

    def test_c970_bad_input_stops_whole(self):
        cases = {
            "version 不是 1": [dict(worker("w1", VALID_A), version=2)],
            "不是 ssh-ed25519": [dict(worker("w1", VALID_A),
                                      public_key="ssh-rsa AAAAB3NzaC1yc2E")],
            "name 太長": [worker("w" * 48, VALID_A)],
            "name 是問號": [worker("?", VALID_A)],
            "頂層不是陣列": {"version": 1, "workers": []},
        }
        for label, entries in cases.items():
            with self.subTest(label):
                m = self.machine()
                m.seed(base_registry())
                path = m.list_path("bad.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(entries, fh)
                proc = m.run(list_path=path)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertFalse(os.path.exists(m.out), label)
                self.assertIn("fail", proc.stderr)
        m = self.machine()
        m.seed(base_registry())
        path = m.list_path("broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        proc = m.run(list_path=path)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))

    def test_c970_duplicate_or_question_stops_whole(self):
        # status=duplicate
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A), worker("w9", status="duplicate")])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("duplicate", proc.stderr)
        # 同一把公鑰出現在兩個 name（兩筆都是 ok）
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A), worker("w9", VALID_A)])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("兩個 name", proc.stderr)
        # 同一個 name 出現兩次
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A), worker("w1", VALID_A)])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))

    def test_c970_empty_or_unreadable_list_stops(self):
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("空的", proc.stderr)
        missing = m.list_path("does-not-exist.json")
        proc = m.run(list_path=missing)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(m.out))

    # -- C-971 --------------------------------------------------------------

    def test_c971_non_mlp_entries_preserved(self):
        """本機管理 principal 與人工金鑰在候選檔裡逐項相同（物件層級）。"""
        m = self.machine()
        registry = base_registry()
        m.seed(registry)
        proc = m.run([worker("w1", VALID_A), worker("w4", VALID_C),
                      worker("w9", fake_key("w9-preserve"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        candidate = m.candidate()
        self.assertEqual(candidate["principals"]["local-admin"],
                         registry["principals"]["local-admin"])
        original = next(key for key in registry["keys"]
                        if key["key_id"] == "llmgwctl-local-admin")
        kept = next(key for key in candidate["keys"]
                    if key["key_id"] == "llmgwctl-local-admin")
        self.assertEqual(kept, original)

    def test_c971_revoke_never_touches_non_mlp(self):
        """`--revoke` 只認 `mlp-` 條目；點名本機管理 principal 要拒絕。"""
        m = self.machine()
        m.seed(base_registry())
        for name in ("local-admin", "l-admin"):
            with self.subTest(name=name):
                proc = m.run([worker("w1", VALID_A), worker("w4", VALID_C)],
                             extra=("--revoke", name))
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("沒有可撤銷的 active key", proc.stderr)
                self.assertFalse(os.path.exists(m.out))

    # -- C-972 --------------------------------------------------------------

    def test_c972_rotation_uses_new_key_id_and_revokes_old(self):
        m = self.machine()
        m.seed(base_registry())
        new_key = fake_key("w1-rotate")
        proc = m.run([worker("w1", new_key), worker("w4", VALID_C)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        old_id = "mlp-w1-aaaaaaaaaaaa"
        new_id = "mlp-w1-" + fp12(new_key)
        self.assertIn("replace   {0} -> {1}".format(old_id, new_id), proc.stdout)
        candidate = m.candidate()
        rows = {key["key_id"]: key for key in candidate["keys"]}
        self.assertEqual(rows[new_id]["status"], "active")
        self.assertEqual(rows[new_id]["public_key"], public_key_line(new_key))
        self.assertEqual(rows[old_id]["status"], "revoked", "舊的標 revoked、不刪除")
        self.assertIn("mlp-w1", candidate["principals"], "principal 要保留")

    def test_c972_tombstone_replay_stops(self):
        """舊清單重放：舊鑰已是墓碑，整份停，不撤銷任何人。"""
        m = self.machine()
        rotated = fake_key("w1-rotated")
        registry = base_registry(generation=13, keys=[
            key_entry("mlp-w1-aaaaaaaaaaaa", "mlp-w1", VALID_A, status="revoked"),
            key_entry("mlp-w1-" + fp12(rotated), "mlp-w1", rotated),
            key_entry("mlp-w4-cccccccccccc", "mlp-w4", VALID_C),
            key_entry("llmgwctl-local-admin", "local-admin", VALID_B),
        ])
        m.seed(registry)
        proc = m.run([worker("w1", VALID_A), worker("w4", VALID_C)])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("已撤銷的公鑰", proc.stderr)
        self.assertIn("w1", proc.stderr)

    def test_c972_key_id_collision_stops(self):
        m = self.machine()
        collision = "mlp-w9-" + fp12(VALID_B)
        registry = base_registry(keys=[
            key_entry("mlp-w1-aaaaaaaaaaaa", "mlp-w1", VALID_A),
            key_entry(collision, "mlp-w1", VALID_C),
            key_entry("llmgwctl-local-admin", "local-admin", fake_key("collision-admin")),
        ])
        m.seed(registry)
        proc = m.run([worker("w1", VALID_A), worker("w9", VALID_B)])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("key ID 已存在但公鑰不同", proc.stderr)

    # -- C-973 --------------------------------------------------------------

    def test_c973_new_principal_gets_b_existing_not_widened(self):
        m = self.machine()
        m.seed(base_registry(narrow=True))
        proc = m.run([worker("w1", fake_key("w1-narrow-new")),
                      worker("w4", VALID_C), worker("w9", fake_key("w9-b"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        candidate = m.candidate()
        self.assertEqual(candidate["principals"]["mlp-w1"],
                         {"ops": ["list"], "resolve_agents": [], "roles": {}},
                         "人工收窄過的 principal 不可以被放寬")
        self.assertEqual(candidate["principals"]["mlp-w9"], b_principal())

    # -- C-974 --------------------------------------------------------------

    def test_c974_no_change_writes_nothing_and_is_idempotent(self):
        m = self.machine()
        raw = m.seed(base_registry())
        del raw
        for attempt in range(2):
            proc = m.run([worker("w1", VALID_A), worker("w4", VALID_C)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("無變更", proc.stdout)
            self.assertFalse(os.path.exists(m.out))
            self.assertEqual(len(m.audit_lines()), attempt + 1,
                             "每次 plan 一筆審計")
        state = m.state()
        self.assertEqual(set(state), {"schema", "input_sha256", "time"})

    def test_c974_not_loaded_stops(self):
        m = self.machine()
        m.seed(base_registry())
        m.write_status(generation=12, loaded_hash="sha256:" + "0" * 64)
        proc = m.run([worker("w1", VALID_A), worker("w4", VALID_C)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("未載入", proc.stderr)
        self.assertFalse(os.path.exists(m.out))
        # result 不是 ok 也停
        m2 = self.machine()
        m2.seed(base_registry())
        with open(m2.paths().registry, "rb") as fh:
            digest = content_hash(fh.read())
        m2.write_status(generation=12, loaded_hash=digest, result="load_failed")
        proc = m2.run([worker("w1", VALID_A), worker("w4", VALID_C)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("未載入", proc.stderr)
        self.assertFalse(os.path.exists(m2.out))

    # -- C-975 --------------------------------------------------------------

    def test_c975_missing_not_revoked_until_flagged(self):
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("missing   w4  (not in list; not revoked; use --revoke w4)",
                      proc.stdout)
        self.assertFalse(os.path.exists(m.out), "消失的 worker 不自動撤銷")
        self.assertNotIn("revoke    ", proc.stdout)

        proc = m.run([worker("w1", VALID_A)], extra=("--revoke", "w4"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        candidate = m.candidate()
        self.assertIsNotNone(candidate)
        rows = {key["key_id"]: key for key in candidate["keys"]}
        self.assertEqual(rows["mlp-w4-cccccccccccc"]["status"], "revoked")
        self.assertIn("mlp-w4", candidate["principals"], "principal 要保留")
        self.assertEqual(rows["mlp-w1-aaaaaaaaaaaa"]["status"], "active")

    def test_c975_revoke_requires_absent_name(self):
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A)], extra=("--revoke", "w1"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("仍在清單裡", proc.stderr)
        self.assertFalse(os.path.exists(m.out))
        proc = m.run([worker("w1", VALID_A)], extra=("--revoke", "w7"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("沒有可撤銷的 active key", proc.stderr)
        self.assertFalse(os.path.exists(m.out))

    def test_c975_new_name_limit_and_allow_new(self):
        m = self.machine()
        m.seed(base_registry())
        workers = [worker("w1", VALID_A), worker("w4", VALID_C),
                   worker("w7", fake_key("w7")), worker("w8", fake_key("w8")),
                   worker("w9", fake_key("w9-limit"))]
        proc = m.run(workers)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("超過上限 2", proc.stderr)
        self.assertFalse(os.path.exists(m.out))
        proc = m.run(workers, extra=("--allow-new", "3"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("guards    new 3/3", proc.stdout)
        self.assertIsNotNone(m.candidate())

    # -- C-976 --------------------------------------------------------------

    def test_c976_candidate_passes_real_check(self):
        m = self.machine()
        m.seed(base_registry())
        rotated = fake_key("w1-check")
        proc = m.run([worker("w1", rotated), worker("w4", VALID_C),
                      worker("w9", fake_key("w9-check"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = m.argv()
        self.assertTrue(any(call.startswith("auth-registry status") for call in calls),
                        calls)
        check_calls = [call for call in calls if call.startswith(
            "auth-registry check --against-trust ")]
        self.assertEqual(len(check_calls), 1, calls)

        # 用真的 llmgwctl 再 check 一次同一份候選檔。
        rerun = subprocess.run([m.wrapper, "auth-registry", "check", "--against-trust",
                                m.out], env=m.env, capture_output=True, text=True,
                               timeout=120)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("result ok", rerun.stdout)

        added_line = next(line for line in rerun.stdout.splitlines()
                          if line.startswith("added "))
        revoked_line = next(line for line in rerun.stdout.splitlines()
                            if line.startswith("revoked "))
        removed_line = next(line for line in rerun.stdout.splitlines()
                            if line.startswith("removed "))
        added = set() if added_line.split(" ", 1)[1] == "（無）" \
            else set(added_line.split(" ", 1)[1].split("、"))
        revoked = set() if revoked_line.split(" ", 1)[1] == "（無）" \
            else set(revoked_line.split(" ", 1)[1].split("、"))
        self.assertEqual(removed_line.split(" ", 1)[1], "（無）",
                         "名冊只加不刪（墓碑那幾列要留下）")
        summary_add = {line.split()[1] for line in proc.stdout.splitlines()
                       if line.startswith("add       ")}
        summary_replace_old = {line.split()[1] for line in proc.stdout.splitlines()
                               if line.startswith("replace   ")}
        summary_replace_new = {line.split()[3] for line in proc.stdout.splitlines()
                               if line.startswith("replace   ")}
        summary_revoke = {line.split()[1] for line in proc.stdout.splitlines()
                          if line.startswith("revoke    ")}
        self.assertEqual(added, summary_add | summary_replace_new,
                         (added, summary_add, summary_replace_new))
        self.assertEqual(revoked, summary_replace_old | summary_revoke,
                         (revoked, summary_replace_old, summary_revoke))

    def test_c976_check_failure_writes_nothing(self):
        m = self.machine(check_fails=True)
        m.seed(base_registry())
        proc = m.run([worker("w9", fake_key("w9-check-fail"))])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("check     failed", proc.stdout)
        self.assertIn("check --against-trust", proc.stderr)
        self.assertFalse(os.path.exists(m.out), "check 失敗不可以留下候選檔")
        leftovers = [name for name in os.listdir(os.path.dirname(m.out))
                     if name.startswith(".candidate.json.")]
        self.assertEqual(leftovers, [], "暫存檔要收掉")

    # -- C-977 --------------------------------------------------------------

    def test_c977_output_has_no_public_key_or_comment(self):
        m = self.machine()
        m.seed(base_registry())
        comment = "worker-w9-hostname.internal"
        new_key = fake_key("w9-comment")
        proc = m.run([worker("w1", VALID_A), worker("w9", new_key, comment=comment)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        encoded = public_key_line(new_key).split(" ", 1)[1]
        for text in (proc.stdout, proc.stderr,
                     "".join(json.dumps(line, ensure_ascii=False)
                             for line in m.audit_lines())):
            self.assertNotIn(encoded, text, "摘要與審計不印公鑰全文")
            self.assertNotIn(comment, text, "註解不進摘要與審計")
            self.assertNotIn("ssh-ed25519", text)
        row = next(key for key in m.candidate()["keys"]
                   if key["key_id"] == "mlp-w9-" + fp12(new_key))
        self.assertEqual(row["public_key"], public_key_line(new_key),
                         "註解要丟掉")

    # -- C-978 --------------------------------------------------------------

    def test_c978_audit_state_and_lock(self):
        m = self.machine()
        m.seed(base_registry())
        new_key = fake_key("w9-audit")
        proc = m.run([worker("w9", new_key)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = m.audit_lines()
        self.assertEqual(len(lines), 1)
        record = lines[0]
        self.assertEqual(record["command"], "auth-registry sync-plan")
        self.assertEqual(record["old"]["generation"], 12)
        self.assertEqual(record["new"]["generation"], 13)
        self.assertEqual(record["new"]["added"], ["mlp-w9-" + fp12(new_key)])
        state = m.state()
        self.assertEqual(set(state), {"schema", "input_sha256", "time"})
        self.assertTrue(state["input_sha256"].startswith("sha256:"))

        # 另一個人正在跑 plan：鎖被持有 → 2，沒有候選檔。
        lock_path = os.path.join(m.paths().state_dir, "registry-sync.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.exists(m.out):
                os.unlink(m.out)
            proc = m.run([worker("w9", new_key)])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("另一個 registry-sync plan", proc.stderr)
            self.assertFalse(os.path.exists(m.out))
        finally:
            os.close(fd)

    # -- C-979 --------------------------------------------------------------

    def test_c979_never_installs_or_calls_gh(self):
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w9", fake_key("w9-never-install"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for call in m.argv():
            self.assertTrue(call.startswith("auth-registry "), call)
            self.assertNotIn("install", call)
            self.assertNotIn("gh ", call)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("/run/", source)
        proc = m.run([worker("w9", fake_key("w9-never-install"))], extra=("--apply",))
        self.assertEqual(proc.returncode, 2, "v1 不提供 --apply")

    # -- 修正第 1 輪（arch 中-1／低-1；agy M-3） --------------------------

    def test_c977_source_and_name_are_sanitized(self):
        """清單來的 source 不得原樣印出；不在允許值內只顯示固定占位字串。"""
        m = self.machine()
        m.seed(base_registry())
        marker = "FORGED"
        evil_source = ("worker\nreplace   mlp-w1-aaaaaaaaaaaa -> mlp-w1-bbbbbbbbbbbb"
                       "\x1b[1A\x1b[2K " + marker)
        proc = m.run([worker("w1", VALID_A),
                      worker("w9", fake_key("w9-sanitize"), source=evil_source)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("source=?", proc.stdout, "不合規則的 source 只顯示占位字串")
        audit_text = "".join(json.dumps(line, ensure_ascii=False)
                             for line in m.audit_lines())
        for text in (proc.stdout, proc.stderr, audit_text):
            self.assertNotIn("\x1b", text, "輸出不得含 ESC")
            self.assertNotIn(marker, text, "source 原值不得印出")
            self.assertNotIn("bbbbbbbbbbbb", text)
        self.assertEqual(
            [line for line in proc.stdout.splitlines() if line.startswith("replace")], [],
            "注入的換行不能多出假的 replace 行")
        self.assertLess(max(len(line) for line in proc.stdout.splitlines()), 300)

    def test_c970_control_characters_in_name_are_not_echoed(self):
        m = self.machine()
        m.seed(base_registry())
        evil = "w1\x1b[2J\x1b[31mPWNED\nresult ok"
        proc = m.run([worker(evil, VALID_A)])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        combined = proc.stdout + proc.stderr
        self.assertIn("第 1 筆", proc.stderr, "不合規則的 name 只印第幾筆")
        self.assertNotIn("\x1b", combined)
        self.assertNotIn("PWNED", combined, "原始 name 不得印出")
        self.assertNotIn("result ok", combined, "原始 name 不得偽造結果行")
        self.assertLess(max(len(line) for line in combined.splitlines()), 300)

    def test_c970_oversized_name_is_not_echoed(self):
        """超長 name：錯誤訊息有上限，不把 600 KB 的原字串印出來。"""
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("a" * 600_000, VALID_A)])
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(m.out))
        self.assertIn("第 1 筆", proc.stderr)
        self.assertLess(len(proc.stderr), 300)
        self.assertLess(max(len(line) for line in proc.stderr.splitlines()), 300)

    def test_c970_list_over_1mib_stops(self):
        """讀清單有 1 MiB 上限（檔案與 stdin 都算）。"""
        m = self.machine()
        m.seed(base_registry())
        big = "[" + " " * 1_100_000 + "]"
        path = m.list_path("big.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(big)
        proc = m.run(list_path=path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("1 MiB", proc.stderr)
        self.assertFalse(os.path.exists(m.out))
        proc = m.run(list_path="-", stdin_text=big)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("1 MiB", proc.stderr)
        self.assertFalse(os.path.exists(m.out))

    def test_c979_stdin_list_supported(self):
        """agy M-3a：--list - 從 stdin 讀清單。"""
        m = self.machine()
        m.seed(base_registry())
        key = fake_key("w9-stdin")
        payload = json.dumps([worker("w9", key)])
        proc = m.run(list_path="-", stdin_text=payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mlp-w9-" + fp12(key), proc.stdout)
        self.assertIsNotNone(m.candidate())

    def test_c975_repeated_revoke_is_deduped(self):
        """agy M-3b：同一個 name 重複 --revoke 只撤一次。"""
        m = self.machine()
        m.seed(base_registry())
        proc = m.run([worker("w1", VALID_A)],
                     extra=("--revoke", "w4", "--revoke", "w4"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        revoke_lines = [line for line in proc.stdout.splitlines()
                        if line.startswith("revoke    ")]
        self.assertEqual(len(revoke_lines), 1, revoke_lines)
        self.assertEqual(len(m.audit_lines()[0]["new"]["revoked"]), 1)
        revoked = [row["key_id"] for row in m.candidate()["keys"]
                   if row["status"] == "revoked"]
        self.assertEqual(revoked, ["mlp-w4-cccccccccccc"])

    def test_c978_state_is_not_a_gate(self):
        """agy M-3c：狀態檔損壞或雜湊不符時，plan 仍照常執行（不當守門）。"""
        m = self.machine()
        m.seed(base_registry())
        m.write_state_text("not json")
        proc = m.run([worker("w9", fake_key("w9-state"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(m.candidate())
        self.assertNotIn("與上次相同", proc.stdout)
        m.write_state_text(json.dumps({"schema": 1,
                                       "input_sha256": "sha256:" + "0" * 64,
                                       "time": 0}))
        os.unlink(m.out)
        proc = m.run([worker("w9", fake_key("w9-state"))])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(m.candidate())
        self.assertEqual(set(json.loads(m.state_text())),
                         {"schema", "input_sha256", "time"})

    # -- 修正第 2 輪（archreview2 低-1／低-2；只加測試） ------------------

    def test_c977_display_is_safe_and_bounded(self):
        """`_display` 的單元測試＋`--revoke` 錯誤訊息含控制字元的端到端案例。"""
        module = script_module()
        display = module._display
        cases = {
            "ESC": ("a\x1b[31mred", "a\\x1b[31mred"),
            "換行與 CR/TAB": ("a\nb\r\tc", "a\\x0ab\\x0d\\x09c"),
            "反斜線": ("a\\b", "a\\\\b"),
            "非 ASCII": ("主機名", "\\u4e3b\\u6a5f\\u540d"),
            "空字串": ("", ""),
        }
        for label, (value, expected) in cases.items():
            with self.subTest(label):
                out = display(value)
                self.assertEqual(out, expected)
                self.assertNotIn("\x1b", out)
                self.assertNotIn("\n", out)
                self.assertNotIn("\r", out)
                self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in out),
                                 "不得留下控制字元")
                self.assertTrue(all(ord(char) < 128 for char in out))
                self.assertLessEqual(len(out), module.DISPLAY_LIMIT)

        long_value = "x" * 5000
        self.assertEqual(display(long_value), "x" * module.DISPLAY_LIMIT)
        escaped = display("中" * 100)
        self.assertLessEqual(len(escaped), module.DISPLAY_LIMIT)
        self.assertTrue(all(ord(char) < 128 for char in escaped))
        self.assertEqual(display(12345), "12345", "非字串也不炸")

        # archreview2 低-2：--revoke 的格式錯誤訊息也走 _display。
        m = self.machine()
        m.seed(base_registry())
        evil = "w4\x1b[2J\x1b[31mPWNED\nresult ok"
        proc = m.run([worker("w1", VALID_A)], extra=("--revoke", evil))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(m.out))
        lines = proc.stderr.splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("--revoke", lines[0])
        self.assertIn("\\x1b", lines[0], "ESC 要以轉義形式出現")
        self.assertIn("\\x0a", lines[0], "換行要以轉義形式出現")
        self.assertNotIn("\x1b", proc.stdout + proc.stderr)
        self.assertLess(len(lines[0]), 300)


if __name__ == "__main__":
    unittest.main()
