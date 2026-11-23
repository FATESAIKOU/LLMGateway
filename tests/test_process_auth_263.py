"""C-605/C-607/C-609: real lgwd processes and lifecycle instrumentation."""
import fcntl
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths
from lgwd.main import INFLIGHT_DRAIN_SECONDS
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer, key_entry, registry_bytes
from tests.test_main import write_policy, key_text, FAKE_KEYS

HOOK = '''
import os, time, sqlite3, builtins
watch = os.environ.get('WATCH_PREFIX')
def track(path):
    if watch and str(path).startswith(watch):
        with builtins.open(os.environ['EVENTS'], 'a') as f: f.write('touch\\n')
old_open, old_stat, old_connect = os.open, os.stat, sqlite3.connect
def os_open(path, *a, **kw):
    track(path); return old_open(path, *a, **kw)
def os_stat(path, *a, **kw):
    track(path); return old_stat(path, *a, **kw)
def connect(path, *a, **kw):
    track(path); return old_connect(path, *a, **kw)
os.open, os.stat, sqlite3.connect = os_open, os_stat, connect
if os.environ.get('CONN_EVENTS'):
    import socketserver
    old_finish = socketserver.BaseServer.finish_request
    def finish_request(self, request, client_address):
        with builtins.open(os.environ['CONN_EVENTS'], 'a') as f: f.write('conn\\n')
        return old_finish(self, request, client_address)
    socketserver.BaseServer.finish_request = finish_request
import lgwd.main as main
import subprocess
original_popen = subprocess.Popen
class CountPopen(original_popen):
    def __init__(self, *args, **kwargs):
        with open(os.environ['EVENTS'], 'a') as f: f.write('spawn\\n')
        super().__init__(*args, **kwargs)
subprocess.Popen = CountPopen
original = main.open_runtime
def opened(cfg, clock):
    r = original(cfg, clock, owner_uid=(os.getuid() if cfg.sshkeygen_path.endswith('/old-ssh-keygen') else 0), registry_owner_uid=os.getuid())
    def wrap(obj, method, event):
        if obj is None: return
        old = getattr(obj, method)
        def call(*a, **k):
            with open(os.environ['EVENTS'], 'a') as f: f.write(event + '\\n')
            return old(*a, **k)
        setattr(obj, method, call)
    wrap(r.watcher, 'stop', 'watcher'); wrap(r.purge, 'stop', 'purge'); wrap(r.lock, 'release', 'lock')
    if os.environ.get('BLOCK_REQUEST'):
        old = r.gate.authenticate
        def auth(*a, **k):
            open(os.environ['ENTERED'], 'w').close()
            while not os.path.exists(os.environ['RELEASE']): time.sleep(.01)
            return old(*a, **k)
        r.gate.authenticate = auth
    return r
main.open_runtime = opened
if os.environ.get('STOP_ENTERED'):
    # 測試掛點：讓 Gateway.stop 停在入口，直到 STOP_RESUME 出現。用來把「第二次
    # SIGTERM」確定性地放進「第一次已被處理、收尾進行中」的視窗，不靠 sleep 賭時序。
    original_stop = main.Gateway.stop
    def paused_stop(self):
        open(os.environ['STOP_ENTERED'], 'w').close()
        while not os.path.exists(os.environ['STOP_RESUME']): time.sleep(.01)
        return original_stop(self)
    main.Gateway.stop = paused_stop
'''

class Processes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / 'state'; self.state.mkdir()
        self.paths = auth_paths(str(self.state), registry=str(self.root / 'registry'))
        self.signer = Signer(self.tmp.name, 'key')
        create_stores(self.paths.trust_db, self.paths.replay_db)
        self.registry(1)
        write_policy(self.root); (self.root / 'keys.env').write_text(key_text(FAKE_KEYS))
        (self.root / 'sitecustomize.py').write_text(HOOK)
        self.env = os.environ.copy(); self.env['PYTHONPATH'] = str(self.root) + os.pathsep + str(Path(__file__).resolve().parent.parent)
        self.env['EVENTS'] = str(self.root / 'events'); self.env['ENTERED'] = str(self.root / 'entered'); self.env['RELEASE'] = str(self.root / 'release')
        self.port = self.free_port(); self.procs = []
        self.addCleanup(self.stop_all)
    def registry(self, n):
        write_registry_file(self.paths.registry, registry_bytes(n, [key_entry('test-key', self.signer)]))
    @staticmethod
    def free_port():
        with socket.socket() as s: s.bind(('127.0.0.1', 0)); return s.getsockname()[1]
    def cmd(self, mode='enforce', port=None, extra=()):
        return [sys.executable, '-m', 'lgwd', '--auth-mode', mode, '--auth-state-dir', str(self.state),
                '--auth-registry', self.paths.registry, '--api-port', str(port or self.port),
                '--db', str(self.root / 'app.db'), '--keys', str(self.root / 'keys.env'),
                '--policy', str(self.root / 'policy.json'), '--keysync-interval', '0', *extra]
    def start(self, mode='enforce', port=None, extra=(), env=None):
        p = subprocess.Popen(self.cmd(mode, port, extra), env=env or self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.procs.append(p); return p
    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=15)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
            if p.stderr: p.stderr.close()
    def health(self, p, port=None):
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            if p.poll() is not None: self.fail(f'process exited {p.returncode}: {p.stderr.read()!r}')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port or self.port}/healthz', timeout=1) as r: return r.read()
            except OSError: time.sleep(.05)
        self.fail('health timeout')
    def refused(self, p, msg):
        _, err = p.communicate(timeout=15); self.assertEqual(p.returncode, 78, err); self.assertIn(msg, err)
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', self.port))
    def conn_count(self, env):
        path = Path(env['CONN_EVENTS'])
        return len(path.read_text().splitlines()) if path.exists() else 0
    def wait_connections(self, env, before, count=2):
        """等 server 為新連線各起一條處理執行緒（HOOK 在 finish_request 記一筆）。"""
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            if self.conn_count(env) >= before + count: return
            time.sleep(.02)
        self.fail('server did not pick up the pending connections')
    def stop_pause_env(self, tag, env):
        """回一份會讓 Gateway.stop 停在入口（HOOK 掛點）的環境。"""
        env = env.copy()
        env['STOP_ENTERED'] = str(self.root / f'stop-entered-{tag}')
        env['STOP_RESUME'] = str(self.root / f'stop-resume-{tag}')
        return env
    def wait_stopping(self, p, env):
        """等第一次 SIGTERM 已被處理、Gateway.stop 已進入掛點；行程這時不可能已退出。"""
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            if p.poll() is not None:
                self.fail(f'process exited {p.returncode} before Gateway.stop: '
                          f'{(p.stderr.read() if p.stderr else b"")!r}')
            if Path(env['STOP_ENTERED']).exists(): return
            time.sleep(.01)
        self.fail('Gateway.stop did not reach the test hook')
    def test_C605_replay_missing_and_identity(self):
        os.unlink(self.paths.replay_db)
        self.refused(self.start(), b'recover-store'); self.assertFalse(os.path.exists(self.paths.replay_db))
        os.unlink(self.paths.trust_db); create_stores(self.paths.trust_db, self.paths.replay_db)
        with sqlite3.connect(self.paths.replay_db) as db: db.execute("update meta set value=? where key='identity'", ('0'*32,))
        self.refused(self.start(), b'identit')
    def test_C605_flock_both_modes_and_release(self):
        p = self.start(); self.health(p)
        for mode in ('warn', 'enforce'):
            q = self.start(mode, self.free_port()); _, err = q.communicate(timeout=15)
            self.assertEqual(q.returncode, 78, err); self.assertIn(b'lock', err); self.health(p)
        p.terminate(); self.assertEqual(p.wait(timeout=15), 0)
        port = self.free_port(); q = self.start(port=port); self.health(q, port)
    def test_C605_unavailable_lock_modes(self):
        os.mkdir(self.paths.store_lock)
        self.refused(self.start(), b'permissions')
        p = self.start('warn'); self.health(p); p.terminate(); self.assertEqual(p.wait(timeout=15), 0)
    def test_C605_verifier_missing_and_old(self):
        old = self.root / 'old-ssh-keygen'
        shutil.copy('/usr/bin/ssh-keygen', old); old.chmod(0o755)
        ssh = self.root / 'ssh'; ssh.write_text('#!/usr/bin/python3\nimport sys\nprint("OpenSSH_9.5p1", file=sys.stderr)\n'); ssh.chmod(0o755)
        for path in (self.root / 'missing', old):
            for mode in ('enforce', 'warn', 'off'):
                with self.subTest(path=path, mode=mode):
                    p = self.start(mode, extra=('--auth-sshkeygen-path', str(path)))
                    if mode == 'enforce': self.refused(p, b'ssh-keygen')
                    else:
                        self.health(p); p.terminate(); self.assertEqual(p.wait(timeout=15), 0)
    def test_C605_invalid_registry_and_snapshot(self):
        Path(self.paths.registry).write_text('invalid'); self.refused(self.start(), b'registry')
    def test_C609_health_only_poll_reloads(self):
        p = self.start(); baseline = self.health(p); self.registry(2)
        deadline = time.monotonic()+12
        while time.monotonic()<deadline:
            self.assertEqual(self.health(p), baseline)
            try:
                if json.loads(Path(self.paths.status).read_text())['loaded_generation'] == 2: break
            except (OSError, ValueError): pass
            time.sleep(.1)
        else: self.fail('registry not reloaded')
    def test_C607_C609_three_modes_health_and_off_zero_spawn(self):
        responses = []
        for mode in ('off','warn','enforce'):
            events = Path(self.env['EVENTS']); events.unlink(missing_ok=True)
            p = self.start(mode); self.health(p)
            samples = []
            signed = self.signer.headers(key_id='test-key', method='GET', target='/healthz')
            variants = [[], signed, [('X-LGW-Key-Id', 'unknown')],
                        [('X-LGW-Key-Id', 'x'), ('X-LGW-Key-Id', 'y')],
                        [('Transfer-Encoding', 'chunked')]]
            for headers in variants:
                wire = ('GET /healthz HTTP/1.0\r\nHost: localhost\r\n' +
                        ''.join(f'{k}: {v}\r\n' for k,v in headers) + '\r\n').encode()
                with socket.create_connection(('127.0.0.1', self.port), timeout=3) as sock:
                    sock.sendall(wire); data = b''
                    while True:
                        part = sock.recv(4096)
                        if not part: break
                        data += part
                header, body = data.split(b'\r\n\r\n', 1)
                stable = [line for line in header.split(b'\r\n')
                          if not line.lower().startswith((b'date:', b'server:'))]
                samples.append((b'\r\n'.join(stable), body))
            with socket.create_connection(('127.0.0.1', self.port), timeout=3) as sock:
                sock.sendall(b'GET /v1/subscriptions?hidden-query-value HTTP/1.0\r\nHost: localhost\r\nX-LGW-Nonce: hidden-nonce-value\r\n\r\n')
                while sock.recv(4096): pass
            p.terminate(); p.wait(timeout=15)
            log = p.stderr.read()
            for secret in (b'hidden-query-value', b'hidden-nonce-value', self.signer.public_key_line.split()[1].encode()):
                self.assertNotIn(secret, log)
            if mode == 'off':
                self.assertNotIn('spawn', events.read_text() if events.exists() else '')
                self.assertIn(b'auth mode=off', log)
            responses.append(samples)
        self.assertEqual(responses[0], responses[1]); self.assertEqual(responses[1], responses[2])
        self.assertTrue(all(sample == responses[0][0] for sample in responses[0]))
    def test_C605_off_does_not_access_auth_paths_or_spawn(self):
        absent = self.root / 'absent'
        env = self.env.copy(); env['WATCH_PREFIX'] = str(absent)
        p = self.start('off', extra=('--auth-state-dir', str(absent), '--auth-registry', str(absent / 'registry'),
                                     '--auth-sshkeygen-path', str(absent / 'ssh-keygen')), env=env)
        self.health(p); p.terminate(); self.assertEqual(p.wait(timeout=15), 0)
        self.assertFalse(absent.exists())
        events = Path(self.env['EVENTS'])
        recorded = events.read_text() if events.exists() else ''
        self.assertNotIn('spawn', recorded); self.assertNotIn('touch', recorded)

    def test_C605_sigterm_waits_for_inflight_then_stops_in_order(self):
        env = self.env.copy(); env['BLOCK_REQUEST'] = '1'; p = self.start(env=env); self.health(p)
        s = socket.create_connection(('127.0.0.1', self.port)); self.addCleanup(s.close)
        s.sendall(b'GET /v1/subscriptions HTTP/1.0\r\nHost: localhost\r\n\r\n')
        deadline = time.monotonic()+5
        while not Path(env['ENTERED']).exists() and time.monotonic()<deadline: time.sleep(.02)
        self.assertTrue(Path(env['ENTERED']).exists()); p.terminate(); time.sleep(.4)
        with open(self.paths.store_lock, 'r+') as f:
            with self.assertRaises(BlockingIOError): fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Path(env['RELEASE']).touch(); self.assertEqual(p.wait(timeout=15), 0)
        s.settimeout(3); response = s.recv(4096)
        self.assertTrue(response.startswith((b'HTTP/1.0 401 ', b'HTTP/1.0 503 ')), response)
        events = Path(env['EVENTS']).read_text().splitlines()
        self.assertLess(events.index('watcher'), events.index('purge')); self.assertLess(events.index('purge'), events.index('lock'))
        port = self.free_port(); q = self.start(port=port); self.health(q, port)

    def pending_connections(self, port, env):
        """開一條閒置連線與一條只送一半標頭的連線；等 server 各起一條執行緒。"""
        before = self.conn_count(env)
        idle = socket.create_connection(('127.0.0.1', port), timeout=5); self.addCleanup(idle.close)
        half = socket.create_connection(('127.0.0.1', port), timeout=5); self.addCleanup(half.close)
        half.sendall(b'GET /healthz HTTP/1.0\r\nHost: local')
        self.wait_connections(env, before, 2)
        return idle, half

    def test_C605_sigterm_does_not_wait_for_idle_or_slow_connections(self):
        """(a) 閒置連線與半截標頭不讓 SIGTERM 卡住；收尾中的第二次 SIGTERM 不會更糟。

        高-1 的迴歸守門：`daemon_threads` 若改回 False，`server_close()` 會 join
        這兩條連線的執行緒，行程在 N＋5 秒內結束不了。
        第二次 SIGTERM 用 HOOK 掛點固定在 `Gateway.stop` 入口（STOP_RESUME 才放行），
        不靠 `sleep` 賭時序：第一次訊號一定已被處理、收尾已在進行，行程也還不可能
        退出，所以第二次訊號必定由仍在安裝的 handler 收到。
        """
        for mode in ('off', 'enforce'):
            with self.subTest(mode=mode):
                port = self.free_port(); env = self.stop_pause_env(mode, self.env)
                env['CONN_EVENTS'] = str(self.root / f'conn-{mode}')
                p = self.start(mode, port=port, env=env); self.health(p, port)
                self.pending_connections(port, env)
                started = time.monotonic()
                p.terminate()
                self.wait_stopping(p, env)
                p.terminate()  # 收尾進行中：handler 仍安裝，只會再 set 一次事件
                Path(env['STOP_RESUME']).touch()
                self.assertEqual(p.wait(timeout=INFLIGHT_DRAIN_SECONDS + 5), 0,
                                 p.stderr.read() if p.stderr else '')
                self.assertLess(time.monotonic() - started, INFLIGHT_DRAIN_SECONDS)

    def test_C605_second_sigterm_during_shutdown_does_not_skip_cleanup(self):
        """收尾期間的第二次 SIGTERM：不加速、不打斷，rc=0 且鎖照常釋放。

        用 BLOCK_REQUEST 讓一個請求卡在 gate（在途數＝1）；掛點先停在 stop 入口，
        送第二次 SIGTERM 後放行，確認行程還活著、鎖仍握著，等在途請求完成後才關庫
        放鎖。handler 若在收尾期間被還原成預設處置，第二次訊號會直接殺掉行程
        （rc=-15），鎖由 OS 釋放而不是照 watcher→purge→lock 的順序。
        """
        env = self.stop_pause_env('second', self.env); env['BLOCK_REQUEST'] = '1'
        p = self.start(env=env); self.health(p)
        s = socket.create_connection(('127.0.0.1', self.port)); self.addCleanup(s.close)
        s.sendall(b'GET /v1/subscriptions HTTP/1.0\r\nHost: localhost\r\n\r\n')
        deadline = time.monotonic()+5
        while not Path(env['ENTERED']).exists() and time.monotonic()<deadline: time.sleep(.02)
        self.assertTrue(Path(env['ENTERED']).exists())
        p.terminate()
        self.wait_stopping(p, env)
        p.terminate()  # 第二次 SIGTERM：確定落在收尾進行中
        Path(env['STOP_RESUME']).touch()
        # 在途請求還卡著，stop 尚未走到放鎖；這裡鎖一定還握著。
        with open(self.paths.store_lock, 'r+') as f:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertIsNone(p.poll(), 'second SIGTERM killed the process during shutdown')
        Path(env['RELEASE']).touch()
        self.assertEqual(p.wait(timeout=INFLIGHT_DRAIN_SECONDS + 5), 0,
                         p.stderr.read() if p.stderr else '')
        s.settimeout(3); response = s.recv(4096)
        self.assertTrue(response.startswith((b'HTTP/1.0 401 ', b'HTTP/1.0 503 ')), response)
        events = Path(env['EVENTS']).read_text().splitlines()
        self.assertLess(events.index('watcher'), events.index('purge'))
        self.assertLess(events.index('purge'), events.index('lock'))
        with open(self.paths.store_lock, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)

    def test_C605_off_sigterm_same_as_d1_for_idle_connections(self):
        """(d) off 的 SIGTERM 與 D1 相同：不等閒置連線（D1 是 daemon_threads=True）。

        差分判準：等待在途請求的上限是 N 秒，但沒有任何請求在途，只有閒置／慢連線，
        所以停止必須遠早於 N 秒完成（若把連線當成在途，這裡會等滿 N 秒）。
        """
        port = self.free_port(); env = self.env.copy()
        env['CONN_EVENTS'] = str(self.root / 'conn-off-d1')
        p = self.start('off', port=port, env=env); self.health(p, port)
        self.pending_connections(port, env)
        started = time.monotonic(); p.terminate()
        self.assertEqual(p.wait(timeout=INFLIGHT_DRAIN_SECONDS + 5), 0)
        self.assertLess(time.monotonic() - started, INFLIGHT_DRAIN_SECONDS / 2)

    def test_C605_sigterm_gives_up_at_cap_and_releases_lock(self):
        """(c) 在途請求卡超過 N 秒：行程仍在 N＋5 秒內結束（rc=0），鎖可立刻取得。"""
        env = self.env.copy(); env['BLOCK_REQUEST'] = '1'
        p = self.start(env=env); self.health(p)
        s = socket.create_connection(('127.0.0.1', self.port)); self.addCleanup(s.close)
        s.sendall(b'GET /v1/subscriptions HTTP/1.0\r\nHost: localhost\r\n\r\n')
        deadline = time.monotonic()+5
        while not Path(env['ENTERED']).exists() and time.monotonic()<deadline: time.sleep(.02)
        self.assertTrue(Path(env['ENTERED']).exists())
        started = time.monotonic(); p.terminate()
        self.assertEqual(p.wait(timeout=INFLIGHT_DRAIN_SECONDS + 5), 0)
        self.assertGreaterEqual(time.monotonic() - started, INFLIGHT_DRAIN_SECONDS - 1)
        with open(self.paths.store_lock, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        port = self.free_port(); q = self.start(port=port); self.health(q, port)

    def test_C605_runtime_stopped_when_gateway_fails_after_open(self):
        """open_runtime 成功、Gateway() 因埠被佔用失敗時，仍要 runtime.stop() 放鎖。

        單元層級（同一個行程）：若漏掉 `runtime.stop()`，認證鎖的 fd 還開著，同一個
        行程再用另一個 fd flock 會得到 BlockingIOError；行程層級測不到這條路徑，
        因為行程一退出作業系統就會替它放鎖。
        """
        import contextlib, io
        from unittest import mock
        import lgwd.main as lgwd_main
        argv = ['--auth-mode', 'enforce', '--auth-state-dir', str(self.state),
                '--auth-registry', self.paths.registry, '--api-port', str(self.port),
                '--db', str(self.root / 'app.db'), '--keys', str(self.root / 'keys.env'),
                '--policy', str(self.root / 'policy.json'), '--keysync-interval', '0']
        original_open = lgwd_main.open_runtime
        def opened(cfg, clock):
            return original_open(cfg, clock, registry_owner_uid=os.getuid())
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', self.port)); occupied.listen()
            with mock.patch.object(lgwd_main, 'open_runtime', opened), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                rc = lgwd_main.main(argv)
        self.assertEqual(rc, 2, err.getvalue())
        self.assertIn('cannot start', err.getvalue())
        with open(self.paths.store_lock, 'r+') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
