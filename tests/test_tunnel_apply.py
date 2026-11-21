"""llmgwctl tunnel-apply（#227；tasks 3.2、契約 C-10）的測試。

外部指令全是假的：假 systemctl（lgwctl_fakes）、假 systemd-run（下面）、假探針
（放在當版的 ops-scripts/llmgateway-tunnel）。假 systemd-run：

- ``--on-active=`` ＝排 transient timer：只記下來（並記「排的當下 tunnel.env 是新是舊、
  備份在不在」），把 ``<unit>.timer`` 標成 active；``sr_timer_fail`` 存在就回 1；
- 其他＝起 transient unit：真的在背景（``setsid``，``sr_async`` 存在時）或同步地跑
  後面的指令，環境多一個 ``INVOCATION_ID``；``sr_launch_fail`` 存在就回 1。
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time

import pytest

from lgwctl import tunnel_apply
from lgwctl.layout import Layout
from tests.lgwctl_fakes import FAKE_TAG, REPO_ROOT, Sandbox

FAKE_SYSTEMD_RUN = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
printf 'systemd-run %s\\n' "$*" >> "${fake}/order.log"
unit=""; timer=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --unit=*) unit="${1#--unit=}" ;;
        --on-active=*) timer="${1#--on-active=}" ;;
        --collect|--setenv=*|--working-directory=*) ;;
        *) break ;;
    esac
    shift
done
if [[ -n "$timer" ]]; then
    [[ -f "${fake}/sr_timer_fail" ]] && exit 1
    envf="${LLMGW_PREFIX}/etc/llmgateway/tunnel.env"
    new=0; grep -q NEWMARK "$envf" 2>/dev/null && new=1
    bak=0; [[ -f "${LLMGW_PREFIX}/var/lib/llmgateway-ctl/tunnel-apply.bak/manifest.json" ]] && bak=1
    printf 'on-active %s new-env-at-schedule=%s backup-at-schedule=%s\\n' "$timer" "$new" "$bak" >> "${fake}/timers.log"
    mkdir -p "${fake}/active"
    : > "${fake}/active/${unit}.timer"
    exit 0
fi
[[ -f "${fake}/sr_launch_fail" ]] && exit 1
cd "${LGW_FAKE_REPO:?}"
if [[ -f "${fake}/sr_async" ]]; then
    INVOCATION_ID=fake-invocation setsid "$@" > "${fake}/unit-${unit}.log" 2>&1 < /dev/null &
    exit 0
fi
INVOCATION_ID=fake-invocation "$@" > "${fake}/unit-${unit}.log" 2>&1 < /dev/null
exit 0
"""

FAKE_PROBE = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
printf 'probe %s env=%s,%s\\n' "$*" "${PROBE_INTERVAL:-}" "${FETCH_MIN_INTERVAL:-}" >> "${fake}/probe.log"
n=0
while [[ -f "${fake}/probe_gate" && $n -lt 600 ]]; do sleep 0.05; n=$((n+1)); done
rcs="${fake}/probe_rcs"
rc="${FAKE_PROBE_RC:-0}"
if [[ -s "$rcs" ]]; then
    rc="$(head -n 1 "$rcs")"
    if [[ "$(wc -l < "$rcs")" -gt 1 ]]; then tail -n +2 "$rcs" > "${rcs}.n"; mv "${rcs}.n" "$rcs"; fi
fi
exit "$rc"
"""

OLD_ENV = "OLDMARK=1\nRENDEZVOUS=rv.example\nSECRET_TOKEN=SENTINEL_OLD_TOKEN_91\n"
NEW_ENV = "NEWMARK=1\nRENDEZVOUS=rv.example\nSECRET_TOKEN=SENTINEL_NEW_TOKEN_92\n"
OLD_DROPIN = "[Service]\nLoadCredential=old:/etc/llmgateway/SENTINEL_OLD_CRED\n"
NEW_DROPIN = "[Service]\nLoadCredential=gateway-token:/etc/llmgateway/SENTINEL_NEW_CRED\n"
SENTINELS = ("SENTINEL_OLD_TOKEN_91", "SENTINEL_NEW_TOKEN_92", "SENTINEL_OLD_CRED",
             "SENTINEL_NEW_CRED", "SENTINEL_FOLLOW_OLD", "SENTINEL_FOLLOW_NEW")


class Box(Sandbox):
    def __init__(self):
        super().__init__()
        self.env.update({
            "LLMGW_SYSTEMD_RUN": os.path.join(self.fakebin, "systemd-run"),
            "LGW_FAKE_REPO": str(REPO_ROOT),
            "LLMGW_TUNNEL_APPLY_TEST": "1",
            "LLMGW_TUNNEL_APPLY_WAIT": "1",
            "LLMGW_TUNNEL_APPLY_INTERVAL": "0.1",
            "LLMGW_TUNNEL_APPLY_LOCK_WAIT": "1",
            "LLMGW_TUNNEL_APPLY_FOLLOW": "60",
            "PROBE_INTERVAL": "1",
            "FETCH_MIN_INTERVAL": "2",
        })
        self.write(os.path.join(self.fakebin, "systemd-run"), FAKE_SYSTEMD_RUN, 0o755)
        self.install_state(FAKE_TAG)
        self.write(os.path.join(self.current_link, "ops-scripts", "llmgateway-tunnel"),
                   FAKE_PROBE, 0o755)
        self.write(self.etc("tunnel.env"), OLD_ENV, 0o640)
        self.new_env = self.write(self.etc("tunnel.env.new"), NEW_ENV, 0o600)
        self.new_dropin = self.write(self.etc("credential.conf.new"), NEW_DROPIN, 0o600)
        self.dropin_dir = self.unit("llmgateway-tunnel.service.d")
        self.dropin = os.path.join(self.dropin_dir, "credential.conf")
        self.follow = self.state("gateway.json")
        self.known_hosts = self.state(".tunnel_known_hosts")
        self.bak = self.path("var", "lib", "llmgateway-ctl", "tunnel-apply.bak")
        self.lock = self.path("var", "lib", "llmgateway-ctl", ".tunnel-apply.lock")
        self.state_file = self.path("var", "lib", "llmgateway-ctl", ".tunnel-apply.state")

    def flag(self, name, text="1\n"):
        self.write(os.path.join(self.fake, name), text)

    def probe_rcs(self, rcs):
        self.flag("probe_rcs", "".join(f"{rc}\n" for rc in rcs))

    def apply(self, *extra, dropin=True, env=None, new_env=None):
        argv = ["tunnel-apply", "--new-env", new_env or self.new_env]
        if dropin:
            argv += ["--new-dropin", self.new_dropin]
        return self.run_cli(argv + list(extra), env=env)

    def inner_env(self):
        env = dict(self.env)
        env["INVOCATION_ID"] = "fake-invocation"
        return env

    def phase(self):
        try:
            return json.loads(self.read(self.state_file))["phase"]
        except (OSError, ValueError, KeyError):
            return None

    def timers(self):
        return self.log_lines("timers.log")

    def restarts(self):
        return [a for a in self.actions() if a == "restart llmgateway-tunnel.service"]

    def everything_visible(self, *outs):
        text = "\n".join(outs)
        for name in ("order.log", "timers.log", "probe.log", "systemctl.log", "actions.log"):
            text += "\n" + self.log(name)
        if os.path.exists(self.state_file):
            text += "\n" + self.read(self.state_file)
        for name in os.listdir(self.fake):
            if name.startswith("unit-"):
                text += "\n" + self.log(name)
        return text

    def assert_no_secrets(self, *outs):
        text = self.everything_visible(*outs)
        for sentinel in SENTINELS:
            assert sentinel not in text, sentinel


@pytest.fixture
def box():
    with Box() as b:
        yield b


def wait_for(cond, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


# -- 基本流程與次序 ------------------------------------------------------


def test_happy_path_backup_then_schedule_then_apply(box):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    rc, out, err = box.apply()
    assert rc == 0, (out, err)
    assert box.phase() == "pending-confirm"
    # 排保險的當下：備份已在、tunnel.env 還是舊的（備份早於套用、保險早於套用）。
    assert box.timers() == ["on-active 10m new-env-at-schedule=0 backup-at-schedule=1"]
    assert box.read(box.etc("tunnel.env")) == NEW_ENV
    assert box.read(box.dropin) == NEW_DROPIN
    # 以複製套用：暫存檔保留。
    assert box.read(box.new_env) == NEW_ENV and box.read(box.new_dropin) == NEW_DROPIN
    assert len(box.actions()) == 2
    assert box.actions()[0].startswith("daemon-reload")
    assert box.actions()[1] == "restart llmgateway-tunnel.service"
    assert "重開機" in out
    # 備份權限與內容（切換前的版本）
    assert stat.S_IMODE(os.stat(box.bak).st_mode) == 0o700
    for name in os.listdir(box.bak):
        assert stat.S_IMODE(os.stat(os.path.join(box.bak, name)).st_mode) == 0o600
    assert box.read(os.path.join(box.bak, "tunnel.env")) == OLD_ENV
    box.assert_no_secrets(out, err)


def test_probe_sees_tuned_intervals(box):
    rc, out, err = box.apply()
    assert rc == 0
    assert "env=1,2" in box.log("probe.log")


def test_defaults_unchanged_and_env_tunes_them(tmp_path):
    layout = Layout.from_env({"LLMGW_PREFIX": str(tmp_path)})
    assert 150 <= tunnel_apply.DEFAULT_PROBE_WAIT <= 180
    assert tunnel_apply.DEFAULT_PROBE_WAIT >= tunnel_apply.MIN_PROBE_WAIT >= 90
    assert tunnel_apply.probe_wait(layout) == tunnel_apply.DEFAULT_PROBE_WAIT
    assert tunnel_apply.probe_interval(layout) == 10.0
    tuned = Layout.from_env({"LLMGW_PREFIX": str(tmp_path), "LLMGW_TUNNEL_APPLY_TEST": "1",
                             "LLMGW_TUNNEL_APPLY_WAIT": "2", "LLMGW_TUNNEL_APPLY_INTERVAL": "0.5"})
    assert tunnel_apply.probe_wait(tuned) == 2.0 and tunnel_apply.probe_interval(tuned) == 0.5


# -- --revert-after ------------------------------------------------------


@pytest.mark.parametrize("value", ["4m", "299s", "1m", "0", "soon"])
def test_revert_after_below_floor_or_bad_is_refused(box, value):
    rc, out, err = box.apply("--revert-after", value)
    assert rc == 1
    assert "revert-after" in out
    assert box.log("order.log") == "" and not os.path.exists(box.bak)
    assert box.phase() is None
    assert box.read(box.etc("tunnel.env")) == OLD_ENV


def test_revert_after_floor_accepted_and_default_is_10m(box):
    rc, out, err = box.apply("--revert-after", "5m")
    assert rc == 0 and box.timers()[0].startswith("on-active 5m ")
    assert box.run_cli(["tunnel-apply", "--confirm"])[0] == 0
    rc, out, err = box.apply()
    assert rc == 0 and box.timers()[1].startswith("on-active 10m ")


# -- root、內部旗標、互斥 ------------------------------------------------


def test_non_root_is_refused(box, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    env = {k: v for k, v in box.env.items() if k != "LLMGW_PREFIX"}
    for argv in (["tunnel-apply", "--new-env", box.new_env], ["tunnel-apply", "--confirm"]):
        rc, out, err = box.run_cli(argv, env=env)
        assert rc == 1 and "root" in out
    assert box.log("order.log") == "" and box.actions() == []


@pytest.mark.parametrize("flag", ["--revert-inner", "--apply-inner"])
def test_inner_flags_need_invocation_id(box, flag):
    env = {k: v for k, v in box.env.items() if k != "INVOCATION_ID"}
    argv = ["tunnel-apply", flag] + (["--new-env", box.new_env] if flag == "--apply-inner" else [])
    rc, out, err = box.run_cli(argv, env=env)
    assert rc == 1 and "INVOCATION_ID" in out
    assert box.actions() == [] and box.read(box.etc("tunnel.env")) == OLD_ENV


def hold_lock(box):
    os.makedirs(os.path.dirname(box.lock), exist_ok=True)
    fd = os.open(box.lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_flock_excludes_second_run(box):
    fd = hold_lock(box)
    try:
        rc, out, err = box.apply()
        assert rc == 1 and "正在跑" in out
        assert not os.path.exists(box.bak) and box.log("order.log") == ""
        assert box.read(box.etc("tunnel.env")) == OLD_ENV
        rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
        assert rc == 1 and "進行中" in out
    finally:
        os.close(fd)
    assert box.apply()[0] == 0


def test_inner_cannot_run_while_lock_held(box):
    fd = hold_lock(box)
    try:
        rc, out, err = box.run_cli(["tunnel-apply", "--apply-inner", "--new-env", box.new_env],
                                   env=box.inner_env())
        assert rc == 1 and "lock" in out
        assert box.read(box.etc("tunnel.env")) == OLD_ENV
        assert box.log("order.log") == "" and box.actions() == []
        rc, out, err = box.run_cli(["tunnel-apply", "--revert-inner"], env=box.inner_env())
        assert rc == 1 and "lock" in out
    finally:
        os.close(fd)


def test_second_apply_refused_while_pending_then_allowed_after_confirm(box):
    assert box.apply()[0] == 0
    rc, out, err = box.apply()
    assert rc == 1 and "pending-confirm" in out
    assert box.run_cli(["tunnel-apply", "--confirm"])[0] == 0
    assert box.apply()[0] == 0


# -- C-10(b)：排程失敗即中止、不套用 ------------------------------------


def test_timer_failure_aborts_without_applying(box):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    box.flag("sr_timer_fail")
    rc, out, err = box.apply()
    assert rc == 1
    assert box.phase() == "aborted"
    assert "不套用" in box.read(os.path.join(box.fake, "unit-lgw-tunnel-apply.log"))
    assert box.read(box.etc("tunnel.env")) == OLD_ENV
    assert not os.path.exists(box.dropin) and not os.path.isdir(box.dropin_dir)
    assert box.read(box.follow) == "SENTINEL_FOLLOW_OLD"
    assert box.actions() == [] and box.timers() == []
    assert box.read(box.new_env) == NEW_ENV
    box.assert_no_secrets(out, err)


def test_timer_failure_leaves_existing_dropin_untouched(box):
    box.write(box.dropin, OLD_DROPIN)
    box.flag("sr_timer_fail")
    assert box.apply()[0] == 1
    assert box.read(box.dropin) == OLD_DROPIN and box.read(box.etc("tunnel.env")) == OLD_ENV


def test_launch_failure_applies_nothing(box):
    box.flag("sr_launch_fail")
    rc, out, err = box.apply()
    assert rc == 1 and box.phase() == "aborted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV and box.actions() == []
    assert box.timers() == []


# -- C-10(a)：前景行程被殺，流程仍完成 -----------------------------------


def spawn_outer(box, tmp_path):
    out = open(tmp_path / "outer.out", "wb")
    return subprocess.Popen(
        [sys.executable, "-m", "lgwctl", "tunnel-apply", "--new-env", box.new_env,
         "--new-dropin", box.new_dropin],
        cwd=str(REPO_ROOT), env=box.env, stdout=out, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        # 在 nohup 底下跑測試時 SIGHUP 會被繼承成「忽略」；前景行程要用預設行為才算被殺。
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL)), out


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
@pytest.mark.parametrize("probe_ok", [True, False])
def test_foreground_killed_during_probe_wait_still_completes(box, tmp_path, sig, probe_ok):
    box.flag("sr_async")
    box.flag("probe_gate")
    if not probe_ok:
        box.probe_rcs([1])
        box.env["LLMGW_TUNNEL_APPLY_WAIT"] = "1"
    proc, out = spawn_outer(box, tmp_path)
    try:
        assert wait_for(lambda: os.path.exists(os.path.join(box.fake, "probe.log"))), "探針沒開始"
        os.kill(proc.pid, sig)
        proc.wait(timeout=20)
        assert proc.returncode != 0                      # 前景真的死了
        os.unlink(os.path.join(box.fake, "probe_gate"))
        want = "pending-confirm" if probe_ok else "reverted"
        assert wait_for(lambda: box.phase() == want), box.phase()
    finally:
        out.close()
        if proc.poll() is None:
            proc.kill()
    if probe_ok:
        assert box.read(box.etc("tunnel.env")) == NEW_ENV
        assert box.timers() and box.restarts() == ["restart llmgateway-tunnel.service"]
    else:
        assert box.read(box.etc("tunnel.env")) == OLD_ENV
        assert not os.path.exists(box.dropin)
        assert len(box.restarts()) == 2                  # 套用一次、還原一次
    box.assert_no_secrets((tmp_path / "outer.out").read_text())


# -- 探針失敗／逾時／rc=2 皆還原 -----------------------------------------


@pytest.mark.parametrize("rcs,calls", [([1], None), ([2], 1), ([1, 1, 2], 3)])
def test_probe_failure_timeout_or_rc2_reverts(box, rcs, calls):
    box.write(box.dropin, OLD_DROPIN)
    box.probe_rcs(rcs)
    rc, out, err = box.apply()
    assert rc == 1
    assert box.phase() == "reverted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV
    assert box.read(box.dropin) == OLD_DROPIN
    assert len(box.restarts()) == 2
    assert len([a for a in box.actions() if a.startswith("daemon-reload")]) == 2
    assert "stop lgw-tunnel-revert.timer" in box.actions()
    if calls is not None:
        assert len(box.log_lines("probe.log")) == calls
    # 還原後 --confirm 拒絕
    rc, out2, err2 = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 1 and "已還原" in out2
    box.assert_no_secrets(out, err, out2, err2)


def test_daemon_reload_failure_restores_files_and_reports_incomplete_revert(box):
    box.flag("reload_fail")          # 假 systemctl 的 daemon-reload 一直失敗（還原那次也是）
    rc, out, err = box.apply()
    assert rc == 1 and box.phase() == "revert-failed"
    assert "人工" in out
    assert box.read(box.etc("tunnel.env")) == OLD_ENV and not os.path.exists(box.dropin)
    assert box.read(box.etc("tunnel.env")) == OLD_ENV and not os.path.exists(box.dropin)


# -- C-10(c)：還原範圍與結果 ---------------------------------------------


def go_pending(box, **kw):
    rc, out, err = box.apply(**kw)
    assert rc == 0, (out, err)


def fire_timer(box):
    return box.run_cli(["tunnel-apply", "--revert-inner"], env=box.inner_env())


def reverted_files(path):
    base = os.path.basename(path) + ".reverted-"
    return [n for n in os.listdir(os.path.dirname(path)) if n.startswith(base)]


def test_timer_fire_restores_env_and_removes_only_our_dropin(box):
    go_pending(box)
    assert os.path.isfile(box.dropin)
    rc, out, err = fire_timer(box)
    assert rc == 0, out
    assert box.phase() == "reverted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV
    assert not os.path.exists(box.dropin)
    assert not os.path.exists(box.dropin_dir)              # 本次新建且已空 → rmdir
    assert box.read(box.new_env) == NEW_ENV
    assert len(box.restarts()) == 2
    box.assert_no_secrets(out, err)


def test_existing_empty_dropin_dir_is_kept(box):
    os.makedirs(box.dropin_dir)
    go_pending(box)
    fire_timer(box)
    assert os.path.isdir(box.dropin_dir) and not os.path.exists(box.dropin)


def test_dropin_dir_with_other_files_is_kept(box):
    go_pending(box)
    box.write(os.path.join(box.dropin_dir, "other.conf"), "keep")
    fire_timer(box)
    assert not os.path.exists(box.dropin)
    assert box.read(os.path.join(box.dropin_dir, "other.conf")) == "keep"


def test_preexisting_dropin_is_restored_not_deleted(box):
    box.write(box.dropin, OLD_DROPIN)
    go_pending(box)
    assert box.read(box.dropin) == NEW_DROPIN
    fire_timer(box)
    assert box.read(box.dropin) == OLD_DROPIN


def test_no_new_dropin_leaves_existing_dropin_alone(box):
    box.write(box.dropin, OLD_DROPIN)
    go_pending(box, dropin=False)
    assert box.read(box.dropin) == OLD_DROPIN
    fire_timer(box)
    assert box.read(box.dropin) == OLD_DROPIN


def test_default_path_follow_file_and_known_hosts_restored_to_prior_content(box):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    box.write(box.known_hosts, "OLD-KNOWN-HOSTS", 0o600)
    go_pending(box)                      # 新舊 tunnel.env 都沒設 FOLLOW_FILE → 預設路徑
    box.write(box.follow, "SENTINEL_FOLLOW_NEW", 0o600)      # 切換後隧道改寫
    box.write(box.known_hosts, "NEW-KNOWN-HOSTS", 0o600)
    rc, out, err = fire_timer(box)
    assert rc == 0
    assert box.read(box.follow) == "SENTINEL_FOLLOW_OLD"
    assert box.read(box.known_hosts) == "OLD-KNOWN-HOSTS"
    assert reverted_files(box.follow) == [] and reverted_files(box.known_hosts) == []
    box.assert_no_secrets(out, err)


def test_files_created_by_the_switch_are_renamed_not_deleted(box):
    go_pending(box)                      # 切換前什麼都不存在
    box.write(box.follow, "SENTINEL_FOLLOW_NEW", 0o600)
    box.write(box.known_hosts, "NEW-KNOWN-HOSTS", 0o600)
    fire_timer(box)
    assert not os.path.exists(box.follow) and not os.path.exists(box.known_hosts)
    [moved] = reverted_files(box.follow)
    assert box.read(os.path.join(box.state(), moved)) == "SENTINEL_FOLLOW_NEW"
    [moved_kh] = reverted_files(box.known_hosts)
    assert box.read(os.path.join(box.state(), moved_kh)) == "NEW-KNOWN-HOSTS"


def test_old_and_new_custom_follow_paths_both_handled(box):
    old_path = box.state("old-follow.json")
    new_path = box.state("new-follow.json")
    box.write(box.etc("tunnel.env"), OLD_ENV + f"FOLLOW_FILE={old_path}\n", 0o640)
    new_env = box.write(box.etc("tunnel.env.new"), NEW_ENV + f"FOLLOW_FILE={new_path}\n", 0o600)
    box.write(old_path, "SENTINEL_FOLLOW_OLD", 0o600)
    box.write(box.follow, "DEFAULT-OLD", 0o600)        # 預設路徑兩邊都沒設，但恰好有檔 → 不動
    rc, out, err = box.apply(new_env=new_env)
    assert rc == 0
    box.write(old_path, "SENTINEL_FOLLOW_NEW", 0o600)    # 切換後隧道改寫舊路徑
    box.write(new_path, "SENTINEL_FOLLOW_NEW", 0o600)    # 並在新路徑建了檔
    fire_timer(box)
    assert box.read(old_path) == "SENTINEL_FOLLOW_OLD"                 # 切換前已存在 → 還原內容
    assert not os.path.exists(new_path) and len(reverted_files(new_path)) == 1  # 新建 → 改名移開
    assert box.read(box.follow) == "DEFAULT-OLD"
    assert reverted_files(old_path) == []
    assert box.read(box.etc("tunnel.env")) == OLD_ENV + f"FOLLOW_FILE={old_path}\n"


def test_custom_new_path_that_already_existed_is_restored(box):
    new_path = box.state("new-follow.json")
    new_env = box.write(box.etc("tunnel.env.new"), NEW_ENV + f"FOLLOW_FILE={new_path}\n", 0o600)
    box.write(new_path, "SENTINEL_FOLLOW_OLD", 0o600)
    assert box.apply(new_env=new_env)[0] == 0
    box.write(new_path, "SENTINEL_FOLLOW_NEW", 0o600)
    fire_timer(box)
    assert box.read(new_path) == "SENTINEL_FOLLOW_OLD" and reverted_files(new_path) == []


def test_backup_taken_before_any_overwrite(box):
    """備份的是切換前的檔：套用後備份仍是舊內容，而且還原後 == 切換前。"""
    box.write(box.dropin, OLD_DROPIN)
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    go_pending(box)
    assert box.read(os.path.join(box.bak, "tunnel.env")) == OLD_ENV
    assert box.read(os.path.join(box.bak, "credential.conf")) == OLD_DROPIN
    manifest = json.loads(box.read(os.path.join(box.bak, "manifest.json")))
    assert manifest["tunnel_env"]["exists"] and manifest["dropin"]["exists"]
    assert [f["exists"] for f in manifest["files"]] == [True, False]


# -- --confirm ----------------------------------------------------------


def test_confirm_cancels_timer_and_never_reverts(box):
    go_pending(box)
    rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 0, out
    assert box.phase() == "confirmed"
    assert "stop lgw-tunnel-revert.timer" in box.actions()
    assert not os.path.exists(os.path.join(box.fake, "active", "lgw-tunnel-revert.timer"))
    restarts = len(box.restarts())
    # 就算（競態下）還有 revert-inner 到點，也不還原。
    rc, out2, err2 = fire_timer(box)
    assert rc == 0
    assert box.read(box.etc("tunnel.env")) == NEW_ENV and os.path.isfile(box.dropin)
    assert len(box.restarts()) == restarts
    assert box.phase() == "confirmed"


def test_confirm_without_pending_is_refused(box):
    rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 1 and "沒有待確認" in out
    assert "stop lgw-tunnel-revert.timer" not in box.actions()


def test_confirm_refused_after_timer_already_reverted(box):
    go_pending(box)
    assert fire_timer(box)[0] == 0
    rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 1 and "已還原" in out
    assert box.phase() == "reverted"


def test_confirm_fails_if_timer_cannot_be_cancelled(box):
    go_pending(box)
    # 假 systemctl 的 stop 總是 0：換成對 timer 會失敗的版本（timer 仍 active）。
    path = os.path.join(box.fakebin, "systemctl")
    text = box.read(path).replace('start|restart|stop)', 'stop)\n        [[ "$unit" == lgw-tunnel-revert.timer ]] && exit 1\n        exit 0 ;;\n    start|restart)', 1)
    box.write(path, text, 0o755)
    rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 1 and box.phase() == "pending-confirm"


# -- 輸出零洩漏 ---------------------------------------------------------


def test_outputs_never_contain_file_contents(box):
    box.write(box.dropin, OLD_DROPIN)
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    outs = []
    outs += box.apply()[1:]
    outs += fire_timer(box)[1:]
    box.probe_rcs([1])
    outs += box.apply()[1:]
    box.probe_rcs([0])
    outs += box.apply()[1:]
    outs += box.run_cli(["tunnel-apply", "--confirm"])[1:]
    box.assert_no_secrets(*outs)


# =========================================================================
# P3a 修正（審查 2 高 4 中 2 低）
# =========================================================================

import shutil                                             # noqa: E402
import threading                                          # noqa: E402

RETRY_GUIDE = "--retry-revert"


def set_state(box, **kw):
    data = json.loads(box.read(box.state_file))
    data.update(kw)
    box.write(box.state_file, json.dumps(data) + "\n", 0o600)


def bak_snapshot(box):
    return {n: open(os.path.join(box.bak, n), "rb").read() for n in sorted(os.listdir(box.bak))}


def retry_units(box):
    return [line for line in box.log_lines("order.log")
            if "--on-active=" in line and "lgw-tunnel-revert-retry" in line]


def retry_revert(box, env=None):
    return box.run_cli(["tunnel-apply", "--retry-revert"], env=env)


# -- 高-1：還原失敗不停保險、不毀備份 ---------------------------------------


def test_revert_restart_failure_keeps_backup_state_and_schedules_retry(box):
    go_pending(box)
    before = bak_snapshot(box)
    box.set_start_fail("llmgateway-tunnel.service")             # 還原時重啟失敗
    rc, out, err = fire_timer(box)
    assert rc == 1
    assert box.phase() == "revert-failed"
    assert bak_snapshot(box) == before                           # 備份原樣保留
    assert box.read(box.etc("tunnel.env")) == OLD_ENV            # 檔案已還原，只是重啟失敗
    [retry] = retry_units(box)
    assert "--on-active=60s" in retry and "--revert-inner" in retry
    assert not [a for a in box.actions() if a.startswith("stop ")]       # 保險不停
    assert RETRY_GUIDE in out and "人工" in out
    assert box.run_cli(["tunnel-apply", "--confirm"])[0] == 1
    box.assert_no_secrets(out, err)


def test_revert_partial_write_failure_keeps_backup(box):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    go_pending(box)
    os.unlink(os.path.join(box.bak, "follow-0"))                 # 其中一項還原不了
    before = bak_snapshot(box)
    rc, out, err = fire_timer(box)
    assert rc == 1 and box.phase() == "revert-failed"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV            # 別項照樣還原
    assert bak_snapshot(box) == before and len(retry_units(box)) == 1
    assert RETRY_GUIDE in out


def test_retry_revert_succeeds_then_terminal(box):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    go_pending(box)
    saved = open(os.path.join(box.bak, "follow-0"), "rb").read()
    os.unlink(os.path.join(box.bak, "follow-0"))
    assert fire_timer(box)[0] == 1 and box.phase() == "revert-failed"
    box.write(os.path.join(box.bak, "follow-0"), saved.decode(), 0o600)
    rc, out, err = retry_revert(box)
    assert rc == 0, out
    assert box.phase() == "reverted"
    assert box.read(box.follow) == "SENTINEL_FOLLOW_OLD"
    assert "stop lgw-tunnel-revert.timer" in box.actions()
    assert box.apply()[0] == 0                                   # 終態後才能再來


def test_retry_revert_failure_stays_revert_failed(box):
    go_pending(box)
    box.set_start_fail("llmgateway-tunnel.service")
    assert fire_timer(box)[0] == 1
    rc, out, err = retry_revert(box)
    assert rc == 1 and box.phase() == "revert-failed" and os.path.isdir(box.bak)
    assert RETRY_GUIDE in out


def test_retry_revert_only_when_revert_failed(box):
    rc, out, err = retry_revert(box)
    assert rc == 1 and box.actions() == []
    go_pending(box)
    rc, out, err = retry_revert(box)
    assert rc == 1 and box.phase() == "pending-confirm"
    assert box.read(box.etc("tunnel.env")) == NEW_ENV


def test_retry_revert_needs_root(box, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    env = {k: v for k, v in box.env.items() if k != "LLMGW_PREFIX"}
    rc, out, err = retry_revert(box, env=env)
    assert rc == 1 and "root" in out


def test_retry_exhausted_gives_guidance_and_no_more_timers(box):
    go_pending(box)
    box.set_start_fail("llmgateway-tunnel.service")
    assert fire_timer(box)[0] == 1
    count = len(retry_units(box))
    rc, out, err = box.run_cli(["tunnel-apply", "--revert-inner", "--revert-attempt", "5"],
                               env=box.inner_env())
    assert rc == 1 and box.phase() == "revert-failed" and os.path.isdir(box.bak)
    assert len(retry_units(box)) == count                        # 用盡：不再排
    assert RETRY_GUIDE in out and "人工" in out


def test_apply_refused_while_revert_failed_and_backup_untouched(box):
    go_pending(box)
    box.set_start_fail("llmgateway-tunnel.service")
    assert fire_timer(box)[0] == 1
    before = bak_snapshot(box)
    manifest = box.read(os.path.join(box.bak, "manifest.json"))
    rc, out, err = box.apply()
    assert rc == 1 and RETRY_GUIDE in out
    assert bak_snapshot(box) == before
    assert box.read(os.path.join(box.bak, "manifest.json")) == manifest
    assert box.phase() == "revert-failed"


def test_apply_inner_revert_failure_also_keeps_backup_and_schedules_retry(box):
    box.flag("reload_fail")
    rc, out, err = box.apply()
    assert rc == 1 and box.phase() == "revert-failed"
    assert os.path.isfile(os.path.join(box.bak, "manifest.json"))
    assert len(retry_units(box)) == 1
    assert not [a for a in box.actions() if a.startswith("stop ")]       # 保險 timer 仍在
    assert os.path.exists(os.path.join(box.fake, "active", "lgw-tunnel-revert.timer"))
    assert box.apply()[0] == 1


def test_second_timer_fire_after_revert_is_noop(box):
    go_pending(box)
    assert fire_timer(box)[0] == 0
    restarts = len(box.restarts())
    box.write(box.etc("tunnel.env"), "HAND-EDITED=1\n", 0o600)
    assert fire_timer(box)[0] == 0
    assert len(box.restarts()) == restarts
    assert box.read(box.etc("tunnel.env")) == "HAND-EDITED=1\n"


def test_stale_round_timer_is_noop(box):
    go_pending(box)
    rc, out, err = box.run_cli(["tunnel-apply", "--revert-inner", "--round", "not-this-round"],
                               env=box.inner_env())
    assert rc == 0 and box.phase() == "pending-confirm"
    assert box.read(box.etc("tunnel.env")) == NEW_ENV


# -- 高-2：no-follow／父目錄 ------------------------------------------------


@pytest.fixture
def outside(box):
    """root 不該碰到的目標（symlink 指向這裡）。"""
    directory = box.path("outside")
    os.makedirs(directory)
    os.makedirs(box.state(), exist_ok=True)
    box.write(os.path.join(directory, "secret.txt"), "SENTINEL_FOLLOW_NEW", 0o600)
    return directory


def listing(directory):
    return {n: open(os.path.join(directory, n), "rb").read() for n in sorted(os.listdir(directory))}


def no_leak_in_backup(box):
    if os.path.isdir(box.bak):
        for name in os.listdir(box.bak):
            assert b"SENTINEL_FOLLOW_NEW" not in open(os.path.join(box.bak, name), "rb").read()


def test_backup_refuses_symlink_follow_file(box, outside):
    os.symlink(os.path.join(outside, "secret.txt"), box.follow)
    before = listing(outside)
    rc, out, err = box.apply()
    assert rc == 1 and box.log("order.log") == ""
    no_leak_in_backup(box)
    assert listing(outside) == before
    assert box.read(box.etc("tunnel.env")) == OLD_ENV


def test_backup_refuses_symlink_parent_of_custom_follow(box, outside):
    os.symlink(outside, box.state("sub"))
    new_env = box.write(box.etc("tunnel.env.new"),
                        NEW_ENV + f"FOLLOW_FILE={box.state('sub', 'f.json')}\n", 0o600)
    box.write(os.path.join(outside, "f.json"), "SENTINEL_FOLLOW_NEW", 0o600)
    rc, out, err = box.apply(new_env=new_env)
    assert rc == 1 and box.log("order.log") == ""
    no_leak_in_backup(box)


def test_backup_refuses_symlink_dropin_dir(box, outside):
    box.write(os.path.join(outside, "credential.conf"), "SENTINEL_FOLLOW_NEW", 0o600)
    os.makedirs(os.path.dirname(box.dropin_dir), exist_ok=True)
    os.symlink(outside, box.dropin_dir)
    before = listing(outside)
    rc, out, err = box.apply()
    assert rc == 1 and box.log("order.log") == ""
    no_leak_in_backup(box)
    assert listing(outside) == before


def test_backup_refuses_fifo_follow_file(box):
    os.makedirs(os.path.dirname(box.follow), exist_ok=True)
    os.mkfifo(box.follow)
    rc, out, err = box.apply()
    assert rc == 1 and box.log("order.log") == ""


def test_backup_refuses_oversized_follow_file(box):
    box.write(box.follow, "x" * ((1 << 20) + 1), 0o600)
    rc, out, err = box.apply()
    assert rc == 1 and box.log("order.log") == ""


def test_oversized_new_env_refused_before_anything_is_scheduled(box):
    big = box.write(box.etc("big.env.new"), "A=1\n" + "x" * ((1 << 20) + 10), 0o600)
    rc, out, err = box.apply(new_env=big)
    assert rc == 1 and box.log("order.log") == "" and box.timers() == []
    assert box.read(box.etc("tunnel.env")) == OLD_ENV


def swap_on_open(monkeypatch, name, make):
    from lgwctl import safefs
    done = []

    def hook(opened):
        if opened == name and not done:
            done.append(1)
            make()
    monkeypatch.setattr(safefs, "TEST_HOOK", hook)
    return done


def test_toctou_follow_file_swapped_to_symlink_between_check_and_open(box, outside, monkeypatch):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)

    def make():
        os.unlink(box.follow)
        os.symlink(os.path.join(outside, "secret.txt"), box.follow)
    done = swap_on_open(monkeypatch, "gateway.json", make)
    rc, out, err = box.apply()
    assert done and rc == 1 and box.log("order.log") == ""
    no_leak_in_backup(box)
    assert box.read(os.path.join(outside, "secret.txt")) == "SENTINEL_FOLLOW_NEW"


def test_toctou_new_env_swapped_to_symlink_between_check_and_open(box, outside, monkeypatch):
    def make():
        os.unlink(box.new_env)
        os.symlink(os.path.join(outside, "secret.txt"), box.new_env)
    done = swap_on_open(monkeypatch, "tunnel.env.new", make)
    rc, out, err = box.apply()
    assert done and rc == 1
    assert box.read(box.etc("tunnel.env")) == OLD_ENV
    assert "SENTINEL_FOLLOW_NEW" not in box.read(box.etc("tunnel.env"))


def test_restore_over_symlinked_follow_file_never_writes_through(box, outside):
    box.write(box.follow, "SENTINEL_FOLLOW_OLD", 0o600)
    go_pending(box)
    os.unlink(box.follow)
    target = os.path.join(outside, "secret.txt")
    os.symlink(target, box.follow)
    before = listing(outside)
    assert fire_timer(box)[0] == 0
    assert listing(outside) == before
    assert not os.path.islink(box.follow) and box.read(box.follow) == "SENTINEL_FOLLOW_OLD"


def test_restore_moves_symlink_created_by_switch_without_touching_target(box, outside):
    go_pending(box)                                     # 切換前 follow 不存在
    os.symlink(os.path.join(outside, "secret.txt"), box.follow)
    before = listing(outside)
    assert fire_timer(box)[0] == 0
    assert listing(outside) == before
    assert not os.path.lexists(box.follow)
    [moved] = reverted_files(box.follow)
    assert os.path.islink(os.path.join(box.state(), moved))


def test_restore_with_parent_swapped_to_symlink_touches_nothing_outside(box, outside):
    real = box.state("sub")
    new_env = box.write(box.etc("tunnel.env.new"),
                        NEW_ENV + f"FOLLOW_FILE={os.path.join(real, 'f.json')}\n", 0o600)
    box.write(os.path.join(real, "f.json"), "SENTINEL_FOLLOW_OLD", 0o600)
    assert box.apply(new_env=new_env)[0] == 0
    shutil.rmtree(real)
    os.symlink(outside, real)
    box.write(os.path.join(outside, "f.json"), "OUTSIDE-ORIGINAL", 0o600)
    before = listing(outside)
    rc, out, err = fire_timer(box)
    assert rc == 1 and box.phase() == "revert-failed"            # 父目錄是 symlink：拒絕
    assert listing(outside) == before
    assert box.read(box.etc("tunnel.env")) == OLD_ENV             # 其他項照樣還原


def test_restore_dropin_with_dir_swapped_to_symlink_deletes_nothing_outside(box, outside):
    go_pending(box)
    shutil.rmtree(box.dropin_dir)
    os.symlink(outside, box.dropin_dir)
    box.write(os.path.join(outside, "credential.conf"), "OUTSIDE-CRED", 0o600)
    before = listing(outside)
    fire_timer(box)
    assert listing(outside) == before
    assert os.path.isdir(outside)


def test_parse_env_file_not_used_on_untrusted_paths():
    import inspect
    assert "parse_env_file(" not in inspect.getsource(tunnel_apply)


# -- 中-1：confirm 與到點的 timer --------------------------------------------


def test_deadline_is_recorded_when_timer_is_scheduled(box):
    now = time.time()
    go_pending(box)
    deadline = json.loads(box.read(box.state_file))["deadline_epoch"]
    assert now + 590 <= deadline <= now + 620


def test_confirm_after_deadline_reverts_instead_of_confirming(box):
    go_pending(box)
    set_state(box, deadline_epoch=time.time() - 1)
    rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 1 and "到期" in out
    assert box.phase() == "reverted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV and not os.path.exists(box.dropin)
    assert len(box.restarts()) == 2


def test_confirm_when_revert_service_already_triggered_reverts(box):
    go_pending(box)
    box.set_active("lgw-tunnel-revert.service")
    rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
    assert rc == 1 and box.phase() == "reverted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV


def test_confirm_before_deadline_still_confirms(box):
    go_pending(box)
    set_state(box, deadline_epoch=time.time() + 300)
    assert box.run_cli(["tunnel-apply", "--confirm"])[0] == 0
    assert box.phase() == "confirmed"


def test_due_timer_waiting_for_lock_beats_confirm(box):
    go_pending(box)
    set_state(box, deadline_epoch=time.time() - 1)
    box.env["LLMGW_TUNNEL_APPLY_LOCK_WAIT"] = "10"
    fd = hold_lock(box)
    proc = subprocess.Popen([sys.executable, "-m", "lgwctl", "tunnel-apply", "--revert-inner"],
                            cwd=str(REPO_ROOT), env={**box.env, "INVOCATION_ID": "x"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(0.5)
        threading.Timer(0.2, lambda: os.close(fd)).start()
        rc, out, err = box.run_cli(["tunnel-apply", "--confirm"])
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 1
    assert box.phase() == "reverted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV


# -- 中-2：探針窗口 -----------------------------------------------------------


def test_probe_subprocess_is_bounded_by_the_window(box):
    box.flag("probe_gate")                       # 探針本身會卡 30 秒
    start = time.monotonic()
    rc, out, err = box.apply()
    assert time.monotonic() - start < 15
    assert rc == 1 and box.phase() == "reverted"


def test_probe_timeout_argument_is_min_of_single_and_remaining(box, monkeypatch):
    seen = []

    def fake_rc(layout, *a, **kw):
        seen.append(kw.get("timeout", a[0] if a else None))
        return 1
    monkeypatch.setattr(tunnel_apply.probe, "remote_rc", fake_rc)
    layout = Layout.from_env(box.env)
    assert tunnel_apply._probe_until_ok(layout, tunnel_apply.Report()) is False
    assert seen and all(t is not None and t <= 1.0 + 0.05 for t in seen)


def test_probe_returning_zero_after_deadline_counts_as_failure(box, monkeypatch):
    box.env["LLMGW_TUNNEL_APPLY_WAIT"] = "0.2"

    def late(layout, *a, **kw):
        time.sleep(0.5)
        return 0
    monkeypatch.setattr(tunnel_apply.probe, "remote_rc", late)
    layout = Layout.from_env(box.env)
    assert tunnel_apply._probe_until_ok(layout, tunnel_apply.Report()) is False


def test_speedup_env_ignored_without_test_flag(tmp_path):
    env = {"LLMGW_PREFIX": str(tmp_path), "LLMGW_TUNNEL_APPLY_WAIT": "0",
           "LLMGW_TUNNEL_APPLY_INTERVAL": "0", "LLMGW_TUNNEL_APPLY_LOCK_WAIT": "0",
           "LLMGW_TUNNEL_APPLY_FOLLOW": "0"}
    layout = Layout.from_env(env)
    assert tunnel_apply.probe_wait(layout) == tunnel_apply.DEFAULT_PROBE_WAIT >= 90
    assert tunnel_apply.probe_interval(layout) == tunnel_apply.DEFAULT_PROBE_INTERVAL
    assert tunnel_apply._float_env(layout, "LLMGW_TUNNEL_APPLY_LOCK_WAIT", 60.0) == 60.0
    flagged = Layout.from_env({**env, "LLMGW_TUNNEL_APPLY_TEST": "1"})
    assert tunnel_apply.probe_wait(flagged) == 0.0
    off = Layout.from_env({**env, "LLMGW_TUNNEL_APPLY_TEST": "0"})
    assert tunnel_apply.probe_wait(off) == tunnel_apply.DEFAULT_PROBE_WAIT


# -- 中-3：外層死亡空窗 -------------------------------------------------------


def kill_outer_after_state_write(box, monkeypatch):
    with monkeypatch.context() as m:
        def boom(*a, **kw):
            raise KeyboardInterrupt
        m.setattr(tunnel_apply.runmod, "run", boom)
        with pytest.raises(KeyboardInterrupt):
            box.apply()
    assert box.phase() == "apply-scheduled"


def test_stale_apply_scheduled_without_unit_allows_new_round(box, monkeypatch):
    kill_outer_after_state_write(box, monkeypatch)
    rc, out, err = box.apply()
    assert rc == 1 and "apply-scheduled" in out             # 寬限內：仍拒絕
    state = json.loads(box.read(box.state_file))
    state["scheduled_epoch"] = time.time() - 500
    box.write(box.state_file, json.dumps(state) + "\n", 0o600)
    rc, out, err = box.apply()
    assert rc == 0, out
    assert "殘留" in out
    assert box.phase() == "pending-confirm"


def test_stale_apply_scheduled_with_live_unit_is_still_refused(box, monkeypatch):
    kill_outer_after_state_write(box, monkeypatch)
    set_state(box, scheduled_epoch=time.time() - 500)
    box.set_active("lgw-tunnel-apply.service")
    rc, out, err = box.apply()
    assert rc == 1 and box.phase() == "apply-scheduled"


# -- 中-4：timer 等鎖逾時要重排 ----------------------------------------------


def test_timer_lock_timeout_reschedules_and_later_still_reverts(box):
    go_pending(box)
    fd = hold_lock(box)
    try:
        rc, out, err = fire_timer(box)
        assert rc == 1
        [retry] = retry_units(box)
        assert "--revert-attempt" in retry and "--on-active=" in retry
    finally:
        os.close(fd)
    rc, out, err = box.run_cli(["tunnel-apply", "--revert-inner", "--revert-attempt", "1"],
                               env=box.inner_env())
    assert rc == 0 and box.phase() == "reverted"
    assert box.read(box.etc("tunnel.env")) == OLD_ENV


def test_timer_lock_timeout_gives_up_after_limit_with_guidance(box):
    go_pending(box)
    fd = hold_lock(box)
    try:
        rc, out, err = box.run_cli(["tunnel-apply", "--revert-inner", "--revert-attempt", "5"],
                                   env=box.inner_env())
    finally:
        os.close(fd)
    assert rc == 1 and retry_units(box) == [] and "人工" in out


# -- 低 ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["10m\x1b[31mSENTINEL_NEW_TOKEN_92", "SENTINEL_OLD_TOKEN_91",
                                   "1s", "10m\nSENTINEL_NEW_CRED"])
def test_revert_after_error_does_not_echo_input(box, value):
    rc, out, err = box.apply("--revert-after", value)
    assert rc == 1 and "revert-after" in out
    for needle in ("SENTINEL", "\x1b", value.strip()):
        assert needle not in out + err
    assert box.log("order.log") == ""


def test_invocation_id_documented_as_misuse_guard_not_auth():
    assert "防誤用" in tunnel_apply.__doc__ and "不是認證" in tunnel_apply.__doc__
