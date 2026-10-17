"""sync：手動觸發一次 key 拉取（等同跑當版的 ``ops-scripts/key-sync``）。

密碼不經 argv、不經 log：以服務帳號身分用 ``/bin/sh -c`` 先 ``. /etc/llmgateway/
keys.pass``（0640 root:llmgw，服務帳號讀得到）再 ``exec`` key-sync，讓
``LLMGW_KEYS_PASSWORD`` 只活在那個子行程的環境裡。key-sync 的輸出（不含值）
照原樣轉印；結束碼＝key-sync 的結束碼。
"""

from __future__ import annotations

import os
import sys

from . import run as runmod
from .report import Report

# $0＝sh、$1＝keys.pass、$2＝key-sync、$3＝keys.env、$4＝當版目錄。
SHELL_SCRIPT = 'set -a; . "$1" || exit 3; exec "$2" --keys "$3" --code-dir "$4"'


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
            layout.keys_pass_file, script, layout.keys_env, layout.current_link]
    argv = runmod.service_user_argv(layout, argv)
    try:
        proc = runmod.run(argv, env=layout.child_env(HOME=layout.service_home),
                          timeout=runmod.SYNC_TIMEOUT)
    except runmod.RunError as exc:
        report.fail("sync", f"跑不起來（{exc.kind}）")
        return 1
    if proc.stdout:
        out.write(proc.stdout)
    if proc.stderr:
        err.write(proc.stderr)
    if proc.returncode == 0:
        report.ok("sync", "key 拉取完成（值不經這裡）")
    else:
        report.fail("sync", f"key-sync rc={proc.returncode}（看上面的訊息）")
    return proc.returncode
