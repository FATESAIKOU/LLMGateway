"""key 檔（design 決定 5b；spec key-vault）。由 review 實作。

key 檔是 `KEY=VALUE` 一行一把：空行略過，# 開頭為註解，鍵名固定見 KEY_NAMES，
重複鍵亦視為格式錯誤。值為空字串視同沒部署。
KEY_NAMES 允許多個訂閱對到同一個鍵名（例 opencode-zen／opencode-go 共用
OPENCODE_ZEN_API_KEY）；key 檔裡仍只寫一行，不是重複鍵。
鍵名與值兩側空白忽略，值內部空白保留；行內註解不處理，一律視為值的一部分。
任何 repr、錯誤訊息、log 都 MUST NOT 含值：錯誤訊息只寫行號與種類，不回顯該行內容。
"""

from __future__ import annotations

import threading

# 訂閱 → key 檔裡的鍵名
KEY_NAMES = {
    "claude-personal": "CLAUDE_PERSONAL_OAUTH_TOKEN",
    "claude-company": "CLAUDE_COMPANY_OAUTH_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_ZEN_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
}


class KeyFileError(ValueError):
    pass


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue  # 空行與註解
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            raise KeyFileError(f"line {lineno}: not KEY=VALUE")
        if name not in KEY_NAMES.values():
            raise KeyFileError(f"line {lineno}: unknown key")
        if name in values:
            raise KeyFileError(f"line {lineno}: duplicate key")
        values[name] = value.strip()
    return values


class KeyStore:
    """thread-safe。reload() 失敗時保留舊值並丟 KeyFileError（呼叫方記錄並在狀態板標示）。

    __init__ 即讀檔一次：檔案缺失或格式錯誤直接丟錯（服務啟動時不啟動）。
    """

    def __init__(self, path: str):
        self._path = str(path)
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}
        self._last_error: str | None = None
        self.reload()

    def reload(self) -> None:
        """重讀檔案；格式錯誤（不是 KEY=VALUE、未知鍵）丟 KeyFileError 且不改變目前的值。"""
        try:
            with open(self._path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            with self._lock:
                self._last_error = "key file unreadable"
            raise KeyFileError("key file unreadable") from exc
        try:
            values = _parse(text)
        except KeyFileError as exc:
            with self._lock:
                self._last_error = str(exc)
            raise
        with self._lock:
            self._values = values
            self._last_error = None

    def _key_name(self, subscription: str) -> str:
        try:
            return KEY_NAMES[subscription]
        except KeyError:
            raise KeyFileError("unknown subscription") from None

    def get(self, subscription: str) -> str | None:
        """回傳值；沒部署（缺鍵或空字串）回 None。"""
        name = self._key_name(subscription)
        with self._lock:
            value = self._values.get(name)
        if not value:
            return None
        return value

    def deployed(self, subscription: str) -> bool:
        name = self._key_name(subscription)  # 不認得的訂閱直接丟錯
        with self._lock:
            return bool(self._values.get(name))

    @property
    def last_reload_error(self) -> str | None:
        """最近一次 reload 的錯誤類型說明（不含值）；成功則為 None。"""
        with self._lock:
            return self._last_error

    def __repr__(self) -> str:  # 永遠不含值
        return "<KeyStore>"
