"""#320 V-0.5：外部埠負載客戶端（只當客戶端；不起 lgwd、不碰服務）。

沿用 C-612（`tests/test_main_auth_rate_263.py`）的做法：producer 用 `Signer.headers` 簽好
HTTP/1.0 原始請求放進佇列，consumer 每筆開一條新連線送出並讀完回應。差別只有：

- 連線被拒（`ConnectionRefusedError`，例如 V-0.5 期間服務被 stop）**單獨計**，不算狀態碼
  （新連線被拒不是 503）；
- 錯誤只記型別名與次數，不記例外內容、不記標頭（金鑰不會進輸出）；
- 收到 SIGTERM／SIGINT 時乾淨結束，並仍然印出結果 JSON。

用法：``python3 -m tests.v0_load_client --dir <準備目錄> --port <埠> --duration <秒> --concurrency N``
（`--dir` 與 `--port` 也可由 `LGW_V0_DIR`、`LGW_V0_PORT` 給。）結束時印一行 JSON 到 stdout。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import signal
import socket
import sys
import threading
import time

from tests.v0_prepare import ENV_DIR, ENV_PORT, V0_KEY_ID, load_signer

PRODUCERS = 4
DEFAULT_TARGET = "/v1/subscriptions"


def run(directory: str, port: int, duration: float, concurrency: int, target: str,
        host: str = "127.0.0.1") -> dict:
    signer = load_signer(directory)
    stop = threading.Event()
    lock = threading.Lock()
    statuses: collections.Counter = collections.Counter()
    errors: collections.Counter = collections.Counter()
    refused = 0
    q: queue.Queue = queue.Queue(maxsize=256)

    def fail(kind: str) -> None:
        with lock:
            errors[kind] += 1

    def producer() -> None:
        while not stop.is_set():
            try:
                headers = signer.headers(key_id=V0_KEY_ID, method="GET", target=target)
            except Exception as exc:  # 只記型別，不記內容（可能含路徑或金鑰片段）
                fail("sign:" + type(exc).__name__)
                return
            raw = ("GET " + target + " HTTP/1.0\r\n"
                   + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode("ascii")
            while not stop.is_set():
                try:
                    q.put(raw, timeout=0.1)
                    break
                except queue.Full:
                    pass

    def consumer() -> None:
        nonlocal refused
        while True:
            try:
                raw = q.get(timeout=0.1)
            except queue.Empty:
                if stop.is_set():
                    return
                continue
            try:
                with socket.create_connection((host, port), timeout=10) as sock:
                    sock.sendall(raw)
                    data = b""
                    while True:
                        part = sock.recv(65536)
                        if not part:
                            break
                        data += part
                try:
                    code = int(data.split(b" ", 2)[1])
                except (IndexError, ValueError):
                    fail("bad_status_line")
                    continue
                with lock:
                    statuses[code] += 1
            except ConnectionRefusedError:
                with lock:
                    refused += 1
            except OSError as exc:
                fail(type(exc).__name__)

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    # 讓呼叫端知道 SIGTERM 處理器已裝好（測試等這一行再送訊號）。
    print("v0_load_client: ready", file=sys.stderr, flush=True)
    threads = [threading.Thread(target=producer, daemon=True) for _ in range(PRODUCERS)]
    threads += [threading.Thread(target=consumer, daemon=True) for _ in range(concurrency)]
    start = time.monotonic()
    for t in threads:
        t.start()
    stop.wait(duration)
    stop.set()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - start
    if any(t.is_alive() for t in threads):
        fail("thread_stuck")

    total_ok = statuses[200]
    return {
        "elapsed_s": round(elapsed, 3),
        "requests": sum(statuses.values()) + refused + sum(errors.values()),
        "statuses": {str(code): n for code, n in sorted(statuses.items())},
        "errors": dict(sorted(errors.items())),
        "connection_refused": refused,
        "rate": round(total_ok / elapsed, 2) if elapsed > 0 else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="v0_load_client")
    parser.add_argument("--dir", default=os.environ.get(ENV_DIR))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    port = args.port if args.port is not None else os.environ.get(ENV_PORT)
    if not args.dir or port is None:
        print(f"v0_load_client: need --dir/--port (or {ENV_DIR}/{ENV_PORT})", file=sys.stderr)
        return 2
    if args.concurrency < 1 or args.duration <= 0 or not args.target.startswith("/"):
        print("v0_load_client: bad --concurrency, --duration or --target", file=sys.stderr)
        return 2
    try:
        result = run(args.dir, int(port), args.duration, args.concurrency, args.target)
    except (OSError, ValueError) as exc:
        print(f"v0_load_client: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
