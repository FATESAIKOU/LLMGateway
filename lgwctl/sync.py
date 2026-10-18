"""sync：手動觸發一次 key 拉取（等同跑當版的 ``ops-scripts/key-sync``）。

密碼不經 argv、不經 log：以服務帳號身分用 ``/bin/sh -c`` 先 ``. /etc/llmgateway/
keys.pass``（0640 root:llmgw，服務帳號讀得到）再 ``exec`` key-sync，讓
``LLMGW_KEYS_PASSWORD`` 只活在那個子行程的環境裡。key-sync 的輸出（不含值）
照原樣轉印；結束碼＝key-sync 的結束碼。

key-sync 本身只寫 keys.env，不寫 T2 的 ``.keysync.status``；這裡在它跑完
（成功或失敗）後比照 T2 格式補寫狀態檔（只有 ``ts``／``ok``／``reason``，
不含任何值），``check`` 才看得到手動拉取的結果（中-5）。寫完要提醒
``systemctl restart``：``LoadCredential`` 是啟動時的快照，沒 restart 服務
仍用舊的。
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import atomic, run as runmod
from .layout import ENV_KEYS_PASSWORD, SERVICE_NAME
from .report import Report

# $0＝sh、$1＝keys.pass、$2＝key-sync、$3＝keys.env、$4＝當版目錄、$5＝gh。
SHELL_SCRIPT = ('set -a; . "$1" || exit 3; '
                'exec "$2" --keys "$3" --code-dir "$4" --gh "$5"')


def _write_status(layout, *, ok: bool, reason: str) -> str | None:
    """用 T2 的格式寫 ``.keysync.status``（0644、原子寫）；出錯回錯誤字串。

    內容只有 ``ts``／``ok``／``reason``：絕不含 key 值或密碼。狀態檔寫不好
    不改 key-sync 的結束碼（key 本身已經更新了）。
    """
    payload = json.dumps({"ts": time.time(), "ok": ok, "reason": reason},
                         ensure_ascii=False)
    try:
        atomic.write_text(layout.keysync_status_file, payload, 0o644)
    except OSError as exc:
        return f"寫 {layout.keysync_status_file} 失敗（{type(exc).__name__}）"
    return None


def run(layout, *, report: Report | None = None, out=None, err=None) -> int:
    report = report or Report()
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if not runmod.require_root(layout, report):
        return report.exit_code(todo_ok=True)
    script = layout.keysync_script
    if not os.path.isfile(script):
        report.fail("sync", f"找不到 {script}")
        return 1
    if not os.path.isfile(layout.keys_pass_file):
        report.fail("sync", f"還沒放 {layout.keys_pass_file}")
        return 1

    argv = [layout.sh, "-c", SHELL_SCRIPT, "sh",
            layout.keys_pass_file, script, layout.keys_env, layout.current_link, layout.gh]
    argv = runmod.service_user_argv(layout, argv)
    # 密碼預設不放行給任何子行程（child_env 一律先移除，低-4）；sync 是唯一
    # 指名放行的地方：操作者 shell export 的同名變數只給這一個子 shell，而它
    # 真正用的值是自己 source keys.pass 出來的。
    child_env = layout.child_env(
        HOME=layout.service_home,
        **{ENV_KEYS_PASSWORD: layout.env.get(ENV_KEYS_PASSWORD)})
    try:
        proc = runmod.run(argv, env=child_env, timeout=runmod.SYNC_TIMEOUT)
    except runmod.RunError as exc:
        report.fail("sync", f"跑不起來（{exc.kind}）")
        return 1
    if proc.stdout:
        out.write(proc.stdout)
    if proc.stderr:
        err.write(proc.stderr)
    if proc.returncode == 0:
        problem = _write_status(layout, ok=True, reason="ok")
        report.ok("sync", "key 拉取完成（值不經這裡）")
        # LoadCredential 是服務啟動時的快照：要 restart 才會換（中-5）。
        report.todo("restart",
                    f"要 systemctl restart {SERVICE_NAME} 讓 LoadCredential 換新快照")
    else:
        problem = _write_status(layout, ok=False,
                                reason=f"manual_sync rc={proc.returncode}")
        report.fail("sync", f"key-sync rc={proc.returncode}（看上面的訊息）")
    if problem:
        report.fail("status", problem)
    # 低-12：狀態檔寫不進去也是失敗（automation 只看結束碼會以為 sync 完整成功；
    # check 之後仍看到舊狀態）。
    return proc.returncode if not problem else 1
