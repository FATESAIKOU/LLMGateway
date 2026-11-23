from lgwctl import cli, units, probe, release
from lgwctl.layout import Layout


def test_start_active_healthy_does_not_restart(monkeypatch):
    monkeypatch.setattr(units, 'unit_state', lambda *a: 'active')
    monkeypatch.setattr(probe, 'local_ok', lambda *a, **k: True)
    monkeypatch.setattr(units, 'start', lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert cli.main(['start']) == 0


def test_stop_inactive_is_idempotent(monkeypatch):
    monkeypatch.setattr(units, 'unit_state', lambda *a: 'inactive')
    monkeypatch.setattr(units, 'stop', lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert cli.main(['stop']) == 0


def test_follow_reset_requires_root(monkeypatch, tmp_path):
    monkeypatch.setattr('os.geteuid', lambda: 1000)
    assert cli.main(['follow-reset'], env={'LLMGW_PREFIX': str(tmp_path)}) != 0


def test_tunnel_files_are_optional_in_release():
    assert 'ops-scripts/llmgateway-tunnel' not in release.REQUIRED_FILES
    assert 'deploy/units/llmgateway-tunnel.service' not in release.REQUIRED_FILES


def test_follow_reset_preserves_watermark_with_fetch_state(monkeypatch, tmp_path):
    monkeypatch.setattr('os.geteuid', lambda: 0)
    state = tmp_path / 'var/lib/llmgateway'
    state.mkdir(parents=True)
    for name in ('.tunnel_generation', '.tunnel_fetch_backoff', '.tunnel_fetch_state', 'gateway.json', '.tunnel_token_meta'):
        (state / name).write_text('test')
    assert cli.main(['follow-reset', '--fetch-state'], env={'LLMGW_PREFIX': str(tmp_path)}) == 0
    assert (state / '.tunnel_generation').exists()
    assert not (state / '.tunnel_fetch_backoff').exists()
    assert not (state / '.tunnel_fetch_state').exists()
    assert all((state / name).exists() for name in ('gateway.json', '.tunnel_token_meta'))
    assert cli.main(['follow-reset'], env={'LLMGW_PREFIX': str(tmp_path)}) == 0
    assert not (state / '.tunnel_generation').exists()
    assert all((state / name).exists() for name in ('gateway.json', '.tunnel_token_meta'))


def test_local_only_unit_install(tmp_path):
    from lgwctl.report import Report
    layout = Layout.from_env({'LLMGW_PREFIX': str(tmp_path)})
    version = tmp_path / 'version'
    source = version / 'deploy/units/llmgateway.service'
    source.parent.mkdir(parents=True)
    source.write_text('service')
    (tmp_path / 'etc/systemd/system').mkdir(parents=True)
    report = Report()
    assert units.install_unit_files(layout, str(version), report)
    assert "llmgateway-tunnel.service" not in " ".join(report.lines)


def test_reset_rejects_symlink_state_directory(monkeypatch, tmp_path):
    monkeypatch.setattr('os.geteuid', lambda: 0)
    target = tmp_path / 'secret'
    target.mkdir()
    (target / '.tunnel_generation').write_text('keep')
    state = tmp_path / 'var/lib/llmgateway'
    state.parent.mkdir(parents=True)
    state.symlink_to(target, target_is_directory=True)
    assert cli.main(['follow-reset'], env={'LLMGW_PREFIX': str(tmp_path)}) != 0
    assert (target / '.tunnel_generation').read_text() == 'keep'


def test_lifecycle_only_changes_service_and_checks_local_health():
    from .lgwctl_fakes import Sandbox
    with Sandbox() as box:
        rc, out, err = box.run_cli(['start'])
        assert rc == 0, out + err
        assert box.actions() == ['start llmgateway.service']
        rc, out, err = box.run_cli(['start'])
        assert rc == 0 and box.actions() == ['start llmgateway.service']
        rc, out, err = box.run_cli(['restart'])
        assert rc == 0 and box.actions()[-1] == 'restart llmgateway.service'
        rc, out, err = box.run_cli(['stop'])
        assert rc == 0 and box.actions()[-1] == 'stop llmgateway.service'
        rc, out, err = box.run_cli(['stop'])
        assert rc == 0 and box.actions().count('stop llmgateway.service') == 1
        assert 'llmgateway-tunnel.service' not in '\n'.join(box.actions())
        box.env['FAKE_CURL_CODE'] = '503'
        assert box.run_cli(['restart'])[0] == 1


def test_reset_unlinks_watermark_symlink_only(monkeypatch, tmp_path):
    monkeypatch.setattr('os.geteuid', lambda: 0)
    state = tmp_path / 'var/lib/llmgateway'
    state.mkdir(parents=True)
    victim = tmp_path / 'victim'
    victim.write_text('keep')
    link = state / '.tunnel_generation'
    link.symlink_to(victim)
    assert cli.main(['follow-reset'], env={'LLMGW_PREFIX': str(tmp_path)}) == 0
    assert not link.is_symlink()
    assert victim.read_text() == 'keep'
