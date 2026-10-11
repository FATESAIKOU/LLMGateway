"""HTTP 層（design 決定 6、11）。由 test 實作。

- make_api_server(service, host, port) → ThreadingHTTPServer：路由 /v1/*；Bearer 認身分（service.authenticate），
  沒有或錯誤 → 401；JSON 解析失敗 → 400；ApiError → 對應狀態碼＋body()；每個回覆加 "api_version": 1。
  路由：POST /v1/leases、POST /v1/leases/{id}/(start|exhausted|renew|release|usage)、GET /v1/board、
  GET /v1/whoami（只驗 Bearer，回 {"client": 名稱}，不碰 service）。
- make_board_server(service, host="127.0.0.1", port) → 只服務 GET /board 的唯讀 HTML（不要憑證、全部輸出跳脫）。
  host 不是 loopback 時丟 ValueError（HTML 只綁 loopback）。
- body 超過 1 MB 直接回 413，不讀 body。
- log 只記方法、路徑（租約 id 可）、狀態碼；500 另記一行例外種類；不記標頭、body 與值。
"""

from __future__ import annotations

import html
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .errors import ApiError, bad_request, not_found

API_VERSION = 1

_LOG = logging.getLogger("lgwd.http")

# 只允許 127.0.0.1（fail closed；::1、localhost 也不收）
_BOARD_HOST = "127.0.0.1"

_LEASE_ACTIONS = ("start", "exhausted", "renew", "release", "usage")

# body 上限：超過直接 413，不讀 body。
_MAX_BODY_BYTES = 1024 * 1024


def _bearer(handler: BaseHTTPRequestHandler) -> str | None:
    """取 Authorization: Bearer 的值；格式不對回 None。"""
    auth = handler.headers.get("Authorization")
    if not auth:
        return None
    scheme, _, value = auth.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    """讀 body 並解析 JSON；空 body 視為 {}；失敗丟 bad_request（不回傳內容）。
    Content-Length 超過上限直接丟 413，不讀 body。"""
    try:
        size = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        size = 0
    if size > _MAX_BODY_BYTES:
        raise ApiError(413, "body_too_large")
    raw = handler.rfile.read(size) if size > 0 else b""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise bad_request()
    if not isinstance(data, dict):
        raise bad_request()
    return data


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    """送 JSON 回覆，一律帶 api_version。"""
    body = dict(payload)
    body["api_version"] = API_VERSION
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass  # 客戶端先斷線，不算錯


def _send_empty(handler: BaseHTTPRequestHandler, status: int) -> None:
    """204 專用：不帶 body（所以也沒有 api_version）。"""
    try:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _match_api(method: str, path: str):
    """回傳 ("leases",) ｜ ("lease_action", lease_id, action) ｜ ("board",) ｜ ("whoami",) ｜ None。"""
    if method == "GET" and path == "/v1/board":
        return ("board",)
    if method == "GET" and path == "/v1/whoami":
        return ("whoami",)
    if method == "POST" and path == "/v1/leases":
        return ("leases",)
    if method == "POST":
        parts = path.split("/")
        # ["", "v1", "leases", "{id}", "{action}"]
        if len(parts) == 5 and parts[1] == "v1" and parts[2] == "leases" and parts[3]:
            if parts[4] in _LEASE_ACTIONS:
                return ("lease_action", unquote(parts[3]), parts[4])
    return None


def _api_handler(service):
    """依 service 產生 handler 類別（service 是 LeaseService 或測試替身）。"""

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
            path = urlsplit(self.path).path  # query 不參與路由
            route = _match_api(method, path)
            if route is None:
                _LOG.info("%s %s -> %s", method, path, 404)
                _send_json(self, 404, not_found().body())
                return
            client = service.authenticate(_bearer(self) or "")
            if client is None:
                _LOG.info("%s %s -> %s", method, path, 401)
                _send_json(self, 401, {"error": "unauthorized"})
                return
            try:
                if route[0] == "board":
                    status, payload = 200, service.board()
                elif route[0] == "whoami":
                    status, payload = 200, {"client": client}  # 不碰 service、不碰租約
                else:
                    body = _read_json(self)
                    if route[0] == "leases":
                        status, payload = service.request_lease(client, body)
                    else:
                        _, lease_id, action = route
                        # 路由名 usage 對應 service 的 report_usage 方法
                        name = "report_usage" if action == "usage" else action
                        status, payload = getattr(service, name)(client, lease_id, body)
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
            else:
                _send_json(self, status, payload)

    return ApiHandler


def make_api_server(service, host: str, port: int) -> ThreadingHTTPServer:
    """建 /v1/* 的 API 伺服器（尚未 serve_forever）。"""
    server = ThreadingHTTPServer((host, port), _api_handler(service))
    server.daemon_threads = True  # 測試結束不卡住
    return server


def _esc(value) -> str:
    """HTML 跳脫；None 轉成「未知」（spec status-board：拿不到顯示未知）。"""
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True), quote=True)
    return html.escape(str(value), quote=True)


def _render_board(data: dict) -> str:
    """board dict 轉唯讀 HTML；全部輸出經 _esc。"""
    subs = data.get("subscriptions", []) if isinstance(data, dict) else []
    rows = []
    for sub in subs:
        actives = sub.get("active") or []
        leases = "".join(
            "<li>{worker} ／ {role} ／ {agent} ／自 {since}</li>".format(
                worker=_esc(a.get("worker")),
                role=_esc(a.get("role")),
                agent=_esc(a.get("agent")),
                since=_esc(a.get("since")),
            )
            for a in actives
        )
        cap = sub.get("cap")
        rows.append(
            "<section><h2>{name}</h2><ul>"
            "<li>部署：{deployed}</li>"
            "<li>使用中：{count}／{cap}</li>"
            "<li>未確認：{pending}</li>"
            "<li>冷卻：{cooling}{until}</li>"
            "<li>用量：{usage}</li>"
            "<li>租約：<ul>{leases}</ul></li>"
            "</ul></section>".format(
                name=_esc(sub.get("name")),
                deployed="已部署" if sub.get("deployed") else "未部署",
                count=_esc(sub.get("count")),
                cap=_esc(cap) if cap is not None else "無上限",
                pending=_esc(sub.get("pending")),
                cooling=_esc(sub.get("cooling")),
                until="（到 {t}）".format(t=_esc(sub.get("cooling_until")))
                if sub.get("cooling_until")
                else "",
                usage=_esc(sub.get("usage")),
                leases=leases or "<li>無</li>",
            )
        )
    return (
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<title>LLMGateway 狀態板</title></head><body>"
        "<h1>LLMGateway 狀態板</h1><p>產生於：{gen}</p>{rows}</body></html>".format(
            gen=_esc(data.get("generated_at") if isinstance(data, dict) else None),
            rows="".join(rows),
        )
    )


def make_board_server(service, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """建唯讀 HTML 伺服器；host 非 127.0.0.1 直接丟 ValueError。"""
    if host != _BOARD_HOST:
        raise ValueError("board server only binds loopback")
    parent = service

    class BoardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            _LOG.debug("%s %s", self.command, self.path)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path != "/board":
                _LOG.info("GET %s -> %s", path, 404)
                self._send_text(404, "not found")
                return
            try:
                page = _render_board(parent.board())
            except Exception:  # 板壞了也不洩漏細節
                _LOG.info("GET %s -> %s", path, 500)
                self._send_text(500, "internal error")
                return
            _LOG.info("GET %s -> %s", path, 200)
            raw = page.encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # 非 GET 一律 404（唯讀，不提供其他方法）
        def do_POST(self):
            _LOG.info("POST %s -> %s", urlsplit(self.path).path, 404)
            self._send_text(404, "not found")

        def do_PUT(self):
            _LOG.info("PUT %s -> %s", urlsplit(self.path).path, 404)
            self._send_text(404, "not found")

        def do_DELETE(self):
            _LOG.info("DELETE %s -> %s", urlsplit(self.path).path, 404)
            self._send_text(404, "not found")

        def do_PATCH(self):
            _LOG.info("PATCH %s -> %s", urlsplit(self.path).path, 404)
            self._send_text(404, "not found")

        def _send_text(self, status: int, text: str) -> None:
            raw = text.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer((host, port), BoardHandler)
    server.daemon_threads = True
    return server
