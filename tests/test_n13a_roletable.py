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
