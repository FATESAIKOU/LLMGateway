"""``llmgwctl cooldown-clear``：本機縮短／清除冷卻（#347 N13a §3）。

HTTP ``cooldown`` 只延長；縮短與清除走這裡，直接以讀寫方式開本機 ``llm.db``（需 root 或
``llmgw``）。冷卻列的改動與 ``audit`` 列在**同一個** ``BEGIN IMMEDIATE`` 交易裡，任一失敗
整筆 rollback，不留「只改冷卻、沒記 audit」的狀態。

- 省略 ``--until``：clear，刪列；列不存在就什麼都不做（冪等，結束 0，不寫 audit）。
- 帶 ``--until``：replace，覆寫成該時刻；須 ``now ≤ until ≤ now + until_max_ahead_seconds``。
- ``--model``：先驗（規則與 ``lgwd/registry.py`` 的 ``_cooldown_model`` 相同；那個是私有函式，
  這裡複製一份，``tests/test_n13a_cooldown_clear.py`` 以 registry 的行為對照）。
- 兩層（整把／單模型）各自獨立，不互相刪除。

只用標準函式庫（sqlite3）與 ``lgwd`` 的 policy／clock；不改 ``lgwd/store.py``。
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import pwd
import re
import sqlite3
import sys
import urllib.parse

from lgwd.clock import UTC, from_rfc3339, to_rfc3339
from lgwd.policy import AGENTS, PolicyError, load_policy

#: 與 lgwd/registry.py 的 _MODEL_MAX_CHARS 相同（複製，不 import 私有常數）。
MODEL_MAX_CHARS = 200

#: 寫 audit 需要的新欄（#347 §4.1，由 lgwd/store.py 的 additive 遷移補上）。
AUDIT_REQUIRED_COLUMNS = ("principal", "cooldown_mode", "prev_until", "os_user")


class CooldownClearError(Exception):
    """可預期的拒絕（參數、政策、DB 狀態）；``code`` 是結束碼。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _has_wildcard(pattern: str) -> bool:
    """與 lgwd/registry.py `_has_wildcard` 相同：*、?、[ 任何位置都算。"""
    return any(ch in pattern for ch in ("*", "?", "["))


def _covered(patterns, model: str) -> bool:
    """與 lgwd/registry.py `_covered` 相同：fnmatch（區分大小寫）。"""
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns)


def validate_model(sub, model) -> str:
    """與 lgwd/registry.py `_cooldown_model` 同規則；不合就丟 code 2。

    非空字串、≤200 字元、不帶 ``<agent>/`` 前綴、不含萬用字元、被該把的 models.allow 涵蓋。
    不正規化（不去前綴、不改大小寫）。
    """
    if not isinstance(model, str) or not model:
        raise CooldownClearError(2, "--model 必須是非空字串")
    if len(model) > MODEL_MAX_CHARS:
        raise CooldownClearError(2, "--model 超過 200 字元")
    if any(model.startswith(agent + "/") for agent in AGENTS):
        raise CooldownClearError(2, "--model 不可帶 <agent>/ 前綴（要供應商自己的模型 id）")
    if _has_wildcard(model):
        raise CooldownClearError(2, "--model 不可含萬用字元")
    if not _covered(sub.models_allow, model):
        raise CooldownClearError(2, "--model 不在該訂閱的 models.allow 裡")
    return model


#: 嚴格 RFC 3339：``T`` 分隔、必帶時區（``Z`` 或 ``±HH:MM``）。不靠 fromisoformat（它接受空白分隔）。
_UNTIL_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})")


def parse_until(text: str, now: _dt.datetime, max_ahead_seconds: int) -> _dt.datetime:
    """嚴格 RFC 3339（須帶時區）；不合格式或日曆無效丟 code 2；不在 [now, now+max_ahead] 丟 code 2。"""
    if not isinstance(text, str) or not _UNTIL_RE.fullmatch(text):
        raise CooldownClearError(2, "--until 必須是 RFC 3339 時刻（T 分隔，帶 Z 或 ±HH:MM）")
    try:
        moment = from_rfc3339(text)
    except (TypeError, ValueError):
        raise CooldownClearError(2, "--until 不是有效的日曆時刻") from None
    if moment < now:
        raise CooldownClearError(2, "--until 早於現在")
    if moment > now + _dt.timedelta(seconds=max_ahead_seconds):
        raise CooldownClearError(2, "--until 超過允許的最遠時刻（until_max_ahead_seconds）")
    return moment


def _os_user(layout) -> str:
    """操作者：``SUDO_USER``，否則有效 uid 的帳號名（查不到就用 uid 數字）。"""
    sudo_user = layout.env.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _connect_rw(db_path: str) -> sqlite3.Connection:
    """以讀寫開庫；檔案不存在就失敗（mode=rw 不會替你建一個空庫）。isolation_level=None，交易自己下。"""
    if not os.path.isfile(db_path):
        raise CooldownClearError(1, "找不到 llm.db（lgwd 還沒有啟動過？）")
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise CooldownClearError(1, "開不了 llm.db：{0}".format(type(exc).__name__)) from None
    conn.row_factory = sqlite3.Row
    return conn


def _check_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
    if not columns:
        raise CooldownClearError(1, "llm.db 沒有 audit 表")
    missing = [name for name in AUDIT_REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise CooldownClearError(
            1, "audit 缺欄 {0}：先讓新版 lgwd 啟動一次完成遷移".format(", ".join(missing)))


def apply(db_path: str, *, subscription: str, model, until, now: _dt.datetime,
          os_user: str):
    """在一筆交易裡改冷卻列並寫 audit。回傳結果 dict；沒有變動時 ``changed`` 為 False。

    ``until`` 為 None 即 clear，否則為 replace（已驗過範圍的 datetime）。
    """
    conn = _connect_rw(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _check_schema(conn)
            if model is None:
                prev = conn.execute(
                    "SELECT until FROM cooldowns WHERE subscription = ?",
                    (subscription,)).fetchone()
            else:
                prev = conn.execute(
                    "SELECT until FROM model_cooldowns WHERE subscription = ? AND model = ?",
                    (subscription, model)).fetchone()
            # prev_until：交易前仍有效（> now）的冷卻才記；過期列不算「已有冷卻」。
            prev_until = None
            if prev is not None and from_rfc3339(prev["until"]) > now:
                prev_until = prev["until"]

            if until is None:
                if prev is None:
                    conn.execute("ROLLBACK")
                    return {"changed": False, "subscription": subscription, "model": model}
                if model is None:
                    conn.execute("DELETE FROM cooldowns WHERE subscription = ?",
                                 (subscription,))
                else:
                    conn.execute(
                        "DELETE FROM model_cooldowns WHERE subscription = ? AND model = ?",
                        (subscription, model))
                action, mode, new_until = "cooldown_clear", "clear", None
            else:
                stamp = to_rfc3339(until)
                if model is None:
                    conn.execute(
                        "INSERT INTO cooldowns (subscription, until) VALUES (?, ?)"
                        " ON CONFLICT(subscription) DO UPDATE SET until = excluded.until",
                        (subscription, stamp))
                else:
                    conn.execute(
                        "INSERT INTO model_cooldowns (subscription, model, until)"
                        " VALUES (?, ?, ?)"
                        " ON CONFLICT(subscription, model) DO UPDATE SET until = excluded.until",
                        (subscription, model, stamp))
                action, mode, new_until = "cooldown_replace", "replace", stamp

            conn.execute(
                "INSERT INTO audit (ts, subscription, action, until, model, principal,"
                " cooldown_mode, prev_until, os_user)"
                " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (to_rfc3339(now), subscription, action, new_until, model,
                 mode, prev_until, os_user))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise CooldownClearError(1, "寫入失敗（已回滾）：{0}".format(type(exc).__name__)) from None
    finally:
        conn.close()
    return {"changed": True, "action": action, "subscription": subscription,
            "model": model, "until": new_until, "prev_until": prev_until}


def run(layout, *, subscription: str, model=None, until=None, now=None,
        out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    now = _dt.datetime.now(tz=UTC) if now is None else now
    try:
        policy = None
        if until is not None or model is not None:
            policy_path = os.path.join(layout.current_link, "policy", "default.json")
            try:
                policy = load_policy(policy_path)
            except (PolicyError, OSError, ValueError):
                raise CooldownClearError(1, "讀不到當版的政策檔（policy/default.json）") from None
        until_moment = None
        if until is not None:
            if subscription not in policy.subscriptions:
                raise CooldownClearError(2, "未知的訂閱 id：{0}".format(subscription))
            until_moment = parse_until(until, now, policy.until_max_ahead_seconds)
        if model is not None:
            if subscription not in policy.subscriptions:
                raise CooldownClearError(2, "未知的訂閱 id：{0}".format(subscription))
            model = validate_model(policy.subscriptions[subscription], model)
        result = apply(layout.db_file, subscription=subscription, model=model,
                       until=until_moment, now=now, os_user=_os_user(layout))
    except CooldownClearError as exc:
        print("llmgwctl cooldown-clear: {0}".format(exc), file=err)
        return exc.code
    if not result["changed"]:
        print("無冷卻列，未變更（冪等）", file=out)
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=out)
    return 0
