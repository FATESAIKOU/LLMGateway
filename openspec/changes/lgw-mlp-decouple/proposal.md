對應工單：#221（b 級設計單；只寫 OpenSpec 文件，本人核准前不改程式、不實作）。上層：MyPMO#297 分流 E2、MyPMO#31「AiCompany 期 1」、MyPMO#4「啟動 LLMGateway」。相關：MyLinuxPool#1511（輪換流程帶隧道主機）、MyLinuxPool#864（worker port 互掛工具 `pool-portmap`）。

> **重要前提**：**預設最小方案（M）只解決「輪換後位址與主機金鑰變動不再需要人工手動修改」與「服務生命週期綁死隧道」，不解決 `mlp-gateway` 整台掛掉的全域單點問題**。

## 給本人的 3 題（決策清單，單一正本）

> 以下是本設計唯一需要本人裁定的 3 個問題（`design.md` 與 `tasks.md` 僅指向此處，不另存副本）；其餘架構細節已由 PM 設為預設（見下方「PM 預設」），本人不反對即照做。

- **Q1（＝U-0）你要這張單解哪個痛點？**
  - **(A) 只解「輪換後 40 分鐘開不了隊員」**（預設最小方案 M：擴充現有 `llmgwctl` 補啟停與隧道可選，並改造現有 `ops-scripts/llmgateway-tunnel` 讀取本機動態設定檔 `gateway.json`）。`mlp-gateway` 掛掉時仍拿不到金鑰。成本最低。**← 建議**
  - **(B) A＋第二條路徑，讓 `mlp-gateway` 掛掉時仍拿得到金鑰**：**(B1)** 第二會合點（解全域；需多養一台主機，並另外再做一輪設計以支援多筆備援位址）、**(B2)** Mac 常備本地 fallback（只解 Mac，不解容器 worker）。
  - **(C) 連傳輸都不經 `mlp-gateway`（直連）**。需補 TLS＋應用層認證、公網暴露與全池容器網路改造。不建議。
- **Q2（＝U-3）跳板機換機器後，新的位址與主機指紋怎麼送到 LLMGateway 主機？**（先看 Q1；若 Q1 選 B1 須再補多筆格式）
  - **(1a) MLP 輪換後上傳到金鑰庫（`LLMGateway-keys`），由主機定時抓回來**：主機不新增登入憑證；**代價**：主機端要新寫一個抓檔程式，且 MLP 輪換自動化須多配一把能寫入該金鑰庫的 secret（寫金鑰庫權限大，誤寫或外洩會卡住金鑰更新並能改寫隧道目的地）。
  - **(1b) MLP 輪換時從既有管理通道直接把檔推到 LLMGateway 主機**：主機端不寫抓檔程式；**代價**：推檔端要持有一把能 SSH 登入 LLMGateway 主機的私鑰（主機須新增授權公鑰，權限等同開出 shell 登入），且必須嚴格確保「新機就位→經舊通道推檔→才刪舊機」的順序（該順序目前尚未驗證）。
  - **(2) MLP 輪換時保留原 IP 與主機金鑰**：主機端**不用寫抓檔程式、也不用改隧道追蹤輪換的程式（P2-B 取消）**，但**仍須升級 `llmgateway-tunnel` 以支援用 `REMOTE_SSH=off` 關閉 `4711`（P2-A 必做）**；**代價**：MLP 輪換時要保證新機還是同一個 IP、同一把主機私鑰（須先把舊機主機私鑰複製給新機，依賴雲端供應商固定 IP 支援，且無法藉輪換換掉主機金鑰），須先向 MLP PM 確認可行。
  - **(3) 主機定時直接向 MLP 讀節點狀態（`NODE_GATEWAY`）**：**不建議**（須在主機放一支 GitHub 讀取 token 檔，違反現有不放 token 檔原則，且須放寬現行規格禁令）。
  - **單一建議**：**授權 PM 先問 MLP PM（輪換能否保留同一 IP 與主機私鑰）；(2) 可行就採 (2)，不行就採 (1a)**。（本人只需回覆「授權」或「不授權」，或另行指定選項。）
- **Q3（＝U-1 事實題）這台主機有沒有不經 `<會合點>:4711` 的進場方式（雲端 Web Console／本機終端／區網 SSH）？**（有／沒有／不確定）
  - 沒有或不確定時：切換當次一律保留 `4711`（設計已預設）；有且驗收確認可用後，才由本人決定是否關閉 `4711`。**只有本人答得出。**

> **PM 預設（本人不反對即照做，不佔選擇題）**：採用最小方案 M（擴充既有 `llmgwctl`，不新造 `lgw-service` 與雙 unit 狀態機；原 U-2）、短期保留 `ops-scripts/mac-fallback` 獨立應急路徑（原 U-4）、`MLP#1511` 暫緩並於 #221 上線後以作廢關閉（原 U-5）。

---

## Why

1. **現況強耦合導致輪換必斷**：LLMGateway 的 `llmgateway-tunnel.service` 透過靜態 `/etc/llmgateway/tunnel.env` 寫死 `RENDEZVOUS`（位址與 SSH 埠）與 `KNOWN_HOSTS`（`~llmgw/.ssh/known_hosts_rendezvous`），以 `ssh -R` 將 `127.0.0.1:4710`（API）與 `127.0.0.1:4711`（管理 SSH）掛上 `<會合點>`。2026-11-20 MLP 輪換 Gateway 後位址與主機金鑰皆變更，主機端未自動跟進，造成所有 worker 約 40 分鐘無法開出新隊員，直到人工修改才恢復。
2. **服務生命週期與傳輸通道綁死**：`lgwctl`（`install`、`check`、`status`、`upgrade`、`rollback`、`units`、`release`）將 `llmgateway-tunnel.service`、`tunnel.env` 與遠端探針視為必選元件，缺乏 `start`／`stop`／`restart` 且無法在無 `<會合點>` 的純本地環境乾淨運作與檢查。
3. **`mlp-gateway` 單點相依風險**：當 `<會合點>` 整台故障時，經 `<會合點>` 轉發的金鑰存取仍會中斷；由 Q1（U-0）讓本人裁定本次是要先解「設定與輪換耦合」，還是連傳輸備援／第二會合點一併納入。

## What Changes

本設計採**最小可行主方案「M」**（完整版僅於 `design.md` 比較表作為方案「F」參考，不寫入 delta spec 與 `tasks.md`）：

- **服務本體啟停與可選隧道解耦（`llmgwctl` 擴充）**：
  - 擴充既有 `llmgwctl`，補齊冪等之 `start`、`stop`、`restart` 子命令（複用 `lgwctl/units.py`）。
  - 將 `llmgateway-tunnel.service`、`tunnel.env` 與遠端探針從 `lgwctl` 核心（`layout.py`、`release.py`、`units.py`、`install.py`、`check.py`、`status.py`、`probe.py`、`upgrade.py`）的「必選」改為「**可選**」：無隧道時純本地 `check`／`status` 通過；**機器上存在該 unit 時仍照常檢查**（除非 `--no-remote`）；`probe.remote()` 回應碼 `2` 改為中性「無法探」。
  - **（依存 P2-B，僅 Q2＝(1)/(3) 實作；Q2＝(2) 跳過）**：`layout.py` 新增 `tunnel_follow_state()`（支援四個異常／狀態：`ok`、`missing`、`invalid`、`regressed`，另含非 root 無權限讀取時的 `unreadable` 與未配置 `unconfigured`；保留 `tunnel_config_state()` 簽名相容），使 `check` 在 follow 檔缺失／無效／回退時報 `fail`（不降級 `todo`）、非 root 無權限時降為 `todo` 且 `status` 顯示「無權限（需 root 或 llmgw 才能讀）」（絕不誤報 `missing`）。
- **最小隧道改造（拆為 P2-A 必做與 P2-B 條件式 follow 檔）**：
  - **P2-A（必做，與 Q2 無關）**：改造 `ops-scripts/llmgateway-tunnel` 的 `REMOTE_SSH` 解析——保留留空＝預設 `4711` 語意，並支援顯式 `REMOTE_SSH=off`／`none`（大小寫不敏感）關閉管理 SSH 埠轉發，消除舊腳本 `int("off")` 觸發 exit `78` 永久停擺之缺陷。
  - **P2-B（僅 Q2＝(1)/(3) 執行；Q2＝(2) 取消）**：支援設定鍵 `FOLLOW_FILE`（優先序高於靜態 `RENDEZVOUS`／`KNOWN_HOSTS`），讀取 `gateway.json` 形狀的本機 follow 檔（欄位：`ip`、`ssh_port`、`tunnel_user`、`host_key`、`generation`、`published_at`），自動以 `[<ip>]:<ssh_port>`（`22` 埠為 `<ip>`）原子寫入私用 `known_hosts`（`/var/lib/llmgateway/.tunnel_known_hosts`），採 `StrictHostKeyChecking=yes`；`--probe-once` 嚴格只讀不寫該檔。
  - **退出碼、運行中故障處置與 fail-closed 語意（P2-B）**：僅靜態設定鍵缺失（`IDENTITY` 缺失，或 `FOLLOW_FILE` 與 `RENDEZVOUS` 皆未設定）才回 exit `78`（停止重試）；follow 檔缺失／壞掉／`host_key` 缺漏／`generation` 回退（獨立事件 `follow_regressed`）或同代篡改時記事件並回 **exit `1`**（由 systemd 每 10 秒自動重試，檔案修復即自癒），**絕不退回 `accept-new` 亦不退回靜態鍵**；watch 迴圈中若已有健康連線而檔案暫時寫壞／缺失，記錄事件但不打斷健康連線，待下次斷線重連才 fail-closed。本機 follow 檔更新後預計恢復時間（RTO）**≤ 2–3 分鐘**（若採 S-1a 既有 keysync 10 分鐘節奏，端到端最壞約 13 分鐘）。
- **管理 SSH（4711）與無空窗切換**：
  - 切換沿用同一 unit 重啟（`llmgwctl upgrade` ＋ 放好 follow 檔〔若做 P2-B〕＋ 觀察期保留舊靜態鍵供 `llmgwctl rollback` 相容 ＋ `systemctl restart llmgateway-tunnel`）；若需反向保險，復用 `lgwctl/migrate.py` 既有的 `lgw-cutover`／`lgw-revert` transient timer 機制（等待上限 ≥ 90 秒，窗口內勿重開機）。
  - **`REMOTE_SSH=off` 與回滾順序**：`REMOTE_SSH=off` 與清理舊靜態鍵同時才算觀察期結束；若觀察期內設了 `REMOTE_SSH=off` 後須執行 `llmgwctl rollback` 回退舊版腳本，**必須先將 `REMOTE_SSH` 改回 `4711`（或留空）**，否則舊腳本不認 `off` 會以 exit `78` 停擺。
- **OpenSpec 規格更新**：
  - 僅更新既有 `connectivity` delta spec（不新增 `deploy-ops`）：新增服務無隧道獨立生命週期條款、改寫可選隧道守護與探針／會合點條款；**逐字保留** `客戶端自備通道` 之「`LLMGateway SHALL NOT 提供連線程式`」與 Mac／手機自行轉發規範；**保留** `會合點與遠端埠` 對 `MyLinuxPool` `pool-tunnel`／`NODE_*` 的禁令（並註明若 Q2 選 (3) 拉取 `NODE_GATEWAY` 須同步放寬該條禁令）。**follow 檔相關條款與 Scenario 僅於 Q2＝(1)/(3) 時歸檔；若 Q2＝(2)，歸檔前須依 `tasks.md` 3.3 刪除 follow 檔條款**，並於歸檔時同步更新主 spec `Purpose` 與版本註記。

## What Does Not Change

- **本 change 不改任何程式、測試或政策檔**：`lgwd/`、`lgw/`、`lgwctl/`、`ops-scripts/`、`deploy/`、`tests/`、`policy/` 在本設計單階段一律不動；實作依 `tasks.md` 拆成 PR 並在本人核准後執行。
- **不改動 `lgwd` HTTP API 與無應用層認證邊界**：不改端點路徑或回應欄位，不 bump `schema` 或 `env_schema`；`lgwd` 仍只綁 `127.0.0.1`。
- **不替本人拍板 Q1／Q2／Q3**：Q1（脫鉤層級 U-0）、Q2（follow 檔來源 U-3）、Q3（帶外進場現況 U-1）待本人／PMO 裁定後再進入對應 PR；若 Q1 選 (B1) 第二會合點，實作前須另補一輪設計以支援多筆備援位址；若 Q2 選 (2) 保留 IP＋主機金鑰，則取消 P2-B（與 P2b、P1 的 `FOLLOW_FILE` 認知），但 **P2-A（`REMOTE_SSH=off`）仍必做**。
- **不在主方案 M 實作方案 F 的擴張項**：不新增 `lgw-link-mlp` 獨立 daemon 與新 unit、不實作 30 秒 GitHub API 輪詢、不新增 `lgw-service` 薄入口與非 systemd pidfile 模式、不整併 `ops-scripts/mac-fallback`、不另造 `lgw-decouple-*` 切換狀態機、不新增 `deploy-ops` capability；亦**不把 narrow PAT 當作預設來源**。
- **不改動 `lgw/client.py` 通訊介面**：呼叫方仍傳入 `base_url`（預設 `http://127.0.0.1:4710`），斷線仍回報 `unreachable`。
- **不修改 MyLinuxPool 倉庫檔案**：跨 repo 協調只列為「給 MLP PM 的問題」與前置條件，不在本 change 代改 MLP。

## Impact

- **受影響的 spec**：僅 `openspec/specs/connectivity/spec.md`（ADDED／RENAMED／MODIFIED；歸檔時同步更新 `Purpose` 與版本註記）。
- **受影響的未來程式與文件（本 change 不改）**：
  - P1：`lgwctl/{cli,layout,release,units,install,check,status,probe,upgrade}.py`（`lgwctl/migrate.py` 採不動核心邏輯／保留常數與 `tunnel_config_state` 接口；`FOLLOW_FILE` 四態認知依存 P2-B）；`tests/` 相關 9 支測試檔。
  - P2-A（必做）：`ops-scripts/llmgateway-tunnel` 之 `REMOTE_SSH=off`/`none` 解析與 `tests/test_tunnel.py`。
  - P2-B（僅 Q2＝(1)/(3) 執行；Q2＝(2) 取消）：`ops-scripts/llmgateway-tunnel` 之 `FOLLOW_FILE` 支援、`docs/deploy.md`；若 Q2 選 (1a) 寫入 `LLMGateway-keys` 且納入本單，另有 P2b 取檔器。
- **對 worker 與 Mac 客戶端的影響**：維持掛載 `<會合點>:127.0.0.1:4710` 時，worker `shared-configs/llmgateway` 與 sidecar **零改動**；Mac 客戶端仍維持自備通道（建議 MLP 補動態轉發入口或依現行應急模式）。
- **對 MLP#1511 與 MLP#864 的影響**：詳見 `design.md` §6（#1511 人工照貼步驟於本案上線後作廢，但 `TUNNEL_EXTRA_PUBKEYS` 與 `NODE_GATEWAY` 契約仍需保留；#864 `pool-portmap` 因僅服務已註冊 `NODE_*` 節點且限定 10000–11999／5220–5399 埠，不能直接作為本案底層）。
