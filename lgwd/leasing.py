"""租用的核心（design 決定 6、8、12；spec agent-lease、key-vault、status-board）。由 impl 實作。

LeaseService 的每個公開方法：
- 參數 client 是已認證的客戶端名稱（HTTP 層用 Bearer 查出來；查不到 HTTP 層自己回 401）。
- 參數 body 是已解析的 JSON dict。
- 成功回傳 (http_status, response_dict)；失敗丟 lgwd.errors.ApiError。
- 會改變狀態的動作：一筆 SQLite `BEGIN IMMEDIATE` 交易；開頭先做延後結算（過期與未確認的租約補記 expired）；
  以（client, request_id）去重，重送回第一次的回覆（保留 7 天，跨重啟）。
- 所有時刻經 self.clock；對外是 RFC 3339 UTC 字串（lgwd.clock.to_rfc3339）。
- 事件只追加、單調序號；事件、log、例外訊息 MUST NOT 含 key 的值。env 只出現在核准的那一次回覆裡。

租約物件：{lease_id, subscription, agent, model, env, unset, expires_at, confirm_by}
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid

from .clock import Clock, from_rfc3339, to_rfc3339
from .envmap import build_env
from .errors import (
    ApiError,
    bad_request,
    client_mismatch,
    lease_not_active,
    lease_not_confirmed,
    not_found,
    not_owner,
    refusal,
    unauthorized,
)
from .keys import KeyStore
from .policy import Policy
from .store import Store

API_VERSION = 1

# design 決定 6 的 signal 列舉。
SIGNALS = (
    "claude_limit",
    "zen_free_limit",
    "zen_go_limit",
    "credit_limit",
    "openrouter_limit",
    "ollama_limit",
    "other",
)

_DEDUP_KEEP = _dt.timedelta(days=7)

# 實作說明（契約內的判斷，簡短）：
# - granted 回覆的 expires_at 為 None（效期從 start 起算，之前沒有效期）。
# - usage 不限租約狀態（docstring 只要求 snapshot 是 dict）；404／403 照查。
# - renew 從現在起算 lease_ttl_seconds（sliding）。
# - 檢查順序：400 →（401：未知或已撤銷客戶端）→ 403 → 404 → 409；
#   租約 id 不存在時先 404（沒有對象可談 owner）。
# - 401 不進 dedup（客戶端集合可能變化，重送應重判）。
# - 403 client_mismatch 與 409 拒絕記 denied 事件；404／409 生命週期錯誤不記。
# - exhausted 的下一個申請不帶 preferences（docstring 只列六個欄位）。
# - resets_at 缺失、非字串、無法解析、在過去、超過上限，一律改用預設冷卻。
# - 冷卻取 max（已有的、新的），寧可等久一點。


class LeaseService:
    def __init__(self, db_path: str, policy: Policy, keys: KeyStore, clock: Clock):
        self._store = Store(db_path)
        self._policy = policy
        self._keys = keys
        self._clock = clock

    def close(self) -> None:
        self._store.close()

    # --- 客戶端身分（design 決定 4） ---
    def add_client(self, name: str) -> str:
        """新增客戶端，回傳一次性的明文憑證（32 bytes 隨機、urlsafe base64）；只存 SHA-256。名稱重複丟 ValueError。"""
        if not isinstance(name, str) or not name or not name.isprintable():
            raise ValueError("bad client name")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = to_rfc3339(self._clock.now())
        with self._store.txn() as cur:
            row = self._store.client_get(cur, name)
            if row is not None and row["revoked_at"] is None:
                raise ValueError("client exists")
            if row is None:
                self._store.client_add(cur, name, digest, now)
            else:
                cur.execute(
                    "UPDATE clients SET cred_hash = ?, created_at = ?, revoked_at = NULL"
                    " WHERE name = ?",
                    (digest, now, name),
                )
            self._store.log(
                cur, "client_added", now, client=name,
                payload={"name": name, "cred_hash": digest, "created_at": now},
            )
        return token

    def revoke_client(self, name: str) -> None:
        """撤銷；該客戶端所有未結束的租約記為 revoked。"""
        now = to_rfc3339(self._clock.now())
        with self._store.txn() as cur:
            row = self._store.client_get(cur, name)
            if row is None:
                raise ValueError("unknown client")
            if row["revoked_at"] is None:
                self._store.client_revoke(cur, name, now)
                self._store.log(cur, "client_revoked", now, client=name,
                                payload={"name": name, "revoked_at": now})
            for lease in self._store.leases_unfinished(cur):
                if lease["client"] == name:
                    self._store.lease_update(cur, lease["lease_id"], status="revoked")
                    self._store.log(cur, "lease_revoked", now, client=name,
                                    lease_id=lease["lease_id"],
                                    subscription=lease["subscription"],
                                    payload={"lease_id": lease["lease_id"]})

    def list_clients(self) -> list[dict]:
        """[{name, created_at, revoked_at}]；不含雜湊。"""
        with self._store.txn() as cur:
            return [
                {"name": r["name"], "created_at": r["created_at"], "revoked_at": r["revoked_at"]}
                for r in self._store.client_all(cur)
            ]

    def authenticate(self, token: str) -> str | None:
        """回傳客戶端名稱；無效或已撤銷回 None。用 hmac.compare_digest 比對雜湊。"""
        if not isinstance(token, str) or not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._store.txn() as cur:
            for row in self._store.client_all(cur):
                if row["revoked_at"] is not None:
                    continue
                if hmac.compare_digest(row["cred_hash"], digest):
                    return row["name"]
        return None

    # --- 內部小工具 ---
    def _settle(self, cur: sqlite3.Cursor, now: _dt.datetime) -> None:
        """延後結算：過期與逾時未確認的租約補記 expired；清 7 天前的去重。"""
        stamp = to_rfc3339(now)
        for lease in self._store.leases_unfinished(cur):
            if lease["status"] == "granted":
                if now >= from_rfc3339(lease["confirm_by"]):
                    self._store.lease_update(cur, lease["lease_id"], status="expired")
                    self._store.log(cur, "expired", stamp, client=lease["client"],
                                    lease_id=lease["lease_id"],
                                    subscription=lease["subscription"],
                                    payload={"lease_id": lease["lease_id"], "reason": "unconfirmed"})
            else:  # active
                if lease["expires_at"] is not None and now >= from_rfc3339(lease["expires_at"]):
                    self._store.lease_update(cur, lease["lease_id"], status="expired")
                    self._store.log(cur, "expired", stamp, client=lease["client"],
                                    lease_id=lease["lease_id"],
                                    subscription=lease["subscription"],
                                    payload={"lease_id": lease["lease_id"], "reason": "ttl"})
        self._store.dedup_prune(cur, to_rfc3339(now - _DEDUP_KEEP))

    def _require_live_client(self, cur: sqlite3.Cursor, client: str) -> None:
        row = self._store.client_get(cur, client)
        if row is None or row["revoked_at"] is not None:
            raise unauthorized()

    def _dedup_hit(self, cur: sqlite3.Cursor, client: str, request_id: str) -> tuple[int, dict] | None:
        row = self._store.dedup_get(cur, client, request_id)
        if row is None:
            return None
        return int(row["status"]), json.loads(row["body"])

    def _dedup_put(
        self, cur: sqlite3.Cursor, client: str, request_id: str,
        status: int, body: dict, now: _dt.datetime,
    ) -> None:
        self._store.dedup_put(cur, client, request_id, status, body, to_rfc3339(now))

    def _need_str(
        self, missing: list[str], name: str, value, *, required: bool, max_len: int | None = None,
    ):
        if value is None or value == "":
            if required:
                missing.append(name)
            return None
        if not isinstance(value, str):
            raise bad_request(detail=f"{name} 須為字串")
        if not value.isprintable():
            raise bad_request(detail=f"{name} 含不可列印字元")
        if max_len is not None and len(value) > max_len:
            raise bad_request(detail=f"{name} 過長")
        return value

    def _check_preferences(self, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise bad_request(detail="preferences 須為物件")
        if "preferred_models" in value and value["preferred_models"] is not None:
            pm = value["preferred_models"]
            if not isinstance(pm, list) or any(not isinstance(m, str) for m in pm):
                raise bad_request(detail="preferred_models 須為字串陣列")
        if "floor_model" in value and value["floor_model"] is not None:
            if not isinstance(value["floor_model"], str):
                raise bad_request(detail="floor_model 須為字串或空")
        return value

    def _deny(
        self, cur: sqlite3.Cursor, client: str, ctx: dict,
        reason: str, earliest_reset: str | None, now: _dt.datetime,
    ) -> ApiError:
        err = refusal(reason, earliest_reset)
        self._store.log(cur, "denied", to_rfc3339(now), client=client,
                        subscription=ctx.get("subscription"),
                        payload={**ctx, "reason": reason, "earliest_reset": earliest_reset})
        if ctx.get("request_id"):
            self._dedup_put(cur, client, ctx["request_id"], err.status, err.body(), now)
        return err

    def _is_cooling(self, cur: sqlite3.Cursor, subscription: str, now: _dt.datetime) -> str | None:
        """冷卻中回結束時刻（RFC 3339），否則回 None。等於結束時刻即結束。"""
        row = self._store.cool_get(cur, subscription)
        if row is None:
            return None
        if now < from_rfc3339(row["until"]):
            return row["until"]
        return None

    def _select_and_grant(
        self, cur: sqlite3.Cursor, client: str, worker: str, role: str, agent: str,
        instance: str, model: str | None, specified: str | None,
        purpose: str | None, preferences: dict | None, now: _dt.datetime,
    ) -> tuple[str, str]:
        """選定並核發；回 (subscription, lease_id)。選不上丟拒絕的 ApiError。"""
        if specified is not None:
            if specified not in self._policy.subscriptions or not self._policy.allows(
                specified, agent, model
            ):
                ctx = self._req_ctx(client, worker, role, agent, instance, specified)
                raise self._deny(cur, client, ctx, "not_allowed", None, now)
            considered = [specified]
        else:
            considered = [
                s for s in self._policy.candidates(agent)
                if self._policy.allows(s, agent, model)
            ]
            if not considered:
                ctx = self._req_ctx(client, worker, role, agent, instance, None)
                raise self._deny(cur, client, ctx, "not_allowed", None, now)
        if self._store.has_unfinished_instance(cur, client, instance):
            ctx = self._req_ctx(client, worker, role, agent, instance, specified)
            raise self._deny(cur, client, ctx, "instance_busy", None, now)
        deployed = [s for s in considered if self._keys.deployed(s)]
        if not deployed:
            ctx = self._req_ctx(client, worker, role, agent, instance, specified)
            raise self._deny(cur, client, ctx, "not_allowed", None, now)
        cooling_until = {s: self._is_cooling(cur, s, now) for s in deployed}
        for sub in deployed:
            if cooling_until[sub] is not None:
                continue
            cap = self._policy.subscriptions[sub].cap
            if cap is not None and self._store.count_unfinished_for_sub(cur, sub) >= cap:
                continue
            return sub, self._grant(
                cur, client, worker, role, agent, instance, model, specified,
                purpose, preferences, sub, now,
                reason="specified" if specified is not None else f"order:{sub}",
            )
        still = [s for s in deployed if cooling_until[s] is None]
        if still:
            ctx = self._req_ctx(client, worker, role, agent, instance, specified)
            raise self._deny(cur, client, ctx, "cap_reached", None, now)
        earliest = min(cooling_until[s] for s in deployed if cooling_until[s] is not None)
        ctx = self._req_ctx(client, worker, role, agent, instance, specified)
        raise self._deny(cur, client, ctx, "all_cooling", earliest, now)

    @staticmethod
    def _req_ctx(client, worker, role, agent, instance, subscription):
        return {
            "client": client, "worker": worker, "role": role, "agent": agent,
            "instance": instance, "subscription": subscription,
        }

    def _grant(
        self, cur: sqlite3.Cursor, client: str, worker: str, role: str, agent: str,
        instance: str, model: str | None, specified: str | None,
        purpose: str | None, preferences: dict | None, sub: str, now: _dt.datetime,
        reason: str,
    ) -> str:
        sub_policy = self._policy.subscriptions[sub]
        resolved = model or sub_policy.default_model
        key = self._keys.get(sub)
        if key is None:  # 部署在選定後被抽掉；視同不可用。
            ctx = self._req_ctx(client, worker, role, agent, instance, specified)
            raise self._deny(cur, client, ctx, "not_allowed", None, now)
        try:
            env, unset = build_env(sub, agent, key, resolved)
        except ValueError:
            ctx = self._req_ctx(client, worker, role, agent, instance, specified)
            raise self._deny(cur, client, ctx, "not_allowed", None, now)
        lease_id = uuid.uuid4().hex
        stamp = to_rfc3339(now)
        confirm_by = to_rfc3339(
            now + _dt.timedelta(seconds=self._policy.confirm_within_seconds)
        )
        self._store.log(cur, "requested", stamp, client=client, subscription=specified, payload={
            "client": client, "worker": worker, "role": role, "agent": agent,
            "instance": instance, "model": resolved, "subscription": specified,
            "purpose": purpose, "preferences": preferences,
        })
        self._store.log(cur, "granted", stamp, client=client, lease_id=lease_id,
                        subscription=sub, payload={
                            "lease_id": lease_id, "client": client, "worker": worker,
                            "role": role, "agent": agent, "instance": instance,
                            "subscription": sub, "model": resolved, "reason": reason,
                            "preferences": preferences, "granted_at": stamp,
                            "confirm_by": confirm_by,
                        })
        self._store.lease_add(
            cur, lease_id=lease_id, client=client, worker=worker, role=role, agent=agent,
            instance=instance, subscription=sub, model=resolved, purpose=purpose,
            preferences=json.dumps(preferences) if preferences is not None else None,
            status="granted", granted_at=stamp, confirm_by=confirm_by,
            started_at=None, expires_at=None,
        )
        return lease_id

    def _lease_obj(
        self, cur: sqlite3.Cursor, lease_id: str, sub: str, agent: str,
        model: str | None, confirm_by: str,
    ) -> dict:
        key = self._keys.get(sub)
        env, unset = build_env(sub, agent, key, model)
        return {
            "lease_id": lease_id, "subscription": sub, "agent": agent, "model": model,
            "env": env, "unset": unset, "expires_at": None, "confirm_by": confirm_by,
        }

    def _get_owned_lease(self, cur: sqlite3.Cursor, client: str, lease_id: str) -> sqlite3.Row:
        row = self._store.lease_get(cur, lease_id)
        if row is None:
            raise not_found()
        if row["client"] != client:
            raise not_owner()
        return row

    # --- 租用 API ---
    def request_lease(self, client: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases。必填 worker、role、agent、instance、request_id；可空 model、subscription、purpose、preferences。
        判定順序：400（欄位缺漏、長度：worker≤64、role≤128、purpose≤256、可列印字元；preferences 必須是
        {preferred_models: list[str], floor_model: str|None} 或省略）→ 403 client_mismatch（worker ≠ client）
        → 409 not_allowed（政策或模型；指定訂閱時也檢查）→ 409 instance_busy（同 client 同 instance 有未結束租約）
        → 選定：照 policy.candidates(agent)（或只看指定的那一個），跳過「冷卻中、達上限、沒部署」的；
        都不行時：若至少一個不在冷卻（且已部署）但都達上限 → cap_reached；否則 → all_cooling（附最早的冷卻結束）。
        沒部署的訂閱視同不可用；若全部都只是沒部署 → not_allowed。
        成功：記 requested＋granted 事件（含選定理由、preferences），回 201 {lease}；被拒記 denied 事件。
        """
        missing: list[str] = []
        worker = self._need_str(missing, "worker", body.get("worker"), required=True, max_len=64)
        role = self._need_str(missing, "role", body.get("role"), required=True, max_len=128)
        agent = self._need_str(missing, "agent", body.get("agent"), required=True)
        instance = self._need_str(missing, "instance", body.get("instance"), required=True)
        request_id = self._need_str(missing, "request_id", body.get("request_id"), required=True)
        if missing:
            raise bad_request(missing=missing)
        purpose = self._need_str([], "purpose", body.get("purpose"), required=False, max_len=256)
        model = self._need_str([], "model", body.get("model"), required=False) or None
        specified = self._need_str([], "subscription", body.get("subscription"),
                                   required=False) or None
        preferences = self._check_preferences(body.get("preferences"))
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            hit = self._dedup_hit(cur, client, request_id)
            if hit is not None:
                return hit
            self._require_live_client(cur, client)
            if worker != client:
                ctx = {**self._req_ctx(client, worker, role, agent, instance, specified),
                       "request_id": request_id}
                self._store.log(cur, "denied", to_rfc3339(now), client=client,
                                subscription=specified,
                                payload={**ctx, "reason": "client_mismatch",
                                         "earliest_reset": None})
                err = client_mismatch()
                self._dedup_put(cur, client, request_id, err.status, err.body(), now)
                raise err
            try:
                sub, lease_id = self._select_and_grant(
                    cur, client, worker, role, agent, instance, model, specified,
                    purpose, preferences, now,
                )
            except ApiError as err:
                if err.code == "refused" and request_id:
                    row = self._store.dedup_get(cur, client, request_id)
                    if row is None:
                        self._dedup_put(cur, client, request_id, err.status, err.body(), now)
                raise
            row = self._store.lease_get(cur, lease_id)
            lease = self._lease_obj(cur, lease_id, sub, agent, row["model"], row["confirm_by"])
            resp = {"lease": lease}
            self._dedup_put(cur, client, request_id, 201, resp, now)
            return 201, resp

    def start(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/start。granted→active，效期從此起算（lease_ttl_seconds）；已 active 重送回 200。
        404 not_found／403 not_owner／409 lease_not_active（已結束）。"""
        missing: list[str] = []
        request_id = self._need_str(missing, "request_id", body.get("request_id"), required=True)
        if missing:
            raise bad_request(missing=missing)
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            hit = self._dedup_hit(cur, client, request_id)
            if hit is not None:
                return hit
            self._require_live_client(cur, client)
            row = self._get_owned_lease(cur, client, lease_id)
            if row["status"] == "active":
                resp = {"expires_at": row["expires_at"]}
                self._dedup_put(cur, client, request_id, 200, resp, now)
                return 200, resp
            if row["status"] != "granted":
                raise lease_not_active()
            expires_at = to_rfc3339(
                now + _dt.timedelta(seconds=self._policy.lease_ttl_seconds)
            )
            self._store.lease_update(cur, lease_id, status="active",
                                    started_at=to_rfc3339(now), expires_at=expires_at)
            self._store.log(cur, "started", to_rfc3339(now), client=client, lease_id=lease_id,
                            subscription=row["subscription"],
                            payload={"lease_id": lease_id, "started_at": to_rfc3339(now),
                                     "expires_at": expires_at})
            resp = {"expires_at": expires_at}
            self._dedup_put(cur, client, request_id, 200, resp, now)
            return 200, resp

    def exhausted(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/exhausted。body: signal（列舉見 design 決定 6）、resets_at?、request_id。
        granted（未確認）→ 409 lease_not_confirmed；已結束 → 409 lease_not_active。
        同一筆交易：該訂閱冷卻到 resets_at＋buffer（resets_at 在過去或超過 resets_at_max_ahead_seconds 就忽略，
        改用 default_cooldown_seconds，且不超過 max_cooldown_seconds）；原租約 superseded（理由、冷卻結束）；
        以同一個 worker/role/agent/instance/model（不指定訂閱）申請下一張。
        回 200 {next: lease} 或 200 {next: null, refusal: {reason, earliest_reset?}}。"""
        missing: list[str] = []
        request_id = self._need_str(missing, "request_id", body.get("request_id"), required=True)
        signal = self._need_str(missing, "signal", body.get("signal"), required=True)
        if missing:
            raise bad_request(missing=missing)
        if signal not in SIGNALS:
            raise bad_request(detail="signal 不在列舉內")
        resets_at = body.get("resets_at")
        resets = self._parse_resets_at(resets_at)
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            hit = self._dedup_hit(cur, client, request_id)
            if hit is not None:
                return hit
            self._require_live_client(cur, client)
            row = self._get_owned_lease(cur, client, lease_id)
            if row["status"] == "granted":
                raise lease_not_confirmed()
            if row["status"] != "active":
                raise lease_not_active()
            sub = row["subscription"]
            until = self._cooling_until(sub, resets, now)
            prev = self._store.cool_get(cur, sub)
            if prev is not None and from_rfc3339(prev["until"]) >= until:
                until_str = prev["until"]
            else:
                until_str = to_rfc3339(until)
                self._store.cool_set(cur, sub, until_str, signal, to_rfc3339(now))
            self._store.lease_update(cur, lease_id, status="superseded")
            self._store.log(cur, "superseded", to_rfc3339(now), client=client,
                            lease_id=lease_id, subscription=sub,
                            payload={"lease_id": lease_id, "reason": signal,
                                     "cooling_until": until_str})
            self._store.log(cur, "cooling", to_rfc3339(now), client=client, subscription=sub,
                            payload={"subscription": sub, "until": until_str, "signal": signal,
                                     "updated_at": to_rfc3339(now)})
            try:
                _, next_id = self._select_and_grant(
                    cur, client, row["worker"], row["role"], row["agent"], row["instance"],
                    row["model"], None, None, None, now,
                )
            except ApiError as err:
                if err.code != "refused":
                    raise
                refusal_body: dict = {"reason": err.extra.get("reason")}
                if err.extra.get("earliest_reset"):
                    refusal_body["earliest_reset"] = err.extra["earliest_reset"]
                resp = {"next": None, "refusal": refusal_body}
                self._dedup_put(cur, client, request_id, 200, resp, now)
                return 200, resp
            nxt = self._store.lease_get(cur, next_id)
            resp = {"next": self._lease_obj(cur, next_id, nxt["subscription"], nxt["agent"],
                                           nxt["model"], nxt["confirm_by"])}
            self._dedup_put(cur, client, request_id, 200, resp, now)
            return 200, resp

    @staticmethod
    def _parse_resets_at(value) -> _dt.datetime | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            return None
        try:
            return from_rfc3339(value)
        except ValueError:
            return None

    def _cooling_until(self, sub: str, resets: _dt.datetime | None, now: _dt.datetime) -> _dt.datetime:
        policy = self._policy.subscriptions[sub]
        maxc = policy.max_cooldown_seconds
        if resets is not None and now < resets <= now + _dt.timedelta(
            seconds=self._policy.resets_at_max_ahead_seconds
        ):
            until = resets + _dt.timedelta(seconds=self._policy.cooldown_buffer_seconds)
        else:
            span = policy.default_cooldown_seconds
            if maxc is not None:
                span = min(span, maxc)
            until = now + _dt.timedelta(seconds=span)
        if maxc is not None:
            until = min(until, now + _dt.timedelta(seconds=maxc))
        return until

    def renew(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/renew。只接受 active；granted → lease_not_confirmed；已結束 → lease_not_active。
        冷卻中訂閱上的 active 租約照常可 renew。回 200 {expires_at}。"""
        missing: list[str] = []
        request_id = self._need_str(missing, "request_id", body.get("request_id"), required=True)
        if missing:
            raise bad_request(missing=missing)
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            hit = self._dedup_hit(cur, client, request_id)
            if hit is not None:
                return hit
            self._require_live_client(cur, client)
            row = self._get_owned_lease(cur, client, lease_id)
            if row["status"] == "granted":
                raise lease_not_confirmed()
            if row["status"] != "active":
                raise lease_not_active()
            expires_at = to_rfc3339(
                now + _dt.timedelta(seconds=self._policy.lease_ttl_seconds)
            )
            self._store.lease_update(cur, lease_id, expires_at=expires_at)
            self._store.log(cur, "renewed", to_rfc3339(now), client=client, lease_id=lease_id,
                            subscription=row["subscription"],
                            payload={"lease_id": lease_id, "expires_at": expires_at})
            resp = {"expires_at": expires_at}
            self._dedup_put(cur, client, request_id, 200, resp, now)
            return 200, resp

    def release(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/release。granted 或 active → released；已結束的重送也回 204。回 (204, {})。"""
        missing: list[str] = []
        request_id = self._need_str(missing, "request_id", body.get("request_id"), required=True)
        if missing:
            raise bad_request(missing=missing)
        reason = body.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.isprintable()):
            raise bad_request(detail="reason 須為可列印字串")
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            hit = self._dedup_hit(cur, client, request_id)
            if hit is not None:
                return hit
            self._require_live_client(cur, client)
            row = self._get_owned_lease(cur, client, lease_id)
            if row["status"] in ("granted", "active"):
                self._store.lease_update(cur, lease_id, status="released")
                self._store.log(cur, "released", to_rfc3339(now), client=client,
                                lease_id=lease_id, subscription=row["subscription"],
                                payload={"lease_id": lease_id, "reason": reason})
            self._dedup_put(cur, client, request_id, 204, {}, now)
            return 204, {}

    def report_usage(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/usage。snapshot 必須是 dict；記 usage 事件。回 (204, {})。"""
        snapshot = body.get("snapshot")
        if snapshot is None:
            raise bad_request(missing=["snapshot"])
        if not isinstance(snapshot, dict):
            raise bad_request(detail="snapshot 須為物件")
        request_id = body.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not request_id or not request_id.isprintable():
                raise bad_request(detail="request_id 須為可列印字串")
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            if request_id is not None:
                hit = self._dedup_hit(cur, client, request_id)
                if hit is not None:
                    return hit
            self._require_live_client(cur, client)
            row = self._get_owned_lease(cur, client, lease_id)
            self._store.log(cur, "usage", to_rfc3339(now), client=client, lease_id=lease_id,
                            subscription=row["subscription"],
                            payload={"lease_id": lease_id, "snapshot": snapshot})
            if request_id is not None:
                self._dedup_put(cur, client, request_id, 204, {}, now)
            return 204, {}

    # --- 狀態板（spec status-board） ---
    def board(self) -> dict:
        """{api_version, generated_at, subscriptions: [{name, deployed, active: [{worker, role, agent, since}],
        count, cap, cooling, cooling_until, usage}]}。先做延後結算。usage 由 lgwd.board 的查詢器填（這裡留 None）。
        絕不含 key 的值。"""
        with self._store.txn() as cur:
            now = self._clock.now()
            self._settle(cur, now)
            subs = []
            for name, sub_policy in self._policy.subscriptions.items():
                active = [
                    {"worker": r["worker"], "role": r["role"], "agent": r["agent"],
                     "since": r["started_at"]}
                    for r in self._store.actives_for_sub(cur, name)
                ]
                cooling_until = self._is_cooling(cur, name, now)
                subs.append({
                    "name": name,
                    "deployed": self._keys.deployed(name),
                    "active": active,
                    "count": len(active),
                    "cap": sub_policy.cap,
                    "cooling": cooling_until is not None,
                    "cooling_until": cooling_until,
                    "usage": None,
                })
            return {
                "api_version": API_VERSION,
                "generated_at": to_rfc3339(now),
                "subscriptions": subs,
            }
