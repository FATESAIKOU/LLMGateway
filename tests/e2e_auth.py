"""D6a 離線整合驗收的共用環境（#222；契約 C-950、C-955 的收集面）。

**這個檔不是測試模組**（`unittest discover` 不收，檔名不是 `test_*`），只給
`tests/test_e2e_auth_matrix.py` 匯入。

每個 `Board` 是一個**獨立暫存版面**（C-950）：自己的 `etc/llmgateway`、
`var/lib/llmgateway`、`keys.env`、`policy.json`、名冊檔、兩個庫、審計檔與 status 檔；
lgwd 一律是**真的子行程**（`python3 -m lgwd`），用真的 `ssh-keygen`（9.6p1）與
`prlimit`，兩個庫由真的 `llmgwctl auth-store init` 建、名冊由真的
`llmgwctl auth-registry install` 裝。收尾一定 SIGTERM，並斷言結束碼 0、認證鎖已
釋放、埠可重綁、沒有殘留子行程。

接縫（不新增正式程式的後門）：

- 名冊檔擁有者：`lgwd.main.open_runtime` 沒有 CLI 旗標可以改
  `registry_owner_uid`，所以照 `tests/test_process_auth_263.py` 的作法，在暫存目錄
  放 `sitecustomize.py`（`PYTHONPATH` 指向它）在子行程內把 `open_runtime` 包一層、
  補上 `registry_owner_uid=os.getuid()`。`lgwd/` 不改一行。
- `llmgwctl` 的 root 判斷：在行程內呼叫 `lgwctl.cli.main`，用 `LLMGW_PREFIX` 指到
  暫存版面；`Layout.from_env` 換成測試替身（回一份 `euid=0` 的版面），只在測試行程
  內生效。
- 不讀憑證：金鑰是 `ssh-keygen` 現產的臨時 ed25519；`keys.env` 只有固定的假字串。

洩密掃描（C-955）的樣本由 `SAMPLES` 收集：所有 `lgw.signer.Signer.headers()` 的輸出
（簽章、nonce、時間戳、key ID）、三把測試公鑰的全文與 base64、私鑰檔內容、假憑證值
與本次產生的簽章 blob。`ARTIFACTS` 收集要掃的輸出（lgwd 的 stdout／stderr、
`auth-audit.log`、status 檔、`llmgwctl` 的輸出、`lgw` 的 `Result` 與 `repr`）。
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import fcntl
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

from lgwctl.layout import Layout
from lgwd.auth_paths import auth_paths
from tests.auth_fakes import principal, principal_all, write_registry_file
from tests.auth_signer import Signer, key_entry, registry_bytes
from tests.subprocess_policy import usage_all_disabled
from tests.test_http_framing import exchange, normalize, parse_response, req
from tests.test_main import FAKE_KEYS, key_text

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_KEYGEN = "/usr/bin/ssh-keygen"
PRLIMIT = "/usr/bin/prlimit"

#: 名冊裡的三個 principal／key（B 的預設值、本機管理 principal、只有 list 的窄權限）。
B_PRINCIPAL = "w1-worker"
ADMIN_PRINCIPAL = "w1-admin"
NARROW_PRINCIPAL = "w1-narrow"
B_KEY_ID = "w1-key-b"
ADMIN_KEY_ID = "llmgwctl-local-admin"
NARROW_KEY_ID = "w1-key-narrow"

#: 本機管理 principal：list／usage／roles，**沒有** resolve 與 cooldown。
ADMIN_PERMISSIONS = {
    "ops": ["list", "usage", "roles"],
    "resolve_agents": [],
    # 與 docs/deploy.md 8.4 的 local-admin 相同：五個角色都填滿（四個 agent）。
    "roles": {
        "PMO": ["claude-code", "opencode", "agy", "codex"],
        "PM": ["claude-code", "opencode", "agy", "codex"],
        "impl": ["claude-code", "opencode", "agy", "codex"],
        "rev": ["claude-code", "opencode", "agy", "codex"],
        "qa": ["claude-code", "opencode", "agy", "codex"],
    },
}
#: 只有 list 的窄權限 principal（C-951／C-952 的負向格子）。
NARROW_PERMISSIONS = {"ops": ["list"], "resolve_agents": [], "roles": {}}

#: 五條業務路由與 healthz。cooldown 的 until 由 `_until()` 現算（必須在未來 8 天內）。
ROUTES = (
    ("list", "GET", "/v1/subscriptions", b""),
    ("usage", "GET", "/v1/usage", b""),
    ("resolve", "GET", "/v1/resolve?agent=opencode&provider=opencode-zen", b""),
    ("roles", "GET", "/v1/roles/impl?agent=opencode", b""),
    ("cooldown", "POST", "/v1/subscriptions/opencode-zen/cooldown", None),
)
HEALTHZ = ("healthz", "GET", "/healthz", b"")

#: 認證日誌要對得上的路由模板（`lgwd.http._ROUTE_TEMPLATES` 的同形狀）。
ROUTE_TEMPLATES = {
    "list": "/v1/subscriptions",
    "usage": "/v1/usage",
    "resolve": "/v1/resolve",
    "roles": "/v1/roles/{role}",
    "cooldown": "/v1/subscriptions/{id}/cooldown",
}

#: 假 key 檔的值（假憑證；C-955 的樣本之一）。
FAKE_CREDENTIALS = tuple(sorted(set(FAKE_KEYS.values())))


def _until() -> bytes:
    moment = datetime.now(timezone.utc) + timedelta(hours=1)
    return ('{"until": "%s"}' % moment.strftime("%Y-%m-%dT%H:%M:%SZ")).encode("ascii")


def route_body(target):
    """一條路由實際送出的 body（cooldown 現算；其餘空 bytes）。"""
    return _until() if target == "/v1/subscriptions/opencode-zen/cooldown" else b""


# 洩密掃描（C-955）：樣本與要掃的輸出都是模組級，整個檔案共用。
# 樣本除了完整值，還收「衍生」樣本：簽章 blob 的尾段（base64／hex）與簽章字串的
# 頭／尾段、nonce 的 hex 與標準 base64、公鑰 base64 的中段。這樣除錯日誌只印截斷的
# 簽章（Ma）或把 nonce 轉成 hex（Mb）時也抓得到（REPORT-286-archreview 中-1）。
SAMPLES: dict[str, set[str]] = {
    "signature": set(),
    "signature_prefix": set(),
    "signature_tail": set(),
    "signature_blob": set(),
    "signature_hex": set(),
    "nonce": set(),
    "nonce_hex": set(),
    "nonce_b64": set(),
    "public_key": set(),
    "private_key": set(),
    "credential": set(),
}
ARTIFACTS: list[tuple[str, str]] = []


#: 樣本的最短長度：太短的值（例如 "1"、key ID）在任何輸出裡都可能自然出現，不是
#: 這次產生過的祕密值。C-955 要的樣本是簽章、nonce、公鑰 base64、私鑰內容與假憑證。
MIN_SAMPLE_CHARS = 16


def add_sample(kind: str, value) -> None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = base64.b64encode(value).decode("ascii")
    if value and len(str(value)) >= MIN_SAMPLE_CHARS:
        SAMPLES[kind].add(str(value))


def add_artifact(label: str, text) -> None:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    ARTIFACTS.append((str(label), str(text)))


def _b64url_decode(text: str) -> bytes | None:
    """無 padding 的 base64url → bytes；格式不對回 None。"""
    try:
        return base64.urlsafe_b64decode(str(text) + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None


def add_signature_samples(value) -> None:
    """一個簽章值的完整與衍生樣本（240 字元＝180 bytes blob 的 base64url）。"""
    if not value:
        return
    text = str(value)
    add_sample("signature", text)
    # 截斷的日誌：只印前 64 字元（Ma）也要抓到。
    add_sample("signature_prefix", text[:64])
    add_sample("signature_tail", text[-32:])
    blob = _b64url_decode(text)
    if blob:
        tail = blob[-64:]          # ed25519 簽章本身（每次不同、真正是祕密的那段）
        add_sample("signature_blob", base64.b64encode(tail).decode("ascii"))
        add_sample("signature_blob", base64.urlsafe_b64encode(tail).rstrip(b"=").decode("ascii"))
        add_sample("signature_hex", tail.hex())


def add_nonce_samples(value) -> None:
    """一個 nonce 的完整與衍生樣本（43 字元＝32 bytes 的 base64url）。"""
    if not value:
        return
    text = str(value)
    add_sample("nonce", text)
    raw = _b64url_decode(text)
    if raw:
        add_sample("nonce_hex", raw.hex())                      # Mb：hex 日誌
        add_sample("nonce_b64", base64.b64encode(raw).decode("ascii"))  # 含 padding


def record_headers(headers) -> None:
    """把一次簽章的輸出收成洩密樣本（簽章與 nonce 的完整與衍生值）。"""
    data = dict(headers)
    add_signature_samples(data.get("X-LGW-Signature"))
    add_nonce_samples(data.get("X-LGW-Nonce"))


def contains_credential(text: str) -> bool:
    """body 裡有沒有本次的假憑證值（任何回應都完整掃描，這裡只給報告／除錯用）。"""
    return any(value in text for value in SAMPLES["credential"])


def _route_path(target) -> str:
    """request-target → 路徑（去掉 query；`None` 原樣回 None）。"""
    if target is None:
        return None
    return str(target).split("?", 1)[0]


def _redact_env(text: str) -> str:
    """200 的 resolve body：只把 `env` 的值換成 `<env>`，其餘原樣留下來掃。

    排除條件是「status 200 且路由是 /v1/resolve」，不是「body 含憑證」；後者會讓
    任何洩漏憑證的回應被當成交付管道而放過（REPORT-286-archreview2 中-1）。
    """
    try:
        document = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return text
    if not isinstance(document, dict) or "env" not in document:
        return text
    redacted = dict(document)
    redacted["env"] = "<env>"
    return json.dumps(redacted, ensure_ascii=False)


def record_response(label: str, status, payload, headers=None, path=None) -> None:
    """把一次 HTTP 回應的標頭與 body 收進掃描清單（C-955 的回應面）。

    只有「status 200 且路由是 `/v1/resolve`」時才把 body 的 `env` 值換成 `<env>`：
    那是設計上的 env 交付管道。其他任何回應（含 200 的 list／usage／roles 與
    401／403／429／503 的 body）以及所有回應標頭都完整掃描。

    `path` 是 request-target（可含 query）或路徑；直接請求由呼叫端傳 target，
    `lgw.client` 由 `_finish` 的記錄器從 `_send` 的 path 帶進來。
    """
    lines = ["status %s" % status]
    items = ()
    if headers is not None:
        try:
            items = headers.items()
        except AttributeError:
            items = headers
    for name, value in items:
        lines.append("%s: %s" % (name, value))
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", "replace")
    else:
        text = str(payload)
    if status == 200 and _route_path(path) == "/v1/resolve":
        text = _redact_env(text)
    lines.append(text)
    add_artifact("response " + label + " headers+body", "\n".join(lines))


#: 子行程注入：名冊擁有者 uid（C-950 的接縫）＋事件記錄（spawn／touch／open_runtime）。
HOOK = '''
import builtins, os, sqlite3, subprocess

watch = os.environ.get("E2E_WATCH_PREFIX")
events = os.environ.get("E2E_EVENTS")


def note(event):
    if events:
        with builtins.open(events, "a") as fh:
            fh.write(event + "\\n")


def track(path):
    if watch and str(path).startswith(watch):
        note("touch")


_old_open, _old_stat, _old_connect = os.open, os.stat, sqlite3.connect


def _open(path, *a, **kw):
    track(path)
    return _old_open(path, *a, **kw)


def _stat(path, *a, **kw):
    track(path)
    return _old_stat(path, *a, **kw)


def _connect(path, *a, **kw):
    track(path)
    return _old_connect(path, *a, **kw)


os.open, os.stat, sqlite3.connect = _open, _stat, _connect

if os.environ.get("E2E_CONN_EVENTS"):
    import socketserver
    _finish = socketserver.BaseServer.finish_request

    def finish_request(self, request, client_address):
        with builtins.open(os.environ["E2E_CONN_EVENTS"], "a") as fh:
            fh.write("conn\\n")
        return _finish(self, request, client_address)

    socketserver.BaseServer.finish_request = finish_request

import lgwd.main as main

_original_open_runtime = main.open_runtime


def _open_runtime(cfg, clock, **kwargs):
    note("open_runtime")
    kwargs.setdefault("registry_owner_uid", os.getuid())
    return _original_open_runtime(cfg, clock, **kwargs)


main.open_runtime = _open_runtime

_original_popen = subprocess.Popen


class _CountPopen(_original_popen):
    def __init__(self, argv, *a, **kwargs):
        try:
            name = os.path.basename(argv[0]) if argv else "?"
        except Exception:
            name = "?"
        note("spawn " + name)
        super().__init__(argv, *a, **kwargs)


subprocess.Popen = _CountPopen
'''

#: 假 ssh（隧道探針的傳輸層）：把 `-W host:port` 當成本機連線，其餘選項忽略。
#: 探針函式本身（`probe_healthy`）是真的；這裡只跳過 ssh 這一段（D1 另有測試）。
FAKE_SSH = '''#!/usr/bin/env python3
import socket
import sys

argv = sys.argv[1:]
if "-W" not in argv:
    sys.exit(255)
target = argv[argv.index("-W") + 1]
host, _, port = target.rpartition(":")
request = sys.stdin.buffer.read()
try:
    sock = socket.create_connection((host, int(port)), timeout=5)
    sock.sendall(request)
    chunks = []
    while True:
        part = sock.recv(65536)
        if not part:
            break
        chunks.append(part)
    sock.close()
except OSError:
    sys.exit(255)
sys.stdout.buffer.write(b"".join(chunks))
sys.stdout.buffer.flush()
sys.exit(0)
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _PortBusy(Exception):
    """lgwd 啟動時撞到別人已佔用的埠（rc=2、`Address already in use`）。"""


def _listening_on(port: int) -> bool:
    """`/proc/net/tcp{,6}` 裡有沒有一個 LISTEN（狀態 0A）在本機的這個埠上。"""
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == int(port):
                return True
    return False


def install_signer_recorder():
    """把 `lgw.signer.Signer.headers` 包一層，記錄每次產生的簽章與 nonce。

    回傳「還原」函式。所有用 `lgw.signer.Signer` 的呼叫方（`lgw.client`、
    `llmgwctl usage`／`check`、本檔的直接請求）都因此被 `SAMPLES` 覆蓋（C-955）。
    """
    original = None
    try:
        from lgw.signer import Signer as LgwSigner
    except Exception:  # pragma: no cover - 匯入失敗時讓測試自己紅
        return lambda: None
    original = LgwSigner.headers

    def recording(self, **kwargs):
        headers = original(self, **kwargs)
        record_headers(headers)
        return headers

    LgwSigner.headers = recording

    def restore():
        LgwSigner.headers = original

    return restore


#: `Client._send` 目前正在送的路徑（給 `_finish` 的記錄器判斷「200 的 resolve」）。
_CLIENT_PATH = contextvars.ContextVar("lgw_e2e_client_path", default=None)


def install_client_recorder():
    """把 `lgw.client.Client._finish` 與 `_send` 包一層，記錄每個客戶端回應。

    `Result` 本身只留狀態碼與 error，標頭與 body 不會出現在 `repr` 裡；C-955 要求掃
    「Client 的結果」，所以直接攔截 `_finish`（靜態方法）把標頭與 body 收進
    `ARTIFACTS`。排除條件要看**路由**，`_finish` 拿不到路徑，因此由 `_send` 的包裝把
    目前的路徑放進 ContextVar，記錄器再取出來（只有 status 200 且路徑是
    `/v1/resolve` 時才把 body 的 `env` 換成佔位字串）。
    """
    from lgw.client import Client

    original_finish = Client._finish
    original_send = Client._send

    def recording_finish(status, payload, error_data=False, headers=None):
        record_response("lgw-client", status, payload, headers,
                        path=_CLIENT_PATH.get())
        return original_finish(status, payload, error_data, headers)

    def recording_send(self, method, path, body, error_data=False):
        token = _CLIENT_PATH.set(path)
        try:
            return original_send(self, method, path, body, error_data)
        finally:
            _CLIENT_PATH.reset(token)

    Client._finish = staticmethod(recording_finish)
    Client._send = recording_send

    def restore():
        Client._finish = staticmethod(original_finish)
        Client._send = original_send

    return restore


class Keys:
    """三把臨時 ed25519 金鑰（B、本機管理、窄權限）與它們的名冊資料。"""

    def __init__(self, directory: str):
        self.directory = directory
        self.b = Signer(directory, "key-b")
        self.admin = Signer(directory, "key-admin")
        self.narrow = Signer(directory, "key-narrow")
        os.chmod(self.admin.private_path, 0o600)
        for signer in (self.b, self.admin, self.narrow):
            add_sample("public_key", signer.public_key_line)
            b64 = signer.public_key_line.split()[1]
            add_sample("public_key", b64)
            # 中間 32 字元：只印公鑰中段的日誌也抓得到（REPORT-286-archreview 中-1）。
            middle = len(b64) // 2
            add_sample("public_key", b64[middle - 16:middle + 16])
            with open(signer.private_path, "r", encoding="ascii") as handle:
                private_text = handle.read()
            add_sample("private_key", private_text.strip())
            for line in private_text.splitlines():
                line = line.strip()
                if line and not line.startswith("-----"):
                    add_sample("private_key", line)

    def entries(self) -> list:
        return [
            key_entry(B_KEY_ID, self.b, principal_name=B_PRINCIPAL),
            key_entry(ADMIN_KEY_ID, self.admin, principal_name=ADMIN_PRINCIPAL),
            key_entry(NARROW_KEY_ID, self.narrow, principal_name=NARROW_PRINCIPAL),
        ]

    def registry(self, generation: int = 1) -> bytes:
        principals = {
            B_PRINCIPAL: principal_all(),
            ADMIN_PRINCIPAL: dict(ADMIN_PERMISSIONS),
            NARROW_PRINCIPAL: dict(NARROW_PERMISSIONS),
        }
        return registry_bytes(generation, self.entries(), principals)

    def client_signer(self, which: str = "b"):
        """一個真的 `lgw.signer.Signer`（給 `lgw.client.Client` 與直接請求用）。"""
        from lgw.signer import Signer as LgwSigner

        mapping = {
            "b": (B_KEY_ID, self.b),
            "admin": (ADMIN_KEY_ID, self.admin),
            "narrow": (NARROW_KEY_ID, self.narrow),
        }
        key_id, factory = mapping[which]
        return LgwSigner(key_id=key_id, private_key_path=factory.private_path)

    def unregistered_signer(self):
        """格式合法但名冊沒有的 key ID（用來拿真的 401；簽章還是真的）。"""
        from lgw.signer import Signer as LgwSigner

        return LgwSigner(key_id="w1-key-unregistered", private_key_path=self.b.private_path)


class Board:
    """一個獨立的暫存版面＋真的 lgwd 子行程（C-950）。"""

    def __init__(self, keys: Keys, mode: str, *, per_key_max: int = 600,
                 name: str = "board", extra_args=()):
        self.keys = keys
        self.name = name
        self.per_key_max = per_key_max
        self.extra_args = tuple(extra_args)
        self.runs = 0
        self.proc = None
        self._log = None
        self._log_path = None
        self._stopped = False
        self.tmp = tempfile.TemporaryDirectory(prefix="lgw-e2e-")
        self.root = Path(self.tmp.name)
        self.state = self.root / "var" / "lib" / "llmgateway"
        self.etc = self.root / "etc" / "llmgateway"
        self.state.mkdir(parents=True, mode=0o700)
        self.etc.mkdir(parents=True)
        self.keys_env = self.root / "keys.env"
        self.policy = self.root / "policy.json"
        self.db = self.root / "llm.db"
        self.registry = self.etc / "auth-registry.json"
        self.candidate = self.root / "candidate.json"
        self.env_file = self.etc / "llmgateway.env"
        self.events = self.root / "events.log"
        self.connections = self.root / "conn.log"
        self.sitecustomize = self.root / "sitecustomize.py"
        self.port = free_port()
        self._prepare(mode)
        self.start(mode)

    # --- 版面 ---------------------------------------------------------------

    def _prepare(self, mode: str) -> None:
        self.keys_env.write_text(key_text(FAKE_KEYS), encoding="utf-8")
        os.chmod(self.keys_env, 0o600)
        policy = json.loads((REPO_ROOT / "policy" / "default.json").read_text(encoding="utf-8"))
        policy["usage"] = usage_all_disabled()
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        self.sitecustomize.write_text(HOOK, encoding="utf-8")
        self._write_env(mode)
        self.cli_env = {
            "PATH": "/usr/bin:/bin",
            "LLMGW_PREFIX": str(self.root),
            "LGWD_AUTH_STATE_DIR": str(self.state),
            "LGWD_AUTH_REGISTRY": str(self.registry),
            "LLMGW_LOCAL_ADMIN_KEY": self.keys.admin.private_path,
            "LLMGW_LOCAL_ADMIN_KEY_ID": ADMIN_KEY_ID,
        }
        layout = Layout.from_env(self.cli_env)
        # 測試機不是 root：root 判斷與管理私鑰的擁有者都用測試替身（C-950／C-710）。
        layout.euid = 0
        layout.local_admin_key_owner = (os.getuid(), os.getegid())
        self.cli_layout = layout
        # 真的兩個庫＋真的名冊：llmgwctl auth-store init／auth-registry install。
        self.cli(["auth-store", "init", "--confirm"], expect=0)
        write_registry_file(str(self.candidate), self.keys.registry(1))
        self.cli(["auth-registry", "install", str(self.candidate), "--no-wait"], expect=0)

    def _write_env(self, mode: str) -> None:
        self.env_file.write_text(
            'LGW_API_PORT="%d"\n'
            'LGWD_AUTH_MODE="%s"\n'
            'LGWD_AUTH_STATE_DIR="%s"\n'
            'LGWD_AUTH_REGISTRY="%s"\n'
            % (self.port, mode, self.state, self.registry),
            encoding="utf-8",
        )

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def mode(self) -> str:
        return self._mode

    # --- llmgwctl（行程內） --------------------------------------------------

    def cli(self, argv, *, expect=0, euid=0, patches=()):
        """在行程內跑 `lgwctl.cli.main`；版面由測試替身給（`Layout.from_env`）。

        回 (rc, stdout, stderr)；`expect` 不是 None 時順便斷言結束碼。
        `euid` 用來驗非 root 的格子（只改這一份版面物件，不動行程）。
        """
        out, err = io.StringIO(), io.StringIO()
        original = self.cli_layout.euid
        self.cli_layout.euid = euid
        try:
            with mock.patch.object(Layout, "from_env", lambda env=None: self.cli_layout):
                with contextlib.ExitStack() as stack:
                    for target, attribute, replacement in patches:
                        stack.enter_context(
                            mock.patch.object(target, attribute, replacement))
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = int(_cli_main(list(argv)))
        finally:
            self.cli_layout.euid = original
        add_artifact("llmgwctl " + " ".join(argv), out.getvalue() + err.getvalue())
        if expect is not None:
            if rc != expect:
                raise AssertionError(
                    "llmgwctl %s rc=%s（預期 %s）\n%s%s"
                    % (" ".join(argv), rc, expect, out.getvalue(), err.getvalue()))
        return rc, out.getvalue(), err.getvalue()

    # --- lgwd 子行程 ---------------------------------------------------------

    def _cmd(self, mode: str) -> list[str]:
        return [
            sys.executable, "-m", "lgwd",
            "--auth-mode", mode,
            "--auth-state-dir", str(self.state),
            "--auth-registry", str(self.registry),
            "--auth-per-key-max", str(self.per_key_max),
            "--api-port", str(self.port),
            "--db", str(self.db),
            "--keys", str(self.keys_env),
            "--policy", str(self.policy),
            "--keysync-interval", "0",
        ] + list(self.extra_args)

    def _env(self) -> dict:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "PYTHONPATH": str(self.root) + os.pathsep + str(REPO_ROOT),
            "E2E_WATCH_PREFIX": str(self.state),
            "E2E_EVENTS": str(self.events),
            "E2E_CONN_EVENTS": str(self.connections),
        }
        return env

    def start(self, mode: str) -> None:
        """起 lgwd；只有「埠被別的行程佔用」時換一個埠重試（最多 3 次）。

        `free_port()` 先 bind 再關閉，到 lgwd 真的 bind 之間有 TOCTOU 窗口；共載機器
        上撞埠時 lgwd 會以 rc=2 結束並印 `Address already in use`。這裡只針對那一種
        失敗換埠重試，逾時與其他啟動錯誤照舊直接失敗（不是通用 retry）。
        """
        self._mode = mode
        self._stopped = False
        for attempt in range(1, 4):
            self._write_env(mode)
            self.events.unlink(missing_ok=True)
            self.connections.unlink(missing_ok=True)
            self.runs += 1
            self._log_path = self.root / ("lgwd-%d.log" % self.runs)
            self._log = open(self._log_path, "wb")
            self.proc = subprocess.Popen(
                self._cmd(mode), cwd=str(REPO_ROOT), env=self._env(),
                stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            )
            try:
                self._wait_healthy()
                return
            except _PortBusy:
                if attempt == 3:
                    raise
                self._close_log()
                self.proc = None
                self.port = free_port()

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _wait_healthy(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._log.flush()
                text = self._log_path.read_text(encoding="utf-8", errors="replace")
                if self.proc.returncode == 2 and "Address already in use" in text:
                    raise _PortBusy(text)
                raise AssertionError(
                    "lgwd %s 提前結束 rc=%s：\n%s" % (
                        self._mode, self.proc.returncode, text))
            try:
                with urllib.request.urlopen(
                        self.base_url + "/healthz", timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.02)
        raise AssertionError("lgwd %s 在 %.0f 秒內沒有回 healthz 200" % (self.mode, timeout))

    def healthz(self):
        return self.request("GET", "/healthz")

    def request(self, method: str, target: str, headers=(), body: bytes = b""):
        """送一份原始請求；回 (status, body bytes, headers dict)。

        每個回應的標頭與 body 都收進 C-955 的掃描清單（body 含 env 時只記標記）。
        """
        extra = list(headers)
        if body and not any(name.lower() == "content-length" for name, _ in extra):
            extra.append(("Content-Length", str(len(body))))
        raw = req(method, target, extra, body)
        status, payload, response_headers = parse_response(exchange(self.port, raw))
        record_response("%s %s %s" % (self.name, method, target), status,
                        payload, response_headers, path=target)
        return status, payload, response_headers

    def request_route(self, route, headers=()):
        _, method, target, body = route
        if body is None:
            body = route_body(target)
        return self.request(method, target, headers, body)

    @staticmethod
    def signed_headers(signer, method: str, target: str, body: bytes = b"") -> list:
        """一次簽章的五個 `X-LGW-*` 標頭（樣本由記錄器收進 `SAMPLES`）。"""
        headers = signer.headers(method=method, target=target, body=body)
        return list(headers.items())

    def request_signed(self, route, signer):
        _, method, target, body = route
        if body is None:
            body = route_body(target)
        return self.request(method, target, self.signed_headers(signer, method, target, body), body)

    def client(self, signer=None, timeout: float = 10):
        from lgw.client import Client

        return Client(self.base_url, timeout=timeout, signer=signer)

    # --- 觀測 ---------------------------------------------------------------

    def event_lines(self) -> list[str]:
        try:
            return self.events.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def spawn_count(self) -> int:
        return sum(1 for line in self.event_lines() if line.startswith("spawn"))

    def touch_count(self) -> int:
        return sum(1 for line in self.event_lines() if line == "touch")

    def connection_count(self) -> int:
        try:
            return len(self.connections.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0

    def log_text(self) -> str:
        try:
            return self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def artifact_paths(self) -> dict:
        return {
            "audit": self.state / "auth-audit.log",
            "status": self.state / ".auth-registry.status",
        }

    def audit_log_text(self) -> str:
        path = self.artifact_paths()["audit"]
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def status_text(self) -> str:
        path = self.artifact_paths()["status"]
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def status_document(self):
        try:
            return json.loads(self.status_text())
        except ValueError:
            return None

    def audit_rows(self) -> list[dict]:
        """業務稽核（`llm.db` 的 audit 表）；唯讀開，不等 lgwd 停。"""
        if not self.db.exists():
            return []
        connection = sqlite3.connect(
            "file:%s?mode=ro" % self.db, uri=True, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "select * from audit order by seq")]
        finally:
            connection.close()

    def registry_bytes(self, generation: int) -> bytes:
        return self.keys.registry(generation)

    def install_registry_file(self, generation: int) -> None:
        """**直接**換名冊檔（不經 install；給 reload／故障注入的情境）。"""
        write_registry_file(str(self.registry), self.registry_bytes(generation))

    def wait_for(self, predicate, timeout: float = 15.0, step: float = 0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(step)
        return predicate()

    # --- 收尾 ---------------------------------------------------------------

    def stop(self, *, check_children: bool = True) -> str:
        """SIGTERM 收尾；斷言 rc=0、鎖已釋放、埠沒有 listen、沒有殘留子行程（C-950）。

        `check_children=False` 只給「收尾時測試自己的背景探針還在跑」的 W-6d 用：那時
        假 ssh 還在飛，殘留的斷言改在探針執行緒 join 之後由 `assert_no_leftover_children()`
        補做。
        """
        if self.proc is None or self._stopped:
            return self.log_text()
        process = self.proc
        if process.poll() is None:
            process.terminate()
            try:
                rc = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise AssertionError("lgwd %s SIGTERM 後沒有結束" % self.mode)
        else:
            rc = process.returncode
        if self._log is not None:
            self._log.close()
            self._log = None
        self.proc = None
        self._stopped = True
        text = self.log_text()
        add_artifact("%s lgwd-%d (%s)" % (self.name, self.runs, self.mode), text)
        for label, path in self.artifact_paths().items():
            try:
                add_artifact("%s %s" % (self.name, label), path.read_text(encoding="utf-8"))
            except OSError:
                pass
        if rc != 0:
            raise AssertionError("lgwd %s rc=%s（預期 0）：\n%s" % (self.mode, rc, text))
        self._assert_lock_released()
        self._assert_port_released()
        if check_children:
            self._assert_no_leftover_children()
        return text

    def cleanup(self) -> None:
        try:
            self.stop()
        finally:
            if self._log is not None:
                try:
                    self._log.close()
                except OSError:
                    pass
                self._log = None
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.tmp.cleanup()

    def _assert_lock_released(self) -> None:
        lock = auth_paths(str(self.state), registry=str(self.registry)).store_lock
        with open(lock, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def _assert_port_released(self) -> None:
        """斷言沒有行程在 listen 這個埠。

        不用 `SO_REUSEADDR` 重新 bind：客戶端連線（HTTP/1.0 由伺服器先關）會讓同一個
        埠留下 `TIME_WAIT`，那時 bind 會失敗但其實沒有人監聽；反之 `SO_REUSEADDR`
        又會在 `TIME_WAIT` 時成功，兩種都測不出「有沒有 listen」。直接讀
        `/proc/net/tcp{,6}` 的 LISTEN（狀態 `0A`）最直接。
        """
        if _listening_on(self.port):
            raise AssertionError("收尾後仍有行程在 listen 127.0.0.1:%d" % self.port)

    def assert_no_leftover_children(self) -> None:
        """公開版：給「收尾時自己的背景探針還在跑」的情境在 join 之後補做（C-950）。"""
        self._assert_no_leftover_children()

    def _assert_no_leftover_children(self) -> None:
        """掃 /proc：任何 cmdline 還帶著這個版面絕對路徑的行程就是殘留（C-950）。"""
        marker = str(self.root)
        leftovers = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            if self.proc is not None and entry.name == str(self.proc.pid):
                continue          # 目前這一個 lgwd 還活著時，它不是「殘留」
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker.encode() in raw:
                leftovers.append((entry.name, raw.replace(b"\x00", b" ").decode(
                    "utf-8", "replace")))
        if leftovers:
            raise AssertionError("收尾後仍有殘留子行程：%r" % (leftovers,))


def _cli_main(argv):
    """`lgwctl.cli.main` 的間接呼叫（保持匯入在呼叫當下，避免拖慢收集）。"""
    from lgwctl import cli

    return cli.main(argv)


def make_fake_ssh(directory: Path) -> str:
    path = directory / "fake-ssh"
    path.write_text(FAKE_SSH, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


def tunnel_probe(_layout_root: Path):
    """載入真的隧道腳本模組（`probe_healthy`）；假 ssh 負責傳輸。"""
    from tests.test_tunnel_follow import load_tunnel

    return load_tunnel()


__all__ = [
    "ADMIN_KEY_ID",
    "ADMIN_PERMISSIONS",
    "ARTIFACTS",
    "B_KEY_ID",
    "B_PRINCIPAL",
    "Board",
    "FAKE_CREDENTIALS",
    "HEALTHZ",
    "Keys",
    "NARROW_KEY_ID",
    "NARROW_PERMISSIONS",
    "NARROW_PRINCIPAL",
    "PRLIMIT",
    "ROUTES",
    "ROUTE_TEMPLATES",
    "SAMPLES",
    "SSH_KEYGEN",
    "add_artifact",
    "add_sample",
    "free_port",
    "install_signer_recorder",
    "make_fake_ssh",
    "normalize",
    "route_body",
    "tunnel_probe",
]
