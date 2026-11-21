import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lgwctl import usage
from .lgwctl_fakes import Sandbox, FAKE_TAG, NEW_TAG

SENTINELS = ('TEST_PASS_SENTINEL', 'sk-test-sentinel-xyz', 'TEST_GH_TOKEN_SENTINEL',
             'SENTINEL_HOST_KEY', 'TEST_IP_SENTINEL', 'MALICIOUS_ESC')

@pytest.mark.parametrize('argv', [
    ['install', '--tag', FAKE_TAG], ['check'], ['status'],
    ['upgrade', '--tag', NEW_TAG], ['rollback'], ['sync'], ['usage'],
    ['start'], ['stop'], ['restart'], ['follow-reset'], ['follow-reset', '--fetch-state'],
    ['prepare-migration', '--tag', 'bad'], ['cutover', '--tag', FAKE_TAG, '--inner'],
    ['confirm'], ['rollback-to-user', '--inner'], ['finalize-migration'],
])
def test_all_commands_hide_sentinels(argv, monkeypatch):
    monkeypatch.setattr('os.geteuid', lambda: 0)
    monkeypatch.setattr(usage, 'Client', lambda *a: SimpleNamespace(list=lambda: SimpleNamespace(ok=True, data={'subscriptions': []})))
    with Sandbox() as b:
        b.install_state(FAKE_TAG, previous='lgw-v1.2.2')
        b.set_active('llmgateway.service')
        b.set_active('llmgateway-tunnel.service')
        b.env.pop('INVOCATION_ID', None)
        b.env['FAKE_CURL_CODE'] = 'TEST_IP_SENTINEL'
        b.write(b.etc('keys.pass'), 'LLMGW_KEYS_PASSWORD=TEST_PASS_SENTINEL\n')
        b.write(b.state('keys.env'), 'API_KEY=sk-test-sentinel-xyz\n')
        b.write(b.etc('tunnel.env'), 'FOLLOW_FILE=' + b.state('gateway.json') + '\nGATEWAY_GITHUB_KEY_FILE=' + b.etc('fake-token') + '\n')
        b.write(b.etc('fake-token'), 'TEST_GH_TOKEN_SENTINEL')
        b.write(b.state('gateway.json'), json.dumps({'ip':'TEST_IP_SENTINEL', 'host_key':'SENTINEL_HOST_KEY', 'malicious':'MALICIOUS_ESC'}))
        b.write(b.state('.tunnel_generation'), json.dumps({'generation': 1, 'fingerprint':'MALICIOUS_ESC'}))
        b.write(b.state('.tunnel_fetch_state'), json.dumps({'status':'MALICIOUS_ESC'}))
        _, out, err = b.run_cli(argv)
        for value in SENTINELS:
            assert value not in out + err, (argv, value, out, err)
