"""The overlay updating ITSELF at launch (OVERLAY_AUTO_UPDATE).

What these lock down: the launch's update check used to only offer a button. Now, when the
install can take the update cleanly, it runs update.cmd on its own and restarts into the new
version -- but only at launch: once the user has started (a message, a resumed conversation, a
draft in the box), nothing restarts on them. Plus the guards that keep "automatic" from turning
into a nag or a loop: a clone update.cmd would stop on gets the button, a folder the update
didn't move is not restarted (and not retried for that version), and two windows can't run two
updates at once. update.cmd is never spawned here.
"""
import os
import shutil
import subprocess
import time
import tkinter as tk

import pytest

import claude_overlay as co
import config
import win32utils
from conftest import chat_text


class _NoThread:
    """Swallows the background update thread, recording the name it was started under."""
    names = []

    def __init__(self, *a, **k):
        _NoThread.names.append(k.get("name"))

    def start(self):
        pass


def _embedded_canvases(ov):
    return [ov.root.nametowidget(n) for n in ov.chat.window_names()
            if isinstance(ov.root.nametowidget(n), tk.Canvas)]


@pytest.fixture
def auto_ok(overlay, monkeypatch):
    """Every precondition for an automatic update met; the lock file cleaned up after."""
    monkeypatch.setattr(co, "OVERLAY_AUTO_UPDATE", True)
    monkeypatch.setattr(co, "can_self_update", lambda: True)
    monkeypatch.setattr(co, "overlay_update_blocker", lambda: None)
    lock = overlay._update_lock_path()
    yield overlay
    overlay._release_update_lock()
    try:
        lock.unlink()
    except FileNotFoundError:
        pass


@pytest.fixture
def restarts(overlay, monkeypatch):
    calls = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: calls.append(1))
    return calls


# ── deciding whether this launch may update on its own (_update_kind) ─────────────────

def test_a_clean_clone_updates_on_its_own_and_takes_the_lock(auto_ok):
    assert auto_ok._update_kind("9.0.0") == "update_auto"
    assert auto_ok._update_lock_path().exists()


def test_the_setting_off_means_the_button(auto_ok, monkeypatch):
    monkeypatch.setattr(co, "OVERLAY_AUTO_UPDATE", False)
    assert auto_ok._update_kind("9.0.0") == "update"


def test_a_zip_install_means_the_button(auto_ok, monkeypatch):
    monkeypatch.setattr(co, "can_self_update", lambda: False)
    assert auto_ok._update_kind("9.0.0") == "update"


def test_a_clone_update_cmd_would_stop_on_means_the_button(auto_ok, monkeypatch):
    # Off main or with local edits, update.cmd stops at a `pause`: a console popping up and
    # waiting on every launch of a developer's clone is a nag, not an update.
    monkeypatch.setattr(co, "overlay_update_blocker", lambda: "this clone has uncommitted changes")
    assert auto_ok._update_kind("9.0.0") == "update"
    assert not auto_ok._update_lock_path().exists()


def test_a_version_an_earlier_launch_couldnt_move_to_means_the_button(auto_ok):
    co._save_state(ov_auto_update_skip="9.0.0")
    try:
        assert auto_ok._update_kind("9.0.0") == "update"
        assert auto_ok._update_kind("9.0.1") == "update_auto"     # a newer release tries again
    finally:
        co._save_state(ov_auto_update_skip=None)


def test_a_second_window_does_not_run_a_second_update(auto_ok):
    assert auto_ok._update_kind("9.0.0") == "update_auto"
    auto_ok._update_lock_held = False          # pretend it's another window's lock
    try:
        assert auto_ok._update_kind("9.0.0") == "update"
    finally:
        auto_ok._update_lock_path().unlink()


def test_a_lock_left_by_a_dead_window_is_taken_over(auto_ok):
    lock = auto_ok._update_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345")
    old = time.time() - auto_ok.UPDATE_LOCK_STALE - 60
    os.utime(lock, (old, old))
    assert auto_ok._update_kind("9.0.0") == "update_auto"


# ── starting it: only while the conversation is still empty ───────────────────────────

def test_at_launch_it_starts_the_updater_without_a_click(auto_ok, monkeypatch):
    _NoThread.names = []
    monkeypatch.setattr(co.threading, "Thread", _NoThread)
    auto_ok._handle("update_auto", "9.0.0")
    txt = chat_text(auto_ok)
    assert "on its own" in txt and "9.0.0" in txt and co.__version__ in txt
    assert "overlay-auto-update" in _NoThread.names
    assert _embedded_canvases(auto_ok) == []           # no button to click
    assert auto_ok._update_available == "9.0.0"        # the statusline ⬆ still shows


def test_once_the_user_has_started_it_falls_back_to_the_button(auto_ok, monkeypatch):
    _NoThread.names = []
    monkeypatch.setattr(co.threading, "Thread", _NoThread)
    assert auto_ok._update_kind("9.0.0") == "update_auto"     # holds the lock
    auto_ok._has_turns = True                                  # a message went out first
    auto_ok._handle("update_auto", "9.0.0")
    assert "overlay-auto-update" not in _NoThread.names
    assert "Update available" in chat_text(auto_ok)
    assert len(_embedded_canvases(auto_ok)) == 1               # the Update button
    assert not auto_ok._update_lock_path().exists()            # and the lock is let go


# ── when update.cmd is done ───────────────────────────────────────────────────────────

def test_success_on_an_unused_window_restarts_into_it(auto_ok, restarts, monkeypatch):
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))
    assert restarts == [1]
    assert "restarting into it" in chat_text(auto_ok)


def test_success_after_the_user_started_keeps_the_window(auto_ok, restarts, monkeypatch):
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    auto_ok._has_turns = True
    auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))
    assert restarts == []
    assert "next time you open the overlay" in chat_text(auto_ok)
    (btn,) = _embedded_canvases(auto_ok)
    assert btn._ustate == "done"                      # the restart offer, not an update button
    btn._click(None)
    assert restarts == [1]


def test_a_draft_in_the_box_is_not_thrown_away_by_a_restart(auto_ok, restarts, monkeypatch):
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    auto_ok._ph_active = False                        # the box shows typed text, not the hint
    auto_ok.entry.delete("1.0", "end")
    auto_ok.entry.insert("1.0", "half a question")
    auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))
    assert restarts == []
    assert auto_ok._entry_text() == "half a question"


def test_a_running_cli_update_is_waited_for_then_it_restarts(auto_ok, restarts, monkeypatch):
    # The real pause (as the launch's CLI auto-update does it): it sets busy so Enter lines
    # messages up. That is maintenance, not the user - the restart must wait, not give up.
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    assert auto_ok._begin_cli_update("claude") == "ok"
    assert auto_ok.busy is True
    auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))
    assert restarts == []
    assert auto_ok._ov_auto_after is not None         # checks again shortly
    assert _embedded_canvases(auto_ok) == []          # no "you'd already started" button
    auto_ok.root.after_cancel(auto_ok._ov_auto_after)
    auto_ok._end_cli_update()                         # the CLI update finished
    auto_ok._finish_overlay_auto_update("9.0.0")      # what that timer runs
    assert restarts == [1]


def test_a_message_lined_up_during_the_cli_pause_counts_as_use(auto_ok, restarts, monkeypatch):
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    assert auto_ok._begin_cli_update("claude") == "ok"
    auto_ok._queue.append({"text": "hi", "shots": None, "images": [], "docs": [], "auto": False})
    try:
        assert auto_ok._overlay_in_use() is True
    finally:
        auto_ok._queue = []
        auto_ok._end_cli_update()


def test_a_cli_update_at_launch_does_not_stop_the_overlay_update(auto_ok, monkeypatch):
    _NoThread.names = []
    monkeypatch.setattr(co.threading, "Thread", _NoThread)
    assert auto_ok._begin_cli_update("claude") == "ok"   # the CLI check landed first
    try:
        auto_ok._handle("update_auto", "9.0.0")
        assert "overlay-auto-update" in _NoThread.names
        assert "Update available" not in chat_text(auto_ok)
    finally:
        auto_ok._end_cli_update()


@pytest.mark.parametrize("attr, value", [
    ("_resume_started", 1.0),        # a history card's Resume: no busy, no Resume-offer state
    ("_paste_busy", True),           # an image still being processed into an attachment
    ("_capture_busy", True),         # a screenshot being taken
])
def test_work_in_flight_counts_as_use(auto_ok, restarts, monkeypatch, attr, value):
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    monkeypatch.setattr(auto_ok, attr, value)
    assert auto_ok._overlay_in_use() is True
    auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))
    assert restarts == []


def test_an_update_landing_during_a_screenshot_only_send_does_not_restart(auto_ok, restarts,
                                                                          monkeypatch, tmp_path):
    # Send with an empty box and auto-shot on: _collect_send's capture() can pump Tk events
    # (root.update), so the update's result may be handled mid-send, before the turn exists.
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    shot = tmp_path / "screen.png"
    shot.write_bytes(b"PNG")

    def capture(announce=True):
        auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))   # lands inside the capture
        return [{"path": str(shot), "index": 0, "primary": True}]
    monkeypatch.setattr(auto_ok, "capture", capture)
    auto_ok.auto_shot = True
    auto_ok._precaptured = None
    auto_ok.busy = False
    auto_ok._ph_active = True                         # the box shows only the placeholder
    auto_ok.worker.calls.clear()
    auto_ok._send_or_stop()
    assert restarts == []
    assert [c for c in auto_ok.worker.calls if c[0] == "ask"]   # the send went out
    assert auto_ok._acting == 0                       # and the guard was let go


def test_an_update_landing_during_a_snap_does_not_restart(auto_ok, restarts, monkeypatch, tmp_path):
    # The Snap button: capture() hides the window and pumps Tk events before the shot is put
    # in pending_shot, so the update's result may be handled while the window looks idle.
    monkeypatch.setattr(co, "_file_version", lambda: "9.0.0")
    shot = tmp_path / "snap.png"
    shot.write_bytes(b"PNG")

    def capture(announce=True, hide=True, quiet=False):
        auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))   # lands inside the capture
        shots = [{"path": str(shot), "index": 0, "primary": True}]
        if announce:
            auto_ok.pending_shot = shots
        return shots
    monkeypatch.setattr(auto_ok, "capture", capture)
    auto_ok.snap_now()
    assert restarts == []
    assert auto_ok.pending_shot                       # the shot is kept
    assert auto_ok._acting == 0


def test_a_draft_typed_before_the_check_lands_gets_the_button(auto_ok, monkeypatch):
    _NoThread.names = []
    monkeypatch.setattr(co.threading, "Thread", _NoThread)
    auto_ok._ph_active = False
    auto_ok.entry.delete("1.0", "end")
    auto_ok.entry.insert("1.0", "already typing")
    auto_ok._handle("update_auto", "9.0.0")
    assert "overlay-auto-update" not in _NoThread.names
    assert "Update available" in chat_text(auto_ok)


def test_an_update_that_did_not_move_the_folder_does_not_restart(auto_ok, restarts, monkeypatch):
    # A restart here would find the same "newer" release and update + restart again, every
    # launch. So: no restart, and no more automatic tries at this version.
    monkeypatch.setattr(co, "_file_version", lambda: co.__version__)
    try:
        auto_ok._handle("ov_auto_result", (True, "", "9.0.0"))
        assert restarts == []
        assert "still on" in chat_text(auto_ok)
        assert co._load_state().get("ov_auto_update_skip") == "9.0.0"
        assert auto_ok._update_kind("9.0.0") == "update"
    finally:
        co._save_state(ov_auto_update_skip=None)


def test_a_failed_update_offers_a_retry_and_releases_the_lock(auto_ok, restarts):
    assert auto_ok._update_kind("9.0.0") == "update_auto"
    auto_ok._handle("ov_auto_result", (False, "update.cmd stopped with exit code 1", "9.0.0"))
    assert restarts == []
    txt = chat_text(auto_ok)
    assert "Couldn't update the overlay on its own" in txt and "exit code 1" in txt
    (btn,) = _embedded_canvases(auto_ok)
    assert btn._ustate == "idle"                      # the ordinary Update button, for a retry
    assert not auto_ok._update_lock_path().exists()


def test_a_malformed_result_never_raises(auto_ok, restarts):
    auto_ok._handle("ov_auto_result", "garbage")
    assert restarts == []


# ── the setting ────────────────────────────────────────────────────────────────────────

def test_the_setting_is_on_by_default_and_overridable():
    assert config.OVERLAY_AUTO_UPDATE is True
    assert "OVERLAY_AUTO_UPDATE" in config._USER_CONFIG_KEYS
    assert config._ENV_BEATS_JSON["OVERLAY_AUTO_UPDATE"] == "CLAUDE_OVERLAY_AUTO_UPDATE"


# ── overlay_update_blocker: the same stops update.cmd makes, against a real git ──────

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_a_clean_main_is_not_blocked(clone):
    assert win32utils.overlay_update_blocker(str(clone)) is None


def test_untracked_files_do_not_block(clone):
    (clone / "notes.log").write_text("scratch")
    assert win32utils.overlay_update_blocker(str(clone)) is None


def test_local_edits_block(clone):
    (clone / "app.py").write_text("x = 2\n")
    assert "uncommitted" in win32utils.overlay_update_blocker(str(clone))


def test_another_branch_blocks(clone):
    _git(clone, "switch", "-q", "-c", "topic")
    assert '"topic"' in win32utils.overlay_update_blocker(str(clone))


def test_not_a_clone_blocks_and_never_raises(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    assert win32utils.overlay_update_blocker(str(tmp_path)) is not None
