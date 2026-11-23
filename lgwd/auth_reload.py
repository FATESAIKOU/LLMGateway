"""名冊背景 reload、單一決策函式與載入狀態檔（D2 PR-4；契約 C-401、C-403～C-406）。

design「名冊 reload」的實作：

- **唯一決策函式** `decide(file_result, trust_state)`：啟動的 `load_initial()` 與執行中的
  `tick()` 都只呼叫它（C-401）。重啟與執行中因此不可能有兩套判斷。
- **背景輪詢**（C-403）：每 `poll_interval` 秒 stat 名冊檔的 `(mtime_ns, size, ino)`，變了
  才讀；**請求路徑完全不觸發讀檔**（`snapshot` 只是回傳目前的不可變參照）。失敗時記下
  該 stamp，同一個 stamp 不重讀；只有暫時性錯誤（`io_error`／`trust_unavailable`）才以
  5 秒起、加倍、上限 300 秒退避，重試**同一個** stamp。
- **status 檔**（C-404）：`{"schema":1,"loaded_generation":…,"loaded_hash":…,
  "attempted_hash":…,"result":…,"heartbeat":…}`，一行 JSON、0644、同目錄暫存＋fsync＋
  rename 原子寫；只在「已載入世代／雜湊、最近嘗試的雜湊、結果」改變時寫，心跳每 60 秒
  至多一次。`UNCHANGED` 而目前停在錯誤時要改回 ok（回滾壞名冊是最常見的修復動作）。
  兩種寫入都只由載入路徑（啟動的 `load_initial` 或背景 `tick`）觸發，與請求量無關。
  **心跳只代表背景執行緒還活著，與結果無關**：`tick()` 的每一條返回路徑（壞檔留在原地
  等 stamp 變、暫時性錯誤退避等待中、無變化）都會先推進它，否則壞名冊留著時 D5 會誤判
  「輪詢執行緒死了」。
- **快照**（C-402／C-406）：信任庫的交易提交成功之後才換掉記憶體快照，再呼叫
  `on_snapshot(registry)`，**回呼完成之後才寫 status 檔**（D5 的 install 要看到「撤銷
  已經真正生效」才回 0）。**D2 範圍內，請求路徑唯一的快照來源是 PR-6 在 `on_snapshot`
  裡原子發布的那一組 `(registry, signers)`**（C-406 由 PR-6 那一組實現）；
  `RegistryWatcher.snapshot` 只用於觀測、status 與啟動判斷，**請求路徑不讀它**——換世代
  的瞬間兩者會短暫不同步（屬性已換新、PR-6 還在寫 allowed_signers），拿它查 key 會
  出現假 503。

這個模組只新增：`lgwd` 的執行路徑不 import 它。不認識 `sshsig`（allowed_signers 的重產
由 PR-6 掛在 `on_snapshot`／`extra_ticks` 上，見 REPORT-256-archreview2 §4）。只用標準
函式庫。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable

from lgwd.auth_db import StoreError
from lgwd.auth_paths import AuthPaths, now_seconds
from lgwd.auth_registry import (
    KIND_DUPLICATE,
    KIND_INSECURE_FILE,
    KIND_INVALID_KEY,
    KIND_IO_ERROR,
    KIND_PARSE_ERROR,
    KIND_TOO_LARGE,
    KIND_TOMBSTONE_VIOLATION,
    Registry,
    RegistryError,
    check_tombstones,
    content_hash,
    parse_registry,
    read_registry_file,
)
from lgwd.auth_trust import KIND_GENERATION_NOT_INCREASED, TrustReject, TrustStore
from lgwd.clock import Clock

# --- status 檔的分類白名單（C-405：這是給 D5 的介面，改動要經架構師） ---

RESULT_OK = "ok"
RESULT_PARSE_ERROR = KIND_PARSE_ERROR
RESULT_INVALID_KEY = KIND_INVALID_KEY
RESULT_DUPLICATE = KIND_DUPLICATE
RESULT_INSECURE_FILE = KIND_INSECURE_FILE
RESULT_TOO_LARGE = KIND_TOO_LARGE
RESULT_TOMBSTONE_VIOLATION = KIND_TOMBSTONE_VIOLATION
RESULT_GENERATION_NOT_INCREASED = KIND_GENERATION_NOT_INCREASED
RESULT_IO_ERROR = KIND_IO_ERROR
RESULT_TRUST_UNAVAILABLE = "trust_unavailable"

#: `result` 只能是這些值（C-405）。
RESULTS = frozenset({
    RESULT_OK,
    RESULT_PARSE_ERROR,
    RESULT_INVALID_KEY,
    RESULT_DUPLICATE,
    RESULT_INSECURE_FILE,
    RESULT_TOO_LARGE,
    RESULT_TOMBSTONE_VIOLATION,
    RESULT_GENERATION_NOT_INCREASED,
    RESULT_IO_ERROR,
    RESULT_TRUST_UNAVAILABLE,
})

#: 只有暫時性錯誤才退避重試（C-403：設計上是信任庫忙碌／I/O 與名冊檔讀取失敗）。
TEMPORARY_RESULTS = frozenset({RESULT_IO_ERROR, RESULT_TRUST_UNAVAILABLE})

# --- 決策的動作 ---

COMMIT_FIRST = "commit_first"   # 信任庫還沒有快照，這是第一份
COMMIT = "commit"               # 較高世代、墓碑過關
UNCHANGED = "unchanged"         # 同世代同雜湊＝無變更（不是錯誤）
REJECT = "reject"               # 整份拒絕，沿用記憶體快照
NO_REGISTRY = "no_registry"     # 檔案無效且信任庫也沒有快照（enforce 拒絕啟動）

STATUS_SCHEMA = 1
STATUS_MODE = 0o644
HEARTBEAT_SECONDS = 60
POLL_INTERVAL = 2.0
BACKOFF_INITIAL = 5.0
BACKOFF_MAX = 300.0

_NEVER = object()        # 從來沒有失敗過（`_failed_stamp` 的初始值）
_UNSTATABLE = object()   # 連 stat 都失敗（名冊檔不在或看不到）

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class Snapshot:
    """一份已驗證、不可變的記憶體快照（C-406）。

    `generation`／`active_keys()` 直接轉給 `registry`；`raw` 與 `content_hash` 就是信任庫
    存的那一份（`UNCHANGED` 時兩者相同，所以不會出現「記憶體快照與庫不同步」）。
    `repr` 不含 `raw`：名冊原始 bytes 裡有公鑰，不進日誌。
    """

    registry: Registry
    raw: bytes
    content_hash: str

    @property
    def generation(self) -> int:
        return self.registry.generation

    def active_keys(self):
        return self.registry.active_keys()

    def __repr__(self) -> str:  # 不含公鑰與 key ID
        return "<Snapshot generation={0} hash={1}>".format(self.generation, self.content_hash)


@dataclass(frozen=True)
class FileResult:
    """一次讀檔＋解析的結果（壞檔也有 `content_hash`，可以記進 status 的 `attempted_hash`）。

    - `error` 為 None＝檔案有效，`registry` 一定有值。
    - `error` 是固定分類（`parse_error`／`invalid_key`／`duplicate`／`insecure_file`／
      `too_large`／`io_error`）；讀不到檔時 `raw`／`content_hash` 為 None。
    """

    raw: bytes | None
    registry: Registry | None
    content_hash: str | None
    error: str | None

    def __post_init__(self) -> None:
        if self.error is None:
            if not isinstance(self.registry, Registry) or self.content_hash is None:
                raise ValueError("a valid file result needs a registry and a content hash")
        elif self.error not in RESULTS or self.error == RESULT_OK:
            raise ValueError("invalid file result classification")
        elif self.registry is not None:
            raise ValueError("an invalid file result cannot carry a registry")


@dataclass(frozen=True)
class ReloadDecision:
    """`decide` 的輸出：動作、要寫進 status 的 `result`、以及這份檔的世代與雜湊。"""

    action: str
    result: str
    content_hash: str | None = None
    generation: int | None = None

    @property
    def commits(self) -> bool:
        return self.action in (COMMIT_FIRST, COMMIT)


def decide(file_result: FileResult, trust_state) -> ReloadDecision:
    """唯一的載入決策（C-401）；`load_initial` 與 `tick` 都只呼叫這一個函式。

    判斷順序（design「名冊 reload」）：檔案無效 → 沿用快照並記分類；沒有快照且檔案有效 →
    第一份；同世代同雜湊 → 無變更；同世代異雜湊或世代較低 → 拒絕；較高世代 → 先驗墓碑，
    過了才換世代。

    「檔案與快照都無效」＝`NO_REGISTRY`（enforce 拒絕啟動、warn 告警放行）。沒有快照但
    信任庫已有墓碑時仍然驗墓碑（`rebuild-trust` 可能留下墓碑而快照寫入失敗；那時讓違反
    墓碑的名冊成為第一份快照等於撤銷失效）。
    """
    if not isinstance(file_result, FileResult):
        raise TypeError("file_result must be a FileResult")
    if trust_state is None or not hasattr(trust_state, "has_snapshot"):
        raise TypeError("trust_state must be an auth_trust.TrustState")
    has_snapshot = bool(trust_state.has_snapshot)
    if file_result.error is not None:
        action = NO_REGISTRY if not has_snapshot else REJECT
        return ReloadDecision(action, file_result.error, file_result.content_hash)
    registry = file_result.registry
    digest = file_result.content_hash
    generation = registry.generation
    if not has_snapshot:
        return _checked_commit(COMMIT_FIRST, registry, trust_state, digest)
    if generation == trust_state.generation and digest == trust_state.snapshot_hash:
        return ReloadDecision(UNCHANGED, RESULT_OK, digest, generation)
    if generation <= trust_state.generation:
        # 同世代但內容不同（例如把已撤銷 key 改回有效、或只改空白）與較低世代一樣拒絕。
        return ReloadDecision(REJECT, RESULT_GENERATION_NOT_INCREASED, digest, generation)
    return _checked_commit(COMMIT, registry, trust_state, digest)


def _checked_commit(action: str, registry: Registry, trust_state, digest: str) -> ReloadDecision:
    """較高世代（或第一份）先驗墓碑，過了才允許提交（C-401、design「撤銷與名冊損毀」）。"""
    try:
        check_tombstones(registry, trust_state.tombstones)
    except RegistryError:
        return ReloadDecision(
            REJECT, RESULT_TOMBSTONE_VIOLATION, digest, registry.generation
        )
    return ReloadDecision(action, RESULT_OK, digest, registry.generation)


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    """同目錄暫存 → fsync → chmod → rename；失敗不留暫存、不動原檔。"""
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def status_bytes(document: dict) -> bytes:
    """把 status 文件序列化成一行 JSON（UTF-8、結尾一個換行）；不含公鑰與 key ID。"""
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _read_registry(path: str, owner_uid: int) -> bytes:
    """預設的讀檔器（C-207 的檔案檢查在 `auth_registry.read_registry_file`）。

    注入的讀檔器簽名一樣是 `(path, owner_uid) -> bytes`，只該丟 `RegistryError`／`OSError`；
    測試用它模擬 I/O 故障並計數讀檔次數。
    """
    return read_registry_file(path, owner_uid=owner_uid)


class RegistryWatcher:
    """名冊檔的背景輪詢＋reload＋status 檔（C-403～C-406）。

    用法：建構子給 `AuthPaths`（名冊檔與狀態檔都由它推導）、已開啟的 `TrustStore` 與注入的
    `Clock`；啟動時先 `load_initial()`，之後 `start()` 讓背景執行緒每 `poll_interval` 秒
    `tick()`。`on_snapshot(registry)` 在換世代（信任庫交易已提交、記憶體快照已換）之後呼叫；
    `extra_ticks` 每個 tick 都跑一次（PR-6 掛 `ssh-keygen` 的 stat 檢查）。

    回呼在載入鎖內執行，且它的例外不會影響已提交的信任庫與記憶體快照；**回呼裡不要回頭呼叫
    `tick()`／`load_initial()`**（同一把鎖，會死鎖）。
    """

    def __init__(self, paths: AuthPaths, trust: TrustStore, clock: Clock, *,
                 on_snapshot: Callable[[Registry], None] | None = None,
                 extra_ticks: tuple[Callable[[], None], ...] | list[Callable[[], None]] = (),
                 poll_interval: float = POLL_INTERVAL,
                 owner_uid: int = 0,
                 heartbeat_seconds: int = HEARTBEAT_SECONDS,
                 backoff_initial: float = BACKOFF_INITIAL,
                 backoff_max: float = BACKOFF_MAX,
                 read_file: Callable[[str, int], bytes] | None = None):
        self.paths = paths
        self.registry_path = paths.registry
        self.status_path = paths.status
        self.trust = trust
        self.clock = clock
        self.on_snapshot = on_snapshot
        self.extra_ticks = list(extra_ticks)
        self.poll_interval = float(poll_interval)
        self.owner_uid = int(owner_uid)
        self.heartbeat_seconds = int(heartbeat_seconds)
        self.backoff_initial = float(backoff_initial)
        self.backoff_max = float(backoff_max)
        self._read_file = read_file if read_file is not None else _read_registry

        self._lock = threading.Lock()   # 載入鎖：同一時間只有一個載入在跑（design「名冊 reload」）
        self._snapshot: Snapshot | None = None
        self._loaded_stamp: tuple[int, int, int] | None = None
        self._failed_stamp: object = _NEVER
        self._retry_at: float | None = None
        self._backoff = 0.0
        self._last_status: dict | None = None
        self._last_heartbeat = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # 測試／觀測用計數（不影響正式路徑）。
        self.reads = 0            # 真的讀了幾次名冊檔
        self.loads = 0            # `load_initial` 被呼叫幾次
        self.ticks = 0            # `tick` 被呼叫幾次
        self.status_writes = 0    # status 檔真的寫了幾次

    # --- 對外狀態 ---

    @property
    def snapshot(self) -> Snapshot | None:
        """觀測用的不可變快照（觀測、status 與啟動判斷）；**請求路徑不讀它**。

        D2 範圍內，請求路徑唯一的快照來源是 PR-6 在 `on_snapshot` 裡原子發布的那一組
        `(registry, signers)`（C-406 由 PR-6 那一組實現）：換世代的瞬間，這個屬性已換成
        新世代、PR-6 那一組還在寫 allowed_signers，兩者會短暫不同步；拿這個屬性查 key
        會出現假 503，所以請求路徑不讀它。背景路徑（PR-6 在 `extra_ticks` 裡的重試）
        讀它不受此限。

        這裡**不**碰檔案、不取載入鎖、不推進任何 stamp：請求量再多也不會造成讀檔或寫檔
        （C-403「請求路徑不觸發讀檔」）。
        """
        return self._snapshot

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- 載入（啟動與執行中共用 decide） ---

    def load_initial(self) -> ReloadDecision:
        """啟動時的首次載入（C-401、design「名冊 reload」的「重啟」條）。

        先載入信任庫裡的已驗證快照（有的話就換成記憶體快照並呼叫 `on_snapshot`，讓 PR-6
        重產 allowed_signers），再讀名冊檔走**同一個** `decide`：同世代同雜湊＝就用它；
        同世代異雜湊或較低世代＝拒絕並沿用快照；較高世代＝驗墓碑後提交；檔案無效＝沿用
        快照。所以一份同世代、內容被原地改過的名冊檔，重啟後與執行中被拒的結果相同。
        """
        self.loads += 1
        with self._lock:
            now = now_seconds(self.clock)
            failure = self._adopt_trust_snapshot(now)
            if failure is not None:
                return failure
            try:
                stamp = self._stat_stamp()
            except OSError:
                return self._fail(_UNSTATABLE, RESULT_IO_ERROR, None, True, now)
            return self._attempt(stamp, now)

    def tick(self) -> ReloadDecision | None:
        """執行中的一次輪詢；回 None 代表「這個 stamp 沒變、沒有動作」（只可能寫心跳）。"""
        self.ticks += 1
        self._run_extra_ticks()
        with self._lock:
            now = now_seconds(self.clock)
            try:
                stamp = self._stat_stamp()
            except OSError:
                stamp = _UNSTATABLE
            if self._failed_stamp is not _NEVER and stamp == self._failed_stamp:
                # 同一個 stamp 不重讀；暫時性錯誤等退避到期才再看一次。等著的每一輪也要
                # 推進心跳（中-3）：壞名冊留在原地時，心跳代表執行緒還活著。
                if self._retry_at is None or float(now) < self._retry_at:
                    self._maybe_heartbeat(now)
                    return None
            if self._snapshot is not None and stamp == self._loaded_stamp:
                self._maybe_heartbeat(now)
                return None
            if stamp is _UNSTATABLE:
                return self._fail(_UNSTATABLE, RESULT_IO_ERROR, None, True, now)
            return self._attempt(stamp, now)

    # --- 背景執行緒 ---

    def start(self) -> None:
        if self.running or self.poll_interval <= 0:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auth-registry", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """叫停並等執行緒收乾淨（可以重複呼叫）；停止後心跳就不再前進。"""
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=timeout)

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # 背景執行緒不能因一次失敗就死掉
                _LOG.warning("auth registry reload failed: %s", type(exc).__name__)
            if self._stop.wait(self.poll_interval):
                return

    # --- 內部：一次載入 ---

    def _adopt_trust_snapshot(self, now: int) -> ReloadDecision | None:
        """啟動時先載入信任庫快照（沒有就回 None）。

        這一步是「重啟與執行中用同一套判斷」的前提：名冊檔壞掉時，服務要用信任庫裡最後
        一份已驗證快照繼續服務（design「重啟」）。信任庫讀不到，或它存的快照解析不合法
        （不該發生：只有驗過的名冊才會提交），都當 `trust_unavailable` 回失敗決策。
        """
        try:
            state = self.trust.state()
        except StoreError:
            return self._fail(_UNSTATABLE, RESULT_TRUST_UNAVAILABLE, None, True, now)
        if not state.has_snapshot:
            return None
        try:
            registry = parse_registry(state.snapshot_raw)
        except RegistryError:
            return self._fail(_UNSTATABLE, RESULT_TRUST_UNAVAILABLE, None, True, now)
        self._snapshot = Snapshot(registry, state.snapshot_raw, state.snapshot_hash)
        self._notify()
        return None

    def _stat_stamp(self) -> tuple[int, int, int]:
        info = os.stat(self.registry_path)
        return (int(info.st_mtime_ns), int(info.st_size), int(info.st_ino))

    def _run_extra_ticks(self) -> None:
        for callback in self.extra_ticks:
            try:
                callback()
            except Exception as exc:
                _LOG.warning("auth registry extra tick failed: %s", type(exc).__name__)

    def _attempt(self, stamp: tuple[int, int, int], now: int) -> ReloadDecision:
        self.reads += 1
        attempted = None
        try:
            raw = self._read_file(self.registry_path, self.owner_uid)
        except RegistryError as exc:
            # 檔案檢查（C-207）的分類：insecure_file／too_large／io_error。也組一個
            # `FileResult` 交給 `decide`，讓「沿用快照（REJECT）或沒有快照（NO_REGISTRY）」
            # 只有 `decide` 一處在判斷（低-1）。
            file_result = FileResult(raw=None, registry=None, content_hash=None, error=exc.kind)
        except OSError:
            file_result = FileResult(
                raw=None, registry=None, content_hash=None, error=RESULT_IO_ERROR
            )
        else:
            raw = bytes(raw)
            attempted = content_hash(raw)   # 壞檔也要有 attempted_hash（REPORT-D2a §4）
            try:
                registry = parse_registry(raw)
            except RegistryError as exc:
                file_result = FileResult(
                    raw=raw, registry=None, content_hash=attempted, error=exc.kind
                )
            else:
                file_result = FileResult(
                    raw=raw, registry=registry, content_hash=attempted, error=None
                )
        try:
            state = self.trust.state()
        except StoreError:
            # 唯一無法進 `decide` 的失敗（沒有 `trust_state`）：連「該不該沿用快照」都
            # 分不出來，所以留在 `_fail`。檔案層失敗與信任庫同時失敗時以信任庫為準。
            return self._fail(stamp, RESULT_TRUST_UNAVAILABLE, attempted, True, now)
        decision = decide(file_result, state)
        if decision.commits:
            try:
                self.trust.commit(file_result.registry, raw, attempted)
            except StoreError:
                return self._fail(stamp, RESULT_TRUST_UNAVAILABLE, attempted, True, now)
            except TrustReject as exc:
                # 世代沒有增加（例如別人先提交了同一代）：交易已 rollback，快照不變。
                return self._fail(stamp, exc.kind, attempted, False, now)
            # 交易提交成功 → 才換記憶體快照 → 才呼叫 on_snapshot → 才寫 status（C-402；
            # 中-2：status 要在回呼完成之後才寫，D5 的 install 才不會在撤銷真正生效前回 0）。
            self._snapshot = Snapshot(file_result.registry, raw, attempted)
            self._succeed(stamp)
            self._notify()
            self._publish(RESULT_OK, attempted, now)
            return decision
        if decision.action is UNCHANGED:
            self._succeed(stamp)
            # 檔案與信任庫快照一致，但記憶體那一份不是它（例如上一次提交成功、讀回狀態
            # 卻失敗）：補上並讓 PR-6 重產 allowed_signers，否則驗章會用舊的鍵集。
            # 順序同提交路徑：換 `_snapshot` → 回呼 → 才寫 status（中-2）。
            repaired = self._snapshot is None or self._snapshot.content_hash != attempted
            if repaired:
                self._snapshot = Snapshot(file_result.registry, raw, attempted)
                self._notify()
            self._publish(RESULT_OK, attempted, now)
            return decision
        return self._fail(stamp, decision.result, attempted,
                          decision.result in TEMPORARY_RESULTS, now)

    def _succeed(self, stamp: tuple[int, int, int]) -> None:
        self._loaded_stamp = stamp
        self._failed_stamp = _NEVER
        self._retry_at = None
        self._backoff = 0.0

    def _fail(self, stamp: object, result: str, attempted: str | None,
              temporary: bool, now: int) -> ReloadDecision:
        """記下失敗的 stamp（同一個 stamp 不重讀）並寫 status；暫時性錯誤排退避重試。

        檔案層的失敗（`insecure_file`／`too_large`／`io_error`）與名冊內容的拒絕都先交給
        `decide`，這裡只負責記憶體狀態與 status；唯一沒有 `FileResult`／`TrustState` 可走
        `decide` 的是「信任庫讀不到」（以及連 stat 都失敗）——那時連「該不該沿用快照」都
        分不出來（低-1）。回傳的決策因此是近似的（沿用快照＝`REJECT`；連記憶體快照都沒有
        ＝`NO_REGISTRY`）。呼叫端判斷「能不能服務」一律看 `watcher.snapshot`，不要只看
        這個動作。
        """
        self._failed_stamp = stamp
        if temporary:
            if self._backoff <= 0:
                self._backoff = self.backoff_initial
            else:
                self._backoff = min(self._backoff * 2, self.backoff_max)
            self._retry_at = float(now) + self._backoff
        else:
            self._backoff = 0.0
            self._retry_at = None
        self._publish(result, attempted, now)
        action = NO_REGISTRY if (self._snapshot is None and result != RESULT_OK) else REJECT
        return ReloadDecision(action, result, attempted)

    def _notify(self) -> None:
        callback = self.on_snapshot
        snapshot = self._snapshot
        if callback is None or snapshot is None:
            return
        try:
            callback(snapshot.registry)
        except Exception as exc:  # 回呼失敗不改已提交的信任庫與快照
            _LOG.warning("auth registry on_snapshot failed: %s", type(exc).__name__)

    # --- status 檔（C-404） ---

    def _publish(self, result: str, attempted: str | None, now: int) -> None:
        snapshot = self._snapshot
        document = {
            "schema": STATUS_SCHEMA,
            "loaded_generation": None if snapshot is None else snapshot.generation,
            "loaded_hash": None if snapshot is None else snapshot.content_hash,
            "attempted_hash": attempted,
            "result": result,
        }
        if self._last_status is not None and all(
            self._last_status.get(key) == value for key, value in document.items()
        ):
            self._maybe_heartbeat(now)   # 狀態沒變：只可能有心跳
            return
        self._write_status(document, now)

    def _maybe_heartbeat(self, now: int) -> None:
        """心跳至多每 `heartbeat_seconds` 秒寫一次；只換 `heartbeat`，其他欄位不變。

        心跳代表「背景執行緒還活著」，與載入結果無關，所以每一條 tick 返回路徑都會呼叫
        它（中-3）。還沒有寫過任何 status 時不寫（啟動的第一份由 `_publish` 寫）。
        """
        if self._last_status is None:
            return
        if float(now) - float(self._last_heartbeat) >= self.heartbeat_seconds:
            self._write_status(self._last_status, now)

    def _write_status(self, document: dict, now: int) -> None:
        payload = dict(document)
        payload["heartbeat"] = int(now)
        try:
            _atomic_write(self.status_path, status_bytes(payload), STATUS_MODE)
        except OSError as exc:
            _LOG.warning("auth registry status write failed: %s", type(exc).__name__)
            return
        self._last_status = document
        self._last_heartbeat = int(now)
        self.status_writes += 1


__all__ = [
    "BACKOFF_INITIAL",
    "BACKOFF_MAX",
    "COMMIT",
    "COMMIT_FIRST",
    "FileResult",
    "HEARTBEAT_SECONDS",
    "NO_REGISTRY",
    "POLL_INTERVAL",
    "REJECT",
    "RESULTS",
    "RESULT_DUPLICATE",
    "RESULT_GENERATION_NOT_INCREASED",
    "RESULT_INSECURE_FILE",
    "RESULT_INVALID_KEY",
    "RESULT_IO_ERROR",
    "RESULT_OK",
    "RESULT_PARSE_ERROR",
    "RESULT_TOMBSTONE_VIOLATION",
    "RESULT_TOO_LARGE",
    "RESULT_TRUST_UNAVAILABLE",
    "RegistryWatcher",
    "ReloadDecision",
    "Snapshot",
    "STATUS_MODE",
    "STATUS_SCHEMA",
    "TEMPORARY_RESULTS",
    "UNCHANGED",
    "decide",
    "status_bytes",
]
