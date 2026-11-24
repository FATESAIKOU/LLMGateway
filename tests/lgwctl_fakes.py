"""lgwctl 測試共用的沙箱：假 systemctl、假檔案系統（LLMGW_PREFIX 指到暫存目錄）。

- 外部指令一律換成假 script（``LLMGW_SYSTEMCTL``／``LLMGW_GIT``／``LLMGW_CURL``／
  ``LLMGW_GH``／``LLMGW_SUDO``／``LLMGW_USERADD``），不碰真機、不呼叫真的
  systemctl；
- 服務帳號的 HOME 指到暫存目錄（隧道金鑰不會碰到真的 ``~/.ssh``）；
- 沙箱底下的 keys.pass／keys.env 都是測試自己的假字串（沒有真 key）；
- 假 script 把動作與參數記在 ``<prefix>/fake/state/``，測試再讀那個 log 斷言。

這個檔不是測試模組（``unittest discover`` 不會收），只給 ``tests/test_lgwctl_*.py``
匯入。
"""

from __future__ import annotations

import contextlib
import io
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POLICY = str(REPO_ROOT / "policy" / "default.json")

FAKE_TAG = "lgw-v1.2.3"
NEW_TAG = "lgw-v1.2.4"
OLD_TAG = "lgw-v1.2.2"
# #289：定期檢查的 unit 名（沙箱預設把 timer 標成已安裝並在跑）。
CHECK_SERVICE = "llmgateway-check.service"
CHECK_TIMER = "llmgateway-check.timer"
# #296：check 失敗通知的 unit 名（沙箱的版本樣板不含它，需要時測試自己放）。
CHECK_NOTIFY = "llmgateway-check-notify.service"
# 假的密碼／key 值：用來證明 llmgwctl 的輸出不含值（只在測試裡用）。
FAKE_PASSWORD = "FakeKeysPassword_DoNotUse_0001"
FAKE_KEY_VALUE = "FakeKeyValue_DoNotUse_0002"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# 假 systemctl：狀態看 $LGW_FAKE/active/<unit>；動作記進 actions.log。
fake="${LGW_FAKE:?}"
mkdir -p "${fake}" "${fake}/active" "${fake}/start_fail"
printf '%s\\n' "$*" >> "${fake}/systemctl.log"
unit="${!#}"
case "$1" in
    is-active)
        # rc 4＝systemd 的 not_found（unit 檔不存在），不是 3。
        [[ -f "${fake}/missing/${unit}" ]] && exit 4
        [[ -f "${fake}/active/${unit}" ]] && exit 0 || exit 3 ;;
    is-failed)
        [[ -f "${fake}/failed/${unit}" ]] && exit 0 || exit 1 ;;
    is-enabled)
        # rc 4＝unit 檔不存在（與 is-active 的 4 一樣是「確定沒裝」）。
        [[ -f "${fake}/missing/${unit}" ]] && exit 4
        [[ -f "${fake}/enabled/${unit}" ]] && exit 0 || exit 1 ;;
    show)
        # #288：`show -p ActiveEnterTimestamp --timestamp=unix --value <unit>`；
        # 有設定檔就印 `@<秒>`，否則空輸出（parse 成 None）。
        if [[ -f "${fake}/active_enter_timestamp" ]]; then
            printf '@%s\\n' "$(cat "${fake}/active_enter_timestamp")"
        fi
        exit 0 ;;
    daemon-reload)
        printf '%s %s\\n' "$1" "$unit" >> "${fake}/actions.log"
        [[ -f "${fake}/reload_fail" ]] && exit 1
        exit 0 ;;
    enable)
        # #289：enable --now 會同時把 unit 標成 enabled 與 active（timer 用）。
        printf '%s\\n' "$*" >> "${fake}/actions.log"
        mkdir -p "${fake}/enabled"
        : > "${fake}/enabled/${unit}"
        [[ "${2:-}" == "--now" ]] && : > "${fake}/active/${unit}"
        exit 0 ;;
    disable)
        printf '%s\\n' "$*" >> "${fake}/actions.log"
        rm -f "${fake}/enabled/${unit}" "${fake}/active/${unit}"
        exit 0 ;;
    start|restart|stop)
        printf '%s %s\\n' "$1" "$unit" >> "${fake}/actions.log"
        if [[ -f "${fake}/start_fail/${unit}" ]]; then
            exit 1
        fi
        # stop 清除 active；start_noop 模擬啟動回 0 但尚未 active。
        if [[ "$1" == "stop" ]]; then
            rm -f "${fake}/active/${unit}"
        elif [[ ! -f "${fake}/start_noop/${unit}" ]]; then
            : > "${fake}/active/${unit}"
        fi
        exit 0 ;;
    *)
        exit 0 ;;
esac
"""

FAKE_GIT = """#!/usr/bin/env bash
# 假 git：只支援 clone --depth 1 --branch <tag> -- <repo> <dest>；用 REAL_TEMPLATE
# 造出一版的內容，unit 檔寫上 tag（好分辨裝的是哪一版）。
fake="${LGW_FAKE:?}"
printf 'git %s\\n' "$*" >> "${fake}/git.log"
if [[ "${FAKE_GIT_FAIL:-0}" == "1" ]]; then
    echo "fake git: forced failure" >&2
    exit 1
fi
if [[ "${1:-}" != "clone" ]]; then
    echo "fake git: only clone" >&2
    exit 1
fi
dest="${!#}"
tag=""
prev=""
for a in "$@"; do
    if [[ "$prev" == "--branch" ]]; then
        tag="$a"
    fi
    prev="$a"
done
mkdir -p "$dest/deploy/units"
cp -a "${REAL_TEMPLATE:?}/." "$dest/"
# 真的 git clone 會有 .git；install 應該把它移掉（發佈目錄不需要）。
mkdir -p "$dest/.git"
printf 'ref: refs/heads/main\\n' > "$dest/.git/HEAD"
if [[ "${FAKE_GIT_BAD_POLICY:-0}" == "1" ]]; then
    printf '{"subscriptions": {}}\\n' > "$dest/policy/default.json"
fi
if [[ "${FAKE_GIT_BAD_LOADER:-0}" == "1" ]]; then
    # 這版的 lgwd.policy 永遠拒收（低-5：dry-run 必須用新 tag 的 loader）
    cat > "$dest/lgwd/policy.py" <<'PY'
class PolicyError(ValueError):
    pass


def load_policy(path):
    raise PolicyError("fake loader always refuses")
PY
fi
printf '[Unit]\\n# llmgateway %s\\n' "$tag" > "$dest/deploy/units/llmgateway.service"
printf '[Unit]\\n# tunnel %s\\n' "$tag" > "$dest/deploy/units/llmgateway-tunnel.service"
if [[ -n "${FAKE_GIT_SKIP:-}" ]]; then
    # 拿掉相對路徑（低-8：驗必備檔）
    rm -rf "$dest/${FAKE_GIT_SKIP}"
fi
exit 0
"""

FAKE_CURL = """#!/usr/bin/env bash
# 假 curl：印出 http 碼。控制方式（先看依路徑的，再看舊的全域介面）：
#   $LGW_FAKE/curl_codes.<key>：這個路徑的連續回碼（一次一行）。
#   $LGW_FAKE/curl_code.<key>：這個路徑的固定回碼。
#     key＝URL 的路徑去掉開頭的 "/" 後把 "/" 換成 "_"（healthz、v1_subscriptions）。
#   $FAKE_CURL_CODE／$LGW_FAKE/curl_codes：不分路徑的舊介面（既有測試靠這個）。
fake="${LGW_FAKE:?}"
printf 'curl %s\\n' "$*" >> "${fake}/curl.log"
if [[ "${FAKE_CURL_RC:-0}" != "0" ]]; then
    exit "${FAKE_CURL_RC}"
fi
url=""
for a in "$@"; do url="$a"; done
path="${url#*://}"
path="${path#*/}"
key="${path//\\//_}"
code=""
codes="${fake}/curl_codes.${key}"
if [[ -s "$codes" ]]; then
    code="$(head -n 1 "$codes")"
    if [[ "$(wc -l < "$codes")" -gt 1 ]]; then
        tail -n +2 "$codes" > "${codes}.next"
        mv "${codes}.next" "$codes"
    else
        : > "$codes"
    fi
elif [[ -f "${fake}/curl_code.${key}" ]]; then
    code="$(cat "${fake}/curl_code.${key}")"
fi
if [[ -z "$code" ]]; then
    code="${FAKE_CURL_CODE:-200}"
    codes="${fake}/curl_codes"
    if [[ -s "$codes" ]]; then
        code="$(head -n 1 "$codes")"
        if [[ "$(wc -l < "$codes")" -gt 1 ]]; then
            tail -n +2 "$codes" > "${codes}.next"
            mv "${codes}.next" "$codes"
        else
            : > "$codes"
        fi
    fi
fi
printf '%s' "$code"
exit 0
"""

FAKE_GH = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
{
    printf 'gh %s\\n' "$*"
    # 這支子行程的環境裡有沒有解密密碼（只記有無，不記值；低-4 的測試用）
    if [[ -n "${LLMGW_KEYS_PASSWORD:-}" ]]; then
        printf 'env-password yes\\n'
    else
        printf 'env-password no\\n'
    fi
} >> "${fake}/gh.log"
case "${1:-}" in
    auth)
        exit "${FAKE_GH_AUTH_RC:-1}" ;;
    api)
        rc="${FAKE_GH_API_RC:-0}"
        if [[ "$rc" != "0" ]]; then
            printf '%s\\n' "${FAKE_GH_API_ERR:-gh: HTTP 404: Not Found}" >&2
            exit "$rc"
        fi
        printf '%s\\n' "${FAKE_GH_API_OUT:-FATESAIKOU/LLMGateway-keys}"
        exit 0 ;;
    *)
        exit 0 ;;
esac
"""

FAKE_SUDO = """#!/usr/bin/env bash
# 假 sudo：-n -u <user> 之後的參數照原樣執行（不真的換身分）。
fake="${LGW_FAKE:?}"
printf 'sudo %s\\n' "$*" >> "${fake}/sudo.log"
if [[ "${1:-}" == "-n" ]]; then
    shift
fi
if [[ "${1:-}" == "-u" ]]; then
    shift 2
fi
exec "$@"
"""

FAKE_USERADD = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
printf 'useradd %s\\n' "$*" >> "${fake}/useradd.log"
exit 0
"""

# 進版內容的假腳本：key-sync 只記 argv 與「環境裡有沒有密碼」（不記值）。
FAKE_KEY_SYNC = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
{
    printf 'argv %s\\n' "$*"
    if [[ -n "${LLMGW_KEYS_PASSWORD:-}" ]]; then
        printf 'password-env yes\\n'
    else
        printf 'password-env no\\n'
    fi
    printf 'home %s\\n' "${HOME:-}"
} >> "${fake}/keysync.log"
printf 'key-sync: fake run\\n'
exit "${FAKE_KEYSYNC_RC:-0}"
"""

FAKE_TUNNEL = """#!/usr/bin/env bash
fake="${LGW_FAKE:?}"
printf 'probe %s\\n' "$*" >> "${fake}/probe.log"
printf 'tunnel-env %s\\n' "${LGW_TUNNEL_ENV:-}" >> "${fake}/probe.log"
# #246：llmgwctl 用環境變數傳模式（argv 維持恰好 --probe-once）；
# 這裡把值記下來，測試才驗得到「有沒有傳、傳了什麼」。
printf 'api-mode %s\\n' "${LGW_PROBE_API_MODE:-}" >> "${fake}/probe.log"
exit "${FAKE_PROBE_RC:-0}"
"""


def url_key(path: str) -> str:
    """假 curl 分路徑控制檔的 key：去掉開頭的 ``/``、``/`` 換成 ``_``。"""
    return path.lstrip("/").replace("/", "_")


def current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        return os.environ.get("USER") or "?"


def lines_with_level(text: str, level: str) -> list:
    """輸出裡某一種三態的行（`ok xxx`／`todo xxx`／`fail xxx`）。"""
    prefix = level + " "
    return [line for line in text.splitlines() if line.startswith(prefix)]


def has_line(text: str, name: str, level: str | None = None) -> bool:
    """輸出裡有沒有某一項（可以指定三態；項目名不含後面的（detail））。"""
    for line in text.splitlines():
        head, _, rest = line.partition(" ")
        if head not in ("ok", "todo", "fail"):
            continue
        if level is not None and head != level:
            continue
        if rest.split("（", 1)[0] == name:
            return True
    return False


class Sandbox:
    """一個測試用的假機器：prefix 底下的檔案系統＋假指令。"""

    def __init__(self, service_user: str = "lgwfake", *, default_timer: bool = True):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.prefix = os.path.join(self.root, "sandbox")
        self.fakebin = os.path.join(self.prefix, "fakebin")
        self.fake = os.path.join(self.prefix, "fake", "state")
        self.service_home = os.path.join(self.root, "home", service_user)
        self.service_user = service_user
        self.template = os.path.join(self.root, "release-template")

        os.makedirs(self.fakebin, exist_ok=True)
        os.makedirs(self.fake, exist_ok=True)
        os.makedirs(os.path.join(self.service_home, ".ssh"), exist_ok=True)
        self._write_tools()
        self._write_template()
        # #289：install／upgrade 類的既有測試可保留「已經裝好 timer 的機器」
        # （enabled＋active），check 的 check-timer 才是 ok；遷移沙箱用
        # default_timer=False，不預設已裝（遷移流程要自己 enable）。
        if default_timer:
            self.set_unit_enabled(CHECK_TIMER)
            self.set_active(CHECK_TIMER)

        self.env = dict(os.environ)
        self.env.update({
            "LLMGW_PREFIX": self.prefix,
            "LLMGW_SERVICE_USER": service_user,
            "LLMGW_SERVICE_HOME": self.service_home,
            "LLMGW_SYSTEMCTL": os.path.join(self.fakebin, "systemctl"),
            "LLMGW_GIT": os.path.join(self.fakebin, "git"),
            "LLMGW_CURL": os.path.join(self.fakebin, "curl"),
            "LLMGW_GH": os.path.join(self.fakebin, "gh"),
            "LLMGW_SUDO": os.path.join(self.fakebin, "sudo"),
            "LLMGW_USERADD": os.path.join(self.fakebin, "useradd"),
            "LLMGW_PYTHON": sys.executable,
            "LLMGW_LIST_WAIT": "0",
            "LGW_FAKE": self.fake,
            "REAL_TEMPLATE": self.template,
            "REAL_POLICY": REAL_POLICY,
        })

    # -- 生命週期 -----------------------------------------------------------

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 建沙箱內容 ---------------------------------------------------------

    def _write_tools(self) -> None:
        for name, text in (("systemctl", FAKE_SYSTEMCTL), ("git", FAKE_GIT),
                           ("curl", FAKE_CURL), ("gh", FAKE_GH),
                           ("sudo", FAKE_SUDO), ("useradd", FAKE_USERADD)):
            path = os.path.join(self.fakebin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(path, 0o755)

    def _write_template(self) -> None:
        self.write(os.path.join(self.template, "policy", "default.json"),
                   Path(REAL_POLICY).read_text(encoding="utf-8"), 0o644)
        self.write(os.path.join(self.template, "lgwd", "__main__.py"),
                   '"""假 lgwd（測試不執行它）。"""\n', 0o644)
        # lgwd.policy 放真的規則檔：升級的 policy dry-run 會真的在版本目錄裡
        # import 它（低-5）；main／keys 與 lgwctl 只要求檔案在（低-8 的必備檔）。
        self.write(os.path.join(self.template, "lgwd", "policy.py"),
                   (REPO_ROOT / "lgwd" / "policy.py").read_text(encoding="utf-8"), 0o644)
        self.write(os.path.join(self.template, "lgwd", "main.py"),
                   '"""假 lgwd.main（必備檔，測試不執行）。"""\n', 0o644)
        self.write(os.path.join(self.template, "lgwd", "keys.py"),
                   '"""假 lgwd.keys（必備檔，測試不執行）。"""\n', 0o644)
        self.write(os.path.join(self.template, "lgwctl", "__main__.py"),
                   '"""假 lgwctl（必備檔，測試不執行）。"""\n', 0o644)
        self.write(os.path.join(self.template, "ops-scripts", "key-sync"),
                   FAKE_KEY_SYNC, 0o755)
        self.write(os.path.join(self.template, "ops-scripts", "llmgateway-tunnel"),
                   FAKE_TUNNEL, 0o755)
        # #289：版本裡的定期檢查 unit（install／upgrade 會照裝）。
        for name in (CHECK_SERVICE, CHECK_TIMER):
            self.write(os.path.join(self.template, "deploy", "units", name),
                       f"[Unit]\n# {name}\n", 0o644)

    # -- 路徑 ---------------------------------------------------------------

    def path(self, *parts) -> str:
        return os.path.join(self.prefix, *parts)

    def opt(self, *parts) -> str:
        return self.path("opt", "llmgateway", *parts)

    def etc(self, *parts) -> str:
        return self.path("etc", "llmgateway", *parts)

    def state(self, *parts) -> str:
        return self.path("var", "lib", "llmgateway", *parts)

    def unit(self, name: str) -> str:
        return self.path("etc", "systemd", "system", name)

    @property
    def current_link(self) -> str:
        return self.opt("current")

    @property
    def previous_link(self) -> str:
        return self.opt("previous")

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def write(self, path: str, text: str = "", mode: int = 0o644) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, mode)
        return path

    def symlink(self, target: str, link: str) -> None:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(target, link)

    # -- 假指令的狀態與 log -------------------------------------------------

    def log(self, name: str) -> str:
        try:
            return self.read(os.path.join(self.fake, name))
        except OSError:
            return ""

    def log_lines(self, name: str) -> list:
        return [line for line in self.log(name).splitlines() if line]

    def actions(self) -> list:
        return self.log_lines("actions.log")

    def set_active(self, unit: str, active: bool = True) -> None:
        path = os.path.join(self.fake, "active", unit)
        if active:
            self.write(path, "1\n")
        elif os.path.exists(path):
            os.unlink(path)

    def set_unit_enabled(self, unit: str, enabled: bool = True) -> None:
        """#289：假 systemctl 的 is-enabled 狀態（rc 0／1）。"""
        path = os.path.join(self.fake, "enabled", unit)
        if enabled:
            self.write(path, "1\n")
        elif os.path.exists(path):
            os.unlink(path)

    def set_missing(self, unit: str, missing: bool = True) -> None:
        """讓假 systemctl 對這個 unit 回 rc 4（systemd 的「unit 檔不存在」）。"""
        path = os.path.join(self.fake, "missing", unit)
        if missing:
            self.write(path, "1\n")
        elif os.path.exists(path):
            os.unlink(path)

    def set_start_fail(self, unit: str) -> None:
        self.write(os.path.join(self.fake, "start_fail", unit), "1\n")

    def set_start_noop(self, unit: str) -> None:
        """start 回 0 但不把 unit 標成 active（systemd 的 simple：fork 完就回 0）。"""
        self.write(os.path.join(self.fake, "start_noop", unit), "1\n")

    def set_daemon_reload_fail(self) -> None:
        self.write(os.path.join(self.fake, "reload_fail"), "1\n")

    def set_failed(self, unit: str) -> None:
        self.write(os.path.join(self.fake, "failed", unit), "1\n")

    def set_active_enter_timestamp(self, seconds: int | None) -> None:
        """假 `systemctl show --timestamp=unix` 的回應（None 表示空輸出）。"""
        path = os.path.join(self.fake, "active_enter_timestamp")
        if seconds is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            self.write(path, f"{int(seconds)}\n")

    def curl_codes(self, codes) -> None:
        self.write(os.path.join(self.fake, "curl_codes"),
                   "".join(f"{code}\n" for code in codes))

    def curl_code(self, path: str, code) -> None:
        """只對某個 URL 路徑回這個碼（#246：healthz 與 list 要分開設）。"""
        self.write(os.path.join(self.fake, "curl_code." + url_key(path)), f"{code}\n")

    def curl_codes_for(self, path: str, codes) -> None:
        """某個 URL 路徑的連續回碼（一次一行）。"""
        self.write(os.path.join(self.fake, "curl_codes." + url_key(path)),
                   "".join(f"{code}\n" for code in codes))

    def set_capabilities(self, version_dir: str, text=None) -> str | None:
        """寫／移除某一版的能力旗標（``deploy/capabilities.json``，#246）。

        ``text=None`` 表示移除（回滾到沒有這個檔的舊版＝missing＝old）。
        """
        path = os.path.join(version_dir, "deploy", "capabilities.json")
        if text is None:
            if os.path.exists(path):
                os.unlink(path)
            return None
        return self.write(path, text)

    # -- 造一版（不經 git）--------------------------------------------------

    def make_release(self, tag: str, *, policy: str | None = None,
                     units: bool = True, check_units: bool = True) -> str:
        dest = self.opt(tag)
        shutil.copytree(self.template, dest, dirs_exist_ok=True)
        if policy is not None:
            self.write(os.path.join(dest, "policy", "default.json"), policy)
        if units:
            self.write(os.path.join(dest, "deploy", "units", "llmgateway.service"),
                       f"[Unit]\n# llmgateway {tag}\n")
            self.write(os.path.join(dest, "deploy", "units", "llmgateway-tunnel.service"),
                       f"[Unit]\n# tunnel {tag}\n")
        if not check_units:
            for name in (CHECK_SERVICE, CHECK_TIMER):
                path = os.path.join(dest, "deploy", "units", name)
                if os.path.exists(path):
                    os.unlink(path)
        self.write(os.path.join(dest, ".lgw-tag"), tag + "\n")
        return dest

    def install_state(self, tag: str = FAKE_TAG, previous: str | None = None) -> str:
        """沙箱已經是「裝好 tag 的機器」（current 指著它，可選 previous）。"""
        dest = self.make_release(tag)
        self.symlink(dest, self.current_link)
        if previous:
            self.symlink(self.make_release(previous), self.previous_link)
        return dest

    # -- 跑指令 -------------------------------------------------------------

    def run_cli(self, argv, env: dict | None = None) -> tuple:
        """在行程內跑 llmgwctl，回 (rc, stdout, stderr)。"""
        from lgwctl import cli  # 只在真的要跑指令時才匯入

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv), env=self.env if env is None else env)
        return rc, out.getvalue(), err.getvalue()
