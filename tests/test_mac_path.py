"""config._mac_fix_path: when the overlay asks the login shell for its PATH, and what it
keeps of the answer.

Two bugs live here if it goes wrong, both invisible from a terminal: started from Finder,
the app finds no CLI that a shell-configured folder holds (nvm, volta); and a CLI the app
installs for itself, into a folder that didn't exist at startup, stays "missing" until
the next launch.

No real shell is started: subprocess.run is a stand-in that answers in the function's own
__OVPATH__ protocol, the home folder is a temp one, and monkeypatch puts PATH back."""
import os
import shutil
import stat
import sys
import uuid

import pytest

import config

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS PATH discovery")

MARKER = "CLAUDE_OVERLAY_FROM_APP"


class Box:
    """A login shell that answers `shell_path`, and a record of whether it was asked."""

    def __init__(self, home):
        self.home = home
        self.asked = []
        self.shell_path = "/from/the/shell"
        self.stdout = None                        # set to answer something else entirely

    def run(self, args, **kw):
        self.asked.append(args)
        out = self.stdout
        if out is None:
            out = f"noise from an rc file\n__OVPATH__{self.shell_path}__OVPATH__\n"
        return type("R", (), {"stdout": out})()

    @property
    def floor(self):
        h = str(self.home)
        return [f"{h}/.local/bin", f"{h}/.local/node/bin", "/opt/homebrew/bin",
                "/usr/local/bin", f"{h}/.npm-global/bin"]

    def fix(self):
        config._mac_fix_path()
        return os.environ["PATH"].split(os.pathsep)


@pytest.fixture
def box(monkeypatch, tmp_path):
    """Started by launchd, as far as the environment says: no SHLVL, no TERM, no marker,
    launchd's short PATH - and an EMPTY home, so none of the per-user folders exist."""
    home = tmp_path / "home"
    home.mkdir()
    b = Box(home)
    monkeypatch.setattr(config.subprocess, "run", b.run)
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: home))
    for name in ("SHLVL", "TERM", MARKER):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")   # setenv first: this is what gets restored
    return b


# ── when the shell is asked ─────────────────────────────────────────────────────────────
def test_the_app_launcher_shlvl_of_zero_is_not_a_shell(box, monkeypatch):
    # What Python really sees under Claude Overlay.app: bash set SHLVL=1, exec took it back.
    monkeypatch.setenv("SHLVL", "0")
    parts = box.fix()
    assert box.asked == [["/bin/zsh", "-ilc", 'printf "__OVPATH__%s__OVPATH__" "$PATH"']]
    assert parts[0] == "/from/the/shell"                    # the shell's answer goes in front


@pytest.mark.parametrize("shlvl", [None, "", "0", "-1", "not a number"])
def test_anything_short_of_a_real_shell_level_asks(box, monkeypatch, shlvl):
    if shlvl is not None:
        monkeypatch.setenv("SHLVL", shlvl)
    assert "/from/the/shell" in box.fix() and len(box.asked) == 1


@pytest.mark.parametrize("env", [{"SHLVL": "1"},                       # CI: a shell, no terminal
                                 {"SHLVL": "3"},
                                 {"TERM": "xterm-256color"},
                                 {"SHLVL": "0", "TERM": "xterm-256color"},
                                 {"SHLVL": "2", "TERM": "xterm-256color"}])
def test_a_terminal_launch_is_not_made_to_wait_for_a_shell(box, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    parts = box.fix()
    assert not box.asked and "/from/the/shell" not in parts
    assert parts[:2] == ["/usr/bin", "/bin"]                # what it had, untouched, in front


def test_the_launchers_marker_asks_even_when_it_looks_like_a_terminal(box, monkeypatch):
    monkeypatch.setenv(MARKER, "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SHLVL", "2")
    parts = box.fix()
    assert len(box.asked) == 1 and parts[0] == "/from/the/shell"
    # It described THIS process. Left in place, every Python the app starts - and every
    # test run from a Claude session inside the overlay - would ask the shell again.
    assert MARKER not in os.environ


# ── what is kept ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("asks", [True, False])
def test_the_install_folders_are_on_path_before_they_exist(box, monkeypatch, asks):
    if not asks:
        monkeypatch.setenv("SHLVL", "1")
    parts = box.fix()
    assert bool(box.asked) is asks
    assert not any(os.path.exists(p) for p in box.floor if p.startswith(str(box.home)))
    assert parts[-len(box.floor):] == box.floor             # all of them, behind the rest


def test_a_cli_installed_while_running_is_found_without_a_restart(box):
    # "Set up Codex": install-codex.sh puts Node.js (and codex) in ~/.local/node, a folder
    # that isn't there when the app starts. The app then looks for codex on ITS path.
    # Not looked up as "codex" here: the floor holds the REAL /opt/homebrew/bin and
    # /usr/local/bin, so a Mac with Codex installed would find that one first. A name
    # nothing else has leaves only the temp home able to answer.
    name = f"ov-test-cli-{uuid.uuid4().hex}"
    box.fix()
    assert shutil.which(name) is None
    cli = box.home / ".local" / "node" / "bin" / name
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    assert shutil.which(name) == str(cli)


def test_what_the_shell_lists_is_kept_whether_or_not_it_exists_yet(box):
    box.shell_path = "/no/such/dir/yet:/usr/bin"
    parts = box.fix()
    assert parts[:3] == ["/no/such/dir/yet", "/usr/bin", "/bin"]
    assert parts.count("/usr/bin") == 1                     # listed twice, kept once


def test_nothing_relative_is_ever_added(box, monkeypatch):
    box.shell_path = "bin:./tools:../up:~/bin:.:/abs/from/shell::node_modules/.bin"
    monkeypatch.setenv("PATH", "also/relative:.:/usr/bin::~/.local/bin")
    parts = box.fix()
    assert parts == ["/abs/from/shell", "/usr/bin"] + box.floor
    assert all(os.path.isabs(p) for p in parts)


@pytest.mark.parametrize("stdout", ["", "zsh: command not found: printf\n",
                                    "__OVPATH__/half/an/answer"])
def test_a_shell_that_does_not_answer_leaves_the_path_and_the_floor(box, stdout):
    box.stdout = stdout
    assert box.fix() == ["/usr/bin", "/bin"] + box.floor
    assert len(box.asked) == 1
