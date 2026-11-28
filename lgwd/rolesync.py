"""Independent background role table synchronizer."""
import logging
import json
import tempfile
import time
import os
import subprocess
import threading

_LOG = logging.getLogger(__name__)
DEFAULT_INTERVAL = 600.0
DEFAULT_TIMEOUT = 120.0

def default_script(env=None):
    e = os.environ if env is None else env
    return (e.get('LLMGW_PREFIX', '').rstrip('/') + '/opt/llmgateway/current/ops-scripts/role-sync')

class RoleSyncTimer:
    def __init__(self, script, interval=DEFAULT_INTERVAL, *, timeout=DEFAULT_TIMEOUT, env=None):
        self.script, self.interval, self.timeout = script, interval, timeout
        self.env = env
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc = None
        self._thread = None

    def _failure(self, reason):
        env = os.environ if self.env is None else self.env
        directory = env.get('LLMGW_PREFIX', '').rstrip('/') + '/var/lib/llmgateway'
        path = os.path.join(directory, '.rolesync.status')
        try:
            try:
                with open(path, encoding='utf-8') as f:
                    ever = json.load(f).get('ever_ok') is True
            except (OSError, ValueError, AttributeError):
                ever = False
            fd, tmp = tempfile.mkstemp(prefix='.rolesync-', dir=directory)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump({'ts': time.time(), 'ok': False, 'reason': reason, 'ever_ok': ever}, f)
                    f.flush(); os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        except OSError:
            _LOG.warning('rolesync status write failed')

    def run_once(self):
        try:
            with self._lock:
                if self._stop.is_set(): return False
                self._proc = subprocess.Popen([self.script], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = self._proc
            try:
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if not self._stop.is_set(): self._failure("timeout")
                return False
            return proc.returncode == 0
        except OSError:
            _LOG.warning('rolesync spawn failed')
            if not self._stop.is_set(): self._failure('spawn_failed')
            return False
        finally:
            with self._lock: self._proc = None

    def _loop(self):
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self.interval): break

    def start(self):
        if self.interval <= 0 or (self._thread and self._thread.is_alive()): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='rolesync')
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock: proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()
        if self._thread: self._thread.join(timeout=5)
