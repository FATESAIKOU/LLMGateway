import os
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = os.path.join(REPO_ROOT, "deploy", "install.sh")
RUN_WRAPPER = os.path.join(REPO_ROOT, "deploy", "llmgateway-run")
SERVICE_UNIT = os.path.join(REPO_ROOT, "deploy", "llmgateway.service")

TOOLS_LIST = [
    "mkdir", "mktemp", "chmod", "mv", "ln", "rm", "cat", "date",
    "whoami", "cp", "bash", "head", "grep", "ls", "readlink",
    "cmp", "stat", "dirname", "sort", "tail"
]


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sandbox = self.tmpdir.name
        self.home = os.path.join(self.sandbox, "home")
        os.makedirs(self.home, exist_ok=True)

        self.fakebin = os.path.join(self.sandbox, "fakebin")
        self.fakebin_nopy = os.path.join(self.sandbox, "fakebin_nopy")
        self.fakebin_nosys = os.path.join(self.sandbox, "fakebin_nosys")
        self.tools = os.path.join(self.sandbox, "tools")
        os.makedirs(self.fakebin, exist_ok=True)
        os.makedirs(self.fakebin_nopy, exist_ok=True)
        os.makedirs(self.fakebin_nosys, exist_ok=True)
        os.makedirs(self.tools, exist_ok=True)

        for t in TOOLS_LIST:
            p = shutil.which(t)
            if not p:
                raise RuntimeError(f"missing system tool {t}")
            os.symlink(p, os.path.join(self.tools, t))

        self._create_fake_tools()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_fake_tools(self):
        git_script = """#!/usr/bin/env bash
echo "git $*" >> "${SANDBOX:?}/git-argv.log"
if [[ "${GIT_FAIL:-0}" == "1" ]]; then
    echo "fake git: forced failure" >&2
    exit 1
fi
if [[ "${1:-}" != "clone" ]]; then
    echo "fake git: only clone" >&2
    exit 1
fi
dest="${@: -1}"
mkdir -p "$dest/policy" "$dest/deploy"
printf '{}\\n' > "$dest/policy/default.json"
if [[ -n "${WRAPPER_MARKER:-}" ]]; then
    printf '#!/bin/sh\\n# %s\\nexit 0\\n' "$WRAPPER_MARKER" > "$dest/deploy/llmgateway-run"
else
    cat "${REAL_WRAPPER:?}" > "$dest/deploy/llmgateway-run"
fi
chmod +x "$dest/deploy/llmgateway-run"
if [[ -n "${SERVICE_MARKER:-}" ]]; then
    printf '[Unit]\\n# %s\\n' "$SERVICE_MARKER" > "$dest/deploy/llmgateway.service"
else
    cat "${REAL_SERVICE:?}" > "$dest/deploy/llmgateway.service"
fi
exit 0
"""
        py_script = """#!/usr/bin/env bash
echo "python3 $*" >> "${SANDBOX:?}/python-argv.log"
if [[ "${1:-}" == "--version" ]]; then
    echo "Python ${FAKE_PY_VERSION:-3.12.0}"
    exit 0
fi
exit 0
"""
        sys_script = """#!/usr/bin/env bash
echo "systemctl $*" >> "${SANDBOX:?}/systemctl-argv.log"
if [[ "$*" == *"is-active"* ]]; then
    if [[ "${SYS_NOBUS:-0}" == "1" ]]; then
        echo "fake systemctl: no bus" >&2
        exit 1
    fi
    if [[ "${SYS_STATE:-inactive}" == "active" ]]; then
        exit 0
    else
        exit 3
    fi
fi
exit 0
"""
        curl_script = """#!/usr/bin/env bash
echo "curl $*" >> "${SANDBOX:?}/curl-argv.log"
printf '%s' "${CURL_CODE:-401}"
exit "${CURL_RC:-0}"
"""
        for name, content in [
            ("git", git_script),
            ("python3", py_script),
            ("systemctl", sys_script),
            ("curl", curl_script),
        ]:
            target = os.path.join(self.fakebin, name)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(target, 0o755)

        # nopy: symlink git, systemctl, curl
        for name in ["git", "systemctl", "curl"]:
            os.symlink(os.path.join(self.fakebin, name), os.path.join(self.fakebin_nopy, name))

        # nosys: symlink git, python3, curl
        for name in ["git", "python3", "curl"]:
            os.symlink(os.path.join(self.fakebin, name), os.path.join(self.fakebin_nosys, name))

    def run_install(self, args, home=None, env_overrides=None, path_mode="full"):
        if home is None:
            home = self.home
        env = {
            "SANDBOX": self.sandbox,
            "HOME": home,
            "REAL_WRAPPER": RUN_WRAPPER,
            "REAL_SERVICE": SERVICE_UNIT,
        }
        if path_mode == "full":
            env["PATH"] = f"{self.fakebin}:{self.tools}"
        elif path_mode == "nopy":
            env["PATH"] = f"{self.fakebin_nopy}:{self.tools}"
        elif path_mode == "nosys":
            env["PATH"] = f"{self.fakebin_nosys}:{self.tools}"
        if env_overrides:
            env.update(env_overrides)
        cmd = ["bash", INSTALL_SH] + list(args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True)

    def read_log(self, name):
        log_path = os.path.join(self.sandbox, f"{name}-argv.log")
        if not os.path.exists(log_path):
            return ""
        with open(log_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_prerequisites_and_file_modes(self):
        self.assertTrue(os.path.isfile(INSTALL_SH))
        self.assertTrue(os.access(INSTALL_SH, os.X_OK))

        self.assertTrue(os.path.isfile(RUN_WRAPPER))
        self.assertTrue(os.access(RUN_WRAPPER, os.X_OK))

        self.assertTrue(os.path.isfile(SERVICE_UNIT))
        with open(SERVICE_UNIT, "r", encoding="utf-8") as f:
            svc_content = f.read()
        self.assertIn("ExecStart=%h/.local/bin/llmgateway-run", svc_content)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", svc_content)
        self.assertNotIn(".mylinuxpool", svc_content)

        # bash -n syntax checks
        res = subprocess.run(["bash", "-n", INSTALL_SH], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = subprocess.run(["bash", "-n", RUN_WRAPPER], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_first_install(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        env_file = os.path.join(self.home, ".config", "llmgateway", "llmgateway.env")
        self.assertTrue(os.path.isfile(env_file))
        with open(env_file, "r", encoding="utf-8") as f:
            env_content = f.read()
        self.assertIn('LGW_API_HOSTS="127.0.0.1"', env_content)
        self.assertIn('LGW_API_PORT="8480"', env_content)
        self.assertIn('LGW_BOARD_PORT="8481"', env_content)
        self.assertIn('LGW_USAGE="1"', env_content)

        state_dir = os.path.join(self.home, ".local", "state", "llmgateway")
        self.assertTrue(os.path.isdir(state_dir))
        self.assertEqual(stat.S_IMODE(os.stat(state_dir).st_mode), 0o700)

        keys_file = os.path.join(state_dir, "keys.env")
        self.assertTrue(os.path.isfile(keys_file))
        self.assertEqual(os.path.getsize(keys_file), 0)
        self.assertEqual(stat.S_IMODE(os.stat(keys_file).st_mode), 0o600)

        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        current_link = os.path.join(share_dir, "current")
        self.assertTrue(os.path.islink(current_link))
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        tag_file = os.path.join(current_link, ".lgw-tag")
        self.assertTrue(os.path.isfile(tag_file))
        with open(tag_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "lgw-v0.1.0")

        installed_run = os.path.join(self.home, ".local", "bin", "llmgateway-run")
        self.assertTrue(os.path.isfile(installed_run))
        self.assertTrue(os.access(installed_run, os.X_OK))

        installed_svc = os.path.join(self.home, ".config", "systemd", "user", "llmgateway.service")
        self.assertTrue(os.path.isfile(installed_svc))

        git_log = self.read_log("git")
        self.assertIn("clone --depth 1 --branch lgw-v0.1.0 https://github.com/FATESAIKOU/LLMGateway", git_log)

        sys_log = self.read_log("systemctl")
        self.assertIn("daemon-reload", sys_log)
        self.assertIn("enable --now llmgateway.service", sys_log)

        leftovers = [f for f in os.listdir(share_dir) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_second_install_preserves_env_and_keys(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        env_file = os.path.join(self.home, ".config", "llmgateway", "llmgateway.env")
        custom_env = 'LGW_API_HOSTS="127.0.0.1 10.9.9.9"\nLGW_API_PORT="9999"\nLGW_BOARD_PORT="9998"\nLGW_USAGE="0"\n'
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(custom_env)

        keys_file = os.path.join(self.home, ".local", "state", "llmgateway", "keys.env")
        custom_keys = "SOME_KEY=fake-not-real\n"
        with open(keys_file, "w", encoding="utf-8") as f:
            f.write(custom_keys)

        res2 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home], env_overrides={"SYS_STATE": "active"})
        self.assertEqual(res2.returncode, 0, res2.stderr)

        with open(env_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_env)
        with open(keys_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), custom_keys)

        sys_log = self.read_log("systemctl")
        self.assertIn("restart llmgateway.service", sys_log)

    def test_reinstall_same_tag_reuses_without_git_clone(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        tag_dir = os.path.join(self.home, ".local", "share", "llmgateway", "lgw-v0.1.0")
        marker = os.path.join(tag_dir, "MARKER-reuse")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("marker")

        git_log_file = os.path.join(self.sandbox, "git-argv.log")
        if os.path.exists(git_log_file):
            os.remove(git_log_file)

        res2 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home], env_overrides={"SYS_STATE": "active"})
        self.assertEqual(res2.returncode, 0, res2.stderr)

        self.assertFalse(os.path.exists(git_log_file))
        self.assertTrue(os.path.exists(marker))

    def test_git_clone_failure_leaves_no_leftovers_and_keeps_current(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        res2 = self.run_install(["--tag", "lgw-v0.2.0", "--home", self.home], env_overrides={"GIT_FAIL": "1"})
        self.assertNotEqual(res2.returncode, 0)

        current_link = os.path.join(share_dir, "current")
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))
        self.assertFalse(os.path.exists(os.path.join(share_dir, "lgw-v0.2.0")))
        leftovers = [f for f in os.listdir(share_dir) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_symlink_atomic_switch(self):
        res1 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res1.returncode, 0, res1.stderr)

        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        current_link = os.path.join(share_dir, "current")
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        res2 = self.run_install(["--tag", "lgw-v0.2.0", "--home", self.home])
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.2.0"))

        res3 = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res3.returncode, 0, res3.stderr)
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        leftovers = [f for f in os.listdir(share_dir) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_check_three_states_with_tag(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        # 0: Active, tag matches, probe 401
        res_ok = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "401", "CURL_RC": "0"}
        )
        self.assertEqual(res_ok.returncode, 0, res_ok.stdout + res_ok.stderr)

        curl_log = self.read_log("curl")
        self.assertIn("/v1/board", curl_log)
        self.assertNotIn("Authorization", curl_log)

        # 1: Service inactive
        res_inact = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "inactive"}
        )
        self.assertEqual(res_inact.returncode, 1)

        # 1: Tag mismatch
        tag_file = os.path.join(self.home, ".local", "share", "llmgateway", "lgw-v0.1.0", ".lgw-tag")
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("other-tag\n")
        res_mismatch = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "401", "CURL_RC": "0"}
        )
        self.assertEqual(res_mismatch.returncode, 1)
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("lgw-v0.1.0\n")

        # 1: Board returns non-401 (e.g. 200 or 500)
        res_non401 = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "200", "CURL_RC": "0"}
        )
        self.assertEqual(res_non401.returncode, 1)

        res_500 = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "500", "CURL_RC": "0"}
        )
        self.assertEqual(res_500.returncode, 1)

        # 2: curl connection failure
        res_curl_fail = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_RC": "7"}
        )
        self.assertEqual(res_curl_fail.returncode, 2)

        # 2: curl timeout
        res_curl_to = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_RC": "28"}
        )
        self.assertEqual(res_curl_to.returncode, 2)

        # 2: systemctl fails / no bus
        res_sys_nobus = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "SYS_NOBUS": "1", "CURL_CODE": "401"}
        )
        self.assertEqual(res_sys_nobus.returncode, 2)

        # 2: systemctl unavailable in PATH
        res_no_sys = self.run_install(
            ["--check", "--tag", "lgw-v0.1.0", "--home", self.home],
            path_mode="nosys"
        )
        self.assertEqual(res_no_sys.returncode, 2)

    def test_check_without_tag(self):
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0, res.stderr)

        # Active + 401 -> 0 even without --tag
        res_ok = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "401", "CURL_RC": "0"}
        )
        self.assertEqual(res_ok.returncode, 0)

        # Does not check version tag: even if .lgw-tag differs, it passes
        tag_file = os.path.join(self.home, ".local", "share", "llmgateway", "lgw-v0.1.0", ".lgw-tag")
        with open(tag_file, "w", encoding="utf-8") as f:
            f.write("different-version-not-checked\n")
        res_ok_diff = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "401", "CURL_RC": "0"}
        )
        self.assertEqual(res_ok_diff.returncode, 0)

        # Service inactive -> 1
        res_inact = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "inactive"}
        )
        self.assertEqual(res_inact.returncode, 1)

        # Probe returns 500 -> 1
        res_500 = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_CODE": "500", "CURL_RC": "0"}
        )
        self.assertEqual(res_500.returncode, 1)

        # curl failure -> 2
        res_curl_fail = self.run_install(
            ["--check", "--home", self.home],
            env_overrides={"SYS_STATE": "active", "CURL_RC": "7"}
        )
        self.assertEqual(res_curl_fail.returncode, 2)

    def test_missing_or_old_python(self):
        # Missing python3 in PATH
        res_nopy = self.run_install(
            ["--tag", "lgw-v0.1.0", "--home", self.home],
            path_mode="nopy"
        )
        self.assertNotEqual(res_nopy.returncode, 0)
        self.assertIn("python3", res_nopy.stderr + res_nopy.stdout)

        # python3 < 3.10
        res_oldpy = self.run_install(
            ["--tag", "lgw-v0.1.0", "--home", self.home],
            env_overrides={"FAKE_PY_VERSION": "3.9.18"}
        )
        self.assertNotEqual(res_oldpy.returncode, 0)
        self.assertIn("3.10", res_oldpy.stderr + res_oldpy.stdout)

    def test_tag_format_validation(self):
        bad_tags = [
            "v1.0.0", "lgw-v1.0", "lgw-v1.0.0-test", "lgw-v1.0.0.0",
            "", "invalid", "lgw-v99999.1.1", "lgw-v1.2.3;echo", "lgw-v01.02.03-beta"
        ]
        for tag in bad_tags:
            res = self.run_install(["--tag", tag, "--home", self.home])
            self.assertEqual(res.returncode, 2, f"Expected 2 for tag={tag!r}, got {res.returncode}")

            res_chk = self.run_install(["--check", "--tag", tag, "--home", self.home])
            self.assertEqual(res_chk.returncode, 2, f"Expected 2 for check with tag={tag!r}")

        # Install mode without --tag exits 2
        res_notag = self.run_install(["--home", self.home])
        self.assertEqual(res_notag.returncode, 2)

    def test_files_installed_from_new_version_deploy(self):
        # Wrapper and unit must come from the checked out tag's deploy/, not from the running script's dir
        wrapper_marker = "TEST_WRAPPER_V2_MARKER_99"
        service_marker = "TEST_SERVICE_V2_MARKER_88"

        res = self.run_install(
            ["--tag", "lgw-v0.2.0", "--home", self.home],
            env_overrides={
                "WRAPPER_MARKER": wrapper_marker,
                "SERVICE_MARKER": service_marker,
            }
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        installed_run = os.path.join(self.home, ".local", "bin", "llmgateway-run")
        with open(installed_run, "r", encoding="utf-8") as f:
            run_content = f.read()
        self.assertIn(wrapper_marker, run_content)

        installed_svc = os.path.join(self.home, ".config", "systemd", "user", "llmgateway.service")
        with open(installed_svc, "r", encoding="utf-8") as f:
            svc_content = f.read()
        self.assertIn(service_marker, svc_content)

    def test_version_retention_only_two_versions_and_current_never_deleted(self):
        share_dir = os.path.join(self.home, ".local", "share", "llmgateway")
        current_link = os.path.join(share_dir, "current")

        def version_dirs():
            return sorted([d for d in os.listdir(share_dir) if d != "current" and not d.startswith(".")])

        # 1. Install v0.1.0 -> 1 version
        res = self.run_install(["--tag", "lgw-v0.1.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.1.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.1.0"))

        # 2. Install v0.2.0 -> 2 versions
        res = self.run_install(["--tag", "lgw-v0.2.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.1.0", "lgw-v0.2.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.2.0"))

        # 3. Install v0.3.0 -> only 2 versions kept (v0.2.0 and v0.3.0); v0.1.0 removed
        res = self.run_install(["--tag", "lgw-v0.3.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.2.0", "lgw-v0.3.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.3.0"))

        # 4. Install v0.4.0 -> only 2 versions kept (v0.3.0 and v0.4.0); v0.2.0 removed
        res = self.run_install(["--tag", "lgw-v0.4.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.3.0", "lgw-v0.4.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.4.0"))

        # 5. Reinstall current v0.4.0 -> current is never deleted; both kept
        res = self.run_install(["--tag", "lgw-v0.4.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.3.0", "lgw-v0.4.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.4.0"))

        # 6. Rollback to v0.3.0 -> current switches to v0.3.0, both kept
        res = self.run_install(["--tag", "lgw-v0.3.0", "--home", self.home])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(version_dirs(), ["lgw-v0.3.0", "lgw-v0.4.0"])
        self.assertEqual(os.readlink(current_link), os.path.join(share_dir, "lgw-v0.3.0"))


if __name__ == "__main__":
    unittest.main()
