"""status：看目前版本、上一版與兩個 unit 的狀態（不用 root、不改任何東西）。"""

from __future__ import annotations

from . import units
from .layout import UNIT_NAMES

LABEL_WIDTH = 25


def run(layout, *, out=None) -> int:
    out = print if out is None else out
    rows = [
        ("current", layout.installed_tag() or "（沒有）"),
        ("previous", layout.previous_tag() or "（沒有）"),
    ]
    for unit in UNIT_NAMES:
        rows.append((unit, units.unit_state(layout, unit)))
    for label, value in rows:
        out(f"{label:<{LABEL_WIDTH}} {value}")
    return 0
