"""管理命令（tasks 1.3；design 決定 4、12）。直接開 db，用 LeaseService 的客戶端方法。

- client add NAME：stdout 只印 token＋換行。
- client rm NAME（revoke 同義）：撤銷。
- client list：不印雜湊。
只用標準函式庫；錯誤訊息去 stderr，不含值與雜湊。
"""

from __future__ import annotations

import argparse
import sys

from lgwd.clock import SystemClock
from lgwd.keys import KeyFileError, KeyStore
from lgwd.leasing import LeaseService
from lgwd.main import (
    DEFAULT_DB,
    DEFAULT_KEYS_PATH,
    DEFAULT_POLICY_PATH,
    ENV_DB,
    ENV_KEYS,
    ENV_POLICY,
    store_paths,
)
from lgwd.policy import PolicyError, load_policy


def open_service(db: str, policy_path: str, keys_path: str) -> LeaseService:
    return LeaseService(db, load_policy(policy_path), KeyStore(keys_path), SystemClock())


def _cmd_add(service: LeaseService, name: str) -> int:
    try:
        token = service.add_client(name)
    except ValueError as exc:
        print(f"lgw.admin: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(token + "\n")
    return 0


def _cmd_rm(service: LeaseService, name: str) -> int:
    try:
        service.revoke_client(name)
    except ValueError as exc:
        print(f"lgw.admin: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_list(service: LeaseService) -> int:
    for row in service.list_clients():
        sys.stdout.write("{0}\t{1}\t{2}\n".format(
            row["name"], row["created_at"], row["revoked_at"] or "-"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lgw.admin")
    parser.add_argument("--db", default=None, help=f"預設 {ENV_DB} 或 {DEFAULT_DB}")
    parser.add_argument("--policy", default=None, help=f"預設 {ENV_POLICY} 或 {DEFAULT_POLICY_PATH}")
    parser.add_argument("--keys", default=None, help=f"預設 {ENV_KEYS} 或 {DEFAULT_KEYS_PATH}")
    sub = parser.add_subparsers(dest="obj", required=True)
    client = sub.add_parser("client")
    ops = client.add_subparsers(dest="op", required=True)
    add = ops.add_parser("add")
    add.add_argument("name")
    rm = ops.add_parser("rm", aliases=["revoke"])
    rm.add_argument("name")
    ops.add_parser("list")
    return parser


def main(argv: list[str] | None = None, env=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code or 0)
    if env is None:
        import os
        env = os.environ
    db, policy_path, keys_path = store_paths(args, env)
    try:
        service = open_service(db, policy_path, keys_path)
    except (PolicyError, KeyFileError, OSError) as exc:
        print(f"lgw.admin: cannot open: {exc}", file=sys.stderr)
        return 2
    try:
        if args.op == "add":
            return _cmd_add(service, args.name)
        if args.op in ("rm", "revoke"):
            return _cmd_rm(service, args.name)
        if args.op == "list":
            return _cmd_list(service)
    finally:
        service.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
