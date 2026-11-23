"""llmgwctl 的子指令分派。

``python3 -m lgwctl <子指令>``（cwd 要在某一版的目錄）；``/usr/local/bin/llmgwctl``
是 install 產生的 wrapper，會先切進 current。

遷移用的子指令（``prepare-migration``、``cutover``、``confirm``、
``rollback-to-user``、``finalize-migration``）在 ``lgwctl/migrate.py``（成員 C 寫）：
那個模組要提供 ``main(argv, env=None)``，argv 是完整的子指令 argv（含子指令名）。
模組不在或沒有 ``main()`` 時給明確的錯誤與結束碼 3，不會 traceback。
"""

from __future__ import annotations

import argparse
import os
import sys

from . import capability, check, install, status, sync, upgrade, usage, units, probe
from .layout import DEFAULT_REPO, Layout, SERVICE_NAME
from .report import Report

MIGRATE_COMMANDS = ("prepare-migration", "cutover", "confirm", "rollback-to-user",
                    "finalize-migration")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmgwctl",
        description="LLMGateway 管理指令（install／check／status／upgrade／rollback／sync／usage）。"
                    " cwd 要在當版目錄（/opt/llmgateway/current）。"
                    f" 遷移子指令 {', '.join(MIGRATE_COMMANDS)} 由 lgwctl/migrate.py 提供。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (("start", "啟動服務"), ("stop", "停止服務"), ("restart", "重啟服務")):
        sub.add_parser(command, help=help_text)
    p_reset = sub.add_parser("follow-reset", help="重設隧道水位或抓取狀態")
    p_reset.add_argument("--fetch-state", action="store_true")

    p_apply = sub.add_parser(
        "tunnel-apply",
        help="以反向保險套用新的 tunnel.env／drop-in 並重啟隧道（需 root；"
             "窗口內勿重開機：transient timer 重開機後失效）")
    p_apply.add_argument("--new-env", default=None, help="暫存的新 tunnel.env（以複製套用，保留）")
    p_apply.add_argument("--new-dropin", default=None, help="暫存的新 credential.conf（選填）")
    p_apply.add_argument("--revert-after", default="10m",
                         help="保險多久後自動還原（預設 10m，下限 5m）")
    p_apply.add_argument("--confirm", action="store_true", help="驗收完成，取消自動還原")
    p_apply.add_argument("--retry-revert", action="store_true",
                         help="還原失敗（revert-failed）後重試還原（需 root）")
    p_apply.add_argument("--revert-attempt", type=int, default=0, help=argparse.SUPPRESS)
    p_apply.add_argument("--round", default=None, help=argparse.SUPPRESS)
    p_apply.add_argument("--revert-inner", action="store_true", help=argparse.SUPPRESS)
    p_apply.add_argument("--apply-inner", action="store_true", help=argparse.SUPPRESS)

    p_install = sub.add_parser("install", help="第一次安裝（可重跑；冪等）")
    p_install.add_argument("--tag", required=True, help="版本 tag（lgw-vX.Y.Z）")
    p_install.add_argument("--repo", default=None, help=f"取碼來源（預設 {DEFAULT_REPO}）")

    p_check = sub.add_parser("check", help="檢查整套（ok／todo／fail；0／1／2）")
    p_check.add_argument("--no-remote", action="store_true",
                         help="跳過經會合點的探針（不列那一項）")
    p_check.add_argument("--tag", default=None, help="順便確認裝的是這個 tag")

    p_status = sub.add_parser("status", help="看版本與 unit 狀態（不用 root）")
    p_status.add_argument("--no-remote", action="store_true")

    p_upgrade = sub.add_parser("upgrade", help="換新版（policy 預檢、失敗自動切回）")
    p_upgrade.add_argument("--tag", required=True, help="新版本 tag（lgw-vX.Y.Z）")
    p_upgrade.add_argument("--repo", default=None, help=f"取碼來源（預設 {DEFAULT_REPO}）")

    sub.add_parser("rollback", help="切回上一版、restart、跑 check")

    sub.add_parser("sync", help="手動跑一次 key 拉取（等同 key-sync）")

    p_usage = sub.add_parser("usage", help="看各訂閱的額度與冷卻（可依角色列候選）")
    # help 不寫死角色／agent 清單（政策檔改動就過時）；列舉的來源留給政策檔與端點。
    p_usage.add_argument("--role", default=None,
                         help="角色名（要搭配 --agent）；可用角色見政策檔 roles 段，"
                              "查詢見 GET /v1/roles/{role}")
    p_usage.add_argument("--agent", default=None,
                         help="agent 種類（有 --role 時必填）；可用值見政策檔的 "
                              "subscriptions[].agents")
    p_usage.add_argument("--data", default=None,
                         help="資料等級（public／general／real）；只在有 --role 時用")
    return parser


def _migrate(argv, env=None) -> int:
    """轉給 lgwctl/migrate.py；沒有那個檔就明確說一聲（結束碼 3）。"""
    import importlib
    import importlib.util

    if importlib.util.find_spec(f"{__package__}.migrate") is None:
        print(f"llmgwctl: {argv[0]} 還沒有實作（這版沒有 lgwctl/migrate.py）",
              file=sys.stderr)
        return 3
    migrate = importlib.import_module(f"{__package__}.migrate")

    if not hasattr(migrate, "main"):
        print(f"llmgwctl: {argv[0]} 還沒有實作（lgwctl/migrate.py 沒有 main()）",
              file=sys.stderr)
        return 3
    return migrate.main(list(argv), env=env)


def main(argv=None, env=None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in and args_in[0] in MIGRATE_COMMANDS:
        return _migrate(args_in, env)
    args = build_parser().parse_args(args_in)
    layout = Layout.from_env(env)
    report = Report()
    if args.command == "follow-reset":
        if os.geteuid() != 0:
            print("follow-reset 需要 root 權限", file=sys.stderr)
            return 1
        names = [".tunnel_fetch_backoff", ".tunnel_fetch_state"]
        if not args.fetch_state:
            names.append(".tunnel_generation")
        try:
            try:
                state_fd = os.open(layout.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return 0
            try:
                for name in names:
                    try:
                        os.unlink(name, dir_fd=state_fd)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(state_fd)
        except OSError:
            print("follow-reset 無法清除狀態", file=sys.stderr)
            return 1
        return 0
    if args.command in ("start", "stop", "restart"):
        state = units.unit_state(layout, SERVICE_NAME)
        if args.command == "stop":
            return 0 if state == "inactive" or units.stop(layout, SERVICE_NAME) else 1
        # 探針的模式跟著 current 的能力旗標（#246）：已 active 時先看健不健康，
        # 起完（或重啟完）再等到健康；stop 不探（只看 unit_state）。
        mode = capability.current(layout).mode
        if args.command == "start" and state == "active" and probe.local_ok(layout, mode=mode):
            return 0
        action = units.start if args.command == "start" else units.restart
        return 0 if action(layout, SERVICE_NAME) and probe.wait_local_ok(layout, layout.list_wait, mode=mode) else 1
    if args.command == "tunnel-apply":
        # 沿用 migrate.py 的鎖與時間格式；晚點才匯入（沒有 migrate.py 時其他子指令不受影響）。
        from . import tunnel_apply
        return tunnel_apply.run(layout, new_env=args.new_env, new_dropin=args.new_dropin,
                                revert_after=args.revert_after, confirm=args.confirm,
                                revert_inner=args.revert_inner, apply_inner=args.apply_inner,
                                retry_revert=args.retry_revert,
                                revert_attempt=args.revert_attempt, round_id=args.round,
                                report=report)
    if args.command == "install":
        return install.run(layout, tag=args.tag, repo=args.repo, report=report)
    if args.command == "check":
        return check.run(layout, report=report, no_remote=args.no_remote, tag=args.tag)
    if args.command == "status":
        return status.run(layout, no_remote=args.no_remote)
    if args.command == "upgrade":
        return upgrade.upgrade(layout, tag=args.tag, repo=args.repo, report=report)
    if args.command == "rollback":
        return upgrade.rollback(layout, report=report)
    if args.command == "sync":
        return sync.run(layout, report=report)
    if args.command == "usage":
        return usage.run(layout, role=args.role, agent=args.agent, data=args.data)
    raise AssertionError(f"unhandled command: {args.command}")
