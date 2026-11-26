"""品質報告的設定與呼叫端 workflow 的檢查（#325）。

``.github/taskquality.json``（config v2）是這個 PR 的本體；呼叫端 workflow
``.github/workflows/quality-report.yml`` 由 PMO 另外推，所以 repo 裡可能還沒有它，
那一段用 ``skipUnless(檔案存在)``。

只用標準函式庫：JSON 用 ``json``，workflow 是 YAML，不裝 pyyaml，改用文字比對。
"""

import json
import os
import re
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, ".github", "taskquality.json")
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "quality-report.yml")

QMETRICS_SHA = "524cd44bb0286edf5082c7cdc16b2bb8bde262ad"
CODE_CRON = "17 3 * * 1"
TASK_CRON = "41 2 * * *"


def _load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _read_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return f.read()


def _job_block(text, name):
    """回傳 ``jobs:`` 底下名為 ``name`` 的那一段文字（到下一個同層 job 或檔尾為止）。"""
    lines = text.splitlines()
    start = lines.index(f"  {name}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        # 頂層 key 或同層的下一個 job（兩格縮排）才是這段的結尾
        if re.match(r"^\S|^  \S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _job_permissions(block):
    """回傳 job 層 ``permissions:`` 的 {權限: 值}；沒有這一段就是空的。"""
    perms = {}
    inside = False
    for line in block.splitlines():
        if re.match(r"^    permissions:\s*$", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^      (\S+):\s*(\S+)\s*$", line)
            if not m:
                break
            perms[m.group(1)] = m.group(2)
    return perms


def _task_if(text):
    """task job 的 ``if:``（``>-`` 折疊寫法），空白正規化後的整段運算式。"""
    lines = _job_block(text, "task").splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^    if: >-\s*$", l))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("      "):
            break
        body.append(line)
    return " ".join(" ".join(body).split())


TASK_IF = (
    "(github.event_name == 'issues' && contains(fromJSON('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), "
    "github.event.issue.author_association)) "
    "|| github.event.schedule == '41 2 * * *' "
    "|| (github.event_name == 'workflow_dispatch' && inputs.tickets != '')"
)


class TaskQualityConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = _load_config()

    def test_config_is_valid_json_object(self):
        self.assertIsInstance(self.config, dict)

    def test_identity_fields(self):
        self.assertEqual(self.config["kind"], "taskquality-config")
        self.assertEqual(self.config["schema_version"], 2)

    def test_ci_gating_is_off(self):
        self.assertIs(self.config["ci_required"], False)
        self.assertIsNone(self.config["ci_workflows"])

    def test_started_label(self):
        self.assertEqual(self.config["started_label"], "狀態:進行中")

    def test_verdict_pattern_matches_review_comment(self):
        pattern = re.compile(self.config["verdict_pattern"])
        m = pattern.match("審查：anthropic/claude-sonnet-5-5 結論：可以 merge（head b4d38dd）")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("role"), "審查")
        self.assertEqual(m.group("model"), "anthropic/claude-sonnet-5-5")
        self.assertEqual(m.group("verdict"), "可以 merge")
        self.assertEqual(m.group("head"), "b4d38dd")

    def test_verdict_pattern_rejects_comment_without_head(self):
        pattern = re.compile(self.config["verdict_pattern"])
        self.assertIsNone(pattern.match("審查：x 結論：可以 merge（細節）"))

    def test_pass_and_fail_verdicts(self):
        self.assertIn("可以 merge", self.config["pass_verdicts"])
        self.assertIn("要修", self.config["fail_verdicts"])


@unittest.skipUnless(os.path.isfile(WORKFLOW_PATH), "呼叫端 workflow 由 PMO 另推，repo 內還沒有")
class QualityReportWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = _read_workflow()

    def test_uses_and_qmetrics_ref_pin_the_same_sha(self):
        uses_lines = [l for l in self.text.splitlines() if re.match(r"^\s+uses:", l)]
        self.assertEqual(len(uses_lines), 2, uses_lines)
        for line in uses_lines:
            self.assertTrue(line.endswith(f"@{QMETRICS_SHA}"), line)
        # code 與 task 兩個 job 各一處
        self.assertEqual(
            re.findall(r"qmetrics-ref:\s*(\S+)", self.text),
            [QMETRICS_SHA, QMETRICS_SHA],
        )
        # 整份檔只出現這一個 40 碼 SHA，沒有殘留舊的 pin
        self.assertEqual(set(re.findall(r"\b[0-9a-f]{40}\b", self.text)), {QMETRICS_SHA})

    def test_schedule_crons_match_job_conditions_verbatim(self):
        on_block = self.text.split("\njobs:", 1)[0]
        crons = re.findall(r'^\s+- cron: "([^"]+)"', on_block, re.MULTILINE)
        self.assertEqual(sorted(crons), sorted([CODE_CRON, TASK_CRON]))

        code_cmp = re.findall(r"github\.event\.schedule == '([^']+)'", _job_block(self.text, "code"))
        task_cmp = re.findall(r"github\.event\.schedule == '([^']+)'", _job_block(self.text, "task"))
        self.assertEqual(code_cmp, [CODE_CRON])
        self.assertEqual(task_cmp, [TASK_CRON])

    def test_task_job_if_expression_is_exact(self):
        # 整段 if 運算式（空白正規化後）逐字比對，守門不能移到 || 之外或被弱化
        self.assertEqual(_task_if(self.text), TASK_IF)

    def test_code_matrix_targets(self):
        block = _job_block(self.text, "code")
        m = re.search(r"target:\s*\[([^\]]*)\]", block)
        self.assertIsNotNone(m)
        targets = [t.strip() for t in m.group(1).split(",")]
        self.assertEqual(sorted(targets), sorted(["lgwd", "lgwctl", "lgw"]))

    def test_code_job_src_tests_target_inputs(self):
        lines = {l.strip() for l in _job_block(self.text, "code").splitlines()}
        self.assertIn("src: ${{ matrix.target }}", lines)
        self.assertIn("tests: tests", lines)
        self.assertIn("target: ${{ matrix.target }}", lines)

    def test_job_permissions_are_read_only_and_minimal(self):
        self.assertEqual(
            _job_permissions(_job_block(self.text, "task")),
            {"contents": "read", "issues": "read", "pull-requests": "read", "actions": "read"},
        )
        self.assertEqual(_job_permissions(_job_block(self.text, "code")), {"contents": "read"})
        self.assertNotIn("write", self.text)

    def test_top_level_permissions_are_empty(self):
        top = [l for l in self.text.splitlines() if re.match(r"^permissions:", l)]
        self.assertEqual(top, ["permissions: {}"])


if __name__ == "__main__":
    unittest.main()
