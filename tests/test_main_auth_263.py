"""Process integration for C-605 and C-606."""
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import time
import urllib.request
from tests.test_main import write_policy, key_text, FAKE_KEYS
from pathlib import Path
from lgwd.main import load_config


class MainAuthTests(unittest.TestCase):
    def test_C606_precedence_and_invalid(self):
        c = load_config(['--auth-mode', 'warn', '--auth-verify-concurrency', '3'],
                        {'LGWD_AUTH_MODE': 'enforce', 'LGWD_AUTH_VERIFY_CONCURRENCY': '9'})
        self.assertEqual((c.auth.mode, c.auth.verify_concurrency), ('warn', 3))
        for flag, value in [('mode', 'unknown'), ('verify-concurrency', '33'),
                            ('verify-timeout', 'nan'), ('per-key-max', '0'),
                            ('state-dir', 'relative')]:
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                load_config(['--auth-' + flag, value], {})

    def test_C606_seven_environment_values_and_cli_overrides(self):
        env = {'LGWD_AUTH_MODE':'warn', 'LGWD_AUTH_REGISTRY':'/env/registry',
               'LGWD_AUTH_STATE_DIR':'/env/state', 'LGWD_AUTH_SSHKEYGEN_PATH':'/env/keygen',
               'LGWD_AUTH_PER_KEY_MAX':'7', 'LGWD_AUTH_VERIFY_CONCURRENCY':'8',
               'LGWD_AUTH_VERIFY_TIMEOUT':'3.5'}
        config = load_config([], env).auth
        self.assertEqual((config.mode, config.registry_path, config.state_dir, config.sshkeygen_path,
                          config.per_key_max, config.verify_concurrency, config.verify_timeout),
                         ('warn', '/env/registry', '/env/state', '/env/keygen', 7, 8, 3.5))
        config = load_config(['--auth-mode', 'off', '--auth-registry', '/cli/registry',
                              '--auth-state-dir', '/cli/state', '--auth-sshkeygen-path', '/cli/keygen',
                              '--auth-per-key-max', '9', '--auth-verify-concurrency', '10',
                              '--auth-verify-timeout', '4.5'], env).auth
        self.assertEqual((config.mode, config.registry_path, config.state_dir, config.sshkeygen_path,
                          config.per_key_max, config.verify_concurrency, config.verify_timeout),
                         ('off', '/cli/registry', '/cli/state', '/cli/keygen', 9, 10, 4.5))
        for flag in ('mode', 'registry', 'state-dir', 'sshkeygen-path', 'per-key-max',
                     'verify-concurrency', 'verify-timeout'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                load_config(['--auth-' + flag, ''], env)

    def test_C605_missing_store_never_binds_or_creates(self):
        with tempfile.TemporaryDirectory() as d:
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                port = s.getsockname()[1]
            result = subprocess.run([sys.executable, '-m', 'lgwd', '--auth-mode', 'enforce',
                '--auth-state-dir', d, '--api-port', str(port)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn(b'trust', result.stderr)
            self.assertIn(b'rebuild-trust', result.stderr)
            self.assertFalse(Path(d, 'auth-trust.sqlite').exists())
            with socket.socket() as s:
                s.bind(('127.0.0.1', port))

    def test_C605_auth_refusal_precedes_even_attempted_bind(self):
        with tempfile.TemporaryDirectory() as d, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            p = subprocess.run([sys.executable, '-m', 'lgwd', '--auth-mode', 'enforce',
                                '--auth-state-dir', d, '--api-port', str(port)],
                               capture_output=True, timeout=15)
            self.assertEqual(p.returncode, 78, p.stderr)
            self.assertIn(b'rebuild-trust', p.stderr)

    def test_C606_unknown_mode_exit_2(self):
        p = subprocess.run([sys.executable, '-m', 'lgwd', '--auth-mode', 'unknown'],
                           capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 2)
        self.assertIn(b"auth mode must", p.stderr)

    def test_C609_off_and_warn_health_and_sigterm(self):
        for mode in ("off", "warn"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                write_policy(root)
                (root / "keys.env").write_text(key_text(FAKE_KEYS))
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                missing = root / "missing"
                cmd = [sys.executable, "-m", "lgwd", "--auth-mode", mode,
                       "--auth-state-dir", str(missing), "--auth-registry", str(missing / "registry"),
                       "--auth-sshkeygen-path", str(missing / "ssh-keygen"),
                       "--db", str(root / "app.sqlite"), "--keys", str(root / "keys.env"),
                       "--policy", str(root / "policy.json"), "--keysync-interval", "0",
                       "--api-port", str(port)]
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                                self.assertEqual(response.status, 200)
                                break
                        except OSError:
                            self.assertIsNone(proc.poll())
                            if time.monotonic() > deadline:
                                self.fail("server did not start")
                            time.sleep(.05)
                finally:
                    proc.terminate()
                    proc.wait(timeout=10)
                self.assertEqual(proc.returncode, 0)
                if mode == "off":
                    self.assertFalse(missing.exists())
