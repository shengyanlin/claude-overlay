# -*- coding: utf-8 -*-
"""Windows-only Win32/ctypes helpers: DPI awareness, AppUserModelID, window-region
and display-affinity calls, and multi-monitor enumeration. Imports config for the
taskbar/app-id settings (one-way: win32utils -> config)."""

import ctypes
import ctypes.wintypes as wt

from config import TASKBAR_BUTTON, APP_ID

def set_dpi_awareness():
    """Make the process DPI-aware so 1 Tk pixel == 1 physical pixel (crisp, no
    OS bitmap-stretch). Must run before the Tk interpreter starts."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER-MONITOR aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def set_app_user_model_id():
    """Give the process an explicit AppUserModelID so the Windows taskbar shows OUR
    window icon (APP_ICON) rather than pythonw's, and doesn't lump the overlay together
    with other Python apps. Must run before the first window is created."""
    if not (TASKBAR_BUTTON and APP_ID):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

# Win32 region calls — set argtypes so 64-bit handles aren't truncated.
_gdi32, _user32 = ctypes.windll.gdi32, ctypes.windll.user32
_gdi32.CreateRoundRectRgn.restype = wt.HRGN
_gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_user32.SetWindowRgn.restype = ctypes.c_int
_user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, ctypes.c_bool]
_gdi32.DeleteObject.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]   # free a region Windows didn't take ownership of
_user32.GetAncestor.restype = wt.HWND
_user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_gdi32.CreateEllipticRgn.restype = wt.HRGN
_gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
# Region from arbitrary silhouette (used to float the collapsed orb as a pixel sprite,
# with no circular frame): OR together one rect per opaque run of the artwork's alpha.
_gdi32.CreateRectRgn.restype = wt.HRGN
_gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
_gdi32.SetRectRgn.restype = ctypes.c_int
_gdi32.SetRectRgn.argtypes = [wt.HRGN] + [ctypes.c_int] * 4
_gdi32.CombineRgn.restype = ctypes.c_int
_gdi32.CombineRgn.argtypes = [wt.HRGN, wt.HRGN, wt.HRGN, ctypes.c_int]
_user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.GetMonitorInfoW.restype = ctypes.c_int
_MONENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(wt.RECT), ctypes.c_void_p)
_user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _MONENUMPROC, ctypes.c_void_p]
_user32.EnumDisplayMonitors.restype = ctypes.c_int
_user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, ctypes.c_uint]
_user32.SetWindowDisplayAffinity.restype = ctypes.c_int
_user32.GetForegroundWindow.restype = wt.HWND
# Extended window-style get/set — used to force a taskbar button onto the frameless
# (overrideredirect) window. argtypes set so the 64-bit HWND isn't truncated.
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
GWL_EXSTYLE      = -20
WS_EX_TOOLWINDOW = 0x00000080   # no taskbar button (what an overrideredirect popup effectively is)
WS_EX_APPWINDOW  = 0x00040000   # force a taskbar button even on a tool/popup window
# Z-order raise + forced activation — used when the taskbar button / alt-tab / hotkey asks for
# the overlay: the OS activates it but does NOT re-order it above other always-on-top windows,
# so a click could leave it buried under another topmost window (or just unfocused). These do
# the raise the activation skips. Plain z-order, no SetWindowRgn → clear of the v1.1.9 freeze class.
HWND_TOPMOST     = wt.HWND(-1)
SWP_NOSIZE       = 0x0001
SWP_NOMOVE       = 0x0002
SWP_NOACTIVATE   = 0x0010
SWP_SHOWWINDOW   = 0x0040
_user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.SetWindowPos.restype = wt.BOOL
_user32.BringWindowToTop.argtypes = [wt.HWND]
_user32.BringWindowToTop.restype = wt.BOOL
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
_user32.AttachThreadInput.restype = wt.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
_user32.IsClipboardFormatAvailable.restype = ctypes.c_int
# Virtual-desktop bounding box — used as a cheap display-topology change signature (the box
# changes when a monitor is plugged/unplugged or a resolution changes), so we can detect that
# the frameless (overrideredirect) window may have been stranded off-screen and pull it back.
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN   = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
# Standard clipboard format ids — used for a cheap, non-blocking "is there an image?" probe
# on the UI thread, so we only spin up the (potentially slow) ImageGrab.grabclipboard() read
# on a background thread when there's actually image/file content.
CF_BITMAP, CF_DIB, CF_HDROP, CF_DIBV5 = 2, 8, 15, 17
CF_TEXT, CF_UNICODETEXT = 1, 13   # so a text copy (which many apps ALSO put a bitmap on the
                                  # clipboard for) pastes as text, not as an image

# Exclude the overlay from screen captures at the OS level (DWM): the window stays
# visible to the user but is omitted from PIL ImageGrab / PrintWindow, so the
# screenshots we send Claude never contain the overlay obscuring the content — and
# we no longer have to withdraw() + sleep() on every capture. Verified on this
# machine (returns the content behind the window, not black).
# WDA_NONE clears the affinity again → the window becomes capturable (shows up in
# Teams/Zoom/OBS screen shares); the "shareable" status-bar toggle flips between them.
WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11

class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

def enumerate_monitors():
    """Return [{'rect': (l,t,r,b), 'work': (l,t,r,b), 'primary': bool}, ...], primary first.
    'rect' is the full monitor; 'work' is the work area (excludes the taskbar) — used to place
    a stranded window somewhere clickable rather than under the taskbar."""
    mons = []

    def _cb(hmon, hdc, lprc, lparam):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r, wk = mi.rcMonitor, mi.rcWork
            mons.append({"rect": (r.left, r.top, r.right, r.bottom),
                         "work": (wk.left, wk.top, wk.right, wk.bottom),
                         "primary": bool(mi.dwFlags & 1)})   # MONITORINFOF_PRIMARY
        return 1

    try:
        proc = _MONENUMPROC(_cb)
        _user32.EnumDisplayMonitors(None, None, proc, 0)
    except Exception:
        pass
    mons.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))  # primary, then L→R
    return mons


def virtual_screen_metrics():
    """(x, y, w, h) bounding box of the whole virtual desktop (all monitors combined), or None.
    Cheap (4 GetSystemMetrics calls) so it's usable as a per-poll display-topology signature:
    the box changes when a monitor is plugged/unplugged or a resolution changes."""
    try:
        g = _user32.GetSystemMetrics
        return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
                g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))
    except Exception:
        return None


def _mon_rect(m):
    """The usable rectangle of a monitor dict: prefer the work area, fall back to the full rect."""
    return m.get("work") or m.get("rect")


def _visible_extent(win, rect):
    """Visible (w, h) of the overlap of window win=(x,y,w,h) with a monitor rect=(l,t,r,b)."""
    x, y, w, h = win
    iw = min(x + w, rect[2]) - max(x, rect[0])
    ih = min(y + h, rect[3]) - max(y, rect[1])
    return max(0, iw), max(0, ih)


def _clamp_into(x, y, w, h, rect):
    """Slide a (w, h) window so it sits fully inside monitor rect=(l,t,r,b); top-left wins if the
    window is larger than the monitor."""
    l, t, r, b = rect
    if x + w > r:
        x = r - w
    if y + h > b:
        y = b - h
    return max(x, l), max(y, t)


def compute_onscreen_move(win, monitors, min_vis_w=48, min_vis_h=32):
    """Decide whether a window has drifted off EVERY connected monitor and, if so, where to
    move its top-left back to (keeping its size).

    win       -- (x, y, w, h) window rectangle, in virtual-desktop pixels.
    monitors  -- an enumerate_monitors()-style list (each entry's 'work' rect is used when
                 present, else 'rect'; a falsy rect is ignored).
    Returns (nx, ny) when the window must move, or None when it's still reachable — i.e. at
    least min_vis_w x min_vis_h of it shows on some monitor. The threshold means a window the
    user deliberately parked slightly off an edge is left alone; only a fully-stranded window
    (e.g. it was on a monitor that got unplugged) is pulled back, onto the monitor whose centre
    is nearest the window's centre.

    Pure geometry — no Win32 calls — so it's unit-testable off Windows."""
    x, y, w, h = win
    if w <= 0 or h <= 0:
        return None
    rects = [r for r in (_mon_rect(m) for m in (monitors or [])) if r]
    if not rects:
        return None
    need_w, need_h = min(min_vis_w, w), min(min_vis_h, h)
    for rect in rects:
        vw, vh = _visible_extent(win, rect)
        if vw >= need_w and vh >= need_h:
            return None                       # still reachable on this monitor → leave it
    cx, cy = x + w / 2.0, y + h / 2.0         # off every monitor → clamp onto the nearest one
    best, best_d = None, None
    for rect in rects:
        d = ((rect[0] + rect[2]) / 2.0 - cx) ** 2 + ((rect[1] + rect[3]) / 2.0 - cy) ** 2
        if best_d is None or d < best_d:
            best, best_d = rect, d
    return _clamp_into(x, y, w, h, best)
