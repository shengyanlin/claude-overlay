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
import sys
import threading
import time
import queue
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageGrab, ImageDraw, ImageChops, ImageFilter, ImageTk

# Make sure both common `claude` install locations are on PATH, in case it was just
# installed this session (PATH not yet refreshed): the native installer drops it in
# %USERPROFILE%\.local\bin, and a global npm install in %APPDATA%\npm.
os.environ["PATH"] = os.pathsep.join(filter(None, [
    os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin"),
    os.path.join(os.environ.get("APPDATA", ""), "npm"),
    os.environ.get("PATH", ""),
]))

# Spawn the `claude` CLI subprocess with no console window. Without this, running
# under pythonw (no console) makes Windows pop a CMD window for the console-mode CLI.
# Best-effort: if a future anyio drops/renames open_process, degrade gracefully
# (worst case a CMD window flashes) rather than crash on import.
try:
    import anyio as _anyio  # noqa: E402
    if sys.platform == "win32":
        _CREATE_NO_WINDOW = 0x08000000
        _orig_open_process = _anyio.open_process

        async def _open_process_no_window(*args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
            return await _orig_open_process(*args, **kwargs)

        _anyio.open_process = _open_process_no_window
except Exception:
    pass

from claude_agent_sdk import (  # noqa: E402
    ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock,
    ToolUseBlock, ResultMessage, StreamEvent,
)

__version__ = "1.1.0"

# ───────────────────────────── configuration ──────────────────────────────
WORKING_DIR = str(Path.home())
# NOTE: the Agent SDK's model=None does NOT follow the CLI's interactive default
# (which is opus-4-8); SDK 0.2.87 resolves None → opus-4-7. So pin the ID explicitly.
# Default to the standard 200K-context Opus: overlay chats never approach 200K, so the
# "[1m]" 1M-context variant only buys latency for context we never use. The 1M variant
# stays one click away in the MODELS switcher.
MODEL = "claude-opus-4-8"
MODELS = [("Opus 4.8", "claude-opus-4-8"), ("Opus 4.8 (1M)", "claude-opus-4-8[1m]"),
          ("Sonnet", "sonnet"), ("Haiku", "haiku")]  # click the statusline to switch
PERMISSION_MODE = "bypassPermissions"
AUTO_SCREENSHOT_DEFAULT = True
HIDE_SCREENSHOT_TOOL = True       # hide the noisy "⚙ Read …shot_*.png" lines every turn
HOTKEY = "ctrl+alt+space"
THEME = "light"                  # "light" (Claude paper) or "dark" (warm dark)
WINDOW_ALPHA = 1.0
CORNER_RADIUS = 18
ORB_SIZE = 56                    # diameter (logical px) of the collapsed Claude orb
# Fonts. Noto Sans/Serif TC cover Chinese + English in one family (closest free
# stand-in for Claude's proprietary Styrene/Copernicus). First available wins.
FONT_SANS = ["Noto Sans TC", "Inter", "Segoe UI Variable Text", "Segoe UI"]
FONT_SERIF = ["Noto Serif TC", "Georgia", "Cambria"]   # the "Claude" wordmark
FONT_MONO = ["Consolas", "Cascadia Mono", "Courier New"]
SHOT_DIR = Path(os.environ.get("TEMP", str(Path.home()))) / "claude_overlay_shots"
KEEP_SHOTS = 24                  # retain a few captures worth (one file per monitor)
SHOT_MAX_EDGE = 1568             # downscale captures to this long edge before sending.
                                 # Claude downsamples larger images internally anyway, so
                                 # bigger files only cost upload time + vision tokens.
IMAGE_INPUT = "inline"           # "inline" → attach screenshots as base64 image blocks
                                 # (no per-turn Read round-trip); "read" → legacy path:
                                 # save PNG + ask Claude to Read it. Flip to "read" if a
                                 # future CLI rejects inline images.
PRECAPTURE_ON_TYPING = True      # grab the screen ~as you type (off the send path) so
                                 # send latency excludes the capture.
PRECAPTURE_MAX_AGE = 6.0         # seconds a pre-captured frame stays reusable; older than
                                 # this at send time → re-grab fresh (bounds staleness).

SYSTEM_APPEND = (
    "You are running as an always-on-top floating overlay assistant on the user's "
    "Windows 11 desktop. The user talks to you without leaving their current app. "
    "Messages may include live screenshots of the user's screen — attached directly "
    "as images, or (legacy) as an [ATTACHMENTS] path you open with the Read tool. "
    "Use them to see what the user is looking at, then help. "
    "Keep replies concise and skimmable since they render in a small floating window; "
    "expand only when asked."
)

THEMES = {
    "light": {
        "bg": "#FAF9F5", "field": "#FFFFFF", "user_card": "#EFEBE1",
        "text": "#28261F", "muted": "#73706A", "faint": "#A9A59B",
        "accent": "#D97757", "accent_hi": "#C25E40", "on_accent": "#FFFFFF",
        "border": "#E6E2D8", "tool_bg": "#F2EFE7", "err": "#B4413A",
        "sel": "#EADDD3", "hover": "#EFEBE1",
    },
    "dark": {
        "bg": "#262624", "field": "#1F1E1D", "user_card": "#34332F",
        "text": "#ECEAE3", "muted": "#9B978D", "faint": "#6F6C64",
        "accent": "#D97757", "accent_hi": "#E68A6C", "on_accent": "#FFFFFF",
        "border": "#3A3934", "tool_bg": "#2E2D2A", "err": "#E0897D",
        "sel": "#3A3934", "hover": "#30302E",
    },
}
T = THEMES.get(THEME, THEMES["light"])


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


# Win32 region calls — set argtypes so 64-bit handles aren't truncated.
_gdi32, _user32 = ctypes.windll.gdi32, ctypes.windll.user32
_gdi32.CreateRoundRectRgn.restype = wt.HRGN
_gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_user32.SetWindowRgn.restype = ctypes.c_int
_user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, ctypes.c_bool]
_user32.GetAncestor.restype = wt.HWND
_user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_gdi32.CreateEllipticRgn.restype = wt.HRGN
_gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
_user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.GetMonitorInfoW.restype = ctypes.c_int
_MONENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(wt.RECT), ctypes.c_void_p)
_user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _MONENUMPROC, ctypes.c_void_p]
_user32.EnumDisplayMonitors.restype = ctypes.c_int


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


# ───────────────────────── background Claude worker ───────────────────────
class ClaudeWorker(threading.Thread):
    def __init__(self, ui_queue: "queue.Queue"):
        super().__init__(daemon=True)
        self.ui = ui_queue
        self.req: "queue.Queue" = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: ClaudeSDKClient | None = None
        self._running = True
        self._saw_stream = False

    def ask(self, text: str, image_paths=None):
        self.req.put(("ask", (text, list(image_paths or []))))
    def reset(self):                  self.req.put(("reset", None))
    def shutdown(self):
        self._running = False
        self.req.put(("stop", None))

    def interrupt(self):
        loop, client = self._loop, self._client
        if loop and client:
            asyncio.run_coroutine_threadsafe(self._safe_interrupt(client), loop)

    async def _safe_interrupt(self, client):
        try:
            await client.interrupt()
        except Exception:
            pass

    def set_model(self, model):
        loop, client = self._loop, self._client
        if loop and client:
            asyncio.run_coroutine_threadsafe(self._do_set_model(client, model), loop)

    async def _do_set_model(self, client, model):
        try:
            await client.set_model(model)
            await self._emit_usage()
        except Exception as e:
            self.ui.put(("error", f"set_model failed: {type(e).__name__}: {e}"))

    def _make_options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            permission_mode=PERMISSION_MODE, cwd=WORKING_DIR, model=MODEL,
            include_partial_messages=True,
            # exclude_dynamic_sections strips the per-turn-changing bits (cwd, git
            # status, auto-memory) out of the preset system prompt so the big static
            # prefix stays byte-stable → prompt-cache hits survive across turns.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_APPEND, "exclude_dynamic_sections": True},
        )

    def run(self):
        try:
            asyncio.run(self._amain())
        except Exception as e:  # pragma: no cover
            self.ui.put(("error", f"worker crashed: {type(e).__name__}: {e}"))

    async def _amain(self):
        self._loop = asyncio.get_running_loop()
        await self._open()
        while self._running:
            kind, payload = await self._loop.run_in_executor(None, self.req.get)
            if kind == "stop":
                break
            elif kind == "reset":
                await self._close()
                self._saw_stream = False
                await self._open()
                self.ui.put(("reset_done", None))
            elif kind == "ask":
                await self._run_turn(payload)
        await self._close()

    async def _open(self):
        try:
            self._client = ClaudeSDKClient(options=self._make_options())
            await self._client.connect()
            self.ui.put(("ready", None))
            await self._emit_usage()
        except Exception as e:
            self._client = None
            self.ui.put(("error",
                f"Could not start Claude: {type(e).__name__}: {e}\n"
                "Is the `claude` CLI installed and logged in? Run `claude --version` "
                "in a terminal; if it's missing, run setup.cmd (or `irm "
                "https://claude.ai/install.ps1 | iex`), then `claude` to /login."))

    async def _emit_usage(self):
        """Push current model + context-window usage % to the UI statusline."""
        try:
            u = await asyncio.wait_for(self._client.get_context_usage(), timeout=6)
            if isinstance(u, dict):
                if u.get("model"):
                    self.ui.put(("model", u["model"]))
                if u.get("percentage") is not None:
                    self.ui.put(("ctx", u["percentage"]))
        except Exception:
            pass

    async def _close(self):
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _run_turn(self, payload):
        text, image_paths = payload if isinstance(payload, tuple) else (payload, [])
        if self._client is None:
            self.ui.put(("error", "Not connected to Claude."))
            self.ui.put(("turn_done", None))
            return
        try:
            await self._client.query(self._build_query(text, image_paths))
            blocks: dict = {}
            async for msg in self._client.receive_response():
                self._dispatch(msg, blocks)
        except Exception as e:
            self.ui.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            self.ui.put(("turn_done", None))
            # Refresh context% off the critical path: schedule it rather than
            # awaiting, so the UI leaves "thinking…" the instant the reply ends
            # instead of after an extra round-trip.
            try:
                self._loop.create_task(self._emit_usage())
            except Exception:
                pass

    def _build_query(self, text: str, image_paths: list):
        """Return the prompt for client.query(). With inline images we yield a
        structured user message (text + base64 image blocks) so the model sees
        the screen directly — no per-turn Read round-trip. Otherwise a plain
        string (the legacy "Read the PNG path" flow builds its own text upstream)."""
        if IMAGE_INPUT != "inline" or not image_paths:
            return text
        content: list = []
        if text:
            content.append({"type": "text", "text": text})
        failed = 0
        for p in image_paths:
            try:
                data = Path(p).read_bytes()
            except Exception:
                failed += 1
                continue
            ext = Path(p).suffix.lower()
            mt = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                  ".gif": "image/gif"}.get(ext, "image/png")
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mt,
                "data": base64.b64encode(data).decode()}})
        if failed:   # tell the user their screen/image didn't actually attach
            self.ui.put(("error", f"{failed} image(s) couldn't be read and were not sent."))
        if not content:
            return text
        msg = {"type": "user",
               "message": {"role": "user", "content": content},
               "parent_tool_use_id": None}

        async def _one():
            yield msg

        return _one()

    def _dispatch(self, msg, blocks: dict):
        if isinstance(msg, StreamEvent):
            self._saw_stream = True
            ev = msg.event or {}
            t = ev.get("type")
            if t == "content_block_start":
                idx = ev.get("index")
                cb = ev.get("content_block", {}) or {}
                blocks[idx] = {"type": cb.get("type"), "name": cb.get("name"), "buf": ""}
            elif t == "content_block_delta":
                idx = ev.get("index")
                d = ev.get("delta", {}) or {}
                dt = d.get("type")
                if dt == "text_delta":
                    self.ui.put(("delta", d.get("text", "")))
                elif dt == "input_json_delta":
                    b = blocks.setdefault(idx, {"type": "tool_use", "name": None, "buf": ""})
                    b["buf"] += d.get("partial_json", "")
            elif t == "content_block_stop":
                idx = ev.get("index")
                b = blocks.get(idx) or {}
                if b.get("type") == "tool_use":
                    try:
                        inp = json.loads(b.get("buf") or "{}")
                    except Exception:
                        inp = {}
                    self.ui.put(("tool", (b.get("name") or "tool", inp)))
        elif isinstance(msg, AssistantMessage):
            if getattr(msg, "model", None):
                self.ui.put(("model", msg.model))
            if not self._saw_stream:
                for blk in msg.content:
                    if isinstance(blk, TextBlock):
                        self.ui.put(("delta", blk.text))
                    elif isinstance(blk, ToolUseBlock):
                        self.ui.put(("tool", (blk.name, blk.input)))
        elif isinstance(msg, ResultMessage):
            self.ui.put(("result", {"cost": msg.total_cost_usd, "is_error": msg.is_error}))


# ───────────────────────────── the overlay UI ─────────────────────────────
PLACEHOLDER = "Reply to Claude…"
TOOL_ICONS = {
    "Read": "▤", "Write": "✎", "Edit": "✎", "MultiEdit": "✎", "NotebookEdit": "✎",
    "Bash": "❯", "BashOutput": "❯", "KillShell": "❯", "PowerShell": "❯",
    "Glob": "⌕", "Grep": "⌕", "WebSearch": "⌕", "WebFetch": "↗", "ToolSearch": "⌕",
    "TodoWrite": "☑", "Task": "◆",
}


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class Overlay:
    def __init__(self):
        self.ui_q: "queue.Queue" = queue.Queue()
        self.worker = ClaudeWorker(self.ui_q)
        self.worker.start()

        self.auto_shot = AUTO_SCREENSHOT_DEFAULT
        self.pending_shot = None
        self.pending_images: list = []
        self._precaptured = None        # (shots, monotonic_ts) grabbed while typing
        self._precapture_after = None   # pending debounce timer id
        self._orb_imgs: dict = {}       # (size, hover) → PhotoImage cache for the orb
        self.busy = False
        self.visible = True
        self.expanded = True
        self._toggle_request = False
        self._model = None
        self._ctx_pct = None
        self._claude_header = False
        self._drag = (0, 0)
        self._resize = None
        self._round_after = None

        SHOT_DIR.mkdir(parents=True, exist_ok=True)
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

        self.root.after(130, lambda: (self.root.focus_force(), self.entry.focus_set()))
        self.root.bind("<Configure>", self._on_configure)
        self.root.after(170, self._apply_region)

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
        name = tk.Label(bar, text="Claude", bg=T["bg"], fg=T["text"], font=self.f_title)
        name.pack(side="left")
        self._bind_drag(name)
        self._title_btn(bar, "✕", self.quit)
        self._title_btn(bar, "—", self.toggle_collapse)

    def _build_chat(self):
        wrap = tk.Frame(self.root, bg=T["bg"])
        wrap.pack(fill="both", expand=True, side="top")
        self.chat_wrap = wrap
        self.chat = tk.Text(
            wrap, bg=T["bg"], fg=T["text"], bd=0, padx=self.px(18), pady=self.px(12),
            wrap="word", font=self.f_body, highlightthickness=0, cursor="arrow",
            width=1, height=1, selectbackground=T["sel"], selectforeground=T["text"],
            spacing1=self.px(2), spacing3=self.px(3),
        )
        self.chat.pack(fill="both", expand=True)
        self.chat.bind("<MouseWheel>", lambda e: self.chat.yview_scroll(int(-e.delta / 120), "units"))
        self.chat.bind("<Key>", self._readonly_keys)

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
        self._chip(st, "Snap", self.snap_now)
        self._chip(st, "Clear", self.reset)
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

    def _draw_orb(self, hover=False):
        s = self.orb_size
        self.orb.delete("all")
        self._orb_photo = self._orb_image(s, hover)   # keep a ref so Tk won't GC it
        self.orb.create_image(s // 2, s // 2, image=self._orb_photo)

    # ── glossy 3-D orb (rendered with Pillow, cached per size+state) ──
    @staticmethod
    def _rgb(hex_):
        hex_ = hex_.lstrip("#")
        return tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))

    @staticmethod
    def _mix(a, b, t):
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

    def _orb_image(self, s, hover):
        """Render a glossy terracotta sphere: off-centre radial gradient (volume),
        a soft top-left specular highlight, a darker bottom rim + lighter top rim
        (bevel), then the cream Claude spark with a faint drop shadow. Supersampled
        ×4 then LANCZOS-downscaled for crisp edges at any DPI. Cached per (size,hover)."""
        import math
        key = (s, hover)
        if key in self._orb_imgs:
            return self._orb_imgs[key]

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
        self.toggle_screen.configure(text=("◉  auto-screenshot" if on else "○  auto-screenshot"),
                                     fg=(T["accent"] if on else T["muted"]))

    # ── rounded input layout ──
    def _layout_input(self, e=None):
        c = self.canvas
        w = c.winfo_width()
        if w < 10:
            return
        h, pad = self.in_h, self.px(5)
        c.delete("box")
        round_rect(c, pad, pad, w - pad, h - pad, self.px(15), fill=T["field"],
                   outline=T["border"], width=1, tags="box")
        c.tag_lower("box")
        rad = self.px(15)
        bx, by = w - pad - self.px(38), h / 2
        ex1, ey1 = pad + self.px(14), pad + self.px(8)
        c.coords(self.entry_win, ex1, ey1)
        c.itemconfigure(self.entry_win, width=bx - rad - self.px(8) - ex1, height=h - 2 * pad - self.px(14))
        c.delete("send")
        col = T["err"] if self.busy else T["accent"]
        c.create_oval(bx - rad, by - rad, bx + rad, by + rad, fill=col, outline="", tags=("send", "sc"))
        c.create_text(bx, by - self.px(1), text=("■" if self.busy else "↑"),
                      fill=T["on_accent"], font=self.f_send, tags=("send", "sa"))
        c.tag_bind("send", "<Button-1>", lambda ev: self._send_or_stop())
        c.tag_bind("send", "<Enter>", lambda ev: c.itemconfigure("sc", fill=T["accent_hi"]))
        c.tag_bind("send", "<Leave>", lambda ev: c.itemconfigure(
            "sc", fill=(T["err"] if self.busy else T["accent"])))

    def _refresh_send(self):
        self.canvas.itemconfigure("sc", fill=(T["err"] if self.busy else T["accent"]))
        self.canvas.itemconfigure("sa", text=("■" if self.busy else "↑"))

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

    def _on_paste(self, e):
        """Ctrl+V: if the clipboard holds an image (or image files), attach it."""
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            data = None
        paths = []
        if isinstance(data, Image.Image):
            p = SHOT_DIR / f"shot_{int(time.time() * 1000)}_paste.png"
            try:
                data.save(p)
            except Exception:
                data.convert("RGB").save(p)
            paths.append(str(p))
        elif isinstance(data, list):
            for f in data:
                if str(f).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
                    paths.append(str(f))
        if paths:
            self.pending_images.extend(paths)
            self._refresh_attach()
            return "break"      # don't paste image bytes as garbage text
        return None             # plain text → let the normal paste happen

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

    def _on_configure(self, e):
        if e.widget is self.root:
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
                rgn = _gdi32.CreateEllipticRgn(0, 0, w + 1, h + 1)   # circular orb
            _user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    # ── chat rendering (main thread only) ──
    def _readonly_keys(self, e):
        if (e.state & 0x4) and e.keysym.lower() in ("c", "a"):
            return
        if e.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"):
            return
        return "break"

    def _ins(self, text, *tags):
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", text, tags)
        if at_bottom:
            self.chat.see("end")

    def add_user(self, text):
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._user_bubble(text), pady=self.px(3))
        self.chat.insert("end", "\n")
        self._claude_header = False
        if at_bottom:
            self.chat.see("end")

    def _user_bubble(self, text):
        """A right-aligned rounded chat bubble (drawn on a full-width canvas)."""
        full = max(self.px(200), self.chat.winfo_width() - 2 * self.px(18))
        maxw = max(self.px(140), int(full * 0.74))
        padx, pady, rad = self.px(13), self.px(9), self.px(14)
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0)
        tmp = c.create_text(0, 0, text=text, font=self.f_body, width=maxw, anchor="nw")
        x1, y1, x2, y2 = c.bbox(tmp)
        c.delete(tmp)
        bw, bh = (x2 - x1) + 2 * padx, (y2 - y1) + 2 * pady
        bx = full - bw                                  # hug the right edge
        round_rect(c, bx, 1, bx + bw, bh - 1, rad, fill=T["user_card"], outline="")
        c.create_text(bx + padx, pady, text=text, font=self.f_body, fill=T["text"],
                      width=maxw, anchor="nw")
        c.configure(width=full, height=bh)
        return c

    def _ensure_header(self):
        if not self._claude_header:
            self._ins("\n✦ Claude\n", "ah")
            self._claude_header = True

    def add_delta(self, text):
        self._ensure_header()
        self._ins(text, "a")

    def add_tool(self, name, inp):
        # Skip the auto-screenshot Read so the chat isn't cluttered every turn.
        if HIDE_SCREENSHOT_TOOL and name == "Read" and isinstance(inp, dict) \
                and "claude_overlay_shots" in str(inp.get("file_path", "")):
            return
        self._ensure_header()
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._tool_chip(name, self._summ(inp, 46)),
                                padx=self.px(16), pady=self.px(3))
        self.chat.insert("end", "\n")
        if at_bottom:
            self.chat.see("end")

    def _tool_chip(self, name, arg):
        """A compact rounded Claude-style tool pill embedded in the chat."""
        icon = TOOL_ICONS.get(name, "●")
        fi, fn, fa = self.f_small, self.f_chip, self.f_small
        padx, gap, h = self.px(11), self.px(7), self.px(26)
        iw, nw = fi.measure(icon), fn.measure(name)
        aw = fa.measure(arg) if arg else 0
        w = padx + iw + gap + nw + ((gap + aw) if arg else 0) + padx
        c = tk.Canvas(self.chat, width=w, height=h, bg=T["bg"], highlightthickness=0)
        round_rect(c, 1, 1, w - 1, h - 1, self.px(8), fill=T["tool_bg"],
                   outline=T["border"], width=1)
        x, cy = padx, h / 2 - self.px(1)
        c.create_text(x, cy, text=icon, fill=T["accent"], font=fi, anchor="w"); x += iw + gap
        c.create_text(x, cy, text=name, fill=T["muted"], font=fn, anchor="w"); x += nw + gap
        if arg:
            c.create_text(x, cy, text=arg, fill=T["faint"], font=fa, anchor="w")
        return c

    def add_sys(self, text):
        self._ins("\n" + text + "\n", "sys")

    def add_err(self, text):
        self._ins("\n⚠  " + text + "\n", "err")

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
        if shots:
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
        pc = self._precaptured                       # a recent frame is still fresh enough —
        if pc and (time.monotonic() - pc[1]) < 2.5:  # skip the redundant grab while typing
            return
        try:
            self._precaptured = (self.capture(announce=False, hide=False, quiet=True), time.monotonic())
        except Exception:
            self._precaptured = None

    def _build_prompt(self, text, shots, images=None):
        parts = []
        lines = []
        if shots:
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
        if hide:
            self.root.withdraw()
            self.root.update()
            time.sleep(0.15)
        shots = []
        err = None
        try:
            ts = int(time.time() * 1000)
            for i, m in enumerate(mons, 1):
                try:
                    bbox = m["rect"]
                    img = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab()
                    if SHOT_MAX_EDGE and max(img.size) > SHOT_MAX_EDGE:
                        img.thumbnail((SHOT_MAX_EDGE, SHOT_MAX_EDGE), Image.LANCZOS)
                    p = SHOT_DIR / f"shot_{ts}_m{i}.png"
                    img.save(p)
                    shots.append({"path": str(p), "primary": m["primary"], "index": i})
                except Exception as ex:
                    err = ex
        finally:
            if hide:
                self.root.deiconify()
                self.root.overrideredirect(True)
                self.root.geometry(geo)
                self.root.attributes("-topmost", True)
                self.root.lift()
                self.root.after(20, self._apply_region)
        self._prune_shots()
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
        for old in sorted(SHOT_DIR.glob("shot_*.png"), key=lambda p: p.stat().st_mtime)[:-KEEP_SHOTS]:
            try:
                old.unlink()
            except Exception:
                pass

    def toggle_auto(self):
        self.auto_shot = not self.auto_shot
        self._paint_screen_toggle()

    def reset(self):
        self.chat.delete("1.0", "end")
        self._claude_header = False
        self.worker.reset()
        self._set_status("resetting…")

    def toggle_collapse(self):
        if self.expanded:
            self._geo_before = self.root.geometry()
            gx, gy, gw = self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width()
            for w in (self.titlebar, self.hairline, self.chat_wrap, self.input_wrap,
                      self.status_frame, self.statusline_frame):
                w.pack_forget()
            self._hide_edges()
            s = self.orb_size
            self.orb.pack(fill="both", expand=True)
            self.root.minsize(s, s)
            self.root.geometry(f"{s}x{s}+{gx + gw - s}+{gy}")   # stay at top-right corner
            self.expanded = False
        else:
            self.orb.pack_forget()
            self.root.minsize(self.px(330), self.px(300))
            self.titlebar.pack(fill="x", side="top")
            self.hairline.pack(fill="x")
            self.statusline_frame.pack(fill="x", side="bottom")
            self.status_frame.pack(fill="x", side="bottom")
            self.input_wrap.pack(fill="x", side="bottom")
            self.chat_wrap.pack(fill="both", expand=True, side="top")
            self._show_edges()
            if hasattr(self, "_geo_before"):
                self.root.geometry(self._geo_before)
            self.expanded = True
        self.root.after(30, self._apply_region)

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

    def toggle_visible(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
        else:
            self.root.deiconify()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.after(40, lambda: (self.root.focus_force(), self.entry.focus_set()))
            self.root.after(60, self._apply_region)
            self.visible = True

    # ── status / busy ──
    def _set_status(self, text):
        self.busy_lbl.configure(text=text)

    def _set_busy(self, busy):
        self.busy = busy
        self._refresh_send()
        self.busy_lbl.configure(text="thinking…" if busy else "")

    def _refresh_statusline(self):
        p = f"{self._ctx_pct:.0f}%" if isinstance(self._ctx_pct, (int, float)) else "—"
        self.statusline.configure(text=f"{self._model or 'Claude'} ▾   ·   context {p}", fg=T["muted"])

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

    # ── event pump ──
    def _poll(self):
        if self._toggle_request:
            self._toggle_request = False
            self.toggle_visible()
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def _handle(self, kind, payload):
        if kind == "ready":
            self.busy_lbl.configure(text="")
            self._refresh_statusline()
        elif kind == "reset_done":
            self.add_sys("🔄 new conversation.")
            self._ctx_pct = None
            self._refresh_statusline()
            self._set_busy(False)
        elif kind == "delta":
            self.add_delta(payload)
        elif kind == "tool":
            self.add_tool(payload[0], payload[1])
        elif kind == "model":
            self._model = str(payload)
            self._refresh_statusline()
        elif kind == "ctx":
            self._ctx_pct = payload
            self._refresh_statusline()
        elif kind == "turn_done":
            self._set_busy(False)
        elif kind == "error":
            self.add_err(str(payload))
            self._set_busy(False)
        elif kind == "system":
            self.add_sys(str(payload))

    def _intro(self):
        self._ins("\n✦ Claude\n", "ah")
        self._ins("Hi — I float on top of everything. Ask me anything and I'll look at "
                  "your screen to help.\n"
                  f"Enter to send · Shift+Enter for a new line · Ctrl +/− to zoom text · "
                  f"drag an edge to resize · {HOTKEY} to summon/hide me.", "a")
        self._claude_header = True

    # ── shutdown ──
    def quit(self):
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
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    set_dpi_awareness()
    try:
        Overlay().run()
    except KeyboardInterrupt:
        sys.exit(0)
