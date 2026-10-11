# LiteLLM PyPI 供應鏈事件查證：惡意版本、植入方式、官方修補與版本固定查核做法

> 範圍：只讀公開的官方文件與公開網頁；不碰憑證檔、不跑需要金鑰的 API。
> 日期基準：2026-10-11。查不到的項目以「截至 2026-10-11 查無」標示。
> 每個論點後附出處網址。

## 結論摘要

- 「LiteLLM 在 PyPI 上曾有被植入惡意程式的版本」屬實。受影響版本就是 `litellm==1.82.7` 與 `litellm==1.82.8`，2026-03-24 10:39 UTC 起上架，當天即被 PyPI 隔離／刪除。官方說法與 PyPI 事後報告、OSV 公告（PYSEC-2026-2）一致。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
  - https://osv.dev/vulnerability/PYSEC-2026-2
- 植入方式不是改 GitHub `main`，而是攻擊者（被稱為 TeamPCP）先經由 LiteLLM CI/CD 裡未固定的 Trivy 依賴拿到長效 `PYPI_PUBLISH` token，再繞過官方 CI/CD 直接上傳惡意 wheel/sdist 到 PyPI。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 官方修補：刪除惡意版、輪換 PyPI/GitHub/Docker 等金鑰並換維護者帳號、暫停發版、請 Mandiant 做鑑識、稽核 v1.78.0～v1.82.6 確認乾淨並公布 SHA-256 對照表、之後以新 CI/CD v2 發乾淨版 v1.83.0，並導入 PyPI Trusted Publisher、GHCR token 認證、Docker cosign 簽章（自 v1.83.0 起）。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://docs.litellm.ai/blog/security-townhall-updates
  - https://docs.litellm.ai/docs/proxy/docker_image_security
- 若要用 LiteLLM：pip 側用 `==` 固定版＋`--require-hashes`；但要知道 hashes 擋不住「用合法憑證發佈的惡意版」（本次 1.82.8 的 `.pth` 在 RECORD 內宣告正確、hash 也對，照樣通過檢查），所以必須疊加 PyPI provenance／attestation 查核、官方 Docker image 的 digest pin＋cosign 驗證、dependency cooldown 與 lock 檔。出處：
  - https://pip.pypa.io/en/stable/topics/secure-installs/
  - https://pip.pypa.io/en/stable/topics/repeatable-installs/
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - https://docs.pypi.org/attestations/consuming-attestations/
  - https://docs.litellm.ai/docs/proxy/docker_image_security

---

## 1. 哪些版本、何時

### 1.1 受影響版本：1.82.7、1.82.8；最後乾淨版是 1.82.6

- 官方確認受影響的就是 `litellm==1.82.7` 與 `litellm==1.82.8`，兩版皆已從 PyPI 移除。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
- OSV 公告編號為 PYSEC-2026-2（別名 MAL-2026-2144），受影響範圍記為 `Introduced 1.82.7`、`Last Affected 1.82.8`。出處：
  - https://osv.dev/vulnerability/PYSEC-2026-2
- Snyk 整理的受影響版本同為 1.82.7、1.82.8，安全版為 `<=1.82.6`。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 官方已稽核 v1.78.0～v1.82.6 的 PyPI 與 Docker 產物，逐版比對 Git commit 並掃 IoC，確認乾淨，SHA-256 對照表見官方公告「Verified safe versions」。例如 PyPI 1.82.6 對應 commit `38d477507dad`。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026

### 1.2 時間線（UTC，2026-03-24）

- 1.82.7 於 10:39 UTC 上傳；1.82.8 於約 10:52 UTC 上傳（相隔約 13 分鐘，屬 payload 迭代）。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - https://osv.dev/vulnerability/PYSEC-2026-2
- 隔離／刪除時間各來源數字略有出入，並陳如下（不自行調和）：
  - 官方說法：兩版上線約 40 分鐘後被 PyPI 隔離；到 16:00 UTC 時已與 PyPI 合作刪除。出處：
    - https://docs.litellm.ai/blog/security-update-march-2026
    - https://docs.litellm.ai/blog/security-townhall-updates
  - PyPI 事後報告：從上傳到隔離總曝險時間為 2 小時 32 分（上傳→首報 1h19m，首報→隔離 1h12m）；攻擊窗口內惡意版被下載超過 119k 次。出處：
    - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
  - Snyk 整理的時間表：約 13:38 UTC 隔離；15:27 UTC 確認惡意版已刪除、套件解除隔離。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 官方認定的使用者風險窗口是「3 月 24 日 10:39 UTC～16:00 UTC 之間 pip 安裝／升級、或在此窗口內構建且未固定版本的 Docker image、或經由未固定的傳遞依賴拉到此兩版」。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
- 觸發後續修補的乾淨版 v1.83.0 是 3 月 30 日更新公告的新版，由新 CI/CD v2 發佈。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026

### 1.3 下載量級（並陳，不調和）

- PyPI 報告：攻擊窗口內惡意版下載超過 119k 次；LiteLLM 平常約每週 1500～2000 萬次安裝，換算約每分鐘 1700 次；約 40～50% 安裝屬於未固定版、每次都抓最新版。出處：
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
- Snyk 寫 LiteLLM 約每日 340 萬次下載。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 被盜憑證是否已被實際利用：截至 2026-10-11 查無官方定論；Trend Micro 當時寫「截稿時無公開確認攻擊者是否在下架前成功利用竊得憑證」。出處：
  - https://www.trendmicro.com/en_gb/research/26/c/inside-litellm-supply-chain-compromise.html

---

## 2. 怎麼被植入

### 2.1 攻擊鏈：Trivy → CI 環境變數裡的長效 token → 直接上傳 PyPI

- 官方認定起因是 CI/CD 安全掃描用的 Trivy 依賴被污染。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://docs.litellm.ai/blog/security-townhall-updates
- 官方歸納三個促成因素：(1) 當時全跑在 CircleCI 且各步驟共用同一環境，爆炸半徑大；(2) PyPI/GHCR/Docker 等發佈憑證以靜態 secret 放在環境變數，遭污染的步驟讀得到長效憑證；(3) 安全掃描的 Trivy 未固定版本，跑到被污染的 Trivy 並讀走環境變數中的憑證。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
- Snyk 的還原：3 月 19 日攻擊者把 `trivy-action` 的 tag 改指到惡意版 v0.69.4；LiteLLM 的 CI 在未固定版本的情況下跑到它，`PYPI_PUBLISH` token 從 runner 環境被外傳；攻擊者於 3 月 24 日 10:39／10:52 直接發佈 1.82.7／1.82.8。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 官方強調：攻擊者繞過官方 CI/CD，直接上傳惡意包到 PyPI；GitHub 上的 `main` 沒有被推惡意碼。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://docs.litellm.ai/blog/security-townhall-updates
- 攻擊者身份第三方稱 TeamPCP（別名 PCPcat／Persy_PCP／ShellForce／DeadCatx3），屬同一波含 Trivy、KICS 的連續行動；LiteLLM 是其中一環。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/

### 2.2 兩版的植入手法差異

- v1.82.7：把 base64 惡意碼直接嵌進 `litellm/proxy/proxy_server.py`，在 `import litellm.proxy`（標準 proxy 啟動路徑）時執行。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://osv.dev/vulnerability/PYSEC-2026-2
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- v1.82.8：保留 `proxy_server.py` 植入，外加 `litellm_init.pth`（約 34,628 bytes）。`.pth` 首行 `import` 會被 CPython `site` 模組在直譯器啟動時 `exec`，因此任何 Python 啟動（含 `pip`、`python -c`、IDE language server）都會觸發，無需 `import litellm`。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://osv.dev/vulnerability/PYSEC-2026-2
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 1.82.8 的 `.pth` 有 fork-bomb 副作用：payload 會再開新 Python 子行程，而子行程啟動又觸發 `.pth`，導致發現者機器 RAM 耗盡、無回應；屬非預期副作用而非刻意設計。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/

### 2.3 惡意程式行為（三階段）

- 第 1 階段搜刮：主機識別（hostname／env／process／route）、SSH 私鑰、`.env`、Git 憑證、shell history、Slack/Discord webhook、CI/CD 設定、`/etc/shadow`、AWS/GCP/Azure 憑證檔、IMDS（含 IMDSv2 簽名實作）直查、AWS Secrets Manager／SSM、Docker `config.json`、kubeconfig／service account token、資料庫密碼、多種加密錢包檔。出處：
  - https://osv.dev/vulnerability/PYSEC-2026-2
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - https://docs.litellm.ai/blog/security-update-march-2026
- 第 2 階段加密與外傳：以 `openssl rand` 產生 32-byte AES-256 session key，AES-256-CBC（PBKDF2）加密資料，再以硬編碼 4096-bit RSA 公鑰（OAEP）包裝 session key，打成 `tpcp.tar.gz` 經 `curl` POST 到 `https://models.litellm.cloud/`；該網域與官方 `litellm.ai` 無關，註冊於攻擊前一天（3 月 23 日）。暫存檔含 `session.key`、`payload.enc`、`session.key.enc`、`tpcp.tar.gz`。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - https://osv.dev/vulnerability/PYSEC-2026-2
  - https://docs.litellm.ai/blog/security-update-march-2026
- 第 3 階段持久化與橫向移動：寫 `~/.config/sysmon/sysmon.py`（root 則 `/root/.config/sysmon/sysmon.py`），建 systemd user service `~/.config/systemd/user/sysmon.service`（描述偽裝「System Telemetry Service」）並 enable；該腳本輪詢 `https://checkmarx.zone/raw`（官方後來補列的第二 IoC 網域，與 Checkmarx 公司無關），把回傳 URL 下載到 `/tmp/pglog`、chmod 755 執行，狀態記於 `/tmp/.pg_state`。若偵測到 K8s service account token，則讀全 namespace secret，並在 `kube-system` 以 `alpine:latest` 向每節點部署特權 pod（`node-setup-{node_name}`，container 名 `setup`，掛 host `/`、hostPID/hostNetwork），把後門裝到宿主機。出處：
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - https://osv.dev/vulnerability/PYSEC-2026-2
  - https://docs.litellm.ai/blog/security-update-march-2026
- 已知惡意檔 SHA-256（第三方整理；官方以「掃 IoC＋對 Git」稽核舊版，未逐一公布惡意版全文 hash，故此處引用第三方）：
  - `litellm_init.pth`（1.82.8）：`71e35aef03099cd1f2d6446734273025a163597de93912df321ef118bf135238`。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - `proxy_server.py`（1.82.7）：`a0d229be8efcb2f9135e2ad55ba275b76ddcfeb55fa4370e0a522a5bdee0120b`。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - `sysmon.py`：`6cf223aea68b0e8031ff68251e30b6017a0513fe152e235c26f248ba1e15c92a`。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/

---

## 3. 官方的事後說明與修補

以下皆為 LiteLLM 官方 blog（docs.litellm.ai，作者 Krrish Dholakia／Ishaan Jaffer）與 PyPI 官方 blog 的說法；第三方僅作補充。

### 3.1 誰不受影響

- 跑官方 LiteLLM Proxy Docker image（`ghcr.io/berriai/litellm`，requirements.txt 固定依賴、不走被污染的 PyPI 包）者不受影響；用 LiteLLM Cloud 者不受影響；停在 v1.82.6 以前且案發窗口沒升級者不受影響；從 GitHub source 安裝者不受影響（repo 未被污染）。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026

### 3.2 已做的緊急處置

- 下架惡意版；刪除／輪換所有受影響與相鄰 secret（含 PyPI、GitHub、Docker 相關憑證）；為謹慎起見連 maintainer 帳號都輪換（新帳號 `@krrish-berri-2`、`@ishaan-berri`）；找 Google Mandiant 協助鑑識 build 與發佈鏈；暫停所有新發版直到供應鏈複查完成。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://docs.litellm.ai/blog/security-townhall-updates
- 加固 CI：清掉約 6000 個 open branch 並加 auto-deletion；pin 住全部 GitHub Actions（CircleCI 依賴 pin 進行中）。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
- 鑑識結論（官方當時口徑）：未發現橫向移動進公司內網，事件侷限於 CI/CD 管線與該兩版的發佈路徑；`main` 無惡意碼；最近 20 版無 IoC（團隊手動驗＋Veria Labs 獨立 review）。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates

### 3.3 結構性修補（CI/CD v2 路線）

- 四原則：limit 各包可存取範圍、reduce 敏感 env 變數、avoid 被污染的包、prevent 發佈竄改；把管線拆成 unit／integration／security scan／release 四個隔離環境。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
- 短效憑證：PyPI 改 Trusted Publisher（GitHub Actions PR #24654）、GHCR 改 token-based authentication（PR #24683）。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
- 發佈可稽核：用 Cosign 簽 Docker image（PR #24683 起；自 `v1.83.0-nightly` 起所有 GHCR image 皆簽，同一把 key，key 引進於 commit `0112e53`，公鑰存於 repo `cosign.pub`）。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
  - https://docs.litellm.ai/docs/proxy/docker_image_security
- 避免吃到被污染的包：CI 內包與 action 固定到 SHA、能不用 `latest` 就不用；升級新版前加 cooldown；導入 zizmor 抓未固定依賴與憑證外洩。出處：
  - https://docs.litellm.ai/blog/security-townhall-updates
- PyPI 側證實兩專案（litellm、telnyx）事後都已採用 Trusted Publishers；並建議維護者另做：避免 `pull_request_target` 等不安全 trigger、輸入消毒、依賴 pin 到 commit SHA、可審查的發佈（如 GitHub Environments 審核）、全帳號 2FA。出處：
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/

### 3.4 中毒者處置（官方指引）

- 若裝過 1.82.7／1.82.8：把該環境當已淪陷。輪換該機上所有憑證（API key、雲 key、DB 密碼、SSH key、K8s token、env／設定檔內 secret）；查 `site-packages` 有無 `litellm_init.pth`（有就移除並鑑識）；稽核本地／CI／Docker build／部署 log 是否裝過此兩版；固定到已知安全版（v1.82.6 以前或之後經驗證的新版）。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
- 官方另提供掃 CI 的社群腳本（GitHub Actions／GitLab CI log 掃 `litellm 1.82.7/1.82.8`），原文提醒使用前自行審查。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026
- 聯繫窗口：`security@berri.ai`、`support@berri.ai`、Slack、X @LiteLLM。出處：
  - https://docs.litellm.ai/blog/security-update-march-2026

---

## 4. 若要用 LiteLLM：固定版本與查核做法

### 4.1 pip：`==` 固定版＋`--require-hashes`

- 先用 `==` 把版號釘死；`pip freeze` 只能記版號，不含 hash，不算 lock（無 hash 就不算安全可重現）。出處：
  - https://pip.pypa.io/en/stable/topics/repeatable-installs/
- 再對每個（含傳遞依賴）加上 `--hash=sha256:…`。官方示例格式為 `FooProject == 1.2 --hash=sha256:…`。出處：
  - https://pip.pypa.io/en/stable/topics/repeatable-installs/
  - https://pip.pypa.io/en/stable/topics/secure-installs/
- 用 `pip hash` 取檔 hash；用 `pip-compile --generate-hashes` 產生帶 hash 的 lock；安裝時加 `--require-hashes` 強制全檔檢查，並可加 `--only-binary :all:` 拒裝 sdist。出處：
  - https://pip.pypa.io/en/stable/topics/secure-installs/
- 重要限制（pip 官方明說）：hash-checking 是 all-or-nothing；任一 requirement 帶 `--hash` 即全域啟動，未列 hash 的依賴會直接報錯；requirements 必須被 pin（URL／路徑或 `==`）；建議 sha256，排除 md5/sha1/sha224。出處：
  - https://pip.pypa.io/en/stable/topics/secure-installs/
- 對本次事件的關鍵限制（Snyk 明說，必讀）：
  - `--require-hashes` 只能證明「抓到的檔＝PyPI 當時宣稱的檔」，不能證明「PyPI 宣稱的內容本身是否惡意」。1.82.8 的 `litellm_init.pth` 在 wheel `RECORD` 內正確宣告且 hash 相符，用合法憑證發佈，因此 `pip install --require-hashes` 照樣通過，標準完整性檢查全過。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
  - 唯一安裝期偵測路徑是檢查「該包是否裝 `.pth`、內容有無 `subprocess`／`base64`／`exec` 模式」；當時無廣泛部署的 pip 外掛自動做這件事。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- 實務建議（綜合官方文件推導）：
  - `requirements.txt` 內 `litellm==<驗證過的乾淨版>`＋全依賴 hash，並在部署腳本以 `--require-hashes` 強制；升級只接受明確審過的新 hash。出處（做法來源）：
    - https://pip.pypa.io/en/stable/topics/secure-installs/
  - 另用 lock 工具（`uv lock`、`pip-compile --generate-hashes`、`pipenv`；pip 實驗性 `pip lock` 產 `pylock.toml`）取代裸 `pip install litellm`／`pip freeze`。出處：
    - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
  - 事件當時 Snyk 建議先釘 `litellm<=1.82.6`；今日選版應以官方「Verified safe versions」與 v1.83.0+ 為準。出處：
    - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
    - https://docs.litellm.ai/blog/security-update-march-2026

### 4.2 官方簽章／provenance（PyPI Trusted Publisher＋attestations；Docker 用 cosign）

PyPI 側（通用機制，PyPI 官方文件）：

- 不要用長效 API token 發佈，改用 Trusted Publisher（OIDC）：CI 向 PyPI 換只活 15 分鐘的短效 token；被偷也難長期重用。出處：
  - https://docs.pypi.org/trusted-publishers/
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
- Trusted Publisher 的附帶好處是 Digital Attestations（PEP 740）：可驗「該檔確實經由特定 Trusted Publisher 發佈」，下游能發現「某版沒走平常發佈流程」就提高警覺。出處：
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
  - https://docs.pypi.org/attestations/
  - https://peps.python.org/pep-0740/
- 取用與驗證：PyPI 經 Simple index（HTML）與 Simple JSON API 暴露各檔 provenance；Integrity API（`GET /integrity/<project>/<release>/<file>/provenance`）取 provenance 物件；用 `pypi-attestations verify pypi --repository https://github.com/<org>/<repo> <wheel直鏈>` 做人工驗證。出處：
  - https://docs.pypi.org/attestations/consuming-attestations/
  - https://docs.pypi.org/api/integrity/
- LiteLLM 在本案的對應狀態：
  - PyPI 官方證實 litellm 事後已採用 Trusted Publishers。出處：
    - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
  - LiteLLM 官方 townhall 列出 PyPI Trusted Publisher（GitHub Actions PR #24654）與 GHCR token 認證（PR #24683）為已著手項目。出處：
    - https://docs.litellm.ai/blog/security-townhall-updates
  - 截至 2026-10-11 查無：本研究未逐檔呼叫 Integrity API，故「你現在要裝的 litellm 某版某檔當下有無 provenance、簽章身份是否等於 BerriAI/litellm 的預期 workflow」無定論；要用時請現查（按上一條命令驗），不要沿用本檔的結論。

Docker／GHCR 側（LiteLLM 官方文件，有明確簽章機制）：

- 自 `v1.83.0`（含 `v1.83.0-nightly` 起）所有發到 `ghcr.io/berriai/` 的 image 皆以同一把 cosign key 簽署；key 引進於 commit `0112e53`，公鑰在 repo `cosign.pub`；涵蓋 `litellm`、`litellm-non_root`、`litellm-spend_logs`、`litellm-database`（後者為 legacy alias）。出處：
  - https://docs.litellm.ai/docs/proxy/docker_image_security
  - https://docs.litellm.ai/blog/security-townhall-updates
- PyPI GPG 簽章：截至 2026-10-11 查無 LiteLLM 為 wheel/sdist 提供可驗 GPG 簽章的官方說明；PyPI 正典方向是 PEP 740 attestations 而非逐檔 PGP。出處：
  - https://peps.python.org/pep-0740/

### 4.3 改用官方 Docker image：以 digest 固定＋cosign 驗證（推薦給 LLMGateway）

- 官方 image 發到 `ghcr.io/berriai`（另鏡像於 `docker.litellm.ai/berriai`）；新部署用 `ghcr.io/berriai/litellm`（已含 Prisma toolchain，Postgres 情境亦用它；`litellm-database` 僅留作相容）。出處：
  - https://docs.litellm.ai/docs/proxy/deploy
  - https://docs.litellm.ai/docs/proxy/docker_image_security
- 生產不要用 `latest`／`main-stable`／`main-latest` 這類滾動 tag；用不可變的 `vX.Y.Z`（現行 PEP 440／semver，如 `v1.86.2`；舊 `vX.Y.Z-stable` 已改制），最好再疊 digest pin。出處：
  - https://docs.litellm.ai/docs/proxy/docker_image_security
  - https://docs.litellm.ai/docs/proxy/deploy
- Digest 固定：manifest 寫 `image: ghcr.io/berriai/litellm@sha256:<digest>`，digest 於 pull 後以 `docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/berriai/litellm:<版號>` 取得。出處：
  - https://docs.litellm.ai/docs/proxy/docker_image_security
- Cosign 驗證（`<release-tag>`／`<digest>` 換你要部署的）：
  - 最強驗法（key 以不可變 commit hash 取用）：`cosign verify --key https://raw.githubusercontent.com/BerriAI/litellm/0112e53046018d726492c814b3644b7d376029d0/cosign.pub ghcr.io/berriai/litellm:<release-tag>`。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
  - 簡便驗法（以 release tag 取 key，依賴 tag 保護規則）：`cosign verify --key https://raw.githubusercontent.com/BerriAI/litellm/<release-tag>/cosign.pub ghcr.io/berriai/litellm:<release-tag>`。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
  - digest 照樣可驗：`cosign verify --key https://raw.githubusercontent.com/BerriAI/litellm/0112e53046018d726492c814b3644b7d376029d0/cosign.pub ghcr.io/berriai/litellm@sha256:<digest>`。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
  - 預期輸出含「cosign claims were validated」「signatures were verified against the specified public key」。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
- CI/CD 強制四選一（LiteLLM 官方文件）：
  - Kubernetes 用 Sigstore Policy Controller＋`ClusterImagePolicy`（glob `ghcr.io/berriai/litellm*`，key 內嵌公鑰），namespace 貼 `policy.sigstore.dev/include=true`，未簽名 pod 直接拒收。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
  - GCP 用 Binary Authorization 配 cosign attestor。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
  - AWS（ECS/ECR 無原生 cosign 驗）：在推 ECR／更新 task definition 前跑 `cosign verify` 當 gate；EKS 則同 Kubernetes 做法。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
  - GitHub Actions gate：在部署 job 前加 `cosign verify … ghcr.io/berriai/litellm:${{ env.LITELLM_VERSION }}` 步驟。出處：
    - https://docs.litellm.ai/docs/proxy/docker_image_security
- 官方建議的升級五步：verify 新 image→staging 測→更新 pinned 參照→production rollout→看 `/health`。出處：
  - https://docs.litellm.ai/docs/proxy/docker_image_security

### 4.4 縱深：cooldown、lock、最小觸發面（PyPI 官方建議）

- Dependency cooldown：安裝器拒裝「剛發佈 N 天內」的包，留時間給研究員與 PyPI 檢舉／隔離。`uv` 以 `--exclude-newer`／`pyproject.toml [tool.uv] exclude-newer = "P3D"` 設相對 cooldown；pip v26.0 起支援絕對 `--uploaded-prior-to`，v26.1 預計支援相對 `P3D`（寫於 `pip.conf [install] uploaded-prior-to = P3D`），並可由 Dependabot／Renovate 對安全更新 bypass。出處：
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
- Lock：不要裸 `pip install` 最新版；應用側用含 hash 的 lock 檔：`uv lock`、`pip-compile --generate-hashes`、`pipenv`；`pip freeze` 只記版號不算 lock；pip 實驗性 `pip lock`（`pylock.toml`）。出處：
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
- 本案教訓：LiteLLM 每分鐘約 1700 次安裝、40～50% 未固定版直接抓最新，使惡意版幾乎即時被吃進去；PyPI 建議維護者把 workflow 依賴 pin 到 commit SHA、避免 `pull_request_target`、用 zizmor 掃。出處：
  - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/

---

## 5. 給 LLMGateway 的採用建議（若決定用 LiteLLM）

1. 優先走官方 Docker image，而非 `pip install litellm`：manifest 以 `ghcr.io/berriai/litellm@sha256:<digest>` 固定，並在 CI 以 commit-hash 取 key 跑 `cosign verify` 當 gate；tag 只用不可變 `vX.Y.Z`，不用 `latest`。出處（做法來源）：
   - https://docs.litellm.ai/docs/proxy/docker_image_security
   - https://docs.litellm.ai/docs/proxy/deploy
2. 若必須 pip：`litellm==<官方 Verified safe 清單或 v1.83.0+ 驗證版>`＋全依賴 `--hash`，部署以 `--require-hashes`＋`--only-binary :all:`；但同時接受「hash 擋不住合法憑證惡意發佈」，故每次升級前加做：(a) PyPI Integrity API／`pypi-attestations` 驗 provenance 是否等於預期的 Trusted Publisher 身份；(b) 掃 wheel 是否含 `.pth` 及 `base64`／`subprocess`／`exec` 模式；(c) 設 dependency cooldown（如 3 天）＋安全更新例外。出處：
   - https://pip.pypa.io/en/stable/topics/secure-installs/
   - https://docs.pypi.org/attestations/consuming-attestations/
   - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
   - https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
3. 監控 IoC（至少）：`litellm_init.pth`、`models.litellm.cloud`、`checkmarx.zone`、`~/.config/sysmon/sysmon.py`、`sysmon.service`、`/tmp/pglog`、`/tmp/.pg_state`、`tpcp.tar.gz`、K8s `kube-system` 內 `node-setup-*`。出處：
   - https://docs.litellm.ai/blog/security-update-march-2026
   - https://osv.dev/vulnerability/PYSEC-2026-2
   - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/

---

## 出處清單（本檔引用的公開網頁）

- https://docs.litellm.ai/blog/security-update-march-2026
- https://docs.litellm.ai/blog/security-townhall-updates
- https://docs.litellm.ai/docs/proxy/docker_image_security
- https://docs.litellm.ai/docs/proxy/deploy
- https://blog.pypi.org/posts/2026-04-02-incident-report-litellm-telnyx-supply-chain-attack/
- https://osv.dev/vulnerability/PYSEC-2026-2
- https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/
- https://www.trendmicro.com/en_gb/research/26/c/inside-litellm-supply-chain-compromise.html
- https://pip.pypa.io/en/stable/topics/secure-installs/
- https://pip.pypa.io/en/stable/topics/repeatable-installs/
- https://docs.pypi.org/trusted-publishers/
- https://docs.pypi.org/attestations/
- https://docs.pypi.org/attestations/consuming-attestations/
- https://docs.pypi.org/api/integrity/
- https://peps.python.org/pep-0740/

## 截至 2026-10-11 查無的事項

- 你現在要裝的 litellm 某版某檔當下的 provenance／attestation 狀態（未逐檔呼叫 Integrity API）：截至 2026-10-11 查無，出處概念見 https://docs.pypi.org/api/integrity/。
- LiteLLM 為 PyPI wheel/sdist 提供可驗 GPG 簽章的官方說明：截至 2026-10-11 查無。
- 被竊憑證是否已被攻擊者實際利用的官方定論：截至 2026-10-11 查無（僅有 Trend Micro「截稿時無公開確認」一句，出處見上）。
- GitHub issue #24518 全文逐字重驗：截至 2026-10-11 查無（本檔相關結論以官方 blog＋Snyk 轉引為準）。
