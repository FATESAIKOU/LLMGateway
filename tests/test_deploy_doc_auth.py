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
from lgwd import main as lgwd_main
from lgwd.auth_registry import OPS, parse_registry
from lgwd.policy import AGENTS, ROLES

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "deploy.md"

CHAPTER_HEADING = "## 8. 應用層認證"
APPENDIX_HEADING = "## 附錄：舊版（v0.1.x，遷移完成後不再使用）"

#: a～q 十七節的固定標題（測試逐一斷言；改標題等於改契約）。
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
    "### 8.18 上線前清單",
    "### 8.19 定時檢查（timer）（r）",
    "### 8.20 從 MLP 清單同步名冊（外圍腳本 `lgw-registry-sync`，#292）",
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
                     "auth-degraded", "check-timer"):
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


if __name__ == "__main__":
    unittest.main()
