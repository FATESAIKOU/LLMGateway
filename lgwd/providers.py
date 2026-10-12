"""CredentialProvider（design 決定 7）：每種 provider 一個實作，負責共通 JSON schema 中
與 provider 有關的欄位（provider、kind、endpoint）。政策檔只管 priority、agents、models。

- anthropic：Claude 訂閱（claude-subscription），endpoint 為 null（由 agent 直連）；
- openrouter、ollama-cloud、opencode-zen：api-key，各帶自己的 endpoint。
- 值不含任何環境變數名；把 key 變成哪些環境變數由 sidecar 的 AgentDriver 決定。
只用標準函式庫。
"""

from __future__ import annotations


class CredentialProvider:
    """介面：一個 provider 的靜態描述。"""

    provider = ""
    kind = ""
    endpoint: str | None = None

    def describe(self) -> dict:
        """回 schema 中與 provider 有關的欄位。"""
        return {"provider": self.provider, "kind": self.kind, "endpoint": self.endpoint}


class AnthropicProvider(CredentialProvider):
    provider = "anthropic"
    kind = "claude-subscription"
    endpoint = None


class OpenRouterProvider(CredentialProvider):
    provider = "openrouter"
    kind = "api-key"
    endpoint = "https://openrouter.ai/api"


class OllamaCloudProvider(CredentialProvider):
    provider = "ollama-cloud"
    kind = "api-key"
    endpoint = "https://ollama.com"


class OpenCodeZenProvider(CredentialProvider):
    provider = "opencode-zen"
    kind = "api-key"
    endpoint = "https://opencode.ai/zen/v1"


# subscription id → provider。id 是設計固定值，不是介面的一部分。
PROVIDERS: dict[str, CredentialProvider] = {
    "claude-personal": AnthropicProvider(),
    "claude-company": AnthropicProvider(),
    "openrouter": OpenRouterProvider(),
    "opencode-zen": OpenCodeZenProvider(),
    "ollama-cloud": OllamaCloudProvider(),
}


def provider_for(subscription: str) -> CredentialProvider:
    """已知 id 的 provider；未知 id 丟 KeyError（呼叫方先驗 id 存在）。"""
    return PROVIDERS[subscription]
