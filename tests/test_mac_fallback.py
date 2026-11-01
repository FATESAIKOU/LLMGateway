"""ops-scripts/mac-fallback 的離線測試（只用假資料、暫存目錄、假的 lgwd 替身）。

涵蓋：名稱對應（值保留 =、不列出沒對應的行、0600）；keysync status 新舊判斷（none／old／ok／fail）；
check-scripts 抓得到全形字；start／stop 的等待與停損（假 lgwd 聽隨機埠）；switch 的稽核擋下與回滾。
不連真的 4710、不讀任何真實密碼檔或狀態目錄。
"""

import os
import pathlib
import shutil
import socket
import sqlite3
import stat
import subprocess
import time

import pytest

MF = pathlib.Path(__file__).resolve().parents[1] / "ops-scripts" / "mac-fallback"
LIB = MF / "lib.sh"

# 假的 lgwd：依 <checkout>/MODE 檔決定行為（沒有檔就 serve）。
FAKE_LGWD = r'''
import argparse, http.server, json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
mode_file = os.path.join(root, "MODE")
mode = open(mode_file).read().strip() if os.path.exists(mode_file) else "serve"
p = argparse.ArgumentParser()
for flag in ("--db", "--policy", "--keys", "--api-host", "--usage-claude-cwd", "--keysync-script"):
    p.add_argument(flag)
p.add_argument("--api-port", type=int)
p.add_argument("--keysync-interval", type=int)
a, _ = p.parse_known_args()
state = os.path.dirname(a.db)

def write_status(ts, ok, reason="ok"):
    with open(os.path.join(state, ".keysync.status"), "w") as f:
        json.dump({"ts": ts, "ok": ok, "reason": reason}, f)

if mode == "route1-ok":
    with open(a.keys, "w") as f:
        f.write("OPENROUTER_API_KEY=fake-value\n")
    write_status(int(time.time()), True)
elif mode == "route1-sha":
    if not os.path.exists(os.path.join(state, ".keysync.sha")):
        with open(a.keys, "w") as f:
            f.write("OPENROUTER_API_KEY=fake-value\n")
    write_status(int(time.time()), True)
elif mode == "route1-empty":
    open(a.keys, "w").close()
    write_status(int(time.time()), True)
elif mode == "route1-old":
    with open(a.keys, "w") as f:
        f.write("OPENROUTER_API_KEY=fake-value\n")
    write_status(0, True)
elif mode == "route1-fail":
    write_status(int(time.time()), False, "failed=x")
if mode == "silent":
    time.sleep(600)

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/v1/subscriptions"):
            if mode == "unavail":  # 沒有冷卻卻不可用：驗收必須擋下
                rows = [{"id": "a", "available": False, "usage": {"pipes": []}}]
            else:
                rows = [
                    {"id": "a", "available": True, "usage": {"pipes": []}},
                    {"id": "b", "available": False, "cooldown": {"until": "x"}, "usage": {"pipes": []}},
                ]
            body = json.dumps(rows).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", a.api_port), H).serve_forever()
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def run_lib(script, **env):
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'], capture_output=True, text=True, env=full_env)


def make_checkout(root, tag, mode="serve"):
    """做一個 git checkout（有 lgwd/__main__.py 與 tag），MODE 檔決定假 lgwd 的行為。"""
    root.mkdir(parents=True)
    (root / "lgwd").mkdir()
    (root / "lgwd" / "__init__.py").write_text("")
    (root / "lgwd" / "__main__.py").write_text(FAKE_LGWD)
    (root / "policy").mkdir()
    (root / "policy" / "default.json").write_text("{}")
    (root / "MODE").write_text(mode + "\n")
    git = ["git", "-C", str(root), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "fake"], check=True)
    subprocess.run(git + ["tag", tag], check=True)


def set_mode(root, mode):
    (root / "MODE").write_text(mode + "\n")


@pytest.fixture
def env_tree(tmp_path):
    code = tmp_path / "code"
    make_checkout(code, "fake-v1")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    pw = tmp_path / "pw"
    pw.write_text("SRC_KEY=secretvalue123\nSRC_PW=pwsecret456\n")
    pw.chmod(0o600)
    port = free_port()
    conf = tmp_path / "mac-fallback.conf"
    conf.write_text(
        f'PWFILE="{pw}"\nCODE="{code}"\nTAG=fake-v1\nSTATE="{state}"\nPORT={port}\nWAIT_SECONDS=3\n'
        'PASS_SOURCE=ABSENT_PASS\nKEY_MAP="SRC_KEY:OPENROUTER_API_KEY"\n'
    )
    conf.chmod(0o600)  # 設定檔權限不依賴執行者的 umask（lib.sh 會拒絕 group／world 可寫）
    env = {"LGW_MAC_FALLBACK_CONF": str(conf)}
    yield {"tmp": tmp_path, "code": code, "state": state, "pw": pw, "port": port, "conf": conf, "env": env}
    # 收尾：不管測試結果如何，把假 lgwd 停掉（只用 stop 腳本，它只停 pid 檔裡的那一個）
    subprocess.run(["bash", str(MF / "stop")], env={**os.environ, **env}, capture_output=True, text=True)


def use_route1(env_tree_):
    conf = env_tree_["conf"]
    conf.write_text(conf.read_text().replace("PASS_SOURCE=ABSENT_PASS", "PASS_SOURCE=SRC_PW"))


def run_script(name, env_tree_, *args, extra_env=None):
    env = {**os.environ, **env_tree_["env"], **(extra_env or {})}
    return subprocess.run(["bash", str(MF / name), *args], capture_output=True, text=True, env=env, timeout=120)


# ---------- 名稱對應 ----------

def test_write_mapped_keeps_equals_drops_unlisted_and_is_0600(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("SRC_A=tok=with=equals==\nSRC_B=other\r\nUNLISTED=nope\nSRC_C=  spaced value  \n")
    out = tmp_path / "out.env"
    r = run_lib(f'fb_write_mapped "{out}" "SRC_A:DST_A" "SRC_C:DST_C"', PWFILE=str(pw))
    assert r.returncode == 0, r.stderr
    assert out.read_text() == "DST_A=tok=with=equals==\nDST_C=  spaced value  \n"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_write_mapped_empty_when_no_source_matches(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("ONLY=1\n")
    out = tmp_path / "out.env"
    r = run_lib(f'fb_write_mapped "{out}" "NOPE:DST"', PWFILE=str(pw))
    assert r.returncode == 0, r.stderr
    assert out.read_text() == ""


# ---------- status 新舊判斷 ----------

@pytest.mark.parametrize(
    "content, t0, expected",
    [
        (None, 1000, "none"),
        ("not json", 1000, "none"),
        ('{"ts": 999, "ok": true, "reason": "ok"}', 1000, "old"),
        ('{"ts": 1000, "ok": true, "reason": "ok"}', 1000, "ok"),
        ('{"ts": 1001, "ok": false, "reason": "failed=x"}', 1000, "fail:failed=x"),
    ],
)
def test_keysync_verdict(tmp_path, content, t0, expected):
    st = tmp_path / ".keysync.status"
    if content is not None:
        st.write_text(content)
    r = run_lib(f'fb_keysync_verdict "{st}" {t0}')
    assert r.stdout.strip() == expected, r.stderr


# ---------- check-scripts ----------

def test_check_scripts_passes_on_real_scripts():
    r = subprocess.run(["bash", str(MF / "check-scripts")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_scripts_catches_fullwidth_after_variable(tmp_path):
    bad = tmp_path / "bad.sh"
    bad.write_text('#!/bin/bash\nset -u\nX=1\necho "$X（中文）"\n', encoding="utf-8")
    r = subprocess.run(["bash", str(MF / "check-scripts"), str(bad)], capture_output=True, text=True)
    assert r.returncode == 1, r.stdout
    assert "第 4 行" in r.stdout


def test_check_scripts_accepts_braced_variable(tmp_path):
    good = tmp_path / "good.sh"
    good.write_text('#!/bin/bash\nset -u\nX=1\necho "${X}（中文）"\n', encoding="utf-8")
    r = subprocess.run(["bash", str(MF / "check-scripts"), str(good)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_check_scripts_catches_bash_syntax_error(tmp_path):
    bad = tmp_path / "syntax.sh"
    bad.write_text("if then\n", encoding="utf-8")
    r = subprocess.run(["bash", str(MF / "check-scripts"), str(bad)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "bash -n FAIL" in r.stdout


# ---------- start／stop（假 lgwd 替身）----------

def test_start_route2_ok_then_stop_cleans_keys(env_tree):
    code = env_tree["code"]
    set_mode(code, "serve")
    r = run_script("start", env_tree)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "路線 2" in r.stdout and "RESULT: OK" in r.stdout
    assert "secretvalue123" not in r.stdout + r.stderr  # 值不經過輸出
    keys = env_tree["state"] / "keys.env"
    assert keys.read_text() == "OPENROUTER_API_KEY=secretvalue123\n"
    assert stat.S_IMODE(os.stat(keys).st_mode) == 0o600
    assert port_open(env_tree["port"])

    s = run_script("stop", env_tree)
    assert s.returncode == 0, s.stdout + s.stderr
    assert "RESULT: STOPPED" in s.stdout
    assert not keys.exists()
    assert not (env_tree["state"] / "lgwd.pid").exists()
    time.sleep(0.5)
    assert not port_open(env_tree["port"])


def test_start_route1_ok_writes_keys_pass_and_keeps_keys_env(env_tree):
    use_route1(env_tree)
    set_mode(env_tree["code"], "route1-ok")
    r = run_script("start", env_tree)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "路線 1" in r.stdout
    assert "pwsecret456" not in r.stdout + r.stderr
    assert (env_tree["state"] / "keys.env").read_text() == "OPENROUTER_API_KEY=fake-value\n"
    s = run_script("stop", env_tree)
    assert s.returncode == 0
    assert not (env_tree["state"] / "creds" / "keys.pass").exists()


def test_start_route1_clears_stale_sha_so_keysync_writes(env_tree):
    """殘留的 .keysync.sha 會讓 keysync 以為已是最新、不寫檔（2026-11-01 事故）；start 必須先刪掉它。"""
    use_route1(env_tree)
    set_mode(env_tree["code"], "route1-sha")
    (env_tree["state"] / ".keysync.sha").write_text("deadbeef\n")
    r = run_script("start", env_tree)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env_tree["state"] / "keys.env").read_text() == "OPENROUTER_API_KEY=fake-value\n"
    assert run_script("stop", env_tree).returncode == 0


def test_stop_refuses_pid_that_is_not_lgwd(env_tree):
    """pid 檔若指到別的行程，stop 不能動它。"""
    other = subprocess.Popen(["sleep", "600"])
    try:
        (env_tree["state"] / "lgwd.pid").write_text(str(other.pid) + "\n")
        r = run_script("stop", env_tree)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "不是 lgwd" in r.stdout
        assert other.poll() is None
        assert (env_tree["state"] / "lgwd.pid").exists()
    finally:
        other.kill()
        other.wait()


def test_start_route1_empty_keys_env_stops(env_tree):
    use_route1(env_tree)
    set_mode(env_tree["code"], "route1-empty")
    r = run_script("start", env_tree)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "keys.env 是空的" in r.stdout
    assert not (env_tree["state"] / "keys.env").exists()
    assert not (env_tree["state"] / "creds" / "keys.pass").exists()


def test_start_route1_old_status_is_ignored_and_stops(env_tree):
    use_route1(env_tree)
    set_mode(env_tree["code"], "route1-old")
    # 殘留的舊 status（ts 很早、ok）也不能騙過啟動檢查
    (env_tree["state"] / ".keysync.status").write_text('{"ts": 1, "ok": true, "reason": "ok"}')
    r = run_script("start", env_tree)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "keysync 結果" in r.stdout


def test_start_route1_keysync_failure_stops(env_tree):
    use_route1(env_tree)
    set_mode(env_tree["code"], "route1-fail")
    r = run_script("start", env_tree)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "keysync 失敗：failed=x" in r.stdout


def test_start_waits_then_stops_when_no_200(env_tree):
    set_mode(env_tree["code"], "silent")
    t = time.time()
    r = run_script("start", env_tree)
    elapsed = time.time() - t
    assert r.returncode == 2, r.stdout + r.stderr
    assert "秒內沒有 200" in r.stdout
    assert elapsed >= 3
    assert not (env_tree["state"] / "keys.env").exists()
    assert not (env_tree["state"] / "lgwd.pid").exists()


def test_start_acceptance_rejects_unavailable_row_without_cooldown(env_tree):
    set_mode(env_tree["code"], "unavail")
    r = run_script("start", env_tree)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "沒有冷卻卻不可用" in r.stdout
    assert not (env_tree["state"] / "keys.env").exists()


def test_start_refuses_when_port_busy(env_tree):
    set_mode(env_tree["code"], "serve")
    s = socket.socket()
    s.bind(("127.0.0.1", env_tree["port"]))
    s.listen(1)
    try:
        r = run_script("start", env_tree)
    finally:
        s.close()
    assert r.returncode == 1
    assert "已經有人在聽" in r.stdout


def test_start_refuses_wrong_tag(env_tree):
    set_mode(env_tree["code"], "serve")
    conf = env_tree["conf"]
    conf.write_text(conf.read_text().replace("TAG=fake-v1", "TAG=fake-v9"))
    r = run_script("start", env_tree)
    assert r.returncode == 1
    assert "HEAD 不是 fake-v9" in r.stdout


def test_start_refuses_when_names_do_not_match_and_says_password_file_hint(env_tree):
    """密碼檔在，但名稱都對不到：start 停下來，而且印出「密碼檔不可用（若已加密…）」提示。"""
    env_tree["pw"].write_text("UNRELATED=x\n")
    set_mode(env_tree["code"], "serve")
    r = run_script("start", env_tree)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "密碼檔不可用（若已加密，請先請本人解密）" in r.stdout
    assert not (env_tree["state"] / "lgwd.pid").exists()


def test_stop_without_pid_file_is_clean(env_tree):
    r = run_script("stop", env_tree)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "本來就沒有" in r.stdout


def test_stop_without_pid_file_refuses_when_port_is_taken_by_someone_else(env_tree):
    """沒有 pid 檔，但 4710 被別人佔：stop 不能當成「本來就沒有」，要 rc=1 且不動它。"""
    s = socket.socket()
    s.bind(("127.0.0.1", env_tree["port"]))
    s.listen(1)
    try:
        r = run_script("stop", env_tree)
    finally:
        s.close()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "不動它" in r.stdout


def test_missing_conf_is_refused(tmp_path):
    env = {**os.environ, "LGW_MAC_FALLBACK_CONF": str(tmp_path / "nope.conf")}
    r = subprocess.run(["bash", str(MF / "start")], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "讀不到設定檔" in r.stdout


def test_conf_writable_by_group_or_world_is_refused(env_tree):
    """設定檔會被 source（等於執行），權限 0666 就拒絕，而且不載入。"""
    env_tree["conf"].chmod(0o666)
    r = run_script("start", env_tree)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "拒絕載入" in r.stdout
    assert not (env_tree["state"] / "lgwd.pid").exists()


# ---------- switch ----------

def _audit_db(state, rows):
    state.mkdir(exist_ok=True)
    con = sqlite3.connect(state / "llm.db")
    con.execute("CREATE TABLE audit (ts TEXT, subscription TEXT, action TEXT, agent TEXT, until TEXT, provider TEXT, model TEXT)")
    con.executemany("INSERT INTO audit (ts, action) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_switch_refuses_when_get_audit_is_not_zero(env_tree):
    _audit_db(env_tree["state"], [("2026-11-01T01:00:00Z", "get"), ("2026-11-01T02:00:00Z", "resolve")])
    old = env_tree["code"]
    r = run_script("switch", env_tree, str(old), "fake-v1", str(old), "fake-v1")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "不換版本" in r.stdout
    assert not (env_tree["state"] / "lgwd.pid").exists()


def test_switch_ok_to_new_version(env_tree):
    _audit_db(env_tree["state"], [("2026-11-01T02:00:00Z", "resolve")])
    old = env_tree["code"]
    new = env_tree["tmp"] / "new"
    make_checkout(new, "fake-v2", mode="serve")
    set_mode(old, "serve")
    first = run_script("start", env_tree)
    assert first.returncode == 0, first.stdout + first.stderr
    r = run_script("switch", env_tree, str(old), "fake-v1", str(new), "fake-v2")
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "RESULT: OK" in r.stdout and "fake-v2" in r.stdout
    finally:
        run_script("stop", env_tree)


def test_switch_rolls_back_when_new_version_does_not_serve(env_tree):
    _audit_db(env_tree["state"], [("2026-11-01T02:00:00Z", "resolve")])
    old = env_tree["code"]
    new = env_tree["tmp"] / "new"
    make_checkout(new, "fake-v2", mode="silent")
    set_mode(old, "serve")
    first = run_script("start", env_tree)
    assert first.returncode == 0, first.stdout + first.stderr
    r = run_script("switch", env_tree, str(old), "fake-v1", str(new), "fake-v2")
    try:
        assert r.returncode == 5, r.stdout + r.stderr
        assert "RESULT: ROLLED-BACK" in r.stdout
    finally:
        run_script("stop", env_tree)


@pytest.mark.parametrize("damage", ["missing", "unrelated"])
def test_switch_refuses_before_stop_when_password_file_unusable(env_tree, damage):
    """密碼檔不在或名稱都對不到：停舊版之前就 rc=1，舊版（假 lgwd）還在跑、pid 檔還在。"""
    _audit_db(env_tree["state"], [("2026-11-01T02:00:00Z", "resolve")])
    old = env_tree["code"]
    new = env_tree["tmp"] / "new"
    make_checkout(new, "fake-v2", mode="serve")
    set_mode(old, "serve")
    first = run_script("start", env_tree)
    assert first.returncode == 0, first.stdout + first.stderr
    if damage == "missing":
        env_tree["pw"].unlink()
    else:
        env_tree["pw"].write_text("UNRELATED=x\n")
    try:
        r = run_script("switch", env_tree, str(old), "fake-v1", str(new), "fake-v2")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "舊版沒動" in r.stdout
        assert "== ② 停" not in r.stdout
        assert port_open(env_tree["port"])
        assert (env_tree["state"] / "lgwd.pid").exists()
    finally:
        run_script("stop", env_tree)


def test_switch_does_not_change_version_when_stop_refuses(env_tree):
    """switch 的 stop 回 rc=1（pid 檔指到別的行程）→ 不換版本，也不起新版。"""
    _audit_db(env_tree["state"], [("2026-11-01T02:00:00Z", "resolve")])
    old = env_tree["code"]
    new = env_tree["tmp"] / "new"
    make_checkout(new, "fake-v2", mode="serve")
    set_mode(old, "serve")
    first = run_script("start", env_tree)
    assert first.returncode == 0, first.stdout + first.stderr
    pid_file = env_tree["state"] / "lgwd.pid"
    real_pid = pid_file.read_text()
    other = subprocess.Popen(["sleep", "600"])
    try:
        pid_file.write_text(str(other.pid) + "\n")
        r = run_script("switch", env_tree, str(old), "fake-v1", str(new), "fake-v2")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "沒有換版本" in r.stdout
        assert "== ③" not in r.stdout
        assert other.poll() is None
        assert port_open(env_tree["port"])
    finally:
        other.kill()
        other.wait()
        pid_file.write_text(real_pid)
        run_script("stop", env_tree)
