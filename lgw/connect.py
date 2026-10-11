"""客戶端連線程式（design 決定 4；spec connectivity「客戶端連線程式」；tasks 3.1c）。

用法（函式庫）：

    from lgw.connect import ConfigError, ConnectConfig, Unavailable, connect

    with connect(cfg) as base_url:      # base_url = "http://127.0.0.1:<臨時埠>"
        ...                             # 走 lgw.client 的 list／get／登錄冷卻

**用法預期是「每次操作才 connect、用完就關」**：這是短命的轉發，不長期持有、也不自動
重連。隧道斷掉時後續請求會變成「無法確認」（lgw.client 回 unreachable），呼叫方
（sidecar 的 SubscriptionSource）等一下再開一次新的 connect 即可。短命是刻意的：
v0 沒有應用層認證，開著不放只增加暴露面。

設定檔（JSON，慣例叫 `connect.json`）
-------------------------------------

`load_config(path)` 每次呼叫都重讀檔案、不快取（sidecar 每次操作都會重讀）；
`ConnectConfig.from_dict(d)` 直接吃已解析的 dict。格式如下，欄位就是全部，多一個
都不行：

| 欄位 | 型別 | 必填 | 預設 | 說明 |
|---|---|---|---|---|
| `schema` | int | 是 | — | 格式版本，只能是 `1`。 |
| `rendezvous` | str | 是 | — | 會合點，`user@host:port`。 |
| `jumps` | array of str | 否 | `[]` | 零到多個跳板，每個 `user@host:port`，照順序。 |
| `identity` | str | 是 | — | 私鑰路徑。 |
| `known_hosts` | str | 二擇一 | — | known_hosts 檔路徑。 |
| `host_keys` | array of str | 二擇一 | — | known_hosts 格式的一行一字串。 |
| `remote_api` | int | 否 | `4710` | 會合點上 lgwd 的埠。 |

- `known_hosts` 與 `host_keys` **恰給一個**：
  - 給 `known_hosts`：ssh 直接用它指到的檔；
  - 給 `host_keys`：設定檔自成一體（輪替時整檔原子改寫），`connect()` 會在私有的
    暫存目錄寫一個 0600 的 known_hosts 給這次的 ssh 用。
- host key 一律照 known_hosts 檢查（`StrictHostKeyChecking yes`），不用 accept-new。
- 未知欄位、缺必填、型別錯、`schema` 不是 1、`known_hosts`／`host_keys` 兩個都給或
  都沒給 → `ConfigError`；訊息只寫欄位名與原因，不印值。檔案不存在或沒權限丟
  `OSError`（路徑／權限問題，不是格式問題）；內容不是 UTF-8 或不是 JSON →
  `ConfigError`。

範例（位址、金鑰一律用假的佔位）：

    {
      "schema": 1,
      "rendezvous": "sshproxy@rendezvous.example:22",
      "jumps": ["jump1@jump.example:22"],
      "identity": "/home/sidecar/.ssh/id_tunnel",
      "known_hosts": "/home/sidecar/.ssh/known_hosts_rendezvous",
      "remote_api": 4710
    }

自成一體的版本（`host_keys` 取代 `known_hosts`）：

    {
      "schema": 1,
      "rendezvous": "sshproxy@rendezvous.example:22",
      "identity": "/home/sidecar/.ssh/id_tunnel",
      "host_keys": ["[rendezvous.example]:22 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAFAKEfakeFAKEfakeFAKEfakeFAKEfake"]
    }

指令：`python3 -m lgw.connect --config <connect.json>`，前景維持轉發、印出一行
base URL，Ctrl-C 結束。

做法
----

- `connect()` 先在私有暫存目錄（`mkdtemp`，0700）寫一個 0600 的 `ssh_config`：

      Host *
          IdentityFile "<identity>"
          IdentitiesOnly yes
          UserKnownHostsFile "<known_hosts>"
          GlobalKnownHostsFile /dev/null
          StrictHostKeyChecking yes
          BatchMode yes
          ExitOnForwardFailure yes
          ServerAliveInterval 30
          ServerAliveCountMax 3

  （路徑都加引號，含空白也能用。）給 `host_keys` 時，同一個目錄再寫一個 0600 的
  known_hosts。
- 起 ssh::

      ssh -F <ssh_config> -N -L 127.0.0.1:<臨時埠>:127.0.0.1:<remote_api>
          [-J <跳板1,跳板2>] -p <port> <user>@<host>

  用 `-F` 而不是命令列上的 `-i`／`-o`：OpenSSH 用 `-J` 時，命令列上的 `-i` 與
  `-o` **不會**套到跳板那一段，只有 `-F` 帶的設定才會。所以**所有跳板與會合點共用
  同一把 identity 與同一份 known_hosts**；用跳板時 `host_keys` 也要包含每個跳板的
  host key。
- 等本機埠真的能連上才 yield `http://127.0.0.1:<臨時埠>`；臨時埠用
  bind 127.0.0.1:0 取得再釋放。
- 離開（正常或例外）時一定關掉 ssh 子行程，並刪掉整個暫存目錄。
- `Unavailable` 只在進入 `with` 時（建立隧道期間）丟，kind ∈ ssh_missing｜bad_config｜
  temp_failed｜ssh_exited｜timeout，只帶錯誤種類、不帶 ssh 的 stderr（裡面可能有
  位址）。進入之後 ssh 若斷掉不會丟例外：`lgw.client` 的後續呼叫回 unreachable，
  呼叫方重新 connect 即可。

只用標準函式庫。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REMOTE_API = 4710
DEFAULT_TIMEOUT = 15.0

_KNOWN_FIELDS = frozenset(
    ("schema", "rendezvous", "jumps", "identity", "known_hosts", "host_keys",
     "remote_api")
)

_PROBE_INTERVAL = 0.05  # 等本機埠時的輪詢間隔（秒）
_PROBE_TIMEOUT = 0.5  # 單次探本機埠的上限（秒）
_TERM_TIMEOUT = 5  # 離開時等 ssh 結束的上限（秒）

_MISSING = object()


class ConfigError(ValueError):
    """設定檔格式錯誤（欄位或原因的訊息，不含值）。"""


class Unavailable(Exception):
    """連不上會合點／開不了轉發。

    只帶錯誤種類 kind（ssh_missing｜bad_config｜temp_failed｜ssh_exited｜timeout），
    不帶 ssh 的 stderr：裡面可能有位址，不進訊息、不進 log。
    """

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind

    def __str__(self) -> str:
        return self.kind


@dataclass
class ConnectConfig:
    """連線設定（JSON 欄位見模組 docstring）；host_keys 與 known_hosts 恰給一個。"""

    jumps: list[str] = field(default_factory=list)  # user@host:port，照順序
    rendezvous: str = ""  # user@host:port
    identity: str = ""  # 金鑰路徑
    known_hosts: str | None = None  # known_hosts 檔路徑
    remote_api: int = DEFAULT_REMOTE_API  # 會合點上 lgwd 的埠
    host_keys: list[str] | None = None  # known_hosts 格式的行；與 known_hosts 二擇一
    ssh_path: str = "ssh"  # 測試用；正式跑 PATH 上的 ssh

    @classmethod
    def from_dict(cls, data) -> "ConnectConfig":
        """已解析的 JSON dict（schema 1）→ ConnectConfig；錯誤丟 ConfigError。"""
        if not isinstance(data, dict):
            raise ConfigError("config must be a JSON object")
        unknown = sorted(str(key) for key in data if key not in _KNOWN_FIELDS)
        if unknown:
            raise ConfigError("unknown field: " + unknown[0])
        schema = data.get("schema", _MISSING)
        if schema is _MISSING:
            raise ConfigError("missing field: schema")
        if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
            raise ConfigError("schema must be 1")
        rendezvous = data.get("rendezvous", _MISSING)
        if rendezvous is _MISSING:
            raise ConfigError("missing field: rendezvous")
        if not isinstance(rendezvous, str) or not rendezvous:
            raise ConfigError("rendezvous must be a non-empty user@host:port string")
        try:
            _split_target(rendezvous)
        except ValueError:
            raise ConfigError("rendezvous must be user@host:port") from None
        identity = data.get("identity", _MISSING)
        if identity is _MISSING:
            raise ConfigError("missing field: identity")
        if not isinstance(identity, str) or not identity:
            raise ConfigError("identity must be a non-empty path string")
        if _unsafe_path(identity):
            raise ConfigError(
                "identity must not contain quotes or control characters"
            )
        raw_jumps = data.get("jumps", [])
        if not isinstance(raw_jumps, list):
            raise ConfigError("jumps must be an array of user@host:port strings")
        jumps: list[str] = []
        for index, item in enumerate(raw_jumps):
            if not isinstance(item, str) or not item:
                raise ConfigError(
                    "jumps[{0}] must be user@host:port".format(index)
                )
            try:
                _split_target(item)
            except ValueError:
                raise ConfigError(
                    "jumps[{0}] must be user@host:port".format(index)
                ) from None
            jumps.append(item)
        has_known_hosts = data.get("known_hosts", _MISSING) is not _MISSING
        has_host_keys = data.get("host_keys", _MISSING) is not _MISSING
        if has_known_hosts and has_host_keys:
            raise ConfigError("known_hosts and host_keys are mutually exclusive")
        if not has_known_hosts and not has_host_keys:
            raise ConfigError("one of known_hosts or host_keys is required")
        known_hosts: str | None = None
        host_keys: list[str] | None = None
        if has_known_hosts:
            known_hosts = data["known_hosts"]
            if not isinstance(known_hosts, str) or not known_hosts:
                raise ConfigError("known_hosts must be a non-empty path string")
            if _unsafe_path(known_hosts):
                raise ConfigError(
                    "known_hosts must not contain quotes or control characters"
                )
        else:
            raw_keys = data["host_keys"]
            if not isinstance(raw_keys, list) or not raw_keys:
                raise ConfigError(
                    "host_keys must be a non-empty array of known_hosts lines"
                )
            for index, line in enumerate(raw_keys):
                if not isinstance(line, str) or not line:
                    raise ConfigError(
                        "host_keys[{0}] must be a non-empty string".format(index)
                    )
            host_keys = list(raw_keys)
        remote_api = data.get("remote_api", DEFAULT_REMOTE_API)
        if (
            isinstance(remote_api, bool)
            or not isinstance(remote_api, int)
            or not 1 <= remote_api <= 65535
        ):
            raise ConfigError("remote_api must be a port number (1-65535)")
        return cls(
            jumps=jumps,
            rendezvous=rendezvous,
            identity=identity,
            known_hosts=known_hosts,
            remote_api=remote_api,
            host_keys=host_keys,
        )


def _unsafe_path(value: str) -> bool:
    """路徑能不能安全寫進 ssh_config 的引號裡（含雙引號或控制字元就不行）。"""
    return any(ch in value for ch in ('"', "\n", "\r"))


def _split_target(value: str) -> tuple[str, str, int]:
    """`user@host:port` → (user, host, port)；格式不對丟 ValueError（訊息不含值）。"""
    user, at, hostport = value.partition("@")
    host, colon, port_text = hostport.rpartition(":")
    if not at or not user or not host or not colon or not port_text.isdigit():
        raise ValueError("expected user@host:port")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return user, host, port


def load_config(path: str | Path) -> ConnectConfig:
    """讀 JSON 設定檔（schema 1，格式見模組 docstring）；每次呼叫都重讀、不快取。

    檔案不存在或沒權限丟 OSError；內容不是 UTF-8 或不是 JSON → ConfigError。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError("config is not valid UTF-8") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("config is not valid JSON") from None
    return ConnectConfig.from_dict(data)


def _free_port() -> int:
    """在 127.0.0.1 上取一個沒人用的埠，立刻釋放（給 -L 用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_open(port: int, timeout: float) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _validate(cfg: ConnectConfig) -> None:
    """直接建構的 ConnectConfig 也過同一套規則；不對丟 Unavailable('bad_config')。"""
    try:
        _split_target(cfg.rendezvous)
        if not isinstance(cfg.identity, str) or not cfg.identity:
            raise ValueError("identity")
        if _unsafe_path(cfg.identity):
            raise ValueError("identity path")
        if (cfg.known_hosts is None) == (cfg.host_keys is None):
            raise ValueError("known_hosts/host_keys")
        if cfg.known_hosts is not None:
            if not isinstance(cfg.known_hosts, str) or not cfg.known_hosts:
                raise ValueError("known_hosts")
            if _unsafe_path(cfg.known_hosts):
                raise ValueError("known_hosts path")
        else:
            if not isinstance(cfg.host_keys, list) or not cfg.host_keys:
                raise ValueError("host_keys")
            for line in cfg.host_keys:
                if not isinstance(line, str) or not line:
                    raise ValueError("host_keys line")
        for jump in cfg.jumps:
            _split_target(jump)
        remote_api = int(cfg.remote_api)
        if not 1 <= remote_api <= 65535:
            raise ValueError("remote_api")
        if not isinstance(cfg.ssh_path, str) or not cfg.ssh_path:
            raise ValueError("ssh_path")
    except (TypeError, ValueError):
        raise Unavailable("bad_config") from None


def _write_private(directory: str, name: str, text: str) -> str:
    """在目錄下寫 0600 的檔；回傳路徑。"""
    path = os.path.join(directory, name)
    raw = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # 不受 umask 影響
    return path


def _known_hosts_text(host_keys: list[str]) -> str:
    return "\n".join(host_keys) + "\n"


def _ssh_config_text(identity: str, known_hosts_path: str) -> str:
    """ssh_config：Host *（含跳板）；路徑加引號（含空白也能用）。"""
    lines = [
        "Host *",
        "    IdentityFile \"{0}\"".format(identity),
        "    IdentitiesOnly yes",
        "    UserKnownHostsFile \"{0}\"".format(known_hosts_path),
        "    GlobalKnownHostsFile /dev/null",
        "    StrictHostKeyChecking yes",
        "    BatchMode yes",
        "    ExitOnForwardFailure yes",
        "    ServerAliveInterval 30",
        "    ServerAliveCountMax 3",
    ]
    return "\n".join(lines) + "\n"


def _build_argv(
    cfg: ConnectConfig, local_port: int, ssh_config_path: str
) -> list[str]:
    user, host, ssh_port = _split_target(cfg.rendezvous)
    argv = [
        cfg.ssh_path,
        "-F",
        ssh_config_path,
        "-N",
        "-L",
        "127.0.0.1:{0}:127.0.0.1:{1}".format(local_port, cfg.remote_api),
    ]
    if cfg.jumps:
        argv += ["-J", ",".join(cfg.jumps)]
    argv += ["-p", str(ssh_port), "{0}@{1}".format(user, host)]
    return argv


def _wait_ready(proc: subprocess.Popen, port: int, timeout: float) -> None:
    """等本機埠能連上；ssh 先結束或逾時丟 Unavailable。"""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if _port_open(port, min(_PROBE_TIMEOUT, max(deadline - time.monotonic(), 0.01))):
            return
        if proc.poll() is not None:
            raise Unavailable("ssh_exited")
        if time.monotonic() >= deadline:
            raise Unavailable("timeout")
        time.sleep(_PROBE_INTERVAL)


def _shutdown(proc: subprocess.Popen) -> None:
    """一定讓 ssh 子行程結束（先 SIGTERM，逾時再 SIGKILL）。"""
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=_TERM_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextmanager
def _connected(cfg: ConnectConfig, timeout: float):
    """yield (ssh 子行程, base URL)；離開時一定關掉子行程、刪掉暫存目錄。"""
    _validate(cfg)
    local_port = _free_port()
    try:
        temp_dir = tempfile.mkdtemp(prefix="lgw-connect-")  # 0700
    except OSError:
        raise Unavailable("temp_failed") from None
    try:
        try:
            if cfg.host_keys is not None:
                known_hosts_path = _write_private(
                    temp_dir, "known_hosts", _known_hosts_text(cfg.host_keys)
                )
            else:
                known_hosts_path = cfg.known_hosts
            ssh_config_path = _write_private(
                temp_dir, "ssh_config",
                _ssh_config_text(cfg.identity, known_hosts_path),
            )
        except OSError:
            raise Unavailable("temp_failed") from None
        argv = _build_argv(cfg, local_port, ssh_config_path)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,  # 不讀也不留 stderr（可能有位址）
            )
        except (OSError, ValueError, IndexError):
            raise Unavailable("ssh_missing") from None
        try:
            _wait_ready(proc, local_port, timeout)
            yield proc, "http://127.0.0.1:{0}".format(local_port)
        finally:
            _shutdown(proc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)  # 連 ssh_config 一起刪掉


@contextmanager
def connect(cfg: ConnectConfig, timeout: float = DEFAULT_TIMEOUT):
    """在本機開一個到會合點的轉發，yield base URL；離開時關掉 ssh。

    每次操作才 connect、用完就關，不長期持有、不自動重連（見模組 docstring）。

    `Unavailable` 只在**進入 with 時**（建立隧道期間）丟，kind 是 ssh_missing｜
    bad_config｜temp_failed｜ssh_exited｜timeout。進入之後 ssh 若斷掉不會丟例外：
    `lgw.client` 的後續呼叫回 unreachable（「無法確認」），呼叫方重新 connect 即可。
    """
    with _connected(cfg, timeout=timeout) as (_proc, base_url):
        yield base_url


def main(argv: list[str] | None = None) -> int:
    """`python3 -m lgw.connect --config <connect.json>`：前景維持轉發，Ctrl-C 結束。"""
    parser = argparse.ArgumentParser(
        prog="lgw.connect",
        description="經會合點（可經跳板）在本機開一個到 LLMGateway 的轉發，印出 base URL。",
    )
    parser.add_argument("--config", required=True, help="connect.json 路徑")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help="等本機埠的秒數（預設 {0}）".format(DEFAULT_TIMEOUT),
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(args.config)
    except (OSError, ConfigError) as exc:
        print("lgw.connect: {0}".format(exc), file=sys.stderr)
        return 2
    try:
        with _connected(cfg, timeout=args.timeout) as (proc, base_url):
            print(base_url, flush=True)
            returncode = proc.wait()
    except Unavailable as exc:
        print("lgw.connect: unavailable: {0}".format(exc.kind), file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # Ctrl-C：__exit__ 已關掉 ssh
        return 0
    if returncode != 0:
        print("lgw.connect: unavailable: ssh_exited", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
