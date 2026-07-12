"""UI tests for the 'your Claude CLI is out of date' notice + one-click Update button.

Drives the real Overlay methods on the shared hidden-root fixture (see conftest) and asserts
the resulting chat text / embedded-button state. No npm or CLI is ever spawned — the tests call
the render/handler methods directly and never click the button (whose click spawns npm)."""
import tkinter as tk

import claude_overlay as co
from conftest import chat_text


def _reset_cli_flags(ov):
    ov._cli_update_shown = False
    ov._cli_update_btn_ref = None
    ov._restarting = False


def test_notice_renders_versions_and_button(overlay):
    _reset_cli_flags(overlay)
    overlay._handle("cli_update", {"installed": "2.1.198", "latest": "2.1.204", "behind": True})
    txt = chat_text(overlay)
    assert "out of date" in txt
    assert "2.1.198" in txt and "2.1.204" in txt
    assert overlay._cli_update_shown is True
    # an embedded Update button (a Canvas) was added to the chat
    assert any(isinstance(overlay.root.nametowidget(n), tk.Canvas)
               for n in overlay.chat.window_names())


def test_notice_shown_at_most_once_per_session(overlay):
    _reset_cli_flags(overlay)
    overlay._handle("cli_update", {"installed": "2.1.198", "latest": "2.1.204", "behind": True})
    before = chat_text(overlay)
    overlay._handle("cli_update", {"installed": "2.1.198", "latest": "2.1.204", "behind": True})
    assert chat_text(overlay) == before          # guard: the second notice is a no-op


def test_button_starts_idle_and_state_can_change(overlay):
    btn = overlay._cli_update_btn("2.1.204")
    assert isinstance(btn, tk.Canvas)
    assert btn._ustate == "idle"
    btn._set_ustate("working")
    assert btn._ustate == "working"


def test_success_result_restyles_button_and_reports_restart(overlay):
    _reset_cli_flags(overlay)
    overlay._handle("cli_update", {"installed": "2.1.198", "latest": "2.1.204", "behind": True})
    # simulate a click having started the update (which records the button ref)
    btn = overlay._cli_update_btn("2.1.204")
    overlay._cli_update_btn_ref = btn
    overlay._handle("cli_update_result", (True, "2.1.204"))
    assert btn._ustate == "done"
    txt = chat_text(overlay).lower()
    assert "updated to v2.1.204" in txt
    assert "restart" in txt or "reopen" in txt


def test_failure_result_reports_manual_command(overlay):
    _reset_cli_flags(overlay)
    overlay._handle("cli_update", {"installed": "2.1.198", "latest": "2.1.204", "behind": True})
    btn = overlay._cli_update_btn("2.1.204")
    overlay._cli_update_btn_ref = btn
    overlay._handle("cli_update_result", (False, "npm error EACCES"))
    txt = chat_text(overlay)
    assert "npm install -g @anthropic-ai/claude-code@latest" in txt
    assert "npm error EACCES" in txt
    assert btn._ustate == "error"


def test_bad_payload_never_raises(overlay):
    _reset_cli_flags(overlay)
    # a malformed notice payload is ignored, not crashed on
    overlay._handle("cli_update", "not-a-dict")
    assert overlay._cli_update_shown is False
    # a malformed result payload degrades to a failure line, no exception
    overlay._handle("cli_update_result", None)
    assert "npm install -g @anthropic-ai/claude-code@latest" in chat_text(overlay)


# ── click "✓ Updated" to restart the overlay ────────────────────────────────────────

def test_restart_overlay_relaunches_and_notes_it(overlay, monkeypatch):
    _reset_cli_flags(overlay)
    seen = {}
    monkeypatch.setattr(co, "relaunch_overlay", lambda p: seen.update(path=p) or 4321)
    monkeypatch.setattr(overlay, "quit", lambda: seen.setdefault("quit", True))  # after(500) won't fire here
    overlay._restart_overlay()
    assert overlay._restarting is True
    assert seen.get("path", "").endswith("claude_overlay.py")
    assert "restarting" in chat_text(overlay).lower()


def test_restart_overlay_does_not_quit_when_relaunch_fails(overlay, monkeypatch):
    _reset_cli_flags(overlay)
    quit_calls = []

    def _boom(_p):
        raise RuntimeError("nope")
    monkeypatch.setattr(co, "relaunch_overlay", _boom)
    monkeypatch.setattr(overlay, "quit", lambda: quit_calls.append(1))
    overlay._restart_overlay()
    assert overlay._restarting is False          # reset so a later retry is possible
    assert quit_calls == []                      # never tore down the working window
    assert "couldn't relaunch" in chat_text(overlay).lower()


def test_done_button_click_triggers_restart(overlay, monkeypatch):
    # Invoke the button's real click handler directly (event_generate on an unmapped canvas is
    # unreliable — that's why the Copy-button click test is the env-flaky/deselected one).
    _reset_cli_flags(overlay)
    triggered = []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: triggered.append(1))
    btn = overlay._cli_update_btn("2.1.204")
    btn._set_ustate("done")
    btn._click(None)
    assert triggered == [1]                      # a click in the 'done' state restarts


def test_error_button_click_retries_update(overlay, monkeypatch):
    # After a failed update (e.g. EBUSY), the button is in 'error' and clicking it RETRIES —
    # so once the user closes the other Claude window they can just click again.
    _reset_cli_flags(overlay)
    triggered, started = [], []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: triggered.append("restart"))

    class _NoThread:
        def __init__(self, *a, **k):
            started.append(k.get("name"))

        def start(self):
            pass
    monkeypatch.setattr(co.threading, "Thread", _NoThread)

    btn = overlay._cli_update_btn("2.1.204")
    btn._set_ustate("error")
    btn._click(None)
    assert triggered == []                       # a failed button doesn't restart
    assert btn._ustate == "working"              # it retries the update
    assert "cli-update" in started


def test_idle_button_click_starts_update_not_restart(overlay, monkeypatch):
    # An IDLE click starts an update, NOT a restart — guard the state routing. Stub Thread so no
    # real npm is spawned (and no async result races the assertions).
    _reset_cli_flags(overlay)
    triggered, started = [], []
    monkeypatch.setattr(overlay, "_restart_overlay", lambda: triggered.append("restart"))

    class _NoThread:
        def __init__(self, *a, **k):
            started.append(k.get("name"))

        def start(self):
            pass
    monkeypatch.setattr(co.threading, "Thread", _NoThread)

    btn = overlay._cli_update_btn("2.1.204")     # starts idle
    btn._click(None)
    assert triggered == []                       # idle click must NOT restart
    assert btn._ustate == "working"              # it kicked off the update thread instead
    assert "cli-update" in started
