import os
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "deploy", "remote-deploy.sh")
CANARY = "CANARY-deploy-value-9z8y7x"


def read(path):
    with open(path, "rb") as f:
        return f.read()


def run(stdin: bytes, state_dir: str, reload_cmd: str = "true", command: str | None = "keys", installer: str | None = None):
    env = {"PATH": os.environ["PATH"], "HOME": state_dir, "LGW_STATE_DIR": state_dir, "LGW_RELOAD_CMD": reload_cmd}
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    if installer is not None:
        env["LGW_INSTALLER"] = installer
    return subprocess.run(["sh", SCRIPT], input=stdin, env=env, capture_output=True, timeout=10)


GOOD = (
    f"CLAUDE_PERSONAL_OAUTH_TOKEN={CANARY}\n"
    "CLAUDE_COMPANY_OAUTH_TOKEN=x2\n"
    "OPENROUTER_API_KEY=x3\n"
    "OPENCODE_ZEN_API_KEY=\n"
    "OLLAMA_API_KEY=x5\n"
).encode()


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.keys = os.path.join(self.dir, "keys.env")

    def assertNoLeak(self, proc):
        self.assertNotIn(CANARY.encode(), proc.stdout + proc.stderr)

    def test_writes_file_0600_and_reloads(self):
        marker = os.path.join(self.dir, "reloaded")
        proc = run(GOOD, self.dir, f"touch {marker}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(self.keys), GOOD)
        self.assertEqual(stat.S_IMODE(os.stat(self.keys).st_mode), 0o600)
        self.assertTrue(os.path.exists(marker))
        self.assertNoLeak(proc)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".keys.env.")], [])

    def _assert_rejected_keeps_old(self, payload: bytes, code: int):
        run(GOOD, self.dir)
        before = read(self.keys)
        proc = run(payload, self.dir)
        self.assertEqual(proc.returncode, code, proc.stderr)
        self.assertEqual(read(self.keys), before)
        self.assertNoLeak(proc)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".keys.env.")], [])

    def test_unknown_key_rejected(self):
        self._assert_rejected_keeps_old(f"EVIL_KEY={CANARY}\n".encode(), 4)

    def test_not_key_value_rejected(self):
        self._assert_rejected_keeps_old(f"{CANARY}\n".encode(), 4)

    def test_duplicate_rejected(self):
        self._assert_rejected_keeps_old(b"OPENROUTER_API_KEY=a\nOPENROUTER_API_KEY=b\n", 4)

    def test_value_with_space_rejected(self):
        self._assert_rejected_keeps_old(f"OPENROUTER_API_KEY={CANARY} x\n".encode(), 4)

    def test_empty_rejected(self):
        self._assert_rejected_keeps_old(b"", 3)

    def test_too_large_rejected(self):
        self._assert_rejected_keeps_old(b"#" * 70000, 3)

    def test_missing_key_name_rejected(self):
        self._assert_rejected_keeps_old(b"CLAUDE_PERSONAL_OAUTH_TOKEN=a\nOPENROUTER_API_KEY=b\nOLLAMA_API_KEY=c\n", 4)

    def test_value_with_base64_padding_accepted(self):
        payload = GOOD.replace(b"OLLAMA_API_KEY=x5", b"OLLAMA_API_KEY=abc+/de==")
        proc = run(payload, self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(self.keys), payload)

    def test_script_and_keystore_agree_on_key_names(self):
        from lgwd.keys import KEY_NAMES
        payload = "".join(f"{name}=v{i}\n" for i, name in enumerate(KEY_NAMES.values())).encode()
        proc = run(payload, self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_reload_failure_reported_but_file_written(self):
        proc = run(GOOD, self.dir, "false")
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(read(self.keys), GOOD)
        self.assertNoLeak(proc)

    def _fake_installer(self):
        log = os.path.join(self.dir, "installer.argv")
        path = os.path.join(self.dir, "fake-install.sh")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n')
        os.chmod(path, 0o755)
        return path, log

    def test_unknown_or_missing_command_rejected(self):
        for command in (None, "", "rm -rf /", "keys extra", "upgrade", "sh", "KEYS"):
            proc = run(GOOD, self.dir, command=command)
            self.assertEqual(proc.returncode, 2, (command, proc.stderr))
            self.assertFalse(os.path.exists(self.keys))

    def test_upgrade_valid_tag_runs_installer(self):
        installer, log = self._fake_installer()
        proc = run(b"", self.dir, command="upgrade lgw-v0.1.0", installer=installer)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(log), b"--tag\nlgw-v0.1.0\n")

    def test_upgrade_missing_installer_rejected(self):
        proc = run(b"", self.dir, command="upgrade lgw-v0.1.0", installer=os.path.join(self.dir, "nope.sh"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"installer not found", proc.stderr)

    def test_upgrade_invalid_tags_rejected(self):
        installer, log = self._fake_installer()
        bad = ["lgw-v0.1", "lgw-v0.1.0;rm -rf /", "lgw-v0.1.0 x", "../lgw-v0.1.0", "v0.1.0",
               "lgw-v0.1.0\nrm -rf /", "lgw-v0.1.0$(id)", "lgw-v99999.1.1"]
        for tag in bad:
            proc = run(b"", self.dir, command=f"upgrade {tag}", installer=installer)
            self.assertEqual(proc.returncode, 2, (tag, proc.stderr))
            self.assertFalse(os.path.exists(log), tag)

    def test_output_written_by_lgwd_keystore_is_readable(self):
        from lgwd.keys import KeyStore
        run(GOOD, self.dir)
        store = KeyStore(self.keys)
        self.assertEqual(store.get("claude-personal"), CANARY)
        self.assertFalse(store.deployed("opencode-zen"))


if __name__ == "__main__":
    unittest.main()
