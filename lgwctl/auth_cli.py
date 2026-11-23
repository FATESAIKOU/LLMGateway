"""名冊與防重放的管理指令（#270／#222 D5b；契約 C-810～C-817）。

`llmgwctl auth-registry check|install|status` 與 `llmgwctl auth-replay status`。
介面與結束碼照 REPORT-D45-arch §2／§4：0＝成功；1＝執行了但結果不合格；2＝無法
執行（非 root、鎖被持有、讀不到檔、參數錯）。

界線（照 REPORT-D45-arch 與 BRIEF-270 的安全邊界）：

- **驗證只有一份**：候選名冊一律走 `lgwd.auth_registry.parse_registry`（公鑰格式、
  小階點、唯一性）與 `check_tombstones`；fingerprint 只用
  `lgwd.auth_registry.fingerprint`。這裡不另寫第二份驗證。
- **不取 `auth-store.lock`**：install 在 lgwd 執行中必須可用；它只在自己的
  `<state>/auth-registry-install.lock` 上序列化（`LOCK_EX|LOCK_NB`，拿不到回 2）。
- **唯讀**：`check`／`status`／`auth-replay status` 不寫任何檔；開庫一律走
  `lgwd.auth_db.open_ro`（三種檔案狀態的唯一一份規則：沒有 `-wal` 用
  `mode=ro&immutable=1`；`-wal`／`-shm` 都在用 `mode=ro`；有 `-wal` 沒 `-shm`
  回 2，不開庫、不建檔）。
- **install 原子寫入**：同目錄暫存＋`fsync`＋`rename`＋目錄 `fsync`；正式機為
  root:<服務帳號群組> 0640，prefix（測試沙箱）不做 chown。
- **審計只有一份格式**：install 用 `lgwd.auth_admin.AuditLog`（與 D5a／D5c 相同：
  `args`／`old`／`new` 物件、去敏白名單），錯誤照 `AdminError.exit_code` 回傳。
- **install 的載入確認**只讀 D2 的 status 檔（C-404／C-405）：`attempted_hash` 與
  `loaded_hash` 都等於自己寫入的內容雜湊且 `result==ok` 才回 0；`attempted_hash`
  是自己的雜湊但結果錯時立即回 1；心跳超過 180 秒沒前進、或逾時，都回 1 且不宣告
  撤銷完成。舊的錯誤分類（`attempted_hash` 不是自己這份）不會讓 install 誤回 1。
- **輸出不含公鑰**。`auth-registry status` 也不含 key ID；`auth-replay status` 依
  design :289 逐 key 列 `H`，因此會印 key ID（工單「輸出不含 key ID」與 design
  「列出各 key 的 H」衝突，已列入報告的待決）。
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys
import tempfile
import time

from lgwd.auth_admin import AdminError, AuditLog
from lgwd.auth_db import (
    LockBusy,
    LockUnavailable,
    StoreBusy,
    StoreError,
    StoreMissing,
    StoreUnavailable,
    StoreUnclean,
    acquire_store_lock,
    open_ro,
)
from lgwd.auth_paths import AuthPaths, auth_paths, now_seconds
from lgwd.auth_registry import (
    KIND_INSECURE_FILE,
    KIND_TOO_LARGE,
    MAX_REGISTRY_BYTES,
    Registry,
    RegistryError,
    check_tombstones,
    content_hash,
    fingerprint,
    parse_registry,
    read_registry_file,
)
from lgwd.auth_reload import RESULTS, RESULT_INSECURE_FILE, RESULT_OK, STATUS_SCHEMA
from lgwd.auth_replay import ReplayStore, WINDOW_SECONDS
from lgwd.auth_trust import TrustState, TrustStore
from lgwd.clock import SystemClock

from .layout import parse_env_file

#: status 檔的心跳上限（design :275；超過就是背景輪詢執行緒沒在跑）。
HEARTBEAT_STALE_SECONDS = 180
#: status 檔是固定的一行 JSON，正常只有幾百 bytes；讀取上限防止被塞大檔。
STATUS_MAX_BYTES = 64 * 1024
#: install 輪詢 status 檔的間隔（設計是每 0.5 秒一次；逾時很短時跟著縮小）。
POLL_SECONDS = 0.5
#: `insecure_file` 要採信時，心跳必須比 install 起跑時間新這麼多秒（`#270` 低-1）。
#: status 檔每次心跳都以**舊內容**重寫，比一輪 2 秒的輪詢長一點才不會拿前一版的
#: 狀態誤回 1（方向安全但訊息誤導）。
INSECURE_SKEW_SECONDS = 3
#: install 自己的鎖檔（不取 `auth-store.lock`；design §④）。
INSTALL_LOCK_NAME = "auth-registry-install.lock"
#: 名冊檔的權限（design :264：root:llmgw 0640）。
REGISTRY_MODE = 0o640

_STATUS_KEYS = frozenset({
    "schema", "loaded_generation", "loaded_hash", "attempted_hash", "result", "heartbeat",
})


class _Unreadable(Exception):
    """讀不到候選檔（結束碼 2）。`reason` 是給人看的固定說法。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Invalid(Exception):
    """候選檔讀到了但不合格（結束碼 1）。`kind` 用 C-405 的同一組字串。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class _StatusMissing(Exception):
    """status 檔不存在。"""


class _StatusBad(Exception):
    """status 檔讀不懂（不是合法的一行 JSON／欄位不合）。"""


class _TrustBad(Exception):
    """信任庫讀得到，但裡面的快照解析不合法（不該發生；指向 rebuild-trust）。"""


class StatusDocument:
    """`auth_registry.status` 檔的一份已驗證內容（C-404／C-405）。"""

    __slots__ = ("loaded_generation", "loaded_hash", "attempted_hash", "result", "heartbeat")

    def __init__(self, loaded_generation, loaded_hash, attempted_hash, result, heartbeat):
        self.loaded_generation = loaded_generation
        self.loaded_hash = loaded_hash
        self.attempted_hash = attempted_hash
        self.result = result
        self.heartbeat = heartbeat


# --- 路徑與環境 -------------------------------------------------------------


def paths_for(layout) -> AuthPaths:
    """由 `llmgateway.env` 的 `LGWD_AUTH_*` 推導路徑（與 lgwd 同一份來源）。

    優先序：行程環境變數 → `llmgateway.env` → 預設（prefix 底下的
    `/var/lib/llmgateway` 與 `/etc/llmgateway/auth-registry.json`）。沒有另開
    路徑旗標（REPORT-D45-arch §2 共通規則）。
    """
    values = dict(layout.env)
    try:
        file_values = parse_env_file(layout.env_file)
    except OSError:
        file_values = {}
    state_dir = values.get("LGWD_AUTH_STATE_DIR") or file_values.get("LGWD_AUTH_STATE_DIR")
    registry = values.get("LGWD_AUTH_REGISTRY") or file_values.get("LGWD_AUTH_REGISTRY")
    return auth_paths(
        state_dir or layout.state_dir,
        registry=registry or os.path.join(layout.etc_dir, "auth-registry.json"),
    )


def _resolve_paths(layout) -> AuthPaths | None:
    try:
        return paths_for(layout)
    except ValueError as exc:
        print("設定錯誤：{0}".format(exc), file=sys.stderr)
        return None


def _is_root(layout) -> bool:
    """auth 指令的 root 判斷**不看 prefix**：測試要能驗非 root 被拒。"""
    return int(layout.euid) == 0


def _refuse_non_root(command: str) -> int:
    print("{0} 需要 root（sudo llmgwctl {0}）".format(command), file=sys.stderr)
    return 2


def _service_ids(layout) -> tuple[int, int]:
    info = pwd.getpwnam(layout.service_user)
    return info.pw_uid, info.pw_gid


# --- 候選名冊 ---------------------------------------------------------------


class Candidate:
    """讀進來並解析成功的候選名冊（`raw`／`digest` 就是 install 要寫的那一份）。"""

    __slots__ = ("raw", "registry", "digest", "warnings")

    def __init__(self, raw: bytes, registry: Registry, digest: str, warnings: list[str]):
        self.raw = raw
        self.registry = registry
        self.digest = digest
        self.warnings = warnings


def _plain_read(path: str) -> bytes:
    """候選檔的檔案權限不合時仍要讀：仍擋 symlink、非一般檔與超過上限。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (getattr(os, "ELOOP", 40), getattr(os, "EMLINK", 31)):
            raise _Invalid(KIND_INSECURE_FILE) from None
        raise _Unreadable("讀不到名冊檔（{0}）".format(type(exc).__name__)) from None
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise _Unreadable("讀不到名冊檔（fstat 失敗）") from None
        if not stat.S_ISREG(info.st_mode):
            raise _Invalid(KIND_INSECURE_FILE)
        if info.st_size > MAX_REGISTRY_BYTES:
            raise _Invalid(KIND_TOO_LARGE)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REGISTRY_BYTES:
                raise _Invalid(KIND_TOO_LARGE)
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise _Unreadable("讀不到名冊檔（I/O 失敗）") from None
    finally:
        os.close(fd)


def read_candidate(path: str) -> Candidate:
    """讀候選檔並用 D2 的同一個驗證函式解析（C-810）。

    檔案權限（`read_registry_file` 的規則）只是**警示**：候選檔常在操作者的暫存
    位置，install 會以正確的擁有者與 0640 寫入。內容不合格才是結束碼 1。
    """
    warnings: list[str] = []
    try:
        raw = read_registry_file(path, owner_uid=0)
    except RegistryError as exc:
        if exc.kind == KIND_INSECURE_FILE:
            raw = _plain_read(path)
            warnings.append("名冊檔權限不符（應為 root 擁有、群組與其他人不可寫）；"
                            "install 會以 root:<服務群組> 0640 寫入")
        elif exc.kind == KIND_TOO_LARGE:
            raise _Invalid(exc.kind) from None
        else:
            raise _Unreadable("讀不到名冊檔（{0}）".format(exc.kind)) from None
    try:
        registry = parse_registry(raw)
    except RegistryError as exc:
        raise _Invalid(exc.kind) from None
    return Candidate(bytes(raw), registry, content_hash(raw), warnings)


# --- 信任庫（唯讀） ---------------------------------------------------------


def read_trust_state(paths: AuthPaths) -> TrustState:
    """唯讀讀信任庫的世代水位、墓碑與快照（經 `auth_db.open_ro`；C-811）。

    開庫的三種檔案狀態規則只有 `open_ro` 一份（沒有 `-wal` 走 `immutable=1`；
    `-wal`／`-shm` 都在走 `mode=ro`；有 `-wal` 沒 `-shm` 丟 `StoreUnclean`）。
    失敗照原樣丟 `StoreMissing`／`StoreUnavailable`／`StoreUnclean`／`StoreBusy`；
    快照解析不合法時丟 `_TrustBad`（指向 rebuild-trust）。
    """
    db = open_ro(paths.trust_db)
    try:
        store = TrustStore(db=db)
        state = store.state()
    finally:
        db.close()
    if state.has_snapshot:
        try:
            parse_registry(state.snapshot_raw)
        except RegistryError:
            raise _TrustBad() from None
    return state


def trust_problems(candidate: Candidate, state: TrustState) -> list[str]:
    """與信任庫比對（C-811）；回傳 C-405 的固定分類字串。"""
    problems: list[str] = []
    if state.has_snapshot:
        if (candidate.registry.generation == state.generation
                and candidate.digest == state.snapshot_hash):
            pass   # 同世代同內容：重裝，合格
        elif candidate.registry.generation <= state.generation:
            problems.append("generation_not_increased")
    if state.tombstones:
        try:
            check_tombstones(candidate.registry, state.tombstones)
        except RegistryError:
            problems.append("tombstone_violation")
    return problems


def _loaded_registry(state: TrustState) -> Registry | None:
    if not state.has_snapshot:
        return None
    return parse_registry(state.snapshot_raw)


def _diff(candidate: Candidate, loaded: Registry) -> tuple[list[str], list[str], list[str]]:
    """候選相對已載入版本的（新增、撤銷、移除）key ID（C-811 的變更摘要）。"""
    proposed = {key.key_id: key for key in candidate.registry.keys}
    current = {key.key_id: key for key in loaded.keys}
    added = [key_id for key_id in proposed if key_id not in current]
    removed = [key_id for key_id in current if key_id not in proposed]
    revoked = [
        key_id for key_id, key in proposed.items()
        if key_id in current and current[key_id].active and not key.active
    ]
    return added, revoked, removed


def _format_ids(ids: list[str]) -> str:
    return "、".join(ids) if ids else "（無）"


# --- check ------------------------------------------------------------------


def _print_candidate(candidate: Candidate) -> None:
    for warning in candidate.warnings:
        print("warn " + warning)
    print("generation {0}".format(candidate.registry.generation))
    print("content_hash {0}".format(candidate.digest))
    for key in candidate.registry.keys:
        print("key {0} principal={1} status={2} fingerprint=SHA256:{3}".format(
            key.key_id, key.principal, key.status, fingerprint(key.public_key)))


def run_check(layout, paths: AuthPaths, args) -> int:
    """`auth-registry check <檔> [--against-trust]`（C-810、C-811）。"""
    if args.against_trust and not _is_root(layout):
        print("auth-registry check --against-trust 需要 root（信任庫是 llmgw 0600）",
              file=sys.stderr)
        return 2
    try:
        candidate = read_candidate(args.file)
    except _Unreadable as exc:
        print(exc.reason, file=sys.stderr)
        return 2
    except _Invalid as exc:
        print("fail 名冊不合格（{0}）；未寫入".format(exc.kind))
        return 1
    _print_candidate(candidate)
    if not args.against_trust:
        print("result ok")
        return 0
    try:
        state = read_trust_state(paths)
    except StoreMissing:
        print("fail 信任庫不存在（先 llmgwctl auth-store init 或 "
              "auth-registry rebuild-trust）；不能驗世代與墓碑")
        return 1
    except _TrustBad:
        print("fail 信任庫快照讀不懂（先 llmgwctl auth-registry rebuild-trust）")
        return 1
    except StoreBusy:
        print("fail 信任庫忙碌，稍後再試", file=sys.stderr)
        return 2
    except StoreUnclean as exc:
        print("fail {0}".format(exc), file=sys.stderr)
        return 2
    except StoreUnavailable as exc:
        print("fail 信任庫讀不到（檢查權限／路徑；{0}）".format(exc.kind), file=sys.stderr)
        return 2
    print("loaded_generation {0}".format(
        state.generation if state.has_snapshot else "（無）"))
    print("loaded_hash {0}".format(state.snapshot_hash if state.has_snapshot else "（無）"))
    loaded = _loaded_registry(state)
    if loaded is not None:
        added, revoked, removed = _diff(candidate, loaded)
        print("added " + _format_ids(added))
        print("revoked " + _format_ids(revoked))
        print("removed " + _format_ids(removed))
    problems = trust_problems(candidate, state)
    if problems:
        print("fail 名冊不合格（{0}）".format("、".join(problems)))
        return 1
    print("result ok")
    return 0


# --- status 檔（讀） --------------------------------------------------------


def read_status(path: str) -> StatusDocument:
    """讀 D2 的 status 檔（C-404／C-405）；格式或分類不合丟 `_StatusBad`。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise _StatusMissing() from None
    except OSError:
        raise _StatusBad() from None
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > STATUS_MAX_BYTES:
                raise _StatusBad()
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError:
        raise _StatusBad() from None
    finally:
        os.close(fd)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _StatusBad() from None
    if not isinstance(document, dict) or set(document) != _STATUS_KEYS:
        raise _StatusBad()
    if document.get("schema") != STATUS_SCHEMA:
        raise _StatusBad()
    result = document.get("result")
    if not isinstance(result, str) or result not in RESULTS:
        raise _StatusBad()
    heartbeat = document.get("heartbeat")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, int):
        raise _StatusBad()
    loaded_generation = document.get("loaded_generation")
    if loaded_generation is not None and (
            isinstance(loaded_generation, bool) or not isinstance(loaded_generation, int)):
        raise _StatusBad()
    for name in ("loaded_hash", "attempted_hash"):
        value = document.get(name)
        if value is not None and not isinstance(value, str):
            raise _StatusBad()
    return StatusDocument(
        loaded_generation,
        document.get("loaded_hash"),
        document.get("attempted_hash"),
        result,
        heartbeat,
    )


def run_status(layout, paths: AuthPaths, args) -> int:
    """`auth-registry status`（C-814）：唯讀、不用 root、不取鎖、不建檔。"""
    try:
        document = read_status(paths.status)
    except _StatusMissing:
        print("status 檔不在（lgwd 沒跑過，或不是 warn／enforce）", file=sys.stderr)
        return 2
    except _StatusBad:
        print("status 檔讀不懂", file=sys.stderr)
        return 2
    now = int(time.time())
    age = max(0, now - document.heartbeat)
    print("loaded_generation {0}".format(
        "（無）" if document.loaded_generation is None else document.loaded_generation))
    print("loaded_hash {0}".format(
        "（無）" if document.loaded_hash is None else document.loaded_hash))
    print("attempted_hash {0}".format(
        "（無）" if document.attempted_hash is None else document.attempted_hash))
    print("result {0}".format(document.result))
    print("heartbeat_age_s {0}".format(age))
    if age > HEARTBEAT_STALE_SECONDS:
        print("fail lgwd 未在輪詢（心跳 {0} 秒前；off 或已停）".format(age))
        return 1
    if document.loaded_hash is None:
        print("fail 還沒有已載入的名冊")
        return 1
    if document.attempted_hash != document.loaded_hash:
        print("fail 最近嘗試的雜湊與已載入的不同（載入沒有生效）")
        return 1
    if document.result != RESULT_OK:
        print("fail 最近一次載入失敗（{0}）".format(document.result))
        return 1
    print("state ok")
    return 0


# --- install ----------------------------------------------------------------


def _atomic_write_registry(layout, path: str, raw: bytes) -> None:
    """同目錄暫存 → chmod/chown → 寫入 → fsync → rename → fsync 目錄（C-812）。"""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=directory)
    try:
        os.fchmod(fd, REGISTRY_MODE)
        if not layout.prefix and _is_root(layout):
            _, gid = _service_ids(layout)
            os.fchown(fd, 0, gid)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        tmp = None
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _chown_install_lock(layout, path: str) -> None:
    """install 鎖檔只給 root 用：正式機改成 `root:root` 0600（低-2）。

    擁有者不能是 `llmgw`：lgwd 被攻破時若能霸占這把鎖，就能擋住撤銷用的 install。
    模式由 `acquire_store_lock` 建成 0600；這裡只補擁有者。
    """
    if layout.prefix or not _is_root(layout):
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchown(fd, 0, 0)
    finally:
        os.close(fd)


def _wait_for_load(paths: AuthPaths, candidate: Candidate, t0: int, timeout: float) -> int:
    """輪詢 status 檔確認載入（C-813；REPORT-D45-arch §4 的五種結局）。"""
    deadline = time.monotonic() + max(0.0, timeout)
    interval = min(POLL_SECONDS, max(0.01, timeout / 10.0 if timeout > 0 else 0.01))
    saw_status = False
    last_result = None
    while True:
        now = int(time.time())
        try:
            document = read_status(paths.status)
        except (_StatusMissing, _StatusBad):
            document = None
        if document is not None:
            saw_status = True
            last_result = document.result
            age = max(0, now - document.heartbeat)
            if age > HEARTBEAT_STALE_SECONDS:
                print("fail lgwd 未在輪詢（off 或已停；心跳 {0} 秒前）；"
                      "名冊已寫入但未確認載入，不得宣告撤銷完成".format(age))
                return 1
            if (document.result == RESULT_INSECURE_FILE
                    and document.attempted_hash is None
                    and document.heartbeat >= t0 + INSECURE_SKEW_SECONDS):
                print("fail 名冊檔權限不符（insecure_file）；lgwd 拒絕載入，"
                      "名冊已寫入但未確認載入，不得宣告撤銷完成")
                return 1
            if document.attempted_hash == candidate.digest:
                if document.result == RESULT_OK and document.loaded_hash == candidate.digest:
                    print("已載入世代 {0}（{1}）".format(
                        candidate.registry.generation, candidate.digest))
                    return 0
                if document.result != RESULT_OK:
                    print("fail 載入失敗（{0}）；名冊已寫入但未生效，"
                          "不得宣告撤銷完成".format(document.result))
                    return 1
        if time.monotonic() >= deadline:
            if not saw_status:
                print("fail lgwd 未在輪詢（off 或已停）；名冊已寫入但未確認載入，"
                      "不得宣告撤銷完成")
            else:
                print("fail 名冊已寫入但服務尚未載入／載入失敗（{0}）；"
                      "不得宣告撤銷完成".format(last_result or "未載入"))
            return 1
        time.sleep(interval)


def run_install(layout, paths: AuthPaths, args) -> int:
    """`auth-registry install <檔>`（C-812、C-813）。"""
    if not _is_root(layout):
        return _refuse_non_root("auth-registry install")
    try:
        candidate = read_candidate(args.file)
    except _Unreadable as exc:
        print(exc.reason, file=sys.stderr)
        return 2
    except _Invalid as exc:
        print("fail 名冊不合格（{0}）；未寫入".format(exc.kind))
        return 1
    for warning in candidate.warnings:
        print("warn " + warning)
    try:
        state = read_trust_state(paths)
    except StoreMissing:
        print("fail 信任庫不存在（先 llmgwctl auth-store init 或 "
              "auth-registry rebuild-trust）；未寫入")
        return 1
    except _TrustBad:
        print("fail 信任庫快照讀不懂（先 llmgwctl auth-registry rebuild-trust）；未寫入")
        return 1
    except StoreBusy:
        print("fail 信任庫忙碌，稍後再試；未寫入", file=sys.stderr)
        return 2
    except StoreUnclean as exc:
        print("fail {0}；未寫入".format(exc), file=sys.stderr)
        return 2
    except StoreUnavailable as exc:
        print("fail 信任庫讀不到（檢查權限／路徑；{0}）；未寫入".format(exc.kind),
              file=sys.stderr)
        return 2
    problems = trust_problems(candidate, state)
    if problems:
        print("fail 名冊不合格（{0}）；未寫入".format("、".join(problems)))
        return 1
    try:
        lock = acquire_store_lock(os.path.join(paths.state_dir, INSTALL_LOCK_NAME))
    except LockBusy:
        print("fail 另一個 auth-registry install 正在進行；未寫入")
        return 2
    except LockUnavailable:
        print("fail install 鎖開不了（檢查狀態目錄權限）", file=sys.stderr)
        return 2
    try:
        try:
            _chown_install_lock(layout, lock.path)
        except OSError:
            pass   # 擁有者不影響本指令；失敗不擋（正式機會在下一次 install 再試）
        t0 = int(time.time())
        old = {
            "generation": state.generation if state.has_snapshot else None,
            "snapshot_hash": state.snapshot_hash if state.has_snapshot else None,
        }
        new = {
            "generation": candidate.registry.generation,
            "snapshot_hash": candidate.digest,
        }
        try:
            # 審計與 D5a／D5c 同一份格式（`auth_admin.AuditLog`）：一行 JSON、去敏白名單、
            # `old`／`new` 物件，不寫扁平欄位；先寫審計再動名冊。
            AuditLog(paths.audit_log, clock=SystemClock()).append(
                "auth-registry install",
                # 不記檔名（低-1）：路徑沒有歸責價值，內容雜湊已在 new.snapshot_hash；
                # 而 `AuditLog` 的白名單不收空白／非 ASCII，記檔名會拒絕合法名冊。
                args={"source": "file"},
                old=old,
                new=new,
            )
            _atomic_write_registry(layout, paths.registry, candidate.raw)
        except AdminError as exc:
            print("fail {0}".format(exc), file=sys.stderr)
            if exc.hint:
                print("hint {0}".format(exc.hint), file=sys.stderr)
            return exc.exit_code
        except OSError as exc:
            print("fail 寫入失敗（{0}）；名冊未寫入".format(type(exc).__name__),
                  file=sys.stderr)
            return 2
        if args.no_wait:
            print("已寫入世代 {0}（{1}）；未確認載入，不得宣告撤銷完成".format(
                candidate.registry.generation, candidate.digest))
            return 0
        return _wait_for_load(paths, candidate, t0, float(args.timeout))
    finally:
        lock.release()


# --- auth-replay status -----------------------------------------------------


def run_replay_status(layout, paths: AuthPaths, args) -> int:
    """`auth-replay status`（C-815）：root、經 `auth_db.open_ro`、不取 store 鎖。"""
    if not _is_root(layout):
        return _refuse_non_root("auth-replay status")
    clock = SystemClock()
    try:
        db = open_ro(paths.replay_db)
    except StoreMissing:
        print("replay 庫不存在（先 llmgwctl auth-replay recover-store）", file=sys.stderr)
        return 2
    except StoreBusy:
        print("replay 庫忙碌，稍後再試", file=sys.stderr)
        return 2
    except StoreUnclean as exc:
        print("fail {0}".format(exc), file=sys.stderr)
        return 2
    except StoreUnavailable as exc:
        print("replay 庫打不開（檢查權限／路徑；{0}）".format(exc.kind), file=sys.stderr)
        return 2
    try:
        store = ReplayStore(paths.replay_db, clock, db=db)
        now = now_seconds(clock)
        water = dict(store.water_rows())
        counts = store.nonce_counts()
    except StoreError as exc:
        print("replay 庫讀不到（{0}）".format(type(exc).__name__), file=sys.stderr)
        return 2
    finally:
        db.close()
    print("server_time {0}".format(now))
    future: list[str] = []
    for key_id in sorted(set(water) | set(counts)):
        h = water.get(key_id)
        print("key {0} h={1} nonces={2}".format(
            key_id, "-" if h is None else h, counts.get(key_id, 0)))
        if h is not None and h > now + WINDOW_SECONDS:
            future.append(key_id)
    if future:
        print("future " + "、".join(future))
        return 1
    print("result ok")
    return 0


# --- 分派 -------------------------------------------------------------------


def run_registry(layout, args) -> int:
    """`llmgwctl auth-registry <check|install|status>` 的入口。"""
    paths = _resolve_paths(layout)
    if paths is None:
        return 2
    command = args.auth_registry_command
    if command == "check":
        return run_check(layout, paths, args)
    if command == "install":
        return run_install(layout, paths, args)
    if command == "status":
        return run_status(layout, paths, args)
    raise AssertionError("unhandled auth-registry command: {0}".format(command))


def run_replay(layout, args) -> int:
    """`llmgwctl auth-replay <status>` 的入口。"""
    paths = _resolve_paths(layout)
    if paths is None:
        return 2
    command = args.auth_replay_command
    if command == "status":
        return run_replay_status(layout, paths, args)
    raise AssertionError("unhandled auth-replay command: {0}".format(command))


__all__ = [
    "HEARTBEAT_STALE_SECONDS",
    "INSTALL_LOCK_NAME",
    "Candidate",
    "StatusDocument",
    "paths_for",
    "read_candidate",
    "read_status",
    "read_trust_state",
    "run_check",
    "run_install",
    "run_registry",
    "run_replay",
    "run_replay_status",
    "run_status",
    "trust_problems",
]
