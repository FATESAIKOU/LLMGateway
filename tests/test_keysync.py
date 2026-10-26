"""ops-scripts/key-sync 的測試（v5-plan §1、spec key-vault「拉取與輪替」）。

一律用假的 gh／openssl script（`tests/` 產生的假資料），不連網、不碰真憑證；
另有一個用真 openssl 的往返測試，證明加解密參數與 v5-plan §1.3 一致。

涵蓋：sha 相同不動、正常替換、解密失敗、格式錯、gh 失敗（含 gh 沒登入）、
密碼不出現在 argv／log、逾時、以及 systemd unit／timer 的靜態檢查。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
KEY_SYNC = os.path.join(REPO_ROOT, "ops-scripts", "key-sync")
KEY_SYNC_SERVICE = os.path.join(REPO_ROOT, "ops-scripts", "key-sync.service")
KEY_SYNC_TIMER = os.path.join(REPO_ROOT, "ops-scripts", "key-sync.timer")

PASSWORD = "FakeDecryptPassword_ForTests_0001"  # 假值，只在測試裡用
PASSWORD_ENV = "LLMGW_KEYS_PASSWORD"

KEY_NAMES = {
    "claude-personal": "CLAUDE_PERSONAL_OAUTH_TOKEN",
    "claude-company": "CLAUDE_COMPANY_OAUTH_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_ZEN_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
}


def key_text(prefix: str) -> str:
    lines = ["# 測試用假 key（不是真值）"]
    by_name: dict[str, str] = {}
    for sub, name in KEY_NAMES.items():
        by_name.setdefault(name, f"{prefix}-{sub}")
    for name, value in by_name.items():
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def blob_json(content_b64: str, sha: str, *, wrap: int = 60) -> str:
    """模擬 `gh api .../contents/...` 的回覆（content 是 base64、每 60 字折行）。"""
    wrapped = "\n".join(textwrap.wrap(content_b64, wrap))
    return json.dumps({
        "name": "keys.env.enc",
        "path": "keys.env.enc",
        "sha": sha,
        "size": len(content_b64),
        "encoding": "base64",
        "content": wrapped + "\n",
    })


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


SHA_A = hashlib.sha1(b"fake-blob-a").hexdigest()
SHA_B = hashlib.sha1(b"fake-blob-b").hexdigest()
SHA_C = hashlib.sha1(b"fake-blob-c").hexdigest()


class KeySyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.state = os.path.join(self.root, "state")
        self.logs = os.path.join(self.root, "logs")
        self.fakebin = os.path.join(self.root, "fakebin")
        for d in (self.state, self.logs, self.fakebin):
            os.makedirs(d, exist_ok=True)
        self.keys_path = os.path.join(self.state, "keys.env")
        self.sha_file = os.path.join(self.state, ".keysync.sha")

        self._set("gh_mode", "ok")
        self._set("gh_response", blob_json(b64(b"whatever"), SHA_A))
        self._set("plaintext", key_text("test-keysync-new"))

        self.fake_gh = os.path.join(self.fakebin, "gh")
        self.fake_openssl = os.path.join(self.fakebin, "openssl")
        self._write_script(self.fake_gh, self._gh_script())
        self._write_script(self.fake_openssl, self._openssl_script())

    def tearDown(self):
        self.tmp.cleanup()

    # --- 假工具與輔助 ---------------------------------------------------------
    def _write_script(self, path, content):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o755)

    def _set(self, name, value):
        with open(os.path.join(self.state, name), "w", encoding="utf-8") as fh:
            fh.write(value)

    def _get_state(self, name):
        path = os.path.join(self.state, name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def _gh_script(self):
        return """#!/usr/bin/env bash
set -uo pipefail
echo "gh $*" >> "${FAKE_LOG_DIR}/gh-argv.log"
printf 'password_env=%s\\n' "${LLMGW_KEYS_PASSWORD:+set}" >> "${FAKE_LOG_DIR}/gh-env.log"
mode="$(cat "${FAKE_STATE_DIR}/gh_mode" 2>/dev/null || echo ok)"
case "$mode" in
    ok) cat "${FAKE_STATE_DIR}/gh_response" ;;
    auth)
        echo "To get started with GitHub CLI, please run: gh auth login" >&2
        exit 4 ;;
    fail)
        echo "fake gh: some other failure" >&2
        exit 1 ;;
    notfound)
        echo "HTTP 404: Not Found (https://api.github.com/repos/x/y/contents/z)" >&2
        exit 1 ;;
    garbage)
        printf 'not json' ;;
    sleep)
        sleep 5 ;;
esac
exit 0
"""

    def _openssl_script(self):
        return """#!/usr/bin/env bash
set -uo pipefail
echo "openssl $*" >> "${FAKE_LOG_DIR}/openssl-argv.log"
if [[ -n "${LLMGW_KEYS_PASSWORD:-}" ]]; then
    echo "password_env=set" >> "${FAKE_LOG_DIR}/openssl-env.log"
else
    echo "password_env=unset" >> "${FAKE_LOG_DIR}/openssl-env.log"
fi
cat >/dev/null 2>&1 || true
mode="$(cat "${FAKE_STATE_DIR}/openssl_mode" 2>/dev/null || echo ok)"
case "$mode" in
    ok) cat "${FAKE_STATE_DIR}/plaintext" ;;
    bad)
        echo "bad decrypt" >&2
        exit 1 ;;
    nonutf8)
        printf '\\xff\\xfe\\x00' ;;
esac
exit 0
"""

    def _log(self, name):
        path = os.path.join(self.logs, f"{name}-argv.log")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def run_sync(self, args=None, env_overrides=None, password=PASSWORD):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.root,
            "LGWD_KEYS": self.keys_path,
            "LGWD_CODE_DIR": REPO_ROOT,
            "LGWSYNC_GH": self.fake_gh,
            "LGWSYNC_OPENSSL": self.fake_openssl,
            "FAKE_STATE_DIR": self.state,
            "FAKE_LOG_DIR": self.logs,
        }
        if password is not None:
            env[PASSWORD_ENV] = password
        if env_overrides:
            env.update(env_overrides)
        cmd = [sys.executable, KEY_SYNC] + list(args or [])
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)

    def read_keys(self):
        with open(self.keys_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write_keys(self, text, sha=None):
        with open(self.keys_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(self.keys_path, 0o600)
        if sha is not None:
            with open(self.sha_file, "w", encoding="utf-8") as fh:
                fh.write(sha + "\n")

    def leftovers(self):
        return sorted(
            name for name in os.listdir(self.state)
            if name.startswith(".") and name != ".keysync.sha"
        )

    def assert_no_leak(self, res):
        """密碼 MUST NOT 出現在輸出、argv 紀錄或任何產物裡。"""
        parts = []
        for name in sorted(os.listdir(self.logs)):
            with open(os.path.join(self.logs, name), encoding="utf-8") as fh:
                parts.append(fh.read())
        all_logs = "\n".join(parts)
        haystacks = {
            "stdout": res.stdout,
            "stderr": res.stderr,
            "argv logs": all_logs,
            "sha 檔": self._get_state_contents(),
        }
        if os.path.exists(self.keys_path):
            haystacks["keys.env"] = self.read_keys()
        for where, text in haystacks.items():
            self.assertNotIn(PASSWORD, text, f"密碼外洩於 {where}")
            self.assertNotIn(b64(PASSWORD.encode()), text, f"密碼外洩於 {where}")

    def _get_state_contents(self):
        parts = []
        for name in os.listdir(self.state):
            path = os.path.join(self.state, name)
            if os.path.isfile(path) and name in (".keysync.sha",):
                with open(path, "r", encoding="utf-8") as fh:
                    parts.append(fh.read())
        return "\n".join(parts)

    # --- sha 相同時的自我修復（審查 中-1） ---------------------------------
    def test_sha相同但本機key檔不見時重拉修復(self):
        # keys.env 被帶外刪掉（.keysync.sha 還在）：不能因為 blob 沒變就擺著不管，
        # 否則 key 永遠不回來（審查 中-1）
        self.write_keys(key_text("test-keysync-old"), sha=SHA_A)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        os.remove(self.keys_path)

        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.read_keys(), key_text("test-keysync-new"))
        self.assertEqual(stat.S_IMODE(os.stat(self.keys_path).st_mode), 0o600)
        self.assertIn("已換 keys.env", res.stderr)
        self.assertIn("-pass env:LLMGW_KEYS_PASSWORD", self._log("openssl"))
        with open(self.sha_file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), SHA_A)
        self.assertEqual(self.leftovers(), [])

    def test_sha相同但本機key檔寫壞時重拉修復(self):
        self.write_keys("NOT A KEY FILE\n", sha=SHA_A)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))

        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.read_keys(), key_text("test-keysync-new"))
        self.assertEqual(stat.S_IMODE(os.stat(self.keys_path).st_mode), 0o600)
        self.assertEqual(self.leftovers(), [])
        self.assert_no_leak(res)

    def test_本機key檔只是空的仍算完好(self):
        # 空檔（install.sh 會先建）是合法狀態＝什麼都沒部署，不算損毀：
        # sha 相同就短路，不要每 10 分鐘重寫一次空檔。
        self.write_keys("", sha=SHA_A)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        before = os.stat(self.keys_path).st_mtime_ns

        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("sha 未變", res.stderr)
        self.assertEqual(self.read_keys(), "")
        self.assertEqual(os.stat(self.keys_path).st_mtime_ns, before)

    # --- 正常路徑 -------------------------------------------------------------
    def test_正常替換且sha與權限正確(self):
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        self.assertEqual(self.read_keys(), key_text("test-keysync-new"))
        self.assertEqual(stat.S_IMODE(os.stat(self.keys_path).st_mode), 0o600)
        with open(self.sha_file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), SHA_A)
        self.assertEqual(self.leftovers(), [])
        self.assertIn("已換 keys.env", res.stderr)
        self.assertIn(SHA_A[:12], res.stderr)

        gh_log = self._log("gh")
        self.assertIn("api repos/FATESAIKOU/LLMGateway-keys/contents/keys.env.enc", gh_log)

        openssl_log = self._log("openssl")
        self.assertIn("-aes-256-cbc", openssl_log)
        self.assertIn("-pbkdf2 -iter 10000", openssl_log)
        self.assertIn("-pass env:LLMGW_KEYS_PASSWORD", openssl_log)
        self.assertNotIn("-pass pass:", openssl_log)
        self.assertNotIn(PASSWORD, openssl_log)
        self.assert_no_leak(res)

    def test_sha相同不動(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_A)
        self._set("gh_response", blob_json(b64(b"cipher-b"), SHA_A))
        self._set("plaintext", key_text("test-keysync-should-not-apply"))
        before = os.stat(self.keys_path).st_mtime_ns

        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("sha 未變", res.stderr)
        self.assertEqual(self.read_keys(), key_text("test-keysync-old"))
        self.assertEqual(os.stat(self.keys_path).st_mtime_ns, before)
        self.assertEqual(self._log("openssl"), "")  # 不解密
        self.assertEqual(self._log("gh").count("api repos"), 1)

    def test_內容沒變但sha變_不換檔只更新sha(self):
        self.write_keys(key_text("test-keysync-new"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-c"), SHA_A))
        before = os.stat(self.keys_path).st_mtime_ns

        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("內容沒變", res.stderr)
        self.assertEqual(os.stat(self.keys_path).st_mtime_ns, before)
        with open(self.sha_file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), SHA_A)
        self.assertEqual(self.leftovers(), [])

    def test_keys_env不存在時建立且0600(self):
        self.assertFalse(os.path.exists(self.keys_path))
        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.isfile(self.keys_path))
        self.assertEqual(stat.S_IMODE(os.stat(self.keys_path).st_mode), 0o600)
        self.assertEqual(self.read_keys(), key_text("test-keysync-new"))

    def test_預設路徑用LGWD_KEYS與同目錄sha檔(self):
        # 不給 --keys／--sha-file：key 檔在 $LGWD_KEYS，sha 檔在它的目錄
        res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.isfile(self.sha_file))
        self.assertEqual(self.leftovers(), [])

    # --- 失敗路徑：一律保留舊 key -------------------------------------------
    def assert_kept_old(self, res, expected_tag, exit_code=None):
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"failed={expected_tag}", res.stderr)
        self.assertEqual(self.read_keys(), key_text("test-keysync-old"))
        self.assertEqual(stat.S_IMODE(os.stat(self.keys_path).st_mode), 0o600)
        with open(self.sha_file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), SHA_B)  # 舊 sha 不動
        self.assertEqual(self.leftovers(), [])
        self.assert_no_leak(res)
        if exit_code is not None:
            self.assertEqual(res.returncode, exit_code, res.stdout + res.stderr)

    def test_解密失敗保留舊key(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        self._set("openssl_mode", "bad")
        res = self.run_sync()
        self.assert_kept_old(res, "decrypt_failed", exit_code=7)

    def test_解密結果不是文字保留舊key(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        self._set("openssl_mode", "nonutf8")
        res = self.run_sync()
        self.assert_kept_old(res, "decrypt_failed", exit_code=7)

    def test_格式錯保留舊key(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        self._set("plaintext", "NOT A KEY FILE\n")
        res = self.run_sync()
        self.assert_kept_old(res, "format_failed", exit_code=8)
        self.assertIn("line 1", res.stderr)  # 只寫行號與種類，不回顯內容

    def test_格式錯_未知鍵名(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        self._set("plaintext", "UNKNOWN_KEY=whatever\n")
        res = self.run_sync()
        self.assert_kept_old(res, "format_failed", exit_code=8)

    def test_格式錯_重複鍵(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        self._set("plaintext", "OLLAMA_API_KEY=a\nOLLAMA_API_KEY=b\n")
        res = self.run_sync()
        self.assert_kept_old(res, "format_failed", exit_code=8)

    def test_gh失敗保留舊key(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_mode", "fail")
        res = self.run_sync()
        self.assert_kept_old(res, "gh_failed", exit_code=5)
        self.assertEqual(self._log("openssl"), "")

    def test_gh沒登入或token失效可分辨(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_mode", "auth")
        res = self.run_sync()
        self.assert_kept_old(res, "gh_auth", exit_code=5)
        self.assertIn("沒登入或 token 失效", res.stderr)
        self.assertNotIn("failed=gh_failed", res.stderr)
        self.assertEqual(self._log("openssl"), "")

    def test_gh找不到repo或檔案(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_mode", "notfound")
        res = self.run_sync()
        self.assert_kept_old(res, "gh_not_found", exit_code=5)

    def test_gh不在PATH(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        res = self.run_sync(["--gh", os.path.join(self.root, "no-such-gh")])
        self.assert_kept_old(res, "gh_missing", exit_code=5)

    def test_密碼沒放時不調openssl(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        res = self.run_sync(password=None)
        self.assert_kept_old(res, "password_missing", exit_code=3)
        self.assertEqual(self._log("openssl"), "")

    def test_gh逾時(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_mode", "sleep")
        res = self.run_sync(["--timeout", "0.5"])
        self.assert_kept_old(res, "gh_timeout", exit_code=5)

    def test_gh回覆不是JSON(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_mode", "garbage")
        res = self.run_sync()
        self.assert_kept_old(res, "bad_response", exit_code=6)

    def test_gh回覆缺sha(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", json.dumps({"encoding": "base64", "content": b64(b"x")}))
        res = self.run_sync()
        self.assert_kept_old(res, "bad_response", exit_code=6)

    def test_gh回覆content不是base64(self):
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", json.dumps(
            {"sha": SHA_A, "encoding": "base64", "content": "!!!not-base64!!!"}))
        res = self.run_sync()
        self.assert_kept_old(res, "decode_failed", exit_code=6)

    def test_舊key檔不存在且失敗時不留下檔案(self):
        self._set("gh_mode", "fail")
        res = self.run_sync()
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(os.path.exists(self.keys_path))
        self.assertFalse(os.path.exists(self.sha_file))
        self.assertEqual(self.leftovers(), [])

    # --- 真 openssl 往返 -----------------------------------------------------
    @unittest.skipUnless(shutil.which("openssl"), "沒有 openssl")
    def test_真openssl往返_參數與加密端一致(self):
        real_openssl = shutil.which("openssl")
        plaintext = key_text("test-keysync-roundtrip")
        enc_out = os.path.join(self.root, "keys.env.enc")
        plain_file = os.path.join(self.root, "plain.env")
        with open(plain_file, "w", encoding="utf-8") as fh:
            fh.write(plaintext)
        env = dict(os.environ, LLMGW_ROUNDTRIP_PW=PASSWORD)
        enc = subprocess.run(
            [real_openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "10000", "-a", "-salt",
             "-pass", "env:LLMGW_ROUNDTRIP_PW", "-in", plain_file, "-out", enc_out],
            env=env, capture_output=True, text=True)
        self.assertEqual(enc.returncode, 0, enc.stderr)
        with open(enc_out, "rb") as fh:
            encrypted = fh.read()
        self._set("gh_response", blob_json(b64(encrypted), SHA_C))

        res = self.run_sync(["--openssl", real_openssl])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.read_keys(), plaintext)
        self.assert_no_leak(res)

    # --- 靜態檢查 -----------------------------------------------------------
    def test_腳本可執行且外部指令只有gh與openssl(self):
        self.assertTrue(os.access(KEY_SYNC, os.X_OK))
        with open(KEY_SYNC, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertTrue(source.startswith("#!/usr/bin/env python3"))
        self.assertIn('-pass", f"env:{password_env}"', source)  # 只用 -pass env:
        self.assertNotIn("-pass pass:", source)
        self.assertNotIn("-pass file:", source)

    def test_unit與timer內容(self):
        with open(KEY_SYNC_SERVICE, "r", encoding="utf-8") as fh:
            service = fh.read()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=%h/.local/bin/key-sync", service)
        self.assertIn("EnvironmentFile=-%h/.config/llmgateway/keysync.env", service)

        with open(KEY_SYNC_TIMER, "r", encoding="utf-8") as fh:
            timer = fh.read()
        self.assertIn("Unit=key-sync.service", timer)
        self.assertIn("OnActiveSec=10min", timer)
        self.assertIn("OnUnitActiveSec=10min", timer)
        self.assertIn("Persistent=true", timer)  # 停機期間錯過的 tick 開機後補跑
        self.assertIn("WantedBy=timers.target", timer)

    def test_help(self):
        res = self.run_sync(["--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("keys.env", res.stdout)
        self.assertIn("--gh", res.stdout)

    def test_timeout環境變數非法不噴traceback(self):
        # 審查 #13 低-3：LGWSYNC_TIMEOUT=abc 原本在 build_parser 裸跑 float()，
        # 連 --help 都炸 traceback。現在要清楚錯誤、非零結束。
        self.write_keys(key_text("test-keysync-old"), sha=SHA_B)
        self._set("gh_response", blob_json(b64(b"cipher-a"), SHA_A))
        for bad in ("abc", "0", "-5", "nan", "inf"):
            with self.subTest(value=bad):
                res = self.run_sync(env_overrides={"LGWSYNC_TIMEOUT": bad})
                self.assertNotEqual(res.returncode, 0)
                self.assertNotIn("Traceback", res.stderr)
                self.assertIn("bad_config", res.stderr)
                self.assertIn("LGWSYNC_TIMEOUT", res.stderr)
                # 舊 key 保留、舊 sha 不動（連 gh 都不該去叫）
                self.assertEqual(self.read_keys(), key_text("test-keysync-old"))
                with open(self.sha_file, "r", encoding="utf-8") as fh:
                    self.assertEqual(fh.read().strip(), SHA_B)
                self.assertEqual(self._log("gh"), "")
                self.assert_no_leak(res)

    def test_timeout環境變數非法時_help仍可用(self):
        res = self.run_sync(["--help"], env_overrides={"LGWSYNC_TIMEOUT": "abc"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("usage: key-sync", res.stdout)
        self.assertNotIn("Traceback", res.stderr)

    def test_timeout環境變數合法照用(self):
        self._set("gh_mode", "sleep")
        res = self.run_sync(env_overrides={"LGWSYNC_TIMEOUT": "0.5"})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("failed=gh_timeout", res.stderr)
        self.assertNotIn("bad_config", res.stderr)


if __name__ == "__main__":
    unittest.main()
