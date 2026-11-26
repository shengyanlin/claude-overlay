"""Auto-hide (AUTO_HIDE_SECONDS) and a hidden start (START_HIDDEN).

Auto-hide: the overlay hides itself after N seconds unused — never while it has the
keyboard, has the pointer over it, or Claude is still replying — and the hotkey brings it
back with a fresh countdown. A hidden start launches with no window until the hotkey.
No test here shows a real window."""
import time

import pytest

import claude_overlay as co
import config


@pytest.fixture
def auto(overlay, monkeypatch):
    calls = []
    monkeypatch.setattr(co, "AUTO_HIDE_SECONDS", 30)
    monkeypatch.setattr(overlay, "visible", True)
    monkeypatch.setattr(overlay, "busy", False)
    monkeypatch.setattr(overlay, "_idle_since", 1000.0)
    monkeypatch.setattr(overlay.root, "withdraw", lambda: calls.append("hide"))
    return overlay, calls


def test_off_by_default():
    assert config.AUTO_HIDE_SECONDS == 0


def test_no_overlay_method_is_defined_twice():
    # A second `def` with an existing name silently replaces the first. Adding auto-hide once
    # did exactly that to _overlay_in_use, the auto-updater's "has the user started?" guard.
    import ast
    import collections
    tree = ast.parse(open(co.__file__, encoding="utf-8").read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Overlay")
    names = collections.Counter(n.name for n in cls.body
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    assert [n for n, c in names.items() if c > 1] == []


def test_config_json_value_is_clamped_not_rejected():
    check = config._USER_CONFIG_KEYS["AUTO_HIDE_SECONDS"]
    assert check(30) == 30 and check(-5) == 0 and check(99999) == 3600


def test_hides_once_idle_for_the_set_time(auto, monkeypatch):
    ov, calls = auto
    monkeypatch.setattr(ov, "_auto_hide_paused", lambda: False)
    ov._auto_hide_check(now=1029.0)
    assert calls == [] and ov.visible is True
    ov._auto_hide_check(now=1030.0)
    assert calls == ["hide"] and ov.visible is False


def test_being_used_restarts_the_countdown(auto, monkeypatch):
    ov, calls = auto
    in_use = {"v": True}
    monkeypatch.setattr(ov, "_auto_hide_paused", lambda: in_use["v"])
    ov._auto_hide_check(now=1100.0)        # in use long past the limit: stays, countdown restarts
    assert calls == []
    in_use["v"] = False
    ov._auto_hide_check(now=1129.0)
    assert calls == []
    ov._auto_hide_check(now=1130.0)
    assert calls == ["hide"]


def test_never_hides_while_claude_is_replying(auto, monkeypatch):
    ov, calls = auto
    ov.busy = True
    monkeypatch.setattr(ov.root, "winfo_containing", lambda *a: None)   # pointer elsewhere
    monkeypatch.setattr(co._user32, "GetForegroundWindow", lambda: 0)    # keyboard elsewhere
    ov._auto_hide_check(now=5000.0)
    assert calls == []


def test_pointer_or_keyboard_in_the_overlay_counts_as_in_use(auto, monkeypatch):
    ov, _ = auto
    monkeypatch.setattr(ov, "_hwnd", lambda: 111)
    monkeypatch.setattr(co._user32, "GetForegroundWindow", lambda: 0)
    monkeypatch.setattr(ov.root, "winfo_containing", lambda *a: ov.root)
    assert ov._auto_hide_paused() is True                                  # pointer over it
    monkeypatch.setattr(ov.root, "winfo_containing", lambda *a: None)
    assert ov._auto_hide_paused() is False                                 # nothing: idle
    monkeypatch.setattr(co._user32, "GetForegroundWindow", lambda: 111)
    assert ov._auto_hide_paused() is True                                  # keyboard in it


def test_hidden_overlay_does_not_count_down(auto, monkeypatch):
    ov, calls = auto
    monkeypatch.setattr(ov, "_auto_hide_paused", lambda: False)
    ov.visible = False
    ov._auto_hide_check(now=9000.0)
    assert calls == [] and ov._idle_since == 9000.0


def test_summoning_gives_the_full_time_again(auto, monkeypatch):
    ov, _ = auto
    monkeypatch.setattr(ov.root, "deiconify", lambda: None)    # keep the test window hidden
    monkeypatch.setattr(ov, "_force_foreground", lambda: None)
    monkeypatch.setattr(ov, "_raise_to_front", lambda focus=False: None)
    ov.visible = False
    ov._show_window()
    assert time.monotonic() - ov._idle_since < 1.0


# ── START_HIDDEN: a sign-in launch that waits for the hotkey ─────────────────────────────

def test_start_hidden_is_off_by_default():
    assert config.START_HIDDEN is False


def test_hidden_start_skips_the_startup_taskbar_nudge(overlay, monkeypatch):
    # The nudge is a withdraw -> deiconify; on a hidden start it would put the window up.
    calls = []
    monkeypatch.setattr(co, "TASKBAR_BUTTON", True)
    monkeypatch.setattr(overlay, "visible", False)
    monkeypatch.setattr(overlay, "_taskbar_nudge_pending", False)
    monkeypatch.setattr(overlay.root, "withdraw", lambda: calls.append("withdraw"))
    monkeypatch.setattr(overlay.root, "deiconify", lambda: calls.append("deiconify"))
    overlay._install_taskbar_button()
    assert calls == [] and overlay._taskbar_nudge_pending is True


def test_first_show_after_a_hidden_start_stamps_the_taskbar_style_before_mapping(overlay, monkeypatch):
    calls = []
    monkeypatch.setattr(overlay, "visible", False)
    monkeypatch.setattr(overlay, "_taskbar_nudge_pending", True)
    monkeypatch.setattr(overlay, "_set_taskbar_button", lambda: calls.append("style"))
    monkeypatch.setattr(overlay.root, "deiconify", lambda: calls.append("map"))
    monkeypatch.setattr(overlay, "_force_foreground", lambda: None)
    monkeypatch.setattr(overlay, "_raise_to_front", lambda focus=False: None)
    overlay._show_window()
    assert calls[:2] == ["style", "map"] and overlay._taskbar_nudge_pending is False
