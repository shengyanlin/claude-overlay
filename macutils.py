# -*- coding: utf-8 -*-
"""macOS counterpart of win32utils: the same names the UI calls, implemented on AppKit /
Quartz (pyobjc) instead of Win32.

The overlay's UI was written against a small set of Win32 calls — a window region, a
display affinity, a handful of z-order and foreground calls, monitor enumeration. Rather
than fork the 8,000-line UI, this module supplies that same surface for macOS:

  * the helper functions win32utils exports (enumerate_monitors, window_bbox,
    relaunch_overlay, run_overlay_update, ...), with the same signatures and results;
  * `_user32` / `_gdi32` objects that answer the few raw calls the UI makes directly
    (SetWindowRgn, CombineRgn, SetWindowDisplayAffinity, ...).

So claude_overlay.py imports from here on darwin and from win32utils on Windows, and its
window code runs unchanged on both. What each call maps to:

  window region (SetWindowRgn)        -> a CAShapeLayer mask on the window's content view
  WDA_EXCLUDEFROMCAPTURE              -> NSWindow.sharingType = none
  topmost + taskbar button            -> an accessory app whose window joins every Space
                                         (so it floats over full-screen apps), plus a
                                         menu-bar status item in place of the taskbar button
  an HWND                             -> the window's CGWindowID (NSWindow.windowNumber)
  RegisterHotKey / the keyboard lib   -> Carbon RegisterEventHotKey (needs no permission)

Imports config for the taskbar/app-id settings (one-way: macutils -> config)."""

import ctypes
import ctypes.util
import functools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import objc
import AppKit
import Quartz

from config import TASKBAR_BUTTON, APP_ID, APP_ICON   # noqa: F401  (APP_ID: parity with win32utils)
from screengeom import (clamp_bbox, compute_onscreen_move,   # noqa: F401  (re-exported)
                        _mon_rect, _visible_extent, _clamp_into)

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))   # this file lives in the app folder

# ── constants the UI names (values kept identical to Win32 so shared tests can assert them) ──
WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
GA_ROOT = 2
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
CF_TEXT, CF_BITMAP, CF_DIB, CF_UNICODETEXT, CF_HDROP, CF_DIBV5 = 1, 2, 8, 13, 15, 17
RGN_OR, RGN_COPY = 2, 5

# NSWindowCollectionBehavior: on every Space, doesn't slide with Spaces, allowed alongside
# another app's full-screen window. Together with the accessory activation policy this is
# what "always on top" has to mean on macOS — without it the overlay stays behind on the
# desktop Space the moment the user's app goes full screen.
_COLLECTION = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
               | AppKit.NSWindowCollectionBehaviorStationary
               | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)


# ───────────────────────────── the overlay's own NSWindow ─────────────────────────────
_app = None
_tk_window_cls = None
_known_before = None        # window numbers that existed before the overlay's Tk root
_root_window = None         # the overlay's NSWindow, once found
_key_patch_done = False


def _nsapp():
    """The application object — Tk's, or None before tkinter has been imported.

    Always reached through TKApplication, never NSApplication: whichever class first
    answers sharedApplication becomes THE application, and Tk aborts the process when it
    finds a plain NSApplication already in place. Asking Tk's own subclass is safe even
    before the first Tk root exists — it is the call Tk itself makes."""
    global _app
    if _app is None:
        try:
            _app = objc.lookUpClass("TKApplication").sharedApplication()
        except Exception:
            return None
    return _app


def _tk_class():
    """Tk's NSWindow subclass, or None before Tk has loaded."""
    global _tk_window_cls
    if _tk_window_cls is None:
        try:
            _tk_window_cls = objc.lookUpClass("TKWindow")
        except Exception:
            return None
    return _tk_window_cls


def _patch_key_window():
    """Let Tk's frameless window take the keyboard.

    A borderless NSWindow answers NO to canBecomeKeyWindow, so an overrideredirect Tk
    window can be clicked but not typed into — and the overlay is nothing but a text box.
    Tk's TKWindow inherits that answer, so override it once, for the class."""
    global _key_patch_done
    if _key_patch_done:
        return
    cls = _tk_class()
    if cls is None:
        return
    try:
        class TKWindow(objc.Category(cls)):    # noqa: F811  (a category, not a new class)
            def canBecomeKeyWindow(self):
                return True

            def canBecomeMainWindow(self):
                return True
        _key_patch_done = True
    except Exception:
        pass


def _tk_windows():
    cls = _tk_class()
    if cls is None:
        return []
    try:
        app = _nsapp()
        return [w for w in app.windows() if isinstance(w, cls)] if app is not None else []
    except Exception:
        return []


def note_windows_before_root():
    """Call just BEFORE creating the overlay's Tk root: remembers which Tk windows already
    exist, so the root's NSWindow can be told apart from any other Tk window in the
    process (the test suite builds more than one root)."""
    global _known_before, _root_window
    _root_window = None
    _known_before = {int(w.windowNumber()) for w in _tk_windows()}


# A rule for every AppKit call made from this module: it runs inside a Tk callback, so it
# must not make Tk deliver an event to Python synchronously — tkinter can't re-enter the
# interpreter from there, and the process dies ("PyEval_RestoreThread ... GIL released").
# The calls below only change window properties or queue events. NSWindow.orderOut_ is
# the known offender (Tk unmaps the window on the spot), which is why hiding is always
# left to Tk's own withdraw().
def _ns_window():
    """The overlay's NSWindow, or None while Tk hasn't created it yet."""
    global _root_window
    w = _root_window
    if w is not None:
        try:
            if int(w.windowNumber()) > 0:
                return w
        except Exception:
            pass
        _root_window = None
    wins = _tk_windows()
    if not wins:
        return None
    fresh = [x for x in wins if int(x.windowNumber()) not in (_known_before or ())]
    _root_window = max(fresh or wins, key=_overlay_likeness)
    _patch_key_window()
    return _root_window


def _overlay_likeness(w):
    """Sort key that puts the overlay's window last among Tk's. Started with no terminal
    (stdin on /dev/null: Finder, the .command launcher) Tk also builds a window for its
    console — never shown, a point wide — and it can come first in the list. Taking that
    one put the frameless style, the mask, always-on-top and the hidden-from-capture mark
    on a window nobody sees, and left the real one square, titled and capturable."""
    try:
        f = w.frame()
        return (int(w.windowNumber()) > 0, bool(w.isVisible()),
                float(f.size.width) * float(f.size.height))
    except Exception:
        return (False, False, 0.0)


def _shown(w):
    """Whether the window is on screen. Raising or focusing one that Tk has withdrawn would
    MAP it from AppKit's side — Tk hears of it on the spot and runs its <Map> bindings, the
    same fatal re-entry as orderOut_ in the other direction. Showing it is Tk's deiconify();
    these calls only ever reorder a window that is already there."""
    try:
        return bool(w.isVisible())
    except Exception:
        return False


def _window_number():
    w = _ns_window()
    try:
        return int(w.windowNumber()) if w is not None else 0
    except Exception:
        return 0


def configure_window(hwnd=None):
    """Make the overlay's window behave like the Windows one: no title bar, a transparent
    backing so the region mask can cut any shape out of it, present on every Space and
    beside full-screen apps. Idempotent and cheap — only writes what differs — so it is
    re-asserted on every show/restore path, like the taskbar-button style on Windows
    (Tk re-applies its own idea of the window style when it re-maps a window).
    Never raises; True when there was a window to configure."""
    try:
        app = _nsapp()
        if app is None:
            return False
        # Accessory, not regular: a regular app's window is confined to its own Space and
        # pulls the user out of a full-screen app when it activates.
        if app.activationPolicy() != AppKit.NSApplicationActivationPolicyAccessory:
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        w = _ns_window()
        if w is None:
            return False
        # Borderless at the AppKit level. Tk's overrideredirect hides the title bar but
        # keeps the titled style, whose theme frame paints an opaque rounded backdrop
        # behind the content — the masked-out corners would show white, not the desktop.
        if w.styleMask() != AppKit.NSWindowStyleMaskBorderless:
            frame = w.frame()
            w.setStyleMask_(AppKit.NSWindowStyleMaskBorderless)
            w.setFrame_display_(frame, True)
        if w.isOpaque():
            w.setOpaque_(False)
        w.setBackgroundColor_(AppKit.NSColor.clearColor())
        if w.collectionBehavior() != _COLLECTION:
            w.setCollectionBehavior_(_COLLECTION)
        if w.hidesOnDeactivate():
            w.setHidesOnDeactivate_(False)
        if not w.hasShadow():
            w.setHasShadow_(True)
        return True
    except Exception:
        return False


# ───────────────────────────── regions (the _gdi32 surface) ─────────────────────────────
class _Region:
    """What an HRGN is to the UI: an opaque handle it creates, combines, offsets and hands
    to SetWindowRgn. Here it is a list of shapes in Tk window coordinates (origin top-left,
    y down); the CGPath is only built when the region is applied."""

    __slots__ = ("shapes",)

    def __init__(self, shapes=None):
        self.shapes = list(shapes or [])    # ("rect"|"round"|"ellipse", x0, y0, x1, y1, r)

    def copy(self):
        return _Region(self.shapes)


class _Gdi32:
    """The region calls claude_overlay.py makes on gdi32, with the same arguments."""

    @staticmethod
    def CreateRectRgn(x0, y0, x1, y1):
        return _Region([("rect", x0, y0, x1, y1, 0)] if x1 > x0 and y1 > y0 else [])

    @staticmethod
    def CreateRoundRectRgn(x0, y0, x1, y1, w_ell, h_ell):
        # Win32 passes the ELLIPSE width/height (a diameter) and an exclusive right/bottom
        # edge, which is why the UI asks for (0, 0, w + 1, h + 1).
        return _Region([("round", x0, y0, x1 - 1, y1 - 1, max(w_ell, h_ell) / 2.0)])

    @staticmethod
    def CreateEllipticRgn(x0, y0, x1, y1):
        return _Region([("ellipse", x0, y0, x1 - 1, y1 - 1, 0)])

    @staticmethod
    def SetRectRgn(rgn, x0, y0, x1, y1):
        rgn.shapes = [("rect", x0, y0, x1, y1, 0)] if x1 > x0 and y1 > y0 else []
        return 1

    @staticmethod
    def CombineRgn(dst, a, b, mode):
        if mode == RGN_COPY:
            dst.shapes = list(a.shapes)
        elif mode == RGN_OR:
            dst.shapes = list(a.shapes) + list(b.shapes)   # a may BE dst: read before write
        else:
            return 0
        return 1

    @staticmethod
    def OffsetRgn(rgn, dx, dy):
        rgn.shapes = [(k, x0 + dx, y0 + dy, x1 + dx, y1 + dy, r)
                      for (k, x0, y0, x1, y1, r) in rgn.shapes]
        return 1

    @staticmethod
    def GetRgnBox(rgn, rect_ref):
        """The region's bounding box, written into the RECT passed with ctypes.byref()."""
        if not rgn.shapes:
            return 1                                    # NULLREGION
        box = getattr(rect_ref, "_obj", rect_ref)
        box.left = int(min(sh[1] for sh in rgn.shapes))
        box.top = int(min(sh[2] for sh in rgn.shapes))
        box.right = int(max(sh[3] for sh in rgn.shapes))
        box.bottom = int(max(sh[4] for sh in rgn.shapes))
        return 2 if len(rgn.shapes) == 1 else 3         # SIMPLEREGION / COMPLEXREGION

    @staticmethod
    def DeleteObject(obj):
        return 1                 # regions are plain Python objects; nothing to free


def _region_path(rgn, height):
    """A CGPath for the region, flipped into the layer's coordinates (origin bottom-left)."""
    path = Quartz.CGPathCreateMutable()
    for kind, x0, y0, x1, y1, r in rgn.shapes:
        rect = Quartz.CGRectMake(x0, height - y1, x1 - x0, y1 - y0)
        if kind == "rect":
            Quartz.CGPathAddRect(path, None, rect)
        elif kind == "ellipse":
            Quartz.CGPathAddEllipseInRect(path, None, rect)
        else:
            rad = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
            Quartz.CGPathAddRoundedRect(path, None, rect, rad, rad)
    return path


def _set_window_region(rgn):
    """Clip the overlay's window to a region: SetWindowRgn, as a layer mask."""
    w = _ns_window()
    if w is None or not isinstance(rgn, _Region):
        return 0
    configure_window()
    view = w.contentView()
    if view is None:
        return 0
    view.setWantsLayer_(True)
    layer = view.layer()
    if layer is None:
        return 0
    bounds = view.bounds()
    mask = Quartz.CAShapeLayer.layer()
    mask.setFrame_(bounds)
    mask.setPath_(_region_path(rgn, bounds.size.height))
    # No implicit animation: the collapsed pet swaps its outline several times a second,
    # and a cross-fade between two silhouettes would smear every frame.
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)
    layer.setMask_(mask)
    Quartz.CATransaction.commit()
    # Tk 8.6 draws in -drawRect: and nothing asks it to after the window changes size and
    # shape together: expanding from the orb left the window in place but blank until some
    # widget happened to repaint. (Tk 9 redraws by itself; asking again costs nothing.)
    view.setNeedsDisplay_(True)
    w.invalidateShadow()         # the shadow follows the visible shape, so recompute it
    return 1


def commit_window_changes():
    """Make window-property changes made a moment ago (hidden-from-capture, opacity) reach
    the window server NOW, for a screenshot that is about to be taken. AppKit otherwise
    batches them until the run loop next turns, and the grab would still see the old ones.

    Tk 9: flush the pending transaction. Tk 8.6 must not: there a flush draws whatever view
    is waiting to be redrawn, Tk runs its idle handlers inside that draw, and one of them
    calling back into Python — the chat's scrollbar update does — kills the process
    ("PyEval_RestoreThread ... the GIL is released"; tkinter can't be re-entered from inside
    a draw). Running the idle handlers first makes it rarer, not impossible. So on Tk 8.6
    the run loop is turned the way Tk itself turns it, with update(), which commits the
    same changes and can call Python safely."""
    try:
        import tkinter
        root, old_tk = tkinter._default_root, tkinter.TkVersion < 9
    except Exception:
        root, old_tk = None, False
    if not old_tk:
        _flush()
        return
    try:
        if root is not None:
            root.update()
    except Exception:
        pass


def _flush():
    Quartz.CATransaction.flush()


# ───────────────────────────── the _user32 surface ─────────────────────────────
class _User32:
    """The user32 calls claude_overlay.py makes directly, answered for macOS. An "hwnd" is
    a CGWindowID: the overlay's own window number, or another app's window from the
    window list — the two are the same kind of number, so they compare like HWNDs do."""

    @staticmethod
    def GetAncestor(hwnd, flag):
        return _window_number()

    @staticmethod
    def GetWindowLongW(hwnd, index):
        return 0

    @staticmethod
    def SetWindowLongW(hwnd, index, value):
        configure_window()       # the "make it an app window" moment: re-assert our style
        return 0

    @staticmethod
    def SetWindowRgn(hwnd, rgn, redraw):
        try:
            return _set_window_region(rgn)
        except Exception:
            return 0

    @staticmethod
    def SetWindowDisplayAffinity(hwnd, affinity):
        """WDA_EXCLUDEFROMCAPTURE -> NSWindowSharingNone: the window stays on screen and
        macOS is ASKED to leave it out of screenshots and screen shares. 1 means the request
        was made, not that it will be honoured — see verify_capture_exclusion()."""
        w = _ns_window()
        if w is None:
            return 0
        try:
            w.setSharingType_(AppKit.NSWindowSharingNone if affinity == WDA_EXCLUDEFROMCAPTURE
                              else AppKit.NSWindowSharingReadOnly)
            # Not committed here: it takes effect when the run loop next turns, which is
            # at once for a toggle. A caller about to take a screenshot can't wait for
            # that and calls commit_window_changes() itself.
            return 1
        except Exception:
            return 0

    @staticmethod
    def BringWindowToTop(hwnd):
        w = _ns_window()
        if w is None or not _shown(w):
            return 0
        try:
            configure_window()
            w.orderFrontRegardless()
            return 1
        except Exception:
            return 0

    @staticmethod
    def SetWindowPos(hwnd, after, x, y, cx, cy, flags):
        return _User32.BringWindowToTop(hwnd)

    @staticmethod
    def GetForegroundWindow():
        """The overlay's own window number while it has the keyboard, else the frontmost
        external window (or 0) — so `fg == hwnd` means "we are the foreground window"."""
        try:
            w = _ns_window()
            if w is not None and _nsapp().isActive() and w.isKeyWindow():
                return int(w.windowNumber())
        except Exception:
            pass
        return foreground_capture_window() or 0

    @staticmethod
    def GetWindowThreadProcessId(hwnd, pid_ref):
        return 0                 # no thread-input attachment on macOS; see SetForegroundWindow

    @staticmethod
    def AttachThreadInput(a, b, attach):
        return 0

    @staticmethod
    def SetForegroundWindow(hwnd):
        """Take the keyboard: activate this app and make the overlay its key window."""
        w = _ns_window()
        if w is None or not _shown(w):
            return 0
        try:
            configure_window()
            _nsapp().activateIgnoringOtherApps_(True)
            w.makeKeyAndOrderFront_(None)
            return 1
        except Exception:
            return 0

    @staticmethod
    def IsClipboardFormatAvailable(fmt):
        try:
            return 1 if _clipboard_has(fmt) else 0
        except Exception:
            return 0

    @staticmethod
    def GetSystemMetrics(index):
        box = virtual_screen_metrics() or (0, 0, 0, 0)
        return {SM_XVIRTUALSCREEN: box[0], SM_YVIRTUALSCREEN: box[1],
                SM_CXVIRTUALSCREEN: box[2], SM_CYVIRTUALSCREEN: box[3]}.get(index, 0)


_gdi32, _user32 = _Gdi32(), _User32()


# ───────────────────────────── process / app identity ─────────────────────────────
def set_dpi_awareness():
    """Nothing to do: Tk on macOS already works in points, and the OS scales for Retina."""


def set_app_user_model_id():
    """Before the first window: run as an accessory app (no Dock tile, allowed over other
    apps' full-screen windows) and give the process the Clawd icon for the dialogs macOS
    shows on its behalf. Never raises."""
    try:
        app = _nsapp()
        if app is None:
            return
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        img = _icon_image()
        if img is not None:
            app.setApplicationIconImage_(img)
    except Exception:
        pass


def _icon_path():
    """A PNG to show as the app's icon: the orb artwork beside this file."""
    for name in ("claude_overlay_2.png",):
        p = os.path.join(_REPO_DIR, name)
        if os.path.exists(p):
            return p
    return ""


def _icon_image():
    p = _icon_path()
    if not p:
        return None
    try:
        return AppKit.NSImage.alloc().initWithContentsOfFile_(p)
    except Exception:
        return None


def set_window_app_id(hwnd, app_id=APP_ID, script_path=None, icon=APP_ICON):
    """Windows stamps the taskbar identity onto the window here. The macOS equivalent of
    "make this frameless window a proper app window" is configure_window()."""
    return configure_window(hwnd)


def set_window_icon(hwnd, icon=APP_ICON):
    """No per-window icon on macOS (the status item carries it). Kept for the call site."""
    return False


def ensure_taskbar_shortcut(script_path, app_id=APP_ID, icon=APP_ICON, name="Claude Overlay"):
    """Windows self-heals a Start Menu shortcut here so a taskbar pin relaunches the app.
    On macOS the launcher is the app bundle setup.command builds; nothing to heal at
    run time."""
    return "skipped"


# ── the menu-bar status item: the taskbar button's stand-in ──
_status_item = None
_status_target = None


class _StatusTarget(AppKit.NSObject):
    """Receives the status item's click. A plain Python callable can't be a Cocoa target."""

    def initWithCallback_(self, callback):
        self = objc.super(_StatusTarget, self).init()
        if self is not None:
            self._callback = callback
        return self

    def clicked_(self, sender):
        try:
            self._callback()
        except Exception:
            pass


def install_status_item(callback, tooltip="Claude Overlay"):
    """Put the Clawd icon in the menu bar; clicking it calls `callback` (on the main
    thread, inside Tk's event loop — so the callback should only set a flag the UI pump
    reads, exactly like the global hotkey does). The frameless overlay has no Dock tile to
    click, and this is where macOS users look for a floating utility. Idempotent; never
    raises. Returns True when the item exists."""
    global _status_item, _status_target
    if _status_item is not None:
        return True
    try:
        bar = AppKit.NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(AppKit.NSSquareStatusItemLength)
        button = item.button()
        img = _icon_image()
        if img is not None:
            img.setSize_(AppKit.NSMakeSize(18, 18))
            button.setImage_(img)
        else:
            button.setTitle_("✻")
        button.setToolTip_(tooltip)
        _status_target = _StatusTarget.alloc().initWithCallback_(callback)
        button.setTarget_(_status_target)
        button.setAction_(objc.selector(_StatusTarget.clicked_, signature=b"v@:@"))
        _status_item = item
        return True
    except Exception:
        _status_item = None
        return False


def remove_status_item():
    global _status_item, _status_target
    try:
        if _status_item is not None:
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(_status_item)
    except Exception:
        pass
    _status_item = _status_target = None


# ───────────────────────────── restart / update / Codex setup ─────────────────────────────
def relaunch_overlay(script_path):
    """Start a FRESH overlay instance that OUTLIVES this process (its own session, stdio
    cut loose), so the app can restart itself after an update. Returns the child pid, or
    RAISES so the caller can choose not to quit when the relaunch didn't even start."""
    exe = sys.executable
    if not exe:
        raise RuntimeError("no interpreter to relaunch with")
    script_path = os.path.abspath(script_path)
    p = subprocess.Popen([exe, script_path], cwd=os.path.dirname(script_path) or None,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return p.pid


def can_self_update(repo=None):
    """True when update.command could actually update THIS install in place: the app
    folder is a git clone and git is on PATH. Never raises."""
    try:
        repo = repo or _REPO_DIR
        return (os.path.exists(os.path.join(repo, "update.command"))
                and os.path.exists(os.path.join(repo, ".git"))    # a worktree's .git is a FILE
                and shutil.which("git") is not None)
    except Exception:
        return False


def overlay_update_blocker(repo=None):
    """Why update.command would STOP on this clone instead of updating it, or None when it
    would go ahead: the same two checks the script makes before it pulls (on main, no
    tracked file modified). Never raises: a git that can't answer counts as blocked."""
    repo = repo or _REPO_DIR

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=15,
                              encoding="utf-8", errors="replace")
    try:
        r = git("rev-parse", "--abbrev-ref", "HEAD")
        branch = (r.stdout or "").strip()
        if r.returncode != 0 or not branch:
            return "git couldn't read this clone's branch"
        if branch != "main":
            return f'this clone is on branch "{branch}", not main'
        rc = git("diff", "--quiet", "HEAD").returncode
        if rc == 1:
            return "this clone has uncommitted changes"
        if rc != 0:
            return "git couldn't check this clone for local changes"
        return None
    except Exception as e:
        return f"git didn't answer ({type(e).__name__})"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True              # e.g. EPERM: it exists, we just can't signal it


def _run_in_terminal(script, args=(), env=None, cwd=None):
    """Run a shell script in its own Terminal window and wait for it. Returns its exit
    code, or None when it never ran / the window was closed before it finished.

    The scripts' output and their "press Return" prompts are the progress and error UI,
    as the console is for the .cmd files on Windows — and this app has no terminal of its
    own to show them in. `open` returns at once, so the exit code comes back through a
    status file; a pid file lets the wait end when the user closes the window instead."""
    tmp = tempfile.mkdtemp(prefix="claude_overlay_run_")
    status, pidfile = os.path.join(tmp, "status"), os.path.join(tmp, "pid")
    wrapper = os.path.join(tmp, "run.command")
    lines = ["#!/bin/bash", "echo $$ > %s" % shlex.quote(pidfile)]
    for k, v in (env or {}).items():
        lines.append("export %s=%s" % (k, shlex.quote(str(v))))
    lines.append("cd %s || exit 1" % shlex.quote(cwd or os.path.dirname(script)))
    lines.append("/bin/bash %s" % " ".join(shlex.quote(a) for a in (script, *args)))
    # Written under another name and renamed: the waiting thread takes the file existing
    # as "finished", and must never find it created but not yet written.
    lines += ["rc=$?", "echo $rc > %s && mv -f %s %s" % (
        shlex.quote(status + ".part"), shlex.quote(status + ".part"), shlex.quote(status)),
        "exit $rc"]
    try:
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(wrapper, 0o700)
        if subprocess.run(["/usr/bin/open", "-a", "Terminal", wrapper], capture_output=True,
                          timeout=30).returncode != 0:
            return None
        started = time.monotonic()
        while True:
            if os.path.exists(status):
                break
            if os.path.exists(pidfile):
                try:
                    pid = int(open(pidfile, encoding="utf-8").read().strip() or "0")
                except Exception:
                    pid = 0
                if pid and not _pid_alive(pid):
                    time.sleep(0.3)              # let a just-written status file land
                    if not os.path.exists(status):
                        return None              # window closed mid-run
                    break
            elif time.monotonic() - started > 60:
                return None                      # Terminal never started the script
            time.sleep(0.4)
        with open(status, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_overlay_update(repo=None):
    """Run the app folder's update.command IN ITS OWN TERMINAL WINDOW and wait for it.
    Returns (ok, message); ok only for exit code 0. Never raises. Call off the UI thread.

    A visible window for the same reason as on Windows: the script is the one sanctioned
    updater, and each of its failure paths ends on the fix, waiting for Return. No timeout
    either — a failing update legitimately waits for as long as the user takes to read it.
    OV_UPDATE_AUTO tells the script it was started by the app, so it skips the pause on
    SUCCESS only."""
    repo = repo or _REPO_DIR
    script = os.path.join(repo, "update.command")
    if not os.path.exists(script):
        return (False, "update.command is missing from the app folder")
    rc = _run_in_terminal(script, env={"OV_UPDATE_AUTO": "1"}, cwd=repo)
    if rc == 0:
        return (True, "")
    if rc is None:
        return (False, "couldn't run update.command (or its window was closed)")
    return (False, f"update.command stopped with exit code {rc} - its window says why")


def run_codex_setup(repo=None):
    """Run install-codex.sh --from-app IN ITS OWN TERMINAL WINDOW and wait for it — the
    overlay's "Set up Codex" button. Returns the script's exit code (0 installed and
    signed in, 2 installed but not signed in, 3 not installed), or None when it couldn't
    run at all. Never raises; call it off the UI thread."""
    repo = repo or _REPO_DIR
    script = os.path.join(repo, "install-codex.sh")
    if not os.path.exists(script):
        return None
    return _run_in_terminal(script, args=("--from-app",),
                            env={"OV_PYTHON": sys.executable or ""}, cwd=repo)


def _pooled(fn):
    """Give a function its own autorelease pool. The capture and paste helpers below run on
    Python threads (the type-ahead grab, the clipboard read), and a thread AppKit didn't
    start has no pool: every temporary Cocoa object they made — window lists, URLs, the
    pasteboard's data — leaked, a little on each keystroke. Everything they return is
    plain Python (ints, strings, a PIL image), so nothing outlives the pool. On the main
    thread it nests inside the run loop's and costs nothing."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with objc.autorelease_pool():
            return fn(*args, **kwargs)
    return run


# ───────────────────────────── monitors ─────────────────────────────
def _main_height():
    return Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.height


def _flip(frame, main_h):
    """An AppKit rect (origin bottom-left of the main screen, y up) as (l, t, r, b) in the
    global top-left coordinates Tk, CoreGraphics and screencapture all use."""
    x, y = frame.origin.x, frame.origin.y
    w, h = frame.size.width, frame.size.height
    return (int(round(x)), int(round(main_h - (y + h))),
            int(round(x + w)), int(round(main_h - y)))


@_pooled
def enumerate_monitors():
    """Return [{'rect': (l,t,r,b), 'work': (l,t,r,b), 'primary': bool}, ...], primary first.
    'rect' is the full display; 'work' excludes the menu bar and the Dock. In points, in
    the same global coordinates Tk uses for window geometry."""
    mons = []
    try:
        main_h = _main_height()
        main_id = int(Quartz.CGMainDisplayID())
        for s in AppKit.NSScreen.screens():
            try:
                num = int(s.deviceDescription()["NSScreenNumber"])
            except Exception:
                num = -1
            mons.append({"rect": _flip(s.frame(), main_h),
                         "work": _flip(s.visibleFrame(), main_h),
                         "primary": num == main_id})
    except Exception:
        pass
    if mons and not any(m["primary"] for m in mons):
        mons[0]["primary"] = True
    mons.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))  # primary, then L→R
    return mons


@_pooled
def virtual_screen_metrics():
    """(x, y, w, h) bounding box of the whole desktop (all displays combined), or None.
    CoreGraphics only — cheap and thread-safe — so it is usable as the per-poll
    display-topology signature: the box changes when a display is plugged or unplugged."""
    try:
        err, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
        if err or not n:
            return None
        l = t = r = b = None
        for d in ids[:n]:
            f = Quartz.CGDisplayBounds(d)
            x0, y0 = f.origin.x, f.origin.y
            x1, y1 = x0 + f.size.width, y0 + f.size.height
            l = x0 if l is None else min(l, x0)
            t = y0 if t is None else min(t, y0)
            r = x1 if r is None else max(r, x1)
            b = y1 if b is None else max(b, y1)
        return (int(l), int(t), int(r - l), int(b - t))
    except Exception:
        return None


# ───────────────────────────── screen-capture permission ─────────────────────────────
@_pooled
def screen_capture_allowed():
    """Whether macOS lets this process record the screen. Without it a capture still
    "succeeds" — but shows only the wallpaper and this app's own windows, which is worse
    than an error, so the UI asks this before trusting a screenshot. True when unknown."""
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def request_screen_capture():
    """Ask macOS to show its Screen Recording prompt (it does so once per app; after that
    the switch lives in System Settings > Privacy & Security). Never raises."""
    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:
        return False


# ───────────────────────────── leaving the overlay out of captures ─────────────────────────────
# Windows' WDA_EXCLUDEFROMCAPTURE is honoured by its capture APIs. Its macOS counterpart,
# NSWindowSharingNone, is not promised to be: Apple documents it as a legacy value that
# "can cause content to not be available in certain sharing situations" and says not to
# use it to keep content out of a capture.
# In practice current macOS honours it (checked on macOS 26 against both `screencapture`
# and ScreenCaptureKit), so the overlay still asks for it — but nothing here TRUSTS it
# until it has been seen working on this Mac, and the fallback (fading the window out for
# the length of a grab) depends on nothing but the window being invisible.
_capture_exclusion = None      # None: no verdict on this Mac yet; then True / False
_exclusion_tries = 0
_PROBE_SIZE = 16               # points


def capture_exclusion_verified():
    """True once verify_capture_exclusion() has seen a non-shareable window left out of a
    capture on this Mac."""
    return _capture_exclusion is True


def capture_exclusion_tested():
    """True once there is a verdict either way (so "not verified" means "failed", not
    "not asked yet")."""
    return _capture_exclusion is not None


def _marker_seen(img):
    """Is the probe window in this grab of its own rectangle? It is magenta on the left and
    green on the right — two saturated opposites side by side, which nothing that happens
    to be underneath will imitate. Judged by which channels dominate rather than by exact
    values, because a capture comes back in the display's colour space, not sRGB."""
    try:
        im = img.convert("RGB")
        w, h = im.size
        if w < 8 or h < 4:
            return False

        def frac(box, test):
            raw = im.crop(box).tobytes()                 # RGBRGB…
            px = [raw[i:i + 3] for i in range(0, len(raw), 3)]
            return sum(1 for p in px if test(p)) / max(1, len(px))

        left = (w // 8, h // 4, w // 2 - w // 8, h - h // 4)
        right = (w // 2 + w // 8, h // 4, w - w // 8, h - h // 4)
        magenta = frac(left, lambda p: min(p[0], p[2]) - p[1] > 80)
        green = frac(right, lambda p: p[1] - max(p[0], p[2]) > 80)
        return magenta > 0.8 and green > 0.8
    except Exception:
        return False


def _probe_exclusion(grab):
    """One round of the test: True (a non-shareable window stays out of a capture), False
    (it shows up), or None when the test itself didn't work — the marker wasn't in the
    control grab either, so its absence afterwards would prove nothing.

    It uses a 16-point window of its own, not the overlay: the overlay is never made
    shareable for a test (a live screen share would see it), never blinks, and what is
    underneath it — a video call, say — can't confuse the comparison."""
    win = None
    try:
        vf = AppKit.NSScreen.screens()[0].visibleFrame()       # the menu-bar screen
        rect = ((vf.origin.x + 2, vf.origin.y + vf.size.height - _PROBE_SIZE - 2),
                (_PROBE_SIZE, _PROBE_SIZE))
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setOpaque_(True)
        win.setHasShadow_(False)
        win.setIgnoresMouseEvents_(True)
        win.setLevel_(AppKit.NSStatusWindowLevel)               # nothing ordinary covers it
        win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                   | AppKit.NSWindowCollectionBehaviorStationary
                                   | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                   | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        view = win.contentView()
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(1.0, 0.0, 1.0, 1.0))
        half = Quartz.CALayer.layer()
        half.setFrame_(((_PROBE_SIZE / 2.0, 0), (_PROBE_SIZE / 2.0, _PROBE_SIZE)))
        half.setBackgroundColor_(Quartz.CGColorCreateGenericRGB(0.0, 1.0, 0.0, 1.0))
        view.layer().addSublayer_(half)
        bbox = _flip(win.frame(), _main_height())

        win.setSharingType_(AppKit.NSWindowSharingReadOnly)
        win.orderFrontRegardless()
        commit_window_changes()
        seen = False
        for _ in range(4):                  # the window server needs a frame or two
            time.sleep(0.05)
            if _marker_seen(grab(bbox)):
                seen = True
                break
        if not seen:
            return None
        win.setSharingType_(AppKit.NSWindowSharingNone)
        commit_window_changes()
        time.sleep(0.1)
        return not _marker_seen(grab(bbox))
    finally:
        try:
            if win is not None:
                win.orderOut_(None)
                win.close()
        except Exception:
            pass


def verify_capture_exclusion(grab):
    """Find out, once per run, whether NSWindowSharingNone really keeps a window of this
    app out of a capture on this Mac. `grab(bbox)` takes a screenshot of a rectangle the
    way the app does (PIL ImageGrab).

    Answers False without settling the question when it can't be asked yet: with no Screen
    Recording permission a capture is just the wallpaper, and a round whose control grab
    failed is retried on the next call (three at most, then the answer is No)."""
    global _capture_exclusion, _exclusion_tries
    if _capture_exclusion is not None:
        return _capture_exclusion
    if not screen_capture_allowed():
        return False
    _exclusion_tries += 1
    try:
        verdict = _probe_exclusion(grab)
    except Exception:
        verdict = None
    if verdict is None and _exclusion_tries < 3:
        return False
    _capture_exclusion = bool(verdict)
    return _capture_exclusion


def _server_alpha():
    """The overlay's opacity as the WINDOW SERVER has it (what a capture will composite),
    not as AppKit remembers asking for it. None when it can't be read."""
    info = _window_info(_window_number())
    try:
        return float(info.get(Quartz.kCGWindowAlpha)) if info else None
    except Exception:
        return None


def fade_out(timeout=0.4):
    """Make the overlay invisible without unmapping it (the keyboard focus stays where it
    is), and wait until the window server has applied that. Returns a function that puts
    the opacity back — or None when the fade could not be CONFIRMED, in which case the
    opacity is already back and the caller knows the window may still be in the picture."""
    w = _ns_window()
    if w is None:
        return None
    try:
        old = float(w.alphaValue())
    except Exception:
        return None

    def restore():
        try:
            w.setAlphaValue_(old)
            commit_window_changes()
        except Exception:
            pass

    try:
        w.setAlphaValue_(0.0)
        commit_window_changes()
        deadline = time.monotonic() + timeout
        while True:
            seen = _server_alpha()
            if seen is not None and seen < 0.01:
                return restore
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
    except Exception:
        pass
    restore()
    return None


# ───────────────────────────── active-window capture ─────────────────────────────
def _window_info(hwnd):
    """The window-server record for one window id, or None when it no longer exists."""
    try:
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, int(hwnd))
        return infos[0] if infos else None
    except Exception:
        return None


@_pooled
def window_is_own(hwnd):
    """True when the window belongs to THIS process (the overlay itself — including the
    collapsed orb): never a capture target. Can't tell → True, so it's never captured by
    mistake."""
    info = _window_info(hwnd) if hwnd else None
    if not info:
        return True
    try:
        return int(info.get(Quartz.kCGWindowOwnerPID, -1)) == os.getpid()
    except Exception:
        return True


@_pooled
def window_capturable(hwnd):
    """A window that still makes sense to screenshot: exists and is on screen (a
    minimized or other-Space window is not)."""
    info = _window_info(hwnd) if hwnd else None
    try:
        return bool(info and info.get(Quartz.kCGWindowIsOnscreen))
    except Exception:
        return False


@_pooled
def foreground_capture_window():
    """The frontmost app's front window as a capture target, or None when the foreground
    is unusable: this process (the user is typing in the overlay), or an app with no
    ordinary window on screen (the desktop). The caller falls back to the last tracked
    external window, then to full-screen capture."""
    try:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return None
        pid = int(front.processIdentifier())
        if pid == os.getpid():
            return None
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
        for info in infos or ():            # front to back
            if int(info.get(Quartz.kCGWindowOwnerPID, -1)) != pid:
                continue
            if int(info.get(Quartz.kCGWindowLayer, 1)) != 0:
                continue                    # menus, the Dock, status items: not a document
            b = info.get(Quartz.kCGWindowBounds) or {}
            if b.get("Width", 0) < 8 or b.get("Height", 0) < 8:
                continue
            if float(info.get(Quartz.kCGWindowAlpha, 1.0)) <= 0.0:
                continue
            return int(info[Quartz.kCGWindowNumber])
        return None
    except Exception:
        return None


@_pooled
def window_title(hwnd):
    """"App — Window title" ('' on failure) — labels the shot for the model. The title part
    needs Screen Recording permission; without it this is just the app's name."""
    info = _window_info(hwnd)
    if not info:
        return ""
    try:
        owner = str(info.get(Quartz.kCGWindowOwnerName) or "")
        name = str(info.get(Quartz.kCGWindowName) or "")
        if owner and name and name != owner:
            return f"{owner} — {name}"
        return name or owner
    except Exception:
        return ""


def window_in_view(hwnd):
    """win32utils' "can the user see it?" test, used by the hotkey toggle. Always False
    here, which keeps the macOS hotkey on its focus-only rule: hide while the overlay is
    the key window, otherwise bring it forward."""
    return False


@_pooled
def window_bbox(hwnd):
    """Screen-space (l,t,r,b) of a window, clipped to the desktop, in the points that
    ImageGrab.grab(bbox=...) takes. None when it can't be determined."""
    info = _window_info(hwnd)
    rect = None
    try:
        b = info.get(Quartz.kCGWindowBounds) if info else None
        if b:
            l, t = int(round(b["X"])), int(round(b["Y"]))
            rect = (l, t, l + int(round(b["Width"])), t + int(round(b["Height"])))
    except Exception:
        rect = None
    return clamp_bbox(rect, virtual_screen_metrics())


# ───────────────────────────── clipboard ─────────────────────────────
_IMAGE_TYPES = ("public.png", "public.tiff", "public.jpeg")


def _pasteboard():
    """The system clipboard. One function, so the tests can hand the helpers a private
    pasteboard instead of overwriting whatever the developer last copied."""
    return AppKit.NSPasteboard.generalPasteboard()


def _pasteboard_file_paths(pb):
    try:
        urls = pb.readObjectsForClasses_options_([AppKit.NSURL], None)
        return [str(u.path()) for u in (urls or []) if u.isFileURL() and u.path()]
    except Exception:
        return []


@_pooled
def _clipboard_has(fmt):
    """IsClipboardFormatAvailable, for the formats the paste probe asks about. One macOS
    difference is folded in here: copying a FILE in Finder also puts its name on the
    pasteboard as text, and the probe's rule is "text wins" — so text is only reported
    when no file is there, or a copied screenshot file would paste as its filename."""
    pb = _pasteboard()
    types = set(pb.types() or [])
    has_files = bool(_pasteboard_file_paths(pb))
    if fmt in (CF_TEXT, CF_UNICODETEXT):
        return (not has_files) and "public.utf8-plain-text" in types
    if fmt == CF_HDROP:
        return has_files
    if fmt in (CF_BITMAP, CF_DIB, CF_DIBV5):
        return any(t in types for t in _IMAGE_TYPES)
    return False


@_pooled
def grab_clipboard():
    """What PIL's ImageGrab.grabclipboard() returns on Windows: a PIL image for copied
    pixels, a list of paths for copied files, else None. (PIL's own macOS version shells
    out to osascript and only understands PNG data.) Safe off the UI thread."""
    import io
    from PIL import Image
    pb = _pasteboard()
    paths = _pasteboard_file_paths(pb)
    if paths:
        return paths
    for t in _IMAGE_TYPES:
        data = pb.dataForType_(t)
        if data is not None and len(data):
            img = Image.open(io.BytesIO(bytes(data)))
            img.load()
            return img
    return None


# ───────────────────────────── global hotkey ─────────────────────────────
_CARBON_MODS = {"cmd": 0x0100, "command": 0x0100, "win": 0x0100, "windows": 0x0100,
                "shift": 0x0200, "alt": 0x0800, "option": 0x0800, "opt": 0x0800,
                "ctrl": 0x1000, "control": 0x1000}
# ANSI virtual key codes (HIToolbox/Events.h) for the keys a hotkey string can name.
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "`": 50, "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51,
    "delete": 51, "esc": 53, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "left": 123, "right": 124, "down": 125, "up": 126,
}


def parse_hotkey(spec):
    """"ctrl+alt+space" (the `keyboard` library's syntax, which HOTKEY is written in) ->
    (key code, Carbon modifier mask). Raises ValueError on anything it can't map."""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods, key = 0, None
    for p in parts:
        if p in _CARBON_MODS:
            mods |= _CARBON_MODS[p]
        elif p in _KEYCODES and key is None:
            key = _KEYCODES[p]
        else:
            raise ValueError(f"unknown key {p!r} in hotkey {spec!r}")
    if key is None:
        raise ValueError(f"hotkey {spec!r} names no key")
    return key, mods


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HOTKEY_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p)


class _HotKeys:
    """The two calls the UI makes on the `keyboard` module — add_hotkey and unhook_all —
    on Carbon's RegisterEventHotKey. That API is old but is still what every macOS app
    with a global shortcut uses, because it needs neither Accessibility nor Input
    Monitoring permission (an event tap, which `keyboard` uses on macOS, needs root).

    The callback runs on the main thread, inside Tk's event loop, when the key is pressed
    — so, like the Windows hook, it must only set a flag for the UI pump to act on."""

    def __init__(self):
        self._carbon = None
        self._refs = []            # EventHotKeyRefs, to unregister
        self._callbacks = {}       # hotkey id -> callable
        self._handler = None       # the ctypes thunk; must outlive the registration
        self._handler_ref = None
        self._next_id = 1

    def _lib(self):
        if self._carbon is None:
            c = ctypes.CDLL(ctypes.util.find_library("Carbon")
                            or "/System/Library/Frameworks/Carbon.framework/Carbon")
            c.GetApplicationEventTarget.restype = ctypes.c_void_p
            c.InstallEventHandler.restype = ctypes.c_int32
            c.InstallEventHandler.argtypes = [
                ctypes.c_void_p, _HOTKEY_HANDLER, ctypes.c_uint32,
                ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p)]
            c.RemoveEventHandler.argtypes = [ctypes.c_void_p]
            c.RegisterEventHotKey.restype = ctypes.c_int32
            c.RegisterEventHotKey.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
            c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
            c.GetEventParameter.restype = ctypes.c_int32
            c.GetEventParameter.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
            self._carbon = c
        return self._carbon

    def _on_event(self, call_ref, event, user_data):
        try:
            hk = _EventHotKeyID()
            # kEventParamDirectObject '----', typeEventHotKeyID 'hkid'
            if self._carbon.GetEventParameter(event, 0x2D2D2D2D, 0x686B6964, None,
                                              ctypes.sizeof(hk), None,
                                              ctypes.byref(hk)) == 0:
                cb = self._callbacks.get(hk.id)
                if cb:
                    cb()
        except Exception:
            pass
        return 0

    def add_hotkey(self, spec, callback):
        key, mods = parse_hotkey(spec)
        c = self._lib()
        target = c.GetApplicationEventTarget()
        if self._handler is None:
            self._handler = _HOTKEY_HANDLER(self._on_event)
            spec_ = _EventTypeSpec(0x6B657962, 5)     # 'keyb', kEventHotKeyPressed
            ref = ctypes.c_void_p()
            if c.InstallEventHandler(target, self._handler, 1, ctypes.byref(spec_), None,
                                     ctypes.byref(ref)) != 0:
                self._handler = None
                raise OSError("couldn't install the hotkey handler")
            self._handler_ref = ref
        hk_id = self._next_id
        self._next_id += 1
        ref = ctypes.c_void_p()
        err = c.RegisterEventHotKey(key, mods, _EventHotKeyID(0x434C4F56, hk_id),  # 'CLOV'
                                    target, 0, ctypes.byref(ref))
        if err != 0:
            raise OSError(f"macOS refused the hotkey {spec!r} (error {err}) - another app "
                          "may already own it")
        self._refs.append(ref)
        self._callbacks[hk_id] = callback

    def unhook_all(self):
        c = self._carbon
        if c is None:
            return
        for ref in self._refs:
            try:
                c.UnregisterEventHotKey(ref)
            except Exception:
                pass
        self._refs, self._callbacks = [], {}
        if self._handler_ref is not None:
            try:
                c.RemoveEventHandler(self._handler_ref)
            except Exception:
                pass
        self._handler = self._handler_ref = None


hotkeys = _HotKeys()
