"""取碼、切 current／previous、清舊版（install 與 upgrade 共用）。

- 取碼：``git clone --depth 1 --branch <tag>`` 到同層暫存目錄，驗必要檔案都在
  才寫 ``.lgw-tag`` 並原子搬進 ``<prefix>/opt/llmgateway/<tag>``；已裝完整就沿用。
- 切版：``current`` 指新 tag，舊的記到 ``previous``（原子換 symlink）。
- 清舊版：只留 current 與 previous 兩版，其他 tag 目錄刪掉。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from .layout import KEYSYNC_BIN_NAME, OPS_DIR_NAME, TAG_RE, TUNNEL_BIN_NAME
from .run import CLONE_TIMEOUT, RunError, run

# 一版取回來至少要有的檔案；缺一個就不算完整（不切 current）。
REQUIRED_FILES = (
    "policy/default.json",
    os.path.join("lgwd", "__main__.py"),
    os.path.join(OPS_DIR_NAME, KEYSYNC_BIN_NAME),
    os.path.join(OPS_DIR_NAME, TUNNEL_BIN_NAME),
    os.path.join("deploy", "units", "llmgateway.service"),
    os.path.join("deploy", "units", "llmgateway-tunnel.service"),
)
TAG_FILE_NAME = ".lgw-tag"


def missing_files(tag_dir: str) -> list[str]:
    return [rel for rel in REQUIRED_FILES if not os.path.isfile(os.path.join(tag_dir, rel))]


def installed_complete(layout, tag: str) -> bool:
    """這個 tag 已經裝好而且完整（有 .lgw-tag 且必要檔案都在）。"""
    tag_dir = layout.tag_dir(tag)
    tag_file = os.path.join(tag_dir, TAG_FILE_NAME)
    try:
        with open(tag_file, encoding="utf-8") as fh:
            if fh.read().strip() != tag:
                return False
    except OSError:
        return False
    return not missing_files(tag_dir)


def write_link(layout, link: str, target: str) -> None:
    """原子把 symlink 換成 target（同層暫存 symlink + rename）。"""
    directory = os.path.dirname(link) or "."
    tmp = os.path.join(directory, f".{os.path.basename(link)}.{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_tag_file(tag_dir: str, tag: str) -> None:
    with open(os.path.join(tag_dir, TAG_FILE_NAME), "w", encoding="utf-8") as fh:
        fh.write(tag + "\n")


def normalize_tree(layout, root: str) -> None:
    """版本目錄要讓服務帳號讀得到（Lima 演練抓到的 status=200/CHDIR）。

    ``git clone`` 進 ``tempfile.mkdtemp`` 的目錄是 0700（root 擁有），但服務是
    ``User=llmgw``、``WorkingDirectory=/opt/llmgateway/current``，進不去就起不來。
    這裡把整棵設成：目錄 0755、一般檔 0644、原本有執行位的維持 0755；有的話
    擁有者設 root:root（``LLMGW_PREFIX`` 測試模式不 chown，但照樣 chmod）。
    ``.git`` 直接移除（發佈目錄不需要；--depth 1 的它也幫不上什麼）。

    symlink 不跟進去、也不改它指到的東西（目前 repo 裡沒有）。
    """
    git_dir = os.path.join(root, ".git")
    if os.path.isdir(git_dir) and not os.path.islink(git_dir):
        shutil.rmtree(git_dir, ignore_errors=True)

    chown = (not layout.prefix) and layout.euid == 0
    for base, dirs, files in os.walk(root):
        os.chmod(base, 0o755)
        if chown:
            os.chown(base, 0, 0)
        dirs[:] = [name for name in dirs
                   if not os.path.islink(os.path.join(base, name))]
        for name in files:
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            mode = os.stat(path).st_mode
            os.chmod(path, 0o755 if mode & 0o111 else 0o644)
            if chown:
                os.chown(path, 0, 0)


def _move_into_place(tmp: str, tag_dir: str) -> None:
    """把驗過的暫存目錄搬進 tag_dir；舊的（不完整的）目錄先移開，失敗搬回去。"""
    stale = None
    if os.path.lexists(tag_dir):
        stale = f"{tag_dir}.stale.{os.getpid()}"
        if os.path.lexists(stale):
            shutil.rmtree(stale, ignore_errors=True)
        os.rename(tag_dir, stale)
    try:
        os.rename(tmp, tag_dir)
    except OSError:
        if stale is not None:
            try:
                os.rename(stale, tag_dir)
            except OSError:
                pass
        raise
    if stale is not None:
        shutil.rmtree(stale, ignore_errors=True)


def fetch(layout, tag: str, repo: str | None, report) -> str | None:
    """取一版程式；成功回 tag 目錄，失敗回 None（已經記 fail 行）。

    不論是新抓的還是沿用的，回傳前都會把目錄權限整理成服務讀得到（見
    ``normalize_tree``）：舊版（這個 bug 修好之前）留下來的 0700 目錄，重跑
    install／upgrade 也會被修好。
    """
    tag_dir = layout.tag_dir(tag)
    if installed_complete(layout, tag):
        try:
            normalize_tree(layout, tag_dir)
        except OSError as exc:
            report.fail("fetch", f"版本目錄權限修不好（{type(exc).__name__}）")
            return None
        report.ok("fetch", f"{tag} 已存在（沿用）")
        return tag_dir
    repo_url = repo or layout.repo
    try:
        os.makedirs(layout.opt_dir, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{tag}.", dir=layout.opt_dir)
    except OSError as exc:
        report.fail("fetch", f"建暫存目錄失敗（{type(exc).__name__}：{layout.opt_dir}）")
        return None
    try:
        proc = run([layout.git, "clone", "--depth", "1", "--branch", tag, repo_url, tmp],
                   env=layout.child_env(), timeout=CLONE_TIMEOUT)
    except RunError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        report.fail("fetch", f"取碼失敗（{layout.git} {exc.kind}：{tag}）")
        return None
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        report.fail("fetch", f"取碼失敗（git clone rc={proc.returncode}：{tag}）")
        return None
    missing = missing_files(tmp)
    if missing:
        shutil.rmtree(tmp, ignore_errors=True)
        report.fail("fetch", f"取到的版本缺 {missing[0]}（tag：{tag}）")
        return None
    try:
        write_tag_file(tmp, tag)
        normalize_tree(layout, tmp)
        _move_into_place(tmp, tag_dir)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        report.fail("fetch", f"就位失敗（{type(exc).__name__}）")
        return None
    report.ok("fetch", f"{tag} 已就位")
    return tag_dir


def switch_current(layout, tag: str) -> str | None:
    """把 current 切到 tag；舊版記到 previous。回舊 tag（本來沒有就 None）。"""
    old = layout.current_tag()
    if old == tag:
        return old
    if old is not None:
        write_link(layout, layout.previous_link, layout.tag_dir(old))
    write_link(layout, layout.current_link, layout.tag_dir(tag))
    return old


def restore_links(layout, current: str | None, previous: str | None) -> None:
    """把 current／previous 還原成指定的兩個 tag（None 表示移除 link）。

    升級失敗要切回舊版時用；兩個 link 的內容以「切換前」為準，不動別的東西。
    """
    for link, tag in ((layout.current_link, current), (layout.previous_link, previous)):
        if tag is None:
            try:
                os.unlink(link)
            except OSError:
                pass
            continue
        write_link(layout, link, layout.tag_dir(tag))


def cleanup_old_versions(layout) -> list[str]:
    """只留 current 與 previous 指著的版本；回被刪掉的版本名單。"""
    keep = {tag for tag in (layout.current_tag(), layout.previous_tag()) if tag}
    removed: list[str] = []
    try:
        entries = sorted(os.listdir(layout.opt_dir))
    except OSError:
        return removed
    for name in entries:
        if name.startswith(".") or name in keep:
            continue
        path = os.path.join(layout.opt_dir, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        if not (os.path.isfile(os.path.join(path, TAG_FILE_NAME)) or TAG_RE.match(name)):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed.append(name)
    return removed
