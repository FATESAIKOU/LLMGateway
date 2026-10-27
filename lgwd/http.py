"""HTTP 層（design 決定 6、5a）。由 test 實作。

- make_api_server(service, host, port) → ThreadingHTTPServer。只有五個路由：
  GET  /v1/subscriptions                      → service.list_subscriptions()
  GET  /v1/subscriptions/{id}?agent=<種類>     → service.get_subscription(id, agent)
  POST /v1/subscriptions/{id}/cooldown        → service.register_cooldown(id, body)（204）
  GET  /v1/resolve?agent=&provider=[&model=&subscription=&exclude=]
                                              → service.resolve(...)（design 決定 1～3）
  GET  /v1/roles/{role}?agent=[&data=]        → service.role_candidates(role, agent, data)
                                              （#100；spec role-allocation）
  其他路徑與方法一律 404。沒有應用層認證（本人選 (a)）；存取控制只靠 SSH。
- JSON 解析失敗或 body 不是物件 → 400；ApiError → 對應狀態碼＋body()；204 不帶 body。
- body 超過 1 MB 直接回 413，不讀 body。
- resolve 與 roles 的 query 用 parse_qs 的 form 解碼（design 決定 1）：空白值＝沒給
  （`subscription=x&exclude=` 因此是合法釘住）；已知參數重複出現 → 400；
  未知參數忽略；`+` 由呼叫端編成 %2B，server 只負責解碼。
- get 與 resolve 的 200 加 Cache-Control: no-store、Pragma: no-cache（回覆含憑證值，
  不讓 HTTP 快取或本機工具留下）。
- log 只記方法、路徑、狀態碼；500 另記一行例外種類；不記標頭、body 與值。
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ApiError, bad_request, body_too_large, not_found

_LOG = logging.getLogger("lgwd.http")

# body 上限：超過直接 413，不讀 body。
_MAX_BODY_BYTES = 1024 * 1024

# resolve 認得的 query 參數；不在這張表的忽略。
_RESOLVE_PARAMS = ("agent", "provider", "model", "subscription", "exclude")

# roles 認得的 query 參數：resolve 的已知參數集合再加上 data（#100；spec
# role-allocation）。另立一份、不共用 _RESOLVE_PARAMS 本身；重複與空值的規則
# 與 resolve 相同。
_ROLES_PARAMS = _RESOLVE_PARAMS + ("data",)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    """讀 body 並解析 JSON；空 body 視為 {}；失敗丟 bad_request（不回傳內容）。
    Content-Length 超過上限直接丟 413，不讀 body。"""
    try:
        size = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        size = 0
    if size > _MAX_BODY_BYTES:
        raise body_too_large()
    raw = handler.rfile.read(size) if size > 0 else b""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise bad_request() from None
    if not isinstance(data, dict):
        raise bad_request()
    return data


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict,
               no_cache: bool = False) -> None:
    """回 JSON；no_cache＝200 的 get／resolve 加不快取標頭（design 決定 1）。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        if no_cache:
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Pragma", "no-cache")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass  # 客戶端先斷線，不算錯


def _send_empty(handler: BaseHTTPRequestHandler, status: int) -> None:
    """204 專用：不帶 body。"""
    try:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _match_route(method: str, path: str):
    """回 ("list",) ｜ ("get", id) ｜ ("cooldown", id) ｜ ("resolve",) ｜ ("roles", role) ｜ None。"""
    if path == "/v1/subscriptions":
        return ("list",) if method == "GET" else None
    if path == "/v1/resolve":
        return ("resolve",) if method == "GET" else None
    parts = path.split("/")
    # ["", "v1", "subscriptions", "{id}"]
    if (method == "GET" and len(parts) == 4
            and parts[1] == "v1" and parts[2] == "subscriptions" and parts[3]):
        return ("get", unquote(parts[3]))
    # ["", "v1", "roles", "{role}"]
    if (method == "GET" and len(parts) == 4
            and parts[1] == "v1" and parts[2] == "roles" and parts[3]):
        return ("roles", unquote(parts[3]))
    # ["", "v1", "subscriptions", "{id}", "cooldown"]
    if (method == "POST" and len(parts) == 5
            and parts[1] == "v1" and parts[2] == "subscriptions" and parts[3]
            and parts[4] == "cooldown"):
        return ("cooldown", unquote(parts[3]))
    return None


def _query(query: str, known: tuple[str, ...]) -> dict:
    """parse_qs 的 form 解碼＋已知參數的共用規則（resolve 與 roles）。

    空白值＝沒給；已知參數重複出現（含空白的那一次）→ 400；未知參數忽略。
    回 {name: 第一個非空值}，只含給了非空值的已知參數。
    """
    groups = parse_qs(query, keep_blank_values=True)
    for name in known:
        if len(groups.get(name, ())) > 1:
            raise bad_request()
    return {
        name: values[0]
        for name, values in groups.items()
        if name in known and values[0]
    }


def _resolve_query(query: str) -> dict:
    """resolve 的 query 規則（design 決定 1）：parse_qs 的 form 解碼。"""
    return _query(query, _RESOLVE_PARAMS)


def _roles_query(query: str) -> dict:
    """roles 的 query 規則：與 resolve 相同，已知參數再加上 data（#100）。"""
    return _query(query, _ROLES_PARAMS)


def _api_handler(service):
    """依 service 產生 handler 類別（service 是 RegistryService 或測試替身）。"""

    class ApiHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # stderr 太吵，轉進 logger（debug，不影響測試斷言）
            _LOG.debug("%s %s", self.command, self.path)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def _dispatch(self, method: str):
            split = urlsplit(self.path)
            path = split.path  # query 不參與路由
            route = _match_route(method, path)
            if route is None:
                _LOG.info("%s %s -> %s", method, path, 404)
                _send_json(self, 404, not_found().body())
                return
            try:
                if route[0] == "list":
                    status, payload = 200, service.list_subscriptions()
                elif route[0] == "resolve":
                    q = _resolve_query(split.query)
                    payload = service.resolve(
                        q.get("agent"), q.get("provider"),
                        model=q.get("model"),
                        subscription=q.get("subscription"),
                        exclude=q.get("exclude"),
                    )
                    status = 200
                elif route[0] == "roles":
                    q = _roles_query(split.query)
                    payload = service.role_candidates(
                        route[1], q.get("agent"), q.get("data"))
                    status = 200
                elif route[0] == "get":
                    # 過渡期、刻意不動（與 resolve 的 query 規則不同）：d 單移除 get 前不要順手統一。
                    agents = parse_qs(split.query).get("agent") or [None]
                    status, payload = 200, service.get_subscription(route[1], agents[0])
                else:
                    body = _read_json(self)
                    service.register_cooldown(route[1], body)
                    status, payload = 204, {}
            except ApiError as exc:
                _LOG.info("%s %s -> %s", method, path, exc.status)
                _send_json(self, exc.status, exc.body())
                return
            except Exception as exc:  # 未預期錯誤：一律泛化 500，只記例外種類
                _LOG.info("%s %s -> %s", method, path, 500)
                _LOG.error("internal error kind=%s", type(exc).__name__)
                _send_json(self, 500, {"error": "internal"})
                return
            _LOG.info("%s %s -> %s", method, path, status)
            if status == 204:
                _send_empty(self, status)
            elif status == 200 and route[0] in ("get", "resolve"):
                _send_json(self, status, payload, no_cache=True)
            else:
                _send_json(self, status, payload)

    return ApiHandler


def make_api_server(service, host: str, port: int) -> ThreadingHTTPServer:
    """建 /v1/* 的 API 伺服器（尚未 serve_forever）。"""
    server = ThreadingHTTPServer((host, port), _api_handler(service))
    server.daemon_threads = True  # 測試結束不卡住
    return server
