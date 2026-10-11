"""服務入口（由 impl 實作）。只用標準函式庫。

- 設定：argv 優先，否則環境變數，否則預設值。
- 建 load_policy／KeyStore／LeaseService／UsagePoller；board 的 usage 由 poller 填。
- 每個 API 位址一個 make_api_server；HTML 板只綁 127.0.0.1。
- SIGHUP 重讀 key 檔與 policy（失敗沿用舊的、記 log、不含值）；SIGTERM 正常關閉。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass, field

from .clock import Clock, SystemClock
from .http import make_api_server, make_board_server
from .keys import KeyFileError, KeyStore
from .leasing import LeaseService
from .policy import PolicyError, load_policy
from .usage import UsageEntry, UsagePoller, http_fetch

_LOG = logging.getLogger("lgwd.main")

# 預設值（key 路徑照 design 決定 2；其餘無上游規定，見註）。
_STATE_DIR = os.path.expanduser("~/.local/state/llmgateway")
DEFAULT_DB = os.path.join(_STATE_DIR, "llm.db")
DEFAULT_KEYS_PATH = os.path.join(_STATE_DIR, "keys.env")
DEFAULT_POLICY_PATH = os.path.join("policy", "default.json")
DEFAULT_API_HOSTS = ("127.0.0.1",)
DEFAULT_API_PORT = 8480
DEFAULT_BOARD_PORT = 8481

ENV_DB = "LGWD_DB"
ENV_POLICY = "LGWD_POLICY"
ENV_KEYS = "LGWD_KEYS"
ENV_API_HOSTS = "LGWD_API_HOSTS"  # 逗號分隔
ENV_API_PORT = "LGWD_API_PORT"
ENV_BOARD_PORT = "LGWD_BOARD_PORT"
ENV_USAGE = "LGWD_USAGE"  # 1/true/yes/on 開；0/false/no/off 關

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass
class Config:
    db: str = DEFAULT_DB
    policy_path: str = DEFAULT_POLICY_PATH
    keys_path: str = DEFAULT_KEYS_PATH
    api_hosts: tuple[str, ...] = field(default_factory=lambda: DEFAULT_API_HOSTS)
    api_port: int = DEFAULT_API_PORT
    board_port: int = DEFAULT_BOARD_PORT
    usage_enabled: bool = True


def _parse_bool(text: str) -> bool:
    low = text.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ValueError("bad boolean (want 1/0, true/false, yes/no, on/off)")


def _parse_port(text: str) -> int:
    port = int(text)
    if not 0 <= port <= 65535:
        raise ValueError("port out of range")
    return port


def load_config(argv: list[str] | None = None, env=None) -> Config:
    """argv（不含程式名）優先，否則 env，否則預設。格式錯誤丟 ValueError。"""
    src = os.environ if env is None else env

    def env_of(name: str) -> str | None:
        value = src.get(name)
        return value if value not in (None, "") else None

    parser = argparse.ArgumentParser(prog="lgwd")
    parser.add_argument("--db", default=None)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--keys", default=None)
    parser.add_argument("--api-host", dest="api_hosts", action="append", default=None)
    parser.add_argument("--api-port", default=None)
    parser.add_argument("--board-port", default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--usage", dest="usage", action="store_true", default=None)
    group.add_argument("--no-usage", dest="usage", action="store_false", default=None)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    hosts: tuple[str, ...] | None = None
    if args.api_hosts is not None:
        hosts = tuple(h for h in args.api_hosts if h)
    elif env_of(ENV_API_HOSTS) is not None:
        hosts = tuple(h.strip() for h in env_of(ENV_API_HOSTS).split(",") if h.strip())
    if not hosts:
        hosts = DEFAULT_API_HOSTS

    try:
        if args.api_port is not None:
            api_port = _parse_port(args.api_port)
        elif env_of(ENV_API_PORT) is not None:
            api_port = _parse_port(env_of(ENV_API_PORT))
        else:
            api_port = DEFAULT_API_PORT
        if args.board_port is not None:
            board_port = _parse_port(args.board_port)
        elif env_of(ENV_BOARD_PORT) is not None:
            board_port = _parse_port(env_of(ENV_BOARD_PORT))
        else:
            board_port = DEFAULT_BOARD_PORT
        if args.usage is not None:
            usage = args.usage
        elif env_of(ENV_USAGE) is not None:
            usage = _parse_bool(env_of(ENV_USAGE))
        else:
            usage = True
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bad config value: {exc}") from exc

    return Config(
        db=args.db or env_of(ENV_DB) or DEFAULT_DB,
        policy_path=args.policy or env_of(ENV_POLICY) or DEFAULT_POLICY_PATH,
        keys_path=args.keys or env_of(ENV_KEYS) or DEFAULT_KEYS_PATH,
        api_hosts=hosts,
        api_port=api_port,
        board_port=board_port,
        usage_enabled=usage,
    )


def store_paths(args, env) -> tuple[str, str, str]:
    """admin 共用：(db, policy, keys)；args 是已解析的命名空間。"""
    def env_of(name: str) -> str | None:
        value = env.get(name)
        return value if value not in (None, "") else None

    return (
        args.db or env_of(ENV_DB) or DEFAULT_DB,
        args.policy or env_of(ENV_POLICY) or DEFAULT_POLICY_PATH,
        args.keys or env_of(ENV_KEYS) or DEFAULT_KEYS_PATH,
    )


class _BoardWithUsage:
    """轉發 LeaseService；board() 的 usage 由 poller 的 snapshot 填。"""

    def __init__(self, service: LeaseService, poller: UsagePoller):
        self._service = service
        self._poller = poller

    def __getattr__(self, name: str):
        return getattr(self._service, name)

    def board(self) -> dict:
        data = self._service.board()
        snap = self._poller.snapshot()
        fallback = UsageEntry().as_dict()
        for sub in data.get("subscriptions", []):
            sub["usage"] = snap.get(sub["name"], fallback)
        return data


class Gateway:
    """組裝並啟停服務；SIGHUP 時呼叫 reload()，SIGTERM 時呼叫 stop()。"""

    def __init__(self, config: Config, clock: Clock | None = None, fetch=None):
        self._config = config
        self._clock = clock or SystemClock()
        policy = load_policy(config.policy_path)  # 不合法直接丟，服務不啟動
        keys = KeyStore(config.keys_path)  # 讀不到直接丟，服務不啟動
        self._keys = keys
        self._service = LeaseService(config.db, policy, keys, self._clock)
        self._retired: list[LeaseService] = []
        self._poller = UsagePoller(keys, self._clock,
                                   fetch=fetch or http_fetch,
                                   enabled=config.usage_enabled)
        self.view = _BoardWithUsage(self._service, self._poller)
        self.api_servers = [make_api_server(self.view, h, config.api_port)
                            for h in config.api_hosts]
        self.board_server = make_board_server(self.view, "127.0.0.1", config.board_port)
        self._threads: list[threading.Thread] = []

    @property
    def service(self) -> LeaseService:
        return self._service

    @property
    def poller(self) -> UsagePoller:
        return self._poller

    @property
    def keys(self) -> KeyStore:
        return self._keys

    @property
    def api_addrs(self) -> list[tuple]:
        return [srv.server_address for srv in self.api_servers]

    @property
    def board_addr(self) -> tuple:
        return self.board_server.server_address

    def start(self) -> None:
        self._poller.poll_once()
        self._poller.start()
        for srv in (*self.api_servers, self.board_server):
            thread = threading.Thread(target=srv.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        for srv in (*self.api_servers, self.board_server):
            try:
                srv.shutdown()
            except Exception:  # 關閉中出錯不擋後面
                pass
            srv.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []
        self._poller.stop()
        self._service.close()
        for old in self._retired:
            old.close()
        self._retired = []

    def reload(self) -> None:
        """SIGHUP：重讀 key 檔與 policy；失敗沿用舊的、記 log（不含值）。"""
        try:
            self._keys.reload()
        except KeyFileError as exc:
            _LOG.warning("keys reload failed, keeping old: %s", exc)
        try:
            new_policy = load_policy(self._config.policy_path)
        except (PolicyError, OSError) as exc:
            _LOG.warning("policy reload failed, keeping old: %s", exc)
            return
        except Exception:  # 未預期錯誤同樣沿用舊的
            _LOG.exception("policy reload failed, keeping old")
            return
        try:
            # 重建服務再換過去（狀態全在 SQLite）；舊連線留到 stop 才關，
            # 進行中的請求不受影響。
            fresh = LeaseService(self._config.db, new_policy, self._keys, self._clock)
        except Exception:
            _LOG.exception("policy reload failed, keeping old")
            return
        old = self._service
        self._service = fresh
        self.view._service = fresh
        self._retired.append(old)


def main(argv: list[str] | None = None) -> int:
    try:
        config = load_config(argv)
    except ValueError as exc:
        print(f"lgwd: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:  # --help 或參數錯誤（argparse）
        return int(exc.code or 0)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        gateway = Gateway(config)
    except (PolicyError, KeyFileError, OSError) as exc:
        print(f"lgwd: cannot start: {exc}", file=sys.stderr)
        return 2
    stop = threading.Event()

    def on_hup(signum, frame):
        gateway.reload()

    def on_term(signum, frame):
        stop.set()

    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, on_hup)
    signal.signal(signal.SIGTERM, on_term)
    gateway.start()
    _LOG.info("serving api=%s board=%s usage=%s",
              gateway.api_addrs, gateway.board_addr, config.usage_enabled)
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()
    return 0
