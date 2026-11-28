"""D5c：會寫庫的 `llmgwctl` 指令與 `check` 的三項整合（#274／#222 D5c）。

契約對應（C-818～C-829 的逐條是團隊目錄 REPORT-D45-arch §2／§4 與
REPORT-270-archreview §4、archreview2 §2 的整理）：

- C-818 `auth-store init`：root＋store flock、兩庫都不存在才建、已存在回 1、
  審計紀錄或世代 > 1 時印「回退防護已失效」
- C-819 `auth-replay recover-store`：只重建 replay、損壞或識別不符先改名
  `.broken-<時刻>`、新庫寫入信任庫識別、每把 key 的 H＝now＋60、信任庫缺失回 1
- C-820 `auth-replay reset-key-water`：`--key`／`--all-future` 二擇一、
  H 設 now−65；沒有 `--confirm` 是唯讀預覽（`open_ro`）——印伺服器時間、每把
  目標 key 目前的 H 與代價，不改庫、回 2；帶 `--confirm` 時印 key id 與
  `舊 H -> 新 H`；找不到 key 回 1
- C-821 `auth-registry rebuild-trust`：名冊須過 parser、健康的信任庫拒絕、
  必印「回退防護已失效」、replay 不可用時印「接著跑 recover-store」
- C-822 `auth-registry reset-generation`：墓碑不過什麼都不改、原子寫名冊、
  同一個 transaction 換水位與快照、可重跑（冪等）
- C-823 寫入共通：非 root 回 2、缺 `--confirm` 回 2、錯誤照
  `AdminError.exit_code`；印 `hint` 與 `owner_error`
- C-824 寫入共通：取 store flock；lgwd 在跑（鎖被持有）回 2、庫完全不變
- C-825 寫名冊檔的指令依序取「store flock → install 鎖」
- C-826 審計一律 `auth_admin.AuditLog`：一行 JSON、固定七個欄位、0600、去敏
- C-827 權限收尾：成功後沒有殘留 `-wal`／`-shm`、庫與鎖 0600、prefix 不 chown、
  lgwd 的開庫／取鎖路徑可用（G-6）
- C-828 `check` 三項：`auth-heartbeat`（>180 秒 fail）、`auth-water`（H 在
  未來 fail）、`auth-stores`（缺失或識別不符 fail）；唯讀、經 `auth_db.open_ro`、
  不建檔
- C-829 寫入前置：未正常關閉（有 `-wal` 無 `-shm`）時持 store flock 先以副本探測、
  再以 `open_rw` 回收 WAL；判定損毀／未知／I/O 類時不中止，交給後面的健康判斷
  連同 `-wal` 改名 `.broken-<時刻>` 留證後重建（`recover-store`／`rebuild-trust`）

全部在 `tests/lgwctl_fakes.Sandbox` 的 prefix 沙箱裡跑：不碰真機、不讀憑證、
不動 `/var/lib/llmgateway`。root 身分以 patch `os.geteuid` 模擬（prefix 模式
一律不 chown）。
"""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from lgwd.auth_admin import AdminError, AuditLog, init_stores
from lgwd.auth_db import (
    REPLAY_SCHEMA,
    TRUST_SCHEMA,
    acquire_store_lock,
    check_store_identity,
    create_store,
    open_rw,
)
from lgwd.clock import SystemClock
from lgwctl import check
from lgwctl.cli import build_parser
from lgwctl.layout import Layout
from lgwctl.report import Report
from tests.auth_fakes import VALID_B, key, registry_bytes, write_registry_file
from tests.lgwctl_fakes import Sandbox
from tests.test_lgwctl_auth_270 import (
    candidate_file,
    make_crashed_replay,
    run,
    sandbox_paths,
    seed_stores,
    set_water,
    status_document,
    write_status,
)

#: 五個寫入指令的成功情境（`prepare` 的鍵）。`init` 要空目錄，其餘都要可用的兩庫。
SETUPS = ("init", "recover-store", "reset-key-water", "rebuild-trust", "reset-generation")

#: 情境 → 審計的 `command` 欄。
AUDIT_COMMANDS = {
    "init": "auth-store init",
    "recover-store": "auth-replay recover-store",
    "reset-key-water": "auth-replay reset-key-water",
    "rebuild-trust": "auth-registry rebuild-trust",
    "reset-generation": "auth-registry reset-generation",
}

AUDIT_FIELDS = {"time", "uid", "sudo_user", "command", "args", "old", "new"}


@pytest.fixture
def box():
    b = Sandbox()
    os.makedirs(b.state(), exist_ok=True)
    os.makedirs(b.etc(), exist_ok=True)
    yield b
    b.close()


# --- 共用夾具 ---------------------------------------------------------------


def prepare(box, setup):
    """把沙箱準備成該指令的成功情境；回 `(argv, paths, candidate)`。"""
    paths = sandbox_paths(box)
    candidate = None
    if setup == "init":
        return ["auth-store", "init", "--confirm"], paths, candidate
    if setup == "recover-store":
        seed_stores(box, registry_bytes())
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(paths.replay_db + suffix):
                os.unlink(paths.replay_db + suffix)
        return ["auth-replay", "recover-store", "--confirm"], paths, candidate
    if setup == "reset-key-water":
        seed_stores(box, registry_bytes())
        set_water(paths.replay_db, "w1-tunnel-a", int(time.time()) + 3600)
        return ["auth-replay", "reset-key-water", "--all-future", "--confirm"], paths, candidate
    if setup == "rebuild-trust":
        seed_stores(box, registry_bytes(5))
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(paths.trust_db + suffix):
                os.unlink(paths.trust_db + suffix)
        candidate = candidate_file(box, registry_bytes(5), "candidate-rebuild.json")
        return ["auth-registry", "rebuild-trust", candidate, "--confirm"], paths, candidate
    if setup == "reset-generation":
        seed_stores(box, registry_bytes(5))
        candidate = candidate_file(box, registry_bytes(6), "candidate-reset.json")
        return ["auth-registry", "reset-generation", candidate, "--confirm"], paths, candidate
    raise AssertionError("unknown setup: {0}".format(setup))


def without_confirm(argv):
    return [part for part in argv if part != "--confirm"]


def read_audit(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle.read().splitlines() if line]


def read_water(path):
    db = open_rw(path)
    try:
        return {str(row[0]): int(row[1]) for row in db.read("SELECT key_id, h FROM water")}
    finally:
        db.close()


def store_bytes(paths):
    """兩庫目前內容的快照（用來證明失敗路徑「庫完全不變」）。"""
    result = {}
    for path in (paths.trust_db, paths.replay_db):
        if os.path.exists(path):
            with open(path, "rb") as handle:
                result[path] = handle.read()
    return result


# --- C-823：root 與 --confirm -------------------------------------------------


@pytest.mark.parametrize("setup", SETUPS)
def test_C823_write_requires_root_and_confirmation(box, setup):
    """C-823：非 root 與缺 `--confirm` 都是結束碼 2，且不寫審計。"""
    argv, paths, _ = prepare(box, setup)
    assert run(box, argv)[0] == 2                       # 非 root
    assert run(box, without_confirm(argv), root=True)[0] == 2
    assert not os.path.exists(paths.audit_log)


def test_C823_admin_error_exit_code_hint_and_owner_error_are_printed(box):
    """C-823：錯誤照 `AdminError.exit_code`、印 `hint` 與 `owner_error`。"""
    failure = AdminError("owner_failed", "庫的擁有者無法設成指定帳號", exit_code=1,
                         hint="檢查是否以 root 執行、以及該帳號是否存在")
    failure.owner_error = AdminError(
        "owner_failed", "檔案的擁有者無法設成指定帳號", exit_code=1)
    from lgwd import auth_admin
    with mock.patch.object(auth_admin, "init_stores", side_effect=failure):
        rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 1, out + err
    assert "檢查是否以 root 執行" in err
    assert "owner_error" in err
    assert "檔案的擁有者無法設成指定帳號" in err


def test_C823_admin_error_exit_code_2_is_preserved(box):
    """C-823：`AdminError.exit_code=2`（無法執行）不能被吞成 1。"""
    failure = AdminError("invalid_argument", "必須剛好指定 --key 或 --all-future 其中一個",
                         exit_code=2)
    from lgwd import auth_admin
    seed_stores(box, registry_bytes())
    with mock.patch.object(auth_admin, "reset_key_water", side_effect=failure):
        rc, out, err = run(box, ["auth-replay", "reset-key-water", "--all-future",
                                 "--confirm"], root=True)
    assert rc == 2, out + err
    assert "必須剛好指定" in err


# --- C-824：store flock -------------------------------------------------------


@pytest.mark.parametrize("setup", SETUPS)
def test_C824_write_refuses_held_store_lock_and_changes_nothing(box, setup):
    """C-824：lgwd 持有 store 鎖時回 2，兩庫位元組完全不變，也不寫審計。"""
    argv, paths, _ = prepare(box, setup)
    before = store_bytes(paths)
    lock = acquire_store_lock(paths.store_lock)
    try:
        rc, out, err = run(box, argv, root=True)
        assert rc == 2, out + err
    finally:
        lock.release()
    assert store_bytes(paths) == before
    assert not os.path.exists(paths.audit_log)


# --- C-818：auth-store init ---------------------------------------------------


def test_C818_init_audit_permissions_and_existing_refusal(box):
    """C-818：init 建兩庫與鎖（0600）、先寫審計、已存在時回 1。"""
    rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 0, out + err
    paths = sandbox_paths(box)
    assert all(stat.S_IMODE(os.stat(p).st_mode) == 0o600
               for p in (paths.trust_db, paths.replay_db, paths.store_lock))
    entries = read_audit(paths.audit_log)
    assert entries[0]["command"] == "auth-store init"
    assert set(entries[0]) == AUDIT_FIELDS
    assert "identity" in out
    rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 1, out + err
    assert "已存在" in err
    assert "recover-store" in err or "rebuild-trust" in err


def test_C818_init_refuses_when_one_store_already_exists(box):
    """C-818：只缺一個也不能用 init（O_EXCL；不可覆寫）。"""
    paths = sandbox_paths(box)
    create_store(paths.trust_db, TRUST_SCHEMA, os.urandom(16).hex())
    rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 1, out + err
    assert "已存在" in err
    assert not os.path.exists(paths.replay_db)


def test_C818_init_warns_when_audit_log_has_records(box):
    """C-818：審計已有紀錄時明示「回退防護已失效」（只警示、不擋）。"""
    paths = sandbox_paths(box)
    with open(paths.audit_log, "w", encoding="utf-8") as handle:
        handle.write('{"command":"old"}\n')
    rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 0, out + err
    assert "回退防護已失效" in out


def test_C818_init_warns_when_registry_generation_is_above_one(box):
    """C-818：名冊世代 > 1 時同樣明示「回退防護已失效」。"""
    paths = sandbox_paths(box)
    write_registry_file(paths.registry, registry_bytes(5))
    rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 0, out + err
    assert "回退防護已失效" in out


# --- C-819：auth-replay recover-store ----------------------------------------


def test_C819_recover_missing_trust_is_exit_1_pointing_rebuild(box):
    """C-819：信任庫不在時不能只重建 replay（回 1，指向 rebuild-trust）。"""
    rc, out, err = run(box, ["auth-replay", "recover-store", "--confirm"], root=True)
    assert rc == 1, out + err
    assert "rebuild-trust" in err


def test_C819_recover_replaces_broken_replay_and_sets_h(box):
    """C-819：壞掉的 replay 改名 `.broken-<時刻>`、新庫用信任庫識別、H＝now＋60。"""
    loaded = registry_bytes(5, keys=[key(), key("w1-tunnel-b", raw=VALID_B, status="revoked")])
    paths = seed_stores(box, loaded)
    with open(paths.replay_db, "wb") as handle:
        handle.write(b"this is not a database")
    before = int(time.time())
    rc, out, err = run(box, ["auth-replay", "recover-store", "--confirm"], root=True)
    assert rc == 0, out + err
    assert "60 秒" in out
    broken = list(Path(paths.state_dir).glob("auth-replay.sqlite.broken-*"))
    assert len(broken) == 1, os.listdir(paths.state_dir)
    water = read_water(paths.replay_db)
    assert set(water) == {"w1-tunnel-a", "w1-tunnel-b"}   # 含 revoked
    for value in water.values():
        assert before + 60 - 5 <= value <= before + 60 + 5
    db = open_rw(paths.replay_db)
    try:
        replay_identity = db.read_identity()
    finally:
        db.close()
    db = open_rw(paths.trust_db)
    try:
        trust_identity = db.read_identity()
    finally:
        db.close()
    assert replay_identity == trust_identity


def test_C819_recover_replaces_when_identity_differs(box):
    """C-819：replay 可讀但庫識別與信任庫不同時，也改名重建（不沿用錯的庫）。"""
    paths = seed_stores(box, registry_bytes())
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(paths.replay_db + suffix):
            os.unlink(paths.replay_db + suffix)
    create_store(paths.replay_db, REPLAY_SCHEMA, "1" * 32)
    rc, out, err = run(box, ["auth-replay", "recover-store", "--confirm"], root=True)
    assert rc == 0, out + err
    assert list(Path(paths.state_dir).glob("auth-replay.sqlite.broken-*"))
    db = open_rw(paths.replay_db)
    try:
        identity = db.read_identity()
    finally:
        db.close()
    db = open_rw(paths.trust_db)
    try:
        trust_identity = db.read_identity()
    finally:
        db.close()
    assert identity == trust_identity


def test_C819_recover_refuses_a_healthy_replay_store(box):
    """C-819：健康且識別相同的 replay 不重建（回 1）；nonce 與 H 不白丟。"""
    paths = seed_stores(box, registry_bytes())
    before = store_bytes(paths)
    rc, out, err = run(box, ["auth-replay", "recover-store", "--confirm"], root=True)
    assert rc == 1, out + err
    assert "不需" in err
    assert store_bytes(paths) == before


# --- C-820：auth-replay reset-key-water --------------------------------------


def test_C820_preview_prints_current_h_changes_nothing_and_returns_2(box):
    """C-820（#274 fix1 中-1）：沒有 `--confirm` 是唯讀預覽——印 H、不改庫、回 2。"""
    paths = seed_stores(box, registry_bytes())
    old = int(time.time()) - 1000
    set_water(paths.replay_db, "w1-tunnel-a", old)
    rc, out, err = run(box, ["auth-replay", "reset-key-water", "--key", "w1-tunnel-a"],
                       root=True)
    assert rc == 2, out + err
    assert "server_time" in out
    assert "key w1-tunnel-a h {0}".format(old) in out
    assert "簽章" in out                              # 代價要印
    assert "確認時鐘已校正後加 --confirm" in out
    assert read_water(paths.replay_db)["w1-tunnel-a"] == old   # 庫沒改
    assert not os.path.exists(paths.audit_log)        # 預覽不寫審計


def test_C820_preview_all_future_lists_only_future_keys(box):
    """C-820：`--all-future` 的預覽只列 H 在未來的那幾把。"""
    paths = seed_stores(box, registry_bytes())
    now = int(time.time())
    set_water(paths.replay_db, "w1-tunnel-a", now + 3600)
    set_water(paths.replay_db, "w1-tunnel-b", now - 500)
    rc, out, err = run(box, ["auth-replay", "reset-key-water", "--all-future"], root=True)
    assert rc == 2, out + err
    assert "key w1-tunnel-a h {0}".format(now + 3600) in out
    assert "key w1-tunnel-b" not in out
    assert read_water(paths.replay_db)["w1-tunnel-a"] == now + 3600


def test_C820_execute_prints_old_h_to_new_h(box):
    """C-820（#274 fix1 中-1）：帶 `--confirm` 時印每把 key 的舊 H -> 新 H。"""
    paths = seed_stores(box, registry_bytes())
    old = int(time.time()) - 1000
    set_water(paths.replay_db, "w1-tunnel-a", old)
    before = int(time.time())
    rc, out, err = run(box, ["auth-replay", "reset-key-water", "--key", "w1-tunnel-a",
                             "--confirm"], root=True)
    assert rc == 0, out + err
    water = read_water(paths.replay_db)
    assert before - 65 - 5 <= water["w1-tunnel-a"] <= before - 65 + 5
    assert "server_time" in out
    assert "key w1-tunnel-a h {0} -> {1}".format(old, water["w1-tunnel-a"]) in out
    assert "簽章" in out            # 代價必須印


def test_C820_all_future_only_touches_keys_above_now_plus_60(box):
    """C-820：`--all-future` 只動 H 在未來的 key。"""
    paths = seed_stores(box, registry_bytes())
    now = int(time.time())
    set_water(paths.replay_db, "w1-tunnel-a", now + 3600)
    set_water(paths.replay_db, "w1-tunnel-b", now - 500)
    rc, out, err = run(box, ["auth-replay", "reset-key-water", "--all-future",
                             "--confirm"], root=True)
    assert rc == 0, out + err
    water = read_water(paths.replay_db)
    assert water["w1-tunnel-a"] <= now
    assert water["w1-tunnel-b"] == now - 500


def test_C820_unknown_key_is_exit_1_and_changes_nothing(box):
    """C-820：找不到 key 回 1（預覽與執行都一樣），什麼都不改。"""
    paths = seed_stores(box, registry_bytes())
    set_water(paths.replay_db, "w1-tunnel-a", 1000)
    preview = run(box, ["auth-replay", "reset-key-water", "--key", "no-such-key"], root=True)
    assert preview[0] == 1, preview[1] + preview[2]
    assert "auth-replay status" in preview[2]
    rc, out, err = run(box, ["auth-replay", "reset-key-water", "--key", "no-such-key",
                             "--confirm"], root=True)
    assert rc == 1, out + err
    assert read_water(paths.replay_db) == {"w1-tunnel-a": 1000}
    assert not os.path.exists(paths.audit_log)


def test_C820_key_and_all_future_are_mutually_exclusive():
    """C-820：`--key` 與 `--all-future` 必須剛好給一個（argparse 回 2）。"""
    parser = build_parser()
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(["auth-replay", "reset-key-water", "--confirm"])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as both:
        parser.parse_args(["auth-replay", "reset-key-water", "--confirm",
                           "--all-future", "--key", "w1-tunnel-a"])
    assert both.value.code == 2


# --- C-821：auth-registry rebuild-trust --------------------------------------


def test_C821_rebuild_prints_rollback_warning_and_rebuilds(box):
    """C-821：信任庫缺失時重建，必印「回退防護已失效」。"""
    paths = seed_stores(box, registry_bytes(5))
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(paths.trust_db + suffix):
            os.unlink(paths.trust_db + suffix)
    candidate = candidate_file(box, registry_bytes(5))
    rc, out, err = run(box, ["auth-registry", "rebuild-trust", candidate, "--confirm"],
                       root=True)
    assert rc == 0, out + err
    assert "回退防護已失效" in out
    assert os.path.exists(paths.trust_db)
    assert "接著跑 auth-replay recover-store" not in out   # replay 健康、識別沿用


def test_C821_rebuild_refuses_a_healthy_trust_store(box):
    """C-821：健康的信任庫不是 rebuild 的對象（回 1，指向 reset-generation／install）。"""
    paths = seed_stores(box, registry_bytes(5))
    before = store_bytes(paths)
    candidate = candidate_file(box, registry_bytes(6))
    rc, out, err = run(box, ["auth-registry", "rebuild-trust", candidate, "--confirm"],
                       root=True)
    assert rc == 1, out + err
    assert "不需要" in err
    assert store_bytes(paths) == before


def test_C821_rebuild_points_to_recover_store_when_replay_is_unusable(box):
    """C-821：replay 也壞了時，重建出新識別並印「接著跑 auth-replay recover-store」。"""
    paths = seed_stores(box, registry_bytes(5))
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(paths.trust_db + suffix):
            os.unlink(paths.trust_db + suffix)
    with open(paths.replay_db, "wb") as handle:
        handle.write(b"this is not a database")
    candidate = candidate_file(box, registry_bytes(5))
    rc, out, err = run(box, ["auth-registry", "rebuild-trust", candidate, "--confirm"],
                       root=True)
    assert rc == 0, out + err
    assert "回退防護已失效" in out
    assert "接著跑 auth-replay recover-store" in out
    # 修好 replay 之後才識別一致（W-D12 的後半）。
    assert run(box, ["auth-replay", "recover-store", "--confirm"], root=True)[0] == 0


def test_C821_rebuild_missing_registry_file_is_exit_1(box):
    """C-821：名冊讀不到（io_error）是結果不合格 1，不是無法執行 2。"""
    seed_stores(box, registry_bytes(5))
    rc, out, err = run(box, ["auth-registry", "rebuild-trust",
                             os.path.join(box.root, "nope.json"), "--confirm"], root=True)
    assert rc == 1, out + err


# --- C-822：auth-registry reset-generation -----------------------------------


def test_C822_reset_generation_writes_registry_and_is_idempotent(box):
    """C-822：降到已核驗的較低世代；重跑同一指令仍成功（W-D7）。"""
    seed_stores(box, registry_bytes(5))
    candidate = candidate_file(box, registry_bytes(2))
    rc, out, err = run(box, ["auth-registry", "reset-generation", candidate, "--confirm"],
                       root=True)
    assert rc == 0, out + err
    assert "5 → 2" in out
    with open(sandbox_paths(box).registry, "rb") as handle:
        assert handle.read() == registry_bytes(2)
    rc, out, err = run(box, ["auth-registry", "reset-generation", candidate, "--confirm"],
                       root=True)
    assert rc == 0, out + err
    assert "2 → 2" in out


def test_C822_tombstone_violation_changes_nothing(box):
    """C-822：名冊讓已撤銷 key 復活時什麼都不改（W-D8）。"""
    revoked = registry_bytes(5, keys=[key(status="revoked")])
    paths = seed_stores(box, revoked)
    before = store_bytes(paths)
    candidate = candidate_file(box, registry_bytes(6, keys=[key()]), "active-again.json")
    rc, out, err = run(box, ["auth-registry", "reset-generation", candidate, "--confirm"],
                       root=True)
    assert rc == 1, out + err
    assert "墓碑" in out + err or "tombstone" in out + err
    assert not os.path.exists(paths.registry)
    assert store_bytes(paths) == before


# --- C-825：鎖順序與「只取這兩把」 -------------------------------------------


def test_C825_reset_generation_waits_for_install_lock(box):
    """C-825：名冊 install 鎖被持有時回 2（store flock 之後才取 install 鎖）。"""
    paths = seed_stores(box, registry_bytes(5))
    candidate = candidate_file(box, registry_bytes(2))
    lock = acquire_store_lock(os.path.join(paths.state_dir, "auth-registry-install.lock"))
    try:
        rc, out, err = run(box, ["auth-registry", "reset-generation", candidate,
                                 "--confirm"], root=True)
        assert rc == 2, out + err
        assert not os.path.exists(paths.registry)
    finally:
        lock.release()


def test_C825_write_commands_create_no_other_locks(box):
    """C-824／C-825：寫入只取 store flock 與（reset-generation）install 鎖。"""
    paths = seed_stores(box, registry_bytes(5))
    candidate = candidate_file(box, registry_bytes(6))
    rc, out, err = run(box, ["auth-registry", "reset-generation", candidate, "--confirm"],
                       root=True)
    assert rc == 0, out + err
    locks = sorted(name for name in os.listdir(paths.state_dir) if name.endswith(".lock"))
    assert locks == ["auth-registry-install.lock", "auth-store.lock"]


def test_C825_install_lock_is_root_owned_only_outside_prefix(box):
    """C-825／agy 低-1：install 鎖在正式機補 `root:root`；prefix（測試）不 chown。"""
    from lgwd import auth_admin

    paths = seed_stores(box, registry_bytes(5))
    candidate = candidate_file(box, registry_bytes(6))
    prefix_calls = []
    with mock.patch("lgwd.auth_admin.os.fchown",
                    side_effect=lambda fd, uid, gid: prefix_calls.append((uid, gid))):
        rc, out, err = run(box, ["auth-registry", "reset-generation", candidate, "--confirm"],
                           root=True)
    assert rc == 0, out + err
    assert prefix_calls == []

    # 正式機路徑（install_lock_root=True）：鎖檔補 root:root；用程式庫直接呼叫。
    higher = candidate_file(box, registry_bytes(7), "candidate-7.json")
    calls = []
    with mock.patch("lgwd.auth_admin.os.fchown",
                    side_effect=lambda fd, uid, gid: calls.append((uid, gid))):
        auth_admin.reset_generation(
            paths, higher, clock=SystemClock(),
            audit=AuditLog(paths.audit_log, clock=SystemClock()),
            owner_uid=os.getuid(), install_lock_root=True)
    assert (0, 0) in calls, calls


# --- C-826：AuditLog ----------------------------------------------------------


@pytest.mark.parametrize("setup", SETUPS)
def test_C826_every_write_uses_the_auditlog_shape(box, setup):
    """C-826：每個寫入指令的審計都是 `AuditLog` 的七欄形狀（不是另一套格式）。"""
    argv, paths, _ = prepare(box, setup)
    rc, out, err = run(box, argv, root=True)
    assert rc == 0, out + err
    entries = read_audit(paths.audit_log)
    assert entries, out
    for entry in entries:
        assert set(entry) == AUDIT_FIELDS
    assert stat.S_IMODE(os.stat(paths.audit_log).st_mode) == 0o600
    assert any(entry["command"] == AUDIT_COMMANDS[setup] for entry in entries), entries


def test_C826_audit_never_contains_public_keys(box):
    """C-826：審計只記世代、雜湊、key ID 與 H；公鑰行不進審計。"""
    argv, paths, _ = prepare(box, "reset-generation")
    rc, out, err = run(box, argv, root=True)
    assert rc == 0, out + err
    text = Path(paths.audit_log).read_text(encoding="utf-8")
    assert "ssh-ed25519" not in text
    assert "public_key" not in text


def test_C826_journal_uses_ident_and_authpriv_facility(box):
    """C-826／低-1：寫 journal 前先 `openlog("llmgwctl-auth", 0, LOG_AUTHPRIV)`。"""
    from lgwd import auth_admin

    paths = sandbox_paths(box)
    fake = mock.Mock()
    with mock.patch.object(auth_admin, "syslog", fake):
        AuditLog(paths.audit_log, clock=SystemClock()).append("auth-store init")
    fake.openlog.assert_called_once_with("llmgwctl-auth", 0, fake.LOG_AUTHPRIV)
    fake.syslog.assert_called_once()
    assert fake.syslog.call_args[0][0] is fake.LOG_NOTICE


# --- C-827：權限收尾與 G-6 ---------------------------------------------------


@pytest.mark.parametrize("setup", SETUPS)
def test_C827_no_sidecars_correct_modes_and_stores_are_reopenable(box, setup):
    """C-827：成功後沒有 `-wal`／`-shm`、庫與鎖 0600、lgwd 的開庫／取鎖路徑可用。"""
    argv, paths, _ = prepare(box, setup)
    rc, out, err = run(box, argv, root=True)
    assert rc == 0, out + err
    state = Path(paths.state_dir)
    assert not list(state.glob("*.sqlite-wal"))
    assert not list(state.glob("*.sqlite-shm"))
    for path in (paths.trust_db, paths.replay_db, paths.store_lock):
        info = os.stat(path)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.getuid()      # prefix 模式不 chown
    trust = open_rw(paths.trust_db)
    replay = open_rw(paths.replay_db)
    try:
        assert check_store_identity(trust, replay)   # lgwd 的 _open_stores 同一判斷
    finally:
        trust.close()
        replay.close()
    lock = acquire_store_lock(paths.store_lock)     # lgwd 啟動的第一步
    lock.release()


@pytest.mark.parametrize("setup", SETUPS)
def test_C827_prefix_mode_never_chowns(box, setup):
    """C-827：測試／prefix 模式沒有服務帳號，不得嘗試 chown（建庫後也不得 chown）。"""
    argv, paths, _ = prepare(box, setup)
    calls = []
    with mock.patch("lgwd.auth_admin.os.chown",
                    side_effect=lambda path, uid, gid: calls.append((path, uid, gid))):
        rc, out, err = run(box, argv, root=True)
    assert rc == 0, out + err
    assert calls == []


def test_C827_init_stores_chowns_stores_and_lock_when_owner_is_given(box):
    """C-827：給了 `owner` 時（正式機）兩庫與鎖都 chown；prefix 由 CLI 傳 None。"""
    paths = sandbox_paths(box)
    calls = []
    with mock.patch("lgwd.auth_admin.os.chown",
                    side_effect=lambda p, u, g: calls.append((p, u, g))):
        init_stores(paths, clock=SystemClock(),
                    audit=AuditLog(paths.audit_log, clock=SystemClock()), owner=(1234, 2345))
    assert {(p, u, g) for p, u, g in calls} >= {
        (p, 1234, 2345) for p in (paths.trust_db, paths.replay_db, paths.store_lock)}


def test_C827_lgwd_can_start_after_init(box):
    """C-827／G-6：`auth-store init` 之後 lgwd 的啟動路徑取得鎖、開得了兩庫。

    用真的 `lgwd.auth.open_runtime`（warn 模式；測試機沒有 llmgw 帳號，`owner_uid`
    用目前使用者）。這正是 G-6「init 之後以 llmgw 啟動 lgwd 能取得鎖」的程式路徑。
    """
    from lgwd import auth

    rc, out, err = run(box, ["auth-store", "init", "--confirm"], root=True)
    assert rc == 0, out + err
    paths = sandbox_paths(box)
    write_registry_file(paths.registry, registry_bytes(1))
    runtime = auth.open_runtime(
        auth.AuthConfig(mode="warn", registry_path=paths.registry,
                        state_dir=paths.state_dir, poll_interval=0.0),
        SystemClock(), owner_uid=os.getuid(), registry_owner_uid=os.getuid())
    try:
        assert runtime.lock is not None
        assert runtime.trust is not None
        assert runtime.replay is not None
    finally:
        runtime.stop()
    lock = acquire_store_lock(paths.store_lock)     # 停止後鎖已放開
    lock.release()


# --- C-828：check 三項 --------------------------------------------------------


def make_layout(box, mode="enforce", *, euid=0):
    box.write(box.etc("llmgateway.env"), 'LGWD_AUTH_MODE="{0}"\n'.format(mode))
    with mock.patch("os.geteuid", return_value=euid):
        return Layout.from_env(box.env)


def run_auth_items(layout):
    report = Report(out=lambda line: None)
    with mock.patch("os.geteuid", return_value=layout.euid):
        check._check_auth_stores(layout, report)
    return report


def test_C828_heartbeat_fresh_is_ok_and_stale_is_fail(box):
    """C-828：心跳在 180 秒內 ok；181 秒 fail；沒有 status 檔 fail。"""
    paths = seed_stores(box, registry_bytes())
    layout = make_layout(box)
    write_status(box, status_document(heartbeat=int(time.time())))
    assert run_auth_items(layout).level_of("auth-heartbeat") == "ok"
    write_status(box, status_document(heartbeat=int(time.time()) - 181))
    assert run_auth_items(layout).level_of("auth-heartbeat") == "fail"
    os.unlink(paths.status)
    assert run_auth_items(layout).level_of("auth-heartbeat") == "fail"


def test_C828_water_future_is_fail_and_stores_ok(box):
    """C-828：H 在未來（now＋60）的 key 讓 `auth-water` fail；健康庫是 ok。"""
    paths = seed_stores(box, registry_bytes())
    layout = make_layout(box)
    write_status(box, status_document())
    set_water(paths.replay_db, "w1-tunnel-a", int(time.time()) + 3600)
    report = run_auth_items(layout)
    assert report.level_of("auth-water") == "fail"
    assert "w1-tunnel-a" in " ".join(report.lines)
    assert report.level_of("auth-stores") == "ok"


def test_C828_stores_missing_or_mismatched_is_fail(box):
    """C-828：replay 庫缺失 → `auth-stores` fail；識別不符也 fail。"""
    paths = seed_stores(box, registry_bytes())
    layout = make_layout(box)
    write_status(box, status_document())
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(paths.replay_db + suffix):
            os.unlink(paths.replay_db + suffix)
    report = run_auth_items(layout)
    assert report.level_of("auth-stores") == "fail"
    assert report.level_of("auth-water") == "fail"       # 庫也不見了
    create_store(paths.replay_db, REPLAY_SCHEMA, "0" * 32)
    report = run_auth_items(layout)
    assert report.level_of("auth-stores") == "fail"
    assert report.level_of("auth-water") == "ok"


def test_C828_non_root_water_and_stores_are_todo_not_fail(box):
    """C-828：非 root 讀不到 llmgw 0600 的庫 → 後兩項 todo。"""
    seed_stores(box, registry_bytes())
    write_status(box, status_document())
    report = run_auth_items(make_layout(box, euid=1000))
    assert report.level_of("auth-heartbeat") == "ok"
    assert report.level_of("auth-water") == "todo"
    assert report.level_of("auth-stores") == "todo"


def test_C828_off_mode_is_ok_and_unknown_mode_is_todo(box):
    """C-828：off 三項 ok（未啟用）；模式讀不懂三項 todo（不假裝 ok）。"""
    report = run_auth_items(make_layout(box, mode="off"))
    for name in ("auth-heartbeat", "auth-water", "auth-stores"):
        assert report.level_of(name) == "ok"
    report = run_auth_items(make_layout(box, mode="bogus"))
    for name in ("auth-heartbeat", "auth-water", "auth-stores"):
        assert report.level_of(name) == "todo"


def test_C828_unreadable_mode_is_todo(box):
    """C-828：`llmgateway.env` 讀不到時三項 todo（不能當成 off 的 ok）。"""
    layout = make_layout(box)
    with mock.patch("lgwctl.layout.Layout.auth_mode", return_value="unreadable"):
        report = run_auth_items(layout)
    for name in ("auth-heartbeat", "auth-water", "auth-stores"):
        assert report.level_of(name) == "todo"


def test_C828_check_is_read_only_and_creates_no_files(box):
    """C-828／C-829：健康庫與未正常關閉的庫都只讀——目錄檔案清單前後相同。"""
    paths = seed_stores(box, registry_bytes())
    layout = make_layout(box)
    write_status(box, status_document())
    before = sorted(os.listdir(paths.state_dir))
    report = run_auth_items(layout)
    assert report.level_of("auth-stores") == "ok"
    assert sorted(os.listdir(paths.state_dir)) == before
    assert not os.path.exists(paths.replay_db + "-wal")
    assert not os.path.exists(paths.replay_db + "-shm")

    before = make_crashed_replay(paths.replay_db, with_shm=False)
    report = run_auth_items(layout)
    assert report.level_of("auth-water") == "fail"
    assert "未正常關閉" in " ".join(report.lines)
    assert sorted(os.listdir(paths.state_dir)) == before
    assert not os.path.exists(paths.replay_db + "-shm")


def test_C828_check_run_lists_the_three_items(box):
    """C-828：`check.run` 真的會列出三項（不是只有測試直接呼叫的函式）。"""
    box.install_state()  # rolesync validates against the installed policy
    seed_stores(box, registry_bytes())
    write_status(box, status_document())
    # #312：auth-busy 要一份新鮮的計數檔（沒有就是 todo，rc 會變 2）。
    box.write(sandbox_paths(box).busy_status, json.dumps({
        "schema": 1, "mode": "enforce", "since": int(time.time()) - 3600,
        "updated": int(time.time()), "store_busy_total": 0,
        "store_busy_30m": 0, "store_busy_60m": 0}, separators=(",", ":")), 0o644)
    layout = make_layout(box)
    report = Report(out=lambda line: None)
    with mock.patch.object(check, "_check_units", lambda *a, **k: (False, "missing")), \
            mock.patch.object(check, "_check_list_local", lambda *a, **k: None), \
            mock.patch.object(check, "_check_auth_local", lambda *a, **k: None), \
            mock.patch.object(check, "_check_keysync", lambda *a, **k: None), \
            mock.patch.object(check, "_check_policy", lambda *a, **k: None), \
            mock.patch.object(check, "_check_gh_repo", lambda *a, **k: None):
        rc = check.run(layout, report=report, no_remote=True)
    for name in ("auth-heartbeat", "auth-water", "auth-stores", "auth-busy"):
        assert report.level_of(name) == "ok", report.lines
    assert rc == 0, report.lines


# --- C-829：未正常關閉的庫先回收 WAL -----------------------------------------


@pytest.mark.parametrize("db_name,command", [
    ("trust_db", ["auth-registry", "rebuild-trust", "{candidate}", "--confirm"]),
    ("replay_db", ["auth-replay", "recover-store", "--confirm"]),
])
def test_C829_unclean_wal_is_recovered_not_renamed(box, db_name, command):
    """C-829：有 `-wal` 無 `-shm` 時持鎖以 `open_rw` 回收，不把健康庫當損毀改名。"""
    paths = seed_stores(box, registry_bytes(5))
    candidate = candidate_file(box, registry_bytes(5))
    dbpath = getattr(paths, db_name)
    script = ("import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); "
              "c.execute('PRAGMA journal_mode=WAL'); "
              "c.execute('CREATE TABLE wal_probe (x)'); c.commit(); os._exit(0)")
    subprocess.run([sys.executable, "-c", script, dbpath], check=True)
    os.unlink(dbpath + "-shm")
    argv = [candidate if part == "{candidate}" else part for part in command]
    rc, out, err = run(box, argv, root=True)
    # 回收後庫是健康的：兩個指令都應該拒絕（1），而不是重建。
    assert rc == 1, out + err
    assert "不需" in err
    assert not list(Path(paths.state_dir).glob(Path(dbpath).name + ".broken-*"))
    assert not os.path.exists(dbpath + "-shm")


# --- C-829（#274 fix1 中-2）：損毀＋未正常關閉不得擋下修復 --------------------


def make_corrupt_unclean(path: str) -> None:
    """把庫寫成損毀，再放一個 `-wal`、不放 `-shm`（上次異常結束＋壞檔）。"""
    with open(path, "wb") as handle:
        handle.write(b"this is not a database")
    with open(path + "-wal", "wb") as handle:
        handle.write(b"not a real wal")
    if os.path.exists(path + "-shm"):
        os.unlink(path + "-shm")


@pytest.mark.parametrize("db_name,command", [
    ("trust_db", ["auth-registry", "rebuild-trust", "{candidate}", "--confirm"]),
    ("replay_db", ["auth-replay", "recover-store", "--confirm"]),
])
def test_C829_corrupt_and_unclean_is_renamed_with_evidence_and_rebuilt(box, db_name, command):
    """C-829（#274 fix1 中-2）：損毀＋unclean 不中止；連 `-wal` 改名留證後重建成功。"""
    paths = seed_stores(box, registry_bytes(5))
    candidate = candidate_file(box, registry_bytes(5))
    dbpath = getattr(paths, db_name)
    make_corrupt_unclean(dbpath)
    argv = [candidate if part == "{candidate}" else part for part in command]
    rc, out, err = run(box, argv, root=True)
    assert rc == 0, out + err
    renamed = list(Path(paths.state_dir).glob(Path(dbpath).name + ".broken-*"))
    broken = [p for p in renamed if not str(p).endswith(("-wal", "-shm"))]
    assert len(broken) == 1, renamed                # 主檔一個
    assert os.path.exists(str(broken[0]) + "-wal")  # -wal 一起留證
    assert not os.path.exists(dbpath + "-wal")
    # 重建後的兩庫健康（識別一致）：再用 recover-store 會被拒絕（1）。
    rc, out, err = run(box, ["auth-replay", "recover-store", "--confirm"], root=True)
    assert rc == 1, out + err
    assert "不需" in err
