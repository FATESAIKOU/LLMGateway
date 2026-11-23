"""usage：各訂閱的額度與冷卻（#101；design §4）。

資料只來自既有的兩個呼叫（不新增端點）：

- ``list``：每筆帶 ``usage``（九鍵）與 ``cooldown``／``model_cooldowns``；
- ``roles(role, agent, data)``：候選依 ``rank``，另帶 ``cooldown``／``model_cooldowns``。

兩邊依 ``id`` 合併排版。``usage.status`` 是 ``not_applicable`` 時依 ``reason`` 分流：``no_quota_api`` 標
「無額度 API」，``unavailable`` 標「無法取得」並附 ``note``，**都不顯示 100%**；
``unknown`` 照實標出固定碼 ``reason``；冷卻旁列
（整把與各模型分開）。沒給 ``--role`` 時只列 ``list`` 的用量表；給了就照 ``roles``
的 ``rank`` 順序，只列該角色的候選。

輸出不含 key：這裡只讀 list／roles 的公開欄位，也刻意不碰 get／resolve。
連不上、list 非 200、roles 404 都回可讀訊息與非 0 結束碼，不丟 traceback。

已簽章呼叫（#222 D4b；C-710～C-712、C-714）：只有 ``warn``／``enforce`` 模式才
建簽章（``off`` 與 0.1.0 完全相同），私鑰照 :mod:`lgwctl.local_auth` 的檢查（root:root
0600、一般檔、不跟隨 symlink）。root 讀不到私鑰時在 stderr 說明固定原因後**送出
一次無簽請求**（design：沒有 signer 就是沒有 signer），**不會**先簽、失敗再改送無簽，
也不重試。非 root 不印原因（design :262：不印原因細節或金鑰路徑內容）；enforce 下
收到 401 時結束碼 1 並印「enforce 下需 sudo 執行」，不印額度。
"""

from __future__ import annotations

import sys

from . import local_auth
from lgw.client import Client

WINDOWS = ("session", "weekly")


def _percent(value) -> str:
    """原始數值照實印（76.5 不變成 77）；不是數字就原樣轉字串。"""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _usage_text(usage) -> str:
    """一個 usage 物件的呈現；不推測缺值，not_applicable 絕不變百分比。"""
    if not isinstance(usage, dict):
        return "未知"
    status = usage.get("status")
    if status == "not_applicable":
        # reason 分流（spec role-allocation）：絕不顯示百分比。
        reason = usage.get("reason")
        if reason == "no_quota_api":
            return "無額度 API"
        if reason == "unavailable":
            note = usage.get("note")
            return f"無法取得；{note}" if note else "無法取得"
        return f"未知（{reason}）" if reason else "未知"
    if status == "ok":
        parts = []
        for name in WINDOWS:
            window = usage.get(name)
            if not isinstance(window, dict):
                continue
            if window.get("remaining_percent") is None:
                continue
            text = f"{name} {_percent(window['remaining_percent'])}%"
            reset = window.get("resets_at")
            if reset:
                text += f"（reset {reset}）"
            parts.append(text)
        if not parts:
            # status=ok 卻沒有窗口數值：說出是哪一種缺，不與 unknown 的「未知」混在一起。
            reason = usage.get("reason")
            detail = f"沒有窗口數值；{reason}" if reason else "沒有窗口數值"
            return f"未知（{detail}）"
        text = "；".join(parts)
        if usage.get("stale"):
            text += "（舊資料）"
        return text
    reason = usage.get("reason")
    return f"未知（{reason}）" if reason else "未知"


def _cooldown_text(row: dict) -> str:
    """整把冷卻與各模型冷卻分開列；都沒有回 ``-``。"""
    parts = []
    whole = row.get("cooldown")
    if isinstance(whole, dict) and whole.get("until"):
        parts.append(f"整把 {whole['until']}")
    models = row.get("model_cooldowns")
    if isinstance(models, dict):
        for model in sorted(models):
            item = models[model]
            until = item.get("until") if isinstance(item, dict) else None
            if until:
                parts.append(f"模型 {model} {until}")
    return "；".join(parts) if parts else "-"


def _by_id(subscriptions) -> dict:
    return {
        item["id"]: item
        for item in subscriptions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _render_row(row: dict, listed: dict, rank=None) -> str:
    subscription_id = str(row.get("id", ""))
    from_list = listed.get(subscription_id, {})
    provider = row.get("provider") or from_list.get("provider") or "-"
    usage = from_list.get("usage") if from_list else None
    if usage is None:
        usage_text = "未知（list 沒有這一筆）"
    else:
        usage_text = _usage_text(usage)
    cooldown = from_list if from_list else row
    columns = []
    if rank is not None:
        columns.append(str(rank))
    columns.extend([subscription_id, str(provider), usage_text, _cooldown_text(cooldown)])
    return " | ".join(columns)


def _print_table(rows, listed: dict, out, with_rank: bool) -> None:
    if with_rank:
        out("rank | id | provider | 額度 | 冷卻")
    else:
        out("id | provider | 額度 | 冷卻")
    for row in rows:
        rank = None
        if with_rank:
            # rank 缺值時仍留一欄（印 ``-``），否則後面的欄位會往前錯位。
            rank = row.get("rank")
            if rank is None:
                rank = "-"
        out(_render_row(row, listed, rank))


def _list_rows(listed: dict) -> list:
    return [
        {"id": subscription_id, "provider": item.get("provider")}
        for subscription_id, item in listed.items()
    ]


def _error(err, text: str) -> int:
    err(f"llmgwctl: {text}")
    return 1


def _signer(layout, err):
    """依模式建管理 principal 的簽章（C-710～C-712、C-714）。

    ``off``／模式讀不到或讀不懂時**不建**（與 0.1.0 完全相同，也不碰私鑰）。warn／enforce
    下私鑰不合格就是「沒有 signer」：root 在 stderr 說明固定原因後照舊送一次無簽
    請求；非 root 不印原因（design :262）。之後**不做**「簽章失敗再改送無簽」。
    """
    if layout.auth_mode() not in ("warn", "enforce"):
        return None
    signer, reason = local_auth.build_signer(layout)
    if signer is None and layout.euid == 0:
        err("llmgwctl: {0}；送出無簽請求".format(
            local_auth.detail(reason, layout.local_admin_key_path)))
    return signer


def _call_error(layout, err, result, what: str, *, signed: bool) -> int | None:
    """共同失敗分類；回結束碼，或 None 代表不是這裡處理的錯（呼叫方照舊判）。

    `signed` 是「這次請求有沒有管理 principal 的簽章」：root 沒簽章又收到 401 時，
    真正的原因是私鑰不可用（原因已印在上一行），不能說成「principal 未登記／已撤銷／
    時鐘偏差」（#271 L-5）。
    """
    if result.unreachable:
        return _error(err, "連不上本機 Gateway（unreachable）；服務沒跑或埠不對")
    if result.error == "signer_unavailable":
        reason = result.data.get("reason") if isinstance(result.data, dict) else None
        return _error(
            err, f"管理私鑰讀到了但簽章失敗（{reason or 'signer_error'}）；未送出請求")
    if result.status == 401:
        if layout.euid != 0:
            # C-712：不印原因細節或金鑰路徑內容。
            return _error(err, "enforce 下需 sudo 執行（管理 principal 只用 root 的私鑰）")
        if not signed:
            return _error(
                err, f"{what} 回 401：未簽章（管理私鑰不可用，原因見上一行）")
        return _error(
            err,
            f"{what} 失敗（status=401, error=unauthorized）；管理 principal 可能未登記、"
            "已撤銷或時鐘偏差",
        )
    return None


def run(layout, *, role=None, agent=None, data=None, out=None, err=None,
        client=None) -> int:
    """印出額度表；回結束碼（0 成功、1 服務錯誤、2 參數不合法）。"""
    out = print if out is None else out
    err = (lambda text: print(text, file=sys.stderr)) if err is None else err

    if role is not None and not agent:
        err("llmgwctl: --role 要搭配 --agent（roles 端點需要 agent）")
        return 2
    if role is None and agent is not None:
        err("llmgwctl: --agent 只在有 --role 時有意義")
        return 2
    if data is not None and role is None:
        err("llmgwctl: --data 只在有 --role 時有意義")
        return 2

    signer = None
    if client is None:
        url = f"http://127.0.0.1:{layout.api_port()}"
        signer = _signer(layout, err)
        # off／沒有 signer 時與 0.1.0 的呼叫形狀完全相同（不多傳關鍵字參數）。
        client = Client(url) if signer is None else Client(url, signer=signer)

    listed_result = client.list()
    if not listed_result.ok:
        code = _call_error(layout, err, listed_result, "list", signed=signer is not None)
        if code is not None:
            return code
        return _error(
            err,
            f"list 失敗（status={listed_result.status}, error={listed_result.error}）",
        )
    subscriptions = listed_result.data.get("subscriptions")
    if not isinstance(subscriptions, list):
        return _error(err, "list 的回覆沒有 subscriptions 陣列")
    listed = _by_id(subscriptions)

    if role is None:
        _print_table(_list_rows(listed), listed, out, with_rank=False)
        return 0

    roles_result = client.roles(role, agent, data=data)
    if not roles_result.ok:
        code = _call_error(layout, err, roles_result, "roles", signed=signer is not None)
        if code is not None:
            return code
        if roles_result.status == 404:
            return _error(
                err,
                f"roles 回 404 not_found：角色 {role} 沒有分配設定，"
                "或這版 Gateway 沒有 roles 端點",
            )
        return _error(
            err,
            f"roles 失敗（status={roles_result.status}, error={roles_result.error}）",
        )
    candidates = roles_result.data.get("candidates")
    if not isinstance(candidates, list):
        return _error(err, "roles 的回覆沒有 candidates 陣列")
    effective = roles_result.data.get("data") or data or "real"
    out(f"role={role} agent={agent} data={effective}")
    _print_table(candidates, listed, out, with_rank=True)
    if not candidates:
        out("（沒有候選）")
    return 0
