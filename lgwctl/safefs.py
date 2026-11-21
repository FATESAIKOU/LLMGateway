"""不信任的路徑上的檔案操作：目錄 fd＋``O_NOFOLLOW``＋``fstat``＋fd-relative（#227）。

``tunnel-apply`` 是 root 在跑，但它碰的 ``FOLLOW_FILE``、``.tunnel_known_hosts`` 在
llmgw 可寫的 state 目錄，drop-in 目錄也可能被換掉。所以：

- 父目錄以 ``O_DIRECTORY|O_NOFOLLOW`` 開成 fd（父目錄是 symlink＝拒絕）；
- 之後的讀、寫、改名、刪除都用 ``dir_fd=``，最終分量不跟 symlink；
- 讀檔 ``O_NOFOLLOW|O_NONBLOCK`` 開（FIFO 不會卡住），``fstat`` 確認是一般檔、
  大小有上限，才讀。

``TEST_HOOK`` 只給測試：在每次 ``open`` 前呼叫（模擬檢查與開啟之間被換掉）。
"""

from __future__ import annotations

import os
import stat

MAX_BYTES = 1 << 20
TEST_HOOK = None

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class UnsafePath(OSError):
    """不是一般檔／太大／父目錄不安全。"""


def open_dir(path: str, *, create: bool = False) -> int:
    """開父目錄 fd；最終分量是 symlink 或不是目錄就丟 ``OSError``。"""
    try:
        return os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        if not create:
            raise
    os.makedirs(path, exist_ok=True)
    return os.open(path, _DIR_FLAGS)


def split(path: str) -> tuple:
    return os.path.dirname(path) or ".", os.path.basename(path)


def lstat(dir_fd: int, name: str):
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def read_regular(dir_fd: int, name: str, limit: int = MAX_BYTES) -> tuple:
    """回 ``(bytes, stat_result)``；最終分量是 symlink／非一般檔／超過上限都丟 ``OSError``。"""
    if TEST_HOOK is not None:
        TEST_HOOK(name)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath("not a regular file")
        if info.st_size > limit:
            raise UnsafePath("too large")
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > limit:
        raise UnsafePath("too large")
    return data, info


def read_path(path: str, limit: int = MAX_BYTES) -> tuple:
    """父目錄 nofollow 開、最終分量 nofollow 讀；回 ``(bytes, stat_result)``。"""
    directory, name = split(path)
    dir_fd = open_dir(directory)
    try:
        return read_regular(dir_fd, name, limit)
    finally:
        os.close(dir_fd)


def write_atomic(dir_fd: int, name: str, data: bytes, mode: int, *, owner=None) -> None:
    """同目錄暫存（``O_EXCL|O_NOFOLLOW``）→ chmod/chown → ``rename``；取代而不跟隨既有的 symlink。"""
    tmp = f".{name}.{os.urandom(6).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, mode)
            if owner is not None:
                os.fchown(fd, owner[0], owner[1])
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass


def unlink(dir_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=dir_fd)


def rename(dir_fd: int, old: str, new: str) -> None:
    os.rename(old, new, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
