"""C-612: opt-in ten-minute process-level sustained throughput test."""
import collections
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

from lgwd.auth_db import create_stores
from lgwd.auth_paths import auth_paths
from tests.auth_fakes import write_registry_file
from tests.auth_signer import Signer, key_entry, registry_bytes
from tests.test_main import write_policy, key_text, FAKE_KEYS

# Test-only instrumentation enables the existing lock-wait collector and exports it
# at shutdown; the actual process remains python -m lgwd and uses real verifier.
HOOK = '''\nimport dataclasses, json, os\nimport lgwd.main as m\n_original = m.open_runtime\ndef opened(cfg, clock):\n    r = _original(dataclasses.replace(cfg, collect_lock_waits=True), clock, registry_owner_uid=os.getuid())\n    old_stop = r.stop\n    def stop():\n        waits = list(r.replay.db.lock_waits or ()) if r.replay else []\n        with open(os.environ["LGW_TEST_WAITS"], "w") as f: json.dump(waits, f)\n        old_stop()\n    r.stop = stop\n    return r\nm.open_runtime = opened\n'''

@unittest.skipUnless(os.environ.get('LGW_SLOW_TESTS') == '1', 'C-612 requires LGW_SLOW_TESTS=1')
class ProcessRate(unittest.TestCase):
    def test_C612_rate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); state = root / 'state'; state.mkdir()
            signer = Signer(d, 'rate'); paths = auth_paths(str(state), registry=str(root / 'registry'))
            create_stores(paths.trust_db, paths.replay_db)
            write_registry_file(paths.registry, registry_bytes(1, [key_entry('rate-key', signer)]))
            write_policy(root); (root / 'keys.env').write_text(key_text(FAKE_KEYS))
            (root / 'sitecustomize.py').write_text(HOOK)
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
            env = os.environ.copy(); env['PYTHONPATH'] = d + os.pathsep + str(Path(__file__).resolve().parent.parent)
            env['LGW_TEST_WAITS'] = str(root / 'waits.json')
            cmd = [sys.executable, '-m', 'lgwd', '--auth-mode', 'enforce', '--auth-state-dir', str(state),
                   '--auth-registry', paths.registry, '--auth-per-key-max', '1000000', '--api-port', str(port),
                   '--db', str(root / 'app.db'), '--keys', str(root / 'keys.env'),
                   '--policy', str(root / 'policy.json'), '--keysync-interval', '0']
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            stop = threading.Event(); q = queue.Queue(maxsize=256); statuses = collections.Counter(); errors = []
            lock = threading.Lock(); target = '/v1/subscriptions'
            def producer():
                while not stop.is_set():
                    try:
                        headers = signer.headers(key_id='rate-key', method='GET', target=target)
                        raw = ('GET ' + target + ' HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in headers) + '\r\n').encode('ascii')
                        while not stop.is_set():
                            try: q.put(raw, timeout=.1); break
                            except queue.Full: pass
                    except Exception as e:
                        with lock: errors.append('sign:' + repr(e))
                        return
            def consumer():
                while not stop.is_set() or not q.empty():
                    try: raw = q.get(timeout=.1)
                    except queue.Empty: continue
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=10) as s:
                            s.sendall(raw); data = b''
                            while True:
                                part = s.recv(65536)
                                if not part: break
                                data += part
                        code = int(data.split(b' ', 2)[1])
                        with lock: statuses[code] += 1
                    except Exception as e:
                        with lock: errors.append('request:' + repr(e))
                    finally: q.task_done()
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=1): break
                    except OSError:
                        if proc.poll() is not None or time.monotonic() > deadline: self.fail('process did not start')
                        time.sleep(.1)
                load_start = os.getloadavg(); cores = os.cpu_count()
                threads = [threading.Thread(target=producer) for _ in range(4)] + [threading.Thread(target=consumer) for _ in range(32)]
                start = time.monotonic()
                for t in threads: t.start()
                stop.wait(600)
                stop.set()
                for t in threads: t.join(60)
                elapsed = time.monotonic() - start
                load_end = os.getloadavg()
                self.assertFalse(any(t.is_alive() for t in threads))
            finally:
                stop.set(); proc.terminate(); proc.wait(timeout=60)
            waits = sorted(json.loads((root / 'waits.json').read_text()))
            def quantile(p): return waits[min(len(waits)-1, int(len(waits)*p))] if waits else None
            # 只有 200 算產能：503（gate 轉 UNAVAILABLE）與其他非 200 一律視為失敗，
            # 不進 rate 的分子，也不放過。
            rate = statuses[200] / elapsed
            version = subprocess.run(['/usr/bin/ssh', '-V'], capture_output=True, text=True).stderr.strip()
            context = (f'elapsed={elapsed:.2f} rate={rate:.2f}/s statuses={dict(statuses)} '
                       f'errors={errors[:5]} wait_count={len(waits)} p50={quantile(.5)} '
                       f'p99={quantile(.99)} max={quantile(1)} load_start={load_start} '
                       f'load_end={load_end} cores={cores} ssh={version}')
            print(f'C-612 {context}', flush=True)
            self.assertFalse(errors, f'request/signing errors: {context}')
            self.assertTrue(waits, f'no lock-wait samples: {context}')
            non_200 = {code: n for code, n in statuses.items() if code != 200}
            self.assertFalse(non_200, 'non-200 responses (503 counts as a failure, not throughput; '
                             f'check whether the host was overloaded): {context}')
            self.assertGreaterEqual(rate, 50, 'sustained authenticated throughput below 50/s '
                                   f'(503/timeouts count as failures; check load1 vs cores): {context}')
            self.assertLess(quantile(.99), 1, 'lock-wait p99 >= 1s '
                            f'(check load1 vs cores before blaming the design): {context}')
