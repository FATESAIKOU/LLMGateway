"""D6c：repo 內呼叫方清冊（C-961）與跨 repo 簽回文件（C-962）（#222）。

契約來源：D6 架構規劃 `REPORT-D6-arch.md` §2.1（R1～R9、X1～X5）、§2.3（三層判定）、
§3.1／§3.2／§3.3、C-961、C-962、§4.3 的變異 8；第 1 輪架構審查（中-1～中-3）。

- C-961：清冊是這支測試裡的一張表（`INVENTORY`：路徑、行的特徵、分類），掃描範圍
  固定為 `lgw`、`lgwctl`、`ops-scripts`、`deploy`、`docs`；分類只有四種。

  程式碼的四個根目錄（`lgw`、`lgwctl`、`ops-scripts`、`deploy`）**預設是候選**：任何
  非整行註解的行，只要出現 lgwd 的路由字樣（`/v1/subscriptions`、`/v1/usage`、
  `/v1/resolve`、`/v1/roles`、`/healthz`），就要在 `INVENTORY`（真的在打 lgwd）或
  `MENTIONS`（只是說明：docstring、help 字串，各附理由）裡；不管用的是 curl、wget、
  http.client、requests 還是別的新寫法都一樣，所以新增一種沒分類的呼叫就會紅。
  `docs` 維持依指令判斷（`DOC_COMMAND_RULES`）：文件是說明文字，純文字提到路由不算
  呼叫方，但 `curl`／`urlopen`／`lgw.client`／路由常數／`path =` 的實際指令算。

- C-962：`docs/auth-rollout.md` 的跨 repo 清單與三層判定，每一列有「誰簽回、日期、
  依據」；寫「已完成」的列，依據欄不得是空的，也不得是「未」；白話版要照現況寫（D6a
  已完成、D6b 的測試已寫好並審查通過但等合併），而且要有「做完之後仍然存在的風險」。

只用標準函式庫；只讀 repo 內的檔案，不碰憑證、不連真機。
"""

from __future__ import annotations

import re
import unittest
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "auth-rollout.md"

#: 程式碼的掃描根目錄：這些目錄裡的行預設是候選（除非整行是註解）。
#: `deploy` 與 `ops-scripts` 是 shell，其餘是 Python。
CODE_ROOTS = ("lgw", "lgwctl", "ops-scripts", "deploy")

#: 文件（md）的掃描根目錄：維持依指令判斷。
DOC_ROOTS = ("docs",)

#: C-961 的固定掃描範圍（改範圍等於改契約）。
SCAN_ROOTS = CODE_ROOTS + DOC_ROOTS

#: 分類只有這四種。
CATEGORIES = ("簽章", "healthz", "只限 off 的本機", "舊版")

#: lgwd 的路由（寫出來才算；`/v1/subscriptions/{id}` 的舊 get 也算在 subscriptions）。
ENDPOINT_RE = re.compile(r"/(?:v1/(?:subscriptions|usage|resolve|roles)|healthz)(?![\w-])")

#: 文件裡「真的在發請求」的寫法（程式碼不需要這張表，預設就是候選）。
DOC_COMMAND_RULES = (
    # shell：curl 後面接的是實際 URL 或變數（只有「curl」這個字不算）。
    ("curl", re.compile(r"\bcurl\b.*(?:https?://|\$)")),
    # Python：真的開連線。
    ("urlopen", re.compile(r"\burl(?:open|retrieve)\s*\(")),
    # lgw.client 的送出點。
    ("client-send", re.compile(r"\._send\(")),
    # 路由常數（探針、舊版退回）。
    ("route-constant", re.compile(
        r"^\s*(?:[A-Z][A-Z0-9_]*PATH|PROBE_REQUEST|HEALTH_REQUEST)\s*=")),
    # 先組好 path 再送出的路由。
    ("route-variable", re.compile(r"^\s*path\s*=\s*[\"']")),
)

CallSite = namedtuple("CallSite", "path lineno rules line")
Entry = namedtuple("Entry", "path marker category note")
Mention = namedtuple("Mention", "path marker reason")


def _text_of(path: Path) -> str:
    """讀檔；二進位檔（含 NUL）跳過，其餘一律用 UTF-8（讀不開就讓測試紅）。"""
    data = path.read_bytes()
    if b"\x00" in data:
        return ""
    return data.decode("utf-8")


def _is_comment(line: str) -> bool:
    """整行註解（Python 與 shell 都是 `#`）。程式碼的候選不含這種行。"""
    return line.lstrip().startswith("#")


def scan_code_text(text: str):
    """程式碼：回 [(行號, "route"), ...]；整行註解不算，其餘含路徑就算候選。"""
    sites = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _is_comment(line):
            continue
        if ENDPOINT_RE.search(line):
            sites.append((lineno, "route"))
    return sites


def scan_docs_text(text: str):
    """文件：回 [(行號, 命中的規則名稱), ...]；要是指令而不是純文字說明。"""
    sites = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not ENDPOINT_RE.search(line):
            continue
        matched = [name for name, rule in DOC_COMMAND_RULES if rule.search(line)]
        if matched:
            sites.append((lineno, ",".join(matched)))
    return sites


def scan_text(text: str, code: bool = True):
    """依檔案種類選掃描方式（測試用；`scan_repo` 會依根目錄自己選）。"""
    return scan_code_text(text) if code else scan_docs_text(text)


def iter_scope_files(root: Path = REPO_ROOT):
    """掃描範圍內的檔案（略過 `__pycache__`）；順序固定，失敗訊息才好讀。"""
    for name in SCAN_ROOTS:
        base = root / name
        if not base.is_dir():
            raise AssertionError("掃描範圍缺少目錄：" + str(base))
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def is_code_path(relative: str) -> bool:
    return relative.split("/", 1)[0] in CODE_ROOTS


def scan_repo(root: Path = REPO_ROOT):
    """把整個掃描範圍掃成 CallSite 清單（程式碼用預設候選、文件用指令規則）。"""
    sites = []
    for path in iter_scope_files(root):
        text = _text_of(path)
        if not text:
            continue
        relative = path.relative_to(root).as_posix()
        code = is_code_path(relative)
        hits = scan_text(text, code=code)
        lines = text.splitlines()
        for lineno, rules in hits:
            sites.append(CallSite(relative, lineno, rules, lines[lineno - 1]))
    return sites


def _tables():
    return (INVENTORY, MENTIONS)


def unexplained(sites, inventory=None, mentions=None):
    """回沒有被清冊或「只是提及」解釋的呼叫行（路徑相同、特徵字串在那一行）。"""
    inventory = INVENTORY if inventory is None else inventory
    mentions = MENTIONS if mentions is None else mentions
    missing = []
    for site in sites:
        if any(entry.path == site.path and entry.marker in site.line
               for entry in inventory):
            continue
        if any(entry.path == site.path and entry.marker in site.line
               for entry in mentions):
            continue
        missing.append(site)
    return missing


#: C-961 的清冊：repo 內每一處打 lgwd 的地方。特徵字串是那一行的一小段。分類：
#:
#: - `簽章`：經 `lgw.client` 送出，enforce 下會簽章（R1）。
#: - `healthz`：健康探針，任何模式都回 200（R4～R6）。
#: - `只限 off 的本機`：對本機 loopback 的 lgwd 直接呼叫，只在 off 模式成立
#:   （Mac 退路 R7）。
#: - `舊版`：只有舊版（`old` capability）才走的退回路由，或還沒遷移的無簽呼叫。
INVENTORY = (
    Entry("lgw/client.py", 'self._send("GET", "/v1/subscriptions"', "簽章",
          "R1 list"),
    Entry("lgw/client.py", 'self._send("GET", "/v1/usage"', "簽章",
          "R1 usage（lgwctl/usage.py 只呼叫 list 與 roles，不呼叫這一條）"),
    Entry("lgw/client.py", 'self._send("GET", "/v1/resolve?"', "簽章",
          "R1 resolve"),
    Entry("lgw/client.py", 'path = "/v1/roles/{0}?{1}"', "簽章",
          "R1 roles"),
    Entry("lgw/client.py", 'path = "/v1/subscriptions/{0}/cooldown"', "簽章",
          "R1 cooldown"),
    Entry("lgwctl/probe.py", 'HEALTH_PATH = "/healthz"', "healthz",
          "R4 本機探針先打 healthz"),
    Entry("lgwctl/probe.py", 'LEGACY_PATH = "/v1/subscriptions"', "舊版",
          "R4 只有 old 且 healthz 404 才退回 list"),
    Entry("ops-scripts/llmgateway-tunnel", 'PROBE_REQUEST = b"GET /v1/subscriptions',
          "舊版", "R5 隧道的 old 退回 list"),
    Entry("ops-scripts/llmgateway-tunnel", 'HEALTH_REQUEST = b"GET /healthz',
          "healthz", "R5 隧道探針先打 healthz"),
    Entry("ops-scripts/mac-fallback/start", '--max-time 3 "http://127.0.0.1:$PORT/v1/subscriptions"',
          "只限 off 的本機", "R7 start 的 list 驗收（Mac 本機 off）"),
    Entry("ops-scripts/mac-fallback/start", 'curl -s --max-time 10 "http://127.0.0.1:$PORT/v1/subscriptions"',
          "只限 off 的本機", "R7 start 的列數檢查（Mac 本機 off）"),
    Entry("ops-scripts/mac-fallback/switch", 'urlopen(base + "/v1/subscriptions"',
          "只限 off 的本機", "R7 switch 的新版 list 驗收（Mac 本機 off）"),
    Entry("ops-scripts/mac-fallback/switch", '"$base/v1/subscriptions/$VERIFY_OLD_GET_ID"',
          "只限 off 的本機",
          "R7 switch 驗已移除的舊 get 回 404（Mac 本機 off；404 不經 gate，"
          "warn／enforce 下也是 404）"),
    Entry("ops-scripts/mac-fallback/switch", '"$base/v1/resolve?$VERIFY_RESOLVE"',
          "只限 off 的本機", "R7 switch 的可選 resolve 驗收（Mac 本機 off）"),
    Entry("ops-scripts/mac-fallback/switch", '"http://127.0.0.1:$PORT/v1/subscriptions"',
          "只限 off 的本機", "R7 switch 停損回滾後驗 list（Mac 本機 off）"),
    Entry("deploy/install.sh", '${port}/healthz"', "healthz",
          "R6 部署後的本機探針"),
    Entry("deploy/install.sh", '${port}/v1/subscriptions"', "舊版",
          "R6 只有 404 才退回 list（v0.1 遷移用）"),
    Entry("docs/deploy.md", "curl -s http://127.0.0.1:4710/healthz", "healthz",
          "手動測試連線：healthz"),
    Entry("docs/deploy.md", "curl -s http://127.0.0.1:4710/v1/subscriptions",
          "舊版",
          "enforce 前要改：這一條經 ssh -L 打正式主機，無簽只在 off／warn 可用，"
          "enforce 之後要改用 §4.2 的 lgw（帶 signer）"),
    Entry("docs/deploy.md", "http://127.0.0.1:<本機埠>/healthz", "healthz",
          "第 6 章隧道排錯的健康檢查"),
    Entry("docs/mac-fallback.md", "127.0.0.1:4710/healthz", "healthz",
          "第 1、7 節的切換判斷（一律打 healthz）"),
    Entry("docs/mac-fallback.md", "curl -s http://127.0.0.1:4710/v1/subscriptions",
          "只限 off 的本機", "第 4 節驗收：Mac 本機 off 的 lgwd"),
    Entry("docs/research/v0-impl-review.md",
          'curl http://127.0.0.1:${port}/v1/subscriptions', "舊版",
          "歷史審查紀錄引用舊的 install.sh；現行呼叫在 deploy/install.sh:251"),
)

#: 程式碼裡「只是提及」的路由：docstring、表格、help 字串。這些不是請求，所以不進
#: 清冊；但預設候選會抓到它們，所以逐行列出來並寫理由（改了就要同步）。
MENTIONS = (
    Mention("lgw/client.py", "- list() → GET /v1/subscriptions",
            "docstring：對外 API 說明，不是請求"),
    Mention("lgw/client.py", "- usage() → GET /v1/usage",
            "docstring：對外 API 說明，不是請求"),
    Mention("lgw/client.py", "- resolve(agent, provider, model, subscription, exclude, data) → GET /v1/resolve",
            "docstring：對外 API 說明，不是請求"),
    Mention("lgw/client.py", "- roles(role, agent, data=None) → GET /v1/roles/{role}",
            "docstring：對外 API 說明，不是請求"),
    Mention("lgw/client.py", "- cooldown(id, until, model=None) → POST /v1/subscriptions/{id}/cooldown",
            "docstring：對外 API 說明，不是請求"),
    Mention("lgw/client.py", "`http://host/prefix` 時簽 `/prefix/v1/subscriptions`）",
            "docstring：簽章範圍的說明，不是請求"),
    Mention("lgw/client.py", '"""用量一覽：GET /v1/usage',
            "docstring：方法的用途說明，不是請求"),
    Mention("lgw/signals.py", "欄位設計直接對應 LLMGateway 的 POST /v1/subscriptions/{id}/cooldown",
            "docstring：body 設計對應的路由說明，不是請求（signals 只組 body）"),
    Mention("lgw/signals.py", '"""產生 POST /v1/subscriptions/{id}/cooldown 的 body',
            "docstring：body 設計對應的路由說明，不是請求"),
    Mention("lgwctl/capability.py", "能正確判活（先探 ``/healthz``，404 才退回 list）",
            "docstring：能力旗標與探針行為說明，不是請求"),
    Mention("lgwctl/capability.py", "會讓隧道用 ``old`` 探",
            "docstring：能力旗標失效時的影響說明，不是請求"),
    Mention("lgwctl/check.py", "- ``list-local``：本機探針。先 ``GET /healthz``",
            "docstring：check 項目的行為說明，不是請求"),
    Mention("lgwctl/check.py", "時才退回 ``GET /v1/subscriptions``",
            "docstring：check 項目的退回條件說明，不是請求"),
    Mention("lgwctl/cli.py", '"查詢見 GET /v1/roles/{role}"',
            "help 字串：使用者提示，不是請求"),
    Mention("lgwctl/probe.py", '"""兩個探針：本機的 ``/healthz``',
            "docstring：模組用途說明，不是請求"),
    Mention("lgwctl/probe.py", "| 模式＼``/healthz``",
            "docstring：探針結果對照表，不是請求"),
    Mention("lgwctl/probe.py", '"""本機探針：先 ``GET /healthz``',
            "docstring：函式行為說明，不是請求"),
    Mention("lgwctl/upgrade.py", "``new`` 就只打 ``/healthz``",
            "docstring：探針模式說明，不是請求"),
    Mention("ops-scripts/llmgateway-tunnel", '"""端對端探針（#246 P-2）：先 ``GET /healthz``',
            "docstring：函式行為說明，不是請求"),
    Mention("ops-scripts/llmgateway-tunnel", "old 只有在拿到 404 時才第二次送 ``GET /v1/subscriptions``",
            "docstring：退回條件說明，不是請求"),
)


def inventory_entry(site: CallSite):
    """給某一行的呼叫找清冊的列（路徑與特徵字串都要對上）。"""
    for entry in INVENTORY:
        if entry.path == site.path and entry.marker in site.line:
            return entry
    return None


class CallerInventoryTests(unittest.TestCase):
    """C-961：repo 內呼叫方清冊。"""

    def test_c961_inventory_is_a_table_with_only_four_categories(self):
        self.assertTrue(INVENTORY, "清冊是空的")
        self.assertEqual(len(INVENTORY), len(set(INVENTORY)), "清冊有重複的列")
        for entry in INVENTORY:
            self.assertIn(entry.category, CATEGORIES, entry)
            self.assertTrue(entry.marker.strip(), entry)
            self.assertTrue(entry.note.strip(), entry)
        # 四種分類都要真的用到，否則分類只是寫好看的。
        self.assertEqual(set(CATEGORIES), {entry.category for entry in INVENTORY})

    def test_c961_mentions_table_is_well_formed(self):
        self.assertTrue(MENTIONS, "「只是提及」的表是空的")
        self.assertEqual(len(MENTIONS), len(set(MENTIONS)), "「只是提及」有重複的列")
        for mention in MENTIONS:
            self.assertTrue(mention.marker.strip(), mention)
            self.assertTrue(mention.reason.strip(), mention)
            self.assertIn(mention.path.split("/", 1)[0], CODE_ROOTS, mention)

    def test_c961_every_scope_root_has_inventory_rows(self):
        # 固定掃描範圍的五個目錄都要有列，否則「範圍」只是寫好看的。
        covered = {entry.path.split("/", 1)[0] for entry in INVENTORY}
        self.assertEqual(covered, set(SCAN_ROOTS))

    def test_c961_every_detected_call_site_is_inventoried(self):
        sites = scan_repo()
        self.assertTrue(sites, "掃描範圍內完全沒有偵測到 lgwd 的路由")
        missing = unexplained(sites)
        self.assertEqual(
            [], [(site.path, site.lineno, site.rules) for site in missing],
            "有 lgwd 路由的行不在清冊或「只是提及」裡（新增呼叫方要進 INVENTORY "
            "並分類，說明性提及要進 MENTIONS 寫理由）：\n"
            + "\n".join("{0}:{1} {2}".format(s.path, s.lineno, s.line.strip())
                        for s in missing),
        )

    def test_c961_inventory_and_mentions_still_exist(self):
        # 反向：兩張表都不能留下已經不存在的列（改了程式或文件要同步）。
        stale = []
        for table in _tables():
            for entry in table:
                path = REPO_ROOT / entry.path
                if not path.is_file():
                    stale.append((entry.path, "檔案不存在"))
                    continue
                if entry.marker not in _text_of(path):
                    stale.append((entry.path, entry.marker))
        self.assertEqual([], stale, "表裡有過期的列（檔案或那一行已改）：" + repr(stale))

    def test_c961_mentions_and_inventory_do_not_overlap(self):
        # 同一個候選行不能同時是「呼叫」和「只是提及」。
        sites = scan_repo()
        for site in sites:
            in_inventory = any(entry.path == site.path and entry.marker in site.line
                               for entry in INVENTORY)
            mentioned = any(entry.path == site.path and entry.marker in site.line
                            for entry in MENTIONS)
            self.assertFalse(in_inventory and mentioned,
                             "同一行同時在清冊與「只是提及」：" + repr(site))

    def test_c961_client_business_routes_are_all_signed(self):
        # R1 的五條業務路由都要在清冊裡、而且分類是簽章。
        sites = [site for site in scan_repo() if site.path == "lgw/client.py"]
        self.assertTrue(sites)
        for site in sites:
            entry = inventory_entry(site)
            if entry is None:
                # 純說明（docstring）要在「只是提及」裡；不可是兩邊都沒有。
                self.assertTrue(
                    any(m.path == site.path and m.marker in site.line
                        for m in MENTIONS),
                    "lgw/client.py 這一行兩張表都沒有：" + site.line,
                )
                continue
            self.assertEqual("簽章", entry.category, site.line)
        calls = "\n".join(site.line for site in sites
                          if inventory_entry(site) is not None)
        for route in ("/v1/subscriptions", "/v1/usage", "/v1/resolve",
                      "/v1/roles", "/v1/subscriptions/{0}/cooldown"):
            self.assertIn(route, calls, route)

    def test_c961_comment_lines_are_not_candidates(self):
        # 整行註解不是呼叫方（例如隧道腳本開頭的用法說明）。
        text = "# curl http://127.0.0.1:4710/v1/subscriptions\n"
        self.assertEqual([], scan_code_text(text))
        text = "  # GET /v1/resolve 的說明\n"
        self.assertEqual([], scan_code_text(text))

    def test_c961_docs_still_use_command_rules(self):
        # 文件維持依指令判斷：純文字提到路由不算，curl 指令算。
        self.assertEqual([], scan_docs_text("服務提供 GET /v1/subscriptions 與 /healthz。"))
        self.assertEqual([(1, "curl")],
                         scan_docs_text("curl -s http://127.0.0.1:4710/v1/subscriptions\n"))
        # 程式碼相反：同樣的純文字行，只要不是註解就是候選。
        self.assertEqual([(1, "route")],
                         scan_code_text("服務提供 GET /v1/subscriptions。\n"))

    def test_c961_new_call_site_outside_inventory_is_red(self):
        # 變異 8 的最小版：在 ops-scripts 多一行沒在清冊的 curl，掃描要看得到、
        # 而且會被判成「沒解釋」。
        text = "#!/bin/sh\ncurl http://127.0.0.1:4710/v1/subscriptions\n"
        hits = scan_code_text(text)
        self.assertEqual([(2, "route")], hits)
        site = CallSite("ops-scripts/llmgateway-tunnel", 2, "route",
                        text.splitlines()[1])
        self.assertEqual([site], unexplained([site]))

    def test_c961_wget_call_site_is_detected(self):
        # 第 1 輪審查的負向案例 1：wget 不是白名單上的寫法，仍必須是候選。
        line = 'wget -qO- "http://127.0.0.1:4710/v1/usage"'
        self.assertEqual([(1, "route")], scan_code_text(line + "\n"))
        site = CallSite("lgwctl/usage.py", 1, "route", line)
        self.assertEqual([site], unexplained([site]))

    def test_c961_http_client_call_site_is_detected(self):
        # 負向案例 2：http.client 的 request() 也必須是候選。
        line = ('http.client.HTTPConnection("127.0.0.1", 4710)'
                '.request("GET", "/v1/resolve?agent=a")')
        self.assertEqual([(1, "route")], scan_code_text(line + "\n"))
        site = CallSite("lgwctl/check.py", 1, "route", line)
        self.assertEqual([site], unexplained([site]))

    def test_c961_requests_call_site_is_detected(self):
        # 負向案例 3：requests.get 也必須是候選。
        line = 'requests.get("http://127.0.0.1:4710/v1/subscriptions")'
        self.assertEqual([(1, "route")], scan_code_text(line + "\n"))
        site = CallSite("lgwctl/usage.py", 1, "route", line)
        self.assertEqual([site], unexplained([site]))


# ---------------------------------------------------------------------------
# C-962：跨 repo 簽回與三層判定（docs/auth-rollout.md）
# ---------------------------------------------------------------------------


def read_doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def markdown_tables(text: str):
    """回每一張表的 (headers, rows)；第一列是表頭，分隔列跳過。"""
    tables = []
    block = []

    def flush():
        if not block:
            return
        headers = _cells(block[0])
        rows = []
        for raw in block[1:]:
            row = _cells(raw)
            if all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in row):
                continue
            rows.append(row)
        tables.append((headers, rows))

    for line in text.splitlines() + [""]:
        if line.lstrip().startswith("|"):
            block.append(line)
            continue
        flush()
        block = []
    return tables


def _cells(line: str):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def section(text: str, heading_prefix: str) -> str:
    """回 `## <heading_prefix>` 這一段（到下一個 `## ` 為止）。"""
    start = text.find(heading_prefix)
    if start < 0:
        raise AssertionError("找不到章節：" + heading_prefix)
    rest = text[start + len(heading_prefix):]
    match = re.search(r"(?m)^## ", rest)
    end = len(text) if match is None else start + len(heading_prefix) + match.start()
    return text[start:end]


class AuthRolloutDocTests(unittest.TestCase):
    """C-962：`docs/auth-rollout.md` 的清單、判定與依據。"""

    def setUp(self):
        self.assertTrue(DOC_PATH.is_file(), "缺少 docs/auth-rollout.md")
        self.doc = read_doc()
        self.tables = markdown_tables(self.doc)

    def test_c962_cross_repo_table_has_signer_date_basis_columns(self):
        # X1～X5 各一列，而且欄名就是「誰簽回」「日期」「依據」。
        found = []
        for headers, rows in self.tables:
            if "誰簽回" not in headers:
                continue
            self.assertIn("日期", headers)
            self.assertIn("依據", headers)
            found.append((headers, rows))
        self.assertTrue(found, "找不到有「誰簽回／日期／依據」欄的跨 repo 表")

        rows = [row for _, block in found for row in block]
        first_cells = {row[0] for row in rows if row}
        for tag in ("X1", "X2", "X3", "X4", "X5"):
            self.assertIn(tag, first_cells, "跨 repo 表缺少 " + tag + " 一列")

    def test_c962_mlp_reply_is_recorded(self):
        # MLP 11-24 的回覆就是 X1、X2 兩列與觀察期的依據。
        self.assertIn("MLP 11-24 回覆", self.doc)
        self.assertIn("MyLinuxPool#1740", self.doc)
        self.assertIn("MyLinuxPool#1741", self.doc)
        self.assertIn("healthz", self.doc)
        self.assertIn("4710", self.doc)

    def test_c962_three_layer_judgement_present(self):
        for layer in ("① repo 內", "② 跨 repo", "③ 執行期"):
            self.assertIn(layer, self.doc, layer)
        # ③ 的判準是 warn 觀察期：所有 principal 都 pass，而且一整個工作日沒有
        # reason=missing 的 deny。
        self.assertIn("outcome=pass", self.doc)
        self.assertIn("reason=missing", self.doc)
        self.assertIn("工作日", self.doc)

    def test_c962_three_layer_table_has_signer_date_basis_columns(self):
        # 三層判定也逐層寫「誰簽回、日期、依據」；還沒簽回的寫「未」。
        rows = []
        headers = None
        for block_headers, block_rows in self.tables:
            if any(row and row[0].startswith(("①", "②", "③")) for row in block_rows):
                headers = block_headers
                rows = block_rows
        self.assertIsNotNone(headers, "找不到三層判定的表")
        for column in ("誰簽回", "日期", "依據"):
            self.assertIn(column, headers, column)
        self.assertEqual(len(rows), 3, rows)
        signer_index = headers.index("誰簽回")
        date_index = headers.index("日期")
        for row in rows:
            self.assertTrue(row[signer_index].strip(), row)
            self.assertTrue(row[date_index].strip(), row)
        # ② 與 ③ 都還沒簽回，日期也要是「未」。
        for row in rows:
            if row[0].startswith(("②", "③")):
                self.assertEqual("未", row[date_index].strip(), row)

    def test_c962_enforce_readiness_items_present(self):
        # REPORT-D6-arch §3.1 的六項；每一項都要看得到，不能只寫「全部完成」。
        for token in (
            "D6a", "D6b", "D6c",
            "d 單-1", "d 單-2",
            "D5d 段二",
            "P-D4",
            "問題 4",
            "8.18",
        ):
            self.assertIn(token, self.doc, token)

    def test_c962_v0_prerequisites_present(self):
        # REPORT-D6-arch §3.2：V-0 的七項與 P-D4（真機，要本人單次授權）；#313 加入 V-0.6b，共八項。
        for item in ("V-0.1", "V-0.2", "V-0.3", "V-0.4", "V-0.5", "V-0.6",
                     "V-0.6b", "V-0.7", "P-D4"):
            self.assertIn(item, self.doc, item)
        self.assertIn("本人單次授權", self.doc)
        self.assertIn("不會打開強制認證", self.doc)

    def test_c962_plain_language_section_present(self):
        self.assertIn("白話版", self.doc)
        self.assertIn("強制認證", self.doc)

    def test_c962_plain_language_reflects_current_state_and_risks(self):
        # 中-1：白話版不能把 D6b 還沒合併的事寫成已驗；而且要有殘餘風險。
        text = section(self.doc, "## 7. 白話版")
        # 排版會換行、引言符號會插在行首；比對句子時把換行與引言符號拿掉
        # （每一行裡原有的空白要留著，關鍵詞可能含空白）。
        flat = "".join(line.strip().lstrip(">").strip()
                       for line in text.splitlines())
        for token in (
            "D6a",               # 已經驗過的那一段
            "D6b",               # 還沒合併的那一段
            "等合併",             # 現況：不是已完成，也不是沒做
            "審查通過",
            "合併後才算數",        # 驗證證據的時點
            "換鑰", "撤銷", "重放", "回退", "修復",   # D6b 的範圍要看得到
            "同一個帳號",          # 殘餘風險 1：任何人都讀得到隧道金鑰
            "隧道金鑰",
            "不能證明是哪一個程式",
            "訂閱憑證",
            "降回去",             # 殘餘風險 2：回退等於沒有認證
            "降級滿 1 小時",       # 有告警
        ):
            self.assertIn(token, flat, token)
        # 也不可以反過來說「整套認證都跑過一遍」。
        self.assertNotIn("整套認證跑過一遍", flat)

    def test_c962_operational_decisions_recorded(self):
        # 本人已決定的事，要留下依據與日期；不能只寫結論。
        for token in ("#292", "外圍腳本", "至少一個工作日", "切 `enforce` 之前",
                      "本人決定", "殘餘風險"):
            self.assertIn(token, self.doc, token)

    def test_c962_done_rows_must_have_basis(self):
        # 寫「已完成」的列，依據欄不得空白、也不得以「未」開頭或只有破折號。
        checked = 0
        for headers, rows in self.tables:
            has_done = any("已完成" in cell for row in rows for cell in row)
            if not has_done:
                continue
            self.assertIn("依據", headers, "有「已完成」的表必須有依據欄")
            basis_index = headers.index("依據")
            for row in rows:
                if not any("已完成" in cell for cell in row):
                    continue
                checked += 1
                basis = row[basis_index] if basis_index < len(row) else ""
                self.assertTrue(
                    basis
                    and not basis.startswith("未")
                    and basis.strip("—－- ") != "",
                    "這一列寫了已完成卻沒有依據：" + repr(row),
                )
        self.assertGreater(checked, 0, "文件裡沒有任何「已完成」列可以檢查（C-962 的斷言會空轉）")

    def test_c321_status_rows_cite_merged_evidence(self):
        # #321：狀態列要附 PR／commit 依據；已合的 D6a／b／c 與 D5d 段二要看得到編號。
        rows = {}
        for _, block in self.tables:
            for row in block:
                if row and row[0].strip().isdigit():
                    rows[int(row[0].strip())] = row
        self.assertIn(1, rows, "§4 表缺第 1 列")
        for token in ("#293", "#301", "#299", "c73ed97", "b9c7c46", "fe5f60f"):
            self.assertIn(token, " ".join(rows[1]), token)
        self.assertIn("#307", " ".join(rows[3]))
        self.assertIn("26fd98f", " ".join(rows[3]))
        self.assertIn("#290", " ".join(rows[2]))
        self.assertIn("#291", " ".join(rows[2]))
        self.assertIn("V-0 計畫 v2", " ".join(rows[6]))
        self.assertIn("lgw-v0", " ".join(rows[6]))
        # 第 4、5 列維持未成立。
        self.assertTrue(rows[4][2].strip().startswith("未"), rows[4])
        self.assertTrue(rows[5][2].strip().startswith("未"), rows[5])

    def test_c962_unsigned_rows_are_not_claimed_done(self):
        # 誰簽回還是「未」的列，不得寫成已完成。
        for headers, rows in self.tables:
            if "誰簽回" not in headers:
                continue
            index = headers.index("誰簽回")
            for row in rows:
                if index >= len(row) or not row[index].startswith("未"):
                    continue
                self.assertFalse(
                    any("已完成" in cell for cell in row),
                    "還沒簽回的列寫成已完成：" + repr(row),
                )


if __name__ == "__main__":
    unittest.main()
