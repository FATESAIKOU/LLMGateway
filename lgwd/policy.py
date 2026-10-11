"""政策檔（design 決定 8）。由 review 實作。

load_policy(path) 讀 JSON、驗證、回傳 Policy；不合法丟 PolicyError（服務啟動時不啟動、SIGHUP 時沿用舊的）。
"""

from __future__ import annotations

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
        raise NotImplementedError

    def candidates(self, agent: str) -> tuple[str, ...]:
        """該 agent 的訂閱順序；不認得的 agent 回空 tuple。"""
        raise NotImplementedError


def load_policy(path: str) -> Policy:
    """驗證規則（不合法丟 PolicyError，訊息說明哪裡錯）：
    - subscriptions 的鍵只能是 SUBSCRIPTIONS；order 的鍵只能是 AGENTS，值只能是已定義的訂閱；
    - claude-personal／claude-company 的 agents 只能是 ["claude-code"]（條款）；opencode-zen 不得含 claude-code；
    - order["claude-code"] 不得含 openrouter、ollama-cloud（v0 的否定約束）；
    - 數值欄位為正整數（cap 可為 null）。
    """
    raise NotImplementedError
