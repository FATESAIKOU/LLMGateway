"""check：一次看完整套（#35 spec）。每項一行 ``ok|todo|fail <名稱>``。

項目與判法：

- ``llmgateway.service``／``llmgateway-tunnel.service``：``systemctl is-active``
  （system 層，不需要任何環境變數）；unit 檔不存在（rc 4）一律 ``fail``（沒裝）；
  隧道那條在「設定還沒填」且還裝著時算 todo（本人才要動），裝了沒跑就照實 fail。
- ``tag``（只有 ``--tag`` 時）：裝的版本是不是指定的那個。
- ``list-local``：本機探針。先 ``GET /healthz`` 要 200；模式是 ``old`` 且得 404
  時才退回 ``GET /v1/subscriptions``（要 200）。模式由 ``current`` 的能力旗標
  （``deploy/capabilities.json``）決定；旗標損毀／讀不到時照樣以 old 探，但等級
  至少 fail／todo，detail 寫明原因。
- ``auth-local``（#222 D4b；C-711）：本機管理 principal 的已簽章 ``list``。
  ``off``（或沒設）→ ok 並註明未啟用認證；``llmgateway.env`` 讀不到 → todo「認證
  模式讀不到」（不是 off）；非 root → todo「需 root 才能做已簽章檢查」且不打業務
  端點；root 時以 :mod:`lgwctl.local_auth` 的私鑰簽章（權限不符就是沒有 signer，
  也不改送無簽）：enforce 的 401／403／429／503 與簽章失敗為 fail、warn 一律
  todo（warn 不拒絕，無法確認）、連不上照實 fail／todo。
- ``auth-degraded``（#288；C-963）：從 enforce 降到 warn／off 滿 3600 秒即 fail；
  紀錄與判定見 :mod:`lgwctl.mode_history`（root 每次順便補記，非 root 只讀）。
- ``check-timer``（#289；C-986）：定期檢查的 timer 是 enabled 而且 active 時 ok，
  否則 todo（未安裝／未啟用／沒在跑分開寫，附下一步指令）；systemctl 不能用時
  todo。只有在這一版的 ``deploy/units/`` 有 timer 檔時才列（更舊的版本沒有這個
  功能）。從不認得 timer 的舊版升級上來的機器會看到「未安裝」，補跑一次
  ``llmgwctl install`` 即可。**#296**：這一版有 notify unit 檔（見下一項）而系統上
  沒有那個檔時，timer 就算在跑也記 todo（安裝指令同上）。
- ``check-notify``（#296；C-996）：check 失敗時的通知有沒有設定好。只會 ok 或
  todo（不會 fail）：``/etc/llmgateway/check-notify.env`` 的三個鍵齊全、找得到
  ``aicomm`` 就是 ok，否則 todo「失敗不會通知 PMO（缺 check-notify.env 或
  aicomm）」。只有這一版的 ``deploy/units/`` 有 notify unit 檔時才列。不送訊息。
- ``list-remote``：經會合點的探針（``llmgateway-tunnel --probe-once``，模式以
  環境變數 ``LGW_PROBE_API_MODE`` 傳）；``--no-remote`` 時整項不列。
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
import re
import shutil
import time

from . import capability, local_auth, mode_history, probe, units
from . import run as runmod
from .layout import (CHECK_NOTIFY_AICOMM, CHECK_NOTIFY_ENV_KEYS, CHECK_NOTIFY_NAME,
                     CHECK_TIMER_NAME, ENV_CHECK_JOURNAL, KEYS_REPO,
                     SERVICE_NAME, TUNNEL_SERVICE_NAME, parse_env_file)
from .report import Report

# .keysync.status 的 reason 白名單（低-8）：writer（lgwd/keysync.py、sync.py）
# 只會寫這些形狀；檔案在 llmgw 可寫目錄，被改過的內容不能原樣印到 root 的
# 終端／log（ANSI escape、控制字元、或把已知 key 值混進來）。
_REASON_LITERALS = ("ok", "timeout", "spawn_failed", "unknown")
_REASON_RE = re.compile(r"^(exit_[0-9]{1,10}|signal_[0-9]{1,10}|manual_sync rc=[0-9]{1,10})$")


def _safe_reason(value) -> str:
    """``reason`` 過白名單；不認得一律顯示 ``unknown``（內容不印出）。"""
    text = str(value or "")
    if text in _REASON_LITERALS or _REASON_RE.match(text):
        return text
    return "unknown"


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


def _check_units(layout, report, *, no_remote=False) -> tuple[bool, str]:
    core = units.unit_state(layout, SERVICE_NAME)
    if core == "active":
        report.ok(SERVICE_NAME)
    elif core == "unknown":
        report.todo(SERVICE_NAME, "systemctl 不能用，無法確認")
    else:
        report.fail(SERVICE_NAME, "unit 不在（沒裝）" if core == "missing" else "沒在跑")
    if no_remote:
        return False, "missing"
    state = units.unit_state(layout, TUNNEL_SERVICE_NAME)
    configured = layout.tunnel_configured()
    present = state != "missing" or configured or layout.tunnel_follow_state()[0] == "unreadable" or os.path.isfile(layout.unit_file(TUNNEL_SERVICE_NAME))
    if not present:
        return False, state
    follow, _ = layout.tunnel_follow_state()
    if state == "active":
        report.ok(TUNNEL_SERVICE_NAME)
    elif follow == "unreadable" or (layout.tunnel_follow_configured() and layout.tunnel_fetch_state() == "unreadable"):
        report.todo(TUNNEL_SERVICE_NAME, "需 root 或 llmgw 才能讀")
    elif state == "unknown":
        report.todo(TUNNEL_SERVICE_NAME, "systemctl 不能用，無法確認")
    elif not configured:
        report.todo(TUNNEL_SERVICE_NAME, "沒在跑（隧道設定未填）")
    else:
        report.fail(TUNNEL_SERVICE_NAME, "unit 不在（沒裝）" if state == "missing" else "沒在跑")
    return True, state


def _check_follow(layout, report, active):
    follow, gen = layout.tunnel_follow_state()
    fetch = layout.tunnel_fetch_state()
    if follow == "unconfigured" and fetch == "none":
        return
    if follow == "unconfigured":
        pass
    elif follow == "unreadable" or fetch == "unreadable":
        report.todo("tunnel-follow", "unreadable（需 root 或 llmgw 才能讀）")
    elif follow in ("missing", "invalid", "regressed"):
        reason = {"missing": "follow 檔缺失", "invalid": "follow 檔無效", "regressed": "follow 檔回退"}[follow]
        if active:
            report.todo("tunnel-follow", reason + "，現存連線靠最後已知位址維持；需人工確認 NODE_GATEWAY")
        else:
            report.fail("tunnel-follow", reason + "，請人工確認 NODE_GATEWAY；確認回退後執行 llmgwctl follow-reset")
    else:
        report.ok("tunnel-follow", "ok (gen=" + (str(gen) if gen is not None else "pending") + ")")
    rejected = {"regressed", "invalid", "hostkey_missing", "auth_failed"}
    if fetch == "auth_failed":
        detail = "auth_failed：token 可能失效，請跑 llmgateway-tunnel --fetch-check，換好 token 後執行 llmgwctl follow-reset --fetch-state"
        (report.todo if active else report.fail)("tunnel-fetch", detail)
    elif fetch in rejected:
        detail = fetch + "：最近抓取被拒，現存連線靠最後已知位址維持，需人工確認 NODE_GATEWAY；確認回退後執行 llmgwctl follow-reset"
        (report.todo if active else report.fail)("tunnel-fetch", detail)
    elif fetch == "config_partial":
        report.todo("tunnel-fetch", "config_partial：GitHub 抓取設定半套，僅跑靜態設定")
    elif fetch == "credential_unreadable":
        report.todo("tunnel-fetch", "credential_unreadable：檢查金鑰檔路徑、權限或 drop-in LoadCredential")
    elif fetch == "unreadable":
        report.todo("tunnel-fetch", "unreadable（需 root 或 llmgw 才能讀）")
    else:
        report.ok("tunnel-fetch", fetch)
    token = layout.tunnel_token_state()
    if token == "expiry_invalid":
        reason = "TOKEN_EXPIRES_AT 格式不合" if layout.tunnel_values().get("TOKEN_EXPIRES_AT") else "token 到期資訊無效"
        report.todo("tunnel-token", reason)
    elif token == "expiry_due":
        report.todo("tunnel-token", "token 即將到期或已到期")
    elif token == "unreadable":
        report.todo("tunnel-token", "unreadable（需 root 或 llmgw 才能讀）")
    else:
        report.ok("tunnel-token", token)
    key = layout.tunnel_values().get("GATEWAY_GITHUB_KEY_FILE")
    if key:
        try:
            os.stat(key)
        except PermissionError:
            report.todo("tunnel-credential", "需權限確認金鑰檔")
        except OSError:
            report.todo("tunnel-credential", "檢查金鑰檔路徑、權限或 drop-in LoadCredential")


def _version_unit(layout, name: str) -> str:
    """當版目錄裡的 unit 檔路徑（判斷「這一版有沒有這個 unit」）。"""
    return os.path.join(layout.current_link, "deploy", "units", name)


def _check_timer(layout, report) -> None:
    """``check-timer``（#289；C-986）：定時檢查的 timer 裝了而且正在跑嗎。

    - enabled 而且 active → ok（#296：這一版有 notify unit 檔、系統上卻沒有時仍
      記 todo「未安裝」＋安裝指令）；
    - systemctl 不能用（跑不起來／結束碼不是 0、1、4）→ todo（無法確認）；
    - 確定沒裝（is-enabled 說 missing，或 unit 檔不在）→ todo「未安裝」＋安裝指令；
    - 已安裝但沒 enable → todo「已安裝但未啟用」＋enable 指令；
    - 已 enable 但沒在跑（例如維護期間 stop）→ todo「已安裝但沒在跑」＋start 指令。

    只有在**這一版**的 ``deploy/units/`` 有 timer 檔時才列這一項：更舊的版本
    根本沒有定時檢查，列 todo 會讓「遷移到沒有 timer 的舊 tag」被誤判停損；
    升級上來的新版目錄一定有這個檔，所以該看到的「未安裝」不會被跳過。
    """
    if not os.path.isfile(_version_unit(layout, CHECK_TIMER_NAME)):
        return
    enabled = units.enabled_state(layout, CHECK_TIMER_NAME)
    active = units.unit_state(layout, CHECK_TIMER_NAME)
    if enabled == "unknown" or active == "unknown":
        report.todo("check-timer", "systemctl 不能用，無法確認")
        return
    if enabled == "enabled" and active == "active":
        # #296：這一版有失敗通知的 unit 檔，但系統上沒有（從 #300 的版本升級上來，
        # 跑升級的是舊版 lgwctl，它的 unit 清單沒有 notify unit）→ 補跑 install。
        if (os.path.isfile(_version_unit(layout, CHECK_NOTIFY_NAME))
                and not os.path.isfile(layout.unit_file(CHECK_NOTIFY_NAME))):
            report.todo("check-timer",
                        f"失敗通知的 {CHECK_NOTIFY_NAME} 未安裝"
                        "（sudo llmgwctl install --tag <tag>）")
            return
        report.ok("check-timer", CHECK_TIMER_NAME)
        return
    if enabled == "missing" or not os.path.isfile(layout.unit_file(CHECK_TIMER_NAME)):
        report.todo("check-timer", "定時檢查未安裝（sudo llmgwctl install --tag <tag>）")
        return
    if enabled != "enabled":
        report.todo("check-timer",
                    "定時檢查已安裝但未啟用（sudo systemctl enable --now "
                    f"{CHECK_TIMER_NAME}）")
        return
    report.todo("check-timer",
                f"定時檢查已安裝但沒在跑（sudo systemctl start {CHECK_TIMER_NAME}）")


def _check_notify(layout, report) -> None:
    """``check-notify``（#296；C-996）：check 失敗時的通知有沒有設定好。

    只會 ok 或 todo（通知沒設好不是「壞掉」，也絕不在此送訊息）：

    - 只有**這一版**的 ``deploy/units/`` 有 notify unit 檔時才列（與 check-timer
      同理：更舊的版本沒有這個功能，列 todo 會讓遷移到舊 tag 被誤判停損）；
    - ``/etc/llmgateway/check-notify.env`` 的三個鍵（頻道、收件人、本機代號）
      齊全，而且 ``PATH`` 上找得到 ``aicomm`` → ok；
    - 否則 todo，寫明缺的是設定檔還是 aicomm（不含任何值）。
    """
    if not os.path.isfile(_version_unit(layout, CHECK_NOTIFY_NAME)):
        return
    values = parse_env_file(layout.check_notify_env)
    missing = [key for key in CHECK_NOTIFY_ENV_KEYS
               if not (values.get(key) or "").strip()]
    if missing:
        report.todo("check-notify",
                    "失敗不會通知 PMO（缺 check-notify.env 或 aicomm）："
                    "check-notify.env 缺 " + "、".join(missing))
        return
    if shutil.which(CHECK_NOTIFY_AICOMM, path=layout.env.get("PATH")) is None:
        report.todo("check-notify",
                    "失敗不會通知 PMO（缺 check-notify.env 或 aicomm）："
                    f"找不到 {CHECK_NOTIFY_AICOMM}")
        return
    report.ok("check-notify", "設定齊全")


def _check_tag(layout, report, tag: str) -> None:
    installed = layout.installed_tag()
    if installed == tag:
        report.ok("tag", installed)
    else:
        report.fail("tag", f"裝的是 {installed or '（沒有）'}，要 {tag}")


def _check_list_local(layout, report) -> None:
    """本機探針；模式的來源是 ``current`` 的能力旗標（#246）。

    旗標 ``invalid``／``unreadable`` 時照樣以 ``old`` 探（old 對新舊服務都判得
    活），但等級至少 fail（損毀）或 todo（讀不到），detail 寫明原因，免得損毀的
    旗標被一個 ok 蓋掉。項目名維持 ``list-local``（D1 不改輸出介面）。
    """
    cap = capability.current(layout)
    level, detail = probe.local_code(layout, mode=cap.mode)
    if cap.state == "invalid":
        report.fail("list-local", f"能力旗標損毀（以 old 探：{detail}）")
        return
    if cap.state == "unreadable":
        if level == "ok":
            report.todo("list-local", f"能力旗標讀不到（以 old 探：{detail}）")
        else:
            report.add(level, "list-local", f"能力旗標讀不到（以 old 探：{detail}）")
        return
    report.add(level, "list-local", detail)


def _check_list_remote(layout, report) -> None:
    level, detail = probe.remote(layout, mode=capability.current(layout).mode)
    report.add(level, "list-remote", detail)


def _auth_failure_text(result) -> str:
    """已簽章 list 失敗的一句話（固定分類；不含金鑰、簽章或 nonce）。"""
    if result.error == "signer_unavailable":
        reason = result.data.get("reason") if isinstance(result.data, dict) else None
        return "簽章失敗（未送出請求）：{0}".format(reason or "signer_error")
    if result.status == 401:
        return "已簽章 list 回 401 unauthorized（管理 principal 未登記、已撤銷或時鐘偏差）"
    if result.status == 403:
        return "已簽章 list 回 403 forbidden（管理 principal 沒有 list 權限）"
    if result.status == 429:
        return "已簽章 list 回 429 rate_limited"
    if result.status == 503:
        return "已簽章 list 回 503 auth_unavailable（驗章工具或庫不可用）"
    return "已簽章 list 失敗（status={0}, error={1}）".format(result.status, result.error)


def _check_auth_local(layout, report) -> None:
    """``auth-local``（C-711）：以本機管理 principal 簽章打一次 list。

    判定順序（設計上的優先序：off 沒有已簽章檢查這回事，先處理）：

    1. ``off``（或 ``llmgateway.env`` 沒設／空字串）→ ok「未啟用認證」，不碰私鑰；
    2. ``llmgateway.env`` 讀不到 → todo「認證模式讀不到」（不能當成 off）；
    3. 模式讀不懂 → todo（不假裝是 off）；
    4. 非 root → todo「需 root 才能做已簽章檢查」，**不打業務端點**；
    5. 私鑰不合格／讀不到 → enforce 為 fail、warn 為 todo；**不重送、不改送無簽**；
    6. enforce：200 為 ok，401／403／429／503 與連不上為 fail；
    7. warn：一律 todo（warn 不拒絕請求，通過也不代表管理 principal 有效）。
    """
    from lgw.client import Client

    mode = layout.auth_mode()
    if mode == "off":
        report.ok("auth-local", "未啟用認證")
        return
    if mode == "unreadable":
        report.todo("auth-local", "認證模式讀不到（llmgateway.env 需 root 或 llmgw 才讀得）")
        return
    if mode == "unknown":
        report.todo("auth-local", "認證模式讀不懂（llmgateway.env 的 LGWD_AUTH_MODE）")
        return
    if layout.euid != 0:
        report.todo("auth-local", "需 root 才能做已簽章檢查")
        return
    signer, reason = local_auth.build_signer(layout)
    if signer is None:
        # C-710／C-714：沒有 signer 就不簽、不送、不改送無簽。
        text = local_auth.detail(reason, layout.local_admin_key_path)
        if mode == "enforce":
            report.fail("auth-local", text)
        else:
            report.todo("auth-local", text)
        return
    result = Client("http://127.0.0.1:{0}".format(layout.api_port()), signer=signer).list()
    if result.ok:
        if mode == "enforce":
            report.ok("auth-local", "已簽章 list 200")
        else:
            report.todo("auth-local", "warn 模式不拒絕請求，無法確認已簽章呼叫（list 200）")
        return
    if result.unreachable:
        text = "連不上本機 Gateway（unreachable）"
    else:
        text = _auth_failure_text(result)
    if mode == "enforce":
        report.fail("auth-local", text)
    else:
        report.todo("auth-local", text)


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
        reason = _safe_reason(data.get("reason"))
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


def _check_auth_stores(layout, report) -> None:
    """`auth-heartbeat`／`auth-water`／`auth-stores`（#222 D5c；C-828）。

    唯讀：status 檔只讀；兩庫一律經 `lgwd.auth_admin.inspect_*`（它用
    `auth_db.open_ro`），不取 store 鎖、不建檔。``auth-heartbeat`` 看 D2 status 檔
    的心跳是否超過 180 秒；``auth-water`` 把 `H` 在未來（now＋60）的 key 報 fail；
    ``auth-stores`` 報兩庫識別不一致或缺失。非 root 讀不到 llmgw 0600 的庫，後兩項
    記 todo（不是 fail），與 ``auth-local`` 的既有語意一致。
    """
    from lgwd.auth_admin import AdminError, inspect_replay, inspect_stores
    from lgwd.clock import SystemClock
    from .auth_cli import (paths_for, read_status, _StatusBad, _StatusMissing,
                           HEARTBEAT_STALE_SECONDS, _is_root)

    names = ("auth-heartbeat", "auth-water", "auth-stores")
    mode = layout.auth_mode()
    if mode == "off":
        for name in names:
            report.ok(name, "未啟用認證")
        return
    if mode == "unreadable":
        for name in names:
            report.todo(name, "認證模式讀不到（llmgateway.env 需 root 或 llmgw 才讀得）")
        return
    if mode not in ("warn", "enforce"):
        for name in names:
            report.todo(name, "認證模式讀不懂（llmgateway.env 的 LGWD_AUTH_MODE）")
        return
    try:
        paths = paths_for(layout)
    except ValueError:
        for name in names:
            report.fail(name, "認證路徑設定錯誤")
        return
    try:
        status = read_status(paths.status)
        if int(time.time()) - status.heartbeat > HEARTBEAT_STALE_SECONDS:
            report.fail("auth-heartbeat", "心跳逾時")
        else:
            report.ok("auth-heartbeat")
    except (_StatusBad, _StatusMissing):
        report.fail("auth-heartbeat", "status 檔不存在或無法讀取")
    if not _is_root(layout):
        report.todo("auth-water", "需要 root")
        report.todo("auth-stores", "需要 root")
        return
    try:
        replay = inspect_replay(paths, clock=SystemClock())
        if replay.future_keys:
            report.fail("auth-water", "水位在未來：" + "、".join(replay.future_keys))
        else:
            report.ok("auth-water")
    except AdminError as exc:
        report.fail("auth-water", _admin_detail(exc))
    try:
        stores = inspect_stores(paths)
        if stores.identity_match:
            report.ok("auth-stores")
        else:
            report.fail("auth-stores", "認證庫缺失或識別不符")
    except AdminError as exc:
        report.fail("auth-stores", _admin_detail(exc))


def _admin_detail(exc) -> str:
    """`AdminError` 的一行說明（訊息＋固定的下一步；不含任何值）。"""
    text = str(exc)
    hint = getattr(exc, "hint", None)
    return text if not hint else "{0}；{1}".format(text, hint)


def _check_auth_degraded(layout, report) -> None:
    """`auth-degraded`（#288；C-963）：從 enforce 降級滿一小時即 fail。

    判定表與狀態檔在 :mod:`lgwctl.mode_history`；root 每次跑 check 會依
    systemd 的啟動時刻補記，非 root 只讀。
    """
    level, detail = mode_history.check_for_report(layout)
    if level == "ok":
        report.ok("auth-degraded", detail)
    elif level == "todo":
        report.todo("auth-degraded", detail)
    else:
        report.fail("auth-degraded", detail)


def run(layout, *, report: Report | None = None, no_remote: bool = False,
        tag: str | None = None) -> int:
    report = report or Report(journal=(layout.env.get(ENV_CHECK_JOURNAL) == "1"))
    tunnel_present, tunnel_state = _check_units(layout, report, no_remote=no_remote)
    _check_timer(layout, report)
    _check_notify(layout, report)
    if tag:
        _check_tag(layout, report, tag)
    _check_list_local(layout, report)
    _check_auth_local(layout, report)
    _check_auth_stores(layout, report)
    _check_auth_degraded(layout, report)
    if tunnel_present:
        _check_follow(layout, report, tunnel_state == "active")
        _check_list_remote(layout, report)
    _check_keysync(layout, report)
    _check_policy(layout, report)
    _check_gh_repo(layout, report)
    return report.exit_code()
