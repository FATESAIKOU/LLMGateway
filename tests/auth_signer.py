"""測試用的 SSHSIG 簽章輔助（#222 D2 PR-6a）。

**只給 `tests/` 用；`lgwd` 不得 import 它**（設計「測試用簽章」：簽章輔助不進服務）。

作法是設計允許的那一種：在 `TemporaryDirectory` 裡用真的 `ssh-keygen -t ed25519 -N ''`
產生一次性金鑰，用 `ssh-keygen -Y sign -n llmgateway` 對八行訊息簽章；私鑰只存在那個
暫存目錄，測試結束就刪。八行訊息在這裡**獨立**組一次（不呼叫 `lgwd.auth` 的
`build_signing_message`），這樣那邊的格式若寫錯，測試會是紅的而不是兩邊一起錯。

不讀任何既有金鑰、不碰 `~/.ssh`、不連網。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time

try:  # python3 -m pytest
    from .auth_fakes import principal_all as _principal
except ImportError:  # python3 -m unittest discover -s tests
    from auth_fakes import principal_all as _principal

SSH_KEYGEN = "/usr/bin/ssh-keygen"
NAMESPACE = "llmgateway"
SIG_LINE_1 = "LGW-HTTP-SIG-V1"

#: 子行程只給這三個（與服務的 `sshsig.Verifier._env` 同形；HOME 指向暫存目錄）。
_BASE_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}


def _env(home: str) -> dict:
    env = dict(_BASE_ENV)
    env["HOME"] = home
    return env


def dearmor(text: str) -> bytes:
    """把 `ssh-keygen -Y sign` 的 armor 轉回 180 bytes 的 blob。"""
    lines = [line for line in text.strip().splitlines() if line]
    if not lines or not lines[0].startswith("-----BEGIN SSH SIGNATURE-----"):
        raise ValueError("not an ssh signature armor")
    body = "".join(lines[1:-1])
    return base64.b64decode(body, validate=True)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_nonce() -> str:
    """32 bytes 的無 padding base64url（固定 43 字元）。"""
    return b64url(os.urandom(32))


def signing_message(*, key_id: str, timestamp: int, nonce: str, method: str,
                    target: str, body: bytes = b"") -> bytes:
    """ASCII 八行（每行以 LF 結尾）；第 7 行是原樣傳進來的 request-target。"""
    lines = (
        SIG_LINE_1,
        NAMESPACE,
        key_id,
        str(timestamp),
        nonce,
        method,
        target,
        hashlib.sha256(body).hexdigest(),
    )
    return ("\n".join(lines) + "\n").encode("ascii")


class Signer:
    """一個暫存目錄裡的 ed25519 金鑰；`sign()` 回 `X-LGW-Signature` 的值。"""

    def __init__(self, directory: str, name: str = "key"):
        self.directory = os.fspath(directory)
        self.name = name
        self.private_path = os.path.join(self.directory, name)
        self.public_path = self.private_path + ".pub"
        env = _env(self.directory)
        subprocess.run(
            [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "test-only",
             "-f", self.private_path],
            check=True, capture_output=True, env=env, timeout=30,
        )
        with open(self.public_path, "r", encoding="ascii") as handle:
            parts = handle.read().split()
        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise ValueError("unexpected public key format")
        self.public_blob = base64.b64decode(parts[1], validate=True)
        self.public_key32 = self.public_blob[-32:]

    @property
    def public_key_line(self) -> str:
        """名冊 `public_key` 欄的形狀：`ssh-ed25519 <base64>`（不含註解）。"""
        with open(self.public_path, "r", encoding="ascii") as handle:
            parts = handle.read().split()
        return parts[0] + " " + parts[1]

    def sign(self, message: bytes) -> str:
        """對訊息簽章，回 240 字元的無 padding base64url。"""
        result = subprocess.run(
            [SSH_KEYGEN, "-Y", "sign", "-n", NAMESPACE, "-f", self.private_path],
            input=message, capture_output=True, env=_env(self.directory), timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("ssh-keygen -Y sign failed with exit {0}".format(
                result.returncode))
        return b64url(dearmor(result.stdout.decode("ascii")))

    def headers(self, *, key_id: str, method: str, target: str, body: bytes = b"",
                timestamp: int | None = None, nonce: str | None = None) -> list:
        """一次簽好五個 `X-LGW-*` 標頭（`(name, value)` 的清單）。"""
        ts = int(time.time()) if timestamp is None else int(timestamp)
        if nonce is None:
            nonce = new_nonce()
        message = signing_message(key_id=key_id, timestamp=ts, nonce=nonce,
                                  method=method, target=target, body=body)
        return [
            ("X-LGW-Auth-Version", "1"),
            ("X-LGW-Key-Id", key_id),
            ("X-LGW-Timestamp", str(ts)),
            ("X-LGW-Nonce", nonce),
            ("X-LGW-Signature", self.sign(message)),
        ]


def key_entry(key_id: str, signer: Signer, *, status: str = "active",
              principal_name: str = "w1-worker") -> dict:
    """名冊裡的一筆 key（用真的公鑰；`tests.auth_fakes.key` 的形狀）。"""
    return {
        "key_id": key_id,
        "principal": principal_name,
        "public_key": signer.public_key_line,
        "status": status,
    }


def registry_object(generation: int = 1, entries: list | None = None,
                    principals: dict | None = None) -> dict:
    return {
        "schema": 1,
        "generation": generation,
        "principals": {"w1-worker": _principal()} if principals is None else principals,
        "keys": entries or [],
    }


def registry_bytes(generation: int = 1, entries: list | None = None,
                   principals: dict | None = None) -> bytes:
    return json.dumps(
        registry_object(generation, entries, principals), ensure_ascii=False
    ).encode("utf-8")


__all__ = [
    "NAMESPACE",
    "Signer",
    "b64url",
    "dearmor",
    "key_entry",
    "new_nonce",
    "registry_bytes",
    "registry_object",
    "signing_message",
]
