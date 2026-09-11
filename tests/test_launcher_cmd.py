"""Tests for the .cmd launchers -- the code that runs before any Python does.

v1.15.1 added a check that `pythonw` was a real interpreter before launching it, and the
check verified the wrong file: the `python.exe` next to it. That is a different binary,
so a machine where `python.exe` was missing or blocked had its perfectly good `pythonw`
thrown away and got `[X] No working Python was found` instead of an app. Nothing in the
Python test suite could see it, because the failure happened before Python started.

v1.15.2 fixed that and still walled machines that had Python, because it only ever looked
at PATH -- while setup.cmd installs Python into %LOCALAPPDATA%\\Programs\\Python\\Python3xx\\
and finds it there by SCANNING, since the PATH in its own window is stale. So setup could
print "[OK] The app loads." on a machine the launcher then declared Pythonless.

So the invariants here are behavioural -- the launcher is actually run against fabricated
PATHs -- rather than assertions about its text. The two that matter are the two that
broke: *the launcher must not dead-end while a usable interpreter exists*, on PATH or in
the folder setup.cmd installs into. Wording changes freely; those properties must not.

A third one was added after a user with genuinely no Python got stuck anyway: when the
launcher is right that nothing will run, it must OFFER TO RUN setup.cmd rather than print
a wall telling the reader to go double-click a different file. Everyone who reaches that
screen reached it by double-clicking the launcher (often via a Desktop shortcut, from
which the folder is never visible), so "the fix is one folder away" is not a fix. That
offer has its own failure modes -- an infinite loop, and offering a setup.cmd that was
never unzipped -- so it is pinned from all three sides below.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "Start Claude Overlay.cmd")

# The .cmd files that have to agree about where Python can be found. They are separate
# scripts on purpose (a user may copy just one out of a ZIP), so the shared block is
# duplicated text -- and duplicated text is what drifts. See the identity test below.
# setup.cmd joined in v1.16.2: it was the one script answering the question its own way
# (`py -3` from the REGISTRY, then a PATH `python`, then the scan), so on a machine where
# that search and the launcher's disagreed it installed every package into an interpreter
# the launcher never runs -- and its closing preflight proved the wrong Python loads.
SHARES_DISCOVERY = ("Start Claude Overlay.cmd", "Diagnose.cmd", "update.cmd", "setup.cmd")
BEGIN = "rem ---- BEGIN find-pythonw"
END = "rem ---- END find-pythonw"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe launchers")


def cmd_files():
    return sorted(f for f in os.listdir(ROOT) if f.lower().endswith(".cmd"))


def read(name):
    return open(os.path.join(ROOT, name), encoding="ascii").read()


def discovery_block(name):
    text = read(name)
    assert BEGIN in text and END in text, f"{name}: missing the find-pythonw markers"
    return text.split(BEGIN, 1)[1].split(END, 1)[0]


# --------------------------------------------------------------------------------------
# File-format conventions. These .cmd files are shipped and double-clicked, so the two
# things below are not cosmetic: cmd.exe reads a BOM as part of the first command, and it
# decodes the file in the machine's OEM codepage, where a non-ASCII byte can mean
# something different than it did on the author's machine.
#
# Line endings are deliberately NOT asserted. Git translates them per checkout (the
# Windows CI runner and every Windows clone get CRLF from the same LF blobs), so pinning
# them would be pinning an artifact of how the repo was cloned rather than a property of
# the shipped script -- and cmd.exe is happy with either.
# --------------------------------------------------------------------------------------

def test_every_cmd_file_is_ascii_and_bom_free():
    for name in cmd_files():
        raw = open(os.path.join(ROOT, name), "rb").read()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{name}: UTF-8 BOM"
        assert all(b < 128 for b in raw), f"{name}: non-ASCII byte"


def test_interpreter_probes_have_no_parentheses_in_the_payload():
    """cmd counts parentheses when it parses a block and does not reliably respect quotes,
    so `-c "sys.exit(0)"` inside `if ... ( ... )` can break the whole block. `-c "pass"`
    is paren-free on purpose; this keeps the next person from "improving" it."""
    for name in cmd_files():
        text = open(os.path.join(ROOT, name), encoding="ascii").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("rem ") or ' -c "' not in line:
                continue
            payload = line.split(' -c "', 1)[1].split('"', 1)[0]
            assert "(" not in payload and ")" not in payload, (
                f"{name}:{lineno} probe payload {payload!r} contains a parenthesis")


def test_every_interpreter_invocation_goes_through_call():
    """Running a .bat/.cmd from a batch file WITHOUT `call` transfers control and never
    returns, ending the script silently, mid-check. `where pythonw` really can return a
    shim -- pyenv-win and several conda wrappers install one -- so an un-`call`ed probe
    is a launcher that dies with no window and no message on those machines.

    Asserted structurally, because the failure is invisible on any machine whose Python
    is a plain .exe: it would pass every manual test the author could think to run."""
    tokens = ("python ", "pythonw ", "pyw ", "py -3 ", "%PY%", "!PY!", "%%i", "%%p",
              "%%d", "!PYW!", "!SIB!", "!RAW!")
    # `if`/`for`/`(` only decide WHETHER a command runs, so strip them and judge the
    # command underneath. Skipping such lines wholesale -- as this test first did -- left
    # every probe in the new directory-scanning block unexamined, which is precisely
    # where an un-`call`ed invocation would hide next.
    _if = r'if\s+(?:not\s+)?'
    prefixes = (_if + r'defined\s+\S+', _if + r'errorlevel\s+\d+',
                _if + r'(?:/i\s+)?\S+==\S+',
                r'for\s+/\S+\s+(?:"[^"]*"\s+)?%%\w+\s+in\s+\(.*?\)\s+do',
                r'\(')
    # `start` spawns a new process, so control transfer is not a concern there. `set`,
    # `echo`, `where` and `dir` never execute the interpreter, they only name it.
    harmless = ("call ", "start ", "set ", "echo", "where ", "dir ", "rem ", "::")
    offenders = []
    for name in cmd_files():
        for lineno, line in enumerate(read(name).splitlines(), 1):
            s = line.strip()
            while True:
                m = re.match("(?:%s)\\s*" % "|".join(prefixes), s, re.IGNORECASE)
                if not m or not m.end():
                    break
                s = s[m.end():]
            low = s.lower()
            if not s or low.startswith(harmless):
                continue
            if any(t.lower() in low for t in tokens):
                offenders.append(f"{name}:{lineno}: {s}")
    assert not offenders, "interpreter invoked without `call`:\n" + "\n".join(offenders)


def test_every_script_that_finds_python_finds_it_the_same_way():
    """Three scripts, one question. When they answered it differently the answers were
    silently inconsistent: v1.15.1's sibling-python bug shipped in all three, and
    update.cmd's copy meant an affected machine could not even refresh packages into the
    interpreter its launcher would use. They are separate files (someone may copy just
    one out of a ZIP), so the block is duplicated -- and duplicated text drifts unless
    something compares it."""
    blocks = {name: discovery_block(name) for name in SHARES_DISCOVERY}
    reference = blocks[SHARES_DISCOVERY[0]]
    for name, block in blocks.items():
        assert block == reference, (
            f"{name}'s find-pythonw block has drifted from "
            f"{SHARES_DISCOVERY[0]}'s.\n--- {name} ---\n{block}\n--- reference ---\n{reference}")


def test_the_launcher_searches_where_setup_installs():
    """The v1.15.3 defect, pinned as a contract between two files rather than as a magic
    string. setup.cmd both installs Python into this folder and scans it to find what it
    installed; a launcher that does not look there can refuse to start an install setup
    just declared healthy -- and no message on screen connects the two."""
    setup = read("setup.cmd")
    installdir = r"%LOCALAPPDATA%\Programs\Python"
    assert installdir in setup, (
        "setup.cmd no longer references %s -- update this test and the launcher "
        "together, because the point is that they agree" % installdir)
    for name in SHARES_DISCOVERY:
        assert installdir in discovery_block(name), (
            f"{name} never looks in {installdir}, which is where setup.cmd puts Python")


def test_the_launcher_never_decides_usability_from_a_different_binary():
    """The defect itself, pinned. The launcher launches `pythonw`, so only `pythonw` can
    tell it whether `pythonw` works. (`update.cmd` may still derive the sibling
    `python.exe` -- pip's output is unreadable under pythonw -- but see the test below:
    it may not let that decide whether an interpreter was found.)"""
    for name in ("Start Claude Overlay.cmd", "Diagnose.cmd"):
        text = open(os.path.join(ROOT, name), encoding="ascii").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("rem "):
                continue
            assert "pythonw.exe=python.exe" not in line, (
                f"{name}:{lineno} judges pythonw by the python.exe beside it")


def test_setup_consults_no_other_python_before_the_launchers_own_search():
    """The v1.16.1 field failure, pinned. setup.cmd used to ask "is any Python on this
    PC?" -- `py -3` (which reads the REGISTRY, PEP 514) first, then a PATH `python` --
    while the launcher asks `where pythonw` and then scans. On a managed machine holding
    both a registry-registered Python that PATH never mentions AND the standalone tree
    setup itself installs, every `pip install` went into the first and the launcher ran
    the second: the next double-click died on `import PIL` with both packages "not
    installed", right after setup printed "[OK] The app loads." about the wrong Python.
    So the PATH/py-launcher forms may appear only BELOW the shared block, as fallbacks
    for the one case where the launcher itself would fall back to them."""
    text = read("setup.cmd")
    above = [l for l in text[:text.index(BEGIN)].splitlines()
             if not l.lstrip().startswith("rem ")]
    for probe in ("py -3", "call python"):
        offenders = [l.strip() for l in above if probe in l]
        assert not offenders, (
            f"setup.cmd consults {probe!r} before the launcher's own search, so pip can "
            "target an interpreter the launcher will never run:\n" + "\n".join(offenders))


def test_setup_can_fall_back_to_pythonw_itself():
    """Same property test_update_can_fall_back_to_pythonw_itself pins for update.cmd, in
    setup.cmd's delayed-expansion-free spelling: the sibling python.exe is a preference
    (readable pip output), never a requirement -- when it does not run, the launcher's
    own pythonw is still the right environment to install into."""
    body = [l for l in read("setup.cmd").splitlines() if not l.lstrip().startswith("rem ")]
    sibling_line = next(i for i, l in enumerate(body) if "pythonw.exe=python.exe" in l)
    later = "\n".join(body[sibling_line:])
    assert 'if not defined PY if defined PYW set PY="%PYW%"' in later, (
        "setup.cmd derives the sibling python.exe but has no fallback to pythonw")


def test_setup_proves_the_app_loads_with_the_interpreter_it_installed_into():
    """The false green that hid the defect above: setup's closing `preflight.py` run must
    use %PY% -- the interpreter the packages just went into and the launcher will run --
    not some other Python that happens to answer on PATH. Structural, because on any
    one-Python machine every alternative passes for the wrong reason."""
    checks = [l.strip() for l in read("setup.cmd").splitlines()
              if "preflight.py" in l and not l.lstrip().startswith("rem ")]
    assert checks, "setup.cmd no longer proves the app loads before claiming success"
    for line in checks:
        assert line.startswith("call %PY%"), (
            f"setup.cmd verifies the app with something other than %PY%: {line}")


def test_update_does_not_run_git_pull_from_the_file_git_is_replacing():
    """`git pull` overwrites update.cmd while it is running, and cmd.exe reads the script
    it is executing from disk by byte offset -- so afterwards it resumes at that offset
    inside whatever now lives there. It came out right the two times it was watched,
    because the line numbers happened to line up. So the pull must not happen in the copy
    that git can replace: update.cmd re-execs itself out of %TEMP% first."""
    text = read("update.cmd")
    lines = text.splitlines()
    guard = next((i for i, l in enumerate(lines) if '"%~1"=="--from-temp"' in l), None)
    assert guard is not None, "update.cmd no longer re-execs itself from outside the repo"
    pull = next(i for i, l in enumerate(lines)
                if l.strip().startswith("git pull") and not l.lstrip().startswith("rem "))
    assert guard < pull, (
        f"update.cmd:{pull + 1} runs `git pull` before handing off to the copy in %TEMP% "
        f"(guard at line {guard + 1}), so git can rewrite the script mid-run again")


def test_the_post_pull_half_runs_the_freshly_pulled_file():
    """The post-pull work must be the version that was just DOWNLOADED, not the one being
    replaced -- it is the half that knows where this release looks for Python and what
    requirements.txt now pins. This used to be guaranteed by putting it in a second file
    (update-finish.cmd); it is now guaranteed by the driver calling back into the update.cmd
    sitting in the REPO, which the pull has just rewritten, in --finish mode.

    The property is the same either way and this test states it directly: the hand-off must
    name %REPO% (the pulled copy) and must NOT go through %~dp0, which for the driver is
    %TEMP% -- i.e. the pre-pull body it is trying to escape. A bare quoted filename does not
    work either: cmd looks one up as a literal program name and reports
    '"update.cmd"' is not recognized."""
    text = read("update.cmd")
    assert 'call "%REPO%\\update.cmd" --finish' in text, (
        "update.cmd's driver no longer hands off to the pulled copy in --finish mode")
    body = [l for l in text.splitlines() if not l.lstrip().startswith("rem ")]
    assert not any("%~dp0update.cmd" in l for l in body), (
        "the driver resolves the post-pull half via %~dp0, which is %TEMP% for the driver")


def test_finish_mode_is_dispatched_before_the_reexec():
    """The recursion guard, and the reason the two files could become one safely.

    --finish and the %TEMP% re-exec live in the same file now. If a --finish invocation ever
    fell through to the re-exec, the driver would pull, call --finish, land back at the top,
    copy itself to %TEMP%, pull again -- forever, with a `git pull` on every lap. Dispatch
    order is the only thing preventing that, so it is pinned rather than left to whoever
    edits the header next."""
    lines = [l.strip() for l in read("update.cmd").splitlines()
             if not l.lstrip().startswith("rem ")]
    finish = next((i for i, l in enumerate(lines) if '"%~1"=="--finish"' in l), None)
    reexec = next((i for i, l in enumerate(lines) if '"%~1"=="--from-temp"' in l), None)
    assert finish is not None, "update.cmd no longer dispatches --finish"
    assert reexec is not None, "update.cmd no longer re-execs itself from outside the repo"
    assert finish < reexec, (
        "update.cmd checks --from-temp before --finish, so a --finish run falls through "
        "into the re-exec and pulls in an infinite loop")


def test_nothing_installs_a_hardcoded_package_list():
    """requirements.txt is the single source of the version list. It used to say
    `claude-agent-sdk>=0.2.87` while setup.cmd and update.cmd both ran
    `pip install --upgrade claude-agent-sdk pillow keyboard` -- so the file constrained
    nobody, every user got whatever PyPI shipped that morning, and this machine stayed on
    the floor version. A pin that the install path ignores is decoration."""
    pattern = re.compile(r"pip\s+install[^\r\n]*?(claude-agent-sdk|pillow|keyboard)")
    offenders = []
    for name in sorted(os.listdir(ROOT)):
        if not name.lower().endswith((".cmd", ".py")) or name == os.path.basename(__file__):
            continue
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            continue
        for lineno, line in enumerate(
                open(path, encoding="utf-8").read().splitlines(), 1):
            if pattern.search(line) and "-r " not in line:
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "install command names packages instead of -r requirements.txt:\n"
        + "\n".join(offenders))


def test_ci_is_the_canary_for_new_sdk_releases():
    """Users get whatever PyPI published this morning, because requirements.txt floors the
    SDK rather than pinning it -- deliberately: the SDK drives the `claude` CLI, which
    updates itself, so freezing one half of that pair causes incompatibility instead of
    preventing it. What makes that safe is that CI installs from the same file on a clean
    runner and proves the app still loads, so a bad release is caught here first.

    Pinning the SDK would quietly disable this: CI would test a version no new user would
    ever get. So the pairing is the invariant -- floor plus a CI run against the newest
    release -- and neither half may be removed on its own."""
    ci = open(os.path.join(ROOT, ".github", "workflows", "tests.yml"),
              encoding="utf-8").read()
    assert "requirements-dev.txt" in ci, "CI no longer installs from the requirements files"
    assert "preflight.py" in ci, (
        "CI no longer runs preflight, so nothing proves the app loads against the newest SDK")

    lines = [l.strip() for l in read("requirements.txt").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    sdk = next((l for l in lines if l.lower().startswith("claude-agent-sdk")), None)
    assert sdk, "claude-agent-sdk is missing from requirements.txt"
    assert "==" not in sdk, (
        f"claude-agent-sdk is pinned ({sdk!r}). That freezes users against a `claude` CLI "
        "that keeps updating, and it stops CI from ever seeing a new SDK release. If a "
        "release genuinely broke something, add a ceiling and name the failure in a "
        "comment in requirements.txt -- do not freeze by default.")

    assert "schedule:" in ci, (
        "CI only runs on push/PR, so a breaking SDK release would first be discovered by "
        "whoever runs update.cmd next. The scheduled run is what keeps that from being a "
        "colleague.")


def test_update_can_fall_back_to_pythonw_itself():
    """update.cmd's --finish half prefers the sibling python.exe for readable pip output.
    That preference must stay a preference: if the sibling doesn't run, the launcher's own
    pythonw is still the right environment to install into, and refusing is how these
    machines got stuck un-updatable."""
    text = read("update.cmd")
    body = [l for l in text.splitlines() if not l.lstrip().startswith("rem ")]
    sibling_line = next(i for i, l in enumerate(body) if "pythonw.exe=python.exe" in l)
    later = "\n".join(body[sibling_line:])
    assert 'if not defined PY if defined PYW set PY="!PYW!"' in later, (
        "update.cmd derives the sibling python.exe but has no fallback to pythonw")


def test_update_skips_only_its_success_pause_when_the_app_started_it():
    """The overlay's one-click Update button runs update.cmd and WAITS on the process, then
    restarts itself. update.cmd's success exit ended in `pause`, so that wait lasted until
    somebody pressed a key in a console the user had not been told they owned -- the button
    sat on "Updating..." and a finished update read as a hung one.

    win32utils.run_overlay_update sets OV_UPDATE_AUTO for exactly that hand-off. What has to
    stay true: that flag skips the SUCCESS pause and nothing else. Every failure path still
    pauses, because the console is where the fix is written and a window that closes itself
    takes the fix with it."""
    body = [l.strip() for l in read("update.cmd").splitlines()
            if not l.lstrip().startswith("rem ")]
    guard = next((i for i, l in enumerate(body) if l == "if defined OV_UPDATE_AUTO ("), None)
    assert guard is not None, (
        "update.cmd no longer skips its success pause for an app-driven update -- the "
        "Update button would hang on a keypress nobody knows to make")
    block = body[guard:guard + 6]
    assert "exit /b 0" in block, "the OV_UPDATE_AUTO branch must exit, not fall into `pause`"
    # every OTHER pause is a failure path and must survive
    paused = [i for i, l in enumerate(body) if l == "pause" or l.startswith("pause &")]
    assert len(paused) >= 5, "update.cmd lost failure pauses; its errors would flash and vanish"
    assert all(i < guard for i in paused[:-1]), (
        "a failure pause moved after the OV_UPDATE_AUTO exit, where it can never be reached")


def test_no_shipped_notice_hides_a_bare_bang():
    """These scripts run with delayed expansion on, where a lone `!` in an `echo` is EATEN:
    `echo [!] x` prints `[] x`, and `^!` does not rescue it in this position either
    (measured, both). update-finish.cmd shipped several `[!]` notices that every user
    therefore read as `[]`, which looks like a typo in the product exactly when the reader
    is already stuck.

    Scoped to the scripts that actually enable delayed expansion -- setup.cmd does not, and
    its `[!]` notices render correctly, which is why the launcher runs it through a fresh
    `cmd /c` rather than inheriting this window's expansion."""
    offenders = []
    for name in cmd_files():
        text = read(name)
        if "enabledelayedexpansion" not in text.lower():
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            low = s.lower()
            # `set /p` prompt strings are displayed to the user too, and delayed
            # expansion eats their bangs the same way an echo's are eaten.
            if not (low.startswith("echo") or low.startswith("set /p")):
                continue
            # Strip the LEGITIMATE expansions (!VAR!, !VAR:a=b!, !VAR:~0,1!) and flag any
            # `!` left over. Skipping the whole line because one legit expansion exists
            # would hide a bare `!` riding on the same line (`echo done with !PY! [!]`).
            residue = re.sub(r"![A-Za-z_][A-Za-z0-9_]*(?::[^!]*)?!", "", s)
            if "!" in residue:
                offenders.append(f"{name}:{lineno}: {s}")
    assert not offenders, (
        "notice contains a bare `!` that delayed expansion will delete before the user "
        "sees it:\n" + "\n".join(offenders))


# --------------------------------------------------------------------------------------
# Behavioural: run the real launcher against a fabricated PATH.
# --------------------------------------------------------------------------------------

def _working_shim():
    """A `pythonw.bat` that really runs Python -- i.e. exactly the shape pyenv-win and
    several conda wrappers put on PATH, which is also the shape that used to kill the
    launcher outright when the probe was not `call`ed.

    Copying a real pythonw.exe into the temp dir would be more literal, but a
    freshly-written .exe is refused outright on locked-down machines ("Access is
    denied."), and a test that depends on the local endpoint-protection mood is worse
    than no test.

    The last two lines matter: `start` runs a batch file as `cmd /K`, so the console
    would stay open forever and leak a window per run. The probe passes `-c` and needs
    control back (`exit /b`); the real launch gets the script path and closes its window
    (`exit`)."""
    return (f'@"{sys.executable}" %*\n'
            '@if "%~1"=="-c" exit /b %errorlevel%\n'
            '@exit %errorlevel%\n')


def _sandbox(tmp_path, shim_dir, shim_body=None):
    """A copy of the launcher next to a stub app, plus one `pythonw` on a bare PATH.

    `shim_body` defaults to a shim that works; pass one that fails for the cases where
    the launcher is supposed to refuse."""
    app = tmp_path / "app"
    app.mkdir()
    marker = tmp_path / "LAUNCHED.txt"
    (app / "claude_overlay.py").write_text(
        "import os, sys\n"
        "p = os.environ.get('OVERLAY_MARKER')\n"
        "open(p, 'w').write(sys.executable) if p else None\n", encoding="ascii")
    (app / "Start Claude Overlay.cmd").write_bytes(open(LAUNCHER, "rb").read())

    shim = tmp_path / shim_dir
    shim.mkdir(parents=True)
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    (shim / "pythonw.bat").write_text(shim_body or _working_shim(), encoding="ascii")

    # A bare PATH: System32 only, so `where` and friends still resolve but no real
    # python / py / pyw can rescue the run and blur what is being tested.
    env["PATH"] = f"{shim};{os.path.join(os.environ['SystemRoot'], 'System32')}"
    env["OVERLAY_MARKER"] = str(marker)

    # The launcher also searches OFF PATH now, so point every one of those roots at an
    # empty folder. Otherwise the author's own C:\Python3xx would satisfy the tests that
    # assert a refusal, and they would pass for a reason that has nothing to do with the
    # case under test -- on the CI runner, where no such folder exists, they would fail.
    for var in ("LOCALAPPDATA", "ProgramFiles", "SystemDrive"):
        blank = tmp_path / ("no_" + var.lower())
        blank.mkdir(exist_ok=True)
        env[var] = str(blank)
    return app, marker, env


_EDR_REFUSAL = "Access is denied."


def _run(app, env, tmp_path, answer=None, script="Start Claude Overlay.cmd", args=()):
    """Collect output through a FILE, not a pipe: the launcher's `start` hands its stdout
    handle to the process it spawns, so waiting for pipe EOF would mean waiting for the
    overlay itself to exit.

    `script`/`args` exist so update.cmd's post-pull half can be driven through the same
    endpoint-protection retry below rather than a second copy of it.

    `answer` is typed at whatever the launcher prompts for. Leaving it None is a distinct
    case rather than "no answer": with stdin at EOF cmd's `set /p` leaves the variable
    holding the default it was given, which is the same path a real user takes by
    pressing Enter. Both are worth exercising, and only one of them can be typed.

    The retry is for endpoint protection, not for flaky assertions. Some managed Windows
    machines refuse to execute a .cmd that was written seconds ago until a scan finishes,
    and answer with exactly "Access is denied." and nothing else. That string is
    unambiguous -- the launcher never produces it -- so retrying on it cannot paper over a
    real failure, and if it never clears the test FAILS rather than skipping."""
    log = tmp_path / "out.txt"
    keyed = tmp_path / "stdin.txt"
    if answer is not None:
        keyed.write_text(answer, encoding="ascii")
    for attempt in range(6):
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            stdin = open(str(keyed), "rb") if answer is not None else None
            try:
                p = subprocess.run(
                    ["cmd", "/c", "call", str(app / script), *args],
                    cwd=str(app), env=env,
                    stdin=stdin if stdin is not None else subprocess.DEVNULL,
                    stdout=fh, stderr=subprocess.STDOUT, timeout=90)
            finally:
                if stdin is not None:
                    stdin.close()
        out = log.read_text(encoding="utf-8", errors="replace")
        if out.strip() != _EDR_REFUSAL:
            return p.returncode, out
        time.sleep(0.5 * (attempt + 1))
    pytest.fail("endpoint protection kept refusing to run the copied launcher "
                f"({_EDR_REFUSAL!r}) -- the launcher itself was never reached")


def _pythonless(tmp_path):
    """A sandbox where nothing anywhere will run Python -- the state a fresh Windows 11
    box is in, where `where python` still answers with the App-execution-alias stub."""
    app, marker, env = _sandbox(tmp_path, "empty", "@exit /b 9009\n")
    os.remove(tmp_path / "empty" / "pythonw.bat")
    return app, marker, env


# The line the launcher prints just before it asks. Asserted instead of the `set /p`
# prompt itself, which is a plain `echo` either way and does not depend on how cmd
# routes prompt text when stdout is redirected.
OFFER = "setup.cmd fixes this"


def _stub_setup(app, tmp_path, env):
    """A setup.cmd that records that it ran and nothing else. A test cannot install
    Python; what has to be pinned is that the launcher REACHES setup instead of telling
    the user to go and find it.

    Before handing the stub back, run it once against a throwaway marker until the
    machine actually executes it. Endpoint protection on managed machines refuses to run
    a .cmd written seconds ago ("Access is denied."), and when the refusal lands on this
    INNER spawn it sits in the middle of the launcher's own output where _run's
    whole-output retry cannot see it -- the launcher then truthfully reports that setup
    fixed nothing, and the test blames the launcher for a stub the OS never ran. Warming
    the exact file caches the verdict before the case under test needs it. Same rule as
    _run: if the refusal never clears this FAILS rather than skipping."""
    ran = tmp_path / "SETUP_RAN.txt"
    stub = app / "setup.cmd"
    stub.write_text(
        "@echo off\n"
        "echo stub setup ran\n"
        '>"%OVERLAY_SETUP_MARKER%" echo ran\n', encoding="ascii")
    env["OVERLAY_SETUP_MARKER"] = str(ran)
    warmed = tmp_path / "SETUP_WARM.txt"
    warm_env = dict(env, OVERLAY_SETUP_MARKER=str(warmed))
    for attempt in range(6):
        subprocess.run(
            ["cmd", "/c", "call", str(stub)], cwd=str(app), env=warm_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30)
        if warmed.exists():
            return ran
        time.sleep(0.5 * (attempt + 1))
    pytest.fail("endpoint protection kept refusing to run the freshly written stub "
                f"setup.cmd ({_EDR_REFUSAL!r}) -- the case under test was never reachable")


def _wait_for(marker, seconds=20):
    """Non-empty, not merely present. The stub app creates the marker and writes to it as
    two separate steps, so "the file exists" is true for a moment before the interpreter
    path is in it -- and a caller that then reads the file gets "" and reports that the
    launcher started the wrong Python. That fired once in ~20 runs and accused the
    launcher of a bug it does not have, which is worse than no test."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if marker.exists() and marker.stat().st_size > 0:
            return True
        time.sleep(0.1)
    return False


@windows_only
def test_a_pythonw_with_no_python_exe_beside_it_still_launches(tmp_path):
    """THE regression. This PATH has a working `pythonw` and nothing else -- no
    `python.exe` sibling, no `py`, no `pyw`. v1.15.1 printed "No working Python was
    found" here and started nothing."""
    app, marker, env = _sandbox(tmp_path, "onlypythonw")
    rc, out = _run(app, env, tmp_path)
    assert _wait_for(marker), f"launcher refused a usable pythonw\n--- output ---\n{out}"


@windows_only
def test_the_app_execution_alias_stub_is_still_refused(tmp_path):
    """The case the check was added for, which must keep working: a `pythonw` under
    \\WindowsApps\\ that runs nothing must NOT be launched blind, and the user must be
    told what was found rather than left with a window that never appears."""
    app, marker, env = _sandbox(
        tmp_path, os.path.join("Microsoft", "WindowsApps"), "@exit /b 9009\n")
    rc, out = _run(app, env, tmp_path)
    assert not _wait_for(marker, seconds=3), "launched the alias stub"
    assert "Could not start Claude Overlay" in out
    assert "WindowsApps" in out, (
        "the failure message must show what it actually found, not just that it failed\n"
        f"--- output ---\n{out}")


@windows_only
def test_no_python_at_all_reports_what_it_looked_at(tmp_path):
    app, marker, env = _pythonless(tmp_path)
    rc, out = _run(app, env, tmp_path)
    assert not _wait_for(marker, seconds=3)
    assert "Could not start Claude Overlay" in out
    assert "(nothing found)" in out, f"give-up path printed no evidence\n{out}"
    # "No Python on PATH" and "no Python on this PC" need different fixes, and the report
    # is the only thing that separates them for whoever is reading it.
    assert "OFF PATH" in out, f"report cannot distinguish PATH from install\n{out}"


@windows_only
def test_the_launcher_runs_setup_for_you_rather_than_naming_it(tmp_path):
    """The dead end, pinned. A machine with no Python is setup.cmd's job, and the person
    looking at this screen got here by double-clicking the launcher -- very often from a
    Desktop shortcut, from which setup.cmd is not visible and not findable. Printing its
    name is not a fix for them; running it is.

    Pressing Enter accepts, which is what an empty stdin reproduces here.

    The `== 1` is the loop guard, and it is the reason this is not just a smoke test:
    the launcher re-runs its whole search after setup so it can find an interpreter that
    PATH will never mention, and a re-search that fails lands back on this very screen.
    Without a latch that is an infinite offer, and the stub setup used here fixes
    nothing -- so this is exactly the shape that would spin."""
    app, marker, env = _pythonless(tmp_path)
    ran = _stub_setup(app, tmp_path, env)
    rc, out = _run(app, env, tmp_path)
    assert ran.exists(), (
        "the launcher named setup.cmd but never ran it, so a user who cannot find that "
        f"file is still stuck\n--- output ---\n{out}")
    assert out.count(OFFER) == 1, (
        f"setup was offered {out.count(OFFER)} times; it must be offered at most once, "
        f"or a setup that does not fix the problem loops forever\n--- output ---\n{out}")


@windows_only
def test_the_launcher_does_not_offer_a_setup_it_does_not_have(tmp_path):
    """Someone who copied one file out of the ZIP has no setup.cmd. Offering to run it
    would replace a useless message with a broken one, so the offer is conditional on the
    file existing -- and the manual instructions have to survive that branch."""
    app, marker, env = _pythonless(tmp_path)
    assert not (app / "setup.cmd").exists(), "test premise: this sandbox has no setup.cmd"
    rc, out = _run(app, env, tmp_path)
    assert OFFER not in out, f"offered to run a setup.cmd that is not there\n{out}"
    assert "Manual alternative" in out, (
        f"dropped the instructions along with the offer\n--- output ---\n{out}")


@windows_only
def test_declining_the_offer_leaves_the_manual_instructions(tmp_path):
    """"n" has to mean no. It also has to still answer the question the user came with,
    so declining lands on the same instructions the offer replaced rather than on
    nothing."""
    app, marker, env = _pythonless(tmp_path)
    ran = _stub_setup(app, tmp_path, env)
    rc, out = _run(app, env, tmp_path, answer="n\n")
    assert not ran.exists(), f"ran setup.cmd after the user declined\n--- output ---\n{out}"
    assert "Manual alternative" in out, (
        f"declining left the user with no instructions at all\n--- output ---\n{out}")


# --------------------------------------------------------------------------------------
# Behavioural: update.cmd's post-pull half, on a machine where nothing will run Python.
#
# This branch used to print "Double-click setup.cmd" and stop -- the same dead end the
# launcher had, and reached by the same route: everybody who sees it double-clicked a file
# from a folder they cannot see. It mattered more here, because update.cmd never installs
# Python itself, so "run Update again" could not clear the wall however many times a stuck
# user tried it. On a managed corporate machine whose device policy blocks PSF-signed Python
# installers outright, this is the only branch an affected user ever reaches.
# --------------------------------------------------------------------------------------

def _pythonless_update(tmp_path):
    """update.cmd in a sandbox where nothing anywhere will run Python.

    --finish is invoked directly, and that is also what proves the mode dispatch works: this
    folder is not a git clone, so a --finish run that fell through into the driver would
    complain about git and never reach the Python search at all."""
    app, _marker, env = _pythonless(tmp_path)
    shutil.copyfile(os.path.join(ROOT, "update.cmd"), str(app / "update.cmd"))
    # Present so that a run which gets PAST the Python search cannot stop on this instead
    # and be mistaken for the refusal under test.
    (app / "requirements.txt").write_text("pillow\n", encoding="ascii")
    return app, env


def _run_finish(app, env, tmp_path, answer=None):
    return _run(app, env, tmp_path, answer=answer, script="update.cmd", args=("--finish",))


@windows_only
def test_update_runs_setup_for_you_rather_than_naming_it(tmp_path):
    """The dead end, pinned on the update path too. Pressing Enter accepts, which is what
    an empty stdin reproduces here.

    `== 1` is the loop guard and the reason this is more than a smoke test: --finish re-runs
    its whole search after setup, so it can find an interpreter PATH will never mention, and
    a re-search that fails lands back on this very screen. The stub setup fixes nothing, so
    without a latch this is exactly the shape that would spin -- and each lap of the
    unlatched version would be a fresh `git pull`."""
    app, env = _pythonless_update(tmp_path)
    ran = _stub_setup(app, tmp_path, env)
    rc, out = _run_finish(app, env, tmp_path)
    assert ran.exists(), (
        "update.cmd named setup.cmd but never ran it, so a user who cannot find that file "
        f"is still stuck\n--- output ---\n{out}")
    assert out.count(OFFER) == 1, (
        f"setup was offered {out.count(OFFER)} times; it must be offered at most once\n"
        f"--- output ---\n{out}")


@windows_only
def test_update_does_not_claim_success_when_the_packages_were_skipped(tmp_path):
    """The false-success guard. The old post-pull half printed its warning and then carried
    on to the "[OK] Updated" banner and exit 0, so an update that left the app unable to
    start reported exactly the same thing as one that worked. The pull genuinely did
    succeed, so the message has to say so -- but the exit status must not."""
    app, env = _pythonless_update(tmp_path)
    _stub_setup(app, tmp_path, env)
    rc, out = _run_finish(app, env, tmp_path)
    assert "[OK] Updated" not in out, (
        f"claimed the update was complete with no interpreter to run it\n{out}")
    assert rc != 0, f"exited 0 after skipping the packages\n--- output ---\n{out}"
    assert "code IS updated" in out, (
        f"did not tell the user the pull itself succeeded\n--- output ---\n{out}")


@windows_only
def test_update_does_not_offer_a_setup_it_does_not_have(tmp_path):
    """Someone who copied one file out of the ZIP has no setup.cmd; offering to run it would
    replace a useless message with a broken one. The manual instructions have to survive
    that branch."""
    app, env = _pythonless_update(tmp_path)
    assert not (app / "setup.cmd").exists(), "test premise: this sandbox has no setup.cmd"
    rc, out = _run_finish(app, env, tmp_path)
    assert OFFER not in out, f"offered to run a setup.cmd that is not there\n{out}"
    assert "Manual alternative" in out, (
        f"dropped the instructions along with the offer\n--- output ---\n{out}")


@windows_only
def test_update_declining_the_offer_leaves_the_manual_instructions(tmp_path):
    """"n" has to mean no, and still has to answer the question the user came with."""
    app, env = _pythonless_update(tmp_path)
    ran = _stub_setup(app, tmp_path, env)
    rc, out = _run_finish(app, env, tmp_path, answer="n\n")
    assert not ran.exists(), f"ran setup.cmd after the user declined\n--- output ---\n{out}"
    assert "Manual alternative" in out, (
        f"declining left the user with no instructions at all\n--- output ---\n{out}")


def _fabricate_offpath_python(tmp_path):
    """A real, working interpreter at the exact path setup.cmd installs to, reachable
    ONLY by scanning that folder -- never through PATH.

    It is a copy of pythonw.exe with its DLLs plus a `pythonw._pth` naming the real
    stdlib, which is the documented way to pin a relocated interpreter's search path.
    Copying the whole install would be more literal and costs ~100 MB per run; a junction
    to the real one would let a stray rmtree walk into somebody's Python installation.

    `sys.base_prefix`, not `dirname(sys.executable)`: inside a virtualenv the latter is
    `.../Scripts`, whose `pythonw.exe` is a stub that redirects to the base interpreter.
    Copying that stub next to a `._pth` produced a process that hung until the timeout --
    a test failure that said nothing about the launcher."""
    home = sys.base_prefix
    pyw = os.path.join(home, "pythonw.exe")
    if not os.path.exists(pyw):
        pytest.fail(f"test premise unavailable: no pythonw.exe in {home}")
    dst = tmp_path / "lad" / "Programs" / "Python" / os.path.basename(home)
    dst.mkdir(parents=True)
    shutil.copy2(pyw, str(dst / "pythonw.exe"))
    for dll in (glob.glob(os.path.join(home, "python3*.dll"))
                + glob.glob(os.path.join(home, "vcruntime*.dll"))):
        shutil.copy2(dll, str(dst))
    (dst / "pythonw._pth").write_text(
        "%s\n%s\n%s\n" % (os.path.join(home, "Lib"), os.path.join(home, "DLLs"),
                          os.path.join(home, "Lib", "site-packages")), encoding="ascii")
    return dst / "pythonw.exe"


@windows_only
def test_python_where_setup_installs_it_but_not_on_path_still_launches(tmp_path):
    """THE v1.15.3 regression, reproduced end to end: PATH holds nothing but dead
    App-execution-alias stubs, and a working Python sits where setup.cmd puts it.
    v1.15.2 printed "no Python on PATH ran" here and started nothing -- on a machine
    where setup.cmd had already reported success."""
    app, marker, env = _sandbox(
        tmp_path, os.path.join("Microsoft", "WindowsApps"), "@exit /b 9009\n")
    fabricated = _fabricate_offpath_python(tmp_path)
    try:
        rc = subprocess.run([str(fabricated), "-c", "pass"], timeout=30).returncode
    except subprocess.TimeoutExpired:
        rc = "hung"
    if rc != 0:
        pytest.fail("test premise unavailable: the relocated interpreter at "
                    f"{fabricated} came back {rc!r}, so this test cannot say anything "
                    "about the launcher either way")

    env["LOCALAPPDATA"] = str(tmp_path / "lad")
    # Neutralise the launcher's OTHER off-PATH candidates, so only the folder setup.cmd
    # installs into can rescue this run and a pass cannot come from the test machine
    # happening to have C:\Python3xx.
    env["ProgramFiles"] = str(tmp_path / "noprogs")
    env["SystemDrive"] = str(tmp_path / "nodrive")

    rc, out = _run(app, env, tmp_path)
    assert _wait_for(marker), (
        "launcher walled a machine whose Python is exactly where setup.cmd puts it"
        f"\n--- output ---\n{out}")
    assert marker.read_text().lower() == str(fabricated).lower(), (
        f"launched something other than the discovered interpreter: {marker.read_text()}")


# ---------------------------------------------------------------------------------
# Behavioural: WHERE update.cmd pulls from, and what it refuses to pull onto.
#
# Through v1.17.0 the pull was a bare `git pull`, which pulls the current branch from ITS
# tracking remote. On a clone that is a FORK that remote is the fork -- and a fork is
# exactly as stale as the install, while the version check asks GitHub for the newest tag
# on the SOURCE repo. So the overlay correctly announced a new release and every pull
# behind the one-click button was a no-op against a fork that did not have it: the update
# reported success, changed nothing, and the notice came back on the next launch. Nothing
# in the text of the script looked wrong, which is why these run the real update.cmd
# against real git repositories instead of reading it.
#
# The driver copy is kept OUTSIDE the sandbox and the sandbox holds a stub update.cmd for
# the --finish hand-off, so the pull half runs for real without installing anything. The
# stub is untracked on purpose: no pull can replace it, and the dirty-tree guard must not
# trip over it either, since a pull never overwrites untracked files.
# ---------------------------------------------------------------------------------

_GIT_ID = ("-c", "user.email=t@example.invalid", "-c", "user.name=t",
           "-c", "commit.gpgsign=false")


def _git(cwd, *args):
    p = subprocess.run(("git",) + args, cwd=str(cwd), capture_output=True,
                       text=True, timeout=90)
    assert p.returncode == 0, f"git {' '.join(args)} in {cwd}:\n{p.stdout}{p.stderr}"
    return p.stdout


def _release(repo, version):
    (repo / "config.py").write_text(f'__version__ = "{version}"\n', encoding="ascii")
    _git(repo, "add", "-A")
    _git(repo, *_GIT_ID, "commit", "-qm", f"v{version}")


def _pull_sandbox(tmp_path, forked):
    """upstream -> (fork) -> app, with upstream one release AHEAD: the shape the bug needed.

    `forked` picks which install is being reproduced. True is this developer's: origin is a
    personal fork stuck a release back, and `upstream` is where releases come from. False is
    every ordinary install, where origin IS the source repo and there is no upstream at all;
    it is here so that fixing the fork case cannot quietly break the common one."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _release(up, "1.0.0")
    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(up), str(fork))      # and never updated again
    app = tmp_path / "app"
    _git(tmp_path, "clone", "-q", str(fork if forked else up), str(app))
    _release(up, "2.0.0")                                  # the release the fork lacks
    if forked:
        _git(app, "remote", "add", "upstream", str(up))
    (app / "update.cmd").write_text(
        '@echo off\r\n>"%~dp0finished.txt" echo reached\r\nexit /b 0\r\n', encoding="ascii")
    driver = tmp_path / "driver"
    driver.mkdir()
    shutil.copy(os.path.join(ROOT, "update.cmd"), str(driver / "update.cmd"))
    return driver, app, dict(os.environ)


def _run_pull(tmp_path, driver, app, env):
    return _run(driver, env, tmp_path, script="update.cmd",
                args=("--from-temp", str(app)))


def _installed(app):
    return (app / "config.py").read_text(encoding="ascii").strip()


@windows_only
@pytest.mark.parametrize("forked", [True, False])
def test_update_pulls_from_the_repo_the_version_check_reads(tmp_path, forked):
    """THE v1.17.0 bug, reproduced end to end for the fork, and pinned for the plain install
    so the fix cannot be a fork-only special case."""
    driver, app, env = _pull_sandbox(tmp_path, forked)
    rc, out = _run_pull(tmp_path, driver, app, env)
    assert "2.0.0" in _installed(app), (
        "update.cmd did not bring in the release the version check is announcing "
        f"(still {_installed(app)!r}) -- an update that succeeds and changes nothing is "
        f"the one failure that reports itself as success\n--- output ---\n{out}")
    assert (app / "finished.txt").exists(), (
        f"pull succeeded but never handed off to the post-pull half\n{out}")
    assert rc == 0, f"exited {rc} after a clean update\n--- output ---\n{out}"


@windows_only
def test_update_refuses_to_merge_a_release_into_a_topic_branch(tmp_path):
    """Releases are cut on main. Pulling main onto a topic branch would merge the release
    into somebody's unfinished work and leave them the merge -- decided by a script running
    unattended behind a button, which is not a decision it gets to make."""
    driver, app, env = _pull_sandbox(tmp_path, forked=True)
    _git(app, "checkout", "-q", "-b", "some/work")
    # ...and give it a tracking remote, which is what makes this a test. A bare `git pull`
    # on a branch with nowhere to pull FROM errors by itself, so a topic branch without one
    # would pass this against the very script that had the bug.
    _git(app, "push", "-q", "-u", "origin", "some/work")
    rc, out = _run_pull(tmp_path, driver, app, env)
    assert "1.0.0" in _installed(app), f"pulled onto a topic branch anyway\n{out}"
    assert not (app / "finished.txt").exists(), f"carried on past the refusal\n{out}"
    assert rc != 0, "reported success without updating anything"
    assert "some/work" in out, (
        f"refused without naming the branch the user has to leave\n--- output ---\n{out}")


@windows_only
def test_update_refuses_a_dirty_tree_before_the_pull_gets_half_way(tmp_path):
    """git's own refusal arrives PART WAY THROUGH and names the files without saying what to
    do about them. The check has to come first, and it has to name the way out."""
    driver, app, env = _pull_sandbox(tmp_path, forked=True)
    (app / "config.py").write_text('__version__ = "1.0.0"\n# local edit\n', encoding="ascii")
    rc, out = _run_pull(tmp_path, driver, app, env)
    assert "# local edit" in (app / "config.py").read_text(encoding="ascii"), (
        f"the update overwrote uncommitted work\n--- output ---\n{out}")
    assert not (app / "finished.txt").exists(), f"carried on past the refusal\n{out}"
    assert rc != 0, "reported success without updating anything"
    assert "git stash" in out, (
        f"refused without telling the user how to proceed\n--- output ---\n{out}")


@windows_only
def test_update_never_makes_a_merge_commit_on_its_own(tmp_path):
    """A clone carrying local commits on main cannot fast-forward onto the release. Without
    --ff-only that becomes a merge commit authored by an unattended script; the user has to
    be the one who decides to merge."""
    driver, app, env = _pull_sandbox(tmp_path, forked=True)
    _release(app, "1.0.0-local")
    before = _git(app, "rev-parse", "HEAD").strip()
    rc, out = _run_pull(tmp_path, driver, app, env)
    assert _git(app, "rev-parse", "HEAD").strip() == before, (
        f"update.cmd rewrote the history of a clone it could not fast-forward\n{out}")
    assert not (app / "finished.txt").exists(), f"carried on past the refusal\n{out}"
    assert rc != 0, "reported success without updating anything"
