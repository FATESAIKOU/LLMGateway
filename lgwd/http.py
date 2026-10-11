"""HTTP 層（design 決定 6、11）。由 test 實作。

- make_api_server(service, host, port) → ThreadingHTTPServer：路由 /v1/*；Bearer 認身分（service.authenticate），
  沒有或錯誤 → 401；JSON 解析失敗 → 400；ApiError → 對應狀態碼＋body()；每個回覆加 "api_version": 1。
  路由：POST /v1/leases、POST /v1/leases/{id}/(start|exhausted|renew|release|usage)、GET /v1/board。
- make_board_server(service, host="127.0.0.1", port) → 只服務 GET /board 的唯讀 HTML（不要憑證、全部輸出跳脫）。
  host 不是 loopback 時丟 ValueError（HTML 只綁 loopback）。
- log 只記方法、路徑（租約 id 可）、狀態碼；不記標頭與 body。
"""
