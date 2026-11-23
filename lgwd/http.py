"""HTTP 層（design 決定 6、5a）。由 test 實作。

- make_api_server(service, host, port, *, gate=None) → ThreadingHTTPServer。只有六條
  路由（五條業務＋健康檢查，後者不碰 service）：
  GET  /healthz                               → {"status": "ok"}（#246；探針用）
  GET  /v1/subscriptions                      → service.list_subscriptions()
  GET  /v1/usage                              → service.list_usage_summary()
                                              （usage-overview；C-5～C-9，用量一覽）
  POST /v1/subscriptions/{id}/cooldown        → service.register_cooldown(id, body)（204）
  GET  /v1/resolve?agent=&provider=[&model=&subscription=&exclude=&data=]
                                              → service.resolve(...)（design 決定 1～3）
  GET  /v1/roles/{role}?agent=[&data=]        → service.role_candidates(role, agent, data)
                                              （#100；spec role-allocation）
  其他路徑與方法一律 404（含 GET 以外對 /v1/usage 與 /healthz 的方法）。沒有應用層
  認證（本人選 (a)）；存取控制只靠 SSH。
- 預驗章 framing（#222 D2 PR-1；C-101～C-108）：``gate`` 的 ``.mode``
  （``off``／``warn``／``enforce``）決定模式，``None`` 等於 off；warn／enforce 的
  gate 必須有可呼叫的 ``.authenticate``（`lgwd.auth.AuthGate`），只有 ``.mode``
  的物件在建伺服器時就丟 ``ValueError``（fail-closed；#262 中-2）。**off 的行為與
  D1 逐 byte 相同**（用 ``self.path`` 與 ``_read_json``，包括「Content-Length 壞掉當 0」；
  C-108 的差分語料鎖住），不設任何新逾時。warn／enforce 下五條業務路由才先過
  ``lgwd.framing.inspect``（原始 request line 的 request-target、標頭名稱須為
  RFC 7230 token、標頭至多一次、Content-Length 形狀、Transfer-Encoding、1 MiB），
  再取號誌、在總期限內讀 body：framing 不合 enforce 回 401 ``unauthorized``（不進
  service）、warn 記 ``outcome=deny`` ``reason=framing`` 後照舊處理（記錄有
  ``mode=warn``，「would」由 mode 表達）；讀逾時 enforce 回 503
  ``auth_unavailable``、warn 記不可判定並關閉連線；號誌（上限 32、等 1 秒）取不到
  enforce 回 503、warn 記不可判定後照舊處理。body 超過 1 MiB 仍照現行回 413（三種
  模式相同）。
- ``/healthz`` 三種模式都**不套** framing、不取號誌（#247 J-1）：它不讀 body、
  HTTP/1.0 回應後即關連線，沒有走私面；若套用，enforce 與 off 對同一個怪請求的
  回應會不同，違反「三模式 bytes 相同」與「off 零變化」。原文不是 origin-form
  （``//healthz``、絕對 URI）時也一樣：先用 D1 的來源（``urlsplit(self.path).path``）
  比對路由，是 ``healthz`` 就直接回應，不經 framing、不記認證日誌（#262 中-3；
  spec resolve-auth「健康請求不受框架檢查」）。
- ``/healthz`` 只證明行程活著、能回應（而且 lgwd 已經過了啟動時的 policy／keys／
  DB 初始化）：body 固定 ``{"status": "ok"}``（16 bytes、只有一個鍵），不加版本、
  tag、模式、時間、主機名或任何額外標頭，也不讀 service、不讀 body、不寫 DB、
  不進稽核表。它**不代表** DB 此刻可寫（#246、H-2～H-4）。
- JSON 解析失敗或 body 不是物件 → 400；ApiError → 對應狀態碼＋body()；204 不帶 body。
- body 超過 1 MB 直接回 413，不讀 body。
- resolve 與 roles 的 query 用 parse_qs 的 form 解碼（design 決定 1）：空白值＝沒給
  （`subscription=x&exclude=` 因此是合法釘住）；已知參數重複出現 → 400；
  未知參數忽略；`+` 由呼叫端編成 %2B，server 只負責解碼。
- 含憑證值的回覆只剩 resolve；它的 200 加 Cache-Control: no-store、
  Pragma: no-cache（回覆含憑證值，不讓 HTTP 快取或本機工具留下）。單筆 get 已於 #48 移除，
  其路徑現在落到一律 404（不進 service）。
- log 只記方法、路徑、狀態碼；500 另記一行例外種類；不記標頭、body 與值。
  認證事件（只有 warn／enforce）只記 mode、結果、固定原因與**路由模板**，不含
  標頭值、簽章、nonce、query、body（C-107；聚合與正式格式是 PR-6 的 C-607）。
  ``outcome`` 只用 design 的白名單值（``pass``／``deny``／``rate_limited``／
  ``unavailable``）；warn 的 would-deny 記成 ``mode=warn outcome=deny``。
  ``route`` 只有原文無法路由時的固定哨兵 ``-`` 不是路由模板（唯一的例外，C-607
  的白名單要列入它）。
- ``gate`` 若有 ``.authenticate``（`lgwd.auth.AuthGate`）就在號誌持有區段內、讀完 body
  之後驗章（#222 D2 PR-6a；C-601～C-604）：五個標頭 → 查表 → 粗篩 → 預解析 → 八行 →
  ``verify`` → ``accept``；``apply(mode, verdict)`` 是模式對應的唯一一處。只有
  ``permit and framing.ok`` 時才呼叫 ``authenticate``：號誌取不到（warn）或 framing
  已判不合時，那一筆事件已由 ``apply`` 記過，``verdict`` 維持 ``None`` 照舊處理，
  不再於號誌外驗章、也不記第二筆事件（#262 中-1）。warn／enforce 的 gate 沒有
  ``.authenticate`` 在建立伺服器時就是 ``ValueError``（中-2）。``/healthz`` 不呼叫 gate。
  429 加 ``Retry-After``；enforce 的 401 body 對所有 reason 逐 byte 相同、不加
  ``WWW-Authenticate``。off 只在建立伺服器時記一行 ``auth mode=off``（C-607）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import auth as auth_mod
from . import framing
from .errors import ApiError, bad_request, body_too_large, not_found

_LOG = logging.getLogger("lgwd.http")

# body 上限：超過直接 413，不讀 body。
_MAX_BODY_BYTES = framing.MAX_BODY_BYTES

# 預驗章 framing 的固定值（design「預驗章請求框架」；不開旗標）。
_FRAMING_PERMITS = 32          # 讀 body＋驗章階段的並發上限（C-106）
_PERMIT_WAIT_SECONDS = 1.0     # 等號誌的上限（C-106）
_READ_DEADLINE_SECONDS = 10.0  # 從請求開始到 body 讀完的總期限（C-105）

# 認證事件只記路由模板，不含實際值（C-107；C-607 的白名單格式在 PR-6）。
_ROUTE_TEMPLATES = {
    "list": "/v1/subscriptions",
    "usage": "/v1/usage",
    "resolve": "/v1/resolve",
    "roles": "/v1/roles/{role}",
    "cooldown": "/v1/subscriptions/{id}/cooldown",
}

# _respond 的「body 還沒讀」標記：off 與 D1 一樣在 service 之前才讀（同一個 try）。
_UNREAD = object()

# resolve 認得的 query 參數；不在這張表的忽略。
_RESOLVE_PARAMS = ("agent", "provider", "model", "subscription", "exclude", "data")

# roles 認得的 query 參數與 resolve 的相同（#100 時另立含 data 的集合；#102 起 data
# 進了 _RESOLVE_PARAMS 而重複，改成同一份別名；spec role-allocation）。
_ROLES_PARAMS = _RESOLVE_PARAMS


def _parse_json_body(raw: bytes) -> dict:
    """解析已讀到的 body：空 body 視為 {}；失敗丟 bad_request（不回傳內容）。"""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise bad_request() from None
    if not isinstance(data, dict):
        raise bad_request()
    return data


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    """（off）讀 body 並解析 JSON；空 body 視為 {}；失敗丟 bad_request。
    Content-Length 超過上限直接丟 413，不讀 body；壞掉的 Content-Length 當 0
    （D1 的舊行為，off 時逐 byte 保留；C-101／C-108）。"""
    try:
        size = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        size = 0
    if size > _MAX_BODY_BYTES:
        raise body_too_large()
    raw = handler.rfile.read(size) if size > 0 else b""
    return _parse_json_body(raw)


def _gate_mode(gate) -> str:
    """gate 的 ``.mode``；``None`` 等於 off。不認得的值在建伺服器時就失敗。"""
    if gate is None:
        return "off"
    mode = getattr(gate, "mode", None)
    if mode not in ("off", "warn", "enforce"):
        raise ValueError("gate.mode must be one of off/warn/enforce")
    return mode


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict,
               no_cache: bool = False, headers=()) -> None:
    """回 JSON；no_cache＝200 的 resolve 加不快取標頭（design 決定 1）。

    ``headers`` 是額外標頭（目前只有 429 的 ``Retry-After``）；enforce 的 401 **不**加
    ``WWW-Authenticate``（C-604）。
    """
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        if no_cache:
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Pragma", "no-cache")
        for name, value in headers:
            handler.send_header(name, value)
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
    """回 ("healthz",) ｜ ("list",) ｜ ("usage",) ｜ ("cooldown", id) ｜ ("resolve",)
    ｜ ("roles", role) ｜ None。"""
    if path == "/healthz":
        # 健康檢查（#246）：只認 GET；query 不參與路由（/healthz?x=1 也是 200），
        # /healthz/ 與 /v1/healthz 落到下面的 404。
        return ("healthz",) if method == "GET" else None
    if path == "/v1/subscriptions":
        return ("list",) if method == "GET" else None
    if path == "/v1/usage":
        return ("usage",) if method == "GET" else None
    if path == "/v1/resolve":
        return ("resolve",) if method == "GET" else None
    parts = path.split("/")
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
    """resolve 的 query 規則（design 決定 1）：parse_qs 的 form 解碼。

    data 的值合法性由 service 驗（與 agent、model 同層），HTTP 層只管形狀。
    """
    return _query(query, _RESOLVE_PARAMS)


def _roles_query(query: str) -> dict:
    """roles 的 query 規則：與 resolve 相同（#100 起 data 也在已知參數集合）。"""
    return _query(query, _ROLES_PARAMS)


def _api_handler(service, mode: str, semaphore, gate=None, auth_log=None):
    """依 service 產生 handler 類別（service 是 RegistryService 或測試替身）。

    ``mode`` 是預驗章 framing 的模式（off／warn／enforce；C-101），``semaphore``
    是讀 body＋驗章的並發上限（C-106），``gate`` 是有 ``.authenticate`` 的認證 gate
    （warn／enforce 必備，off 為 ``None``；中-2），``auth_log`` 是 ``AuthLog``
    （C-607 的聚合）。
    """

    class ApiHandler(BaseHTTPRequestHandler):
        _framing_mode = mode
        _framing_sem = semaphore
        _gate = gate
        _auth_log = auth_log

        def handle_one_request(self):
            # C-105：總期限從這個請求的處理開始算；不是每次 recv 重新起算。
            self._framing_deadline = time.monotonic() + _READ_DEADLINE_SECONDS
            super().handle_one_request()

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
            if self._framing_mode == "off":
                return self._dispatch_off(method)
            return self._dispatch_gated(method)

        def _dispatch_off(self, method: str):
            """off（gate=None／mode=off）：與 D1 逐 byte 相同（C-101／C-108）。"""
            split = urlsplit(self.path)
            path = split.path  # query 不參與路由
            route = _match_route(method, path)
            if route is None:
                _LOG.info("%s %s -> %s", method, path, 404)
                _send_json(self, 404, not_found().body())
                return
            self._respond(method, path, route, split.query)

        def _authenticate(self, method: str, fr, raw: bytes):
            """驗章；warn／enforce 的 gate 一定帶可呼叫的 ``authenticate``（中-2）。

            建立伺服器時就擋掉只有 ``.mode`` 的物件，所以這裡不需要（也不應該）靜默
            跳過——那會讓 enforce 變成 fail-open。
            """
            return self._gate.authenticate(
                method=method, framing=fr, headers=self.headers, body=raw)

        def _apply_verdict(self, verdict, template: str, *, method: str, log_path: str,
                           close_on_warn: bool = False) -> bool:
            """`auth.apply` 的唯一呼叫點：continue 回 False，回應／關閉回 True。

            ``log_path`` 必須**不含 query**；原文不可信時呼叫端傳固定 ``-``。
            """
            action = auth_mod.apply(
                self._framing_mode, verdict, route=template, log=self._auth_log,
                close_on_warn=close_on_warn)
            if action.kind == auth_mod.ACTION_CONTINUE:
                return False
            if action.kind == auth_mod.ACTION_CLOSE:
                self.close_connection = True
                return True
            _LOG.info("%s %s -> %s", method, log_path, action.status)
            _send_json(self, action.status, action.error.body(), headers=action.headers)
            return True

        def _dispatch_gated(self, method: str):
            """warn／enforce：原文 → 路由 → /healthz → framing → 讀 body → gate → service。"""
            fr = framing.inspect(self.requestline, self.headers)
            if not fr.target_ok:
                # 原文不是 origin-form：路由無從判斷（判讀原文先於路由）。
                split = urlsplit(self.path)
                path, query = split.path, split.query
                route = _match_route(method, path)
                if route is not None and route[0] == "healthz":
                    # 中-3（C-609）：用 D1 的來源比對路由；非 origin-form 的 /healthz
                    # （//healthz、絕對 URI）仍是健康請求：不經 framing、不取號誌、不
                    # 呼叫 gate、不記認證日誌，與 off 同一條路。
                    self._respond(method, path, route, query)
                    return
                # 原文不可信，access log 的 path 也記固定 -（中-1-3）。
                if self._apply_verdict(
                        auth_mod.denied(auth_mod.REASON_FRAMING), "-",
                        method=method, log_path="-"):
                    return
                # warn：記 would-deny（mode=warn outcome=deny）後照舊處理
                # （路由與 body 都退回 D1 的來源）。
            else:
                path, query = fr.path, fr.query
            route = _match_route(method, path)
            if route is not None and route[0] == "healthz":
                # C-101（J-1）：/healthz 三種模式都走 D1，不套 framing、不取號誌、不呼叫
                # gate、不記認證日誌（C-601）。
                self._respond(method, path, route, query)
                return
            if route is None:
                # 路由先於 framing：未知路徑維持 404（不看標頭、不進 service）。
                _LOG.info("%s %s -> %s", method, path, 404)
                _send_json(self, 404, not_found().body())
                return
            template = _ROUTE_TEMPLATES[route[0]]
            if fr.target_ok and fr.too_large:
                # C-103：> 1 MiB 照現行 413，三種模式相同。
                _LOG.info("%s %s -> %s", method, path, 413)
                _send_json(self, 413, body_too_large().body())
                return
            if fr.target_ok and not fr.ok:
                if self._apply_verdict(
                        auth_mod.denied(auth_mod.REASON_FRAMING), template,
                        method=method, log_path=path):
                    return
                # warn：記 would-deny（mode=warn outcome=deny）後照舊處理
                # （業務結果不變）。
            length = fr.length
            if length is None:
                # 只可能是 warn（enforce 已拒絕）：照 D1 把壞掉的 CL 當 0。
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
            if length > _MAX_BODY_BYTES:
                # 只可能是 warn 的照舊路徑（D1 也是在讀 body 前先 413）。
                _LOG.info("%s %s -> %s", method, path, 413)
                _send_json(self, 413, body_too_large().body())
                return
            permit = self._framing_sem.acquire(timeout=_PERMIT_WAIT_SECONDS)
            if not permit:
                # C-106：等滿 1 秒仍取不到號誌。
                if self._apply_verdict(
                        auth_mod.unavailable(auth_mod.REASON_BUSY), template,
                        method=method, log_path=path):
                    return
                # warn：記不可判定，照舊處理。
            verdict = None
            try:
                raw = self._read_gated_body(length, template, method, path)
                if raw is None:  # 已回應（enforce 503）或已關閉（warn）
                    return
                # C-601：gate 在號誌持有區段內（含 accept 那一次 SQLite 寫入）。
                # 中-1：只有 permit and fr.ok 時才驗章——號誌取不到或 framing 已判
                # 不合時，那一筆事件已記過，verdict 維持 None 照舊處理（不再於號誌
                # 外驗章、不記第二筆互相矛盾的事件）。
                if permit and fr.ok:
                    verdict = self._authenticate(method, fr, raw)
            finally:
                if permit:
                    self._framing_sem.release()
            if verdict is not None:
                # 號誌先放、再決定要回應什麼（REPORT-D2c-arch §2.2）。
                if self._apply_verdict(verdict, template, method=method, log_path=path):
                    return
            if route[0] == "cooldown":
                try:
                    body = _parse_json_body(raw)
                except ApiError as exc:
                    _LOG.info("%s %s -> %s", method, path, exc.status)
                    _send_json(self, exc.status, exc.body())
                    return
            else:
                body = _UNREAD
            self._respond(method, path, route, query, body)

        def _read_gated_body(self, length: int, template: str,
                             method: str, path: str):
            """在總期限內讀 body（C-105）；回原始 bytes，已回應／已關閉時回 None。"""
            try:
                return framing.read_body(self.rfile, length, self._framing_deadline,
                                         sock=self.connection)
            except (framing.ReadDeadline, framing.ShortBody):
                # 逾時／斷線：enforce 回 503，warn 關閉連線（body 沒讀完，無法照舊
                # 處理）。log 只記不含 query 的 path（中-1-3）。
                self._apply_verdict(
                    auth_mod.unavailable(auth_mod.REASON_FRAMING), template,
                    method=method, log_path=path, close_on_warn=True)
                return None

        def _respond(self, method: str, path: str, route, query: str, body=_UNREAD):
            """路由 → service → 回應（含錯誤對應與 log）。off 與 gated 共用。"""
            try:
                if route[0] == "healthz":
                    # 不碰 service 的任何屬性、不讀 body、不寫 DB、不進稽核。
                    status, payload = 200, {"status": "ok"}
                elif route[0] == "list":
                    status, payload = 200, service.list_subscriptions()
                elif route[0] == "usage":
                    status, payload = 200, service.list_usage_summary()
                elif route[0] == "resolve":
                    q = _resolve_query(query)
                    payload = service.resolve(
                        q.get("agent"), q.get("provider"),
                        model=q.get("model"),
                        subscription=q.get("subscription"),
                        exclude=q.get("exclude"),
                        data=q.get("data"),
                    )
                    status = 200
                elif route[0] == "roles":
                    q = _roles_query(query)
                    payload = service.role_candidates(
                        route[1], q.get("agent"), q.get("data"))
                    status = 200
                else:
                    if body is _UNREAD:
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
            elif status == 200 and route[0] == "resolve":
                _send_json(self, status, payload, no_cache=True)
            else:
                _send_json(self, status, payload)

    return ApiHandler


def make_api_server(service, host: str, port: int, *, gate=None,
                    auth_log=None) -> ThreadingHTTPServer:
    """建 /v1/* 的 API 伺服器（尚未 serve_forever）。

    ``gate`` 是 ``off``／``warn``／``enforce`` 的物件（讀 ``.mode``）；``None`` 等於
    off，行為與 D1 逐 byte 相同（#222 D2 PR-1；C-101／C-108）。warn／enforce 的
    gate **必須**有可呼叫的 ``.authenticate``（`lgwd.auth.AuthGate`）；只有 ``.mode``
    的物件會在這裡丟 ``ValueError``（fail-closed：接錯物件時建不起伺服器，而不是
    enforce 靜默放行；#262 中-2）。``/healthz`` 三種模式都不套 framing、不呼叫 gate。
    號誌上限（C-106）掛在回傳的伺服器上 ``framing_semaphore``；``auth_log`` 是
    C-607 的聚合（``None`` 就建一個寫 ``lgwd.http``）。off 只在這裡記一行
    ``auth mode=off``（C-607）。
    """
    mode = _gate_mode(gate)
    if mode in ("warn", "enforce") and not callable(getattr(gate, "authenticate", None)):
        # 中-2：安全閘門的預設必須是關，不是開；接錯物件要當場失敗。
        raise ValueError("gate must provide authenticate() in warn/enforce mode")
    semaphore = threading.BoundedSemaphore(_FRAMING_PERMITS)
    if auth_log is None:
        auth_log = auth_mod.AuthLog(sink=_LOG.info)
    if mode == "off":
        _LOG.info("auth mode=off")
    server = ThreadingHTTPServer(
        (host, port), _api_handler(service, mode, semaphore, gate, auth_log))
    server.daemon_threads = True  # 測試結束不卡住
    server.framing_semaphore = semaphore
    server.auth_log = auth_log
    return server
