"""服務入口（design 決定 5a、5b、6；tasks 1.3、1.5；v5：mtime 重讀）。只用標準函式庫。

- 設定：argv 優先，否則環境變數，否則預設值；--api-host 只收 127.0.0.1
  （spec key-vault：服務不對外，只綁 loopback）。
- 建 load_policy／KeyStore／RegistryService／make_api_server（單一位址）。
- 每個請求處理前看一次 keys.env 的 mtime（v5.4：key-sync 換檔後由這裡自己偵測，
  不再用 SIGHUP／ExecReload）：變了就重讀，失敗沿用舊的、記 log、不含值。
  policy 只在啟動時讀（換政策要重啟）。
- 內建 key 拉取計時（#35 T2，見 lgwd/keysync.py）：背景執行緒每 interval 秒跑一次
  ops-scripts/key-sync；interval 0 或 script 不可用＝關閉。失敗與逾時不影響 API。
- SIGTERM／SIGINT 正常關閉（含計時執行緒）。
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace as _dc_replace
from .auth import AuthConfig, StartupRefused, open_runtime
from .clock import Clock, SystemClock
from .http import make_api_server
from .keys import KeyFileError, KeyStore
from .keysync import DEFAULT_INTERVAL as DEFAULT_KEYSYNC_INTERVAL
from .keysync import DEFAULT_TIMEOUT as DEFAULT_KEYSYNC_TIMEOUT
from .keysync import KeySyncTimer, default_script as default_keysync_script
from .policy import PolicyError, load_policy
from . import roletable
from .rolesync import RoleSyncTimer, default_script as default_rolesync_script
from .providers import UsageDeps, provider_for, validate_endpoints
from .registry import RegistryService
from .usage import UsageService

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
ENV_KEYSYNC_SCRIPT = "LGW_KEYSYNC_SCRIPT"
ENV_KEYSYNC_INTERVAL = "LGW_KEYSYNC_INTERVAL"
ENV_KEYSYNC_TIMEOUT = "LGW_KEYSYNC_TIMEOUT"
ENV_USAGE_CLAUDE_CWD = "LGW_USAGE_CLAUDE_CWD"

# 停止時最多等這麼久讓「在途請求」做完再關庫（#263 高-1）。比單一請求的內部期限
# 長（讀 body 10 秒＋等號誌 1 秒＋驗章 2 秒＋accept 5 秒）；idle／慢連線不等。
INFLIGHT_DRAIN_SECONDS = 20.0


@dataclass
class Config:
    db: str = DEFAULT_DB
    policy_path: str = DEFAULT_POLICY_PATH
    keys_path: str = DEFAULT_KEYS_PATH
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    # 內建 key 拉取（#35 T2）：script None＝用預設路徑；interval 0＝整個關閉。
    keysync_script: str | None = None
    keysync_interval: float = DEFAULT_KEYSYNC_INTERVAL
    keysync_timeout: float = DEFAULT_KEYSYNC_TIMEOUT
    # Claude 取用量子行程的固定工作目錄（部署時事先 trust；#60）。None＝沒設定→取用量回 unknown。
    usage_claude_cwd: str | None = None
    auth: AuthConfig = AuthConfig()


def _parse_port(text: str) -> int:
    port = int(text)
    if not 0 <= port <= 65535:
        raise ValueError("port out of range")
    return port


def _parse_interval(text: str) -> float:
    """keysync interval：0 或正有限秒數（0＝關閉）。"""
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError("keysync interval out of range")
    return value


def _parse_timeout(text: str) -> float:
    """keysync timeout：正有限秒數。"""
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("keysync timeout out of range")
    return value


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
    parser.add_argument("--keysync-script", default=None)
    parser.add_argument("--keysync-interval", default=None)
    parser.add_argument("--keysync-timeout", default=None)
    parser.add_argument("--usage-claude-cwd", default=None)
    for flag in ("mode", "registry", "state-dir", "sshkeygen-path", "per-key-max", "verify-concurrency", "verify-timeout"):
        parser.add_argument("--auth-" + flag, default=None)
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
        if args.keysync_interval is not None:
            keysync_interval = _parse_interval(args.keysync_interval)
        elif env_of(ENV_KEYSYNC_INTERVAL) is not None:
            keysync_interval = _parse_interval(env_of(ENV_KEYSYNC_INTERVAL))
        else:
            keysync_interval = DEFAULT_KEYSYNC_INTERVAL
        if args.keysync_timeout is not None:
            keysync_timeout = _parse_timeout(args.keysync_timeout)
        elif env_of(ENV_KEYSYNC_TIMEOUT) is not None:
            keysync_timeout = _parse_timeout(env_of(ENV_KEYSYNC_TIMEOUT))
        else:
            keysync_timeout = DEFAULT_KEYSYNC_TIMEOUT
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bad config value: {exc}") from exc

    def auth_value(name, default):
        value = getattr(args, "auth_" + name)
        if value is not None:
            return value
        return env_of("LGWD_AUTH_" + name.upper()) or default

    try:
        defaults = AuthConfig()
        timeout = float(auth_value("verify_timeout", defaults.verify_timeout))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("auth verify timeout must be positive and finite")
        auth = AuthConfig(
            mode=auth_value("mode", defaults.mode),
            registry_path=auth_value("registry", defaults.registry_path),
            state_dir=auth_value("state_dir", defaults.state_dir),
            sshkeygen_path=auth_value("sshkeygen_path", defaults.sshkeygen_path),
            per_key_max=int(auth_value("per_key_max", defaults.per_key_max)),
            verify_concurrency=int(auth_value("verify_concurrency", defaults.verify_concurrency)),
            verify_timeout=timeout,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bad auth config: {exc}") from exc

    return Config(
        auth=auth,
        db=args.db or env_of(ENV_DB) or DEFAULT_DB,
        policy_path=args.policy or env_of(ENV_POLICY) or DEFAULT_POLICY_PATH,
        keys_path=args.keys or env_of(ENV_KEYS) or DEFAULT_KEYS_PATH,
        api_host=api_host,
        api_port=api_port,
        keysync_script=args.keysync_script or env_of(ENV_KEYSYNC_SCRIPT),
        keysync_interval=keysync_interval,
        keysync_timeout=keysync_timeout,
        usage_claude_cwd=args.usage_claude_cwd or env_of(ENV_USAGE_CLAUDE_CWD),
    )


def make_keysync(config: Config, env=None) -> KeySyncTimer | None:
    """依設定建內建 key 拉取計時器；關閉時回 None 並記一行 log。

    關閉＝`interval` 為 0，或 script 沒設又沒有預設路徑可用（不存在／不是執行檔）。
    """
    src = os.environ if env is None else env
    if config.keysync_interval <= 0:
        _LOG.info("keysync disabled: interval=0")
        return None
    script = config.keysync_script or default_keysync_script(src)
    if not os.path.isfile(script) or not os.access(script, os.X_OK):
        _LOG.warning("keysync disabled: script not usable: %s", script)
        return None
    return KeySyncTimer(script, config.keysync_interval, timeout=config.keysync_timeout,
                        keys_path=config.keys_path, env=src)


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
        self._owner.reload_roles_if_changed()
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


class _Inflight:
    """在途請求計數（#263 高-1）：數的是「已進入 handler._dispatch 的請求」。

    閒置連線、只送一半標頭的連線都還沒進到 ``_dispatch``，所以不算在途——停止時
    不必等它們（D1 的 ``daemon_threads=True`` 本來就不等）。計數器掛在 server
    物件上（``server.inflight``），handler 進出 ``_dispatch`` 時加一減一。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._count = 0

    def enter(self) -> None:
        with self._cond:
            self._count += 1

    def leave(self) -> None:
        with self._cond:
            self._count -= 1
            if self._count <= 0:
                self._cond.notify_all()

    @property
    def count(self) -> int:
        with self._cond:
            return self._count

    def wait_idle(self, timeout: float) -> bool:
        """等計數歸零，最多 ``timeout`` 秒；回 True 表示歸零了。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


def _track_inflight(server) -> _Inflight:
    """讓 server 的 handler 在每筆請求進出 ``_dispatch`` 時計數（#263 高-1）。

    不改 ``lgwd/http.py``：``make_api_server`` 每次呼叫都產生新的 handler 類別，
    所以在這裡換成它的子類別，只影響這個 server（測試自己建的 server 不受影響）。
    連線執行緒維持 ``daemon_threads=True``（與 D1 相同），``server_close()`` 因此
    不會 join 閒置／慢連線的執行緒。
    """
    inflight = _Inflight()
    server.inflight = inflight
    base = server.RequestHandlerClass

    class _CountedHandler(base):
        def _dispatch(self, method):
            tracker = self.server.inflight
            tracker.enter()
            try:
                return super()._dispatch(method)
            finally:
                tracker.leave()

    server.RequestHandlerClass = _CountedHandler
    return inflight


class Gateway:
    """組裝並啟停服務；請求前偵測 key 檔變動，SIGTERM 時呼叫 stop()。"""

    def __init__(self, config: Config, clock: Clock | None = None,
                 usage_deps: UsageDeps | None = None, auth_runtime=None):
        self._auth_runtime = auth_runtime
        self._config = config
        self._clock = clock or SystemClock()
        policy = load_policy(config.policy_path)  # 不合法直接丟，服務不啟動
        try:
            validate_endpoints()  # providers 表的 endpoint 不得內嵌憑證；不合法不啟動
        except ValueError as exc:
            # providers 只依賴 policy 的常數、不丟 PolicyError：固定字串在這裡轉成 PolicyError，
            # 讓 spec 的「PolicyError、lgwd 不啟動」字面成立。
            raise PolicyError(str(exc)) from None
        self._keys_stamp = _stat_stamp(config.keys_path)  # 讀檔前先記，避免漏掉變動
        keys = KeyStore(config.keys_path)  # 讀不到直接丟，服務不啟動
        self._keys = keys
        self._keys_lock = threading.Lock()
        self._usage = self._make_usage(policy, usage_deps)
        self._role_policy = policy
        prefix = os.environ.get("LLMGW_PREFIX", "").rstrip("/")
        self._roles_path = prefix + "/var/lib/llmgateway/role-rotation.json"
        self._roles_lock = threading.Lock()
        stamp = _stat_stamp(self._roles_path)
        self._roles = roletable.load_valid(self._roles_path, policy)
        self._roles_stamp = stamp if self._roles is not None else None
        if self._roles is None:
            _LOG.warning("role table unavailable; using tag fallback")
            self._roles = roletable.load_valid(os.path.join(os.path.dirname(config.policy_path), "role-rotation.default.json"), policy)
        self._service = RegistryService(config.db, policy, keys, self._clock, usage=self._usage, roles_provider=lambda: self._roles)
        self._current = _Current(self, self._service)
        self.api_server = make_api_server(self._current, config.api_host, config.api_port,
                                          gate=auth_runtime.gate if auth_runtime else None)
        if auth_runtime is not None:
            auth_runtime.auth_log = self.api_server.auth_log  # C-607 收尾（#315）
        # 停止時只等「在途請求」，不等閒置／慢連線；連線執行緒維持 daemon
        # （與 D1 相同，#263 高-1：設成 False 會讓 server_close() 無限期 join）。
        self._inflight = _track_inflight(self.api_server)
        script = default_rolesync_script()
        self.rolesync = RoleSyncTimer(script) if os.access(script, os.X_OK) else None
        if self.rolesync is not None:
            self.rolesync.start()
        self.keysync = make_keysync(config)  # 關閉時是 None
        self._thread: threading.Thread | None = None

    def _make_usage(self, policy, usage_deps: UsageDeps | None) -> UsageService:
        """依 providers 的取用量掛點組裝 UsageService（#60）；啟動時掃 Claude 取用量的舊殘留。"""
        deps = usage_deps or UsageDeps(claude_cwd=self._config.usage_claude_cwd, clock=self._clock)
        if getattr(deps, "clock", None) is None:
            deps = _dc_replace(deps, clock=self._clock)
        fetchers = {}
        for name in policy.subscriptions:
            fetcher = provider_for(name).make_usage_fetcher(deps)
            if fetcher is not None:
                fetchers[name] = fetcher
                sweep = getattr(fetcher, "sweep", None)
                if sweep is not None:
                    sweep()
        return UsageService(
            self._clock, fetchers=fetchers, settings=policy.usage,
            key_of=self._usage_key, deployed=lambda name: self._keys.deployed(name))

    def _usage_key(self, subscription: str) -> str | None:
        """取用量執行緒用：換 key 後用新值（先重讀再取）；None＝沒部署。"""
        self.reload_keys_if_changed()
        return self._keys.get(subscription)

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
        if self.keysync is not None:
            self.keysync.start()  # 啟動後先跑一次（在 30 秒內），之後照 interval

    def stop(self) -> None:
        if self.rolesync is not None:
            self.rolesync.stop()
        self._usage.stop()  # 先設已停止旗標並收掉取用量子行程，再停其他
        if self.keysync is not None:
            self.keysync.stop()  # 先叫停 key 拉取，再關 API
        try:
            self.api_server.shutdown()  # 不再接受新連線
        except Exception:  # 關閉中出錯不擋後面
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        # 最多等 N 秒讓已進來的請求做完，才關兩庫、放鎖（#263 高-1）。超過上限還在
        # 跑的請求之後碰到已關閉的庫只會得到例外，gate 會把它轉成 UNAVAILABLE
        # （fail-closed）；鎖在兩庫關閉之後才放，下一個 lgwd 不會同時寫庫。
        if not self._inflight.wait_idle(INFLIGHT_DRAIN_SECONDS):
            _LOG.warning(
                "inflight requests still running after %.0f s, closing anyway",
                INFLIGHT_DRAIN_SECONDS)
        self.api_server.server_close()
        self._service.close()
        if self._auth_runtime is not None:
            self._auth_runtime.stop()

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

        檢查在每個請求處理前跑。重讀失敗時**不推進戳**，所以下一個請求會再試
        （不會因為「戳已是新的」就擺著不管）；舊 key 全程沿用。key-sync 只寫驗過
        的檔，實務上失敗多來自人工手改壞，修好後下一個請求就會讀到。
        """
        stamp = _stat_stamp(self._config.keys_path)
        if stamp == self._keys_stamp:
            return False
        with self._keys_lock:
            if stamp == self._keys_stamp:  # 別的執行緒已經處理過
                return False
            ok = self.reload_keys()
            if ok:
                self._keys_stamp = stamp
            return ok

    def reload_roles_if_changed(self):
        stamp = _stat_stamp(self._roles_path)
        if stamp == self._roles_stamp:
            return False
        with self._roles_lock:
            stamp = _stat_stamp(self._roles_path)
            if stamp == self._roles_stamp:
                return False
            table = roletable.load_valid(self._roles_path, self._role_policy)
            if table is None:
                _LOG.warning("role table reload failed")
                return False
            self._roles = table
            self._roles_stamp = stamp
            return True


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
    runtime = None
    try:
        if config.auth.mode != "off":
            runtime = open_runtime(config.auth, SystemClock())
            runtime.start()
        gateway = Gateway(config, auth_runtime=runtime)
    except StartupRefused as exc:
        print(f"lgwd: cannot start authentication: {exc.store or ''} {exc.message}", file=sys.stderr)
        return 78
    except (PolicyError, KeyFileError, OSError) as exc:
        if runtime is not None:
            runtime.stop()
        print(f"lgwd: cannot start: {exc}", file=sys.stderr)
        return 2
    except BaseException:
        # 低-1（#263）：其他未預期例外也要先收掉已啟動的 watcher／purge 執行緒，
        # 再照原樣往上丟（結束碼由 Python 決定）。
        if runtime is not None:
            runtime.stop()
        raise
    signalled: list[int] = []

    def on_signal(signum, frame):
        # #263 修正第 2 輪：SIGTERM 與 SIGINT 共用同一個 handler，且一路安裝到
        # gateway.stop() 跑完、行程退出為止，**不還原預設行為**。收尾期間重複收到的
        # 訊號只會再 append 一次（主迴圈已離開，沒有影響），不會加速也不會打斷收尾
        # ——尤其不會讓行程在鎖還沒放、兩庫還沒關時以預設處置被殺掉。
        # #297：handler 只把號碼記下來（list.append 不取鎖、不寫 I/O），其他什麼都
        # 不做。原本先寫一行 log 再 stop.set()：SIGTERM 恰好落在主執行緒自己寫
        # stderr 的中間時，logging 撞上 BufferedWriter 的重入保護丟 RuntimeError，
        # 例外從 handler 冒出去，stop.set() 沒被執行——這次 SIGTERM 就被吞掉
        # （systemd 只送一次），行程要等到 TimeoutStopSec 才被 SIGKILL。而
        # stop.set() 本身也會取 threading.Event 的（不可重入）鎖：訊號落在主執行緒
        # 持有那把鎖的幾微秒內，handler 在同一執行緒再取鎖就永久死結（#297 中-1）。
        # 所以 handler 連 set 都不做，log 也由主執行緒在主迴圈醒來之後才寫。
        signalled.append(signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    gateway.start()
    host, port = gateway.addr[:2]
    _LOG.info("serving api=%s:%s", host, port)
    try:
        # 只檢查 list（不需要鎖）。time.sleep 被訊號打斷時會先跑 handler，再睡完剩下
        # 的時間，所以最多晚 0.5 秒醒來；不需要 Event 的 set／wait，也就沒有 handler
        # 與主執行緒互搶同一把鎖的問題。
        while not signalled:
            time.sleep(0.5)
    finally:
        # 這裡到 return 之前都不碰 signal.signal：handler 保持安裝，第二次 SIGTERM
        # 落在收尾中時只會再 append 一次（見上）。log 在訊號已到之後、由主執行緒寫
        # （#297），不在 handler 裡寫。
        if signalled:
            _LOG.info("received %s, shutting down", signal.Signals(signalled[0]).name)
        gateway.stop()
    return 0
