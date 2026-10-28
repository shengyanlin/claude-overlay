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
if "%1"=="login" echo yes> "%FAKE_ROOT%\signed-in"
exit /b 0
"""
# `npm install -g @openai/codex` drops a codex shim into npm's global bin (%APPDATA%\npm).
FAKE_NPM = r"""@echo off
echo npm %*>> "%FAKE_LOG%"
if "%FAKE_NPM_FAILS%"=="1" exit /b 1
copy /y "%FAKE_ROOT%\codex.src" "%APPDATA%\npm\codex.cmd" >nul
exit /b 0
"""
# `winget install ... OpenJS.NodeJS.LTS` puts npm into %ProgramFiles%\nodejs.
FAKE_WINGET = r"""@echo off
echo winget %*>> "%FAKE_LOG%"
copy /y "%FAKE_ROOT%\npm.src" "%ProgramFiles%\nodejs\npm.cmd" >nul
exit /b 0
"""


@pytest.fixture
def box(tmp_path):
    """An isolated machine: nothing installed until a test says so."""
    for d in ("appdata/npm", "pf/nodejs", "local", "bin"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "codex.src").write_text(FAKE_CODEX)
    (tmp_path / "npm.src").write_text(FAKE_NPM)
    (tmp_path / "winget.src").write_text(FAKE_WINGET)
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

        def has(self, *tools):
            for t in tools:
                src = {"codex": "codex.src", "npm": "npm.src", "winget": "winget.src"}[t]
                (tmp_path / "bin" / f"{t}.cmd").write_text((tmp_path / src).read_text())

        def signed_in(self):
            (tmp_path / "signed-in").write_text("yes")

        def run(self, *args, answers="", **extra):
            e = dict(env, **extra)
            p = subprocess.run(["cmd", "/c", SCRIPT, *args], input=answers, env=e,
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


def test_setup_and_update_both_call_it():
    setup = open(os.path.join(ROOT, "setup.cmd"), encoding="ascii").read()
    update = open(os.path.join(ROOT, "update.cmd"), encoding="ascii").read()
    assert 'call "%~dp0install-codex.cmd" --from-setup' in setup
    assert 'call "%~dp0install-codex.cmd" --from-update' in update
    # the old inline copies are gone, so there is one install path to keep right
    for text in (setup, update):
        assert "npm install -g @openai/codex" not in "\n".join(
            l for l in text.splitlines() if not l.lstrip().lower().startswith("rem"))
