"""upgrade：抓新 tag → policy dry-run（新 tag 的 loader）→ 能力旗標檢查（#246）
→ 切 current → 換 unit ＋daemon-reload → 只 restart 服務 → 等本機探針 200；
unit／reload／restart／探針任何一步不對就把 current、previous 與 unit 檔一起切回
切換前、非零結束（#35 spec、中-2）。rollback：切回 previous、restart、跑 check。

本機探針的模式跟著「這一版」的能力旗標（``deploy/capabilities.json``）：新 tag 是
``new`` 就只打 ``/healthz``，是 ``old``（含缺檔）就先打 ``/healthz``、404 才退回
list。rollback 讀的是 previous 目錄的旗標；回滾是救急，旗標損毀**不擋**。

升級只碰 ``llmgateway.service``，不碰隧道單位（隧道承載管理連線，重啟會斷線）；
成功後只留 current 與 previous 兩版。unit 檔是版本的一部分：升級時換成新 tag 的
（含 ``daemon-reload``），回復時連 unit 檔一起還原。
"""

from __future__ import annotations

import os

from . import capability, check, mode_history, probe, release, run as runmod, units
from .layout import CHECK_TIMER_NAME, SERVICE_NAME, TAG_PATTERN, TAG_RE
from .report import Report

# policy dry-run 的子行程：cwd 指到新 tag 目錄（``-c`` 會把 cwd 放進
# sys.path[0]），所以 import 到的是**那一版**的 lgwd.policy，不是跑 llmgwctl
# 的這份（低-5）。錯誤只印一行（PolicyError 的訊息／例外種類），不 traceback。
DRY_RUN_SNIPPET = """\
import sys
from lgwd.policy import PolicyError, load_policy
import os

try:
    policy = load_policy(sys.argv[1])
    from lgwd.roletable import load_valid
    if load_valid(sys.argv[2], policy) is None:
        raise ValueError("invalid tag role table")
    if os.path.exists(sys.argv[3]):
        if load_valid(sys.argv[3], policy) is None:
            raise ValueError("invalid installed role table")
    else:
        print("installed role table absent: skipped")
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
        proc = runmod.run([layout.python3, "-c", DRY_RUN_SNIPPET, policy_path, os.path.join(tag_dir, "policy", "role-rotation.default.json"), layout.role_table_file],
                          cwd=tag_dir, timeout=runmod.DEFAULT_TIMEOUT)
    except runmod.RunError as exc:
        report.fail("policy", f"dry-run 跑不起來（{layout.python3} {exc.kind}）")
        return False
    if proc.returncode != 0:
        detail = (_first_line(proc.stdout) or _last_line(proc.stderr) or "未知原因")
        report.fail("policy", f"dry-run 不過（不切版）：{detail}")
        return False
    if _first_line(proc.stdout):
        report.ok("rolesync", _first_line(proc.stdout))
    report.ok("policy", "dry-run 通過")
    return True


def _restart_service(layout, report) -> bool:
    if not units.restart(layout, SERVICE_NAME):
        report.fail("restart", f"{SERVICE_NAME} restart 失敗")
        return False
    report.ok("restart", SERVICE_NAME)
    return True


def _enable_timer_after_units(layout, tag_dir: str, report) -> bool:
    """#289 C-984／C-985：目標版本有 timer 就 enable --now（已在跑就不動）。

    版本裡沒有這個 unit 就跳過（舊版目錄）。失敗由呼叫方決定回復。
    """
    if not os.path.isfile(units.unit_source(tag_dir, CHECK_TIMER_NAME)):
        return True
    if (units.enabled_state(layout, CHECK_TIMER_NAME) == "enabled"
            and units.is_active(layout, CHECK_TIMER_NAME)):
        report.ok("enable-timer", f"{CHECK_TIMER_NAME} 已在跑（不動它）")
        return True
    if not units.enable_now(layout, CHECK_TIMER_NAME):
        report.fail("enable-timer", f"{CHECK_TIMER_NAME} enable --now 失敗")
        return False
    report.ok("enable-timer", CHECK_TIMER_NAME)
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

    # K-3（#246）：切 current **之前**驗新 tag 的能力旗標。損毀／讀不到就停手
    # （不切 current、不動 unit、不 restart）；缺檔是正常的（舊 tag 沒有這個檔）。
    cap = capability.read(tag_dir)
    if not capability.precheck(report, cap):
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

    if not probe.wait_local_ok(layout, layout.list_wait, mode=cap.mode):
        report.fail("list", "本機探針不是 200")
        return _rollback_after_switch(layout, saved_units, old_current, old_previous,
                                      restart=True, report=report)

    report.ok("list", "200")
    # #289（架構師低-2）：timer 在 restart 與探針 ok **之後**才 enable --now。開機
    # 已超過 5 分鐘時，enable --now 會立刻觸發一次 check；太早啟用會剛好撞上
    # lgwd 重啟，讓那一次 check 失敗、unit 短暫進入 failed。
    if not _enable_timer_after_units(layout, tag_dir, report):
        return _rollback_after_switch(layout, saved_units, old_current, old_previous,
                                      restart=True, report=report)
    # #288：換版重啟成功、探針 ok 之後記模式；任何例外都只警告，不改結束碼。
    try:
        mode_history.record(layout, source="restart")
    except Exception as exc:  # 記錄不能讓重啟失敗（C-962 的縱深防禦）
        mode_history.warn_record_failure(exc)
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
    missing = [name for name in (units.SERVICE_NAME,)
               if not os.path.isfile(units.unit_source(previous_dir, name))]
    if missing:
        report.fail("rollback", f"{previous} 沒有 deploy/units 的 "
                                f"{'、'.join(missing)}；這一版無法用 rollback"
                                "（請手動處理或裝別的版）")
        return 1

    saved_units = units.snapshot_units(layout)
    # #246 K-3：回滾是救急，旗標壞掉**不擋**（照樣切回），探針一律照這一版的
    # 旗標決定模式；損毀時是 old，之後 check 的 list-local 會照實報 fail。
    cap = capability.read(previous_dir)
    if cap.state == "invalid":
        report.todo("capabilities", "損毀（照樣回滾，探針用 old）")
    elif cap.state == "unreadable":
        report.todo("capabilities", "讀不到（照樣回滾，探針用 old）")
    elif cap.state == "missing":
        report.ok("capabilities", "無（old）")
    else:
        report.ok("capabilities", "new")
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
    # #289 C-985：目標版本沒有的可選 unit（例如新版留下的 check timer）先
    # disable --now 再刪檔；目標版本有就照上面的 install 裝。
    if not units.remove_missing_optional_units(layout, previous_dir, report):
        return _rollback_after_switch(layout, saved_units, old_current, previous,
                                      restart=False, report=report)
    if not units.daemon_reload(layout):
        report.fail("daemon-reload", f"{layout.systemctl} daemon-reload 失敗")
        return _rollback_after_switch(layout, saved_units, old_current, previous,
                                      restart=False, report=report)

    if not _restart_service(layout, report):
        return _rollback_after_switch(layout, saved_units, old_current, previous,
                                      restart=True, report=report)
    if probe.wait_local_ok(layout, layout.list_wait, mode=cap.mode):
        # #289（架構師低-2）：timer 在 restart 與探針 ok 之後才 enable；理由同 upgrade。
        if not _enable_timer_after_units(layout, previous_dir, report):
            return _rollback_after_switch(layout, saved_units, old_current, previous,
                                          restart=True, report=report)
        # #288：rollback 重啟成功、探針 ok 之後記模式；任何例外都只警告。
        try:
            mode_history.record(layout, source="restart")
        except Exception as exc:  # 記錄不能讓重啟失敗（C-962 的縱深防禦）
            mode_history.warn_record_failure(exc)
    return check.run(layout, report=report, no_remote=no_remote)
