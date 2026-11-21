# design：LLMGateway 與 MLP 脫鉤（#221）

對應工單：#221（b 級設計單；本人核准開工前不實作）。上層：MyPMO#297 分流 E2、MyPMO#469（決定檔 `decisions/2026-11-21-1500-manual.yml`）。相關：MyLinuxPool#1511、MyLinuxPool#864。

> **重要前提**：**本方案（G1＝A）只解決「輪換後位址與主機金鑰變動不再要人手動改」與「服務生命週期綁死隧道」，不解決 `mlp-gateway` 整台掛掉的全域單點問題**。
>
> **公開 repo 規範**：本文不寫真實主機 IP、主機名、帳號密碼、金鑰內容或私人資訊；位址與埠以 `<會合點>`、`<服務主機>`、`<API埠>`、`<SSH埠>` 等佔位符表示，本地檢視目錄以 `<MLP checkout>` 表示。

## 已決定事項與待核准事項（單一正本見 `proposal.md`）

> 決策全文、已排除選項理由與「給 MLP PM 的問題」以 [`proposal.md`](proposal.md) 為唯一正本；本節僅列對應設計章節摘要。

- **G1＝A（已定案）**：只解輪換耦合與服務生命週期脫鉤（主方案 M）；B1／B2／C 已排除（比 A 更不穩定）。見 §4.3。
- **G2＝確定性事件驅動自 GitHub 抓 Gateway 位址（已定案）**：由 `llmgateway-tunnel` 自行維護連線狀態——**健康時不輪詢**，連不上時才以唯讀 GitHub key 抓取 MLP `NODE_GATEWAY`，更新本機狀態後重連，失敗保留最後已知位址持續重試（兩組輸入）。見 §4.4、§4.5。
- **G3＝B（已定案）**：先不另開不經跳板機的進場方式，**`4711` 一律保留**（不實作 `REMOTE_SSH=off`）。見 §5.1。
- **待本人核准（唯一門檻）**：這份設計（含放寬 `docs/deploy.md:36`「不放 token 檔」、放寬主 spec「不依賴 `NODE_*`」，以及接受信任方向／拓樸可見性／格式注入風險與緩解、平穩期悄然到期限制）可不可以開工？見 §4.5、§10。

---

## 1. 耦合面摘要（一手行號盤點見附錄 A）

| 耦合點 | 目前位置 | 問題與影響 | 本設計處置 |
|---|---|---|---|
| 1. 靜態位址與 SSH 埠 | `/etc/llmgateway/tunnel.env` 的 `RENDEZVOUS` | 輪換後 IP／SSH 埠變動即斷線，需人工上機改。 | 初始 `RENDEZVOUS` 退為「最後已知位址」基準；連不上時自動抓 `NODE_GATEWAY` 更新本機 follow 檔。 |
| 2. 靜態主機金鑰 pin | `~llmgw/.ssh/known_hosts_rendezvous` | 輪換換 host key（或供應商回收舊 IP）時 `StrictHostKeyChecking=yes` 拒連。 | 自動驗證新 `host_key` 並單筆原子寫入私用 `.tunnel_known_hosts`（fail-closed，不退回 `accept-new`）。 |
| 3. `sshproxy` 授權公鑰 | `TUNNEL_EXTRA_PUBKEYS` ↔ `id_llmgateway_tunnel.pub` | 跨 repo 身份信任契約須確認長期保留。 | 列入 Q-MLP-D 確認長期契約與撤換流程。 |
| 4. 固定保留埠 `4710`／`4711` | `tunnel.env`、`NODE_GATEWAY.ports.llmgateway` | G3＝B 裁定不另開帶外進場。 | **`4711` 一律保留**（留空亦為 `4711`），與 `4710` 隨同一次隧道自動切往新會合點。 |
| 5. `lgwctl` 生命週期強綁隧道 | `layout.py`、`release.py`、`install.py`、`check.py`、`status.py`、`units.py`、`probe.py` | 無法在無隧道環境裝／檢純本地 `lgwd`；缺 `start`／`stop`／`restart`。 | 補齊 `start`／`stop`／`restart` 並將隧道改為可選（存在時仍嚴格檢查）。 |
| 6. Mac 客戶端連線指引 | `docs/deploy.md` §4.1 手動 `known_hosts_rendezvous` | 輪換後 Mac 同步斷線。 | 維持 `客戶端自備通道` 不提供連線程式，向 MLP 建議補基於 `NODE_GATEWAY` 的轉發指令（Q-MLP-E）。 |

---

## 2. 架構介面圖（現況 vs 目標單一路徑架構）

### 2.1 現況架構（服務、隧道、靜態 pin 全耦合）

```
+-----------------------+                          +---------------------------+
| LLMGateway 主機       |                          | MLP Gateway (<會合點>)    |
|  lgwd (127.0.0.1:8480)|<--+                      |  sshproxy authorized_keys |
|  sshd (127.0.0.1:22)  |<--+--+                   |  含 TUNNEL_EXTRA_PUBKEYS  |
|                       |   |  |                   |                           |
| llmgateway-tunnel.svc |---+--+-- ssh -R -------->| 127.0.0.1:4710 (API)      |
|  tunnel.env (靜態寫死)|          (靜態           | 127.0.0.1:4711 (管理 SSH) |
|  known_hosts_rendezvous          known_hosts)    +---------------------------+
| lgwctl (強綁隧道unit) |                             ^                   ^
+-----------------------+                      +------+------+     +------+------+
                                               | Worker      |     | Mac 客戶端  |
                                               | sidecar     |     | lgw.client  |
                                               +-------------+     +-------------+
```

### 2.2 目標架構：確定性單一路徑（`llmgwctl` 解耦＋事件驅動 `llmgateway-tunnel`）

```
  [GitHub API：FATESAIKOU/MyLinuxPool -> actions/variables/NODE_GATEWAY]
       ^
       | (僅在「連不上」時抓取，健康時完全不輪詢；具退避／速率保護)
       | 輸入②：GATEWAY_SOURCE_REPO + GATEWAY_SOURCE_PATH + GATEWAY_GITHUB_KEY_FILE (drop-in LoadCredential, 0600)
+------+-----------------------------------------------------------------------+
| LLMGateway 主機（純本地模式可完全不裝／不啟右側隧道）                       |
|                                                                              |
|  +----------------------------------+    +--------------------------------+  |
|  | [script a: 擴充既有 llmgwctl]    |    | [script b': 改現有隧道守護]    |  |
|  |  - start/stop/restart           |    | ops-scripts/llmgateway-tunnel  |  |
|  |  - follow-reset [--fetch-state] |    | + llmgateway-tunnel.service    |  |
|  |  - tunnel-apply [--confirm] (P3)|    |   (出貨不含 LoadCredential；   |  |
|  |  - install/upgrade/rollback/sync|    |    具同款 systemd 加固)        |  |
|  |  - check/status（隧道改為可選； |    | - 輸入①：IDENTITY＋本機使用者  |  |
|  |    認 FOLLOW_FILE 六態＋抓取分類|    |   ＋初始 RENDEZVOUS/KNOWN_HOSTS|  |
|  |    ＋TOKEN_EXPIRES_AT/401/403   |    | - 同行程抓->嚴格驗格式->寫檔   |  |
|  |    告警；O_NOFOLLOW）           |    |   -> -l/-- 重連；probe 200 後  |  |
|  |  只必管 llmgateway.service      |    |   寫 .tunnel_generation JSON   |  |
|  +----------------+-----------------+    +---------------+----------------+  |
|          lgwd (127.0.0.1:8480) <---------+---------------+----------------+  |
|          (無應用層 auth；只綁 loopback)   (無隧道時免啟)  | ssh -R 4710 & 4711|
+----------------------------------------------------------+ (G3=B 一律保留4711)|
                                                           +-------------------+
                                                           |
                                                           v
                                                 +---------------------------+
                                                 | MLP Gateway (<會合點>)    |
                                                 |  127.0.0.1:4710 -> API    |
                                                 |  127.0.0.1:4711 -> 管理SSH|
                                                 +---------------------------+
                                                    ^                   ^
                                             +------+------+     +------+------+
                                             | Worker      |     | Mac 客戶端  |
                                             | sidecar     |     | 自備通道    |
                                             +-------------+     +-------------+
```

---

## 3. script a 設計：擴充既有 `llmgwctl` 並將隧道改為可選

### 3.1 決定：只擴充 `llmgwctl`，不新造 `lgw-service` 與非 systemd 模式（PM 預設）

- **為什麼**：`llmgwctl` 已有原子切版、policy dry-run、失敗自動回滾與安全 fd 檢查（`O_NOFOLLOW`、`LoadCredential`）。最小集合只需：
  1. 在 `lgwctl/cli.py` 補上 `start`、`stop`、`restart` 三個子命令（`lgwctl/units.py` 已有 `units.start` 與 `units.restart`，僅需補 `units.stop`），操作對象**只管 `llmgateway.service`**，啟動／重啟後以 `probe.wait_local_ok` 驗證本機 `GET /v1/subscriptions` HTTP 200，`start`／`stop` 皆冪等回 `0`；另新增需 root 權限之顯式重置子命令 **`follow-reset`**（非 root 呼叫直接拒絕並提示需 root；預設執行時以安全路徑檢查移除 `/var/lib/llmgateway/.tunnel_generation` 與 `/var/lib/llmgateway/.tunnel_fetch_backoff` 並重置 `.tunnel_fetch_state`；加上 **`--fetch-state`** 時**只清除 `.tunnel_fetch_state` 與 `.tunnel_fetch_backoff`，不動 `.tunnel_generation` 水位與 `FOLLOW_FILE`**，供換好 token 後清除 `auth_failed` 告警，絕不要求維運人員手動 `rm`）。
  2. 將 `llmgateway-tunnel.service` 與 `tunnel.env` 從「必選」改為「**可選**」：
     - `layout.py`：
       - `UNIT_NAMES` 拆為核心 `CORE_UNIT_NAMES = (SERVICE_NAME,)` 與可選 `OPTIONAL_UNIT_NAMES = (TUNNEL_SERVICE_NAME,)`（保留 `TUNNEL_SERVICE_NAME` 常數供可選檢查與 `migrate.py` 複用）。
       - **`tunnel_config_state()` 與新增 `tunnel_follow_state()`**：
         - **保留 `tunnel_config_state() -> tuple[bool, str]` 既有簽名與語意**（供 `install.py:494`、`migrate.py:1322,1412` 相容使用）：若 `tunnel.env` 已設定動態模式（`GATEWAY_SOURCE_REPO`／`GATEWAY_SOURCE_PATH`／`GATEWAY_GITHUB_KEY_FILE` 或 `FOLLOW_FILE`），`tunnel_config_state()` 改檢 `IDENTITY`、本機 SSH 使用者（`TUNNEL_USER` 或 `RENDEZVOUS` 之 `user@`）與動態設定（含初始 `RENDEZVOUS`／`FOLLOW_FILE` 可用性），不再強制要求靜態 `KNOWN_HOSTS` 檔；未設任何隧道鍵時視為未配置；僅設 `RENDEZVOUS` 時沿用既有靜態三鍵檢查。
         - **新增 `tunnel_follow_state() -> tuple[Literal["unconfigured", "ok", "missing", "invalid", "regressed", "unreadable"], int | None]`**，明確區分未啟用動態追蹤、正常（附帶 `.tunnel_generation` JSON 內已驗證 `verified` 之整數 `generation`，未驗證時為 `None`）、缺失、格式無效、`generation` 回退（**`regressed` 一律以 `FOLLOW_FILE.generation < verified.generation` 判定；`FOLLOW_FILE` 比 `verified` 新但未驗通之過渡態不判 `regressed`**），以及**非 root 呼叫者無權限讀取 `0700` `STATE_DIR` 時的 `unreadable`**（`PermissionError` 不得回 `missing`）。
         - 讀取最近一次抓取結果分類檔 `/var/lib/llmgateway/.tunnel_fetch_state`（無祕密 JSON 或單行狀態，固定分類：`ok`、`regressed`、`invalid`、`hostkey_missing`、`auth_failed`、`rate_limited`、`unreachable`、`config_partial`、`credential_unreadable`，未抓過則為 `none`）。
         - **安全讀取（`O_NOFOLLOW`）**：`root` 端（`check`／`status`）讀取位於 `llmgw` 可寫目錄 `STATE_DIR` 內的 `FOLLOW_FILE`、`.tunnel_generation`、`.tunnel_token_meta`、`.tunnel_fetch_state` 時，**一律使用 `O_NOFOLLOW`＋`stat.S_ISREG` 檢查＋檔案大小上限（例如 64 KiB）**，拒絕跟隨 symlink（遇 symlink 或超限回 `invalid`，不讀取目標內容）。
     - `release.py`：**於 P1 即從 `REQUIRED_FILES` 移除 `llmgateway-tunnel` 與 `deploy/units/llmgateway-tunnel.service`**（使純本地發布包可乾淨通過驗證；當機器存在或版本目錄含隧道 unit 時，仍於 `units` 與 `check` 處理）。
     - `units.py`：`install_unit_files` 僅必裝 `CORE_UNIT_NAMES`，可選 unit 若存在於版本目錄再裝；`snapshot_units`／`restore_units` 改對兩個都做存在性快照，確保對含舊 unit 的 `previous` 目錄 `rollback` 不崩潰。
     - `install.py`：無隧道環境下不再強制產生 `tunnel.env` 範本或在隧道未設定時標記 `todo`（改為僅在已設定或顯式啟用時處理）。
     - `check.py` 與 `status.py`（**follow 檔六態、最近抓取分類、運行中告警、無權限與 token 到期告警**）：
       - `llmgateway.service` 為必檢；`llmgateway-tunnel.service` 為可選——**只要機器上存在該 unit 檔（`unit_state != "missing"`）或已設定隧道設定，就照常檢查其 active 狀態與 `probe.remote`（除非 `--no-remote`，該旗標已存在 `cli.py:39`）**。
       - **關鍵狀態區分**：
         - 僅當「機器存在隧道 unit 但 `tunnel.env` 完全未設定（無 `RENDEZVOUS` 亦無 `GATEWAY_SOURCE_*`／`FOLLOW_FILE`）」時，隧道未跑才回 `todo`（尚未配置）。
         - **若已啟用動態跟隨（`FOLLOW_FILE` 或 `GATEWAY_SOURCE_*`）**：
           - **`missing`／`invalid`／`regressed` 且隧道未 `active`**，或 **隧道未 `active` 且最近抓取分類為被拒／認證失敗（`regressed`／`invalid`／`hostkey_missing`／`auth_failed`）**：`check` **MUST NOT 降級成 `todo`**，必須回 **`fail`**（exit `1`）並明確列出對應固定原因與處置（如「`follow 檔缺失`」「`follow 檔無效`」「`最近抓取被拒，需人工確認 NODE_GATEWAY；若確認是 MLP 回退，執行 llmgwctl follow-reset 重置；若為認證失敗換好 token 後執行 llmgwctl follow-reset --fetch-state`」）。
           - **隧道仍 `active` 但 follow 檔狀態為 `missing`／`invalid`／`regressed`、最近抓取為 `auth_failed`／`regressed`／`invalid`／`hostkey_missing`，或最近抓取狀態為 `config_partial`**：`check` **MUST NOT 靜默全綠**，該檢查項（`tunnel-follow`／`tunnel-token`）**回報 `todo`**（附固定原因，如「follow 檔異常，現存健康連線暫維持，下次斷線將拒連」「最近抓取被拒（回退／格式無效／缺 host_key），現存連線靠最後已知位址維持，下次斷線將拒連，需人工確認 `NODE_GATEWAY`」「token 可能失效，請跑 `llmgateway-tunnel --fetch-check`，換好 token 後執行 `llmgwctl follow-reset --fetch-state`」或「GitHub 抓取設定半套，僅跑靜態設定」），避免誤以為系統完全健康。
           - **`unreadable`（非 root 呼叫者無權限讀 `STATE_DIR` `0700`）**：`status`（`lgwctl/cli.py:43` 標明不用 root）之 `tunnel-follow`／`tunnel-fetch` 列顯示 **`unreadable（需 root 或 llmgw 才能讀）`**，**絕不得誤報 `missing`**；`check` 在非 root 遇 `unreadable` 且隧道未跑時降級回 **`todo`** 並註明「需 root 或 llmgw 才能讀」。
           - **`status` 顯示 `generation` 與抓取分類**：`tunnel-follow` 列正常時顯示 `ok (gen=<n>)`（`<n>` 來自 `.tunnel_generation` JSON 內已驗證水位 `verified`；尚未完成首次成功探針 200 時顯示 `pending`）；`tunnel-fetch` 列顯示 `.tunnel_fetch_state` 固定分類（`ok`、`regressed`、`invalid`、`hostkey_missing`、`auth_failed`、`rate_limited`、`unreachable`、`config_partial`、`credential_unreadable`、`none`）。
       - **GitHub token 到期與失效告警（`auth_failed` 依隧道狀態分級）**：
         - 到期來源優先讀人填之 `TOKEN_EXPIRES_AT`（建 PAT 時即可填入），其次讀取 `/var/lib/llmgateway/.tunnel_token_meta`（僅含上次抓取 HTTP 狀態、抓取時間戳 `fetched_at`、`github-authentication-token-expiration` 解析出之到期時間，**不含 token 原文**；以 `O_NOFOLLOW` 安全讀取）。
         - 若 `TOKEN_EXPIRES_AT` 設了非空字串卻無法解析為合法 ISO-8601 日期／UTC 時間：`status` 之 `tunnel-token` 列顯示 `expiry_invalid`，`check` 回 **`todo`** 並帶固定原因 **`TOKEN_EXPIRES_AT 格式不合`**（絕不拋未捕獲例外亦不回 `fail`）。
         - 因 GitHub `github-authentication-token-expiration` 標頭屬非官方文件標頭且 fine-grained PAT 曾回傳異常值（如等於現在時間），**若標頭解析出之到期時間 `≤ fetched_at`（不合理）一律視為 `expiry_unknown`，絕不因此報 `fail`**。
         - 若距離 `TOKEN_EXPIRES_AT` 或合理標頭到期日 **≤ 7 天**（含已達提醒日期；可由 `TOKEN_EXPIRY_WARN_DAYS` 調整）：`status` 之 `tunnel-token` 列顯示 `expiring (<n>d left)`（或 `expiry_due`），`check` 回 **`todo`** 告警提早輪替。
         - **最近抓取實際錄得認證失敗（`.tunnel_fetch_state == auth_failed`，含 HTTP `401` 或無 rate-limit 標頭之 `403`）時**：`status` 顯示 `auth_failed`；`check` 在**隧道未 `active` 時回 `fail`**（exit `1`），在**隧道仍 `active` 時回 `todo`**（提示「token 可能失效，請跑 `llmgateway-tunnel --fetch-check`」；換好 token 後可由管理員執行 `llmgwctl follow-reset --fetch-state` 清除狀態，或待下次成功抓取自動清除為 `ok`）。
         - 若未設 `TOKEN_EXPIRES_AT` 且尚未觸發過 GitHub 抓取，或標頭未帶／被判為不合理：`status` 明確顯示 `expiry_unknown（未設 TOKEN_EXPIRES_AT 且尚未取得有效到期標頭）`，`check` 照常通過（不誤報），**並於手冊寫明「平穩無斷線期間若無人工檢查，token 可能悄悄到期並於下次輪換抓取時才 401/403 發現」之限制**。
         - 若配置了 `GATEWAY_GITHUB_KEY_FILE` 且該檔不在（僅限具權限之呼叫者檢查，非 root 遇權限不足同採不誤報策略）或 `.tunnel_fetch_state == credential_unreadable`：`check` **固定回 `todo`**（提示檢查金鑰檔路徑、權限或 drop-in `LoadCredential`）。
       - **輸出隔離（sentinel 驗證）**：`check` 常以 `root` 讀取 `llmgw` 可寫之 `STATE_DIR` 檔案，輸出只准印上述固定白名單狀態字串（仿 `check.py:36-47` `_safe_reason`），**絕不印出 `FOLLOW_FILE` 內容、IP、帳號、`host_key` 或 GitHub token**。
     - `probe.py`：`remote()` 呼叫 `llmgateway-tunnel --probe-once`（嚴格只讀，不寫私用 `known_hosts` 亦不觸發 GitHub 抓取；私用 `known_hosts` 尚未生成或不符合當前有效設定時回 `2`）；**`probe.remote()` 對應 `rc=2` 之訊息改為中性「`無法探`」**。
     - `upgrade.py`：維持只重啟 `llmgateway.service`；`rollback` 僅在隧道存在時傳遞既有 `no_remote` 行為。
     - `lgwctl/migrate.py`：保留 `TUNNEL_SERVICE_NAME`、`OLD_USER_UNIT_NAMES`、`tunnel_config_state()` 等常數與介面，**P1 對 `migrate.py` 採不動核心邏輯**，既有 `test_lgwctl_migrate.py` 原樣通過。
- **被放棄的選項**：
  - 獨立薄入口 `ops-scripts/lgw-service` ＋ 非 systemd pidfile 模式 ＋ 整併 `mac-fallback`：超出需求最小集，不納入本單。

---

## 4. script b 設計：改造現有 `llmgateway-tunnel`（單一確定性方案）

### 4.1 評估 MLP 現有互串工具（唯讀查證結論）

1. **`MLP#864`（`pool-portmap`）不能直接作為底層**：`pool-portmap` 僅騎既有 `pool-tunnel` 的 `ctl-gateway.sock`（`pool-portmap:23, 75, 122`），要求呼叫方是已註冊 `NODE_<NAME>`、且 `gateway_port` 在 `2200–2399`（`portmap.sh:9-10, 25-28`），發布埠限 `10000–11999` 或該節點自己的 `5220–5399` sidecar 埠（`portmap.sh:11-12, 33-36`、`pool-portmap.publish.sh:199-208`）。LLMGateway 主機不應為了掛一個服務埠而被迫註冊為 MLP node 並將整台主機 `localhost:22` 暴露進網格；且現有消費端固定打 `4710`。
2. **`pool-tunnel` 與 `known-hosts.sh` 是可復用的模式**：`pool-tunnel` 讀 `NODE_GATEWAY` 之欄位正規化（`.port // 22`，`pool-tunnel:395`）與 `known-hosts.sh` 的 `[<ip>]:<port>` 單一世代覆寫規則可直接復用。
3. **`ops-scripts/mlp`（`trust-gateway`／`fwd`／`sidecar-link`）**：僅用於 Mac 側 `known_hosts` 或前景 `-L` 轉發，不能建服務端 `-R`。

### 4.2 為什麼選 b'（改造現有 `llmgateway-tunnel`）

1. 現有 `ops-scripts/llmgateway-tunnel`（274 行）已實作並驗證：`ExitOnForwardFailure=yes`、雙 `-R`、`ssh -W 127.0.0.1:<remote_api>` 端對端 HTTP 200 探針（L161-203）、exit `78` 防護（`RestartPreventExitStatus=78`，`deploy/units/llmgateway-tunnel.service:14`）、與不寫值進 log 的安全日誌原則（L16-17）。
2. 沿用同一個 `llmgateway-tunnel.service` unit，切換時只要 `llmgwctl upgrade` ＋設定就緒＋`systemctl restart llmgateway-tunnel`，**徹底消滅「4710 同一時間只能一條隧道綁定」的雙 unit 交接問題**，也不需要新造切換狀態機。
3. 將「連不上才向 GitHub 抓新位址」直接內建在 `llmgateway-tunnel` 生命週期內，達成**零常駐輪詢、零 AI 依賴、純確定性狀態管理**。

### 4.3 已排除／未採用方案摘要（供歷史脈絡對照，不納入實作）

| 選項 | 摘要 | 狀態與原因 |
|---|---|---|
| **G1-B1 / B2 / C**（第二會合點 / Mac 替代 / 直連） | 多養備援機或改全直連＋應用層認證 | **已排除（本人 11-21）**：A 以外選項比 A 更不穩定，不考慮。 |
| **原 Q2-(1a) / (1b) / (2)**（推金鑰庫 / SSH 直推 / 固定 IP＋私鑰） | 由 MLP 主動推檔或硬保固定 IP＋私鑰 | **已排除（本人 11-21）**：本人選定由 LLMGateway 自行維護連線狀態，連不上就向 GitHub 抓新位址。 |
| **常駐 30 秒輪詢方案** | 每 30 秒定時打 GitHub API | **未採用**：本人明確要求「發現連不上才抓」，避免健康期間不必要的 API 請求。 |

### 4.4 確定性連線狀態機、兩組輸入、抓取介面與失敗保底

1. **輸入只有兩組（`/etc/llmgateway/tunnel.env` 設定鍵）**：
   - **輸入①：登入用 private key 路徑與本機登入資訊（沿用既有鍵當初始／最後已知位址基準）**：
     - `IDENTITY`（必填，私鑰路徑，如 `~llmgw/.ssh/id_llmgw_tunnel`）。
     - **本機 SSH 使用者（權威來源）**：沿用初始 `RENDEZVOUS` 的 `user@` 部分或新增 `TUNNEL_USER`（兩者至少須提供其一；若兩者皆設則必須相等，否則 `config_missing` exit `78`；須符合 `re.fullmatch` `[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z`，嚴禁 `-` 開頭或尾端 `\n`）。**GitHub `NODE_GATEWAY.tunnel_user` 僅作一致性比對，不採用遠端字面作別號**。
     - `RENDEZVOUS`（`user@host:port`，選填若已設 `TUNNEL_USER`＋輸入②且曾產生本機 `FOLLOW_FILE`；建議於初始設定保留既有值作為初始「最後已知位址」）。
     - `KNOWN_HOSTS`（初始靜態 `known_hosts` 路徑，如 `~llmgw/.ssh/known_hosts_rendezvous`，僅供尚未有任何本機 follow 檔時的起始基準；觀察期保留供 `rollback`）。
     - 既有埠與探針設定不變：`REMOTE_API=4710`、`REMOTE_SSH=4711`（G3＝B 一律保留，留空維持 `4711`，不支援 `off`）、`LOCAL_API=8480`、`PROBE_INTERVAL=60`、`PROBE_FAILS=3`，並將 `SSH_CONNECT_TIMEOUT`（預設 `10` 秒）、`SERVER_ALIVE_INTERVAL`（預設 `30` 秒）與 `SERVER_ALIVE_COUNT_MAX`（預設 `3` 次）設為可調鍵（便於測試覆蓋與防止黑洞 TCP 卡死）。
   - **輸入②：MLP repo 內 Gateway 位址路徑與讀取用 GitHub key**：
     - `GATEWAY_SOURCE_REPO`（例如 `FATESAIKOU/MyLinuxPool`）。
     - `GATEWAY_SOURCE_PATH`（MLP repo 內位址路徑，現況為 `actions/variables/NODE_GATEWAY`；支援自訂 repo 內 API 路徑）。
     - `GATEWAY_GITHUB_KEY_FILE`（讀取用 GitHub key 檔案路徑，如 `/etc/llmgateway/mlp-gateway.token`；執行期優先讀 `$CREDENTIALS_DIRECTORY/gateway-token`〔由 P4 真機 drop-in `LoadCredential=` 注入〕，否則直接讀 `GATEWAY_GITHUB_KEY_FILE`。**若檔案不在或遇 `PermissionError`，視為 `.tunnel_fetch_state=credential_unreadable`〔記 `fetch_failed` 並走 `exit 1` 退避重試〕，絕不未捕獲例外崩潰、不歸類為 `unreachable`，亦不回 `78`**）。
     - `TOKEN_EXPIRES_AT`（選填，人填之 ISO-8601 日期／UTC 時間，如 `2027-02-01`，供 `check`/`status` 確定性 ≤ 7 天 `todo` 提醒；若非空且格式不合則 `status` 顯示 `expiry_invalid`、`check` 回 `todo`「`TOKEN_EXPIRES_AT 格式不合`」）。
     - `FOLLOW_FILE`（選填，本機快取之最後已知位址狀態檔路徑，預設 `/var/lib/llmgateway/gateway.json`；權限 `0600`、`llmgw:llmgw`）。**`tunnel.env` 沒設 `FOLLOW_FILE` 鍵時，程式仍讀預設路徑**：若該預設檔存在且通過格式驗證，即視同有效 `FOLLOW_FILE` 優先使用（與 §4.4「優先用通過格式驗證之 `FOLLOW_FILE`」一致）；因此 `tunnel-apply` 備份時要一併記錄切換前的 `FOLLOW_FILE`（含預設路徑）與 `.tunnel_known_hosts`，`--revert-inner` 還原時：切換前已存在者還原為切換前內容，本次新建者才改名移開（見 §5.2 步驟 2／6）。
   - **設定半套處理（`config_partial`）**：若靜態基準（或有效 `FOLLOW_FILE`）完整，但 GitHub 三鍵（`GATEWAY_SOURCE_REPO`、`GATEWAY_SOURCE_PATH`、`GATEWAY_GITHUB_KEY_FILE`）只填部分鍵，**不停機、不回 `78`**——照靜態設定運行、記固定事件 `config_partial` 並寫入 `.tunnel_fetch_state` 供 `status` 顯示與 `check` 報 `todo`。
   - **可選微調參數（具安全預設）**：
     - `FETCH_MIN_INTERVAL`（預設 `60` 秒）、`FETCH_BACKOFF_MAX`（預設 `900` 秒即 15 分鐘）、`FETCH_TIMEOUT`（預設 `10` 秒）、`TOKEN_EXPIRY_WARN_DAYS`（預設 `7` 天）。

2. **抓取介面、安全限制與 `NODE_GATEWAY` 嚴格格式驗證**：
   - **HTTP 介面與安全限制（純標準函式庫，不新增套件）**：
     - 使用 `urllib.request` 直連 `https://api.github.com/repos/<GATEWAY_SOURCE_REPO>/<GATEWAY_SOURCE_PATH>`（附 `Authorization: Bearer <token>`、`Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-11-28`、`User-Agent: llmgateway-tunnel`）。
     - **端點白名單**：正式模式**只接受 `https://api.github.com`**；`LGW_GATEWAY_API_BASE` **僅在同時設明確測試旗標 `LGW_ALLOW_INSECURE_API_BASE=1` 且主機為 loopback（`127.0.0.1`、`::1`、`localhost`）時生效**，否則**拒絕啟動（`exit 78`）**。
     - **請求與轉址限制**：逾時 `FETCH_TIMEOUT`（預設 `10` 秒）；回應體積上限 **`65536` bytes（64 KiB）**（讀取 `65537` bytes 超限即拒收）；自訂 `HTTPRedirectHandler` **禁止任何跨主機／跨協定轉址**（防止 `Authorization: Bearer` 外洩給第三方主機）。
     - 選用 `urllib` 而非 `gh api` CLI 的原因是能直接取得完整 HTTP 狀態碼與回應標頭（且 token 不經 argv／環境變數；手動除錯等價指令 `gh api repos/FATESAIKOU/MyLinuxPool/actions/variables/NODE_GATEWAY --jq .value`）。
   - **回應解包與嚴格格式驗證（防 SSH 選項／`known_hosts` 注入；所有 regex 一律使用 `re.fullmatch`＋`\Z`，不用 `$`）**：
     - 外層 Actions Variable API 回應 `{"name": "NODE_GATEWAY", "value": "<JSON字串>", ...}` → 解析 `.value` JSON。
     - 內層 `NODE_GATEWAY` 逐欄嚴格驗證（**任何一欄不合或拋 `ValueError` 即拒收記 `fetch_invalid`／`hostkey_missing`**）：
       - `ip`：必須 `isinstance(v, str)`（JSON 整數如 `16909060` 拒收）。若為字面 IP 須可由 `ipaddress.ip_address(v)` 解析且 **`getattr(addr, "scope_id", None) is None`**（**不接受任何 IPv6 zone id／`%`、空白或控制字元**），並**一律將正規化後的 `str(addr)` 用於寫入 `FOLLOW_FILE`、`.tunnel_known_hosts` 與 `ssh` 參數，不用原字串**；否則須以 `re.fullmatch` 吻合嚴格 RFC 1123 主機名 regex `(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z`（**使用 `\Z`、不用 `$`**，嚴禁尾端 `\n`、`-` 開頭、空白、控制字元及 `,%*?!|@` 等特殊字元），**且最後一個 label 不得全為數字**（`socket.inet_aton(v)` 成功也拒；否則 `16909060`、`0x7f.1`、`0177.0.0.1`、`1.2.3`、`01.2.3.4` 雖吻合 regex，卻會被 `ssh` 另行解讀為其他位址）。
       - `port`：**只認 `port`（`NODE_GATEWAY` 不讀 `ssh_port`；`ssh_port` 僅為本機 `FOLLOW_FILE` 內部欄位名）**。`port` 缺漏或為 `null` 時預設 `22`；若存在且非 `null`，**必須滿足 `isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535`**（明確排除 JSON `true`/`false`/字串/浮點數）。
       - `tunnel_user`：必填字串（`isinstance(v, str)`），須以 `re.fullmatch` 吻合 `[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z`（嚴禁 `-` 開頭、空白或尾端 `\n`），**且必須等於本機設定的 SSH 使用者**（來自 `TUNNEL_USER` 或 `RENDEZVOUS` 的 `user@`）；不符即拒收記 `fetch_invalid`（log 不輸出帳號字串）。
       - `host_key`：必填字串（`isinstance(v, str)`），**必須為單行**（無 `\r`／`\n`）、以 `re.fullmatch` 吻合 `(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521) ([A-Za-z0-9+/]+={0,2})\Z`（**嚴格兩欄，禁止第三欄註解、萬用字元或尾端 `\n`**），且 `raw = base64.b64decode(b64, validate=True)` 須滿足正規化往返 **`base64.b64encode(raw).decode("ascii") == b64`**（拒收多餘 `=` 等非標準編碼），並**完整解析 OpenSSH wire-format**（前 4-byte 長度前綴之金鑰型別字串須與宣告型別相符，且後續各型別對應之長度前綴欄位須剛好吃完整段 `raw` 位元組、無殘留尾端位元組；**必須為本代新機公鑰，fail-closed**）。
       - `generation`：接受非負整數（`isinstance(v, int) and not isinstance(v, bool) and v >= 0`，**排除 `bool`**）或純 ASCII 十進位數字字串（`isinstance(v, str)` 且以 `re.fullmatch` 吻合 `[0-9]{1,18}\Z`，上限 18 位避免超長數字字串觸發 `int()` 例外），一律轉 `int` 比較；其餘或轉換擲出 `ValueError` 視為 `fetch_invalid`。
       - `published_at` / `rotated_at`：時間戳字串（紀錄於本機 `FOLLOW_FILE`）。
       - **嚴格忽略其他欄位**：`NODE_GATEWAY` 內的 `user`、`key_secret`、`ssh_listen_ports`、`ports` 等非位址欄位一概忽略。
   - **SSH 命令列參數隔離與連線逾時**：
     - `_ssh_common(cfg)` 一律包含 `-o ConnectTimeout=<SSH_CONNECT_TIMEOUT>`（預設 `10` 秒）、`-o ServerAliveInterval=<SERVER_ALIVE_INTERVAL>`、`-o ServerAliveCountMax=<SERVER_ALIVE_COUNT_MAX>`，並以 **`-l <local_user> -- <normalized_host>`** 分開傳遞使用者與正規化目的主機（不再將 `user@host` 放於無 `--` 保護的尾端），確保絕無資料被 `ssh` 解析為選項，且驗證的 `ip`／主機名恆等於 `ssh` 實際連線之主機與 `.tunnel_known_hosts` 寫入值。
   - **本機 `FOLLOW_FILE`（`/var/lib/llmgateway/gateway.json`）形狀**：正規化儲存 `{"ip", "ssh_port", "tunnel_user", "host_key", "generation", "published_at"}`（`ip` 為正規化後字串、`ssh_port` 為本機內部欄位名；`0600`、`llmgw` 擁有）。

3. **確定性連線狀態機（健康時不輪詢；連不上才同行程抓並重連）**：
   - **啟動與重啟後行為**：
     1. 讀設定與本機狀態目錄（`/var/lib/llmgateway/.tunnel_generation`、`.tunnel_known_hosts`、`.tunnel_fetch_backoff`、`.tunnel_fetch_state`、`FOLLOW_FILE`）。
     2. **若有有效本機位址可用（優先用通過格式驗證之 `FOLLOW_FILE`，否則用初始 `RENDEZVOUS`＋`KNOWN_HOSTS`）**：**直接啟動 `ssh -R` 連線，不先打 GitHub API**！
     3. **若完全沒有本機可用位址（例如首次部署未設 `RENDEZVOUS`，但已設 `TUNNEL_USER`＋GitHub 輸入②）**：視為「尚無連線能力」，於**同一行程內**依退避保護向 GitHub 抓一次並寫入本機狀態後直接建連 `ssh`。
   - **運行健康期間與首次探針時點**：
     - 新 `ssh` 子行程啟動後，**隨即執行首次端對端探針**（短暫就緒緩衝如 `1–2s` 後即打一次 `ssh -W`，失敗則依 `PROBE_INTERVAL` 繼續），以便新代連線盡快完成驗通、原子寫入 `.tunnel_generation` 並將退避 `consecutive_failures` 歸零。
     - Watch 執行緒之後每隔 `PROBE_INTERVAL`（預設 `60` 秒）只經現有隧道跑 `ssh -W 127.0.0.1:<remote_api>` 端對端 HTTP 200 探針；**只要探針與 `ssh` 子行程皆健康，絕對不呼叫 GitHub API（零輪詢）**。
     - 若人為或外部工具在運行中修改或弄壞 `FOLLOW_FILE`：僅於狀態轉換記一次事件，不打斷仍健康之 `ssh`（且 `llmgwctl check` 至少回 `todo` 警示）。
   - **「發現連不上」的觸發條件與同行程恢復流程**：
     - 條件 A：`ssh` 子行程非預期退出（包含 `StrictHostKeyChecking=yes` 拒連、`ConnectTimeout`／`ServerAlive` 逾時、遠端埠被佔等，即退出非 0 或未收到 SIGTERM/SIGINT 即終止）。
     - 條件 B：`ssh -W` 探針連續失敗達 `PROBE_FAILS`（預設 3 次），守護程式主動結束 `ssh` 子行程。
     - 觸發後流程（**全在同一 `llmgateway-tunnel` 行程內進行，避免無謂 `exit 1` 重連舊死機**）：
       1. 檢查持久化退避檔 `/var/lib/llmgateway/.tunnel_fetch_backoff`（含 `next_allowed_epoch`、`consecutive_failures`）：若尚在冷卻／退避／rate-limit 重置窗口內，**跳過 GitHub API 呼叫**，直接記事件後以 `exit 1` 交由 systemd `RestartSec=10` 帶起下一輪用最後已知位址重試。
       2. 若允許抓取：在**同一個行程內**呼叫 GitHub API 抓 `NODE_GATEWAY`（並依標頭與 `fetched_at` 寫入 `.tunnel_token_meta`；若標頭時間 `≤ fetched_at` 則記 `expiry_unknown`）。
       3. **驗證、雙水位（`applied` vs `verified`）`generation` 單調檢查與退避語意**：
          - **雙水位定義**：
            - **`applied`**：本機 `FOLLOW_FILE` 內容（含 `generation` 與正規化連線欄位 `(ip, ssh_port, tunnel_user, host_key)`），在抓到合法且獲接受之設定後、重建 `ssh` 前即原子寫入。
            - **`verified`**：`/var/lib/llmgateway/.tunnel_generation`（**改存 JSON：`{"generation": <int>, "fingerprint": "<sha256>"}`**，其中 `fingerprint` 為正規化連線欄位 `(ip, ssh_port, tunnel_user, host_key)` 之 SHA-256 指紋；`0600`、`llmgw` 擁有），**只在該連線實際建連 `-R` 並通過首次端對端 `ssh -W` HTTP 200 探針後才原子寫入**。缺失或內容損毀視為 `verified = None`。
          - **比對規則（嚴格依序判定）**：
            1. 若 `verified is not None` 且 **`new_gen < verified.gen`**：回退異常！**拒絕套用**，記 **`follow_regressed`**、寫入 `.tunnel_fetch_state=regressed`、累加 `consecutive_failures` 並更新退避（不降代、不覆寫最後已知位址），以 `exit 1` 重試。
            2. 若 `applied is not None` 且 **`new_gen == applied.gen` 且欄位完全等於 `applied`**：**未換代**（含正常網路閃斷，以及「已套用 `N+1` 但尚未通過首次探針 200 又抓到同一份 `N+1`」的過渡態）——寫入 `.tunnel_fetch_state=ok`、設定 `next_allowed_epoch = now + FETCH_MIN_INTERVAL`，**不累加 `consecutive_failures`、不覆寫 `FOLLOW_FILE` 與 `.tunnel_known_hosts`、不重建為新代**（若 `verified is None` 或 `applied.gen != verified.gen`，另於 log 記固定事件 `unverified_retry` 提示該代尚未驗通），**以 `exit 1` 交由 systemd `RestartSec=10` 使用既有最後已知位址重試**。
            3. 若 `verified is not None` 且 **`new_gen == verified.gen` 且連線欄位指紋等於 `verified.fingerprint`**：**接受為「回到已驗證的世代」**（即使 `applied` 較新但沒驗通＝MLP 輪換回退至已驗證代之合法情形，無需人工介入！）——原子寫回 `FOLLOW_FILE` 與 `.tunnel_known_hosts`（依 `[<ip>]:<ssh_port> <host_key>` 或 `22` 埠 `<ip> <host_key>` 使用正規化 `ip` 單筆覆寫）、寫入 `.tunnel_fetch_state=ok` 與 `next_allowed_epoch = now + FETCH_MIN_INTERVAL`，記 `gateway_reverted_to_verified`，並**在同一個行程內立即以該已驗證位址重建 `ssh` 子行程**。
            4. 若（`verified is not None` 且 **`new_gen == verified.gen` 但指紋不同於 `verified.fingerprint`**）或（`applied is not None` 且 **`new_gen == applied.gen` 但欄位不同於 `applied`**）：同代篡改！**拒絕套用**，記 `follow_invalid`、寫入 `.tunnel_fetch_state=invalid`、累加 `consecutive_failures` 並更新退避（`exit 1`）。
            5. **`new_gen > verified.gen`（或 `verified is None`）**：合法新代（套用）！**原子更新 `FOLLOW_FILE` 與私用 `/var/lib/llmgateway/.tunnel_known_hosts`**（一律以正規化 `ip` 組行，清除舊條目防止雲端 IP 回收撞鍵），寫入 `.tunnel_fetch_state=ok` 與最短冷卻 `next_allowed_epoch = now + FETCH_MIN_INTERVAL`，並**在同一個行程內立即以新位址重建 `ssh` 子行程**（記 `gateway_updated`）。
            - 缺 `host_key` 或格式／帳號／wire-format／base64 驗證無效：**fail-closed 拒收**，記 `hostkey_missing`／`fetch_invalid`、寫入 `.tunnel_fetch_state`（`hostkey_missing` 或 `invalid`）、累加 `consecutive_failures` 並更新退避，**絕不退回 `accept-new`**。
       4. **失敗保底（保留最後已知位址持續重試）**：無論 GitHub API 斷網／逾時（`.tunnel_fetch_state=unreachable`）、金鑰檔 `PermissionError`/`FileNotFoundError`（記 `fetch_failed`、`.tunnel_fetch_state=credential_unreadable`）、`401` 或**無 `X-RateLimit-Remaining: 0` 與 `Retry-After` 標頭之 `403`**（記 `fetch_auth_failed`、`.tunnel_fetch_state=auth_failed`）、`429` 或**帶 `X-RateLimit-Remaining: 0`／`Retry-After` 標頭之 `403`**（記 `fetch_rate_limited`、`.tunnel_fetch_state=rate_limited` 並依標頭設退避）、`404`/5xx/回應超限或無效，守護程式**MUST NOT 清除或覆寫既有的最後已知有效位址**（`FOLLOW_FILE`＋`.tunnel_known_hosts`；或初始 `RENDEZVOUS`＋`KNOWN_HOSTS`），累加 `consecutive_failures`（`60s → 120s → 240s → 480s → 上限 900s`）並以 **`exit 1`** 交由 systemd 重試（待恢復後自癒）。

4. **`.tunnel_generation` 寫入與退避歸零時點（確保 `status` 忠實反映「最近一次成功套用並驗通之世代」）**：
   - `/var/lib/llmgateway/.tunnel_generation`（`0600`、`llmgw` 擁有；JSON `{"generation": <int>, "fingerprint": "<sha256>"}`）**只在新的連線實際建立 `-R` 並通過首次端對端 `ssh -W` HTTP 200 探針後才原子寫入**（在 `ssh` 剛啟動尚未驗通前不提前寫入），**並於此時將 `.tunnel_fetch_backoff` 的 `consecutive_failures` 歸零**。
   - 若新代位址尚未通過首次探針即失敗斷線，`verified`（`.tunnel_generation`）維持上一成功代數與指紋（或 `None`），`llmgwctl status` 不會誤報尚未連通的新代為 `ok (gen=<新>)`，且若上游輪換回退至已驗證代時仍可合法接受。

5. **私用 `known_hosts`、`--probe-once`／`--fetch-check` 只讀與退出碼分離（`78` vs `1`）**：
   - **私用 `known_hosts`**：`/var/lib/llmgateway/.tunnel_known_hosts`（`0600`、`llmgw` 擁有），配合 `-o UserKnownHostsFile=/var/lib/llmgateway/.tunnel_known_hosts`、`-o GlobalKnownHostsFile=/dev/null`、`-o StrictHostKeyChecking=yes`。僅使用靜態初始 `RENDEZVOUS`＋`KNOWN_HOSTS` 且尚未由 GitHub 寫入過 `FOLLOW_FILE` 時用設定之 `KNOWN_HOSTS`；一旦存在有效 `FOLLOW_FILE`，固定使用 `.tunnel_known_hosts`。
   - **`--probe-once` 只讀不寫**：嚴格禁止建立或覆寫任何狀態檔亦禁止打 GitHub；私用 `known_hosts` 缺失或與 `FOLLOW_FILE` 不符時直接回 `2`（無法探）。
   - **`--fetch-check` 人工確定性驗證（PM 預設）**：人工執行（如 P4.2 放好 token 後立即驗收）——讀取同一份 `tunnel.env` 與 credential，發送**單次** GitHub API 請求，只驗證 HTTP 200、`NODE_GATEWAY` 必填欄位與嚴格格式（含 `tunnel_user` 一致性）及是否帶到期標頭，輸出無祕密摘要（例如 HTTP 狀態、format ok/invalid、expiry header present/absent），**成功時並印出提示「`如 check 仍紅請執行 llmgwctl follow-reset --fetch-state`」**，**嚴格不寫入任何本機狀態檔**（不更動 `FOLLOW_FILE`、`.tunnel_known_hosts`、`.tunnel_generation`、`.tunnel_fetch_backoff`、`.tunnel_fetch_state`、`.tunnel_token_meta`）。
   - **退出碼 `78` vs `1`**：
     - **exit `78`（`RestartPreventExitStatus=78`）**：**只保留給設定本身完全無法啟動**——缺 `IDENTITY`、缺本機登入使用者（或 `TUNNEL_USER` 與 `RENDEZVOUS` 使用者衝突）、或「靜態基準」與「GitHub 抓取（完整三鍵）」**兩者皆未設定且無有效 `FOLLOW_FILE`**、或固定埠／探針參數語法非法（含 `REMOTE_SSH=off` 及未設測試旗標或非 loopback 卻注入 `LGW_GATEWAY_API_BASE`）。記 `config_missing`。
     - **exit `1`（由 systemd `RestartSec=10` 自動重試）**：所有執行期連線失敗、探針連敗、GitHub 抓取失敗/權限不足/限流/驗證失敗、`FOLLOW_FILE` 損毀或 `generation` 回退等，一律寫對應事件與 `.tunnel_fetch_state` 並回 **`exit 1`**（`RestartSec=10` 搭配持久化退避保護，不造成 API 風暴）。
   - **恢復時間目標（RTO）**：輪換時若舊機變黑洞，由 `ServerAlive`（`30s × 3 ≈ 90s`）或探針連敗（`60s × 3 = 180s`）先觸發者決定斷線偵測，加同行程抓取（`≤ 10s`）與 `-o ConnectTimeout=10` 重連，整體恢復時間誠實範圍約 **2–3.5 分鐘**（大幅低於先前人工處理的 40 分鐘）；若為重啟連死位址路徑，因有 `ConnectTimeout=10`，約 `≤ 30s` 即抓取切換。
   - **日誌安全**：只寫固定事件種類（`tunnel_up`、`gateway_updated`、`gateway_reverted_to_verified`、`unverified_retry`、`fetch_ok`、`fetch_failed`、`fetch_auth_failed`、`fetch_rate_limited`、`fetch_invalid`、`follow_missing`、`follow_invalid`、`follow_regressed`、`hostkey_missing`、`probe_fail`、`restart`、`config_missing`、`config_partial`、`spawn_fail`）與 `generation` 數字，絕不輸出 IP、帳號、金鑰、token 原文或 `ssh` 原始錯誤文字。

### 4.5 與既有規矩的衝突、Token 最小權限配置與信任風險緩解

1. **衝突一：`docs/deploy.md:36`「不放 token 檔」之修訂方案**
   - **既有規矩**：`docs/deploy.md:36` 規定 `gh` 登入由本人以 `llmgw` 身分瀏覽器登入，**不放 token 檔**（該條原為保護 `LLMGateway-keys` 金鑰正本同步）。
   - **本人 11-21 決定與文件修改**：本人明確同意為了確定性讀取 MLP Gateway 位址而新增一把唯讀 GitHub key 檔。設計於 P3 修訂 `docs/deploy.md`：保留 `LLMGateway-keys` 同步維持 `gh auth login`、不放 token 檔之原則，並**明確增列「`NODE_GATEWAY` 位址讀取用唯讀 token 檔」之受控例外與規格**：
     - **最小授權（已實查 GitHub 官方文件與 MLP 確認）**：因 `NODE_GATEWAY` 存放於 `FATESAIKOU/MyLinuxPool` 的 GitHub Actions repository variable（`GET /repos/FATESAIKOU/MyLinuxPool/actions/variables/NODE_GATEWAY`），必須使用 **Fine-grained Personal Access Token**：
       - **Resource owner / Repository access**：`Only select repositories` → 僅勾選 **`FATESAIKOU/MyLinuxPool`**（嚴禁選取 `LLMGateway-keys` 或其他任何 repo）。
       - **Repository permissions**：僅授予 **`Variables: Read-only`**（加上 GitHub 預設唯讀 `Metadata`）；不授予 `Contents`、`Actions`、`Secrets` 或任何 `Write` 權限。
       - **權限實況聲明**：這是 GitHub fine-grained PAT **能給的最小授權**，但該授權**仍可讀取該 repo 全部 Actions variables**（含其他 `NODE_*` 等位址拓樸，不含 Secrets），無法單獨限制至僅 `NODE_GATEWAY` 一個變數；此拓樸可見性列為本人接受之風險。
     - **檔案權限與 drop-in `LoadCredential` 讀取方式（出貨 unit 不寫死 `LoadCredential=`）**：
       - 實體檔案路徑：`/etc/llmgateway/mlp-gateway.token`（單行純 token 文字去掉前後空白），權限 **`0600`**、擁有者 **`root:root`**。
       - **出貨 unit（`deploy/units/llmgateway-tunnel.service`）不含 `LoadCredential=`**：若將 `LoadCredential=` 寫進出貨 unit，所有沒有 token 檔的機器（現役尚未切換真機、全新安裝、純靜態部署）重啟隧道時都會因 systemd `243/CREDENTIALS` 直接啟動失敗且 `RestartPreventExitStatus=78` 擋不到。因此出貨 unit **不寫** `LoadCredential=`，改由 P4.3 的 `tunnel-apply` 套用暫存 drop-in（`credential.conf.new`）（`/etc/systemd/system/llmgateway-tunnel.service.d/credential.conf` 寫入 `LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token`）；程式則優先讀 `$CREDENTIALS_DIRECTORY/gateway-token`，否則依 `GATEWAY_GITHUB_KEY_FILE` 讀取（無權限或讀不到當成 `.tunnel_fetch_state=credential_unreadable`〔記 `fetch_failed`〕走 `exit 1`）。
       - **unit 加固（對齊 `llmgateway.service:26-29`）**：出貨之 `deploy/units/llmgateway-tunnel.service` 補齊 `NoNewPrivileges=true`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/llmgateway`、`PrivateTmp=true`（P2 實測確認 `ssh` 子行程仍可唯讀存取 `IDENTITY`／`KNOWN_HOSTS`）。
     - **到期告警與平穩期悄然到期限制**：
       - 優先依人填 `TOKEN_EXPIRES_AT`（≤ 7 天 `check` 回 `todo`；格式不合回 `todo`「`TOKEN_EXPIRES_AT 格式不合`」），次依回應標頭 `github-authentication-token-expiration`（存入 `.tunnel_token_meta`；若日期 `≤ fetched_at` 視為 `expiry_unknown`，標頭只觸發 `todo` 不觸發 `fail`）；**只有認證失敗（`.tunnel_fetch_state == auth_failed`，即 `401` 或無 rate-limit 標頭之 `403`）且隧道未 `active` 時才回 `fail`**（隧道仍 `active` 時回 `todo`，換好 token 後執行 `llmgwctl follow-reset --fetch-state` 或待下次成功抓取清除）。
       - **明確限制（告知本人核准）**：因連線健康時不輪詢 GitHub，平穩無斷線的幾個月內若未使用人工檢查，**token 可能悄悄到期，直到下一次輪換斷線需要它的那一刻才收到 `401`/`403` 被動發現**。實作提供人工觸發之 `--fetch-check` 與手填 `TOKEN_EXPIRES_AT` 降低此風險。
     - **零外洩保證**：token 僅於記憶體中填入 HTTP `Authorization` header，嚴禁進入任何 log、`status`、`check`、`.tunnel_token_meta`、`.tunnel_fetch_state`、例外訊息或子行程環境。

2. **衝突二：主 spec `connectivity`「MUST NOT 依賴 `NODE_*`」之放寬**
   - **既有規矩**：`openspec/specs/connectivity/spec.md:4` `## Purpose` 與 `:42`「服務端與客戶端都 MUST NOT 依賴 MyLinuxPool 的 pool-tunnel 或 NODE_* 設定」。
   - **修訂方案**：於本 delta spec 明文將該條放寬為：**「服務端與客戶端都 MUST NOT 依賴 MyLinuxPool 的 `pool-tunnel` 與其他 `NODE_*` 設定；唯一允許例外為服務端隧道守護在連線失敗時，唯讀讀取 `NODE_GATEWAY` 之位址相關欄位（`ip`、`port`、`host_key`、`generation`）與比對 `tunnel_user` 作為動態會合點定位來源」**，並於歸檔（`tasks.md` 3.3）同步更新主 spec `## Purpose` 反映此唯一例外。

3. **信任方向、拓樸可見性與格式注入風險（本人已接受，設計明載與緩解）**
   - **風險陳述**：
     1. **無認證 API 重導**：`lgwd` 無應用層認證，且 `NODE_GATEWAY` 同時提供隧道目的地位址（`ip`／`port`）與 `host_key`（同源），誰能寫入 MLP `NODE_GATEWAY` 就能將 `ssh -R 4710/4711` 指到其指定機器接觸無 auth `lgwd` API。
     2. **主機指令執行風險（若無格式驗證）**：**若不做嚴格格式驗證，`NODE_GATEWAY` 中的 `tunnel_user`／`ip`／`host_key` 可透過 `ssh` 選項（如 `tunnel_user` 以 `-oProxyCommand=…` 開頭）或 `known_hosts` 多行／多欄注入，直接升級為服務主機 `llmgw` 帳號之任意指令執行**。
     3. **Repo variables 拓樸可見性**：`Variables: Read-only` 涵蓋 `FATESAIKOU/MyLinuxPool` 全部 variables（位址拓樸，不含 Secrets）。
   - **緩解措施**：
     1. **本機帳號權威＋嚴格格式白名單＋參數隔離**：SSH 使用者以本機 `TUNNEL_USER`／`RENDEZVOUS` 為準（`NODE_GATEWAY.tunnel_user` 只比對不覆蓋）；嚴格驗證 `isinstance(str)` 之無 zone id 正規化字面 IP／`\Z` 結尾 `re.fullmatch` 嚴格主機名、非布林整數 `port`、單行兩欄完整 wire-format 與 base64 往返合法之 `host_key` 與雙水位 `generation`；`ssh` argv 一律使用 `-l <user> -- <normalized_host>` 隔離（`known_hosts` 與 `ssh` 主機同用正規化字串），徹底消除選項／`known_hosts` 注入風險；
     2. **嚴格 fail-closed**：`host_key` 必填且通過 `StrictHostKeyChecking=yes`，不退回 `accept-new`；
     3. **單調 `generation` 防回退／同代篡改**：阻止重放舊代資料；
     4. **變更與拒絕可稽核**：於 journald 記 `gateway_updated`／`follow_regressed` 等事件及 `.tunnel_fetch_state`，`llmgwctl status` 顯示當前 `generation` 與抓取分類；
     5. **最小欄位依賴**：忽略 `NODE_GATEWAY` 內非位址欄位，token 限單一 repo 唯讀 `Variables`。

---

## 5. G3＝B（保留 `4711`）、無空窗切換與真實輪換觀察 SOP

### 5.1 G3＝B 處置：一律保留 `4711`

- 依本人 11-21 決定（G3＝B），不另開不經跳板機的進場方式；**`REMOTE_SSH` 維持現況預設 `4711`（留空亦為 `4711`），移除所有關閉 `4711` 與 `REMOTE_SSH=off` 的規劃**。
- 管理 SSH `4711` 與 API `4710` 仍在同一條 `llmgateway-tunnel` 連線內一起建通；輪換後新隧道連上新 `<會合點>` 時，`4711` 即同步於新會合點恢復。

### 5.2 切換流程、舊版回滾相容與必用反向保險

1. **常規切換流程（沿用同一個 `llmgateway-tunnel.service`）**：
   - 放好 `/etc/llmgateway/mlp-gateway.token`（`root:root` `0600`）；**新設定與 drop-in 只做成暫存檔**（`/etc/llmgateway/tunnel.env.new`、`/etc/llmgateway/credential.conf.new`），**不動目前運行中的設定**，改由下方 `tunnel-apply` 在**備份目前可用設定之後**才套用（備份必須早於任何覆寫，否則保險還原的會是已改壞的檔）；systemd drop-in 目標為 `/etc/systemd/system/llmgateway-tunnel.service.d/credential.conf`（`LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token`）；
   - `llmgwctl upgrade --tag <新版>`（`upgrade.py` 只重啟 `llmgateway.service`，不碰正在跑的隧道）；
   - **複製** `/etc/llmgateway/tunnel.env` 為暫存 `/etc/llmgateway/tunnel.env.new`，並在**暫存檔**加入 `GATEWAY_SOURCE_REPO`、`GATEWAY_SOURCE_PATH`、`GATEWAY_GITHUB_KEY_FILE`、`TOKEN_EXPIRES_AT`、`FOLLOW_FILE`（及確認 `TUNNEL_USER`／`RENDEZVOUS` 帳號），**暫存檔內保留原靜態 `RENDEZVOUS`／`KNOWN_HOSTS`（與 `known_hosts_rendezvous` 檔）與 `REMOTE_SSH=4711`，供 `llmgwctl rollback` 回退舊版腳本相容**；**目前運行中的 `tunnel.env` 與 drop-in 此步完全不動**；4.2～4.3 之間勿改運行中的 `tunnel.env`（`tunnel-apply` 會以暫存檔取代之，並先備份）；
   - 以 `llmgw` 身分＋`LoadCredential`、**指向暫存設定檔**執行 `--fetch-check` 一次性驗證 token 可讀 `NODE_GATEWAY` 且格式／標頭正確（`-E LGW_TUNNEL_ENV` 讓 transient unit 讀暫存檔，否則找不到設定而 exit 78）：
     - `systemd-run --wait --pipe -p User=llmgw -p LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token -E LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env.new /opt/llmgateway/current/ops-scripts/llmgateway-tunnel --fetch-check`
   - **必用反向保險**下執行重啟切換（切換瞬間舊 `4711` SSH 會中斷，待新隧道連上後重新經 `<會合點>:4711` 登入驗收並確認）。
2. **必用無人值守反向保險（P3 新增 `llmgwctl tunnel-apply`，非遷移用 `lgw-cutover`／`lgw-revert`）**：
   - 因 G3＝B 下唯一進場只有 `4711`，重啟隧道若寫錯設定即會失聯，故於 **P3 新增獨立小保險子命令 `llmgwctl tunnel-apply`**（含 `--confirm` 與內部 `--revert-inner`；沿用 `lgwctl/migrate.py` 之 `flock` 互斥鎖與 `systemd-run` transient timer 寫法，但**不是**舊 user→system 遷移用的 `lgw-cutover`／`lgw-revert` 子命令），列為 **P4.3 必用**（C-10 為 P3 完成與 P4.3 執行前置條件）：
     1. **互斥與執行位置**：以 `flock`（`/var/lib/llmgateway-ctl/.tunnel-apply.lock`）互斥。下列步驟 2～5 的**真正做事部分**（備份後之排程、套用、重啟、探針、還原）跑在 `systemd-run` 起的獨立 transient unit 內（以 `INVOCATION_ID` 判斷，沿用 `migrate.py` 先例）；前景行程即使被 SIGHUP／SIGTERM 殺掉（例如本人走的 `4711` 連線被重啟斷開），流程仍由該 transient unit 完成。
     2. **先備份**（`0600`、`root`，存於 `/var/lib/llmgateway-ctl/tunnel-apply.bak/`）目前可用的 `/etc/llmgateway/tunnel.env`、`/etc/systemd/system/llmgateway-tunnel.service.d/credential.conf`，以及**切換前已存在的** `FOLLOW_FILE`（含預設路徑）與私用 `.tunnel_known_hosts`（逐項記錄**路徑與存在性**；新舊 `tunnel.env` 的 `FOLLOW_FILE` 路徑不同時兩個路徑都記，原不存在則記為「不存在」）。備份發生在任何覆寫之前。
     3. **再排保險**：排定 `systemd-run --on-active=<revert-after> --unit=lgw-tunnel-revert ... llmgwctl tunnel-apply --revert-inner`；旗標 **`--revert-after <時長>`**，預設 `10m`，下限 `5m`（低於下限拒收）。**排程失敗（`systemd-run` 非零）即中止、不套用任何新檔**，與 `migrate.py` 先排保險再動的先例一致。
     4. **再套用**：以 `--new-env`／`--new-dropin` 指定的暫存檔**複製**（非 rename）原子寫入目標路徑，保留 `.new` 供失敗後重試；不得在 4.2～4.3 之間改動運行中的 `tunnel.env`（本步會以暫存檔取代之，且已先行備份）。
     5. **重啟與探針**：執行 `systemctl daemon-reload && systemctl restart llmgateway-tunnel.service`，並以 **等待上限預設 150–180 秒（下限 ≥ 90 秒）** 執行遠端探針（依據 MLP `pool-tunnel:525-530` 實測，死埠釋放需 60–120 秒）。探針回 **`rc=2`（無法探）視為失敗**；探針失敗或超時即自動還原（見下）。
     6. **還原（`--revert-inner`）**：還原備份之 `tunnel.env`；drop-in 若備份時原不存在則**只刪本次放的 `credential.conf`**，目錄為本次新建且已空才 `rmdir`；並把 `FOLLOW_FILE`（含預設路徑 `/var/lib/llmgateway/gateway.json`）與 `.tunnel_known_hosts` **恢復為切換前內容**：切換前已存在者以備份還原；切換前不存在、為本次新建者才**改名移開**（`*.reverted-<時間戳>`，不刪除；理由見 §4.4 `FOLLOW_FILE` 預設路徑規則）；新舊自訂路徑不同時兩個路徑都處理；再 `daemon-reload` 並重啟隧道。
     7. **確認**：探針通過且管理員重新經新 `<會合點>:4711` 登入確認後，執行 **`llmgwctl tunnel-apply --confirm`** 取消 `lgw-tunnel-revert.timer`。若 timer 已觸發還原，之後跑 `--confirm` 須拒絕並說明「已還原」。
     8. **必須明確警示的限制**：**`lgw-tunnel-revert.timer` 為 transient unit，重開機後即失效，切換窗口內切勿重開機**。
     9. **測試契約**：本流程之測試（前景行程被殺仍完成、排程失敗不套用、還原範圍與內容、測試加速旗標）見 tasks C-10(a)–(d)。

### 5.3 本人在 `<服務主機>` 要貼的步驟＋第一次真實輪換觀察與退回 SOP

1. **切換步驟輪廓（佔位符）**：
   - **(a) 準備 Token 與暫存 drop-in**：於 GitHub 建立僅授權 `FATESAIKOU/MyLinuxPool` `Variables: Read-only` 的 fine-grained PAT（記下到期日），寫入 `/etc/llmgateway/mlp-gateway.token`（`root:root` `0600`），並把 drop-in 內容暫存為 `/etc/llmgateway/credential.conf.new`（**不直接裝進 `/etc/systemd/system/`**，由 (c) 的 `tunnel-apply` 在備份後套用）。
   - **(b) 升級、暫存設定與先驗 token**：執行 `llmgwctl upgrade --tag <新版>` → 複製 `tunnel.env` 為 `tunnel.env.new` 並在暫存檔加入輸入②、`TOKEN_EXPIRES_AT` 與 `FOLLOW_FILE=/var/lib/llmgateway/gateway.json`（**保留既有 `RENDEZVOUS`、`KNOWN_HOSTS` 與 `REMOTE_SSH=4711`**；運行中設定不動）→ 執行 `systemd-run --wait --pipe -p User=llmgw -p LoadCredential=gateway-token:/etc/llmgateway/mlp-gateway.token -E LGW_TUNNEL_ENV=/etc/llmgateway/tunnel.env.new /opt/llmgateway/current/ops-scripts/llmgateway-tunnel --fetch-check` 確認 200／欄位／標頭無誤。
   - **(c) 必用反向保險套用、重啟與驗收**：執行 `llmgwctl tunnel-apply --new-env /etc/llmgateway/tunnel.env.new --new-dropin /etc/llmgateway/credential.conf.new`（先備份目前可用設定 → 排 `lgw-tunnel-revert.timer`，排程失敗即中止、不套用 → 才套用暫存新檔、`daemon-reload` 並重啟 `llmgateway-tunnel.service`；窗口內勿重開機）→ `systemctl show llmgateway-tunnel -p LoadCredential` 確認生效 → 重連 `<會合點>:4711` 確認 `llmgwctl check`、`llmgwctl status`（見已驗證 `generation` 與抓取狀態）與 `GET /v1/subscriptions` HTTP 200 → 執行 `llmgwctl tunnel-apply --confirm` 取消保險。
   - **(d) 觀察期結束**：完成第一次真實輪換或觀察期確認無須 `llmgwctl rollback` 後，清理真機 `tunnel.env` 舊靜態 `KNOWN_HOSTS`（視需要保留 `TUNNEL_USER` 並清 `RENDEZVOUS`）及 `~llmgw/.ssh/known_hosts_rendezvous`（至此 P4 觀察期結束，方可進入 P5 版本升級）。
2. **第一次真實輪換觀察與退回 SOP（因 `rotate-gateway.yml` `dry_run` 不動 `NODE_GATEWAY`，端到端只能於真實輪換觀察）**：
   - **觀察清單**：由本人或 MLP PMO 觸發真實輪換後，於 `<服務主機>` 檢視 `journalctl -u llmgateway-tunnel` 是否出現斷線／探針失敗 → `fetch_ok` → `gateway_updated` → `tunnel_up`（無 `fetch_auth_failed` / `hostkey_missing` / `follow_regressed`），記錄 RTO（預期約 2–3.5 分鐘），確認 `llmgwctl status` 顯示新已驗證 `generation`、`tunnel-fetch` 為 `ok` 與 `llmgwctl check` 全綠。**若見連續 `tunnel_up` 失敗每 10 秒重啟、且 `NODE_GATEWAY` `generation` 已升，先查 `NODE_GATEWAY` 之 `host_key` 是否仍殘留上一代舊金鑰**（因 `rotate-gateway.sh:581-582` 在 `$host_key == ""` 時會保留 `$existing`）。
   - **退回 SOP（應急止血）**：若自動跟隨失敗，修復 MLP `NODE_GATEWAY`（確保為新機 IP、`port`、新機 `host_key` 與遞增 `generation`）並確認 `/etc/llmgateway/mlp-gateway.token` 有效（換好 token 後執行 `llmgwctl follow-reset --fetch-state` 清除失敗分類）；若確認為 MLP 輪換回退至低於已驗證水位導致 `follow_regressed` 鎖死，在修復 `NODE_GATEWAY` 後執行 **`llmgwctl follow-reset`**（需 root）並重啟隧道，或立即手動寫入有效 `FOLLOW_FILE`（或註解動態鍵並更新靜態 `RENDEZVOUS`／`KNOWN_HOSTS`）後執行 `systemctl restart llmgateway-tunnel.service`。

---

## 6. 對 MLP#1511 與 MLP#864 的關係，以及對 worker／Mac 的影響

- **`MLP#1511`**：本案上線後不再需要輪換後人工照貼新位址與 `known_hosts_rendezvous` 到 LLMGateway 主機，該部分施工可於 #221 上線後以 `not planned（作廢）` 關閉（PM 預設）；但 `rotate-gateway.yml` 仍必須保留收錄 `TUNNEL_EXTRA_PUBKEYS` 及完整寫入 `NODE_GATEWAY`（含新機 `host_key` 與 `[4710, 4711]`）。
- **`MLP#864`（`pool-portmap`）**：不能直接作為底層（限已註冊 `NODE_*` 節點與特定埠段）。
- **worker sidecar（零改動相容）**：維持綁 `<會合點>:127.0.0.1:4710`，MLP `shared-configs/llmgateway` 照舊透過 `/run/mlp-gateway/gateway.json` 跟隨。
- **Mac 客戶端**：維持 `客戶端自備通道`（`LLMGateway SHALL NOT 提供連線程式`），建議 MLP 補基於 `NODE_GATEWAY` 的 Mac 轉發指令（Q-MLP-E）；`<會合點>` 不可用時可續用 `ops-scripts/mac-fallback/`。

---

## 7. 相容、風險、版本與停損

- **相容性**：`lgw/client.py` 無變更；出貨 unit 不含 `LoadCredential=` 且相容僅填靜態三鍵之舊 `tunnel.env`；P1 保留對含舊 `llmgateway-tunnel.service` 之 `previous` 版本 `rollback` 能力；P4 觀察期內保留 `tunnel.env` 靜態鍵以相容 `rollback`；**P4 觀察期結束（含清理真機舊靜態鍵）前，不得對真機執行 P5 清理版本升級**。
- **版本策略**：`lgwd` HTTP API 的 `schema` 與 `env_schema` 不 bump；實作合併照常規 bump release tag。
- **停損條件**：
  1. 抓取回應缺 `host_key`／格式／帳號不符／`generation` 回退（`follow_regressed`）或同代欄位改變（`follow_invalid`）→ fail-closed 拒收新代、寫入 `.tunnel_fetch_state`，**保留最後已知位址**並以退避＋`exit 1` 重試（禁止 `accept-new`、禁止 exit `78`）；僅必要設定完全缺失回 `78`。
  2. 切換時必用反向保險，遠端探針於 ≥ 90 秒窗口內未見 HTTP 200 或到期未確認 → 自動還原舊設定重啟隧道。
  3. 切換前須完成 `--fetch-check` 驗證（確認 `NODE_GATEWAY` 讀取正常、格式與 `host_key` 合法）並於真機就緒 drop-in 與 `TOKEN_EXPIRES_AT`。

---

## 8. 專家基準、一手資料、工具方法與驗證證明（四點檢核）

1. **頂尖專家會怎麼做**：維持服務與隧道解耦的同時，讓 `llmgateway-tunnel` 以最小確定性狀態機（健康零輪詢、斷線同行程抓取＋嚴格格式與本機帳號驗證＋持久化退避＋`StrictHostKeyChecking=yes`＋`-l`/`--` 隔離＋最後已知位址保底）自動追蹤輪換，不引入額外常駐 daemon 與輪詢流量。
2. **一手資料**：見附錄 A 逐檔行號盤點（所有 MLP 與 LLMGateway 行號皆以唯讀命令核對）。
3. **方法與工具**：唯讀 `git -C <MLP checkout> show`、`openspec validate --strict` 與副本 `openspec archive` 演練。
4. **怎麼證明有用（真 `sshd` ＋ 真 `ssh` ＋ 本地 mock GitHub HTTP API 驗證）**：
   - **前提條件**：CI／測試環境具備真實 `/usr/sbin/sshd`、`/usr/bin/ssh`、`/usr/bin/ssh-keygen`（non-root loopback ephemeral port；找不到 `sshd` 時明確 `skipTest`，CI 須確保至少一組環境真實執行），搭配本地 mock HTTP server 模擬 `lgwd`（200）與模擬 GitHub Actions Variables API（`NODE_GATEWAY`，經 `LGW_GATEWAY_API_BASE`＋`LGW_ALLOW_INSECURE_API_BASE=1` 限 loopback；此法不測真實 TLS 憑證鏈，由正式程式碼預設綁定 `https://api.github.com` 與禁跨主機轉址補齊）。
   - **三大驗證 Gate**：
     - **Gate 1（純本地無隧道＋六態＋抓取分類＋`follow-reset`＋Token 到期／`auth_failed` 分級＋`O_NOFOLLOW`）**：無 `tunnel.env`／無隧道 unit 下 `llmgwctl` 全命令綠燈；存在 unit 且設定動態追蹤時：缺失／無效／回退（對比 `verified` 水位）或最近抓取被拒／`auth_failed` **且未跑**回 `fail`（提示 `follow-reset`／`follow-reset --fetch-state`）、**運行中異常、`auth_failed` 或 `config_partial` 回 `todo`**（`auth_failed` 提示執行 `--fetch-check`）、`credential_unreadable` 與 `TOKEN_EXPIRES_AT` 格式不合（`TOKEN_EXPIRES_AT 格式不合`）回 `todo`、`follow-reset` 非 root 拒執行而 root 清除 `.tunnel_generation`／`.tunnel_fetch_backoff`、`follow-reset --fetch-state` 只清 `.tunnel_fetch_state`／`.tunnel_fetch_backoff` 而保留 `.tunnel_generation`、非 root `unreadable` 顯示需權限不誤報 `missing`（以 `monkeypatch`／非 root 子行程測 `chmod 000`）；`TOKEN_EXPIRES_AT`／合理標頭剩 ≤7 天回 `todo`、標頭 `≤ fetched_at` 視為 `expiry_unknown` 不誤報；symlink 目標以 `O_NOFOLLOW` 拒讀。
     - **Gate 2（雙真實 loopback `sshd` ＋ mock GitHub API 輪換、零輪詢、同行程重連、雙水位 S 狀態、欄位格式注入負向與退避語意）**：
       1. 起真實 `sshd` G1（`P1`, `K1`）與 G2（`P2`, `K2`），起 mock `lgwd` 與 mock GitHub API（使用 ephemeral 埠避免撞 `4710`/`4711`）；驗證 `ssh` argv 含 `-o ConnectTimeout=` 與 `-l <user> -- <normalized_host>`。
       2. 健康連線期間持續數個 `PROBE_INTERVAL` 驗證 mock GitHub API **請求次數＝0**（不輪詢）。
       3. 以 **`SIGSTOP` 凍結 G1** 模擬黑洞並於 mock GitHub API 更新為 G2（`gen=2, port=P2, host_key=K2`）→ **同行程**觸發單次抓取 → 更新 `FOLLOW_FILE` 與 `.tunnel_known_hosts` → 同行程直接連上 G2 並於首次探針 200 後才寫入 `.tunnel_generation` JSON（`generation=2`＋指紋）且歸零 `consecutive_failures`（驗證 G2 未通前不預寫 `gen=2`）。
       4. **負向安全、雙水位 S 狀態與退避語意**：
          - GitHub 回 `tunnel_user`／`ip` 以 `-o...` 開頭、**IPv6 zone id（含 `fe80::1%a\n* ssh-ed25519 ...`、`fe80::1%a b`、`fe80::1%x@evil.example`、`fe80::1%a,*`、`fe80::1%-oProxyCommand=x`）、JSON 整數 `ip`（`16909060`）、尾端 `\n`（如 `evil.example\n`、`1\n`）**、`tunnel_user` 與本機不符、`port`／`generation` 為 JSON `true`/`false`（或 `generation` 超長字串）、`host_key` 含換行／第三欄／非標準 padding／壞 wire-format、缺 `host_key`、`429`/`Retry-After`（或含 rate-limit 標頭之 `403`）、`401`（或無 rate-limit 標頭之 `403`→`auth_failed`）、500、逾時／回應 >64 KiB／跨主機轉址、`generation` 回退低於 `verified`（`follow_regressed`）或同代改欄位（`follow_invalid`）時：**使用真 `python3` `ipaddress` 驗收並全數拒收，fake `ssh` 斷言 argv 未收到惡意參數且「驗證的 ip ＝ ssh 實際連的正規化主機」與 `.tunnel_known_hosts` 一致、保留最後已知位址、寫入 `.tunnel_fetch_state`、不退回 `accept-new`、遵守持久化退避並 `exit 1`**；
          - **雙水位 S 狀態**：先套用 `N+1`（`applied=N+1`）但不通過探針（`verified=N`）：① 再抓回 `N`（指紋相符 `verified`）→ 合法接受回退並重建 `ssh` 連回 `N`；② 再抓回 `N+1`（欄位同 `applied`）→ 判定未換代（記 `unverified_retry`、不累加 `consecutive_failures`、不覆寫檔案）；③ 抓回 `N` 但欄位與 `verified` 指紋不同 → 拒收記 `follow_invalid` 並累加退避；
          - 同代同欄位（閃斷重連）驗證 `consecutive_failures` 不累加；`--probe-once` 與 `--fetch-check` 只讀不寫（`--fetch-check` 成功印 `如 check 仍紅請執行 llmgwctl follow-reset --fetch-state`）；靜態三鍵回歸全綠；無 token 檔 unit 正常啟動且 key 檔 `PermissionError` 記 `.tunnel_fetch_state=credential_unreadable`（`exit 1`）；設定完全缺失、`REMOTE_SSH=off` 或未設測試旗標／非 loopback `LGW_GATEWAY_API_BASE` 回 `78`（`config_partial` 不回 `78`）。
     - **Gate 3（無洩漏與 `REMOTE_SSH=4711` 驗證）**：對完整 stdout／stderr／日誌／狀態檔搜尋假密碼、假 API key、假 GitHub token、假 IP 與 `host_key`，確認零命中；驗證 `REMOTE_SSH` 留空或 `4711` 均同時轉發 `4710` 與 `4711`。

---

## 9. 給 MLP PM 的問題清單（索引，正本見 `proposal.md`）

見 [`proposal.md`](proposal.md)「給 MLP PM 的問題」**Q-MLP-A ～ Q-MLP-F**（涵蓋 `NODE_GATEWAY` 欄位與 `Variables: Read-only` 權限、保證寫入本代 `host_key`、輪換時先更新 `NODE_GATEWAY` 再刪舊機與保留 `[4710, 4711]`、`TUNNEL_EXTRA_PUBKEYS` 契約、Mac 指令與 `MLP#1511`、以及**回退與重試禁止降低或重用 `generation`**）。

---

## 10. 已決定事項、待核准事項與 PM 預設彙整

- **已決定（本人 11-21，MyPMO#469）**：G1＝A（B1/B2/C 排除）、G2＝確定性事件驅動自 GitHub 抓 `NODE_GATEWAY`（不輪詢、兩組輸入；(1a)/(1b)/(2) 排除）、G3＝B（保留 `4711`）。
- **待本人核准**：這份設計（含放寬 `docs/deploy.md:36`「不放 token 檔」與放寬主 spec「不依賴 `NODE_*`」，及接受信任方向／repo 全 variables 拓樸可見性／格式注入風險與緩解、token 平穩期悄然到期限制）**是否核准開工**。
- **PM 預設**：擴充 `llmgwctl`（含 `follow-reset`）、新增人工觸發 `--fetch-check` 與 `TOKEN_EXPIRES_AT`、短期保留 `ops-scripts/mac-fallback`、`MLP#1511` 上線後作廢。

---

## 附錄 A：現況一手檔案與行號盤點

### A.1 LLMGateway 端現況檔案與行號

| 檔案／路徑 | 行號 | 現況實作摘要 |
|---|---|---|
| `docs/deploy.md` | §0 L15-26 | `lgwd` 綁 `127.0.0.1:<API埠>`（預設 8480）；`llmgateway-tunnel.service` 用自有金鑰 `ssh -R` 將 `<會合點>` `127.0.0.1:4710` 指到 API、`4711` 指到 `sshd`；客戶端自備通道連 `4710`。 |
| `docs/deploy.md` | §1 L29-82 | 服務帳號 `llmgw`；`gh` 要求「由本人親自以 `llmgw` 身分登入…**不放 token 檔**」（L36）；隧道私鑰 `~llmgw/.ssh/id_llmgw_tunnel`、known_hosts `~llmgw/.ssh/known_hosts_rendezvous`（L37）；`/etc/llmgateway/tunnel.env`（L49）；system unit 含 `llmgateway.service` 與 `llmgateway-tunnel.service`（L56）。 |
| `docs/deploy.md` | §4 L231-258 | Mac 客戶端手動跑 `ssh -N ... -o StrictHostKeyChecking=yes -o UserKnownHostsFile=~/.ssh/known_hosts_rendezvous -p <埠> -L 127.0.0.1:4710:127.0.0.1:4710 sshproxy@<跳板機>`（L238-240）；`lgw.client.Client("http://127.0.0.1:4710")`（L253）。 |
| `ops-scripts/llmgateway-tunnel` | L1-274 | 讀 `LGW_TUNNEL_ENV`。必填 `RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS`（L35, 81-104）；預設 `REMOTE_API=4710`、`REMOTE_SSH=4711`、`LOCAL_API=8480`、`PROBE_INTERVAL=60`、`PROBE_FAILS=3`（L36-43）；`values.get(key) or default` 語意為留空＝預設值（L107）。缺設定回 exit `78`（L44, 221-223）。已有 `ExitOnForwardFailure=yes`（L166）、雙 `-R`（L164-165）與 `ssh -W 127.0.0.1:<remote_api>` 端對端 HTTP 200 探針（L172-203）。 |
| `deploy/install.sh` | L70, 458, 465-478 | **舊版 user-level 安裝器**（`STATE_DIR="${HOME_DIR}/.local/state/llmgateway"` 設 `0700`，L70, 458）；system 版面之 `tunnel.env` 由 `lgwctl/install.py:60` 與 `deploy/bootstrap.sh:820` 寫明確 `REMOTE_SSH=4711`。 |
| `deploy/units/llmgateway.service` | L1-32 | system unit（`Type=exec`、`User=llmgw`、`LoadCredential=keys.pass:/etc/llmgateway/keys.pass` L24、`ReadWritePaths=/var/lib/llmgateway` L28），只管 `lgwd` 本身，無隧道相依。 |
| `deploy/units/llmgateway-tunnel.service` | L1-17 | system unit（`User=llmgw`、`ExecStart=/opt/llmgateway/current/ops-scripts/llmgateway-tunnel` L10、`Restart=always` L11、`RestartSec=10` L12、`RestartPreventExitStatus=78` L14）。 |
| `lgwctl/layout.py` | L31, 35-40, 94, 307-310, 358-376 | 定義 `STATE_DIR = "/var/lib/llmgateway"`（L31）、寫死 `UNIT_NAMES = (SERVICE_NAME, TUNNEL_SERVICE_NAME)`（L37）、`TUNNEL_ENV_NAME = "tunnel.env"`（L40）、`tunnel_config_state()`（L358-376）。 |
| `lgwctl/release.py` | L23-34 | `REQUIRED_FILES` 規定發佈 tag 必含 `ops-scripts/llmgateway-tunnel` 與 `deploy/units/llmgateway-tunnel.service`。 |
| `lgwctl/units.py` | L31-76, 131-143 | `install_unit_files`、`snapshot_units`、`restore_units` 固定迴圈 `UNIT_NAMES`；已有 `units.start`（L131）與 `units.restart`（L138），缺 `units.stop`。 |
| `lgwctl/install.py` | L41-64, 233-237, 270-276, 494-495 | 將 system `STATE_DIR`（`/var/lib/llmgateway`）與 `.ssh` 權限校為 `0700`（`llmgw` 擁有，L233-237）；必產 `tunnel.env` 範本（寫明確 `REMOTE_SSH=4711` L60）、enable 並嘗試 start `llmgateway-tunnel.service`。 |
| `lgwctl/upgrade.py` | L6-8, 125-136 | `upgrade` 換新版 unit 檔並 `daemon-reload`，但只重啟 `llmgateway.service`；`rollback` 呼叫 `check.run(..., no_remote=no_remote)`。 |
| `lgwctl/check.py` | L32, 36-47, 70-86, 101-104, 205-217 | `_safe_reason` 白名單（L36-47）；`_check_units` 迴圈 `UNIT_NAMES`（L70-86）；`--no-remote` 已存在（`cli.py:39`、`check.py:212-213`）。 |
| `lgwctl/probe.py` | L18-81 | `local_code` 打本機 200；`remote` 以呼叫者身分執行 `llmgateway-tunnel --probe-once`（0/1/2，L58-81）。 |
| `lgwctl/status.py` | L1-21 | 固定迴圈 `UNIT_NAMES` 輸出兩個 unit 狀態（標明不用 root）。 |
| `lgwctl/cli.py` | L21-63 | 有 `install`/`check`/`status`/`upgrade`/`rollback`/`sync`/`usage` 與遷移五指令；缺 `start`/`stop`/`restart`。 |
| `lgwctl/migrate.py` | L1-166, 1321-1422（共 2628 行；81 處引用） | 舊 user unit 遷移五部曲，呼叫 `layout.tunnel_config_state()`（L1322, 1412），已有 `lgw-cutover`／`lgw-revert` transient timer（L99-103，預設 10m）、`.cutover.lock` 互斥與「重開機後不在」（L32-35）之防護。 |
| `tests/` | 8+1 檔 | `test_lgwctl_{check,install,upgrade,migrate,layout}.py`、`test_install.py`、`test_bootstrap.py`、`lgwctl_fakes.py`（P1）；`test_tunnel.py`（P2）。 |
| `lgw/client.py` | L5, 73-110, 135-198 | 純 `urllib` HTTP client，由呼叫方傳 `base_url`；斷線回 `Result(ok=False, unreachable=True, error="unreachable")`。 |
| `ops-scripts/mac-fallback/` | `start`/`stop`/`switch`/`lib.sh` 等 | 供 Mac 本機起 `lgwd`（預設 `127.0.0.1:4710`）。 |

### A.2 MLP 端現況檔案與行號（`git -C <MLP checkout> show origin/master:<path>`）

| 檔案／路徑 | 現況實作摘要 |
|---|---|
| `shared-configs/pool-runtime/files/pool-resolve` | 定義透過 `gh api "repos/${REPO}/actions/variables/${var}" --jq .value` 讀取 `NODE_GATEWAY`（L173；`REPO` 預設 `FATESAIKOU/MyLinuxPool`）。 |
| `shared-configs/pool-runtime/files/pool-tunnel` | MLP 節點 SSH reverse tunnel 守護。Follow 模式 `gateway.json` schema：`{"ip","tunnel_user","ssh_port","generation","host_key","published_at"}`（L72-76, 163）；`pool-resolve` 讀 `NODE_GATEWAY` 則是 `.port // 22`（L395）。L525-530 實測死連線每 15s 探針會讓 sshd 延遲至 >120s 才釋放遠端埠（不介入約 60s）。 |
| `shared-configs/pool-runtime/files/known-hosts.sh` | 規範非 22 埠 `[<ip>]:<port> <host_key>` 與同時清除舊條目，避免雲端供應商回收舊 IP 導致連線被擋。 |
| `shared-configs/pool-runtime/files/pool-portmap`、`portmap.sh`、`pool-portmap.publish.sh` | MLP#864 實作。僅騎 `pool-tunnel` 的 `ctl-gateway.sock`（`pool-portmap:23, 75, 122`），要求 `gateway_port` 在 `2200–2399`（`portmap.sh:9-10, 25-28`），發布埠限 `10000–11999`（`portmap.sh:11-12, 33-34`）或節點自己的 `5220–5399` sidecar 埠（`portmap.sh:35-36`、`pool-portmap.publish.sh:199-208`）。 |
| `ops-scripts/mlp` | `cmd_trust_gateway`（L421-463）只寫 `~/.mylinuxpool/known_hosts`；`cmd_fwd`／`cmd_sidecar_link` 不建 `-R`。 |
| `docs/KEY-DESIGN.md`、`scripts/refresh-authkeys.sh` | `TUNNEL_EXTRA_PUBKEYS`（JSON 物件 `name -> pubkey`）收錄外部隧道公鑰（`KEY-DESIGN.md:55`、`refresh-authkeys.sh:164`）。 |
| `scripts/rotate-gateway.sh`、`.github/workflows/rotate-gateway.yml` | 輪換時寫入 Actions variable `NODE_GATEWAY`（`rotate-gateway.sh:572-583`：L581-582 先合併 `$existing * ... \| if $host_key == "" then . else . + {host_key: $host_key} end`，故 `$host_key` 為空時**會殘留上一代舊 `host_key`**，而非省略欄位）；`rotate-gateway.yml:461,511-545` 在切換失敗回退時寫回 `$old_json`（較低 `generation`），重試則重用 `old + 1`；`:651-680` 現況以供應商 CLI 刪除舊機，不包含通知外部 LLMGateway 主機。`rotate-gateway.yml` 的 `dry_run` 不動 `NODE_GATEWAY`（L33）。 |
| `shared-configs/llmgateway/files/{llmgateway-tunnel,start}` | worker 容器端在 `MLP_LLMGATEWAY=1` 時讀 `/run/mlp-gateway/gateway.json` 自動跟隨輪換並轉發 `127.0.0.1:4710`。 |
