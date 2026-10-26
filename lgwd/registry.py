"""Subscription registry 的服務邏輯（design 決定 6、7、8、10 與 v5 的決定 8 取代；spec subscription-registry）。

- list_subscriptions()：所有 subscription 的共通 JSON，依 priority，不含 key；另帶 usage／available_again_at（#60）；
- get_subscription(id, agent)：一筆的共通 JSON，含 key（agent 必填、要在該筆的 agents 裡、key 要已部署）；
- resolve(agent, provider[, model, subscription, exclude])：照政策選把後回
  {schema, env_schema, meta, env}（meta 含 cooldown／model_cooldowns，是提示；不含
  available／available_again_at，帶 usage；env 依 agent 組成）。
  選把集中在這裡（design 決定 3）：純規則在 _pick（釘住先限縮成那把 → provider＋
  agent＋allow 涵蓋／能推導模型 → exclude），resolve 再接已部署 → priority→id；
  **冷卻不參與選把**（不排除、也不是排序鍵）；錯誤 precedence＝輸入形狀 400 →
  查無 404 → 政策 403／no_model → 候選狀態 409。
  每次成功 resolve 記一筆只追加的稽核（provider／model 是中介資料）。
- register_cooldown(id, body)：登錄冷卻，body {"until": RFC 3339 UTC[, "model": 模型 id]}
  （過去或超過上限拒絕；同一筆重複登錄取較晚的 until；到時自動解除）。有 model 時只
  冷卻那個模型，沒有（或 null）時冷卻整把；整把與模型兩層各自獨立、互不刪除。
- 每次 get（過渡期）與登錄冷卻也記一筆只追加的稽核（get 照舊只記 agent；不含 key）。
- 所有時刻經注入的 Clock；對外是 RFC 3339 UTC。
- 環境變數的組成在 providers.build_env（design 決定 2）；這裡只選把與聚合回覆。

HTTP 層把 ApiError 轉成狀態碼與 JSON；這裡不碰 HTTP。
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import logging

from .clock import Clock, from_rfc3339, to_rfc3339
from .errors import (
    agent_not_allowed,
    bad_request,
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
from .providers import CredentialProvider, provider_for
from .store import Store
from .usage import UsageService, available_again_at, internal_usage

_LOG = logging.getLogger("lgwd.registry")

SCHEMA_VERSION = 1

# resolve 回覆形狀的版本（與 list 的 SCHEMA_VERSION 各自獨立；design 決定 1）：
# 不相容變更（移除／改名／改語意）才 bump；只新增 meta 欄位不 bump（消費端忽略新欄位）。
RESOLVE_SCHEMA_VERSION = 1

# env 組成（鍵集與鍵的語意）的版本（design 決定 1、2）：新增／移除 env 鍵或改變
# 鍵的語意時 MUST bump；同一個 env_schema 內不改變鍵集。
ENV_SCHEMA_VERSION = 1

# 登錄冷卻的 model 長度上限（Unicode 字元數；design 決定 1）：allow 是 ["*"] 的
# 把什麼字串都收，過期列又不刪，所以把誤送或亂送的長字串擋在寫入之前。
_MODEL_MAX_CHARS = 200


def _has_wildcard(pattern: str) -> bool:
    """樣式含 fnmatch 的萬用字元（*、?、[；任何位置的 [ 都算）即不可推導。"""
    return any(ch in pattern for ch in ("*", "?", "["))


def _covered(patterns: tuple[str, ...], model: str) -> bool:
    """model 被任一 allow 樣式涵蓋（與政策 loader 同一個 fnmatch）。

    fnmatch 的 `*` 會匹配 `/` 等副作用與分開控制做法見 docs/deploy.md 5.8。
    """
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns)


def _derive_model(sub: SubscriptionPolicy, kind: str) -> tuple[bool, str | None]:
    """model 省略時能不能從該把推導，以及推導出的值（design 決定 4）。

    default 非 null 用 default；claude-subscription 恒算能推導（default null 時用
    agent 自己的預設、合法 200）；其他 kind 用 allow 第一個不含萬用字元的項。
    (False, None)＝推不出，步驟 4 據此回 400 no_model。
    """
    if sub.models_default is not None:
        return True, sub.models_default
    if kind == "claude-subscription":
        return True, None
    for pattern in sub.models_allow:
        if not _has_wildcard(pattern):
            return True, pattern
    return False, None


class RegistryService:
    def __init__(
        self,
        db_path: str,
        policy: Policy,
        keys: KeyStore,
        clock: Clock,
        usage: UsageService | None = None,
    ):
        self._store = Store(db_path)
        self._policy = policy
        self._keys = keys
        self._clock = clock
        # 沒注入＝沒有任何取用量實作（所有 subscription 都是 not_applicable）；
        # 真正的取用量由 main.Gateway 組裝後注入（#60）。
        self._usage = usage if usage is not None else UsageService(clock)

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
        # 用量欄位在交易外合入（只讀快取與觸發判斷，不做 I/O；壞了只影響該筆的 usage）。
        # _item 不動：get 與 list 共用它，get 不帶 usage／available_again_at。
        for item in subs:
            usage, again = self._usage_fields(item, now)
            item["available_again_at"] = again
            item["usage"] = usage
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
        id 清單（不在政策檔的 id 忽略），只在沒有釘住時套用。錯誤照 design 決定 3
        的步驟與 precedence 斷在最前面的一層。成功才記一筆 resolve 稽核。

        冷卻只是提示：不參與候選，也不參與排序（design 決定 2）；選中的那把的
        cooldown／model_cooldowns 只在 meta 裡附上（design 決定 3）。
        """
        # 形狀驗證與步驟 1～5（純規則）在 _pick；這裡只做 key、挑選、附資訊與回覆。
        candidates = self._pick(agent, provider, model, subscription, exclude)

        # 步驟 6：key 未部署的候選排除；全部未部署 → 409。
        # 每把只在這裡讀一次 key（None＝未部署），後面沿用同一份值：deployed() 與
        # get() 分兩次讀的話，中間換檔會讓 200 帶 null 憑證（TOCTOU）。
        candidates = [(sub, info, value, self._keys.get(sub.name))
                      for sub, info, value in candidates]
        candidates = [item for item in candidates if item[3] is not None]
        if not candidates:
            raise not_deployed()

        with self._store.txn() as cur:
            now = self._clock.now()
            # 步驟 7：priority 最小；同分按 id。冷卻不參與（不是排序鍵）。
            selected, provider_info, model_value, key = min(
                candidates, key=lambda item: (item[0].priority, item[0].name))
            # 冷卻資訊只在回覆裡（提示）：選中的那一把的整把與各模型。
            cooldown = self._cooldown(cur, selected.name, now)
            model_cooldowns = self._model_cooldowns(cur, selected.name, now)
            # env 依（provider、kind、agent）組成（providers.build_env）；
            # key 是步驟 6 讀到的那份（非 null）。env 的憑證值只在這裡出現。
            env = provider_info.build_env(agent, key, model_value)
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
            "cooldown": cooldown,
            "model_cooldowns": model_cooldowns,
            "usage": self._safe_usage(selected.name),
        }
        return {
            "schema": RESOLVE_SCHEMA_VERSION,
            "env_schema": ENV_SCHEMA_VERSION,
            "meta": meta,
            "env": env,
        }

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
        """POST /v1/subscriptions/{id}/cooldown {until[, model]}：成功為 None（HTTP 204）。

        未知欄位忽略；同一筆重複登錄取較晚的 until。有 model（且不是 null）時只冷卻
        那個模型；沒有或 null 時冷卻整把；兩層各自獨立、互不刪除。驗證順序照舊：
        未知 id 404 → 缺 until 400 → until 格式／範圍 400 → model 400。
        """
        if subscription not in self._policy.subscriptions:
            raise not_found()
        if "until" not in body:
            raise bad_request(missing=["until"])
        now = self._clock.now()
        until = self._cooldown_until(body["until"], now)
        model = self._cooldown_model(
            self._policy.subscriptions[subscription], body.get("model"))
        stamp = to_rfc3339(now)
        with self._store.txn() as cur:
            if model is None:
                prev = self._store.cooldown_get(cur, subscription)
                effective = until
                if prev is not None and from_rfc3339(prev["until"]) > until:
                    effective = from_rfc3339(prev["until"])
                self._store.cooldown_set(cur, subscription, to_rfc3339(effective))
            else:
                prev = self._store.cooldown_get_model(cur, subscription, model)
                effective = until
                if prev is not None and from_rfc3339(prev["until"]) > until:
                    effective = from_rfc3339(prev["until"])
                self._store.cooldown_set_model(
                    cur, subscription, model, to_rfc3339(effective))
            self._store.audit_add(
                cur, stamp, subscription, "cooldown",
                until=to_rfc3339(effective), model=model,
            )

    def _cooldown_model(self, sub: SubscriptionPolicy, model) -> str | None:
        """驗登錄冷卻的選填 model；回有效的模型 id，或 None（沒給／null＝整把）。

        有給（而且不是 null）就必須是供應商自己的模型 id：非空字串、≤200 字元、不帶
        `<agent>/` 前綴（allow 的 `*` 會匹配 `/`，只靠 allow 擋不到）、不含 fnmatch
        萬用字元、且被該把的 models.allow 涵蓋。任何一件不成立都是 400 bad_request
        （不是 403 model_not_allowed）；不正規化（不去前綴、不改大小寫）。
        """
        if model is None:
            return None
        if not isinstance(model, str) or not model:
            raise bad_request()
        if len(model) > _MODEL_MAX_CHARS:
            raise bad_request()
        if any(model.startswith(agent + "/") for agent in AGENTS):
            raise bad_request()
        if _has_wildcard(model):
            raise bad_request()
        if not _covered(sub.models_allow, model):
            raise bad_request()
        return model

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
    def _safe_usage(self, subscription: str) -> dict:
        """resolve meta 用：任何錯誤都回 unknown／internal，不拖累 resolve（#60）。"""
        try:
            return self._usage.usage_for(subscription)
        except Exception as exc:
            _LOG.warning("usage unavailable for %s: %s", subscription, type(exc).__name__)
            return internal_usage(self._source_of(subscription))

    def _source_of(self, subscription: str) -> str | None:
        try:
            return self._usage.source_of(subscription)
        except Exception:
            return None

    def _usage_fields(self, item: dict, now: _dt.datetime) -> tuple[dict, str | None]:
        """list 用：回 (usage, available_again_at)；失敗該筆 unknown／internal＋null。"""
        try:
            usage = self._usage.usage_for(item["id"])
            cooldown = item["cooldown"]
            until = from_rfc3339(cooldown["until"]) if cooldown else None
            return usage, available_again_at(usage, until, now, self._keys.deployed(item["id"]))
        except Exception as exc:
            _LOG.warning("usage unavailable for %s: %s", item["id"], type(exc).__name__)
            return internal_usage(self._source_of(item["id"])), None

    def _pick(
        self,
        agent: str | None,
        provider: str | None,
        model: str | None,
        subscription: str | None,
        exclude: str | None,
    ) -> list[tuple[SubscriptionPolicy, CredentialProvider, str | None]]:
        """形狀驗證與步驟 1～5（純規則）：回候選 [(sub, provider_info, model)]。

        不碰 keys／DB；每個候選的 provider_info 只解析一次。錯誤照 design 決定 3
        的步驟與 precedence：400 → 404 → 403／no_model → 409 no_candidate。
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

        if subscription is not None:
            # 步驟 1：釘住＝候選先限縮為這一把；後續每一步只在這一把上判斷。
            sub = self._policy.subscriptions.get(subscription)
            if sub is None:
                raise not_found()
            provider_info = provider_for(subscription)
            if provider_info.provider != provider:
                raise bad_request()
            candidates = [(sub, provider_info)]
        else:
            # 步驟 2：provider 查無 → 404（先於步驟 3 的政策拒絕）。
            candidates = []
            for sub in self._policy.in_priority_order():
                provider_info = provider_for(sub.name)
                if provider_info.provider == provider:
                    candidates.append((sub, provider_info))
            if not candidates:
                raise not_found()

        # 步驟 3：agent 不被任何候選允許 → 403（釘住時只看該把）。
        candidates = [(sub, info) for sub, info in candidates if agent in sub.agents]
        if not candidates:
            raise agent_not_allowed()

        # 步驟 4：model 守門（403）／能推導模型（400）；在 priority 之前過濾，
        # 避免「最高 priority 的候選推不出、下一個可以」直接 400（design 決定 3）。
        if model is not None:
            picked = [(sub, info, model) for sub, info in candidates
                      if _covered(sub.models_allow, model)]
            if not picked:
                raise model_not_allowed()
        else:
            picked = []
            for sub, info in candidates:
                ok, derived = _derive_model(sub, info.kind)
                if ok:
                    picked.append((sub, info, derived))
            if not picked:
                raise no_model()

        if subscription is None:
            # 步驟 5（沒有釘住）：排除 exclude 的 id；不在政策檔的 id 自然不命中。
            excluded = set(exclude.split(",")) if exclude else set()
            picked = [item for item in picked if item[0].name not in excluded]
            if not picked:
                raise no_candidate()
        return picked

    def _item(
        self, cur, subscription: str, now: _dt.datetime, key: str | None = None
    ) -> dict:
        """共通 JSON schema（design 決定 7）；不放任何環境變數名。"""
        sub_policy = self._policy.subscriptions[subscription]
        provider = provider_for(subscription)
        cooldown = self._cooldown(cur, subscription, now)
        model_cooldowns = self._model_cooldowns(cur, subscription, now)
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
        item["model_cooldowns"] = model_cooldowns
        # available 仍是「部署＋整把冷卻」的粗提示；模型冷卻不影響它（design 決定 5）。
        item["available"] = deployed and cooldown is None
        return item

    def _cooldown(self, cur, subscription: str, now: _dt.datetime) -> dict | None:
        """{"until": …} 或 null；過了 until（含相等）自動解除。"""
        row = self._store.cooldown_get(cur, subscription)
        if row is not None and from_rfc3339(row["until"]) > now:
            return {"until": row["until"]}
        return None

    def _model_cooldowns(self, cur, subscription: str, now: _dt.datetime) -> dict:
        """{模型 id: {"until": …}}；只列還沒過的（過了含相等即不列），沒有時 {}。"""
        result = {}
        for row in self._store.model_cooldowns_of(cur, subscription):
            if from_rfc3339(row["until"]) > now:
                result[row["model"]] = {"until": row["until"]}
        return result
