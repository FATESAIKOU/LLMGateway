import json
import base64
import struct
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lgwctl import check, layout as lm, status
from lgwctl.layout import Layout
from .lgwctl_fakes import Sandbox, has_line

@pytest.fixture
def box():
    with Sandbox() as b:
        b.install_state()
        b.set_active('llmgateway.service')
        b.set_active('llmgateway-tunnel.service')
        b.write(b.etc('tunnel.env'), 'FOLLOW_FILE=' + b.state('gateway.json') + '\n')
        b.write(b.state('keys.env'), 'sk-test-sentinel-xyz')
        b.write(b.state('.keysync.status'), json.dumps({'ok': True, 'ts': __import__('time').time()}))
        yield b

def follow(b, gen=2):
    b.write(b.state('gateway.json'), json.dumps({'generation': gen, 'ip': 'example.invalid', 'ssh_port': 22, 'tunnel_user': 'user', 'host_key': 'ssh-ed25519 ' + base64.b64encode(struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'x' * 32).decode()}))

def test_follow_six_states(box, monkeypatch):
    l = Layout.from_env(box.env)
    assert l.tunnel_follow_state() == ('missing', None)
    box.write(box.state('gateway.json'), '{')
    assert l.tunnel_follow_state() == ('invalid', None)
    follow(box, 1)
    box.write(box.state('.tunnel_generation'), json.dumps({'generation': 2, 'fingerprint': 'a' * 64}))
    assert l.tunnel_follow_state() == ('regressed', 2)
    follow(box, 3)
    assert l.tunnel_follow_state() == ('ok', 2)
    original = os.open
    def denied(path, *a, **kw):
        if str(path).endswith('gateway.json'):
            raise PermissionError()
        return original(path, *a, **kw)
    monkeypatch.setattr(os, 'open', denied)
    assert l.tunnel_follow_state() == ('unreadable', None)
    box.write(box.etc('tunnel.env'), '')
    assert l.tunnel_follow_state() == ('unconfigured', None)

@pytest.mark.parametrize('name', ['gateway.json', '.tunnel_generation', '.tunnel_fetch_state', '.tunnel_token_meta'])
def test_state_symlinks_are_never_read(box, name, tmp_path):
    follow(box)
    secret = tmp_path / 'secret'
    payloads = {
        'gateway.json': json.loads(Path(box.state('gateway.json')).read_text()),
        '.tunnel_generation': {'generation': 1, 'fingerprint': 'a' * 64},
        '.tunnel_fetch_state': {'status': 'ok'},
        '.tunnel_token_meta': {'fetched_at': '2026-01-01T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z'},
    }
    payloads[name]['secret'] = 'TEST_GH_TOKEN_SENTINEL'
    secret.write_text(json.dumps(payloads[name]))
    target = Path(box.state(name))
    target.unlink(missing_ok=True)
    target.symlink_to(secret)
    l = Layout.from_env(box.env)
    if name in ('gateway.json', '.tunnel_generation'):
        assert l.tunnel_follow_state()[0] == 'invalid'
    elif name == '.tunnel_fetch_state':
        assert l.tunnel_fetch_state() == 'invalid'
    else:
        assert l.tunnel_token_state() == 'expiry_invalid'
    rc, out, err = box.run_cli(['status'])
    assert 'TEST_GH_TOKEN_SENTINEL' not in out + err

@pytest.mark.parametrize('kind', ['regressed', 'invalid', 'hostkey_missing', 'auth_failed', 'config_partial'])
def test_active_fetch_alerts(box, kind):
    follow(box)
    box.write(box.state('.tunnel_fetch_state'), json.dumps({'status': kind}))
    rc, out, err = box.run_cli(['check'])
    assert rc == 2, out + err
    assert kind in out
    if kind == 'auth_failed':
        assert '--fetch-check' in out and '--fetch-state' in out
    elif kind in ('regressed', 'invalid', 'hostkey_missing'):
        assert '最後已知位址' in out and 'NODE_GATEWAY' in out

@pytest.mark.parametrize('kind', ['regressed', 'invalid', 'hostkey_missing', 'auth_failed'])
def test_inactive_fetch_fail(box, kind):
    follow(box)
    box.write(box.state('.tunnel_fetch_state'), kind)
    box.set_active('llmgateway-tunnel.service', False)
    rc, out, err = box.run_cli(['check'])
    assert rc == 1 and 'follow-reset' in out
    assert has_line(out, 'tunnel-fetch', 'fail'), out

@pytest.mark.parametrize('state', ['missing', 'invalid', 'regressed'])
def test_inactive_follow_fail(box, state):
    if state == 'invalid': box.write(box.state('gateway.json'), '{')
    if state == 'regressed':
        follow(box, 1)
        box.write(box.state('.tunnel_generation'), json.dumps({'generation': 2, 'fingerprint': 'a' * 64}))
    box.set_active('llmgateway-tunnel.service', False)
    rc, out, err = box.run_cli(['check'])
    assert rc == 1 and 'follow-reset' in out
    assert has_line(out, 'tunnel-follow', 'fail'), out


@pytest.mark.parametrize('name', ['gateway.json', '.tunnel_generation', '.tunnel_fetch_state', '.tunnel_token_meta'])
def test_unreadable_status_and_check(box, monkeypatch, name):
    follow(box)
    box.set_active('llmgateway-tunnel.service', False)
    old_open = os.open
    def denied(path, *a, **kw):
        if str(path).endswith(name): raise PermissionError()
        return old_open(path, *a, **kw)
    monkeypatch.setattr(os, 'open', denied)
    rc, out, err = box.run_cli(['status'])
    if name == '.tunnel_token_meta':
        assert 'tunnel-token' in out and 'unreadable' in out
    else:
        assert 'unreadable' in out and '需 root 或 llmgw' in out and 'missing' not in out
    rc, out, err = box.run_cli(['check'])
    assert rc == (1 if name == '.tunnel_token_meta' else 2) and '需 root 或 llmgw' in out


def test_token_expiry_and_fetch_status(box):
    follow(box)
    env = Path(box.etc('tunnel.env'))
    env.write_text(env.read_text() + 'TOKEN_EXPIRES_AT=bad\n')
    assert 'TOKEN_EXPIRES_AT 格式不合' in box.run_cli(['check'])[1]
    env.write_text(env.read_text().replace('bad', (datetime.now(timezone.utc)+timedelta(days=3)).isoformat()))
    assert box.run_cli(['check'])[0] == 2
    env.write_text('FOLLOW_FILE=' + box.state('gateway.json') + '\n')
    box.write(box.state('.tunnel_token_meta'), json.dumps({'fetched_at': '2026-01-02T00:00:00Z', 'expires_at': '2026-01-01T00:00:00Z'}))
    assert Layout.from_env(box.env).tunnel_token_state() == 'expiry_unknown'
    box.write(box.state('.tunnel_fetch_state'), 'credential_unreadable')
    assert box.run_cli(['check'])[0] == 2


def test_local_only_check_and_status(box):
    Path(box.etc('tunnel.env')).unlink()
    box.set_missing('llmgateway-tunnel.service')
    assert box.run_cli(['check'])[0] == 0
    rc, out, err = box.run_cli(['status'])
    assert 'llmgateway-tunnel.service' not in out


def test_local_only_install_upgrade_rollback(monkeypatch):
    from .lgwctl_fakes import FAKE_TAG, NEW_TAG
    with Sandbox() as b:
        b.set_missing('llmgateway-tunnel.service')
        b.env['FAKE_GIT_SKIP'] = 'deploy/units/llmgateway-tunnel.service'
        rc, out, err = b.run_cli(['install', '--tag', FAKE_TAG])
        assert rc == 0, out + err
        assert not Path(b.etc('tunnel.env')).exists()
        assert 'tunnel-key' not in out and 'enable llmgateway-tunnel.service' not in b.actions()
        b.write(b.state('.keysync.status'), json.dumps({'ok': True, 'ts': __import__('time').time()}))
        assert b.run_cli(['check'])[0] == 0
        assert 'llmgateway-tunnel.service' not in b.run_cli(['status'])[1]
        rc, out, err = b.run_cli(['upgrade', '--tag', NEW_TAG])
        assert rc == 0, out + err
        rc, out, err = b.run_cli(['rollback'])
        assert rc == 0, out + err


def test_invalid_follow_fields_rejected(box):
    follow(box)
    p = Path(box.state('gateway.json'))
    data = json.loads(p.read_text())
    for field, value in [('ip', '-oProxyCommand=bad'), ('ip', 'fe80::1%bad'), ('tunnel_user', '-evil'), ('host_key', 'bad\nkey'), ('ssh_port', True), ('generation', True)]:
        p.write_text(json.dumps({**data, field: value}))
        assert Layout.from_env(box.env).tunnel_follow_state()[0] == 'invalid', field


def test_same_generation_changed_fields_invalid(box):
    follow(box, 5)
    data = json.loads(Path(box.state('gateway.json')).read_text())
    fingerprint = lm.follow_fingerprint(data)
    box.write(box.state('.tunnel_generation'), json.dumps({'generation': 5, 'fingerprint': fingerprint}))
    assert Layout.from_env(box.env).tunnel_follow_state() == ('ok', 5)
    data['ip'] = 'different.example'
    box.write(box.state('gateway.json'), json.dumps(data))
    assert Layout.from_env(box.env).tunnel_follow_state()[0] == 'invalid'


def test_follow_status_generation_and_pending(box):
    follow(box, 8)
    rc, out, err = box.run_cli(['status'])
    assert 'ok (gen=pending)' in out and 'tunnel-fetch' in out and 'none' in out
    data = json.loads(Path(box.state('gateway.json')).read_text())
    box.write(box.state('.tunnel_generation'), json.dumps({'generation': 8, 'fingerprint': lm.follow_fingerprint(data)}))
    box.write(box.state('.tunnel_fetch_state'), 'ok')
    rc, out, err = box.run_cli(['status'])
    assert 'ok (gen=8)' in out and 'tunnel-fetch' in out


@pytest.mark.parametrize('name', ['gateway.json', '.tunnel_generation', '.tunnel_fetch_state', '.tunnel_token_meta'])
def test_state_files_reject_oversize(box, name):
    follow(box)
    box.write(box.state(name), 'x' * 65537)
    l = Layout.from_env(box.env)
    if name in ('gateway.json', '.tunnel_generation'):
        assert l.tunnel_follow_state()[0] == 'invalid'
    elif name == '.tunnel_fetch_state':
        assert l.tunnel_fetch_state() == 'invalid'
    else:
        assert l.tunnel_token_state() == 'expiry_invalid'


def test_installed_unit_checked_and_no_remote_skips(box):
    follow(box)
    box.set_active('llmgateway-tunnel.service', False)
    box.env['FAKE_PROBE_RC'] = '1'
    rc, out, err = box.run_cli(['check'])
    assert rc == 1 and 'list-remote' in out and 'llmgateway-tunnel.service' in out
    rc, out, err = box.run_cli(['check', '--no-remote'])
    assert rc == 0 and 'list-remote' not in out and 'llmgateway-tunnel.service' not in out
    rc, out, err = box.run_cli(['status', '--no-remote'])
    assert 'list-remote' not in out and 'llmgateway-tunnel.service' not in out


def test_missing_credential_is_todo(box):
    follow(box)
    with open(box.etc('tunnel.env'), 'a') as f:
        f.write('GATEWAY_GITHUB_KEY_FILE=' + box.etc('absent') + '\n')
    rc, out, err = box.run_cli(['check'])
    assert rc == 2 and 'tunnel-credential' in out


def test_expiry_header_recent_and_auth_failed_status(box):
    follow(box)
    now = datetime.now(timezone.utc)
    box.write(box.state('.tunnel_token_meta'), json.dumps({'fetched_at': (now-timedelta(days=1)).isoformat(), 'expires_at': (now+timedelta(days=2)).isoformat()}))
    rc, out, err = box.run_cli(['check'])
    assert rc == 2 and 'token 即將到期' in out
    box.write(box.state('.tunnel_fetch_state'), 'auth_failed')
    assert 'auth_failed' in box.run_cli(['status'])[1]


def test_active_follow_anomalies_warn(box):
    for contents in ('{', None):
        path = Path(box.state('gateway.json'))
        if contents is None: path.unlink(missing_ok=True)
        else: path.write_text(contents)
        rc, out, err = box.run_cli(['check'])
        assert rc == 2 and 'tunnel-follow' in out and '最後已知位址' in out


def test_dynamic_partial_requires_usable_initial_baseline(box, tmp_path):
    identity = tmp_path / 'identity'
    identity.write_text('fake')
    Path(box.etc('tunnel.env')).write_text(f'IDENTITY={identity}\nRENDEZVOUS=user@example.invalid:22\nGATEWAY_SOURCE_REPO=example/repo\n')
    l = Layout.from_env(box.env)
    assert l.tunnel_config_state()[0] is False
    known = tmp_path / 'known_hosts'
    known.write_text('fake')
    with open(box.etc('tunnel.env'), 'a') as f:
        f.write(f'KNOWN_HOSTS={known}\n')
    assert l.tunnel_config_state()[0] is True


def test_active_regressed_follow_reason(box):
    follow(box, 1)
    box.write(box.state('.tunnel_generation'), json.dumps({'generation': 2, 'fingerprint': 'a' * 64}))
    rc, out, err = box.run_cli(['check'])
    assert rc == 2 and has_line(out, 'tunnel-follow', 'todo')
    assert 'follow 檔回退，現存連線靠最後已知位址維持；需人工確認 NODE_GATEWAY' in out


def test_remote_probe_unavailable_detail(box):
    follow(box)
    box.env['FAKE_PROBE_RC'] = '2'
    rc, out, err = box.run_cli(['check'])
    assert has_line(out, 'list-remote', 'todo')
    line = next(line for line in out.splitlines() if 'list-remote' in line)
    assert '無法探' in line and '隧道設定未填' not in line


def test_configured_missing_tunnel_unit_fails(box):
    follow(box)
    box.set_missing('llmgateway-tunnel.service')
    rc, out, err = box.run_cli(['check'])
    assert rc == 1 and has_line(out, 'llmgateway-tunnel.service', 'fail')
    assert 'unit 不在（沒裝）' in out


def test_unreadable_tunnel_env(box, monkeypatch):
    import builtins
    old_open = builtins.open
    def denied(path, *args, **kwargs):
        if str(path) == box.etc('tunnel.env'):
            raise PermissionError()
        return old_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', denied)
    box.set_missing('llmgateway-tunnel.service')
    assert Layout.from_env(box.env).tunnel_follow_state() == ('unreadable', None)
    rc, out, err = box.run_cli(['check'])
    assert rc == 2 and '需 root 或 llmgw 才能讀' in out
    assert '隧道設定未填' not in out
    rc, out, err = box.run_cli(['status'])
    assert 'unreadable（需 root 或 llmgw 才能讀）' in out


def test_follow_fingerprint_golden(box):
    follow(box)
    data = json.loads(Path(box.state('gateway.json')).read_text())
    assert lm.follow_fingerprint(data) == '73d0194f7c979828b6da5f50f5bf40722637ae923471e173dc5794c969a344dd'
