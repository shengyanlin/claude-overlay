"""The global hotkey's show/hide decision (Overlay.toggle_visible), the guard that keeps a
hidden window hidden (Overlay._raise_to_front), and the key-repeat filter
(Overlay._hotkey_fired).

The bugs these pin down:
- the hide never stuck: Tk fires <FocusIn> right after `wm withdraw`, and the focus handler's
  raise (SWP_SHOWWINDOW) put the window straight back while the app believed it hidden, so
  presses alternated between a hide-flash and doing nothing;
- the overlay is always-on-top, so while you work in another app it sits in plain view
  WITHOUT the keyboard. toggle_visible used to hide only when the overlay was the foreground
  window, so a press from another app just re-focused a window that was already on top."""
import time
import types

import pytest

import claude_overlay as co


@pytest.fixture
def toggle_env(overlay, monkeypatch):
    """The overlay as 'visible', with the window calls replaced by recorders, so no test
    shows a real window or steals focus. Returns (overlay, calls, set_state)."""
    calls = []
    monkeypatch.setattr(overlay, "visible", True)
    monkeypatch.setattr(overlay, "_hwnd", lambda: 111)
    monkeypatch.setattr(overlay, "_show_window",
                        lambda: (calls.append("show"), setattr(overlay, "visible", True)))
    monkeypatch.setattr(overlay.root, "withdraw", lambda: calls.append("hide"))
    state = {"fg": 0, "in_view": True}
    monkeypatch.setattr(co._user32, "GetForegroundWindow", lambda: state["fg"])
    monkeypatch.setattr(co, "window_in_view", lambda hwnd: state["in_view"])

    def set_state(fg, in_view):
        state.update(fg=fg, in_view=in_view)
    return overlay, calls, set_state


def test_hidden_overlay_is_shown(toggle_env):
    ov, calls, set_state = toggle_env
    ov.visible = False
    set_state(fg=0, in_view=False)
    ov.toggle_visible()
    assert calls == ["show"] and ov.visible is True


def test_in_view_but_unfocused_overlay_hides(toggle_env):
    # The reported bug: you're typing in another app, the overlay is on top in plain view.
    ov, calls, set_state = toggle_env
    set_state(fg=999, in_view=True)
    ov.toggle_visible()
    assert calls == ["hide"] and ov.visible is False


def test_focused_overlay_hides(toggle_env):
    ov, calls, set_state = toggle_env
    set_state(fg=111, in_view=True)
    ov.toggle_visible()
    assert calls == ["hide"] and ov.visible is False


def test_covered_overlay_is_summoned_not_hidden(toggle_env):
    # Another always-on-top window covers it: the hotkey means "bring Claude to me".
    ov, calls, set_state = toggle_env
    set_state(fg=999, in_view=False)
    ov.toggle_visible()
    assert calls == ["show"] and ov.visible is True


@pytest.fixture
def raise_calls(overlay, monkeypatch):
    """Record the Win32 raise instead of doing it; returns the SetWindowPos call list."""
    calls = []
    monkeypatch.setattr(co._user32, "SetWindowPos", lambda *a: calls.append(a) or 1)
    monkeypatch.setattr(co._user32, "BringWindowToTop", lambda *a: 1)
    monkeypatch.setattr(co, "TASKBAR_BUTTON", True)
    monkeypatch.setattr(overlay, "_fronting", False)
    return calls


def test_focus_in_after_a_hotkey_hide_does_not_reshow(overlay, monkeypatch, raise_calls):
    # The "hides for a blink, then pops back" bug: Tk fires <FocusIn> on the root right after
    # `wm withdraw`, and _on_focus_in → _raise_to_front's SetWindowPos(SWP_SHOWWINDOW) put the
    # just-hidden window straight back on screen.
    monkeypatch.setattr(overlay, "visible", False)
    overlay._on_focus_in(types.SimpleNamespace(widget=overlay.root))
    overlay._raise_to_front(focus=True)
    assert raise_calls == []


def test_hotkey_show_still_raises_the_window(overlay, monkeypatch, raise_calls):
    monkeypatch.setattr(overlay, "visible", False)
    monkeypatch.setattr(overlay.root, "deiconify", lambda: None)   # keep the test window hidden
    monkeypatch.setattr(overlay, "_force_foreground", lambda: None)
    overlay._show_window()
    assert overlay.visible is True
    assert raise_calls, "showing must still lift the window above other always-on-top windows"


def test_held_hotkey_auto_repeat_toggles_once(overlay, monkeypatch):
    # The `keyboard` library calls back on every auto-repeat key-down while the combo is held.
    monkeypatch.setattr(overlay, "_toggle_request", False)
    monkeypatch.setattr(overlay, "_hotkey_last", 0.0)
    overlay._hotkey_fired()
    assert overlay._toggle_request is True
    overlay._toggle_request = False
    for _ in range(5):                 # repeats arrive tens of ms apart while held
        overlay._hotkey_fired()
    assert overlay._toggle_request is False
    # A fresh press after a quiet gap counts again.
    overlay._hotkey_last = time.monotonic() - (overlay.HOTKEY_REPEAT_GAP + 0.1)
    overlay._hotkey_fired()
    assert overlay._toggle_request is True
