import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from lgwd import main, roletable
from lgwd.policy import load_policy
from lgwd.rolesync import RoleSyncTimer
from lgwctl.check import _check_rolesync
from lgwctl.layout import Layout
from lgwctl.report import Report

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT/'policy/role-rotation.default.json').read_bytes()
POLICY = load_policy(str(ROOT/'policy/default.json'))

def layout(tmp_path):
    l=Layout.from_env({'LLMGW_PREFIX':str(tmp_path)})
    Path(l.current_link, 'policy').mkdir(parents=True)
    Path(l.state_dir).mkdir(parents=True)
    shutil.copy(ROOT/'policy/default.json', Path(l.current_link,'policy/default.json'))
    Path(l.current_link,'policy/role-rotation.default.json').write_bytes(RAW)
    return l

def check(l):
    r=Report(out=lambda _:None); _check_rolesync(l,r); return r.level_of('rolesync')

def test_check_order(tmp_path):
    l=layout(tmp_path)
    assert check(l)=='todo'
    Path(l.rolesync_status_file).write_text(json.dumps({'ts':time.time(),'ok':False,'ever_ok':False,'reason':'gh_auth'}))
    assert check(l)=='todo'
    Path(l.role_table_file).write_bytes(b'bad')
    assert check(l)=='fail'
    Path(l.role_table_file).write_bytes(RAW)
    Path(l.rolesync_status_file).write_text(json.dumps({'ts':time.time(),'ok':False,'ever_ok':True,'reason':'gh_auth'}))
    assert check(l)=='fail'
    Path(l.rolesync_status_file).write_text(json.dumps({'ts':time.time(),'ok':True,'ever_ok':True,'reason':'ok'}))
    assert check(l)=='ok'
    Path(l.role_table_file).unlink()
    assert check(l)=='fail'
    Path(l.current_link,'policy/role-rotation.default.json').unlink()
    Path(l.rolesync_status_file).unlink()
    assert check(l)=='fail'

@pytest.mark.parametrize('value', ['bad', '[]', '{"ok":1}', json.dumps({'ts':True,'ok':True,'ever_ok':True,'reason':'ok'}), json.dumps({'ts':time.time()+300,'ok':True,'ever_ok':True,'reason':'ok'})])
def test_bad_status(tmp_path,value):
    l=layout(tmp_path); Path(l.rolesync_status_file).write_text(value); assert check(l)=='fail'

def test_reload_retries_bad_same_stamp_and_success_does_not_retry(tmp_path):
    p=tmp_path/'roles'; p.write_bytes(RAW)
    g=object.__new__(main.Gateway); import threading
    g._roles_path=str(p); g._role_policy=POLICY; g._roles_lock=threading.Lock(); g._roles=roletable.parse(RAW,POLICY); g._roles_stamp=main._stat_stamp(str(p))
    p.write_bytes(b'bad'); old=g._roles
    with patch.object(roletable,'load_valid', wraps=roletable.load_valid) as load:
        assert not g.reload_roles_if_changed(); assert not g.reload_roles_if_changed(); assert load.call_count==2; assert g._roles is old
        replacement=tmp_path/'new'; replacement.write_bytes(RAW); replacement.replace(p)
        assert g.reload_roles_if_changed(); assert not g.reload_roles_if_changed(); assert load.call_count==3

def test_role_sync_fake_gh_and_failed_retains_files(tmp_path):
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    gh=tmp_path/'gh'; response=json.dumps({'content':base64.b64encode(RAW).decode(),'sha':'testsha'})
    gh.write_text('#!/bin/sh\nprintf %s '+repr(response)+'\n'); gh.chmod(0o755)
    env=dict(os.environ, LLMGW_PREFIX=str(tmp_path), LGWSYNC_GH=str(gh))
    p=subprocess.run([str(ROOT/'ops-scripts/role-sync')],env=env)
    assert p.returncode==0
    path=d/'role-rotation.json'; sha=d/'.rolesync.sha'; assert path.read_bytes()==RAW; assert sha.read_text()=='testsha\n'
    assert path.stat().st_mode & 0o777 == 0o644 and sha.stat().st_mode & 0o777 == 0o600
    gh.unlink(); p=subprocess.run([str(ROOT/'ops-scripts/role-sync')],env=env)
    assert p.returncode != 0 and path.read_bytes()==RAW and sha.read_text()=='testsha\n'
    status=json.loads((d/'.rolesync.status').read_text()); assert status['reason']=='gh_missing' and status['ever_ok'] and not status['ok']

def test_timer_timeout(tmp_path):
    script=tmp_path/'slow'; script.write_text('#!/bin/sh\nexec sleep 5\n'); script.chmod(0o755)
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    (d/'.rolesync.status').write_text(json.dumps({'ever_ok':True}))
    timer=RoleSyncTimer(str(script), timeout=.05, env=dict(os.environ, LLMGW_PREFIX=str(tmp_path))); assert not timer.run_once()
    status=json.loads((d/'.rolesync.status').read_text()); assert status['reason']=='timeout' and status['ever_ok'] and not status['ok']

def test_gateway_bad_table_fallback_and_none(tmp_path, monkeypatch):
    from tests.test_main import key_text, FAKE_KEYS
    from tests.usage_fakes import refuse_http
    from lgwd.providers import UsageDeps
    monkeypatch.setenv('LLMGW_PREFIX', str(tmp_path))
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True); (d/'role-rotation.json').write_bytes(b'bad')
    policy_dir=tmp_path/'policy'; policy_dir.mkdir(); shutil.copy(ROOT/'policy/default.json',policy_dir/'default.json'); (policy_dir/'role-rotation.default.json').write_bytes(RAW)
    keys=tmp_path/'keys'; keys.write_text(key_text(FAKE_KEYS))
    cfg=main.Config(db=str(tmp_path/'db'), keys_path=str(keys), policy_path=str(policy_dir/'default.json'), api_port=0, keysync_interval=0)
    for fallback in (True,False):
        if not fallback: (policy_dir/'role-rotation.default.json').unlink()
        gw=main.Gateway(cfg,usage_deps=UsageDeps(http_get=refuse_http))
        assert (gw._service._roles_provider() is not None) == fallback
        gw.start(); gw.stop()

def test_upgrade_dry_run(tmp_path):
    from lgwctl.upgrade import _dry_run_policy
    l=layout(tmp_path)
    # Target tag imports its own loader.
    tag=tmp_path/'tag'; (tag/'policy').mkdir(parents=True)
    shutil.copytree(ROOT/'lgwd',tag/'lgwd',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy(ROOT/'policy/default.json',tag/'policy/default.json')
    (tag/'policy/role-rotation.default.json').write_bytes(RAW)
    r=Report(out=lambda _:None); assert _dry_run_policy(l,str(tag),r)
    Path(l.role_table_file).write_bytes(b'bad')
    r=Report(out=lambda _:None); assert not _dry_run_policy(l,str(tag),r)
    Path(l.role_table_file).unlink(); (tag/'policy/role-rotation.default.json').write_bytes(b'bad')
    r=Report(out=lambda _:None); assert not _dry_run_policy(l,str(tag),r)

def test_provider_atomic_concurrent_reload(tmp_path):
    import threading
    p=tmp_path/'roles'; p.write_bytes(RAW)
    other=json.loads(RAW); other['roles']['impl'][0]['tier']='normal'; raw_b=json.dumps(other).encode()
    a=roletable.parse(RAW,POLICY); b=roletable.parse(raw_b,POLICY)
    g=object.__new__(main.Gateway); g._roles_path=str(p); g._role_policy=POLICY; g._roles_lock=threading.Lock(); g._roles=a; g._roles_stamp=main._stat_stamp(str(p))
    seen=[]
    def reader():
        for _ in range(80):
            g.reload_roles_if_changed(); seen.append(g._roles.table_sha)
    threads=[threading.Thread(target=reader) for _ in range(3)]
    for t in threads: t.start()
    for data in [raw_b,b'invalid',RAW]*8:
        candidate=tmp_path/'new'; candidate.write_bytes(data); candidate.replace(p); g.reload_roles_if_changed()
    for t in threads: t.join()
    assert seen and set(seen) <= {a.table_sha,b.table_sha}

def test_concurrent_same_stamp_only_one_load(tmp_path):
    import threading
    p=tmp_path/'roles'; p.write_bytes(RAW)
    g=object.__new__(main.Gateway); g._roles_path=str(p); g._role_policy=POLICY; g._roles_lock=threading.Lock(); g._roles=None; g._roles_stamp=None
    barrier=threading.Barrier(3)
    def reload(): barrier.wait(); g.reload_roles_if_changed()
    with patch.object(roletable,'load_valid', wraps=roletable.load_valid) as load:
        threads=[threading.Thread(target=reload) for _ in range(2)]
        for t in threads: t.start()
        barrier.wait()
        for t in threads: t.join()
        assert load.call_count==1

def test_gh_auth_and_invalid_response_keep_existing(tmp_path):
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    (d/'role-rotation.json').write_bytes(RAW); (d/'.rolesync.sha').write_text('old\n')
    (d/'.rolesync.status').write_text(json.dumps({'ts':time.time(),'ok':True,'ever_ok':True,'reason':'ok'}))
    gh=tmp_path/'gh'; env=dict(os.environ, LLMGW_PREFIX=str(tmp_path), LGWSYNC_GH=str(gh))
    for body,reason in [('#!/bin/sh\necho auth >&2\nexit 1\n','gh_auth'),('#!/bin/sh\necho invalid\n','validation_failed')]:
        gh.write_text(body); gh.chmod(0o755)
        assert subprocess.run([str(ROOT/'ops-scripts/role-sync')],env=env).returncode != 0
        assert (d/'role-rotation.json').read_bytes()==RAW and (d/'.rolesync.sha').read_text()=='old\n'
        status=json.loads((d/'.rolesync.status').read_text()); assert status['reason']==reason and status['ever_ok'] and not status['ok']

def test_registry_optional_provider(tmp_path):
    from lgwd.registry import RegistryService
    from lgwd.clock import FakeClock
    fake=SimpleNamespace()
    default=RegistryService(str(tmp_path/'one'),POLICY,fake,FakeClock())
    provider=lambda: None
    given=RegistryService(str(tmp_path/'two'),POLICY,fake,FakeClock(),roles_provider=provider)
    assert default._roles_provider is None and given._roles_provider is provider
    default.close(); given.close()

def test_startup_order(tmp_path, monkeypatch):
    from tests.test_main import key_text, FAKE_KEYS
    from tests.usage_fakes import refuse_http
    from lgwd.providers import UsageDeps
    monkeypatch.setenv('LLMGW_PREFIX',str(tmp_path))
    policy_dir=tmp_path/'policy'; policy_dir.mkdir(); shutil.copy(ROOT/'policy/default.json',policy_dir/'default.json')
    (policy_dir/'role-rotation.default.json').write_bytes(RAW)
    keys=tmp_path/'keys'; keys.write_text(key_text(FAKE_KEYS))
    script=tmp_path/'opt/llmgateway/current/ops-scripts/role-sync'; script.parent.mkdir(parents=True); script.write_text('#!/bin/sh\nexit 0\n'); script.chmod(0o755)
    cfg=main.Config(db=str(tmp_path/'db'), keys_path=str(keys), policy_path=str(policy_dir/'default.json'), api_port=0, keysync_interval=0)
    events=[]
    def wrapper(name, original):
        def call(*args,**kwargs):
            events.append(name); return original(*args,**kwargs)
        return call
    class Timer:
        def __init__(self,*a,**kw): pass
        def start(self): events.append('timer')
        def stop(self): pass
    with patch.object(main,'load_policy',wrapper('policy',main.load_policy)), patch.object(main,'KeyStore',wrapper('keys',main.KeyStore)), patch.object(main.Gateway,'_make_usage',wrapper('usage',main.Gateway._make_usage)), patch.object(roletable,'load_valid',wrapper('roles',roletable.load_valid)), patch.object(main,'RegistryService',wrapper('registry',main.RegistryService)), patch.object(main,'make_api_server',wrapper('api',main.make_api_server)), patch.object(main,'RoleSyncTimer',Timer):
        gw=main.Gateway(cfg,usage_deps=UsageDeps(http_get=refuse_http)); gw.start(); gw.stop()
    assert events.index('policy') < events.index('keys') < events.index('usage') < events.index('roles') < events.index('registry') < events.index('api') < events.index('timer')

def test_self_heal_same_sha(tmp_path):
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    (d/'role-rotation.json').write_bytes(b'bad'); (d/'.rolesync.sha').write_text('same\n')
    response=json.dumps({'content':base64.b64encode(RAW).decode(),'sha':'same'})
    gh=tmp_path/'gh'; gh.write_text('#!/bin/sh\nprintf %s '+repr(response)+'\n'); gh.chmod(0o755)
    env=dict(os.environ, LLMGW_PREFIX=str(tmp_path), LGWSYNC_GH=str(gh))
    assert subprocess.run([str(ROOT/'ops-scripts/role-sync')],env=env).returncode==0
    assert (d/'role-rotation.json').read_bytes()==RAW

@pytest.mark.parametrize('local_good,tag_good', [(False,False),(True,False),(True,True)])
def test_gateway_tag_and_local_combinations(tmp_path, monkeypatch, local_good, tag_good):
    from tests.test_main import key_text, FAKE_KEYS
    from tests.usage_fakes import refuse_http
    from lgwd.providers import UsageDeps
    monkeypatch.setenv('LLMGW_PREFIX',str(tmp_path))
    directory=tmp_path/'var/lib/llmgateway'; directory.mkdir(parents=True)
    alt=json.loads(RAW); alt['roles']['impl'][0]['tier']='normal'; local_raw=json.dumps(alt).encode()
    (directory/'role-rotation.json').write_bytes(local_raw if local_good else b'invalid')
    policy_dir=tmp_path/'policy'; policy_dir.mkdir(); shutil.copy(ROOT/'policy/default.json',policy_dir/'default.json')
    (policy_dir/'role-rotation.default.json').write_bytes(RAW if tag_good else b'invalid')
    keys=tmp_path/'keys'; keys.write_text(key_text(FAKE_KEYS))
    cfg=main.Config(db=str(tmp_path/'db'), keys_path=str(keys), policy_path=str(policy_dir/'default.json'), api_port=0, keysync_interval=0)
    gw=main.Gateway(cfg,usage_deps=UsageDeps(http_get=refuse_http)); gw.start()
    try:
        selected=gw._service._roles_provider()
        assert (selected.table_sha if selected else None) == (roletable.parse(local_raw,POLICY).table_sha if local_good else None)
    finally: gw.stop()

def test_restart_new_policy_rejects_old_local_table(tmp_path, monkeypatch):
    from tests.test_main import key_text, FAKE_KEYS
    from tests.usage_fakes import refuse_http
    from lgwd.providers import UsageDeps
    monkeypatch.setenv('LLMGW_PREFIX',str(tmp_path))
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    local=json.loads(RAW); local['roles']['impl'][0]['model']='temporary-model-free'
    old=json.loads((ROOT/'policy/default.json').read_text()); new=json.loads(json.dumps(old)); new['subscriptions']['opencode-zen']['models']['allow'] = sorted({slot['model'] for slots in json.loads(RAW)['roles'].values() for slot in slots if slot['subscription']=='opencode-zen'} | {'big-pickle'})
    policy_dir=tmp_path/'policy'; policy_dir.mkdir(); path=policy_dir/'default.json'; path.write_text(json.dumps(old)); (policy_dir/'role-rotation.default.json').write_bytes(RAW)
    (d/'role-rotation.json').write_text(json.dumps(local)); keys=tmp_path/'keys'; keys.write_text(key_text(FAKE_KEYS))
    cfg=main.Config(db=str(tmp_path/'db'),keys_path=str(keys),policy_path=str(path),api_port=0,keysync_interval=0)
    gw=main.Gateway(cfg,usage_deps=UsageDeps(http_get=refuse_http)); gw.start()
    try:
        assert gw._service._roles_provider().roles['impl'][0].model=='temporary-model-free'
    finally: gw.stop()
    path.write_text(json.dumps(new))
    gw=main.Gateway(cfg,usage_deps=UsageDeps(http_get=refuse_http)); gw.start()
    try:
        assert gw._service._roles_provider().table_sha==roletable.parse(RAW,POLICY).table_sha
    finally: gw.stop()

def test_timer_kills_and_reaps_its_fake_gh_without_harming_other_process(tmp_path):
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    gh=tmp_path/'gh'; pidfile=tmp_path/'gh.pid'
    gh.write_text('#!/bin/sh\necho $$ > "$GH_PID_FILE"\nexec sleep 30\n'); gh.chmod(0o755)
    env=dict(os.environ,LLMGW_PREFIX=str(tmp_path),LGWSYNC_GH=str(gh),GH_PID_FILE=str(pidfile))
    other=subprocess.Popen(['sleep','30'])
    try:
        timer=RoleSyncTimer(str(ROOT/'ops-scripts/role-sync'),timeout=.8,env=env)
        assert not timer.run_once()
        assert pidfile.exists()
        gh_pid=int(pidfile.read_text())
        with pytest.raises(ProcessLookupError): os.kill(gh_pid,0)
        assert other.poll() is None
        assert json.loads((d/'.rolesync.status').read_text())['reason']=='timeout'
    finally:
        if pidfile.exists():
            try: os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError: pass
        other.terminate(); other.wait(timeout=5)

def test_timer_retries_automatically_on_next_interval(tmp_path):
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    gh=tmp_path/'gh'; calls=tmp_path/'calls'; response=tmp_path/'response'
    response.write_text(json.dumps({'content':base64.b64encode(RAW).decode(),'sha':'retrysha'}))
    gh.write_text('#!/bin/sh\nif [ ! -e "$GH_CALLS" ]; then touch "$GH_CALLS"; exit 1; fi\ncat "$GH_RESPONSE"\n'); gh.chmod(0o755)
    env=dict(os.environ,LLMGW_PREFIX=str(tmp_path),LGWSYNC_GH=str(gh),GH_CALLS=str(calls),GH_RESPONSE=str(response))
    timer=RoleSyncTimer(str(ROOT/'ops-scripts/role-sync'),interval=.1,timeout=3,env=env)
    timer.start()
    try:
        deadline=time.monotonic()+8
        def done():
            # role-sync writes .rolesync.sha before .rolesync.status; wait for the status of the retry.
            try: return (d/'.rolesync.sha').read_text()=='retrysha\n' and json.loads((d/'.rolesync.status').read_text())['ok'] is True
            except (OSError, ValueError, KeyError, TypeError): return False
        while time.monotonic()<deadline and not done(): time.sleep(.03)
        assert done(), f"retry did not succeed before deadline; status={(d/'.rolesync.status').read_text() if (d/'.rolesync.status').exists() else None}"
        assert calls.exists() and (d/'.rolesync.sha').read_text()=='retrysha\n'
        assert json.loads((d/'.rolesync.status').read_text())['ok'] is True
    finally: timer.stop()

def test_timer_stop_sigterms_its_child_and_lets_it_clean_up(tmp_path):
    d=tmp_path/'var/lib/llmgateway'; d.mkdir(parents=True)
    script=tmp_path/'fake-role-sync'; ready=tmp_path/'ready'; mark=tmp_path/'term'
    script.write_text('#!/bin/sh\ntrap \'echo term > "$MARK"; exit 0\' TERM\necho ready > "$READY"\nwhile :; do sleep 0.1; done\n'); script.chmod(0o755)
    env=dict(os.environ,LLMGW_PREFIX=str(tmp_path),MARK=str(mark),READY=str(ready))
    timer=RoleSyncTimer(str(script),interval=60,timeout=30,env=env)
    timer.start()
    try:
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and not ready.exists(): time.sleep(.03)
        assert ready.exists(), 'fake role-sync never installed its SIGTERM trap'
    finally: timer.stop()
    assert mark.exists() and mark.read_text()=='term\n'
