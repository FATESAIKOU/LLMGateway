"""upgrade：抓新 tag → policy dry-run → 切 current → 只 restart 服務 → 等本機
list 200；不是 200 就自動切回上一版（#35 spec）。rollback：切回 previous、
restart、跑 check。

升級只碰 ``llmgateway.service``，不碰隧道單位（隧道承載管理連線，重啟會斷線）；
成功後只留 current 與 previous 兩版。unit 檔不隨升級換（要換 unit 是 install
的事；見回報的「不確定」）。
"""

from __future__ import annotations

import os

from . import check, probe, release, units
from .layout import SERVICE_NAME, TAG_PATTERN, TAG_RE
from .report import Report
from .run import require_root


def _dry_run_policy(layout, tag_dir: str, report) -> bool:
    """用跑 llmgwctl 的這份 lgwd 規則驗當版 policy（不過就不切）。"""
    policy_path = os.path.join(tag_dir, "policy", "default.json")
    try:
        from lgwd.policy import PolicyError, load_policy
    except ImportError:
        report.fail("policy", "載不到 lgwd.policy（cwd 不在版本目錄？）")
        return False
    try:
        load_policy(policy_path)
    except PolicyError as exc:
        report.fail("policy", f"dry-run 不過（不切版）：{exc}")
        return False
    except Exception as exc:  # 未預期錯誤也只記種類
        report.fail("policy", f"dry-run 不過（不切版）：{type(exc).__name__}")
        return False
    report.ok("policy", "dry-run 通過")
    return True


def _restart_service(layout, report) -> bool:
    if not units.restart(layout, SERVICE_NAME):
        report.fail("restart", f"{SERVICE_NAME} restart 失敗")
        return False
    report.ok("restart", SERVICE_NAME)
    return True


def upgrade(layout, *, tag: str, repo: str | None = None,
            report: Report | None = None) -> int:
    report = report or Report()
    if not require_root(layout, report):
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

    if not _restart_service(layout, report):
        release.restore_links(layout, old_current, old_previous)
        return report.exit_code(todo_ok=True)

    if not probe.wait_local_ok(layout, layout.list_wait):
        # 自動回滾：切回切換前的兩個 link，再重啟一次（best effort）。
        release.restore_links(layout, old_current, old_previous)
        units.restart(layout, SERVICE_NAME)
        report.fail("list", f"本機 list 不是 200；已切回 {old_current or '（沒有上一版）'}")
        return 1

    report.ok("list", "200")
    removed = release.cleanup_old_versions(layout)
    report.ok("cleanup", "、".join(removed) + " 已移除" if removed else "沒有多餘版本")
    return report.exit_code(todo_ok=True)


def rollback(layout, *, report: Report | None = None, no_remote: bool = False) -> int:
    """切回 previous、restart、跑 check；回 check 的結束碼。"""
    report = report or Report()
    if not require_root(layout, report):
        return report.exit_code(todo_ok=True)
    old_current = layout.current_tag()
    previous = layout.previous_tag()
    if previous is None:
        report.fail("rollback", "沒有 previous 可回")
        return 1
    if not os.path.isdir(layout.tag_dir(previous)):
        report.fail("rollback", f"previous 的目錄不在（{previous}）")
        return 1

    try:
        release.write_link(layout, layout.current_link, layout.tag_dir(previous))
        if old_current:
            release.write_link(layout, layout.previous_link, layout.tag_dir(old_current))
    except OSError as exc:
        report.fail("rollback", f"切 link 失敗（{type(exc).__name__}）")
        return 1
    report.ok("current", f"{old_current or '（沒有）'} → {previous}")

    if not _restart_service(layout, report):
        return report.exit_code(todo_ok=True)
    probe.wait_local_ok(layout, layout.list_wait)
    return check.run(layout, report=report, no_remote=no_remote)
