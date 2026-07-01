"""Tests for win32utils — the Windows-only ctypes helpers. They call the real Win32
API, so the module is skipped off Windows, and assertions stay at the level of
contracts that hold even on a headless CI runner (which may enumerate zero monitors)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Win32 helpers")

if sys.platform == "win32":
    import win32utils


def test_capture_affinity_constants():
    assert win32utils.WDA_NONE == 0x00
    assert win32utils.WDA_EXCLUDEFROMCAPTURE == 0x11


def test_exstyle_constants():
    assert win32utils.GWL_EXSTYLE == -20
    assert win32utils.WS_EX_APPWINDOW == 0x00040000
    assert win32utils.WS_EX_TOOLWINDOW == 0x00000080


def test_clipboard_format_ids():
    # Used by the "is there an image on the clipboard?" probe; text ids must be right so a
    # text copy pastes as text, not as an image (the v1.7.1 fix).
    assert win32utils.CF_UNICODETEXT == 13
    assert win32utils.CF_TEXT == 1


def test_dpi_and_appid_calls_never_raise():
    # Both are best-effort and must never raise into the caller, regardless of OS state.
    win32utils.set_dpi_awareness()
    win32utils.set_app_user_model_id()


def test_enumerate_monitors_shape():
    mons = win32utils.enumerate_monitors()
    assert isinstance(mons, list)
    for m in mons:
        assert {"rect", "work", "primary"} <= set(m)
        for key in ("rect", "work"):
            assert isinstance(m[key], tuple) and len(m[key]) == 4
            assert all(isinstance(v, int) for v in m[key])
        assert isinstance(m["primary"], bool)
    # primary-first ordering: if anything is flagged primary, it sorts to the front.
    if any(m["primary"] for m in mons):
        assert mons[0]["primary"] is True


def test_virtual_screen_metrics_shape():
    vs = win32utils.virtual_screen_metrics()
    # None only on a bare/headless runner; on a real display it's a 4-int box with positive size.
    if vs is not None:
        assert len(vs) == 4 and all(isinstance(v, int) for v in vs)
        assert vs[2] > 0 and vs[3] > 0


# ── compute_onscreen_move: the pure "did the window get stranded off-screen?" decision ──
# (This is the core of the unplug-a-monitor fix; kept Win32-free so it's fully unit-testable.)

_PRIMARY = {"rect": (0, 0, 1920, 1080), "work": (0, 0, 1920, 1040), "primary": True}
_SECOND_RIGHT = {"rect": (1920, 0, 3840, 1080), "work": (1920, 0, 3840, 1040), "primary": False}
_SECOND_LEFT = {"rect": (-1920, 0, 0, 1080), "work": (-1920, 0, 0, 1040), "primary": False}


def test_onscreen_move_none_when_fully_on_a_monitor():
    # A window comfortably inside the primary monitor must not be moved.
    assert win32utils.compute_onscreen_move((100, 100, 420, 620), [_PRIMARY]) is None


def test_onscreen_move_none_on_legit_negative_coord_secondary():
    # A window living on a secondary monitor at negative X (left of primary) is NOT off-screen —
    # the fix must not yank windows off a legitimate monitor just because their coords are negative.
    assert win32utils.compute_onscreen_move((-1800, 100, 420, 620),
                                            [_PRIMARY, _SECOND_LEFT]) is None


def test_onscreen_move_pulls_back_when_monitor_unplugged():
    # Window was on the right-hand secondary monitor; that monitor is now gone (only primary left)
    # → it's stranded off-screen → must move onto the primary work area.
    mv = win32utils.compute_onscreen_move((2600, 200, 420, 620), [_PRIMARY])
    assert mv is not None
    nx, ny = mv
    # the whole window now fits inside the primary work area
    assert _PRIMARY["work"][0] <= nx and nx + 420 <= _PRIMARY["work"][2]
    assert _PRIMARY["work"][1] <= ny and ny + 620 <= _PRIMARY["work"][3]


def test_onscreen_move_picks_nearest_monitor():
    # Stranded far below both monitors, but nearer (in X) to the right one → clamp onto the right.
    mv = win32utils.compute_onscreen_move((3000, 5000, 420, 620), [_PRIMARY, _SECOND_RIGHT])
    assert mv is not None
    nx, ny = mv
    assert _SECOND_RIGHT["work"][0] <= nx and nx + 420 <= _SECOND_RIGHT["work"][2]


def test_onscreen_move_uses_work_area_not_full_rect():
    # Clamped bottom must respect the work area (above the taskbar), not the full monitor rect.
    mv = win32utils.compute_onscreen_move((0, 9999, 420, 620), [_PRIMARY])
    assert mv is not None
    _, ny = mv
    assert ny + 620 <= _PRIMARY["work"][3]        # 1040, not 1080


def test_onscreen_move_none_with_no_monitors():
    # Degenerate inputs never crash and never invent a move.
    assert win32utils.compute_onscreen_move((100, 100, 420, 620), []) is None
    assert win32utils.compute_onscreen_move((100, 100, 0, 0), [_PRIMARY]) is None


def test_onscreen_move_barely_visible_strip_counts_as_reachable():
    # A window a user parked mostly off the right edge, but with a >min_vis strip still showing,
    # is left alone (only a fully-stranded window is pulled back).
    win = (1920 - 120, 100, 420, 620)   # 120px still visible on the primary
    assert win32utils.compute_onscreen_move(win, [_PRIMARY], min_vis_w=48, min_vis_h=32) is None
