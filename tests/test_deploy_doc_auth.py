"""docs/deploy.md 第 8 章（應用層認證）的測試（#222 D5d 段一；C-833、C-834）。

- C-833：章節存在、在附錄之前，附錄不再留 `LGWD_AUTH_` 內容；a～q 各節都有固定標題。
- C-834 的 1～6：文件的指令能被真的 argparse 接受；設定表與 `lgwd/main.py`／
  `AuthConfig` 兩個方向一致；check 項目名真的在 `lgwctl/check.py` 的 report 呼叫；
  私鑰路徑／key ID／權限等於 `lgwctl/local_auth.py`；範例名冊能通過
  `lgwd.auth_registry.parse_registry`；結束碼 2／78 對得到程式的 `return 2`／`return 78`。

只用標準函式庫與臨時目錄；範例名冊用臨時產生的 ed25519 公鑰（`ssh-keygen`），
不碰真鑰、不連真機。
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lgwctl import local_auth
from lgwctl.cli import build_parser
from lgwd import auth_db
from lgwd import http as lgwd_http
from lgwd import main as lgwd_main
from lgwd import sshsig
from lgwd.auth_registry import OPS, parse_registry
from lgwd.policy import AGENTS, ROLES

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "deploy.md"

CHAPTER_HEADING = "## 8. 應用層認證"
APPENDIX_HEADING = "## 附錄：舊版（v0.1.x，遷移完成後不再使用）"

#: a～q 十七節（另有 8.17.1 資源上限）的固定標題（測試逐一斷言；改標題等於改契約）。
SECTION_HEADINGS = (
    "### 8.1 認證設定（a）",
    "### 8.2 首次啟用（離線可重現）（b）",
    "### 8.3 本機管理 principal（c）",
    "### 8.4 名冊預設（B）與範例（d）",
    "### 8.5 resolve_agents 與 roles 的用法（e）",
    "### 8.6 resolve 不能依角色限制（f）",
    "### 8.7 cooldown 粒度粗（g）",
    "### 8.8 warn 到 enforce 與回退（h）",
    "### 8.9 回滾版本的副作用（i）",
    "### 8.10 新增鍵與輪替（j）",
    "### 8.11 撤銷與撤銷延遲（k）",
    "### 8.12 不得用備份還原（l）",
    "### 8.13 故障修復（m）",
    "### 8.14 維護期間的 check（n）",
    "### 8.15 policy 新增 agent 或角色時（o）",
    "### 8.16 warn 的恢復（p）",
    "### 8.17 停止與日誌（q）",
    "### 8.17.1 資源上限",
    "### 8.17.2 enforce 的磁碟與 IO 前提（#313）",
    "### 8.18 上線前清單",
    "### 8.19 定時檢查（timer）（r）",
    "### 8.20 從 MLP 清單同步名冊（外圍腳本 `lgw-registry-sync`，#292）",
    "### 8.21 check 失敗時通知 PMO（失敗通知 unit，#296）",
)

#: `--auth-*` 旗標對應 `AuthConfig` 的欄位（C-834 第 2 項）。
AUTH_FIELD_BY_FLAG = {
    "--auth-mode": "mode",
    "--auth-registry": "registry_path",
    "--auth-state-dir": "state_dir",
    "--auth-sshkeygen-path": "sshkeygen_path",
    "--auth-per-key-max": "per_key_max",
    "--auth-verify-concurrency": "verify_concurrency",
    "--auth-verify-timeout": "verify_timeout",
}


def read_doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def chapter_bounds(doc: str) -> tuple[int, int]:
    """回第 8 章的 (start, end) 字元位置；找不到標題就失敗。"""
    start = doc.find(CHAPTER_HEADING)
    if start < 0:
        raise AssertionError("docs/deploy.md 沒有 " + CHAPTER_HEADING)
    rest = doc[start + len(CHAPTER_HEADING):]
    match = re.search(r"(?m)^## ", rest)
    end = len(doc) if match is None else start + len(CHAPTER_HEADING) + match.start()
    return start, end


def chapter_text(doc: str | None = None) -> str:
    doc = read_doc() if doc is None else doc
    start, end = chapter_bounds(doc)
    return doc[start:end]


def code_blocks(text: str) -> list[list[str]]:
    """圍欄程式碼區塊的內容（不含 ``` 那幾行）。"""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def default_cell(text: str) -> str:
    """表格「預設值」欄的比較值：去掉說明括號與反引號。"""
    value = text.split("（", 1)[0].strip().strip("`").strip()
    return value


def number_text(value) -> str:
    """把 2.0 寫成 2，其餘照 str()。"""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


class _Captured(Exception):
    """借 argparse 的 parse_args 取得 parser 本身（不讓 load_config 跑完）。"""


class DeployDocChapterTests(unittest.TestCase):
    """C-833：章節存在、不在附錄、十七節都在、附錄不再有認證內容。"""

    def test_c833_chapter_exists_outside_appendix_with_all_sections(self):
        doc = read_doc()
        start, end = chapter_bounds(doc)
        appendix = doc.find(APPENDIX_HEADING)
        self.assertGreaterEqual(appendix, 0, "找不到附錄標題")
        self.assertLess(start, appendix, "第 8 章必須在附錄之前")
        self.assertLess(start, end)

        chapter = doc[start:end]
        for heading in SECTION_HEADINGS:
            self.assertIn(heading, chapter, heading)

        appendix_text = doc[appendix:]
        self.assertNotIn("LGWD_AUTH_", appendix_text,
                         "附錄不能留認證設定（已搬到第 8 章）")


class DeployDocConsistencyTests(unittest.TestCase):
    """C-834 的 1～6：文件與程式自動比對。"""

    def setUp(self):
        self.chapter = chapter_text()

    # -- C-834 第 1 項 -------------------------------------------------------

    def test_c834_1_every_documented_command_parses(self):
        commands = []
        for block in code_blocks(self.chapter):
            for line in block:
                stripped = line.strip()
                if stripped.startswith("sudo llmgwctl "):
                    commands.append(stripped[len("sudo "):])
                elif stripped.startswith("llmgwctl "):
                    commands.append(stripped)
        # 行內程式碼（例如 8.13 的表格）也要回放，否則表格寫錯旗標不會紅。
        for span in re.findall(r"`([^`\n]+)`", self.chapter):
            if span.startswith("sudo llmgwctl "):
                commands.append(span[len("sudo "):])
            elif span.startswith("llmgwctl "):
                commands.append(span)
        self.assertTrue(commands, "第 8 章沒有 llmgwctl 指令範例")

        parser = build_parser()
        for command in commands:
            # 佔位（<檔>、<key ID>、<已核驗名冊>…）換成固定字串；argparse 不檢查檔案在不在。
            filled = re.sub(r"<[^<>]*>", "X", command)
            argv = shlex.split(filled)
            self.assertEqual(argv[0], "llmgwctl", command)
            with self.subTest(command=command):
                # 不得 SystemExit：旗標、子指令與必要參數都必須真的存在。
                parser.parse_args(argv[1:])

    # -- C-834 第 2 項 -------------------------------------------------------

    def _doc_rows(self):
        rows = {}
        for line in self.chapter.splitlines():
            if not line.startswith("| `--auth-"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            self.assertEqual(len(cells), 3, line)
            flag = cells[0].strip("`")
            env = cells[1].strip("`")
            rows[flag] = (env, default_cell(cells[2]))
        return rows

    def _parser_auth_flags(self):
        captured = {}

        def fake_parse(self, args=None, namespace=None):
            captured["parser"] = self
            raise _Captured()

        with mock.patch.object(argparse.ArgumentParser, "parse_args", fake_parse):
            with self.assertRaises(_Captured):
                lgwd_main.load_config([], env={})
        parser = captured["parser"]
        flags = set()
        for action in parser._actions:
            for option in action.option_strings:
                if option.startswith("--auth-"):
                    flags.add(option)
        return flags

    def test_c834_2_settings_table_matches_parser_and_authconfig(self):
        rows = self._doc_rows()
        parser_flags = self._parser_auth_flags()
        # 兩個方向都要一致：文件不多、也不漏。
        self.assertEqual(set(rows), parser_flags)
        self.assertEqual(parser_flags, set(AUTH_FIELD_BY_FLAG))

        defaults = lgwd_main.load_config([], env={}).auth
        for flag, (env_name, default) in rows.items():
            expected_env = "LGWD_AUTH_" + flag[len("--auth-"):].upper().replace("-", "_")
            self.assertEqual(env_name, expected_env, flag)
            expected = getattr(defaults, AUTH_FIELD_BY_FLAG[flag])
            self.assertEqual(default, number_text(expected), flag)

    # -- C-834 第 3 項 -------------------------------------------------------

    def test_c834_3_check_items_exist_in_report_calls(self):
        source = (REPO_ROOT / "lgwctl" / "check.py").read_text(encoding="utf-8")
        for name in ("auth-local", "auth-heartbeat", "auth-water", "auth-stores",
                     "auth-busy", "auth-degraded", "check-timer"):
            self.assertIn(name, self.chapter, name)
            self.assertRegex(source, r'report\.(?:ok|todo|fail)\(\s*"' + re.escape(name) + r'"',
                             name)

    # -- C-834／C-988：定時檢查的 unit 名與環境變數（#289） -------------------

    def test_c834_7_timer_units_and_journal_env_are_documented(self):
        """#289 C-988：文件要提到兩個 unit、timer 的環境變數與安裝指令。"""
        from lgwctl.layout import CHECK_SERVICE_NAME, CHECK_TIMER_NAME, ENV_CHECK_JOURNAL

        self.assertIn(CHECK_SERVICE_NAME, self.chapter)
        self.assertIn(CHECK_TIMER_NAME, self.chapter)
        self.assertIn(ENV_CHECK_JOURNAL, self.chapter)
        # 從舊版升級後要再跑一次 install（契約 C-988）。
        self.assertIn("install --tag", self.chapter)
        # 維護期間的停／啟指令（PM 的裁定）。
        self.assertIn("systemctl stop " + CHECK_TIMER_NAME, self.chapter)
        self.assertIn("systemctl start " + CHECK_TIMER_NAME, self.chapter)

    # -- C-834 第 4 項 -------------------------------------------------------

    def test_c834_4_local_admin_key_facts_match_program(self):
        self.assertEqual(local_auth.REQUIRED_MODE, 0o600)
        self.assertIn(local_auth.DEFAULT_KEY_PATH, self.chapter)
        self.assertIn(local_auth.DEFAULT_KEY_ID, self.chapter)
        # 私鑰那一句必須寫「恰為 0600」；0400 只能出現在「0400 也會被拒」那一句。
        self.assertIn("權限**恰為 0600**", self.chapter)
        sentences = [part for part in re.split(r"[。\n]", self.chapter) if "0400" in part]
        self.assertEqual(len(sentences), 1, "文件應該恰好有一句提到 0400")
        self.assertIn("0400 也會被拒", sentences[0])

    # -- C-834 第 5 項 -------------------------------------------------------

    def _registry_block(self):
        for block in code_blocks(self.chapter):
            text = "\n".join(block)
            if '"principals"' in text:
                return text
        self.fail("第 8 章找不到名冊 JSON 範例")

    def test_c834_5_example_registry_passes_parse_registry(self):
        block = self._registry_block()
        tokens = sorted(set(re.findall(r"<[A-Z_]+>", block)))
        self.assertTrue(tokens, "名冊範例沒有任何公鑰佔位")
        with tempfile.TemporaryDirectory() as tmp:
            for index, token in enumerate(tokens):
                key_path = Path(tmp) / ("key{0}".format(index))
                subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "",
                     "-f", str(key_path)],
                    check=True, capture_output=True,
                )
                public = key_path.with_suffix(".pub").read_text(encoding="utf-8").split()
                block = block.replace(token, public[0] + " " + public[1])
        registry = parse_registry(block.encode("utf-8"))
        self.assertEqual(registry.generation, 12)
        self.assertEqual(set(registry.principals), {"w1-worker", "local-admin"})
        self.assertEqual(registry.key_by_id("w1-tunnel-old").status, "revoked")

        # 範例本身是規格：節點 principal 是 B（五種操作全開、agent 與 roles 填滿），
        # 本機管理 principal 只能 list／usage／roles、不得 resolve 或 cooldown。
        all_roles = {role: tuple(AGENTS) for role in ROLES}
        worker = registry.principals["w1-worker"]
        self.assertEqual(worker.ops, frozenset(OPS))
        self.assertEqual(worker.resolve_agents, tuple(AGENTS))
        self.assertEqual(dict(worker.roles), all_roles)
        admin = registry.principals["local-admin"]
        self.assertEqual(admin.ops, frozenset({"list", "usage", "roles"}))
        self.assertEqual(admin.resolve_agents, ())
        self.assertEqual(dict(admin.roles), all_roles)

    # -- C-834 第 6 項 -------------------------------------------------------

    def test_c834_6_exit_codes_match_program(self):
        source = (REPO_ROOT / "lgwd" / "main.py").read_text(encoding="utf-8")
        self.assertIn("return 2", source)
        self.assertIn("return 78", source)
        self.assertIn("結束碼 2", self.chapter)
        self.assertIn("結束碼 78", self.chapter)
        self.assertIn("RestartPreventExitStatus=78", self.chapter)


def between(doc: str, start_heading: str, end_heading: str) -> str:
    """回兩個標題之間的文字（不含結束標題）；找不到就失敗。"""
    start = doc.find(start_heading)
    end = doc.find(end_heading, start + 1)
    if start < 0 or end < 0:
        raise AssertionError("找不到 " + start_heading + " 到 " + end_heading + " 的區段")
    return doc[start:end]


class IoPrerequisiteTests(unittest.TestCase):
    """#313：8.17.2 的數字與事實對得上程式；8.18 的 V-0.6b 指到真的 10 分鐘測試。

    數字來源：5 秒＝`auth_db.DEFAULT_BUSY_TIMEOUT`；32＝`http._FRAMING_PERMITS`；
    16＝`sshsig.DEFAULT_CONCURRENCY`。268.1 筆/秒、p99 0.553 秒、StoreBusy 8 次、
    p99 2.564 秒是 #310 的量測紀錄（報告在團隊目錄，不在 repo），測試無法重現，
    只保證文件寫的是那個來源，不驗數字本身。
    """

    def setUp(self):
        self.doc = read_doc()
        self.section = between(self.doc, "### 8.17.2 ", "### 8.18 上線前清單")
        self.table_section = between(self.doc, "### 8.18 上線前清單", "### 8.19 ")

    def test_numbers_match_code(self):
        self.assertEqual(auth_db.DEFAULT_BUSY_TIMEOUT, 5.0)
        self.assertIn("5.0 秒", self.section)
        self.assertEqual(lgwd_http._FRAMING_PERMITS, 32)
        self.assertIn("**32**", self.section)
        self.assertEqual(sshsig.DEFAULT_CONCURRENCY, 16)
        self.assertIn("**16**", self.section)
        self.assertLessEqual(sshsig.DEFAULT_CONCURRENCY, lgwd_http._FRAMING_PERMITS)

    def test_auth_db_facts_match_code(self):
        source = (REPO_ROOT / "lgwd" / "auth_db.py").read_text(encoding="utf-8")
        for token in ("journal_mode=WAL", "synchronous=FULL", "BEGIN IMMEDIATE", "threading.Lock"):
            self.assertIn(token, source, token)
            self.assertIn(token, self.section, token)
        self.assertIn("busy_timeout", self.section)
        self.assertIn("503 `auth_unavailable`", self.section)
        self.assertIn("fail-closed", self.section)

    def v06b_row(self):
        rows = [line for line in self.table_section.splitlines() if line.startswith("| V-0.6b |")]
        self.assertEqual(len(rows), 1, "8.18 要有且只有一列 V-0.6b")
        return rows[0]

    def test_v06b_points_at_the_ten_minute_test(self):
        source = (REPO_ROOT / "tests" / "test_main_auth_rate_263.py").read_text(encoding="utf-8")
        self.assertIn("LGW_SLOW_TESTS", source)
        self.assertEqual(re.findall(r"stop\.wait\((\d+)\)", source), ["600"])
        # 文件說 repo 內的 C-611 只有 30 秒版：這一句要守住。
        auth_runtime = (REPO_ROOT / "tests" / "test_auth_runtime.py").read_text(encoding="utf-8")
        self.assertIn("DURATION = 30.0", auth_runtime)
        row = self.v06b_row()
        self.assertIn("tests/test_main_auth_rate_263.py", row)
        for needle in ("C-612", "LGW_SLOW_TESTS=1", "stop.wait(600)", "C-611", "30 秒",
                       "/proc/pressure/io", "findmnt", "≥ 50 筆/秒", "p99 < 1 秒",
                       "不得只調大 `busy_timeout`"):
            self.assertIn(needle, row, needle)

    def test_v06b_pins_tmpdir_and_device_steps(self):
        # 高-1：C-612 的庫建在 $TMPDIR，量測前要對齊到認證庫的檔案系統。
        row = self.v06b_row()
        for needle in ("跑之前", "TMPDIR", "tempfile.TemporaryDirectory()", "/var/lib/llmgateway",
                       "findmnt -T /var/lib/llmgateway", "tmpfs", "PrivateTmp=true"):
            self.assertIn(needle, row, needle)
        # 低-1：lsblk 要指定裝置並追到底層；OPTIONS 貼之前要看。
        for needle in ("lsblk -s", "overlay", "upperdir", "LVM", "OPTIONS"):
            self.assertIn(needle, row, needle)

    def test_v06b_503_count_is_upper_bound_and_pid_kill_only(self):
        row = self.v06b_row()
        # 量測欄的說明（上界是 503 的總和，不是 StoreBusy 數）；判準欄還有一個「上界」，所以要比整句。
        self.assertIn("**總和，是上界**", row)
        self.assertIn("只 kill 那個 pid", row)
        self.assertIn("不得", row)
        self.assertIn("當成 StoreBusy 數", row)

    def test_v06b_reads_auth_busy_status_file(self):
        # #312 之後 lgwd 每 60 秒寫 .auth-busy.status；V-0.6b 要寫怎麼讀它，舊的「沒有獨立計數」不得留著。
        row = self.v06b_row()
        self.assertNotIn("目前沒有獨立計數", row)
        self.assertIn(".auth-busy.status", row)
        self.assertIn("每 60 秒", self.doc)
        for needle in ("每分鐘讀暫存 state dir", "結束前讀", "暫存目錄結束即刪"):
            self.assertIn(needle, row, needle)
        # 寫在 C-612 的暫存 state dir 裡（測試的 state 目錄），而不是正式庫目錄。
        source = (REPO_ROOT / "lgwd" / "auth_paths.py").read_text(encoding="utf-8")
        self.assertIn('BUSY_STATUS_NAME = ".auth-busy.status"', source)

    def test_measurements_match_source_table(self):
        # 數字來源：#310 報告（不在 repo；這張表是測試自己的來源副本）。
        # 改掉文件的數字而不改這張表，測試會紅；要改數字兩邊都要改，並回到來源確認。
        measurements = (
            "268.1 筆/秒",       # REPORT-310-impl §5.1，load1 3.20
            "0.553 秒",
            "102.4 筆/秒",       # PM-RUN-305.txt:39，load1 36.95（skip 訊息）
            "0.988 秒",
            "load1 3.20",
            "load1 36.95",
            "1.2%",              # 0.988 離 1 秒只差 1.2%（REPORT-310 §0.2.3）
            "0～8 次",           # io2 三次：busy 8／0／6
            "1.73～2.56 秒",     # io2 三次：p99 2.564／1.730／1.945
            "一份測試加 2 個 writer",
            "高負載下已貼近判準",
        )
        for item in measurements:
            self.assertIn(item, self.section, item)
        self.assertNotIn("唯一的真實路徑實測", self.section)

    def test_cited_code_lines_name_the_expected_code(self):
        # 文件引的每個 lgwd 行號都要在這張表裡，而且該行真的含預期字樣：
        # 行號漂移或寫錯檔案，這裡會紅。
        expected = {
            ("lgwd/auth_db.py", 32): "DEFAULT_BUSY_TIMEOUT = 5.0",
            ("lgwd/auth_db.py", 169): "threading.Lock()",
            ("lgwd/auth_db.py", 183): "PRAGMA journal_mode=WAL",
            ("lgwd/auth_db.py", 186): "PRAGMA synchronous=FULL",
            ("lgwd/auth_db.py", 188): "int(row[0]) != 2",
            ("lgwd/auth_db.py", 220): "deadline = time.monotonic() + self.busy_timeout",
            ("lgwd/auth_db.py", 255): "BEGIN IMMEDIATE",
            ("lgwd/auth.py", 754): "except (StoreError, OSError):",
            ("lgwd/auth.py", 755): "return unavailable(REASON_STORE_UNAVAILABLE)",
            ("lgwd/http.py", 106): "_FRAMING_PERMITS = 32",
            ("lgwd/http.py", 107): "_PERMIT_WAIT_SECONDS = 1.0",
            ("lgwd/http.py", 416): "self._framing_sem.acquire(timeout=_PERMIT_WAIT_SECONDS)",
            ("lgwd/http.py", 420): "REASON_BUSY",
            ("lgwd/sshsig.py", 84): "DEFAULT_CONCURRENCY = 16",
        }
        for (path, line), keyword in expected.items():
            lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(lines), line, path)
            self.assertIn(keyword, lines[line - 1], f"{path}:{line} 應含 {keyword!r}")
        cited = set()
        for text in (self.section, self.v06b_row()):
            for path, line in re.findall(r"(lgwd/[\w/]+\.py):(\d+)", text):
                cited.add((path, int(line)))
        self.assertTrue(cited, "8.17.2 與 V-0.6b 應該有 lgwd 行號引用")
        self.assertEqual(cited - set(expected), set(), "文件引了表外的行號")

    def test_v06b_503_and_busy_wording_matches_section(self):
        # 中-2：兩條 503 路徑都要寫進 8.17.2，「不要只調大 busy_timeout」要涵蓋兩條。
        for needle in ("REASON_BUSY", "1 秒", "號誌", "StoreBusy", "不要只調大 `busy_timeout`",
                       "不與重 fsync 的負載共用"):
            self.assertIn(needle, self.section, needle)

    def test_rollout_v0_list_matches_818(self):
        # 中-3：auth-rollout.md 的 V-0 清單與 8.18 的 ID 一致，白話版的項數也一致。
        rollout = (REPO_ROOT / "docs" / "auth-rollout.md").read_text(encoding="utf-8")
        id_pattern = re.compile(r"^\| (V-0\.\w+|P-D4) \|", re.MULTILINE)
        deploy_ids = set(id_pattern.findall(self.table_section))
        rollout_ids = set(id_pattern.findall(rollout))
        self.assertEqual(rollout_ids, deploy_ids)
        self.assertIn("V-0.6b", rollout_ids)
        v0_count = len([i for i in deploy_ids if i.startswith("V-0.")])
        self.assertEqual(v0_count, 8)
        self.assertIn(f"V-0 在正式主機做了 {v0_count} 項檢查", rollout)

    def test_behavior_open_rw_sets_wal_and_full_sync(self):
        # 行為支：不靠字串。改掉 PRAGMA 會讓 configure() 的讀回驗證失敗，open_rw 就丟例外。
        with tempfile.TemporaryDirectory() as d:
            trust = str(Path(d) / "trust.db")
            replay = str(Path(d) / "replay.db")
            auth_db.create_stores(trust, replay)
            db = auth_db.open_rw(replay)
            try:
                self.assertEqual(db.conn.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(
                    str(db.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")
                self.assertEqual(db.busy_timeout, 5.0)
            finally:
                db.close()


class V05TransientServiceTests(unittest.TestCase):
    """#320：8.18.1 的 transient service 範本與 unit 屬性一致；步驟指到真的工具與斷言。

    範本是文件，不是程式：這裡只比對「共用屬性」的值與 unit 檔相同，以及步驟裡的
    關鍵字樣（工具名、三項斷言、停損）還在。`ReadWritePaths` 與 `WorkingDirectory`
    刻意不同，不比對。
    """

    SHARED = ("User", "Group", "TasksMax", "MemoryMax", "LimitNOFILE", "OOMPolicy",
              "TimeoutStopSec", "KillMode", "NoNewPrivileges", "ProtectSystem", "PrivateTmp")

    def setUp(self):
        self.doc = read_doc()
        self.section = between(self.doc, "### 8.18.1 ", "### 8.19 ")

    def _unit_service_props(self):
        unit = (REPO_ROOT / "deploy" / "units" / "llmgateway.service").read_text(encoding="utf-8")
        props = {}
        for line in unit.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line and not line.startswith("["):
                key, _, value = line.partition("=")
                props[key] = value
        return props

    def _template_props(self):
        start = self.section.index("```bash")
        block = self.section[start + len("```bash"):self.section.index("```", start + 7)]
        joined = block.replace("\\\n", " ")
        # 範本第一條指令到 lgwd 參數為止；<...> 佔位那一段不是 shell 字，shlex 會把它當一個字。
        tokens = shlex.split(joined)
        props = {}
        for token in tokens:
            if token.startswith("--property="):
                key, _, value = token[len("--property="):].partition("=")
                props[key] = value
        return tokens, props

    def test_shared_properties_match_unit_file(self):
        unit = self._unit_service_props()
        _, template = self._template_props()
        for key in self.SHARED:
            self.assertIn(key, template, key)
            self.assertEqual(template[key], unit[key], key)

    def test_template_uses_exec_type_and_no_restart(self):
        tokens, template = self._template_props()
        self.assertIn("--service-type=exec", tokens)
        self.assertEqual(template["Restart"], "no")
        self.assertEqual(self._unit_service_props()["Type"], "exec")

    def test_section_names_the_tools_and_the_three_assertions(self):
        for needle in ("tests.v0_prepare", "tests.v0_load_client", "--auth-per-key-max 1000000",
                       "cgroup.procs", "20 秒", "503", "connection_refused", "停損",
                       "沒有驗到的", "不碰 `/opt/llmgateway/current`"):
            self.assertIn(needle, self.section, needle)

    def _step(self, prefix):
        lines = [line for line in self.section.splitlines() if line.startswith(prefix)]
        self.assertEqual(len(lines), 1, prefix)
        return lines[0]

    def test_prepare_step_says_three_lines_with_key_id(self):
        step = self._step("2. 準備：")
        self.assertIn("它印出三行", step)
        for name in ("lgwd-args:", "client-env:", "client-key-id:"):
            self.assertIn(name, step)

    def test_prepare_step_has_inline_chown_between_prepare_and_start(self):
        # #329：名冊要 root 擁有；指令在步驟 2 之後、步驟 3 之前，用行內程式碼（不另開 bash 區塊，
        # 以免 _template_props 取錯區塊）。
        self._step("2. 準備：")
        self.assertIn("note: registry.json must be owned by root", self.section)
        self.assertIn("stdout 仍是三行", self.section)
        lines = [l for l in self.section.splitlines()
                 if "sudo chown root:root /var/lib/llmgateway-v0/run/registry.json" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("```", lines[0])
        pos = self.section.index(lines[0])
        self.assertLess(self.section.index("2. 準備："), pos)
        self.assertLess(pos, self.section.index("3. 啟動："))

    def test_owner_bullet_says_file_perms_only_and_dir_is_advice(self):
        # #329 fix1：lgwd 只檢查名冊檔、不檢查目錄；目錄權限寫成安全建議，不寫成判準。
        bullet = [l for l in self.section.splitlines() if "名冊擁有者（啟動前必做）" in l]
        self.assertEqual(len(bullet), 1, bullet)
        line = bullet[0]
        for needle in ("**檔案**不得 group／world 可寫（0644 即可）", "只檢查名冊檔",
                       "不檢查所在目錄", "安全建議", "不是 lgwd 的判準"):
            self.assertIn(needle, line, needle)
        self.assertNotIn("名冊目錄與檔案不得", line)

    def test_start_step_stops_on_degraded_or_exit_78(self):
        # #329：起來後 journal 不得有 auth event=degraded；degraded 或 exit 78 就停、取 journal 回報。
        line = self._step("3. 啟動：")
        for needle in ("`auth event=degraded`", "journalctl -u lgw-v0 --no-pager", "exit 78",
                       "cannot start authentication", "停損", "不開負載"):
            self.assertIn(needle, line, needle)

    def test_v02_row_also_requires_no_degraded_line(self):
        # #329：V-0.2 判準多一句 degraded 檢查。
        table = between(self.doc, "### 8.18 上線前清單", "### 8.18.1 ")
        rows = [l for l in table.splitlines() if l.startswith("| V-0.2 |")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("日誌沒有 self-test 失敗，也沒有 `auth event=degraded`", rows[0])

    def test_load_start_has_no_subshell_and_checks_pid_with_ps(self):
        # 中-1：cd 與背景指令分行；$! 取到的是 python 本身；用 ps 確認。
        self.assertIn("LOAD_PID=$!", self.section)
        self.assertIn('ps -p "$LOAD_PID" -o cmd=', self.section)
        self.assertIn("python3 -m tests.v0_load_client", self.section)
        load_lines = [l for l in self.section.splitlines() if "tests.v0_load_client --duration" in l]
        self.assertEqual(len(load_lines), 1)
        self.assertNotIn("&&", load_lines[0])
        self.assertTrue(load_lines[0].rstrip().endswith("&"), load_lines[0])
        self.assertIn("不用 pkill", self.section)

    def test_assertion1_separates_cut_errors_by_stop_time(self):
        # 中-2：切斷類錯誤只能出現在停止之後；errors 原樣進紀錄。
        line = self._step("1. **期間無 503")
        for needle in ("`errors`", "`connection_reset`", "`bad_status_line`", "`error_window`",
                       "`STOP_EPOCH`", "不得早於", "停止之前出現任一項即未達"):
            self.assertIn(needle, line, needle)
        self.assertIn("STOP_EPOCH=$(date +%s.%N)", self.section)

    def test_assertion2_is_the_20_second_sentence_with_monotonic_method(self):
        # 低-2：「20 秒內結束」鎖在斷言 2 那一句，判準是單調時間差。
        line = self._step("2. **主行程 20 秒內結束**")
        for needle in ("ExecMainExitTimestampMonotonic", "/proc/uptime", "T0",
                       "≤ 20", "值為 `0`", "未在真機驗"):
            self.assertIn(needle, line, needle)
        self.assertIn("T0=$(cut -d' ' -f1 /proc/uptime)", self.section)
        self.assertNotIn("InactiveEnterTimestamp ", self._step("2. **主行程 20 秒內結束"))

    def test_assertion3_locks_full_cgroup_path_and_absent_counts_as_empty(self):
        # 低-1：完整字串；檔案不存在也算空。
        line = self._step("3. **cgroup 沒有殘留**")
        self.assertIn("/sys/fs/cgroup/system.slice/lgw-v0.service/cgroup.procs", line)
        self.assertIn("檔案不存在", line)
        self.assertIn("也算空", line)

    def test_v06_rows_untouched_by_v05_section(self):
        table = between(self.doc, "### 8.18 上線前清單", "### 8.18.1 ")
        self.assertEqual(len([l for l in table.splitlines() if l.startswith("| V-0.6b |")]), 1)
        self.assertEqual(len([l for l in table.splitlines() if l.startswith("| V-0.6 |")]), 1)

class V06MeasurementTests(unittest.TestCase):
    """V-0.6 改用 scope 量測（#319）：量 C-612 的 scope 組與服務 cgroup 基線，分開標示、判準不放寬。

    推論的依據是 `tests/test_main_auth_rate_263.py` 的 `subprocess.Popen`（子行程繼承 pytest 的 cgroup）；
    這裡只鎖文件的字樣與判準數字，不驗 systemd 的行為（沙箱沒有 systemd）。
    """

    def setUp(self):
        self.doc = read_doc()
        self.table_section = between(self.doc, "### 8.18 上線前清單", "### 8.19 ")
        self.procedure = between(self.doc, "#### V-0.6 量測步驟", "### 8.19 ")
        self.rollout = (REPO_ROOT / "docs" / "auth-rollout.md").read_text(encoding="utf-8")

    def row(self, prefix):
        rows = [line for line in self.table_section.splitlines() if line.startswith(prefix)]
        self.assertEqual(len(rows), 1, prefix)
        return rows[0]

    def test_v06_names_scope_method_and_service_baseline(self):
        row = self.row("| V-0.6 |")
        for needle in ("systemd-run --scope", "systemctl show -p ControlGroup --value", "pids.peak",
                       "memory.peak", "兩組、分開標示", "A＝scope 組", "B＝服務 cgroup 基線",
                       "含客戶端的上界", "subprocess.Popen", "scope 方法不依賴這個推論"):
            self.assertIn(needle, row, needle)

    def test_v06_criteria_not_loosened(self):
        # 判準：A＋B 的和比 256／768M／2048（保守上界）；字樣不得放寬。
        row = self.row("| V-0.6 |")
        for needle in ("用 A＋B 的和比較", "`pids.peak` ≤ 256", "`memory.peak` ≤ 768M",
                       "fd 高峰 ≤ 2048", "不放寬", "不放寬到 `infinity`"):
            self.assertIn(needle, row, needle)

    def code(self):
        # 只比對 fenced 程式碼區塊（指令範本）；說明文字裡提到 MemoryMax 等不算。
        return "\n".join(line for block in code_blocks(self.procedure) for line in block)

    def test_v06_procedure_code_pins_commands(self):
        code = self.code()
        for needle in (
            "sudo systemd-run --scope", "--unit=\"$SCOPE\"", "-p MemoryAccounting=yes",
            "-p TasksAccounting=yes", "-p IOAccounting=yes", "runuser -u llmgw",
            "env TMPDIR=<TMPDIR>", "LGW_SLOW_TESTS=1", "PYTHONDONTWRITEBYTECODE=1",
            "python3 -m unittest -v tests.test_main_auth_rate_263",
            "/proc/self/cgroup", "cat $CG/pids.peak", "$CG/memory.peak",
            "ControlGroup=", "git -C <repo> rev-parse HEAD",
            '-lt "$3"', "A_WAIT", "/proc/$p/fd", "sudo sh -c", "sampler", "trap ",
            "fdmax", "pids.current", "memory.current",
        ):
            self.assertIn(needle, code, needle)
        # 不設上限（見說明文）；pytest 不再是指令的一部分。
        for banned in ("MemoryMax", "TasksMax", "pytest"):
            self.assertNotIn(banned, code, banned)
        # TMPDIR 的 findmnt 檢查是步驟文字（與 V-0.6b 一致），不在指令範本裡。
        self.assertIn("findmnt -T /var/lib/llmgateway", self.procedure)

    def test_v06_every_systemctl_targets_the_scope(self):
        # 高-1：ControlGroup 與 is-active 一律對到 scope 單元，不能是無後綴的名字（會被補成 .service）。
        code = self.code()
        # 範本裡的 sh -c 會收 "$1"，呼叫端必須把 "$SCOPE"（已含 .scope）傳進去。
        lines = [l for l in self.procedure.splitlines()
                 if "systemctl" in l and ("ControlGroup" in l or "is-active" in l)]
        self.assertGreaterEqual(len(lines), 3)  # 取樣 CG、內層 CG、收尾 is-active
        for line in lines:
            self.assertTrue(any(s in line for s in ("$SCOPE", ".scope", '"$1"')), line)
        self.assertIn('sh "$SCOPE"', code)
        self.assertIn('sampler "$SCOPE"', code)
        self.assertNotIn('"$UNIT"', self.procedure)
        self.assertIn('SCOPE=lgw-v06-c612-$(date +%s).scope', code)

    def test_v06_sampler_output_is_written_under_sudo(self):
        # 中-1：取樣輸出在 sudo 內寫（`>> "$2"` 在 sh -c 的字串裡），不是呼叫者的 >>。
        code = self.code()
        # 每個 sudo sh -c 取樣腳本裡的每一個 >> 都要寫到 "$2"（外層傳進來的輸出檔）。
        scripts = re.findall(r"sudo sh -c '(.*?)' sampler", code, re.S)
        self.assertEqual(len(scripts), 2, "A 與 B 兩個取樣腳本")
        for script in scripts:
            script = script.replace('\\"', '"').replace("\\$", "$")
            targets = re.findall(r">>\s*([^\s;]+)", script)
            self.assertTrue(targets, script)
            for target in targets:
                self.assertEqual(target, '"$2"', script)
        self.assertNotIn('sampler "$SCOPE" >>', code)
        self.assertNotIn('sampler-b "$SVC" >>', code)

    def test_v06_b_group_has_fallback_and_kill_by_pid(self):
        # 中-3：B 組也有 *.current 取樣退路；收尾只 kill 記下的 pid。
        procedure = self.procedure
        self.assertIn("sampler-b", procedure)
        self.assertIn("memory.current", procedure)
        self.assertIn("kill <B 的 pid>", procedure)
        self.assertNotIn("5.3 以上", procedure)
        self.assertIn("兩者不存在就都用取樣", self.row("| V-0.6 |"))

    def test_v06_sampler_has_timeout_and_fd_max(self):
        # 低-1：scope 起不來要逾時結束並記錄；低-2：fd 取最大值並記錄。
        code = self.code()
        self.assertIn('not started within $3s', code)
        self.assertIn('${A_WAIT:-60}', code)
        self.assertIn("max fd=", code)
        self.assertIn("max fd", self.procedure)

    def test_v06_sampler_call_lines_are_exact(self):
        # 低-新3：取樣呼叫的整行要完全等於這裡寫的，任何後加的重導（呼叫者的 >>、2>&1）都會紅。
        code_lines = [l.strip() for l in self.code().splitlines()]
        for ending in ("""' sampler "$SCOPE" "$OUT" "${A_WAIT:-60}" &""", """' sampler-b "$SVC" "$OUT" &"""):
            self.assertTrue(any(l.endswith(ending) for l in code_lines), ending)

    def test_v06_steps_run_in_order(self):
        # 中-新1：B 取樣與前一次讀 peak 在前景 scope（步驟 4）之前；觸發在步驟 4 執行期間；收尾前再讀。
        proc = self.procedure
        markers = ["\n2. **A 組取樣**", "\n3. **B 組", "\n4. **C-612 跑在", "\n5. **觸發",
                   "\n6. **收尾前再讀", "\n7. **收尾**"]
        positions = [proc.index(m) for m in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("前一次讀 peak", proc[positions[1]:positions[2]])
        self.assertIn("另開一個終端", proc[positions[3]:positions[4]])
        self.assertIn("步驟 4 執行期間", proc[positions[3]:positions[4]])
        self.assertIn("步驟 4 結束後", proc[positions[4]:positions[5]])
        self.assertIn("**順序很重要**", proc)
        self.assertIn("立刻", proc)
        self.assertIn("A_WAIT", proc)

    def test_v06b_no_longer_leaves_inference_open(self):
        row = self.row("| V-0.6b |")
        self.assertIn("scope 方法不依賴該推論", row)
        self.assertIn("可以共用同一次 C-612 執行", row)
        self.assertIn("同一個 scope", row)
        self.assertIn(".auth-busy.status", row)
        self.assertIn("結束前讀（暫存目錄結束即刪）", row)
        self.assertNotIn("要確認", row)
        self.assertNotIn("推論：", row)

    def test_rollout_v06_row_matches_scope_method(self):
        line = next(l for l in self.rollout.splitlines() if l.startswith("| V-0.6 |"))
        self.assertIn("systemd-run --scope", line)
        self.assertIn("服務 cgroup 基線", line)
        self.assertIn("A＋B", line)
        self.assertIn("不放寬", line)
        self.assertNotIn("整個 cgroup 的", line)


class RolloutV0ResultTests(unittest.TestCase):
    """2026-11-26 V-0 結果回寫 auth-rollout.md：§5 每列都有結果、缺口有編號、兩個 sha 在。

    只鎖文件的字樣（狀態欄不是「未」、#331／#332 在對的列、sha 前 7 碼在），不驗真機結果本身。
    P-D4 還沒做，所以維持「未」。
    """

    def setUp(self):
        self.rollout = (REPO_ROOT / "docs" / "auth-rollout.md").read_text(encoding="utf-8")
        self.section5 = between(self.rollout, "## 5. V-0 前置", "## 6. ")

    def rows(self):
        return [l for l in self.section5.splitlines() if re.match(r"^\| (V-0\.\w+|P-D4) \|", l)]

    def row(self, prefix):
        rows = [l for l in self.rows() if l.startswith(prefix)]
        self.assertEqual(len(rows), 1, prefix)
        return rows[0]

    @staticmethod
    def status(line):
        return [c.strip() for c in line.strip().strip("|").split("|")][2]

    def test_every_v0_row_has_a_result_not_pending(self):
        # 中-1：§5 的 V-0 列（8 列）狀態都不是「未」，且寫了結果日期。
        v0_rows = [l for l in self.rows() if l.startswith("| V-0.")]
        self.assertEqual(len(v0_rows), 8)
        for line in v0_rows:
            status = self.status(line)
            self.assertFalse(status.startswith("未"), line)
            self.assertIn("2026-11-26", status, line)

    def test_p_d4_stays_pending(self):
        self.assertEqual(self.status(self.row("| P-D4 |")), "未")

    def test_v06_row_names_gap_331(self):
        row = self.row("| V-0.6 |")
        self.assertIn("A＋B 判準 PASS；B 組未量到 claude 子行程（#331，not planned）", self.status(row))
        self.assertIn("pids.peak", row)
        self.assertIn("memory.peak", row)

    def test_v07_row_names_gap_332(self):
        row = self.row("| V-0.7 |")
        self.assertIn("寫入 PASS；名冊讀取＝開 warn 固定步驟（2）（#332 結案）", self.status(row))
        self.assertIn("名冊", row)
        self.assertIn("固定步驟", row)

    def test_v04_records_v008_rc1_and_v009_rc0(self):
        # 中-2：current＝v0.8.0 時 verify rc=1（check-notify 不存在），換 v0.9.0 路徑後 rc=0。
        row = self.row("| V-0.4 |")
        for needle in ("lgw-v0.8.0", "rc=1", "check-notify", "v0.9.0", "verify_rc=0"):
            self.assertIn(needle, row, needle)

    def test_v05_records_stop_timing_deviation(self):
        # 中-2：斷言 2 改量 systemctl stop 耗時，要明寫偏差，不能假裝照文件做。
        row = self.row("| V-0.5 |")
        self.assertIn("偏差", self.status(row))
        self.assertIn("偏差", row)
        self.assertIn("systemctl stop", row)
        self.assertIn("ExecMainExitTimestampMonotonic", row)

    def test_header_records_both_shas_tag_and_service_version(self):
        # 中-3：目標 tag、文件修正 sha（前 7 碼）與正式服務版本都要寫在表前。
        header = between(self.rollout, "**V-0 結果（2026-11-26）**", "| # | 內容")
        for needle in ("lgw-v0.9.0", "4090b20", "#329", "32352b5", "lgw-v0.8.0"):
            self.assertIn(needle, header, needle)

    def test_current_service_status_paragraph(self):
        # 換版補充：§5 前有「正式服務現況」，寫 lgw-v0.9.0、auth off，且 warn／enforce 沒開。
        status = between(self.rollout, "**正式服務現況（2026-11-26 UTC）**", "**V-0 結果（2026-11-26）**")
        for needle in ("lgw-v0.9.0", "auth 維持 `off`", "warn 與 enforce **都還沒開**",
                       "P-D4、X1、X2 **都還沒完成**", "check-notify"):
            self.assertIn(needle, status, needle)
        self.assertNotIn("已開 warn", status)
        self.assertNotIn("warn 已開", self.rollout)

    def test_s7_has_no_stale_pre_v0_sentences(self):
        # W1：§7 白話版在 V-0 之後要是現況；過時的「請您授權 V-0」「V-0 通過後，下一步」不得留。
        for stale in ("請您授權 V-0", "V-0 通過後，下一步", "現在要請您決定的，是「可不可以在正式主機上做上線前的實機檢查（V-0）」"):
            self.assertNotIn(stale, self.rollout, stale)
        s7 = between(self.rollout, "## 7. 白話版", "**做完之後仍然存在的風險**")
        self.assertIn("warn", s7)
        self.assertIn("#332", s7)
        self.assertIn("#331", s7)
        self.assertIn("lgw-v0.9.0", s7)
        self.assertIn("不會打開強制認證", s7)

    def test_fixed_warn_steps_list_is_pinned(self):
        # 11-27 補充：開 warn 的固定步驟（6 步）在 §5 末，鎖住關鍵字樣；check-notify 不設通知。
        section5 = self.section5
        head = section5.index("**開 warn 的固定步驟**")
        steps = section5[head:]
        for needle in ("lgw-registry-sync", "root", "group／world 可寫", "systemd-run", "User=llmgw",
                       "ProtectSystem=strict", "/etc/llmgateway/auth-registry.json", "[ -r ",
                       "P-D4、X1／X2", "再次單次授權", "warn", "restart", "degraded",
                       "journalctl -t llmgwctl-check", "reason=missing"):
            self.assertIn(needle, steps, needle)
        for n in range(1, 7):
            self.assertRegex(steps, rf"(?m)^{n}\. ", n)
        self.assertNotIn("唯一的觀察來源", self.rollout)

    def test_fixed_step6_splits_check_and_caller_observation(self):
        # W4：6a 看 check 的 journal（-t llmgwctl-check）；6b 看 lgwd 的日誌（-u llmgateway、reason=missing、outcome=pass）。
        lines = self.section5.splitlines()
        six_a = next(l for l in lines if "(6a)" in l)
        six_b = next(l for l in lines if "(6b)" in l)
        self.assertIn("journalctl -t llmgwctl-check -p warning", six_a)
        self.assertIn("journalctl -u llmgateway", six_b)
        self.assertIn("reason=missing", six_b)
        self.assertIn("outcome=pass", six_b)
        self.assertIn("auth mode=… outcome=… reason=… route=… principal=… count=…", six_b)

    def test_fixed_step6a_checks_timer_unit_and_journal(self):
        # #353：6a 只看 llmgwctl-check 的輸出不夠（啟動失敗、逾時、被殺可能沒有該 identifier）；
        # 要同時看 timer 排程、unit 最近結果、unit 自己的 journal，三條指令的名稱要與單元檔一致。
        lines = self.section5.splitlines()
        start = next(i for i, l in enumerate(lines) if "(6a)" in l)
        end = next(i for i, l in enumerate(lines) if "(6b)" in l)
        block = "\n".join(lines[start:end])
        for needle in ("(6a-1)", "(6a-2)", "(6a-3)",
                       "systemctl list-timers llmgateway-check.timer",
                       "systemctl show -p Result,ExecMainStatus,ExecMainExitTimestamp llmgateway-check.service",
                       "journalctl -u llmgateway-check.service",
                       "Result=timeout", "Result=signal", "ExecMainStatus=2", "ExecMainStatus=1"):
            self.assertIn(needle, block, needle)
        self.assertIn("journalctl -t llmgwctl-check -p warning", block)
        for unit in ("llmgateway-check.timer", "llmgateway-check.service"):
            self.assertTrue((REPO_ROOT / "deploy" / "units" / unit).is_file(), unit)

    def test_step2_has_sandbox_command_marked_unverified(self):
        # N3：sandbox 指令要標明「seed 裝上後才實跑」（#337 把它移到 install seed 之後）。
        for needle in ("systemd-run --wait --pipe --collect", "-p User=llmgw", "-p Group=llmgw",
                       "-p NoNewPrivileges=true", "-p ProtectSystem=strict", "-p PrivateTmp=true",
                       "[ -r /etc/llmgateway/auth-registry.json ]", "stat -c %s",
                       "seed 裝上後才實跑"):
            self.assertIn(needle, self.section5, needle)
        self.assertNotIn("未在名冊存在時驗過", self.rollout)

    def test_decisions_are_settled_not_open(self):
        # 11-27 決定：check-notify 不設通知；#331 not planned；#332 結案。不寫成待決或待補。
        self.assertIn("不設通知", self.rollout)
        self.assertIn("#331，not planned", self.rollout)
        self.assertIn("#332 結案", self.rollout)
        for open_word in ("二選一", "通知路徑待決", "待補", "待決"):
            self.assertNotIn(open_word, self.rollout, open_word)

    def test_s5_tail_says_p_d4_and_pre_warn_gates(self):
        # W2：§5 結尾要寫開 warn 前還要 P-D4、X1、X2 與再次授權，不能只寫「下一步是開 warn」。
        lines = self.section5.splitlines()
        last_row = max(i for i, l in enumerate(lines) if l.startswith("|"))
        tail = "\n".join(lines[last_row + 1:])
        self.assertIn("P-D4", tail)
        self.assertIn("開 warn 前", tail)
        self.assertIn("X1", tail)
        self.assertIn("X2", tail)

    def test_status_time_is_utc_not_mixed_with_jst(self):
        # N2：時區統一；不得出現 JST 的 2026-11-27 日期。
        self.assertNotIn("2026-11-27", self.rollout)

    def test_no_stale_not_yet_v0_sentences(self):
        # 中-4：沒有任何一行同時提到 V-0 與「還沒做」（舊的「V-0 還沒做」句子都已改掉）。
        for line in self.rollout.splitlines():
            if "V-0" in line:
                self.assertNotIn("還沒做", line, line)
        self.assertNotIn("要等 V-0 通過之後", self.rollout)
        self.assertNotIn("V-0 全部通過之後才", self.rollout)

    def test_warn_enforce_and_gates_not_claimed_done(self):
        # 不得把 warn 觀察期、enforce、P-D4、X1／X2 寫成已成立。
        self.assertNotIn("已開 warn", self.rollout)
        self.assertNotIn("已切 enforce", self.rollout)
        self.assertNotIn("已切換 enforce", self.rollout)
        ledger = [l for l in self.rollout.splitlines() if l.startswith("| ③ 正式主機 warn 觀察期 |")]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].split("|")[2].strip(), "未")
        conclusion = [l for l in self.rollout.splitlines() if l.startswith("| 結論 |")]
        self.assertEqual(conclusion[0].split("|")[2].strip(), "未")
        self.assertEqual(self.status(self.row("| P-D4 |")), "未")
        for xrow in ("| X1 |", "| X2 |"):
            line = next(l for l in self.rollout.splitlines() if l.startswith(xrow))
            self.assertEqual(line.split("|")[6].strip(), "未", line)



class FirstRegistryOrderTests(unittest.TestCase):
    """#337：首次名冊順序鎖在文件裡（seed → init → 授權 → warn＋install seed → MLP 名冊）。

    只鎖文件字樣與步驟先後，不跑真機。改了順序或拿掉關鍵字，這裡要紅。
    """

    def setUp(self):
        self.rollout = (REPO_ROOT / "docs" / "auth-rollout.md").read_text(encoding="utf-8")
        self.steps = between(self.rollout, "**開 warn 的固定步驟**", "## 6. ")
        self.doc = read_doc()
        self.s82 = between(self.doc, "### 8.2 首次啟用", "### 8.3 ")
        self.s820 = between(self.doc, "### 8.20 從 MLP 清單同步名冊", "### 8.21 ")
        self.first_use = self.s820[self.s820.index("#### 首次使用"):]

    def test_seed_is_generation_1_and_local_admin_only(self):
        for needle in ("seed", "generation 1", "local-admin", "llmgwctl-local-admin"):
            self.assertIn(needle, self.steps, needle)
        self.assertIn("seed 名冊", self.s82)
        self.assertIn("generation 1", self.s82)
        self.assertIn("generation 1", self.first_use)

    def test_order_init_gates_warn_install_then_mlp(self):
        # 順序：init 在前；授權（再次單次授權）在 warn 之前；install seed 在 warn 之後；
        # MLP 的 --allow-new 在 install seed 之後。
        pos = self.steps.index
        init = pos("auth-store init --confirm")
        gate = pos("再次單次授權")
        warn = pos("LGWD_AUTH_MODE=warn")
        seed_install = pos("auth-registry install <seed>")
        allow_new = pos("--allow-new")
        self.assertLess(init, gate)
        self.assertLess(gate, warn)
        self.assertLess(warn, seed_install)
        self.assertLess(seed_install, allow_new)

    def test_loaded_hash_is_the_plan_baseline(self):
        self.assertIn("loaded_hash", self.steps)
        self.assertIn("loaded_hash", self.first_use)
        self.assertIn("registry_not_loaded", self.first_use)

    def test_allow_new_and_unknown_list_path_are_stated(self):
        self.assertIn("--allow-new", self.steps)
        self.assertIn("--allow-new", self.first_use)
        self.assertIn("預設上限 2", self.first_use)
        self.assertNotIn("目前未知", self.steps)
        self.assertNotIn("目前未知", self.first_use)
        for part in (self.steps, self.first_use):
            self.assertIn("list-tunnel-keys.sh --json", part)
            self.assertIn("Variables: read", part)
            self.assertIn("worker", part)

    def test_w1_restart_degraded_is_expected_before_install(self):
        # W1：seed 還沒 install 時 restart 後的 area=registry degraded 是預期；判準移到 install 之後。
        self.assertIn("area=registry kind=registry", self.steps)
        self.assertIn("預期", self.steps)
        self.assertNotIn("restart，看日誌無 degraded", self.steps)
        self.assertIn("沒有新的 `area=registry` degraded", self.steps)
        self.assertLess(self.steps.index("預期"), self.steps.index("沒有新的 `area=registry` degraded"))

    def test_n1_worker_403_and_no_worker_listing_are_pinned(self):
        # N1：403（設計）與「不能在 worker 上取清單／跑」在 auth-rollout 與 deploy 各鎖一次。
        self.assertIn("403（設計）", self.steps)
        self.assertIn("不能在 worker 上取清單", self.steps)
        self.assertIn("403", self.first_use)
        self.assertIn("不能在 worker 上跑", self.first_use)

    def test_n1_key_id_upper_bound_is_64(self):
        self.assertIn("≤64", self.first_use)
        self.assertIn("{0,63}", self.first_use)

    def test_n3_no_result_ok_claim_left(self):
        # N3：腳本實際檢查 loaded_hash 與 loaded generation；文件不再寫 result=ok。
        self.assertNotIn("result=ok", self.steps)
        self.assertNotIn("result=ok", self.s820)
        self.assertIn("loaded_generation", self.steps)
        self.assertIn("loaded_generation", self.s820)

    def test_fixed_steps_still_numbered_one_to_six_with_6a_6b(self):
        for n in range(1, 7):
            self.assertRegex(self.steps, rf"(?m)^{n}\. ", n)
        self.assertIn("(6a)", self.steps)
        self.assertIn("(6b)", self.steps)

    def test_deploy_82_points_to_auth_rollout_seed_steps(self):
        self.assertIn("docs/auth-rollout.md", self.s82)
        self.assertIn("§5", self.s82)


class KeyIdVectorTests(unittest.TestCase):
    """#337 補充 2：key ID 規則與測試向量。文件寫的值要等於 lgw-registry-sync 實算的結果。

    腳本沒有 .py 副檔名，用 importlib 以路徑載入；只呼叫 `_parse_public_key`、`_fp12`。
    向量一變（文件或腳本算法變了）這裡就紅。
    """

    PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAZ6BM9XEM+FOeS1stgh0YAH2U4YdfTdx/6yS6w1B/+O"
    FINGERPRINT = "SHA256:vzs1gYCu8dp09iou3qFMOfppx1ZZX82uZvIuNyPizmU"
    FP12 = "bf3b358180ae"
    NAME = "mlp-fh-l-default-45799726300"
    KEY_ID = "mlp-mlp-fh-l-default-45799726300-bf3b358180ae"

    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util
        path = str(REPO_ROOT / "ops-scripts" / "lgw-registry-sync")
        loader = importlib.machinery.SourceFileLoader("lgw_registry_sync_vec", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        cls.sync = module

    def setUp(self):
        self.doc = read_doc()
        self.s820 = between(self.doc, "### 8.20 從 MLP 清單同步名冊", "### 8.21 ")

    def test_program_computes_the_documented_vector(self):
        raw = self.sync._parse_public_key(self.PUBLIC_KEY, self.NAME)
        fp12 = self.sync._fp12(raw)
        self.assertEqual(fp12, self.FP12)
        key_id = self.sync.OURS_PREFIX + self.NAME + "-" + fp12
        self.assertEqual(key_id, self.KEY_ID)
        self.assertEqual(len(key_id), 45)

    def test_fp12_is_the_fingerprint_prefix_per_the_stated_equivalence(self):
        # 等價算法：fingerprint 去 SHA256:、補 =、base64 解碼、hex 取前 12。
        import base64
        b64 = self.FINGERPRINT[len("SHA256:"):]
        blob = base64.b64decode(b64 + "=" * (-len(b64) % 4))
        self.assertEqual(blob.hex()[:12], self.FP12)

    def test_key_id_fits_registry_id_pattern_and_length_budget(self):
        from lgwd.auth_registry import ID_PATTERN
        self.assertIsNotNone(ID_PATTERN.match(self.KEY_ID))
        self.assertLessEqual(len(self.KEY_ID), 64)
        self.assertLessEqual(len(self.NAME), 47)

    def test_vector_table_rows_are_exact(self):
        # 每一列要逐列等於期望值（只靠子字串會被「改了別列、其他列還含原值」騙過）。
        rows = {}
        for line in self.s820.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0] in {"public_key", "fingerprint", "fp12", "name",
                                                "principal", "key ID"}:
                rows[cells[0]] = cells[1]
        self.assertEqual(rows, {
            "public_key": "`" + self.PUBLIC_KEY + "`",
            "fingerprint": "`" + self.FINGERPRINT + "`",
            "fp12": "`" + self.FP12 + "`",
            "name": "`" + self.NAME + "`",
            "principal": "`mlp-mlp-fh-l-default-45799726300`",
            "key ID": "`" + self.KEY_ID + "`（45 字元）",
        })

    def test_doc_states_the_rule_and_the_vector(self):
        for needle in (
            "principal＝`mlp-<name>`", "key ID＝`mlp-<name>-<fp12>`",
            "SHA-256 前 12 個**小寫** hex", "[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
            "不得寫死", "換鑰＝換 key ID",
            self.PUBLIC_KEY, self.FINGERPRINT, self.FP12, self.NAME, self.KEY_ID,
            "（45 字元）",
        ):
            self.assertIn(needle, self.s820, needle)


if __name__ == "__main__":
    unittest.main()
