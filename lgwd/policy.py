"""政策檔（design 決定 7；tasks 1.2）。

load_policy(path) 讀 JSON、驗證、回傳 Policy；不合法丟 PolicyError。
政策檔只管 priority、agents、models（與 provider 有關的欄位在 lgwd/providers.py）。
訂閱 id 是固定集合（SUBSCRIPTIONS）；其中 antigravity 是沒有憑證的訂閱（provider 的
kind 為 local-login），只給 agy（#107）。
PolicyError 的訊息只用固定字串與固定的訂閱 id，不回顯檔案裡的其他字串（值永不進例外）。

載入時機：只在啟動時（lgwd 啟動失敗就不啟動）。v5 沒有熱重載、沒有 SIGHUP，
換政策要重啟 lgwd（政策檔跟著 tag 換，見 docs/deploy.md 4.2）。key 檔是另一回事：
那個由 key-sync 換檔後 lgwd 依 mtime 自動重讀。
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field

SUBSCRIPTIONS = ("claude-personal", "claude-company", "opencode-zen", "opencode-go",
                 "groq", "openrouter", "ollama-cloud", "antigravity")
AGENTS = ("claude-code", "opencode", "agy")

# 隱私等級（public < general < real）；缺省 public（fail-closed）。
PRIVACY_LEVELS = ("public", "general", "real")

# 角色鍵（政策檔 roles 段的鍵只能是這五個）。
ROLES = ("PMO", "PM", "impl", "rev", "qa")

# 條款與設計的否定約束（固定字串的錯誤訊息）
_CLAUDE_ONLY = ("claude-personal", "claude-company")
_OPENCODE_ONLY = ("opencode-zen", "opencode-go", "groq")
# 沒有憑證的訂閱（kind local-login，#107）：agents 只能剛好是 ("agy",)，
# 其他訂閱的 agents 也 MUST NOT 含 agy（否則 api-key 的 row 會用空 env 被選中）。
_AGY_ONLY = ("antigravity",)


_OPENROUTER_ALLOW = ("*:free", "openrouter/free")

# groq（#88）：只有**免費層**可用的模型寫進政策檔的 models.allow，不放 "*" 也不放
# 寬樣式。出廠清單（policy/default.json）取自官方 Free Plan 的 rate limits 表
# （https://console.groq.com/docs/rate-limits，2026-10-27 讀）裡可做 chat／coding 的
# 那幾個 id。這條是政策檔的資料面約定，policy 不另外驗 allow 的形狀（與其他
# provider 一致）。
# 免費層的**訓練政策頁查不到**（console.groq.com/docs/your-data 只寫「預設不保留」
# 與 ZDR，沒有 training 字樣；docs/research/free-model-capacity.md §3.6）：因此這把
# 只給 public repo 與假資料的工作，不碰真資料。要改這個限制得先查到官方的訓練條款。


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionPolicy:
    name: str
    priority: int
    agents: tuple[str, ...]
    models_allow: tuple[str, ...]      # fnmatch 樣式，例如 "*" 或 "*:free"
    models_default: str | None
    privacy: str = "public"


@dataclass(frozen=True)
class UsageSetting:
    """政策檔 `usage` 段裡一個 subscription 的設定（#60）；None＝沿用預設。"""
    enabled: bool | None = None
    ttl_seconds: int | None = None


@dataclass(frozen=True)
class RolePolicy:
    """政策檔 `roles` 段裡一個角色的分配（floor、order）。"""
    floor: str
    order: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    subscriptions: dict[str, SubscriptionPolicy]
    until_max_ahead_seconds: int
    usage: dict[str, UsageSetting] = field(default_factory=dict)
    roles: dict[str, RolePolicy] = field(default_factory=dict)

    def in_priority_order(self) -> tuple[SubscriptionPolicy, ...]:
        """依 priority 排序（數字小的先；同分照 id）。list 用。"""
        return tuple(
            sorted(self.subscriptions.values(), key=lambda s: (s.priority, s.name))
        )


def _positive_int(value: object) -> bool:
    # bool 是 int 的子類，要先排除
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_priority(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_privacy(name: str, spec: dict) -> str:
    """共用的 privacy 驗證（訊息只有一份）。缺省 public；非法丟 PolicyError。"""
    privacy = spec.get("privacy", "public")
    if privacy not in PRIVACY_LEVELS:
        raise PolicyError(f"subscription {name} privacy must be one of public, general, real")
    return privacy


def _parse_subscription(name: str, spec: object) -> SubscriptionPolicy:
    if not isinstance(spec, dict):
        raise PolicyError("subscription must be an object")
    priority = spec.get("priority")
    if not _is_priority(priority):
        raise PolicyError("subscription priority must be a non-negative integer")
    privacy = _parse_privacy(name, spec)
    agents = spec.get("agents")
    if not isinstance(agents, list) or not agents or any(a not in AGENTS for a in agents):
        raise PolicyError("subscription agents invalid")
    if len(set(agents)) != len(agents):
        raise PolicyError("subscription agents must not repeat")
    models = spec.get("models")
    if not isinstance(models, dict):
        raise PolicyError("subscription models must be an object")
    allow = models.get("allow")
    if not isinstance(allow, list) or not allow or any(
        not isinstance(p, str) or not p for p in allow
    ):
        raise PolicyError("subscription models.allow must be a non-empty list of strings")
    default = models.get("default")
    if default is not None and (not isinstance(default, str) or not default):
        raise PolicyError("subscription models.default must be a non-empty string or null")
    # fnmatch 的 `*` 會匹配 `/`，`*-free` 會同時涵蓋 zen 與 go；副作用與分開控制做法見 docs/deploy.md 5.8。
    if default is not None and not any(
        fnmatch.fnmatchcase(default, pattern) for pattern in allow
    ):
        # fail-closed：default 蓋不進 allow 的政策檔直接拒收（訊息只帶訂閱 id；
        # 值本身照模組原則不進例外）
        raise PolicyError(
            f"subscription {name} models.default must be covered by models.allow"
        )
    return SubscriptionPolicy(
        name=name,
        priority=priority,
        agents=tuple(agents),
        models_allow=tuple(allow),
        models_default=default,
        privacy=privacy,
    )


_USAGE_KEYS = ("enabled", "ttl_seconds")


def _parse_usage(data: object, subs: dict) -> dict[str, UsageSetting]:
    """頂層 `usage`（選填）：{subscription id: {enabled?, ttl_seconds?}}；fail-closed。

    本身不是 object、鍵不在政策的 subscriptions、值不是 object、有 enabled／ttl_seconds
    以外的鍵（打錯的鍵不能靜默忽略）、enabled 不是布林、ttl_seconds 不是正整數，一律拒收。
    """
    if not isinstance(data, dict):
        raise PolicyError("policy.usage must be an object")
    out: dict[str, UsageSetting] = {}
    for name, spec in data.items():
        if name not in subs:
            raise PolicyError("policy.usage has unknown subscription")
        if not isinstance(spec, dict):
            raise PolicyError("policy.usage entry must be an object")
        if any(key not in _USAGE_KEYS for key in spec):
            raise PolicyError("policy.usage entry has unknown key")
        enabled = spec.get("enabled")
        if "enabled" in spec and not isinstance(enabled, bool):
            raise PolicyError("policy.usage enabled must be a boolean")
        ttl = spec.get("ttl_seconds")
        if "ttl_seconds" in spec and not _positive_int(ttl):
            raise PolicyError("policy.usage ttl_seconds must be a positive integer")
        out[name] = UsageSetting(enabled=enabled, ttl_seconds=ttl)
    return out


_ROLES_KEYS = ("floor", "order")


def _parse_roles(data: object, subs: dict[str, SubscriptionPolicy]) -> dict[str, RolePolicy]:
    """頂層 `roles`（選填）：{role: {floor, order}}；fail-closed。

    驗證順序：角色鍵 → floor → order（privacy／訂閱 id 已在更早的步驟驗過）。
    角色鍵只能是 ROLES；物件只能有 floor／order；floor 必填且合法（無預設）；
    order 非空、不重複、每個 id 存在於 subs。
    """
    if not isinstance(data, dict):
        raise PolicyError("policy.roles must be an object")
    # 第 3 步：角色鍵（先於 floor／order，與 dict 順序無關）
    for role in data:
        if role not in ROLES:
            raise PolicyError("policy.roles has unknown role")
    # entry 必須是 object
    for role, spec in data.items():
        if not isinstance(spec, dict):
            raise PolicyError("policy.roles entry must be an object")
    # 多餘鍵（先於 floor）
    for role, spec in data.items():
        if any(key not in _ROLES_KEYS for key in spec):
            raise PolicyError("policy.roles entry has unknown key")
    # 第 4 步：floor（必填、合法，無預設）
    for role, spec in data.items():
        if "floor" not in spec:
            raise PolicyError("policy.roles floor is required")
        if spec.get("floor") not in PRIVACY_LEVELS:
            raise PolicyError("policy.roles floor must be one of public, general, real")
    # 第 5 步：order
    out: dict[str, RolePolicy] = {}
    for role, spec in data.items():
        if "order" not in spec:
            raise PolicyError("policy.roles order is required")
        order = spec.get("order")
        if not isinstance(order, list) or not order:
            raise PolicyError("policy.roles order must be a non-empty list")
        if any(not isinstance(x, str) or not x for x in order):
            raise PolicyError("policy.roles order must be a list of subscription ids")
        if len(set(order)) != len(order):
            raise PolicyError("policy.roles order must not repeat")
        for rid in order:
            if rid not in subs:
                raise PolicyError("policy.roles order has unknown subscription")
        out[role] = RolePolicy(floor=spec.get("floor"), order=tuple(order))
    return out


def load_policy(path: str) -> Policy:
    """驗證規則（不合法丟 PolicyError，訊息說明哪裡錯、不含檔案字串）：
    - subscriptions 的鍵只能是 SUBSCRIPTIONS；
    - claude-personal／claude-company 的 agents 只能剛好是 ["claude-code"]（條款）；
    - opencode-zen／opencode-go／groq 的 agents 只能剛好是 ["opencode"]（沒有官方的
      Claude Code 接法；groq 的端點是 OpenAI 相容，見 providers.py）；
    - openrouter 的 agents 只能剛好是 ["opencode"]（免費模型只經過 opencode；design 決定 3）；
    - openrouter 的 models.allow 只能是 ["*:free", "openrouter/free"]（只允許免費模型）；
    - antigravity（沒有憑證的訂閱）的 agents 只能剛好是 ["agy"]（條款）；其他訂閱的
      agents 不能含 agy；
    - 非 null 的 models.default 必須被同一筆的 models.allow 樣式涵蓋（fnmatch；fail-closed）；
    - priority 為非負整數；cooldown 的 until 上限為正整數；
    - 選填的頂層 `usage`（#60）：見 _parse_usage，不合法 fail-closed。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise PolicyError("policy file unreadable") from exc
    except ValueError as exc:  # JSON 解析失敗（含 Unicode 錯誤）
        raise PolicyError("policy file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy root must be an object")
    subs = data.get("subscriptions")
    if not isinstance(subs, dict):
        raise PolicyError("policy.subscriptions must be an object")
    # 驗證順序第 1 步：privacy（先於訂閱 id；值永不進例外）
    for name, spec in subs.items():
        if isinstance(spec, dict):
            _parse_privacy(name, spec)
    # 驗證順序第 2 步：訂閱 id
    for name in subs:
        if name not in SUBSCRIPTIONS:
            raise PolicyError("policy.subscriptions has unknown key")
    parsed_subs = {name: _parse_subscription(name, spec) for name, spec in subs.items()}
    for name in _CLAUDE_ONLY:
        if name in parsed_subs and parsed_subs[name].agents != ("claude-code",):
            raise PolicyError("claude subscriptions allow claude-code only")
    for name in _OPENCODE_ONLY:
        if name in parsed_subs and parsed_subs[name].agents != ("opencode",):
            raise PolicyError(f"{name} must only allow opencode")
    for name in _AGY_ONLY:
        if name in parsed_subs and parsed_subs[name].agents != ("agy",):
            raise PolicyError("antigravity must only allow agy")
    for name, sub in parsed_subs.items():
        if name not in _AGY_ONLY and "agy" in sub.agents:
            # 反向的條款：agy 只准出現在沒有憑證的訂閱；不然 api-key 的 row 會用
            # 空 env 被選中（spec「共通 JSON schema」）。
            raise PolicyError("agy is only allowed for antigravity")
    if "openrouter" in parsed_subs and parsed_subs["openrouter"].agents != ("opencode",):
        # 免費模型只經過 opencode（v5 既有 MUST，本 change 補上檢查；design 決定 3）。
        raise PolicyError("openrouter must only allow opencode")
    if "openrouter" in parsed_subs and parsed_subs["openrouter"].models_allow != _OPENROUTER_ALLOW:
        raise PolicyError("openrouter must only allow *:free models")
    max_ahead = data.get("until_max_ahead_seconds")
    if not _positive_int(max_ahead):
        raise PolicyError("policy.until_max_ahead_seconds must be a positive integer")
    usage = _parse_usage(data.get("usage"), parsed_subs) if "usage" in data else {}
    roles = _parse_roles(data.get("roles"), parsed_subs) if "roles" in data else {}
    return Policy(
        subscriptions=parsed_subs,
        until_max_ahead_seconds=max_ahead,
        usage=usage,
        roles=roles,
    )
