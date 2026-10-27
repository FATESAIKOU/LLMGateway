"""usage：各訂閱的額度與冷卻（#101；design §4）。

資料只來自既有的兩個呼叫（不新增端點）：

- ``list``：每筆帶 ``usage``（七鍵）與 ``cooldown``／``model_cooldowns``；
- ``roles(role, agent, data)``：候選依 ``rank``，另帶 ``cooldown``／``model_cooldowns``。

兩邊依 ``id`` 合併排版。``usage.status`` 是 ``not_applicable``（沒有額度 API）時標
「無額度 API」，**不顯示 100%**；``unknown`` 照實標出固定碼 ``reason``；冷卻旁列
（整把與各模型分開）。沒給 ``--role`` 時只列 ``list`` 的用量表；給了就照 ``roles``
的 ``rank`` 順序，只列該角色的候選。

輸出不含 key：這裡只讀 list／roles 的公開欄位，也刻意不碰 get／resolve。
連不上、list 非 200、roles 404 都回可讀訊息與非 0 結束碼，不丟 traceback。
"""

from __future__ import annotations

import sys

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
        return "無額度 API"
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


def run(layout, *, role=None, agent=None, data=None, out=None, err=None,
        client=None) -> int:
    """印出額度表；回結束碼（0 成功、1 服務錯誤、2 參數不合法）。"""
    out = print if out is None else out
    err = (lambda text: print(text, file=sys.stderr)) if err is None else err

    if role is not None and not agent:
        err("llmgwctl: --role 要搭配 --agent（roles 端點需要 agent）")
        return 2
    if data is not None and role is None:
        err("llmgwctl: --data 只在有 --role 時有意義")
        return 2

    if client is None:
        client = Client(f"http://127.0.0.1:{layout.api_port()}")

    listed_result = client.list()
    if not listed_result.ok:
        if listed_result.unreachable:
            return _error(err, "連不上本機 Gateway（unreachable）；服務沒跑或埠不對")
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
        if roles_result.unreachable:
            return _error(err, "連不上本機 Gateway（unreachable）；服務沒跑或埠不對")
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
