"""D6b：生命週期的離線整合驗收（#222；契約 C-956～C-960）。

契約來源：`/home/worker/work/LLMGateway-team/REPORT-D6-arch.md` §1 的 D6b 列、
§2.2 的「生命週期（D6b）」表、§4.1 的 C-956～C-960、§4.2 的 W-6a／W-6b／W-6c／
W-6e／W-6f、§4.3 的變異 4～6、§4.4 的停損。

共用環境沿用 D6a 已合的 `tests/e2e_auth.py`（**沒有改它**）：每個情境一個獨立暫存
版面，lgwd 一律是真的 `python3 -m lgwd` 子行程，真的 `ssh-keygen`／`prlimit`，兩個庫
由真的 `llmgwctl auth-store init` 建，名冊由真的 `llmgwctl auth-registry install`
裝；`llmgwctl` 在行程內呼叫 `lgwctl.cli.main`，root 判斷用版面替身（與 D6a 相同）。

本檔只新增測試，不碰 `lgwd/`、`lgw/`、`lgwctl/` 的正式程式。兩個接縫都屬於
D6a 已用的形狀：

- 名冊檔擁有者與 `layout.euid`：`tests/e2e_auth.py` 的 `sitecustomize` 與 `Board.cli`。
- W-6a／W-6f 的「在途驗章」：在 `Board` 的 `sitecustomize` **再附加一段**（不重寫
  共用檔），把 `lgwd.sshsig.Verifier.verify` 包一層；只有測試放的 marker 檔存在時
  才在驗章入口停住並寫 `entered`，其餘時間照原樣呼叫。正式程式不動。

窗口對測試：

- W-6a → `test_W6a_inflight_old_key_uses_its_snapshot_then_next_is_401`
- W-6b → `test_C958_W6b_replay_across_restart_is_401`
- W-6c → `test_C958_W6c_32_parallel_same_nonce_exactly_one_200`
- W-6e → `test_C959_W6e_recover_store_water_now_plus_60_immediate_401`
- W-6f → `test_W6f_sigterm_inflight_returns_200_not_503`

不讀憑證、不連真機、不讀 `/run/mlp-secrets` 或 `~/.ssh`；金鑰都是暫存目錄裡現產的
一次性 ed25519，測試結束就刪。整套只用標準函式庫。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from lgw.signer import Signer as LgwSigner
from lgwd.auth_db import open_ro
from lgwd.auth_paths import auth_paths
from lgwd.auth_replay import WINDOW_SECONDS
from tests import e2e_auth as e2e
from tests.auth_fakes import principal_all, write_registry_file
from tests.auth_signer import Signer, key_entry, registry_bytes
from tests.test_http_framing import parse_response, recv_until_eof, req

KEYS = None
_KEYS_TMP = None

#: 輪替用的第二把 key（與 B 同一個 principal，重疊期兩把都有效）。
ROT_KEY_ID = "w1-key-rot"
#: 墓碑情境：同一把已撤銷公鑰換到新的 key ID（必須被擋）。
REUSED_KEY_ID = "w1-key-b-reused"

UNAUTHORIZED = b'{"error": "unauthorized"}'
HEALTHZ_BODY = b'{"status": "ok"}'

PRINCIPALS = {
    e2e.B_PRINCIPAL: principal_all(),
    e2e.ADMIN_PRINCIPAL: dict(e2e.ADMIN_PERMISSIONS),
    e2e.NARROW_PRINCIPAL: dict(e2e.NARROW_PERMISSIONS),
}

#: W-6a／W-6f 的驗章停等的注入碼：附加在 `Board` 的 `sitecustomize` 後面。
#: marker 不存在時完全照原樣（不影響任何其他情境）。
PAUSE_TEMPLATE = '''

# --- D6b 測試接縫（只在這一個暫存版面的子行程內生效）---
import os as _d6b_os
import time as _d6b_time
import lgwd.sshsig as _d6b_sshsig

_d6b_marker = {marker!r}
_d6b_entered = {entered!r}
_d6b_original_verify = _d6b_sshsig.Verifier.verify


def _d6b_paused_verify(self, *args, **kwargs):
    if _d6b_os.path.exists(_d6b_marker):
        with open(_d6b_entered, "w") as handle:
            handle.write("entered")
        while _d6b_os.path.exists(_d6b_marker):
            _d6b_time.sleep(0.01)
    return _d6b_original_verify(self, *args, **kwargs)


_d6b_sshsig.Verifier.verify = _d6b_paused_verify
'''


def setUpModule():
    global KEYS, _KEYS_TMP
    _KEYS_TMP = tempfile.TemporaryDirectory(prefix="lgw-e2e-lifecycle-keys-")
    KEYS = e2e.Keys(_KEYS_TMP.name)


def tearDownModule():
    global _KEYS_TMP
    if _KEYS_TMP is not None:
        _KEYS_TMP.cleanup()
        _KEYS_TMP = None


def registry_bytes_for(generation, *, rotation=None, revoke=(), drop=(), extra=()):
    """一份名冊：B／本機管理／窄權限三把，加上可選的輪替 key 與額外列。

    `revoke` 是改成 `status=revoked` 的 key ID（撤銷）；`drop` 是整筆移除（移除不是
    墓碑，與撤銷不同）；`extra` 是 `(key_id, signer, principal, status)` 的額外列（給
    「同公鑰換新 key ID」的墓碑情境）。三把基準 key 的公鑰都是 `KEYS` 現產的那幾把。
    """
    entries = []
    base = (
        (e2e.B_KEY_ID, "b", e2e.B_PRINCIPAL),
        (e2e.ADMIN_KEY_ID, "admin", e2e.ADMIN_PRINCIPAL),
        (e2e.NARROW_KEY_ID, "narrow", e2e.NARROW_PRINCIPAL),
    )
    for key_id, attribute, principal_name in base:
        if key_id in drop:
            continue
        status = "revoked" if key_id in revoke else "active"
        entries.append(key_entry(key_id, getattr(KEYS, attribute),
                                 principal_name=principal_name, status=status))
    if rotation is not None and ROT_KEY_ID not in drop:
        entries.append(key_entry(ROT_KEY_ID, rotation, principal_name=e2e.B_PRINCIPAL))
    for key_id, signer, principal_name, status in extra:
        entries.append(key_entry(key_id, signer, principal_name=principal_name,
                                 status=status))
    return registry_bytes(generation, entries, PRINCIPALS)


def route(name):
    for item in e2e.ROUTES:
        if item[0] == name:
            return item
    raise KeyError(name)


def tombstone_ids(board):
    """信任庫裡的墓碑 key ID（唯讀開；lgwd 停著時用）。"""
    paths = auth_paths(str(board.state), registry=str(board.registry))
    db = open_ro(paths.trust_db)
    try:
        return [str(row[0]) for row in db.read(
            "SELECT key_id FROM tombstones ORDER BY key_id")]
    finally:
        db.close()


def water_rows(board):
    """防重放庫每個 key 的清除水位 H（唯讀開；lgwd 停著時用）。"""
    paths = auth_paths(str(board.state), registry=str(board.registry))
    db = open_ro(paths.replay_db)
    try:
        return {str(row[0]): int(row[1]) for row in db.read(
            "SELECT key_id, h FROM water ORDER BY key_id")}
    finally:
        db.close()


def nonce_row_count(board):
    """防重放庫目前的 nonce 列數（唯讀開；lgwd 執行中也可以讀）。"""
    paths = auth_paths(str(board.state), registry=str(board.registry))
    db = open_ro(paths.replay_db)
    try:
        return int(db.read("SELECT COUNT(*) FROM nonces")[0][0])
    finally:
        db.close()


def registry_generation(board) -> int:
    """目前名冊檔的世代（直接讀檔；用來看 install 是不是已經寫進去）。"""
    return int(json.loads(board.registry.read_text(encoding="utf-8"))["generation"])


class LifecycleCase(unittest.TestCase):
    """共用：真的 lgwd 子行程版面（不含測試方法）。"""

    def board(self, mode: str, *, name: str = "board", per_key_max: int = 600,
              extra_args=()):
        board = e2e.Board(KEYS, mode, name=name, per_key_max=per_key_max,
                          extra_args=extra_args)
        self.addCleanup(board.cleanup)
        return board

    def new_signer(self, name: str = "key-rot"):
        directory = tempfile.TemporaryDirectory(prefix="lgw-e2e-lifecycle-key-")
        self.addCleanup(directory.cleanup)
        return Signer(directory.name, name)

    @staticmethod
    def client_signer(key_id: str, signer):
        return LgwSigner(key_id=key_id, private_key_path=signer.private_path)

    def signer(self, which: str = "b"):
        return KEYS.client_signer(which)

    def install(self, board, raw, *, timeout: float = 15.0, expect=0, args=()):
        """把 `raw` 寫到候選檔，再跑真的 `llmgwctl auth-registry install`。"""
        write_registry_file(str(board.candidate), raw)
        argv = ["auth-registry", "install", str(board.candidate),
                "--timeout", str(timeout)] + list(args)
        return board.cli(argv, expect=expect)

    def assert_status(self, result, status: int):
        self.assertEqual(result[0], status, result)

    @staticmethod
    def wait_for_path(path, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return True
            time.sleep(0.01)
        return os.path.exists(path)

    @staticmethod
    def serving_generation(board):
        """lgwd 現在可服務且已載入的世代；healthz 不是 200 或 status 讀不到就回 None。"""
        try:
            status, _, _ = board.healthz()
        except OSError:
            return None
        if status != 200:
            return None
        document = board.status_document()
        if not isinstance(document, dict):
            return None
        return document.get("loaded_generation")

    def pause_verification(self, board):
        """在版面的 `sitecustomize` 附加驗章停等；回 `(marker, entered)`。

        重啟一次讓 lgwd 讀到新的 `sitecustomize`（`Board` 原本那一段不動）。
        marker 存在時，下一個進到 `Verifier.verify` 的請求會寫 `entered` 並停住，
        直到 marker 被刪掉。
        """
        marker = board.root / "pause-marker"
        entered = board.root / "pause-entered"
        with open(board.sitecustomize, "a", encoding="utf-8") as handle:
            handle.write(PAUSE_TEMPLATE.format(marker=str(marker), entered=str(entered)))
        board.stop()
        board.start("enforce")
        return marker, entered

    def run_start_attempt(self, board, mode: str):
        """用同一份版面直接起一個 lgwd，回 `CompletedProcess`（用來驗結束碼 78）。"""
        return subprocess.run(
            board._cmd(mode), cwd=str(e2e.REPO_ROOT), env=board._env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60,
        )


class RotationTests(LifecycleCase):
    """C-956：雙鍵輪替與撤銷生效的界線。"""

    def test_C956_rotation_overlap_then_revoke_boundary_is_install_zero(self):
        """C-956：重疊期兩把都 200；撤銷後 install 回 0 的同一刻，舊鍵立刻 401。"""
        board = self.board("enforce", name="rotation")
        rotation = self.new_signer()
        old = self.signer("b")
        new = self.client_signer(ROT_KEY_ID, rotation)

        # 重疊期：同一世代兩把都是 active。
        rc, out, err = self.install(
            board, registry_bytes_for(2, rotation=rotation), timeout=15.0)
        self.assertIn("已載入世代 2", out)
        self.assert_status(board.request_signed(route("list"), old), 200)
        self.assert_status(board.request_signed(route("list"), new), 200)

        # 撤銷：舊鍵改 revoked、新鍵留下；install 回 0 就是舊鍵失效的界線。
        rc, out, err = self.install(
            board, registry_bytes_for(3, rotation=rotation, revoke=(e2e.B_KEY_ID,)),
            timeout=15.0)
        self.assertIn("已載入世代 3", out)
        status, body, _ = board.request_signed(route("list"), old)
        self.assertEqual(status, 401, "install 回 0 之後舊鍵不得再有 200")
        self.assertEqual(body, UNAUTHORIZED)
        self.assert_status(board.request_signed(route("list"), new), 200)

        log = board.stop()
        self.assertIn(
            "outcome=deny reason=revoked route=/v1/subscriptions principal=-", log)
        self.assertIn(
            "outcome=pass reason=- route=/v1/subscriptions principal=w1-worker", log)


class InstallConfirmationTests(LifecycleCase):
    """C-957：撤銷確認的三種不成立情形都不得回 0 並宣稱已載入。"""

    def test_C957_no_wait_returns_zero_and_says_unconfirmed(self):
        """C-957：`--no-wait` 可以回 0，但一定要印「未確認載入，不得宣告撤銷完成」。"""
        board = self.board("enforce", name="no-wait")
        rc, out, err = self.install(
            board, registry_bytes_for(2), timeout=15.0, args=("--no-wait",))
        self.assertIn("未確認載入，不得宣告撤銷完成", out)
        self.assertNotIn("已載入世代", out)
        # 檔案真的寫進去了（只是沒有等載入確認）。
        self.assertEqual(registry_generation(board), 2)

    def test_C957_timeout_with_frozen_lgwd_returns_one(self):
        """C-957：lgwd 停住（載入不可能發生）時，install 逾時回 1 且不宣告撤銷完成。

        收尾順序（agy 高-1；真正根因見 REPORT-294-archreview2 §1.2）：缺陷在 lgwd
        的 signal handler——`lgwd/main.py` 的 `on_signal` 先寫 `received …` 的 log、
        後 `stop.set()`。SIGTERM 若落在主執行緒自己正在寫 stderr（啟動時的
        `serving api=…`）的時候，log 寫入的重入保護會丟 `RuntimeError`，例外把
        `stop.set()` 吞掉，主執行緒回到 `while not stop.wait(0.5)` 永遠等不到停止事件，
        最後被 SIGKILL。已另開 d 單 #297 修正式程式；本支先把 SIGTERM 移出那個窗口：
        SIGCONT 之後等 lgwd 回到可服務狀態（healthz 200 且 status 已載入 install 寫入
        的世代），才讓 cleanup 送 SIGTERM。#297 合併後可以考慮拿掉這個等待，當作那個
        缺陷的回歸保護。
        """
        board = self.board("enforce", name="timeout")
        os.kill(board.proc.pid, signal.SIGSTOP)
        settled = False
        try:
            rc, out, err = self.install(
                board, registry_bytes_for(2), timeout=0.3, expect=None)
        finally:
            os.kill(board.proc.pid, signal.SIGCONT)
            settled = bool(board.wait_for(
                lambda: self.serving_generation(board) == 2, timeout=20.0))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("不得宣告撤銷完成", out)
        self.assertNotIn("已載入世代", out)
        # 逾時是「已寫入、未確認」，不是驗證失敗：名冊檔已是新世代。
        self.assertEqual(registry_generation(board), 2)
        self.assertTrue(settled, "SIGCONT 後 lgwd 沒有回到已載入世代 2 的可服務狀態")

    def test_C957_install_with_lgwd_stopped_returns_one(self):
        """C-957：lgwd 停著時 install 不得回 0；名冊已寫入但不得宣告撤銷完成。"""
        board = self.board("enforce", name="stopped")
        board.stop()
        rc, out, err = self.install(
            board, registry_bytes_for(2), timeout=0.3, expect=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("不得宣告撤銷完成", out)
        self.assertNotIn("已載入世代", out)
        self.assertIn("fail 名冊已寫入但服務尚未載入", out)
        self.assertEqual(registry_generation(board), 2)


class ReplayTests(LifecycleCase):
    """C-958：重放的單次 200、跨重啟、32 路並行（W-6b、W-6c）。"""

    def test_C958_same_request_twice_second_is_401(self):
        """C-958：同一個已簽請求送兩次，第二次 401，業務只做一次（resolve 只記 1 列）。"""
        board = self.board("enforce", name="replay-twice")
        signer = self.signer("b")
        target = "/v1/resolve?agent=opencode&provider=opencode-zen"
        headers = board.signed_headers(signer, "GET", target)

        first = board.request("GET", target, headers)
        self.assert_status(first, 200)
        second = board.request("GET", target, headers)
        self.assertEqual(second[0], 401)
        self.assertEqual(second[1], UNAUTHORIZED)
        self.assertEqual(nonce_row_count(board), 1)
        # 業務只做一次：resolve 是會寫稽核的動作，重放那一筆不得再進業務。
        rows = board.audit_rows()
        self.assertEqual([(row["action"], row["principal"]) for row in rows],
                         [("resolve", "w1-worker")])

        log = board.stop()
        self.assertIn("outcome=deny reason=replay route=/v1/resolve", log)

    def test_C958_W6b_replay_across_restart_is_401(self):
        """W-6b／C-958：重啟前被接受的請求，重啟後重送必須 401（防重放庫是持久的）。"""
        board = self.board("enforce", name="replay-restart")
        signer = self.signer("b")
        headers = board.signed_headers(signer, "GET", "/v1/subscriptions")

        self.assert_status(board.request("GET", "/v1/subscriptions", headers), 200)
        board.stop()
        board.start("enforce")
        replay = board.request("GET", "/v1/subscriptions", headers)
        self.assertEqual(replay[0], 401)
        self.assertEqual(replay[1], UNAUTHORIZED)

        log = board.stop()
        self.assertIn("outcome=deny reason=replay route=/v1/subscriptions", log)

    def test_C958_W6c_32_parallel_same_nonce_exactly_one_200(self):
        """W-6c／C-958：同一個 nonce 32 路真的並行，恰一個 200，其餘都是 401。"""
        board = self.board("enforce", name="replay-parallel")
        signer = self.signer("b")
        headers = board.signed_headers(signer, "GET", "/v1/subscriptions")
        raw = req("GET", "/v1/subscriptions", headers)

        count = 32
        barrier = threading.Barrier(count)
        statuses = [None] * count

        # 先把 32 條連線都建好（避開 listen backlog），再一次同時送出。
        sockets = []
        try:
            for _ in range(count):
                sock = socket.create_connection(
                    ("127.0.0.1", board.port), timeout=20)
                sock.settimeout(20)
                sockets.append(sock)

            def one(index: int, sock) -> None:
                try:
                    barrier.wait(timeout=20)
                    sock.sendall(raw)
                    statuses[index] = parse_response(recv_until_eof(sock))[0]
                except Exception as exc:  # noqa: BLE001 - 失敗要出現在斷言裡
                    statuses[index] = "error:{0}".format(type(exc).__name__)

            threads = [threading.Thread(target=one, args=(index, sockets[index]),
                                        daemon=True)
                       for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass

        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual([item for item in statuses if item != 200], [401] * (count - 1),
                         statuses)
        # 防重放庫只留一列：恰一次被接受。
        self.assertEqual(nonce_row_count(board), 1)

        log = board.stop()
        self.assertIn("outcome=deny reason=replay route=/v1/subscriptions", log)


class FallbackAndRepairTests(LifecycleCase):
    """C-959：回退、庫損毀、recover、rebuild 與墓碑。"""

    def test_C959_fallback_enforce_warn_off_each_step_matches_matrix(self):
        """C-959：enforce→warn→off 每一步重啟後的行為與 C-951 的矩陣相同。

        斷言與 D6a 的 C-951 同等：狀態碼、401 的固定 body（逐 byte）與不帶
        `WWW-Authenticate`、業務稽核列數與 principal、日誌 outcome。
        """
        board = self.board("enforce", name="fallback")
        signer = self.signer("b")

        # enforce：無簽 401 固定 body、不進業務、不寫稽核；已簽 200／204 並記 principal。
        for item in e2e.ROUTES:
            expected = 204 if item[0] == "cooldown" else 200
            status, body, headers = board.request_route(item)
            self.assertEqual(status, 401, item[0])
            self.assertEqual(body, UNAUTHORIZED, item[0])
            self.assertNotIn("www-authenticate", headers, item[0])
            signed = board.request_signed(item, signer)
            self.assertEqual(signed[0], expected, item[0])
            if item[0] == "resolve":
                # 200 的 resolve 一定會帶 env（交付管道）。
                self.assertIn(b'"env"', signed[1])
        self.assertEqual(board.request_route(e2e.HEALTHZ)[1], HEALTHZ_BODY)
        self.assertEqual([(row["action"], row["principal"]) for row in board.audit_rows()],
                         [("resolve", "w1-worker"), ("cooldown", "w1-worker")])
        log = board.stop()
        for name in ("list", "usage", "resolve", "roles", "cooldown"):
            self.assertIn("outcome=deny reason=missing route=%s principal=-"
                          % e2e.ROUTE_TEMPLATES[name], log, name)
            self.assertIn("outcome=pass reason=- route=%s principal=w1-worker"
                          % e2e.ROUTE_TEMPLATES[name], log, name)

        # warn：無簽照舊 200／204（body 與已簽相同）並記 would-deny；已簽 pass。
        board.start("warn")
        for item in e2e.ROUTES:
            expected = 204 if item[0] == "cooldown" else 200
            unsigned = board.request_route(item)
            signed = board.request_signed(item, signer)
            self.assertEqual(unsigned[0], expected, item[0])
            self.assertEqual(signed[0], expected, item[0])
            if item[0] != "cooldown":
                self.assertEqual(unsigned[1], signed[1], item[0])
        self.assertEqual([(row["action"], row["principal"]) for row in board.audit_rows()],
                         [("resolve", "w1-worker"), ("cooldown", "w1-worker"),
                          ("resolve", None), ("resolve", "w1-worker"),
                          ("cooldown", None), ("cooldown", "w1-worker")])
        log = board.stop()
        for name in ("list", "usage", "resolve", "roles", "cooldown"):
            self.assertIn("outcome=deny reason=missing route=%s principal=-"
                          % e2e.ROUTE_TEMPLATES[name], log, name)
            self.assertIn("outcome=pass reason=- route=%s principal=w1-worker"
                          % e2e.ROUTE_TEMPLATES[name], log, name)

        # off：無簽與已簽都照舊 200／204、body 相同；不驗章、不 spawn、不記 outcome。
        board.start("off")
        for item in e2e.ROUTES:
            expected = 204 if item[0] == "cooldown" else 200
            unsigned = board.request_route(item)
            signed = board.request_signed(item, signer)
            self.assertEqual(unsigned[0], expected, item[0])
            self.assertEqual(signed[0], expected, item[0])
            if item[0] != "cooldown":
                self.assertEqual(unsigned[1], signed[1], item[0])
        self.assertEqual(board.request_route(e2e.HEALTHZ)[1], HEALTHZ_BODY)
        self.assertEqual(board.spawn_count(), 0)
        # 稽核照舊寫入，但沒有已驗證身分（off 不驗章）。
        off_rows = [(row["action"], row["principal"]) for row in board.audit_rows()][6:]
        self.assertEqual(off_rows, [("resolve", None), ("resolve", None),
                                    ("cooldown", None), ("cooldown", None)])
        log = board.stop()
        self.assertIn("auth mode=off", log)
        self.assertNotIn("outcome=", log)

    def test_C959_corrupt_replay_store_enforce_78_warn_degrades(self):
        """C-959：防重放庫損毀時 enforce 以 78 結束、warn 降級；業務照舊 200。

        對應 §2.2 生命週期表的「replay 庫損毀：enforce 啟動 78、warn 降級 200」；
        recover-store 之後能啟動並立刻 401 由 W-6e 那一支驗。
        """
        board = self.board("enforce", name="corrupt-replay")
        board.stop()
        (board.state / "auth-replay.sqlite").write_bytes(b"not a sqlite database")

        failed = self.run_start_attempt(board, "enforce")
        self.assertEqual(failed.returncode, 78, failed.stderr)
        self.assertIn(b"cannot start authentication", failed.stderr)
        self.assertIn(b"replay", failed.stderr)

        # warn：庫不可用只降級、業務照舊（200），healthz 仍 200。
        board.start("warn")
        self.assertEqual(board.healthz()[1], HEALTHZ_BODY)
        self.assertEqual(board.request_signed(route("list"), self.signer("b"))[0], 200)
        self.assertEqual(board.request_route(route("list"))[0], 200)
        log = board.stop()
        self.assertIn("auth event=degraded", log)
        self.assertIn("area=stores", log)
        self.assertIn("store=replay", log)

    def test_C959_W6e_recover_store_water_now_plus_60_immediate_401(self):
        """W-6e／C-959：recover-store 把 H 設成 now＋60；之後立刻送的已簽請求是 401。"""
        board = self.board("enforce", name="recover-water")
        board.stop()
        (board.state / "auth-replay.sqlite").write_bytes(b"broken replay store")

        rc, out, err = board.cli(["auth-replay", "recover-store", "--confirm"],
                                 expect=None)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("每把 key 在 60 秒後才能再簽", out)

        now = int(time.time())
        water = water_rows(board)
        self.assertIn(e2e.B_KEY_ID, water)
        # H 一定在未來（now＋60＝一個時窗），不在行程裡等 60 秒。
        self.assertGreaterEqual(water[e2e.B_KEY_ID], now + WINDOW_SECONDS - 2)
        self.assertLessEqual(water[e2e.B_KEY_ID], now + WINDOW_SECONDS + 5)

        board.start("enforce")
        status, body, _ = board.request_signed(route("list"), self.signer("b"))
        self.assertEqual(status, 401, "recover 之後的 H 在未來，不得是 503")
        self.assertEqual(body, UNAUTHORIZED)
        log = board.stop()
        self.assertIn("outcome=deny reason=water route=/v1/subscriptions", log)

    def test_C959_recover_and_rebuild_keep_tombstones_and_block_revival(self):
        """C-959：recover、rebuild 之後能啟動，墓碑還在，被撤銷的鍵不得復活。"""
        board = self.board("enforce", name="tombstone")
        rotation = self.new_signer()
        old = self.signer("b")
        new = self.client_signer(ROT_KEY_ID, rotation)

        # 撤銷 B：install 之後信任庫要有 B 的墓碑。
        rc, out, err = self.install(
            board, registry_bytes_for(2, rotation=rotation, revoke=(e2e.B_KEY_ID,)),
            timeout=15.0)
        self.assertIn("已載入世代 2", out)
        self.assertEqual(tombstone_ids(board), [e2e.B_KEY_ID])
        self.assert_status(board.request_signed(route("list"), old), 401)
        self.assert_status(board.request_signed(route("list"), new), 200)

        # --- recover-store：防重放庫損毀，墓碑與水位不動 ---
        board.stop()
        (board.state / "auth-replay.sqlite").write_bytes(b"broken replay store")
        rc, out, err = board.cli(["auth-replay", "recover-store", "--confirm"],
                                 expect=None)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(tombstone_ids(board), [e2e.B_KEY_ID])

        board.start("enforce")
        self.assertEqual(board.healthz()[1], HEALTHZ_BODY)
        # H 已被設成未來：舊鍵（撤銷）與新鍵（水位）都是 401，差別在原因。
        self.assert_status(board.request_signed(route("list"), old), 401)
        self.assert_status(board.request_signed(route("list"), new), 401)
        # 復活：同一把 key（同 ID、同公鑰）改回 active 必須被墓碑擋下。
        write_registry_file(str(board.candidate), registry_bytes_for(3, rotation=rotation))
        rc, out, err = board.cli(
            ["auth-registry", "install", str(board.candidate), "--timeout", "15"],
            expect=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)
        rc, out, err = board.cli(
            ["auth-registry", "check", str(board.candidate), "--against-trust"],
            expect=None)
        self.assertEqual(rc, 1, out + err)
        # 另一半：把撤銷的 key 拿掉、同一把公鑰換成新的 key ID（重用墓碑 bytes）也要被擋。
        write_registry_file(str(board.candidate), registry_bytes_for(
            3, rotation=rotation, drop=(e2e.B_KEY_ID,),
            extra=((REUSED_KEY_ID, KEYS.b, e2e.B_PRINCIPAL, "active"),)))
        rc, out, err = board.cli(
            ["auth-registry", "install", str(board.candidate), "--timeout", "15"],
            expect=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)
        rc, out, err = board.cli(
            ["auth-registry", "check", str(board.candidate), "--against-trust"],
            expect=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)
        log = board.stop()
        self.assertIn("outcome=deny reason=water", log)

        # --- rebuild-trust：信任庫遺失，從已核驗的名冊重建（墓碑由 revoked 列重建） ---
        (board.state / "auth-trust.sqlite").unlink()
        write_registry_file(str(board.candidate),
                            registry_bytes_for(2, rotation=rotation,
                                               revoke=(e2e.B_KEY_ID,)))
        rc, out, err = board.cli(
            ["auth-registry", "rebuild-trust", str(board.candidate), "--confirm"],
            expect=None)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("回退防護已失效，名冊須人工核驗", out)
        self.assertEqual(tombstone_ids(board), [e2e.B_KEY_ID])

        board.start("enforce")
        self.assertEqual(board.healthz()[1], HEALTHZ_BODY)
        self.assert_status(board.request_signed(route("list"), old), 401)
        # 復活仍然被擋（同 ID、同公鑰改回 active）。
        write_registry_file(str(board.candidate), registry_bytes_for(4, rotation=rotation))
        rc, out, err = board.cli(
            ["auth-registry", "install", str(board.candidate), "--timeout", "15"],
            expect=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)
        self.assert_status(board.request_signed(route("list"), old), 401)
        # rebuild 之後同樣驗另一半：同一把公鑰換新 key ID 仍被擋。
        write_registry_file(str(board.candidate), registry_bytes_for(
            4, rotation=rotation, drop=(e2e.B_KEY_ID,),
            extra=((REUSED_KEY_ID, KEYS.b, e2e.B_PRINCIPAL, "active"),)))
        rc, out, err = board.cli(
            ["auth-registry", "install", str(board.candidate), "--timeout", "15"],
            expect=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("tombstone_violation", out)
        board.stop()


class ResetTests(LifecycleCase):
    """C-960：reset-generation 的行程情境（reset-key-water 沿用 D5 的單元測試）。"""

    def test_C960_reset_generation_process_scenario_12_vs_100(self):
        """C-960：誤裝世代 100 → reset-generation 降到 12；只在 12 的鍵 200、只在 100 的 401。"""
        board = self.board("enforce", name="reset-generation")
        rotation = self.new_signer()
        old = self.signer("b")
        new = self.client_signer(ROT_KEY_ID, rotation)

        # 誤裝世代 100：只留輪替 key（B 被移除，不是撤銷）。
        rc, out, err = self.install(
            board, registry_bytes_for(100, rotation=rotation, drop=(e2e.B_KEY_ID,)),
            timeout=15.0)
        self.assertIn("已載入世代 100", out)
        self.assert_status(board.request_signed(route("list"), old), 401)
        self.assert_status(board.request_signed(route("list"), new), 200)

        board.stop()
        write_registry_file(str(board.candidate),
                            registry_bytes_for(12, drop=(ROT_KEY_ID,)))
        rc, out, err = board.cli(
            ["auth-registry", "reset-generation", str(board.candidate), "--confirm"],
            expect=None)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("generation 100 → 12", out)

        board.start("enforce")
        self.assertEqual(board.healthz()[1], HEALTHZ_BODY)
        self.assertEqual(board.status_document()["loaded_generation"], 12)
        self.assert_status(board.request_signed(route("list"), old), 200)
        self.assert_status(board.request_signed(route("list"), new), 401)
        board.stop()


class WindowTests(LifecycleCase):
    """W-6a（install 與在途驗章的窗口）與 W-6f（SIGTERM 時的在途驗章）。"""

    def test_W6a_inflight_old_key_uses_its_snapshot_then_next_is_401(self):
        """W-6a：install 寫檔後、lgwd 載入前，在途的舊鍵請求用它的快照判 200；下一筆 401。"""
        board = self.board("enforce", name="window-a")
        rotation = self.new_signer()
        marker, entered = self.pause_verification(board)
        old = self.signer("b")
        headers = board.signed_headers(old, "GET", "/v1/subscriptions")
        result = {}

        def send():
            try:
                result["response"] = board.request("GET", "/v1/subscriptions", headers)
            except Exception as exc:  # noqa: BLE001 - 失敗要出現在斷言裡
                result["error"] = exc

        marker.write_text("pause", encoding="utf-8")
        thread = threading.Thread(target=send, daemon=True)
        try:
            thread.start()
            self.assertTrue(self.wait_for_path(entered),
                            "請求沒有進到驗章（在途窗口沒有建立）")
            # 請求停在驗章中；install 寫檔並等載入確認。
            rc, out, err = self.install(
                board, registry_bytes_for(2, rotation=rotation,
                                          revoke=(e2e.B_KEY_ID,)),
                timeout=15.0)
            self.assertIn("已載入世代 2", out)
            # install 回 0 之後新快照一定已生效：此刻的新舊界線就是 401。
            status, body, _ = board.request_signed(route("list"), old)
            self.assertEqual(status, 401)
            self.assertEqual(body, UNAUTHORIZED)
        finally:
            marker.unlink(missing_ok=True)
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        # 在途那一筆依它取得的舊快照（B 還是 active）判定。
        self.assertEqual(result["response"][0], 200)
        self.assert_status(board.request_signed(route("list"), old), 401)
        self.assert_status(
            board.request_signed(route("list"),
                                 self.client_signer(ROT_KEY_ID, rotation)), 200)
        board.stop()

    def test_W6f_sigterm_inflight_returns_200_not_503(self):
        """W-6f：SIGTERM 時有一筆在途驗章；它做完（200）或依 C-605 收尾，不得 503。

        同步方式（審查 agy 中-2）：不在 SIGTERM 之後盲等。`board.stop()` 在背景
        執行緒送**唯一一次** SIGTERM；主執行緒輪詢 lgwd 日誌，等到 signal handler 已
        執行的 `received SIGTERM, shutting down` 那一行（可觀測條件），確認行程還活著
        （等於在等這一筆在途請求）才放行驗章。收尾的 rc／鎖／埠／殘留斷言都由同一個
        `board.stop()` 完成，不再由測試自己送第二次 SIGTERM（那會在收尾尾巴把行程
        殺成 rc=-15，實測重現兩次）。
        """
        board = self.board("enforce", name="window-f")
        marker, entered = self.pause_verification(board)
        signer = self.signer("b")
        headers = board.signed_headers(signer, "GET", "/v1/subscriptions")
        process = board.proc
        result = {}
        stopped = {}

        def send():
            try:
                result["response"] = board.request("GET", "/v1/subscriptions", headers)
            except Exception as exc:  # noqa: BLE001 - 失敗要出現在斷言裡
                result["error"] = exc

        def shutdown():
            try:
                stopped["log"] = board.stop()
            except BaseException as exc:  # noqa: BLE001 - 收尾失敗要傳回主執行緒
                stopped["error"] = exc

        marker.write_text("pause", encoding="utf-8")
        sender = threading.Thread(target=send, daemon=True)
        stopper = threading.Thread(target=shutdown, daemon=True)
        try:
            sender.start()
            self.assertTrue(self.wait_for_path(entered),
                            "請求沒有進到驗章（在途窗口沒有建立）")
            stopper.start()
            self.assertTrue(
                board.wait_for(
                    lambda: "received SIGTERM, shutting down" in board.log_text(),
                    timeout=20.0),
                "lgwd 沒有在 20 秒內處理 SIGTERM：\n" + board.log_text())
            self.assertIsNone(process.poll(), "lgwd 不該切斷在途請求直接結束")
        finally:
            marker.unlink(missing_ok=True)
        sender.join(timeout=30)
        stopper.join(timeout=40)
        self.assertFalse(sender.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertNotIn("error", result, result.get("error"))
        if "error" in stopped:
            raise stopped["error"]
        self.assertEqual(result["response"][0], 200, "在途請求不得是 503")
        self.assertNotIn("outcome=unavailable", stopped["log"])


if __name__ == "__main__":
    unittest.main()
