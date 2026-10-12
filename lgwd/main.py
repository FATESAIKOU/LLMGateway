"""服務入口（design 決定 5a、5b、6；tasks 1.3、1.5；v5：mtime 重讀）。只用標準函式庫。

- 設定：argv 優先，否則環境變數，否則預設值；--api-host 只收 127.0.0.1
  （spec key-vault：服務不對外，只綁 loopback）。
- 建 load_policy／KeyStore／RegistryService／make_api_server（單一位址）。
- 每個請求處理前看一次 keys.env 的 mtime（v5.4：key-sync 換檔後由這裡自己偵測，
  不再用 SIGHUP／ExecReload）：變了就重讀，失敗沿用舊的、記 log、不含值。
  policy 只在啟動時讀（換政策要重啟）。
- SIGTERM 正常關閉。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass

from .clock import Clock, SystemClock
from .http import make_api_server
from .keys import KeyFileError, KeyStore
from .policy import PolicyError, load_policy
from .registry import RegistryService

_LOG = logging.getLogger("lgwd.main")

# 預設值（key 路徑照 design 決定 5b；其餘無上游規定，見註）。
_STATE_DIR = os.path.expanduser("~/.local/state/llmgateway")
DEFAULT_DB = os.path.join(_STATE_DIR, "llm.db")
DEFAULT_KEYS_PATH = os.path.join(_STATE_DIR, "keys.env")
DEFAULT_POLICY_PATH = os.path.join("policy", "default.json")
LOOPBACK = "127.0.0.1"
DEFAULT_API_HOST = LOOPBACK
DEFAULT_API_PORT = 8480

ENV_DB = "LGWD_DB"
ENV_POLICY = "LGWD_POLICY"
ENV_KEYS = "LGWD_KEYS"
ENV_API_HOST = "LGWD_API_HOST"
ENV_API_PORT = "LGWD_API_PORT"


@dataclass
class Config:
    db: str = DEFAULT_DB
    policy_path: str = DEFAULT_POLICY_PATH
    keys_path: str = DEFAULT_KEYS_PATH
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT


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
    parser.add_argument("--api-host", dest="api_host", default=None)
    parser.add_argument("--api-port", default=None)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    api_host = args.api_host or env_of(ENV_API_HOST) or DEFAULT_API_HOST
    if api_host != LOOPBACK:
        # 只綁 loopback；其他位址（含 0.0.0.0、::1、主機名）一律拒絕。
        raise ValueError(f"api host must be {LOOPBACK}")
    try:
        if args.api_port is not None:
            api_port = _parse_port(args.api_port)
        elif env_of(ENV_API_PORT) is not None:
            api_port = _parse_port(env_of(ENV_API_PORT))
        else:
            api_port = DEFAULT_API_PORT
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bad config value: {exc}") from exc

    return Config(
        db=args.db or env_of(ENV_DB) or DEFAULT_DB,
        policy_path=args.policy or env_of(ENV_POLICY) or DEFAULT_POLICY_PATH,
        keys_path=args.keys or env_of(ENV_KEYS) or DEFAULT_KEYS_PATH,
        api_host=api_host,
        api_port=api_port,
    )


class _Current:
    """轉發到目前的 RegistryService；取用前先看 key 檔的 mtime（v5.4）。

    HTTP 層每個請求都會呼叫這裡轉發出去的方法，所以「處理請求前」=
    這個 __getattr__；取用服務屬性本身（例如 server 建構時）也會檢查一次。
    """

    def __init__(self, owner: "Gateway", service: RegistryService):
        self._owner = owner
        self.service = service

    def __getattr__(self, name: str):
        self._owner.reload_keys_if_changed()
        return getattr(self.service, name)


def _stat_stamp(path: str):
    """(mtime_ns, size, ino)；讀不到回 None。

    帶 inode 是因為 key-sync 用 rename 換檔（inode 一定變），這樣就算檔案系統的
    時間戳解析度粗也偵測得到。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class Gateway:
    """組裝並啟停服務；請求前偵測 key 檔變動，SIGTERM 時呼叫 stop()。"""

    def __init__(self, config: Config, clock: Clock | None = None):
        self._config = config
        self._clock = clock or SystemClock()
        policy = load_policy(config.policy_path)  # 不合法直接丟，服務不啟動
        self._keys_stamp = _stat_stamp(config.keys_path)  # 讀檔前先記，避免漏掉變動
        keys = KeyStore(config.keys_path)  # 讀不到直接丟，服務不啟動
        self._keys = keys
        self._keys_lock = threading.Lock()
        self._service = RegistryService(config.db, policy, keys, self._clock)
        self._current = _Current(self, self._service)
        self.api_server = make_api_server(self._current, config.api_host, config.api_port)
        self._thread: threading.Thread | None = None

    @property
    def service(self) -> RegistryService:
        return self._service

    @property
    def keys(self) -> KeyStore:
        return self._keys

    @property
    def addr(self) -> tuple:
        return self.api_server.server_address

    def start(self) -> None:
        thread = threading.Thread(
            target=self.api_server.serve_forever,
            kwargs={"poll_interval": 0.1}, daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        try:
            self.api_server.shutdown()
        except Exception:  # 關閉中出錯不擋後面
            pass
        self.api_server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._service.close()

    def reload_keys(self) -> bool:
        """重讀 key 檔；失敗沿用舊的、記 log（不含值）。回 True 表示重讀成功。"""
        try:
            self._keys.reload()
        except KeyFileError as exc:
            _LOG.warning("keys reload failed, keeping old: %s", exc)
            return False
        except Exception as exc:  # 未預期錯誤同樣沿用舊的
            _LOG.warning("keys reload failed, keeping old: %s", type(exc).__name__)
            return False
        _LOG.info("keys reload ok")
        return True

    def reload_keys_if_changed(self) -> bool:
        """keys.env 的 (mtime_ns, size, ino) 變了才重讀；回 True 表示這次有重讀。

        檢查在每個請求處理前跑；失敗時沿用舊 key，等下次檔案再變才重試。
        """
        stamp = _stat_stamp(self._config.keys_path)
        if stamp == self._keys_stamp:
            return False
        with self._keys_lock:
            if stamp == self._keys_stamp:  # 別的執行緒已經處理過
                return False
            self._keys_stamp = stamp
            return self.reload_keys()


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

    def on_term(signum, frame):
        _LOG.info("received SIGTERM, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    gateway.start()
    host, port = gateway.addr[:2]
    _LOG.info("serving api=%s:%s", host, port)
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()
    return 0
