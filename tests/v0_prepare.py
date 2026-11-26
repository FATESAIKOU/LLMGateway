"""#320 V-0：準備外部埠負載用的 lgwd 目錄（只給測試與 V-0 腳本用；lgwd 不 import 它）。

給一個**空的**暫存目錄與模式（warn｜enforce）與埠，會：

- 建認證狀態（`create_stores`）與臨時 ed25519 簽章金鑰（0600，只存在該目錄）；
- 寫只含該金鑰的名冊、假 keys.env（0600）、**所有用量抓取關掉**的 policy；
- 印出三行到 stdout：(1) 可貼進 `systemd-run` 的 lgwd 參數行；(2) 客戶端要的環境變數名；(3) 簽章金鑰 ID。
- stderr 多印一行 `note:`：名冊 `registry.json` 要 root 擁有（lgwd 的 `insecure_file` 檢查），提醒先 `sudo chown root:root`。

不印任何金鑰內容（私鑰、keys.env 的值都不出現在輸出裡）。

用法：``python3 -m tests.v0_prepare --dir <空目錄> --mode warn|enforce --port <埠>``
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import sys
from pathlib import Path

from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer, key_entry, registry_bytes
from tests.test_main import FAKE_KEYS, key_text, write_policy

REPO_ROOT = Path(__file__).resolve().parent.parent

V0_KEY_ID = "v0-load-key"
V0_KEY_NAME = "v0-load"
MODES = ("warn", "enforce")
#: 客戶端從環境讀的變數名（`tests.v0_load_client` 會用；只印名字，不印值）。
ENV_DIR = "LGW_V0_DIR"
ENV_PORT = "LGW_V0_PORT"
#: 每 key 配額：出廠 600 是 65 秒視窗（約 9 筆/秒），負載客戶端會被 429 擋；與 C-612 同值。
PER_KEY_MAX = "1000000"


def load_signer(directory: str) -> Signer:
    """讀既有的臨時金鑰（不重新產生；`Signer.__init__` 會呼叫 ssh-keygen 覆寫）。"""
    d = os.fspath(directory)
    signer = Signer.__new__(Signer)
    signer.directory = d
    signer.name = V0_KEY_NAME
    signer.private_path = os.path.join(d, V0_KEY_NAME)
    signer.public_path = signer.private_path + ".pub"
    with open(signer.public_path, "r", encoding="ascii") as handle:
        parts = handle.read().split()
    signer.public_blob = base64.b64decode(parts[1], validate=True)
    signer.public_key32 = signer.public_blob[-32:]
    return signer


def prepare(directory: str, mode: str, port: int) -> list[str]:
    """建立 lgwd 的目錄與設定，回傳 lgwd 參數（不含 `python3 -m lgwd`）。"""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    root = Path(directory)
    if not root.is_dir():
        raise ValueError("directory must already exist")
    if any(root.iterdir()):
        raise ValueError("directory must be empty")

    state = root / "state"
    state.mkdir()
    registry = root / "registry.json"
    paths = auth_paths(str(state), registry=str(registry))
    create_stores(paths.trust_db, paths.replay_db)

    signer = Signer(str(root), V0_KEY_NAME)
    os.chmod(signer.private_path, 0o600)
    write_registry_file(str(registry), registry_bytes(1, [key_entry(V0_KEY_ID, signer)]))

    keys = root / "keys.env"
    keys.write_text(key_text(FAKE_KEYS), encoding="utf-8")
    os.chmod(keys, 0o600)
    write_policy(root)

    return [
        "--auth-mode", mode,
        "--auth-state-dir", str(state),
        "--auth-registry", str(registry),
        "--auth-per-key-max", PER_KEY_MAX,
        "--api-host", "127.0.0.1",
        "--api-port", str(port),
        "--db", str(root / "app.db"),
        "--keys", str(keys),
        "--policy", str(root / "policy.json"),
        "--keysync-interval", "0",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="v0_prepare")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        lgwd_args = prepare(args.dir, args.mode, args.port)
    except (OSError, ValueError) as exc:
        print(f"v0_prepare: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("lgwd-args: " + shlex.join(lgwd_args))
    print(f"client-env: {ENV_DIR} {ENV_PORT}")
    print(f"client-key-id: {V0_KEY_ID}")
    registry = lgwd_args[lgwd_args.index("--auth-registry") + 1]
    print(f"note: registry.json must be owned by root (sudo chown root:root {registry}) "
          "before starting lgwd", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
