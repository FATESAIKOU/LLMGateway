import os
import re
import subprocess
import unittest

WORKFLOWS = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows")

CONFIG_MARKER = "cat > ~/.ssh/lgw_ssh_config <<'EOF'"
CONFIG_ITEMS = [
    "Host *",
    "IdentityFile ~/.ssh/lgw_deploy",
    "IdentitiesOnly yes",
    "UserKnownHostsFile ~/.ssh/lgw_known_hosts",
    "GlobalKnownHostsFile /dev/null",
    "StrictHostKeyChecking yes",
    "BatchMode yes",
    "ConnectTimeout 20",
]
CLEANUP_CMD = "rm -f ~/.ssh/lgw_deploy ~/.ssh/lgw_known_hosts ~/.ssh/lgw_ssh_config"


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
        return f.read()


def stripped(text):
    return [line.strip() for line in text.splitlines()]


def ssh_lines(text):
    return [line.strip() for line in text.splitlines() if re.search(r"\bssh -F ", line)]


def config_block(text):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == CONFIG_MARKER)
    out = []
    for line in lines[start + 1:]:
        if line.strip() == "EOF":
            return out
        out.append(line.strip())
    raise AssertionError("unterminated lgw_ssh_config heredoc")


def step_lines(text, name):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    out = []
    for line in lines[start + 1:]:
        if re.match(r"\s*- name: ", line):
            break
        out.append(line.strip())
    return out


def event_keys(text):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "on:")
    events = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            events.append(line.strip())
    return events


class WorkflowChecks:
    name = ""

    def setUp(self):
        self.text = read(self.name)
        self.lines = stripped(self.text)

    def test_ssh_goes_through_config_file(self):
        lines = ssh_lines(self.text)
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertIn("-F ~/.ssh/lgw_ssh_config", line)
        self.assertIn('-J "$JUMP"', line)
        self.assertIn('-p "$target_port"', line)
        self.assertIn('"$target_user_host"', line)
        self.assertNotIn("-i ", line)
        self.assertNotIn("UserKnownHostsFile=", line)
        self.assertNotIn(" -o ", line)

    def test_config_file_has_all_options(self):
        self.assertEqual(config_block(self.text), CONFIG_ITEMS)

    def test_config_file_written_0600_and_checked(self):
        prepare = step_lines(self.text, "Prepare SSH identity")
        self.assertIn("umask 077", prepare)
        self.assertIn("chmod 600 ~/.ssh/lgw_ssh_config", prepare)
        self.assertIn(
            "test -s ~/.ssh/lgw_deploy && test -s ~/.ssh/lgw_known_hosts && test -s ~/.ssh/lgw_ssh_config",
            prepare,
        )

    def test_cleanup_removes_all_three_files(self):
        cleanup = step_lines(self.text, "Remove SSH identity")
        self.assertIn("if: always()", cleanup)
        rm = [line for line in cleanup if line.startswith("run: rm -f ")]
        self.assertEqual(rm, [f"run: {CLEANUP_CMD}"])

    def test_permissions_and_environment(self):
        self.assertIn("permissions: {}", self.lines)
        self.assertIn("environment: llmgateway-deploy", self.lines)

    def test_only_manual_dispatch(self):
        self.assertEqual(event_keys(self.text), ["workflow_dispatch:"])

    def test_header_explains_why_f(self):
        self.assertTrue(any(
            line.strip().startswith("#") and "-F" in line and "跳板" in line
            for line in self.text.splitlines()
        ))

    def test_address_masking_uses_bash_literal_replacement(self):
        m = re.search(r"2>\s*>\((while\s+IFS=\s*read\s+-r\s+line;.*?done)\s*>&2\)", self.text)
        self.assertIsNotNone(m, f"mask loop not found in {self.name}")
        loop = m.group(1)
        self.assertIn('${line//"$jump_host"/[jump]}', loop)
        self.assertIn('${line//"$target_host"/[target]}', loop)
        self.assertNotIn("sed", loop)

        # 抽出這段 bash 實際跑一次，餵 jump[1].example 與 2001:db8::7 這類位址，確認整段都被換掉
        script = f"""
set -euo pipefail
jump_host='jump[1].example'
target_host='2001:db8::7'
{loop}
"""
        sample_input = (
            "ssh: connect to jump[1].example: Connection refused\n"
            "debug1: Next authentication to 2001:db8::7\n"
            "error: jump[1].example and 2001:db8::7 failed\n"
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            input=sample_input,
            text=True,
            capture_output=True,
            check=True,
        )
        output = proc.stdout
        self.assertNotIn("jump[1].example", output)
        self.assertNotIn("2001:db8::7", output)
        self.assertIn("ssh: connect to [jump]: Connection refused\n", output)
        self.assertIn("debug1: Next authentication to [target]\n", output)
        self.assertIn("error: [jump] and [target] failed\n", output)



class DeployKeysWorkflowTests(WorkflowChecks, unittest.TestCase):
    name = "deploy-keys.yml"

    def test_keys_verb_is_last_argument(self):
        self.assertRegex(ssh_lines(self.text)[0], r"\bkeys \\$")


class DeployServiceWorkflowTests(WorkflowChecks, unittest.TestCase):
    name = "deploy-service.yml"

    def test_upgrade_verb_present(self):
        self.assertIn('"upgrade $TAG"', ssh_lines(self.text)[0])


if __name__ == "__main__":
    unittest.main()
