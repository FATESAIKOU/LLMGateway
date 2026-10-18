"""lgwctl 的路徑與外部指令（#35 共同規格的「版面」固定值）。

固定值（共同規格，改一邊要三方一起改）：

- 程式：``<prefix>/opt/llmgateway/<tag>``，``current``（用中的版本）與
  ``previous``（上一版）兩個 symlink；
- 設定：``<prefix>/etc/llmgateway/llmgateway.env``（``LGW_API_PORT``）、
  ``tunnel.env``、``keys.pass``（root:llmgw 0640，單行 ``LLMGW_KEYS_PASSWORD=…``）；
- 狀態：``<prefix>/var/lib/llmgateway/``（``keys.env`` 0600、``llm.db``、
  ``.keysync.sha``、``.keysync.status``）；
- unit：``<prefix>/etc/systemd/system/``（system 層）。

``LLMGW_PREFIX``（預設空字串）是所有絕對路徑的前綴：正式機器留空，測試指到
暫存目錄（chown／chmod 也只在那裡發生）。外部指令都能換成假指令
（``LLMGW_SYSTEMCTL``、``LLMGW_GIT``、``LLMGW_CURL``、``LLMGW_GH``、
``LLMGW_SUDO``…），測試不呼叫真的 systemctl。

隧道金鑰與 known_hosts 維持在 ``~llmgw/.ssh``（本人決定 3），不在 prefix 底下；
``LLMGW_SERVICE_HOME`` 可以指定服務帳號的 HOME（測試用）。
"""

from __future__ import annotations

import os
import pwd
import re

# 版面固定值（#35 spec）。
OPT_DIR = "/opt/llmgateway"
ETC_DIR = "/etc/llmgateway"
STATE_DIR = "/var/lib/llmgateway"
UNIT_DIR = "/etc/systemd/system"
BIN_DIR = "/usr/local/bin"

SERVICE_NAME = "llmgateway.service"
TUNNEL_SERVICE_NAME = "llmgateway-tunnel.service"
UNIT_NAMES = (SERVICE_NAME, TUNNEL_SERVICE_NAME)

ENV_FILE_NAME = "llmgateway.env"
TUNNEL_ENV_NAME = "tunnel.env"
KEYS_PASS_NAME = "keys.pass"
KEYS_ENV_NAME = "keys.env"
DB_NAME = "llm.db"
KEYSYNC_STATUS_NAME = ".keysync.status"
KEYSYNC_SHA_NAME = ".keysync.sha"

OPS_DIR_NAME = "ops-scripts"
KEYSYNC_BIN_NAME = "key-sync"
TUNNEL_BIN_NAME = "llmgateway-tunnel"

# tag 格式與 deploy/install.sh、deploy/bootstrap.sh 同值；改一邊要改另一邊。
TAG_PATTERN = r"^lgw-v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$"
TAG_RE = re.compile(TAG_PATTERN)

DEFAULT_REPO = "https://github.com/FATESAIKOU/LLMGateway"
# 金鑰正本所在的 private repo（check 只問讀不讀得到，不讀內容）。
KEYS_REPO = "FATESAIKOU/LLMGateway-keys"
DEFAULT_API_PORT = 8480
DEFAULT_SERVICE_USER = "llmgw"
DEFAULT_LIST_WAIT = 30.0

# 環境變數名（測試用假指令、假路徑）。
ENV_PREFIX = "LLMGW_PREFIX"
ENV_SERVICE_USER = "LLMGW_SERVICE_USER"
ENV_SERVICE_HOME = "LLMGW_SERVICE_HOME"
ENV_SYSTEMCTL = "LLMGW_SYSTEMCTL"
ENV_JOURNALCTL = "LLMGW_JOURNALCTL"
ENV_GIT = "LLMGW_GIT"
ENV_CURL = "LLMGW_CURL"
ENV_GH = "LLMGW_GH"
ENV_SUDO = "LLMGW_SUDO"
ENV_SH = "LLMGW_SH"
ENV_USERADD = "LLMGW_USERADD"
ENV_PYTHON = "LLMGW_PYTHON"
ENV_REPO = "LLMGW_REPO"
ENV_KEYSYNC_SCRIPT = "LLMGW_KEYSYNC_SCRIPT"
ENV_LIST_WAIT = "LLMGW_LIST_WAIT"
# 解密密碼的變數名：一律不進子行程（child_env 預設移除；只有 sync 的子 shell
# 會明確放行，且它真正用的值是 keys.pass source 出來的，低-4）。
ENV_KEYS_PASSWORD = "LLMGW_KEYS_PASSWORD"

# 隧道守護的設定檔路徑（T3 的 ops-scripts/llmgateway-tunnel 讀這個變數；
# 這裡的探針要讓它讀新位置，而不是舊的 ~/.config/llmgateway/tunnel.env）。
ENV_TUNNEL_ENV = "LGW_TUNNEL_ENV"

# check 的 key 拉取新鮮度：lgwd 每 600 秒跑一次，狀態檔超過 3 個 interval（30 分鐘）
# 沒更新就算 fail（T2/T3 審查的補充）。真的有需要換 interval 的人可以調這個。
ENV_KEYSYNC_STALE = "LLMGW_KEYSYNC_STALE"
DEFAULT_KEYSYNC_STALE = 1800.0


def parse_env_file(path: str) -> dict:
    """讀 KEY=VALUE 的設定檔（llmgateway.env／tunnel.env）；讀不到回空字典。

    與 ops-scripts/llmgateway-tunnel 的 parse_env_file 同一套規則：略過空行與
    ``#`` 註解、去掉 ``export `` 前綴、值兩側的單／雙引號。
    """
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if name:
            values[name] = value
    return values


class Layout:
    """一次執行用到的位置與指令；``Layout.from_env()`` 由環境變數組出來。

    ``env`` 同時是子行程的基礎環境（假指令的路徑與開關都在裡面），所以測試
    只要傳一份 env 進來，不必動 os.environ。
    """

    def __init__(
        self,
        *,
        env: dict,
        prefix: str,
        service_user: str,
        service_home: str | None,
        systemctl: str,
        journalctl: str,
        git: str,
        curl: str,
        gh: str,
        sudo: str,
        sh: str,
        useradd: str,
        python3: str,
        repo: str,
        list_wait: float,
        keysync_stale: float,
        euid: int,
    ):
        self.env = dict(env)
        self.prefix = prefix
        self.service_user = service_user
        self._service_home = service_home
        self.systemctl = systemctl
        self.journalctl = journalctl
        self.git = git
        self.curl = curl
        self.gh = gh
        self.sudo = sudo
        self.sh = sh
        self.useradd = useradd
        self.python3 = python3
        self.repo = repo
        self.list_wait = list_wait
        self.keysync_stale = keysync_stale
        self.euid = euid

    @classmethod
    def from_env(cls, env=None) -> "Layout":
        src = dict(os.environ if env is None else env)

        def get(name: str, default: str | None) -> str | None:
            value = src.get(name)
            return default if value in (None, "") else value

        prefix = (get(ENV_PREFIX, "") or "").rstrip("/")
        if prefix == "/":
            prefix = ""
        raw_wait = get(ENV_LIST_WAIT, None)
        try:
            list_wait = float(raw_wait) if raw_wait is not None else DEFAULT_LIST_WAIT
        except ValueError:
            list_wait = DEFAULT_LIST_WAIT
        raw_stale = get(ENV_KEYSYNC_STALE, None)
        try:
            keysync_stale = float(raw_stale) if raw_stale is not None else DEFAULT_KEYSYNC_STALE
        except ValueError:
            keysync_stale = DEFAULT_KEYSYNC_STALE
        return cls(
            env=src,
            prefix=prefix,
            service_user=get(ENV_SERVICE_USER, DEFAULT_SERVICE_USER),
            service_home=get(ENV_SERVICE_HOME, None),
            systemctl=get(ENV_SYSTEMCTL, "systemctl"),
            journalctl=get(ENV_JOURNALCTL, "journalctl"),
            git=get(ENV_GIT, "git"),
            curl=get(ENV_CURL, "curl"),
            gh=get(ENV_GH, "gh"),
            sudo=get(ENV_SUDO, "sudo"),
            sh=get(ENV_SH, "/bin/sh"),
            useradd=get(ENV_USERADD, "useradd"),
            python3=get(ENV_PYTHON, "python3"),
            repo=get(ENV_REPO, DEFAULT_REPO),
            list_wait=list_wait,
            keysync_stale=keysync_stale,
            euid=os.geteuid(),
        )

    # -- 路徑 ---------------------------------------------------------------

    def _path(self, absolute: str) -> str:
        return absolute if not self.prefix else self.prefix + absolute

    @property
    def opt_dir(self) -> str:
        return self._path(OPT_DIR)

    @property
    def current_link(self) -> str:
        return os.path.join(self.opt_dir, "current")

    @property
    def previous_link(self) -> str:
        return os.path.join(self.opt_dir, "previous")

    def tag_dir(self, tag: str) -> str:
        return os.path.join(self.opt_dir, tag)

    @property
    def etc_dir(self) -> str:
        return self._path(ETC_DIR)

    @property
    def env_file(self) -> str:
        return os.path.join(self.etc_dir, ENV_FILE_NAME)

    @property
    def tunnel_env_file(self) -> str:
        return os.path.join(self.etc_dir, TUNNEL_ENV_NAME)

    @property
    def keys_pass_file(self) -> str:
        return os.path.join(self.etc_dir, KEYS_PASS_NAME)

    @property
    def state_dir(self) -> str:
        return self._path(STATE_DIR)

    @property
    def keys_env(self) -> str:
        return os.path.join(self.state_dir, KEYS_ENV_NAME)

    @property
    def db_file(self) -> str:
        return os.path.join(self.state_dir, DB_NAME)

    @property
    def keysync_status_file(self) -> str:
        return os.path.join(self.state_dir, KEYSYNC_STATUS_NAME)

    @property
    def keysync_sha_file(self) -> str:
        return os.path.join(self.state_dir, KEYSYNC_SHA_NAME)

    @property
    def unit_dir(self) -> str:
        return self._path(UNIT_DIR)

    def unit_file(self, name: str) -> str:
        return os.path.join(self.unit_dir, name)

    @property
    def bin_dir(self) -> str:
        return self._path(BIN_DIR)

    @property
    def keysync_script(self) -> str:
        """手動 key 拉取要跑的腳本（預設當版目錄裡的 ops-scripts/key-sync）。"""
        override = self.env.get(ENV_KEYSYNC_SCRIPT)
        if override:
            return override
        return os.path.join(self.current_link, OPS_DIR_NAME, KEYSYNC_BIN_NAME)

    @property
    def tunnel_bin(self) -> str:
        return os.path.join(self.current_link, OPS_DIR_NAME, TUNNEL_BIN_NAME)

    @property
    def service_home(self) -> str:
        """服務帳號的 HOME（隧道金鑰與 known_hosts 在這裡）。"""
        if self._service_home:
            return self._service_home
        try:
            return pwd.getpwnam(self.service_user).pw_dir
        except KeyError:
            return "/home/" + self.service_user

    # -- 讀狀態 -------------------------------------------------------------

    @staticmethod
    def _link_tag(link: str) -> str | None:
        try:
            target = os.readlink(link)
        except OSError:
            return None
        name = os.path.basename(target.rstrip("/"))
        return name or None

    def current_tag(self) -> str | None:
        return self._link_tag(self.current_link)

    def previous_tag(self) -> str | None:
        return self._link_tag(self.previous_link)

    def installed_tag(self) -> str | None:
        """用中的版本：先看 current 裡的 .lgw-tag，沒有才用 symlink 的名字。"""
        try:
            with open(os.path.join(self.current_link, ".lgw-tag"), encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
        return text or self.current_tag()

    def api_port(self) -> int:
        """llmgateway.env 的 LGW_API_PORT；讀不到或壞掉回預設 8480。"""
        raw = parse_env_file(self.env_file).get("LGW_API_PORT")
        if raw is None:
            return DEFAULT_API_PORT
        try:
            port = int(raw)
        except ValueError:
            return DEFAULT_API_PORT
        return port if 1 <= port <= 65535 else DEFAULT_API_PORT

    def tunnel_config_state(self) -> tuple[bool, str]:
        """隧道設定是否就緒；回 (就緒, 缺什麼的文字)。

        必填 RENDEZVOUS，IDENTITY／KNOWN_HOSTS 要是存在的檔案（本人決定 3：
        金鑰維持在 ~llmgw/.ssh）。文字不含值，只寫鍵名。
        """
        values = parse_env_file(self.tunnel_env_file)
        missing: list[str] = []
        if not values.get("RENDEZVOUS"):
            missing.append("RENDEZVOUS")
        for name in ("IDENTITY", "KNOWN_HOSTS"):
            value = values.get(name)
            if not value:
                missing.append(name)
            elif not os.path.isfile(value):
                missing.append(f"{name}（檔案不在）")
        if missing:
            return False, "、".join(missing)
        return True, ""

    def child_env(self, **extra) -> dict:
        """子行程的環境：這份 env 加 extra（值為 None 表示移除該變數）。

        **一律先移除 ``LLMGW_KEYS_PASSWORD``**（低-4）：那是解密密碼，只該經
        keys.pass／LoadCredential 給服務；操作者的 shell 若 export 過它，
        不該被 git／curl／gh／systemctl 全部繼承。要放行的呼叫方（sync）在
        extra 裡明確指名。
        """
        env = dict(self.env)
        env.pop(ENV_KEYS_PASSWORD, None)
        for name, value in extra.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = str(value)
        return env
