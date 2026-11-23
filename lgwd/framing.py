"""預驗章 HTTP framing（#222 D2 PR-1；C-101～C-108 的判讀面）。

- ``inspect(requestline, headers) -> Framing``：**純函式**。request-target 取
  ``requestline`` 以空白切開的第二段**原文**（不用 stdlib 改寫過的 ``self.path``；
  Python 3.12 起 ``http.server`` 會把開頭的 ``//`` 併成 ``/``，gh-87389）。只收
  origin-form：以 ``/`` 開頭但不是 ``//``、全 ASCII 0x21–0x7E、不含 ``#``；絕對
  URI、authority-form、``*``、沒有版本欄的 HTTP/0.9 一律拒絕（C-102）。
- 標頭名稱：必須符合 RFC 7230 token（``tchar``），空白（含冒號前空白）、控制
  字元或其他分隔符一律算 framing 不合。``email.parser`` 會把
  ``Transfer-Encoding : chunked`` 解析成名為 ``"Transfer-Encoding "`` 的標頭，
  使得 ``get_all("Transfer-Encoding")`` 看不到它；有些版本／形狀則把整行丟掉、
  只在 ``Message.defects`` 留下缺陷。名稱檢查與缺陷檢查兩者都做，不倚賴個別標頭
  的形狀（#252 修正）。
- 標頭：``Content-Length``、``Transfer-Encoding``、``Host`` 與五個 ``X-LGW-*``
  以 ``get_all`` 判各至多一次（不分大小寫，C-104）；``Content-Length`` 須符
  ``^[0-9]+$``（空值、``+5``、``05x`` 都算壞）、GET 不得非零、出現
  ``Transfer-Encoding`` 即拒（C-103）；宣告超過 1 MiB 標 ``too_large``，由
  HTTP 層照現行回 413（三種模式相同）。
- ``read_body(rfile, length, deadline, *, sock)``：讀滿 length bytes，**共用
  一個單調時鐘的總期限**（C-105）：每讀一塊都用「截止時刻 − 現在」當這次 recv
  的逾時，所以逐 byte 慢送也會在期限到時被切斷；不是每次 recv 重新起算。
  ``sock`` 是**必填**的連線 socket（HTTP 層明確傳 ``self.connection``）；不從
  ``rfile`` 的私有屬性推導，拿不到就由呼叫端負責，避免靜默降級。
- 不 import ``lgwd.http``／``lgwd.main``／``lgwd.registry``（沒有環）；只用標準
  函式庫。這裡**不**決定「拒絕之後回什麼」（那是 HTTP 層與 PR-6 的 gate）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

# body 上限（與 lgwd.http 現行相同；超過在讀 body 前就 413）。
MAX_BODY_BYTES = 1024 * 1024

# C-104：這些標頭各至多一次（Message.get_all 不分大小寫）。
SINGLE_HEADERS = (
    "Content-Length",
    "Transfer-Encoding",
    "Host",
    "X-LGW-Auth-Version",
    "X-LGW-Key-Id",
    "X-LGW-Timestamp",
    "X-LGW-Nonce",
    "X-LGW-Signature",
)

_LENGTH_RE = re.compile(r"[0-9]+")
# RFC 7230 token（tchar）：標頭名稱只收這些字元；冒號前空白、控制字元都不在內。
_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_READ_CHUNK = 65536
_PRINTABLE_LOW, _PRINTABLE_HIGH = 0x21, 0x7E


class ReadDeadline(Exception):
    """C-105：總期限到（body 還沒讀完）。"""


class ShortBody(Exception):
    """連線在 body 讀完前就關了。"""


@dataclass(frozen=True)
class Framing:
    """一次請求的 framing 判讀結果。

    - ``target_ok``：request-target 是合法的 origin-form（C-102）；False 時
      ``path``／``query`` 是空的，不能拿去路由。
    - ``target``：``requestline`` 第二段的**原文**（PR-6 簽入八行訊息第 7 行用的
      就是這一份；這裡先原樣留著，不解碼、不經 stdlib 改寫）。
    - ``path``／``query``：取自同一段原文（路由與簽入第 7 行的字串同源）。
    - ``length``：合法的 Content-Length；缺或壞（形狀錯、重複）為 None。
    - ``too_large``：宣告的 body 超過 1 MiB（C-103；HTTP 層回 413，與模式無關）。
    - ``ok``：``target_ok`` 且所有標頭規則都通過、且沒有超過 1 MiB。
    """

    target_ok: bool
    target: str
    path: str
    query: str
    length: int | None
    too_large: bool
    ok: bool


def _parse_requestline(requestline: str):
    """回 (method, target) 或 None（非 origin-form／沒有版本欄／含禁用字元）。"""
    parts = requestline.split(" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not method or not version.startswith("HTTP/"):
        return None
    for char in version:
        if not _PRINTABLE_LOW <= ord(char) <= _PRINTABLE_HIGH:
            return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    for char in target:
        if char == "#" or not _PRINTABLE_LOW <= ord(char) <= _PRINTABLE_HIGH:
            return None
    return method, target


def inspect(requestline: str, headers) -> Framing:
    """判讀一份請求的 framing（純函式；不讀 body、不碰網路）。"""
    parsed = _parse_requestline(requestline)
    if parsed is None:
        target_ok, target, method, path, query = False, "", None, "", ""
    else:
        target_ok = True
        method, target = parsed
        split = urlsplit(target)
        path, query = split.path, split.query

    counts_ok = all(
        len(headers.get_all(name) or ()) <= 1 for name in SINGLE_HEADERS)
    names_ok = all(_TOKEN_RE.fullmatch(name) for name in headers.keys())
    # 名稱不合法的標頭有些會被 email.parser 整行丟掉（例如冒號前空白的
    # ``Transfer-Encoding : chunked``），只在 ``Message.defects`` 留下缺陷；
    # 那同樣代表標頭名稱不是 RFC 7230 token，一律算 framing 不合。
    names_ok = names_ok and not getattr(headers, "defects", ())

    raw_length = headers.get("Content-Length")
    length = None
    if counts_ok and raw_length is not None and _LENGTH_RE.fullmatch(raw_length):
        length = int(raw_length)
    too_large = length is not None and length > MAX_BODY_BYTES

    bad = not names_ok or not counts_ok
    if not bad and raw_length is not None:
        # 有 CL 就必須是純數字（空值、+5、05x 都不行）
        bad = length is None
    if not bad and headers.get_all("Transfer-Encoding"):
        bad = True
    if not bad and length and method == "GET":
        bad = True  # GET 不得有非零 body

    return Framing(
        target_ok=target_ok,
        target=target,
        path=path,
        query=query,
        length=length,
        too_large=too_large,
        ok=target_ok and not bad and not too_large,
    )


def read_body(rfile, length: int, deadline: float, *, sock) -> bytes:
    """讀滿 ``length`` bytes；``deadline`` 是**一次性的總期限**（C-105）。

    逾時丟 :class:`ReadDeadline`、連線提前關閉丟 :class:`ShortBody`；兩者都代表
    「body 沒讀完」。``sock`` 是必填的連線 socket（用來設每次 recv 的逾時），
    由 HTTP 層明確傳入；收尾會把連線原本的逾時還原，不給下一個請求留下新逾時。
    """
    if length <= 0:
        return b""
    reader = getattr(rfile, "read1", None)
    if reader is None:  # 不是 BufferedReader 的替身（測試用）
        reader = rfile.read
    original = sock.gettimeout()
    chunks: list[bytes] = []
    remaining = length
    try:
        while remaining > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ReadDeadline()
            sock.settimeout(left)
            try:
                chunk = reader(min(remaining, _READ_CHUNK))
            except TimeoutError:
                raise ReadDeadline() from None
            except OSError:
                raise ShortBody() from None
            if not chunk:
                raise ShortBody()
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        try:
            sock.settimeout(original)
        except OSError:
            pass
    return b"".join(chunks)


__all__ = [
    "Framing",
    "MAX_BODY_BYTES",
    "ReadDeadline",
    "SINGLE_HEADERS",
    "ShortBody",
    "inspect",
    "read_body",
]
