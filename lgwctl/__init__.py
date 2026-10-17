"""lgwctl：LLMGateway 的管理指令（#35 安裝簡單化 T1）。

用 ``python3 -m lgwctl <子指令>`` 跑，cwd 要在某一版的目錄
（``/opt/llmgateway/current``）；``/usr/local/bin/llmgwctl`` 只是切進 current
再轉呼叫的 wrapper（由 ``llmgwctl install`` 產生）。

子指令：install、check、status、upgrade、rollback、sync。遷移用的
prepare-migration／cutover／confirm／rollback-to-user 由 ``lgwctl/migrate.py``
提供（T1 只預留分派，見 cli.py）。

公開 repo 的規則：任何輸出、log、例外訊息都不含 key 的值或解密密碼；所有
路徑以 ``LLMGW_PREFIX`` 為前綴（測試指到暫存目錄），外部指令可由環境變數
換成假指令（見 layout.py）。
"""
