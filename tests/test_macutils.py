"""Tests for macutils — the macOS platform layer (the counterpart of test_win32utils.py).

They call the real AppKit / Quartz APIs, so the module is skipped off macOS. Nothing here
opens a window, touches the user's clipboard, or starts Terminal: the window-dependent
half of macutils is exercised by the UI suite, which builds a real Overlay on it."""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only AppKit helpers")

if sys.platform == "darwin":
    import AppKit
    import macutils


# ── the constants the UI names keep their Win32 values ──────────────────────────────────
def test_constants_match_win32utils():
    assert macutils.WDA_NONE == 0x00 and macutils.WDA_EXCLUDEFROMCAPTURE == 0x11
    assert macutils.GWL_EXSTYLE == -20
    assert macutils.WS_EX_APPWINDOW == 0x00040000 and macutils.WS_EX_TOOLWINDOW == 0x00000080
    assert macutils.CF_UNICODETEXT == 13 and macutils.CF_TEXT == 1


def test_identity_calls_never_raise_before_any_window():
    macutils.set_dpi_awareness()
    macutils.set_app_user_model_id()
    assert macutils.set_window_icon(0) is False
    assert macutils.ensure_taskbar_shortcut(__file__) == "skipped"


# ── regions ─────────────────────────────────────────────────────────────────────────────
def _box(rgn):
    box = wt.RECT()
    kind = macutils._gdi32.GetRgnBox(rgn, ctypes.byref(box))
    return kind, (box.left, box.top, box.right, box.bottom)


def test_region_combine_offset_and_box():
    g = macutils._gdi32
    full, tmp = g.CreateRectRgn(0, 0, 0, 0), g.CreateRectRgn(0, 0, 0, 0)
    assert full and tmp                                   # an EMPTY region is still a handle
    assert _box(full)[0] == 1                             # NULLREGION
    g.SetRectRgn(tmp, 2, 3, 10, 4)
    g.CombineRgn(full, full, tmp, macutils.RGN_OR)        # dst is also a source
    g.SetRectRgn(tmp, 0, 4, 5, 5)
    g.CombineRgn(full, full, tmp, macutils.RGN_OR)
    assert _box(full) == (3, (0, 3, 10, 5))               # COMPLEXREGION, the union's box

    copy = g.CreateRectRgn(0, 0, 0, 0)
    g.CombineRgn(copy, full, None, macutils.RGN_COPY)
    g.OffsetRgn(copy, 7, 1)
    assert _box(copy)[1] == (7, 4, 17, 6)
    assert _box(full)[1] == (0, 3, 10, 5)                 # the master is untouched
    assert g.DeleteObject(copy) == 1


def test_round_and_elliptic_regions_use_win32s_exclusive_edge():
    g = macutils._gdi32
    # The UI asks for (0, 0, w + 1, h + 1): Win32's right/bottom edge is exclusive.
    assert _box(g.CreateRoundRectRgn(0, 0, 421, 621, 18, 18))[1] == (0, 0, 420, 620)
    assert _box(g.CreateEllipticRgn(0, 0, 57, 57))[1] == (0, 0, 56, 56)
    # ...and the last two arguments are the corner ELLIPSE's size, i.e. a diameter.
    assert g.CreateRoundRectRgn(0, 0, 421, 621, 18, 18).shapes[0][5] == 9


def test_region_path_is_flipped_into_layer_coordinates():
    import Quartz
    rgn = macutils._gdi32.CreateRectRgn(0, 0, 10, 4)      # the TOP 4 px of a 20-px window
    r = Quartz.CGPathGetBoundingBox(macutils._region_path(rgn, 20))
    assert (r.origin.x, r.origin.y, r.size.width, r.size.height) == (0, 16, 10, 4)


# ── monitors ────────────────────────────────────────────────────────────────────────────
def test_enumerate_monitors_shape():
    mons = macutils.enumerate_monitors()
    assert isinstance(mons, list)
    for m in mons:
        assert set(m) == {"rect", "work", "primary"}
        l, t, r, b = m["rect"]
        assert r > l and b > t
        wl, wt_, wr, wb = m["work"]
        assert l <= wl < wr <= r and t <= wt_ < wb <= b    # the work area is inside it
    if mons:
        assert mons[0]["primary"] and sum(m["primary"] for m in mons) == 1
        assert mons[0]["rect"][:2] == (0, 0)               # global coordinates start there


def test_virtual_screen_metrics_covers_every_monitor():
    box = macutils.virtual_screen_metrics()
    mons = macutils.enumerate_monitors()
    if box is None or not mons:
        pytest.skip("no display attached")
    x, y, w, h = box
    for m in mons:
        l, t, r, b = m["rect"]
        assert x <= l and y <= t and r <= x + w and b <= y + h
    g = macutils._user32.GetSystemMetrics
    assert (g(macutils.SM_XVIRTUALSCREEN), g(macutils.SM_CXVIRTUALSCREEN)) == (x, w)


def test_window_helpers_refuse_a_window_that_does_not_exist():
    gone = 0x7FFFFFF0
    assert macutils.window_is_own(gone) is True            # can't tell -> never a target
    assert macutils.window_capturable(gone) is False
    assert macutils.window_title(gone) == ""
    assert macutils.window_bbox(gone) is None
    fg = macutils.foreground_capture_window()
    assert fg is None or (isinstance(fg, int) and not macutils.window_is_own(fg))


# ── clipboard (on a private pasteboard: the user's clipboard is never touched) ──────────
@pytest.fixture
def pasteboard(monkeypatch):
    pb = AppKit.NSPasteboard.pasteboardWithUniqueName()
    monkeypatch.setattr(macutils, "_pasteboard", lambda: pb)
    yield pb
    pb.releaseGlobally()


def _has(*fmts):
    return [bool(macutils._user32.IsClipboardFormatAvailable(f)) for f in fmts]


def test_copied_text_is_text(pasteboard):
    pasteboard.clearContents()
    pasteboard.setString_forType_("hello", "public.utf8-plain-text")
    assert _has(macutils.CF_UNICODETEXT, macutils.CF_DIB, macutils.CF_HDROP) == [True, False, False]
    assert macutils.grab_clipboard() is None


def test_copied_pixels_come_back_as_an_image(pasteboard):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "PNG")
    data = buf.getvalue()
    pasteboard.clearContents()
    pasteboard.setData_forType_(AppKit.NSData.dataWithBytes_length_(data, len(data)), "public.png")
    assert _has(macutils.CF_UNICODETEXT, macutils.CF_DIB) == [False, True]
    img = macutils.grab_clipboard()
    assert isinstance(img, Image.Image) and img.size == (40, 30)


def test_a_copied_file_is_a_file_not_its_name(pasteboard, tmp_path):
    # Finder puts the file's NAME on the pasteboard as text beside the file itself. The
    # paste probe's rule is "text wins", so that text must not count — or copying a
    # screenshot file in Finder would paste its filename instead of attaching it.
    f = tmp_path / "shot one.png"
    f.write_bytes(b"x")
    pasteboard.clearContents()
    pasteboard.writeObjects_([AppKit.NSURL.fileURLWithPath_(str(f))])
    pasteboard.addTypes_owner_(["public.utf8-plain-text"], None)
    pasteboard.setString_forType_("shot one.png", "public.utf8-plain-text")
    assert _has(macutils.CF_UNICODETEXT, macutils.CF_HDROP) == [False, True]
    assert [os.path.realpath(p) for p in macutils.grab_clipboard()] == [os.path.realpath(str(f))]


# ── the global hotkey's string ──────────────────────────────────────────────────────────
def test_parse_hotkey_reads_the_keyboard_librarys_syntax():
    assert macutils.parse_hotkey("alt+shift+space") == (49, 0x0800 | 0x0200)
    assert macutils.parse_hotkey("Ctrl + Alt + Space") == (49, 0x1000 | 0x0800)
    assert macutils.parse_hotkey("cmd+shift+k") == (40, 0x0100 | 0x0200)
    assert macutils.parse_hotkey("f8") == (100, 0)


@pytest.mark.parametrize("bad", ["", "ctrl+alt", "ctrl+nosuchkey", "a+b", None])
def test_parse_hotkey_rejects_what_it_cannot_map(bad):
    with pytest.raises(ValueError):
        macutils.parse_hotkey(bad)


# ── update / Codex setup: the Terminal hand-off ─────────────────────────────────────────
def test_the_scripts_macutils_runs_are_the_ones_named_in_config():
    import config
    assert config.UPDATE_SCRIPT == "update.command"
    assert config.CODEX_SETUP_SCRIPT == "install-codex.sh"


def test_missing_scripts_fail_cleanly(tmp_path):
    ok, msg = macutils.run_overlay_update(str(tmp_path))
    assert ok is False and "update.command" in msg
    assert macutils.run_codex_setup(str(tmp_path)) is None


@pytest.fixture
def fake_terminal(monkeypatch):
    """`open -a Terminal <wrapper>` without Terminal: run the wrapper right here, the way
    Terminal would, so everything else in _run_in_terminal — the wrapper it writes, the
    status file, the exit code — is the real thing."""
    real_run = subprocess.run
    seen = {}

    def run(args, **kw):
        if isinstance(args, list) and args[:3] == ["/usr/bin/open", "-a", "Terminal"]:
            seen["wrapper"] = open(args[3], encoding="utf-8").read()
            real_run(["/bin/bash", args[3]], stdin=subprocess.DEVNULL, capture_output=True)
            return subprocess.CompletedProcess(args, seen.get("open_rc", 0))
        return real_run(args, **kw)
    monkeypatch.setattr(macutils.subprocess, "run", run)
    return seen


def test_run_overlay_update_maps_the_exit_code(tmp_path, fake_terminal):
    # A folder with a space, quotes and an ampersand in its name: the wrapper must quote it.
    app = tmp_path / "Tools & \"Apps\" (x)"
    app.mkdir()
    script = app / "update.command"
    # exit 0 only when the app's own flag is set AND it runs in its folder; record what it saw.
    script.write_text('echo "$OV_UPDATE_AUTO|$PWD|$PATH" > seen.txt\n'
                      '[ "$OV_UPDATE_AUTO" = "1" ] && exit 0\nexit 5\n')
    assert macutils.run_overlay_update(str(app)) == (True, "")
    flag, cwd, path = (app / "seen.txt").read_text().strip().split("|")
    assert flag == "1" and os.path.realpath(cwd) == os.path.realpath(str(app))
    assert path                                            # inherited, not a bare 1-var env

    script.write_text("exit 7\n")
    ok, msg = macutils.run_overlay_update(str(app))
    assert ok is False and "7" in msg


def test_the_exit_code_file_appears_whole_or_not_at_all(tmp_path, fake_terminal):
    """The waiting thread reads `status` the moment it exists. Written in place, it exists —
    empty — for an instant before the exit code is in it, and a finished update would be
    reported as "never ran". So it is written under another name and renamed."""
    (tmp_path / "update.command").write_text("exit 3\n")
    ok, msg = macutils.run_overlay_update(str(tmp_path))
    assert ok is False and "3" in msg
    last = [l for l in fake_terminal["wrapper"].splitlines() if "status" in l][-1]
    part, _, rest = last.partition(" && ")
    assert part.startswith("echo $rc > ") and part.endswith("status.part")
    assert rest.startswith("mv -f ") and rest.endswith("status")


def test_run_codex_setup_passes_the_mode_and_the_python(tmp_path, fake_terminal, monkeypatch):
    (tmp_path / "install-codex.sh").write_text(
        'echo "$1|$OV_PYTHON" > seen.txt\n[ "$1" = "--from-app" ] && exit 2\nexit 9\n')
    monkeypatch.setattr(macutils.sys, "executable", "/some where/python")
    assert macutils.run_codex_setup(str(tmp_path)) == 2
    assert (tmp_path / "seen.txt").read_text().strip() == "--from-app|/some where/python"


def test_a_terminal_that_will_not_open_is_reported_not_raised(tmp_path, fake_terminal, monkeypatch):
    (tmp_path / "update.command").write_text("exit 0\n")
    (tmp_path / "install-codex.sh").write_text("exit 0\n")

    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(macutils.subprocess, "run", boom)
    ok, msg = macutils.run_overlay_update(str(tmp_path))
    assert ok is False and "update.command" in msg
    assert macutils.run_codex_setup(str(tmp_path)) is None


def test_relaunch_starts_a_detached_child(tmp_path, monkeypatch):
    seen = {}

    class P:
        pid = 4242

    def popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return P()
    monkeypatch.setattr(macutils.subprocess, "Popen", popen)
    script = tmp_path / "claude_overlay.py"
    assert macutils.relaunch_overlay(str(script)) == 4242
    assert seen["args"] == [sys.executable, str(script)]
    assert seen["kw"]["start_new_session"] is True         # outlives this process
    monkeypatch.setattr(macutils.sys, "executable", "")
    with pytest.raises(RuntimeError):
        macutils.relaunch_overlay(str(script))


# ── which Tk window is the overlay ──

class _FakeNSWindow:
    def __init__(self, number, visible, w, h):
        self._n, self._v, self._w, self._h = number, visible, w, h

    def windowNumber(self):
        return self._n

    def isVisible(self):
        return self._v

    def frame(self):
        from types import SimpleNamespace as NS
        return NS(size=NS(width=self._w, height=self._h))


def test_tks_hidden_console_window_is_never_taken_for_the_overlay(monkeypatch):
    """Started with stdin on /dev/null (Finder, the .command launcher) Tk builds a second
    window, for its console: never shown, a point wide, and it can be first in the list.
    Every window property the overlay sets used to land on it instead."""
    console = _FakeNSWindow(-1, False, 1, 29)
    overlay = _FakeNSWindow(4711, True, 420, 620)
    monkeypatch.setattr(macutils, "_root_window", None)
    monkeypatch.setattr(macutils, "_known_before", set())
    monkeypatch.setattr(macutils, "_patch_key_window", lambda: None)
    for order in ([console, overlay], [overlay, console]):
        monkeypatch.setattr(macutils, "_root_window", None)
        monkeypatch.setattr(macutils, "_tk_windows", lambda order=order: order)
        assert macutils._ns_window() is overlay


def test_the_overlay_is_found_while_it_is_withdrawn_too(monkeypatch):
    # Hidden by the hotkey, the overlay isn't visible either: size still tells them apart.
    console = _FakeNSWindow(-1, False, 1, 29)
    overlay = _FakeNSWindow(4711, False, 420, 620)
    monkeypatch.setattr(macutils, "_root_window", None)
    monkeypatch.setattr(macutils, "_known_before", set())
    monkeypatch.setattr(macutils, "_patch_key_window", lambda: None)
    monkeypatch.setattr(macutils, "_tk_windows", lambda: [console, overlay])
    assert macutils._ns_window() is overlay


def test_a_window_that_cannot_be_measured_ranks_last():
    class Broken:
        def windowNumber(self):
            raise RuntimeError("gone")
    assert macutils._overlay_likeness(Broken()) < macutils._overlay_likeness(
        _FakeNSWindow(1, False, 1, 1))


# ── committing window changes ──

class _Root:
    def __init__(self, order):
        self.order = order

    def update(self):
        self.order.append("update")


def test_tk_9_commits_with_a_flush(monkeypatch):
    import tkinter
    order = []
    monkeypatch.setattr(tkinter, "_default_root", _Root(order), raising=False)
    monkeypatch.setattr(tkinter, "TkVersion", 9.0)
    monkeypatch.setattr(macutils, "_flush", lambda: order.append("flush"))
    macutils.commit_window_changes()
    assert order == ["flush"]


def test_tk_86_never_flushes_it_turns_the_run_loop_instead(monkeypatch):
    """On Tk 8.6 a flush draws, Tk runs idle handlers inside the draw, and a Python callback
    there kills the process. update() commits the same changes from a safe place."""
    import tkinter
    order = []
    monkeypatch.setattr(tkinter, "_default_root", _Root(order), raising=False)
    monkeypatch.setattr(tkinter, "TkVersion", 8.6)
    monkeypatch.setattr(macutils, "_flush", lambda: order.append("flush"))
    macutils.commit_window_changes()
    assert order == ["update"]
    monkeypatch.setattr(tkinter, "_default_root", None, raising=False)
    macutils.commit_window_changes()                    # no root: nothing, and no flush
    assert order == ["update"]


def test_marking_the_window_hidden_does_not_commit_by_itself(monkeypatch):
    """A toggle is committed by the run loop a moment later; only a caller about to take a
    screenshot commits, so the risky call is made in as few places as possible."""
    committed = []

    class Win:
        def setSharingType_(self, t):
            committed.append(("sharing", t))

    monkeypatch.setattr(macutils, "_ns_window", lambda: Win())
    monkeypatch.setattr(macutils, "commit_window_changes", lambda: committed.append("commit"))
    assert macutils._user32.SetWindowDisplayAffinity(1, macutils.WDA_EXCLUDEFROMCAPTURE) == 1
    assert committed == [("sharing", macutils.AppKit.NSWindowSharingNone)]
