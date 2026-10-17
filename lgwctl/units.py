"""systemd unit 檔的安裝，與 systemctl 的薄包裝（專案裡唯一碰 systemctl 的地方）。

unit 一律從當版目錄的 ``deploy/units/`` 裝到 ``<prefix>/etc/systemd/system/``，
檔案內容不經過這裡改寫（版本自己的 unit 就是版本的一部分）。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from .layout import UNIT_NAMES
from .run import RunError, run

UNITS_REL_DIR = os.path.join("deploy", "units")


def copy_file(src: str, dst: str, mode: int = 0o644) -> None:
    """同目錄暫存 → chmod → rename（原子，不留暫存）。"""
    directory = os.path.dirname(dst) or "."
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as fh:
            shutil.copyfileobj(fh, out)
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def unit_source(tag_dir: str, name: str) -> str:
    return os.path.join(tag_dir, UNITS_REL_DIR, name)


def install_unit_files(layout, tag_dir: str, report) -> bool:
    """把兩個 system unit 檔裝進 unit 目錄；缺檔或寫不進去回 False。"""
    for name in UNIT_NAMES:
        if not os.path.isfile(unit_source(tag_dir, name)):
            report.fail("units", f"版本裡缺 {UNITS_REL_DIR}/{name}")
            return False
    try:
        for name in UNIT_NAMES:
            copy_file(unit_source(tag_dir, name), layout.unit_file(name), 0o644)
    except OSError as exc:
        report.fail("units", f"寫 unit 失敗（{type(exc).__name__}）")
        return False
    report.ok("units", "、".join(UNIT_NAMES))
    return True


# -- systemctl（system 層；不需要任何 --user 或環境變數）-----------------------

def _systemctl(layout, *args):
    return run([layout.systemctl, *args], env=layout.child_env())


def unit_state(layout, unit: str) -> str:
    """``active``｜``inactive``｜``unknown``（systemctl 不能用時 unknown）。"""
    try:
        proc = _systemctl(layout, "is-active", unit)
    except RunError:
        return "unknown"
    if proc.returncode == 0:
        return "active"
    if proc.returncode == 3:
        return "inactive"
    return "unknown"


def is_active(layout, unit: str) -> bool:
    return unit_state(layout, unit) == "active"


def daemon_reload(layout) -> bool:
    try:
        return _systemctl(layout, "daemon-reload").returncode == 0
    except RunError:
        return False


def enable(layout, unit: str) -> bool:
    try:
        return _systemctl(layout, "enable", unit).returncode == 0
    except RunError:
        return False


def start(layout, unit: str) -> bool:
    try:
        return _systemctl(layout, "start", unit).returncode == 0
    except RunError:
        return False


def restart(layout, unit: str) -> bool:
    try:
        return _systemctl(layout, "restart", unit).returncode == 0
    except RunError:
        return False
