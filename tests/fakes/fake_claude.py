#!/usr/bin/env python3
"""假的 claude（#60 測試用）：模擬原版 Claude Code 的最小行為；不連網。

FAKE_CLAUDE_MODE：
  ok        解析 --settings 的 statusLine 命令，把 FAKE_CLAUDE_PAYLOAD 當 stdin 餵給它
  silent    不執行 statusline、結束碼 0
  fail      不執行 statusline、結束碼 3
  hang      卡住（sleep 60），並留一個孫行程
  noisy     在 stdout／stderr 印 FAKE_CLAUDE_NOISE 後照 ok 行為
FAKE_CLAUDE_DUMP：把 argv／cwd／環境鍵 dump 到這個檔（JSON）；token 值也寫進去供測試核對「只在環境」。
"""
import json
import os
import subprocess
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
dump = os.environ.get("FAKE_CLAUDE_DUMP")
if dump:
    with open(dump, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
                             "pid": os.getpid(), "pgid": os.getpgid(0)}) + "\n")
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    pidfile = os.environ.get("FAKE_CLAUDE_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as fh:
            fh.write(str(child.pid))
    time.sleep(60)
    sys.exit(0)
if mode == "fail":
    sys.exit(3)
if mode == "silent":
    sys.exit(0)
if mode == "noisy":
    noise = os.environ.get("FAKE_CLAUDE_NOISE", "")
    sys.stdout.write(noise + "\n")
    sys.stderr.write(noise + "\n")
args = sys.argv[1:]
settings = json.loads(args[args.index("--settings") + 1])
subprocess.run(settings["statusLine"]["command"], shell=True, check=False,
               input=os.environ.get("FAKE_CLAUDE_PAYLOAD", "{}").encode())
