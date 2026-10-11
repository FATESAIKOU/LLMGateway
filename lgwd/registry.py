"""Subscription registry 的服務邏輯（design 決定 6、7、8、10；spec subscription-registry）。

- list_subscriptions()：所有 subscription 的共通 JSON，依 priority，不含 key；
- get_subscription(id, agent)：一筆的共通 JSON，含 key（agent 必填、要在該筆的 agents 裡、key 要已部署）；
- register_cooldown(id, body)：登錄 5h／1w 冷卻（resets_at＋2 分鐘或窗口預設長度；同一窗口取較晚；到時自動解除）。
- 每次 get 與登錄冷卻記一筆只追加的稽核（時刻、subscription id、agent 或窗口與結束時刻；不含 key）。
- 所有時刻經注入的 Clock；對外是 RFC 3339 UTC。

HTTP 層把 ApiError 轉成狀態碼與 JSON；這裡不碰 HTTP。
"""

from __future__ import annotations

import datetime as _dt

from .clock import Clock, from_rfc3339, to_rfc3339
from .errors import agent_not_allowed, bad_request, not_deployed, not_found
from .keys import KeyStore
from .policy import Policy
from .providers import PROVIDERS, provider_for
from .store import Store

SCHEMA_VERSION = 1

WINDOWS = ("5h", "1w")
WINDOW_DEFAULT_SECONDS = {"5h": 5 * 60 * 60, "1w": 7 * 24 * 60 * 60}


class RegistryService:
    def __init__(
        self,
        db_path: str,
        policy: Policy,
        keys: KeyStore,
        clock: Clock,
        providers=None,
    ):
        self._store = Store(db_path)
        self._policy = policy
        self._keys = keys
        self._clock = clock
        self._providers = dict(PROVIDERS if providers is None else providers)

    def close(self) -> None:
        self._store.close()

    # --- 三個介面 ---
    def list_subscriptions(self) -> dict:
        """GET /v1/subscriptions：{schema, subscriptions:[…]}，依 priority，不含 key。"""
        now = self._clock.now()
        with self._store.txn() as cur:
            subs = [
                self._item(cur, sub.name, now)
                for sub in self._policy.in_priority_order()
            ]
        return {"schema": SCHEMA_VERSION, "subscriptions": subs}

    def get_subscription(self, subscription: str, agent: str | None) -> dict:
        """GET /v1/subscriptions/{id}?agent=：一筆的共通 JSON，含 key。"""
        if not isinstance(agent, str) or not agent:
            raise bad_request(missing=["agent"])
        sub_policy = self._policy.subscriptions.get(subscription)
        if sub_policy is None:
            raise not_found()
        if agent not in sub_policy.agents:
            raise agent_not_allowed()
        key = self._keys.get(subscription)
        if key is None:
            raise not_deployed()
        now = self._clock.now()
        with self._store.txn() as cur:
            item = self._item(cur, subscription, now, key=key)
            self._store.audit_add(cur, to_rfc3339(now), subscription, "get", agent=agent)
        return item

    def register_cooldown(self, subscription: str, body: dict) -> None:
        """POST /v1/subscriptions/{id}/cooldown：成功為 None（HTTP 204）。"""
        if subscription not in self._policy.subscriptions:
            raise not_found()
        window = body.get("window")
        if window not in WINDOWS:
            raise bad_request(detail="window must be 5h or 1w")
        now = self._clock.now()
        until = self._cooldown_until(window, body.get("resets_at"), now)
        stamp = to_rfc3339(now)
        with self._store.txn() as cur:
            prev = self._store.cooldown_get(cur, subscription, window)
            effective = until
            if prev is not None and from_rfc3339(prev["until"]) > until:
                effective = from_rfc3339(prev["until"])
            self._store.cooldown_set(cur, subscription, window, to_rfc3339(until))
            self._store.audit_add(
                cur, stamp, subscription, "cooldown",
                window=window, until=to_rfc3339(effective),
            )

    def _cooldown_until(
        self, window: str, resets_at, now: _dt.datetime
    ) -> _dt.datetime:
        buffer = _dt.timedelta(seconds=self._policy.cooldown_buffer_seconds)
        if resets_at not in (None, ""):
            if not isinstance(resets_at, str):
                raise bad_request(detail="resets_at must be RFC 3339")
            try:
                resets = from_rfc3339(resets_at)
            except ValueError:
                raise bad_request(detail="resets_at must be RFC 3339") from None
            if resets < now:
                raise bad_request(detail="resets_at is in the past")
            max_ahead = _dt.timedelta(seconds=self._policy.resets_at_max_ahead_seconds)
            if resets > now + max_ahead:
                raise bad_request(detail="resets_at is too far ahead")
            return resets + buffer
        return now + _dt.timedelta(seconds=WINDOW_DEFAULT_SECONDS[window])

    # --- 內部 ---
    def _item(
        self, cur, subscription: str, now: _dt.datetime, key: str | None = None
    ) -> dict:
        """共通 JSON schema（design 決定 7）；不放任何環境變數名。"""
        sub_policy = self._policy.subscriptions[subscription]
        provider = provider_for(subscription)
        cooldown = self._cooldown(cur, subscription, now)
        deployed = True if key is not None else self._keys.deployed(subscription)
        item = {
            "schema": SCHEMA_VERSION,
            "id": subscription,
            "provider": provider.provider,
            "kind": provider.kind,
            "agents": list(sub_policy.agents),
            "priority": sub_policy.priority,
            "models": {
                "allow": list(sub_policy.models_allow),
                "default": sub_policy.models_default,
            },
            "endpoint": provider.endpoint,
        }
        if key is not None:
            item["key"] = key
        item["cooldown"] = cooldown
        item["available"] = deployed and all(v is None for v in cooldown.values())
        return item

    def _cooldown(self, cur, subscription: str, now: _dt.datetime) -> dict:
        """{"5h": null|{until}, "1w": null|{until}}；過了結束時刻自動解除。"""
        out: dict = {window: None for window in WINDOWS}
        for row in self._store.cooldown_for(cur, subscription):
            if row["window"] in out and from_rfc3339(row["until"]) > now:
                out[row["window"]] = {"until": row["until"]}
        return out
