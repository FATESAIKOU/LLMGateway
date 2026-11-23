"""兩個探針：本機的 ``/healthz``（curl）與經會合點的探針（``llmgateway-tunnel
--probe-once``）。

check、status、cli、install／upgrade／rollback／tunnel-apply 與 migrate 共用這裡。
回三態（``"ok"``／``"todo"``／``"fail"``）與一行 detail；detail 只有 http 碼、rc
與種類，沒有設定值。

**模式一律由呼叫方明確傳入**（``mode`` 是必填的關鍵字參數，沒有預設值）：llmgwctl
依「探的是哪一版」向 :mod:`lgwctl.capability` 取模式（``current`` 或某個 tag
目錄），``rollback-to-user`` 探的是 user 層舊服務，一律寫死 ``old``。

判法（#246 的 P-3 矩陣）：

| 模式＼``/healthz`` | 200 | 404 | 401／503／其他 | 逾時／連不上 |
|---|---|---|---|---|
| ``new`` | 健康（不打 list） | 失敗（**不**打 list） | 失敗 | 失敗 |
| ``old`` | 健康（不打 list） | 打 list：200 才健康 | 失敗 | 失敗 |

``new`` 對舊服務會一直失敗，所以只有旗標確定是 ``ok`` 時才用它；其他情況由
:mod:`lgwctl.capability` 降為 ``old``。

往隧道腳本傳模式**不用 argv**，用環境變數 ``LGW_PROBE_API_MODE``：舊腳本的
``main()`` 遇到不認得的 argv 會直接起 daemon（會多出第二條隧道搶 4710／4711），
而環境變數舊腳本會忽略、照舊探 list——正好等同 ``old``。
"""

from __future__ import annotations

import os
import re
import time

from .layout import ENV_TUNNEL_ENV
from .run import PROBE_TIMEOUT, RunError, run

HEALTH_PATH = "/healthz"
LEGACY_PATH = "/v1/subscriptions"
# 傳給隧道腳本的模式（K-7）；argv 維持恰好 [probe, "--probe-once"]。
ENV_PROBE_API_MODE = "LGW_PROBE_API_MODE"
MODES = ("old", "new")
# 兩次 curl 各有自己的逾時（R-6）：old 退回時第二次 curl 才不會被第一次的
# 預算吃掉，也不會把逾時算進同一個 run() 裡。
CURL_TIMEOUT = 5
CURL_RUN_TIMEOUT = 10


def _check_mode(mode) -> str:
    if mode not in MODES:
        raise ValueError(f"mode 只能是 old 或 new：{mode!r}")
    return mode


def _curl_code(layout, path: str) -> tuple[str | None, str, bool]:
    """對本機 API 跑一次 curl；回 (http 碼或 None, 說明, 是否「無法確認」)。

    ``code is None`` 表示這次拿不到碼；第三個元素 True 代表「無法確認」（找不到
    curl，對應 todo），False 代表確定失敗（fail）。
    """
    url = f"http://127.0.0.1:{layout.api_port()}{path}"
    argv = [layout.curl, "-s", "-o", os.devnull, "-w", "%{http_code}",
            "--max-time", str(CURL_TIMEOUT), url]
    try:
        proc = run(argv, env=layout.child_env(), timeout=CURL_RUN_TIMEOUT)
    except RunError as exc:
        if exc.kind == "missing":
            return None, f"沒有 {layout.curl}，無法探", True
        return None, f"curl 跑不起來（{exc.kind}）", False
    if proc.returncode != 0:
        return None, f"curl rc={proc.returncode}（連不上或逾時）", False
    code = (proc.stdout or "").strip()
    if not re.fullmatch(r"[0-9]{3}", code):
        return None, "回應無效", False
    return code, "", False


def local_code(layout, *, mode: str) -> tuple[str, str]:
    """本機探針：先 ``GET /healthz``，只有 old 且拿到 404 才退回 list。

    回 (三態, detail)。連不上或逾時算 fail（unit 那項會先講清楚是不是服務沒跑）；
    找不到 curl 算 todo（無法確認）。
    """
    _check_mode(mode)
    code, problem, unknown = _curl_code(layout, HEALTH_PATH)
    if code is None:
        return ("todo" if unknown else "fail"), problem
    if code == "200":
        return "ok", "healthz 200"
    if code != "404":
        return "fail", f"healthz 回 {code}"
    if mode == "new":
        # new 的服務一定認得 /healthz；404 就是不對，不退回 list。
        return "fail", "healthz 回 404（new，不退回 list）"
    legacy, problem, unknown = _curl_code(layout, LEGACY_PATH)
    if legacy is None:
        return ("todo" if unknown else "fail"), problem
    if legacy == "200":
        return "ok", "list 200（old 退回）"
    return "fail", f"healthz 回 404，list 回 {legacy}"


def local_ok(layout, *, mode: str) -> bool:
    level, _ = local_code(layout, mode=mode)
    return level == "ok"


def wait_local_ok(layout, seconds: float, *, mode: str) -> bool:
    """等本機探針回 200，最多 seconds 秒（0＝只試一次）；成功回 True。"""
    _check_mode(mode)
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if local_ok(layout, mode=mode):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def _probe_argv(layout) -> list | None:
    """隧道探針的 argv：**恰好** ``[probe, "--probe-once"]``（或經 python3）。"""
    probe = layout.tunnel_bin
    if not os.path.isfile(probe):
        return None
    if os.access(probe, os.X_OK):
        return [probe, "--probe-once"]
    return [layout.python3, probe, "--probe-once"]


def _probe_env(layout, mode: str) -> dict:
    return layout.child_env(HOME=layout.service_home,
                            **{ENV_TUNNEL_ENV: layout.tunnel_env_file,
                               ENV_PROBE_API_MODE: mode})


def remote_rc(layout, *, mode: str, timeout: float | None = None) -> int | None:
    """經會合點探針的原始結束碼（0／1／2…）；找不到或跑不起來回 None。

    給需要區分 rc=2（無法探）與 rc=1（不是 200）的呼叫方（tunnel-apply）。
    """
    _check_mode(mode)
    argv = _probe_argv(layout)
    if argv is None:
        return None
    try:
        return run(argv, env=_probe_env(layout, mode),
                   timeout=PROBE_TIMEOUT if timeout is None else timeout).returncode
    except RunError:
        return None


def remote(layout, *, mode: str) -> tuple[str, str]:
    """經會合點的探針：用當版的 ``llmgateway-tunnel --probe-once``（0／1／2）。"""
    _check_mode(mode)
    argv = _probe_argv(layout)
    if argv is None:
        return "todo", "找不到當版的 llmgateway-tunnel"
    try:
        proc = run(argv, env=_probe_env(layout, mode), timeout=PROBE_TIMEOUT)
    except RunError as exc:
        if exc.kind == "missing":
            return "todo", f"探針跑不起來（找不到 {argv[0]}）"
        return "todo", f"探針跑不起來（{exc.kind}）"
    if proc.returncode == 0:
        return "ok", "經會合點 200"
    if proc.returncode == 1:
        return "fail", "經會合點不是 200 或沒回應"
    if proc.returncode == 2:
        return "todo", "無法探"
    return "todo", f"探針無法執行（rc={proc.returncode}）"
