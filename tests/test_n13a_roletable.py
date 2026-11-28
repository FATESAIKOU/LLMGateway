import copy
import hashlib
import json
from pathlib import Path
import pytest
from lgwd.policy import load_policy
from lgwd.roletable import parse, RoleTableError, load_valid

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(str(ROOT / 'policy/default.json'))
RAW = (ROOT / 'policy/role-rotation.default.json').read_bytes()

def test_appendix_and_sha():
    table = parse(RAW, POLICY)
    assert {k:len(v) for k,v in table.roles.items()} == {'impl':15,'qa':14,'rev':6,'arch':4}
    assert table.table_sha == hashlib.sha256(RAW).hexdigest()[:12]

@pytest.mark.parametrize('change', ['extra_role','family','tier','wildcard','prefix','effort','order','duplicate','flag','schema'])
def test_invalid_table(change):
    obj = json.loads(RAW)
    slot = obj['roles']['impl'][0]
    if change == 'extra_role': obj['roles']['PM'] = [slot]
    if change == 'family': slot['family'] = 'BAD'
    if change == 'tier': slot['tier'] = 'other'
    if change == 'wildcard': slot['model'] = '*'
    if change == 'prefix': slot['model'] = 'opencode/test'
    if change == 'effort': obj['roles']['arch'][0]['effort'] = 'high'
    if change == 'order': obj['roles']['rev'][0]['subscription'] = 'opencode-go'
    if change == 'duplicate': obj['roles']['impl'].append(copy.deepcopy(slot))
    if change == 'flag': slot['unscored'] = 1
    if change == 'schema': obj['schema'] = True
    with pytest.raises(RoleTableError): parse(json.dumps(obj).encode(), POLICY)

def test_bad_json_and_size(tmp_path):
    p = tmp_path / 'table'
    for raw in (b'{"schema":1,"schema":1}', b'NaN', b'['*2000 + b']'*2000, b'x'*1048577):
        p.write_bytes(raw)
        assert load_valid(p, POLICY) is None


# ---- M-3（#347 審計）：每條拒絕規則各一個轉紅測試 -----------------------------
# parse() 把內部的 RoleTableError 包成 'invalid role table'；原因在 __cause__。
# 斷言原因字串，才知道是哪一條規則擋下；只看 raises 會被別的規則誤擋而騙過去。

import dataclasses


def _reason(raw, policy=POLICY):
    with pytest.raises(RoleTableError) as info:
        parse(raw, policy)
    return str(info.value.__cause__)


def _obj():
    return json.loads(RAW)


def _too_large(raw):
    # 大小檢查在 try 之外，直接拋 RoleTableError('too large')，沒有 __cause__
    with pytest.raises(RoleTableError) as info:
        parse(raw, POLICY)
    return str(info.value)


def test_size_limit_boundary():
    # 1 MiB 整（1048576 bytes）允許；多 1 byte 拒絕（尾端空白仍是合法 JSON，只靠大小擋）
    exact = RAW + b' ' * (1048576 - len(RAW))
    assert len(exact) == 1048576
    assert parse(exact, POLICY).table_sha == hashlib.sha256(exact).hexdigest()[:12]
    assert _too_large(exact + b' ') == 'too large'


def test_size_limit_rejects_valid_json_over_limit():
    raw = RAW + b' ' * (1048577 - len(RAW))
    assert json.loads(raw)  # 合法 JSON：只有大小限制能擋
    assert _too_large(raw) == 'too large'


def test_duplicate_key_top_level_and_slot():
    top = b'{"schema":1,' + RAW.lstrip(b'{')
    assert _reason(top) == 'duplicate key'
    obj = _obj()
    slot_raw = json.dumps(obj).replace('"family": "google"', '"family": "google", "family": "google"', 1).encode()
    assert _reason(slot_raw) == 'duplicate key'


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_constant_rejected_by_hook(token):
    # schema 寫成非有限數：不靠 schema 檢查擋，要由 parse_constant 先擋（原因 nonfinite）
    raw = RAW.replace(b'"schema": 1', ('"schema": ' + token).encode(), 1)
    assert raw != RAW
    assert _reason(raw) == 'nonfinite'


def test_missing_role_rejected():
    obj = _obj(); del obj['roles']['qa']
    assert _reason(json.dumps(obj).encode()) == 'roles'


def test_slot_agent_must_be_in_subscription_agents():
    obj = _obj()
    slot = obj['roles']['impl'][0]          # opencode-zen 只允許 opencode
    assert slot['subscription'] == 'opencode-zen'
    slot['agent'] = 'claude-code'
    assert _reason(json.dumps(obj).encode()) == 'policy'


def test_privacy_floor_rejects_public_subscription_below_role_floor():
    rp = POLICY.roles['impl']
    strict = dataclasses.replace(POLICY, roles={**POLICY.roles, 'impl': dataclasses.replace(rp, floor='real')})
    assert _reason(RAW, strict) == 'policy'
    # 對照：預設 floor（public）時同一張表合法
    assert parse(RAW, POLICY).roles['impl']


def test_empty_slot_list_rejected():
    obj = _obj(); obj['roles']['rev'] = []
    assert _reason(json.dumps(obj).encode()) == 'slots'


@pytest.mark.parametrize('length,ok', [(200, True), (201, False)])
def test_model_length_boundary(length, ok):
    obj = _obj()
    slot = obj['roles']['impl'][0]          # opencode-zen 允許 "*-free"
    slot['model'] = 'a' * (length - len('-free')) + '-free'
    assert len(slot['model']) == length
    raw = json.dumps(obj).encode()
    if ok:
        parse(raw, POLICY)
    else:
        assert _reason(raw) == 'model'


@pytest.mark.parametrize('effort', ['ultra', 'HIGH', '', 1])
def test_effort_enum_rejected(effort):
    obj = _obj()
    obj['roles']['impl'][0]['effort'] = effort   # opencode 不是 agy，不走 agy 規則
    assert _reason(json.dumps(obj).encode()) == 'effort'


def test_agy_slot_must_not_carry_effort():
    obj = _obj()
    agy = next(s for s in obj['roles']['arch'] if s['agent'] == 'agy')
    agy['effort'] = 'low'
    assert _reason(json.dumps(obj).encode()) == 'effort'


def test_unknown_slot_key_rejected():
    obj = _obj(); obj['roles']['impl'][0]['extra'] = 1
    assert _reason(json.dumps(obj).encode()) == 'slot keys'
