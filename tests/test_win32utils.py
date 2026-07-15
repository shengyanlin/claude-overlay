"""Tests for win32utils — the Windows-only ctypes helpers. They call the real Win32
API, so the module is skipped off Windows, and assertions stay at the level of
contracts that hold even on a headless CI runner (which may enumerate zero monitors)."""
import os
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


# ── ensure_taskbar_shortcut: the self-heal that makes taskbar pinning work ──
# (The Start Menu .lnk carrying a matching AppUserModelID is what lets a pin relaunch the
# overlay when closed and show the Clawd icon, not pythonw's. Tests mock the PowerShell
# builder + isolate HOME/APPDATA, so they never touch the real Start Menu or spawn a shell.)

def test_pythonw_exe_prefers_windowless(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(win32utils.sys, "executable", str(tmp_path / "python.exe"))
    assert win32utils._pythonw_exe() == str(tmp_path / "pythonw.exe")


def test_pythonw_exe_falls_back_without_sibling(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_text("")   # no pythonw.exe next to it
    monkeypatch.setattr(win32utils.sys, "executable", str(tmp_path / "python.exe"))
    assert win32utils._pythonw_exe() == str(tmp_path / "python.exe")


class _FakeProc:
    def __init__(self, rc):
        self.returncode, self.stdout, self.stderr = rc, b"", b""


def _setup_shortcut_env(tmp_path, monkeypatch, rc=0):
    """Build an isolated fake repo + HOME/APPDATA and stub the PowerShell builder so the
    builder 'creates' the .lnk on success. Returns (script_path, calls-list)."""
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "install-startmenu-shortcut.ps1").write_text("stub")
    (repo / "claude_overlay_2.ico").write_text("ico")
    script = repo / "claude_overlay.py"; script.write_text("app")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if rc == 0:                                  # emulate the builder writing the .lnk
            lnk = args[args.index("-Lnk") + 1]
            os.makedirs(os.path.dirname(lnk), exist_ok=True)
            open(lnk, "w").close()
        return _FakeProc(rc)

    monkeypatch.setattr(win32utils.subprocess, "run", fake_run)
    return str(script), calls


def test_ensure_shortcut_creates_then_noops(tmp_path, monkeypatch):
    script, calls = _setup_shortcut_env(tmp_path, monkeypatch, rc=0)
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "created"
    assert len(calls) == 1
    # Second launch: .lnk + marker already match → cheap no-op, NO second spawn.
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "ok"
    assert len(calls) == 1


def test_ensure_shortcut_skipped_without_taskbar_button(tmp_path, monkeypatch):
    script, calls = _setup_shortcut_env(tmp_path, monkeypatch, rc=0)
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", False)
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "skipped"
    assert calls == []


def test_ensure_shortcut_skipped_without_app_id(tmp_path, monkeypatch):
    script, calls = _setup_shortcut_env(tmp_path, monkeypatch, rc=0)
    assert win32utils.ensure_taskbar_shortcut(script, app_id="") == "skipped"
    assert calls == []


def test_ensure_shortcut_error_on_builder_failure(tmp_path, monkeypatch):
    script, calls = _setup_shortcut_env(tmp_path, monkeypatch, rc=2)
    # A failed build must NOT write the marker, so the next launch retries.
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "error"
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "error"
    assert len(calls) == 2


def test_ensure_shortcut_error_when_builder_missing(tmp_path, monkeypatch):
    script, calls = _setup_shortcut_env(tmp_path, monkeypatch, rc=0)
    os.remove(os.path.join(os.path.dirname(script), "install-startmenu-shortcut.ps1"))
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "error"
    assert calls == []


def test_ensure_shortcut_recreates_when_signature_changes(tmp_path, monkeypatch):
    script, calls = _setup_shortcut_env(tmp_path, monkeypatch, rc=0)
    assert win32utils.ensure_taskbar_shortcut(script, app_id="test.app") == "created"
    # A different AppUserModelID changes the recorded signature → rebuild, not a no-op.
    assert win32utils.ensure_taskbar_shortcut(script, app_id="other.app") == "created"
    assert len(calls) == 2


# ── relaunch_overlay: spawn a fresh, detached instance so the app can restart itself ──

class _FakePopen:
    def __init__(self, pid=4321):
        self.pid = pid


def test_relaunch_overlay_spawns_detached_child(tmp_path, monkeypatch):
    rec = {}

    def fake_popen(args, cwd=None, creationflags=0):
        rec.update(args=args, cwd=cwd, flags=creationflags)
        return _FakePopen(4321)

    monkeypatch.setattr(win32utils, "_pythonw_exe", lambda: r"C:\py\pythonw.exe")
    monkeypatch.setattr(win32utils.subprocess, "Popen", fake_popen)
    script = str(tmp_path / "claude_overlay.py")
    assert win32utils.relaunch_overlay(script) == 4321
    assert rec["args"][0] == r"C:\py\pythonw.exe"
    assert rec["args"][1].endswith("claude_overlay.py")
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so quitting this instance can't kill the child.
    assert rec["flags"] == (0x00000008 | 0x00000200)
    assert rec["cwd"] == os.path.dirname(os.path.abspath(script))


def test_relaunch_overlay_raises_without_interpreter(monkeypatch):
    # No interpreter → raise (caller must NOT then quit, or the user is left with nothing).
    monkeypatch.setattr(win32utils, "_pythonw_exe", lambda: "")
    with pytest.raises(Exception):
        win32utils.relaunch_overlay("x.py")


# ── set_window_app_id: stamp the window-level AppUserModelID (Store-Python taskbar-icon fix) ──
# A window-level AUMID outranks both the process id and an MSIX host's package id, so Microsoft
# Store Python shows the Clawd icon instead of pythonw's. The COM happy path needs a live
# top-level HWND (can't be faked on a headless runner), so these lock in the best-effort
# contract instead: every guard and every failure path returns False and never raises.

def test_set_window_app_id_false_without_taskbar_button(monkeypatch):
    # Taskbar button disabled → short-circuit before any COM call.
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", False)
    assert win32utils.set_window_app_id(0x10, app_id="test.app") is False


def test_set_window_app_id_false_without_app_id(monkeypatch):
    # Empty id → nothing to stamp.
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    assert win32utils.set_window_app_id(0x10, app_id="") is False


def test_set_window_app_id_false_without_hwnd(monkeypatch):
    # A null handle → no window to stamp.
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    assert win32utils.set_window_app_id(0, app_id="test.app") is False


def test_set_window_app_id_bogus_hwnd_degrades_to_false(monkeypatch):
    # A non-zero but invalid HWND drives the REAL COM path (SHGetPropertyStoreForWindow):
    # it must report the failure as False and never raise, honouring the best-effort contract.
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    assert win32utils.set_window_app_id(0x1, app_id="test.app") is False


def test_set_window_app_id_relaunch_props_bogus_hwnd_never_raises(tmp_path, monkeypatch):
    # With script_path given, the call ALSO builds RelaunchCommand + RelaunchIconResource; a
    # bogus hwnd must still degrade to False without raising (proves the extra prop-building
    # path is safe even when the store can't be opened).
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    script = tmp_path / "claude_overlay.py"
    script.write_text("app")
    assert win32utils.set_window_app_id(0x1, app_id="test.app",
                                        script_path=str(script)) is False


def test_set_window_props_false_on_null_hwnd_or_empty_props():
    # The generalized writer rejects a null handle or an empty prop set before any COM.
    assert win32utils._set_window_props(0, {5: "x"}) is False
    assert win32utils._set_window_props(0x1, {}) is False


# ── set_window_icon: WM_SETICON fallback so the RUNNING button shows Clawd with NO shortcut ──
# (Independent of the Start Menu shortcut, so it covers a locked-down box where the shortcut
# builder is blocked. HICONs are cached, so re-asserts never leak GDI handles.)

def test_set_window_icon_false_without_taskbar_button(monkeypatch):
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", False)
    assert win32utils.set_window_icon(0x1) is False


def test_set_window_icon_false_without_hwnd(monkeypatch):
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    assert win32utils.set_window_icon(0) is False


def test_set_window_icon_false_when_icon_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    assert win32utils.set_window_icon(0x1, icon=str(tmp_path / "nope.ico")) is False


def test_set_window_icon_caches_handles_no_leak(monkeypatch):
    # With the real bundled .ico, the icon is loaded once and reused: a second call adds NO new
    # cache entry (hence no new GDI handle), honouring the v1.1.8 bounded-handle discipline. A
    # bogus hwnd is fine here — LoadImage reads the file; SendMessage just no-ops on the handle.
    monkeypatch.setattr(win32utils, "TASKBAR_BUTTON", True)
    win32utils._icon_handle_cache.clear()
    r1 = win32utils.set_window_icon(0x1)               # populates the cache
    snapshot = dict(win32utils._icon_handle_cache)
    r2 = win32utils.set_window_icon(0x1)               # must reuse, not grow
    assert isinstance(r1, bool) and isinstance(r2, bool)   # never raised
    assert win32utils._icon_handle_cache == snapshot       # no new handles loaded


# ── active-window capture helpers (SHOT_SCOPE="window") ─────────────────────────────

# clamp_bbox is pure math (no Win32), so it gets exact assertions.

def test_clamp_bbox_inside_virtual_screen_unchanged():
    assert win32utils.clamp_bbox((100, 100, 900, 700), (0, 0, 1920, 1080)) == (100, 100, 900, 700)


def test_clamp_bbox_clips_offscreen_overhang():
    # Window dragged half off the left edge and past the bottom → only the visible part.
    assert win32utils.clamp_bbox((-300, 900, 500, 1300), (0, 0, 1920, 1080)) == (0, 900, 500, 1080)


def test_clamp_bbox_degenerate_overlap_is_none():
    # Fully outside the virtual screen (e.g. stranded on an unplugged monitor) → nothing to grab.
    assert win32utils.clamp_bbox((5000, 100, 5800, 700), (0, 0, 1920, 1080)) is None


def test_clamp_bbox_sliver_is_none():
    # A <8px visible sliver isn't worth sending as a "screenshot of the window".
    assert win32utils.clamp_bbox((-795, 100, 5, 700), (0, 0, 1920, 1080)) is None


def test_clamp_bbox_none_rect_is_none():
    assert win32utils.clamp_bbox(None, (0, 0, 1920, 1080)) is None


def test_clamp_bbox_without_vbox_passes_rect_through():
    # virtual_screen_metrics() can return None (headless); the rect must survive unclipped.
    assert win32utils.clamp_bbox((10, 10, 400, 300), None) == (10, 10, 400, 300)


def test_clamp_bbox_negative_coords_legit_secondary():
    # A monitor left of the primary means legitimate negative coords — must NOT be clipped
    # when the virtual screen extends there.
    assert win32utils.clamp_bbox((-1800, 50, -600, 900),
                                 (-1920, 0, 3840, 1080)) == (-1800, 50, -600, 900)


# The hwnd-taking helpers call the real Win32 API; assertions stay at the contract level
# (type / no-raise), which holds even on a headless runner with no foreground window.

def test_window_capturable_rejects_null_and_bogus():
    assert win32utils.window_capturable(None) is False
    assert win32utils.window_capturable(0) is False
    assert win32utils.window_capturable(0xDEAD) is False   # not a real window


def test_foreground_capture_window_never_raises():
    hw = win32utils.foreground_capture_window()
    assert hw is None or int(hw) != 0
    if hw:
        # Whatever it returned must be a live, visible, non-minimized, non-own window.
        assert win32utils.window_capturable(hw)
        assert win32utils.window_is_own(hw) is False


def test_window_bbox_and_title_on_bogus_hwnd():
    # Must degrade to None / "" rather than raise.
    assert win32utils.window_bbox(0xDEAD) is None
    assert win32utils.window_title(0xDEAD) == ""


def test_window_is_own_unknown_defaults_true():
    # "Can't tell" must fail SAFE (treated as our own window → never captured).
    assert win32utils.window_is_own(None) is True
