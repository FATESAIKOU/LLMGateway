"""upgrade：抓新 tag → policy dry-run（新 tag 的 loader）→ 切 current → 換 unit
＋daemon-reload → 只 restart 服務 → 等本機 list 200；unit／reload／restart／list
任何一步不對就把 current、previous 與 unit 檔一起切回切換前、非零結束（#35 spec、
中-2）。rollback：切回 previous、restart、跑 check。

升級只碰 ``llmgateway.service``，不碰隧道單位（隧道承載管理連線，重啟會斷線）；
成功後只留 current 與 previous 兩版。unit 檔是版本的一部分：升級時換成新 tag 的
（含 ``daemon-reload``），回復時連 unit 檔一起還原。
"""

from __future__ import annotations

import os

from . import check, probe, release, run as runmod, units
from .layout import SERVICE_NAME, TAG_PATTERN, TAG_RE
from .report import Report

# policy dry-run 的子行程：cwd 指到新 tag 目錄（``-c`` 會把 cwd 放進
# sys.path[0]），所以 import 到的是**那一版**的 lgwd.policy，不是跑 llmgwctl
# 的這份（低-5）。錯誤只印一行（PolicyError 的訊息／例外種類），不 traceback。
DRY_RUN_SNIPPET = """\
import sys
from lgwd.policy import PolicyError, load_policy

try:
    load_policy(sys.argv[1])
except PolicyError as exc:
    print(exc)
    sys.exit(1)
except Exception as exc:
    print(type(exc).__name__)
    sys.exit(1)
"""


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _dry_run_policy(layout, tag_dir: str, report) -> bool:
    """用**新 tag 目錄裡的 lgwd** 驗那一版的 policy（不過就不切）。"""
    policy_path = os.path.join(tag_dir, "policy", "default.json")
    try:
        proc = runmod.run([layout.python3, "-c", DRY_RUN_SNIPPET, policy_path],
                          cwd=tag_dir, timeout=runmod.DEFAULT_TIMEOUT)
    except runmod.RunError as exc:
        report.fail("policy", f"dry-run 跑不起來（{layout.python3} {exc.kind}）")
        return False
    if proc.returncode != 0:
        detail = (_first_line(proc.stdout) or _last_line(proc.stderr) or "未知原因")
        report.fail("policy", f"dry-run 不過（不切版）：{detail}")
        return False
    report.ok("policy", "dry-run 通過")
    return True


def _restart_service(layout, report) -> bool:
    if not units.restart(layout, SERVICE_NAME):
        report.fail("restart", f"{SERVICE_NAME} restart 失敗")
        return False
    report.ok("restart", SERVICE_NAME)
    return True


def _rollback_after_switch(layout, saved_units: dict,
                           old_current: str | None, old_previous: str | None,
                           *, restart: bool, report: Report) -> int:
    """切換之後才發現不對：current／previous 與 unit 檔都回到切換前，回 1。

    ``restart=True`` 表示服務已經動過（或 list 已探過），回復後要再把舊版跑
    起來；每一步都只記一行 fail（低-6：restore 失敗也不 traceback）。
    """
    try:
        release.restore_links(layout, old_current, old_previous)
        report.ok("current", f"已切回 {old_current or '（沒有上一版）'}")
    except OSError as exc:
        report.fail("rollback", f"切回舊版失敗（{type(exc).__name__}）")
    if not units.restore_units(layout, saved_units):
        report.fail("rollback", "unit 檔寫回失敗")
    if not units.daemon_reload(layout):
        report.fail("rollback", f"{layout.systemctl} daemon-reload 失敗（unit 檔已寫回）")
    if restart and old_current is not None:
        units.restart(layout, SERVICE_NAME)   # best effort：把舊版跑回來
    return 1


def upgrade(layout, *, tag: str, repo: str | None = None,
            report: Report | None = None) -> int:
    report = report or Report()
    if not runmod.require_root(layout, report):
        return report.exit_code(todo_ok=True)
    if not TAG_RE.match(tag or ""):
        report.fail("tag", f"格式不符（要 {TAG_PATTERN}）：{tag}")
        return report.exit_code(todo_ok=True)

    tag_dir = release.fetch(layout, tag, repo, report)
    if tag_dir is None:
        return report.exit_code(todo_ok=True)
    if not _dry_run_policy(layout, tag_dir, report):
        return report.exit_code(todo_ok=True)

    old_current = layout.current_tag()
    old_previous = layout.previous_tag()
    try:
        if old_current != tag:
            old = release.switch_current(layout, tag)
            report.ok("current", f"{old or '（沒有）'} → {tag}")
        else:
            report.ok("current", f"{tag}（已經是同版）")
    except OSError as exc:
        report.fail("current", f"切 current 失敗（{type(exc).__name__}）")
        return report.exit_code(todo_ok=True)

    # unit 是版本的一部分：換新版的 unit 再 daemon-reload（中-2）；之後任何一步
    # 失敗，link 與 unit 檔都回到切換前的狀態。
    saved_units = units.snapshot_units(layout)
    if not units.install_unit_files(layout, tag_dir, report):
        return _rollback_after_switch(layout, saved_units, old_current, old_previous,
                                      restart=False, report=report)
    if not units.daemon_reload(layout):
        report.fail("daemon-reload", f"{layout.systemctl} daemon-reload 失敗")
        return _rollback_after_switch(layout, saved_units, old_current, old_previous,
                                      restart=False, report=report)

    if not _restart_service(layout, report):
        return _rollback_after_switch(layout, saved_units, old_current, old_previous,
                                      restart=True, report=report)

    if not probe.wait_local_ok(layout, layout.list_wait):
        report.fail("list", "本機 list 不是 200")
        return _rollback_after_switch(layout, saved_units, old_current, old_previous,
                                      restart=True, report=report)

    report.ok("list", "200")
    removed = release.cleanup_old_versions(layout)
    report.ok("cleanup", "、".join(removed) + " 已移除" if removed else "沒有多餘版本")
    return report.exit_code(todo_ok=True)


def rollback(layout, *, report: Report | None = None, no_remote: bool = False) -> int:
    """切回 previous、換回它的 unit 檔＋daemon-reload、restart、跑 check（中-3）。

    unit 是版本的一部分：upgrade 換了新 tag 的 unit，rollback 也要把 previous
    的裝回來——不然舊程式跑在新 unit 的 ExecStart／sandbox 下，可能起不來。
    previous 沒有 ``deploy/units``（舊版格式）就**在切 link 之前**拒絕。
    """
    report = report or Report()
    if not runmod.require_root(layout, report):
        return report.exit_code(todo_ok=True)
    old_current = layout.current_tag()
    previous = layout.previous_tag()
    if previous is None:
        report.fail("rollback", "沒有 previous 可回")
        return 1
    previous_dir = layout.tag_dir(previous)
    if not os.path.isdir(previous_dir):
        report.fail("rollback", f"previous 的目錄不在（{previous}）")
        return 1
    # 中-3：先確認 previous 有 unit 檔（沒有就在切 link 之前停手）。
    missing = [name for name in units.UNIT_NAMES
               if not os.path.isfile(units.unit_source(previous_dir, name))]
    if missing:
        report.fail("rollback", f"{previous} 沒有 deploy/units 的 "
                                f"{'、'.join(missing)}；這一版無法用 rollback"
                                "（請手動處理或裝別的版）")
        return 1

    saved_units = units.snapshot_units(layout)
    # 失敗回復要用「切換前」的兩個 link（previous 是換之前的 previous）。
    try:
        release.write_link(layout, layout.current_link, layout.tag_dir(previous))
        if old_current:
            release.write_link(layout, layout.previous_link, layout.tag_dir(old_current))
    except OSError as exc:
        report.fail("rollback", f"切 link 失敗（{type(exc).__name__}）")
        return 1
    report.ok("current", f"{old_current or '（沒有）'} → {previous}")

    if not units.install_unit_files(layout, previous_dir, report):
        return _rollback_after_switch(layout, saved_units, old_current, previous,
                                      restart=False, report=report)
    if not units.daemon_reload(layout):
        report.fail("daemon-reload", f"{layout.systemctl} daemon-reload 失敗")
        return _rollback_after_switch(layout, saved_units, old_current, previous,
                                      restart=False, report=report)

    if not _restart_service(layout, report):
        return _rollback_after_switch(layout, saved_units, old_current, previous,
                                      restart=True, report=report)
    probe.wait_local_ok(layout, layout.list_wait)
    return check.run(layout, report=report, no_remote=no_remote)
