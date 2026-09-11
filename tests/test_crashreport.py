"""Tests for the startup crash reporter.

The thing under test is a diagnostic, so the failure mode that matters is the quiet one:
a reporter that writes nothing, or that raises while reporting, is indistinguishable from
the silent death it exists to replace. These tests therefore assert on the OBSERVABLE
outputs (a file on disk, a dialog call, a process exit code) rather than on internals,
and deliberately include the hostile cases — unwritable log directory, a dying thread,
a console-less run — because those are the conditions the real bug happens under.
"""
import os
import subprocess
import sys
import textwrap
import threading

import pytest

import crashreport


@pytest.fixture
def applog(tmp_path, monkeypatch):
    """Point the reporter's log directory at a throwaway path (never this machine's)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("CLAUDE_OVERLAY_DIALOG", raising=False)
    return tmp_path / "claude-overlay" / "crash.log"


def _no_dialogs(monkeypatch):
    """Record dialog calls instead of showing them (a modal in a test run is a hang)."""
    shown = []
    monkeypatch.setattr(crashreport, "_dialog", lambda t, b: shown.append((t, b)) or True)
    return shown


# ── the log ─────────────────────────────────────────────────────────────────
def test_report_writes_traceback_and_environment(applog, monkeypatch):
    _no_dialogs(monkeypatch)
    try:
        raise ValueError("boom")
    except ValueError as e:
        path = crashreport.report_exception(e, "Unhandled error", app_version="9.9.9")

    assert path and os.path.isfile(path)
    text = open(path, encoding="utf-8").read()
    assert "ValueError: boom" in text
    assert "Unhandled error" in text
    assert "9.9.9" in text
    # The environment block is the half of a bug report users never include; the
    # interpreter path in particular is what makes a two-Python machine visible.
    assert sys.executable in text
    assert "claude-agent-sdk" in text


def test_log_rotates_and_keeps_one_generation(applog, monkeypatch):
    _no_dialogs(monkeypatch)
    applog.parent.mkdir(parents=True, exist_ok=True)
    applog.write_text("x" * (crashreport._MAX_LOG_BYTES + 10), encoding="utf-8")

    crashreport.report("second crash")

    assert (applog.parent / "crash.log.old").is_file(), "previous log should be kept"
    assert "second crash" in applog.read_text(encoding="utf-8")
    assert applog.stat().st_size < crashreport._MAX_LOG_BYTES


def test_report_survives_an_unwritable_log_dir(tmp_path, monkeypatch):
    """A reporter that throws turns one legible failure into two illegible ones."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))   # makedirs() cannot succeed here
    shown = _no_dialogs(monkeypatch)
    monkeypatch.setattr(crashreport, "has_console", lambda: False)

    path = crashreport.report("could not be logged")

    assert path is None                 # honest about not having written anything
    assert shown, "the user must still be told, even when the log can't be written"


# ── the dialog ──────────────────────────────────────────────────────────────
def test_dialog_shown_only_when_there_is_no_console(applog, monkeypatch):
    """Under pythonw a dialog is the only channel; with a console it would block an
    automated run, and a hang is a worse bug than the one being reported."""
    shown = _no_dialogs(monkeypatch)

    monkeypatch.setattr(crashreport, "has_console", lambda: True)
    crashreport.report("with a console")
    assert shown == []

    monkeypatch.setattr(crashreport, "has_console", lambda: False)
    crashreport.report("without a console")
    assert len(shown) == 1
    assert "without a console" in shown[0][1]


@pytest.mark.parametrize("env,console,expected", [
    ("0", False, False),    # forced off beats "no console"
    ("1", True, True),      # forced on beats "has console"
    ("", False, True),
    ("", True, False),
])
def test_dialog_env_override(applog, monkeypatch, env, console, expected):
    shown = _no_dialogs(monkeypatch)
    monkeypatch.setattr(crashreport, "has_console", lambda: console)
    if env:
        monkeypatch.setenv("CLAUDE_OVERLAY_DIALOG", env)
    crashreport.report("check the gate")
    assert bool(shown) is expected


# ── the hooks ───────────────────────────────────────────────────────────────
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_excepthook_logs_a_dying_worker(applog, monkeypatch):
    """The conversation runs on a daemon thread. Before this hook, a worker that died
    left a window that simply never answered again, with nothing written anywhere."""
    monkeypatch.setattr(crashreport, "_installed", False)
    _no_dialogs(monkeypatch)
    prev_sys, prev_thread = sys.excepthook, threading.excepthook
    try:
        crashreport.install("1.2.3")
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError("thread died")),
                             name="claude-worker")
        t.start()
        t.join(timeout=5)
    finally:
        sys.excepthook, threading.excepthook = prev_sys, prev_thread
        crashreport._installed = False

    text = applog.read_text(encoding="utf-8")
    assert "RuntimeError: thread died" in text
    assert "claude-worker" in text


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(crashreport, "_installed", False)
    prev_sys, prev_thread = sys.excepthook, threading.excepthook
    try:
        crashreport.install()
        first = sys.excepthook
        crashreport.install()
        assert sys.excepthook is first, "a second install must not re-wrap the hook"
    finally:
        sys.excepthook, threading.excepthook = prev_sys, prev_thread
        crashreport._installed = False


# ── the guard, end to end ───────────────────────────────────────────────────
def test_guard_reports_and_exits_nonzero(tmp_path):
    """os._exit can't be observed in-process, so run it for real in a subprocess -
    which also proves the whole path works from a cold interpreter."""
    script = tmp_path / "boom.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
        import crashreport
        with crashreport.guard("starting up", "1.2.3"):
            raise RuntimeError("startup exploded")
        print("UNREACHABLE")
    """), encoding="utf-8")

    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), CLAUDE_OVERLAY_DIALOG="0")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       timeout=120, env=env)

    assert r.returncode == 1
    assert "UNREACHABLE" not in r.stdout
    log = tmp_path / "claude-overlay" / "crash.log"
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "RuntimeError: startup exploded" in text
    assert "failed while starting up" in text


def test_guard_lets_a_clean_exit_through(tmp_path):
    """KeyboardInterrupt / SystemExit are how the app quits normally - the guard must
    not turn Ctrl+C into a crash report."""
    script = tmp_path / "quit.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
        import crashreport
        with crashreport.guard("starting up"):
            raise SystemExit(0)
    """), encoding="utf-8")

    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), CLAUDE_OVERLAY_DIALOG="0")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       timeout=120, env=env)

    assert r.returncode == 0
    assert not (tmp_path / "claude-overlay" / "crash.log").exists()
