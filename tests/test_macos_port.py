"""The cross-platform seams the macOS port added to the shared modules: per-platform
paths and message text in config, the keychain-backed credential record in authstate, the
Unix npm layout in cliupdate / codexworker.

Most of this is plain logic that runs on any OS (a path layout, a string table), so it is
NOT skipped off macOS — Windows CI runs it too, which is what keeps the port from rotting
between Mac releases. The few tests that need the real platform say so."""
import json
import os
import sys

import pytest

import authstate
import cliupdate as cu
import codexworker as cw
import config
import crashreport
import screengeom
import telemetry

MAC = sys.platform == "darwin"


# ── config ──────────────────────────────────────────────────────────────────────────────
def test_data_home_prefers_localappdata_then_the_platforms_own(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config._data_home() == tmp_path                  # Windows' answer, unchanged
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr(config, "IS_MAC", True)
    assert config._data_home() == config.Path.home() / "Library" / "Application Support"
    monkeypatch.setattr(config, "IS_MAC", False)
    assert config._data_home() == config.Path.home()


def test_platform_text_is_identity_off_macos(monkeypatch):
    monkeypatch.setattr(config, "IS_MAC", False)
    msg = "Run update.cmd, or  irm https://claude.ai/install.ps1 | iex  in PowerShell"
    assert config.platform_text(msg) is msg


def test_platform_text_swaps_in_the_mac_names(monkeypatch):
    monkeypatch.setattr(config, "IS_MAC", True)
    t = config.platform_text
    assert t("double-click update.cmd in the app folder") == \
        "double-click update.command in the app folder"
    assert t("re-run setup.cmd, which does both") == "re-run setup.command, which does both"
    assert t("Couldn't start install-codex.cmd") == "Couldn't start install-codex.sh"
    assert "curl -fsSL https://claude.ai/install.sh | bash" in \
        t("install it with  irm https://claude.ai/install.ps1 | iex  in PowerShell, then")
    assert t("Ctrl +/− to zoom text") == "⌘ +/− to zoom text"
    assert t(None) is None and t("") == ""
    assert t(12) == "12"                                    # add_sys accepts any object


def test_every_script_platform_text_names_exists_in_the_repo():
    # A message that tells a Mac user to double-click a file must name a file that ships.
    repo = os.path.dirname(os.path.abspath(config.__file__))
    for _old, new in config._MAC_TEXT:
        if new.endswith((".command", ".sh")):
            assert os.path.isfile(os.path.join(repo, new)), new
    for name in ("setup.command", "update.command", "install-codex.sh"):
        assert os.path.isfile(os.path.join(repo, name))


@pytest.mark.skipif(not MAC, reason="the macOS defaults")
def test_macos_defaults():
    assert config.SETUP_SCRIPT == "setup.command" and config.UPDATE_SCRIPT == "update.command"
    assert "ctrl+alt+space" not in config.HOTKEY           # macOS keeps that chord for itself
    assert "macOS desktop" in config.SYSTEM_APPEND and "PowerShell" not in config.SYSTEM_APPEND
    assert "Library/Application Support/claude-overlay" in str(config.STATE_FILE)
    assert str(config.SHOT_DIR) != str(config.Path.home() / "claude_overlay_shots")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH syntax")
def test_mac_fix_path_asks_the_login_shell_only_when_not_started_from_one(monkeypatch, tmp_path):
    shell_dir, floor_like = tmp_path / "from shell", tmp_path / "already"
    shell_dir.mkdir(), floor_like.mkdir()
    asked = []

    class R:
        stdout = f"noise from an rc file\n__OVPATH__{shell_dir}:relative/dir:/no/such/dir__OVPATH__\n"

    def run(args, **kw):
        asked.append(args)
        return R()
    monkeypatch.setattr(config.subprocess, "run", run)
    monkeypatch.setenv("PATH", str(floor_like))

    monkeypatch.setenv("SHLVL", "1")                        # started from a terminal
    config._mac_fix_path()
    assert not asked and str(shell_dir) not in os.environ["PATH"]

    monkeypatch.delenv("SHLVL")
    monkeypatch.delenv("TERM", raising=False)               # started from Finder
    config._mac_fix_path()
    parts = os.environ["PATH"].split(os.pathsep)
    assert asked and parts[0] == str(shell_dir)             # the shell's PATH goes in front
    assert str(floor_like) in parts                         # what we had is kept
    assert all(os.path.isabs(p) for p in parts)             # nothing relative


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH syntax")
def test_mac_fix_path_survives_a_shell_that_fails(monkeypatch, tmp_path):
    def run(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(config.subprocess, "run", run)
    monkeypatch.delenv("SHLVL", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    config._mac_fix_path()
    assert str(tmp_path) in os.environ["PATH"].split(os.pathsep)


# ── screengeom: one implementation, re-exported by both platform layers ─────────────────
def test_platform_layer_reexports_the_shared_geometry():
    layer = __import__("macutils" if MAC else "win32utils") if sys.platform in ("darwin", "win32") else None
    if layer is None:
        pytest.skip("no platform layer for this OS")
    assert layer.clamp_bbox is screengeom.clamp_bbox
    assert layer.compute_onscreen_move is screengeom.compute_onscreen_move


def test_shared_geometry_still_computes():
    mons = [{"rect": (0, 0, 1920, 1080), "work": (0, 0, 1920, 1040), "primary": True}]
    assert screengeom.compute_onscreen_move((100, 100, 420, 620), mons) is None
    assert screengeom.compute_onscreen_move((5000, 100, 420, 620), mons) == (1500, 100)
    assert screengeom.clamp_bbox((-50, -50, 100, 100), (0, 0, 1920, 1080)) == (0, 0, 100, 100)
    assert screengeom.clamp_bbox((0, 0, 4, 4), (0, 0, 1920, 1080)) is None


# ── authstate: the keychain record ──────────────────────────────────────────────────────
@pytest.fixture
def keychain(monkeypatch, tmp_path):
    """A Mac with no credential file and a keychain whose record the test controls."""
    record = {"blob": None, "reads": 0}

    def read():
        record["reads"] += 1
        return record["blob"]
    monkeypatch.setattr(authstate.sys, "platform", "darwin")
    monkeypatch.setattr(authstate, "_keychain_read", read)
    monkeypatch.setattr(authstate, "_kc", {"at": None, "blob": None, "busy": False})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(authstate.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    for name in authstate._ALT_AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    return record


def _creds(**oauth):
    return json.dumps({"claudeAiOauth": oauth})


def test_keychain_record_is_read_when_there_is_no_file(keychain):
    keychain["blob"] = _creds(accessToken="tok", refreshToken="ref", expiresAt=0)
    assert authstate.read_oauth(block=True)["accessToken"] == "tok"
    assert authstate.dead_reason() is None                  # a usable login is not "dead"
    sig = authstate.signature()
    assert sig and sig[0] == "keychain"
    keychain["blob"] = _creds(accessToken="tok2", refreshToken="ref2", expiresAt=0)
    authstate._kc["at"] = None                              # the cache went stale
    assert authstate.read_oauth(block=True)["accessToken"] == "tok2"
    assert authstate.signature() != sig                     # rotated tokens change it


def test_a_blanked_keychain_record_is_a_dead_login(keychain):
    keychain["blob"] = _creds(accessToken="", refreshToken="", expiresAt=0)
    authstate.read_oauth(block=True)
    assert authstate.dead_reason() is not None


def test_the_credential_file_wins_over_the_keychain(keychain, tmp_path):
    keychain["blob"] = _creds(accessToken="from-keychain", refreshToken="r")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(
        _creds(accessToken="from-file", refreshToken="r"))
    assert authstate.read_oauth(block=True)["accessToken"] == "from-file"
    assert authstate.signature()[0] != "keychain"


def test_no_keychain_evidence_is_never_bad_news(keychain):
    assert authstate.read_oauth(block=True) is None         # no item at all
    assert authstate.dead_reason() is None and authstate.signature() is None
    keychain["blob"] = "not json"
    authstate._kc["at"] = None
    assert authstate.read_oauth(block=True) is None and authstate.dead_reason() is None


def test_the_ui_thread_never_waits_for_the_keychain(keychain):
    # dead_reason() runs on the UI thread: an unread or stale record must come back at
    # once (as "nothing yet") and be re-read in the background, not block on `security`.
    keychain["blob"] = _creds(accessToken="tok", refreshToken="ref")
    started = []
    monkeypatch_thread = authstate.threading.Thread

    class T:
        def __init__(self, target=None, **kw):
            started.append(target)

        def start(self):
            pass
    authstate.threading.Thread = T
    try:
        assert authstate.read_oauth() is None and keychain["reads"] == 0
        assert len(started) == 1
        assert authstate.read_oauth() is None and len(started) == 1   # one refresh at a time
        started[0]()                                                   # the refresh lands
        assert authstate.read_oauth()["accessToken"] == "tok"
    finally:
        authstate.threading.Thread = monkeypatch_thread


def test_the_keychain_is_left_alone_off_macos_and_with_a_custom_config_dir(keychain, monkeypatch, tmp_path):
    keychain["blob"] = _creds(accessToken="tok", refreshToken="ref")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert authstate.read_oauth(block=True) is None
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(authstate.sys, "platform", "win32")
    assert authstate.read_oauth(block=True) is None
    assert keychain["reads"] == 0


# ── npm's Unix layout ───────────────────────────────────────────────────────────────────
def _unix_npm(tmp_path, pkg, cli):
    """<prefix>/bin/<cli> -> ../lib/node_modules/<pkg>/bin/<cli>.js, as npm lays it out."""
    prefix = tmp_path / "prefix"
    target = prefix / "lib" / "node_modules" / pkg / "bin" / f"{cli}.js"
    target.parent.mkdir(parents=True)
    target.write_text("")
    (prefix / "bin").mkdir()
    link = prefix / "bin" / cli
    try:
        os.symlink(os.path.join("..", "lib", "node_modules", pkg, "bin", f"{cli}.js"), link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")
    return prefix, link


@pytest.mark.skipif(sys.platform == "win32", reason="npm's Unix layout")
def test_npm_prefix_follows_the_unix_symlink(monkeypatch, tmp_path):
    prefix, link = _unix_npm(tmp_path, os.path.join("@openai", "codex"), "codex")
    monkeypatch.setattr(cu.shutil, "which", lambda name: str(link))
    assert os.path.realpath(cu.npm_prefix("codex")) == os.path.realpath(str(prefix))
    assert cu.npm_managed("codex") is True


def test_a_native_install_is_not_npms_to_update(monkeypatch, tmp_path):
    # Claude's native installer: ~/.local/bin/claude -> ~/.local/share/claude/versions/x.
    real = tmp_path / "share" / "claude" / "versions" / "2.1.0"
    real.parent.mkdir(parents=True)
    real.write_text("")
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "claude"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")
    monkeypatch.setattr(cu.shutil, "which", lambda name: str(link))
    assert cu.npm_prefix("claude") is None and cu.npm_managed("claude") is False


@pytest.mark.skipif(not MAC, reason="npm's Unix layout and the macOS build")
def test_codex_resolves_to_the_native_binary_in_a_unix_install(monkeypatch, tmp_path):
    prefix, link = _unix_npm(tmp_path, os.path.join("@openai", "codex"), "codex")
    exe = (prefix / "lib" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
           / "codex-darwin-arm64" / "vendor" / "aarch64-apple-darwin" / "bin" / "codex")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.delenv("CLAUDE_OVERLAY_CODEX_CMD", raising=False)
    monkeypatch.setattr(cw.shutil, "which", lambda name: str(link))
    monkeypatch.setattr(cw.platform, "machine", lambda: "arm64")
    assert [os.path.realpath(p) for p in cw.codex_command()] == [os.path.realpath(str(exe))]
    exe.unlink()                                            # no native build: the shim runs
    assert cw.codex_command() == [str(link)]


@pytest.mark.skipif(sys.platform == "win32", reason="the no-shell launch path")
def test_npm_runs_without_a_shell_off_windows(monkeypatch, tmp_path):
    tool = tmp_path / "bin" / "npm"
    tool.parent.mkdir()
    tool.write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool.parent) + os.pathsep + os.environ.get("PATH", ""))
    # An argument a shell would mangle arrives exactly as written.
    rc, out, _err = cu._run(["npm", "view", "a b & $(touch pwned) ; c"], 20)
    assert rc == 0 and out == "view|a b & $(touch pwned) ; c|"
    assert not os.path.exists("pwned")
    assert cu._run(["no-such-tool-xyz"], 5) is None


# ── telemetry / crashreport ─────────────────────────────────────────────────────────────
def test_os_build_names_macos_by_product_version(monkeypatch):
    import platform
    monkeypatch.setattr(telemetry.sys, "platform", "darwin")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("15.5", ("", "", ""), "arm64"))
    assert telemetry._os_build() == "macos-15.5"


def test_crash_log_lives_in_application_support_on_macos(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(crashreport.sys, "platform", "darwin")
    assert crashreport.log_dir() == os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "claude-overlay")
    assert os.path.dirname(crashreport.log_dir()) == str(config._data_home()) or not MAC


def test_a_finder_launch_has_no_console_on_macos(monkeypatch):
    # stderr exists but goes nowhere — only a terminal counts as somebody to print to,
    # or a startup crash from a double-click would be written to nobody.
    class Tty:
        def isatty(self):
            return True

    class Pipe:
        def isatty(self):
            return False
    monkeypatch.setattr(crashreport.sys, "platform", "darwin")
    monkeypatch.setattr(crashreport.sys, "stderr", Pipe())
    assert crashreport.has_console() is False
    monkeypatch.setattr(crashreport.sys, "stderr", Tty())
    assert crashreport.has_console() is True


def test_the_mac_dialog_passes_text_as_arguments_not_script(monkeypatch):
    seen = {}

    def run(args, **kw):
        seen["args"] = args
    import subprocess
    monkeypatch.setattr(subprocess, "run", run)
    body = 'Traceback: "quote" & return & do shell script "rm -rf ~"'
    assert crashreport._mac_dialog("Claude Overlay", body) is True
    script = seen["args"][2]
    assert seen["args"][:2] == ["/usr/bin/osascript", "-e"] and "rm -rf" not in script
    assert seen["args"][3:] == ["Claude Overlay", body]     # data, never code
