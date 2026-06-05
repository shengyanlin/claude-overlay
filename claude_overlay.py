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
    ToolUseBlock, ResultMessage, StreamEvent, PermissionResultAllow,
)
# Error types used to decide when the transport is broken and we should reconnect.
# Imported defensively: older/newer SDKs may not export all of them.
try:
    from claude_agent_sdk import (  # noqa: E402
        ClaudeSDKError, CLIConnectionError, CLIJSONDecodeError, ProcessError,
    )
except Exception:  # pragma: no cover
    class ClaudeSDKError(Exception): ...
    class CLIConnectionError(ClaudeSDKError): ...
    class CLIJSONDecodeError(ClaudeSDKError): ...
    class ProcessError(ClaudeSDKError): ...

__version__ = "1.2.2"

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
STRICT_MCP_CONFIG = True          # do NOT inherit the user's ~/.claude MCP servers. The overlay
                                  # is a lightweight screen-chat that only needs the core Claude
                                  # Code tools; inheriting every MCP server the user has configured
                                  # (Atlassian, Figma, M365, …) injects their tool schemas into the
                                  # context — easily 50-70K+ tokens, a third of a 200K window, gone
                                  # before you type. Flip to False to expose your MCP tools here.
AUTO_SCREENSHOT_DEFAULT = True
HIDE_SCREENSHOT_TOOL = True       # hide the noisy "⚙ Read …shot_*.png" lines every turn
HOTKEY = "ctrl+alt+space"
THEME = "light"                  # "light" (Claude paper) or "dark" (warm dark)
WINDOW_ALPHA = 1.0
CORNER_RADIUS = 18
ORB_SIZE = 56                    # diameter (logical px) of the collapsed Claude orb
ORB_IMAGE = "claude_overlay_2.png"  # collapsed-orb artwork. "" → procedural glossy
                                 # terracotta sphere (original look). A path (relative
                                 # to this script, or absolute) to a PNG/ICO renders that
                                 # image instead: it's auto-scaled + centred so the whole
                                 # opaque shape fits inside the circular orb. RGBA with a
                                 # transparent background works best.
ORB_IMAGE_MARGIN = 0.04          # fraction of the radius kept clear around the artwork
                                 # (0 = touches the circle edge; 0.04 = tiny breathing room)
ORB_FLOAT = True                 # True + an ORB_IMAGE → the collapsed orb is clipped to the
                                 # artwork's own silhouette (a free-floating pixel sprite, no
                                 # circular frame; clicks outside the shape pass through).
                                 # False → the classic circular orb. Ignored without artwork.
ORB_ALPHA_THRESHOLD = 110        # pixels at/above this alpha (0-255) count as "solid" when
                                 # building the silhouette — higher = tighter, crisper edge
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
MAX_BUFFER_SIZE = 64 * 1024 * 1024   # the SDK aborts a turn with CLIJSONDecodeError when a
                                 # single stream-json line exceeds this (default 1MB). Inline
                                 # screenshots (base64, ×monitors) blow past 1MB easily and
                                 # used to crash the worker — 64MB gives huge headroom.
# A hang is NOT an exception, so the reconnect / bounded-restart guards (which only fire
# on a raised error) can't preempt an SDK call that never resolves — a wedged transport
# (broken corporate TLS, half-open socket, CLI waiting on a prompt with no TTY) would pin
# the worker forever. Bound every SDK lifecycle call so a hang degrades to a clean
# reconnect instead of a permanent freeze.
CONNECT_TIMEOUT = 30        # connect() that hasn't resolved by here ⇒ wedged transport
QUERY_TIMEOUT = 60          # sending the request is near-instant; bound it anyway
DISCONNECT_TIMEOUT = 10     # don't let a stuck disconnect hang shutdown/reconnect
RECV_IDLE_TIMEOUT = 300     # no stream activity for this long ⇒ treat the transport as dead
                            # (generous: a long-running tool can legitimately go quiet a while)
MAX_INLINE_IMAGE_BYTES = 16 * 1024 * 1024   # never base64-inline a local file bigger than this
                            # (a multi-GB file with an image extension would otherwise be read
                            # whole into RAM and explode the query payload)
MAX_CHAT_LINES = 4000       # cap the rendered transcript; prune oldest lines past this so a
                            # very long session doesn't slow Tk layout / leak embedded canvases
MAX_CHAT_CHARS = 350_000    # also cap by characters — one giant whitespace-free assistant
                            # line counts as 1 line and would otherwise bypass the line cap
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
TOOL_IDLE_TIMEOUT = 1800    # once a tool call is in flight, allow a much longer silent gap
                            # (a long build/test can legitimately stream nothing for minutes)
MAX_PASTE_SOURCES = 8       # cap how many files one paste fans out into
MAX_PENDING_IMAGES = 16     # cap total queued attachments (a hostile clipboard can't pile up)
MAX_PASTE_PIXELS = 32_000_000   # reject a pasted image above this pixel count BEFORE decode/
                            # thumbnail — a "decompression bomb" PNG decodes to a huge bitmap
                            # (Pillow only *warns*, doesn't raise, below ~178M px)
MAX_INLINE_IMAGES = 16      # cap images per turn (count) ...
MAX_INLINE_TOTAL_BYTES = 32 * 1024 * 1024   # ... and aggregate bytes (the per-file cap alone
                            # doesn't bound many-attachment memory blow-up)
MAX_UPDATE_BODY = 1 * 1024 * 1024   # cap the update-check response body before json.loads
MAX_UPDATE_TAGS = 300       # and the number of tags parsed

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
_user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
_user32.IsClipboardFormatAvailable.restype = ctypes.c_int
# Standard clipboard format ids — used for a cheap, non-blocking "is there an image?" probe
# on the UI thread, so we only spin up the (potentially slow) ImageGrab.grabclipboard() read
# on a background thread when there's actually image/file content.
CF_BITMAP, CF_DIB, CF_HDROP, CF_DIBV5 = 2, 8, 15, 17

# Exclude the overlay from screen captures at the OS level (DWM): the window stays
# visible to the user but is omitted from PIL ImageGrab / PrintWindow, so the
# screenshots we send Claude never contain the overlay obscuring the content — and
# we no longer have to withdraw() + sleep() on every capture. Verified on this
# machine (returns the content behind the window, not black).
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
        self._lifecycle_task = None   # the in-flight connect()/disconnect() task, if any

    def ask(self, text: str, image_paths=None):
        self.req.put(("ask", (text, list(image_paths or []))))
    def reset(self):                  self.req.put(("reset", None))
    def shutdown(self):
        self._running = False
        # If the worker is currently AWAITING a lifecycle call (connect/disconnect), the
        # queued "stop" can't be read until that await returns (up to CONNECT_TIMEOUT).
        # Cancel the in-flight lifecycle task so the worker can wind down promptly instead
        # of leaving a daemon thread + orphaned `claude` CLI child after the UI is gone.
        loop, task = self._loop, self._lifecycle_task
        if loop and task and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        self.req.put(("stop", None))

    def interrupt(self):
        loop, client = self._loop, self._client
        # The loop may be closed (worker finished / between restarts) — calling
        # run_coroutine_threadsafe on a closed loop raises RuntimeError straight into the
        # Tk callback (reset()/Stop don't guard it) and leaks the coroutine object.
        if not (loop and client) or loop.is_closed():
            return
        coro = self._safe_interrupt(client)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            try:
                coro.close()
            except Exception:
                pass

    async def _safe_interrupt(self, client):
        try:
            await client.interrupt()
        except Exception:
            pass

    def set_model(self, model):
        # Go through the request queue (not run_coroutine_threadsafe) so a model switch is
        # serialized behind any queued reset/ask and can't interleave with _close() tearing
        # down the same client — which could leave a half-disconnected client or a status
        # line stuck on "switching model…".
        self.req.put(("set_model", model))

    async def _do_set_model(self, client, model):
        try:
            await client.set_model(model)
            await self._emit_usage()
            self.ui.put(("status", ""))   # clear the "switching model…" notice
        except Exception as e:
            self.ui.put(("error", f"set_model failed: {type(e).__name__}: {e}"))

    async def _allow_tool(self, tool_name, input_data, context):
        # Auto-approve every tool. permission_mode="bypassPermissions" already does
        # this on most machines, but managed/enterprise installs can DISABLE bypass
        # mode (managed-settings.json: disableBypassPermissionsMode), which makes the
        # CLI fall back to "default" and emit a permission prompt. The overlay is a
        # GUI with no TTY, so an unanswered prompt would just hang the turn forever
        # ("nowhere to approve"). This callback answers those prompts so the overlay
        # works regardless of the host's permission policy. The tool call still shows
        # up as a chip in the chat via the normal streaming path, so it isn't silent.
        return PermissionResultAllow()

    def _make_options(self) -> ClaudeAgentOptions:
        opts = dict(
            permission_mode=PERMISSION_MODE, cwd=WORKING_DIR, model=MODEL,
            can_use_tool=self._allow_tool,
            include_partial_messages=True,
            # exclude_dynamic_sections strips the per-turn-changing bits (cwd, git
            # status, auto-memory) out of the preset system prompt so the big static
            # prefix stays byte-stable → prompt-cache hits survive across turns.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_APPEND, "exclude_dynamic_sections": True},
        )
        opts["max_buffer_size"] = MAX_BUFFER_SIZE
        if STRICT_MCP_CONFIG:
            # Use ONLY the (empty) MCP servers defined here, ignoring the user's filesystem
            # config. Without this the spawned CLI loads every MCP server from ~/.claude.json
            # and injects all their tool schemas — measured at 72K tokens (36% of Haiku's
            # 200K window) on one machine with many MCP servers, gone before the first
            # message. setting_sources
            # alone does NOT stop this; the CLI loads MCP servers via a separate path.
            opts["strict_mcp_config"] = True
        # Some kwargs (max_buffer_size, can_use_tool, strict_mcp_config) only exist on newer
        # SDKs. Strip any the installed SDK rejects, one at a time, so an older install still
        # loads (with reduced features) instead of failing to construct options at all.
        droppable = ["strict_mcp_config", "max_buffer_size", "can_use_tool",
                     "include_partial_messages"]
        while True:
            try:
                return ClaudeAgentOptions(**opts)
            except TypeError as e:
                victim = next((k for k in droppable if k in opts and k in str(e)), None)
                if victim is None:
                    victim = next((k for k in droppable if k in opts), None)
                if victim is None:
                    raise
                opts.pop(victim, None)

    def run(self):
        # Bounded auto-restart: even if _amain falls over entirely (e.g. the event loop
        # dies), bring it back so the overlay self-heals instead of becoming a zombie
        # window that never answers again.
        attempts = 0
        last_start = 0.0
        while self._running and attempts < 5:
            now = time.monotonic()
            if last_start and now - last_start > 180:
                attempts = 0           # survived a stable stretch → forget old failures, so
                                       # rare crashes spread over a long session don't add up
                                       # to a permanent "stopped" state (storm-based, not lifetime)
            last_start = now
            attempts += 1
            try:
                asyncio.run(self._amain())
                return                      # _amain returned cleanly (stop requested)
            except BaseException as e:  # pragma: no cover  (BaseException: e.g. CancelledError)
                self.ui.put(("error", f"worker restarting after: {type(e).__name__}: {e}"))
                self._client = None
                time.sleep(0.5)
            finally:
                # asyncio.run() closed this loop; null it so interrupt()/set_model() don't
                # schedule onto a dead loop before the next iteration sets a fresh one.
                self._loop = None
        if self._running:
            self.ui.put(("error", "Claude worker stopped after repeated failures — "
                                  "please restart the overlay."))

    async def _amain(self):
        self._loop = asyncio.get_running_loop()
        await self._open()
        while self._running:
            try:
                kind, payload = await self._loop.run_in_executor(None, self.req.get)
            except Exception:
                continue
            if kind == "stop":
                break
            # Each request is fully guarded: a failure here must never break the loop
            # (that would leave the UI waiting on a worker that's gone). Worst case we
            # reconnect and keep serving.
            try:
                if kind == "reset":
                    await self._close()
                    self._saw_stream = False
                    await self._open()
                    self.ui.put(("reset_done", None))
                elif kind == "ask":
                    await self._run_turn(payload)
                elif kind == "set_model":
                    if self._client is None:
                        self.ui.put(("error", "Not connected to Claude yet — can't switch model."))
                        self.ui.put(("status", ""))
                    else:
                        await self._do_set_model(self._client, payload)
            except asyncio.CancelledError:
                # a cancel (Stop / transport teardown) must not break the loop or be
                # mistaken for a fatal error — CancelledError is BaseException, not
                # Exception, so it would otherwise escape and kill the worker.
                self.ui.put(("turn_done", None))
            except BaseException as e:
                self.ui.put(("error", f"{type(e).__name__}: {e}"))
                self.ui.put(("turn_done", None))
                await self._reconnect()
        await self._close()

    async def _reconnect(self):
        """Tear down a broken client and stand up a fresh one so the next turn works.
        The conversation context is lost (new session), but the app stays alive instead
        of freezing on a dead transport."""
        self.ui.put(("system", "↻ Connection hiccup — reconnected with a fresh session."))
        try:
            await self._close()
        except Exception:
            pass
        self._saw_stream = False
        await self._open()

    async def _open(self):
        try:
            self._client = ClaudeSDKClient(options=self._make_options())
            # Bound the connect: a wedged transport (TLS MITM, half-open socket, CLI stuck on
            # a prompt) would otherwise hang the worker here forever, where no reconnect/restart
            # guard can reach it. A timeout degrades to the normal "couldn't start" path.
            # Run it as a tracked task so shutdown() can cancel it (see shutdown/_lifecycle_task).
            self._lifecycle_task = asyncio.ensure_future(self._client.connect())
            try:
                await asyncio.wait_for(self._lifecycle_task, CONNECT_TIMEOUT)
            finally:
                self._lifecycle_task = None
            self.ui.put(("ready", None))
            await self._emit_usage()
        except BaseException as e:   # incl. CancelledError — _open must never propagate
            self._client = None
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                self.ui.put(("error",
                    f"Connecting to Claude timed out after {CONNECT_TIMEOUT}s. The next "
                    "message will try again. (Check your network / `claude --version`.)"))
            elif isinstance(e, TypeError):   # ClaudeAgentOptions rejected a kwarg → SDK too old
                self.ui.put(("error",
                    f"Your claude-agent-sdk looks too old ({type(e).__name__}: {e}). "
                    "Update it:  pip install --upgrade claude-agent-sdk  (or run update.cmd)."))
            else:
                self.ui.put(("error",
                    f"Could not start Claude: {type(e).__name__}: {e}\n"
                    "Is the `claude` CLI installed and logged in? Run `claude --version` "
                    "in a terminal; if it's missing, run setup.cmd (or `irm "
                    "https://claude.ai/install.ps1 | iex`), then `claude` to /login."))

    async def _emit_usage(self):
        """Push current model + context-window usage % to the UI statusline."""
        # Capture the client we're measuring. A turn's finally schedules this against the
        # *current* client; if a Clear/reconnect swaps the client out while the (slow,
        # round-trips to the CLI) get_context_usage() is in flight, the result describes a
        # session that no longer exists. Emitting it would overwrite the fresh post-reset
        # baseline with the OLD conversation's high % — the "Clear didn't drop context" bug.
        client = self._client
        if client is None:
            return
        try:
            u = await asyncio.wait_for(client.get_context_usage(), timeout=6)
            if client is not self._client:   # reset/reconnect happened mid-flight → stale
                return
            if isinstance(u, dict):
                if u.get("model"):
                    self.ui.put(("model", u["model"]))
                if u.get("percentage") is not None:
                    self.ui.put(("ctx", u["percentage"]))
        except Exception:
            pass

    async def _close(self):
        # Null the handle FIRST so a disconnect that hangs (bounded below) can't leave the
        # rest of the worker pointing at a half-dead client.
        client, self._client = self._client, None
        if client is not None:
            self._lifecycle_task = asyncio.ensure_future(client.disconnect())
            try:
                await asyncio.wait_for(self._lifecycle_task, DISCONNECT_TIMEOUT)
            except Exception:
                pass
            finally:
                self._lifecycle_task = None

    async def _run_turn(self, payload):
        text, image_paths = payload if isinstance(payload, tuple) else (payload, [])
        if self._client is None:        # initial connect failed earlier — try once more
            await self._open()
        if self._client is None:
            self.ui.put(("error", "Not connected to Claude. Check `claude --version`."))
            self.ui.put(("turn_done", None))
            return
        agen = None
        try:
            await asyncio.wait_for(
                self._client.query(self._build_query(text, image_paths)), QUERY_TIMEOUT)
            blocks: dict = {}
            tool_active = False
            # Iterate the stream item-by-item under an idle timeout instead of a bare
            # `async for`: if the transport goes silent forever (dead CLI, wedged socket)
            # the turn would otherwise hold "thinking…" indefinitely. A gap longer than the
            # idle budget is treated as a broken transport → reconnect. Once a tool call is in
            # flight we switch to a much longer budget so a legitimately silent long-running
            # tool (a big build/test that streams nothing for minutes) isn't mistaken for dead.
            agen = self._client.receive_response()
            while True:
                budget = TOOL_IDLE_TIMEOUT if tool_active else RECV_IDLE_TIMEOUT
                try:
                    msg = await asyncio.wait_for(agen.__anext__(), budget)
                except StopAsyncIteration:
                    break
                if not tool_active and self._msg_has_tool(msg):
                    tool_active = True
                self._dispatch(msg, blocks)
        except asyncio.CancelledError:
            # Stop button / interrupt() / transport cancel — end this turn cleanly.
            # (BaseException, so it'd otherwise escape every `except Exception` and the
            # worker thread would die permanently.) Don't reconnect; shutdown is queue-driven.
            self.ui.put(("system", "⏹ stopped."))
        except (asyncio.TimeoutError, TimeoutError):
            # query() wedged or the stream went silent past the idle budget → the transport
            # is effectively dead; rebuild it so the next turn works instead of hanging here.
            self.ui.put(("error", "Claude stopped responding — reconnecting with a fresh session."))
            await self._reconnect()
        except BaseException as e:
            self.ui.put(("error", f"{type(e).__name__}: {e}"))
            # a decode/connection/process error means the transport is dead — the client
            # is unusable now, so rebuild it before the next turn instead of erroring
            # forever (the classic "it crashed and won't respond anymore" symptom).
            if isinstance(e, (CLIJSONDecodeError, CLIConnectionError, ProcessError, ClaudeSDKError)):
                await self._reconnect()
        finally:
            # Finalize the response stream. wait_for cancelling __anext__() does NOT close the
            # async generator, so without this the SDK's reader task / stdout pipe can be left
            # half-open (a leak, or a later disconnect() that hangs). Bounded so a broken close
            # can't reintroduce a hang.
            if agen is not None:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    try:
                        await asyncio.wait_for(aclose(), DISCONNECT_TIMEOUT)
                    except BaseException:
                        pass
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
        total = 0
        seen = set()
        for p in image_paths:
            if p in seen:           # dedupe repeated paths (same screenshot/paste twice)
                continue
            seen.add(p)
            if len(seen) > MAX_INLINE_IMAGES:   # cap count per turn
                failed += 1
                continue
            try:
                # Cap before reading: per-file AND aggregate, so a huge non-image file (per
                # file) or many accumulated attachments (aggregate) can't be read whole into
                # RAM and base64-expanded into one query.
                size = Path(p).stat().st_size
                if size > MAX_INLINE_IMAGE_BYTES or (total + size) > MAX_INLINE_TOTAL_BYTES:
                    failed += 1
                    continue
                data = Path(p).read_bytes()
            except Exception:
                failed += 1
                continue
            if not data:            # 0-byte / unreadable-as-empty → don't send a blank block
                failed += 1
                continue
            total += size
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

    @staticmethod
    def _msg_has_tool(msg):
        """True if this stream message starts/contains a tool_use — used to extend the
        receive idle budget so a long, silent tool isn't mistaken for a dead transport."""
        try:
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                if ev.get("type") == "content_block_start":
                    return ((ev.get("content_block") or {}).get("type") == "tool_use")
            elif isinstance(msg, AssistantMessage):
                return any(isinstance(b, ToolUseBlock)
                           for b in (getattr(msg, "content", None) or []))
        except Exception:
            pass
        return False

    def _dispatch(self, msg, blocks: dict):
        # The contents are untrusted CLI stream-json — a single malformed frame
        # (non-dict block value, unhashable index, content=None, …) must never abort
        # the turn (which would also skip the reconnect logic). Skip the bad frame and
        # keep streaming.
        try:
            self._dispatch_inner(msg, blocks)
        except Exception:
            pass

    def _dispatch_inner(self, msg, blocks: dict):
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
                    b = blocks.get(idx)
                    if not isinstance(b, dict):   # corrupted/missing → reset to a fresh buf
                        b = {"type": "tool_use", "name": None, "buf": ""}
                        blocks[idx] = b
                    b["buf"] = (b.get("buf") or "") + (d.get("partial_json") or "")
            elif t == "content_block_stop":
                idx = ev.get("index")
                b = blocks.get(idx)
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    try:
                        inp = json.loads(b.get("buf") or "{}")
                    except Exception:
                        inp = {}
                    self.ui.put(("tool", (b.get("name") or "tool", inp)))
        elif isinstance(msg, AssistantMessage):
            if getattr(msg, "model", None):
                self.ui.put(("model", msg.model))
            if not self._saw_stream:
                for blk in (getattr(msg, "content", None) or []):
                    if isinstance(blk, TextBlock):
                        self.ui.put(("delta", blk.text))
                    elif isinstance(blk, ToolUseBlock):
                        self.ui.put(("tool", (blk.name, blk.input)))
        elif isinstance(msg, ResultMessage):
            self.ui.put(("result", {"cost": getattr(msg, "total_cost_usd", None),
                                    "is_error": getattr(msg, "is_error", False)}))


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


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class Overlay:
    def __init__(self):
        self.ui_q: "queue.Queue" = queue.Queue()
        _ensure_shot_dir()          # before the worker, so a bad TEMP can't crash us mid-startup
        self.worker = ClaudeWorker(self.ui_q)
        self.worker.start()

        self.auto_shot = AUTO_SCREENSHOT_DEFAULT
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
        self._drag = (0, 0)
        self._resize = None
        self._round_after = None
        self._last_cfg_size = None   # last (w,h) we re-applied the window region for
        self._capture_excluded = False   # set once WDA_EXCLUDEFROMCAPTURE is applied
        self._update_available = None     # set to the newer version string if one exists

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
        self.root.after(180, self._exclude_from_capture)
        self.root.after(1200, self._check_for_update)

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

    def _exclude_from_capture(self):
        """Ask DWM to omit the overlay from screen captures so our own window never
        appears in the screenshots we send Claude. If it succeeds, capture() can skip
        the withdraw()+sleep() dance entirely (no flicker, no UI freeze)."""
        try:
            self.root.update_idletasks()
            hwnd = _user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            if _user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                self._capture_excluded = True
        except Exception:
            self._capture_excluded = False

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
        self.toggle_screen.configure(text=("◉  auto-screenshot" if on else "○  auto-screenshot"),
                                     fg=(T["accent"] if on else T["muted"]))

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
        """Cheap, non-blocking probe (no OLE render): is there image/file content on the
        clipboard? Lets the UI thread decide whether to spin up the (possibly slow)
        grabclipboard() read without blocking on it first."""
        try:
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
                if ORB_FLOAT and getattr(self, "_orb_mask", None) is not None \
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
        at_bottom = self.chat.yview()[1] > 0.999
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._user_bubble(text), pady=self.px(3))
        self.chat.insert("end", "\n")
        try:
            self.chat.tag_remove("current_ah", "1.0", "end")   # a new turn starts; old header
        except Exception:                                       # is no longer the "active" one
            pass
        self._claude_header = False
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
        """A right-aligned rounded chat bubble (drawn on a full-width canvas)."""
        text = self._clip_bubble(text)
        full = max(self.px(200), self.chat.winfo_width() - 2 * self.px(18))
        maxw = max(self.px(140), int(full * 0.74))
        padx, pady, rad = self.px(13), self.px(9), self.px(14)
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0)
        # Snapshot the body font at *current* zoom into a private Font. The shared self.f_body
        # is reconfigured live on Ctrl +/−; if this canvas (fixed pixel width/height) kept using
        # it, a later zoom would regrow the text inside an unchanged box and clip/overflow it.
        body_font = tkfont.Font(root=self.root, font=self.f_body)
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
        self._prune_chat()

    def _tool_chip(self, name, arg):
        """A compact rounded Claude-style tool pill embedded in the chat."""
        icon = TOOL_ICONS.get(name, "●")
        # Private font snapshots at current zoom (see _user_bubble): this chip is a fixed-size
        # canvas, so it must not track the shared fonts when the user later zooms.
        fi = tkfont.Font(root=self.root, font=self.f_small)
        fn = tkfont.Font(root=self.root, font=self.f_chip)
        fa = tkfont.Font(root=self.root, font=self.f_small)
        padx, gap, h = self.px(11), self.px(7), self.px(26)
        iw, nw = fi.measure(icon), fn.measure(name)
        aw = fa.measure(arg) if arg else 0
        w = padx + iw + gap + nw + ((gap + aw) if arg else 0) + padx
        c = tk.Canvas(self.chat, width=w, height=h, bg=T["bg"], highlightthickness=0)
        c._overlay_fonts = [fi, fn, fa]                 # keep refs so Tk won't GC them
        round_rect(c, 1, 1, w - 1, h - 1, self.px(8), fill=T["tool_bg"],
                   outline=T["border"], width=1)
        x, cy = padx, h / 2 - self.px(1)
        c.create_text(x, cy, text=icon, fill=T["accent"], font=fi, anchor="w"); x += iw + gap
        c.create_text(x, cy, text=name, fill=T["muted"], font=fn, anchor="w"); x += nw + gap
        if arg:
            c.create_text(x, cy, text=arg, fill=T["faint"], font=fa, anchor="w")
        return c

    def add_sys(self, text):
        self._ins("\n" + ("" if text is None else str(text)) + "\n", "sys")

    def add_err(self, text):
        self._ins("\n⚠  " + ("" if text is None else str(text)) + "\n", "err")

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
            shots, _ = self._grab_shots(mons)
        except BaseException:
            shots = None
        finally:
            self.ui_q.put(("precapture_done", shots))

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
                    p = SHOT_DIR / f"shot_{ts}_m{i}.png"
                    img.save(p)
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
            shots, err = self._grab_shots(mons)
        finally:
            if do_hide:
                self.root.deiconify()
                self.root.overrideredirect(True)
                self.root.geometry(geo)
                self.root.attributes("-topmost", True)
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
            for p in SHOT_DIR.glob("shot_*.png"):
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

    def reset(self):
        # Interrupt any in-flight turn FIRST. Otherwise the worker is blocked in
        # receive_response() and the reset just queues behind it — meanwhile the tail
        # of the old reply keeps streaming deltas into the chat we just cleared.
        self.worker.interrupt()
        self.chat.delete("1.0", "end")
        self._claude_header = False
        # Clear the shown % immediately so the OLD conversation's usage can't linger while the
        # async reset (close + reconnect) runs; the new session's true baseline arrives via the
        # worker's post-_open _emit_usage.
        self._ctx_pct = None
        self._refresh_statusline()
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

    def _show_window(self):
        self.root.deiconify()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.after(40, lambda: (self.root.focus_force(), self.entry.focus_set()))
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
        # Whatever happens in here, the pump MUST reschedule itself — an unhandled
        # exception that skipped the next after() used to silently freeze the whole UI
        # (window still drawn, but no replies, no events ever again). The finally
        # guarantees the next tick; per-message guarding keeps one bad render from
        # dropping the rest of the queue.
        deadline = time.monotonic() + 0.012   # ~12ms budget per tick, so the drain can never
        handled = 0                            # monopolize Tk: a fast stream yields back for
        pending_delta = []                     # repaint / clicks / hotkey between slices.

        def flush_delta():
            if pending_delta:
                joined = "".join(pending_delta)
                pending_delta.clear()
                try:
                    self._handle("delta", joined)
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
                if kind == "delta":            # coalesce adjacent deltas into one insert
                    pending_delta.append("" if payload is None else str(payload))
                    continue
                flush_delta()                  # preserve ordering around non-delta messages
                try:
                    self._handle(kind, payload)
                except Exception as e:
                    try:
                        self.add_err(f"UI hiccup handling '{kind}': {type(e).__name__}: {e}")
                    except Exception:
                        pass
            flush_delta()
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
        elif kind == "result":
            # the SDK reports a turn that ended in error here even when no exception
            # was raised on our side; surface it instead of dropping it silently.
            if isinstance(payload, dict) and payload.get("is_error"):
                self.add_err("The last turn ended with an error.")
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
        elif kind == "system":
            self.add_sys(str(payload))
        elif kind == "update":
            self._update_available = str(payload)
            self.add_sys(f"🔔 Update available: v{payload} (you have v{__version__}). "
                         "Close the overlay and run update.cmd (or: git pull) to upgrade.")
            self._refresh_statusline()

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

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    set_dpi_awareness()
    try:
        Overlay().run()
    except KeyboardInterrupt:
        sys.exit(0)
