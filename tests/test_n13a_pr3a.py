"""Role selection regression tests using generated fixture credentials."""
import json
from pathlib import Path

import pytest
from lgwd.clock import FakeClock
from lgwd.errors import ApiError
from lgwd.http import _resolve_query, _roles_query
from lgwd.keys import KeyStore
from lgwd.policy import load_policy
from lgwd.registry import RegistryService
from lgwd.roletable import load_valid
from tests.test_registry import key_text, CANARY

ROOT = Path(__file__).resolve().parent.parent

class CountingKeyStore(KeyStore):
    def __init__(self, path):
        self.get_calls = []
        self.deployed_calls = []
        super().__init__(path)

    def get(self, subscription):
        self.get_calls.append(subscription)
        return super().get(subscription)

    def deployed(self, subscription):
        self.deployed_calls.append(subscription)
        return super().deployed(subscription)

@pytest.fixture
def service(tmp_path):
    policy = load_policy(str(ROOT / 'policy/default.json'))
    keys = tmp_path / 'keys'
    keys.write_text(key_text(CANARY))
    table = load_valid(ROOT / 'policy/role-rotation.default.json', policy)
    assert table is not None
    obj = RegistryService(str(tmp_path / 'db'), policy, CountingKeyStore(str(keys)), FakeClock(), roles_provider=lambda: table)
    yield obj
    obj.close()

def test_query_and_legacy_compatibility():
    q = _resolve_query('role=impl&task_length=short&exclude=a,b&exclude=b,c&exclude_family=foo,bar&exclude_family=bar')
    assert q['exclude'] == ['a', 'b', 'c']
    assert q['exclude_family'] == ['foo', 'bar']
    assert _resolve_query('role=&agent=opencode&provider=x')['agent'] == 'opencode'
    for q in ('role=impl&role=', 'agent=x&exclude=a&exclude=b', 'role=impl', 'role=impl&task_length=long&ref=' + 'x'*65):
        with pytest.raises(ApiError) as exc:
            _resolve_query(q)
        assert exc.value.status == 400
    with pytest.raises(ApiError):
        _roles_query('agent=x&agent=y')

def test_role_select_and_audit(service):
    q = _resolve_query('role=impl&task_length=short&data=public&ref=abc')
    result = service.resolve_role(q)
    assert result['meta']['role'] == 'impl'
    assert result['meta']['slot'] == 1
    assert result['meta']['table_sha']
    with service._store.txn() as cur:
        row = dict(cur.execute('SELECT * FROM audit').fetchone())
    assert row['ref'] == 'abc' and row['role'] == 'impl'
    assert json.loads(row['skipped']) == []
    assert 'canary-registry' not in repr(row)

def test_precedence_and_no_candidates(service):
    for role, code in [('PMO', 'role_not_selectable'), ('unknown', 'not_found')]:
        with pytest.raises(ApiError) as exc:
            service.resolve_role(_resolve_query(f'role={role}&task_length=short'))
        assert exc.value.code == code
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query('role=impl&task_length=short&model=not-in-table'))
    assert exc.value.code == 'model_not_allowed'
    table = service._roles_provider()
    excluded = ','.join({x.subscription for x in table.roles['impl']})
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query(f'role=impl&task_length=long&data=public&exclude={excluded}'))
    assert exc.value.code == 'no_candidate'
    assert len(exc.value.extra['skipped']) == len(table.roles['impl'])

def test_cooldown_audit_and_role_gate(service):
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T01:00:00Z'})
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T02:00:00Z', 'mode':'clear'})
    with service._store.txn() as cur:
        rows = [dict(x) for x in cur.execute('SELECT * FROM audit')]
    assert [(x['cooldown_mode'], x['prev_until']) for x in rows] == [('extend', None), ('extend', '2026-10-11T01:00:00Z')]
    result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert result['meta']['skipped'][0]['reason'] == 'subscription_cooling'

@pytest.mark.parametrize('mode,agent_allowed,role_allowed,status,outcome', [
    ('off', False, False, 200, None),
    ('warn', True, True, 200, 'pass'),
    ('enforce', True, True, 200, 'pass'),
    ('warn', False, True, 200, 'forbidden'),
    ('enforce', False, True, 403, 'forbidden'),
    ('warn', True, False, 200, 'forbidden'),
    ('enforce', True, False, 403, 'forbidden'),
])
def test_http_role_authz(service, mode, agent_allowed, role_allowed, status, outcome):
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    import threading
    entry = PrincipalEntry(frozenset({'resolve'}), ('opencode',) if agent_allowed else (), {'impl': ('opencode',)} if role_allowed else {})
    class Gate:
        def __init__(self): self.mode = mode
        def authenticate(self, **kwargs): return auth.passed('test', entry)
    class Log:
        def __init__(self): self.events = []
        def record(self, **kwargs): self.events.append(kwargs)
    log = Log()
    server = make_api_server(service, '127.0.0.1', 0, gate=None if mode == 'off' else Gate(), auth_log=log)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    try:
        response = exchange(server.server_address[1], req('GET', '/v1/resolve?role=impl&task_length=short&data=public'))
        code, body, headers = parse_response(response)
        assert code == status, body
        assert len(log.events) == (0 if outcome is None else 1)
        if outcome: assert log.events[0]['outcome'] == outcome
        assert len(service._store.audit_all()) == (1 if status == 200 else 0)
        if status == 200: assert headers['cache-control'] == 'no-store'
    finally:
        server.shutdown(); server.server_close(); thread.join()

@pytest.mark.parametrize('role,public,real', [('impl',15,5),('qa',14,5),('rev',6,5),('arch',4,4)])
def test_data_role_matrix(service, role, public, real):
    slots = service._roles_provider().roles[role]
    from lgwd.policy import PRIVACY_LEVELS
    for data, expected in [('public',public), ('general',real), ('real',real)]:
        floor = service._policy.roles[role].floor
        effective = max(PRIVACY_LEVELS.index(data), PRIVACY_LEVELS.index(floor))
        assert sum(PRIVACY_LEVELS.index(service._policy.subscriptions[s.subscription].privacy) >= effective for s in slots) == expected

def test_excluded_still_records_cooling_until(service):
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T01:00:00Z'})
    result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public&exclude=opencode-zen'))
    assert result['meta']['skipped'][0]['reason'] == 'excluded'
    assert result['meta']['skipped'][0]['until'] == '2026-10-11T01:00:00Z'

def test_audit_skipped_truncation(service):
    table = service._roles_provider()
    slot = table.roles['impl'][0]
    skipped = [{'slot': i, 'subscription': 's'*70, 'model': 'm'*70, 'reason':'excluded', 'until':None} for i in range(60)]
    with service._store.txn() as cur:
        service._store.audit_role(cur, '2026-10-11T00:00:00Z', slot.subscription, slot, 1, 'test', None, {'role':'impl','task_length':'short'}, 'public', table.table_sha, skipped)
        row = dict(cur.execute('SELECT * FROM audit').fetchone())
    assert len(row['skipped'].encode()) <= 2048
    assert row['skipped_truncated'] == '1'
    assert json.loads(row['skipped'])[-1] == {'truncated':True}

def test_unavailable_roles_precedence(service):
    service._roles_provider = lambda: None
    for role, code, status in [('PMO','role_not_selectable',400), ('unknown','roles_unavailable',503)]:
        with pytest.raises(ApiError) as exc:
            service.resolve_role(_resolve_query(f'role={role}&task_length=short'))
        assert (exc.value.code, exc.value.status) == (code,status)

def test_authorize_precedes_key_read_and_audit(service):
    from unittest.mock import patch
    q = _resolve_query('role=impl&task_length=short&data=public')
    with patch.object(service._keys, 'get', wraps=service._keys.get) as get, patch.object(service._keys, 'deployed', wraps=service._keys.deployed) as deployed:
        def reject(agent):
            assert agent == 'opencode'
            assert deployed.call_count >= 1
            assert get.call_count == 0
            raise PermissionError()
        with pytest.raises(PermissionError):
            service.resolve_role(q, authz=reject)
        assert get.call_count == 0
    assert service._store.audit_all() == []

def test_usage_fail_open_in_registry(service):
    from unittest.mock import patch
    with patch.object(service._usage, 'usage_for', side_effect=RuntimeError('fixture')):
        result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert result['meta']['slot'] == 1

def test_model_cooldown_max_and_exclusion(service):
    model = service._roles_provider().roles['impl'][0].model
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T01:00:00Z'})
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T02:00:00Z', 'model':model})
    result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    first = result['meta']['skipped'][0]
    assert first['reason'] == 'subscription_cooling'
    assert first['until'] == '2026-10-11T02:00:00Z'

def test_legacy_duplicate_exclude_is_rejected():
    with pytest.raises(ApiError) as e:
        _resolve_query('agent=opencode&provider=opencode-zen&exclude=a&exclude=b')
    assert e.value.status == 400

def test_http_roles_unavailable_and_concurrent_snapshots(service):
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    import threading
    from concurrent.futures import ThreadPoolExecutor
    table = service._roles_provider()
    service._roles_provider = lambda: None
    server = make_api_server(service, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01})
    thread.start()
    try:
        def request():
            status, body, _ = parse_response(exchange(server.server_address[1], req('GET', '/v1/resolve?role=impl&task_length=short&data=public')))
            return status, json.loads(body)
        status, body = request()
        assert (status,body['error']) == (503,'roles_unavailable')
        service._roles_provider = lambda: table
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: request(), range(8)))
        assert all(status == 200 and body['meta']['table_sha'] == table.table_sha for status, body in results)
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_toctou_missing_key_reports_every_slot(service, tmp_path):
    from unittest.mock import patch
    q = _resolve_query('role=impl&task_length=short&data=public')
    def remove_key(agent):
        assert agent == 'opencode'
        values = {name: value for name, value in CANARY.items() if name not in ('opencode-zen', 'opencode-go')}
        Path(service._keys._path).write_text(key_text(values))
        service._keys.reload()
    with patch.object(service._keys, 'get', wraps=service._keys.get) as get:
        with pytest.raises(ApiError) as exc:
            service.resolve_role(q, authz=remove_key)
    assert get.call_count == 1
    assert exc.value.code == 'not_deployed'
    skipped = exc.value.extra['skipped']
    assert len(skipped) == len(service._roles_provider().roles['impl'])
    assert skipped[0]['reason'] == 'not_deployed'
    assert any(x['reason'] is None for x in skipped[1:])
    assert service._store.audit_all() == []

def test_cooling_skipped_is_complete_and_non_null(service):
    slots = service._roles_provider().roles['impl']
    for sub in {s.subscription for s in slots}:
        service.register_cooldown(sub, {'until':'2026-10-11T01:00:00Z'})
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert exc.value.code == 'cooling'
    assert len(exc.value.extra['skipped']) == len(slots)
    assert all(item['reason'] is not None for item in exc.value.extra['skipped'])

def test_model_and_short_only(service):
    from urllib.parse import urlencode
    slots = service._roles_provider().roles['impl']
    target = next(s for s in slots if s.short_only)
    q = {'role':'impl', 'task_length':'short', 'data':'public', 'model':target.model}
    assert service.resolve_role(_resolve_query(urlencode(q)))['meta']['model'] == target.model
    q['task_length'] = 'long'
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query(urlencode(q)))
    assert exc.value.code == 'no_candidate'
    assert next(x for x in exc.value.extra['skipped'] if x['model'] == target.model)['reason'] == 'short_only'

def test_legacy_query_differential_corpus():
    from lgwd.http import _query, _RESOLVE_PARAMS, _ROLES_PARAMS
    corpus = ['agent=opencode&provider=x', 'agent=&provider=', 'agent=opencode&provider=x&ref=z&task_length=long', 'role=&agent=codex&provider=openai']
    for query in corpus:
        assert _resolve_query(query) == _query(query, _RESOLVE_PARAMS)
    for key in _ROLES_PARAMS:
        with pytest.raises(ApiError):
            _roles_query(f'{key}=one&{key}=two')
    assert _roles_query('agent=codex&role=x&exclude_family=a&task_length=short&ref=x') == {'agent':'codex'}
    for query in ('role=impl&role=impl', 'role=impl&role='):
        with pytest.raises(ApiError): _resolve_query(query)

def test_privacy_matrix_by_resolving_each_model(service):
    from urllib.parse import urlencode
    from lgwd.policy import PRIVACY_LEVELS
    for role, slots in service._roles_provider().roles.items():
        for slot in slots:
            for data in ('public', 'general', 'real'):
                q = _resolve_query(urlencode({'role':role, 'model':slot.model, 'data':data, 'task_length':'short'}))
                expected = max(PRIVACY_LEVELS.index(data), PRIVACY_LEVELS.index(service._policy.roles[role].floor))
                eligible = any(s.model == slot.model and PRIVACY_LEVELS.index(service._policy.subscriptions[s.subscription].privacy) >= expected for s in slots)
                try:
                    result = service.resolve_role(q)
                except ApiError as exc:
                    assert (exc.code == 'privacy_not_allowed') == (not eligible)
                    assert exc.code in ('privacy_not_allowed','not_deployed')
                else:
                    assert eligible and result['meta']['model'] == slot.model
                    assert PRIVACY_LEVELS.index(service._policy.subscriptions[result['meta']['subscription']].privacy) >= expected

@pytest.mark.parametrize('mode,case,status,event,reason,audit_count,get_count', [
    ('off','pass',200,None,None,1,1),
    ('warn','deny',200,'deny','missing',1,1),
    ('enforce','deny',401,'deny','missing',0,0),
    ('warn','limited',200,'rate_limited','rate_limited',1,1),
    ('enforce','limited',429,'rate_limited','rate_limited',0,0),
    ('warn','unavailable',200,'unavailable','busy',1,1),
    ('enforce','unavailable',503,'unavailable','busy',0,0),
    ('warn','op',200,'forbidden','forbidden_op',1,1),
    ('enforce','op',403,'forbidden','forbidden_op',0,0),
    ('warn','shape',400,'pass','',0,0),
    ('enforce','shape',400,'pass','',0,0),
    ('warn','business',503,'pass','',0,0),
    ('enforce','business',503,'pass','',0,0),
    ('warn','pass',200,'pass','',1,1),
    ('enforce','pass',200,'pass','',1,1),
    ('warn','agent',200,'forbidden','forbidden_agent',1,1),
    ('enforce','agent',403,'forbidden','forbidden_agent',0,0),
    ('warn','role',200,'forbidden','forbidden_role',1,1),
    ('enforce','role',403,'forbidden','forbidden_role',0,0),
    ('warn','exception',500,'pass','',0,0),
    ('enforce','exception',500,'pass','',0,0),
])
def test_auth_event_table(service, mode, case, status, event, reason, audit_count, get_count):
    import threading
    from unittest.mock import patch
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    entry = PrincipalEntry(frozenset() if case == 'op' else frozenset({'resolve'}), () if case == 'agent' else ('opencode',), {} if case == 'role' else {'impl':('opencode',)})
    verdict = {'deny': lambda: auth.denied(auth.REASON_MISSING), 'limited':lambda: auth.limited(1), 'unavailable':lambda: auth.unavailable(auth.REASON_BUSY)}.get(case, lambda: auth.passed('test', entry))()
    class Gate:
        def __init__(self): self.mode = mode
        def authenticate(self, **kwargs): return verdict
    class Log:
        def __init__(self): self.events = []
        def record(self, **kwargs): self.events.append(kwargs)
    if case == 'business': service._roles_provider = lambda: None
    if case == 'exception': service._roles_provider = lambda: (_ for _ in ()).throw(RuntimeError('fixture'))
    log = Log()
    server = make_api_server(service, '127.0.0.1', 0, gate=None if mode == 'off' else Gate(), auth_log=log)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01}); thread.start()
    target = '/v1/resolve?role=impl&data=public' + ('' if case == 'shape' else '&task_length=short')
    try:
        with patch.object(service._keys, 'get', wraps=service._keys.get) as get:
            actual, body, _ = parse_response(exchange(server.server_address[1], req('GET', target)))
            assert actual == status, body
            payload = json.loads(body)
            if status == 200:
                assert payload['meta']['role'] == 'impl'
            else:
                codes = {400:'bad_request', 401:'unauthorized', 403:'forbidden', 429:'rate_limited', 500:'internal', 503:'roles_unavailable' if case == 'business' else 'auth_unavailable'}
                assert payload['error'] == codes[status]
            assert get.call_count == get_count
        rows = service._store.audit_all()
        assert len(rows) == audit_count
        if rows:
            assert rows[0]['principal'] == (None if mode == 'off' or case in ('deny','limited','unavailable') else 'test')
        assert len(log.events) == (0 if event is None else 1)
        if event: assert (log.events[0]['outcome'], log.events[0]['reason']) == (event, reason)
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_local_login_never_reads_key(service):
    from unittest.mock import patch
    from urllib.parse import urlencode
    slot = next(s for s in service._roles_provider().roles['arch'] if s.agent == 'codex')
    q = _resolve_query(urlencode({'role':'arch','task_length':'short','model':slot.model}))
    with patch.object(service._keys, 'get', wraps=service._keys.get) as get, patch.object(service._keys, 'deployed', wraps=service._keys.deployed) as deployed:
        result = service.resolve_role(q, authz=lambda agent: None)
    assert result['meta']['agent'] == 'codex'
    assert get.call_count == 0
    assert all(call.args[0] != slot.subscription for call in deployed.call_args_list)

def test_gateway_bad_tables_and_atomic_reload_http(tmp_path, monkeypatch):
    import hashlib
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from lgwd.main import Config, Gateway
    from lgwd.providers import UsageDeps
    from tests.usage_fakes import refuse_http
    from tests.test_http_framing import req, exchange, parse_response
    monkeypatch.setenv('LLMGW_PREFIX', str(tmp_path))
    monkeypatch.setattr('lgwd.main.default_rolesync_script', lambda: str(tmp_path / 'no-script'))
    policy_dir = tmp_path / 'policy'; policy_dir.mkdir()
    policy = policy_dir / 'default.json'; policy.write_bytes((ROOT / 'policy/default.json').read_bytes())
    fallback = policy_dir / 'role-rotation.default.json'
    fallback.write_text('{invalid')
    keyfile = tmp_path / 'keys'; keyfile.write_text(key_text(CANARY))
    config = Config(db=str(tmp_path/'db'), policy_path=str(policy), keys_path=str(keyfile), api_host='127.0.0.1', api_port=0, keysync_interval=0)
    gateway = Gateway(config, clock=FakeClock(), usage_deps=UsageDeps(http_get=refuse_http))
    gateway.start()
    rolepath = tmp_path / 'var/lib/llmgateway/role-rotation.json'; rolepath.parent.mkdir(parents=True)
    raw_a = (ROOT/'policy/role-rotation.default.json').read_bytes()
    obj = json.loads(raw_a); obj['roles']['impl'][0], obj['roles']['impl'][1] = obj['roles']['impl'][1], obj['roles']['impl'][0]
    raw_b = json.dumps(obj).encode()
    shas = {hashlib.sha256(x).hexdigest()[:12] for x in (raw_a, raw_b)}
    def rotate(raw):
        candidate = rolepath.with_suffix('.new'); candidate.write_bytes(raw); os.replace(candidate, rolepath)
    def get():
        status, raw, _ = parse_response(exchange(gateway.addr[1], req('GET', '/v1/resolve?role=impl&task_length=short&data=public')))
        return status, json.loads(raw)
    try:
        status, body = get(); assert (status, body['error']) == (503, 'roles_unavailable')
        rotate(raw_a); status, body = get(); assert status == 200 and body['meta']['table_sha'] in shas
        def writer():
            for raw in (raw_b, b'{bad', raw_a, raw_b): rotate(raw)
        with ThreadPoolExecutor(max_workers=3) as pool:
            future = pool.submit(writer)
            responses = list(pool.map(lambda _: get(), range(8)))
            future.result()
        assert all(status == 200 and body['meta']['table_sha'] in shas for status, body in responses)
    finally:
        gateway.stop()

@pytest.mark.parametrize('session,weekly,expected', [
    ({'remaining_percent':0, 'resets_at':'broken'}, {'remaining_percent':0, 'resets_at':'2026-10-11T02:00:00Z'}, '2026-10-11T02:00:00Z'),
    ({'remaining_percent':True, 'resets_at':'2026-10-11T03:00:00Z'}, {'remaining_percent':0, 'resets_at':'2026-10-11T02:00:00Z'}, '2026-10-11T02:00:00Z'),
    ({'remaining_percent':'0', 'resets_at':'2026-10-11T03:00:00Z'}, {'remaining_percent':0, 'resets_at':'2026-10-11T02:00:00Z'}, '2026-10-11T02:00:00Z'),
    ({'remaining_percent':0, 'resets_at':'2026-10-11T03:00:00Z'}, {'remaining_percent':0, 'resets_at':'2026-10-11T02:00:00Z'}, '2026-10-11T03:00:00Z'),
])
def test_registry_usage_mixed_windows(service, session, weekly, expected):
    from unittest.mock import patch
    usage = {'status':'ok','session':session,'weekly':weekly}
    with patch.object(service._usage, 'usage_for', return_value=usage):
        with pytest.raises(ApiError) as exc:
            service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert exc.value.code == 'cooling'
    assert exc.value.extra['until'] == expected
    assert all(item['reason'] == 'usage_exhausted' for item in exc.value.extra['skipped'])

def test_usage_fractional_and_equal_now_do_not_block(service):
    from unittest.mock import patch
    usage = {'status':'ok','session':{'remaining_percent':0.2,'resets_at':'2026-10-11T02:00:00Z'},'weekly':{'remaining_percent':0,'resets_at':'2026-10-11T00:00:00Z'}}
    with patch.object(service._usage, 'usage_for', return_value=usage):
        assert service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))['meta']['slot'] == 1

def test_role_audit_columns_and_sensitive_values(service):
    from urllib.parse import urlencode
    q = _resolve_query(urlencode({'role':'impl','task_length':'long','data':'public','exclude_family':'deepseek,zhipu','ref':'life.1'}))
    result = service.resolve_role(q)
    meta = result['meta']
    with service._store.txn() as cur: row = dict(cur.execute('SELECT * FROM audit').fetchone())
    for name in ('role','tier','family','effort','task_length','table_sha'):
        assert row[name] == meta[name]
    assert row['slot'] == str(meta['slot'])
    assert row['exclude_family'] == 'deepseek,zhipu'
    assert (row['data'], row['data_declared'], row['ref'], row['explicit_model'],row['skipped_truncated']) == ('public','public','life.1','0','0')
    assert json.loads(row['skipped']) == meta['skipped']
    assert all(value not in repr(row) for value in CANARY.values())

@pytest.mark.parametrize('mode', ['warn','enforce'])
def test_http_toctou_auth_event_once(service, mode):
    import threading
    from unittest.mock import patch
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    entry = PrincipalEntry(frozenset({'resolve'}), ('opencode',), {'impl':('opencode',)})
    class Gate:
        def __init__(self): self.mode = mode
        def authenticate(self, **kwargs): return auth.passed('test',entry)
    class Log:
        def __init__(self): self.events = []
        def record(self, **kwargs): self.events.append(kwargs)
    log = Log(); server = make_api_server(service, '127.0.0.1', 0, gate=Gate(), auth_log=log)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01}); thread.start()
    try:
        with patch.object(service._keys, 'get', return_value=None) as get:
            status, raw, _ = parse_response(exchange(server.server_address[1], req('GET','/v1/resolve?role=impl&task_length=short&data=public')))
        body = json.loads(raw)
        assert status == 409 and body['error'] == 'not_deployed'
        assert get.call_count == 1
        assert len(body['skipped']) == len(service._roles_provider().roles['impl'])
        assert body['skipped'][0]['reason'] == 'not_deployed'
        assert any(item['reason'] is None for item in body['skipped'][1:])
        assert len(log.events) == 1 and log.events[0]['outcome'] == 'pass'
        assert not service._store.audit_all()
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_legacy_policy_results_and_no_cooldown_gate(service):
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T01:00:00Z'})
    r = service.resolve('opencode', 'opencode-zen')
    assert r['meta']['subscription'] == 'opencode-zen'
    assert r['meta']['cooldown']['until'] == '2026-10-11T01:00:00Z'
    r = service.resolve('agy','google')
    assert r['meta']['subscription'] == 'antigravity-gemini'
    assert r['meta']['model'] == 'gemini-3.8-flash-low'
    assert service.resolve('codex','openai',data='real')['meta']['subscription'] == 'chatgpt-personal'
    assert service.role_candidates('arch','codex')['role'] == 'arch'
    assert service.role_candidates('PMO','claude-code')['role'] == 'PMO'
    assert service.role_candidates('PM','claude-code')['role'] == 'PM'

def test_explicit_model_precedence_and_reasons(service):
    from urllib.parse import urlencode
    slots = service._roles_provider().roles['impl']
    target = next(s for s in slots if s.subscription == 'ollama-cloud')
    q = _resolve_query(urlencode({'role':'impl','task_length':'short','data':'public','model':target.model}))
    result = service.resolve_role(q)
    assert result['meta']['model'] == target.model
    assert any(x['reason'] == 'model_mismatch' for x in result['meta']['skipped'])
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query(urlencode({'role':'impl','task_length':'short','data':'real','model':slots[0].model})))
    assert exc.value.code == 'privacy_not_allowed'

def test_family_exclusion_and_reason(service):
    q = _resolve_query('role=impl&task_length=short&data=public&exclude_family=deepseek')
    result = service.resolve_role(q)
    assert result['meta']['skipped'][0]['reason'] == 'excluded_family'
    assert result['meta']['family'] != 'deepseek'

def test_undeployed_and_model_cooling_reasons(service):
    from unittest.mock import patch
    with patch.object(service._keys, 'deployed', return_value=False):
        result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert any(x['reason'] == 'not_deployed' for x in result['meta']['skipped'])
    model = service._roles_provider().roles['impl'][0].model
    service.register_cooldown('opencode-zen', {'model':model,'until':'2026-10-11T01:00:00Z'})
    result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert result['meta']['skipped'][0]['reason'] == 'model_cooling'

def test_cooling_min_of_cell_maxima(service):
    slots = service._roles_provider().roles['impl']
    for sub in {s.subscription for s in slots}:
        service.register_cooldown(sub, {'until':'2026-10-11T01:00:00Z'})
    service.register_cooldown('opencode-zen', {'model': slots[0].model, 'until':'2026-10-11T03:00:00Z'})
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert exc.value.code == 'cooling'
    assert exc.value.extra['until'] == '2026-10-11T01:00:00Z'
    assert exc.value.extra['skipped'][0]['until'] == '2026-10-11T03:00:00Z'

def test_verification_time_permissions_snapshot(service):
    import threading
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    allow = PrincipalEntry(frozenset({'resolve'}), ('opencode',), {'impl':('opencode',)})
    deny = PrincipalEntry(frozenset({'resolve'}), (), {})
    class Gate:
        mode = 'enforce'
        entry = allow
        def authenticate(self, **kwargs): return auth.passed('test', self.entry)
    class Log:
        def __init__(self): self.events=[]
        def record(self, **kwargs): self.events.append(kwargs)
    gate = Gate(); log = Log(); provider = service._roles_provider
    def swap():
        gate.entry = deny
        return provider()
    service._roles_provider = swap
    server = make_api_server(service, '127.0.0.1', 0, gate=gate, auth_log=log)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01}); thread.start()
    try:
        status, _, _ = parse_response(exchange(server.server_address[1], req('GET','/v1/resolve?role=impl&task_length=short&data=public')))
        assert status == 200 and gate.entry is deny
        assert len(log.events) == 1 and log.events[0]['outcome'] == 'pass'
        assert len(service._store.audit_all()) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()

@pytest.mark.parametrize('query', [
    'role=impl&task_length=invalid', 'role=impl&task_length=',
    'role=impl&task_length=short&task_length=long',
    'role=impl&task_length=short&agent=', 'role=impl&task_length=short&provider=x',
    'role=impl&task_length=short&subscription=x', 'role=impl&task_length=short&data=secret',
    'role=impl&task_length=short&model=', 'role=impl&task_length=short&model=opencode%2Fx',
    'role=impl&task_length=short&model=x%2A', 'role=impl&task_length=short&ref=a%2Fb',
    'role=impl&task_length=short&exclude_family=ABC',
])
def test_role_query_rejects_invalid_shape(query):
    with pytest.raises(ApiError) as exc: _resolve_query(query)
    assert (exc.value.status,exc.value.code) == (400,'bad_request')

def test_no_candidate_preserves_excluded_cooling_until(service):
    slots = service._roles_provider().roles['impl']
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T01:00:00Z'})
    excluded = ','.join({slot.subscription for slot in slots})
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query(f'role=impl&task_length=short&data=public&exclude={excluded}'))
    assert exc.value.code == 'no_candidate'
    assert exc.value.extra['until'] == '2026-10-11T01:00:00Z'
    assert len(exc.value.extra['skipped']) == len(slots)
    assert all(item['reason'] is not None for item in exc.value.extra['skipped'])

def test_expired_cooldown_not_selected_as_gate(service):
    service.register_cooldown('opencode-zen', {'until':'2026-10-11T01:00:00Z'})
    service._clock.advance(hours=1)
    assert service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))['meta']['slot'] == 1

def test_real_data_does_not_fall_back_to_free(service):
    slots = service._roles_provider().roles['impl']
    for slot in slots:
        if service._policy.subscriptions[slot.subscription].privacy == 'real':
            service.register_cooldown(slot.subscription, {'until':'2026-10-11T01:00:00Z'})
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query('role=impl&task_length=short&data=real'))
    assert exc.value.code == 'cooling'
    for item in exc.value.extra['skipped']:
        if item['subscription'] in ('opencode-zen','openrouter'):
            assert item['reason'] == 'privacy_not_allowed'

@pytest.mark.parametrize('mode,permissions', [('off','pass'),('off','agent'),('off','role')])
def test_off_does_not_check_permissions(service, mode, permissions, monkeypatch):
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    import threading
    monkeypatch.setattr('lgwd.http.authorize_role_slot', lambda *args: (_ for _ in ()).throw(AssertionError('off checked permissions')))
    class Log:
        def __init__(self): self.events=[]
        def record(self, **kwargs): self.events.append(kwargs)
    class Gate:
        mode='off'
        def authenticate(self, **kwargs): raise AssertionError('off called gate')
    gate = Gate(); gate.permissions = permissions
    log=Log(); server=make_api_server(service,'127.0.0.1',0,gate=gate,auth_log=log)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01}); thread.start()
    try:
        status, _, _ = parse_response(exchange(server.server_address[1], req('GET','/v1/resolve?role=impl&task_length=short&data=public')))
        assert status == 200 and not log.events and len(service._store.audit_all()) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_legacy_http_records_one_auth_event(service):
    import threading
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    class Gate:
        mode='enforce'
        def authenticate(self, **kwargs): return auth.passed('test', PrincipalEntry(frozenset({'resolve'}),('opencode',),{}))
    class Log:
        def __init__(self): self.events=[]
        def record(self, **kwargs): self.events.append(kwargs)
    log=Log(); server=make_api_server(service,'127.0.0.1',0,gate=Gate(),auth_log=log)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01}); thread.start()
    try:
        status, _, _ = parse_response(exchange(server.server_address[1], req('GET','/v1/resolve?agent=opencode&provider=opencode-zen')))
        assert status == 200 and len(log.events) == 1 and log.events[0]['outcome'] == 'pass'
        assert len(service._store.audit_all()) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_request_shape_precedes_pmo_even_if_table_unavailable(service):
    service._roles_provider = lambda: None
    with pytest.raises(ApiError) as exc:
        _resolve_query('role=PMO')
    assert exc.value.status == 400 and exc.value.extra['missing'] == ['task_length']
    for role in ('PMO','PM'):
        with pytest.raises(ApiError) as exc:
            service.resolve_role(_resolve_query(f'role={role}&task_length=short'))
        assert exc.value.code == 'role_not_selectable'

@pytest.mark.parametrize('mode', ['off','warn','enforce'])
def test_http_local_login_no_key_access(service, mode):
    import threading
    from unittest.mock import patch
    from urllib.parse import urlencode
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    slot = next(s for s in service._roles_provider().roles['arch'] if s.agent == 'codex')
    entry = PrincipalEntry(frozenset({'resolve'}), ('codex',), {'arch':('codex',)})
    class Gate:
        def __init__(self): self.mode=mode
        def authenticate(self, **kwargs): return auth.passed('test',entry)
    class Log:
        def __init__(self): self.events=[]
        def record(self, **kwargs): self.events.append(kwargs)
    log=Log(); server=make_api_server(service,'127.0.0.1',0,gate=Gate(),auth_log=log)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01}); thread.start()
    try:
        target='/v1/resolve?'+urlencode({'role':'arch','model':slot.model,'task_length':'short'})
        with patch.object(service._keys, 'get', wraps=service._keys.get) as get, patch.object(service._keys, 'deployed', wraps=service._keys.deployed) as deployed:
            status, _, _ = parse_response(exchange(server.server_address[1],req('GET',target)))
        assert status == 200 and get.call_count == 0
        assert all(c.args[0] != slot.subscription for c in deployed.call_args_list)
        assert len(log.events) == (0 if mode == 'off' else 1)
        assert len(service._store.audit_all()) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_real_keystore_call_counts_and_deny(service):
    def reject(agent):
        assert service._keys.deployed_calls
        assert service._keys.get_calls == []
        raise PermissionError()
    q = _resolve_query('role=impl&task_length=short&data=public')
    with pytest.raises(PermissionError): service.resolve_role(q, authz=reject)
    assert service._keys.get_calls == [] and service._store.audit_all() == []
    result = service.resolve_role(q, authz=lambda _: None)
    assert service._keys.get_calls == [result['meta']['subscription']]

@pytest.mark.parametrize('mode,denied,status', [('warn','agent',200),('warn','role',200),('enforce','agent',403),('enforce','role',403)])
def test_http_local_login_denial_never_reads_keys(service, mode, denied, status):
    import threading
    from urllib.parse import urlencode
    from lgwd import auth
    from lgwd.auth_registry import PrincipalEntry
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    slot = next(s for s in service._roles_provider().roles['arch'] if s.agent == 'codex')
    entry = PrincipalEntry(frozenset({'resolve'}), () if denied == 'agent' else ('codex',), {} if denied == 'role' else {'arch':('codex',)})
    class Gate:
        def __init__(self): self.mode=mode
        def authenticate(self, **kwargs): return auth.passed('test',entry)
    class Log:
        def __init__(self): self.events=[]
        def record(self, **kwargs): self.events.append(kwargs)
    log=Log(); server=make_api_server(service,'127.0.0.1',0,gate=Gate(),auth_log=log)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01}); thread.start()
    try:
        target='/v1/resolve?'+urlencode({'role':'arch','model':slot.model,'task_length':'short'})
        actual, _, _ = parse_response(exchange(server.server_address[1],req('GET',target)))
        assert actual == status and service._keys.get_calls == []
        assert slot.subscription not in service._keys.deployed_calls
        assert len(log.events) == 1 and log.events[0]['reason'] == 'forbidden_'+denied
        assert len(service._store.audit_all()) == (1 if status == 200 else 0)
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_legacy_http_differential_status_and_shape(service):
    import threading
    from urllib.parse import urlsplit
    from lgwd.http import make_api_server, _query, _RESOLVE_PARAMS, _ROLES_PARAMS
    from tests.test_http_framing import req, exchange, parse_response
    server=make_api_server(service,'127.0.0.1',0)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01}); thread.start()
    def shape(value):
        if isinstance(value,dict): return {key:shape(val) for key,val in value.items()}
        if isinstance(value,list): return [shape(value[0])] if value else []
        return type(value).__name__
    corpus = ['/v1/resolve?agent=opencode&provider=opencode-zen',
              '/v1/resolve?agent=opencode&provider=opencode-zen&ref=x&task_length=long',
              '/v1/resolve?role=&agent=opencode&provider=opencode-zen',
              '/v1/resolve?agent=opencode&provider=opencode-zen&exclude=a&exclude=b',
              '/v1/roles/impl?agent=opencode&role=ignored&ref=x&task_length=short&exclude_family=a']
    corpus += [f'/v1/roles/impl?{key}=one&{key}=two' for key in _ROLES_PARAMS]
    try:
        for target in corpus:
            split=urlsplit(target)
            try:
                if split.path == '/v1/resolve':
                    q=_query(split.query,_RESOLVE_PARAMS)
                    expected=service.resolve(q.get('agent'),q.get('provider'),model=q.get('model'),subscription=q.get('subscription'),exclude=q.get('exclude'),data=q.get('data'))
                else:
                    q=_query(split.query,_ROLES_PARAMS)
                    expected=service.role_candidates('impl',q.get('agent'),q.get('data'))
                expected_status=200
            except ApiError as exc:
                expected_status,expected=exc.status,exc.body()
            status, raw, _=parse_response(exchange(server.server_address[1],req('GET',target)))
            assert (status,shape(json.loads(raw))) == (expected_status,shape(expected)), target
    finally:
        server.shutdown(); server.server_close(); thread.join()


def _impl_slot(service, subscription):
    return next(s for s in service._roles_provider().roles['impl'] if s.subscription == subscription)

def _item(skipped, slot_number):
    return next(x for x in skipped if x['slot'] == slot_number)

def test_until_equal_now_does_not_block(service):
    slot = service._roles_provider().roles['impl'][0]
    service.register_cooldown(slot.subscription, {'until': '2026-10-11T00:00:00Z'})
    service.register_cooldown(slot.subscription, {'until': '2026-10-11T00:00:00Z', 'model': slot.model})
    result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    assert result['meta']['slot'] == 1 and result['meta']['skipped'] == []

def test_privacy_reason_on_success_path(service):
    result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=general'))
    assert result['meta']['skipped'][0]['reason'] == 'privacy_not_allowed'
    assert result['meta']['skipped'][0]['subscription'] == 'opencode-zen'
    assert result['meta']['slot'] > 1

def test_data_omitted_is_real(service):
    result = service.resolve_role(_resolve_query('role=impl&task_length=short'))
    assert result['meta']['data'] is None and result['meta']['data_effective'] == 'real'
    assert any(x['reason'] == 'privacy_not_allowed' and x['subscription'] == 'opencode-zen' for x in result['meta']['skipped'])

def test_privacy_precedes_short_cooling_and_lift_is_kept(service):
    slots = service._roles_provider().roles['impl']
    lift = '2026-10-11T01:00:00Z'
    for sub in {s.subscription for s in slots}:
        service.register_cooldown(sub, {'until': lift})
    with pytest.raises(ApiError) as exc:
        service.resolve_role(_resolve_query('role=impl&task_length=long&data=general'))
    assert exc.value.code == 'cooling' and exc.value.extra['until'] == lift
    for number, slot in enumerate(slots, 1):
        item = _item(exc.value.extra['skipped'], number)
        if service._policy.subscriptions[slot.subscription].privacy == 'public':
            assert item['reason'] == 'privacy_not_allowed' and item['until'] == lift
        elif slot.short_only:
            assert item['reason'] == 'short_only' and item['until'] == lift
        else:
            assert item['reason'] == 'subscription_cooling'

def test_short_only_precedes_excluded(service):
    slots = service._roles_provider().roles['impl']
    number, slot = next((n, s) for n, s in enumerate(slots, 1) if s.short_only)
    result = service.resolve_role(_resolve_query(f'role=impl&task_length=long&data=public&exclude={slot.subscription}'))
    assert _item(result['meta']['skipped'], number)['reason'] == 'short_only'

def test_not_deployed_precedes_cooling(service):
    from unittest.mock import patch
    service.register_cooldown('opencode-zen', {'until': '2026-10-11T01:00:00Z'})
    with patch.object(service._keys, 'deployed', return_value=False):
        result = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    first = result['meta']['skipped'][0]
    assert (first['subscription'], first['reason'], first['until']) == ('opencode-zen', 'not_deployed', '2026-10-11T01:00:00Z')

def test_cooling_beats_usage_exhausted(service):
    from unittest.mock import patch
    service.register_cooldown('opencode-zen', {'until': '2026-10-11T01:00:00Z'})
    usage = {'status': 'ok', 'session': {'remaining_percent': 0, 'resets_at': '2026-10-11T05:00:00Z'}}
    with patch.object(service._usage, 'usage_for', return_value=usage):
        with pytest.raises(ApiError) as exc:
            service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))
    first = exc.value.extra['skipped'][0]
    assert first['reason'] == 'subscription_cooling' and first['until'] == '2026-10-11T05:00:00Z'

def test_usage_fail_open_status_not_ok(service):
    from unittest.mock import patch
    usage = {'status': 'unknown', 'session': {'remaining_percent': 0, 'resets_at': '2026-10-11T05:00:00Z'}}
    with patch.object(service._usage, 'usage_for', return_value=usage):
        assert service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))['meta']['slot'] == 1

def test_role_meta_has_unscored_and_table_fields(service):
    meta = service.resolve_role(_resolve_query('role=impl&task_length=short&data=public'))['meta']
    for key in ('role', 'slot', 'effort', 'tier', 'family', 'short_only', 'unscored', 'data_effective', 'task_length', 'table_sha', 'skipped'):
        assert key in meta
    assert isinstance(meta['unscored'], bool) and isinstance(meta['short_only'], bool)

def test_role_not_selectable_over_http_and_roles_pm_still_200(service):
    import threading
    from lgwd.http import make_api_server
    from tests.test_http_framing import req, exchange, parse_response
    server = make_api_server(service, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01}); thread.start()
    try:
        port = server.server_address[1]
        status, raw, _ = parse_response(exchange(port, req('GET', '/v1/resolve?role=PMO&task_length=short')))
        assert (status, json.loads(raw)['error']) == (400, 'role_not_selectable')
        status, _, _ = parse_response(exchange(port, req('GET', '/v1/roles/PMO?agent=claude-code')))
        assert status == 200
        status, _, _ = parse_response(exchange(port, req('GET', '/v1/roles/PM?agent=claude-code')))
        assert status == 200
        status, _, _ = parse_response(exchange(port, req('GET', '/v1/roles/arch?agent=codex')))
        assert status == 200
    finally:
        server.shutdown(); server.server_close(); thread.join()

def test_authorize_role_slot_reasons():
    from lgwd.auth_authz import authorize_role_slot
    from lgwd.auth_registry import PrincipalEntry
    entry = PrincipalEntry(frozenset({'resolve'}), ('opencode',), {'impl': ('opencode',)})
    assert authorize_role_slot(entry, 'impl', 'opencode') is None
    assert authorize_role_slot(entry, 'impl', 'codex') == 'forbidden_agent'
    assert authorize_role_slot(entry, 'qa', 'opencode') == 'forbidden_role'
    assert authorize_role_slot(PrincipalEntry(frozenset({'resolve'}), ('opencode',), {'qa': ('opencode',)}), 'impl', 'opencode') == 'forbidden_role'
