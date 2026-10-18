"""install：第一次安裝（冪等；缺本人動作的項目列 todo，不算失敗）。

步驟（#35 spec）：建帳號 llmgw（已存在略過）→ 目錄與權限 → 取 tag 到
``<prefix>/opt/llmgateway/<tag>`` → 切 current → 放 unit → 設定範本（不覆蓋已
存在的）→ ``daemon-reload`` → enable＋start 兩個 unit → 缺本人動作的項目
（gh 登入、keys.pass 的值、隧道金鑰與 known_hosts）列 todo。

已裝過再跑要安全：既有的設定檔、keys.pass、keys.env 一律不動；服務已在跑就
不重啟；取碼已完整就沿用。
"""

from __future__ import annotations

import os
import pwd
import tempfile

from . import release, units
from . import run as runmod
from .layout import (
    DEFAULT_API_PORT,
    ENV_FILE_NAME,
    KEYS_ENV_NAME,
    KEYS_PASS_NAME,
    SERVICE_NAME,
    TAG_PATTERN,
    TAG_RE,
    TUNNEL_ENV_NAME,
    TUNNEL_SERVICE_NAME,
    UNIT_NAMES,
)
from .report import Report

WRAPPER_NAME = "llmgwctl"
KEYS_PASS_VAR = "LLMGW_KEYS_PASSWORD"
TUNNEL_KEY_NAME = "id_llmgw_tunnel"
TUNNEL_KNOWN_HOSTS_NAME = "known_hosts_rendezvous"

LLMGATEWAY_ENV_TEMPLATE = (
    "# LLMGateway API 埠（lgwd 只綁 127.0.0.1；改完 systemctl restart "
    + SERVICE_NAME + "）。\n"
    'LGW_API_PORT="{port}"\n'
)

TUNNEL_ENV_TEMPLATE = (
    "# LLMGateway 隧道守護設定（system 層 " + TUNNEL_SERVICE_NAME + " 讀這份）。\n"
    "# RENDEZVOUS 必填；IDENTITY／KNOWN_HOSTS 寫絕對路徑，金鑰維持在 {home}/.ssh。\n"
    "RENDEZVOUS=\n"
    "IDENTITY={identity}\n"
    "KNOWN_HOSTS={known_hosts}\n"
    "REMOTE_API=4710\n"
    "REMOTE_SSH=4711\n"
    "LOCAL_API={port}\n"
    "PROBE_INTERVAL=60\n"
    "PROBE_FAILS=3\n"
)

# keys.pass 先放空殼（只有註解）：LoadCredential 的來源檔不在，服務會起不來；
# 值只有本人知道，install 不猜也不寫，只列 todo。
KEYS_PASS_TEMPLATE = (
    "# 放單行 " + KEYS_PASS_VAR + "=…（值只有本人知道；不要加引號）。\n"
    "# 只給 systemd LoadCredential= 交給服務（服務裡是 "
    "$CREDENTIALS_DIRECTORY/keys.pass；權限 0640 root:llmgw）。\n"
)

WRAPPER_TEMPLATE = (
    "#!/bin/sh\n"
    "# 由 llmgwctl install 產生：切進當版目錄跑 python3 -m lgwctl。\n"
    "cd {current} || exit 1\n"
    'exec python3 -m lgwctl "$@"\n'
)


def _write_text(path: str, text: str, mode: int) -> None:
    """同目錄暫存 → chmod → rename（原子）。"""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _chown(layout, path: str) -> None:
    """把檔案／目錄給服務帳號；不是 root 或 prefix 模式就跳過（測試沙箱）。"""
    if layout.prefix or layout.euid != 0:
        return
    info = pwd.getpwnam(layout.service_user)
    os.chown(path, info.pw_uid, info.pw_gid)


def _chmod_quiet(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _step_root(layout, report) -> bool:
    """install 要 root（prefix 模式除外）：與 upgrade／rollback 共用同一個判準。"""
    return runmod.require_root(layout, report)


def _step_account(layout, report) -> bool:
    try:
        pwd.getpwnam(layout.service_user)
    except KeyError:
        pass
    else:
        report.ok("account", f"{layout.service_user} 已存在")
        return True
    try:
        proc = runmod.run([layout.useradd, "-m", "-s", "/bin/bash", layout.service_user],
                          env=layout.child_env())
    except runmod.RunError as exc:
        report.fail("account", f"跑不起來 {layout.useradd}（{exc.kind}）")
        return False
    if proc.returncode != 0:
        report.fail("account", f"useradd rc={proc.returncode}")
        return False
    report.ok("account", f"{layout.service_user} 已建立")
    return True


def _step_dirs(layout, report) -> bool:
    ssh_dir = os.path.join(layout.service_home, ".ssh")
    dirs = (layout.opt_dir, layout.etc_dir, layout.state_dir, layout.unit_dir,
            layout.bin_dir, layout.service_home, ssh_dir)
    try:
        for path in dirs:
            os.makedirs(path, exist_ok=True)
        # 安全相關的兩個目錄權限一律校正（其他沿用系統預設／既有設定）。
        for path in (layout.state_dir, ssh_dir):
            os.chmod(path, 0o700)
            _chown(layout, path)
        # current 的父目錄要讓服務帳號進得去（umask 很嚴的機器也一樣）；
        # 版本目錄本身由 release.normalize_tree 處理。
        os.chmod(layout.opt_dir, 0o755)
    except OSError as exc:
        report.fail("dirs", f"目錄或權限沒弄好（{type(exc).__name__}）")
        return False
    report.ok("dirs", "目錄與權限")
    return True


def _step_config(layout, report) -> bool:
    created: list[str] = []
    kept: list[str] = []
    templates = (
        (layout.env_file, ENV_FILE_NAME,
         LLMGATEWAY_ENV_TEMPLATE.format(port=DEFAULT_API_PORT), 0o644),
        (layout.tunnel_env_file, TUNNEL_ENV_NAME,
         TUNNEL_ENV_TEMPLATE.format(
             home=layout.service_home,
             identity=os.path.join(layout.service_home, ".ssh", TUNNEL_KEY_NAME),
             known_hosts=os.path.join(layout.service_home, ".ssh", TUNNEL_KNOWN_HOSTS_NAME),
             port=DEFAULT_API_PORT), 0o644),
        (layout.keys_pass_file, KEYS_PASS_NAME, KEYS_PASS_TEMPLATE, 0o640),
        (layout.keys_env, KEYS_ENV_NAME, "", 0o600),
    )
    try:
        for path, name, text, mode in templates:
            if os.path.exists(path):
                kept.append(name)
                continue
            _write_text(path, text, mode)
            created.append(name)
    except OSError as exc:
        report.fail("config", f"寫設定檔失敗（{type(exc).__name__}）")
        return False
    # 權限：既有的也校正（能改就改；不是自己的檔就跳過）。
    _chmod_quiet(layout.keys_pass_file, 0o640)
    _chmod_quiet(layout.keys_env, 0o600)
    try:
        _chown(layout, layout.keys_pass_file)
        _chown(layout, layout.keys_env)
    except OSError as exc:
        report.fail("config", f"chown 設定檔失敗（{type(exc).__name__}）")
        return False
    if created and kept:
        detail = "建立 " + "、".join(created) + "；保留 " + "、".join(kept)
    elif created:
        detail = "建立 " + "、".join(created)
    else:
        detail = "都已經在了（不覆蓋）"
    report.ok("config", detail)
    return True


def _step_wrapper(layout, report) -> bool:
    path = os.path.join(layout.bin_dir, WRAPPER_NAME)
    try:
        _write_text(path, WRAPPER_TEMPLATE.format(current=layout.current_link), 0o755)
    except OSError as exc:
        report.fail("wrapper", f"寫 {path} 失敗（{type(exc).__name__}）")
        return False
    report.ok("wrapper", path)
    return True


def _step_daemon_reload(layout, report) -> bool:
    if not units.daemon_reload(layout):
        report.fail("daemon-reload", f"{layout.systemctl} daemon-reload 失敗")
        return False
    report.ok("daemon-reload")
    return True


def _step_enable(layout, report) -> bool:
    failed = [name for name in UNIT_NAMES if not units.enable(layout, name)]
    if failed:
        report.fail("enable", "、".join(failed) + " enable 失敗")
        return False
    report.ok("enable", "、".join(UNIT_NAMES))
    return True


def _step_service(layout, report) -> bool:
    if units.is_active(layout, SERVICE_NAME):
        report.ok("start", f"{SERVICE_NAME} 已在跑（不動它）")
        return True
    if not units.start(layout, SERVICE_NAME):
        report.fail("start",
                    f"{SERVICE_NAME} 起不來（{layout.journalctl} -u {SERVICE_NAME}）")
        return False
    report.ok("start", SERVICE_NAME)
    return True


def _step_tunnel(layout, report, ready: bool, missing: str) -> None:
    """隧道單位的 start：設定沒齊就只列 todo（本人動作），不當失敗。"""
    if not ready:
        report.todo("tunnel-key",
                    f"補齊 tunnel.env（{missing}）與 {layout.service_home}/.ssh 的金鑰")
        return
    report.ok("tunnel-config", "tunnel.env 已就緒")
    if units.is_active(layout, TUNNEL_SERVICE_NAME):
        report.ok("tunnel-run", "已在跑（不動它）")
        return
    if units.start(layout, TUNNEL_SERVICE_NAME):
        report.ok("tunnel-run", TUNNEL_SERVICE_NAME)
    else:
        report.todo("tunnel-run",
                    f"start 未成功（{layout.journalctl} -u {TUNNEL_SERVICE_NAME}；會自己重試）")


def _keys_pass_filled(layout) -> bool:
    """keys.pass 裡有沒有非空的 ``LLMGW_KEYS_PASSWORD=``（只看得有沒有，不讀值）。"""
    try:
        with open(layout.keys_pass_file, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == KEYS_PASS_VAR and value.strip():
            return True
    return False


def _gh_logged_in(layout) -> bool | None:
    """以服務帳號身分問 gh 有沒有登入；無法確認（找不到 gh／sudo 失敗）回 None。"""
    argv = runmod.service_user_argv(layout, [layout.gh, "auth", "status"])
    try:
        proc = runmod.run(argv, env=layout.child_env())
    except runmod.RunError:
        return None
    if proc.returncode == 0:
        return True
    if runmod.needs_sudo(layout) and runmod.sudo_failed(proc.stderr or ""):
        return None
    return False


def _step_todos(layout, report) -> None:
    if _keys_pass_filled(layout):
        report.ok("keys-pass", f"已有非空的 {KEYS_PASS_VAR}")
    else:
        report.todo("keys-pass",
                    f"把 {KEYS_PASS_VAR} 填進 {layout.keys_pass_file}，"
                    f"再 systemctl restart {SERVICE_NAME}")
    logged_in = _gh_logged_in(layout)
    if logged_in is True:
        report.ok("gh-login", f"{layout.service_user} 的 gh 已登入")
    elif logged_in is False:
        report.todo("gh-login", f"以 {layout.service_user} 身分 gh auth login")
    else:
        report.todo("gh-login", f"無法確認（找不到 {layout.gh} 或 sudo 失敗）")


def run(layout, *, tag: str, repo: str | None = None, report: Report | None = None) -> int:
    report = report or Report()
    if not _step_root(layout, report):
        return report.exit_code(todo_ok=True)
    if not TAG_RE.match(tag or ""):
        report.fail("tag", f"格式不符（要 {TAG_PATTERN}）：{tag}")
        return report.exit_code(todo_ok=True)

    if not _step_account(layout, report):
        return report.exit_code(todo_ok=True)
    if not _step_dirs(layout, report):
        return report.exit_code(todo_ok=True)
    tag_dir = release.fetch(layout, tag, repo, report)
    if tag_dir is None:
        return report.exit_code(todo_ok=True)
    try:
        old_tag = release.switch_current(layout, tag)
    except OSError as exc:
        report.fail("current", f"切 current 失敗（{type(exc).__name__}）")
        return report.exit_code(todo_ok=True)
    if old_tag and old_tag != tag:
        report.ok("current", f"{old_tag} → {tag}")
    else:
        report.ok("current", tag)
    if not units.install_unit_files(layout, tag_dir, report):
        return report.exit_code(todo_ok=True)
    if not _step_config(layout, report):
        return report.exit_code(todo_ok=True)
    if not _step_wrapper(layout, report):
        return report.exit_code(todo_ok=True)
    if not _step_daemon_reload(layout, report):
        return report.exit_code(todo_ok=True)
    if not _step_enable(layout, report):
        return report.exit_code(todo_ok=True)
    if not _step_service(layout, report):
        return report.exit_code(todo_ok=True)
    ready, missing = layout.tunnel_config_state()
    _step_tunnel(layout, report, ready, missing)
    _step_todos(layout, report)
    removed = release.cleanup_old_versions(layout)
    report.ok("cleanup", "、".join(removed) + " 已移除" if removed else "沒有多餘版本")
    return report.exit_code(todo_ok=True)
