"""客戶端連線程式（design 決定 4；spec connectivity「客戶端連線程式」；tasks 3.1c）。

用法（函式庫）：

    from lgw.connect import ConnectConfig, Unavailable, connect

    with connect(cfg) as base_url:      # base_url = "http://127.0.0.1:<臨時埠>"
        ...                             # 走 lgw.client 的 list／get／登錄冷卻

**用法預期是「每次操作才 connect、用完就關」**：這是短命的轉發，不長期持有、也不自動
重連。隧道斷掉時後續請求會變成「無法確認」（lgw.client 回 unreachable），呼叫方
（sidecar 的 SubscriptionSource）等一下再開一次新的 connect 即可。短命是刻意的：
v0 沒有應用層認證，開著不放只增加暴露面。

做法：

- 用 subprocess 起::

      ssh -N -L 127.0.0.1:<臨時埠>:127.0.0.1:<remote_api> [-J <跳板1,跳板2>]
          -i <identity> -o UserKnownHostsFile=<known_hosts>
          -o StrictHostKeyChecking=yes -o BatchMode=yes
          -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
          -p <port> <user>@<host>

  （host key 一律照 known_hosts 檢查，不用 accept-new；BatchMode 讓它不會停下來問。）
- 等本機埠真的能連上才 yield `http://127.0.0.1:<臨時埠>`；臨時埠用
  bind 127.0.0.1:0 取得再釋放。
- 離開（正常或例外）時一定關掉 ssh 子行程。
- 連不上、ssh 先結束、逾時：丟 `Unavailable`，只帶錯誤種類（kind），不帶 ssh 的
  stderr（裡面可能有位址）。kind ∈ ssh_missing｜bad_config｜ssh_exited｜timeout。

設定檔（connect.env）用 `load_config(path)` 讀，KEY=VALUE：

- `JUMPS`（可空）：逗號分隔的 `user@host:port`，照順序當跳板；
- `RENDEZVOUS`：`user@host:port`；
- `IDENTITY`、`KNOWN_HOSTS`：路徑；
- `REMOTE_API`：預設 4710。

空行與 `#` 開頭略過；未知的鍵視為錯誤（錯誤訊息只寫行號與鍵名，不印值）。

指令：`python3 -m lgw.connect --config <connect.env>`，前景維持轉發、印出一行
base URL，Ctrl-C 結束。

只用標準函式庫。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REMOTE_API = 4710
DEFAULT_TIMEOUT = 15.0

_KNOWN_KEYS = ("JUMPS", "RENDEZVOUS", "IDENTITY", "KNOWN_HOSTS", "REMOTE_API")

_PROBE_INTERVAL = 0.05  # 等本機埠時的輪詢間隔（秒）
_PROBE_TIMEOUT = 0.5  # 單次探本機埠的上限（秒）
_TERM_TIMEOUT = 5  # 離開時等 ssh 結束的上限（秒）


class ConfigError(ValueError):
    """connect.env 格式錯誤。訊息只有行號與鍵名，不含值。"""


class Unavailable(Exception):
    """連不上會合點／開不了轉發。

    只帶錯誤種類 kind（ssh_missing｜bad_config｜ssh_exited｜timeout），不帶 ssh 的
    stderr：裡面可能有位址，不進訊息、不進 log。
    """

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind

    def __str__(self) -> str:
        return self.kind


@dataclass
class ConnectConfig:
    """連線設定；欄位順序＝ConnectConfig(jumps, rendezvous, identity, known_hosts, ...)。"""

    jumps: list[str] = field(default_factory=list)  # user@host:port，照順序
    rendezvous: str = ""  # user@host:port
    identity: str = ""  # 金鑰路徑
    known_hosts: str = ""  # known_hosts 路徑
    remote_api: int = DEFAULT_REMOTE_API  # 會合點上 lgwd 的埠
    ssh_path: str = "ssh"  # 測試用；正式跑 PATH 上的 ssh


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
    """讀 KEY=VALUE 的 connect.env（鍵名見模組 docstring）。"""
    values: dict[str, tuple[int, str]] = {}
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError("line {0}: not KEY=VALUE".format(number))
        if key not in _KNOWN_KEYS:
            raise ConfigError("line {0}: unknown key {1}".format(number, key))
        values[key] = (number, value.strip())
    for key in ("RENDEZVOUS", "IDENTITY", "KNOWN_HOSTS"):
        if key not in values or not values[key][1]:
            raise ConfigError("missing " + key)
    rendezvous = values["RENDEZVOUS"][1]
    try:
        _split_target(rendezvous)
    except ValueError:
        raise ConfigError(
            "line {0}: RENDEZVOUS: expected user@host:port".format(
                values["RENDEZVOUS"][0]
            )
        ) from None
    jumps: list[str] = []
    if "JUMPS" in values:
        number, raw = values["JUMPS"]
        if raw:
            for item in raw.split(","):
                item = item.strip()
                try:
                    _split_target(item)
                except ValueError:
                    raise ConfigError(
                        "line {0}: JUMPS: expected user@host:port".format(number)
                    ) from None
                jumps.append(item)
    remote_api = DEFAULT_REMOTE_API
    if "REMOTE_API" in values:
        number, raw = values["REMOTE_API"]
        try:
            remote_api = int(raw)
        except ValueError:
            raise ConfigError(
                "line {0}: REMOTE_API: expected port number".format(number)
            ) from None
        if not 1 <= remote_api <= 65535:
            raise ConfigError(
                "line {0}: REMOTE_API: expected port number".format(number)
            )
    return ConnectConfig(
        jumps=jumps,
        rendezvous=rendezvous,
        identity=values["IDENTITY"][1],
        known_hosts=values["KNOWN_HOSTS"][1],
        remote_api=remote_api,
    )


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


def _build_argv(cfg: ConnectConfig, local_port: int) -> list[str]:
    user, host, ssh_port = _split_target(cfg.rendezvous)
    argv = [
        cfg.ssh_path,
        "-N",
        "-L",
        "127.0.0.1:{0}:127.0.0.1:{1}".format(local_port, cfg.remote_api),
    ]
    if cfg.jumps:
        argv += ["-J", ",".join(cfg.jumps)]
    argv += [
        "-i",
        cfg.identity,
        "-o",
        "UserKnownHostsFile={0}".format(cfg.known_hosts),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-p",
        str(ssh_port),
        "{0}@{1}".format(user, host),
    ]
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
    """yield (ssh 子行程, base URL)；離開時一定關掉子行程。CLI 需要握住子行程。"""
    try:
        _split_target(cfg.rendezvous)
        for jump in cfg.jumps:
            _split_target(jump)
        remote_api = int(cfg.remote_api)
        if not 1 <= remote_api <= 65535:
            raise ValueError("remote api out of range")
    except (TypeError, ValueError):
        raise Unavailable("bad_config") from None
    local_port = _free_port()
    argv = _build_argv(cfg, local_port)
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


@contextmanager
def connect(cfg: ConnectConfig, timeout: float = DEFAULT_TIMEOUT):
    """在本機開一個到會合點的轉發，yield base URL；離開時關掉 ssh。

    每次操作才 connect、用完就關，不長期持有、不自動重連（見模組 docstring）。
    ssh 先結束、逾時或起不來丟 Unavailable（只帶錯誤種類）。
    """
    with _connected(cfg, timeout=timeout) as (_proc, base_url):
        yield base_url


def main(argv: list[str] | None = None) -> int:
    """`python3 -m lgw.connect --config <connect.env>`：前景維持轉發，Ctrl-C 結束。"""
    parser = argparse.ArgumentParser(
        prog="lgw.connect",
        description="經會合點（可經跳板）在本機開一個到 LLMGateway 的轉發，印出 base URL。",
    )
    parser.add_argument("--config", required=True, help="connect.env 路徑")
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
