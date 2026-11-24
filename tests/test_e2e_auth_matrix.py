"""D6a：三種模式 × 呼叫方 × 五條業務路由＋healthz 的離線整合驗收（#222）。

契約來源：`/home/worker/work/LLMGateway-team/REPORT-D6-arch.md` §2.1、§2.2、§4.1 的
C-950～C-955、§4.2 的 W-6d、§4.3 的變異 1／2／3／7。共用環境在
`tests/e2e_auth.py`（真的 `python3 -m lgwd` 子行程、真的 `ssh-keygen` 與 `prlimit`、
由真的 `llmgwctl auth-store init`／`auth-registry install` 建的兩個庫與名冊）。

每一支測試都用自己的獨立暫存版面（C-950）；呼叫方都是真的：

- `lgw.client.Client`＋`lgw.signer.Signer`；
- `llmgwctl usage` 與 `llmgwctl check` 的 `auth-local`（行程內 `lgwctl.cli.main`，
  root 判斷用測試替身）；
- 隧道探針：真的 `ops-scripts/llmgateway-tunnel` 的 `probe_healthy` 與
  `lgwctl/probe.py` 的 `local_code`／`local_ok`。

不碰憑證、不連真機、不讀 `/run/mlp-secrets` 或 `~/.ssh`；金鑰都是暫存目錄裡現產的
一次性 ed25519，測試結束就刪。
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from lgwctl import check, probe
from tests import e2e_auth as e2e

KEYS = None
_KEYS_TMP = None
_RESTORE = None
_RESTORE_CLIENT = None


def setUpModule():
    global KEYS, _KEYS_TMP, _RESTORE, _RESTORE_CLIENT
    # 每次匯入都從空的收集器開始（明確重置，不依賴別的模組殘留）。
    for values in e2e.SAMPLES.values():
        values.clear()
    e2e.ARTIFACTS.clear()
    _KEYS_TMP = tempfile.TemporaryDirectory(prefix="lgw-e2e-keys-")
    KEYS = e2e.Keys(_KEYS_TMP.name)
    for value in e2e.FAKE_CREDENTIALS:
        e2e.add_sample("credential", value)
    _RESTORE = e2e.install_signer_recorder()
    _RESTORE_CLIENT = e2e.install_client_recorder()


def tearDownModule():
    global _KEYS_TMP, _RESTORE, _RESTORE_CLIENT
    if _RESTORE_CLIENT is not None:
        _RESTORE_CLIENT()
        _RESTORE_CLIENT = None
    if _RESTORE is not None:
        _RESTORE()
        _RESTORE = None
    if _KEYS_TMP is not None:
        _KEYS_TMP.cleanup()
        _KEYS_TMP = None


#: `check` 的其餘項目換成 no-op：本檔只驗 `auth-local`（與 #271 的既有作法相同，
#: 避免 gh／systemctl 碰到真機或網路）。
CHECK_PATCHES = (
    (check, "_check_units", lambda *a, **k: (False, "missing")),
    (check, "_check_keysync", lambda *a, **k: None),
    (check, "_check_policy", lambda *a, **k: None),
    (check, "_check_gh_repo", lambda *a, **k: None),
    (check, "_check_auth_stores", lambda *a, **k: None),
)

UNAUTHORIZED = b'{"error": "unauthorized"}'
FORBIDDEN = b'{"error": "forbidden"}'
HEALTHZ_BODY = b'{"status": "ok"}'


def route(name):
    for item in e2e.ROUTES:
        if item[0] == name:
            return item
    raise KeyError(name)


def auth_local_line(text: str) -> str:
    lines = [line for line in text.splitlines() if " auth-local" in line]
    if not lines:
        raise AssertionError("輸出裡沒有 auth-local 那一行：\n" + text)
    return lines[-1]


class BoardCase(unittest.TestCase):
    """共用：真正的 lgwd 子行程版面（不含測試方法）。"""

    def board(self, mode: str, *, name: str = "board", per_key_max: int = 600,
              extra_args=()):
        board = e2e.Board(KEYS, mode, name=name, per_key_max=per_key_max,
                          extra_args=extra_args)
        self.addCleanup(board.cleanup)
        return board

    def signer(self, which: str = "b"):
        return KEYS.client_signer(which)

    def assert_status(self, result, status: int):
        self.assertEqual(result[0], status, result)

    # --- 共用小工具 -------------------------------------------------------

    @staticmethod
    def _tunnel_cfg(board, ssh_path=None) -> dict:
        return {
            "ssh_path": ssh_path or e2e.make_fake_ssh(board.root),
            "identity": str(board.root / "no-identity"),
            "connect_timeout": 5,
            "local_user": "tester",
            "remote_api": board.port,
            "probe_interval": 0.5,
        }

    @staticmethod
    def _tunnel_target(board) -> dict:
        return {
            "host": "127.0.0.1",
            "port": 22,
            "known_hosts": str(board.root / "known_hosts"),
            "follow": None,
        }

    @staticmethod
    def _wait_unavailable(board, client, timeout: float = 20.0):
        """等已簽章 list 變成 503（名冊發布失敗要等 watcher 下一輪）。"""
        def probe_status():
            result = client.list()
            return 503 if result.status == 503 else None
        return board.wait_for(probe_status, timeout=timeout)


class MatrixCase(BoardCase):
    """矩陣：三模式 × 呼叫方 × 五條業務路由＋healthz。"""

    # --- C-950 -----------------------------------------------------------

    def test_C950_independent_boards_real_subprocess_and_clean_teardown(self):
        """C-950：每個情境獨立版面；lgwd 是子行程；收尾鎖釋放且無殘留。"""
        off = self.board("off", name="iso-off")
        enforce = self.board("enforce", name="iso-enf")

        self.assertNotEqual(off.root, enforce.root)
        self.assertNotEqual(off.state, enforce.state)
        self.assertNotEqual(off.registry, enforce.registry)
        self.assertNotEqual(off.db, enforce.db)
        self.assertNotEqual(off.port, enforce.port)
        for binary in (e2e.SSH_KEYGEN, e2e.PRLIMIT):
            self.assertTrue(os.access(binary, os.X_OK), binary)

        for board in (off, enforce):
            self.assertIsNotNone(board.proc)
            self.assertNotEqual(board.proc.pid, os.getpid())
            cmdline = Path("/proc/%d/cmdline" % board.proc.pid).read_bytes().split(b"\0")
            self.assertIn(b"-m", cmdline)
            self.assertIn(b"lgwd", cmdline)
            self.assertEqual(board.healthz()[1], HEALTHZ_BODY)

        # 接縫：off 不得呼叫 open_runtime（也就沒有 spawn／touch）。
        self.assertNotIn("open_runtime", off.event_lines())
        self.assertEqual(off.spawn_count(), 0)
        # enforce 有真的 open_runtime，而且自測真的開了 prlimit（真 prlimit＋ssh-keygen）。
        self.assertIn("open_runtime", enforce.event_lines())
        self.assertGreater(enforce.spawn_count(), 0)
        self.assertIn("spawn " + os.path.basename(e2e.PRLIMIT), enforce.event_lines())
        self.assertEqual(enforce.status_document()["loaded_generation"], 1)

        off.stop()
        enforce.stop()
        # stop() 已斷言：rc=0、flock 可取得、埠可重綁、/proc 沒有帶著版面路徑的行程。

    # --- C-951 -----------------------------------------------------------

    def test_C951_off_all_routes_and_callers(self):
        """C-951：off 的 5 條業務路由＋healthz（無簽與已簽都 200／204，零 spawn）。"""
        board = self.board("off", name="off")
        signer = self.signer("b")
        for item in e2e.ROUTES:
            unsigned = board.request_route(item)
            signed = board.request_signed(item, signer)
            expected = 204 if item[0] == "cooldown" else 200
            self.assert_status(unsigned, expected)
            self.assert_status(signed, expected)
            if item[0] != "cooldown":
                self.assertEqual(unsigned[1], signed[1], item[0])
        for target in (e2e.HEALTHZ,):
            self.assertEqual(board.request_route(target)[1], HEALTHZ_BODY)
        self.assertEqual(board.request_route(target, board.signed_headers(
            signer, "GET", "/healthz"))[1], HEALTHZ_BODY)
        # 每一個回應（含 body 與標頭）都由 Board.request → record_response 收進掃描
        # 清單，不再有旁路 add_artifact；200 的 resolve 只把 env 的值換成佔位字串。

        # off 不 spawn ssh-keygen、不碰認證路徑（已簽請求也不驗章）。
        self.assertEqual(board.spawn_count(), 0, board.event_lines())
        self.assertEqual(board.touch_count(), 0, board.event_lines())
        self.assertEqual(board.event_lines(), [])

        # 客戶端（無簽）：off 與 0.1.0 相同。
        result = board.client().list()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.status, 200)
        e2e.add_artifact("off client list repr", repr(result))

        # llmgwctl usage／check（root，off 不建 signer）。
        rc, out, err = board.cli(["usage"], expect=0)
        self.assertIn("opencode-zen", out)
        # off 的非 root 也照舊可用（與 0.1.0 相同，不印任何原因）。
        rc, out, err = board.cli(["usage"], expect=0, euid=1000)
        self.assertEqual(err, "")
        rc, out, err = board.cli(["check", "--no-remote"], expect=None,
                                 patches=CHECK_PATCHES)
        self.assertIn("ok auth-local（未啟用認證）", out, err)

        # 探針：真的 lgwctl/probe.py 與真的隧道腳本探針函式（假 ssh 只跳過傳輸）。
        for mode in ("new", "old"):
            self.assertEqual(probe.local_code(board.cli_layout, mode=mode),
                             ("ok", "healthz 200"))
            self.assertTrue(e2e.tunnel_probe(board.root).probe_healthy(
                self._tunnel_cfg(board), self._tunnel_target(board), mode))

        rows = board.audit_rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNone(row["principal"], row)
        self.assertEqual({row["action"] for row in rows}, {"resolve", "cooldown"})

        log = board.stop()
        self.assertIn("auth mode=off", log)
        self.assertNotIn("outcome=", log)

    def test_C951_warn_unsigned_missing_and_signed_pass(self):
        """C-951：warn 的無簽 deny 不改業務結果；B 的已簽是 pass 並記 principal。"""
        board = self.board("warn", name="warn")
        signer = self.signer("b")
        for item in e2e.ROUTES:
            expected = 204 if item[0] == "cooldown" else 200
            self.assert_status(board.request_route(item), expected)
            self.assert_status(board.request_signed(item, signer), expected)
        self.assertEqual(board.request_route(e2e.HEALTHZ)[1], HEALTHZ_BODY)

        # llmgwctl usage（root）在 warn 以本機管理 principal 簽章、結果仍 200。
        rc, out, err = board.cli(["usage"], expect=0)
        self.assertIn("opencode-zen", out)
        # warn 不拒絕請求：非 root 也照舊（不會出現 sudo 提示）。
        rc, out, err = board.cli(["usage"], expect=0, euid=1000)
        self.assertEqual(err, "")
        # R3：check 的 auth-local 在 warn 一律 todo（不拒絕請求，無從確認）。
        rc, out, err = board.cli(["check", "--no-remote"], expect=None,
                                 patches=CHECK_PATCHES)
        line = auth_local_line(out)
        self.assertTrue(line.startswith("todo auth-local"), line)
        self.assertIn("warn", line)

        log = board.stop()
        for name in ("list", "usage", "resolve", "roles", "cooldown"):
            self.assertIn("outcome=deny reason=missing route=%s principal=-"
                          % e2e.ROUTE_TEMPLATES[name], log, name)
        # 已簽（B）的五條路由都要有 pass 日誌（不是只抽檢 resolve）。
        for name in ("list", "usage", "resolve", "roles", "cooldown"):
            self.assertIn("outcome=pass reason=- route=%s principal=w1-worker"
                          % e2e.ROUTE_TEMPLATES[name], log, name)
        self.assertIn("outcome=pass reason=- route=/v1/subscriptions principal=w1-admin", log)

        rows = board.audit_rows()
        resolve_principals = sorted(
            (row["principal"] or "-") for row in rows if row["action"] == "resolve")
        self.assertEqual(resolve_principals, ["-", "w1-worker"], rows)
        cooldown_principals = sorted(
            (row["principal"] or "-") for row in rows if row["action"] == "cooldown")
        self.assertEqual(cooldown_principals, ["-", "w1-worker"])

    def test_C951_warn_forbidden_keeps_business_and_audits_principal(self):
        """C-951／W-3e：warn 下權限不足仍照舊處理，稽核記已驗證的 principal。"""
        board = self.board("warn", name="warn-forbidden")
        admin = self.signer("admin")
        narrow = self.signer("narrow")

        status, body, _ = board.request_signed(route("resolve"), admin)
        self.assertEqual(status, 200)
        self.assertIn(b'"env"', body)          # warn 的 forbidden 不改變業務
        status, body, _ = board.request_signed(route("cooldown"), narrow)
        self.assertEqual(status, 204)

        log = board.stop()
        self.assertIn("outcome=forbidden reason=forbidden_op route=/v1/resolve "
                      "principal=w1-admin", log)
        self.assertIn("outcome=forbidden reason=forbidden_op "
                      "route=/v1/subscriptions/{id}/cooldown principal=w1-narrow", log)
        rows = board.audit_rows()
        self.assertEqual([(row["action"], row["principal"]) for row in rows],
                         [("resolve", "w1-admin"), ("cooldown", "w1-narrow")])

    def test_C951_enforce_unsigned_is_401_fixed_and_writes_no_business_audit(self):
        """C-951／C-952：enforce 的無簽一律 401 固定 body、不進業務、不寫稽核。"""
        board = self.board("enforce", name="enforce-unsigned")
        for item in e2e.ROUTES:
            status, body, headers = board.request_route(item)
            self.assertEqual(status, 401, item[0])
            self.assertEqual(body, UNAUTHORIZED, item[0])
            self.assertNotIn("www-authenticate", headers)
        self.assertEqual(board.request_route(e2e.HEALTHZ)[1], HEALTHZ_BODY)

        # 非 root 的 llmgwctl usage：401、結束碼 1、提示 sudo，且沒有稽核列。
        rc, out, err = board.cli(["usage"], expect=1, euid=1000)
        self.assertIn("enforce 下需 sudo 執行", err)
        self.assertEqual(out, "")

        client = board.client()
        before = board.connection_count()
        result = client.list()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 401)
        self.assertEqual(result.error, "unauthorized")
        self.assertFalse(result.unreachable)
        self.assertEqual(board.connection_count() - before, 1, "401 後不得重試或改送無簽")
        e2e.add_artifact("enforce unsigned client repr", repr(result))

        self.assertEqual(board.audit_rows(), [])
        log = board.stop()
        self.assertIn("outcome=deny reason=missing route=/v1/resolve principal=-", log)

    # --- C-952 -----------------------------------------------------------

    def test_C952_enforce_signed_B_all_routes_and_callers(self):
        """C-952：B 的節點全通過；稽核有 principal；llmgwctl 與探針照常。"""
        board = self.board("enforce", name="enforce-signed")
        signer = self.signer("b")
        for item in e2e.ROUTES:
            status, body, _ = board.request_signed(item, signer)
            self.assertEqual(status, 204 if item[0] == "cooldown" else 200, item[0])
            if item[0] == "resolve":
                # 200 的 resolve body 一定會帶 env（交付管道）；掃描時只排除 env 的值。
                self.assertIn(b'"env"', body)
        self.assertEqual(board.request_route(e2e.HEALTHZ)[1], HEALTHZ_BODY)

        rc, out, err = board.cli(["usage"], expect=0)
        self.assertIn("opencode-zen", out)
        rc, out, err = board.cli(["check", "--no-remote"], expect=None,
                                 patches=CHECK_PATCHES)
        self.assertIn("ok auth-local（已簽章 list 200）", out, err)

        client = board.client(self.signer("b"))
        result = client.list()
        self.assertTrue(result.ok, result)
        e2e.add_artifact("enforce signed client repr", repr(result))
        result = client.resolve("opencode", "opencode-zen")
        self.assertTrue(result.ok, result)
        self.assertIn("env", result.data)
        e2e.add_artifact("enforce signed resolve repr", repr(result))

        for mode in ("new", "old"):
            self.assertEqual(probe.local_code(board.cli_layout, mode=mode),
                             ("ok", "healthz 200"))
            self.assertTrue(e2e.tunnel_probe(board.root).probe_healthy(
                self._tunnel_cfg(board), self._tunnel_target(board), mode))

        rows = board.audit_rows()
        self.assertEqual({(row["action"], row["principal"]) for row in rows},
                         {("resolve", "w1-worker"), ("cooldown", "w1-worker")})
        self.assertEqual(len(rows), 3)   # resolve、cooldown、客戶端的 resolve
        log = board.stop()
        self.assertIn("outcome=pass reason=- route=/v1/resolve principal=w1-worker", log)
        self.assertIn("outcome=pass reason=- route=/v1/subscriptions principal=w1-admin", log)

    def test_C952_enforce_forbidden_narrow_and_local_admin(self):
        """C-952：本機管理 principal 打 resolve 是 403 且沒有 env；窄權限打 cooldown 403。"""
        board = self.board("enforce", name="enforce-forbidden")
        admin = self.signer("admin")
        narrow = self.signer("narrow")

        status, body, headers = board.request_signed(route("resolve"), admin)
        self.assertEqual(status, 403)
        self.assertEqual(body, FORBIDDEN)
        self.assertNotIn(b'"env"', body)
        self.assertNotIn("www-authenticate", headers)

        status, body, _ = board.request_signed(route("cooldown"), narrow)
        self.assertEqual(status, 403)
        self.assertEqual(body, FORBIDDEN)

        # 窄權限的 list 仍然通過（權限是逐動作判的）。
        self.assert_status(board.request_signed(route("list"), narrow), 200)

        self.assertEqual(board.audit_rows(), [])
        log = board.stop()
        self.assertIn("outcome=forbidden reason=forbidden_op route=/v1/resolve "
                      "principal=w1-admin", log)
        self.assertIn("outcome=forbidden reason=forbidden_op "
                      "route=/v1/subscriptions/{id}/cooldown principal=w1-narrow", log)

    # --- C-953 -----------------------------------------------------------

    def test_C953_client_401_403_429_503_never_retries(self):
        """C-953：客戶端對真的 lgwd 收到 401／403／429／503 都是原樣 Result、各一次請求。"""
        board = self.board("enforce", name="enforce-client", per_key_max=1)

        before = board.connection_count()
        result = board.client().list()
        self.assertEqual((result.status, result.error), (401, "unauthorized"))
        self.assertEqual(board.connection_count() - before, 1)
        e2e.add_artifact("C953 401 repr", repr(result))

        # 已簽章但名冊沒有的 key：401，而且不得改送無簽（連線數恰 1）。
        before = board.connection_count()
        result = board.client(KEYS.unregistered_signer()).list()
        self.assertEqual((result.status, result.error), (401, "unauthorized"))
        self.assertEqual(board.connection_count() - before, 1, "401 後不得改送無簽")
        e2e.add_artifact("C953 signed 401 repr", repr(result))

        before = board.connection_count()
        result = board.client(self.signer("narrow")).resolve("opencode", "opencode-zen")
        self.assertEqual((result.status, result.error), (403, "forbidden"))
        self.assertNotIn("env", result.data)
        self.assertEqual(board.connection_count() - before, 1)
        e2e.add_artifact("C953 403 repr", repr(result))

        signed = board.client(self.signer("b"))
        first = signed.list()
        self.assertTrue(first.ok, first)
        before = board.connection_count()
        second = signed.list()
        self.assertEqual((second.status, second.error), (429, "rate_limited"))
        self.assertEqual(board.connection_count() - before, 1)
        self.assertIsInstance(second.data.get("retry_after"), int)
        e2e.add_artifact("C953 429 repr", repr(second))

        # 名冊不可用（allowed_signers 寫不進去）→ enforce 503，且 healthz 仍 200。
        blocked = board.state / "allowed_signers.2"
        blocked.mkdir(mode=0o700)
        board.install_registry_file(2)
        result = self._wait_unavailable(board, signed)
        self.assertEqual(result, 503, board.log_text())
        self.assertEqual(board.request_route(e2e.HEALTHZ)[1], HEALTHZ_BODY)
        before = board.connection_count()
        unavailable = signed.list()
        self.assertEqual((unavailable.status, unavailable.error),
                         (503, "auth_unavailable"))
        self.assertEqual(board.connection_count() - before, 1)
        e2e.add_artifact("C953 503 repr", repr(unavailable))

        log = board.stop()
        self.assertIn("outcome=unavailable", log)

    # --- C-954 -----------------------------------------------------------

    def test_C954_tunnel_and_local_probes_in_three_modes(self):
        """C-954：三模式的探針；名冊不可用或 warn 降級時 healthz 仍 200。"""
        off = self.board("off", name="probe-off")
        warn = self.board("warn", name="probe-warn")
        enforce = self.board("enforce", name="probe-enforce")

        for board in (off, warn, enforce):
            self.assertEqual(board.healthz()[1], HEALTHZ_BODY)
            for mode in ("new", "old"):
                self.assertEqual(probe.local_code(board.cli_layout, mode=mode),
                                 ("ok", "healthz 200"), (board.mode, mode))
                self.assertTrue(e2e.tunnel_probe(board.root).probe_healthy(
                    self._tunnel_cfg(board), self._tunnel_target(board), mode),
                    (board.mode, mode))

        # enforce：名冊不可用（signers 發布失敗）→ 業務 503、healthz 與探針仍正常。
        (enforce.state / "allowed_signers.2").mkdir(mode=0o700)
        enforce.install_registry_file(2)
        signed = enforce.client(self.signer("b"))
        self.assertEqual(self._wait_unavailable(enforce, signed), 503,
                         enforce.log_text())
        self.assertEqual(enforce.healthz()[1], HEALTHZ_BODY)
        self.assertEqual(probe.local_code(enforce.cli_layout, mode="new"),
                         ("ok", "healthz 200"))
        self.assertTrue(e2e.tunnel_probe(enforce.root).probe_healthy(
            self._tunnel_cfg(enforce), self._tunnel_target(enforce), "new"))

        # warn：驗章工具在啟動時就不可用（降級），業務照舊 200、healthz 200。
        degraded = self.board("warn", name="probe-warn-degraded",
                              extra_args=("--auth-sshkeygen-path",
                                          str(Path(_KEYS_TMP.name) / "no-ssh-keygen")))
        self.assertEqual(degraded.healthz()[1], HEALTHZ_BODY)
        status, body, _ = degraded.request_signed(route("list"), self.signer("b"))
        self.assertEqual(status, 200)
        self.assertIn(b'"subscriptions"', body)
        self.assertEqual(probe.local_code(degraded.cli_layout, mode="new"),
                         ("ok", "healthz 200"))
        log = degraded.stop()
        self.assertIn("outcome=unavailable", log)

    # --- W-6d ------------------------------------------------------------

    def test_W6d_restart_probe_one_failure_and_measured_time(self):
        """W-6d：用背景 0.2 秒持續探針量 restart 期間最長的連續失敗時間。

        `PROBE_INTERVAL=60`、`PROBE_FAILS=3`：只要停機（最長連續失敗）小於 60 秒，
        正式守護程式以 60 秒為週期時，停機期間最多只會打到 1 次失敗，且 1 < 3 不會
        觸發隧道重開（重開邏輯本身沿用 `test_T19`）。不以「只人工打一次探針」的
        建構性保證當證據。
        """
        board = self.board("off", name="restart")
        tunnel = e2e.tunnel_probe(board.root)
        target = self._tunnel_target(board)
        ssh = e2e.make_fake_ssh(board.root)

        def probe_once():
            # 每次重讀 board.port：restart 撞埠而換埠時也跟著換。
            return tunnel.probe_healthy(self._tunnel_cfg(board, ssh), target, "new")

        self.assertTrue(probe_once())
        samples: list[tuple[float, float, bool]] = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                began = time.monotonic()
                ok = probe_once()
                samples.append((began, time.monotonic(), ok))
                time.sleep(0.2)

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            deadline = time.monotonic() + 10
            while not samples and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(samples, "背景探針沒有取樣")
            started = time.monotonic()
            # 背景探針還在飛（假 ssh 是測試自己的行程），殘留斷言在 join 之後補做。
            board.stop(check_children=False)
            board.start("enforce")
            healthy = probe_once()
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            poller.join(timeout=15)
        board.assert_no_leftover_children()

        self.assertTrue(healthy)
        longest_failure, failure_runs = longest_failure_window(samples)
        interval = float(tunnel.DEFAULTS["PROBE_INTERVAL"])
        patience = int(tunnel.DEFAULTS["PROBE_FAILS"])
        self.assertEqual(interval, 60.0)
        self.assertEqual(patience, 3)
        self.assertGreaterEqual(failure_runs, 1, samples)
        # 主要證據：最長連續失敗時間 < 一個探針週期，所以正式守護程式最多被打了
        # 一次（1 < 3，不重開）。
        self.assertLess(longest_failure, interval)
        self.assertLess(elapsed, interval)
        print("E2E-W6D restart_seconds=%.3f longest_failure=%.3f runs=%d"
              % (elapsed, longest_failure, failure_runs))
        self.assertIn("open_runtime", board.event_lines())


class LeakScanTest(BoardCase):
    """C-955：放在獨立類別、定義在檔尾，讓它明確在矩陣測試之後跑。

    若只單獨跑這一支（`-k C955`），它自己的情境也會產生樣本與要掃的輸出（下面有
    「樣本不是空的」斷言），不會空掃；但那種跑法看不到其他測試的輸出（模組級
    收集器的本質）。不用 pytest 的隨機排序時，整個檔會把前面所有測試的輸出都掃過。
    """

    def test_C955_leak_scan_of_generated_values(self):
        """C-955：本次產生過的值（含截斷／換編碼的衍生樣本）都不得出現在任何輸出。"""
        board = self.board("enforce", name="leak")
        signer = self.signer("b")
        admin = self.signer("admin")
        narrow = self.signer("narrow")

        self.assert_status(board.request_route(route("list")), 401)
        for item in e2e.ROUTES:
            board.request_signed(item, signer)
        board.request_signed(route("cooldown"), narrow)      # 403
        board.request_signed(route("resolve"), admin)        # 403
        plain = board.client()
        signed = board.client(self.signer("b"))
        e2e.add_artifact("C955 plain list repr", repr(plain.list()))
        e2e.add_artifact("C955 signed usage repr", repr(signed.usage()))
        board.cli(["usage"], expect=0)
        board.cli(["check", "--no-remote"], expect=None, patches=CHECK_PATCHES)
        log = board.stop()

        # 樣本本身確實在這次跑裡產生（不是空的掃描）；衍生樣本也一起檢查。
        self.assertTrue(any(len(value) == 240 for value in e2e.SAMPLES["signature"]))
        self.assertTrue(any(len(value) == 43 for value in e2e.SAMPLES["nonce"]))
        for kind in ("signature_prefix", "signature_tail", "signature_blob",
                     "signature_hex", "nonce_hex", "nonce_b64", "public_key",
                     "private_key", "credential"):
            self.assertTrue(e2e.SAMPLES[kind], kind)

        self.assertIn("auth mode=enforce", log)
        labels = {label for label, _ in e2e.ARTIFACTS}
        self.assertTrue(any(label.startswith("response ") for label in labels), labels)
        self.assertGreater(len(e2e.ARTIFACTS), 0)
        for label, text in e2e.ARTIFACTS:
            for kind, values in e2e.SAMPLES.items():
                for value in values:
                    if value in text:
                        # 只印 label 與樣本種類：不把整份輸出或樣本值印出來。
                        self.fail("洩密（%s）出現在 %s" % (kind, label))


def longest_failure_window(samples):
    """回 `(最長連續失敗的秒數, 失敗區段數)`；`samples` 是 (開始, 結束, 成功?) 序列。"""
    longest = 0.0
    runs = 0
    began_failures = None
    last_failure_end = None
    for began, ended, ok in samples:
        if ok:
            if began_failures is not None:
                longest = max(longest, last_failure_end - began_failures)
                runs += 1
                began_failures = None
                last_failure_end = None
        else:
            if began_failures is None:
                began_failures = began
            last_failure_end = ended
    if began_failures is not None:
        longest = max(longest, last_failure_end - began_failures)
        runs += 1
    return longest, runs


if __name__ == "__main__":
    unittest.main()
