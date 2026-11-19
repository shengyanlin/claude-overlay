"""The macOS scripts — update.command, setup.command, install-codex.sh, mac-app.sh and the
launcher — run for real under bash, the way test_launcher_cmd.py / test_install_codex_cmd.py run the
Windows ones under cmd.

Nothing here reaches the network or the real machine: HOME is a temp folder (so the state
files and "the Desktop" are too), the app folder's Python is a recording stand-in, and
OV_TEST_PATH decides which CLIs exist. Skipped off macOS."""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS shell scripts")

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ("update.command", "setup.command", "install-codex.sh", "mac-app.sh",
           "mac-common.sh", "Start Claude Overlay.command", "Diagnose.command",
           "Create Desktop Shortcut.command")
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _exe(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _app_folder(folder):
    """A folder holding the real scripts plus the few files they look for."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copy2(REPO / name, folder / name)
    for name in ("requirements.txt", "requirements-docs.txt", "config.py",
                 "claude_overlay_2.png"):
        shutil.copy2(REPO / name, folder / name)
    (folder / "preflight.py").write_text("")
    (folder / "claude_overlay.py").write_text("")
    return folder


def _fake_python(folder, rc=0):
    """A stand-in for .venv/bin/python that records how it was called and succeeds."""
    # Recorded relative to the script itself: the folder's name may hold quotes.
    return _exe(folder / ".venv" / "bin" / "python",
                f'echo "$@" >> "$(dirname "$0")/../../py-calls.txt"\nexit {rc}\n')


def _bundle(folder):
    """The launcher bundle in `folder`, and the two files it cannot open without."""
    app = folder / "Claude Overlay.app"
    return app, app / "Contents" / "MacOS" / "ClaudeOverlay", app / "Contents" / "Info.plist"


def _snapshot(app):
    """Every file in a bundle, byte for byte, with its mode - signature included."""
    return {str(f.relative_to(app)): (f.read_bytes(), f.stat().st_mode)
            for f in sorted(app.rglob("*")) if f.is_file()}


def _stats(app):
    """What shows a bundle was not replaced, re-signed or even touched: which file each
    thing in it is, and when it was last written - the bundle folder itself included."""
    return {str(f.relative_to(app)): (f.lstat().st_ino, f.lstat().st_mtime_ns)
            for f in [app, *sorted(app.rglob("*"))]}


def _stages(folder):
    """What mac-app.sh builds in, beside the bundle. None may outlive a run."""
    return sorted(f.name for f in folder.iterdir() if f.name.startswith(".claude-overlay-app."))


ICON = "Contents/Resources/AppIcon.icns"
SEAL = "Contents/_CodeSignature/CodeResources"        # there once codesign has signed it
STALE = "# written by an older release\n"


def _outdated(folder):
    """Make the launcher in `folder` one a build has to replace - its script is not what
    this release writes. (One that is current is left alone, and so shows nothing.)"""
    with _bundle(folder)[1].open("a") as f:
        f.write(STALE)


def _icon_tools(folder, env):
    """Let the icon step "work" (the real one needs Pillow): a Python that runs, and an
    iconutil that writes the file it is asked for. A bundle built with these is complete:
    launcher, Info.plist, icon - and codesign's signature."""
    _fake_python(folder)
    env.cli("iconutil", 'while [ $# -gt 1 ]; do [ "$1" = -o ] && echo icns > "$2"; shift; done\n')


# Stand-ins for the tools mac-app.sh calls (the bin folder is first on PATH), each
# failing at one step of the build and passing everything else through.
NO_CHMOD = "exit 1\n"                                # the launcher can't be made executable
NO_PLIST = ('data="$(/bin/cat "$@"; echo .)"\n'       # the write of Info.plist fails
            'case "$data" in *"<plist"*) exit 1 ;; esac\nprintf %s "${data%.}"\n')
NO_SWAP_IN = ('case "$1" in */.claude-overlay-app.*/"Claude Overlay.app") exit 1 ;; esac\n'
              'exec /bin/mv "$@"\n')                  # the new bundle can't take its name
NO_SWAP_NO_UNDO = ('case "$2" in */"Claude Overlay.app") exit 1 ;; esac\n'
                   'exec /bin/mv "$@"\n')             # ...and the old one can't go back
# $PPID is mac-app.sh itself: a signal while it builds, and one in the middle of the swap.
STOPPED_BUILDING = 'kill -{sig} $PPID\nexec /bin/chmod "$@"\n'
STOPPED_SWAPPING = 'case "$2" in */previous) kill -{sig} $PPID ;; esac\nexec /bin/mv "$@"\n'
SIGNALS = ["HUP", "INT", "TERM"]                      # window closed, Ctrl-C, kill


class Env:
    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        (self.home / "Desktop").mkdir(parents=True)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = self.home / "Library" / "Application Support" / "claude-overlay"

    def cli(self, name, body="exit 0\n"):
        return _exe(self.bin / name, body)

    def run(self, script, *args, **extra):
        env = {"HOME": str(self.home), "OV_TEST_PATH": f"{self.bin}:/usr/bin:/bin",
               "PATH": f"{self.bin}:/usr/bin:/bin", "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        env.update(extra)
        return subprocess.run(["/bin/bash", str(script), *args], env=env, text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=120)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ── every script at least parses ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_is_valid_bash(name):
    r = subprocess.run(["/bin/bash", "-n", str(REPO / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", [n for n in SCRIPTS if n != "mac-common.sh"])
def test_double_clickable_scripts_are_executable(name):
    # Finder refuses to run a .command without the execute bit ("you do not have
    # appropriate access privileges"), and git records that bit - so it must be committed.
    assert os.access(REPO / name, os.X_OK), f"{name} lost its execute bit"


# ── update.command ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def clone(tmp_path, env):
    """An "upstream" with one release, and a clone of it one release behind. The clone's
    folder name has a space, quotes and an ampersand in it on purpose."""
    src = _app_folder(tmp_path / "src")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run(GIT + ["add", "-A"], cwd=src, check=True)
    subprocess.run(GIT + ["commit", "-q", "-m", "1.0"], cwd=src, check=True)
    dest = tmp_path / "My \"Apps\" & Tools" / "overlay"
    dest.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(src), str(dest)], check=True)
    (src / "NEW.txt").write_text("the next release\n")
    subprocess.run(GIT + ["add", "-A"], cwd=src, check=True)
    subprocess.run(GIT + ["commit", "-q", "-m", "1.1"], cwd=src, check=True)
    env.cli("codex")                      # installed and signed in: not what's under test
    return dest


def test_update_pulls_installs_and_reports_success(clone, env):
    _fake_python(clone)
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "NEW.txt").exists()                               # the pull landed
    calls = (clone / "py-calls.txt").read_text()
    assert f"-m pip install --upgrade -r {clone}/requirements.txt" in calls
    assert f"{clone}/preflight.py" in calls                           # ...and was verified
    assert "The overlay restarts itself now" in r.stdout
    app, launcher, plist = _bundle(clone)
    assert os.access(launcher, os.X_OK) and plist.exists()
    assert r.stdout.count("[OK] Built: ") == 1 and f"[OK] Built: {app}" in r.stdout
    assert "[NOTE]" not in r.stdout and "[X]" not in r.stdout
    assert _stages(clone) == []
    # Only a Desktop launcher the user made is rebuilt; the update doesn't put one there.
    assert not (env.home / "Desktop" / "Claude Overlay.app").exists()


def test_update_rebuilds_the_launchers_that_are_out_of_date(clone, env):
    _fake_python(clone)
    desktop = env.home / "Desktop"
    for folder in (clone, desktop):
        assert env.run(clone / "mac-app.sh", str(folder)).returncode == 0
        _outdated(folder)
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 0, r.stdout + r.stderr
    for folder in (clone, desktop):
        app, launcher, plist = _bundle(folder)
        assert f"[OK] Built: {app}" in r.stdout
        assert STALE not in launcher.read_text()                      # a new bundle
        assert os.access(launcher, os.X_OK) and plist.exists()
        assert _stages(folder) == []
    assert "[NOTE]" not in r.stdout and "[X]" not in r.stdout


def test_update_leaves_launchers_that_are_current_alone(clone, env):
    # The usual update: the release changed the app, not the launcher. Rebuilding it
    # anyway made it a new app to macOS, which then asked for Screen Recording again.
    _icon_tools(clone, env)
    desktop = env.home / "Desktop"
    for folder in (clone, desktop):
        assert env.run(clone / "mac-app.sh", str(folder)).returncode == 0
    stats = {folder: _stats(_bundle(folder)[0]) for folder in (clone, desktop)}
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "NEW.txt").exists()
    for folder in (clone, desktop):
        assert f"[OK] Already up to date: {_bundle(folder)[0]}" in r.stdout
        assert _stats(_bundle(folder)[0]) == stats[folder] and _stages(folder) == []
    assert "[OK] Built" not in r.stdout
    assert "[NOTE]" not in r.stdout and "[X]" not in r.stdout
    assert "The overlay restarts itself now" in r.stdout


def test_update_says_when_a_launcher_was_not_rebuilt_and_still_succeeds(clone, env):
    # The bundle is a convenience, so this is a note and exit 0 - but it used to be
    # nothing at all, over a launcher the failed build had half overwritten.
    _fake_python(clone)
    assert env.run(clone / "mac-app.sh").returncode == 0
    _outdated(clone)
    app, launcher, plist = _bundle(clone)
    before = _snapshot(app)
    env.cli("chmod", NO_CHMOD)
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "NEW.txt").exists()
    assert "[X] Could not write the launcher" in r.stdout             # the reason is shown
    assert '[NOTE] The "Claude Overlay.app" launcher in this folder was not rebuilt' in r.stdout
    assert '"Start Claude Overlay.command" starts the app without it' in r.stdout
    assert f'/bin/bash "{clone}/mac-app.sh" "{clone}"' in r.stdout
    assert "[OK] Built" not in r.stdout
    assert _snapshot(app) == before and _stages(clone) == []          # still the old one
    assert f"{clone}/preflight.py" in (clone / "py-calls.txt").read_text()   # still verified
    assert "The overlay restarts itself now" in r.stdout


def test_update_notes_only_the_launcher_that_failed(clone, env):
    if os.geteuid() == 0:
        pytest.skip("root can write to a read-only folder")
    _fake_python(clone)
    desktop = env.home / "Desktop"
    assert env.run(clone / "mac-app.sh", str(desktop)).returncode == 0
    _outdated(desktop)
    app = _bundle(desktop)[0]
    before = _snapshot(app)
    desktop.chmod(0o555)
    try:
        r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    finally:
        desktop.chmod(0o755)                          # or pytest can't clear tmp_path
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"[OK] Built: {_bundle(clone)[0]}" in r.stdout
    assert r.stdout.count("[NOTE]") == 1
    assert '[NOTE] The "Claude Overlay.app" launcher on the Desktop was not rebuilt' in r.stdout
    assert f'/bin/bash "{clone}/mac-app.sh" "{desktop}"' in r.stdout
    assert _snapshot(app) == before and _stages(desktop) == []


def test_update_without_a_launcher_does_not_send_the_user_to_one(clone, env):
    _fake_python(clone)
    r = env.run(clone / "update.command")             # not from the Update button
    assert r.returncode == 0, r.stdout + r.stderr
    assert 're-open it ("Claude Overlay.app")' in r.stdout.split("[OK] Updated.")[1]
    # Now with nothing to open: the build fails and there was no launcher before it.
    shutil.rmtree(_bundle(clone)[0])
    env.cli("chmod", NO_CHMOD)
    r = env.run(clone / "update.command")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "was not rebuilt" in r.stdout
    closing = r.stdout.split("[OK] Updated.")[1]
    assert "Claude Overlay.app" not in closing
    assert 're-open it ("Start Claude Overlay.command")' in closing


def test_update_stops_off_main(clone, env):
    _fake_python(clone)
    subprocess.run(["git", "switch", "-q", "-c", "topic"], cwd=clone, check=True)
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 1 and 'on branch "topic", not main' in r.stdout
    assert not (clone / "NEW.txt").exists()


def test_update_stops_on_uncommitted_changes(clone, env):
    _fake_python(clone)
    (clone / "requirements.txt").write_text("edited\n")
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 1 and "uncommitted changes" in r.stdout
    assert not (clone / "NEW.txt").exists()


def test_update_outside_a_clone_says_to_redownload(tmp_path, env):
    folder = _app_folder(tmp_path / "zip install")
    r = env.run(folder / "update.command")
    assert r.returncode == 1 and "isn't a git clone" in r.stdout


def test_update_without_an_environment_keeps_the_code_and_names_the_fix(clone, env):
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")         # no .venv at all
    assert r.returncode == 1
    assert (clone / "NEW.txt").exists()                               # the code DID update
    assert "setup.command" in r.stdout and "packages were NOT refreshed" in r.stdout


def test_update_fails_when_the_packages_do(clone, env):
    # pip can fail while everything else works - that must not be reported as an update.
    _exe(clone / ".venv" / "bin" / "python",
         'case "$*" in *"pip install"*) exit 1 ;; esac\nexit 0\n')
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 1 and "Refreshing the packages FAILED" in r.stdout


def test_update_fails_when_the_new_code_will_not_load(clone, env):
    _exe(clone / ".venv" / "bin" / "python",
         'case "$*" in *preflight.py*) exit 1 ;; esac\nexit 0\n')
    r = env.run(clone / "update.command", OV_UPDATE_AUTO="1")
    assert r.returncode == 1 and "unable to start" in r.stdout


# ── install-codex.sh ────────────────────────────────────────────────────────────────────
def test_codex_installed_and_signed_in_is_ready(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    env.cli("codex")
    env.state.mkdir(parents=True)
    (env.state / "codex-setup-declined").write_text("declined\n")
    (env.state / "codex-setup-result").write_text("--from-update 3 npm-failed\n")
    r = env.run(folder / "install-codex.sh", "--from-app")
    assert r.returncode == 0 and "codex is signed in" in r.stdout
    assert not (env.state / "codex-setup-declined").exists()          # both markers cleared
    assert not (env.state / "codex-setup-result").exists()


def test_codex_installed_but_signed_out_is_2(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    env.cli("codex", 'case "$*" in "login status") exit 1 ;; esac\nexit 0\n')
    r = env.run(folder / "install-codex.sh", "--from-setup")          # nobody to ask: no login
    assert r.returncode == 2 and "not signed in" in r.stdout
    assert not (env.state / "codex-setup-result").exists()            # a "no" is not a failure


def test_from_app_signs_in_without_asking(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    flag = tmp_path / "logged-in"
    env.cli("codex", f'case "$*" in\n  "login status") [ -e "{flag}" ]; exit $? ;;\n'
                     f'  "login") touch "{flag}"; exit 0 ;;\nesac\nexit 0\n')
    r = env.run(folder / "install-codex.sh", "--from-app")
    assert r.returncode == 0 and flag.exists()


def test_an_update_asks_first_and_remembers_a_no(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    env.cli("npm", 'echo "npm must not run" >&2; exit 1\n')
    r = env.run(folder / "install-codex.sh", "--from-update")         # no terminal = "no"
    assert r.returncode == 3 and "New in this version: Codex mode" in r.stdout
    assert "npm must not run" not in r.stderr
    assert (env.state / "codex-setup-declined").exists()
    again = env.run(folder / "install-codex.sh", "--from-update")
    assert again.returncode == 3 and "you skipped it before" in again.stdout
    assert "New in this version" not in again.stdout                  # asked once, not again


def test_a_failed_npm_install_is_recorded_for_the_overlay(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    env.cli("npm", 'case "$1" in prefix) echo "/nowhere"; exit 0 ;; esac\n'
                   'echo "npm ERR! boom"; exit 1\n')
    r = env.run(folder / "install-codex.sh", "--from-setup")
    assert r.returncode == 3 and "npm couldn't install the codex CLI" in r.stdout
    assert (env.state / "codex-setup-result").read_text().split() == \
        ["--from-setup", "3", "npm-failed"]


def test_npm_installs_codex_into_the_users_folder_when_its_own_is_not_searched(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    node_home = env.home / ".local" / "node"
    # First install "succeeds" somewhere codex isn't found; the --prefix retry puts it
    # where the overlay looks. (OV_TEST_PATH is fixed, so add that folder to it up front.)
    env.cli("npm", 'case "$*" in\n  "prefix -g") echo "/somewhere/else"; exit 0 ;;\n'
                   f'  *"--prefix {node_home}"*) mkdir -p "{node_home}/bin"; '
                   f'printf "#!/bin/bash\\nexit 0\\n" > "{node_home}/bin/codex"; '
                   f'chmod +x "{node_home}/bin/codex"; exit 0 ;;\nesac\nexit 0\n')
    r = env.run(folder / "install-codex.sh", "--from-app",
                OV_TEST_PATH=f"{env.bin}:{node_home}/bin:/usr/bin:/bin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Trying again into" in r.stdout and "codex CLI installed" in r.stdout


# ── mac-app.sh and the launchers ────────────────────────────────────────────────────────
# Names a script mishandles if it forgets one pair of quotes - or leaves a path
# unquoted where bash reads a pattern, as in the `case` that guards mac-app.sh's rm -rf.
ODD_NAMES = ["It's \"here\" & (there) $HOME", "a [b]* ?\\ `c` $(d) ; e", " -lead and trail "]


@pytest.mark.parametrize("name", ODD_NAMES)
@pytest.mark.parametrize("rebuild", [False, True])
def test_the_launcher_bundle_points_back_at_its_folder(tmp_path, env, name, rebuild):
    folder = _app_folder(tmp_path / name)
    if rebuild:                                       # over one that is already there
        assert env.run(folder / "mac-app.sh").returncode == 0
        _outdated(folder)
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    app, launcher, _ = _bundle(folder)
    assert f"[OK] Built: {app}" in r.stdout and r.stderr == ""
    assert _stages(folder) == []
    assert os.access(launcher, os.X_OK)
    assert subprocess.run(["/bin/bash", "-n", str(launcher)]).returncode == 0
    # The folder is written so bash reads back exactly that path, whatever is in its name.
    out = subprocess.run(["/bin/bash", "-c", f'eval "$(grep ^REPO= "$1")"; printf %s "$REPO"',
                          "_", str(launcher)], capture_output=True, text=True).stdout
    assert out == str(folder)
    assert subprocess.run(["plutil", "-lint", str(app / "Contents" / "Info.plist")],
                          capture_output=True).returncode == 0
    plist = (app / "Contents" / "Info.plist").read_text()
    assert "<key>LSUIElement</key><true/>" in plist and "NSAppleEventsUsageDescription" in plist


def test_the_launcher_bundle_can_be_built_somewhere_else(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    r = env.run(folder / "Create Desktop Shortcut.command")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.home / "Desktop" / "Claude Overlay.app" / "Contents" / "MacOS"
            / "ClaudeOverlay").exists()
    again = env.run(folder / "Create Desktop Shortcut.command")      # it is there already
    assert again.returncode == 0 and "[OK] Already up to date" in again.stdout
    assert "[X]" not in again.stdout
    assert env.run(folder / "mac-app.sh", str(tmp_path / "no such folder")).returncode == 1


@pytest.mark.parametrize("name", ODD_NAMES)
def test_the_destination_can_have_anything_in_its_name(tmp_path, env, name):
    folder = _app_folder(tmp_path / "app")
    dest = tmp_path / "elsewhere" / name
    dest.mkdir(parents=True)
    app, launcher, plist = _bundle(dest)
    # A first build, one with nothing to do, and one over a launcher that has to go.
    for says in ("Built", "Already up to date", "Built"):
        r = env.run(folder / "mac-app.sh", str(dest))
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout == f"[OK] {says}: {app}\n" and r.stderr == ""
        assert os.access(launcher, os.X_OK) and plist.exists()
        assert STALE not in launcher.read_text()
        assert sorted(f.name for f in dest.iterdir()) == ["Claude Overlay.app"]
        if says == "Already up to date":
            _outdated(dest)
    assert not _bundle(folder)[0].exists()            # ...and nowhere else


def test_the_launcher_tells_python_it_was_started_from_the_app(tmp_path, env):
    # Finder starts the launcher with no SHLVL; bash sets it to 1 and exec takes it back
    # to 0, which is no way to tell a Finder start from a terminal one. So the launcher
    # says it outright, and config._mac_fix_path asks the login shell for PATH on that.
    folder = _app_folder(tmp_path / "app")
    assert env.run(folder / "mac-app.sh").returncode == 0
    _exe(folder / ".venv" / "bin" / "python",
         'echo "from_app=$CLAUDE_OVERLAY_FROM_APP args=$*" > "$(dirname "$0")/../../launch.txt"\n')
    r = env.run(folder / "Claude Overlay.app" / "Contents" / "MacOS" / "ClaudeOverlay")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (folder / "launch.txt").read_text().strip() == \
        f"from_app=1 args={folder}/claude_overlay.py"


@pytest.mark.parametrize("tool, body", [("chmod", NO_CHMOD), ("cat", NO_PLIST)],
                         ids=["launcher", "Info.plist"])
def test_a_bundle_missing_a_required_file_is_not_reported_as_built(tmp_path, env, tool, body):
    # Without the launcher or Info.plist the bundle can't open, so a failed write of
    # either (as on a full disk) must not end in [OK] - nor leave half a bundle behind.
    folder = _app_folder(tmp_path / "app")
    env.cli(tool, body)
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "[X] Could not write" in r.stdout and "[OK] Built" not in r.stdout
    assert not _bundle(folder)[0].exists() and _stages(folder) == []


# What a rebuild can fail at, in the order it gets there: making the folder it builds
# in, the two required files (or being stopped while at it), moving the old bundle
# aside, moving the new one in.
REBUILD_FAILURES = {
    "folder read-only": (None, None, "[X] Could not create"),
    "launcher": ("chmod", NO_CHMOD, "[X] Could not write the launcher"),
    "Info.plist": ("cat", NO_PLIST, "[X] Could not write"),
    **{f"stopped by SIG{sig}": ("chmod", STOPPED_BUILDING.format(sig=sig), "")
       for sig in SIGNALS},
    "old bundle read-only": (None, None, "[X] Could not replace"),
    "new bundle not moved in": ("mv", NO_SWAP_IN, "[X] Could not move the new launcher"),
}


@pytest.mark.parametrize("failure", list(REBUILD_FAILURES))
def test_a_failed_rebuild_leaves_the_launcher_that_was_there(tmp_path, env, failure):
    # update.command rebuilds over a launcher that works. Written in place, a build that
    # failed halfway left it broken; built aside and swapped in, it is never touched.
    tool, body, says = REBUILD_FAILURES[failure]
    folder = _app_folder(tmp_path / "It's \"here\" & [there]*")
    assert env.run(folder / "mac-app.sh").returncode == 0
    _outdated(folder)                                 # or there is nothing to rebuild
    app, launcher, plist = _bundle(folder)
    before, listing = _snapshot(app), sorted(f.name for f in folder.iterdir())
    assert {"Contents/MacOS/ClaudeOverlay", "Contents/Info.plist"} <= set(before)
    locked = {"folder read-only": folder, "old bundle read-only": app}.get(failure)
    if locked is not None and os.geteuid() == 0:
        pytest.skip("root can write to a read-only folder")
    if tool:
        env.cli(tool, body)
    if locked is not None:
        locked.chmod(0o555)
    try:
        r = env.run(folder / "mac-app.sh")
    finally:
        if locked is not None:
            locked.chmod(0o755)                       # or pytest can't clear tmp_path
    assert r.returncode == 1, r.stdout + r.stderr
    assert says in r.stdout and "[OK] Built" not in r.stdout
    assert _snapshot(app) == before                   # byte for byte, modes included
    assert sorted(f.name for f in folder.iterdir()) == listing        # nothing left beside it
    assert os.access(launcher, os.X_OK) and launcher.read_text().endswith(STALE)


def test_a_launcher_that_cannot_be_put_back_is_kept_not_deleted(tmp_path, env):
    # The worst the swap can do: the old bundle is aside, the new one won't move in and
    # the old one won't move back. Clearing the build folder then would delete the only
    # launcher there is - so it stays, and the script says where.
    folder = _app_folder(tmp_path / "app")
    assert env.run(folder / "mac-app.sh").returncode == 0
    _outdated(folder)
    app = _bundle(folder)[0]
    before = _snapshot(app)
    env.cli("mv", NO_SWAP_NO_UNDO)
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "[X] The launcher that was there could not be put back" in r.stdout
    assert "[OK] Built" not in r.stdout and not app.exists()
    (stage,) = _stages(folder)
    assert str(folder / stage / "previous") in r.stdout
    assert _snapshot(folder / stage / "previous") == before


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_swap_is_not_left_half_done_by_a_signal(tmp_path, env, sig):
    # Between the two renames nothing has the launcher's name. A closed window or a
    # Ctrl-C arriving right there is ignored: the swap finishes, and so does the tidying.
    folder = _app_folder(tmp_path / "app")
    assert env.run(folder / "mac-app.sh").returncode == 0
    app, launcher, plist = _bundle(folder)
    _outdated(folder)
    env.cli("mv", STOPPED_SWAPPING.format(sig=sig))
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"[OK] Built: {app}" in r.stdout
    assert os.access(launcher, os.X_OK) and STALE not in launcher.read_text()
    assert plist.exists() and _stages(folder) == []


def test_a_rebuild_replaces_the_whole_bundle(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    assert env.run(folder / "mac-app.sh").returncode == 0
    app, launcher, plist = _bundle(folder)
    listing = sorted(f.name for f in folder.iterdir())
    # The old bundle: a file no build makes, and a launcher that is broken in a way
    # writing over it in place could not mend (a folder has its name).
    (app / "Contents" / "left-over.txt").write_text("from an older release\n")
    launcher.unlink()
    launcher.mkdir()
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"[OK] Built: {app}" in r.stdout and r.stderr == ""
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert launcher.read_text().startswith("#!/bin/bash\n")
    assert "export CLAUDE_OVERLAY_FROM_APP=1\n" in launcher.read_text()
    assert plist.is_file() and not (app / "Contents" / "left-over.txt").exists()
    assert sorted(f.name for f in folder.iterdir()) == listing        # no stage, no old copy


# ── a launcher that is current is left alone ────────────────────────────────────────────
# macOS knows an ad-hoc signed app by the hash of what was signed, and remembers Screen
# Recording / Automation permission against that. A rebuilt bundle can be a new app to
# it - so mac-app.sh replaces one only when it has to.
def test_a_current_launcher_is_not_rebuilt_resigned_or_touched(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    _icon_tools(folder, env)
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    app = _bundle(folder)[0]
    assert r.stdout == f"[OK] Built: {app}\n"
    before, stats = _snapshot(app), _stats(app)
    signed = SEAL in before
    assert ICON in before and (signed or not os.path.exists("/usr/bin/codesign"))
    seen = (folder.stat().st_mtime_ns, (folder / "py-calls.txt").read_text())
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == f"[OK] Already up to date: {app}\n" and r.stderr == ""
    assert _snapshot(app) == before and _stats(app) == stats          # signature included
    assert _stages(folder) == []
    if not signed:
        return                                        # (it had to try to sign it to know)
    # Complete, so that was decided before building anything: nothing was written
    # beside the bundle, and the icon was not drawn again.
    assert (folder.stat().st_mtime_ns, (folder / "py-calls.txt").read_text()) == seen
    if os.geteuid() != 0:                             # ...so it needs no way to write, either
        folder.chmod(0o555)
        try:
            r = env.run(folder / "mac-app.sh")
        finally:
            folder.chmod(0o755)
        assert r.returncode == 0 and r.stdout == f"[OK] Already up to date: {app}\n"
        assert _stats(app) == stats


def test_a_new_app_version_does_not_change_the_launcher(tmp_path, env):
    # Info.plist used to carry the app's version, so every release rebuilt the bundle -
    # and the permissions granted to "Claude Overlay" had to be granted again each time.
    folder = _app_folder(tmp_path / "app")
    _icon_tools(folder, env)
    (folder / "config.py").write_text('__version__ = "41.42.43"\n')
    assert env.run(folder / "mac-app.sh").returncode == 0
    app, launcher, plist = _bundle(folder)
    assert "41.42.43" not in plist.read_text() + launcher.read_text()
    assert "<key>CFBundleVersion</key><string>1.0</string>" in plist.read_text()
    before, stats = _snapshot(app), _stats(app)
    (folder / "config.py").write_text('__version__ = "41.42.44"\n')  # the next release
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == f"[OK] Already up to date: {app}\n"
    assert _snapshot(app) == before and _stats(app) == stats


def test_a_moved_app_folder_gets_a_new_launcher(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    _icon_tools(folder, env)
    desktop = env.home / "Desktop"
    for dest in (folder, desktop):
        assert env.run(folder / "mac-app.sh", str(dest)).returncode == 0
    moved = tmp_path / "moved" / "to here"
    moved.parent.mkdir()
    folder.rename(moved)
    for dest in (moved, desktop):                     # both still point at the old place
        app, launcher, plist = _bundle(dest)
        r = env.run(moved / "mac-app.sh", str(dest))
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout == f"[OK] Built: {app}\n"
        out = subprocess.run(["/bin/bash", "-c", 'eval "$(grep ^REPO= "$1")"; printf %s "$REPO"',
                              "_", str(launcher)], capture_output=True, text=True).stdout
        assert out == str(moved)
        assert (app / ICON).exists() and _stages(dest) == []


@pytest.mark.parametrize("wrong", ["launcher from an older release", "launcher not executable",
                                   "no Info.plist", "Info.plist with the app's version",
                                   "no icon", "no signature"])
def test_a_launcher_that_is_not_right_is_rebuilt(tmp_path, env, wrong):
    folder = _app_folder(tmp_path / "app")
    _icon_tools(folder, env)
    assert env.run(folder / "mac-app.sh").returncode == 0
    app, launcher, plist = _bundle(folder)
    good, signed = _snapshot(app), (app / SEAL).exists()
    if wrong == "launcher from an older release":
        _outdated(folder)
    elif wrong == "launcher not executable":
        launcher.chmod(0o644)
    elif wrong == "no Info.plist":
        plist.unlink()
    elif wrong == "Info.plist with the app's version":   # as built before it was fixed
        plist.write_text(plist.read_text().replace("<string>1.0</string>",
                                                   "<string>1.23.4</string>"))
    elif wrong == "no icon":
        (app / ICON).unlink()
    elif not signed:
        pytest.skip("codesign did not sign the bundle here")
    else:
        shutil.rmtree(app / "Contents" / "_CodeSignature")
    assert _snapshot(app) != good
    was = app.stat().st_ino
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == f"[OK] Built: {app}\n" and r.stderr == ""
    assert app.stat().st_ino != was and _stages(folder) == []         # a new bundle...
    now = _snapshot(app)
    for part in ("Contents/MacOS/ClaudeOverlay", "Contents/Info.plist", ICON):
        assert now[part] == good[part]                                # ...and a right one
    assert (app / SEAL).exists() is signed


@pytest.mark.parametrize("short_of", ["its icon", "its signature", "both"])
def test_a_launcher_is_not_rebuilt_for_what_the_build_cannot_add(tmp_path, env, short_of):
    # A launcher with no icon or no signature is worth rebuilding - when the rebuild
    # would have one. Where it can't (no Pillow, a codesign that fails), swapping in the
    # same thing again would only happen on every update.
    folder = _app_folder(tmp_path / "app")
    if short_of != "its icon":
        env.cli("codesign", "exit 1\n")
    if short_of == "its signature":
        _icon_tools(folder, env)                      # ...the others have no Python to draw with
    assert env.run(folder / "mac-app.sh").returncode == 0
    app = _bundle(folder)[0]
    before, stats = _snapshot(app), _stats(app)
    assert (ICON in before) is (short_of == "its signature")
    if short_of == "its icon" and SEAL not in before:
        pytest.skip("codesign did not sign the bundle here")
    assert (SEAL in before) is (short_of == "its icon")
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == f"[OK] Already up to date: {app}\n" and r.stderr == ""
    assert _snapshot(app) == before and _stats(app) == stats and _stages(folder) == []


def test_something_else_with_the_bundles_name_is_left_alone(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    squatter = folder / "Claude Overlay.app"
    squatter.write_text("not a launcher\n")
    r = env.run(folder / "mac-app.sh")
    assert r.returncode == 1 and "[X] Could not create" in r.stdout
    assert squatter.read_text() == "not a launcher\n" and _stages(folder) == []


@pytest.mark.parametrize("desktop", ["missing", "read-only"])
def test_a_shortcut_that_was_not_created_fails_and_says_so(tmp_path, env, desktop):
    folder = _app_folder(tmp_path / "app")
    target = env.home / "Desktop"
    if desktop == "missing":
        target.rmdir()
    elif os.geteuid() == 0:
        pytest.skip("root can write to a read-only folder")
    else:
        target.chmod(0o555)
    try:
        r = env.run(folder / "Create Desktop Shortcut.command")
    finally:
        if target.exists():
            target.chmod(0o755)                       # or pytest can't clear tmp_path
    assert r.returncode == 1, r.stdout + r.stderr
    assert "[X] The Desktop shortcut was NOT created" in r.stdout
    assert "[OK] Built" not in r.stdout
    assert not (target / "Claude Overlay.app" / "Contents" / "MacOS" / "ClaudeOverlay").exists()


# ── setup.command ───────────────────────────────────────────────────────────────────────
def _ready_for_setup(folder, env):
    """Everything setup.command looks for ahead of the launcher, already in place - so it
    has nothing to download and (with no terminal) nothing to ask."""
    _fake_python(folder)
    env.cli("claude")
    env.cli("codex")


def test_setup_builds_the_launcher_and_points_at_it(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    _ready_for_setup(folder, env)
    r = env.run(folder / "setup.command")
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.access(folder / "Claude Overlay.app" / "Contents" / "MacOS" / "ClaudeOverlay",
                     os.X_OK)
    closing = r.stdout.split("[OK] Setup complete.")[1]
    assert 'double-click "Claude Overlay.app"' in closing
    assert "launcher was not built" not in r.stdout
    again = env.run(folder / "setup.command")         # safe to run again: nothing to rebuild
    assert again.returncode == 0, again.stdout + again.stderr
    assert "[OK] Already up to date" in again.stdout and "[OK] Built" not in again.stdout
    assert "launcher was not built" not in again.stdout
    assert 'double-click "Claude Overlay.app"' in again.stdout.split("[OK] Setup complete.")[1]


def test_setup_without_a_launcher_finishes_but_does_not_send_the_user_to_it(tmp_path, env):
    # The bundle is a convenience (Start Claude Overlay.command needs none of it), so a
    # failed build doesn't fail setup - but "double-click Claude Overlay.app" would then
    # point at nothing.
    folder = _app_folder(tmp_path / "app")
    _ready_for_setup(folder, env)
    (folder / "Claude Overlay.app").write_text("a file where the bundle goes\n")
    r = env.run(folder / "setup.command")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '[NOTE] The "Claude Overlay.app" launcher was not built' in r.stdout
    assert "[OK] Built" not in r.stdout
    closing = r.stdout.split("[OK] Setup complete.")[1]
    assert "Claude Overlay.app" not in closing
    assert 'double-click "Start Claude Overlay.command"' in closing
    assert f"{folder}/preflight.py" in (folder / "py-calls.txt").read_text()   # still verified


def test_start_without_an_environment_says_to_run_setup(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    r = env.run(folder / "Start Claude Overlay.command")
    assert r.returncode == 1 and "setup.command" in r.stdout


def test_start_runs_this_folders_environment(tmp_path, env):
    folder = _app_folder(tmp_path / "app")
    _fake_python(folder)
    r = env.run(folder / "Start Claude Overlay.command")
    assert r.returncode == 0
    for _ in range(50):                               # it is started detached
        if (folder / "py-calls.txt").exists() and "claude_overlay.py" in \
                (folder / "py-calls.txt").read_text():
            break
        __import__("time").sleep(0.1)
    assert f"{folder}/claude_overlay.py" in (folder / "py-calls.txt").read_text()
