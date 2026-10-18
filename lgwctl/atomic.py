"""檔案的原子寫入（同目錄暫存 → chmod → rename）：install／units／sync 共用。

暫存檔與目標同目錄（同一個檔案系統），rename 是原子的：寫到一半被中斷也
不會留下半個檔，看到的都是完整的舊檔或完整的新檔。
"""

from __future__ import annotations

import os
import tempfile


def write_bytes(path: str, data: bytes, mode: int = 0o644, *,
                owner: tuple[int, int] | None = None) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, owner[0], owner[1])
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_text(path: str, text: str, mode: int = 0o644, *,
               owner: tuple[int, int] | None = None) -> None:
    write_bytes(path, text.encode("utf-8"), mode, owner=owner)
