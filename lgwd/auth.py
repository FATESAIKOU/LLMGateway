"""認證 gate／apply／執行期狀態／日誌／啟動組裝（D2 PR-6a；C-601～C-611）。

這是唯一把 `auth_registry`、`auth_trust`、`auth_reload`、`auth_replay`、`sshsig` 串起來
的模組；依 design「D2 實作切分」的依賴方向，它不 import `lgwd.http`、`lgwd.main`、
`lgwd.registry`（`http`→`auth` 是單向）。

對外介面（REPORT-D2c-arch §4）：

- `AuthConfig`：一份執行期設定（模式、名冊路徑、狀態目錄、`ssh-keygen` 路徑、配額、
  驗章並發與期限）。預設值是 design「設定」的那一組。
- `StartupRefused(kind, store, message)`：啟動被認證理由拒絕；`kind` 是固定分類字串
  （6b 依它決定結束碼與訊息），`store` 是 `"trust"`／`"replay"`／None。訊息不含路徑以外
  的值（也沒有金鑰、簽章、nonce）。
- `open_runtime(cfg, clock, *, owner_uid=0) -> AuthRuntime`：**off 不得呼叫**（丟
  `ValueError`）。warn 下任何故障都降級回 runtime（只有 `LockBusy` 例外，兩種模式都拒絕）。
- `AuthRuntime`：持有鎖、兩庫、watcher、verifier、purge timer 與 `AuthGate`；
  `start()`／`stop()`（停止順序見 `stop` 的 docstring，API server 的 shutdown 由 6b 在
  呼叫 `stop()` 之前做）。
- `Verdict`／`Action`／`apply(mode, verdict)`：C-603 的模式對應只在一處；D3a 起
  `Verdict` 多帶驗章時那一份 `permissions`（C-905），並新增 `forbidden` 結果與三個
  授權 reason（C-906：enforce 403／warn 照舊／off 不呼叫）。
- `AuthGate.authenticate(...)`：C-602 的 gate 流程，回 `Verdict`。
- `AuthLog`：C-607 的 60 秒聚合；off 只在啟動記一行 `auth mode=off`（`http.make_api_server`）。

安全邊界（REPORT-D2c-arch §2）：

- 請求路徑**只讀** `AuthGate` 在 `on_snapshot` 裡原子發布的那一組 `(registry, signers)`，
  不讀 `watcher.snapshot`；取快照時在同一把 `state_lock` 內 `acquire()`，請求結束
  `release()`，換世代 retire 舊檔（在途請求歸零後才刪）。
- `on_snapshot` 失敗＝fail-closed：**任何**例外（不只 `VerifierUnavailable`）都把
  `signers_ok` 設為 False，之後所有 key 都 UNAVAILABLE。名冊已經提交，沿用舊的一組
  等於讓剛撤銷的 key 繼續有效。恢復由 `extra_ticks` 以 5→300 秒退避重試。
- enforce 的 401 body 對所有 reason 逐 byte 相同、不帶 `WWW-Authenticate`；warn 一律
  照舊處理業務並記 would-deny（`mode=warn outcome=deny`）；warn 的 PASS 照常 INSERT
  nonce（`accept` 在 gate 內，與 enforce 同一條路徑）。
- 日誌只記 `mode`、`outcome`、白名單 `reason`、路由模板、已驗證 principal 與聚合計數；
  不記公鑰、簽章、nonce、query、body、env 值、`Reject.kind`、`Outcome.value`、
  `StoreUnavailable.kind` 或 stderr 原文。

只用標準函式庫與本專案的認證模組。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from lgwd import sshsig
from lgwd.auth_db import (
    LockBusy,
    LockUnavailable,
    StoreBusy,
    StoreError,
    StoreMissing,
    acquire_store_lock,
    check_store_identity,
)
from lgwd.auth_authz import FORBIDDEN_AGENT, FORBIDDEN_OP, FORBIDDEN_REASONS, FORBIDDEN_ROLE
from lgwd.auth_busy import (
    BUSY_STATUS_SCHEMA,
    BusyCounter,
    BusyWindow,
    write_status,
)
from lgwd.auth_paths import AuthPaths, auth_paths, now_seconds
from lgwd.auth_registry import ID_PATTERN, PrincipalEntry, Registry
from lgwd.auth_reload import RegistryWatcher
from lgwd.auth_replay import ACCEPTED, DENIED, RATE_LIMITED, PurgeTimer, ReplayStore
from lgwd.auth_trust import TrustStore
from lgwd.clock import Clock
from lgwd.errors import (
    ApiError,
    auth_unavailable,
    forbidden as forbidden_error,
    rate_limited,
    unauthorized,
)
from lgwd.sshsig import Outcome, Reject, SignersFile

_LOG = logging.getLogger("lgwd.auth")

#: 三種模式（design「設定」；off 不建 runtime）。
MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_WARN, MODE_ENFORCE)

# --- 結果與原因（design :154 的白名單） ---

OUTCOME_PASS = "pass"
OUTCOME_DENY = "deny"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_UNAVAILABLE = "unavailable"
#: 已驗章（含 accept）但 principal 權限不足（D3a；設計 :152 的 403）。
OUTCOME_FORBIDDEN = "forbidden"
OUTCOMES = (OUTCOME_PASS, OUTCOME_DENY, OUTCOME_RATE_LIMITED, OUTCOME_UNAVAILABLE,
            OUTCOME_FORBIDDEN)

REASON_FRAMING = "framing"
REASON_MISSING = "missing"
REASON_MALFORMED = "malformed"
REASON_UNKNOWN_KEY = "unknown_key"
REASON_REVOKED = "revoked"
REASON_EXPIRED = "expired"
REASON_WATER = "water"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_REPLAY = "replay"
REASON_RATE_LIMITED = "rate_limited"
REASON_BUSY = "busy"
#: 防重放庫忙碌到期限（C-1000）：回應與 `store_unavailable` 逐 byte 相同，只在日誌與計數檔分開。
REASON_STORE_BUSY = "store_busy"
REASON_STORE_UNAVAILABLE = "store_unavailable"
REASON_VERIFIER_UNAVAILABLE = "verifier_unavailable"
REASON_REGISTRY_UNAVAILABLE = "registry_unavailable"
# 授權（D3a）：三個原因的常數在 `lgwd.auth_authz`（判斷的唯一來源），這裡只轉出同名
# 模組常數，讓 `auth.REASON_*` 的命名維持一致。
REASON_FORBIDDEN_OP = FORBIDDEN_OP
REASON_FORBIDDEN_AGENT = FORBIDDEN_AGENT
REASON_FORBIDDEN_ROLE = FORBIDDEN_ROLE

#: `reason` 只能是這些值（design :154）；`pass` 時是空字串。白名單外的值不得進日誌。
REASONS = frozenset({
    REASON_FRAMING,
    REASON_MISSING,
    REASON_MALFORMED,
    REASON_UNKNOWN_KEY,
    REASON_REVOKED,
    REASON_EXPIRED,
    REASON_WATER,
    REASON_BAD_SIGNATURE,
    REASON_REPLAY,
    REASON_RATE_LIMITED,
    REASON_BUSY,
    REASON_STORE_BUSY,
    REASON_STORE_UNAVAILABLE,
    REASON_VERIFIER_UNAVAILABLE,
    REASON_REGISTRY_UNAVAILABLE,
    REASON_FORBIDDEN_OP,
    REASON_FORBIDDEN_AGENT,
    REASON_FORBIDDEN_ROLE,
})

#: 三個授權原因（只屬於 `forbidden` 結果；`deny` 等結果不得使用）。
AUTHORIZATION_REASONS = frozenset(FORBIDDEN_REASONS)

#: 簽章資料的固定前綴與 audience（design「線上格式」）。
SIG_LINE_1 = "LGW-HTTP-SIG-V1"
AUDIENCE = sshsig.NAMESPACE

#: 五個 `X-LGW-*` 標頭（順序無關；每個恰一次）。
HEADER_VERSION = "X-LGW-Auth-Version"
HEADER_KEY_ID = "X-LGW-Key-Id"
HEADER_TIMESTAMP = "X-LGW-Timestamp"
HEADER_NONCE = "X-LGW-Nonce"
HEADER_SIGNATURE = "X-LGW-Signature"
HEADER_NAMES = (
    HEADER_VERSION,
    HEADER_KEY_ID,
    HEADER_TIMESTAMP,
    HEADER_NONCE,
    HEADER_SIGNATURE,
)

#: 交易外粗篩（design：只為早拒，不是最終判斷；最終判斷在 `accept` 的交易內）。
COARSE_WINDOW_SECONDS = 60

#: `X-LGW-Timestamp`：十進位 Unix 秒、無前導零。
_TIMESTAMP_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
#: `X-LGW-Nonce`：32 bytes 的無 padding base64url（固定 43 字元）。
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")

#: `on_snapshot` 失敗後重試 allowed_signers 的退避（REPORT-260-archreview §3 第 3 點）。
SIGNERS_BACKOFF_INITIAL = 5.0
SIGNERS_BACKOFF_MAX = 300.0

#: `.auth-busy.status` 的寫入間隔（monotonic 秒；C-1007）。寫失敗時下一輪 tick 直接補寫。
BUSY_STATUS_INTERVAL = 60.0

#: 觀測用：`_monotonic` 是可替換的單調時鐘（只給重試退避與測試；不影響請求判定）。
_monotonic: Callable[[], float] = time.monotonic


# ---------------------------------------------------------------------------
# 啟動拒絕
# ---------------------------------------------------------------------------

KIND_LOCK_BUSY = "lock_busy"
KIND_LOCK_UNAVAILABLE = "lock_unavailable"
KIND_STORE_MISSING = "store_missing"
KIND_STORE_UNAVAILABLE = "store_unavailable"
KIND_IDENTITY_MISMATCH = "identity_mismatch"
KIND_REGISTRY = "registry"
KIND_VERIFIER_BUILD = "verifier_build"
KIND_VERIFIER_SELFTEST = "verifier_selftest"


class StartupRefused(Exception):
    """啟動因認證理由被拒絕（6b 轉成結束碼 78）。

    `kind` 是固定分類字串；`store` 是 `"trust"`／`"replay"`／None；`message` 是給人看
    的一句話（指名哪一庫並指示 `rebuild-trust`／`recover-store` 或檢查權限）。三者都
    不含金鑰、簽章、nonce 或名冊內容。
    """

    def __init__(self, kind: str, store: str | None = None, message: str = ""):
        self.kind = kind
        self.store = store
        self.message = message or kind
        super().__init__(self.message)


def _missing_message(store: str) -> str:
    if store == "trust":
        return ("trust store is missing: rebuild the trust store with auth-registry "
                "rebuild-trust before starting")
    return ("replay store is missing: recover it with auth-replay recover-store "
            "before starting")


_STORE_UNAVAILABLE_MESSAGE = (
    "store cannot be opened: check ownership, permissions, path and disk space")


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    """執行期認證設定（design「設定」；CLI／環境變數的解析在 6b 的 `main.py`）。

    `busy_timeout`、`poll_interval` 與 `collect_lock_waits` 不在 design 的旗標表裡：前兩者
    是技術參數（與 PR-3／PR-4 的預設一致），第三者只給測試量「等鎖 p99」。三者的預設值
    就是正式值，6b 不需要開旗標。
    """

    mode: str = MODE_OFF
    registry_path: str = "/etc/llmgateway/auth-registry.json"
    state_dir: str = "/var/lib/llmgateway"
    sshkeygen_path: str = sshsig.DEFAULT_SSHKEYGEN
    per_key_max: int = 600
    verify_concurrency: int = sshsig.DEFAULT_CONCURRENCY
    verify_timeout: float = sshsig.DEFAULT_TIMEOUT
    busy_timeout: float = 5.0
    poll_interval: float = 2.0
    collect_lock_waits: bool = False

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("auth mode must be one of off/warn/enforce")
        for name in ("registry_path", "state_dir", "sshkeygen_path"):
            value = os.fspath(getattr(self, name))
            if not os.path.isabs(value):
                raise ValueError("{0} must be an absolute path".format(name))
        if isinstance(self.per_key_max, bool) or not isinstance(self.per_key_max, int):
            raise ValueError("per_key_max must be an integer")
        if self.per_key_max < 1:
            raise ValueError("per_key_max must be positive")
        if isinstance(self.verify_concurrency, bool) or not 1 <= int(self.verify_concurrency) <= 32:
            raise ValueError("verify_concurrency must be between 1 and 32")
        if float(self.verify_timeout) <= 0:
            raise ValueError("verify_timeout must be positive")
        if float(self.busy_timeout) <= 0:
            raise ValueError("busy_timeout must be positive")
        if float(self.poll_interval) < 0:
            raise ValueError("poll_interval must not be negative")


# ---------------------------------------------------------------------------
# 判定與動作
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """一次請求的認證／授權結論（C-602、C-906）。

    - `outcome`：`pass`／`deny`／`rate_limited`／`unavailable`／`forbidden`。
    - `reason`：design :154 的白名單值；`pass` 時是空字串。
    - `principal`：`pass` 與 `forbidden` 有值（都是驗章已通過的 principal；`forbidden`
      帶它是為了 warn 的 would-deny 日誌與 D3b 的稽核，不是自稱身分）。
    - `retry_after`：只在 `rate_limited` 有值（秒）。
    - `permissions`：只在 `pass` 有值；是驗章時那一份名冊快照的不可變
      `PrincipalEntry`（C-905）。`authorize` 只讀這個欄位，不再讀 watcher／AuthState。

    建構時就驗不變式：表外的 `reason`、`pass` 帶 reason、非 `rate_limited` 帶
    `retry_after`、`pass` 缺 `permissions`、`forbidden` 的原因不是三個授權原因、
    `forbidden` 缺 principal，或非 `pass` 帶 `permissions`，都丟 `ValueError`。這樣
    「`Reject.kind`／`Outcome.value` 直接漏進日誌」與「授權拿到別人的權限」在型別層
    就不可能發生。
    """

    outcome: str
    reason: str = ""
    principal: str | None = None
    retry_after: int = 0
    permissions: PrincipalEntry | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError("unknown verdict outcome")
        if self.outcome == OUTCOME_PASS:
            if self.reason != "":
                raise ValueError("a pass verdict has no reason")
            if not isinstance(self.principal, str) or not self.principal:
                raise ValueError("a pass verdict needs a verified principal")
            if not isinstance(self.permissions, PrincipalEntry):
                raise ValueError("a pass verdict needs the verification-time permissions")
            if self.retry_after:
                raise ValueError("a pass verdict has no retry_after")
            return
        if self.outcome == OUTCOME_FORBIDDEN:
            if self.reason not in FORBIDDEN_REASONS:
                raise ValueError("forbidden uses one of the authorization reasons")
            if not isinstance(self.principal, str) or not self.principal:
                raise ValueError("a forbidden verdict carries the verified principal")
            if self.retry_after:
                raise ValueError("a forbidden verdict has no retry_after")
            if self.permissions is not None:
                raise ValueError("only a pass verdict carries permissions")
            return
        if self.reason not in REASONS:
            raise ValueError("reason is not on the whitelist")
        if self.reason in FORBIDDEN_REASONS:
            raise ValueError("authorization reasons belong to the forbidden outcome")
        if self.outcome == OUTCOME_RATE_LIMITED:
            if self.reason != REASON_RATE_LIMITED:
                raise ValueError("rate_limited uses the rate_limited reason")
            if isinstance(self.retry_after, bool) or not 1 <= int(self.retry_after) <= 126:
                raise ValueError("retry_after must be 1..126")
        elif self.retry_after:
            raise ValueError("only rate_limited carries retry_after")
        if self.principal is not None:
            raise ValueError("only a pass verdict carries a principal")
        if self.permissions is not None:
            raise ValueError("only a pass verdict carries permissions")


def passed(principal: str, permissions: PrincipalEntry) -> Verdict:
    """驗證全部通過（含 `accept` 的 ACCEPTED）；帶驗章時那一份權限（C-905）。"""
    return Verdict(OUTCOME_PASS, "", principal=principal, permissions=permissions)


def denied(reason: str) -> Verdict:
    """401 類（缺標頭、格式、未知／撤銷、時窗、錯簽、重放、水位、framing）。"""
    return Verdict(OUTCOME_DENY, reason)


def limited(retry_after: int) -> Verdict:
    """429 類（該 key 的防重放配額滿）。"""
    return Verdict(OUTCOME_RATE_LIMITED, REASON_RATE_LIMITED, retry_after=int(retry_after))


def unavailable(reason: str) -> Verdict:
    """503 類（驗章工具、allowed_signers、庫、號誌、名冊不可用）。"""
    return Verdict(OUTCOME_UNAVAILABLE, reason)


def forbidden(principal: str, reason: str) -> Verdict:
    """403 類（已驗章、已 accept，但 principal 權限不足；C-902 的三個原因）。"""
    return Verdict(OUTCOME_FORBIDDEN, reason, principal=principal)


ACTION_CONTINUE = "continue"
ACTION_RESPOND = "respond"
ACTION_CLOSE = "close"


@dataclass(frozen=True)
class Action:
    """HTTP 層要做的動作（C-603）：繼續、回應（含狀態碼／錯誤／額外標頭）或關閉連線。"""

    kind: str
    status: int = 0
    error: ApiError | None = None
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in (ACTION_CONTINUE, ACTION_RESPOND, ACTION_CLOSE):
            raise ValueError("unknown action kind")
        if self.kind == ACTION_RESPOND:
            if not isinstance(self.status, int) or not isinstance(self.error, ApiError):
                raise ValueError("a respond action needs a status and an ApiError")
        elif self.status or self.error is not None or self.headers:
            raise ValueError("only a respond action carries a status/error/headers")


CONTINUE = Action(ACTION_CONTINUE)
CLOSE = Action(ACTION_CLOSE)


def apply(mode: str, verdict: Verdict, *, route: str = "-", log: "AuthLog | None" = None,
          close_on_warn: bool = False) -> Action:
    """模式對應的**唯一一處**（C-603）；同時記一行認證事件（C-607）。

    - `pass` → continue（warn／enforce 都一樣；off 不呼叫 gate）。
    - `deny` → enforce 401 `unauthorized`（固定 body、不加 `WWW-Authenticate`）；warn 記
      would-deny 後 continue。
    - `forbidden`（D3a／C-906）→ enforce 403 `forbidden`（**所有授權 reason 的 body 都
      逐 byte 相同**，不加任何標頭）；warn 記 would-deny（`outcome=forbidden`）後
      continue，業務結果不變。
    - `rate_limited` → enforce 429 `rate_limited` 加 `Retry-After`；warn 記 would-deny 後
      continue。
    - `unavailable` → enforce 503 `auth_unavailable`；warn 記不可判定後 continue（唯一
      例外是 body 沒讀完：`close_on_warn=True` 時關閉連線，因為沒有 body 可以照舊處理）。

    `route` 是路由模板（原文無法路由時是固定哨兵 `-`），只進日誌，不影響回應。
    """
    _record(mode, verdict, route, log)
    if mode == MODE_OFF:
        # off 不建 gate、也不呼叫 apply；防禦性地當成照舊處理，不記認證事件。
        return CONTINUE
    if verdict.outcome == OUTCOME_PASS:
        return CONTINUE
    if mode == MODE_WARN:
        if verdict.outcome == OUTCOME_UNAVAILABLE and close_on_warn:
            return CLOSE
        return CONTINUE
    if verdict.outcome == OUTCOME_FORBIDDEN:
        return Action(ACTION_RESPOND, 403, forbidden_error())
    if verdict.outcome == OUTCOME_DENY:
        return Action(ACTION_RESPOND, 401, unauthorized())
    if verdict.outcome == OUTCOME_RATE_LIMITED:
        return Action(ACTION_RESPOND, 429, rate_limited(),
                      (("Retry-After", str(verdict.retry_after)),))
    return Action(ACTION_RESPOND, 503, auth_unavailable())


def _record(mode: str, verdict: Verdict, route: str, log: "AuthLog | None") -> None:
    if log is None or mode == MODE_OFF:
        return
    log.record(
        mode=mode,
        outcome=verdict.outcome,
        reason=verdict.reason,
        route=route,
        principal=verdict.principal,
    )


# ---------------------------------------------------------------------------
# C-607 日誌聚合
# ---------------------------------------------------------------------------

class AuthLog:
    """認證事件的 60 秒聚合（C-607；#315 語意）。

    - 每一行只含 `mode`、`outcome`、白名單 `reason`、路由模板、已驗證 principal 與聚合
      計數；不含標頭值、簽章、nonce、query、body、公鑰、key ID、`Reject.kind`、
      `Outcome.value`、`StoreUnavailable.kind` 或 stderr 原文。
    - **count＝這一行之前尚未被任何一行記錄的事件數**：同一鍵所有行的 count 加總＝該鍵
      的事件總數（flush 之後）。鍵的第一筆事件立刻寫 `count=1`（不把單一事件藏起來）；
      之後同一視窗內的事件只累加、不寫；視窗到期時由下列三處之一寫出累計（pending>0 才寫）
      並刪掉這個鍵：(a) 同鍵下一筆事件進來（先寫舊累計，再開新視窗寫 `count=1`）；
      (b) 背景 `flush_expired()`（watcher 的 tick）；(c) 關機 `flush_all()`（不管到期，全寫出）。
    - 每鍵每視窗至多兩行（開頭 1、收尾 pending）；收尾行最晚約「視窗＋watcher 輪詢間隔」
      之後出現。`_windows` 只保留未到期的鍵，記憶體有界。
    - 已知限制：沒有 watcher（warn 降級）時沒有背景 flush，收尾行只在同鍵下一筆事件或關機
      時出現；關機 drain 逾時後才 record 的尾數，可能在 `flush_all()` 之後漏寫（不補寫）。
    - clock 必須單調（`time.monotonic` 或等價）；時鐘回退時鍵不會到期，直到時鐘追上。
    - 鍵是 `(mode, outcome, reason, 路由模板, principal)`。design 的聚合鍵沒列 principal；
      但 pass 事件帶 principal，若不分開計數會把不同身分混在同一行——那反而讓「已驗證的
      principal」失去意義。這一點列在報告的「待決」。
    """

    def __init__(self, *, sink: Callable[[str], None] | None = None,
                 clock: Callable[[], float] | None = None, window: float = 60.0):
        self.window = float(window)
        self._sink = sink if sink is not None else _LOG.info
        self._clock = clock if clock is not None else time.monotonic
        # key -> [視窗起點, pending]；pending＝已發生但還沒寫出的事件數（不含立刻寫出的第一筆）。
        self._windows: dict[tuple, list] = {}
        self._lock = threading.Lock()
        self.lines = 0

    def record(self, *, mode: str, outcome: str, reason: str, route: str,
               principal: str | None = None) -> None:
        if outcome not in OUTCOMES:
            return
        if outcome == OUTCOME_PASS:
            reason = ""
        elif reason not in REASONS:
            # 不該發生（Verdict 已驗）；寧可改記固定分類，也不讓表外值進日誌。
            reason = REASON_MALFORMED
        key = (mode, outcome, reason, route, principal or "")
        now = self._clock()
        with self._lock:
            entry = self._windows.get(key)
            if entry is None:
                self._windows[key] = [now, 0]
                self._emit(key, 1)
                return
            if now - entry[0] < self.window:
                entry[1] += 1
                return
            # 視窗到期：先寫舊視窗的收尾（pending>0 才寫），再開新視窗立刻寫 1。
            del self._windows[key]
            if entry[1] > 0:
                self._emit(key, entry[1])
            self._windows[key] = [now, 0]
            self._emit(key, 1)

    def flush_expired(self) -> int:
        """寫出已到期鍵的 pending 並刪掉這些鍵；回傳這次嘗試寫出的行數（含 sink 丟例外者）。

        只給 watcher 的背景 tick 用（`AuthRuntime._log_tick`），不持有任何 lgwd 其他鎖。
        未到期的鍵不動；pending=0 的到期鍵只刪不寫。
        """
        now = self._clock()
        with self._lock:
            before = self.lines
            for key in [k for k, e in self._windows.items() if now - e[0] >= self.window]:
                entry = self._windows.pop(key)
                if entry[1] > 0:
                    self._emit(key, entry[1])
            return self.lines - before

    def flush_all(self) -> int:
        """不管到期與否，寫出所有鍵的 pending 並清空；回傳這次嘗試寫出的行數。

        只給關機用（`AuthRuntime.stop`，在請求已停下、watcher 已停止之後）。清空後同鍵
        下一筆事件照「鍵不存在」立刻寫 1。
        """
        with self._lock:
            before = self.lines
            for key, entry in self._windows.items():
                if entry[1] > 0:
                    self._emit(key, entry[1])
            self._windows.clear()
            return self.lines - before

    def _emit(self, key: tuple, count: int) -> None:
        mode, outcome, reason, route, principal = key
        line = (
            "auth mode={0} outcome={1} reason={2} route={3} principal={4} count={5}".format(
                mode, outcome, reason or "-", route, principal or "-", count
            )
        )
        self.lines += 1
        try:
            self._sink(line)
        except Exception:  # 日誌本身不能讓請求失敗
            pass


# ---------------------------------------------------------------------------
# gate（C-602）
# ---------------------------------------------------------------------------

@dataclass
class AuthState:
    """請求路徑唯一的快照來源（C-406／窗口 A）。由 `AuthGate._lock` 保護。

    `registry` 與 `signers` 一定一起換：`signers` 是那個 registry 的有效 key 產生的
    allowed_signers。`signers_ok=False` 對所有 key 生效（fail-closed）；`pending_registry`
    是寫檔失敗時留下的那一代，給 `extra_ticks` 重試。
    """

    registry: Registry | None = None
    signers: SignersFile | None = None
    signers_ok: bool = False
    pending_registry: Registry | None = None
    retry_at: float = 0.0


class AuthGate:
    """五條業務路由的 gate（C-602）。`mode` 只讀屬性（`http._gate_mode` 仍只看這個）。

    請求路徑只碰 `_lock`、`_state`、verifier 與 replay store；不碰檔案、不碰
    `watcher.snapshot`（背景路徑才讀它）。
    """

    def __init__(self, *, mode: str, clock: Clock, verifier, replay,
                 stores_ok: bool = True, busy_counter: BusyCounter | None = None):
        if mode not in (MODE_WARN, MODE_ENFORCE):
            raise ValueError("the gate is only used in warn/enforce")
        self.mode = mode
        self.clock = clock
        self.verifier = verifier
        self.replay = replay
        self.stores_ok = bool(stores_ok)
        # StoreBusy 的計數（C-1001）；None 時判定結果相同，只是不計數。
        self.busy_counter = busy_counter
        self._lock = threading.Lock()
        self._state = AuthState()
        # 觀測用（測試與報告；不影響判定）。
        self.authenticate_calls = 0

    # --- 發布（只由 AuthRuntime 的背景路徑呼叫） ---

    def publish(self, registry: Registry, signers: SignersFile) -> None:
        """原子發布一組 `(registry, signers)`，再 retire 舊的那一份（C-406）。

        **同一個路徑**（同一世代重新發布：`write_allowed_signers` 已用 `os.replace`
        換掉那個檔）不 retire：舊的 `SignersFile` 指向的已經是新 inode 的同名檔，retire
        會把剛換上的新檔刪掉，這個世代就會一直 UNAVAILABLE（#262 低-1）。舊物件沒有
        在途引用時自然消失，`_unlink` 由新物件在換世代時負責。
        """
        with self._lock:
            old = self._state.signers
            self._state.registry = registry
            self._state.signers = signers
            self._state.signers_ok = True
            self._state.pending_registry = None
            self._state.retry_at = 0.0
        if old is not None and old is not signers and old.path != signers.path:
            old.retire()

    def fail_publish(self, registry: Registry | None) -> None:
        """`on_snapshot` 失敗：`signers_ok=False` 對所有 key 生效（fail-closed）。

        舊的 `signers` 留在 state 裡（不 retire、不刪檔），下一次成功換世代時才 retire
        （W-17）；這段期間請求一律 UNAVAILABLE，所以它有沒有檔案都不影響判定。
        """
        with self._lock:
            self._state.signers_ok = False
            self._state.pending_registry = registry

    def pending(self) -> tuple[bool, Registry | None, float]:
        """背景重試要看的三件事：`(signers_ok, pending_registry, retry_at)`。"""
        with self._lock:
            state = self._state
            return state.signers_ok, state.pending_registry, state.retry_at

    def defer_retry(self, when: float) -> None:
        """設定下一次重試 allowed_signers 的最早時刻（單調時鐘秒）。"""
        with self._lock:
            self._state.retry_at = float(when)

    def snapshot(self) -> tuple[Registry | None, SignersFile | None, bool]:
        """目前那一組（觀測用；請求路徑走 `_acquire`，不經這裡）。"""
        with self._lock:
            state = self._state
            return state.registry, state.signers, state.signers_ok

    def _acquire(self):
        """在同一把鎖內取一組並 acquire；回 `(registry, signers)` 或 None（判 UNAVAILABLE）。"""
        with self._lock:
            state = self._state
            if state.registry is None:
                return "registry", None
            if not state.signers_ok or state.signers is None:
                return "verifier", None
            if not state.signers.acquire():
                return "verifier", None
            return None, (state.registry, state.signers)

    # --- 請求（C-602） ---

    def authenticate(self, *, method: str, framing, headers, body) -> Verdict:
        """驗一個業務請求；**不丟例外**（一律轉成 `Verdict`），回 401／429／503 的分類。"""
        self.authenticate_calls += 1
        if framing is not None and not framing.ok:
            return denied(REASON_FRAMING)
        if not self.stores_ok:
            return unavailable(REASON_STORE_UNAVAILABLE)
        failure, acquired = self._acquire()
        if failure == "registry":
            return unavailable(REASON_REGISTRY_UNAVAILABLE)
        if acquired is None:
            return unavailable(REASON_VERIFIER_UNAVAILABLE)
        registry, signers = acquired
        try:
            return self._decide(method, framing, headers, body, registry, signers)
        finally:
            signers.release()

    def _decide(self, method: str, framing, headers, body,
                registry: Registry, signers: SignersFile) -> Verdict:
        # 1. 五個標頭齊全、版本恰為 1、至多一次（framing 已擋重複；gate 自己再擋一次）。
        values: dict[str, str] = {}
        for name in HEADER_NAMES:
            items = _header_values(headers, name)
            if not items:
                return denied(REASON_MISSING)
            if len(items) > 1 or not items[0]:
                return denied(REASON_MALFORMED)
            values[name] = items[0]
        if values[HEADER_VERSION] != "1":
            return denied(REASON_MALFORMED)

        # 2. key ID 格式（名冊的 ID 格式；也排除 allowed_signers 的樣式字元）。
        key_id = values[HEADER_KEY_ID]
        if not ID_PATTERN.match(key_id):
            return denied(REASON_MALFORMED)

        # 3. 查表：未知與撤銷都在 fork 前（C-602；未知不洩漏「存在但撤銷」）。
        entry = registry.key_by_id(key_id)
        if entry is None:
            return denied(REASON_UNKNOWN_KEY)
        if not entry.active:
            return denied(REASON_REVOKED)

        # 4. timestamp 與 nonce 的形狀。
        timestamp_text = values[HEADER_TIMESTAMP]
        if not _TIMESTAMP_RE.match(timestamp_text) or len(timestamp_text) > 20:
            # 20 位十進位遠超合理範圍；也避開 `int()` 對超長字串的限制（4000 位）。
            return denied(REASON_MALFORMED)
        timestamp = int(timestamp_text)
        nonce = values[HEADER_NONCE]
        if not _NONCE_RE.match(nonce) or _b64url_bytes(nonce) is None:
            return denied(REASON_MALFORMED)

        # 5. 交易外粗篩 ±60（只為早拒；最終以 accept 交易內的 now 為準）。
        now = now_seconds(self.clock)
        if abs(now - timestamp) > COARSE_WINDOW_SECONDS:
            return denied(REASON_EXPIRED)

        # 6. 信封預解析（不 fork；任何 Reject 都是 401，kind 不進日誌）。
        try:
            blob = sshsig.preparse(values[HEADER_SIGNATURE], entry)
        except Reject:
            return denied(REASON_MALFORMED)

        # 7. 八行（第 7 行＝framing 的原文 request-target；第 8 行＝實際 body 的雜湊）。
        message = build_signing_message(
            key_id=key_id,
            timestamp=timestamp_text,
            nonce=nonce,
            method=method,
            target=framing.target,
            body=body,
        )

        # 8. 驗章（唯一 401 出口在 sshsig.classify；這裡只翻譯）。
        if self.verifier is None:
            return unavailable(REASON_VERIFIER_UNAVAILABLE)
        try:
            outcome = self.verifier.verify(message, blob, entry, signers)
        except Exception:  # 驗章不能把例外帶成 500
            return unavailable(REASON_VERIFIER_UNAVAILABLE)
        if outcome is Outcome.BAD_SIG:
            return denied(REASON_BAD_SIGNATURE)
        if outcome is not Outcome.PASS:
            return unavailable(REASON_VERIFIER_UNAVAILABLE)

        # 9. 防重放交易（warn 的 PASS 也照常 INSERT；C-603／G-14）。
        if self.replay is None:
            return unavailable(REASON_STORE_UNAVAILABLE)
        try:
            accepted = self.replay.accept(key_id, nonce, timestamp)
        except StoreBusy:
            # C-1000／C-1001：只有忙碌到期限才計數；其他錯誤維持 store_unavailable。
            if self.busy_counter is not None:
                self.busy_counter.hit()
            return unavailable(REASON_STORE_BUSY)
        except (StoreError, OSError):
            return unavailable(REASON_STORE_UNAVAILABLE)
        except Exception:  # 任何未預期錯誤也絕不當成 PASS
            return unavailable(REASON_STORE_UNAVAILABLE)
        if accepted.outcome == ACCEPTED:
            # C-905：權限來自驗章時那一份名冊快照（`registry` 是本請求 `_acquire`
            # 取到的同一份），不是 watcher 的最新快照。
            permissions = registry.principals.get(entry.principal)
            if permissions is None:
                # 名冊 parser 保證 key 的 principal 在 principals 裡；走得到這裡代表
                # 名冊物件不一致，fail-closed（不當成 PASS）。
                return unavailable(REASON_REGISTRY_UNAVAILABLE)
            return passed(entry.principal, permissions)
        if accepted.outcome == RATE_LIMITED:
            return limited(accepted.retry_after)
        if accepted.outcome == DENIED:
            if accepted.reason in (REASON_EXPIRED, REASON_WATER, REASON_REPLAY):
                return denied(accepted.reason)
        return unavailable(REASON_STORE_UNAVAILABLE)


def _header_values(headers, name: str) -> list[str]:
    """取一個標頭的所有值（不分大小寫）；支援 `email.message.Message` 與測試用 dict。"""
    getter = getattr(headers, "get_all", None)
    if getter is not None:
        return [value for value in (getter(name) or ())]
    values: list[str] = []
    items = getattr(headers, "items", None)
    if items is None:
        return values
    for key, value in items():
        if str(key).lower() != name.lower():
            continue
        if isinstance(value, (list, tuple)):
            values.extend(value)
        else:
            values.append(value)
    return values


def _b64url_bytes(text: str) -> bytes | None:
    """嚴格解 base64url（無 padding）：長度 43、往返相同、解得 32 bytes。"""
    try:
        raw = base64.urlsafe_b64decode(text + "=")
    except (binascii.Error, ValueError):
        return None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text:
        return None
    if len(raw) != 32:
        return None
    return raw


def build_signing_message(*, key_id: str, timestamp: str, nonce: str, method: str,
                          target: str, body) -> bytes:
    """組 ASCII 八行（每行以 LF 結尾；design「線上格式」）。

    第 7 行只取 `framing.Framing.target` 的**原文**（呼叫端傳進來的那一份）；第 8 行是
    實際送出的 body bytes 的 SHA-256 小寫 hex。這裡不重新解碼或正規化任何欄位。
    """
    if body is None:
        raw = b""
    elif isinstance(body, (bytes, bytearray, memoryview)):
        raw = bytes(body)
    else:
        raise TypeError("body must be bytes")
    digest = hashlib.sha256(raw).hexdigest()
    lines = (SIG_LINE_1, AUDIENCE, key_id, timestamp, nonce, str(method), target, digest)
    for line in lines:
        if "\n" in line or "\r" in line:
            raise ValueError("signing lines must not contain line breaks")
    return ("\n".join(lines) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# 執行期（C-605 程式庫層；C-610）
# ---------------------------------------------------------------------------

class AuthRuntime:
    """認證執行期的持有者：鎖、兩庫、watcher、verifier、purge timer 與 gate。

    生命週期見 REPORT-D2c-arch §2.1：`open_runtime` 建好、`start()` 起背景執行緒、
    `stop()` 依序收掉（API server 的 shutdown 由 6b 在 `stop()` 之前做）。`stop()` 可以
    重複呼叫。
    """

    def __init__(self, cfg: AuthConfig, clock: Clock, paths: AuthPaths, owner_uid: int):
        self.cfg = cfg
        self.clock = clock
        self.paths = paths
        self.mode = cfg.mode
        self.owner_uid = int(owner_uid)
        # 名冊檔的擁有者（正式值與 owner_uid 同為 root；測試可以分開給）。
        self.registry_owner_uid = int(owner_uid)
        self.lock = None
        self.trust: TrustStore | None = None
        self.replay: ReplayStore | None = None
        self.watcher: RegistryWatcher | None = None
        self.verifier = None
        self.purge: PurgeTimer | None = None
        self.gate: AuthGate | None = None
        # C-607 聚合（#315）：由 Gateway 在建好 API server 後接上（`make_api_server` 建的那一個）。
        self.auth_log: AuthLog | None = None
        # StoreBusy 計數與計數檔（#312；warn／enforce 才建，`_open` 裡建立）。
        self.busy_counter: BusyCounter | None = None
        self.busy_window: BusyWindow | None = None
        self._busy_since = 0
        self._busy_last_write: float | None = None
        self._busy_pending = False
        self._degraded: set[str] = set()
        self._stopped = False
        self._signers_backoff = 0.0
        # 觀測用（測試與報告）。
        self.clean_spool_calls = 0
        self.signers_writes = 0

    # --- 對外狀態 ---

    @property
    def degraded(self) -> frozenset[str]:
        """`{"stores","registry","verifier"}` 的子集（warn 的降級面）。"""
        return frozenset(self._degraded)

    @property
    def running(self) -> bool:
        watcher = self.watcher
        return watcher is not None and watcher.running

    # --- 啟動 ---

    def _degrade(self, area: str) -> None:
        self._degraded.add(area)

    def _event(self, name: str, **fields) -> None:
        extra = " ".join("{0}={1}".format(k, v) for k, v in sorted(fields.items()))
        _LOG.warning("auth event=%s%s", name, (" " + extra) if extra else "")

    def _open(self) -> None:
        cfg = self.cfg
        # 1. flock（LockBusy：任何模式都拒絕；LockUnavailable：enforce 拒絕、warn 降級）。
        try:
            self.lock = acquire_store_lock(self.paths.store_lock)
        except LockBusy:
            raise StartupRefused(KIND_LOCK_BUSY, None,
                                 "another lgwd or store writer holds the store lock")
        except LockUnavailable:
            if cfg.mode == MODE_ENFORCE:
                raise StartupRefused(
                    KIND_LOCK_UNAVAILABLE, None,
                    "store lock cannot be opened: check permissions / read-only filesystem")
            self._degrade("stores")
            self._event("lock_unavailable")
        # 2. 兩庫（含識別）。
        if "stores" not in self._degraded:
            self._open_stores()

        # 3. verifier（先自測，再 load_initial；REPORT-D2c-arch §2.2／§5 決定 2）。
        self._open_verifier()

        # StoreBusy 計數與取樣（#312；C-1001／C-1006）。gate 之前建好，要注入它。
        self.busy_counter = BusyCounter()
        self.busy_window = BusyWindow(self.busy_counter, monotonic=lambda: _monotonic())
        self._busy_since = now_seconds(self.clock)

        # gate 必須在 load_initial 之前存在：on_snapshot 會發布到它。
        self.gate = AuthGate(
            mode=cfg.mode,
            clock=self.clock,
            verifier=self.verifier,
            replay=self.replay,
            stores_ok="stores" not in self._degraded,
            busy_counter=self.busy_counter,
        )

        # 4. 名冊與信任庫快照。
        self._open_registry()

        # 5. `H[key]` 在未來只記事件（不拒絕啟動）。
        if self.replay is not None:
            try:
                future = self.replay.keys_with_future_water()
            except StoreError:
                future = []
            if future:
                self._event("key_water_in_future", count=len(future))

        # 6. 背景清除（watcher 的輪詢在 start() 才起，測試可以只 tick）。
        if self.replay is not None:
            self.purge = PurgeTimer(self.replay)

    def _open_stores(self) -> None:
        cfg = self.cfg
        try:
            self.trust = TrustStore(self.paths.trust_db, busy_timeout=cfg.busy_timeout)
        except StoreError as exc:
            self._store_failure("trust", exc)
            return
        try:
            self.replay = ReplayStore(
                self.paths.replay_db, self.clock,
                per_key_max=cfg.per_key_max,
                busy_timeout=cfg.busy_timeout,
                collect_lock_waits=cfg.collect_lock_waits,
            )
        except StoreError as exc:
            self._store_failure("replay", exc)
            return
        try:
            same = check_store_identity(self.trust.db, self.replay.db)
        except StoreError as exc:
            self._store_failure("trust", exc)
            return
        if not same:
            self._refuse_or_degrade(KIND_IDENTITY_MISMATCH, None,
                                    "trust and replay store identities differ: decide by audit",
                                    area="stores")
            if "stores" in self._degraded:
                self._close_stores()

    def _store_failure(self, store: str, exc: StoreError) -> None:
        if isinstance(exc, StoreMissing):
            kind, message = KIND_STORE_MISSING, _missing_message(store)
        else:
            kind, message = KIND_STORE_UNAVAILABLE, _STORE_UNAVAILABLE_MESSAGE
        self._refuse_or_degrade(kind, store, message, area="stores")
        if "stores" in self._degraded:
            self._close_stores()

    def _refuse_or_degrade(self, kind: str, store: str | None, message: str,
                           *, area: str) -> None:
        """enforce 丟 `StartupRefused`；warn 只把那一塊標成不可判定（J-6）。"""
        if self.cfg.mode == MODE_ENFORCE:
            raise StartupRefused(kind, store, message)
        self._degrade(area)
        self._event("degraded", kind=kind, area=area, store=store or "-")

    def _open_verifier(self) -> None:
        cfg = self.cfg
        try:
            _ensure_spool_dir(self.paths.sshsig_dir)
            self.verifier = sshsig.Verifier(
                sshkeygen_path=cfg.sshkeygen_path,
                spool_dir=self.paths.sshsig_dir,
                home_dir=self.paths.sshsig_dir,
                concurrency=cfg.verify_concurrency,
                timeout=cfg.verify_timeout,
                owner_uid=self.owner_uid,
            )
        except (ValueError, OSError):
            self.verifier = None
            self._refuse_or_degrade(
                KIND_VERIFIER_BUILD, None,
                "ssh-keygen is missing or the sshsig spool directory is not private",
                area="verifier")
            return
        self.clean_spool_calls += 1
        self.verifier.clean_spool()
        if not self.verifier.selftest():
            # warn：保留 verifier（它自己會把 verify 判成 UNAVAILABLE），只標不可判定。
            self._refuse_or_degrade(
                KIND_VERIFIER_SELFTEST, None,
                "ssh-keygen selftest failed (missing binary, version below 9.6, or unknown output)",
                area="verifier")
        if self.verifier.version_unknown:
            self._event("openssh_version_unknown")

    def _open_registry(self) -> None:
        if self.trust is None:
            # 庫降級：沒有信任庫就沒有名冊快照（warn 只有這一條會走到這裡）。
            self._degrade("registry")
            return
        watcher = RegistryWatcher(
            self.paths,
            self.trust,
            self.clock,
            on_snapshot=self._on_snapshot,
            extra_ticks=(self._extra_tick, self._busy_tick, self._log_tick),
            poll_interval=self.cfg.poll_interval,
            owner_uid=self.registry_owner_uid,
        )
        decision = watcher.load_initial()
        self.watcher = watcher
        if watcher.snapshot is None:
            self._refuse_or_degrade(
                KIND_REGISTRY, None,
                "registry file and verified snapshot are both invalid: {0}".format(
                    decision.result),
                area="registry")

    def _close_stores(self) -> None:
        if self.replay is not None:
            self.replay.close()
            self.replay = None
        if self.trust is not None:
            self.trust.close()
            self.trust = None

    # --- on_snapshot（fail-closed）與背景重試 ---

    def _on_snapshot(self, registry: Registry) -> None:
        """名冊換世代：寫 allowed_signers → 原子發布 → retire 舊檔。

        整個回呼 fail-closed：**任何**例外（含 `VerifierUnavailable`、`OSError`、任意
        `BaseException`）都把 `signers_ok` 設為 False，之後所有 key 都 UNAVAILABLE
        （REPORT-260-archreview2 §2(a)）。舊的 `signers` 不 retire（W-17）。
        """
        gate = self.gate
        try:
            signers = sshsig.write_allowed_signers(
                registry.active_keys(), self.paths.state_dir, registry.generation)
        except BaseException as exc:  # noqa: BLE001 - 契約要求任何例外都 fail-closed
            self._event("allowed_signers_failed", kind=type(exc).__name__)
            self._signers_backoff = 0.0
            if gate is not None:
                gate.fail_publish(registry)
                self._signers_backoff = SIGNERS_BACKOFF_INITIAL
                gate.defer_retry(_monotonic() + self._signers_backoff)
            return
        self._signers_backoff = 0.0
        self.signers_writes += 1
        if gate is not None:
            gate.publish(registry, signers)

    def _extra_tick(self) -> None:
        """watcher 每輪最先執行（載入鎖之外）：verifier 自檢檢查＋allowed_signers 重試。"""
        if self.verifier is not None:
            try:
                self.verifier.recheck_if_changed()
            except Exception as exc:  # 背景執行緒不能因一次失敗就死掉
                _LOG.warning("auth verifier recheck failed: %s", type(exc).__name__)
        self._retry_signers()

    def _busy_tick(self) -> None:
        """`.auth-busy.status`（C-1007）：啟動後第一次就寫，之後每 60 秒寫一次；寫失敗的下一輪直接補寫。

        只在 watcher 的背景執行緒上跑。寫入失敗不往上丟，只在每段連續失敗的第一次記一行
        （不帶路徑、不帶 errno）。
        """
        window = self.busy_window
        if window is None or self.watcher is None:
            return
        now = _monotonic()
        last = self._busy_last_write
        if not self._busy_pending and last is not None and now - last < BUSY_STATUS_INTERVAL:
            return
        total, last_30m, last_60m = window.sample()
        document = {
            "schema": BUSY_STATUS_SCHEMA,
            "mode": self.mode,
            "since": self._busy_since,
            "updated": now_seconds(self.clock),
            "store_busy_total": total,
            "store_busy_30m": last_30m,
            "store_busy_60m": last_60m,
        }
        try:
            write_status(self.paths.busy_status, document)
        except OSError as exc:
            if not self._busy_pending:
                self._event("busy_status_write_failed", kind=type(exc).__name__)
            self._busy_pending = True
            return
        self._busy_pending = False
        self._busy_last_write = now

    def _log_tick(self) -> None:
        """C-607 收尾（#315）：視窗到期的鍵由背景補寫；只在 watcher 的背景執行緒上跑，不持任何鎖。

        延遲上限＝視窗＋`poll_interval`。`auth_log` 還沒接上（啟動的前幾輪）就什麼都不做。
        """
        log = self.auth_log
        if log is None:
            return
        log.flush_expired()

    def _flush_log(self) -> None:
        """關機時把 pending 全部寫出（#315 C-1017）；失敗不擋後續收尾。"""
        log = self.auth_log
        if log is None:
            return
        try:
            log.flush_all()
        except Exception:  # 日誌收尾不能讓關機失敗
            pass

    def _retry_signers(self) -> None:
        gate = self.gate
        if gate is None:
            return
        ok, pending, retry_at = gate.pending()
        if ok:
            return
        registry = pending
        if registry is None:
            watcher = self.watcher
            snapshot = watcher.snapshot if watcher is not None else None
            if snapshot is None:
                return
            registry = snapshot.registry   # 背景路徑，不受窗口 A 限制
        now = _monotonic()
        if now < retry_at:
            return
        try:
            signers = sshsig.write_allowed_signers(
                registry.active_keys(), self.paths.state_dir, registry.generation)
        except BaseException as exc:  # noqa: BLE001 - 契約要求 fail-closed
            if self._signers_backoff <= 0:
                self._signers_backoff = SIGNERS_BACKOFF_INITIAL
            else:
                self._signers_backoff = min(
                    self._signers_backoff * 2, SIGNERS_BACKOFF_MAX)
            gate.defer_retry(now + self._signers_backoff)
            _LOG.warning("auth allowed_signers retry failed: %s", type(exc).__name__)
            return
        self._signers_backoff = 0.0
        self.signers_writes += 1
        gate.publish(registry, signers)

    # --- 啟停 ---

    def start(self) -> None:
        if self.purge is not None:
            self.purge.start()
        if self.watcher is not None:
            self.watcher.start()

    def stop(self) -> None:
        """依 REPORT-D2c-arch §2.2 的停止順序收掉（API server 已由 6b 先 shutdown）。

        watcher → purge → 兩庫 → 鎖（鎖一定最後放，否則下一個 lgwd 可能在這行程還在寫
        的時候拿到鎖）。可重複呼叫；`exited` 之後任何欄位都可以是 None。
        """
        if self._stopped:
            return
        self._stopped = True
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        # 收尾行（#315）：watcher 已停（不會再有背景 flush）、請求已停下；watcher 為 None
        # 的降級路徑也要寫，所以放在 watcher 區塊之外。
        self._flush_log()
        if self.purge is not None:
            self.purge.stop()
            self.purge = None
        self._close_stores()
        if self.lock is not None:
            self.lock.release()
            self.lock = None


def _ensure_spool_dir(path: str) -> None:
    """`sshsig_dir` 必須存在且是 0700（不存在就建；權限不對由 Verifier 建構子拒絕）。"""
    if os.path.isdir(path):
        return
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def open_runtime(cfg: AuthConfig, clock: Clock, *, owner_uid: int = 0,
                 registry_owner_uid: int | None = None) -> AuthRuntime:
    """照 REPORT-D2c-arch §2.2 的順序建一個 runtime（off 不得呼叫）。

    `owner_uid` 是 `ssh-keygen` 的擁有者（正式值是 root）；`registry_owner_uid` 是名冊檔
    的擁有者（預設同 `owner_uid`）。後者是**測試用**的接縫：正式部署兩者都是 root，測試
    機器上 `ssh-keygen` 是 root 的、名冊檔是測試使用者的。與 `Verifier`／
    `read_registry_file` 既有的 `owner_uid` 參數同一種形狀，不開 CLI 或環境變數。

    失敗時丟 `StartupRefused`（`kind`／`store`／`message`），並先關掉已開的庫、放掉鎖
    ——enforce 要在 bind 之前就結束，所以不能留下任何資源（也讓下一次啟動不被自己的
    flock 擋住）。

    warn 下除 `LockBusy` 以外都降級回 runtime（`degraded` 說明哪一塊不可判定）；
    `LockBusy` 在任何模式都是拒絕（禁止多實例）。
    """
    if not isinstance(cfg, AuthConfig):
        raise TypeError("cfg must be an AuthConfig")
    if cfg.mode == MODE_OFF:
        raise ValueError("open_runtime must not be called in off mode")
    paths = auth_paths(cfg.state_dir, registry=cfg.registry_path)
    runtime = AuthRuntime(cfg, clock, paths, owner_uid)
    if registry_owner_uid is not None:
        runtime.registry_owner_uid = int(registry_owner_uid)
    try:
        runtime._open()
    except BaseException:
        runtime.stop()
        raise
    return runtime


__all__ = [
    "ACTION_CLOSE",
    "ACTION_CONTINUE",
    "ACTION_RESPOND",
    "AUDIENCE",
    "AUTHORIZATION_REASONS",
    "AuthConfig",
    "AuthGate",
    "AuthLog",
    "AuthRuntime",
    "AuthState",
    "Action",
    "CONTINUE",
    "CLOSE",
    "HEADER_NAMES",
    "KIND_IDENTITY_MISMATCH",
    "KIND_LOCK_BUSY",
    "KIND_LOCK_UNAVAILABLE",
    "KIND_REGISTRY",
    "KIND_STORE_MISSING",
    "KIND_STORE_UNAVAILABLE",
    "KIND_VERIFIER_BUILD",
    "KIND_VERIFIER_SELFTEST",
    "MODES",
    "MODE_ENFORCE",
    "MODE_OFF",
    "MODE_WARN",
    "OUTCOMES",
    "OUTCOME_DENY",
    "OUTCOME_FORBIDDEN",
    "OUTCOME_PASS",
    "OUTCOME_RATE_LIMITED",
    "OUTCOME_UNAVAILABLE",
    "REASONS",
    "REASON_BAD_SIGNATURE",
    "REASON_BUSY",
    "REASON_EXPIRED",
    "REASON_FORBIDDEN_AGENT",
    "REASON_FORBIDDEN_OP",
    "REASON_FORBIDDEN_ROLE",
    "REASON_FRAMING",
    "REASON_MALFORMED",
    "REASON_MISSING",
    "REASON_RATE_LIMITED",
    "REASON_REGISTRY_UNAVAILABLE",
    "REASON_REPLAY",
    "REASON_REVOKED",
    "REASON_STORE_UNAVAILABLE",
    "REASON_UNKNOWN_KEY",
    "REASON_VERIFIER_UNAVAILABLE",
    "REASON_WATER",
    "SIG_LINE_1",
    "StartupRefused",
    "Verdict",
    "apply",
    "build_signing_message",
    "denied",
    "forbidden",
    "limited",
    "open_runtime",
    "passed",
    "unavailable",
]
