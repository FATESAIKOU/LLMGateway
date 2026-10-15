"""政策檔（design 決定 7；tasks 1.2）。

load_policy(path) 讀 JSON、驗證、回傳 Policy；不合法丟 PolicyError。
政策檔只管 priority、agents、models（與 provider 有關的欄位在 lgwd/providers.py）。
PolicyError 的訊息只用固定字串與固定的訂閱 id，不回顯檔案裡的其他字串（值永不進例外）。

載入時機：只在啟動時（lgwd 啟動失敗就不啟動）。v5 沒有熱重載、沒有 SIGHUP，
換政策要重啟 lgwd（政策檔跟著 tag 換，見 docs/deploy.md 4.2）。key 檔是另一回事：
那個由 key-sync 換檔後 lgwd 依 mtime 自動重讀。
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass

SUBSCRIPTIONS = ("claude-personal", "claude-company", "opencode-zen", "openrouter", "ollama-cloud")
AGENTS = ("claude-code", "opencode")

# 條款與設計的否定約束（固定字串的錯誤訊息）
_CLAUDE_ONLY = ("claude-personal", "claude-company")
_ZEN_ONLY = ("opencode-zen",)


_OPENROUTER_ALLOW = ("*:free", "openrouter/free")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionPolicy:
    name: str
    priority: int
    agents: tuple[str, ...]
    models_allow: tuple[str, ...]      # fnmatch 樣式，例如 "*" 或 "*:free"
    models_default: str | None


@dataclass(frozen=True)
class Policy:
    subscriptions: dict[str, SubscriptionPolicy]
    until_max_ahead_seconds: int

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


def _parse_subscription(name: str, spec: object) -> SubscriptionPolicy:
    if not isinstance(spec, dict):
        raise PolicyError("subscription must be an object")
    priority = spec.get("priority")
    if not _is_priority(priority):
        raise PolicyError("subscription priority must be a non-negative integer")
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
    )


def load_policy(path: str) -> Policy:
    """驗證規則（不合法丟 PolicyError，訊息說明哪裡錯、不含檔案字串）：
    - subscriptions 的鍵只能是 SUBSCRIPTIONS；
    - claude-personal／claude-company 的 agents 只能剛好是 ["claude-code"]（條款）；
    - opencode-zen 的 agents 只能剛好是 ["opencode"]（沒有官方的 Claude Code 接法）；
    - openrouter 的 models.allow 只能是 ["*:free", "openrouter/free"]（只允許免費模型）；
    - 非 null 的 models.default 必須被同一筆的 models.allow 樣式涵蓋（fnmatch；fail-closed）；
    - priority 為非負整數；cooldown 的 until 上限為正整數。
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
    for name in subs:
        if name not in SUBSCRIPTIONS:
            raise PolicyError("policy.subscriptions has unknown key")
    parsed_subs = {name: _parse_subscription(name, spec) for name, spec in subs.items()}
    for name in _CLAUDE_ONLY:
        if name in parsed_subs and parsed_subs[name].agents != ("claude-code",):
            raise PolicyError("claude subscriptions allow claude-code only")
    for name in _ZEN_ONLY:
        if name in parsed_subs and parsed_subs[name].agents != ("opencode",):
            raise PolicyError("opencode-zen must only allow opencode")
    if "openrouter" in parsed_subs and parsed_subs["openrouter"].models_allow != _OPENROUTER_ALLOW:
        raise PolicyError("openrouter must only allow *:free models")
    max_ahead = data.get("until_max_ahead_seconds")
    if not _positive_int(max_ahead):
        raise PolicyError("policy.until_max_ahead_seconds must be a positive integer")
    return Policy(
        subscriptions=parsed_subs,
        until_max_ahead_seconds=max_ahead,
    )
