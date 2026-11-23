"""SSHSIG 驗章（D2 PR-5）的測試；對應 tasks C-501～C-507。

全部用臨時目錄與臨時產生的內容；固定向量（`tests/vectors/sshsig_v1.json`）本身是用
一次性金鑰簽出來、私鑰早已丟棄。真正 exec `/usr/bin/ssh-keygen` 的測試用
`skipUnless(本機 OpenSSH ≥ 9.6)`；其餘分類與故障用假的 `ssh-keygen`／`prlimit`
腳本，不碰真機、不讀 ~/.ssh。

測試名都帶 C 編號，報告的「C 編號對測試名稱」表直接由這裡對出來。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lgwd import auth_registry, sshsig
from lgwd.auth_registry import KeyEntry
from lgwd.sshsig import (
    COULD_NOT,
    INCORRECT,
    NAMESPACE,
    Outcome,
    Reject,
    SignersFile,
    Verifier,
    VerifierUnavailable,
    classify,
    fingerprint,
    good_line,
    preparse,
    public_key_blob,
    write_allowed_signers,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTOR_PATH = REPO_ROOT / "tests" / "vectors" / "sshsig_v1.json"
VECTOR = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))

SSH_KEYGEN = "/usr/bin/ssh-keygen"
REAL_PRLIMIT = "/usr/bin/prlimit"


def _local_openssh() -> tuple[int, int] | None:
    try:
        done = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"OpenSSH_(\d+)\.(\d+)", (done.stdout + done.stderr).decode(
        "ascii", "replace"))
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


LOCAL_OPENSSH = _local_openssh()
REAL_SSH_OK = LOCAL_OPENSSH is not None and LOCAL_OPENSSH >= (9, 6)


def _ssh_string(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + data


def vector_blob() -> bytes:
    return base64.urlsafe_b64decode(VECTOR["signature"] + "==")


def bad_vector_blob() -> bytes:
    return base64.urlsafe_b64decode(VECTOR["bad_signature"] + "==")


def encode(blob: bytes) -> str:
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def make_entry(key_id: str = "key-one", line: str | None = None,
               status: str = "active") -> KeyEntry:
    line = line or VECTOR["public_key"]
    blob = base64.b64decode(line.split()[1], validate=True)
    return KeyEntry(
        key_id=key_id,
        principal="alice",
        public_key=blob[-32:],
        status=status,
    )


OTHER_KEY_LINE = "ssh-ed25519 " + base64.b64encode(
    _ssh_string(b"ssh-ed25519")
    + _ssh_string(hashlib.sha256(b"another-key").digest())
).decode("ascii")


def make_blob(*, public_key: bytes | None = None, magic: bytes = b"SSHSIG",
              version: int = 1, namespace: bytes = NAMESPACE.encode(),
              reserved: bytes = b"", hash_algorithm: bytes = b"sha512",
              signature_type: bytes = b"ssh-ed25519",
              signature: bytes | None = None,
              inner_trailing: bytes = b"", outer_trailing: bytes = b"",
              signature_field: bytes | None = None) -> bytes:
    """組一份 SSHSIG 信封；預設用固定向量的公鑰與正確簽章。"""
    entry = make_entry()
    if public_key is None:
        public_key = public_key_blob(entry)
    if signature is None:
        good = vector_blob()
        signature = good[-64:]
    inner = (signature_field if signature_field is not None else
             _ssh_string(signature_type) + _ssh_string(signature) + inner_trailing)
    return (
        magic
        + version.to_bytes(4, "big")
        + _ssh_string(public_key)
        + _ssh_string(namespace)
        + _ssh_string(reserved)
        + _ssh_string(hash_algorithm)
        + _ssh_string(inner)
        + outer_trailing
    )


def write_script(path: str, body: str, mode: int = 0o755) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(path, mode)
    return path


def passthrough_prlimit(path: str) -> str:
    """假的 prlimit：跳過資源旗標後 exec 後面的指令（不依賴 util-linux）。"""
    return write_script(path, """#!/bin/sh
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
""")


def fake_keygen(path: str, *, stdout: bytes = b"", stderr: bytes = b"",
                returncode: int = 0, sleep: float = 0.0,
                segv: bool = False) -> str:
    """假 ssh-keygen：吃 stdin、依參數輸出固定 bytes／結束碼。"""
    body = '''#!/usr/bin/python3
import os, sys, time
sys.stdin.buffer.read()
time.sleep(%r)
sys.stdout.buffer.write(bytes.fromhex(%r))
sys.stdout.buffer.flush()
sys.stderr.buffer.write(bytes.fromhex(%r))
sys.stderr.buffer.flush()
''' % (sleep, stdout.hex(), stderr.hex())
    if segv:
        body += "os.kill(os.getpid(), 11)\n"
    body += "os._exit(%d)\n" % returncode
    return write_script(path, body)


def fake_ssh(path: str, version_text: str) -> str:
    return write_script(path, """#!/bin/sh
echo %r >&2
""" % version_text)


def holding_keygen(path: str, pid_file: str) -> str:
    """假 ssh-keygen：開一個孫行程後長睡，並寫下自己的 pid／pgid／孫行程 pid。

    用來驗證逾時**真的 SIGKILL 整個行程群並回收**（agy 的 M5）：拿掉 `_kill_group`
    時這些 pid 會活著，測試就轉紅。
    """
    return write_script(path, '''#!/usr/bin/python3
import os, sys, time
sys.stdin.buffer.read()
child = os.fork()
if child == 0:
    time.sleep(60)
    os._exit(0)
with open(%r, "w") as handle:
    handle.write("%%d %%d %%d\\n" %% (os.getpid(), os.getpgid(0), child))
time.sleep(60)
os._exit(0)
''' % pid_file)


def sig_checking_keygen(path: str, line: bytes, record: str, sleep: float) -> str:
    """假 ssh-keygen：睡一下，醒來時看 `-s` 的簽章檔還在不在，再輸出 Good 行。

    記下 `exists`／`missing`，用來驗「自測不會刪在途請求的簽章檔」。
    """
    return write_script(path, '''#!/usr/bin/python3
import os, sys, time
sys.stdin.buffer.read()
args = sys.argv[1:]
sig = args[args.index("-s") + 1] if "-s" in args else ""
time.sleep(%r)
with open(%r, "w") as handle:
    handle.write("exists" if sig and os.path.exists(sig) else "missing")
sys.stdout.buffer.write(bytes.fromhex(%r))
sys.stdout.buffer.flush()
os._exit(0)
''' % (sleep, record, line.hex()))


def live_group_members(pgid: int) -> list:
    """掃 /proc，回傳行程群 `pgid` 裡還活著的行程（殭屍不算）。"""
    alive = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "rb") as handle:
                fields = handle.read().split()
        except OSError:
            continue
        if len(fields) < 5 or fields[2] == b"Z":
            continue
        try:
            member_pgid = int(fields[4])
        except ValueError:
            continue
        if member_pgid == pgid:
            alive.append(int(entry))
    return alive


def wait_group_gone(pgid: int, timeout: float = 3.0) -> bool:
    """等行程群裡沒有任何活著的行程（已結束、只剩殭屍算不在）；逾時回 False。"""
    deadline = time.monotonic() + timeout
    while True:
        if not live_group_members(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


class RecordingLock:
    """記錄進入次數的 Lock 替身（只給「引用計數真的在鎖內改」的測試用）。"""

    def __init__(self) -> None:
        self.entries = 0
        self._inner = threading.RLock()

    def __enter__(self) -> "RecordingLock":
        self.entries += 1
        self._inner.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self._inner.release()


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sshsig-test-")
        os.chmod(self.tmp, 0o700)
        self.spool = os.path.join(self.tmp, "spool")
        os.mkdir(self.spool, 0o700)
        self.signers_dir = os.path.join(self.tmp, "signers")
        os.mkdir(self.signers_dir, 0o700)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin, 0o700)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 共用裝配 ----------------------------------------------------

    def real_verifier(self, **kwargs) -> Verifier:
        return Verifier(spool_dir=self.spool, **kwargs)

    def fake_verifier(self, keygen: str, *, concurrency: int = 16,
                      timeout: float = 2.0) -> Verifier:
        prlimit = passthrough_prlimit(os.path.join(self.bin, "prlimit"))
        return Verifier(
            sshkeygen_path=keygen,
            prlimit_path=prlimit,
            spool_dir=self.spool,
            home_dir=self.spool,
            concurrency=concurrency,
            timeout=timeout,
            owner_uid=os.geteuid(),
        )

    def signers(self, generation: int = 1, entries=None) -> SignersFile:
        return write_allowed_signers(
            entries or [make_entry()], self.signers_dir, generation)

    def assert_reject(self, blob: bytes, kind: str | None = None) -> None:
        with self.assertRaises(Reject) as caught:
            preparse(encode(blob), make_entry())
        if kind is not None:
            self.assertEqual(caught.exception.kind, kind)

    def verify_with_fake(self, keygen: str, *, entry: KeyEntry | None = None,
                         signers: SignersFile | None = None,
                         blob: bytes | None = None,
                         message: bytes | None = None, **kwargs) -> Outcome:
        entry = entry or make_entry()
        signers = signers or self.signers()
        verifier = self.fake_verifier(keygen, **kwargs)
        # 中-4：呼叫端在取快照時 acquire，請求結束才 release。
        self.assertTrue(signers.acquire())
        try:
            return verifier.verify(
                message if message is not None else VECTOR["signing_data"].encode(),
                blob if blob is not None else vector_blob(),
                entry,
                signers,
            )
        finally:
            signers.release()


# ---------------------------------------------------------------------------
# C-501 信封預解析
# ---------------------------------------------------------------------------


class PreparseTests(TempDirCase):
    def test_c501_accepts_fixed_vector_envelope(self) -> None:
        blob = preparse(VECTOR["signature"], make_entry())
        self.assertIsInstance(blob, bytes)
        self.assertEqual(len(blob), 180)
        self.assertTrue(blob.startswith(b"SSHSIG"))

    def test_c501_rejects_padding(self) -> None:
        with self.assertRaises(Reject):
            preparse(VECTOR["signature"] + "==", make_entry())
        with self.assertRaises(Reject):
            preparse(VECTOR["signature"] + "=", make_entry())

    def test_c501_rejects_length_239_and_241(self) -> None:
        with self.assertRaises(Reject) as caught:
            preparse(VECTOR["signature"][:-1], make_entry())
        self.assertEqual(caught.exception.kind, "bad_length")
        with self.assertRaises(Reject) as caught:
            preparse(VECTOR["signature"] + "A", make_entry())
        self.assertEqual(caught.exception.kind, "bad_length")

    def test_c501_rejects_noncanonical_base64(self) -> None:
        # 180 bytes 是 3 的倍數，240 字元的規範編碼一一對應；不合 base64url 字集的
        # 字元（標準 base64 的 +／/、空白、空白字元）與 padding 都要擋。
        for bad in ("+" + VECTOR["signature"][1:],
                    "/" + VECTOR["signature"][1:],
                    VECTOR["signature"][:-1] + " "):
            with self.subTest(bad=bad[-1]):
                with self.assertRaises(Reject) as caught:
                    preparse(bad, make_entry())
                self.assertEqual(caught.exception.kind, "bad_encoding")

    def test_c501_rejects_wrong_magic(self) -> None:
        self.assert_reject(make_blob(magic=b"SSHSIh"), kind="bad_magic")

    def test_c501_rejects_wrong_version(self) -> None:
        self.assert_reject(make_blob(version=2), kind="bad_version")

    def test_c501_rejects_public_key_mismatch(self) -> None:
        other = base64.b64decode(OTHER_KEY_LINE.split()[1])
        self.assert_reject(make_blob(public_key=other), kind="public_key_mismatch")

    def test_c501_rejects_other_namespace(self) -> None:
        # 同長度（10 bytes）的另一個 namespace，才走得到 namespace 檢查。
        self.assert_reject(make_blob(namespace=b"mlpsidecar"), kind="bad_namespace")

    def test_c501_rejects_nonempty_reserved(self) -> None:
        # 補 1 byte 的 reserved、減 1 byte 的簽章，維持 180 bytes 才走得到 reserved 檢查。
        self.assert_reject(make_blob(reserved=b"x", signature=bytes(63)),
                           kind="reserved_not_empty")

    def test_c501_rejects_bad_hash_algorithm(self) -> None:
        self.assert_reject(make_blob(hash_algorithm=b"sha256"), kind="bad_hash_algorithm")

    def test_c501_rejects_bad_signature_type(self) -> None:
        # 同長度（11 bytes）的另一種型別，維持 180 bytes。
        self.assert_reject(make_blob(signature_type=b"ssh-rsa-256"),
                           kind="bad_signature_type")

    def test_c501_rejects_signature_length_not_64(self) -> None:
        self.assert_reject(make_blob(signature=bytes(63)), kind="bad_length")
        self.assert_reject(make_blob(signature=bytes(65)), kind="bad_length")

    def test_c501_rejects_extended_s_ge_l(self) -> None:
        good = vector_blob()
        scalar = int.from_bytes(good[-32:], "little")
        extended = scalar + sshsig.ED25519_ORDER
        self.assertLess(extended, 2 ** 256)
        self.assert_reject(make_blob(signature=good[:32] + extended.to_bytes(32, "little")),
                           kind="signature_not_canonical")

    def test_c501_rejects_extra_byte(self) -> None:
        # 簽章減 1 byte＋多 1 byte 的尾端，維持 180 bytes 才走得到「多餘 bytes」。
        self.assert_reject(make_blob(outer_trailing=b"x", signature=bytes(63)),
                           kind="trailing_bytes")
        self.assert_reject(make_blob(inner_trailing=b"x", signature=bytes(63)),
                           kind="trailing_bytes")

    def test_c501_rejects_truncated_180(self) -> None:
        self.assert_reject(vector_blob()[:-1], kind="bad_length")

    def test_c501_reject_does_not_fork(self) -> None:
        scalar = int.from_bytes(vector_blob()[-32:], "little")
        bad = make_blob(signature=(scalar + sshsig.ED25519_ORDER).to_bytes(32, "little"))
        with mock.patch.object(sshsig.subprocess, "Popen",
                               side_effect=AssertionError("must not fork")):
            with self.assertRaises(Reject):
                preparse(encode(bad), make_entry())
            for blob in (make_blob(namespace=b"other"), make_blob(version=2),
                         make_blob(outer_trailing=b"x")):
                with self.assertRaises(Reject):
                    preparse(encode(blob), make_entry())


# ---------------------------------------------------------------------------
# C-502 allowed_signers
# ---------------------------------------------------------------------------


class AllowedSignersTests(TempDirCase):
    def test_c502_writes_active_keys_only(self) -> None:
        active = make_entry("key-one")
        revoked = make_entry("key-two", status="revoked")
        signers = self.signers(7, [active, revoked])
        self.assertEqual(signers.generation, 7)
        self.assertTrue(signers.path.endswith("allowed_signers.7"))
        self.assertEqual([entry.key_id for entry in signers.entries], ["key-one"])
        self.assertEqual(stat.S_IMODE(os.stat(signers.path).st_mode), 0o600)
        lines = Path(signers.path).read_text(encoding="ascii").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0],
            '%s namespaces="%s" ssh-ed25519 %s' % (
                "key-one", NAMESPACE,
                base64.b64encode(public_key_blob(active)).decode("ascii")),
        )

    def test_c502_readback_selfcheck_blocks_bad_format(self) -> None:
        with mock.patch.object(sshsig, "_format_signers_line",
                               return_value='key-one namespaces="other" ssh-ed25519 AAA='):
            with self.assertRaises(VerifierUnavailable):
                write_allowed_signers([make_entry()], self.spool, 3)
        self.assertEqual(
            [name for name in os.listdir(self.spool) if name.startswith(".allowed")], [])
        self.assertFalse(os.path.exists(os.path.join(self.spool, "allowed_signers.3")))

    def test_c502_refcount_deletes_old_file_after_release(self) -> None:
        first = self.signers(1)
        first.acquire()
        second = self.signers(2)
        first.retire()
        self.assertTrue(os.path.exists(first.path))  # 在途請求還在用
        first.release()
        self.assertFalse(os.path.exists(first.path))
        self.assertTrue(os.path.exists(second.path))

    def test_c502_external_modification_is_unavailable_without_fork(self) -> None:
        entry = make_entry()
        signers = self.signers(1, [entry])
        marker = os.path.join(self.tmp, "forked")
        recorder = write_script(os.path.join(self.bin, "prlimit"), """#!/bin/sh
touch %s
exit 255
""" % marker)
        verifier = Verifier(sshkeygen_path=os.path.join(self.bin, "unused"),
                            prlimit_path=recorder, spool_dir=self.spool,
                            home_dir=self.spool, owner_uid=os.geteuid())
        with open(signers.path, "a", encoding="ascii") as handle:
            handle.write("tampered\n")
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(), entry,
                                signers),
                Outcome.UNAVAILABLE,
            )
        finally:
            signers.release()
        self.assertFalse(os.path.exists(marker))

    def test_c502_verify_after_retire_with_caller_ref_is_pass(self) -> None:
        # 中-4(a)：呼叫端先 acquire，換世代 retire，verify 仍 PASS；舊檔在
        # caller release() 之後才刪。
        entry = make_entry()
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(entry) + "\n").encode())
        verifier = self.fake_verifier(keygen)
        signers = self.signers(1, [entry])
        self.assertTrue(signers.acquire())
        signers.retire()
        self.assertTrue(os.path.exists(signers.path))  # caller 還持有引用
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                entry, signers),
                Outcome.PASS,
            )
            self.assertTrue(os.path.exists(signers.path))
        finally:
            signers.release()
        self.assertFalse(os.path.exists(signers.path))

    def test_c502_verify_without_held_ref_is_unavailable(self) -> None:
        # 中-4(b)：沒持有引用就呼叫 verify → UNAVAILABLE、不 fork。
        entry = make_entry()
        signers = self.signers(1, [entry])
        self.assertEqual(signers.refs, 0)
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(entry) + "\n").encode())
        verifier = self.fake_verifier(keygen)
        with mock.patch.object(sshsig.subprocess, "Popen",
                               side_effect=AssertionError("must not fork")):
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                entry, signers),
                Outcome.UNAVAILABLE,
            )
        self.assertTrue(os.path.exists(signers.path))  # 沒有被刪

    def test_c502_inflight_verify_keeps_old_file(self) -> None:
        entry = make_entry()
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(entry) + "\n").encode(), sleep=0.4)
        verifier = self.fake_verifier(keygen)
        signers = self.signers(1, [entry])
        # 中-4：呼叫端（請求）在進 verify 前就持有引用。
        self.assertTrue(signers.acquire())
        results: list[Outcome] = []
        worker = threading.Thread(target=lambda: results.append(
            verifier.verify(VECTOR["signing_data"].encode(), vector_blob(), entry, signers)))
        worker.start()
        time.sleep(0.15)
        signers.retire()
        self.assertTrue(os.path.exists(signers.path))  # 在途請求還沒結束
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [Outcome.PASS])
        self.assertTrue(os.path.exists(signers.path))  # caller 還沒 release
        signers.release()
        self.assertFalse(os.path.exists(signers.path))

    def test_c502_missing_directory_rejected(self) -> None:
        with self.assertRaises(VerifierUnavailable):
            write_allowed_signers([make_entry()], os.path.join(self.tmp, "nope"), 1)

    def test_c502_old_generation_file_not_deleted_while_held(self) -> None:
        first = self.signers(4)
        first.acquire()
        first.retire()
        self.assertTrue(os.path.exists(first.path))
        first.release()
        first.release()  # 多餘的 release 不會把引用數變負或刪別人的檔
        self.assertFalse(os.path.exists(first.path))

    def test_c502_acquire_after_retire_is_false(self) -> None:
        # 中-2：retire 之後 acquire 回 False；verify 判 UNAVAILABLE、不 fork。
        entry = make_entry()
        signers = self.signers(1, [entry])
        signers.retire()
        self.assertFalse(os.path.exists(signers.path))
        self.assertFalse(signers.acquire())
        self.assertEqual(signers.refs, 0)
        verifier = self.fake_verifier(fake_keygen(os.path.join(self.bin, "ssh-keygen")))
        with mock.patch.object(sshsig.subprocess, "Popen",
                               side_effect=AssertionError("must not fork")):
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                entry, signers),
                Outcome.UNAVAILABLE,
            )

    def test_c502_refcount_changes_take_the_lock(self) -> None:
        # 中-2：acquire／release／retire 的計數改動都在 `threading.Lock` 內（拿掉鎖轉紅）。
        locks: list[RecordingLock] = []

        def factory() -> RecordingLock:
            lock = RecordingLock()
            locks.append(lock)
            return lock

        with mock.patch.object(sshsig.threading, "Lock", side_effect=factory):
            signers = self.signers(1)
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        self.assertTrue(signers.acquire())
        self.assertEqual(lock.entries, 1)
        signers.release()
        self.assertEqual(lock.entries, 2)
        signers.retire()
        self.assertEqual(lock.entries, 3)
        self.assertEqual(signers.refs, 0)
        self.assertEqual(lock.entries, 4)
        self.assertFalse(signers.acquire())
        self.assertEqual(lock.entries, 5)

    def test_c502_concurrent_refcount_with_retire(self) -> None:
        # 中-2：多執行緒反覆 acquire／release 加一次 retire：計數歸零、檔案恰好刪一次。
        signers = self.signers(1)
        unlinked: list = []
        real_unlink = os.unlink
        errors: list = []

        def spy(path, *args, **kwargs):
            if path == signers.path:
                unlinked.append(path)
            return real_unlink(path, *args, **kwargs)

        def worker() -> None:
            try:
                for _ in range(300):
                    if signers.acquire():
                        signers.release()
            except BaseException as exc:  # noqa: BLE001 - 測試要看到任何例外
                errors.append(exc)

        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # 讓計數的非原子更新真的撞在一起
        try:
            with mock.patch.object(sshsig.os, "unlink", side_effect=spy):
                threads = [threading.Thread(target=worker) for _ in range(8)]
                for thread in threads:
                    thread.start()
                signers.retire()
                for thread in threads:
                    thread.join(10)
        finally:
            sys.setswitchinterval(interval)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(signers.refs, 0)
        self.assertEqual(unlinked, [signers.path])
        self.assertFalse(os.path.exists(signers.path))
        self.assertFalse(signers.acquire())

    def test_c502_verify_requires_entry_in_snapshot(self) -> None:
        # 低-1：快照裡沒有這個 entry 就 UNAVAILABLE、不 fork（-I 與檔案同源）。
        signers = self.signers(1, [make_entry("key-one")])
        other = make_entry("key-two")
        verifier = self.fake_verifier(fake_keygen(os.path.join(self.bin, "ssh-keygen")))
        self.assertTrue(signers.acquire())
        try:
            with mock.patch.object(sshsig.subprocess, "Popen",
                                   side_effect=AssertionError("must not fork")):
                self.assertEqual(
                    verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                    other, signers),
                    Outcome.UNAVAILABLE,
                )
        finally:
            signers.release()


# ---------------------------------------------------------------------------
# C-503 子行程 argv／env／暫存檔
# ---------------------------------------------------------------------------


class SubprocessTests(TempDirCase):
    def test_c503_argv_and_env_are_fixed(self) -> None:
        record_path = os.path.join(self.tmp, "record.json")
        recorder = write_script(os.path.join(self.bin, "prlimit"), """#!/usr/bin/python3
import json, os, sys
with open(%r, "w") as handle:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, handle)
sys.stdout.write("Could not verify signature.\\n")
sys.stderr.write("Signature verification failed: incorrect signature\\n")
raise SystemExit(255)
""" % record_path)
        entry = make_entry()
        signers = self.signers(2, [entry])
        verifier = Verifier(sshkeygen_path="/opt/fake/ssh-keygen",
                            prlimit_path=recorder, spool_dir=self.spool,
                            home_dir=self.spool, owner_uid=os.geteuid())
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(), entry,
                                signers),
                Outcome.BAD_SIG,
            )
        finally:
            signers.release()
        record = json.loads(Path(record_path).read_text(encoding="utf-8"))
        argv = record["argv"]
        self.assertEqual(argv[:5], ["--cpu=2", "--as=268435456", "--nofile=32", "--",
                                    "/opt/fake/ssh-keygen"])
        self.assertEqual(argv[5:], [
            "-Y", "verify", "-f", signers.path, "-I", entry.key_id,
            "-n", NAMESPACE, "-s", argv[-1],
        ])
        self.assertTrue(argv[-1].startswith(os.path.join(self.spool, "sig-")))
        self.assertEqual(record["env"],
                         {"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": self.spool})

    def test_c503_popen_flags(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(make_entry()) + "\n").encode())
        real_popen = subprocess.Popen
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_popen(*args, **kwargs)

        with mock.patch.object(sshsig.subprocess, "Popen", side_effect=spy):
            outcome = self.verify_with_fake(keygen)
        self.assertEqual(outcome, Outcome.PASS)
        self.assertTrue(calls)
        _, kwargs = calls[0]
        self.assertTrue(kwargs["close_fds"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("preexec_fn", kwargs)
        self.assertFalse(kwargs.get("shell", False))

    def test_c503_signature_file_removed_after_run(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stderr=b"boom\n", returncode=1)
        self.assertEqual(self.verify_with_fake(keygen), Outcome.UNAVAILABLE)
        self.assertEqual(os.listdir(self.spool), [])

    def test_c503_clean_spool_removes_leftovers(self) -> None:
        Path(self.spool, "sig-crash.asc").write_text("x", encoding="ascii")
        os.mkdir(os.path.join(self.spool, "selftest-crash"))
        verifier = self.real_verifier()
        self.assertEqual(verifier.clean_spool(), 2)
        self.assertEqual(os.listdir(self.spool), [])

    def test_c503_clean_spool_only_removes_own_named_files(self) -> None:
        # 中-1(b)：只刪 `sig-*.asc` 與 `selftest-*`；別人的檔（含狀態目錄會有的檔）不碰。
        Path(self.spool, "sig-crash.asc").write_text("x", encoding="ascii")
        os.mkdir(os.path.join(self.spool, "selftest-crash"))
        Path(self.spool, "selftest-crash", "tmp").write_text("x", encoding="ascii")
        foreign = ("unrelated.dat", "allowed_signers.3", "sig-keep.txt")
        for name in foreign:
            Path(self.spool, name).write_text("keep", encoding="ascii")
        self.assertEqual(self.real_verifier().clean_spool(), 2)
        self.assertEqual(sorted(os.listdir(self.spool)), sorted(foreign))
        for name in foreign:
            self.assertEqual(Path(self.spool, name).read_text(encoding="ascii"), "keep")

    def test_c503_signature_file_removed_after_timeout(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"), sleep=5)
        started = time.monotonic()
        self.assertEqual(self.verify_with_fake(keygen, timeout=0.3), Outcome.UNAVAILABLE)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(os.listdir(self.spool), [])

    def test_c503_timeout_kills_process_group(self) -> None:
        """M5：逾時要 SIGKILL 整個行程群並回收；拿掉 kill 時 pid 還活著就轉紅。"""
        entry = make_entry()
        pid_file = os.path.join(self.tmp, "pids")
        keygen = holding_keygen(os.path.join(self.bin, "ssh-keygen"), pid_file)
        self.assertEqual(
            self.verify_with_fake(keygen, entry=entry, timeout=0.3),
            Outcome.UNAVAILABLE,
        )
        pid, pgid, child = (
            int(value) for value in Path(pid_file).read_text(encoding="ascii").split())
        self.assertEqual(pgid, pid)
        try:
            self.assertTrue(
                wait_group_gone(pgid),
                "行程群沒被 SIGKILL／回收：pgid=%d child=%d" % (pgid, child),
            )
            self.assertEqual(live_group_members(pgid), [])
        finally:
            for leftover in (pid, child):
                try:
                    os.kill(leftover, signal.SIGKILL)
                except OSError:
                    pass
        self.assertEqual(os.listdir(self.spool), [])

    def test_c503_output_capped_at_4kib(self) -> None:
        good = (good_line(make_entry()) + "\n").encode()
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=good + b"A" * 5000)
        self.assertEqual(self.verify_with_fake(keygen), Outcome.UNAVAILABLE)

    def test_c503_spool_must_be_private(self) -> None:
        loose = os.path.join(self.tmp, "loose-spool")
        os.mkdir(loose, 0o755)
        with self.assertRaises(ValueError):
            Verifier(spool_dir=loose, owner_uid=os.geteuid())
        missing = os.path.join(self.tmp, "not-created")
        verifier = Verifier(spool_dir=missing, owner_uid=os.geteuid())
        signers = self.signers()
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                make_entry(), signers),
                Outcome.UNAVAILABLE,
            )
        finally:
            signers.release()

    def test_c503_no_preexec_fn_and_no_time_time_in_source(self) -> None:
        source = (REPO_ROOT / "lgwd" / "sshsig.py").read_text(encoding="utf-8")
        self.assertNotIn("preexec_fn=", source)
        self.assertNotIn("time.time(", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("import lgwd.http", source)
        self.assertNotIn("import lgwd.main", source)
        self.assertIn("close_fds=True", source)
        self.assertIn("start_new_session=True", source)


# ---------------------------------------------------------------------------
# C-504 分類（唯一的 401 出口）
# ---------------------------------------------------------------------------


class ClassifyTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.entry = make_entry()

    def test_c504_pass_requires_good_line(self) -> None:
        line = (good_line(self.entry) + "\n").encode()
        self.assertEqual(classify(0, line, b"", self.entry), Outcome.PASS)
        self.assertEqual(classify(0, line.replace(b"\n", b"\r\n"), b"", self.entry),
                         Outcome.PASS)

    def test_c504_exit_zero_alone_is_not_pass(self) -> None:
        self.assertEqual(classify(0, b"", b"", self.entry), Outcome.UNAVAILABLE)
        self.assertEqual(classify(0, b"Good\n", b"", self.entry), Outcome.UNAVAILABLE)
        self.assertEqual(
            classify(0, (good_line(self.entry) + "\n" + "extra\n").encode(), b"",
                     self.entry),
            Outcome.UNAVAILABLE,
        )
        self.assertEqual(
            classify(0, (good_line(self.entry) + "\n").encode(), b"warning\n",
                     self.entry),
            Outcome.UNAVAILABLE,
        )

    def test_c504_pass_needs_matching_key_id(self) -> None:
        other = good_line(self.entry).replace('for key-one ', 'for key-two ')
        self.assertEqual(classify(0, (other + "\n").encode(), b"", self.entry),
                         Outcome.UNAVAILABLE)

    def test_c504_pass_needs_matching_fingerprint(self) -> None:
        correct = good_line(self.entry)
        wrong = correct[:correct.rindex(":") + 1] + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        self.assertEqual(classify(0, (wrong + "\n").encode(), b"", self.entry),
                         Outcome.UNAVAILABLE)

    def test_c504_openssh96_form_is_bad_sig(self) -> None:
        self.assertEqual(
            classify(255, (COULD_NOT + "\n").encode(), (INCORRECT + "\r\n").encode(),
                     self.entry),
            Outcome.BAD_SIG,
        )

    def test_c504_two_line_stderr_form_is_bad_sig(self) -> None:
        self.assertEqual(
            classify(255, b"",
                     (INCORRECT + "\n" + COULD_NOT + "\n").encode(), self.entry),
            Outcome.BAD_SIG,
        )

    def test_c504_could_not_in_both_streams_is_unavailable(self) -> None:
        self.assertEqual(
            classify(255, (COULD_NOT + "\n").encode(),
                     (INCORRECT + "\r\n" + COULD_NOT + "\r\n").encode(), self.entry),
            Outcome.UNAVAILABLE,
        )

    def test_c504_only_could_not_is_unavailable(self) -> None:
        self.assertEqual(classify(255, (COULD_NOT + "\n").encode(), b"", self.entry),
                         Outcome.UNAVAILABLE)
        self.assertEqual(classify(255, b"", (COULD_NOT + "\n").encode(), self.entry),
                         Outcome.UNAVAILABLE)

    def test_c504_empty_stderr_is_unavailable(self) -> None:
        self.assertEqual(classify(255, (COULD_NOT + "\n").encode(), b"", self.entry),
                         Outcome.UNAVAILABLE)

    def test_c504_table_external_stderr_is_unavailable(self) -> None:
        self.assertEqual(
            classify(255, (COULD_NOT + "\n").encode(),
                     b"prefix: " + INCORRECT.encode() + b"\n", self.entry),
            Outcome.UNAVAILABLE,
        )
        self.assertEqual(
            classify(255, (COULD_NOT + "\n").encode(),
                     INCORRECT.encode() + b" extra\n", self.entry),
            Outcome.UNAVAILABLE,
        )
        self.assertEqual(
            classify(255, (COULD_NOT + "\n").encode(),
                     b"Couldn't verify signature: namespace does not match\r\n",
                     self.entry),
            Outcome.UNAVAILABLE,
        )

    def test_c504_stdout_garbage_with_good_stderr_is_unavailable(self) -> None:
        # 變異「忽略 stdout」會讓這個落到 401。
        self.assertEqual(
            classify(255, b"unexpected\n", (INCORRECT + "\r\n").encode(), self.entry),
            Outcome.UNAVAILABLE,
        )

    def test_c504_other_exit_codes_are_unavailable(self) -> None:
        for code in (1, 2, 127, -11, -9):
            self.assertEqual(
                classify(code, (COULD_NOT + "\n").encode(),
                         (INCORRECT + "\r\n").encode(), self.entry),
                Outcome.UNAVAILABLE,
            )

    def test_c504_end_to_end_fake_forms(self) -> None:
        good = (good_line(make_entry()) + "\n").encode()
        cases = [
            (dict(stdout=good), Outcome.PASS),
            (dict(stdout=(COULD_NOT + "\n").encode(),
                  stderr=(INCORRECT + "\r\n").encode(), returncode=255),
             Outcome.BAD_SIG),
            (dict(stdout=b"", stderr=b"", returncode=255), Outcome.UNAVAILABLE),
            (dict(stdout=(COULD_NOT + "\n").encode(), stderr=b"", returncode=255),
             Outcome.UNAVAILABLE),
            (dict(stderr=b"unknown\n", returncode=1), Outcome.UNAVAILABLE),
            (dict(returncode=0, segv=True), Outcome.UNAVAILABLE),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"), **kwargs)
                self.assertEqual(self.verify_with_fake(keygen), expected)


# ---------------------------------------------------------------------------
# C-505 並發與期限
# ---------------------------------------------------------------------------


class ConcurrencyTests(TempDirCase):
    def test_c505_semaphore_wait_is_bounded(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(make_entry()) + "\n").encode())
        verifier = self.fake_verifier(keygen, concurrency=1, timeout=0.2)
        signers = self.signers()
        self.assertTrue(signers.acquire())
        self.assertTrue(verifier._semaphore.acquire(blocking=False))
        started = time.monotonic()
        try:
            outcome = verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                      make_entry(), signers)
        finally:
            verifier._semaphore.release()
            signers.release()
        self.assertEqual(outcome, Outcome.UNAVAILABLE)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_c505_exec_failure_is_unavailable(self) -> None:
        verifier = Verifier(sshkeygen_path=os.path.join(self.bin, "missing-keygen"),
                            prlimit_path=os.path.join(self.bin, "missing-prlimit"),
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid())
        signers = self.signers()
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                make_entry(), signers),
                Outcome.UNAVAILABLE,
            )
        finally:
            signers.release()
        self.assertEqual(os.listdir(self.spool), [])

    def test_c505_healthy_false_blocks_without_fork(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(make_entry()) + "\n").encode())
        verifier = self.fake_verifier(keygen)
        verifier._healthy = False
        signers = self.signers()
        self.assertTrue(signers.acquire())
        try:
            with mock.patch.object(sshsig.subprocess, "Popen",
                                   side_effect=AssertionError("must not fork")):
                self.assertEqual(
                    verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                    make_entry(), signers),
                    Outcome.UNAVAILABLE,
                )
        finally:
            signers.release()


# ---------------------------------------------------------------------------
# C-506 啟動自測
# ---------------------------------------------------------------------------


class SelfTestTests(TempDirCase):
    def _copy_binaries(self) -> tuple[str, str]:
        keygen = shutil.copy2(SSH_KEYGEN, os.path.join(self.bin, "ssh-keygen"))
        ssh = shutil.copy2("/usr/bin/ssh", os.path.join(self.bin, "ssh"))
        return keygen, ssh

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c506_selftest_passes_with_real_tools(self) -> None:
        verifier = self.real_verifier(owner_uid=0)
        self.assertTrue(verifier.selftest())
        self.assertIs(verifier.healthy, True)
        self.assertEqual(os.listdir(self.spool), [])

    def test_c506_selftest_fails_on_old_version(self) -> None:
        if not os.path.exists(SSH_KEYGEN):
            self.skipTest("no ssh-keygen")
        keygen, ssh = self._copy_binaries()
        fake_ssh(ssh, "OpenSSH_9.5p1, OpenSSL fake")
        verifier = Verifier(sshkeygen_path=keygen, prlimit_path=REAL_PRLIMIT,
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid())
        self.assertFalse(verifier.selftest())

    def test_c506_unknown_version_uses_selftest(self) -> None:
        if not os.path.exists(SSH_KEYGEN):
            self.skipTest("no ssh-keygen")
        keygen, ssh = self._copy_binaries()
        fake_ssh(ssh, "not an openssh version")
        verifier = Verifier(sshkeygen_path=keygen, prlimit_path=REAL_PRLIMIT,
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid())
        self.assertIsNone(verifier.openssh_version())
        self.assertTrue(verifier.selftest())
        self.assertTrue(verifier.version_unknown)

    def test_c506_selftest_fails_when_binary_missing(self) -> None:
        verifier = Verifier(sshkeygen_path=os.path.join(self.bin, "absent"),
                            prlimit_path=REAL_PRLIMIT, spool_dir=self.spool,
                            home_dir=self.spool, owner_uid=os.geteuid())
        self.assertFalse(verifier.selftest())

    def test_c506_selftest_fails_on_world_writable_binary(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"), stdout=b"")
        os.chmod(keygen, 0o777)
        fake_ssh(os.path.join(self.bin, "ssh"), "OpenSSH_9.6p1")
        verifier = Verifier(sshkeygen_path=keygen, prlimit_path=REAL_PRLIMIT,
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid())
        self.assertFalse(verifier.selftest())

    def test_c506_selftest_fails_on_owner_mismatch(self) -> None:
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"), stdout=b"")
        fake_ssh(os.path.join(self.bin, "ssh"), "OpenSSH_9.6p1")
        verifier = Verifier(sshkeygen_path=keygen, prlimit_path=REAL_PRLIMIT,
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid() + 12345)
        self.assertFalse(verifier.selftest())

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c506_recheck_reruns_only_after_change(self) -> None:
        if not os.path.exists(SSH_KEYGEN):
            self.skipTest("no ssh-keygen")
        keygen, _ssh = self._copy_binaries()
        verifier = Verifier(sshkeygen_path=keygen, prlimit_path=REAL_PRLIMIT,
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid())
        self.assertTrue(verifier.selftest())
        with mock.patch.object(verifier, "selftest", wraps=verifier.selftest) as spy:
            self.assertTrue(verifier.recheck_if_changed())
            spy.assert_not_called()
            current = os.stat(keygen)
            os.utime(keygen, ns=(current.st_atime_ns, current.st_mtime_ns + 10 ** 9))
            verifier.recheck_if_changed()
            spy.assert_called_once()

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c506_openssh_version_reads_sibling_ssh(self) -> None:
        verifier = self.real_verifier(owner_uid=0)
        version = verifier.openssh_version()
        self.assertIsNotNone(version)
        self.assertGreaterEqual(version, (9, 6))

    def test_c506_selftest_does_not_clean_spool(self) -> None:
        # 中-1(a)：自測不清 spool；在途請求的 sig-*.asc 與別人的檔都要留著。
        keygen = fake_keygen(os.path.join(self.bin, "ssh-keygen"),
                             stdout=(good_line(make_entry()) + "\n").encode())
        verifier = self.fake_verifier(keygen)
        leftovers = (Path(self.spool, "sig-inflight.asc"),
                     Path(self.spool, "unrelated.dat"))
        for path in leftovers:
            path.write_text("x", encoding="ascii")
        with mock.patch.object(verifier, "clean_spool",
                               side_effect=AssertionError("selftest must not clean")):
            with mock.patch.object(verifier, "_run_selftest_cases", return_value=True):
                self.assertTrue(verifier.selftest())
        for path in leftovers:
            self.assertTrue(path.exists(), path)

    def test_c506_recheck_selftest_does_not_disturb_inflight_verify(self) -> None:
        # 中-1：recheck 觸發自測時，在途 verify 的簽章檔還在、結果仍是 PASS。
        entry = make_entry()
        record = os.path.join(self.tmp, "sig-state")
        keygen = sig_checking_keygen(
            os.path.join(self.bin, "ssh-keygen"),
            (good_line(entry) + "\n").encode(), record, 0.5)
        fake_ssh(os.path.join(self.bin, "ssh"), "OpenSSH_9.6p1")
        verifier = self.fake_verifier(keygen)
        signers = self.signers(1, [entry])
        results: list[Outcome] = []
        # 中-4：呼叫端在取快照時 acquire，請求結束才 release。
        self.assertTrue(signers.acquire())
        worker = threading.Thread(target=lambda: results.append(
            verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                            entry, signers)))
        worker.start()
        time.sleep(0.2)
        # 假裝 ssh-keygen 的 mtime 變了 → 重跑自測（自測不得清 spool）。
        with mock.patch.object(verifier, "_run_selftest_cases", return_value=True):
            verifier._binary_id = None
            self.assertTrue(verifier.recheck_if_changed())
        worker.join(5)
        self.assertFalse(worker.is_alive())
        signers.release()
        self.assertEqual(results, [Outcome.PASS])
        self.assertEqual(Path(record).read_text(encoding="ascii"), "exists")
        self.assertEqual(os.listdir(self.spool), [])

    def test_c506_version_unknown_resets_after_successful_probe(self) -> None:
        # 低-4：一次讀不到版本不會讓 version_unknown 永遠為真。
        if not os.path.exists(SSH_KEYGEN):
            self.skipTest("no ssh-keygen")
        keygen, ssh = self._copy_binaries()
        fake_ssh(ssh, "not an openssh version")
        verifier = Verifier(sshkeygen_path=keygen, prlimit_path=REAL_PRLIMIT,
                            spool_dir=self.spool, home_dir=self.spool,
                            owner_uid=os.geteuid())
        self.assertTrue(verifier._version_ok())
        self.assertTrue(verifier.version_unknown)
        fake_ssh(ssh, "OpenSSH_9.6p1")
        self.assertTrue(verifier._version_ok())
        self.assertFalse(verifier.version_unknown)


# ---------------------------------------------------------------------------
# C-507 固定向量
# ---------------------------------------------------------------------------


class VectorTests(TempDirCase):
    def test_c507_vector_file_matches_module_constant(self) -> None:
        self.assertEqual(VECTOR, sshsig.SELFTEST_VECTOR)
        self.assertEqual(VECTOR["schema"], 1)
        self.assertEqual(len(VECTOR["signature"]), 240)
        self.assertEqual(len(VECTOR["bad_signature"]), 240)

    def test_c507_vector_has_no_private_key(self) -> None:
        text = VECTOR_PATH.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE KEY", text)
        self.assertNotIn("BEGIN OPENSSH", text)
        self.assertNotIn("-----", text)

    def test_c507_bad_signature_differs_only_in_r_bit(self) -> None:
        good, bad = vector_blob(), bad_vector_blob()
        self.assertEqual(len(good), len(bad))
        differing = [i for i, (a, b) in enumerate(zip(good, bad)) if a != b]
        self.assertEqual(len(differing), 1)
        self.assertLess(differing[0], len(good) - 32)  # 在 R 內，S 不動

    def test_c507_vector_blob_passes_preparse(self) -> None:
        entry = make_entry()
        self.assertEqual(preparse(VECTOR["signature"], entry), vector_blob())
        self.assertEqual(preparse(VECTOR["bad_signature"], entry), bad_vector_blob())

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c507_real_ssh_keygen_passes_vector(self) -> None:
        entry = make_entry()
        signers = self.signers(1, [entry])
        verifier = self.real_verifier(owner_uid=0)
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                entry, signers),
                Outcome.PASS,
            )
        finally:
            signers.release()

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c507_real_ssh_keygen_bad_signature_is_401(self) -> None:
        entry = make_entry()
        signers = self.signers(1, [entry])
        verifier = self.real_verifier(owner_uid=0)
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), bad_vector_blob(),
                                entry, signers),
                Outcome.BAD_SIG,
            )
        finally:
            signers.release()

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c507_real_missing_principal_is_unavailable(self) -> None:
        # 走真的子行程：allowed_signers 不含這個 key ID → exit 255 空 stderr → 503。
        # 快照聲稱 entry 在檔裡（否則低-1 的檢查會先擋掉、不 fork），檔案實際只寫
        # 另一個 key ID，構造出「principal 不在」的真實輸出。
        entry = make_entry()
        other = KeyEntry(key_id="someone-else", principal="bob",
                         public_key=entry.public_key, status="active")
        path = os.path.join(self.spool, "allowed_signers.2")
        with open(path, "w", encoding="ascii") as handle:
            handle.write(sshsig._format_signers_line(other) + "\n")
        signers = SignersFile(path, 2, sshsig._stat_id(path), (entry,))
        verifier = self.real_verifier(owner_uid=0)
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(VECTOR["signing_data"].encode(), vector_blob(),
                                entry, signers),
                Outcome.UNAVAILABLE,
            )
        finally:
            signers.release()

    @unittest.skipUnless(REAL_SSH_OK, "needs OpenSSH >= 9.6")
    def test_c507_real_mutated_message_is_401(self) -> None:
        entry = make_entry()
        signers = self.signers(1, [entry])
        verifier = self.real_verifier(owner_uid=0)
        message = VECTOR["signing_data"].encode() + b"\n"
        self.assertTrue(signers.acquire())
        try:
            self.assertEqual(
                verifier.verify(message, vector_blob(), entry, signers),
                Outcome.BAD_SIG,
            )
        finally:
            signers.release()


class SharedFingerprintTests(unittest.TestCase):
    """中-3：公鑰 blob 與 fingerprint 只有 `lgwd.auth_registry` 那一份。"""

    def test_fingerprint_is_the_auth_registry_implementation(self) -> None:
        entry = make_entry()
        self.assertEqual(
            public_key_blob(entry),
            auth_registry.ssh_public_key_blob(entry.public_key),
        )
        self.assertEqual(fingerprint(entry), auth_registry.fingerprint(entry.public_key))
        self.assertEqual(fingerprint(entry),
                         "aJNFOHQV7gykg5qlLvJszgg3JWG9Kf529FjlcpaG+OI")

    def test_sshsig_has_no_second_fingerprint_implementation(self) -> None:
        source = (REPO_ROOT / "lgwd" / "sshsig.py").read_text(encoding="utf-8")
        self.assertNotIn("hashlib", source)


# ---------------------------------------------------------------------------
# 只新增模組的結構檢查
# ---------------------------------------------------------------------------


class ModuleSurfaceTests(unittest.TestCase):
    def test_module_only_imports_stdlib_and_registry_types(self) -> None:
        source = (REPO_ROOT / "lgwd" / "sshsig.py").read_text(encoding="utf-8")
        for forbidden in ("import sqlite3", "import lgwd.http", "import lgwd.main",
                          "import lgwd.registry", "preexec_fn=", "shell=True"):
            self.assertNotIn(forbidden, source)
        for required in ("import subprocess", "import tempfile", "import threading"):
            self.assertIn(required, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
