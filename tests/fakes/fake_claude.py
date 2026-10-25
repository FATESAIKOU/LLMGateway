#!/usr/bin/env python3
"""假的 claude（#60／#68 測試用）：模擬互動式（pty）原版 Claude Code 的最小行為；不連網。

像真機：啟動時呼叫一次 statusline（帶 "{}"，沒有 rate_limits），印畫面、讀 stdin 到 "ok"，再依情境
呼叫 --settings 裡的 statusline 命令，**回完不會自己退出**，只在收到 "/exit" 時結束。
FAKE_CLAUDE_MODE：
  ok        送出後 statusline 帶 FAKE_CLAUDE_PAYLOAD
  norate    送出後 statusline 帶已有 API 回應、但沒有 rate_limits 的 JSON
  slowapi   送出後先呼叫一次「還沒有 API 回應」的 statusline，隔 FAKE_CLAUDE_DELAY 秒才帶 FAKE_CLAUDE_PAYLOAD
  silent    完全不執行 statusline
  fail      讀完輸入後結束碼 3，不執行 statusline
  hang      收到輸入後卡住（sleep 60），並留一個孫行程
  noisy     畫面與 stderr 印 FAKE_CLAUDE_NOISE 後照 ok 行為
  trust     停在 trust 對話框（不讀輸入、不結束）
  theme     停在主題選擇畫面（不讀輸入、不結束）
  loginerr  讀完輸入後印要求 /login 的錯誤畫面，不呼叫 statusline、不結束
FAKE_CLAUDE_EXIT（特例，預設 cmd）：
  cmd       收到 "/exit" 才結束
  self      statusline 呼叫完就自己結束
  ignore    忽略 /exit（sleep 60），用來驗證 fetcher 會終止行程群組
FAKE_CLAUDE_DUMP：把 argv／cwd／環境／tty 狀態 dump 到這個檔（JSON 一行）；token 值也寫進去供測試核對「只在環境」。
                  收到的輸入（含 "/exit"）另寫到 <dump>.input。
"""
import json
import os
import subprocess
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
dump = os.environ.get("FAKE_CLAUDE_DUMP")


def has_ctty():
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        return True
    except OSError:
        return False


if dump:
    with open(dump, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
                             "pid": os.getpid(), "pgid": os.getpgid(0),
                             "isatty": [os.isatty(fd) for fd in (0, 1, 2)],
                             "ctty": has_ctty()}) + "\n")


def say(text):
    sys.stdout.write(text + "\r\n")
    sys.stdout.flush()


if mode == "trust":
    say("Do you trust the files in this folder?")
    say("1. Yes, proceed   2. No, exit")
    time.sleep(60)
    sys.exit(0)
if mode == "theme":
    say("Welcome. Choose the text style that looks best with your terminal")
    time.sleep(60)
    sys.exit(0)

args = sys.argv[1:]
settings = json.loads(args[args.index("--settings") + 1])


def statusline(payload):
    subprocess.run(settings["statusLine"]["command"], shell=True, check=False, input=payload.encode())


if mode not in ("silent", "fail"):
    statusline("{}")                  # 啟動時的第一次呼叫：還沒有 API 回應，沒有 rate_limits
noise = os.environ.get("FAKE_CLAUDE_NOISE", "")
say("Claude Code (fake) " + noise)
if mode == "noisy":
    sys.stderr.write(noise + "\n")
say("> ")
line = sys.stdin.readline().strip()
if dump:
    with open(dump + ".input", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

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
if mode == "loginerr":
    say("API Error: 401 - Please run /login")
    time.sleep(60)
    sys.exit(0)
API_DONE = json.dumps({"cost": {"total_api_duration_ms": 1234}})   # 已有 API 回應
payload = os.environ.get("FAKE_CLAUDE_PAYLOAD", "{}")
if mode == "slowapi":
    statusline(json.dumps({"cost": {"total_api_duration_ms": 0}}))    # 送出時：還沒有 API 回應
    time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY", "1.5")))
    statusline(payload)
elif mode == "norate":
    statusline(API_DONE)
elif mode != "silent":
    statusline(payload)

exit_style = os.environ.get("FAKE_CLAUDE_EXIT", "cmd")
if exit_style == "self":
    sys.exit(0)
if exit_style == "ignore":
    time.sleep(60)
for line in sys.stdin:
    if dump:
        with open(dump + ".input", "a", encoding="utf-8") as fh:
            fh.write(line.strip() + "\n")
    if line.strip() == "/exit":
        sys.exit(0)
