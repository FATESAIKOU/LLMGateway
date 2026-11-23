"""授權判斷（#222 D3a；C-902～C-904、C-909 的判斷部分）。

`authorize` 是**純函式**：不讀檔案、不讀名冊快照、不解析請求、不碰時鐘或資料庫。
它只讀呼叫端傳進來的不可變 `PrincipalEntry`（C-905：那一份一定是驗章時同一份名冊
快照裡的值），所以「授權用哪一份快照」由呼叫端決定，這個模組不可能是那個漏洞的來源。

判斷順序（C-902，回傳第一個命中的固定原因字串）：

1. `op` 不在 `permissions.ops` → `forbidden_op`；
2. `op == "resolve"`：`agent` 有值、屬於 `lgwd.policy.AGENTS`，但不在
   `permissions.resolve_agents` → `forbidden_agent`；
3. `op == "roles"`：`role` 屬於 `lgwd.policy.ROLES`、`agent` 屬於 `AGENTS`，但
   `role` 不在 `permissions.roles`，或 `agent` 不在 `permissions.roles[role]` →
   `forbidden_role`；
4. 其他情況通過（回 `None`）。

`agent` 或 `role` 缺少、或不是合法值時**不判**（回 `None`），交給業務回 400 或
404：維持「輸入形狀錯誤 400 優先」的既有 precedence（`registry.role_candidates` 與
`registry._pick` 的 docstring）。

`resolve` 的請求不帶 role，所以只能限制 agent，不能限制「以哪個角色 resolve」
（C-903）；要做到依角色限制必須改 API，不在 D3。`cooldown`、`list`、`usage` 只判
操作（C-904：schema 1 沒有「每個訂閱」的範圍）。「未列就是拒絕」：名冊沒有的操作、
agent、role 一律拒絕，沒有萬用字元。
"""

from __future__ import annotations

from lgwd.auth_registry import PrincipalEntry
from lgwd.policy import AGENTS, ROLES

#: 授權拒絕的三個固定原因（design :154 的白名單；D3a 新增）。
FORBIDDEN_OP = "forbidden_op"
FORBIDDEN_AGENT = "forbidden_agent"
FORBIDDEN_ROLE = "forbidden_role"

#: 三個授權原因（`auth.REASONS` 與 `Verdict` 的不變式共用這一組）。
FORBIDDEN_REASONS = (FORBIDDEN_OP, FORBIDDEN_AGENT, FORBIDDEN_ROLE)

__all__ = [
    "FORBIDDEN_AGENT",
    "FORBIDDEN_OP",
    "FORBIDDEN_REASONS",
    "FORBIDDEN_ROLE",
    "authorize",
]


def authorize(permissions: PrincipalEntry, op: str, *, agent: str | None = None,
              role: str | None = None) -> str | None:
    """判一次業務請求的權限（C-902）；通過回 `None`，否則回固定原因字串。

    參數都必須是呼叫端已經解析好的那一份（C-909）：HTTP 層傳的是 service 實際使用的
    同一個 query 解析結果與 `route[1]`，不在這裡重新解析。`permissions` 必須是驗章
    時那一份名冊快照的 `PrincipalEntry`（C-905）；傳錯型別是程式錯誤，直接丟
    `TypeError`（不是「放行」，也不是「拒絕」——呼叫端壞掉要在測試裡看得見）。
    """
    if not isinstance(permissions, PrincipalEntry):
        raise TypeError("authorize needs the verification-time PrincipalEntry")
    if op not in permissions.ops:
        return FORBIDDEN_OP
    if op == "resolve":
        if agent is not None and agent in AGENTS and agent not in permissions.resolve_agents:
            return FORBIDDEN_AGENT
        return None
    if op == "roles":
        if role in ROLES and agent in AGENTS:
            allowed = permissions.roles.get(role)
            if allowed is None or agent not in allowed:
                return FORBIDDEN_ROLE
        return None
    return None
