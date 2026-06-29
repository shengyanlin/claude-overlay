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
    """Return [{'rect': (l, t, r, b), 'primary': bool}, ...], primary first."""
    mons = []

    def _cb(hmon, hdc, lprc, lparam):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            mons.append({"rect": (r.left, r.top, r.right, r.bottom),
                         "primary": bool(mi.dwFlags & 1)})   # MONITORINFOF_PRIMARY
        return 1

    try:
        proc = _MONENUMPROC(_cb)
        _user32.EnumDisplayMonitors(None, None, proc, 0)
    except Exception:
        pass
    mons.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))  # primary, then L→R
    return mons
