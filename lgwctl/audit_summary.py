"""``llmgwctl audit-summary``：唯讀統計 ``audit``（#347 N13a §4.2、§4.3、C-1042）。

- ``--since``／``--until`` 嚴格格式（``YYYY-MM-DDTHH:MM:SSZ`` 或 ``+00:00``、日曆有效），
  先正規化為 ``Z`` 再比較；區間為半開 ``[since, until)``。格式錯或 ``since >= until`` → 結束 2。
- 以 ``mode=ro`` 開庫；舊 DB 缺欄（``PRAGMA table_info``）時該指標輸出
  ``{"available": false, "reason": "missing_columns", ...}``，不輸出假 0。
- ``c2`` 固定 ``null``（``--sidecar-events`` 屬 PR4，不在這裡）。
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
import urllib.parse

from lgwd.clock import UTC, to_rfc3339

#: 嚴格時間格式（C-1042）。這個正規式本身就是閘門：用 [0-9] 不用 \d（\d 會吃全形數字）、
#: fullmatch 不讓 $ 吞掉結尾換行；通過後才用捕獲的欄位組 datetime（不靠 fromisoformat 的寬鬆）。
_TIME_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:Z|\+00:00)")

#: 只數這些 tier 為「免費／低階」（契約 §4.2 的 free_low）。
_FREE_LOW = ("free", "low")


class SummaryError(Exception):
    """參數不合（時間格式、區間）→ 結束 2。"""


def normalize_time(text) -> str:
    """嚴格檢查後正規化為 ``...Z``；不合就丟 SummaryError。"""
    match = _TIME_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise SummaryError("時間格式必須是 YYYY-MM-DDTHH:MM:SSZ 或 +00:00（整秒、無小數、無空白）")
    try:
        moment = _dt.datetime(*(int(part) for part in match.groups()), tzinfo=UTC)
    except ValueError:  # 例如 2026-02-30
        raise SummaryError("時間不是有效的日曆時刻：{0}".format(text)) from None
    return to_rfc3339(moment)


def _missing(columns, needed) -> list:
    return [name for name in needed if name not in columns]


def _unavailable(missing: list) -> dict:
    return {"available": False, "reason": "missing_columns", "missing_columns": missing}


def _ratio(num: int, den: int):
    return None if den == 0 else num / den


def _free_low_count(rows) -> int:
    return sum(1 for row in rows if row.get("tier") in _FREE_LOW)


def _life(ref: str) -> str:
    """ref 中第一個 ``.`` 之前的字串；沒有 ``.`` 時整串就是一個 life。"""
    return ref.split(".", 1)[0]


def _open_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def summarize(conn: sqlite3.Connection, since: str, until: str) -> dict:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
    if not columns:
        raise SummaryError("audit 表不存在")
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM audit WHERE ts >= ? AND ts < ? ORDER BY seq", (since, until))]
    resolves = [row for row in rows if row.get("action") == "resolve"]
    cooldowns = [row for row in rows if row.get("action") == "cooldown"]

    out = {"schema": 1, "since": since, "until": until,
           "total_resolves": len(resolves)}

    # --- role 相關（role 欄缺＝無法分辨新舊路徑，整組 unavailable）---
    has_role = "role" in columns
    if has_role:
        role_rows = [row for row in resolves if row["role"] is not None]
        legacy_rows = [row for row in resolves if row["role"] is None]
        out["role_resolves"] = len(role_rows)
        out["legacy_resolves"] = len(legacy_rows)
    else:
        role_rows = []
        out["role_resolves"] = _unavailable(["role"])
        out["legacy_resolves"] = _unavailable(["role"])

    # --- explicit_model ---
    if "explicit_model" in columns:
        out["explicit_model_resolves"] = sum(
            1 for row in resolves if row["explicit_model"] == "1")
    else:
        out["explicit_model_resolves"] = _unavailable(["explicit_model"])

    # --- ref／life ---
    if "ref" in columns:
        with_ref = [row["ref"] for row in resolves if row["ref"]]
        out["distinct_ref"] = len(set(with_ref))
        out["distinct_life"] = len({_life(ref) for ref in with_ref})
        per_life = collections.Counter(_life(ref) for ref in with_ref)
        out["resolves_per_life"] = {life: n for life, n in sorted(per_life.items()) if n > 1}
    else:
        out["distinct_ref"] = _unavailable(["ref"])
        out["distinct_life"] = _unavailable(["ref"])
        out["resolves_per_life"] = _unavailable(["ref"])

    # --- tier／data（只數 role_resolves；legacy 無 tier，不入比率）---
    tier_missing = _missing(columns, ("role", "tier", "data"))
    if not tier_missing:
        by_tier = collections.Counter(row["tier"] for row in role_rows)
        by_data = collections.Counter(row["data"] for row in role_rows)
        by_data_tier = collections.defaultdict(collections.Counter)
        for row in role_rows:
            by_data_tier[row["data"]][row["tier"]] += 1
        out["by_tier"] = dict(sorted(by_tier.items(), key=lambda kv: str(kv[0])))
        out["by_data"] = dict(sorted(by_data.items(), key=lambda kv: str(kv[0])))
        out["by_data_tier"] = {
            data: dict(sorted(tiers.items(), key=lambda kv: str(kv[0])))
            for data, tiers in sorted(by_data_tier.items(), key=lambda kv: str(kv[0]))
        }
        # free_low_ratio 與 free_low_ratio_all：契約 §4.2（rev 3g 中 3）只定義分子分母皆為
        # role_resolves，兩者因此同值；是否要區分尚待架構師確認（報告列為待決）。
        free_low = _free_low_count(role_rows)
        out["free_low_ratio"] = _ratio(free_low, len(role_rows))
        out["free_low_ratio_all"] = _ratio(free_low, len(role_rows))
        public = by_data_tier.get("public", collections.Counter())
        out["public_free_low_ratio"] = _ratio(
            sum(public[t] for t in _FREE_LOW), sum(public.values()))
    else:
        for key in ("by_tier", "by_data", "by_data_tier", "free_low_ratio",
                    "free_low_ratio_all", "public_free_low_ratio"):
            out[key] = _unavailable(tier_missing)

    # --- table_versions ---
    tv_missing = _missing(columns, ("role", "table_sha"))
    if not tv_missing:
        groups = collections.OrderedDict()
        for row in role_rows:
            sha = row.get("table_sha")
            if not sha:
                continue
            entry = groups.setdefault(sha, {"table_sha": sha, "first_ts": row["ts"],
                                            "last_ts": row["ts"], "count": 0})
            entry["first_ts"] = min(entry["first_ts"], row["ts"])
            entry["last_ts"] = max(entry["last_ts"], row["ts"])
            entry["count"] += 1
        out["table_versions"] = sorted(groups.values(), key=lambda e: e["first_ts"])
    else:
        out["table_versions"] = _unavailable(tv_missing)

    # --- breakdown ---
    bd_missing = _missing(columns, ("role", "subscription", "model", "tier"))
    if not bd_missing:
        counts = collections.Counter(
            (row["role"], row["subscription"], row["model"], row["tier"]) for row in role_rows)
        out["breakdown"] = [
            {"role": r, "subscription": s, "model": m, "tier": t, "count": n}
            for (r, s, m, t), n in sorted(counts.items(), key=lambda kv: tuple(str(x) for x in kv[0]))
        ]
    else:
        out["breakdown"] = _unavailable(bd_missing)

    # --- cooldown_transitions（只認新列：cooldown_mode 非 NULL）---
    cd_needed = ("cooldown_mode", "prev_until")
    cd_missing = _missing(columns, cd_needed)
    if cd_missing:
        out["cooldown_transitions"] = _unavailable(cd_missing)
    else:
        new_rows = [row for row in cooldowns if row["cooldown_mode"] is not None]
        legacy_count = sum(1 for row in cooldowns if row["cooldown_mode"] is None)
        if not new_rows:
            out["cooldown_transitions"] = {
                "available": False, "reason": "no_new_rows", "coverage": "none",
                "legacy_cooldown_rows": legacy_count}
        else:
            transitions = [row for row in new_rows if row["prev_until"] is None]
            groups = collections.OrderedDict()
            for row in transitions:
                key = (row["subscription"], row["model"])
                entry = groups.setdefault(key, {"subscription": row["subscription"],
                                                "model": row["model"], "count": 0,
                                                "events": []})
                entry["count"] += 1
                entry["events"].append({"ts": row["ts"], "until": row["until"]})
            out["cooldown_transitions"] = {
                "available": True,
                "coverage": "complete_new" if legacy_count == 0 else "partial_new_and_legacy",
                "total": len(transitions),
                "legacy_cooldown_rows": legacy_count,
                "by_subscription": [
                    groups[key] for key in sorted(groups, key=lambda k: tuple(str(x) for x in k))
                ],
            }

    # --- usage_exhausted_observed（只看成功且未截斷的 role resolve 的 skipped）---
    ue_missing = _missing(columns, ("role", "skipped", "skipped_truncated"))
    if ue_missing:
        out["usage_exhausted_observed"] = _unavailable(ue_missing)
    elif not role_rows:
        out["usage_exhausted_observed"] = {"available": False, "reason": "no_new_rows"}
    else:
        seen = {}
        truncated = 0
        unparsed = 0
        for row in role_rows:
            if row["skipped_truncated"] == "1":
                truncated += 1
                continue
            try:
                items = json.loads(row["skipped"]) if row["skipped"] else []
            except (TypeError, ValueError):
                unparsed += 1
                continue
            if not isinstance(items, list):
                unparsed += 1
                continue
            keys = set()
            for item in items:
                if isinstance(item, dict) and item.get("reason") == "usage_exhausted":
                    keys.add((item.get("subscription"), item.get("until")))
            for key in keys:
                entry = seen.get(key)
                if entry is None:
                    seen[key] = {"subscription": key[0], "until": key[1],
                                 "first_seen_ts": row["ts"], "seen_count": 1}
                else:
                    entry["seen_count"] += 1
                    entry["first_seen_ts"] = min(entry["first_seen_ts"], row["ts"])
        out["usage_exhausted_observed"] = {
            "available": True,
            "items": sorted(seen.values(), key=lambda e: (str(e["subscription"]), str(e["until"]))),
            "completeness": {
                "role_resolves_scanned": len(role_rows),
                "skipped_truncated_resolves": truncated,
                "unparsed_resolves": unparsed,
                "observed_only": True,
            },
        }

    out["c2"] = None
    return out


def run(layout, *, since: str, until: str, db=None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        since_n = normalize_time(since)
        until_n = normalize_time(until)
        if since_n >= until_n:
            raise SummaryError("--since 必須早於 --until")
    except SummaryError as exc:
        print("llmgwctl audit-summary: {0}".format(exc), file=err)
        return 2
    db_path = layout.db_file if db is None else db
    if not os.path.isfile(db_path):
        print("llmgwctl audit-summary: 找不到資料庫檔", file=err)
        return 1
    try:
        conn = _open_ro(db_path)
    except sqlite3.Error as exc:
        print("llmgwctl audit-summary: 開不了資料庫（{0}）".format(type(exc).__name__), file=err)
        return 1
    try:
        result = summarize(conn, since_n, until_n)
    except SummaryError as exc:
        print("llmgwctl audit-summary: {0}".format(exc), file=err)
        return 1
    except sqlite3.Error as exc:
        print("llmgwctl audit-summary: 查詢失敗（{0}）".format(type(exc).__name__), file=err)
        return 1
    finally:
        conn.close()
    print(json.dumps(_null_keys(result), ensure_ascii=False, indent=2, sort_keys=True), file=out)
    return 0


def _null_keys(value):
    """L-11：tier／data 為 NULL 時 Counter 的鍵是 None，和字串鍵一起 sort_keys 會 TypeError。

    遞迴把 None 鍵轉成字串 "null"（JSON 本來就會寫成 null；這裡只是讓排序穩定）。
    """
    if isinstance(value, dict):
        return {("null" if k is None else k): _null_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_null_keys(v) for v in value]
    return value
