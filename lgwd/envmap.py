"""env 對照表（design 決定 7）。由 review 實作。

build_env(subscription, agent, key, model) → (env, unset)。不允許的組合丟 ValueError（呼叫方轉成 not_allowed）。
- Claude 兩把 × claude-code：env={"CLAUDE_CODE_OAUTH_TOKEN": key}；unset=["ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN","ANTHROPIC_BASE_URL"]
- Claude 兩把 × opencode：不允許
- openrouter × claude-code：env={"ANTHROPIC_BASE_URL":"https://openrouter.ai/api","ANTHROPIC_AUTH_TOKEN":key,"ANTHROPIC_API_KEY":""}；unset=["CLAUDE_CODE_OAUTH_TOKEN"]
  （v0 政策不會選到，但對照表要有）
- ollama-cloud × claude-code：env={"ANTHROPIC_BASE_URL":OLLAMA_ANTHROPIC_BASE_URL,"ANTHROPIC_AUTH_TOKEN":key,"ANTHROPIC_API_KEY":""}；unset=["CLAUDE_CODE_OAUTH_TOKEN"]
- opencode-zen × claude-code：不允許
- openrouter × opencode：env={"OPENROUTER_API_KEY": key}；unset 同下四個變數
- ollama-cloud × opencode：env={"OLLAMA_API_KEY": key}；unset 同下
- opencode-zen × opencode：env={"OPENCODE_API_KEY": key}；unset 同下
- opencode 的 unset 另含 CLAUDE_CODE_OAUTH_TOKEN（共四個），避免訂閱 token 殘留。
model 有值時：claude-code 加 ANTHROPIC_MODEL；opencode 加 LGW_MODEL（由 sidecar 轉成 opencode 的 -m）。
錯誤訊息固定為 "not allowed"，不回顯任何傳入字串（值永不進例外）。
變數名是暫定的，實作時實打確認（tasks 4.4）。
"""

from __future__ import annotations

OLLAMA_ANTHROPIC_BASE_URL = "https://ollama.com"  # 暫定，tasks 4.4 確認

_CLAUDE_UNSET = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
_OPENCODE_UNSET = [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
]


def build_env(subscription: str, agent: str, key: str, model: str | None) -> tuple[dict[str, str], list[str]]:
    if agent == "claude-code":
        if subscription in ("claude-personal", "claude-company"):
            env = {"CLAUDE_CODE_OAUTH_TOKEN": key}
            unset = list(_CLAUDE_UNSET)
        elif subscription == "openrouter":
            env = {
                "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_API_KEY": "",
            }
            unset = ["CLAUDE_CODE_OAUTH_TOKEN"]
        elif subscription == "ollama-cloud":
            env = {
                "ANTHROPIC_BASE_URL": OLLAMA_ANTHROPIC_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_API_KEY": "",
            }
            unset = ["CLAUDE_CODE_OAUTH_TOKEN"]
        else:
            raise ValueError("not allowed")
        if model:
            env["ANTHROPIC_MODEL"] = model
        return env, unset
    if agent == "opencode":
        if subscription == "openrouter":
            env = {"OPENROUTER_API_KEY": key}
        elif subscription == "ollama-cloud":
            env = {"OLLAMA_API_KEY": key}
        elif subscription == "opencode-zen":
            env = {"OPENCODE_API_KEY": key}
        else:
            raise ValueError("not allowed")
        if model:
            env["LGW_MODEL"] = model
        return env, list(_OPENCODE_UNSET)
    raise ValueError("not allowed")
