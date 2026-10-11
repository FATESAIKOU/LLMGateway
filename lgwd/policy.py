"""政策檔（design 決定 8）。由 review 實作。

load_policy(path) 讀 JSON、驗證、回傳 Policy；不合法丟 PolicyError（服務啟動時不啟動、SIGHUP 時沿用舊的）。
PolicyError 的訊息只用固定字串，不回顯檔案裡的字串（值永不進例外）。
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass

SUBSCRIPTIONS = ("claude-personal", "claude-company", "openrouter", "opencode-zen", "ollama-cloud")
AGENTS = ("claude-code", "opencode")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionPolicy:
    name: str
    agents: tuple[str, ...]
    model_allow: tuple[str, ...]      # fnmatch 樣式，例如 "*" 或 "*:free"
    default_model: str | None
    cap: int | None                   # None＝不設上限
    default_cooldown_seconds: int
    max_cooldown_seconds: int | None


@dataclass(frozen=True)
class Policy:
    subscriptions: dict[str, SubscriptionPolicy]
    order: dict[str, tuple[str, ...]]  # agent → 訂閱順序
    cooldown_buffer_seconds: int
    lease_ttl_seconds: int
    confirm_within_seconds: int
    resets_at_max_ahead_seconds: int

    def allows(self, subscription: str, agent: str, model: str | None) -> bool:
        """訂閱存在、agent 在該訂閱的 agents 裡、model（None 視為預設模型）符合 model_allow。"""
        sub = self.subscriptions.get(subscription)
        if sub is None or agent not in sub.agents:
            return False
        if model is None:
            model = sub.default_model
        if model is None:
            return True  # 沒有模型資訊就不擋
        if not isinstance(model, str):
            return False
        return any(fnmatch.fnmatchcase(model, pat) for pat in sub.model_allow)

    def candidates(self, agent: str) -> tuple[str, ...]:
        """該 agent 的訂閱順序；不認得的 agent 回空 tuple。"""
        if not isinstance(agent, str):
            return ()
        return tuple(self.order.get(agent, ()))


def _positive_int(value: object) -> bool:
    # bool 是 int 的子類，要先排除
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_subscription(name: str, spec: object) -> SubscriptionPolicy:
    if not isinstance(spec, dict):
        raise PolicyError("subscription must be an object")
    agents = spec.get("agents")
    if not isinstance(agents, list) or any(a not in AGENTS for a in agents):
        raise PolicyError("subscription agents invalid")
    models = spec.get("models")
    if not isinstance(models, dict):
        raise PolicyError("subscription models must be an object")
    allow = models.get("allow")
    if not isinstance(allow, list) or any(not isinstance(p, str) for p in allow):
        raise PolicyError("subscription models.allow must be a list of strings")
    default = models.get("default")
    if default is not None and not isinstance(default, str):
        raise PolicyError("subscription models.default must be a string or null")
    cap = spec.get("cap")
    if cap is not None and not _positive_int(cap):
        raise PolicyError("subscription cap must be a positive integer or null")
    cooling = spec.get("default_cooldown_seconds")
    if not _positive_int(cooling):
        raise PolicyError("subscription default_cooldown_seconds must be a positive integer")
    max_cooling = spec.get("max_cooldown_seconds")
    if max_cooling is not None and not _positive_int(max_cooling):
        raise PolicyError("subscription max_cooldown_seconds must be a positive integer or null")
    return SubscriptionPolicy(
        name=name,
        agents=tuple(agents),
        model_allow=tuple(allow),
        default_model=default,
        cap=cap,
        default_cooldown_seconds=cooling,
        max_cooldown_seconds=max_cooling,
    )


def load_policy(path: str) -> Policy:
    """驗證規則（不合法丟 PolicyError，訊息說明哪裡錯、不含檔案字串）：
    - subscriptions 的鍵只能是 SUBSCRIPTIONS；order 的鍵只能是 AGENTS，值只能是已定義的訂閱；
    - claude-personal／claude-company 的 agents 只能是 ["claude-code"]（條款）；opencode-zen 不得含 claude-code；
    - order["claude-code"] 不得含 openrouter、ollama-cloud（v0 的否定約束）；
    - 數值欄位為正整數（cap 可為 null）。
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
    order = data.get("order")
    if not isinstance(order, dict):
        raise PolicyError("policy.order must be an object")
    for name in subs:
        if name not in SUBSCRIPTIONS:
            raise PolicyError("policy.subscriptions has unknown key")
    parsed_subs = {name: _parse_subscription(name, spec) for name, spec in subs.items()}
    parsed_order: dict[str, tuple[str, ...]] = {}
    for agent, seq in order.items():
        if agent not in AGENTS:
            raise PolicyError("policy.order has unknown agent")
        if not isinstance(seq, list) or any(not isinstance(s, str) or s not in parsed_subs for s in seq):
            raise PolicyError("policy.order must list defined subscriptions")
        parsed_order[agent] = tuple(seq)
    # 條款否定約束（只用固定字串，見模組 docstring）
    for name in ("claude-personal", "claude-company"):
        if name in parsed_subs and parsed_subs[name].agents != ("claude-code",):
            raise PolicyError("claude subscriptions allow claude-code only")
    if "opencode-zen" in parsed_subs and "claude-code" in parsed_subs["opencode-zen"].agents:
        raise PolicyError("opencode-zen must not allow claude-code")
    if "claude-code" in parsed_order and (
        "openrouter" in parsed_order["claude-code"] or "ollama-cloud" in parsed_order["claude-code"]
    ):
        raise PolicyError("order[claude-code] must not contain openrouter or ollama-cloud")
    numbers: dict[str, int] = {}
    for field in (
        "cooldown_buffer_seconds",
        "lease_ttl_seconds",
        "confirm_within_seconds",
        "resets_at_max_ahead_seconds",
    ):
        value = data.get(field)
        if not _positive_int(value):
            raise PolicyError(f"policy.{field} must be a positive integer")
        numbers[field] = value
    return Policy(
        subscriptions=parsed_subs,
        order=parsed_order,
        cooldown_buffer_seconds=numbers["cooldown_buffer_seconds"],
        lease_ttl_seconds=numbers["lease_ttl_seconds"],
        confirm_within_seconds=numbers["confirm_within_seconds"],
        resets_at_max_ahead_seconds=numbers["resets_at_max_ahead_seconds"],
    )
