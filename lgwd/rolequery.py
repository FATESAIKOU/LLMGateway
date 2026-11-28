"""Query parsing for role-based resolution."""
import re
from urllib.parse import parse_qs

from .errors import bad_request
from .policy import AGENTS, PRIVACY_LEVELS


def resolve_query(query, legacy, known):
    groups = parse_qs(query, keep_blank_values=True)
    roles = groups.get('role', [])
    if len(roles) > 1:
        raise bad_request()
    if not roles or not roles[0]:
        return legacy(query, known)
    singles = ('role', 'model', 'data', 'task_length', 'ref', 'agent', 'provider', 'subscription')
    if any(len(groups.get(k, [])) > 1 for k in singles):
        raise bad_request()
    if any(k in groups for k in ('agent', 'provider', 'subscription')):
        raise bad_request()
    q = {k: groups[k][0] for k in singles if k in groups}
    if 'task_length' not in q:
        raise bad_request(missing=['task_length'])
    if q['task_length'] not in ('short', 'long') or ('data' in q and q['data'] not in PRIVACY_LEVELS):
        raise bad_request()
    if 'ref' in q and not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', q['ref']):
        raise bad_request()
    if 'model' in q:
        m = q['model']
        if not m or len(m) > 200 or any(c in m for c in '*?[') or any(m.startswith(a + '/') for a in AGENTS):
            raise bad_request()
    for k in ('exclude', 'exclude_family'):
        values = [x for v in groups.get(k, []) for x in v.split(',') if x]
        if k == 'exclude_family' and any(not re.fullmatch(r'[a-z0-9_-]+', x) for x in values):
            raise bad_request()
        q[k] = list(dict.fromkeys(values))
    return q
