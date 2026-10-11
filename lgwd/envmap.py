"""env 對照表（design 決定 7）。由 review 實作。

build_env(subscription, agent, key, model) → (env, unset)。不允許的組合丟 ValueError（呼叫方轉成 not_allowed）。
- Claude 兩把 × claude-code：env={"CLAUDE_CODE_OAUTH_TOKEN": key}；unset=["ANTHROPIC_API_KEY","ANTHROPIC_AUTH_TOKEN","ANTHROPIC_BASE_URL"]
- Claude 兩把 × opencode：不允許
- openrouter × claude-code：env={"ANTHROPIC_BASE_URL":"https://openrouter.ai/api","ANTHROPIC_AUTH_TOKEN":key,"ANTHROPIC_API_KEY":""}；unset=["CLAUDE_CODE_OAUTH_TOKEN"]
  （v0 政策不會選到，但對照表要有）
- ollama-cloud × claude-code：env={"ANTHROPIC_BASE_URL":OLLAMA_ANTHROPIC_BASE_URL,"ANTHROPIC_AUTH_TOKEN":key,"ANTHROPIC_API_KEY":""}；unset=["CLAUDE_CODE_OAUTH_TOKEN"]
- opencode-zen × claude-code：不允許
- openrouter × opencode：env={"OPENROUTER_API_KEY": key}；unset 同 Claude 的四個變數
- ollama-cloud × opencode：env={"OLLAMA_API_KEY": key}；unset 同上
- opencode-zen × opencode：env={"OPENCODE_API_KEY": key}；unset 同上
model 有值時：claude-code 加 ANTHROPIC_MODEL；opencode 加 LGW_MODEL（由 sidecar 轉成 opencode 的 -m）。
變數名是暫定的，實作時實打確認（tasks 4.4）。
"""

from __future__ import annotations

OLLAMA_ANTHROPIC_BASE_URL = "https://ollama.com"  # 暫定，tasks 4.4 確認


def build_env(subscription: str, agent: str, key: str, model: str | None) -> tuple[dict[str, str], list[str]]:
    raise NotImplementedError
