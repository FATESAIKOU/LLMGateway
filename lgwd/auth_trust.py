"""信任庫（D2 PR-4；契約 C-402，design「信任庫與防重放庫分開」「名冊 reload」的「重啟」條）。

信任庫 `auth-trust.sqlite` 存三樣東西，而且**永遠在同一個 transaction 一起寫**（C-402）：

- **世代水位**：最後一次成功載入的名冊世代（防回退）。
- **撤銷墓碑**：每個已撤銷 key 的 `(key ID, 驗證後公鑰 32 bytes)`；只 `INSERT OR IGNORE`，
  **永不 DELETE、永不 UPDATE**（較高世代想刪墓碑、改回有效、或以新 key ID 重用同一把
  公鑰，都由 `auth_reload.decide` 整份拒絕）。
- **已驗證快照**：名冊的原始 bytes 與其內容雜湊（重啟時與名冊檔走同一套判斷）。

世代只有一個來源：`snapshot.generation`（低-3-4「`generation` 表與 `snapshot.generation`
擇一為準」）。`generation` 表是同一交易內同步更新的鏡像，只給外部讀者看，判斷不用它。

這個模組只新增：`lgwd` 的執行路徑（`main.py`／`http.py`）不 import 它；錯誤一律沿用
`auth_db` 的型別（`StoreMissing`／`StoreBusy`／`StoreUnavailable`），PR-6 全部對應成
`trust_unavailable`（REPORT-D2a §4）。只用標準函式庫。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lgwd.auth_db import DEFAULT_BUSY_TIMEOUT, AuthDb, open_rw
from lgwd.auth_registry import Registry, content_hash

#: 信任庫語意拒絕的分類（與 C-405 的白名單同一組字串）。
KIND_GENERATION_NOT_INCREASED = "generation_not_increased"

TOMBSTONE_KEY_BYTES = 32


class TrustReject(ValueError):
    """信任庫的語意拒絕（不是 SQLite 錯誤）；`kind` 是固定分類字串。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class TrustStateError(ValueError):
    """`TrustState` 的欄位自相矛盾（呼叫端的 bug，不進 status 檔）。"""


@dataclass(frozen=True)
class TrustState:
    """信任庫的一個一致快照（讀取時一次取完）。

    - `generation`：世代水位；沒有快照時為 None。
    - `snapshot_raw`／`snapshot_hash`：最後一份已驗證名冊的原始 bytes 與內容雜湊。
    - `tombstones`：`(key ID, 公鑰 32 bytes)`，依 key ID 排序；直接餵
      `auth_registry.check_tombstones`（PR-2 接受的形狀，見低-2-2／低-3-4）。

    不變式：三個快照欄位同時有值或同時為 None。
    """

    generation: int | None
    snapshot_raw: bytes | None
    snapshot_hash: str | None
    tombstones: tuple[tuple[str, bytes], ...] = field(default=())

    def __post_init__(self) -> None:
        present = (
            self.snapshot_raw is not None,
            self.snapshot_hash is not None,
            self.generation is not None,
        )
        if len(set(present)) != 1:
            raise TrustStateError("snapshot fields must be present together")

    @property
    def has_snapshot(self) -> bool:
        """信任庫裡有沒有已驗證快照（剛 `auth-store init` 的庫沒有）。"""
        return self.snapshot_raw is not None

    def tombstone_list(self) -> list[tuple[str, bytes]]:
        """`check_tombstones` 直接可用的清單（每次給新 list，呼叫端改不到內部）。"""
        return [(key_id, key) for key_id, key in self.tombstones]


EMPTY_TRUST_STATE = TrustState(generation=None, snapshot_raw=None, snapshot_hash=None)


class TrustStore:
    """信任庫的唯一讀寫介面；一個行程一條連線（`auth_db.open_rw`，lgwd 不建庫）。

    寫入只有 `commit` 與 D5 CLI 專用的 `reset_to`（後者只差在不在交易內檢查世代
    是否增加，見該方法的 docstring）：兩者都在一個 `BEGIN IMMEDIATE` 交易內寫世代
    水位、補墓碑、換快照，交易成功才把新的 `TrustState` 回給呼叫端（C-402）。
    """

    def __init__(self, path: str | None = None, *, db: AuthDb | None = None,
                 busy_timeout: float = DEFAULT_BUSY_TIMEOUT):
        if db is None and path is None:
            raise ValueError("TrustStore needs a path or a db")
        self.path = None if path is None else str(path)
        self.busy_timeout = float(busy_timeout)
        self._db = db if db is not None else open_rw(path, busy_timeout=busy_timeout)

    @property
    def db(self) -> AuthDb:
        return self._db

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "TrustStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- 讀 ---

    def state(self) -> TrustState:
        """一次讀完世代水位、快照與墓碑（沒有快照＝空狀態，不是錯誤）。

        兩次 SELECT 不在同一個交易內，所以「一定是同一代」只在「單一寫入者、且呼叫端與
        `commit` 在同一把載入鎖下」時成立（watcher 的 `load_initial`／`tick` 就是這樣
        呼叫）；D5 的 CLI 在 lgwd 停止時讀，屆時也只有它一個連線。之後若出現多寫入者，
        要包成一個唯讀交易（低-2，目前沒有這個需求）。
        """
        rows = self._db.read(
            "SELECT generation, content_hash, raw FROM snapshot WHERE id = 1"
        )
        tombstones = tuple(
            (str(row[0]), bytes(row[1]))
            for row in self._db.read(
                "SELECT key_id, key FROM tombstones ORDER BY key_id"
            )
        )
        if not rows:
            return TrustState(None, None, None, tombstones)
        generation, stored_hash, raw = rows[0]
        return TrustState(
            generation=int(generation),
            snapshot_raw=bytes(raw),
            snapshot_hash=str(stored_hash),
            tombstones=tombstones,
        )

    # --- 寫（唯一寫入點） ---

    def commit(self, registry: Registry, raw: bytes, hash: str) -> TrustState:
        """同一交易寫入世代水位、補墓碑、換快照（C-402）。

        - 墓碑只 `INSERT OR IGNORE`：同一 key ID 已經有墓碑就保留原本那一份 bytes，
          永不刪除、永不覆寫（較高世代不能讓已撤銷的 key 復活）。
        - 世代只增：目前快照的世代 ≥ 這份名冊的世代就拒絕（`TrustReject`），整個交易
          rollback，庫裡什麼都不變。
        - 墓碑的**內容**是否合法（刪墓碑、改回有效、新 ID 重用）由 `auth_reload.decide`
          這一個決策函式先驗（C-401）；這裡只保證只增。

        `hash` 必須等於 `auth_registry.content_hash(raw)`，否則是呼叫端的 bug（`ValueError`）。
        """
        if not isinstance(registry, Registry):
            raise TypeError("registry must be a Registry")
        raw = bytes(raw)
        expected = content_hash(raw)
        if hash != expected:
            raise ValueError("content hash does not match the raw bytes")
        with self._db.txn() as cur:
            row = cur.execute("SELECT generation FROM snapshot WHERE id = 1").fetchone()
            current = None if row is None else int(row[0])
            if current is not None and registry.generation <= current:
                raise TrustReject(KIND_GENERATION_NOT_INCREASED)
            for key in registry.keys:
                if key.status == "revoked":
                    cur.execute(
                        "INSERT OR IGNORE INTO tombstones (key_id, key) VALUES (?, ?)",
                        (key.key_id, key.public_key),
                    )
            cur.execute(
                "INSERT INTO snapshot (id, generation, content_hash, raw)"
                " VALUES (1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  generation = excluded.generation,"
                "  content_hash = excluded.content_hash,"
                "  raw = excluded.raw",
                (registry.generation, hash, raw),
            )
            # 鏡像：同一交易內同步，外部讀者（D5 診斷）看到的水位不會落後；判斷只用
            # snapshot.generation。
            cur.execute(
                "INSERT INTO generation (id, generation) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET generation = excluded.generation",
                (registry.generation,),
            )
            # 回傳值就在交易內組出來（不再另外讀一次）：提交已經成功，這一讀若失敗會讓
            # 呼叫端誤以為沒提交、記憶體快照停在舊世代。
            tombstones = tuple(
                (str(row[0]), bytes(row[1]))
                for row in cur.execute(
                    "SELECT key_id, key FROM tombstones ORDER BY key_id"
                ).fetchall()
            )
        return TrustState(registry.generation, raw, hash, tombstones)

    # --- 只給 D5 CLI 的例外路徑（不改上面的世代守門） ---

    def reset_to(self, registry: Registry, raw: bytes, hash: str) -> TrustState:
        """把世代水位**與**已驗證快照在同一個交易內一起換掉；回傳新的 `TrustState`。

        **這是繞過防回退的入口，只給 `auth-registry reset-generation` 用，而且只能在
        lgwd 停止時呼叫**（呼叫端必須先取 `auth-store.lock` 的 `LOCK_EX`）。它與
        `commit` 的差別只有一處：
        **不檢查新世代是否大於目前水位**（reset-generation 的用途就是把誤設過高的
        水位換回可信的較低世代），其餘完全相同——同一個交易、墓碑只
        `INSERT OR IGNORE`、快照寫入後才回傳。

        `commit` 的世代守門**不得**為了這個用途放寬：一般 reload／install 仍然只能
        往上，否則撤銷與防回退的保證會一起失效（REPORT-D45-arch §②）。
        """
        if not isinstance(registry, Registry):
            raise TypeError("registry must be a Registry")
        raw = bytes(raw)
        expected = content_hash(raw)
        if hash != expected:
            raise ValueError("content hash does not match the raw bytes")
        with self._db.txn() as cur:
            for key in registry.keys:
                if key.status == "revoked":
                    cur.execute(
                        "INSERT OR IGNORE INTO tombstones (key_id, key) VALUES (?, ?)",
                        (key.key_id, key.public_key),
                    )
            cur.execute(
                "INSERT INTO snapshot (id, generation, content_hash, raw)"
                " VALUES (1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  generation = excluded.generation,"
                "  content_hash = excluded.content_hash,"
                "  raw = excluded.raw",
                (registry.generation, hash, raw),
            )
            cur.execute(
                "INSERT INTO generation (id, generation) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET generation = excluded.generation",
                (registry.generation,),
            )
            tombstones = tuple(
                (str(row[0]), bytes(row[1]))
                for row in cur.execute(
                    "SELECT key_id, key FROM tombstones ORDER BY key_id"
                ).fetchall()
            )
        return TrustState(registry.generation, raw, hash, tombstones)


__all__ = [
    "EMPTY_TRUST_STATE",
    "KIND_GENERATION_NOT_INCREASED",
    "TOMBSTONE_KEY_BYTES",
    "TrustReject",
    "TrustState",
    "TrustStateError",
    "TrustStore",
]
