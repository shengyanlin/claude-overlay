"""UI tests for the 'a newer overlay is out' notice + one-click Update button.

Sibling of test_ui_cli_update.py, for the OTHER update: the overlay updating itself by running
update.cmd. Drives the real Overlay methods on the shared hidden-root fixture and asserts the
chat text / embedded-button state. update.cmd is never spawned — run_overlay_update is stubbed
wherever a click is exercised, and the notice/result handlers spawn nothing at all."""
import tkinter as tk

import claude_overlay as co
import win32utils
from conftest import chat_text


def _drain(ov):
    """Empty the session-wide UI queue so the next get_nowait() returns THIS test's event."""
    while True:
        try:
            ov.ui_q.get_nowait()
        except Exception:
            return


def _reset_ov_flags(ov):
    ov._ov_update_shown = False
    ov._ov_update_btn_ref = None
    ov._restarting = False


class _NoThread:
    """Swallows the background update thread so a click never spawns update.cmd and no async
    result races the assertions. Records the thread name the click asked for."""
    names = []

    def __init__(self, *a, **k):
        _NoThread.names.append(k.get("name"))

    def start(self):
        pass


# ── the notice ─────────────────────────────────────────────────────────────────────

def test_notice_on_a_clone_renders_versions_and_a_button(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    monkeypatch.setattr(co, "can_self_update", lambda: True)
    overlay._handle("update", "1.18.0")
    txt = chat_text(overlay)
    assert "1.18.0" in txt and co.__version__ in txt
    assert overlay._update_available == "1.18.0"
    # an embedded Update button (a Canvas) was added to the chat
    assert any(isinstance(overlay.root.nametowidget(n), tk.Canvas)
               for n in overlay.chat.window_names())


def test_notice_on_a_zip_install_falls_back_to_instructions(overlay, monkeypatch):
    # No git clone → update.cmd could only print "re-download the ZIP", so we say it ourselves
    # rather than offer a button whose whole job is to refuse.
    _reset_ov_flags(overlay)
    monkeypatch.setattr(co, "can_self_update", lambda: False)
    before = list(overlay.chat.window_names())
    overlay._handle("update", "1.18.0")
    txt = chat_text(overlay)
    assert "update.cmd" in txt and "1.18.0" in txt
    assert list(overlay.chat.window_names()) == before      # no button embedded


def test_notice_shown_at_most_once_per_session(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    monkeypatch.setattr(co, "can_self_update", lambda: True)
    overlay._handle("update", "1.18.0")
    before = chat_text(overlay)
    overlay._handle("update", "1.18.0")
    assert chat_text(overlay) == before                     # guard: the second notice is a no-op


# ── the button ─────────────────────────────────────────────────────────────────────

def test_button_starts_idle_and_state_can_change(overlay):
    btn = overlay._ov_update_btn("1.18.0")
    assert isinstance(btn, tk.Canvas)
    assert btn._ustate == "idle"
    btn._set_ustate("working")
    assert btn._ustate == "working"


def test_idle_click_starts_the_updater_and_records_the_button(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    _NoThread.names = []
    restarts = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: restarts.append(1))
    monkeypatch.setattr(co.threading, "Thread", _NoThread)
    btn = overlay._ov_update_btn("1.18.0")                  # starts idle
    btn._click(None)
    assert restarts == []                                   # an idle click must NOT restart
    assert btn._ustate == "working"
    assert "overlay-update" in _NoThread.names              # its own thread name, not the CLI's
    assert overlay._ov_update_btn_ref is btn                # so the result restyles THIS button


def test_error_click_retries(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    _NoThread.names = []
    restarts = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: restarts.append(1))
    monkeypatch.setattr(co.threading, "Thread", _NoThread)
    btn = overlay._ov_update_btn("1.18.0")
    btn._set_ustate("error")
    btn._click(None)
    assert restarts == []
    assert btn._ustate == "working"
    assert "overlay-update" in _NoThread.names


def test_done_click_restarts(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    restarts = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: restarts.append(1))
    btn = overlay._ov_update_btn("1.18.0")
    btn._set_ustate("done")
    btn._click(None)
    assert restarts == [1]


def test_click_runs_the_updater_and_reports_its_outcome(overlay, monkeypatch):
    # The click's worker is wired to run_overlay_update and its (ok, msg) is what gets queued.
    _reset_ov_flags(overlay)
    monkeypatch.setattr(co, "run_overlay_update", lambda: (True, ""))
    ran = {}

    class _RunNow:
        def __init__(self, target=None, **k):
            ran["target"] = target

        def start(self):
            ran["target"]()
    monkeypatch.setattr(co.threading, "Thread", _RunNow)
    btn = overlay._ov_update_btn("1.18.0")
    _drain(overlay)                      # the fixture's queue is shared across the suite
    btn._click(None)
    assert overlay.ui_q.get_nowait() == ("ov_update_result", (True, ""))


def test_updater_failure_surfaces_as_a_failed_result(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    monkeypatch.setattr(co, "run_overlay_update",
                        lambda: (False, "update.cmd stopped with exit code 1"))

    class _RunNow:
        def __init__(self, target=None, **k):
            self.t = target

        def start(self):
            self.t()
    monkeypatch.setattr(co.threading, "Thread", _RunNow)
    btn = overlay._ov_update_btn("1.18.0")
    _drain(overlay)
    btn._click(None)
    kind, (ok, msg) = overlay.ui_q.get_nowait()
    assert kind == "ov_update_result" and ok is False and "exit code 1" in msg


# ── the result ─────────────────────────────────────────────────────────────────────

def test_success_result_restyles_button_and_restarts_by_itself(overlay, monkeypatch):
    # A successful update leaves the new code on disk and this process on the old modules, and
    # the click that used to bridge the two carried no decision -- so the result handler makes
    # the restart happen rather than asking for it.
    _reset_ov_flags(overlay)
    restarts = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: restarts.append(1))
    btn = overlay._ov_update_btn("1.18.0")
    overlay._ov_update_btn_ref = btn
    overlay._handle("ov_update_result", (True, ""))
    assert btn._ustate == "done"
    assert restarts == [1]                                  # restarted without a second click
    txt = chat_text(overlay).lower()
    assert "updated" in txt and "restart" in txt


def test_failure_result_names_the_manual_route_and_does_not_restart(overlay, monkeypatch):
    # Restarting into an update that did NOT land would swap a working window for the same
    # window, minus the console that says why -- the failure path stays put.
    _reset_ov_flags(overlay)
    restarts = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: restarts.append(1))
    btn = overlay._ov_update_btn("1.18.0")
    overlay._ov_update_btn_ref = btn
    overlay._handle("ov_update_result", (False, "git pull failed"))
    txt = chat_text(overlay)
    assert "git pull failed" in txt and "update.cmd" in txt
    assert btn._ustate == "error"
    assert restarts == []


def test_bad_result_payload_never_raises(overlay, monkeypatch):
    _reset_ov_flags(overlay)
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: None)
    overlay._handle("ov_update_result", None)               # degrades to a failure line
    assert "update.cmd" in chat_text(overlay)


# ── win32utils.can_self_update / run_overlay_update ────────────────────────────────

def test_can_self_update_needs_a_clone_git_and_update_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(win32utils.shutil, "which", lambda _n: r"C:\git\git.exe")
    assert win32utils.can_self_update(str(tmp_path)) is False       # empty folder
    (tmp_path / "update.cmd").write_text("rem")
    assert win32utils.can_self_update(str(tmp_path)) is False       # no .git yet
    (tmp_path / ".git").mkdir()
    assert win32utils.can_self_update(str(tmp_path)) is True
    monkeypatch.setattr(win32utils.shutil, "which", lambda _n: None)
    assert win32utils.can_self_update(str(tmp_path)) is False       # git not on PATH


def test_can_self_update_accepts_a_worktree_dot_git_file(tmp_path, monkeypatch):
    # In a git worktree, .git is a FILE pointing at the real gitdir — still updatable.
    monkeypatch.setattr(win32utils.shutil, "which", lambda _n: r"C:\git\git.exe")
    (tmp_path / "update.cmd").write_text("rem")
    (tmp_path / ".git").write_text("gitdir: ../.git/worktrees/x")
    assert win32utils.can_self_update(str(tmp_path)) is True


def test_run_overlay_update_without_the_script_fails_cleanly(tmp_path):
    ok, msg = win32utils.run_overlay_update(str(tmp_path))
    assert ok is False and "update.cmd" in msg


def test_run_overlay_update_maps_the_exit_code(tmp_path, monkeypatch):
    (tmp_path / "update.cmd").write_text("rem")
    seen = {}

    class _P:
        def __init__(self, rc):
            self.rc = rc

        def wait(self):
            return self.rc

    def _popen(args, **k):
        seen["args"], seen["kw"] = args, k
        return _P(seen["rc"])
    monkeypatch.setattr(win32utils.subprocess, "Popen", _popen)

    seen["rc"] = 0
    assert win32utils.run_overlay_update(str(tmp_path))[0] is True
    # goes through cmd (a .cmd shim can't be exec'd directly) and gets its OWN console, since
    # under pythonw there is none to inherit and the updater's `pause` prompts must be visible.
    assert seen["args"][:2] == ["cmd", "/c"]
    assert seen["kw"]["creationflags"] & 0x00000010
    # and it tells update.cmd it was started by the app, so update.cmd skips the `pause` on its
    # success exit -- otherwise this wait() hangs on a keypress nobody knows to make and the
    # button sits on "Updating..." after the update has already finished.
    assert seen["kw"]["env"]["OV_UPDATE_AUTO"] == "1"
    assert "PATH" in {k.upper() for k in seen["kw"]["env"]}   # inherited, not a bare 1-var env

    seen["rc"] = 1
    ok, msg = win32utils.run_overlay_update(str(tmp_path))
    assert ok is False and "1" in msg


def test_run_overlay_update_never_raises(tmp_path, monkeypatch):
    (tmp_path / "update.cmd").write_text("rem")

    def _boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(win32utils.subprocess, "Popen", _boom)
    ok, msg = win32utils.run_overlay_update(str(tmp_path))
    assert ok is False and "OSError" in msg
