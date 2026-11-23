"""D2 認證相關測試共用的假資料（名冊 bytes／公鑰行／狀態目錄）。

只給測試用；`lgwd` 不 import 它。公鑰取自 `tests/vectors/ed25519_pubkeys.json` 的固定
向量（沒有私鑰、沒有真鑰）；名冊一律寫在 `tempfile` 的暫存目錄，不碰
`/var/lib/llmgateway` 或 `/etc/llmgateway`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads(
    (REPO_ROOT / "tests" / "vectors" / "ed25519_pubkeys.json").read_text(encoding="utf-8")
)["public_key_vectors"]
VECTOR = {item["name"]: item for item in VECTORS}

#: 兩把固定的合法 Ed25519 公鑰（驗證後的 32 bytes）。
VALID_A = base64.b64decode(VECTOR["valid_ed25519_a"]["blob_b64"])[-32:]
VALID_B = base64.b64decode(VECTOR["valid_ed25519_b"]["blob_b64"])[-32:]
#: 第三組 bytes（只在本檔用得到；與 `tests/test_auth_registry.py` 的 VALID_C 同一個值）。
VALID_C = hashlib.sha256(b"test-only third key").digest()


def ssh_blob(raw32: bytes) -> bytes:
    """`string "ssh-ed25519"` ＋ `string(32)`。"""
    return b"\x00\x00\x00\x0bssh-ed25519" + b"\x00\x00\x00\x20" + raw32


def public_key_line(raw32: bytes) -> str:
    """名冊的 `public_key` 欄：`ssh-ed25519 <base64>`（不含註解）。"""
    return "ssh-ed25519 " + base64.b64encode(ssh_blob(raw32)).decode("ascii")


def principal(**over) -> dict:
    """一個 principal 的權限形狀（值不重要，D2 只驗形狀）。"""
    base = {
        "ops": ["list", "usage", "roles", "resolve", "cooldown"],
        "resolve_agents": ["claude-code", "opencode"],
        "roles": {"PM": ["claude-code"], "impl": ["opencode"]},
    }
    base.update(over)
    return base


def key(key_id: str = "w1-tunnel-a", *, raw: bytes = VALID_A, status: str = "active",
        principal_name: str = "w1-worker") -> dict:
    return {
        "key_id": key_id,
        "principal": principal_name,
        "public_key": public_key_line(raw),
        "status": status,
    }


def registry_object(generation: int = 1, *, keys: list | None = None,
                    principals: dict | None = None) -> dict:
    return {
        "schema": 1,
        "generation": generation,
        "principals": {"w1-worker": principal()} if principals is None else principals,
        "keys": [key()] if keys is None else keys,
    }


def registry_bytes(generation: int = 1, *, keys: list | None = None,
                   principals: dict | None = None) -> bytes:
    return json.dumps(
        registry_object(generation, keys=keys, principals=principals), ensure_ascii=False
    ).encode("utf-8")


def write_registry_file(path: str, data: bytes) -> None:
    """原子替換名冊檔（同目錄暫存＋rename，換 inode），權限 0644（C-207 允許）。

    測試機的 umask 常是 002，直接以 `open()` 寫會變 0664（群組可寫）而被
    `read_registry_file` 判成 `insecure_file`，所以這裡明確 chmod。
    """
    tmp = os.fspath(path) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.fspath(path))
