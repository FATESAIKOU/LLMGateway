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

from .clock import Clock
from .keys import KeyStore
from .policy import Policy


class LeaseService:
    def __init__(self, db_path: str, policy: Policy, keys: KeyStore, clock: Clock):
        raise NotImplementedError

    # --- 客戶端身分（design 決定 4） ---
    def add_client(self, name: str) -> str:
        """新增客戶端，回傳一次性的明文憑證（32 bytes 隨機、urlsafe base64）；只存 SHA-256。名稱重複丟 ValueError。"""
        raise NotImplementedError

    def revoke_client(self, name: str) -> None:
        """撤銷；該客戶端所有未結束的租約記為 revoked。"""
        raise NotImplementedError

    def list_clients(self) -> list[dict]:
        """[{name, created_at, revoked_at}]；不含雜湊。"""
        raise NotImplementedError

    def authenticate(self, token: str) -> str | None:
        """回傳客戶端名稱；無效或已撤銷回 None。用 hmac.compare_digest 比對雜湊。"""
        raise NotImplementedError

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
        raise NotImplementedError

    def start(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/start。granted→active，效期從此起算（lease_ttl_seconds）；已 active 重送回 200。
        404 not_found／403 not_owner／409 lease_not_active（已結束）。"""
        raise NotImplementedError

    def exhausted(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/exhausted。body: signal（列舉見 design 決定 6）、resets_at?、request_id。
        granted（未確認）→ 409 lease_not_confirmed；已結束 → 409 lease_not_active。
        同一筆交易：該訂閱冷卻到 resets_at＋buffer（resets_at 在過去或超過 resets_at_max_ahead_seconds 就忽略，
        改用 default_cooldown_seconds，且不超過 max_cooldown_seconds）；原租約 superseded（理由、冷卻結束）；
        以同一個 worker/role/agent/instance/model（不指定訂閱）申請下一張。
        回 200 {next: lease} 或 200 {next: null, refusal: {reason, earliest_reset?}}。"""
        raise NotImplementedError

    def renew(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/renew。只接受 active；granted → lease_not_confirmed；已結束 → lease_not_active。
        冷卻中訂閱上的 active 租約照常可 renew。回 200 {expires_at}。"""
        raise NotImplementedError

    def release(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/release。granted 或 active → released；已結束的重送也回 204。回 (204, {})。"""
        raise NotImplementedError

    def report_usage(self, client: str, lease_id: str, body: dict) -> tuple[int, dict]:
        """POST /v1/leases/{id}/usage。snapshot 必須是 dict；記 usage 事件。回 (204, {})。"""
        raise NotImplementedError

    # --- 狀態板（spec status-board） ---
    def board(self) -> dict:
        """{api_version, generated_at, subscriptions: [{name, deployed, active: [{worker, role, agent, since}],
        count, cap, cooling, cooling_until, usage}]}。先做延後結算。usage 由 lgwd.board 的查詢器填（這裡留 None）。
        絕不含 key 的值。"""
        raise NotImplementedError
