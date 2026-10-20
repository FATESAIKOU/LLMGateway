"""Subscription registry 的服務邏輯（design 決定 6、7、8、10 與 v5 的決定 8 取代；spec subscription-registry）。

- list_subscriptions()：所有 subscription 的共通 JSON，依 priority，不含 key；
- get_subscription(id, agent)：一筆的共通 JSON，含 key（agent 必填、要在該筆的 agents 裡、key 要已部署）；
- resolve(agent, provider[, model, subscription, exclude])：照政策選把後回
  {schema, env_schema, meta, env}（meta 不含 cooldown／available；env 依 agent 組成）。
  選把集中在這裡的一個地方（design 決定 3）：釘住先限縮成那把 → provider＋agent＋
  allow 涵蓋／能推導模型 → exclude → 已部署 → 非冷卻 → priority→id；
  錯誤 precedence＝輸入形狀 400 → 查無 404 → 政策 403／no_model → 候選狀態 409。
  每次成功 resolve 記一筆只追加的稽核（provider／model 是中介資料）。
- register_cooldown(id, body)：登錄冷卻，body {"until": RFC 3339 UTC}（過去或超過上限拒絕；
  同一筆重複登錄取較晚的 until；到時自動解除）。
- 每次 get（過渡期）與登錄冷卻也記一筆只追加的稽核（get 照舊只記 agent；不含 key）。
- 所有時刻經注入的 Clock；對外是 RFC 3339 UTC。
- 環境變數的組成在 providers.build_env（design 決定 2）；這裡只選把與聚合回覆。

HTTP 層把 ApiError 轉成狀態碼與 JSON；這裡不碰 HTTP。
"""

from __future__ import annotations

import datetime as _dt
import fnmatch

from .clock import Clock, from_rfc3339, to_rfc3339
from .errors import (
    agent_not_allowed,
    bad_request,
    cooling,
    invalid_until,
    model_not_allowed,
    no_candidate,
    no_model,
    not_deployed,
    not_found,
    until_out_of_range,
)
from .keys import KeyStore
from .policy import AGENTS, Policy, SubscriptionPolicy
from .providers import PROVIDERS, provider_for
from .store import Store

SCHEMA_VERSION = 1

# resolve 回覆形狀的版本（與 list 的 SCHEMA_VERSION 各自獨立；design 決定 1）：
# 不相容變更（移除／改名／改語意）才 bump；只新增 meta 欄位不 bump（消費端忽略新欄位）。
RESOLVE_SCHEMA_VERSION = 1

# env 組成（鍵集與鍵的語意）的版本（design 決定 1、2）：新增／移除 env 鍵或改變
# 鍵的語意時 MUST bump；同一個 env_schema 內不改變鍵集。
ENV_SCHEMA_VERSION = 1

# resolve 認得的 agent 與 AGENTS 同一張表；未知 → 400 bad_request（不是 403：
# 403 的語意是政策不允許，不是輸入打錯）。


def _has_wildcard(pattern: str) -> bool:
    """樣式裡有 fnmatch 的萬用字元（*、?、開頭字元的 [）。"""
    return any(ch in pattern for ch in ("*", "?", "["))


def _covered(patterns: tuple[str, ...], model: str) -> bool:
    """model 被任一 allow 樣式涵蓋（與政策 loader 同一個 fnmatch）。"""
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns)


def _derivable(sub: SubscriptionPolicy, kind: str) -> bool:
    """model 省略時能不能從該把推導模型（design 決定 4）。

    claude-subscription 恒算能推導（default null 時用 agent 自己的預設、合法 200）。
    """
    if kind == "claude-subscription" or sub.models_default is not None:
        return True
    return any(not _has_wildcard(pattern) for pattern in sub.models_allow)


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

    # --- 介面 ---
    def list_subscriptions(self) -> dict:
        """GET /v1/subscriptions：{schema, subscriptions:[…]}，依 priority，不含 key。"""
        now = self._clock.now()
        with self._store.txn() as cur:
            subs = [
                self._item(cur, sub.name, now)
                for sub in self._policy.in_priority_order()
            ]
        return {"schema": SCHEMA_VERSION, "subscriptions": subs}

    def resolve(
        self,
        agent: str | None,
        provider: str | None,
        model: str | None = None,
        subscription: str | None = None,
        exclude: str | None = None,
    ) -> dict:
        """GET /v1/resolve：選把後回 {schema, env_schema, meta, env}。

        subscription 是釘住（有給時只看該把，不代換另一把）；exclude 是逗號分隔的
        id 清單（不在政策檔的 id 忽略），只在没有釘住時套用。錯誤照 design 決定 3
        的步驟與 precedence 斷在最前面的一層。成功才記一筆 resolve 稽核。
        """
        # 形狀驗證（400 先於 404；訊息固定字串、不回顯請求值）。
        missing = [
            name for name, value in (("agent", agent), ("provider", provider))
            if not value
        ]
        if missing:
            raise bad_request(missing=missing)
        if agent not in AGENTS:
            raise bad_request()
        if subscription is not None and exclude:
            # 釘住與排除是兩個互斥的意圖；此檢查先於 id 存在與 provider 一致。
            raise bad_request()
        if subscription is not None and subscription not in self._policy.subscriptions:
            raise not_found()
        if subscription is not None and provider_for(subscription).provider != provider:
            raise bad_request()

        if subscription is not None:
            # 步驟 1：釘住＝候選先限縮為這一把；後續每一步只在這一把上判斷。
            candidates = [self._policy.subscriptions[subscription]]
        else:
            # 步驟 2：provider 查無 → 404（先於步驟 3 的政策拒絕）。
            candidates = [
                sub for sub in self._policy.in_priority_order()
                if provider_for(sub.name).provider == provider
            ]
            if not candidates:
                raise not_found()

        # 步驟 3：agent 不被任何候選允許 → 403（釘住時只看該把）。
        candidates = [sub for sub in candidates if agent in sub.agents]
        if not candidates:
            raise agent_not_allowed()

        # 步驟 4：model 守門（403）／能推導模型（400）；在 priority 之前過濾，
        # 避免「最高 priority 的候選推不出、下一個可以」直接 400（design 決定 3）。
        if model is not None:
            candidates = [sub for sub in candidates if _covered(sub.models_allow, model)]
            if not candidates:
                raise model_not_allowed()
        else:
            candidates = [
                sub for sub in candidates
                if _derivable(sub, provider_for(sub.name).kind)
            ]
            if not candidates:
                raise no_model()

        if subscription is None:
            # 步驟 5（没有釘住）：排除 exclude 的 id；不在政策檔的 id 自然不命中。
            excluded = set(exclude.split(",")) if exclude else set()
            candidates = [sub for sub in candidates if sub.name not in excluded]
            if not candidates:
                raise no_candidate()

        # 步驟 6：key 未部署的候選排除；全部未部署 → 409（先於冷卻）。
        deployed = [sub for sub in candidates if self._keys.deployed(sub.name)]
        if not deployed:
            raise not_deployed()

        with self._store.txn() as cur:
            # 步驟 7：冷卻中的候選排除；全部冷卻 → 409 cooling（until＝最早恢復時刻）。
            now = self._clock.now()
            live = []
            earliest = None
            for sub in deployed:
                cooldown = self._cooldown(cur, sub.name, now)
                if cooldown is None:
                    live.append(sub)
                elif earliest is None or from_rfc3339(cooldown["until"]) < earliest:
                    earliest = from_rfc3339(cooldown["until"])
            if not live:
                raise cooling(until=to_rfc3339(earliest))

            # 步驟 8：priority 最小；同分按 id。
            selected = min(live, key=lambda sub: (sub.priority, sub.name))
            provider_info = provider_for(selected.name)
            model_value = (
                model if model is not None else self._derive_model(selected, provider_info)
            )
            # env 依（provider、kind、agent）組成（providers.build_env）；
            # deployed 檢查已保證 key 非 null（env 的憑證值只在這裡出現）。
            env = provider_info.build_env(agent, self._keys.get(selected.name), model_value)
            self._store.audit_add(
                cur, to_rfc3339(now), selected.name, "resolve",
                agent=agent, provider=provider_info.provider, model=model_value,
            )

        meta = {
            "subscription": selected.name,
            "provider": provider_info.provider,
            "kind": provider_info.kind,
            "agent": agent,
            "model": model_value,
            "models": {
                "allow": list(selected.models_allow),
                "default": selected.models_default,
            },
            "endpoint": provider_info.endpoint,
            "priority": selected.priority,
        }
        return {
            "schema": RESOLVE_SCHEMA_VERSION,
            "env_schema": ENV_SCHEMA_VERSION,
            "meta": meta,
            "env": env,
        }

    @staticmethod
    def _derive_model(sub: SubscriptionPolicy, provider_info) -> str | None:
        """model 省略時選中那把的模型值（design 決定 4；sidecar 現行推導的同一個機械）。

        default 非 null 用 default；claude-subscription 用 agent 自己的預設（null）；
        其他 kind 用 allow 第一個不含萬用字元的項。沒有候選能到這裡還推不出
        （選把步驟 4 已擋），最後一行只是防禦。
        """
        if sub.models_default is not None:
            return sub.models_default
        if provider_info.kind == "claude-subscription":
            return None
        for pattern in sub.models_allow:
            if not _has_wildcard(pattern):
                return pattern
        raise no_model()  # pragma: no cover - 選把已保證可推導

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
        """POST /v1/subscriptions/{id}/cooldown {until}：成功為 None（HTTP 204）。

        未知欄位忽略；同一筆重複登錄取較晚的 until。
        """
        if subscription not in self._policy.subscriptions:
            raise not_found()
        if "until" not in body:
            raise bad_request(missing=["until"])
        now = self._clock.now()
        until = self._cooldown_until(body["until"], now)
        stamp = to_rfc3339(now)
        with self._store.txn() as cur:
            prev = self._store.cooldown_get(cur, subscription)
            effective = until
            if prev is not None and from_rfc3339(prev["until"]) > until:
                effective = from_rfc3339(prev["until"])
            self._store.cooldown_set(cur, subscription, to_rfc3339(effective))
            self._store.audit_add(
                cur, stamp, subscription, "cooldown", until=to_rfc3339(effective),
            )

    def _cooldown_until(self, until, now: _dt.datetime) -> _dt.datetime:
        """驗 until 並回 UTC datetime；型別／格式不對丟 invalid_until，過去或超過上限丟 until_out_of_range。"""
        if not isinstance(until, str):
            raise invalid_until()
        try:
            moment = from_rfc3339(until)
        except ValueError:
            raise invalid_until() from None
        if moment < now:
            raise until_out_of_range()
        max_ahead = _dt.timedelta(seconds=self._policy.until_max_ahead_seconds)
        if moment > now + max_ahead:
            raise until_out_of_range()
        return moment

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
        item["available"] = deployed and cooldown is None
        return item

    def _cooldown(self, cur, subscription: str, now: _dt.datetime) -> dict | None:
        """{"until": …} 或 null；過了 until（含相等）自動解除。"""
        row = self._store.cooldown_get(cur, subscription)
        if row is not None and from_rfc3339(row["until"]) > now:
            return {"until": row["until"]}
        return None
