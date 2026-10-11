"""key 檔（design 決定 2、12；spec key-vault）。由 review 實作。

key 檔是 `KEY=VALUE` 一行一把（# 開頭為註解），鍵名固定如下；值為空字串視同沒部署。
任何 repr、錯誤訊息、log 都 MUST NOT 含值。
"""

from __future__ import annotations

# 訂閱 → key 檔裡的鍵名
KEY_NAMES = {
    "claude-personal": "CLAUDE_PERSONAL_OAUTH_TOKEN",
    "claude-company": "CLAUDE_COMPANY_OAUTH_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
}


class KeyFileError(ValueError):
    pass


class KeyStore:
    """thread-safe。reload() 失敗時保留舊值並丟 KeyFileError（呼叫方記錄並在狀態板標示）。"""

    def __init__(self, path: str):
        raise NotImplementedError

    def reload(self) -> None:
        """重讀檔案；格式錯誤（不是 KEY=VALUE、未知鍵）丟 KeyFileError 且不改變目前的值。"""
        raise NotImplementedError

    def get(self, subscription: str) -> str | None:
        """回傳值；沒部署（缺鍵或空字串）回 None。"""
        raise NotImplementedError

    def deployed(self, subscription: str) -> bool:
        raise NotImplementedError

    @property
    def last_reload_error(self) -> str | None:
        """最近一次 reload 的錯誤類型說明（不含值）；成功則為 None。"""
        raise NotImplementedError

    def __repr__(self) -> str:  # 永遠不含值
        return "<KeyStore>"
