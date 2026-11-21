"""唯讀顯示服務與選用隧道的狀態。"""
from __future__ import annotations
import os

from . import units, probe
from .layout import SERVICE_NAME, TUNNEL_SERVICE_NAME

LABEL_WIDTH = 25


def run(layout, *, out=None, no_remote=False) -> int:
    out = print if out is None else out
    rows = [("current", layout.installed_tag() or "（沒有）"),
            ("previous", layout.previous_tag() or "（沒有）"),
            (SERVICE_NAME, units.unit_state(layout, SERVICE_NAME))]
    if not no_remote:
        state = units.unit_state(layout, TUNNEL_SERVICE_NAME)
        if state != "missing" or layout.tunnel_configured() or layout.tunnel_follow_state()[0] == "unreadable" or os.path.isfile(layout.unit_file(TUNNEL_SERVICE_NAME)):
            rows.append((TUNNEL_SERVICE_NAME, state))
            follow, gen = layout.tunnel_follow_state()
            if follow != "unconfigured" or layout.tunnel_fetch_state() != "none":
                if follow == "ok":
                    follow = "ok (gen=" + (str(gen) if gen is not None else "pending") + ")"
                elif follow == "unreadable":
                    follow = "unreadable（需 root 或 llmgw 才能讀）"
                fetch = layout.tunnel_fetch_state()
                if fetch == "unreadable":
                    fetch = "unreadable（需 root 或 llmgw 才能讀）"
                rows.extend((("tunnel-follow", follow), ("tunnel-fetch", fetch),
                             ("tunnel-token", layout.tunnel_token_state())))
            level, _ = probe.remote(layout)
            rows.append(("list-remote", level))
    for label, value in rows:
        out(f"{label:<{LABEL_WIDTH}} {value}")
    return 0
