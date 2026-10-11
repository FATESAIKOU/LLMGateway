"""租用 API 客戶端（design 決定 9；tasks 2.1）。由 test 實作。

LeaseClient(base_url, token_path, timeout=10)：
- token 從 token_path 讀（去掉結尾換行），不印出、不進 repr。
- request_lease(worker, role, agent, instance, model=None, subscription=None, purpose=None, preferences=None)
  start(lease_id)、exhausted(lease_id, signal, resets_at=None)、renew(lease_id)、release(lease_id, reason=None)、
  report_usage(lease_id, snapshot)、board()
- 每個改變狀態的呼叫自動帶新的 request_id（uuid4）；呼叫方要重送時可傳 request_id 參數。
- 回傳 Result：ok(bool)、status(int|None)、data(dict)、refusal(reason, earliest_reset)|None、error(str|None)、
  unreachable(bool)。連不上或逾時 → unreachable=True、ok=False，不丟例外。不認得的拒絕理由照樣放在 refusal。
- 只用 urllib；不引用 subprocess、herdr、agora。
"""
