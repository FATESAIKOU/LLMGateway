"""能力旗標（``deploy/capabilities.json``）：探針要用 old 還是 new 的判法（#246）。

載體是**版本目錄裡的追蹤檔**：它跟著 tag 一起取碼，``current``／``previous`` 由
``release.write_link`` 以 symlink＋``os.replace`` 原子切換，所以任何讀者在一次
open 裡只會看到舊版或新版其中一份旗標，部署器不必另外寫任何東西。

解析結果四態：

- ``ok``：一般檔、≤ 4096 bytes、UTF-8 JSON、頂層 object 的鍵恰好是 ``schema``
  與 ``probe``、``schema`` 是整數 1（``True`` 不算整數）、``probe`` 是
  ``"healthz"``。
- ``missing``：路徑不存在（ENOENT）。回滾到沒有這個檔的舊版時就是這一態，
  屬正常狀態。
- ``unreadable``：權限等 OSError（EACCES…）。
- ``invalid``：其他任何形狀（多餘的鍵、``schema`` 不是 1、未知的 ``probe``、
  重複鍵、超過上限、最後一段是 symlink、是目錄…）。

模式：**只有 ``ok`` 才是 ``new``，其他三態一律 ``old``**。``old`` 對新舊服務都
能正確判活（先探 ``/healthz``，404 才退回 list），``new`` 對舊服務會一直失敗；
異常一律降為 old 是為了不讓隧道因為誤判而反覆重建（反覆中斷 4710）。

開檔時跟隨路徑上的 ``current`` symlink，但**最後一段用 ``O_NOFOLLOW``**：旗標
本身不可以是 symlink，免得被指到別處。

依賴方向：只用標準函式庫與 ``layout``（取路徑）；``probe`` 不依賴這裡（模式由
呼叫方明確傳入）。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass

CAPABILITIES_REL = os.path.join("deploy", "capabilities.json")
MAX_BYTES = 4096
NEW = "new"
OLD = "old"


@dataclass(frozen=True)
class Capability:
    """一次讀取的結果：``state`` 是四態之一，``mode`` 是 ``new``／``old``。"""

    state: str
    mode: str


MISSING = Capability("missing", OLD)
UNREADABLE = Capability("unreadable", OLD)
INVALID = Capability("invalid", OLD)
OK = Capability("ok", NEW)


class _DuplicateKey(ValueError):
    """JSON 物件有重複的鍵（``json`` 預設只留最後一個，這裡一律算 invalid）。"""


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise _DuplicateKey()
        seen.add(key)
    return dict(pairs)


def _parse(raw: bytes) -> Capability:
    """把檔案的位元組解析成四態（不做 I/O）。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return INVALID
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return INVALID
    if not isinstance(data, dict) or set(data) != {"schema", "probe"}:
        return INVALID
    schema = data["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
        return INVALID
    probe = data["probe"]
    if not isinstance(probe, str) or probe != "healthz":
        return INVALID
    return OK


def read(version_dir: str) -> Capability:
    """讀 ``version_dir`` 的旗標；**不丟例外**（任何問題都折成四態之一）。"""
    path = os.path.join(version_dir, CAPABILITIES_REL)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return MISSING
    except PermissionError:
        return UNREADABLE
    except OSError:
        # ELOOP（最後一段是 symlink）、ENOTDIR、EACCES 以外的 OSError…
        return INVALID
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            return UNREADABLE
        if not stat.S_ISREG(info.st_mode):
            return INVALID
        if info.st_size > MAX_BYTES:
            return INVALID
        try:
            with os.fdopen(fd, "rb", closefd=False) as fh:
                raw = fh.read(MAX_BYTES + 1)
        except PermissionError:
            return UNREADABLE
        except OSError:
            return INVALID
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        return INVALID
    return _parse(raw)


def current(layout) -> Capability:
    """``current``（用中的那一版）的旗標：= ``read(layout.current_link)``。"""
    return read(layout.current_link)


def precheck(report, cap: Capability) -> bool:
    """切換前的驗證（K-3）：回「能不能切這版」。

    ``invalid``／``unreadable`` 一律 fail（不切 current、不動 unit）——損毀的旗標
    會讓隧道用 ``old`` 探，而舊服務的 ``/healthz`` 是 404、新服務的 list 也可能
    不再回答；先擋下來比切過去再回滾便宜。``missing`` 是正常的（舊 tag 沒有這個
    檔），也就是 ``old``。

    ``report`` 由呼叫方傳入（這裡不 import ``report``，維持只依賴標準庫）。
    """
    if cap.state == "invalid":
        report.fail("capabilities", "損毀（不切版）")
        return False
    if cap.state == "unreadable":
        report.fail("capabilities", "讀不到（不切版）")
        return False
    if cap.state == "missing":
        report.ok("capabilities", "無（old）")
    else:
        report.ok("capabilities", "new")
    return True
