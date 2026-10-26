# 測試套件的安全網（#60、#85）：行程內的測試不得對外取用量。預設的 Ollama／OpenRouter HTTP 層
# 換成一律丟例外的版本；原版留在 REAL_DEFAULT_*_HTTP_GET 給專門測它的測試用。起真 lgwd
# 子行程的測試改用政策檔 `usage` 段關掉 Ollama／OpenRouter／Claude（這個 patch 管不到子行程）。
from lgwd import usage_ollama, usage_openrouter

REAL_DEFAULT_HTTP_GET = usage_ollama.default_http_get
REAL_DEFAULT_OPENROUTER_HTTP_GET = usage_openrouter.default_http_get


def _no_network(url, headers, timeout):
    raise ConnectionRefusedError("tests must not reach the network")


usage_ollama.default_http_get = _no_network
usage_openrouter.default_http_get = _no_network
