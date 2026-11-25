"""認證狀態檔案的路徑推導（D2 PR-3，契約 C-310）。

lgwd 與 D5 的 `llmgwctl` 共用同一份：由單一設定 `auth_state_dir` 推導兩庫、鎖檔、
status 檔、`allowed_signers.<世代>`、`sshsig/` 與審計檔，不另開旗標（design
「設定（#247 G-3）」）。路徑一律絕對；呼叫端給相對路徑就是設定錯誤。

時間一律經過注入的 `lgwd.clock.Clock`（整數秒＝`floor(now.timestamp())`）；本模組
不直接呼叫 `time.time`，也不用 `datetime.now`。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from lgwd.clock import Clock


DEFAULT_STATE_DIR = "/var/lib/llmgateway"
DEFAULT_REGISTRY_PATH = "/etc/llmgateway/auth-registry.json"

REPLAY_DB_NAME = "auth-replay.sqlite"
TRUST_DB_NAME = "auth-trust.sqlite"
STORE_LOCK_NAME = "auth-store.lock"
STATUS_NAME = ".auth-registry.status"
BUSY_STATUS_NAME = ".auth-busy.status"
AUDIT_LOG_NAME = "auth-audit.log"
SSHSIG_DIR_NAME = "sshsig"

# 名冊世代是 1～2^53−1 的整數（design「名冊檔格式」）。
MAX_GENERATION = 2**53 - 1


def now_seconds(clock: Clock) -> int:
    """整數秒：floor(clock.now().timestamp())。交易內只讀一次，整個交易共用。"""
    return int(math.floor(clock.now().timestamp()))


def _absolute(path: str, what: str) -> str:
    value = os.fspath(path)
    if not os.path.isabs(value):
        raise ValueError("{0} must be an absolute path".format(what))
    return os.path.abspath(value)


@dataclass(frozen=True)
class AuthPaths:
    """`auth_state_dir` 的展開；`registry` 不在狀態目錄底下，另由設定給。"""

    state_dir: str
    registry: str = DEFAULT_REGISTRY_PATH

    @property
    def replay_db(self) -> str:
        return os.path.join(self.state_dir, REPLAY_DB_NAME)

    @property
    def trust_db(self) -> str:
        return os.path.join(self.state_dir, TRUST_DB_NAME)

    @property
    def store_lock(self) -> str:
        return os.path.join(self.state_dir, STORE_LOCK_NAME)

    @property
    def status(self) -> str:
        return os.path.join(self.state_dir, STATUS_NAME)

    @property
    def busy_status(self) -> str:
        return os.path.join(self.state_dir, BUSY_STATUS_NAME)

    @property
    def audit_log(self) -> str:
        return os.path.join(self.state_dir, AUDIT_LOG_NAME)

    @property
    def sshsig_dir(self) -> str:
        return os.path.join(self.state_dir, SSHSIG_DIR_NAME)

    def allowed_signers(self, generation: int) -> str:
        """`allowed_signers.<世代>`；世代不合法就拒絕組路徑。"""
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("generation must be an integer")
        if generation < 1 or generation > MAX_GENERATION:
            raise ValueError("generation out of range")
        return os.path.join(self.state_dir, "allowed_signers.{0}".format(generation))

    def broken_path(self, path: str, clock: Clock) -> str:
        """事故檔名：`<原檔名>.broken-<整數秒>`（design「時鐘事故」(v)）。

        給 D5 的 `recover-store` 把損毀的 replay 庫改名留作分析用；時間經注入的
        Clock，`-wal`／`-shm` 由呼叫端以同樣後綴處理（`sqlite_sidecars`）。
        """
        base = os.path.basename(os.fspath(path))
        return os.path.join(os.path.dirname(os.fspath(path)) or ".", "{0}.broken-{1}".format(
            base, now_seconds(clock)))


def sqlite_sidecars(path: str) -> tuple[str, str]:
    """SQLite 的 `-wal`／`-shm` 檔名（改名或清除損毀庫時要一起處理）。"""
    value = os.fspath(path)
    return value + "-wal", value + "-shm"


def auth_paths(state_dir: str = DEFAULT_STATE_DIR, *, registry: str = DEFAULT_REGISTRY_PATH) -> AuthPaths:
    """建 `AuthPaths`；兩個路徑都必須是絕對路徑（design「設定」）。"""
    return AuthPaths(
        state_dir=_absolute(state_dir, "auth_state_dir"),
        registry=_absolute(registry, "auth_registry"),
    )
