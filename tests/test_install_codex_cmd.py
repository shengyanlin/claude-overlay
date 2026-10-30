"""install-codex.cmd -- the one place Codex mode gets set up (setup.cmd, update.cmd and the
overlay's "Set up Codex" button all call it).

The field failure it exists for: an install from before Codex mode updated to 1.25.0, the
update printed a [NOTE] about the missing codex CLI into a console that the app's Update
button closes on success, and the user was left with a Codex switch that couldn't be
clicked. So these run the real script under cmd.exe against fake `codex` / `npm` / `winget`
shims on an isolated PATH (APPDATA, ProgramFiles and LOCALAPPDATA all point into tmp, so the
developer's own Node and codex can't leak in), and pin what each caller gets: the exit code
the overlay reads, what is asked, and the remembered "no".
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install-codex.cmd")

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe script")

# `codex login status` exits 0 once tmp\signed-in exists; `codex login` creates it.
FAKE_CODEX = r"""@echo off
echo codex %*>> "%FAKE_LOG%"
if "%1"=="login" if "%2"=="status" (
  if exist "%FAKE_ROOT%\signed-in" exit /b 0
  exit /b 1
)
if "%1"=="login" if not "%FAKE_LOGIN_FAILS%"=="1" echo yes> "%FAKE_ROOT%\signed-in"
exit /b 0
"""
# `npm install -g @openai/codex` drops a codex shim into npm's global bin: %APPDATA%\npm, as
# the official installer sets it, or FAKE_NPM_PREFIX (an .npmrc prefix) -- or --prefix's.
#   FAKE_NPM_FAILS: every install fails (no network, say).
#   FAKE_NPM_EPERM: an install into npm's own folder fails (admin-only); --prefix works.
#   Each run also logs the certificate settings it was given (an "npm-env" line).
FAKE_NPM = r"""@echo off
echo npm %*>> "%FAKE_LOG%"
>> "%FAKE_LOG%" echo npm-env SYSCA=%NODE_USE_SYSTEM_CA% EXTRA=%NODE_EXTRA_CA_CERTS%
if "%1"=="prefix" goto prefix
if "%FAKE_NPM_FAILS%"=="1" exit /b 1
set "DEST="
:args
if "%~1"=="" goto go
if "%~1"=="--prefix" set "DEST=%~2"
shift
goto args
:go
if defined DEST goto copy
if "%FAKE_NPM_EPERM%"=="1" exit /b 1
set "DEST=%APPDATA%\npm"
if defined FAKE_NPM_PREFIX set "DEST=%FAKE_NPM_PREFIX%"
:copy
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%FAKE_ROOT%\codex.src" "%DEST%\codex.cmd" >nul
exit /b 0
:prefix
if defined FAKE_NPM_PREFIX echo %FAKE_NPM_PREFIX%
if not defined FAKE_NPM_PREFIX echo %APPDATA%\npm
exit /b 0
"""
# `winget install ... OpenJS.NodeJS.LTS` puts npm into %ProgramFiles%\nodejs.
FAKE_WINGET = r"""@echo off
echo winget %*>> "%FAKE_LOG%"
copy /y "%FAKE_ROOT%\npm.src" "%ProgramFiles%\nodejs\npm.cmd" >nul
exit /b 0
"""
# `powershell -File install-node.ps1` (the per-user Node.js) puts npm into
# %LOCALAPPDATA%\Programs\nodejs -- or, with FAKE_PS_FAILS, nothing (a blocked download).
#   FAKE_PS_BROKEN: it fails AFTER leaving behind an npm.cmd that doesn't work (node.exe
#   blocked by policy) -- one that says so in the log, so a test can tell which npm ran.
FAKE_POWERSHELL = r"""@echo off
echo powershell %*>> "%FAKE_LOG%"
if "%FAKE_PS_FAILS%"=="1" exit /b 1
if not exist "%LOCALAPPDATA%\Programs\nodejs" mkdir "%LOCALAPPDATA%\Programs\nodejs"
if "%FAKE_PS_BROKEN%"=="1" (
  copy /y "%FAKE_ROOT%\broken-npm.src" "%LOCALAPPDATA%\Programs\nodejs\npm.cmd" >nul
  exit /b 1
)
copy /y "%FAKE_ROOT%\npm.src" "%LOCALAPPDATA%\Programs\nodejs\npm.cmd" >nul
exit /b 0
"""
# `python windowsca.py <pem>` writes a stand-in PEM there -- or, with FAKE_CA_FAILS, nothing.
# `python -c "pass"` (the is-there-a-Python probe) just works.
FAKE_PYTHON = r"""@echo off
echo python %*>> "%FAKE_LOG%"
if "%~1"=="-c" exit /b 0
if /i not "%~nx1"=="windowsca.py" exit /b 1
if "%FAKE_CA_FAILS%"=="1" exit /b 1
if not exist "%~dp2" mkdir "%~dp2"
echo fake-ca> "%~2"
exit /b 0
"""
BROKEN_NPM = r"""@echo off
echo broken-npm %*>> "%FAKE_LOG%"
exit /b 1
"""


@pytest.fixture
def box(tmp_path):
    """An isolated machine: nothing installed until a test says so."""
    for d in ("appdata/npm", "pf/nodejs", "local", "bin"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "codex.src").write_text(FAKE_CODEX)
    (tmp_path / "npm.src").write_text(FAKE_NPM)
    (tmp_path / "winget.src").write_text(FAKE_WINGET)
    (tmp_path / "powershell.src").write_text(FAKE_POWERSHELL)
    (tmp_path / "broken-npm.src").write_text(BROKEN_NPM)
    (tmp_path / "python.src").write_text(FAKE_PYTHON)
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env = {
        "PATH": str(tmp_path / "bin") + ";" + os.path.join(sysroot, "System32") + ";" + sysroot,
        "SystemRoot": sysroot,
        "ComSpec": os.environ.get("ComSpec", os.path.join(sysroot, "System32", "cmd.exe")),
        "APPDATA": str(tmp_path / "appdata"),
        "ProgramFiles": str(tmp_path / "pf"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "FAKE_ROOT": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "log.txt"),
    }

    class Box:
        root = tmp_path
        declined = tmp_path / "local" / "claude-overlay" / "codex-setup-declined"
        result = tmp_path / "local" / "claude-overlay" / "codex-setup-result"

        def has(self, *tools):
            for t in tools:
                src = {"codex": "codex.src", "npm": "npm.src", "winget": "winget.src",
                       "powershell": "powershell.src", "python": "python.src"}[t]
                (tmp_path / "bin" / f"{t}.cmd").write_text((tmp_path / src).read_text())

        def signed_in(self):
            (tmp_path / "signed-in").write_text("yes")

        def run(self, *args, answers="", **extra):
            # From a FILE, not a pipe: on a pipe the first `set /p` swallows every buffered
            # line, so a second question reads EOF and takes its default -- "\nn\n" would
            # answer yes twice. A file is read a line per question, like a console.
            e = dict(env, **extra)
            f = tmp_path / "answers.txt"
            f.write_text(answers)
            with open(f) as stdin:
                p = subprocess.run(["cmd", "/c", SCRIPT, *args], stdin=stdin, env=e,
                                   capture_output=True, text=True, timeout=60)
            return p.returncode, p.stdout

        def log(self):
            f = tmp_path / "log.txt"
            return f.read_text() if f.exists() else ""

    return Box()


def test_installed_and_signed_in_is_a_quiet_zero(box):
    box.has("codex")
    box.signed_in()
    rc, out = box.run("--from-update")
    assert rc == 0
    assert "[Y/n]" not in out                     # nothing to ask
    assert "npm" not in box.log()


def test_update_installs_the_missing_cli_and_signs_in(box):
    """The 1.23 -> 1.25 case: no codex, npm present. Enter at both questions = yes."""
    box.has("npm")
    rc, out = box.run("--from-update", answers="\n\n")
    assert rc == 0, out
    assert "npm install -g @openai/codex" in box.log()
    assert "codex login" in box.log()
    assert (box.root / "appdata" / "npm" / "codex.cmd").exists()


def test_update_installs_node_first_when_there_is_no_npm(box):
    box.has("winget")
    rc, out = box.run("--from-update", answers="\n\n\n")
    assert rc == 0, out
    log = box.log()
    assert "OpenJS.NodeJS.LTS" in log
    assert log.index("winget") < log.index("npm install")


def test_update_no_is_remembered_and_not_asked_again(box):
    box.has("npm")
    rc, out = box.run("--from-update", answers="n\n")
    assert rc == 3
    assert box.declined.exists()
    assert "npm" not in box.log()
    rc, out = box.run("--from-update", answers="")   # stdin empty: a question would hang/answer
    assert rc == 3
    assert "[Y/n]" not in out
    assert "skipped it before" in out


def test_declining_the_sign_in_on_update_is_remembered_too(box):
    box.has("codex")
    rc, out = box.run("--from-update", answers="n\n")
    assert rc == 2
    assert box.declined.exists()
    rc, out = box.run("--from-update", answers="")
    assert rc == 2 and "[Y/n]" not in out


def test_the_app_button_ignores_an_earlier_no_and_clears_it_on_success(box):
    """The button is an explicit request: no install / sign-in questions, the remembered
    "no" doesn't apply, and a finished setup forgets it."""
    box.has("npm")
    box.declined.parent.mkdir(parents=True)
    box.declined.write_text("declined")
    rc, out = box.run("--from-app", answers="")
    assert rc == 0, out
    assert "[Y/n]" not in out
    assert not box.declined.exists()


def test_the_app_button_still_asks_before_installing_node(box):
    box.has("winget")
    rc, out = box.run("--from-app", answers="n\n\n")   # no to Node; Enter for the pause
    assert rc == 3
    assert "Node.js LTS" in out
    assert "winget" not in box.log()
    assert not box.declined.exists()                    # only an update's "no" is remembered


def test_setup_installs_without_asking_first(box):
    """setup.cmd is a fresh install the user ran on purpose: the CLI just goes on; only the
    sign-in is offered."""
    box.has("npm")
    rc, out = box.run("--from-setup", answers="\n")
    assert rc == 0, out
    assert "Install the codex CLI now" not in out


def test_double_clicked_it_waits_at_the_end_so_the_result_can_be_read(box):
    box.has("codex")
    box.signed_in()
    rc, by_hand = box.run(answers="\n")
    assert rc == 0
    rc, from_setup = box.run("--from-setup")
    # pause's "Press any key" is in the Windows display language, so count it instead of
    # reading it: one extra line, and setup carries on to its own ending without it
    assert len(by_hand.strip().splitlines()) == len(from_setup.strip().splitlines()) + 1


def test_a_failed_npm_install_is_not_fatal_and_says_what_to_do(box):
    box.has("npm")
    rc, out = box.run("--from-update", answers="\n", FAKE_NPM_FAILS="1")
    assert rc == 3
    assert "npm install -g @openai/codex" in out
    assert "Claude mode works without it" in out


# ── the 1.25.0 -> 1.25.1 field report: no Node.js, a managed PC, a window that closed ──

def test_node_goes_in_per_user_first_and_winget_is_not_needed(box):
    """winget's Node.js is the machine-wide MSI, which a standard user can't approve; the
    per-user .zip (install-node.ps1) needs no admin, so it is tried first."""
    box.has("powershell", "winget")
    rc, out = box.run("--from-update", answers="\n\n\n")
    assert rc == 0, out
    log = box.log()
    assert "install-node.ps1" in log
    assert "winget" not in log
    assert log.index("powershell") < log.index("npm install")
    assert "no admin needed" in out


def test_winget_is_the_fallback_when_the_per_user_node_fails(box):
    box.has("powershell", "winget")
    rc, out = box.run("--from-update", answers="\n\n\n", FAKE_PS_FAILS="1")
    assert rc == 0, out
    log = box.log()
    assert log.index("install-node.ps1") < log.index("OpenJS.NodeJS.LTS")
    assert "may ask for admin approval" in out


def test_a_failed_per_user_node_is_not_trusted_for_an_npm_it_left(box):
    """install-node.ps1 failing is the signal, not whether some npm.cmd turned up -- and
    winget's npm, once installed, is the one found."""
    box.has("powershell", "winget")
    rc, out = box.run("--from-update", answers="\n\n\n", FAKE_PS_BROKEN="1")
    assert rc == 0, out
    log = box.log()
    assert (box.root / "local" / "Programs" / "nodejs" / "npm.cmd").exists()   # it IS there
    assert "broken-npm" not in log                   # ...and never ran
    assert log.index("install-node.ps1") < log.index("OpenJS.NodeJS.LTS") < log.index("npm install")


def test_a_failed_node_install_is_recorded_for_the_overlay(box):
    box.has("powershell")
    rc, out = box.run("--from-update", answers="\n\n", FAKE_PS_FAILS="1")
    assert rc == 3
    assert box.result.read_text().split() == ["--from-update", "3", "node-failed"]


def test_a_failed_npm_install_and_a_failed_sign_in_are_recorded(box):
    box.has("npm")
    rc, _ = box.run("--from-update", answers="\n", FAKE_NPM_FAILS="1")
    assert rc == 3
    assert box.result.read_text().split() == ["--from-update", "3", "npm-failed"]
    box.result.unlink()
    box.has("codex")
    rc, _ = box.run("--from-setup", answers="\n", FAKE_LOGIN_FAILS="1")
    assert rc == 2
    assert box.result.read_text().split() == ["--from-setup", "2", "login-failed"]


def test_a_no_is_not_a_failure_and_leaves_no_record(box):
    box.has("npm")
    rc, out = box.run("--from-update", answers="n\n", OV_UPDATE_AUTO="1")
    assert rc == 3
    assert not box.result.exists()
    assert "didn't finish" not in out                 # nothing to hold the window for


def test_a_no_to_node_is_not_a_failure_either(box):
    box.has("powershell")
    rc, out = box.run("--from-update", answers="\nn\n", OV_UPDATE_AUTO="1")
    assert rc == 3
    assert "powershell" not in box.log()
    assert not box.result.exists()
    assert "didn't finish" not in out


def test_the_app_buttons_run_reads_its_own_exit_code_and_leaves_no_record(box):
    box.has("npm")
    rc, _ = box.run("--from-app", answers="\n", FAKE_NPM_FAILS="1")
    assert rc == 3
    assert not box.result.exists()


def test_a_finished_setup_clears_an_older_failure_record(box):
    box.result.parent.mkdir(parents=True)
    box.result.write_text("--from-update 3 npm-failed\n")
    box.has("codex")
    box.signed_in()
    rc, _ = box.run("--from-app")
    assert rc == 0
    assert not box.result.exists()


def test_a_failure_holds_the_apps_update_window_open(box):
    """The app's Update button sets OV_UPDATE_AUTO and closes the window when update.cmd
    succeeds -- which it does, Codex or not. So a failed setup waits for a key itself."""
    box.has("npm")
    rc, auto = box.run("--from-update", answers="\n", FAKE_NPM_FAILS="1", OV_UPDATE_AUTO="1")
    assert rc == 3
    assert "Codex setup didn't finish" in auto
    rc, by_hand = box.run("--from-update", answers="\n", FAKE_NPM_FAILS="1")
    assert "Codex setup didn't finish" not in by_hand  # update.cmd's own ending pauses there
    assert len(auto.strip().splitlines()) > len(by_hand.strip().splitlines())


# ── the 1.25.2 field report: npm present, `npm install` printed nothing, still no codex ──

def _installs(box):
    return [l for l in box.log().splitlines() if l.startswith("npm install")]


def test_npm_is_told_to_say_what_it_does(box):
    """An npm configured silent (loglevel=silent in an .npmrc) hides its errors and its
    success alike -- the field screenshot: the "Installing it with npm" line, then straight
    to the [NOTE]. npm's own default level, passed explicitly, outranks any config."""
    box.has("npm")
    rc, out = box.run("--from-app")
    assert rc == 0, out
    assert _installs(box) == ["npm install -g @openai/codex --loglevel notice"]


def test_codex_put_where_nothing_looks_is_installed_again_where_it_is_looked_for(box):
    """npm's global folder is elsewhere (an .npmrc prefix): the install works and codex still
    isn't on PATH. Once more into %APPDATA%\\npm, which the overlay searches too."""
    box.has("npm")
    elsewhere = box.root / "elsewhere"
    rc, out = box.run("--from-app", FAKE_NPM_PREFIX=str(elsewhere))
    assert rc == 0, out
    assert (elsewhere / "codex.cmd").exists()
    assert (box.root / "appdata" / "npm" / "codex.cmd").exists()
    first, again = _installs(box)
    assert "--prefix" not in first
    assert again.endswith('--prefix "%s"' % (box.root / "appdata" / "npm"))
    assert str(elsewhere) in out                        # says where npm's folder was


def test_an_admin_only_global_folder_is_retried_in_the_users_own(box):
    """A Node.js not from the official installer: npm's global folder is Node's own, under
    Program Files, and a standard user can't write there."""
    box.has("npm")
    rc, out = box.run("--from-app", FAKE_NPM_PREFIX=str(box.root / "pf" / "nodejs"),
                      FAKE_NPM_EPERM="1")
    assert rc == 0, out
    assert len(_installs(box)) == 2
    assert (box.root / "appdata" / "npm" / "codex.cmd").exists()


def test_a_failure_in_the_usual_folder_is_not_retried_and_says_how_it_ended(box):
    """Already %APPDATA%\\npm: a second try would fail the same way. What the screenshot
    needs instead: npm's exit code, which npm ran, and on which Node."""
    box.has("npm")
    rc, out = box.run("--from-app", answers="\n", FAKE_NPM_FAILS="1")
    assert rc == 3
    assert len(_installs(box)) == 1
    assert "npm exit code 1" in out
    assert str(box.root / "bin" / "npm") in out
    assert "Node.js: not on PATH" in out


def test_a_retry_that_fails_too_ends_in_the_same_note(box):
    box.has("npm")
    rc, out = box.run("--from-update", answers="\n", FAKE_NPM_FAILS="1",
                      FAKE_NPM_PREFIX=str(box.root / "elsewhere"))
    assert rc == 3
    assert len(_installs(box)) == 2
    assert box.result.read_text().split() == ["--from-update", "3", "npm-failed"]
    assert "Claude mode works without it" in out


# ── the second 1.25.2 field report: UNABLE_TO_GET_ISSUER_CERT_LOCALLY ──────────────────
# A network that inspects HTTPS: its root certificate is in Windows' store, not in Node's.

def _npm_envs(box):
    return [l for l in box.log().splitlines() if l.startswith("npm-env")]


def test_npm_gets_the_certificates_windows_trusts(box):
    box.has("npm", "python")
    rc, out = box.run("--from-app")
    assert rc == 0, out
    pem = box.root / "local" / "claude-overlay" / "windows-ca.pem"
    assert "windowsca.py" in box.log() and pem.exists()
    assert _npm_envs(box)[0] == "npm-env SYSCA=1 EXTRA=%s" % pem


def test_it_uses_the_python_its_caller_found(box):
    """setup.cmd and update.cmd hand down %PY% (quoted -- here a folder with an & in it), the
    overlay's button OV_PYTHON; neither is the PATH python."""
    box.has("npm", "python")
    odd = box.root / "py & co"
    odd.mkdir()
    (odd / "python.cmd").write_text(FAKE_PYTHON.replace("echo python", "echo their-python"))
    rc, out = box.run("--from-setup", answers="\n", PY='"%s"' % (odd / "python.cmd"))
    assert rc == 0, out
    assert "their-python" in box.log() and "\npython " not in "\n" + box.log()
    assert _npm_envs(box)[0].endswith("windows-ca.pem")
    (box.root / "appdata" / "npm" / "codex.cmd").unlink()
    (box.root / "log.txt").unlink()
    rc, out = box.run("--from-app", OV_PYTHON=str(odd / "python.cmd"))
    assert rc == 0, out
    assert "their-python" in box.log() and "\npython " not in "\n" + box.log()


def test_certificate_settings_someone_already_made_are_kept(box):
    box.has("npm", "python")
    rc, out = box.run("--from-app", NODE_EXTRA_CA_CERTS=r"C:\it\proxy-root.pem",
                      NODE_USE_SYSTEM_CA="0")
    assert rc == 0, out
    assert "windowsca.py" not in box.log()
    assert _npm_envs(box)[0] == r"npm-env SYSCA=0 EXTRA=C:\it\proxy-root.pem"


def test_no_certificate_file_still_installs(box):
    """No Python, or one that can't write the file: Node's own store reading
    (NODE_USE_SYSTEM_CA) is still on, and the install goes ahead."""
    box.has("npm")
    rc, out = box.run("--from-app")
    assert rc == 0, out
    assert _npm_envs(box)[0] == "npm-env SYSCA=1 EXTRA="
    (box.root / "appdata" / "npm" / "codex.cmd").unlink()          # not installed again
    box.has("python")
    rc, out = box.run("--from-app", FAKE_CA_FAILS="1")
    assert rc == 0, out
    assert _npm_envs(box)[-1] == "npm-env SYSCA=1 EXTRA="


def test_setup_and_update_both_call_it():
    setup = open(os.path.join(ROOT, "setup.cmd"), encoding="ascii").read()
    update = open(os.path.join(ROOT, "update.cmd"), encoding="ascii").read()
    assert 'call "%~dp0install-codex.cmd" --from-setup' in setup
    assert 'call "%~dp0install-codex.cmd" --from-update' in update
    # the old inline copies are gone, so there is one install path to keep right
    for text in (setup, update):
        assert "npm install -g @openai/codex" not in "\n".join(
            l for l in text.splitlines() if not l.lstrip().lower().startswith("rem"))
