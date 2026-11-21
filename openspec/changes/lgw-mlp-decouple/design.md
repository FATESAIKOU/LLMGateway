# design：LLMGateway 與 MLP 脫鉤（#221）

對應工單：#221（b 級設計單；本人核准前不實作）。上層：MyPMO#297 分流 E2。相關：MyLinuxPool#1511、MyLinuxPool#864。

> **重要前提**：**預設方案（M 最小集）只解決「輪換後位址與主機金鑰變動不再要人手動改」與「服務生命週期綁死隧道」，不解決 `mlp-gateway` 整台掛掉的全域單點問題**（是否連第二會合點／傳輸備援一併處理，見 §5.1 與決策清單 Q1）。
>
> **公開 repo 規範**：本文不寫真實主機 IP、主機名、帳號密碼、金鑰內容或私人資訊；位址與埠以 `<會合點>`、`<服務主機>`、`<API埠>`、`<SSH埠>` 等佔位符表示，本地檢視目錄以 `<MLP checkout>` 表示。

## 給本人的 3 題（決策摘要，正本見 `proposal.md`）

> 「給本人的 3 題」以 [`proposal.md`](proposal.md) 為**單一正本**；此處僅列索引與對應設計章節。U-2／U-4／U-5 已收斂為 PM 預設（M 方案、保留 `ops-scripts/mac-fallback`、暫緩 `MLP#1511` 並於上線後作廢），本人不反對即照做。

- **Q1（＝U-0）脫鉤層級**（見 §5.1、§10.1）——建議選 **(A)**（最小方案 M）。
- **Q2（＝U-3）位址與主機指紋來源**（見 §4.5、§10.1）——**建議授權 PM 先問 MLP PM（Q-MLP-6）；(2) 可行就採 (2)（取消 P2-B，仍做 P2-A），不行就採 (1a)**；(3) 不建議。
- **Q3（＝U-1 事實題）是否有不經 `<會合點>:4711` 的帶外進場**（見 §5.2、§10.1）——切換當次預設保留 `4711`；帶外驗收後才由本人決定以 `REMOTE_SSH=off` 關閉。

---

## 1. 耦合面摘要（一手行號盤點見附錄 A）

| 耦合點 | 目前位置 | 問題與影響 |
|---|---|---|
| 1. 靜態位址與 SSH 埠 | `/etc/llmgateway/tunnel.env` 的 `RENDEZVOUS` | 輪換後 IP／SSH 埠變動即斷線，需人工 SSH 上機修改。 |
| 2. 靜態主機金鑰 pin | `~llmgw/.ssh/known_hosts_rendezvous` | 輪換換 host key（或雲端供應商回收舊 IP）時 `StrictHostKeyChecking=yes` 立即拒連。 |
| 3. `sshproxy` 授權公鑰 | `TUNNEL_EXTRA_PUBKEYS` ↔ `id_llmgw_tunnel.pub` | 跨 repo 身份信任契約，目前未文件化檢查與撤銷步驟。 |
| 4. 固定保留埠 `4710`／`4711` | `tunnel.env`、`NODE_GATEWAY.ports.llmgateway` | `4711` 把主機管理 SSH 綁在會合點；且主機是否有帶外進場在 repo 內查不到。 |
| 5. `lgwctl` 生命週期強制綁定隧道 | `layout.py`、`release.py`、`install.py`、`check.py`、`status.py`、`units.py`、`probe.py` | 無法在無隧道的環境安裝／檢查純本地 `lgwd`；缺 `start`／`stop`／`restart`。 |
| 6. Mac 客戶端連線指引 | `docs/deploy.md` §4.1 手動 `known_hosts_rendezvous` | 輪換後 Mac 同步斷線。 |

---

## 2. 架構介面圖（現況 vs 主方案 M 目標架構）

### 2.1 現況架構（服務、隧道、4711、靜態 pin 全耦合）

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

### 2.2 目標架構：主方案 M（最小集：`llmgwctl` 解耦＋擴充 ＋ 現有 `llmgateway-tunnel` 讀本機 follow 檔）

```
  [follow 檔來源選項（Q2 / Q-MLP-1,6,7 裁定；Q-MLP-6 可行則走 (2) 免改 follow 追蹤）]
   - S-1a 推至 LLMGateway-keys(+主機取檔器) / S-1b 經既有通道直推 -> gateway.json
   - S-2 固定：MLP 輪換保留 IP + host_key -> P2-B 取消（僅做 P2-A REMOTE_SSH=off）
   - S-3 拉(不建議)：主機拉 NODE_GATEWAY（需新憑證、信任方向擴大、須放寬 spec）
                                     |
                                     v (寫入本機 follow 檔 gateway.json)
+-----------------------------------------------------------------------------+
| LLMGateway 主機（純本地模式可完全不裝／不啟右側隧道）                      |
|                                                                             |
|  +----------------------------------+    +-------------------------------+  |
|  | [script a: 擴充既有 llmgwctl]    |    | [script b': 改現有隧道守護]   |  |
|  |  - 新增 start / stop / restart   |    | ops-scripts/llmgateway-tunnel |  |
|  |  - install/upgrade/rollback/sync |    | + llmgateway-tunnel.service   |  |
|  |  - check/status（隧道改為可選；  |    | - P2-A(必做): REMOTE_SSH=off  |  |
|  |    依存P2-B認FOLLOW_FILE四態）   |    | - P2-B(條件): FOLLOW_FILE優先 |  |
|  |  只必管 llmgateway.service       |    | - 缺/壞/回退 -> exit 1        |  |
|  +----------------+-----------------+    | - ssh -W 探針 200 / 靜態缺 78 |  |
|                   |                      +---------------+---------------+  |
|                   v                      (無隧道時免啟)  |                  |
|          lgwd (127.0.0.1:8480) <-------------------------+ ssh -R 4710      |
|          (無應用層 auth；只綁 loopback)                  | (REMOTE_SSH 留空 |
+----------------------------------------------------------+ 維持預設 4711；  |
                                                           | =off/none 關閉)  |
                                                           v                  |
 [帶外進場（Q3 事實題）]                         +---------------------------+
  切換當次預設保留 4711；                        | MLP Gateway (<會合點>)    |
  驗收並確認帶外可用後再以 REMOTE_SSH=off 關閉   |  127.0.0.1:4710 -> API    |
                                                 +---------------------------+
                                                    ^                   ^
                                             +------+------+     +------+------+
                                             | Worker      |     | Mac 客戶端  |
                                             | sidecar     |     | 自備通道/本機|
                                             +-------------+     +-------------+
```

---

## 3. script a 設計：擴充既有 `llmgwctl` 並將隧道改為可選

### 3.1 決定：只擴充 `llmgwctl`，不新造 `lgw-service` 與非 systemd 模式（PM 預設）

- **為什麼**：`llmgwctl` 已有原子切版、policy dry-run、失敗自動回滾與安全 fd 檢查（`O_NOFOLLOW`、`LoadCredential`）。最小集合只需：
  1. 在 `lgwctl/cli.py` 補上 `start`、`stop`、`restart` 三個子命令（`lgwctl/units.py` 已有 `units.start` 與 `units.restart`，僅需補 `units.stop`），操作對象**只管 `llmgateway.service`**，啟動／重啟後以 `probe.wait_local_ok` 驗證本機 `GET /v1/subscriptions` HTTP 200，`start`／`stop` 皆冪等回 `0`。
  2. 將 `llmgateway-tunnel.service` 與 `tunnel.env` 從「必選」改為「**可選**」：
     - `layout.py`：
       - `UNIT_NAMES` 拆為核心 `CORE_UNIT_NAMES = (SERVICE_NAME,)` 與可選 `OPTIONAL_UNIT_NAMES = (TUNNEL_SERVICE_NAME,)`（保留 `TUNNEL_SERVICE_NAME` 常數供可選檢查與 `migrate.py` 複用）。
       - **`tunnel_config_state()` 與新增 `tunnel_follow_state()`**：
         - **保留 `tunnel_config_state() -> tuple[bool, str]` 既有簽名與語意**（供 `install.py:494`、`migrate.py:1322,1412` 相容使用）；若為 Q2＝(1)/(3) 且 `tunnel.env` 設有 `FOLLOW_FILE`，`tunnel_config_state()` 改檢 `IDENTITY` 與 `FOLLOW_FILE` 目標檔，不再要求 `RENDEZVOUS` 與靜態 `KNOWN_HOSTS`（未設 `FOLLOW_FILE` 且未設 `RENDEZVOUS` 時視為未啟用隧道；設 `RENDEZVOUS` 時沿用既有靜態三鍵檢查）。
         - **（依存 P2-B，僅 Q2＝(1)/(3) 實作；Q2＝(2) 跳過）新增 `tunnel_follow_state() -> tuple[Literal["unconfigured", "ok", "missing", "invalid", "regressed", "unreadable"], int | None]`**，明確區分未設 `FOLLOW_FILE`、正常（附帶整數 `generation`）、缺失、格式無效、`generation` 回退，以及**非 root 呼叫者無權限讀取 `0700` `STATE_DIR` 時的 `unreadable`**（`PermissionError` 不得回 `missing`）。
     - `release.py`：**於 P1 即從 `REQUIRED_FILES` 移除 `llmgateway-tunnel` 與 `llmgateway-tunnel.service`**（使純本地發布包可乾淨通過驗證；當機器存在或版本目錄含隧道 unit 時，仍於 `units` 與 `check` 處理）。
     - `units.py`：`install_unit_files` 僅必裝 `CORE_UNIT_NAMES`，可選 unit 若存在於版本目錄再裝；`snapshot_units`／`restore_units` 改對兩個都做存在性快照，確保對含舊 unit 的 `previous` 目錄 `rollback` 不崩潰。
     - `install.py`：無隧道環境下不再強制產生 `tunnel.env` 範本或在隧道未設定時標記 `todo`（改為僅在已設定或顯式啟用時處理）。
     - `check.py` 與 `status.py`（**含 follow 檔四態與無權限可視化契約，follow 檔部分依存 P2-B**）：
       - `llmgateway.service` 為必檢；`llmgateway-tunnel.service` 為可選——**只要機器上存在該 unit 檔（`unit_state != "missing"`）或已設定隧道設定，就照常檢查其 active 狀態與 `probe.remote`（除非 `--no-remote`，該旗標已存在 `cli.py:39`）**。
       - **關鍵狀態區分（防止 follow 檔異常被靜默報 `todo`，亦防止非 root 誤報 `missing`）**：
         - 僅當「機器存在隧道 unit 但 `tunnel.env` 完全未設定（`FOLLOW_FILE` 與 `RENDEZVOUS` 皆空）」時，隧道未跑才回 `todo`（尚未配置）。
         - **若 `tunnel.env` 已設定 `FOLLOW_FILE`**：
           - **`missing`／`invalid`／`regressed` 且隧道未 `active`**：`check` **MUST NOT 降級成 `todo`**，必須回 **`fail`**並明確列出對應固定原因（如 `follow 檔缺失`、`follow 檔無效`、`follow 檔 generation 回退（若為刻意重置請移除 .tunnel_generation）`），讓 `llmgwctl check` 以非 `0`（`1`）結束；`status` 之 `tunnel-follow` 列顯示 `missing`／`invalid`／`regressed`／`ok (gen=<n>)`。
           - **`unreadable`（非 root 呼叫者無權限讀 `/var/lib/llmgateway` `0700`）**：`status`（`lgwctl/cli.py:43` 標明不用 root）之 `tunnel-follow` 列顯示 **`unreadable（需 root 或 llmgw 才能讀）`**，**絕不得誤報 `missing`**；`check` 在非 root 遇 `unreadable` 且隧道未跑時降級回 **`todo`** 並註明「需 root 或 llmgw 才能讀」。
           - **輸出隔離（sentinel 驗證）**：`check` 常以 `root` 讀取 `llmgw` 可寫之 `STATE_DIR` 檔案，輸出只准印上述固定白名單狀態字串（仿 `check.py:36-47` `_safe_reason`），**絕不印出 `FOLLOW_FILE` 內容、IP、帳號或 `host_key`**。
     - `probe.py`：`remote()` 呼叫 `llmgateway-tunnel --probe-once`（於 P2-B 規範為**嚴格只讀**，不寫私用 `known_hosts`；私用 `known_hosts` 尚未生成或不符合當前 follow 檔時回 `2`）；因退出碼 `2` 涵蓋設定未填、找不到 `ssh` 與私用 `known_hosts` 未就緒，**`probe.remote()` 對應 `rc=2` 之訊息改為中性「`無法探`」**，避免誤導為設定未填。
     - `upgrade.py`：維持只重啟 `llmgateway.service`；`rollback` 僅在隧道存在時傳遞既有 `no_remote` 行為。
     - `lgwctl/migrate.py`：該檔有 2628 行、81 處 `tunnel` 引用，專門服務 v0.1.x user unit 歷史遷移。只要 `layout.py` 保留 `TUNNEL_SERVICE_NAME`、`OLD_USER_UNIT_NAMES`、`tunnel_config_state()` 等常數與介面，**P1 對 `migrate.py` 採不動核心邏輯（僅確保 follow 檔模式相容既有 `tunnel_config_state()` 介面）**，既有 `test_lgwctl_migrate.py` 可原樣通過。
- **被放棄的選項**：
  - 方案 F 之「薄入口 `ops-scripts/lgw-service` ＋ 非 systemd pidfile 模式 ＋ 整併 `mac-fallback`」：超出本次需求最小集，移至 §4.3 比較表；且 `mac-fallback` 已有獨立腳本，過渡期保持獨立反而零破壞。
  - 另寫獨立 bash script 重做啟停更新：重複實作 `release.fetch`／`policy` dry-run／原子回滾。

---

## 4. script b 設計：主方案 M（b' 改造現有 `llmgateway-tunnel`）vs 方案 F（完整版）

### 4.1 評估 MLP 現有互串工具（唯讀查證結論）

1. **`MLP#864`（`pool-portmap`）不能直接作為底層**：`pool-portmap` 僅騎既有 `pool-tunnel` 的 `ctl-gateway.sock`（`pool-portmap:23, 75, 122`），要求呼叫方是已註冊 `NODE_<NAME>`、且 `gateway_port` 在 `2200–2399`（`portmap.sh:9-10, 25-28`），發布埠限 `10000–11999` 或該節點自己的 `5220–5399` sidecar 埠（`portmap.sh:11-12, 33-36`、`pool-portmap.publish.sh:199-208`）。LLMGateway 主機不應為了掛一個服務埠而被迫註冊為 MLP node 並將整台主機 `localhost:22` 暴露進網格；且現有消費端固定打 `4710`。
2. **`pool-tunnel` 與 `known-hosts.sh` 是可復用的模式**：`pool-tunnel` 的 `gateway.json` follow 檔契約（`pool-tunnel:66-76, 163, 197-209`，讀本機發布檔不需憑證）與 `known-hosts.sh` 的 `[<ip>]:<port>` 單一世代覆寫規則可直接復用。
3. **`ops-scripts/mlp`（`trust-gateway`／`fwd`／`sidecar-link`）**：僅用於 Mac 側 `known_hosts` 或前景 `-L` 轉發，不能建服務端 `-R`。

### 4.2 為什麼選 b'（改造現有 `llmgateway-tunnel`）作為主方案 M

把隧道是否「必選」拆離 `lgwctl/{layout,release,install,check,status}.py`（P1）之後，沿用並改造 `ops-scripts/llmgateway-tunnel` 並**不會**把服務生命週期綁死在 MLP 上：

1. 現有 `ops-scripts/llmgateway-tunnel`（274 行）已實作並驗證：`ExitOnForwardFailure=yes`、雙 `-R`、`ssh -W 127.0.0.1:<remote_api>` 端對端 HTTP 200 探針（L161-203）、exit `78` 防護（`RestartPreventExitStatus=78`，`deploy/units/llmgateway-tunnel.service:14`）、與不寫值進 log 的安全日誌原則（L16-17）。
2. 沿用同一個 `llmgateway-tunnel.service` unit，切換時只要 `llmgwctl upgrade` ＋放好 follow 檔＋`systemctl restart llmgateway-tunnel`，**徹底消滅「4710 同一時間只能一條隧道綁定」的雙 unit 交接問題**，也不需要新造 `lgw-decouple-*` 狀態機。
3. 本人需求原話「原本那套隧道設定**可能**全部刪掉」為可選手段；b' 只需要把 `tunnel.env` 中靜態寫死的 `RENDEZVOUS` 與手動 `KNOWN_HOSTS` 拔除，換成讀 follow 檔，不必為了重命名而重造一套功能相同的 daemon。

### 4.3 方案 M（最小集，主方案）vs 方案 F（完整版，僅供比較）

| 面向 | 方案 M（最小集，**本設計主方案**） | 方案 F（完整版，**降為比較參考，不寫 delta spec/tasks**） |
|---|---|---|
| **script a** | 擴充現有 `llmgwctl` 補 `start`/`stop`/`restart` ＋ tunnel 變可選（存在時仍檢查）。 | 另增 `ops-scripts/lgw-service` 薄入口、非 systemd pidfile 模式、整併 `ops-scripts/mac-fallback`。 |
| **script b** | 改現有 `ops-scripts/llmgateway-tunnel`（續用 `llmgateway-tunnel.service`），支援本機 `gateway.json` follow 檔＋自動寫私用 `known_hosts`（`host_key` fail-closed）。 | 全新 `ops-scripts/lgw-link-mlp` daemon ＋ 新 unit `llmgateway-bridge-mlp.service` ＋ 私有 `BRIDGE_*` 設定體系。 |
| **輪換追蹤** | 啟動／斷線重連時重讀本機 follow 檔，並於既有 `PROBE_INTERVAL` watch 迴圈偵測 follow 檔變更即結束 ssh 以非 0 退出（systemd 10s 重啟帶新設定；**無常駐 GitHub API 輪詢**）。 | daemon 內建每 30 秒常駐輪詢 GitHub API `NODE_GATEWAY`。 |
| **4711 控制** | 沿用 `REMOTE_SSH`（**留空維持預設 `4711`**；顯式設 `REMOTE_SSH=off` 才不轉發；切換當次保留 `4711`）。 | 新增 `BRIDGE_ADMIN_SSH_PORT`（原建議預設關閉）。 |
| **切換機制** | 同一 unit 重啟（`upgrade` ＋ 放檔 ＋ `systemctl restart llmgateway-tunnel`）；必要時復用既有 `lgw-cutover`／`lgw-revert`。 | 新造 `lgw-decouple-cutover`／`lgw-decouple-revert` 狀態機與雙 unit 4710 交接。 |
| **OpenSpec** | 僅更新既有 `connectivity` spec，零重複規範。 | 新增 `deploy-ops` capability（與 `connectivity` 重疊）。 |
| **為何先做 M** | 改動面最小、沿用已驗證探針與 unit、不增新 daemon 維護與雙 unit 交接風險；**同樣徹底解決「輪換不再要人手動改」**。若未來 Q1（U-0）選 (B1) 第二會合點，須另加一輪 follow 檔多筆 schema 變更後再擴充。 | F 比 M 多出大量新介面、雙 unit 切換複雜度與測試面，對解決 11-20 輪換痛點並非必要；僅於未來確有跨平台無 systemd 統一包裝或獨立多橋接需求時再另開單評估。 |

### 4.4 b' 介面（P2-A 必做 vs P2-B follow 檔契約）、退出碼與恢復時間

> **拆分為兩階段**：**P2-A（`REMOTE_SSH` 解析：`off`／`none` 與留空＝`4711`）為必做**，不論 Q2 選哪一項都必須實作，以消除舊腳本 `int("off")` 觸發 exit `78` 的故障；**P2-B（下方 1～3 與 4～5 中 `FOLLOW_FILE` 機制）僅於 Q2＝(1)/(3) 時實作，若 Q2＝(2) 則取消**。

1. **Follow 檔格式（P2-B；對齊 MLP `pool-tunnel:163` 的 `gateway.json` 形狀；統一欄位名與型別正規化）**：
   ```json
   {
     "ip": "<會合點IP>",
     "tunnel_user": "sshproxy",
     "ssh_port": 22,
     "generation": 12,
     "host_key": "ssh-ed25519 AAAA...",
     "published_at": "2026-11-21T00:00:00Z"
   }
   ```
   - **欄位一致性與型別正規化**：
     - **`ssh_port`**：MLP `pool-tunnel:395` 讀 `NODE_GATEWAY` 時鍵名為 `port`，而 `gateway.json` 鍵名為 `ssh_port`（`pool-tunnel:163, 206`）。本契約一律要求 **`ssh_port`**；若上游來源為 `NODE_GATEWAY`，餵檔器寫入時須將 `.port // 22` 正規化為 `ssh_port`。
     - **`generation`**：MLP `NODE_GATEWAY.generation` 為 JSON 數字（`rotate-gateway.sh:576`），而 `pool-tunnel:163` 發佈檔為字串 `"%s"`。本契約規定：餵檔器優先寫非負整數（或純十進位數字字串），**解析端同時接受 JSON 非負整數與純十進位數字字串並一律轉為整數 `int` 比較**；非數字、空值或負數視為格式無效（`follow_invalid`）。
   - **第二會合點（U-0 選 B1 時的 schema 邊界）**：本版 follow 檔為單筆物件；若本人於 Q1（U-0）裁定採 **(B1) 第二會合點**，實作前**必須另加一輪 schema 變更**，將 follow 檔擴充為多筆陣列／主備清單與切換策略，本版不預先引入未使用的陣列結構。
   - **驗證與 fail-closed**：`ip`（非空且無空白/控制字元）、`tunnel_user`（非空）、`ssh_port`（`1..65535`）、`generation`（非負整數）與 `host_key`（非空且為合法 OpenSSH 公鑰格式）皆為必要。若缺漏或格式非法，**拒絕連線，絕不退回 `StrictHostKeyChecking=accept-new`（TOFU）**。

2. **`generation` 單調比較與同代重寫規則（P2-B）**：
   - 守護程式將最近一次成功套用連線的 `generation` 與連線欄位指紋記錄於服務狀態檔 `/var/lib/llmgateway/.tunnel_generation`（`0600`、`llmgw` 擁有；註：若緊急退回更低代數時須同步移除該檔或改用靜態設定）：
     - **`.tunnel_generation` 不存在或損毀（含首次切換）**：視為無上一代紀錄（`last_gen = None`），只要 `FOLLOW_FILE` 本身有效且 `generation >= 0` 即接受並寫入新狀態檔。
     - **`new_gen > last_gen`**：接受新世代，原子更新私用 `known_hosts` 與 `.tunnel_generation`，建立／重建隧道。
     - **`new_gen < last_gen`（generation 回退，含 repo revert 或舊檔覆蓋）**：**拒絕套用該舊代連線**，固定記 **`follow_regressed`** 事件並以 **exit `1`** 退出（不以舊代死 IP 反覆連線，待上游寫回正確世代後由 systemd `RestartSec=10` 自動恢復；若為上游故意重置世代，須由 `status`/`check` 提示移除 `.tunnel_generation`）。
     - **`new_gen == last_gen`**：若 `ip`／`ssh_port`／`tunnel_user`／`host_key` 完全相同則維持現狀；若代數未升但上述連線欄位任一改變，視為異常同代覆寫，記 `follow_invalid` 並以 **exit `1`** 拒連。

3. **私用 `known_hosts` 自動寫入與 `--probe-once` 只讀原則**：
   - **固定路徑**：`/var/lib/llmgateway/.tunnel_known_hosts`（權限 `0600`，位於既有 `STATE_DIR` `/var/lib/llmgateway/`、`llmgw` 擁有；不採暫存執行檔）。
   - **寫入規則**：守護程式啟動與連線前依 `known-hosts.sh` 規格（非 `22` 埠寫 `[<ip>]:<ssh_port> <host_key>`，`22` 埠寫 `<ip> <host_key>`）原子寫入**僅此一條目**於 `/var/lib/llmgateway/.tunnel_known_hosts`，配合 `-o UserKnownHostsFile=/var/lib/llmgateway/.tunnel_known_hosts`、`-o GlobalKnownHostsFile=/dev/null` 與 `-o StrictHostKeyChecking=yes`。每次只留當前這一代一條目，防止雲端供應商回收舊 IP 時碰撞。
   - **`--probe-once` 只讀不寫**：`lgwctl check` 等常以 `root` 身分呼叫 `llmgateway-tunnel --probe-once`（`lgwctl/probe.py:58-70`），而守護程式以 `llmgw` 執行。因此 **`--probe-once` 嚴格禁止建立或覆寫 `/var/lib/llmgateway/.tunnel_known_hosts`**（避免 root 與 `llmgw` 互踩權限）。當 `FOLLOW_FILE` 模式啟用時，若 `/var/lib/llmgateway/.tunnel_known_hosts` 不存在（守護尚未成功寫入第一代）或內容與當前 `FOLLOW_FILE` 不一致，`--probe-once` 直接回退出碼 **`2`（無法探）**。

4. **設定鍵與優先序（`/etc/llmgateway/tunnel.env` 最小演進）**：
   - **`FOLLOW_FILE`**（選填，建議路徑 `/var/lib/llmgateway/gateway.json`）：
     - **優先序**：只要 `FOLLOW_FILE` 非空，**`FOLLOW_FILE` 絕對優先**；此時**即使 `tunnel.env` 同時保留舊的靜態 `RENDEZVOUS`／`KNOWN_HOSTS`（觀察期供 `llmgwctl rollback` 回退舊版腳本用），新版守護程式亦MUST NOT 在 `FOLLOW_FILE` 缺失或無效時退回靜態鍵**（否則舊位址與舊指紋會被當成正解）。
   - **`IDENTITY`**：維持必填（`~llmgw/.ssh/id_llmgw_tunnel`）。
   - **`REMOTE_SSH`（P2-A 必做）**：**保留「未設或留空（`REMOTE_SSH=`）＝預設 `4711`」的既有語意**（`ops-scripts/llmgateway-tunnel:38,107`），避免既有留空設定主機升級後意外斷開 `4711`；改以明確關閉值 **`REMOTE_SSH=off`**（大小寫不敏感，亦接受 `none`）表示**不轉發管理 SSH 埠**，消滅現行 `int("off")` 觸發 exit `78` 停擺之問題。切換當次預設保留 `4711`。
   - **`REMOTE_API`**（預設 `4710`）、**`LOCAL_API`**（預設 `8480`）、**`PROBE_INTERVAL`**（預設 `60`）、**`PROBE_FAILS`**（預設 `3`）沿用不變。

5. **輪換追蹤、退出碼（`78` vs `1`）與恢復時間（RTO）（P2-B）**：
   - **退出碼嚴格分離（防止 follow 檔異常導致 systemd 永久停擺）**：
     - **exit `78`（`RestartPreventExitStatus=78`，停止自動重啟）**：**只保留給靜態設定鍵本身缺失／語法非法**——即 `tunnel.env` 缺少 `IDENTITY`，或 **`FOLLOW_FILE` 與 `RENDEZVOUS` 兩者皆未設定**（或靜態模式缺 `KNOWN_HOSTS`／靜態埠數值非法）。記 `config_missing`。
     - **exit `1`（由 systemd `Restart=always`＋`RestartSec=10` 每 10 秒自動重試自癒）**：當 `FOLLOW_FILE` 路徑**已設定**，但啟動或重連前遇 follow 檔**不存在**（`follow_missing`）、**JSON 損毀／欄位缺失／型別無效／同代欄位篡改**（`follow_invalid`）、**`generation` 回退**（`follow_regressed`）、或 **`host_key` 缺漏或格式非法**（`hostkey_missing`）時，守護程式記對應事件並以 **exit `1`** 結束（絕不 exit `78`，亦絕不退回 `accept-new`）。一旦取檔器或人工修復 follow 檔，systemd 下一次 10 秒重啟即自動讀入並建通；`RestartSec=10` 使重試頻率至多每分鐘 6 次（受 journald 預設速率限制保護，不致刷爆日誌）。
   - **watch 迴圈變更偵測與運行中損壞處置**：於既有 `PROBE_INTERVAL` watch 迴圈中檢查 `FOLLOW_FILE`：
     - 若偵測到**合法且 `new_gen > last_gen` 之變更**：**優雅結束（terminate）舊 `ssh` 子行程並以非 `0`（exit `1`，≠ `78`）退出**，使 systemd 帶入最新設定與私用 `known_hosts` 重新啟動（避免沿用啟動時載入的舊 `cfg`）。
     - **若運行中 follow 檔暫時變缺失、JSON 損毀或 `generation` 回退**：**僅於狀態轉換時記錄一次對應事件（`follow_missing`／`follow_invalid`／`follow_regressed`），維持現有仍健康的 `ssh` 連線不動**（避免一次短暫壞寫入就打斷正常服務）；直到該隧道實際斷線或探針失敗退出後，才於下次重啟時依 fail-closed 規則以 exit `1` 拒連自癒。
   - **恢復時間目標（RTO）**：本機 follow 檔更新後，偵測延遲 ≤ `PROBE_INTERVAL`（預設 `60s`；若舊機已死而先觸發 `ServerAliveInterval=30 × ServerAliveCountMax=3` ≈ `90s` 退出則更早）＋ systemd `RestartSec=10`（`10s`）＋ 新會合點 SSH 建連與埠公開（新機無舊連線搶埠，數秒內完成）≈ **≤ 2–3 分鐘**（相較 11-20 的 40 分鐘大幅縮短；實際端到端時間仍加上游推／拉延遲，例如 S-1a 若沿用 keysync 每 10 分鐘節奏最壞約 13 分鐘，須於 Gate 2 與 P4 第一次真實輪換實測確認）。
   - **日誌安全**：只寫事件種類（`tunnel_up`、`follow_changed`、`follow_missing`、`follow_invalid`、`follow_regressed`、`hostkey_missing`、`probe_fail`、`restart`、`config_missing`、`spawn_fail`），不輸出 IP、金鑰原文或 `ssh` 原始錯誤文字。

### 4.5 follow 檔由誰餵（Q2 / U-3 完整選項與成本對比表）

| 選項 | 主機端要寫什麼 | MLP 端要做什麼 | 影響哪個 PR | 誰負責 | 新增什麼憑證或寫入面 | 風險、信任方向與限制 |
|---|---|---|---|---|---|---|
| **S-1a：推至 `LLMGateway-keys`＋主機取檔器** | **須新增主機端取檔器碼**（擴充 `lgwd/keysync.py` 或新增 timer／腳本讀 `gateway.json` 並原子寫入 `FOLLOW_FILE`；註：現有 `keysync.py` 只拉金鑰 blob，**沒有**「順帶拉取」通用檔案）。 | `rotate-gateway.yml` 輪換後將 `gateway.json`（將 `NODE_GATEWAY.port` 正規化為 `ssh_port`）推入 `FATESAIKOU/LLMGateway-keys`。 | MLP PR ＋ LLMGateway **P2b** PR（取檔器）＋ P2-B（follow 支援）＋ P2-A。 | MLP 團隊（推送）＋ LLMGateway 團隊（取檔器＋P2）。 | 主機端**無新憑證**（沿用 `llmgw` 既有 `gh`）；**MLP Actions 須新增一把可寫入 `LLMGateway-keys` 的 secret**。 | **寫入面風險高於讀 PAT**：`LLMGateway-keys` 是全公司加密金鑰 blob 庫，該 secret 洩漏或誤寫可毀損金鑰 blob——依 `ops-scripts/key-sync:18`，解密失敗時 `MUST NOT` 動既有 `keys.env`、**服務照用舊 key 運作**，但會造成**金鑰更新停擺**（`check` 30 分鐘後報 stale）；且寫入該 repo 者可指定無 auth 的 `lgwd` 隧道目的地。**端到端時間**：若沿用 `lgwd/keysync.py:44`（預設 `600s`）／`ops-scripts/key-sync.timer:9`（`OnUnitActiveSec=10min`）節奏，最壞端到端 ≈ **10 分鐘＋3 分鐘（約 13 分鐘）**；要壓至 2–3 分鐘須另設更短 timer。 |
| **S-1b：經既有管理通道直推 `FOLLOW_FILE`** | 不寫輪詢取檔器；僅配置 `FOLLOW_FILE` 目標路徑、權限（`llmgw` 可讀）及**新增推檔端公鑰至主機 `authorized_keys`**。 | 輪換腳本或外部控制端在切換時經 SSH 將 `gateway.json` 寫入 `<服務主機>` `FOLLOW_FILE`。 | 外部／MLP 推檔腳本 ＋ LLMGateway P2-B ＋ P2-A。 | 控管直推之控制端／MLP 團隊 ＋ LLMGateway 團隊。 | **主機端須新增一把推檔端之 authorized key**（賦予 `llmgw` 或寫入帳號之 SSH 權限）；推檔端須持有對應私鑰。 | **高權限寫入面＋強時序限制**：① 賦予推檔端能 SSH 登入金鑰主機的私鑰，屬具主機 shell 權限之寫入面（風險類別高於僅寫資料 repo 的 S-1a）；② `4711` 路徑正是 `<會合點>`，且 `rotate-gateway.sh:677-680` 會直接以供應商 CLI 刪除舊機，故經舊通道直推必須嚴守「**新機就位→經舊通道推 `FOLLOW_FILE`→才刪舊機**」（**該順序在 MLP 現行流程尚未驗證**）。 |
| **S-2：MLP 輪換保留 IP＋主機金鑰** | **免寫取檔器、P2-B 取消**；**仍須升級 P1 與 P2-A（`REMOTE_SSH=off` 支援）**以配合 Q3 後續關閉 `4711`。 | 改造 `rotate-gateway.sh`（**現況 `:651-680` 為 delete-then-relabel，沒有保留 IP 機制**）以支援供應商固定 IP 保留／重指派，並將舊機主機 SSH 私鑰複製注入新機。 | 僅 **MLP PR**（`rotate-gateway.sh`／workflow）＋ **LLMGateway P1＋P2-A＋P3**（**P2-B／P2b 取消**）。 | MLP 團隊為主（納入 Q-MLP-6 確認）；LLMGateway 實作 P1＋P2-A＋P3。 | 主機端**無新憑證**；MLP 輪換流程／secret 須安全持有與搬運**會合點主機 SSH 私鑰**。 | **與 S-1/S-3 同等級之風險**：① **機敏搬運**：舊機主機私鑰複製到新機屬 MLP 側機敏搬運面；② **供應商限制**：保留／重指派 IP 依賴雲端供應商支援，跨區或異常重建未必可行；③ **輪換目的衝突**：若輪換安全目的包含「輪替會合點主機金鑰」，保留同一把 host key 將直接衝突；④ **不得過度承諾**：僅在雲端成功保留 IP 且未輪替 host key 前提下免重配，非「永不失效」。 |
| **S-3：主機定時拉取 MLP `NODE_GATEWAY`（不建議）** | 新增拉取 timer／腳本呼叫 `gh` 讀 `NODE_GATEWAY` 轉寫 `FOLLOW_FILE`（正規化 `port`→`ssh_port`）；**若選此項，須同步放寬 spec「MUST NOT 依賴 `NODE_*`」條款**。 | 維持寫入 `NODE_GATEWAY`（並須保證每次皆寫入**新機** `host_key`，見 Q-MLP-7）。 | LLMGateway P2b（拉取器）＋ P2-B ＋ P2-A ＋ **spec 放寬修訂**。 | LLMGateway 團隊（且須配合放寬 spec）。 | **主機端須新增讀 MLP repo 的 narrow PAT token 檔**。 | ① **違反規則**：直接違反 `docs/deploy.md:36`「使用瀏覽器登入，不放 token 檔」；② **到期無告警**：PAT 過期會靜默斷線（且現行 `gh` 瀏覽器登入 OAuth scope 能否讀 MLP 在 repo 內查不到）；③ **信任方向擴大**：誰能寫 `NODE_GATEWAY` 就能改存放金鑰主機的隧道端點。 |

- **PM 單一建議（寫入 `proposal.md` Q2 / U-3）**：**授權 PM 先問 MLP PM（Q-MLP-6：輪換能否保留 IP＋主機金鑰）；(2) 可行就採 (2)，不行就採 (1a)；(3) 不建議。**

---

## 5. 「脫鉤」定義與 Q1（U-0）分析、4711 處置與無空窗切換

### 5.1 「脫鉤」的選項比較與 Q1（U-0）分析

> **核心張力**：11-20 事故與本人背景包含兩個痛點——(A) 輪換後 40 分鐘開不了新隊員（位址與 host key 手動耦合）；(B) `mlp-gateway` 整台掛掉時全公司拿不到金鑰（傳輸單點依賴）。只要傳輸仍經 `mlp-gateway`，**(B) 就沒有解決**。

| Q1（U-0）選項 | 做法與範圍 | 解決了本人的哪個痛點 | 沒解決哪個痛點／風險與代價 |
|---|---|---|---|
| **(A) 僅設定、生命週期與輪換脫鉤**（傳輸仍走 `mlp-gateway`，**預設 M 方案**） | script a 獨立管 `lgwd`；script b' 讀本機 follow 檔自動跟進輪換與 pin `host_key`。 | **解決 (A)**：輪換後不需人工改設定；服務生命週期不再綁死隧道。 | **沒解決 (B)**：`mlp-gateway` 整台掛掉時，經它連線的 worker 與消費端仍拿不到金鑰。 |
| **(B) 另加第二條路徑／fallback**（使 `mlp-gateway` 掛掉時仍拿得到金鑰） | **(B1) 第二會合點**：另一台非 MLP 跳板機，不動 `lgwd`；**需在實作前另加一輪 follow 檔多筆 schema 變更**；<br>**(B2) 主要端常備本地 fallback**：Mac／PM 主機常備 `mac-fallback` 或 script a 本地起 `lgwd`。 | **(B1)** 解決 (A) 且緩解全域單點 (B)；<br>**(B2)** 解決 (A) 且讓 Mac 等本地端在會合點掛掉時仍拿得到金鑰。 | **(B1)** 需多維護一台獨立會合點主機、消費端切換與額外一輪多筆 schema 變更；<br>**(B2)** **不解決容器內 worker** 在 `mlp-gateway` 掛掉時取金鑰的問題。 |
| **(C) 連傳輸都不經 `mlp-gateway`**（全直連或自建網格） | 移除 `mlp-gateway` 轉發，worker 與 Mac 直連 `<服務主機>`。 | **同時徹底解決 (A) 與 (B)**。 | **代價與風險極高**：`lgwd` 現無應用層認證（`docs/deploy.md:17`），直連須補 TLS＋mTLS/token、暴露主機服務端點於公網或另建 WireGuard/Tailscale 網格，且需改動全池 worker 容器網路隔離設計。 |

- **單一建議**：**建議選 (A)（主方案 M）**，以最小成本優先落地消除 (A) 輪換斷線與維運耦合（並清楚接受此階段不解 `mlp-gateway` 單點）。

### 5.2 4711（管理用 SSH）處置：預設當次保留，帶外存在性列為 Q3（U-1 事實題）

- **事實狀態（查不到）**：`docs/deploy.md:113` 只寫「切換前務必確認具備不經隧道的進場方式」，repo 內**沒有任何紀錄證實 `<服務主機>` 目前實際具備哪一條帶外進場管道**；架構師與審查者皆無法查證，只有本人能回答（Q3：有／沒有／不確定）。
- **切換當次一律預設保留 4711**：因重啟隧道會斷開正在走 4711 的 SSH 連線，且是否有帶外進場未知，**本方案保留「`REMOTE_SSH` 留空＝預設 `4711`」語意，切換當次一律保留 `4711` 轉發**（不設 `REMOTE_SSH=off`）。
- **關閉順序**：待本案切換驗收穩定，且本人親自實測不經 `<會合點>:4711` 的帶外進場管道（直連／VPN／Cloud Console）可登入並能執行 `llmgwctl upgrade` 後，再由本人決定於 `tunnel.env` 明確寫入 **`REMOTE_SSH=off`** 並重啟 `llmgateway-tunnel.service` 關閉 `4711`，最後通知 MLP 移除 `NODE_GATEWAY.ports.llmgateway` 中的 `4711`。

### 5.3 M 方案切換流程、舊版回滾相容與反向保險

主方案 M 沿用同一個 `llmgateway-tunnel.service` unit：
1. **常規切換流程（含舊版 `llmgwctl rollback` 相容）**：
   - `llmgwctl upgrade --tag <新版>`（`upgrade.py` 原生只重啟 `llmgateway.service`，**完全不碰正在跑的 `llmgateway-tunnel.service`**）；
   - 放好本機 follow 檔（`gateway.json`）並更新 `/etc/llmgateway/tunnel.env`：**新增 `FOLLOW_FILE=<路徑>`，保留 `REMOTE_SSH=4711`（或留空，兩者皆維持 4711）**；
   - **觀察期內保留靜態鍵與 `REMOTE_SSH=off` 回滾順序**：**P4 觀察期內不要刪除 `/etc/llmgateway/tunnel.env` 既有的 `RENDEZVOUS`／`KNOWN_HOSTS`（與 `known_hosts_rendezvous` 檔）**——因新版以 `FOLLOW_FILE` 優先，保留靜態鍵不會影響新版運作，且萬一執行 `llmgwctl rollback` 切回不認 `FOLLOW_FILE` 的舊版腳本時，舊版仍讀得到靜態三鍵；**且設 `REMOTE_SSH=off` 與清理舊靜態鍵同時才算觀察期結束——若設了 `REMOTE_SSH=off` 之後還未結束觀察期就須執行 `llmgwctl rollback` 回退舊腳本，必須先把 `REMOTE_SSH` 改回 `4711`（或留空），否則舊腳本 `int("off")` 會以 exit `78` 永久停擺**；
   - 若直接有帶外進場，執行 `systemctl restart llmgateway-tunnel.service` 並等探針 200 即完成。
2. **需要無人值守反向保險時（復用 `lgw-cutover`／`lgw-revert`，不重造 `lgw-decouple-*`）**：
   - 若操作者只能經 4711 進場或希望有自動回退保險，切換腳本應**復用 `lgwctl/migrate.py` 既有的 `systemd-run` transient unit 與 `lgw-cutover`／`lgw-revert` 名稱與 `.cutover.lock` 互斥機制**（先備份原 `tunnel.env` 與 `known_hosts_rendezvous`）：
     1. 排程 `lgw-revert.timer`（≥ 5 分鐘，預設 10 分鐘）並於分離式 transient unit 執行重啟；
     2. **等待上限 ≥ 90 秒**才判定新隧道遠端探針失敗（依據 MLP `pool-tunnel:525-530` 實測，舊連線斷掉後遠端 sshd 可能持有死埠 60 秒、頻繁探測下甚至超過 120 秒；只等 30 秒容易誤判回滾）；
     3. 若探針失敗或超時未確認，自動還原舊 `tunnel.env` 並重啟 `llmgateway-tunnel.service`；
     4. **必須明確警示的限制**：**`lgw-revert.timer` 是 transient unit，重開機後即失效，切換窗口內切勿重開機**（同 `lgwctl/migrate.py:32-35`、`docs/deploy.md:120`）。

### 5.4 本人在 `<服務主機>` 要貼的步驟＋第一次真實輪換觀察與退回 SOP

1. **切換步驟輪廓（佔位符）**：
   - **(a) 準備**：盤點並實測不經 `<會合點>:4711` 的帶外進場方式；確保目前無進行中的 `lgwctl migrate` 狀態。
   - **(b) 升級與設定**：執行 `llmgwctl upgrade --tag <新版>` → 依 Q2（U-3）定案機制放入 `gateway.json` follow 檔（`llmgw` 可讀）→ 更新 `/etc/llmgateway/tunnel.env`（加入 `FOLLOW_FILE=<路徑>`，**保留原有靜態 `RENDEZVOUS`／`KNOWN_HOSTS` 供回退，並保持 `REMOTE_SSH=4711` 或留空**）。
   - **(c) 重啟與驗收**：透過獨立終端／帶外連線或帶反向保險重啟 `llmgateway-tunnel.service` → 確認 `llmgwctl check`、`llmgwctl status`、`systemctl status llmgateway-tunnel` 與端對端 `GET /v1/subscriptions` HTTP 200（若有保險則執行取消保險指令）。
   - **(d) 帶外確認後關閉 4711**：觀察期後若帶外進場已證實可靠，再於 `tunnel.env` 設 **`REMOTE_SSH=off`** 並重啟隧道；**設 `REMOTE_SSH=off` 後若尚未清理舊靜態鍵而須執行 `llmgwctl rollback` 回退舊腳本，必須先將 `REMOTE_SSH` 改回 `4711`（或留空）**；待兩者皆穩才清理舊靜態鍵與 `known_hosts_rendezvous`，此時 P4 觀察期完全結束。
2. **第一次真實輪換觀察與退回 SOP（因 `rotate-gateway.yml` `dry_run` 不動 `NODE_GATEWAY`，真機端到端只能於下次真實輪換觀察）**：
   - **觀察清單**：由本人或 MLP PMO 在場觸發輪換；輪換完成後於 `<服務主機>` 檢查 `journalctl -u llmgateway-tunnel` 是否出現 `follow_changed` → `tunnel_up`（無 `follow_missing` / `follow_invalid` / `follow_regressed` / `hostkey_missing` / `probe_fail`），記錄實際恢復秒數是否符合預期，並驗證 worker 端與 `llmgwctl check` 經 `<會合點>:4710` 回 HTTP 200。**若見 `journalctl` 出現連續 `tunnel_up` 失敗與每 10 秒重啟迴圈、且 follow 檔 `generation` 已升，須先查 follow 檔／`NODE_GATEWAY` 之 `host_key` 是否仍殘留上一代舊機金鑰**（因 `rotate-gateway.sh:581-582` 在 `$host_key == ""` 時會保留 `$existing`）。
   - **退回 SOP（應急手貼輪廓，不寫真實位址）**：若自動跟隨失敗，本人登入 `<服務主機>`（經保留的 `<舊會合點>:4711` 若尚未斷，或經帶外進場），視情況擇一止血：
     - **方式 A（修復 follow 檔）**：將 `<新會合點>` 的 `ip`、`ssh_port`、`tunnel_user`、`host_key`、`generation`（確保大於舊代；若需刻意降代則同時移除 `/var/lib/llmgateway/.tunnel_generation`）寫入本機 follow 檔，等待 10 秒內自癒或 `systemctl restart llmgateway-tunnel.service`；
     - **方式 B（退回靜態鍵）**：暫時註解 `# FOLLOW_FILE=...`、將靜態 `RENDEZVOUS=<帳號>@<新會合點>:<SSH埠>` 與 `KNOWN_HOSTS` 更新為新值，執行 `systemctl restart llmgateway-tunnel.service` 與 `llmgwctl check` 立即止血（若還須 `llmgwctl rollback` 回退舊版腳本，務必同時將 `REMOTE_SSH` 改回 `4711` 或留空）。

---

## 6. 對 MLP#1511 與 MLP#864 的關係，以及對 worker／Mac 的影響

### 6.1 對 `MLP#1511` 的關係
- **作廢部分**：只要本案 M 方案上線並搭配 Q2 的自動餵檔（或 MLP 保留 IP＋host key），不再需要輪換後人工照貼新位址與 `known_hosts_rendezvous` 到 LLMGateway 主機，該部分施工可在 #221 上線後以 `not planned（作廢）` 關閉（PM 預設）。
- **仍需保留的部分**：`rotate-gateway.yml` 必須繼續將 `TUNNEL_EXTRA_PUBKEYS` 收錄至新 Gateway 的 `sshproxy` `authorized_keys`，且 `NODE_GATEWAY` 須持續記錄完整 `{ip, port, tunnel_user, host_key, generation}`。過渡期間（#221 上線前）若先輪換，仍須留意人工同步。

### 6.2 對 `MLP#864`（`pool-portmap`）的關係
- **結論：不能直接當作 script b 底層**（詳見 §4.1 與附錄 A 行號證據）。未來若要讓 `pool-portmap` 支援外部服務，尚缺：非 node 輕量隧道建立器、開放非 `2200–2399` 節點註冊服務埠、消費端改為服務名解析。

### 6.3 對 worker sidecar 與 Mac 客戶端的影響
- **worker sidecar（零改動相容）**：script b' 維持綁 `<會合點>:127.0.0.1:4710`，MLP `shared-configs/llmgateway` 照舊透過 `/run/mlp-gateway/gateway.json` 自動追蹤輪換。
- **Mac 客戶端**：維持 `客戶端自備通道`（`LLMGateway SHALL NOT 提供連線程式`；Mac 用 `mlp` 或 `ssh -J` 自行轉發，建議 MLP 補基於 `NODE_GATEWAY` 的 Mac 轉發指令〔Q-MLP-5〕）；`<會合點>` 不可用時可續用 `ops-scripts/mac-fallback/`。

---

## 7. 相容、風險、版本與停損

- **相容性**：`lgw/client.py` 無變更；P1 拆除必選隧道耦合時，保留對含舊 `llmgateway-tunnel.service` 之 `previous` 版本 `rollback` 能力；**P4 觀察期內保留 `tunnel.env` 靜態鍵，且設 `REMOTE_SSH=off` 與清靜態鍵同時才算觀察期結束（未清前若須 `rollback` 至舊版腳本，必先將 `REMOTE_SSH` 改回 `4711`／留空，避免舊腳本 exit `78`）**；**P4 觀察期結束前，不得對真機執行會刪除舊相容檔的 P5 版本升級**。
- **版本策略**：`lgwd` HTTP API 的 `schema` 與 `env_schema` 不 bump；實作合併照常規 bump release tag。
- **停損條件**：
  1. follow 檔缺失／無效／`host_key` 缺漏或格式不符／`generation` 回退（`follow_regressed`）→ fail-closed 拒連並以 **exit `1`** 重試（不 exit `78`、禁止退回 `accept-new`、禁止退回靜態鍵）；僅靜態設定鍵本身缺失回 `78`。
  2. 切換時若帶反向保險，遠端探針於 ≥ 90 秒窗口內仍未見 HTTP 200 或到期未 confirm → 自動還原舊設定重啟隧道。
  3. 未確認帶外進場可用前，禁止設 `REMOTE_SSH=off` 移除 `4711`。

---

## 8. 專家基準、一手資料、工具方法與驗證證明（四點檢核）

1. **頂尖專家會怎麼做**：保留已實證可靠的 `llmgateway-tunnel` 探針與 unit 骨架，僅將生命週期強耦合拆成可選、把靜態位址/known_hosts 換成可驗證的單一本機 follow 檔契約（fail-closed＋exit 1 自癒）；最小改動解決輪換故障類，不提前過度工程化。
2. **一手資料**：見附錄 A 逐檔行號盤點（所有 MLP 與 LLMGateway 行號皆以唯讀命令核對）。
3. **方法與工具**：唯讀 `git -C <MLP checkout> show`、`openspec validate --strict` 與副本 `openspec archive` 演練。
4. **怎麼證明有用（真 `sshd` ＋ 真 `ssh` 驗證 OpenSSH 行為）**：
   - **前提條件**：測試環境（CI 或本機）需具備真實 `/usr/sbin/sshd`、`/usr/bin/ssh`、`/usr/bin/ssh-keygen`（不需 root，於 loopback ephemeral port 起 non-root `sshd -D -f`）；`lgwd` 端可由本地 mock HTTP server 模擬 `GET /v1/subscriptions` 回 `200`。
   - **為何不能用假 ssh**：11-20 與 2026-09-14 事故核心是 **OpenSSH 的 `StrictHostKeyChecking=yes` 與 `[ip]:port` `known_hosts` 比對語意**（MLP `known-hosts.sh:12-19` 亦以真實 loopback `sshd` 實測），假 ssh 無法證明金鑰比對與替換是否正確。
   - **三大驗證 Gate**：
     - **Gate 1（純本地無隧道＋follow 四態/無權限可視化）**：無 `tunnel.env`／無 `llmgateway-tunnel.service` 沙箱下，`llmgwctl install`/`start`/`stop`/`restart`/`upgrade`/`rollback`/`check`/`status` 全綠（exit `0`）；另測「unit 存在時 `check` 仍照常檢查」、（P2-B 啟用時）「已設 `FOLLOW_FILE` 但檔缺/無效/回退且隧道未跑時 `check` 報 `fail`、`status` 顯示對應態」，以及「非 root 無權限讀 `0700` `STATE_DIR` 時 `status` 顯示需權限不誤報 `missing`、`check` 回 `todo`」。
     - **Gate 2（雙真實 loopback `sshd` 模擬輪換、負向安全與自癒）**：
       1. 起真實 `sshd` G1（loopback port `P1`、真實 ed25519 key `K1`）與 `sshd` G2（port `P2`、host key `K2`），起本地 HTTP 200 server 模擬 `lgwd`。
       2. follow 檔指 G1（`generation=1`），啟動 `ops-scripts/llmgateway-tunnel` → 驗證私用 `known_hosts`（`/var/lib/llmgateway/.tunnel_known_hosts`）寫入 `[127.0.0.1]:P1 K1`，真實 `ssh -R` 掛上並經 G1 探針回 HTTP 200（並驗證 `--probe-once` 不寫入該檔）。
       3. 模擬輪換：停止 G1、follow 檔更新為 G2（`generation=2, P2, K2`）→ **不改程式、不手動執行指令**，驗證隧道自動更新私用 `known_hosts` 為 `[127.0.0.1]:P2 K2`、成功連線 G2 並探針回 HTTP 200，記錄切換恢復耗時（驗證符合 RTO ≤ 2–3 分鐘）。
       4. 負向安全與自癒：
          - (a) follow 檔 `host_key` 篡改為 `K1`（對端 G2 `K2`）或清空、`FOLLOW_FILE` 指向不存在檔案或損毀 JSON、`generation` 回退（如 `1 < 2`，記 `follow_regressed`）或同代欄位改變（記 `follow_invalid`）→ 驗證拒絕連線且**絕不退回 `accept-new` 或靜態鍵**，並回 **exit `1`**（非 `78`）；運行中健康連線遇檔損毀／回退不主動中斷；`.tunnel_generation` 缺失或損毀時可正常接受；隨後修復為有效 `generation=3` 可自動重連恢復；
          - (b) 靜態鍵 `IDENTITY` 缺失（或 `FOLLOW_FILE` 與 `RENDEZVOUS` 皆空）→ 回 exit `78`。
     - **Gate 3（無洩漏與 `REMOTE_SSH` 語意驗證）**：對所有輸出（含 root 讀 `FOLLOW_FILE` 的 `check`/`status`）與日誌 grep fixture 金鑰／密碼／測試 IP，確認零命中；驗證 `REMOTE_SSH=` 留空仍轉發 4711、`REMOTE_SSH=off`/`OFF`/`none`/`None` 不綁 4711。

---

## 9. 給 MLP PM 的問題清單（不猜測）

1. **Q-MLP-1（follow 檔餵入與憑證機制）**：在 §4.5 方案中，若 Q-MLP-6 不可行，MLP 偏好哪一種對接？(S-1a) 推至 `LLMGateway-keys`（須 MLP Actions 配寫金鑰庫 repo secret）、或 (S-1b) 經既有通道於刪舊機前直推、或 (S-3) 主機另建拉取機制（不建議）？
2. **Q-MLP-2（`TUNNEL_EXTRA_PUBKEYS` 契約）**：是否確認長期保留 `TUNNEL_EXTRA_PUBKEYS`（`docs/KEY-DESIGN.md:55`）作為外部隧道撥入 `sshproxy` 的白名單與撤換流程？
3. **Q-MLP-3（`4710`／`4711` 保留段）**：`NODE_GATEWAY.ports.llmgateway`（`[4710, 4711]`）短期維持 `4710` 相容是否符合規劃？`4711` 待 LLMGateway 主機帶外驗收完成後再通知移除是否可行？
4. **Q-MLP-4（已自證，不列阻塞前置）**：現行 `ops-scripts/llmgateway-tunnel:172-176` 已使用同一把金鑰透過 `ssh -W 127.0.0.1:<remote_api>` 執行探針並於 11-20 前長期正常運作；**此項已由現行行為自證，無須列為等 MLP 回覆的前置條件**（僅請本人在真機最後確認現行探針無被拒絕即可）。
5. **Q-MLP-5（Mac 端接入與 `MLP#1511`）**：MLP 是否計畫提供基於 `NODE_GATEWAY` 的 Mac 轉發指令（取代 `deploy.md §4.1` 手動 `known_hosts_rendezvous`）？`MLP#1511` 是否於 #221 部署上線後以作廢關閉？
6. **Q-MLP-6（輪換能否保留 IP＋host key）**：MLP 執行 `rotate-gateway` 時，在雲端環境與自動化流程上是否可行「保留固定 IP 並複製沿用同一把 Gateway host key」？若可行，機敏私鑰搬運邊界與是否影響輪替主機金鑰目的為何？
7. **Q-MLP-7（`rotate-gateway` 取不到新機 `host_key` 會殘留上一代指紋）**：現況 `rotate-gateway.sh:581-582` 先合併 `$existing`（`$existing * {ip, generation, rotated_at} | if $host_key == "" then . else . + {host_key: $host_key} end`），導致當 `rotate_read_host_key` 回空字串時，**`NODE_GATEWAY` 並不會省略 `host_key`，而是會讓上一代舊機的 `host_key` 原樣殘留在新一代中**！這會讓上游產出「新 IP＋新 `generation`＋舊 `host_key`」的合法格式 follow 檔，使守護程式不報 `hostkey_missing` 而是陷入 `StrictHostKeyChecking=yes` 比對失敗的靜默 exit `1` 重試迴圈。請問 MLP 能否保證每次輪換都寫入**新機**的 `host_key`（或在取不到新機 `host_key` 時直接讓輪換失敗而不留用舊金鑰）？

---

## 10. 未決事項與 PM 預設彙整

### 10.1 給本人的 3 題（索引；單一正本見 `proposal.md`）

> 題目全文與選項白話代價以 [`proposal.md`](proposal.md)「給本人的 3 題（決策清單，單一正本）」為唯一正本；本節僅保留裁定對設計階段之分支影響。

- **Q1（＝U-0 脫鉤層級）**：決定只做主方案 M（選 **(A)**，建議），或須另補多筆備援設計（**(B1)**）／Mac 本地（**(B2)**）／直連改網格（**(C)**）。
- **Q2（＝U-3 位址與主機指紋來源）**：**單一建議「授權 PM 先問 MLP PM（Q-MLP-6）；(2) 可行就採 (2)，不行就採 (1a)」**。採 (2) 時取消 P2-B、P2b 與 P1 `FOLLOW_FILE` 認知（仍做 P2-A `REMOTE_SSH=off`）；採 (1a) 時執行 P1＋P2-A＋P2-B＋P2b。
- **Q3（＝U-1 帶外進場事實題）**：有／沒有／不確定；切換當次預設保留 `4711`，確認帶外可用後才以 `REMOTE_SSH=off` 關閉。

### 10.2 PM 預設（本人不反對即照做，不佔選擇題額度）

- **U-2（script a 形態）**：採主方案 M——擴充現有 `llmgwctl`（`start`/`stop`/`restart`＋tunnel 可選，存在時仍檢查），不另造 `lgw-service` 與非 systemd pidfile 模式。
- **U-4（`ops-scripts/mac-fallback` 去留）**：短期保留獨立腳本，不在本單整併。
- **U-5（`MLP#1511` 過渡期處置）**：暫緩施工，待 #221 部署上線後以作廢關閉。

---

## 附錄 A：現況一手檔案與行號盤點

### A.1 LLMGateway 端現況檔案與行號

| 檔案／路徑 | 行號 | 現況實作摘要 |
|---|---|---|
| `docs/deploy.md` | §0 L15-26 | `lgwd` 綁 `127.0.0.1:<API埠>`（預設 8480）；`llmgateway-tunnel.service` 用自有金鑰 `ssh -R` 將 `<會合點>` `127.0.0.1:4710` 指到 `lgwd` API、`127.0.0.1:4711` 指到主機 `sshd`；客戶端自備通道連 `4710`。 |
| `docs/deploy.md` | §1 L29-82 | 服務帳號 `llmgw`；`gh` 要求「由本人親自以 `llmgw` 身分登入…**不放 token 檔**」（L36）；隧道私鑰 `~llmgw/.ssh/id_llmgw_tunnel`、known_hosts `~llmgw/.ssh/known_hosts_rendezvous`（L37）；`/etc/llmgateway/tunnel.env`（L49）；system unit 含 `llmgateway.service` 與 `llmgateway-tunnel.service`（L56）。 |
| `docs/deploy.md` | §2.2 L111-123 | 寫明切換前須備妥**不經隧道的進場方式**（L113，屬操作要求，非「主機已有」之實證）、窗口內切勿重開機（L120，因反向保險為 transient timer）。 |
| `docs/deploy.md` | §4 L231-258 | Mac 客戶端手動跑 `ssh -N ... -o StrictHostKeyChecking=yes -o UserKnownHostsFile=~/.ssh/known_hosts_rendezvous -p <埠> -L 127.0.0.1:4710:127.0.0.1:4710 sshproxy@<跳板機>`（L238-240）；`lgw.client.Client("http://127.0.0.1:4710")`（L253）。 |
| `ops-scripts/llmgateway-tunnel` | L1-274 | 讀 `LGW_TUNNEL_ENV`。必填 `RENDEZVOUS`、`IDENTITY`、`KNOWN_HOSTS`（L35, 81-104）；預設 `REMOTE_API=4710`、`REMOTE_SSH=4711`、`LOCAL_API=8480`、`PROBE_INTERVAL=60`、`PROBE_FAILS=3`（L36-43）；`values.get(key) or default` 語意為留空＝預設值（L107）。缺設定回 exit `78`（L44, 221-223）。已有 `ExitOnForwardFailure=yes`（L166）、雙 `-R`（L164-165）與 `ssh -W 127.0.0.1:<remote_api>` 端對端 HTTP 200 探針（L172-203）。 |
| `deploy/install.sh` | L70, 458, 465-478 | **舊版 user-level 安裝器**（`STATE_DIR="${HOME_DIR}/.local/state/llmgateway"` 設 `0700`，L70, 458）；範本寫 `REMOTE_SSH=`（L473）僅影響舊 user 版面，system 版面之 `tunnel.env` 由 `lgwctl/install.py:60` 與 `deploy/bootstrap.sh:820` 寫明確 `REMOTE_SSH=4711`。 |
| `deploy/units/llmgateway.service` | L1-32 | system unit（`Type=exec`、`User=llmgw`、`ReadWritePaths=/var/lib/llmgateway` L30），只管 `lgwd` 本身，無隧道相依。 |
| `deploy/units/llmgateway-tunnel.service` | L1-17 | system unit（`User=llmgw`、`ExecStart=/opt/llmgateway/current/ops-scripts/llmgateway-tunnel` L10、`Restart=always` L11、`RestartSec=10` L12、`RestartPreventExitStatus=78` L14）。 |
| `lgwctl/layout.py` | L31, 35-40, 52-57, 94, 256-259, 307-310, 358-376 | 定義 `STATE_DIR = "/var/lib/llmgateway"`（L31）、寫死 `UNIT_NAMES = (SERVICE_NAME, TUNNEL_SERVICE_NAME)`（L37）、`TUNNEL_ENV_NAME = "tunnel.env"`（L40）、`tunnel_config_state()`（強制檢查 `RENDEZVOUS`／`IDENTITY`／`KNOWN_HOSTS`，L358-376）。 |
| `lgwctl/release.py` | L23-34 | `REQUIRED_FILES` 規定發佈 tag 必含 `ops-scripts/llmgateway-tunnel` 與 `deploy/units/llmgateway-tunnel.service`。 |
| `lgwctl/units.py` | L31-76, 131-143 | `install_unit_files`、`snapshot_units`、`restore_units` 固定迴圈 `UNIT_NAMES`；已有 `units.start`（L131）與 `units.restart`（L138），缺 `units.stop`。 |
| `lgwctl/install.py` | L41-42, 53-64, 233-237, 270-276, 322-328, 383-402, 494-495 | 將 system `STATE_DIR`（`/var/lib/llmgateway`）與 `.ssh` 權限校為 `0700`（`llmgw` 擁有，L233-237）；必產 `tunnel.env` 範本（寫明確 `REMOTE_SSH=4711` L60）、enable 並嘗試 start `llmgateway-tunnel.service`。 |
| `lgwctl/upgrade.py` | L6-8, 125-136 | `upgrade` 換新版 unit 檔並 `daemon-reload`，但**刻意只重啟 `llmgateway.service`**（不碰承載 4711 的隧道）；`rollback` 呼叫 `check.run(..., no_remote=no_remote)`。 |
| `lgwctl/check.py` | L32, 70-86, 101-104, 205-217 | `_check_units` 迴圈 `UNIT_NAMES`（unit 不在直接 `fail`；未跑且 `!tunnel_ready` 報 `todo`）；`--no-remote` 已存在（`cli.py:39`、`check.py:212-213`）。 |
| `lgwctl/probe.py` | L18-81 | `local_code` 打本機 200；`remote` 以呼叫者身分執行 `llmgateway-tunnel --probe-once`（0/1/2，L58-81）。 |
| `lgwctl/status.py` | L1-21 | 固定迴圈 `UNIT_NAMES` 輸出兩個 unit 狀態。 |
| `lgwctl/cli.py` | L21-63 | 有 `install`/`check`/`status`/`upgrade`/`rollback`/`sync`/`usage` 與遷移五指令；**缺 `start`/`stop`/`restart`**。 |
| `lgwctl/migrate.py` | L1-166, 1321-1422（共 2628 行；`grep -ic tunnel` = 81 處） | 舊 user unit 遷移五部曲，呼叫 `layout.tunnel_config_state()`（L1322, 1412），已有完整 `lgw-cutover`／`lgw-revert` transient timer（L99-103，預設 10m）、`.cutover.lock` 互斥與「`lgw-revert.timer` 重開機後不在」（L32-35）之防護。 |
| `tests/` | 9 檔 | `test_lgwctl_{check,install,upgrade,migrate,layout}.py`、`test_tunnel.py`、`test_install.py`、`test_bootstrap.py`、`lgwctl_fakes.py` 含必選隧道假設。 |
| `lgw/client.py` | L5, 73-110, 135-198 | 純 `urllib` HTTP client，由呼叫方傳 `base_url`；斷線回 `Result(ok=False, unreachable=True, error="unreachable")`。 |
| `ops-scripts/mac-fallback/` | `start`/`stop`/`switch`/`lib.sh` 等 | 供 Mac 本機起 `lgwd`（預設 `127.0.0.1:4710`）。 |

### A.2 MLP 端現況檔案與行號（`git -C <MLP checkout> show origin/master:<path>`）

| 檔案／路徑 | 現況實作摘要 |
|---|---|
| `shared-configs/pool-runtime/files/pool-tunnel` | MLP 節點 SSH reverse tunnel 守護。Follow 模式定義 `gateway.json` schema：`{"ip","tunnel_user","ssh_port","generation","host_key","published_at"}`（L72-76, 163；讀取 L197-209，註記「讀取發布檔不需憑證」L66-71）。`pool-resolve` 讀 `NODE_GATEWAY` 則是 `.port // 22`（L395）。L482-523 為 30s 漂移偵測；L525-530 實測死連線每 15s 探針會讓 sshd 延遲至 **>120s** 才釋放遠端埠（不介入約 **60s**）；L544-598 為 `start_master`（`-R 127.0.0.1:${MY_PORT}:localhost:22` 在 L595；`host_key` 為空時退回 `accept-new` 在 L572-574）。 |
| `shared-configs/pool-runtime/files/known-hosts.sh` | 規範非 22 埠 `[<ip>]:<port> <host_key>` 與同時清除舊條目，避免雲端供應商回收舊 IP 導致連線被擋。 |
| `shared-configs/pool-runtime/files/pool-portmap`、`portmap.sh`、`pool-portmap.publish.sh` | MLP#864 實作。僅騎 `pool-tunnel` 的 `ctl-gateway.sock`（`pool-portmap:23, 75, 122`），要求 `gateway_port` 在 `2200–2399`（`portmap.sh:9-10, 25-28`），發布埠限 `10000–11999`（`portmap.sh:11-12, 33-34`）或節點自己的 `5220–5399` sidecar 埠（`portmap.sh:35-36`、`pool-portmap.publish.sh:199-208`）。 |
| `shared-configs/pool-runtime/files/sidecar-port.sh` | 定義 sidecar 埠段；約 L12 註明 `pool-peer` 已於 MLP#1085 PR-8 刪除（實際不存在）。 |
| `ops-scripts/mlp` | `cmd_trust_gateway`（L421-463）只寫 `~/.mylinuxpool/known_hosts`；`cmd_fwd`／`cmd_sidecar_link` 不建 `-R`。 |
| `docs/KEY-DESIGN.md`、`scripts/refresh-authkeys.sh` | `TUNNEL_EXTRA_PUBKEYS`（JSON 物件 `name -> pubkey`）收錄外部隧道公鑰（`KEY-DESIGN.md:55`、`refresh-authkeys.sh:164`）。 |
| `scripts/rotate-gateway.sh`、`.github/workflows/rotate-gateway.yml` | 輪換時寫入 `NODE_GATEWAY`（`rotate-gateway.sh:572-583`：L581-582 先合併 `$existing * ... | if $host_key == "" then . else . + {host_key: $host_key} end`，故 `$host_key` 為空時**會殘留上一代舊 `host_key`**，而非省略欄位）；**`:651-680` 現況為 delete-then-relabel（`:677-680` 以供應商 CLI 刪除舊機），沒有保留 IP 機制**；**不包含通知外部 LLMGateway 主機**。`rotate-gateway.yml` 的 `dry_run` 不動 `NODE_GATEWAY`（L33）。 |
| `shared-configs/llmgateway/files/{llmgateway-tunnel,start}` | worker 容器端在 `MLP_LLMGATEWAY=1` 時讀 `/run/mlp-gateway/gateway.json` 自動跟隨輪換並轉發 `127.0.0.1:4710`。 |
