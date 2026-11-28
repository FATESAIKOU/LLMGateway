"""Validated immutable role rotation tables."""
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from .policy import AGENTS, SELECTABLE_ROLES, PRIVACY_LEVELS

class RoleTableError(ValueError):
    pass

@dataclass(frozen=True)
class RoleSlot:
    agent: str
    subscription: str
    model: str
    effort: str | None
    tier: str
    family: str
    short_only: bool = False
    unscored: bool = False

@dataclass(frozen=True)
class RoleTable:
    roles: object
    table_sha: str

def _object(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise RoleTableError('duplicate key')
        d[k] = v
    return d

def parse(raw, policy):
    if len(raw) > 1048576:
        raise RoleTableError('too large')
    try:
        obj = json.loads(raw, object_pairs_hook=_object, parse_constant=lambda _: (_ for _ in ()).throw(RoleTableError('nonfinite')))
        if not isinstance(obj, dict) or set(obj) != {'schema', 'roles'} or type(obj['schema']) is not int or obj['schema'] != 1:
            raise RoleTableError('schema')
        roles = obj['roles']
        if not isinstance(roles, dict) or set(roles) != set(SELECTABLE_ROLES):
            raise RoleTableError('roles')
        result = {}
        required = {'agent','subscription','model','effort','tier','family'}
        for role, slots in roles.items():
            if not isinstance(slots, list) or not slots:
                raise RoleTableError('slots')
            out, seen = [], set()
            for s in slots:
                if not isinstance(s, dict) or not required <= s.keys() or s.keys() - required - {'short_only','unscored'}:
                    raise RoleTableError('slot keys')
                a, subname, model, effort, tier, family = (s[k] for k in ('agent','subscription','model','effort','tier','family'))
                if not isinstance(a, str) or a not in AGENTS or not isinstance(subname, str) or subname not in policy.subscriptions:
                    raise RoleTableError('agent or subscription')
                if not isinstance(model, str) or not model or len(model)>200 or any(c in model for c in '*?[') or any(model.startswith(x+'/') for x in AGENTS):
                    raise RoleTableError('model')
                if effort is not None and (not isinstance(effort,str) or effort not in ('non-reasoning','low','medium','high','xhigh','max')) or a == 'agy' and effort is not None:
                    raise RoleTableError('effort')
                if not isinstance(tier,str) or tier not in ('free','low','normal') or not isinstance(family,str) or not re.fullmatch(r'[a-z0-9_-]+', family):
                    raise RoleTableError('tier or family')
                if any(type(s.get(k,False)) is not bool for k in ('short_only','unscored')):
                    raise RoleTableError('flags')
                sub, rp = policy.subscriptions[subname], policy.roles[role]
                if a not in sub.agents or not any(fnmatch.fnmatchcase(model,p) for p in sub.models_allow) or PRIVACY_LEVELS.index(sub.privacy) < PRIVACY_LEVELS.index(rp.floor) or subname not in rp.order:
                    raise RoleTableError('policy')
                key=(a,subname,model,effort)
                if key in seen: raise RoleTableError('duplicate slot')
                seen.add(key)
                out.append(RoleSlot(**s))
            result[role] = tuple(out)
        return RoleTable(MappingProxyType(result), hashlib.sha256(raw).hexdigest()[:12])
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise RoleTableError('invalid role table') from exc

def load_valid(path, policy):
    try:
        with open(path, 'rb') as f:
            raw = f.read(1048577)
        return parse(raw, policy)
    except (OSError, RoleTableError):
        return None
