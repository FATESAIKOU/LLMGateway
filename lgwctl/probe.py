"""兩個探針：本機的 list（curl）與經會合點的 list（llmgateway-tunnel --probe-once）。

check 與 upgrade／rollback 共用這裡；回三態（"ok"／"todo"／"fail"）與一行
detail。detail 只有 http 碼、rc 與種類，沒有設定值。
"""

from __future__ import annotations

import os
import time

from .layout import ENV_TUNNEL_ENV
from .run import PROBE_TIMEOUT, RunError, run

PROBE_PATH = "/v1/subscriptions"


def local_code(layout) -> tuple[str, str]:
    """本機 ``GET /v1/subscriptions``：回 (三態, detail)。

    連不上或逾時算 fail（unit 那項會先講清楚是不是服務沒跑）；找不到 curl 算
    todo（無法確認）。
    """
    url = f"http://127.0.0.1:{layout.api_port()}{PROBE_PATH}"
    argv = [layout.curl, "-s", "-o", os.devnull, "-w", "%{http_code}",
            "--max-time", "5", url]
    try:
        proc = run(argv, env=layout.child_env(), timeout=10)
    except RunError as exc:
        if exc.kind == "missing":
            return "todo", f"沒有 {layout.curl}，無法探"
        return "fail", f"curl 跑不起來（{exc.kind}）"
    code = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return "fail", f"curl rc={proc.returncode}（連不上或逾時）"
    if code == "200":
        return "ok", "200"
    return "fail", f"回 {code or '（空）'}"


def local_ok(layout) -> bool:
    level, _ = local_code(layout)
    return level == "ok"


def wait_local_ok(layout, seconds: float) -> bool:
    """等本機 list 回 200，最多 seconds 秒（0＝只試一次）；成功回 True。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if local_ok(layout):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def remote(layout) -> tuple[str, str]:
    """經會合點的探針：用當版的 ``llmgateway-tunnel --probe-once``（0／1／2）。"""
    probe = layout.tunnel_bin
    if not os.path.isfile(probe):
        return "todo", "找不到當版的 llmgateway-tunnel"
    env = layout.child_env(HOME=layout.service_home,
                           **{ENV_TUNNEL_ENV: layout.tunnel_env_file})
    if os.access(probe, os.X_OK):
        argv = [probe, "--probe-once"]
    else:
        argv = [layout.python3, probe, "--probe-once"]
    try:
        proc = run(argv, env=env, timeout=PROBE_TIMEOUT)
    except RunError as exc:
        if exc.kind == "missing":
            return "todo", f"探針跑不起來（找不到 {argv[0]}）"
        return "todo", f"探針跑不起來（{exc.kind}）"
    if proc.returncode == 0:
        return "ok", "經會合點 200"
    if proc.returncode == 1:
        return "fail", "經會合點不是 200 或沒回應"
    if proc.returncode == 2:
        return "todo", "無法探（隧道設定未填或找不到 ssh）"
    return "todo", f"探針無法執行（rc={proc.returncode}）"
