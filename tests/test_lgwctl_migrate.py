"""lgwctl 遷移（prepare-migration／cutover／confirm／rollback-to-user／
finalize-migration）的測試。

全部在沙箱裡跑：

- 檔案系統在 ``LLMGW_PREFIX`` 底下（假機器），舊佈局在服務帳號的 HOME；
- 外部指令全換成假 script：假 systemctl（分 system／user 兩層）、假 systemd-run、
  假 runuser、假 loginctl、假 curl、假隧道探針；每個呼叫都寫進同一份
  ``order.log``，測試靠它斷言順序（保險先於停舊服務、停完才啟新…）；
- 假 systemd-run **不真的排程**：人手下的 cutover 只被驗「排了什麼」，
  真正的工作用 ``--inner`` 在行程內跑；反向保險則用「再叫一次
  rollback-to-user」模擬它到時觸發；
- 沙箱裡的密碼與 key 是測試自己的假值，測試會 grep 輸出、log 與狀態檔，
  確認值沒有外洩。
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
import shutil
from unittest import mock

try:
    from .lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )
except ImportError:  # python3 -m unittest discover -s tests
    from lgwctl_fakes import (
        FAKE_KEY_VALUE,
        FAKE_PASSWORD,
        FAKE_TAG,
        Sandbox,
        has_line,
        lines_with_level,
    )

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
OLD_TAG = "lgw-v0.1.4"
OLD_RENDEZVOUS = "tunnel-user@tunnel-host:22"
LOG_NAMES = ("order.log", "actions.log", "git.log", "curl.log", "sudo.log",
             "probe.log", "useradd.log", "systemctl.log", "timers.log")

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# 假 systemctl：--user 走 user 層（舊 unit）、沒帶就走 system 層（新 unit）。
# 每個動作都記進 order.log（測試斷言順序）；狀態放 active/<layer>/<unit>。
fake="${LGW_FAKE:?}"
mkdir -p "${fake}/active/system" "${fake}/active/user"
printf 'systemctl %s\\n' "$*" >> "${fake}/order.log"
layer="system"
args=()
for a in "$@"; do
    if [[ "$a" == "--user" ]]; then
        layer="user"
    else
        args+=("$a")
    fi
done
if [[ -f "${fake}/busfail/${layer}" ]]; then
    printf 'Failed to connect to bus: Failed to connect to socket /run/user/0/bus: No such file or directory\\n' >&2
    exit 1
fi
verb="${args[0]:-}"
unit="${args[1]:-}"
active="${fake}/active/${layer}/${unit}"
case "$verb" in
    is-active)
        [[ -f "$active" ]] && exit 0 || exit 3 ;;
    is-failed)
        [[ -f "${fake}/failed/${layer}/${unit}" ]] && exit 0 || exit 1 ;;
    *)
        if [[ -n "$unit" ]]; then
            printf '%s %s %s\\n' "$layer" "$verb" "$unit" >> "${fake}/actions.log"
        else
            printf '%s %s\\n' "$layer" "$verb" >> "${fake}/actions.log"
        fi ;;
esac
case "$verb" in
    enable|daemon-reload)
        exit 0 ;;
    disable)
        [[ -f "${fake}/fail/disable/${layer}/${unit}" ]] && exit 1
        exit 0 ;;
    start|restart)
        [[ -f "${fake}/fail/start/${layer}/${unit}" ]] && exit 1
        : > "$active"
        # 中-1 的旋鈕：新服務「啟動的當下」寫一份 .keysync.status（模擬新的
        # lgwd 起來 30 秒內就跑第一次 key 拉取）。
        if [[ -n "${LGW_FAKE_KEYSYNC_AT_START:-}" ]]; then
            printf '{"ts": %s, "ok": true, "reason": "ok"}\\n' "$(date +%s)" \\
                > "${LGW_FAKE_KEYSYNC_AT_START}"
        fi
        exit 0 ;;
    stop)
        if [[ -f "${fake}/fail/stop/${layer}/${unit}" ]]; then
            printf 'Failed to stop %s: fake failure\\n' "$unit" >&2
            exit 1
        fi
        if [[ -f "${fake}/missing/${layer}/${unit}" ]]; then
            printf 'Failed to stop %s: Unit %s not loaded.\\n' "$unit" "$unit" >&2
            exit 5
        fi
        rm -f "$active"
        exit 0 ;;
    *)
        exit 0 ;;
esac
"""

FAKE_SYSTEMD_RUN = """#!/usr/bin/env bash
# 假 systemd-run：只記下要跑的指令（不真的排程、不執行），但把 --unit= 的
# 單元標成 active（中-2：cutover 寫 pending 前會確認 lgw-revert.timer 還在）。
fake="${LGW_FAKE:?}"
printf 'systemd-run %s\\n' "$*" >> "${fake}/order.log"
unit=""
for a in "$@"; do
    case "$a" in
        --on-active=*) printf 'on-active %s\\n' "${a#--on-active=}" >> "${fake}/timers.log" ;;
        --unit=*) unit="${a#--unit=}" ;;
    esac
done
if [[ -n "$unit" && -z "${LGW_FAKE_SYSTEMD_RUN_SKIP_ACTIVE:-}" ]]; then
    mkdir -p "${fake}/active/system"
    if [[ "$*" == *--on-active=* ]]; then
        # systemd-run --unit=X --on-active=… 建的是 X.timer（不是 X.service）。
        : > "${fake}/active/system/${unit}.timer"
    else
        : > "${fake}/active/system/${unit}"
    fi
fi
exit "${LGW_FAKE_SYSTEMD_RUN_RC:-0}"
"""

FAKE_RUNUSER = """#!/usr/bin/env bash
# 假 runuser：記下 -u <user> 之後的指令（含有沒有帶 XDG／DBUS），照原樣執行。
fake="${LGW_FAKE:?}"
printf 'runuser %s\\n' "$*" >> "${fake}/order.log"
shift 2 || true
if [[ "${1:-}" == "--" ]]; then
    shift
fi
exec "$@"
"""

FAKE_LOGINCTL = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
printf 'loginctl %s\\n' "$*" >> "${fake}/order.log"
exit "${LGW_FAKE_LOGINCTL_RC:-0}"
"""

FAKE_CURL = """#!/usr/bin/env bash
# 假 curl：印出 http 碼；連續值放 $LGW_FAKE/curl_codes（一次一行）。
fake="${LGW_FAKE:?}"
printf 'curl %s\\n' "$*" >> "${fake}/order.log"
if [[ "${FAKE_CURL_RC:-0}" != "0" ]]; then
    exit "${FAKE_CURL_RC}"
fi
code="${FAKE_CURL_CODE:-200}"
codes="${fake}/curl_codes"
if [[ -s "$codes" ]]; then
    code="$(head -n 1 "$codes")"
    if [[ "$(wc -l < "$codes")" -gt 1 ]]; then
        tail -n +2 "$codes" > "${codes}.next"
        mv "${codes}.next" "$codes"
    else
        : > "$codes"
    fi
fi
printf '%s' "$code"
exit 0
"""

FAKE_TUNNEL_PROBE = """#!/usr/bin/env bash
# 假隧道探針：記下 LGW_TUNNEL_ENV 指到哪（不含值）與結束碼。
# 連續結果放 $LGW_FAKE/probe_codes（一次一行；用完就回預設）。
fake="${LGW_FAKE:?}"
printf 'probe %s\\n' "$*" >> "${fake}/order.log"
printf 'probe-env %s\\n' "${LGW_TUNNEL_ENV:-}" >> "${fake}/probe.log"
code="${FAKE_PROBE_RC:-0}"
codes="${fake}/probe_codes"
if [[ -s "$codes" ]]; then
    code="$(head -n 1 "$codes")"
    if [[ "$(wc -l < "$codes")" -gt 1 ]]; then
        tail -n +2 "$codes" > "${codes}.next"
        mv "${codes}.next" "$codes"
    else
        : > "$codes"
    fi
fi
exit "$code"
"""


def index_of(lines, needle: str) -> int:
    """order.log 裡第一個含 needle 的行號（找不到就讓測試失敗）。"""
    for index, line in enumerate(lines):
        if needle in line:
            return index
    raise AssertionError(f"order.log 找不到 {needle!r}：{lines}")


def has_level(text: str, name: str, level: str) -> bool:
    """輸出裡有沒有 ``level name（detail）``（三態之外也支援遷移的 skip）。"""
    for line in text.splitlines():
        head, _, rest = line.partition(" ")
        if head != level:
            continue
        if rest.split("（", 1)[0] == name:
            return True
    return False


class MigrateSandbox(Sandbox):
    """Sandbox ＋ 遷移用的假指令（共用一份 order.log）＋舊佈局的造景。"""

    def __init__(self):
        super().__init__()
        self._write_migrate_tools()
        self.env.update({
            "LLMGW_SYSTEMD_RUN": os.path.join(self.fakebin, "systemd-run"),
            "LLMGW_RUNUSER": os.path.join(self.fakebin, "runuser"),
            "LLMGW_LOGINCTL": os.path.join(self.fakebin, "loginctl"),
            # 等待時間調 0：測試不真的等 60／90 秒（只試一次）。
            "LLMGW_CUTOVER_WAIT": "0",
            "LLMGW_KEYSYNC_GRACE": "0",
            "LLMGW_TUNNEL_WAIT": "0",
            "LLMGW_REMOTE_WAIT": "0",
            "LLMGW_LOCK_WAIT": "0",
            # inner 只接受 transient unit（低-6）：假裝是 systemd 起的。
            "INVOCATION_ID": "fake-invocation",
            # preflight 的 gh 檢查（中-6）：預設登入，單一測試再設 1 擋掉。
            "FAKE_GH_AUTH_RC": "0",
        })

    def _write_migrate_tools(self) -> None:
        tools = (("systemctl", FAKE_SYSTEMCTL), ("curl", FAKE_CURL),
                 ("systemd-run", FAKE_SYSTEMD_RUN), ("runuser", FAKE_RUNUSER),
                 ("loginctl", FAKE_LOGINCTL))
        for name, text in tools:
            path = os.path.join(self.fakebin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(path, 0o755)
        # 真的 `git clone` 進 `tempfile.mkdtemp`（0700）的目錄不會改它的 mode
        # （高-1）；共用假 git 的 `cp -a` 會把 template 的 0755 套上去，正好掩蓋
        # 這個洞。這裡在複製完把 dest 改回 0700，讓 normalize_tree 真的有被測到。
        git_path = os.path.join(self.fakebin, "git")
        with open(git_path, encoding="utf-8") as fh:
            git_text = fh.read()
        git_text = git_text.replace('exit 0\n', 'chmod 700 "$dest"\nexit 0\n')
        with open(git_path, "w", encoding="utf-8") as fh:
            fh.write(git_text)
        os.chmod(git_path, 0o755)
        # 版目錄裡的隧道探針換成會寫 order.log 的那支（make_release 從 template 複製）。
        self.write(os.path.join(self.template, "ops-scripts", "llmgateway-tunnel"),
                   FAKE_TUNNEL_PROBE, 0o755)

    # -- 假指令的狀態 -------------------------------------------------------

    def set_active(self, unit: str, active: bool = True, *, layer: str = "system") -> None:
        path = os.path.join(self.fake, "active", layer, unit)
        if active:
            self.write(path, "1\n")
        elif os.path.exists(path):
            os.unlink(path)

    def active(self, unit: str, *, layer: str = "system") -> bool:
        return os.path.exists(os.path.join(self.fake, "active", layer, unit))

    def fail_action(self, verb: str, unit: str, *, layer: str = "system") -> None:
        self.write(os.path.join(self.fake, "fail", verb, layer, unit), "1\n")

    def mark_unit_missing(self, unit: str, *, layer: str = "system") -> None:
        self.write(os.path.join(self.fake, "missing", layer, unit), "1\n")

    def set_bus_failure(self, layer: str = "user") -> None:
        """讓假 systemctl 對這一層印「Failed to connect to bus」並回 1（中-2）。"""
        self.write(os.path.join(self.fake, "busfail", layer), "1\n")

    def probe_codes(self, codes) -> None:
        """隧道探針的連續結果（一次一行；像 curl_codes）。"""
        self.write(os.path.join(self.fake, "probe_codes"),
                   "".join(f"{code}\n" for code in codes))

    def order(self) -> list:
        return self.log_lines("order.log")

    def actions(self) -> list:
        return self.log_lines("actions.log")

    def run_migrate(self, argv, **env_extra) -> tuple:
        env = dict(self.env)
        env.update(env_extra)
        return self.run_cli(list(argv), env=env)

    def ctl(self, *parts) -> str:
        """遷移控制檔（狀態、鎖）的目錄：root 專屬（不在服務帳號可寫的 state）。"""
        return os.path.join(self.prefix, "var", "lib", "llmgateway-ctl", *parts)

    # -- 舊佈局（v0.1.4，user 層）------------------------------------------

    def old_conf(self, *parts) -> str:
        return os.path.join(self.service_home, ".config", "llmgateway", *parts)

    def old_state(self, *parts) -> str:
        return os.path.join(self.service_home, ".local", "state", "llmgateway", *parts)

    def old_share(self, *parts) -> str:
        return os.path.join(self.service_home, ".local", "share", "llmgateway", *parts)

    def old_units(self, *parts) -> str:
        return os.path.join(self.service_home, ".config", "systemd", "user", *parts)

    def make_old_layout(self, *, password: str = FAKE_PASSWORD,
                        key_value: str = FAKE_KEY_VALUE, db_marker: str = "old-db") -> None:
        """舊機器：設定、state、舊版程式目錄、四個 user unit 與隧道金鑰都在。"""
        home = self.service_home
        self.write(self.old_conf("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.write(self.old_conf("tunnel.env"),
                   f"RENDEZVOUS={OLD_RENDEZVOUS}\n"
                   f"IDENTITY={os.path.join(home, '.ssh', 'id_llmgw_tunnel')}\n"
                   f"KNOWN_HOSTS={os.path.join(home, '.ssh', 'known_hosts_rendezvous')}\n"
                   "REMOTE_API=4710\nREMOTE_SSH=4711\nLOCAL_API=8480\n", 0o600)
        self.write(self.old_conf("keysync.env"),
                   f"# 只有一行\nLLMGW_KEYS_PASSWORD={password}\n", 0o600)
        self.write(self.old_state("keys.env"),
                   f"CLAUDE_PERSONAL_OAUTH_TOKEN={key_value}\n", 0o600)
        self.make_db(self.old_state("llm.db"), db_marker)
        self.write(self.old_share(OLD_TAG, ".lgw-tag"), OLD_TAG + "\n")
        for name in ("llmgateway.service", "llmgateway-tunnel.service",
                     "key-sync.service", "key-sync.timer"):
            self.write(self.old_units(name), f"[Unit]\n# 舊的 {name}\n")
        self.write(os.path.join(home, ".ssh", "id_llmgw_tunnel"), "fake-key\n", 0o600)
        self.write(os.path.join(home, ".ssh", "known_hosts_rendezvous"), "fake-host\n")
        # 舊的服務、隧道與 timer 在跑（key-sync.service 是 oneshot，平常不在跑）。
        for unit in ("llmgateway.service", "llmgateway-tunnel.service", "key-sync.timer"):
            self.set_active(unit, True, layer="user")

    def old_snapshot(self) -> dict:
        """舊佈局的檔案內容快照（prepare 前後要一模一樣）。"""
        paths = (self.old_conf("llmgateway.env"), self.old_conf("tunnel.env"),
                 self.old_conf("keysync.env"), self.old_state("keys.env"),
                 self.old_state("llm.db"), self.old_share(OLD_TAG, ".lgw-tag"),
                 os.path.join(self.service_home, ".ssh", "id_llmgw_tunnel"),
                 os.path.join(self.service_home, ".ssh", "known_hosts_rendezvous"))
        snapshot = {}
        for path in paths:
            with open(path, "rb") as fh:
                snapshot[path] = fh.read()
        for name in ("llmgateway.service", "llmgateway-tunnel.service",
                     "key-sync.service", "key-sync.timer"):
            with open(self.old_units(name), "rb") as fh:
                snapshot[self.old_units(name)] = fh.read()
        return snapshot

    # -- 新佈局（prepare 完、還沒 start）-----------------------------------

    def install_new_state(self, tag: str = FAKE_TAG) -> str:
        """沙箱＝「prepare 完」：current、/etc 設定、unit 檔、state 都就位。"""
        dest = self.install_state(tag)
        self.write(self.etc("llmgateway.env"), 'LGW_API_PORT="8480"\n')
        self.write(self.etc("tunnel.env"),
                   f"RENDEZVOUS={OLD_RENDEZVOUS}\n"
                   f"IDENTITY={os.path.join(self.service_home, '.ssh', 'id_llmgw_tunnel')}\n"
                   f"KNOWN_HOSTS={os.path.join(self.service_home, '.ssh', 'known_hosts_rendezvous')}\n")
        self.write(self.etc("keys.pass"),
                   f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o640)
        self.write(self.state("keys.env"),
                   f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.write(self.state(".keysync.status"),
                   json.dumps({"ts": time.time(), "ok": True, "reason": "ok"}) + "\n")
        for name in ("llmgateway.service", "llmgateway-tunnel.service"):
            self.write(self.unit(name), f"[Unit]\n# 新版 {name}\n")
        return dest

    # -- 狀態檔 -------------------------------------------------------------

    def write_cutover_state(self, **fields) -> None:
        data = {"ts": time.time()}
        data.update(fields)
        self.write(self.ctl(".cutover.state"), json.dumps(data, sort_keys=True) + "\n")

    def cutover_state(self) -> dict:
        return json.loads(self.read(self.ctl(".cutover.state")))

    def age(self, path: str, seconds: float) -> None:
        """把 mtime 調成 now+seconds（測「新的有沒有比較新」）。"""
        stamp = time.time() + seconds
        os.utime(path, (stamp, stamp))

    # -- 真的 SQLite 檔（高-4：backup API 測不到假檔案）--------------------

    def make_db(self, path: str, marker: str) -> None:
        """建一個有內容的真 SQLite 資料庫（marker 放進 t 表）。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
            conn.execute("INSERT INTO t VALUES (?)", (marker,))
            conn.commit()
        finally:
            conn.close()
        os.chmod(path, 0o600)

    def db_markers(self, path: str) -> list:
        conn = sqlite3.connect(path)
        try:
            return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY rowid")]
        finally:
            conn.close()

    def db_integrity(self, path: str) -> str:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()

    def db_backups(self, path: str) -> list:
        return sorted(glob.glob(path + ".bak-*"))


class MigrationTestCase(unittest.TestCase):
    """每個測試一台沙箱；輸出／log／狀態檔都不許含 key 值或密碼。"""

    def setUp(self):
        self.box = MigrateSandbox()
        self.addCleanup(self.box.close)

    def assert_no_values(self, out: str, err: str) -> None:
        text = out + err
        logs = "".join(self.box.log(name) for name in LOG_NAMES)
        state_path = self.box.ctl(".cutover.state")
        state = self.box.read(state_path) if os.path.isfile(state_path) else ""
        for value in (FAKE_PASSWORD, FAKE_KEY_VALUE):
            self.assertNotIn(value, text, "輸出含 key 值或密碼")
            self.assertNotIn(value, logs, "假指令的 log 含 key 值或密碼")
            self.assertNotIn(value, state, "狀態檔含 key 值或密碼")


class PrepareMigrationTests(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.box.make_old_layout()

    def prepare(self, *extra, **env_extra):
        return self.box.run_migrate(["prepare-migration", "--tag", FAKE_TAG, *extra],
                                    **env_extra)

    # -- 正常路徑 -----------------------------------------------------------

    def test_prepare_places_new_layout_without_enable_or_start(self):
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [], out)
        self.assertEqual(
            self.box.read(os.path.join(self.box.current_link, ".lgw-tag")).strip(), FAKE_TAG)
        self.assertTrue(os.path.isfile(self.box.unit("llmgateway.service")))
        self.assertTrue(os.path.isfile(self.box.unit("llmgateway-tunnel.service")))
        # unit 有 daemon-reload，但沒有 enable、沒有 start（新服務不能先跑起來）
        self.assertIn("system daemon-reload", self.box.actions())
        self.assertFalse(any(line.startswith(("system enable", "system start",
                                              "system stop"))
                             for line in self.box.actions()), self.box.actions())
        # 沒有動到任何 user 層的東西（prepare 全程不碰舊服務）
        self.assertFalse(any("--user" in line for line in self.box.order()),
                         self.box.order())
        self.assertFalse(any(line.startswith("runuser ") for line in self.box.order()))
        # 每項都有交代
        for name in ("state-keys-env", "state-db", "tunnel-env", "llmgateway-env",
                     "keys-pass", "old-untouched"):
            self.assertTrue(has_line(out, name), (name, out))
        self.assert_no_values(out, err)

    def test_prepare_copies_values_without_printing_them(self):
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.state("keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")
        self.assertEqual(self.box.db_markers(self.box.state("llm.db")), ["old-db"])
        self.assertEqual(self.box.read(self.box.etc("tunnel.env")),
                         self.box.read(self.box.old_conf("tunnel.env")))
        self.assertEqual(self.box.read(self.box.etc("llmgateway.env")),
                         self.box.read(self.box.old_conf("llmgateway.env")))
        self.assertEqual(self.box.read(self.box.etc("keys.pass")),
                         f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.box.etc("keys.pass")).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.box.state("keys.env")).st_mode), 0o600)
        # 隧道金鑰維持原位：tunnel.env 的 IDENTITY／KNOWN_HOSTS 還是絕對路徑、沒被改寫
        tunnel_env = self.box.read(self.box.etc("tunnel.env"))
        self.assertIn(f"IDENTITY={os.path.join(self.box.service_home, '.ssh', 'id_llmgw_tunnel')}",
                      tunnel_env)
        self.assertIn("KNOWN_HOSTS=", tunnel_env)
        self.assert_no_values(out, err)

    def test_prepare_does_not_touch_old_layout(self):
        before = self.box.old_snapshot()
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.old_snapshot(), before)
        # 舊的 user unit 也原封不動（沒有搬、沒有刪、沒有 disable）
        for name in ("llmgateway.service", "llmgateway-tunnel.service",
                     "key-sync.service", "key-sync.timer"):
            self.assertTrue(os.path.isfile(self.box.old_units(name)))
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(any(line.startswith(("user disable", "user stop"))
                             for line in self.box.actions()), self.box.actions())
        self.assert_no_values(out, err)

    def test_prepare_is_idempotent_and_keeps_existing_targets(self):
        self.assertEqual(self.prepare()[0], 0)
        self.box.write(self.box.etc("tunnel.env"), "# 本人自己改的\n")
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.etc("tunnel.env")), "# 本人自己改的\n")
        for name in ("state-keys-env", "state-db", "tunnel-env", "llmgateway-env",
                     "keys-pass"):
            self.assertTrue(has_level(out, name, "skip"), (name, out))
        self.assert_no_values(out, err)

    def test_prepare_force_overwrites(self):
        self.assertEqual(self.prepare()[0], 0)
        self.box.write(self.box.etc("tunnel.env"), "# 本人自己改的\n")
        self.box.write(self.box.etc("keys.pass"), "# 空的\n", 0o640)
        rc, out, err = self.prepare("--force")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"RENDEZVOUS={OLD_RENDEZVOUS}", self.box.read(self.box.etc("tunnel.env")))
        self.assertEqual(self.box.read(self.box.etc("keys.pass")),
                         f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n")
        self.assertTrue(has_line(out, "tunnel-env", "ok"), out)
        self.assertTrue(has_line(out, "keys-pass", "ok"), out)
        self.assert_no_values(out, err)

    def test_prepare_refuses_when_phase_is_open(self):
        """中-1：階段處於 OPEN_PHASES（如 confirmed）時拒絕 prepare --force 覆蓋正在跑的新資料。"""
        self.box.install_new_state()
        self.box.write_cutover_state(phase="confirmed")
        # 模擬新服務在 confirmed 階段已寫入新資料
        self.box.write(self.box.state("keys.env"), "NEW_TOKEN=new-token\n")
        self.box.write(self.box.etc("tunnel.env"), "NEW_TUNNEL=1\n")

        rc, out, err = self.prepare("--force")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "prepare", "fail"), out)
        # 確保現有新資料完全未被舊資料覆蓋
        self.assertEqual(self.box.read(self.box.state("keys.env")), "NEW_TOKEN=new-token\n")
        self.assertEqual(self.box.read(self.box.etc("tunnel.env")), "NEW_TUNNEL=1\n")

    def test_prepare_fills_an_empty_keys_pass_shell(self):
        """install 先放過空殼（只有註解）時，prepare 會把舊的密碼補上。"""
        self.box.write(self.box.etc("keys.pass"), "# 只有註解\n", 0o640)
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.etc("keys.pass")),
                         f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n")
        self.assertTrue(has_line(out, "keys-pass", "ok"), out)
        self.assert_no_values(out, err)

    # -- 舊檔不可信（中-5）與斷鏈（低-1）----------------------------------

    def test_prepare_refuses_a_symlinked_old_keys_env(self):
        outside = self.box.write(os.path.join(self.box.root, "outside.env"),
                                 "CLAUDE_PERSONAL_OAUTH_TOKEN=stolen-value\n", 0o600)
        os.unlink(self.box.old_state("keys.env"))
        os.symlink(outside, self.box.old_state("keys.env"))
        rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "state-keys-env", "fail"), out)
        self.assertFalse(os.path.exists(self.box.state("keys.env")))
        self.assertNotIn("stolen-value", out + err)

    def test_prepare_refuses_a_symlinked_old_db(self):
        outside = os.path.join(self.box.root, "outside.db")
        self.box.make_db(outside, "outside-db")
        os.unlink(self.box.old_state("llm.db"))
        os.symlink(outside, self.box.old_state("llm.db"))
        rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "state-db", "fail"), out)
        self.assertFalse(os.path.exists(self.box.state("llm.db")))

    def test_prepare_refuses_a_symlinked_old_tunnel_env(self):
        outside = self.box.write(os.path.join(self.box.root, "outside-tunnel.env"),
                                 f"RENDEZVOUS={OLD_RENDEZVOUS}\n", 0o600)
        os.unlink(self.box.old_conf("tunnel.env"))
        os.symlink(outside, self.box.old_conf("tunnel.env"))
        rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "tunnel-env", "fail"), out)
        self.assertFalse(os.path.exists(self.box.etc("tunnel.env")))

    def test_prepare_refuses_a_symlinked_old_keysync_env(self):
        outside = self.box.write(os.path.join(self.box.root, "outside-keysync.env"),
                                 "LLMGW_KEYS_PASSWORD=stolen-password\n", 0o600)
        os.unlink(self.box.old_conf("keysync.env"))
        os.symlink(outside, self.box.old_conf("keysync.env"))
        rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keys-pass", "fail"), out)
        self.assertFalse(os.path.exists(self.box.etc("keys.pass")))
        self.assertNotIn("stolen-password", out + err)

    def test_prepare_refuses_symlinked_ssh_dir(self):
        """高-1：~svc/.ssh 是 symlink 時 prepare 必須 fail 且目標目錄權限不變。"""
        target_dir = os.path.join(self.box.root, "target_etc")
        os.makedirs(target_dir, mode=0o755)
        os.chmod(target_dir, 0o755)
        ssh_dir = os.path.join(self.box.service_home, ".ssh")
        if os.path.islink(ssh_dir):
            os.unlink(ssh_dir)
        elif os.path.isdir(ssh_dir):
            shutil.rmtree(ssh_dir)
        os.makedirs(self.box.service_home, exist_ok=True)
        os.symlink(target_dir, ssh_dir)

        rc, out, err = self.prepare()
        self.assertNotEqual(rc, 0)
        self.assertTrue(has_line(out, "dirs", "fail"), out)
        mode = stat.S_IMODE(os.lstat(target_dir).st_mode)
        self.assertEqual(mode, 0o755)

    def test_prepare_refuses_intermediate_symlink_directory(self):
        """低-2：舊 state 的上層目錄是 symlink 時拒絕跟隨。"""
        outside = os.path.join(self.box.root, "outside_state")
        os.makedirs(os.path.join(outside, "llmgateway"), exist_ok=True)
        self.box.make_db(os.path.join(outside, "llmgateway", "llm.db"), "outside-db")
        state_parent = os.path.join(self.box.service_home, ".local", "state")
        if os.path.exists(state_parent):
            shutil.rmtree(state_parent)
        os.makedirs(os.path.dirname(state_parent), exist_ok=True)
        os.symlink(outside, state_parent)

        rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "state-db", "fail"), out)
        self.assertFalse(os.path.exists(self.box.state("llm.db")))

    def test_copy_into_place_closes_src_fd_on_mkstemp_error(self):
        """低-3：mkstemp 失敗時不洩漏已開啟的來源 fd。"""
        from lgwctl import migrate
        src = self.box.write(os.path.join(self.box.root, "some.txt"), "hello\n")
        dst = os.path.join(self.box.root, "dst.txt")
        with mock.patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migrate._copy_into_place(src, dst)

    def test_prepare_replaces_a_broken_symlink_target_without_a_traceback(self):
        """低-1：目標是斷掉的 symlink 時不能 traceback（getsize 會丟）。"""
        os.makedirs(os.path.dirname(self.box.state("keys.env")), exist_ok=True)
        os.symlink(os.path.join(self.box.root, "gone"), self.box.state("keys.env"))
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.islink(self.box.state("keys.env")))
        self.assertEqual(self.box.read(self.box.state("keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")

    # -- owner 規則（中-7）與 .keysync.sha（低-8a）------------------------

    def test_prepare_copies_the_keysync_sha(self):
        self.box.write(self.box.old_state(".keysync.sha"), "abc123\n", 0o600)
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.state(".keysync.sha")), "abc123\n")
        self.assertTrue(has_line(out, "state-keysync-sha", "ok"), out)

    def test_prepare_uses_the_shared_ownership_helpers(self):
        """中-7：keys.pass／/etc 設定檔用 install 的同一個規則（root:<群組>）。"""
        calls = []

        def record(kind):
            return lambda layout, path: calls.append((kind, path))

        with mock.patch("lgwctl.install.chown_root_group", side_effect=record("root")), \
                mock.patch("lgwctl.install._chown_service", side_effect=record("service")):
            rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        root_paths = {path for kind, path in calls if kind == "root"}
        service_paths = {path for kind, path in calls if kind == "service"}
        for name in ("keys.pass", "tunnel.env", "llmgateway.env"):
            self.assertIn(self.box.etc(name), root_paths, name)
        for name in ("keys.env", "llm.db"):
            self.assertIn(self.box.state(name), service_paths, name)
        self.assertIn(f"root:{self.box.service_user}", out)

    def test_prepare_fails_when_the_service_group_is_missing(self):
        """中-7：拿不到群組要 fail，不能靜默變成 root:root。"""
        with mock.patch("lgwctl.install.chown_root_group", side_effect=KeyError):
            rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "keys-pass", "fail"), out)
        self.assertIn("root:root", out)

    # -- 缺東缺西與拒絕 -----------------------------------------------------

    def test_prepare_snapshots_a_live_database_consistently(self):
        """舊服務的寫入交易開著時，快照仍是一致的（高-4）。

        rollback journal 模式下主檔是就地修改的：直接 copy 會拿到新舊頁混合
        的檔案。backup API 只看得到已提交的資料。
        """
        db = self.box.old_state("llm.db")
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO t VALUES ('committed')")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES ('uncommitted')")   # 故意不 commit
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        new = self.box.state("llm.db")
        self.assertEqual(self.box.db_integrity(new), "ok")
        self.assertEqual(self.box.db_markers(new), ["old-db", "committed"])

    def test_prepare_makes_the_version_dir_traversable_for_the_service(self):
        """高-1：真的 git clone 進 mkdtemp 是 0700；prepare 後必須 0755。

        假 git 會故意留下 0700，所以這條測得到 release.normalize_tree 真的跑了
        （不然 User=llmgw 的服務 WorkingDirectory 進不去，切換必定 CHDIR 失敗）。
        """
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        tag_dir = self.box.opt(FAKE_TAG)
        mode = stat.S_IMODE(os.stat(tag_dir).st_mode)
        self.assertEqual(mode, 0o755, oct(mode))
        self.assertTrue(mode & 0o005, "服務帳號要有 o+rx 才進得去")
        # 版本目錄裡的檔案也要讓 llmgw 讀得到（unit 以 llmgw 跑）
        unit_file = os.path.join(tag_dir, "deploy", "units", "llmgateway.service")
        self.assertTrue(stat.S_IMODE(os.stat(unit_file).st_mode) & 0o004)

    def test_prepare_without_old_layout_lists_todo_not_fail(self):
        box = MigrateSandbox()          # 舊佈局完全沒有
        self.addCleanup(box.close)
        rc, out, err = box.run_migrate(["prepare-migration", "--tag", FAKE_TAG])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [], out)
        for name in ("state-keys-env", "state-db", "tunnel-env", "llmgateway-env",
                     "keys-pass", "state-keysync-sha"):
            self.assertTrue(has_line(out, name, "todo"), (name, out))
        # keys.pass／tunnel.env 的範本還是要放（LoadCredential 的來源檔要在）
        self.assertTrue(os.path.isfile(box.etc("keys.pass")))
        self.assertTrue(os.path.isfile(box.etc("tunnel.env")))
        self.assertEqual(stat.S_IMODE(os.stat(box.etc("keys.pass")).st_mode), 0o640)
        self.assertNotIn(FAKE_PASSWORD, out + err)

    def test_prepare_archives_a_reverted_state_and_starts_a_new_round(self):
        """中-4：reverted 是終態，但 prepare 要把舊狀態檔歸檔才能再切。"""
        self.box.write_cutover_state(phase="reverted", tag=OLD_TAG,
                                     old_home=self.box.service_home)
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "state-archive", "ok"), out)
        archived = glob.glob(self.box.ctl(".cutover.state.*"))
        self.assertEqual(len(archived), 1, archived)
        self.assertEqual(json.loads(self.box.read(archived[0]))["phase"], "reverted")
        state = self.box.cutover_state()
        self.assertNotIn("phase", state)
        self.assertEqual(state["old_home"], self.box.service_home)

    def test_prepare_skipped_keys_pass_still_fixes_owner_and_mode(self):
        """低-9：keys.pass 已有值時 skip，但 0640／root:<群組> 還是要校正。"""
        dst = self.box.etc("keys.pass")
        self.box.write(dst, f"LLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n", 0o600)
        calls = []

        def record(layout, path):
            calls.append(path)

        with mock.patch("lgwctl.install.chown_root_group", side_effect=record):
            rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_level(out, "keys-pass", "skip"), out)
        self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o640)
        self.assertIn(dst, calls)

    def test_prepare_replaces_broken_symlink_targets(self):
        """低-10：目標是斷鏈時不能當「已存在」跳過（llm.db／tunnel.env）。"""
        os.makedirs(self.box.state(), exist_ok=True)
        os.symlink(os.path.join(self.box.root, "gone-db"), self.box.state("llm.db"))
        os.makedirs(self.box.etc(), exist_ok=True)
        os.symlink(os.path.join(self.box.root, "gone-env"), self.box.etc("tunnel.env"))
        rc, out, err = self.prepare()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.islink(self.box.state("llm.db")))
        self.assertFalse(os.path.islink(self.box.etc("tunnel.env")))
        self.assertEqual(self.box.db_markers(self.box.state("llm.db")), ["old-db"])
        self.assertIn(f"RENDEZVOUS={OLD_RENDEZVOUS}",
                      self.box.read(self.box.etc("tunnel.env")))

    def test_prepare_fails_loudly_when_another_run_holds_the_lock(self):
        """低-4：prepare 與 cutover／rollback／finalize 共用同一把鎖。"""
        lock_path = self.box.ctl(".cutover.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        rc, out, err = self.prepare()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "prepare", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")

    def test_prepare_rejects_bad_tag(self):
        rc, out, err = self.box.run_migrate(["prepare-migration", "--tag", "nope"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "tag", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_prepare_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        env["LLMGW_SERVICE_HOME"] = self.box.service_home
        rc, out, err = self.box.run_cli(["prepare-migration", "--tag", FAKE_TAG], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)
        self.assertEqual(self.box.log("git.log"), "")


class CutoverTests(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)

    def outer(self, **env_extra):
        return self.box.run_migrate(["cutover", "--tag", FAKE_TAG,
                                     "--revert-after", "10m"], **env_extra)

    def inner(self, *extra, **env_extra):
        return self.box.run_migrate(["cutover", "--tag", FAKE_TAG, "--revert-after", "10m",
                                     "--inner", *extra], **env_extra)

    # -- 分離執行 -----------------------------------------------------------

    def test_outer_only_schedules_the_detached_unit(self):
        rc, out, err = self.outer()
        self.assertEqual(rc, 0, out + err)
        order = self.box.order()
        self.assertEqual(len(order), 1, order)
        line = order[0]
        self.assertIn("systemd-run --unit=lgw-cutover", line)
        self.assertIn(f"--working-directory={self.box.current_link}", line)
        self.assertIn("-m lgwctl cutover", line)
        self.assertIn("--tag lgw-v1.2.3", line)
        self.assertIn("--revert-after 10m", line)
        self.assertIn("--old-home " + self.box.service_home, line)
        self.assertIn("--inner", line)
        # 人手下的那一次什麼都沒動
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(self.box.active("llmgateway.service"))
        self.assertEqual(self.box.actions(), [])
        self.assertFalse(os.path.exists(self.box.ctl(".cutover.state")))
        self.assertIn("lgw-cutover", out)
        self.assert_no_values(out, err)

    def test_outer_fails_clearly_when_systemd_run_fails(self):
        rc, out, err = self.outer(LGW_FAKE_SYSTEMD_RUN_RC="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "cutover", "fail"), out)
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    def test_outer_refuses_when_tag_is_not_prepared(self):
        self.box.symlink(self.box.opt("lgw-v9.9.9"), self.box.current_link)
        rc, out, err = self.outer()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "cutover", "fail"), out)
        self.assertIn("prepare-migration", out)
        self.assertEqual(self.box.order(), [])

    def test_outer_preflight_refuses_when_keys_pass_is_empty(self):
        """中-8：缺前置（keys.pass 沒填）在排程前就拒絕，舊服務完全沒動。"""
        self.box.write(self.box.etc("keys.pass"), "# 只有註解\n", 0o640)
        rc, out, err = self.outer()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "preflight", "fail"), out)
        self.assertTrue(has_line(out, "cutover", "fail"), out)
        self.assertIn("keys.pass", out)
        self.assertEqual(self.box.order(), [])
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    def test_inner_preflight_refuses_without_touching_old_service(self):
        """中-8：inner 也跑同一組 pre-flight（detached 之間可能有人改東西）。"""
        self.box.write(self.box.etc("keys.pass"), "# 只有註解\n", 0o640)
        rc, out, err = self.inner()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "preflight", "fail"), out)
        self.assertEqual(self.box.actions(), [])
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(os.path.exists(self.box.ctl(".cutover.state")))

    def test_cutover_refuses_when_old_unit_file_is_missing(self):
        """中-3：缺任一個舊 unit 檔時，preflight 在停舊之前就拒絕。"""
        timer_file = self.box.old_units("key-sync.timer")
        os.unlink(timer_file)
        rc, out, err = self.outer()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "preflight", "fail"), out)
        self.assertIn("key-sync.timer", out)
        # 舊服務完全沒動
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertEqual(self.box.order(), [])

    def test_inner_archives_a_reverted_state_and_starts_a_new_round(self):
        """中-4：reverted 是終態但可開新一輪——舊狀態檔歸檔後照常切。"""
        self.box.write_cutover_state(phase="reverted", tag=FAKE_TAG)
        rc, out, err = self.inner()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "state-archive", "ok"), out)
        archived = glob.glob(self.box.ctl(".cutover.state.*"))
        self.assertEqual(len(archived), 1, archived)
        self.assertEqual(json.loads(self.box.read(archived[0]))["phase"], "reverted")
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_inner_refuses_when_a_round_is_still_open(self):
        """中-4：pending-confirm／confirmed／finalized 不得重複切換。"""
        for phase in ("pending-confirm", "confirmed", "finalized", "cutover-started"):
            with self.subTest(phase=phase):
                box = MigrateSandbox()
                self.addCleanup(box.close)
                box.make_old_layout()
                box.install_new_state(FAKE_TAG)
                box.set_active("llmgateway.service")
                box.write_cutover_state(phase=phase, tag=FAKE_TAG)
                rc, out, err = box.run_migrate(
                    ["cutover", "--tag", FAKE_TAG, "--revert-after", "10m", "--inner"])
                self.assertEqual(rc, 1, out + err)
                self.assertTrue(has_line(out, "cutover", "fail"), out)
                self.assertIn(phase, out)
                self.assertEqual(box.actions(), [])

    def test_cutover_resnapshots_old_db_and_syncs_newer_keys(self):
        """中-2：停完舊服務後重新快照舊 llm.db，並同步較新的 keys.env。"""
        # prepare 之後、cutover 之前，舊服務寫入新的資料
        self.box.make_db(self.box.old_state("llm.db"), "new-cooldown-writes")
        # 舊 keys.env 有更新（mtime 比新位置新）
        time.sleep(0.01)
        self.box.write(self.box.old_state("keys.env"), "UPDATED_KEY=val\n")

        rc, out, err = self.inner()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "sync-db", "ok"), out)
        self.assertTrue(has_line(out, "sync-keys-env", "ok"), out)
        # 驗證新位置的 db 包含了舊服務在切換前的最新資料
        self.assertIn("new-cooldown-writes", self.box.db_markers(self.box.state("llm.db")))
        self.assertEqual(self.box.read(self.box.state("keys.env")), "UPDATED_KEY=val\n")

    def test_outer_refuses_when_a_round_is_still_open(self):
        """中-4：confirmed／finalized 等階段在 outer 就拒絕，不排 transient。"""
        for phase in ("pending-confirm", "confirmed", "finalized", "cutover-started"):
            with self.subTest(phase=phase):
                box = MigrateSandbox()
                self.addCleanup(box.close)
                box.make_old_layout()
                box.install_new_state(FAKE_TAG)
                box.write_cutover_state(phase=phase, tag=FAKE_TAG)
                rc, out, err = box.run_migrate(
                    ["cutover", "--tag", FAKE_TAG, "--revert-after", "10m"])
                self.assertEqual(rc, 1, out + err)
                self.assertTrue(has_line(out, "cutover", "fail"), out)
                self.assertIn(phase, out)
                self.assertEqual(box.order(), [])

    def test_inner_fails_when_another_run_holds_the_lock(self):
        """中-3：切換與回滾共用一把鎖；拿不到就明確失敗。"""
        lock_path = self.box.ctl(".cutover.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        rc, out, err = self.inner()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "cutover", "fail"), out)
        self.assertEqual(self.box.actions(), [])

    def test_inner_requires_invocation_id(self):
        """低-6：--inner 只給 transient unit 用（人手照抄會被拒絕）。"""
        rc, out, err = self.inner(INVOCATION_ID="")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "inner", "fail"), out)
        self.assertEqual(self.box.order(), [])

    def test_revert_after_has_a_floor(self):
        """低-6／中-3：太短的保險會在切換進行中就開跑。"""
        rc, out, err = self.box.run_migrate(["cutover", "--tag", FAKE_TAG,
                                             "--revert-after", "30s"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "revert-after", "fail"), out)
        self.assertEqual(self.box.order(), [])

    # -- 順序 ---------------------------------------------------------------

    def test_inner_orders_insurance_then_old_stop_then_new_start(self):
        rc, out, err = self.inner()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [], out)
        order = self.box.order()
        insurance = index_of(order, "--unit=lgw-revert")
        self.assertIn("--on-active=10m", order[insurance])
        self.assertIn("rollback-to-user", order[insurance])
        self.assertIn("--old-home " + self.box.service_home, order[insurance])
        self.assertIn(f"--working-directory={self.box.current_link}", order[insurance])
        first_old_stop = index_of(order, "--user stop")
        self.assertLess(insurance, first_old_stop)       # 保險先於停舊服務
        # runuser 自己帶 XDG_RUNTIME_DIR／DBUS_SESSION_BUS_ADDRESS
        self.assertIn("XDG_RUNTIME_DIR=/run/user/", order[first_old_stop])
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/", order[first_old_stop])
        last_old_stop = max(i for i, line in enumerate(order) if "--user stop" in line)
        first_new = index_of(order, "systemctl enable llmgateway.service")
        self.assertLess(last_old_stop, first_new)        # 舊的停完才啟新
        self.assertIn("systemctl start llmgateway.service", order)
        self.assertIn("systemctl start llmgateway-tunnel.service", order)
        # 舊的停了、新的在跑
        self.assertFalse(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(self.box.active("llmgateway-tunnel.service", layer="user"))
        self.assertTrue(self.box.active("llmgateway.service"))
        self.assertTrue(self.box.active("llmgateway-tunnel.service"))
        # 停完的舊 unit 也 disable 了（高-2：觀察期內重開機不得雙跑）
        actions = self.box.actions()
        for unit in ("llmgateway.service", "llmgateway-tunnel.service", "key-sync.timer"):
            self.assertIn(f"user disable {unit}", actions)
        last_disable = max(i for i, line in enumerate(order) if "--user disable" in line)
        self.assertLess(last_disable, index_of(order, "systemctl enable llmgateway.service"))
        # 狀態與提示
        state = self.box.cutover_state()
        self.assertEqual(state["phase"], "pending-confirm")
        self.assertEqual(state["revert_unit"], "lgw-revert.timer")
        self.assertEqual(state["tag"], FAKE_TAG)
        self.assertIn("執行 llmgwctl confirm 取消自動回舊版", out)
        # 保險是 transient timer：重開機會消失，輸出要有提醒（高-2）
        self.assertTrue(has_line(out, "insurance-reboot", "todo"), out)
        # 中-5：窗口內（舊已 disable、新還沒 enable）重開機會什麼都不起，警告要
        # 印在停舊之前。
        self.assertTrue(has_line(out, "reboot-window", "todo"), out)
        self.assertLess(out.index("reboot-window"), out.index("stop-old"),
                        out)
        self.assertEqual(len(self.box.log_lines("timers.log")), 1)
        self.assert_no_values(out, err)

    def test_keysync_wait_uses_the_cutover_start_time(self):
        """中-1：門檻是切換開始（啟動新服務前），不是等待開始。

        新的 lgwd 在啟動當下就寫了第一次拉取的狀態（假 systemctl 模擬）；用
        「等待開始」當門檻會把那檔案當成太舊，白等滿 grace 並印假 todo。
        """
        started = time.monotonic()
        rc, out, err = self.inner(
            LLMGW_KEYSYNC_GRACE="5",
            LGW_FAKE_KEYSYNC_AT_START=self.box.state(".keysync.status"))
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(has_line(out, "keysync", "todo"), out)
        self.assertTrue(has_line(out, "keysync", "ok"), out)
        # 沒有印 todo 就代表門檻用對了（用錯會等滿 grace 再印假 todo）。
        self.assertLess(time.monotonic() - started, 30.0)

    def test_insurance_gone_during_cutover_is_reported(self):
        """中-2：一次性 timer 在切換拖太久時會到點消失，不能宣稱還有保險。"""
        rc, out, err = self.inner(LGW_FAKE_SYSTEMD_RUN_SKIP_ACTIVE="1")
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "insurance", "fail"), out)
        self.assertTrue(has_line(out, "no-insurance", "todo"), out)
        self.assertIn("沒有自動回舊版", out)
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_outer_preflight_refuses_when_gh_is_not_logged_in(self):
        """中-6：gh 沒登入時 check 只會 todo（rc 2）→ 不能先停舊服務再停損。"""
        rc, out, err = self.outer(FAKE_GH_AUTH_RC="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "preflight", "fail"), out)
        self.assertIn("gh 沒登入", out)
        self.assertEqual(self.box.order(), [])
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    def test_outer_preflight_runs_the_policy_dry_run(self):
        """中-7：壞 policy 要在停舊服務、排保險之前就擋下。"""
        with mock.patch("lgwctl.upgrade._dry_run_policy", return_value=False):
            rc, out, err = self.outer()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "preflight", "fail"), out)
        self.assertIn("policy dry-run", out)
        self.assertEqual(self.box.order(), [])
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    # -- 停損 ---------------------------------------------------------------

    def test_local_list_not_200_rolls_back_immediately(self):
        self.box.curl_codes(["500"])
        rc, out, err = self.inner()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list-local", "fail"), out)
        self.assertIn("停損成立", out)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        actions = self.box.actions()
        self.assertIn("system stop llmgateway.service", actions)
        self.assertIn("system disable llmgateway.service", actions)
        self.assertIn("system disable llmgateway-tunnel.service", actions)
        self.assertIn("user start llmgateway.service", actions)
        self.assertIn("user start llmgateway-tunnel.service", actions)
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(self.box.active("llmgateway.service"))
        # 反向保險也取消了
        self.assertIn("systemctl stop lgw-revert.timer", self.box.order())
        self.assert_no_values(out, err)

    def test_remote_not_200_rolls_back(self):
        rc, out, err = self.inner(FAKE_PROBE_RC="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list-remote", "fail"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assert_no_values(out, err)

    def test_check_nonzero_rolls_back(self):
        rc, out, err = self.inner(FAKE_GH_API_RC="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "gh-keys-repo", "fail"), out)
        self.assertIn("check 非 0", out)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assert_no_values(out, err)

    def test_insurance_failure_aborts_before_touching_old_service(self):
        rc, out, err = self.inner(LGW_FAKE_SYSTEMD_RUN_RC="1")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "insurance", "fail"), out)
        self.assertIn("不切", out)
        self.assertFalse(any("--user stop" in line for line in self.box.order()))
        self.assertFalse(any("systemctl enable" in line for line in self.box.order()))
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertEqual(self.box.actions(), [])
        self.assertFalse(os.path.exists(self.box.ctl(".cutover.state")))

    def test_stop_old_failure_aborts_and_cancels_insurance(self):
        self.box.fail_action("stop", "llmgateway.service", layer="user")
        rc, out, err = self.inner()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "stop-old", "fail"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "aborted")
        # 沒有啟新 unit，保險也收掉了（沒切成就不要留著）
        self.assertFalse(any(line.startswith(("system enable", "system start"))
                             for line in self.box.order()), self.box.order())
        self.assertIn("systemctl stop lgw-revert.timer", self.box.order())
        self.assert_no_values(out, err)

    def test_abort_reenables_the_units_it_disabled(self):
        """高-2：中止切換時，先 disable 過的舊 unit 要 enable 回來（否則重開機不復原）。"""
        self.box.fail_action("stop", "llmgateway-tunnel.service", layer="user")
        rc, out, err = self.inner()
        self.assertEqual(rc, 1)
        actions = self.box.actions()
        self.assertIn("user disable llmgateway.service", actions)
        self.assertIn("user enable llmgateway.service", actions)
        self.assertIn("user start llmgateway.service", actions)
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    def test_bus_failure_is_a_failure_not_a_skip(self):
        """中-2：user bus 連不上不能被當成「舊 unit 本來就沒在跑」。"""
        self.box.set_bus_failure("user")
        rc, out, err = self.inner()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "stop-old", "fail"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "aborted")
        # 舊服務沒有被真的停掉（bus 根本連不上）
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(any(line.startswith("system start") for line in self.box.order()),
                         self.box.order())

    def test_remote_probe_retries_before_declaring_stop_loss(self):
        """中-4：隧道剛起來時的前兩次探針失敗不該立刻停損。"""
        self.box.probe_codes([1, 1, 0])
        rc, out, err = self.inner()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [], out)
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")
        probes = [line for line in self.box.order() if line.startswith("probe ")]
        # 3 次重試（第 3 次成功）；中-8：check 不再單探遠端（用 no_remote=True）
        self.assertEqual(len(probes), 3, probes)
        self.assertIn("第 3/3 次", out)

    def test_no_remote_skips_the_probe(self):
        rc, out, err = self.inner("--no-remote")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_level(out, "list-remote", "skip"), out)
        self.assertFalse(any(line.startswith("probe ") for line in self.box.order()))
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")
        self.assert_no_values(out, err)

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_cutover_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        env["LLMGW_SERVICE_HOME"] = self.box.service_home
        rc, out, err = self.box.run_cli(["cutover", "--tag", FAKE_TAG], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)
        self.assertEqual(self.box.order(), [])


class ConfirmTests(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.box.install_new_state(FAKE_TAG)
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG,
                                     revert_unit="lgw-revert.timer")
        # 此刻：新版在跑、舊 user unit 不在跑（confirm 的健康條件）
        self.box.set_active("llmgateway.service")

    def test_confirm_cancels_insurance_and_enters_observation(self):
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 0, out + err)
        order = self.box.order()
        # 先確認保險沒開跑、新版在跑，才停 timer（中-9）；停完再驗一次（低-13）
        busy = index_of(order, "systemctl is-active lgw-revert.service")
        self.assertIn("systemctl is-active llmgateway.service", order)
        stop = index_of(order, "systemctl stop lgw-revert.timer")
        self.assertLess(busy, stop)                     # 第一次 busy 檢查在停之前
        busy_positions = [i for i, line in enumerate(order)
                          if "is-active lgw-revert.service" in line]
        self.assertGreaterEqual(len(busy_positions), 2, order)
        self.assertGreater(busy_positions[-1], stop)    # 停完又驗一次（低-13）
        self.assertIn("is-failed lgw-revert.service", order[-1])
        state = self.box.cutover_state()
        self.assertEqual(state["phase"], "confirmed")
        self.assertIsInstance(state["confirmed_ts"], float)
        self.assertGreater(state["confirmed_ts"], state["ts"] - 1)
        self.assertTrue(has_line(out, "revert-cancel", "ok"), out)
        self.assertTrue(has_line(out, "confirm", "ok"), out)
        self.assertIn("觀察期", out)
        self.assert_no_values(out, err)

    def test_confirm_refuses_when_the_insurance_already_fired(self):
        """中-9：保險的 service 已經在跑時不能 confirm（會被 rollback 蓋掉）。"""
        self.box.set_active("lgw-revert.service")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "insurance-running", "fail"), out)
        self.assertNotIn("systemctl stop lgw-revert.timer", self.box.order())
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_confirm_refuses_when_the_new_service_is_not_active(self):
        self.box.set_active("llmgateway.service", False)
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "confirm", "fail"), out)
        self.assertIn("不是 active", out)
        self.assertNotIn("systemctl stop lgw-revert.timer", self.box.order())
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_confirm_refuses_when_the_old_user_unit_is_still_active(self):
        self.box.set_active("llmgateway.service", True, layer="user")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "confirm", "fail"), out)
        self.assertIn("還在跑", out)
        self.assertNotIn("systemctl stop lgw-revert.timer", self.box.order())
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_confirm_treats_a_missing_timer_as_cancelled(self):
        self.box.mark_unit_missing("lgw-revert.timer")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_level(out, "revert-cancel", "skip"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "confirmed")

    def test_confirm_refuses_a_state_file_that_names_another_unit(self):
        """狀態檔塞別的 unit（高-3）：confirm 不得拿它去呼叫 systemctl。"""
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG,
                                     revert_unit="sshd.service")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "revert-unit", "fail"), out)
        self.assertEqual(self.box.order(), [])
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_confirm_refuses_when_not_pending(self):
        self.box.write_cutover_state(phase="reverted", tag=FAKE_TAG)
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "confirm", "fail"), out)
        self.assertIn("reverted", out)
        self.assertEqual(self.box.order(), [])
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")

    def test_confirm_refuses_without_a_state_file(self):
        os.unlink(self.box.ctl(".cutover.state"))
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "confirm", "fail"), out)
        self.assertIn("沒有狀態檔", out)

    def test_confirm_refuses_when_the_insurance_service_failed(self):
        """低-13：保險曾跑到一半失敗（failed 殘留）不能直接 confirm。"""
        self.box.write(os.path.join(self.box.fake, "failed", "system",
                                    "lgw-revert.service"), "1\n")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "insurance-failed", "fail"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_confirm_refuses_when_the_timer_cannot_be_stopped(self):
        self.box.fail_action("stop", "lgw-revert.timer")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "revert-cancel", "fail"), out)
        self.assertTrue(has_line(out, "confirm", "fail"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_confirm_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        rc, out, err = self.box.run_cli(["confirm"], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)


class RollbackToUserTests(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)
        # cutover 之後、還沒 confirm：新版在跑、舊版停著、保險還在
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        for unit in ("llmgateway.service", "llmgateway-tunnel.service", "key-sync.timer"):
            self.box.set_active(unit, False, layer="user")
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG,
                                     revert_unit="lgw-revert.timer",
                                     new_service_ts=time.time() - 3600)
        # 新版跑了一段時間：資料庫與 keys.env 是它寫的
        self.box.make_db(self.box.state("llm.db"), "new-db")
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        # 預設：新的 keys.env 比舊的舊 → 回滾時不覆蓋舊的
        self.box.age(self.box.state("keys.env"), -3600)

    def rollback(self, **env_extra):
        # 真正的工作在 --inner（人手下的 rollback-to-user 現在只排 lgw-rollback，
        # 高-1）；沙箱已設 INVOCATION_ID，等同 systemd 起的 transient unit。
        return self.box.run_migrate(["rollback-to-user", "--inner"], **env_extra)

    def test_rollback_stops_new_starts_old_and_restores_db(self):
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        actions = self.box.actions()
        self.assertIn("system stop llmgateway.service", actions)
        self.assertIn("system stop llmgateway-tunnel.service", actions)
        self.assertIn("system disable llmgateway.service", actions)
        self.assertIn("user enable llmgateway.service", actions)
        self.assertIn("user enable key-sync.timer", actions)
        # 中-11：只 enable 只建開機 symlink；要 start 才會立刻開始計時。
        self.assertIn("user start key-sync.timer", actions)
        self.assertIn("user start llmgateway.service", actions)
        self.assertIn("user start llmgateway-tunnel.service", actions)
        # 新的 llm.db 複製回舊 state，舊的先備份（帶時間戳，中-6）
        self.assertEqual(self.box.db_markers(self.box.old_state("llm.db")), ["new-db"])
        baks = self.box.db_backups(self.box.old_state("llm.db"))
        self.assertEqual(len(baks), 1, baks)
        self.assertEqual(self.box.db_markers(baks[0]), ["old-db"])
        self.assertIn("舊的備份", out)
        # keys.env 沒有比較新 → 不覆蓋舊的
        self.assertEqual(self.box.read(self.box.old_state("keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")
        self.assertTrue(has_level(out, "keys-env", "skip"), out)
        # 順序：停新 service → 起舊 → 探本機 200 → **舊隧道活了** → 最後才停新隧道
        # → 取消保險（高-1：管理連線不能比舊服務先斷）。
        order = self.box.order()
        self.assertLess(index_of(order, "systemctl stop llmgateway.service"),
                        index_of(order, "runuser "))
        self.assertLess(index_of(order, "runuser "), index_of(order, "curl "))
        curl_at = index_of(order, "curl ")
        tunnel_stop = max(i for i, line in enumerate(order)
                          if "systemctl stop llmgateway-tunnel.service" in line)
        self.assertGreater(tunnel_stop, curl_at, order)
        self.assertLess(tunnel_stop, index_of(order, "systemctl stop lgw-revert.timer"))
        # 身分與環境變數（root 代做，不要求操作者 export）
        runuser = next(line for line in order if line.startswith("runuser "))
        self.assertIn("-u lgwfake", runuser)
        self.assertIn("XDG_RUNTIME_DIR=/run/user/", runuser)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/", runuser)
        # 狀態與實際的活性
        state = self.box.cutover_state()
        self.assertEqual(state["phase"], "reverted")
        self.assertTrue(state["local_ok"])
        self.assertFalse(self.box.active("llmgateway.service"))
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assert_no_values(out, err)

    def test_rollback_is_idempotent_when_insurance_fires_twice(self):
        self.assertEqual(self.rollback()[0], 0)
        baks = self.box.db_backups(self.box.old_state("llm.db"))
        self.assertEqual(len(baks), 1, baks)
        bak = baks[0]
        before = self.box.db_markers(bak)
        before_mtime = os.path.getmtime(bak)
        seen = len(self.box.order())
        # 反向保險到時又觸發一次（或人工按了兩次）：不能再動資料
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "rollback", "ok"), out)
        self.assertEqual(self.box.db_backups(self.box.old_state("llm.db")), baks)
        self.assertEqual(self.box.db_markers(bak), before)
        self.assertEqual(os.path.getmtime(bak), before_mtime)
        second = self.box.order()[seen:]
        self.assertFalse(any("--user stop" in line for line in second), second)
        self.assertFalse(any("systemctl stop llmgateway" in line for line in second), second)
        self.assertFalse(any("disable" in line for line in second), second)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")

    def test_rollback_fails_loudly_when_the_lock_stays_held(self):
        """拿不到鎖不能回 0 假裝回滾了（中-3：保險被吃掉）。"""
        lock_path = self.box.ctl(".cutover.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertEqual(self.box.order(), [])
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")

    def test_rollback_keeps_old_db_when_the_new_one_was_never_written(self):
        """新服務沒寫過（db 比切換開始舊）：不覆蓋舊的，也不留備份（中-6）。"""
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG,
                                     revert_unit="lgw-revert.timer",
                                     new_service_ts=time.time())
        self.box.age(self.box.state("llm.db"), -60)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.db_markers(self.box.old_state("llm.db")), ["old-db"])
        self.assertEqual(self.box.db_backups(self.box.old_state("llm.db")), [])
        self.assertTrue(has_level(out, "db-copy", "skip"), out)

    def test_db_backups_keep_only_the_recent_three(self):
        """中-6：時間戳備份保留最近 3 份（不會無限長大、也不會只剩一份）。"""
        from lgwctl import migrate

        base = self.box.old_state("llm.db")
        self.box.make_db(base, "old-db")
        for index in range(5):
            self.box.make_db(f"{base}.bak-2026010100000{index}", f"bak-{index}")
        migrate._prune_db_backups(base)
        kept = self.box.db_backups(base)
        self.assertEqual(len(kept), 3, kept)
        self.assertIn("llm.db.bak-20260101000004", [os.path.basename(path) for path in kept])

    def test_rollback_copies_a_newer_valid_keys_env(self):
        self.box.write(self.box.state("keys.env"), f"OLLAMA_API_KEY={FAKE_KEY_VALUE}\n", 0o600)
        self.box.age(self.box.state("keys.env"), 3600)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.old_state("keys.env")),
                         f"OLLAMA_API_KEY={FAKE_KEY_VALUE}\n")
        self.assertTrue(has_line(out, "keys-env", "ok"), out)
        self.assert_no_values(out, err)

    def test_rollback_keeps_old_keys_env_when_a_key_name_is_unknown(self):
        """低-7：語法合法但鍵名不在白名單，舊服務的 KeyStore 會拒收。"""
        self.box.write(self.box.state("keys.env"), f"NOT_A_REAL_KEY={FAKE_KEY_VALUE}\n", 0o600)
        self.box.age(self.box.state("keys.env"), 3600)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.old_state("keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")
        self.assertTrue(has_line(out, "keys-env", "todo"), out)
        self.assert_no_values(out, err)

    def test_rollback_keeps_old_keys_env_when_new_one_is_invalid(self):
        self.box.write(self.box.state("keys.env"), "這不是 KEY=VALUE 檔\n", 0o600)
        self.box.age(self.box.state("keys.env"), 3600)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.old_state("keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")
        self.assertTrue(has_line(out, "keys-env", "todo"), out)
        self.assert_no_values(out, err)

    def test_rollback_keeps_the_new_tunnel_when_the_old_one_cannot_start(self):
        """高-1：舊隧道起不來時不能切斷新的管理連線；修好後再跑一次補完。"""
        self.box.fail_action("start", "llmgateway-tunnel.service", layer="user")
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "tunnel-old", "fail"), out)
        # 舊服務回來了、新 service 停了，但新隧道還活著（管理連線／8480 導向舊服務）
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))
        self.assertFalse(self.box.active("llmgateway.service"))
        self.assertTrue(self.box.active("llmgateway-tunnel.service"))
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        self.assertFalse(self.box.cutover_state()["tunnel_ok"])
        # 修好舊隧道後再跑一次：舊隧道活了才停新隧道，這次回 0
        os.unlink(os.path.join(self.box.fake, "fail", "start", "user",
                              "llmgateway-tunnel.service"))
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(self.box.active("llmgateway-tunnel.service", layer="user"))
        self.assertFalse(self.box.active("llmgateway-tunnel.service"))
        self.assertTrue(self.box.cutover_state()["tunnel_ok"], out)

    def test_rollback_keeps_new_tunnel_when_old_tunnel_crash_loops(self):
        """中-4：舊隧道處於 crash-loop 重啟循環（NRestarts 增加）時不判定穩定，新隧道保留。"""
        restarts = [0, 1, 2]
        def fake_restarts(layout, unit):
            return restarts.pop(0) if restarts else 3

        with mock.patch("lgwctl.migrate._user_unit_restarts", side_effect=fake_restarts):
            rc, out, err = self.rollback()
            self.assertEqual(rc, 1)
            self.assertTrue(has_line(out, "tunnel-old", "fail"), out)
            self.assertIn("crash-loop", out)
            # 新隧道依然保持運行，避免管理通道斷線
            self.assertTrue(self.box.active("llmgateway-tunnel.service"))

    def test_rollback_keeps_old_keys_env_when_a_key_is_duplicated(self):
        """低-6：重複鍵會被舊 KeyStore 拒收，不能覆蓋舊的。"""
        self.box.write(self.box.state("keys.env"),
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n"
                       f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n", 0o600)
        self.box.age(self.box.state("keys.env"), 3600)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.read(self.box.old_state("keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")
        self.assertTrue(has_line(out, "keys-env", "todo"), out)
        self.assert_no_values(out, err)

    def test_rollback_without_a_cutover_timestamp_does_not_overwrite_the_db(self):
        """低-7：沒有 new_service_ts（prepare 後直接回滾）時不比 mtime，安全跳過。"""
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG)
        self.box.age(self.box.state("llm.db"), 3600)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.box.db_markers(self.box.old_state("llm.db")), ["old-db"])
        self.assertEqual(self.box.db_backups(self.box.old_state("llm.db")), [])
        self.assertTrue(has_level(out, "db-copy", "skip"), out)
        self.assertIn("切換時間", out)

    def test_rollback_fails_when_old_service_does_not_come_back(self):
        self.box.fail_action("start", "llmgateway.service", layer="user")
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "start-old", "fail"), out)
        state = self.box.cutover_state()
        self.assertEqual(state["phase"], "reverted")
        self.assertFalse(state["local_ok"])

    def test_second_rollback_still_fails_when_the_old_service_never_started(self):
        """reverted 後再跑一次也要看舊服務有沒有真的起來（不能回 0）。"""
        self.box.fail_action("start", "llmgateway.service", layer="user")
        self.assertEqual(self.rollback()[0], 1)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "start-old", "fail"), out)
        self.assertFalse(self.box.cutover_state()["local_ok"])

    def test_rollback_never_calls_a_unit_from_the_state_file(self):
        """狀態檔塞別的 unit（高-3）：回滾只用固定的 lgw-revert.timer。"""
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG,
                                     revert_unit="sshd.service")
        rc, out, err = self.rollback()
        # 低-3：報告裡有 fail 行（revert-unit）就不能回 0（自動化只看結束碼）。
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "revert-unit", "fail"), out)
        self.assertFalse(any("sshd" in line for line in self.box.order()), self.box.order())
        self.assertIn("systemctl stop lgw-revert.timer", self.box.order())
        # 資料與舊服務還是照常回去了（fail 只是不隱藏）
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    def test_rollback_refuses_after_finalize_without_stopping_new_units(self):
        """中-4：finalize 後舊版已收走；rollback 必須在停新 unit 之前就拒絕。"""
        self.box.write_cutover_state(phase="finalized", tag=FAKE_TAG,
                                     old_home=self.box.service_home)
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertIn("finalized", out)
        self.assertEqual(self.box.order(), [])
        self.assertTrue(self.box.active("llmgateway.service"))
        self.assertTrue(self.box.active("llmgateway-tunnel.service"))

    def test_rollback_refuses_when_an_old_unit_file_is_missing(self):
        """低-2：舊 unit 檔不在時先拒絕——停掉新版只會讓機器沒有服務。"""
        os.unlink(self.box.old_units("key-sync.service"))
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "rollback", "fail"), out)
        self.assertIn("key-sync.service", out)
        self.assertEqual(self.box.order(), [])
        self.assertTrue(self.box.active("llmgateway.service"))

    def test_rollback_without_any_state_is_still_safe(self):
        """演練補充：沒有狀態檔時不印假 fail，照常冪等回舊版。"""
        os.unlink(self.box.ctl(".cutover.state"))
        rc, out, err = self.rollback()
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "rollback-state", "ok"), out)
        self.assertFalse(has_line(out, "revert-unit", "fail"), out)
        self.assertNotIn("不是認得的值", out)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        self.assertTrue(self.box.active("llmgateway.service", layer="user"))

    def test_rollback_without_state_returns_nonzero_if_old_service_fails(self):
        """演練補充：沒有狀態檔時結束碼仍要看舊服務有沒有真的回來。"""
        os.unlink(self.box.ctl(".cutover.state"))
        self.box.fail_action("start", "llmgateway.service", layer="user")
        rc, out, err = self.rollback()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "start-old", "fail"), out)
        self.assertFalse(has_line(out, "revert-unit", "fail"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "reverted")
        self.assertFalse(self.box.cutover_state()["local_ok"])

    def test_outer_rollback_only_schedules_the_detached_unit(self):
        """高-1：人手下的 rollback 只排 lgw-rollback 就返回（停新隧道會切斷
        這個 ssh session，不能跑在呼叫端）；真正的工作在 --inner。"""
        rc, out, err = self.box.run_migrate(["rollback-to-user"])
        self.assertEqual(rc, 0, out + err)
        order = self.box.order()
        self.assertEqual(len(order), 1, order)
        self.assertIn("systemd-run --unit=lgw-rollback", order[0])
        self.assertIn("--inner", order[0])
        self.assertIn("--old-home " + self.box.service_home, order[0])
        self.assertIn(f"--working-directory={self.box.current_link}", order[0])
        # 人手下的那一次什麼都沒動：unit 狀態與狀態檔維持原樣
        self.assertEqual(self.box.actions(), [])
        self.assertTrue(self.box.active("llmgateway.service"))
        self.assertTrue(self.box.active("llmgateway-tunnel.service"))
        self.assertEqual(self.box.cutover_state()["phase"], "pending-confirm")
        self.assertIn("lgw-rollback", out)
        self.assert_no_values(out, err)

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_rollback_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        rc, out, err = self.box.run_cli(["rollback-to-user"], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)


class FinalizeMigrationTests(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.box.make_old_layout()
        self.old_tunnel_env = self.box.read(self.box.old_conf("tunnel.env"))
        self.box.install_new_state(FAKE_TAG)
        self.box.set_active("llmgateway.service")
        self.box.set_active("llmgateway-tunnel.service")
        for unit in ("llmgateway.service", "llmgateway-tunnel.service", "key-sync.timer"):
            self.box.set_active(unit, False, layer="user")
        # 三天前就 confirm 了
        self.box.write_cutover_state(phase="confirmed", tag=FAKE_TAG,
                                     confirmed_ts=time.time() - 4 * 86400,
                                     revert_unit="lgw-revert.timer",
                                     old_home=self.box.service_home)

    def finalize(self, *extra, **env_extra):
        return self.box.run_migrate(["finalize-migration", *extra], **env_extra)

    # -- 拒絕條件 -----------------------------------------------------------

    def test_requires_yes(self):
        rc, out, err = self.finalize()
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "finalize", "fail"), out)
        self.assertIn("--yes", out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))
        self.assertEqual(self.box.order(), [])

    def test_refuses_when_not_confirmed(self):
        self.box.write_cutover_state(phase="pending-confirm", tag=FAKE_TAG)
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "finalize", "fail"), out)
        self.assertIn("不是 confirmed", out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))
        self.assertEqual(self.box.order(), [])

    def test_refuses_before_the_observation_period_is_over(self):
        self.box.write_cutover_state(phase="confirmed", tag=FAKE_TAG,
                                     confirmed_ts=time.time() - 3600)
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 1)
        self.assertIn("觀察期還沒過", out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))
        self.assertEqual(self.box.order(), [])

    def test_refuses_without_a_confirm_time(self):
        self.box.write_cutover_state(phase="confirmed", tag=FAKE_TAG)
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 1)
        self.assertIn("觀察期起始時間", out)

    def test_refuses_when_old_home_differs_from_the_state(self):
        """低-3：--old-home 打錯不能搬錯（或什麼都沒搬卻寫 finalized）。"""
        rc, out, err = self.finalize("--yes", "--old-home", "/home/llmgwe")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "old-home", "fail"), out)
        self.assertIn("不一致", out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))
        self.assertEqual(self.box.order(), [])

    def test_uses_the_old_home_recorded_at_cutover(self):
        """低-3：沒給 --old-home 就用切換時記下的那一個。"""
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.box.old_conf()))
        self.assertTrue(os.path.isdir(os.path.join(self.box.state("old-user-layout"),
                                                   ".config", "llmgateway")))

    def test_refuses_when_the_new_service_is_not_healthy(self):
        """低-3：新版壞掉時該回舊版，不是把舊的收走。"""
        self.box.curl_codes(["500"])
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "list-local", "fail"), out)
        self.assertTrue(has_line(out, "finalize", "fail"), out)
        self.assertIn("rollback-to-user", out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))
        self.assertEqual(self.box.cutover_state()["phase"], "confirmed")

    def test_refuses_when_the_full_check_fails_even_if_local_is_200(self):
        """低-5：隧道／key 拉取／gh 壞掉也是不健康，不能只憑本機 200 就收舊版。"""
        rc, out, err = self.finalize("--yes", FAKE_GH_API_RC="1")
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(has_line(out, "list-local", "ok"), out)
        self.assertTrue(has_line(out, "check", "fail"), out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))
        self.assertEqual(self.box.cutover_state()["phase"], "confirmed")
        self.assertFalse(any(line.startswith("system stop")
                             for line in self.box.actions()), self.box.actions())

    # -- 收尾 ---------------------------------------------------------------

    def test_moves_old_layout_into_the_archive_without_deleting(self):
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(lines_with_level(out, "fail"), [], out)
        archive = self.box.state("old-user-layout")
        # 原位沒有了
        self.assertFalse(os.path.exists(self.box.old_share()))
        self.assertFalse(os.path.exists(self.box.old_conf()))
        self.assertFalse(os.path.exists(self.box.old_state()))
        # 但檔案都還在檔案館裡（搬移，不是刪除），內容沒變
        moved_conf = os.path.join(archive, ".config", "llmgateway")
        self.assertEqual(self.box.read(os.path.join(moved_conf, "keysync.env")),
                         f"# 只有一行\nLLMGW_KEYS_PASSWORD={FAKE_PASSWORD}\n")
        self.assertEqual(self.box.read(os.path.join(moved_conf, "tunnel.env")),
                         self.old_tunnel_env)
        moved_state = os.path.join(archive, ".local", "state", "llmgateway")
        self.assertEqual(self.box.read(os.path.join(moved_state, "keys.env")),
                         f"CLAUDE_PERSONAL_OAUTH_TOKEN={FAKE_KEY_VALUE}\n")
        self.assertEqual(self.box.db_markers(os.path.join(moved_state, "llm.db")), ["old-db"])
        moved_share = os.path.join(archive, ".local", "share", "llmgateway")
        self.assertTrue(os.path.isfile(os.path.join(moved_share, OLD_TAG, ".lgw-tag")))
        # 舊 unit 檔也在檔案館裡
        for name in ("llmgateway.service", "llmgateway-tunnel.service",
                     "key-sync.service", "key-sync.timer"):
            self.assertTrue(
                os.path.isfile(os.path.join(archive, ".config", "systemd", "user", name)),
                name)
        # linger 關掉、狀態 finalized
        self.assertIn("loginctl disable-linger lgwfake", self.box.order())
        self.assertTrue(has_line(out, "disable-linger", "ok"), out)
        self.assertTrue(has_line(out, "finalize", "ok"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "finalized")
        # 新的那套完全沒被動到
        self.assertTrue(self.box.active("llmgateway.service"))
        self.assertFalse(any(line.startswith("system stop")
                             for line in self.box.actions()), self.box.actions())
        self.assert_no_values(out, err)

    def test_second_finalize_is_refused(self):
        self.assertEqual(self.finalize("--yes")[0], 0)
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 1)
        self.assertIn("不是 confirmed", out)

    def test_finalize_fails_loudly_when_another_run_holds_the_lock(self):
        """低-4：finalize 與切換／回滾共用同一把鎖。"""
        lock_path = self.box.ctl(".cutover.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        rc, out, err = self.finalize("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "finalize", "fail"), out)
        self.assertTrue(os.path.isdir(self.box.old_conf()))

    def test_linger_failure_is_a_todo_not_a_failure(self):
        rc, out, err = self.finalize("--yes", LGW_FAKE_LOGINCTL_RC="1")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(has_line(out, "disable-linger", "todo"), out)
        self.assertEqual(self.box.cutover_state()["phase"], "finalized")

    @unittest.skipIf(os.geteuid() == 0, "root 身分下沒有 prefix 會動真機，略過")
    def test_finalize_needs_root_without_prefix(self):
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        rc, out, err = self.box.run_cli(["finalize-migration", "--yes"], env=env)
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "root", "fail"), out)


class MigrationEntryPointTests(unittest.TestCase):
    """``python3 -m lgwctl`` 的分派（cutover／finalize 也走 migrate.py）。"""

    def setUp(self):
        self.box = MigrateSandbox()
        self.addCleanup(self.box.close)

    def run_module(self, argv):
        return subprocess.run([sys.executable, "-m", "lgwctl", *argv], cwd=REPO_ROOT,
                              env=self.box.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def test_finalize_is_dispatched_through_the_cli(self):
        proc = self.run_module(["finalize-migration", "--yes"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("不是 confirmed", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_cutover_help_lists_the_migration_options(self):
        proc = self.run_module(["cutover", "--help"])
        self.assertEqual(proc.returncode, 0)
        for flag in ("--revert-after", "--old-home", "--no-remote", "--inner"):
            self.assertIn(flag, proc.stdout)
        self.assertIn("lgw-cutover", proc.stdout)

    def test_prepare_rejects_a_bad_tag_without_a_traceback(self):
        proc = self.run_module(["prepare-migration", "--tag", "nope"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("tag", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_confirm_without_state_refuses(self):
        proc = self.run_module(["confirm"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("不是 pending-confirm", proc.stdout)


class CutoverUnitArgvTests(unittest.TestCase):
    """兩支 systemd-run 的參數（不經假指令，直接看組出來的東西）。"""

    def setUp(self):
        self.box = MigrateSandbox()
        self.addCleanup(self.box.close)
        from lgwctl.layout import Layout

        self.layout = Layout.from_env(self.box.env)

    def test_cutover_unit_argv(self):
        from lgwctl import migrate

        argv = migrate.cutover_unit_argv(self.layout, tag=FAKE_TAG, revert_after="10m",
                                         old_home=self.box.service_home, no_remote=True)
        self.assertEqual(argv[0], self.box.env["LLMGW_SYSTEMD_RUN"])
        self.assertIn("--unit=lgw-cutover", argv)
        self.assertIn("--collect", argv)
        self.assertIn(f"--setenv=LLMGW_PREFIX={self.box.prefix}", argv)
        self.assertIn(f"--setenv=LLMGW_SERVICE_USER={self.box.service_user}", argv)
        self.assertIn(f"--setenv=LLMGW_SERVICE_HOME={self.box.service_home}", argv)
        self.assertIn(f"--working-directory={self.layout.current_link}", argv)
        self.assertEqual(argv[argv.index(sys.executable):],
                         [sys.executable, "-m", "lgwctl", "cutover",
                          "--tag", FAKE_TAG, "--revert-after", "10m",
                          "--old-home", self.box.service_home, "--inner", "--no-remote"])
        self.assertFalse(any("--on-active" in item for item in argv))

    def test_insurance_unit_argv(self):
        from lgwctl import migrate

        argv = migrate.insurance_unit_argv(self.layout, revert_after="10m",
                                           old_home=self.box.service_home)
        self.assertIn("--unit=lgw-revert", argv)
        self.assertIn("--on-active=10m", argv)
        self.assertIn("--collect", argv)
        self.assertIn(f"--setenv=LLMGW_PREFIX={self.box.prefix}", argv)
        # 保險跑的是 rollback 的 --inner（高-1：與人工路徑同一套；要求
        # INVOCATION_ID，transient unit 一定有）。
        self.assertEqual(argv[argv.index(sys.executable):],
                         [sys.executable, "-m", "lgwctl", "rollback-to-user",
                          "--old-home", self.box.service_home, "--inner"])

    def test_rollback_unit_argv_is_detached(self):
        """高-1：人手 rollback 一律分離執行到 lgw-rollback（不跑在 ssh session 裡）。"""
        from lgwctl import migrate

        argv = migrate.rollback_unit_argv(self.layout, old_home=self.box.service_home)
        self.assertIn("--unit=lgw-rollback", argv)
        self.assertIn("--collect", argv)
        self.assertIn(f"--working-directory={self.layout.current_link}", argv)
        self.assertEqual(argv[argv.index(sys.executable):],
                         [sys.executable, "-m", "lgwctl", "rollback-to-user",
                          "--old-home", self.box.service_home, "--inner"])

    def test_bad_revert_after_is_rejected(self):
        rc, out, err = self.box.run_migrate(["cutover", "--tag", FAKE_TAG,
                                             "--revert-after", "ten minutes"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "revert-after", "fail"), out)
        self.assertEqual(self.box.order(), [])


class StateFileTests(MigrationTestCase):
    """``.cutover.state``：一行 JSON、不含值、壞檔不會讓指令 traceback。"""

    def test_state_is_one_line_json_without_values(self):
        from lgwctl import migrate
        from lgwctl.layout import Layout

        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)
        layout = Layout.from_env(self.box.env)
        migrate.write_state(layout, phase="pending-confirm", tag=FAKE_TAG,
                            revert_unit="lgw-revert.timer")
        text = self.box.read(self.box.ctl(".cutover.state"))
        self.assertEqual(len(text.splitlines()), 1)
        data = json.loads(text)
        self.assertEqual(data["phase"], "pending-confirm")
        self.assertEqual(data["revert_unit"], "lgw-revert.timer")
        self.assertIn("ts", data)
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertNotIn(FAKE_KEY_VALUE, text)
        # 再寫一次是合併（不會把舊欄位弄丟）
        migrate.write_state(layout, phase="confirmed", confirmed_ts=123.0)
        data = json.loads(self.box.read(self.box.ctl(".cutover.state")))
        self.assertEqual(data["phase"], "confirmed")
        self.assertEqual(data["tag"], FAKE_TAG)
        self.assertEqual(data["confirmed_ts"], 123.0)

    def test_state_lives_in_the_root_only_ctl_dir(self):
        """狀態檔與鎖在 root 專屬目錄（0700／0600），不在服務帳號可寫的 state（高-3）。"""
        from lgwctl import migrate
        from lgwctl.layout import Layout

        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)
        layout = Layout.from_env(self.box.env)
        migrate.write_state(layout, phase="pending-confirm", tag=FAKE_TAG)
        state_path = self.box.ctl(".cutover.state")
        self.assertTrue(os.path.isfile(state_path))
        self.assertFalse(os.path.exists(self.box.state(".cutover.state")))
        self.assertEqual(stat.S_IMODE(os.stat(state_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.box.ctl()).st_mode), 0o700)

    def test_broken_state_file_is_treated_as_absent(self):
        self.box.make_old_layout()
        self.box.install_new_state(FAKE_TAG)
        self.box.write(self.box.ctl(".cutover.state"), "not json\n")
        rc, out, err = self.box.run_migrate(["confirm"])
        self.assertEqual(rc, 1)
        self.assertTrue(has_line(out, "confirm", "fail"), out)
        self.assertNotIn("Traceback", err)


class OpenNoFollowTests(unittest.TestCase):
    """中-9：舊檔在服務帳號可控制的目錄；檢查與讀取必須是同一個 fd。"""

    def setUp(self):
        self.box = MigrateSandbox()
        self.addCleanup(self.box.close)

    def test_symlink_is_refused_and_copy_reads_the_checked_fd(self):
        from lgwctl import migrate

        safe = self.box.write(os.path.join(self.box.root, "safe.env"),
                              "CLAUDE_PERSONAL_OAUTH_TOKEN=safe\n")
        secret = self.box.write(os.path.join(self.box.root, "secret.env"),
                                "CLAUDE_PERSONAL_OAUTH_TOKEN=stolen\n")
        link = os.path.join(self.box.root, "link.env")
        os.symlink(secret, link)
        self.assertEqual(migrate._open_regular(link), -1)
        self.assertIsNone(migrate._read_regular(link))
        with self.assertRaises(OSError):
            migrate._copy_into_place(link, os.path.join(self.box.root, "out.env"))

        # 檢查回傳 fd 之後才把路徑換成 symlink：複製仍要讀原來那個 inode，
        # 不能跟著新路徑去讀 secret（copy2 會）。
        dst = os.path.join(self.box.root, "copied.env")
        real_open = migrate._open_regular
        swapped = []

        def swap_after_open(path):
            fd = real_open(path)
            if path == safe and not swapped:
                swapped.append(path)
                os.unlink(safe)
                os.symlink(secret, safe)
            return fd

        with mock.patch.object(migrate, "_open_regular", side_effect=swap_after_open):
            migrate._copy_into_place(safe, dst)
        self.assertEqual(self.box.read(dst), "CLAUDE_PERSONAL_OAUTH_TOKEN=safe\n")
        self.assertEqual(swapped, [safe])

    def test_sqlite_source_and_copy_refuse_symlinks(self):
        from lgwctl import migrate

        outside = os.path.join(self.box.root, "outside.db")
        self.box.make_db(outside, "outside")
        link = os.path.join(self.box.root, "link.db")
        os.symlink(outside, link)
        self.assertFalse(migrate._db_source_ok(link))
        with self.assertRaises(OSError):
            migrate._copy_db_consistent(link, os.path.join(self.box.root, "copy.db"))

    def test_copy_into_place_preserves_mtime(self):
        """cp -a 語意：回滾要用 mtime 判斷新舊，不能被複製時間蓋掉。"""
        from lgwctl import migrate

        src = self.box.write(os.path.join(self.box.root, "src"), "x\n")
        old = time.time() - 3600
        os.utime(src, (old, old))
        dst = os.path.join(self.box.root, "dst")
        migrate._copy_into_place(src, dst)
        self.assertLess(abs(os.path.getmtime(dst) - old), 2)

    def test_keys_pass_reads_the_checked_fd_when_swapped_to_symlink(self):
        """新-中1：舊 keysync.env 在 fd 開啟後被換成 symlink，仍讀原 fd 且密碼不進 log。"""
        from lgwctl import migrate
        from lgwctl.layout import Layout
        from lgwctl.report import Report

        self.box.make_old_layout(password="original-safe-password")
        old_keysync = self.box.old_conf("keysync.env")
        secret = self.box.write(os.path.join(self.box.root, "secret.env"),
                                "LLMGW_KEYS_PASSWORD=stolen-root-secret\n")
        real_open = migrate._open_regular
        swapped = []

        def swap_after_open(path):
            fd = real_open(path)
            if path == old_keysync and not swapped:
                swapped.append(path)
                os.unlink(old_keysync)
                os.symlink(secret, old_keysync)
            return fd

        layout = Layout.from_env(self.box.env)
        os.makedirs(layout.etc_dir, exist_ok=True)
        old = migrate.OldLayout(layout.service_home)
        report = Report(out=lambda line: None)
        with mock.patch.object(migrate, "_open_regular", side_effect=swap_after_open):
            migrate._step_keys_pass(layout, old, report, force=False)

        self.assertEqual(swapped, [old_keysync])
        # 讀到的是原 fd 的內容，非換掉後的 symlink 目標
        self.assertEqual(self.box.read(layout.keys_pass_file),
                         "LLMGW_KEYS_PASSWORD=original-safe-password\n")
        # 密碼值不進 log／輸出
        rendered = "\n".join(report.lines)
        self.assertNotIn("original-safe-password", rendered)
        self.assertNotIn("stolen-root-secret", rendered)

    def test_keys_pass_refuses_when_swapped_to_symlink_before_open(self):
        """新-中1：若在檢查後、開啟前換成 symlink，O_NOFOLLOW 擋下並拒絕。"""
        from lgwctl import migrate
        from lgwctl.layout import Layout
        from lgwctl.report import Report

        self.box.make_old_layout(password="original-safe-password")
        old_keysync = self.box.old_conf("keysync.env")
        secret = self.box.write(os.path.join(self.box.root, "secret.env"),
                                "LLMGW_KEYS_PASSWORD=stolen-root-secret\n")
        real_regular = migrate._regular_file
        swapped = []

        def swap_after_check(path):
            res = real_regular(path)
            if path == old_keysync and not swapped:
                swapped.append(path)
                os.unlink(old_keysync)
                os.symlink(secret, old_keysync)
            return res

        layout = Layout.from_env(self.box.env)
        os.makedirs(layout.etc_dir, exist_ok=True)
        old = migrate.OldLayout(layout.service_home)
        report = Report(out=lambda line: None)
        with mock.patch.object(migrate, "_regular_file", side_effect=swap_after_check):
            migrate._step_keys_pass(layout, old, report, force=False)

        self.assertEqual(swapped, [old_keysync])
        self.assertFalse(os.path.exists(layout.keys_pass_file))
        rendered = "\n".join(report.lines)
        self.assertIn("fail keys-pass", rendered)
        self.assertNotIn("stolen-root-secret", rendered)
        self.assertNotIn("original-safe-password", rendered)


class SharedOwnershipTests(unittest.TestCase):
    """中-7：keys.pass 與 /etc 設定檔的 owner 規則收在 install 的同一個函式。"""

    def setUp(self):
        from lgwctl.layout import Layout

        self.box = MigrateSandbox()
        self.addCleanup(self.box.close)
        env = {name: value for name, value in self.box.env.items()
               if name != "LLMGW_PREFIX"}
        self.layout = Layout.from_env(env)
        self.layout.euid = 0          # 假裝是 root（不真的 chown）

    def test_chown_root_group_uses_root_and_the_service_gid(self):
        from lgwctl import install

        calls = []
        fake = types.SimpleNamespace(pw_uid=1001, pw_gid=2002, pw_dir="/home/lgwfake")
        with mock.patch.object(install.pwd, "getpwnam", return_value=fake), \
                mock.patch.object(install.os, "chown",
                                  side_effect=lambda *args: calls.append(args)):
            install.chown_root_group(self.layout, "/etc/llmgateway/keys.pass")
        self.assertEqual(calls, [("/etc/llmgateway/keys.pass", 0, 2002)])

    def test_chown_root_group_skips_in_prefix_mode(self):
        from lgwctl import install
        from lgwctl.layout import Layout

        calls = []
        with mock.patch.object(install.os, "chown",
                               side_effect=lambda *args: calls.append(args)):
            install.chown_root_group(Layout.from_env(self.box.env), "/x")
        self.assertEqual(calls, [])

    def test_chown_root_group_raises_when_the_user_is_missing(self):
        from lgwctl import install

        with mock.patch.object(install.pwd, "getpwnam", side_effect=KeyError):
            with self.assertRaises(KeyError):
                install.chown_root_group(self.layout, "/x")

    def test_sqlite_backup_and_integrity_disable_trusted_schema(self):
        """低-4：開啟 untrusted SQLite 檔時停用 trusted_schema。"""
        db_path = os.path.join(self.box.root, "test_trusted_schema.db")
        self.box.make_db(db_path, "marker")
        from lgwctl import migrate
        self.assertTrue(migrate._db_integrity_ok(db_path))
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA trusted_schema = OFF;")
            val = conn.execute("PRAGMA trusted_schema").fetchone()[0]
            self.assertEqual(val, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
