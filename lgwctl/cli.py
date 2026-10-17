"""llmgwctl 的子指令分派。

``python3 -m lgwctl <子指令>``（cwd 要在某一版的目錄）；``/usr/local/bin/llmgwctl``
是 install 產生的 wrapper，會先切進 current。

遷移用的子指令（``prepare-migration``、``cutover``、``confirm``、``rollback-to-user``）
在 ``lgwctl/migrate.py``（成員 C 寫，T1 只預留分派）：那個檔不在時給明確的錯誤
與結束碼 3，不會 traceback。
"""

from __future__ import annotations

import argparse
import sys

from . import check, install, status, sync, upgrade
from .layout import DEFAULT_REPO, Layout
from .report import Report

MIGRATE_COMMANDS = ("prepare-migration", "cutover", "confirm", "rollback-to-user")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmgwctl",
        description="LLMGateway 管理指令（install／check／status／upgrade／rollback／sync）。"
                    " cwd 要在當版目錄（/opt/llmgateway/current）。"
                    f" 遷移子指令 {', '.join(MIGRATE_COMMANDS)} 由 lgwctl/migrate.py 提供。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="第一次安裝（可重跑；冪等）")
    p_install.add_argument("--tag", required=True, help="版本 tag（lgw-vX.Y.Z）")
    p_install.add_argument("--repo", default=None, help=f"取碼來源（預設 {DEFAULT_REPO}）")

    p_check = sub.add_parser("check", help="檢查整套（ok／todo／fail；0／1／2）")
    p_check.add_argument("--no-remote", action="store_true",
                         help="跳過經會合點的探針（不列那一項）")
    p_check.add_argument("--tag", default=None, help="順便確認裝的是這個 tag")

    sub.add_parser("status", help="看版本與兩個 unit 狀態（不用 root）")

    p_upgrade = sub.add_parser("upgrade", help="換新版（policy 預檢、失敗自動切回）")
    p_upgrade.add_argument("--tag", required=True, help="新版本 tag（lgw-vX.Y.Z）")
    p_upgrade.add_argument("--repo", default=None, help=f"取碼來源（預設 {DEFAULT_REPO}）")

    sub.add_parser("rollback", help="切回上一版、restart、跑 check")

    sub.add_parser("sync", help="手動跑一次 key 拉取（等同 key-sync）")
    return parser


def _migrate(argv) -> int:
    """轉給 lgwctl/migrate.py；沒有那個檔就明確說一聲（結束碼 3）。"""
    import importlib.util

    if importlib.util.find_spec(f"{__package__}.migrate") is None:
        print(f"llmgwctl: {argv[0]} 還沒有實作（這版沒有 lgwctl/migrate.py）",
              file=sys.stderr)
        return 3
    from . import migrate

    if not hasattr(migrate, "main"):
        print(f"llmgwctl: {argv[0]} 還沒有實作（lgwctl/migrate.py 沒有 main()）",
              file=sys.stderr)
        return 3
    return migrate.main(list(argv))


def main(argv=None, env=None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in and args_in[0] in MIGRATE_COMMANDS:
        return _migrate(args_in)
    args = build_parser().parse_args(args_in)
    layout = Layout.from_env(env)
    report = Report()
    if args.command == "install":
        return install.run(layout, tag=args.tag, repo=args.repo, report=report)
    if args.command == "check":
        return check.run(layout, report=report, no_remote=args.no_remote, tag=args.tag)
    if args.command == "status":
        return status.run(layout)
    if args.command == "upgrade":
        return upgrade.upgrade(layout, tag=args.tag, repo=args.repo, report=report)
    if args.command == "rollback":
        return upgrade.rollback(layout, report=report)
    if args.command == "sync":
        return sync.run(layout, report=report)
    raise AssertionError(f"unhandled command: {args.command}")
