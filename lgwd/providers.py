"""CredentialProvider（design 決定 7）：每種 provider 一個實作，負責共通 JSON schema 中
與 provider 有關的欄位（provider、kind、endpoint）。政策檔只管 priority、agents、models。

- anthropic：Claude 訂閱（claude-subscription），endpoint 為 null（由 agent 直連）；
- openrouter、ollama-cloud、opencode-zen、opencode-go：api-key，各帶自己的 endpoint。
- 把（provider、kind、agent）＋key＋model 組成 resolve 的 env 由 build_env（design 決定 2）；
  opencode 只回片段（OPENCODE_* 四鍵），OPENCODE_CONFIG_CONTENT 由 sidecar 組。
- endpoint 是中介資料（會進 meta 與稽核），不得內嵌憑證：validate_endpoints() 在
  組裝根檢查（main.Gateway、load_policy 之後），不合法丟固定字串 ValueError，
  不合法不啟動。本模組不 import policy.py（避免循環 import）。
只用標準函式庫。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageDeps:
    """組裝取用量實作時的外部相依（#60）；全部可注入，測試用假的。"""
    http_get: object = None        # (url, headers, timeout) -> (status, headers, body)；None＝標準函式庫
    clock: object = None           # 可替換時鐘（#85 中-3）；None＝SystemClock
    claude_cwd: str | None = None  # Claude 取用量的固定工作目錄（Config.usage_claude_cwd）
    claude_command: str = "claude"
    claude_work_dir: str | None = None


class CredentialProvider:
    """介面：一個 provider 的靜態描述。"""

    provider = ""
    kind = ""
    endpoint: str | None = None
    # opencode 全域的 provider id（resolve env 的 OPENCODE_PROVIDER_ID；design 決定 2）
    opencode_provider_id = ""

    def make_usage_fetcher(self, deps: "UsageDeps"):
        """取用量的實作（#60）；預設沒有＝list／resolve 回 not_applicable。

        回 fetcher（介面見 lgwd/usage.py UsageService）。實作模組延後 import，
        不讓 providers 在載入時依賴 HTTP／子行程相關程式。
        """
        return None

    def describe(self) -> dict:
        """回 schema 中與 provider 有關的欄位。"""
        return {"provider": self.provider, "kind": self.kind, "endpoint": self.endpoint}

    def build_env(self, agent: str, key: str, model: str | None) -> dict:
        """resolve 的 env（design 決定 2）：名稱→值，只含憑證、端點、模型命名三類。

        鍵集＝spec「env 的組成」的清單（env_schema 1）；呼叫方的維運變數（server
        password、permission、instructions 等）一律不在。model 為 null（agent 自己
        的預設）時不給模型變數。
        """
        if agent == "opencode":
            # 片段：OPENCODE_CONFIG_CONTENT／baseURL 由 sidecar 組（不回）。
            env = {
                "OPENCODE_API_KEY": key,
                "OPENCODE_PROVIDER_ID": self.opencode_provider_id,
            }
            if model is not None:
                env["OPENCODE_MODEL"] = model
                env["OPENCODE_SMALL_MODEL"] = model
            return env
        # agent == "claude-code"
        if self.kind == "claude-subscription":
            env = {"CLAUDE_CODE_OAUTH_TOKEN": key}
        elif self.provider == "anthropic":
            # 預留形狀（現行不可達：anthropic 恆為 claude-subscription）；啟用前需 bump env_schema。
            env = {"ANTHROPIC_API_KEY": key}
        else:
            env = {
                "ANTHROPIC_AUTH_TOKEN": key,
                # 刻意佔位：蓋掉子行程環境可能殘留的繼承值。
                "ANTHROPIC_API_KEY": "",
            }
            if self.endpoint is not None:
                env["ANTHROPIC_BASE_URL"] = self.endpoint
        if model is not None:
            env["ANTHROPIC_MODEL"] = model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        return env


class AnthropicProvider(CredentialProvider):
    provider = "anthropic"
    kind = "claude-subscription"
    endpoint = None

    def make_usage_fetcher(self, deps: "UsageDeps"):
        from .usage_claude import ClaudeUsageFetcher
        return ClaudeUsageFetcher(cwd=deps.claude_cwd, command=deps.claude_command,
                                  work_dir=deps.claude_work_dir)


class OpenRouterProvider(CredentialProvider):
    provider = "openrouter"
    kind = "api-key"
    endpoint = "https://openrouter.ai/api"
    opencode_provider_id = "openrouter"

    def make_usage_fetcher(self, deps: "UsageDeps"):
        from .usage_openrouter import OpenRouterUsageFetcher
        return OpenRouterUsageFetcher(http_get=deps.http_get, clock=deps.clock)


class OllamaCloudProvider(CredentialProvider):
    provider = "ollama-cloud"
    kind = "api-key"
    endpoint = "https://ollama.com"
    opencode_provider_id = "ollama-cloud"

    def make_usage_fetcher(self, deps: "UsageDeps"):
        from .usage_ollama import OllamaUsageFetcher
        return OllamaUsageFetcher(http_get=deps.http_get)


class OpenCodeZenProvider(CredentialProvider):
    provider = "opencode-zen"
    kind = "api-key"
    endpoint = "https://opencode.ai/zen/v1"
    opencode_provider_id = "opencode"


class OpenCodeGoProvider(CredentialProvider):
    provider = "opencode-go"
    kind = "api-key"
    endpoint = "https://opencode.ai/inference/go/openai/v1"
    opencode_provider_id = "opencode-go"


# subscription id → provider。id 是設計固定值，不是介面的一部分。
PROVIDERS: dict[str, CredentialProvider] = {
    "claude-personal": AnthropicProvider(),
    "claude-company": AnthropicProvider(),
    "openrouter": OpenRouterProvider(),
    "opencode-zen": OpenCodeZenProvider(),
    "opencode-go": OpenCodeGoProvider(),
    "ollama-cloud": OllamaCloudProvider(),
}


def provider_for(subscription: str) -> CredentialProvider:
    """已知 id 的 provider；未知 id 丟 KeyError（呼叫方先驗 id 存在）。"""
    return PROVIDERS[subscription]


# endpoint 值會照「中介資料」進 meta 與稽核；內嵌憑證的值會躲過只掃憑證鍵的
# canary，所以在這裡擋（design 決定 2、3；canary 的樣式掃描是第二道）。
_ENDPOINT_CREDENTIAL_MARKS = ("key=", "token=", "api_key=", "access_token=")


def validate_endpoints() -> None:
    """走 PROVIDERS 逐筆驗 endpoint：含 query 憑證樣式或 userinfo（@）即丟固定字串
    ValueError（訊息不回顯任何值）。

    呼叫點在組裝根（main.Gateway.__init__、load_policy 之後）；不合法＝服務不啟動。
    """
    for provider in PROVIDERS.values():
        endpoint = provider.endpoint
        if endpoint is None:
            continue  # claude-subscription：agent 直連，沒有 endpoint
        lowered = endpoint.lower()
        if "@" in endpoint or any(mark in lowered for mark in _ENDPOINT_CREDENTIAL_MARKS):
            raise ValueError("provider endpoint must not embed credentials")
