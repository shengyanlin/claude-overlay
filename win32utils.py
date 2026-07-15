# -*- coding: utf-8 -*-
"""Windows-only Win32/ctypes helpers: DPI awareness, AppUserModelID, window-region
and display-affinity calls, and multi-monitor enumeration. Imports config for the
taskbar/app-id settings (one-way: win32utils -> config)."""

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys

from config import TASKBAR_BUTTON, APP_ID, APP_ICON

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

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


def set_window_app_id(hwnd, app_id=APP_ID, script_path=None, icon=APP_ICON):
    """Stamp THIS window's taskbar identity onto its shell property store so the taskbar
    button -- AND a pin created from it -- are correct with NO dependency on a Start Menu
    shortcut (so it works even on a locked-down box where the PowerShell shortcut builder is
    blocked by AppLocker / ExecutionPolicy).

    Writes, in one property-store session (SHGetPropertyStoreForWindow -> IPropertyStore):
      * PKEY_AppUserModel_ID (pid 5) = app_id -- a WINDOW-level id OUTRANKS both the
        process-wide id AND an MSIX host's package id, which is what fixes Microsoft Store
        Python (that packaged interpreter otherwise forces the button to pythonw's icon);
      * PKEY_AppUserModel_RelaunchCommand (pid 2) -- so a pin made from the running window
        relaunches the overlay after it's closed, and
      * PKEY_AppUserModel_RelaunchIconResource (pid 3) = "<clawd.ico>,0" -- so that pin shows
        the Clawd icon. The last two are built from script_path + icon and skipped when
        script_path is omitted (so the bare AUMID call is unchanged).

    Re-stamp on EVERY show/restore call: toggling overrideredirect / a withdraw->deiconify
    recreates the top-level HWND, so the id must be re-applied to whatever handle is current.
    Windows-only, best-effort, never raises; returns True only when every SetValue succeeded."""
    if sys.platform != "win32" or not (TASKBAR_BUTTON and app_id and hwnd):
        return False
    props = {5: app_id}   # PKEY_AppUserModel_ID
    try:
        target = _pythonw_exe()
        if script_path and target:
            sp = os.path.abspath(script_path)
            props[2] = '"%s" "%s"' % (target, sp)           # RelaunchCommand (quoted cmdline)
            ip = icon if (icon and os.path.isabs(icon)) else (
                os.path.join(os.path.dirname(sp), icon) if icon else "")
            if ip and os.path.exists(ip):
                props[3] = "%s,0" % ip                       # RelaunchIconResource: path,index
    except Exception:
        pass
    return _set_window_props(hwnd, props)


def _set_window_props(hwnd, props):
    """Write a set of PKEY_AppUserModel_* STRING properties onto a window's shell property
    store in ONE session. `props` maps a pid under the AppUserModel fmtid
    ({9F4C2855-...D5F3}) to a string value (2=RelaunchCommand, 3=RelaunchIconResource,
    5=AppUserModel_ID). Raw COM vtable calls (SHGetPropertyStoreForWindow -> IPropertyStore
    SetValue/Commit/Release). Windows-only, best-effort, never raises; returns True only when
    EVERY SetValue reported success."""
    if sys.platform != "win32" or not (hwnd and props):
        return False
    try:
        HRESULT = ctypes.c_long

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        def _guid(d1, d2, d3, tail):
            g = GUID()
            g.Data1, g.Data2, g.Data3 = d1, d2, d3
            g.Data4 = (ctypes.c_ubyte * 8)(*tail)
            return g

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]

        # Minimal PROPVARIANT: 8-byte header + one 8-byte union slot (a pointer, for the
        # VT_LPWSTR we store). Safe ONLY because SetValue's internal copy reads just vt + that
        # pointer for an LPWSTR -- never past offset 16. Do NOT reuse this for a wider variant.
        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                        ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                        ("p", ctypes.c_void_p)]

        iid_ps = _guid(0x886D8EEB, 0x8CF2, 0x4446,
                       (0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))
        fmtid = _guid(0x9F4C2855, 0x9F79, 0x4B39,          # PKEY_AppUserModel_* family
                      (0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3))

        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wt.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        shell32.SHGetPropertyStoreForWindow.restype = HRESULT
        pps = ctypes.c_void_p()
        if shell32.SHGetPropertyStoreForWindow(wt.HWND(hwnd), ctypes.byref(iid_ps),
                                               ctypes.byref(pps)) != 0 or not pps:
            return False
        # Raw IPropertyStore vtable: IUnknown's 3 slots, then GetCount/GetAt/GetValue, so
        # SetValue=6, Commit=7, Release=2.
        vtbl = ctypes.cast(pps, ctypes.POINTER(ctypes.c_void_p))[0]
        slots = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))
        SetValue = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                      ctypes.POINTER(PROPERTYKEY),
                                      ctypes.POINTER(PROPVARIANT))(slots[6])
        Commit = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)(slots[7])
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(slots[2])
        try:
            keepalive = []            # hold each c_wchar_p alive through Commit
            all_ok = True
            for pid, value in props.items():
                pk = PROPERTYKEY()
                pk.fmtid = fmtid
                pk.pid = pid
                s = ctypes.c_wchar_p(value)   # SetValue copies it; kept alive to be safe
                keepalive.append(s)
                pv = PROPVARIANT()
                pv.vt = 31                     # VT_LPWSTR
                pv.p = ctypes.cast(s, ctypes.c_void_p)
                if SetValue(pps, ctypes.byref(pk), ctypes.byref(pv)) != 0:
                    all_ok = False
            Commit(pps)
            return all_ok
        finally:
            Release(pps)
    except Exception:
        return False


_icon_handle_cache = {}   # (abspath, cx, cy) -> HICON int, so re-asserts reuse the handles


def set_window_icon(hwnd, icon=APP_ICON):
    """Stamp the Clawd icon straight onto THIS window's taskbar button via WM_SETICON, with NO
    dependency on any Start Menu shortcut. This is the fallback that keeps the RUNNING window's
    button icon correct even when the AUMID->shortcut chain can't be built (a locked-down box
    where AppLocker / ExecutionPolicy blocks the shortcut builder), and it reinforces the
    Store-Python case after set_window_app_id() pulls the button off the package group. Meant
    to be re-applied on every taskbar re-assert (the HWND can be recreated by a
    withdraw->deiconify / overrideredirect toggle); HICONs are LOADED ONCE and cached, so those
    frequent calls reuse the same 1-2 handles and never leak GDI handles (the v1.1.8
    handle-budget discipline). Windows-only, best-effort, never raises; True iff the big icon
    was set."""
    if sys.platform != "win32" or not (TASKBAR_BUTTON and icon and hwnd):
        return False
    try:
        ip = icon if os.path.isabs(icon) else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), icon)
        if not os.path.exists(ip):
            return False
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x00000010
        SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
        _user32.LoadImageW.restype = wt.HANDLE
        _user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, ctypes.c_uint,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        _user32.SendMessageW.restype = ctypes.c_ssize_t
        _user32.SendMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t,
                                         ctypes.c_void_p]

        def _load(cx, cy):
            key = (ip, cx, cy)
            if key not in _icon_handle_cache:
                _icon_handle_cache[key] = _user32.LoadImageW(
                    None, ip, IMAGE_ICON, cx, cy, LR_LOADFROMFILE) or None
            return _icon_handle_cache[key]

        hbig = _load(_user32.GetSystemMetrics(SM_CXICON) or 32,
                     _user32.GetSystemMetrics(SM_CYICON) or 32)
        hsmall = _load(_user32.GetSystemMetrics(SM_CXSMICON) or 16,
                       _user32.GetSystemMetrics(SM_CYSMICON) or 16)
        if hsmall:
            _user32.SendMessageW(wt.HWND(hwnd), WM_SETICON, ICON_SMALL, hsmall)
        if hbig:
            _user32.SendMessageW(wt.HWND(hwnd), WM_SETICON, ICON_BIG, hbig)
        return bool(hbig)
    except Exception:
        return False


def _pythonw_exe():
    """The interpreter to launch the overlay from a taskbar-pin relaunch. Prefer a
    windowless pythonw.exe (so the relaunch doesn't flash a console); fall back to the
    running interpreter. Uses sys.executable so it tracks whatever Python is actually in
    use, even a moved/reinstalled one."""
    exe = sys.executable or ""
    if exe and os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def ensure_taskbar_shortcut(script_path, app_id=APP_ID, icon=APP_ICON, name="Claude Overlay"):
    """Self-heal the Start Menu shortcut Windows needs to pin the overlay to the taskbar
    like a normal app. The overlay is a frameless pythonw window that declares an explicit
    AppUserModelID; Windows will only back a taskbar PIN with a Start Menu .lnk whose
    System.AppUserModel.ID matches that id. With no such shortcut, pinning degrades to the
    raw pythonw.exe — so the pin won't relaunch the overlay once it's closed, and it shows
    pythonw's generic icon instead of Clawd. Creating this matching shortcut fixes both.

    Cheap by design: the common path is a couple of file reads (a stored-signature match)
    and spawns NO subprocess. The one-shot PowerShell builder runs only when the .lnk is
    missing or its recorded signature changed (first launch, the folder moved via OneDrive,
    a new interpreter or icon). Windows-only, never raises; returns one of
    'skipped' / 'ok' / 'created' / 'error' for logging."""
    if sys.platform != "win32" or not (TASKBAR_BUTTON and app_id):
        return "skipped"
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return "skipped"
        script_path = os.path.abspath(script_path)
        repo = os.path.dirname(script_path)
        target = _pythonw_exe()
        if not target:
            return "skipped"
        icon_abs = ""
        if icon:
            ip = icon if os.path.isabs(icon) else os.path.join(repo, icon)
            if os.path.exists(ip):
                icon_abs = ip
        lnk = os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                           "Programs", name + ".lnk")
        # The recorded signature: if the live .lnk still matches this exactly, do nothing.
        desired = {"v": 1, "lnk": lnk, "target": target, "args": script_path,
                   "workdir": repo, "icon": icon_abs, "app_id": app_id}
        state_dir = os.path.join(os.path.expanduser("~"), ".claude-overlay")
        marker = os.path.join(state_dir, "startmenu_shortcut.json")
        try:
            if os.path.exists(lnk) and os.path.exists(marker):
                with open(marker, "r", encoding="utf-8") as f:
                    if json.load(f) == desired:
                        return "ok"
        except Exception:
            pass
        ps1 = os.path.join(repo, "install-startmenu-shortcut.ps1")
        if not os.path.exists(ps1):
            return "error"
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
                "-Lnk", lnk, "-Target", target, "-Arguments", script_path,
                "-WorkingDir", repo, "-AppId", app_id]
        if icon_abs:
            args += ["-Icon", icon_abs]
        r = subprocess.run(args, capture_output=True, timeout=60,
                           creationflags=_CREATE_NO_WINDOW)
        if r.returncode != 0:
            return "error"
        os.makedirs(state_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(desired, f)
        return "created"
    except Exception:
        return "error"

def relaunch_overlay(script_path):
    """Start a FRESH overlay instance (pythonw + the script) that OUTLIVES this process, so the
    app can restart itself — e.g. after updating the CLI, to pick up the newest models without
    the user manually reopening. The child is fully detached (its own process group, untied from
    this process's console) so the caller can then quit() — which ends in os._exit — without
    taking the new instance down with it. The overlay already supports several instances at once,
    so a brief overlap during the hand-off is fine. Windows-only meaningful. Returns the child
    pid, or RAISES so the caller can choose NOT to quit if the relaunch didn't even start."""
    exe = _pythonw_exe()
    if not exe:
        raise RuntimeError("no interpreter to relaunch with")
    script_path = os.path.abspath(script_path)
    flags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: cut the child loose from this process's
        # console/lifetime so closing this window can't cascade to the new one. pythonw is a GUI
        # binary (no console), so there's no window to suppress.
        flags = 0x00000008 | 0x00000200
    p = subprocess.Popen([exe, script_path],
                         cwd=os.path.dirname(script_path) or None, creationflags=flags)
    return p.pid


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


# ── active-window capture (SHOT_SCOPE="window") ────────────────────────────────
# Everything needed to answer "which window is the user actually working in, and what
# rectangle of the screen does it cover?" — used when screenshots are scoped to the
# active window instead of every monitor.
_user32.IsWindow.argtypes = [wt.HWND]
_user32.IsWindow.restype = wt.BOOL
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsWindowVisible.restype = wt.BOOL
_user32.IsIconic.argtypes = [wt.HWND]
_user32.IsIconic.restype = wt.BOOL
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
_user32.GetWindowRect.restype = wt.BOOL
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetShellWindow.restype = wt.HWND
_user32.GetDesktopWindow.restype = wt.HWND
_kernel32 = ctypes.windll.kernel32
_kernel32.GetCurrentProcessId.restype = wt.DWORD
GA_ROOT = 2
# DWMWA_EXTENDED_FRAME_BOUNDS is the window's VISIBLE frame — unlike GetWindowRect it
# excludes the drop shadow and the invisible resize borders, so the capture doesn't
# include a strip of whatever sits behind the window. dwmapi guarded: it exists on
# every supported Windows, but a load failure must degrade to GetWindowRect, not crash.
DWMWA_EXTENDED_FRAME_BOUNDS = 9
try:
    _dwmapi = ctypes.windll.dwmapi
    _dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
except Exception:
    _dwmapi = None


def window_is_own(hwnd):
    """True when the window belongs to THIS process (the overlay itself — including the
    collapsed orb): never a capture target, we want what the user is working in."""
    try:
        pid = wt.DWORD(0)
        tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) if hwnd else 0
        if not tid or not pid.value:
            return True   # lookup failed → can't tell whose window this is
        return pid.value == _kernel32.GetCurrentProcessId()
    except Exception:
        return True     # can't tell → treat as our own so it's never captured by mistake


def window_capturable(hwnd):
    """A window that still makes sense to screenshot: exists, visible, not minimized
    (a minimized window's rect is a meaningless off-screen stub)."""
    try:
        return bool(hwnd and _user32.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd)
                    and not _user32.IsIconic(hwnd))
    except Exception:
        return False


def foreground_capture_window():
    """The top-level foreground window as a capture target, or None when the foreground
    is unusable: this process (the user is typing in the overlay), the desktop/shell
    (nothing focused), or a window that's gone/minimized. The caller falls back to the
    last tracked external window, then to full-screen capture."""
    try:
        fg = _user32.GetForegroundWindow()
        if not fg:
            return None
        fg = _user32.GetAncestor(fg, GA_ROOT) or fg
        if fg in (_user32.GetShellWindow(), _user32.GetDesktopWindow()):
            return None
        if window_is_own(fg) or not window_capturable(fg):
            return None
        return fg
    except Exception:
        return None


def window_title(hwnd):
    """The window's title bar text ('' on failure) — labels the shot for the model."""
    try:
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def clamp_bbox(rect, vbox):
    """Intersect a window rect (l,t,r,b) with the virtual-desktop box (x,y,w,h) so a
    half-dragged-offscreen window grabs only its visible part; None when the visible
    overlap is degenerate (<8px a side — nothing worth sending). Pure math, no Win32."""
    if not rect:
        return None
    l, t, r, b = rect
    if vbox:
        x, y, w, h = vbox
        l, t = max(l, x), max(t, y)
        r, b = min(r, x + w), min(b, y + h)
    if r - l < 8 or b - t < 8:
        return None
    return (l, t, r, b)


def window_bbox(hwnd):
    """Screen-space (l,t,r,b) of a window suitable for ImageGrab(all_screens=True):
    DWM extended frame bounds (visible frame, no shadow) with GetWindowRect as the
    fallback, clipped to the virtual desktop. None when it can't be determined."""
    rect = None
    if _dwmapi is not None:
        rc = wt.RECT()
        try:
            if _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                             ctypes.byref(rc), ctypes.sizeof(rc)) == 0:
                rect = (rc.left, rc.top, rc.right, rc.bottom)
        except Exception:
            rect = None
    if rect is None:
        rc = wt.RECT()
        try:
            if _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                rect = (rc.left, rc.top, rc.right, rc.bottom)
        except Exception:
            rect = None
    return clamp_bbox(rect, virtual_screen_metrics())


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
