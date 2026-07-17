# -*- coding: utf-8 -*-
"""
Claude Overlay — a frameless, always-on-top floating chat window styled like the
Claude desktop app. Talks to Claude Code (via the Agent SDK, using your existing
subscription) and can SEE your screen by attaching screenshots.

Stack: Tkinter (UI) + claude-agent-sdk (drives the npm `claude` CLI) + Pillow
(screen capture) + keyboard (global hotkey). No API key required — it reuses the
`claude` login you already have.

Run:   pythonw claude_overlay.py     (no console)
       python  claude_overlay.py     (console, for debugging)
"""

import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import sys
import threading
import time
import queue
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageGrab, ImageDraw, ImageChops, ImageFilter, ImageTk

from config import *
from config import __version__
from debuglog import dbg, DEBUG_LOG
from win32utils import *
from win32utils import _user32, _gdi32
from worker import ClaudeWorker

# ───────────────────────────── the overlay UI ─────────────────────────────
PLACEHOLDER = "Reply to Claude…"
TOOL_ICONS = {
    "Read": "▤", "Write": "✎", "Edit": "✎", "MultiEdit": "✎", "NotebookEdit": "✎",
    "Bash": "❯", "BashOutput": "❯", "KillShell": "❯", "PowerShell": "❯",
    "Glob": "⌕", "Grep": "⌕", "WebSearch": "⌕", "WebFetch": "↗", "ToolSearch": "⌕",
    "TodoWrite": "☑", "Task": "◆",
}


def _ensure_shot_dir():
    """Create SHOT_DIR, guarded, BEFORE the worker starts. If the configured TEMP path can't
    hold it (permission, path-length, it's a file not a dir), fall back to a fresh temp dir
    rather than crashing startup after the background worker is already running."""
    global SHOT_DIR
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        if SHOT_DIR.is_dir():
            return
    except Exception:
        pass
    import tempfile
    try:
        SHOT_DIR = Path(tempfile.mkdtemp(prefix="claude_overlay_shots_"))
    except Exception:
        SHOT_DIR = Path(tempfile.gettempdir())


def _load_state():
    """Best-effort read of the tiny persisted UI state (STATE_FILE). Any problem —
    missing, unreadable, not a dict, absurdly large — yields {} so startup can't break."""
    try:
        if STATE_FILE.stat().st_size > 64 * 1024:   # sanity cap; ours is tens of bytes
            return {}
        data = json.loads(STATE_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(**updates):
    """Merge updates into STATE_FILE (temp file + os.replace, so a crash mid-write can't
    leave truncated JSON behind). Best-effort: persisting a toggle must never break the UI."""
    try:
        state = _load_state()
        state.update(updates)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), "utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _startup_permission_mode():
    """Decide this launch's permission state: (read_only, mode to LAUNCH the worker in).
    The remembered Read-only toggle — a deliberate user choice, like Window-only — wins
    over the config default; PERMISSION_MODE seeds the first launch (no saved state).
    A remembered unlock launches straight in the full-access mode, so the session is
    born bypass-capable when full access means bypassPermissions — a running session
    can never be ELEVATED to bypass later (see worker._bypass_capable)."""
    ro = (PERMISSION_MODE == "plan")
    saved = _load_state().get("read_only")
    if isinstance(saved, bool):
        ro = saved
    full = PERMISSION_MODE if PERMISSION_MODE != "plan" else "bypassPermissions"
    return ro, ("plan" if ro else full)


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class Overlay:
    def __init__(self):
        self.ui_q: "queue.Queue" = queue.Queue()
        _ensure_shot_dir()          # before the worker, so a bad TEMP can't crash us mid-startup
        # Resolve the remembered Read-only toggle BEFORE the worker exists: the session
        # must be LAUNCHED in the remembered mode (a plan-launched session can't be
        # elevated to bypassPermissions at run time, only started in it).
        self.read_only, _launch_mode = _startup_permission_mode()
        self.worker = ClaudeWorker(self.ui_q, permission_mode=_launch_mode)
        self.worker.start()

        self.auto_shot = AUTO_SCREENSHOT_DEFAULT
        self.window_shot = (SHOT_SCOPE == "window")   # True → capture only the active window
        if not SHOT_SCOPE_FORCED:                     # remembered toggle choice survives a
            self.window_shot = bool(_load_state().get("window_shot", self.window_shot))
                                                      # relaunch; an explicit env var beats it
        self.share_visible = SHOW_IN_SCREEN_SHARE_DEFAULT   # True → overlay shows in screen shares
        # self.read_only was set above (worker launch); the toggle flips it via the
        # worker (confirmed async) and each confirmed change is persisted.
        self._full_mode = (PERMISSION_MODE if PERMISSION_MODE != "plan"
                           else "bypassPermissions")  # what Read-only OFF returns to: the
                                                      # configured mode — unless that IS plan,
                                                      # then the CLI default full-access mode
        self.pending_shot = None
        self.pending_images: list = []
        self._precaptured = None        # (shots, monotonic_ts) grabbed while typing
        self._precapture_after = None   # pending debounce timer id
        self._capture_busy = False      # a background precapture grab is in flight
        self._paste_busy = False        # a background clipboard paste is in flight
        self._quitting = False          # make quit() idempotent (double-close → one teardown)
        self._orb_imgs: dict = {}       # (size, hover) → PhotoImage cache for the orb
        self._send_imgs: dict = {}      # (diameter, state) → PhotoImage cache for the send button
        self._send_hover = False
        self.busy = False
        self.visible = True
        self.expanded = True
        self._toggle_request = False
        self._model = None
        self._ctx_pct = None
        self._claude_header = False
        self._thinking_active = False   # a thinking block is open in the current turn
        # streaming-Markdown renderer state (per turn): the current unfinished answer line
        # (re-rendered live so inline emphasis lands the moment its closing marker streams in),
        # a table being assembled across lines, and whether we're inside a ``` code fence.
        self._md_tail = ""
        self._md_tbl = None
        self._md_fence = False
        self._md_last_scroll = 0.0   # throttle yview()/see() — both are O(line) on a giant line
        # "Copy message" support: accumulate the raw streamed answer text for the current turn
        # so each message's Copy button can snapshot it. _turn_copy_added guards against the
        # several turn_done/result events emitting more than one button per assistant turn.
        self._turn_raw = ""
        self._turn_copy_added = False
        self._last_pump = time.monotonic()   # hang-watchdog heartbeat
        self._pump_logged = 0.0              # throttle the periodic "pump alive" debug line
        self._drag = (0, 0)
        self._resize = None
        self._round_after = None
        self._last_cfg_size = None   # last (w,h) we re-applied the window region for
        self._capture_excluded = False   # set once WDA_EXCLUDEFROMCAPTURE is applied
        self._update_available = None     # set to the newer version string if one exists
        self._cli_update_shown = False    # show the "CLI is out of date" notice at most once/session
        self._cli_update_btn_ref = None   # the in-chat Update button, so its result can restyle it
        self._restarting = False          # guard: one self-restart (relaunch + quit) at a time
        self._mapping = False             # re-entrancy guard for the <Map> taskbar re-assert
        self._fronting = False            # re-entrancy guard for _raise_to_front (focus churn)
        self._vscreen_sig = None          # last virtual-desktop bounding box (display-topology sig)
        self._vscreen_checked = 0.0       # throttle the topology watchdog to ~1.5s in _poll
        self._last_ext_fg = None          # last EXTERNAL foreground hwnd — the window the user was
                                          # working in before focusing the overlay; the "window"
                                          # capture scope targets it whenever the overlay has focus
        self._fg_checked = 0.0            # throttle that tracking to ~0.5s in _poll
        # Per-overlay custom name (session-only, set by clicking the titlebar "Claude"). Shown
        # in the titlebar + window title when expanded, and as a small pill UNDER the orb when
        # collapsed — so several overlays open at once (one per task) are tellable apart at a
        # glance while collapsed. Empty → the default "Claude" everywhere (original behaviour).
        self.overlay_name = ""
        self._rename_entry = None         # the in-place rename Entry while editing, else None
        self._collapsed_mask = None       # PIL 'L' silhouette (orb ∪ name-pill ∪ done-badge) for the
                                          # clipped collapsed window; None → plain orb sprite/ellipse
        self._task_done_badge = False     # show a "stage complete" badge on the collapsed orb after
                                          # a reply finishes while collapsed; cleared on expand/new turn
        # /compact: a context-summarization pass driven by sending the CLI's `/compact` command.
        # While it runs we animate a one-line banner in the chat (mirrors the CLI's compaction
        # spinner) — REAL Text content rewritten in place, so it wraps + zooms — then rewrite
        # that same line as the result.
        self._compacting = False
        self._compact_line = False        # True while the animated/result line exists in the chat
        self._compact_anim_after = None   # pending animation timer id
        self._compact_t0 = 0.0            # monotonic start (for the elapsed-seconds counter)
        self._compact_frame = 0

        self._build()
        self._register_hotkey()
        self.root.after(60, self._poll)

    def px(self, v):
        return int(round(v * self.s))

    # ── construction ──
    def _build(self):
        self.root = tk.Tk()
        self.root.title("Claude")
        self.s = max(1.0, self.root.winfo_fpixels("1i") / 96.0)   # DPI scale factor
        self.root.overrideredirect(True)
        self._apply_app_icon()                 # Clawd icon for the taskbar button / alt-tab
        self.root.configure(bg=T["bg"])
        self.root.attributes("-topmost", True)
        if WINDOW_ALPHA < 1.0:   # avoid WS_EX_LAYERED, which ignores SetWindowRgn rounding
            self.root.attributes("-alpha", WINDOW_ALPHA)
        w, h = self.px(420), self.px(620)
        wa = wt.RECT()                                  # primary monitor work area
        _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
        x = max(self.px(10), wa.right - w - self.px(28))
        y = wa.top + self.px(56)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(self.px(330), self.px(300))

        avail = set(tkfont.families())
        pick = lambda cands: next((c for c in cands if c in avail), cands[-1])
        self.sans, self.serif, self.mono = pick(FONT_SANS), pick(FONT_SERIF), pick(FONT_MONO)
        self.zoom = 1.0
        self._fonts = []   # (Font, base_logical_size) → reconfigured live on Ctrl +/-
        # Embedded canvases (user bubbles, tool chips, tables, Copy buttons) are fixed-size and
        # draw with their own font, so they don't track the shared fonts. Register each with its
        # render() here so a zoom can redraw it at the new size (see _rezoom_embeds).
        self._zoomables = []
        self._rezoom_after = None
        def mk(fam, base, **k):
            f = tkfont.Font(family=fam, size=-self.px(base), **k)
            self._fonts.append((f, base))
            return f
        self.f_title = mk(self.serif, 16, weight="bold")
        self.f_body  = mk(self.sans, 15)
        self.f_small = mk(self.sans, 12)
        self.f_chip  = mk(self.sans, 11, weight="bold")
        self.f_mono  = mk(self.mono, 12)
        self.f_send  = mk(self.sans, 17, weight="bold")
        self.f_think = mk(self.sans, 13, slant="italic")   # streamed extended-thinking text
        # Markdown answer styling — registered with self._fonts so they live-zoom with the body.
        self.f_bold  = mk(self.sans, 15, weight="bold")
        self.f_ital  = mk(self.sans, 15, slant="italic")
        self.f_code  = mk(self.mono, 13)
        self.f_h1    = mk(self.sans, 19, weight="bold")
        self.f_h2    = mk(self.sans, 17, weight="bold")
        self.f_h3    = mk(self.sans, 15, weight="bold")
        # Collapsed-orb name pill: a fixed-size label (NOT registered for zoom — it only shows
        # while collapsed, where the chat-text zoom is irrelevant). Kept as a ref so Tk won't GC it.
        self.f_pill  = tkfont.Font(family=self.sans, size=-self.px(13), weight="bold")

        self._build_titlebar()
        self.hairline = tk.Frame(self.root, bg=T["border"], height=1)
        self.hairline.pack(fill="x")
        self._build_statusline()   # very bottom: model + context %
        self._build_statusbar()    # controls row (above statusline)
        self._build_input()        # side=bottom (above controls)
        self._build_chat()         # side=top, fills the middle
        self._build_orb()          # collapsed bubble (hidden until "—")
        self._build_edges()        # invisible drag strips on every edge/corner → resize
        self._bind_zoom()          # Ctrl +/- and Ctrl+wheel → live text zoom
        self._intro()
        # A remembered Read-only choice silently overriding the config default is a
        # SAFETY state — say so up front, so a launch never surprises.
        if self.read_only != (PERMISSION_MODE == "plan"):
            self.add_sys("🔒 Read-only restored from your last session — flip the "
                         "Read-only toggle off for full access." if self.read_only else
                         "⚡ Full access restored from your last session (you had "
                         "switched Read-only off). Flip it back on any time.")
        # Anything the per-machine config.json couldn't apply (typo'd key, wrong type,
        # unreadable file) must be SEEN — a silently-skipped PERMISSION_MODE would
        # launch a full-access session the user believed was read-only.
        for w in USER_CONFIG_WARNINGS:
            self.add_sys(f"⚠ {USER_CONFIG_FILE.name}: {w}")

        self.root.after(130, lambda: (self.root.focus_force(), self.entry.focus_set()))
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<Map>", self._on_map, add="+")   # restore (incl. from taskbar) re-asserts the frameless look
        self.root.bind("<FocusIn>", self._on_focus_in, add="+")  # taskbar-click / alt-tab activation → raise above topmost peers
        self.root.after(170, self._apply_region)
        self.root.after(180, self._apply_share_visibility)
        self.root.after(220, self._install_taskbar_button)
        self.root.after(1200, self._check_for_update)
        self.root.after(1500, self._check_cli_update)
        self._start_hang_watchdog()    # diagnostic: dumps all-thread stacks if the UI pump stalls

    def _start_hang_watchdog(self):
        """Diagnostic (active only when CLAUDE_OVERLAY_DEBUG_LOG is set): a daemon thread that,
        if the Tk event pump (_poll) stops heart-beating for >4 s — i.e. the UI is actually
        wedged, which from OUTSIDE looks identical to a healthy idle window (CPU 0, not
        "hung") — dumps every thread's Python stack to the log. That names the exact line /
        lock / queue.get the UI thread is stuck on. Pure in-process (faulthandler), no memory
        reads, no external profiler."""
        if not DEBUG_LOG:
            return
        import faulthandler
        import threading as _th

        def _watch():
            dumped_for = None
            while True:
                time.sleep(1.0)
                try:
                    lp = getattr(self, "_last_pump", 0.0)
                    stalled = time.monotonic() - lp
                    if stalled > 4.0 and dumped_for != lp:   # one dump per distinct stall episode
                        dumped_for = lp
                        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                            f.write("\n===== HANG WATCHDOG: pid=%d UI pump _poll stalled %.1fs @ %s "
                                    "(>1800s usually = laptop sleep/long idle, not a real hang) — all-thread stacks =====\n"
                                    % (os.getpid(), stalled, time.strftime("%H:%M:%S")))
                            f.flush()
                            faulthandler.dump_traceback(file=f, all_threads=True)
                            f.write("===== END HANG DUMP =====\n")
                            f.flush()
                except Exception:
                    pass

        _th.Thread(target=_watch, name="hang-watchdog", daemon=True).start()

    @staticmethod
    def _parse_ver(s):
        import re
        # str() so a non-string tag name can't throw; [:9] caps digits so a hostile
        # 1MB-digit "version" can't hit Python's int-from-string limit (ValueError).
        nums = re.findall(r"\d+", str(s or ""))
        return tuple(int(n[:9]) for n in nums[:3]) if nums else (0,)

    def _check_for_update(self):
        """Best-effort: ask GitHub for the newest tag in a background thread and, if it's
        newer than __version__, flag it. Stays silent on any failure (offline, GitHub
        down, corporate TLS interception) so it never blocks or nags."""
        def work():
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.github.com/repos/shengyanlin/claude-overlay/tags",
                    headers={"User-Agent": "claude-overlay", "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    body = r.read(MAX_UPDATE_BODY + 1)   # bound the body BEFORE json.loads — a
                if len(body) > MAX_UPDATE_BODY:          # hostile/compromised endpoint could
                    return                               # otherwise stream us a huge document
                tags = json.loads(body.decode("utf-8", "replace"))
                if not isinstance(tags, list):
                    return
                latest = max((self._parse_ver(t.get("name", "")) for t in tags[:MAX_UPDATE_TAGS]
                              if isinstance(t, dict)), default=None)
                if latest and latest > self._parse_ver(__version__):
                    self.ui_q.put(("update", ".".join(map(str, latest))))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _check_cli_update(self):
        """Best-effort, background: if the installed `claude` CLI is behind the latest npm
        release, surface a one-click update notice (see cliupdate.py). The overlay and the CLI
        update independently — a current overlay still drives whatever CLI is installed, and an
        old CLI silently runs an older model. Silent on any failure (no npm, offline, corporate
        proxy) so it never blocks or nags; the check is throttled to once/day inside cliupdate."""
        if not CLI_UPDATE_CHECK:
            return
        def work():
            try:
                from cliupdate import check_update
                info = check_update()
                if info and info.get("behind"):
                    self.ui_q.put(("cli_update", info))
            except Exception:
                pass
        threading.Thread(target=work, name="cli-update-check", daemon=True).start()

    def _apply_share_visibility(self):
        """Apply the current screen-share visibility to the window's DWM display affinity.
        share_visible=False (default) → WDA_EXCLUDEFROMCAPTURE: the overlay is omitted from
        ALL screen captures (Teams/Zoom/OBS share, PrintScreen, our own screenshots) while
        staying visible to the user — and capture() can then skip the withdraw()+sleep()
        dance (no flicker, no UI freeze). share_visible=True → WDA_NONE: the overlay shows up
        in screen shares again, and capture() falls back to a brief withdraw during its own
        grabs so Claude's screenshots still never contain the overlay. The affinity is bound
        to the HWND and persists across show/hide (verified: the +180ms exclusion survives the
        +220ms taskbar withdraw→deiconify), so this only needs re-applying when the toggle flips."""
        try:
            self.root.update_idletasks()
            hwnd = _user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            want_excluded = not self.share_visible
            affinity = WDA_EXCLUDEFROMCAPTURE if want_excluded else WDA_NONE
            ok = bool(_user32.SetWindowDisplayAffinity(hwnd, affinity))
            # Only claim exclusion when we asked for it AND the call succeeded. If anything
            # failed, treat the window as capturable so capture() still hides it via withdraw()
            # — never leak the overlay into the screenshots we send Claude.
            self._capture_excluded = want_excluded and ok
        except Exception:
            self._capture_excluded = False

    # ── taskbar button (frameless windows get none by default) ──
    def _hwnd(self):
        """The top-level window handle (GA_ROOT), not the Tk child."""
        return _user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()

    def _apply_app_icon(self):
        """Give the window (hence the taskbar button + alt-tab) the Clawd icon."""
        if not APP_ICON:
            return
        try:
            p = Path(APP_ICON)
            if not p.is_absolute():
                p = Path(__file__).with_name(APP_ICON)
            if p.exists():
                self.root.iconbitmap(default=str(p))
        except Exception:
            pass

    def _set_taskbar_button(self):
        """Force a taskbar button onto the frameless (overrideredirect) window by setting
        WS_EX_APPWINDOW / clearing WS_EX_TOOLWINDOW on its top-level handle. Idempotent —
        only writes when the bits actually need changing, so it's cheap to re-assert on
        every show/restore. No window-region / <Configure> work, so it's clear of the
        v1.1.9 freeze class."""
        if not TASKBAR_BUTTON:
            return
        try:
            hwnd = self._hwnd()
            # Stamp our full taskbar identity onto the window BEFORE the app-window style flip so
            # the button is born under our id: PKEY_AppUserModel_ID (fixes MSIX/Store-Python,
            # where the process-wide id is ignored) PLUS RelaunchCommand + RelaunchIconResource so
            # a pin made from it stays correct — right icon AND relaunches the overlay — even with
            # NO Start Menu shortcut (a locked-down box where the shortcut builder is blocked).
            # Re-stamped on EVERY call: toggling overrideredirect / a withdraw→deiconify recreates
            # the top-level HWND, so it must be re-applied to whatever handle is current.
            set_window_app_id(hwnd, script_path=os.path.abspath(__file__))
            style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            if new != style:
                _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
            # Belt-and-suspenders for the RUNNING button: stamp the Clawd icon straight onto the
            # window (WM_SETICON) so it's right even when no shortcut / relaunch icon resolves.
            set_window_icon(hwnd)
        except Exception:
            pass

    def _install_taskbar_button(self):
        """One-time at startup: set the app-window style, then nudge the shell with a
        single withdraw→deiconify so it actually materializes the button (Windows only
        (re)evaluates taskbar membership when a window is shown). The brief hide/show is a
        one-shot at launch, not on the streaming path."""
        if not TASKBAR_BUTTON:
            return
        self._set_taskbar_button()
        try:
            geo = self.root.geometry()
            self.root.withdraw()
            self.root.after(10, lambda: self._after_taskbar_show(geo))
        except Exception:
            pass

    def _after_taskbar_show(self, geo=None):
        try:
            self.root.deiconify()
            self.root.overrideredirect(True)        # deiconify can re-add decorations → strip them
            if geo:
                self.root.geometry(geo)
            self.root.attributes("-topmost", True)
            self._set_taskbar_button()
            self.root.after(20, self._apply_region)  # restore rounded corners
        except Exception:
            pass

    def _on_map(self, e):
        """Fires on the initial show and on every restore — including a restore from a
        taskbar-button click that had minimized us. Re-assert the frameless look + topmost
        + rounded region + app-window style so a taskbar restore can't bring back the title
        bar / square corners. Guarded against the recursion our own deiconify triggers, and
        ignores child-widget <Map> events."""
        if e.widget is not self.root or not TASKBAR_BUTTON or self._mapping:
            return
        self._mapping = True
        try:
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self._set_taskbar_button()
            self.root.after(20, self._apply_region)
            self.root.after(40, lambda: self._raise_to_front(focus=True))  # restore-from-minimize → come forward + focus
        except Exception:
            pass
        finally:
            self.root.after(150, lambda: setattr(self, "_mapping", False))

    def _safe_focus_entry(self):
        try:
            if getattr(self, "entry", None) and self.entry.winfo_exists():
                self.entry.focus_set()
        except Exception:
            pass

    def _raise_to_front(self, focus=False):
        """Bring the overlay above ALL windows — including other always-on-top windows — and
        optionally focus the input. The OS activates us on a taskbar-button click / alt-tab /
        restore but does NOT re-order us above topmost peers, so the click could leave us
        buried under another always-on-top window (or just unfocused). This does the z-order
        raise the activation skips. Pure z-order (no SetWindowRgn / <Configure>), so it's
        idempotent, flicker-free when already on top, and clear of the v1.1.9 freeze class.
        Guarded against the focus churn it can itself trigger."""
        if self._fronting:
            return
        self._fronting = True
        try:
            hwnd = self._hwnd()
            self._ensure_on_screen()   # a raise is z-order only; if the window was stranded off a
                                       # now-unplugged monitor, first bring it back into view
            self.root.lift()
            _user32.BringWindowToTop(hwnd)
            # re-insert at the top of the topmost band → above other always-on-top windows;
            # NOACTIVATE because the OS has already handed us activation (or _force_foreground did).
            _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            if focus:
                self.root.after(30, self._safe_focus_entry)
        except Exception:
            pass
        finally:
            self.root.after(150, lambda: setattr(self, "_fronting", False))

    def _force_foreground(self):
        """Steal the foreground to us even when another process owns the current foreground
        window (the hotkey path — there WE initiate activation, so the OS hasn't handed us
        foreground yet). The AttachThreadInput trick gets past Windows' foreground lock."""
        try:
            hwnd = self._hwnd()
            fg = _user32.GetForegroundWindow()
            try:                              # the hotkey moment is the freshest possible
                hw = foreground_capture_window()   # answer to "which window was the user in?"
                if hw:                             # — record it before we steal the foreground
                    self._last_ext_fg = hw
            except Exception:
                pass
            cur = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
            me = _user32.GetWindowThreadProcessId(hwnd, None)
            attached = bool(cur and cur != me and _user32.AttachThreadInput(cur, me, True))
            _user32.SetForegroundWindow(hwnd)
            if attached:
                _user32.AttachThreadInput(cur, me, False)
        except Exception:
            pass

    def _ensure_on_screen(self):
        """If the window has drifted off EVERY connected monitor — e.g. a secondary monitor it
        was sitting on got unplugged — move it back onto a visible monitor's work area. Without
        this, a taskbar-button click / hotkey / restore correctly raises the window's z-order but
        leaves it at coordinates that no longer exist, so it never appears (the "can't bring it to
        the front after unplugging a screen" bug). A no-op when the window is already reachable, so
        it's safe on every bring-to-front path. Move-only geometry (never resizes) → can't trigger
        a SetWindowRgn size change, so it's clear of the v1.1.9 freeze class. Returns the applied
        (x, y) when it moved, else None."""
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            w, h = self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            return None
        if w <= 1 or h <= 1:
            return None
        try:
            mons = enumerate_monitors()
        except Exception:
            return None
        move = compute_onscreen_move((x, y, w, h), mons,
                                     min_vis_w=self.px(48), min_vis_h=self.px(32))
        if move is None:
            return None
        nx, ny = move
        try:
            self.root.geometry(f"+{nx}+{ny}")   # move-only; keeps size, so no region churn
        except Exception:
            return None
        if DEBUG_LOG:
            try:
                dbg("onscreen", "stranded window pulled back: (%d,%d %dx%d)->(%d,%d)"
                    % (x, y, w, h, nx, ny))
            except Exception:
                pass
        return (nx, ny)

    def _on_focus_in(self, e):
        """The OS activated our top-level window — a taskbar-button click while we're visible-
        but-behind, an alt-tab to us, or a restore all fire <FocusIn> on the root window. Raise
        above any topmost peers so the activation actually brings us forward. Child-widget focus
        (entry, chat) fires <FocusIn> on the CHILD, not the root, so this only runs on real
        window activation. focus=False so we don't yank input focus from e.g. a chat-text
        selection — the OS already gave the window focus; we only need to raise it."""
        if e.widget is not self.root or not TASKBAR_BUTTON:
            return
        self._raise_to_front(focus=False)

    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg=T["bg"], height=self.px(44))
        bar.pack(fill="x", side="top")
        self.titlebar = bar
        bar.pack_propagate(False)
        self._bind_drag(bar)
        sz = self.px(24)
        mark = tk.Canvas(bar, width=sz, height=sz, bg=T["bg"], highlightthickness=0)
        mark.pack(side="left", padx=(self.px(14), self.px(7)))
        self._draw_spark(mark, sz / 2, sz / 2, self.px(9))
        self._bind_drag(mark)
        # The title doubles as the rename target: click it (without dragging) to edit this
        # overlay's name; dragging it still moves the window (moved-detection, like the orb).
        self.title_lbl = tk.Label(bar, text=self.overlay_name or "Claude", bg=T["bg"],
                                  fg=T["text"], font=self.f_title, cursor="hand2")
        self.title_lbl.pack(side="left")
        self.title_lbl.bind("<ButtonPress-1>", self._title_press)
        self.title_lbl.bind("<B1-Motion>", self._title_drag)
        self.title_lbl.bind("<ButtonRelease-1>", self._title_release)
        # Faint hint to the right of the title, shown ONLY before this overlay is named (and not
        # while editing) — invites the user to click and name the session. Clicking it starts the
        # rename too. Hidden the moment a name exists.
        self.title_hint = tk.Label(bar, text="Click to name this session",
                                   bg=T["bg"], fg=T["faint"], font=self.f_small, cursor="hand2")
        self.title_hint.bind("<Button-1>", lambda e: self._begin_rename())
        self._title_btn(bar, "✕", self.quit)
        self._title_btn(bar, "—", self.toggle_collapse)
        self._update_title_hint()

    def _update_title_hint(self):
        """Show the 'type to name this session' hint only before the overlay is named and while
        not editing; hide it once it has a name (or during an edit)."""
        hint = getattr(self, "title_hint", None)
        if hint is None:
            return
        show = not (self.overlay_name or "").strip() and getattr(self, "_rename_entry", None) is None
        try:
            if show and not hint.winfo_ismapped():
                hint.pack(side="left", padx=(self.px(8), 0))
            elif not show:
                hint.pack_forget()
        except Exception:
            pass

    # ── rename this overlay (click the titlebar "Claude") ──
    def _title_press(self, e):
        self._title_moved = False
        self._drag_start(e)

    def _title_drag(self, e):
        self._title_moved = True
        self._drag_move(e)

    def _title_release(self, e):
        # A click (no drag) on the title opens the inline rename editor; a drag just moved
        # the window (handled in _title_drag) and must NOT also trigger a rename.
        if not self._title_moved:
            self._begin_rename()

    def _begin_rename(self):
        if getattr(self, "_rename_entry", None) is not None:
            return                                  # already editing
        lbl = self.title_lbl
        ent = tk.Entry(self.titlebar, font=self.f_title, bg=T["field"], fg=T["text"],
                       insertbackground=T["text"], relief="flat", highlightthickness=1,
                       highlightbackground=T["border"], highlightcolor=T["accent"])
        ent.insert(0, self.overlay_name)
        ent.select_range(0, "end")
        ent.icursor("end")
        # Overlay it on the titlebar (place, so the pack layout is untouched), spanning from the
        # title text to just before the —/✕ buttons.
        x = max(self.px(40), lbl.winfo_x())
        w = max(self.px(140), self.titlebar.winfo_width() - x - self.px(78))
        ent.place(x=x, y=self.px(8), width=w, height=self.px(28))
        ent.focus_set()
        ent.bind("<Return>", lambda ev: self._commit_rename())
        ent.bind("<KP_Enter>", lambda ev: self._commit_rename())
        ent.bind("<Escape>", lambda ev: self._cancel_rename())
        ent.bind("<FocusOut>", lambda ev: self._commit_rename())
        self._rename_entry = ent
        self._update_title_hint()          # hide the hint while editing

    def _commit_rename(self):
        ent = getattr(self, "_rename_entry", None)
        if ent is None:
            return
        try:
            name = ent.get().strip()
        except Exception:
            name = ""
        self._rename_entry = None          # null FIRST so the destroy-triggered <FocusOut> no-ops
        try:
            ent.destroy()
        except Exception:
            pass
        self._apply_name(name)

    def _cancel_rename(self):
        ent = getattr(self, "_rename_entry", None)
        self._rename_entry = None
        if ent is not None:
            try:
                ent.destroy()
            except Exception:
                pass
        self._update_title_hint()          # re-show the hint if still unnamed

    def _apply_name(self, name):
        self.overlay_name = name or ""
        shown = self.overlay_name or "Claude"
        try:
            self.title_lbl.configure(text=shown)
        except Exception:
            pass
        try:
            self.root.title(shown)         # also updates the taskbar tooltip / alt-tab label
        except Exception:
            pass
        self._update_title_hint()          # named → hide hint; cleared → show it again

    def _build_chat(self):
        wrap = tk.Frame(self.root, bg=T["bg"])
        wrap.pack(fill="both", expand=True, side="top")
        self.chat_wrap = wrap
        # Custom thin scrollbar on the right edge: a draggable grey thumb that also shows where
        # you are in the transcript. Drawn on a Canvas to match the app's look, and (crucially)
        # it's a wheel-independent way to scroll — useful because hovering an embedded widget can
        # still swallow the mouse wheel.
        self._sb_w = self.px(11)
        self._sb_first, self._sb_last = 0.0, 1.0
        self._sb_drag = None
        self._sb_hover = False
        self.scrollbar = tk.Canvas(wrap, width=self._sb_w, bg=T["bg"], highlightthickness=0,
                                   cursor="arrow", takefocus=0)
        # inset by the resize-edge thickness (px 6) so the right-edge resize strip (which is
        # lifted on top) doesn't sit over the bar and steal its drag.
        self.scrollbar.pack(side="right", fill="y", padx=(0, self.px(6)))
        self.scrollbar.bind("<ButtonPress-1>", self._sb_press)
        self.scrollbar.bind("<B1-Motion>", self._sb_motion)
        self.scrollbar.bind("<ButtonRelease-1>", lambda e: (setattr(self, "_sb_drag", None), self._sb_redraw()))
        self.scrollbar.bind("<Configure>", lambda e: self._sb_redraw())
        self.scrollbar.bind("<MouseWheel>", self._fwd_wheel)
        self.scrollbar.bind("<Enter>", lambda e: (setattr(self, "_sb_hover", True), self._sb_redraw()))
        self.scrollbar.bind("<Leave>", lambda e: (setattr(self, "_sb_hover", False), self._sb_redraw()))
        self.chat = tk.Text(
            wrap, bg=T["bg"], fg=T["text"], bd=0, padx=self.px(18), pady=self.px(12),
            wrap="word", font=self.f_body, highlightthickness=0, cursor="arrow",
            width=1, height=1, selectbackground=T["sel"], selectforeground=T["text"],
            spacing1=self.px(2), spacing3=self.px(3),
        )
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.configure(yscrollcommand=self._sb_set)
        self.chat.bind("<MouseWheel>", self._on_wheel)
        self.chat.bind("<Key>", self._readonly_keys)
        self.chat.bind("<Configure>", self._on_chat_configure, add="+")

        m = self.f_body.measure("0") * 5
        self.chat.tag_configure("uh", foreground=T["muted"], font=self.f_chip,
                                spacing1=self.px(12), lmargin1=m, lmargin2=m, justify="right")
        self.chat.tag_configure("user", background=T["user_card"], foreground=T["text"],
                                lmargin1=m, lmargin2=m, rmargin=self.px(2),
                                spacing1=self.px(6), spacing3=self.px(8))
        self.chat.tag_configure("ah", foreground=T["accent"], font=self.f_chip,
                                spacing1=self.px(16), spacing3=self.px(2))
        self.chat.tag_configure("a", foreground=T["text"], spacing2=self.px(2))
        self.chat.tag_configure("tool", foreground=T["muted"], font=self.f_mono,
                                background=T["tool_bg"], lmargin1=self.px(18), lmargin2=self.px(30),
                                spacing1=self.px(4), spacing3=self.px(4), rmargin=self.px(14))
        self.chat.tag_configure("sys", foreground=T["faint"], font=self.f_small,
                                spacing1=self.px(6), spacing3=self.px(4))
        self.chat.tag_configure("err", foreground=T["err"], font=self.f_small,
                                spacing1=self.px(6), spacing3=self.px(4))
        # extended-thinking: a muted "✻ thinking" label + faint italic body, indented so it
        # reads as a side-channel before the answer (mirrors how the CLI streams thinking, so
        # the long pre-answer wait isn't a dead, frozen-looking screen).
        self.chat.tag_configure("think_label", foreground=T["faint"], font=self.f_chip,
                                lmargin1=self.px(18), spacing1=self.px(8), spacing3=self.px(2))
        self.chat.tag_configure("think", foreground=T["faint"], font=self.f_think,
                                lmargin1=self.px(18), lmargin2=self.px(18), rmargin=self.px(14),
                                spacing2=self.px(1))
        # Markdown styling for the answer. These tags layer ON TOP of "a" (which owns the
        # answer's foreground colour + paragraph spacing) and are created AFTER it, so on the
        # font/background options they conflict on, the md_* tag wins while "a" still supplies
        # the colour — i.e. a range tagged ("a", "md_b") keeps the answer colour but renders bold.
        code_bg = T["tool_bg"]
        self.chat.tag_configure("md_b", font=self.f_bold)
        self.chat.tag_configure("md_i", font=self.f_ital)
        self.chat.tag_configure("md_code", font=self.f_code, background=code_bg)
        self.chat.tag_configure("md_h1", font=self.f_h1, spacing1=self.px(14), spacing3=self.px(5))
        self.chat.tag_configure("md_h2", font=self.f_h2, spacing1=self.px(11), spacing3=self.px(4))
        self.chat.tag_configure("md_h3", font=self.f_h3, spacing1=self.px(9), spacing3=self.px(3))
        self.chat.tag_configure("md_bullet", lmargin1=self.px(20), lmargin2=self.px(36))
        self.chat.tag_configure("md_quote", font=self.f_ital, foreground=T["muted"],
                                lmargin1=self.px(20), lmargin2=self.px(20))
        self.chat.tag_configure("md_codeblock", font=self.f_code, background=code_bg,
                                lmargin1=self.px(20), lmargin2=self.px(20), rmargin=self.px(14),
                                spacing1=self.px(1), spacing3=self.px(1))

    # ── custom scrollbar (right edge of the chat) ──
    def _sb_set(self, first, last):
        """Tk's Text calls this (yscrollcommand) whenever the view changes — scrolling, a new
        reply streaming in, see('end'), resize. Store the visible fraction and redraw the thumb,
        so it always tracks the real position."""
        try:
            self._sb_first, self._sb_last = float(first), float(last)
        except Exception:
            self._sb_first, self._sb_last = 0.0, 1.0
        self._sb_redraw()

    def _sb_geom(self):
        """Return (height, thumb_top_px, thumb_bottom_px) honouring a minimum thumb size, or
        None if the bar isn't laid out yet / the content fits (nothing to scroll)."""
        h = self.scrollbar.winfo_height()
        if h <= 1:
            return None
        if self._sb_first <= 0.0 and self._sb_last >= 1.0:
            return None                                   # everything fits → no thumb
        y0, y1 = self._sb_first * h, self._sb_last * h
        minh = self.px(28)
        if y1 - y0 < minh:                                # keep the thumb grabbable
            mid = max(minh / 2, min((y0 + y1) / 2, h - minh / 2))
            y0, y1 = mid - minh / 2, mid + minh / 2
        return h, y0, y1

    def _sb_redraw(self):
        cv = self.scrollbar
        cv.delete("all")
        g = self._sb_geom()
        if not g:
            return
        h, y0, y1 = g
        w = self._sb_w
        pad = self.px(3)
        col = T["muted"] if self._sb_hover or self._sb_drag is not None else T["faint"]
        round_rect(cv, pad, y0 + pad, w - pad, y1 - pad, (w - 2 * pad) / 2, fill=col, outline="")

    def _sb_press(self, e):
        g = self._sb_geom()
        if not g:
            return
        h, y0, y1 = g
        if y0 <= e.y <= y1:
            self._sb_drag = e.y - y0                       # grab offset within the thumb
        else:                                              # clicked the track → jump there
            self._sb_drag = (y1 - y0) / 2
            self.chat.yview_moveto(max(0.0, min(1.0, (e.y - self._sb_drag) / h)))
        self._sb_redraw()

    def _sb_motion(self, e):
        if self._sb_drag is None:
            return
        h = self.scrollbar.winfo_height()
        if h <= 1:
            return
        self.chat.yview_moveto(max(0.0, min(1.0, (e.y - self._sb_drag) / h)))

    def _build_input(self):
        wrap = tk.Frame(self.root, bg=T["bg"])
        wrap.pack(fill="x", side="bottom")
        self.input_wrap = wrap
        self.in_h = self.px(62)
        self.canvas = tk.Canvas(wrap, bg=T["bg"], height=self.in_h, highlightthickness=0)
        self.canvas.pack(fill="x", padx=self.px(12), pady=self.px(2))
        self.entry = tk.Text(self.canvas, bg=T["field"], fg=T["text"], bd=0, height=2,
                             wrap="word", font=self.f_body, insertbackground=T["accent"],
                             highlightthickness=0, padx=0, pady=0)
        self.entry_win = self.canvas.create_window(0, 0, window=self.entry, anchor="nw")
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<KP_Enter>", self._on_return)
        self.entry.bind("<Control-v>", self._on_paste)
        self.entry.bind("<Control-V>", self._on_paste)
        self.entry.bind("<Shift-Insert>", self._on_paste)
        self.entry.bind("<FocusIn>", self._ph_out)
        self.entry.bind("<FocusOut>", self._ph_in)
        self.entry.bind("<FocusIn>", self._precapture_soon, add="+")
        self.entry.bind("<KeyRelease>", self._precapture_soon, add="+")
        self._ph_active = False
        self._ph_in()
        self.canvas.bind("<Configure>", self._layout_input)

    def _build_statusbar(self):
        st = tk.Frame(self.root, bg=T["bg"])
        st.pack(fill="x", side="bottom")
        self.status_frame = st
        pad = self.px(4)
        self.toggle_screen = tk.Label(st, bg=T["bg"], font=self.f_small, cursor="hand2")
        self.toggle_screen.pack(side="left", padx=(self.px(16), self.px(2)), pady=pad)
        self.toggle_screen.bind("<Button-1>", lambda e: self.toggle_auto())
        self._paint_screen_toggle()
        self._chip(st, "Compact", self.compact_now)
        self._chip(st, "Clear", self.reset)
        # The Window-only / Shareable / Read-only toggles used to sit inline here, which
        # crowded the bar. They now live behind a single ⚙ settings menu (see _gear_menu).
        # The gear turns the accent color while Read-only is ON, so that safety state stays
        # visible at a glance without opening the menu.
        self.gear = tk.Label(st, text="⚙", bg=T["bg"], font=self.f_small, cursor="hand2")
        self.gear.pack(side="left", padx=(self.px(10), self.px(2)), pady=pad)
        self.gear.bind("<Button-1>", self._gear_menu)
        self.gear.bind("<Enter>", lambda e: self.gear.configure(fg=T["accent"]))
        self.gear.bind("<Leave>", lambda e: self._paint_gear())
        self._paint_gear()
        self.attach_lbl = tk.Label(st, text="", bg=T["bg"], fg=T["accent"],
                                   font=self.f_small, cursor="hand2")
        self.attach_lbl.pack(side="left", padx=self.px(6), pady=pad)
        self.attach_lbl.bind("<Button-1>", lambda e: self._clear_attachments())
        self.grip = tk.Label(st, text="◢", bg=T["bg"], fg=T["faint"], font=self.f_small,
                             cursor="size_nw_se")
        self.grip.pack(side="right", padx=(0, self.px(8)), pady=pad)
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)

    def _build_statusline(self):
        sl = tk.Frame(self.root, bg=T["bg"])
        sl.pack(fill="x", side="bottom")
        self.statusline_frame = sl
        self.statusline = tk.Label(sl, text="connecting…", bg=T["bg"], fg=T["faint"],
                                   font=self.f_small, anchor="w", cursor="hand2")
        self.statusline.pack(side="left", padx=(self.px(16), self.px(6)), pady=(0, self.px(6)))
        self.statusline.bind("<Button-1>", self._model_menu)
        self.busy_lbl = tk.Label(sl, text="", bg=T["bg"], fg=T["accent"],
                                 font=self.f_small, anchor="e")
        self.busy_lbl.pack(side="right", padx=(0, self.px(16)), pady=(0, self.px(6)))

    def _build_orb(self):
        s = self.px(ORB_SIZE)
        self.orb_size = s
        self.orb = tk.Canvas(self.root, width=s, height=s, bg=T["bg"],
                             highlightthickness=0, cursor="hand2")
        self._draw_orb()
        self.orb.bind("<ButtonPress-1>", self._orb_press)
        self.orb.bind("<B1-Motion>", self._orb_drag)
        self.orb.bind("<ButtonRelease-1>", self._orb_release)
        self.orb.bind("<Enter>", lambda e: self._draw_orb(hover=True))
        self.orb.bind("<Leave>", lambda e: self._draw_orb(hover=False))
        # Name pill shown UNDER the orb while collapsed (only when this overlay has a custom
        # name). The whole collapsed cluster (orb + pill) shares the orb's click=expand /
        # drag=move handlers, so clicking the name expands too. Hidden until collapse.
        self.orb_name = tk.Canvas(self.root, highlightthickness=0, bg=T["accent"], cursor="hand2")
        self.orb_name.bind("<ButtonPress-1>", self._orb_press)
        self.orb_name.bind("<B1-Motion>", self._orb_drag)
        self.orb_name.bind("<ButtonRelease-1>", self._orb_release)

    def _pill_ttf(self, px_size):
        """A PIL TrueType font for the collapsed name (PIL needs an actual font FILE to render
        CJK). Prefer Traditional-Chinese-capable faces, fall back to Latin. Cached per size."""
        cache = self.__dict__.setdefault("_pill_ttf_cache", {})
        if px_size in cache:
            return cache[px_size]
        from PIL import ImageFont
        font = None
        for path in (r"C:\Windows\Fonts\msjhbd.ttc",   # Microsoft JhengHei Bold (TC)
                     r"C:\Windows\Fonts\msjh.ttc",     # Microsoft JhengHei
                     r"C:\Windows\Fonts\msyhbd.ttc",   # MS YaHei Bold (SC fallback)
                     r"C:\Windows\Fonts\segoeuib.ttf", # Segoe UI Bold (Latin)
                     r"C:\Windows\Fonts\arialbd.ttf"):
            try:
                font = ImageFont.truetype(path, px_size)
                break
            except Exception:
                continue
        if font is None:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
        cache[px_size] = font
        return font

    @staticmethod
    def _truncate_pil(font, text, budget_px, stroke):
        """Longest prefix of `text` whose rendered width (incl. the halo stroke) fits `budget_px`,
        with a trailing … if cut. Measured with the PIL font so it matches the actual render."""
        if not text or font is None:
            return text or ""
        def width(s):
            try:
                box = font.getbbox(s, stroke_width=stroke)
            except TypeError:
                box = font.getbbox(s)
            return box[2] - box[0]
        if width(text) <= budget_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if width(text[:mid] + "…") <= budget_px:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo] + "…") if lo > 0 else "…"

    def _draw_name_pill(self):
        """Render the collapsed name as BLACK text with a WHITE halo that hugs the glyph shapes —
        no box, no border. Done by drawing the text with a thick white PIL stroke; the white sits
        only around the letters, and the window region (built from this image's alpha in
        _build_collapsed_mask) clips the window to that halo silhouette, so it floats like a
        captioned label rather than a rectangle. Returns (w, h) in logical px."""
        name = (self.overlay_name or "").strip()
        SS = 4                                   # supersample for crisp edges at any DPI
        fpx = self.px(14) * SS                   # caption font size
        grow = max(2 * SS, self.px(3) * SS)      # white halo thickness (supersampled)
        font = self._pill_ttf(fpx)
        shown = self._truncate_pil(font, name, self.px(230) * SS, grow) or " "

        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))
        try:
            l, t, r, b = probe.textbbox((0, 0), shown, font=font, stroke_width=grow)
            has_stroke = True
        except TypeError:                        # very old Pillow: no stroke param
            l, t, r, b = probe.textbbox((0, 0), shown, font=font)
            has_stroke = False
        pad = grow + SS                          # margin so anti-aliased halo isn't clipped
        Wn, Hn = (r - l) + 2 * pad, (b - t) + 2 * pad
        img = Image.new("RGBA", (max(1, Wn), max(1, Hn)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ox, oy = pad - l, pad - t
        white, black = (255, 255, 255, 255), (0, 0, 0, 255)
        if has_stroke:
            d.text((ox, oy), shown, font=font, fill=black, stroke_width=grow, stroke_fill=white)
        else:                                    # emulate the halo: white at offsets, black centre
            g = max(1, grow)
            for dx in range(-g, g + 1):
                for dy in range(-g, g + 1):
                    if dx * dx + dy * dy <= g * g:
                        d.text((ox + dx, oy + dy), shown, font=font, fill=white)
            d.text((ox, oy), shown, font=font, fill=black)

        lw, lh = max(1, round(Wn / SS)), max(1, round(Hn / SS))
        out = img.resize((lw, lh), Image.LANCZOS)
        self._orb_name_photo = ImageTk.PhotoImage(out)   # keep a ref so Tk won't GC it
        c = self.orb_name
        c.delete("all")
        c.configure(width=lw, height=lh, bg="#FFFFFF")   # bg = white → any AA edge blends into halo
        c.create_image(lw // 2, lh // 2, image=self._orb_name_photo)
        self._name_label_mask = out.split()[3]           # alpha silhouette → the window region
        self._name_pill_size = (lw, lh)
        return lw, lh

    def _draw_orb(self, hover=False):
        s = self.orb_size
        self.orb.delete("all")
        self._orb_photo = self._orb_image(s, hover)   # keep a ref so Tk won't GC it
        self.orb.create_image(s // 2, s // 2, image=self._orb_photo)
        if getattr(self, "_task_done_badge", False):
            self._draw_orb_badge()

    def _badge_geom(self, x_off=0):
        """(centre_x, centre_y, radius) of the done-badge, relative to a collapsed-window origin
        with the orb's left edge at x_off (0 when drawing on the orb canvas itself). Tucked high in
        the orb's top-right corner so it sits on the sprite's body but clears its eyes; sized to fit
        inside the s x s orb box (centre_y >= radius keeps the top from clipping the window edge)."""
        s = self.orb_size
        br = max(self.px(5), int(s * 0.15))
        return x_off + int(s * 0.78), int(s * 0.16), br

    def _draw_orb_badge(self):
        """A small green check at the orb's top-right — signals the last reply finished while the
        overlay was collapsed. Drawn on the orb canvas; the clip region (rebuilt in
        _rebuild_collapsed_mask) includes this circle so the floating sprite doesn't clip it away."""
        bx, by, br = self._badge_geom(0)
        self.orb.create_oval(bx - br, by - br, bx + br, by + br,
                             fill="#3FB950", outline="#FFFFFF", width=max(1, self.px(1.5)))
        w = max(2, self.px(2))
        self.orb.create_line(bx - br * 0.42, by + br * 0.04, bx - br * 0.08, by + br * 0.40,
                             bx + br * 0.46, by - br * 0.42,
                             fill="#FFFFFF", width=w, capstyle="round", joinstyle="round")

    # ── glossy 3-D orb (rendered with Pillow, cached per size+state) ──
    @staticmethod
    def _rgb(hex_):
        # tolerate a malformed THEMES hex (empty / short / non-hex) rather than crashing
        # at startup before any guard exists; fall back to a neutral grey.
        h = (hex_ or "").lstrip("#")
        try:
            if len(h) < 6:
                raise ValueError(h)
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return (128, 128, 128)

    @staticmethod
    def _mix(a, b, t):
        t = 0.0 if not (t == t) else t   # NaN guard (NaN fails all comparisons)
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

    def _orb_image_from_file(self, s, hover):
        """Render the collapsed orb from ORB_IMAGE instead of the procedural sphere.
        The artwork is auto-scaled so its whole opaque silhouette fits inside the
        circular orb (the collapsed window is clipped to a circle), then centred.
        Supersampled ×4 + LANCZOS for crisp edges at any DPI. Returns a PhotoImage,
        or None if the file is missing/unreadable (caller falls back to the sphere)."""
        import math
        try:
            from PIL import ImageEnhance
            p = Path(ORB_IMAGE)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent / p
            if not p.exists():
                return None
            art = Image.open(p).convert("RGBA")
        except Exception:
            return None

        SS = 4
        n = s * SS
        try:
            alpha = art.split()[3]
            bbox = alpha.getbbox() or (0, 0, art.width, art.height)
        except Exception:
            bbox = (0, 0, art.width, art.height)

        if ORB_FLOAT:
            # Floating sprite: the window is clipped to the artwork's own silhouette, so no
            # circle to fit inside — scale the opaque content to fill the orb box (minus a hair).
            bw, bh = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
            scale = (n * (1.0 - max(0.0, min(0.4, ORB_IMAGE_MARGIN)))) / max(bw, bh)
        else:
            # Circular orb: fit the farthest opaque pixel just inside the circle so nothing clips.
            cx, cy = art.width / 2.0, art.height / 2.0
            corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]),
                       (bbox[0], bbox[3]), (bbox[2], bbox[3])]
            opaque_r = max(math.hypot(x - cx, y - cy) for x, y in corners) or max(cx, cy)
            scale = ((n / 2.0) * (1.0 - max(0.0, min(0.4, ORB_IMAGE_MARGIN)))) / opaque_r

        nw, nh = max(1, int(round(art.width * scale))), max(1, int(round(art.height * scale)))
        art = art.resize((nw, nh), Image.LANCZOS)

        if hover:                                   # gentle lift on hover
            art = ImageEnhance.Brightness(art).enhance(1.08)

        canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        canvas.alpha_composite(art, ((n - nw) // 2, (n - nh) // 2))
        out = canvas.resize((s, s), Image.LANCZOS)
        # Stash the alpha at window size so _apply_region can clip the window to the sprite.
        self._orb_mask = out.split()[3]
        self._orb_mask_size = (s, s)
        return ImageTk.PhotoImage(out)

    def _orb_image(self, s, hover):
        """Render a glossy terracotta sphere: off-centre radial gradient (volume),
        a soft top-left specular highlight, a darker bottom rim + lighter top rim
        (bevel), then the cream Claude spark with a faint drop shadow. Supersampled
        ×4 then LANCZOS-downscaled for crisp edges at any DPI. Cached per (size,hover)."""
        import math
        key = (s, hover)
        if key in self._orb_imgs:
            return self._orb_imgs[key]

        # Custom artwork path: load ORB_IMAGE, auto-fit it inside the circular orb.
        if ORB_IMAGE:
            photo = self._orb_image_from_file(s, hover)
            if photo is not None:
                self._orb_imgs[key] = photo
                return photo
            # fall through to the procedural orb if the file is missing/unreadable

        SS = 4
        n = s * SS
        base = self._rgb(T["accent"])
        WHITE, BLACK = (255, 255, 255), (0, 0, 0)
        light = self._mix(base, WHITE, 0.55 if hover else 0.42)   # gradient core
        edge = self._mix(base, BLACK, 0.40)                        # gradient rim
        inset = SS                                                 # ~1 logical px border

        # sphere alpha mask (anti-aliased via the supersample)
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).ellipse([inset, inset, n - inset - 1, n - inset - 1], fill=255)

        def ramp(t):
            return self._mix(light, base, t / 0.5) if t < 0.5 else self._mix(base, edge, (t - 0.5) / 0.5)

        # directional gradient → concentric rings centred OUTSIDE the top-left, so the
        # top-left rim is the brightest (lit edge) and shading only deepens toward the
        # bottom-right. A light centre *inside* the disc would re-darken the top-left rim
        # and read as an unwanted shadow there.
        grad = Image.new("RGB", (n, n), edge)
        gd = ImageDraw.Draw(grad)
        lx, ly = int(n * -0.12), int(n * -0.15)
        maxr = int(math.hypot(max(abs(lx), abs(n - lx)), max(abs(ly), abs(n - ly)))) + 2
        for r in range(maxr, 0, -1):
            gd.ellipse([lx - r, ly - r, lx + r, ly + r], fill=ramp(r / maxr))
        orb = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        orb.paste(grad, (0, 0), mask)

        # bevel: lighter top rim, darker bottom rim
        rim = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        rd = ImageDraw.Draw(rim)
        bb = [inset, inset, n - inset - 1, n - inset - 1]
        rd.arc(bb, 180, 360, fill=self._mix(light, WHITE, 0.4) + (170,), width=max(2, SS))
        rd.arc(bb, 0, 180, fill=edge + (150,), width=max(2, SS))
        rim = rim.filter(ImageFilter.GaussianBlur(SS * 0.6))
        rim.putalpha(ImageChops.multiply(rim.split()[3], mask))
        orb = Image.alpha_composite(orb, rim)

        # soft specular highlight near the top-left
        hl = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        hw, hh = n * 0.46, n * 0.32
        hcx, hcy = n * 0.37, n * 0.27
        ImageDraw.Draw(hl).ellipse([hcx - hw / 2, hcy - hh / 2, hcx + hw / 2, hcy + hh / 2],
                                   fill=(255, 255, 255, 150 if hover else 120))
        hl = hl.filter(ImageFilter.GaussianBlur(n * 0.05))
        hl.putalpha(ImageChops.multiply(hl.split()[3], mask))
        orb = Image.alpha_composite(orb, hl)

        # Claude spark (cream sunburst) with a faint drop shadow for depth
        cx = cy = n / 2
        R = n * 0.24
        spokes = []
        for i in range(12):
            a = math.pi * i / 6
            r1 = R if i % 2 == 0 else R * 0.46
            spokes.append((cx, cy, cx + r1 * math.cos(a), cy + r1 * math.sin(a)))
        wln = max(2, int(SS * 1.6))
        dot = max(2, int(SS * 1.7))

        sh = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        off = SS
        for x0, y0, x1, y1 in spokes:
            sd.line([x0, y0 + off, x1, y1 + off], fill=(60, 24, 12, 110), width=wln)
        sh = sh.filter(ImageFilter.GaussianBlur(SS * 0.8))
        sh.putalpha(ImageChops.multiply(sh.split()[3], mask))
        orb = Image.alpha_composite(orb, sh)

        sp = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        spd = ImageDraw.Draw(sp)
        cream = (255, 252, 246, 255)
        for x0, y0, x1, y1 in spokes:
            spd.line([x0, y0, x1, y1], fill=cream, width=wln)
            for (ex, ey) in ((x0, y0), (x1, y1)):           # round the spoke ends
                spd.ellipse([ex - wln / 2, ey - wln / 2, ex + wln / 2, ey + wln / 2], fill=cream)
        spd.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=cream)
        orb = Image.alpha_composite(orb, sp)

        out = orb.resize((s, s), Image.LANCZOS)
        photo = ImageTk.PhotoImage(out)
        self._orb_imgs[key] = photo
        return photo

    def _orb_press(self, e):
        self._orb_moved = False
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _orb_drag(self, e):
        self._orb_moved = True
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _orb_release(self, e):
        if not self._orb_moved:
            self.toggle_collapse()   # click the bubble → expand

    # ── small widgets ──
    def _draw_spark(self, c, cx, cy, r):
        import math
        for i in range(12):
            a = math.pi * i / 6
            r1 = r if i % 2 == 0 else r * 0.5
            c.create_line(cx, cy, cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                          fill=T["accent"], width=max(2, self.px(2)), capstyle="round")
        d = max(2, self.px(2))
        c.create_oval(cx - d, cy - d, cx + d, cy + d, fill=T["accent"], outline="")

    def _title_btn(self, parent, text, cmd):
        b = tk.Label(parent, text=text, bg=T["bg"], fg=T["muted"], font=self.f_small,
                     cursor="hand2", width=3)
        b.pack(side="right", padx=(0, self.px(6)))
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=T["hover"], fg=T["text"]))
        b.bind("<Leave>", lambda e: b.configure(bg=T["bg"], fg=T["muted"]))
        return b

    def _chip(self, parent, text, cmd):
        b = tk.Label(parent, text=text, bg=T["bg"], fg=T["muted"], font=self.f_small, cursor="hand2")
        b.pack(side="left", padx=self.px(8), pady=self.px(4))
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(fg=T["accent"]))
        b.bind("<Leave>", lambda e: b.configure(fg=T["muted"]))
        return b

    def _paint_screen_toggle(self):
        on = self.auto_shot
        self.toggle_screen.configure(text=("◉  Auto-shot" if on else "○  Auto-shot"),
                                     fg=(T["accent"] if on else T["muted"]))

    def _paint_gear(self):
        # The ⚙ settings menu holds Window-only / Shareable / Read-only. It carries no
        # per-toggle text on the bar; instead it turns the accent color while Read-only is
        # ON, so that safety lock stays visible without opening the menu. Guarded so the
        # paint helpers below are safe to call before the gear exists / in headless tests.
        if not hasattr(self, "gear"):
            return
        self.gear.configure(fg=(T["accent"] if self.read_only else T["muted"]))

    # Window-only / Shareable / Read-only moved into the ⚙ menu; their state is shown by
    # the checkmarks in _gear_items and (for Read-only) the gear color. These three keep
    # their old names so every existing caller (the toggle handlers, _apply_permission_mode)
    # just refreshes the gear.
    def _paint_window_toggle(self):
        self._paint_gear()

    def _paint_share_toggle(self):
        self._paint_gear()

    def _paint_ro_toggle(self):
        self._paint_gear()

    def _gear_items(self):
        """The (label, command) rows of the ⚙ settings menu. A ✓ prefixes each setting
        that is currently ON. Split out from _gear_menu so it's unit-testable without
        popping a real Tk menu. Read-only reflects the CONFIRMED state (it flips only after
        the worker confirms), so the checkmark never claims a lock that isn't live."""
        def row(on, name):
            return ("✓  " + name) if on else ("      " + name)
        return [
            (row(self.window_shot, "Window-only"), self.toggle_window_shot),
            (row(self.share_visible, "Shareable"), self.toggle_screen_share),
            (row(self.read_only, "Read-only"), self.toggle_read_only),
        ]

    def _gear_menu(self, e):
        # Same popup pattern as the model switcher (_model_menu): build fresh on each open
        # so the checkmarks reflect current state, then tk_popup at the click point.
        m = tk.Menu(self.root, tearoff=0, bg=T["field"], fg=T["text"],
                    activebackground=T["accent"], activeforeground=T["on_accent"], bd=0)
        for lbl, cmd in self._gear_items():
            m.add_command(label=lbl, command=cmd)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # ── rounded input layout ──
    def _layout_input(self, e=None):
        c = self.canvas
        w = c.winfo_width()
        if w < self.px(110):     # below this there isn't room for entry + send button; a tiny
            return               # transient width (construction/DPI/pack) would size them negative
        h, pad = self.in_h, self.px(5)
        c.delete("box")
        round_rect(c, pad, pad, w - pad, h - pad, self.px(15), fill=T["field"],
                   outline=T["border"], width=1, tags="box")
        c.tag_lower("box")
        rad = self.px(15)
        bx, by = w - pad - self.px(38), h / 2
        ex1, ey1 = pad + self.px(14), pad + self.px(8)
        c.coords(self.entry_win, ex1, ey1)
        c.itemconfigure(self.entry_win, width=max(self.px(40), bx - rad - self.px(8) - ex1),
                        height=max(self.px(20), h - 2 * pad - self.px(14)))
        c.delete("send")
        # Pillow-rendered (supersampled, anti-aliased) button — Tk's create_oval is aliased
        # and looked low-res. Centred PhotoImage; state (idle/hover/busy) swaps the cached image.
        self._send_d = 2 * rad
        self._send_item = c.create_image(bx, by, image=self._send_img(self._send_d, self._send_state()),
                                         tags=("send",))
        c.tag_bind("send", "<Button-1>", lambda ev: self._send_or_stop())
        c.tag_bind("send", "<Enter>", lambda ev: self._on_send_hover(True))
        c.tag_bind("send", "<Leave>", lambda ev: self._on_send_hover(False))

    def _send_state(self):
        return ("busy" if self.busy else "idle") + ("_hover" if self._send_hover else "")

    def _on_send_hover(self, hovering):
        self._send_hover = hovering
        self._paint_send()

    def _paint_send(self):
        item = getattr(self, "_send_item", None)
        d = getattr(self, "_send_d", None)
        if item is None or not d:
            return
        try:
            self.canvas.itemconfigure(item, image=self._send_img(d, self._send_state()))
        except Exception:
            pass

    def _send_img(self, d, state):
        """Render the round send/stop button with Pillow (×4 supersample + LANCZOS) so the
        circle is smoothly anti-aliased and the glyph is a crisp vector, not a font character.
        Cached per (diameter, state). state ∈ {idle, idle_hover, busy, busy_hover}."""
        key = (d, state)
        if key in self._send_imgs:
            return self._send_imgs[key]
        busy = state.startswith("busy")
        hover = state.endswith("hover")
        base = self._rgb(T["err"] if busy else T["accent"])
        if hover:
            base = self._mix(base, (255, 255, 255), 0.12) if busy else self._rgb(T["accent_hi"])
        fg = self._rgb(T["on_accent"])

        SS = 4
        n = max(4, d * SS)
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse([0, 0, n - 1, n - 1], fill=base + (255,))
        if busy:                                   # rounded "stop" square
            sq = n * 0.30
            o = (n - sq) / 2
            dr.rounded_rectangle([o, o, o + sq, o + sq], radius=n * 0.055, fill=fg + (255,))
        else:                                      # upward "send" arrow (stem + chevron)
            cx = n / 2
            topy, boty = n * 0.31, n * 0.71
            wln = max(2, int(round(n * 0.11)))
            hw = n * 0.18
            r = wln / 2
            dr.line([cx, boty, cx, topy], fill=fg + (255,), width=wln)
            dr.line([cx - hw, topy + hw, cx, topy], fill=fg + (255,), width=wln)
            dr.line([cx + hw, topy + hw, cx, topy], fill=fg + (255,), width=wln)
            for (ex, ey) in ((cx, topy), (cx, boty), (cx - hw, topy + hw), (cx + hw, topy + hw)):
                dr.ellipse([ex - r, ey - r, ex + r, ey + r], fill=fg + (255,))   # round the caps
        out = img.resize((d, d), Image.LANCZOS)
        photo = ImageTk.PhotoImage(out)
        self._send_imgs[key] = photo
        return photo

    def _refresh_send(self):
        self._paint_send()

    # ── placeholder ──
    def _ph_in(self, e=None):
        if not self.entry.get("1.0", "end").strip():
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", PLACEHOLDER)
            self.entry.configure(fg=T["faint"])
            self._ph_active = True

    def _ph_out(self, e=None):
        if self._ph_active:
            self.entry.delete("1.0", "end")
            self.entry.configure(fg=T["text"])
            self._ph_active = False

    def _entry_text(self):
        return "" if self._ph_active else self.entry.get("1.0", "end").strip()

    def _clipboard_has_image(self):
        """Cheap, non-blocking probe (no OLE render): should this paste be treated as an image?
        TEXT WINS: if the clipboard carries text, paste it as text — many apps (browsers, Office,
        screenshot tools) ALSO put a bitmap/DIB on the clipboard next to copied text, which used to
        make a plain text copy paste as an image. Only treat it as image when there's image/file
        content AND no text."""
        try:
            if any(_user32.IsClipboardFormatAvailable(f) for f in (CF_UNICODETEXT, CF_TEXT)):
                return False
            return any(_user32.IsClipboardFormatAvailable(f)
                       for f in (CF_DIB, CF_DIBV5, CF_BITMAP, CF_HDROP))
        except Exception:
            return False

    def _on_paste(self, e):
        """Ctrl+V: if the clipboard holds an image (or image files), attach it. Everything
        slow — the grabclipboard() OLE read AND the decode/downscale/save — runs on a
        background thread, so a wedged clipboard owner / cloud-placeholder / huge file can't
        freeze the Tk thread. Results return via ui_q as ("attach", …)."""
        if not self._clipboard_has_image():
            return None             # plain text → let the normal paste happen
        if self._paste_busy:
            return "break"          # one paste at a time — don't fan out unbounded threads
        self._paste_busy = True
        threading.Thread(target=self._paste_clipboard_bg, daemon=True).start()
        return "break"              # don't paste image bytes as garbage text

    def _paste_clipboard_bg(self):
        """Background side of paste: do the slow clipboard read + stash off the Tk thread.
        Always ends by posting ("attach", …) so _paste_busy is cleared even on failure."""
        srcs = []
        try:
            data = ImageGrab.grabclipboard()
            if isinstance(data, Image.Image):
                srcs.append(data)
            elif isinstance(data, list):
                seen = set()
                for f in data:
                    s = str(f)
                    if s.lower().endswith(IMAGE_EXTS) and s not in seen:
                        seen.add(s)
                        srcs.append(s)
                        if len(srcs) >= MAX_PASTE_SOURCES:   # bound a hostile file-list
                            break
        except Exception:
            srcs = []
        self._stash_images_bg(srcs)

    def _stash_images_bg(self, srcs):
        """Stash each source, then hand the saved paths back to the UI thread. _stash_image
        touches no Tk, so this is safe off-thread. Always posts ("attach", …)."""
        out, failed = [], 0
        try:
            for s in srcs:
                p = self._stash_image(s)
                if p:
                    out.append(p)
                else:
                    failed += 1    # never fall back to the original path — a file we
                                   # couldn't open/downscale must not be inlined as-is
        except BaseException:
            pass
        finally:
            self.ui_q.put(("attach", (out, failed)))

    def _stash_image(self, src):
        """Save a clipboard image (or a copy of a pasted image file) into SHOT_DIR,
        downscaled to SHOT_MAX_EDGE so a pasted 4K/8K image can't blow past the stream
        buffer (capture() already does this for screenshots; paste used not to).
        Returns the saved path, or None on failure so the caller can fall back."""
        opened = not isinstance(src, Image.Image)
        try:
            img = src if isinstance(src, Image.Image) else Image.open(src)
        except Exception:
            return None
        try:
            # Reject by pixel count BEFORE thumbnail/decode. img.size comes from the header
            # without decoding, so this stops a "decompression bomb" (a tiny file that decodes
            # to a giant bitmap) from blowing up memory in thumbnail() — Pillow only *warns*
            # below ~178M px, it doesn't raise.
            w, h = img.size
            if w <= 0 or h <= 0 or (w * h) > MAX_PASTE_PIXELS:
                return None
            if SHOT_MAX_EDGE and max(w, h) > SHOT_MAX_EDGE:
                img.thumbnail((SHOT_MAX_EDGE, SHOT_MAX_EDGE), Image.LANCZOS)
            p = SHOT_DIR / f"shot_{int(time.time() * 1000)}_paste.png"
            try:
                img.save(p)
            except Exception:
                img.convert("RGB").save(p)
            self._prune_shots()
            return str(p)
        except Exception:
            return None
        finally:
            if opened:                  # close only the handle WE opened (not a clipboard img)
                try:
                    img.close()
                except Exception:
                    pass

    def _refresh_attach(self):
        n = len(self.pending_images)
        self.attach_lbl.configure(text=(f"📎 {n} image{'s' if n != 1 else ''}  ✕" if n else ""))

    def _clear_attachments(self):
        self.pending_images = []
        self._refresh_attach()

    # ── window drag / resize / rounding ──
    def _bind_drag(self, w):
        w.bind("<ButtonPress-1>", self._drag_start)
        w.bind("<B1-Motion>", self._drag_move)
        w.bind("<Double-Button-1>", lambda e: self.toggle_collapse())

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _resize_start(self, e):
        self._resize = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, e):
        x0, y0, w0, h0 = self._resize
        self.root.geometry(f"{max(self.px(330), w0 + e.x_root - x0)}x{max(self.px(300), h0 + e.y_root - y0)}")

    # ── edge / corner resize (no native frame, so we draw our own grips) ──
    def _build_edges(self):
        B, C = self.px(6), self.px(15)        # edge thickness / corner box
        edges = [
            (dict(x=0, y=0, relwidth=1, height=B), "n", "size_ns"),
            (dict(x=0, rely=1.0, y=-B, relwidth=1, height=B), "s", "size_ns"),
            (dict(x=0, y=0, relheight=1, width=B), "w", "size_we"),
            (dict(relx=1.0, x=-B, y=0, relheight=1, width=B), "e", "size_we"),
            (dict(x=0, y=0, width=C, height=C), "nw", "size_nw_se"),
            (dict(relx=1.0, x=-C, y=0, width=C, height=C), "ne", "size_ne_sw"),
            (dict(x=0, rely=1.0, y=-C, width=C, height=C), "sw", "size_ne_sw"),
            (dict(relx=1.0, x=-C, rely=1.0, y=-C, width=C, height=C), "se", "size_nw_se"),
        ]
        self._edge_widgets = []
        for place_kw, dirs, cur in edges:
            f = tk.Frame(self.root, bg=T["bg"], cursor=cur)
            f.place(**place_kw)
            f.bind("<ButtonPress-1>", lambda e, d=dirs: self._edge_resize_start(e, d))
            f.bind("<B1-Motion>", self._edge_resize_move)
            f.lift()
            self._edge_widgets.append((f, place_kw))

    def _show_edges(self):
        for f, kw in self._edge_widgets:
            f.place(**kw)
            f.lift()

    def _hide_edges(self):
        for f, _ in self._edge_widgets:
            f.place_forget()

    def _edge_resize_start(self, e, dirs):
        self._ers = (dirs, e.x_root, e.y_root, self.root.winfo_x(),
                     self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())

    def _edge_resize_move(self, e):
        ers = getattr(self, "_ers", None)
        if not ers:
            return
        dirs, mx, my, x, y, w, h = ers
        minw, minh = self.px(330), self.px(300)
        dx, dy = e.x_root - mx, e.y_root - my
        nx, ny, nw, nh = x, y, w, h
        if "e" in dirs:
            nw = max(minw, w + dx)
        if "s" in dirs:
            nh = max(minh, h + dy)
        if "w" in dirs:
            nw = max(minw, w - dx); nx = x + (w - nw)
        if "n" in dirs:
            nh = max(minh, h - dy); ny = y + (h - nh)
        self.root.geometry(f"{nw}x{nh}+{nx}+{ny}")

    # ── text zoom (Ctrl +/- · Ctrl+0 reset · Ctrl+wheel) ──
    def _bind_zoom(self):
        for w in (self.root, self.entry, self.chat):
            for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
                w.bind(seq, lambda e: self._zoom_evt(1))
            for seq in ("<Control-minus>", "<Control-underscore>", "<Control-KP_Subtract>"):
                w.bind(seq, lambda e: self._zoom_evt(-1))
            w.bind("<Control-0>", lambda e: self._zoom_evt(0))
        self.chat.bind("<Control-MouseWheel>", lambda e: self._zoom_evt(1 if e.delta > 0 else -1))
        self.entry.bind("<Control-MouseWheel>", lambda e: self._zoom_evt(1 if e.delta > 0 else -1))

    def _zoom_evt(self, d):
        self._set_zoom(self.zoom * 1.1 if d > 0 else self.zoom / 1.1 if d < 0 else 1.0)
        return "break"

    def _set_zoom(self, z):
        self.zoom = min(2.4, max(0.7, z))
        for f, base in self._fonts:
            f.configure(size=-max(self.px(7), int(round(self.px(base) * self.zoom))))
        try:
            self._layout_input()
        except Exception:
            pass
        self._rezoom_embeds()      # redraw embedded canvases (bubbles/chips/tables/Copy) at new zoom

    @staticmethod
    def _widget_alive(w):
        try:
            return bool(w.winfo_exists())
        except Exception:
            return False

    def _register_zoomable(self, canvas, render):
        """Track an embedded canvas + its render() so Ctrl +/− can redraw it at the new zoom —
        a fixed-size canvas would otherwise stay frozen while the flowing text grows. Compact
        dead entries (pruning/reset destroys the widget) once the list grows, so it stays bounded
        even across a long session."""
        self._zoomables.append((canvas, render))
        if len(self._zoomables) > 300:
            self._zoomables = [(c, r) for (c, r) in self._zoomables if self._widget_alive(c)]

    def _rezoom_embeds(self):
        """Schedule a redraw of every live embedded canvas at the current zoom. Debounced (~20 ms)
        so a fast Ctrl+wheel spin coalesces into one pass instead of re-rendering every notch."""
        try:
            if self._rezoom_after is not None:
                self.root.after_cancel(self._rezoom_after)
        except Exception:
            pass
        try:
            self._rezoom_after = self.root.after(20, self._do_rezoom_embeds)
        except Exception:
            self._rezoom_after = None
            self._do_rezoom_embeds()

    def _do_rezoom_embeds(self):
        # Re-render only on a zoom event (never per-delta), so this can't reintroduce the v1.1.9
        # per-<Configure> freeze; cost is bounded by the capped transcript. Drop dead widgets.
        self._rezoom_after = None
        live = []
        for c, render in self._zoomables:
            if not self._widget_alive(c):
                continue
            try:
                render()
            except Exception:
                pass
            live.append((c, render))
        self._zoomables = live

    def _on_chat_configure(self, e):
        """The chat's embedded canvases (user bubbles, tables) are sized to the chat width when
        they're drawn; resizing the window narrower would otherwise leave them at their old, wider
        size — a right-aligned user bubble then slides off the right edge and its text gets clipped
        (worst for short messages, which hug the far right — hence 'sometimes'). Re-fit them to the
        new width, reusing the debounced re-render so a drag coalesces into one pass when it settles
        (no per-pixel re-render → stays off the v1.1.9 freeze path)."""
        w = e.width
        if w == getattr(self, "_last_chat_w", None):
            return                       # height-only Configure (or no change) → nothing to re-fit
        self._last_chat_w = w
        self._rezoom_embeds()

    def _on_configure(self, e):
        if e.widget is not self.root:
            return
        # The rounded/elliptic region depends ONLY on the window SIZE (and expanded state),
        # not its position. Re-applying on every <Configure> — move-only events, and the
        # redraw that SetWindowRgn(…, bRedraw=True) itself triggers — spun _apply_region in a
        # ~50 ms self-feeding loop (SetWindowRgn → repaint → <Configure> → reschedule), and
        # each pass ran update_idletasks() (a full layout flush, expensive on a big chat).
        # That intermittently starved the UI thread: scrolling froze, the reply only rendered
        # in the gaps. Only re-apply when the size actually changed; collapse/expand still
        # re-apply explicitly via their own after(_apply_region) calls.
        size = (e.width, e.height)
        if size == self._last_cfg_size:
            return
        self._last_cfg_size = size
        if self._round_after:
            self.root.after_cancel(self._round_after)
        self._round_after = self.root.after(50, self._apply_region)

    def _apply_region(self):
        try:
            self.root.update_idletasks()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            hwnd = _user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()  # GA_ROOT
            if self.expanded:
                r = self.px(CORNER_RADIUS)
                rgn = _gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
            else:
                rgn = None
                # Named-collapse: clip to the composite silhouette (orb sprite ∪ name pill) so the
                # pill floats as its own rounded tag under the free-floating orb.
                cm = getattr(self, "_collapsed_mask", None)
                if cm is not None and cm.size == (w, h):
                    rgn = self._build_alpha_region(cm)
                if rgn is None and ORB_FLOAT and getattr(self, "_orb_mask", None) is not None \
                        and self._orb_mask_size == (w, h):
                    rgn = self._build_alpha_region(self._orb_mask)   # float as the raw sprite
                if not rgn:
                    rgn = _gdi32.CreateEllipticRgn(0, 0, w + 1, h + 1)   # circular orb fallback
            # On success Windows owns the region handle; on failure WE still own it and must
            # free it, or repeated <Configure>/resize churn with a stale hwnd leaks GDI
            # handles until drawing eventually fails. SetWindowRgn returns 0 on failure.
            ok = _user32.SetWindowRgn(hwnd, rgn, True)
            if not ok and rgn:
                _gdi32.DeleteObject(rgn)
        except Exception:
            pass

    def _build_alpha_region(self, mask, thr=None):
        """Build a Win32 region matching an alpha mask's opaque silhouette: one rect per
        horizontal run of pixels at/above the threshold, OR-ed together. Lets the collapsed
        window float as the raw pixel sprite (hard binary edge — ideal for pixel art).
        Returns an HRGN owned by the caller, or None on failure."""
        try:
            thr = ORB_ALPHA_THRESHOLD if thr is None else thr
            w, h = mask.size
            px = mask.load()
            full = _gdi32.CreateRectRgn(0, 0, 0, 0)
            tmp = _gdi32.CreateRectRgn(0, 0, 0, 0)
            if not full or not tmp:
                for r in (full, tmp):
                    if r:
                        _gdi32.DeleteObject(r)
                return None
            for y in range(h):
                x = 0
                while x < w:
                    if px[x, y] >= thr:
                        x0 = x
                        while x < w and px[x, y] >= thr:
                            x += 1
                        _gdi32.SetRectRgn(tmp, x0, y, x, y + 1)
                        _gdi32.CombineRgn(full, full, tmp, 2)   # RGN_OR
                    else:
                        x += 1
            _gdi32.DeleteObject(tmp)
            return full
        except Exception:
            return None

    def _rebuild_collapsed_mask(self):
        """Compose the alpha silhouette for the collapsed window from current state: the orb sprite
        at the top, the name label's halo below it (if named), and the done-badge circle (if set).
        Fed to _build_alpha_region in _apply_region so the window floats as sprite [+ haloed name]
        [+ badge] with no surrounding box. None when neither extra is present (the plain orb
        sprite/ellipse fast path handles that). Built only on collapse / badge-or-name change — never
        in a <Configure> loop, so it stays off the v1.1.9 freeze path. None on failure too."""
        if self.expanded:
            self._collapsed_mask = None
            return
        s = self.orb_size
        named = bool((self.overlay_name or "").strip())
        badge = bool(getattr(self, "_task_done_badge", False))
        if not named and not badge:
            self._collapsed_mask = None       # fast path: plain sprite/ellipse region
            return
        try:
            if named:
                pw, ph = self._name_pill_size
                gap = self.px(5)
                W, H = max(s, pw), s + gap + ph
            else:
                pw = ph = gap = 0
                W, H = s, s
            x_orb = (W - s) // 2
            mask = Image.new("L", (W, H), 0)
            om = getattr(self, "_orb_mask", None)
            if ORB_FLOAT and om is not None and om.size == (s, s):
                mask.paste(om, (x_orb, 0))                       # the raw sprite silhouette
            else:
                ImageDraw.Draw(mask).ellipse([x_orb, 0, x_orb + s - 1, s - 1], fill=255)  # circular orb
            if named:
                x_pill, y_pill = (W - pw) // 2, s + gap
                lm = getattr(self, "_name_label_mask", None)
                if lm is not None and lm.size == (pw, ph):
                    mask.paste(lm, (x_pill, y_pill))             # the text-halo silhouette
                else:                                            # fallback: a plain filled box
                    ImageDraw.Draw(mask).rectangle(
                        [x_pill, y_pill, x_pill + pw - 1, y_pill + ph - 1], fill=255)
            if badge:
                bx, by, br = self._badge_geom(x_orb)
                ImageDraw.Draw(mask).ellipse([bx - br, by - br, bx + br, by + br], fill=255)
            self._collapsed_mask = mask
        except Exception:
            self._collapsed_mask = None

    def _set_task_badge(self, on):
        """Toggle the collapsed-orb done-badge. Redraws the orb + the clip region only when
        collapsed (the badge is invisible while expanded; the flag is just cleared)."""
        on = bool(on)
        if on == getattr(self, "_task_done_badge", False):
            return
        self._task_done_badge = on
        if not self.expanded:
            self._draw_orb()                 # draw (or remove) the badge dot
            self._rebuild_collapsed_mask()   # include/exclude the badge circle in the clip region
            self.root.after(10, self._apply_region)

    def _maybe_flag_done(self):
        """A reply just finished — flag the orb as 'done' if it actually produced text. The badge
        means "last turn complete, awaiting your next message": it PERSISTS across expand/collapse
        and is only cleared when the next turn starts (add_user) or the chat is reset. It's shown
        only while collapsed; setting it while expanded just records the state so the next collapse
        shows it."""
        if (self._turn_raw or "").strip():
            self._set_task_badge(True)

    # ── chat rendering (main thread only) ──
    def _readonly_keys(self, e):
        if (e.state & 0x4) and e.keysym.lower() in ("c", "a"):
            return
        if e.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"):
            return
        return "break"

    def _prune_chat(self):
        """Cap the rendered transcript so a long session doesn't slow Tk layout / pile up
        embedded canvases. Delete oldest lines in a chunk (deleting a text range also
        destroys any embedded windows inside it, so the user-bubble/tool-chip canvases are
        freed, not leaked). Chunked so we don't delete on every single insert."""
        try:
            n = int(self.chat.index("end-1c").split(".")[0])
            # Only act once we're a chunk past the cap (so we don't delete on every insert),
            # then trim back to exactly the cap — never more, or a small cap would wipe the
            # whole buffer.
            if n > MAX_CHAT_LINES + 500:
                self.chat.delete("1.0", f"{n - MAX_CHAT_LINES}.0")
            # Also cap by characters: one giant whitespace-free assistant line is a single
            # logical line, so the line cap alone wouldn't bound it.
            try:
                cnt = self.chat.count("1.0", "end-1c", "chars")
                chars = cnt[0] if cnt else 0
            except Exception:
                chars = 0
            if chars and chars > MAX_CHAT_CHARS + 50_000:
                self.chat.delete("1.0", f"1.0+{chars - MAX_CHAT_CHARS}c")
            # If pruning removed the current assistant header but the flag still says we have
            # one, deltas would append with no "Claude" header → a detached turn. Re-arm so the
            # next delta re-inserts the header.
            if self._claude_header and not self.chat.tag_ranges("current_ah"):
                self._claude_header = False
                self._thinking_active = False   # header was pruned mid-thinking → re-arm the
                                                # "✻ thinking" label with the re-inserted header
        except Exception:
            pass

    def _ins(self, text, *tags):
        text = "" if text is None else str(text)   # Tk insert rejects None
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", text, tags)
        if at_bottom:
            self.chat.see("end")
        self._prune_chat()

    def add_user(self, text):
        self._md_finalize()              # commit the previous turn's last line before a new bubble
        self._turn_raw = ""              # a new turn starts → fresh assistant-answer buffer
        self._turn_copy_added = False
        self._set_task_badge(False)      # a new task → clear any stale "done" badge on the orb
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._user_bubble(text), pady=self.px(3))
        self.chat.insert("end", "\n")
        try:
            self.chat.tag_remove("current_ah", "1.0", "end")   # a new turn starts; old header
        except Exception:                                       # is no longer the "active" one
            pass
        self._claude_header = False
        self._thinking_active = False    # new turn → next thinking re-inserts its label
        if at_bottom:
            self.chat.see("end")
        self._prune_chat()

    @staticmethod
    def _clip_bubble(text):
        """Sanitize text for the bubble's *echo* only (the full text already went to
        Claude). Tk's canvas word-wrap is ~O(n²) on whitespace-free strings, so a pasted
        URL / base64 / minified-JSON blob would freeze the UI for seconds. Cap the length
        and break up long unbroken runs so wrapping stays linear."""
        s = "" if text is None else str(text)
        if len(s) > 2000:
            s = s[:2000] + " …"
        out, run = [], 0
        for ch in s:
            if ch.isspace():
                run = 0
            else:
                run += 1
                if run >= 50:        # force a wrap opportunity in a long unbroken run
                    out.append(" ")
                    run = 0
            out.append(ch)
        return "".join(out)

    def _user_bubble(self, text):
        """A right-aligned rounded chat bubble (drawn on a full-width canvas). render() recomputes
        the whole box from a body font at the *current* zoom, so it grows/shrinks with Ctrl +/−
        like the flowing text — recomputing the box each time means the bigger font never overflows
        a stale fixed size (the reason this used to be frozen). Registered with _register_zoomable."""
        text = self._clip_bubble(text)
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0)
        def render():
            c.delete("all")
            full = max(self.px(200), self.chat.winfo_width() - 2 * self.px(18))
            maxw = max(self.px(140), int(full * 0.74))
            padx, pady, rad = self.px(13), self.px(9), self.px(14)
            body_font = tkfont.Font(root=self.root, font=self.f_body)   # current zoom
            c._overlay_fonts = [body_font]                  # keep a ref so Tk won't GC it
            tmp = c.create_text(0, 0, text=text, font=body_font, width=maxw, anchor="nw")
            bb = c.bbox(tmp)
            x1, y1, x2, y2 = bb if bb else (0, 0, maxw, self.px(18))
            c.delete(tmp)
            bw, bh = (x2 - x1) + 2 * padx, (y2 - y1) + 2 * pady
            bx = full - bw                                  # hug the right edge
            round_rect(c, bx, 1, bx + bw, bh - 1, rad, fill=T["user_card"], outline="")
            c.create_text(bx + padx, pady, text=text, font=body_font, fill=T["text"],
                          width=maxw, anchor="nw")
            c.configure(width=full, height=bh)
        render()
        c.bind("<MouseWheel>", self._fwd_wheel)   # embedded widget must not swallow the scroll
        self._register_zoomable(c, render)
        return c

    def _ensure_header(self):
        if not self._claude_header:
            # Mark the header range with "current_ah" (left gravity so it stays put across the
            # insert) so _prune_chat can tell if a later trim removed the active header.
            self.chat.mark_set("ah_start", "end-1c")
            self.chat.mark_gravity("ah_start", "left")
            self._ins("\n✦ Claude\n", "ah")
            try:
                self.chat.tag_remove("current_ah", "1.0", "end")
                self.chat.tag_add("current_ah", "ah_start", "end-1c")
            except Exception:
                pass
            self._claude_header = True

    def add_think(self, text):
        # Stream extended-thinking tokens as a muted block under the Claude header, before
        # the answer. The "✻ thinking" label is inserted once per turn; subsequent thinking
        # text just appends. This keeps the (often 10-20s) pre-answer wait visibly alive.
        self._md_finalize()              # seal any answer text before a (re-opened) thinking block
        self._ensure_header()
        if not self._thinking_active:
            self._ins("\n✻ thinking\n", "think_label")
            self._thinking_active = True
        self._ins(text, "think")

    def add_delta(self, text):
        if text is not None:
            self._turn_raw += str(text)  # accumulate the raw answer text so the Copy button can
                                         # snapshot exactly what Claude wrote (Markdown and all)
        self._ensure_header()
        if self._thinking_active:        # the visible answer is starting → close the thinking block
            self._raw_ins("\n", "a")
            self._thinking_active = False
        self._md_feed(text)

    # ── streaming Markdown renderer ───────────────────────────────────────────────────
    # Claude streams Markdown token-by-token, so markup spans deltas. We commit BLOCK
    # elements (headings, lists, blockquotes, code fences, tables) when a line completes,
    # and render INLINE emphasis (**bold**, *italic*, `code`) live by re-rendering only the
    # current unfinished line on every delta — so a marker turns into formatting the instant
    # its closing token arrives. A table can't align row-by-row, so its raw rows show as they
    # stream, then snap into a real Tk grid the moment the table block ends.
    MD_INLINE = {"b": "md_b", "i": "md_i", "code": "md_code"}

    def _raw_ins(self, text, *tags):
        """Append text + tags without the per-insert see()/_prune_chat() that _ins does;
        the md feed batches scroll + prune once at the end (many tiny inline inserts otherwise)."""
        if text:
            self.chat.insert("end", text, tags)

    def _md_feed(self, chunk):
        if chunk is None:
            return
        chunk = str(chunk)
        if not chunk:
            return
        # Auto-scroll-follow: measure "am I at the bottom" BEFORE mutating content (an append
        # below the fold would otherwise read as "not at bottom" and break following). yview()/
        # see() are cheap for normal multi-line content (Tk caches per-line heights) but
        # O(line length) on a pathological newline-free GIANT line — so only for such a giant
        # current line do we throttle the scroll to ~25/s (a long stream of one huge line would
        # otherwise monopolise the UI thread → the v1.1.9-class freeze). Normal replies keep the
        # exact, correct per-delta follow.
        giant = len(self._md_tail) > self.MD_LIVE_REPARSE_MAX
        scroll = (time.monotonic() - self._md_last_scroll) >= 0.04 if giant else True
        at_bottom = False
        if scroll:
            try:
                at_bottom = self.chat.yview()[1] > 0.999
            except Exception:
                at_bottom = False
            if giant:
                self._md_last_scroll = time.monotonic()
        parts = chunk.split("\n")
        for i, part in enumerate(parts):
            if i < len(parts) - 1:                  # this part is terminated by a newline → commit
                self._md_clear_tail()               # lift whatever of the line is rendered
                self._md_unset_tail_mark()          # the next line re-anchors its own tail mark
                line = self._md_tail + part
                self._md_tail = ""
                self._md_commit_line(line)
            elif part:                              # the trailing, still-unfinished line
                self._md_grow_tail(part)
        if scroll and at_bottom:
            try:
                self.chat.see("end")
            except Exception:
                pass
        self._prune_chat()

    # cap live inline re-parsing on absurdly long single lines; formatting still finalizes
    # correctly when the line completes / on _md_finalize.
    MD_LIVE_REPARSE_MAX = 2000

    def _md_grow_tail(self, part):
        """Extend the current unfinished line. To stay O(n) over a long, newline-free line we
        APPEND new text cheaply and only re-parse the whole tail when a marker char (`*` or
        `` ` ``) arrives: plain text can't change existing spans (an unclosed span already
        renders raw until its closing marker, which is itself a marker char and so triggers the
        re-parse). Re-rendering the whole growing line on *every* delta was O(n²) and froze
        scrolling on long replies — this is the fix."""
        if "md_tail" not in self.chat.mark_names():
            self.chat.mark_set("md_tail", "end-1c")
            self.chat.mark_gravity("md_tail", "left")   # stays at the tail start as we append after it
        self._md_tail += part
        if self._md_fence:
            self._raw_ins(part, "a", "md_codeblock")    # fenced: raw monospace, never inline
        elif ("*" in part or "`" in part) and len(self._md_tail) <= self.MD_LIVE_REPARSE_MAX:
            self._md_clear_tail()                       # a marker arrived → re-parse the whole tail
            self._md_render_inline(self._md_tail, ("a",))
        else:
            self._raw_ins(part, "a")                    # no marker (or line too long) → cheap append

    def _md_autoscroll_final(self):
        """One-shot scroll-to-end at turn end (a giant line's last deltas may have been throttled
        out, leaving the view a hair off the bottom). Loose threshold so 'slightly behind due to
        throttling' still snaps to the end, while a user who clearly scrolled up to read earlier
        content is left alone."""
        try:
            if self.chat.yview()[1] > 0.90:
                self.chat.see("end")
        except Exception:
            pass
        self._md_last_scroll = time.monotonic()

    def _md_clear_tail(self):
        """Delete the live-rendered tail (md_tail mark → end) so it can be re-rendered."""
        try:
            if "md_tail" in self.chat.mark_names():
                self.chat.delete("md_tail", "end-1c")
        except Exception:
            pass

    def _md_unset_tail_mark(self):
        try:
            if "md_tail" in self.chat.mark_names():
                self.chat.mark_unset("md_tail")
        except Exception:
            pass

    def _md_commit_line(self, line, trailing_nl=True):
        """A complete line: classify it (fence / table row / heading / list / quote / text)
        and render it permanently."""
        if line.lstrip().startswith("```"):
            self._md_fence = not self._md_fence     # the fence line itself is not rendered
            return
        if self._md_fence:
            self._raw_ins(line + ("\n" if trailing_nl else ""), "a", "md_codeblock")
            return
        if self._md_is_table_row(line):
            if self._md_tbl is None:
                self._md_tbl = []
                self.chat.mark_set("md_tbl", "end-1c")
                self.chat.mark_gravity("md_tbl", "left")
            self._md_tbl.append(line)
            self._raw_ins(line + "\n", "a")         # raw preview; replaced by the grid on flush
            return
        if self._md_tbl is not None:                # a non-table line ends the table block
            self._md_flush_table()
        self._md_render_block_line(line, trailing_nl)

    def _md_render_block_line(self, line, trailing_nl=True):
        nl = "\n" if trailing_nl else ""
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            lvl = min(3, len(m.group(1)))
            tag = "md_h%d" % lvl
            self._md_render_inline(m.group(2), ("a", tag))
            self._raw_ins(nl, "a", tag)              # carry the tag onto the newline so spacing3 applies
            return
        m = re.match(r'^\s*[-*+]\s+(.*)$', line)
        if m:
            self._raw_ins("•  ", "a", "md_bullet")
            self._md_render_inline(m.group(1), ("a", "md_bullet"))
            self._raw_ins(nl, "a", "md_bullet")
            return
        m = re.match(r'^\s*(\d+)[.)]\s+(.*)$', line)
        if m:
            self._raw_ins("%s. " % m.group(1), "a", "md_bullet")
            self._md_render_inline(m.group(2), ("a", "md_bullet"))
            self._raw_ins(nl, "a", "md_bullet")
            return
        if line.lstrip().startswith(">"):
            self._md_render_inline(line.lstrip()[1:].lstrip(), ("a", "md_quote"))
            self._raw_ins(nl, "a", "md_quote")
            return
        if re.match(r'^\s*([-*_])\1{2,}\s*$', line):      # horizontal rule
            self._raw_ins("─" * 16 + nl, "a", "md_quote")
            return
        self._md_render_inline(line, ("a",))             # plain paragraph line
        self._raw_ins(nl, "a")

    def _md_render_inline(self, text, base):
        for seg, kind in self._md_inline_segments(text):
            if not seg:
                continue
            self._raw_ins(seg, *(base + ((self.MD_INLINE[kind],) if kind else ())))

    @staticmethod
    def _md_inline_segments(text):
        """Split a line into (text, kind) segments where kind ∈ {None,'b','i','code'}. Only
        COMPLETE spans get a kind; an unclosed `**`/`*`/`` ` `` is emitted as plain text so the
        live tail shows raw markers until the closing token streams in (then a re-render snaps
        it to formatting)."""
        segs, buf, i, n = [], [], 0, len(text)

        def flush():
            if buf:
                segs.append(("".join(buf), None))
                buf.clear()

        while i < n:
            c = text[i]
            if c == '`':
                j = text.find('`', i + 1)
                if j != -1:
                    flush(); segs.append((text[i + 1:j], "code")); i = j + 1; continue
                buf.append(text[i:]); break                      # unclosed → raw
            if c == '*':
                if text[i:i + 2] == '**':
                    j = text.find('**', i + 2)
                    if j != -1 and j > i + 2:
                        flush(); segs.append((text[i + 2:j], "b")); i = j + 2; continue
                    buf.append(text[i:]); break                  # unclosed → raw
                j = text.find('*', i + 1)
                if j != -1 and j > i + 1 and text[i + 1] != ' ':
                    flush(); segs.append((text[i + 1:j], "i")); i = j + 1; continue
                buf.append(c); i += 1; continue                  # lone '*' (e.g. a*b) → literal
            buf.append(c); i += 1
        flush()
        return segs

    @staticmethod
    def _md_is_table_row(line):
        t = line.strip()
        return t.startswith("|") and t.count("|") >= 2

    @staticmethod
    def _md_is_separator(line):
        t = line.strip().strip("|").strip()
        return bool(t) and set(t) <= set("-: |") and "-" in t

    @staticmethod
    def _md_strip_inline(text):
        """Table cells are plain Labels (no partial styling), so drop emphasis/code markers
        instead of showing them raw."""
        return text.replace("**", "").replace("`", "")

    def _md_split_table_cells(self, row):
        """Split a table row into cells on pipe boundaries — but NOT on a pipe inside an
        inline-code span (`` `a|b` ``) or one that's backslash-escaped (`\\|`). Splitting on
        every pipe byte would wrongly break a cell like `a|b` into two. Outer pipes are
        stripped; emphasis/code markers dropped (cells are plain Labels)."""
        s = row.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        out, buf, in_code, esc = [], [], False, False
        for ch in s:
            if esc:
                buf.append(ch); esc = False; continue
            if ch == "\\":
                buf.append(ch); esc = True; continue
            if ch == "`":
                in_code = not in_code; buf.append(ch); continue
            if ch == "|" and not in_code:
                out.append(self._md_strip_inline("".join(buf).strip())); buf = []
            else:
                buf.append(ch)
        out.append(self._md_strip_inline("".join(buf).strip()))
        return out

    def _md_flush_table(self):
        """Replace the raw rows buffered since md_tbl with a real Tk grid (or, if it wasn't a
        valid table after all, re-render them as plain lines)."""
        rows = self._md_tbl or []
        self._md_tbl = None
        try:
            if "md_tbl" in self.chat.mark_names():
                self.chat.delete("md_tbl", "end-1c")
                self.chat.mark_unset("md_tbl")
        except Exception:
            pass
        if len(rows) >= 2 and self._md_is_separator(rows[1]):
            try:
                header = self._md_split_table_cells(rows[0])
                body = [self._md_split_table_cells(r) for r in rows[2:]]
                tbl = self._build_table(header, body)
                self._raw_ins("\n", "a")
                self.chat.window_create("end", window=tbl, pady=self.px(4))
                self._raw_ins("\n", "a")
                return
            except Exception:
                pass                                  # fall through to a plain re-render
        for r in rows:
            self._md_render_block_line(r)

    def _build_table(self, header, body):
        """Render the table as a SINGLE lightweight Canvas that draws its own grid lines + cell
        text — NOT a Frame of N Labels. A Frame-of-Labels cost ~400 ms of synchronous Tk
        geometry management to embed/lay out each table (the "freezes when a table appears"
        stall), and worse, an embedded child widget SWALLOWS the mouse wheel so scrolling died
        whenever the cursor sat over a table. One Canvas lays out instantly and we forward its
        wheel to the chat. Columns are sized by the real measured pixel width of each cell, so
        CJK and ASCII still line up. Fonts are snapshotted at the current zoom and pinned on
        _overlay_fonts so Tk won't GC them; _prune_chat frees the canvas with its text range."""
        rows = [list(header)] + [list(r) for r in body]
        ncol = max((len(r) for r in rows), default=1) or 1
        cv = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, takefocus=0)
        def render():
            cv.delete("all")
            cell_f = tkfont.Font(root=self.root, font=self.f_body)   # current zoom
            head_f = tkfont.Font(root=self.root, font=self.f_chip)
            cv._overlay_fonts = [cell_f, head_f]
            padx, pady = self.px(9), self.px(5)
            avail = max(self.px(200), self.chat.winfo_width() - self.px(56))
            cap = max(self.px(90), int(avail / ncol))
            colw = [self.px(36)] * ncol
            for ri, r in enumerate(rows):
                f = head_f if ri == 0 else cell_f
                for c in range(ncol):
                    t = r[c] if c < len(r) else ""
                    colw[c] = max(colw[c], min(f.measure(t) + 2 * padx, cap))
            xs = [0]
            for c in range(ncol):
                xs.append(xs[-1] + colw[c])
            total_w = xs[-1]
            ys = [0]
            for ri, r in enumerate(rows):
                f = head_f if ri == 0 else cell_f
                rowmax = 0
                for c in range(ncol):
                    t = r[c] if c < len(r) else ""
                    tid = cv.create_text(xs[c] + padx, ys[ri] + pady, text=t, font=f, fill=T["text"],
                                         width=max(1, colw[c] - 2 * padx), anchor="nw")
                    bb = cv.bbox(tid)
                    rowmax = max(rowmax, (bb[3] - bb[1]) if bb else f.metrics("linespace"))
                ys.append(ys[ri] + rowmax + 2 * pady)
            total_h = ys[-1]
            cv.configure(width=total_w, height=total_h)
            # header tint behind the text, then thin grid lines + outer border (border colour)
            rect = cv.create_rectangle(0, 0, total_w, ys[1], fill=T["tool_bg"], outline="")
            cv.tag_lower(rect)
            b = T["border"]
            cv.create_rectangle(0, 0, total_w - 1, total_h - 1, outline=b)
            for c in range(1, ncol):
                cv.create_line(xs[c], 0, xs[c], total_h, fill=b)
            for ri in range(1, len(rows)):
                cv.create_line(0, ys[ri], total_w, ys[ri], fill=b)
        render()
        cv.bind("<MouseWheel>", self._fwd_wheel)   # don't let the table swallow the scroll
        self._register_zoomable(cv, render)
        return cv

    def _fwd_wheel(self, e):
        """Forward a wheel event that landed on an embedded widget to the chat's scroll, so
        hovering a table (or any embedded widget) never freezes scrolling."""
        try:
            self._on_wheel(e)
        except Exception:
            pass
        return "break"

    def _md_seal_mark(self):
        """Forget the live-tail / table marks (nothing left to re-render or delete)."""
        for m in ("md_tail", "md_tbl"):
            try:
                if m in self.chat.mark_names():
                    self.chat.mark_unset(m)
            except Exception:
                pass

    def _md_finalize(self):
        """Commit any in-flight table/tail into permanent content. Called before non-answer
        content (tool chip, thinking, system line, new turn) is appended at the end — otherwise
        the next _md_clear_tail would delete that content along with the tail — and at turn end
        so the last line gets full block styling. Idempotent."""
        try:
            self._md_clear_tail()
            tail = self._md_tail
            self._md_tail = ""
            if tail:
                self._md_commit_line(tail, trailing_nl=False)
            if self._md_tbl is not None:
                self._md_flush_table()
            self._md_autoscroll_final()       # giant-line throttling may have left us off-bottom
        except Exception:
            pass
        self._md_fence = False
        self._md_seal_mark()

    def _md_reset(self):
        """Drop md state without committing (the caller has wiped the chat)."""
        self._md_tail = ""
        self._md_tbl = None
        self._md_fence = False
        self._md_seal_mark()

    def add_tool(self, name, inp):
        # Skip the auto-screenshot Read so the chat isn't cluttered every turn.
        if HIDE_SCREENSHOT_TOOL and name == "Read" and isinstance(inp, dict) \
                and "claude_overlay_shots" in str(inp.get("file_path", "")):
            return
        self._md_finalize()              # seal the answer text streamed so far, then the tool chip
        self._ensure_header()
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._tool_chip(name, self._summ(inp, 46)),
                                padx=self.px(16), pady=self.px(3))
        self.chat.insert("end", "\n")
        if at_bottom:
            self.chat.see("end")
        self._prune_chat()

    @staticmethod
    def _truncate_to_px(font, text, budget):
        """Longest prefix of `text` that fits within `budget` pixels, with a trailing … if it had
        to be cut. Binary-searched on the font's pixel measure (so it's correct for CJK + ASCII)."""
        if not text or budget <= 0:
            return ""
        if font.measure(text) <= budget:
            return text
        ew = font.measure("…")
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid]) + ew <= budget:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo] + "…") if lo > 0 else "…"

    def _tool_chip(self, name, arg):
        """A compact rounded Claude-style tool pill embedded in the chat. render() rebuilds it from
        fonts at the *current* zoom so it grows/shrinks with Ctrl +/− (registered via
        _register_zoomable), and caps its width to the available chat width — ellipsizing the arg —
        so a long command/path can't overflow and get clipped when the window is narrow."""
        icon = TOOL_ICONS.get(name, "●")
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0)
        def render():
            c.delete("all")
            fi = tkfont.Font(root=self.root, font=self.f_small)   # current zoom
            fn = tkfont.Font(root=self.root, font=self.f_chip)
            fa = tkfont.Font(root=self.root, font=self.f_small)
            c._overlay_fonts = [fi, fn, fa]                 # keep refs so Tk won't GC them
            padx, gap, h = self.px(11), self.px(7), self.px(26)
            iw, nw = fi.measure(icon), fn.measure(name)
            # Cap to what fits in the chat (minus the chat's own padx + the window_create padx),
            # then ellipsize the arg into whatever width is left so the chip never overflows.
            fixed = 2 * padx + iw + gap + nw
            avail = max(self.px(120), self.chat.winfo_width() - self.px(68))
            shown = ""
            if arg:
                budget = avail - fixed - gap
                if budget > fa.measure("…"):
                    shown = self._truncate_to_px(fa, arg, budget)
            aw = fa.measure(shown) if shown else 0
            w = fixed + ((gap + aw) if shown else 0)
            c.configure(width=w, height=h)
            round_rect(c, 1, 1, w - 1, h - 1, self.px(8), fill=T["tool_bg"],
                       outline=T["border"], width=1)
            x, cy = padx, h / 2 - self.px(1)
            c.create_text(x, cy, text=icon, fill=T["accent"], font=fi, anchor="w"); x += iw + gap
            c.create_text(x, cy, text=name, fill=T["muted"], font=fn, anchor="w"); x += nw + gap
            if shown:
                c.create_text(x, cy, text=shown, fill=T["faint"], font=fa, anchor="w")
        render()
        c.bind("<MouseWheel>", self._fwd_wheel)   # embedded widget must not swallow the scroll
        self._register_zoomable(c, render)
        return c

    # ── per-message "Copy" button (ChatGPT/Claude-style) ──────────────────────────────
    def _copy_btn(self, text):
        """A small, always-visible ghost 'Copy' button rendered as an embedded canvas (same
        pattern as the tool chip / user bubble). Click copies `text` — a snapshot captured here,
        so an OLD message still copies the right thing after newer turns reset the live buffers —
        to the clipboard and flashes '✓ Copied' for ~1.2 s. Forwards the wheel so it can't swallow
        scrolling (the v1.4.1 embedded-widget trap)."""
        text = "" if text is None else str(text)
        idle, done = "⧉ Copy", "✓ Copied"
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2", takefocus=0)
        c._copied = False
        st = {"f": None, "w": 0, "h": 0, "rad": 0}   # current-zoom font + box, refreshed by render()

        def draw(label, fg, bg=None):
            c.delete("all")
            if bg:
                round_rect(c, 1, 1, st["w"] - 1, st["h"] - 1, st["rad"], fill=bg, outline="")
            c.create_text(st["w"] / 2, st["h"] / 2, text=label, fill=fg, font=st["f"], anchor="center")

        def show(hover=False):
            if c._copied:
                draw(done, T["accent"], T["tool_bg"])
            else:
                draw(idle, T["muted"] if hover else T["faint"], T["tool_bg"] if hover else None)

        def render():
            f = tkfont.Font(root=self.root, font=self.f_small)   # current zoom
            c._overlay_fonts = [f]                               # keep a ref so Tk won't GC it
            pad = self.px(9)
            st.update(f=f, h=self.px(20), rad=self.px(6),
                      w=pad + max(f.measure(idle), f.measure(done)) + pad)  # widest label → no reflow
            c.configure(width=st["w"], height=st["h"])
            show(False)

        def restore():
            try:
                c._copied = False
                show(False)
            except Exception:
                pass

        def on_click(_e):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
            except Exception:
                pass
            c._copied = True
            show(False)                # show() honours _copied → draws the '✓ Copied' state
            try:
                c.after(1200, restore)
            except Exception:
                pass
            return "break"

        render()
        c.bind("<Enter>", lambda e: show(True))
        c.bind("<Leave>", lambda e: show(False))
        c.bind("<Button-1>", on_click)
        c.bind("<MouseWheel>", self._fwd_wheel)   # embedded widget must not swallow the scroll
        self._register_zoomable(c, render)
        return c

    def _add_copy(self, text):
        """Drop a Copy button on its own line, left-aligned under the message it belongs to.
        No-ops on empty/whitespace text (e.g. a turn that produced only tool calls)."""
        if not (text and str(text).strip()):
            return
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._copy_btn(text), padx=self.px(16), pady=self.px(1))
        self.chat.insert("end", "\n")
        if at_bottom:
            self.chat.see("end")
        self._prune_chat()

    def _finish_turn_copy(self):
        """At turn end, add ONE Copy button under the assistant's reply, only if the turn
        produced answer text. Snapshots _turn_raw (passed by value into the button) so it keeps
        working after a later turn resets the buffer. Idempotent across the multiple
        turn_done/result events a single turn can emit."""
        if self._turn_copy_added or not (self._turn_raw or "").strip():
            return
        self._turn_copy_added = True
        self._add_copy(self._turn_raw)

    def add_sys(self, text):
        self._md_finalize()
        self._ins("\n" + ("" if text is None else str(text)) + "\n", "sys")

    def add_err(self, text):
        self._md_finalize()
        self._ins("\n⚠  " + ("" if text is None else str(text)) + "\n", "err")

    # ── "your CLI is out of date" notice + one-click update (see cliupdate.py) ──────────
    def _show_cli_update_notice(self, info):
        """Render the 'CLI is behind' notice + a one-click Update button in the chat. Shown at
        most once per session (guarded), and only reached when cliupdate found the CLI behind."""
        if getattr(self, "_cli_update_shown", False) or not isinstance(info, dict):
            return
        self._cli_update_shown = True
        inst, latest = info.get("installed", "?"), info.get("latest", "?")
        self.add_sys(f"🔔 Your Claude CLI is out of date (v{inst} → v{latest}). The overlay is "
                     "current, but the CLI it drives isn't — and the newest models need the "
                     "latest CLI. Update it in one click:")
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._cli_update_btn(latest),
                                padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")
        if at_bottom:
            self.chat.see("end")
        self._prune_chat()

    def _cli_update_btn(self, latest):
        """One-click 'Update CLI' button embedded in the chat (same embedded-canvas pattern as the
        Copy button). Click runs `npm install -g @anthropic-ai/claude-code@latest` in a background
        thread; the button shows 'Updating…' meanwhile and the outcome arrives as a
        ('cli_update_result', ...) event that restyles it. Forwards the wheel so it can't swallow
        scrolling (the v1.4.1 embedded-widget trap)."""
        latest = str(latest)
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2", takefocus=0)
        c._ustate = "idle"                              # idle | working | done | error
        st = {"f": None, "w": 0, "h": 0, "rad": 0}      # current-zoom font + box, set by render()
        labels = {"idle": f"⬆  Update CLI to v{latest}",
                  "working": "Updating…  (≈1 min)",
                  "done": "✓  Updated — click to restart",
                  "error": "⚠  Update failed — click to retry"}

        def draw(hover=False):
            c.delete("all")
            state = c._ustate
            if state in ("idle", "done"):
                bg = T["accent_hi"] if (hover and state in ("idle", "done")) else T["accent"]
                fg = T["on_accent"]
            elif state == "error":                      # clickable (retry) → hover-lit
                bg, fg = (T["hover"] if hover else T["tool_bg"]), T["err"]
            else:                                       # working
                bg, fg = T["tool_bg"], T["muted"]
            round_rect(c, 1, 1, st["w"] - 1, st["h"] - 1, st["rad"], fill=bg, outline="")
            c.create_text(st["w"] / 2, st["h"] / 2, text=labels[c._ustate], fill=fg,
                          font=st["f"], anchor="center")

        def render():
            f = tkfont.Font(root=self.root, font=self.f_small)   # current zoom
            c._overlay_fonts = [f]                               # keep a ref so Tk won't GC it
            pad = self.px(11)
            widest = max(f.measure(v) for v in labels.values())  # widest state → no reflow
            st.update(f=f, h=self.px(24), rad=self.px(7), w=pad + widest + pad)
            c.configure(width=st["w"], height=st["h"])
            draw()

        def set_state(s):
            c._ustate = s
            try:
                c.configure(cursor="hand2" if s in ("idle", "done", "error") else "arrow")
                draw()
            except Exception:
                pass
        c._set_ustate = set_state    # let the result handler restyle this exact button

        def on_click(_e):
            if c._ustate in ("idle", "error"):          # first click, or retry after a failure
                set_state("working")
                self._cli_update_btn_ref = c
                def work():
                    try:
                        from cliupdate import run_update
                        ok, msg = run_update()
                    except Exception as e:
                        ok, msg = False, type(e).__name__
                    self.ui_q.put(("cli_update_result", (bool(ok), str(msg))))
                threading.Thread(target=work, name="cli-update", daemon=True).start()
            elif c._ustate == "done":                   # after a successful update → restart now
                self._restart_overlay()
            return "break"                              # working → inert
        c._click = on_click    # a named handle so the routing is directly testable

        render()
        c.bind("<Enter>", lambda e: draw(hover=True))
        c.bind("<Leave>", lambda e: draw(hover=False))
        c.bind("<Button-1>", on_click)
        c.bind("<MouseWheel>", self._fwd_wheel)          # embedded widget must not swallow scroll
        self._register_zoomable(c, render)
        return c

    def _show_cli_update_result(self, payload):
        """Restyle the Update button to its final state and print a follow-up line: success →
        'restart to use it'; failure → the reason + the manual npm command as a fallback."""
        try:
            ok, msg = payload
        except Exception:
            ok, msg = False, str(payload)
        c = getattr(self, "_cli_update_btn_ref", None)
        if c is not None:
            try:
                c._set_ustate("done" if ok else "error")
            except Exception:
                pass
        if ok:
            self.add_sys(f"✅ Claude CLI updated to v{msg}. Click the button above to restart the "
                         "overlay now and load the newest models (or restart it yourself later).")
        else:
            self.add_err(f"CLI update didn't complete — {msg}. You can also update from a terminal: "
                         " npm install -g @anthropic-ai/claude-code@latest")

    def _restart_overlay(self):
        """Relaunch a fresh overlay instance, then close this one — the 'click to restart' action
        on the Update button (and reusable for any future restart affordance). Launches the new
        instance DETACHED (see win32utils.relaunch_overlay) so quitting this one can't take it
        down, then tears this one down after a short beat so the two barely overlap. If the
        relaunch can't even start, DON'T quit — leave the user with a working window + a note."""
        if getattr(self, "_restarting", False):
            return
        self._restarting = True
        try:
            relaunch_overlay(os.path.abspath(__file__))
        except Exception as e:
            self._restarting = False
            dbg("restart", f"relaunch failed: {type(e).__name__}: {e}")
            self.add_err("Couldn't relaunch automatically — please close and reopen the overlay.")
            return
        self.add_sys("↻ Restarting the overlay…")
        self.root.after(500, self.quit)

    def _format_turn_error(self, payload):
        """Turn the CLI's errored ResultMessage (subtype / result / stop_reason) into a one-line
        reason, so the chat says WHY the turn errored instead of a generic message. The leading ⚠
        is added by add_err. Examples: 'error_max_turns' → 'max turns'; an overloaded_error carries
        its detail text."""
        subtype = payload.get("subtype")
        detail = payload.get("result")
        reason = None
        if subtype and subtype != "success":
            reason = str(subtype).replace("error_", "").replace("_", " ").strip()
        if detail:
            d = str(detail).replace("\n", " ").strip()
            if len(d) > 200:
                d = d[:200] + "…"
            reason = f"{reason} — {d}" if reason else d
        if not reason:
            sr = payload.get("stop_reason")
            reason = f"stop reason: {sr}" if sr else "no detail reported by the CLI"
        return f"Last turn ended with an error ({reason}). Your next message is unaffected."

    @staticmethod
    def _summ(inp, maxlen=84):
        if not isinstance(inp, dict) or not inp:
            return ""
        for k in ("file_path", "path"):          # show just the filename
            if inp.get(k):
                return os.path.basename(str(inp[k]).rstrip("/\\"))
        for k in ("command", "pattern", "url", "query", "description", "prompt"):
            if inp.get(k):
                v = str(inp[k]).replace("\n", " ").strip()
                return v[:maxlen] + "…" if len(v) > maxlen else v
        v = ", ".join(f"{k}={str(val)[:20]}" for k, val in list(inp.items())[:2])
        return v[:maxlen]

    # ── actions ──
    def _on_return(self, e):
        if e.state & 0x0001:
            return
        self._send_or_stop()
        return "break"

    def _send_or_stop(self):
        if self.busy:
            self.worker.interrupt()
            self._set_status("stopping…")
            return
        text = self._entry_text()
        shots = None
        if self.auto_shot:
            pc = self._precaptured
            if pc and (time.monotonic() - pc[1]) < PRECAPTURE_MAX_AGE:
                shots = pc[0]                     # reuse the frame grabbed while you typed
            else:
                shots = self.capture(announce=False)
        elif self.pending_shot:
            shots = self.pending_shot
        self._precaptured = None
        images = list(self.pending_images)
        if not text and not shots and not images:
            return
        self.pending_shot = None
        self.pending_images = []
        self._refresh_attach()
        self.entry.delete("1.0", "end")
        self._ph_active = False
        n = (len(shots) if shots else 0) + len(images)
        label = text if text else "(look at my screens)"
        if n:
            label += (f"   🖼×{n}" if n > 1 else "   🖼")
        self.add_user(label)
        if IMAGE_INPUT == "inline":
            paths = [s["path"] for s in (shots or [])] + list(images)
            self.worker.ask(self._inline_text(text, shots, images), paths)
        else:
            self.worker.ask(self._build_prompt(text, shots, images), [])
        self._set_busy(True)

    def _inline_text(self, text, shots, images):
        """Short text companion for inline-image turns: the model sees the images
        directly, so we only add a one-line note about what's attached."""
        note = []
        if shots and shots[0].get("window") is not None:
            note.append(f"[Attached: a live screenshot of my ACTIVE WINDOW only — "
                        f"“{shots[0]['window']}” — not the full screen; other "
                        f"windows and monitors are not visible to you.]")
        elif shots:
            tags = ", ".join(f"monitor {s['index']}" + (" (primary)" if s["primary"] else "")
                             for s in shots)
            note.append(f"[Attached: a live screenshot of my screen — {tags}.]")
        if images:
            note.append(f"[Attached: {len(images)} pasted image(s).]")
        body = text if text else ("Look at the attached screen(s)/image(s) and tell me "
                                   "what's there / what I might want help with.")
        return ("\n".join(note) + "\n\n" + body) if note else body

    def _precapture_soon(self, e=None):
        """Debounced: schedule a screen grab shortly after the last keystroke so a
        fresh frame is ready at send time, off the critical path."""
        if not (PRECAPTURE_ON_TYPING and self.auto_shot) or self.busy:
            return
        if self._precapture_after:
            try:
                self.root.after_cancel(self._precapture_after)
            except Exception:
                pass
        self._precapture_after = self.root.after(180, self._do_precapture)

    def _do_precapture(self):
        self._precapture_after = None
        if not (PRECAPTURE_ON_TYPING and self.auto_shot) or self.busy:
            return
        if self._capture_busy:                       # a grab is already in flight — don't pile up
            return
        pc = self._precaptured                       # a recent frame is still fresh enough —
        if pc and (time.monotonic() - pc[1]) < 2.5:  # skip the redundant grab while typing
            return
        # Run the grab off the Tk thread: precapture is hide=False (never touched the window),
        # so it does no Tk work and a slow/wedged display stack can't freeze typing. Result
        # comes back via ui_q as ("precapture_done", shots).
        self._capture_busy = True
        threading.Thread(target=self._precapture_bg, daemon=True).start()

    def _precapture_bg(self):
        # enumerate_monitors() must be INSIDE the try: if it raises here (it used to be above
        # the guard), the thread dies without posting precapture_done, leaving _capture_busy
        # stuck True forever → type-ahead capture silently stops for the rest of the session.
        # The finally guarantees the flag is always cleared.
        shots = None
        try:
            mons = enumerate_monitors() or [{"rect": None, "primary": True}]
            shots, _ = self._grab_shots_scoped(mons)
        except BaseException:
            shots = None
        finally:
            self.ui_q.put(("precapture_done", shots))

    def _build_prompt(self, text, shots, images=None):
        parts = []
        lines = []
        if shots and shots[0].get("window") is not None:
            lines.append("My ACTIVE WINDOW was just captured — window only, NOT the full "
                         "screen (other windows/monitors are not visible to you):")
            lines.append(f"- Active window “{shots[0]['window']}”: {shots[0]['path']}")
        elif shots:
            lines.append("My current display was just captured — one image per monitor:")
            for s in shots:
                tag = "PRIMARY screen" if s["primary"] else "secondary screen"
                lines.append(f"- Monitor {s['index']} ({tag}): {s['path']}")
        for i, p in enumerate(images or [], 1):
            lines.append(f"- Pasted image {i}: {p}")
        if lines:
            parts.append("[ATTACHMENTS] " + "\n".join(lines) +
                         "\nUse the Read tool on each of these exact paths to view them, then respond.")
        parts.append(text if text else
                     "Look at the attached image(s)/screen(s) and tell me what's there / what I might want help with.")
        return "\n\n".join(parts)

    def _save_shot(self, img, stem: Path) -> Path:
        """Save a captured screen with the smallest practical inline payload."""
        fmt = SHOT_FORMAT if SHOT_FORMAT in {"auto", "png", "jpeg", "jpg"} else "auto"

        def save_png() -> Path:
            p = stem.with_suffix(".png")
            img.save(p)
            return p

        def save_jpeg() -> Path:
            p = stem.with_suffix(".jpg")
            rgb = img.convert("RGB") if img.mode != "RGB" else img
            rgb.save(p, format="JPEG", quality=SHOT_JPEG_QUALITY,
                     optimize=False, progressive=False, subsampling=1)
            return p

        if fmt == "png":
            return save_png()
        if fmt in {"jpeg", "jpg"}:
            return save_jpeg()

        png_path = jpg_path = None
        try:
            png_path = save_png()
        except Exception:
            png_path = None
        try:
            jpg_path = save_jpeg()
        except Exception:
            jpg_path = None
        if png_path is None and jpg_path is None:
            raise OSError("could not save screenshot")
        if png_path is None:
            return jpg_path
        if jpg_path is None:
            return png_path
        if jpg_path.stat().st_size < png_path.stat().st_size:
            keep, drop = jpg_path, png_path
        else:
            keep, drop = png_path, jpg_path
        try:
            drop.unlink()
        except Exception:
            pass
        return keep

    def _capture_target_hwnd(self):
        """The window a 'window'-scope capture should shoot: the current foreground
        window when it's a usable external one, else the last external foreground
        window _poll tracked (the app the user was in before focusing the overlay).
        None → no usable window; the caller falls back to full-screen capture."""
        hw = foreground_capture_window()
        if hw:
            return hw
        hw = self._last_ext_fg
        return hw if window_capturable(hw) else None

    def _grab_window_shot(self):
        """Capture ONLY the active window → ([shot], None), or (None, err) when there is
        no usable window / the grab failed — the caller falls back to _grab_shots so a
        capture is never silently dropped. Win32 + Pillow only, no Tk: safe on the
        background precapture thread, like _grab_shots."""
        try:
            hwnd = self._capture_target_hwnd()
            if not hwnd:
                return None, None
            bbox = window_bbox(hwnd)
            if not bbox:
                return None, None
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            if SHOT_MAX_EDGE and max(img.size) > SHOT_MAX_EDGE:
                img.thumbnail((SHOT_MAX_EDGE, SHOT_MAX_EDGE), Image.LANCZOS)
            p = self._save_shot(img, SHOT_DIR / f"shot_{int(time.time() * 1000)}_w")
            self._prune_shots()
            return [{"path": str(p), "primary": True, "index": 1,
                     "window": window_title(hwnd) or "untitled window"}], None
        except Exception as ex:
            return None, ex

    def _grab_shots_scoped(self, mons):
        """Scope dispatcher used by both the send-time and precapture paths: the active
        window when that scope is on AND a usable window exists, else every monitor."""
        if self.window_shot:
            shots, err = self._grab_window_shot()
            if shots:
                return shots, err
        return self._grab_shots(mons)

    def _grab_shots(self, mons):
        """Pure capture: one screenshot per monitor → downscale → save. Touches NO Tk, so
        it is safe to run on a background thread (used by the precapture path). Returns
        (shots, last_error)."""
        shots, err = [], None
        try:
            ts = int(time.time() * 1000)
            for i, m in enumerate(mons, 1):
                try:
                    bbox = m["rect"]
                    img = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab()
                    if SHOT_MAX_EDGE and max(img.size) > SHOT_MAX_EDGE:
                        img.thumbnail((SHOT_MAX_EDGE, SHOT_MAX_EDGE), Image.LANCZOS)
                    p = self._save_shot(img, SHOT_DIR / f"shot_{ts}_m{i}")
                    shots.append({"path": str(p), "primary": m["primary"], "index": i})
                except Exception as ex:
                    err = ex
        except Exception as ex:
            err = ex
        self._prune_shots()
        return shots, err

    def capture(self, announce=True, hide=True, quiet=False):
        """Grab one screenshot per monitor; returns a list of
        {'path', 'primary', 'index'} dicts. Images are downscaled to
        SHOT_MAX_EDGE before saving (Claude downsamples larger ones anyway).
        hide=True withdraws the overlay during the grab so it isn't in the shot
        (send time); hide=False skips that to avoid a flicker during
        pre-capture-while-typing. quiet=True suppresses the in-chat error if a
        grab fails (used for the silent pre-capture path)."""
        mons = enumerate_monitors() or [{"rect": None, "primary": True}]
        geo = self.root.geometry()
        # If the OS is excluding us from capture, the overlay is already invisible to
        # ImageGrab — no need to withdraw (which both flickers and freezes the UI for
        # the 0.15s settle). Only fall back to hiding if exclusion isn't active.
        do_hide = hide and not self._capture_excluded
        if do_hide:
            self.root.withdraw()
            self.root.update()
            time.sleep(0.15)
        try:
            shots, err = self._grab_shots_scoped(mons)
        finally:
            if do_hide:
                self.root.deiconify()
                self.root.overrideredirect(True)
                self.root.geometry(geo)
                self.root.attributes("-topmost", True)
                self._set_taskbar_button()   # withdraw→deiconify dropped the button; bring it back
                self.root.lift()
                self.root.after(20, self._apply_region)
        if not shots and not quiet:   # total failure — don't silently send no image
            self.add_err(f"Couldn't capture the screen: {type(err).__name__}: {err}"
                         if err else "Couldn't capture the screen.")
        if announce:
            self.pending_shot = shots
            n = len(shots)
            self.add_sys(f"📸 captured {n} screen{'s' if n != 1 else ''} — sends with your next message.")
        return shots

    def snap_now(self):
        self.capture(announce=True)

    def _prune_shots(self):
        # Best-effort from the very first filesystem op: a concurrent deleter (AV/quarantine,
        # a second overlay, a cleanup job) can remove a shot between glob() and stat(), which
        # used to throw OUT of here (the old try only wrapped unlink) — aborting capture() or
        # making paste silently fall back to the original path.
        try:
            files = []
            for p in list(SHOT_DIR.glob("shot_*.png")) + list(SHOT_DIR.glob("shot_*.jpg")):
                try:
                    files.append((p.stat().st_mtime, p))
                except Exception:
                    continue
            for _, old in sorted(files, key=lambda t: t[0])[:-KEEP_SHOTS]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def toggle_auto(self):
        self.auto_shot = not self.auto_shot
        self._paint_screen_toggle()

    def toggle_window_shot(self):
        """Flip the capture scope between the active window and all screens. Like the
        share toggle, the change is invisible on screen, so confirm it in-chat."""
        self.window_shot = not self.window_shot
        self._precaptured = None   # a frame grabbed under the OLD scope must not be sent
        self._paint_window_toggle()
        _save_state(window_shot=self.window_shot)   # deliberate choice → survives relaunch
        if self.window_shot:
            self.add_sys("🎯 Screenshots now capture the ACTIVE WINDOW only "
                         "(falls back to full screen when no window is in focus).")
        else:
            self.add_sys("🖥 Screenshots capture all screens again (one image per monitor).")

    def toggle_read_only(self):
        """Ask the worker to flip between read-only ("plan") and the configured
        full-access mode. Unlike the other toggles the state does NOT flip
        optimistically: it only changes when the worker confirms the CLI accepted
        the switch (the "permission_mode" event → _apply_permission_mode), so the
        label never claims a safety state the agent isn't actually in."""
        target = self._full_mode if self.read_only else "plan"
        self._set_status("switching permissions…")
        self.worker.set_permission_mode(target)

    def _apply_permission_mode(self, mode):
        """Worker confirmed the active permission mode: sync the toggle and, when it
        actually changed, say so in-chat (the switch itself is invisible on screen)."""
        ro = (mode == "plan")
        changed = (ro != self.read_only)
        self.read_only = ro
        self._paint_ro_toggle()
        if changed:
            _save_state(read_only=ro)   # persist only CONFIRMED switches, never requests
        if changed and ro:
            self.add_sys("🔒 Read-only: Claude can see your screen, read files, and "
                         "answer — but won't edit anything or run commands.")
        elif changed:
            self.add_sys(f"⚡ Full access ({mode}): Claude can now edit files and run "
                         "commands without asking. Flip Read-only back on any time.")

    def toggle_screen_share(self):
        """Flip whether the overlay is visible in screen shares (Teams/Zoom/OBS). The change
        is invisible on your OWN screen — the window looks identical either way; it only
        affects what others see — so confirm it in-chat so you know the toggle took."""
        self.share_visible = not self.share_visible
        self._apply_share_visibility()
        self._paint_share_toggle()
        if self.share_visible:
            self.add_sys("📺 Overlay will now appear in screen shares (Teams / Zoom / OBS).")
        else:
            self.add_sys("🙈 Overlay hidden from screen shares again — private (only you can see it).")

    def reset(self):
        # Interrupt any in-flight turn FIRST. Otherwise the worker is blocked in
        # receive_response() and the reset just queues behind it — meanwhile the tail
        # of the old reply keeps streaming deltas into the chat we just cleared.
        self.worker.interrupt()
        self.chat.delete("1.0", "end")
        self._md_reset()                 # chat wiped → drop md tail/table/fence state + marks
        self._zoomables = []             # all embedded canvases were just destroyed with the text
        self._turn_raw = ""              # drop the assistant-answer buffer + its Copy-button guard
        self._turn_copy_added = False
        self._set_task_badge(False)      # fresh conversation → drop any "task done" badge
        self._claude_header = False
        self._thinking_active = False    # don't carry a half-open thinking block into the new turn
        # Clear the shown % immediately so the OLD conversation's usage can't linger while the
        # async reset (close + reconnect) runs; the new session's true baseline arrives via the
        # worker's post-_open _emit_usage.
        self._ctx_pct = None
        self._refresh_statusline()
        self.worker.reset()
        self._set_status("resetting…")
        # Chat was just wiped — drop the compaction banner/timer so a stray result line
        # can't land in the fresh conversation (the worker's interrupt above ends the turn).
        if self._compacting:
            self._compacting = False
            self.busy = False
            self._refresh_send()
            if self._compact_anim_after is not None:
                try:
                    self.root.after_cancel(self._compact_anim_after)
                except Exception:
                    pass
                self._compact_anim_after = None
            self._compact_line = False
            try:
                self.chat.mark_unset("compact_ln")   # chat was wiped; drop the dangling mark
            except Exception:
                pass

    def compact_now(self):
        """Summarize the conversation so far to free up context (the CLI's /compact)."""
        if self._compacting:
            return
        if self.busy:
            self.add_sys("⏳ Finish (or Stop) the current reply before compacting.")
            return
        self.worker.compact()
        self._set_status("compacting…")   # instant feedback; the animation starts on ("compacting")

    def toggle_collapse(self):
        if self.expanded:
            # editing the name when the — / double-click collapses → commit it first
            if getattr(self, "_rename_entry", None) is not None:
                self._commit_rename()
            self._geo_before = self.root.geometry()
            gx, gy, gw = self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width()
            for w in (self.titlebar, self.hairline, self.chat_wrap, self.input_wrap,
                      self.status_frame, self.statusline_frame):
                w.pack_forget()
            self._hide_edges()
            s = self.orb_size
            name = (self.overlay_name or "").strip()
            if name:
                # Named: orb on top, name pill below — both placed at exact coords so the window
                # region (orb silhouette ∪ pill rounded-rect) lines up pixel-for-pixel.
                pw, ph = self._draw_name_pill()
                gap = self.px(5)
                W, H = max(s, pw), s + gap + ph
                x_orb, x_pill, y_pill = (W - s) // 2, (W - pw) // 2, s + gap
                self.orb.place(x=x_orb, y=0, width=s, height=s)
                self.orb_name.place(x=x_pill, y=y_pill, width=pw, height=ph)
                self.root.minsize(W, H)
                self.root.geometry(f"{W}x{H}+{gx + gw - W}+{gy}")   # right edge stays put
            else:
                self.orb_name.place_forget()
                self.orb.place(x=0, y=0, width=s, height=s)
                self.root.minsize(s, s)
                self.root.geometry(f"{s}x{s}+{gx + gw - s}+{gy}")   # stay at top-right corner
            self.expanded = False
            self._draw_orb()                  # ensure the badge (if any) is drawn for this collapse
            self._rebuild_collapsed_mask()    # silhouette = sprite [+ name] [+ badge]
        else:
            self._collapsed_mask = None
            # Do NOT clear the done-badge on expand: it must survive expand→collapse and only go
            # away when the next turn starts (add_user) or on reset. Re-collapsing redraws it.
            self.orb.place_forget()
            self.orb_name.place_forget()
            self.root.minsize(self.px(330), self.px(300))
            self.titlebar.pack(fill="x", side="top")
            self.hairline.pack(fill="x")
            self.statusline_frame.pack(fill="x", side="bottom")
            self.status_frame.pack(fill="x", side="bottom")
            self.input_wrap.pack(fill="x", side="bottom")
            self.chat_wrap.pack(fill="both", expand=True, side="top")
            self._show_edges()
            if hasattr(self, "_geo_before"):
                self.root.geometry(self._geo_before)   # may be a now-unplugged monitor's coords
            self.expanded = True
        self.root.after(30, self._apply_region)
        self.root.after(35, self._ensure_on_screen)   # keep the restored geometry on a live monitor

    # ── visibility (hotkey) ──
    def _register_hotkey(self):
        try:
            import keyboard
            keyboard.add_hotkey(HOTKEY, self._hotkey_fired)
            self._keyboard = keyboard
        except Exception as e:
            self._keyboard = None
            self.root.after(300, lambda: self.add_sys(f"(global hotkey unavailable: {e})"))

    def _hotkey_fired(self):
        self._toggle_request = True

    def _show_window(self):
        self.root.deiconify()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._set_taskbar_button()   # re-assert the taskbar button after a hotkey-hide → show
        self._force_foreground()     # hotkey path: WE initiate activation, push past the fg lock
        self._raise_to_front(focus=True)   # lift above topmost peers + focus the input
        self.root.after(60, self._apply_region)
        self.visible = True

    def toggle_visible(self):
        # Hidden → always show. Visible → only hide if we're already the foreground
        # window; if we're visible-but-behind/unfocused, pressing the hotkey means
        # "bring Claude to me", so raise+focus instead of making it vanish (the old
        # behaviour, which felt like the hotkey "couldn't summon" the app).
        if not self.visible:
            self._show_window()
            return
        try:
            hwnd = _user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            fg = _user32.GetForegroundWindow()
        except Exception:
            hwnd, fg = 1, 0
        if fg == hwnd:
            self.root.withdraw()
            self.visible = False
        else:
            self._show_window()

    # ── status / busy ──
    def _set_status(self, text):
        self.busy_lbl.configure(text=text)

    def _set_busy(self, busy):
        self.busy = busy
        self._refresh_send()
        self.busy_lbl.configure(text="thinking…" if busy else "")

    def _refresh_statusline(self):
        p = f"{self._ctx_pct:.0f}%" if isinstance(self._ctx_pct, (int, float)) else "—"
        # version goes last so it clips first if the window is narrow; ⬆ flags an update
        ver = f"v{__version__}" + ("  ⬆" if self._update_available else "")
        self.statusline.configure(
            text=f"{self._model or 'Claude'} ▾   ·   context {p}   ·   {ver}", fg=T["muted"])

    # ── compaction animation (mirrors the Claude Code CLI's /compact spinner) ──
    def _start_compact_anim(self):
        """Animate a one-line banner in the chat and pulse it until compaction finishes,
        then rewrite that same line as the result. It's REAL Text content (not an embedded
        widget), so it word-wraps with the window width and zooms with Ctrl +/−. The line is
        rewritten in place via the left-gravity mark `compact_ln`."""
        self._compacting = True
        self.busy = True                  # send button → Stop, so the user can cancel compaction
        self._refresh_send()
        self._md_finalize()               # seal any prior streamed line before the banner
        # dedicated wrapping tag for the live animation line (recoloured each frame to pulse)
        self.chat.tag_configure("compact", foreground=T["accent"], font=self.f_chip,
                                lmargin1=self.px(18), lmargin2=self.px(18), rmargin=self.px(14),
                                spacing1=self.px(6), spacing3=self.px(4))
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        start = self.chat.index("end-1c")           # start of our (about-to-be-written) line
        self.chat.insert("end", " \n", "compact")
        self.chat.mark_set("compact_ln", start)
        self.chat.mark_gravity("compact_ln", "left")  # stays at the line start across rewrites
        self._compact_line = True
        self._compact_t0 = time.monotonic()
        self._compact_frame = 0
        self._set_status("compacting…")
        if at_bottom:
            self.chat.see("end")
        self._compact_tick()

    def _compact_tick(self):
        if not self._compacting or not self._compact_line:
            return
        frames = "✶✷✸✹✺✹✸✷"             # a sparkle that pulses (same ✦/✻ family as the rest of the UI)
        i = self._compact_frame
        spark = frames[i % len(frames)]
        dots = "." * (i % 4)
        el = int(time.monotonic() - self._compact_t0)
        try:
            self.chat.delete("compact_ln", "compact_ln lineend")
            self.chat.insert("compact_ln", f"{spark}  Compacting conversation{dots}   ({el}s)",
                             "compact")
            self.chat.tag_configure(
                "compact", foreground=(T["accent"] if (i // 2) % 2 == 0 else T["accent_hi"]))
        except tk.TclError:
            return                        # line/mark gone (chat cleared) → stop quietly
        self._compact_frame = i + 1
        self._compact_anim_after = self.root.after(110, self._compact_tick)

    def _stop_compact_anim(self, payload):
        self._compacting = False
        self.busy = False
        self._refresh_send()
        if self._compact_anim_after is not None:
            try:
                self.root.after_cancel(self._compact_anim_after)
            except Exception:
                pass
            self._compact_anim_after = None
        if isinstance(payload, dict):
            status = payload.get("status", "ok")
            meta = payload.get("meta")
            detail = payload.get("detail")
        else:
            status, meta, detail = "ok", payload, None
        if status == "ok":
            final = self._format_compact_result(meta)
        elif status == "unconfirmed":
            final = "⚠ Compaction finished, but success couldn't be confirmed — context may be unchanged."
            if detail:
                final += f"  ({detail})"
        elif status == "cancelled":
            final = "⏹ Compaction stopped — conversation unchanged."
        elif status == "timeout":
            final = "⚠ Compaction timed out — conversation unchanged."
        else:
            final = "⚠ Compaction failed — conversation unchanged."
            if detail:
                final += f"  ({detail})"
        # Retag the result with a PERMANENT style tag (not the mutated "compact" tag) so a later
        # compaction recolouring "compact" can't repaint this finished line. ok → faint "sys"
        # line; everything else → "err". Both wrap + zoom like every other chat line.
        tag = "sys" if status in ("ok", "cancelled") else "err"
        active = self._compact_line
        self._compact_line = False
        self._set_status("")
        if active:
            try:
                self.chat.delete("compact_ln", "compact_ln lineend")
                self.chat.insert("compact_ln", final, tag)   # animation line → result line, in place
                self.chat.mark_unset("compact_ln")
                self._refresh_statusline()
                return
            except tk.TclError:
                pass
        # the banner line is gone (a Clear wiped the chat mid-compaction): a cancelled run needs
        # no trailing line (reset prints "new conversation"); other outcomes still report.
        if status != "cancelled":
            self.add_sys(final)
        self._refresh_statusline()

    def _format_compact_result(self, meta):
        if isinstance(meta, dict) and meta.get("pre_tokens") and meta.get("post_tokens"):
            try:
                pre, post = int(meta["pre_tokens"]), int(meta["post_tokens"])
                saved = (1 - post / pre) * 100 if pre else 0
                return (f"✦ Compacted — {pre:,} → {post:,} tokens "
                        f"(saved {saved:.0f}%). History summarized; keep going.")
            except Exception:
                pass
        return "✦ Compacted — conversation history summarized; keep going."

    def _model_menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg=T["field"], fg=T["text"],
                    activebackground=T["accent"], activeforeground=T["on_accent"], bd=0)
        for lbl, val in MODELS:
            m.add_command(label=lbl, command=lambda v=val: self._switch_model(v))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _switch_model(self, val):
        if self.busy:   # switching mid-stream is undefined against the SDK — defer
            self.add_sys("⏳ Finish (or Stop) the current reply before switching model.")
            return
        self._set_status("switching model…")
        self.worker.set_model(val)

    def _on_wheel(self, e):
        # Same scroll as before (no "break", so behavior is unchanged) — but timed. Tk
        # relayouts a Text holding many embedded canvases (our message bubbles + tool
        # chips) synchronously inside yview_scroll, so a janky scroll frame shows up as a
        # slow call here. Log only the slow frames (>50 ms) plus whether a reply is
        # streaming and how big the transcript is, so the intermittent scroll lag can be
        # caught in the act and attributed (large transcript vs. streaming contention).
        t0 = time.monotonic()
        self.chat.yview_scroll(int(-e.delta / 120), "units")
        if DEBUG_LOG:
            dt = (time.monotonic() - t0) * 1000
            if dt > 50:   # only genuinely janky frames
                try:
                    lines = int(self.chat.index("end-1c").split(".")[0])
                    wins = len(self.chat.window_names())   # embedded widgets (bubbles+chips+tables) in play
                except Exception:
                    lines = -1; wins = -1
                dbg("scroll_slow", f"{dt:.0f}ms streaming={getattr(self, 'busy', False)} lines={lines} embeds={wins}")

    # ── event pump ──
    def _poll(self):
        # Whatever happens in here, the pump MUST reschedule itself — an unhandled
        # exception that skipped the next after() used to silently freeze the whole UI
        # (window still drawn, but no replies, no events ever again). The finally
        # guarantees the next tick; per-message guarding keeps one bad render from
        # dropping the rest of the queue.
        self._last_pump = time.monotonic()    # hang-watchdog heartbeat (see _start_hang_watchdog)
        # Display-topology watchdog: if a monitor was just plugged/unplugged (the virtual-desktop
        # box changed), the frameless window may have been stranded off-screen — pull it back so it
        # comes forward on its own, without waiting for a taskbar click. Cheap (4 GetSystemMetrics)
        # and throttled to ~1.5s, so it's off the streaming/scroll path (no v1.1.9-class cost).
        if self._last_pump - self._vscreen_checked > 1.5:
            self._vscreen_checked = self._last_pump
            try:
                sig = virtual_screen_metrics()
                if sig is not None:
                    if self._vscreen_sig is not None and sig != self._vscreen_sig:
                        self._ensure_on_screen()
                    self._vscreen_sig = sig
            except Exception:
                pass
        # Track the most recent EXTERNAL foreground window (throttled, one cheap Win32
        # call): when a "window"-scope capture happens while the overlay itself has
        # focus — which is ALWAYS the case at send time, the user just typed here —
        # this remembered hwnd is the window the user was actually working in. Tracked
        # even while the toggle is off, so flipping it on works on the very next send.
        if self._last_pump - self._fg_checked > 0.5:
            self._fg_checked = self._last_pump
            try:
                hw = foreground_capture_window()
                if hw:
                    self._last_ext_fg = hw
            except Exception:
                pass
        if DEBUG_LOG and (self._last_pump - getattr(self, "_pump_logged", 0.0)) > 10.0:
            self._pump_logged = self._last_pump
            try:
                dbg("pump", "alive q=%d busy=%s" % (self.ui_q.qsize(), getattr(self, "busy", False)))
            except Exception:
                pass
        deadline = time.monotonic() + 0.012   # ~12ms budget per tick, so the drain can never
        handled = 0                            # monopolize Tk: a fast stream yields back for
        pending_delta = []                     # repaint / clicks / hotkey between slices.
        pending_think = []                     # thinking tokens, coalesced the same way

        def flush_delta():
            if pending_delta:
                joined = "".join(pending_delta)
                pending_delta.clear()
                try:
                    self._handle("delta", joined)
                except Exception:
                    pass

        def flush_think():
            if pending_think:
                joined = "".join(pending_think)
                pending_think.clear()
                try:
                    self._handle("think", joined)
                except Exception:
                    pass

        try:
            if self._toggle_request:
                self._toggle_request = False
                try:
                    self.toggle_visible()
                except Exception:
                    pass
            while handled < 400 and time.monotonic() < deadline:
                try:
                    kind, payload = self.ui_q.get_nowait()
                except queue.Empty:
                    break
                handled += 1
                if kind == "delta":            # coalesce adjacent text deltas into one insert
                    flush_think()              # ordering: any pending thinking renders first
                    pending_delta.append("" if payload is None else str(payload))
                    continue
                if kind == "think":            # coalesce adjacent thinking deltas too
                    flush_delta()
                    pending_think.append("" if payload is None else str(payload))
                    continue
                flush_think(); flush_delta()   # preserve ordering around non-stream messages
                try:
                    self._handle(kind, payload)
                except Exception as e:
                    try:
                        self.add_err(f"UI hiccup handling '{kind}': {type(e).__name__}: {e}")
                    except Exception:
                        pass
            flush_think(); flush_delta()
        except Exception:
            pass
        finally:
            # If we left messages behind (hit the budget), come back fast; otherwise idle.
            self.root.after(1 if not self.ui_q.empty() else 60, self._poll)

    def _handle(self, kind, payload):
        if kind == "ready":
            self.busy_lbl.configure(text="")
            self._refresh_statusline()
        elif kind == "reset_done":
            self.add_sys("🔄 new conversation.")
            # Don't null _ctx_pct here: reset() already cleared it on click, and the worker's
            # post-_open _emit_usage has (just before this) pushed the NEW session's real
            # baseline. Nulling now would discard that correct value and leave a bare "—".
            self._refresh_statusline()
            self._set_busy(False)
        elif kind == "delta":
            self.add_delta(payload)
        elif kind == "think":
            self.add_think(payload)
        elif kind == "tool":
            self.add_tool(payload[0], payload[1])
        elif kind == "model":
            self._model = str(payload)
            self._refresh_statusline()
        elif kind == "ctx":
            self._ctx_pct = payload
            self._refresh_statusline()
        elif kind == "turn_done":
            self._md_finalize()          # the turn ended → give the last line full block styling
            self._finish_turn_copy()     # then a Copy button under the reply
            self._set_busy(False)
            self._maybe_flag_done()      # badge the orb if this finished while collapsed
        elif kind == "compacting":
            self._start_compact_anim()
        elif kind == "compact_done":
            self._stop_compact_anim(payload)
        elif kind == "error":
            self.add_err(str(payload))
            self._set_busy(False)
        elif kind == "result":
            self._md_finalize()          # finalize before any error line is appended
            self._finish_turn_copy()     # Copy button under whatever reply text we did get
            # the SDK reports a turn that ended in error here even when no exception was raised on
            # our side; surface it WITH the CLI's reason (subtype/result) instead of a generic line.
            if isinstance(payload, dict) and payload.get("is_error"):
                self.add_err(self._format_turn_error(payload))
            self._set_busy(False)
        elif kind == "attach":          # background paste finished (paths, failed_count)
            self._paste_busy = False
            paths, failed = payload
            if paths:
                room = max(0, MAX_PENDING_IMAGES - len(self.pending_images))
                self.pending_images.extend(paths[:room])
                if len(paths) > room:   # over the queue cap → count the rest as not attached
                    failed += len(paths) - room
                self._refresh_attach()
            if failed:
                self.add_err(f"{failed} pasted image(s) couldn't be attached.")
        elif kind == "precapture_done":
            self._capture_busy = False
            if payload:
                self._precaptured = (payload, time.monotonic())
        elif kind == "status":
            self._set_status(str(payload))
        elif kind == "permission_mode":
            self._apply_permission_mode(str(payload))
        elif kind == "system":
            self.add_sys(str(payload))
        elif kind == "update":
            self._update_available = str(payload)
            self.add_sys(f"🔔 Update available: v{payload} (you have v{__version__}). "
                         "Close the overlay and run update.cmd (or: git pull) to upgrade.")
            self._refresh_statusline()
        elif kind == "cli_update":
            self._show_cli_update_notice(payload)
        elif kind == "cli_update_result":
            self._show_cli_update_result(payload)

    def _intro(self):
        self._ins("\n✦ Claude\n", "ah")
        self._ins("Hi — I float on top of everything. Ask me anything and I'll look at "
                  "your screen to help.\n"
                  "Enter to send · Shift+Enter for a new line · Ctrl +/− to zoom text · "
                  "drag an edge to resize.", "a")
        self._claude_header = True

    # ── shutdown ──
    def quit(self):
        if self._quitting:        # idempotent: a rapid double-close must not destroy() twice
            return
        self._quitting = True
        try:
            if getattr(self, "_keyboard", None):
                self._keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.worker.interrupt()      # stop any in-flight turn so it can close cleanly
        except Exception:
            pass
        self.worker.shutdown()
        # Let the worker disconnect the agent before we tear down. If Claude is
        # mid-turn (running a command or editing your open document), a hard kill
        # could interrupt that write — so wait, but bounded so quit never hangs.
        try:
            self.worker.join(timeout=3.0)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # Guarantee the process actually exits. Normally destroy() ends mainloop() and the
        # interpreter exits on its own — every thread we start (worker, paste, pre-capture,
        # update check) and even the `keyboard` listener are daemons, so nothing *should*
        # keep it alive. But "should" isn't "will": one wedged daemon thread stuck in a
        # C call (a hung SDK transport, an OS hook), or any non-daemon thread a future change
        # introduces, would leave a headless pythonw process running in the background after
        # the user clicked ✕ — exactly the "I closed it but it's still running" symptom.
        # os._exit is the unconditional terminator. We've already asked the worker to
        # interrupt + disconnect cleanly (bounded by the join above), so this can't cut short
        # a mid-turn write; and when this process dies its stdio pipes to the `claude` CLI
        # child close, so the child exits too (no orphaned agent left behind).
        dbg("quit", "terminating")
        os._exit(0)

    def run(self):
        self.root.mainloop()


def _selfheal_taskbar_shortcut():
    """Make sure the Start Menu shortcut (matching AppUserModelID) exists so the overlay
    pins to the taskbar correctly — relaunches when closed and shows the Clawd icon, not
    pythonw's. Runs off the UI thread: the common case is a cheap file-read no-op, and only
    the first launch (or a moved folder) pays a one-time ~1s builder spawn."""
    try:
        threading.Thread(
            target=lambda: dbg("shortcut",
                               ensure_taskbar_shortcut(os.path.abspath(__file__))),
            daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    set_dpi_awareness()
    set_app_user_model_id()   # before any window, so the taskbar uses our icon
    _selfheal_taskbar_shortcut()
    try:
        Overlay().run()
    except KeyboardInterrupt:
        sys.exit(0)
