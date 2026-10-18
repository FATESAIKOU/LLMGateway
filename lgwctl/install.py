"""install：第一次安裝（冪等；缺本人動作的項目列 todo，不算失敗）。

步驟（#35 spec）：建帳號 llmgw（已存在略過）→ 目錄與權限 → 取 tag 到
``<prefix>/opt/llmgateway/<tag>`` → 切 current → 放 unit → 設定範本（不覆蓋已
存在的）→ ``daemon-reload`` → enable＋start 兩個 unit → 缺本人動作的項目
（gh 登入、keys.pass 的值、隧道金鑰與 known_hosts）列 todo。

已裝過再跑要安全：既有的設定檔、keys.pass、keys.env 一律不動；服務已在跑就
不重啟；取碼已完整就沿用。**current 已存在且不是同一個 tag 就直接拒絕**
（換版請用 upgrade；install 偷偷換 tag 會出現「跑的是舊碼、status 說新版」的
假象，中-3）。
"""

from __future__ import annotations

import errno
import grp
import os
import pwd
import stat

from . import atomic, probe, release, units
from . import run as runmod
from .layout import (
    DEFAULT_API_PORT,
    ENV_FILE_NAME,
    KEYS_ENV_NAME,
    KEYS_PASS_NAME,
    OLD_USER_UNIT_NAMES,
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
# start 之後用本機 list 確認服務真的起來的上限秒數；本人的 LLMGW_LIST_WAIT 更小
# 就用他的值（測試沙箱設 0＝只試一次）。
INSTALL_WAIT_CAP = 10.0

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


def _write_one(layout, path: str, name: str, text: str, mode: int,
               created: list[str], kept: list[str]) -> None:
    """設定範本：已在的一律不動（不覆蓋），沒有才寫。"""
    if os.path.exists(path):
        kept.append(name)
        return
    atomic.write_text(path, text, mode)
    created.append(name)


def _chown_service(layout, path: str) -> None:
    """目錄／keys.env 給服務帳號（llmgw:llmgw）；不是 root 或 prefix 模式就跳過。
    高-1：以 O_NOFOLLOW 開啟 fd 再 fchown，拒絕跟隨 symlink。
    """
    if layout.prefix or layout.euid != 0:
        return
    if os.path.islink(path):
        raise OSError(f"{path} 是 symlink；拒絕以 root 變更擁有者")
    info = pwd.getpwnam(layout.service_user)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if os.path.isdir(path):
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path) or getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise
        os.chown(path, info.pw_uid, info.pw_gid)
        return
    try:
        os.fchown(fd, info.pw_uid, info.pw_gid)
    finally:
        os.close(fd)


def chown_root_group(layout, path: str) -> None:
    """``/etc`` 的設定檔與 ``keys.pass``：root:<服務帳號群組>（中-7）。

    install 與 migrate 共用同一個規則：檔案由 root 擁有（服務帳號不能改自己
    的 credential／設定），群組是服務帳號的群組（``User=llmgw`` 的 unit 讀得
    到）。不是 root 或 prefix 模式就跳過；服務帳號不在時丟 KeyError，由呼叫
    方決定 fail（不要靜默變成 root:root）。高-1：拒絕跟隨 symlink。
    """
    if layout.prefix or layout.euid != 0:
        return
    if os.path.islink(path):
        raise OSError(f"{path} 是 symlink；拒絕以 root 變更擁有者")
    gid = pwd.getpwnam(layout.service_user).pw_gid
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path) or getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise
        os.chown(path, 0, gid)
        return
    try:
        os.fchown(fd, 0, gid)
    finally:
        os.close(fd)


def _chown_keys_pass(layout) -> None:
    """``keys.pass`` 設成 root:<llmgw 群組> 0640（中-4）。

    解密密碼檔不能由服務帳號擁有：服務只能讀、不能改自己的 credential
    （defense-in-depth），這也是規格固定值。重跑 install 也不會把它改成
    llmgw 擁有（每次都是設 root:<gid>）。
    """
    chown_root_group(layout, layout.keys_pass_file)


def _chmod_quiet(path: str, mode: int) -> None:
    try:
        if os.path.islink(path):
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
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
        return _step_group(layout, report)
    try:
        # -U：一定要有同名群組（unit 的 Group=llmgw 要用；不靠發行版預設，
        # USERGROUPS_ENAB=no 的機器沒建群組服務會起不來，低-9）。
        proc = runmod.run([layout.useradd, "-m", "-U", "-s", "/bin/bash", layout.service_user],
                          env=layout.child_env())
    except runmod.RunError as exc:
        report.fail("account", f"跑不起來 {layout.useradd}（{exc.kind}）")
        return False
    if proc.returncode != 0:
        report.fail("account", f"useradd rc={proc.returncode}")
        return False
    report.ok("account", f"{layout.service_user} 已建立")
    return _step_group(layout, report)


def _group_exists(layout) -> bool:
    """同名群組在不在（unit 的 ``Group=llmgw`` 要用）。

    ``LLMGW_PREFIX`` 沙箱裡帳號是假的、沒有真的群組，這裡直接說有
    （測試要驗這條路徑就覆寫這個函式）。
    """
    if layout.prefix:
        return True
    try:
        grp.getgrnam(layout.service_user)
    except KeyError:
        return False
    return True


def _step_group(layout, report) -> bool:
    if not _group_exists(layout):
        report.fail("account",
                    f"群組 {layout.service_user} 不在（unit 的 Group= 需要；"
                    f"useradd -U 沒建出來）")
        return False
    return True


def _step_dirs(layout, report) -> bool:
    ssh_dir = os.path.join(layout.service_home, ".ssh")
    dirs = (layout.opt_dir, layout.etc_dir, layout.state_dir, layout.unit_dir,
            layout.bin_dir, layout.service_home, ssh_dir)
    # 高-1: 先拒絕 symlink 目錄，防止提權
    for path in dirs:
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                report.fail("dirs", f"{path} 是 symlink；拒絕以 root 變更權限")
                return False
        except OSError:
            pass
    try:
        for path in dirs:
            os.makedirs(path, exist_ok=True)
        # 安全相關的兩個目錄權限一律校正（O_DIRECTORY|O_NOFOLLOW ＋ fchmod）。
        for path in (layout.state_dir, ssh_dir):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
            _chown_service(layout, path)
        # current 的父目錄要讓服務帳號進得去
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(layout.opt_dir, flags)
        try:
            os.fchmod(fd, 0o755)
        finally:
            os.close(fd)
        fd = os.open(layout.etc_dir, flags)
        try:
            os.fchmod(fd, 0o755)
        finally:
            os.close(fd)
    except OSError as exc:
        report.fail("dirs", f"目錄或權限沒弄好（{type(exc).__name__}）")
        return False
    report.ok("dirs", "目錄與權限")
    return True


def _step_config(layout, report) -> bool:
    created: list[str] = []
    kept: list[str] = []
    try:
        # 先弄 llmgateway.env：tunnel.env 的 LOCAL_API 要跟它的 LGW_API_PORT
        # 一致（低-1；已存在的 llmgateway.env 也照它算，不動它）。
        _write_one(layout, layout.env_file, ENV_FILE_NAME,
                   LLMGATEWAY_ENV_TEMPLATE.format(port=DEFAULT_API_PORT),
                   0o644, created, kept)
        port = layout.api_port()
        identity = os.path.join(layout.service_home, ".ssh", TUNNEL_KEY_NAME)
        known_hosts = os.path.join(layout.service_home, ".ssh", TUNNEL_KNOWN_HOSTS_NAME)
        _write_one(layout, layout.tunnel_env_file, TUNNEL_ENV_NAME,
                   TUNNEL_ENV_TEMPLATE.format(
                       home=layout.service_home, identity=identity,
                       known_hosts=known_hosts, port=port),
                   0o644, created, kept)
        _write_one(layout, layout.keys_pass_file, KEYS_PASS_NAME, KEYS_PASS_TEMPLATE,
                   0o640, created, kept)
        _write_one(layout, layout.keys_env, KEYS_ENV_NAME, "", 0o600, created, kept)
    except OSError as exc:
        report.fail("config", f"寫設定檔失敗（{type(exc).__name__}）")
        return False
    # 權限：既有的也校正（能改就改；不是自己的檔就跳過）。keys.pass 是
    # root:<llmgw gid>（中-4），keys.env 才是 llmgw:llmgw。
    _chmod_quiet(layout.keys_pass_file, 0o640)
    _chmod_quiet(layout.keys_env, 0o600)
    try:
        _chown_keys_pass(layout)
        _chown_service(layout, layout.keys_env)
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
        atomic.write_text(path, WRAPPER_TEMPLATE.format(current=layout.current_link), 0o755)
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
        # 低-11：LoadCredential 是啟動時的快照；keys.pass 填／改完要 restart
        # 才會生效（不然 key 拉取一直用沒有密碼的舊快照）。
        if _keys_pass_filled(layout):
            report.todo("restart",
                        f"keys.pass 是啟動時的快照：填／改完要 "
                        f"systemctl restart {SERVICE_NAME}（或 llmgwctl sync）")
        return True
    if not units.start(layout, SERVICE_NAME):
        report.fail("start",
                    f"{SERVICE_NAME} 起不來（{layout.journalctl} -u {SERVICE_NAME}）")
        return False
    # systemctl start 回 0 只代表啟動被接受（Type=exec 也只保到 exec 成功）；
    # app 層壞掉（policy 壞、keys.env 壞、埠展開成空）會 crash-loop，
    # 再用本機 list 確認一次，不然會「印 ok start、其實沒起來」（中-7）。
    if not probe.wait_local_ok(layout, min(layout.list_wait, INSTALL_WAIT_CAP)):
        report.fail("start",
                    f"{SERVICE_NAME} 起了但本機 list 不是 200"
                    f"（{layout.journalctl} -u {SERVICE_NAME}）")
        return False
    # 中-10 (3)：本機 list 也可能是**舊 user 版**在 8480 回答（它比新服務先起）；
    # 用 unit 自己的 is-active 確認真的是新 unit 在跑。
    if not units.is_active(layout, SERVICE_NAME):
        report.fail("start",
                    f"{SERVICE_NAME} 的 is-active 不是 active（crash-loop？"
                    f"{layout.journalctl} -u {SERVICE_NAME}）")
        return False
    report.ok("start", f"{SERVICE_NAME}（本機 list 200）")
    return True


def _step_refuse_migration(layout, report) -> bool:
    """中-10：舊 user 版還在跑時拒絕 install——那台機器要用 prepare-migration。

    舊 user ``lgwd`` 佔著 8480：新服務 start 會 bind 失敗（或 crash-loop），
    本機 list 的 200 是**舊服務**在回答（install 假成功）；而且新服務先建了
    空的 ``llm.db``，之後 prepare 看到 db 已存在就 skip，舊 audit／cooldown
    不會被複製（資料遺失）。四個舊 unit 任一 active 就擋。
    """
    running = [unit for unit in OLD_USER_UNIT_NAMES
               if units.user_unit_state(layout, unit) == "active"]
    if running:
        report.fail("old-user-units",
                    "舊（user 版）unit 還在跑：" + "、".join(running) +
                    "；這是遷移不是安裝——請用 llmgwctl prepare-migration "
                    "＋llmgwctl cutover（install 會與舊服務搶 8480 埠）")
        return False
    return True


def _step_tunnel(layout, report, ready: bool, missing: str) -> None:
    """隧道單位的 start：設定沒齊就只列 todo（本人動作），不當失敗。"""
    if not ready:
        report.todo("tunnel-key",
                    f"補齊 tunnel.env（{missing}）與 {layout.service_home}/.ssh 的金鑰，"
                    f"填好後跑 systemctl start {TUNNEL_SERVICE_NAME}（或重跑 install）")
        return
    report.ok("tunnel-config", "tunnel.env 已就緒")
    if units.is_active(layout, TUNNEL_SERVICE_NAME):
        report.ok("tunnel-run", "已在跑（不動它）")
        return
    # start 之後再看一次 is-active：start 的結束碼不等於「連上了」（中-7）。
    if units.start(layout, TUNNEL_SERVICE_NAME) \
            and units.is_active(layout, TUNNEL_SERVICE_NAME):
        report.ok("tunnel-run", TUNNEL_SERVICE_NAME)
    else:
        report.todo("tunnel-run",
                    f"start 沒確認成功（{layout.journalctl} -u {TUNNEL_SERVICE_NAME}；"
                    f"會自己重試）")


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
                    f"把 {KEYS_PASS_VAR} 填進 {layout.keys_pass_file}"
                    f"（單行、root:llmgw 0640），再 systemctl restart {SERVICE_NAME}")
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
    # install 只做「第一次裝」與「同版重裝」；換版請用 upgrade（install 換 tag
    # 會把 current 切走卻不重啟：status 說新版、跑的是舊碼，中-3）。
    installed = layout.current_tag()
    if installed is not None and installed != tag:
        report.fail("tag", f"已經裝了 {installed}；換版請用 upgrade")
        return report.exit_code(todo_ok=True)

    # 中-10：舊 user 版還在跑就不要碰（這台是遷移，不是安裝）。
    if not _step_refuse_migration(layout, report):
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
