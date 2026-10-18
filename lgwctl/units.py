"""systemd unit 檔的安裝，與 systemctl 的薄包裝（專案裡唯一碰 systemctl 的地方）。

unit 一律從當版目錄的 ``deploy/units/`` 裝到 ``<prefix>/etc/systemd/system/``，
檔案內容不經過這裡改寫（版本自己的 unit 就是版本的一部分）。install 與
**upgrade 都會裝 unit**（unit 是版本的一部分，中-2）；upgrade 換之前先
``snapshot_units``，任何一步失敗就把 unit 與 current／previous 一起還原。
"""

from __future__ import annotations

import os

from . import atomic
from .layout import UNIT_NAMES
from .run import RunError, run

UNITS_REL_DIR = os.path.join("deploy", "units")


def copy_file(src: str, dst: str, mode: int = 0o644) -> None:
    """把版本裡的 unit 檔複製到 unit 目錄（原子；內容不經改寫）。"""
    with open(src, "rb") as fh:
        atomic.write_bytes(dst, fh.read(), mode)


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


def snapshot_units(layout) -> dict:
    """換 unit 之前的備份（unit 名 → 檔案內容；原本沒有那個檔是 ``None``）。"""
    saved: dict = {}
    for name in UNIT_NAMES:
        try:
            with open(layout.unit_file(name), "rb") as fh:
                saved[name] = fh.read()
        except OSError:
            saved[name] = None
    return saved


def restore_units(layout, saved: dict) -> bool:
    """把備份寫回（原本沒有的就刪掉剛裝的）；回「是否全部成功」。

    升級失敗回復時用：unit 檔要跟 current 一起回到切換前的樣子（中-2）。
    """
    ok = True
    for name in UNIT_NAMES:
        path = layout.unit_file(name)
        try:
            if saved.get(name) is None:
                if os.path.lexists(path):
                    os.unlink(path)
            else:
                atomic.write_bytes(path, saved[name], 0o644)
        except OSError:
            ok = False
    return ok


# -- systemctl（system 層；不需要任何 --user 或環境變數）-----------------------

def _systemctl(layout, *args):
    return run([layout.systemctl, *args], env=layout.child_env())


def unit_state(layout, unit: str) -> str:
    """``active``｜``inactive``｜``missing``｜``unknown``（systemctl 不能用）。

    systemd 的 ``is-active``：0＝active、3＝inactive、**4＝unit 檔不存在**
    （原始碼的 not_found 分支）。4 是「確定沒裝」，不是「無法確認」（中-1）。
    """
    try:
        proc = _systemctl(layout, "is-active", unit)
    except RunError:
        return "unknown"
    if proc.returncode == 0:
        return "active"
    if proc.returncode == 3:
        return "inactive"
    if proc.returncode == 4:
        return "missing"
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
