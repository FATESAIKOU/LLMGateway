"""#226：llmgateway-tunnel 測試共用的假外部：mock GitHub Variables API（loopback http）。

GitHub 回的是 Variables API 的形狀 ``{"name": "NODE_GATEWAY", "value": "<JSON 字串>"}``；
每個請求都記下來（路徑、Authorization、次數），可程式化回 200／401／403／429／壞 JSON
／過大／慢回應／轉址。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VARIABLE_PATH = "/repos/FATESAIKOU/MyLinuxPool/actions/variables/NODE_GATEWAY"


class MockGitHub:
    def __init__(self):
        self.requests = []          # [(path, authorization)]
        self.lock = threading.Lock()
        self.status = 200
        self.headers = {}
        self.value = None           # 內層 NODE_GATEWAY（dict 或字串）
        self.raw_body = None        # 若設定，原樣回這段 bytes
        self.delay = 0.0
        self.redirect_to = None
        self.server = None
        self.thread = None

    # -- 設定 ---------------------------------------------------------------
    def set_gateway(self, **fields):
        self.value = dict(fields)
        self.raw_body = None
        self.status = 200
        self.headers = {}

    def reset_behavior(self):
        self.status = 200
        self.headers = {}
        self.raw_body = None
        self.delay = 0.0
        self.redirect_to = None

    @property
    def request_count(self):
        with self.lock:
            return len(self.requests)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    # -- 生命週期 -------------------------------------------------------------
    def start(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with mock.lock:
                    mock.requests.append((self.path, self.headers.get("Authorization")))
                if mock.delay:
                    time.sleep(mock.delay)
                if mock.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", mock.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mock.raw_body is not None:
                    body = mock.raw_body
                else:
                    value = mock.value
                    if not isinstance(value, str):
                        value = json.dumps(value)
                    body = json.dumps({"name": "NODE_GATEWAY", "value": value}).encode()
                self.send_response(mock.status)
                for key, val in mock.headers.items():
                    self.send_header(key, val)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# ---------------------------------------------------------------------------
# 真實 loopback sshd、可模擬黑洞的 TCP relay、mock lgwd、守護程式 harness
# ---------------------------------------------------------------------------
import os
import shutil
import socket
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TUNNEL = os.path.join(REPO_ROOT, "ops-scripts", "llmgateway-tunnel")
SSHD = "/usr/sbin/sshd"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def have_real_ssh():
    return all(os.path.exists(p) for p in (SSHD, "/usr/bin/ssh", "/usr/bin/ssh-keygen")) \
        and os.path.isdir("/run/sshd")


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", path], check=True)
    with open(path + ".pub") as f:
        parts = f.read().split()
    return " ".join(parts[:2])


class MockLgwd:
    """假的 lgwd：GET /v1/subscriptions 回 200。"""

    def __init__(self):
        self.server = None

    def start(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class Sshd:
    """非 root 的真 sshd（loopback、ephemeral 埠、自己的主機金鑰）。"""

    def __init__(self, root, name, authorized_pub):
        self.dir = os.path.join(root, name)
        os.makedirs(self.dir)
        self.host_key = keygen(os.path.join(self.dir, "hostkey"))
        self.port = free_port()
        auth = os.path.join(self.dir, "authorized_keys")
        shutil.copy(authorized_pub, auth)
        config = os.path.join(self.dir, "sshd_config")
        with open(config, "w") as f:
            f.write("Port %d\nListenAddress 127.0.0.1\nHostKey %s\nPidFile %s\n"
                    "AuthorizedKeysFile %s\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                    "UsePAM no\nStrictModes no\nAllowTcpForwarding yes\nGatewayPorts no\n"
                    "LogLevel ERROR\n" % (self.port, os.path.join(self.dir, "hostkey"),
                                           os.path.join(self.dir, "pid"), auth))
        self.config = config
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen([SSHD, "-D", "-e", "-f", self.config],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_until(lambda: port_open(self.port), 10):
            raise RuntimeError("sshd did not start")
        return self

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Relay:
    """TCP relay 夾在 ssh 與 sshd 之間，可切成「黑洞」：

    黑洞時 relay 把往 sshd 的那一側全關掉（sshd 因 EOF 結束 session、釋放 -R 的遠端埠），
    但對 ssh 這一側既不回應也不關閉——ssh 看起來仍連著，要靠 ServerAlive 或探針逾時才發現。
    這等同 SIGSTOP 舊會合點的黑洞效果；用 relay 是因為兩個 sshd 共用同一個 loopback，
    被 SIGSTOP 的舊 sshd 仍占著 -R 的遠端埠，新會合點會因 ExitOnForwardFailure 建不起來。
    """

    def __init__(self, target_port):
        self.target_port = target_port
        self.blackholed = False
        self.lock = threading.Lock()
        self.upstreams = []
        self.held = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            with self.lock:
                if self.blackholed:
                    self.held.append(client)
                    continue
            try:
                upstream = socket.create_connection(("127.0.0.1", self.target_port), timeout=5)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.upstreams.append((client, upstream))
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    def _pump(self, src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                with self.lock:
                    if self.blackholed:
                        return
                dst.sendall(data)
        except OSError:
            pass
        with self.lock:
            if self.blackholed:
                return
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def blackhole(self):
        with self.lock:
            self.blackholed = True
            pairs = list(self.upstreams)
        for client, upstream in pairs:
            self.held.append(client)           # 對 ssh 那一側保持開著、不讀不寫
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upstream.close()

    def stop(self):
        self.closed = True
        try:
            self.listener.close()
        except OSError:
            pass
        for sock in list(self.held):
            try:
                sock.close()
            except OSError:
                pass
        with self.lock:
            pairs = list(self.upstreams)
        for client, upstream in pairs:
            for sock in (client, upstream):
                try:
                    sock.close()
                except OSError:
                    pass


FAKE_SSH = """#!/usr/bin/env bash
# 假 ssh：把每個 argv 以 [..] 記下；行為由 $FAKE_DIR 內的控制檔決定
printf 'argv:' >> "${FAKE_DIR:?}/ssh.log"
for a in "$@"; do printf ' [%s]' "$a" >> "$FAKE_DIR/ssh.log"; done
printf '\\n' >> "$FAKE_DIR/ssh.log"
mode=""
for arg in "$@"; do
    case "$arg" in
        -N) mode=tunnel ;;
        -W) mode=probe ;;
    esac
done
if [[ "$mode" == "probe" ]]; then
    cat > /dev/null
    code="$(cat "$FAKE_DIR/probe" 2>/dev/null || echo 200)"
    case "$code" in
        200) printf 'HTTP/1.0 200 OK\\r\\n\\r\\n' ;;
        none) ;;
        *) printf 'HTTP/1.0 %s X\\r\\n\\r\\n' "$code" ;;
    esac
    exit 0
fi
if [[ -e "$FAKE_DIR/tunnel_exit" ]]; then
    sleep 0.05
    exit 255
fi
trap 'echo tunnel_terminated >> "$FAKE_DIR/ssh.log"; exit 143' TERM
while :; do
    if [[ -e "$FAKE_DIR/tunnel_die" ]]; then rm -f "$FAKE_DIR/tunnel_die"; exit 255; fi
    sleep 0.05
done
"""


class Harness:
    """一個守護程式的沙箱：tunnel.env、STATE_DIR、stderr，以及（選用）假 ssh 的控制目錄。"""

    def __init__(self, root, fake_ssh=False):
        self.root = root
        self.home = os.path.join(root, "home")
        os.makedirs(self.home, exist_ok=True)
        self.state = os.path.join(root, "state")
        os.makedirs(self.state, exist_ok=True)
        self.env_path = os.path.join(root, "tunnel.env")
        self.fake_dir = os.path.join(root, "fake")
        os.makedirs(self.fake_dir, exist_ok=True)
        self.fake_ssh = None
        if fake_ssh:
            self.fake_ssh = os.path.join(root, "fake-ssh")
            with open(self.fake_ssh, "w") as f:
                f.write(FAKE_SSH)
            os.chmod(self.fake_ssh, 0o755)
        self.proc = None
        self.stderr_path = os.path.join(root, "stderr.log")
        self.stderr_fh = None

    # -- 設定 -----------------------------------------------------------------
    def write_env(self, values):
        with open(self.env_path, "w") as f:
            f.write("".join("%s=%s\n" % kv for kv in values.items() if kv[1] is not None))

    def env(self, extra=None):
        full = {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LGW_TUNNEL_ENV": self.env_path, "LGW_STATE_DIR": self.state,
                "FAKE_DIR": self.fake_dir}
        if extra:
            full.update(extra)
        return {k: v for k, v in full.items() if v is not None}

    # -- 行程 -----------------------------------------------------------------
    def start(self, extra_env=None, args=()):
        self.stop()
        self.stderr_fh = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen([sys.executable, TUNNEL, *args], stdout=subprocess.DEVNULL,
                                     stderr=self.stderr_fh, env=self.env(extra_env), cwd=REPO_ROOT)
        return self.proc

    def wait(self, timeout=30):
        return self.proc.wait(timeout=timeout)

    def stop(self):
        rc = None
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(5)
            rc = self.proc.returncode
            self.proc = None
        if self.stderr_fh is not None:
            self.stderr_fh.close()
            self.stderr_fh = None
        return rc

    def stderr(self):
        try:
            with open(self.stderr_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    # -- 檔案 -----------------------------------------------------------------
    def path(self, name):
        return os.path.join(self.state, name)

    def exists(self, name):
        return os.path.lexists(self.path(name))

    def read_text(self, name):
        with open(self.path(name), encoding="utf-8") as f:
            return f.read()

    def read_json(self, name):
        return json.loads(self.read_text(name))

    def write_state(self, name, data):
        with open(self.path(name), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def ssh_log(self):
        try:
            with open(os.path.join(self.fake_dir, "ssh.log"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def set_fake(self, name, value=None):
        path = os.path.join(self.fake_dir, name)
        if value is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            with open(path, "w") as f:
                f.write(value)

    def listing(self):
        out = {}
        for name in sorted(os.listdir(self.state)):
            path = os.path.join(self.state, name)
            try:
                out[name] = (os.stat(path).st_mtime_ns, open(path, "rb").read())
            except OSError:                       # 目錄／不可讀的壞水位測試
                out[name] = ("unreadable",)
        return out
