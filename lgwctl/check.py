"""check：一次看完整套（#35 spec）。每項一行 ``ok|todo|fail <名稱>``。

項目與判法：

- ``llmgateway.service``／``llmgateway-tunnel.service``：``systemctl is-active``
  （system 層，不需要任何環境變數）；隧道那條在「設定還沒填」時算 todo
  （本人才要動），填了就照實 fail。
- ``tag``（只有 ``--tag`` 時）：裝的版本是不是指定的那個。
- ``list-local``：本機 ``GET /v1/subscriptions`` 要 200。
- ``list-remote``：經會合點的探針（``llmgateway-tunnel --probe-once``）；
  ``--no-remote`` 時整項不列。
- ``keysync``：``lgwd`` 寫的 ``.keysync.status``（T2 的格式）：不存在→todo
  （key 拉取可能被關閉或還沒跑過）；``ok:false``→fail；``ok:true`` 但 ``ts``
  超過 3 個 interval（預設 30 分鐘，``LLMGW_KEYSYNC_STALE`` 可調）沒更新→fail。
  順便印 ``keys.env`` 年齡（只看時間，不看值）。
- ``policy``：當版 ``policy/default.json`` dry-run 載入（fail-closed）。
- ``gh-keys-repo``：以服務帳號身分問 ``gh`` 讀不讀得到私有 repo（只看 repo，
  不讀內容、不讀值）。

結束碼：0 全好、1 有 fail、2 只有 todo。輸出絕不含 key 值或密碼。
"""

from __future__ import annotations

import json
import os
import time

from . import probe, units
from . import run as runmod
from .layout import KEYS_REPO, TUNNEL_SERVICE_NAME, UNIT_NAMES
from .report import Report


def _age_text(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(seconds / 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _keys_age(path: str) -> float | None:
    """keys.env 的年齡（秒）；讀不到回 None。"""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _check_units(layout, report) -> None:
    tunnel_ready, _ = layout.tunnel_config_state()
    for unit in UNIT_NAMES:
        state = units.unit_state(layout, unit)
        if state == "active":
            report.ok(unit)
        elif state == "unknown":
            report.todo(unit, f"{layout.systemctl} 不能用，無法確認")
        elif unit == TUNNEL_SERVICE_NAME and not tunnel_ready:
            report.todo(unit, "沒在跑（隧道設定未填，屬本人動作）")
        else:
            report.fail(unit, "沒在跑")


def _check_tag(layout, report, tag: str) -> None:
    installed = layout.installed_tag()
    if installed == tag:
        report.ok("tag", installed)
    else:
        report.fail("tag", f"裝的是 {installed or '（沒有）'}，要 {tag}")


def _check_list_local(layout, report) -> None:
    level, detail = probe.local_code(layout)
    report.add(level, "list-local", detail)


def _check_list_remote(layout, report) -> None:
    level, detail = probe.remote(layout)
    report.add(level, "list-remote", detail)


def _check_keysync(layout, report) -> None:
    """``.keysync.status``（lgwd 寫的）：不存在→todo；ok:false→fail；
    ok:true 但 ts 超過 ``keysync_stale``（預設 30 分鐘，3 個 interval）→fail。"""
    status_path = layout.keysync_status_file
    if not os.path.isfile(status_path):
        report.todo("keysync", "狀態檔不在（key 拉取可能被關閉，或還沒跑過）")
        return
    try:
        with open(status_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        report.fail("keysync", "狀態檔讀不懂")
        return
    if not isinstance(data, dict):
        report.fail("keysync", "狀態檔讀不懂")
        return

    ts = data.get("ts")
    ts_known = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
    details: list[str] = []
    if ts_known is not None:
        details.append(f"最後一次 {_age_text(time.time() - ts_known)}前")
    if os.path.isfile(layout.keys_env):
        details.append(
            f"{os.path.basename(layout.keys_env)} {_age_text(_keys_age(layout.keys_env))}前"
        )
    else:
        details.append(f"{os.path.basename(layout.keys_env)} 不在")
    tail = "（" + "、".join(details) + "）" if details else ""

    if data.get("ok") is not True:
        reason = str(data.get("reason") or "unknown")
        report.fail("keysync", f"最後一次失敗：{reason}{tail}")
        return
    if ts_known is None:
        report.fail("keysync", f"狀態檔說成功但沒有時間戳{tail}")
        return
    age = time.time() - ts_known
    if age > layout.keysync_stale:
        report.fail("keysync",
                    f"狀態檔 {_age_text(age)}沒更新（拉取可能停了；"
                    + "、".join(details) + "）")
        return
    if not os.path.isfile(layout.keys_env):
        report.fail("keysync", f"狀態說成功，但 keys.env 不在{tail}")
        return
    report.ok("keysync", "、".join(details))


def _check_policy(layout, report) -> None:
    policy_path = os.path.join(layout.current_link, "policy", "default.json")
    try:
        from lgwd.policy import PolicyError, load_policy
    except ImportError:
        report.fail("policy", "載不到 lgwd.policy（cwd 不在版本目錄？）")
        return
    try:
        load_policy(policy_path)
    except PolicyError as exc:
        report.fail("policy", f"載入失敗：{exc}")
    except Exception as exc:  # 未預期的錯誤也只記種類
        report.fail("policy", f"載入失敗：{type(exc).__name__}")
    else:
        report.ok("policy")


def _check_gh_repo(layout, report) -> None:
    argv = runmod.service_user_argv(layout, [layout.gh, "api", f"repos/{KEYS_REPO}",
                                              "--jq", ".full_name"])
    try:
        proc = runmod.run(argv, env=layout.child_env())
    except runmod.RunError as exc:
        if exc.kind == "missing":
            report.todo("gh-keys-repo", f"找不到 {layout.gh}")
        else:
            report.todo("gh-keys-repo", f"無法確認（{exc.kind}）")
        return
    stderr = proc.stderr or ""
    if proc.returncode == 0:
        name = runmod.trimmed(proc.stdout)
        if name == KEYS_REPO:
            report.ok("gh-keys-repo", KEYS_REPO)
        else:
            report.fail("gh-keys-repo", f"回的不是 {KEYS_REPO}")
        return
    low = stderr.lower()
    if proc.returncode == 4 or "gh auth login" in low or "bad credentials" in low \
            or "not logged in" in low or "authentication" in low:
        report.todo("gh-keys-repo", "gh 沒登入（要本人以服務帳號登入）")
        return
    if "http 403" in low or "http 404" in low:
        report.fail("gh-keys-repo", f"讀不到 {KEYS_REPO}（登入的帳號沒有讀權限）")
        return
    if runmod.sudo_failed(stderr) or proc.returncode == 127:
        report.todo("gh-keys-repo", "無法以服務帳號確認（sudo 失敗或找不到）")
        return
    report.todo("gh-keys-repo", f"無法確認（gh rc={proc.returncode}）")


def run(layout, *, report: Report | None = None, no_remote: bool = False,
        tag: str | None = None) -> int:
    report = report or Report()
    _check_units(layout, report)
    if tag:
        _check_tag(layout, report, tag)
    _check_list_local(layout, report)
    if not no_remote:
        _check_list_remote(layout, report)
    _check_keysync(layout, report)
    _check_policy(layout, report)
    _check_gh_repo(layout, report)
    return report.exit_code()
