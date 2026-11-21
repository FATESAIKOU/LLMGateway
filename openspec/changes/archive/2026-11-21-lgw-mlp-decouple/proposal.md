對應工單：#221（b 級設計單；只寫 OpenSpec 文件，本人核准開工前不改程式、不實作）。上層：MyPMO#297 分流 E2、MyPMO#31「AiCompany 期 1」、MyPMO#4「啟動 LLMGateway」、MyPMO#469（決定檔 `decisions/2026-11-21-1500-manual.yml`）。相關：MyLinuxPool#1511（輪換流程帶隧道主機）、MyLinuxPool#864（worker port 互掛工具 `pool-portmap`）。

**四句白話總結**：
1. Gateway 輪換後，LLMGateway 會自己到 GitHub 抓新位址重連，連線健康時不碰 GitHub。
2. 代價：主機上要放一把唯讀 GitHub key（可讀 `FATESAIKOU/MyLinuxPool` 全部 variables）；而且在平穩無斷線期間若這把 key 悄悄到期，會等到下次輪換需要它的那一刻才 401 發現。
3. 兩條現有規矩要放寬：不放 token 檔、不依賴 `NODE_*`（只放寬到唯讀位址這一項）。
4. 風險：誰能改 MLP 的 `NODE_GATEWAY`，就能把 LLMGateway 的無認證 API 導向他的機器；設計用嚴格格式驗證擋住 ssh 注入，但這個信任邊界要你接受。

> **重要前提**：**本方案（G1＝A）只解決「輪換後位址與主機金鑰變動不再需要人工手動修改」與「服務生命週期綁死隧道」，不解決 `mlp-gateway` 整台掛掉的全域單點問題**。

## 已決定事項（本人 11-21）

> 來源：決定檔 MyPMO `decisions/2026-11-21-1500-manual.yml`（issue #221 留言／MyPMO#469），為本設計唯一裁定基準；`design.md` 與 `tasks.md` 皆直接依此單一路徑規劃，不再保留條件分岐。

- **G1＝A（只解輪換耦合與服務生命週期脫鉤）**：採用最小方案 M（擴充既有 `llmgwctl` 補啟停與隧道可選，並改造既有 `ops-scripts/llmgateway-tunnel` 維護連線與跟進輪換）。`mlp-gateway` 整台掛掉時仍拿不到金鑰。
  - **已排除選項（本人 11-21）**：**B1**（第二會合點）、**B2**（Mac 常備本地 fallback 替代方案）、**C**（全直連不經 `mlp-gateway`）一律排除——理由：本人裁定 A 以外的選項比 A 更不穩定，不考慮。
- **G2＝確定性事件驅動自 GitHub 抓 Gateway 位址（原 (3) 型之改進版）**：由 `llmgateway-tunnel`（同一個 `llmgateway-tunnel.service` unit）自己維護連線狀態——**不要以 AI 為前提、要確定性**：
  - **連線健康時不輪詢 GitHub**。
  - **發現連不上**（`ssh` 退出或 `ssh -W` 端對端探針連續失敗達門檻）時，才以唯讀 GitHub key 呼叫 GitHub API 抓取 MyLinuxPool repo 內的 `NODE_GATEWAY` 位址與 `host_key`，驗證通過後原子更新本機 follow 檔與私用 `known_hosts` 並重連；具指數退避與 API 速率保護，抓取失敗時保留最後已知位址持續重試。
  - **輸入只有兩組**：① 登入用 private key 路徑與登入資訊（沿用 `IDENTITY` 與本機登入使用者〔`RENDEZVOUS` 之 `user@` 或 `TUNNEL_USER`〕，並以初始 `RENDEZVOUS`／`KNOWN_HOSTS` 作為最後已知位址基準）；② MLP repo 內 Gateway 位址的路徑（`FATESAIKOU/MyLinuxPool` 的 Actions variable `NODE_GATEWAY`）與讀取用 GitHub key（檔案路徑，不寫值）。
  - **已排除選項（本人 11-21）**：(1a) 推至 `LLMGateway-keys`、(1b) 經通道直推主機、(2) 輪換保留原 IP 與主機金鑰。
- **G3＝B（保留 `4711`，不另開帶外進場）**：先不要另開不經跳板機的進場方式（太複雜）；管理用 SSH `4711` 一律保留（不支援 `REMOTE_SSH=off`，`REMOTE_SSH` 留空或設 `4711` 維持預設 `4711` 不動），管理 SSH 仍經跟隨後的同一條隧道（`4710`／`4711` 同一次連線）。

## 待本人核准事項

**要請本人核准的只有：這份設計可不可以開工？**（本設計誠實指出並提出修改以下兩條與既有規矩之衝突，以及必須接受的信任邊界風險：）

1. **放寬 `docs/deploy.md:36`「不放 token 檔」**：允許主機放置一把唯讀 GitHub fine-grained PAT 檔，只授權 `FATESAIKOU/MyLinuxPool` 單一 repo 的 **`Variables: Read-only`**（這是 GitHub 最小授權，但仍可讀該 repo **全部** Actions variables 拓樸，不含 Secrets；`LLMGateway-keys` 同步仍維持 `gh auth login`、不放 token 檔）。**明確限制**：平穩無斷線期間若該 key 悄悄到期，會等到下次輪換斷線需要它的那一刻才收 `401` 發現；實作提供手動 `llmgateway-tunnel --fetch-check` 與 `TOKEN_EXPIRES_AT` 提醒降低風險（存放權限 `0600`、`LoadCredential` drop-in 與標頭細節見 `design.md` §4.5）。
2. **放寬主 spec `connectivity`「MUST NOT 依賴 `NODE_*`」**：將 `openspec/specs/connectivity/spec.md` 之禁令明確放寬為「僅允許服務端隧道守護在連線失敗時唯讀讀取 MyLinuxPool `NODE_GATEWAY` 之位址相關欄位（`ip`、`port`、`host_key`、`generation`）並比對 `tunnel_user`，其餘 `pool-tunnel` 與其他 `NODE_*` 設定仍禁止依賴」，並於歸檔時同步更新主 spec `## Purpose`。
3. **接受信任方向、拓樸可見性與格式注入風險（附緩解）**：因 `lgwd` 無應用層認證且位址與 `host_key` 同源來自 `NODE_GATEWAY`，誰能寫入 MLP `NODE_GATEWAY` 就能將無 auth 隧道重導至其指定主機；且 `Variables: Read-only` 可讀該 repo 全部 variables 拓樸。此外，**若不做格式驗證，`NODE_GATEWAY` 欄位可透過 `ssh` 選項（如 `-oProxyCommand=…`）或 `known_hosts` 注入升級為服務主機 `llmgw` 帳號之任意指令執行**。緩解措施：**SSH 使用者以本機設定為準**（`NODE_GATEWAY.tunnel_user` 僅作一致性比對）、嚴格格式白名單（無 zone id 之正規化字面 IP／`\Z` 結尾嚴格主機名、非布林整數埠、單行兩欄完整 wire-format base64 `host_key`）與 `-l`／`--` 參數隔離、`host_key` 必填且 fail-closed（絕不退回 `accept-new`）、雙水位 `generation` 單調防回退、變更事件記至 log（不含 IP／金鑰）與 `llmgwctl status` 顯示已驗證 `generation`。

## 給 MLP PM 的問題與 PM 預設

### 給 MLP PM 的問題（不影響本人裁定方向，開工 P2／上線前須對齊）

1. **Q-MLP-A（`NODE_GATEWAY` 存放路徑、格式與讀取權限確認）**：本設計已從 MLP repo 唯讀查證 `NODE_GATEWAY` 存於 `FATESAIKOU/MyLinuxPool` 之 GitHub Actions repository variable（`GET /repos/FATESAIKOU/MyLinuxPool/actions/variables/NODE_GATEWAY`），內層 JSON 含 `ip`、`port`、`tunnel_user`、`host_key`、`generation`、`rotated_at`。請確認：(1) 線上 `NODE_GATEWAY` 是否固定包含以上全部欄位（確認 `port` 鍵名與 `generation` 型別）？(2) 以 fine-grained PAT 僅授權 `MyLinuxPool` 之 `Variables: Read-only` 是否確定可讀取該 endpoint？短期是否計畫搬移存放路徑？
2. **Q-MLP-B（`host_key` 必為本代）**：現行 `rotate-gateway.sh:581-582` 在 `$host_key == ""` 時會保留 `$existing` 的舊 `host_key`，導致新一代 `NODE_GATEWAY` 殘留上一代舊金鑰、使隧道進入比對失敗重試迴圈。請問 MLP 能否保證每次輪換都寫入**新機** `host_key`（或在取不到新機 `host_key` 時直接讓輪換失敗）？
3. **Q-MLP-C（輪換寫入時序與 `4710`/`4711` 保留）**：請確認真實輪換時會在「新機就位並驗證可連後、刪除舊機之前」更新 `NODE_GATEWAY`（使 LLMGateway 舊機斷線後即能抓到新位址），且 `NODE_GATEWAY.ports.llmgateway` 持續保留 `[4710, 4711]`（配合 G3＝B）。
4. **Q-MLP-D（`TUNNEL_EXTRA_PUBKEYS` 契約）**：確認長期保留 `TUNNEL_EXTRA_PUBKEYS`（`docs/KEY-DESIGN.md:55`）作為外部隧道撥入 `sshproxy` 的白名單契約與撤換流程。
5. **Q-MLP-E（Mac 端接入與 `MLP#1511`）**：MLP 是否計畫提供基於 `NODE_GATEWAY` 的 Mac 轉發指令（取代 `docs/deploy.md` §4.1 手動 `known_hosts_rendezvous`）？`MLP#1511` 人工照貼步驟是否於 #221 上線後以作廢關閉？
6. **Q-MLP-F（輪換回退與重試禁止降低或重用 `generation`）**：現行 `rotate-gateway.yml:539` 回退會寫回較低之 `$old_json` generation，再重試 `:461` 又重用 `old + 1`（同代號配不同新機），會被 LLMGateway 單調防線永久拒收而卡死在已刪機。請問 MLP 能否保證回退與重試時 `generation` 永遠嚴格遞增（絕不寫回較低或重用已發布代號）？（另建議可評估提供「只升 `generation` 不換機」的低風險預演模式供切換測試。）

> **PM 預設（本人不反對即照做）**：擴充既有 `llmgwctl`（含 `follow-reset` 需 root 顯式重置）、新增人工觸發確定性檢查 `llmgateway-tunnel --fetch-check`（不背景輪詢）與手填 `TOKEN_EXPIRES_AT`（≤7 天 `todo` 提醒）、短期保留 `ops-scripts/mac-fallback` 獨立應急路徑、`MLP#1511` 暫緩並於 #221 上線後以作廢關閉。

---

## Why

1. **現況強耦合導致輪換必斷**：LLMGateway 的 `llmgateway-tunnel.service` 透過靜態 `/etc/llmgateway/tunnel.env` 寫死 `RENDEZVOUS`（位址與 SSH 埠）與 `KNOWN_HOSTS`（`~llmgw/.ssh/known_hosts_rendezvous`），以 `ssh -R` 將 `127.0.0.1:4710`（API）與 `127.0.0.1:4711`（管理 SSH）掛上 `<會合點>`。2026-11-20 MLP 輪換 Gateway 後位址與主機金鑰皆變更，主機端未自動跟進，造成所有 worker 約 40 分鐘無法開出新隊員，直到人工修改才恢復。
2. **服務生命週期與傳輸通道綁死**：`lgwctl`（`install`、`check`、`status`、`upgrade`、`rollback`、`units`、`release`）將 `llmgateway-tunnel.service`、`tunnel.env` 與遠端探針視為必選元件，缺乏 `start`／`stop`／`restart` 且無法在無 `<會合點>` 的純本地環境乾淨運作與檢查。

## What Changes

本設計採**單一確定性方案（G1＝A、G2＝事件驅動抓 GitHub、G3＝B）**：

- **服務本體啟停與可選隧道解耦（`llmgwctl` 擴充，P1）**：
  - 擴充既有 `llmgwctl`，補齊冪等之 `start`、`stop`、`restart` 子命令（複用 `lgwctl/units.py`），並新增需 root 之顯式重置子命令 `follow-reset`（預設清除 `.tunnel_generation`、`.tunnel_fetch_backoff` 與重置最近抓取狀態；另提供 **`follow-reset --fetch-state`** 只清 `.tunnel_fetch_state` 與 `.tunnel_fetch_backoff`，不動世代水位，供換好 token 後清除告警），絕不要求維運人員手動 `rm`。
  - 將 `llmgateway-tunnel.service`、`tunnel.env` 與遠端探針從 `lgwctl` 核心（`layout.py`、`release.py`、`units.py`、`install.py`、`check.py`、`status.py`、`probe.py`、`upgrade.py`）的「必選」改為「**可選**」：無隧道時純本地 `check`／`status` 通過；**機器上存在該 unit 時仍照常檢查**（除非 `--no-remote`）；`probe.remote()` 回應碼 `2` 改為中性「`無法探`」。
  - `layout.py` 新增 `tunnel_follow_state()`（**六態**：`unconfigured`、`ok`、`missing`、`invalid`、`regressed`〔明確與已驗證水位 `.tunnel_generation` 比對〕、`unreadable`；保留 `tunnel_config_state()` 簽名相容並以 `O_NOFOLLOW`＋大小上限安全讀取），並讀取無祕密最近抓取分類檔 `.tunnel_fetch_state`（`ok`／`regressed`／`invalid`／`hostkey_missing`／`auth_failed`／`rate_limited`／`unreachable`／`config_partial`／`credential_unreadable`）：使 `check` 在 follow 檔缺失／無效／回退或最近抓取被拒／`auth_failed` **且隧道未 `active`** 時報 `fail`（不降級 `todo`，提示處置與 `llmgwctl follow-reset`）、**在隧道仍 `active` 但 follow 檔異常或最近抓取為 `auth_failed`／`regressed`／`invalid`／`hostkey_missing`／`config_partial` 時回報 `todo`**（`auth_failed` 提示「token 可能失效，請跑 `llmgateway-tunnel --fetch-check`」及換好 token 後執行 `follow-reset --fetch-state`）、`credential_unreadable`／`TOKEN_EXPIRES_AT` 格式不合時回 `todo`、非 root 無權限讀 `0700` 目錄時降為 `todo` 且 `status` 顯示「`unreadable（需 root 或 llmgw 才能讀）`」（絕不誤報 `missing`）；`status` 顯示 `.tunnel_generation` 已驗證之 `generation` 與最近抓取分類，且 token 面向的真實 `auth_failed`（`401` 或無 rate-limit 標頭之 `403`）於隧道未 `active` 時由 `check` 報 `fail`（`status` 同步顯示；隧道運行中則 `check` 回 `todo`）；`TOKEN_EXPIRES_AT` 或合理到期標頭（≤ 抓取當下時間視為 `expiry_unknown`）於 ≤ 7 天回 `todo` 告警。
- **單一確定性隧道守護改造（`ops-scripts/llmgateway-tunnel`＋`llmgateway-tunnel.service`，P2）**：
  - **保留 `REMOTE_SSH=4711` 預設不變（G3＝B，不實作 `REMOTE_SSH=off`）**：管理 SSH 埠 `4711` 一律保留並隨隧道一同重連至新會合點。
  - **出貨 unit 不含 `LoadCredential` 並補齊安全加固**：`deploy/units/llmgateway-tunnel.service` 不寫 `LoadCredential=`（確保無 token 檔之純靜態／現役／全新機器均可啟動），改於 P4 真機放置 systemd drop-in，並支援 `$CREDENTIALS_DIRECTORY/gateway-token` 與 `GATEWAY_GITHUB_KEY_FILE` 雙來源讀取（無權限或讀不到當成 `.tunnel_fetch_state=credential_unreadable` 走 `exit 1`，非例外亦非 `78`）；同步補齊 `NoNewPrivileges=true`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/llmgateway`、`PrivateTmp=true`。
  - **兩組輸入與本機狀態管理**：
    - ① 登入用 private key 與本機登入資訊：沿用 `IDENTITY`、本機 SSH 使用者（`RENDEZVOUS` 之 `user@` 或 `TUNNEL_USER`）與初始 `RENDEZVOUS`／`KNOWN_HOSTS` 作為最後已知位址基準；
    - ② MLP 位址路徑與讀取用 GitHub key：`GATEWAY_SOURCE_REPO`（`FATESAIKOU/MyLinuxPool`）、`GATEWAY_SOURCE_PATH`（`actions/variables/NODE_GATEWAY`）、`GATEWAY_GITHUB_KEY_FILE`（權限 `0600` `root:root` 經 drop-in `LoadCredential` 傳遞，不寫值）、選填 `TOKEN_EXPIRES_AT` 與 `FOLLOW_FILE`（預設 `/var/lib/llmgateway/gateway.json`）。半套 GitHub 設定於靜態有效時不停機，改記 `config_partial` 告警。
  - **連線健康時不輪詢；連不上才同行程抓取、嚴格驗格式並重連**：僅當 `ssh` 退出、`ssh -W` 探針連續失敗達門檻或首次無可用位址時，在**同一行程內**以 `urllib.request`（逾時 10s、上限 64 KiB、禁跨主機轉址、僅限 `https://api.github.com`；`LGW_GATEWAY_API_BASE` 限顯式測試旗標且 loopback，否則 `exit 78`）抓 `NODE_GATEWAY`：嚴格驗證 `isinstance(v, str)` 之無 zone id 字面 IP（`ipaddress.ip_address(v)` 且 `scope_id is None`，統一使用正規化 `str(addr)` 組裝 `known_hosts` 與 `ssh` 主機）／`\Z` 結尾 `re.fullmatch` 嚴格主機名、非布林整數 `port`（本機 `FOLLOW_FILE` 寫為 `ssh_port`）、`tunnel_user` 與本機設定一致（只比對不採用遠端字串）、單行兩欄完整 wire-format 與 base64 往返合法之 `host_key` 及雙水位 `generation` 單調檢核（區分 `applied`＝`FOLLOW_FILE` 與 `verified`＝`.tunnel_generation` JSON，支援「回到已驗證世代」與「同代未換代不累加不重建」），原子更新 `FOLLOW_FILE` 與私用 `.tunnel_known_hosts` 後**同行程**以 `-o ConnectTimeout=10 -l <user> -- <host>` 重連（SSH 選項永不接受 `-` 開頭資料），通過首次探針 200 後原子寫入 `.tunnel_generation` JSON（`generation`＋連線欄位指紋）並歸零退避；另提供 `--fetch-check` 人工一次性檢查（不寫狀態檔、不洩漏，成功時印出提示「如 check 仍紅請執行 `llmgwctl follow-reset --fetch-state`」）。
  - **退避、速率保護、fail-closed 與最後已知位址保底**：僅請求失敗或被拒才累加 `consecutive_failures`（`new_gen == applied.gen` 且欄位不變時僅設短節流而不累加失敗次數、不重建）；保留最後已知位址並寫入 `.tunnel_fetch_state`，回 `exit 1` 重試；僅必要設定全缺或未授權 API base 回 `78`。
  - **Token 到期中繼記錄**：抓取回應含 `github-authentication-token-expiration` 標頭時僅記錄到期日（不含 token）至 `/var/lib/llmgateway/.tunnel_token_meta`（日期 ≤ 抓取時間視為 `expiry_unknown`）。
- **文件、規矩修訂與 OpenSpec 規格更新（P3）**：
  - 更新 `docs/deploy.md`（納入唯讀 GitHub key 檔例外、drop-in `LoadCredential`、repo 全 variables 可見性與平穩期悄然到期限制、`--fetch-check`、`TOKEN_EXPIRES_AT`、`follow-reset`／`follow-reset --fetch-state`；保留 `4711` 相關維運流程）。
  - **新增**過渡隧道切換保險子命令 `llmgwctl tunnel-apply`（含 `--confirm` 與 transient timer `lgw-tunnel-revert`，非遷移用之 `lgw-cutover`／`lgw-revert`）列為 P4.3 **必用**前置；流程為「備份 → 排 `lgw-tunnel-revert` 保險（失敗即中止）→ 複製套用暫存新檔 → 重啟 → 探針（`rc=2` 視為失敗）」，`--revert-after` 預設 `10m`（細節見 design §5.2、tasks 3.2／C-10）。
  - 僅更新既有 `connectivity` delta spec（不新增 `deploy-ops`）：新增服務無隧道獨立生命週期條款（含 `follow-reset`、最近抓取分類、`401`-only `fail` 與運行中告警）、改寫可選隧道守護與會合點條款（放寬為僅允許服務端斷線恢復時唯讀讀取 `NODE_GATEWAY` 位址欄位與比對 `tunnel_user`），並於歸檔時同步更新主 spec `## Purpose` 與版本註記（`v6`）。**逐字保留** `客戶端自備通道` 之「`LLMGateway SHALL NOT 提供連線程式`」與 Mac／手機自行轉發規範。

## What Does Not Change

- **本 change 不改任何程式、測試或政策檔**：`lgwd/`、`lgw/`、`lgwctl/`、`ops-scripts/`、`deploy/`、`tests/`、`policy/` 在本設計單階段一律不動；實作依 `tasks.md` 拆成 PR 並在本人核准後執行。
- **不改動 `lgwd` HTTP API 與無應用層認證邊界**：不改端點路徑或回應欄位，不 bump `schema` 或 `env_schema`；`lgwd` 仍只綁 `127.0.0.1`。
- **不做 G1 排除項與 G3 另開進場**：不建第二會合點（B1）、不做直連改網格（C）、不在本單整併 `ops-scripts/mac-fallback`（B2）、不另開不經跳板機的進場方式、不支援 `REMOTE_SSH=off` 關 `4711`、不新增 `lgw-link-mlp` 獨立 daemon 與新 unit、不實作常駐 30 秒 GitHub API 輪詢、不新增 `lgw-service` 非 systemd 模式、不新增 `deploy-ops` capability。
- **不改動 `lgw/client.py` 通訊介面**：呼叫方仍傳入 `base_url`（預設 `http://127.0.0.1:4710`），斷線仍回報 `unreachable`。
- **不修改 MyLinuxPool 倉庫檔案**：跨 repo 協調只列為「給 MLP PM 的問題」與前置條件，不在本 change 代改 MLP。

## Impact

- **受影響的 spec**：僅 `openspec/specs/connectivity/spec.md`（ADDED／RENAMED／MODIFIED；歸檔時同步更新 `## Purpose` 與版本註記 `v6`）。
- **受影響的未來程式與文件（本 change 不改）**：
  - P1：`lgwctl/{cli,layout,release,units,install,check,status,probe,upgrade}.py`（`lgwctl/migrate.py` 採不動核心邏輯／保留常數與 `tunnel_config_state` 接口）；`tests/` 相關 8 支 `lgwctl`/`install`/`bootstrap` 測試檔。
  - P2：`ops-scripts/llmgateway-tunnel`、`deploy/units/llmgateway-tunnel.service`、`tests/test_tunnel.py`。
  - P3：`docs/deploy.md`、新增過渡隧道切換保險 `llmgwctl tunnel-apply`（`lgw-tunnel-revert.timer`）、`openspec/specs/connectivity/spec.md` 歸檔同步。
- **對 worker 與 Mac 客戶端的影響**：維持掛載 `<會合點>:127.0.0.1:4710` 時，worker `shared-configs/llmgateway` 與 sidecar **零改動**；Mac 客戶端仍維持自備通道（建議 MLP 補基於 `NODE_GATEWAY` 的動態轉發指令，否則仍依現行應急模式）。
- **對 MLP#1511 與 MLP#864 的影響**：#1511 人工照貼步驟於本案上線後作廢，但 `TUNNEL_EXTRA_PUBKEYS` 與 `NODE_GATEWAY` 契約仍需保留；#864 `pool-portmap` 因僅服務已註冊 `NODE_*` 節點且限定 10000–11999／5220–5399 埠，不能直接作為本案底層。
